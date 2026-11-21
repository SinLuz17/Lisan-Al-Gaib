"""从 command/args/url 抠出包名、版本与 transport 形状。

MCP 配置没有统一的"包名+版本"字段，只有启动命令。这里按真实世界
出现过的形状解析；解析不出来的如实返回 None，不猜。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Probe:
    """一个 server 的接入面摘要。"""

    name: str
    transport: str                     # "stdio" | "http" | "unknown"
    ecosystem: Optional[str] = None    # "npm" | "pypi" | "docker" | "local" | None
    package: Optional[str] = None
    version: Optional[str] = None      # None = 没钉版本或无从判断
    unpinned: bool = False             # 包管理器参与但没钉版本
    authenticated: Optional[bool] = None   # 仅 http：有没有认证头
    env_keys: List[str] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)


_NPX = {"npx", "pnpm", "pnpm dlx", "bunx", "yarn"}
_UVX = {"uvx", "uvx.exe", "pipx", "pipx run"}
_PIP = {"pip", "pip3", "uv"}

# npx -y pkg@1.2.3 之类；包名允许 scoped @scope/pkg@ver
_PKG_NPM = re.compile(r"^(@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+(@[A-Za-z0-9_.<>^~*-]+)?$")
_PKG_PYPI = re.compile(r"^([A-Za-z0-9_.-]+)(==|>=|<=|~=)([A-Za-z0-9_.]+)$")
_SEMVERISH = re.compile(r"^[0-9]+(\.[0-9]+){1,3}([-.][A-Za-z0-9.]+)?$")

# 信号词：名字/参数里出现说明这个"只读"声明值得再看一眼
_ACTION_WORDS = (
    "eval", "exec", "execute", "run", "evaluate", "runtime",
    "write", "create", "delete", "del", "drop", "remove", "insert",
    "update", "patch", "send", "deploy", "install", "submit", "post",
)
_CODE_PARAM_WORDS = ("code", "script", "source", "expression", "expr", "snippet")


def _clean_flags(args: List[str]) -> List[str]:
    """去掉 -y / --yes / --package 这类 flag，只留位置参数。"""
    out, skip_next = [], False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a.startswith("-"):
            # --package pkg@ver 把后面的参数吃掉
            if a in ("--package", "-p", "--from"):
                skip_next = True
            continue
        out.append(a)
    return out


def probe_server(name: str, server: Dict[str, Any],
                 tools: Optional[List[Dict[str, Any]]] = None) -> Probe:
    url = server.get("url")
    is_remote = bool(url) or str(server.get("type", "")).lower() in ("http", "sse", "streamable-http")
    env = server.get("env") or {}
    env_keys = sorted(str(k) for k in env.keys())

    if is_remote:
        headers = server.get("headers") or {}
        authed = any(
            str(k).lower() in ("authorization", "x-api-key", "x-auth-token", "proxy-authorization")
            for k in headers.keys()
        ) or bool(server.get("auth")) or "token" in str(url).lower()
        return Probe(
            name=name,
            transport="http",
            authenticated=bool(authed),
            env_keys=env_keys,
            tools=list(tools or []),
        )

    command = str(server.get("command") or "")
    args = [str(a) for a in (server.get("args") or [])]
    base = command.rsplit("/", 1)[-1].lower()

    p = Probe(name=name, transport="stdio", env_keys=env_keys, tools=list(tools or []))

    if base in _NPX:
        p.ecosystem = "npm"
        pos = _clean_flags(args)
        if pos:
            m = _PKG_NPM.match(pos[0])
            if m:
                pkg = m.group(0)
                if "@" in pkg.lstrip("@"):
                    stem, _, ver = pkg.rpartition("@")
                    p.package, p.version = stem, ver
                else:
                    p.package, p.unpinned = pkg, True
    elif base in _UVX or base in _PIP:
        p.ecosystem = "pypi"
        pos = _clean_flags(args)
        if base == "uvx" and "--from" in args:
            idx = args.index("--from")
            if idx + 1 < len(args):
                p.package = args[idx + 1]
        if p.package is None and pos:
            m = _PKG_PYPI.match(pos[0])
            if m:
                p.package, p.version = m.group(1), m.group(3)
            elif _SEMVERISH.match(pos[0]) is None and not pos[0].endswith(".py"):
                p.package, p.unpinned = pos[0], True
        # pkg==ver 之外的直接调用（uvx somepkg==1.2 未命中上面的正则兜一把）
        if p.package is None and pos and "==" in pos[0]:
            stem, _, ver = pos[0].partition("==")
            p.package, p.version, p.unpinned = stem, ver, False
    elif base == "docker":
        p.ecosystem = "docker"
        subcommands = {"run", "create", "start", "exec", "container"}
        pos = _clean_flags(args)
        for a in pos:
            if a.lower() in subcommands:
                continue
            if ":" in a and "/" in a:
                img, _, tag = a.rpartition(":")
                p.package, p.version = img, tag
            elif "/" in a:
                p.package, p.unpinned = a, True
            break
    else:
        p.ecosystem = "local"
        p.package = command or None

    return p


def sensitive_env_keys(keys: List[str]) -> List[str]:
    """变量名里带敏感信号的；只碰名字，值一律不读。"""
    marks = ("token", "key", "secret", "password", "credential", "auth")
    return [k for k in keys if any(m in k.lower() for m in marks)]
