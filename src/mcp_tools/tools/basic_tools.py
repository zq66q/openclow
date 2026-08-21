"""基础工具 — 时间、安全计算、单位换算。

提供 current_time / safe_eval / unit_convert 三个工具。
这些工具在多个 Agent 的 tools 清单和提示词中被引用，
必须在此注册，否则运行时会报"工具未注册"。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Any

from mcp_tools.base import Tool, ToolDangerLevel, ToolMeta
from mcp_tools.registry import register_tool

# 安全计算白名单
_SAFE_BUILTINS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "pow": pow,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "pi": math.pi,
    "e": math.e,
}

# 单位换算表: (类别, from_unit) -> 到基准单位的乘数
# 基准单位: 长度=m, 重量=kg, 温度特殊处理
_UNIT_TABLE: dict[str, dict[str, float]] = {
    "length": {
        "mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0,
        "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344,
        "inch": 0.0254, "foot": 0.3048, "mile": 1609.344,
    },
    "weight": {
        "mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0,
        "oz": 0.0283495, "lb": 0.453592, "pound": 0.453592,
        "斤": 0.5, "两": 0.05,
    },
    "data": {
        "B": 1.0, "KB": 1024.0, "MB": 1024.0**2, "GB": 1024.0**3,
        "TB": 1024.0**4,
    },
    "time": {
        "s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0, "hour": 3600.0,
        "day": 86400.0, "week": 604800.0,
    },
}

# 货币换算（近似静态汇率，仅作粗略参考；精确汇率应使用 web_search 查询）
_CURRENCY_TABLE: dict[str, float] = {
    # 以 CNY 为基准（1 CNY = X 单位外币）
    "CNY": 1.0, "USD": 0.14, "EUR": 0.13, "JPY": 20.0,
    "GBP": 0.11, "HKD": 1.09, "KRW": 190.0, "RUB": 11.0,
    "AUD": 0.21, "CAD": 0.19, "SGD": 0.18,
}


@register_tool
class CurrentTimeTool(Tool):
    """获取当前时间工具。"""

    name = "current_time"
    description = (
        "获取当前的日期、时间和星期信息。\n"
        "返回格式: 2026-08-21 00:30:15 (星期五) UTC+8\n"
        "适用于任何需要知道'现在'的时间相关问题。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "timezone_offset": {
                "type": "number",
                "description": "时区偏移小时数，默认 8（北京时间 UTC+8）。",
                "default": 8,
            },
        },
        "required": [],
    }
    meta = ToolMeta(
        timeout=5,
        danger_level=ToolDangerLevel.SAFE,
        max_retries=0,
        tags=["time", "datetime"],
    )

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        tz_offset = kwargs.get("timezone_offset", 8)
        tz = timezone(timedelta(hours=tz_offset))
        now = datetime.now(tz)
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday = weekdays[now.weekday()]
        return {
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "weekday": weekday,
            "timezone": f"UTC{'+' if tz_offset >= 0 else ''}{tz_offset}",
            "timestamp": int(now.timestamp()),
            "display": f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({weekday}) UTC{'+' if tz_offset >= 0 else ''}{tz_offset}",
        }


@register_tool
class SafeEvalTool(Tool):
    """安全数学计算工具。"""

    name = "safe_eval"
    description = (
        "安全地计算数学表达式（支持 + - * / % ** 括号、sqrt/log/exp/sin/cos/tan/pi/e）。\n"
        "适用于任何数值计算场景，如费用总计、增长率、比例换算等。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，例如 '553*2 + 800 + 600' 或 'sqrt(144)'。",
            },
        },
        "required": ["expression"],
    }
    meta = ToolMeta(
        timeout=5,
        danger_level=ToolDangerLevel.SAFE,
        max_retries=0,
        tags=["math", "calculator", "eval"],
    )

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        expression = str(kwargs["expression"]).strip()

        # 防御性检查：只允许白名单字符
        import re

        if not re.fullmatch(r"[0-9a-zA-Z_+\-*/%().,\s]+", expression):
            raise RuntimeError(f"表达式包含不允许的字符: {expression!r}")

        try:
            code = compile(expression, "<safe_eval>", "eval")
            # 校验所有名字都在白名单内
            for name in code.co_names:
                if name not in _SAFE_BUILTINS:
                    raise RuntimeError(f"不允许的函数或变量: {name}")
            result = eval(code, {"__builtins__": {}}, dict(_SAFE_BUILTINS))  # noqa: S307
        except RuntimeError:
            raise
        except ZeroDivisionError:
            raise RuntimeError("除零错误: 表达式中存在除以 0 的运算")
        except Exception as exc:
            raise RuntimeError(f"表达式解析失败: {exc}")

        # 浮点数友好展示
        if isinstance(result, float):
            if abs(result - round(result)) < 1e-9:
                result_display = int(round(result))
            else:
                result_display = round(result, 6)
        else:
            result_display = result

        return {
            "expression": expression,
            "result": result_display,
            "type": type(result).__name__,
        }


@register_tool
class UnitConvertTool(Tool):
    """单位换算工具。"""

    name = "unit_convert"
    description = (
        "单位换算工具。支持:\n"
        "- 长度: mm/cm/m/km/in/ft/yd/mi\n"
        "- 重量: mg/g/kg/t/oz/lb/斤/两\n"
        "- 数据量: B/KB/MB/GB/TB\n"
        "- 时间: s/min/h/day/week\n"
        "- 温度: ℃(celsius)/℉(fahrenheit)/K(kelvin)\n"
        "- 货币: CNY/USD/EUR/JPY/GBP/HKD/KRW 等 (静态近似汇率，精确汇率请用 web_search)"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "description": "要换算的数值。",
            },
            "from_unit": {
                "type": "string",
                "description": "原始单位，如 'CNY'、'kg'、'℃'。",
            },
            "to_unit": {
                "type": "string",
                "description": "目标单位，如 'USD'、'lb'、'℉'。",
            },
        },
        "required": ["value", "from_unit", "to_unit"],
    }
    meta = ToolMeta(
        timeout=5,
        danger_level=ToolDangerLevel.SAFE,
        max_retries=0,
        tags=["convert", "unit", "currency"],
    )

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        value = float(kwargs["value"])
        from_unit = str(kwargs["from_unit"]).strip()
        to_unit = str(kwargs["to_unit"]).strip()

        # 规范化常见别名
        alias = {
            "celsius": "℃", "c": "℃", "C": "℃", "°C": "℃",
            "fahrenheit": "℉", "f": "℉", "F": "℉", "°F": "℉",
            "kelvin": "K",
            "rmb": "CNY", "￥": "CNY", "元": "CNY",
            "$": "USD", "美元": "USD",
            "日元": "JPY", "港币": "HKD",
        }
        from_unit = alias.get(from_unit, from_unit)
        to_unit = alias.get(to_unit, to_unit)

        # 温度换算（特殊：非线性）
        temp_units = {"℃", "℉", "K"}
        if from_unit in temp_units or to_unit in temp_units:
            if from_unit not in temp_units or to_unit not in temp_units:
                raise RuntimeError(f"温度单位只能和温度单位互转，收到: {from_unit} -> {to_unit}")
            # 先转摄氏度
            if from_unit == "℃":
                c = value
            elif from_unit == "℉":
                c = (value - 32) * 5 / 9
            else:  # K
                c = value - 273.15
            # 再转目标
            if to_unit == "℃":
                result = c
            elif to_unit == "℉":
                result = c * 9 / 5 + 32
            else:  # K
                result = c + 273.15
            return {
                "value": value, "from_unit": from_unit, "to_unit": to_unit,
                "result": round(result, 4),
                "formula": f"{value} {from_unit} = {round(result, 4)} {to_unit}",
            }

        # 货币换算
        if from_unit in _CURRENCY_TABLE and to_unit in _CURRENCY_TABLE:
            cny_value = value / _CURRENCY_TABLE[from_unit]
            result = cny_value * _CURRENCY_TABLE[to_unit]
            return {
                "value": value, "from_unit": from_unit, "to_unit": to_unit,
                "result": round(result, 2),
                "formula": f"{value} {from_unit} ≈ {round(result, 2)} {to_unit} (静态近似汇率，仅供参考)",
            }

        # 通用线性单位换算：在同一类别内查找
        for category, table in _UNIT_TABLE.items():
            if from_unit in table and to_unit in table:
                base_value = value * table[from_unit]
                result = base_value / table[to_unit]
                return {
                    "value": value, "from_unit": from_unit, "to_unit": to_unit,
                    "result": round(result, 6),
                    "formula": f"{value} {from_unit} = {round(result, 6)} {to_unit}",
                }

        raise RuntimeError(f"不支持的单位换算: {from_unit} -> {to_unit}（同类单位才可互转）")
