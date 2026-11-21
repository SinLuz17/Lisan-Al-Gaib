"""CLI: lint, compile, demo."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Sequence

from .compile import compile_all, load_tools
from .profiles import PROFILES, all_keys, get
from .report import render_text, to_json

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE = os.path.join(os.path.dirname(_HERE), "examples", "ops-tools.json")


def _parse_targets(raw: str) -> List[str]:
    if not raw:
        return list(all_keys())
    out = []
    for chunk in raw.replace(" ", ",").split(","):
        if not chunk:
            continue
        get(chunk)  # raises with a helpful message
        out.append(chunk)
    return out


def _read(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _run(path: str, targets: Sequence[str]):
    tools = load_tools(_read(path))
    profiles = [get(t) for t in targets]
    return tools, compile_all(tools, profiles)


def cmd_lint(args: argparse.Namespace) -> int:
    targets = _parse_targets(args.targets)
    tools, compiled = _run(args.file, targets)
    if args.tool:
        compiled = [c for c in compiled if c.name == args.tool]
        if not compiled:
            print("no such tool: %s" % args.tool, file=sys.stderr)
            return 2
    if args.json:
        print(to_json(compiled, targets))
    else:
        sources = {key: PROFILES[key].sources for key in targets}
        sys.stdout.write(
            render_text(compiled, targets, limit=args.limit, explain=args.explain, sources=sources)
        )

    worst = "pass"
    for item in compiled:
        if item.verdict == "reject":
            worst = "reject"
            break
        if item.verdict == "degraded":
            worst = "degraded"
    if worst == "reject":
        return 1
    if worst == "degraded" and args.fail_on == "degraded":
        return 1
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    targets = _parse_targets(args.targets)
    tools, compiled = _run(args.file, targets)
    out_dir = args.out
    written = 0
    for item in compiled:
        folder = os.path.join(out_dir, item.profile)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, item.name + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(item.schema, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print("%-10s %-24s %-9s %s" % (item.profile, item.name, item.verdict, path))
        written += 1
    print("\n%d file(s) written; losses per file: run `lint` on the same input" % written)
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    for key in all_keys():
        profile = PROFILES[key]
        print("%s  %s" % (key, profile.label))
        print("  strict object : %s" % ("yes" if profile.strict_object else "no"))
        print("  accepts $ref  : %s" % ("yes" if profile.accepts_ref else "no"))
        print("  rejects       : %s" % (", ".join(sorted(profile.rejects)) or "-"))
        print("  silent        : %s" % (", ".join(sorted(profile.silent)) or "-"))
        print("  unverified    : %s" % (", ".join(sorted(profile.unverified)) or "-"))
        for note in profile.notes:
            print("  note          : %s" % note)
        for label, url in profile.sources:
            print("  source        : %s <%s>" % (label, url))
        print("")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    targets = list(all_keys())
    tools, compiled = _run(_EXAMPLE, targets)
    sources = {key: PROFILES[key].sources for key in targets}
    print("input: examples/ops-tools.json (%d tools)" % len(tools))
    print("")
    sys.stdout.write(render_text(compiled, targets, limit=5, explain=True, sources=sources))

    sample = [c for c in compiled if c.name == "refund_order" and c.profile == "gemini"]
    if sample:
        print("what actually goes on the wire \u2014 refund_order @ gemini")
        print(json.dumps(sample[0].schema, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wirecheck",
        description="Check and compile tool schemas for more than one provider.",
    )
    sub = parser.add_subparsers(dest="command")

    lint = sub.add_parser("lint", help="report what each target will reject or silently drop")
    lint.add_argument("file")
    lint.add_argument("--targets", default="", help="comma separated, default: all")
    lint.add_argument("--tool", default="", help="only this tool")
    lint.add_argument("--json", action="store_true")
    lint.add_argument("--explain", action="store_true", help="print the profile sources")
    lint.add_argument("--limit", type=int, default=6, help="detail lines per group")
    lint.add_argument(
        "--fail-on",
        choices=["reject", "degraded"],
        default="reject",
        help="exit 1 at this verdict (default: reject)",
    )
    lint.set_defaults(func=cmd_lint)

    comp = sub.add_parser("compile", help="write one wire schema per target")
    comp.add_argument("file")
    comp.add_argument("--targets", default="")
    comp.add_argument("--out", default="build")
    comp.set_defaults(func=cmd_compile)

    prof = sub.add_parser("profiles", help="show the rule snapshot and its sources")
    prof.set_defaults(func=cmd_profiles)

    demo = sub.add_parser("demo", help="run the bundled example")
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: Sequence[str] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)
