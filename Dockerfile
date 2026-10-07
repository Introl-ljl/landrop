# LAN Drop - 局域网文件收集与快捷分享
# 轻量、零第三方依赖的 Python 镜像（标准库 + SQLite）
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANDROP_DATA_DIR=/data

WORKDIR /app

COPY landrop/ /app/landrop/
# Metadata and collected files use the same persistent volume.
RUN mkdir -p /data && chown -R 10001:0 /app /data

RUN useradd -r -u 10001 -g root -d /app appuser
USER appuser

EXPOSE 8000

# exec 形式：python 是 1 号进程，能正确接收 SIGTERM 优雅退出
ENTRYPOINT ["python", "-m", "landrop", "serve"]
CMD ["--port", "8000", "--collect-dir", "/data/collected"]
