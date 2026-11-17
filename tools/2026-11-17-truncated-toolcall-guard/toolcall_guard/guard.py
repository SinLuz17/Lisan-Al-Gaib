"""The gate you put between "the model answered" and "the tools run".

Usage shape::

    guard = ToolCallGuard()
    decision = guard.inspect(turn_id, provider_payload, calls)
    for call in decision.allowed:      # only on a COMPLETE turn
        run(call)
    for err in decision.tool_results():  # feed refusals back to the model
        append(err)

Design decisions worth knowing about (all argued in the README):

* A turn is refused **as a unit** by default. Tool calls in one assistant turn
  are a plan, not a list of independent requests; running half of the plan is
  not a smaller version of the plan.
* Refusals never carry the argument values, only their shape. A cut call can
  contain a secret in the part that *did* arrive, and the refusal path is a
  logging path.
* Retry budgets are keyed on call shape, not call content, so a model that
  keeps overrunning gets stopped instead of looped.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from .args import ArgScan, scan_arguments, shape_signature
from .stop import (
    NON_RETRYABLE_KINDS,
    RETRYABLE_KINDS,
    StopKind,
    StopVerdict,
    classify,
)


@dataclass(frozen=True)
class ToolCall:
    """One tool call as the framework handed it to you."""

    call_id: str
    name: str
    arguments: Any = None
    #: Raw argument text when the transport exposes it. Chat Completions and
    #: Anthropic streams do; Gemini hands back a decoded object only.
    raw_arguments: Optional[str] = None

    def scan(self) -> ArgScan:
        if self.raw_arguments is not None:
            return scan_arguments(self.raw_arguments)
        if self.arguments is None:
            return scan_arguments("")
        import json

        try:
            return scan_arguments(json.dumps(self.arguments, ensure_ascii=False))
        except (TypeError, ValueError):
            return scan_arguments("")


@dataclass(frozen=True)
class Refusal:
    call_id: str
    name: str
    reason: str
    message: str
    retryable: bool
    attempt: int

    def as_tool_result(self) -> dict:
        """Provider-neutral error result. Adapt the shape to your transport."""
        return {
            "tool_call_id": self.call_id,
            "name": self.name,
            "is_error": True,
            "content": self.message,
            "meta": {"reason": self.reason, "attempt": self.attempt, "retryable": self.retryable},
        }


@dataclass(frozen=True)
class Decision:
    turn_id: str
    verdict: StopVerdict
    allowed: Tuple[ToolCall, ...] = ()
    refused: Tuple[Refusal, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.refused)

    @property
    def executed_nothing(self) -> bool:
        return not self.allowed

    def tool_results(self) -> List[dict]:
        return [r.as_tool_result() for r in self.refused]

    def as_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "verdict": self.verdict.as_dict(),
            "allowed": [c.call_id for c in self.allowed],
            "refused": [
                {
                    "call_id": r.call_id,
                    "name": r.name,
                    "reason": r.reason,
                    "attempt": r.attempt,
                    "retryable": r.retryable,
                }
                for r in self.refused
            ],
            "notes": list(self.notes),
        }


_REASON_BY_KIND = {
    StopKind.TRUNCATED: "truncated",
    StopKind.MALFORMED: "malformed",
    StopKind.PAUSED: "paused",
    StopKind.FILTERED: "filtered",
    StopKind.UNKNOWN: "unknown-stop",
}


class ToolCallGuard:
    """Stateful gate: tracks retry budgets and keeps a redacted journal."""

    def __init__(
        self,
        max_attempts: int = 2,
        atomic_turn: bool = True,
        on_unknown: str = "deny",
        journal: bool = True,
    ) -> None:
        if on_unknown not in {"deny", "allow"}:
            raise ValueError("on_unknown must be 'deny' or 'allow'")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts
        self.atomic_turn = atomic_turn
        self.on_unknown = on_unknown
        self._journaling = journal
        self._shape_attempts: Counter = Counter()
        self._tool_attempts: Counter = Counter()
        self._events: List[dict] = []

    # -- inspection ------------------------------------------------------

    def inspect(
        self,
        turn_id: str,
        payload: Any,
        calls: Sequence[ToolCall],
        transport: Optional[str] = None,
    ) -> Decision:
        verdict = classify(payload, transport)
        calls = tuple(calls)

        if not calls:
            decision = Decision(turn_id, verdict, notes=("no tool calls in this turn",))
            self._record(turn_id, verdict, calls, decision)
            return decision

        if verdict.executable:
            decision = Decision(turn_id, verdict, allowed=calls)
            self._record(turn_id, verdict, calls, decision)
            return decision

        if verdict.kind is StopKind.UNKNOWN and self.on_unknown == "allow":
            decision = Decision(
                turn_id,
                verdict,
                allowed=calls,
                notes=("unknown stop signal allowed by configuration (on_unknown='allow')",),
            )
            self._record(turn_id, verdict, calls, decision)
            return decision

        reason = _REASON_BY_KIND.get(verdict.kind, "unknown-stop")
        notes: List[str] = []
        allowed: List[ToolCall] = []
        refused: List[Refusal] = []

        if self.atomic_turn:
            targets, passed, skipped = calls, (), ()
            notes.append("atomic turn: every call in the turn was withheld")
        else:
            targets, passed, skipped = self._per_call_split(calls, notes)

        allowed.extend(passed)
        for call in targets:
            refused.append(self._refuse(call, verdict, reason))

        decision = Decision(turn_id, verdict, allowed=tuple(allowed), refused=tuple(refused), notes=tuple(notes))
        self._record(turn_id, verdict, calls, decision, skipped=skipped)
        return decision

    def _per_call_split(self, calls: Sequence[ToolCall], notes: List[str]):
        """Per-call mode: withhold only calls whose arguments are unreadable.

        This is the weaker mode and it is documented as such: a call whose
        argument object parses can still be cut, so anything this mode lets
        through is unevidenced. It exists for teams that need partial progress.
        """
        targets: List[ToolCall] = []
        passed: List[ToolCall] = []
        skipped: List[str] = []
        for call in calls:
            if call.raw_arguments is None:
                targets.append(call)
                skipped.append(call.call_id)
                continue
            if call.scan().parseable:
                passed.append(call)
                notes.append(
                    f"per-call mode let {call.call_id} through on syntax alone; completeness unproven"
                )
                continue
            targets.append(call)
        if skipped:
            notes.append("per-call mode withheld calls with no raw argument text (cannot verify)")
        return targets, tuple(passed), tuple(skipped)

    def _refuse(self, call: ToolCall, verdict: StopVerdict, reason: str) -> Refusal:
        scan = call.scan()
        sig = shape_signature(call.name, scan)
        self._shape_attempts[sig] += 1
        self._tool_attempts[call.name] += 1
        attempt = self._shape_attempts[sig]

        retryable = verdict.kind in RETRYABLE_KINDS and attempt <= self.max_attempts
        if verdict.kind in NON_RETRYABLE_KINDS:
            retryable = False

        if not retryable and verdict.kind in RETRYABLE_KINDS:
            reason = "retry-exhausted"

        return Refusal(
            call_id=call.call_id,
            name=call.name,
            reason=reason,
            message=self._message(call, verdict, scan, attempt, retryable, reason),
            retryable=retryable,
            attempt=attempt,
        )

    def _message(
        self,
        call: ToolCall,
        verdict: StopVerdict,
        scan: ArgScan,
        attempt: int,
        retryable: bool,
        reason: str,
    ) -> str:
        where = f"{verdict.transport}:{verdict.raw}" if verdict.raw else verdict.transport
        shape = ""
        if scan.keys_closed or scan.key_open:
            done = ", ".join(scan.keys_closed) or "none"
            shape = f" Complete arguments before the cut: {done}."
            if scan.key_open:
                shape += f" Cut while writing `{scan.key_open}`."
        head = (
            f"[toolcall-guard] `{call.name}` was NOT executed and nothing was changed. "
            f"The turn stopped without finishing ({where} - {verdict.detail})."
        )
        if reason == "retry-exhausted":
            return (
                f"{head}{shape} This call has already been refused {attempt - 1} time(s), so do not "
                "resend it as-is. Reduce the payload (summarize long text fields, drop optional ones, "
                "or split the work into several smaller calls), or tell the user what detail is missing."
            )
        if verdict.kind in {StopKind.PAUSED, StopKind.FILTERED}:
            return (
                f"{head}{shape} Do not resend it; this turn needs to be resumed or re-planned "
                "rather than retried."
            )
        if verdict.kind is StopKind.UNKNOWN:
            return (
                f"{head}{shape} The stop signal could not be read, so completeness cannot be "
                "proven. Resend the call only if it is cheap and safe to repeat."
            )
        return (
            f"{head}{shape} Resend it with shorter arguments. Attempt {attempt} of "
            f"{self.max_attempts} for this call shape."
        )

    # -- journal ---------------------------------------------------------

    def _record(
        self,
        turn_id: str,
        verdict: StopVerdict,
        calls: Sequence[ToolCall],
        decision: Decision,
        skipped: Iterable[str] = (),
    ) -> None:
        if not self._journaling:
            return
        self._events.append(
            {
                "ts": round(time.time(), 3),
                "turn_id": turn_id,
                "transport": verdict.transport,
                "stop_kind": verdict.kind.value,
                "stop_raw": verdict.raw,
                "blocked": decision.blocked,
                "allowed": [c.call_id for c in decision.allowed],
                "refused": [
                    {"call_id": r.call_id, "name": r.name, "reason": r.reason, "attempt": r.attempt}
                    for r in decision.refused
                ],
                # Shape only: a cut call can contain secrets in the part that arrived.
                "calls": [
                    {
                        "call_id": c.call_id,
                        "name": c.name,
                        **c.scan().to_log(),
                    }
                    for c in calls
                ],
                "skipped_by_policy": list(skipped),
            }
        )

    def journal(self) -> List[dict]:
        return list(self._events)

    def reset(self) -> None:
        self._shape_attempts.clear()
        self._tool_attempts.clear()
        self._events.clear()


def call_digest(call: ToolCall) -> str:
    """Content digest of a call, for de-duplication outside the retry budget."""
    scan = call.scan()
    material = f"{call.name}:{scan.digest}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:12]
