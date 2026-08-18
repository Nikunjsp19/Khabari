"""ProGo math — stdlib only, no pandas/pandas_ta (those imports hang pytest here)."""

from app.progo import progo_from_bars, progo_score_delta

def test_progo_score_delta_confirms_longs_on_accumulation():
    delta, note = progo_score_delta("accumulation", pts=8)
    assert delta == 8
    assert note and "accumulation" in note.lower()

    delta, note = progo_score_delta("distribution", pts=8)
    assert delta == -8
    assert note and "distribution" in note.lower()

    assert progo_score_delta("mixed") == (0.0, None)
    assert progo_score_delta(None) == (0.0, None)


def _bars(n: int, *, gap: float, grind: float, start: float = 100.0):
    """n sessions that each gap `gap` then grind `grind` as fractions of prior close."""
    opens: list[float] = []
    closes: list[float] = []
    prev = start
    opens.append(start)
    closes.append(start)
    for _ in range(n):
        o = prev * (1.0 + gap)
        c = o + prev * grind
        opens.append(o)
        closes.append(c)
        prev = c
    return opens, closes


def test_progo_accumulation_when_pros_buy_intraday():
    opens, closes = _bars(20, gap=0.0, grind=0.01)
    out = progo_from_bars(opens, closes, length=14)
    assert out is not None
    assert out["regime"] == "accumulation"
    assert out["pro"] > 0
    assert abs(out["public"]) < 0.01
    assert out["pro_above_public"] is True


def test_progo_distribution_when_gaps_fade():
    opens, closes = _bars(20, gap=0.02, grind=-0.005)
    out = progo_from_bars(opens, closes, length=14)
    assert out is not None
    assert out["public"] > 0
    assert out["pro"] < 0
    assert out["regime"] == "distribution"


def test_progo_none_without_enough_bars():
    assert progo_from_bars([100.0] * 5, [101.0] * 5, length=14) is None
    assert progo_from_bars([100.0], [101.0], length=14) is None
    assert progo_from_bars([100.0] * 20, [101.0] * 19, length=14) is None


def test_progo_scale_invariant():
    cheap = progo_from_bars(*_bars(20, gap=0.002, grind=0.008, start=50.0), length=14)
    rich = progo_from_bars(*_bars(20, gap=0.002, grind=0.008, start=500.0), length=14)
    assert cheap is not None and rich is not None
    assert abs(cheap["pro"] - rich["pro"]) < 1e-6
    assert abs(cheap["public"] - rich["public"]) < 1e-6
