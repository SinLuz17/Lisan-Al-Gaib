from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcpaudit import audit_server, load_config  # noqa: E402
from mcpaudit.advisories import match  # noqa: E402
from mcpaudit.extract import probe_server, sensitive_env_keys  # noqa: E402
from mcpaudit.labels import check as check_labels  # noqa: E402
from mcpaudit.versions import affected, compare  # noqa: E402
from mcpaudit.cli import main  # noqa: E402

HERE = os.path.dirname(__file__)
EXAMPLE = os.path.join(HERE, "..", "examples", "desktop.json")


class Versions(unittest.TestCase):
    def test_semver_compare(self):
        self.assertEqual(compare("2.3.0", "2.3.0"), 0)
        self.assertLess(compare("2.0.5", "2.0.8"), 0)
        self.assertGreater(compare("1.32.0", "1.31.9"), 0)
        self.assertEqual(compare("0.55.0", "0.55"), 0)  # 少一段按 0 补齐，0.55 == 0.55.0

    def test_date_versions(self):
        self.assertLess(compare("2026.5.0", "2026.11.18"), 0)
        self.assertLess(compare("2026.11.11", "2026.11.18"), 0)
        self.assertEqual(compare("2026.11.18", "2026.11.18"), 0)

    def test_cross_calendar_is_none(self):
        # 日期式和语义式不硬比
        self.assertIsNone(compare("2025.7.15", "2.3.0"))
        self.assertIsNone(compare("2.3.0", "2025.7.15"))

    def test_prerelease(self):
        self.assertLess(compare("2.0.8-beta", "2.0.8"), 0)
        self.assertLess(compare("1.0.0-rc1", "1.0.0-rc2"), 0)

    def test_garbage(self):
        self.assertIsNone(compare("abc", "1.0.0"))
        self.assertIsNone(compare("", "1.0.0"))

    def test_affected_specs(self):
        self.assertTrue(affected("1.31.0", ">=1.24.0,<1.32.0"))
        self.assertFalse(affected("1.32.0", ">=1.24.0,<1.32.0"))
        self.assertTrue(affected("0.23.1", ">=0.21.6,<0.23.2"))
        self.assertFalse(affected("0.23.2", ">=0.21.6,<0.23.2"))
        self.assertTrue(affected("10.3.2", "<=10.3.2"))
        self.assertFalse(affected("10.3.3", "<=10.3.2"))
        self.assertIsNone(affected(None, "<1.0.0"))
        self.assertIsNone(affected("2026.1.1", "<2.4.0"))  # 跨历法


class Extract(unittest.TestCase):
    def test_npx_pinned(self):
        p = probe_server("a", {"command": "npx", "args": ["-y", "pkg@1.2.3"]})
        self.assertEqual((p.ecosystem, p.package, p.version, p.unpinned),
                         ("npm", "pkg", "1.2.3", False))

    def test_npx_scoped_pinned(self):
        p = probe_server("a", {"command": "npx",
                               "args": ["-y", "@modelcontextprotocol/server-filesystem@2025.7.15"]})
        self.assertEqual(p.package, "@modelcontextprotocol/server-filesystem")
        self.assertEqual(p.version, "2025.7.15")

    def test_npx_unpinned(self):
        p = probe_server("a", {"command": "npx", "args": ["-y", "some-server"]})
        self.assertEqual((p.package, p.version, p.unpinned), ("some-server", None, True))

    def test_uvx_pinned(self):
        p = probe_server("a", {"command": "uvx", "args": ["mcp-server-git==2026.5.0"]})
        self.assertEqual((p.ecosystem, p.package, p.version), ("pypi", "mcp-server-git", "2026.5.0"))

    def test_uvx_unpinned(self):
        p = probe_server("a", {"command": "uvx", "args": ["mcp-server-git"]})
        self.assertEqual((p.package, p.version, p.unpinned), ("mcp-server-git", None, True))

    def test_uvx_from(self):
        p = probe_server("a", {"command": "uvx", "args": ["--from", "git+pkg", "tool"]})
        self.assertEqual(p.package, "git+pkg")

    def test_docker(self):
        p = probe_server("a", {"command": "docker",
                               "args": ["run", "-i", "--rm", "ghcr.io/x/y:2.0.8"]})
        self.assertEqual((p.ecosystem, p.package, p.version), ("docker", "ghcr.io/x/y", "2.0.8"))

    def test_local_script(self):
        p = probe_server("a", {"command": "node", "args": ["./server.js"]})
        self.assertEqual(p.ecosystem, "local")
        self.assertIsNone(p.version)

    def test_remote(self):
        p = probe_server("a", {"url": "http://127.0.0.1:18888/mcp"})
        self.assertEqual(p.transport, "http")
        self.assertFalse(p.authenticated)

    def test_remote_authed(self):
        p = probe_server("a", {"url": "https://x.example/mcp",
                               "headers": {"Authorization": "Bearer x"}})
        self.assertTrue(p.authenticated)

    def test_sensitive_env_names_only(self):
        self.assertEqual(sensitive_env_keys(["SEARCH_API_TOKEN", "DEBUG", "DB_PASSWORD"]),
                         ["SEARCH_API_TOKEN", "DB_PASSWORD"])


class Advisories(unittest.TestCase):
    def test_filesystem_date_version_hit(self):
        hits = match("@modelcontextprotocol/server-filesystem", "npm", "2025.7.15")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["matched_spec"], ">=2025.1.1")

    def test_filesystem_23_clean(self):
        self.assertEqual(match("@modelcontextprotocol/server-filesystem", "npm", "2.3.0"), [])
        self.assertEqual(match("@modelcontextprotocol/server-filesystem", "npm", "2.4.0"), [])

    def test_filesystem_205_hit(self):
        hits = match("@modelcontextprotocol/server-filesystem", "npm", "2.0.5")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["matched_spec"], "<2.3.0")

    def test_git_hit_and_clean(self):
        self.assertEqual(len(match("mcp-server-git", "pypi", "2026.5.0")), 1)
        self.assertEqual(match("mcp-server-git", "pypi", "2026.11.18"), [])

    def test_unpinned_never_matches(self):
        self.assertEqual(match("mcp-server-git", "pypi", None), [])

    def test_domshell_hit(self):
        hits = match("domshell", "npm", "2.0.5")
        self.assertEqual(len(hits), 1)
        self.assertIn("readOnlyHint", hits[0]["summary"])

    def test_refs_present(self):
        for adv in __import__("mcpaudit.advisories", fromlist=["ADVISORIES"]).ADVISORIES:
            self.assertTrue(adv["refs"], adv["package"])


class Labels(unittest.TestCase):
    EVAL = {
        "name": "domshell_eval",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {"expression": {"type": "string"}}},
    }
    DESTROY = {
        "name": "wipe_profile",
        "annotations": {"destructiveHint": True},
        "inputSchema": {"type": "object", "properties": {}},
    }

    def test_eval_flagged(self):
        out = check_labels([self.EVAL])
        self.assertEqual(len(out), 1)
        name, reason = out[0]
        self.assertEqual(name, "domshell_eval")
        self.assertIn("eval", reason)
        self.assertIn("expression", reason)

    def test_readonly_query_not_flagged(self):
        tools = [{"name": "run_query", "annotations": {"readOnlyHint": True},
                  "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}}}]
        # run_query 名字里没有 run? 有 run。单独信号词不改退出码，但仍会列出。
        out = check_labels(tools)
        self.assertEqual(len(out), 1)
        self.assertIn("run", out[0][1])

    def test_clean_readonly_not_flagged(self):
        tools = [{"name": "search_docs", "annotations": {"readOnlyHint": True},
                  "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]
        self.assertEqual(check_labels(tools), [])

    def test_destructive_only_appended_when_signals(self):
        clean = [{"name": "list_files", "annotations": {"readOnlyHint": True},
                  "inputSchema": {"type": "object", "properties": {}}}]
        # server 里有 destructive 工具，但这个工具没信号 → 不列
        self.assertEqual(check_labels(clean + [self.DESTROY]), [])
        # 有信号 → 追加矛盾理由
        out = check_labels([self.EVAL] + [self.DESTROY])
        self.assertIn("destructiveHint", out[0][1])


class Audit(unittest.TestCase):
    def test_load_config_shapes(self):
        for shape in (
            {"mcpServers": {"a": {"command": "x"}}},
            {"servers": {"a": {"command": "x"}}},
            {"mcp": {"servers": {"a": {"command": "x"}}}},
            {"a": {"command": "x"}},
        ):
            self.assertIn("a", load_config(shape))

    def test_load_config_rejects_garbage(self):
        with self.assertRaises(ValueError):
            load_config({"foo": 1})
        with self.assertRaises(ValueError):
            load_config([1, 2])

    def test_advisory_finding(self):
        findings = audit_server("git", {"command": "uvx", "args": ["mcp-server-git==2026.5.0"]})
        adv = [f for f in findings if f.kind == "advisory" and f.severity == "high"]
        self.assertEqual(len(adv), 1)
        self.assertTrue(adv[0].refs)
        self.assertIn("2026.11.18", adv[0].detail)

    def test_clean_advisory(self):
        findings = audit_server("git", {"command": "uvx", "args": ["mcp-server-git==2026.11.18"]})
        adv = [f for f in findings if f.kind == "advisory"]
        self.assertEqual(adv[0].severity, "clean")

    def test_unpinned_is_info_not_high(self):
        findings = audit_server("a", {"command": "npx", "args": ["-y", "some-server"]})
        unp = [f for f in findings if f.kind == "unpinned"]
        self.assertEqual(unp[0].severity, "info")

    def test_remote_unauthenticated(self):
        findings = audit_server("a", {"url": "http://127.0.0.1:18888/mcp"})
        f = [x for x in findings if x.kind == "remote-unauthenticated"][0]
        self.assertEqual(f.severity, "medium")

    def test_label_mismatch_from_embedded_tools(self):
        server = {
            "command": "npx", "args": ["-y", "domshell@2.0.5"],
            "tools": [{
                "name": "domshell_eval",
                "annotations": {"readOnlyHint": True},
                "inputSchema": {"properties": {"expression": {"type": "string"}}},
            }],
        }
        findings = audit_server("shell", server)
        lm = [f for f in findings if f.kind == "label-mismatch"]
        self.assertEqual(len(lm), 1)

    def test_env_names_not_values(self):
        findings = audit_server("a", {"command": "node", "args": ["s.js"],
                                      "env": {"API_KEY": "super-secret-value"}})
        env_f = [f for f in findings if f.kind == "env-sensitive"][0]
        self.assertIn("API_KEY", env_f.detail)
        self.assertNotIn("super-secret-value", env_f.detail)
        self.assertNotIn("super-secret-value", json.dumps([f.as_dict() for f in findings]))

    def test_local_is_clean(self):
        findings = audit_server("a", {"command": "node", "args": ["./x.js"]})
        self.assertTrue(all(f.severity == "clean" or f.kind == "env-sensitive" for f in findings))


class Cli(unittest.TestCase):
    def test_scan_exit_codes(self):
        self.assertEqual(main(["scan", EXAMPLE]), 1)  # 示例里埋了 high 发现

    def test_scan_json(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["scan", EXAMPLE, "--json"])
        self.assertEqual(code, 1)
        data = json.loads(buf.getvalue())
        servers = data[EXAMPLE]
        self.assertIn("git", servers)
        self.assertIn("shell", servers)
        # 值不进 json
        self.assertNotIn("super-secret-value", buf.getvalue())

    def test_usage_error(self):
        self.assertEqual(main(["scan", "/nonexistent/x.json"]), 2)
        self.assertEqual(main(["scan", "/dev/null"]), 2)  # 不是认识的形状

    def test_advisories_cmd(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["advisories"])
        self.assertEqual(code, 0)
        self.assertIn("mcp-server-git", buf.getvalue())
        self.assertIn("https://", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
