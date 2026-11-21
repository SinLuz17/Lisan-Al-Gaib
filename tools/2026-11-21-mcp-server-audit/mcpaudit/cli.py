"""scan / advisories 两个子命令；退出码 0=干净 1=有发现 2=用法错误。"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List

from .advisories import ADVISORIES
from .audit import Finding, scan_config


def _print_report(results, out) -> int:
    bad = 0
    for path, findings in results.items():
        out.write("config: %s\n" % path)
        for server, fs in findings.items():
            out.write("\n[%s]\n" % server)
            if not fs:
                out.write("  no findings\n")
            for f in fs:
                if f.severity in ("high", "medium"):
                    bad += 1
                line = "  %-6s %-24s %s" % (f.severity.upper(), f.kind, f.detail)
                out.write(line + "\n")
                for r in f.refs:
                    out.write("         ref: %s\n" % r)
        out.write("\n")
    return bad


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(prog="mcpaudit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="audit MCP config file(s)")
    p_scan.add_argument("configs", nargs="+", help="config file path(s)")
    p_scan.add_argument("--json", action="store_true", dest="as_json")
    p_scan.add_argument("--tools", help="sidecar JSON file with tool defs per server name, "
                                        '{ "server-name": [ {tool...}, ... ] }')

    sub.add_parser("advisories", help="list snapshot advisories with sources")

    args = ap.parse_args(argv)

    if args.cmd == "advisories":
        for adv in ADVISORIES:
            print("%s (%s)  affected %s  fixed %s" % (
                adv["package"], adv["ecosystem"],
                ",".join(adv["affected"]), adv["fixed"]))
            for r in adv["refs"]:
                print("   ref: %s" % r)
        return 0

    tools_by_server = {}
    if getattr(args, "tools", None):
        try:
            with open(args.tools, "r", encoding="utf-8") as fh:
                tools_by_server = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write("mcpaudit: cannot read --tools file: %s\n" % e)
            return 2
        if not isinstance(tools_by_server, dict):
            sys.stderr.write("mcpaudit: --tools file must map server name -> list of tools\n")
            return 2

    results = {}
    for path in args.configs:
        try:
            results[path] = scan_config(path, tools_by_server)
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write("mcpaudit: cannot read %s: %s\n" % (path, e))
            return 2
        except ValueError as e:
            sys.stderr.write("mcpaudit: %s: %s\n" % (path, e))
            return 2

    if args.as_json:
        print(json.dumps(
            {path: {s: [f.as_dict() for f in fs] for s, fs in findings.items()}
             for path, findings in results.items()},
            indent=2, ensure_ascii=False))
    else:
        bad = _print_report(results, sys.stdout)
        if bad:
            print("%d finding(s) worth acting on (exit 1)" % bad)

    bad_total = sum(
        1 for findings in results.values() for fs in findings.values()
        for f in fs if f.severity in ("high", "medium")
    )
    return 1 if bad_total else 0


if __name__ == "__main__":
    sys.exit(main())
