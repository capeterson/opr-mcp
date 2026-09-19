// Army & Unit Browser (ui://opr/browser).
//
// Backs three tools -- list_armies, list_units, lookup_unit -- in one
// resource so navigating armies -> units -> a unit card needs no extra
// tool call to the model: list_units / lookup_unit are called directly
// from here via app.callServerTool(). The initial screen is whichever
// of the three tools opened this view, discriminated by the
// `structuredContent.view` field ("armies" | "units" | "unit") the
// server sets in opr_mcp/server/tools.py.
//
// The game-system toggle on the armies screen is pure client-side
// filtering: list_armies already returns every army with its system in
// one call, so no tool round-trip is needed to switch it.

const { escapeHtml, viewEl, createApp, callServerTool } = window.OprShell;

const state = {
  armiesPayload: null,   // last {view:"armies", armies:[...]}
  unitsPayload: null,    // last {view:"units", army, game_system, units:[...]}
  systemFilter: null,    // null = "All"
  sortKey: "base_points",
  sortDir: "asc",
};

function fmtPts(n) {
  return n === null || n === undefined ? "—" : `${n}`;
}

function fmtStat(n) {
  return n === null || n === undefined ? "—" : String(n);
}

// -- Armies screen ----------------------------------------------------

function renderArmies(payload) {
  state.armiesPayload = payload;
  const armies = payload.armies || [];
  const systems = [...new Set(armies.map((a) => a.game_system).filter(Boolean))].sort();

  const rows = armies
    .filter((a) => !state.systemFilter || a.game_system === state.systemFilter)
    .sort((a, b) => (a.army || "").localeCompare(b.army || ""));

  const toggle = systems.length > 1
    ? `<div class="opr-toggle" id="opr-system-toggle">
        <button data-system="" aria-pressed="${!state.systemFilter}">All</button>
        ${systems.map((s) => `<button data-system="${escapeHtml(s)}" aria-pressed="${state.systemFilter === s}">${escapeHtml(s)}</button>`).join("")}
      </div>`
    : "";

  viewEl.innerHTML = `
    <div class="opr-toolbar">${toggle}<span class="opr-muted">${rows.length} armies</span></div>
    ${rows.length === 0 ? "<div class=\"opr-empty\">No armies indexed yet.</div>" : `
    <table class="opr-table">
      <thead><tr><th>Army</th><th>System</th><th>Units</th><th>Docs</th></tr></thead>
      <tbody>
        ${rows.map((a, i) => `
          <tr data-idx="${i}">
            <td>${escapeHtml(a.army)}</td>
            <td><span class="opr-pill">${escapeHtml(a.game_system || "—")}</span></td>
            <td>${fmtStat(a.unit_count)}</td>
            <td>${fmtStat(a.document_count)}</td>
          </tr>`).join("")}
      </tbody>
    </table>`}
  `;

  viewEl.querySelectorAll("#opr-system-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.systemFilter = btn.dataset.system || null;
      renderArmies(state.armiesPayload);
    });
  });

  viewEl.querySelectorAll("tbody tr").forEach((tr) => {
    tr.addEventListener("click", async () => {
      const a = rows[Number(tr.dataset.idx)];
      try {
        const result = await callServerTool(window.OprApp, "list_units", {
          army: a.army,
          game_system: a.game_system || undefined,
        });
        renderUnits(result);
      } catch {
        // callServerTool already surfaced the error banner.
      }
    });
  });
}

// -- Units screen -------------------------------------------------------

const UNIT_COLUMNS = [
  { key: "name", label: "Name" },
  { key: "base_points", label: "Points" },
  { key: "qty", label: "Qty" },
  { key: "quality", label: "Quality" },
  { key: "defense", label: "Defense" },
];

function renderUnits(payload) {
  state.unitsPayload = payload;
  const units = [...(payload.units || [])];
  const { sortKey, sortDir } = state;
  units.sort((a, b) => {
    const av = a[sortKey];
    const bv = b[sortKey];
    if (av === bv) return 0;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    const cmp = typeof av === "number" ? av - bv : String(av).localeCompare(String(bv));
    return sortDir === "asc" ? cmp : -cmp;
  });

  viewEl.innerHTML = `
    <div class="opr-breadcrumbs">
      <button id="opr-back-armies">Armies</button> ›
      <span>${escapeHtml(payload.army)}</span>
      ${payload.game_system ? `<span class="opr-pill">${escapeHtml(payload.game_system)}</span>` : ""}
    </div>
    ${units.length === 0 ? "<div class=\"opr-empty\">No units found.</div>" : `
    <table class="opr-table">
      <thead><tr>${UNIT_COLUMNS.map((c) => `<th data-key="${c.key}">${c.label}${sortKey === c.key ? (sortDir === "asc" ? " ↑" : " ↓") : ""}</th>`).join("")}</tr></thead>
      <tbody>
        ${units.map((u, i) => `
          <tr data-idx="${i}">
            <td>${escapeHtml(u.name)}</td>
            <td>${fmtPts(u.base_points)}</td>
            <td>${fmtStat(u.qty)}</td>
            <td>${escapeHtml(u.quality ?? "—")}</td>
            <td>${escapeHtml(u.defense ?? "—")}</td>
          </tr>`).join("")}
      </tbody>
    </table>`}
  `;

  viewEl.querySelector("#opr-back-armies").addEventListener("click", () => {
    if (state.armiesPayload) renderArmies(state.armiesPayload);
  });

  viewEl.querySelectorAll("th[data-key]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      state.sortDir = state.sortKey === key && state.sortDir === "asc" ? "desc" : "asc";
      state.sortKey = key;
      renderUnits(state.unitsPayload);
    });
  });

  viewEl.querySelectorAll("tbody tr").forEach((tr) => {
    tr.addEventListener("click", async () => {
      const u = units[Number(tr.dataset.idx)];
      try {
        const result = await callServerTool(window.OprApp, "lookup_unit", {
          name: u.name,
          army: payload.army,
          game_system: payload.game_system || undefined,
        });
        renderUnit(result);
      } catch {
        // callServerTool already surfaced the error banner.
      }
    });
  });
}

// -- Unit card screen -----------------------------------------------------

function renderUpgradeGroups(groups) {
  if (!groups || groups.length === 0) return "";
  return groups.map((g) => `
    <div class="opr-upgrade-group">
      <h4>${escapeHtml(g.kind || "Upgrade")}</h4>
      ${(g.options || []).map((o) => `
        <div class="opr-upgrade-option">
          <span>${escapeHtml(o.text)}</span>
          <span class="opr-muted">${fmtPts(o.points_cost)} pts</span>
        </div>`).join("")}
    </div>`).join("");
}

function renderRules(rules) {
  if (!rules || rules.length === 0) return "<span class=\"opr-muted\">None</span>";
  return rules.map((r) => {
    if (typeof r === "string") return `<span class="opr-pill">${escapeHtml(r)}</span>`;
    return `<span class="opr-pill" title="${escapeHtml(r.description || "")}">${escapeHtml(r.name)}</span>`;
  }).join(" ");
}

function unitCard(u) {
  const src = u.source || {};
  const equipment = (u.equipment || [])
    .map((e) => `<span class="opr-pill">${escapeHtml(typeof e === "string" ? e : JSON.stringify(e))}</span>`)
    .join(" ") || "<span class=\"opr-muted\">None</span>";
  return `
    <div class="opr-card">
      <h3>${escapeHtml(u.name)}</h3>
      <div class="opr-stat-row">
        <div class="opr-stat"><span class="opr-stat-label">Points</span>${fmtPts(u.base_points)}</div>
        <div class="opr-stat"><span class="opr-stat-label">Qty</span>${fmtStat(u.qty)}</div>
        <div class="opr-stat"><span class="opr-stat-label">Quality</span>${escapeHtml(u.quality ?? "—")}</div>
        <div class="opr-stat"><span class="opr-stat-label">Defense</span>${escapeHtml(u.defense ?? "—")}</div>
      </div>
      <div><span class="opr-stat-label">Equipment</span><div>${equipment}</div></div>
      <div style="margin-top:6px"><span class="opr-stat-label">Rules</span><div>${renderRules(u.rules)}</div></div>
      ${renderUpgradeGroups(u.upgrade_groups)}
      <div class="opr-muted" style="margin-top:8px">${escapeHtml(src.filename || "")}${src.page ? ` · p.${src.page}` : ""}${src.version ? ` · v${src.version}` : ""}</div>
    </div>`;
}

function renderUnit(payload) {
  const units = payload.units || [];
  viewEl.innerHTML = `
    <div class="opr-breadcrumbs">
      <button id="opr-back-armies">Armies</button> ›
      <button id="opr-back-units">${escapeHtml(state.unitsPayload?.army || "")}</button> ›
      <span>${escapeHtml(units[0]?.name || "")}</span>
    </div>
    ${units.length === 0 ? "<div class=\"opr-empty\">Unit not found.</div>" : units.map(unitCard).join("")}
  `;
  viewEl.querySelector("#opr-back-armies").addEventListener("click", () => {
    if (state.armiesPayload) renderArmies(state.armiesPayload);
  });
  const backUnits = viewEl.querySelector("#opr-back-units");
  if (backUnits) {
    backUnits.addEventListener("click", () => {
      if (state.unitsPayload) renderUnits(state.unitsPayload);
    });
  }
}

// -- Bootstrap ------------------------------------------------------------

function route(payload) {
  if (!payload) return;
  if (payload.view === "units") renderUnits(payload);
  else if (payload.view === "unit") renderUnit(payload);
  else renderArmies(payload);
}

const app = createApp("browser");
window.OprApp = app;
app.ontoolresult = (params) => route(params.structuredContent);
app.connect();
