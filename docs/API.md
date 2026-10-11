# API 文档 API Reference

Base URL：`http://<host>:9009`。均为 JSON（导出除外）。

## 任务

### POST `/api/task` 主页批量
```json
{ "url": "https://www.douyin.com/user/xxxxx", "order": "order" }
```
- `order`: `order`（新→旧） | `random`（随机）
- 返回：`{"ok":true,"task_id":"..."}`

### POST `/api/download_single` 单链接
```json
{ "url": "https://www.douyin.com/video/7690498107435519266" }
```

### GET `/api/tasks` 实时进度
返回全部任务（含 videos 数组，每条有 download_status/transcript_status/transcript/local_url/audio_url 等）。

## 查询与导出

### GET `/api/videos`
参数（全部可选）：`status`、`source`(single|batch)、`author`、`q`(标题/逐字稿)、`date_from`(ms)、`date_to`(ms)、`min_duration`、`max_duration`(秒)、`limit`、`offset`。
返回：`{"ok":true,"items":[...],"total":命中数,"stats":{total,done,failed}}`

### GET `/api/export`
参数与 `/api/videos` 相同，**只导出当前筛选结果**。返回 CSV（UTF-8 BOM，Excel 直开）。
字段：aweme_id,title,author_nickname,source,status,publish_time,duration,transcript,local_url,audio_url,douyin_url,created_at

## 设置

### GET `/api/settings` 读全部配置
### POST `/api/settings` 保存（部分更新）
```json
{ "values": { "douyin.cookie": "...", "llm.model": "qwen-max" } }
```

## 其它

### GET `/api/health`
`{"ok":true,"service":"douyin-distiller-web"}`
