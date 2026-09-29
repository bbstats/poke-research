# Poke Research

A free, data-driven Pokémon TCG analytics site: card & set valuation built on open data.
Inspired by the Collectrics "IQ / Price Lab" approach; rebuilt from scratch on public APIs.

## What it does

- **Price Lab**: a clustered regression estimates each card's "fair" price from explainable
  signals; the gap vs the live market flags **undervalued / overvalued** cards (ranked by raw $
  difference, like the original).
- **Sealed EV**: expected value of ripping each set, `Σ (pull_rate × card_price)`, vs the box
  price → the best "bang for your buck" sets.
- **Signals**: *live:* character premium, scarcity, pull cost, months-since-release, in-set rank,
  plus eBay demand pressure and supply saturation from the daily eBay sweep (shown next to the
  model, not fed into it). *Partial:* PSA grading intensity, for a few hand-mapped cards.
  *Not wired in yet:* Google-Trends appeal. The price model itself uses three of these:
  character premium, scarcity and months since release (`config.FEATURES`). Pull cost and
  in-set rank are computed for display and sealed EV only.
- **Interactive**: open any card → "View Market Signals" → drag the sliders and watch the
  expected price recompute live (client-side, from the card's cluster coefficients).

## The model

Ridge regression on `log(market_price)`, **one model per rarity cluster** (+ a global fallback),
the clustering step the original author called out as necessary to fit thousands of cards.
**Squared log correlation, in-sample ≈ 0.90** across **2,936 priced cards / 15 sets / 16
clusters**, as of 2026-09-24 (`docs/data/meta.json` key `model_r2_log`, rebuilt daily). That is
the square of the correlation between log market price and log predicted price, measured on the
same cards the model was fit on. It is not a standard R² (1 - SSE/SST), it does not penalize
predictions that are all too high or all too low, and it is not out-of-sample accuracy; the Track
Record page is the forward test. The residual
`(market − expected)/expected` is the over/under signal. Chase cards (Umbreon, Mew, Charizard)
correctly sit at the top of expected price and read as "trading above fundamentals". That premium
is the demand and hype the eBay and PSA signals track, which stay out of the model until they
have enough history to backtest.

## Data

- Cards, sets, rarities, images, prices → [pokemontcg.io](https://pokemontcg.io) (TCGplayer market).
- Pull rates → estimated from each set's rarity structure (`pipeline/config.py` → `SETS`,
  `pipeline/pullrates.py` → `TIER_PROB`); official rates are never published.
- eBay market dynamics → **live**. The daily GitHub Action (`.github/workflows/daily-refresh.yml`)
  runs `collectors/ebay.py` against the eBay Browse API, snapshotting active listings for a fixed,
  price-ordered set of the most valuable cards. `pipeline/market_dynamics.py` turns the
  day-over-day diffs into demand pressure and supply saturation. Without eBay keys the collector
  writes a neutral snapshot instead of failing.
- PSA pop → `collectors/psa.py` calls the PSA Public API, but it runs by hand, not in the daily
  Action, and its hand-curated spec map (`data/psa_specs.json`) covers 3 cards so far.
- Google Trends → a collector exists (`collectors/trends.py`) but is not wired into the build yet.

## A note on the eBay data

The daily snapshots in `data/snapshots/` hold per-card aggregates: active listing
counts, flow, and average price. The raw eBay item IDs behind those aggregates are
deliberately **not** committed. They are kept in the GitHub Actions cache, because
`collectors.ebay.diff_snapshots` only needs yesterday's IDs to count new and ended
listings exactly. Each run saves a new cache entry, and GitHub deletes entries that
go unused for 7 days. The step that strips the IDs fails the run if it errors, and a
separate check (`scripts/check_no_item_ids.py`) refuses the commit if any staged
file still has an `item_ids` key. See `scripts/ebay_ids_sidecar.py`.

## Run it locally

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pipeline.fetch    # pull fresh cards + prices + a daily snapshot
./.venv/bin/python -m pipeline.build    # compute EV + model + scores -> docs/data/*.json
cd docs && python3 -m http.server       # open http://localhost:8000
```

Run `fetch` + `build` on a daily cron to keep prices current and grow the history.

## Layout

```
pipeline/   fetch, pullrates, ev, signals, market_dynamics, model, build, config
collectors/ ebay.py            (daily eBay listing-snapshot collector, run by the daily Action)
data/       normalized/, snapshots/   (source data + price history)
docs/       index.html, app.js, styles.css, data/*.json   (the static site Netlify serves)
DESIGN.md   the visual system (Miro-inspired)
CONTRACT.md the build spec every module conforms to
```

## Caveats

Estimates, not financial advice. Pull-rate and pack-price inputs are rough and tunable. Built for
fun and learning. Card data via pokemontcg.io; design language adapted from Miro. Not affiliated
with Nintendo / The Pokémon Company / Collectrics.

- **The Track Record backtest separates in-sample from out-of-sample.** The model's features,
  gates and price floor were chosen on the price history up to 2026-06-11, so those numbers are
  likely optimistic; later weeks, from the site's own daily prices, are reported separately but
  are only a few overlapping months. Trades buy a week after the signal, the popularity input
  uses only the 2020 poll (no hand-pinned overrides), and naive baselines are scored alongside.
  It is run by hand (`python -m pipeline.backtest`), not by the daily refresh.
- **Pull rates are estimates, not official odds.** Most come from community pack-opening
  samples, but some tiers are still rough estimates or placeholders (marked in the comments in
  `pipeline/pullrates.py`), so sealed EV is approximate.
- **"Fair price" is a model estimate.** It is less reliable for small clusters: as of 2026-09-25
  the smallest cluster models are fit on 12 or 13 cards for three features plus an intercept.
  The card view on the site shows how many cards each estimate is fit on.
