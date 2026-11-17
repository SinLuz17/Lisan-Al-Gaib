"""Classify *why* a model turn ended, across four provider transports.

Why this module exists
----------------------
Tool-call arguments are model output, and model output can be cut off by the
output-token limit. On some transports a cut turn still hands back an argument
object that parses cleanly, so "the arguments parsed" says nothing about
whether they are complete. The stop signal on the *turn* is the only evidence
that survives every transport -- provided you still have it.

Each transport names it differently:

    Chat Completions   choices[0].finish_reason == "length"
    Responses API      status == "incomplete", incomplete_details.reason == "max_output_tokens"
    Anthropic Messages stop_reason == "max_tokens"
    Gemini             candidates[0].finishReason == "MAX_TOKENS"

This module turns any of those payloads into one :class:`StopVerdict`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Mapping, Optional


class StopKind(str, enum.Enum):
    """What the stop reason lets us conclude about the turn's tool calls."""

    #: The model finished on its own terms. Tool calls in this turn are intact.
    COMPLETE = "complete"
    #: The output budget ran out. Arguments may be cut mid-value.
    TRUNCATED = "truncated"
    #: The provider reports the call itself as unusable (syntax or semantics).
    MALFORMED = "malformed"
    #: Generation stopped but is explicitly resumable/continuable, not finished.
    PAUSED = "paused"
    #: Content filtering stopped the turn.
    FILTERED = "filtered"
    #: No stop signal, or one we do not recognise.
    UNKNOWN = "unknown"


#: Kinds where asking the model to resend a shorter call is a sensible response.
RETRYABLE_KINDS = frozenset({StopKind.TRUNCATED, StopKind.MALFORMED})

#: Kinds where a resend would just burn tokens differently.
NON_RETRYABLE_KINDS = frozenset({StopKind.PAUSED, StopKind.FILTERED, StopKind.UNKNOWN})


@dataclass(frozen=True)
class StopVerdict:
    kind: StopKind
    raw: str
    transport: str
    detail: str = ""

    @property
    def executable(self) -> bool:
        """True only when tool calls from this turn may run as-is."""
        return self.kind is StopKind.COMPLETE

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "raw": self.raw,
            "transport": self.transport,
            "detail": self.detail,
            "executable": self.executable,
        }


# --------------------------------------------------------------------------
# Transport detection
# --------------------------------------------------------------------------

_TRANSPORTS = ("chat_completions", "responses", "anthropic", "gemini")


def detect_transport(payload: Any) -> str:
    """Best-effort sniffing of which wire format a payload came from."""
    if not isinstance(payload, Mapping):
        return "unknown"
    # Gemini is the only one with a `candidates` array.
    if "candidates" in payload:
        return "gemini"
    # Chat Completions is the only one with a `choices` array.
    if "choices" in payload:
        return "chat_completions"
    # Responses API responses carry `object: "response"` and a status enum.
    if payload.get("object") == "response" or "incomplete_details" in payload:
        return "responses"
    status = payload.get("status")
    if isinstance(status, str) and status in {
        "completed",
        "incomplete",
        "failed",
        "cancelled",
    }:
        return "responses"
    # Anthropic Messages carry `type: "message"` and/or a top-level stop_reason.
    if payload.get("type") == "message" or "stop_reason" in payload:
        return "anthropic"
    return "unknown"


# --------------------------------------------------------------------------
# Reason maps
# --------------------------------------------------------------------------

_CHAT_FINISH = {
    "STOP": (StopKind.COMPLETE, "natural stop"),
    "TOOL_CALLS": (StopKind.COMPLETE, "model requested tool calls"),
    "FUNCTION_CALL": (StopKind.COMPLETE, "model requested a function call"),
    "LENGTH": (StopKind.TRUNCATED, "output token limit reached"),
    "CONTENT_FILTER": (StopKind.FILTERED, "content filter"),
}

_ANTHROPIC_STOP = {
    "END_TURN": (StopKind.COMPLETE, "natural end of turn"),
    "TOOL_USE": (StopKind.COMPLETE, "model requested tool use"),
    "STOP_SEQUENCE": (StopKind.COMPLETE, "hit a stop sequence"),
    "MAX_TOKENS": (StopKind.TRUNCATED, "output token limit reached"),
    "REFUSAL": (StopKind.FILTERED, "model refused"),
    "PAUSE_TURN": (StopKind.PAUSED, "turn paused and must be resumed"),
}

_RESPONSES_REASON = {
    "MAX_OUTPUT_TOKENS": (StopKind.TRUNCATED, "output token limit reached"),
    "CONTENT_FILTER": (StopKind.FILTERED, "content filter"),
}

_GEMINI_FINISH = {
    "STOP": (StopKind.COMPLETE, "natural stop"),
    "MAX_TOKENS": (StopKind.TRUNCATED, "output token limit reached"),
    # Google reports a call cut mid-argument as MALFORMED_FUNCTION_CALL far more
    # often than as MAX_TOKENS, because the truncated JSON fails to parse on
    # their side first. Treat it as retryable, not as a permanent defect.
    "MALFORMED_FUNCTION_CALL": (StopKind.MALFORMED, "function call syntactically invalid"),
    "UNEXPECTED_TOOL_CALL": (StopKind.MALFORMED, "function call semantically invalid"),
    "MALFORMED_RESPONSE": (StopKind.MALFORMED, "malformed response"),
    # Token limit reached, but the API says generation is not complete and can
    # be continued with a continuation token.
    "CONTINUATION": (StopKind.PAUSED, "output limit reached, response continuable"),
    "TOO_MANY_TOOL_CALLS": (StopKind.PAUSED, "server-side tool loop stopped early"),
    "MISSING_THOUGHT_SIGNATURE": (StopKind.MALFORMED, "missing thought signature"),
    "SAFETY": (StopKind.FILTERED, "safety policy"),
    "PROHIBITED_CONTENT": (StopKind.FILTERED, "prohibited content"),
    "BLOCKLIST": (StopKind.FILTERED, "blocklist term"),
    "SPII": (StopKind.FILTERED, "sensitive personal information"),
    "RECITATION": (StopKind.FILTERED, "recitation"),
    "LANGUAGE": (StopKind.FILTERED, "unsupported language"),
    "IMAGE_SAFETY": (StopKind.FILTERED, "image safety policy"),
    "IMAGE_PROHIBITED_CONTENT": (StopKind.FILTERED, "prohibited image content"),
    "OTHER": (StopKind.UNKNOWN, "unmapped provider reason: OTHER"),
    "FINISH_REASON_UNSPECIFIED": (StopKind.UNKNOWN, "provider sent no finish reason"),
}


def _norm_token(value: Any) -> str:
    """Normalise enum-ish values from SDKs into a plain upper-case token."""
    if value is None:
        return ""
    if isinstance(value, enum.Enum):
        value = value.name
    text = str(value)
    if "." in text:  # e.g. "FinishReason.MAX_TOKENS" or "StopReason.MAX_TOKENS"
        text = text.rsplit(".", 1)[-1]
    return text.strip().upper()


def _first(mapping: Any, key: str) -> Any:
    if isinstance(mapping, Mapping):
        return mapping.get(key)
    return None


def classify(payload: Any, transport: Optional[str] = None) -> StopVerdict:
    """Classify a provider payload into a :class:`StopVerdict`.

    ``transport`` may be given explicitly (``chat_completions``, ``responses``,
    ``anthropic``, ``gemini``, ``normalized``); otherwise it is sniffed.
    Unrecognised payloads return ``UNKNOWN`` -- callers should decide whether
    that blocks tool execution (the guard denies by default).
    """
    if transport is None:
        transport = detect_transport(payload)
    if transport not in _TRANSPORTS and transport != "normalized":
        return StopVerdict(StopKind.UNKNOWN, "", str(transport), "unsupported transport")
    if not isinstance(payload, Mapping):
        return StopVerdict(StopKind.UNKNOWN, "", transport, "payload is not a mapping")

    if transport == "normalized":
        token = _norm_token(payload.get("kind") or payload.get("stop_kind"))
        for kind in StopKind:
            if token == kind.value.upper():
                return StopVerdict(kind, token, "normalized", "explicitly supplied")
        return StopVerdict(StopKind.UNKNOWN, token, "normalized", "unmapped normalized kind")

    if transport == "chat_completions":
        choices = payload.get("choices") or []
        raw_value = _first(choices[0], "finish_reason") if choices else None
        raw = "" if raw_value is None else str(raw_value)
        token = _norm_token(raw_value)
        if not token:
            return StopVerdict(StopKind.UNKNOWN, "", transport, "no finish_reason yet")
        kind, detail = _CHAT_FINISH.get(token, (StopKind.UNKNOWN, f"unmapped finish_reason: {token}"))
        return StopVerdict(kind, raw, transport, detail)

    if transport == "anthropic":
        raw_value = payload.get("stop_reason")
        raw = "" if raw_value is None else str(raw_value)
        token = _norm_token(raw_value)
        if not token:
            return StopVerdict(StopKind.UNKNOWN, "", transport, "no stop_reason yet")
        kind, detail = _ANTHROPIC_STOP.get(
            token, (StopKind.UNKNOWN, f"unmapped stop_reason: {token}")
        )
        return StopVerdict(kind, raw, transport, detail)

    if transport == "responses":
        status = _norm_token(payload.get("status"))
        if not status:
            return StopVerdict(StopKind.UNKNOWN, "", transport, "no status yet")
        if status == "COMPLETED":
            return StopVerdict(StopKind.COMPLETE, str(payload.get("status")), transport, "completed")
        if status == "INCOMPLETE":
            raw_reason = _first(payload.get("incomplete_details"), "reason")
            reason = _norm_token(raw_reason)
            kind, detail = _RESPONSES_REASON.get(
                reason,
                (StopKind.UNKNOWN, f"incomplete for unmapped reason: {reason or 'none'}"),
            )
            return StopVerdict(
                kind, f"INCOMPLETE/{raw_reason or 'none'}", transport, detail
            )
        detail = {
            "FAILED": "request failed",
            "CANCELLED": "request cancelled",
        }.get(status, f"unmapped status: {status}")
        return StopVerdict(StopKind.UNKNOWN, str(payload.get("status")), transport, detail)

    # gemini
    candidates = payload.get("candidates") or []
    raw_value = _first(candidates[0], "finishReason") if candidates else None
    raw = "" if raw_value is None else str(raw_value)
    token = _norm_token(raw_value)
    if not token:
        return StopVerdict(StopKind.UNKNOWN, "", transport, "no finishReason yet")
    kind, detail = _GEMINI_FINISH.get(token, (StopKind.UNKNOWN, f"unmapped finishReason: {token}"))
    return StopVerdict(kind, raw, transport, detail)
