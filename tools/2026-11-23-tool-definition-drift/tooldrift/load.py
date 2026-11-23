"""输入归一化：三种常见快照形状 → {name: 规范化的 tool dict}。

接受的形状（和 wirecheck 的经验一致，真实世界就这三种）：
- MCP list_tools 风格：{"tools": [{"name", "description", "inputSchema", "annotations"}]}
- 裸数组：[{"name": ..., "parameters": ...}]
- 映射：{"工具名": {json schema}}

description / annotations 缺失时按 None 处理——"没写"和"写了"的信息都要能比。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple


class InputError(ValueError):
    pass


def _extract_tool_list(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("tools", "mcpServers"):
            if isinstance(data.get(key), list):
                return data[key]
    raise InputError("not a recognized snapshot shape: expected a list or {\"tools\": [...]} ; "
                     "a {name: schema} mapping is also accepted")


def normalize_tool(raw: Any) -> Dict[str, Any]:
    """把一种形状的 tool 记录抹平成统一字段。

    返回 {name, description, schema, annotations}。schema 统一取
    inputSchema / parameters / 本身（映射形状）。
    """
    if isinstance(raw, dict) and ("name" not in raw) and \
            all(isinstance(v, dict) for v in raw.values()) and raw:
        # 单个映射形状的条目不该走到这，防御一下
        raise InputError("bare schema mapping must go through load_tools")
    if not isinstance(raw, dict):
        raise InputError("tool entry must be an object")
    name = raw.get("name")
    if not name:
        raise InputError("tool entry without a name")
    schema = raw.get("inputSchema") or raw.get("parameters") or raw.get("schema")
    if schema is None:
        # 映射形状的裸条目：除元数据外整块就是 schema
        schema = {k: v for k, v in raw.items() if k not in
                  ("name", "description", "annotations", "type", "schema")}
    return {
        "name": name,
        "description": raw.get("description"),
        "schema": schema if isinstance(schema, dict) else {},
        "annotations": raw.get("annotations"),
    }


def load_tools(data: Any) -> Dict[str, Dict[str, Any]]:
    """快照 → {name: normalized}。同名条目后写的覆盖先写的（记着即可，不假装没发生）。

    对 load_tools 的输出再跑一遍是幂等的（diff_snapshots 依赖这一点）。
    """
    if isinstance(data, dict) and data:
        values = list(data.values())
        if all(isinstance(v, dict) for v in values):
            if all("name" in v for v in values):
                # 已经是规范化形状（大概率来自 load_tools 自己），直接归一
                return {v["name"]: normalize_tool(v) for v in values}
            out: Dict[str, Dict[str, Any]] = {}
            for name, schema in data.items():
                out[name] = normalize_tool(dict(schema, name=name))
            return out
    tools = _extract_tool_list(data)
    out = {}
    for raw in tools:
        t = normalize_tool(raw)
        out[t["name"]] = t
    return out


def load_file(path: str) -> Dict[str, Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        return load_tools(json.load(fh))
