"""Walk-forward backtest: which signals actually predict a card's next-month price?

The site's falsifiable bet: a card flagged UNDERVALUED (market below model fair
value, ``residual_pct < 0``) should out-return one flagged OVERVALUED. This module
grades that bet, and grades simple baselines next to it on the same cards and
dates, so a signal only counts if it beats something naive.

Prices:
  * ``data/history/price-<date>.json``: weekly TCGplayer market prices from
    tcgcsv (``collectors.tcgcsv``), 2024-02 .. 2026-06-11. This is the IN-SAMPLE
    period: the model's features, the leaderboard gates and the Kanto tilt were
    all chosen after looking at it.
  * ``data/snapshots/snapshot-<date>.json``: the daily prices the site fetched
    (pokemontcg.io / TCGdex, same TCGplayer market price) from 2026-06-17. Resampled
    to the same weekly grid, they are the OUT-OF-SAMPLE period (``OOS_START``).
    A set whose prices are all unchanged from the previous day is treated as
    missing that day: fetch.py used to carry a failed set's cached prices into
    the snapshot as if they were fresh.
  * ``data/snapshots/pred-<date>.json``: what the site actually published each
    day. ``grade_live`` scores those calls forward (the live track record).

Method (no lookahead):
  * At each signal date T, refit the model on T's prices only, with backtest-safe
    features (2020 poll only: no hand-pinned overrides, no 2025-26 Wikipedia
    views, no Kanto tilt; structural counts from sets released by then).
  * Buy at T + ``SKIP_DAYS``, sell ``horizon`` days later. The skip matters:
    buying at T means the same noisy price sits in both the signal and the
    return, which shows up as fake mean-reversion "edge" (run with skip=0 to see).
  * Score with per-date Spearman rank IC, averaged, with a Newey-West t-stat for
    the overlapping windows. Also a within-set IC (ranks cards against their own
    set) and top/bottom-quintile excess return.

Fresh-release results are also reported per set release: weekly cuts in a
release's first five weeks are not independent, so the honest sample size for
"fresh cards fall" is the number of releases (about a dozen), not weeks.

Writes ``docs/data/backtest.json`` for the Track Record page.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import config, model, signals, pullrates

HISTORY_DIR = config.DATA / "history"
SNAPSHOT_DIR = config.SNAPSHOTS
OUT_PATH = config.SITE_DATA / "backtest.json"

BAND = 0.15                  # |residual| below this = "fair" (not graded for hit-rate)
HORIZONS = [7, 28, 56, 84]   # days; 28 is the headline
PRIMARY_HORIZON = 28
SKIP_DAYS = 7                # buy one week after the signal (kills bid/ask-style noise)
PAIR_TOL_DAYS = 3            # how far a panel date may sit from its target and still count
STEP_DAYS = 7                # weekly panel spacing (drives the HAC lag)
BASE_FLOOR = 2.0             # minimum price to enter the universe at all
MATURE_AGE = 90              # days since release to count a card as "mature"
FRESH_AGE = 35               # days since release to count a card as a "fresh release"
MATURE_FLOOR = 10.0          # price floor for the "mature & liquid" cut
OOS_START = date(2026, 6, 12)  # first day after the history every rule was tuned on
STALE_MIN_CARDS = 5          # a set needs this many overlapping cards to be judged stale
MIN_CUT = 8                  # minimum cards in a cut to compute a rank IC
MIN_QUINTILE_CUT = 25        # minimum cards in a cut for quintile returns

Obs = Dict[date, List[tuple]]   # T -> [(cid, fwd_return, age_days, price_T, rarity_group, set_id)]
Signals = Dict[date, Dict[str, Dict[str, float]]]  # T -> signal name -> {cid: score}


# ---------------------------------------------------------------------------
# Panel loading
# ---------------------------------------------------------------------------
def _read_prices(path) -> Dict[str, float]:
    raw = json.loads(path.read_text())
    return {k: float(v) for k, v in raw.items() if v is not None and float(v) > 0}


def load_panel() -> Dict[date, Dict[str, float]]:
    """Load ``data/history/price-<date>.json`` -> ``{date: {card_id: price}}``."""
    panel: Dict[date, Dict[str, float]] = {}
    if not HISTORY_DIR.exists():
        return panel
    for f in sorted(HISTORY_DIR.glob("price-*.json")):
        try:
            d = date.fromisoformat(f.stem.replace("price-", ""))
            prices = _read_prices(f)
        except (ValueError, OSError):  # pragma: no cover
            continue
        if prices:
            panel[d] = prices
    return panel


def stale_sets(today: Dict[str, float], prev: Dict[str, float],
               set_of: Dict[str, str]) -> set:
    """Sets whose every overlapping card has the exact same price as ``prev``.

    Real TCGplayer market prices move daily on at least a few cards of any set, so
    a fully frozen set means the fetch failed and cached prices were carried.
    """
    total: Dict[str, int] = {}
    same: Dict[str, int] = {}
    for cid, p in today.items():
        sid = set_of.get(cid)
        if sid is None or cid not in prev:
            continue
        total[sid] = total.get(sid, 0) + 1
        same[sid] = same.get(sid, 0) + (prev[cid] == p)
    return {s for s, n in total.items() if n >= STALE_MIN_CARDS and same[s] == n}


def load_daily_snapshots(set_of: Dict[str, str]) -> Dict[date, Dict[str, float]]:
    """Daily ``snapshot-<date>.json`` prices with stale (carried) sets removed."""
    out: Dict[date, Dict[str, float]] = {}
    prev: Optional[Dict[str, float]] = None
    for f in sorted(SNAPSHOT_DIR.glob("snapshot-*.json")):
        try:
            d = date.fromisoformat(f.stem.replace("snapshot-", ""))
            prices = _read_prices(f)
        except (ValueError, OSError):  # pragma: no cover
            continue
        if prev is not None:
            dead = stale_sets(prices, prev, set_of)
            prev = prices
            prices = {k: v for k, v in prices.items() if set_of.get(k) not in dead}
        else:
            prev = prices
        if prices:
            out[d] = prices
    return out


def price_as_of(daily: Dict[date, Dict[str, float]], d: date,
                tol: int = PAIR_TOL_DAYS) -> Dict[str, float]:
    """Each card's most recent daily price on or before ``d`` (up to ``tol`` days back).

    Backward-only, so a price "as of d" never uses a later day.
    """
    out: Dict[str, float] = {}
    for back in range(tol + 1):
        for cid, p in daily.get(d - timedelta(days=back), {}).items():
            out.setdefault(cid, p)
    return out


def weekly_from_daily(daily: Dict[date, Dict[str, float]], start: date,
                      step: int = STEP_DAYS) -> Dict[date, Dict[str, float]]:
    """Resample daily prices onto a weekly grid starting at ``start``."""
    if not daily:
        return {}
    last = max(daily)
    out: Dict[date, Dict[str, float]] = {}
    d = start
    while d <= last:
        prices = price_as_of(daily, d)
        if prices:
            out[d] = prices
        d += timedelta(days=step)
    return out


def load_full_panel(cards: List[dict]) -> Tuple[Dict[date, Dict[str, float]], Optional[date]]:
    """History panel + snapshot prices continued on the same weekly grid.

    Returns ``(panel, last_history_date)``.
    """
    panel = load_panel()
    last_hist = max(panel) if panel else None
    set_of = {c["id"]: c["set_id"] for c in cards}
    daily = load_daily_snapshots(set_of)
    if last_hist is not None and daily:
        for d, prices in weekly_from_daily(daily, last_hist + timedelta(days=STEP_DAYS)).items():
            panel.setdefault(d, prices)
    return panel, last_hist


# ---------------------------------------------------------------------------
# As-of-T model refit
# ---------------------------------------------------------------------------
def _pull_costs(cards: List[dict]) -> Dict[str, float]:
    """Price-independent retail pull cost per card (stable across dates)."""
    counts = pullrates.rarity_counts(cards)
    set_by_id = {s["set_id"]: s for s in json.loads((config.NORMALIZED / "sets.json").read_text())}
    out: Dict[str, float] = {}
    for c in cards:
        sr = set_by_id.get(c["set_id"], {})
        pack = sr.get("retail_pack_price") or sr.get("pack_price") or 0.0
        pc = pullrates.pull_cost(c["rarity"], c["set_id"], counts, pack)
        out[c["id"]] = pc if pc != float("inf") else 0.0
    return out


def residuals_at(T: date, prices_T: Dict[str, float], cards: List[dict],
                 pull_costs: Dict[str, float], backtest_safe: bool = True) -> Dict[str, float]:
    """Refit the model on T's prices and return ``{card_id: residual_pct}``.

    A copy of each priced card carries its T-date market price; features are
    recomputed as-of T. ``backtest_safe=False`` reproduces the live site's
    features (hand-pinned overrides, Wikipedia, Kanto tilt), which leak.
    """
    cards_T: List[dict] = []
    for c in cards:
        p = prices_T.get(c["id"])
        if p is None or p <= 0:
            continue
        cards_T.append({**c, "market_price": p})
    if len(cards_T) < config.MIN_CLUSTER_SIZE:
        return {}
    feats = signals.compute_features(cards_T, today=T, backtest_safe=backtest_safe)
    signals.inject_pull_cost(feats, {c["id"]: pull_costs.get(c["id"], 0.0) for c in cards_T})
    result = model.fit(cards_T, feats)
    out: Dict[str, float] = {}
    for c in cards_T:
        pred = result.get(c["id"])
        if pred is not None and pred.residual_pct is not None:
            out[c["id"]] = float(pred.residual_pct)
    return out


# ---------------------------------------------------------------------------
# Stats helpers (numpy only)
# ---------------------------------------------------------------------------
def _rankdata(a: np.ndarray) -> np.ndarray:
    """Average-rank (ties shared) — for Spearman."""
    a = np.asarray(a, dtype=float)
    n = len(a)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(n, dtype=float)
    sorted_a = a[order]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        ranks[order[i:j + 1]] = 0.5 * (i + j) + 1.0  # 1-based average rank
        i = j + 1
    return ranks


def _spearman(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < MIN_CUT:
        return None
    rx, ry = _rankdata(x), _rankdata(y)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def _nw_tstat(ics: Sequence[float], lag: int) -> Optional[float]:
    """Newey-West (HAC) t-stat for the mean of an autocorrelated IC series.

    Weekly evaluation dates with a multi-week forward horizon produce OVERLAPPING
    windows, so the per-date ICs are autocorrelated and the naive
    ``mean/(std/sqrt(n))`` t overstates significance ~2x. Bartlett-weighted HAC
    long-run variance with ``lag`` ≈ horizon/step corrects it.
    """
    x = np.asarray(ics, dtype=float)
    n = len(x)
    if n < 5:
        return None
    d = x - x.mean()
    var = float((d * d).mean())                       # gamma_0
    for l in range(1, min(lag, n - 1) + 1):
        w = 1.0 - l / (lag + 1.0)
        var += 2.0 * w * float((d[l:] * d[:-l]).mean())  # Bartlett-weighted autocov
    if var <= 0:
        return None
    se = math.sqrt(var / n)
    return float(x.mean() / se) if se > 0 else None


def _series_stats(ics: Sequence[float], lag: int) -> Optional[dict]:
    """Mean of a per-date IC series + naive/HAC t + sign count.

    The HAC t is withheld (None) when the series is shorter than 2x(lag+1): with
    a handful of overlapping windows the long-run variance estimate is garbage
    and produces absurd t-stats. ``n_independent`` is the number of
    non-overlapping windows the dates span.
    """
    if len(ics) < 5:
        return None
    arr = np.asarray(ics)
    mean = float(arr.mean())
    std = float(arr.std())
    naive_t = float(mean / (std / math.sqrt(len(ics)))) if std > 0 else None
    nw = _nw_tstat(ics, lag) if len(ics) >= 2 * (lag + 1) else None
    pos = int((arr > 0).sum())
    return {
        "ic": round(mean, 4),
        "naive_t": round(naive_t, 2) if naive_t is not None else None,
        "nw_t": round(nw, 2) if nw is not None else None,
        "n_dates": len(ics),
        "n_independent": round(len(ics) / max(1, lag - 1), 1),
        "sign_pos": pos,
        "sign_z": round(float((pos - len(ics) / 2) / (math.sqrt(len(ics)) / 2)), 2),
    }


def _ic_stats(per_date: List[Tuple[Sequence[float], Sequence[float]]],
              lag: int) -> Optional[dict]:
    """Per-date Spearman ICs -> {ic mean, naive_t, nw_t, sign test, n_dates}."""
    return _series_stats([ic for (m, r) in per_date if (ic := _spearman(m, r)) is not None], lag)


def _winsorize(x: np.ndarray, lo: float = 1.0, hi: float = 99.0) -> np.ndarray:
    if len(x) < 20:
        return x
    a, b = np.percentile(x, [lo, hi])
    return np.clip(x, a, b)


# ---------------------------------------------------------------------------
# Dates, ages, groups
# ---------------------------------------------------------------------------
def _nearest_forward(dates: List[date], T: date, horizon: int) -> Optional[date]:
    """Panel date closest to ``T + horizon`` (within PAIR_TOL_DAYS), strictly after T."""
    target = T + timedelta(days=horizon)
    best, best_gap = None, PAIR_TOL_DAYS + 1
    for d in dates:
        if d <= T:
            continue
        gap = abs((d - target).days)
        if gap < best_gap:
            best, best_gap = d, gap
    return best


def _nearest(dates: List[date], target: date) -> Optional[date]:
    best, best_gap = None, PAIR_TOL_DAYS + 1
    for d in dates:
        gap = abs((d - target).days)
        if gap < best_gap:
            best, best_gap = d, gap
    return best


def _rarity_group(rarity: str) -> str:
    r = (rarity or "").lower()
    if "hyper" in r:
        return "Hyper Rare"
    if "special illustration" in r:
        return "Special Illustration Rare"
    if "illustration" in r:
        return "Illustration Rare"
    if "ultra" in r or "mega" in r:
        return "Ultra/Mega Rare"
    if "double" in r:
        return "Double Rare"
    if r in ("common", "uncommon", "rare"):
        return "Common/Uncommon/Rare"
    return "Other"


def _price_tier(p: float) -> str:
    if p < 5:
        return "$2-5"
    if p < 20:
        return "$5-20"
    if p < 100:
        return "$20-100"
    return "$100+"


def _age_days(card: Optional[dict], T: date) -> Optional[int]:
    """Days between a card's release and evaluation date T (None if unknown)."""
    rel = (card or {}).get("release_date")
    if not rel:
        return None
    try:
        return (T - date.fromisoformat(rel[:10])).days
    except ValueError:
        return None


def _hac_lag(horizon: int, skip: int = 0) -> int:
    """HAC lag for overlapping windows: ~ how many weekly dates a window spans."""
    return max(1, math.ceil((horizon + skip) / STEP_DAYS) + 1)


def live_gate(age: Optional[int], price: float, residual: Optional[float]) -> bool:
    """True when the live site would publish an over/under call (mirrors build._edge).

    Fresh cards always pass; mature cards are hidden in the [GATE_DEAD_LO,
    GATE_DEAD_HI) price band, or when market > CHASE_PREMIUM_MULT x the estimate.
    A card with no release date counts as fresh, as build._edge does (its
    months_since_release reads 0).
    """
    if age is None or age <= config.GATE_FRESH_AGE_DAYS:
        return True
    if config.GATE_DEAD_LO <= price < config.GATE_DEAD_HI:
        return False
    if residual is not None and (1.0 + residual) > config.CHASE_PREMIUM_MULT:
        return False
    return True


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
SIGNAL_LABELS = {
    "model": "Model residual (backtest-safe features)",
    "model_legacy": "Model residual (live-site features: overrides, Wikipedia, Kanto)",
    "cheap_in_cluster": "Cheapest in its rarity group (log price vs group mean)",
    "mom_4w": "4-week momentum",
    "mom_8w": "8-week momentum",
    "age": "Days since release (older = higher)",
}


def signals_at(T: date, panel: Dict[date, Dict[str, float]], dates: List[date],
               cards: List[dict], pull_costs: Dict[str, float],
               legacy: bool = True) -> Dict[str, Dict[str, float]]:
    """Every signal's score per card at T, using prices on or before T only.

    Convention: HIGHER score = expected to do BETTER, so a positive IC means the
    signal ranks future winners correctly.
    """
    card_meta = {c["id"]: c for c in cards}
    pT = panel[T]
    out: Dict[str, Dict[str, float]] = {}
    out["model"] = {cid: -r for cid, r in residuals_at(T, pT, cards, pull_costs, True).items()}
    if legacy:
        out["model_legacy"] = {cid: -r for cid, r in
                               residuals_at(T, pT, cards, pull_costs, False).items()}

    logs: Dict[str, List[float]] = {}
    for cid, p in pT.items():
        c = card_meta.get(cid)
        if c is not None:
            logs.setdefault(model._cluster_key(c), []).append(math.log(p))
    mean_log = {k: sum(v) / len(v) for k, v in logs.items()}
    out["cheap_in_cluster"] = {
        cid: -(math.log(p) - mean_log[model._cluster_key(card_meta[cid])])
        for cid, p in pT.items() if cid in card_meta}

    for name, weeks in (("mom_4w", 4), ("mom_8w", 8)):
        back = _nearest([d for d in dates if d < T], T - timedelta(days=7 * weeks))
        pB = panel.get(back, {}) if back else {}
        out[name] = {cid: p / pB[cid] - 1.0 for cid, p in pT.items() if pB.get(cid)}

    out["age"] = {cid: float(a) for cid in pT
                  if (a := _age_days(card_meta.get(cid), T)) is not None}
    return out


def build_obs(panel: Dict[date, Dict[str, float]], dates: List[date], cards: List[dict],
              signal_dates: Sequence[date], horizon: int, skip: int) -> Obs:
    """Rows per signal date T: buy at T+skip, sell at T+skip+horizon.

    Forward returns are winsorized at the 1st/99th percentile within each cut
    (variant fallbacks around presale produce a few 3x jumps).
    """
    card_meta = {c["id"]: c for c in cards}
    obs: Obs = {}
    for T in signal_dates:
        E = T if skip == 0 else _nearest_forward(dates, T, skip)
        if E is None:
            continue
        X = _nearest_forward(dates, E, horizon)
        if X is None:
            continue
        pT, pE, pX = panel[T], panel[E], panel[X]
        rows = []
        for cid, a in pT.items():
            e, x = pE.get(cid), pX.get(cid)
            if e is None or x is None or a < BASE_FLOOR:
                continue
            c = card_meta.get(cid, {})
            rows.append([cid, x / e - 1.0, _age_days(c, T), a,
                         _rarity_group(c.get("rarity", "")), c.get("set_id")])
        if not rows:
            continue
        fr = _winsorize(np.asarray([r[1] for r in rows]))
        for r, f in zip(rows, fr):
            r[1] = float(f)
        obs[T] = [tuple(r) for r in rows]
    return obs


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
Pred = Callable[[tuple], bool]

SEGMENTS: Dict[str, Tuple[str, Pred]] = {
    "all": ("All cards $2+", lambda r: True),
    "fresh": (f"Fresh (0-{FRESH_AGE}d)", lambda r: r[2] is not None and 0 <= r[2] <= FRESH_AGE),
    "mid_age": (f"{FRESH_AGE + 1}-{MATURE_AGE}d", lambda r: r[2] is not None and FRESH_AGE < r[2] <= MATURE_AGE),
    "mature_cheap": (f"Mature >{MATURE_AGE}d, $2-10", lambda r: r[2] is not None and r[2] > MATURE_AGE and r[3] < MATURE_FLOOR),
    "mature_liquid": (f"Mature >{MATURE_AGE}d, $10+", lambda r: r[2] is not None and r[2] > MATURE_AGE and r[3] >= MATURE_FLOOR),
    "mature_mid": (f"Mature >{MATURE_AGE}d, $10-100", lambda r: r[2] is not None and r[2] > MATURE_AGE and MATURE_FLOOR <= r[3] < 100),
    "mature_high": (f"Mature >{MATURE_AGE}d, $100+", lambda r: r[2] is not None and r[2] > MATURE_AGE and r[3] >= 100),
}


def score(obs: Obs, sig: Dict[date, Dict[str, float]], seg: Pred, lag: int,
          dates: Optional[Sequence[date]] = None) -> Optional[dict]:
    """IC stats of one signal over a segment of rows.

    ``sig`` is ``{T: {card_id: score}}``. Adds a within-set IC (ranks cards only
    against their own set, then averages across sets weighted by size) and the
    top/bottom quintile's mean forward return minus the cut's mean return.
    """
    ics, within, top, bot = [], [], [], []
    for T in (dates if dates is not None else sorted(obs)):
        rows, s = obs.get(T), sig.get(T)
        if not rows or not s:
            continue
        sel = [(s[r[0]], r[1], r[5]) for r in rows if r[0] in s and seg(r)]
        if len(sel) < MIN_CUT:
            continue
        x = np.asarray([a for a, _, _ in sel])
        y = np.asarray([b for _, b, _ in sel])
        ic = _spearman(x, y)
        if ic is None:
            continue
        ics.append(ic)
        by_set: Dict[str, list] = {}
        for a, b, sid in sel:
            by_set.setdefault(sid, []).append((a, b))
        num = den = 0.0
        for rs in by_set.values():
            wic = _spearman([a for a, _ in rs], [b for _, b in rs])
            if wic is not None:
                num += wic * len(rs)
                den += len(rs)
        if den:
            within.append(num / den)
        if len(sel) >= MIN_QUINTILE_CUT:
            order = np.argsort(x)
            k = max(1, len(order) // 5)
            top.append(float(y[order[-k:]].mean() - y.mean()))
            bot.append(float(y[order[:k]].mean() - y.mean()))
    st = _series_stats(ics, lag)
    if st is None:
        return None
    ws = _series_stats(within, lag)
    st["ic_within_set"] = ws["ic"] if ws else None
    st["nw_t_within_set"] = ws["nw_t"] if ws else None
    st["top_q_excess"] = round(float(np.mean(top)), 4) if top else None
    st["bot_q_excess"] = round(float(np.mean(bot)), 4) if bot else None
    return st


def fresh_by_release(obs: Obs, sigs: Signals, cards: List[dict],
                     dates: Sequence[date]) -> List[dict]:
    """One row per set release: its first FRESH_AGE days pooled into one result."""
    rel_of: Dict[str, str] = {}
    for c in cards:
        rel_of.setdefault(c["set_id"], c.get("release_date") or "")
    names = {c["set_id"]: c.get("set_name") or c["set_id"] for c in cards}
    out = []
    for sid, rel in sorted(rel_of.items(), key=lambda kv: kv[1]):
        model_ics, cheap_ics, rets = [], [], []
        for T in dates:
            rows = [r for r in obs.get(T, ()) if r[5] == sid and r[2] is not None
                    and 0 <= r[2] <= FRESH_AGE]
            if len(rows) < MIN_CUT:
                continue
            rets.extend(r[1] for r in rows)
            for name, bucket in (("model", model_ics), ("cheap_in_cluster", cheap_ics)):
                s = sigs[T].get(name, {})
                sel = [(s[r[0]], r[1]) for r in rows if r[0] in s]
                ic = _spearman([a for a, _ in sel], [b for _, b in sel])
                if ic is not None:
                    bucket.append(ic)
        if not rets:
            continue
        out.append({
            "set_id": sid, "set_name": names[sid], "release_date": rel,
            "n_cuts": len(model_ics),
            "model_ic": round(float(np.mean(model_ics)), 4) if model_ics else None,
            "cheap_ic": round(float(np.mean(cheap_ics)), 4) if cheap_ics else None,
            "median_return": round(float(np.median(rets)), 4),
            "n_obs": len(rets),
        })
    return out


def _release_summary(rows: List[dict]) -> dict:
    ics = [r["model_ic"] for r in rows if r["model_ic"] is not None]
    return {
        "n_releases": len(rows),
        "model_ic_positive": sum(1 for v in ics if v > 0),
        "median_return_negative": sum(1 for r in rows if r["median_return"] < 0),
        "mean_model_ic": round(float(np.mean(ics)), 4) if ics else None,
    }


# ---------------------------------------------------------------------------
# Live track record: grade what the site actually published
# ---------------------------------------------------------------------------
def grade_live(cards: List[dict], horizon: int = PRIMARY_HORIZON, skip: int = SKIP_DAYS,
               band: float = BAND, step: int = STEP_DAYS) -> Optional[dict]:
    """Score ``pred-<date>.json`` (the published residuals) against later snapshots.

    Uses one pred file per ``step`` days, buys at date+skip and sells at
    date+skip+horizon, both from stale-filtered daily snapshots.
    """
    card_meta = {c["id"]: c for c in cards}
    set_of = {c["id"]: c["set_id"] for c in cards}
    daily = load_daily_snapshots(set_of)
    preds = {}
    for f in sorted(SNAPSHOT_DIR.glob("pred-*.json")):
        try:
            preds[date.fromisoformat(f.stem.replace("pred-", ""))] = f
        except ValueError:  # pragma: no cover
            continue
    if not preds or not daily:
        return None
    last_snap = max(daily)
    grid, T = [], min(preds)
    while T + timedelta(days=skip + horizon) <= last_snap:
        near = _nearest(sorted(preds), T)
        if near is not None:
            grid.append(near)
        T += timedelta(days=step)

    obs: Obs = {}
    sig: Dict[date, Dict[str, float]] = {}
    verdicts: Dict[date, Dict[str, str]] = {}
    for T in grid:
        P = json.loads(preds[T].read_text())
        pE = price_as_of(daily, T + timedelta(days=skip))
        pX = price_as_of(daily, T + timedelta(days=skip + horizon))
        rows, s, v = [], {}, {}
        for cid, rec in P.items():
            m, r = rec.get("m"), rec.get("r")
            e, x = pE.get(cid), pX.get(cid)
            if m is None or r is None or m < BASE_FLOOR or not e or not x:
                continue
            c = card_meta.get(cid, {})
            rows.append([cid, x / e - 1.0, _age_days(c, T), m,
                         _rarity_group(c.get("rarity", "")), c.get("set_id")])
            s[cid] = -r
            v[cid] = "under" if r < -band else "over" if r > band else "fair"
        if len(rows) < MIN_CUT:
            continue
        fr = _winsorize(np.asarray([r[1] for r in rows]))
        for r, f in zip(rows, fr):
            r[1] = float(f)
        obs[T] = [tuple(r) for r in rows]
        sig[T] = s
        verdicts[T] = v
    if not obs:
        return None

    lag = _hac_lag(horizon, skip)
    segs = {k: score(obs, sig, pred, lag) for k, (_, pred) in SEGMENTS.items()}
    hu = hun = ho = hon = bu = bn = 0
    for T, rows in obs.items():
        for r in rows:
            bn += 1
            bu += r[1] > 0
            if verdicts[T][r[0]] == "under":
                hun += 1
                hu += r[1] > 0
            elif verdicts[T][r[0]] == "over":
                hon += 1
                ho += r[1] < 0
    ds = sorted(obs)
    return {
        "first_date": ds[0].isoformat(), "last_date": ds[-1].isoformat(),
        "n_dates": len(ds), "horizon_days": horizon, "skip_days": skip,
        "segments": segs,
        "hit_rates": {"under": round(hu / hun, 4) if hun else None,
                      "over": round(ho / hon, 4) if hon else None,
                      "base_up": round(bu / bn, 4) if bn else None,
                      "n_under": hun, "n_over": hon},
        "note": "The calls the site actually published (live-site features), graded "
                "against later daily prices. Few, overlapping weeks: indicative only.",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(horizons: Sequence[int] = HORIZONS, band: float = BAND, skip: int = SKIP_DAYS,
        write: bool = True) -> dict:
    cards = json.loads((config.NORMALIZED / "cards.json").read_text())
    panel, last_hist = load_full_panel(cards)
    dates = sorted(panel)
    if last_hist is None or len(dates) < 3:
        raise SystemExit(f"need >=3 history dates to backtest (have {len(dates)}). "
                         "Run: python -m collectors.tcgcsv")
    pull_costs = _pull_costs(cards)

    # 1) every signal at every date (the expensive step: two model refits per date).
    sigs: Signals = {T: signals_at(T, panel, dates, cards, pull_costs) for T in dates}
    by_name = lambda name: {T: s.get(name, {}) for T, s in sigs.items()}

    def split(obs: Obs, H: int, sk: int) -> Tuple[List[date], List[date]]:
        """Signal dates whose whole trade sits in-sample vs after OOS_START."""
        ins = [T for T in obs if T + timedelta(days=sk + H) <= last_hist + timedelta(days=PAIR_TOL_DAYS)]
        oos = [T for T in obs if T >= OOS_START]
        return sorted(ins), sorted(oos)

    lag = _hac_lag(PRIMARY_HORIZON, skip)
    primary = build_obs(panel, dates, cards, dates, PRIMARY_HORIZON, skip)
    ins, oos = split(primary, PRIMARY_HORIZON, skip)

    # --- signal x segment x period: the baseline table ------------------------
    baselines = []
    for name, label in SIGNAL_LABELS.items():
        sig = by_name(name)
        for seg_key, (seg_label, pred) in SEGMENTS.items():
            baselines.append({
                "signal": name, "segment": seg_key,
                "in_sample": score(primary, sig, pred, lag, ins),
                "oos": score(primary, sig, pred, lag, oos),
            })

    # --- what the old headline was made of: noise + leaky features ------------
    raw = build_obs(panel, dates, cards, dates, PRIMARY_HORIZON, 0)
    ins0, _ = split(raw, PRIMARY_HORIZON, 0)
    lag0 = _hac_lag(PRIMARY_HORIZON, 0)
    bridge = []
    for feat_name, feat_label in (("model_legacy", "live-site features"),
                                  ("model", "backtest-safe features")):
        for sk, ob, ds, lg in ((0, raw, ins0, lag0), (skip, primary, ins, lag)):
            row = {"features": feat_label, "skip_days": sk}
            for seg_key in ("all", "fresh", "mature_liquid"):
                st = score(ob, by_name(feat_name), SEGMENTS[seg_key][1], lg, ds)
                row[seg_key] = st and {"ic": st["ic"], "nw_t": st["nw_t"]}
            bridge.append(row)

    # --- fresh releases, one row per set ---------------------------------------
    releases_in = fresh_by_release(primary, sigs, cards, ins)
    releases_oos = fresh_by_release(primary, sigs, cards, oos)

    # --- the live gate, judged only after OOS_START -----------------------------
    model_sig = by_name("model")
    gate_check = {"ungated": score(primary, model_sig, lambda r: True, lag, oos)}
    gated_obs: Obs = {}
    for T in oos:
        s = model_sig.get(T, {})
        gated_obs[T] = [r for r in primary[T]
                        if live_gate(r[2], r[3], (-s[r[0]]) if r[0] in s else None)]
    gate_check["gated"] = score(gated_obs, model_sig, lambda r: True, lag, oos)
    gate_check["note"] = ("Model IC after OOS_START with and without the live site's "
                          "leaderboard gates (dead zone + chase premium), which were tuned "
                          "on the in-sample history.")

    # --- headline cuts (honest settings, in-sample; out-of-sample alongside) ---
    fresh_rows = [r for T in ins for r in primary[T] if r[2] is not None and 0 <= r[2] <= FRESH_AGE]
    fresh_mean_ret = round(float(np.mean([r[1] for r in fresh_rows])), 4) if fresh_rows else None

    def cut(seg_key: str, note: str, extra: Optional[dict] = None) -> Optional[dict]:
        st = score(primary, model_sig, SEGMENTS[seg_key][1], lag, ins)
        if st is None:
            return None
        st = {**st, **(extra or {}), "note": note}
        st["oos"] = score(primary, model_sig, SEGMENTS[seg_key][1], lag, oos)
        return st

    headline = {
        "fresh_release": cut("fresh",
            f"cards <={FRESH_AGE}d old. About a dozen set releases drive this, so the weekly "
            f"t-stat overstates confidence; see the per-release table",
            {"mean_fwd_return": fresh_mean_ret}),
        "all_cards": cut("all", "floor $2, all ages"),
        "mature_liquid": cut("mature_liquid", f"cards >{MATURE_AGE}d old & >=${MATURE_FLOOR:.0f}"),
    }

    # --- floor / age / rarity / price / horizon breakdowns (model, in-sample) ---
    floor_sensitivity = []
    for f in (2.0, 5.0, 10.0, 20.0):
        st = score(primary, model_sig, lambda r, f=f: r[3] >= f, lag, ins)
        if st:
            floor_sensitivity.append({"floor": f, "ic": st["ic"], "nw_t": st["nw_t"],
                                      "n_dates": st["n_dates"]})

    flat = [r for T in ins for r in primary[T]]
    by_age = []
    for label, lo, hi in (("0-35d (fresh)", 0, 35), ("36-90d", 36, 90),
                          ("91-180d", 91, 180), ("180d+ (mature)", 181, 10 ** 9)):
        st = score(primary, model_sig, lambda r, lo=lo, hi=hi: r[2] is not None and lo <= r[2] <= hi, lag, ins)
        rows = [r for r in flat if r[2] is not None and lo <= r[2] <= hi]
        if st and rows:
            by_age.append({"bucket": label, "ic": st["ic"], "nw_t": st["nw_t"],
                           "mean_return": round(float(np.mean([r[1] for r in rows])), 4),
                           "n_obs": len(rows)})

    def bucket_rows(idx_fn) -> List[dict]:
        groups: Dict[str, List[tuple]] = {}
        for T in ins:
            s = model_sig.get(T, {})
            for r in primary[T]:
                if r[0] in s:
                    groups.setdefault(idx_fn(r), []).append((s[r[0]], r[1]))
        out_rows = []
        for key, rs in groups.items():
            if len(rs) < 30:
                continue
            ic = _spearman([a for a, _ in rs], [b for _, b in rs])
            out_rows.append({"key": key, "n": len(rs),
                             "ic": round(ic, 4) if ic is not None else None,
                             "frac_up": round(sum(1 for _, b in rs if b > 0) / len(rs), 4),
                             "mean_return": round(float(np.mean([b for _, b in rs])), 4)})
        return sorted(out_rows, key=lambda x: -x["n"])

    by_rarity = bucket_rows(lambda r: r[4])
    by_price = bucket_rows(lambda r: _price_tier(r[3]))

    top_legs, bot_legs = [], []
    hu = hun = ho = hon = bu = bn = 0
    for T in ins:
        s = model_sig.get(T, {})
        rows = [(s[r[0]], r[1]) for r in primary[T] if r[0] in s]
        for m, fr in rows:
            bn += 1
            bu += fr > 0
            if -m < -band:
                hun += 1
                hu += fr > 0
            elif -m > band:
                hon += 1
                ho += fr < 0
        if len(rows) < 20:
            continue
        m = np.asarray([a for a, _ in rows])
        fr = np.asarray([b for _, b in rows])
        order = np.argsort(m)
        k = max(1, len(order) // 10)
        top_legs.append(float(fr[order[-k:]].mean()))
        bot_legs.append(float(fr[order[:k]].mean()))
    decile = None
    if top_legs:
        t, b = float(np.mean(top_legs)), float(np.mean(bot_legs))
        decile = {"top_undervalued_return": round(t, 4),
                  "bottom_overvalued_return": round(b, 4),
                  "spread": round(t - b, 4), "n_dates": len(top_legs)}
    hit_rates = {"under": round(hu / hun, 4) if hun else None,
                 "over": round(ho / hon, 4) if hon else None,
                 "base_up": round(bu / bn, 4) if bn else None,
                 "n_under": hun, "n_over": hon}

    by_horizon = []
    for H in horizons:
        o = build_obs(panel, dates, cards, dates, H, skip)
        i_H, o_H = split(o, H, skip)
        lg = _hac_lag(H, skip)
        row = {"horizon_days": H}
        for seg_key, col in (("all", "all"), ("mature_liquid", "mature")):
            si = score(o, model_sig, SEGMENTS[seg_key][1], lg, i_H)
            so = score(o, model_sig, SEGMENTS[seg_key][1], lg, o_H)
            row[f"ic_{col}"] = si["ic"] if si else None
            row[f"nw_t_{col}"] = si["nw_t"] if si else None
            row[f"oos_ic_{col}"] = so["ic"] if so else None
        by_horizon.append(row)

    ic_ts = []
    for T in sorted(primary):
        s = model_sig.get(T, {})
        rows = [(s[r[0]], r[1]) for r in primary[T] if r[0] in s]
        ic = _spearman([a for a, _ in rows], [b for _, b in rows])
        if ic is not None:
            ic_ts.append({"date": T.isoformat(), "ic": round(ic, 4), "n": len(rows),
                          "oos": T >= OOS_START})

    out = {
        "generated_for": dates[-1].isoformat(),
        "panel": {
            "first_date": dates[0].isoformat(), "last_date": dates[-1].isoformat(),
            "last_history_date": last_hist.isoformat(), "oos_start": OOS_START.isoformat(),
            "n_dates": len(dates), "n_in_sample": len(ins), "n_oos": len(oos),
            "step_days": STEP_DAYS, "primary_horizon_days": PRIMARY_HORIZON,
            "skip_days": skip, "base_floor": BASE_FLOOR, "verdict_band": band,
            "features": "backtest-safe (2020 poll only; no overrides, Wikipedia or Kanto tilt)",
        },
        "signals": SIGNAL_LABELS,
        "segments": {k: v[0] for k, v in SEGMENTS.items()},
        "headline": headline,
        "baselines": baselines,
        "bridge": bridge,
        "fresh_by_release": {"in_sample": releases_in, "oos": releases_oos,
                             "summary_in_sample": _release_summary(releases_in),
                             "summary_oos": _release_summary(releases_oos)},
        "gate_check_oos": gate_check,
        "live": grade_live(cards, PRIMARY_HORIZON, skip, band),
        "hit_rates": hit_rates,
        "decile": decile,
        "floor_sensitivity": floor_sensitivity,
        "by_age_bucket": by_age,
        "by_horizon": by_horizon,
        "by_rarity": by_rarity,
        "by_price_tier": by_price,
        "ic_timeseries": ic_ts,
        "caveats": [
            f"Trades buy {skip} days after the signal. Buying on the signal date "
            "lets price noise pose as edge (see the bridge table: skip 0 vs 7).",
            "In-sample means the history the model, gates and features were tuned on "
            f"(to {last_hist.isoformat()}). Out-of-sample is later, from the site's own "
            "daily prices, and is only a few overlapping months.",
            "Out-of-sample prices come from a different feed (pokemontcg.io / TCGdex "
            "vs tcgcsv); both report TCGplayer market price and agree within ~15% on "
            "about 93% of cards at the splice. Momentum lookbacks in the first weeks "
            "after the splice mix the two.",
            "Fresh-release results rest on about a dozen set releases; weekly cuts inside "
            "one release are not independent.",
            "Significance uses Newey-West (HAC) t-stats for the overlapping windows.",
            "Universe is products that still exist on TCGplayer today (mild survivorship), "
            "and the historical price variant was picked from today's card data.",
        ],
    }
    if write:
        OUT_PATH.write_text(json.dumps(out, indent=2, allow_nan=False))
    return out


def _fmt(st: Optional[dict]) -> str:
    if not st:
        return "      —      "
    t = st["nw_t"]
    w = st.get("ic_within_set")
    w = f"{w:+.3f}" if w is not None else "  —   "
    return (f"{st['ic']:+.3f} (t{t if t is not None else '—':>5}) {st['sign_pos']:>3}/{st['n_dates']:<3} "
            f"in-set {w}")


if __name__ == "__main__":
    s = run()
    p = s["panel"]
    print(f"Backtest {p['first_date']}..{p['last_date']}: {p['n_in_sample']} in-sample + "
          f"{p['n_oos']} out-of-sample weekly cuts (OOS from {p['oos_start']}), "
          f"horizon {p['primary_horizon_days']}d, buy +{p['skip_days']}d, HAC t\n")

    print("What the old headline was made of (model IC, in-sample):")
    for r in s["bridge"]:
        print(f"  {r['features']:24s} skip {r['skip_days']}:  " + "  ".join(
            f"{k} {r[k]['ic']:+.3f}" if r[k] else f"{k} —" for k in ("all", "fresh", "mature_liquid")))

    print("\nSignal x segment  IC (HAC t) positive/cuts, within-set IC:   in-sample  |  out-of-sample")
    for seg in SEGMENTS:
        print(f"  [{s['segments'][seg]}]")
        for row in (b for b in s["baselines"] if b["segment"] == seg):
            print(f"    {row['signal']:17s} {_fmt(row['in_sample'])} | {_fmt(row['oos'])}")

    for per in ("in_sample", "oos"):
        sm = s["fresh_by_release"][f"summary_{per}"]
        print(f"\nFresh releases ({per}): {sm['n_releases']} releases, model IC>0 in "
              f"{sm['model_ic_positive']}, median return <0 in {sm['median_return_negative']}")
        for r in s["fresh_by_release"][per]:
            mic = f"{r['model_ic']:+.3f}" if r["model_ic"] is not None else "  —  "
            cic = f"{r['cheap_ic']:+.3f}" if r["cheap_ic"] is not None else "  —  "
            print(f"    {r['set_name'][:22]:22s} {r['release_date']}  model {mic}  cheap {cic}  "
                  f"median ret {r['median_return']:+.1%}  ({r['n_cuts']} cuts)")

    g = s["gate_check_oos"]
    print(f"\nGate check (OOS): ungated {_fmt(g.get('ungated'))}  gated {_fmt(g.get('gated'))}")
    lv = s["live"]
    if lv:
        print(f"\nLive published calls {lv['first_date']}..{lv['last_date']} ({lv['n_dates']} cuts):")
        for k, st in lv["segments"].items():
            print(f"    {k:14s} {_fmt(st)}")
        hr = lv["hit_rates"]
        print(f"    hit: under rose {hr['under']}  over fell {hr['over']}  (base up {hr['base_up']})")
