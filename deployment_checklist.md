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

### P2 `pyproject.toml` 作者是占位符

```toml
authors = [
    { name = "Your Name", email = "your.email@example.com" }
]
```

发布到 PyPI 或 Docker Hub 会很难看。

### P2 缺少生产运维文档

没有：
- `DEPLOYMENT.md` 或 `docs/deployment.md`
- 环境变量完整清单和示例
- 升级/回滚步骤
- 监控告警方案

### P2 没有集中式日志 / 指标

- 审计日志写到本地文件，生产难以聚合
- 没有 Prometheus / Grafana 指标
- 没有结构化日志（JSON）开关
- 没有错误追踪（Sentry）集成点

### P2 WebSocket / SSE 没有连接数限制

生产直接暴露，长连接可能把服务器拖垮。

### P2 没有 graceful shutdown 超时配置

Dockerfile 有 `STOPSIGNAL SIGTERM`，但没有 `uvicorn` 的 `--graceful-timeout` 配置。

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
| P2 | 填写 `pyproject.toml` 作者信息 | 元数据完整 |
| P2 | 写 `DEPLOYMENT.md` | 降低运维成本 |
| P2 | 增加 Prometheus/结构化日志集成点 | 可观测性 |

## 总体评估

**当前状态：黄灯偏红。**

代码质量、测试、容器化基础已经具备，但 README 变量名错误、LICENSE 缺失、没有生产 compose 和备份策略，直接部署有较高风险。建议先把 P0 + P1 前 5 项补齐，再考虑真正上线。
