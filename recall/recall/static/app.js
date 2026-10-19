/* Recall UI — vanilla JS single-page app. All numbers come from the local API. */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const app = $("#app");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtInt = (n) => (n ?? 0).toLocaleString("en-US");
const fmtT = (t) => (t == null ? "" : `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`);
const fmtBytes = (b) => { if (b == null) return "—"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; } return `${b.toFixed(i >= 2 ? 2 : 0)} ${u[i]}`; };
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "—";
const media = (p) => (p ? `/media/${p.split("/").map(encodeURIComponent).join("/")}` : "");
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}, ...opts,
    body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r;
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

let STATS = null;
let lastSearchMs = null;
const timers = [];
const clearTimers = () => { while (timers.length) clearInterval(timers.pop()); };

/* ------------------------------------------------------------------ footer */
async function refreshStats() {
  try { STATS = await api("/api/stats"); } catch (e) { return; }
  const c = STATS.counts;
  $("#footer-left").textContent = `Recall System v${STATS.version} — ${fmtInt(STATS.live)} saves indexed (${fmtInt(c.deep)} deep, ${c.partial ? `${fmtInt(c.partial)} thumbnail-only, ` : ""}${fmtInt(c.meta)} metadata-only${c.new ? `, ${fmtInt(c.new)} queued` : ""})`;
  renderClock();
}
function renderClock() {
  const t = new Date().toLocaleTimeString("en-GB");
  const lat = lastSearchMs != null ? `Last search: ${lastSearchMs.toFixed(0)} ms` : (STATS?.latency?.avg_ms != null ? `Avg search: ${STATS.latency.avg_ms.toFixed(0)} ms` : "No searches yet");
  $("#footer-right").textContent = `Local time: ${t} — ${location.hostname === "127.0.0.1" || location.hostname === "localhost" ? "Localhost only" : location.hostname} — ${lat}`;
}
setInterval(renderClock, 1000);
setInterval(refreshStats, 5000);

/* ------------------------------------------------------------------ router */
function route() {
  clearTimers();
  const h = location.hash || "#/";
  const [path, qs] = h.slice(1).split("?");
  const params = new URLSearchParams(qs || "");
  const nav = { "/": "index", "/search": "index", "/clusters": "clusters", "/import": "import", "/export": "export", "/settings": "settings" }[path] || "index";
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
  window.scrollTo(0, 0);
  if (path === "/search") return viewSearch(params);
  if (path === "/clusters") return viewClusters();
  if (path === "/import") return viewImport();
  if (path === "/export") return viewExport(params);
  if (path === "/settings") return viewSettings(params);
  return viewHome();
}
window.addEventListener("hashchange", route);

function searchHash(q, facets) {
  const p = new URLSearchParams();
  if (q) p.set("q", q);
  if (facets && Object.keys(facets).length) p.set("f", JSON.stringify(facets));
  return `#/search?${p.toString()}`;
}
function goSearch(q, facets) { location.hash = searchHash(q, facets); }

/* ------------------------------------------------------------------ shared: command bar + facets */
function commandBar(value = "") {
  return `<section class="search-section">
    <input type="text" class="command-bar" id="cmd" autocomplete="off" spellcheck="false" value="${esc(value)}"
      placeholder="Search your saves — “pasta recipe with feta”, “GRWM blue dress”, “@creator last week”">
    <span class="search-meta" id="search-meta"></span></section>`;
}
const FACET_TITLES = { cluster: "Clusters", attr: "Attributes", creator: "Creators", sound: "Sounds", hashtag: "Hashtags", saved: "Saved", duration: "Duration" };
function facetsHtml(facets, active = {}) {
  return Object.keys(FACET_TITLES).map((k) => {
    const items = facets[k] || [];
    const act = (active[k] || []).map(String);
    const body = items.length ? items.map((it) => {
      const on = act.includes(String(it.value));
      return `<a class="facet-item ${on ? "active" : ""} ${!it.count && !on ? "zero" : ""}" data-fk="${k}" data-fv="${esc(it.value)}" title="${esc(it.label)}">${esc(it.label)} (${fmtInt(it.count)})</a>`;
    }).join("") : `<span class="facet-empty">${k === "cluster" ? "Clusters appear after indexing" : "—"}</span>`;
    return `<div class="facet-group"><span class="facet-label">${FACET_TITLES[k]}</span>${body}</div>`;
  }).join("");
}
function bindFacets(root, q, active) {
  $$(".facet-item", root).forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    if (a.classList.contains("zero")) return;
    const k = a.dataset.fk, v = a.dataset.fv;
    const f = JSON.parse(JSON.stringify(active || {}));
    f[k] = f[k] || [];
    f[k] = f[k].map(String).includes(v) ? f[k].filter((x) => String(x) !== v) : [...f[k], v];
    if (!f[k].length) delete f[k];
    goSearch(q, f);
  }));
}
function bindCommandBar(facets) {
  const cmd = $("#cmd");
  cmd.focus();
  cmd.setSelectionRange(cmd.value.length, cmd.value.length);
  const go = debounce(() => {
    const q = cmd.value;
    const target = searchHash(q.trim() ? q : "", facets);
    if (location.hash.startsWith("#/search")) history.replaceState(null, "", target), runSearch(q, facets);
    else if (q.trim()) location.hash = target;
  }, 250);
  cmd.addEventListener("input", go);
  cmd.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); const h = searchHash(cmd.value, facets); if (location.hash === h) route(); else location.hash = h; } });
}

/* ------------------------------------------------------------------ shared: coverage + download banner */
const COV_SHORT = { Captions: "captions", Transcripts: "transcripts", "On-screen text": "on-screen", Visual: "visual", Sound: "sound", Hashtags: "hashtags" };
function coverageLine(st) {
  if (!st || !st.live) return `<span>Index coverage: —</span>`;
  const parts = ["Transcripts", "On-screen text", "Visual", "Captions"].filter((k) => st.coverage[k]).map((k) =>
    `<span title="${fmtInt(st.coverage[k].videos)} of ${fmtInt(st.live)} saves have ${esc(k.toLowerCase())}">${COV_SHORT[k]} <b>${st.coverage[k].pct}%</b></span>`);
  return `<span class="cov-line">Coverage: ${parts.join("")}</span>`;
}
function breakerBanner(b, env) {
  if (!b || !b.open) return "";
  const kind = { blocked: "TikTok returned a block page", forbidden: "TikTok refused the request (HTTP 403)", rate_limited: "TikTok is rate-limiting", cookies: "browser cookies couldn't be read", login_required: "TikTok wants a login", extract_failed: "yt-dlp couldn't parse TikTok's page", other: "unexpected yt-dlp error", no_file: "no file produced" }[b.kind] || b.kind;
  return `<div class="alert-banner" id="breaker"><div class="privacy-icon">[!]</div><div>
    <h4>Deep indexing paused — ${fmtInt(b.consecutive)} video downloads in a row failed (${esc(kind)})</h4>
    <p>Your saves are still searchable by caption, creator, hashtags and thumbnail; only transcript and full on-screen-text indexing is waiting. ${esc(b.hint || "")}</p>
    <p class="err mono" title="${esc((b.ids || []).join(", "))}">Last error: ${esc(b.message || "")}${env ? ` · yt-dlp ${esc(env.yt_dlp || "?")}${env.curl_cffi ? ` · curl_cffi ${esc(env.curl_cffi)}` : " · no curl_cffi"}` : ""}</p>
    <ol><li>Quit Recall and start it again with run.sh / run.bat — it updates yt-dlp on every launch.</li>
      <li>Settings → Video downloads → <b>Use cookies from my browser</b>: pick the browser you use TikTok in (Firefox is most reliable).</li>
      <li>Still failing? Paste your browser's User-Agent into Settings, or wait an hour (TikTok rate-limits bursts).</li></ol>
    <div class="btns"><button class="button" data-breaker="retry">Retry downloads</button><button class="button" data-breaker="thumbs">Index from thumbnails only</button><a class="button" href="#/settings?sec=downloads" style="text-decoration:none">Download settings</a></div>
  </div></div>`;
}
function bindBreaker(after) {
  $$("[data-breaker]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    await api(b.dataset.breaker === "retry" ? "/api/downloads/retry" : "/api/downloads/thumbnail-only", { method: "POST" });
    await refreshStats(); if (after) after();
  }));
}

/* ------------------------------------------------------------------ HOME */
async function viewHome() {
  app.innerHTML = commandBar("") + `<main class="workspace"><aside class="facets" id="facets"></aside><section class="results" id="home"><div class="loading">Loading index…</div></section></main>`;
  bindCommandBar({});
  const [facets] = await Promise.all([api("/api/facets"), refreshStats()]);
  $("#facets").innerHTML = facetsHtml(facets);
  bindFacets($("#facets"), "", {});
  const s = STATS, c = s.counts;
  const cov = Object.entries(s.coverage);
  const ideas = [];
  (facets.hashtag || []).slice(0, 2).forEach((h) => ideas.push({ q: h.label, why: `${h.count} saves` }));
  (facets.creator || []).slice(0, 2).forEach((h) => ideas.push({ q: h.label, why: `${h.count} saves` }));
  if ((facets.saved || []).find((x) => x.value === "This week")) ideas.push({ q: "saved last week", why: "date filter" });
  (facets.attr || []).filter((a) => a.count).slice(0, 1).forEach((a) => ideas.push({ f: { attr: [a.value] }, q: a.label, why: `${a.count} saves` }));
  $("#home").innerHTML = `
    <div class="results-header"><span>Command bar · type to search ${fmtInt(s.live)} saves</span>${coverageLine(s)}</div>
    ${breakerBanner(s.download_breaker, s.downloader)}
    <div class="home-hero">
      <span class="big-number">${fmtInt(s.live)}</span>
      <div class="empty-message">${s.live ? `saves in your private memory. Search by what was said, shown, written on screen, or how it looked.` : `Your memory is empty. Import your TikTok data export to begin — nothing leaves this machine.`}</div>
    </div>
    ${s.live ? "" : `<p style="margin-bottom:40px"><a class="button" href="#/import" style="text-decoration:none">Go to Import →</a></p>`}
    <div class="suggestions">
      <div><span class="facet-label">Try</span><ul class="suggestion-list">
        ${ideas.length ? ideas.map((i, n) => `<li class="click" data-idea="${n}">${esc(i.q)} <span style="opacity:.6">(${esc(i.why)})</span></li>`).join("") : "<li>Import saves to get suggestions from your own data</li>"}
        <li class="click" data-ex="video where a guy says stop doing this">“video where a guy says …” searches transcripts</li>
      </ul></div>
      <div><span class="facet-label">Index status</span><ul class="suggestion-list">
        <li>${fmtInt(c.deep)} deep-indexed · ${fmtInt(c.partial)} thumbnail-only · ${fmtInt(c.meta)} metadata only · ${fmtInt(c.new)} waiting</li>
        <li>${fmtInt(c.dead)} dead links · ${fmtInt(c.failed)} failed</li>
        <li>Worker: ${esc(s.worker.state)}${s.worker.models_loaded.length ? ` · models loaded: ${esc(s.worker.models_loaded.join(", "))}` : ""}</li>
      </ul></div>
    </div>
    <h3 class="fuzzy-section-title" style="margin-top:60px">Signal coverage</h3>
    <div class="stat-grid three">${cov.map(([k, v]) => `<div class="stat-card"><span class="stat-label">${esc(k)}</span><span class="stat-value">${v.pct}%</span>
      <span class="stat-sub">${fmtInt(v.videos)} of ${fmtInt(s.live)} saves</span><div class="cov-bar"><div style="width:${v.pct}%"></div></div></div>`).join("")}</div>`;
  $$("[data-idea]").forEach((li) => li.addEventListener("click", () => { const i = ideas[+li.dataset.idea]; goSearch(i.f ? "" : i.q, i.f || {}); }));
  $$("[data-ex]").forEach((li) => li.addEventListener("click", () => goSearch(li.dataset.ex, {})));
  bindBreaker(() => route());
}

/* ------------------------------------------------------------------ SEARCH / RESULTS */
let searchSeq = 0;
async function viewSearch(params) {
  const q = params.get("q") || "";
  let facets = {};
  try { facets = JSON.parse(params.get("f") || "{}"); } catch (e) {}
  app.innerHTML = commandBar(q) + `<main class="workspace"><aside class="facets" id="facets"></aside><section class="results" id="results"><div class="loading">Searching…</div></section></main>`;
  bindCommandBar(facets);
  if (!STATS) refreshStats();
  runSearch(q, facets);
}

async function runSearch(q, facets) {
  const seq = ++searchSeq;
  if (!STATS) await refreshStats();
  const t0 = performance.now();
  let res;
  try { res = await api(`/api/search?q=${encodeURIComponent(q)}&f=${encodeURIComponent(JSON.stringify(facets))}&limit=60`); }
  catch (e) { $("#results").innerHTML = `<div class="loading">Search failed: ${esc(e.message)}</div>`; return; }
  if (seq !== searchSeq) return;
  lastSearchMs = res.timing.total_ms; renderClock();
  $("#search-meta").textContent = `${res.timing.total_ms.toFixed(0)} ms server · ${(performance.now() - t0).toFixed(0)} ms total`;
  $("#facets").innerHTML = facetsHtml(res.facets, facets);
  bindFacets($("#facets"), q, facets);
  if (!res.total) return renderNoResults(res, q, facets);
  renderResults(res, q, facets);
}

function parsedChips(res) {
  const p = res.parsed;
  const chips = p.filters.map((f) => `<span class="pchip">${esc(f.type)}: <b>${esc(f.label)}</b></span>`);
  if (res.semantic_text) chips.push(`<span class="pchip intent">intent: <b>${esc(res.semantic_text)}</b></span>`);
  return chips.length ? `<div class="parsed">${chips.join("")}</div>` : "";
}

function renderResults(res, q, facets) {
  const box = $("#results");
  box.innerHTML = `<div class="results-header"><span>${fmtInt(res.total)} ${res.total === 1 ? "match" : "matches"} from ${fmtInt(res.candidates)} candidates · ${res.timing.total_ms.toFixed(0)} ms</span>
     <span><a id="export-results">Export these results</a> &nbsp;·&nbsp; ${coverageLine(STATS)}</span></div>
     ${parsedChips(res)}<div class="grid dense" id="grid"></div><div class="sentinel" id="sentinel"></div>`;
  const grid = $("#grid");
  let items = res.results.slice(), rendered = 0, fetching = false, total = res.total;
  const renderMore = () => {
    const frag = document.createElement("div");
    frag.innerHTML = items.slice(rendered, rendered + 24).map((r, i) => card(r, rendered + i + 1)).join("");
    const nodes = Array.from(frag.children);
    nodes.forEach((n) => { grid.appendChild(n); bindCard(n); });
    rendered += nodes.length;
  };
  renderMore();
  const io = new IntersectionObserver(async (ents) => {
    if (!ents.some((e) => e.isIntersecting)) return;
    if (rendered < items.length) return renderMore();
    if (items.length < total && !fetching) {
      fetching = true;
      const more = await api(`/api/search?q=${encodeURIComponent(q)}&f=${encodeURIComponent(JSON.stringify(facets))}&limit=60&offset=${items.length}`);
      items = items.concat(more.results); fetching = false; renderMore();
    }
  }, { rootMargin: "800px" });
  io.observe($("#sentinel"));
  $("#export-results").addEventListener("click", async () => {
    const ids = [];
    let off = 0;
    while (off < total) { const r = await api(`/api/search?q=${encodeURIComponent(q)}&f=${encodeURIComponent(JSON.stringify(facets))}&limit=200&offset=${off}`); ids.push(...r.results.map((x) => x.id)); off += 200; if (!r.results.length) break; }
    download({ ids, format: "json" });
  });
}

const SIGNAL_NAMES = { caption: "Caption", transcript: "Transcript", ocr: "On-screen text", creator: "Creator", sound: "Sound", hashtag: "Hashtag", note: "Note", visual: "Visual" };
function chipHtml(w) {
  const t = w.t != null ? ` @${fmtT(w.t)}` : "";
  let v = "";
  if (w.method === "keyword" || w.method === "phrase") v = `bm25 ${w.score.toFixed(2)}`;
  else if (w.method === "semantic") v = `cos ${w.score.toFixed(2)}`;
  else if (w.method === "visual") v = `clip ${w.score.toFixed(2)}`;
  const tip = w.method === "filter" || w.method === "facet" ? `Filter applied: ${w.label}` :
    `${w.label} via ${w.method}${w.rank ? ` · rank ${w.rank} in ${w.method} list` : ""}${w.rrf ? ` · RRF +${w.rrf.toFixed(4)}` : ""}${w.z != null ? ` · z=${w.z}` : ""}${w.terms ? ` · terms: ${w.terms.join(", ")}` : ""}`;
  return `<div class="chip ${w.method === "filter" || w.method === "facet" ? "filter" : ""}" title="${esc(tip)}"><span class="k">${esc(w.label)}${esc(t)}</span><span class="v">${esc(v)}</span></div>`;
}

function filmstrip(r) {
  const fr = r.frames || [];
  if (!fr.length) return "";
  let hit = -1;
  if (r.best_t != null) { let best = 1e9; fr.forEach((f, i) => { const d = Math.abs(f.t - r.best_t); if (d < best) { best = d; hit = i; } }); }
  const dur = r.duration || (fr[fr.length - 1].t + 1);
  const mark = r.best_t != null ? `<div class="mark" style="left:${Math.min(100, (100 * r.best_t) / dur).toFixed(1)}%" title="matched at ${fmtT(r.best_t)}"></div>` : "";
  return `<div class="filmstrip">${fr.map((f, i) => `<div class="fr ${i === hit ? "hit" : ""}" title="${fmtT(f.t)}"><img loading="lazy" src="${media(f.path)}" alt=""></div>`).join("")}</div>
    <div class="timeline">${mark}<span class="tl-l">0:00</span><span class="tl-r">${fmtT(dur)}</span></div>`;
}

function bestSnippet(r) {
  const w = r.why.find((x) => x.snippet && x.method !== "filter" && x.method !== "facet" && x.method !== "visual");
  if (w) return w.snippet;
  const v = r.why.find((x) => x.method === "visual");
  return v ? v.snippet : esc(r.caption || "");
}

function card(r, rank) {
  const tiktok = r.url && r.source !== "local" ? r.url : null;
  const why = r.why.filter((w) => w.method !== "facet");
  const boosts = (r.boosts || []).map((b) => `<div class="chip boost" title="Multiplies the fused score by (1 + ${b.value})"><span class="k">Boost: ${esc(b.name.replace("_", " "))}</span><span class="v">+${(b.value * 100).toFixed(1)}%</span></div>`).join("");
  const attrs = Object.keys(r.attrs || {}).map((k) => ({ has_recipe: "has recipe", has_product_link: "product link", talking_head: "talking head", screen_recording: "screen rec" }[k] || k));
  return `<article class="video-doc" data-id="${esc(r.id)}">
    <div class="video-thumb">${r.thumb ? `<img loading="lazy" src="${media(r.thumb)}" alt="">` : `<span class="thumb-missing">No thumbnail</span>`}
      <span class="rank-badge">#${rank}</span>${r.status === "partial" ? `<span class="state-badge" title="${esc(r.error || "Video could not be downloaded")}">thumbnail only</span>` : r.status !== "deep" ? `<span class="state-badge" title="Only caption/creator metadata indexed so far">metadata only</span>` : ""}
      ${r.best_t != null ? `<span class="ts-badge">▶ ${fmtT(r.best_t)}</span>` : ""}</div>
    ${filmstrip(r)}
    <div class="doc-meta">
      <div class="doc-row"><span class="doc-label">Score</span><span class="doc-value" title="RRF ${esc(JSON.stringify(r.rrf))}">${r.score.toFixed(4)} RRF</span></div>
      <div class="doc-row"><span class="doc-label">Creator</span><span class="doc-value">${r.creator ? "@" + esc(r.creator) : "—"}</span></div>
      <div class="doc-row"><span class="doc-label">Saved</span><span class="doc-value">${fmtDate(r.saved_at)}${r.duration ? ` · ${Math.round(r.duration)}s` : ""}</span></div>
      ${r.sound ? `<div class="doc-row"><span class="doc-label">Sound</span><span class="doc-value" title="${esc(r.sound)}">${esc(r.sound)}</span></div>` : ""}
      <div class="doc-row"><span class="doc-label">Matched</span><span class="doc-value wrap">${bestSnippet(r)}</span></div>
      ${attrs.length ? `<div class="doc-row"><span class="doc-label">Attributes</span><span class="doc-value">${esc(attrs.join(" · "))}</span></div>` : ""}
    </div>
    <div class="why"><span class="why-title">Why this matched</span>${why.map(chipHtml).join("")}${boosts}</div>
    ${r.note ? `<div class="note-text">“${esc(r.note)}”</div>` : ""}
    <div class="actions">${tiktok ? `<a href="${esc(tiktok)}" target="_blank" rel="noopener noreferrer">Open in TikTok ↗</a>` : `<a class="disabled" title="Local file">Local file</a>`}
      <a data-act="note">${r.note ? "Edit note" : "Add note"}</a><a data-act="delete">Delete from index</a></div>
  </article>`;
}

function bindCard(el) {
  const id = el.dataset.id;
  $$("[data-act]", el).forEach((a) => a.addEventListener("click", async () => {
    if (a.dataset.act === "delete") {
      if (!confirm("Remove this save from your Recall index? (It stays in TikTok; re-imports will skip it.)")) return;
      await api(`/api/videos/${encodeURIComponent(id)}`, { method: "DELETE" });
      el.remove(); refreshStats();
    } else if (a.dataset.act === "note") {
      if ($(".note-box", el)) return;
      const cur = $(".note-text", el)?.textContent.replace(/^“|”$/g, "") || "";
      const ta = document.createElement("textarea");
      ta.className = "note-box"; ta.rows = 2; ta.placeholder = "Your note (searchable). Enter to save, Esc to cancel"; ta.value = cur;
      a.parentElement.after(ta); ta.focus();
      ta.addEventListener("keydown", async (e) => {
        if (e.key === "Escape") ta.remove();
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          await api(`/api/videos/${encodeURIComponent(id)}/note`, { method: "POST", body: { note: ta.value } });
          ta.remove();
          let nt = $(".note-text", el);
          if (!ta.value.trim()) { nt?.remove(); return; }
          if (!nt) { nt = document.createElement("div"); nt.className = "note-text"; $(".actions", el).before(nt); }
          nt.textContent = `“${ta.value.trim()}”`;
        }
      });
    }
  }));
}

function renderNoResults(res, q, facets) {
  const s = res.suggestions || { broaden: [], help: [] };
  const short = q.length > 40 ? q.slice(0, 40) + "…" : q;
  $("#results").innerHTML = `
    <div class="results-header"><span>0 matches found for query · ${res.timing.total_ms.toFixed(0)} ms</span>${coverageLine(STATS)}</div>
    ${parsedChips(res)}
    <div class="empty-hero">
      <span class="big-number">0 matches</span>
      <div class="empty-message">Your search for “${esc(short)}” didn't return any direct hits. No caption, transcript, on-screen text, sound, creator or visual signal cleared its match threshold across ${fmtInt(res.candidates)} candidate saves.</div>
      <div class="suggestions">
        <div><span class="facet-label">Broaden search</span><ul class="suggestion-list">
          ${s.broaden.length ? s.broaden.map((b, i) => `<li class="click" data-b="${i}">${esc(b.text)}</li>`).join("") : "<li>Try a shorter or more general query</li>"}</ul></div>
        <div><span class="facet-label">System help</span><ul class="suggestion-list">
          ${s.help.map((h) => `<li>${esc(h)}</li>`).join("")}</ul></div>
      </div>
    </div>
    <div class="fuzzy-matches"><h3 class="fuzzy-section-title">Nearest fuzzy matches from archive (below match threshold)</h3>
      <div class="grid">${(res.fuzzy || []).map((f) => `
        <article class="video-doc" data-q="${esc(f.caption || "")}">
          <div class="video-thumb">${f.thumb ? `<img loading="lazy" src="${media(f.thumb)}" alt="">` : `<span class="thumb-missing">No thumbnail</span>`}</div>
          <div class="doc-meta">
            <div class="doc-row"><span class="doc-label">Sim Score</span><span class="doc-value" title="${esc(f.metric)}">${f.score.toFixed(2)} (${esc(f.signal)})</span></div>
            <div class="doc-row"><span class="doc-label">Creator</span><span class="doc-value">${f.creator ? "@" + esc(f.creator) : "—"}</span></div>
            <div class="doc-row"><span class="doc-label">Nearest</span><span class="doc-value">${esc(f.detail || f.caption || "")}</span></div>
          </div></article>`).join("") || `<p class="facet-empty">Nothing indexed with embeddings yet.</p>`}</div></div>`;
  $$("[data-b]").forEach((li) => li.addEventListener("click", () => { const b = s.broaden[+li.dataset.b]; goSearch(b.query, b.clear_facets ? {} : facets); }));
}

/* ------------------------------------------------------------------ IMPORT */
let importSource = "tiktok";
async function viewImport() {
  await refreshStats();
  const d = STATS.disk;
  const pct = d.disk_total ? Math.max(0.5, (100 * d.total_bytes) / d.disk_total) : 0;
  app.innerHTML = `<main class="workspace" style="margin-top:40px">
    <aside class="side">
      <div style="margin-bottom: 32px;"><span style="text-transform: capitalize; margin-bottom: 12px; display: block;">Sources</span>
        ${[["tiktok", "TikTok Archive"], ["local", "Local Files"], ["url", "URL Ingest"]].map(([k, l]) => `<a data-src="${k}" style="display:block;text-decoration:none;color:var(--text);margin-bottom:6px;cursor:pointer;${importSource === k ? "" : "opacity:0.6;"}">${l}</a>`).join("")}</div>
      <div style="margin-bottom: 32px;"><span style="text-transform: capitalize; margin-bottom: 12px; display: block;">Storage</span>
        <div style="font-size: 11px; opacity: 0.6; margin-bottom: 4px;" title="${esc(d.home)}">${fmtBytes(d.total_bytes)} used / ${fmtBytes(d.disk_free)} free</div>
        <div style="height: 2px; background: rgba(0,0,0,0.1); width: 100%;"><div style="height: 100%; background: var(--text); width: ${pct.toFixed(1)}%;"></div></div>
        <div style="font-size: 11px; opacity: 0.6; margin-top: 8px;">DB ${fmtBytes(d.db_bytes)} · thumbnails ${fmtBytes(d.media_bytes)}</div></div>
      <div class="howto"><span style="text-transform: capitalize; margin-bottom: 6px; display: block; opacity:1; font-size:13px">Get your TikTok export</span>
        <ol><li>TikTok app → Profile → ☰ → Settings and privacy → Account → Download your data</li><li>Choose <b>JSON</b>, select All data (or Activity), request</li><li>When ready (hours to days), download the .zip and drop it here</li></ol></div>
    </aside>
    <section class="page-content">
      <h2 class="section-title">Add to Archive</h2>
      <div id="breaker-slot"></div>
      <div id="src-panel"></div>
      <div class="flash" id="flash"></div>
      <div class="queue-container" id="queue"></div>
      <div class="privacy-banner"><div class="privacy-icon">[!]</div><div class="privacy-text"><h4>Local Processing Active</h4>
        <p>All transcription, OCR, visual embedding and indexing run on this computer. Nothing is uploaded. The only network requests are to TikTok itself: the public oEmbed endpoint for captions/thumbnails, and (if deep indexing is on) a temporary yt-dlp download of each saved video, deleted right after indexing.</p></div></div>
    </section></main>`;
  $$("[data-src]").forEach((a) => a.addEventListener("click", () => { importSource = a.dataset.src; viewImport(); }));
  renderSourcePanel();
  await renderQueue();
  timers.push(setInterval(renderQueue, 1500));
}

function renderSourcePanel() {
  const p = $("#src-panel");
  if (importSource === "url") {
    p.innerHTML = `<div class="url-ingest"><textarea id="links" rows="6" placeholder="Paste one or more TikTok links (https://www.tiktok.com/@user/video/…, vm.tiktok.com/…)"></textarea></div>
      <p style="margin-bottom:40px"><button class="button" id="add-links">Add links</button></p>`;
    $("#add-links").addEventListener("click", async () => {
      const r = await api("/api/import/links", { method: "POST", body: { text: $("#links").value } });
      flash(`Found ${r.found} links · added ${r.added} new · ${r.already_indexed} already indexed${r.unresolved ? ` · ${r.unresolved} could not be resolved` : ""}`);
      $("#links").value = ""; renderQueue();
    });
    return;
  }
  const local = importSource === "local";
  p.innerHTML = `<div class="drop-zone" id="drop"><span class="drop-zone-text">${local ? "Drop video files here" : "Drop video files or .zip archive here"}</span>
      <span class="drop-zone-subtext">${local ? "MP4, MOV, M4V, WEBM — or click to choose" : "Supports MP4, MOV, and TikTok Data Exports (.zip, .json, .txt)"}</span></div>
      ${local ? `<div class="url-ingest" style="margin-top:-24px;margin-bottom:40px"><input class="text-input" id="lpath" placeholder="…or index a folder in place: /path/to/videos (files are never moved or deleted)"><button class="button" id="lpath-go">Index folder</button></div>` : ""}`;
  const drop = $("#drop"), input = $("#file-input");
  drop.addEventListener("click", () => input.click());
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (e) => upload(e.dataTransfer.files));
  input.onchange = () => { upload(input.files); input.value = ""; };
  if (local) $("#lpath-go").addEventListener("click", async () => {
    try { const r = await api("/api/import/path", { method: "POST", body: { path: $("#lpath").value } }); flash(`Found ${r.found} videos · ${r.added} new`); }
    catch (e) { flash(`Error: ${e.message}`); }
    renderQueue();
  });
}
function flash(t) { const f = $("#flash"); if (f) f.textContent = t; }
async function upload(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  Array.from(files).forEach((f) => fd.append("files", f));
  flash(`Uploading ${files.length} file(s)…`);
  try {
    const r = await api("/api/import/file", { method: "POST", body: fd });
    flash(r.results.map((x) => x.error ? `${x.name}: ${x.error}` : `${x.name}: found ${x.found}, added ${x.added}${x.already_indexed ? `, ${x.already_indexed} already indexed` : ""}`).join(" · "));
  } catch (e) { flash(`Import failed: ${e.message}`); }
  renderQueue(); refreshStats();
}

const STAGE_LABEL = { new: "Queued · metadata", meta: "Queued · deep index", partial: "Thumbnail only", deep: "Done", dead: "Dead link", failed: "Failed" };
async function renderQueue() {
  const box = $("#queue");
  if (!box) return;
  let q;
  try { q = await api("/api/queue"); } catch (e) { return; }
  const c = q.counts;
  const waiting = (c.new || 0) + (q.deep_index ? c.meta || 0 : 0);
  const slot = $("#breaker-slot");
  if (slot) { const html = breakerBanner(q.breaker, STATS && STATS.downloader); if (slot.dataset.html !== html) { slot.innerHTML = html; slot.dataset.html = html; bindBreaker(renderQueue); } }
  const name = (it) => it.caption || (it.local_path ? it.local_path.split(/[\\/]/).pop() : null) || `tiktok_video_${it.id}`;
  const metaOf = (it) => [it.creator ? "@" + it.creator : (it.source === "local" ? "local file" : it.id), it.duration ? `${Math.round(it.duration)}s` : null, it.error && it.status !== "deep" ? it.error.slice(0, 80) : null].filter(Boolean).join(" · ");
  const row = (it, done) => {
    const p = it.stage ? Math.round(100 * (it.progress || 0)) : it.status === "deep" ? 100 : it.status === "partial" ? 60 : it.status === "meta" ? 10 : 0;
    return `<div class="queue-item ${done ? "status-done" : ""}"><div class="file-info"><span class="file-name" title="${esc(name(it))}">${esc(name(it))}</span><span class="file-meta" title="${esc(it.error || "")}">${esc(metaOf(it))}</span></div>
      <div class="status-tag">${esc(it.stage || STAGE_LABEL[it.status] || it.status)}</div>
      <div class="progress-bar-bg"><div class="progress-bar-fill" style="width: ${p}%;"></div></div>
      <div style="text-align: right; font-size: 11px;">${["dead", "failed", "partial"].includes(it.status) ? `<a data-retry="${esc(it.id)}" style="cursor:pointer">retry</a>` : `${p}%`}</div></div>`;
  };
  const active = q.active.map((x) => row(x, false)).join("");
  const pend = q.pending.filter((x) => !q.active.find((a) => a.id === x.id)).slice(0, 12).map((x) => row(x, false)).join("");
  const recent = q.recent.slice(0, 10).map((x) => row(x, true)).join("");
  box.innerHTML = `<div class="queue-header"><span>Processing Queue (${fmtInt(waiting)})</span>
      <span>Worker: ${esc(q.paused ? "paused" : q.worker)} · <a id="pause">${q.paused ? "Resume" : "Pause"}</a> · Status</span></div>
    <div style="font-size:11px;opacity:.6;margin:-8px 0 8px">new ${fmtInt(c.new || 0)} · metadata ${fmtInt(c.meta || 0)} · deep-indexed ${fmtInt(c.deep || 0)} · thumbnail-only ${fmtInt(c.partial || 0)} · dead ${fmtInt(c.dead || 0)} · failed ${fmtInt(c.failed || 0)}${q.deep_index ? "" : " · deep indexing is OFF (Settings)"}${q.deep_index && !q.video_downloads ? " · video downloads OFF: thumbnail-only indexing" : ""}</div>
    ${active}${pend}${recent || (!active && !pend ? `<div class="queue-item status-done"><div class="file-info"><span class="file-name">Queue is empty</span><span class="file-meta">Drop a TikTok export to begin</span></div></div>` : "")}`;
  $("#pause").addEventListener("click", async () => { await api("/api/worker", { method: "POST", body: { paused: !q.paused } }); renderQueue(); });
  $$("[data-retry]").forEach((a) => a.addEventListener("click", async () => { await api(`/api/videos/${encodeURIComponent(a.dataset.retry)}/retry`, { method: "POST" }); renderQueue(); }));
}

/* ------------------------------------------------------------------ EXPORT */
async function viewExport(params) {
  const [{ clusters }] = await Promise.all([api("/api/clusters"), refreshStats()]);
  app.innerHTML = `<section class="page-title-section"><h1 class="page-title">Export archive data</h1></section>
  <main class="ex-workspace">
    <section class="configuration">
      <div class="config-section"><span class="section-label">01. Select Scope</span>
        <div class="selection-grid">
          <div class="input-group"><label>Cluster</label><select id="ex-cluster"><option value="all">All Data (${fmtInt(STATS.live)})</option>
            ${clusters.filter((c) => c.size).map((c) => `<option value="${c.id}">${esc(c.name)} (${fmtInt(c.size)})</option>`).join("")}</select></div>
          <div class="input-group"><label>Date Range</label><select id="ex-date"><option value="all">All Time</option><option value="24h">Past 24 Hours</option><option value="7d">Past 7 Days</option><option value="30d">Past 30 Days</option></select></div>
        </div></div>
      <div class="config-section"><span class="section-label">02. Output Formats</span>
        <div class="format-options">
          <label class="checkbox-item"><input type="checkbox" id="fmt-json" checked><div><span class="checkbox-label">JSON Metadata</span><span class="checkbox-desc">Full transcript, OCR results, and creator attributes.</span></div></label>
          <label class="checkbox-item"><input type="checkbox" id="fmt-csv"><div><span class="checkbox-label">CSV Spreadsheet</span><span class="checkbox-desc">Flattened list of video links, creators, and timestamps.</span></div></label>
          <label class="checkbox-item disabled" title="Not implemented in this version"><input type="checkbox" disabled><div><span class="checkbox-label">MP4 Video Clips — not available in v0.1</span><span class="checkbox-desc">Personal use only. Recall deletes each video after indexing, so clips would require re-downloading; not built yet.</span></div></label>
        </div></div>
      <div class="config-section"><span class="section-label">03. Media Handling</span>
        <div class="format-options"><label class="checkbox-item disabled"><input type="checkbox" disabled><div><span class="checkbox-label">Burn-in Transcripts</span><span class="checkbox-desc">Requires MP4 clip export (not available in v0.1).</span></div></label></div></div>
    </section>
    <aside class="summary-sidebar"><div class="summary-card">
      <div class="summary-header">Export Summary</div>
      <div class="summary-row"><span class="label">Total items</span><span id="sum-count">—</span></div>
      <div class="summary-row"><span class="label">Scope</span><span id="sum-scope">—</span></div>
      <div class="summary-row"><span class="label">Time period</span><span id="sum-period">—</span></div>
      <div class="summary-row"><span class="label">Est. File Size</span><span id="sum-size">—</span></div>
      <div class="summary-row"><span class="label">Components</span><span id="sum-comp">—</span></div>
      <div class="export-action"><button class="btn-generate" id="gen">Generate Export</button>
        <p class="notice" id="ex-notice">Files are generated locally and downloaded by your browser. A copy is also saved to ${esc(STATS.disk.home)}/exports.</p></div>
    </div></aside></main>`;
  const upd = async () => {
    const fm = [$("#fmt-json").checked && "json", $("#fmt-csv").checked && "csv"].filter(Boolean);
    const cl = $("#ex-cluster"), dt = $("#ex-date");
    const e = await api(`/api/export/estimate?cluster=${cl.value}&date_range=${dt.value}&formats=${fm.join(",") || "json"}`);
    $("#sum-count").textContent = `${fmtInt(e.count)} videos`;
    $("#sum-scope").textContent = cl.value === "all" ? "All data" : `“${cl.options[cl.selectedIndex].text.replace(/ \(\d[\d,]*\)$/, "")}”`;
    $("#sum-period").textContent = dt.options[dt.selectedIndex].text;
    $("#sum-size").textContent = fm.length ? fmtBytes(e.bytes) : "—";
    $("#sum-comp").textContent = fm.map((x) => x.toUpperCase()).join(", ") || "none selected";
    $("#gen").disabled = !fm.length || !e.count;
  };
  $$("#ex-cluster, #ex-date, #fmt-json, #fmt-csv").forEach((el) => el.addEventListener("change", upd));
  await upd();
  $("#gen").addEventListener("click", async () => {
    const body = { cluster: $("#ex-cluster").value, date_range: $("#ex-date").value, save_copy: true };
    if ($("#fmt-json").checked) await download({ ...body, format: "json" });
    if ($("#fmt-csv").checked) await download({ ...body, format: "csv" });
  });
}
async function download(body) {
  const r = await fetch("/api/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const blob = await r.blob();
  const name = (r.headers.get("content-disposition") || "").match(/filename="([^"]+)"/)?.[1] || `recall-export.${body.format}`;
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); a.remove();
  const n = $("#ex-notice"); if (n) n.textContent = `Exported ${r.headers.get("x-recall-count")} items → ${name}${r.headers.get("x-recall-saved") ? ` (copy: ${r.headers.get("x-recall-saved")})` : ""}`;
}

/* ------------------------------------------------------------------ CLUSTERS */
async function viewClusters() {
  const [{ clusters }] = await Promise.all([api("/api/clusters"), refreshStats()]);
  const live = clusters.filter((c) => c.size || c.pinned);
  app.innerHTML = `<section class="page-title-section"><h1 class="page-title">Clusters</h1>
      <span class="search-meta">${fmtInt(live.length)} auto-clusters over ${fmtInt(STATS.live)} saves · <a id="recluster" style="cursor:pointer">Recluster now</a></span></section>
    <main class="workspace"><aside class="facets"><div class="facet-group"><span class="facet-label">Pinned</span>
        ${live.filter((c) => c.pinned).map((c) => `<a class="facet-item" href="${searchHash("", { cluster: [String(c.id)] })}">${esc(c.name)} (${c.size})</a>`).join("") || `<span class="facet-empty">Pin a cluster to keep it on top</span>`}</div>
      <div class="facet-group"><span class="facet-label">How clusters work</span><span class="facet-empty" style="display:block;line-height:1.5">KMeans over each save's MiniLM text + CLIP image embeddings. Names come from the cluster's most distinctive keywords and hashtags (c-TF-IDF). Renames and pins survive re-clustering.</span></div></aside>
      <section class="results"><div class="cluster-list">
      ${live.map((c) => `<div class="cluster-row" data-cid="${c.id}">
        <div><div class="cluster-name" data-name>${esc(c.name)}</div>
          <div class="cluster-sub">${fmtInt(c.size)} saves${c.pinned ? " · pinned" : ""}${c.custom_name ? ` · auto: ${esc(c.auto_name)}` : ""}</div>
          <div class="cluster-kw">${c.keywords.slice(0, 7).map((k) => esc(k)).join(" · ")}</div></div>
        <div class="cluster-thumbs">${c.samples.map((s) => `<div class="video-thumb" title="${esc(s.caption || "")}">${s.thumb ? `<img loading="lazy" src="${media(s.thumb)}">` : ""}</div>`).join("")}</div>
        <div class="cluster-actions"><button class="button" data-open>Open</button><button class="button" data-pin>${c.pinned ? "Unpin" : "Pin"}</button><button class="button" data-rename>Rename</button></div>
      </div>`).join("") || `<div class="empty-hero"><span class="big-number">0</span><div class="empty-message">Clusters appear once at least 4 saves are indexed.</div></div>`}
      </div></section></main>`;
  $("#recluster").addEventListener("click", async () => { $("#recluster").textContent = "Clustering…"; await api("/api/recluster", { method: "POST" }); viewClusters(); });
  $$(".cluster-row").forEach((row) => {
    const id = row.dataset.cid, c = clusters.find((x) => String(x.id) === id);
    $("[data-open]", row).addEventListener("click", () => goSearch("", { cluster: [id] }));
    $(".cluster-thumbs", row).addEventListener("click", () => goSearch("", { cluster: [id] }));
    $("[data-pin]", row).addEventListener("click", async () => { await api(`/api/clusters/${id}`, { method: "POST", body: { pinned: !c.pinned } }); viewClusters(); });
    $("[data-rename]", row).addEventListener("click", () => {
      const nm = $("[data-name]", row);
      nm.innerHTML = `<input value="${esc(c.name)}">`;
      const inp = $("input", nm); inp.focus(); inp.select();
      const save = async () => { await api(`/api/clusters/${id}`, { method: "POST", body: { name: inp.value } }); viewClusters(); };
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") viewClusters(); });
      inp.addEventListener("blur", save);
    });
  });
}

/* ------------------------------------------------------------------ SETTINGS */
const TOGGLES = [
  ["deep_index", "Deep Indexing (Wave 2)", "Temporarily download each save with yt-dlp to transcribe, OCR and embed it; the video file is deleted afterwards", "wave2_total"],
  ["transcripts", "Transcript Extraction", "Automatically convert speech to searchable text for every video (faster-whisper, int8, CPU)", "transcribe"],
  ["visual", "Visual Similarity Index", "CLIP ViT-B/32 embeddings of keyframes + thumbnail, for searches like “green bowl”", "visual"],
  ["ocr", "Optical Character Recognition (OCR)", "Identify and index text appearing on-screen (RapidOCR, one frame every 2 s)", "ocr"],
  ["sound", "Sound & Music Indexing", "Index the track / original-sound name from video metadata", null],
  ["text_embeddings", "Semantic Text Search", "MiniLM embeddings of captions, transcript lines and on-screen text, for meaning-based matches", "index"],
];
const BROWSER_OPTS = [["none", "None (don't use cookies)"], ["chrome", "Chrome"], ["edge", "Edge"], ["firefox", "Firefox"], ["safari", "Safari"], ["brave", "Brave"]];
async function viewSettings(params) {
  const [s] = await Promise.all([api("/api/settings"), refreshStats()]);
  const env = STATS.downloader || {}, br = STATS.download_breaker || {};
  const pr = STATS.partial_reasons || {};
  const st = STATS, ps = st.per_stage;
  const cost = (k) => k === null ? "No extra processing — read from the video's metadata" : k && ps[k] ? `Measured on this machine: ${ps[k].avg_s.toFixed(2)} s / video (avg of ${ps[k].n})` : "Not measured yet — no videos processed with this signal";
  app.innerHTML = `<section class="view-title"><h1>System Configuration</h1></section>
  <main class="workspace">
    <aside class="sidebar-nav">
      <div class="nav-group"><span class="nav-label">Preferences</span>
        <a class="nav-item active" data-jump="indexing">Indexing</a><a class="nav-item" data-jump="downloads">Video Downloads</a><a class="nav-item" data-jump="privacy">Privacy</a><a class="nav-item" data-jump="storage">Storage</a><a class="nav-item" data-jump="api">API Access</a></div>
      <div class="nav-group"><span class="nav-label">Support</span>
        <a class="nav-item" data-jump="docs">Documentation</a><a class="nav-item" data-jump="issue">Report Issue</a></div>
    </aside>
    <section class="settings-content">
      <div class="section-header" id="sec-indexing"><span>Indexing Engine Options</span><span>Auto-save: Enabled</span></div>
      ${TOGGLES.map(([k, name, desc, ck]) => `<div class="settings-row"><div class="setting-info"><span class="setting-name">${name}</span><span class="setting-desc">${desc}</span>
        <span class="setting-cost">${cost(ck)}</span></div><div class="toggle ${s[k] ? "on" : "off"}" data-key="${k}" role="switch" aria-checked="${!!s[k]}" title="${s[k] ? "On" : "Off"}"></div></div>`).join("")}
      <div class="settings-row"><div class="setting-info"><span class="setting-name">Whisper Model</span><span class="setting-desc">tiny ≈ 75 MB, fastest · base ≈ 145 MB, more accurate (default). Both run int8 on CPU.</span></div>
        <select class="small-select" id="whisper"><option value="tiny" ${s.whisper_model === "tiny" ? "selected" : ""}>tiny</option><option value="base" ${s.whisper_model === "base" ? "selected" : ""}>base</option></select></div>
      <div class="settings-row"><div class="setting-info"><span class="setting-name">Batch Size</span><span class="setting-desc">Videos per wave-2 batch. Models load one at a time per batch (Whisper → OCR → CLIP) to fit 8 GB RAM.</span></div>
        <select class="small-select" id="batch">${[1, 2, 4, 8].map((n) => `<option ${+s.batch_size === n ? "selected" : ""}>${n}</option>`).join("")}</select></div>

      <div class="section-header" style="margin-top: 48px;" id="sec-downloads"><span>Video Downloads (yt-dlp)</span><span>yt-dlp ${esc(env.yt_dlp || "not installed")} · ${env.curl_cffi ? `curl_cffi ${esc(env.curl_cffi)}` : "no curl_cffi"}</span></div>
      <div class="settings-row"><div class="setting-info"><span class="setting-name">Download Videos for Deep Index</span>
        <span class="setting-desc">On: each save is downloaded temporarily for transcript, on-screen text and keyframes. Off (or when a download fails): Recall indexes the TikTok thumbnail only — visual and thumbnail-text search still work, transcripts don't.</span>
        <span class="setting-cost">${fmtInt(STATS.counts.partial)} saves are thumbnail-only now${Object.keys(pr).length ? ` (${Object.entries(pr).map(([k, n]) => `${esc(k)}: ${fmtInt(n)}`).join(", ")})` : ""}</span></div>
        <div class="toggle ${s.video_downloads ? "on" : "off"}" data-key="video_downloads" role="switch" aria-checked="${!!s.video_downloads}"></div></div>
      <div class="settings-row"><div class="setting-info"><span class="setting-name">Use Cookies From My Browser</span>
        <span class="setting-desc">Passes your browser's TikTok cookies to yt-dlp (<span class="mono">--cookies-from-browser</span>), read locally and never sent anywhere except TikTok. Helps when TikTok blocks anonymous requests. Firefox works best; Chrome/Edge on Windows may need to be fully closed, and newer Chrome builds may not be readable at all.</span></div>
        <select class="small-select" id="cookies">${BROWSER_OPTS.map(([v, l]) => `<option value="${v}" ${s.cookies_browser === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div class="settings-row"><div class="setting-info"><span class="setting-name">Cookies File (optional)</span>
        <span class="setting-desc">Path to a Netscape-format cookies.txt exported from your browser. Overrides the browser setting above.</span></div>
        <input class="text-input settings-input" id="cookies-file" placeholder="e.g. C:\\Users\\me\\tiktok-cookies.txt" value="${esc(s.cookies_file || "")}"></div>
      <div class="settings-row"><div class="setting-info"><span class="setting-name">Browser Impersonation</span>
        <span class="setting-desc">Auto: try a plain desktop-browser request first, then curl_cffi TLS impersonation${env.impersonation ? "" : " (curl_cffi not installed here, so impersonation is unavailable)"}, then yt-dlp's own default — and remember whichever worked. Always: impersonate first. Never: no impersonation.</span></div>
        <select class="small-select" id="imp">${["auto", "always", "never"].map((v) => `<option ${s.ytdlp_impersonate === v ? "selected" : ""}>${v}</option>`).join("")}</select></div>
      <div class="settings-row"><div class="setting-info"><span class="setting-name">Custom User-Agent (optional)</span>
        <span class="setting-desc">If downloads keep failing, paste your real browser's User-Agent here (search “what is my user agent”). Empty = Recall's built-in desktop Chrome UA.</span></div>
        <input class="text-input settings-input" id="ua" placeholder="Mozilla/5.0 …" value="${esc(s.ytdlp_user_agent || "")}"></div>
      <div class="settings-row"><div class="setting-info"><span class="setting-name">Download Health</span>
        <span class="setting-desc">${br.open ? `<b>Paused</b> after ${fmtInt(br.consecutive)} consecutive failures [${esc(br.kind)}]: ${esc(br.message || "")}` : br.consecutive ? `${fmtInt(br.consecutive)} recent consecutive failure(s) [${esc(br.kind)}] — the queue pauses at ${fmtInt(br.threshold)}` : "OK — no recent download failures"}</span></div>
        <button class="button" id="dl-retry">Retry failed downloads</button></div>

      <div class="section-header" style="margin-top: 48px;" id="sec-storage"><span>Local Storage Statistics</span><span>${esc(st.disk.home)}</span></div>
      <div class="stat-grid">
        <div class="stat-card"><span class="stat-label">Total Indexed Items</span><span class="stat-value">${fmtInt(st.live)}</span><span class="stat-sub">${fmtInt(st.counts.deep)} deep · ${fmtInt(st.counts.partial)} thumbnail-only · ${fmtInt(st.counts.meta)} metadata · ${fmtInt(st.counts.dead)} dead</span></div>
        <div class="stat-card"><span class="stat-label">Disk Usage</span><span class="stat-value">${fmtBytes(st.disk.total_bytes)}</span><span class="stat-sub">DB ${fmtBytes(st.disk.db_bytes)} · thumbnails/keyframes ${fmtBytes(st.disk.media_bytes)}</span></div>
        ${Object.entries(st.coverage).filter(([k]) => ["Transcripts", "On-screen text", "Visual", "Sound"].includes(k)).map(([k, v]) => `<div class="stat-card"><span class="stat-label">${esc(k)}</span><span class="stat-value">${v.pct}%</span><span class="stat-sub">${fmtInt(v.videos)} of ${fmtInt(st.live)} saves</span></div>`).join("")}
        <div class="stat-card"><span class="stat-label">Search Latency</span><span class="stat-value">${st.latency.avg_ms != null ? st.latency.avg_ms.toFixed(0) + " ms" : "—"}</span><span class="stat-sub">${st.latency.n ? `average of last ${st.latency.n} searches (server-side)` : "no searches yet"}</span></div>
        <div class="stat-card"><span class="stat-label">Clusters</span><span class="stat-value">${fmtInt(st.clusters)}</span><span class="stat-sub">auto topic clusters</span></div>
      </div>
      <div class="settings-row" style="margin-top: 24px;"><div class="setting-info"><span class="setting-name">Encrypted Local Storage</span>
        <span class="setting-desc">Not available in this build: SQLCipher is not bundled (it needs a native build on Windows). Use full-disk encryption instead — BitLocker / FileVault / LUKS.</span></div>
        <div class="toggle off disabled" title="Not available"></div></div>

      <div class="section-header" style="margin-top: 48px;" id="sec-privacy"><span>Privacy</span></div>
      <div class="prose"><p>The server listens on ${esc(location.host)} only. No accounts, analytics or telemetry.</p>
        <p>Network use: TikTok oEmbed (caption, creator, thumbnail) for each saved link, rate-limited to 1 request/second; yt-dlp downloads when Deep Indexing is on (deleted after processing); one-time model downloads from Hugging Face on first run.</p></div>

      <div class="section-header" style="margin-top: 48px;" id="sec-api"><span>API Access (local)</span></div>
      <ul class="api-list mono">
        <li><span>GET /api/search?q=&amp;f=</span><span>hybrid search with why-matched signals</span></li>
        <li><span>GET /api/stats</span><span>counts, coverage, disk, latency</span></li>
        <li><span>GET /api/queue</span><span>ingestion queue and progress</span></li>
        <li><span>POST /api/import/file | links | path</span><span>add saves</span></li>
        <li><span>GET /api/clusters</span><span>auto clusters</span></li>
        <li><span>POST /api/export</span><span>JSON / CSV export</span></li></ul>

      <div class="section-header" style="margin-top: 48px;" id="sec-docs"><span>Documentation</span></div>
      <div class="prose"><p>See README.md in the Recall folder for install, the TikTok export walkthrough, per-signal costs and known limits.</p></div>
      <div class="section-header" style="margin-top: 32px;" id="sec-issue"><span>Report Issue</span></div>
      <div class="prose"><p>Recall sends nothing automatically. To report a problem, copy the server console output (it never contains your saves' content unless you paste it).</p></div>

      <div class="danger-zone"><div class="section-header" style="color: #666;"><span>Maintenance</span></div>
        <div class="settings-row" style="border: none;"><div class="setting-info"><span class="setting-name">Purge Local Index</span>
          <span class="setting-desc">Remove all local data (database, thumbnails, keyframes). This action is irreversible. Type <b>PURGE</b> to enable.</span></div>
          <input class="text-input" id="purge-confirm" placeholder="Type PURGE" style="max-width:130px;flex:none">
          <button class="button" id="purge" disabled>Clear Data</button></div></div>
    </section></main>`;
  $$(".toggle[data-key]").forEach((t) => t.addEventListener("click", async () => {
    const on = !t.classList.contains("on");
    await api("/api/settings", { method: "POST", body: { [t.dataset.key]: on } });
    t.classList.toggle("on", on); t.classList.toggle("off", !on); t.title = on ? "On" : "Off";
  }));
  $("#whisper").addEventListener("change", (e) => api("/api/settings", { method: "POST", body: { whisper_model: e.target.value } }));
  $("#batch").addEventListener("change", (e) => api("/api/settings", { method: "POST", body: { batch_size: +e.target.value } }));
  $("#cookies").addEventListener("change", (e) => api("/api/settings", { method: "POST", body: { cookies_browser: e.target.value } }));
  $("#imp").addEventListener("change", (e) => api("/api/settings", { method: "POST", body: { ytdlp_impersonate: e.target.value } }));
  $("#cookies-file").addEventListener("change", (e) => api("/api/settings", { method: "POST", body: { cookies_file: e.target.value.trim() } }));
  $("#ua").addEventListener("change", (e) => api("/api/settings", { method: "POST", body: { ytdlp_user_agent: e.target.value.trim() } }));
  $("#dl-retry").addEventListener("click", async (e) => { const r = await api("/api/downloads/retry", { method: "POST" }); e.target.textContent = `Re-queued ${r.requeued}`; refreshStats(); });
  if (params && params.get("sec")) setTimeout(() => $(`#sec-${params.get("sec")}`)?.scrollIntoView({ block: "start" }), 0);
  $$("[data-jump]").forEach((a) => a.addEventListener("click", () => {
    $$("[data-jump]").forEach((x) => x.classList.toggle("active", x === a));
    $(`#sec-${a.dataset.jump}`).scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  const pc = $("#purge-confirm"), pb = $("#purge");
  pc.addEventListener("input", () => (pb.disabled = pc.value !== "PURGE"));
  pb.addEventListener("click", async () => {
    if (pc.value !== "PURGE") return;
    await api("/api/purge", { method: "POST", body: { confirm: "PURGE" } });
    pc.value = ""; pb.disabled = true; viewSettings();
  });
}

refreshStats();
route();
