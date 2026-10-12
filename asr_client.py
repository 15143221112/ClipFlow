"""
三通道语音转写客户端：
  - local : 本地 whisper 服务（OpenAI 兼容 /v1/audio/transcriptions）
  - xfyun : 讯飞语音听写 WebSocket API（v2/iat）
  - feishu: 飞书开放平台语音识别（lark-oapi，端点可配置）

统一入口：transcribe(audio_path, engine=None, config=None) -> str
"""
import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import subprocess
from urllib.parse import quote

import httpx
import websockets

import config as cfg


# ---------------------------------------------------------------- 通用工具
def _ffmpeg_to_16k_wav(src: str, dst: str):
    """用系统 ffmpeg 把任意音频转 16k 单声道 PCM wav（讯飞要求）。"""
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", dst],
        capture_output=True,
        check=True,
    )


# ---------------------------------------------------------------- 本地 whisper
async def _local_transcribe(audio_path: str, conf: dict) -> str:
    url = conf.get("url", "http://192.168.1.5:8003/v1/audio/transcriptions")
    language = conf.get("language", "zh")
    fmt = conf.get("response_format", "text")
    async with httpx.AsyncClient(timeout=600) as client:
        with open(audio_path, "rb") as f:
            files = {"file": (audio_path, f, "application/octet-stream")}
            data = {"language": language, "response_format": fmt}
            resp = await client.post(url, files=files, data=data)
        resp.raise_for_status()
        try:
            j = resp.json()
            if isinstance(j, dict) and "text" in j:
                return j["text"]
        except Exception:
            pass
        return resp.text


# ---------------------------------------------------------------- 讯飞语音听写
def _xfyun_url(conf: dict) -> str:
    host = "iat-api.xfyun.cn"
    path = "/v2/iat"
    app_id = conf["app_id"]
    api_key = conf["api_key"]
    api_secret = conf["api_secret"]
    now = datetime.datetime.utcnow()
    date = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    signature = base64.b64encode(
        hmac.new(api_secret.encode(), signature_origin.encode(), hashlib.sha256).digest()
    ).decode()
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode()).decode()
    return (
        f"wss://{host}{path}?authorization={quote(authorization)}"
        f"&date={quote(date)}&host={host}"
    )


async def _xfyun_transcribe(audio_path: str, conf: dict) -> str:
    tmp = audio_path + ".16k.wav"
    _ffmpeg_to_16k_wav(audio_path, tmp)
    try:
        language = conf.get("language", "zh_cn")
        app_id = conf["app_id"]
        uri = _xfyun_url(conf)
        result = []
        async with websockets.connect(uri, ping_interval=None) as ws:
            # 首帧
            first = {
                "common": {"app_id": app_id},
                "business": {
                    "language": language,
                    "domain": "iat",
                    "accent": "mandarin",
                    "ptt": 0,
                    "dwa": 0,
                },
                "data": {"status": 0, "format": "audio/L16;rate=16000", "encoding": "raw", "audio": ""},
            }
            await ws.send(json.dumps(first))
            with open(tmp, "rb") as f:
                while True:
                    chunk = f.read(12800)
                    if not chunk:
                        break
                    data = {
                        "data": {
                            "status": 2 if False else 1,
                            "format": "audio/L16;rate=16000",
                            "encoding": "raw",
                            "audio": base64.b64encode(chunk).decode(),
                        }
                    }
                    await ws.send(json.dumps(data))
            await ws.send(json.dumps({"data": {"status": 2, "format": "audio/L16;rate=16000", "encoding": "raw", "audio": ""}}))
            # 收结果
            while True:
                msg = json.loads(await ws.recv())
                code = msg.get("code", 0)
                if code != 0:
                    raise RuntimeError(f"讯飞错误: {msg.get('message')}")
                if msg.get("data"):
                    for item in msg["data"].get("result", {}).get("ws", []):
                        for w in item.get("cw", []):
                            result.append(w.get("w", ""))
                if msg.get("data", {}).get("status", 2) == 2 and not msg.get("data", {}).get("result"):
                    pass
                # 结束判定：data.status==2
                if msg.get("data", {}).get("status", 2) == 2 and msg.get("data", {}).get("result"):
                    break
        return "".join(result)
    finally:
        # 清理临时 16k 文件
        try:
            import os
            os.remove(tmp)
        except Exception:
            pass


# ---------------------------------------------------------------- 飞书语音识别
async def _feishu_transcribe(audio_path: str, conf: dict) -> str:
    # 飞书语音识别走 REST API：tenant_access_token + 上传音频 → 返回文本。
    # 端点/请求体以飞书开放平台官方为准，凭据申请后联调确认。
    # 说明：lark_client.py 的写表同样走 REST，不依赖 lark-oapi SDK。
    raise NotImplementedError(
        "飞书语音识别端点需联调确认（请核对飞书开放平台语音识别 API 与请求体）。"
    )


# ---------------------------------------------------------------- 标点恢复 + 领域纠错
_PUNCT_PROMPT = (
    "你是水稻农业领域的文字校对专家。下面这段文本是语音转写的草稿，存在大量同音字/错别字"
    "（尤其农业术语被转错），请改正为正确的水稻种植专业术语，并补充正确的标点符号和断句"
    "（每个完整句子单独成行）。\n"
    "常见正确术语参考（括号内为常见错误写法，请更正）：\n"
    "- 穗肥（不是碎肥/水费/食肥）\n"
    "- 孕穗期（不是运碎期）、出穗期（不是出碎期）\n"
    "- 分蘖肥（不是分裂肥）、基肥（底肥）\n"
    "- 贪青晚熟（不是贪心晚熟/贪清晚熟/摊清碗苏/碎心晚熟）\n"
    "- 吉林省（不是麒麟省）、稻农、尿素、每公顷、公斤、亩\n"
    "只输出改正后的文本，不要任何解释，不要增删内容，不要改变原意。\n\n"
)
_PUNCT_CHUNK = 1500  # 每批字符数，避免超出 LLM 上下文


async def _call_llm_punctuate(chunk: str, llm: dict) -> str:
    base_url = llm.get("base_url", "http://192.168.1.5:8002/v1")
    model = llm.get("model", "Qwen3-VL-8B-FP8")
    api_key = llm.get("api_key") or ""
    prompt = (llm.get("prompt") or _PUNCT_PROMPT).rstrip() + "\n\n"  # 可配置，默认内置
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt + chunk}],
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        j = resp.json()
        return j["choices"][0]["message"]["content"].strip()


async def _punctuate(text: str) -> str:
    """用 LLM（默认本地 vLLM 多模态模型）给无标点的转写文本加标点断句。
    未配置 LLM 或调用失败时，原样返回原文。
    """
    if not text or not text.strip():
        return text
    llm = cfg.get_section("llm")
    if not llm or not llm.get("base_url"):
        return text
    chunks = [text[i : i + _PUNCT_CHUNK] for i in range(0, len(text), _PUNCT_CHUNK)]
    parts = []
    for c in chunks:
        try:
            parts.append(await _call_llm_punctuate(c, llm))
        except Exception:
            parts.append(c)  # 单批失败则保留原文，不阻塞
    return "\n".join(parts)


# ---------------------------------------------------------------- 统一入口
async def transcribe(audio_path: str, engine: str = None, conf: dict = None) -> str:
    engine = engine or cfg.get_section("asr", "engine") or "local"
    if conf is None:
        conf = cfg.get_section("asr", engine) or {}
    if engine == "local":
        text = await _local_transcribe(audio_path, conf)
        return await _punctuate(text)
    if engine == "xfyun":
        text = await _xfyun_transcribe(audio_path, conf)
        return await _punctuate(text)
    if engine == "feishu":
        return await _feishu_transcribe(audio_path, conf)
    raise ValueError(f"未知语音引擎: {engine}")
