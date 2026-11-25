---
name: update-readme-media
description: "Use when modifying desktop/plugin.js, Desktop UI styles/views, docs/images/architecture.svg, or tools/capture_readme_media.py. Regenerates and visually verifies README screenshots and animated GIFs."
license: MIT
---

# Update Factonio README Media

Keep the screenshots, animated feature demos, and architecture diagram in `README.md` (`docs/images/`) in sync whenever Factonio's Desktop UI or workflow changes.

## When to Use

- You modified `desktop/plugin.js` (components, styles, layout, filters, inline editor, sidebar tab, Settings page, or status pill).
- You modified `tools/capture_readme_media.py` or `docs/images/architecture.svg`.
- CI failed on the **README media freshness** check (`desktop/plugin.js` or `tools/capture_readme_media.py` changed without updating `docs/images/`).
- **Do NOT use** for backend-only Python changes that have no visual effect on the Desktop UI.
- If your `desktop/plugin.js` edit is strictly non-visual (internal refactor, hook ordering, comments, lint), do **not** churn binary images — instead explain why in the PR description and request/apply the `no-visual-change` label so CI skips the freshness check cleanly.

## UI Surface to Asset Map

Pick the smallest `--only` target set that covers the UI you changed (or `--only all` for global styling/theme changes):

| UI surface changed | `--only` target(s) | Output file(s) in `docs/images/` |
|---|---|---|
| `/factonio` KPI cards, Phase Durations, Phase Models, or Repositories table layout | `dashboard` | `dashboard.png` |
| Registered Repositories inline editor (`PATCH /repos/:slug`, form fields, validation, badges, toast) | `repo-editor` | `demo-repo-editor.gif` |
| Docked right-sidebar Factonio tab (filters, workstream pipeline, cards, detail drawer, status pill) | `sidebar,workflow` | `sidebar.png`, `demo-workflow.gif` |
| `Settings -> Plugins -> Factonio` landing page | `settings` | `settings.png` |
| Shared styles, fonts, theme tokens, or multiple surfaces | `all` | All 5 rendered PNGs and GIFs |
| Pipeline phases, Information Barrier, or merge gate architecture | *(edit SVG directly)* | `architecture.svg` (previewed via `--only architecture --qa-dir ...`) |

## Workflow

### 1. Preflight local capture dependencies

```bash
python3 tools/capture_readme_media.py --check-deps
```

- Needs a headless Chromium/Chrome binary (auto-detected on `PATH`, macOS `/Applications`, Playwright cache, or via `CHROME_BIN=/path/to/chrome`) and `ffmpeg` (only needed when regenerating GIFs: `workflow`, `repo-editor`, or `all`).
- If dependencies are missing in the current environment, install them or clearly report the blocker in the PR description — never fake or hand-edit screenshots.

### 2. Sync the capture harness if UI contracts changed

`tools/capture_readme_media.py` imports and renders the **unmodified** `desktop/plugin.js` inside headless Chromium using local zero-dependency shims. Never hack `desktop/plugin.js` to appease the harness; update `tools/capture_readme_media.py` instead when:

- `desktop/plugin.js` imports a new symbol from `@hermes/plugin-sdk` or `react` → add it to `PLUGIN_SDK_SHIM_JS` or `REACT_SHIM_JS`.
- A REST endpoint (`/metrics`, `/repos`, `/models`, `/cards`, `/status`) gains new fields → update `SHARED_MOCK_DATA_JS` or the scene mock handlers so the new UI renders with realistic data.
- A new interactive control or step should be showcased (or a selector changed, e.g., a `data-action` attribute) → update the step script in `WORKFLOW_DEMO_HTML` or `REPO_EDITOR_DEMO_HTML`.

### 3. Regenerate assets and export QA frames

```bash
# Example: sidebar + workflow demo
python3 tools/capture_readme_media.py --capture --only sidebar,workflow --qa-dir /tmp/factonio-media-qa

# Or regenerate everything
python3 tools/capture_readme_media.py --capture --only all --qa-dir /tmp/factonio-media-qa
```

`--qa-dir` saves static keyframe PNGs from each GIF (`workflow_step0.png`, `workflow_step4.png`, `workflow_step7.png`, `repo-editor_step0.png`, `repo-editor_step4.png`, `repo-editor_step5.png`) and renders `architecture.svg` to `architecture.png` so you can visually inspect every animation state without extra scripts.

### 4. Visually inspect every regenerated asset (mandatory)

Inspect every updated `.png` in `docs/images/` **and** every exported keyframe PNG in `/tmp/factonio-media-qa/` with your vision tool (or open `python3 tools/capture_readme_media.py --serve-only --port 18432` in a browser if you have no vision tool). Confirm:

- **No clipping or overlap:** Headings, legend pills, badges, table columns, and expanded drawers fit cleanly with breathing room.
- **Dark theme fidelity:** No jarring light-mode scrollbars, unreadable low-contrast labels, or broken CSS tokens.
- **Interactive states actually triggered:**
  - `sidebar.png`: card detail drawer is open and legible.
  - `demo-workflow.gif` final frame (`workflow_step7.png`): `Verify` filter is active and the completed verification card drawer is fully visible and unclipped.
  - `demo-repo-editor.gif` (`repo-editor_step4.png` & `repo-editor_step5.png`): inline editor shows all 5 fields + live unsaved counter; final frame shows updated solid override badge and save toast.
- **Compact size:** Keep each GIF crisp and lightweight (typically under 600 KB) so the repository stays fast to clone.

If any defect appears, adjust the UI or harness viewport/step timing, re-run `--capture`, and re-inspect until clean.

### 5. Run repository checks and clean up scratch output

```bash
rm -rf /tmp/factonio-media-qa
python3 -W error -m compileall -q .
uvx ruff check --select E9,F .
node --check desktop/plugin.js
python3 tools/check_repo_integrity.py
python3 -m unittest tests/test_desktop_plugin.py -v
```

Stage only the updated `docs/images/*` files (and `tools/capture_readme_media.py` if modified). Never commit temporary QA frame directories. In your PR description, list which assets were regenerated and confirmed visually.
