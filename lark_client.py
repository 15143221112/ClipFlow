"""
飞书多维表读写客户端（REST API + httpx，避免 SDK 版本差异）。

字段类型映射（飞书 bitable 字段 type 数字）：
  text=1   number=2   select=3(单选)   datetime=5(日期,毫秒时间戳)
  url=14(超链接, 值 {"text":"..","link":".."})
"""
import httpx

_BASE = "https://open.feishu.cn/open-apis"


class LarkTableClient:
    def __init__(self, app_id: str, app_secret: str, base_token: str, table_id: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_token = base_token
        self.table_id = table_id
        self._token = None

    # ---------------- 鉴权
    def _ensure_token(self):
        if self._token:
            return self._token
        resp = httpx.post(
            f"{_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书鉴权失败: {data}")
        self._token = data["tenant_access_token"]
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    # ---------------- 字段
    TYPE_MAP = {"text": 1, "number": 2, "select": 3, "datetime": 5, "url": 15}
    # 飞书新接口 ui_type 用字符串枚举（不是数字）
    UI_TYPE = {"text": "Text", "number": "Number", "select": "SingleSelect",
               "datetime": "DateTime", "url": "Url"}

    def list_fields(self) -> dict:
        """返回 {字段名: ui_type字符串}"""
        url = f"{_BASE}/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/fields"
        fields = {}
        page_token = None
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            resp = httpx.get(url, headers=self._headers(), params=params, timeout=20)
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"列字段失败: {data}")
            for it in data.get("data", {}).get("items", []):
                fields[it["field_name"]] = str(it.get("type", ""))
            page_token = data.get("data", {}).get("page_token")
            if not data.get("data", {}).get("has_more"):
                break
        return fields

    def create_field(self, name: str, ui_type: str, property_body: dict = None):
        type_num = self.TYPE_MAP[ui_type]
        body = {"field_name": name, "type": type_num, "ui_type": self.UI_TYPE[ui_type]}
        if property_body:
            body["property"] = property_body
        url = f"{_BASE}/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/fields"
        resp = httpx.post(url, headers=self._headers(), json=body, timeout=20)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"建字段失败[{name}]: {data}")

    def ensure_fields(self, wanted: dict, select_options: dict = None):
        """wanted: {字段名: ui_type字符串}；select_options: {字段名: [选项...]} 供单选字段"""
        existing = self.list_fields()
        for name, ui_type in wanted.items():
            if name in existing:
                continue
            prop = None
            if ui_type == "select":
                opts = (select_options or {}).get(name) or ["未下载", "已下载", "转写完成", "失败"]
                prop = {"options": [{"name": o} for o in opts]}
            self.create_field(name, ui_type, prop)

    # ---------------- 记录
    def list_records(self, page_size: int = 500) -> list:
        url = f"{_BASE}/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/records"
        rows, page_token = [], None
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            resp = httpx.get(url, headers=self._headers(), params=params, timeout=20)
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"查记录失败: {data}")
            rows.extend(data.get("data", {}).get("items", []))
            page_token = data.get("data", {}).get("page_token")
            if not data.get("data", {}).get("has_more"):
                break
        return rows

    def existing_urls(self) -> set:
        """去重：返回表格中已存在的 video_url（超链接 .link 或纯文本）集合"""
        urls = set()
        for rec in self.list_records():
            fields = rec.get("fields", {})
            v = fields.get("video_url")
            if isinstance(v, dict):
                urls.add(v.get("link", ""))
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                urls.add(v[0].get("link", ""))
            elif isinstance(v, str):
                urls.add(v)
        return urls

    def existing_aweme_ids(self) -> set:
        """返回表格中已存在的 aweme_id 集合（用于写入去重）。"""
        ids = set()
        for rec in self.list_records():
            a = (rec.get("fields") or {}).get("aweme_id")
            if a is not None:
                ids.add(str(a))
        return ids

    def record_map_by_aweme(self) -> dict:
        """返回 {aweme_id: record_id}，供 upsert 更新定位。"""
        m = {}
        for rec in self.list_records():
            a = (rec.get("fields") or {}).get("aweme_id")
            if a is not None:
                m[str(a)] = rec.get("record_id")
        return m

    def batch_create(self, fields_list: list):
        """fields_list: [ {字段名: 值}, ... ]，一次最多 500 条（分批）。"""
        url = f"{_BASE}/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/records/batch_create"
        total = 0
        for i in range(0, len(fields_list), 500):
            batch = fields_list[i : i + 500]
            body = {"records": [{"fields": f} for f in batch]}
            resp = httpx.post(url, headers=self._headers(), json=body, timeout=60)
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"批量写入失败: {data}")
            total += len(data.get("data", {}).get("records", []))
        return total

    def batch_update(self, record_fields_list: list):
        """record_fields_list: [ (record_id, {字段名: 值}), ... ]，一次最多 500 条（分批）。"""
        url = f"{_BASE}/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/records/batch_update"
        total = 0
        for i in range(0, len(record_fields_list), 500):
            batch = record_fields_list[i : i + 500]
            body = {"records": [{"record_id": rid, "fields": f} for rid, f in batch]}
            resp = httpx.post(url, headers=self._headers(), json=body, timeout=60)
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"批量更新失败: {data}")
            total += len(data.get("data", {}).get("records", []))
        return total

    def upsert_by_aweme(self, rows: list):
        """rows: [ (aweme_id, {字段名: 值}), ... ]
        已存在（按 aweme_id 匹配）→ 更新该记录；不存在 → 新建。返回 (created, updated)。"""
        if not rows:
            return 0, 0
        m = self.record_map_by_aweme()
        to_create, to_update = [], []
        for aweme, fields in rows:
            rid = m.get(str(aweme))
            if rid:
                to_update.append((rid, fields))
            else:
                to_create.append(fields)
        created = self.batch_create(to_create) if to_create else 0
        updated = self.batch_update(to_update) if to_update else 0
        return created, updated


def value_for(field_ui_type: str, val):
    """把 python 值转成飞书字段值格式。"""
    if field_ui_type == "url":
        return {"text": val.get("text", ""), "link": val["link"]}
    if field_ui_type == "select":
        return [val] if not isinstance(val, list) else val
    if field_ui_type == "datetime":
        return int(val)
    return val
