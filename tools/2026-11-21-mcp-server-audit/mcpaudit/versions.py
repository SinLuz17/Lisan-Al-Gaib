"""混合历法的版本比较：日期式（2026.11.18）与语义式（2.3.0）逐段数字比。

两种历法之间不做跨制式排序（没有可比意义），比较结果约定为：
- 同制式（都是全数字段）→ 真比较；
- 不同制式 → 返回 None，调用方应按"无法判断"处理，不硬猜。

预发布后缀（alpha/beta/rc/dev）排在同数字段的正式版之前。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

_SUFFIX = re.compile(r"[.\-](alpha|beta|rc|dev|pre|a|b)([0-9]*)$", re.IGNORECASE)
_NUM = re.compile(r"^[0-9]+$")


def _parse(ver: str) -> Optional[Tuple[Tuple[int, ...], Optional[str], int]]:
    ver = (ver or "").strip().lstrip("vV")
    if not ver:
        return None
    suf: Optional[str] = None
    suf_n = 0
    m = _SUFFIX.search(ver)
    if m:
        suf = m.group(1).lower()
        suf_n = int(m.group(2) or 0)
        ver = ver[: m.start()]
    if not all(_NUM.match(x) for x in ver.split(".")):
        return None
    return tuple(int(x) for x in ver.split(".")), suf, suf_n


def _same_calendar(a: Tuple[int, ...], b: Tuple[int, ...]) -> bool:
    """日期式（首段是四位年份，>=100）与语义式（首段 <100）不同制式：
    各自内部可比，跨制式硬比大小没有意义，返回 False。
    """
    return (a[0] >= 100) == (b[0] >= 100)


def compare(a: str, b: str) -> Optional[int]:
    """返回 -1/0/1；不同制式或解析不了返回 None。"""
    pa, pb = _parse(a), _parse(b)
    if pa is None or pb is None:
        return None
    (va, sa, na), (vb, sb, nb) = pa, pb
    if not _same_calendar(va, vb):
        return None
    n = max(len(va), len(vb))
    va += (0,) * (n - len(va))
    vb += (0,) * (n - len(vb))
    if va != vb:
        return -1 if va < vb else 1
    # 数字段相同：预发布 < 正式
    if sa == sb:
        return -1 if na < nb else (1 if na > nb else 0)
    if sa is None:
        return 1
    if sb is None:
        return -1
    order = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "rc": 3}
    oa, ob = order.get(sa, 4), order.get(sb, 4)
    return -1 if oa < ob else (1 if oa > ob else 0)


def affected(version: Optional[str], spec: str) -> Optional[bool]:
    """版本是否命中 spec。spec 形如 "<2.4.0"、"<=1.31.0"、"==0.23.1"、
    ">=0.21.6,<0.23.2"（逗号连接的区间，闭开自定）。无法判断返回 None。
    """
    if version is None:
        return None
    version = version.strip()
    ok_any = True  # 所有子条件都命中才算
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("<="):
            base, op = part[2:], "le"
        elif part.startswith(">="):
            base, op = part[2:], "ge"
        elif part.startswith("<"):
            base, op = part[1:], "lt"
        elif part.startswith(">"):
            base, op = part[1:], "gt"
        elif part.startswith("=="):
            base, op = part[2:], "eq"
        else:
            base, op = part, "eq"
        c = compare(version, base)
        if c is None:
            return None
        hit = {
            "lt": c < 0, "le": c <= 0, "gt": c > 0,
            "ge": c >= 0, "eq": c == 0,
        }[op]
        if not hit:
            ok_any = False
            break
    return ok_any
