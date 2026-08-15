# OpenClaw 部署就绪检查报告

检查时间：2026-08-14
检查范围：Docker / Compose / CI / 配置 / 安全 / 文档 / 运维

## ✅ 已有的、做得不错的地方

| 项目 | 状态 | 说明 |
|------|------|------|
| Dockerfile | ✅ | 多阶段构建、非 root 用户、HEALTHCHECK、tini init |
| docker-compose.yml | ✅ | API + UI、数据卷、资源限制、健康检查 |
| GitHub Actions CI | ✅ | lint / test / docker build / 可选 push |
| 测试覆盖 | ✅ | 110 passed, 1 skipped |
| pre-commit | ✅ | ruff + mypy + conventional commit |
| Makefile | ✅ | 常用命令封装完整 |
| .gitignore / .dockerignore | ✅ | 密钥、数据、缓存都排除了 |
| 健康检查端点 | ✅ | `/health` |
| API Key / JWT 认证 | ✅ | 已实现，已改为默认 apikey |
| CORS 收紧 | ✅ | 已改为环境变量驱动，默认本地来源 |

## 🔴 部署前必须补的（P0/P1）

### P0 关键配置错误：README 变量名和代码不一致

README 写的变量名和实际代码读取的不一样，用户按 README 配会直接跑不起来：

| README 写的 | 代码实际读取的 | 位置 |
|------------|--------------|------|
| `OPENCLAW_LLM_API_KEY` | `LLM_API_KEY` | `src/core/settings.py` |
| `OPENCLAW_LLM_BASE_URL` | `LLM_BASE_URL` | `src/core/settings.py` |
| `OPENCLAW_LLM_MODEL` | `LLM_MODEL` | `src/core/settings.py` |
| `OPENCLAW_MEMORY_DB` | `MEMORY_DB_PATH` | `src/core/settings.py` |
| `OPENCLAW_RAG_DIR` | `RAG_VECTOR_STORE_PATH` | `src/core/settings.py` |

`.env.example` 顶部还有 `OPENAI_API_KEY`，代码里实际用的是 `LLM_API_KEY`。

**风险**：第一次部署的人按 README 填完发现服务读不到 key，LLM 调用全失败。

### P0 LICENSE 文件缺失

README 写了 MIT License，但仓库根目录没有 `LICENSE` 文件。GitHub 不会识别为 MIT 仓库，且存在法律风险。

### P1 没有生产级 docker-compose.prod.yml

现有的 `docker-compose.yml` 适合本地开发/单节点，但生产缺：
- 反向代理（Caddy / Nginx）
- HTTPS 自动证书
- 不直接暴露 8000 端口到公网
- 没有 WebSocket 升级配置
- 没有请求速率限制 / WAF

### P1 没有 `data/.gitkeep`

`.gitignore` 排除了整个 `data/` 目录，但代码运行时依赖 `data/` 存在。新 clone 的仓库没有 `data/` 目录，首次启动会报错。应加 `data/.gitkeep` 并在 `.gitignore` 中 `!data/.gitkeep`。

（`.gitignore` 里已经写了 `!data/.gitkeep`，但实际文件不存在。）

### P1 Dockerfile 没有从 pyproject.toml 安装依赖

Dockerfile 里把依赖硬编码写了一遍，和 `pyproject.toml` 不一致：
- 缺少 `unstructured[all-docs]` 等文档解析依赖（除非加 `INSTALL_EXTRAS=true`）
- 版本号可能和 pyproject 漂移
- 维护两份依赖清单容易出错

**建议**：`COPY pyproject.toml ./` 后直接用 `pip install -e .` 或 `pip install -e ".[all]"`。

### ✅ P1 CI 的 Docker 健康检查参数

已在 `.github/workflows/ci.yml` 的 `Test Docker image health` 步骤中：
- 使用正确变量名：`LLM_API_KEY=mock`、`EMBEDDING_API_KEY=mock`
- 关闭外部依赖：`OPENCLAW_AUTH_MODE=none`、`OPENCLAW_RATE_LIMIT_ENABLED=false`、`LANGCHAIN_TRACING_V2=false`
- 将固定 `sleep 5` 改为最多 60 秒轮询 `/health`，输出 JSON 报告后再清理容器
- 修复了 Docker Hub 推送条件，避免 `schedule` / `workflow_dispatch` 触发时访问不存在的 inputs

`Dockerfile` 与 `docker-compose.prod.yml` 已保留 `HEALTHCHECK` / `healthcheck` 配置。

### P1 没有请求限流 / 防暴力破解

生产环境直接暴露 API，没有：
- 全局速率限制（Rate Limiting）
- 单 IP 限制
- 上传文件大小 / 类型二次校验（虽然配置里有，但代码层是否严格检查需确认）
- 认证失败锁定 / 延迟

### P1 ~~没有备份与恢复方案~~ ✅ 已完成

生产数据在 `data/` 里（SQLite + Chroma 向量库 + 上传文件 + 审计日志）。

已实现：
- `scripts/backup.py`：SQLite 在线安全备份（`Connection.backup`，服务运行时可用）+ 非 SQLite 文件复制 + tar.gz 打包 + 自动清理过期备份（`--keep`）
- `scripts/backup.sh`：Linux cron 入口（自动探测 venv）
- `BACKUP.md`：备份范围、手动/cron/异地同步命令、恢复流程与演练建议
- 剩余可做：数据卷快照策略、备份上传到对象存储（S3/OSS）自动化

### ✅ P1 没有数据库迁移机制

使用 SQLite + aiosqlite，但没有 Alembic 或任何 schema 迁移工具。如果未来 memory/audit 表结构变更，升级会炸。

已接入 Alembic 管理主库 `memory.db`（`memories` + `business_sessions` + `_schema_version`）：
- `alembic/` + `alembic.ini`：`env.py` 动态解析库路径（`settings.memory.db_path`，可用 `ALEMBIC_DB_PATH` 覆盖）；baseline 迁移 `be41079bf6fb` 用幂等 DDL 平滑接管旧库，不丢数据
- 使用：`make migrate`（升级）/ `make migration name="..."`（新脚本）
- 依赖：`alembic>=1.13.0` + `sqlalchemy>=2.0.0`（已加入 pyproject 主依赖）
- 测试：`tests/unit/test_migrations.py` 4 项（全新库 / 旧库接管 / downgrade / current）
- 说明见 README「数据库迁移」节

注：`state.db` / `cost_records.db` / `review_history.db` 为独立小型库，结构稳定，暂由应用幂等建表；未来如需版本管理可在 env.py 扩展多库支持。

## 🟡 强烈建议补的（P2）

### ✅ P2 `config/.env.example` 冗余

已删除 `config/.env.example`（整个 `config/` 目录唯一文件）。代码/文档无任何引用，根目录 `.env.example` 更完整且已包含限流、认证、CORS、域名等新变量。

### ✅ P2 `pyproject.toml` 作者是占位符

已填写为 `zq66q <zq66q@users.noreply.github.com>`（commit `e5620b0` 时同步更新）。

### ✅ P2 缺少生产运维文档

已新增 [`DEPLOYMENT.md`](DEPLOYMENT.md)，覆盖：
- 架构概览与前置条件
- 首次部署步骤、环境变量完整清单
- 认证/安全基线、升级/回滚流程
- Docker 场景备份与恢复（含 cron）
- 日志、健康检查、故障排查、性能调优、运维速查

### ✅ 顺带修复: Dockerfile 的 `openclaw` 入口失效 bug

验证发现 `pip install .` 后 `pip uninstall -y openclaw` 会删除 `/opt/venv/bin/openclaw` 入口脚本，且 `cli` 模块位于项目根（不在安装的 src-layout 包内），导致 `CMD ["openclaw", ...]` 启动即失败。修复：
- 删除 `pip uninstall -y openclaw` 行
- 运行时 `ENV PYTHONPATH="/app"`，使 console script 能 `import cli`
- 已在等价模拟环境验证 `openclaw --help` 与 `import cli/api.server/business.service_facade` 均正常

### ✅ P2 没有集中式日志 / 指标

已提供可观测性基础（零新增依赖）：
- **Prometheus `/metrics`**：`src/api/metrics.py` 内置采集器（Prometheus 文本格式），HTTP 请求计数 / 耗时直方图、活跃流式连接数、运行时长；`OPENCLAW_METRICS_ENABLED=false` 可关闭；`/metrics` 已加入认证白名单
- **结构化日志**：`OPENCLAW_LOG_FORMAT=json` 让控制台输出 JSON（容器内易被采集）；文件日志本就是 JSONL
- SSE 流端点已接入 `StreamCounter` 统计活跃连接
- 测试：`tests/unit/test_metrics.py` 6 项
- 剩余可选：错误追踪（Sentry）集成、日志聚合（Loki/ELK）

### ✅ P2 WebSocket / SSE 没有连接数限制

已实现并发上限：
- `src/api/metrics.py` 的 `Metrics` 新增 `max_streams`（`OPENCLAW_MAX_STREAMS` 环境变量，默认 64，`0` 表示不限）与 `try_enter_stream()`；被拒连接计入 `openclaw_streams_rejected_total` 指标
- `StreamCounter` 提供 `try_enter()` / `release()`（幂等），SSE 与 WebSocket 共享同一配额
- `/chat/stream` 超限返回 `503`；`/chat/ws` 超限先发 error 再以 `code 1013` 关闭
- 测试：`tests/unit/test_metrics.py` 的 `TestStreamLimit` 4 项（含 SSE 503 / WS 1013 集成测试）
- 修复：`tests/conftest.py` 的 `_patch_env` 先触发 `core.settings` 的一次性 `load_dotenv(override=True)`，避免 `.env` 污染测试环境

### ✅ P2 没有 graceful shutdown 超时配置

已在启动链路加入优雅停机超时：
- `src/api/server.py` 的 `start()` 新增 `timeout_graceful_shutdown`（默认 30s，None/0 不限时）与 `workers` 透传
- `cli.py` 的 `serve` 子命令新增 `--graceful-timeout`（默认 30，0 表示不限时）
- `Dockerfile` 的 `CMD` 显式传 `--graceful-timeout 30`，配合 `STOPSIGNAL SIGTERM`，滚动升级时最多等 30s 让在途请求收尾

## 🔵 可以后续再做的（P3）

- CHANGELOG.md
- SECURITY.md（漏洞上报流程）
- CONTRIBUTING.md
- 性能基准测试
- 多语言 README

## 📋 按优先级排序的待办清单

| 优先级 | 任务 | 影响 |
|--------|------|------|
| P0 | 统一 README / `.env.example` / `pyproject.toml` 变量名 | 避免部署即翻车 |
| P0 | 添加 LICENSE 文件 | 合规 |
| P1 | 创建 `docker-compose.prod.yml` + Caddyfile（HTTPS） | 安全上线 |
| P1 | 创建 `data/.gitkeep` | 新环境可启动 |
| P1 | Dockerfile 改用 `pyproject.toml` 安装依赖 | 避免依赖漂移 |
| P1 | 修复 CI Docker 健康检查参数 | CI 稳定 |
| P1 | 增加 Rate Limiting 中间件 | 防攻击 |
| ~~P1~~ | ✅ ~~增加数据备份脚本 / 文档~~ | 数据安全 |
| ~~P1~~ | ✅ ~~增加 SQLite 迁移机制（Alembic）~~ | 可升级 |
| ✅ | ~~删除/合并 `config/.env.example`~~ | 已删除 |
| ✅ | ~~填写 `pyproject.toml` 作者信息~~ | 已填写（e5620b0） |
| ✅ | ~~写 `DEPLOYMENT.md`~~ | 已交付 |
| ✅ | ~~优雅停机超时配置~~ | 已加 `--graceful-timeout` |
| ✅ | ~~Prometheus/结构化日志集成点~~ | 已加 `/metrics` + JSON 日志开关 |
| ✅ | ~~WebSocket/SSE 连接数限制~~ | 已加 `OPENCLAW_MAX_STREAMS`（默认 64）|

## 总体评估

**当前状态：黄灯偏红。**

代码质量、测试、容器化基础已经具备，但 README 变量名错误、LICENSE 缺失、没有生产 compose 和备份策略，直接部署有较高风险。建议先把 P0 + P1 前 5 项补齐，再考虑真正上线。

---

## 📌 2026-08-15 状态更新（CI 全绿 + 可观测性补齐）

### 本次更新内容

| 类别 | 变更 | 说明 |
|------|------|------|
| CI 修复 | `pyproject.toml` 加 `mypy_path` / `explicit_package_bases` | 修复 mypy 内部导入被解析为 `Any` 的根因 |
| CI 修复 | `document_parser.py` 循环变量改名 | 修复 pdfplumber `Page` 与 PyPDF2 `PageObject` 类型冲突 |
| CI 修复 | `docker buildx build --load` | 修复 `--cache-to type=local` 不兼容传统 builder |
| CI 修复 | Docker Hub 推送改为 `workflow_dispatch` 手动触发 | 未配置 secrets 时 push 不再导致 CI 失败 |
| 可观测性 | `docker-compose.prod.yml` 新增 `prometheus` + `grafana` 服务（`monitoring` profile） | `/metrics` 采集 + 可视化开箱即用 |
| 可观测性 | 新增 `monitoring/prometheus.yml`、`monitoring/grafana/provisioning/` | 抓取配置 + 数据源自动注册 |
| 文档 | `DEPLOYMENT.md` §11 补充监控栈接入说明 | 含 SSH 隧道访问与安全提示 |

### CI 最终结果（commit `7a5b6e8`）

- ✅ Lint & Format Check（mypy + ruff）
- ✅ Test Suite（Python 3.10 / 3.11 / 3.12）
- ✅ Docker Build（镜像构建 + 容器健康检查）
- 本地验证：`mypy` 0 错误，`ruff check/format` 通过，pytest 143 passed / 1 skipped

### 剩余部署缺口（仓库外资源）

- 域名 + DNS（`api.` / `app.` 子域名）
- Linux 服务器（≥4 核 8G，Docker 24+）
- 轮换本地 `.env` 中已暴露的 6 个 API key（DeepSeek / DashScope / LangSmith / Tavily / OPENCLAW_API_KEYS）
- 备份异地同步（对象存储上传脚本，可选）
- 外部探活告警（UptimeRobot / 云监控）
