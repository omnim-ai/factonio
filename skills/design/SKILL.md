---
name: design
description: "Use inside a Factonio Phase 1b Design Kanban card to produce UI/UX design specifications, static HTML previews, and screenshots via interactive Claude Design (/design) or pure-Hermes prototyping. Do not use for writing production code or opening pull requests."
license: MIT
---

# Factonio Design Phase (Phase 1b)

Produce an approved UI/UX design specification (`DESIGN.md`), static HTML previews, and rendered screenshots before Implementation writes UI code.

## When to Use

- Preloaded on `[design]` Kanban cards running under the `factonio` worker profile when a workstream touches user-facing UI.
- Supports two execution modes set by the card's `Design provider` field: `claude-design` (interactive Claude Code `/design` in a PTY) and `hermes` (pure-Hermes HTML/CSS prototyping with browser verification).
- Do not use to edit repository production code, commit changes, or open pull requests.

## Information Barrier

- Read only the target repository files, `spec.md`, and the Design Brief on this card.
- Never search for, open, or reference any `holdout/` directory. Keeping hidden holdout scenarios out of design and implementation preserves independent Black-Box Verification.

## Procedure

### 1. Ground in the Repository and Spec

1. Read `spec.md` and the existing UI files, design tokens, CSS variables, and component constraints in the target repository.
2. Identify hard platform constraints (for example: no build step, public plugin SDK surfaces only, theme variables, viewport widths, light/dark modes).
3. Write a concise brief at `<design_dir>/.design-brief.md` naming the target screens, real data shapes, platform constraints, files not to touch, and "do not commit".

### 2. Run the Configured Design Provider

#### Mode A: `claude-design` (Interactive Claude Code `/design`)

Headless `claude -p` only manages `/design` access and never designs, so drive an interactive Claude Code session inside a PTY:

1. Create a hooks file `<design_dir>/.claude-hooks.json` so turn completion and prompts write to `<design_dir>/.claude-events`:
   ```json
   {
     "hooks": {
       "Stop": [{"hooks": [{"type": "command", "command": "echo stop >> \"<design_dir>/.claude-events\""}]}],
       "Notification": [{"hooks": [{"type": "command", "command": "echo notify >> \"<design_dir>/.claude-events\""}]}]
     }
   }
   ```
2. Launch Claude Code in a PTY with `--effort medium` (never `high`, which multiplies round latency):
   ```text
   terminal(
     command="cd <repo_path> && claude --model claude-opus-5-5 --effort medium --settings <design_dir>/.claude-hooks.json --dangerously-skip-permissions",
     background=true,
     pty=true
   )
   ```
3. Step through interactive prompts with `process_manage`, reading the log before each input and always sending `"\r"` as a separate write after typed text:
   - **Folder trust dialog:** send `"\u001b[B"` then `"\r"` if prompted.
   - **Design consent:** send `"/design consent"` then `"\r"`, and wait for access granted.
   - **Login required (403 or `/design-login`):** do not guess credentials or ask for OAuth codes in chat. Call `kanban_block(kind="needs_input", reason="Claude Design requires interactive /design-login on this host; complete login and unblock.")` and exit cleanly.
4. Submit the design turn: send `"/design Follow the brief in <design_dir>/.design-brief.md exactly. Export DESIGN.md and self-contained static HTML previews into <design_dir>."` then `"\r"`.
5. Supervise via the events file (`: > <design_dir>/.claude-events` before each turn, wait up to 390s for non-empty output, call `kanban_heartbeat` on each wait, inspect `process_manage(action="log")` on wake). Answer routine permission/read prompts; escalate product ambiguity via `kanban_block(kind="needs_input", reason=...)`.
6. Pull or export the resulting design artifacts (`DESIGN.md`, static HTML preview files, assets) into `<design_dir>`, and capture the `claude.ai/artifact/<id>` URL.
7. Verify proof before accepting: confirm the session transcript under `~/.claude/projects/` contains `<command-name>/design</command-name>` and `"entrypoint":"cli"`.
8. Exit cleanly: send `"/exit"` then `"\r"` and poll until the process exits. Never leave a background Claude session running.

#### Mode B: `hermes` (Pure-Hermes Prototyping)

When `Design provider` is `hermes`:

1. Author `<design_dir>/DESIGN.md` with concrete layout hierarchy, typography, spacing, color tokens (mapped to the target app's existing theme variables), interaction states (loading, empty, error, active), and component structure.
2. Author one or more self-contained static HTML preview files under `<design_dir>` (for example `<design_dir>/preview.html`) that render cleanly in a browser with realistic sample data and both light/dark theme toggles when applicable.

### 2b. Arena Judge Mode (Multi-Variant HTML Switcher)

When the card is an `/arena` Design judge (`role=judge`, `target=design`):

1. Read every candidate design directory listed on the card, still strictly without `holdout/` access.
2. Hand-graft canonical `<design_dir>/DESIGN.md` from the strongest candidate material, ending with `## Arena Synthesis`.
3. Emit ONE self-contained zero-build static `<design_dir>/preview.html` multi-variant switcher: pure HTML/CSS with inline vanilla JS, host theme tokens, and no external CDN, assets, or build step. It lets the reviewer toggle Candidate A / Candidate B / ... / Synthesis (labeled by slot and vendor) and a side-by-side comparison in a single view.
4. Verify every switcher state in `browser_exec` + `vision_analyze` (§3) and list `preview.html` in `preview_files`.

### 3. Verify Rendered Output

1. Open each static HTML preview under `<design_dir>` in a headless browser (`browser_exec`) or verify its syntax/render, capture PNG screenshots into `<design_dir>/` (for example `<design_dir>/preview-desktop.png`), and inspect them with `vision_analyze` to catch layout clipping, contrast defects, or broken states.
2. Confirm `<design_dir>/DESIGN.md` exists and names every preview file, design token, and binding UI acceptance requirement for Implementation.
3. Confirm `git status` in `<repo_path>` is clean (zero uncommitted edits left in the repository).

### 3b. Frozen Visual Baselines and Formal Invariants

- **Approved previews become frozen baselines.** When the repo declares a `visual_diff` hook in `.factonio/harness.json`, name in `DESIGN.md` which screenshots should become its `baseline_paths` (frozen by default via `frozen_baseline: true`). Later phases must never regenerate those baselines (`--update-snapshots`, `-u`, `UPDATE_SNAPSHOTS=1`) or loosen visual thresholds to turn a failure green; `hermes factonio harness check` flags that as gate loosening. Changing an approved baseline needs a new Design round.
- **Formal invariants:** when a design fixes a state machine or UI contract that a repo-defined `invariant` hook models, record the hook's `covers` globs (the files the model describes) in `DESIGN.md`. `sync_commit` is set by whoever re-proves the model, so `harness audit` can report `stale_invariants` when covered files drift.

### 4. Complete the Card

Call `kanban_complete` with a concise `summary` and structured `metadata`:

```json
{
  "phase": "design",
  "provider_used": "claude-design",
  "design_dir": "<absolute path to design/>",
  "design_doc": "<absolute path to design/DESIGN.md>",
  "artifact_url": "https://claude.ai/artifact/<id-or-null>",
  "preview_files": ["<absolute path to design/preview.html>"],
  "screenshots": ["<absolute path to design/preview-desktop.png>"],
  "approval_policy": "ask"
}
```

## Error Handling

- **Claude CLI missing or unauthenticated in `claude-design` mode:** Do not silently substitute fake artifacts; block with `needs_input` naming the missing prerequisite or recommending switching `design_provider` to `hermes`.
- **Interactive Claude session stalls (>15 minutes with no log progress):** Send one lone `"\r"` to flush any unsubmitted prompt; if still idle, exit cleanly, retry once, and escalate via `needs_input` if it stalls again.
- **Rendered preview has layout or contrast flaws:** Revise before calling `kanban_complete`; never hand off unverified mockups.

## Example

```text
Card: [design] Refresh Factonio dashboard and thread sidebar (provider: claude-design, approval: ask)
Steps:
1. Read spec.md and desktop/plugin.js; write <design_dir>/.design-brief.md.
2. Launch interactive `claude --effort medium` in PTY with Stop/Notification hooks, run `/design consent`, submit `/design`.
3. Pull exported DESIGN.md and preview.html into <design_dir>, verify `<command-name>/design</command-name>` in transcript, exit Claude cleanly.
4. Capture preview-desktop.png via browser_exec, inspect with vision_analyze, confirm repo git status is clean.
5. Call `kanban_complete` with design_dir, artifact_url, preview_files, and screenshots.
```
