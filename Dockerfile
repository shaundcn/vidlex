# 单段构建（技术选型第 2 节）：python:3.13-slim 按 digest 固定 + apt 安装 ffmpeg
FROM python:3.13-slim@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39

# ffmpeg 来自 Debian 包，许可证说明（GPL）在 /usr/share/doc/ffmpeg/copyright，源码见 README
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
# uv 只在构建时挂载使用，不留在镜像里；按锁文件安装，不装开发依赖
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21,source=/uv,target=/bin/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --frozen --no-dev

COPY backend/app /app/backend/app
WORKDIR /app/backend

EXPOSE 8462
# 容器以 root 运行，用来把新建文件的属主改成与视频库一致（技术选型第 6 节）
CMD ["python", "-m", "app.main"]
