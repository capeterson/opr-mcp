// Force Organization Reference (ui://opr/force-org).
//
// Backs force_org_guidance. Pure client-side arithmetic -- no
// app.callServerTool() calls -- so the four limits recompute instantly
// as the game-size slider moves. The formulas are duplicated from
// opr_mcp/tools/validate_army_list.py; tests/test_ui_force_org.py pins
// this file's LIMIT_FORMULAS against that module's _limits() so the two
// can't silently drift apart.

const { viewEl, createApp } = window.OprShell;

const MIN_PTS = 500;
const MAX_PTS = 3000;
const STEP_PTS = 50;
const DEFAULT_PTS = 1000;

// Mirrors opr_mcp/tools/validate_army_list.py::_limits(). Keep the four
// entries and their formula text in sync with that function.
const LIMIT_FORMULAS = [
  { key: "max_heroes", label: "Heroes", formula: "floor(G/375)", compute: (g) => Math.floor(g / 375) },
  { key: "max_duplicates", label: "Duplicates", formula: "1 + floor(G/750)", compute: (g) => 1 + Math.floor(g / 750) },
  { key: "max_unit_cost", label: "Unit cost cap", formula: "floor(35*G/100)", compute: (g) => Math.floor((35 * g) / 100) },
  { key: "max_units", label: "Unit count cap", formula: "floor(G/150)", compute: (g) => Math.floor(g / 150) },
];

function render(gamePts) {
  viewEl.innerHTML = `
    <div class="opr-slider-row">
      <label for="opr-pts">Game size</label>
      <input type="range" id="opr-pts" min="${MIN_PTS}" max="${MAX_PTS}" step="${STEP_PTS}" value="${gamePts}" />
      <strong id="opr-pts-value">${gamePts} pts</strong>
    </div>
    <div class="opr-limits-grid">
      ${LIMIT_FORMULAS.map((l) => `
        <div class="opr-limit-tile">
          <div class="opr-limit-value">${l.compute(gamePts)}</div>
          <div class="opr-limit-label">${l.label}</div>
          <div class="opr-limit-formula">${l.formula}</div>
        </div>`).join("")}
    </div>
    <p class="opr-muted" style="margin-top:14px">
      Verified for AoF and Grimdark Future only. Duplicates: combined units count
      as one. Unit cost cap: an attached hero + unit counts as one unit.
      Call <code>validate_army_list</code> to check an actual roster against
      these limits.
    </p>
  `;

  const slider = viewEl.querySelector("#opr-pts");
  const valueEl = viewEl.querySelector("#opr-pts-value");
  slider.addEventListener("input", () => {
    const g = Number(slider.value);
    valueEl.textContent = `${g} pts`;
    viewEl.querySelectorAll(".opr-limit-value").forEach((el, i) => {
      el.textContent = LIMIT_FORMULAS[i].compute(g);
    });
  });
}

// Pure client-side view -- render immediately, don't wait on the host
// round-trip. `app.connect()` still runs (unawaited) purely so
// createApp's onhostcontextchanged wiring picks up host theming.
render(DEFAULT_PTS);

const app = createApp("force-org");
window.OprApp = app;
app.connect();
