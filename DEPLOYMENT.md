# OpenClaw 生产部署与运维手册

本文档面向生产环境部署与长期运维，覆盖架构、部署步骤、环境变量、安全基线、升级/回滚、备份恢复、监控与故障排查。

相关文档：
- [`README.md`](README.md) — 快速开始、开发部署
- [`BACKUP.md`](BACKUP.md) — 备份与恢复详细策略
- [`deployment_checklist.md`](deployment_checklist.md) — 部署就绪检查报告

---

## 1. 架构概览

生产部署使用 `docker-compose.prod.yml`，由 Caddy 统一接收公网流量并自动签发 HTTPS 证书，API / UI 容器不直接暴露端口到公网。

```
                 公网 80/443
                      │
              ┌───────▼────────┐
              │  Caddy 反向代理 │  自动 HTTPS (Let's Encrypt)
              │  docker 容器   │
              └───┬───────┬────┘
                  │       │
        https://api.<域>  https://app.<域>
                  │       │
          ┌───────▼──┐ ┌──▼────────┐
          │ api:8000 │ │ ui:8501   │  (--profile full 才启动)
          │ FastAPI  │ │ Streamlit │
          └───────┬──┘ └───────────┘
                  │
          ┌───────▼────────────┐
          │ openclaw_data 数据卷 │  memory.db / state.db / vector_db /
          │ /app/data          │  rag / audit_logs / uploads
          └─────────────────────┘
```

| 组件 | 镜像/入口 | 职责 |
|------|-----------|------|
| `caddy` | `caddy:2-alpine` | 反向代理、自动 HTTPS、gzip、安全响应头 |
| `api` | `openclaw` 镜像 | FastAPI 服务（认证/限流/RAG/记忆/MCP） |
| `ui` | `openclaw` 镜像 | Streamlit Web UI（可选） |
| 数据卷 | `openclaw_data` | 全部运行时数据持久化 |

与开发版 `docker-compose.yml` 的区别：**仅开放 80/443**，8000/8501 通过 `expose` 只在 compose 内网可访问；开发版会直接映射端口到宿主机。

---

## 2. 部署前置条件

| 项目 | 要求 |
|------|------|
| 服务器 | Linux x86_64（Debian/Ubuntu 示例），建议 ≥ 4 核 / 8G 内存 |
| Docker | Docker Engine 24+ 且 `docker compose`（v2 插件） |
| 域名 | 一个主域名，`api.` 与 `app.` 子域名 A 记录指向服务器公网 IP |
| 端口 | 80、443 可被公网访问（Caddy 签证书需要） |
| 外部服务 | LLM API Key、Embedding API Key（RAG 需要）；Tavily / LangSmith 可选 |
| 时长 | Caddy 首次签发证书约需几十秒，期间访问会 502 |

> 没有公网域名时无法签发正式证书；本地联调可在 Caddyfile 的 site 块中加入 `tls internal` 使用自签名证书。

---

## 3. 首次部署

以服务器上项目目录 `/opt/openclaw` 为例：

```bash
# 1. 获取代码
git clone https://github.com/zq66q/openclow.git /opt/openclaw
cd /opt/openclaw

# 2. 配置环境变量（按需修改，见第 4 节）
cp .env.example .env
vi .env

# 3. 生成 API Key（生产必做，禁止用模板里的占位 key）
#    本地生成后写入 .env 的 OPENCLAW_API_KEYS（逗号分隔可多个）
python -m pip install -e . --quiet   # 首次需安装依赖（或用项目 venv）
PYTHONPATH=src python cli.py key

# 4. 启动（首次会构建镜像，耗时几分钟）
docker compose -f docker-compose.prod.yml up -d --build

# 5. 验证
docker compose -f docker-compose.prod.yml ps            # 全部 healthy
curl -sf https://api.你的域名/health | python -m json.tool
```

访问入口：
- API 文档：`https://api.你的域名/docs`
- Web UI：`https://app.你的域名`（需用 `--profile full` 启动）

```bash
# 含 Web UI 的启动方式
docker compose -f docker-compose.prod.yml --profile full up -d --build
```

---

## 4. 环境变量参考

完整模板见 [`.env.example`](.env.example)。以下为生产重点：

### 必填（不填服务不可用）

| 变量 | 说明 | 生产建议 |
|------|------|----------|
| `LLM_API_KEY` | 主 LLM 的 API Key | **必填**；留空会回退成 `mock`，对话全失败 |
| `OPENCLAW_DOMAIN` | 部署域名，如 `example.com` | Caddy 据此签发证书 |
| `OPENCLAW_AUTH_MODE` | `apikey` / `jwt` / `both` / `none` | **禁止 `none`** |
| `OPENCLAW_API_KEYS` | 逗号分隔的 API Key 列表 | 用 `openclaw key` 生成 |

### 认证与安全

| 变量 | 默认 | 说明 |
|------|------|------|
| `OPENCLAW_AUTH_MODE` | `apikey` | 认证模式 |
| `OPENCLAW_API_KEYS` | — | 静态 API Key 列表 |
| `OPENCLAW_JWT_SECRET` | — | JWT 签名密钥（≥32 字节）；`openssl rand -hex 32` 生成 |
| `OPENCLAW_JWT_EXPIRE_HOURS` | `24` | JWT 有效期 |
| `OPENCLAW_CORS_ORIGINS` | 本地来源 | 逗号分隔，**务必改成前端真实域名** `https://app.你的域名` |
| `OPENCLAW_RATE_LIMIT_ENABLED` | `true` | 是否启用限流 |
| `OPENCLAW_RATE_LIMIT_PER_MINUTE` | `120` | 每 IP+Key 每分钟上限 |
| `OPENCLAW_RATE_LIMIT_WINDOW_SECONDS` | `60` | 限流窗口 |

> ⚠️ 限流为单进程内存实现。`API_WORKERS > 1` 时总上限会按进程数放大，需要同时提高阈值或用外部限流。

### 模型服务

| 变量 | 说明 |
|------|------|
| `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_MODEL` | 主模型（默认 OpenAI 兼容） |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | 生成参数 |
| `LLM_MINI_MODEL` | 摘要/路由等低成本小模型 |
| `EMBEDDING_API_KEY` | Embedding 模型 Key（RAG 检索需要） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` | Embedding 端点 |

### 数据与存储（容器内已被 compose 固定，勿改）

容器内固定为 `/app/data`，`.env` 中对应的相对路径配置在容器里不生效：

| 容器内路径 | 内容 |
|-----------|------|
| `/app/data/memory.db` | 记忆库（Alembic 管理） |
| `/app/data/state.db/` | Agent 状态 |
| `/app/data/vector_db/` | Chroma 向量库 |
| `/app/data/rag/` | RAG 文档切片 |
| `/app/data/audit_logs/` | 审计日志（jsonl） |
| `/app/data/uploads/` | 文件上传沙箱 |

### 其他常用

| 变量 | 默认 | 说明 |
|------|------|------|
| `OPENCLAW_VERSION` | `latest` | 镜像标签，升级/回滚用 |
| `API_WORKERS` | `1` | uvicorn worker 数（见限流注意） |
| `LANGCHAIN_TRACING_V2` | `true` | LangSmith 链路追踪开关 |
| `LANGCHAIN_API_KEY` | — | LangSmith Key |
| `TAVILY_API_KEY` | — | 联网搜索 |
| `ALLOWED_FILE_EXTENSIONS` | `pdf,docx,...` | 允许上传的类型白名单 |
| `MAX_UPLOAD_SIZE_MB` | `10` | 上传大小上限 |
| `AUDIT_LOG_LEVEL` | `INFO` | 审计日志级别 |
| `OPENCLAW_LOG_FORMAT` | `text` | 控制台日志格式：`text`（彩色） / `json`（结构化，容器内采集推荐） |
| `OPENCLAW_METRICS_ENABLED` | `true` | Prometheus `/metrics` 开关，`false` 关闭 |
| `OPENCLAW_MAX_STREAMS` | `64` | SSE `/chat/stream` 与 WebSocket `/chat/ws` 并发连接上限，`0` 表示不限；超限返回 `503` / `1013` |

---

## 5. 认证与安全基线

上线前逐项核对：

- [ ] **认证模式**：`OPENCLAW_AUTH_MODE` 为 `apikey` / `jwt` / `both`，绝不是 `none`
- [ ] **API Key**：使用 `openclaw key` 生成的真实随机 Key，不要用模板占位符
- [ ] **CORS**：`OPENCLAW_CORS_ORIGINS` 只包含你控制的域名
- [ ] **HTTPS**：Caddy 自动签发，确认 80/443 可达；避免中间用 HTTP 明文访问
- [ ] **限流**：保持 `OPENCLAW_RATE_LIMIT_ENABLED=true`，根据并发调整阈值
- [ ] **非 root**：镜像内以 `openclaw` 用户运行（Dockerfile 已配置）
- [ ] **密钥不入库**：`.env` 已被 `.gitignore` 排除，切勿提交
- [ ] **数据卷隔离**：API 端口未暴露到宿主机，只有 Caddy 对公网

生成/轮换 API Key：

```bash
# 生成新 Key（容器内）
docker compose -f docker-compose.prod.yml exec api openclaw key
# 或源码目录下
PYTHONPATH=src python cli.py key
```

轮换流程：新 Key 追加到 `OPENCLAW_API_KEYS` → 重启 → 客户端切到新 Key → 移除旧 Key 再重启。

---

## 6. 数据库迁移

主库 `memory.db`（记忆 + 会话表）由 Alembic 管理。**升级应用前**如果有 schema 变更，先执行迁移：

```bash
# Docker 部署（容器内已配置 MEMORY_DB_PATH=/app/data/memory.db）
docker compose -f docker-compose.prod.yml exec api alembic upgrade head

# 裸机 / 源码部署
make migrate          # 等价: alembic upgrade head
```

生成新迁移脚本（结构变更时，需手动填写 upgrade/downgrade）：

```bash
make migration name="add_xxx_column"    # 裸机
# 容器内: docker compose ... exec api alembic revision -m "add_xxx_column"
```

要点：
- 对旧部署（应用已自建表、无 `alembic_version` 表）同样安全，baseline 用幂等 DDL 平滑接管，不丢数据
- 应用启动时的 `CREATE TABLE IF NOT EXISTS` 幂等建表保留，与 Alembic 并存互不冲突
- `state.db` / 其他独立小型库结构稳定，暂由应用自建，不在 Alembic 管理范围

---

## 7. 升级流程

**原则：先备份、再迁移、后升级。**

```bash
# 0. 备份（必做，见第 9 节）
docker compose -f docker-compose.prod.yml exec api python scripts/backup.py \
  --data /app/data --dir /app/backups

# 1. 获取新代码
cd /opt/openclaw && git fetch origin && git checkout <新 tag 或 commit>

# 2. 应用数据库迁移（如有 schema 变更）
docker compose -f docker-compose.prod.yml exec api alembic upgrade head

# 3. 重新构建并重启（构建缓存加速，只重建有变化的层）
docker compose -f docker-compose.prod.yml up -d --build

# 4. 验证
docker compose -f docker-compose.prod.yml ps      # 全部 healthy
curl -sf https://api.你的域名/health
# 抽样验证：登录对话 / 检索知识库 / 查看日志无 ERROR
```

若采用 CI 推送镜像的方式（GitHub Actions `push_image`），可在 `.env` 设置 `OPENCLAW_VERSION=<tag>` 后：

```bash
docker compose -f docker-compose.prod.yml up -d   # 拉取新 tag 重启
```

---

## 8. 回滚

```bash
# 1. 恢复到上一个正常版本
git checkout <上一个 commit/tag>

# 2. 若本次升级跑过数据库迁移且为"破坏性"变更，回滚数据库
#    先看迁移历史确认可降级版本：
docker compose -f docker-compose.prod.yml exec api alembic history
docker compose -f docker-compose.prod.yml exec api alembic downgrade -1   # 回退一步

# 3. 重新构建重启
docker compose -f docker-compose.prod.yml up -d --build
```

> ⚠️ 生产库建议先恢复备份再回滚（备份最可靠），尤其无法确定迁移是否可逆时。
> `downgrade` 依赖迁移脚本中的 `downgrade()` 实现，破坏性变更（如删列）通常不可逆，以备份为准。

---

## 9. 备份与恢复

裸机部署的完整策略见 [`BACKUP.md`](BACKUP.md)。本节是 **Docker Compose 部署**的用法。

### 备份

数据在 `openclaw_data` 命名卷中，备份脚本需要在容器内执行。推荐给 api 服务加一个宿主机的备份目录挂载：

```yaml
# docker-compose.override.yml（同目录，compose 自动合并）
services:
  api:
    volumes:
      - openclaw_data:/app/data
      - ./backups:/app/backups
```

```bash
# 手动备份（备份文件直接落到宿主机 ./backups）
docker compose -f docker-compose.prod.yml exec api python scripts/backup.py \
  --data /app/data --dir /app/backups

# 定时备份（宿主机 crontab；注意 exec 需 -T，非交互）
0 3 * * * cd /opt/openclaw && docker compose -f docker-compose.prod.yml \
  exec -T api python scripts/backup.py --data /app/data --dir /app/backups \
  >> /var/log/openclaw-backup.log 2>&1
```

备份内容：`memory.db`（在线一致快照）、`state.db`、`vector_db`（Chroma SQLite）、`rag`、`audit_logs`、`uploads`。默认保留 14 份（`--keep N` 调整）。

### 恢复

```bash
# 1. 停服
docker compose -f docker-compose.prod.yml down

# 2. 解压备份到宿主机临时目录
mkdir -p /tmp/restore && tar -xzf backups/openclaw-backup-<时间戳>.tar.gz -C /tmp/restore
# 注意 tar 内层目录名，例如解压后数据在 /tmp/restore/openclaw-backup-<时间戳>/

# 3. 用一次性容器替换卷内容（先备份当前数据更稳妥）
docker run --rm -v openclaw_data:/app/data \
  -v /tmp/restore:/backup \
  alpine sh -c 'rm -rf /app/data/* && cp -a /backup/openclaw-backup-<时间戳>/. /app/data/'

# 4. 重启并验证
docker compose -f docker-compose.prod.yml up -d
curl -sf https://api.你的域名/health
# 抽查记忆/检索是否可读
```

> 建议每月做一次恢复演练，确保备份可用；异地同步见 BACKUP.md 的 rsync 示例。

---

## 10. 日志

```bash
# 容器日志（已配置轮转：10MB × 3 个文件）
docker compose -f docker-compose.prod.yml logs -f api        # 跟踪
docker compose -f docker-compose.prod.yml logs --tail=200 api # 最近 200 行
docker compose -f docker-compose.prod.yml logs -f caddy ui    # 多服务

# 审计日志（jsonl，逐条 JSON，可被日志采集器消费）
docker compose -f docker-compose.prod.yml exec api \
  sh -c 'ls -lh /app/data/audit_logs && tail -5 /app/data/audit_logs/*.jsonl'
```

**结构化日志**：设置 `OPENCLAW_LOG_FORMAT=json` 后，控制台/容器 stdout 也输出逐行 JSON（含 `trace_id`），可直接被 Loki/ELK 等采集器消费：

```bash
# 生产 compose 中开启（在 api 服务的 environment 下加一行）
#   - OPENCLAW_LOG_FORMAT=json
docker compose -f docker-compose.prod.yml exec api sh -c \
  'tail -3 /proc/1/fd/1 2>/dev/null || true'
```

日志轮转参数已在 compose 中配置（`max-size: 10m`、`max-file: 3`），无需额外处理。审计日志是文件形式，长期会增长，建议纳入备份并在日志量大的场景接入集中式采集（ELK/Loki，P2 可观测性项）。

---

## 11. 健康检查与监控

### 内置健康检查

- Dockerfile 与 compose 均配置了 `HEALTHCHECK`（curl `/health`，30s 间隔，失败 3 次标记 unhealthy）
- `/health` 返回 JSON：`status`、`uptime_seconds`、`components`（各组件状态）

```bash
# 命令行健康检查（容器内）
docker compose -f docker-compose.prod.yml exec api openclaw check --url http://localhost:8000

# 公网探活（建议接入外部监控）
curl -sf https://api.你的域名/health
```

### 可观测性现状与建议

| 能力 | 现状 |
|------|------|
| 链路追踪 | 已接入 LangSmith（`LANGCHAIN_TRACING_V2=true` + API Key） |
| 审计日志 | 本地 jsonl，可采集；`OPENCLAW_LOG_FORMAT=json` 可让 stdout 也输出 JSON |
| 进程/容器监控 | Docker `stats` / `docker ps` 人工查看 |
| 指标 | 已内置 `/metrics`（Prometheus 文本格式）：HTTP 请求数/耗时直方图、活跃/累计/被拒的流式连接数、运行时长；免认证白名单 |
| 流式连接保护 | SSE/WS 并发上限 `OPENCLAW_MAX_STREAMS`（默认 64），超限 `/chat/stream` 返回 `503`、`/chat/ws` 关闭 `1013` |

```bash
# 查看内置指标
curl -s http://localhost:8000/metrics | head -30

# 快速看资源占用
docker stats --no-stream
```

建议生产接入：
- 外部探活（UptimeRobot / 云监控 HTTP 探针）盯 `/health`，异常告警
- Prometheus + Grafana 抓取 `/metrics`（docker-compose 加 `prom/prometheus` + `grafana/grafana` 服务即可）

---

## 12. 故障排查

| 现象 | 排查 | 处理 |
|------|------|------|
| 容器一直 `starting` / `unhealthy` | `docker compose logs api` 看启动报错 | 确认 `LLM_API_KEY` 已配置；首次启动模型/向量库初始化较慢，等待 `start_period` 过后再看 |
| 访问 502 Bad Gateway | Caddy 后端不可达 | `docker compose ps` 确认 api 是否 healthy；首次证书签发期间正常，稍等重试 |
| HTTPS 证书未签发 | 80/443 未开放、DNS 未生效 | 检查防火墙与 A 记录；`docker compose logs caddy` 看错误 |
| 对话报模型错误 | LLM 配置问题 | 核对 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`；`LLM_API_KEY` 留空会回退 mock |
| 提示 429 | 触发限流 | 调大 `OPENCLAW_RATE_LIMIT_PER_MINUTE` 或确认客户端 IP 变化 |
| 提示 401/403 | 认证失败 | 核对请求头 `X-API-Key` / Bearer Token 与 `OPENCLAW_API_KEYS` |
| CORS 报错 | 前端来源不在白名单 | 更新 `OPENCLAW_CORS_ORIGINS` 后重启 api |
| 磁盘告警 | 数据卷/日志增长 | 检查 `backups/` 清理策略（`--keep`）、审计日志大小、容器日志轮转 |
| 升级后 schema 报错 | 未跑迁移 | 执行第 6 节 `alembic upgrade head` |

通用排查起点：

```bash
docker compose -f docker-compose.prod.yml ps                # 服务状态
docker compose -f docker-compose.prod.yml logs --tail=100 api   # API 最近日志
curl -s https://api.你的域名/health                          # 健康详情
```

---

## 13. 性能与资源

- compose 已配置资源上限：api 默认 2 CPU / 2G 内存（可在 `deploy.resources` 调整）
- `API_WORKERS`：并发高可提升到 2-4，但**限流按进程放大**，需同步调整阈值
- 首次启动 RAG 会加载 Embedding 模型，内存占用明显，预留足够内存
- 数据量增长时关注 `vector_db` 体积，及时备份与规划磁盘

---

## 14. 日常运维速查

```bash
# 状态
docker compose -f docker-compose.prod.yml ps

# 启动 / 停止 / 重启
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml down            # 停止（数据卷保留）
docker compose -f docker-compose.prod.yml restart api

# 日志
docker compose -f docker-compose.prod.yml logs -f api

# 进入容器
docker compose -f docker-compose.prod.yml exec api sh

# 生成 API Key
docker compose -f docker-compose.prod.yml exec api openclaw key

# 数据库迁移
docker compose -f docker-compose.prod.yml exec api alembic upgrade head

# 备份
docker compose -f docker-compose.prod.yml exec api python scripts/backup.py \
  --data /app/data --dir /app/backups
```

> ⚠️ `docker compose down -v` 会删除数据卷（数据全没），**生产禁止**。仅清空测试环境时使用。

---

## 15. 安全加固清单

上线后建议逐步完成：

- [ ] 服务器仅开放 80/443（及 SSH），其余端口关闭
- [ ] 防火墙/安全组限制 SSH 来源
- [ ] 定期轮换 API Key 与 JWT Secret
- [ ] 启用系统自动安全更新
- [ ] 把备份同步到异地/对象存储
- [ ] 接入外部 HTTP 探活告警（盯 `/health`）
- [ ] 按需接入日志集中采集（ELK/Loki）与指标监控（Prometheus/Grafana）
- [ ] 关注依赖安全更新，定期 `docker compose build` 刷新镜像
