from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tooldrift import load_tools, diff_snapshots, Change  # noqa: E402
from tooldrift.diff import BREAKING, BEHAVIOR, COSMETIC, _walk_leaves  # noqa: E402
from tooldrift.load import InputError  # noqa: E402
from tooldrift.cli import main  # noqa: E402


def _tool(name, **kw):
    return dict({"name": name, "description": kw.get("description"),
                 "annotations": kw.get("annotations"),
                 "schema": kw.get("schema", {"type": "object", "properties": {}})})


class Load(unittest.TestCase):
    def test_mcp_shape(self):
        data = {"tools": [{"name": "t", "description": "d",
                           "inputSchema": {"type": "object"},
                           "annotations": {"readOnlyHint": True}}]}
        out = load_tools(data)
        self.assertEqual(out["t"]["schema"], {"type": "object"})
        self.assertTrue(out["t"]["annotations"]["readOnlyHint"])

    def test_bare_array_shape(self):
        data = [{"name": "t", "parameters": {"type": "object"}}]
        out = load_tools(data)
        self.assertEqual(out["t"]["schema"], {"type": "object"})
        self.assertIsNone(out["t"]["description"])

    def test_mapping_shape(self):
        data = {"t": {"type": "object", "properties": {"a": {"type": "string"}}}}
        out = load_tools(data)
        self.assertEqual(out["t"]["name"], "t")
        self.assertIn("a", out["t"]["schema"]["properties"])

    def test_roundtrip_idempotent(self):
        data = {"tools": [{"name": "t", "inputSchema": {"type": "object",
                                                        "properties": {"x": {"type": "string"}}}}]}
        once = load_tools(data)
        twice = load_tools(once)
        self.assertEqual(once, twice)

    def test_garbage(self):
        with self.assertRaises(InputError):
            load_tools({"foo": 1})
        with self.assertRaises(InputError):
            load_tools([{"description": "no name"}])


class WalkLeaves(unittest.TestCase):
    def test_arrays_use_bracket_path(self):
        tree = _walk_leaves({"items": [{"type": "string"}]})
        self.assertEqual(tree["/items/[]/type"], "string")


class DiffTool(unittest.TestCase):
    def _diff(self, old_schema, new_schema, old_desc="d", new_desc="d",
              old_ann=None, new_ann=None):
        old = _tool("t", schema=old_schema, description=old_desc, annotations=old_ann)
        new = _tool("t", schema=new_schema, description=new_desc, annotations=new_ann)
        from tooldrift.diff import diff_tool
        return diff_tool(old, new)

    def test_param_removed_is_breaking(self):
        old = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
        new = {"type": "object", "properties": {"a": {"type": "string"}}}
        kinds = [c.kind for c in self._diff(old, new)]
        self.assertEqual(kinds, ["param"])
        self.assertEqual(self._diff(old, new)[0].severity, BREAKING)

    def test_param_added_is_breaking(self):
        old = {"type": "object", "properties": {}}
        new = {"type": "object", "properties": {"a": {"type": "string"}}}
        self.assertEqual(self._diff(old, new)[0].severity, BREAKING)

    def test_type_change_is_breaking(self):
        old = {"type": "object", "properties": {"a": {"type": "integer"}}}
        new = {"type": "object", "properties": {"a": {"type": "string"}}}
        c = self._diff(old, new)[0]
        self.assertEqual((c.kind, c.severity), ("param", BREAKING))

    def test_required_add_is_breaking_remove_is_behavior(self):
        old = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
        new = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
        c = self._diff(old, new)[0]
        self.assertEqual((c.kind, c.severity), ("required", BREAKING))
        c2 = self._diff(new, old)[0]
        self.assertEqual((c2.kind, c2.severity), ("required", BEHAVIOR))

    def test_enum_value_gone_is_breaking_added_is_behavior(self):
        old = {"type": "object", "properties": {"tier": {"enum": ["a", "b"]}}}
        new = {"type": "object", "properties": {"tier": {"enum": ["a"]}}}
        c = self._diff(old, new)[0]
        self.assertEqual((c.kind, c.severity), ("enum", BREAKING))
        self.assertIn("replayed", c.detail)
        c2 = self._diff(new, old)[0]
        self.assertEqual(c2.severity, BEHAVIOR)

    def test_default_change_is_behavior(self):
        old = {"type": "object", "properties": {"x": {"type": "boolean", "default": True}}}
        new = {"type": "object", "properties": {"x": {"type": "boolean", "default": False}}}
        c = self._diff(old, new)[0]
        self.assertEqual((c.kind, c.severity), ("default", BEHAVIOR))

    def test_description_change_is_behavior(self):
        old = {"type": "object", "properties": {}}
        c = self._diff(old, old, old_desc="old text", new_desc="old text, plus a rule")
        self.assertEqual([x.kind for x in c], ["description"])
        self.assertEqual(c[0].severity, BEHAVIOR)

    def test_readonly_flip_called_out(self):
        old = {"type": "object", "properties": {}}
        c = self._diff(old, old, old_ann={"readOnlyHint": False}, new_ann={"readOnlyHint": True})
        self.assertEqual([x.kind for x in c], ["annotations"])
        self.assertIn("readOnlyHint", c[0].detail)

    def test_no_change_no_noise(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}},
                  "required": ["a"]}
        self.assertEqual(self._diff(schema, schema), [])

    def test_nested_change_reported(self):
        old = {"type": "object", "properties": {
            "a": {"type": "object", "properties": {"x": {"type": "string"}}}}}
        new = {"type": "object", "properties": {
            "a": {"type": "object", "properties": {"x": {"type": "integer"}}}}}
        c = self._diff(old, new)
        # x 的类型变化在嵌套层，走兜底路径报 schema 变化
        self.assertTrue(any(x.kind == "schema" for x in c), c)


class DiffSnapshots(unittest.TestCase):
    def test_added_removed_tools(self):
        old = load_tools([{"name": "a", "inputSchema": {}}])
        new = load_tools([{"name": "a", "inputSchema": {}}, {"name": "b", "inputSchema": {}}])
        changes, added, removed = diff_snapshots(old, new)
        self.assertEqual(added, ["b"])
        self.assertEqual(removed, [])
        self.assertEqual(changes[0].severity, COSMETIC)

    def test_removed_tool_is_breaking(self):
        old = load_tools([{"name": "a", "inputSchema": {}}])
        new = load_tools([{"name": "b", "inputSchema": {}}])
        changes, _, removed = diff_snapshots(old, new)
        self.assertEqual(removed, ["a"])
        self.assertEqual(changes[0].severity, BREAKING)
        self.assertIn("ghost", changes[0].detail)

    def test_identical_is_empty(self):
        data = load_tools([{"name": "a", "inputSchema": {}}])
        changes, added, removed = diff_snapshots(data, data)
        self.assertEqual((changes, added, removed), ([], [], []))


class Cli(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.old = os.path.join(self.dir, "old.json")
        self.new = os.path.join(self.dir, "new.json")
        self.same = os.path.join(self.dir, "same.json")

    def _write(self, path, data):
        with open(path, "w") as fh:
            json.dump(data, fh)

    def test_exit_codes(self):
        self._write(self.old, {"tools": [{"name": "t", "inputSchema": {
            "type": "object", "properties": {"a": {"type": "string"}}}}]})
        self._write(self.new, {"tools": [{"name": "t", "inputSchema": {
            "type": "object", "properties": {"a": {"type": "integer"}}}}]})
        self.assertEqual(main(["diff", self.old, self.new]), 1)

        self._write(self.same, {"tools": [{"name": "t", "inputSchema": {
            "type": "object", "properties": {"a": {"type": "string"}}}}]})
        self.assertEqual(main(["diff", self.old, self.same]), 0)

    def test_json_output(self):
        import io, contextlib
        self._write(self.old, {"tools": [{"name": "t", "inputSchema": {}}]})
        self._write(self.new, {"tools": [{"name": "t", "inputSchema": {}}]})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["diff", self.old, self.new, "--json"])
        data = json.loads(buf.getvalue())
        self.assertEqual(data["breaking"], 0)
        self.assertEqual(data["changes"], [])

    def test_usage_error(self):
        self.assertEqual(main(["diff", "/nonexistent/a.json", "/nonexistent/b.json"]), 2)
        self.assertEqual(main([]), 2)

    def test_demo(self):
        self.assertEqual(main(["demo"]), 1)


if __name__ == "__main__":
    unittest.main()
