# Poke Research — project guide for Claude

Free Pokémon TCG analytics site modeled on mycollectrics.com ("Collectrics IQ /
Price Lab"), aiming to match-or-beat it. Scores cards as over/under/fair-valued
from a clustered price model, surfaces undervalued/overvalued cards + sealed EV.

- **Local:** `~/Pokemon Bot/poke-research` · **GitHub:** Raz-Gits/poke-research (**public**)
- **Live:** deployed on **Netlify** (auto-deploys from `main` on every push;
  `netlify.toml` pins publish dir = `docs`). GitHub Pages is **disabled**.
- **Owner:** values accuracy and honest signals; this is a clean analytics tool —
  **never** build scalping / anti-bot evasion / auto-checkout.

## Stack & data flow

Python pipeline (stdlib + numpy in `.venv`) → writes `docs/data/*.json` → static
vanilla-JS frontend in `docs/` (Miro design system, see `DESIGN.md`). No build
step for the frontend; Pages serves `/docs` directly.

```
fetch.py ──→ data/normalized/cards.json ──→ build.py ──→ docs/data/{cards,model,
  (pokemontcg.io + TCGdex prices)                         leaderboard,sets,meta,
                                                          premium_review}.json
```

## Run it

```bash
./refresh.sh                                  # SHORTCUT: fetch + build (DEPLOY=1 to push live)
./.venv/bin/python -m pipeline.fetch          # refresh prices (pokemontcg.io + TCGdex)
./.venv/bin/python -m pipeline.build          # recompute everything -> docs/data
./.venv/bin/python -m pipeline.review_premium # premium vs price discrepancy report
./.venv/bin/python -m collectors.wikipopularity  # refresh Wikipedia views (rarely)
./run_daily.sh            # fetch + build (DEPLOY=1 to also git commit+push docs/data)
```
```bash
# Backtest pipeline (validates the model against real TCGplayer history)
./.venv/bin/python -m collectors.tcgcsv idmap   # rebuild card->TCGplayer id-map (+ coverage)
./.venv/bin/python -m collectors.tcgcsv         # download archives + backfill data/history/
./.venv/bin/python -m pipeline.backtest         # walk-forward backtest -> docs/data/backtest.json
```
Deploy = commit + push to `main`; **Netlify** rebuilds and swaps in ~1 min
(hard-refresh). No GitHub Pages anymore. `pip install -r requirements.txt`
(numpy, py7zr) into `.venv` on a fresh clone.

## Pipeline modules (`pipeline/`)

- **config.py** — `SETS` (15: SV + Mega-era), `FEATURES` (label/status/min/max →
  drives the frontend sliders), model constants, `SITE_DATA = docs/data`.
- **fetch.py** — normalize cards; fills Mega-set prices from TCGdex (pokemontcg.io
  doesn't price them). `base_name` is supertype-aware.
- **pullrates.py** — `BASE_TIER_PROB` + per-set `SET_TIER_PROB`. SV sets (151,
  Prismatic, Paldean Fates) and the 4 Mega-era sets (Ascended Heroes, Perfect
  Order, Chaos Rising, Shrouded Fable) now have data-backed per-rarity odds
  (only Shrouded's Hyper is still a small-sample estimate). pull_cost =
  pack_price ÷ (tier_prob ÷ #cards-of-that-rarity-in-set).
- **signals.py** — `char_premium_table` (uses popularity.py + structural
  fallback; now FULLY price-INDEPENDENT — the old 30% current-price component was
  dropped per the Codex audit, `_CHARPREM_W_PRICE=0`: it was mild residual
  circularity for no-prior chars; backtest showed dropping it holds forward IC +
  improves decile spread; + a systematic **Kanto/Gen-1 tilt**: every char with
  dex# 1-151 gets +0.75, capped 9.5, never lowering — preserves intra-region
  ranking, validated to not hurt forward IC), `scarcity` = 10·(0.7·rarity_rank +
  0.3·age_factor), `set_rank` = within-set **rarity-tier** percentile
  (price-FREE, non-circular — was a within-set *price* percentile that leaked
  the label, corr 0.86 w/ price; swapped after the formula-eval backtest),
  stub features (demand_pressure/grading/universal_appeal).
- **popularity.py** — character premium backbone (see below).
- **review_premium.py** — flags price-vs-premium discrepancies for manual review.
- **model.py** — clustered ridge regression on log(price), one model per
  (supertype · rarity) cluster + global fallback. R²(log) ≈ 0.92 (was 0.96
  before removing the circular set_rank — R² is IN-SAMPLE fit, NOT accuracy;
  judge the model on forward IC, not R²; the frontend chip says "in-sample fit").
- **market_dynamics.py** + **collectors/ebay.py** — eBay demand pressure / supply
  saturation. `EBAY_APP_ID`/`EBAY_CERT_ID` are LIVE (in `.env`, gitignored; also
  GitHub repo secrets for the Action). Browse API = ACTIVE listings only (no
  public sold endpoint), so demand is INFERRED from day-over-day listing/price
  diffs → needs a few daily snapshots to accrue. Collector hardened after a
  rate-limit incident: `MAX_PAGES_PER_CARD=1`, `REQUEST_PAUSE_S=0.25` (~4 req/s;
  20/s tripped eBay's burst limit → 65-min 429 backoff crawl), plus wall-clock
  (7-min) + consecutive-fail circuit-breakers so a sweep can NEVER hang. The
  daily sweep is scoped to cards ≥ `$LEADERBOARD_MIN_PRICE` (~850, valuable-
  first) — the only cards the signal serves. **Market-anchored price band**
  (`_apply_price_band`, `PRICE_BAND_LOW=0.5`/`HIGH=4.0`, anchor ≥$2): drops eBay
  listings <50% / >4× the card's TCGplayer `market_price` BEFORE IQR cleaning —
  the free-text search matches same-name printings (a chase IR query also pulls
  the $2 base print), which collapsed avg_price (audit: Ethan's Ho-Oh ex read
  $5.71 vs $198). Verified live to fix the mismatches without hurting good
  matches (`experiments/verify_price_band.py`). Still NOT a model feature yet
  (dynamics is display-only / `awaiting_data`); the live site must NEVER present
  simulated data as real. When wiring into the model/backtest, `compute()` MUST
  become as-of-date aware (it currently reads latest history) or it leaks.
- **collectors/wikipopularity.py** — caches Wikipedia article views.
- **collectors/tcgcsv.py** — real TCGplayer price *history* from tcgcsv.com (free
  mirror of TCGplayer's price API; NOT a scrape). Builds the card→productId
  id-map by (set→group)+(collector number), name-validated (2937/2937);
  downloads daily `.ppmd.7z` archives → `data/history/price-<date>.json` panel.
  `SET_TO_GROUP` + `RARITY_CORRECTIONS` (fetch.py) handle source quirks.
- **backtest.py** — walk-forward backtest: refit the model as-of each historical
  date (no lookahead), score over/under calls vs real later prices. Newey-West
  (HAC) significance, age-gating, floor sweep → `docs/data/backtest.json`
  (the Track Record page). build.py also writes `pred-<date>.json` each run for
  forward self-grading.

## Backtest / Track Record — 2026-09-29 re-evaluation (supersedes the numbers below)

`pipeline/backtest.py` was rebuilt. It now buys 7 days after the signal (`SKIP_DAYS`),
refits with backtest-safe features (2020 poll only: no `USER_OVERRIDES`, no Wikipedia,
no Kanto tilt), scores naive baselines alongside the model (cheapest-in-group,
4w/8w momentum, age), reports within-set IC, a per-release table for fresh cards, an
out-of-sample block (`OOS_START = 2026-06-12`, from the daily `snapshot-*` prices,
stale carried sets dropped), and grades the published `pred-*` calls (`grade_live`).
Regression bridge: live features + skip 0 reproduce the old 0.110 / 0.263 / -0.039.

What it shows (28d horizon, in-sample → out-of-sample):
- **Fresh releases** are the model's only card-level skill: within-set IC +0.23, positive
  in 12 of 13 releases. A naive "cheapest in its rarity group" rule gets +0.19.
  New-set cards fell in 9 of 13 releases.
- **Mature $10+**: the model is about zero or negative (−0.056 in-sample). Momentum
  (+0.10 → +0.11) and set age (+0.25 → +0.17) predict, but their within-set IC is ~0:
  it is a SET-level effect (older, already-rising sets keep rising), not a card-level one.
- **Mature $100+ out of sample**: the model's calls were wrong in 0 of 10 weeks (IC −0.10;
  the live-site version −0.24). It calls chase cards in appreciating sets "overvalued".
- The old "+0.134 surfaced" headline was in-sample; the gates were tuned on that history.
- The out-of-sample block is only 10 overlapping weeks (~2 independent months), so
  HAC t is withheld there. Don't treat it as proof.
Next (P2): "wait to buy" decay curve for fresh cards, set-level trend signal for mature
cards, fair value as an explainer only. The ridge itself is not the bottleneck; its
target (price level) is.

## Backtest / Track Record — what it found (original, in-sample; superseded above)

The model's edge is **real but specific** (28-day horizon, HAC t). Current
formula (rarity-ordinal set_rank + Kanto tilt):
- **Strong on fresh releases** (post-release ≤35d): IC ≈ +0.27, HAC t ≈ 10.9,
  positive 63/64 weeks — fresh chase cards are overpriced and fall ~10%/month,
  and the model ranks which fall hardest.
- **≈ zero on mature, liquid cards** (>90d & >$10): IC ≈ −0.03 (HAC t −1.05,
  insignificant). Not a crystal ball for blue-chips. Edge also collapses above
  a ~$10 floor (cheap-card noise). all-cards IC ≈ +0.094 (t 6.5).
- **formula-eval (multi-agent, 6 variants)**: proved `set_rank` was circular
  (corr 0.86 w/ price), inflating R² 0.91→0.95 without aiding forward IC.
  Swapping to a non-circular rarity ordinal lifted fresh IC 0.23→0.27 and made
  R² honest. Supply-only (drop char_premium) is NOT better — it flips mature IC
  to significantly wrong-signed (t −2.27). Don't strip features; don't chase R².
  One modest cost: decile spread on 'all' dipped 0.096→0.081.
4 adversarial agents verified: no look-ahead leakage, id-map correct. They caught
the inflated naive t (10→~6 after HAC), the presale-window effect (TCGCSV carries
presale prices; headline fresh-cut excludes negative ages), and the ME variant
bug — all fixed. Present these numbers honestly; never lead with the inflated IC.

### 2026-06-21 accuracy audit (multi-agent) — SHIPPED
A 5-agent backtest audit drove three backtest-proven changes (all on the 123-cut
walk-forward, leak-free):
- **LEAN 3-feature model** — `config.FEATURES = [char_premium, scarcity,
  months_since_release]`. Dropped `set_rank` (within-cluster near-constant, corr
  0.87 w/ scarcity), `pull_cost` (extreme-skew, duplicates rarity post-cluster),
  and the 3 dead stubs. **all-cards IC +0.094 → +0.110, decile $-spread +30%**,
  fresh held (+0.26). set_rank/pull_cost still COMPUTED for display + sealed-EV,
  just not model inputs. R²(log) 0.92 → 0.90 (in-sample fit, not accuracy).
- **Leaderboard GATING** (`config.GATE_DEAD_LO/HI`, `build._surfaced`) — the
  mature MID-PRICE zone [$20,$100) (non-fresh) is wrong-signed (IC −0.05), so we
  don't surface over/under calls there; fresh + cheap + marquee $100+ stay.
  **`surfaced` (gated) IC = +0.134** — the new honest headline on the Track
  Record (`backtest.json.headline.surfaced`; frontend `trHeadlineCard` +
  `cardHasEdge`/'no edge' verdict for dead-zone cards).
- **Mature is NEAR-EFFICIENT, measured-dry** — every feature set, alpha sweep,
  and a momentum feature fail to lift mature IC above ~0. Don't keep trying to
  squeeze a price-structural mature edge; the honest ceiling there is ~0.
- **Demand is the only untried lever for mature, but DATA-BLOCKED** (3 snapshot
  dates, the old collector swept a budget-truncated VARIABLE slice → 0 day-over-
  day same-card overlap; `compute()` wasn't as-of-aware). PREP shipped, NOT a
  model feature: (a) `market_dynamics.load_history(as_of=)` + `compute(as_of=)`
  now gate out future snapshots (no look-ahead), drop undated rows; regression
  test `tests/test_demand_asof.py`; (b) collector now sweeps a FIXED valuable-
  first universe (`config.DEMAND_UNIVERSE_SIZE=150`, price-ordered) so the same
  top cards get a daily count and diffs accrue. First forward (not retrospective)
  validation on mature&liquid is ~5-6 weeks out; never present pre-accrual demand
  as real accuracy. DEFERRED: leave-one-out displayed residuals (0 verdict
  changes, ~1% magnitude, slider-coherence subtlety — not worth the risk yet).

### 2026-06-21 plain-language market-signal labels + gross-flow collector — SHIPPED
Built after reviewing the Collectrics creator's "market divergence / watchlist" video
+ screenshots. His one model move (feeding demand_pressure into expected price as a
Price-Lab slider, then projecting "hot market +32%") is the contemporaneous-fit LEAK
we reject — confirmed on camera ("too aggressive… more testing needed"). We took only
the honest, cheap win: a RULES layer, never a model input.
- **`pipeline/signal_labels.py`** — fuses recent price move (daily `snapshot-<date>.json`
  history; `load_price_history`/`price_move`, as-of-gated) + `dynamics.demand_pressure`/
  `supply_saturation` into ONE English verdict per card (`compute_signal` →
  `{label, tone, detail, confidence, basis}`). DESCRIPTIVE only; demand never touches
  `expected_price`. Thresholds in `config.SIGNAL_*` (calibrated to the live feed:
  demand p90≈18% → HOT=15). Graceful degradation: supply_saturation is flat 1.0 until
  >7d span, so "cooling/loosening" clauses stay dark (we NEVER cry cooling on a low
  demand level — that's the noisy default) and auto-light as span grows. `confidence`:
  full (price+live flow) / price_only (no eBay sweep) / none (awaiting). Baked into
  `cards.json` + leaderboard rows in `build.py` (`build_cards` `signals_by_id`).
  Honesty test `tests/test_signal_labels.py`. Frontend: `signalPill` (grids/rows),
  `signalBanner` (modal), `flowStats` (per-card active/new/sold-est, like his card view).
- **Gross-flow est_sold refinement** (`collectors/ebay.py`) — live sweep now records
  the surviving listings' `item_ids`; `diff_row` infers ended/new from day-over-day
  id-SET differences (gross) when both days carry ids, falling back to the old NET
  count-delta otherwise. Catches churn the net delta hid (5 end + 5 appear → net 0 but
  3 sold). Backward-compatible; sim/old snapshots use the net path.
- **Data-volume reality** (answer to "how do we match him"): he's eBay-based too and
  his only real edge is TIME × COVERAGE. eBay active-listing history CANNOT be
  backfilled (no historical endpoint) — it only accrues forward from when you start;
  he started earlier. Levers: (1) keep running daily (~2wk → supply labels live, ~30d
  → full 30d window); (2) widen `DEMAND_UNIVERSE_SIZE` / raise quota for more cards/day
  (bounded by the ~5k/day Browse quota + 7-min sweep wall-clock); (3) PriceCharting API
  is the ONLY way to backfill real sold history instantly (paid, ToS-clean).
  In flight (user-selected): `EBAY_INSIGHTS_APPLICATION.md` (apply for Marketplace
  Insights — official sold endpoint, restricted); PSA/CGC pop reports next (real
  collector-appeal + graded-tier prices, like his PSA9/PSA10 + Gem Rate).
- **CAUTION**: never run `python -m collectors.ebay` against the real `data/snapshots/`
  — its `__main__` runs `simulate_history` and overwrites real snapshots with synthetic
  ones (recoverable via git since snapshots are tracked, but don't).

## Character premium — how it works (important, heavily iterated)

Popularity is a **durable, price-INDEPENDENT** signal (so an Eevee reads as
valuable for *being Eevee*, not because a card is currently hyped). Two
recognition signals combined in `popularity.py`:

1. **Wikipedia** 12-mo article views (mainstream recognition) — `data/wiki_pageviews.json`.
2. **2020 "Pokémon of the Year" poll** vote counts (fan recognition) — 240 ranked.

Combine: both present → `0.7·max + 0.3·min` (rewards corroboration, tempers
high-wiki/low-poll spikes like Raichu); poll-only trusted alone; wiki-only capped
at 8.3 (nostalgia noise — Jynx/Butterfree/Cubone). Characters with neither →
structural print-metadata fallback, compressed below 6.0.

**`USER_OVERRIDES`** in popularity.py win over everything (10.0 reserved for
them). The premiums are a **starting point, not gospel** — run `review_premium`,
eyeball the flags, and pin values in `USER_OVERRIDES`. Most "market hotter than
rating" flags are art-driven Illustration Rares (card art ≠ character fame).

## Open threads

- **Watchlists tab** (`#/watchlists`, `docs/app.js` `viewWatchlists`): a sealed-
  ETB **deal monitor**, separate from the price model. Fed by
  `docs/data/watchlist.json`, written by **`../ebay_deals.py`** (lives in the
  parent `~/Pokemon Bot`, NOT this repo — same place as the restock `monitor.py`).
  That watcher polls eBay Browse for a small watchlist (`../deals_watchlist.json`:
  11 ETBs incl. Pokémon Center variants — Ascended Heroes, Prismatic, White Flare,
  PC Pitch Black, Surging Sparks (+PC), and 151 (+PC)) and pushes a phone alert (ntfy, reusing
  the restock monitor's `config.NTFY_TOPIC`) when a deal lands at/under the per-
  product `max_price` cap. Notify-only, manual buy — same clean-monitor boundary
  as the restock alerter (no scraping/auto-checkout; Browse API's intended use).
  Junk control = the SAME ">50% under market → ignore" rule as the card
  collector, anchored to each watch's `market_price` (clean live median), plus
  `exclude_all` keywords (graded singles incl. ACE/Black-Star-Promo slabs,
  opened/empty boxes, foreign-language prints, single-pack mislistings, proxies,
  lots). Already-ended/sold listings are dropped (`_hours_left < 0`) so neither
  the site nor the phone shows stale results. First run primes silently (no alert
  storm). **Phone-alert gate (`_push_worthy`):** only Buy-It-Nows + auctions in
  their final 15 min (`PUSH_AUCTION_WINDOW_H` = 0.25h — owner's choice: auction prices
  climb to the close, so an at/under-cap bid is only meaningful late) ping the
  phone; auctions with more time left ride the site until they enter that closing
  window, THEN ping (priming silences only currently-worthy ones, so a far-off
  auction still fires when it closes in). The **site keeps ALL** active under-cap
  deals (both kinds, any time-left) — the gate is phone-only. The site JSON is a lean snapshot
  (aggregates + 2 best links + the cheapest 6 BINs and 6 auctions under cap); each
  Watchlist card has a **Buy It Now / Auction / All switch** (`wlListingsPanel` in
  app.js) to view each kind on its own. Page shows "updated <time>". **Shipping is
  made explicit**: the price is all-in when eBay returns a shipping cost; when it
  doesn't (common — ~half of BINs), the row/best-cell/alert flag "⚠ shipping not
  listed — check" so the owner knows the shown price is item-only (the
  `cap_no_shipping` item-price cap is the hedge for those).
  - **RUN MODEL (live):** always-on **launchd** service
    `~/Library/LaunchAgents/com.razsela.ebay-deals.plist` (label
    `com.razsela.ebay-deals`, KeepAlive, `PYTHONUNBUFFERED=1`, log →
    `~/Pokemon Bot/ebay_deals.log`), mirroring the restock `com.razsela.pokemon-
    monitor`. **Quiet hours 02:30–07:00 local — zero Browse calls** (owner asleep);
    awake sweeps auto-paced from the live watch count to a `DAILY_CALL_BUDGET`
    (~4,600 calls/day, well under eBay's ~5k cap) packed into the awake window, so
    they run a bit QUICKER while up (~168s for 11 watches) and the interval is
    recomputed every pass (adding watches never blows the budget; `_sweep_interval`,
    `_in_quiet_hours`). `launchctl bootout gui/$UID/com.razsela.ebay-deals` pauses;
    `launchctl bootstrap gui/$UID <plist>` reloads (and loads any code changes).
  - **Commits are MANUAL by owner's choice** — do NOT add periodic auto-commit of
    `watchlist.json` (every-sweep pushes would burn Netlify build minutes for no
    real gain; the phone is the real-time trigger, the site is just a browsing
    convenience). The service updates the LOCAL JSON each sweep; the deployed tab
    only refreshes when the owner explicitly asks for a commit/push.
  - **7th watch = Pokémon Center Pitch Black ETB** (ME05, group 24688, product
    692949), added 2026-06-21. PRESALE (set releases 7/17): `market_lock: true`
    pins the anchor to eBay reality (~$190) instead of TCGplayer's thin-volume
    $562, with `market_lock_until: 2026-07-24` to auto-re-engage live pricing a
    week after release. Cap $150 all-in / $140 item.
  - **Reliability hardening (2026-06-21, from a multi-agent audit):** OAuth token
    now CACHED with expiry + auto-refreshed each sweep (+ 401 re-auth in browse) —
    was fetched once and the loop went blind after ~2h. `seen` is set ONLY after
    a push delivers (failed ntfy → retry next sweep, not silently buried). Adding
    a watch mid-run now auto-primes via `primed_watches` state (no alert storm).
    `_collect` drops used/opened boxes (`USED_CONDITIONS`). State + site writes are
    atomic (`_atomic_write`, temp+os.replace). The 3 PC watches got shipping-aware
    caps (`cap_after_shipping`/`cap_no_shipping`). Skipped: the "auction re-alert"
    finding (a non-issue — `seen` is item-id-keyed, so a fired auction never
    re-pings) and a still-open offer to add the STANDARD (non-PC) Pitch Black watch.
  - **Watches 8–11 + deal-quality upgrades (2026-06-22):** added Surging Sparks ETB
    (+PC) and 151 ETB (+PC) → 11 watches. Three new mechanics in `ebay_deals.py`:
    (1) **Self-adjusting % caps** — a watch may set `cap_pct_after_shipping` /
    `cap_pct_no_shipping` / `max_pct`; `apply_pct_caps()` re-derives the dollar caps
    from the daily-refreshed `market_price` each sweep so caps track market instead
    of going stale (absolute cap is the seed/fallback; all 11 watches now carry pcts
    derived to preserve their then-current caps). (2) **"Lowest in N days" deal
    quality** — `watch_floor_history.json` (in `docs/data`, written by the local
    watcher) logs each watch's cheapest in-cap **Buy-It-Now per day**;
    `BEST_PRICE_WINDOW_DAYS=14`. A BIN at/under the prior N-day floor gets a 🔥
    "lowest in 14d" tag in the phone alert + a 🔥 badge / eBay-floor sparkline on the
    dashboard (`floor_today`/`window_low`/`is_window_low`/`floor_history` in
    watchlist.json; `wlFloorRow`/`floorSparkSvg` + `.chip--fire`/`.wl-floor` in the
    frontend). Needs ~2 weeks to accrue before the flag fires meaningfully. (3) the
    **quiet-hours + budget pacing** above. NOTE: these watcher files live in the
    non-git parent, so only the *data + dashboard* they produce is in this repo.
    A `MARKET_RESEARCH_2026-06-22.md` audit found our card signal labels contradict
    real SOLD trends on chase cards because the TCGplayer-"market" anchor sits above
    sold — the standing fix direction is to anchor trend signals to sold data.
- eBay demand feed is LIVE (Production keys in `.env` + GitHub secrets). Daily
  snapshots accrue via the `daily-refresh` GitHub Action (`.github/workflows/`,
  14:00 UTC: fetch → build [collects eBay, valuable-first, idempotent] → push →
  Netlify). Demand is still DISPLAY-ONLY, not a model feature yet. **Next big
  step — wire demand into the model** (after ~3-5 days of snapshots): (a) Codex
  #5 — make `market_dynamics.compute()` as-of-date aware FIRST (else look-ahead);
  (b) add demand features + backtest the lift on the dead mature segment; (c)
  Codex #4 — cross-fit/leave-one-out the displayed residuals (own-card leakage).
  Then fix the demand-gauge scale + movers chip stub→live in `docs/app.js`.
- Codex pricing audit (done): UI now respects a ±15% verdict band ("fair" inside
  it; `CARD_BAND` in app.js); char_premium price component dropped; RIDGE_ALPHA
  swept and kept at 1.0 (all within noise). #4/#5 above are the remaining items.
- PSA grading intensity (`collectors/psa.py`, token LIVE): real per-card DEMAND
  signal (graded pop + PSA-10 count) via PSA's free Public API. Gotchas: (1) auth
  is `authorization: bearer <PSA_API_TOKEN>` + a real User-Agent (Cloudflare 403s
  the default urllib UA, error 1010); (2) the API has NO card→spec search — pop
  is only by `specID`, so we keep a curated `data/psa_specs.json` (card_id→specID,
  looked up via web search → the number in a psacard.com `/spec/psa/<id>` URL),
  cache 14 days, ~100 calls/day free tier. Quick check on the leaderboard CONFIRMS
  it differentiates: overvalued chase cards (Umbreon 19k / Mew 48k graded) dwarf
  an undervalued peer (Eevee 8.7k). NOT yet a model feature — pop is a snapshot
  (no history), so it can't be backtested against the past; grow the spec map +
  accrue, then validate going forward. Output: `data/psa_pop.json`.
- Black Bolt (zsv10pt5) + Mega Evolution (me1) pull odds are special-set
  ESTIMATES (`pullrates.py`), pending the owner's large-sample numbers. Shrouded
  Fable's Hyper is also a small-sample estimate (~1/240).
- The id-map is built from TODAY's live TCGplayer products (mild survivorship);
  delisted cards never enter the backtest. Re-runs need `data/tcgcsv_archive/`
  (gitignored, ~380MB) re-downloaded — `data/history/` panel is committed.
- Possible future signal: Google Trends "universal appeal".
