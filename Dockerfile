# douyin-distiller 镜像
# 说明：whisper ASR 走外部服务（8003），本镜像仅内置 ffmpeg 用于提取音轨
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DATA_DIR=/data
# 下载/数据库挂载目录（docker-compose 会挂载宿主 ./data）
RUN mkdir -p /data

EXPOSE 9009

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9009"]
