# ============================================================
# OpenClaw — 企业级多 Agent 业务自动化平台
# 生产 Docker 镜像（多阶段构建）
# ============================================================
#
# 构建:
#   docker build -t openclaw:latest .
#
# 运行:
#   docker run -p 8000:8000 \
#     -v $(pwd)/data:/app/data \
#     --env-file .env \
#     openclaw:latest
#
# 构建参数:
#   --build-arg INSTALL_EXTRAS=true   # 安装文档解析等重型依赖
#
# ============================================================

# ── Stage 1: 依赖安装 ──
FROM python:3.11-slim-bookworm AS builder

SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]

# 构建依赖
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python 环境
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装依赖到虚拟环境
# 依赖清单来自 pyproject.toml（单一事实来源），避免与 Dockerfile 硬编码清单漂移
WORKDIR /app
COPY pyproject.toml README.md ./

# 创建 venv 并安装依赖（核心依赖；INSTALL_EXTRAS=true 时额外安装文档解析等重型依赖）
ARG INSTALL_EXTRAS=false
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip setuptools wheel && \
    if [ "$INSTALL_EXTRAS" = "true" ]; then \
        /opt/venv/bin/pip install ".[rag]"; \
    else \
        /opt/venv/bin/pip install .; \
    fi \
    && echo "Dependencies installed."
    # 注意: 不要 pip uninstall openclaw —— 那会同时删除 /opt/venv/bin/openclaw
    # 入口脚本，导致 CMD ["openclaw", ...] 启动即失败。


# ── Stage 2: 生产运行时 ──
FROM python:3.11-slim-bookworm AS runtime

SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]

# 运行时系统依赖
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN groupadd -r openclaw -g 1000 && \
    useradd -r -g openclaw -u 1000 -m -s /bin/bash openclaw

# 复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
# PYTHONPATH 指向 /app: cli 模块位于项目根（非包内），console script 入口
# `openclaw` 运行时需要 import cli；cli.py 内部会再注入 /app/src。
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app"

# 复制应用代码
WORKDIR /app
COPY --chown=openclaw:openclaw . .

# 确保数据目录存在并设置权限
RUN mkdir -p /app/data/vector_db /app/data/uploads /app/data/audit_logs && \
    chown -R openclaw:openclaw /app

# 安全: 切换到非 root 用户
USER openclaw

# 暴露端口
EXPOSE 8000

# 优雅退出
STOPSIGNAL SIGTERM

# Health Check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

# Tini 作为 init 进程（正确处理信号、僵尸进程回收）
ENTRYPOINT ["/usr/bin/tini", "--"]

# 启动命令（通过 pyproject.toml 注册的 openclaw 入口）
CMD ["openclaw", "serve", "--host", "0.0.0.0", "--port", "8000"]
