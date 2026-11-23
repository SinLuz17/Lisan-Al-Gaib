"""diff 的核心判断。

三条设计决定（README 里展开）：
1. 变化按"老调用还成立吗"分级，不按 diff 行数分——
   breaking：老调用会失败或语义反转（参数没了、枚举值没了、required 变了、类型变了）；
   behavior：参数还能发，但工具会做不一样的事（description、默认值、readOnlyHint 变了）；
   cosmetic：只改了措辞里不影响语义的部分不管，description 一字未动就是没变。
2. description 的对比是"整段都算"，不做词级相似度——
   词级相似度会把"加了一句不要给 internal 用户开票"淹没在 95% 的相似里，而那一句
   恰恰是行为变化。宁可把所有 description 变化都列出来让人扫一眼。
3. 只报"有什么变了"，不猜"谁对谁错"——两个快照都是事实，判断归人。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .load import load_tools

BREAKING = "breaking"
BEHAVIOR = "behavior"
COSMETIC = "cosmetic"

_SEV_ORDER = {BREAKING: 0, BEHAVIOR: 1, COSMETIC: 2, None: 3}


class Change:
    def __init__(self, tool: str, kind: str, severity: str, detail: str):
        self.tool = tool
        self.kind = kind        # removed / added / param / required / enum / default / description / annotations
        self.severity = severity
        self.detail = detail

    def as_dict(self) -> Dict[str, str]:
        return {"tool": self.tool, "kind": self.kind,
                "severity": self.severity, "detail": self.detail}


def _walk_leaves(obj: Any, prefix: str = "") -> Dict[str, Any]:
    """把 schema 压平成 叶子路径 -> 值，数组下标不入路径（用 []）。"""
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_walk_leaves(v, "%s/%s" % (prefix, k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_walk_leaves(v, "%s/[]" % prefix))
    else:
        out[prefix or "/"] = obj
    return out


def _properties(schema: Dict[str, Any]) -> Dict[str, Any]:
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


def _required(schema: Dict[str, Any]) -> List[str]:
    req = schema.get("required")
    return sorted(req) if isinstance(req, list) else []


def _enum_of(prop: Any) -> Optional[List[Any]]:
    if not isinstance(prop, dict):
        return None
    e = prop.get("enum")
    return e if isinstance(e, list) else None


def diff_tool(old: Dict[str, Any], new: Dict[str, Any]) -> List[Change]:
    changes: List[Change] = []
    name = old["name"]

    old_desc, new_desc = old.get("description"), new.get("description")
    if old_desc != new_desc:
        changes.append(Change(name, "description", BEHAVIOR,
                              "description changed; the model re-reads it but an agent running "
                              "on old memories will not: %r -> %r" % (old_desc, new_desc)))

    old_ann, new_ann = old.get("annotations"), new.get("annotations")
    if old_ann != new_ann:
        ro = (old_ann or {}).get("readOnlyHint"), (new_ann or {}).get("readOnlyHint")
        extra = ""
        if ro[0] != ro[1]:
            extra = " (readOnlyHint %s -> %s: approval UI and agent-side guards both read this)" % ro
        changes.append(Change(name, "annotations", BEHAVIOR,
                              "annotations changed: %r -> %r%s" % (old_ann, new_ann, extra)))

    oschema, nschema = old["schema"], new["schema"]
    oprops, nprops = _properties(oschema), _properties(nschema)
    touched = set(set(oprops) ^ set(nprops))  # 增删过的参数，下面逐个再补 type/enum/default

    for pname in sorted(set(oprops) - set(nprops)):
        changes.append(Change(name, "param", BREAKING,
                              "parameter %s removed; old calls still sending it either break "
                              "or silently lose the constraint" % pname))
    for pname in sorted(set(nprops) - set(oprops)):
        changes.append(Change(name, "param", BREAKING,
                              "parameter %s added; replayed old calls omit it, and if it is "
                              "required the call now fails" % pname))

    oreq, nreq = set(_required(oschema)), set(_required(nschema))
    for pname in sorted(nreq - oreq):
        changes.append(Change(name, "required", BREAKING,
                              "parameter %s became required; previously optional calls "
                              "omitting it now fail" % pname))
    for pname in sorted(oreq - nreq):
        changes.append(Change(name, "required", BEHAVIOR,
                              "parameter %s is no longer required; omitting it now takes the "
                              "default behavior instead of an error" % pname))

    for pname in sorted(set(oprops) & set(nprops)):
        oprop, nprop = oprops[pname], nprops[pname]
        if oprop.get("type") != nprop.get("type"):
            touched.add(pname)
            changes.append(Change(name, "param", BREAKING,
                                  "parameter %s type changed %r -> %r" %
                                  (pname, oprop.get("type"), nprop.get("type"))))
        oenum, nenum = _enum_of(oprop), _enum_of(nprop)
        if oenum != nenum:
            touched.add(pname)
            gone = [v for v in (oenum or []) if v not in (nenum or [])]
            added = [v for v in (nenum or []) if v not in (oenum or [])]
            sev = BREAKING if gone else BEHAVIOR
            detail_bits = []
            if gone:
                detail_bits.append("values gone: %r — replayed calls carrying them will be rejected" % gone)
            if added:
                detail_bits.append("values added: %r" % added)
            changes.append(Change(name, "enum", sev,
                                  "enum of %s changed; %s" % (pname, "; ".join(detail_bits))))
        odef, ndef = oprop.get("default"), nprop.get("default")
        if odef != ndef:
            touched.add(pname)
            changes.append(Change(name, "default", BEHAVIOR,
                                  "default of %s changed %r -> %r; old calls omitting it now "
                                  "take a different value" % (pname, odef, ndef)))

    # 整棵 schema 的其他叶子变化（嵌套对象、items 等）——兜底列出来，人扫一眼。
    # 顶层参数里已经点名过的不再重复。
    known_kinds = {"param", "required", "enum", "default", "description", "annotations"}
    otree, ntree = _walk_leaves(oschema), _walk_leaves(nschema)
    stray = []
    for path in sorted(set(otree) | set(ntree)):
        if path in ("/properties", "/type", "/description", "/title") or \
                path.startswith("/required"):
            continue  # required 的变化上面单独报过，别再当嵌套变化凑数
        rest = path[len("/properties/"):] if path.startswith("/properties/") else None
        if rest is not None:
            top = rest.split("/", 1)[0]
            if top in touched:
                continue  # 这个参数上面已经单独说过了
        if otree.get(path) != ntree.get(path):
            stray.append(path)
    if stray:
        changes.append(Change(name, "schema", BEHAVIOR,
                              "nested schema changed at %s; worth a look even though the "
                              "top-level params are untouched" % ", ".join(stray[:5])))

    return changes


def diff_snapshots(old_data: Any, new_data: Any) -> Tuple[List[Change], List[str], List[str]]:
    """两个快照 → (changes, added_tools, removed_tools)，changes 已按严重度排序。"""
    old = load_tools(old_data)
    new = load_tools(new_data)

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changes: List[Change] = []
    for name in sorted(set(old) & set(new)):
        changes.extend(diff_tool(old[name], new[name]))
    changes.sort(key=lambda c: (_SEV_ORDER[c.severity], c.tool, c.kind))
    for t in removed:
        changes.append(Change(t, "removed", BREAKING,
                              "tool removed; agents holding it in memory will call a ghost"))
    for t in added:
        changes.append(Change(t, "added", COSMETIC, "tool added"))
    changes.sort(key=lambda c: (_SEV_ORDER[c.severity], c.tool, c.kind))
    return changes, added, removed
