# OpenClaw 数据备份与恢复

本目录说明 OpenClaw 的数据备份策略、操作命令与恢复流程。

## 需要备份的数据

| 路径 | 内容 | 类型 |
|------|------|------|
| `data/memory.db` | 分层记忆系统（短期/长期记忆） | SQLite |
| `data/state.db/` | Agent 状态存储 | SQLite |
| `data/vector_db/` | RAG 向量库（chroma） | SQLite + 目录 |
| `data/rag/` | RAG 文档切片库 | SQLite + 目录 |
| `data/audit_logs/` | 审计日志（jsonl） | 文本 |
| `data/uploads/` | 文件沙箱（若有上传） | 文件 |

> `src/data/audit_logs/` 是代码仓库内的空目录占位（git 跟踪），不包含运行时数据，无需备份。

## 备份命令

### 手动备份（Windows / Linux 均可）

```bash
cd /opt/openclaw
python scripts/backup.py                 # 默认输出到 ./backups
python scripts/backup.py --dir /var/backups/openclaw   # 指定目录
python scripts/backup.py --keep 7        # 只保留最近 7 份
```

每次备份生成 `openclaw-backup-<时间戳>.tar.gz`，自动清理超过保留份数的旧备份。

### Linux 定时备份（cron）

编辑 crontab（`crontab -e`）：

```cron
# 每天凌晨 3:00 备份，日志写到独立文件
0 3 * * * /opt/openclaw/scripts/backup.sh >> /var/log/openclaw-backup.log 2>&1
```

验证 cron 是否正常：手动跑一次后看日志，然后 `crontab -l` 确认任务已安装。

### 远程备份（可选）

本地备份只是第一道防线，建议把备份同步到异地。示例（rsync 到另一台机器）：

```cron
# 每天 3:05 备份后同步到远程主机
5 3 * * * rsync -az /opt/openclaw/backups/ user@backup-host:/srv/backups/openclaw/
```

### 对象存储异地备份（推荐）

`scripts/upload_backup.py` 把本地 `backups/` 下的 `openclaw-backup-*.tar.gz` 上传到对象存储（**AWS S3 / 阿里云 OSS / 腾讯云 COS 通用**，纯标准库实现 AWS SigV4 签名，无需安装任何 SDK），支持上传后下载校验与远端保留份数清理。

**1. 在 `.env` 中配置（脚本默认读取 `./.env`，也可 `--env-file` 指定）：**

```dotenv
# 三家任选其一（region 换成你的实际地域）
OPENCLAW_OBS_ENDPOINT=https://s3.ap-northeast-1.amazonaws.com      # AWS S3
# OPENCLAW_OBS_ENDPOINT=https://s3.oss-cn-hangzhou.aliyuncs.com   # 阿里云 OSS（仅支持 virtual 风格）
# OPENCLAW_OBS_ENDPOINT=https://cos.ap-guangzhou.myqcloud.com     # 腾讯云 COS
OPENCLAW_OBS_REGION=ap-northeast-1       # 与端点地域一致，如 oss-cn-hangzhou / ap-guangzhou
OPENCLAW_OBS_BUCKET=my-openclaw-backups  # 存储桶（需提前创建，建议私有读写）
OPENCLAW_OBS_ACCESS_KEY=xxxxxxxx          # AccessKey（OSS/COS 为 SecretId）
OPENCLAW_OBS_SECRET_KEY=xxxxxxxx          # SecretKey（OSS/COS 为 SecretKey）
OPENCLAW_OBS_PREFIX=openclaw-backups/     # 远端对象前缀（默认 openclaw-backups/）
OPENCLAW_OBS_STYLE=virtual                # 寻址风格 virtual（默认）| path
```

> 安全提示：建议为备份单独创建**只写该 Bucket 的最小权限密钥**，不要把主账号密钥放进 `.env`。

**2. 手动上传：**

```bash
python scripts/upload_backup.py                          # 上传所有备份
python scripts/upload_backup.py --file xxx.tar.gz        # 只上传指定文件
python scripts/upload_backup.py --verify                 # 上传后下载回来校验 sha256
python scripts/upload_backup.py --dry-run                # 预演，只打印将要执行的操作
```

**3. cron 自动化（每天备份 + 上传 + 校验 + 远端保留 30 份）：**

```cron
0 3 * * * /opt/openclaw/scripts/backup.sh >> /var/log/openclaw-backup.log 2>&1
10 3 * * * /opt/openclaw/scripts/upload_backup.py --env-file /opt/openclaw/.env \
    --keep-remote 30 --verify >> /var/log/openclaw-backup.log 2>&1
```

**4. 从对象存储恢复（下载到本地备份目录后走下方恢复流程）：**

```bash
# 先用浏览器/控制台或 aws cli 下载，例如：
aws s3 cp s3://my-openclaw-backups/openclaw-backups/openclaw-backup-20260814-030000.tar.gz \
    /opt/openclaw/backups/
```

## 恢复流程

1. 停止服务：
   ```bash
   docker compose -f docker-compose.prod.yml down
   ```

2. 找到要恢复的备份，解压到临时目录：
   ```bash
   mkdir -p /tmp/restore && tar -xzf backups/openclaw-backup-20260814-030000.tar.gz -C /tmp/restore
   ```

3. 替换数据目录（先备份当前损坏数据）：
   ```bash
   mv data data.corrupt
   mv /tmp/restore/openclaw-backup-20260814-030000 data
   ```

4. 重启服务：
   ```bash
   docker compose -f docker-compose.prod.yml up -d
   ```

5. 验证：访问 `/health` 确认服务正常，抽查记忆/检索是否可读。

> 建议先在一台临时环境演练一次恢复流程，确保备份可用。定期验证：`tar -tzf backups/xxx.tar.gz | head`。

## 备份策略建议

| 项目 | 建议 |
|------|------|
| 频率 | 每天 1 次（数据量大或业务关键可每小时） |
| 保留 | 14 份（约两周，可通过 `--keep` 调整） |
| 异地 | 至少同步一份到其他机器/对象存储（`scripts/upload_backup.py`，远端保留 `--keep-remote`，默认 30 份） |
| 验证 | 每月手动恢复演练一次 |
