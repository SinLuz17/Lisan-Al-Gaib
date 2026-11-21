"""readOnlyHint 与工具形状的矛盾检查。

只读声明 + 执行类信号词 = 值得再看一眼，但不算硬证据（有的工具真的只读）。
同一个 server 内"别的工具标了 destructiveHint 而我标 readOnlyHint"是更硬的矛盾。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

_NAME_SIGNALS = (
    "eval", "exec", "execute", "runtime", "run",
    "write", "create", "delete", "del", "drop", "remove", "insert",
    "update", "patch", "send", "deploy", "install", "submit",
)
_CODE_PARAM_WORDS = ("code", "script", "source", "expression", "expr", "snippet", "command", "cmd")


def _tool_annotations(tool: Dict[str, Any]) -> Dict[str, Any]:
    ann = tool.get("annotations")
    return ann if isinstance(ann, dict) else {}


def _param_names(tool: Dict[str, Any]) -> List[str]:
    schema = tool.get("inputSchema") or tool.get("parameters") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    return list(props.keys()) if isinstance(props, dict) else []


def _name_hits(name: str) -> List[str]:
    low = name.lower()
    return [w for w in _NAME_SIGNALS if w in low]


def _code_params(tool: Dict[str, Any]) -> List[str]:
    hits = []
    for p in _param_names(tool):
        low = p.lower()
        if any(w in low for w in _CODE_PARAM_WORDS):
            hits.append(p)
    return hits


def check(tools: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """返回 (tool_name, reason) 列表；没有工具清单时返回空。"""
    out: List[Tuple[str, str]] = []
    destructive_names = {
        t.get("name") for t in tools if _tool_annotations(t).get("destructiveHint")
    }
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "?")
        ann = _tool_annotations(t)
        if not ann.get("readOnlyHint"):
            continue
        reasons = []
        words = _name_hits(name)
        if words:
            reasons.append("name carries action word(s): " + ", ".join(words))
        cp = _code_params(t)
        if cp:
            reasons.append("code-shaped parameter(s): " + ", ".join(cp))
        if (words or cp) and destructive_names:
            # server 内自相矛盾：一边标只读一边标破坏性——只在已有信号时追加，避免噪音
            reasons.append("same server also marks destructiveHint: "
                           + ", ".join(sorted(n for n in destructive_names if n)))
        if reasons:
            out.append((name, "; ".join(reasons)))
    return out
