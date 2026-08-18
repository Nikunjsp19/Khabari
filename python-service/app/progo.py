"""Larry Williams ProGo — professional vs public flow.

No pandas / pandas_ta. Safe to import from tests and the chase gate without
pulling the indicator stack (that import is what hung pytest in this repo).
"""

from __future__ import annotations

from typing import Any, Sequence


def progo_from_bars(
    opens: Sequence[float | None],
    closes: Sequence[float | None],
    length: int = 14,
) -> dict[str, Any] | None:
    """14-session (by default) ProGo from parallel open/close lists.

    * ``pro``    — mean of ``(Close - Open) / PrevClose`` as %. Intraday,
      i.e. what traded after the open.
    * ``public`` — mean of ``(Open - PrevClose) / PrevClose`` as %. The
      overnight gap, i.e. what the public paid on the open.

    Returns None when there are not enough valid consecutive bars.
    """
    length = max(2, int(length))
    if len(opens) != len(closes) or len(closes) < length + 1:
        return None

    pro_pts: list[float] = []
    public_pts: list[float] = []
    prev: float | None = None
    for o, c in zip(opens, closes):
        try:
            open_ = float(o) if o is not None else None
            close = float(c) if c is not None else None
        except (TypeError, ValueError):
            prev = None
            continue
        if open_ is None or close is None:
            prev = close if close is not None else prev
            continue
        if prev is not None and prev > 0:
            pro_pts.append((close - open_) / prev * 100.0)
            public_pts.append((open_ - prev) / prev * 100.0)
        prev = close

    if len(pro_pts) < length or len(public_pts) < length:
        return None

    pro = sum(pro_pts[-length:]) / length
    public = sum(public_pts[-length:]) / length

    if pro > public and pro > 0:
        regime = "accumulation"
    elif pro < public and pro < 0:
        regime = "distribution"
    else:
        regime = "mixed"

    return {
        "pro": round(pro, 4),
        "public": round(public, 4),
        "spread": round(pro - public, 4),
        "regime": regime,
        "pro_above_public": pro > public,
        "length": length,
        "bars": len(pro_pts),
    }


def progo_score_delta(
    regime: str | None, pts: float = 8.0
) -> tuple[float, str | None]:
    """Stock-side confirmation overlay (Williams / Rosputnia).

    Accumulation (pros buying the close) confirms a long. Distribution
    (public paying the gap, desks selling into it) is a reason not to buy.
    Missing/mixed regime is a no-op — fail-open.
    """
    if regime == "accumulation":
        return (
            float(pts),
            "ProGo accumulation — professionals buying the close, not the gap",
        )
    if regime == "distribution":
        return (
            -float(pts),
            "ProGo distribution — public paying the gap, desks selling into it",
        )
    return 0.0, None
