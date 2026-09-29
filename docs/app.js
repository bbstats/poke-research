/* ============================================================
   Poke Research — frontend app (vanilla JS, no build step)
   Loads ./data/*.json, hash routing, live price model recompute.
   ============================================================ */
'use strict';

/* ---------- tiny DOM helpers ---------- */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

/* ---------- formatting ---------- */
const USD = (n) => (n == null || Number.isNaN(n))
  ? '—'
  : '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const USD0 = (n) => (n == null || Number.isNaN(n))
  ? '—'
  : '$' + Number(n).toLocaleString('en-US', { maximumFractionDigits: 0 });
const PCT = (n, digits = 1) => (n == null || Number.isNaN(n)) ? '—' : (n * 100).toFixed(digits) + '%';
const signedPct = (n, digits = 1) => {
  if (n == null || Number.isNaN(n)) return '—';
  const v = n * 100;
  return (v > 0 ? '+' : '') + v.toFixed(digits) + '%';
};
const PLACEHOLDER = 'data:image/svg+xml;utf8,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="56"><rect width="40" height="56" rx="8" fill="#F5F5F7"/><text x="20" y="32" font-size="9" fill="#9B98B0" text-anchor="middle" font-family="sans-serif">card</text></svg>'
);
function imgFallback(node) { node.onerror = null; node.src = PLACEHOLDER; }

/* Freshness shown on the home page and footer.
   meta.prices_as_of = the last time prices were fetched live for every set.
   meta.built_at     = when the site was rebuilt, which can happen on cached prices.
   Only prices_as_of may be called "Prices refreshed". Data built before that
   field existed has only built_at, so we say "Site built" and claim nothing
   about the prices. */
function ageOf(iso) {
  if (!iso) return null;
  const s = String(iso);
  /* A bare YYYY-MM-DD is a calendar date: parse it as local midnight, or it
     would be read as UTC and could show the previous day. */
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(s);
  const d = dateOnly ? new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) : new Date(s);
  if (Number.isNaN(d.getTime())) return null;
  /* Count calendar days in the viewer's time zone, so the label and the date
     always agree (a build from last night reads "Yesterday", not "Today"). */
  const day0 = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((day0(new Date()) - day0(d)) / 86400000);
  const pad = (n) => String(n).padStart(2, '0');
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const label = days <= 0 ? 'Today' : days === 1 ? 'Yesterday' : `${days} days ago`;
  return { date, days, label };
}
function refreshInfo(meta) {
  const p = ageOf(meta && meta.prices_as_of);
  if (p) {
    const sub = p.days >= 3 ? `as of ${p.date} · refresh recommended` : `as of ${p.date}`;
    return { kicker: 'Prices refreshed', label: p.label, sub, days: p.days,
      footer: `Prices last refreshed ${p.date} · ${p.label}` };
  }
  const b = ageOf(meta && meta.built_at);
  if (b) {
    return { kicker: 'Site built', label: b.label, sub: `on ${b.date}`, days: b.days,
      footer: `Site built ${b.date}` };
  }
  return { kicker: 'Site built', label: 'Unknown', sub: 'no date published', days: null,
    footer: 'Site build date unknown' };
}

/* Per-set "sealed heat" line from the manual TCGplayer feed (pack momentum +
   today's velocity vs supply). Price up = green (bullish), down = red. */
function sealedLine(sealed) {
  const p = sealed && sealed.pack;
  if (!p) return null;
  const ch = p.change_24h ?? 0;
  const up = ch >= 0;
  const col = up ? 'var(--success-accent)' : 'var(--brand-red-dark)';
  const listed = p.listings == null ? '—'
    : p.listings >= 1000 ? (p.listings / 1000).toFixed(1) + 'k' : String(p.listings);
  /* These figures are from a hand-pasted TCGplayer dump, so show the day they
     describe instead of implying they are today's 24h numbers. */
  const when = sealed.as_of ? `24h to ${sealed.as_of}` : '24h';
  return el('div', { class: 'lb-sub', html:
    `📦 ${USD(p.price)} <span style="color:${col};font-weight:600">${up ? '▲' : '▼'} ${Math.abs(ch).toFixed(1)}%</span>` +
    ` · ${(p.sold_today ?? 0).toLocaleString()} sold · ${listed} listed <span style="opacity:.55">${when}</span>` });
}

/* Sealed (pack / ETB) prices come from a TCGplayer dump pasted by hand, not a
   daily feed, so the Sets page shows their date and flags them once they are
   older than this. Verdicts stay visible; the reader decides. */
const SEALED_STALE_DAYS = 14;
function sealedPriceInfo(sets) {
  /* Freshness is judged by the OLDEST pasted date, so one fresh paste cannot
     make the whole page look fresh; a range is shown when the dates differ
     (Codex verification, follow-up 4). */
  const dates = (sets || []).map((s) => s.sealed && s.sealed.as_of).filter(Boolean).sort();
  const oldest = dates.length ? dates[0] : null;
  const newest = dates.length ? dates[dates.length - 1] : null;
  const age = ageOf(oldest);
  return {
    oldest,
    newest,
    label: !oldest ? null : (oldest === newest ? oldest : `${oldest} to ${newest}`),
    days: age ? age.days : null,
    stale: !!age && age.days > SEALED_STALE_DAYS,
    nPasted: dates.length,
    nSets: (sets || []).length,
  };
}

/* residual_pct convention (from real data):
     residual_pct < 0  -> market below expected -> UNDERVALUED (green / down pill)
     residual_pct > 0  -> market above expected -> OVERVALUED  (red / up pill)        */
/* Model verdict band (matches PRED_BAND in pipeline/build.py): a card within
   ±15% of its expected price is "fair", NOT a deal — so we don't flag a 2% gap
   as undervalued/overvalued. Honest verdict for the pill, modal, and slider. */
const CARD_BAND = 0.15;
/* 'No edge' = the model can't make a trustworthy over/under call, so we show no
   verdict. The build (pipeline/_edge) bakes a per-card `edge` flag + `edge_reason`
   (mature mid-price dead zone, OR a chase-grail premium the 3 features can't price).
   We read that flag; the old price-based check stays as a fallback for cached data. */
const GATE_DEAD_LO = 20, GATE_DEAD_HI = 100, GATE_FRESH_MONTHS = 35 / 30.4375;
function cardHasEdge(card) {
  if (!card) return true;
  if (typeof card.edge === 'boolean') return card.edge;
  const p = card.market_price || 0;
  if (p >= GATE_DEAD_LO && p < GATE_DEAD_HI) {
    const msr = card.features && card.features.months_since_release;
    if (msr == null || msr > GATE_FRESH_MONTHS) return false;  // mature mid-price dead zone
  }
  return true;
}
function noEdgeReason(card) {
  return (card && card.edge_reason) || 'the model has no measured edge here';
}
function residualVerdict(residual, card) {
  if (card && !cardHasEdge(card)) return { dir: 'flat', label: 'no edge', cls: 'delta--flat', pd: 'pd-flat', noEdge: true };
  if (residual == null) return { dir: 'flat', label: 'fair', cls: 'delta--flat', pd: 'pd-flat' };
  if (residual < -CARD_BAND) return { dir: 'under', label: 'undervalued', cls: 'delta--down', pd: 'pd-down' };
  if (residual >  CARD_BAND) return { dir: 'over',  label: 'overvalued',  cls: 'delta--up',   pd: 'pd-up' };
  return { dir: 'flat', label: 'fair', cls: 'delta--flat', pd: 'pd-flat' };
}
function deltaPill(residual, card) {
  if (card && !cardHasEdge(card)) return el('span', { class: 'delta delta--flat', title: noEdgeReason(card), text: 'no edge' });
  if (residual == null) return el('span', { class: 'delta delta--flat', text: '—' });
  const v = residualVerdict(residual);
  return el('span', { class: `delta ${v.cls}`, text: signedPct(residual) });
}
function iqChip(iq) {
  return el('span', { class: 'chip chip--iq', html: `IQ&nbsp;${iq != null ? iq.toFixed(0) : '—'}` });
}

/* ---------- plain-language market-signal labels (baked in build.py/signal_labels.py) ----------
   DESCRIPTIVE only — they summarize recent price + eBay demand/supply flow. They are
   NOT predictions and never feed the expected price. */
const SIGNAL_TONES = {
  hot: 'sig--hot', opportunity: 'sig--opp', warm: 'sig--warm', neutral: 'sig--neutral',
  cool: 'sig--cool', cold: 'sig--cold', caution: 'sig--caution',
};
/* compact pill for grids/rows — hidden when there's nothing to say (awaiting data) */
function signalPill(card) {
  const s = card && card.signal;
  if (!s || !s.label || s.confidence === 'none') return null;
  const pill = el('span', { class: `sig-pill ${SIGNAL_TONES[s.tone] || 'sig--neutral'}`, text: s.label });
  if (s.detail) pill.title = s.detail;
  return pill;
}
/* full banner for the modal (label + supporting numbers), like the reference card view */
function signalBanner(card) {
  const s = card && card.signal;
  if (!s || !s.label) return null;
  const kids = [el('span', { class: 'sig-banner-label', text: s.label })];
  if (s.detail) kids.push(el('span', { class: 'sig-banner-detail', text: s.detail }));
  if (s.confidence === 'price_only') kids.push(el('span', { class: 'sig-banner-flag', text: 'price only · eBay flow pending' }));
  return el('div', { class: `sig-banner ${SIGNAL_TONES[s.tone] || 'sig--neutral'}` }, kids);
}
/* per-card eBay flow stats (active / new / sold est), shown only with a live read */
function flowStats(dyn) {
  if (!dyn || dyn.status !== 'ok') return null;
  const cell = (label, val) => el('div', { class: 'flow-cell' }, [
    el('div', { class: 'flow-num', text: val == null ? '—' : val.toLocaleString() }),
    el('div', { class: 'flow-label', text: label }),
  ]);
  return el('div', { class: 'flow-stats' }, [
    el('div', { class: 'flow-title', text: 'eBay market dynamics · 7-day' }),
    el('div', { class: 'flow-row' }, [
      cell('active listings', dyn.active_listings),
      cell('new (7d)', dyn.new_7d),
      cell('sold est (7d)', dyn.sold_7d),
    ]),
  ]);
}
/* PSA grading intensity (population) — real collector-appeal evidence, shown only
   for cards with a curated specID. DISPLAY only; not a price-model input. */
function psaPanel(card) {
  const p = card && card.psa_pop;
  if (!p || !p.total) return null;
  const gem = p.psa10_rate != null ? (p.psa10_rate * 100).toFixed(1) + '%' : '—';
  const cell = (label, val) => el('div', { class: 'flow-cell' }, [
    el('div', { class: 'flow-num', text: val }),
    el('div', { class: 'flow-label', text: label }),
  ]);
  return el('div', { class: 'flow-stats' }, [
    el('div', { class: 'flow-title', text: 'PSA population · collector appeal' }),
    el('div', { class: 'flow-row' }, [
      cell('graded total', p.total.toLocaleString()),
      cell('PSA 10', (p.psa10 || 0).toLocaleString()),
      cell('gem rate', gem),
    ]),
  ]);
}

/* ---------- global state ---------- */
const STATE = {
  cards: null, sets: null, leaderboard: null, model: null, meta: null,
  backtest: null, watchlist: null, sealedTrend: null, priceHistory: null,
  myWatch: {}, byId: new Map(), loaded: false, error: null,
};

/* ---------- My Cards watchlist (client-side, localStorage) ----------
   A personal card portfolio lives entirely in the browser (no account/backend):
   { cardId: {added:'YYYY-MM-DD', priceAtAdd:number} }. Performance is charted from
   the exported price_history.json, which deepens one point/day as snapshots accrue. */
const WATCH_KEY = 'pr_mywatch_v1';
function loadWatch() {
  try { return JSON.parse(localStorage.getItem(WATCH_KEY)) || {}; } catch (_) { return {}; }
}
function saveWatch() { try { localStorage.setItem(WATCH_KEY, JSON.stringify(STATE.myWatch)); } catch (_) {} }
function isWatched(id) { return !!STATE.myWatch[id]; }
function toggleWatch(card) {
  const w = STATE.myWatch;
  if (w[card.id]) delete w[card.id];
  else w[card.id] = { added: new Date().toISOString().slice(0, 10), priceAtAdd: card.market_price ?? null };
  saveWatch();
}

async function loadData() {
  const files = ['cards', 'sets', 'leaderboard', 'model', 'meta'];
  const results = await Promise.all(files.map(async (f) => {
    const res = await fetch(`./data/${f}.json`, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`Failed to load ${f}.json (HTTP ${res.status})`);
    return res.json();
  }));
  [STATE.cards, STATE.sets, STATE.leaderboard, STATE.model, STATE.meta] = results;
  STATE.byId = new Map(STATE.cards.map((c) => [c.id, c]));
  /* backtest.json is optional — never block boot if it's absent */
  try {
    const r = await fetch('./data/backtest.json', { cache: 'no-cache' });
    if (r.ok) STATE.backtest = await r.json();
  } catch (_) { /* no track record yet */ }
  /* watchlist.json is optional too — written by the eBay deal-watcher */
  try {
    const r = await fetch('./data/watchlist.json', { cache: 'no-cache' });
    if (r.ok) STATE.watchlist = await r.json();
  } catch (_) { /* no watchlist yet */ }
  /* sealed_trend.json is optional too — TCGplayer price-history trend */
  try {
    const r = await fetch('./data/sealed_trend.json', { cache: 'no-cache' });
    if (r.ok) STATE.sealedTrend = await r.json();
  } catch (_) { /* no trend yet */ }
  /* price_history.json is optional — powers the My Cards performance chart */
  try {
    const r = await fetch('./data/price_history.json', { cache: 'no-cache' });
    if (r.ok) STATE.priceHistory = await r.json();
  } catch (_) { /* no history yet */ }
  STATE.myWatch = loadWatch();
  STATE.loaded = true;
}

/* ============================================================
   Live price model — recompute expected price client-side
   expected = exp(intercept + Σ coef_i * ((x_i - mean_i)/std_i))
   ============================================================ */
function modelForCluster(clusterName) {
  const m = STATE.model;
  if (clusterName && m.clusters && m.clusters[clusterName]) return m.clusters[clusterName];
  return m.global;
}
function computeExpected(clusterModel, featureValues) {
  let z = clusterModel.intercept;
  clusterModel.features.forEach((f, i) => {
    const std = clusterModel.stds[i];
    const x = featureValues[f];
    if (std === 0 || x == null || Number.isNaN(x)) return;
    z += clusterModel.coef[i] * ((x - clusterModel.means[i]) / std);
  });
  return Math.exp(z);
}

/* ============================================================
   View renderers
   ============================================================ */
const viewRoot = () => $('#view');

function setView(nodes) {
  const root = viewRoot();
  root.innerHTML = '';
  [].concat(nodes).forEach((n) => n && root.appendChild(n));
  window.scrollTo({ top: 0, behavior: 'auto' });
}

/* ---- shared: leaderboard table ---- */
function leaderboardTable(rows, opts = {}) {
  const head = el('thead', {}, el('tr', {}, [
    el('th', { class: 'lb-rank', text: '#' }),
    el('th', { text: 'Card' }),
    el('th', { class: 'num', text: 'Market' }),
    el('th', { class: 'num', text: 'Expected' }),
    el('th', { class: 'num', text: opts.deltaLabel || 'Signal' }),
    el('th', { class: 'num', text: 'IQ' }),
  ]));
  const body = el('tbody', {}, rows.map((r, i) => {
    const thumb = el('img', { class: 'lb-thumb', src: r.image_small || PLACEHOLDER, alt: r.name, loading: 'lazy' });
    thumb.addEventListener('error', () => imgFallback(thumb));
    const tr = el('tr', { class: 'clickable', dataset: { id: r.id } }, [
      el('td', { class: 'lb-rank num', text: String(i + 1) }),
      el('td', {}, el('div', { class: 'lb-card-cell' }, [
        thumb,
        el('div', {}, [
          el('div', { class: 'lb-name', text: r.name }),
          el('div', { class: 'lb-sub', text: `${r.set_name} · #${r.number} · ${r.rarity}` }),
          signalPill(r),
        ]),
      ])),
      el('td', { class: 'num lb-price', text: USD(r.market_price) }),
      el('td', { class: 'num lb-price lb-price-exp', text: USD(r.expected_price) }),
      el('td', { class: 'num' }, deltaPill(r.residual_pct)),
      el('td', { class: 'num' }, iqChip(r.iq_score)),
    ]);
    tr.addEventListener('click', () => openModal(r.id));
    return tr;
  }));
  return el('div', { class: 'table-card' },
    el('div', { class: 'table-scroll' }, el('table', { class: 'lb' }, [head, body])));
}

/* ============================================================
   1. LANDING / HERO
   ============================================================ */
function viewHome() {
  const meta = STATE.meta;
  const lb = STATE.leaderboard;

  /* hero */
  const heroCardRows = lb.undervalued.slice(0, 3).map((r) => {
    const img = el('img', { src: r.image_small || PLACEHOLDER, alt: r.name, loading: 'lazy' });
    img.addEventListener('error', () => imgFallback(img));
    const row = el('div', { class: 'hero-card-row' }, [
      img,
      el('div', { class: 'hcr-main' }, [
        el('div', { class: 'hcr-name', text: r.name }),
        el('div', { class: 'hcr-set', text: r.set_name }),
      ]),
      deltaPill(r.residual_pct),
    ]);
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => openModal(r.id));
    return row;
  });

  const hero = el('section', { class: 'hero container' },
    el('div', { class: 'hero-grid' }, [
      el('div', {}, [
        el('span', { class: 'chip chip--lavender hero-eyebrow', html: `Beta · ${meta.priced.toLocaleString()} priced cards` }),
        el('h1', { class: 'hero-display' }, [
          'The price lab for ',
          el('span', { class: 'accent', text: 'Pokémon' }),
          ' cards.',
        ]),
        el('p', { class: 'subtitle hero-sub', text:
          'A clustered price model estimates what every card should cost, flags the under- and over-valued, and ranks sealed product by expected value — all from free pokemontcg.io data.' }),
        el('div', { class: 'hero-actions' }, [
          el('a', { class: 'button button-primary', href: '#/pricelab', text: 'Explore the Price Lab' }),
          el('a', { class: 'button button-secondary', href: '#/sets', text: 'See sealed EV →' }),
        ]),
      ]),
      el('div', { class: 'hero-card' }, [
        el('div', { class: 'hero-card-title' }, [
          el('span', { class: 'caption-bold', text: 'Most undervalued right now' }),
          el('span', { class: 'chip chip--teal', text: 'live model' }),
        ]),
        el('div', { class: 'hero-card-rows' }, heroCardRows),
      ]),
    ]));

  /* pastel stat cards from meta.json */
  const fresh = refreshInfo(meta);
  const statRow = el('div', { class: 'stat-row container' }, [
    statCard('bg-yellow', 'Tracked', meta.cards.toLocaleString(), 'cards in the corpus'),
    statCard('bg-teal', 'Coverage', String(meta.sets), 'Scarlet & Violet sets'),
    statCard('bg-coral', fresh.kicker, fresh.label, fresh.sub),
  ]);

  /* how it works (the signal count comes from model.json, like the feature names) */
  const nFeat = ((STATE.model && STATE.model.features) || []).length;
  const nFeatWord = ['No', 'One', 'Two', 'Three', 'Four', 'Five', 'Six'][nFeat] || String(nFeat);
  const explain = el('section', { class: 'section container' }, [
    el('div', { class: 'section-head' }, [
      el('div', {}, [
        el('p', { class: 'micro section-eyebrow', text: 'How it works' }),
        el('h2', { class: 'h2', text: `${nFeatWord} signals, one honest model` }),
        el('p', { class: 'section-sub', text:
          'The model uses only inputs computed from card data and set composition. eBay demand and PSA grading data are shown next to it, not fed into it, until they have enough history to test.' }),
      ]),
    ]),
    el('div', { class: 'explainer-row' }, [
      explainerCard('bg-yellow', '◎', 'Sealed EV', 'Per-card pull rates × market prices roll up to an expected value per pack and per Elite Trainer Box, versus what each actually costs.'),
      explainerCard('bg-teal', '◈', 'Fair-price model', `A ridge regression per rarity cluster predicts log price from ${modelFeatureList()}, recomputed live as you move the sliders.`),
      explainerCard('bg-coral', '◇', 'Shown, not modeled', 'eBay demand pressure and supply saturation come from a daily eBay listing sweep of the most valuable cards. They sit next to the model, not inside it. PSA grading data covers a few cards so far, and Google Trends is not wired in yet.'),
    ]),
  ]);

  /* quick links into views */
  const links = el('section', { class: 'section section--tight container' },
    el('div', { class: 'explainer-row' }, [
      quickLink('#/sets', 'bg-lavender', 'Sets / Market Trends', 'Sealed EV leaderboard — best bang for the buck.'),
      quickLink('#/pricelab', 'bg-rose', 'Price Lab', 'Undervalued & overvalued cards with live deltas.'),
      quickLink('#/search', 'bg-teal', 'Search', `Filter all ${meta.cards.toLocaleString()} cards by name or set.`),
    ]));

  setView([hero, statRow, explain, links]);
  highlightNav(null);
}
/* The model's actual inputs, read from model.json (config.FEATURES), so this
   copy cannot drift from the code, e.g. "character premium, scarcity score and
   months since release". */
function modelFeatureList() {
  const m = STATE.model || {};
  const meta = m.feature_meta || {};
  const names = (m.features || []).map((f) => String((meta[f] && meta[f].label) || f).toLowerCase());
  if (!names.length) return 'its input signals';
  if (names.length === 1) return names[0];
  return names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
}
function statCard(bg, kicker, big, label) {
  return el('div', { class: `stat-card ${bg}` }, [
    el('div', { class: 'stat-kicker', text: kicker }),
    el('div', { class: 'stat-display', text: big }),
    el('div', { class: 'stat-label', text: label }),
  ]);
}
function explainerCard(bg, icon, title, body) {
  return el('div', { class: 'explainer-card' }, [
    el('div', { class: `explainer-icon ${bg}`, text: icon }),
    el('h4', { class: 'h4', text: title }),
    el('p', { text: body }),
  ]);
}
function quickLink(href, bg, title, body) {
  return el('a', { class: `explainer-card`, href }, [
    el('div', { class: `explainer-icon ${bg}`, text: '→' }),
    el('h4', { class: 'h4', text: title }),
    el('p', { text: body }),
  ]);
}

/* ============================================================
   2. SETS / MARKET TRENDS — EV leaderboard
   ============================================================ */
function viewSets() {
  const sets = STATE.sets.slice().sort((a, b) => b.signal_pct - a.signal_pct);

  const head = el('thead', {}, el('tr', {}, [
    el('th', { class: 'lb-rank', text: '#' }),
    el('th', { text: 'Set' }),
    el('th', { class: 'num', text: 'Pack' }),
    el('th', { class: 'num', text: 'EV / pack' }),
    el('th', { class: 'num', text: 'Pack verdict' }),
    el('th', { class: 'num', text: 'ETB' }),
    el('th', { class: 'num', text: 'EV / ETB' }),
    el('th', { class: 'num', text: 'ETB verdict' }),
  ]));
  const body = el('tbody', {}, sets.map((s, i) => {
    const logo = el('img', { class: 'lb-logo', src: s.logo || PLACEHOLDER, alt: s.name, loading: 'lazy' });
    logo.addEventListener('error', () => imgFallback(logo));
    return el('tr', {}, [
      el('td', { class: 'lb-rank num', text: String(i + 1) }),
      el('td', {}, el('div', { class: 'lb-set-cell' }, [
        logo,
        el('div', {}, [
          el('div', { class: 'lb-name', text: s.name }),
          el('div', { class: 'lb-sub', text: `${s.series} · ${s.packs_per_etb} packs/ETB` }),
          sealedLine(s.sealed),
        ]),
      ])),
      el('td', { class: 'num lb-price', text: USD(s.pack_price) }),
      el('td', { class: 'num lb-price', text: USD(s.ev_per_pack) }),
      el('td', { class: 'num' }, evSignalPill(s.pack_signal_pct)),
      el('td', { class: 'num lb-price', text: USD0(s.etb_price) }),
      el('td', { class: 'num lb-price', text: USD0(s.ev_per_etb) }),
      el('td', { class: 'num' }, evSignalPill(s.etb_signal_pct)),
    ]);
  }));
  const table = el('div', { class: 'table-card' },
    el('div', { class: 'table-scroll' }, el('table', { class: 'lb' }, [head, body])));

  const sealed = sealedPriceInfo(STATE.sets);
  const sealedChip = sealed.label
    ? el('span', { class: `chip ${sealed.stale ? 'chip--yellow' : 'chip--teal'}`,
        text: `sealed prices as of ${sealed.label}` })
    : el('span', { class: 'chip chip--yellow', text: 'sealed prices: estimates' });
  const staleNote = sealed.stale
    ? el('p', { class: 'stale-note', text:
        (sealed.oldest === sealed.newest
          ? `Stale: the pasted sealed prices are ${sealed.days} days old (TCGplayer, ${sealed.oldest}). `
          : `Stale: the oldest pasted sealed prices are ${sealed.days} days old (TCGplayer pastes from ${sealed.label}; each set shows its own date). `) +
        'The EV verdicts below compare today’s card prices with those old pack and ETB prices, so check current sealed prices before acting on a verdict.' })
    : null;

  const section = el('section', { class: 'section container' }, [
    el('div', { class: 'section-head' }, [
      el('div', {}, [
        el('p', { class: 'micro section-eyebrow', text: 'Sets · Market Trends' }),
        el('h2', { class: 'h2', text: 'Undervalued & overvalued sets' }),
        el('p', { class: 'section-sub', text:
          'Sealed expected value = Σ (per-card pull rate × market price), shown per single pack and per Elite Trainer Box (9 packs), each vs what that product costs. Green “Undervalued” = the cards inside are worth more than the sealed product (good to rip / hold); red “Overvalued” = you pay a premium for sealed (buy singles). A value signal, not a price-trend forecast.' }),
      ]),
      sealedChip,
    ]),
    staleNote,
    table,
    el('p', { class: 'caption', html: 'Pull rates are estimates, not official odds (official odds are never published). Most come from community pack-opening samples, but some tiers are still rough estimates or placeholders, so treat each set’s EV as approximate. ' +
      `Card prices refresh daily; pack and ETB prices do not. ${sealed.nPasted} of ${sealed.nSets} sets use a TCGplayer price paste` +
      (sealed.label ? ` from ${sealed.label}` : '') + '; the rest use rough estimates set by hand in the config (Paldean Fates and Shrouded Fable ETB prices are lower-confidence).' }),
  ].filter(Boolean));

  setView(section);
  highlightNav('sets');
}
function evSignalPill(pct) {
  /* positive signal_pct = sealed box cheaper than the cards inside = undervalued (green);
     negative = paying a premium for sealed = overvalued (red) */
  if (pct == null) return el('span', { class: 'delta delta--flat', text: '—' });
  const verdict = pct >= 0.10 ? 'Undervalued' : pct <= -0.10 ? 'Overvalued' : 'Fair';
  const cls = pct >= 0.10 ? 'delta delta--down' : pct <= -0.10 ? 'delta delta--up' : 'delta delta--flat';
  return el('span', { class: cls, text: `${verdict} ${signedPct(pct, 0)}` });
}

/* ============================================================
   3. PRICE LAB — undervalued + overvalued
   ============================================================ */
function viewPriceLab() {
  const lb = STATE.leaderboard;

  const under = el('div', {}, [
    el('div', { class: 'lab-col-head' }, [
      el('span', { class: 'chip chip--teal', text: 'Undervalued' }),
      el('h3', { class: 'h3', text: 'Market below model' }),
    ]),
    leaderboardTable(lb.undervalued, { deltaLabel: 'Discount' }),
  ]);
  const over = el('div', {}, [
    el('div', { class: 'lab-col-head' }, [
      el('span', { class: 'chip', style: 'background:var(--brand-red);color:var(--brand-red-dark)', text: 'Overvalued' }),
      el('h3', { class: 'h3', text: 'Market above model' }),
    ]),
    leaderboardTable(lb.overvalued, { deltaLabel: 'Premium' }),
  ]);

  const section = el('section', { class: 'section container' }, [
    el('div', { class: 'section-head' }, [
      el('div', {}, [
        el('p', { class: 'micro section-eyebrow', text: 'Price Lab' }),
        el('h2', { class: 'h2', text: 'Undervalued & overvalued cards' }),
        el('p', { class: 'section-sub', text:
          `The model predicts a fair price for each card from ${modelFeatureList()}, with a separate model per rarity cluster. The delta is how far the live market sits from that estimate. For how accurate these calls actually are, see the Track Record. Open any card to tune the signals yourself.` }),
      ]),
      el('span', { class: 'chip chip--lavender',
        title: 'Square of the correlation between log market price and log model price, on the same cards the model was fit on. Not a standard R², and not out-of-sample accuracy (see the Track Record).',
        html: `Squared log correlation, in-sample&nbsp;${(STATE.meta.model_r2_log ?? 0).toFixed(2)} · ${STATE.meta.clusters} clusters` }),
    ]),
    el('div', { class: 'lab-grid' }, [under, over]),
    el('p', { class: 'caption', html: 'Expected price is a statistical <strong>estimate</strong>, not an appraisal, and it is less reliable for cards in small clusters (open a card to see how many cards its estimate is fit on). Green = trading below the model (potential value); red = trading above (potential premium).' }),
  ]);

  setView(section);
  highlightNav('pricelab');
}

/* ============================================================
   4. MOVERS — saturation-shift when live, price-fallback stub otherwise
   ============================================================ */
const isSaturationBasis = (basis) => basis === 'saturation_shift';

/* supply-saturation tag: <1 tightening (green), >1 loosening (red), ~1 steady */
function saturationTag(sat) {
  if (sat == null) return el('span', { class: 'delta delta--flat', text: '—' });
  const shiftPct = Math.abs(sat - 1) * 100;
  if (sat < 0.97) return el('span', { class: 'delta delta--down', text: `tightening −${shiftPct.toFixed(0)}%` });
  if (sat > 1.03) return el('span', { class: 'delta delta--up', text: `loosening +${shiftPct.toFixed(0)}%` });
  return el('span', { class: 'delta delta--flat', text: 'steady' });
}

/* live movers table: rank by |saturation−1|, show tag + active listings + 7d sold */
function moversTableLive(rows) {
  const head = el('thead', {}, el('tr', {}, [
    el('th', { class: 'lb-rank', text: '#' }),
    el('th', { text: 'Card' }),
    el('th', { class: 'num', text: 'Saturation' }),
    el('th', { class: 'num', text: 'Supply shift' }),
    el('th', { class: 'num', text: 'Active' }),
    el('th', { class: 'num', text: 'Sold 7d' }),
  ]));
  const body = el('tbody', {}, rows.map((r, i) => {
    const dyn = r.dynamics || {};
    const awaiting = dyn.status === 'awaiting_data';
    const sat = dyn.supply_saturation;
    const thumb = el('img', { class: 'lb-thumb', src: r.image_small || PLACEHOLDER, alt: r.name, loading: 'lazy' });
    thumb.addEventListener('error', () => imgFallback(thumb));
    const tr = el('tr', { class: 'clickable', dataset: { id: r.id } }, [
      el('td', { class: 'lb-rank num', text: String(i + 1) }),
      el('td', {}, el('div', { class: 'lb-card-cell' }, [
        thumb,
        el('div', {}, [
          el('div', { class: 'lb-name', text: r.name }),
          el('div', { class: 'lb-sub', text: `${r.set_name} · #${r.number} · ${r.rarity}` }),
          signalPill(r),
        ]),
      ])),
      el('td', { class: 'num lb-price', text: awaiting ? '—' : `${Number(sat).toFixed(2)}×` }),
      el('td', { class: 'num' }, awaiting ? el('span', { class: 'badge-stub', text: 'awaiting data' }) : saturationTag(sat)),
      el('td', { class: 'num lb-price', text: awaiting || dyn.active_listings == null ? '—' : dyn.active_listings.toLocaleString() }),
      el('td', { class: 'num lb-price', text: awaiting || dyn.sold_7d == null ? '—' : dyn.sold_7d.toLocaleString() }),
    ]);
    tr.addEventListener('click', () => openModal(r.id));
    return tr;
  }));
  return el('div', { class: 'table-card' },
    el('div', { class: 'table-scroll' }, el('table', { class: 'lb' }, [head, body])));
}

function viewMovers() {
  const lb = STATE.leaderboard;
  const live = isSaturationBasis(lb.movers_basis);

  const banner = live
    ? el('div', { class: 'table-card', style: 'padding:20px 24px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:24px;box-shadow:none;background:var(--surface-soft)' }, [
        el('span', { class: 'chip chip--teal', text: 'live · eBay supply' }),
        el('p', { class: 'body-sm', style: 'margin:0;color:var(--slate);max-width:64ch', html:
          'Ranked by <strong>supply-saturation shift</strong> = mean active listings over the last 7 days vs the last 30 days. <span style="color:var(--success-accent);font-weight:600">Tightening</span> (below 1×) means supply is drying up faster than it’s replenishing; <span style="color:var(--brand-red-dark);font-weight:600">loosening</span> (above 1×) means listings are piling up. Sold figures are estimated by diffing daily active-listing snapshots.' }),
      ])
    : el('div', { class: 'table-card', style: 'padding:20px 24px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:24px;box-shadow:none;background:var(--surface-soft)' }, [
        el('span', { class: 'badge-stub', text: 'awaiting eBay data' }),
        el('p', { class: 'body-sm', style: 'margin:0;color:var(--slate);max-width:62ch', html:
          'Real movers compare 7 days of daily <strong>eBay</strong> active-listing snapshots with 30 days of them, and this build has no current eBay observations to compare (too little history, or the latest sweep returned no valid counts). Until it does, this shows the cards whose market price diverges most from the model.' }),
      ]);

  const section = el('section', { class: 'section container' }, [
    el('div', { class: 'section-head' }, [
      el('div', {}, [
        el('p', { class: 'micro section-eyebrow', text: 'Movers' }),
        el('h2', { class: 'h2', text: 'Biggest market movers' }),
        el('p', { class: 'section-sub', text: live
          ? 'Ranked by 7-day vs 30-day listing-supply shift. Tightening supply (green) tends to precede price strength; loosening supply (red) tends to precede softening.'
          : 'When supply data lands, this ranks cards by 7-day vs 30-day listing-supply shift (loosening vs tightening). For now it shows the largest price-vs-model gaps as a placeholder basis.' }),
      ]),
      live ? el('span', { class: 'chip chip--teal', text: 'live' }) : el('span', { class: 'badge-stub', text: 'stub · price fallback' }),
    ]),
    banner,
    live ? moversTableLive(lb.movers) : leaderboardTable(lb.movers, { deltaLabel: 'Price gap' }),
    el('p', { class: 'caption', html: live
      ? 'Basis: <code>saturation_shift</code> — mean active listings (7d) ÷ mean active listings (30d), inferred from daily eBay active-listing snapshots. Sold/unsold are estimated by day-over-day diffing, not confirmed sold comps.'
      : `Basis: <code>${lb.movers_basis || 'price_signal_fallback'}</code> — a stand-in until supply-saturation data exists. No eBay / sold-comp feed is implied.` }),
  ]);

  setView(section);
  highlightNav('movers');
}

/* ============================================================
   4b. WATCHLISTS — eBay sealed-product deal monitor
   ============================================================ */
function ebaySearchUrl(query) {
  /* live eBay search: fixed-price + auction, sorted lowest price + shipping */
  return `https://www.ebay.com/sch/i.html?_nkw=${encodeURIComponent(query)}&_sop=15`;
}

/* SITE display rule (user pref): show all Buy-It-Nows, but auctions only when they
   end within 24h — a far-off auction isn't actionable and its price will still climb. */
const SITE_AUCTION_WINDOW_H = 24;
function hoursUntil(endStr) {
  if (!endStr) return null;
  const t = Date.parse(endStr);
  return Number.isNaN(t) ? null : (t - Date.now()) / 3600000;
}
function listingShown(it) {
  if (!it) return false;
  if (it.kind !== 'Auction') return true;                  // Buy It Now: always shown
  const h = hoursUntil(it.end);
  return h != null && h >= 0 && h <= SITE_AUCTION_WINDOW_H;
}
function bestAuctionShown(best) {
  if (!best) return null;
  const h = hoursUntil(best.end);
  return (h != null && h >= 0 && h <= SITE_AUCTION_WINDOW_H) ? best : null;
}

function wlBestCell(icon, label, best, market) {
  if (!best || best.price == null) {
    return el('div', { class: 'wl-bestcell' }, [
      el('span', { class: 'wl-bestlabel', text: `${icon} ${label}` }),
      el('span', { class: 'wl-bestprice wl-bestprice--none', text: '—' }),
    ]);
  }
  const ends = best.end ? ` · ends ${String(best.end).slice(0, 10)}` : '';
  const pct = (market && best.price) ? Math.round((1 - best.price / market) * 100) : null;
  const under = (pct && pct > 0) ? ` · ${pct}% under` : '';
  // When the seller didn't list shipping, the headline price is item-only — say so.
  const shipWarn = best.ship == null
    ? el('span', { class: 'wl-bestship-warn',
      title: 'Seller didn’t list shipping — verify the real all-in price on eBay.',
      text: '⚠ + shipping not listed' })
    : null;
  return el('a', { class: 'wl-bestcell wl-bestcell--link', href: best.url || '#',
    target: '_blank', rel: 'noopener', title: best.title || '' }, [
    el('span', { class: 'wl-bestlabel', text: `${icon} ${label}` }),
    el('span', { class: 'wl-bestprice', text: USD0(best.price) }),
    el('span', { class: 'wl-bestmeta', text: `view${ends}${under}` }),
    shipWarn,
  ].filter(Boolean));
}

/* TCGplayer realized-price trend: one ▲/▼ chip per window. Up = heating (red),
   down = cooling (teal) — a buyer reads a downtrend as 'prices sliding'. */
function wlTrendPart(window, frac) {
  if (frac == null) return null;
  const up = frac > 0.005, down = frac < -0.005;
  const arrow = up ? '▲' : (down ? '▼' : '→');
  const cls = up ? 'wl-trend--up' : (down ? 'wl-trend--down' : 'wl-trend--flat');
  const pctText = (up || down) ? signedPct(frac, 0) : '0%';   // avoid a misleading -0%/+0%
  return el('span', { class: `wl-trendpart ${cls}`, title: `${window} TCGplayer market change` },
    `${window} ${arrow} ${pctText}`);
}
function wlTrend(t) {
  if (!t) return null;
  const parts = [wlTrendPart('30d', t.trend_30d), wlTrendPart('90d', t.trend_90d)].filter(Boolean);
  if (!parts.length) return null;
  return el('div', { class: 'wl-trend' }, [
    el('span', { class: 'wl-trendlabel', text: 'TCGplayer trend' }),
    ...parts,
  ]);
}

/* % change of the TCGplayer sealed series over the last `days` (client-side, for
   horizons we don't precompute). Returns null if the series doesn't reach back. */
function trendOver(series, days) {
  if (!series || series.length < 2) return null;
  const last = series[series.length - 1];
  const cutMs = Date.parse(last.date) - days * 86400000;
  let base = null;
  for (const pt of series) { if (Date.parse(pt.date) <= cutMs) base = pt; }
  if (!base || !base.market) return null;
  return (last.market - base.market) / base.market;
}

/* Forward-leaning "trajectory" read: a plain-language momentum classification from
   the 30/90/180d sealed move. NOT a guaranteed forecast and reprint-blind — it's the
   shape of the recent move, to be read alongside the print/reprint fundamentals. */
function wlTrajectory(t) {
  if (!t || t.trend_30d == null) return null;
  const t30 = t.trend_30d, t90 = t.trend_90d || 0, t180 = trendOver(t.series, 180) || 0;
  const n = (t.series || []).length;
  const conf = n < 20 ? 'low' : (n < 45 ? 'med' : 'high');
  let label, dir;
  if (t30 <= -0.04 && t90 <= 0) { label = 'Falling'; dir = 'down'; }
  else if (t30 <= -0.03 && t180 > 0.15) { label = 'Cooling after run'; dir = 'down'; }
  else if (t30 >= 0.05 && t90 >= 0.10) { label = 'Accelerating ↑'; dir = 'up'; }
  else if (t30 >= 0.02 && t90 >= 0) { label = 'Steady ↑'; dir = 'up'; }
  else if (Math.abs(t30) < 0.03) { label = 'Flat / digesting'; dir = 'flat'; }
  else { label = 'Choppy'; dir = 'flat'; }
  const cls = dir === 'up' ? 'wl-traj--up' : (dir === 'down' ? 'wl-traj--down' : 'wl-traj--flat');
  return el('div', { class: 'wl-traj' }, [
    el('span', { class: 'wl-trajlabel', text: 'Trajectory' }),
    el('span', { class: `wl-trajchip ${cls}`,
      title: 'Momentum from TCGplayer sealed market (30/90/180d). A read on the recent move, not a guaranteed forecast — check reprint/print status too.',
      text: label }),
    conf === 'low' ? el('span', { class: 'wl-trajconf', text: 'thin data' }) : null,
  ].filter(Boolean));
}

/* Tiny inline SVG of the TCGplayer market series — the visual trend line. */
function wlSparkline(series) {
  const vals = (series || []).map((p) => p.market).filter((v) => v != null);
  if (vals.length < 3) return null;
  const w = 150, h = 34, pad = 3, n = vals.length;
  const min = Math.min(...vals), max = Math.max(...vals), range = (max - min) || 1;
  const xy = vals.map((v, i) => [
    pad + (i / (n - 1)) * (w - 2 * pad),
    pad + (1 - (v - min) / range) * (h - 2 * pad),
  ]);
  const pts = xy.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const [lx, ly] = xy[n - 1];
  const stroke = vals[n - 1] >= vals[0] ? 'var(--brand-red-dark)' : 'var(--moss-dark)';
  const svg = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" class="wl-spark" preserveAspectRatio="none" aria-hidden="true">`
    + `<polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>`
    + `<circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2.2" fill="${stroke}"/></svg>`;
  return el('div', { class: 'wl-sparkwrap', title: `${n} TCGplayer market points`, html: svg });
}

/* One under-cap listing row, linking out to the live eBay listing. The price is
   all-in (item + shipping) when eBay gave a shipping cost; when it didn't, we flag
   it so you know the shown price is item-only and the real all-in is higher. */
function wlListingRow(it) {
  const tag = it.kind === 'Buy It Now' ? '🟢' : '🔨';
  let shipNode;
  if (it.ship == null) {
    shipNode = el('span', { class: 'wl-listing-ship wl-listing-ship--warn',
      title: 'Seller didn’t list shipping — the real all-in price is higher. Check the listing.',
      text: '+ ship not listed' });
  } else if (it.item_price != null && it.price > it.item_price + 0.001) {
    shipNode = el('span', { class: 'wl-listing-ship',
      text: `${USD0(it.item_price)} + ${USD0(it.ship)} ship` });
  } else {
    shipNode = el('span', { class: 'wl-listing-ship wl-listing-ship--free', text: 'free ship' });
  }
  // Auctions show their end (time-sensitive); BIN end dates are GTC noise.
  const ends = (it.kind === 'Auction' && it.end)
    ? el('span', { class: 'wl-listing-meta', text: `ends ${String(it.end).slice(0, 10)}` }) : null;
  return el('a', { class: 'wl-listing', href: it.url || '#', target: '_blank', rel: 'noopener',
    title: it.title || '' }, [
    el('span', { class: 'wl-listing-price', text: USD0(it.price) }),
    el('span', { class: 'wl-listing-title', text: `${tag} ${it.title || ''}` }),
    shipNode,
    ends,
  ].filter(Boolean));
}

/* The current under-cap deals, with a Buy-It-Now / Auction switch so you can look
   at each kind on its own before clicking through to the live listing on eBay. */
function wlListingsPanel(listings) {
  // Site rule: drop auctions ending >24h out (keep all Buy-It-Nows).
  listings = (listings || []).filter(listingShown);
  if (!listings.length) return null;
  const bins = listings.filter((it) => it.kind === 'Buy It Now');
  const aucs = listings.filter((it) => it.kind === 'Auction');
  const views = [
    { key: 'all', label: `All ${listings.length}`, items: listings },
    { key: 'bin', label: `🟢 Buy It Now ${bins.length}`, items: bins },
    { key: 'auc', label: `🔨 Auction ${aucs.length}`, items: aucs },
  ];

  const listDiv = el('div', { class: 'wl-listings' });
  const render = (items) => {
    listDiv.replaceChildren(
      items.length
        ? el('div', { class: 'wl-listings-rows' }, items.map(wlListingRow))
        : el('p', { class: 'wl-listings-empty', text: 'None in this view right now.' }),
    );
  };
  const btns = {};
  const setActive = (key) => {
    Object.entries(btns).forEach(([k, b]) => {
      const on = k === key;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    render(views.find((v) => v.key === key).items);
  };
  const seg = el('div', { class: 'wl-seg', role: 'tablist' }, views.map((v) => {
    const b = el('button', { class: 'wl-seg-btn', type: 'button', role: 'tab', text: v.label,
      onclick: () => setActive(v.key) });
    btns[v.key] = b;
    return b;
  }));

  const wrap = el('div', { class: 'wl-listings-wrap' }, [seg, listDiv]);
  setActive('all');                     // default to the full list
  return wrap;
}

/* Mini SVG of the watch's recent eBay Buy-It-Now floor (cheapest under-cap BIN per
   day). Distinct from the TCGplayer sparkline above — this is the real deal floor. */
function floorSparkSvg(vals) {
  if (!vals || vals.length < 3) return null;
  const w = 120, h = 28, pad = 3, n = vals.length;
  const min = Math.min(...vals), max = Math.max(...vals), range = (max - min) || 1;
  const xy = vals.map((v, i) => [
    pad + (i / (n - 1)) * (w - 2 * pad),
    pad + (1 - (v - min) / range) * (h - 2 * pad),
  ]);
  const pts = xy.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const [lx, ly] = xy[n - 1];
  const stroke = vals[n - 1] <= vals[0] ? 'var(--moss-dark)' : 'var(--brand-red-dark)';
  const svg = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" class="wl-spark" preserveAspectRatio="none" aria-hidden="true">`
    + `<polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>`
    + `<circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2.2" fill="${stroke}"/></svg>`;
  return el('div', { class: 'wl-sparkwrap', title: `${n} daily cheapest-in-cap BIN points`, html: svg });
}

/* Deal-quality strip: where today's cheapest under-cap Buy-It-Now sits vs the
   cheapest this monitor has SEEN over the window. Honest scope (Codex #3): it's the
   lowest IN-CAP BIN observed by the watcher — not a full-market floor, and it has
   small gaps (quiet hours / only awake sweeps). The 🔥 badge = a fresh observed low. */
function wlFloorRow(w) {
  const days = w.best_window_days || 14;
  const hist = (w.floor_history || []).map((p) => p[1]).filter((v) => v != null);
  if (w.window_low == null && !hist.length) return null;       // nothing logged yet
  const parts = [el('span', { class: 'wl-floorlabel',
    title: `Cheapest under-cap Buy-It-Now this monitor observed in ${days} days — not a full-market floor (quiet-hours gaps; in-cap only).`,
    text: `cheapest BIN seen ${days}d` })];
  if (w.window_low != null) {
    parts.push(el('span', { class: 'wl-floorval', text: USD0(w.window_low) }));
  }
  if (w.floor_today != null) {
    parts.push(el('span', { class: 'wl-floortoday',
      text: `· cheapest now ${USD0(w.floor_today)}` }));
  }
  if (w.is_window_low) {
    parts.push(el('span', { class: 'wl-floorflag', text: `🔥 cheapest seen in ${days}d` }));
  }
  const spark = floorSparkSvg(hist);
  if (spark) parts.push(spark);
  return el('div', { class: 'wl-floor' }, parts);
}

function watchCard(w) {
  const under = w.under_cap || 0;
  const days = w.best_window_days || 14;
  const trend = STATE.sealedTrend && STATE.sealedTrend.products
    ? STATE.sealedTrend.products[String(w.tcg_product)] : null;
  const head = el('div', { class: 'wl-head' }, [
    el('h3', { class: 'wl-title', text: w.label }),
    el('span', { class: under > 0 ? 'chip chip--teal' : 'badge-stub',
      text: under > 0 ? `${under} under ${USD0(w.max_price)}` : `none under ${USD0(w.max_price)}` }),
    w.notify === false ? el('span', { class: 'chip chip--lavender',
      title: 'Tracking only — prices and floors are recorded, but this watch never sends phone alerts.',
      text: '👁 tracking only' }) : null,
    w.is_window_low ? el('span', { class: 'chip chip--fire',
      title: `Cheapest under-cap Buy-It-Now this monitor has observed in ${days} days (in-cap only).`,
      text: `🔥 cheapest ${days}d` }) : null,
  ].filter(Boolean));
  const capNote = w.cap_after_shipping ? ' <span class="wl-capnote">incl. ship</span>' : '';
  // Self-adjusting cap: show that the cap tracks market, and at what % of it.
  const dynNote = (w.cap_is_dynamic && w.cap_pct)
    ? ` &middot; <span class="wl-capnote wl-capnote--dyn" title="Cap auto-tracks TCGplayer market — ${Math.round(w.cap_pct * 100)}% of it">↺ ${Math.round(w.cap_pct * 100)}% of market</span>`
    : '';
  const meta = el('p', { class: 'wl-meta', html:
    `TCGplayer market <strong>${USD0(w.market_price)}</strong> &middot; your cap <strong>${USD0(w.max_price)}</strong>${capNote}${dynNote} &middot; ignore &lt; ${USD0(w.floor)}` });
  const best = el('div', { class: 'wl-best' }, [
    wlBestCell('🟢', 'Buy It Now', w.best_bin, w.market_price),
    wlBestCell('🔨', 'Auction', bestAuctionShown(w.best_auction), w.market_price),
  ]);
  const cta = el('a', { class: 'wl-cta', href: ebaySearchUrl(w.query),
    target: '_blank', rel: 'noopener',
    text: under > 0 ? `View all ${under} on eBay →` : 'Browse on eBay →' });
  // Official TCGplayer product image (same clean source as the price); broken
  // URLs fall back to the shared placeholder.
  const thumb = w.tcg_product ? el('img', {
    class: 'wl-thumb', loading: 'lazy', alt: w.label,
    src: `https://tcgplayer-cdn.tcgplayer.com/product/${w.tcg_product}_200w.jpg`,
    onerror: (e) => imgFallback(e.target),
  }) : null;
  return el('div', { class: 'table-card wl-card' }, [
    thumb,
    el('div', { class: 'wl-body' }, [
      head, meta, wlTrend(trend), wlTrajectory(trend), wlSparkline(trend && trend.series),
      best, wlFloorRow(w), wlListingsPanel(w.listings), cta,
    ]),
  ].filter(Boolean));
}

/* ---------- watch buttons (toggle a card in/out of My Cards) ---------- */
function watchStar(card) {
  const btn = el('button', { class: `wl-star${isWatched(card.id) ? ' is-on' : ''}`, type: 'button',
    title: isWatched(card.id) ? 'Watching — click to remove' : 'Add to My Cards', 'aria-label': 'Toggle watch',
    text: isWatched(card.id) ? '★' : '☆' });
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleWatch(card);
    const on = isWatched(card.id);
    btn.classList.toggle('is-on', on);
    btn.textContent = on ? '★' : '☆';
  });
  return btn;
}
function watchBtnLabeled(card) {
  const label = () => isWatched(card.id) ? '★ Watching' : '☆ Watch';
  const btn = el('button', { class: `button button-secondary button-sm wl-watchbtn${isWatched(card.id) ? ' is-on' : ''}`, type: 'button', text: label() });
  btn.addEventListener('click', () => {
    toggleWatch(card);
    btn.textContent = label();
    btn.classList.toggle('is-on', isWatched(card.id));
  });
  return btn;
}

/* ---------- My Cards portfolio (localStorage) ---------- */
let WL_ACTIVE = null;  // remembered segment across re-renders

function svgLineChart(dates, vals) {
  const W = 680, H = 190, padL = 6, padR = 6, padT = 14, padB = 22;
  const min = Math.min(...vals), max = Math.max(...vals), span = (max - min) || 1, n = vals.length;
  const X = (i) => padL + i * (W - padL - padR) / (n - 1);
  const Y = (v) => padT + (1 - (v - min) / span) * (H - padT - padB);
  const line = vals.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
  const area = `${padL},${(H - padB).toFixed(1)} ${line} ${(W - padR).toFixed(1)},${(H - padB).toFixed(1)}`;
  const up = vals[n - 1] >= vals[0];
  const stroke = up ? 'var(--success-accent)' : 'var(--brand-red-dark)';
  const fill = up ? 'rgba(43,166,106,.10)' : 'rgba(192,57,43,.10)';
  const wrap = el('div', { class: 'wl-chart' });
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="wl-chart-svg" preserveAspectRatio="none" role="img" aria-label="Portfolio value over time"><polygon points="${area}" fill="${fill}"/><polyline points="${line}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  return el('div', {}, [wrap, el('div', { class: 'wl-chart-axis' }, [
    el('span', { text: dates[0] }), el('span', { text: dates[dates.length - 1] }),
  ])]);
}

function portfolioChart(ids) {
  const ph = STATE.priceHistory;
  const series = {}; const dateSet = new Set();
  if (ph) ids.forEach((id) => {
    const s = (ph[id] || []).slice().sort((a, b) => (a[0] < b[0] ? -1 : 1));
    if (s.length) { series[id] = s; s.forEach(([d]) => dateSet.add(d)); }
  });
  const dates = [...dateSet].sort();
  if (dates.length < 2) {
    return el('p', { class: 'caption wl-chart-note', text:
      'Performance chart appears once there are ≥2 days of price history for your cards — it deepens one point per day as snapshots accrue.' });
  }
  // Carry-forward each card's most recent price across the shared date axis.
  const totals = dates.map((d) => {
    let t = 0;
    Object.values(series).forEach((s) => {
      let price = null;
      for (const [dd, pp] of s) { if (dd <= d) price = pp; else break; }
      if (price != null) t += price;
    });
    return t;
  });
  return svgLineChart(dates, totals);
}

function myCardsPanel() {
  const ids = Object.keys(STATE.myWatch);
  if (!ids.length) {
    return el('div', { class: 'table-card', style: 'padding:40px 28px;text-align:center' }, [
      el('p', { class: 'body-sm', style: 'color:var(--slate);max-width:54ch;margin:0 auto', html:
        'Your card watchlist is empty. Open any card and hit <strong>☆ Watch</strong> to track it here — total value, change since you added it, and a performance chart that deepens every day.' }),
    ]);
  }
  let curTotal = 0, addBase = 0, haveBase = 0;
  ids.forEach((id) => {
    const c = STATE.byId.get(id); const m = STATE.myWatch[id];
    curTotal += c ? (c.market_price || 0) : 0;
    if (m.priceAtAdd != null) { addBase += m.priceAtAdd; haveBase += 1; }
  });
  const since = haveBase ? curTotal - addBase : null;
  const sincePct = (haveBase && addBase > 0) ? since / addBase : null;
  const stat = (kicker, big, sub) => el('div', { class: 'wl-stat' }, [
    el('div', { class: 'wl-stat-kicker', text: kicker }),
    el('div', { class: 'wl-stat-big', text: big }),
    sub ? el('div', { class: 'wl-stat-sub', text: sub }) : null,
  ].filter(Boolean));
  const statRow = el('div', { class: 'wl-stat-row' }, [
    stat('Cards watched', String(ids.length), `${ids.length}/100`),
    stat('Total raw value', USD(curTotal), 'latest market'),
    stat('Since added', since == null ? '—' : (since >= 0 ? '+' : '') + USD(since),
      sincePct == null ? `base price on ${haveBase}/${ids.length}` : (sincePct >= 0 ? '+' : '') + (sincePct * 100).toFixed(1) + '%'),
  ]);

  const rows = ids.map((id) => {
    const c = STATE.byId.get(id); const m = STATE.myWatch[id];
    if (!c) return null;
    const cur = c.market_price || 0;
    const d = (m.priceAtAdd != null && m.priceAtAdd > 0) ? (cur - m.priceAtAdd) / m.priceAtAdd : null;
    const img = el('img', { class: 'lb-thumb', src: c.image_small || PLACEHOLDER, alt: c.name, loading: 'lazy' });
    img.addEventListener('error', () => imgFallback(img));
    const rm = el('button', { class: 'wl-remove', type: 'button', title: 'Remove from watchlist', 'aria-label': 'Remove', text: '✕' });
    rm.addEventListener('click', (e) => { e.stopPropagation(); delete STATE.myWatch[id]; saveWatch(); viewWatchlists(); });
    const row = el('div', { class: 'wl-myrow clickable' }, [
      img,
      el('div', { class: 'wl-myrow-main' }, [
        el('div', { class: 'lb-name', text: c.name }),
        el('div', { class: 'lb-sub', text: `${c.set_name} · added ${m.added}` }),
        signalPill(c),
      ]),
      el('div', { class: 'wl-myrow-price' }, [
        el('div', { class: 'lb-price', text: USD(cur) }),
        /* green for gains (delta--down is the green class), red for losses */
        d == null ? null : el('span', { class: `delta ${d >= 0 ? 'delta--down' : 'delta--up'}`, text: signedPct(d) }),
      ].filter(Boolean)),
      rm,
    ]);
    row.addEventListener('click', () => openModal(id));
    return row;
  }).filter(Boolean);

  return el('div', {}, [
    statRow,
    el('div', { class: 'table-card wl-chart-card' }, [
      el('div', { class: 'wl-chart-head' }, [
        el('h3', { class: 'h4', text: 'Price performance' }),
        el('span', { class: 'caption', text: 'total raw value over time' }),
      ]),
      portfolioChart(ids),
    ]),
    el('div', { class: 'wl-myrows table-card' }, rows),
    el('p', { class: 'caption', html: 'Stored only in this browser (no account). “Since added” compares each card’s current TCGplayer market price to its price when you added it.' }),
  ]);
}

/* ---------- sealed-ETB deal monitor (the original watchlist) ---------- */
const WATCHLIST_STALE_DAYS = 7;  /* older than this: warn that listings have likely ended */
function sealedWatchlistPanel() {
  const wl = STATE.watchlist;
  if (!wl || !wl.watches || !wl.watches.length) {
    return el('div', { class: 'table-card', style: 'padding:28px;text-align:center' }, [
      el('p', { class: 'body-sm', style: 'color:var(--slate)', html:
        'No watchlist snapshot yet. Run <code>python ebay_deals.py --once</code> to populate it.' }),
    ]);
  }
  const updated = wl.updated ? new Date(wl.updated) : null;
  const age = ageOf(wl.updated);
  const refreshBtn = el('button', { class: 'button button-secondary button-sm wl-refresh', type: 'button',
    text: '⟳ Refresh', title: 'Re-pull the latest published deal snapshot',
    onclick: (e) => refreshSealedWatchlist(e.currentTarget) });
  /* The deal list is published by hand from a local watcher, not by the daily
     refresh, so it can be weeks old. Say so plainly once it is. */
  const staleNote = (age && age.days > WATCHLIST_STALE_DAYS)
    ? el('p', { class: 'stale-note', text:
        `This list is ${age.days} days old; most listings have likely ended. Treat the prices below as history, not live deals.` })
    : (!age ? el('p', { class: 'stale-note', text: 'This list has no update date, so its age is unknown; listings may have ended.' }) : null);
  const banner = el('div', { class: 'table-card', style: 'padding:16px 22px;margin-bottom:24px;box-shadow:none;background:var(--surface-soft);display:flex;gap:14px;align-items:center;flex-wrap:wrap' }, [
    el('span', { class: 'chip chip--lavender', text: 'snapshot' }),
    el('p', { class: 'body-sm', style: 'margin:0;color:var(--slate);max-width:72ch;flex:1', html:
      `A published snapshot of our eBay deal monitor, last updated <strong>${updated && !Number.isNaN(updated.getTime()) ? updated.toLocaleString() : 'at an unknown time'}</strong>. The monitor’s real-time phone alerts go to its owner only; this page changes only when a new snapshot is published. Auctions show here only when they end within 24 hours. Tap any price to open the listing on eBay (it may have ended). The “>50% under market → ignore” rule strips out code cards, empty boxes and proxies.` }),
    refreshBtn,
  ]);
  return el('div', {}, [staleNote, banner,
    el('div', { class: 'wl-grid' }, wl.watches.map(watchCard)),
    el('p', { class: 'caption', html: 'Prices are eBay <em>asking</em> prices (Buy It Now) or auction bids at the time of the snapshot, not sold comps. Auctions show only when they end within 24 hours. You buy manually on eBay; this is a notify-only monitor.' }),
  ]);
}

/* Re-pull the latest published watchlist.json (no full page reload) and re-render
   the Sealed Deals panel. The snapshot is whatever was last deployed. */
async function refreshSealedWatchlist(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '⟳ Refreshing…'; }
  let ok = false, prevUpdated = STATE.watchlist && STATE.watchlist.updated;
  try {
    const r = await fetch('./data/watchlist.json?t=' + Date.now(), { cache: 'no-store' });
    if (r.ok) { STATE.watchlist = await r.json(); ok = true; }
  } catch (_) { /* keep the data we have */ }
  WL_ACTIVE = 'sealed';
  viewWatchlists();
  // Confirm on the freshly-rendered button so a click is never a no-op to the eye.
  const fresh = document.querySelector('.wl-refresh');
  if (fresh) {
    const changed = ok && STATE.watchlist && STATE.watchlist.updated !== prevUpdated;
    /* "No newer data", not "Up to date": an unchanged file can still be weeks old. */
    fresh.textContent = !ok ? '⚠ Offline' : (changed ? '✓ Updated' : 'No newer data');
    fresh.disabled = false;
    setTimeout(() => { if (fresh.isConnected) fresh.textContent = '⟳ Refresh'; }, 2000);
  }
}

function viewWatchlists() {
  /* Was a "live · eBay" chip; the deal list is a dated snapshot, so show its date. */
  const wlAge = ageOf(STATE.watchlist && STATE.watchlist.updated);
  const wlChip = wlAge
    ? el('span', { class: `chip ${wlAge.days > WATCHLIST_STALE_DAYS ? 'chip--yellow' : 'chip--teal'}`,
        text: `deal list last updated ${wlAge.date}` })
    : null;
  const head = el('div', { class: 'section-head' }, [
    el('div', {}, [
      el('p', { class: 'micro section-eyebrow', text: 'Watchlists' }),
      el('h2', { class: 'h2', text: 'Watchlists' }),
      el('p', { class: 'section-sub', text: 'Your personal card portfolio, plus our sealed-ETB deal monitor — in one place.' }),
    ]),
    wlChip,
  ].filter(Boolean));
  const body = el('div', {});
  const nWatch = Object.keys(STATE.myWatch).length;
  const views = [
    { key: 'mycards', label: `My Cards${nWatch ? ` (${nWatch})` : ''}`, render: myCardsPanel },
    { key: 'sealed', label: 'Sealed Deals', render: sealedWatchlistPanel },
  ];
  const btns = {};
  const show = (key) => {
    Object.entries(btns).forEach(([k, b]) => {
      const on = k === key;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    WL_ACTIVE = key;
    body.replaceChildren(views.find((v) => v.key === key).render());
  };
  const seg = el('div', { class: 'wl-seg', role: 'tablist', style: 'margin-bottom:22px' }, views.map((v) => {
    const b = el('button', { class: 'wl-seg-btn', type: 'button', role: 'tab', text: v.label, onclick: () => show(v.key) });
    btns[v.key] = b;
    return b;
  }));
  setView(el('section', { class: 'section container' }, [head, seg, body]));
  show(WL_ACTIVE || (nWatch ? 'mycards' : 'sealed'));
  highlightNav('watchlists');
}

/* ============================================================
   5. SEARCH — filter all cards
   ============================================================ */
let searchTimer = null;
function viewSearch() {
  const setNames = Array.from(new Set(STATE.cards.map((c) => c.set_name))).sort();

  const input = el('input', { class: 'search-input', type: 'search', placeholder: 'Search by card name…', 'aria-label': 'Search cards' });
  const setSelect = el('select', { class: 'search-select', 'aria-label': 'Filter by set' }, [
    el('option', { value: '', text: 'All sets' }),
    ...setNames.map((s) => el('option', { value: s, text: s })),
  ]);
  const sortSelect = el('select', { class: 'search-select', 'aria-label': 'Sort' }, [
    el('option', { value: 'price-desc', text: 'Price: high → low' }),
    el('option', { value: 'price-asc', text: 'Price: low → high' }),
    el('option', { value: 'iq-desc', text: 'IQ: high → low' }),
    el('option', { value: 'under', text: 'Most undervalued' }),
    el('option', { value: 'over', text: 'Most overvalued' }),
    el('option', { value: 'name', text: 'Name A → Z' }),
  ]);

  const count = el('div', { class: 'search-count' });
  const grid = el('div', { class: 'card-grid' });

  function render() {
    const q = input.value.trim().toLowerCase();
    const setFilter = setSelect.value;
    const sort = sortSelect.value;

    let rows = STATE.cards.filter((c) => {
      if (setFilter && c.set_name !== setFilter) return false;
      if (!q) return true;
      return c.name.toLowerCase().includes(q) || (c.set_name || '').toLowerCase().includes(q) || (c.number || '').toLowerCase().includes(q);
    });

    const priceOf = (c) => (c.market_price == null ? -1 : c.market_price);
    rows.sort((a, b) => {
      switch (sort) {
        case 'price-asc': return priceOf(a) - priceOf(b);
        case 'iq-desc': return (b.iq_score ?? 0) - (a.iq_score ?? 0);
        case 'under': return (a.residual_pct ?? 0) - (b.residual_pct ?? 0);
        case 'over': return (b.residual_pct ?? 0) - (a.residual_pct ?? 0);
        case 'name': return a.name.localeCompare(b.name);
        case 'price-desc':
        default: return priceOf(b) - priceOf(a);
      }
    });

    const total = rows.length;
    const shown = rows.slice(0, 120);
    count.textContent = `${total.toLocaleString()} card${total === 1 ? '' : 's'}${total > shown.length ? ` (showing first ${shown.length})` : ''}`;

    grid.innerHTML = '';
    if (!shown.length) {
      grid.appendChild(el('div', { class: 'state' }, [
        el('div', { class: 'h3', text: 'No cards match' }),
        el('p', { text: 'Try a different name or clear the set filter.' }),
      ]));
      return;
    }
    const frag = document.createDocumentFragment();
    shown.forEach((c) => frag.appendChild(miniCard(c)));
    grid.appendChild(frag);
  }

  input.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(render, 120); });
  setSelect.addEventListener('change', render);
  sortSelect.addEventListener('change', render);

  const section = el('section', { class: 'section container' }, [
    el('div', { class: 'section-head' }, [
      el('div', {}, [
        el('p', { class: 'micro section-eyebrow', text: 'Search' }),
        el('h2', { class: 'h2', text: 'Browse every card' }),
        el('p', { class: 'section-sub', text: `All ${STATE.cards.length.toLocaleString()} cards in the corpus, each with its market price, model price and IQ score. Click any card for the full signal panel.` }),
      ]),
    ]),
    el('div', { class: 'search-bar' }, [input, setSelect, sortSelect]),
    count,
    grid,
  ]);

  setView(section);
  highlightNav('search');
  render();
  input.focus();
}

function miniCard(c) {
  const img = el('img', { src: c.image_small || PLACEHOLDER, alt: c.name, loading: 'lazy' });
  img.addEventListener('error', () => imgFallback(img));
  const node = el('div', { class: 'mini-card', dataset: { id: c.id } }, [
    el('div', { class: 'mini-card-img' }, [img, watchStar(c)]),
    el('div', {}, [
      el('div', { class: 'mini-card-name', text: c.name }),
      el('div', { class: 'mini-card-sub', text: `${c.set_name} · #${c.number}` }),
      signalPill(c),
    ]),
    el('div', { class: 'mini-card-foot' }, [
      el('span', { class: 'lb-price', text: USD(c.market_price) }),
      deltaPill(c.residual_pct, c),
    ]),
  ]);
  node.addEventListener('click', () => openModal(c.id));
  return node;
}

/* ============================================================
   CARD-DETAIL MODAL + live signals panel
   ============================================================ */
let lastFocused = null;

function openModal(id) {
  const card = STATE.byId.get(id);
  if (!card) return;
  lastFocused = document.activeElement;

  const backdrop = $('#modalBackdrop');
  const modal = $('#modal');
  modal.innerHTML = '';
  modal.appendChild(buildModalContent(card));
  backdrop.hidden = false;
  document.body.style.overflow = 'hidden';
  if (location.hash.indexOf('card=') === -1) {
    history.replaceState(null, '', `${location.hash || '#/pricelab'}${location.hash.includes('?') ? '&' : '?'}card=${encodeURIComponent(id)}`);
  }
  const closeBtn = $('.modal-close', modal);
  if (closeBtn) closeBtn.focus();
}

function closeModal() {
  const backdrop = $('#modalBackdrop');
  backdrop.hidden = true;
  $('#modal').innerHTML = '';
  document.body.style.overflow = '';
  if (location.hash.includes('card=')) {
    history.replaceState(null, '', location.hash.replace(/[?&]card=[^&]*/,''));
  }
  if (lastFocused && lastFocused.focus) lastFocused.focus();
}

function buildModalContent(card) {
  const clusterModel = modelForCluster(card.cluster);
  const featMeta = STATE.model.feature_meta;

  /* working copy of feature values (start at the card's real features) */
  const working = {};
  clusterModel.features.forEach((f) => { working[f] = card.features[f]; });

  /* image */
  const bigImg = el('img', { src: card.image_large || card.image_small || PLACEHOLDER, alt: card.name, loading: 'lazy' });
  bigImg.addEventListener('error', () => imgFallback(bigImg));

  /* big delta — respects the ±15% verdict band ("fair" inside it) and the gating
     ("no edge" for mature mid-price cards the model can't call) */
  const verdict = residualVerdict(card.residual_pct, card);
  const deltaBig = el('div', { class: 'price-delta-big' }, [
    el('div', { class: `pd-num ${verdict.pd}`, text: verdict.noEdge ? '—' : signedPct(card.residual_pct) }),
    el('div', { class: `pd-label ${verdict.pd}`, text: verdict.label }),
  ]);

  const priceBlock = el('div', { class: 'price-block' }, [
    el('div', { class: 'price-cell' }, [
      el('div', { class: 'price-label', text: 'Market' }),
      el('div', { class: 'price-value', text: USD(card.market_price) }),
    ]),
    el('div', { class: 'price-cell' }, [
      el('div', { class: 'price-label', text: 'Model (expected)' }),
      el('div', { class: 'price-value expected', text: USD(card.expected_price) }),
    ]),
    deltaBig,
  ]);

  /* gauges: demand pressure + supply saturation from card.dynamics */
  const dyn = card.dynamics || {};
  const dynLive = dyn.status === 'ok' && dyn.demand_pressure != null;
  const gauges = el('div', { class: 'gauges' }, [
    demandGauge(dyn, !dynLive),
    saturationGauge(dyn, dyn.status === 'awaiting_data'),
  ]);

  /* ----- live signals panel ----- */
  const livePriceVal = el('span', { class: 'slp-val', text: USD(card.expected_price) });
  const livePriceDelta = el('span', { class: 'slp-delta', text: signedPct(card.residual_pct) });

  function recompute() {
    const expected = computeExpected(clusterModel, working);
    livePriceVal.textContent = USD(expected);
    if (card.market_price != null && expected > 0) {
      const resid = (card.market_price - expected) / expected;
      const v = residualVerdict(resid);
      livePriceDelta.textContent = signedPct(resid) + ' ' + v.label;
      livePriceDelta.style.color = v.dir === 'under' ? 'var(--success-accent)'
        : v.dir === 'over' ? 'var(--brand-red-dark)' : 'var(--steel)';
    } else {
      livePriceDelta.textContent = 'no market price';
      livePriceDelta.style.color = 'var(--steel)';
    }
  }

  const sliderRows = clusterModel.features.map((f, i) => {
    const meta = featMeta[f] || { label: f, min: 0, max: 10, status: 'live' };
    const isStub = meta.status === 'stub';
    const std = clusterModel.stds[i];
    const inert = std === 0; /* feature with zero variance in this cluster: no price effect */
    const step = (meta.max - meta.min) / 200 || 0.01;
    const val0 = working[f] != null ? working[f] : (meta.min + meta.max) / 2;

    const valOut = el('span', { class: 'slider-val', text: fmtFeat(f, val0) });
    const contribOut = el('div', { class: 'slider-contrib' });
    const slider = el('input', {
      class: `sig-slider${isStub ? ' is-stub' : ''}`, type: 'range',
      min: meta.min, max: meta.max, step, value: val0,
      'aria-label': meta.label,
    });

    function updateContrib() {
      if (inert) { contribOut.textContent = 'No price effect in this cluster (flat in the model).'; return; }
      const z = clusterModel.coef[i] * ((working[f] - clusterModel.means[i]) / std);
      const dir = z > 0.0005 ? '▲ raises' : (z < -0.0005 ? '▼ lowers' : '· neutral');
      contribOut.textContent = `${dir} the estimate (contribution ${z >= 0 ? '+' : ''}${z.toFixed(3)} in log-price)`;
    }

    slider.addEventListener('input', () => {
      working[f] = parseFloat(slider.value);
      valOut.textContent = fmtFeat(f, working[f]);
      updateContrib();
      recompute();
    });
    updateContrib();

    const nameEl = el('span', { class: 'slider-name' }, [
      meta.label,
      isStub ? el('span', { class: 'badge-stub', text: 'awaiting data' }) : null,
      inert && !isStub ? el('span', { class: 'chip', text: 'no effect' }) : null,
    ]);

    return { node: el('div', { class: 'slider-row' }, [
      el('div', { class: 'slider-top' }, [nameEl, valOut]),
      slider, contribOut,
    ]), reset: () => { working[f] = card.features[f]; slider.value = val0; valOut.textContent = fmtFeat(f, val0); updateContrib(); } };
  });

  const resetBtn = el('button', { class: 'button button-secondary button-sm', text: 'Reset to model values' });
  resetBtn.addEventListener('click', () => { sliderRows.forEach((r) => r.reset()); recompute(); });

  const signalsPanel = el('div', { class: 'signals-panel' }, [
    el('div', { class: 'signals-head' }, [
      el('h4', { class: 'h4', text: 'View Market Signals' }),
      el('span', { class: 'chip chip--lavender', html: `cluster · ${card.cluster}` }),
    ]),
    el('p', { class: 'signals-note', html:
      'Each slider is a model input. Moving one recomputes the expected price live, client-side, from this cluster’s exported coefficients: <code>expected = exp(intercept + Σ coef·z)</code>. Stubbed signals are labeled and currently carry zero weight.' }),
    el('div', { class: 'signals-live-price' }, [
      el('span', { class: 'slp-label', text: 'Live expected price' }),
      livePriceVal,
      livePriceDelta,
    ]),
    ...sliderRows.map((r) => r.node),
    el('div', { class: 'signals-foot' }, [
      el('span', { class: 'caption', text: 'Estimate only — not an appraisal or offer.' }),
      resetBtn,
    ]),
  ]);

  /* ----- assemble ----- */
  const closeBtn = el('button', { class: 'modal-close', 'aria-label': 'Close', text: '✕' });
  closeBtn.addEventListener('click', closeModal);

  return el('div', {}, [
    closeBtn,
    el('div', { class: 'modal-grid' }, [
      el('div', { class: 'modal-img-wrap' }, bigImg),
      el('div', { class: 'modal-body' }, [
        el('div', { class: 'modal-eyebrow' }, [
          el('span', { class: 'chip', text: card.rarity }),
          el('span', { class: 'chip', text: `#${card.number}` }),
          card.price_variant ? el('span', { class: 'chip chip--yellow', text: card.price_variant }) : null,
          watchBtnLabeled(card),
        ]),
        el('h3', { class: 'h2 modal-title', text: card.name }),
        el('div', { class: 'modal-setline', text: `${card.set_name} · ${card.series} · ${card.release_date}` }),
        priceBlock,
        fairPriceNote(card),
        signalBanner(card),
        (card.features && card.features.pull_cost)
          ? el('p', { class: 'caption', html:
              `<strong>Cost to pull one ≈ ${USD0(card.features.pull_cost)}</strong> — expected spend on booster packs (at today's market pack price) to hit this exact card once. Buying the single at ${USD(card.market_price)} is ${card.features.pull_cost > (card.market_price || 0) ? 'far cheaper' : 'pricier'} than chasing it.` })
          : null,
        el('div', { class: 'iq-row' }, [
          el('span', { class: 'iq-badge' }, [
            el('span', { class: 'iq-num', text: card.iq_score != null ? card.iq_score.toFixed(0) : '—' }),
            el('span', { class: 'iq-of', text: '/ 100 IQ' }),
          ]),
          el('span', { class: 'caption', text: 'composite of scarcity, character premium & in-set rank' }),
        ]),
        gauges,
        flowStats(dyn),
        psaPanel(card),
        el('p', { class: 'caption', html: dynLive
          ? `Demand pressure = est. sold (7d) ÷ total supply; supply saturation = active listings 7-day vs 30-day average. Inferred from daily eBay active-listing snapshots${dyn.active_listings != null ? ` (${dyn.active_listings.toLocaleString()} active, ${(dyn.sold_7d ?? 0).toLocaleString()} est. sold this week)` : ''}.`
          : (dyn.basis && dyn.basis.reason === 'no_current_observation')
            ? `No current eBay read for this card: the latest daily sweep did not return a valid listing count for it${dyn.basis.last_observed ? ` (last valid read ${dyn.basis.last_observed})` : ''}, so older days are not shown as a live signal. Shown as <strong>awaiting data</strong>.`
            : 'No eBay demand read for this card yet: the daily eBay sweep covers only the most valuable cards, and a card needs a few days of snapshots. Shown as <strong>awaiting data</strong>.' }),
        signalsPanel,
      ]),
    ]),
  ]);
}

/* "Fair price" is a model estimate. Say how many cards it was fit on (the
   cluster's n, already in model.json) and flag small clusters. A card whose
   cluster was too small for its own model uses the global model. */
const SMALL_CLUSTER_N = 30;
function fairPriceNote(card) {
  const m = STATE.model || {};
  const own = m.clusters && m.clusters[card.cluster];
  const n = (own || m.global || {}).n;
  if (!n) return el('p', { class: 'caption', text: 'The expected price is a model estimate; it is less reliable for small clusters.' });
  const base = own
    ? `The expected price is a model estimate fit on ${n.toLocaleString()} priced cards in this card’s cluster (${card.cluster}).`
    : `This card’s cluster is too small for its own model, so its expected price comes from the global model (${n.toLocaleString()} cards).`;
  const tail = (own && n < SMALL_CLUSTER_N)
    ? ' That is a small sample, so treat this estimate as rough.'
    : ' Estimates from small clusters are less reliable.';
  return el('p', { class: 'caption', text: base + tail });
}

/* shared gauge shell */
function gaugeShell(label, badge, barChildren, readout) {
  return el('div', { class: 'gauge' }, [
    el('div', { class: 'gauge-head' }, [
      el('span', { class: 'gauge-label', text: label }),
      badge,
    ]),
    el('div', { class: 'gauge-bar' }, barChildren),
    el('div', { class: 'gauge-value', text: readout }),
  ]);
}

/* Demand pressure: est_sold(7d)/total_supply as a %. Coral→yellow→red fill by magnitude.
   The gradient bar is full-width and a mask reveals it up to the value, so low % reads
   coral and high % climbs into red. Scaled so ~40%+ pressure pegs the gauge. */
function demandGauge(dyn, isStub) {
  const DP_FULL = 0.40; /* demand_pressure that fills the gauge */
  if (isStub) {
    const fill = el('div', { class: 'gauge-fill is-stub', style: 'width:100%' });
    return gaugeShell('Demand pressure',
      el('span', { class: 'badge-stub', text: 'stub' }), fill, 'awaiting data');
  }
  const dp = dyn.demand_pressure;
  const frac = Math.max(0, Math.min(1, dp / DP_FULL));
  const fill = el('div', { class: 'gauge-fill', style: `width:${(frac * 100).toFixed(0)}%` });
  return gaugeShell('Demand pressure',
    el('span', { class: 'chip chip--teal', text: 'est' }), fill,
    `${PCT(dp)} of supply sold in 7d (est.)`);
}

/* Supply saturation: a needle centered on 1.0 (steady). Left of center = tightening
   (green), right = loosening (red). Maps saturation in [0.5, 1.5] to needle [0,100%]. */
function saturationGauge(dyn, isStub) {
  if (isStub) {
    const fill = el('div', { class: 'gauge-fill is-stub', style: 'width:100%' });
    return gaugeShell('Supply saturation',
      el('span', { class: 'badge-stub', text: 'stub' }), fill, 'awaiting data');
  }
  const sat = dyn.supply_saturation != null ? dyn.supply_saturation : 1;
  const SPAN = 0.5; /* saturation range shown each side of 1.0 */
  const pos = Math.max(0, Math.min(1, (sat - (1 - SPAN)) / (2 * SPAN))) * 100;
  const tone = sat < 0.97 ? 'tightening' : sat > 1.03 ? 'loosening' : 'steady';
  const needle = el('div', { class: `gauge-needle gauge-needle--${tone}`, style: `left:${pos.toFixed(1)}%` });
  const track = el('div', { class: 'gauge-track gauge-track--saturation' }, [
    el('div', { class: 'gauge-center-mark' }), needle,
  ]);
  const word = sat < 0.97 ? 'tightening' : sat > 1.03 ? 'loosening' : 'steady';
  return gaugeShell('Supply saturation',
    el('span', { class: 'chip chip--teal', text: 'est' }), track,
    `${sat.toFixed(2)}× · ${word} (>1 loosening, <1 tightening)`);
}

function fmtFeat(f, v) {
  if (v == null) return '—';
  if (f === 'pull_cost') return USD0(v);
  if (f === 'set_rank') return v.toFixed(2);
  if (f === 'months_since_release') return `${v.toFixed(0)} mo`;
  return v.toFixed(2);
}

/* ============================================================
   5. TRACK RECORD — backtest of the model vs real TCGplayer history
   ============================================================ */
function trIcColor(ic) {
  if (ic == null) return '#868e96';
  if (ic >= 0.04) return 'var(--success-accent)';
  if (ic <= -0.02) return 'var(--brand-red-dark)';
  return '#868e96';
}
const trIc = (ic) => ic == null ? '—' : (ic >= 0 ? '+' : '') + Number(ic).toFixed(3);
const icCell = (ic) => el('span', { style: 'font-weight:600;color:' + trIcColor(ic), text: trIc(ic) });

function trHeadlineCard(title, c, verdict) {
  const tone = trIcColor(c.ic);
  return el('div', { style: 'flex:1 1 220px;min-width:200px;background:var(--surface,#fff);border:1px solid var(--line,#ECECF1);border-radius:16px;padding:20px;border-top:4px solid ' + tone }, [
    el('div', { class: 'micro', text: title }),
    el('div', { style: 'font-size:40px;font-weight:700;line-height:1.1;margin:6px 0;color:' + tone, text: trIc(c.ic) }),
    el('div', { class: 'caption', text: 'rank IC · 28-day' }),
    el('div', { class: 'micro', style: 'margin-top:8px', text: 'HAC t = ' + (c.nw_t == null ? '—' : c.nw_t) + ' · ' + c.sign_pos + '/' + c.n_dates + ' weeks positive' }),
    c.oos ? el('div', { class: 'micro', style: 'margin-top:4px', text: 'Out of sample: IC ' + trIc(c.oos.ic) + ' · ' + c.oos.sign_pos + '/' + c.oos.n_dates + ' weeks positive' })
          : el('div', { class: 'micro', style: 'margin-top:4px', text: 'Out of sample: not enough data yet' }),
    el('div', { style: 'margin-top:10px;font-weight:700;color:' + tone, text: verdict }),
    el('p', { class: 'caption', style: 'margin-top:8px', text: c.note || '' }),
  ]);
}

function trMiniTable(caption, headers, rows) {
  const head = el('thead', {}, el('tr', {}, headers.map((hh, i) => el('th', { class: i ? 'num' : '', text: hh }))));
  const body = el('tbody', {}, rows.map((r) => el('tr', {}, r.map((cell, i) =>
    (cell && cell.nodeType) ? el('td', { class: i ? 'num' : '' }, cell)
                            : el('td', { class: i ? 'num' : '', text: String(cell) })))));
  return el('div', {}, [
    caption ? el('p', { class: 'micro section-eyebrow', text: caption }) : null,
    el('div', { class: 'table-card' }, el('div', { class: 'table-scroll' }, el('table', { class: 'lb' }, [head, body]))),
  ]);
}

function trSparkline(series) {
  const max = Math.max(0.3, ...series.map((s) => Math.abs(s.ic)));
  return el('div', { style: 'display:flex;gap:2px;align-items:flex-end;height:48px;overflow-x:auto;padding-top:4px' },
    series.map((s) => el('div', {
      title: s.date + ': IC ' + trIc(s.ic),
      style: 'width:5px;flex:0 0 5px;height:' + Math.max(2, Math.round(Math.abs(s.ic) / max * 44)) + 'px;border-radius:1px;background:' + (s.ic >= 0 ? 'var(--success-accent)' : 'var(--brand-red-dark)'),
    })));
}

function viewTrackRecord() {
  const bt = STATE.backtest;
  if (!bt) {
    setView(el('section', { class: 'section container' }, el('div', { class: 'section-head' }, el('div', {}, [
      el('p', { class: 'micro section-eyebrow', text: 'Track Record' }),
      el('h2', { class: 'h2', text: 'Track record is being prepared' }),
      el('p', { class: 'section-sub', text: 'The backtest appears here after the next data refresh publishes it.' }),
    ]))));
    highlightNav('trackrecord');
    return;
  }
  const p = bt.panel, h = bt.headline, hr = bt.hit_rates || {}, dec = bt.decile || {};

  const freshNote = 'Cards 35 days old or younger' +
    ((h.fresh_release && h.fresh_release.mean_fwd_return != null)
      ? `, average ${p.primary_horizon_days}-day return ${signedPct(h.fresh_release.mean_fwd_return, 0)}.` : '.');
  const heads = el('div', { style: 'display:flex;flex-wrap:wrap;gap:14px;margin-top:6px' }, [
    h.fresh_release ? trHeadlineCard('Fresh releases (≤35 days old)', Object.assign({}, h.fresh_release, { note: freshNote }), 'Real, but ~a dozen releases') : null,
    h.all_cards ? trHeadlineCard('All cards ($2+)', h.all_cards, 'Modest') : null,
    h.mature_liquid ? trHeadlineCard('Mature & liquid (>90d, $10+)', h.mature_liquid, 'No edge') : null,
  ]);

  const fr = bt.fresh_by_release || {}, frs = fr.summary_in_sample || {};
  const verdict = el('div', { style: 'background:var(--surface-soft,#F7F7FB);border-radius:16px;padding:18px 20px;margin-top:18px' }, [
    el('p', { class: 'h3', style: 'margin:0 0 6px', text: 'What this replay suggests' }),
    el('p', { class: 'section-sub', style: 'margin:0', html:
      'The model’s one clear skill is ranking <strong>freshly released cards</strong>: within a new set it picked which cards would fall hardest in <strong>' +
      (frs.model_ic_positive != null ? frs.model_ic_positive + ' of ' + frs.n_releases : 'most') + '</strong> releases, and new-set cards fell in ' +
      (frs.median_return_negative != null ? frs.median_return_negative + ' of ' + frs.n_releases : 'most') +
      ' of them. A naive “cheapest in its rarity group” rule gets most of the way there. On established cards the model has <strong>no edge</strong>; ' +
      'there, what predicted the next month was the <strong>set</strong> (older sets, and sets already rising), not the individual card. See the signal table below.' }),
  ]);

  const SIG = bt.signals || {};
  const findB = (sig, seg) => (bt.baselines || []).find((b) => b.signal === sig && b.segment === seg) || {};
  const cellIc = (st) => st ? icCell(st.ic) : '—';
  const cellPos = (st) => st ? st.sign_pos + '/' + st.n_dates : '—';
  const cellIn = (st) => (st && st.ic_within_set != null) ? icCell(st.ic_within_set) : '—';
  const sigTables = ['fresh', 'mature_liquid', 'mature_high'].map((seg) => trMiniTable(
    'What predicted the next ' + p.primary_horizon_days + ' days — ' + ((bt.segments || {})[seg] || seg),
    ['Signal', 'IC (in-sample)', 'Within set', 'IC (out of sample)', 'Weeks +'],
    Object.keys(SIG).filter((k) => findB(k, seg).in_sample || findB(k, seg).oos).map((k) => {
      const b = findB(k, seg);
      return [SIG[k], cellIc(b.in_sample), cellIn(b.in_sample), cellIc(b.oos), cellPos(b.oos)];
    })));

  const relTable = trMiniTable('Fresh releases, one row per set (first ' + 35 + ' days)',
    ['Set', 'Released', 'Model IC', 'Naive IC', 'Median return'],
    (fr.in_sample || []).concat(fr.oos || []).map((r) => [r.set_name, r.release_date,
      r.model_ic == null ? '—' : icCell(r.model_ic), r.cheap_ic == null ? '—' : icCell(r.cheap_ic),
      signedPct(r.median_return, 0)]));

  const lv = bt.live;
  const liveNote = lv ? el('p', { class: 'caption', style: 'margin-top:14px', html:
    '<strong>Live track record</strong> (the calls this site actually published, ' + lv.first_date + ' → ' + lv.last_date + ', ' + lv.n_dates + ' weekly cuts): all cards IC ' +
    trIc((lv.segments.all || {}).ic) + ', mature $10+ IC ' + trIc((lv.segments.mature_liquid || {}).ic) + ', mature $100+ IC ' + trIc((lv.segments.mature_high || {}).ic) +
    '. Only a couple of independent months, so indicative only.' }) : null;

  const ageTable = trMiniTable('Edge by card age (since release)',
    ['Age bucket', 'IC', 'Avg 28d return', 'Obs'],
    (bt.by_age_bucket || []).map((r) => [r.bucket, icCell(r.ic), signedPct(r.mean_return, 0), r.n_obs.toLocaleString()]));
  const floorTable = trMiniTable('Edge by price floor',
    ['Min price', 'IC', 'Significance', 'Weeks'],
    (bt.floor_sensitivity || []).map((r) => ['$' + r.floor.toFixed(0) + '+', icCell(r.ic), 'HAC t ' + (r.nw_t == null ? '—' : r.nw_t), r.n_dates]));
  const rarTable = trMiniTable('Edge by rarity',
    ['Rarity', 'IC', 'Avg 28d return', 'Frac up', 'n'],
    (bt.by_rarity || []).map((r) => [r.key, icCell(r.ic), signedPct(r.mean_return, 0), PCT(r.frac_up, 0), r.n.toLocaleString()]));

  const fine = el('p', { class: 'caption', html:
    'Confident calls: undervalued cards rose <strong>' + PCT(hr.under, 0) + '</strong> of the time, overvalued fell <strong>' + PCT(hr.over, 0) +
    '</strong> — vs a ' + PCT(hr.base_up, 0) + ' coin-flip base rate. Most-undervalued decile returned ' + signedPct(dec.top_undervalued_return, 0) +
    ' vs ' + signedPct(dec.bottom_overvalued_return, 0) + ' for the most-overvalued (28-day).' });

  const section = el('section', { class: 'section container' }, [
    el('div', { class: 'section-head' }, [
      el('div', {}, [
        el('p', { class: 'micro section-eyebrow', text: 'Track Record · backtested on real TCGplayer history' }),
        el('h2', { class: 'h2', text: 'Does the model actually work?' }),
        el('p', { class: 'section-sub', text:
          'We replayed the model across ' + p.n_dates + ' weekly snapshots (' + p.first_date + ' → ' + p.last_date +
          '), refitting it with only the prices known on each date, then checked its over/under calls against what prices actually did ' +
          p.primary_horizon_days + ' days later. “Rank IC” measures how well the calls ranked future winners (0 = coin flip).' }),
      ]),
      el('span', { class: 'chip chip--lavender', text: p.n_dates + ' weeks · ' + p.primary_horizon_days + 'd horizon' }),
    ]),
    el('p', { class: 'stale-note', style: 'margin:6px 0 14px', text:
      `Each call is bought ${p.skip_days} days after the signal, and the model uses only popularity data that existed at the time. ` +
      `“In-sample” is the history the model and its rules were tuned on (to ${p.last_history_date}); “out of sample” is later data from this site’s own daily prices ` +
      `(${p.n_oos} overlapping weeks, so treat it as indicative). The backtest runs by hand, not in the daily refresh.` }),
    heads,
    verdict,
    el('div', { style: 'margin-top:20px;display:grid;gap:20px' }, sigTables),
    el('div', { style: 'margin-top:20px' }, relTable),
    liveNote,
    el('div', { class: 'lab-grid', style: 'margin-top:20px' }, [ageTable, floorTable]),
    el('div', { style: 'margin-top:20px' }, rarTable),
    el('div', { style: 'margin-top:20px' }, [
      el('p', { class: 'micro section-eyebrow', text: 'Weekly rank IC (all cards, 28-day) — green = the model ranked that week right' }),
      trSparkline(bt.ic_timeseries || []),
    ]),
    fine,
    el('div', { style: 'margin-top:14px' }, [
      el('p', { class: 'micro section-eyebrow', text: 'Honest caveats' }),
      el('ul', { class: 'caption', style: 'margin:6px 0 0;padding-left:18px' },
        (bt.caveats || []).map((c) => el('li', { text: c }))),
    ]),
    el('p', { class: 'caption', style: 'margin-top:12px', html:
      'Source: daily TCGplayer price history via tcgcsv.com (free, no scraping). Significance uses overlap-corrected (Newey-West) t-stats. This is a backtest of a statistical model, <strong>not</strong> investment advice.' }),
  ]);

  setView(section);
  highlightNav('trackrecord');
}

/* ============================================================
   Router
   ============================================================ */
const ROUTES = {
  '': viewHome,
  '/': viewHome,
  '/sets': viewSets,
  '/pricelab': viewPriceLab,
  '/movers': viewMovers,
  '/watchlists': viewWatchlists,
  '/trackrecord': viewTrackRecord,
  '/search': viewSearch,
};

function parseHash() {
  let h = location.hash.replace(/^#/, '');
  let cardId = null;
  const qi = h.indexOf('?');
  if (qi !== -1) {
    const params = new URLSearchParams(h.slice(qi + 1));
    cardId = params.get('card');
    h = h.slice(0, qi);
  }
  return { path: h || '/', cardId };
}

function router() {
  if (!STATE.loaded) return;
  const { path, cardId } = parseHash();
  const view = ROUTES[path] || ROUTES[path.replace(/\/$/, '')] || viewHome;
  view();
  if (cardId) openModal(cardId);
  else if (!$('#modalBackdrop').hidden) closeModal();
}

function highlightNav(route) {
  $$('.nav-tab').forEach((t) => t.classList.toggle('is-active', t.dataset.route === route));
  $$('.nav-mobile-link').forEach((t) => t.classList.toggle('is-active', t.dataset.route === route));
}

/* ============================================================
   Boot
   ============================================================ */
function showLoading() {
  setView(el('div', { class: 'state' }, [
    el('div', { class: 'spinner' }),
    el('div', { class: 'h3', text: 'Loading the lab…' }),
    el('p', { text: 'Fetching cards, sets and the price model.' }),
  ]));
}
function showError(err) {
  setView(el('div', { class: 'state' }, [
    el('div', { class: 'h3', text: 'Could not load data' }),
    el('p', { text: String(err && err.message || err) }),
    el('p', { class: 'caption', text: 'Make sure the site is served over http:// (not opened as a file://) so fetch can read ./data/*.json.' }),
  ]));
}

function wireChrome() {
  /* hamburger */
  const ham = $('#navHamburger');
  const mobile = $('#navMobile');
  ham.addEventListener('click', () => {
    const open = mobile.classList.toggle('is-open');
    ham.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  $$('.nav-mobile-link').forEach((l) => l.addEventListener('click', () => {
    mobile.classList.remove('is-open');
    ham.setAttribute('aria-expanded', 'false');
  }));

  /* modal: backdrop click + esc */
  $('#modalBackdrop').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeModal(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#modalBackdrop').hidden) closeModal();
  });

  /* footer: price freshness (or only the build date, for older data) */
  const meta = STATE.meta;
  if (meta) {
    const fb = $('#footerBuilt');
    if (fb) fb.textContent = refreshInfo(meta).footer;
    /* eBay source line: the date of the newest live snapshot when the build
       publishes it; older data keeps the static text in index.html. */
    const fe = $('#footerEbay');
    if (fe && meta.ebay_latest_snapshot) fe.textContent = `eBay listings (snapshot ${meta.ebay_latest_snapshot})`;
  }
}

async function boot() {
  showLoading();
  try {
    await loadData();
    wireChrome();
    window.addEventListener('hashchange', router);
    router();
  } catch (err) {
    console.error('[Poke Research] boot failed:', err);
    showError(err);
  }
}

document.addEventListener('DOMContentLoaded', boot);
