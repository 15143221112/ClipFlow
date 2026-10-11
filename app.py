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


# ---------------- 请求模型
class TaskReq(BaseModel):
    url: str
    order: str = "order"          # 主页批量：order 顺序 / random 随机
    cookie: str = ""              # 批量页直接填写的抖音 cookie（可选，优先于设置）


class SingleReq(BaseModel):
    url: str
    cookie: str = ""


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
    """主页批量：采集全部作品链接入库 → 顺序/随机逐个下载转写。"""
    url = req.url.strip()
    if not _validate_douyin_url(url):
        return JSONResponse({"ok": False, "msg": "链接已被拦截：仅支持 douyin.com 域名，拒绝非抖音/IP/恶意链接"}, status_code=400)
    if req.cookie.strip():                       # 批量页填写的 cookie 优先，回写配置
        cfg.set_value("douyin.cookie", req.cookie.strip())
    sec_user_id = f2_worker.resolve_sec_user_id(url)
    if not sec_user_id:
        return JSONResponse({"ok": False, "msg": "无法从链接解析 sec_user_id（需 /user/xxx 格式）"}, status_code=400)
    cookie = cfg.get_section("douyin", "cookie") or ""
    if not cookie:
        return JSONResponse({"ok": False, "msg": "请提供抖音 cookie（批量页填写或设置页录入）"}, status_code=400)
    tid = worker.manager.new_task(url, sec_user_id, task_type="batch", order=req.order or "order")
    asyncio.create_task(worker.run_pipeline(tid, sec_user_id, order=req.order or "order"))
    return {"ok": True, "task_id": tid}


@app.post("/api/download_single")
async def download_single(req: SingleReq):
    """单链接下载：输入单个作品链接 → 入库(single) → 下载转写。"""
    url = req.url.strip()
    if not _validate_douyin_url(url):
        return JSONResponse({"ok": False, "msg": "链接已被拦截：仅支持 douyin.com 域名，拒绝非抖音/IP/恶意链接"}, status_code=400)
    if req.cookie.strip():                       # 单链接页填写的 cookie 优先，回写配置
        cfg.set_value("douyin.cookie", req.cookie.strip())
    aweme_id = f2_worker.resolve_aweme_id(url)
    if not aweme_id:
        return JSONResponse({"ok": False, "msg": "无法从链接解析作品ID（需 /video/xxx 或含数字ID）"}, status_code=400)
    cookie = cfg.get_section("douyin", "cookie") or ""
    if not cookie:
        return JSONResponse({"ok": False, "msg": "请提供抖音 cookie（单链接页填写或设置页录入）"}, status_code=400)
    tid = worker.manager.new_task(url, "", task_type="single")
    asyncio.create_task(worker.process_single(tid, aweme_id))
    return {"ok": True, "task_id": tid}


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
