"""Fetch card + set data (and today's prices) from pokemontcg.io.

Stdlib only so it runs without the numpy venv. Produces:
  data/raw/<set_id>.json          raw API payloads (cache / audit trail)
  data/normalized/cards.json      list[NormalizedCard]
  data/normalized/sets.json       list[SetRecord]
  data/snapshots/snapshot-<date>.json   {card_id: market_price}  (history seed)
  data/normalized/price_fetch.json      price freshness record (see below)

price_fetch.json holds ``prices_as_of``: the last time EVERY configured set came
back from a live fetch with at least one priced card. On a partial or total
failure it is carried forward unchanged, so every published price is at least
as fresh as ``prices_as_of``. build.py publishes it next to ``built_at``.

NormalizedCard schema (the contract every downstream module reads):
  id, name, base_name, number, rarity, set_id, set_name, series,
  release_date (YYYY-MM-DD), image_small, image_large,
  market_price (float|None), price_variant (str|None), price_updated (str|None)
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from pipeline import config, pricing_tcgdex

# Suffix tokens that are card mechanics, not part of the character name.
_SUFFIX_TOKENS = {"ex", "EX", "V", "VMAX", "VSTAR", "GX", "BREAK", "LV.X", "δ", "Prime", "Star"}


def base_name(name: str, supertype: str = "Pokémon") -> str:
    """Reduce a printed card name to its underlying character identity.

    For Pokémon, drop the "Owner's" possessive (a real Pokémon follows it) and
    the mechanic suffix: 'Team Rocket's Moltres ex' -> 'Moltres', 'Blastoise ex'
    -> 'Blastoise'. For Trainers/Energy the possessive IS the card's identity
    (a Supporter, not a Pokémon), so keep it: 'Morty's Conviction' ->
    'Morty's Conviction', 'Iono' -> 'Iono'.
    """
    n = name.strip()
    # Only Pokémon use the "Owner's Pokémon" form; for Trainers "X's Y" is the
    # card name itself, so stripping it would invent a fake character.
    if supertype == "Pokémon" and "'s " in n:
        n = n.rsplit("'s ", 1)[1]
    tokens = n.split()
    while tokens and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens) if tokens else n


def _request(path: str, params: dict, *, retries: int = 4) -> dict:
    """GET a pokemontcg.io endpoint with retry + exponential backoff.

    Transient failures (read timeouts, dropped connections, 429/5xx) are the
    norm against this free API and used to kill the whole daily refresh on a
    single bad response. We now retry a few times with backoff; only a genuine
    client error (4xx other than 429) or exhausted retries propagates.
    """
    url = f"{config.API_BASE}/{path}?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "price-lab/0.1"}
    if config.API_KEY:
        headers["X-Api-Key"] = config.API_KEY
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Retry rate-limit / server errors; a real client error is fatal.
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 2 ** attempt
                print(f"    {path}: HTTP {e.code}, retrying in {wait}s "
                      f"({attempt + 1}/{retries})", flush=True)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            # OSError covers TimeoutError/ConnectionError/socket timeouts.
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"    {path}: {type(e).__name__} ({e}), retrying in {wait}s "
                      f"({attempt + 1}/{retries})", flush=True)
                time.sleep(wait)
                continue
            raise


def _pick_price(tcgplayer: dict | None):
    """Return (market_price, variant) using VARIANT_PRIORITY, else best available."""
    if not tcgplayer or not tcgplayer.get("prices"):
        return None, None
    prices = tcgplayer["prices"]
    for variant in config.VARIANT_PRIORITY:
        v = prices.get(variant)
        if v and v.get("market"):
            return float(v["market"]), variant
    # Fall back to any variant that has a market price.
    for variant, v in prices.items():
        if v and v.get("market"):
            return float(v["market"]), variant
    return None, None


def fetch_set_cards(set_id: str) -> list[dict]:
    cards, page = [], 1
    while True:
        payload = _request("cards", {"q": f"set.id:{set_id}", "page": page, "pageSize": 250})
        batch = payload.get("data", [])
        cards.extend(batch)
        if len(batch) < 250:
            break
        page += 1
        time.sleep(0.2)
    return cards


# pokemontcg.io occasionally mislabels a secret/chase card with a base rarity,
# which wrecks the sealed-EV math (a base "Rare" is treated as a guaranteed pull
# slot). We correct the rare, confirmed cases to the rarity TCGplayer and the
# card's own print run agree on. Keyed by card id -> corrected rarity.
#   zsv10pt5-171 Victini: source says "Rare" (a $577 card in the guaranteed slot!);
#   TCGplayer + its paired secret Zekrom ex #172 confirm "Black White Rare".
RARITY_CORRECTIONS: dict[str, str] = {
    "zsv10pt5-171": "Black White Rare",
}


def normalize_card(c: dict) -> dict:
    market, variant = _pick_price(c.get("tcgplayer"))
    s = c.get("set", {})
    rel = (s.get("releaseDate") or "").replace("/", "-") or None
    supertype = c.get("supertype") or "Pokémon"
    rarity = RARITY_CORRECTIONS.get(c["id"], c.get("rarity") or "Unknown")
    return {
        "id": c["id"],
        "name": c["name"],
        "base_name": base_name(c["name"], supertype),
        "supertype": supertype,
        "number": c.get("number"),
        "rarity": rarity,
        "set_id": s.get("id"),
        "set_name": s.get("name"),
        "series": s.get("series"),
        "release_date": rel,
        "image_small": (c.get("images") or {}).get("small"),
        "image_large": (c.get("images") or {}).get("large"),
        "market_price": market,
        "price_variant": variant,
        "price_updated": (c.get("tcgplayer") or {}).get("updatedAt"),
        # National Pokédex number (first listed) — lets us derive region/gen.
        # 1-151 = Kanto (Gen 1). None for Trainers/Energy.
        "dex": (c.get("nationalPokedexNumbers") or [None])[0],
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def next_price_status(prev: dict | None, now_iso: str,
                      sets_live: list[str], sets_not_live: list[str]) -> dict:
    """Fold one fetch attempt into the persisted price-freshness record.

    ``prices_as_of`` moves to ``now_iso`` only when every configured set came
    back live with at least one priced card (``sets_not_live`` is empty). On a
    partial or total failure it is carried forward unchanged (None if there was
    never a full success), so it is a true lower bound on price freshness.
    """
    prev = prev if isinstance(prev, dict) else {}
    ok = bool(sets_live) and not sets_not_live
    return {
        "prices_as_of": now_iso if ok else prev.get("prices_as_of"),
        "last_attempt_at": now_iso,
        "last_attempt_ok": ok,
        "sets_live": sorted(sets_live),
        "sets_not_live": sorted(sets_not_live),
    }


def _record_price_status(sets_live: list[str]) -> dict:
    """Write data/normalized/price_fetch.json for this attempt and return it."""
    sets_not_live = [s for s in config.SETS if s not in set(sets_live)]
    path = config.PRICE_FETCH_STATUS
    try:
        prev = json.loads(path.read_text())
    except (OSError, ValueError):
        prev = None
    status = next_price_status(prev, _utc_now_iso(), sets_live, sets_not_live)
    path.write_text(json.dumps(status, indent=1))
    if status["last_attempt_ok"]:
        print(f"  prices_as_of -> {status['prices_as_of']} (all {len(sets_live)} sets live)", flush=True)
    else:
        print(f"  prices_as_of kept at {status['prices_as_of']}: not live this run: "
              f"{status['sets_not_live']}", flush=True)
    return status


def snapshot_prices(cards: list[dict], carried_sets: list[str]) -> dict:
    """Today's ``{card_id: market_price}``, leaving out sets carried from cache.

    A carried set's prices are from an earlier day; writing them into today's
    snapshot made stale prices look fresh to price_move, price_history and the
    backtest.
    """
    carried = set(carried_sets)
    return {c["id"]: c["market_price"] for c in cards
            if c["market_price"] is not None and c.get("set_id") not in carried}


def main() -> None:
    all_cards: list[dict] = []
    set_records: list[dict] = []
    failed_sets: list[str] = []
    live_priced_sets: list[str] = []  # fetched live AND at least one card priced
    for set_id, cfg in config.SETS.items():
        print(f"  fetching {set_id} ({cfg['name']}) ...", flush=True)
        try:
            raw = fetch_set_cards(set_id)
        except Exception as e:  # _request already retried; this set is out for now
            print(f"    !! fetch failed for {set_id} after retries: {e} — "
                  f"carrying cached cards for it", flush=True)
            failed_sets.append(set_id)
            continue
        (config.RAW / f"{set_id}.json").write_text(json.dumps(raw))
        if not raw:
            print(f"    !! no cards returned for {set_id} — carrying cache")
            failed_sets.append(set_id)
            continue
        s = raw[0].get("set", {})
        set_records.append({
            "set_id": set_id,
            "name": s.get("name", cfg["name"]),
            "series": s.get("series"),
            "release_date": (s.get("releaseDate") or "").replace("/", "-") or None,
            "printed_total": s.get("printedTotal"),
            "total": s.get("total"),
            "logo": (s.get("images") or {}).get("logo"),
            "symbol": (s.get("images") or {}).get("symbol"),
            "pack_price": cfg["pack_price"],
            "retail_pack_price": cfg.get("retail_pack_price", cfg["pack_price"]),
            "packs_per_etb": cfg.get("packs_per_etb", 9),
            "etb_price": cfg.get("etb_price"),
        })
        set_cards = [normalize_card(c) for c in raw]
        # Fill prices for sets pokemontcg.io catalogues but doesn't price yet
        # (e.g. Mega Evolution series) from TCGdex -> TCGplayer market price.
        if set_id in pricing_tcgdex.SET_ID_MAP and not any(c["market_price"] for c in set_cards):
            print(f"    no pokemontcg.io prices for {set_id} — pulling TCGplayer prices via TCGdex ...", flush=True)
            tdx = pricing_tcgdex.prices_for_set(set_id)
            filled = 0
            for c in set_cards:
                hit = tdx.get(pricing_tcgdex.normalize_number(c["number"]))
                if hit and c["market_price"] is None:
                    c["market_price"], c["price_variant"], c["price_updated"] = hit
                    filled += 1
            print(f"    filled {filled}/{len(set_cards)} cards from TCGdex", flush=True)
        if any(c["market_price"] is not None for c in set_cards):
            live_priced_sets.append(set_id)
        all_cards.extend(set_cards)

    # If a set's live fetch failed, carry its cards from the last good cache so a
    # transient outage never SHRINKS the catalog.
    if failed_sets:
        try:
            cached = json.loads((config.NORMALIZED / "cards.json").read_text())
            carried = [c for c in cached if c.get("set_id") in failed_sets]
            all_cards.extend(carried)
            cached_sets = json.loads((config.NORMALIZED / "sets.json").read_text())
            set_records.extend([s for s in cached_sets if s.get("set_id") in failed_sets])
            print(f"  carried {len(carried)} cached cards for {len(failed_sets)} "
                  f"failed set(s): {failed_sets}", flush=True)
        except (json.JSONDecodeError, OSError):
            print(f"  no cache to carry for failed set(s): {failed_sets}", flush=True)

    if not all_cards:
        # Total fetch failure with no cache: do NOT clobber the cache with nothing.
        # Return cleanly so the daily-refresh BUILD step still runs on existing data.
        print("  no cards fetched — leaving existing normalized data untouched so "
              "the build can run on cached prices.", flush=True)
        _record_price_status([])  # prices_as_of carried forward unchanged
        return

    (config.NORMALIZED / "cards.json").write_text(json.dumps(all_cards, indent=1))
    (config.NORMALIZED / "sets.json").write_text(json.dumps(set_records, indent=1))

    # Seed a price snapshot for today (history for trends / movers / demand / the
    # backtest's out-of-sample prices).
    snap = snapshot_prices(all_cards, failed_sets)
    (config.SNAPSHOTS / f"snapshot-{date.today().isoformat()}.json").write_text(json.dumps(snap))

    # Only after the data above is written: advance prices_as_of if every set
    # came back live, otherwise carry it forward.
    _record_price_status(live_priced_sets)

    priced = sum(1 for c in all_cards if c["market_price"] is not None)
    print(f"\n  {len(all_cards)} cards across {len(set_records)} sets "
          f"({priced} with prices). Wrote normalized + snapshot.")


if __name__ == "__main__":
    main()
