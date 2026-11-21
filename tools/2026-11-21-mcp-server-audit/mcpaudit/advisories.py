"""advisory 快照：2026-11-21 手工整理，每条带来源。

只收有公开来源的条目；新的披露往这儿加。范围声明用 affected spec，
日期式与语义式版本混在一个生态里是常态（见 filesystem 那条）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .versions import affected as _ver_affected

ADVISORIES: List[Dict[str, Any]] = [
    {
        "package": "@modelcontextprotocol/server-filesystem",
        "ecosystem": "npm",
        "summary": "two symlink swaps after path validation reach files outside allowed dirs",
        "affected": ["<2.3.0", ">=2025.1.1"],
        # 2.3.0 修复，但 npm 把 2.3.0 排在所有 2025.x 日期版本之前——
        # 写死版本的人很容易钉回一个仍带洞的日期版本，所以范围两头都写。
        "fixed": "2.3.0 (also pulled into 2.4.0)",
        "note": "npm sorts 2.3.0 before every 2025.x date version; pin explicitly",
        "refs": [
            "https://artofcode.in/blog/ai-citations-not-proof-edtech",
            "https://undercodenews.com/mcp-servers-release-eight-security-fixes-after-git-configuration-flaw-exposes-ai-agents-to-code-execution-video",
        ],
    },
    {
        "package": "mcp-server-git",
        "ecosystem": "pypi",
        "summary": "repo-controlled git config runs commands through read-only tools "
                   "(filter drivers, promisor remote lazy fetch, gc.recentObjectsHook, config include)",
        "affected": ["<2026.11.18"],
        "fixed": "2026.11.18",
        "refs": [
            "https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open",
            "https://cyberstrategyinstitute.com/read-only-mcp-not-a-security-boundary",
        ],
    },
    {
        "package": "@modelcontextprotocol/server-everything",
        "ecosystem": "npm",
        "summary": "security fixes in the reference demo server",
        "affected": ["<2.3.1", ">=2025.1.1"],
        "fixed": "2.3.1",
        "refs": [
            "https://undercodenews.com/mcp-servers-release-eight-security-fixes-after-git-configuration-flaw-exposes-ai-agents-to-code-execution-video",
        ],
    },
    {
        "package": "@modelcontextprotocol/sdk",
        "ecosystem": "npm",
        "summary": "task store without session check (CVE-2026-111767), validation DoS via "
                   "unbounded arrays (CVE-2026-111768), cross-origin redirect carries headers "
                   "and OAuth bodies (CVE-2026-111769)",
        "affected": [">=1.24.0,<1.32.0", ">=1.3.0,<1.32.0"],
        "fixed": "1.32.0",
        "refs": ["https://psirt.com/de/advisory/CLSA-2026-1794695478"],
    },
    {
        "package": "mcp-server-fetch",
        "ecosystem": "pypi",
        "summary": "SSRF guard gap (IPv6 range treated as public) and robots.txt bypass",
        "affected": ["<2026.11.11"],
        "fixed": "2026.11.11",
        "refs": ["https://artofcode.in/blog/ai-citations-not-proof-edtech"],
    },
    {
        "package": "domshell",
        "ecosystem": "npm",
        "summary": "domshell_eval runs arbitrary JS via CDP Runtime.evaluate while declaring "
                   "readOnlyHint: true; approval UI shows the wrong trust tier (CVE-2026-75518)",
        "affected": ["<2.0.8"],
        "fixed": "2.0.8",
        "refs": [
            "https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open",
        ],
    },
    {
        "package": "jupyter-mcp-server",
        "ecosystem": "npm",
        "summary": "HTTP mode leaves /api/connect and /api/stop unguarded; DNS rebinding "
                   "points the kernel at attacker infrastructure (CVSS 9.3)",
        "affected": ["<1.0.3"],
        "fixed": "1.0.3",
        "refs": [
            "https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open",
        ],
    },
    {
        "package": "todoist-mcp",
        "ecosystem": "npm",
        "summary": "todoist-mcp-http write tools reachable by a rebinding page using the "
                   "API key the server holds (GHSA-833v-v58w-294h); PoC exists",
        "affected": ["<=10.3.2"],
        "fixed": "10.3.3",
        "refs": [
            "https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open",
        ],
    },
    {
        "package": "yt-mcp",
        "ecosystem": "npm",
        "summary": "add_attachment reads any file on disk and attaches it to a YouTrack "
                   "ticket (CVE-2026-75612)",
        "affected": ["<=1.18.0"],
        "fixed": "1.18.1",
        "refs": [
            "https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open",
        ],
    },
    {
        "package": "gortex",
        "ecosystem": "npm",
        "summary": "read tools are confined but write_file/edit_file/move_inline accept "
                   "absolute paths and skip checks; prompt-injected agent can write "
                   "~/.zshrc or .git/hooks/pre-commit (CVE-2026-62964)",
        "affected": ["<0.55.0"],
        "fixed": "0.55.0",
        "refs": [
            "https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open",
        ],
    },
    {
        "package": "obsidian-sync-mcp",
        "ecosystem": "npm",
        "summary": "runs without MCP_AUTH_TOKEN by default and exposes the vault",
        "affected": ["<0.5.7"],
        "fixed": "0.5.7",
        "refs": [
            "https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open",
        ],
    },
]


def match(package: str, ecosystem: str, version: Optional[str]) -> List[Dict[str, Any]]:
    """返回命中的 advisory 条目（原文 dict，另附命中原因）。

    version 为 None（没钉版本）时返回空列表——"没钉"与"踩雷"是两件事，
    调用方应单独报 unpinned，这里不掺和。
    """
    hits: List[Dict[str, Any]] = []
    if version is None:
        return hits
    for adv in ADVISORIES:
        if adv["package"] != package or adv["ecosystem"] != ecosystem:
            continue
        for spec in adv["affected"]:
            hit = _ver_affected(version, spec)
            if hit is None:  # 历法不混比：换个 spec 再试
                continue
            if hit:
                hit_entry = dict(adv)
                hit_entry["matched_spec"] = spec
                hits.append(hit_entry)
                break
    return hits
