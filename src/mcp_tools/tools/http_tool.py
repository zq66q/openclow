"""HTTP 请求工具 — 通用 HTTP 客户端（内部共用，不做 Tool 注册）。

基于标准库 urllib.request，无需外部依赖。
支持 GET / POST / PUT / DELETE / PATCH，自动解析 JSON 响应。
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any


_ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """发送 HTTP 请求并返回结构化结果。

    Returns:
        {
            "status_code": int,
            "headers": dict,
            "body": dict | str,
            "content_length": int,
            "error": str | None,
        }
    """
    method = method.upper()
    if method not in _ALLOWED_METHODS:
        raise ValueError(f"不支持的 HTTP 方法: {method}，允许: {_ALLOWED_METHODS}")

    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)

    if "User-Agent" not in (headers or {}):
        req.add_header("User-Agent", "OpenClaw/0.1")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
            resp_headers = dict(resp.headers)

        try:
            parsed_body = json.loads(raw)
        except json.JSONDecodeError:
            parsed_body = raw

        return {
            "status_code": status,
            "headers": resp_headers,
            "body": parsed_body,
            "content_length": len(raw),
            "error": None,
        }

    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed_body = json.loads(raw)
        except json.JSONDecodeError:
            parsed_body = raw

        return {
            "status_code": e.code,
            "headers": dict(e.headers),
            "body": parsed_body,
            "content_length": len(raw),
            "error": e.reason,
        }

    except urllib.error.URLError as e:
        raise ConnectionError(f"HTTP 请求失败: {e.reason}") from e


def http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> dict[str, Any]:
    """GET 请求快捷函数。"""
    return http_request(url, method="GET", headers=headers, timeout=timeout)


def http_post(
    url: str,
    body: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST 请求快捷函数。"""
    return http_request(url, method="POST", headers=headers, body=body, timeout=timeout)
