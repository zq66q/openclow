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
| 异地 | 至少同步一份到其他机器/对象存储 |
| 验证 | 每月手动恢复演练一次 |
