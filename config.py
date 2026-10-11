"""
配置管理：读写 SQLite settings 表，线程安全，页面可改后即时生效。

所有敏感项（cookie / 飞书 app_secret / 讯飞 secret）都存数据库 settings 表；
首次启动自动把 settings.json 迁移进库。settings.json 仅作为初始种子/备份。
"""
import os
import threading

import db

_BASE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(_BASE, "settings.json")

_lock = threading.Lock()
_cache = None


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            db.migrate_from_file(SETTINGS_PATH)  # 首次从文件灌库
            _cache = db.get_all_settings()
        return _cache


def get() -> dict:
    return _load()


def get_section(*keys):
    node = _load()
    for k in keys:
        node = node.get(k) if isinstance(node, dict) else None
        if node is None:
            return None
    return node


def set_value(dotted_key: str, value):
    """按 a.b.c 路径写配置到数据库，并同步内存缓存。返回 True。"""
    _load()
    db.set_setting(dotted_key, value)
    node = _cache
    parts = dotted_key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    return True


def set_many(updates: dict):
    """批量更新数据库设置，updates 形如 {"a.b": value, ...}"""
    _load()
    for key, value in updates.items():
        db.set_setting(key, value)
        node = _cache
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return True
