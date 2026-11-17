"""Audit run *after* the fact: which past turns ran tools they had no right to run?

The guard only helps from the moment you install it. Everything already logged
is unguarded, and the interesting failures left no error behind -- the run was
recorded as a success, the tool wrote something, and the model said "Done".
So the second half of this project reads recorded turns and reports exposure.

Trace format (JSON Lines, one turn per line)::

    {"turn_id": "t-001",
     "transport": "anthropic",          # optional, sniffed when absent
     "payload": {"stop_reason": "max_tokens", ...},   # raw provider payload
     "usage":  {"output_tokens": 102},               # optional
     "budget": {"max_output_tokens": 102},           # optional
     "calls": [{"call_id": "c1", "name": "refund",
                "raw_arguments": "{\"order_id\": \"A-1001\", \"amo",
                "arguments": {"order_id": "A-1001", "amount": 10},
                "executed": true, "status": "success"}]}

Any field beyond ``turn_id`` and ``calls`` is optional; a missing payload
classifies as UNKNOWN, which the report counts as unsafe-by-default.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .args import scan_arguments
from .stop import StopKind, StopVerdict, classify

READ = "read"
WRITE = "write"
UNKNOWN_EFFECT = "unknown"

_EFFECT_RANK = {WRITE: 3, UNKNOWN_EFFECT: 2, READ: 1}


@dataclass
class CallRecord:
    call_id: str
    name: str
    executed: bool
    parseable: bool
    has_decoded_args: bool
    raw_chars: int
    effect: str = UNKNOWN_EFFECT


@dataclass
class TurnRecord:
    turn_id: str
    verdict: StopVerdict
    calls: List[CallRecord] = field(default_factory=list)
    suspected_limit: bool = False


@dataclass
class AuditReport:
    source: str
    turns: int = 0
    by_kind: Dict[str, int] = field(default_factory=dict)
    unsafe_turns: int = 0
    withheld_turns: int = 0
    executed_calls: int = 0
    invisible_calls: int = 0
    unparseable_calls: int = 0
    repaired_calls: int = 0
    suspected_limit_turns: int = 0
    by_effect: Dict[str, int] = field(default_factory=dict)
    by_tool: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    examples: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "turns": self.turns,
            "by_kind": self.by_kind,
            "unsafe_turns": self.unsafe_turns,
            "withheld_turns": self.withheld_turns,
            "executed_calls_in_unsafe_turns": self.executed_calls,
            "schema_valid_but_unsafe_calls": self.invisible_calls,
            "unparseable_executed_calls": self.unparseable_calls,
            "repaired_executed_calls": self.repaired_calls,
            "suspected_output_limit_turns": self.suspected_limit_turns,
            "by_effect": self.by_effect,
            "by_tool": self.by_tool,
            "examples": self.examples,
        }


def _call_executed(raw: Mapping) -> bool:
    if "executed" in raw:
        return bool(raw["executed"])
    status = str(raw.get("status", "success")).lower()
    return status not in {"skipped", "not_run", "not-run", "withheld", "refused"}


def _effect(name: str, side_effects: Mapping[str, str]) -> str:
    value = str(side_effects.get(name, UNKNOWN_EFFECT)).lower()
    return value if value in {READ, WRITE} else UNKNOWN_EFFECT


def load_turns(path: str) -> List[TurnRecord]:
    turns: List[TurnRecord] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                raw = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{line_no}: not valid JSON ({exc})") from exc
            if not isinstance(raw, Mapping):
                raise ValueError(f"{path}:{line_no}: expected a JSON object")
            turns.append(_build_turn(raw, f"{path}:{line_no}"))
    return turns


def _build_turn(raw: Mapping, where: str) -> TurnRecord:
    turn_id = str(raw.get("turn_id") or raw.get("id") or where)
    transport = raw.get("transport")
    verdict = classify(raw.get("payload"), transport)

    usage = raw.get("usage") or {}
    budget = raw.get("budget") or {}
    used = _as_int(usage.get("output_tokens") or usage.get("completion_tokens"))
    cap = _as_int(budget.get("max_output_tokens") or budget.get("max_tokens"))
    suspected = bool(
        used is not None
        and cap
        and cap > 0
        and used >= cap * 0.95
        and verdict.kind in {StopKind.MALFORMED, StopKind.UNKNOWN}
    )

    calls: List[CallRecord] = []
    for entry in raw.get("calls") or []:
        if not isinstance(entry, Mapping):
            continue
        raw_args = entry.get("raw_arguments")
        decoded = entry.get("arguments")
        if raw_args is not None:
            arg_text: Any = raw_args
        elif decoded is None:
            arg_text = ""
        else:
            arg_text = json.dumps(decoded, ensure_ascii=False)
        scan = scan_arguments(arg_text)
        calls.append(
            CallRecord(
                call_id=str(entry.get("call_id") or entry.get("id") or f"{turn_id}-c{len(calls)}"),
                name=str(entry.get("name") or "unknown"),
                executed=_call_executed(entry),
                parseable=scan.parseable,
                has_decoded_args=decoded is not None,
                raw_chars=scan.chars,
            )
        )
    return TurnRecord(turn_id, verdict, calls, suspected)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def audit(
    turns: Sequence[TurnRecord],
    side_effects: Optional[Mapping[str, str]] = None,
    source: str = "trace",
    example_limit: int = 5,
) -> AuditReport:
    side_effects = side_effects or {}
    report = AuditReport(source=source, turns=len(turns))
    kinds: Counter = Counter()
    effects: Counter = Counter()
    tools: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"calls": 0, "turns": 0, "effect": UNKNOWN_EFFECT})
    seen_tool_turns: Dict[str, set] = defaultdict(set)

    for turn in turns:
        kinds[turn.verdict.kind.value] += 1
        if turn.suspected_limit:
            report.suspected_limit_turns += 1

        executed = [c for c in turn.calls if c.executed]
        if turn.verdict.executable:
            continue

        if turn.calls and not executed:
            report.withheld_turns += 1
            continue
        if not executed:
            continue

        report.unsafe_turns += 1
        for call in executed:
            effect = _effect(call.name, side_effects)
            call.effect = effect
            report.executed_calls += 1
            effects[effect] += 1
            if call.parseable:
                report.invisible_calls += 1
            else:
                report.unparseable_calls += 1
                if call.has_decoded_args:
                    # Invalid text but a decoded object exists: something
                    # repaired it, and repair invents values rather than
                    # removing them.
                    report.repaired_calls += 1
            tools[call.name]["calls"] += 1
            tools[call.name]["effect"] = effect
            if turn.turn_id not in seen_tool_turns[call.name]:
                seen_tool_turns[call.name].add(turn.turn_id)
                tools[call.name]["turns"] += 1

        if len(report.examples) < example_limit:
            report.examples.append(
                {
                    "turn_id": turn.turn_id,
                    "stop": f"{turn.verdict.transport}:{turn.verdict.raw}",
                    "executed": [c.name for c in executed],
                    "suspected_output_limit": turn.suspected_limit,
                }
            )

    report.by_kind = {k: kinds.get(k, 0) for k in [kind.value for kind in StopKind]}
    report.by_effect = {k: effects.get(k, 0) for k in [WRITE, UNKNOWN_EFFECT, READ]}
    report.by_tool = dict(
        sorted(
            tools.items(),
            key=lambda kv: (-_EFFECT_RANK.get(kv[1]["effect"], 0), -kv[1]["calls"], kv[0]),
        )
    )
    return report


def render(report: AuditReport) -> str:
    lines: List[str] = []
    lines.append(f"trace: {report.source}")
    lines.append(f"turns: {report.turns}")

    parts = [f"{k}: {v}" for k, v in report.by_kind.items() if v]
    if parts:
        lines.append("stop classes: " + ", ".join(parts))

    lines.append("")
    lines.append(f"unsafe turns that still ran tools : {report.unsafe_turns}")
    lines.append(f"turns correctly withheld          : {report.withheld_turns}")
    lines.append(f"tool calls executed behind them   : {report.executed_calls}")
    lines.append(f"  schema-valid (invisible to arg validation): {report.invisible_calls}")
    lines.append(f"  unparseable text (repair territory)      : {report.unparseable_calls}")
    lines.append(f"  of those, decoded args present (repaired): {report.repaired_calls}")
    if report.suspected_limit_turns:
        lines.append(f"  turns where usage >= 95% of the budget    : {report.suspected_limit_turns}")

    if report.executed_calls:
        lines.append("")
        lines.append(
            "exposure by side effect: "
            f"write {report.by_effect.get(WRITE, 0)}, "
            f"unknown {report.by_effect.get(UNKNOWN_EFFECT, 0)}, "
            f"read {report.by_effect.get(READ, 0)}"
        )
        lines.append("")
        lines.append("by tool")
        for name, row in report.by_tool.items():
            lines.append(
                f"  {name:<24} {row['effect']:<8} {row['calls']} call(s) in {row['turns']} turn(s)"
            )
    else:
        lines.append("")
        lines.append("no tool calls ran on a turn that did not finish.")

    if report.examples:
        lines.append("")
        lines.append("examples")
        for ex in report.examples:
            flag = " [usage at limit]" if ex["suspected_output_limit"] else ""
            lines.append(f"  {ex['turn_id']}: {ex['stop']} -> {', '.join(ex['executed'])}{flag}")
    return "\n".join(lines)
