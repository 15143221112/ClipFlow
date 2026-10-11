"""
SQLite 持久化：作品记录（下载历史）落库，cookie 过期后仍可基于已存链接重新下载/转写。
表 videos：每条作品一行，含抖音链接、本地视频/音频路径、逐字稿、状态、来源。
"""
import os
import json
import sqlite3
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(ROOT, exist_ok=True)
DB_PATH = os.path.join(ROOT, "douyin.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  aweme_id      TEXT UNIQUE,
  title         TEXT,
  douyin_url    TEXT,
  author_nickname TEXT,
  source        TEXT,               -- single=单链接 / batch=主页批量
  status        TEXT,               -- 待处理/下载中/已下载/转写中/转写完成/失败
  download_status TEXT,
  transcript_status TEXT,
  local_path    TEXT,
  local_url     TEXT,
  audio_path    TEXT,
  audio_url     TEXT,
  transcript    TEXT,
  duration      REAL,
  publish_time  INTEGER,
  created_at    TEXT,
  updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_source ON videos(source);
CREATE TABLE IF NOT EXISTS settings (
  key         TEXT PRIMARY KEY,     -- 点分路径，如 douyin.cookie
  value       TEXT,                 -- JSON 序列化
  updated_at  TEXT
);
"""


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    c = _conn()
    c.executescript(_SCHEMA)
    c.commit()
    c.close()


def upsert_video(rec: dict):
    """按 aweme_id 插入或更新。rec 至少含 aweme_id。"""
    init()
    c = _conn()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    keys = ["aweme_id", "title", "douyin_url", "author_nickname", "source", "status",
            "download_status", "transcript_status", "local_path", "local_url",
            "audio_path", "audio_url", "transcript", "duration", "publish_time"]
    rec = {k: rec.get(k) for k in keys}
    rec.setdefault("aweme_id", "")
    rec.setdefault("source", "single")
    rec.setdefault("status", "待处理")
    rec.setdefault("download_status", "pending")
    rec.setdefault("transcript_status", "pending")
    rec.setdefault("created_at", now)
    rec.setdefault("updated_at", now)
    cols = ", ".join(keys + ["created_at", "updated_at"])
    ph = ", ".join(["?"] * (len(keys) + 2))
    upd = ", ".join([f"{k}=excluded.{k}" for k in keys + ["updated_at"]])
    c.execute(
        f"INSERT INTO videos ({cols}) VALUES ({ph}) ON CONFLICT(aweme_id) DO UPDATE SET {upd}",
        [rec[k] for k in keys] + [rec["created_at"], rec["updated_at"]],
    )
    c.commit()
    c.close()


def update_video(aweme_id: str, **fields):
    """按 aweme_id 更新字段（如 status/local_path/transcript）。"""
    if not aweme_id:
        return
    init()
    c = _conn()
    fields["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [aweme_id]
    c.execute(f"UPDATE videos SET {cols} WHERE aweme_id=?", vals)
    c.commit()
    c.close()


def get_by_aweme(aweme_id: str):
    init()
    c = _conn()
    row = c.execute("SELECT * FROM videos WHERE aweme_id=?", (aweme_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def list_videos(limit=200, offset=0, status=None, source=None, q=None, author=None,
                date_from=None, date_to=None, min_duration=None, max_duration=None):
    init()
    c = _conn()
    sql = "SELECT * FROM videos WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"; args.append(status)
    if source:
        sql += " AND source=?"; args.append(source)
    if author:
        sql += " AND author_nickname LIKE ?"; args.append(f"%{author}%")
    if q:
        sql += " AND (title LIKE ? OR transcript LIKE ?)"; args.append(f"%{q}%"); args.append(f"%{q}%")
    if date_from:
        sql += " AND publish_time>=?"; args.append(int(date_from))
    if date_to:
        sql += " AND publish_time<=?"; args.append(int(date_to))
    if min_duration is not None and min_duration != "":
        sql += " AND duration>=?"; args.append(float(min_duration))
    if max_duration is not None and max_duration != "":
        sql += " AND duration<=?"; args.append(float(max_duration))
    total = c.execute(sql, args).fetchone()[0]
    rows = c.execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    c.close()
    return [dict(r) for r in rows], total


def stats():
    init()
    c = _conn()
    total = c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    done = c.execute("SELECT COUNT(*) FROM videos WHERE status='转写完成'").fetchone()[0]
    failed = c.execute("SELECT COUNT(*) FROM videos WHERE status='失败'").fetchone()[0]
    c.close()
    return {"total": total, "done": done, "failed": failed}


# ---------------------------------------------------------------- settings 表
def _dotted_set(node: dict, dotted: str, value):
    """把 'douyin.cookie' 写进嵌套 dict node。"""
    parts = dotted.split(".")
    cur = node
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def get_all_settings() -> dict:
    """返回全部设置的嵌套 dict（settings 表，点分 key 展开）。"""
    init()
    c = _conn()
    rows = c.execute("SELECT key,value FROM settings").fetchall()
    c.close()
    out = {}
    for r in rows:
        try:
            v = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            v = r["value"]
        _dotted_set(out, r["key"], v)
    return out


def set_setting(dotted: str, value):
    """写入/更新一条设置（JSON 序列化存 value）。"""
    init()
    c = _conn()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (dotted, json.dumps(value, ensure_ascii=False), now),
    )
    c.commit()
    c.close()


def set_many_settings(updates: dict):
    """批量写入设置：{"douyin.cookie": "...", "llm.model": "qwen-max", ...}"""
    for k, v in (updates or {}).items():
        set_setting(k, v)


def migrate_from_file(json_path: str):
    """首次启动：若 settings 表为空，把 settings.json 的内容灌入数据库。"""
    init()
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    if n > 0:
        c.close()
        return
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        c.close()
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    def flat(node, prefix=""):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                yield from flat(v, key)
            else:
                yield key, v

    rows = [(k, json.dumps(v, ensure_ascii=False), now) for k, v in flat(data)]
    if rows:
        c.executemany(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)", rows
        )
        c.commit()
    c.close()
