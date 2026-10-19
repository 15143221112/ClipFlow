"""
douyin-distiller Web 主程序（FastAPI）。
启动：uvicorn app:app --host 0.0.0.0 --port 9009
"""
import asyncio
import os
import traceback
import ipaddress
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config as cfg
import db
import f2_worker
import worker
import engine_ytdlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = cfg.get_section("download", "root") or os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

app = FastAPI(title="douyin-distiller-web")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
# 挂载下载目录，供查看/下载视频：http://<host>:9009/data/<task>/<file>.mp4
app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")

db.init()


# ---------------- 安全校验
def _validate_douyin_url(url: str) -> bool:
    """只允许 http(s) + douyin.com 域名；拒绝 IP/内网/其他域（防 SSRF / 恶意链接）。"""
    try:
        p = urlparse(url.strip())
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    # 拒绝 IP（含内网/回环/链接解析）
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    # 只允许 douyin.com 及其子域
    return host == "douyin.com" or host.endswith(".douyin.com")


_PLATFORM_DOMAINS = {
    "douyin": ("douyin.com",),
    "tiktok": ("tiktok.com",),
    "twitter": ("x.com", "twitter.com"),
    "weibo": ("weibo.com", "weibo.cn"),
}


def _validate_platform_url(platform: str, url: str) -> bool:
    """按平台校验链接；ytdlp 用引擎自身的宽松 SSRF 校验。"""
    if platform == "ytdlp":
        return engine_ytdlp._safe_url(url) and engine_ytdlp._public_host((urlparse(url).hostname or "").lower())
    try:
        p = urlparse(url.strip())
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    domains = _PLATFORM_DOMAINS.get(platform, ())
    return any(host == d or host.endswith("." + d) for d in domains)


# ---------------- 请求模型
class TaskReq(BaseModel):
    url: str
    order: str = "order"          # 主页批量：order 顺序 / random 随机
    cookie: str = ""              # 批量页直接填写的抖音 cookie（可选，优先于设置）


class SingleReq(BaseModel):
    url: str
    cookie: str = ""
    engine: str = "f2"          # f2 / ytdlp
    platform: str = "douyin"    # f2: douyin/tiktok/twitter/weibo；ytdlp: any


class SettingReq(BaseModel):
    # 支持部分更新：{"douyin.cookie": "...", "asr.engine": "local", ...}
    values: dict


class ReprocessReq(BaseModel):
    aweme_id: str
    mode: str = "transcribe"   # transcribe=只重转写(用本地音频) / download=重新下载+转写


# ---------------- 前端页面
@app.get("/", response_class=HTMLResponse)
async def index():
    path = os.path.join(BASE_DIR, "static", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ---------------- 任务
@app.post("/api/task")
async def create_task(req: TaskReq):
    """主页批量：采集全部作品链接入库 → 顺序/随机逐个下载转写。
    支持三类链接（自动识别 showTab）：
      - 博主主页          https://www.douyin.com/user/xxx
      - 自己主页/点赞     https://www.douyin.com/user/self?showTab=like  （需登录 cookie）
      - 自己主页/收藏     https://www.douyin.com/user/self?showTab=favorite （需登录 cookie）
    """
    url = req.url.strip()
    if not _validate_douyin_url(url):
        return JSONResponse({"ok": False, "msg": "链接已被拦截：仅支持 douyin.com 域名，拒绝非抖音/IP/恶意链接"}, status_code=400)
    if req.cookie.strip():                       # 批量页填写的 cookie 优先，回写配置
        cfg.set_value("douyin.cookie", req.cookie.strip())
    cookie = cfg.get_section("douyin", "cookie") or ""
    if not cookie:
        return JSONResponse({"ok": False, "msg": "请提供抖音 cookie（批量页填写或设置页录入）"}, status_code=400)

    collect_type = f2_worker.resolve_collect_type(url)
    is_self = f2_worker.is_self_url(url)
    sec_user_id = f2_worker.resolve_sec_user_id(url)
    if is_self or not sec_user_id:
        # user/self 是登录态占位，用 f2 SecUserIdFetcher 带 cookie 解析真实 sec_uid
        sec_user_id = await f2_worker.resolve_self_sec_user_id(url)
        if not sec_user_id:
            return JSONResponse({"ok": False, "msg": "无法解析用户身份（user/self 需有效登录 cookie，或改贴完整主页链接）"}, status_code=400)

    tid = worker.manager.new_task(url, sec_user_id, task_type="batch", order=req.order or "order",
                                  collect_type=collect_type)
    asyncio.create_task(worker.run_pipeline(tid, sec_user_id, order=req.order or "order",
                                            collect_type=collect_type))
    return {"ok": True, "task_id": tid, "collect_type": collect_type}


@app.post("/api/download_single")
async def download_single(req: SingleReq):
    """单链接下载：按引擎/平台分发。
    - engine=f2 + platform=douyin/tiktok/twitter/weibo：作品 ID 下载
    - engine=ytdlp：任意公开站 URL 下载（YouTube/B站 等）"""
    url = req.url.strip()
    engine = (req.engine or "f2").strip().lower()
    platform = (req.platform or "douyin").strip().lower()
    if engine not in ("f2", "ytdlp"):
        return JSONResponse({"ok": False, "msg": "engine 仅支持 f2 / ytdlp"}, status_code=400)
    if engine == "f2" and platform not in f2_worker.SUPPORTED_PLATFORMS:
        return JSONResponse({"ok": False, "msg": f"F2 平台仅支持 {f2_worker.SUPPORTED_PLATFORMS}"}, status_code=400)
    if not _validate_platform_url("ytdlp" if engine == "ytdlp" else platform, url):
        return JSONResponse({"ok": False, "msg": f"链接已被拦截：平台[{platform}] 仅允许合法公开域名，拒绝 IP/内网/恶意链接"}, status_code=400)
    if req.cookie.strip():                       # 页面填写的 cookie 优先，回写对应平台配置
        cfg.set_value(f"{platform}.cookie", req.cookie.strip())
    tid = worker.manager.new_task(url, "", task_type="single")
    if engine == "ytdlp":
        asyncio.create_task(worker.process_single(tid, url, platform="", engine="ytdlp"))
        return {"ok": True, "task_id": tid, "engine": "ytdlp"}
    obj_id = f2_worker.resolve_id_platform(platform, url)
    if not obj_id:
        return JSONResponse({"ok": False, "msg": f"无法从链接解析 {platform} 作品ID"}, status_code=400)
    asyncio.create_task(worker.process_single(tid, obj_id, platform=platform, engine="f2"))
    return {"ok": True, "task_id": tid, "engine": "f2", "platform": platform}


@app.post("/api/reprocess")
async def reprocess(req: ReprocessReq):
    """历史记录重试：对已入库作品重新转写（transcribe，用本地音频，无需 cookie）
    或重新下载+转写（download，需 cookie）。"""
    aweme_id = (req.aweme_id or "").strip()
    mode = (req.mode or "transcribe").strip().lower()
    if mode not in ("transcribe", "download"):
        return JSONResponse({"ok": False, "msg": "mode 仅支持 transcribe / download"}, status_code=400)
    if not aweme_id:
        return JSONResponse({"ok": False, "msg": "缺少 aweme_id"}, status_code=400)
    tid = worker.manager.new_task("", "", task_type="single")
    asyncio.create_task(worker.reprocess_single(aweme_id, mode))
    return {"ok": True, "task_id": tid, "mode": mode}


@app.delete("/api/video/{aweme_id}")
async def delete_video(aweme_id: str, delete_files: int = 1):
    """删除一条历史记录，默认同时删除本地视频/音频文件（及其空目录）。"""
    row = db.get_by_aweme(aweme_id)
    if not row:
        return JSONResponse({"ok": False, "msg": "记录不存在"}, status_code=404)
    removed = []
    if delete_files:
        for p in (row.get("local_path"), row.get("audio_path")):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    removed.append(p)
                except Exception:
                    pass
        # 清理由此留下的空任务目录
        for p in list(removed):
            d = os.path.dirname(p)
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except Exception:
                pass
    db.delete_video(aweme_id)
    return {"ok": True, "removed_files": removed}


class BatchDeleteReq(BaseModel):
    aweme_ids: list = []
    delete_files: int = 1


@app.post("/api/videos/batch_delete")
async def batch_delete_videos(req: BatchDeleteReq):
    """批量删除多条历史记录。aweme_ids 非空，默认同时删除本地视频/音频文件。"""
    ids = [str(x).strip() for x in (req.aweme_ids or []) if str(x).strip()]
    if not ids:
        return JSONResponse({"ok": False, "msg": "缺少要删除的记录"}, status_code=400)
    # 先定位待删记录的本地文件
    rows = db.list_rows_by_aweme(ids)
    removed = []
    if req.delete_files:
        for row in rows:
            for p in (row.get("local_path"), row.get("audio_path")):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                        removed.append(p)
                    except Exception:
                        pass
        # 清理由此留下的空任务目录
        for p in list(removed):
            d = os.path.dirname(p)
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except Exception:
                pass
    n = db.delete_videos_batch(ids)
    return {"ok": True, "deleted": n, "removed_files": removed}


# ---------------- 历史查询
@app.get("/api/videos")
async def list_videos(status: str = None, source: str = None, q: str = None,
                      author: str = None, date_from: int = None, date_to: int = None,
                      min_duration: float = None, max_duration: float = None,
                      page: int = None, page_size: int = None,
                      limit: int = 200, offset: int = 0):
    if page is not None and page_size:
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size
        limit = page_size
    limit = min(limit, 500)
    rows, total = db.list_videos(
        limit=limit, offset=offset,
        status=status, source=source, q=q, author=author,
        date_from=date_from, date_to=date_to,
        min_duration=min_duration, max_duration=max_duration,
    )
    total_pages = max(1, (total + limit - 1) // limit) if limit else 1
    cur_page = page or (offset // limit + 1 if limit else 1)
    return {"ok": True, "items": rows, "total": total, "page": cur_page,
            "page_size": limit, "total_pages": total_pages, "stats": db.stats()}


@app.get("/api/export")
async def export_videos(status: str = None, source: str = None, q: str = None,
                        author: str = None, date_from: int = None, date_to: int = None,
                        min_duration: float = None, max_duration: float = None):
    """按当前查询条件导出 CSV（不是全量导出）。"""
    import csv
    import io
    rows, _ = db.list_videos(
        limit=100000, offset=0,
        status=status, source=source, q=q, author=author,
        date_from=date_from, date_to=date_to,
        min_duration=min_duration, max_duration=max_duration,
    )
    header = ["aweme_id", "title", "author_nickname", "source", "status",
              "publish_time", "duration", "transcript", "local_url",
              "audio_url", "douyin_url", "created_at"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        r = dict(r)
        r["transcript"] = (r.get("transcript") or "").replace("\n", " ")
        w.writerow(r)
    # utf-8-sig BOM，保证 Excel 打开不乱码
    content = "\ufeff" + buf.getvalue()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=douyin_distiller_export.csv"},
    )


@app.get("/api/tasks")
async def list_tasks():
    return {"tasks": worker.manager.all()}


@app.get("/api/task/{task_id}")
async def task_detail(task_id: str):
    t = worker.manager.get(task_id)
    if not t:
        return JSONResponse({"ok": False, "msg": "任务不存在"}, status_code=404)
    return t


@app.post("/api/task/{task_id}/pause")
async def pause_task(task_id: str):
    """暂停任务（batch/single）。已完成的/已删除的不能暂停。"""
    ok = worker.manager.pause(task_id)
    if not ok:
        t = worker.manager.get(task_id)
        if not t:
            return JSONResponse({"ok": False, "msg": "任务不存在"}, status_code=404)
        return JSONResponse({"ok": False, "msg": "任务不可暂停（已完成/失败/已删除）"}, status_code=400)
    return {"ok": True, "msg": "已暂停"}


@app.post("/api/task/{task_id}/resume")
async def resume_task(task_id: str):
    """恢复已暂停的任务，从未完成的条目继续。"""
    ok = worker.manager.resume(task_id)
    if not ok:
        t = worker.manager.get(task_id)
        if not t:
            return JSONResponse({"ok": False, "msg": "任务不存在"}, status_code=404)
        return JSONResponse({"ok": False, "msg": "任务不是暂停状态，无法恢复"}, status_code=400)
    return {"ok": True, "msg": "已恢复"}


@app.delete("/api/task/{task_id}")
async def delete_task(task_id: str, delete_files: int = 1):
    """删除任务：从内存移除，可选同时删除该任务下载的视频/音频文件。"""
    ok, removed = worker.manager.remove(task_id, delete_files=bool(delete_files))
    if not ok:
        return JSONResponse({"ok": False, "msg": "任务不存在"}, status_code=404)
    return {"ok": True, "removed_files": removed}


@app.post("/api/task/{task_id}/cookie")
async def update_task_cookie(task_id: str, request: Request):
    """就地更新任务 cookie：写全局 douyin.cookie，若任务因 cookie 过期暂停则自动恢复续跑。
    无需重新创建任务/重新采集——已入库部分保留，从剩余未处理条目继续。"""
    import json as _json
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "请求体需为 JSON"}, status_code=400)
    cookie = (body.get("cookie") or "").strip()
    if not cookie:
        return JSONResponse({"ok": False, "msg": "cookie 不能为空"}, status_code=400)

    # 写全局配置（后续采集/下载自动用新 cookie）
    cfg.set_value("douyin.cookie", cookie)

    t = worker.manager.get(task_id)
    if not t:
        return {"ok": True, "msg": "cookie 已更新"}
    # 若任务正因 cookie 过期暂停 → 自动恢复续跑
    if t.get("paused"):
        worker.manager.resume(task_id)
        return {"ok": True, "msg": "cookie 已更新，任务已恢复", "task_status": "running"}
    return {"ok": True, "msg": "cookie 已更新，任务无需恢复", "task_status": t.get("status", "")}


# ---------------- 设置
def _mask(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return "*" * len(v)
    return v[:4] + "*" * (len(v) - 8) + v[-4:]


@app.get("/api/settings")
async def get_settings():
    s = cfg.get()
    # 脱敏输出
    safe = json_safe(s)
    if safe.get("douyin", {}).get("cookie"):
        safe["douyin"]["cookie"] = _mask(safe["douyin"]["cookie"])
    for k in ("xfyun", "feishu"):
        sec = safe.get("asr", {}).get(k, {})
        for sk in ("api_key", "api_secret", "app_secret"):
            if sec.get(sk):
                sec[sk] = _mask(sec[sk])
    ft = safe.get("feishu_table", {})
    if ft.get("app_secret"):
        ft["app_secret"] = _mask(ft["app_secret"])
    llm = safe.get("llm", {})
    if llm.get("api_key"):
        llm["api_key"] = _mask(llm["api_key"])
    return safe


@app.post("/api/settings")
async def set_settings(req: SettingReq):
    try:
        cfg.set_many(req.values)
        return {"ok": True, "msg": "已保存"}
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=500)


def json_safe(obj):
    import copy
    return copy.deepcopy(obj)


# ---------------- 健康检查
@app.get("/health")
async def health():
    return {"ok": True, "service": "douyin-distiller-web"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=9009, reload=False)
