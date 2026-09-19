# Vendored `@modelcontextprotocol/ext-apps`

`ext-apps-2.0.0.js` is the published `dist/src/app-with-deps.js` build
from `@modelcontextprotocol/ext-apps@2.0.0` (npm), unmodified. It's the
package's own "with deps" bundle: every dependency (including `zod`) is
already inlined, so it has zero `import` statements and can be pasted
directly into a `<script type="module">` with no bundler on our side.
License: MIT, see `ext-apps-2.0.0.LICENSE.txt` (copied verbatim from the
package).

It's inlined into every rendered view by `opr_mcp.ui.render.render_view`
rather than fetched from a CDN at runtime, so views work with **no**
`_meta.ui.csp` declaration -- nothing ever leaves the iframe.

Exports consumed by `shell.js` (see the `export { ... }` statement at the
end of the file): `App`, `RESOURCE_MIME_TYPE`, `applyDocumentTheme`,
`applyHostStyleVariables`, `applyHostFonts`, `getDocumentTheme`.

## Refreshing the pinned version

```bash
npm pack @modelcontextprotocol/ext-apps@<new-version>
tar -xzf modelcontextprotocol-ext-apps-<new-version>.tgz
cp package/dist/src/app-with-deps.js src/opr_mcp/ui/assets/vendor/ext-apps-<new-version>.js
cp package/LICENSE src/opr_mcp/ui/assets/vendor/ext-apps-<new-version>.LICENSE.txt
```

Then update `_VENDOR_FILE` in `src/opr_mcp/ui/render.py`, delete the old
`ext-apps-*.js`/`.LICENSE.txt` pair, and re-run `uv run pytest` --
`tests/test_ui_render.py` re-checks the export names above still exist
in whatever file `_VENDOR_FILE` points to, so an API rename in a new
release fails the test suite instead of failing silently in a browser.
