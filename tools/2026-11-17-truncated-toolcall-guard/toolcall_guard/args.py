"""Inspect tool-call argument text that may have been cut mid-flight.

Two jobs, both defensive:

1. :func:`scan_arguments` reports *shape* facts about a possibly-truncated JSON
   object: did it parse strictly, which top-level keys were closed before the
   cut, which key was being written. This is what makes a refusal debuggable
   ("the model had finished ``order_id`` but was cut inside ``amount``")
   without putting argument values into logs.

2. :func:`shape_signature` builds a retry-budget key that survives truncation.
   Content hashes do not: a model that resends the same oversized call gets cut
   at a slightly different byte every time, so a content-keyed retry counter
   never advances and the loop never ends. Keying on tool name plus the set of
   *closed* top-level keys is stable across those variations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class ArgScan:
    """Shape facts about one argument blob. Never carries the values."""

    parseable: bool
    keys_closed: Tuple[str, ...]
    key_open: Optional[str]
    depth: int
    unclosed_string: bool
    chars: int
    digest: str

    def to_log(self) -> dict:
        """A log-safe view: lengths and key names only, no values."""
        return {
            "parseable": self.parseable,
            "chars": self.chars,
            "sha256_12": self.digest,
            "keys_closed": list(self.keys_closed),
            "key_open": self.key_open,
            "depth": self.depth,
            "unclosed_string": self.unclosed_string,
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def scan_arguments(raw: Any) -> ArgScan:
    """Scan raw tool-call argument text.

    Accepts ``None`` (treated as empty) and non-string values (stringified),
    because SDKs differ on whether they expose the argument text or only the
    decoded object.
    """
    if raw is None:
        text = ""
    elif isinstance(raw, str):
        text = raw
    else:
        text = str(raw)

    digest = _digest(text)

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = _UNPARSEABLE
    if parsed is not _UNPARSEABLE:
        if isinstance(parsed, dict):
            return ArgScan(True, tuple(str(k) for k in parsed.keys()), None, 0, False, len(text), digest)
        # A JSON scalar or array is not a valid tool-argument object.
        return ArgScan(False, (), None, 0, False, len(text), digest)

    closed = []
    pending = None
    buf = []
    in_str = False
    esc = False
    depth = 0
    expect_key = True

    for ch in text:
        if in_str:
            if esc:
                esc = False
                if expect_key:
                    buf.append(ch)
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                if depth == 1 and expect_key:
                    pending = "".join(buf)
                    buf = []
                    expect_key = False
            elif expect_key:
                buf.append(ch)
            continue

        if ch == '"':
            in_str = True
            if depth == 1 and expect_key:
                buf = []
            continue

        if ch in "{[":
            depth += 1
            if depth == 1:
                expect_key = True
            continue

        if ch in "}]":
            if depth == 1 and pending is not None and not expect_key:
                closed.append(pending)
                pending = None
            depth -= 1
            continue

        if ch == ":" and depth == 1:
            expect_key = False
            continue

        if ch == "," and depth == 1:
            if pending is not None and not expect_key:
                closed.append(pending)
            pending = None
            expect_key = True
            continue

    return ArgScan(
        parseable=False,
        keys_closed=tuple(closed),
        key_open=pending,
        depth=depth,
        unclosed_string=in_str,
        chars=len(text),
        digest=digest,
    )


class _Unparseable:
    def __repr__(self) -> str:  # pragma: no cover - debug only
        return "<unparseable>"


_UNPARSEABLE = _Unparseable()


def shape_signature(tool_name: str, scan: ArgScan) -> str:
    """Stable retry-budget key: tool name + which top-level keys got closed."""
    material = "|".join(
        [
            tool_name,
            str(len(scan.keys_closed)),
            ",".join(sorted(scan.keys_closed)),
            scan.key_open or "",
        ]
    )
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def redact_arguments(raw: Any) -> dict:
    """Log-safe description of arguments, with no values and no raw text."""
    return scan_arguments(raw).to_log()
