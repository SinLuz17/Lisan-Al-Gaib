"""Tests for toolcall-guard. Run with:

    python -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from toolcall_guard import (  # noqa: E402
    StopKind,
    ToolCall,
    ToolCallGuard,
    audit,
    classify,
    load_turns,
    render,
    scan_arguments,
    shape_signature,
)


class TestStopClassification(unittest.TestCase):
    def test_chat_completions_length_is_truncation(self):
        payload = {"choices": [{"finish_reason": "length"}]}
        verdict = classify(payload)
        self.assertIs(verdict.kind, StopKind.TRUNCATED)
        self.assertEqual(verdict.transport, "chat_completions")
        self.assertFalse(verdict.executable)

    def test_chat_completions_tool_calls_is_complete(self):
        payload = {"choices": [{"finish_reason": "tool_calls"}]}
        self.assertIs(classify(payload).kind, StopKind.COMPLETE)

    def test_anthropic_max_tokens(self):
        self.assertIs(classify({"stop_reason": "max_tokens"}).kind, StopKind.TRUNCATED)
        self.assertIs(classify({"stop_reason": "tool_use"}).kind, StopKind.COMPLETE)
        self.assertIs(classify({"stop_reason": "pause_turn"}).kind, StopKind.PAUSED)

    def test_responses_incomplete_max_output_tokens(self):
        payload = {
            "object": "response",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        }
        verdict = classify(payload)
        self.assertIs(verdict.kind, StopKind.TRUNCATED)
        self.assertEqual(verdict.transport, "responses")

    def test_responses_completed(self):
        self.assertIs(classify({"object": "response", "status": "completed"}).kind, StopKind.COMPLETE)

    def test_gemini_finish_reasons(self):
        def payload(reason):
            return {"candidates": [{"finishReason": reason}]}

        self.assertIs(classify(payload("MAX_TOKENS")).kind, StopKind.TRUNCATED)
        self.assertIs(classify(payload("MALFORMED_FUNCTION_CALL")).kind, StopKind.MALFORMED)
        self.assertIs(classify(payload("CONTINUATION")).kind, StopKind.PAUSED)
        self.assertIs(classify(payload("STOP")).kind, StopKind.COMPLETE)
        self.assertIs(classify(payload("SAFETY")).kind, StopKind.FILTERED)

    def test_gemini_sdk_enum_is_normalised(self):
        payload = {"candidates": [{"finishReason": "FinishReason.MAX_TOKENS"}]}
        self.assertIs(classify(payload).kind, StopKind.TRUNCATED)

    def test_missing_signal_is_unknown_not_complete(self):
        verdict = classify({"role": "assistant"})
        self.assertIs(verdict.kind, StopKind.UNKNOWN)
        self.assertFalse(verdict.executable)

    def test_unmapped_value_is_unknown(self):
        self.assertIs(classify({"choices": [{"finish_reason": "eos"}]}).kind, StopKind.UNKNOWN)

    def test_explicit_transport_overrides_sniffing(self):
        payload = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        self.assertIs(classify(payload, "responses").kind, StopKind.TRUNCATED)


class TestArgumentScan(unittest.TestCase):
    def test_complete_object(self):
        scan = scan_arguments('{"order_id": "A-1001", "amount": 1000}')
        self.assertTrue(scan.parseable)
        self.assertEqual(scan.keys_closed, ("order_id", "amount"))
        self.assertIsNone(scan.key_open)

    def test_cut_mid_number_reports_open_key(self):
        scan = scan_arguments('{"order_id": "A-1001", "amount": 10')
        self.assertFalse(scan.parseable)
        self.assertEqual(scan.keys_closed, ("order_id",))
        self.assertEqual(scan.key_open, "amount")

    def test_cut_inside_nested_object(self):
        scan = scan_arguments('{"query": {"text": "a", "top_k": 5')
        self.assertFalse(scan.parseable)
        self.assertEqual(scan.keys_closed, ())
        self.assertEqual(scan.key_open, "query")
        self.assertEqual(scan.depth, 2)

    def test_cut_inside_string(self):
        scan = scan_arguments('{"to": "a@b.c", "body": "If i')
        self.assertTrue(scan.unclosed_string)
        self.assertEqual(scan.keys_closed, ("to",))
        self.assertEqual(scan.key_open, "body")

    def test_shape_signature_survives_different_cuts(self):
        a = shape_signature("send_email", scan_arguments('{"to": "a@b.c", "body": "If i'))
        b = shape_signature("send_email", scan_arguments('{"to": "a@b.c", "body": "If the cust'))
        self.assertEqual(a, b)

    def test_shape_signature_distinguishes_tools(self):
        a = shape_signature("send_email", scan_arguments('{"to": "a", "body": "x'))
        b = shape_signature("refund", scan_arguments('{"to": "a", "body": "x'))
        self.assertNotEqual(a, b)

    def test_non_object_json_is_not_a_valid_argument_object(self):
        self.assertFalse(scan_arguments("[1, 2, 3]").parseable)


class TestGuard(unittest.TestCase):
    def _truncated_payload(self):
        return {"type": "message", "stop_reason": "max_tokens"}

    def _refund(self, call_id="c1", raw='{"order_id": "A-1001", "amount": 10'):
        return ToolCall("c1" if call_id is None else call_id, "refund", {"amount": 10}, raw)

    def test_truncated_turn_blocks_every_call(self):
        guard = ToolCallGuard()
        calls = [self._refund("c1"), self._refund("c2", '{"order_id": "A-2", "amount": 3}')]
        decision = guard.inspect("t1", self._truncated_payload(), calls)
        self.assertEqual(decision.allowed, ())
        self.assertEqual(len(decision.refused), 2)
        self.assertIn("NOT executed", decision.refused[0].message)

    def test_valid_json_on_truncated_turn_is_still_blocked(self):
        guard = ToolCallGuard()
        call = ToolCall("c1", "refund", {"amount": 1000}, '{"order_id": "A-1", "amount": 1000}')
        decision = guard.inspect("t1", self._truncated_payload(), [call])
        self.assertTrue(decision.blocked)

    def test_complete_turn_allows_calls(self):
        guard = ToolCallGuard()
        decision = guard.inspect("t1", {"type": "message", "stop_reason": "tool_use"}, [self._refund()])
        self.assertEqual(len(decision.allowed), 1)
        self.assertEqual(decision.refused, ())

    def test_unknown_stop_denied_by_default(self):
        guard = ToolCallGuard()
        self.assertTrue(guard.inspect("t1", {}, [self._refund()]).blocked)

    def test_unknown_stop_can_be_allowed(self):
        guard = ToolCallGuard(on_unknown="allow")
        decision = guard.inspect("t1", {}, [self._refund()])
        self.assertEqual(len(decision.allowed), 1)
        self.assertTrue(any("on_unknown" in note for note in decision.notes))

    def test_per_call_mode_keeps_parseable_calls(self):
        guard = ToolCallGuard(atomic_turn=False)
        calls = [
            ToolCall("c1", "refund", {"amount": 1000}, '{"order_id": "A-1", "amount": 1000}'),
            ToolCall("c2", "refund", None, '{"order_id": "A-2", "amo'),
        ]
        decision = guard.inspect("t1", self._truncated_payload(), calls)
        self.assertEqual([c.call_id for c in decision.allowed], ["c1"])
        self.assertEqual([r.call_id for r in decision.refused], ["c2"])
        self.assertTrue(any("unproven" in note for note in decision.notes))

    def test_per_call_mode_withholds_calls_without_raw_text(self):
        guard = ToolCallGuard(atomic_turn=False)
        decision = guard.inspect("t1", self._truncated_payload(), [ToolCall("c1", "refund", {"a": 1})])
        self.assertEqual([r.call_id for r in decision.refused], ["c1"])

    def test_retry_budget_exhausts_on_repeated_shape(self):
        guard = ToolCallGuard(max_attempts=2)
        results = []
        for i in range(3):
            call = ToolCall(f"c{i}", "send_email", None, '{"to": "a@b.c", "body": "If i')
            results.append(guard.inspect(f"t{i}", self._truncated_payload(), [call]).refused[0])
        self.assertTrue(results[0].retryable)
        self.assertTrue(results[1].retryable)
        self.assertFalse(results[2].retryable)
        self.assertEqual(results[2].reason, "retry-exhausted")
        self.assertIn("do not", results[2].message)

    def test_retry_budget_does_not_advance_on_content_hash(self):
        """A model that keeps overrunning must not reset its budget by changing bytes."""
        guard = ToolCallGuard(max_attempts=1)
        first = guard.inspect("t0", self._truncated_payload(),
                              [ToolCall("c0", "x", None, '{"body": "aaaa')]).refused[0]
        second = guard.inspect("t1", self._truncated_payload(),
                               [ToolCall("c1", "x", None, '{"body": "aaaaaa')]).refused[0]
        self.assertTrue(first.retryable)
        self.assertFalse(second.retryable)

    def test_filtered_turn_is_not_retryable(self):
        guard = ToolCallGuard()
        refusal = guard.inspect("t1", {"stop_reason": "refusal"}, [self._refund()]).refused[0]
        self.assertFalse(refusal.retryable)
        self.assertEqual(refusal.reason, "filtered")

    def test_paused_turn_is_not_retryable(self):
        guard = ToolCallGuard()
        refusal = guard.inspect("t1", {"stop_reason": "pause_turn"}, [self._refund()]).refused[0]
        self.assertFalse(refusal.retryable)
        self.assertEqual(refusal.reason, "paused")

    def test_journal_never_stores_argument_values(self):
        guard = ToolCallGuard()
        call = ToolCall("c1", "send_email", None, '{"to": "a@b.c", "body": "secret-token-123')
        guard.inspect("t1", self._truncated_payload(), [call])
        blob = json.dumps(guard.journal(), ensure_ascii=False)
        self.assertNotIn("secret-token-123", blob)
        self.assertNotIn("a@b.c", blob)
        self.assertIn("body", blob)

    def test_tool_results_are_marked_as_errors(self):
        guard = ToolCallGuard()
        decision = guard.inspect("t1", self._truncated_payload(), [self._refund()])
        result = decision.tool_results()[0]
        self.assertTrue(result["is_error"])
        self.assertEqual(result["tool_call_id"], "c1")
        self.assertTrue(result["meta"]["retryable"])

    def test_turn_without_calls_is_not_flagged(self):
        guard = ToolCallGuard()
        decision = guard.inspect("t1", self._truncated_payload(), [])
        self.assertFalse(decision.blocked)


class TestAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace = ROOT / "examples" / "traces.jsonl"
        cls.turns = load_turns(str(cls.trace))
        cls.side_effects = {
            "refund": "write",
            "close_tickets": "write",
            "send_email": "write",
            "search_docs": "read",
            "get_order": "read",
        }
        cls.report = audit(cls.turns, cls.side_effects, source=str(cls.trace))

    def test_counts_turns_and_kinds(self):
        self.assertEqual(self.report.turns, 9)
        self.assertEqual(self.report.by_kind["truncated"], 5)
        self.assertEqual(self.report.by_kind["malformed"], 1)
        self.assertEqual(self.report.by_kind["paused"], 1)
        self.assertEqual(self.report.by_kind["filtered"], 1)
        self.assertEqual(self.report.by_kind["complete"], 1)

    def test_separates_unsafe_from_withheld(self):
        self.assertEqual(self.report.unsafe_turns, 6)
        self.assertEqual(self.report.withheld_turns, 2)

    def test_counts_invisible_and_repaired_calls(self):
        self.assertEqual(self.report.executed_calls, 6)
        # t-004 and t-005 had valid JSON arguments on a cut turn: a schema
        # validator would have passed them.
        self.assertEqual(self.report.invisible_calls, 2)
        self.assertEqual(self.report.unparseable_calls, 4)
        # t-001 and t-003 arrived as invalid text but with a decoded object.
        self.assertEqual(self.report.repaired_calls, 2)

    def test_flags_suspected_output_limit(self):
        self.assertEqual(self.report.suspected_limit_turns, 1)

    def test_exposure_by_side_effect(self):
        self.assertEqual(self.report.by_effect["write"], 4)
        self.assertEqual(self.report.by_effect["read"], 2)

    def test_write_tools_rank_first(self):
        first = next(iter(self.report.by_tool))
        self.assertEqual(self.report.by_tool[first]["effect"], "write")

    def test_render_mentions_the_key_numbers(self):
        text = render(self.report)
        self.assertIn("unsafe turns that still ran tools : 6", text)
        self.assertIn("schema-valid", text)


if __name__ == "__main__":
    unittest.main()
