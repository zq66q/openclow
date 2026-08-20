# OpenClaw 生产部署手册（从零到上线）

> 适用环境：腾讯云轻量应用服务器 4核4G / Ubuntu 22.04 / 域名已备案
> 预计耗时：30~40 分钟（大部分在等构建和证书签发）
> 全程复制粘贴即可，遇到问题见文末「常见问题」

---

## 0. 前置条件检查

| 条件 | 状态 | 说明 |
|---|---|---|
| 服务器 | ☐ | 腾讯云轻量 4核4G Ubuntu 22.04 |
| 域名 | ☐ | 已备案，能改 DNS 解析 |
| API Key | ☐ | DeepSeek ×2、DashScope ×1、Tavily ×1、LangSmith（可选）×1 |
| SSH 能登录 | ☐ | `ssh root@<服务器IP>` 可用 |

---

## 1. 服务器初始化（约 5 分钟）

### 1.1 SSH 登录服务器

```bash
ssh root@<服务器公网IP>
```

> Windows 本地可用 `ssh` 命令（Win10+ 自带）或 PuTTY。

### 1.2 更新系统并安装 Docker

```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker compose version   # 确认输出 v2.x
```

### 1.3 防火墙放行端口

腾讯云轻量在**控制台 → 防火墙**里放行（不是 ufw）：

| 端口 | 用途 |
|---|---|
| 22 | SSH（建议限制来源 IP） |
| 80 | HTTP（证书签发 + 跳转） |
| 443 | HTTPS |

> 不要放行 8000 / 8501 / 3000，它们只在内网/隧道访问。

---

## 2. 拉取代码（约 2 分钟）

```bash
cd /opt
git clone https://github.com/zq66q/openclow.git
cd openclow
```

> 仓库是公开的，无需配置凭证。如果以后转私有，需在服务器配置 deploy key。

---

## 3. 配置 .env（约 10 分钟，最关键的一步）

```bash
cp deploy/production.env.example .env
vim .env     # 或 nano .env
```

必须填写的项（所有 `<占位符>` 都要替换）：

```ini
# ── LLM 密钥 ──
LLM_API_KEY=<DeepSeek key>
LLM_MINI_API_KEY=<DeepSeek key，与上面同一个>
LLM_VISION_API_KEY=<DashScope key>
EMBEDDING_API_KEY=<DashScope key，与 vision 同一个>

# ── 域名 ──
OPENCLAW_DOMAIN=<你的域名，如 example.com>
OPENCLAW_CORS_ORIGINS=https://app.<你的域名>,https://api.<你的域名>

# ── API 鉴权 ──
OPENCLAW_API_KEYS=<至少 2 个 oc_ 开头的 key，本地生成，见 3.1>
OPENCLAW_JWT_SECRET=<openssl 生成，见 3.2>

# ── 日志/版本 ──
OPENCLAW_LOG_FORMAT=json
OPENCLAW_VERSION=latest

# ── Grafana（用监控时才需要）──
GRAFANA_ADMIN_PASSWORD=<强密码>
```

### 3.1 生成 API key（在任意有 Python 的机器上）

```bash
python -c "import os,base64; print('oc_'+base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip('='))"
```

跑两次，得到两个 key，填入 `OPENCLAW_API_KEYS`，逗号分隔。

### 3.2 生成 JWT secret（在服务器上）

```bash
openssl rand -base64 48
```

### 3.3 验证 .env 没有遗漏占位符

```bash
grep -n '<' .env
# 输出为空（或只有注释行）才算通过；有输出说明还有没填的
```

---

## 4. DNS 解析（约 5 分钟 + 生效等待）

到域名服务商（如 DNSPod）添加两条 A 记录：

| 主机记录 | 记录类型 | 记录值 |
|---|---|---|
| api | A | 服务器公网 IP |
| app | A | 服务器公网 IP |

验证生效（本地电脑跑）：

```bash
nslookup api.<你的域名>
nslookup app.<你的域名>
# 都应返回服务器公网 IP
```

---

## 5. 启动服务（约 10 分钟，主要是镜像构建）

```bash
cd /opt/openclow

# 构建 + 启动 API + Caddy（含 Web UI）
docker compose -f docker-compose.prod.yml --profile full up -d --build

# 查看进度
docker compose -f docker-compose.prod.yml logs -f
# 看到 caddy 证书签发成功、api healthy 即可 Ctrl+C 退出日志
```

> 首次构建要装依赖，耐心等。4核4G 大约 5~8 分钟。

---

## 6. 验证上线（约 2 分钟）

```bash
# 6.1 容器状态：三个都应是 Up（api 需为 healthy）
docker compose -f docker-compose.prod.yml ps

# 6.2 API 健康检查（无需鉴权）
curl -s https://api.<你的域名>/health

# 6.3 API 鉴权验证（用你填的 oc_ key）
curl -s https://api.<你的域名>/chat \
  -H "X-API-Key: <你的oc_key>" \
  -H "Content-Type: application/json" \
  -d '{"query":"你好"}'
# 返回 JSON 里有 answer 字段即成功

# 6.4 浏览器访问
# Web UI: https://app.<你的域名>
```

四步都通过 → 部署完成 🎉

---

## 7. 可选：监控栈（Prometheus + Grafana）

```bash
docker compose -f docker-compose.prod.yml --profile monitoring up -d

# Grafana 只绑定了服务器本机 3000 端口，用 SSH 隧道访问：
# 本地电脑执行：
ssh -L 3000:localhost:3000 root@<服务器IP>
# 然后浏览器打开 http://localhost:3000
# 账号 admin / 你在 .env 里填的 GRAFANA_ADMIN_PASSWORD
```

---

## 8. 日常运维

### 更新代码重新部署

```bash
cd /opt/openclow
git pull
docker compose -f docker-compose.prod.yml --profile full up -d --build
```

### 查看日志

```bash
docker compose -f docker-compose.prod.yml logs -f api     # API 日志
docker compose -f docker-compose.prod.yml logs -f caddy   # 反代/证书日志
```

### 重启 / 停止

```bash
docker compose -f docker-compose.prod.yml --profile full restart
docker compose -f docker-compose.prod.yml --profile full down
```

### 数据备份

数据在 docker 卷 `openclaw_data`（向量库、记忆库、上传文件）。备份：

```bash
docker run --rm -v openclaw_data:/data -v /opt/backups:/backup alpine \
  tar czf /backup/openclaw-data-$(date +%Y%m%d).tar.gz -C /data .
```

建议加 crontab 每日备份：

```bash
crontab -e
# 添加一行（每天凌晨 3 点备份，保留最近 7 天）：
0 3 * * * docker run --rm -v openclaw_data:/data -v /opt/backups:/backup alpine tar czf /backup/openclaw-data-$(date +\%Y\%m\%d).tar.gz -C /data . && find /opt/backups -mtime +7 -delete
```

---

## 9. 常见问题

### Q1: Caddy 证书签发失败
- 检查 DNS 解析是否生效（`nslookup api.<域名>`）
- 检查防火墙 80/443 是否放行
- 域名必须已备案，否则国内服务器 80/443 会被拦截
- 看日志：`docker compose -f docker-compose.prod.yml logs caddy`

### Q2: API 容器起不来 / 反复重启
```bash
docker compose -f docker-compose.prod.yml logs api | tail -50
```
大概率是 .env 有问题（key 填错、占位符没替换）。

### Q3: 构建失败提示网络超时
国内服务器拉 PyPI 慢，Dockerfile 里已配置镜像源；若仍超时，重试一次：
```bash
docker compose -f docker-compose.prod.yml --profile full up -d --build
```

### Q4: 内存不够（2G 小服务器）
api 容器限制了 2G 内存。4核4G 够用；2核2G 需把 `docker-compose.prod.yml` 里 `memory: "2G"` 改成 `1G`，并加 swap：
```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

### Q5: 想不配域名先测试
在 Caddyfile 每个 site 块里加一行 `tls internal`（用自签证书），然后本地加 hosts 记录访问。仅测试用，正式上线必须去掉。

---

## 附录：本地已有但服务器不需要的东西

| 本地文件 | 是否上传服务器 |
|---|---|
| `.env`（含真实 key） | ❌ 服务器上用 production.env.example 重新生成 |
| `.venv/` | ❌ 容器内自带环境 |
| `backups/*.tar.gz` | ❌ 数据在 docker 卷里，服务器单独备份 |
| `data/` | 看需求：要迁移本地知识库就把 `data/vector_db` 拷过去 |
| `2026-*/` 会话目录 | ❌ 已被 .gitignore 排除 |
