# ============================================================
# OpenClaw — Makefile 常用命令速查
# ============================================================
# 使用方式:
#   make <target>
#
# 示例:
#   make install      # 安装依赖
#   make test         # 运行全部测试
#   make lint         # 代码检查
#   make serve        # 启动开发服务
#   make docker-up    # Docker 一键启动
# ============================================================

# ── 配置 ──
PYTHON    ?= python
PYTEST    ?= pytest
RUFF      ?= ruff
MYPY      ?= mypy
DOCKER    ?= docker
COMPOSE   ?= docker compose

# ── 帮助 ──
.PHONY: help
help: ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── 安装 ──
.PHONY: install install-dev
install: ## 安装生产依赖
	$(PYTHON) -m pip install -e .

install-dev: ## 安装开发依赖（含测试、lint、pre-commit）
	$(PYTHON) -m pip install -e ".[dev]"
	$(PYTHON) -m pre_commit install

# ── 代码质量 ──
.PHONY: lint lint-fix format format-check type-check
lint: ## 运行 ruff 代码检查
	$(RUFF) check src/ tests/ cli.py

lint-fix: ## 自动修复 ruff 可修复的问题
	$(RUFF) check --fix src/ tests/ cli.py

format: ## 自动格式化代码
	$(RUFF) format src/ tests/ cli.py

format-check: ## 检查代码格式（CI 使用）
	$(RUFF) format --check src/ tests/ cli.py

type-check: ## 运行 mypy 类型检查
	$(MYPY) src/ cli.py

# ── 测试 ──
.PHONY: test test-cov test-integration
test: ## 运行单元测试
	PYTHONPATH=src $(PYTEST) tests/unit -q

test-cov: ## 运行测试并生成覆盖率报告
	PYTHONPATH=src $(PYTEST) tests/ --cov=src --cov-report=term-missing --cov-report=html -q

test-integration: ## 运行集成测试
	PYTHONPATH=src $(PYTEST) tests/integration -q

# ── 服务 ──
.PHONY: serve serve-reload ui
serve: ## 启动 API 服务（生产模式）
	PYTHONPATH=src $(PYTHON) cli.py serve --host 0.0.0.0 --port 8000

serve-reload: ## 启动 API 服务（开发热重载）
	PYTHONPATH=src $(PYTHON) cli.py serve --host 0.0.0.0 --port 8000 --reload

ui: ## 启动 Streamlit Web UI
	PYTHONPATH=src streamlit run src/ui/app.py

# ── Docker ──
.PHONY: docker-build docker-up docker-down docker-logs
docker-build: ## 构建 Docker 镜像
	$(DOCKER) build -t openclaw:latest .

docker-up: ## Docker Compose 启动 API 服务
	$(COMPOSE) up -d api

docker-up-full: ## Docker Compose 启动 API + UI
	$(COMPOSE) --profile full up -d

docker-down: ## 停止所有 Docker 容器
	$(COMPOSE) down

docker-logs: ## 查看 Docker 日志
	$(COMPOSE) logs -f api

# ── 工具 ──
.PHONY: key token check
check: ## 运行健康检查
	PYTHONPATH=src $(PYTHON) cli.py check

key: ## 生成新的 API Key
	PYTHONPATH=src $(PYTHON) cli.py key

token: ## 签发 JWT Token（需设置 OPENCLAW_JWT_SECRET）
	PYTHONPATH=src $(PYTHON) cli.py token --sub user123

# ── 清理 ──
.PHONY: clean
clean: ## 清理缓存和构建产物
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	rm -f coverage.xml .coverage
