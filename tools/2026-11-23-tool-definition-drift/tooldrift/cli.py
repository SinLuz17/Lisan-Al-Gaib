"""cli：diff / demo。

退出码：0 两个快照一致；1 有 breaking 或 behavior 变化（可直接当门禁）；
2 用法错误（读不到文件、不认识的形状）。
cosmetic 不影响退出码——加了个工具不算事故。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

from .diff import diff_snapshots, BREAKING, BEHAVIOR
from .load import InputError, load_file, load_tools
from .report import dump_json, render


def _read(path: str):
    try:
        return load_file(path)
    except OSError as e:
        raise InputError("cannot read %s: %s" % (path, e))
    except json.JSONDecodeError as e:
        raise InputError("%s is not valid JSON: %s" % (path, e))


def cmd_diff(args) -> int:
    old = _read(args.old)
    new = _read(args.new)
    # load_tools 返回 {name: normalized}，diff_snapshots 自己再 load 一次，
    # 这里直接把 dict 喂给它（load_tools 对这个形状幂等）
    changes, added, removed = diff_snapshots(old, new)
    if args.json:
        print(dump_json(changes, added, removed))
    else:
        print(render(changes, added, removed, args.old, args.new))
    return 1 if any(c.severity in (BREAKING, BEHAVIOR) for c in changes) else 0


DEMO_OLD = {
    "tools": [
        {"name": "create_invoice",
         "description": "Create an invoice for a customer.",
         "annotations": {"readOnlyHint": False},
         "inputSchema": {"type": "object",
                         "properties": {"customer_id": {"type": "string"},
                                        "amount": {"type": "integer", "minimum": 1},
                                        "tier": {"type": "string",
                                                 "enum": ["standard", "priority"]},
                                        "note": {"type": "string"}},
                         "required": ["customer_id", "amount"]}},
        {"name": "search_orders",
         "description": "Search orders by filter.",
         "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object",
                         "properties": {"status": {"type": "string"},
                                        "internal_only": {"type": "boolean",
                                                          "default": True}}}},
    ],
}

DEMO_NEW = {
    "tools": [
        {"name": "create_invoice",
         "description": "Create an invoice for a customer. Never used for internal "
                        "accounts; if the customer record is flagged internal, refuse.",
         "annotations": {"readOnlyHint": False},
         "inputSchema": {"type": "object",
                         "properties": {"customer_id": {"type": "string"},
                                        "amount": {"type": "string"},
                                        "tier": {"type": "string",
                                                 "enum": ["standard"]},
                                        "note": {"type": "string"}},
                         "required": ["customer_id", "amount", "tier"]}},
        {"name": "search_orders",
         "description": "Search orders by filter.",
         "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object",
                         "properties": {"status": {"type": "string"},
                                        "internal_only": {"type": "boolean",
                                                          "default": False}}}},
        {"name": "export_orders", "description": "Export orders to CSV.",
         "inputSchema": {"type": "object", "properties": {}}},
    ],
}


def cmd_demo(args) -> int:
    old = load_tools(DEMO_OLD)
    new = load_tools(DEMO_NEW)
    changes, added, removed = diff_snapshots(old, new)
    print("demo: the usual story — the description got a new rule, an enum lost a "
          "value, an integer went string, a default flipped. Agent memory based on "
          "the old snapshot keeps calling it the old way.")
    print()
    print(render(changes, added, removed, "snapshot@old", "snapshot@new"))
    return 1


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="tooldrift",
                                     description="diff two snapshots of a toolset")
    sub = parser.add_subparsers(dest="cmd")

    p_diff = sub.add_parser("diff", help="diff old.json new.json")
    p_diff.add_argument("old")
    p_diff.add_argument("new")
    p_diff.add_argument("--json", action="store_true")
    p_diff.set_defaults(func=cmd_diff)

    p_demo = sub.add_parser("demo", help="run a built-in example")
    p_demo.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except InputError as e:
        print("tooldrift: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
