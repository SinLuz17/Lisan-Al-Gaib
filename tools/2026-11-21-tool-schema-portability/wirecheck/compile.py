"""Compile one tool schema onto one provider profile, and account for the loss.

The interesting output is not the rewritten schema. It is the list of
constraints that the wire format can no longer carry: those are the ones that
produce no error at any layer and no field in any log.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .profiles import Profile

SEVERITY_ORDER = {"blocker": 0, "loss": 1, "edge": 2, "info": 3}

VERDICT_ORDER = {"reject": 0, "degraded": 1, "edge": 2, "pass": 3}


@dataclass(frozen=True)
class Loss:
    pointer: str
    keyword: str
    kind: str
    severity: str
    detail: str


@dataclass(frozen=True)
class Compiled:
    name: str
    profile: str
    schema: dict
    losses: Tuple[Loss, ...]

    @property
    def verdict(self) -> str:
        sev = {loss.severity for loss in self.losses}
        if "blocker" in sev:
            return "reject"
        if "loss" in sev:
            return "degraded"
        if "edge" in sev:
            return "edge"
        return "pass"

    def counts(self) -> Dict[str, int]:
        out = {"blocker": 0, "loss": 0, "edge": 0, "info": 0}
        for loss in self.losses:
            out[loss.severity] += 1
        return out


# ---------------------------------------------------------------- loading


def _parameters_from(item: Any) -> Optional[Tuple[str, dict]]:
    if not isinstance(item, dict):
        return None
    if "function" in item and isinstance(item["function"], dict):
        fn = item["function"]
        name = fn.get("name")
        params = fn.get("parameters") or {}
        if isinstance(name, str):
            return name, params
        return None
    name = item.get("name")
    if isinstance(name, str):
        params = item.get("parameters") or item.get("input_schema") or {}
        return name, params
    return None


def load_tools(payload: Any) -> List[Tuple[str, dict]]:
    """Accept the three shapes people actually keep tool schemas in."""
    if isinstance(payload, dict) and isinstance(payload.get("tools"), list):
        payload = payload["tools"]
    if isinstance(payload, list):
        out = []
        for item in payload:
            parsed = _parameters_from(item)
            if parsed is None:
                raise ValueError("cannot find a tool name in entry: %r" % (item,))
            out.append(parsed)
        return out
    if isinstance(payload, dict):
        # bare {"tool_name": {json schema}}
        out = []
        for name, schema in payload.items():
            if not isinstance(schema, dict):
                raise ValueError("schema for %r is not an object" % name)
            out.append((name, schema))
        return out
    raise ValueError("unsupported tool file: expected a list or an object")


# ---------------------------------------------------------------- refs


def _resolve(root: Any, ref: str) -> Optional[Any]:
    if not ref.startswith("#"):
        return None
    node = root
    for raw in ref[1:].split("/"):
        if raw == "":
            continue
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            if not token.isdigit() or int(token) >= len(node):
                return None
            node = node[int(token)]
        elif isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        else:
            return None
    return node


def _inline(node: Any, root: Any, ptr: str, losses: List[Loss], stack: Tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_inline(v, root, "%s/%d" % (ptr, i), losses, stack) for i, v in enumerate(node)]
    if not isinstance(node, dict):
        return node

    out: Dict[str, Any] = {}
    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#"):
            losses.append(
                Loss(ptr, "$ref", "refused", "blocker", "non-local $ref %r cannot be inlined" % (ref,))
            )
            return {}
        if ref in stack:
            losses.append(
                Loss(
                    ptr,
                    "$ref",
                    "refused",
                    "blocker",
                    "recursive schema (%s): a target without $ref cannot express it" % ref,
                )
            )
            return {}
        target = _resolve(root, ref)
        if target is None:
            losses.append(Loss(ptr, "$ref", "refused", "blocker", "unresolvable $ref %r" % (ref,)))
            return {}
        base = _inline(copy.deepcopy(target), root, ptr, losses, stack + (ref,))
        if isinstance(base, dict):
            out.update(base)

    for key, value in node.items():
        if key == "$ref":
            continue
        if key in ("$defs", "definitions"):
            # Inlined into their use sites; carrying them along would just be
            # the same 400 under another name.
            continue
        out[key] = _inline(value, root, ptr + "/" + key, losses, stack)
    return out


# ---------------------------------------------------------------- allOf


def _is_object_branch(node: Any) -> bool:
    return isinstance(node, dict) and (
        node.get("type") == "object" or "properties" in node or "required" in node
    )


def _merge_all_of(node: Any, ptr: str, losses: List[Loss]) -> Any:
    if isinstance(node, list):
        return [_merge_all_of(v, "%s/%d" % (ptr, i), losses) for i, v in enumerate(node)]
    if not isinstance(node, dict):
        return node

    node = {k: _merge_all_of(v, ptr + "/" + k, losses) for k, v in node.items()}
    branches = node.get("allOf")
    if not isinstance(branches, list):
        return node

    if all(_is_object_branch(b) for b in branches):
        merged = {k: v for k, v in node.items() if k != "allOf"}
        props: Dict[str, Any] = dict(merged.get("properties") or {})
        required: List[str] = list(merged.get("required") or [])
        for branch in branches:
            for name, sub in (branch.get("properties") or {}).items():
                if name in props and props[name] != sub:
                    losses.append(
                        Loss(
                            ptr + "/properties/" + name,
                            "allOf",
                            "coerced",
                            "loss",
                            "two branches describe %r differently; the later one won" % name,
                        )
                    )
                props[name] = sub
            for name in branch.get("required") or []:
                if name not in required:
                    required.append(name)
        if props:
            merged["properties"] = props
            merged.setdefault("type", "object")
        if required:
            merged["required"] = required
        return merged

    # Non-object branches: keep the alternatives instead of guessing an order.
    flat: List[Any] = []
    for branch in branches:
        if isinstance(branch, dict) and isinstance(branch.get("anyOf"), list):
            flat.extend(branch["anyOf"])
        else:
            flat.append(branch)
    out = {k: v for k, v in node.items() if k != "allOf"}
    existing = out.get("anyOf")
    if isinstance(existing, list):
        existing = list(existing)
        existing.extend(flat)
        out["anyOf"] = existing
    else:
        out["anyOf"] = flat
    losses.append(
        Loss(
            ptr,
            "allOf",
            "coerced",
            "loss",
            "allOf over non-object branches rewritten as anyOf: the conjunction is gone",
        )
    )
    return out


# ---------------------------------------------------------------- transforms


def _nullable(value: Any) -> Any:
    if not isinstance(value, dict):
        return {"anyOf": [value, {"type": "null"}]}
    node = dict(value)
    kind = node.get("type")
    if isinstance(kind, str):
        if kind == "null":
            return node
        node["type"] = [kind, "null"]
        return node
    if isinstance(kind, list):
        if "null" in kind:
            return node
        node["type"] = list(kind) + ["null"]
        return node
    for union in ("anyOf", "oneOf"):
        if isinstance(node.get(union), list):
            node[union] = list(node[union]) + [{"type": "null"}]
            return node
    return {"anyOf": [value, {"type": "null"}]}


def _describe(pairs: List[Tuple[str, Any]]) -> str:
    """Prose that survives on the wire, split by what kind of meaning was lost."""
    value_rules = [
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    ]
    constrained = [
        "%s %s" % (keyword, json.dumps(value, ensure_ascii=False))
        for keyword, value in pairs
        if keyword in value_rules
    ]
    other = [
        "%s %s" % (keyword, json.dumps(value, ensure_ascii=False))
        for keyword, value in pairs
        if keyword not in value_rules
    ]
    sentences = []
    if constrained:
        sentences.append(
            "Values are constrained (%s); this endpoint does not enforce it, so validate in code."
            % "; ".join(constrained)
        )
    if other:
        sentences.append(
            "The original schema also declared %s, which this endpoint has no equivalent for."
            % "; ".join(other)
        )
    return " ".join(sentences)


def _walk(node: Any, ptr: str, losses: List[Loss], profile: Profile) -> Any:
    if isinstance(node, list):
        return [_walk(v, "%s/%d" % (ptr, i), losses, profile) for i, v in enumerate(node)]
    if not isinstance(node, dict):
        return node

    # Walk only the parts that are schemas. A parameter literally called
    # "format" or "maximum" is data, and must not be mistaken for a keyword.
    out: Dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            out[key] = {
                name: _walk(sub, ptr + "/properties/" + name, losses, profile)
                for name, sub in value.items()
            }
        elif key == "enum":
            out[key] = value
        elif key in ("const", "description", "title", "default", "examples", "$comment", "required"):
            out[key] = value
        elif isinstance(value, (dict, list)):
            out[key] = _walk(value, ptr + "/" + key, losses, profile)
        else:
            out[key] = value

    # const -> enum, because two providers in the test matrix don't verify const.
    if "const" in out:
        value = out.pop("const")
        if isinstance(out.get("enum"), list):
            out["enum"] = [value]
        else:
            out["enum"] = [value]
        losses.append(
            Loss(ptr, "const", "coerced", "loss", "const rewritten as a one-value enum")
        )

    if profile.string_enums_only and isinstance(out.get("enum"), list):
        values = out["enum"]
        if any(not isinstance(v, str) for v in values):
            out["enum"] = [v if isinstance(v, str) else json.dumps(v) for v in values]
            losses.append(
                Loss(
                    ptr,
                    "enum",
                    "coerced",
                    "loss",
                    "enum values sent as strings (%s); parse them back, the model will echo text"
                    % ", ".join(json.dumps(v, ensure_ascii=False) for v in values),
                )
            )

    moved: List[Tuple[str, Any]] = []
    for keyword in list(out.keys()):
        if keyword in profile.rejects:
            value = out.pop(keyword)
            moved.append((keyword, value))
            losses.append(
                Loss(
                    ptr,
                    keyword,
                    "removed",
                    "loss",
                    "rejected outright by this target; removed from the wire schema",
                )
            )
        elif keyword in profile.silent:
            value = out.pop(keyword)
            moved.append((keyword, value))
            losses.append(
                Loss(
                    ptr,
                    keyword,
                    "removed",
                    "loss",
                    "accepted by the API but not enforced; removed so the schema stops lying",
                )
            )
        elif keyword in profile.unverified:
            losses.append(
                Loss(
                    ptr,
                    keyword,
                    "unverified",
                    "edge",
                    "not documented either way for this target; kept, but untested",
                )
            )

    if moved:
        prose = _describe(moved)
        existing = out.get("description")
        if isinstance(existing, str) and existing.strip():
            if prose not in existing:
                out["description"] = existing.rstrip() + " " + prose
        else:
            out["description"] = prose

    # Strict-mode closure: every object closed, every property required, and
    # the optional ones made nullable so the meaning survives.
    is_object = out.get("type") == "object" or "properties" in out
    if profile.strict_object and is_object:
        props = out.get("properties") if isinstance(out.get("properties"), dict) else {}
        declared = list(out.get("required") or [])
        became_required = [name for name in props if name not in declared]
        for name in became_required:
            props[name] = _nullable(props[name])
        if props:
            out["properties"] = props
            out["required"] = declared + became_required
        if profile.accepts_additional_properties:
            was_open = out.get("additionalProperties")
            out["additionalProperties"] = False
            if was_open is True or isinstance(was_open, dict):
                losses.append(
                    Loss(
                        ptr,
                        "additionalProperties",
                        "coerced",
                        "loss",
                        "was open (%s); strict mode closes it, so callers can no longer send extra keys"
                        % json.dumps(was_open, ensure_ascii=False),
                    )
                )
        if became_required:
            losses.append(
                Loss(
                    ptr,
                    "required",
                    "closed",
                    "info",
                    "%d optional propert%s became required-and-nullable for strict mode: %s"
                    % (
                        len(became_required),
                        "y" if len(became_required) == 1 else "ies",
                        ", ".join(became_required),
                    ),
                )
            )

    return out


def _measure(node: Any, depth: int = 1) -> Tuple[int, int, int]:
    """(properties, max depth, largest union) for limit checks."""
    props = 0
    max_depth = depth
    max_union = 0
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            props += len(node["properties"])
            for sub in node["properties"].values():
                p, d, u = _measure(sub, depth + 1)
                props += p
                max_depth = max(max_depth, d)
                max_union = max(max_union, u)
        for key in ("items", "anyOf", "oneOf", "allOf", "$defs", "definitions"):
            value = node.get(key)
            if isinstance(value, list):
                max_union = max(max_union, len(value)) if key in ("anyOf", "oneOf") else max_union
                for sub in value:
                    p, d, u = _measure(sub, depth + 1)
                    props += p
                    max_depth = max(max_depth, d)
                    max_union = max(max_union, u)
            elif isinstance(value, dict):
                if key in ("$defs", "definitions"):
                    for sub in value.values():
                        p, d, u = _measure(sub, depth + 1)
                        props += p
                        max_depth = max(max_depth, d)
                        max_union = max(max_union, u)
                    continue
                p, d, u = _measure(value, depth + 1)
                props += p
                max_depth = max(max_depth, d)
                max_union = max(max_union, u)
    return props, max_depth, max_union


def compile_tool(name: str, schema: dict, profile: Profile) -> Compiled:
    losses: List[Loss] = []
    root = copy.deepcopy(schema)

    if profile.accepts_ref:
        working = root
    else:
        working = _inline(root, root, "", losses, ())

    working = _merge_all_of(working, "", losses)
    working = _walk(working, "", losses, profile)

    props, depth, union = _measure(working)
    if profile.max_props is not None and props > profile.max_props:
        losses.append(
            Loss(
                "",
                "properties",
                "limit",
                "blocker",
                "%d object properties, target allows %d" % (props, profile.max_props),
            )
        )
    if profile.max_depth is not None and depth > profile.max_depth:
        losses.append(
            Loss(
                "",
                "nesting",
                "limit",
                "blocker",
                "%d levels of nesting, target allows %d" % (depth, profile.max_depth),
            )
        )
    if profile.max_union is not None and union > profile.max_union:
        losses.append(
            Loss(
                "",
                "anyOf",
                "limit",
                "blocker",
                "%d union branches, target allows %d" % (union, profile.max_union),
            )
        )

    ordered = tuple(
        sorted(losses, key=lambda l: (SEVERITY_ORDER.get(l.severity, 9), l.pointer, l.keyword))
    )
    return Compiled(name=name, profile=profile.key, schema=working, losses=ordered)


def compile_all(
    tools: Sequence[Tuple[str, dict]], profiles: Iterable[Profile]
) -> List[Compiled]:
    profiles = list(profiles)
    out: List[Compiled] = []
    for name, schema in tools:
        for profile in profiles:
            out.append(compile_tool(name, schema, profile))
    return out
