"""
f2 集成：采集博主全部作品元信息 + 逐条下载 mp4。

关键 API（来自 f2 源码 v0.0.1.8-pw3）：
  - DouyinHandler(kwargs).fetch_user_post_videos(sec_user_id, ...)  → 分页迭代作品
  - DouyinDownloader(kwargs).handler_download(kwargs, aweme_dict, user_path)  → 单条入队
  - DouyinDownloader(kwargs).execute_tasks()                        → 执行下载队列

策略：先采集全部元信息；再逐条"入队+执行"，以便 web 显示每条精准进度。
注意：字段名以 f2 实际返回为准，联调时校准；以下取值均做容错。
"""
import asyncio
import os
import re
from datetime import datetime

import config as cfg


def build_kwargs() -> dict:
    c = cfg.get_section("douyin")
    return {
        "headers": c.get("headers"),
        "proxies": c.get("proxies"),
        "timeout": c.get("timeout", 15),
        "cookie": c.get("cookie", ""),
        "mode": "post",
        "max_tasks": c.get("max_tasks", 5),
        "folderize": False,
    }


def resolve_sec_user_id(url_or_text: str):
    """从博主主页链接提取 sec_user_id：https://www.douyin.com/user/xxx"""
    m = re.search(r"/user/([A-Za-z0-9_-]+)", url_or_text or "")
    return m.group(1) if m else None


def resolve_aweme_id(url_or_text: str):
    """从单个视频链接/文本提取 aweme_id（纯数字长ID）。"""
    t = url_or_text or ""
    m = re.search(r"/video/(\d+)", t)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{15,19})(?:[?&#]|$)", t)
    if m:
        return m.group(1)
    m = re.search(r"(\d{15,19})", t)
    return m.group(1) if m else None


def _num(v, default=0):
    try:
        return int(v or default)
    except (TypeError, ValueError):
        return default


def _parse_create_time(v):
    """把 create_time 解析为毫秒时间戳。支持秒/毫秒数字、字符串 'YYYY-MM-DD HH-MM-SS'。"""
    if isinstance(v, (int, float)):
        return int(v) if v < 1e12 else int(v) // 1000
    if isinstance(v, str) and v.strip():
        from datetime import datetime
        for fmt in ("%Y-%m-%d %H-%M-%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(v.strip(), fmt).timestamp() * 1000)
            except ValueError:
                continue
    return 0


def extract_meta(a: dict) -> dict:
    """把 f2 的单条作品 dict 转成我们的元信息结构（兼容主页列表精简结构 + 单条详情结构）。

    - 主页列表：无 statistics，时长在顶层 video_duration，昵称 nickname_raw，标题 desc/caption
    - 单条详情：有 statistics、video（含 bit_rate[].play_addr）、author.nickname
    """
    a = a or {}
    stats = a.get("statistics") or {}
    author = a.get("author") or {}
    video = a.get("video") or {}
    # 播放地址：顶层 video_play_addr，其次 video 下 bit_rate 的 play_addr.url_list[0]
    play = a.get("video_play_addr")
    if not play and video:
        brs = video.get("bit_rate") or []
        if brs:
            pa = (brs[0] or {}).get("play_addr") or {}
            urls = pa.get("url_list") or []
            if urls:
                play = urls[0]
    aweme_id = a.get("aweme_id") or a.get("video_id") or ""
    # 时长：单条 video.duration 为秒；主页顶层 video_duration 为毫秒
    dur = _num(video.get("duration")) or (_num(a.get("video_duration")) // 1000)
    meta = {
        "aweme_id": str(aweme_id),
        "title": a.get("desc") or a.get("desc_raw") or a.get("caption") or "",
        "desc": a.get("desc") or a.get("desc_raw") or "",
        "video_play_addr": play or "",
        "publish_time": _parse_create_time(a.get("create_time")),
        "like_count": _num(stats.get("digg_count")),
        "comment_count": _num(stats.get("comment_count")),
        "share_count": _num(stats.get("share_count")),
        "collect_count": _num(stats.get("collect_count")),
        "duration": dur,
        "author_nickname": author.get("nickname") or a.get("nickname_raw") or a.get("nickname") or "",
        "sec_user_id": author.get("sec_user_id") or a.get("sec_user_id") or "",
        "video_url": f"https://www.douyin.com/video/{aweme_id}",
        "_raw": a,  # 保留原始 dict，供下载器使用
    }
    return meta


async def fetch_single_meta(aweme_id: str):
    """按 aweme_id 获取单条作品元信息（用于单链接下载）。"""
    from f2.apps.douyin.handler import DouyinHandler
    handler = DouyinHandler(build_kwargs())
    aweme = await handler.fetch_one_video(aweme_id)
    if aweme is None:
        return None
    # 兼容 Aweme 对象 / 列表包装
    if hasattr(aweme, "_to_dict"):
        aweme = aweme._to_dict()
    elif hasattr(aweme, "_to_list"):
        lst = aweme._to_list()
        aweme = lst[0] if lst else None
    if isinstance(aweme, dict):
        return extract_meta(aweme)
    return None


async def collect_user_videos(sec_user_id: str, on_meta=None) -> list:
    """采集博主全部作品元信息，分页遍历直到 has_more=0。
    on_meta 每次拿到一条即回调（用于进度）。
    注意：f2 签名 fetch_user_post_videos(sec_user_id, min_cursor, max_cursor, page_counts, max_counts)
    ——max_counts 传 None 表示不限数量，采集博主全部作品，而不是被截断。
    """
    from f2.apps.douyin.handler import DouyinHandler

    kwargs = build_kwargs()
    handler = DouyinHandler(kwargs)
    metas = []
    # page_counts=20（抖音单页最多约20条），max_counts=None 不限总数 → 翻页直到采完
    async for aweme_list in handler.fetch_user_post_videos(sec_user_id, 0, 0, 20, None):
        for a in aweme_list._to_list():
            meta = extract_meta(a)
            metas.append(meta)
            if on_meta:
                on_meta(meta)
    return metas


async def download_one(meta: dict, user_path: str) -> str:
    """下载单个作品 mp4，返回本地文件路径（找不到则返回空）。"""
    from f2.apps.douyin.dl import DouyinDownloader

    kwargs = build_kwargs()
    dl = DouyinDownloader(kwargs)
    before = _newest_mp4(user_path)
    await dl.handler_download(kwargs, meta["_raw"], user_path)
    await dl.execute_tasks()
    return _locate_new_mp4(user_path, before)


def _newest_mp4(folder: str):
    if not os.path.isdir(folder):
        return None
    files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".mp4")]
    return max(files, key=os.path.getmtime) if files else None


def _locate_new_mp4(folder: str, before):
    """返回比 before 更新的最新 mp4；before 为 None 时返回最新的 mp4。"""
    if not os.path.isdir(folder):
        return ""
    files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".mp4")]
    if not files:
        return ""
    if before is None:
        return max(files, key=os.path.getmtime)
    new = [f for f in files if os.path.getmtime(f) > (os.path.getmtime(before) if before else 0)]
    if not new:
        # 可能刚好同名覆盖，返回最新的
        return max(files, key=os.path.getmtime)
    return max(new, key=os.path.getmtime)
