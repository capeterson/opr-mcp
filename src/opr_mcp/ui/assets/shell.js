// Shared bootstrap for every opr-mcp view: App instantiation, theming,
// error surfacing, and small render helpers. Loaded as a separate
// <script type="module"> from the vendor bundle, so it reads the App
// class off `window.__OPR_EXT_APPS__` (set by the vendor script in the
// same module scope) rather than an ES import -- inline module scripts
// don't share scope with each other.
//
// Exposes `window.OprShell` for the per-view scripts (views/*.js) to
// consume. Kept framework-free: no bundler, no JSX -- just DOM calls.

const {
  App, applyDocumentTheme, applyHostStyleVariables, applyHostFonts,
} = window.__OPR_EXT_APPS__;

const errorEl = document.getElementById("opr-error");
const viewEl = document.getElementById("opr-view");

function showError(message) {
  errorEl.textContent = String(message);
  errorEl.hidden = false;
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** Apply the host's theme/styles/fonts to this document. Called once
 * after connect and again on every `onhostcontextchanged`. */
function applyHostContext(ctx) {
  if (!ctx) return;
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  if (ctx.styles?.css?.fonts) applyHostFonts(ctx.styles.css.fonts);
}

/** Create a standard App instance for a view. Does NOT call connect() --
 * the caller must register its `ontoolresult` handler (and any other
 * one-shot event handlers) first, per the SDK's "register before
 * connect" guidance, then call `app.connect()` itself. */
function createApp(viewName) {
  const app = new App(
    { name: `opr-mcp/${viewName}`, version: "1.0.0" },
    {},
    { autoResize: true },
  );
  app.onhostcontextchanged = (ctx) => applyHostContext(ctx);
  return app;
}

/** Call a server tool and return its `structuredContent`. Surfaces
 * `isError` results (and transport failures) via `showError` and
 * rethrows, so callers can `try {} catch { return; }` without
 * duplicating error UI. */
async function callServerTool(app, name, args) {
  clearError();
  try {
    const result = await app.callServerTool({ name, arguments: args || {} });
    if (result.isError) {
      const text = result.content?.find((c) => c.type === "text")?.text
        || "Tool call failed.";
      showError(text);
      throw new Error(text);
    }
    return result.structuredContent || {};
  } catch (err) {
    if (!errorEl.textContent) showError(err?.message || String(err));
    throw err;
  }
}

window.OprShell = {
  App,
  viewEl,
  escapeHtml,
  showError,
  clearError,
  applyHostContext,
  createApp,
  callServerTool,
};
