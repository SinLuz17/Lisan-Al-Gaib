"""Command line entry point: ``python -m toolcall_guard``.

    classify <payload.json> [--transport NAME]
    audit    <trace.jsonl> [--side-effects map.json] [--json]
    demo     [--trace path]

Nothing here needs a network or an API key.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .audit import audit, load_turns, render
from .guard import ToolCall, ToolCallGuard
from .stop import classify


def _read_payload(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cmd_classify(args: argparse.Namespace) -> int:
    payload = _read_payload(args.path)
    verdict = classify(payload, args.transport)
    print(json.dumps(verdict.as_dict(), indent=2, ensure_ascii=False))
    return 0 if verdict.executable else 2


def _cmd_audit(args: argparse.Namespace) -> int:
    turns = load_turns(args.trace)
    side_effects = {}
    if args.side_effects:
        side_effects = _read_payload(args.side_effects)
        if not isinstance(side_effects, dict):
            print("side-effects map must be a JSON object", file=sys.stderr)
            return 1
    report = audit(turns, side_effects, source=args.trace, example_limit=args.examples)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render(report))
    return 0 if report.executed_calls == 0 else 1


def _cmd_demo(args: argparse.Namespace) -> int:
    guard = ToolCallGuard(max_attempts=2)
    cut_payload = {
        "type": "message",
        "role": "assistant",
        "stop_reason": "max_tokens",
        "content": [],
    }

    print("=" * 72)
    print("1. Cut mid-number, but the framework still handed over amount=10")
    print("=" * 72)
    call = ToolCall(
        call_id="toolu_01",
        name="refund",
        arguments={"order_id": "A-1001", "amount": 10},
        raw_arguments='{"order_id": "A-1001", "amount": 10',
    )
    decision = guard.inspect("turn-1", cut_payload, [call])
    print(f"stop verdict : {decision.verdict.kind.value} ({decision.verdict.detail})")
    print(f"allowed      : {[c.call_id for c in decision.allowed] or 'none'}")
    print(f"blocked      : {[r.call_id for r in decision.refused]}")
    print(f"model sees   : {decision.refused[0].message}")
    print("what would have happened: a $10 refund instead of $1000.")

    print()
    print("=" * 72)
    print("2. Valid JSON on a cut turn: schema validation cannot save you here")
    print("=" * 72)
    call = ToolCall(
        call_id="toolu_02",
        name="refund",
        arguments={"order_id": "A-2002", "amount": 1000},
        raw_arguments='{"order_id": "A-2002", "amount": 1000}',
    )
    decision = guard.inspect("turn-2", cut_payload, [call])
    print(f"stop verdict : {decision.verdict.kind.value} ({decision.verdict.detail})")
    print(f"arguments    : parse cleanly, satisfy the schema")
    print(f"allowed      : {[c.call_id for c in decision.allowed] or 'none'}")
    print(f"reason       : {decision.refused[0].reason}")

    print()
    print("=" * 72)
    print("3. A model that keeps overrunning hits the retry budget")
    print("=" * 72)
    for i in range(3):
        again = ToolCall(
            call_id=f"toolu_1{i}",
            name="send_email",
            arguments={"to": "ops@example.com", "body": "..."},
            raw_arguments='{"to": "ops@example.com", "body": "If i',
        )
        d = guard.inspect(f"turn-1{i}", cut_payload, [again])
        refusal = d.refused[0]
        print(f"attempt {i + 1}: retryable={refusal.retryable} reason={refusal.reason}")
        if not refusal.retryable:
            print(f"  -> {refusal.message}")

    print()
    print("=" * 72)
    print("4. A clean turn passes straight through")
    print("=" * 72)
    ok_payload = dict(cut_payload, stop_reason="tool_use")
    decision = guard.inspect("turn-4", ok_payload, [ToolCall("toolu_04", "refund", {"amount": 1000},
                                                             '{"amount": 1000}')])
    print(f"stop verdict : {decision.verdict.kind.value} ({decision.verdict.detail})")
    print(f"allowed      : {[c.call_id for c in decision.allowed]}")

    print()
    print("=" * 72)
    print("5. Journal from the guard (shapes only, no argument values)")
    print("=" * 72)
    blocked_events = [event for event in guard.journal() if event["blocked"]]
    print(json.dumps(blocked_events[-1], indent=2, ensure_ascii=False))

    trace = Path(args.trace) if args.trace else _default_trace()
    if trace and trace.exists():
        print()
        print("=" * 72)
        print(f"6. Auditing a recorded trace: {trace.name}")
        print("=" * 72)
        report = audit(load_turns(str(trace)), _default_side_effects(), source=str(trace))
        print(render(report))
    else:
        print()
        print("(no trace file found; skipping the audit demo)")
    return 0


def _default_trace() -> Optional[Path]:
    candidate = Path(__file__).resolve().parent.parent / "examples" / "traces.jsonl"
    return candidate if candidate.exists() else None


def _default_side_effects() -> dict:
    return {
        "refund": "write",
        "close_tickets": "write",
        "send_email": "write",
        "search_docs": "read",
        "get_order": "read",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolcall_guard",
        description="Refuse tool calls from turns the model never finished.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser("classify", help="classify one raw provider payload")
    p_classify.add_argument("path", help="path to a JSON payload file")
    p_classify.add_argument(
        "--transport",
        choices=["chat_completions", "responses", "anthropic", "gemini", "normalized"],
        default=None,
    )
    p_classify.set_defaults(func=_cmd_classify)

    p_audit = sub.add_parser("audit", help="audit a JSONL trace of recorded turns")
    p_audit.add_argument("trace", help="path to a .jsonl trace")
    p_audit.add_argument("--side-effects", help="JSON map of tool name -> read|write")
    p_audit.add_argument("--json", action="store_true", help="machine-readable report")
    p_audit.add_argument("--examples", type=int, default=5, help="how many turns to quote")
    p_audit.set_defaults(func=_cmd_audit)

    p_demo = sub.add_parser("demo", help="run the built-in scenarios")
    p_demo.add_argument("--trace", help="trace to audit at the end (defaults to examples/)")
    p_demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
