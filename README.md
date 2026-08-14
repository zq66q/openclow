# OpenClaw

**企业级多 Agent 业务自动化平台** — 从 LLM 基础层到 Web 前端的全栈 AI Agent 框架。

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![LangChain](https://img.shields.io/badge/LangChain-0.3+-green.svg)](https://www.langchain.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-purple.svg)](https://langchain-ai.github.io/langgraph/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-teal.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

---

## 架构

```
┌──────────────────────────────────────────────────────┐
│                    用户入口                           │
│   CLI (openclaw)    REST API    WebSocket    Web UI  │
├──────────────────────────────────────────────────────┤
│  L7  API 层          FastAPI + WebSocket + SSE       │
│  L6  业务层          场景 / 工作流 / 会话 / 提示词     │
│  L5  Agent 层        ReAct / Function Calling / 融合  │
│  L4  记忆层          短期 / 长期 / 提取 / 压缩         │
│  L3  RAG 层          检索 / 切片 / 嵌入 / 向量库       │
│  L2  嵌入层          OpenAI / 本地嵌入                │
│  L1  LLM 客户端      OpenAI / DeepSeek / 兼容 API     │
├──────────────────────────────────────────────────────┤
│  横切关注点:  认证(JWT/APIKey) · 可观测 · 成本 · MCP  │
└──────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 安装

```bash
git clone https://github.com/your-org/openclaw.git
cd openclaw
pip install -e .
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env 填入 API Key:
#   LLM_API_KEY=sk-...
#   LLM_BASE_URL=https://api.openai.com/v1
```

### 3. 启动服务

```bash
# CLI 启动
openclaw serve --reload

# 或直接启动
python -m uvicorn api.server:create_app --factory --reload
```

访问 http://localhost:8000/docs 查看 Swagger API 文档。

### 4. Docker 部署

```bash
docker compose up -d          # API 服务
docker compose --profile full up -d   # API + Web UI
```

## 核心功能

### 多场景对话

一行代码搭建业务应用：

```python
from business.service_facade import ServiceFacade

svc = ServiceFacade.quick(api_key="sk-...")

# 通用助手
app = svc.create_scenario("general_assistant")
print(app.chat("帮我写一封邮件"))

# 知识库客服
app = svc.create_scenario("rag_customer_service", docs_dir="./knowledge")
print(app.chat("退货流程是什么？"))

# 数据分析
app = svc.create_scenario("data_analyst")
print(app.chat("上季度销售额趋势？"))
```

### 多 Agent 工作流

支持串行、并行、条件分支的工作流编排：

```python
# 并行数据分析工作流
result = svc.run_workflow("data_analysis", "分析近期销售趋势")

# 代码审查工作流
result = svc.run_workflow("code_review", code_block)
```

### RAG 知识库

```python
# 文档导入
svc.rag_pipeline.ingest_file("企业制度.pdf")

# 检索增强对话
app = svc.create_scenario("rag_customer_service")
app.chat("年假怎么申请？")
```

### WebSocket 流式对话

```javascript
const ws = new WebSocket("ws://localhost:8000/chat/ws");
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === "chunk") console.write(msg.content);
  if (msg.type === "done") console.log("完成");
};
ws.send(JSON.stringify({ type: "chat", query: "你好" }));
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/chat` | POST | 同步对话 |
| `/chat/stream` | POST | SSE 流式对话 |
| `/chat/ws` | WS | WebSocket 流式对话 |
| `/sessions` | GET/POST | 会话列表/创建 |
| `/sessions/{id}` | GET/DELETE | 会话详情/删除 |
| `/rag/ingest` | POST | 文档入库 |
| `/memory/search` | POST | 记忆搜索 |
| `/agents` | GET | 预置 Agent 列表 |
| `/scenarios` | GET | 预置场景列表 |

## 目录结构

```
openclaw/
├── src/
│   ├── core/           L1-L2: LLM 客户端、嵌入、日志、配置
│   ├── rag/            L3: RAG 检索（切片、向量库、文档解析）
│   ├── memory/         L4: 记忆系统（短期/长期、提取、压缩）
│   ├── agent/          L5: Agent 引擎（ReAct/FC、路由、融合、成本）
│   ├── business/       L6: 业务层（场景、工作流、会话、门面）
│   ├── mcp/            MCP 工具协议（注册表、熔断器、内置工具）
│   ├── auth/           认证中间件（API Key + JWT）
│   ├── api/            FastAPI 后端（REST + SSE + WebSocket）
│   └── ui/             Streamlit 前端
├── tests/
│   ├── unit/           单元测试
│   └── integration/    集成测试
├── scripts/            开发调试脚本
├── Dockerfile          多阶段生产镜像
├── docker-compose.yml  一键部署
├── .env.example        环境变量模板
├── langgraph.json      LangGraph Server 配置
└── pyproject.toml      项目元数据与工具链
```

## 配置项

关键环境变量（完整列表见 `.env.example`）：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_API_KEY` | LLM API Key | (必填) |
| `LLM_BASE_URL` | LLM API 地址 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 默认模型 | `gpt-4o-mini` |
| `OPENCLAW_AUTH_MODE` | 认证模式: apikey/jwt/both/none | `apikey` |
| `OPENCLAW_API_KEYS` | 合法 API Key（逗号分隔） | (空) |
| `OPENCLAW_JWT_SECRET` | JWT 签名密钥 | (空) |
| `MEMORY_DB_PATH` | 记忆数据库路径 | `./data/memory.db` |
| `RAG_VECTOR_STORE_PATH` | RAG 向量库路径 | `./data/vector_db` |

## 测试

```bash
# 全量测试 (111 项)
pytest tests/ -q

# 按模块测试
pytest tests/unit/test_server.py -v
pytest tests/unit/test_auth.py -v
pytest tests/integration/ -v

# 覆盖率
pytest tests/ --cov=src --cov-report=html
```

## 技术栈

- **LLM**: LangChain + OpenAI / DeepSeek / 兼容 API
- **Graph**: LangGraph (ReAct 状态图)
- **RAG**: ChromaDB + sentence-transformers + BM25
- **Memory**: SQLite (aiosqlite) + 自动提取/压缩
- **API**: FastAPI + WebSocket + SSE
- **UI**: Streamlit
- **MCP**: 工具注册表 + 熔断器 + 内置计算器/HTTP/文件工具
- **Auth**: API Key + JWT (PyJWT) 双轨认证
- **Deploy**: Docker 多阶段构建 + docker-compose

## License

MIT
