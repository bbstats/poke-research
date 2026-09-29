"""TCGCSV collector — real historical TCGplayer price panel for the backtest.

`tcgcsv.com` is a free, no-auth mirror of TCGplayer's OWN price API
(market/low/mid/high/direct per product, sealed included). It is NOT a scrape of
TCGplayer's JS site — it's their published data re-served as JSON — so it stays
on the right side of the "clean tool" line. It also keeps a **daily archive**
(`/archive/tcgplayer/prices-<YYYY-MM-DD>.ppmd.7z`) back to 2024-02-08, which is
what lets us backtest the model against real later prices instead of waiting
months for forward data to accrue.

The hard part is the JOIN: our normalized cards carry **no TCGplayer product
id** (pokemontcg.io never exposes one). So we match our cards to TCGplayer
products by **(set -> group) + (collector number -> product)**, with rarity as a
tiebreak. That mapping is built once (`build_idmap`) from the LIVE products
endpoint and cached; historical prices then come from the daily archives keyed by
product id.

Data shapes (verified 2026-06-18):
  * groups:   GET /tcgplayer/3/groups            -> [{groupId, name, abbreviation, ...}]
  * products: GET /tcgplayer/3/<group>/products  -> [{productId, name, extendedData:[{name,value}], ...}]
              extendedData carries "Number" (e.g. "007/165") and "Rarity".
  * prices:   GET /tcgplayer/3/<group>/prices    -> [{productId, subTypeName, marketPrice, ...}]
              one row per variant (Normal / Holofoil / Reverse Holofoil).
  * archive:  /archive/tcgplayer/prices-<date>.ppmd.7z -> <date>/3/<group>/prices files.

Pokémon = categoryId 3. This collector only READS public price data; it never
scrapes, evades bot protection, or touches checkout.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

try:
    from pipeline.config import DATA, NORMALIZED
except Exception:  # pragma: no cover - keep runnable standalone
    DATA = Path(__file__).resolve().parent.parent / "data"
    NORMALIZED = DATA / "normalized"

log = logging.getLogger("collectors.tcgcsv")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

CATEGORY_POKEMON = 3
BASE = "https://tcgcsv.com"
ARCHIVE_DAY1 = date(2024, 2, 8)  # first day TCGCSV has an archive for

ARCHIVE_DIR = DATA / "tcgcsv_archive"   # cached .ppmd.7z (gitignored; large)
HISTORY_DIR = DATA / "history"          # derived per-day {card_id: price} panel
IDMAP_PATH = DATA / "tcgplayer_idmap.json"

# ---------------------------------------------------------------------------
# Our set_id -> TCGCSV groupId (resolved by NAME against the live groups list;
# the Mega-era numbering differs from ours, e.g. our me4 == TCGCSV "ME04").
# Verified 2026-06-18 against GET /tcgplayer/3/groups (217 Pokémon groups).
# ---------------------------------------------------------------------------
SET_TO_GROUP: Dict[str, int] = {
    "sv3pt5": 23237,    # SV: Scarlet & Violet 151
    "sv4pt5": 23353,    # SV: Paldean Fates
    "sv5": 23381,       # SV05: Temporal Forces
    "sv6": 23473,       # SV06: Twilight Masquerade
    "sv6pt5": 23529,    # SV: Shrouded Fable
    "sv7": 23537,       # SV07: Stellar Crown
    "sv8": 23651,       # SV08: Surging Sparks
    "sv8pt5": 23821,    # SV: Prismatic Evolutions
    "sv9": 24073,       # SV09: Journey Together
    "sv10": 24269,      # SV10: Destined Rivals
    "zsv10pt5": 24325,  # SV: Black Bolt
    "me1": 24380,       # ME01: Mega Evolution  (NOT the Promo/Energies groups)
    "me2pt5": 24541,    # ME: Ascended Heroes
    "me3": 24587,       # ME03: Perfect Order
    "me4": 24655,       # ME04: Chaos Rising
}

# our price_variant -> TCGplayer subTypeName, with a fallback order if the exact
# variant has no price row on a given day. SV cards use camelCase variants
# ("reverseHolofoil"); Mega-era cards (priced via TCGdex) use prefixed, hyphenated
# ones ("tcgplayer:reverse-holofoil"). We normalize both to a single key so ME
# cards don't all silently default to Holofoil (which mis-priced 286 of them).
_SUBTYPE_BY_KEY = {
    "holofoil": "Holofoil",
    "reverseholofoil": "Reverse Holofoil",
    "normal": "Normal",
    "1steditionholofoil": "1st Edition Holofoil",
    "unlimitedholofoil": "Unlimited Holofoil",
}
_SUBTYPE_FALLBACK = ["Holofoil", "Normal", "Reverse Holofoil",
                     "1st Edition Holofoil", "Unlimited Holofoil"]

# product-name qualifiers that mark an alt/oddity printing we should de-prefer
# when two products share a collector number. pokemontcg.io prices the BASE
# card, so the "Poke Ball Pattern" / "Master Ball Pattern" mass-reverse-holos
# (separate, higher-priced products with the same number) must lose the tiebreak.
_ALT_PRINT_MARKERS = (
    "poke ball pattern", "master ball pattern", "pattern",
    "metal card", "jumbo", "oversized", "staff", "promo",
)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _get_json(url: str, retries: int = 3, timeout: int = 30):
    """GET ``url`` -> parsed JSON (``results`` unwrapped), with simple backoff."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "poke-research/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            return d.get("results", d) if isinstance(d, dict) else d
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:  # pragma: no cover
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


# ---------------------------------------------------------------------------
# Number / variant normalization
# ---------------------------------------------------------------------------
def _norm_name(s: Optional[str]) -> str:
    """Accent-folded, alphanumeric-only lowercase name for robust matching."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _product_base_name(p: Mapping) -> str:
    """A product's name without the ``" - 081/088"`` suffix or ``"(...)"`` tags."""
    name = (p.get("name") or "").split(" - ")[0]
    if "(" in name:
        name = name[: name.index("(")]
    return _norm_name(name)


def _names_agree(card_name: str, product: Mapping) -> bool:
    """True if our card name and a product's base name plausibly match.

    Substring either way so abbreviations (``Bubbly Water`` vs ``Bubbly W``) and
    suffixes still pass without false-flagging a genuinely different card.
    """
    a, b = _norm_name(card_name), _product_base_name(product)
    if not a or not b:
        return False
    return a in b or b in a


def _norm_number(raw: Optional[str]) -> Optional[str]:
    """Normalize a collector number for matching.

    ``"007/165" -> "7"``, ``"170/165" -> "170"``, ``"TG01/TG30" -> "TG01"``,
    ``"2" -> "2"``. Purely-numeric parts lose leading zeros; alphanumeric parts
    are upper-cased and kept (TG/GG/SV promos).
    """
    if raw is None:
        return None
    s = str(raw).strip().split("/")[0].strip().upper()
    if not s:
        return None
    if s.isdigit():
        return str(int(s))
    return s


def _ext(product: Mapping) -> Dict[str, Optional[str]]:
    return {e["name"]: e.get("value") for e in product.get("extendedData", [])}


# ---------------------------------------------------------------------------
# ID map  (our card_id -> {productId, subtype})
# ---------------------------------------------------------------------------
def _load_cards() -> List[dict]:
    return json.loads((NORMALIZED / "cards.json").read_text())


def build_idmap(cards: Optional[List[dict]] = None,
                write: bool = True) -> Tuple[Dict[str, dict], dict]:
    """Match our cards to TCGplayer products. Returns ``(idmap, coverage)``.

    ``idmap[card_id] = {"productId": int, "subtype": str, "number": str,
                         "name": str}``. ``coverage`` is a per-set report of
    matched / unmatched / ambiguous so we never silently drop misses.
    """
    cards = cards if cards is not None else _load_cards()
    by_set: Dict[str, List[dict]] = {}
    for c in cards:
        by_set.setdefault(c["set_id"], []).append(c)

    idmap: Dict[str, dict] = {}
    coverage: Dict[str, dict] = {}

    for set_id, set_cards in sorted(by_set.items()):
        group = SET_TO_GROUP.get(set_id)
        if group is None:
            coverage[set_id] = {"group": None, "matched": 0,
                                "total": len(set_cards), "unmatched": [c["id"] for c in set_cards],
                                "note": "no group mapping"}
            log.warning("set %s has no TCGCSV group mapping", set_id)
            continue

        products = _get_json(f"{BASE}/tcgplayer/{CATEGORY_POKEMON}/{group}/products")
        # index products by normalized number AND by normalized base name. Number
        # is the unique key; name is the correctness guard + fallback for the rare
        # card whose pokemontcg.io number disagrees with TCGplayer's.
        by_num: Dict[str, List[dict]] = {}
        by_name: Dict[str, List[dict]] = {}
        for p in products:
            num = _norm_number(_ext(p).get("Number"))
            if num is not None:
                by_num.setdefault(num, []).append(p)
            by_name.setdefault(_product_base_name(p), []).append(p)

        matched, unmatched, ambiguous, name_fixed = 0, [], [], []
        for c in set_cards:
            cnum = _norm_number(c.get("number"))
            cands = by_num.get(cnum, []) if cnum else []
            chosen = _pick_product(cands, c) if cands else None

            # Validate by name; if the number landed on a different card AND an
            # exact name match exists, prefer the name match (e.g. Black Bolt's
            # "Antique Cover Fossil", numbered 60 by us / 80 by TCGplayer).
            if chosen is None or not _names_agree(c.get("name", ""), chosen):
                name_cands = by_name.get(_norm_name(c.get("name")), [])
                if name_cands:
                    chosen = _pick_product(name_cands, c)
                    if chosen is not None:
                        name_fixed.append(c["id"])
            if chosen is None:
                unmatched.append(c["id"])
                continue
            if len(cands) > 1:
                ambiguous.append(c["id"])
            idmap[c["id"]] = {
                "productId": int(chosen["productId"]),
                "subtype": _pick_subtype_name(c),
                "number": cnum,
                "name": c.get("name"),
            }
            matched += 1

        coverage[set_id] = {
            "group": group,
            "matched": matched,
            "total": len(set_cards),
            "unmatched": unmatched,
            "ambiguous": ambiguous,
            "name_fixed": name_fixed,
        }
        log.info("set %-9s group %-6s matched %d/%d (ambiguous %d, name-fixed %d, unmatched %d)",
                 set_id, group, matched, len(set_cards), len(ambiguous), len(name_fixed), len(unmatched))

    if write:
        IDMAP_PATH.write_text(json.dumps(idmap, indent=0))
        (DATA / "tcgplayer_idmap_coverage.json").write_text(json.dumps(coverage, indent=2))
    return idmap, coverage


def _pick_product(cands: List[dict], card: Mapping) -> Optional[dict]:
    """Choose the best TCGplayer product among same-number candidates.

    pokemontcg.io prices the BASE single; TCGplayer sells the pattern reverse-
    holos (``X (Poke Ball Pattern)``, ``X (Master Ball Pattern)``, ``X (Energy
    Symbol Pattern)``, ``X (Poke Ball)`` ...) as separate, higher-priced products
    sharing the same collector number. The base product's name has **no
    parenthetical suffix**, which is the most reliable separator; we layer rarity
    match, the explicit alt-print markers, and an exact-name check on top.
    Preference, high→low: (1) rarity matches, (2) name has no "(...)" suffix,
    (3) no alt-print marker, (4) name equals our card name, (5) first.
    """
    if len(cands) == 1:
        return cands[0]
    cr = (card.get("rarity") or "").lower()
    cname = (card.get("name") or "").strip().lower()

    def score(p: dict) -> tuple:
        pr = (_ext(p).get("Rarity") or "").lower()
        pname = (p.get("name") or "").strip().lower()
        rarity_match = 1 if cr and pr and cr == pr else 0
        no_paren = 0 if "(" in pname else 1
        clean = 0 if any(m in pname for m in _ALT_PRINT_MARKERS) else 1
        exact = 1 if cname and pname == cname else 0
        return (rarity_match, no_paren, clean, exact)

    return max(cands, key=score)


def _pick_subtype_name(card: Mapping) -> str:
    """Our card's price_variant -> a TCGplayer subTypeName (best guess).

    Handles both SV camelCase ("reverseHolofoil") and Mega-era prefixed/hyphenated
    ("tcgplayer:reverse-holofoil") forms by normalizing to a bare lowercase key.
    """
    pv = card.get("price_variant") or ""
    key = pv.split(":")[-1].replace("-", "").replace("_", "").lower()
    return _SUBTYPE_BY_KEY.get(key, "Holofoil")


def _price_from_rows(rows: Iterable[Mapping], productId: int, subtype: str,
                     fallbacks: Optional[List[int]] = None) -> Optional[float]:
    """Pick a marketPrice for ``productId`` preferring ``subtype`` then fallbacks.

    A fallback prices a DIFFERENT printing (e.g. holo instead of reverse holo), so
    the card's series can jump that day. When ``fallbacks`` is given, the
    productId is appended to it each time that happens so callers can report it.
    """
    avail = {r.get("subTypeName"): r.get("marketPrice")
             for r in rows if r.get("productId") == productId and r.get("marketPrice") is not None}
    if not avail:
        return None
    if subtype in avail:
        return float(avail[subtype])
    if fallbacks is not None:
        fallbacks.append(productId)
    for st in _SUBTYPE_FALLBACK:
        if st in avail:
            return float(avail[st])
    return float(next(iter(avail.values())))


# ---------------------------------------------------------------------------
# Archive download + extraction
# ---------------------------------------------------------------------------
def _archive_path(d: date) -> Path:
    return ARCHIVE_DIR / f"prices-{d.isoformat()}.ppmd.7z"


def download_archive(d: date, force: bool = False) -> Optional[Path]:
    """Download (and cache) the daily archive for ``d``. Returns path or None."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _archive_path(d)
    if dest.exists() and dest.stat().st_size > 0 and not force:
        return dest
    url = f"{BASE}/archive/tcgplayer/prices-{d.isoformat()}.ppmd.7z"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "poke-research/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        dest.write_bytes(data)
        return dest
    except urllib.error.HTTPError as e:  # pragma: no cover
        log.warning("no archive for %s (HTTP %s)", d, e.code)
        return None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:  # pragma: no cover
        log.warning("archive download failed for %s: %s", d, e)
        return None


def prices_for_date(d: date, idmap: Mapping[str, dict],
                    groups: Optional[Iterable[int]] = None) -> Dict[str, float]:
    """Extract our cards' market prices for date ``d`` -> ``{card_id: price}``.

    Reads only the group price files we need from the cached archive. Cards
    whose set didn't exist yet (no group file that day) are simply absent.
    """
    import py7zr  # local import so the rest of the module loads without it

    arc = download_archive(d)
    if arc is None:
        return {}
    wanted_groups = set(groups) if groups is not None else set(SET_TO_GROUP.values())
    tmp = ARCHIVE_DIR / f"_x_{d.isoformat()}"

    rows_by_group: Dict[int, list] = {}
    with py7zr.SevenZipFile(arc, "r") as z:
        names = set(z.getnames())
        targets = [f"{d.isoformat()}/{CATEGORY_POKEMON}/{g}/prices"
                   for g in wanted_groups
                   if f"{d.isoformat()}/{CATEGORY_POKEMON}/{g}/prices" in names]
        if not targets:
            return {}
        z.extract(path=tmp, targets=targets)
    for g in wanted_groups:
        f = tmp / f"{d.isoformat()}/{CATEGORY_POKEMON}/{g}/prices"
        if f.exists():
            doc = json.loads(f.read_text())
            rows_by_group[g] = doc.get("results", doc) if isinstance(doc, dict) else doc

    # flatten all rows once, index by productId for fast lookup
    all_rows: List[dict] = [r for rows in rows_by_group.values() for r in rows]
    out: Dict[str, float] = {}
    fallbacks: List[int] = []
    for cid, m in idmap.items():
        price = _price_from_rows(all_rows, m["productId"], m.get("subtype", "Holofoil"), fallbacks)
        if price is not None:
            out[cid] = round(price, 2)
    if fallbacks:
        log.warning("%s: %d card(s) priced from a different variant (preferred subtype missing)",
                    d, len(fallbacks))

    # cleanup extracted temp tree
    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:  # pragma: no cover
        pass
    return out


# ---------------------------------------------------------------------------
# Backfill driver
# ---------------------------------------------------------------------------
def weekly_dates(start: date = ARCHIVE_DAY1, end: Optional[date] = None,
                 step_days: int = 7) -> List[date]:
    """Sampled dates ``start..end`` every ``step_days`` (default weekly)."""
    end = end or (date.today() - timedelta(days=1))
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(days=step_days)
    return out


def prefetch_archives(dates: Iterable[date], workers: int = 8) -> None:
    """Download all needed daily archives concurrently (idempotent, cached)."""
    from concurrent.futures import ThreadPoolExecutor
    dates = list(dates)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda d: download_archive(d), dates))


def backfill(dates: Iterable[date], idmap: Optional[Mapping[str, dict]] = None,
             skip_existing: bool = True) -> Dict[str, int]:
    """Write ``data/history/price-<date>.json = {card_id: price}`` for each date.

    Returns ``{date: n_cards_priced}``. Idempotent: existing day files are
    skipped unless ``skip_existing=False``.
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    if idmap is None:
        idmap = json.loads(IDMAP_PATH.read_text()) if IDMAP_PATH.exists() else build_idmap()[0]

    result: Dict[str, int] = {}
    for d in dates:
        out_file = HISTORY_DIR / f"price-{d.isoformat()}.json"
        if out_file.exists() and skip_existing:
            try:
                result[d.isoformat()] = len(json.loads(out_file.read_text()))
                continue
            except Exception:  # pragma: no cover
                pass
        prices = prices_for_date(d, idmap)
        out_file.write_text(json.dumps(prices, separators=(",", ":")))
        result[d.isoformat()] = len(prices)
        log.info("history %s: %d cards priced", d.isoformat(), len(prices))
    return result


if __name__ == "__main__":  # pragma: no cover
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "idmap":
        _, cov = build_idmap()
        tot_m = sum(v["matched"] for v in cov.values())
        tot_t = sum(v["total"] for v in cov.values())
        print(f"idmap: matched {tot_m}/{tot_t} cards across {len(cov)} sets")
        for sid, v in cov.items():
            print(f"  {sid:9s} {v['matched']:>4}/{v['total']:<4} "
                  f"ambiguous={len(v.get('ambiguous', []))} unmatched={len(v['unmatched'])}")
    else:
        idmap, cov = build_idmap()
        dates = weekly_dates()
        print(f"backfilling {len(dates)} weekly dates {dates[0]}..{dates[-1]}", flush=True)
        prefetch_archives(dates)
        print("archives prefetched; extracting price panel...", flush=True)
        res = backfill(dates, idmap)
        priced_days = sum(1 for n in res.values() if n)
        print(f"done: {priced_days}/{len(dates)} days have prices "
              f"(max {max(res.values())} cards/day)", flush=True)
