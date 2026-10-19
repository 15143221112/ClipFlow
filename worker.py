"""
流水线编排 + 任务进度管理 + 数据库持久化：
  单链接下载：输入作品链接 → 入库(single) → 下载 → 提取音频 → 转写(纠错) → 写飞书 → 更新库
  主页批量：输入主页链接 → 采集全部作品链接入库(batch) → 按顺序/随机逐个下载转写
  历史查询：所有作品记录落 SQLite，cookie 过期后仍可基于已存链接重新处理。

单例 TaskManager 保存内存实时进度（前端轮询读取）；db.py 持久化历史。
"""
import asyncio
import os
import random
import subprocess
import time
import uuid

import config as cfg
import db
import f2_worker
import engine_ytdlp
from asr_client import transcribe
from lark_client import LarkTableClient

ROOT = cfg.get_section("download", "root") or "/opt/douyin-web/data"

# 飞书建字段用
FIELDS = (cfg.get_section("feishu_table", "fields") or {
    "video_title": "text", "video_url": "url", "publish_time": "datetime",
    "like_count": "number", "comment_count": "number", "share_count": "number",
    "collect_count": "number", "duration": "number", "transcript": "text",
    "status": "select", "local_file_path": "text", "author_nickname": "text",
    "desc": "text", "aweme_id": "text",
})


class TaskManager:
    def __init__(self):
        self.tasks = {}

    def new_task(self, url: str, sec_user_id: str, task_type: str = "batch",
                 order: str = "order", collect_type: str = "post") -> str:
        tid = uuid.uuid4().hex[:12]
        self.tasks[tid] = {
            "task_id": tid, "url": url, "sec_user_id": sec_user_id or "",
            "task_type": task_type,            # batch / single
            "order": order,                    # order 顺序 / random 随机
            "status": "pending",
            "paused": False,                   # 是否被用户暂停
            "cancelled": False,                # 是否被用户取消/删除
            "error": "", "created_at": time.time(),
            "nickname": "", "videos": [],
            "_collect_type": collect_type,     # batch 采集类型 post/like/favorite
            "_waiting_cookie": False,          # 是否因 cookie 过期而暂停等待新 cookie
        }
        return tid

    def get(self, task_id):
        return self.tasks.get(task_id)

    def all(self):
        return list(self.tasks.values())

    def pause(self, task_id):
        """暂停任务：置 paused=True，worker 循环下次检查到即停（不删数据，可恢复）。"""
        t = self.tasks.get(task_id)
        if not t:
            return False
        if t["status"] in ("done", "failed", "cancelled"):
            return False
        t["paused"] = True
        t["status"] = "paused"
        return True

    def resume(self, task_id):
        """恢复已暂停任务：置 paused=False，重新调度 worker 继续。
        - 普通暂停：batch 重新采集（用 _collect_type），跳过已完成条目续跑
        - cookie 过期暂停（_waiting_cookie）：清标记，跳过重新采集，直接续跑已入库的剩余条目"""
        t = self.tasks.get(task_id)
        if not t or not t.get("paused"):
            return False
        t["paused"] = False
        t["status"] = "running"
        # cookie 等待恢复：已入库部分保留，跳过重新采集
        skip_collect = bool(t.get("_waiting_cookie"))
        t["_waiting_cookie"] = False
        if t.get("task_type") == "batch":
            asyncio.create_task(run_pipeline(task_id, t.get("sec_user_id", ""),
                                             order=t.get("order", "order"),
                                             collect_type=t.get("_collect_type", "post"),
                                             resume=True, skip_collect=skip_collect))
        else:
            asyncio.create_task(process_single(task_id, t.get("_target", ""),
                                               platform=t.get("_platform", "douyin"),
                                               engine=t.get("_engine", "f2")))
        return True

    def remove(self, task_id, delete_files: bool = True):
        """删除任务：从内存移除。delete_files 时同时删除该任务下载目录的文件。"""
        t = self.tasks.pop(task_id, None)
        if not t:
            return False, []
        t["cancelled"] = True
        removed = []
        if delete_files:
            # 删除该任务目录下的视频/音频文件
            task_dir = os.path.join(ROOT, task_id)
            for name in (t.get("videos") or []):
                for key in ("local_path", "audio_path"):
                    p = name.get(key)
                    if p and os.path.exists(p):
                        try:
                            os.remove(p); removed.append(p)
                        except Exception:
                            pass
            try:
                if os.path.isdir(task_dir) and not os.listdir(task_dir):
                    os.rmdir(task_dir)
            except Exception:
                pass
        return True, removed


manager = TaskManager()


async def _try_nickname(sec_user_id: str, collect_type: str) -> str:
    """点赞/收藏列表可能不含当前用户昵称，尝试从用户 profile 获取。
    失败返回空（不阻塞主流程）。"""
    if collect_type in ("like", "favorite") and sec_user_id:
        try:
            meta = await f2_worker.fetch_single_meta_platform("douyin", sec_user_id)
            if meta and meta.get("author_nickname"):
                return meta["author_nickname"]
        except Exception:
            pass
    return ""


def _feishu_client():
    t = cfg.get_section("feishu_table")
    if not t or not t.get("app_id") or not t.get("app_secret") or not t.get("base_token"):
        return None
    return LarkTableClient(t["app_id"], t["app_secret"], t["base_token"], t.get("table_id", ""))


def _local_video_url(local_path: str) -> str:
    """本地文件路径 -> 可通过 Web 访问/下载的 URL。"""
    if not local_path:
        return ""
    try:
        rel = os.path.relpath(local_path, ROOT)
    except ValueError:
        return ""
    if rel.startswith("..") or os.path.isabs(rel):
        return ""
    from urllib.parse import quote
    base = cfg.get_section("server", "base_url") or "http://192.168.1.5:9009"
    return f"{base}/data/{quote(rel)}"


def _row_for(meta: dict, status: str) -> dict:
    """meta + 状态 -> 飞书多维表一行字段值。"""
    local_url = _local_video_url(meta.get("local_path", ""))
    video_url_val = local_url or meta.get("video_url", "")
    return {
        "文本": meta.get("title", ""),
        "video_title": meta.get("title", ""),
        "video_url": {"text": meta.get("title", ""), "link": video_url_val},
        "publish_time": int(meta.get("publish_time") or 0),
        "like_count": meta.get("like_count", 0),
        "comment_count": meta.get("comment_count", 0),
        "share_count": meta.get("share_count", 0),
        "collect_count": meta.get("collect_count", 0),
        "duration": meta.get("duration", 0),
        "transcript": meta.get("transcript", ""),
        "status": status,
        "local_file_path": meta.get("local_path", ""),
        "author_nickname": meta.get("author_nickname", ""),
        "desc": meta.get("desc", ""),
        "aweme_id": meta.get("aweme_id", ""),
    }


def _extract_audio(mp4: str) -> str:
    """从 mp4 提取音轨为同目录 mp3，返回路径（失败返回空）。"""
    if not mp4 or not os.path.exists(mp4):
        return ""
    base, _ = os.path.splitext(mp4)
    out = base + ".mp3"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", mp4, "-vn", "-acodec", "libmp3lame", "-q:a", "4", out],
            capture_output=True, check=True, timeout=300,
        )
        return out if os.path.exists(out) else ""
    except Exception:
        return ""


def _set_meta(task, meta, **updates):
    for v in task["videos"]:
        if v.get("aweme_id") == meta.get("aweme_id"):
            v.update(updates)
            return
    task["videos"].append({**meta, **updates})


def _merged_meta(task, meta):
    for v in task["videos"]:
        if v.get("aweme_id") == meta.get("aweme_id"):
            return v
    return meta


def _record_for(meta, source):
    """构造入库记录（不含 _raw）。"""
    return {
        "aweme_id": str(meta.get("aweme_id", "")),
        "title": meta.get("title", ""),
        "douyin_url": meta.get("video_url", ""),
        "author_nickname": meta.get("author_nickname", ""),
        "source": source,
        "duration": meta.get("duration", 0),
        "publish_time": meta.get("publish_time", 0),
    }


async def _process_one(meta, task, user_path, lark, engine):
    """单条作品完整流程：下载 → 提取音频 → 转写(纠错) → 写飞书 → 更新库。"""
    aweme = str(meta.get("aweme_id") or "")
    # 下载（按引擎分发：f2 多平台 / yt-dlp 通用）
    _set_meta(task, meta, download_status="downloading")
    path = ""
    try:
        if meta.get("_engine") == "ytdlp":
            path = await asyncio.to_thread(engine_ytdlp.download, meta.get("video_url") or "", user_path)
        else:
            path = await f2_worker.download_one_platform(meta.get("_platform", "douyin"), meta, user_path)
    except Exception as e:
        path = ""
        _set_meta(task, meta, download_status="failed", status="失败")
        db.update_video(aweme, status="失败", download_status="failed", transcript_status="failed")
        return
    if not path:
        _set_meta(task, meta, download_status="failed", status="失败")
        db.update_video(aweme, status="失败", download_status="failed")
        return
    lurl = _local_video_url(path)
    _set_meta(task, meta, local_path=path, download_status="downloaded", local_url=lurl)
    db.update_video(aweme, status="已下载", download_status="downloaded",
                    local_path=path, local_url=lurl)

    # 提取音频
    audio = _extract_audio(path)
    if audio:
        alink = _local_video_url(audio)
        _set_meta(task, meta, audio_path=audio, audio_url=alink)
        db.update_video(aweme, audio_path=audio, audio_url=alink)

    # 转写（whisper + 标点/术语纠错）
    _set_meta(task, meta, transcript_status="transcribing")
    text = ""
    try:
        text = await transcribe(path, engine=engine)
        _set_meta(task, meta, transcript=text, transcript_status="done")
    except Exception as e:
        _set_meta(task, meta, transcript_status="failed", transcript=f"[转写失败]{e}")
        db.update_video(aweme, transcript_status="failed", transcript=f"[转写失败]{e}")
    st = "转写完成" if text else "已下载"
    _set_meta(task, meta, status=st)
    db.update_video(aweme, status=st, transcript_status="done" if text else "downloaded",
                    transcript=text)

    # 写飞书（存在则更新该记录，不存在则新建——保证旧错字被新文本覆盖）
    if lark and text:
        try:
            lark.upsert_by_aweme([(aweme, _row_for(_merged_meta(task, meta), st))])
            _set_meta(task, meta, write_status="ok")
        except Exception as e:
            _set_meta(task, meta, write_status="failed", write_error=str(e))


async def run_pipeline(task_id: str, sec_user_id: str, order: str = "order",
                       collect_type: str = "post", nickname: str = "", resume: bool = False,
                       skip_collect: bool = False):
    """主页批量：采集全部作品链接入库 → 按顺序/随机逐个下载转写。
    collect_type 决定采集哪类视频：
      - post     发布（默认，fetch_user_post_videos）
      - like     点赞（fetch_user_like_videos，需登录 cookie）
      - favorite 收藏（fetch_user_collection_videos，只用 cookie 识别自己）
    resume=True 表示从暂停处恢复：跳过已下载/已完成的条目继续处理。
    skip_collect=True 表示 cookie 过期恢复：跳过重新采集，直接用已入库的剩余条目续跑。
    采集阶段若 cookie 过期/失效：任务自动置为「暂停等待新 cookie」（_waiting_cookie），
    用户就地更新 cookie 后恢复，无需重新从头采集。
    支持过滤：标题命中 skip_titles 的作品（如直播回顾/预告）不入库、不下载、不转写。
    skip_titles 可在设置页 douyin.skip_titles 配置，默认过滤直播回顾/预告/开始/回放。"""
    task = manager.get(task_id)
    if not task:
        return
    task["status"] = "running"
    task["paused"] = False
    try:
        skips = cfg.get_section("douyin", "skip_titles") or \
            ["直播回顾", "直播预告", "直播开始", "直播回放"]

        def _skip(m):
            t = (m.get("title") or "")
            return any(k and k in t for k in skips)

        lark = _feishu_client()
        if lark:
            try:
                lark.ensure_fields(FIELDS)
            except Exception as e:
                task["error"] = f"建字段警告(跳过): {e}"

        user_path = os.path.join(ROOT, task_id)
        os.makedirs(user_path, exist_ok=True)

        # 1. 采集全部作品（回调时跳过直播类，不入 task.videos）
        def _on_meta(m):
            if _skip(m):
                task["skip_count"] = task.get("skip_count", 0) + 1
                return
            _set_meta(task, m, download_status="pending",
                      transcript_status="pending", transcript="", local_path="")

        # cookie 过期恢复：跳过重新采集，直接用已入库的剩余条目续跑
        if skip_collect:
            metas = task.get("videos") or []
        else:
            try:
                if collect_type == "like":
                    metas = await f2_worker.collect_user_like_videos(sec_user_id, on_meta=_on_meta)
                elif collect_type == "favorite":
                    metas = await f2_worker.collect_user_collection_videos(on_meta=_on_meta)
                else:
                    metas = await f2_worker.collect_user_videos(sec_user_id, on_meta=_on_meta)
            except Exception as e:
                msg = str(e)
                # 识别 cookie 过期/失效 → 任务暂停等待新 cookie，就地更新后恢复
                if any(k in msg for k in ("cookie", "Cookie", "登录", "login", "Login",
                                          "expired", "失效", "会话", "验证", "captcha", "Captcha")):
                    task["status"] = "paused"
                    task["paused"] = True
                    task["_waiting_cookie"] = True
                    task["error"] = "采集时 cookie 已过期或失效，请在任务卡点『填 Cookie』就地更新后自动继续"
                    return
                task["status"] = "failed"
                task["error"] = f"采集失败：{msg}"
                return
        if not metas:
            task["status"] = "failed"
            task["error"] = "未采集到任何作品（请检查 cookie 是否有效，点赞/收藏需登录）"
            return
        task["nickname"] = nickname or metas[0].get("author_nickname", "") or (await _try_nickname(sec_user_id, collect_type))
        skipped = 0
        for m in metas:
            if _skip(m):
                skipped += 1
                continue
            db.upsert_video(_record_for(m, "batch"))
        if skipped:
            task["skip_count"] = skipped

        # 2. 逐条处理（顺序/随机）
        engine = cfg.get_section("asr", "engine") or "local"
        items = list(task["videos"])
        if order == "random" and not resume:
            random.shuffle(items)
        for v in items:
            # 暂停检查：用户点了暂停就停
            if task.get("paused") or task.get("cancelled"):
                if not task.get("cancelled"):
                    task["status"] = "paused"
                return
            # 恢复续跑：跳过已下载/已完成的，避免重复处理
            if resume and (v.get("transcript_status") in ("done", "empty")
                           or v.get("download_status") in ("downloaded", "failed")):
                continue
            await _process_one(v, task, user_path, lark, engine)
            # 单条处理完再检查一次暂停（处理耗时可能较长）
            if task.get("paused") or task.get("cancelled"):
                if not task.get("cancelled"):
                    task["status"] = "paused"
                return

        task["status"] = "done"
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)


async def process_single(task_id: str, target: str, platform: str = "douyin", engine: str = "f2"):
    """单链接下载：获取单条元信息 → 入库(single) → 下载 → 转写 → 写飞书 → 更新库。
    - engine=f2：target 为作品 ID（douyin/tiktok/weibo/twitter）
    - engine=ytdlp：target 为完整 URL（YouTube/B站等 1500+ 站点）"""
    task = manager.get(task_id)
    if not task:
        return
    task["_target"] = target
    task["_platform"] = platform
    task["_engine"] = engine
    task["status"] = "running"
    task["paused"] = False
    try:
        lark = _feishu_client()
        if lark:
            try:
                lark.ensure_fields(FIELDS)
            except Exception as e:
                task["error"] = f"建字段警告(跳过): {e}"

        if task.get("paused") or task.get("cancelled"):
            if not task.get("cancelled"):
                task["status"] = "paused"
            return
        if engine == "ytdlp":
            meta = await asyncio.to_thread(engine_ytdlp.fetch_meta, target)
        else:
            meta = await f2_worker.fetch_single_meta_platform(platform, target)
        if not meta or not meta.get("aweme_id"):
            task["status"] = "failed"
            task["error"] = f"无法获取作品信息（{platform}/{target[:40]}），请检查链接或 cookie"
            return
        task["nickname"] = meta.get("author_nickname", "")
        # 入库 source=single
        db.upsert_video(_record_for(meta, "single"))
        _set_meta(task, meta, download_status="pending", transcript_status="pending",
                  transcript="", local_path="")

        user_path = os.path.join(ROOT, task_id)
        os.makedirs(user_path, exist_ok=True)
        asr_engine = cfg.get_section("asr", "engine") or "local"
        await _process_one(meta, task, user_path, lark, asr_engine)
        if task.get("paused") or task.get("cancelled"):
            if not task.get("cancelled"):
                task["status"] = "paused"
            return
        task["status"] = "done"
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)


async def reprocess_single(aweme_id: str, mode: str = "transcribe"):
    """重试单条作品：
    - transcribe：用已下载的本地音频/视频只重新转写（不依赖 cookie）
    - download：重新获取元信息并下载+转写（需 cookie）
    复用内存任务，前端进度条可见。
    """
    tid = manager.new_task("", "", task_type="single")
    task = manager.get(tid)
    task["status"] = "running"
    try:
        row = db.get_by_aweme(aweme_id)
        if not row:
            task["status"] = "failed"
            task["error"] = "记录不存在"
            return
        task["nickname"] = row.get("author_nickname", "")
        engine = cfg.get_section("asr", "engine") or "local"
        lark = _feishu_client()

        meta = {
            "aweme_id": aweme_id,
            "title": row.get("title", ""),
            "video_url": row.get("douyin_url", ""),
            "author_nickname": row.get("author_nickname", ""),
            "local_path": row.get("local_path", ""),
            "audio_path": row.get("audio_path", ""),
            "transcript": row.get("transcript", ""),
            "publish_time": row.get("publish_time", 0),
            "duration": row.get("duration", 0),
            "desc": row.get("title", ""),
        }

        if mode == "transcribe":
            audio = row.get("audio_path")
            video = row.get("local_path")
            path = audio or video
            if not path or not os.path.exists(path):
                task["status"] = "failed"
                task["error"] = "本地无音频/视频文件，请改用「重新下载」"
                return
            _set_meta(task, meta, download_status="downloaded",
                      transcript_status="transcribing", local_path=video, audio_path=audio)
            text = ""
            try:
                text = await transcribe(path, engine=engine)
            except Exception as e:
                text = ""
                _set_meta(task, meta, transcript_status="failed", transcript=f"[转写失败]{e}")
                db.update_video(aweme_id, transcript_status="failed", transcript=f"[转写失败]{e}")
            ts = "done" if text else "empty"          # empty=有语音但未转出（如无配音视频）
            st = "转写完成" if text else "已下载"
            _set_meta(task, meta, transcript=text, transcript_status=ts, status=st)
            db.update_video(aweme_id, status=st, transcript_status=ts, transcript=text)
            if lark and text:
                try:
                    lark.upsert_by_aweme([(aweme_id, _row_for(_merged_meta(task, meta), st))])
                    _set_meta(task, meta, write_status="ok")
                except Exception as e:
                    _set_meta(task, meta, write_status="failed", write_error=str(e))
        else:  # download：重新下载 + 转写
            meta_f = await f2_worker.fetch_single_meta(aweme_id)
            if not meta_f or not meta_f.get("aweme_id"):
                task["status"] = "failed"
                task["error"] = "无法获取作品信息，请检查 cookie 是否有效"
                return
            task["nickname"] = meta_f.get("author_nickname", "")
            user_path = os.path.join(ROOT, tid)
            os.makedirs(user_path, exist_ok=True)
            _set_meta(task, meta_f, download_status="pending", transcript_status="pending",
                      transcript="", local_path="")
            await _process_one(meta_f, task, user_path, lark, engine)
        task["status"] = "done"
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)
