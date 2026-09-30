# LAN Drop - 局域网文件收集与快捷分享
# 轻量、零第三方依赖的 Python 镜像（标准库 + SQLite）
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY server.py store.py /app/
COPY static/ /app/static/
# 命令行客户端：容器内可用，也通过 /static/downloads/ 提供下载（Linux 二进制由 pyinstaller 构建后放入该目录）
COPY landrop.py /app/
COPY landrop.py /app/static/downloads/landrop.py

# 数据目录：状态库、文件、分块、回收站都由 compose 挂载宿主机
RUN mkdir -p /data && chown -R 10001:0 /app /data

RUN useradd -r -u 10001 -g root -d /app appuser
USER appuser

EXPOSE 8000

# exec 形式：python 是 1 号进程，能正确接收 SIGTERM 优雅退出
ENTRYPOINT ["python", "/app/server.py"]
CMD ["--data-dir", "/data", "--host", "0.0.0.0", "--port", "8000"]
