// Trader HQ: markets, charts, screeners and the order ticket.
// Uses helpers from dashboard.html: el, api, toast, money, money0, signed, pct, S (latest state).
"use strict";

const MK = {
  view: "bot", kind: "stock", q: "", offset: 0, pageSize: 50,
  symbol: null, range: "1D", chartType: "candles", info: null, bars: null,
  charts: [], series: {}, liveTimer: null, refetchTimer: null,
  studies: {volume: true, sma20: false, sma50: true, sma200: false, ema9: false, ema21: false, vwap: false, bb: false, rsi: false, macd: false},
  scanKind: "stock", moversMarket: "stocks",
  ticket: {side: "buy", order_class: "simple", type: "market", amount: "shares", trail: "percent"},
};
// Study colors follow the categorical palette, in fixed order.
const STUDY = {
  sma20: {label: "SMA 20", color: ["#2a78d6", "#3987e5"]}, sma50: {label: "SMA 50", color: ["#eb6834", "#d95926"]},
  sma200: {label: "SMA 200", color: ["#1baf7a", "#199e70"]}, ema9: {label: "EMA 9", color: ["#eda100", "#c98500"]},
  ema21: {label: "EMA 21", color: ["#e87ba4", "#d55181"]}, vwap: {label: "VWAP", color: ["#6250d6", "#9085e9"]},
  bb: {label: "Bollinger (20, 2)", color: ["#898781", "#898781"]},
};
const isDark = () => matchMedia("(prefers-color-scheme: dark)").matches;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const studyColor = (k) => STUDY[k].color[isDark() ? 1 : 0];
const UP = () => "#0ca30c", DOWN = () => "#d03b3b";
const fmtNum = (x, d = 2) => x === null || x === undefined || isNaN(x) ? "–" : Number(x).toLocaleString("en-US", {maximumFractionDigits: d, minimumFractionDigits: d});
const fmtBig = (x) => {
  if (x === null || x === undefined || isNaN(x)) return "–";
  const a = Math.abs(x);
  for (const [v, s] of [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]]) if (a >= v) return (x / v).toFixed(2) + s;
  return Math.round(x).toLocaleString("en-US");
};
const fmtPrice = (x) => x === null || x === undefined ? "–" : x >= 1 ? money(x) : "$" + Number(x).toPrecision(4);
const chg = (q) => {
  if (!q || q.change_pct === null || q.change_pct === undefined) return el("span", {class: "muted"}, "–");
  const up = q.change_pct > 0, down = q.change_pct < 0;
  return el("span", {class: up ? "up" : down ? "down" : ""}, (up ? "▲ " : down ? "▼ " : "") + pct(q.change_pct));
};
const safeUrl = (u) => typeof u === "string" && /^https:\/\//i.test(u) ? u : null;
const ext = (u, text) => safeUrl(u) ? el("a", {href: safeUrl(u), target: "_blank", rel: "noopener noreferrer"}, text) : el("span", {}, text);
const enc = (s) => encodeURIComponent(s);
const timeAgo = (iso) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(s)) return "";
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return new Date(iso).toLocaleDateString([], {month: "short", day: "numeric"});
};
const loading = (text) => el("div", {class: "empty"}, el("div", {class: "spinner", style: "display:inline-block;vertical-align:middle;margin-right:8px"}), text || "Loading…");

// ---------- routing ----------
function route() {
  const h = decodeURIComponent(location.hash.slice(1));
  let view = "bot";
  if (h.startsWith("s/")) { view = "symbol"; openSymbol(h.slice(2)); }
  else if (h.startsWith("markets")) { view = "markets"; const k = h.split("/")[1]; if (k) MK.kind = k; renderMarkets(); }
  else if (h === "scanner") { view = "scanner"; renderScanner(); }
  else if (h === "orders") { view = "orders"; renderOrdersView(); }
  MK.view = view;
  if (view !== "symbol") stopLive();
  for (const v of ["bot", "markets", "scanner", "orders", "symbol"]) {
    const node = document.getElementById("view-" + v); if (node) node.hidden = v !== view;
  }
  document.querySelectorAll("#nav a").forEach((a) => a.setAttribute("aria-current", a.dataset.view === (view === "symbol" ? "markets" : view) ? "page" : "false"));
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
const go = (sym) => { location.hash = "#s/" + enc(sym); };

// ---------- header search ----------
function setupSearch() {
  const input = document.getElementById("symSearch"), list = document.getElementById("symResults");
  let timer = null, rows = [];
  const close = () => { list.hidden = true; };
  const show = () => {
    list.replaceChildren(...rows.map((r) => el("button", {type: "button", class: "sr-row", onmousedown: (e) => { e.preventDefault(); input.value = ""; close(); go(r.symbol); }},
      el("b", {}, r.symbol), el("span", {}, r.name), el("span", {class: "tag"}, r.kind.toUpperCase()))));
    list.hidden = !rows.length;
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { rows = []; close(); return; }
    timer = setTimeout(async () => {
      try { rows = (await api("/api/market/search?q=" + enc(q))).rows; show(); } catch (e) { toast(e.message, 6000); }
    }, 200);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); const r = rows[0]; const sym = r ? r.symbol : input.value.trim().toUpperCase(); if (sym) { input.value = ""; close(); go(sym); } }
    if (e.key === "Escape") { close(); input.blur(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}

// ---------- markets list ----------
async function renderMarkets() {
  const root = document.getElementById("view-markets");
  if (!root.dataset.built) {
    root.dataset.built = "1";
    root.append(
      el("div", {class: "card"},
        el("div", {class: "row", style: "margin-bottom:12px"},
          el("h2", {style: "margin:0;flex:1"}, "Markets"),
          el("input", {type: "search", id: "mkQ", placeholder: "Filter by symbol or name", style: "max-width:280px", "aria-label": "Filter list"})),
        el("div", {class: "tabs", role: "tablist", id: "mkTabs"},
          ["stock", "etf", "crypto"].map((k) => el("button", {role: "tab", type: "button", "data-kind": k, onclick: () => { location.hash = "#markets/" + k; }}, {stock: "Stocks", etf: "ETFs", crypto: "Crypto"}[k]))),
        el("p", {class: "hint"}, "Every stock, ETF and crypto pair Alpaca can trade (over-the-counter stocks excluded). ETFs are recognized by name, so a few may be listed under Stocks. Click any row for its chart and details."),
        el("div", {id: "mkTable", class: "scroll"}),
        el("div", {class: "row", style: "justify-content:space-between;margin-top:10px"},
          el("button", {class: "btn small", type: "button", id: "mkPrev"}, "← Previous"),
          el("span", {id: "mkPage", class: "hint", style: "margin:0"}),
          el("button", {class: "btn small", type: "button", id: "mkNext"}, "Next →"))));
    let t = null;
    document.getElementById("mkQ").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { MK.q = e.target.value; MK.offset = 0; loadMarkets(); }, 250); });
    document.getElementById("mkPrev").onclick = () => { MK.offset = Math.max(0, MK.offset - MK.pageSize); loadMarkets(); };
    document.getElementById("mkNext").onclick = () => { MK.offset += MK.pageSize; loadMarkets(); };
  }
  if (root.dataset.kind !== MK.kind) { MK.offset = 0; root.dataset.kind = MK.kind; }
  document.querySelectorAll("#mkTabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.kind === MK.kind));
  loadMarkets();
}
async function loadMarkets() {
  const box = document.getElementById("mkTable");
  box.replaceChildren(loading("Loading the list (the first time downloads every symbol; this takes a few seconds)…"));
  let data;
  try { data = await api(`/api/market/assets?kind=${MK.kind}&q=${enc(MK.q)}&offset=${MK.offset}&limit=${MK.pageSize}`); }
  catch (e) { box.replaceChildren(el("div", {class: "empty"}, e.message)); return; }
  const c = data.counts || {};
  document.querySelectorAll("#mkTabs button").forEach((b) => { b.textContent = {stock: "Stocks", etf: "ETFs", crypto: "Crypto"}[b.dataset.kind] + (c[b.dataset.kind] ? " (" + c[b.dataset.kind].toLocaleString() + ")" : ""); });
  if (!data.rows.length) { box.replaceChildren(el("div", {class: "empty"}, "Nothing matches.")); }
  else box.replaceChildren(el("table", {class: "click"},
    el("thead", {}, el("tr", {}, el("th", {}, "Symbol"), el("th", {}, "Name"), el("th", {class: "num"}, "Price"), el("th", {class: "num"}, "Change"), el("th", {class: "num"}, "Volume"))),
    el("tbody", {}, data.rows.map((r) => el("tr", {tabindex: 0, onclick: () => go(r.symbol), onkeydown: (e) => e.key === "Enter" && go(r.symbol)},
      el("td", {}, el("b", {}, r.symbol)), el("td", {class: "name"}, r.name),
      el("td", {class: "num"}, r.quote ? fmtPrice(r.quote.price) : "–"), el("td", {class: "num"}, chg(r.quote)),
      el("td", {class: "num"}, r.quote ? fmtBig(r.quote.volume) : "–"))))));
  const end = Math.min(MK.offset + MK.pageSize, data.total);
  document.getElementById("mkPage").textContent = data.total ? `${(MK.offset + 1).toLocaleString()}–${end.toLocaleString()} of ${data.total.toLocaleString()}` : "";
  document.getElementById("mkPrev").disabled = MK.offset === 0;
  document.getElementById("mkNext").disabled = end >= data.total;
}

// ---------- symbol page ----------
async function openSymbol(sym) {
  sym = sym.toUpperCase();
  const root = document.getElementById("view-symbol");
  if (MK.symbol !== sym) {
    MK.symbol = sym; MK.info = null;
    destroyCharts();
    root.replaceChildren(loading("Loading " + sym + "…"));
    try { MK.info = await api("/api/market/info?symbol=" + enc(sym)); }
    catch (e) { root.replaceChildren(el("div", {class: "card"}, el("h2", {}, sym), el("p", {}, e.message), el("a", {href: "#markets"}, "← Back to Markets"))); return; }
    if (MK.symbol !== sym) return;
    if (MK.info.asset.kind === "crypto") Object.assign(MK.ticket, {order_class: "simple", type: "market"});
    buildSymbolPage();
    loadChart();
    loadNews();
  }
  startLive();
}

function buildSymbolPage() {
  const {asset, company, stats, position} = MK.info;
  const root = document.getElementById("view-symbol");
  const logo = safeUrl(company.profile && company.profile.logo);
  root.replaceChildren(
    el("div", {class: "sym-head"},
      el("a", {href: "#markets/" + asset.kind, class: "back"}, "← " + {stock: "Stocks", etf: "ETFs", crypto: "Crypto"}[asset.kind]),
      el("div", {class: "row", style: "gap:12px;align-items:center"},
        logo ? el("img", {src: logo, alt: "", class: "logo"}) : null,
        el("div", {},
          el("h2", {class: "sym-title"}, company.name || asset.name),
          el("div", {class: "row", style: "gap:6px"}, el("b", {}, asset.symbol), asset.exchange && asset.exchange.toUpperCase() !== asset.kind.toUpperCase() ? el("span", {class: "tag"}, asset.exchange) : null, el("span", {class: "tag"}, asset.kind.toUpperCase()),
            company.profile && company.profile.industry ? el("span", {class: "tag"}, company.profile.industry) : null))),
      el("div", {class: "price-row"}, el("span", {id: "symPrice", class: "big"}, "–"), el("span", {id: "symChange"}), el("span", {id: "symLive", class: "pill"}, el("span", {class: "dot"}), el("span", {}, "")))),
    el("div", {class: "sym-grid"},
      el("div", {class: "sym-main"},
        el("section", {class: "card"},
          el("div", {class: "row", style: "justify-content:space-between"},
            el("div", {class: "seg", role: "group", "aria-label": "Range", id: "rangeSeg"},
              ["1D", "1W", "1M", "3M", "YTD", "1Y", "5Y", "ALL"].map((r) => el("button", {type: "button", "data-r": r, "aria-pressed": r === MK.range, onclick: () => { MK.range = r; loadChart(); }}, r === "ALL" ? "All" : r))),
            el("div", {class: "seg", role: "group", "aria-label": "Chart type"},
              ["candles", "line"].map((t) => el("button", {type: "button", "data-t": t, "aria-pressed": t === MK.chartType, onclick: () => { MK.chartType = t; document.querySelectorAll("[data-t]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.t === t)); drawCharts(); }}, t === "candles" ? "Candles" : "Line")))),
          el("div", {class: "studies", id: "studyBar"},
            Object.keys(MK.studies).map((k) => el("label", {class: "study"},
              el("input", {type: "checkbox", checked: MK.studies[k], onchange: (e) => { MK.studies[k] = e.target.checked; drawCharts(); }}),
              STUDY[k] ? el("span", {class: "key", style: `background:${studyColor(k)}`}) : null,
              {volume: "Volume", rsi: "RSI (14)", macd: "MACD (12, 26, 9)"}[k] || STUDY[k].label))),
          el("div", {class: "chart-box"}, el("div", {id: "ohlc", class: "ohlc"}), el("div", {id: "mainChart", class: "main-chart"})),
          el("div", {id: "rsiChart", class: "sub-chart", hidden: true}),
          el("div", {id: "macdChart", class: "sub-chart", hidden: true}),
          el("div", {class: "row", style: "justify-content:space-between;margin-top:6px"},
            el("span", {id: "chartNote", class: "hint", style: "margin:0"}),
            el("a", {href: "https://www.tradingview.com/", target: "_blank", rel: "noopener noreferrer", class: "hint", style: "margin:0"}, "Charts by TradingView Lightweight Charts™"))),
        el("section", {class: "card"}, el("h2", {}, "Key stats"), el("div", {class: "stats", id: "statsGrid"})),
        company.about || (company.profile && Object.keys(company.profile).length) ? el("section", {class: "card"}, el("h2", {}, "About"),
          company.about ? el("p", {class: "about"}, company.about) : null,
          profileList(company.profile || {}),
          el("p", {class: "hint"}, "Sources: ", ...(company.sources || []).flatMap((s, i) => [i ? ", " : "", ext(s.url, s.name)]),
            company.about ? ". The description is matched by company name and may occasionally be about a different company." : "")) : null,
        company.financials && company.financials.length ? el("section", {class: "card"}, el("h2", {}, "Financials (yearly, from SEC filings)"),
          el("div", {class: "scroll"}, el("table", {},
            el("thead", {}, el("tr", {}, el("th", {}, "Fiscal year ending"), el("th", {class: "num"}, "Revenue"), el("th", {class: "num"}, "Net income"), el("th", {class: "num"}, "EPS (diluted)"))),
            el("tbody", {}, company.financials.slice().reverse().map((f) => el("tr", {}, el("td", {}, f.year_end), el("td", {class: "num"}, f.revenue === null ? "–" : "$" + fmtBig(f.revenue)),
              el("td", {class: "num " + (f.net_income < 0 ? "down" : "")}, f.net_income === null ? "–" : (f.net_income < 0 ? "-$" : "$") + fmtBig(Math.abs(f.net_income))),
              el("td", {class: "num"}, f.eps_diluted === null ? "–" : money(f.eps_diluted)))))))) : null,
        el("section", {class: "card"}, el("h2", {}, "News"), el("div", {id: "newsList"}, loading()))),
      el("aside", {class: "sym-side"},
        position ? positionCard(position) : null,
        el("section", {class: "card ticket", id: "ticket"}),
        (company.notes || []).length ? el("section", {class: "card"}, el("h2", {}, "Data notes"), ...company.notes.map((n) => el("p", {class: "hint"}, n))) : null)));
  renderStats();
  renderTicket();
}

function profileList(p) {
  const rows = [["Industry", p.industry], ["Headquarters", p.headquarters], ["Website", p.website], ["Fiscal year ends", p.fiscal_year_end],
    ["IPO date", p.ipo_date], ["Incorporated in", p.incorporated_in], ["SEC filings", p.sec_url]].filter((r) => r[1]);
  if (!rows.length) return null;
  return el("dl", {class: "profile"}, rows.flatMap(([k, v]) => [el("dt", {}, k),
    el("dd", {}, k === "Website" || k === "SEC filings" ? ext(/^https?:/i.test(v) ? v.replace(/^http:/i, "https:") : "https://" + v, k === "SEC filings" ? "View on SEC.gov" : v.replace(/^https?:\/\//i, "")) : v)]));
}

function renderStats() {
  if (!MK.info) return;
  const q = MK.info.quote || {}, st = MK.info.stats || {}, crypto = MK.info.asset.kind === "crypto";
  const rows = [
    ["Open", fmtPrice(q.open)], ["Day high", fmtPrice(q.high)], ["Day low", fmtPrice(q.low)], ["Previous close", fmtPrice(q.prev_close)],
    ["Volume", fmtBig(q.volume)], ["Avg volume (30 days)", fmtBig(st.avg_volume_30d)],
    ["52-week high", fmtPrice(st.high_52w)], ["52-week low", fmtPrice(st.low_52w)],
  ];
  if (!crypto) rows.push(["Market cap", st.market_cap ? "$" + fmtBig(st.market_cap) : "–"], ["P/E ratio", st.pe_ratio ? fmtNum(st.pe_ratio) : "–"],
    ["EPS (last fiscal year)", MK.info.company.facts && MK.info.company.facts.eps_diluted !== undefined ? money(MK.info.company.facts.eps_diluted) : "–"],
    ["Shares outstanding", MK.info.company.facts && MK.info.company.facts.shares_outstanding ? fmtBig(MK.info.company.facts.shares_outstanding) : "–"],
    ["Dividend yield", st.dividend_yield_pct !== undefined ? fmtNum(st.dividend_yield_pct) + "%" : "–"], ["Beta", st.beta !== undefined ? fmtNum(st.beta) : "–"]);
  rows.push(["Bid / Ask", q.bid || q.ask ? fmtPrice(q.bid) + " / " + fmtPrice(q.ask) : "–"]);
  document.getElementById("statsGrid").replaceChildren(...rows.map(([k, v]) => el("div", {class: "stat"}, el("span", {}, k), el("b", {}, v))));
  const pe = document.getElementById("symPrice"), ce = document.getElementById("symChange");
  if (pe) { pe.textContent = fmtPrice(q.price); ce.replaceChildren(chg(q), q.change !== null && q.change !== undefined ? el("span", {class: "muted"}, " (" + (q.change >= 0 ? "+" : "-") + fmtPrice(Math.abs(q.change)) + ")") : ""); }
}

function positionCard(p) {
  return el("section", {class: "card"}, el("h2", {}, "Your position"),
    el("div", {class: "stats", style: "grid-template-columns:1fr 1fr"},
      el("div", {class: "stat"}, el("span", {}, "Quantity"), el("b", {}, String(p.qty))),
      el("div", {class: "stat"}, el("span", {}, "Average cost"), el("b", {}, fmtPrice(p.avg_entry_price))),
      el("div", {class: "stat"}, el("span", {}, "Market value"), el("b", {}, money(p.market_value))),
      el("div", {class: "stat"}, el("span", {}, "Profit / loss"), el("b", {class: p.unrealized_pl > 0 ? "up" : p.unrealized_pl < 0 ? "down" : ""}, signed(p.unrealized_pl) + " (" + pct(p.unrealized_plpc) + ")"))),
    el("div", {class: "row", style: "margin-top:10px"},
      MK.info.asset.kind !== "crypto" ? el("button", {class: "btn small", type: "button", onclick: () => {
        const price = (MK.info.quote || {}).price || p.current_price;
        Object.assign(MK.ticket, {side: "sell", order_class: "oco", type: "limit", amount: "shares"});
        renderTicket({qty: p.qty, take_profit: (price * 1.05).toFixed(2), stop_loss: (price * 0.95).toFixed(2)});
        document.getElementById("ticket").scrollIntoView({behavior: "smooth"});
      }}, "Protect with OCO") : null,
      el("button", {class: "btn small danger", type: "button", onclick: async () => {
        if (!confirm(`Sell all ${p.qty} ${MK.symbol} at the market price now?`)) return;
        try { await api("/api/positions/close", {symbol: MK.symbol}); toast("Sell order sent."); } catch (e) { toast(e.message, 8000); }
      }}, "Sell all at market")));
}

async function loadNews() {
  const box = document.getElementById("newsList");
  try {
    const rows = (await api("/api/market/news?symbol=" + enc(MK.symbol))).rows;
    box.replaceChildren(...(rows.length ? rows.map((n) => el("article", {class: "news"},
      safeUrl(n.image) ? el("img", {src: safeUrl(n.image), alt: "", loading: "lazy"}) : null,
      el("div", {}, el("div", {class: "news-h"}, ext(n.url, n.headline)), el("div", {class: "hint", style: "margin:2px 0"}, n.source + " · " + timeAgo(n.created_at)),
        n.summary ? el("p", {class: "news-s"}, n.summary) : null))) : [el("div", {class: "empty"}, "No recent news.")]));
  } catch (e) { box.replaceChildren(el("div", {class: "empty"}, e.message)); }
}

// ---------- charts ----------
async function loadChart() {
  document.querySelectorAll("#rangeSeg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.r === MK.range));
  const sym = MK.symbol, rng = MK.range;
  try { MK.bars = await api(`/api/market/bars?symbol=${enc(sym)}&range=${rng}`); }
  catch (e) { const n = document.getElementById("chartNote"); if (n) n.textContent = e.message; return; }
  if (sym !== MK.symbol || rng !== MK.range) return;
  drawCharts();
}
function destroyCharts() { for (const c of MK.charts) c.remove(); MK.charts = []; MK.series = {}; }
const intraday = () => MK.bars && /Min|Hour/.test(MK.bars.timeframe);
// Charts label times in UTC, so shift each timestamp to show the Mac's local time.
const localT = (t) => t - new Date(t * 1000).getTimezoneOffset() * 60;

function chartOptions(height, showTime) {
  return {
    autoSize: true, height,
    layout: {background: {type: "solid", color: css("--surface")}, textColor: css("--ink-2"), fontFamily: "system-ui, -apple-system, sans-serif", fontSize: 11},
    grid: {vertLines: {color: css("--grid")}, horzLines: {color: css("--grid")}},
    rightPriceScale: {borderColor: css("--axis")},
    timeScale: {borderColor: css("--axis"), timeVisible: showTime, secondsVisible: false, visible: true},
    crosshair: {mode: 0},
  };
}
function sma(v, n) { const out = []; let s = 0; for (let i = 0; i < v.length; i++) { s += v[i]; if (i >= n) s -= v[i - n]; out.push(i >= n - 1 ? s / n : null); } return out; }
function ema(v, n) { const out = [], k = 2 / (n + 1); let e = null; for (let i = 0; i < v.length; i++) { if (i === n - 1) e = v.slice(0, n).reduce((a, b) => a + b, 0) / n; else if (i >= n) e = v[i] * k + e * (1 - k); out.push(i >= n - 1 ? e : null); } return out; }
function bollinger(v, n = 20, k = 2) {
  const mid = sma(v, n), up = [], lo = [];
  for (let i = 0; i < v.length; i++) {
    if (mid[i] === null) { up.push(null); lo.push(null); continue; }
    const w = v.slice(i - n + 1, i + 1), sd = Math.sqrt(w.reduce((a, x) => a + (x - mid[i]) ** 2, 0) / n);
    up.push(mid[i] + k * sd); lo.push(mid[i] - k * sd);
  }
  return {mid, up, lo};
}
function rsi(v, n = 14) {
  const out = [null]; let g = 0, l = 0;
  for (let i = 1; i < v.length; i++) {
    const d = v[i] - v[i - 1], up = Math.max(d, 0), dn = Math.max(-d, 0);
    if (i <= n) { g += up / n; l += dn / n; out.push(i === n ? (l === 0 ? 100 : 100 - 100 / (1 + g / l)) : null); }
    else { g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n; out.push(l === 0 ? 100 : 100 - 100 / (1 + g / l)); }
  }
  return out;
}
function macd(v) {
  const f = ema(v, 12), s = ema(v, 26), line = v.map((_, i) => f[i] !== null && s[i] !== null ? f[i] - s[i] : null);
  const start = line.findIndex((x) => x !== null), sig = Array(v.length).fill(null);
  if (start >= 0) { const e = ema(line.slice(start), 9); e.forEach((x, i) => { sig[start + i] = x; }); }
  return {line, sig, hist: line.map((x, i) => x !== null && sig[i] !== null ? x - sig[i] : null)};
}
function vwapSeries(bars) {
  const out = []; let day = null, pv = 0, vol = 0;
  for (const b of bars) {
    const d = new Date(b.t * 1000).toDateString();
    if (d !== day) { day = d; pv = 0; vol = 0; }
    pv += (b.h + b.l + b.c) / 3 * b.v; vol += b.v; out.push(vol ? pv / vol : null);
  }
  return out;
}
const lineData = (bars, vals) => bars.map((b, i) => vals[i] === null || vals[i] === undefined ? {time: localT(b.t)} : {time: localT(b.t), value: vals[i]});

function drawCharts() {
  if (!window.LightweightCharts) { document.getElementById("chartNote").textContent = "The chart library didn't load."; return; }
  destroyCharts();
  const bars = (MK.bars && MK.bars.bars) || [];
  const note = document.getElementById("chartNote");
  note.textContent = bars.length ? `${bars.length} bars · ${MK.bars.timeframe.replace("Min", "-minute").replace("1Hour", "hourly").replace("1Day", "daily").replace("1Week", "weekly").replace("1Month", "monthly")} · prices adjusted for splits` : "No price history for this range.";
  const LWC = window.LightweightCharts, showTime = intraday();
  const main = LWC.createChart(document.getElementById("mainChart"), chartOptions(420, showTime));
  MK.charts.push(main);
  const closes = bars.map((b) => b.c);
  const priceSeries = MK.chartType === "candles"
    ? main.addCandlestickSeries({upColor: UP(), downColor: DOWN(), wickUpColor: UP(), wickDownColor: DOWN(), borderVisible: false})
    : main.addLineSeries({color: css("--series"), lineWidth: 2, priceLineVisible: true});
  priceSeries.setData(MK.chartType === "candles" ? bars.map((b) => ({time: localT(b.t), open: b.o, high: b.h, low: b.l, close: b.c})) : bars.map((b) => ({time: localT(b.t), value: b.c})));
  MK.series.price = priceSeries;
  if (MK.studies.volume) {
    const vol = main.addHistogramSeries({priceFormat: {type: "volume"}, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false});
    main.priceScale("vol").applyOptions({scaleMargins: {top: 0.82, bottom: 0}});
    vol.setData(bars.map((b, i) => ({time: localT(b.t), value: b.v, color: (i && b.c < bars[i - 1].c ? DOWN() : UP()) + "66"})));
    MK.series.volume = vol;
  }
  const addLine = (key, vals, opts) => { const s = main.addLineSeries({color: studyColor(key), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...(opts || {})}); s.setData(lineData(bars, vals)); return s; };
  for (const [k, n] of [["sma20", 20], ["sma50", 50], ["sma200", 200]]) if (MK.studies[k]) addLine(k, sma(closes, n));
  for (const [k, n] of [["ema9", 9], ["ema21", 21]]) if (MK.studies[k]) addLine(k, ema(closes, n));
  if (MK.studies.vwap) addLine("vwap", showTime ? vwapSeries(bars) : bars.map((b) => b.vw ?? null));
  if (MK.studies.bb) { const bb = bollinger(closes); addLine("bb", bb.up, {lineWidth: 1}); addLine("bb", bb.mid, {lineWidth: 1, lineStyle: 2}); addLine("bb", bb.lo, {lineWidth: 1}); }
  main.timeScale().fitContent();

  const subs = [];
  const rsiBox = document.getElementById("rsiChart"), macdBox = document.getElementById("macdChart");
  rsiBox.hidden = !MK.studies.rsi; macdBox.hidden = !MK.studies.macd;
  if (MK.studies.rsi) {
    const c = LWC.createChart(rsiBox, chartOptions(130, showTime)); MK.charts.push(c); subs.push(c);
    const s = c.addLineSeries({color: css("--series"), lineWidth: 2, priceLineVisible: false});
    s.setData(lineData(bars, rsi(closes)));
    s.createPriceLine({price: 70, color: css("--axis"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "70"});
    s.createPriceLine({price: 30, color: css("--axis"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "30"});
  }
  if (MK.studies.macd) {
    const c = LWC.createChart(macdBox, chartOptions(150, showTime)); MK.charts.push(c); subs.push(c);
    const m = macd(closes);
    c.addHistogramSeries({priceLineVisible: false, lastValueVisible: false}).setData(bars.map((b, i) => m.hist[i] === null ? {time: localT(b.t)} : {time: localT(b.t), value: m.hist[i], color: (m.hist[i] >= 0 ? UP() : DOWN()) + "99"}));
    c.addLineSeries({color: studyColor("sma20"), lineWidth: 2, priceLineVisible: false}).setData(lineData(bars, m.line));
    c.addLineSeries({color: studyColor("sma50"), lineWidth: 2, priceLineVisible: false}).setData(lineData(bars, m.sig));
  }
  for (const c of subs) {
    main.timeScale().subscribeVisibleLogicalRangeChange((r) => r && c.timeScale().setVisibleLogicalRange(r));
    c.timeScale().subscribeVisibleLogicalRangeChange((r) => r && main.timeScale().setVisibleLogicalRange(r));
  }
  const ohlc = document.getElementById("ohlc");
  const showBar = (b) => {
    if (!b) { ohlc.textContent = ""; return; }
    const ch = b.c - b.o;
    ohlc.replaceChildren(el("span", {}, "O ", el("b", {}, fmtNum(b.o, b.o < 1 ? 4 : 2))), el("span", {}, "H ", el("b", {}, fmtNum(b.h, b.h < 1 ? 4 : 2))),
      el("span", {}, "L ", el("b", {}, fmtNum(b.l, b.l < 1 ? 4 : 2))), el("span", {}, "C ", el("b", {}, fmtNum(b.c, b.c < 1 ? 4 : 2))),
      el("span", {class: ch >= 0 ? "up" : "down"}, (ch >= 0 ? "+" : "") + fmtNum(ch / b.o * 100) + "%"), el("span", {}, "Vol ", el("b", {}, fmtBig(b.v))));
  };
  const byTime = new Map(bars.map((b) => [localT(b.t), b]));
  main.subscribeCrosshairMove((p) => showBar(p && p.time ? byTime.get(p.time) : bars[bars.length - 1]));
  showBar(bars[bars.length - 1]);
}

// ---------- live prices ----------
function startLive() {
  stopLive();
  const tick = async () => {
    if (MK.view !== "symbol" || !MK.info) return;
    const crypto = MK.info.asset.kind === "crypto", open = crypto || (S && S.clock && S.clock.is_open);
    const pill = document.getElementById("symLive");
    if (pill) { pill.className = "pill " + (open ? "open" : ""); pill.lastChild.textContent = open ? "Live · updates every 2 s" : "Market closed · last price"; }
    if (!open) return;
    try {
      const q = (await api("/api/market/quote?symbols=" + enc(MK.symbol))).quotes[MK.symbol];
      if (!q || !q.price) return;
      MK.info.quote = q; renderStats(); updateLastBar(q.price);
    } catch (_) {}
  };
  tick();
  MK.liveTimer = setInterval(tick, 2000);
  MK.refetchTimer = setInterval(() => { if (intraday()) loadChart(); }, 60000);
}
function stopLive() { clearInterval(MK.liveTimer); clearInterval(MK.refetchTimer); MK.liveTimer = MK.refetchTimer = null; }
function updateLastBar(price) {
  const bars = MK.bars && MK.bars.bars; if (!bars || !bars.length || !MK.series.price) return;
  const b = bars[bars.length - 1];
  b.c = price; b.h = Math.max(b.h, price); b.l = Math.min(b.l, price);
  MK.series.price.update(MK.chartType === "candles" ? {time: localT(b.t), open: b.o, high: b.h, low: b.l, close: b.c} : {time: localT(b.t), value: b.c});
}

// ---------- order ticket ----------
function renderTicket(prefill) {
  const box = document.getElementById("ticket"); if (!box || !MK.info) return;
  const t = MK.ticket, crypto = MK.info.asset.kind === "crypto", held = MK.info.position ? MK.info.position.qty : 0;
  const price = (MK.info.quote || {}).price || 0, p2 = (x) => x ? (x >= 1 ? x.toFixed(2) : x.toPrecision(4)) : "";
  const classes = crypto ? [["simple", "Simple"]] : [["simple", "Simple"], ["bracket", "Bracket"], ["oto", "OTO"], ["oco", "OCO (exit)"]];
  if (!classes.some((c) => c[0] === t.order_class)) t.order_class = "simple";
  if (t.order_class === "oco") { t.side = "sell"; t.type = "limit"; }
  let types = [["market", "Market"], ["limit", "Limit"], ["stop", "Stop (stop-loss)"], ["stop_limit", "Stop limit"], ["trailing_stop", "Trailing stop"]];
  if (crypto) types = types.filter((x) => ["market", "limit", "stop_limit"].includes(x[0]));
  if (t.order_class === "bracket" || t.order_class === "oto") types = types.filter((x) => ["market", "limit"].includes(x[0]));
  if (t.order_class === "oco") types = [["limit", "Limit"]];
  if (!types.some((x) => x[0] === t.type)) t.type = types[0][0];
  if (t.amount === "dollars" && !(t.type === "market" && t.order_class === "simple" && (MK.info.asset.fractionable || crypto))) t.amount = "shares";
  const tifs = crypto ? [["gtc", "Good till canceled"], ["ioc", "Immediate or cancel"]] : [["day", "Day"], ["gtc", "Good till canceled"]];
  const f = Object.assign({qty: "", notional: "", limit_price: p2(price), stop_price: p2(price * (t.side === "buy" ? 1.02 : 0.97)), trail: "3",
    take_profit: p2(price * (t.side === "buy" ? 1.05 : 0.95)), stop_loss: p2(price * (t.side === "buy" ? 0.97 : 1.03)), stop_loss_limit: "", tif: tifs[0][0], ext: false}, box._vals || {}, prefill || {});
  const seg = (name, opts, cur, cb) => el("div", {class: "seg full", role: "group", "aria-label": name}, opts.map(([v, label]) => el("button", {type: "button", "aria-pressed": v === cur, onclick: () => { cb(v); keep(); renderTicket(); }}, label)));
  const inp = (label, name, help, attrs) => el("label", {}, label, help ? el("small", {}, help) : null, el("input", {type: "number", name, value: f[name], step: "any", min: 0, inputmode: "decimal", ...(attrs || {})}));
  const keep = () => { const v = {}; box.querySelectorAll("input,select").forEach((i) => { v[i.name] = i.type === "checkbox" ? i.checked : i.value; }); box._vals = v; };
  const help = {simple: "One order.", bracket: "Buy, then automatically place a take-profit and a stop-loss once it fills. When one triggers, the other is canceled.",
    oto: "Buy, then automatically place one exit order (take-profit or stop-loss) once it fills.", oco: "For shares you own: a take-profit and a stop-loss together. When one fills, the other is canceled."}[t.order_class];
  const typeHelp = {market: "Fills right away at the best available price.", limit: "Fills only at your price or better.", stop: "Becomes a market order once the price reaches your stop. Use it as a stop-loss.",
    stop_limit: "Becomes a limit order once the price reaches your stop.", trailing_stop: "A stop that follows the price up (for sells) and triggers if the price falls back by your trail."}[t.type];
  const showLimit = (t.type === "limit" || t.type === "stop_limit") && t.order_class !== "oco";
  const fields = [
    el("h2", {}, "Trade " + MK.symbol),
    seg("Side", [["buy", "Buy"], ["sell", "Sell"]], t.side, (v) => { t.side = v; if (v === "buy" && t.order_class === "oco") t.order_class = "simple"; }),
    el("label", {}, "Strategy", el("select", {name: "order_class", onchange: (e) => { t.order_class = e.target.value; keep(); renderTicket(); }}, classes.map(([v, l]) => el("option", {value: v, selected: v === t.order_class}, l)))),
    el("p", {class: "hint"}, help),
    el("label", {}, "Order type", el("select", {name: "type", onchange: (e) => { t.type = e.target.value; keep(); renderTicket(); }}, types.map(([v, l]) => el("option", {value: v, selected: v === t.type}, l)))),
    el("p", {class: "hint"}, typeHelp),
  ];
  const canDollars = t.type === "market" && t.order_class === "simple" && (MK.info.asset.fractionable || crypto);
  if (canDollars) fields.push(seg("Amount in", [["shares", crypto ? "Coins" : "Shares"], ["dollars", "Dollars"]], t.amount, (v) => { t.amount = v; }));
  fields.push(t.amount === "dollars" ? inp("Amount ($)", "notional") : inp(crypto ? "Quantity (coins)" : "Quantity (shares)", "qty", held && t.side === "sell" ? `You own ${held}.` : ""));
  if (showLimit) fields.push(inp("Limit price ($)", "limit_price"));
  if (t.type === "stop" || t.type === "stop_limit") fields.push(inp("Stop price ($)", "stop_price", t.side === "sell" ? "Sells if the price falls to this." : "Buys if the price rises to this."));
  if (t.type === "trailing_stop") fields.push(seg("Trail by", [["percent", "Percent"], ["price", "Dollars"]], t.trail, (v) => { t.trail = v; }), inp(t.trail === "percent" ? "Trail (%)" : "Trail ($)", "trail"));
  if (t.order_class !== "simple") {
    fields.push(inp("Take-profit price ($)", "take_profit", t.order_class === "oto" ? "Leave empty to use a stop-loss instead." : ""));
    fields.push(inp("Stop-loss price ($)", "stop_loss", t.order_class === "oto" ? "Leave empty to use a take-profit instead." : ""));
    if (t.order_class !== "oto") fields.push(inp("Stop-loss limit price ($, optional)", "stop_loss_limit", "Leave empty for a plain stop."));
  }
  fields.push(el("label", {}, "Time in force", el("select", {name: "tif"}, tifs.map(([v, l]) => el("option", {value: v, selected: v === f.tif}, l)))));
  if (!crypto && t.type === "limit" && t.order_class === "simple") fields.push(el("label", {class: "check"}, el("input", {type: "checkbox", name: "ext", checked: !!f.ext}), el("span", {}, "Allow pre-market and after-hours (Day orders)")));
  const result = el("div", {id: "ticketResult", class: "msg"});
  fields.push(el("div", {class: "row"}, el("button", {class: "btn primary", type: "submit"}, "Review order"), el("button", {class: "btn", type: "reset", onclick: () => { box._vals = null; setTimeout(() => renderTicket(), 0); }}, "Clear")), result);
  const form = el("form", {class: "form", onsubmit: (e) => { e.preventDefault(); keep(); review(); }}, fields);
  box.replaceChildren(form);
  if (S && S.mode === "live") box.prepend(el("div", {class: "banner live", style: "margin-bottom:10px"}, "LIVE: real money"));
}
function ticketSpec() {
  const v = document.getElementById("ticket")._vals || {}, t = MK.ticket;
  const spec = {symbol: MK.symbol, side: t.side, type: t.type, order_class: t.order_class, time_in_force: v.tif, extended_hours: !!v.ext};
  if (t.amount === "dollars") spec.notional = v.notional; else spec.qty = v.qty;
  if (t.order_class !== "oco") { spec.limit_price = v.limit_price; }
  spec.stop_price = v.stop_price;
  if (t.type === "trailing_stop") spec[t.trail === "percent" ? "trail_percent" : "trail_price"] = v.trail;
  if (t.order_class !== "simple") { spec.take_profit = v.take_profit || null; spec.stop_loss = v.stop_loss || null; spec.stop_loss_limit = v.stop_loss_limit || null; }
  for (const k of Object.keys(spec)) if (spec[k] === "" || spec[k] === undefined) delete spec[k];
  return spec;
}
async function review() {
  const res = document.getElementById("ticketResult"), spec = ticketSpec();
  res.className = "msg"; res.textContent = "Checking…";
  let p;
  try { p = await api("/api/orders/preview", {order: spec}); }
  catch (e) { res.className = "msg err"; res.textContent = e.message; return; }
  if (!p.confirm) return send(spec);
  res.replaceChildren(el("div", {class: "review"}, el("b", {}, p.summary), el("div", {class: "hint", style: "margin:4px 0 8px"}, "Estimated " + (spec.side === "buy" ? "cost" : "value") + ": " + money(p.estimate || 0)),
    el("div", {class: "row"}, el("button", {class: "btn primary", type: "button", onclick: () => send({...spec, confirmed: true})}, "Place order"),
      el("button", {class: "btn", type: "button", onclick: () => { res.textContent = ""; }}, "Cancel"))));
}
async function send(spec) {
  const res = document.getElementById("ticketResult");
  res.className = "msg"; res.textContent = "Sending…";
  try {
    const r = await api("/api/orders", {order: spec});
    res.className = "msg ok"; res.textContent = `Order ${r.status || "sent"} in ${r.took_ms} ms: ${r.summary}`;
    toast(`Order sent in ${r.took_ms} ms.`);
    setTimeout(async () => { try { MK.info = {...MK.info, ...(await api("/api/market/info?symbol=" + enc(MK.symbol)))}; } catch (_) {} }, 1500);
  } catch (e) {
    res.className = "msg err"; res.textContent = e.message;
    if (e.needLive) toast("Type LIVE on the Bot page first.", 7000);
  }
}

// ---------- scanner ----------
const PRESETS = [
  ["Gainers 5%+", {price_min: 1, change_min: 5}, "change_pct"], ["Losers 5%+", {price_min: 1, change_max: -5}, "-change_pct"],
  ["Gap up 3%+", {price_min: 1, gap_min: 3}, "gap_pct"], ["Gap down 3%+", {price_min: 1, gap_max: -3}, "-gap_pct"],
  ["Unusual volume (2×)", {price_min: 1, rel_volume_min: 2}, "rel_volume"], ["Near day's high", {price_min: 1, change_min: 1, near_high_pct: 0.5}, "change_pct"],
  ["Under $10, up 5%+", {price_min: 1, price_max: 10, change_min: 5}, "change_pct"],
];
const FILTERS = [["price_min", "Price at least ($)"], ["price_max", "Price at most ($)"], ["change_min", "Change at least (%)"], ["change_max", "Change at most (%)"],
  ["volume_min", "Volume at least"], ["dollar_volume_min", "Dollar volume at least ($)"], ["gap_min", "Gap at least (%)"], ["gap_max", "Gap at most (%)"],
  ["rel_volume_min", "Volume vs yesterday at least (×)"], ["near_high_pct", "Within % of day's high"]];
function renderScanner() {
  const root = document.getElementById("view-scanner");
  if (!root.dataset.built) {
    root.dataset.built = "1";
    root.append(
      el("section", {class: "card"},
        el("div", {class: "row", style: "justify-content:space-between"}, el("h2", {style: "margin:0"}, "Top movers today"),
          el("div", {class: "seg", role: "group", id: "moversSeg"}, [["stocks", "Stocks"], ["crypto", "Crypto"]].map(([v, l]) => el("button", {type: "button", "data-m": v, "aria-pressed": v === MK.moversMarket, onclick: () => { MK.moversMarket = v; loadMovers(); }}, l)))),
        el("div", {class: "two-eq", id: "moversBox", style: "margin-top:12px"})),
      el("section", {class: "card"}, el("h2", {}, "Most active stocks"), el("div", {id: "activesBox", class: "scroll"})),
      el("section", {class: "card"},
        el("h2", {}, "Custom scan"),
        el("p", {class: "hint"}, "Checks every listed symbol in the chosen list against your filters, using today's prices. The first scan takes 10–30 seconds; repeats within a minute are instant. On the free IEX data feed, volume counts only IEX trades (a small share of the market), so volume filters run low."),
        el("div", {class: "row", style: "margin-bottom:10px"},
          el("div", {class: "seg", role: "group", id: "scanKind"}, [["stock", "Stocks"], ["etf", "ETFs"], ["crypto", "Crypto"]].map(([v, l]) => el("button", {type: "button", "data-k": v, "aria-pressed": v === MK.scanKind, onclick: (e) => { MK.scanKind = v; document.querySelectorAll("#scanKind button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.k === v)); }}, l)))),
        el("div", {class: "chips"}, PRESETS.map(([name, filters, sort]) => el("button", {type: "button", class: "chip", onclick: () => { setFilters(filters, sort); runScan(); }}, name))),
        el("form", {id: "scanForm", class: "form", onsubmit: (e) => { e.preventDefault(); runScan(); }},
          el("div", {class: "form-grid"}, FILTERS.map(([k, label]) => el("label", {}, label, el("input", {type: "number", name: k, step: "any"}))),
            el("label", {}, "Sort by", el("select", {name: "sort"}, [["change_pct", "Biggest gain"], ["-change_pct", "Biggest loss"], ["volume", "Volume"], ["gap_pct", "Gap up"], ["-gap_pct", "Gap down"], ["rel_volume", "Volume vs yesterday"], ["price", "Price (high first)"], ["-price", "Price (low first)"]].map(([v, l]) => el("option", {value: v}, l))))),
          el("div", {class: "row"}, el("button", {class: "btn primary", type: "submit"}, "Run scan"), el("button", {class: "btn", type: "button", onclick: () => setFilters({}, "change_pct")}, "Clear filters"), el("span", {id: "scanStatus", class: "hint", style: "margin:0"}))),
        el("div", {id: "scanResults", class: "scroll", style: "margin-top:12px"})));
  }
  loadMovers(); loadActives();
}
function setFilters(filters, sort) {
  const form = document.getElementById("scanForm");
  for (const [k] of FILTERS) form.elements[k].value = filters[k] ?? "";
  form.elements.sort.value = sort;
}
const quoteTable = (rows, cols) => rows.length ? el("table", {class: "click"},
  el("thead", {}, el("tr", {}, cols.map((c) => el("th", {class: c[2] ? "num" : ""}, c[0])))),
  el("tbody", {}, rows.map((r) => el("tr", {tabindex: 0, onclick: () => go(r.symbol), onkeydown: (e) => e.key === "Enter" && go(r.symbol)}, cols.map((c) => el("td", {class: c[2] ? "num" : ""}, c[1](r))))))) : el("div", {class: "empty"}, "Nothing to show right now.");
async function loadMovers() {
  document.querySelectorAll("#moversSeg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.m === MK.moversMarket));
  const box = document.getElementById("moversBox"); box.replaceChildren(loading());
  try {
    const m = await api("/api/market/movers?market=" + MK.moversMarket);
    const cols = [["Symbol", (r) => el("b", {}, r.symbol)], ["Price", (r) => fmtPrice(r.price), 1], ["Change", (r) => chg({change_pct: r.percent_change}), 1]];
    box.replaceChildren(el("div", {}, el("h3", {}, "Gainers"), quoteTable(m.gainers, cols)), el("div", {}, el("h3", {}, "Losers"), quoteTable(m.losers, cols)));
  } catch (e) { box.replaceChildren(el("div", {class: "empty"}, e.message)); }
}
async function loadActives() {
  const box = document.getElementById("activesBox"); box.replaceChildren(loading());
  try {
    const a = await api("/api/market/actives");
    box.replaceChildren(quoteTable(a.rows, [["Symbol", (r) => el("b", {}, r.symbol)], ["Volume", (r) => fmtBig(r.volume), 1], ["Trades", (r) => fmtBig(r.trade_count), 1]]));
  } catch (e) { box.replaceChildren(el("div", {class: "empty"}, e.message)); }
}
async function runScan() {
  const form = document.getElementById("scanForm"), status = document.getElementById("scanStatus"), box = document.getElementById("scanResults");
  const filters = {};
  for (const [k] of FILTERS) if (form.elements[k].value !== "") filters[k] = form.elements[k].value;
  status.textContent = "Scanning…"; box.replaceChildren(loading("Checking every symbol…"));
  try {
    const r = await api("/api/market/scan", {kind: MK.scanKind, filters, sort: form.elements.sort.value});
    status.textContent = `${r.matched.toLocaleString()} of ${r.scanned.toLocaleString()} matched` + (r.matched > r.rows.length ? ` (showing ${r.rows.length})` : "");
    box.replaceChildren(quoteTable(r.rows, [["Symbol", (x) => el("b", {}, x.symbol)], ["Name", (x) => el("span", {class: "name"}, x.name)], ["Price", (x) => fmtPrice(x.price), 1],
      ["Change", (x) => chg(x), 1], ["Gap", (x) => x.gap_pct === null ? "–" : pct(x.gap_pct), 1], ["Volume", (x) => fmtBig(x.volume), 1], ["Vol vs yesterday", (x) => x.rel_volume ? fmtNum(x.rel_volume, 1) + "×" : "–", 1]]));
  } catch (e) { status.textContent = ""; box.replaceChildren(el("div", {class: "empty"}, e.message)); }
}

// ---------- orders page ----------
async function renderOrdersView() {
  const root = document.getElementById("view-orders");
  if (!root.dataset.built) {
    root.dataset.built = "1";
    root.append(el("section", {class: "card"}, el("div", {class: "row", style: "justify-content:space-between"}, el("h2", {style: "margin:0"}, "Orders"),
      el("button", {class: "btn small", type: "button", onclick: renderOrdersView}, "Refresh")),
      el("p", {class: "hint"}, "Your most recent 60 orders, including the take-profit and stop-loss parts of bracket and OCO orders."),
      el("div", {id: "ordersBox", class: "scroll"})));
  }
  const box = document.getElementById("ordersBox"); box.replaceChildren(loading());
  try {
    const rows = (await api("/api/orders/recent")).rows;
    const openish = (s) => ["new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding", "pending_replace"].includes(s);
    const priceText = (o) => [o.limit_price && "limit " + fmtPrice(+o.limit_price), o.stop_price && "stop " + fmtPrice(+o.stop_price), o.trail_percent && "trail " + o.trail_percent + "%"].filter(Boolean).join(", ") || "market";
    box.replaceChildren(rows.length ? el("table", {},
      el("thead", {}, el("tr", {}, ["Submitted", "Order", "Type", "Price", "Status", "Filled", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.flatMap((o) => [el("tr", {},
        el("td", {}, o.submitted_at ? new Date(o.submitted_at).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : ""),
        el("td", {}, el("a", {href: "#s/" + enc(o.symbol)}, o.side.toUpperCase() + " " + (o.qty || money(+o.notional)) + " " + o.symbol)),
        el("td", {}, o.type.replace("_", " ") + (o.order_class && o.order_class !== "simple" ? " · " + o.order_class.toUpperCase() : "") + " · " + (o.time_in_force || "").toUpperCase()),
        el("td", {}, priceText(o)), el("td", {}, o.status.replace("_", " ")),
        el("td", {}, +o.filled_qty ? o.filled_qty + " @ " + fmtPrice(+o.filled_avg_price) : "–"),
        el("td", {}, openish(o.status) ? el("button", {class: "btn small", type: "button", onclick: () => confirm("Cancel this order?") && act("/api/cancel-order", {id: o.id}, "Cancel requested.").then(renderOrdersView)}, "Cancel") : "")),
        ...o.legs.map((l) => el("tr", {class: "leg"}, el("td", {}), el("td", {}, "↳ " + l.side.toUpperCase() + " leg"), el("td", {}, l.type.replace("_", " ")), el("td", {}, priceText(l)), el("td", {}, l.status.replace("_", " ")), el("td", {}), el("td", {})))]))) : el("div", {class: "empty"}, "No orders yet."));
  } catch (e) { box.replaceChildren(el("div", {class: "empty"}, e.message)); }
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (MK.view === "symbol" && MK.bars) drawCharts(); });
setupSearch();
route();
