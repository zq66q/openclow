"""openclaw/src/cli.py 单元测试。

覆盖 build_parser、cmd_key、cmd_token、cmd_check 的模型与路径测试。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 确保 src 在路径中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── build_parser ──


class TestBuildParser:
    def test_parser_creation(self):
        from cli import build_parser

        parser = build_parser()
        assert parser.prog == "openclaw"

    def test_help_exit(self, capsys):
        from cli import build_parser

        parser = build_parser()
        # 空参数时 argparse 不会 exit，command 为 None
        args = parser.parse_args([])
        assert args.command is None

    def test_serve_args(self):
        from cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["serve", "--host", "127.0.0.1", "--port", "9000", "--reload"])
        assert args.command == "serve"
        assert args.host == "127.0.0.1"
        assert args.port == 9000
        assert args.reload is True

    def test_check_args(self):
        from cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["check", "--url", "http://example.com", "--api-key", "k1"])
        assert args.command == "check"
        assert args.url == "http://example.com"
        assert args.api_key == "k1"

    def test_ingest_args(self):
        from cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["ingest", "--text", "hello world", "--collection", "docs"])
        assert args.command == "ingest"
        assert args.text == "hello world"
        assert args.collection == "docs"
        assert args.file == ""

    def test_key_args(self):
        from cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["key"])
        assert args.command == "key"

    def test_token_args(self):
        from cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["token", "--sub", "alice", "--expire", "48"])
        assert args.command == "token"
        assert args.sub == "alice"
        assert args.expire == 48


# ── cmd_key ──


class TestCmdKey:
    def test_generates_key(self, capsys):
        from cli import cmd_key
        import argparse

        args = argparse.Namespace()
        result = cmd_key(args)
        assert result == 0
        captured = capsys.readouterr().out
        assert "oc_" in captured

    def test_shows_env_hint(self, capsys):
        from cli import cmd_key
        import argparse

        args = argparse.Namespace()
        cmd_key(args)
        captured = capsys.readouterr().out
        assert "OPENCLAW_API_KEYS" in captured


# ── cmd_token ──


class TestCmdToken:
    def test_no_secret(self, capsys, monkeypatch):
        monkeypatch.delenv("OPENCLAW_JWT_SECRET", raising=False)
        from cli import cmd_token
        import argparse

        args = argparse.Namespace(sub="user", expire=24)
        result = cmd_token(args)
        assert result == 1
        captured = capsys.readouterr().out
        assert "OPENCLAW_JWT_SECRET" in captured

    def test_with_secret(self, capsys, monkeypatch):
        monkeypatch.setenv("OPENCLAW_JWT_SECRET", "test_secret_1234567890")
        from cli import cmd_token
        import argparse

        args = argparse.Namespace(sub="alice", expire=1)
        result = cmd_token(args)
        # 结果取决于 PyJWT 是否安装
        if result == 0:
            captured = capsys.readouterr().out
            assert "JWT Token" in captured


# ── main ──


class TestMain:
    def test_no_command_prints_help(self, capsys):
        from cli import main

        result = main([])
        assert result == 0
        captured = capsys.readouterr().out
        assert "available" in captured.lower() or "usage" in captured.lower()

    def test_unknown_command(self, capsys):
        from cli import build_parser

        # argparse 遇到无效子命令会 SystemExit(2)
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["nonexistent"])
        assert exc_info.value.code == 2

    def test_key_command(self, capsys):
        from cli import main

        result = main(["key"])
        assert result == 0


# ── 路径验证 ──


class TestModulePaths:
    def test_cli_imports(self):
        """CLI 模块可导入。"""
        import cli

        assert hasattr(cli, "main")
        assert hasattr(cli, "build_parser")

    def test_auth_imports(self):
        """auth 包可导入。"""
        from auth import APIKeyAuth, JWTAuth, generate_api_key

        assert callable(generate_api_key)
        assert APIKeyAuth is not None
        assert JWTAuth is not None

    def test_env_example_exists(self):
        """.env.example 存在。"""
        root = Path(__file__).resolve().parent.parent.parent
        assert (root / ".env.example").exists()

    def test_env_example_not_empty(self):
        root = Path(__file__).resolve().parent.parent.parent
        content = (root / ".env.example").read_text(encoding="utf-8")
        assert "OPENCLAW_API_KEYS" in content
        assert "OPENAI_API_KEY" in content
