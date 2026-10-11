# -*- coding: utf-8 -*-
"""db 层最小单元测试（独立临时库，不污染真实数据）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

_tmp = tempfile.mkdtemp()
db.ROOT = _tmp
db.DB_PATH = os.path.join(_tmp, "test.db")
db.init()


def setup_function():
    # 每个用例删除旧库重建，保证隔离（WAL 模式下 DELETE 不保险）
    for f in (db.DB_PATH, db.DB_PATH + "-wal", db.DB_PATH + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    db.init()


def _seed():
    db.upsert_video({"aweme_id": "1", "title": "水稻穗肥施用", "author_nickname": "严博士",
                     "source": "single", "duration": 30, "publish_time": 1791540000000})
    db.upsert_video({"aweme_id": "2", "title": "其他内容", "author_nickname": "李老师",
                     "source": "batch", "duration": 300, "publish_time": 1791540000000,
                     "status": "转写完成"})


def test_total_and_seed():
    _seed()
    rows, total = db.list_videos()
    assert total == 2


def test_filter_by_status():
    _seed()
    rows, total = db.list_videos(status="转写完成")
    assert total == 1 and rows[0]["aweme_id"] == "2"


def test_filter_by_author():
    _seed()
    rows, total = db.list_videos(author="严博士")
    assert total == 1 and rows[0]["aweme_id"] == "1"


def test_filter_by_source():
    _seed()
    rows, total = db.list_videos(source="batch")
    assert total == 1 and rows[0]["aweme_id"] == "2"


def test_filter_by_keyword():
    _seed()
    rows, total = db.list_videos(q="穗肥")
    assert total == 1


def test_filter_by_duration():
    _seed()
    rows, total = db.list_videos(min_duration=60)
    assert total == 1 and rows[0]["aweme_id"] == "2"
    rows, total = db.list_videos(max_duration=60)
    assert total == 1 and rows[0]["aweme_id"] == "1"


def test_filter_by_date():
    _seed()
    rows, total = db.list_videos(date_from=1791540000000, date_to=1791540000000)
    assert total == 2


def test_update_video():
    _seed()
    db.update_video("1", status="转写完成", transcript="测试文本")
    r = db.get_by_aweme("1")
    assert r["status"] == "转写完成" and r["transcript"] == "测试文本"


def test_stats():
    _seed()
    s = db.stats()
    assert s["total"] == 2 and s["done"] == 1
