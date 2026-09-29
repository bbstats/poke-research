"""Guards for the honest backtest harness (pipeline/backtest.py).

* trades buy SKIP days after the signal, and returns use the entry price;
* signals at T never read a price from after T (leakage canary);
* backtest-safe features ignore the hand-pinned overrides, Wikipedia and the
  Kanto tilt, and only count sets released by T;
* carried (stale) set prices are dropped from snapshots;
* the backtest's gate mirrors the live site's build._edge.

Run: pytest tests/test_backtest_eval.py
"""
import json
import math
from datetime import date, timedelta

import pytest

from pipeline import backtest, build, fetch, popularity, signals

RARITIES = ["Common", "Uncommon", "Rare", "Double Rare", "Illustration Rare",
            "Special Illustration Rare"]


def _cards(n_per_rarity: int = 14) -> list:
    """Enough cards for every rarity cluster to get its own model."""
    names = ["Charizard", "Pikachu", "Bulbasaur", "Greninja", "Sunkern", "Umbreon", "Wooloo"]
    out = []
    for ri, rar in enumerate(RARITIES):
        for i in range(n_per_rarity):
            sid = "setA" if i % 2 == 0 else "setB"
            out.append({
                "id": f"{sid}-{ri}-{i}", "name": names[i % len(names)],
                "base_name": names[i % len(names)], "supertype": "Pokémon",
                "rarity": rar, "set_id": sid, "set_name": sid,
                "release_date": "2024-01-05" if sid == "setA" else "2024-03-01",
                "dex": 6 if i % len(names) == 0 else None,
            })
    return out


def _panel(cards: list, dates: list) -> dict:
    """Deterministic, card- and date-varying prices."""
    panel = {}
    for k, d in enumerate(dates):
        prices = {}
        for j, c in enumerate(cards):
            base = 1.0 + 3.0 * RARITIES.index(c["rarity"]) + (j % 7)
            prices[c["id"]] = round(base * (1.0 + 0.03 * math.sin(j + 0.7 * k)), 2)
        panel[d] = prices
    return panel


WEEKS = [date(2024, 6, 6) + timedelta(days=7 * i) for i in range(16)]


def test_skip_buys_after_the_signal():
    cards = _cards(2)
    panel = _panel(cards, WEEKS)
    T = WEEKS[4]
    obs = backtest.build_obs(panel, WEEKS, cards, [T], horizon=28, skip=7)
    E, X = WEEKS[5], WEEKS[9]
    for cid, fwd, *_ in obs[T]:
        if panel[T][cid] >= backtest.BASE_FLOOR:
            assert fwd == pytest.approx(panel[X][cid] / panel[E][cid] - 1.0, abs=0.05)

    obs0 = backtest.build_obs(panel, WEEKS, cards, [T], horizon=28, skip=0)
    cid, fwd0 = obs0[T][0][0], obs0[T][0][1]
    assert fwd0 == pytest.approx(panel[WEEKS[8]][cid] / panel[T][cid] - 1.0, abs=0.05)


def test_signals_at_T_ignore_future_prices(monkeypatch):
    cards = _cards()
    panel = _panel(cards, WEEKS)
    T = WEEKS[10]
    pull = {c["id"]: 0.0 for c in cards}
    before = backtest.signals_at(T, panel, WEEKS, cards, pull)

    future = {d: ({k: v * (3.0 if i % 2 else 0.2) for i, (k, v) in enumerate(p.items())}
                  if d > T else p) for d, p in panel.items()}
    after = backtest.signals_at(T, future, WEEKS, cards, pull)

    assert set(before) == set(after)
    for name in before:
        assert before[name] == pytest.approx(after[name]), f"{name} read a price after T"


def test_backtest_safe_prior_uses_poll_only():
    assert popularity.popularity_prior("Charizard")[0] == 10.0          # hand-pinned
    safe, src = popularity.popularity_prior("Charizard", backtest_safe=True)
    assert src == "poll" and safe == popularity.POLL_SCORE["Charizard"] and safe < 10.0
    # A wiki-only character has no prior in the safe path.
    wiki_only = next(n for n in popularity.WIKI_SCORE if n not in popularity.POLL_SCORE
                     and n not in popularity.USER_OVERRIDES)
    assert popularity.popularity_prior(wiki_only, backtest_safe=True) == (None, None)


def test_backtest_safe_skips_kanto_tilt():
    cards = [{"id": "a", "base_name": "Bulbasaur", "rarity": "Common", "set_id": "s", "dex": 1,
              "release_date": "2024-01-01"}]
    live = signals.char_premium_table(cards)["Bulbasaur"]
    safe = signals.char_premium_table(cards, backtest_safe=True)["Bulbasaur"]
    assert safe == pytest.approx(popularity.POLL_SCORE["Bulbasaur"])
    live_prior = popularity.popularity_prior("Bulbasaur")[0]
    assert live == pytest.approx(min(9.5, live_prior + 0.75))


def test_structural_counts_only_use_released_sets():
    base = [{"id": f"x{i}", "base_name": "Nobody", "rarity": "Common", "set_id": "old",
             "release_date": "2024-01-01"} for i in range(3)]
    base += [{"id": f"y{i}", "base_name": "Somebody", "rarity": "Common", "set_id": "old",
              "release_date": "2024-01-01"} for i in range(3)]
    later = [{"id": f"z{i}", "base_name": "Nobody", "rarity": "Hyper Rare", "set_id": "new",
              "release_date": "2025-06-01"} for i in range(5)]
    as_of = date(2024, 6, 1)
    assert (signals.char_premium_table(base, as_of=as_of)
            == signals.char_premium_table(base + later, as_of=as_of))


def test_stale_sets_are_dropped(tmp_path, monkeypatch):
    set_of = {f"a{i}": "A" for i in range(6)} | {f"b{i}": "B" for i in range(6)}
    day1 = {k: 1.0 + i for i, k in enumerate(set_of)}
    day2 = {k: (v if k.startswith("a") else v * 1.1) for k, v in day1.items()}  # A frozen
    for d, prices in (("2026-07-01", day1), ("2026-07-02", day2)):
        (tmp_path / f"snapshot-{d}.json").write_text(json.dumps(prices))
    monkeypatch.setattr(backtest, "SNAPSHOT_DIR", tmp_path)

    assert backtest.stale_sets(day2, day1, set_of) == {"A"}
    daily = backtest.load_daily_snapshots(set_of)
    assert set(daily[date(2026, 7, 2)]) == {f"b{i}" for i in range(6)}
    # As-of lookups fall back to the last fresh day for the frozen set.
    asof = backtest.price_as_of(daily, date(2026, 7, 2))
    assert asof["a0"] == day1["a0"] and asof["b0"] == day2["b0"]


def test_price_as_of_never_looks_forward():
    daily = {date(2026, 7, 1): {"c": 1.0}, date(2026, 7, 3): {"c": 5.0}}
    assert backtest.price_as_of(daily, date(2026, 7, 2)) == {"c": 1.0}


def test_grade_live_scores_published_calls(tmp_path, monkeypatch):
    cards = [{"id": f"c{i}", "rarity": "Rare", "set_id": "S", "release_date": "2025-01-01"}
             for i in range(12)]
    t0 = date(2026, 7, 1)
    # Residual r = -i/20: higher i = more undervalued; make those rise more.
    pred = {f"c{i}": {"m": 10.0, "e": 10.0, "r": -i / 20, "v": "fair"} for i in range(12)}
    (tmp_path / f"pred-{t0}.json").write_text(json.dumps(pred))
    for off in range(0, 40):
        d = t0 + timedelta(days=off)
        prices = {f"c{i}": round(10.0 * (1 + 0.004 * i * off) + 0.001 * off, 4) for i in range(12)}
        (tmp_path / f"snapshot-{d}.json").write_text(json.dumps(prices))
    monkeypatch.setattr(backtest, "SNAPSHOT_DIR", tmp_path)

    live = backtest.grade_live(cards, horizon=28, skip=7, step=7)
    assert live is not None and live["n_dates"] == 1
    assert live["hit_rates"]["n_under"] > 0 and live["hit_rates"]["under"] == 1.0


@pytest.mark.parametrize("age,price,resid", [
    (10, 50.0, 2.0),      # fresh: always shown
    (400, 50.0, 0.0),     # mature dead zone: hidden
    (400, 500.0, 3.0),    # mature chase premium: hidden
    (400, 500.0, 0.2),    # mature marquee, near estimate: shown
    (400, 5.0, -0.4),     # mature cheap: shown
])
def test_live_gate_matches_build_edge(age, price, resid):
    record = {"market_price": price, "expected_price": price / (1.0 + resid),
              "features": {"months_since_release": age / 30.4375}}
    assert backtest.live_gate(age, price, resid) == build._edge(record)[0]


def test_snapshot_leaves_out_carried_sets():
    cards = [{"id": "a1", "set_id": "A", "market_price": 1.0},
             {"id": "b1", "set_id": "B", "market_price": 2.0},
             {"id": "b2", "set_id": "B", "market_price": None}]
    assert fetch.snapshot_prices(cards, ["A"]) == {"b1": 2.0}
    assert fetch.snapshot_prices(cards, []) == {"a1": 1.0, "b1": 2.0}


def test_unreleased_characters_still_get_a_premium():
    cards = [{"id": "a", "base_name": "Nobody", "rarity": "Common", "set_id": "s",
              "release_date": "2024-01-01"},
             {"id": "b", "base_name": "Future", "rarity": "Common", "set_id": "t",
              "release_date": "2025-01-01"}]
    table = signals.char_premium_table(cards, as_of=date(2024, 6, 1))
    assert set(table) == {"Nobody", "Future"}
