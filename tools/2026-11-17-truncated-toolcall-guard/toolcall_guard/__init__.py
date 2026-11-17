"""toolcall-guard: refuse tool calls from a turn the model never finished.

Public surface:

    from toolcall_guard import ToolCallGuard, ToolCall, classify, audit
"""

from .args import ArgScan, scan_arguments, shape_signature, redact_arguments
from .audit import AuditReport, audit, load_turns, render
from .guard import Decision, Refusal, ToolCall, ToolCallGuard
from .stop import StopKind, StopVerdict, classify, detect_transport

__all__ = [
    "ArgScan",
    "AuditReport",
    "Decision",
    "Refusal",
    "StopKind",
    "StopVerdict",
    "ToolCall",
    "ToolCallGuard",
    "audit",
    "classify",
    "detect_transport",
    "load_turns",
    "redact_arguments",
    "render",
    "scan_arguments",
    "shape_signature",
]

__version__ = "0.1.0"
