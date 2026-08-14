# 命令行入口，支持 serve/check/ingest/key/token 命令

"""CLI 入口 — 生产级命令行工具。

命令:
    openclaw serve    启动 API 服务
    openclaw check    运行健康检查
    openclaw ingest   RAG 文档入库
    openclaw key      生成 API Key
    openclaw token    签发 JWT Token

用法:
    # 开发启动
    openclaw serve --reload

    # 生产启动
    openclaw serve --host 0.0.0.0 --port 8000 --workers 4

    # 健康检查
    openclaw check --url http://localhost:8000

    # 生成 API Key
    openclaw key

    # 签发 JWT
    export OPENCLAW_JWT_SECRET=$(openssl rand -hex 32)
    openclaw token --sub user123 --expire 48
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

# 确保 src 在路径中
_PROJECT_ROOT = Path(__file__).resolve().parent
_SRC_DIR = str(_PROJECT_ROOT / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _print(msg: str) -> None:
    """同步输出到 stdout。"""
    print(msg, flush=True)


# ── 子命令: serve ──


def cmd_serve(args: argparse.Namespace) -> int:
    """启动 API 服务。"""
    # 延迟导入，避免 CLI 本身依赖全部 Heavy 库
    from business.service_facade import ServiceConfig, ServiceFacade

    config = ServiceConfig.from_env() if hasattr(ServiceConfig, "from_env") else ServiceConfig()
    if not config.llm_api_key:
        config.llm_api_key = os.getenv("OPENAI_API_KEY", "mock")

    facade = ServiceFacade(config)
    facade.start()

    # 启动前健康检查
    health = facade.health_check()
    status = health.status.value if hasattr(health.status, "value") else str(health.status)
    _print(f"Service health: {status}")
    for comp, state in health.components.items():
        _print(f"  [{comp}] {state}")

    # 导入 server 启动
    from api.server import start

    _print(f"\nStarting OpenClaw API on {args.host}:{args.port}")
    _print("Press Ctrl+C to stop\n")

    try:
        start(
            host=args.host,
            port=args.port,
            reload=args.reload,
            facade=facade,
            workers=args.workers,
            timeout_graceful_shutdown=args.graceful_timeout,
        )
    except KeyboardInterrupt:
        _print("\nShutting down...")
    finally:
        facade.shutdown()
    return 0


# ── 子命令: check ──


def cmd_check(args: argparse.Namespace) -> int:
    """运行健康检查。"""
    import json
    import urllib.request

    url = args.url.rstrip("/") + "/health"
    _print(f"Checking {url} ...")

    try:
        req = urllib.request.Request(url, headers={"X-API-Key": args.api_key} if args.api_key else {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            status = data.get("status", "unknown")
            uptime = data.get("uptime_seconds", 0)
            comps = data.get("components", {})

            _print(f"Status : {status}")
            _print(f"Uptime : {uptime:.1f}s")
            _print("Components:")
            for comp, state in comps.items():
                _print(f"  [{comp}] {state}")

            if status.lower() in ("running", "ok", "healthy"):
                _print("\n✅ Health check passed")
                return 0
            _print(f"\n⚠️  Service status: {status}")
            return 1
    except Exception as exc:
        _print(f"\n❌ Health check failed: {exc}")
        return 2


# ── 子命令: ingest ──


def cmd_ingest(args: argparse.Namespace) -> int:
    """RAG 文档入库。"""
    from business.service_facade import ServiceConfig, ServiceFacade

    config = ServiceConfig.from_env() if hasattr(ServiceConfig, "from_env") else ServiceConfig()
    if not config.llm_api_key:
        config.llm_api_key = os.getenv("OPENAI_API_KEY", "mock")

    facade = ServiceFacade(config)
    facade.start()

    pipeline = facade.rag_pipeline
    if pipeline is None:
        _print("❌ RAG pipeline not available")
        facade.shutdown()
        return 1

    text = args.text
    if args.file:
        p = Path(args.file)
        if not p.exists():
            _print(f"❌ File not found: {args.file}")
            facade.shutdown()
            return 1
        text = p.read_text(encoding="utf-8")

    if not text:
        _print("❌ No text or file provided")
        facade.shutdown()
        return 1

    try:
        result = pipeline.ingest(text, metadata={"source": args.file or "cli"})
        _print(f"✅ Ingested: {result}")
        facade.shutdown()
        return 0
    except Exception as exc:
        _print(f"❌ Ingest failed: {exc}")
        facade.shutdown()
        return 1


# ── 子命令: key ──


def cmd_key(_args: argparse.Namespace) -> int:
    """生成 API Key。"""
    from auth.middleware import generate_api_key

    key = generate_api_key()
    _print(f"Generated API Key:\n{key}")
    _print("\nAdd to .env:")
    _print(f'  OPENCLAW_API_KEYS="{key}"')
    return 0


# ── 子命令: token ──


def cmd_token(args: argparse.Namespace) -> int:
    """签发 JWT Token。"""
    from auth.middleware import JWTAuth

    auth = JWTAuth(expire_hours=args.expire)
    if not auth.enabled:
        _print("❌ JWT not configured. Set OPENCLAW_JWT_SECRET first.")
        _print("  export OPENCLAW_JWT_SECRET=$(openssl rand -hex 32)")
        return 1

    try:
        token = auth.encode({"sub": args.sub})
        _print(f"JWT Token (expires in {args.expire}h):\n{token}")
        return 0
    except Exception as exc:
        _print(f"❌ Token generation failed: {exc}")
        return 1


# ── 参数解析 ──


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openclaw",
        description="OpenClaw 企业级多 Agent 业务自动化平台 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="可用命令")

    # serve
    p_serve = sub.add_parser("serve", help="启动 API 服务")
    p_serve.add_argument("--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=8000, help="监听端口 (默认: 8000)")
    p_serve.add_argument("--reload", action="store_true", help="开发模式热重载")
    p_serve.add_argument("--workers", type=int, default=1, help="工作进程数 (仅生产)")
    p_serve.add_argument(
        "--graceful-timeout",
        type=int,
        default=30,
        help="优雅停机超时秒数，等待在途请求完成 (默认: 30，0 表示不限时)",
    )

    # check
    p_check = sub.add_parser("check", help="运行健康检查")
    p_check.add_argument("--url", default="http://localhost:8000", help="服务地址")
    p_check.add_argument("--api-key", default="", help="API Key")

    # ingest
    p_ingest = sub.add_parser("ingest", help="RAG 文档入库")
    p_ingest.add_argument("--text", default="", help="直接传入文本")
    p_ingest.add_argument("--file", default="", help="文件路径")
    p_ingest.add_argument("--collection", default="default", help="集合名称")

    # key
    sub.add_parser("key", help="生成 API Key")

    # token
    p_token = sub.add_parser("token", help="签发 JWT Token")
    p_token.add_argument("--sub", default="user", help="Subject (用户标识)")
    p_token.add_argument("--expire", type=int, default=24, help="过期小时数")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    commands: dict[str, Callable[[argparse.Namespace], int]] = {
        "serve": cmd_serve,
        "check": cmd_check,
        "ingest": cmd_ingest,
        "key": cmd_key,
        "token": cmd_token,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
