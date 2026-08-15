#!/usr/bin/env python3
"""OpenClaw 备份上传到对象存储（AWS S3 / 阿里云 OSS / 腾讯云 COS 通用）。

纯标准库实现 AWS Signature V4 签名，无需安装 boto3 / oss2 / cos SDK，
配合 scripts/backup.py 实现"本地备份 + 异地容灾"闭环。

用法:
    python scripts/upload_backup.py                              # 上传 ./backups 下所有 tar.gz
    python scripts/upload_backup.py --file xxx.tar.gz            # 只上传指定文件
    python scripts/upload_backup.py --keep-remote 30            # 远端仅保留最近 30 份
    python scripts/upload_backup.py --verify                    # 上传后下载回来校验 sha256
    python scripts/upload_backup.py --dry-run                   # 只打印将要执行的操作

环境变量（可用 --env-file 从 .env 加载，已存在的环境变量优先）:
    OPENCLAW_OBS_ENDPOINT      对象存储服务端点（不含 bucket）
    OPENCLAW_OBS_REGION        地域，如 ap-northeast-1 / oss-cn-hangzhou / ap-guangzhou
    OPENCLAW_OBS_BUCKET        Bucket 名称
    OPENCLAW_OBS_ACCESS_KEY    访问密钥 ID（OSS/COS 为 SecretId）
    OPENCLAW_OBS_SECRET_KEY    访问密钥 Secret（OSS/COS 为 SecretKey）
    OPENCLAW_OBS_PREFIX        远端对象前缀（默认 openclaw-backups/）
    OPENCLAW_OBS_STYLE         寻址风格: virtual（默认）| path

三家端点示例（region 换成你自己的）:
    AWS S3      https://s3.ap-northeast-1.amazonaws.com      （virtual / path 均可）
    阿里云 OSS  https://s3.oss-cn-hangzhou.aliyuncs.com      （仅支持 virtual）
    腾讯云 COS  https://cos.ap-guangzhou.myqcloud.com        （virtual / path 均可）

cron 示例（每天 3:00 备份 + 3:10 上传异地）:
    0 3 * * * /opt/openclaw/scripts/backup.sh >> /var/log/openclaw-backup.log 2>&1
    10 3 * * * /opt/openclaw/scripts/upload_backup.py --env-file /opt/openclaw/.env \
        --keep-remote 30 --verify >> /var/log/openclaw-backup.log 2>&1
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import os
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB
DEFAULT_PREFIX = "openclaw-backups/"
BACKUP_FILE_PATTERN = ("openclaw-backup-", ".tar.gz")


class ObsError(RuntimeError):
    """对象存储请求失败（带 HTTP 状态与响应体摘要）。"""


def _load_env_file(path: str) -> None:
    """从 .env 文件加载 KEY=VALUE（不覆盖已存在的环境变量）。"""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> tuple[str, str, int]:
    """流式计算文件 sha256 / md5（hex + base64），返回 (sha256_hex, md5_b64, size)。"""
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    md5_b64 = base64.b64encode(md5.digest()).decode("ascii")
    return sha.hexdigest(), md5_b64, size


def _urlencode_path(value: str) -> str:
    """S3 风格的路径编码：保留 '/'，其余严格编码。"""
    return urllib.parse.quote(value, safe="/")


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def _build_auth_headers(
    method: str,
    host: str,
    uri_path: str,
    query: dict[str, str],
    headers: dict[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    amz_date: str | None = None,
) -> dict[str, str]:
    """构造 AWS SigV4 认证头（https://docs.aws.amazon.com/general/latest/gr/sigv4-signed-request.html）。

    amz_date 仅供测试注入（格式 %Y%m%dT%H%M%SZ），生产默认取当前 UTC 时间。
    """
    if amz_date:
        date_stamp = amz_date[:8]
    else:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

    all_headers: dict[str, str] = {"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    all_headers.update({k.lower(): v for k, v in headers.items()})

    # canonical query: 按 key 字典序，值做 RFC3986 编码
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in sorted(query.items())
    )

    signed_headers = ";".join(sorted(all_headers))
    canonical_headers = "".join(f"{k}:{all_headers[k].strip()}\n" for k in sorted(all_headers))

    canonical_request = "\n".join([method, uri_path, canonical_query, canonical_headers, signed_headers, payload_hash])

    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _sha256_hex(canonical_request.encode("utf-8"))])

    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, "s3"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {**all_headers, "authorization": authorization}


class ObjectStorage:
    """极简 S3 兼容客户端（仅实现备份所需：PUT / GET / DELETE / List）。"""

    def __init__(
        self, endpoint: str, region: str, bucket: str, access_key: str, secret_key: str, style: str = "virtual"
    ):
        parsed = urllib.parse.urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
        if parsed.scheme != "https":
            raise ObsError(f"端点必须使用 https（当前: {endpoint}）")
        self._secure = True
        self._endpoint_host = parsed.netloc
        self._region = region
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._style = style
        if self._style not in ("virtual", "path"):
            raise ObsError(f"OPENCLAW_OBS_STYLE 仅支持 virtual|path（当前: {style}）")

    # ── 内部工具 ──

    def _url(self, key: str, query: dict[str, str] | None = None) -> tuple[str, str, str]:
        """返回 (host, path, query_string)。virtual 风格 bucket 进 host。"""
        if self._style == "virtual":
            host = f"{self._bucket}.{self._endpoint_host}"
            path = "/" + _urlencode_path(key.lstrip("/"))
        else:
            host = self._endpoint_host
            path = "/" + _urlencode_path(f"{self._bucket}/{key.lstrip('/')}")
        qs = urllib.parse.urlencode(query) if query else ""
        return host, path, qs

    def _request(
        self,
        method: str,
        key: str,
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        payload_hash: str = _sha256_hex(b""),
    ) -> tuple[int, dict[str, str], bytes]:
        host, path, qs = self._url(key, query)
        full_path = f"{path}?{qs}" if qs else path
        auth_headers = _build_auth_headers(
            method,
            host,
            path,
            query or {},
            headers or {},
            payload_hash,
            self._access_key,
            self._secret_key,
            self._region,
        )
        final_headers = dict(auth_headers)
        if body is not None:
            final_headers["content-length"] = str(len(body))

        conn = http.client.HTTPSConnection(host, timeout=120)
        try:
            conn.request(method, full_path, body=body, headers=final_headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    # ── 公共操作 ──

    def upload(self, key: str, local_path: Path) -> dict[str, str]:
        """流式上传单个文件，返回 (sha256, md5_b64, size)。"""
        sha256_hex, md5_b64, size = _hash_file(local_path)
        headers = {
            "content-md5": md5_b64,
            "x-amz-meta-sha256": sha256_hex,
            "content-type": "application/gzip",
        }
        host, path, _ = self._url(key)
        auth_headers = _build_auth_headers(
            "PUT",
            host,
            path,
            {},
            headers,
            sha256_hex,
            self._access_key,
            self._secret_key,
            self._region,
        )
        final_headers = {**auth_headers, **headers, "content-length": str(size)}

        conn = http.client.HTTPSConnection(host, timeout=600)
        try:
            with local_path.open("rb") as fh:
                conn.request("PUT", path, body=fh, headers=final_headers)
                resp = conn.getresponse()
                data = resp.read()
            status = resp.status
            resp_headers = dict(resp.getheaders())
        finally:
            conn.close()

        if status not in (200, 201):
            raise ObsError(f"上传失败 HTTP {status}: {data[:500].decode('utf-8', errors='replace')}")

        etag = resp_headers.get("etag", "").strip('"')
        if etag and etag != base64.b64decode(md5_b64).hex():
            raise ObsError(f"ETag 校验失败: 服务端 {etag} != 本地 {base64.b64decode(md5_b64).hex()}")
        return {"sha256": sha256_hex, "md5": md5_b64, "size": size}

    def download(self, key: str, dest: Path) -> str:
        """下载对象到文件，返回响应中的 x-amz-meta-sha256（可能为空）。"""
        status, headers, data = self._request("GET", key)
        if status != 200:
            raise ObsError(f"下载失败 HTTP {status}: {data[:500].decode('utf-8', errors='replace')}")
        dest.write_bytes(data)
        return headers.get("x-amz-meta-sha256", "")

    def list_backups(self, prefix: str) -> list[dict[str, str]]:
        """列出 prefix 下所有 backup 文件，返回 [{key, last_modified, size}]（按时间升序）。"""
        result: list[dict[str, str]] = []
        marker = ""
        while True:
            query = {"prefix": prefix, "max-keys": "1000"}
            if marker:
                query["marker"] = marker
            status, _, data = self._request("GET", "", query=query)
            if status != 200:
                raise ObsError(f"列对象失败 HTTP {status}: {data[:500].decode('utf-8', errors='replace')}")
            root = ET.fromstring(data)
            for contents in root.iter():
                if not contents.tag.endswith("Contents"):
                    continue
                key = last = size = ""
                for child in contents:
                    if child.tag.endswith("Key"):
                        key = child.text or ""
                    elif child.tag.endswith("LastModified"):
                        last = child.text or ""
                    elif child.tag.endswith("Size"):
                        size = child.text or ""
                # 双重防御：先按 prefix 过滤（部分 S3 兼容实现过滤不严格），
                # 再只匹配备份文件名（key 带 prefix 时先取文件名段），防止误删其他对象
                if not key.startswith(prefix):
                    continue
                filename = key.rsplit("/", 1)[-1]
                if filename.startswith(BACKUP_FILE_PATTERN[0]) and filename.endswith(BACKUP_FILE_PATTERN[1]):
                    result.append({"key": key, "last_modified": last, "size": size})
            is_truncated = any(n.tag.endswith("IsTruncated") and n.text == "true" for n in root.iter())
            next_marker = None
            for n in root.iter():
                if n.tag.endswith("NextMarker"):
                    next_marker = n.text
                    break
            if not is_truncated:
                break
            marker = next_marker or (result[-1]["key"] if result else "")
        result.sort(key=lambda o: o["last_modified"])
        return result

    def delete(self, key: str) -> None:
        status, _, data = self._request("DELETE", key)
        if status not in (200, 204):
            raise ObsError(f"删除失败 HTTP {status}: {data[:500].decode('utf-8', errors='replace')}")


def _resolve_key(prefix: str, filename: str) -> str:
    return f"{prefix.rstrip('/')}/{filename}"


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenClaw 备份上传到对象存储（S3/OSS/COS 通用）")
    parser.add_argument(
        "--dir", default=os.environ.get("OPENCLAW_BACKUP_DIR", "./backups"), help="本地备份目录（默认 ./backups）"
    )
    parser.add_argument("--file", help="只上传指定文件（相对 --dir 的文件名）")
    parser.add_argument(
        "--prefix",
        default=os.environ.get("OPENCLAW_OBS_PREFIX", DEFAULT_PREFIX),
        help=f"远端对象前缀（默认 {DEFAULT_PREFIX}）",
    )
    parser.add_argument("--keep-remote", type=int, default=30, help="远端仅保留最近 N 份备份（默认 30，0 表示不清理）")
    parser.add_argument("--verify", action="store_true", help="上传后下载回来校验 sha256")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的操作")
    parser.add_argument("--env-file", default="./.env", help="从该文件加载 OBS 配置（默认 ./.env）")
    args = parser.parse_args()

    _load_env_file(args.env_file)

    required = {
        "OPENCLAW_OBS_ENDPOINT": os.environ.get("OPENCLAW_OBS_ENDPOINT"),
        "OPENCLAW_OBS_REGION": os.environ.get("OPENCLAW_OBS_REGION"),
        "OPENCLAW_OBS_BUCKET": os.environ.get("OPENCLAW_OBS_BUCKET"),
        "OPENCLAW_OBS_ACCESS_KEY": os.environ.get("OPENCLAW_OBS_ACCESS_KEY"),
        "OPENCLAW_OBS_SECRET_KEY": os.environ.get("OPENCLAW_OBS_SECRET_KEY"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"[ERROR] 缺少环境变量: {', '.join(missing)}", file=sys.stderr)
        print("       可在 .env 中配置（--env-file），或直接 export。", file=sys.stderr)
        return 2

    backup_dir = Path(args.dir).resolve()
    if not backup_dir.is_dir():
        print(f"[ERROR] 备份目录不存在: {backup_dir}", file=sys.stderr)
        return 2

    files = [backup_dir / args.file] if args.file else sorted(backup_dir.glob("openclaw-backup-*.tar.gz"))
    files = [f for f in files if f.is_file()]
    if not files:
        print(f"[INFO] {backup_dir} 下没有可上传的 openclaw-backup-*.tar.gz")
        return 0

    obs = ObjectStorage(
        endpoint=required["OPENCLAW_OBS_ENDPOINT"],
        region=required["OPENCLAW_OBS_REGION"],
        bucket=required["OPENCLAW_OBS_BUCKET"],
        access_key=required["OPENCLAW_OBS_ACCESS_KEY"],
        secret_key=required["OPENCLAW_OBS_SECRET_KEY"],
        style=os.environ.get("OPENCLAW_OBS_STYLE", "virtual"),
    )

    print(
        f"[INFO] 目标: {required['OPENCLAW_OBS_ENDPOINT']} / {required['OPENCLAW_OBS_BUCKET']} "
        f"(prefix={args.prefix}, style={obs._style})"
    )

    if args.dry_run:
        for f in files:
            print(f"[DRY-RUN] 将上传 {_resolve_key(args.prefix, f.name)} ({f.stat().st_size / 1024:.0f} KiB)")
        if args.keep_remote:
            print(f"[DRY-RUN] 将清理 prefix 下最旧的备份，仅保留 {args.keep_remote} 份")
        return 0

    ok = 0
    for f in files:
        key = _resolve_key(args.prefix, f.name)
        try:
            meta = obs.upload(key, f)
            print(f"[OK] 已上传 {key} ({meta['size'] / 1024:.0f} KiB, sha256={meta['sha256'][:12]}…)")
            if args.verify:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    obs.download(key, tmp_path)
                    dl_sha, _, dl_size = _hash_file(tmp_path)
                    if dl_sha != meta["sha256"] or dl_size != meta["size"]:
                        raise ObsError(f"校验失败: 本地 {meta['sha256']} != 远端 {dl_sha}")
                    print(f"    [OK] 下载校验通过（sha256 一致, {dl_size / 1024:.0f} KiB）")
                finally:
                    tmp_path.unlink(missing_ok=True)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其余文件
            print(f"[ERROR] 上传失败 {key}: {exc}", file=sys.stderr)

    if args.keep_remote and ok:
        try:
            remote = obs.list_backups(args.prefix)
            print(f"[INFO] 远端现有 {len(remote)} 份备份")
            for old in remote[: max(0, len(remote) - args.keep_remote)]:
                obs.delete(old["key"])
                print(f"[INFO] 已删除远端过期备份 {old['key']} (mtime {old['last_modified']})")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 远端清理失败（不影响上传）: {exc}", file=sys.stderr)

    if ok != len(files):
        print(f"[ERROR] {len(files) - ok}/{len(files)} 个文件上传失败", file=sys.stderr)
        return 1
    print(f"[DONE] 全部 {ok} 个备份已上传")
    return 0


if __name__ == "__main__":
    sys.exit(main())
