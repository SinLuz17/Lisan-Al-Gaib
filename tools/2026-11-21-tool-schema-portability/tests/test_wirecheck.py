import json
import os
import tempfile
import unittest

from wirecheck import PROFILES, all_keys, compile_all, compile_tool, get, load_tools
from wirecheck.cli import main
from wirecheck.report import render_text, to_json

SIMPLE = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}

REF_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"$ref": "#/$defs/Item"}},
    },
    "required": ["items"],
    "$defs": {
        "Item": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        }
    },
}


def _losses(compiled):
    return [(loss.pointer, loss.keyword, loss.kind, loss.severity) for loss in compiled.losses]


class LoadTests(unittest.TestCase):
    def test_openai_wrapper_shape(self):
        tools = load_tools({"tools": [{"type": "function", "function": {"name": "a", "parameters": SIMPLE}}]})
        self.assertEqual(tools[0][0], "a")
        self.assertEqual(tools[0][1]["type"], "object")

    def test_plain_list_shape(self):
        tools = load_tools([{"name": "a", "parameters": SIMPLE}])
        self.assertEqual(tools, [("a", SIMPLE)])

    def test_bare_map_shape(self):
        tools = load_tools({"a": SIMPLE})
        self.assertEqual(tools, [("a", SIMPLE)])

    def test_input_schema_is_accepted(self):
        tools = load_tools([{"name": "a", "input_schema": SIMPLE}])
        self.assertEqual(tools[0][1], SIMPLE)

    def test_nameless_entry_raises(self):
        with self.assertRaises(ValueError):
            load_tools([{"parameters": SIMPLE}])


class ProfileTests(unittest.TestCase):
    def test_all_profiles_have_sources(self):
        for key in all_keys():
            self.assertTrue(PROFILES[key].sources, key)
            self.assertTrue(PROFILES[key].notes, key)

    def test_unknown_profile_message(self):
        with self.assertRaises(KeyError) as ctx:
            get("cohere")
        self.assertIn("gemini", str(ctx.exception))

    def test_canonical_order(self):
        self.assertEqual(all_keys(), ("openai", "azure", "anthropic", "gemini"))


class RefTests(unittest.TestCase):
    def test_ref_is_inlined_for_gemini(self):
        compiled = compile_tool("t", REF_SCHEMA, get("gemini"))
        text = json.dumps(compiled.schema)
        self.assertNotIn("$ref", text)
        self.assertNotIn("$defs", text)
        self.assertEqual(compiled.schema["properties"]["items"]["items"]["properties"]["sku"]["type"], "string")

    def test_ref_is_kept_for_openai(self):
        compiled = compile_tool("t", REF_SCHEMA, get("openai"))
        self.assertIn("$defs", compiled.schema)
        self.assertEqual(compiled.schema["properties"]["items"]["items"]["$ref"], "#/$defs/Item")

    def test_recursive_ref_is_a_blocker(self):
        schema = {
            "type": "object",
            "properties": {"node": {"$ref": "#/$defs/Node"}},
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/$defs/Node"}},
                }
            },
        }
        compiled = compile_tool("t", schema, get("gemini"))
        self.assertEqual(compiled.verdict, "reject")
        refused = [loss for loss in compiled.losses if loss.kind == "refused"]
        self.assertTrue(refused)
        self.assertIn("recursive", refused[0].detail)

    def test_unresolvable_ref_is_a_blocker(self):
        schema = {"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}}
        compiled = compile_tool("t", schema, get("gemini"))
        self.assertEqual(compiled.verdict, "reject")
        self.assertTrue(any("unresolvable" in loss.detail for loss in compiled.losses))

    def test_external_ref_is_a_blocker(self):
        schema = {"type": "object", "properties": {"x": {"$ref": "https://example.com/s.json"}}}
        compiled = compile_tool("t", schema, get("gemini"))
        self.assertEqual(compiled.verdict, "reject")
        self.assertTrue(any("non-local" in loss.detail for loss in compiled.losses))


class StrictClosureTests(unittest.TestCase):
    def test_openai_closes_objects_and_requires_everything(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],
        }
        compiled = compile_tool("t", schema, get("openai"))
        wire = compiled.schema
        self.assertIs(wire["additionalProperties"], False)
        self.assertEqual(wire["required"], ["a", "b"])
        self.assertEqual(wire["properties"]["b"]["type"], ["integer", "null"])

    def test_gemini_never_gets_additional_properties(self):
        compiled = compile_tool("t", SIMPLE, get("gemini"))
        self.assertNotIn("additionalProperties", compiled.schema)

    def test_open_object_is_reported_as_a_coercion(self):
        schema = {
            "type": "object",
            "properties": {"meta": {"type": "object", "additionalProperties": True}},
            "required": ["meta"],
        }
        compiled = compile_tool("t", schema, get("openai"))
        hits = [l for l in compiled.losses if l.keyword == "additionalProperties"]
        self.assertEqual(hits[0].severity, "loss")
        self.assertEqual(compiled.verdict, "degraded")

    def test_nullable_keeps_existing_null(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": ["string", "null"]}},
            "required": [],
        }
        compiled = compile_tool("t", schema, get("openai"))
        self.assertEqual(compiled.schema["properties"]["a"]["type"], ["string", "null"])

    def test_nullable_handles_union(self):
        schema = {
            "type": "object",
            "properties": {"a": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
        }
        compiled = compile_tool("t", schema, get("openai"))
        self.assertEqual(
            compiled.schema["properties"]["a"]["oneOf"],
            [{"type": "string"}, {"type": "integer"}, {"type": "null"}],
        )


class KeywordTests(unittest.TestCase):
    def test_azure_drops_constraints_and_says_so(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 10}},
            "required": ["n"],
        }
        compiled = compile_tool("t", schema, get("azure"))
        node = compiled.schema["properties"]["n"]
        self.assertNotIn("minimum", node)
        self.assertIn("minimum 1", node["description"])
        self.assertIn("does not enforce", node["description"])

    def test_openai_keeps_constraints_but_flags_them(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1}},
            "required": ["n"],
        }
        compiled = compile_tool("t", schema, get("openai"))
        self.assertEqual(compiled.schema["properties"]["n"]["minimum"], 1)
        self.assertTrue(any(l.keyword == "minimum" and l.severity == "edge" for l in compiled.losses))

    def test_gemini_stringifies_enum(self):
        schema = {
            "type": "object",
            "properties": {"sev": {"type": "integer", "enum": [1, 2, 3]}},
            "required": ["sev"],
        }
        compiled = compile_tool("t", schema, get("gemini"))
        self.assertEqual(compiled.schema["properties"]["sev"]["enum"], ["1", "2", "3"])
        self.assertTrue(any("parse them back" in l.detail for l in compiled.losses))

    def test_const_becomes_enum(self):
        schema = {"type": "object", "properties": {"kind": {"const": "refund"}}, "required": ["kind"]}
        compiled = compile_tool("t", schema, get("gemini"))
        self.assertEqual(compiled.schema["properties"]["kind"]["enum"], ["refund"])
        self.assertNotIn("const", compiled.schema["properties"]["kind"])

    def test_property_named_like_a_keyword_survives(self):
        schema = {
            "type": "object",
            "properties": {
                "format": {"type": "string"},
                "maximum": {"type": "integer"},
            },
            "required": ["format", "maximum"],
        }
        compiled = compile_tool("t", schema, get("azure"))
        self.assertIn("format", compiled.schema["properties"])
        self.assertIn("maximum", compiled.schema["properties"])

    def test_prose_is_not_duplicated_on_recompile(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1}},
            "required": ["n"],
        }
        once = compile_tool("t", schema, get("azure"))
        twice = compile_tool("t", once.schema, get("azure"))
        self.assertEqual(twice.schema["properties"]["n"]["description"].count("does not enforce"), 1)


class AllOfTests(unittest.TestCase):
    def test_object_branches_merge(self):
        schema = {
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]},
            ]
        }
        compiled = compile_tool("t", schema, get("anthropic"))
        self.assertNotIn("allOf", compiled.schema)
        self.assertEqual(sorted(compiled.schema["properties"]), ["a", "b"])
        self.assertEqual(compiled.schema["required"], ["a", "b"])

    def test_non_object_branches_become_anyof_with_a_loss(self):
        schema = {"allOf": [{"type": "string", "minLength": 1}, {"type": "string", "maxLength": 5}]}
        compiled = compile_tool("t", schema, get("openai"))
        self.assertIn("anyOf", compiled.schema)
        self.assertTrue(any("conjunction is gone" in l.detail for l in compiled.losses))

    def test_conflicting_branches_are_reported(self):
        schema = {
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}},
                {"type": "object", "properties": {"a": {"type": "integer"}}},
            ]
        }
        compiled = compile_tool("t", schema, get("openai"))
        self.assertTrue(any("differently" in l.detail for l in compiled.losses))


class LimitTests(unittest.TestCase):
    def test_azure_property_limit(self):
        props = {"p%d" % i: {"type": "string"} for i in range(101)}
        compiled = compile_tool("t", {"type": "object", "properties": props}, get("azure"))
        self.assertEqual(compiled.verdict, "reject")
        self.assertTrue(any(l.keyword == "properties" and l.severity == "blocker" for l in compiled.losses))

    def test_azure_depth_limit(self):
        node = {"type": "object", "properties": {"leaf": {"type": "string"}}}
        for _ in range(5):
            node = {"type": "object", "properties": {"next": node}}
        compiled = compile_tool("t", node, get("azure"))
        self.assertEqual(compiled.verdict, "reject")
        self.assertTrue(any(l.keyword == "nesting" for l in compiled.losses))

    def test_anthropic_union_cap(self):
        schema = {
            "type": "object",
            "properties": {"x": {"anyOf": [{"type": "string"}] * 17}},
        }
        compiled = compile_tool("t", schema, get("anthropic"))
        self.assertEqual(compiled.verdict, "reject")
        self.assertTrue(any(l.keyword == "anyOf" for l in compiled.losses))

    def test_small_schema_hits_no_limits(self):
        compiled = compile_tool("t", SIMPLE, get("azure"))
        self.assertFalse(any(l.severity == "blocker" for l in compiled.losses))


class VerdictTests(unittest.TestCase):
    def test_portable_schema_passes_everywhere(self):
        for key in all_keys():
            compiled = compile_tool("t", SIMPLE, get(key))
            self.assertEqual(compiled.verdict, "pass", key + ": " + str(_losses(compiled)))

    def test_compile_all_is_a_product(self):
        tools = [("a", SIMPLE), ("b", SIMPLE)]
        compiled = compile_all(tools, [get(k) for k in all_keys()])
        self.assertEqual(len(compiled), len(tools) * len(all_keys()))

    def test_matrix_and_json(self):
        compiled = compile_all([("a", REF_SCHEMA)], [get("openai"), get("gemini")])
        text = render_text(compiled, ["openai", "gemini"])
        self.assertIn("openai", text)
        self.assertIn("a ", text)
        payload = json.loads(to_json(compiled, ["openai", "gemini"]))
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual({r["target"] for r in payload["results"]}, {"openai", "gemini"})

    def test_pointer_uses_property_names(self):
        schema = {
            "type": "object",
            "properties": {"inner": {"type": "object", "properties": {"v": {"type": "string", "maxLength": 3}}}},
        }
        compiled = compile_tool("t", schema, get("azure"))
        pointers = [l.pointer for l in compiled.losses]
        self.assertIn("/properties/inner/properties/v", pointers)


class CliTests(unittest.TestCase):
    def _write(self, payload):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(payload, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_exit_zero_for_portable_tool(self):
        path = self._write([{"name": "a", "parameters": SIMPLE}])
        self.assertEqual(main(["lint", path, "--targets", "gemini,openai"]), 0)

    def test_exit_one_for_blocker(self):
        path = self._write([{"name": "a", "parameters": {"type": "object", "properties": {"x": {"$ref": "x"}}}}])
        self.assertEqual(main(["lint", path, "--targets", "gemini"]), 1)

    def test_fail_on_degraded(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1}},
            "required": ["n"],
        }
        path = self._write([{"name": "a", "parameters": schema}])
        self.assertEqual(main(["lint", path, "--targets", "azure"]), 0)
        self.assertEqual(main(["lint", path, "--targets", "azure", "--fail-on", "degraded"]), 1)

    def test_compile_writes_one_file_per_target(self):
        path = self._write([{"name": "a", "parameters": SIMPLE}])
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(main(["compile", path, "--targets", "openai,gemini", "--out", folder]), 0)
            self.assertTrue(os.path.exists(os.path.join(folder, "openai", "a.json")))
            self.assertTrue(os.path.exists(os.path.join(folder, "gemini", "a.json")))

    def test_bad_target_is_a_usage_error(self):
        path = self._write([{"name": "a", "parameters": SIMPLE}])
        self.assertEqual(main(["lint", path, "--targets", "cohere"]), 2)

    def test_unknown_tool_is_a_usage_error(self):
        path = self._write([{"name": "a", "parameters": SIMPLE}])
        self.assertEqual(main(["lint", path, "--tool", "nope"]), 2)


if __name__ == "__main__":
    unittest.main()
