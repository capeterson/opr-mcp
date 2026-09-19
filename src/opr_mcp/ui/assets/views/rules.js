// Rules Search (ui://opr/rules).
//
// Backs search_rules (a list of hits) and get_special_rule (a single
// rule card), discriminated by `structuredContent.view` ("search" |
// "rule") set in opr_mcp/server/tools.py. Read-only -- no
// app.callServerTool() calls here, unlike the browser view.

const { escapeHtml, viewEl, createApp } = window.OprShell;

function scorePct(score) {
  if (typeof score !== "number") return "";
  return `${Math.round(Math.max(0, Math.min(1, score)) * 100)}%`;
}

function sourceLine(source) {
  if (!source) return "";
  const parts = [source.army, source.game_system, source.filename, source.page ? `p.${source.page}` : null]
    .filter(Boolean);
  return parts.map(escapeHtml).join(" · ");
}

function citeText(hit) {
  const src = hit.source || {};
  const loc = [src.filename, src.page ? `p.${src.page}` : null].filter(Boolean).join(" ");
  return `${hit.section_title || "Untitled section"}${loc ? ` (${loc})` : ""}:\n${hit.text}`;
}

function renderSearch(payload) {
  const hits = payload.hits || [];
  viewEl.innerHTML = `
    <div class="opr-toolbar">
      <span class="opr-muted">${hits.length} result${hits.length === 1 ? "" : "s"}${payload.query ? ` for "${escapeHtml(payload.query)}"` : ""}</span>
    </div>
    ${hits.length === 0 ? "<div class=\"opr-empty\">No matches.</div>" : hits.map((h, i) => `
      <div class="opr-card opr-hit" data-idx="${i}">
        <h3>${escapeHtml(h.section_title || "Untitled section")}
          ${typeof h.score === "number" ? `<span class="opr-pill">${scorePct(h.score)}</span>` : ""}
        </h3>
        <div class="opr-muted">${sourceLine(h.source)}</div>
        <div class="opr-hit-body">${escapeHtml(h.text)}</div>
        <div style="margin-top:6px">
          <button class="opr-btn opr-cite" data-idx="${i}">Cite this</button>
        </div>
      </div>`).join("")}
  `;

  viewEl.querySelectorAll(".opr-hit").forEach((card) => {
    card.addEventListener("click", (ev) => {
      if (ev.target.closest(".opr-cite")) return;
      card.classList.toggle("opr-expanded");
    });
  });

  viewEl.querySelectorAll(".opr-cite").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const hit = hits[Number(btn.dataset.idx)];
      const original = btn.textContent;
      try {
        await window.OprApp.updateModelContext({
          content: [{ type: "text", text: citeText(hit) }],
        });
        btn.textContent = "Cited";
        setTimeout(() => { btn.textContent = original; }, 1500);
      } catch {
        btn.textContent = "Failed";
        setTimeout(() => { btn.textContent = original; }, 1500);
      }
    });
  });
}

function renderRule(payload) {
  const rule = payload.rule;
  if (!rule) {
    viewEl.innerHTML = `<div class="opr-empty">No rule named "${escapeHtml(payload.name || "")}" found.</div>`;
    return;
  }
  const src = rule.source || {};
  viewEl.innerHTML = `
    <div class="opr-card">
      <h3>${escapeHtml(rule.name)}
        ${rule.parametric ? '<span class="opr-pill">parametric</span>' : ""}
        ${rule.scope ? `<span class="opr-pill">${escapeHtml(rule.scope)}</span>` : ""}
      </h3>
      <div style="white-space:pre-wrap">${escapeHtml(rule.description || "")}</div>
      <div class="opr-muted" style="margin-top:8px">${escapeHtml(src.filename || "")}${src.page ? ` · p.${src.page}` : ""}${src.version ? ` · v${src.version}` : ""}</div>
    </div>
  `;
}

function route(payload) {
  if (!payload) return;
  if (payload.view === "rule") renderRule(payload);
  else renderSearch(payload);
}

const app = createApp("rules");
window.OprApp = app;
app.ontoolresult = (params) => route(params.structuredContent);
app.connect();
