# LAN Drop - 局域网文件收集与快捷分享
# 轻量、零第三方依赖的 Python 镜像（标准库 + SQLite）
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY landrop/ /app/landrop/
COPY packaging/build_pyz.py /app/packaging/build_pyz.py

# 把单文件客户端放进静态目录：服务起来后可在 /downloads/landrop.pyz 下载，给没装过任何东西的电脑用
RUN mkdir -p /app/landrop/server/static/downloads \
 && python /app/packaging/build_pyz.py /app/landrop/server/static/downloads/landrop.pyz

# 数据目录：状态库、文件、分块、回收站都由 compose 挂载宿主机
RUN mkdir -p /data && chown -R 10001:0 /app /data

RUN useradd -r -u 10001 -g root -d /app appuser
USER appuser

EXPOSE 8000

# exec 形式：python 是 1 号进程，能正确接收 SIGTERM 优雅退出
ENTRYPOINT ["python", "-m", "landrop", "serve"]
CMD ["--data-dir", "/data", "--host", "0.0.0.0", "--port", "8000"]
