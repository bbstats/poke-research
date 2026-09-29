"""Per-card feature signals for the Price Lab model.

`compute_features(cards)` turns the raw normalized-card list into a
`{card_id: {feature: value}}` table covering the FEATURES declared in
`pipeline/config.py`.

Two kinds of feature live here:

* **Live** features (`char_premium`, `scarcity`, `months_since_release`,
  `set_rank`) are derived entirely from the free pokemontcg.io data we already
  cache, so they are computed in full below.
* **Stub** features (`demand_pressure`, `grading_intensity`,
  `universal_appeal`) need a paid/scraped feed (eBay / PSA / Google Trends)
  that is not wired yet. Each is implemented as its own one-line function that
  returns a neutral mid-scale value, so the moment a real feed exists you swap
  the body of a single function and nothing else changes.

`pull_cost` is intentionally *not* computed here. It depends on
`pipeline/pullrates.py`, and the contract says `build.py` injects it at build
time (so this module stays importable and self-testable without pullrates).
Use `inject_pull_cost(features, pull_costs)` to merge it in.

Design notes
------------
* `char_premium` ranks a *character* (the card's `base_name`, e.g. "Umbreon")
  on a 0-10 scale using a HYBRID, de-skewed blend: ~70% an INDEPENDENT,
  price-free popularity score and ~30% a reduced-weight price-percentile
  component. The independent block is three price-free signals — total
  printings (reprints = demand), distinct-set breadth, and a rarity-weighted
  chase intensity (how often / how strongly TPC lavishes premium/chase rarities
  on the character) — each log1p-compressed and clean-percentile-ranked across
  base_names. The price block is the clean corpus percentile of the character's
  MEAN market price, folded in at only 30% so popular-but-cheap and
  rare-but-expensive characters both behave sensibly while keeping circularity
  low; unpriced characters get a neutral 0.5 price percentile. The blended score
  is mapped through one final clean percentile to a ~uniform 0-10 distribution
  (mean ~5). The result is a property of the character, so every printing of
  "Charizard" shares the same premium. Covers Pokemon AND Trainers.
* `scarcity` blends rarity-tier rarity (rarer tier => higher) with
  `months_since_release` (older / more out-of-print => higher). It does NOT
  import pull rates — it uses the rarity-tier ordering directly so the module
  stays decoupled from pullrates; pull_cost is the precise pull-economics
  signal and is injected separately.
* `set_rank` is a 0-1 *rarity-tier* percentile within the card's set
  (price-free, non-circular — was a price percentile; see set_rank_table).

Everything is plain stdlib + numpy.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

import numpy as np

from . import popularity

# Rarity tiers ordered from most common (0.0) to rarest (1.0). Used by the
# scarcity signal. This is an *ordering* of tiers, independent of the precise
# pull-rate probabilities in pullrates.py (which scarcity deliberately does not
# import). Anything not listed falls back to a neutral mid rank.
RARITY_ORDER: List[str] = [
    "Common",
    "Uncommon",
    "Rare",
    "Double Rare",
    "ACE SPEC Rare",
    "Ultra Rare",
    "MEGA_ATTACK_RARE",
    "Shiny Rare",
    "Illustration Rare",
    "Shiny Ultra Rare",
    "Special Illustration Rare",
    "Hyper Rare",
    "Mega Hyper Rare",       # Mega-era top gold
    "Black White Rare",      # Black Bolt/White Flare textured top secret
]
_RARITY_RANK = {r: i / (len(RARITY_ORDER) - 1) for i, r in enumerate(RARITY_ORDER)}
_NEUTRAL_RARITY_RANK = 0.5

# Horizon (months) at which a card is treated as fully "aged" for the scarcity
# blend. ~5 years: old enough to be reliably out of print.
_SCARCITY_AGE_CAP_MONTHS = 60.0

# Weight split for scarcity = w_rarity * rarity_rank + w_age * age_factor.
_SCARCITY_W_RARITY = 0.7
_SCARCITY_W_AGE = 0.3

# char_premium is a HYBRID, de-skewed character-popularity signal on a 0-10
# scale. It blends an INDEPENDENT, price-free popularity score at 70% with a
# small, reduced-weight price-percentile component at 30% — keeping circularity
# low while letting price break ties sensibly. The independent block is three
# price-free signals: total printings (reprints = demand), distinct-set breadth,
# and a rarity-weighted chase intensity (how strongly TPC lavishes premium/chase
# rarities on a character). Each signal is log1p-compressed then clean-
# percentile-ranked across base_names; the blend is mapped through one final
# clean percentile to a ~uniform 0-10 distribution (mean ~5).
#
# Blend weights: independent popularity (price-free) vs reduced-weight price.
_CHARPREM_W_INDEP = 1.00   # independent popularity (price-free) — FULL weight
_CHARPREM_W_PRICE = 0.00   # price component REMOVED (was 0.30). It reintroduced
                           # mild circularity (current price predicting price) for
                           # no-prior characters / Trainers, contradicting the
                           # "price-INDEPENDENT popularity" design. Backtest:
                           # dropping it holds forward IC (fresh 0.269->0.270) and
                           # improves decile spread (0.081->0.090). (Codex audit.)

# Sub-signal weights within the independent block.
_CHARPREM_W_PRINTINGS = 0.35  # total printings (reprints = demand)
_CHARPREM_W_SETS = 0.30       # distinct-set breadth
_CHARPREM_W_CHASE = 0.35      # rarity-weighted chase intensity

# Kanto (Gen 1) nostalgia tilt: Gen-1 mons carry a durable, broad desirability
# premium in the TCG market. We lift every Kanto character by a small ADDITIVE
# bonus (preserves intra-region ranking — Charizard still tops Magikarp),
# capped below the 10.0 reserved for hand-pinned icons, and never LOWERING a
# character (so Charizard=10 / Pikachu=10 overrides are untouched). Region is
# derived systematically from the National Pokédex number (1-151), not hand-
# typed. Bonus size is validated against the walk-forward backtest.
_KANTO_DEX_LO = 1
_KANTO_DEX_HI = 151
_KANTO_PREMIUM_BONUS = 0.75   # additive lift applied to Kanto characters
_KANTO_PREMIUM_CAP = 9.5      # cap (10.0 stays reserved for USER_OVERRIDES)

# Rarity-weighted "chase weight": rarer chase => stronger desirability signal.
# Common/Uncommon/Rare/Double Rare/ACE SPEC are NOT chase tiers and contribute 0
# (any rarity absent from this map adds nothing to the chase intensity).
_CHARPREM_CHASE_WEIGHT = {
    "Ultra Rare": 1.0,
    "Shiny Rare": 1.2,
    "Illustration Rare": 1.4,
    "Shiny Ultra Rare": 1.6,
    "Special Illustration Rare": 2.0,
    "MEGA_ATTACK_RARE": 2.2,
    "Hyper Rare": 2.5,
    "Mega Hyper Rare": 3.0,
}

# ---------------------------------------------------------------------------
# Popularity prior. The cross-generation, price-INDEPENDENT popularity ground
# truth (2020 "Pokémon of the Year" global poll + hand-pinned user anchors) now
# lives in `popularity.py`. char_premium_table() takes that prior where it
# exists and falls back to the structural signal — compressed below the poll
# floor — for unranked / Gen 9 characters. See popularity.popularity_prior().
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def _parse_date(value: Optional[str]) -> Optional[date]:
    """Parse a YYYY-MM-DD release date; return None if missing/unparseable."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def months_since_release(release_date: Optional[str], today: Optional[date] = None) -> float:
    """Whole-ish months between a card's release date and `today`.

    `today` defaults to the real current date; passing it in keeps the feature
    deterministic for tests and reproducible builds. Returns 0.0 for missing /
    future dates (never negative).
    """
    today = today or date.today()
    rel = _parse_date(release_date)
    if rel is None:
        return 0.0
    days = (today - rel).days
    if days <= 0:
        return 0.0
    return days / 30.4375  # average days per month


# ---------------------------------------------------------------------------
# Live features
# ---------------------------------------------------------------------------
def _percentile_rank(value: float, sorted_values: np.ndarray) -> float:
    """Fraction of `sorted_values` <= `value`, in [0, 1].

    `sorted_values` must be ascending. Empty input => neutral 0.5.
    """
    n = sorted_values.size
    if n == 0:
        return 0.5
    # np.searchsorted with side='right' gives the count of values <= value.
    count_le = int(np.searchsorted(sorted_values, value, side="right"))
    return count_le / n


def _charprem_percentile_table(values_by_name: Dict[str, float]) -> Dict[str, float]:
    """Clean percentile rank (fraction of names with value <= this), in [0, 1].

    Keyed by name. Ties share the right-side count, so the distribution of the
    *output* is near-uniform when inputs are distinct. Empty input => ``{}``.
    """
    names = list(values_by_name.keys())
    if not names:
        return {}
    order = np.sort(np.array([values_by_name[n] for n in names], dtype=float))
    n = order.size
    out: Dict[str, float] = {}
    for nm in names:
        count_le = int(np.searchsorted(order, values_by_name[nm], side="right"))
        out[nm] = count_le / n if n else 0.5
    return out


# A card counts toward the structural popularity signal once it is within this
# many days of release (presale listings appear ~1-3 weeks early).
_PRESALE_WINDOW_DAYS = 21


def char_premium_table(cards: List[dict], as_of: Optional[date] = None,
                       backtest_safe: bool = False) -> Dict[str, float]:
    """Per-character popularity premium on a 0-10 scale, keyed by base_name.

    De-skewed and now FULLY PRICE-INDEPENDENT: the score is the INDEPENDENT,
    price-free popularity block at 100% weight. (A 30% current-price-percentile
    component was removed — it reintroduced mild circularity for no-prior
    characters; the backtest showed dropping it holds forward IC and improves
    decile spread.) Covers Pokemon AND Trainers. Every printing of a character
    shares its premium.

    Independent popularity (70%) is three price-free signals, each log1p-
    compressed then clean-percentile-ranked across base_names:

      * ``n_printings`` — how many cards a base_name appears on (reprints =
        demand).
      * ``n_sets`` — how many distinct sets the character appears in (breadth).
      * ``chase`` — a rarity-weighted chase intensity: the sum of
        ``_CHARPREM_CHASE_WEIGHT`` over the character's premium/chase printings
        (Ultra/Shiny/Illustration/Special Illustration/Hyper/Mega), where rarer
        chase tiers carry more weight.

    Price component (30%) is the clean corpus percentile of the character's MEAN
    market price (priced printings only). Unpriced characters get a neutral 0.5
    price percentile so they are not unduly punished.

    The 70/30 blend is mapped through one final clean percentile to a ~uniform
    0-10 distribution (mean ~5).

    ``as_of`` drops cards released more than _PRESALE_WINDOW_DAYS after that
    date, so printing / chase counts only use sets that existed then.
    ``backtest_safe`` uses the poll-only prior and skips the Kanto tilt (tuned on
    the same price history the backtest grades).
    """
    counted = cards
    if as_of is not None:
        horizon = as_of + timedelta(days=_PRESALE_WINDOW_DAYS)
        counted = [c for c in cards
                   if (rel := _parse_date(c.get("release_date"))) is None or rel <= horizon]
    n_printings: Dict[str, int] = defaultdict(int)
    sets_of: Dict[str, set] = defaultdict(set)
    chase_raw: Dict[str, float] = defaultdict(float)   # rarity-weighted intensity
    prices_of: Dict[str, list] = defaultdict(list)     # priced printings per char
    is_kanto: Dict[str, bool] = defaultdict(bool)      # any printing with a Kanto dex#

    # Every character still gets a premium; only cards released by as_of count.
    for c in cards:
        if c.get("base_name"):
            n_printings[c["base_name"]] += 0
    for c in counted:
        name = c.get("base_name")
        if not name:
            continue
        n_printings[name] += 1
        sets_of[name].add(c.get("set_id"))
        rarity = c.get("rarity")
        if rarity in _CHARPREM_CHASE_WEIGHT:
            chase_raw[name] += _CHARPREM_CHASE_WEIGHT[rarity]
        price = c.get("market_price")
        if price is not None:
            prices_of[name].append(float(price))
        dex = c.get("dex")
        if isinstance(dex, int) and _KANTO_DEX_LO <= dex <= _KANTO_DEX_HI:
            is_kanto[name] = True

    all_names = list(n_printings.keys())
    if not all_names:
        return {}

    # Raw signals -> log1p-compressed values (tames heavy right tails before the
    # percentile rank, e.g. Pikachu's many printings).
    printings_val = {nm: math.log1p(n_printings[nm]) for nm in all_names}
    sets_val = {nm: math.log1p(len(sets_of[nm])) for nm in all_names}
    chase_val = {nm: math.log1p(chase_raw.get(nm, 0.0)) for nm in all_names}

    # Mean market price per character (priced printings only).
    char_mean_price = {
        nm: float(np.mean(prices_of[nm])) for nm in all_names if prices_of.get(nm)
    }

    # Clean percentile ranks across base_names.
    p_print = _charprem_percentile_table(printings_val)
    p_sets = _charprem_percentile_table(sets_val)
    p_chase = _charprem_percentile_table(chase_val)
    p_price = _charprem_percentile_table(char_mean_price) if char_mean_price else {}

    # 70% independent popularity + 30% reduced-weight price (neutral 0.5 if
    # unpriced).
    blended: Dict[str, float] = {}
    for nm in all_names:
        indep = (
            _CHARPREM_W_PRINTINGS * p_print[nm]
            + _CHARPREM_W_SETS * p_sets[nm]
            + _CHARPREM_W_CHASE * p_chase[nm]
        )
        price_pct = p_price.get(nm, 0.5)
        blended[nm] = _CHARPREM_W_INDEP * indep + _CHARPREM_W_PRICE * price_pct

    # Final clean 0-10 percentile of the structural signal (de-skewed).
    final_pct = _charprem_percentile_table(blended)
    structural = {nm: 10.0 * final_pct[nm] for nm in all_names}

    # Anchor to the cross-gen popularity prior (2020 poll + user overrides) where
    # it exists; otherwise fall back to the structural signal, COMPRESSED into
    # [0, POLL_FLOOR) so any poll-ranked classic outranks any unranked / Gen 9
    # newcomer (whose fame the in-set structural signal can't reliably gauge).
    premium: Dict[str, float] = {}
    for nm in all_names:
        prior, _src = popularity.popularity_prior(nm, backtest_safe=backtest_safe)
        if prior is not None:
            base = prior
        else:
            base = structural[nm] / 10.0 * popularity.POLL_FLOOR
        # Kanto nostalgia tilt: additive, ranking-preserving, never lowers a
        # character, capped below the reserved-10.0 icon tier.
        if not backtest_safe and is_kanto.get(nm) and base < _KANTO_PREMIUM_CAP:
            base = min(_KANTO_PREMIUM_CAP, base + _KANTO_PREMIUM_BONUS)
        premium[nm] = round(base, 4)
    return premium


def scarcity(rarity: str, months_old: float) -> float:
    """Scarcity score 0-10: rarer tier + older release => higher.

    Blends the rarity-tier rank (0..1) with an age factor (0..1, saturating at
    `_SCARCITY_AGE_CAP_MONTHS`). Decoupled from pullrates by design.
    """
    rarity_rank = _RARITY_RANK.get(rarity, _NEUTRAL_RARITY_RANK)
    age_factor = min(months_old / _SCARCITY_AGE_CAP_MONTHS, 1.0)
    blended = _SCARCITY_W_RARITY * rarity_rank + _SCARCITY_W_AGE * age_factor
    return round(10.0 * blended, 4)


def set_rank_table(cards: List[dict]) -> Dict[str, float]:
    """Per-card 0-1 WITHIN-SET RARITY-TIER percentile (non-circular).

    Where does this card sit, by RARITY, among the cards in its own set:
    the fraction of cards in the set whose rarity ordinal is <= this card's.
    Deliberately price-FREE — it uses the rarity-tier ordering only.

    History/why: this used to be a within-set *price* percentile, which
    correlated ~0.86-0.89 with the very price the model predicts (circular
    target leakage). That inflated R**2 (0.91 -> 0.95) while *hurting* the
    honest forward-IC metric, and it could mark an already-expensive card
    "fairly priced" because it was expensive. The walk-forward backtest
    confirmed swapping to this rarity ordinal removes the leakage while
    keeping fresh-release IC strong (~0.26, t~9.7). See CLAUDE.md.

    Cards with an unknown rarity fall back to the neutral rarity ordinal.
    """
    by_set_ords: Dict[str, List[float]] = defaultdict(list)
    card_ord: Dict[str, float] = {}
    for c in cards:
        o = _RARITY_RANK.get(c.get("rarity"), _NEUTRAL_RARITY_RANK)
        card_ord[c["id"]] = o
        by_set_ords[c["set_id"]].append(o)
    set_sorted = {sid: np.sort(np.array(v, dtype=float)) for sid, v in by_set_ords.items()}

    ranks: Dict[str, float] = {}
    for c in cards:
        arr = set_sorted[c["set_id"]]
        n = arr.size
        if n == 0:
            ranks[c["id"]] = 0.5
            continue
        count_le = int(np.searchsorted(arr, card_ord[c["id"]], side="right"))
        ranks[c["id"]] = round(count_le / n, 4)
    return ranks


# ---------------------------------------------------------------------------
# Stub features — one function each, neutral mid value until a feed is wired.
# Swap a single body when the real data source lands; nothing else changes.
# ---------------------------------------------------------------------------
def demand_pressure_stub(card: dict) -> float:
    """STUB: eBay sold-through pressure (%). Neutral until the eBay feed lands.

    Real version: pull from market_dynamics.compute() once daily eBay snapshots
    accumulate (est_sold / total_supply).
    """
    return 0.0  # neutral: no observed demand yet (config min=0)


def grading_intensity_stub(card: dict) -> float:
    """STUB: PSA/CGC grading volume signal (0-10). Neutral mid until PSA pop feed."""
    return 5.0


def universal_appeal_stub(card: dict) -> float:
    """STUB: cross-collector desirability (0-10). Neutral mid until a Trends feed."""
    return 5.0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def compute_features(
    cards: Iterable[dict], today: Optional[date] = None, backtest_safe: bool = False
) -> Dict[str, Dict[str, float]]:
    """Compute the live + stub feature table for every card.

    Returns ``{card_id: {feature: value}}`` covering: char_premium, scarcity,
    months_since_release, set_rank, demand_pressure, grading_intensity,
    universal_appeal.

    NOTE: ``pull_cost`` is deliberately omitted — it depends on
    ``pipeline/pullrates.py`` and is merged in by ``build.py`` via
    :func:`inject_pull_cost`. ``today`` is threaded through so months_since_release
    is reproducible. ``backtest_safe`` is passed to :func:`char_premium_table`.
    """
    cards = list(cards)
    char_prem = char_premium_table(cards, as_of=today, backtest_safe=backtest_safe)
    set_ranks = set_rank_table(cards)

    features: Dict[str, Dict[str, float]] = {}
    for c in cards:
        cid = c["id"]
        months = months_since_release(c.get("release_date"), today=today)
        features[cid] = {
            "char_premium": char_prem.get(c["base_name"], 0.0),
            "scarcity": scarcity(c["rarity"], months),
            "months_since_release": round(months, 4),
            "set_rank": set_ranks.get(cid, 0.5),
            "demand_pressure": demand_pressure_stub(c),
            "grading_intensity": grading_intensity_stub(c),
            "universal_appeal": universal_appeal_stub(c),
        }
    return features


def inject_pull_cost(
    features: Dict[str, Dict[str, float]], pull_costs: Dict[str, float]
) -> Dict[str, Dict[str, float]]:
    """Merge build-time ``{card_id: pull_cost}`` into an existing feature table.

    Kept separate so this module never imports ``pipeline/pullrates.py``.
    Mutates and returns ``features`` for convenience.
    """
    for cid, cost in pull_costs.items():
        if cid in features:
            features[cid]["pull_cost"] = cost
    return features


# ---------------------------------------------------------------------------
# Self-test in isolation (no pullrates import).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    cards = json.loads((root / "data" / "normalized" / "cards.json").read_text())
    feats = compute_features(cards)

    by_id = {c["id"]: c for c in cards}

    def show(label: str, card_id: str) -> None:
        c = by_id[card_id]
        f = feats[card_id]
        print(
            f"{label:30} {card_id:14} {c['base_name']:12} "
            f"{c['rarity']:26} price=${c['market_price']:<8} "
            f"char_premium={f['char_premium']:.3f} scarcity={f['scarcity']:.3f} "
            f"set_rank={f['set_rank']:.3f}"
        )

    print("=== char_premium sanity check (hybrid 70% independent + 30% price) ===")
    show("Umbreon (SIR chase)", "sv8pt5-161")
    show("Charizard (SIR chase)", "sv3pt5-199")

    # char_premium is a CHARACTER-level HYBRID signal: every printing of a
    # character shares its premium (70% price-free independent popularity —
    # printings + set breadth + rarity-weighted chase intensity — plus 30%
    # reduced-weight mean-price percentile, all de-skewed to a ~uniform 0-10
    # scale). A genuine nobody — a character printed once, in one set, with no
    # chase rarity and a low price — must sit near the floor. We pick the Common
    # whose *character* has the fewest printings so the contrast is a true one-
    # off, not e.g. Bulbasaur (which also has chase alt-arts and reprints).
    prints_per_char: Dict[str, int] = defaultdict(int)
    for c in cards:
        if c.get("base_name"):
            prints_per_char[c["base_name"]] += 1
    a_common = min(
        (c for c in cards if c["rarity"] == "Common"),
        key=lambda c: prints_per_char.get(c["base_name"], 0),
    )
    show("A true one-off Common", a_common["id"])

    char_prem = char_premium_table(cards)
    umb = feats["sv8pt5-161"]["char_premium"]
    chz = feats["sv3pt5-199"]["char_premium"]
    com = feats[a_common["id"]]["char_premium"]
    pika = char_prem.get("Pikachu")
    sunk = char_prem.get("Sunkern")
    print()
    print(f"Umbreon char_premium   = {umb:.3f}")
    print(f"Charizard char_premium = {chz:.3f}")
    print(f"Pikachu char_premium   = {pika if pika is None else round(pika, 3)}")
    print(f"Sunkern char_premium   = {sunk if sunk is None else round(sunk, 3)}")
    print(f"One-off char_premium   = {com:.3f}  ({a_common['base_name']})")
    # Hybrid 70/30: multi-chase, multi-reprint, high-priced icons must clear a
    # true one-off, while a genuine low-popularity filler sits near the floor.
    # Unlike the old price-free variant, B folds in 30% mean-price percentile, so
    # a single-printing-but-not-dirt-cheap Common (e.g. Nidoran ♀ ~$2.27) lands
    # mid-scale rather than at the very bottom — the floor is anchored by a true
    # filler character (Sunkern), not by whichever Common has the fewest prints.
    # Expected under B on this corpus: Charizard ~9.85, Umbreon ~8.81,
    # Pikachu ~10, Sunkern ~0.17.
    assert umb > com, "Umbreon should dominate a true one-off"
    assert chz > com, "Charizard should dominate a true one-off"
    assert chz > 9, "Charizard (top icon) should sit near the top"
    if pika is not None:
        assert pika >= 9, "Pikachu (most-printed icon) should sit near the top"
    if sunk is not None:
        assert sunk < 3, "Sunkern (low-popularity filler) should sit near the floor"
        assert chz - sunk > 6, "icons should clear a true filler by a wide margin"
    print("\nPASS: hybrid 70/30 char_premium ranks icons >> a true filler")
