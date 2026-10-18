# 📥 ClipFlow

> Douyin creator homepage → bulk collection → download MP4 → Speech-to-Text (ASR) → text correction → write to Feishu Bitable.
> A web-driven automated pipeline turning Douyin videos into structured text.

[English](README.en.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

---

## 🖼 UI Preview

![ClipFlow console](docs/assets/screenshot.png)

---

## ✨ Highlights

- **End-to-end automation**: Paste a creator homepage or a single video link — it auto-runs **collect → download → extract audio → transcribe → correct terms → write to Feishu Bitable**, all visible in the web UI.
- **Local-first & offline-friendly**: Videos, audio and transcripts are all persisted locally (SQLite + files) with history query and re-run; an expired cookie never blocks re-processing of already-stored works.
- **Three ASR channels**: Local Whisper (faster-whisper) / iFlytek / Feishu, switchable in the UI. Local by default — no sensitive audio leaves your machine.
- **LLM smart correction**: Post-transcription text is fed to an LLM (Aliyun qwen-max or local vLLM) to add punctuation and fix domain homophone errors, yielding deliverable copy.
- **Single-link & homepage bulk**: Bulk mode supports sequential / random order; it first collects all work links into the DB, then processes them one by one reliably.
- **Database persistence**: Works and settings are stored in SQLite (`videos` + `settings` tables); page edits take effect immediately and survive restarts.
- **Conditional query + on-demand export**: History page filters by status / source / author / publish date range / duration / keyword; **export only the currently filtered results** (CSV with UTF-8 BOM, opens clean in Excel).
- **Local media accessible**: `/data` static mount; one-click view, play and download from the page.
- **Deep Feishu Bitable integration**: Fields auto-created; writes are **upsert by aweme_id** (update if exists, create if not) — existing records get overwritten with the latest text, no duplicates.
- **Easy deploy**: Native `venv + systemd` or one-shot `Docker / docker-compose`; all config lives in a single `settings.json`.

---

## 📦 Features

| Capability | Description |
|---|---|
| Single-link download | Paste one video link → download → transcribe → store → write to Feishu |
| Homepage bulk | Input creator homepage → collect all work links → sequential/random processing |
| History query | Multi-condition filter: status / source / author / date / duration / keyword |
| Conditional export | Export the current filtered result as CSV (UTF-8 BOM) |
| ASR | Local Whisper / iFlytek / Feishu, switchable |
| Term correction | LLM punctuation + domain homophone fix (built-in rice/agriculture lexicon) |
| Feishu write | Auto-create fields, dedupe + upsert by aweme_id |
| Settings management | All config in DB `settings` table, page saves take effect instantly |
| Local storage | mp4 / mp3 / transcript all persisted, accessible & downloadable |

---

## 🏗 Architecture

```
                        ┌──────────────────────────────────────┐
   Homepage/Single link  │         ClipFlow (Web)        │
  ────────────────────▶  │                                        │
                        │  collect(f2) → download(mp4) → ffmpeg   │
                        │   → extract audio(mp3)                  │
                        │   → ASR transcribe → LLM correct        │
                        │   → SQLite store → write to Feishu      │
                        └─────────────┬──────────────┬────────────┘
                                      │              │
                             ┌────────▼────┐  ┌──────▼────────┐
                             │ ASR service │  │ LLM service    │
                             │ whisper 8003 │  │ qwen-max/local │
                             └─────────────┘  └───────────────┘
```

**Modules**
- `app.py` — FastAPI entry: web page + REST API
- `worker.py` — pipeline orchestration (single / bulk / progress)
- `f2_worker.py` — F2 wrapper: homepage collect, single detail, download
- `asr_client.py` — three-channel ASR + LLM punctuation/term correction
- `lark_client.py` — Feishu Bitable read/write (auth, fields, upsert)
- `db.py` — SQLite: `videos` (records) + `settings` (config)
- `config.py` — config read/write (DB-first, settings.json as seed)

---

## 🚀 Quick Start

### Option 1: Docker (recommended)

```bash
git clone https://github.com/<you>/ClipFlow.git
cd ClipFlow
cp settings.example.json settings.json   # edit: cookie / Feishu / LLM
cp .env.example .env

docker compose up -d --build

# Open http://<host>:9009
```

### Option 2: Native venv + systemd

```bash
git clone https://github.com/<you>/ClipFlow.git
cd ClipFlow
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# ffmpeg required (Ubuntu: sudo apt install ffmpeg)
cp settings.example.json settings.json

uvicorn app:app --host 0.0.0.0 --port 9009
```

systemd example (`/etc/systemd/system/ClipFlow.service`):

```ini
[Unit]
Description=ClipFlow
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/douyin-web
ExecStart=/opt/douyin-web/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 9009
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ClipFlow
```

---

## ⚙️ Configuration (`settings.json` / the page's "Settings" tab)

> All config is also stored in the DB `settings` table; page saves apply instantly. Keep secrets out of git.

| Section | Key fields | Description |
|---|---|---|
| `douyin.cookie` | login cookie | required for collect/download; grab from douyin.com browser session |
| `asr.engine` | local / xfyun / feishu | ASR channel |
| `asr.local.url` | whisper endpoint | e.g. `http://<host>:8003/v1/audio/transcriptions` |
| `asr.xfyun.*` | app_id/api_key/api_secret | iFlytek voice dictation |
| `asr.feishu.*` | app_id/app_secret/api_endpoint | Feishu speech recognition |
| `feishu_table.*` | app_id/app_secret/base_token/table_id | Feishu Bitable target |
| `llm.*` | base_url/model/api_key | correction model (Aliyun qwen-max or local vLLM) |
| `server.base_url` | public-accessible URL | used to build local video/audio download links |

---

## 🧩 Usage Flow

1. **Direct download**: paste a single video link → download & transcribe → auto-store + write to Feishu.
2. **Homepage bulk**: paste a creator homepage → choose sequential / random → collect all work links → process one by one.
3. **History query**: filter by multiple conditions, view video/audio/transcript, click "Export current results" for CSV.
4. **Settings**: enter cookie, ASR channel, Feishu, LLM.

> **Feishu Bitable permission trio (official requirement)**: ① enable `bitable:app` application permission → ② publish an app version → ③ add the app as a collaborator with "Manage" access on the Bitable. Otherwise writes fail with 91403 Forbidden. The `wiki_token` from a wiki link can be used directly as `base_token`.

---

## 🔌 API Overview

| Method | Path | Description |
|---|---|---|
| POST | `/api/task` | Homepage bulk (`{"url","order":"order|random"}`) |
| POST | `/api/download_single` | Single-link download (`{"url"}`) |
| GET | `/api/tasks` | Real-time progress of all tasks |
| GET | `/api/videos` | History query (status/source/author/q/date_from/date_to/min_duration/max_duration/limit/offset) |
| GET | `/api/export` | Export current filtered results as CSV |
| GET | `/api/settings` | Read all config |
| POST | `/api/settings` | Save config (`{"values":{"douyin.cookie":"...",...}}`) |
| GET | `/api/health` | Health check |

---

## ❓ FAQ

**Q: Can I still work after the cookie expires?**
Yes. Stored works, local videos, audio and transcripts live in the DB and disk — query, export and playback need no cookie.

**Q: Transcripts have homophone errors?**
Built-in LLM correction (Aliyun qwen-max by default, or local vLLM) adds punctuation and fixes domain terms; on failure the original text is kept (non-blocking).

**Q: Can text/multimodal LLMs be shared?**
Correction uses an OpenAI-compatible endpoint — local vLLM or cloud models both work; just set base_url/model/api_key.

**Q: Where are videos/audio stored?**
Under `download.root` (default `/opt/douyin-web/data`) with a per-task subfolder; served at `/data/...`.

---

## 🛣 Roadmap

- [x] Single-link / homepage bulk collect & download
- [x] Three-channel ASR + LLM term correction
- [x] SQLite persistence + settings in DB
- [x] Conditional query + on-demand CSV export
- [x] Feishu Bitable upsert
- [x] Docker / systemd deploy
- [ ] Interaction stats (likes/comments) completion
- [ ] Scheduled / incremental re-transcribe
- [ ] Webhook / authenticated external API
- [ ] More README languages (PRs welcome)

---

## 🤝 Contributing & License

Issues and PRs welcome. Please: avoid destructive changes to the DB schema; never commit secrets; attach tests for new features.
Released under the [MIT](LICENSE) license, compatible with the open-source ecosystem.

**Disclaimer**: use only in compliance with Douyin platform rules and local laws, for legitimate content processing.
