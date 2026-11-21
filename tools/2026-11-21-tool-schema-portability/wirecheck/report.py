"""Rendering. The matrix answers "can I ship this to X"; the groups answer "what did I lose"."""

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Sequence

from .compile import Compiled

VERDICT_MARK = {
    "reject": "reject",
    "degraded": "degraded",
    "edge": "edge",
    "pass": "pass",
}

GROUP_TITLES = [
    ("blocker", "blockers \u2014 the target will refuse the request"),
    ("loss", "silent losses \u2014 accepted, not enforced, nothing reports them"),
    ("edge", "unverified \u2014 kept on the wire, needs one real request to settle"),
    ("info", "shape changes \u2014 required for the target, no meaning lost"),
]


def matrix(compiled: Sequence[Compiled], targets: Sequence[str]) -> str:
    by_key: Dict[tuple, Compiled] = {(c.name, c.profile): c for c in compiled}
    names: List[str] = []
    for c in compiled:
        if c.name not in names:
            names.append(c.name)

    tool_width = max([len("tool")] + [len(n) for n in names]) + 2
    col = max(9, max(len(t) for t in targets) + 1) if targets else 9
    fmt = "%-" + str(tool_width) + "s" + (" %-" + str(col) + "s") * len(targets)
    lines = [fmt % tuple(["tool"] + list(targets))]
    for name in names:
        cells = []
        for target in targets:
            item = by_key.get((name, target))
            if item is None:
                cells.append("-")
                continue
            counts = item.counts()
            extra = counts["blocker"] + counts["loss"]
            cells.append(VERDICT_MARK[item.verdict] + ("" if not extra else " %d" % extra))
        lines.append(fmt % tuple([name] + cells))
    return "\n".join(lines)


def render_text(
    compiled: Sequence[Compiled],
    targets: Sequence[str],
    limit: int = 6,
    explain: bool = False,
    sources: Dict[str, Sequence[tuple]] = None,
) -> str:
    names: List[str] = []
    for c in compiled:
        if c.name not in names:
            names.append(c.name)

    out: List[str] = []
    out.append("%d tool(s) x %d target(s)" % (len(names), len(targets)))
    out.append("")
    out.append(matrix(compiled, targets))
    out.append("")

    for severity, title in GROUP_TITLES:
        flat = [(item, loss) for item in compiled for loss in item.losses if loss.severity == severity]
        if not flat:
            continue
        out.append("%s: %d" % (title, len(flat)))
        current = None
        for index, (item, loss) in enumerate(flat):
            if index >= limit:
                out.append("  ... %d more in this group" % (len(flat) - index))
                break
            if item is not current:
                out.append("  %s @ %s  [%s]" % (item.name, item.profile, item.verdict))
                current = item
            out.append(
                "    %-44s %-24s %s" % (loss.pointer or "/", loss.keyword, loss.detail)
            )
        out.append("")

    if explain and sources:
        out.append("profile sources")
        for target in targets:
            out.append("  %s" % target)
            for label, url in sources.get(target, ()):
                out.append("    - %s: %s" % (label, url))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_json(compiled: Sequence[Compiled], targets: Sequence[str]) -> str:
    payload = {
        "targets": list(targets),
        "results": [
            {
                "tool": item.name,
                "target": item.profile,
                "verdict": item.verdict,
                "counts": item.counts(),
                "losses": [
                    {
                        "pointer": loss.pointer,
                        "keyword": loss.keyword,
                        "kind": loss.kind,
                        "severity": loss.severity,
                        "detail": loss.detail,
                    }
                    for loss in item.losses
                ],
            }
            for item in compiled
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
