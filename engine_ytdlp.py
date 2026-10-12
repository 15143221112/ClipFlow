"""
yt-dlp 下载引擎：支持 YouTube / B站 / TikTok / 微博 等 1500+ 网站的单条下载。

与 F2 引擎区别：
  - F2 引擎：深度结构化采集（主页批量列作品、点赞/评论等），支持抖音/TikTok/微博/Twitter
  - yt-dlp 引擎：站点覆盖面极广，但以"按 URL 下载视频"为主，不提供主页列表采集

职责：按 URL 获取元信息（fetch_meta）+ 下载到本地目录（download）。
安全：仅放行 http/https、拒绝内网/IP（防 SSRF）。
"""
import os
import re
import socket
import time
import urllib.parse
import ipaddress

import yt_dlp

SAFE_HOST_FRAGMENTS = ("localhost", ".local", ".internal", "127.0.0.1")


def _safe_url(url: str) -> bool:
    """校验 URL：仅 http/https，拒绝内网/IP（防 SSRF）。"""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return False
    # 显式 IP（含内网段）
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
        return True
    except ValueError:
        pass
    # 本地主机名
    if host == "localhost" or host.endswith((".local", ".internal")):
        return False
    return True


def _public_host(host: str) -> bool:
    """解析域名，若解析到内网 IP 则拒绝（防 DNS rebinding）。"""
    try:
        for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
            ip = info[4][0]
            o = ipaddress.ip_address(ip)
            if o.is_private or o.is_loopback or o.is_link_local or o.is_reserved:
                return False
    except Exception:
        pass  # 解析失败放行，由上层报错
    return True


def _num(v, default=0):
    try:
        return int(v or default)
    except (TypeError, ValueError):
        return default


def _parse_ts(v):
    """时间戳 → 毫秒。"""
    if isinstance(v, (int, float)) and v:
        return int(v * 1000) if v < 1e12 else int(v)
    return 0


def _base_opts(**extra):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 3,
        "nocheckcertificate": False,
    }
    opts.update(extra)
    return opts


def fetch_meta(url: str):
    """用 yt-dlp 提取元信息（不下载）。返回统一 meta 结构。"""
    if not _safe_url(url):
        raise ValueError("链接已被拦截：仅支持公开 http/https，拒绝内网/IP")
    if not _public_host(urllib.parse.urlparse(url).hostname or ""):
        raise ValueError("目标解析到内网地址，已拦截（防 SSRF）")
    with yt_dlp.YoutubeDL(_base_opts(skip_download=True)) as ydl:
        info = ydl.extract_info(url, download=False)
    title = info.get("title") or info.get("fulltitle") or info.get("id") or url
    meta = {
        "aweme_id": str(info.get("id") or ""),
        "title": str(title),
        "desc": str(info.get("description") or info.get("title") or ""),
        "publish_time": _parse_ts(info.get("timestamp")),
        "duration": _num(info.get("duration")),
        "author_nickname": str(info.get("uploader") or info.get("channel") or
                              info.get("uploader_id") or info.get("channel_id") or ""),
        "like_count": _num(info.get("like_count")),
        "comment_count": _num(info.get("comment_count")),
        "share_count": 0,
        "collect_count": _num(info.get("repost_count")),
        "video_url": str(info.get("webpage_url") or url),
        "extractor": str(info.get("extractor") or ""),
        "_source": "ytdlp",
    }
    return meta


def download(url: str, user_path: str, progress=None):
    """下载视频到 user_path，返回本地文件路径（找不到则空）。"""
    if not _safe_url(url):
        raise ValueError("链接已被拦截：仅支持公开 http/https，拒绝内网/IP")
    if not _public_host(urllib.parse.urlparse(url).hostname or ""):
        raise ValueError("目标解析到内网地址，已拦截（防 SSRF）")
    os.makedirs(user_path, exist_ok=True)
    before = set(_files(user_path))
    outtmpl = os.path.join(user_path, "%(id)s_%(title).80B.%(ext)s")
    opts = _base_opts(
        outtmpl=outtmpl,
        # 优先 mp4；无则退化为可用格式，由 ffmpeg 合并
        format="bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        merge_output_format="mp4",
        postprocessor_args=[],
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    after = set(_files(user_path))
    new = after - before
    candidates = [f for f in new if f.lower().endswith((".mp4", ".mkv", ".webm", ".flv"))]
    if not candidates:
        candidates = [f for f in new if not f.lower().endswith((".part", ".ytdl", ".tmp", ".jpg", ".png", ".webp", ".info.json"))]
    if not candidates:
        return ""
    # 优先 mp4
    mp4s = [f for f in candidates if f.lower().endswith(".mp4")]
    if mp4s:
        return max(mp4s, key=os.path.getmtime)
    return max(candidates, key=os.path.getmtime)


def _files(folder: str):
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, f) for f in os.listdir(folder)]
