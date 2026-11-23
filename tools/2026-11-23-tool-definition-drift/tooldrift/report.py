"""报告：人看的文本版和给 CI 的 JSON 版。"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .diff import Change

_ICON = {"breaking": "BREAKING", "behavior": "behavior", "cosmetic": "cosmetic"}


def render(changes: List[Change], added: List[str], removed: List[str],
           old_label: str, new_label: str) -> str:
    lines = []
    n_break = sum(1 for c in changes if c.severity == "breaking")
    lines.append("%s -> %s" % (old_label, new_label))
    if not changes:
        lines.append("no changes; the two snapshots describe the same toolkit")
        return "\n".join(lines)
    lines.append("%d change(s): %d breaking, %d behavior, %d cosmetic" % (
        len(changes),
        n_break,
        sum(1 for c in changes if c.severity == "behavior"),
        sum(1 for c in changes if c.severity == "cosmetic"),
    ))
    lines.append("")
    last_sev = None
    for c in changes:
        if c.severity != last_sev:
            lines.append("--- %s ---" % _ICON[c.severity])
            last_sev = c.severity
        lines.append("  [%s] %s" % (c.tool, c.kind))
        lines.append("    %s" % c.detail)
    return "\n".join(lines)


def to_json(changes: List[Change], added: List[str], removed: List[str]) -> Dict[str, Any]:
    return {
        "breaking": sum(1 for c in changes if c.severity == "breaking"),
        "changes": [c.as_dict() for c in changes],
        "added": added,
        "removed": removed,
    }


def dump_json(changes: List[Change], added: List[str], removed: List[str]) -> str:
    return json.dumps(to_json(changes, added, removed), ensure_ascii=False, indent=2)
