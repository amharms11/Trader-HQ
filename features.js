// Trader HQ extras: watchlists, alerts, recurring buys, crypto bot, Ask Claude, events, reports, backtests.
// Uses helpers from dashboard.html and market.js (el, api, toast, money, pct, fmtPrice, fmtBig, chg, go, enc, MK, ...).
"use strict";
const FX = window.FX = {};
// The dashboard keeps its latest state in a top-level variable S (not on window), shared by name across scripts.
const ST = () => (typeof S !== "undefined" ? S : null);
const WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
const fmtDate = (t) => new Date(t * 1000).toLocaleDateString([], {month: "short", day: "numeric", year: "numeric"});
const pctOrDash = (x) => x === null || x === undefined ? "–" : pct(x);
const tone = (x) => x > 0 ? "up" : x < 0 ? "down" : "";
const lineKey = (color) => el("span", {style: `display:inline-block;width:14px;height:2px;margin-right:6px;vertical-align:middle;background:${color}`});
async function post(path, body, okText) {
  try { const r = await api(path, body); if (okText) toast(okText); return r; }
  catch (e) { toast(e.message, 7000); if (e.needLive) location.hash = "#bot"; throw e; }
}
function richText(text) {
  // Turn Claude's **bold** and line breaks into safe DOM (never innerHTML).
  return (text || "").split(/\n{2,}/).map((para) => el("p", {}, ...para.split(/(\*\*[^*]+\*\*)/).map((part) =>
    /^\*\*[^*]+\*\*$/.test(part) ? el("b", {}, part.slice(2, -2)) : part.replace(/^#+\s*/gm, ""))));
}

// ---------- Bot page: watchlists and crypto bot ----------
let WL = null;
async function loadWatchlists() { WL = await api("/api/watchlists"); return WL; }
async function renderWatchCard() {
  const box = document.getElementById("watchCard"); if (!box || MK.view !== "bot") return;
  try { await loadWatchlists(); } catch (e) { box.replaceChildren(el("h2", {}, "Watchlists"), el("div", {class: "empty"}, e.message)); return; }
  const sel = box.dataset.list ? +box.dataset.list : WL.lists[0].id;
  const list = WL.lists.find((l) => l.id === sel) || WL.lists[0];
  box.dataset.list = list.id;
  const rows = list.symbols.map((s) => ({symbol: s, ...(WL.quotes[s] || {})}));
  box.replaceChildren(
    el("div", {class: "row", style: "justify-content:space-between"},
      el("h2", {style: "margin:0"}, "Watchlists"),
      el("div", {class: "row"},
        el("select", {"aria-label": "Watchlist", onchange: (e) => { box.dataset.list = e.target.value; renderWatchCard(); }}, WL.lists.map((l) => el("option", {value: l.id, selected: l.id === list.id}, `${l.name} (${l.symbols.length})`))),
        el("button", {class: "btn small", type: "button", onclick: async () => { const n = prompt("Name for the new watchlist:"); if (n) { const r = await post("/api/watchlists", {action: "create", name: n}); box.dataset.list = r.id; renderWatchCard(); } }}, "New list"),
        WL.lists.length > 1 ? el("button", {class: "btn small danger", type: "button", onclick: async () => { if (confirm(`Delete the list "${list.name}"?`)) { await post("/api/watchlists", {action: "delete", id: list.id}); box.dataset.list = ""; renderWatchCard(); } }}, "Delete list") : null)),
    el("form", {class: "row", style: "margin:10px 0", onsubmit: async (e) => {
      e.preventDefault(); const q = e.target.sym.value.trim(); if (!q) return;
      let sym = q.toUpperCase();
      try { const r = (await api("/api/market/search?q=" + enc(q))).rows; if (r.length) sym = r[0].symbol; } catch (_) {}
      try { await post("/api/watchlists", {action: "add", id: list.id, symbol: sym}, `Added ${sym}.`); e.target.reset(); renderWatchCard(); } catch (_) {}
    }}, el("input", {type: "search", name: "sym", placeholder: "Add a symbol (e.g. AAPL, BTC)", style: "max-width:240px"}), el("button", {class: "btn small", type: "submit"}, "Add")),
    rows.length ? quoteTable(rows, [["Symbol", (r) => el("b", {}, r.symbol)], ["Price", (r) => fmtPrice(r.price), 1], ["Change", (r) => chg(r), 1],
      ["", (r) => el("button", {class: "btn small", type: "button", title: "Remove", onclick: async (e) => { e.stopPropagation(); await post("/api/watchlists", {action: "remove", id: list.id, symbol: r.symbol}); renderWatchCard(); }}, "✕")]])
      : el("div", {class: "empty"}, "Empty. Add symbols here or with the ☆ Watch button on any stock page."),
    list.symbols.length ? el("div", {class: "row", style: "margin-top:10px"}, el("span", {class: "hint", style: "margin:0"}, "Let a bot watch this list:"),
      [["long_term", "Long-term"], ["day", "Day trading"], ["options", "Options"], ["crypto", "Crypto"]].map(([t, l]) =>
        el("button", {class: "btn small", type: "button", onclick: () => post("/api/watchlists", {action: "use", id: list.id, target: t}, `The ${l.toLowerCase()} bot will watch this list.`)}, l))) : null);
}
function cryptoStatus() {
  if (!ST() || !S.crypto) return;
  const c = S.crypto, start = document.getElementById("cryptoStart"), stop = document.getElementById("cryptoStop"), text = document.getElementById("cryptoText");
  if (!start) return;
  start.hidden = c.running; stop.hidden = !c.running;
  const cy = S.crypto_day || {};
  document.getElementById("cdayStart").hidden = cy.running; document.getElementById("cdayStop").hidden = !cy.running;
  if (c.running) {
    const secs = c.next_check ? Math.max(0, Math.round(c.next_check - Date.now() / 1000)) : null;
    text.textContent = "Running 24/7 while Trader HQ is open. " + (secs !== null ? `Next check in ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}.` : "Checking now.");
  } else text.textContent = "Claude watches your crypto list and suggests small buys with a stop-loss, or sells.";
}
let lastNote = null;
function watchNotifications() {
  if (!ST() || !S.notifications || !S.notifications.length) return;
  const top = S.notifications[0];
  if (lastNote === null) { lastNote = top.id; return; }
  if (top.id > lastNote) {
    for (const n of S.notifications.filter((x) => x.id > lastNote).reverse()) toast(`${n.title}: ${n.body}`, 8000);
    lastNote = top.id;
  }
}

// ---------- symbol page extras ----------
FX.symbolExtras = async function () {
  const info = MK.info, sym = info.asset.symbol, crypto = info.asset.kind === "crypto";
  const price = (info.quote || {}).price;
  // star
  const star = document.getElementById("starBox");
  const drawStar = async () => {
    try { await loadWatchlists(); } catch (_) { return; }
    const first = WL.lists[0], on = WL.lists.some((l) => l.symbols.includes(sym));
    star.replaceChildren(el("button", {class: "btn small", type: "button", "aria-pressed": on, onclick: async () => {
      const lst = WL.lists.find((l) => l.symbols.includes(sym)) || first;
      await post("/api/watchlists", {action: on ? "remove" : "add", id: lst.id, symbol: sym}, on ? `Removed from ${lst.name}.` : `Added to ${lst.name}.`); drawStar();
    }}, on ? "★ Watching" : "☆ Watch"));
  };
  drawStar();
  // Ask Claude
  const ex = document.getElementById("explainCard");
  const out = el("div", {class: "explain"});
  ex.replaceChildren(el("div", {class: "row", style: "justify-content:space-between"}, el("h2", {style: "margin:0"}, "Ask Claude about " + sym),
    el("button", {class: "btn primary small", type: "button", onclick: async (e) => {
      e.target.disabled = true; out.replaceChildren(loading("Claude is reading the chart, numbers and headlines (up to a minute)…"));
      try { const r = await api("/api/market/explain", {symbol: sym}); out.replaceChildren(...richText(r.text), el("p", {class: "hint"}, "Written by Claude from the data on this page. Explanation only, not advice. It never places trades.")); }
      catch (err) { out.replaceChildren(el("p", {class: "msg err"}, err.message)); }
      e.target.disabled = false;
    }}, "Explain this " + (crypto ? "coin" : info.asset.kind === "etf" ? "ETF" : "stock"))),
    el("p", {class: "hint"}, "A plain-English summary of what it is, how it's been doing, what the numbers mean and the main risks."), out);
  // side cards
  const box = document.getElementById("symExtras");
  const events = el("section", {class: "card"}, el("h2", {}, "Upcoming events"), loading());
  const alertForm = el("form", {class: "form", onsubmit: async (e) => {
    e.preventDefault(); const f = e.target;
    try { await post("/api/alerts", {symbol: sym, kind: f.kind.value, value: f.value.value, note: f.note.value}, "Alert set. You'll get a Mac notification."); f.note.value = ""; } catch (_) {}
  }},
    el("label", {}, "Notify me when", el("select", {name: "kind", onchange: (e) => { const v = e.target.form.value; v.value = e.target.value === "move" ? "5" : price ? (price * (e.target.value === "above" ? 1.05 : 0.95)).toFixed(2) : ""; }},
      el("option", {value: "above"}, "Price rises above ($)"), el("option", {value: "below"}, "Price falls below ($)"), el("option", {value: "move"}, "It moves more than (% today)"))),
    el("input", {type: "number", name: "value", step: "any", min: 0, value: price ? (price * 1.05).toFixed(2) : "", "aria-label": "Alert value"}),
    el("input", {type: "text", name: "note", placeholder: "Note (optional)", maxlength: 120, "aria-label": "Note"}),
    el("button", {class: "btn small", type: "submit"}, "Set alert"));
  const recForm = el("form", {class: "form", onsubmit: async (e) => {
    e.preventDefault(); const f = e.target;
    try { await post("/api/recurring", {action: "add", symbol: sym, amount: f.amount.value, frequency: f.frequency.value, day: f.day.value}, "Recurring buy set up. It runs while Trader HQ is open."); } catch (_) {}
  }},
    el("label", {}, "Amount ($)", el("input", {type: "number", name: "amount", min: 1, step: "any", value: 50})),
    el("label", {}, "How often", el("select", {name: "frequency", onchange: (e) => fillDays(e.target.form)}, el("option", {value: "weekly"}, "Every week"), el("option", {value: "biweekly"}, "Every two weeks"), el("option", {value: "monthly"}, "Every month"))),
    el("label", {}, "On", el("select", {name: "day"})),
    el("p", {class: "hint"}, crypto || info.asset.fractionable ? "Buys the exact dollar amount (fractional shares)." : "This stock can't be bought in fractions, so it buys as many whole shares as the amount allows."),
    el("button", {class: "btn small", type: "submit"}, "Set up recurring buy"));
  fillDays(recForm);
  box.replaceChildren(events, el("section", {class: "card"}, el("h2", {}, "Price alert"), alertForm), el("section", {class: "card"}, el("h2", {}, "Recurring buy"), recForm));
  try {
    const ev = await api("/api/market/events?symbol=" + enc(sym));
    const today = new Date().toISOString().slice(0, 10);
    const rows = [];
    if (ev.earnings === null && !crypto) rows.push(el("p", {class: "hint"}, "Add a free Finnhub key in Settings to see earnings dates."));
    else if (ev.earnings && ev.earnings.length) rows.push(el("p", {}, el("b", {}, "Earnings report: "), ev.earnings[0].date + (ev.earnings[0].hour === "bmo" ? " (before the open)" : ev.earnings[0].hour === "amc" ? " (after the close)" : "")));
    else if (!crypto) rows.push(el("p", {}, "No earnings report in the next 3 months."));
    const divs = ev.dividends || [], next = divs.find((d) => d.ex_date >= today), past = divs.filter((d) => d.ex_date < today);
    if (next) rows.push(el("p", {}, el("b", {}, "Next dividend: "), `${money(next.rate)} per share. Own it before ${next.ex_date}, paid ${next.payable_date || "later"}.`));
    if (past.length && price) { const yr = past.slice(-4).reduce((a, d) => a + d.rate, 0); rows.push(el("p", {}, el("b", {}, "Dividends, last 12 months: "), `${money(yr)} per share (about ${(yr / price * 100).toFixed(2)}% yield).`)); }
    if (!crypto && !divs.length) rows.push(el("p", {class: "hint"}, "No dividends in the last year."));
    if (crypto) rows.push(el("p", {class: "hint"}, "Crypto has no earnings or dividends."));
    events.replaceChildren(el("h2", {}, "Upcoming events"), ...rows);
  } catch (e) { events.replaceChildren(el("h2", {}, "Upcoming events"), el("p", {class: "hint"}, e.message)); }
};
function fillDays(form) {
  const monthly = form.frequency.value === "monthly";
  form.day.replaceChildren(...(monthly ? Array.from({length: 28}, (_, i) => el("option", {value: i + 1}, "Day " + (i + 1))) : WEEKDAY_NAMES.map((d, i) => el("option", {value: i}, d))));
}

// ---------- Reports ----------
let REP = {period: "3M"};
FX.renderReports = function () {
  const root = document.getElementById("view-reports");
  if (!root.dataset.built) {
    root.dataset.built = "1";
    const year = new Date().getFullYear();
    root.append(
      el("section", {class: "card"},
        el("div", {class: "row", style: "justify-content:space-between"}, el("h2", {style: "margin:0"}, "Is the bot working? Performance vs. the S&P 500"),
          el("div", {class: "seg", role: "group", id: "perfSeg"}, ["1M", "3M", "1Y", "ALL"].map((p) => el("button", {type: "button", "data-p": p, "aria-pressed": p === REP.period, onclick: () => { REP.period = p; loadPerformance(); }}, p === "ALL" ? "All" : p)))),
        el("p", {class: "hint"}, "Your account's % change compared with simply holding SPY (an S&P 500 fund) over the same time. Closed trades are matched first-in, first-out from your Alpaca order history and grouped by what placed them."),
        el("div", {id: "perfBody"})),
      el("section", {class: "card"},
        el("div", {class: "row", style: "justify-content:space-between"}, el("h2", {style: "margin:0"}, "Daily summaries"),
          el("button", {class: "btn small", type: "button", onclick: async () => { await post("/api/reports/summary-now", {}, "Summary created."); loadSummaries(); }}, "Make today's summary now")),
        el("p", {class: "hint"}, "Created automatically after the market closes on weekdays while Trader HQ is open, with a Mac notification."),
        el("div", {id: "sumBody"})),
      el("section", {class: "card"},
        el("div", {class: "row", style: "justify-content:space-between"}, el("h2", {style: "margin:0"}, "Tax report: realized gains and losses"),
          el("div", {class: "row"}, el("select", {id: "taxYear", onchange: loadTax}, [year, year - 1, year - 2].map((y) => el("option", {value: y}, y))),
            el("a", {class: "btn small", id: "taxCsv", href: "#", download: ""}, "Download CSV"))),
        el("p", {class: "hint"}, "An estimate from your Alpaca orders (first-in, first-out). Held over a year is long-term. Paper trades aren't taxable, and your broker's 1099 is the official record."),
        el("div", {id: "taxBody"})));
  }
  loadPerformance(); loadSummaries(); loadTax();
};
async function loadPerformance() {
  document.querySelectorAll("#perfSeg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.p === REP.period));
  const body = document.getElementById("perfBody"); body.replaceChildren(loading("Reading your account history…"));
  let r;
  try { r = await api("/api/reports/performance?period=" + REP.period); } catch (e) { body.replaceChildren(el("div", {class: "empty"}, e.message)); return; }
  const o = r.overall, beat = r.account_return !== null && r.spy_return !== null ? r.account_return - r.spy_return : null;
  const stat = (k, v, cls) => el("div", {class: "stat"}, el("span", {}, k), el("b", {class: cls || ""}, v));
  const chartBox = el("div", {class: "perf-chart"});
  body.replaceChildren(
    el("div", {class: "stats", style: "margin-top:8px"},
      stat("Your account", pctOrDash(r.account_return), tone(r.account_return)), stat("Holding SPY", pctOrDash(r.spy_return), tone(r.spy_return)),
      stat(beat === null ? "Difference" : beat >= 0 ? "You beat SPY by" : "You trailed SPY by", beat === null ? "–" : Math.abs(beat).toFixed(2) + " pts", tone(beat)),
      stat("Worst drop (max drawdown)", r.max_drawdown === null ? "–" : r.max_drawdown.toFixed(1) + "%", r.max_drawdown < 0 ? "down" : ""),
      stat("Closed trades", String(o.trades)), stat("Win rate", o.win_rate === null ? "–" : o.win_rate.toFixed(0) + "%"),
      stat("Average win / loss", (o.avg_win === null ? "–" : money(o.avg_win)) + " / " + (o.avg_loss === null ? "–" : money(o.avg_loss))),
      stat("Worst losing streak", o.worst_losing_streak + " in a row")),
    el("div", {class: "row legend"}, el("span", {}, lineKey(cmpColor(0)), "Your account"), el("span", {}, lineKey(cmpColor(1)), "Holding SPY")),
    chartBox,
    r.by_strategy.length ? el("div", {class: "scroll"}, el("h3", {}, "By strategy"), el("table", {},
      el("thead", {}, el("tr", {}, ["Strategy", "Trades", "Win rate", "Profit / loss", "Avg win", "Avg loss", "Profit factor", "Avg days held"].map((h, i) => el("th", {class: i ? "num" : ""}, h)))),
      el("tbody", {}, r.by_strategy.map((s) => el("tr", {}, el("td", {}, s.strategy), el("td", {class: "num"}, s.trades), el("td", {class: "num"}, s.win_rate === null ? "–" : s.win_rate.toFixed(0) + "%"),
        el("td", {class: "num " + tone(s.total_pnl)}, signed(s.total_pnl)), el("td", {class: "num"}, s.avg_win === null ? "–" : money(s.avg_win)), el("td", {class: "num"}, s.avg_loss === null ? "–" : money(s.avg_loss)),
        el("td", {class: "num"}, s.profit_factor === null ? "–" : s.profit_factor.toFixed(2)), el("td", {class: "num"}, s.avg_days_held === null ? "–" : s.avg_days_held.toFixed(1)))))))
      : el("div", {class: "empty"}, "No closed trades in this period yet. Results appear here once positions are sold."),
    r.recent_trades.length ? el("div", {class: "scroll"}, el("h3", {}, "Recent closed trades"), el("table", {},
      el("thead", {}, el("tr", {}, ["Closed", "Symbol", "Strategy", "Qty", "Bought", "Sold", "Profit / loss"].map((h, i) => el("th", {class: i > 2 ? "num" : ""}, h)))),
      el("tbody", {}, r.recent_trades.map((t) => el("tr", {}, el("td", {}, t.closed.slice(0, 10)), el("td", {}, t.symbol), el("td", {}, t.strategy), el("td", {class: "num"}, +t.qty.toFixed(6)),
        el("td", {class: "num"}, fmtPrice(t.buy_price)), el("td", {class: "num"}, fmtPrice(t.sell_price)), el("td", {class: "num " + tone(t.pnl)}, signed(t.pnl))))))) : null,
    el("p", {class: "hint"}, "A rule of thumb: run on paper for at least a few months and 50+ closed trades. If the account doesn't beat simply holding SPY after that, the bot isn't adding value."));
  pctChart(chartBox, [{label: "Your account", color: cmpColor(0), points: r.account}, {label: "Holding SPY", color: cmpColor(1), points: r.spy}]);
}
function pctChart(box, sets, height) {
  if (!window.LightweightCharts || !sets.some((s) => s.points.length > 1)) { box.replaceChildren(el("div", {class: "empty"}, "Not enough history yet. Check back after a few trading days.")); return; }
  const c = LightweightCharts.createChart(box, chartOptions(height || 280, false));
  c.applyOptions({localization: {priceFormatter: (v) => (v >= 0 ? "+" : "") + v.toFixed(1) + "%"}});
  for (const s of sets) {
    if (s.points.length < 2) continue;
    const seen = new Set();
    const data = s.points.map((p) => ({time: localT(p.t), value: p.pct})).filter((p) => !seen.has(p.time) && seen.add(p.time));
    c.addLineSeries({color: s.color, lineWidth: 2, priceLineVisible: false, title: ""}).setData(data);
  }
  c.timeScale().fitContent();
  return c;
}
async function loadSummaries() {
  const body = document.getElementById("sumBody");
  try {
    const rows = (await api("/api/reports/summaries")).rows;
    body.replaceChildren(...(rows.length ? rows.map((s) => el("div", {class: "summary"}, el("b", {}, new Date(s.day + "T12:00").toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"})),
      el("span", {class: tone(s.change)}, ` ${signed(s.change)} (${pct(s.change_pct)})`), el("p", {}, s.text),
      s.fills.length ? el("p", {class: "hint"}, "Trades: " + s.fills.map((f) => `${f.side} ${+f.qty.toFixed(6)} ${f.symbol} @ ${fmtPrice(f.price)} (${f.strategy})`).join("; ")) : null))
      : [el("div", {class: "empty"}, "No summaries yet.")]));
  } catch (e) { body.replaceChildren(el("div", {class: "empty"}, e.message)); }
}
async function loadTax() {
  const year = document.getElementById("taxYear").value, body = document.getElementById("taxBody");
  document.getElementById("taxCsv").href = `/api/reports/tax?year=${year}&format=csv`;
  body.replaceChildren(loading());
  try {
    const r = await api("/api/reports/tax?year=" + year), t = r.totals;
    body.replaceChildren(
      el("div", {class: "stats"}, ...["Short-term", "Long-term"].flatMap((k) => [
        el("div", {class: "stat"}, el("span", {}, k + " gain / loss"), el("b", {class: tone(t[k].gain)}, signed(t[k].gain))),
        el("div", {class: "stat"}, el("span", {}, k + " sales"), el("b", {}, t[k].count + " (proceeds " + money(t[k].proceeds) + ")"))])),
      r.rows.length ? el("div", {class: "scroll", style: "max-height:360px;margin-top:10px"}, el("table", {},
        el("thead", {}, el("tr", {}, ["Symbol", "Qty", "Acquired", "Sold", "Proceeds", "Cost", "Gain / loss", "Term"].map((h, i) => el("th", {class: i > 3 && i < 7 ? "num" : ""}, h)))),
        el("tbody", {}, r.rows.slice(-300).reverse().map((x) => el("tr", {}, el("td", {}, x.symbol), el("td", {}, +x.qty.toFixed(6)), el("td", {}, x.opened.slice(0, 10)), el("td", {}, x.closed.slice(0, 10)),
          el("td", {class: "num"}, money(x.proceeds)), el("td", {class: "num"}, money(x.cost)), el("td", {class: "num " + tone(x.pnl)}, signed(x.pnl)), el("td", {}, x.term))))))
        : el("div", {class: "empty"}, "No sales in " + year + "."));
  } catch (e) { body.replaceChildren(el("div", {class: "empty"}, e.message)); }
}

// ---------- Tools ----------
FX.renderTools = function () {
  const root = document.getElementById("view-tools");
  if (!root.dataset.built) {
    root.dataset.built = "1";
    const symInput = (name) => el("input", {type: "text", name, placeholder: "Symbol, e.g. AAPL or BTC/USD", required: true, autocomplete: "off"});
    const resolve = async (q) => { try { const r = (await api("/api/market/search?q=" + enc(q))).rows; if (r.length) return r[0].symbol; } catch (_) {} return q.toUpperCase(); };
    root.append(
      el("div", {class: "two-eq"},
        el("section", {class: "card"}, el("h2", {}, "Price alerts"),
          el("p", {class: "hint"}, "Checked every 20 seconds while Trader HQ is open. You get a Mac notification once, then the alert switches off."),
          el("form", {class: "form-grid", onsubmit: async (e) => { e.preventDefault(); const f = e.target;
            try { await post("/api/alerts", {symbol: await resolve(f.sym.value), kind: f.kind.value, value: f.value.value, note: f.note.value}, "Alert set."); f.reset(); loadAlerts(); } catch (_) {} }},
            el("label", {}, "Symbol", symInput("sym")),
            el("label", {}, "When", el("select", {name: "kind"}, el("option", {value: "above"}, "Price rises above ($)"), el("option", {value: "below"}, "Price falls below ($)"), el("option", {value: "move"}, "Moves more than (% today)"))),
            el("label", {}, "Value", el("input", {type: "number", name: "value", step: "any", min: 0, required: true})),
            el("label", {}, "Note (optional)", el("input", {type: "text", name: "note", maxlength: 120})),
            el("div", {style: "align-self:end"}, el("button", {class: "btn primary", type: "submit"}, "Add alert"))),
          el("div", {id: "alertList", class: "scroll", style: "margin-top:10px"})),
        el("section", {class: "card"}, el("h2", {}, "Recurring buys"),
          el("p", {class: "hint"}, "Buys a fixed dollar amount on a schedule (dollar-cost averaging). Stocks buy during market hours on or after the scheduled day; crypto any time. Runs while Trader HQ is open, within your cash and order limits."),
          el("form", {class: "form-grid", onsubmit: async (e) => { e.preventDefault(); const f = e.target;
            try { await post("/api/recurring", {action: "add", symbol: await resolve(f.sym.value), amount: f.amount.value, frequency: f.frequency.value, day: f.day.value}, "Recurring buy set up."); loadRecurring(); } catch (_) {} }},
            el("label", {}, "Symbol", symInput("sym")),
            el("label", {}, "Amount ($)", el("input", {type: "number", name: "amount", min: 1, step: "any", value: 50, required: true})),
            el("label", {}, "How often", el("select", {name: "frequency", onchange: (e) => fillDays(e.target.form)}, el("option", {value: "weekly"}, "Every week"), el("option", {value: "biweekly"}, "Every two weeks"), el("option", {value: "monthly"}, "Every month"))),
            el("label", {}, "On", el("select", {name: "day"})),
            el("div", {style: "align-self:end"}, el("button", {class: "btn primary", type: "submit"}, "Add"))),
          el("div", {id: "recList", class: "scroll", style: "margin-top:10px"}))),
      el("section", {class: "card"}, el("h2", {}, "Backtest a strategy"),
        el("p", {class: "hint"}, "See how simple trading rules would have done on past daily prices, compared with buying and holding. Trades happen at the next day's open after a signal, with no fees, taxes or slippage, so real results are usually worse. Claude's suggestions can't be backtested because they aren't fixed rules."),
        el("form", {id: "btForm", class: "form", onsubmit: (e) => { e.preventDefault(); runBacktest(e.target); }},
          el("div", {class: "form-grid"},
            el("label", {}, "Symbol", el("input", {type: "text", name: "sym", value: "SPY", required: true, autocomplete: "off"})),
            el("label", {}, "Period", el("select", {name: "range"}, el("option", {value: "1Y"}, "1 year"), el("option", {value: "BT5Y", selected: true}, "5 years"), el("option", {value: "BTALL"}, "All available"))),
            el("label", {}, "Strategy", el("select", {name: "strategy", onchange: (e) => btParams(e.target.form)},
              el("option", {value: "sma"}, "Moving-average crossover"), el("option", {value: "rsi"}, "RSI buy-the-dip"), el("option", {value: "breakout"}, "Breakout (new highs)"), el("option", {value: "dca"}, "Monthly recurring buy"))),
            el("label", {}, "Starting cash ($)", el("input", {type: "number", name: "cash", value: 10000, min: 100, step: "any"}))),
          el("div", {class: "form-grid", id: "btParams"}),
          el("p", {class: "hint", id: "btExplain"}),
          el("div", {}, el("button", {class: "btn primary", type: "submit"}, "Run backtest"))),
        el("div", {id: "btResult"})),
      el("div", {class: "two-eq"},
        el("section", {class: "card"}, el("h2", {}, "Earnings & dividend calendar"), el("p", {class: "hint"}, "For everything you own or watch."), el("div", {id: "calBody"})),
        el("section", {class: "card"}, el("h2", {}, "Automatic trailing stops"),
          el("p", {class: "hint"}, "Gives every stock you own a trailing stop: if the price falls this far below its highest point since you bought, it sells. Positions that already have sell orders (like a bracket or OCO) are left alone. Selling yourself or with the bot removes the stop first. Checked every 2 minutes while Trader HQ is open."),
          el("form", {class: "row", id: "protForm", onsubmit: async (e) => { e.preventDefault(); const v = e.target.pct.value || 0;
            try { await api("/api/settings", {settings: {market: {auto_trailing_stop_pct: v}}}); toast(+v ? `Trailing stops on at ${v}%.` : "Automatic trailing stops are off."); } catch (err) { toast(err.message, 7000); } }},
            el("label", {}, "Trail by (%), 0 = off", el("input", {type: "number", name: "pct", min: 0, max: 50, step: "any", style: "max-width:120px"})),
            el("button", {class: "btn", type: "submit", style: "align-self:end"}, "Save")))),
      el("section", {class: "card"}, el("h2", {}, "Notifications"), el("div", {id: "noteList", class: "log"})));
    fillDays(root.querySelectorAll("form.form-grid")[1]);  // the recurring-buy form
    btParams(document.getElementById("btForm"));
  }
  loadAlerts(); loadRecurring(); loadCalendar(); loadNotes();
  api("/api/settings").then((r) => { document.querySelector("#protForm input[name=pct]").value = r.settings.market.auto_trailing_stop_pct; }).catch(() => {});
};
async function loadAlerts() {
  const box = document.getElementById("alertList");
  try {
    const rows = (await api("/api/alerts")).rows;
    const desc = (a) => ({above: `price ≥ ${money(a.value)}`, below: `price ≤ ${money(a.value)}`, move: `moves ≥ ${a.value}% in a day`})[a.kind];
    box.replaceChildren(rows.length ? el("table", {}, el("tbody", {}, rows.map((a) => el("tr", {},
      el("td", {}, el("a", {href: "#s/" + enc(a.symbol)}, a.symbol)), el("td", {}, desc(a) + (a.note ? ` · ${a.note}` : "")),
      el("td", {}, a.active ? el("span", {class: "tag"}, "Watching") : el("span", {class: "muted"}, "Triggered " + (a.triggered ? new Date(a.triggered * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "") + (a.price ? " at " + fmtPrice(a.price) : ""))),
      el("td", {}, el("button", {class: "btn small", type: "button", onclick: async () => { await post("/api/alerts", {action: "delete", id: a.id}); loadAlerts(); }}, "Delete")))))) : el("div", {class: "empty"}, "No alerts yet."));
  } catch (e) { box.replaceChildren(el("div", {class: "empty"}, e.message)); }
}
async function loadRecurring() {
  const box = document.getElementById("recList");
  try {
    const r = await api("/api/recurring");
    box.replaceChildren(r.rows.length ? el("table", {}, el("tbody", {}, r.rows.map((x) => el("tr", {},
      el("td", {}, el("a", {href: "#s/" + enc(x.symbol)}, x.symbol)), el("td", {}, money(x.amount) + " · " + x.schedule + (x.mode !== r.mode ? ` (${x.mode} account)` : "")),
      el("td", {}, x.active ? "Next: " + x.next_run : el("span", {class: "muted"}, "Paused"), x.last_result ? el("div", {class: "hint", style: "margin:2px 0 0"}, "Last: " + x.last_run + " · " + x.last_result) : null),
      el("td", {}, el("div", {class: "row"},
        el("button", {class: "btn small", type: "button", onclick: async () => { await post("/api/recurring", {action: x.active ? "pause" : "resume", id: x.id}); loadRecurring(); }}, x.active ? "Pause" : "Resume"),
        el("button", {class: "btn small danger", type: "button", onclick: async () => { if (confirm("Delete this recurring buy?")) { await post("/api/recurring", {action: "delete", id: x.id}); loadRecurring(); } }}, "Delete"))))))) : el("div", {class: "empty"}, "No recurring buys yet."));
  } catch (e) { box.replaceChildren(el("div", {class: "empty"}, e.message)); }
}
async function loadCalendar() {
  const box = document.getElementById("calBody"); box.replaceChildren(loading());
  try {
    const r = await api("/api/calendar");
    const rows = r.rows;
    box.replaceChildren(
      !r.has_finnhub ? el("p", {class: "hint"}, "Earnings dates need a free Finnhub key (Settings). Dividends are shown without it.") : null,
      rows.length ? el("table", {}, el("thead", {}, el("tr", {}, ["Symbol", "Earnings", "Ex-dividend", "Dividend"].map((h) => el("th", {}, h)))),
        el("tbody", {}, rows.map((x) => el("tr", {}, el("td", {}, el("a", {href: "#s/" + enc(x.symbol)}, x.symbol)),
          el("td", {}, x.earnings ? x.earnings + (x.earnings_hour ? ` (${x.earnings_hour})` : "") : "–"), el("td", {}, x.ex_dividend || "–"), el("td", {}, x.dividend ? money(x.dividend) : "–")))))
        : el("div", {class: "empty"}, r.symbols ? "Nothing scheduled for your holdings and watchlists." : "Own or watch some stocks to see their dates here."));
  } catch (e) { box.replaceChildren(el("div", {class: "empty"}, e.message)); }
}
async function loadNotes() {
  const box = document.getElementById("noteList");
  try {
    const rows = (await api("/api/notifications")).rows;
    box.replaceChildren(...(rows.length ? rows.map((n) => el("div", {}, el("time", {}, new Date(n.time * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"})), el("b", {}, n.title + ": "), n.body)) : [el("div", {class: "empty"}, "Nothing yet.")]));
  } catch (e) { box.replaceChildren(el("div", {class: "empty"}, e.message)); }
}

// ---------- backtester ----------
const BT_PARAMS = {
  sma: [["fast", "Fast average (days)", 50], ["slow", "Slow average (days)", 200]],
  rsi: [["period", "RSI period (days)", 14], ["buy", "Buy when RSI falls below", 30], ["sell", "Sell when RSI rises above", 55]],
  breakout: [["entry", "Buy on a new high over (days)", 55], ["exit", "Sell on a new low over (days)", 20]],
  dca: [["amount", "Invest each month ($)", 500]],
};
const BT_TEXT = {
  sma: "Own it while the fast moving average is above the slow one (a classic trend-following rule, like the 50/200-day 'golden cross').",
  rsi: "Buy after a sharp drop (low RSI), sell once it bounces back (higher RSI).",
  breakout: "Buy when the price closes at its highest level in N days, sell when it closes at its lowest in M days.",
  dca: "Invest the same amount at the start of every month no matter the price, starting from nothing. Compared with investing the same total all at once.",
};
function btParams(form) {
  const s = form.strategy.value;
  document.getElementById("btParams").replaceChildren(...BT_PARAMS[s].map(([k, label, v]) => el("label", {}, label, el("input", {type: "number", name: k, value: v, min: 1, step: "any"}))));
  document.getElementById("btExplain").textContent = BT_TEXT[s];
  form.cash.closest("label").hidden = s === "dca";
}
function backtest(bars, strategy, p, startCash) {
  const closes = bars.map((b) => b.c), n = bars.length, want = Array(n).fill(0);
  if (strategy === "sma") { const f = sma(closes, p.fast), s = sma(closes, p.slow); for (let i = 0; i < n; i++) want[i] = f[i] !== null && s[i] !== null && f[i] > s[i] ? 1 : 0; }
  if (strategy === "rsi") { const r = rsi(closes, p.period); let pos = 0; for (let i = 0; i < n; i++) { if (r[i] !== null) { if (!pos && r[i] < p.buy) pos = 1; else if (pos && r[i] > p.sell) pos = 0; } want[i] = pos; } }
  if (strategy === "breakout") { let pos = 0; for (let i = 0; i < n; i++) {
    if (i >= p.entry && !pos && bars[i].c > Math.max(...bars.slice(i - p.entry, i).map((b) => b.h))) pos = 1;
    else if (i >= p.exit && pos && bars[i].c < Math.min(...bars.slice(i - p.exit, i).map((b) => b.l))) pos = 0;
    want[i] = pos; } }
  let cash = startCash, shares = 0, entry = null;
  const equity = [], trades = [];
  let invested = 0, contributed = 0, month = null;
  for (let i = 0; i < n; i++) {
    const b = bars[i];
    if (strategy === "dca") {
      const m = new Date(b.t * 1000).getUTCMonth();
      if (m !== month) { month = m; shares += p.amount / b.o; contributed += p.amount; }
      equity.push({t: b.t, v: shares * b.c, contributed});
      continue;
    }
    const target = i > 0 ? want[i - 1] : 0;  // act at today's open on yesterday's signal
    if (target && !shares) { shares = cash / b.o; cash = 0; entry = {t: b.t, p: b.o}; }
    else if (!target && shares) { cash = shares * b.o; trades.push({entry_t: entry.t, entry_p: entry.p, exit_t: b.t, exit_p: b.o, ret: (b.o / entry.p - 1) * 100}); shares = 0; entry = null; }
    if (shares) invested++;
    equity.push({t: b.t, v: cash + shares * b.c});
  }
  if (shares && entry) trades.push({entry_t: entry.t, entry_p: entry.p, exit_t: null, exit_p: bars[n - 1].c, ret: (bars[n - 1].c / entry.p - 1) * 100, open: true});
  return {equity, trades, exposure: strategy === "dca" ? 100 : invested / n * 100, contributed};
}
async function runBacktest(form) {
  const out = document.getElementById("btResult"); out.replaceChildren(loading("Loading price history…"));
  const q = form.sym.value.trim(); let sym = q.toUpperCase();
  try { const r = (await api("/api/market/search?q=" + enc(q))).rows; if (r.length) sym = r[0].symbol; } catch (_) {}
  let bars;
  try { bars = (await api(`/api/market/bars?symbol=${enc(sym)}&range=${form.range.value}`)).bars; } catch (e) { out.replaceChildren(el("div", {class: "empty"}, e.message)); return; }
  if (bars.length < 30) { out.replaceChildren(el("div", {class: "empty"}, "Not enough daily history for " + sym + ".")); return; }
  const strategy = form.strategy.value, p = {};
  for (const [k] of BT_PARAMS[strategy]) p[k] = +form.elements[k].value;
  const cash = +form.cash.value || 10000, res = backtest(bars, strategy, p, cash);
  const years = (bars[bars.length - 1].t - bars[0].t) / (365.25 * 86400);
  const first = bars[0], last = bars[bars.length - 1];
  const vals = res.equity.map((e) => e.v);
  let stratRet, bhRet, bhPoints;
  if (strategy === "dca") {
    stratRet = (vals[vals.length - 1] / res.contributed - 1) * 100;
    bhRet = (last.c / first.o - 1) * 100;
    bhPoints = bars.map((b) => ({t: b.t, pct: (b.c / first.o - 1) * 100}));
  } else {
    stratRet = (vals[vals.length - 1] / cash - 1) * 100;
    bhRet = (last.c / first.o - 1) * 100;
    bhPoints = bars.map((b) => ({t: b.t, pct: (b.c / first.o - 1) * 100}));
  }
  const cagr = (r) => years > 0.9 ? ((1 + r / 100) ** (1 / years) - 1) * 100 : null;
  const dd = (arr) => { let peak = -Infinity, w = 0; for (const v of arr) { peak = Math.max(peak, v); if (peak > 0) w = Math.min(w, v / peak - 1); } return w * 100; };
  const closed = res.trades.filter((t) => !t.open), wins = closed.filter((t) => t.ret > 0);
  const stat = (k, v, cls) => el("div", {class: "stat"}, el("span", {}, k), el("b", {class: cls || ""}, v));
  const stratPoints = strategy === "dca" ? res.equity.map((e) => ({t: e.t, pct: (e.v / e.contributed - 1) * 100})) : res.equity.map((e) => ({t: e.t, pct: (e.v / cash - 1) * 100}));
  const chartBox = el("div", {class: "perf-chart"});
  out.replaceChildren(
    el("h3", {}, `${sym}: ${fmtDate(first.t)} to ${fmtDate(last.t)} (${years.toFixed(1)} years)`),
    el("div", {class: "stats"},
      stat("Strategy return", pct(stratRet), tone(stratRet)), stat("Buy & hold return", pct(bhRet), tone(bhRet)),
      stat("Strategy per year", cagr(stratRet) === null ? "–" : pct(cagr(stratRet))), stat("Buy & hold per year", cagr(bhRet) === null ? "–" : pct(cagr(bhRet))),
      stat("Strategy worst drop", dd(vals).toFixed(1) + "%", "down"), stat("Buy & hold worst drop", dd(bars.map((b) => b.c)).toFixed(1) + "%", "down"),
      strategy === "dca" ? stat("Total invested", money(res.contributed)) : stat("Trades", String(res.trades.length)),
      strategy === "dca" ? stat("Ending value", money(vals[vals.length - 1])) : stat("Win rate", closed.length ? (wins.length / closed.length * 100).toFixed(0) + "%" : "–"),
      strategy === "dca" ? null : stat("Time invested", res.exposure.toFixed(0) + "%")),
    el("div", {class: "row legend"}, el("span", {}, lineKey(cmpColor(0)), "Strategy"), el("span", {}, lineKey(cmpColor(1)), "Buy & hold")),
    chartBox,
    strategy !== "dca" && res.trades.length ? el("details", {}, el("summary", {style: "cursor:pointer"}, `All ${res.trades.length} trades`),
      el("div", {class: "scroll", style: "max-height:300px"}, el("table", {}, el("thead", {}, el("tr", {}, ["Bought", "Price", "Sold", "Price", "Return"].map((h, i) => el("th", {class: i % 2 ? "num" : ""}, h)))),
        el("tbody", {}, res.trades.map((t) => el("tr", {}, el("td", {}, fmtDate(t.entry_t)), el("td", {class: "num"}, fmtPrice(t.entry_p)),
          el("td", {}, t.open ? "still open" : fmtDate(t.exit_t)), el("td", {class: "num"}, fmtPrice(t.exit_p)), el("td", {class: "num " + tone(t.ret)}, pct(t.ret)))))))) : null,
    el("p", {class: "hint"}, "Past results don't predict future ones. A rule that worked on one stock or period often fails on another, so try several."));
  pctChart(chartBox, [{label: "Strategy", color: cmpColor(0), points: stratPoints}, {label: "Buy & hold", color: cmpColor(1), points: bhPoints}], 300);
}

// ---------- wiring ----------
document.getElementById("cryptoStart").onclick = () => post("/api/crypto/start", {}, "Crypto bot started.").then(refresh).catch(() => {});
document.getElementById("cryptoStop").onclick = () => post("/api/crypto/stop", {}, "Crypto bot stopped.").then(refresh).catch(() => {});
document.getElementById("cdayStart").onclick = () => post("/api/crypto-day/start", {}, "Crypto day trading started.").then(refresh).catch(() => {});
document.getElementById("cdayStop").onclick = () => post("/api/crypto-day/stop", {}, "Crypto day trading stopped.").then(refresh).catch(() => {});
setInterval(() => { cryptoStatus(); watchNotifications(); }, 1000);
setInterval(() => { if (MK.view === "bot") renderWatchCard(); }, 15000);
window.addEventListener("hashchange", () => { if (MK.view === "bot") renderWatchCard(); });
renderWatchCard();

// ---------- Live view: watch the bot trade ----------
const LIVE = {symbol: "", chart: null, series: null, bars: [], lines: [], data: null, timers: false, barSec: 300};
FX.renderLive = function () {
  const root = document.getElementById("view-live");
  if (!root.dataset.built) {
    root.dataset.built = "1";
    root.append(
      el("section", {class: "card live-controls"},
        el("div", {class: "row", style: "justify-content:space-between"},
          el("h2", {style: "margin:0"}, "Live"),
          el("span", {id: "liveBusy", class: "pill"}, el("span", {class: "dot"}), el("span", {}, "Idle"))),
        el("div", {class: "live-bots"},
          el("div", {class: "live-bot"}, el("b", {}, "Day trading"), el("span", {id: "liveDayText", class: "hint", style: "margin:0"}),
            el("div", {id: "liveCfg-day", class: "bot-cfg"}),
            el("div", {class: "row"},
              el("button", {class: "btn primary", type: "button", id: "liveDayStart", onclick: () => post("/api/day/start", {}, "Day trading started.").then(liveTick).catch(() => {})}, "Start day trading"),
              el("button", {class: "btn danger", type: "button", id: "liveDayStop", hidden: true, onclick: () => post("/api/day/stop", {}, "Day trading stopped.").then(liveTick).catch(() => {})}, "Stop day trading"),
              el("button", {class: "btn", type: "button", onclick: () => confirm("Sell everything day trading bought today, at market price, now?") && post("/api/flatten", {}, "Closing day trades.").then(liveTick).catch(() => {})}, "Close day trades now"))),
          el("div", {class: "live-bot"}, el("b", {}, "Crypto bot (24/7)"), el("span", {id: "liveCryptoText", class: "hint", style: "margin:0"}),
            el("div", {id: "liveCfg-crypto", class: "bot-cfg"}),
            el("div", {class: "row"},
              el("button", {class: "btn primary", type: "button", id: "liveCryptoStart", onclick: () => post("/api/crypto/start", {}, "Crypto bot started.").then(liveTick).catch(() => {})}, "Start crypto bot"),
              el("button", {class: "btn danger", type: "button", id: "liveCryptoStop", hidden: true, onclick: () => post("/api/crypto/stop", {}, "Crypto bot stopped.").then(liveTick).catch(() => {})}, "Stop crypto bot"))),
          el("div", {class: "live-bot"}, el("b", {}, "Crypto day trading"), el("span", {id: "liveCdayText", class: "hint", style: "margin:0"}),
            el("div", {id: "liveCfg-crypto_day", class: "bot-cfg"}),
            el("div", {class: "row"},
              el("button", {class: "btn primary", type: "button", id: "liveCdayStart", onclick: () => post("/api/crypto-day/start", {}, "Crypto day trading started.").then(liveTick).catch(() => {})}, "Start crypto day trading"),
              el("button", {class: "btn danger", type: "button", id: "liveCdayStop", hidden: true, onclick: () => post("/api/crypto-day/stop", {}, "Crypto day trading stopped.").then(liveTick).catch(() => {})}, "Stop crypto day trading")))),
        el("div", {id: "autoPanelLive", style: "margin-top:12px"})),
      el("section", {class: "card", id: "livePending", hidden: true}, el("h2", {}, "Waiting for your approval"), el("div", {class: "proposals", id: "livePendingList"})),
      el("div", {class: "two"},
        el("section", {class: "card"},
          el("div", {class: "row", style: "justify-content:space-between"}, el("h2", {style: "margin:0"}, el("span", {id: "liveSym"}, ""), " ", el("span", {id: "livePrice", class: "muted"})),
            el("div", {id: "liveChips", class: "row", style: "gap:6px"})),
          el("div", {class: "row legend"},
            el("span", {}, el("b", {class: "up"}, "▲"), " Bot bought"), el("span", {}, el("b", {class: "down"}, "▼"), " Bot sold"),
            el("span", {}, lineKey("#d03b3b"), "Stop-loss"), el("span", {}, lineKey("#0ca30c"), "Target"), el("span", {}, lineKey("#898781"), "Your entry")),
          el("div", {id: "liveChart", class: "live-chart"}),
          el("p", {class: "hint", id: "liveChartNote"}, "Today's 5-minute candles. Updates every 2 seconds while the market is open (crypto any time).")),
        el("section", {class: "card"}, el("h2", {}, "Live feed"), el("div", {id: "liveFeed", class: "log live-feed"}))),
      el("div", {class: "two"},
        el("section", {class: "card"}, el("h2", {}, "Open trades"), el("div", {id: "livePositions", class: "scroll"})),
        el("section", {class: "card"}, el("h2", {}, "Today's scoreboard"), el("div", {id: "liveScore", class: "stats", style: "grid-template-columns:1fr 1fr"}))));
  }
  if (!LIVE.timers) {
    LIVE.timers = true;
    setInterval(() => { if (MK.view === "live") liveControls(); }, 1000);
    setInterval(() => { if (MK.view === "live") liveTick(); }, 5000);
    setInterval(() => { if (MK.view === "live") livePrice(); }, 2000);
    setInterval(() => { if (MK.view === "live") liveBars(); }, 60000);
  }
  liveControls(); liveTick(true);
};
function liveControls() {
  if (!ST()) return;
  const d = S.day || {}, c = S.crypto || {}, now = Date.now() / 1000;
  const cd = (t) => t ? (() => { const s = Math.max(0, Math.round(t - now)); return `next check in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; })() : "checking now";
  document.getElementById("liveDayStart").hidden = d.running; document.getElementById("liveDayStop").hidden = !d.running;
  document.getElementById("liveCryptoStart").hidden = c.running; document.getElementById("liveCryptoStop").hidden = !c.running;
  document.getElementById("liveDayText").textContent = d.running ? `Running, ${cd(d.next_check)}. ${d.entries_today} trade(s) opened today.` : d.flattened ? "Finished for today." : "Stopped.";
  const bs = S.bot_status || {}, cy = S.crypto_day || {};
  document.getElementById("liveCryptoText").textContent = c.running ? (bs.crypto || `Running, ${cd(c.next_check)}.`) : (bs.crypto ? bs.crypto + "." : "Stopped.");
  document.getElementById("liveCdayStart").hidden = cy.running; document.getElementById("liveCdayStop").hidden = !cy.running;
  document.getElementById("liveCdayText").textContent = cy.running ? (bs.crypto_day || `Running, ${cd(cy.next_check)}.`) : (bs.crypto_day ? bs.crypto_day + "." : "Stopped.");
  if (bs.day && (d.running || /Shut off/.test(bs.day))) document.getElementById("liveDayText").textContent = bs.day + ".";
  const busy = document.getElementById("liveBusy");
  busy.className = "pill " + (S.busy || d.running || c.running || cy.running ? "open" : "");
  busy.lastChild.textContent = S.busy || (d.running || c.running || cy.running ? "Watching the market" : "Idle");
  const pending = (S.proposals || []).filter((p) => p.status === "pending");
  document.getElementById("livePending").hidden = !pending.length;
  document.getElementById("livePendingList").replaceChildren(...pending.map((p) => propCard(p, now)));
  const feed = document.getElementById("liveFeed"), rows = S.activity || [];
  if (feed.dataset.top !== (rows[0] ? rows[0].time + rows[0].text : "")) {
    feed.dataset.top = rows[0] ? rows[0].time + rows[0].text : "";
    feed.replaceChildren(...(rows.length ? rows.map((a) => el("div", {class: /order sent|Bought|Sold|placed/i.test(a.text) ? "feed-trade" : /fail|couldn't|skipped|problem/i.test(a.text) ? "feed-warn" : ""}, el("time", {}, a.time), a.text))
      : [el("div", {class: "empty"}, "Nothing yet. Start a bot above and its checks, Claude's reasoning and every order show up here.")]));
  }
}
async function liveTick(first) {
  let d;
  try { d = await api("/api/live?symbol=" + enc(LIVE.symbol)); } catch (e) { document.getElementById("liveChartNote").textContent = e.message; return; }
  LIVE.data = d;
  const changed = d.symbol !== LIVE.symbol;
  LIVE.symbol = d.symbol;
  document.getElementById("liveSym").replaceChildren(el("a", {href: "#s/" + enc(d.symbol)}, d.symbol));
  document.getElementById("liveChips").replaceChildren(...d.symbols.map((s) => el("button", {type: "button", class: "chip" + (s === d.symbol ? " on" : ""), onclick: () => { LIVE.symbol = s; liveTick(true); }}, s)));
  if (first || changed) await liveBars();
  else liveMarks();
  const pos = d.positions;
  document.getElementById("livePositions").replaceChildren(pos.length ? el("table", {class: "click"},
    el("thead", {}, el("tr", {}, ["Holding", "Qty", "Entry", "Now", "Profit / loss", "Stop", "Target"].map((h, i) => el("th", {class: i ? "num" : ""}, h)))),
    el("tbody", {}, pos.map((p) => el("tr", {tabindex: 0, onclick: () => { LIVE.symbol = p.link; liveTick(true); }},
      el("td", {}, el("b", {}, p.name)), el("td", {class: "num"}, String(p.qty)), el("td", {class: "num"}, fmtPrice(p.avg_entry_price)), el("td", {class: "num"}, fmtPrice(p.current_price)),
      el("td", {class: "num " + tone(p.unrealized_pl)}, signed(p.unrealized_pl) + " (" + pct(p.unrealized_plpc) + ")"),
      el("td", {class: "num"}, p.stop ? fmtPrice(p.stop) + ` (${pct(p.to_stop_pct)})` : "–"),
      el("td", {class: "num"}, p.target ? fmtPrice(p.target) + ` (${pct(p.to_target_pct)})` : "–")))))
    : el("div", {class: "empty"}, "No open positions."));
  for (const key of ["day", "crypto", "crypto_day"]) botConfig(key, d.bots && d.bots[key]);
  const sc = d.score, stat = (k, v, cls) => el("div", {class: "stat"}, el("span", {}, k), el("b", {class: cls || ""}, v));
  document.getElementById("liveScore").replaceChildren(
    stat("Realized today", signed(sc.realized), tone(sc.realized)), stat("Open profit / loss", signed(sc.unrealized), tone(sc.unrealized)),
    stat("Closed trades", String(sc.closed)), stat("Wins / losses", `${sc.wins} / ${sc.losses}`), stat("Fills today", String(sc.fills)),
    stat("Account today", ST() && S.account ? signed(S.account.equity - S.account.last_equity) : "–", ST() && S.account ? tone(S.account.equity - S.account.last_equity) : ""));
}
async function liveBars() {
  const sym = LIVE.symbol; if (!sym) return;
  let r;
  try { r = await api(`/api/market/bars?symbol=${enc(sym)}&range=1D`); } catch (e) { document.getElementById("liveChartNote").textContent = e.message; return; }
  if (sym !== LIVE.symbol) return;
  LIVE.bars = r.bars;
  const box = document.getElementById("liveChart");
  if (LIVE.chart) { LIVE.chart.remove(); LIVE.chart = null; }
  if (!window.LightweightCharts) return;
  LIVE.chart = LightweightCharts.createChart(box, chartOptions(420, true));
  LIVE.series = LIVE.chart.addCandlestickSeries({upColor: UP(), downColor: DOWN(), wickUpColor: UP(), wickDownColor: DOWN(), borderVisible: false});
  LIVE.series.setData(LIVE.bars.map((b) => ({time: localT(b.t), open: b.o, high: b.h, low: b.l, close: b.c})));
  LIVE.lines = [];
  LIVE.chart.timeScale().fitContent();
  document.getElementById("liveChartNote").textContent = LIVE.bars.length ? "Today's 5-minute candles. Updates every 2 seconds while the market is open (crypto any time)." : "No trading yet today for " + sym + ".";
  liveMarks();
}
function liveMarks() {
  const d = LIVE.data; if (!LIVE.series || !d || d.symbol !== LIVE.symbol) return;
  const bars = LIVE.bars;
  const barFor = (iso) => { const t = Date.parse(iso) / 1000; let hit = null; for (const b of bars) { if (b.t <= t) hit = b; else break; } return hit; };
  const marks = [];
  for (const f of d.fills) {
    const b = barFor(f.time); if (!b) continue;
    marks.push({time: localT(b.t), position: f.side === "buy" ? "belowBar" : "aboveBar", color: f.side === "buy" ? UP() : DOWN(),
      shape: f.side === "buy" ? "arrowUp" : "arrowDown", text: `${f.side === "buy" ? "Buy" : "Sell"} ${+(+f.qty).toFixed(6)} @ ${fmtPrice(f.price)}`});
  }
  marks.sort((a, b) => a.time - b.time);
  LIVE.series.setMarkers(marks);
  for (const l of LIVE.lines) LIVE.series.removePriceLine(l);
  LIVE.lines = [];
  const add = (price, color, title, style) => price && LIVE.lines.push(LIVE.series.createPriceLine({price, color, lineWidth: 2, lineStyle: style, axisLabelVisible: true, title}));
  for (const o of d.orders) {
    if (o.side === "sell" && ["stop", "stop_limit", "trailing_stop"].includes(o.type)) add(o.stop_price, "#d03b3b", "Stop", 2);
    else if (o.side === "sell" && o.type === "limit") add(o.limit_price, "#0ca30c", "Target", 2);
    else if (o.side === "buy" && o.limit_price) add(o.limit_price, css("--series"), "Buy order", 1);
  }
  const p = d.positions.find((x) => x.link === d.symbol);
  if (p) add(p.avg_entry_price, "#898781", "Your entry", 1);
}
async function livePrice() {
  const sym = LIVE.symbol; if (!sym || !LIVE.series || !LIVE.bars.length) return;
  const open = sym.includes("/") || (ST() && S.clock && S.clock.is_open);
  if (!open) { document.getElementById("livePrice").textContent = "· market closed"; return; }
  try {
    const q = (await api("/api/market/quote?symbols=" + enc(sym))).quotes[sym];
    if (!q || !q.price || sym !== LIVE.symbol) return;
    document.getElementById("livePrice").textContent = "· " + fmtPrice(q.price);
    const now = Date.now() / 1000, last = LIVE.bars[LIVE.bars.length - 1];
    if (now >= last.t + LIVE.barSec) {  // a new 5-minute candle has started
      const t = Math.floor(now / LIVE.barSec) * LIVE.barSec;
      LIVE.bars.push({t, o: q.price, h: q.price, l: q.price, c: q.price, v: 0});
    } else { last.c = q.price; last.h = Math.max(last.h, q.price); last.l = Math.min(last.l, q.price); }
    const b = LIVE.bars[LIVE.bars.length - 1];
    LIVE.series.update({time: localT(b.t), open: b.o, high: b.h, low: b.l, close: b.c});
  } catch (_) {}
}


// ---------- auto-approve panel (Live tab and Bot page) ----------
const AUTO_BOTS = [["long_term", "Long-term"], ["day", "Day trading"], ["options", "Options"], ["crypto", "Crypto"]];
FX.autoPanel = function (box) {
  if (!box || !ST() || !S.auto_rules) return;
  if (box.contains(document.activeElement) && document.activeElement.type === "range") return;  // don't redraw mid-drag
  const live = S.mode === "live", on = !!S.auto_enabled, r = S.auto_rules;
  const save = async (patch) => {
    try { await api("/api/settings", {settings: patch}); await refresh(); } catch (e) { toast(e.message, 7000); }
  };
  const amount = el("b", {}, money0(r.max_usd));
  const slider = el("input", {type: "range", min: 0, max: 2000, step: 25, value: r.max_usd, disabled: !on || live, "aria-label": "Largest trade to auto-approve",
    oninput: (e) => { amount.textContent = money0(+e.target.value); },
    onchange: (e) => save({auto_rules: {max_usd: +e.target.value}})});
  box.replaceChildren(el("div", {class: "auto-panel" + (on && !live ? " on" : "")},
    el("label", {class: "switch"},
      el("input", {type: "checkbox", role: "switch", checked: on && !live, disabled: live, onchange: (e) => save({auto_approve: e.target.checked})}),
      el("span", {class: "track"}, el("span", {class: "thumb"})),
      el("b", {}, "Auto-approve"), el("span", {class: "muted"}, live ? " (paper only: off in live mode)" : on ? " on" : " off")),
    el("div", {class: "auto-slider"},
      el("span", {}, "Approve trades up to ", amount, " each"), slider,
      el("div", {class: "row hint", style: "justify-content:space-between;margin:0"}, el("span", {}, "$0"), el("span", {}, "$1,000"), el("span", {}, "$2,000"))),
    el("div", {class: "row", style: "gap:14px"}, el("span", {class: "muted"}, "For:"),
      AUTO_BOTS.map(([k, label]) => el("label", {class: "check", style: "font-weight:500"},
        el("input", {type: "checkbox", checked: !!r[k], disabled: !on || live, onchange: (e) => save({auto_rules: {[k]: e.target.checked}})}), el("span", {}, label)))),
    el("p", {class: "hint", style: "margin:0"}, live ? "In live mode every trade waits for your approval."
      : on ? "Trades from the ticked bots that pass every limit and are no bigger than this are placed without asking. Bigger ones, and other bots', still wait for your Approve."
        : "Off: every trade waits for your Approve.")));
};
setInterval(() => {
  if (MK.view === "live") FX.autoPanel(document.getElementById("autoPanelLive"));
  if (MK.view === "bot") FX.autoPanel(document.getElementById("autoPanelBot"));
}, 1000);

// ---------- Calendar: profit or loss for every day ----------
const CAL = {month: null, data: null};
FX.renderCalendar = async function () {
  const root = document.getElementById("view-calendar");
  if (!root.dataset.built) {
    root.dataset.built = "1";
    root.append(el("section", {class: "card"},
      el("div", {class: "row", style: "justify-content:space-between"},
        el("div", {class: "row"},
          el("button", {class: "btn small", type: "button", "aria-label": "Previous month", onclick: () => { CAL.month = addMonths(CAL.month, -1); drawCalendar(); }}, "‹"),
          el("h2", {id: "calTitle", style: "margin:0;min-width:170px;text-align:center"}),
          el("button", {class: "btn small", type: "button", "aria-label": "Next month", onclick: () => { CAL.month = addMonths(CAL.month, 1); drawCalendar(); }}, "›"),
          el("button", {class: "btn small", type: "button", onclick: () => { CAL.month = null; drawCalendar(); }}, "This month")),
        el("span", {class: "hint", style: "margin:0"}, "Daily change in your account value (it includes open positions, not just closed trades).")),
      el("div", {id: "calStats", class: "stats", style: "margin:12px 0"}),
      el("div", {id: "calGrid", class: "cal-grid"})),
      el("section", {class: "card"}, el("h2", {}, "Month by month"), el("div", {id: "calMonths", class: "cal-months"})));
  }
  document.getElementById("calGrid").replaceChildren(loading("Reading your account history…"));
  try { CAL.data = await api("/api/reports/daily-pl"); } catch (e) { document.getElementById("calGrid").replaceChildren(el("div", {class: "empty"}, e.message)); return; }
  drawCalendar();
};
const ymKey = (d) => d.slice(0, 7);
function addMonths(ym, n) { const [y, m] = (ym || CAL.data.today.slice(0, 7)).split("-").map(Number); const d = new Date(Date.UTC(y, m - 1 + n, 1)); return d.toISOString().slice(0, 7); }
function drawCalendar() {
  const data = CAL.data; if (!data) return;
  const ym = CAL.month || data.today.slice(0, 7);
  CAL.month = ym;
  const byDate = Object.fromEntries(data.days.map((d) => [d.date, d]));
  const [y, m] = ym.split("-").map(Number);
  document.getElementById("calTitle").textContent = new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString([], {month: "long", year: "numeric", timeZone: "UTC"});
  const days = data.days.filter((d) => ymKey(d.date) === ym && d.change !== null && d.change !== undefined);
  const total = days.reduce((a, d) => a + d.change, 0), up = days.filter((d) => d.change > 0.005), down = days.filter((d) => d.change < -0.005);
  const best = days.reduce((a, d) => !a || d.change > a.change ? d : a, null), worst = days.reduce((a, d) => !a || d.change < a.change ? d : a, null);
  const realized = data.days.filter((d) => ymKey(d.date) === ym).reduce((a, d) => a + (d.realized || 0), 0);
  const stat = (k, v, cls) => el("div", {class: "stat"}, el("span", {}, k), el("b", {class: cls || ""}, v));
  const short = (d) => d ? new Date(d.date + "T12:00").toLocaleDateString([], {month: "short", day: "numeric"}) : "";
  document.getElementById("calStats").replaceChildren(
    stat("Month total", days.length ? signed(total) : "–", tone(total)), stat("Up days / down days", `${up.length} ▲ / ${down.length} ▼`),
    stat("Best day", best && best.change > 0 ? `${signed(best.change)} (${short(best)})` : "–", "up"),
    stat("Worst day", worst && worst.change < 0 ? `${signed(worst.change)} (${short(worst)})` : "–", "down"),
    stat("Realized from closed trades", signed(realized), tone(realized)));
  const first = new Date(Date.UTC(y, m - 1, 1)), startDow = first.getUTCDay(), count = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const maxAbs = Math.max(1, ...days.map((d) => Math.abs(d.pct || 0)));
  const cells = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((d) => el("div", {class: "cal-head"}, d));
  for (let i = 0; i < startDow; i++) cells.push(el("div", {class: "cal-cell blank"}));
  for (let day = 1; day <= count; day++) {
    const date = `${ym}-${String(day).padStart(2, "0")}`, d = byDate[date], has = d && d.change !== null && d.change !== undefined;
    const dow = (startDow + day - 1) % 7, future = date > data.today;
    const strength = has ? Math.min(1, Math.abs(d.pct || 0) / Math.min(maxAbs, 3)) : 0;
    const bg = has && Math.abs(d.change) > 0.005 ? `rgba(${d.change > 0 ? "12,163,12" : "208,59,59"},${(0.12 + 0.38 * strength).toFixed(2)})` : "";
    const tip = has ? `${date}: ${signed(d.change)} (${pct(d.pct)})` + (d.fills ? `, ${d.fills} fill(s)` : "") + (d.realized ? `, realized ${signed(d.realized)}` : "") + (d.live ? " (so far today)" : "") : date;
    cells.push(el("div", {class: "cal-cell" + (date === data.today ? " today" : "") + (dow === 0 || dow === 6 ? " weekend" : "") + (future ? " future" : ""), style: bg ? `background:${bg}` : "", title: tip},
      el("span", {class: "cal-day"}, String(day)),
      has ? el("b", {class: "cal-amt"}, (d.change > 0.005 ? "▲ " : d.change < -0.005 ? "▼ " : "") + signed(d.change)) : null,
      has ? el("span", {class: "cal-pct"}, pct(d.pct)) : null,
      d && d.fills ? el("span", {class: "cal-fills"}, d.fills + (d.fills === 1 ? " trade" : " trades")) : null));
  }
  document.getElementById("calGrid").replaceChildren(...cells);
  const months = {};
  for (const d of data.days) if (d.change !== null && d.change !== undefined) months[ymKey(d.date)] = (months[ymKey(d.date)] || 0) + d.change;
  const keys = Object.keys(months).sort().slice(-12).reverse();
  document.getElementById("calMonths").replaceChildren(...(keys.length ? keys.map((k) => el("button", {type: "button", class: "cal-month" + (k === ym ? " on" : ""), onclick: () => { CAL.month = k; drawCalendar(); }},
    el("span", {}, new Date(k + "-15T12:00").toLocaleDateString([], {month: "short", year: "numeric"})), el("b", {class: tone(months[k])}, signed(months[k])))) : [el("div", {class: "empty"}, "No history yet. Results appear after your first trading day.")]));
}


// ---------- per-bot goals and run hours (Live tab) ----------
const toTime = (v) => { v = +v || 0; return v ? String(Math.floor(v / 100)).padStart(2, "0") + ":" + String(v % 100).padStart(2, "0") : ""; };
const fromTime = (t) => { if (!t) return 0; const [h, m] = t.split(":").map(Number); return h * 100 + m; };
function botConfig(key, b) {
  const box = document.getElementById("liveCfg-" + key);
  if (!box || !b || box.contains(document.activeElement)) return;
  const open = box.querySelector("details") && box.querySelector("details").open;
  const window_ = b.run_from === b.run_to ? "any time" : `${toTime(b.run_from)}–${toTime(b.run_to)} ET`;
  const dollars = (p) => b.portfolio ? ` (${money0(b.portfolio * p / 100)})` : "";
  const goals = [b.target_pct > 0 ? `goal +${b.target_pct}%${dollars(b.target_pct)}` : "", b.limit_pct > 0 ? `limit −${b.limit_pct}%${dollars(b.limit_pct)}` : ""].filter(Boolean).join(", ") || "no daily goal";
  const equiv = (input, out) => { out.textContent = input.value > 0 && b.portfolio ? "≈ " + money(b.portfolio * input.value / 100) + " today" : "off"; };
  const tOut = el("small", {}), lOut = el("small", {});
  const form = el("form", {class: "bot-cfg-form", onsubmit: async (e) => {
    e.preventDefault(); const f = e.target;
    const from = fromTime(f.from.value), to = fromTime(f.to.value);
    try {
      await api("/api/settings", {settings: {[key]: {daily_profit_target_pct: f.target.value || 0, daily_loss_limit_pct: f.limit.value || 0,
        run_from_hhmm: from, run_to_hhmm: (f.from.value && f.to.value) ? to : from, check_every_min: f.every.value}}});
      toast("Saved. The bot uses these from its next check."); f.closest("details").open = false; liveTick();
    } catch (err) { toast(err.message, 7000); }
  }},
    el("div", {class: "form-grid"},
      el("label", {}, "Shut off at profit (% of portfolio)", el("input", {type: "number", name: "target", min: 0, max: 100, step: 0.1, value: b.target_pct || "", placeholder: "off", oninput: (e) => equiv(e.target, tOut)}), tOut),
      el("label", {}, "Shut off at loss (% of portfolio)", el("input", {type: "number", name: "limit", min: 0, max: 100, step: 0.1, value: b.limit_pct || "", placeholder: "off", oninput: (e) => equiv(e.target, lOut)}), lOut),
      el("label", {}, "Run from (New York time)", el("input", {type: "time", name: "from", value: toTime(b.run_from)})),
      el("label", {}, "Run until", el("input", {type: "time", name: "to", value: toTime(b.run_to)})),
      el("label", {}, "Search every (minutes)", el("input", {type: "number", name: "every", min: 5, max: 240, step: 1, value: b.every || 15}))),
    el("p", {class: "hint", style: "margin:0"}, "Percent of your portfolio's value at the start of the day, counting this bot's trades today. When either is reached the bot shuts itself off for the day: no new trades and no more Claude use, while trades already open still finish at their target or stop-loss, or are sold at the end of the day. Leave empty for no limit, and both times empty to run any time."),
    el("div", {}, el("button", {class: "btn small primary", type: "submit"}, "Save")));
  setTimeout(() => { equiv(form.target, tOut); equiv(form.limit, lOut); }, 0);
  box.replaceChildren(
    el("div", {class: "row bot-today"}, el("span", {}, "Today: ", el("b", {class: tone(b.total)}, signed(b.total))),
      el("span", {class: "muted"}, `· ${goals} · ${window_} · every ${b.every || "?"} min`)),
    el("details", {open: open}, el("summary", {}, "Goals & hours"), form));
}


// ---------- Settings > Phone access (shown on the Mac only) ----------
const PUSH_KINDS = [["push_approvals", "A trade needs my approval"], ["push_trades", "Trades placed or closed"],
  ["push_goals_alerts", "Goals reached and price alerts"], ["push_summary", "Daily summary"], ["push_errors", "Problems (e.g. an order failed)"]];
FX.phoneSettings = async function (refresh) {
  const box = document.getElementById("phoneSettings"); if (!box) return;
  if (ST() && S.remote) {
    box.replaceChildren(el("fieldset", {}, el("legend", {}, "This phone"),
      el("p", {class: "hint", style: "margin-top:0"}, "You're using Trader HQ from your phone. Keys, phone settings and switching paper/live can only be changed on the Mac."),
      el("button", {class: "btn small", type: "button", onclick: async () => { try { await api("/api/phone/logout", {}); } catch (_) {} location.replace("/?signed-out=1"); }}, "Sign out this phone")));
    return;
  }
  let st;
  try { st = await api("/api/phone" + (refresh ? "?refresh=1" : "")); } catch (e) { box.replaceChildren(el("fieldset", {}, el("legend", {}, "Phone access"), el("p", {class: "msg err"}, e.message))); return; }
  const ts = st.tailscale || {}, ph = st.settings;
  const again = () => FX.phoneSettings(true);
  const saveSettings = async (patch, msg) => {
    try { await api("/api/settings", {settings: {phone: patch}}); if (msg) toast(msg); } catch (e) { toast(e.message, 7000); }
    setTimeout(again, 300);
  };
  const step = (n, done, title, ...body) => el("div", {class: "phone-step" + (done ? " done" : "")}, el("h3", {}, el("span", {class: "num"}, done ? "✓" : String(n)), title), ...body);
  const copy = (text) => el("button", {class: "btn small", type: "button", onclick: () => { navigator.clipboard && navigator.clipboard.writeText(text); toast("Copied."); }}, "Copy");

  const s1 = step(1, !!ts.ip, "Connect this Mac and your iPhone with Tailscale (free)",
    ts.ip ? el("p", {class: "hint", style: "margin:0"}, `This Mac is on your private Tailscale network as ${ts.dns || ts.ip}.`)
      : el("div", {class: "hint", style: "margin:0"},
        el("p", {style: "margin:0 0 6px"}, ts.installed ? "Tailscale is installed but not connected. Open Tailscale on this Mac and sign in." : "Tailscale makes a private, encrypted link between your own devices. Nothing is opened to the internet."),
        el("ol", {style: "margin:0;padding-left:18px"},
          el("li", {}, "On this Mac: install Tailscale from the Mac App Store (or tailscale.com/download), open it and sign in."),
          el("li", {}, "On your iPhone: install Tailscale from the App Store and sign in with the same account."),
          el("li", {}, "Turn Tailscale on (connected) on both."))),
    el("div", {}, el("button", {class: "btn small", type: "button", onclick: again}, "Check again")));

  const pass = el("input", {type: "password", autocomplete: "new-password", minlength: 6, placeholder: st.has_passcode ? "Enter a new passcode to change it" : "At least 6 characters", style: "max-width:280px"});
  const s2 = step(2, st.has_passcode, "Choose a phone passcode",
    el("p", {class: "hint", style: "margin:0"}, "Your phone asks for it once, then stays signed in. After 5 wrong tries it locks for 15 minutes. Changing it signs out every phone."),
    el("form", {class: "row", onsubmit: async (e) => { e.preventDefault();
      try { await api("/api/phone/passcode", {passcode: pass.value}); toast("Passcode saved."); pass.value = ""; again(); } catch (err) { toast(err.message, 7000); } }},
      pass, el("button", {class: "btn small primary", type: "submit"}, st.has_passcode ? "Change" : "Save")));

  const s3 = step(3, st.running, "Turn on phone access",
    el("label", {class: "check"}, el("input", {type: "checkbox", checked: ph.enabled, disabled: !st.has_passcode, onchange: (e) => saveSettings({enabled: e.target.checked}, e.target.checked ? "Phone access on." : "Phone access off.")}),
      el("span", {}, "Allow my phone to open Trader HQ", !st.has_passcode ? el("small", {style: "display:block"}, "Set a passcode first.") : null)),
    el("label", {class: "check"}, el("input", {type: "checkbox", checked: ph.keep_mac_awake, onchange: (e) => saveSettings({keep_mac_awake: e.target.checked})}),
      el("span", {}, "Keep this Mac awake while phone access or a bot is on", el("small", {style: "display:block"}, "Keep it plugged in. Closing the lid still puts most Macs to sleep."))),
    ph.enabled && !ts.ip ? el("p", {class: "msg err", style: "margin:0"}, "Waiting for Tailscale (step 1).") : null);

  let s4 = null;
  if (st.running && st.url) {
    const qrBox = el("div", {class: "qr"});
    try { const qr = qrcode(0, "M"); qr.addData(st.url); qr.make(); qrBox.innerHTML = qr.createSvgTag({cellSize: 4, margin: 2}); } catch (_) { qrBox.hidden = true; }
    s4 = step(4, st.sessions.length > 0, "Open it on your iPhone",
      el("div", {class: "phone-link"}, qrBox,
        el("div", {style: "display:grid;gap:6px;min-width:0"},
          el("div", {class: "row"}, el("code", {}, st.url), copy(st.url)),
          el("ol", {class: "hint", style: "margin:0;padding-left:18px"},
            el("li", {}, "Make sure Tailscale is on (connected) on the iPhone."),
            el("li", {}, "Point the iPhone Camera at this code and tap the link (or type the address into Safari)."),
            el("li", {}, "Enter your passcode."),
            el("li", {}, "In Safari, tap Share, then Add to Home Screen, for a Trader HQ app icon.")))),
      st.sessions.length ? el("div", {}, el("b", {}, "Signed-in phones"),
        el("table", {}, el("tbody", {}, st.sessions.map((x) => el("tr", {}, el("td", {}, x.name), el("td", {class: "muted"}, "since " + new Date(x.created * 1000).toLocaleDateString()),
          el("td", {class: "muted"}, "last used " + new Date(x.last * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"})),
          el("td", {}, el("button", {class: "btn small danger", type: "button", onclick: async () => { await api("/api/phone/revoke", {id: x.id}); toast("Signed out."); again(); }}, "Sign out")))))),
        el("button", {class: "btn small", type: "button", onclick: async () => { if (confirm("Sign out every phone?")) { await api("/api/phone/revoke", {}); again(); } }}, "Sign out all phones")) : null);
  }

  const topic = ph.push_topic;
  const s5 = step(st.running ? 5 : 4, !!topic, "Phone notifications (optional)",
    !topic ? el("div", {}, el("p", {class: "hint", style: "margin:0 0 8px"}, "Get a notification on your phone when a trade needs your approval, a goal is reached, an alert fires and more. It uses the free ntfy app and works even without phone access."),
      el("button", {class: "btn small primary", type: "button", onclick: async () => { await api("/api/phone/new-topic", {}); again(); }}, "Set up notifications"))
      : el("div", {style: "display:grid;gap:8px"},
        el("ol", {class: "hint", style: "margin:0;padding-left:18px"},
          el("li", {}, "On your iPhone, install the free \"ntfy\" app from the App Store and allow notifications."),
          el("li", {}, "In ntfy, tap +, and subscribe to this topic (leave the server as ntfy.sh):")),
        el("div", {class: "row topic"}, el("code", {}, topic), copy(topic)),
        el("div", {class: "form-grid", style: "grid-template-columns:repeat(auto-fill,minmax(230px,1fr))"}, PUSH_KINDS.map(([k, label]) =>
          el("label", {class: "check"}, el("input", {type: "checkbox", checked: ph[k], onchange: (e) => saveSettings({[k]: e.target.checked})}), el("span", {}, label)))),
        el("div", {class: "row"},
          el("button", {class: "btn small primary", type: "button", onclick: async () => { try { await api("/api/phone/test-push", {}); toast("Test sent. It should appear on your phone in a few seconds."); } catch (e) { toast(e.message, 7000); } }}, "Send a test"),
          el("button", {class: "btn small", type: "button", onclick: async () => { if (confirm("Make a new topic? Your phone stops getting notifications until you subscribe to the new one.")) { await api("/api/phone/new-topic", {}); again(); } }}, "New topic"),
          el("button", {class: "btn small danger", type: "button", onclick: () => saveSettings({push_topic: ""}, "Phone notifications off.")}, "Turn off")),
        el("p", {class: "hint", style: "margin:0"}, "The topic name works like a private address: anyone who knows it could read these notifications, so keep it to yourself. Notifications never include keys or passwords.")));

  box.replaceChildren(el("fieldset", {}, el("legend", {}, "Phone access"),
    el("p", {class: "hint", style: "margin-top:0"}, "Use Trader HQ from your iPhone anywhere: start and stop bots, approve trades, watch Live. Your Mac does the work, so it needs to be on with Trader HQ open."),
    el("div", {class: "phone-steps"}, s1, s2, s3, s4, s5)));
};

route();
