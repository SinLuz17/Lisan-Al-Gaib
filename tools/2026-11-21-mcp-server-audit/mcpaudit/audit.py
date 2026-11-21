"""审计编排：配置文件 → 每个 server 的 Finding 列表。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .advisories import match as match_advisories
from .extract import Probe, probe_server, sensitive_env_keys
from .labels import check as check_labels

_SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2, "clean": 3}


@dataclass
class Finding:
    server: str
    kind: str          # advisory / unpinned / remote-unauthenticated / label-mismatch / env-sensitive / local
    severity: str      # high / medium / info / clean
    detail: str
    refs: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "server": self.server,
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
            **({"refs": self.refs} if self.refs else {}),
        }


def _server_tools(server: Dict[str, Any]) -> List[Dict[str, Any]]:
    """配置里内嵌的工具清单（少见，但有的客户端会写）。"""
    tools = server.get("tools")
    return tools if isinstance(tools, list) else []


def audit_server(name: str, server: Dict[str, Any]) -> List[Finding]:
    tools = _server_tools(server)
    p = probe_server(name, server, tools=tools)
    findings: List[Finding] = []

    if p.transport == "http":
        if p.authenticated is False:
            findings.append(Finding(
                server=name, kind="remote-unauthenticated", severity="medium",
                detail="remote http/sse server without auth headers; browser-reachable "
                       "(DNS rebinding / CSRF class; Jupyter MCP and todoist-mcp were both hit this way)",
                refs=[
                    "https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open",
                ],
            ))
        else:
            findings.append(Finding(
                server=name, kind="remote-unauthenticated", severity="clean",
                detail="remote server, auth headers present",
            ))
    elif p.ecosystem == "local":
        findings.append(Finding(
            server=name, kind="local", severity="clean",
            detail="local stdio server via %s; no package manager involved, no advisory match possible" % (p.package or "unknown command"),
        ))

    if p.package and p.ecosystem in ("npm", "pypi", "docker"):
        if p.version is None and p.unpinned:
            findings.append(Finding(
                server=name, kind="unpinned", severity="info",
                detail="%s package %s without a pinned version: latest is pulled on every start, "
                       "so a clean check today says nothing about next month" % (p.ecosystem, p.package),
            ))
        elif p.version is not None:
            hits = match_advisories(p.package, p.ecosystem, p.version)
            if hits:
                for adv in hits:
                    findings.append(Finding(
                        server=name, kind="advisory", severity="high",
                        detail="%s (%s) %s hits %s — %s; fixed in %s%s" % (
                            p.package, p.ecosystem, p.version, adv["matched_spec"],
                            adv["summary"], adv["fixed"],
                            "; " + adv["note"] if adv.get("note") else "",
                        ),
                        refs=list(adv["refs"]),
                    ))
            else:
                findings.append(Finding(
                    server=name, kind="advisory", severity="clean",
                    detail="%s (%s) %s: no snapshot advisory matches" % (p.package, p.ecosystem, p.version),
                ))

    mismatches = check_labels(tools) if tools else []
    for tname, reason in mismatches:
        findings.append(Finding(
            server=name, kind="label-mismatch", severity="medium",
            detail="tool %s declares readOnlyHint but %s; the approval UI trusts the label "
                   "(see domshell_eval / CVE-2026-75518)" % (tname, reason),
            refs=["https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open"],
        ))

    sens = sensitive_env_keys(p.env_keys)
    if sens:
        findings.append(Finding(
            server=name, kind="env-sensitive", severity="info",
            detail="passes env var(s) %s to the server process (names only, values not read); "
                   "sandbox escapes have read env files back before" % ", ".join(sens),
            refs=["https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open"],
        ))

    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.kind))
    return findings


def load_config(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """认识的形状：mcpServers / servers / mcp.servers / 顶层 server 映射。"""
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    for key in ("mcpServers", "servers"):
        if isinstance(data.get(key), dict):
            return data[key]
    mcp = data.get("mcp")
    if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
        return mcp["servers"]
    # 顶层直接是 server 映射：每个值都是 dict 且长得像 server
    if data and all(isinstance(v, dict) and ("command" in v or "url" in v or "type" in v)
                    for v in data.values()):
        return data
    raise ValueError("no mcpServers/servers mapping found")


def scan_config(path: str,
                tools_by_server: Optional[Dict[str, Any]] = None) -> Dict[str, List[Finding]]:
    """读配置并审计。tools_by_server 可选：sidecar 里按 server 名给的
    工具清单，并进对应 server 后一起审（大多数配置不带 tools）。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    servers = load_config(data)
    if tools_by_server:
        servers = {
            name: (dict(server, tools=tools_by_server[name])
                   if name in tools_by_server else server)
            for name, server in servers.items()
        }
    return {name: audit_server(name, server) for name, server in sorted(servers.items())}
