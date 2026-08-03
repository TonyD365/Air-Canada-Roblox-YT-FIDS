# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Crane-Airlines-YT-FIDS" \
      org.opencontainers.image.description="Airport style FIDS rendered with Pillow and streamed to YouTube via FFmpeg" \
      org.opencontainers.image.source="https://github.com/TonyD365/Air-Canada-Roblox-YT-FIDS" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN rm -f /etc/apt/sources.list.d/* && \
    printf "deb http://ftp.us.debian.org/debian bookworm main\n\
deb http://ftp.us.debian.org/debian bookworm-updates main\n\
deb http://security.debian.org/debian-security bookworm-security main\n" > /etc/apt/sources.list && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        fontconfig \
        fonts-dejavu-core \
        fonts-liberation \
        tini \
        git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 可修改仓库及分支
ARG GIT_REPO=https://github.com/TonyD365/Air-Canada-Roblox-YT-FIDS.git
ARG GIT_BRANCH=main

# Clone 最新代码
RUN git clone --depth=1 --branch ${GIT_BRANCH} ${GIT_REPO} .

# 安装 Python 依赖
RUN pip install -r requirements.txt

# 创建普通用户
RUN useradd --create-home --uid 10001 fids \
    && chown -R fids:fids /app

USER fids

ENV WIDTH=1920 \
    HEIGHT=1080 \
    FPS=30 \
    LOG_LEVEL=INFO \
    AIRPORT_NAME="CRANE INTERNATIONAL AIRPORT" \
    FONT_DIR=/app/assets/fonts

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
CMD python -c "import sys; from crane_fids.config import Config; from crane_fids.renderer import FidsRenderer; sys.exit(0 if FidsRenderer.from_config(Config.from_env()) else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["python", "app.py"]
