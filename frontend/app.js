const state = { org: null, project: null, rollup: null, hideRTR: false, featureCtx: null, highlightTag: "ADNOC" };

const $ = (sel, root = document) => root.querySelector(sel);
const pct = (v) => `${Math.round(Number(v || 0))}%`;

function setStatus(html, kind = "info") {
  $("#status").innerHTML = html ? `<div class="${kind}">${html}</div>` : "";
}

function statusLabel(s) {
  return s === "no-data" ? "no data" : s.replace("-", " ");
}
function confColor(node) {
  if (!node.hasData) return "var(--gray)";
  if (node.confidence >= 80) return "var(--green)";
  if (node.confidence >= 50) return "var(--yellow)";
  return "var(--red)";
}

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
function workItemUrl(id) {
  if (!state.org || !state.project) return "#";
  return `https://dev.azure.com/${state.org}/${state.project}/_workitems/edit/${id}`;
}
function escapeHtml(str) {
  return String(str || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function loadConfig() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    state.org = cfg.org;
    state.project = cfg.project;
  } catch { /* ignore */ }
}

async function loadQuarters() {
  try {
    const data = await (await fetch("/api/quarters")).json();
    const sel = $("#quarter-select");
    sel.innerHTML = data.quarters
      .map((q) => `<option value="${q.value}">${q.label}</option>`)
      .join("");
    sel.value = data.current;
  } catch { /* ignore */ }
}

async function loadRollup() {
  const btn = $("#refresh");
  const quarterEl = $("#quarter-select");
  const quarter = quarterEl ? quarterEl.value : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Loading…';
  }
  setStatus("A browser window may open for Azure DevOps sign-in (MFA). Complete it to continue.", "info");
  try {
    const res = await fetch(`/api/rollup?quarter=${encodeURIComponent(quarter)}`);
    if (res.status === 401) {
      const redirect = res.headers.get("X-Auth-Redirect");
      if (redirect === "device") { startDeviceLogin(); return; }
      if (redirect) { window.location.href = redirect; return; }
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `Request failed (${res.status})`);
    }
    state.rollup = await res.json();
    state.highlightTag = state.rollup.highlightTag || "ADNOC";
    renderScope();
    renderUnmapped(state.rollup);
    showPortfolios();
  } catch (e) {
    setStatus(`Failed to load data:\n${e.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Refresh";
    }
  }
}

// Device-code sign-in: show the code in the browser so the first sign-in can be
// completed from any device, not just the server console.
let deviceLoginPoll = null;

function renderDevicePrompt(p) {
  const uri = p.verificationUri || "https://microsoft.com/devicelogin";
  setStatus(
    `<div class="device-login">
       <strong>Azure DevOps sign-in required</strong>
       <p>On any device, open
         <a href="${escapeHtml(uri)}" target="_blank" rel="noopener">${escapeHtml(uri)}</a>
         and enter this code:</p>
       <div class="device-code">${escapeHtml(p.userCode || "")}</div>
       <p class="muted small">Complete your Microsoft sign-in (MFA). This page will
         continue automatically once you're signed in.</p>
     </div>`,
    "info"
  );
}

async function pollDeviceLogin() {
  try {
    const s = await (await fetch("/api/device-login/status")).json();
    if (s.status === "authenticated") {
      clearInterval(deviceLoginPoll); deviceLoginPoll = null;
      setStatus("Signed in — loading data…", "info");
      loadRollup();
    } else if (s.status === "error") {
      clearInterval(deviceLoginPoll); deviceLoginPoll = null;
      setStatus(`Sign-in failed:\n${s.error || "unknown error"}`, "error");
    } else if (s.status === "pending" && s.prompt) {
      renderDevicePrompt(s.prompt);
    }
  } catch { /* keep polling */ }
}

async function startDeviceLogin() {
  if (deviceLoginPoll) return; // already in progress
  setStatus("Starting Azure DevOps sign-in…", "info");
  try {
    const s = await (await fetch("/api/device-login/start", { method: "POST" })).json();
    if (s.status === "authenticated") { loadRollup(); return; }
    if (s.status === "error") { setStatus(`Sign-in failed:\n${s.error || "unknown error"}`, "error"); return; }
    if (s.prompt) renderDevicePrompt(s.prompt);
    deviceLoginPoll = setInterval(pollDeviceLogin, 3000);
  } catch (e) {
    setStatus(`Could not start sign-in:\n${e.message}`, "error");
  }
}

function renderScope() {
  const d = state.rollup;
  const el = $("#scope");
  if (!el) return; // element removed from DOM — nothing to render
  el.textContent =
    `${d.quarter.label} · ${d.window.monthsRemaining} months remaining · ` +
    `velocity from last ${d.window.velocityWindowDays} days (${d.window.velocityBasis})`;
}

function renderTotals(node, children) {
  const el = $("#totals");
  el.classList.remove("hidden");
  $("#conf-note").classList.remove("hidden");
  const kids = children && children.length ? children : [node];
  const green = kids.filter((c) => c.status === "on-track").length;
  const yellow = kids.filter((c) => c.status === "at-risk").length;
  const red = kids.filter((c) => c.status === "off-track").length;
  const scope = node.name && node.level !== "all" ? escapeHtml(node.name) : "Overall";
  el.innerHTML = `
    <div class="tstat"><div class="n">${pct(node.pctComplete)}</div><div class="muted small">${scope} complete</div></div>
    <div class="tstat"><div class="n">${node.confidence}%</div><div class="muted small">confidence</div></div>
    <div class="tstat"><div class="n">${node.totalPoints}</div><div class="muted small">committed pts</div></div>
    <div class="tstat"><div class="n">${node.remainingEffort}</div><div class="muted small">remaining open pts</div></div>
    <div class="tstat"><div class="n">${node.predictedRemainingCapacity}</div><div class="muted small">remaining capacity pts</div></div>
    <div class="tstat green"><div class="n">${green}</div><div class="muted small">on track</div></div>
    <div class="tstat yellow"><div class="n">${yellow}</div><div class="muted small">at risk</div></div>
    <div class="tstat red"><div class="n">${red}</div><div class="muted small">off track</div></div>
  `;
}

function renderUnmapped(d) {
  const list = d.unmappedTeams || [];
  if (!list.length) return;
  const top = list.slice(0, 12).map((u) => `${escapeHtml(u.team)} (${u.features})`).join(", ");
  const more = list.length > 12 ? ` +${list.length - 12} more` : "";
  const total = list.reduce((a, u) => a + u.features, 0);
  setStatus(
    `<strong>${list.length} Dev squad(s)</strong> with ${total} features this quarter are not mapped to a squad and are excluded. ` +
    `Add their names under the right product squad in <code>backend/squads.json</code> to include them.<br><span class="muted">${top}${more}</span>`,
    "warn"
  );
}

/* ---- navigation ---- */

function setBreadcrumb(items) {
  const bc = $("#breadcrumb");
  bc.classList.remove("hidden");
  bc.innerHTML = "";
  items.forEach((it, i) => {
    if (i > 0) {
      const sep = document.createElement("span");
      sep.className = "crumb-sep";
      sep.textContent = "›";
      bc.appendChild(sep);
    }
    const b = document.createElement("button");
    b.className = "crumb" + (it.onClick ? "" : " current");
    b.textContent = it.label;
    if (it.onClick) b.addEventListener("click", it.onClick);
    bc.appendChild(b);
  });
}

const rootCrumb = () => ({ label: "All portfolios", onClick: showPortfolios });

function showPortfolios() {
  renderTotals(state.rollup.totals, state.rollup.portfolios);
  setBreadcrumb([{ label: "All portfolios", onClick: null }]);
  renderTiles(state.rollup.portfolios, {
    trail: [rootCrumb()],
    childLabel: "Product squads",
    onDrill: (node) => showProducts(node),
  });
}

function showProducts(pf) {
  renderTotals(pf, pf.products);
  const trail = [rootCrumb(), { label: pf.name, onClick: () => showProducts(pf) }];
  setBreadcrumb([rootCrumb(), { label: pf.name, onClick: null }]);
  renderTiles(pf.products, {
    trail,
    childLabel: "Dev squads",
    onDrill: (node) => showTeams(node, pf),
  });
}

function showTeams(product, pf) {
  renderTotals(product, product.teams);
  const trail = [
    rootCrumb(),
    { label: pf.name, onClick: () => showProducts(pf) },
    { label: product.name, onClick: () => showTeams(product, pf) },
  ];
  setBreadcrumb([
    rootCrumb(),
    { label: pf.name, onClick: () => showProducts(pf) },
    { label: product.name, onClick: null },
  ]);
  renderTiles(product.teams, { trail, teamLevel: true });
}

function collectFeatures(node) {
  if (node.level === "team") return node.features || [];
  if (node.level === "product")
    return (node.features || []).concat((node.teams || []).flatMap((t) => t.features || []));
  if (node.level === "portfolio")
    return (node.products || []).flatMap((p) =>
      (p.features || []).concat((p.teams || []).flatMap((t) => t.features || []))
    );
  return [];
}

function showFeaturesView(node, trailToNode) {
  state.featureCtx = { node, trailToNode };
  renderTotals(node, node.products || node.teams || null);
  setBreadcrumb(trailToNode.map((c, i) =>
    i === trailToNode.length - 1 ? { label: c.label, onClick: null } : c
  ).concat([{ label: "Features", onClick: null }]));
  renderFeatures(node, collectFeatures(node));
}

/* ---- rendering ---- */

function renderTiles(nodes, opts) {
  state.featureCtx = null;
  const view = $("#view");
  view.className = "view";
  view.innerHTML = "";
  if (!nodes || !nodes.length) {
    view.innerHTML = '<div class="dd-empty">Nothing mapped at this level yet. Edit backend/squads.json to add Dev squads.</div>';
    return;
  }
  const tpl = $("#tile");
  for (const node of nodes) {
    const frag = tpl.content.cloneNode(true);
    fillTile(frag, node, opts);
    view.appendChild(frag);
  }
}

function fillTile(frag, node, opts) {
  const tile = $(".tile", frag);
  tile.classList.add(node.status);
  if (node.empty) tile.classList.add("empty");
  $(".tile-name", frag).textContent = node.name;
  const lead = $(".tile-lead", frag);
  lead.textContent = node.lead ? `Lead: ${node.lead}` : (opts.teamLevel ? "Dev squad" : "");

  const badge = $(".badge", frag);
  badge.classList.add(node.status);
  badge.textContent = statusLabel(node.status);

  $(".pct", frag).textContent = node.hasData ? pct(node.pctComplete) : "—";
  const conf = $(".conf", frag);
  conf.textContent = node.hasData ? `${node.confidence}%` : "—";
  conf.style.color = confColor(node);

  $(".progress-fill", frag).style.width = `${Math.min(node.pctComplete, 100)}%`;

  const sub = $(".sub-alert", frag);
  const off = node.subOffTrack || 0;
  const atr = node.subAtRisk || 0;
  if (sub && (off || atr)) {
    const parts = [];
    if (off) parts.push(`<span class="sa-off">${off} off track</span>`);
    if (atr) parts.push(`<span class="sa-at">${atr} at risk</span>`);
    sub.innerHTML = `<span class="sa-icon">⚠</span> dev squads: ${parts.join(" · ")}`;
    sub.classList.remove("hidden");
    tile.classList.add("has-sub-alert");
  }

  $(".effort-label", frag).textContent =
    `Effort ${node.totalPoints} pts · ${node.remainingEffort} pts open`;
  $(".cap-label", frag).textContent =
    `Capacity ${node.predictedRemainingCapacity} pts left / ${node.predictedQuarterCapacity} pts qtr`;
  const max = Math.max(node.totalPoints, node.predictedQuarterCapacity, 1);
  $(".cap-fill", frag).style.width = `${(node.predictedQuarterCapacity / max) * 100}%`;
  $(".cap-marker", frag).style.left = `${(node.totalPoints / max) * 100}%`;

  const vsrc = node.velocitySource ? ` (${node.velocitySource})` : "";
  if (node.empty) {
    const next = node.nextQuarter
      ? `Next commitment: <strong>${escapeHtml(node.nextQuarter.label)}</strong>`
      : "No upcoming commitment in view";
    $(".vel", frag).innerHTML =
      `No commitment this quarter · ${next} · velocity ${node.velocityPerMonth} pts/mo`;
  } else {
    $(".vel", frag).textContent =
      `Velocity ${node.velocityPerMonth} pts/mo${vsrc} · ${node.featureCount} features · ${node.completedItems}/${node.totalItems} items`;
  }

  const drill = $(".drill", frag);
  const feats = $(".feats", frag);
  const trailToNode = opts.trail.concat([{
    label: node.name,
    onClick: opts.teamLevel ? null : () => opts.onDrill(node),
  }]);

  if (opts.teamLevel) {
    drill.classList.add("hidden");
  } else {
    drill.textContent = opts.childLabel;
    drill.addEventListener("click", () => opts.onDrill(node));
    const title = $(".tile-title-wrap", frag);
    title.style.cursor = "pointer";
    title.addEventListener("click", () => opts.onDrill(node));
  }
  if (node.empty) feats.classList.add("hidden");
  else feats.addEventListener("click", () => showFeaturesView(node, trailToNode));
}

function mergeFeatures(feats) {
  // The same feature can appear once per contributing squad at product/portfolio
  // level. Collapse to one entry per feature id, unioning the "mine" (in-scope)
  // flag across slices, and recompute the in-scope PBI counts.
  const byId = new Map();
  for (const f of feats) {
    let e = byId.get(f.id);
    if (!e) {
      byId.set(f.id, { ...f, pbis: (f.pbis || []).map((p) => ({ ...p })) });
      continue;
    }
    const mineIds = new Set((f.pbis || []).filter((p) => p.mine).map((p) => p.id));
    for (const p of e.pbis) if (mineIds.has(p.id)) p.mine = true;
  }
  const merged = [...byId.values()];
  for (const e of merged) {
    const mine = e.pbis.filter((p) => p.mine);
    e.totalChildren = mine.length;
    e.completedChildren = mine.filter((p) => p.completed).length;
    e.percentComplete = mine.length
      ? Math.round((e.completedChildren / mine.length) * 100)
      : e.percentComplete;
  }
  return merged;
}

// States hidden by the "Hide Ready to Release / Removed" toggle. Removed/Cut are
// already excluded by the backend, but we filter defensively so the logic is
// symmetric with Ready to Release.
const HIDDEN_STATES = new Set(["ready to release", "removed", "cut"]);
function isHiddenState(x) {
  return HIDDEN_STATES.has((x.state || "").toLowerCase());
}

// A feature owned by another squad (assignedHigher) contributes only its in-scope
// PBIs here. When those are all Ready to Release / Removed there is nothing left
// to track, so it should be hidden alongside features that are themselves hidden.
function isFeatureHidden(f) {
  if (isHiddenState(f)) return true;
  if (f.assignedHigher) {
    const mine = (f.pbis || []).filter((p) => p.mine);
    if (mine.length && mine.every(isHiddenState)) return true;
  }
  return false;
}

function renderFeatures(node, feats) {
  const view = $("#view");
  view.className = "view features-view";
  feats = mergeFeatures(feats);
  if (state.hideRTR) {
    feats = feats.filter((f) => !isFeatureHidden(f));
  }
  if (!feats.length) {
    const why = state.hideRTR ? " (Ready to Release / Removed items are hidden)" : "";
    view.innerHTML = `<div class="dd-empty">No features for ${escapeHtml(node.name)} this quarter${why}.</div>`;
    return;
  }
  const scopeWord =
    node.level === "team" ? "this squad"
    : node.level === "product" ? "this product"
    : node.level === "portfolio" ? "this portfolio"
    : "scope";
  const complete = feats.filter((f) => f.percentComplete >= 100).length;
  const avg = Math.round(feats.reduce((a, f) => a + f.percentComplete, 0) / feats.length);
  const showTeam = node.level !== "team";
  const totalPbis = feats.reduce((a, f) => a + (f.pbis ? f.pbis.filter((p) => p.mine).length : 0), 0);
  const expandAll =
    totalPbis ? `<button class="link" id="expand-all">Expand all PBIs</button>` : "";
  const head =
    `<div class="features-summary"><span class="muted small">${escapeHtml(node.name)} · ` +
    `${feats.length} features · ${complete} complete · avg ${avg}% complete` +
    ` · ${totalPbis} PBIs in ${scopeWord}` +
    `</span>${expandAll}</div>`;
  // Group features by their parent epic where available.
  const ordered = feats.slice().sort((a, b) => a.percentComplete - b.percentComplete);
  const groups = new Map();
  for (const f of ordered) {
    const key = f.epicTitle || "— No epic —";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  }
  const keys = [...groups.keys()].sort((a, b) => {
    const na = a.startsWith("—"), nb = b.startsWith("—");
    if (na !== nb) return na ? 1 : -1;
    return a.toLowerCase().localeCompare(b.toLowerCase());
  });
  let html = head;
  for (const key of keys) {
    const list = groups.get(key);
    const epicId = list[0].epicId;
    const link = epicId
      ? `<a href="${workItemUrl(epicId)}" target="_blank" rel="noopener">#${epicId}</a> `
      : "";
    html +=
      `<div class="epic-group"><div class="epic-head">${link}${escapeHtml(key)} ` +
      `<span class="muted small">(${list.length})</span></div>` +
      list.map((f) => featureRow(f, showTeam, scopeWord)).join("") +
      `</div>`;
  }
  view.innerHTML = html;

  view.querySelectorAll(".feature-row").forEach((row) => {
    const clicker = row.querySelector(".feature-head-click");
    const list = row.querySelector(".pbi-list");
    if (clicker && list) {
      clicker.addEventListener("click", () => {
        list.classList.toggle("hidden");
        row.classList.toggle("open");
      });
    }
  });

  const ea = view.querySelector("#expand-all");
  if (ea) {
    let open = false;
    ea.addEventListener("click", () => {
      open = !open;
      view.querySelectorAll(".pbi-list").forEach((l) => l.classList.toggle("hidden", !open));
      view.querySelectorAll(".feature-row").forEach((r) => r.classList.toggle("open", open));
      ea.textContent = open ? "Collapse all PBIs" : "Expand all PBIs";
    });
  }
}

function featureRow(f, showTeam, scopeWord) {
  const p = Math.max(0, Math.min(100, f.percentComplete));
  const cls = p >= 100 ? "on-track" : p >= 50 ? "at-risk" : "off-track";
  const totalP = f.totalPbis != null ? f.totalPbis : (f.pbis ? f.pbis.length : 0);
  const squadCount = f.totalChildren
    ? `${f.completedChildren}/${f.totalChildren} PBIs in ${scopeWord}`
    : `no PBIs in ${scopeWord}`;
  const ofFeature = totalP > f.totalChildren ? ` · ${totalP} on the feature` : "";
  const owner = ` · owned by ${escapeHtml(f.owner || "Unassigned")}`;
  const ownerSquad = f.team && f.team !== "Unassigned" ? f.team : "another squad";
  const higher = f.assignedHigher
    ? `<span class="tag higher" title="This feature sits on ${escapeHtml(ownerSquad)}; only its in-scope PBIs are counted here">↑ ${escapeHtml(ownerSquad)}</span>`
    : "";
  const team = showTeam && f.team ? `<span class="feature-team">${escapeHtml(f.team)}</span>` : "";
  const hlChip = f.highlight
    ? `<span class="tag hl-tag" title="Tagged ${escapeHtml(state.highlightTag)} (or under a parent that is)">${escapeHtml(state.highlightTag)}</span>`
    : "";
  const visiblePbis = state.hideRTR
    ? (f.pbis || []).filter((p) => !isHiddenState(p))
    : (f.pbis || []);
  const hasPbis = visiblePbis.length > 0;
  const caret = hasPbis ? '<span class="caret">▸</span>' : '<span class="caret-empty"></span>';
  const note =
    f.assignedHigher
      ? `<div class="pbi-note">This feature sits on <strong>${escapeHtml(ownerSquad)}</strong> (owned by ${escapeHtml(f.owner || "Unassigned")}), outside ${escapeHtml(scopeWord)}. Only the PBIs marked in blue belong to ${escapeHtml(scopeWord)}; others are shown for context.</div>`
      : "";
  const pbiList = hasPbis
    ? `<div class="pbi-list hidden">${note}${visiblePbis.map(pbiRow).join("")}</div>`
    : "";
  return `
    <div class="feature-row ${f.assignedHigher ? "is-higher" : ""} ${f.highlight ? "hl" : ""}">
      <div class="feature-head-click">
        <div class="feature-top">
          ${caret}
          <a href="${workItemUrl(f.id)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">#${f.id}</a>
          <span class="feature-title">${escapeHtml(f.title)}</span>
          ${hlChip}${higher}${team}
          <span class="feature-pct">${Math.round(p)}%</span>
        </div>
        <div class="progress feature-progress ${cls}"><div class="progress-fill" style="width:${p}%"></div></div>
        <div class="muted small feature-meta">${escapeHtml(f.state)} · ${squadCount}${ofFeature}${owner}</div>
      </div>
      ${pbiList}
    </div>`;
}

function pbiRow(p) {
  const meta = [
    escapeHtml(p.state),
    p.points ? `${p.points} pts` : "",
    p.assignedTo ? escapeHtml(p.assignedTo) : "",
  ].filter(Boolean).join(" · ");
  let teamCls = "pbi-team";
  let teamTitle = "Not part of the current structure";
  if (p.mine) { teamCls += " mine"; teamTitle = "This squad"; }
  else if (p.mapped) { teamCls += " mapped"; teamTitle = "Dev squad in the current breakdown"; }
  const teamChip = `<span class="${teamCls}" title="${teamTitle}">${escapeHtml(p.team || "Unassigned")}</span>`;
  const hlChip = p.highlight
    ? `<span class="pbi-badge hl-tag">${escapeHtml(state.highlightTag)}</span>`
    : "";
  const lateChip = p.iterationAfterQuarter
    ? `<span class="pbi-badge late-iter" title="Scheduled on iteration '${escapeHtml(p.iterationName || "")}', which starts after this delivery quarter ends">⚠ iteration after quarter</span>`
    : "";
  return `
    <div class="pbi-row ${p.completed ? "done" : ""} ${p.mine ? "mine" : "notmine"} ${p.highlight ? "hl" : ""} ${p.iterationAfterQuarter ? "late-iter" : ""}">
      <span class="pbi-check">${p.completed ? "✓" : "○"}</span>
      <a href="${workItemUrl(p.id)}" target="_blank" rel="noopener">#${p.id}</a>
      <span class="pbi-title">${escapeHtml(p.title)}</span>
      ${hlChip}${lateChip}${teamChip}
      <span class="pbi-meta muted small">${meta}</span>
    </div>`;
}

$("#refresh").addEventListener("click", loadRollup);
$("#quarter-select").addEventListener("change", () => {
  if (state.rollup) loadRollup();
});
$("#hide-rtr").addEventListener("change", (e) => {
  state.hideRTR = e.target.checked;
  if (state.featureCtx) showFeaturesView(state.featureCtx.node, state.featureCtx.trailToNode);
});

// In per-user OAuth mode, show who's signed in (and a sign-in/out link). In
// interactive mode the auth box stays hidden and nothing changes.
async function loadAuth() {
  try {
    const info = await (await fetch("/auth/me")).json();
    if (info.authMode !== "oauth") return;
    const box = $("#auth-box");
    box.classList.remove("hidden");
    if (info.authenticated) {
      const who = escapeHtml(info.user?.name || info.user?.username || "signed in");
      box.innerHTML = `<span class="muted small">${who}</span> · <a href="/auth/logout">Sign out</a>`;
    } else {
      box.innerHTML = `<a href="/auth/login">Sign in</a>`;
    }
  } catch { /* ignore */ }
}

loadConfig();
loadQuarters();
loadAuth();
