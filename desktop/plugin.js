// Factonio Desktop UI — strictly public @hermes/plugin-sdk surfaces only.
// Registers:
//  - /factonio page (ROUTES_AREA + SIDEBAR_NAV_AREA + PALETTE_AREA)
//  - Factonio tab in the right sidebar (PANES_AREA, hideable, docked as a tab
//    beside Files; `enforce` re-homes existing installs on boot/reload)
//    + 'Show Factonio Sidebar' palette command
//  - Running/blocked card count pill (STATUSBAR_AREAS.right)
//  - ::your-turn manual step card (TRANSCRIPT_DIRECTIVE_AREA)
//  - Settings → Plugins → Factonio landing page (ctx.registerSettingsPage);
//    Hermes Desktop folds plugin.yaml config_schema underneath as "Agent settings"

import {
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  PALETTE_AREA,
  PANES_AREA,
  STATUSBAR_AREAS,
  TRANSCRIPT_DIRECTIVE_AREA,
  EmptyState,
  host,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useRef, useState } from 'react'

const ROUTE_PATH = '/factonio'
// Contribution-scoped pane id (`<pluginId>:<paneId>`) used by host.revealPane / host.paneVisibility.
const PANE_ID = 'factonio:pane'
const CARDS_POLL_MS = 15000

// Factonio phase models live in Hermes Auxiliary Models (Settings → Model →
// Auxiliary) under the `factonio_<phase>` aux task keys.
const PHASE_ORDER = ['define', 'design', 'implement', 'verify', 'watchdog']

function auxModelRoute(phase) {
  return `/settings?tab=config:model&aux=factonio_${phase}`
}

const DEFAULT_AUX_MODEL_ROUTE = auxModelRoute('define')

function formatPhaseModelLabel(info) {
  const i = info || {}
  return i.provider && i.model ? `${i.provider}/${i.model}` : (i.model || '(profile default)')
}

const STYLES = `
.fc-page { height: 100%; overflow: auto; background: var(--ui-chat-surface-background); color: var(--ui-text-primary); font-size: 13px; line-height: 1.45; }
.fc-wrap { max-width: 1040px; margin: 0 auto; padding: 20px 24px 48px; }
.fc-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 12px; padding-bottom: 14px; border-bottom: 1px solid var(--ui-stroke-secondary); margin-bottom: 18px; }
.fc-title { font-size: 18px; font-weight: 600; margin: 0; }
.fc-sub { color: var(--ui-text-secondary); font-size: 12px; margin: 2px 0 0; }
.fc-ranges { display: inline-flex; gap: 4px; }
.fc-btn { border: 1px solid var(--ui-stroke-secondary); background: transparent; color: var(--ui-text-primary); border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer; }
.fc-btn[data-active="true"] { background: var(--ui-row-active-background); border-color: var(--ui-accent); font-weight: 600; }
.fc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-bottom: 20px; }
.fc-card { border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; padding: 12px 14px; background: var(--ui-bg-elevated, transparent); }
.fc-k { font-size: 11px; color: var(--ui-text-secondary); text-transform: uppercase; letter-spacing: 0.04em; }
.fc-v { font-size: 20px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }
.fc-hint { font-size: 11px; color: var(--ui-text-secondary); margin-top: 2px; }
.fc-sec { margin-top: 22px; }
.fc-sec h2 { font-size: 13px; font-weight: 600; margin: 0 0 10px; color: var(--ui-text-primary); }
.fc-table { width: 100%; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }
.fc-table th, .fc-table td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--ui-stroke-tertiary, var(--ui-stroke-secondary)); }
.fc-table th { color: var(--ui-text-secondary); font-weight: 500; }
.fc-mono { font-family: var(--dt-font-mono, ui-monospace, monospace); font-size: 11px; }
.fc-warn { border: 1px solid color-mix(in srgb, var(--ui-warning, var(--ui-yellow, color-mix(in srgb, var(--dt-destructive) 55%, var(--ui-success)))) 45%, transparent); background: color-mix(in srgb, var(--ui-warning, var(--ui-yellow, color-mix(in srgb, var(--dt-destructive) 55%, var(--ui-success)))) 9%, transparent); border-radius: 8px; padding: 10px 12px; margin-bottom: 16px; font-size: 12px; }
.fc-pill { display: inline-flex; align-items: center; gap: 6px; border: 0; background: transparent; color: var(--ui-text-secondary); font-size: 11px; padding: 2px 6px; border-radius: 4px; cursor: pointer; font-variant-numeric: tabular-nums; }
.fc-pill:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary); }
.fc-dot { width: 7px; height: 7px; border-radius: 999px; display: inline-block; }
.fc-dot-green { background: var(--ui-success); }
.fc-dot-amber { background: var(--ui-warning, var(--ui-yellow, color-mix(in srgb, var(--dt-destructive) 55%, var(--ui-success)))); }
.fc-dot-red { background: var(--dt-destructive); }
.fc-dot-muted { background: var(--ui-text-tertiary); }
.fc-badge { font-size: 10px; padding: 1px 6px; border-radius: 999px; border: 1px solid var(--ui-stroke-secondary); color: var(--ui-text-secondary); }
.fc-step { border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; padding: 10px 12px; margin: 8px 0; display: flex; flex-direction: column; gap: 8px; font-size: 12px; }
.fc-step-cmd { background: var(--ui-bg-elevated, color-mix(in srgb, var(--ui-text-secondary) 8%, transparent)); padding: 6px 8px; border-radius: 5px; font-family: var(--dt-font-mono, ui-monospace, monospace); font-size: 11px; overflow-x: auto; }
.fc-step-actions { display: flex; gap: 8px; align-items: center; }
.fc-sec-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.fc-sec-head h2 { margin: 0; }
.fc-settings { padding: 4px 0 24px; color: var(--ui-text-primary); font-size: 13px; line-height: 1.45; }
.fc-settings-note { border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; padding: 10px 12px; margin-bottom: 14px; display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; font-size: 12px; color: var(--ui-text-secondary); }
.fc-settings-err { color: var(--ui-text-secondary); font-size: 12px; margin-bottom: 10px; }
/* Registered Repositories editor (.fc-repos*, .fc-tag*, .fc-ed*): host theme tokens, transparent, currentColor and color-mix only. */
.fc-repos-sec { container-type: inline-size; }
.fc-sec-sub { margin: 2px 0 0; font-size: 11.5px; color: var(--ui-text-secondary); }
.fc-legend { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--ui-text-secondary); }
.fc-btn:hover:not(:disabled) { background: var(--ui-row-hover-background); }
.fc-btn:disabled { opacity: 0.5; cursor: default; }
.fc-btn:focus-visible, .fc-input:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 1px; }
.fc-btn--primary { border-color: color-mix(in srgb, var(--ui-accent) 55%, transparent); background: color-mix(in srgb, var(--ui-accent) 14%, transparent); font-weight: 600; }
.fc-btn--primary:hover:not(:disabled) { background: color-mix(in srgb, var(--ui-accent) 22%, transparent); }
.fc-repos-wrap { overflow-x: auto; max-width: 100%; }
.fc-repos { table-layout: fixed; min-width: 992px; }
.fc-repos th { white-space: normal; line-height: 1.25; vertical-align: bottom; }
.fc-repos td { vertical-align: middle; }
.fc-repos tbody tr:not(.fc-repo-ed):hover > td { background: var(--ui-row-hover-background); }
.fc-col-repo { width: 19%; } .fc-col-branch { width: 8%; } .fc-col-merge { width: 7%; } .fc-col-publish { width: 7%; } .fc-col-rounds { width: 6%; }
.fc-col-prov { width: 12%; } .fc-col-appr { width: 12%; } .fc-col-rigor { width: 14%; } .fc-col-act { width: 15%; }
.fc-pol { display: inline-flex; align-items: center; gap: 5px; max-width: 100%; font-family: var(--dt-font-mono); font-size: 11.5px; color: var(--ui-text-secondary); white-space: nowrap; }
.fc-pol::before { content: ""; flex: none; width: 6px; height: 6px; box-sizing: border-box; border-radius: 999px; border: 1.25px solid color-mix(in srgb, var(--ui-text-secondary) 58%, transparent); }
.fc-pol[data-pol="ask"] { color: var(--ui-text-primary); font-weight: 600; }
.fc-pol[data-pol="ask"]::before { border-color: var(--ui-accent); background: var(--ui-accent); }
.fc-th-sub { display: block; font-weight: 400; font-size: 10.5px; color: var(--ui-text-secondary); }
.fc-num, .fc-act { text-align: right !important; }
.fc-trunc { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fc-repo-name { font-family: var(--dt-font-mono, ui-monospace, monospace); font-size: 11.5px; color: var(--ui-text-primary); }
.fc-repo-path { font-family: var(--dt-font-mono, ui-monospace, monospace); font-size: 10.5px; color: var(--ui-text-secondary); margin-top: 1px; }
.fc-tag { display: inline-flex; align-items: center; gap: 4px; max-width: 100%; height: 20px; box-sizing: border-box; padding: 0 7px; border-radius: 999px; border: 1px solid transparent; font-family: var(--dt-font-mono, ui-monospace, monospace); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; vertical-align: middle; }
.fc-tag--inherit { border: 1px dashed color-mix(in srgb, var(--ui-text-secondary) 48%, transparent); background: color-mix(in srgb, var(--ui-chat-surface-background) 65%, transparent); color: var(--ui-text-secondary); }
.fc-tag--inherit .fc-tag-src { font-family: inherit; color: var(--ui-text-secondary); opacity: 0.85; }
.fc-tag--set { border-color: color-mix(in srgb, var(--ui-accent) 55%, transparent); background: color-mix(in srgb, var(--ui-accent) 15%, transparent); color: var(--ui-text-primary); font-weight: 600; }
.fc-repos tr[data-editing="true"] > td { background: var(--ui-row-active-background); border-bottom-color: transparent; }
.fc-repos tr[data-editing="true"] > td:first-child { box-shadow: inset 2px 0 0 var(--ui-accent); }
.fc-repos tr[data-editing="true"] .fc-tag--inherit { background: var(--ui-chat-surface-background); }
.fc-repo-ed > td { padding: 0; }
.fc-ed { display: flex; flex-direction: column; gap: 12px; margin: 0; padding: 2px 14px 14px 16px; border: 0; min-width: 0; position: sticky; left: 0; max-width: 100cqi; box-sizing: border-box; }
.fc-ed-id { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 11px; color: var(--ui-text-secondary); }
.fc-ed-id .fc-mono { color: var(--ui-text-primary); }
.fc-ed-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 16px; border: 0; margin: 0; padding: 0; min-width: 0; }
@container (max-width: 640px) { .fc-ed-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@container (max-width: 640px) { .fc-ed-grid > .fc-field[data-field="default_branch"] { grid-column: 1 / -1; } }
.fc-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.fc-field label { font-size: 11.5px; font-weight: 500; color: var(--ui-text-primary); }
.fc-input { height: 28px; box-sizing: border-box; width: 100%; padding: 0 8px; border: 1px solid color-mix(in srgb, var(--ui-text-secondary) 32%, var(--ui-stroke-secondary)); border-radius: 6px; background: var(--ui-chat-surface-background); color: var(--ui-text-primary); font: inherit; font-size: 12px; }
select.fc-input { padding: 0 22px 0 8px; }
.fc-input.fc-mono { font-family: var(--dt-font-mono, ui-monospace, monospace); font-size: 11.5px; }
.fc-input:focus-visible { border-color: var(--ui-accent); }
.fc-input:disabled { opacity: 0.68; cursor: default; }
.fc-input[aria-invalid="true"] { border-color: var(--dt-destructive); box-shadow: 0 0 0 1px color-mix(in srgb, var(--dt-destructive) 35%, transparent); }
.fc-field-hint { font-size: 11px; color: var(--ui-text-secondary); }
.fc-field-err, .fc-ed-msg--err { font-size: 11px; color: color-mix(in srgb, var(--dt-destructive) 80%, var(--ui-text-primary)); }
.fc-ed-foot { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px; padding-top: 12px; border-top: 1px solid var(--ui-stroke-tertiary); }
.fc-ed-msg { flex: 1 1 280px; min-width: 0; font-size: 11.5px; color: var(--ui-text-secondary); }
.fc-alert { display: flex; align-items: flex-start; gap: 7px; padding: 7px 10px; border-radius: 6px; font-size: 12px; border: 1px solid color-mix(in srgb, var(--dt-destructive) 45%, transparent); background: color-mix(in srgb, var(--dt-destructive) 11%, transparent); color: color-mix(in srgb, var(--dt-destructive) 82%, var(--ui-text-primary)); }
.fc-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.fc-alert svg { flex: none; margin-top: 2px; }
.fc-spin { display: inline-block; width: 10px; height: 10px; margin-right: 6px; border: 1.5px solid currentColor; border-right-color: transparent; border-radius: 999px; vertical-align: -1px; animation: fc-spin .8s linear infinite; }
@keyframes fc-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .fc-spin { animation: none; border-right-color: currentColor; opacity: .6; } }
/* Factonio sidebar tab (.fc-sb-*): host theme tokens + translucent color-mix tints only. */
.fc-sb {
  --fc-sb-warn: var(--fc-warn);
  --fc-sb-danger-text: color-mix(in srgb, var(--dt-destructive) 72%, var(--ui-text-primary));
  --fc-sb-warn-text: color-mix(in srgb, var(--fc-sb-warn) 48%, var(--ui-text-primary));
  --fc-sb-success-text: color-mix(in srgb, var(--ui-success) 78%, var(--ui-text-primary));
  position: relative; height: 100%; min-width: 0; display: flex; flex-direction: column;
  color: var(--ui-text-primary); font-size: 12px; line-height: 1.4;
}
.fc-sb *, .fc-sb *::before, .fc-sb *::after { box-sizing: border-box; }
:where(.fc-sb button) { font: inherit; color: inherit; margin: 0; }
.fc-sb :focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 1px; }
.fc-sb-ico { flex: none; display: block; }
.fc-sb-mono { font-family: var(--dt-font-mono); font-variant-numeric: tabular-nums; }

/* header + controls */
.fc-sb-head { flex: none; display: flex; flex-direction: column; gap: 7px; padding: 10px 12px 9px; border-bottom: 1px solid var(--ui-stroke-tertiary); }
.fc-sb-titlebar { display: flex; align-items: center; gap: 6px; min-height: 24px; }
.fc-sb-title { margin: 0; font-size: 13px; font-weight: 600; }
.fc-sb-count { font-family: var(--dt-font-mono); font-variant-numeric: tabular-nums; font-size: 10.5px; line-height: 18px; padding: 0 6px; border-radius: 999px; background: var(--ui-row-active-background); color: var(--ui-text-secondary); }
.fc-sb-spacer { flex: 1; }
.fc-sb-btn { display: inline-flex; align-items: center; gap: 5px; height: 24px; padding: 0 8px; border: 1px solid var(--ui-stroke-secondary); border-radius: 6px; background: transparent; font-size: 11.5px; cursor: pointer; white-space: nowrap; }
.fc-sb-btn:hover { background: var(--ui-row-hover-background); }
.fc-sb-btn--primary { border-color: color-mix(in srgb, var(--ui-accent) 55%, transparent); background: color-mix(in srgb, var(--ui-accent) 14%, transparent); font-weight: 600; }
.fc-sb-btn--primary:hover { background: color-mix(in srgb, var(--ui-accent) 22%, transparent); }
.fc-sb-seg { display: flex; gap: 2px; padding: 2px; border-radius: 7px; background: var(--ui-row-hover-background); border: 1px solid var(--ui-stroke-tertiary); }
.fc-sb-seg button { flex: 1 1 0; min-width: 0; height: 22px; padding: 0 4px; border: 0; border-radius: 5px; background: transparent; color: var(--ui-text-secondary); font-size: 11.5px; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc-sb-seg button:hover { color: var(--ui-text-primary); }
.fc-sb-seg button[aria-pressed="true"] { background: var(--ui-bg-elevated); color: var(--ui-text-primary); font-weight: 600; box-shadow: 0 0 0 1px var(--ui-stroke-secondary); }
.fc-sb-seg--fit button { flex: 1 1 auto; font-size: 11px; }
.fc-sb-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 3px; }
.fc-sb-stat { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 1px; padding: 4px 5px 4px 6px; border: 1px solid var(--ui-stroke-tertiary); border-radius: 6px; background: transparent; text-align: left; cursor: pointer; }
.fc-sb-stat:hover { background: var(--ui-row-hover-background); }
.fc-sb-stat[aria-pressed="true"] { border-color: color-mix(in srgb, var(--ui-accent) 60%, transparent); background: color-mix(in srgb, var(--ui-accent) 11%, transparent); }
.fc-sb-stat-n { font-family: var(--dt-font-mono); font-variant-numeric: tabular-nums; font-size: 14px; font-weight: 600; line-height: 1.15; }
.fc-sb-stat-l { max-width: 100%; font-size: 10px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc-sb-stat[data-hot="true"] .fc-sb-stat-n { color: var(--fc-sb-danger-text); }
.fc-sb-stat[data-hot="true"] { border-color: color-mix(in srgb, var(--dt-destructive) 35%, transparent); }

/* loading line + banner */
.fc-sb-progress { flex: none; height: 2px; overflow: hidden; background: transparent; }
.fc-sb-progress::after { content: ""; display: block; width: 35%; height: 100%; background: var(--ui-accent); animation: fc-sb-slide 1.1s ease-in-out infinite; }
@keyframes fc-sb-slide { from { transform: translateX(-100%); } to { transform: translateX(290%); } }
.fc-sb-banner { flex: none; display: flex; gap: 6px; align-items: flex-start; margin: 8px 8px 0; padding: 6px 8px; border-radius: 6px; font-size: 11px; color: var(--fc-sb-warn-text); border: 1px solid color-mix(in srgb, var(--fc-sb-warn) 40%, transparent); background: color-mix(in srgb, var(--fc-sb-warn) 10%, transparent); }
.fc-sb-banner .fc-sb-ico { margin-top: 1px; }

/* scroll body */
.fc-sb-body { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; padding: 8px 6px 16px 8px; display: flex; flex-direction: column; gap: 8px; scrollbar-width: thin; scrollbar-color: var(--ui-stroke-secondary) transparent; }
.fc-sb-body::-webkit-scrollbar { width: 5px; }
.fc-sb-body::-webkit-scrollbar-track { background: transparent; }
.fc-sb-body::-webkit-scrollbar-thumb { background: var(--ui-stroke-secondary); border-radius: 999px; }

/* workstream */
.fc-sb-ws { border: 1px solid var(--ui-stroke-tertiary); border-radius: 8px; min-width: 0; }
.fc-sb-ws[data-state="attention"] { border-color: color-mix(in srgb, var(--dt-destructive) 30%, var(--ui-stroke-tertiary)); }
.fc-sb-ws-head { display: flex; align-items: center; gap: 4px; padding: 3px 6px 3px 3px; }
.fc-sb-ws-toggle { flex: 1; min-width: 0; display: flex; align-items: center; gap: 5px; padding: 4px; border: 0; border-radius: 5px; background: transparent; text-align: left; cursor: pointer; }
.fc-sb-ws-toggle:hover { background: var(--ui-row-hover-background); }
.fc-sb-chev { flex: none; color: var(--ui-text-tertiary); transition: transform 120ms ease; }
[aria-expanded="true"] > .fc-sb-chev, [aria-expanded="true"] .fc-sb-chev { transform: rotate(90deg); }
.fc-sb-ws-text { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.fc-sb-ws-label { font-size: 12px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc-sb-ws-meta { display: flex; align-items: center; gap: 4px; min-width: 0; font-size: 10.5px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; }
.fc-sb-ws-meta > span { flex: none; }
.fc-sb-ws-meta > .fc-sb-ws-repo { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.fc-sb-ws-meta .fc-sb-mono { font-size: 10.5px; }
.fc-sb-attn-text { color: var(--fc-sb-danger-text); font-weight: 600; }
.fc-sb-ws-cards { display: flex; flex-direction: column; gap: 5px; padding: 0 6px 6px; }
.fc-sb-tone-done { color: var(--fc-sb-success-text); }
.fc-sb-tone-running { color: var(--ui-accent); }
.fc-sb-tone-queued { color: var(--fc-sb-warn-text); }
.fc-sb-tone-attention { color: var(--fc-sb-danger-text); }
.fc-sb-tone-pending, .fc-sb-tone-skipped { color: var(--ui-text-secondary); }

/* phase pipeline */
.fc-sb-pipe { list-style: none; margin: 0; padding: 2px 6px 8px; display: grid; grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.02fr) minmax(0, 1.08fr) minmax(0, 1.24fr); gap: 4px; }
.fc-sb-stage { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.fc-sb-bar { display: flex; gap: 2px; height: 3px; }
.fc-sb-bar span { flex: 1; border-radius: 2px; background: var(--ui-stroke-secondary); }
.fc-sb-bar span[data-state="done"] { background: var(--ui-success); }
.fc-sb-bar span[data-state="running"] { background: var(--ui-accent); animation: fc-sb-pulse 1.6s ease-in-out infinite; }
.fc-sb-bar span[data-state="queued"] { background: color-mix(in srgb, var(--fc-sb-warn) 75%, transparent); }
.fc-sb-bar span[data-state="attention"] { background: var(--dt-destructive); }
.fc-sb-bar span[data-state="skipped"] { background: repeating-linear-gradient(90deg, var(--ui-text-tertiary) 0 3px, transparent 3px 6px); opacity: 0.7; }
.fc-sb-stage-name { display: flex; align-items: center; gap: 3px; font-size: 10.5px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc-sb-stage[data-state="skipped"] .fc-sb-stage-name, .fc-sb-stage[data-state="pending"] .fc-sb-stage-name { color: var(--ui-text-secondary); opacity: 0.78; font-weight: 400; }
.fc-sb-stage-word { display: flex; align-items: center; gap: 2px; min-width: 0; font-size: 9.5px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; }
.fc-sb-stage-word b { font-weight: 500; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.fc-sb-mini { display: inline-flex; gap: 2px; width: 34px; height: 3px; }
.fc-sb-mini span { flex: 1; border-radius: 2px; background: var(--ui-success); }
@keyframes fc-sb-pulse { 50% { opacity: 0.4; } }

/* card */
.fc-sb-card { min-width: 0; display: flex; flex-direction: column; gap: 5px; padding: 7px 8px 5px; border: 1px solid var(--ui-stroke-secondary); border-radius: 6px; background: var(--ui-bg-elevated); cursor: pointer; }
.fc-sb-card:hover { background: linear-gradient(var(--ui-row-hover-background), var(--ui-row-hover-background)), var(--ui-bg-elevated); }
.fc-sb-card[data-attn="true"] { border-color: color-mix(in srgb, var(--dt-destructive) 30%, var(--ui-stroke-secondary)); }
.fc-sb-card[data-open="true"] { border-color: color-mix(in srgb, var(--ui-accent) 50%, var(--ui-stroke-secondary)); cursor: default; }
.fc-sb-card-top { display: flex; align-items: center; gap: 5px; min-width: 0; }
.fc-sb-dot { flex: none; width: 7px; height: 7px; border-radius: 999px; background: var(--ui-text-tertiary); }
.fc-sb-dot[data-status="running"] { background: var(--ui-accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ui-accent) 22%, transparent); animation: fc-sb-pulse 1.6s ease-in-out infinite; }
.fc-sb-dot[data-status="blocked"], .fc-sb-dot[data-status="reject"] { background: var(--dt-destructive); }
.fc-sb-dot[data-status="triage"], .fc-sb-dot[data-status="ready"], .fc-sb-dot[data-status="todo"], .fc-sb-dot[data-status="review"] { background: var(--fc-sb-warn); }
.fc-sb-dot[data-status="done"] { background: var(--ui-success); }
.fc-sb-tag { flex: none; font-family: var(--dt-font-mono); font-size: 10px; line-height: 16px; padding: 0 5px; border-radius: 4px; white-space: nowrap; background: var(--ui-row-active-background); color: var(--ui-text-secondary); }
.fc-sb-tag[data-tone="pass"] { background: color-mix(in srgb, var(--ui-success) 16%, transparent); color: var(--fc-sb-success-text); }
.fc-sb-tag[data-tone="reject"] { background: color-mix(in srgb, var(--dt-destructive) 16%, transparent); color: var(--fc-sb-danger-text); }
.fc-sb-tag[data-tone="warn"] { background: color-mix(in srgb, var(--fc-sb-warn) 16%, transparent); color: var(--fc-sb-warn-text); }
.fc-sb-dur { margin-left: auto; flex: none; font-family: var(--dt-font-mono); font-variant-numeric: tabular-nums; font-size: 10.5px; color: var(--ui-text-secondary); }
.fc-sb-card-title { margin: 0; font-size: 12px; font-weight: 500; line-height: 1.35; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.fc-sb-card[data-open="true"] .fc-sb-card-title { -webkit-line-clamp: unset; display: block; }
.fc-sb-callout { display: flex; gap: 6px; align-items: flex-start; padding: 5px 7px; border-radius: 5px; font-size: 11px; line-height: 1.35; color: var(--fc-sb-danger-text); border: 1px solid color-mix(in srgb, var(--dt-destructive) 38%, transparent); background: color-mix(in srgb, var(--dt-destructive) 11%, transparent); }
.fc-sb-callout[data-tone="warn"] { color: var(--fc-sb-warn-text); border-color: color-mix(in srgb, var(--fc-sb-warn) 42%, transparent); background: color-mix(in srgb, var(--fc-sb-warn) 11%, transparent); }
.fc-sb-callout .fc-sb-ico { margin-top: 2px; }
.fc-sb-callout-body { min-width: 0; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.fc-sb-card[data-open="true"] .fc-sb-callout-body { -webkit-line-clamp: unset; display: block; }
.fc-sb-kind { font-family: var(--dt-font-mono); font-size: 9.5px; font-weight: 600; letter-spacing: 0.02em; text-transform: uppercase; padding: 0 4px; margin-right: 4px; border-radius: 3px; background: color-mix(in srgb, currentColor 12%, transparent); }
.fc-sb-card-foot { display: flex; align-items: center; gap: 4px; min-width: 0; margin: 0 -3px; }
.fc-sb-pr { display: inline-flex; align-items: center; gap: 3px; height: 20px; padding: 0 5px; border-radius: 4px; border: 1px solid var(--ui-stroke-secondary); font-family: var(--dt-font-mono); font-size: 10.5px; color: var(--ui-text-primary); text-decoration: none; white-space: nowrap; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.fc-sb-pr:hover { background: var(--ui-row-hover-background); border-color: var(--ui-accent); }
.fc-sb-idbtn { display: inline-flex; align-items: center; gap: 4px; height: 20px; padding: 0 4px; border: 0; border-radius: 4px; background: transparent; font-family: var(--dt-font-mono); font-size: 10.5px; color: var(--ui-text-secondary) !important; cursor: pointer; min-width: 0; }
.fc-sb-idbtn:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary) !important; }
.fc-sb-iconbtn { margin-left: auto; flex: none; display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border: 0; border-radius: 4px; background: transparent; cursor: pointer; }
.fc-sb-iconbtn:hover { background: var(--ui-row-hover-background); }
.fc-sb-drawer { margin: 0; padding: 6px 0 2px; border-top: 1px dashed var(--ui-stroke-secondary); display: grid; grid-template-columns: 60px minmax(0, 1fr); gap: 4px 8px; font-size: 11px; cursor: auto; }
.fc-sb-drawer dt { color: var(--ui-text-secondary); }
.fc-sb-drawer dd { margin: 0; min-width: 0; overflow-wrap: anywhere; display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.fc-sb-drawer .fc-sb-full { grid-column: 1 / -1; }
.fc-sb-summary { padding: 6px 7px; border-radius: 5px; background: var(--ui-row-hover-background); color: var(--ui-text-primary); line-height: 1.4; }
.fc-sb-chip { font-family: var(--dt-font-mono); font-size: 10px; line-height: 16px; padding: 0 5px; border-radius: 4px; border: 1px solid var(--ui-stroke-secondary); color: var(--ui-text-secondary); }

/* completed disclosure */
.fc-sb-done-toggle { width: 100%; display: flex; align-items: center; gap: 6px; height: 26px; padding: 0 7px; border: 1px dashed var(--ui-stroke-secondary); border-radius: 6px; background: transparent; color: var(--ui-text-secondary) !important; font-size: 11px; cursor: pointer; text-align: left; }
.fc-sb-done-toggle:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary) !important; }
.fc-sb-done-toggle .fc-sb-done-phases { flex: 1; min-width: 0; color: var(--ui-text-secondary); opacity: 0.82; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-family: var(--dt-font-mono); font-size: 10px; }
.fc-sb-done-list { display: flex; flex-direction: column; gap: 5px; }

/* empty + skeleton */
.fc-sb-empty { margin: 28px 10px; display: flex; flex-direction: column; align-items: center; gap: 6px; text-align: center; color: var(--ui-text-secondary); font-size: 11.5px; }
.fc-sb-empty-ico { width: 32px; height: 32px; border-radius: 999px; display: grid; place-items: center; background: var(--ui-row-active-background); color: var(--ui-text-secondary); margin-bottom: 2px; }
.fc-sb-empty h3 { margin: 0; font-size: 13px; font-weight: 600; color: var(--ui-text-primary); }
.fc-sb-empty p { margin: 0 0 6px; max-width: 220px; }
.fc-sb-skel { border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; padding: 10px; display: flex; flex-direction: column; gap: 7px; }
.fc-sb-skel span { display: block; height: 8px; border-radius: 4px; background: color-mix(in srgb, var(--ui-text-secondary) 22%, transparent); animation: fc-sb-pulse 1.6s ease-in-out infinite; }
.fc-sb-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (prefers-reduced-motion: reduce) {
  .fc-sb *, .fc-sb *::after { animation: none !important; transition: none !important; }
}
.fc-sb-sub { display: inline-flex; align-items: center; gap: 2px; }
.fc-sb-sub-l { color: var(--ui-text-secondary); }
/* Decision Trail — dashboard (.fc-trail*): host theme tokens, transparent, currentColor and color-mix only. */
.fc-trail {
  --fc-trail-warn: var(--ui-warning, var(--ui-yellow, color-mix(in srgb, var(--dt-destructive) 55%, var(--ui-success))));
  --fc-trail-ok-text: color-mix(in srgb, var(--ui-success) 78%, var(--ui-text-primary));
  --fc-trail-bad-text: color-mix(in srgb, var(--dt-destructive) 72%, var(--ui-text-primary));
  --fc-trail-warn-text: color-mix(in srgb, var(--fc-trail-warn) 52%, var(--ui-text-primary));
  --fc-trail-info-text: color-mix(in srgb, var(--ui-accent) 70%, var(--ui-text-primary));
}
.fc-trail-head-l { min-width: 0; }

.fc-trail-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px; margin-bottom: 8px; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; background: var(--ui-bg-elevated); }
.fc-trail-bar[data-disabled="true"] { opacity: 0.58; }
.fc-trail-sel { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.fc-trail-sel label { font-size: 11px; color: var(--ui-text-secondary); white-space: nowrap; }
.fc-trail-sel .fc-input { width: auto; min-width: 140px; max-width: 240px; padding-right: 4px; }
.fc-trail-sel .fc-input[data-set="true"] { border-color: color-mix(in srgb, var(--ui-accent) 60%, transparent); }
.fc-trail-phases button:disabled, .fc-trail-sel .fc-input:disabled { opacity: 0.55; cursor: default; }

.fc-trail-phase { display: inline-flex; align-items: center; height: 18px; padding: 0 6px; border-radius: 4px; font-family: var(--dt-font-mono); font-size: 10.5px; background: var(--ui-row-active-background); color: var(--ui-text-secondary); white-space: nowrap; }
.fc-trail-phase[data-phase="escape"], .fc-trail-phase[data-phase="watchdog"] { box-shadow: inset 0 0 0 1px var(--ui-stroke-secondary); color: var(--ui-text-primary); }

.fc-trail-pill { display: inline-flex; align-items: center; gap: 4px; max-width: 100%; height: 20px; box-sizing: border-box; padding: 0 7px; border-radius: 999px; border: 1px solid var(--ui-stroke-secondary); background: transparent; font-family: var(--dt-font-mono); font-size: 11px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-decoration: none; }
.fc-trail-pill .fc-trail-k { font-family: var(--dt-font-sans); color: var(--ui-text-tertiary); }
.fc-trail-pill[data-tone="ok"] { border-color: color-mix(in srgb, var(--ui-success) 40%, transparent); color: var(--fc-trail-ok-text); }
.fc-trail-pill[data-tone="bad"] { border-color: color-mix(in srgb, var(--dt-destructive) 45%, transparent); color: var(--fc-trail-bad-text); }
a.fc-trail-pill:hover, button.fc-trail-pill:hover { background: var(--ui-row-hover-background); border-color: var(--ui-accent); color: var(--ui-text-primary); }
button.fc-trail-pill { font: inherit; font-family: var(--dt-font-mono); font-size: 11px; cursor: pointer; }
.fc-trail :focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 1px; }

.fc-trail-state { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 32px 16px 34px; border: 1px dashed var(--ui-stroke-secondary); border-radius: 8px; text-align: center; color: var(--ui-text-secondary); font-size: 12px; }
.fc-trail-state h3 { margin: 0; font-size: 13px; font-weight: 600; color: var(--ui-text-primary); }
.fc-trail-state p { margin: 0; max-width: 46ch; line-height: 1.5; }
.fc-trail-state-ico { width: 32px; height: 32px; border-radius: 999px; display: grid; place-items: center; background: var(--ui-row-active-background); color: var(--ui-text-secondary); margin-bottom: 2px; }
.fc-trail-state .fc-step-cmd { margin: 6px 0 2px; color: var(--ui-text-primary); }
.fc-trail-state .fc-btn { margin-top: 6px; }
.fc-trail-active { display: flex; flex-wrap: wrap; justify-content: center; gap: 4px; }
.fc-trail-alert { align-items: center; padding: 9px 10px 9px 12px; }
.fc-trail-alert .fc-trail-alert-body { flex: 1; min-width: 0; }
.fc-trail-alert .fc-trail-alert-body b { color: var(--ui-text-primary); }
.fc-trail-alert .fc-btn { flex: none; display: inline-flex; align-items: center; gap: 5px; color: var(--ui-text-primary); }
.fc-trail-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (prefers-reduced-motion: reduce) {
  .fc-trail * { animation: none !important; transition: none !important; }
}

/* Decision Trail — sidebar (.fc-sb-trail*): inside .fc-sb, reuses --fc-sb-* tone tokens. */
.fc-sb-trail { display: flex; flex-direction: column; gap: 5px; }
.fc-sb-trail-toggle { width: 100%; display: flex; align-items: center; gap: 6px; height: 26px; padding: 0 7px; border: 1px dashed var(--ui-stroke-secondary); border-radius: 6px; background: transparent; color: var(--ui-text-secondary) !important; font-size: 11px; cursor: pointer; text-align: left; min-width: 0; }
.fc-sb-trail-toggle:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary) !important; }
.fc-sb-trail-toggle .fc-sb-trail-phases { flex: 1; min-width: 0; opacity: 0.82; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-family: var(--dt-font-mono); font-size: 10px; }
.fc-sb-trail-toggle .fc-sb-trail-flag { flex: none; display: inline-flex; align-items: center; gap: 3px; font-size: 10.5px; font-weight: 600; color: var(--fc-sb-danger-text); }
.fc-sb-trail-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; border: 1px solid var(--ui-stroke-tertiary); border-radius: 6px; overflow: hidden; }
.fc-sb-trail-item { min-width: 0; display: flex; flex-direction: column; gap: 3px; padding: 6px 7px; border-bottom: 1px solid var(--ui-stroke-tertiary); }
.fc-sb-trail-item:last-child { border-bottom: 0; }
.fc-sb-trail-item[data-superseded="true"] { background: color-mix(in srgb, var(--ui-text-tertiary) 6%, transparent); }
.fc-sb-trail-item[data-superseded="true"] .fc-sb-trail-title { color: var(--ui-text-secondary); text-decoration: line-through; text-decoration-color: var(--ui-text-tertiary); }
.fc-sb-trail-top { display: flex; flex-wrap: wrap; align-items: center; gap: 3px 4px; min-width: 0; }
.fc-sb-trail-time { margin-left: auto; flex: none; font-family: var(--dt-font-mono); font-variant-numeric: tabular-nums; font-size: 10px; color: var(--ui-text-secondary); }
.fc-sb-trail-title { margin: 0; font-size: 11.5px; font-weight: 500; line-height: 1.35; overflow-wrap: anywhere; }
.fc-sb-trail-why { margin: 0; font-size: 11px; line-height: 1.4; color: var(--ui-text-secondary); overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.fc-sb-trail-ev { display: flex; flex-wrap: wrap; gap: 3px; min-width: 0; }
.fc-sb-trail-ev .fc-sb-chip { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fc-sb-trail-ev .fc-sb-chip[data-tone="ok"] { color: var(--fc-sb-success-text); border-color: color-mix(in srgb, var(--ui-success) 40%, transparent); }
.fc-sb-trail-ev .fc-sb-chip[data-tone="bad"] { color: var(--fc-sb-danger-text); border-color: color-mix(in srgb, var(--dt-destructive) 45%, transparent); }
.fc-sb-tag[data-tone="info"] { background: color-mix(in srgb, var(--ui-accent) 14%, transparent); color: color-mix(in srgb, var(--ui-accent) 70%, var(--ui-text-primary)); }
.fc-sb-trail-sup { flex: none; font-size: 10px; line-height: 16px; padding: 0 4px; border-radius: 4px; border: 1px dashed var(--ui-stroke-secondary); color: var(--ui-text-secondary); white-space: nowrap; }
.fc-sb-trail-more { display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 5px 7px; font-size: 10.5px; color: var(--ui-text-secondary); background: var(--ui-row-hover-background); }
.fc-sb-trail-more button { border: 0; background: transparent; padding: 0; color: var(--ui-accent) !important; font-size: 10.5px; cursor: pointer; white-space: nowrap; }
.fc-sb-trail-more button:hover { text-decoration: underline; }
.fc-sb-drawer .fc-sb-trail-dt { margin-top: 3px; padding-top: 5px; border-top: 1px dashed var(--ui-stroke-tertiary); }
.fc-sb-drawer .fc-sb-trail-dd { display: block; }
.fc-sb-drawer .fc-sb-trail-dd .fc-sb-trail-list { margin-top: 1px; }
.fc-sb-trail-none { color: var(--ui-text-tertiary); font-style: italic; font-size: 11px; }

/* v0.2.0 polish — verification panels, rigor, trends (.fc-kpi*, .fc-trend*, .fc-spark*, .fc-vp*, .fc-sb-panel*, .fc-sb-vendor*): host tokens, transparent, currentColor and color-mix only. */
.fc-page, .fc-sb {
  --fc-warn: var(--ui-warning, var(--ui-yellow, color-mix(in srgb, var(--dt-destructive) 55%, var(--ui-success))));
  --fc-ok-text: color-mix(in srgb, var(--ui-success) 78%, var(--ui-text-primary));
  --fc-bad-text: color-mix(in srgb, var(--dt-destructive) 72%, var(--ui-text-primary));
  --fc-warn-text: color-mix(in srgb, var(--fc-warn) 52%, var(--ui-text-primary));
  --fc-info-text: color-mix(in srgb, var(--ui-accent) 70%, var(--ui-text-primary));
}
.fc-wrap { container-type: inline-size; }
.fc-grid--6 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
@container (max-width: 700px) { .fc-grid--6 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@container (max-width: 420px) { .fc-grid--6 { grid-template-columns: minmax(0, 1fr); } }
.fc-kpi-vp { display: flex; flex-direction: column; min-width: 0; }
.fc-kpi-vp[data-flag="disagree"] { border-color: color-mix(in srgb, var(--fc-warn) 55%, var(--ui-stroke-secondary)); }
.fc-kpi-row { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px 8px; }
.fc-kpi-unit { font-size: 12px; font-weight: 400; color: var(--ui-text-secondary); margin-left: 4px; }
.fc-kpi-flag { display: inline-flex; align-items: center; gap: 4px; height: 20px; padding: 0 7px; border-radius: 999px; font-size: 11px; font-weight: 600; white-space: nowrap; color: var(--fc-warn-text); background: color-mix(in srgb, var(--fc-warn) 17%, transparent); border: 1px solid color-mix(in srgb, var(--fc-warn) 45%, transparent); }
.fc-kpi-bar { display: flex; gap: 2px; height: 4px; margin-top: 8px; border-radius: 2px; overflow: hidden; }
.fc-kpi-bar > span { min-width: 3px; border-radius: 2px; background: var(--ui-stroke-secondary); }
.fc-kpi-bar > [data-tone="ok"] { background: var(--ui-success); }
.fc-kpi-bar > [data-tone="bad"] { background: var(--dt-destructive); }
.fc-kpi-bar > [data-tone="warn"] { background: var(--fc-warn); }
.fc-kpi-bar > [data-tone="pending"] { background: repeating-linear-gradient(90deg, var(--ui-accent) 0 3px, transparent 3px 5px); }
.fc-kpi-break { display: flex; flex-wrap: wrap; gap: 2px 8px; margin-top: 5px; font-size: 11px; color: var(--ui-text-secondary); font-variant-numeric: tabular-nums; }
.fc-kpi-break b { font-weight: 600; color: var(--ui-text-primary); }
.fc-kpi-break [data-tone="ok"] b { color: var(--fc-ok-text); }
.fc-kpi-break [data-tone="bad"] b { color: var(--fc-bad-text); }
.fc-kpi-break [data-tone="warn"] b { color: var(--fc-warn-text); }

.fc-trend { margin: -4px 0 20px; }
.fc-trend-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 6px 12px; margin-bottom: 8px; }
.fc-trend-head h2 { margin: 0; font-size: 13px; font-weight: 600; }
.fc-trend-srcs { display: flex; flex-wrap: wrap; gap: 4px; }
.fc-trend-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
@container (max-width: 700px) { .fc-trend-grid { grid-template-columns: minmax(0, 1fr); } }
.fc-trend[data-stale="true"] .fc-trend-grid { opacity: 0.62; }
.fc-trend > .fc-alert { margin-bottom: 8px; }
.fc-spark { min-width: 0; display: flex; flex-direction: column; gap: 4px; padding: 10px 12px 9px; }
.fc-spark-top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; min-width: 0; }
.fc-spark-v { font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.2; }
.fc-spark-v small { font-size: 11px; font-weight: 400; color: var(--ui-text-secondary); margin-left: 6px; }
.fc-spark-delta { display: inline-flex; align-items: center; gap: 3px; font-family: var(--dt-font-mono); font-size: 11px; font-weight: 600; white-space: nowrap; color: var(--ui-text-secondary); }
.fc-spark-delta[data-tone="ok"] { color: var(--fc-ok-text); }
.fc-spark-delta[data-tone="bad"] { color: var(--fc-bad-text); }
.fc-spark-plot { position: relative; display: block; border-radius: 4px; cursor: crosshair; touch-action: none; }
.fc-spark-plot:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.fc-spark-svg { display: block; width: 100%; height: 56px; overflow: visible; }
.fc-spark-base { stroke: var(--ui-stroke-tertiary); stroke-width: 1; vector-effect: non-scaling-stroke; }
.fc-spark-line { fill: none; stroke: var(--ui-accent); stroke-width: 1.75; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
.fc-spark-line--2 { stroke: color-mix(in srgb, var(--ui-text-primary) 50%, var(--ui-text-secondary)); stroke-width: 1.4; stroke-dasharray: 3 3; }
.fc-spark[data-metric="escape"] .fc-spark-line { stroke: var(--fc-warn); }
.fc-spark-area { fill: color-mix(in srgb, var(--fc-warn) 14%, transparent); stroke: none; }
.fc-spark-end { fill: var(--ui-bg-elevated); stroke: currentColor; stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.fc-spark-cursor { stroke: var(--ui-stroke-primary, var(--ui-stroke-secondary)); stroke-width: 1; vector-effect: non-scaling-stroke; }
.fc-spark-hot { fill: currentColor; stroke: var(--ui-bg-elevated); stroke-width: 2; vector-effect: non-scaling-stroke; }
.fc-spark-legend { display: flex; flex-wrap: wrap; gap: 2px 10px; font-size: 10.5px; color: var(--ui-text-secondary); }
.fc-spark-legend i { display: inline-block; width: 12px; height: 0; margin-right: 4px; vertical-align: middle; border-top: 2px solid var(--ui-accent); }
.fc-spark-legend i[data-s="2"] { border-top: 1.5px dashed color-mix(in srgb, var(--ui-text-primary) 50%, var(--ui-text-secondary)); }
.fc-spark-read { margin-top: auto; min-height: 18px; display: flex; flex-wrap: wrap; align-items: center; gap: 2px 6px; font-size: 11px; color: var(--ui-text-secondary); font-variant-numeric: tabular-nums; }
.fc-spark-read b { font-family: var(--dt-font-mono); font-weight: 600; color: var(--ui-text-primary); }
.fc-spark-read .fc-trail-pill { height: 18px; font-size: 10.5px; padding: 0 6px; }
.fc-trend-skel { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
@container (max-width: 700px) { .fc-trend-skel { grid-template-columns: minmax(0, 1fr); } }
.fc-trend-skel > div { height: 132px; box-sizing: border-box; display: flex; flex-direction: column; gap: 8px; }
.fc-trend-skel span { display: block; height: 8px; border-radius: 4px; background: color-mix(in srgb, var(--ui-text-secondary) 20%, transparent); animation: fc-sb-pulse 1.6s ease-in-out infinite; }
.fc-trend-skel span.fc-skel-plot { height: 56px; margin-top: auto; background: color-mix(in srgb, var(--ui-text-secondary) 10%, transparent); }
.fc-trend-cmd { font-family: var(--dt-font-mono); font-size: 11px; padding: 2px 7px; border-radius: 5px; background: var(--ui-row-active-background); color: var(--ui-text-primary); }

.fc-vp { margin-bottom: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
.fc-vp-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; min-width: 0; }
.fc-vp-title { font-size: 12px; font-weight: 600; margin-right: 2px; }
.fc-vp-lbl { font-size: 11px; color: var(--ui-text-secondary); }
.fc-vp-src { margin-left: auto; font-family: var(--dt-font-mono); font-size: 10.5px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.fc-rigor { font-weight: 600; }
.fc-rigor[data-rigor="standard"] { border-color: var(--ui-stroke-primary, var(--ui-stroke-secondary)); color: var(--ui-text-primary); }
.fc-rigor[data-rigor="high"] { border-color: color-mix(in srgb, var(--ui-accent) 55%, transparent); background: color-mix(in srgb, var(--ui-accent) 15%, transparent); color: var(--fc-info-text); }
.fc-ready { display: inline-flex; align-items: center; gap: 4px; height: 20px; padding: 0 8px 0 6px; border-radius: 999px; font-size: 11px; font-weight: 600; white-space: nowrap; }
.fc-ready[data-tone="ok"] { color: var(--fc-ok-text); background: color-mix(in srgb, var(--ui-success) 15%, transparent); }
.fc-ready[data-tone="warn"] { color: var(--fc-warn-text); background: color-mix(in srgb, var(--fc-warn) 17%, transparent); }
.fc-ready[data-tone="muted"] { color: var(--ui-text-secondary); background: var(--ui-row-active-background); }
.fc-vp-members { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
.fc-vp-member { display: inline-flex; align-items: center; gap: 6px; min-width: 0; max-width: 100%; height: 26px; box-sizing: border-box; padding: 0 4px 0 8px; border: 1px solid var(--ui-stroke-tertiary); border-radius: 6px; background: var(--ui-chat-surface-background); }
.fc-vp-member .fc-mono { color: var(--ui-text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.fc-vp-idx { font-family: var(--dt-font-mono); font-size: 10px; color: var(--ui-text-secondary); }
.fc-vendor { flex: none; display: inline-flex; align-items: center; gap: 3px; height: 18px; padding: 0 6px; border-radius: 4px; font-family: var(--dt-font-mono); font-size: 10.5px; color: var(--ui-text-secondary); background: var(--ui-row-active-background); white-space: nowrap; }
.fc-vendor[data-dup="true"] { color: var(--fc-warn-text); background: color-mix(in srgb, var(--fc-warn) 17%, transparent); }
.fc-vp-warn { display: flex; align-items: flex-start; gap: 7px; padding: 7px 10px; border-radius: 6px; font-size: 12px; color: var(--fc-warn-text); border: 1px solid color-mix(in srgb, var(--fc-warn) 45%, transparent); background: color-mix(in srgb, var(--fc-warn) 10%, transparent); }
.fc-vp-warn svg { flex: none; margin-top: 2px; }
.fc-vp-warn ul { margin: 3px 0 0; padding-left: 16px; color: var(--ui-text-secondary); font-size: 11.5px; }
.fc-vp-empty { margin: 0; font-size: 11.5px; color: var(--ui-text-secondary); }
.fc-vp-skel { height: 8px; width: 60%; border-radius: 4px; background: color-mix(in srgb, var(--ui-text-secondary) 20%, transparent); animation: fc-sb-pulse 1.6s ease-in-out infinite; }

.fc-tag--set[data-rigor="high"] svg { flex: none; }

.fc-sb-dot[data-status="inconclusive"] { background: var(--fc-warn); }
.fc-sb-card[data-attn="warn"] { border-color: color-mix(in srgb, var(--fc-warn) 42%, var(--ui-stroke-secondary)); }
.fc-sb-vendor { flex: none; display: inline-flex; align-items: center; gap: 3px; max-width: 100%; font-family: var(--dt-font-mono); font-size: 10px; line-height: 16px; padding: 0 5px; border-radius: 4px; border: 1px solid var(--ui-stroke-secondary); color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc-sb-vendor[data-verdict="pass"] { color: var(--fc-ok-text); }
.fc-sb-vendor[data-verdict="reject"] { color: var(--fc-bad-text); border-color: color-mix(in srgb, var(--dt-destructive) 40%, var(--ui-stroke-secondary)); }
.fc-sb-vendor[data-verdict="inconclusive"] { color: var(--fc-warn-text); }
.fc-sb-lane { color: var(--ui-text-secondary); font-family: var(--dt-font-mono); font-size: 10.5px; }
.fc-sb-panel { margin: -2px 6px 8px; padding: 6px 7px; display: flex; flex-direction: column; gap: 5px; min-width: 0; border: 1px solid var(--ui-stroke-tertiary); border-radius: 6px; background: var(--ui-row-hover-background); }
.fc-sb-panel[data-disagree="true"] { border-color: color-mix(in srgb, var(--fc-warn) 45%, var(--ui-stroke-tertiary)); }
.fc-sb-panel-top { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 5px; min-width: 0; }
.fc-sb-panel-k { flex: none; display: inline-flex; color: var(--ui-text-secondary); }
.fc-sb-panel-id { flex: 0 1 auto; min-width: 0; font-size: 10.5px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc-sb-panel-lanes { margin-left: auto; flex: none; font-family: var(--dt-font-mono); font-size: 10.5px; color: var(--ui-text-secondary); }
.fc-sb-vendors { display: flex; flex-wrap: wrap; gap: 3px; min-width: 0; }
.fc-sb-callout[data-kind="disagree"] .fc-sb-callout-body { -webkit-line-clamp: 4; }
.fc-sb-callout[data-kind="disagree"] .fc-sb-callout-body q { color: var(--ui-text-primary); }
@media (prefers-reduced-motion: reduce) {
  .fc-trend-skel span, .fc-vp-skel { animation: none !important; }
}

/* v0.4.0 Wave 5 — workstream modes, /arena strip, 12-phase trail, harness badge: host tokens, transparent, currentColor and color-mix only. */
.fc-sb-ws-labelrow { display: flex; align-items: center; gap: 5px; min-width: 0; container-type: inline-size; }
.fc-sb-ws-labelrow > .fc-sb-ws-label { flex: 0 1 auto; min-width: 0; }
.fc-sb-mode { flex: none; display: inline-flex; align-items: center; gap: 3px; height: 16px; padding: 0 5px 0 4px; border: 1px solid transparent; border-radius: 4px; font-family: var(--dt-font-mono); font-size: 10px; font-weight: 500; line-height: 1; white-space: nowrap; color: var(--ui-text-secondary); background: var(--ui-row-active-background); }
.fc-sb-mode[data-mode="perf"] { color: var(--fc-info-text); background: color-mix(in srgb, var(--ui-accent) 14%, transparent); }
.fc-sb-mode[data-mode="investigate"] { color: var(--fc-info-text); background: transparent; border: 1px dashed color-mix(in srgb, var(--ui-accent) 55%, transparent); }
.fc-sb-mode-ro { color: var(--ui-text-secondary); }
@container (max-width: 230px) { .fc-sb-mode-ro { display: none; } }
.fc-sb-report { display: inline-flex; align-items: center; gap: 3px; font-weight: 500; }
.fc-sb-report[data-ready="true"] { color: var(--fc-sb-success-text); }
.fc-sb-report[data-ready="false"] { color: var(--ui-text-secondary); }
.fc-sb-arena { container-type: inline-size; }
.fc-sb-arena .fc-sb-panel-top { flex-wrap: nowrap; }
.fc-sb-arena .fc-sb-panel-id { flex: 1 1 24px; min-width: 0; }
.fc-sb-arena-k { flex: none; display: inline-flex; align-items: center; gap: 4px; font-size: 10.5px; font-weight: 600; color: var(--ui-text-primary); }
.fc-sb-arena-k .fc-sb-ico { color: var(--ui-text-secondary); }
@container (max-width: 270px) { .fc-sb-arena-word { display: none; } }
.fc-sb-arena-target { flex: none; font-family: var(--dt-font-mono); font-size: 10px; line-height: 14px; padding: 0 4px; border-radius: 4px; border: 1px solid var(--ui-stroke-primary, var(--ui-stroke-secondary)); color: var(--ui-text-primary); }
.fc-sb-vendor[data-run="ready"] { color: var(--fc-ok-text); }
.fc-sb-vendor[data-run="running"] { color: var(--fc-info-text); }
.fc-sb-vendor[data-run="no-artifact"] { color: var(--fc-warn-text); border-color: color-mix(in srgb, var(--fc-warn) 40%, var(--ui-stroke-secondary)); }
.fc-sb-vendor[data-run="failed"] { color: var(--fc-bad-text); border-color: color-mix(in srgb, var(--dt-destructive) 40%, var(--ui-stroke-secondary)); }
.fc-sb-vendor[data-role="judge"] { border-style: dashed; }
.fc-sb-vendor[data-role="judge"][data-cross="true"] { border-color: color-mix(in srgb, var(--ui-accent) 50%, transparent); }
.fc-sb-vendor[data-role="judge"][data-cross="false"] { color: var(--fc-warn-text); background: color-mix(in srgb, var(--fc-warn) 14%, transparent); }
.fc-sb-arena-tag { flex: 0 1 auto; min-width: 0; color: var(--fc-info-text); border-color: color-mix(in srgb, var(--ui-accent) 45%, transparent); }
/* Blocked arena callout: same 48% foreground mix as the amber callouts, so it stays legible in dark themes. */
.fc-sb-arena .fc-sb-callout[data-kind="blocked"] { color: color-mix(in srgb, var(--dt-destructive) 48%, var(--ui-text-primary)); }
/* Drawer Arena row: id, target and status wrap instead of crowding at 280px / 344px. */
.fc-sb-drawer dd[data-field="arena"] { flex-wrap: wrap; row-gap: 3px; }
.fc-sb-drawer dd[data-field="arena"] .fc-sb-mono { min-width: 0; overflow-wrap: anywhere; }
.fc-trail-pill[data-tone="warn"] { border-color: color-mix(in srgb, var(--fc-trail-warn) 50%, transparent); color: var(--fc-trail-warn-text); }
.fc-trail-pill[data-tone="info"] { border-color: color-mix(in srgb, var(--ui-accent) 45%, transparent); color: var(--fc-trail-info-text); }
.fc-sb-trail-ev .fc-sb-chip[data-tone="warn"] { color: var(--fc-sb-warn-text); border-color: color-mix(in srgb, var(--fc-sb-warn) 45%, transparent); }
.fc-sb-trail-ev .fc-sb-chip[data-tone="info"] { color: var(--fc-info-text); border-color: color-mix(in srgb, var(--ui-accent) 45%, transparent); }
@container (max-width: 640px) {
  .fc-trail-sel { flex: 1 1 220px; }
  .fc-trail-sel .fc-input { flex: 1; min-width: 0; max-width: none; }
}
.fc-harness { display: flex; width: fit-content; align-items: center; gap: 4px; max-width: 100%; height: 18px; margin-top: 4px; padding: 0 6px 0 5px; border-radius: 999px; border: 1px solid var(--ui-stroke-secondary); font-family: var(--dt-font-mono); font-size: 10.5px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; box-sizing: border-box; }
.fc-harness .fc-sb-ico { flex: none; }
.fc-harness-k { flex: none; font-family: var(--dt-font-sans); color: var(--ui-text-tertiary); }
.fc-harness-t { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.fc-harness[data-state="ok"] .fc-sb-ico { color: var(--fc-ok-text); }
.fc-harness[data-state="error"] { color: var(--fc-warn-text); border-color: color-mix(in srgb, var(--fc-warn) 50%, transparent); background: color-mix(in srgb, var(--fc-warn) 12%, transparent); font-weight: 600; }
.fc-harness[data-state="error"] .fc-harness-k { color: inherit; font-weight: 400; }
.fc-harness[data-state="absent"] { border-style: dashed; border-color: color-mix(in srgb, var(--ui-text-secondary) 38%, transparent); color: var(--ui-text-tertiary); }
/* Decision History + control plane (DESIGN.md d_889fc8b89d65: .fc-dh*, .fc-m*, .fc-au*, .fc-md, .fc-sb-appr*, .fc-sb-reeval): host tokens, transparent, currentColor and color-mix only. */
.fc-spacer { flex: 1 1 auto; }
.fc-dh .fc-btn, .fc-m .fc-btn, .fc-au .fc-btn, .fc-act-btns .fc-btn { display: inline-flex; align-items: center; gap: 6px; }
.fc-btn--s { height: 24px; padding: 0 8px; font-size: 11.5px; }
.fc-btn--primary:disabled { opacity: 1; background: color-mix(in srgb, var(--ui-accent) 10%, var(--ui-bg-elevated)); border-color: color-mix(in srgb, var(--ui-accent) 32%, transparent); color: var(--ui-text-secondary); }
textarea.fc-input { height: auto; padding: 6px 8px; resize: vertical; line-height: 1.45; }
.fc-chip { display: inline-flex; align-items: center; gap: 4px; max-width: 100%; height: 20px; box-sizing: border-box; padding: 0 7px; border-radius: 5px; border: 1px solid var(--ui-stroke-tertiary); background: transparent; font-size: 11px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-decoration: none; font-variant-numeric: tabular-nums; }
.fc-chip code, .fc-chip b { font-family: var(--dt-font-mono); color: var(--ui-text-primary); }
.fc-chip .fc-chip-k { color: var(--ui-text-tertiary); }
.fc-chip[data-ev] { font-family: var(--dt-font-mono); }
.fc-chip[data-ev] .fc-chip-k { font-family: var(--dt-font-sans); }
.fc-chip[data-tone="ok"] { color: var(--fc-ok-text); border-color: transparent; background: color-mix(in srgb, var(--ui-success) 14%, transparent); }
.fc-chip[data-tone="bad"] { color: var(--fc-bad-text); border-color: transparent; background: color-mix(in srgb, var(--dt-destructive) 14%, transparent); }
.fc-chip[data-tone="warn"] { color: var(--fc-warn-text); border-color: transparent; background: color-mix(in srgb, var(--fc-warn) 16%, transparent); }
.fc-chip[data-tone="info"] { color: var(--fc-info-text); border-color: transparent; background: color-mix(in srgb, var(--ui-accent) 14%, transparent); }
.fc-dh-mode { margin-left: 1px; padding-left: 5px; border-left: 1px solid var(--ui-stroke-secondary); color: var(--fc-info-text); font-family: var(--dt-font-sans); }
.fc-chip--muted { border-style: dashed; color: var(--ui-text-tertiary); }
a.fc-chip, button.fc-chip { cursor: pointer; font: inherit; font-family: var(--dt-font-mono); font-size: 11px; }
a.fc-chip:hover, button.fc-chip:hover { border-color: var(--ui-stroke-primary, var(--ui-stroke-secondary)); color: var(--ui-text-primary); }
.fc-seg { display: inline-flex; padding: 2px; gap: 2px; border-radius: 7px; border: 1px solid var(--ui-stroke-tertiary); background: var(--ui-row-hover-background); }
.fc-seg button { display: inline-flex; align-items: center; gap: 6px; height: 24px; padding: 0 10px; border: 0; border-radius: 5px; background: transparent; color: var(--ui-text-secondary); font: inherit; font-size: 12px; cursor: pointer; white-space: nowrap; }
.fc-seg button:hover:not(:disabled) { color: var(--ui-text-primary); }
.fc-seg button:disabled { opacity: 0.55; cursor: default; }
.fc-seg button[aria-pressed="true"] { background: var(--ui-chat-surface-background); color: var(--ui-text-primary); font-weight: 600; box-shadow: 0 0 0 1px var(--ui-stroke-secondary); }
.fc-count { min-width: 16px; height: 16px; box-sizing: border-box; padding: 0 4px; border-radius: 8px; font-size: 10px; line-height: 16px; text-align: center; font-weight: 700; font-family: var(--dt-font-mono); background: color-mix(in srgb, var(--fc-warn) 22%, transparent); color: var(--fc-warn-text); }
.fc-chk { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ui-text-secondary); cursor: pointer; white-space: nowrap; }
.fc-chk input { accent-color: var(--ui-accent); margin: 0; }
.fc-chk--wrap { align-items: flex-start; white-space: normal; line-height: 1.45; }
.fc-chk--wrap input { margin-top: 2px; }
.fc-chk code, .fc-m code, .fc-au code, .fc-sb-confirm code, .fc-sb-appr code { font-family: var(--dt-font-mono); font-size: 0.92em; }
.fc-banner { display: flex; gap: 8px; align-items: flex-start; padding: 8px 10px; border-radius: 7px; font-size: 12px; }
.fc-banner > svg { flex: none; margin-top: 2px; }
.fc-banner[data-tone="bad"] { background: color-mix(in srgb, var(--dt-destructive) 11%, transparent); border: 1px solid color-mix(in srgb, var(--dt-destructive) 35%, transparent); color: var(--fc-bad-text); }
.fc-banner[data-tone="warn"] { background: color-mix(in srgb, var(--fc-warn) 10%, transparent); border: 1px solid color-mix(in srgb, var(--fc-warn) 35%, transparent); color: var(--fc-warn-text); }
.fc-banner[data-tone="ok"] { background: color-mix(in srgb, var(--ui-success) 10%, transparent); border: 1px solid color-mix(in srgb, var(--ui-success) 32%, transparent); color: var(--fc-ok-text); }
.fc-banner[data-tone="info"] { background: color-mix(in srgb, var(--ui-accent) 9%, transparent); border: 1px solid color-mix(in srgb, var(--ui-accent) 30%, transparent); color: var(--fc-info-text); }
.fc-banner b { font-weight: 600; }
.fc-banner-body { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.fc-banner-detail { display: block; margin-top: 4px; font-family: var(--dt-font-mono, ui-monospace, monospace); font-size: 11px; opacity: .9; }

.fc-dh .fc-sec-head { align-items: flex-end; }
.fc-dh-status { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.fc-dh-status .fc-btn { height: 28px; }
.fc-dh-audit { margin-bottom: 8px; border: 1px solid color-mix(in srgb, var(--fc-warn) 38%, transparent); background: color-mix(in srgb, var(--fc-warn) 7%, transparent); border-radius: 8px; }
.fc-dh-audit-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; width: 100%; padding: 8px 12px; border: 0; background: transparent; text-align: left; cursor: pointer; color: var(--fc-warn-text); font: inherit; font-size: 12px; }
.fc-dh-audit-head b { color: var(--ui-text-primary); font-weight: 600; }
.fc-dh-audit-cats { display: inline-flex; flex-wrap: wrap; gap: 4px; }
.fc-dh-audit-chev { margin-left: auto; transform: rotate(90deg); transition: transform 160ms ease; }
.fc-dh-audit-head[aria-expanded="true"] .fc-dh-audit-chev { transform: rotate(-90deg); }
.fc-dh-audit-list { list-style: none; margin: 0; padding: 0 12px 10px; display: grid; gap: 6px; }
.fc-dh-audit-item { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 4px 10px; align-items: center; padding: 8px 10px; border-radius: 6px; background: var(--ui-chat-surface-background); border: 1px solid var(--ui-stroke-tertiary); }
.fc-dh-audit-item > .fc-chip { min-width: 58px; justify-content: center; }
.fc-dh-audit-item p { margin: 0; font-size: 12px; color: var(--ui-text-primary); overflow-wrap: anywhere; }
.fc-dh-audit-item small { display: block; color: var(--ui-text-tertiary); font-size: 11px; }
.fc-dh-bar { display: grid; gap: 8px; flex-wrap: nowrap; }
.fc-dh-row1, .fc-dh-row2 { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; min-width: 0; }
.fc-dh-search { position: relative; flex: 1 1 220px; min-width: 160px; display: flex; align-items: center; }
.fc-dh-search > svg { position: absolute; left: 8px; color: var(--ui-text-tertiary); pointer-events: none; }
.fc-dh-search .fc-input { width: 100%; padding-left: 26px; padding-right: 26px; }
.fc-dh-search button { position: absolute; right: 3px; width: 22px; height: 22px; border: 0; border-radius: 4px; background: transparent; color: var(--ui-text-tertiary); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; }
.fc-dh-search button:hover { color: var(--ui-text-primary); background: var(--ui-row-hover-background); }
.fc-dh .fc-trail-sel .fc-input { min-width: 0; max-width: 220px; }
.fc-dh-meta { margin-left: auto; font-size: 11px; color: var(--ui-text-secondary); font-variant-numeric: tabular-nums; white-space: nowrap; }
.fc-dh-scroll { max-height: 720px; overflow: auto; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; overscroll-behavior: contain; scrollbar-width: thin; scrollbar-color: var(--ui-stroke-primary, var(--ui-stroke-secondary)) transparent; }
.fc-dh-scroll > .fc-trail-state { border: 0; }
.fc-dh-day { position: sticky; top: 0; z-index: 2; display: flex; justify-content: space-between; padding: 5px 14px; font-size: 11px; font-weight: 600; letter-spacing: 0.03em; color: var(--ui-text-secondary); background: var(--ui-bg-elevated); border-bottom: 1px solid var(--ui-stroke-tertiary); }
.fc-dh-day span:last-child { font-weight: 400; color: var(--ui-text-tertiary); }
.fc-dh-list { list-style: none; margin: 0; padding: 0; }
.fc-dh-row { position: relative; border-bottom: 1px solid var(--ui-stroke-tertiary); scroll-margin: 40px; transition: background-color 160ms ease; }
.fc-dh-main { display: grid; grid-template-columns: 52px 78px minmax(0, 1fr) auto; gap: 2px 12px; align-items: start; padding: 10px 14px; cursor: pointer; }
.fc-dh-main:hover { background: var(--ui-row-hover-background); }
.fc-dh-row[data-tier="1"]::before, .fc-dh-row[data-human="true"]::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--dt-destructive); }
.fc-dh-row[data-accent="warn"]::before { background: var(--fc-warn); }
.fc-dh-row[data-human="true"]::before { background: var(--ui-accent); }
.fc-dh-row[data-tier="1"] .fc-dh-title { font-weight: 600; }
.fc-dh-row[data-tier="3"] .fc-dh-title { font-weight: 400; color: var(--ui-text-secondary); }
.fc-dh-row[data-superseded="true"] .fc-dh-title { color: var(--ui-text-secondary); text-decoration: line-through; text-decoration-color: var(--ui-text-tertiary); }
.fc-dh-row[data-flash="true"] { background: color-mix(in srgb, var(--ui-accent) 13%, transparent); }
.fc-dh-row[aria-expanded="true"] > .fc-dh-main { background: var(--ui-row-active-background); }
.fc-dh-time { font-family: var(--dt-font-mono); font-size: 11.5px; color: var(--ui-text-primary); font-variant-numeric: tabular-nums; padding-top: 1px; }
.fc-dh-time small { display: block; font-family: var(--dt-font-sans); font-size: 10.5px; color: var(--ui-text-tertiary); }
.fc-dh-phase { display: inline-block; width: 78px; box-sizing: border-box; height: 20px; line-height: 18px; padding: 0 6px; border-radius: 4px; font-family: var(--dt-font-mono); font-size: 10.5px; text-align: center; color: var(--ui-text-secondary); background: var(--ui-row-hover-background); border: 1px solid var(--ui-stroke-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fc-dh-phase[data-phase="escape"], .fc-dh-phase[data-phase="watchdog"] { color: var(--ui-text-primary); border-color: var(--ui-stroke-secondary); }
.fc-dh-body { min-width: 0; }
.fc-dh-title { margin: 0; font-size: 13px; font-weight: 500; color: var(--ui-text-primary); line-height: 20px; }
.fc-dh-actor { display: inline-flex; align-items: center; gap: 3px; margin-left: 6px; font-weight: 400; font-size: 11.5px; color: var(--ui-text-tertiary); text-decoration: none; }
.fc-dh-actor[data-actor="human"] { color: var(--fc-info-text); font-weight: 600; }
.fc-dh-why { margin: 1px 0 0; font-size: 12px; color: var(--ui-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fc-dh-row[aria-expanded="true"] .fc-dh-why { white-space: normal; }
.fc-dh-ev { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; min-width: 0; }
.fc-dh-end { display: flex; align-items: center; gap: 6px; }
.fc-dh-result { font-family: var(--dt-font-mono); font-weight: 600; }
.fc-dh-chev { width: 22px; height: 22px; border: 0; padding: 0; background: transparent; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; cursor: pointer; color: var(--ui-text-tertiary); }
.fc-dh-chev:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary); }
.fc-dh-chev svg { transition: transform 160ms ease; }
.fc-dh-row[aria-expanded="true"] .fc-dh-chev svg { transform: rotate(90deg); }
.fc-dh-chain { margin: -2px 14px 10px 168px; }
.fc-dh-chain-btn { display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 8px; border-radius: 5px; border: 1px dashed color-mix(in srgb, var(--ui-accent) 45%, var(--ui-stroke-primary, var(--ui-stroke-secondary))); background: transparent; color: var(--fc-info-text); font: inherit; font-size: 11.5px; cursor: pointer; }
.fc-dh-chain-btn:hover { background: color-mix(in srgb, var(--ui-accent) 10%, transparent); }
.fc-dh-chain-list { list-style: none; margin: 6px 0 0; padding: 0 0 0 12px; border-left: 2px solid var(--ui-stroke-secondary); display: grid; gap: 6px; }
.fc-dh-chain-item { font-size: 12px; color: var(--ui-text-secondary); overflow-wrap: anywhere; }
.fc-dh-chain-item s { text-decoration-color: var(--ui-text-tertiary); }
.fc-dh-chain-why, .fc-dh-chain-fix { display: block; }
.fc-dh-chain-why { color: var(--ui-text-tertiary); }
.fc-dh-arrow { color: var(--fc-info-text); font-family: var(--dt-font-mono); }
.fc-dh-foldwrap { border-bottom: 1px solid var(--ui-stroke-tertiary); }
.fc-dh-fold { display: grid; grid-template-columns: 52px 78px minmax(0, 1fr) auto; gap: 0 12px; align-items: center; width: 100%; padding: 6px 14px; border: 0; background: color-mix(in srgb, var(--ui-text-tertiary) 5%, transparent); color: var(--ui-text-secondary); font: inherit; font-size: 11.5px; text-align: left; cursor: pointer; }
.fc-dh-fold:hover { color: var(--ui-text-primary); background: var(--ui-row-hover-background); }
.fc-dh-fold-ico { display: inline-flex; justify-content: flex-end; color: var(--ui-text-tertiary); }
.fc-dh-fold-chev { transition: transform 160ms ease; }
.fc-dh-fold[aria-expanded="true"] .fc-dh-fold-chev { transform: rotate(90deg); }
.fc-dh-fold .fc-dh-phase { height: 18px; line-height: 16px; font-size: 10px; }
.fc-dh-fold-l { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fc-dh-fold b { font-weight: 600; color: var(--ui-text-primary); }
.fc-dh-fold .fc-mono { font-family: var(--dt-font-mono); font-variant-numeric: tabular-nums; }
.fc-dh-fold-x { color: var(--fc-info-text); }
.fc-dh-foldrows { border-top: 1px solid var(--ui-stroke-tertiary); background: color-mix(in srgb, var(--ui-text-tertiary) 3%, transparent); box-shadow: inset 3px 0 0 var(--ui-stroke-primary, var(--ui-stroke-secondary)); }
.fc-dh-foldrows > .fc-dh-row:last-child { border-bottom: 0; }
.fc-dh-insp { margin: 0 14px 12px 168px; border: 1px solid var(--ui-stroke-secondary); border-radius: 7px; background: var(--ui-bg-elevated); overflow: hidden; cursor: auto; }
.fc-dh-insp-tabs { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 6px 8px; border-bottom: 1px solid var(--ui-stroke-tertiary); }
.fc-dh-kv { display: grid; grid-template-columns: 152px minmax(0, 1fr); margin: 0; font-size: 12px; }
.fc-dh-kv dt, .fc-dh-kv dd { margin: 0; padding: 5px 10px; border-bottom: 1px solid var(--ui-stroke-tertiary); }
.fc-dh-kv dt { color: var(--ui-text-secondary); font-family: var(--dt-font-mono); font-size: 11px; }
.fc-dh-kv dd { font-family: var(--dt-font-mono); font-size: 11.5px; overflow-wrap: anywhere; }
.fc-dh-kv dd.fc-dh-prose { font-family: var(--dt-font-sans); font-size: 12px; }
.fc-dh-json { margin: 0; padding: 10px; font-family: var(--dt-font-mono); font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 260px; overflow: auto; }
.fc-dh-skel { display: grid; grid-template-columns: 52px 78px minmax(0, 1fr); gap: 12px; padding: 14px; border-bottom: 1px solid var(--ui-stroke-tertiary); }
.fc-dh-skel span { display: block; height: 10px; border-radius: 4px; background: color-mix(in srgb, var(--ui-text-secondary) 18%, transparent); animation: fc-sb-pulse 1.6s ease-in-out infinite; }
.fc-dh-skel div { display: flex; flex-direction: column; gap: 8px; }
.fc-dh-more { display: flex; justify-content: center; padding: 10px; }

.fc-m-scrim { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center; justify-content: center; padding: 16px; background: color-mix(in srgb, var(--ui-bg-chrome, var(--ui-chat-surface-background)) 72%, transparent); }
.fc-m { width: min(620px, 100%); max-height: calc(100vh - 32px); display: flex; flex-direction: column; border: 1px solid var(--ui-stroke-primary, var(--ui-stroke-secondary)); border-radius: 10px; background: var(--ui-bg-elevated); color: var(--ui-text-primary); font-size: 13px; line-height: 1.45; text-align: left; box-shadow: 0 18px 48px color-mix(in srgb, var(--ui-bg-chrome, var(--ui-chat-surface-background)) 60%, transparent); outline: none; }
.fc-m[data-size="wide"] { width: min(780px, 100%); }
.fc-m-h { display: flex; align-items: flex-start; gap: 10px; padding: 14px 16px 10px; border-bottom: 1px solid var(--ui-stroke-tertiary); }
.fc-m-ht { flex: 1; min-width: 0; }
.fc-m-h h2 { margin: 0; font-size: 15px; font-weight: 600; }
.fc-m-h p { margin: 2px 0 0; font-size: 12px; color: var(--ui-text-secondary); }
.fc-m-guard { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.fc-m-x { width: 28px; height: 28px; flex: none; border: 0; border-radius: 6px; background: transparent; color: var(--ui-text-secondary); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; }
.fc-m-x:hover:not(:disabled) { background: var(--ui-row-hover-background); color: var(--ui-text-primary); }
.fc-m-b { padding: 14px 16px; overflow: auto; display: grid; gap: 12px; }
.fc-m-f { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 16px 14px; border-top: 1px solid var(--ui-stroke-tertiary); }
.fc-m-f .fc-btn { height: 28px; }
.fc-m-form { display: grid; gap: 12px; }
.fc-m-grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
@media (max-width: 520px) { .fc-m-grid2 { grid-template-columns: minmax(0, 1fr); } }
.fc-m-facts { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 6px 12px; margin: 0; padding: 10px 12px; border: 1px solid var(--ui-stroke-tertiary); border-radius: 7px; background: var(--ui-chat-surface-background); font-size: 12px; }
.fc-m-facts dt { color: var(--ui-text-secondary); }
.fc-m-facts dd { margin: 0; min-width: 0; overflow-wrap: anywhere; display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.fc-m-quote { margin: 0; padding: 8px 10px; border-left: 3px solid var(--ui-stroke-primary, var(--ui-stroke-secondary)); background: var(--ui-chat-surface-background); border-radius: 0 6px 6px 0; font-size: 12px; color: var(--ui-text-secondary); overflow-wrap: anywhere; }
.fc-m-quote b { color: var(--ui-text-primary); font-weight: 600; }
.fc-m-note { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 11.5px; color: var(--ui-text-secondary); }
.fc-m-shield { display: flex; gap: 6px; align-items: flex-start; margin: 0; font-size: 12px; color: var(--ui-text-primary); }
.fc-m-shield svg { flex: none; margin-top: 3px; color: var(--fc-info-text); }
.fc-m-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.fc-m-report { display: grid; gap: 10px; }
.fc-m-skel { display: grid; gap: 10px; }
.fc-m-skel span { display: block; height: 10px; border-radius: 4px; background: color-mix(in srgb, var(--ui-text-secondary) 18%, transparent); animation: fc-sb-pulse 1.6s ease-in-out infinite; }
.fc-md { font-size: 13px; line-height: 1.6; color: var(--ui-text-primary); overflow-wrap: anywhere; }
.fc-md h1 { font-size: 18px; margin: 0 0 8px; }
.fc-md h2 { font-size: 15px; margin: 18px 0 6px; }
.fc-md h3 { font-size: 13.5px; margin: 14px 0 4px; }
.fc-md h4 { font-size: 12.5px; margin: 12px 0 4px; color: var(--ui-text-secondary); }
.fc-md > :first-child { margin-top: 0; }
.fc-md p { margin: 0 0 10px; }
.fc-md ul, .fc-md ol { margin: 0 0 10px; padding-left: 20px; }
.fc-md code { font-family: var(--dt-font-mono); font-size: 0.92em; padding: 1px 4px; border-radius: 4px; background: var(--ui-row-hover-background); }
.fc-md pre { margin: 0 0 10px; padding: 10px; border-radius: 6px; background: var(--ui-chat-surface-background); border: 1px solid var(--ui-stroke-tertiary); overflow: auto; white-space: pre; }
.fc-md pre code { padding: 0; background: transparent; }
.fc-md blockquote { margin: 0 0 10px; padding: 4px 12px; border-left: 3px solid var(--ui-stroke-primary, var(--ui-stroke-secondary)); color: var(--ui-text-secondary); }
.fc-md blockquote p { margin: 0; }
.fc-md a { color: var(--fc-info-text); }
.fc-md hr { border: 0; border-top: 1px solid var(--ui-stroke-secondary); margin: 14px 0; }
.fc-md-table { overflow-x: auto; margin: 0 0 10px; }
.fc-md table { border-collapse: collapse; font-size: 12px; }
.fc-md th, .fc-md td { border: 1px solid var(--ui-stroke-secondary); padding: 4px 8px; text-align: left; vertical-align: top; }
.fc-md th { background: var(--ui-row-hover-background); }

.fc-repos td.fc-act { overflow: visible; }
.fc-act-btns { display: flex; flex-wrap: nowrap; align-items: center; justify-content: flex-end; gap: 4px; }
.fc-act-btns .fc-harness { flex: 0 1 auto; min-width: 22px; margin: 0 auto 0 0; }
.fc-act-btns .fc-harness-k { display: none; }
.fc-act-btns .fc-harness-t { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.fc-act-btns .fc-btn { flex: none; }
.fc-act-btns .fc-btn { height: 24px; }
.fc-repos tr[data-audit="true"] > td { background: var(--ui-row-active-background); border-bottom-color: transparent; }
.fc-repos tr[data-audit="true"] > td:first-child { box-shadow: inset 2px 0 0 var(--ui-accent); }
.fc-repos tr.fc-au-row:hover > td { background: var(--ui-bg-elevated); }
.fc-au-row > td { padding: 0; white-space: normal; background: var(--ui-bg-elevated); box-shadow: inset 2px 0 0 var(--ui-accent); }
.fc-au { padding: 12px 14px; display: grid; gap: 10px; }
.fc-au-h { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; font-size: 12px; }
.fc-au-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
.fc-au-grid section { border: 1px solid var(--ui-stroke-tertiary); border-radius: 6px; padding: 8px 10px; background: var(--ui-chat-surface-background); min-width: 0; }
.fc-au-grid section.fc-au-full { grid-column: 1 / -1; }
.fc-au-grid h4 { margin: 0 0 4px; font-size: 11px; font-weight: 600; color: var(--ui-text-secondary); text-transform: uppercase; letter-spacing: 0.04em; }
.fc-au-grid ul { margin: 0; padding-left: 16px; font-size: 12px; overflow-wrap: anywhere; }
.fc-au-grid li { margin: 2px 0; }
.fc-au-none { margin: 0; font-size: 12px; color: var(--ui-text-tertiary); }

.fc-sb-panel { container-type: inline-size; }
.fc-sb-reeval { flex: none; height: 20px; padding: 0 6px; font-size: 10.5px; color: var(--ui-text-primary); }
.fc-sb-reeval:disabled { cursor: default; opacity: 0.7; }
.fc-sb-reeval .fc-spin { width: 9px; height: 9px; margin-right: 0; }
@container (max-width: 270px) { .fc-sb-reeval-l { display: none; } }
.fc-sb-panel-top > .fc-sb-reeval { margin-left: 0; }
.fc-sb-arena .fc-sb-panel-id { flex: 0 0 auto; }
.fc-sb-arena .fc-sb-panel-lanes { flex: 1 1 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@container (max-width: 340px) { .fc-sb-arena .fc-sb-reeval-l, .fc-sb-arena .fc-sb-arena-target { display: none; } }
@container (max-width: 270px) { .fc-sb-arena .fc-sb-panel-lanes { display: none; } }
.fc-sb-strip-msg { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; font-size: 10.5px; color: var(--ui-text-secondary); }
.fc-sb-strip-msg[data-tone="ok"] { color: var(--fc-ok-text); }
.fc-sb-strip-msg[data-tone="bad"] { color: var(--fc-bad-text); }
.fc-sb-linkbtn { border: 0; padding: 0; background: transparent; color: var(--fc-info-text) !important; cursor: pointer; font: inherit; text-decoration: underline; text-underline-offset: 2px; }
.fc-sb-report-btn { height: 16px; padding: 0 4px 0 3px; border: 1px solid color-mix(in srgb, var(--ui-success) 55%, transparent); border-radius: 4px; background: color-mix(in srgb, var(--ui-success) 9%, transparent); font: inherit; font-size: 10.5px; cursor: pointer; }
.fc-sb-report-btn:hover { background: color-mix(in srgb, var(--ui-success) 18%, transparent); border-color: var(--ui-success); }
.fc-sb-pub { height: 22px; font-size: 11px; }
.fc-sb-appr { margin: 0 8px 6px; padding: 6px 7px; display: grid; gap: 6px; border: 1px solid color-mix(in srgb, var(--fc-warn) 38%, var(--ui-stroke-tertiary)); border-radius: 6px; background: color-mix(in srgb, var(--fc-warn) 6%, transparent); font-size: 11px; }
.fc-sb-appr[data-state="approved"], .fc-sb-appr[data-state="revising"] { border-color: var(--ui-stroke-tertiary); background: transparent; }
.fc-sb-appr-top { display: flex; align-items: center; gap: 4px; }
.fc-sb-appr-a, .fc-sb-confirm-a { display: flex; flex-wrap: wrap; gap: 6px; }
.fc-sb-appr .fc-banner { padding: 6px 8px; font-size: 11px; }
.fc-sb-confirm { display: grid; gap: 6px; padding: 7px; border-radius: 6px; border: 1px solid color-mix(in srgb, var(--ui-accent) 35%, var(--ui-stroke-tertiary)); background: color-mix(in srgb, var(--ui-accent) 6%, transparent); }
.fc-sb-confirm label { display: grid; gap: 3px; font-size: 11px; color: var(--ui-text-secondary); }
.fc-sb-confirm textarea.fc-input { font-size: 11.5px; }
.fc-sb-confirm-note { display: flex; align-items: flex-start; gap: 4px; font-size: 10.5px; color: var(--ui-text-secondary); overflow-wrap: anywhere; }
.fc-sb-confirm-note svg { flex: none; margin-top: 2px; }
.fc-sb .fc-sb-btn:disabled { opacity: 0.6; cursor: default; }

@container (max-width: 760px) {
  .fc-dh-main { grid-template-columns: 44px minmax(0, 1fr) auto; }
  .fc-dh-main .fc-dh-phase { grid-column: 2; grid-row: 1; width: auto; justify-self: start; }
  .fc-dh-main .fc-dh-body { grid-column: 2; grid-row: 2; }
  .fc-dh-main .fc-dh-end { grid-column: 3; grid-row: 1 / span 2; }
  .fc-dh-fold { grid-template-columns: 44px auto minmax(0, 1fr) auto; }
  .fc-dh-fold .fc-dh-phase { width: auto; }
  .fc-dh-chain, .fc-dh-insp { margin-left: 70px; }
  .fc-dh .fc-trail-sel .fc-input { max-width: 160px; }
}
@container (max-width: 640px) {
  .fc-dh-chain, .fc-dh-insp { margin-left: 14px; }
  .fc-dh-fold { grid-template-columns: auto auto minmax(0, 1fr); }
  .fc-dh-fold-x { display: none; }
  .fc-dh-kv { grid-template-columns: minmax(0, 1fr); }
  .fc-dh-kv dt { border-bottom: 0; padding-bottom: 0; }
  .fc-dh-meta { margin-left: 0; }
  .fc-dh .fc-seg button { padding: 0 8px; }
}
@media (prefers-reduced-motion: reduce) {
  .fc-dh *, .fc-dh-row, .fc-m *, .fc-sb-appr * { transition: none !important; }
  .fc-dh-skel span, .fc-m-skel span { animation: none !important; }
}
`

function fmtDur(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return '—'
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

function fmtPct(ratio) {
  if (ratio == null || Number.isNaN(ratio)) return '—'
  return `${Math.round(ratio * 100)}%`
}

function PhaseModelsTable({ models }) {
  const map = models || {}
  return jsxs('table', {
    className: 'fc-table',
    children: [
      jsx('thead', {
        children: jsxs('tr', {
          children: [
            jsx('th', { children: 'Phase' }),
            jsx('th', { children: 'Provider / Model' }),
            jsx('th', { children: 'Source' }),
            jsx('th', { children: '' })
          ]
        })
      }),
      jsx('tbody', {
        children: PHASE_ORDER.map(phase => {
          const info = map[phase] || {}
          return jsxs('tr', {
            key: phase,
            children: [
              jsx('td', { className: 'fc-mono', children: phase }),
              jsx('td', { className: 'fc-mono', children: formatPhaseModelLabel(info) }),
              jsx('td', { className: 'fc-mono', children: info.source || '—' }),
              jsx('td', {
                style: { textAlign: 'right' },
                children: jsx('button', {
                  type: 'button',
                  className: 'fc-btn',
                  title: `Configure the ${phase} phase model in Auxiliary Models`,
                  'aria-label': `Configure ${phase} model`,
                  onClick: () => host.navigate(auxModelRoute(phase)),
                  children: 'Configure'
                })
              })
            ]
          })
        })
      })
    ]
  })
}

function ConfigurePhaseModelsButton() {
  return jsx('button', {
    type: 'button',
    className: 'fc-btn',
    onClick: () => host.navigate(DEFAULT_AUX_MODEL_ROUTE),
    children: 'Configure phase models'
  })
}

// Shared explanatory note: always rendered (independent of /models query
// state) on both the Settings landing page and the /factonio dashboard.
function AuxModelsNote({ suffix }) {
  return jsxs('span', {
    className: 'fc-aux-note',
    children: [
      'Factonio phase models (define, design, implement, verify, watchdog) are configured in ',
      jsx('strong', { children: 'Hermes Auxiliary Models' }),
      ' (Settings → Model → Auxiliary).',
      suffix || null
    ]
  })
}

// Compact, non-blocking status for the /models query.
function ModelsQueryStatus({ query }) {
  if (query?.isError) {
    return jsx('div', {
      className: 'fc-settings-err',
      children: 'Could not load resolved phase models; showing defaults. Configuration links still work.'
    })
  }
  if (query?.isLoading) {
    return jsx('div', { className: 'fc-settings-err', children: 'Loading resolved phase models…' })
  }
  return null
}

function FactonioSettingsPage({ ctx }) {
  const modelsQ = useQuery({
    queryKey: ['factonio', 'models'],
    queryFn: () => ctx.rest('/models'),
    refetchInterval: 30000
  })
  const models = modelsQ.data?.models || {}
  const warn = modelsQ.data?.cross_model_warning

  return jsxs('div', {
    className: 'fc-settings',
    children: [
      jsxs('div', {
        className: 'fc-settings-note',
        children: [
          jsx(AuxModelsNote, { suffix: ' Plugin options are under Agent settings.' }),
          jsx(ConfigurePhaseModelsButton, {})
        ]
      }),
      warn ? jsx('div', { className: 'fc-warn', children: warn }) : null,
      jsx(ModelsQueryStatus, { query: modelsQ }),
      jsx(PhaseModelsTable, { models })
    ]
  })
}

// ---------------------------------------------------------------------------
// Registered Repositories editor (approved Phase 1b design: design/DESIGN.md).
// GET /repos feeds the table; PATCH /repos/{slug} saves only changed fields.
// ---------------------------------------------------------------------------

const MERGE_POLICY_OPTIONS = ['auto', 'ask']
const PUBLISH_POLICY_OPTIONS = ['auto', 'ask']
const POLICY_TITLES = {
  merge_policy: { ask: 'ask: waits for your approval to merge', auto: 'auto: merges automatically' },
  publish_policy: { ask: 'ask: waits for your approval to publish', auto: 'auto: publishes automatically' }
}
const DESIGN_PROVIDER_OPTIONS = ['auto', 'claude-design', 'hermes', 'off']
const DESIGN_APPROVAL_OPTIONS = ['ask', 'auto']
const RIGOR_OPTIONS = [['standard', 'Standard'], ['high', 'High (multi-vendor panel)']]
const RIGOR_HINT = {
  standard: 'Single cross-model diff check + black-box check.',
  high: 'Multi-vendor panel: needs ≥2 verify models from distinct vendor families; fails closed on disagreement.'
}
const REPO_FIELD_ORDER = ['default_branch', 'merge_policy', 'publish_policy', 'max_fix_rounds', 'design_provider', 'design_approval', 'default_rigor']
const REPOS_QUERY_KEY = ['factonio', 'repos']

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`
const controlChars = s => [...s].some(ch => ch.charCodeAt(0) < 32 || ch.charCodeAt(0) === 127)

// Pure client-side validator (mirrors repos.py `_validate_default_branch` /
// `_validate_max_fix_rounds`). Returns `{ field: message }`; empty when valid.
function validateRepoDraft(draft) {
  const d = draft || {}
  const errors = {}
  const branch = String(d.default_branch ?? '').trim()
  if (!branch) errors.default_branch = 'Branch is required.'
  else if (/\s/.test(branch)) errors.default_branch = 'Branch can’t contain spaces.'
  else if (branch.includes('..')) errors.default_branch = 'Branch can’t contain “..”.'
  else if (branch.startsWith('-')) errors.default_branch = 'Branch can’t start with “-”.'
  else if (controlChars(branch)) errors.default_branch = 'Branch has invalid characters.'

  const roundsErr = validateRounds(d.max_fix_rounds)
  if (roundsErr) errors.max_fix_rounds = roundsErr
  return errors
}

// Max fix rounds must be a canonical base-10 integer in [1, Number.MAX_SAFE_INTEGER]
// so `Number(...)` in the PATCH body is exact (no silent rounding / Infinity -> null).
const ROUNDS_WHOLE_MSG = 'Must be a whole number, 1 or more.'
const ROUNDS_RANGE_MSG = `Must be ${Number.MAX_SAFE_INTEGER} or less.`
function validateRounds(rounds) {
  let big
  if (typeof rounds === 'number') {
    if (!Number.isInteger(rounds) || rounds < 1) return ROUNDS_WHOLE_MSG
    if (!Number.isSafeInteger(rounds)) return ROUNDS_RANGE_MSG
    return null
  }
  if (typeof rounds !== 'string') return ROUNDS_WHOLE_MSG
  const t = rounds.trim()
  if (!/^[0-9]+$/.test(t)) return ROUNDS_WHOLE_MSG
  try { big = BigInt(t) } catch { return ROUNDS_WHOLE_MSG }
  if (big < 1n) return ROUNDS_WHOLE_MSG
  if (big > BigInt(Number.MAX_SAFE_INTEGER)) return ROUNDS_RANGE_MSG
  return null
}

const isPolicy = v => v === 'auto' || v === 'ask'

// Older backends omit `publish_policy`: inherit the global default (auto when that is
// omitted too). An explicit null / invalid value means the server could not resolve a
// valid policy, so it resolves to '' (unknown) and is never presented as auto.
function resolvePublishPolicy(repo, defaults) {
  const own = repo?.publish_policy
  if (own !== undefined) return isPolicy(own) ? own : ''
  const global = defaults?.publish_policy
  if (global === undefined) return 'auto'
  return isPolicy(global) ? global : ''
}

function seedRepoDraft(repo, defaults) {
  return {
    default_branch: String(repo.default_branch ?? ''),
    merge_policy: String(repo.merge_policy ?? 'auto'),
    publish_policy: resolvePublishPolicy(repo, defaults),
    max_fix_rounds: repo.max_fix_rounds == null ? '' : String(repo.max_fix_rounds),
    design_provider: repo.design_provider ? String(repo.design_provider) : '',
    design_approval: repo.design_approval ? String(repo.design_approval) : '',
    default_rigor: repo.default_rigor ? String(repo.default_rigor) : ''
  }
}

// Normalized draft values used for change detection (trimmed text, '' overrides -> null,
// safe-integer rounds compared numerically so '007' equals 7).
function normalizeRepoDraft(d) {
  const rounds = String(d.max_fix_rounds ?? '').trim()
  const n = /^[0-9]+$/.test(rounds) ? Number(rounds) : NaN
  return {
    default_branch: String(d.default_branch ?? '').trim(),
    merge_policy: String(d.merge_policy ?? ''),
    publish_policy: String(d.publish_policy ?? ''),
    max_fix_rounds: Number.isSafeInteger(n) ? String(n) : rounds,
    design_provider: d.design_provider || null,
    design_approval: d.design_approval || null,
    default_rigor: d.default_rigor || null
  }
}

// The PATCH body: only fields present in `edits` (the fields the user explicitly
// changed) whose normalized value differs from the current live `repo`. Fields the
// user never touched are never sent, so background refreshes are not reverted; an
// explicit choice equal to an older value is still sent once `repo` has moved on.
function repoDraftChanges(edits, repo, defaults) {
  const own = edits || {}
  const next = normalizeRepoDraft({ ...seedRepoDraft(repo || {}, defaults), ...own })
  const base = normalizeRepoDraft(seedRepoDraft(repo || {}, defaults))
  const body = {}
  for (const k of REPO_FIELD_ORDER) {
    if (!Object.prototype.hasOwnProperty.call(own, k) || next[k] === base[k]) continue
    body[k] = k === 'max_fix_rounds' ? Number(next[k]) : next[k]
  }
  return body
}

// ctx.rest rejects with an Error whose message carries "<status>: <body>" (possibly
// behind an IPC prefix). Returns { status, detail } or { status: null } for network errors.
function describeSaveError(err) {
  const e = err && typeof err === 'object' ? err : {}
  let status = Number(e.statusCode ?? e.status ?? e.response?.status)
  let body = e.body ?? e.data ?? e.response?.data
  const msg = String(e.message ?? err ?? '')
  const m = /(?:^|[^0-9])([1-5][0-9]{2}):\s?([\s\S]*)$/.exec(msg)
  if (!(status >= 100) && m) status = Number(m[1])
  if (body == null && m) body = m[2]
  if (!(status >= 100)) return { status: null, detail: '' }
  let detail = body
  if (typeof detail === 'string') {
    const t = detail.trim()
    try { detail = JSON.parse(t) } catch { detail = t }
  }
  // FastAPI nests structured refusals ({detail: {error, ...}}); unwrap up to two levels.
  for (let i = 0; i < 2 && detail && typeof detail === 'object' && !Array.isArray(detail); i++) detail = detail.detail ?? detail.error ?? detail.message ?? ''
  if (Array.isArray(detail)) detail = detail.map(x => (x && typeof x === 'object' ? x.msg || JSON.stringify(x) : String(x))).join('; ')
  detail = String(detail ?? '').trim()
  return { status, detail: detail || `HTTP ${status}` }
}

function providerHint(draft, repo, defaults, autoResolved) {
  const globalProvider = defaults?.design_provider || 'auto'
  const target = draft.design_provider || globalProvider
  const mono = v => jsx('span', { className: 'fc-mono', children: v })
  if (target === 'off') return 'Design phase is skipped.'
  if (draft.design_provider && draft.design_provider !== 'auto') {
    return ['Resolves to ', mono(draft.design_provider), ' (overrides global ', mono(globalProvider), ').']
  }
  if (target === 'auto') {
    const resolved = repo.effective_design_provider === 'auto' && repo.resolved_design_provider
      ? repo.resolved_design_provider
      : autoResolved
    if (resolved) return ['Currently resolves to ', mono(resolved), '.']
    return 'Picks a provider automatically at run time.'
  }
  return ['Currently resolves to ', mono(target), '.']
}

function rigorHint(draft, defaults) {
  const effective = draft.default_rigor || defaults?.default_rigor || 'standard'
  const base = RIGOR_HINT[effective] || `Rigor ${effective}.`
  return draft.default_rigor ? base : `${base} (inherited)`
}

function DesignTag({ value, effective, resolved }) {
  if (value) {
    return jsx('span', { className: 'fc-tag fc-tag--set', title: 'Repository override', children: String(value) })
  }
  return jsxs('span', {
    className: 'fc-tag fc-tag--inherit',
    title: resolved ? `Inherited from global default; resolves to ${resolved}` : 'Inherited from global default',
    children: [String(effective ?? '—'), ' ', jsx('span', { className: 'fc-tag-src', children: '· global' })]
  })
}

function PanelGlyph({ size = 11 }) {
  return jsx(Icon, { name: 'panel', size })
}

function RigorTag({ value, effective }) {
  if (!value) return jsx(DesignTag, { value: null, effective })
  const high = value === 'high'
  return jsxs('span', {
    className: 'fc-tag fc-tag--set',
    'data-rigor': String(value),
    title: high ? 'Repository override · multi-vendor panel' : 'Repository override',
    children: [high ? jsx(PanelGlyph, { key: 'g' }) : null, String(value)]
  })
}

// Policies are non-clearable per-repo values, so they render as plain text with a
// dot (ask) / ring (auto) instead of the inherited/override `.fc-tag` badges.
function PolicyCell({ field, value }) {
  const v = value == null || value === '' ? null : String(value)
  return jsx('td', {
    'data-field': field,
    children: jsx('span', {
      className: 'fc-pol',
      'data-pol': v ?? undefined,
      title: (v && POLICY_TITLES[field]?.[v]) || `${field.replace('_', ' ')}: ${v ?? 'unknown'}`,
      children: v ?? '—'
    })
  })
}

// Read-only `.factonio/` harness badge from GET /repos `harness`; null (render nothing) when an older
// backend omits it. A malformed manifest is checked first so it never reads as healthy.
const HARNESS_ICON = { ok: 'shieldOk', error: 'attention', absent: 'shield' }
function summarizeHarnessBadge(h) {
  if (!h || typeof h !== 'object' || Array.isArray(h)) return null
  const n = v => (Number.isSafeInteger(v) && v >= 0 ? v : 0)
  const one = (title, sr) => ({ title: title.join('\n'), sr })
  if (h.has_manifest_error === true) {
    return {
      state: 'error', key: 'harness', text: 'error',
      ...one(['Harness manifest error', '.factonio/harness.json is malformed or uses an unsupported schema_version.', 'Hook counts are not trusted until it is fixed.', 'Run: hermes factonio harness check'],
        'Harness manifest error: .factonio/harness.json is malformed or uses an unsupported schema_version; run hermes factonio harness check.')
    }
  }
  if (h.present === true && (h.mode === 'verify_md_only' || h.mode === 'markdown_only')) {
    return { state: 'ok', key: 'harness', text: 'md only', ...one(['Harness · markdown only', '.factonio/verify.md present, no harness.json hooks'], 'Harness markdown only: verify.md present, no hooks declared.') }
  }
  if (h.present === true) {
    const req = n(h.required_hooks)
    const opt = n(h.optional_hooks)
    const frozen = n(h.frozen_visual_hooks)
    const mode = typeof h.mode === 'string' && /^[a-z_]{1,24}$/.test(h.mode) ? h.mode : 'unknown'
    const text = plural(req + opt, 'hook', 'hooks') + (frozen > 0 ? ` · ${frozen} frozen` : '')
    return {
      state: 'ok', key: 'harness', text,
      ...one([
        `Harness · ${mode} mode`,
        `${req} required + ${opt} optional hooks`,
        `${n(h.invariant_hooks)} invariant · ${n(h.visual_diff_hooks)} visual_diff (${plural(frozen, 'frozen baseline', 'frozen baselines')})`,
        'Read from .factonio/harness.json'
      ], `Harness ${mode} mode: ${req} required and ${opt} optional hooks, ${n(h.invariant_hooks)} invariant, ${n(h.visual_diff_hooks)} visual diff, ${frozen} frozen.`)
    }
  }
  return { state: 'absent', key: null, text: 'no harness', ...one(['No .factonio/ harness in this repository.', 'Verification falls back to repository defaults.'], 'No harness: verification falls back to repository defaults.') }
}

function HarnessBadge({ harness }) {
  const b = summarizeHarnessBadge(harness)
  if (!b) return null
  return jsxs('span', {
    className: 'fc-harness',
    'data-state': b.state,
    title: b.title,
    children: [
      jsx(Icon, { key: 'i', name: HARNESS_ICON[b.state], size: 11 }),
      b.key ? jsx('span', { key: 'k', className: 'fc-harness-k', 'aria-hidden': 'true', children: b.key }) : null,
      jsx('span', { key: 't', className: 'fc-harness-t', 'aria-hidden': 'true', children: b.text }),
      jsx('span', { key: 'sr', className: 'fc-sr', children: b.sr })
    ]
  })
}

function RepoRow({ repo: r, defaults, editing, busy, editorId, onEdit, buttonRef, audit, auditId, onAudit }) {
  const auditing = audit?.status === 'busy'
  return jsxs('tr', {
    'data-slug': r.slug,
    'data-editing': editing ? 'true' : undefined,
    'data-audit': audit ? 'true' : undefined,
    children: [
      jsxs('td', {
        children: [
          jsx('span', { className: 'fc-trunc fc-repo-name', title: r.repo || r.slug, children: r.repo || r.slug }),
          jsx('span', { className: 'fc-trunc fc-repo-path', title: r.path || '', children: r.path || '—' })
        ]
      }),
      jsx('td', { children: jsx('span', { className: 'fc-trunc fc-mono', title: r.default_branch || '', children: r.default_branch || '—' }) }),
      jsx(PolicyCell, { field: 'merge_policy', value: r.merge_policy }),
      jsx(PolicyCell, { field: 'publish_policy', value: resolvePublishPolicy(r, defaults) }),
      jsx('td', { className: 'fc-num', children: r.max_fix_rounds ?? '—' }),
      jsx('td', {
        'data-field': 'design_provider',
        children: jsx(DesignTag, {
          value: r.design_provider,
          effective: r.effective_design_provider ?? defaults?.design_provider,
          resolved: r.resolved_design_provider
        })
      }),
      jsx('td', {
        'data-field': 'design_approval',
        children: jsx(DesignTag, { value: r.design_approval, effective: r.effective_design_approval ?? defaults?.design_approval })
      }),
      jsx('td', {
        'data-field': 'default_rigor',
        children: jsx(RigorTag, { value: r.default_rigor, effective: r.effective_default_rigor ?? defaults?.default_rigor ?? 'standard' })
      }),
      jsx('td', {
        className: 'fc-act',
        children: jsxs('div', {
          className: 'fc-act-btns',
          children: [
            jsx(HarnessBadge, { key: 'h', harness: r.harness }),
            jsxs('button', {
              key: 'a',
              type: 'button',
              className: 'fc-btn fc-btn--s',
              'data-action': 'audit-repo',
              'data-active': audit ? 'true' : undefined,
              'aria-expanded': String(!!audit),
              'aria-controls': audit ? auditId : undefined,
              'aria-label': `Audit harness for ${r.repo || r.slug}`,
              title: 'Audit .factonio/ against the checkout (read-only)',
              disabled: auditing,
              onClick: onAudit,
              children: [auditing ? jsx('span', { key: 's', className: 'fc-spin', 'aria-hidden': 'true' }) : jsx(Icon, { key: 'i', name: 'search', size: 11 }), 'Audit']
            }),
            jsx('button', {
              key: 'e',
              type: 'button',
              className: 'fc-btn fc-btn--s',
              ref: buttonRef,
              'data-action': 'edit-repo',
              'data-active': editing ? 'true' : undefined,
              'aria-expanded': String(!!editing),
              'aria-controls': editing ? editorId : undefined,
              'aria-label': `Edit ${r.repo || r.slug}`,
              disabled: busy,
              onClick: onEdit,
              children: 'Edit'
            })
          ]
        })
      })
    ]
  })
}

function AlertIcon() {
  return jsxs('svg', {
    width: 13,
    height: 13,
    viewBox: '0 0 16 16',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.6,
    'aria-hidden': 'true',
    focusable: 'false',
    children: [
      jsx('circle', { cx: 8, cy: 8, r: 6.5 }),
      jsx('path', { d: 'M8 4.5v4M8 11v.01', strokeLinecap: 'round' })
    ]
  })
}

function RepoEditor({ ctx, repo, defaults, autoResolved, reposQ, editorId, saving, setSaving, onClose, onSaved }) {
  const queryClient = useQueryClient()
  // Explicit per-field edits: { [field]: userValue } only for fields the user changed.
  // Untouched fields always show the live (possibly refreshed) repo value and are never
  // PATCHed; edited fields keep the user's value across background refreshes.
  const [edits, setEdits] = useState({})
  const live = seedRepoDraft(repo, defaults)
  const draft = { ...live, ...edits }
  const [errors, setErrors] = useState({})
  const [apiError, setApiError] = useState(null)
  const branchRef = useRef(null)
  const roundsRef = useRef(null)

  // Focus moves to the Default branch input when the editor opens.
  useEffect(() => {
    branchRef.current?.focus?.()
  }, [])

  const changes = repoDraftChanges(edits, repo, defaults)
  const count = Object.keys(changes).length
  const errorCount = Object.keys(errors).length
  const fid = name => `${editorId}-${name}`

  const setField = (name, value) => {
    setEdits(prev => {
      const next = { ...prev }
      // Choosing the current live value drops the edit (field tracks live again).
      if (value === live[name]) delete next[name]
      else next[name] = value
      return next
    })
    // Live re-validation only for fields already flagged (each rule is per-field).
    setErrors(prev => {
      if (!prev[name]) return prev
      const fresh = validateRepoDraft({ default_branch: 'main', max_fix_rounds: 1, [name]: value })[name]
      const next = { ...prev }
      if (fresh) next[name] = fresh
      else delete next[name]
      return next
    })
  }

  const cancel = () => {
    if (saving) return
    onClose()
  }

  const submit = async e => {
    e?.preventDefault?.()
    if (saving) return
    const found = validateRepoDraft(draft)
    if (Object.keys(found).length) {
      setErrors(found)
      setApiError(null)
      if (found.default_branch) branchRef.current?.focus?.()
      else if (found.max_fix_rounds) roundsRef.current?.focus?.()
      return
    }
    setErrors({})
    if (!count) return
    setApiError(null)
    setSaving(true)
    try {
      await ctx.rest('/repos/' + encodeURIComponent(repo.slug), { method: 'PATCH', body: changes })
    } catch (err) {
      setApiError(describeSaveError(err))
      setSaving(false)
      return
    }
    try {
      if (typeof queryClient?.invalidateQueries === 'function') queryClient.invalidateQueries({ queryKey: REPOS_QUERY_KEY })
      else reposQ?.refetch?.()
    } catch { /* refresh is best-effort; the 30s poll catches up */ }
    host.notify({ kind: 'info', message: 'Saved settings for ' + (repo.repo || repo.slug) })
    onSaved()
  }

  const field = ({ name, label, hint, control }) => {
    const err = errors[name]
    const describedBy = err ? fid(`${name}-e`) : fid(`${name}-h`)
    return jsxs('div', {
      key: name,
      className: 'fc-field',
      'data-field': name,
      children: [
        jsx('label', { htmlFor: fid(name), children: label }),
        control({
          id: fid(name),
          name,
          'aria-invalid': err ? 'true' : undefined,
          'aria-describedby': describedBy,
          value: draft[name],
          onChange: ev => setField(name, ev?.target?.value ?? '')
        }),
        err
          ? jsx('span', { className: 'fc-field-err', id: fid(`${name}-e`), children: err })
          : (Array.isArray(hint) ? jsxs : jsx)('span', { className: 'fc-field-hint', id: fid(`${name}-h`), children: hint })
      ]
    })
  }
  // `unknownLabel` adds a disabled '' option only while the value is '' so real DOM
  // <select> shows it instead of falling back to the first option (selectedIndex 0).
  const select = (options, inheritLabel, unknownLabel) => props => jsx('select', {
    ...props,
    className: 'fc-input',
    children: [
      inheritLabel ? jsx('option', { key: '__inherit', value: '', children: inheritLabel }) : null,
      unknownLabel && props.value === '' ? jsx('option', { key: '__unknown', value: '', disabled: true, children: unknownLabel }) : null,
      ...[...new Set([...options, ...(props.value && !options.includes(props.value) ? [props.value] : [])])]
        .map(o => jsx('option', { key: o, value: o, children: o }))
    ]
  })

  let msg = ''
  let msgClass = 'fc-ed-msg'
  if (saving) msg = `Saving ${plural(count, 'change', 'changes')}…`
  else if (apiError) {
    msg = jsxs('div', {
      className: 'fc-alert',
      role: 'alert',
      children: [
        jsx(AlertIcon, {}),
        apiError.status
          ? jsxs('span', { children: [jsx('b', { children: `Couldn’t save (${apiError.status}).` }), ` ${apiError.detail} Your changes are still here.`] })
          : jsxs('span', { children: [jsx('b', { children: 'Couldn’t save.' }), ' Network error. Your changes are still here.'] })
      ]
    })
  } else if (errorCount) {
    msg = `Fix ${plural(errorCount, 'field', 'fields')} to save.`
    msgClass = 'fc-ed-msg fc-ed-msg--err'
  } else if (count) msg = `${plural(count, 'unsaved change', 'unsaved changes')}`

  const label = repo.repo || repo.slug
  return jsx('tr', {
    className: 'fc-repo-ed',
    'data-editing': 'true',
    'data-slug': repo.slug,
    children: jsx('td', {
      colSpan: 9,
      children: jsxs('form', {
        className: 'fc-ed',
        id: editorId,
        'aria-label': `Edit settings for ${label}`,
        'aria-busy': saving ? 'true' : undefined,
        noValidate: true,
        onSubmit: submit,
        onKeyDown: e => {
          if (e?.key === 'Escape' && !saving) {
            e.preventDefault?.()
            e.stopPropagation?.()
            cancel()
          }
        },
        children: [
          jsxs('div', {
            className: 'fc-ed-id',
            children: [
              jsxs('span', { children: ['Editing ', jsx('span', { className: 'fc-mono', children: repo.slug })] }),
              jsxs('span', { children: ['Path ', jsx('span', { className: 'fc-mono', children: repo.path || '—' })] })
            ]
          }),
          jsxs('fieldset', {
            className: 'fc-ed-grid',
            disabled: saving,
            children: [
              field({
                name: 'default_branch',
                label: 'Default branch',
                hint: 'No spaces, “..”, or leading “-”.',
                control: props => jsx('input', { ...props, ref: branchRef, className: 'fc-input fc-mono', type: 'text', spellCheck: false, autoComplete: 'off' })
              }),
              field({
                name: 'merge_policy',
                label: 'Merge policy',
                hint: 'auto merges when checks pass; ask waits for you.',
                control: select(MERGE_POLICY_OPTIONS)
              }),
              field({
                name: 'publish_policy',
                label: 'Publish policy',
                hint: 'auto pushes and opens a PR once verified; ask keeps work local until you approve.',
                control: select(PUBLISH_POLICY_OPTIONS, null, 'Invalid / unknown — choose policy')
              }),
              field({
                name: 'max_fix_rounds',
                label: 'Max fix rounds',
                hint: 'Whole number, 1 or more.',
                control: props => jsx('input', { ...props, ref: roundsRef, className: 'fc-input', type: 'number', min: 1, step: 1, inputMode: 'numeric' })
              }),
              field({
                name: 'design_provider',
                label: 'Design provider',
                hint: providerHint(draft, repo, defaults, autoResolved),
                control: select(DESIGN_PROVIDER_OPTIONS, `Inherit global (${defaults?.design_provider || 'auto'})`)
              }),
              field({
                name: 'design_approval',
                label: 'Design approval',
                hint: 'ask pauses for your approval before build.',
                control: select(DESIGN_APPROVAL_OPTIONS, `Inherit global (${defaults?.design_approval || 'ask'})`)
              }),
              field({
                name: 'default_rigor',
                label: 'Default rigor',
                hint: rigorHint(draft, defaults),
                control: props => jsx('select', {
                  ...props,
                  className: 'fc-input',
                  children: [
                    jsx('option', { key: '__inherit', value: '', children: `Inherit global (${defaults?.default_rigor || 'standard'})` }),
                    ...RIGOR_OPTIONS.map(([v, l]) => jsx('option', { key: v, value: v, children: l })),
                    props.value && !RIGOR_OPTIONS.some(([v]) => v === props.value)
                      ? jsx('option', { key: props.value, value: props.value, children: props.value })
                      : null
                  ]
                })
              })
            ]
          }),
          jsxs('div', {
            className: 'fc-ed-foot',
            children: [
              jsx('div', { className: msgClass, 'aria-live': 'polite', children: msg }),
              jsx('button', { type: 'button', className: 'fc-btn', 'data-action': 'cancel-edit', disabled: saving, onClick: cancel, children: 'Cancel' }),
              jsxs('button', {
                type: 'submit',
                className: 'fc-btn fc-btn--primary',
                'data-action': 'save-repo',
                disabled: saving || count === 0,
                children: saving
                  ? [jsx('span', { key: 'spin', className: 'fc-spin', 'aria-hidden': 'true' }), 'Saving…']
                  : [apiError ? 'Retry save' : 'Save changes']
              })
            ]
          })
        ]
      })
    })
  })
}

function ReposSection({ ctx, reposQ }) {
  const repos = Array.isArray(reposQ?.data?.repos) ? reposQ.data.repos : []
  const defaults = reposQ?.data?.defaults || {}
  const [editing, setEditing] = useState(null)
  const [saving, setSaving] = useState(false)
  const [audits, setAudits] = useState({})
  const editButtons = useRef({})
  const focusAfterClose = useRef(null)
  const auditSeq = useRef({})
  const queryClient = useQueryClient()

  // Cancel / save returns focus to the row's Edit button once it is re-enabled.
  useEffect(() => {
    const slug = focusAfterClose.current
    if (!slug) return
    focusAfterClose.current = null
    editButtons.current[slug]?.focus?.()
  })

  if (repos.length === 0) {
    return jsxs('div', {
      className: 'fc-sec',
      children: [
        jsx('h2', { children: 'Registered Repositories' }),
        jsx(EmptyState, {
          title: 'No repositories registered',
          description: 'Run `hermes factonio add owner/repo` or ask in chat to register a GitHub repo.'
        })
      ]
    })
  }

  const close = slug => {
    focusAfterClose.current = slug
    setSaving(false)
    setEditing(null)
  }
  // On-demand read-only harness audit (POST /repos/{slug}/harness/audit); one drawer per repo.
  const runAudit = async r => {
    const n = (auditSeq.current[r.slug] || 0) + 1
    auditSeq.current[r.slug] = n
    setAudits(cur => ({ ...cur, [r.slug]: { status: 'busy' } }))
    let next
    try {
      const result = await ctx.rest('/repos/' + encodeURIComponent(r.slug) + '/harness/audit', { method: 'POST', body: {} })
      next = { status: 'done', result }
      invalidateKeys(queryClient, [REPOS_QUERY_KEY, ['factonio', 'trail']])
    } catch (err) {
      next = { status: 'error', error: describeSaveError(err) }
    }
    if (auditSeq.current[r.slug] === n) setAudits(cur => (cur[r.slug] ? { ...cur, [r.slug]: next } : cur))
  }
  const closeAudit = slug => {
    auditSeq.current[slug] = (auditSeq.current[slug] || 0) + 1
    setAudits(cur => {
      const next = { ...cur }
      delete next[slug]
      return next
    })
  }
  const auditIdFor = slug => `fc-repo-au-${domSafe(slug)}`
  const editorIdFor = slug => `fc-repo-ed-${domSafe(slug)}`
  const editingSlug = repos.some(r => r.slug === editing) ? editing : null
  // `auto` resolves the same for every repo (a host capability probe), so any
  // repo currently on `auto` tells us what inheriting/choosing `auto` resolves to.
  const autoResolved = repos.find(r => r.effective_design_provider === 'auto' && r.resolved_design_provider)?.resolved_design_provider || null

  const rows = []
  for (const r of repos) {
    const open = editingSlug === r.slug
    const audit = audits[r.slug] || null
    rows.push(jsx(RepoRow, {
      key: `row:${r.slug}`,
      repo: r,
      defaults,
      editing: open,
      busy: saving,
      audit,
      auditId: auditIdFor(r.slug),
      onAudit: () => (audit && audit.status !== 'busy' ? closeAudit(r.slug) : audit ? null : runAudit(r)),
      editorId: editorIdFor(r.slug),
      buttonRef: el => { editButtons.current[r.slug] = el },
      onEdit: () => {
        if (saving) return
        if (open) close(r.slug)
        else setEditing(r.slug)
      }
    }))
    if (open) {
      rows.push(jsx(RepoEditor, {
        key: `ed:${r.slug}`,
        ctx,
        repo: r,
        defaults,
        autoResolved,
        reposQ,
        editorId: editorIdFor(r.slug),
        saving,
        setSaving,
        onClose: () => close(r.slug),
        onSaved: () => close(r.slug)
      }))
    }
    if (audit) {
      rows.push(jsx(AuditDrawer, { key: `au:${r.slug}`, repo: r, audit, id: auditIdFor(r.slug), onRun: () => runAudit(r), onClose: () => closeAudit(r.slug) }))
    }
  }

  const col = c => jsx('col', { key: c, className: c })
  return jsxs('div', {
    className: 'fc-sec fc-repos-sec',
    children: [
      jsxs('div', {
        className: 'fc-sec-head',
        children: [
          jsxs('div', {
            children: [
              jsx('h2', { children: 'Registered Repositories' }),
              jsxs('p', {
                className: 'fc-sec-sub',
                children: [
                  'Global defaults: design provider ',
                  jsx('span', { className: 'fc-mono', children: defaults.design_provider || 'auto' }),
                  ' · design approval ',
                  jsx('span', { className: 'fc-mono', children: defaults.design_approval || 'ask' }),
                  ' · rigor ',
                  jsx('span', { className: 'fc-mono', children: defaults.default_rigor || 'standard' })
                ]
              }),
              jsxs('p', {
                className: 'fc-sec-sub',
                children: [
                  'New repos start with merge ',
                  jsx('span', { className: 'fc-mono', children: defaults.merge_policy || 'auto' }),
                  ' · publish ',
                  jsx('span', { className: 'fc-mono', children: resolvePublishPolicy({}, defaults) || 'unknown' }),
                  ', set per repo.'
                ]
              }),
              jsxs('p', {
                className: 'fc-sec-sub',
                'data-field': 'audit-note',
                children: ['Audit runs the harness check live, read-only. It never edits repo files or ', jsx('span', { className: 'fc-mono', children: '.git/index' }), '.']
              })
            ]
          }),
          jsxs('span', {
            className: 'fc-legend',
            children: [
              jsxs('span', { className: 'fc-tag fc-tag--inherit', children: ['value ', jsx('span', { className: 'fc-tag-src', children: '· global' })] }),
              ' inherited ',
              jsx('span', { className: 'fc-tag fc-tag--set', children: 'value' }),
              ' override'
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'fc-repos-wrap',
        children: jsxs('table', {
          className: 'fc-table fc-repos',
          children: [
            jsxs('colgroup', {
              children: ['fc-col-repo', 'fc-col-branch', 'fc-col-merge', 'fc-col-publish', 'fc-col-rounds', 'fc-col-prov', 'fc-col-appr', 'fc-col-rigor', 'fc-col-act'].map(col)
            }),
            jsx('thead', {
              children: jsxs('tr', {
                children: [
                  jsxs('th', { scope: 'col', children: ['Repository', jsx('span', { className: 'fc-th-sub', children: 'Local path' })] }),
                  jsx('th', { scope: 'col', children: 'Branch' }),
                  jsx('th', { scope: 'col', children: 'Merge policy' }),
                  jsx('th', { scope: 'col', children: 'Publish policy' }),
                  jsx('th', { scope: 'col', className: 'fc-num', children: 'Max fix rounds' }),
                  jsx('th', { scope: 'col', children: 'Design provider' }),
                  jsx('th', { scope: 'col', children: 'Design approval' }),
                  jsx('th', { scope: 'col', children: 'Rigor' }),
                  jsx('th', { scope: 'col', className: 'fc-act', children: 'Harness & actions' })
                ]
              })
            }),
            jsx('tbody', { children: rows })
          ]
        })
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// Decision Trail (approved Phase 1b design: design/DESIGN.md). GET /trail feeds
// both the /factonio TrailSection and the sidebar strips / card drawers through
// one shared query key; filtering happens client-side.
// ---------------------------------------------------------------------------

const TRAIL_LIMIT = 200
const TRAIL_QUERY_KEY = ['factonio', 'trail', TRAIL_LIMIT]
const TRAIL_PHASE_ORDER = ['define', 'design', 'implement', 'verify', 'publish', 'gate', 'merge', 'intake', 'harness', 'eval', 'escape', 'watchdog']
// Phase filter runs (delivery | Wave 5 | safety), separated in the bar.
const TRAIL_PHASE_GROUPS = [['define', 'design', 'implement', 'verify', 'publish', 'gate', 'merge'], ['intake', 'harness', 'eval'], ['escape', 'watchdog']]
const TRAIL_TONE = {
  merged: 'ok', pass: 'ok', approved: 'ok', ready: 'ok', published: 'ok',
  synthesized: 'ok', verified_fix: 'ok', decided: 'ok', clean: 'ok',
  reject: 'bad', stalled: 'bad', dead_worker: 'bad', blocked: 'bad', refused: 'bad', error: 'bad',
  stop_the_line: 'bad', fix_incomplete: 'bad', no_winner: 'bad', proof_failed: 'bad', product_regression: 'bad',
  inconclusive: 'warn', pivot: 'warn', escape: 'warn',
  flaky: 'warn', not_reproduced: 'warn', tie: 'warn', harness_gap: 'warn', doc_drift: 'warn',
  spawned: 'info', recorded: 'info', queued: 'info',
  actionable: 'info', no_action: 'info'
}
const TRAIL_SB_TONE = { ok: 'pass', bad: 'reject', warn: 'warn', info: 'info' }
const TRAIL_ACTOR_ICON = { orchestrator: 'dash', worker: 'running', watchdog: 'queued', human: 'person', eval: 'split' }
const TRAIL_KNOWN_EVIDENCE = new Set(['pr_number', 'head_sha', 'patch_id', 'tests_passed', 'tests_failed', 'escaped_from', 'side_effect_age_s', 'worker_pid', 'card_id', 'branch', 'base_branch', 'draft'])
const GH_SLUG = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/

const resultTone = r => TRAIL_TONE[String(r || '').toLowerCase()] || null

// Decision History titles for the common codes (DESIGN.md); everything else is humanized.
const DECISION_TITLES = {
  pr_gate: 'PR gate check', bootstrap_verify: 'Bootstrap verify', merge_pr: 'PR merged', publish_pr: 'Publish PR',
  start_workstream: 'Start workstream', approve_design: 'Design approved', request_design_revision: 'Design revision requested',
  verify_blackbox_verdict: 'Black-box verdict', verify_panel_consensus: 'Panel consensus', escape_detected: 'Escape detected',
  harness_audit: 'Harness audit', spawn_verify: 'Spawn verification', spawn_implement: 'Spawn implementation',
  arena_synthesis: 'Arena synthesis', gate_policy_change: 'Gate policy change', override_gate: 'Human gate override'
}
// "pr_merged" -> "PR merged"
function humanDecision(code) {
  if (Object.hasOwn(DECISION_TITLES, String(code))) return DECISION_TITLES[code]
  const s = String(code || '').replace(/_/g, ' ').trim()
  return (s.charAt(0).toUpperCase() + s.slice(1)).replace(/\b(pr|Pr)\b/g, 'PR')
}

// trail.db stores microsecond ISO timestamps; Date.parse needs milliseconds.
function trailMs(ts) {
  const ms = Date.parse(String(ts || '').replace(/(\.\d{3})\d+/, '$1'))
  return Number.isNaN(ms) ? null : ms
}
function fmtTrailClock(ts) {
  const ms = trailMs(ts)
  return ms == null ? '—' : new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}
function fmtTrailWhen(ts) {
  const ms = trailMs(ts)
  if (ms == null) return String(ts || '—')
  return `${new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric' })} · ${fmtTrailClock(ts)}`
}
function fmtAgo(ts, now = Date.now()) {
  const ms = trailMs(ts)
  if (ms == null) return ''
  const m = Math.round((now - ms) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  if (m < 1440) return `${Math.floor(m / 60)}h ${m % 60}m ago`
  return `${Math.floor(m / 1440)}d ago`
}
function fmtIdle(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.round(s / 60)}m`
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`
}
const shortId = id => String(id || '').slice(0, 10)

// Newest first; ties keep the API order (already newest-first by ts, rowid).
function trailDecisions(data) {
  const list = Array.isArray(data?.decisions) ? data.decisions.filter(d => d && typeof d === 'object' && d.id) : []
  return list.map((d, i) => [d, i]).sort((a, b) => (trailMs(b[0].ts) ?? 0) - (trailMs(a[0].ts) ?? 0) || a[1] - b[1]).map(x => x[0])
}

// GitHub owner/repo for a decision's repo_slug: registered repo mapping first,
// then a literal "owner/repo" slug. Anything else gets no PR link.
function trailGithubRepo(slug, ghBySlug) {
  const s = String(slug || '')
  const mapped = ghBySlug?.[s]
  if (mapped && GH_SLUG.test(mapped)) return mapped
  return GH_SLUG.test(s) ? s : null
}

// Information barrier, client side: drop anything that mentions holdout scenarios. Text is
// NFKC-folded and stripped of ANSI escapes and control/format characters first, so fullwidth,
// zero-width or escape-split spellings cannot slip past the match. Fails closed on nesting.
const HOLDOUT_RE = /holdout/i
const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g
const barrierText = s => String(s).normalize('NFKC').replace(ANSI_RE, '').replace(/[\p{Cc}\p{Cf}]/gu, '')
function mentionsHoldout(v, depth = 0) {
  if (v == null || typeof v === 'boolean') return false
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'bigint') return HOLDOUT_RE.test(barrierText(v))
  if (depth > 6) return true
  if (Array.isArray(v)) return v.some(x => mentionsHoldout(x, depth + 1))
  if (typeof v === 'object') return Object.entries(v).some(([k, x]) => mentionsHoldout(k, depth + 1) || mentionsHoldout(x, depth + 1))
  return false
}
const safeEvidence = e => Object.fromEntries(Object.entries(e).filter(([k, v]) => !mentionsHoldout(k) && !mentionsHoldout(v)))
const barrierStr = v => (typeof v === 'string' && v.trim() && !mentionsHoldout(v) ? v : null)
// Basename only: a report path is never rendered with its directory.
function safeBasename(p) {
  const s = barrierStr(p)
  if (!s) return null
  return barrierStr(s.split(/[\\/]/).filter(Boolean).pop() || '')
}

const evidenceList = v => (Array.isArray(v) ? v.filter(x => x != null && x !== '') : [])
const evidenceCount = v => (Array.isArray(v) ? evidenceList(v).length : Number.isSafeInteger(v) && v > 0 ? v : 0)
const isArenaDecision = (d, e) => /(^|_)arena(_|$)/.test(String(d?.decision || '')) || (e.arena_id != null && e.arena_id !== '')
const isIntakeDecision = d => /^issue_intake/.test(String(d?.decision || ''))

// Wave 5 structured pills (start_workstream, arena, issue intake, skill eval). Marks handled keys in `used`.
function wave5Pills(d, e, gh, used) {
  const out = []
  const has = k => e[k] != null && e[k] !== ''
  const add = (key, pill, keys = [key]) => {
    keys.forEach(k => used.add(k))
    out.push({ key, title: `${key} ${Array.isArray(e[key]) ? e[key].join(', ') : e[key]}`, ...pill })
  }
  const short = v => String(v).slice(0, 32)
  if (d?.decision === 'start_workstream') {
    used.add('kind'); used.add('read_only')
    if (has('kind') && typeof e.kind === 'string') add('kind', { label: 'mode', text: short(e.kind), tone: 'info' })
    if (e.read_only === true) add('read_only', { text: 'read-only', tone: 'warn' })
  }
  if (isArenaDecision(d, e)) {
    if (has('arena_id') && typeof e.arena_id !== 'object') add('arena_id', { label: 'arena', text: short(e.arena_id) })
    if (has('target') && typeof e.target !== 'object') add('target', { label: 'target', text: short(e.target) })
    if (Array.isArray(e.candidates) && e.candidates.length) {
      const vendors = e.candidates.map(c => (c && typeof c === 'object' ? c.vendor : c)).filter(v => typeof v === 'string' && v)
      const text = vendors.length && vendors.length <= 3 && vendors.length === e.candidates.length ? vendors.join(' · ') : String(e.candidates.length)
      add('candidates', { label: 'candidates', text: short(text), title: `candidates ${vendors.join(', ') || e.candidates.length}` })
    }
    if (has('judge_vendor') && typeof e.judge_vendor !== 'object') add('judge_vendor', { label: 'judge', text: short(e.judge_vendor) })
  }
  if (isIntakeDecision(d)) {
    if (has('issue_number')) {
      const n = Number(e.issue_number)
      const repo = gh || (typeof e.repo === 'string' && GH_SLUG.test(e.repo) ? e.repo : null)
      const href = repo && Number.isSafeInteger(n) && n > 0 ? `https://github.com/${repo}/issues/${n}` : null
      add('issue_number', href
        ? { text: `#${n}`, href, title: `Open ${repo} issue #${n}` }
        : { label: 'issue', text: `#${short(e.issue_number)}` })
    }
    if (has('verdict') && typeof e.verdict !== 'object') {
      const v = String(e.verdict)
      add('verdict', { label: 'verdict', text: short(v), tone: v === 'verified_fix' ? 'ok' : v === 'fix_incomplete' ? 'bad' : undefined })
    }
    for (const [key, text, tone] of [['stop_the_line', 'stop the line', 'bad'], ['actionable', 'actionable', 'info']]) {
      used.add(key)
      const n = Array.isArray(e[key]) ? evidenceList(e[key]).length : 0
      if (e[key] === true || n) add(key, { text: n ? `${text} ${n}` : text, tone })
    }
    used.add('counts')
    const issues = evidenceCount(e.issue_numbers) ||
      (e.counts && typeof e.counts === 'object' && !Array.isArray(e.counts) ? Object.values(e.counts).reduce((s, x) => s + (Number.isSafeInteger(x) && x > 0 ? x : 0), 0) : 0)
    if (issues || has('issue_numbers')) add('issue_numbers', { label: 'issues', text: String(issues), title: `issues ${issues}` }, ['issue_numbers', 'counts'])
  }
  if (d?.decision === 'skill_eval') {
    used.add('verdict')
    const winner = has('winner_label') ? e.winner_label : e.winner
    used.add('winner'); used.add('winner_label')
    if (winner != null && winner !== '' && winner !== 'none' && typeof winner !== 'object') {
      add(has('winner_label') ? 'winner_label' : 'winner', { label: 'winner', text: short(winner), tone: winner === 'tie' ? 'warn' : 'ok' }, ['winner', 'winner_label'])
    }
    const dq = Array.isArray(e.disqualified) ? evidenceList(e.disqualified).filter(x => typeof x !== 'object') : (has('disqualified') && typeof e.disqualified !== 'object' && e.disqualified !== false ? [e.disqualified] : [])
    used.add('disqualified')
    if (dq.length) add('disqualified', { label: 'disqualified', text: short(dq.join(', ')), tone: 'bad' })
    if (has('eval_phase') && typeof e.eval_phase !== 'object') add('eval_phase', { label: 'eval phase', text: short(e.eval_phase) })
    if (has('decided_by') && typeof e.decided_by !== 'object') add('decided_by', { label: 'decided by', text: short(e.decided_by) })
  }
  return out
}

// Ordered evidence pills: { key, text, label?, tone?, href?, title }.
function evidencePills(d, gh) {
  const raw = d?.evidence && typeof d.evidence === 'object' && !Array.isArray(d.evidence) ? d.evidence : {}
  const e = safeEvidence(raw)
  const has = k => e[k] != null && e[k] !== ''
  const out = []
  if (has('pr_number')) {
    const n = Number(e.pr_number)
    const href = gh && Number.isSafeInteger(n) && n > 0 ? `https://github.com/${gh}/pull/${n}` : null
    out.push({ key: 'pr_number', text: `PR #${e.pr_number}`, href, title: href ? `Open ${gh}#${n}` : `pr_number ${e.pr_number}` })
  }
  if (has('head_sha')) out.push({ key: 'head_sha', text: String(e.head_sha).slice(0, 7), title: `head_sha ${e.head_sha}` })
  const hasBase = has('base_branch') && typeof e.base_branch !== 'object'
  if (has('branch') && typeof e.branch !== 'object') {
    out.push({
      key: 'branch',
      label: 'branch',
      text: String(e.branch).slice(0, 48) + (hasBase ? ` → ${e.base_branch}` : ''),
      title: `branch ${e.branch}` + (hasBase ? ` → base ${e.base_branch}` : '')
    })
  } else if (hasBase) {
    out.push({ key: 'base_branch', label: 'base branch', text: String(e.base_branch).slice(0, 32), title: `base_branch ${e.base_branch}` })
  }
  if (e.draft === true) out.push({ key: 'draft', text: 'draft', title: 'draft true' })
  if (has('patch_id')) out.push({ key: 'patch_id', label: 'patch-id', text: String(e.patch_id).slice(0, 7), title: `patch_id ${e.patch_id}` })
  if (has('tests_passed')) out.push({ key: 'tests_passed', text: `${e.tests_passed} pass`, tone: 'ok', title: `tests_passed ${e.tests_passed}` })
  if (has('tests_failed') && Number(e.tests_failed) !== 0) out.push({ key: 'tests_failed', text: `${e.tests_failed} fail`, tone: 'bad', title: `tests_failed ${e.tests_failed}` })
  if (has('escaped_from')) out.push({ key: 'escaped_from', label: 'escaped from', text: String(e.escaped_from), title: `escaped_from ${e.escaped_from}` })
  if (has('side_effect_age_s')) out.push({ key: 'side_effect_age_s', label: 'idle', text: fmtIdle(e.side_effect_age_s), tone: 'bad', title: `side_effect_age_s ${e.side_effect_age_s}` })
  if (has('worker_pid')) out.push({ key: 'worker_pid', label: 'pid', text: String(e.worker_pid), title: `worker_pid ${e.worker_pid}` })
  if (has('card_id')) out.push({ key: 'card_id', text: String(e.card_id), title: `card_id ${e.card_id}` })
  const used = new Set()
  out.push(...wave5Pills(d, e, gh, used))
  for (const [k, v] of Object.entries(e)) {
    if (TRAIL_KNOWN_EVIDENCE.has(k) || used.has(k) || v == null || v === '' || typeof v === 'object') continue
    out.push({ key: k, label: k.replace(/_/g, ' '), text: String(v).slice(0, 32), title: `${k} ${v}` })
  }
  return out
}

const prefersReducedMotion = () => {
  try { return !!globalThis.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches } catch { return false }
}

// `?tenant=` from the hash route (`#/factonio?tenant=…`) or the plain search string.
function routeQueryParam(name) {
  try {
    const loc = globalThis.location
    const hash = String(loc?.hash || '')
    const q = hash.includes('?') ? hash.slice(hash.indexOf('?') + 1) : String(loc?.search || '').replace(/^\?/, '')
    return new URLSearchParams(q).get(name) || null
  } catch {
    return null
  }
}

function retryQuery(query, queryClient, queryKey) {
  if (typeof query?.refetch === 'function') return query.refetch()
  if (typeof queryClient?.invalidateQueries === 'function') return queryClient.invalidateQueries({ queryKey })
  return null
}

function trailWsLabel(tenant, ghRepos) {
  return wsName(tenant, ghRepos).slug
}

// ---------------------------------------------------------------------------
// Decision History (approved Phase 1b design `d_889fc8b89d65`): Attention banner from
// GET /trail/audit, signal presets, routine-tick folding, inline supersession chains, the
// Evidence Inspector and confirmation-gated human decisions (POST /trail/decisions).
// ---------------------------------------------------------------------------

const ROUTINE_DECISIONS = new Set(['pr_gate', 'bootstrap_verify'])
// Mirrors trail.py `_routine_sql`: these results keep a routine code visible under Key Decisions.
const ROUTINE_KEEP_RESULTS = new Set(['proof_failed', 'product_regression', 'error', 'refused', 'escalated'])
const ATTENTION_RESULTS = new Set(['reject', 'blocked', 'escalated', 'needs_input', 'inconclusive', 'conflict', 'proof_failed', 'refused', 'error', 'stalled', 'dead_worker', 'stop_the_line', 'product_regression', 'changes_requested', 'fix_incomplete'])
const TIER1_RESULTS = new Set(['reject', 'product_regression', 'refused', 'proof_failed', 'stalled', 'inconclusive'])
const AUDIT_KINDS = ['gate_loosening', 'unclosed_escalation', 'weak_evidence', 'missing_evidence']
const AUDIT_KIND_LABEL = { gate_loosening: 'gate loosening', missing_evidence: 'missing evidence', weak_evidence: 'weak evidence', unclosed_escalation: 'unclosed escalation' }
const AUDIT_SEVERITY = { gate_loosening: 'high', unclosed_escalation: 'medium', weak_evidence: 'medium', missing_evidence: 'low' }
const HUMAN_DECISION_PHASES = ['define', 'design', 'implement', 'verify', 'publish', 'gate', 'merge', 'intake', 'harness', 'eval', 'escape', 'watchdog', 'retro']
const TRAIL_PHASE_OPTGROUPS = [['Delivery', TRAIL_PHASE_GROUPS[0]], ['Factory', TRAIL_PHASE_GROUPS[1]], ['Signals', TRAIL_PHASE_GROUPS[2]]]
const SIGNAL_PRESETS = [['key', 'Key Decisions'], ['attention', 'Attention'], ['all', 'All']]
const EVIDENCE_MAX_BYTES = 16 * 1024
const TENANT_SLUG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const DH_PAGE = 60
const DH_MAX_LIMIT = 1000
const low = v => String(v ?? '').trim().toLowerCase()
const utf8Bytes = s => (typeof TextEncoder === 'function' ? new TextEncoder().encode(String(s)).length : unescape(encodeURIComponent(String(s))).length)

// Routine tick (the server `signal=key` exclusion): a non-human pr_gate / bootstrap_verify row that
// supersedes nothing, is not an escape/retro and did not fail hard.
function isRoutineDecision(d) {
  return !!d && ROUTINE_DECISIONS.has(low(d.decision)) && low(d.actor) !== 'human' && !d.supersedes_id &&
    low(d.phase) !== 'escape' && low(d.phase) !== 'retro' && !ROUTINE_KEEP_RESULTS.has(low(d.result))
}

// Client mirror of `signal=attention`: active rows with an audit finding, or non-routine escapes,
// watchdog rows and escalating / failing results.
function isAttentionDecision(d, auditIds) {
  if (!d || d.is_superseded) return false
  if (auditIds?.has?.(d.id)) return true
  if (isRoutineDecision(d)) return false
  const phase = low(d.phase)
  return phase === 'escape' || phase === 'watchdog' || ATTENTION_RESULTS.has(low(d.result))
}

// 1 Attention, 2 Key, 3 Routine (DESIGN.md §1.6).
function trailTier(d, auditIds) {
  if (!d) return 2
  if (auditIds?.has?.(d.id) || ['escape', 'watchdog'].includes(low(d.phase)) || TIER1_RESULTS.has(low(d.result)) || /override|policy/.test(low(d.decision))) return 1
  return isRoutineDecision(d) ? 3 : 2
}

// Case-insensitive keyword match over the public row text. A query naming holdout never matches,
// so search cannot probe what the barrier withheld.
function decisionMatchesQuery(d, q) {
  const needle = String(q ?? '').trim().toLowerCase()
  if (!needle) return true
  if (!d || mentionsHoldout(needle)) return false
  let ev = ''
  try { ev = JSON.stringify(stripHoldout(d.evidence ?? {})) } catch { ev = '' }
  return [d.decision, humanDecision(d.decision), d.why, d.result, d.card_id, d.tenant, d.repo_slug, d.id, d.phase, d.actor, ev]
    .some(v => typeof v === 'string' && !mentionsHoldout(v) && v.toLowerCase().includes(needle))
}

// Recursively drop holdout-referencing keys and values (client-side information barrier).
function stripHoldout(v, depth = 0) {
  if (v == null || typeof v === 'boolean' || typeof v === 'number') return v
  if (typeof v === 'string') return mentionsHoldout(v) ? undefined : v
  if (depth > 6) return undefined
  if (Array.isArray(v)) return v.map(x => stripHoldout(x, depth + 1)).filter(x => x !== undefined)
  if (typeof v === 'object') {
    const out = {}
    for (const [k, x] of Object.entries(v)) {
      if (mentionsHoldout(k)) continue
      const y = stripHoldout(x, depth + 1)
      if (y !== undefined) out[k] = y
    }
    return out
  }
  return undefined
}

const pad2 = n => String(n).padStart(2, '0')
function dayKeyOfMs(ms) {
  const t = new Date(ms)
  return `${t.getFullYear()}-${pad2(t.getMonth() + 1)}-${pad2(t.getDate())}`
}
function trailDayKey(ts) {
  const ms = trailMs(ts)
  return ms == null ? 'unknown' : dayKeyOfMs(ms)
}
// "Today · Nov 27", "Yesterday · Nov 26", then the ISO date.
function trailDayLabel(key, now = Date.now()) {
  if (key === 'unknown') return 'Unknown date'
  const [y, m, d] = key.split('-').map(Number)
  const short = new Date(y, m - 1, d).toLocaleDateString([], { month: 'short', day: 'numeric' })
  if (key === dayKeyOfMs(now)) return `Today · ${short}`
  if (key === dayKeyOfMs(now - 86400000)) return `Yesterday · ${short}`
  return key
}

function foldSummary(run) {
  const passed = run.filter(d => resultTone(d.result) === 'ok').length
  const blocked = run.filter(d => resultTone(d.result) === 'bad').length
  return {
    kind: 'fold',
    key: `fold:${run[0].id}`,
    decision: low(run[0].decision),
    phase: String(run[0].phase || ''),
    rows: run,
    count: run.length,
    passed,
    blocked,
    other: run.length - passed - blocked,
    from: fmtTrailClock(run[run.length - 1].ts),
    to: fmtTrailClock(run[0].ts)
  }
}

// Newest-first rows -> [{ kind: 'row', d } | { kind: 'fold', ... }]: two or more consecutive routine
// rows with the same decision code on the same calendar day fold into one summary chip.
function foldRoutineDecisions(rows, isRoutine = isRoutineDecision) {
  const list = Array.isArray(rows) ? rows.filter(Boolean) : []
  const out = []
  for (let i = 0; i < list.length;) {
    const d = list[i]
    if (isRoutine(d)) {
      const code = low(d.decision)
      const day = trailDayKey(d.ts)
      let j = i + 1
      while (j < list.length && isRoutine(list[j]) && low(list[j].decision) === code && trailDayKey(list[j].ts) === day) j++
      if (j - i >= 2) {
        out.push(foldSummary(list.slice(i, j)))
        i = j
        continue
      }
    }
    out.push({ kind: 'row', d })
    i++
  }
  return out
}

function foldText(f) {
  const parts = [`${f.passed} passed`]
  if (f.blocked) parts.push(`${f.blocked} blocked`)
  if (f.other) parts.push(`${f.other} other`)
  return { lead: `${f.count} routine ${f.decision} checks`, detail: parts.join(', '), span: f.from === f.to ? f.to : `${f.from}–${f.to}` }
}

// Calendar-day groups (sticky headers) of folded rows.
function groupTrailRows(rows, now = Date.now()) {
  const days = []
  for (const d of Array.isArray(rows) ? rows : []) {
    if (!d) continue
    const key = trailDayKey(d.ts)
    let day = days[days.length - 1]
    if (!day || day.key !== key) {
      day = { key, rows: [] }
      days.push(day)
    }
    day.rows.push(d)
  }
  return days.map(day => ({ key: day.key, label: trailDayLabel(day.key, now), count: day.rows.length, items: foldRoutineDecisions(day.rows) }))
}

// { [correctionId]: { priors: [nearest original first ...], missing: [ids not loaded] } }. Follows
// `supersedes_id` transitively; a cycle or a 50-hop chain stops instead of looping.
function buildSupersessionChains(rows) {
  const byId = new Map()
  for (const d of Array.isArray(rows) ? rows : []) if (d && d.id && !byId.has(d.id)) byId.set(d.id, d)
  const chains = {}
  for (const d of byId.values()) {
    if (!d.supersedes_id) continue
    const priors = []
    const missing = []
    const seen = new Set([d.id])
    let id = d.supersedes_id
    while (id && !seen.has(id) && priors.length < 50) {
      seen.add(id)
      const p = byId.get(id)
      if (!p) {
        missing.push(String(id))
        break
      }
      priors.push(p)
      id = p.supersedes_id
    }
    chains[d.id] = { priors, missing }
  }
  return chains
}

// GET /trail/audit -> { available, clean, findings, counts, inspected, ids, byId }. Anything that is not
// an audit payload (404/503 on older or broken backends) reads as unavailable: no banner, no pill.
function summarizeTrailAudit(data, error) {
  const none = { available: false, clean: false, findings: [], counts: [], inspected: null, ids: new Set(), byId: new Map() }
  if (error || !data || typeof data !== 'object' || Array.isArray(data) || !Array.isArray(data.findings) || data.ok === false) return none
  const findings = []
  for (const f of data.findings) {
    if (!f || typeof f !== 'object' || !AUDIT_KIND_LABEL[f.kind]) continue
    const id = barrierStr(f.decision_id)
    if (!id) continue
    const sev = ['high', 'medium', 'low'].includes(f.severity) ? f.severity : AUDIT_SEVERITY[f.kind]
    findings.push({ kind: f.kind, label: AUDIT_KIND_LABEL[f.kind], decisionId: id, message: barrierStr(f.message) || AUDIT_KIND_LABEL[f.kind], severity: sev, decision: barrierStr(f.decision) })
  }
  const counts = AUDIT_KINDS.map(k => [k, findings.filter(f => f.kind === k).length]).filter(([, n]) => n > 0)
  const byId = new Map()
  for (const f of findings) {
    if (!byId.has(f.decisionId)) byId.set(f.decisionId, [])
    byId.get(f.decisionId).push(f)
  }
  const inspected = Number.isSafeInteger(data.inspected) && data.inspected >= 0 ? data.inspected : null
  return { available: true, clean: findings.length === 0, findings, counts, inspected, ids: new Set(byId.keys()), byId }
}

function trailPath({ tenant, limit = TRAIL_LIMIT, signal, q } = {}) {
  const parts = []
  if (tenant) parts.push(`tenant=${encodeURIComponent(tenant)}`)
  parts.push(`limit=${limit}`)
  if (signal && signal !== 'all') parts.push(`signal=${encodeURIComponent(signal)}`)
  if (q) parts.push(`q=${encodeURIComponent(q)}`)
  return `/trail?${parts.join('&')}`
}

// At most 3 scan-line pills in a fixed order (PR, branch -> base, head sha, then the first other key)
// plus the number of further safe evidence keys left for the inspector.
const SCAN_ORDER = ['pr_number', 'branch', 'base_branch', 'head_sha']
function scanEvidence(d, gh) {
  const pills = evidencePills(d, gh)
  const ranked = [...pills].sort((a, b) => {
    const ra = SCAN_ORDER.indexOf(a.key)
    const rb = SCAN_ORDER.indexOf(b.key)
    return (ra < 0 ? 99 : ra) - (rb < 0 ? 99 : rb)
  })
  const top = ranked.slice(0, 3)
  const covered = new Set(top.map(p => p.key))
  if (covered.has('branch')) covered.add('base_branch')
  const raw = d?.evidence && typeof d.evidence === 'object' && !Array.isArray(d.evidence) ? safeEvidence(d.evidence) : {}
  const more = Object.keys(raw).filter(k => !covered.has(k) && raw[k] != null && raw[k] !== '').length
  return { top, more }
}

const ROW_TONE = r => resultTone(r) || undefined

// Log Human Decision / Supersede form: { field: message } for every invalid field.
function validateDecisionDraft(f) {
  const v = f || {}
  const errors = {}
  if (!String(v.decision ?? '').trim()) errors.decision = 'A decision code is required.'
  else if (/\s/.test(String(v.decision).trim())) errors.decision = 'Use a short code without spaces, e.g. override_gate.'
  if (!String(v.why ?? '').trim()) errors.why = 'Explain why; a future reader needs it.'
  if (!HUMAN_DECISION_PHASES.includes(v.phase)) errors.phase = 'Choose a phase.'
  const tenant = String(v.tenant ?? '').trim()
  if (tenant && (!TENANT_SLUG_RE.test(tenant) || tenant.includes('..'))) errors.tenant = 'Workstream must be a tenant slug (letters, digits, . _ -).'
  const ev = String(v.evidence ?? '').trim()
  if (ev) {
    let parsed
    try { parsed = JSON.parse(ev) } catch { parsed = undefined }
    if (parsed === undefined) errors.evidence = 'Evidence is not valid JSON.'
    else if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) errors.evidence = 'Evidence must be a JSON object.'
    else if (utf8Bytes(JSON.stringify(parsed)) > EVIDENCE_MAX_BYTES) errors.evidence = 'Evidence must be 16 KiB or less.'
  }
  return errors
}

// POST /trail/decisions body. `actor` is never sent: the server always records `human`.
function humanDecisionBody(f, target) {
  const v = f || {}
  const body = { decision: String(v.decision).trim(), phase: v.phase, why: String(v.why).trim(), confirmed: true }
  for (const k of ['result', 'repo', 'tenant', 'card_id']) {
    const s = String(v[k] ?? '').trim()
    if (s) body[k] = s
  }
  if (target?.id) body.supersedes_id = String(target.id)
  const ev = String(v.evidence ?? '').trim()
  if (ev) body.evidence = JSON.parse(ev)
  return body
}

function decisionDraftFor(target, tenant) {
  if (!target) return { phase: 'gate', decision: '', why: '', result: '', repo: '', tenant: tenant || '', card_id: '', evidence: '', confirm: false }
  return {
    phase: HUMAN_DECISION_PHASES.includes(target.phase) ? target.phase : 'gate',
    decision: `override_${String(target.decision || 'decision').replace(/^override_/, '')}`,
    why: '',
    result: '',
    repo: barrierStr(target.repo_slug) || '',
    tenant: barrierStr(target.tenant) || '',
    card_id: barrierStr(target.card_id) || '',
    evidence: '',
    confirm: false
  }
}

function invalidateKeys(queryClient, keys) {
  for (const queryKey of keys) {
    try { queryClient?.invalidateQueries?.({ queryKey }) } catch { /* refresh is best-effort */ }
  }
}

// Shared async-action state: { status: idle|busy|ok|error, result?, error? }; a newer run wins.
function useAction() {
  const [state, setState] = useState({ status: 'idle' })
  const seq = useRef(0)
  const run = async fn => {
    const n = ++seq.current
    setState({ status: 'busy' })
    try {
      const result = await fn()
      if (n === seq.current) setState({ status: 'ok', result, at: Date.now() })
      return { ok: true, result }
    } catch (err) {
      const error = describeSaveError(err)
      if (n === seq.current) setState({ status: 'error', error })
      return { ok: false, error }
    }
  }
  return [state, run, setState]
}

function errorLine(error) {
  if (!error?.status) return 'Network error.'
  return `${error.status}: ${error.detail}`
}

// Shared modal shell: role=dialog + aria-modal, Escape and × close (blocked while busy), focus guards
// keep Tab / Shift+Tab inside (no DOM queries), focus restored to the trigger on close.
// lastRef may be one ref or a list (preferred first); guards skip disabled controls because native
// focus() is a no-op on them, and fall back to the dialog itself (tabIndex -1) when all are disabled.
function ConfirmModal({ id, title, sub, size, busy, onClose, initialFocusRef, lastRef, children, foot }) {
  const dialogRef = useRef(null)
  const closeBtnRef = useRef(null)
  const trigger = useRef(undefined)
  if (trigger.current === undefined) trigger.current = globalThis.document?.activeElement || null
  const close = () => {
    if (busy) return
    onClose()
    try { trigger.current?.focus?.() } catch { /* trigger may be gone */ }
  }
  useEffect(() => {
    const el = initialFocusRef?.current || dialogRef.current
    try {
      el?.focus?.()
      if (el && typeof el.value === 'string' && typeof el.setSelectionRange === 'function' && el.tagName !== 'SELECT') el.setSelectionRange(el.value.length, el.value.length)
    } catch { /* focus is best-effort */ }
  }, [])
  useEffect(() => {
    // Disabling the focused control drops focus to <body>; keep it on the dialog while in flight.
    if (busy) try { dialogRef.current?.focus?.() } catch { /* focus is best-effort */ }
  }, [busy])
  const lastRefs = [].concat(lastRef || [])
  const focusEnabled = refs => {
    const el = refs.map(r => r?.current).find(c => c && !c.disabled) || dialogRef.current
    try { el?.focus?.() } catch { /* focus is best-effort */ }
  }
  const focusLast = () => focusEnabled([...lastRefs, closeBtnRef])
  const focusFirst = () => focusEnabled([closeBtnRef, initialFocusRef, ...lastRefs])
  const onKeyDown = e => {
    if (e?.key === 'Tab' && e.target && e.target === dialogRef.current) {
      e.preventDefault?.()
      if (e.shiftKey) focusLast()
      else focusFirst()
      return
    }
    if (e?.key !== 'Escape') return
    e.preventDefault?.()
    e.stopPropagation?.()
    close()
  }
  const guard = edge => jsx('span', {
    key: `guard-${edge}`,
    tabIndex: 0,
    'aria-hidden': 'true',
    'data-focus-guard': edge,
    className: 'fc-m-guard',
    onFocus: edge === 'start' ? focusLast : focusFirst
  })
  return jsx('div', {
    className: 'fc-m-scrim',
    onClick: e => { if (e?.target === e?.currentTarget) close() },
    children: jsxs('div', {
      className: 'fc-m',
      'data-size': size || undefined,
      'data-modal': id,
      role: 'dialog',
      'aria-modal': 'true',
      'aria-labelledby': `${id}-t`,
      'aria-busy': busy ? 'true' : undefined,
      tabIndex: -1,
      ref: dialogRef,
      onKeyDown,
      onClick: e => e?.stopPropagation?.(),
      children: [
        guard('start'),
        jsxs('div', {
          key: 'h',
          className: 'fc-m-h',
          children: [
            jsxs('div', { className: 'fc-m-ht', children: [jsx('h2', { id: `${id}-t`, children: title }), sub ? jsx('p', { children: sub }) : null] }),
            jsx('button', { type: 'button', className: 'fc-m-x', ref: closeBtnRef, 'data-action': 'close-modal', 'aria-label': 'Close dialog', disabled: busy, onClick: close, children: jsx(Icon, { name: 'x', size: 14 }) })
          ]
        }),
        jsx('div', { key: 'b', className: 'fc-m-b', children }),
        foot ? jsx('div', { key: 'f', className: 'fc-m-f', children: typeof foot === 'function' ? foot(close) : foot }) : null,
        guard('end')
      ]
    })
  })
}

function Banner({ tone, icon = 'attention', role, title, children, action }) {
  return jsxs('div', {
    className: 'fc-banner',
    'data-tone': tone,
    role: role || (tone === 'bad' ? 'alert' : 'status'),
    children: [
      jsx(Icon, { key: 'i', name: icon, size: 13 }),
      jsxs('div', { key: 'b', className: 'fc-banner-body', children: [title ? jsx('b', { children: title }) : null, title && children ? ' ' : null, children] }),
      action || null
    ]
  })
}

function LogDecisionModal({ ctx, target, tenant, onClose, onAppended }) {
  const [form, setForm] = useState(() => decisionDraftFor(target, tenant))
  const [tried, setTried] = useState(false)
  const [state, run] = useAction()
  const phaseRef = useRef(null)
  const goRef = useRef(null)
  const cancelRef = useRef(null)
  const busy = state.status === 'busy'
  const errors = tried ? validateDecisionDraft(form) : {}
  const set = (k, v) => setForm(prev => ({ ...prev, [k]: v }))
  const submit = async e => {
    e?.preventDefault?.()
    if (busy) return
    setTried(true)
    const found = validateDecisionDraft(form)
    if (Object.keys(found).length || !form.confirm) return
    const body = humanDecisionBody(form, target)
    const res = await run(() => ctx.rest('/trail/decisions', { method: 'POST', body }))
    if (res.ok) onAppended(res.result?.decision || null, target)
  }
  const field = (name, label, control, { optional, hint } = {}) => jsxs('div', {
    key: name,
    className: 'fc-field',
    'data-field': name,
    children: [
      jsxs('label', { htmlFor: `fc-hd-${name}`, children: [label, optional ? jsx('span', { key: 'o', className: 'fc-field-hint', children: ' (optional)' }) : null] }),
      control({
        id: `fc-hd-${name}`,
        name,
        disabled: busy,
        'aria-invalid': errors[name] ? 'true' : undefined,
        'aria-describedby': errors[name] ? `fc-hd-${name}-e` : undefined,
        value: form[name],
        onChange: ev => set(name, ev?.target?.value ?? '')
      }),
      errors[name] ? jsx('span', { className: 'fc-field-err', id: `fc-hd-${name}-e`, children: errors[name] }) : hint ? jsx('span', { className: 'fc-field-hint', children: hint }) : null
    ]
  })
  const input = (extra = {}) => props => jsx('input', { ...props, className: `fc-input${extra.mono ? ' fc-mono' : ''}`, type: 'text', spellCheck: false, autoComplete: 'off', placeholder: extra.placeholder })
  const textarea = (rows, extra = {}) => props => jsx('textarea', { ...props, className: `fc-input${extra.mono ? ' fc-mono' : ''}`, rows, placeholder: extra.placeholder })
  const err = state.status === 'error' ? state.error : null
  const title = target ? 'Supersede decision' : 'Log human decision'
  return jsx(ConfirmModal, {
    id: 'fc-hd',
    title,
    sub: target ? 'Appends a correction. The original row stays in trail.db, shown struck through beneath yours.' : 'Record an override, approval or note in the append-only ledger.',
    busy,
    onClose,
    initialFocusRef: phaseRef,
    lastRef: [goRef, cancelRef],
    foot: close => [
      jsxs('span', { key: 'n', className: 'fc-m-note', children: [jsx(Icon, { name: 'person', size: 12 }), 'Appends immutable human decision (actor: human)'] }),
      jsx('span', { key: 's', className: 'fc-spacer' }),
      jsx('button', { key: 'c', type: 'button', className: 'fc-btn', ref: cancelRef, 'data-action': 'cancel-modal', disabled: busy, onClick: close, children: 'Cancel' }),
      jsxs('button', {
        key: 'go',
        type: 'button',
        className: 'fc-btn fc-btn--primary',
        ref: goRef,
        'data-action': 'append-decision',
        disabled: busy || !form.confirm,
        onClick: submit,
        children: [busy ? jsx('span', { key: 'sp', className: 'fc-spin', 'aria-hidden': 'true' }) : null, busy ? 'Appending…' : 'Confirm & append to trail.db']
      })
    ],
    children: jsxs('form', {
      className: 'fc-m-form',
      noValidate: true,
      onSubmit: submit,
      children: [
        err ? jsx(Banner, { key: 'err', tone: 'bad', title: err.status ? `Not recorded (${err.status}).` : 'Not recorded.', children: `${err.status ? err.detail : 'Network error.'} Nothing was appended. Your entries are kept.` }) : null,
        target
          ? jsxs('blockquote', {
              key: 'q',
              className: 'fc-m-quote',
              'data-target': target.id,
              children: [
                jsx('b', { children: humanDecision(target.decision) }),
                ` · ${target.phase || '—'} · ${target.result || '—'} · `,
                jsx('code', { children: target.id }),
                target.why ? [jsx('br', { key: 'br' }), target.why] : null
              ]
            })
          : null,
        jsxs('div', {
          key: 'g1',
          className: 'fc-m-grid2',
          children: [
            field('phase', 'Phase', props => jsx('select', { ...props, ref: phaseRef, className: 'fc-input', children: HUMAN_DECISION_PHASES.map(p => jsx('option', { key: p, value: p, children: p })) })),
            field('decision', 'Decision', input({ mono: true, placeholder: 'override_gate' }))
          ]
        }),
        field('why', 'Why', textarea(3, { placeholder: 'The reason a future reader needs' })),
        jsxs('div', {
          key: 'g2',
          className: 'fc-m-grid2',
          children: [
            field('result', 'Result', input({ placeholder: 'approved' }), { optional: true }),
            field('repo', 'Repo', input({ mono: true }), { optional: true }),
            field('tenant', 'Workstream', input({ mono: true }), { optional: true }),
            field('card_id', 'Card ID', input({ mono: true }), { optional: true })
          ]
        }),
        field('evidence', 'Evidence JSON', textarea(3, { mono: true, placeholder: '{"pr_number": 44}' }), { optional: true, hint: 'A JSON object, 16 KiB or less.' }),
        jsxs('label', {
          key: 'ok',
          className: 'fc-chk fc-chk--wrap',
          children: [
            jsx('input', { type: 'checkbox', 'data-field': 'confirm', checked: !!form.confirm, disabled: busy, onChange: ev => set('confirm', !!ev?.target?.checked) }),
            jsxs('span', {
              children: [
                'I confirm this appends an immutable decision with ',
                jsx('b', { key: 'a', children: 'actor: human' }),
                target ? [' superseding ', jsx('code', { key: 'c', children: target.id })] : null,
                '.'
              ]
            })
          ]
        })
      ]
    })
  })
}

function DhPill({ ctx, pill }) {
  const kids = [pill.label ? jsx('span', { key: 'k', className: 'fc-chip-k', children: pill.label }) : null, pill.text]
  if (pill.href) {
    return jsxs('a', {
      className: 'fc-chip',
      href: pill.href,
      target: '_blank',
      rel: 'noopener noreferrer',
      title: pill.title,
      'data-ev': pill.key,
      'data-action': 'open-pr',
      onClick: e => {
        e?.preventDefault?.()
        e?.stopPropagation?.()
        openExternalUrl(ctx, pill.href)
      },
      children: [...kids, jsx(Icon, { key: 'ext', name: 'ext', size: 10 })]
    })
  }
  return jsxs('span', { className: 'fc-chip', 'data-ev': pill.key, 'data-tone': pill.tone || undefined, title: pill.title, children: kids })
}

function EvidenceInspector({ ctx, d, tab, onTab, onSupersede, superseded }) {
  const ev = d.evidence && typeof d.evidence === 'object' && !Array.isArray(d.evidence) ? safeEvidence(d.evidence) : {}
  const val = v => (v == null || v === '' ? '—' : typeof v === 'object' ? JSON.stringify(stripHoldout(v)) : String(v))
  const field = v => (barrierStr(v) ?? (v == null || v === '' ? '—' : mentionsHoldout(v) ? '[redacted]' : String(v)))
  const fields = [
    ['why', field(d.why), true],
    ['id', d.id],
    ['decision', field(d.decision)],
    ['actor', field(d.actor)],
    ['card_id', field(d.card_id)],
    ['tenant', field(d.tenant)],
    ['repo_slug', field(d.repo_slug)],
    ['ts', field(d.ts)],
    ...(d.supersedes_id ? [['supersedes_id', String(d.supersedes_id)]] : []),
    ...Object.entries(ev).map(([k, v]) => [`evidence.${k}`, val(v)])
  ]
  const json = JSON.stringify(stripHoldout({ ...d }), null, 2)
  return jsxs('div', {
    className: 'fc-dh-insp',
    id: `insp-${domSafe(d.id)}`,
    role: 'region',
    'aria-label': 'Evidence inspector',
    onClick: e => e?.stopPropagation?.(),
    children: [
      jsxs('div', {
        key: 'tabs',
        className: 'fc-dh-insp-tabs',
        children: [
          jsx('span', {
            className: 'fc-seg',
            role: 'group',
            'aria-label': 'Evidence view',
            children: [['fields', 'Fields'], ['json', 'JSON']].map(([v, l]) => jsx('button', { key: v, type: 'button', 'data-tab': v, 'aria-pressed': String(tab === v), onClick: () => onTab(v), children: l }))
          }),
          jsx('span', { className: 'fc-spacer' }),
          jsxs('button', { type: 'button', className: 'fc-btn fc-btn--s', 'data-action': 'copy-decision-id', onClick: () => copyText(ctx, d.id, 'decision ID'), children: [jsx(Icon, { name: 'copy', size: 11 }), 'Copy id'] }),
          superseded
            ? null
            : jsxs('button', { type: 'button', className: 'fc-btn fc-btn--s', 'data-action': 'supersede', onClick: () => onSupersede(d), children: [jsx(Icon, { name: 'link', size: 11 }), 'Supersede…'] })
        ]
      }),
      tab === 'json'
        ? jsx('pre', { key: 'json', className: 'fc-dh-json', children: json })
        : jsx('dl', {
            key: 'kv',
            className: 'fc-dh-kv',
            children: fields.flatMap(([k, v, prose]) => [
              jsx('dt', { key: `${k}-t`, children: k }),
              jsx('dd', { key: `${k}-d`, className: prose ? 'fc-dh-prose' : undefined, 'data-k': k, children: v })
            ])
          })
    ]
  })
}

function SupersessionChain({ d, chain, open, loading, failed, onToggle }) {
  const n = chain.priors.length + chain.missing.length
  const listId = `chain-${domSafe(d.id)}`
  const sha = d.evidence && typeof d.evidence === 'object' && barrierStr(String(d.evidence.head_sha ?? '')) ? String(d.evidence.head_sha).slice(0, 7) : null
  return jsxs('div', {
    className: 'fc-dh-chain',
    onClick: e => e?.stopPropagation?.(),
    children: [
      jsxs('button', {
        key: 'b',
        type: 'button',
        className: 'fc-dh-chain-btn',
        'data-action': 'toggle-chain',
        'aria-expanded': String(!!open),
        'aria-controls': listId,
        onClick: onToggle,
        children: [jsx(Icon, { name: 'link', size: 11 }), `Supersedes ${plural(n, 'prior decision', 'prior decisions')} · original → corrected`]
      }),
      open
        ? jsxs('ul', {
            key: 'l',
            className: 'fc-dh-chain-list',
            id: listId,
            children: [
              ...chain.priors.map(p => jsxs('li', {
                key: p.id,
                className: 'fc-dh-chain-item',
                'data-id': p.id,
                children: [
                  jsx('span', { className: 'fc-mono', children: fmtTrailClock(p.ts) }),
                  ' · ',
                  jsx('s', { children: `${humanDecision(p.decision)} — ${p.result || '—'}` }),
                  jsx('span', { className: 'fc-trail-sr', children: ' (superseded)' }),
                  barrierStr(p.why) ? jsx('span', { className: 'fc-dh-chain-why', children: p.why }) : null,
                  jsxs('span', {
                    className: 'fc-dh-chain-fix',
                    children: [
                      jsx('span', { className: 'fc-dh-arrow', children: '→ corrected:' }),
                      ` ${d.result || '—'}`,
                      sha ? [' at ', jsx('code', { key: 's', children: sha })] : null,
                      ' · ',
                      jsx('code', { children: p.id })
                    ]
                  })
                ]
              })),
              ...chain.missing.map(id => jsxs('li', {
                key: `m:${id}`,
                className: 'fc-dh-chain-item',
                'data-missing': id,
                children: [loading ? 'Loading prior decision ' : 'Prior decision ', jsx('code', { children: id }), loading ? '…' : failed ? ' could not be loaded.' : ' is outside the loaded window.']
              }))
            ]
          })
        : null
    ]
  })
}

function DecisionRow({ ctx, d, gh, tier, findings, open, tab, flash, superseded, chain, chainOpen, chainLoading, showWs, wsLabel, wsKind, chainFailed, onToggle, onTab, onSupersede, onChain, onJump, rowRef, chevRef }) {
  const human = low(d.actor) === 'human'
  const tone = ROW_TONE(d.result)
  const hardFail = ['escape', 'watchdog'].includes(low(d.phase)) || TIER1_RESULTS.has(low(d.result)) || /override|policy/.test(low(d.decision))
  const accent = tier !== 1 ? undefined : hardFail || findings.some(f => f.kind === 'gate_loosening') ? 'bad' : 'warn'
  const title = humanDecision(d.decision)
  const scan = tier === 3 ? { top: [], more: 0 } : scanEvidence(d, gh)
  const why = barrierStr(d.why)
  const inspId = `insp-${domSafe(d.id)}`
  const toggle = e => {
    const sel = globalThis.getSelection?.()
    if (sel && String(sel.toString?.() || '')) return
    e?.stopPropagation?.()
    onToggle(d.id)
  }
  return jsxs('li', {
    className: 'fc-dh-row',
    id: `trail-${domSafe(d.id)}`,
    ref: rowRef,
    'data-id': d.id,
    'data-tier': String(tier),
    'data-accent': accent,
    'data-human': human && tier !== 1 ? 'true' : undefined,
    'data-superseded': String(!!superseded),
    'data-flash': String(!!flash),
    'aria-expanded': String(!!open),
    children: [
      jsxs('div', {
        key: 'main',
        className: 'fc-dh-main',
        onClick: toggle,
        children: [
          jsxs('div', {
            key: 't',
            className: 'fc-dh-time',
            children: [jsx('time', { dateTime: d.ts, title: d.ts, children: fmtTrailClock(d.ts) }), jsx('small', { children: fmtAgo(d.ts) })]
          }),
          jsx('span', { key: 'p', className: 'fc-dh-phase', 'data-phase': d.phase || '', title: d.phase || '', children: d.phase || '—' }),
          jsxs('div', {
            key: 'b',
            className: 'fc-dh-body',
            children: [
              jsxs('h3', {
                className: 'fc-dh-title',
                title: d.decision,
                children: [
                  title,
                  superseded ? jsx('span', { key: 'sr', className: 'fc-trail-sr', children: ' (superseded)' }) : null,
                  jsxs('span', {
                    key: 'a',
                    className: 'fc-dh-actor',
                    'data-actor': d.actor || '',
                    children: [human ? jsx(Icon, { key: 'i', name: 'person', size: 11 }) : null, d.actor || 'unknown']
                  })
                ]
              }),
              why ? jsx('p', { className: 'fc-dh-why', children: why }) : null,
              findings.length || (superseded && Array.isArray(d.superseded_by) && d.superseded_by.length) || scan.top.length || showWs
                ? jsxs('div', {
                    className: 'fc-dh-ev',
                    children: [
                      ...findings.map(f => jsxs('span', { key: `a:${f.kind}`, className: 'fc-chip', 'data-tone': f.kind === 'gate_loosening' ? 'bad' : 'warn', 'data-ev': 'audit', children: [jsx(Icon, { name: 'attention', size: 10 }), `audit · ${f.label}`] })),
                      superseded && Array.isArray(d.superseded_by) && d.superseded_by.length
                        ? jsxs('button', {
                            key: 'sb',
                            type: 'button',
                            className: 'fc-chip fc-chip--btn',
                            'data-tone': 'info',
                            'data-ev': 'superseded-by',
                            'data-action': 'jump-correction',
                            'data-jump': String(d.superseded_by[0]),
                            'aria-label': `Superseded by ${d.superseded_by[0]}, jump to it`,
                            onClick: e => {
                              e?.stopPropagation?.()
                              return onJump(String(d.superseded_by[0]))
                            },
                            children: [jsx(Icon, { key: 'i', name: 'link', size: 10 }), 'superseded by ', jsx('code', { key: 'c', children: String(d.superseded_by[0]) })]
                          })
                        : null,
                      ...scan.top.map(p => jsx(DhPill, { key: p.key, ctx, pill: p })),
                      scan.more > 0 ? jsx('span', { key: 'more', className: 'fc-chip fc-chip--muted', 'data-ev': 'more', children: `+${scan.more} in inspector` }) : null,
                      showWs && d.tenant
                        ? jsxs('span', {
                            key: 'ws',
                            className: 'fc-chip',
                            'data-ev': 'tenant',
                            title: d.tenant,
                            children: [
                              jsx('span', { key: 'k', className: 'fc-chip-k', children: 'ws' }),
                              wsLabel,
                              BADGED_MODES.has(wsKind) && d.decision !== 'start_workstream' ? jsx('span', { key: 'm', className: 'fc-dh-mode', children: wsKind }) : null
                            ]
                          })
                        : null
                    ]
                  })
                : null
            ]
          }),
          jsxs('div', {
            key: 'e',
            className: 'fc-dh-end',
            children: [
              jsx('span', { className: 'fc-chip fc-dh-result', 'data-tone': tone, children: d.result || '—' }),
              jsx('button', {
                type: 'button',
                className: 'fc-dh-chev',
                ref: chevRef,
                'data-action': 'toggle-row',
                'aria-expanded': String(!!open),
                'aria-controls': inspId,
                'aria-label': `${open ? 'Collapse' : 'Expand'} evidence for ${title}`,
                onClick: toggle,
                children: jsx(Icon, { name: 'chev', size: 12 })
              })
            ]
          })
        ]
      }),
      chain && (chain.priors.length || chain.missing.length)
        ? jsx(SupersessionChain, { key: 'chain', d, chain, open: chainOpen, loading: chainLoading, failed: chainFailed, onToggle: () => onChain(d.id) })
        : null,
      open ? jsx(EvidenceInspector, { key: 'insp', ctx, d, tab, onTab: v => onTab(d.id, v), onSupersede, superseded }) : null
    ]
  })
}

function FoldChip({ fold, open, onToggle, children }) {
  const t = foldText(fold)
  const id = `fold-${domSafe(fold.key)}`
  return jsxs('li', {
    className: 'fc-dh-foldwrap',
    'data-fold': fold.key,
    children: [
      jsxs('button', {
        key: 'b',
        type: 'button',
        className: 'fc-dh-fold',
        'data-action': 'toggle-fold',
        'aria-expanded': String(!!open),
        'aria-controls': id,
        onClick: onToggle,
        children: [
          jsx('span', { key: 'i', className: 'fc-dh-fold-ico', children: jsx(Icon, { name: 'chev', size: 12, className: 'fc-dh-fold-chev' }) }),
          jsx('span', { key: 'p', className: 'fc-dh-phase', 'data-phase': fold.phase, children: fold.phase || '—' }),
          jsxs('span', { key: 'l', className: 'fc-dh-fold-l', children: [jsx('b', { children: t.lead }), ` · ${t.detail} · `, jsx('span', { className: 'fc-mono', children: t.span })] }),
          jsx('span', { key: 'x', className: 'fc-dh-fold-x', children: open ? 'Collapse' : 'Expand' })
        ]
      }),
      open ? jsx('ul', { key: 'rows', className: 'fc-dh-list fc-dh-foldrows', id, children }) : null
    ]
  })
}

function DhSkeleton() {
  const row = (i, a, b) => jsxs('div', {
    key: i,
    className: 'fc-dh-skel',
    'aria-hidden': 'true',
    children: [jsx('span', { style: { width: '80%' } }), jsx('span', { style: { width: '100%' } }), jsxs('div', { children: [jsx('span', { style: { width: `${a}%` } }), jsx('span', { style: { width: `${b}%` } })] })]
  })
  return [row(0, 40, 82), row(1, 34, 70), row(2, 46, 88), row(3, 30, 64), row(4, 38, 76), jsx('span', { key: 'sr', className: 'fc-trail-sr', children: 'Loading decision history…' })]
}

function TrailSection({ ctx, reposQ }) {
  const routeTenant = routeQueryParam('tenant')
  const [preset, setPreset] = useState('key')
  const [q, setQ] = useState('')
  const [qServer, setQServer] = useState('')
  const [repo, setRepo] = useState('')
  const [tenant, setTenant] = useState(routeTenant || '')
  const [phase, setPhase] = useState('')
  const [showSup, setShowSup] = useState(false)
  const [scoped, setScoped] = useState(routeTenant || null)
  const [limit, setLimit] = useState(TRAIL_LIMIT)
  const [shown, setShown] = useState(DH_PAGE)
  const [open, setOpen] = useState({})
  const [tabs, setTabs] = useState({})
  const [folds, setFolds] = useState({})
  const [chains, setChains] = useState({})
  const [chainLoading, setChainLoading] = useState({})
  const [chainFailed, setChainFailed] = useState({})
  const [auditOpen, setAuditOpen] = useState(false)
  const [reveal, setReveal] = useState(null)
  const [flash, setFlash] = useState(null)
  const [msg, setMsg] = useState('')
  const [pinned, setPinned] = useState({})
  const [modal, setModal] = useState(null)
  const headingRef = useRef(null)
  const rowEls = useRef({})
  const chevEls = useRef({})
  const pendingJump = useRef(null)
  const flashTimer = useRef(null)
  const qTimer = useRef(null)
  const jumpSeq = useRef(0)
  const queryClient = useQueryClient()

  const baseKey = scoped ? [...TRAIL_QUERY_KEY, 'tenant', scoped] : TRAIL_QUERY_KEY
  const queryKey = limit === TRAIL_LIMIT ? baseKey : [...baseKey, 'limit', limit]
  const query = useQuery({
    queryKey,
    queryFn: () => ctx.rest(trailPath({ tenant: scoped, limit })),
    refetchInterval: 30000
  })
  // Server-side signal / keyword page beyond the base window; refreshed on key change or invalidation,
  // never on a timer of its own (the base query's 30s interval stays the only one).
  const signalOn = preset !== 'all' || !!qServer
  const signalKey = [...queryKey, 'signal', preset, ...(qServer ? ['q', qServer] : [])]
  const signalQ = useQuery({
    queryKey: signalKey,
    queryFn: () => ctx.rest(trailPath({ tenant: scoped, limit, signal: preset, q: qServer })),
    enabled: signalOn,
    staleTime: 30000,
    retry: false
  })
  const auditKey = ['factonio', 'trail', 'audit', scoped || 'all']
  const auditQ = useQuery({
    queryKey: auditKey,
    queryFn: () => ctx.rest(scoped ? `/trail/audit?tenant=${encodeURIComponent(scoped)}` : '/trail/audit'),
    staleTime: 30000,
    retry: false
  })
  const audit = summarizeTrailAudit(auditQ?.data, auditQ?.isError)

  // Each successful refresh of the polled base page also refreshes the audit and an active server-filtered
  // page, so they never stay cached behind a newer ledger. No timer of their own; a key change only re-arms.
  const baseMark = useRef(null)
  const baseStamp = Number(query?.dataUpdatedAt) || query?.data || null
  const baseKeyStr = JSON.stringify(queryKey)
  useEffect(() => {
    const prev = baseMark.current
    baseMark.current = { key: baseKeyStr, stamp: baseStamp }
    if (!prev || prev.key !== baseKeyStr || prev.stamp == null || baseStamp == null || Object.is(prev.stamp, baseStamp)) return
    auditQ?.refetch?.()
    if (signalOn) signalQ?.refetch?.()
  }, [baseKeyStr, baseStamp])
  useEffect(() => {
    if (!routeTenant) return
    setTenant(routeTenant)
    setScoped(routeTenant)
    setRepo('')
    setPhase('')
    headingRef.current?.scrollIntoView?.({ block: 'start', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
  }, [routeTenant])

  const data = query.data
  const isError = !!query.isError
  const hasData = data != null
  const loading = !hasData && !isError
  const signalRows = signalOn && signalQ?.data && !signalQ?.isError ? trailDecisions(signalQ.data) : []
  const loaded = trailDecisions(data)
  const known = new Map()
  for (const d of [...loaded, ...signalRows, ...Object.values(pinned)]) if (d && d.id && !known.has(d.id)) known.set(d.id, d)
  const all = trailDecisions({ decisions: [...known.values()] })
  const supersededIds = new Set([...all.filter(d => d.is_superseded).map(d => d.id), ...all.map(d => d.supersedes_id).filter(Boolean)])
  const isSup = d => supersededIds.has(d.id)
  const chainMap = buildSupersessionChains(all)
  const empty = hasData && loaded.length === 0 && !scoped && !signalRows.length

  const registered = Array.isArray(reposQ?.data?.repos) ? reposQ.data.repos : []
  const ghBySlug = Object.fromEntries(registered.filter(r => r?.slug && r?.repo).map(r => [r.slug, r.repo]))
  const ghRepos = [...new Set([...registered.map(r => r?.repo).filter(Boolean), ...all.map(d => trailGithubRepo(d.repo_slug, ghBySlug)).filter(Boolean)])]
  const summary = data?.summary && typeof data.summary === 'object' ? data.summary : {}
  const total = Number.isFinite(summary.total) ? summary.total : all.length

  const repoOpts = [...new Set([...all.map(d => d.repo_slug).filter(Boolean), ...registered.map(r => r?.slug).filter(Boolean), ...(repo ? [repo] : [])])].sort()
  const tenantOpts = [...new Set([
    ...Object.keys(summary.by_tenant && typeof summary.by_tenant === 'object' ? summary.by_tenant : {}).filter(t => t && t !== 'unknown'),
    ...all.map(d => d.tenant).filter(Boolean),
    ...(tenant ? [tenant] : [])
  ])].filter(t => !mentionsHoldout(t)).sort()
  const byTenantRows = groupBy(all.filter(d => d.tenant), d => d.tenant)
  const modeKind = t => workstreamMode(t, null, [], byTenantRows.get(t) || []).kind
  const wsOptionLabel = t => {
    const k = modeKind(t)
    const slug = trailWsLabel(t, ghRepos)
    if (!BADGED_MODES.has(k)) return slug
    return k === 'investigate' ? `${slug} · investigate · read-only` : `${slug} · ${k}`
  }
  const inScope = d => (!repo || d.repo_slug === repo) && (!tenant || d.tenant === tenant)
  const scopeRows = all.filter(inScope)
  const presetOk = (d, p) => (p === 'all' ? true : p === 'key' ? !isRoutineDecision(d) : isAttentionDecision({ ...d, is_superseded: isSup(d) }, audit.ids))
  const passes = (d, f) => (!f.repo || d.repo_slug === f.repo) && (!f.tenant || d.tenant === f.tenant) && (!f.phase || d.phase === f.phase) &&
    (f.showSup || !isSup(d)) && presetOk(d, f.preset) && decisionMatchesQuery(d, f.q)
  const filters = { repo, tenant, phase, showSup, preset, q }
  const visible = all.filter(d => passes(d, filters))
  const routineHidden = preset === 'key' ? scopeRows.filter(d => isRoutineDecision(d) && (showSup || !isSup(d))).length : 0
  const attnN = scopeRows.filter(d => isAttentionDecision({ ...d, is_superseded: isSup(d) }, audit.ids)).length
  const phaseN = p => scopeRows.filter(d => d.phase === p).length
  const knownPhases = new Set(TRAIL_PHASE_ORDER)
  const extraPhases = [...new Set([...all.map(d => d.phase).filter(Boolean), ...(phase ? [phase] : [])])].filter(p => !knownPhases.has(p) && !mentionsHoldout(p)).sort()
  const anyFilter = !!(repo || tenant || phase || q || preset !== 'all')
  const controlsOff = !hasData

  // Keep a pending jump until its row mounts: grow the rendered page to reach it first, and drop it only
  // once loading settled and the target is neither visible nor mountable.
  useEffect(() => {
    const id = pendingJump.current
    if (!id) return
    const idx = visible.findIndex(d => d.id === id)
    if (idx >= shown) {
      setShown(idx + 1)
      return
    }
    const row = idx >= 0 ? rowEls.current[id] : null
    if (!row) {
      if (!loading) pendingJump.current = null
      return
    }
    pendingJump.current = null
    row.scrollIntoView?.({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
    ;(chevEls.current[id] || row).focus?.({ preventScroll: true })
    clearTimeout(flashTimer.current)
    flashTimer.current = setTimeout(() => setFlash(cur => (cur === id ? null : cur)), 1600)
  })

  const resetFilters = () => {
    setRepo(''); setTenant(''); setPhase(''); setQ(''); setQServer(''); setScoped(null)
  }
  const clearAll = () => {
    resetFilters()
    setShowSup(false)
    setPreset('all')
    setMsg('Filters cleared.')
  }
  const onQuery = v => {
    const next = String(v ?? '')
    setQ(next)
    clearTimeout(qTimer.current)
    qTimer.current = setTimeout(() => setQServer(next.trim()), 250)
  }
  const onFilter = (key, value) => {
    const v = value || ''
    if (key === 'repo') setRepo(v)
    if (key === 'phase') setPhase(v)
    if (key === 'tenant') {
      setTenant(v)
      if (scoped && v !== scoped) setScoped(null)
    }
    const next = { ...filters, [key]: v }
    setMsg(`${plural(all.filter(d => passes(d, next)).length, 'decision', 'decisions')} shown.`)
  }
  const onPreset = p => {
    setPreset(p)
    setShown(DH_PAGE)
    const next = { ...filters, preset: p }
    setMsg(`${SIGNAL_PRESETS.find(x => x[0] === p)?.[1] || p}: ${plural(all.filter(d => passes(d, next)).length, 'decision', 'decisions')} shown.`)
  }
  const onShowSup = v => {
    setShowSup(v)
    setMsg(v ? 'Superseded decisions shown in place.' : 'Superseded decisions hidden; open a chain to see them.')
  }

  // Single-row lookup; older hosts without GET /trail/{id} fall back to one wide unscoped /trail page.
  const fetchDecision = async (id, mayBeOutside) => {
    try {
      const res = await ctx.rest(`/trail/${encodeURIComponent(id)}`)
      if (res?.decision?.id === id) return res.decision
    } catch (err) {
      if (describeSaveError(err).status === 404) return null
    }
    if (!mayBeOutside) return null
    return trailDecisions(await ctx.rest('/trail?limit=1000')).find(d => d.id === id) || null
  }
  // Clear the filters (Show decision), unfold and open the row, then scroll, focus and flash it.
  const jumpTo = async (id, announce) => {
    const seq = ++jumpSeq.current
    const hadFilters = !visible.some(d => d.id === id)
    resetFilters()
    setPreset('all')
    setReveal(id)
    setOpen(cur => ({ ...cur, [id]: true }))
    let target = all.find(d => d.id === id) || null
    if (!target) {
      pendingJump.current = null
      setMsg(`Loading ${id}…`)
      target = await fetchDecision(id, true).catch(() => null)
      if (seq !== jumpSeq.current) return
      if (!target) {
        setMsg(`Could not load decision ${id}.`)
        return
      }
      const row = target
      setPinned(cur => ({ ...cur, [row.id]: row }))
    }
    if (target.is_superseded || isSup(target)) setShowSup(true)
    pendingJump.current = id
    setFlash(id)
    setMsg(announce || (hadFilters ? `Filters cleared to show ${id}.` : `Jumped to ${id}.`))
  }
  const loadChain = async rootId => {
    let map = new Map(all.map(d => [d.id, d]))
    const found = {}
    setChainLoading(cur => ({ ...cur, [rootId]: true }))
    for (let hop = 0; hop < 10; hop++) {
      const missing = buildSupersessionChains([...map.values()])[rootId]?.missing || []
      if (!missing.length) break
      const row = await fetchDecision(missing[0], true).catch(() => null)
      if (!row) {
        setChainFailed(cur => ({ ...cur, [rootId]: true }))
        break
      }
      found[row.id] = row
      map = new Map([...map, [row.id, row]])
    }
    setChainLoading(cur => ({ ...cur, [rootId]: false }))
    if (Object.keys(found).length) setPinned(cur => ({ ...cur, ...found }))
  }
  const toggleChain = id => {
    const next = !(chains[id] ?? showSup)
    setChains(cur => ({ ...cur, [id]: next }))
    if (next && chainMap[id]?.missing.length) return loadChain(id)
    return null
  }
  const onAppended = (row, target) => {
    setModal(null)
    resetFilters()
    setPreset('key')
    if (row?.id) {
      setPinned(cur => ({ ...cur, [row.id]: row }))
      if (target) setChains(cur => ({ ...cur, [row.id]: true }))
      pendingJump.current = row.id
      setFlash(row.id)
      setMsg(`Appended ${row.decision || 'decision'} (actor: human)`)
    } else {
      setMsg('Decision appended (actor: human)')
    }
    host.notify?.({ kind: 'info', message: `Appended ${row?.decision || 'decision'} to trail.db` })
    invalidateKeys(queryClient, [['factonio', 'trail']])
  }

  const select = (id, key, label, children, value) => jsxs('span', {
    key,
    className: 'fc-trail-sel',
    children: [
      jsx('label', { htmlFor: id, children: label }),
      jsx('select', { id, className: 'fc-input', 'data-filter': key, 'data-set': String(!!value), disabled: controlsOff, value, onChange: ev => onFilter(key, ev?.target?.value), children })
    ]
  })
  const opt = (v, l, extra) => jsx('option', { key: v || '__all', value: v, ...extra, children: l })
  const logBtn = jsxs('button', {
    key: 'log',
    type: 'button',
    className: 'fc-btn fc-btn--primary',
    'data-action': 'log-decision',
    disabled: loading,
    onClick: () => setModal({ target: null }),
    children: [jsx(Icon, { name: 'plus', size: 12 }), 'Log Human Decision']
  })
  const head = jsxs('div', {
    key: 'head',
    className: 'fc-sec-head',
    children: [
      jsxs('div', {
        className: 'fc-trail-head-l',
        children: [
          jsx('h2', { id: 'fc-trail-h', ref: headingRef, children: 'Decision History' }),
          jsxs('p', { className: 'fc-sec-sub', children: ['Append-only ledger from ', jsx('span', { className: 'fc-mono', children: 'trail.db' }), '. Key decisions by default; routine gate ticks are folded.'] })
        ]
      }),
      jsxs('div', {
        className: 'fc-dh-status',
        children: [
          hasData && audit.available && audit.clean
            ? jsxs('span', { key: 'clean', className: 'fc-chip', 'data-tone': 'ok', 'data-field': 'audit-clean', title: 'trail --audit: 0 findings', children: [jsx(Icon, { name: 'shieldOk', size: 12 }), 'Audit clean'] })
            : null,
          jsxs('span', { key: 'n', className: 'fc-chip', 'data-field': 'total', children: [jsx('b', { className: 'fc-mono', children: hasData ? String(total) : '–' }), ' decisions'] }),
          logBtn
        ]
      })
    ]
  })

  const auditBanner = hasData && audit.available && !audit.clean
    ? jsxs('div', {
        key: 'audit',
        className: 'fc-dh-audit',
        role: 'region',
        'aria-label': 'Trail audit findings',
        children: [
          jsxs('button', {
            key: 'h',
            type: 'button',
            className: 'fc-dh-audit-head',
            'data-action': 'toggle-audit',
            'aria-expanded': String(auditOpen),
            'aria-controls': 'fc-dh-audit-list',
            onClick: () => setAuditOpen(!auditOpen),
            children: [
              jsx(Icon, { key: 'i', name: 'attention', size: 13 }),
              jsx('b', { key: 't', children: `${plural(audit.findings.length, 'audit finding needs', 'audit findings need')} attention` }),
              jsx('span', {
                key: 'c',
                className: 'fc-dh-audit-cats',
                children: audit.counts.map(([k, n]) => jsx('span', { key: k, className: 'fc-chip', 'data-tone': k === 'gate_loosening' ? 'bad' : 'warn', 'data-kind': k, children: `${AUDIT_KIND_LABEL[k]} ${n}` }))
              }),
              jsx('span', { key: 'm', className: 'fc-m-note', children: `trail --audit${audit.inspected != null ? ` · ${audit.inspected.toLocaleString('en-US')} inspected` : ''}` }),
              jsx(Icon, { key: 'chev', name: 'chev', size: 12, className: 'fc-dh-audit-chev' })
            ]
          }),
          auditOpen
            ? jsx('ul', {
                key: 'l',
                className: 'fc-dh-audit-list',
                id: 'fc-dh-audit-list',
                children: audit.findings.map((f, i) => jsxs('li', {
                  key: `${f.decisionId}:${f.kind}:${i}`,
                  className: 'fc-dh-audit-item',
                  'data-id': f.decisionId,
                  children: [
                    jsx('span', { className: 'fc-chip', 'data-tone': f.severity === 'high' ? 'bad' : 'warn', children: f.severity }),
                    jsxs('div', { children: [jsx('p', { children: f.message }), jsxs('small', { children: [`${f.label} · `, jsx('code', { children: f.decisionId })] })] }),
                    jsxs('button', { type: 'button', className: 'fc-btn fc-btn--s', 'data-action': 'show-decision', 'data-id': f.decisionId, onClick: () => jumpTo(f.decisionId, 'Filters cleared · showing the flagged decision'), children: ['Show decision', jsx(Icon, { key: 'c', name: 'chev', size: 10 })] })
                  ]
                }))
              })
            : null
        ]
      })
    : null

  const bar = empty
    ? null
    : jsxs('div', {
        key: 'bar',
        className: 'fc-dh-bar fc-trail-bar',
        role: 'search',
        'aria-label': 'Filter decisions',
        'data-disabled': String(controlsOff),
        children: [
          jsxs('div', {
            key: 'r1',
            className: 'fc-dh-row1',
            children: [
              jsx('span', {
                className: 'fc-seg',
                role: 'group',
                'aria-label': 'Signal preset',
                children: SIGNAL_PRESETS.map(([v, l]) => jsxs('button', {
                  key: v,
                  type: 'button',
                  'data-action': 'preset',
                  'data-value': v,
                  'aria-pressed': String(preset === v),
                  disabled: controlsOff,
                  onClick: () => onPreset(v),
                  children: [l, v === 'attention' && hasData && attnN > 0 ? jsx('span', { key: 'n', className: 'fc-count', 'aria-label': `${attnN} need attention`, children: String(attnN) }) : null]
                }))
              }),
              jsxs('div', {
                className: 'fc-dh-search',
                children: [
                  jsx(Icon, { key: 'i', name: 'search', size: 13 }),
                  jsx('label', { key: 'l', className: 'fc-trail-sr', htmlFor: 'fc-dh-q', children: 'Search decisions' }),
                  jsx('input', {
                    key: 'q',
                    id: 'fc-dh-q',
                    type: 'search',
                    className: 'fc-input',
                    'data-filter': 'q',
                    'data-set': String(!!q),
                    placeholder: 'Search decision, why, card, PR, sha…',
                    autoComplete: 'off',
                    spellCheck: false,
                    disabled: controlsOff,
                    value: q,
                    onChange: ev => onQuery(ev?.target?.value)
                  }),
                  q ? jsx('button', { key: 'x', type: 'button', 'data-action': 'clear-q', 'aria-label': 'Clear search', onClick: () => onQuery(''), children: jsx(Icon, { name: 'x', size: 11 }) }) : null
                ]
              })
            ]
          }),
          jsxs('div', {
            key: 'r2',
            className: 'fc-dh-row2',
            children: [
              select('fc-trail-repo', 'repo', 'Repository', [opt('', 'All repos'), ...repoOpts.map(r => opt(r, r))], repo),
              select('fc-trail-ws', 'tenant', 'Workstream', [opt('', 'All workstreams'), ...tenantOpts.map(t => opt(t, wsOptionLabel(t)))], tenant),
              select('fc-trail-phase', 'phase', 'Phase', [
                opt('', `All phases (${scopeRows.length})`),
                ...[...TRAIL_PHASE_OPTGROUPS, ...(extraPhases.length ? [['Other', extraPhases]] : [])].map(([g, ps]) => jsx('optgroup', {
                  key: g,
                  label: g,
                  children: ps.map(p => {
                    const n = hasData ? phaseN(p) : 0
                    return opt(p, `${p} (${n})`, { disabled: n === 0 && phase !== p })
                  })
                }))
              ], phase),
              jsxs('label', {
                key: 'sup',
                className: 'fc-chk',
                children: [
                  jsx('input', { type: 'checkbox', 'data-filter': 'sup', checked: showSup, disabled: controlsOff, onChange: ev => onShowSup(!!ev?.target?.checked) }),
                  'Show superseded'
                ]
              }),
              jsx('span', {
                key: 'meta',
                className: 'fc-dh-meta',
                'aria-live': 'polite',
                'data-field': 'shown',
                children: loading ? 'Loading…' : hasData ? `${visible.length} shown${preset === 'key' && routineHidden ? ` · ${plural(routineHidden, 'routine tick', 'routine ticks')} hidden` : ''}` : ''
              })
            ]
          })
        ]
      })

  let errStatus = null
  if (isError) errStatus = describeSaveError(query.error).status
  const updated = Number(query.dataUpdatedAt) || 0
  const alert = isError
    ? jsxs('div', {
        key: 'alert',
        className: 'fc-alert fc-trail-alert',
        role: 'alert',
        children: [
          jsx(Icon, { name: 'attention' }),
          jsxs('div', {
            className: 'fc-trail-alert-body',
            children: [
              jsx('b', { children: hasData ? 'Couldn’t refresh the ledger.' : 'Couldn’t load the ledger.' }),
              hasData ? ` Showing data from ${updated ? fmtTrailClock(new Date(updated).toISOString()) : 'earlier'}.` : '',
              errStatus
                ? [' ', jsx('span', { key: 'r', className: 'fc-mono', children: 'GET /trail' }), ' returned ', jsx('span', { key: 's', className: 'fc-mono', children: String(errStatus) }), '.']
                : ' The trail endpoint could not be reached.',
              ' Nothing was changed.'
            ]
          }),
          jsxs('button', { type: 'button', className: 'fc-btn', 'data-action': 'retry', onClick: () => retryQuery(query, queryClient, queryKey), children: [jsx(Icon, { name: 'retry' }), 'Retry'] })
        ]
      })
    : null

  const renderRow = d => jsx(DecisionRow, {
    key: d.id,
    ctx,
    d,
    gh: trailGithubRepo(d.repo_slug, ghBySlug),
    tier: trailTier(d, audit.ids),
    findings: audit.byId.get(d.id) || [],
    open: !!open[d.id],
    tab: tabs[d.id] || 'fields',
    flash: flash === d.id,
    superseded: isSup(d),
    chain: chainMap[d.id] || null,
    chainOpen: chains[d.id] ?? showSup,
    chainLoading: !!chainLoading[d.id],
    chainFailed: !!chainFailed[d.id],
    showWs: !tenant && (!!repo || !!phase || !!q),
    wsLabel: trailWsLabel(d.tenant, ghRepos),
    wsKind: d.tenant ? modeKind(d.tenant) : null,
    onToggle: id => setOpen(cur => ({ ...cur, [id]: !cur[id] })),
    onTab: (id, v) => setTabs(cur => ({ ...cur, [id]: v })),
    onSupersede: t => setModal({ target: t }),
    onChain: toggleChain,
    onJump: id => jumpTo(id),
    rowRef: el => { rowEls.current[d.id] = el },
    chevRef: el => { chevEls.current[d.id] = el }
  })

  let body
  if (loading) {
    body = jsx('div', { key: 'body', className: 'fc-dh-scroll', 'aria-busy': 'true', 'aria-label': 'Loading decisions', children: jsx(DhSkeleton, {}) })
  } else if (!hasData) {
    body = null
  } else if (empty) {
    body = jsx('div', {
      key: 'body',
      className: 'fc-dh-scroll',
      children: jsxs('div', {
        className: 'fc-trail-state',
        'data-state': 'empty',
        children: [
          jsx('span', { className: 'fc-trail-state-ico', children: jsx(Icon, { name: 'trail', size: 16 }) }),
          jsx('h3', { children: 'No decisions yet' }),
          jsx('p', { children: 'Gates, pivots and overrides are appended to trail.db as workstreams run. You can log a human decision now, or inspect the ledger from a terminal:' }),
          jsx('div', { className: 'fc-step-cmd', children: 'hermes factonio trail --limit 20' })
        ]
      })
    })
  } else if (!visible.length) {
    const chips = [
      preset !== 'all' ? ['preset', `preset: ${SIGNAL_PRESETS.find(x => x[0] === preset)?.[1]}`] : null,
      q ? ['q', `“${mentionsHoldout(q) ? '[redacted]' : q}”`] : null,
      repo ? ['repo', `repo ${repo}`] : null,
      tenant ? ['ws', [`workstream ${trailWsLabel(tenant, ghRepos)}`, BADGED_MODES.has(modeKind(tenant)) ? jsx('span', { key: 'm', className: 'fc-dh-mode', children: modeKind(tenant) }) : null]] : null,
      phase ? ['phase', `phase ${phase}`] : null
    ].filter(Boolean)
    body = jsx('div', {
      key: 'body',
      className: 'fc-dh-scroll',
      children: jsxs('div', {
        className: 'fc-trail-state',
        'data-state': 'filtered',
        children: [
          jsx('span', { className: 'fc-trail-state-ico', children: jsx(Icon, { name: 'filter', size: 16 }) }),
          jsx('h3', { children: 'No decisions match' }),
          jsx('div', { className: 'fc-trail-active', children: chips.map(([k, t]) => jsx('span', { key: k, className: 'fc-chip', children: t })) }),
          jsx('p', { children: `${plural(all.length, 'decision is', 'decisions are')} loaded outside this view.` }),
          jsx('button', { type: 'button', className: 'fc-btn fc-btn--primary', 'data-action': 'clear', onClick: clearAll, children: 'Clear filters' })
        ]
      })
    })
  } else {
    const page = visible.slice(0, shown)
    const moreLocal = visible.length > shown
    const moreServer = !moreLocal && total > loaded.length && limit < DH_MAX_LIMIT
    const days = groupTrailRows(page)
    body = jsxs('div', {
      key: 'body',
      className: 'fc-dh-scroll',
      children: [
        ...days.map(day => jsxs('section', {
          key: day.key,
          className: 'fc-dh-dayblock',
          'aria-label': day.label,
          children: [
            jsxs('div', { key: 'h', className: 'fc-dh-day', role: 'heading', 'aria-level': 3, children: [jsx('span', { children: day.label }), jsx('span', { children: plural(day.count, 'decision', 'decisions') })] }),
            jsx('ol', {
              key: 'l',
              className: 'fc-dh-list',
              'aria-label': 'Decisions, newest first',
              children: day.items.map(item => (item.kind === 'row'
                ? renderRow(item.d)
                : jsx(FoldChip, {
                    key: item.key,
                    fold: item,
                    open: !!folds[item.key] || item.rows.some(r => r.id === reveal),
                    onToggle: () => setFolds(cur => ({ ...cur, [item.key]: !(cur[item.key] || item.rows.some(r => r.id === reveal)) })),
                    children: item.rows.map(renderRow)
                  })))
            })
          ]
        })),
        jsx('div', {
          key: 'more',
          className: 'fc-dh-more',
          children: moreLocal || moreServer
            ? jsx('button', {
                type: 'button',
                className: 'fc-btn',
                'data-action': 'load-more',
                onClick: () => {
                  setShown(shown + 50)
                  if (moreServer) setLimit(Math.min(DH_MAX_LIMIT, limit + 50))
                },
                children: 'Load 50 older'
              })
            : jsx('span', { className: 'fc-m-note', children: `Start of loaded window · server limit ${limit}` })
        })
      ]
    })
  }

  return jsxs('section', {
    className: 'fc-sec fc-trail fc-dh',
    'aria-labelledby': 'fc-trail-h',
    'aria-busy': String(loading),
    'data-state': loading ? 'loading' : isError && !hasData ? 'error' : empty ? 'empty' : hasData && !visible.length ? 'filtered' : 'populated',
    'data-preset': preset,
    children: [
      head,
      auditBanner,
      alert,
      bar,
      body,
      jsx('p', { key: 'live', className: 'fc-trail-sr', 'aria-live': 'polite', children: msg }),
      modal ? jsx(LogDecisionModal, { key: 'modal', ctx, target: modal.target, tenant: tenant || scoped || '', onClose: () => setModal(null), onAppended }) : null
    ]
  })
}

// ---------------------------------------------------------------------------
// Part A control plane (DESIGN.md Part 2): safe Markdown report viewer, Publish PR confirmation,
// repo harness audit drawer, design approval strip and panel / arena Re-evaluate.
// ---------------------------------------------------------------------------

const SHA_SHORT = 12
const FULL_SHA_RE = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/
const MD_LIST_RE = /^\s*([-*+]|\d+[.)])\s+/
const MD_TABLE_SEP_RE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/
const MD_INLINE_RE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\s][^*\n]*\*)|(\[[^\]\n]+\]\([^)\s]+\))/g
const shortSha = s => String(s || '').slice(0, SHA_SHORT)
const fmtKiB = n => `${(Math.max(0, Number(n) || 0) / 1024).toFixed(1)} KiB`

// Only absolute https:// URLs become links; javascript:, data:, relative and file paths stay text.
function safeMarkdownHref(raw) {
  const s = String(raw ?? '').trim()
  if (!/^https:\/\/[^\s"'<>\\`]+$/i.test(s) || mentionsHoldout(s)) return null
  try {
    const u = new URL(s)
    return u.protocol === 'https:' && u.hostname && !u.username && !u.password ? u.href : null
  } catch {
    return null
  }
}

// Inline tokens: text | code | strong | em | link (https only). Never HTML.
function parseInlineMarkdown(text) {
  const s = String(text ?? '')
  const re = new RegExp(MD_INLINE_RE.source, 'g')
  const out = []
  let last = 0
  let m
  while ((m = re.exec(s))) {
    if (m.index > last) out.push({ type: 'text', text: s.slice(last, m.index) })
    const t = m[0]
    if (m[1]) out.push({ type: 'code', text: t.slice(1, -1) })
    else if (m[2]) out.push({ type: 'strong', text: t.slice(2, -2) })
    else if (m[3]) out.push({ type: 'em', text: t.slice(1, -1) })
    else {
      const lm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(t)
      const href = lm ? safeMarkdownHref(lm[2]) : null
      out.push(href ? { type: 'link', text: lm[1], href } : { type: 'text', text: t })
    }
    last = re.lastIndex
  }
  if (last < s.length) out.push({ type: 'text', text: s.slice(last) })
  return out
}

// Block tokens: heading | paragraph | code | quote | list | table | rule.
function parseMarkdownBlocks(src) {
  const lines = String(src ?? '').replace(/\r\n?/g, '\n').split('\n')
  const cells = row => {
    let t = row.trim()
    if (t.startsWith('|')) t = t.slice(1)
    if (t.endsWith('|')) t = t.slice(0, -1)
    return t.split('|').map(c => c.trim())
  }
  const tableAt = i => lines[i].includes('|') && i + 1 < lines.length && lines[i + 1].includes('|') && lines[i + 1].includes('-') && MD_TABLE_SEP_RE.test(lines[i + 1])
  const startsBlock = i => /^\s*(```|~~~|#{1,6}\s|>)/.test(lines[i]) || MD_LIST_RE.test(lines[i]) || tableAt(i)
  const blocks = []
  let i = 0
  while (i < lines.length) {
    const l = lines[i]
    if (!l.trim()) { i++; continue }
    const fence = /^\s*(```|~~~)\s*([A-Za-z0-9_+-]*)/.exec(l)
    if (fence) {
      const buf = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) buf.push(lines[i++])
      i++
      blocks.push({ type: 'code', lang: fence[2] || null, text: buf.join('\n') })
      continue
    }
    const h = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(l)
    if (h) { blocks.push({ type: 'heading', level: Math.min(h[1].length, 4), text: h[2] }); i++; continue }
    if (/^\s*>/.test(l)) {
      const buf = []
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''))
      blocks.push({ type: 'quote', text: buf.join(' ').trim() })
      continue
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { blocks.push({ type: 'rule' }); i++; continue }
    if (MD_LIST_RE.test(l)) {
      const ordered = /^\s*\d/.test(l)
      const items = []
      while (i < lines.length && MD_LIST_RE.test(lines[i]) && /^\s*\d/.test(lines[i]) === ordered) items.push(lines[i++].replace(MD_LIST_RE, ''))
      blocks.push({ type: 'list', ordered, items })
      continue
    }
    if (tableAt(i)) {
      const head = cells(l)
      i += 2
      const rows = []
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(cells(lines[i++]))
      blocks.push({ type: 'table', head, rows })
      continue
    }
    const buf = [l.trim()]
    i++
    while (i < lines.length && lines[i].trim() && !startsBlock(i)) buf.push(lines[i++].trim())
    blocks.push({ type: 'paragraph', text: buf.join(' ') })
  }
  return blocks
}

function renderInline(text, ctx) {
  return parseInlineMarkdown(text).map((t, i) => {
    if (t.type === 'code') return jsx('code', { key: i, children: t.text })
    if (t.type === 'strong') return jsx('strong', { key: i, children: t.text })
    if (t.type === 'em') return jsx('em', { key: i, children: t.text })
    if (t.type === 'link') {
      return jsx('a', {
        key: i,
        href: t.href,
        target: '_blank',
        rel: 'noopener noreferrer',
        onClick: e => {
          e?.preventDefault?.()
          openExternalUrl(ctx, t.href)
        },
        children: t.text
      })
    }
    return t.text
  })
}

// Markdown as plain React elements: text is always a child string, never markup.
function renderMarkdown(src, ctx) {
  return parseMarkdownBlocks(src).map((b, i) => {
    if (b.type === 'heading') return jsx(`h${b.level}`, { key: i, children: renderInline(b.text, ctx) })
    if (b.type === 'code') return jsx('pre', { key: i, 'data-lang': b.lang || undefined, children: jsx('code', { children: b.text }) })
    if (b.type === 'quote') return jsx('blockquote', { key: i, children: jsx('p', { children: renderInline(b.text, ctx) }) })
    if (b.type === 'rule') return jsx('hr', { key: i })
    if (b.type === 'list') return jsx(b.ordered ? 'ol' : 'ul', { key: i, children: b.items.map((it, j) => jsx('li', { key: j, children: renderInline(it, ctx) })) })
    if (b.type === 'table') {
      return jsx('div', {
        key: i,
        className: 'fc-md-table',
        children: jsxs('table', {
          children: [
            jsx('thead', { children: jsx('tr', { children: b.head.map((c, j) => jsx('th', { key: j, children: renderInline(c, ctx) })) }) }),
            jsx('tbody', { children: b.rows.map((r, k) => jsx('tr', { key: k, children: r.map((c, j) => jsx('td', { key: j, children: renderInline(c, ctx) })) })) })
          ]
        })
      })
    }
    return jsx('p', { key: i, children: renderInline(b.text, ctx) })
  })
}

// GET /workstreams/{tenant}/report -> view model. Lines that still mention holdout are withheld client
// side too; the filename is always the bare `investigation.md`, never a path.
function reportView(data) {
  const r = data && typeof data === 'object' && !Array.isArray(data) ? data : {}
  if (r.exists !== true || typeof r.content !== 'string') return { exists: false }
  let redacted = r.redacted === true
  const lines = r.content.split('\n').map(line => {
    if (!mentionsHoldout(line)) return line
    redacted = true
    return '[redacted]'
  })
  const size = Number.isFinite(r.size_bytes) && r.size_bytes >= 0 ? r.size_bytes : utf8Bytes(r.content)
  return {
    exists: true,
    content: lines.join('\n'),
    truncated: r.truncated === true,
    redacted,
    sizeBytes: size,
    shownBytes: utf8Bytes(lines.join('\n')),
    updated: typeof r.updated_at === 'string' ? fmtTrailClock(r.updated_at) : null
  }
}

function ReportModal({ ctx, tenant, label, onClose }) {
  const queryClient = useQueryClient()
  const closeRef = useRef(null)
  const key = ['factonio', 'report', tenant]
  const q = useQuery({
    queryKey: key,
    queryFn: () => ctx.rest(`/workstreams/${encodeURIComponent(tenant)}/report`),
    staleTime: 60000,
    retry: false
  })
  const view = q?.data != null ? reportView(q.data) : null
  let body
  let state
  if (q?.isError && q?.data == null) {
    state = 'error'
    const e = describeSaveError(q.error)
    body = jsx(Banner, {
      tone: 'bad',
      title: e.status ? `Couldn’t load the report (${e.status}).` : 'Couldn’t load the report.',
      children: e.status === 403 ? 'The file is not a regular file, so it was not read.' : e.status ? e.detail : 'The report endpoint could not be reached.',
      action: jsxs('button', { key: 'r', type: 'button', className: 'fc-btn fc-btn--s', 'data-action': 'retry-report', onClick: () => retryQuery(q, queryClient, key), children: [jsx(Icon, { name: 'retry', size: 11 }), 'Retry'] })
    })
  } else if (!view) {
    state = 'loading'
    body = jsxs('div', { className: 'fc-m-skel', 'aria-busy': 'true', children: [jsx('span', { style: { width: '60%' } }), jsx('span', {}), jsx('span', { style: { width: '90%' } }), jsx('span', { style: { width: '70%' } })] })
  } else if (!view.exists) {
    state = 'missing'
    body = jsxs('div', {
      className: 'fc-trail-state',
      'data-state': 'missing',
      children: [jsx('h3', { children: 'Report not written yet' }), jsxs('p', { children: ['The investigation hasn’t produced ', jsx('code', { children: 'investigation.md' }), ' yet. It appears here once the investigate card finishes.'] })]
    })
  } else {
    state = view.truncated ? 'truncated' : 'populated'
    body = [
      jsxs('div', {
        key: 'meta',
        className: 'fc-m-meta',
        children: [
          jsxs('span', { className: 'fc-chip', 'data-field': 'filename', children: [jsx(Icon, { name: 'doc', size: 11 }), 'investigation.md'] }),
          jsx('span', { className: 'fc-m-note', children: [view.updated ? `updated ${view.updated}` : null, view.truncated ? `${fmtKiB(view.shownBytes)} of ${fmtKiB(view.sizeBytes)}` : fmtKiB(view.sizeBytes)].filter(Boolean).join(' · ') })
        ]
      }),
      view.truncated ? jsx(Banner, { key: 'trunc', tone: 'warn', role: 'note', title: 'Truncated at the 256 KiB cap.', children: 'The end of the report isn’t shown.' }) : null,
      view.redacted ? jsx(Banner, { key: 'red', tone: 'info', icon: 'shield', role: 'note', title: 'Redacted.', children: 'Lines the information barrier restricts were withheld before display.' }) : null,
      jsx('article', { key: 'md', className: 'fc-md', 'aria-label': 'Investigation report', children: renderMarkdown(view.content, ctx) })
    ]
  }
  return jsx(ConfirmModal, {
    id: 'fc-report',
    title: 'Investigation report',
    sub: [jsx('code', { key: 'c', children: label }), ' · investigate · read-only'],
    size: 'wide',
    onClose,
    initialFocusRef: closeRef,
    lastRef: closeRef,
    foot: close => [
      jsxs('span', { key: 'n', className: 'fc-m-note', children: [jsx(Icon, { name: 'shield', size: 12 }), 'Read-only. Rendered as plain elements; HTML in the file is shown as text.'] }),
      jsx('span', { key: 's', className: 'fc-spacer' }),
      jsx('button', { key: 'c', type: 'button', className: 'fc-btn', ref: closeRef, 'data-action': 'close-report', onClick: close, children: 'Close' })
    ],
    children: jsx('div', { className: 'fc-m-report', 'data-state': state, children: body })
  })
}

// Local verification of an unpublished branch from the polled /cards lanes. Mirrors
// metrics.local_branch_verification_status; the server re-checks before anything is pushed.
function localVerification(target, wsCards, panels) {
  const sha = low(target?.head_sha)
  const repo = low(target?.repo)
  const lanes = (Array.isArray(wsCards) ? wsCards : []).filter(c => {
    const lt = c?.local_target
    if (!lt || (c.phase !== 'verify-diff' && c.phase !== 'verify-blackbox')) return false
    return lt.branch === target.branch && low(lt.head_sha) === sha && (!repo || !lt.repo || low(lt.repo) === repo)
  })
  if (!lanes.length) return { verified: false, mode: 'none', label: 'not verified', reason: 'No local verification at this head yet.', lanes: [] }
  const standard = lanes.filter(c => !c.panel_id)
  const blockers = standard.filter(c => ['reject', 'warn'].includes(verdictTone(c.verdict)))
  if (blockers.length) return { verified: false, mode: 'standard', label: 'rejected', reason: `${plural(blockers.length, 'verification card', 'verification cards')} rejected or inconclusive at this head.`, lanes }
  const panelIds = new Set(lanes.filter(c => c.panel_id).map(c => String(c.panel_id)))
  if (panelIds.size) {
    const p = (Array.isArray(panels) ? panels : []).find(x => panelIds.has(x.id))
    if (p && p.consensus === 'pass') return { verified: true, mode: 'panel', label: `Panel PASS · ${plural(Array.isArray(p.vendors) ? p.vendors.length : p.lanes.length, 'vendor', 'vendors')}`, reason: '', lanes, panelId: p.id }
    return { verified: false, mode: 'panel', label: `Panel ${p?.consensus || 'pending'}`, reason: 'The verification panel has not reached a pass consensus.', lanes }
  }
  const passed = ph => standard.some(c => c.phase === ph && c.status === 'done' && verdictTone(c.verdict) === 'pass')
  const allDone = standard.every(c => c.status === 'done' && verdictTone(c.verdict) === 'pass')
  if (passed('verify-diff') && passed('verify-blackbox') && allDone) return { verified: true, mode: 'standard', label: 'Diff PASS · Black-Box PASS', reason: '', lanes }
  return { verified: false, mode: 'standard', label: 'verifying', reason: 'Diff and Black-Box checks must both pass at this head.', lanes }
}

// Publish PR is offered only for a non-verify card with an unpublished local target, a known
// owner/repo, a full head SHA and passing local verification at exactly that SHA.
function publishEligibility(card, wsCards, panels) {
  const t = card?.local_target
  if (!t || typeof t !== 'object' || Array.isArray(t)) return { eligible: false, reason: 'no-local-target' }
  if (card.pr) return { eligible: false, reason: 'has-pr' }
  if (card.phase === 'verify-diff' || card.phase === 'verify-blackbox') return { eligible: false, reason: 'verify-card' }
  const branch = barrierStr(t.branch)
  const sha = low(t.head_sha)
  const repo = barrierStr(t.repo)
  if (!branch || !FULL_SHA_RE.test(sha)) return { eligible: false, reason: 'invalid-target' }
  const target = {
    repo: repo && GH_SLUG.test(repo) ? repo : null,
    branch,
    base_branch: barrierStr(t.base_branch),
    head_sha: sha,
    pr_title: barrierStr(t.pr_title),
    pr_body: barrierStr(t.pr_body)
  }
  if (!target.repo) return { eligible: false, reason: 'unknown-repo', target }
  const verification = localVerification(target, wsCards, panels)
  return { eligible: verification.verified, reason: verification.verified ? null : 'unverified', target, verification }
}

function publishDefaults(card, target, verification) {
  return {
    title: target.pr_title || stripTag(card?.title) || target.branch,
    body: target.pr_body || `Verified locally: ${verification?.label || 'local verification passed'} at ${shortSha(target.head_sha)}.`,
    draft: false
  }
}

// POST /publish-pr body: the exact verified target plus the confirmed, edited title / body.
function publishRequestBody(target, form, card) {
  const body = {
    repo: target.repo,
    branch: target.branch,
    expected_head_sha: target.head_sha,
    title: String(form?.title ?? '').trim(),
    draft: !!form?.draft,
    confirmed: true
  }
  const text = String(form?.body ?? '').trim()
  if (text) body.body = text
  if (target.base_branch) body.base_branch = target.base_branch
  if (typeof card?.tenant === 'string' && TENANT_SLUG_RE.test(card.tenant)) body.tenant = card.tenant
  if (typeof card?.id === 'string' && card.id) body.card_id = card.id
  return body
}

const PUBLISH_ERROR_TITLES = { 404: 'Repository not registered', 409: 'Publish refused', 422: 'Request rejected', 502: 'GitHub push failed' }
function publishErrorText(err, sha) {
  if (err?.status === 409) return `Nothing was pushed: ${sha} is no longer the verified head, or its verification changed. Re-verify before publishing.`
  if (err?.status === 502) return 'GitHub push or PR readback failed. Retry is safe; no duplicate PR is created.'
  return err?.status ? err.detail : 'Network error. Nothing was pushed.'
}

// Innermost structured object of a rejected ctx.rest call (FastAPI nests refusals as {detail: {...}}),
// or null when the server sent no JSON object.
function errorDetailObject(err) {
  const e = err && typeof err === 'object' ? err : {}
  let body = e.detail ?? e.body ?? e.data ?? e.response?.data
  if (body == null) body = /(?:^|[^0-9])[1-5][0-9]{2}:\s?([\s\S]*)$/.exec(String(e.message ?? ''))?.[1]
  if (typeof body === 'string') {
    try { body = JSON.parse(body.trim()) } catch { return null }
  }
  for (let i = 0; i < 3 && body?.detail && typeof body.detail === 'object' && !Array.isArray(body.detail); i++) body = body.detail
  return body && typeof body === 'object' && !Array.isArray(body) ? body : null
}

const HEX_SHA = /^[0-9a-f]{7,40}$/i
// 409 drift facts: the observed head (when it differs from the verified one) and the scrubbed server message.
function publishDrift(info, verifiedSha) {
  if (!info) return null
  const expected = HEX_SHA.test(String(info.expected_head_sha || '')) ? info.expected_head_sha : verifiedSha
  const observed = HEX_SHA.test(String(info.head_sha || '')) && String(info.head_sha).toLowerCase() !== String(expected || '').toLowerCase() ? info.head_sha : null
  const message = barrierStr(info.error) || barrierStr(info.message)
  return observed || message ? { expected, observed, message } : null
}

function PublishModal({ ctx, card, target, verification, onClose }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(() => publishDefaults(card, target, verification))
  const [tried, setTried] = useState(false)
  const [state, run] = useAction()
  const [errInfo, setErrInfo] = useState(null)
  const titleRef = useRef(null)
  const goRef = useRef(null)
  const busy = state.status === 'busy'
  const done = state.status === 'ok' ? state.result : null
  const err = state.status === 'error' ? state.error : null
  const titleErr = tried && !String(form.title).trim()
  const set = (k, v) => setForm(prev => ({ ...prev, [k]: v }))
  const submit = async e => {
    e?.preventDefault?.()
    if (busy) return
    setTried(true)
    if (!String(form.title).trim()) return
    setErrInfo(null)
    const res = await run(async () => {
      try {
        return await ctx.rest('/publish-pr', { method: 'POST', body: publishRequestBody(target, form, card) })
      } catch (error) {
        setErrInfo(errorDetailObject(error))
        throw error
      }
    })
    if (res.ok) {
      const n = res.result?.pr_number
      host.notify?.({ kind: 'info', message: n ? `Published PR #${n}` : 'Published the PR' })
      invalidateKeys(queryClient, [['factonio', 'cards'], ['factonio', 'trail']])
    }
  }
  const sha = shortSha(target.head_sha)
  const drift = err?.status === 409 ? publishDrift(errInfo, target.head_sha) : null
  const facts = [
    ['Repository', jsx('code', { children: target.repo })],
    ['Branch', [jsx('code', { key: 'b', children: target.branch }), ' → ', jsx('code', { key: 'base', children: target.base_branch || 'default branch' })]],
    ['Verified head', jsx('span', { className: 'fc-chip', 'data-field': 'head', title: target.head_sha, children: jsx('code', { children: sha }) })],
    ['Local verification', jsxs('span', { className: 'fc-chip', 'data-tone': 'ok', children: [jsx(Icon, { name: 'check', size: 10 }), verification.label] })]
  ]
  if (done) {
    const url = typeof done.url === 'string' && /^https:\/\//i.test(done.url) ? done.url : null
    return jsx(ConfirmModal, {
      id: 'fc-publish',
      title: 'PR published',
      sub: 'Readback confirmed on GitHub.',
      onClose,
      initialFocusRef: titleRef,
      lastRef: titleRef,
      foot: close => [jsx('span', { key: 's', className: 'fc-spacer' }), jsx('button', { key: 'd', type: 'button', className: 'fc-btn fc-btn--primary', ref: titleRef, 'data-action': 'close-modal', onClick: close, children: 'Done' })],
      children: [
        jsx(Banner, { key: 'ok', tone: 'ok', icon: 'check', title: `${target.repo} #${done.pr_number ?? '?'} is open${form.draft ? ' as a draft' : ''}.`, children: form.title }),
        jsxs('dl', {
          key: 'facts',
          className: 'fc-m-facts',
          children: [
            jsx('dt', { children: 'Pull request' }),
            jsx('dd', {
              children: url
                ? jsxs('a', { className: 'fc-chip', 'data-tone': 'ok', href: url, target: '_blank', rel: 'noopener noreferrer', 'data-action': 'open-pr', onClick: e => { e?.preventDefault?.(); openExternalUrl(ctx, url) }, children: [url.replace(/^https:\/\//, ''), jsx(Icon, { key: 'e', name: 'ext', size: 10 })] })
                : `#${done.pr_number ?? '?'}`
            }),
            jsx('dt', { children: 'Head' }),
            jsxs('dd', { children: [jsx('code', { children: sha }), ' (unchanged)'] }),
            jsx('dt', { children: 'Trail' }),
            jsxs('dd', { children: [jsx('code', { children: 'publish_pr' }), ' · actor human'] })
          ]
        })
      ]
    })
  }
  return jsx(ConfirmModal, {
    id: 'fc-publish',
    title: 'Publish PR to GitHub',
    sub: ['Pushes the verified branch to ', jsx('code', { key: 'o', children: 'origin' }), ' and opens a pull request.'],
    busy,
    onClose,
    initialFocusRef: titleRef,
    lastRef: goRef,
    foot: close => [
      jsx('span', { key: 'n', className: 'fc-m-note', 'aria-live': 'polite', children: busy ? 'Publishing… pushing branch and reading the PR back' : '' }),
      jsx('span', { key: 's', className: 'fc-spacer' }),
      jsx('button', { key: 'c', type: 'button', className: 'fc-btn', 'data-action': 'cancel-modal', disabled: busy, onClick: close, children: 'Cancel' }),
      jsxs('button', {
        key: 'go',
        type: 'button',
        className: 'fc-btn fc-btn--primary',
        ref: goRef,
        'data-action': 'publish-go',
        disabled: busy,
        onClick: submit,
        children: [busy ? jsx('span', { key: 'sp', className: 'fc-spin', 'aria-hidden': 'true' }) : jsx(Icon, { key: 'i', name: 'up', size: 12 }), err ? 'Retry publish' : busy ? 'Publishing…' : 'Publish to GitHub']
      })
    ],
    children: jsxs('form', {
      className: 'fc-m-form',
      noValidate: true,
      onSubmit: submit,
      children: [
        err
          ? jsx(Banner, {
              key: 'err',
              tone: 'bad',
              title: err.status ? `${err.status} · ${drift?.observed ? 'Head drifted' : PUBLISH_ERROR_TITLES[err.status] || 'Publish failed'}` : 'Publish failed',
              children: drift
                ? [
                    drift.observed
                      ? jsxs('span', { key: 'h', 'data-field': 'drift', children: ['The branch is now at ', jsx('code', { key: 'o', 'data-field': 'observed-head', title: drift.observed, children: shortSha(drift.observed) }), ', not the verified ', jsx('code', { key: 'v', 'data-field': 'verified-head', title: drift.expected, children: shortSha(drift.expected) }), '. Re-verify before publishing.'] })
                      : publishErrorText(err, sha),
                    ' Your title and body are kept.',
                    drift.message ? jsx('span', { key: 'd', className: 'fc-banner-detail', 'data-field': 'server-detail', children: drift.message }) : null
                  ]
                : `${publishErrorText(err, sha)} Your title and body are kept.`
            })
          : null,
        jsx('dl', { key: 'facts', className: 'fc-m-facts', children: facts.flatMap(([k, v]) => [jsx('dt', { key: `${k}-t`, children: k }), jsx('dd', { key: `${k}-d`, children: v })]) }),
        jsxs('div', {
          key: 'title',
          className: 'fc-field',
          'data-field': 'title',
          children: [
            jsx('label', { htmlFor: 'fc-pp-title', children: 'PR title' }),
            jsx('input', { id: 'fc-pp-title', ref: titleRef, className: 'fc-input', type: 'text', value: form.title, disabled: busy, 'aria-invalid': titleErr ? 'true' : undefined, 'aria-describedby': titleErr ? 'fc-pp-title-e' : undefined, onChange: ev => set('title', ev?.target?.value ?? '') }),
            titleErr ? jsx('span', { className: 'fc-field-err', id: 'fc-pp-title-e', children: 'A title is required.' }) : null
          ]
        }),
        jsxs('div', {
          key: 'body',
          className: 'fc-field',
          'data-field': 'body',
          children: [
            jsxs('label', { htmlFor: 'fc-pp-body', children: ['PR body', jsx('span', { key: 'o', className: 'fc-field-hint', children: ' (optional)' })] }),
            jsx('textarea', { id: 'fc-pp-body', className: 'fc-input', rows: 6, value: form.body, disabled: busy, onChange: ev => set('body', ev?.target?.value ?? '') })
          ]
        }),
        jsxs('label', { key: 'draft', className: 'fc-chk', children: [jsx('input', { type: 'checkbox', 'data-field': 'draft', checked: !!form.draft, disabled: busy, onChange: ev => set('draft', !!ev?.target?.checked) }), 'Open as draft PR'] }),
        jsxs('p', {
          key: 'note',
          className: 'fc-m-shield',
          children: [jsx(Icon, { name: 'shield', size: 12 }), jsxs('span', { children: ['The server re-checks that ', jsx('code', { key: 's', children: sha }), ' is still the verified head. If the branch moved, nothing is pushed.'] })]
        })
      ]
    })
  })
}

// POST /repos/{slug}/harness/audit -> drawer view model.
const AUDIT_CLASS_TONE = { clean: 'ok', doc_drift: 'warn', harness_gap: 'bad', product_regression: 'bad' }
function formatHarnessAudit(res) {
  const r = res && typeof res === 'object' && !Array.isArray(res) ? res : {}
  const text = x => {
    if (typeof x === 'string') return x
    if (x && typeof x === 'object') {
      if (typeof x.name === 'string' && Array.isArray(x.changed_files)) return `${x.name} — ${x.changed_files.filter(f => typeof f === 'string').join(', ') || 'covered files changed'}`
      for (const k of ['message', 'path', 'file', 'name', 'glob', 'hook']) if (typeof x[k] === 'string') return x[k]
      try { return JSON.stringify(x) } catch { return '' }
    }
    return x == null ? '' : String(x)
  }
  const list = v => [...new Set((Array.isArray(v) ? v : []).map(text).filter(s => s && !mentionsHoldout(s)))]
  // Fail closed: an unrecognised payload never reads as clean.
  const severity = ['clean', 'stale', 'missing'].includes(r.severity) ? r.severity : r.drifted === true ? 'stale' : r.drifted === false ? 'clean' : 'unknown'
  const dc = r.drift_classification
  const named = typeof dc === 'string' ? dc : dc && typeof dc === 'object' && typeof dc.classification === 'string' ? dc.classification : null
  const classification = barrierStr(named) || (severity === 'clean' ? 'clean' : 'unknown')
  const gate = r.gate_loosening && typeof r.gate_loosening === 'object' ? r.gate_loosening : {}
  const gateItems = list(gate.findings)
  return {
    severity,
    tone: severity === 'clean' ? 'ok' : severity === 'missing' ? 'bad' : 'warn',
    mode: barrierStr(r.mode) || (r.present === false ? 'absent' : 'unknown'),
    classification,
    classTone: AUDIT_CLASS_TONE[classification] || 'warn',
    driftedFiles: list([...(Array.isArray(r.uncovered_changed_files) ? r.uncovered_changed_files : []), ...(Array.isArray(r.visual_baseline_changes) ? r.visual_baseline_changes : [])]),
    staleInvariants: list(r.stale_invariants),
    gateLoosening: gateItems.length ? gateItems : gate.loosened === true ? ['A required gate was loosened since the base.'] : [],
    manifestErrors: list(r.manifest_errors),
    recommendations: list(r.recommendations),
    head: barrierStr(r.head_commit) ? String(r.head_commit).slice(0, 7) : null
  }
}

function AuditDrawer({ repo, audit, onRun, onClose, id }) {
  const label = repo.repo || repo.slug
  const btns = [
    jsxs('button', { key: 'r', type: 'button', className: 'fc-btn fc-btn--s', 'data-action': 're-audit', disabled: audit.status === 'busy', onClick: onRun, children: [jsx(Icon, { name: 'retry', size: 11 }), 'Re-audit'] }),
    jsx('button', { key: 'c', type: 'button', className: 'fc-btn fc-btn--s', 'data-action': 'close-audit', onClick: onClose, children: 'Close' })
  ]
  let inner
  if (audit.status === 'busy') {
    inner = jsxs('div', {
      className: 'fc-au-h',
      'aria-busy': 'true',
      children: [jsx('span', { className: 'fc-spin', 'aria-hidden': 'true' }), jsxs('span', { children: ['Auditing ', jsx('code', { children: '.factonio/' }), ' against ', jsx('code', { children: repo.default_branch || 'HEAD' }), ' — read-only, no repo files touched…'] })]
    })
  } else if (audit.status === 'error') {
    const e = audit.error || {}
    inner = [
      jsx(Banner, {
        key: 'e',
        tone: 'bad',
        title: e.status === 409 ? '409 · Local checkout missing.' : e.status ? `Audit failed (${e.status}).` : 'Audit failed.',
        children: e.status === 409 ? [jsx('code', { key: 'c', children: label }), ' has no checkout on this machine, so the harness can’t be audited. Clone it or update the registered path, then re-audit.'] : `${e.status ? e.detail : 'The server could not be reached.'} Nothing was changed.`
      }),
      jsxs('div', { key: 'h', className: 'fc-au-h', children: [jsx('span', { className: 'fc-spacer' }), ...btns] })
    ]
  } else {
    const v = formatHarnessAudit(audit.result)
    const card = (key, title, items, full) => jsxs('section', {
      key,
      'data-section': key,
      className: full ? 'fc-au-full' : undefined,
      children: [
        jsx('h4', { children: full ? title : `${title} · ${items.length}` }),
        items.length ? jsx('ul', { children: items.map((x, i) => jsx('li', { key: i, children: x })) }) : jsx('p', { className: 'fc-au-none', children: 'None' })
      ]
    })
    inner = [
      jsxs('div', {
        key: 'h',
        className: 'fc-au-h',
        children: [
          jsxs('span', { className: 'fc-chip', 'data-tone': v.tone, 'data-field': 'severity', children: [jsx(Icon, { name: v.severity === 'clean' ? 'check' : 'attention', size: 10 }), v.severity] }),
          jsxs('span', { className: 'fc-chip', 'data-field': 'mode', children: ['mode ', jsx('code', { children: v.mode })] }),
          jsxs('span', { className: 'fc-chip', 'data-tone': v.classification === 'clean' ? undefined : v.classTone, 'data-field': 'drift', children: ['drift ', jsx('code', { children: v.classification })] }),
          jsxs('span', { className: 'fc-m-note', children: ['audited just now · logged ', jsx('code', { children: 'harness_audit' }), ' to trail'] }),
          jsx('span', { className: 'fc-spacer' }),
          ...btns
        ]
      }),
      v.severity === 'clean' && !v.recommendations.length
        ? jsxs('p', { key: 'ok', className: 'fc-m-note', children: [jsx(Icon, { name: 'check', size: 11 }), v.head ? ['feature_map, hooks and invariants match the tree at ', jsx('code', { key: 'h', children: v.head }), '.'] : 'feature_map, hooks and invariants match the tree.'] })
        : jsxs('div', {
            key: 'grid',
            className: 'fc-au-grid',
            children: [
              card('drifted', 'Drifted files', v.driftedFiles),
              card('stale', 'Stale invariants', v.staleInvariants),
              card('gate', 'Gate loosening', v.gateLoosening),
              card('manifest', 'Manifest errors', v.manifestErrors),
              card('recs', 'Recommendations', v.recommendations, true)
            ]
          })
    ]
  }
  return jsx('tr', {
    className: 'fc-au-row',
    'data-slug': repo.slug,
    children: jsx('td', {
      colSpan: 9,
      children: jsx('div', { className: 'fc-au', id, role: 'region', 'aria-label': `Harness audit result for ${label}`, 'data-status': audit.status, children: inner })
    })
  })
}

// --- sidebar actions ---------------------------------------------------------

const APPROVE_DESIGN_CODES = new Set(['approve_design', 'design_approved'])
const REVISE_DESIGN_CODES = new Set(['request_design_revision', 'design_revision_requested'])

// 'awaiting' | 'approved' | 'revising' | null for a finished design card. The newest active approval /
// revision row for the card (or an unscoped design row in its workstream) decides; once a newer
// Implementation card exists the design is taken as approved and no strip is shown.
function designApprovalState(card, wsCards, trailRows) {
  if (!card || card.phase !== 'design' || card.arena_role === 'candidate') return null
  if (card.status !== 'done' && card.status !== 'review') return null
  const rows = (Array.isArray(trailRows) ? trailRows : []).filter(d => d && !d.is_superseded &&
    (APPROVE_DESIGN_CODES.has(low(d.decision)) || REVISE_DESIGN_CODES.has(low(d.decision))) &&
    (d.card_id === card.id || (!d.card_id && low(d.phase) === 'design')))
  const newest = rows.sort((a, b) => (trailMs(b.ts) ?? 0) - (trailMs(a.ts) ?? 0))[0]
  if (newest) return APPROVE_DESIGN_CODES.has(low(newest.decision)) ? 'approved' : 'revising'
  if ((Array.isArray(wsCards) ? wsCards : []).some(c => c && c.phase === 'implement' && (c.created_at || 0) >= (card.created_at || 0) && c.id !== card.id)) return null
  return 'awaiting'
}

function DesignApproval({ ctx, card, state: initial }) {
  const queryClient = useQueryClient()
  const [kind, setKind] = useState(null)
  const [note, setNote] = useState('')
  const [action, run, setAction] = useAction()
  const busy = action.status === 'busy'
  const recorded = action.status === 'ok' ? action.result : null
  const state = recorded ? (recorded.kind === 'approve' ? 'approved' : 'revising') : initial
  const approve = kind === 'approve'
  const code = approve ? 'approve_design' : 'request_design_revision'
  const send = async () => {
    if (busy || (!approve && !note.trim())) return
    const body = {
      decision: code,
      phase: 'design',
      why: note.trim() || 'Design approved from the Factonio sidebar.',
      confirmed: true
    }
    if (typeof card.tenant === 'string' && TENANT_SLUG_RE.test(card.tenant)) body.tenant = card.tenant
    if (card.id) body.card_id = card.id
    const res = await run(async () => ({ kind, row: (await ctx.rest('/trail/decisions', { method: 'POST', body }))?.decision || null }))
    if (res.ok) {
      host.notify?.({ kind: 'info', message: approve ? 'Design approved' : 'Revision requested' })
      invalidateKeys(queryClient, [['factonio', 'trail']])
    }
  }
  const pill = jsx('span', {
    className: 'fc-sb-tag',
    'data-tone': state === 'awaiting' ? 'warn' : state === 'approved' ? 'pass' : 'info',
    'data-field': 'design-state',
    children: state === 'awaiting' ? 'awaiting approval' : state
  })
  let ctl = null
  if (recorded) {
    const id = recorded.row?.id
    ctl = jsx(Banner, {
      tone: recorded.kind === 'approve' ? 'ok' : 'info',
      icon: 'check',
      title: recorded.kind === 'approve' ? 'Design approved' : 'Revision requested',
      children: ['by you · just now', id ? [' · ', jsx('code', { key: 'c', children: id }), ' appended to trail.db'] : null]
    })
  } else if (state !== 'awaiting') {
    ctl = null
  } else if (kind) {
    const err = action.status === 'error' ? action.error : null
    ctl = jsxs('div', {
      className: 'fc-sb-confirm',
      role: 'group',
      'aria-label': approve ? 'Confirm design approval' : 'Request design revision',
      children: [
        err ? jsx(Banner, { key: 'e', tone: 'bad', title: err.status ? `Not recorded (${err.status}).` : 'Not recorded.', children: 'Your note is kept.' }) : null,
        jsxs('label', {
          key: 'l',
          children: [
            approve ? 'Note (optional)' : 'What needs to change? (required)',
            jsx('textarea', {
              className: 'fc-input',
              rows: 2,
              'data-field': 'design-note',
              value: note,
              disabled: busy,
              placeholder: approve ? 'Anything the implementer should know' : 'e.g. Attention banner should collapse to one line when clean',
              onChange: ev => setNote(ev?.target?.value ?? '')
            })
          ]
        }),
        jsxs('div', { key: 'n', className: 'fc-sb-confirm-note', children: [jsx(Icon, { name: 'person', size: 11 }), jsxs('span', { children: ['Appends an immutable human decision · ', jsx('code', { children: code })] })] }),
        jsxs('div', {
          key: 'a',
          className: 'fc-sb-confirm-a',
          children: [
            jsxs('button', { type: 'button', className: 'fc-sb-btn fc-sb-btn--primary', 'data-action': 'design-confirm', disabled: busy || (!approve && !note.trim()), onClick: send, children: [busy ? jsx('span', { key: 's', className: 'fc-spin', 'aria-hidden': 'true' }) : null, approve ? 'Confirm approval' : 'Send revision request'] }),
            jsx('button', { type: 'button', className: 'fc-sb-btn', 'data-action': 'design-cancel', disabled: busy, onClick: () => { setKind(null); setAction({ status: 'idle' }) }, children: 'Cancel' })
          ]
        })
      ]
    })
  } else {
    ctl = jsxs('div', {
      className: 'fc-sb-appr-a',
      children: [
        jsxs('button', { type: 'button', className: 'fc-sb-btn fc-sb-btn--primary', 'data-action': 'design-approve', onClick: () => setKind('approve'), children: [jsx(Icon, { name: 'check', size: 11 }), 'Approve Design'] }),
        jsx('button', { type: 'button', className: 'fc-sb-btn', 'data-action': 'design-revise', onClick: () => setKind('revise'), children: 'Request Revision' })
      ]
    })
  }
  return jsxs('div', {
    className: 'fc-sb-appr',
    'data-state': state,
    onClick: e => e?.stopPropagation?.(),
    children: [jsx('div', { key: 'p', className: 'fc-sb-appr-top', children: pill }), ctl ? jsx('div', { key: 'c', children: ctl }) : null]
  })
}

function ReevalButton({ label, state, onRun }) {
  const busy = state.status === 'busy'
  return jsxs('button', {
    type: 'button',
    className: 'fc-sb-btn fc-sb-reeval',
    'data-action': 're-evaluate',
    'aria-label': `Re-evaluate ${label}`,
    title: `Re-evaluate ${label}`,
    'aria-busy': busy ? 'true' : undefined,
    disabled: busy,
    onClick: e => {
      e?.stopPropagation?.()
      return onRun()
    },
    children: [busy ? jsx('span', { key: 's', className: 'fc-spin', 'aria-hidden': 'true' }) : jsx(Icon, { key: 'i', name: 'retry', size: 11 }), jsx('span', { key: 'l', className: 'fc-sb-reeval-l', children: busy ? 'Evaluating' : 'Re-evaluate' })]
  })
}

function ReevalMessage({ state, onRun, what }) {
  if (state.status === 'ok') return jsxs('div', { className: 'fc-sb-strip-msg', 'data-tone': 'ok', role: 'status', children: [jsx(Icon, { name: 'check', size: 10 }), 'Updated just now'] })
  if (state.status !== 'error') return null
  const e = state.error || {}
  return jsxs('div', {
    className: 'fc-sb-strip-msg',
    'data-tone': 'bad',
    role: 'alert',
    children: [
      jsx(Icon, { name: 'attention', size: 10 }),
      jsx('span', { children: `Evaluate failed (${e.status ? `${e.status}: ${e.status === 404 ? `${what} not found` : e.detail}` : 'network error'}).` }),
      jsx('button', { type: 'button', className: 'fc-sb-linkbtn', 'data-action': 're-evaluate-retry', onClick: e2 => { e2?.stopPropagation?.(); return onRun() }, children: 'Retry' })
    ]
  })
}

// --- sidebar --------------------------------------------------------------

// One `{ status, byTenant, byCard, retry }` view of the shared trail query.
function sidebarTrail(query, queryClient) {
  const data = query?.data
  const isError = !!query?.isError
  const retry = () => retryQuery(query, queryClient, TRAIL_QUERY_KEY)
  if (data == null) return { status: isError ? 'error' : 'loading', byTenant: new Map(), byCard: new Map(), retry }
  const all = trailDecisions(data)
  return { status: 'ready', stale: isError, byTenant: groupBy(all, d => d.tenant || ''), byCard: groupBy(all.filter(d => d.card_id), d => d.card_id), retry }
}

function TrailItem({ d, why, hideCard }) {
  const tone = resultTone(d.result)
  const sup = !!d.is_superseded
  const pills = evidencePills(d, null).filter(p => !(hideCard && p.key === 'card_id'))
  const max = 3
  const chip = p => jsx('span', {
    key: p.key,
    className: 'fc-sb-chip',
    'data-ev': p.key,
    'data-tone': p.tone || undefined,
    title: p.title,
    children: p.label ? `${p.label} ${p.text}` : p.text
  })
  const chips = [
    ...pills.slice(0, max).map(chip),
    pills.length > max
      ? jsx('span', { key: 'more', className: 'fc-sb-chip', 'data-ev': 'more', title: pills.slice(max).map(p => p.title).join(', '), children: `+${pills.length - max}` })
      : null
  ]
  return jsxs('li', {
    className: 'fc-sb-trail-item',
    'data-id': d.id,
    'data-superseded': String(sup),
    children: [
      jsxs('div', {
        className: 'fc-sb-trail-top',
        children: [
          jsx('span', { className: 'fc-sb-tag', 'data-phase': d.phase || '', children: d.phase || '—' }),
          jsx('span', { className: 'fc-sb-tag', 'data-tone': TRAIL_SB_TONE[tone] || undefined, 'data-result': d.result || '', children: d.result || '—' }),
          sup
            ? jsx('span', { className: 'fc-sb-trail-sup', title: `Superseded by ${(d.superseded_by || []).join(', ')}`, children: 'superseded' })
            : null,
          jsx('time', { className: 'fc-sb-trail-time', dateTime: d.ts, title: d.ts, children: fmtTrailClock(d.ts) })
        ]
      }),
      jsx('p', { className: 'fc-sb-trail-title', title: d.decision, children: humanDecision(d.decision) }),
      why && d.why ? jsx('p', { className: 'fc-sb-trail-why', children: d.why }) : null,
      pills.length ? jsx('div', { className: 'fc-sb-trail-ev', children: chips }) : null
    ]
  })
}

function TrailStrip({ tenant, trail, open, onToggle }) {
  if (!trail) return null
  if (trail.status === 'loading') {
    return jsx('div', {
      className: 'fc-sb-trail',
      children: jsxs('button', {
        type: 'button',
        className: 'fc-sb-trail-toggle',
        'data-action': 'toggle-trail',
        disabled: true,
        'aria-busy': 'true',
        children: [jsx(Icon, { name: 'trail' }), jsx('span', { children: 'Loading decisions…' })]
      })
    })
  }
  if (trail.status === 'error') {
    return jsx('div', {
      className: 'fc-sb-trail',
      children: jsxs('button', {
        type: 'button',
        className: 'fc-sb-trail-toggle',
        'data-action': 'retry-trail',
        title: 'Retry loading decisions',
        onClick: e => {
          e?.stopPropagation?.()
          trail.retry()
        },
        children: [
          jsx('span', { className: 'fc-sb-tone-attention', children: jsx(Icon, { name: 'attention' }) }),
          jsx('span', { style: { flex: 1, minWidth: 0 }, children: 'Decisions unavailable' }),
          jsx('span', { className: 'fc-sb-tone-running', children: 'Retry' })
        ]
      })
    })
  }
  const ds = trail.byTenant.get(tenant || '') || []
  if (!ds.length) return null
  const listId = `fc-sb-trail-${domSafe(tenant || '__ungrouped')}`
  const bad = ds.filter(d => resultTone(d.result) === 'bad' && !d.is_superseded)
  const uniq = [...new Set(ds.map(d => d.phase).filter(Boolean))]
  const phases = uniq.length > 3 ? `${uniq.slice(0, 3).join(' · ')} · +${uniq.length - 3}` : uniq.join(' · ')
  const cap = 5
  return jsxs('div', {
    className: 'fc-sb-trail',
    'data-tenant': tenant || '',
    children: [
      jsxs('button', {
        key: 'toggle',
        type: 'button',
        className: 'fc-sb-trail-toggle',
        'data-action': 'toggle-trail',
        'aria-expanded': String(open),
        'aria-controls': listId,
        onClick: onToggle,
        children: [
          jsx(Icon, { name: 'chev', className: 'fc-sb-chev' }),
          jsx(Icon, { name: 'trail' }),
          jsxs('span', { children: [jsx('span', { className: 'fc-sb-mono', children: String(ds.length) }), ds.length === 1 ? ' decision' : ' decisions'] }),
          jsx('span', { className: 'fc-sb-trail-phases', children: phases }),
          bad.length
            ? jsxs('span', {
                className: 'fc-sb-trail-flag',
                title: `${plural(bad.length, 'active decision', 'active decisions')} need attention: ${bad.map(d => d.result).join(', ')}`,
                children: [jsx(Icon, { name: 'attention', size: 10 }), String(bad.length)]
              })
            : null
        ]
      }),
      open
        ? jsxs('ul', {
            key: 'list',
            className: 'fc-sb-trail-list',
            id: listId,
            'aria-label': 'Decisions for this workstream, newest first',
            children: [
              ...ds.slice(0, cap).map(d => jsx(TrailItem, { key: d.id, d })),
              jsxs('li', {
                key: 'more',
                className: 'fc-sb-trail-more',
                children: [
                  jsx('span', { children: ds.length > cap ? `+${ds.length - cap} older` : 'Full ledger' }),
                  jsx('button', {
                    type: 'button',
                    'data-action': 'open-trail',
                    onClick: () => host.navigate(`${ROUTE_PATH}?tenant=${encodeURIComponent(tenant || '')}`),
                    children: 'Open in Decision Trail ›'
                  })
                ]
              })
            ]
          })
        : null
    ]
  })
}

function CardDecisions({ card, trail }) {
  const ds = trail?.status === 'ready' ? trail.byCard.get(card.id) || [] : []
  let value
  if (!trail || trail.status === 'loading') value = jsx('span', { className: 'fc-sb-trail-none', children: 'Loading…' })
  else if (trail.status === 'error') value = jsx('span', { className: 'fc-sb-trail-none', children: 'Unavailable' })
  else if (!ds.length) value = jsx('span', { className: 'fc-sb-trail-none', children: 'None recorded' })
  else {
    value = jsx('ul', {
      className: 'fc-sb-trail-list',
      'aria-label': 'Decisions for this card, newest first',
      children: ds.map(d => jsx(TrailItem, { key: d.id, d, why: true, hideCard: true }))
    })
  }
  return [
    jsxs('dt', {
      key: 'decisions-t',
      className: 'fc-sb-full fc-sb-trail-dt',
      children: ['Decisions', trail?.status === 'ready' ? jsx('span', { key: 'n', className: 'fc-sb-mono', children: ` · ${ds.length}` }) : null]
    }),
    jsx('dd', { key: 'decisions-d', className: 'fc-sb-full fc-sb-trail-dd', 'data-field': 'decisions', children: value })
  ]
}

// ---------------------------------------------------------------------------
// Verification Panels KPI, verify_panel / rigor status, and the /metrics/history trend strip.
// ---------------------------------------------------------------------------

function VerificationPanelsKpi({ m }) {
  const head = jsx('div', { className: 'fc-k', children: 'Verification Panels' })
  if (!m) {
    return jsxs('div', {
      className: 'fc-card fc-kpi-vp',
      children: [head, jsx('div', { className: 'fc-v', children: '—' }), jsx('div', { className: 'fc-hint', children: 'Loading' })]
    })
  }
  const vp = m.verification_panels && typeof m.verification_panels === 'object' ? m.verification_panels : {}
  const n = k => (Number.isFinite(vp[k]) ? vp[k] : 0)
  const total = n('total')
  if (!total) {
    return jsxs('div', {
      className: 'fc-card fc-kpi-vp',
      'data-state': 'empty',
      children: [head, jsx('div', { className: 'fc-v', children: '0' }), jsx('div', { className: 'fc-hint', children: 'No panels yet · repos with rigor high run one' })]
    })
  }
  const dis = n('disagreements')
  const parts = [['ok', 'pass', 'pass'], ['bad', 'reject', 'reject'], ['warn', 'inconclusive', 'inconcl.'], ['pending', 'pending', 'pending']]
  return jsxs('div', {
    className: 'fc-card fc-kpi-vp',
    'data-flag': dis ? 'disagree' : undefined,
    'data-state': 'populated',
    children: [
      head,
      jsxs('div', {
        className: 'fc-kpi-row',
        children: [
          jsxs('div', { className: 'fc-v', children: [String(total), jsx('span', { className: 'fc-kpi-unit', children: total === 1 ? 'panel' : 'panels' })] }),
          dis
            ? jsxs('span', { className: 'fc-kpi-flag', children: [jsx(Icon, { name: 'split', size: 11 }), plural(dis, 'disagreement', 'disagreements')] })
            : null
        ]
      }),
      jsx('div', {
        className: 'fc-kpi-bar',
        role: 'img',
        'aria-label': `${n('pass')} pass, ${n('reject')} reject, ${n('inconclusive')} inconclusive, ${n('pending')} pending`,
        children: parts.filter(([, k]) => n(k) > 0).map(([tone, k]) => jsx('span', { key: k, 'data-tone': tone, style: { flex: n(k) } }))
      }),
      jsx('div', {
        className: 'fc-kpi-break',
        children: parts.map(([tone, k, label]) => jsxs('span', {
          key: k,
          'data-tone': tone === 'pending' ? undefined : tone,
          children: [jsx('b', { children: String(n(k)) }), ` ${label}`]
        }))
      })
    ]
  })
}

// GET /models `verify_panel` (config.validate_verify_panel) + `default_rigor`.
function VerifyPanelStatus({ query }) {
  const title = jsx('span', { className: 'fc-vp-title', children: 'Verification panel' })
  if (query?.isError && query?.data == null) return null
  if (query?.data == null) {
    return jsxs('div', {
      className: 'fc-card fc-vp',
      'aria-busy': 'true',
      'data-state': 'loading',
      children: [jsx('div', { className: 'fc-vp-bar', children: title }), jsx('span', { className: 'fc-vp-skel' })]
    })
  }
  const p = query.data.verify_panel
  if (!p || typeof p !== 'object') return null
  const entries = Array.isArray(p.entries) ? p.entries : []
  const dups = Array.isArray(p.duplicate_vendors) ? p.duplicate_vendors : []
  const advisories = Array.isArray(p.advisories) ? p.advisories : []
  const rigor = query.data.default_rigor ? String(query.data.default_rigor) : null
  const state = p.valid ? 'ready' : entries.length ? 'incomplete' : 'unconfigured'
  const ready = {
    ready: jsxs('span', { className: 'fc-ready', 'data-tone': 'ok', children: [jsx(Icon, { name: 'check', size: 11 }), `Ready · ${plural(p.distinct_vendor_count ?? 0, 'vendor', 'vendors')}`] }),
    incomplete: jsxs('span', { className: 'fc-ready', 'data-tone': 'warn', children: [jsx(Icon, { name: 'attention', size: 11 }), 'Incomplete'] }),
    unconfigured: jsxs('span', { className: 'fc-ready', 'data-tone': 'muted', children: [jsx(Icon, { name: 'dot', size: 11 }), 'Unconfigured'] })
  }[state]
  const members = entries.length
    ? jsx('ul', {
        className: 'fc-vp-members',
        'aria-label': 'Panel members',
        children: entries.map((e, i) => {
          const vendor = e?.vendor ? String(e.vendor) : null
          return jsxs('li', {
            key: `${i}:${e?.label}`,
            className: 'fc-vp-member',
            children: [
              jsx('span', { className: 'fc-vp-idx', children: String((Number.isInteger(e?.index) ? e.index : i) + 1) }),
              jsx('span', { className: 'fc-mono', title: String(e?.label ?? ''), children: String(e?.label ?? '—') }),
              jsx('span', { className: 'fc-vendor', 'data-dup': String(!vendor || dups.includes(vendor)), children: vendor || 'unknown' })
            ]
          })
        })
      })
    : jsxs('p', { className: 'fc-vp-empty', children: ['No panel members. Add ≥2 models from different vendors under ', jsx('span', { className: 'fc-mono', children: 'settings.verify_panel' }), '.'] })
  const warn = !p.valid && p.warning
    ? jsxs('div', {
        className: 'fc-vp-warn',
        role: 'status',
        children: [
          jsx(Icon, { name: 'attention', size: 13 }),
          jsxs('div', {
            children: [
              String(p.warning),
              advisories.length ? jsx('ul', { children: advisories.map(a => jsx('li', { key: a, children: String(a) })) }) : null
            ]
          })
        ]
      })
    : null
  return jsxs('div', {
    className: 'fc-card fc-vp',
    'aria-label': 'Verification panel and rigor status',
    'data-state': state,
    children: [
      jsxs('div', {
        className: 'fc-vp-bar',
        children: [
          title,
          jsx('span', { className: 'fc-vp-lbl', children: 'Default rigor' }),
          jsx('span', { className: 'fc-tag fc-rigor', 'data-rigor': rigor || '', children: rigor || 'invalid' }),
          ready,
          p.source && p.source !== 'unset' ? jsx('span', { className: 'fc-vp-src', title: 'Config source', children: String(p.source) }) : null
        ]
      }),
      members,
      warn
    ]
  })
}

// Inline-SVG sparkline geometry. Pure helpers, exported for tests.
const SPARK = { W: 240, H: 56, PX: 4, PT: 6, PB: 6 }
const finiteOrNull = v => (typeof v === 'number' && Number.isFinite(v) ? v : null)

function sparkX(i, n) {
  return n === 1 ? SPARK.W / 2 : SPARK.PX + (i * (SPARK.W - 2 * SPARK.PX)) / (n - 1)
}
function sparkY(v, lo, hi) {
  return SPARK.PT + (1 - (v - lo) / (hi - lo || 1)) * (SPARK.H - SPARK.PT - SPARK.PB)
}
// [[x, y] | null] per snapshot; null where the value is missing.
function sparkPoints(values, lo, hi) {
  const n = values.length
  return values.map((raw, i) => {
    const v = finiteOrNull(raw)
    return v == null ? null : [Number(sparkX(i, n).toFixed(1)), Number(sparkY(v, lo, hi).toFixed(1))]
  })
}
// "M x,y L x,y …"; a missing value starts a new segment instead of bridging the gap.
function sparkPath(points) {
  let d = ''
  let pen = false
  for (const p of points) {
    if (!p) { pen = false; continue }
    d += `${d ? ' ' : ''}${pen ? 'L' : 'M'}${p[0]},${p[1]}`
    pen = true
  }
  return d
}

const fmtCount = v => (finiteOrNull(v) == null ? '—' : String(v))
const fmtShortDate = ts => {
  const ms = trailMs(ts)
  return ms == null ? '—' : new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric' })
}
const ptsDelta = d => `${Math.round(Math.abs(d) * 100)} pts`
const passDomain = vals => (vals.some(v => v != null && v < 0.6) ? [0, 1] : [0.6, 1])
const maxOf = vals => Math.max(0, ...vals.filter(v => v != null))

const TREND_METRICS = [
  {
    metric: 'pass',
    title: 'Verification Pass Rate',
    a: s => finiteOrNull(s.diff_pass_rate),
    b: s => finiteOrNull(s.blackbox_pass_rate),
    domains: (as, bs) => { const d = passDomain([...as, ...bs]); return [d, d] },
    upGood: true,
    fmt: fmtPct,
    delta: ptsDelta,
    flat: d => Math.round(d * 100) === 0,
    sub: s => `BB ${fmtPct(finiteOrNull(s.blackbox_pass_rate))}`,
    legend: ['Diff check', 'Black-box'],
    read: s => [['Diff', fmtPct(finiteOrNull(s.diff_pass_rate))], ['BB', fmtPct(finiteOrNull(s.blackbox_pass_rate))]]
  },
  {
    metric: 'escape',
    title: 'Escape Rate',
    a: s => finiteOrNull(s.escape_rate),
    domains: as => [[0, Math.max(0.2, maxOf(as))]],
    upGood: false,
    area: true,
    fmt: fmtPct,
    delta: ptsDelta,
    flat: d => Math.round(d * 100) === 0,
    read: s => [['Escape', fmtPct(finiteOrNull(s.escape_rate))]]
  },
  {
    metric: 'throughput',
    title: 'Workstreams / Throughput',
    a: s => finiteOrNull(s.workstreams_total),
    b: s => finiteOrNull(s.cards_done),
    domains: (as, bs) => [[0, maxOf(as) * 1.15 || 1], [0, maxOf(bs) * 1.15 || 1]],
    upGood: true,
    fmt: fmtCount,
    delta: d => String(Math.abs(d)),
    flat: d => d === 0,
    sub: s => `${fmtCount(finiteOrNull(s.cards_done))} cards done`,
    legend: ['Workstreams', 'Cards done'],
    read: s => [['WS', fmtCount(finiteOrNull(s.workstreams_total))], ['Cards', fmtCount(finiteOrNull(s.cards_done))]]
  }
]

function SparkCard({ cfg, snaps }) {
  const [hover, setHover] = useState(null)
  const n = snaps.length
  const as = snaps.map(cfg.a)
  const bs = cfg.b ? snaps.map(cfg.b) : null
  const [domA, domB] = cfg.domains(as, bs || [])
  const p1 = sparkPoints(as, domA[0], domA[1])
  const p2 = bs ? sparkPoints(bs, (domB || domA)[0], (domB || domA)[1]) : null
  const last = snaps[n - 1]
  const finiteA = as.filter(v => v != null)
  const color = cfg.metric === 'escape' ? 'var(--fc-warn)' : 'var(--ui-accent)'
  const base = SPARK.H - SPARK.PB

  let tone = 'flat'
  let deltaText = n === 1 ? 'first snapshot' : '—'
  let arrow = null
  if (n > 1 && finiteA.length >= 2) {
    const d = finiteA[finiteA.length - 1] - finiteA[0]
    if (cfg.flat(d)) deltaText = 'no change'
    else {
      tone = (d > 0) === cfg.upGood ? 'ok' : 'bad'
      deltaText = `${d > 0 ? '+' : '−'}${cfg.delta(d)}`
      arrow = jsx(Icon, { name: d > 0 ? 'up' : 'down', size: 11 })
    }
  }

  const drawn = p1.filter(Boolean)
  let body
  if (n === 1) {
    const y = p1[0] ? p1[0][1] : base
    body = [
      jsx('line', { key: 'base', className: 'fc-spark-base', x1: SPARK.PX, y1: y, x2: SPARK.W - SPARK.PX, y2: y, strokeDasharray: '2 3' }),
      p1[0] ? jsx('circle', { key: 'end', className: 'fc-spark-end', cx: p1[0][0], cy: y, r: 3.5, style: { color } }) : null
    ]
  } else {
    const lastPt = drawn[drawn.length - 1]
    const idx = hover == null ? null : Math.max(0, Math.min(n - 1, hover))
    const hot = idx == null ? null : p1[idx]
    const hot2 = idx == null || !p2 ? null : p2[idx]
    const cursorX = idx == null ? 0 : Number(sparkX(idx, n).toFixed(1))
    body = [
      jsx('line', { key: 'base', className: 'fc-spark-base', x1: SPARK.PX, y1: base, x2: SPARK.W - SPARK.PX, y2: base }),
      cfg.area && drawn.length >= 2
        ? jsx('path', { key: 'area', className: 'fc-spark-area', d: `${sparkPath(p1)} L${lastPt[0]},${base} L${drawn[0][0]},${base} Z` })
        : null,
      p2 ? jsx('path', { key: 'l2', className: 'fc-spark-line fc-spark-line--2', d: sparkPath(p2) }) : null,
      jsx('path', { key: 'l1', className: 'fc-spark-line', d: sparkPath(p1) }),
      lastPt ? jsx('circle', { key: 'end', className: 'fc-spark-end', cx: lastPt[0], cy: lastPt[1], r: 2.75, style: { color } }) : null,
      jsxs('g', {
        key: 'hover',
        className: 'fc-spark-hover',
        visibility: idx == null ? 'hidden' : 'visible',
        children: [
          jsx('line', { className: 'fc-spark-cursor', x1: cursorX, y1: SPARK.PT - 2, x2: cursorX, y2: base }),
          hot ? jsx('circle', { key: 'h1', className: 'fc-spark-hot', cx: hot[0], cy: hot[1], r: 3.25, style: { color } }) : null,
          hot2 ? jsx('circle', { key: 'h2', className: 'fc-spark-hot', cx: hot2[0], cy: hot2[1], r: 2.75, style: { color: 'var(--ui-text-secondary)' } }) : null
        ]
      })
    ]
  }

  const readIdx = hover == null ? n - 1 : Math.max(0, Math.min(n - 1, hover))
  const r = snaps[readIdx]
  const label = `${cfg.title}: ${plural(n, 'snapshot', 'snapshots')}, latest ${cfg.fmt(cfg.a(last))}. Use left and right arrow keys to read each snapshot.`
  const keys = { ArrowLeft: -1, ArrowRight: 1 }
  return jsxs('div', {
    className: 'fc-card fc-spark',
    'data-metric': cfg.metric,
    children: [
      jsx('div', { className: 'fc-k', children: cfg.title }),
      jsxs('div', {
        className: 'fc-spark-top',
        children: [
          jsxs('div', { className: 'fc-spark-v', children: [cfg.fmt(cfg.a(last)), cfg.sub ? jsx('small', { children: cfg.sub(last) }) : null] }),
          jsxs('span', { className: 'fc-spark-delta', 'data-tone': tone, title: 'Change vs first snapshot in window', children: [arrow, deltaText] })
        ]
      }),
      jsx('div', {
        className: 'fc-spark-plot',
        tabIndex: 0,
        role: 'group',
        'aria-label': label,
        onPointerMove: e => {
          if (n < 2) return
          const box = e?.currentTarget?.getBoundingClientRect?.()
          if (!box || !box.width) return
          const vx = ((e.clientX - box.left) / box.width) * SPARK.W
          setHover(Math.max(0, Math.min(n - 1, Math.round(((vx - SPARK.PX) / (SPARK.W - 2 * SPARK.PX)) * (n - 1)))))
        },
        onPointerLeave: e => {
          if (globalThis.document?.activeElement !== e?.currentTarget) setHover(null)
        },
        onFocus: () => setHover(cur => (cur == null ? n - 1 : cur)),
        onBlur: () => setHover(null),
        onKeyDown: e => {
          const key = e?.key
          let next = null
          if (key in keys) next = (hover == null ? n - 1 : hover) + keys[key]
          else if (key === 'Home') next = 0
          else if (key === 'End') next = n - 1
          else if (key === 'Escape') {
            e?.currentTarget?.blur?.()
            return
          }
          if (next == null) return
          e?.preventDefault?.()
          setHover(Math.max(0, Math.min(n - 1, next)))
        },
        children: jsx('svg', {
          className: 'fc-spark-svg',
          viewBox: `0 0 ${SPARK.W} ${SPARK.H}`,
          preserveAspectRatio: 'none',
          'aria-hidden': 'true',
          focusable: 'false',
          children: body
        })
      }),
      cfg.legend
        ? jsxs('div', {
            className: 'fc-spark-legend',
            'aria-hidden': 'true',
            children: [
              jsxs('span', { key: 'a', children: [jsx('i', {}), cfg.legend[0]] }),
              jsxs('span', { key: 'b', children: [jsx('i', { 'data-s': '2' }), cfg.legend[1]] })
            ]
          })
        : null,
      jsxs('div', {
        className: 'fc-spark-read',
        'aria-live': 'polite',
        children: [
          jsx('span', { key: 'd', children: `${readIdx === n - 1 ? 'Latest · ' : ''}${fmtShortDate(r.ts)}` }),
          r.source ? jsx('span', { key: 's', className: 'fc-trail-pill', children: String(r.source) }) : null,
          ...cfg.read(r).map(([k, v]) => jsxs('span', { key: k, children: [`${k} `, jsx('b', { children: v })] }))
        ]
      })
    ]
  })
}

function TrendSkeleton() {
  return jsx('div', {
    className: 'fc-trend-skel',
    'aria-hidden': 'true',
    children: [0, 1, 2].map(i => jsxs('div', {
      key: i,
      className: 'fc-card',
      children: [jsx('span', { style: { width: '45%' } }), jsx('span', { style: { width: '30%', height: '14px' } }), jsx('span', { className: 'fc-skel-plot' })]
    }))
  })
}

const HISTORY_LIMIT = 100

// GET /metrics/history {snapshots, summary, filters}; `since` follows the page range.
function MetricsTrendSection({ ctx, range }) {
  const queryClient = useQueryClient()
  const queryKey = ['factonio', 'metrics-history', range]
  const query = useQuery({
    queryKey,
    queryFn: () => ctx.rest(`/metrics/history?since=${encodeURIComponent(range)}&limit=${HISTORY_LIMIT}`),
    refetchInterval: 30000
  })
  const data = query.data
  const hasData = data != null
  const isError = !!query.isError
  const raw = Array.isArray(data?.snapshots) ? data.snapshots.filter(s => s && typeof s === 'object') : []
  const snaps = raw.map((s, i) => [s, i]).sort((a, b) => (trailMs(a[0].ts) ?? 0) - (trailMs(b[0].ts) ?? 0) || a[1] - b[1]).map(x => x[0])
  const summary = data?.summary && typeof data.summary === 'object' ? data.summary : {}
  const state = !hasData ? (isError ? 'error' : 'loading') : snaps.length ? 'populated' : 'empty'

  let sub = null
  let srcs = null
  if (state === 'loading') sub = 'Loading snapshots…'
  else if (state === 'populated') {
    const total = Number.isFinite(summary.snapshot_count) ? summary.snapshot_count : snaps.length
    const first = fmtShortDate(snaps[0].ts)
    const lastD = fmtShortDate(snaps[snaps.length - 1].ts)
    const count = total > snaps.length ? `${snaps.length} of ${total} snapshots` : plural(snaps.length, 'snapshot', 'snapshots')
    sub = `${count} · ${first}${snaps.length > 1 && lastD !== first ? ` – ${lastD}` : ''}`
    const bySrc = summary.by_source && typeof summary.by_source === 'object'
      ? summary.by_source
      : snaps.reduce((acc, s) => ({ ...acc, [s.source || 'unknown']: (acc[s.source || 'unknown'] || 0) + 1 }), {})
    srcs = jsx('div', {
      className: 'fc-trend-srcs',
      'aria-label': 'Snapshots by source',
      children: Object.entries(bySrc).map(([k, v]) => jsxs('span', { key: k, className: 'fc-trail-pill', children: [jsx('span', { className: 'fc-trail-k', children: k }), ` ${v}`] }))
    })
  }

  let alert = null
  if (isError) {
    const status = describeSaveError(query.error).status
    const updated = Number(query.dataUpdatedAt) || 0
    const mins = updated ? Math.max(0, Math.round((Date.now() - updated) / 60000)) : null
    alert = jsxs('div', {
      className: 'fc-alert fc-trail-alert',
      role: 'alert',
      children: [
        jsx(Icon, { name: 'attention' }),
        jsxs('div', {
          className: 'fc-trail-alert-body',
          children: [
            jsx('b', { children: status ? `Couldn’t refresh snapshots (HTTP ${status}).` : 'Couldn’t refresh snapshots.' }),
            hasData ? ` Showing data from ${mins == null ? 'earlier' : mins < 1 ? 'just now' : `${mins} min ago`}.` : ' The history endpoint could not be loaded.'
          ]
        }),
        jsxs('button', {
          type: 'button',
          className: 'fc-btn',
          'data-action': 'retry-history',
          onClick: () => retryQuery(query, queryClient, queryKey),
          children: [jsx(Icon, { name: 'retry' }), 'Retry']
        })
      ]
    })
  }

  let body = null
  if (state === 'loading') body = jsx(TrendSkeleton, {})
  else if (state === 'empty') {
    body = jsxs('div', {
      className: 'fc-trail-state',
      'data-state': 'empty',
      children: [
        jsx('span', { className: 'fc-trail-state-ico', children: jsx(Icon, { name: 'trend', size: 16 }) }),
        jsx('h3', { children: 'Snapshots appear automatically as PRs merge' }),
        jsxs('p', { children: ['Capture one now with ', jsx('code', { className: 'fc-trend-cmd', children: 'hermes factonio metrics --snapshot' })] })
      ]
    })
  } else if (state === 'populated') {
    body = jsx('div', {
      className: 'fc-trend-grid',
      children: TREND_METRICS.map(cfg => jsx(SparkCard, { key: cfg.metric, cfg, snaps }))
    })
  }

  return jsxs('section', {
    className: 'fc-trend',
    'aria-labelledby': 'fc-trend-h',
    'aria-busy': state === 'loading' ? 'true' : undefined,
    'data-state': state,
    'data-stale': isError && hasData ? 'true' : undefined,
    children: [
      jsxs('div', {
        className: 'fc-trend-head',
        children: [
          jsxs('div', { children: [jsx('h2', { id: 'fc-trend-h', children: 'Trends' }), sub ? jsx('p', { className: 'fc-sec-sub', children: sub }) : null] }),
          srcs
        ]
      }),
      alert,
      body
    ]
  })
}

function FactonioPage({ ctx }) {
  const [range, setRange] = useState('30d')
  const metricsQ = useQuery({
    queryKey: ['factonio', 'metrics', range],
    queryFn: () => ctx.rest(`/metrics?range=${encodeURIComponent(range)}`),
    refetchInterval: 30000
  })
  const reposQ = useQuery({
    queryKey: ['factonio', 'repos'],
    queryFn: () => ctx.rest('/repos'),
    refetchInterval: 30000
  })
  const modelsQ = useQuery({
    queryKey: ['factonio', 'models'],
    queryFn: () => ctx.rest('/models'),
    refetchInterval: 30000
  })

  const m = metricsQ.data
  const models = modelsQ.data?.models || {}
  const warn = modelsQ.data?.cross_model_warning

  const ranges = ['7d', '30d', '90d', 'all']

  return jsxs('div', {
    className: 'fc-page',
    children: [
      jsxs('div', {
        className: 'fc-wrap',
        children: [
          jsxs('div', {
            className: 'fc-head',
            children: [
              jsxs('div', {
                children: [
                  jsx('h1', { className: 'fc-title', children: 'Factonio' }),
                  jsx('p', {
                    className: 'fc-sub',
                    children: 'Phase-based software factory (Definition, Implementation, isolated parallel Verification)'
                  })
                ]
              }),
              jsx('div', {
                className: 'fc-ranges',
                children: ranges.map(r =>
                  jsx('button', {
                    key: r,
                    type: 'button',
                    className: 'fc-btn',
                    'data-active': String(range === r),
                    onClick: () => setRange(r),
                    children: r
                  })
                )
              })
            ]
          }),
          warn ? jsx('div', { className: 'fc-warn', children: warn }) : null,
          jsxs('div', {
            className: 'fc-grid fc-grid--6',
            children: [
              jsxs('div', {
                className: 'fc-card',
                children: [
                  jsx('div', { className: 'fc-k', children: 'Workstreams' }),
                  jsx('div', { className: 'fc-v', children: m?.totals?.workstreams ?? '—' }),
                  jsx('div', {
                    className: 'fc-hint',
                    children: m ? `${m.totals.done} done · ${m.totals.running} running · ${m.totals.blocked} blocked` : 'Loading'
                  })
                ]
              }),
              jsxs('div', {
                className: 'fc-card',
                children: [
                  jsx('div', { className: 'fc-k', children: 'Diff Check Pass' }),
                  jsx('div', { className: 'fc-v', children: fmtPct(m?.verification?.diff?.pass_rate) }),
                  jsx('div', {
                    className: 'fc-hint',
                    children: m ? `${m.verification.diff.pass} pass · ${m.verification.diff.reject} reject` : '—'
                  })
                ]
              }),
              jsxs('div', {
                className: 'fc-card',
                children: [
                  jsx('div', { className: 'fc-k', children: 'Black-Box Pass' }),
                  jsx('div', { className: 'fc-v', children: fmtPct(m?.verification?.blackbox?.pass_rate) }),
                  jsx('div', {
                    className: 'fc-hint',
                    children: m ? `${m.verification.blackbox.pass} pass · ${m.verification.blackbox.reject} reject` : '—'
                  })
                ]
              }),
              jsxs('div', {
                className: 'fc-card',
                children: [
                  jsx('div', { className: 'fc-k', children: 'Escape Rate' }),
                  jsx('div', { className: 'fc-v', children: fmtPct(m?.escapes?.escape_rate) }),
                  jsx('div', {
                    className: 'fc-hint',
                    children: m ? `${m.escapes.count} escapes / ${m.escapes.verified_total} verified` : '—'
                  })
                ]
              }),
              jsxs('div', {
                className: 'fc-card',
                children: [
                  jsx('div', { className: 'fc-k', children: 'Rework' }),
                  jsx('div', { className: 'fc-v', children: m?.rework?.avg_runs_per_implement_card ?? '—' }),
                  jsx('div', {
                    className: 'fc-hint',
                    children: m ? `avg runs/card (${m.rework.cards_with_rework} reworked)` : '—'
                  })
                ]
              }),
              jsx(VerificationPanelsKpi, { m })
            ]
          }),
          jsx(MetricsTrendSection, { ctx, range }),
          jsxs('div', {
            className: 'fc-sec',
            children: [
              jsx('h2', { children: 'Phase Durations' }),
              jsxs('table', {
                className: 'fc-table',
                children: [
                  jsx('thead', {
                    children: jsxs('tr', {
                      children: [
                        jsx('th', { children: 'Phase' }),
                        jsx('th', { children: 'Cards' }),
                        jsx('th', { children: 'Done' }),
                        jsx('th', { children: 'Median' }),
                        jsx('th', { children: 'P90' })
                      ]
                    })
                  }),
                  jsx('tbody', {
                    children: ['define', 'design', 'implement', 'verify-diff', 'verify-blackbox'].map(phase => {
                      const row = m?.phases?.[phase] || {}
                      return jsxs('tr', {
                        key: phase,
                        children: [
                          jsx('td', { className: 'fc-mono', children: phase }),
                          jsx('td', { children: row.count ?? 0 }),
                          jsx('td', { children: row.done ?? 0 }),
                          jsx('td', { children: fmtDur(row.median_seconds) }),
                          jsx('td', { children: fmtDur(row.p90_seconds) })
                        ]
                      })
                    })
                  })
                ]
              })
            ]
          }),
          jsx(TrailSection, { ctx, reposQ }),
          jsxs('div', {
            className: 'fc-sec',
            children: [
              jsxs('div', {
                className: 'fc-sec-head',
                children: [
                  jsx('h2', { children: 'Phase Model Configuration' }),
                  jsx(ConfigurePhaseModelsButton, {})
                ]
              }),
              jsx('p', { className: 'fc-hint', children: jsx(AuxModelsNote, {}) }),
              jsx(ModelsQueryStatus, { query: modelsQ }),
              jsx(VerifyPanelStatus, { query: modelsQ }),
              jsx(PhaseModelsTable, { models })
            ]
          }),
          jsx(ReposSection, { ctx, reposQ })
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// Factonio sidebar tab (right sidebar, docked in the Files tab strip).
// Pure derivation helpers are ported 1:1 from the approved Phase 1b design
// (design/preview.html) and reuse fmtDur / formatPhaseModelLabel above.
// ---------------------------------------------------------------------------

function revealSidebar() {
  if (typeof host.revealPane === 'function') host.revealPane(PANE_ID)
  else host.navigate(ROUTE_PATH)
}

const ACTIVE = new Set(['running', 'ready', 'todo', 'review'])
const QUEUED = new Set(['ready', 'todo', 'review'])
const PHASE_LABEL = { define: 'define', design: 'design', implement: 'implement', 'verify-diff': 'diff', 'verify-blackbox': 'black-box', other: 'other' }
const PHASE_RANK = { define: 0, design: 1, implement: 2, 'verify-diff': 3, 'verify-blackbox': 4, other: 5 }
const WS_RANK = { attention: 0, active: 1, completed: 2 }
// Same reject vocabulary as metrics.py `_REJECT_VERDICTS` (counts.rejected).
const REJECT_VERDICTS = new Set(['reject', 'fail', 'block', 'changes_requested'])
const SCOPES = ['thread', 'all']
const STATUS_FILTERS = ['all', 'active', 'attention', 'done']
const PHASE_FILTERS = ['all', 'define', 'design', 'implement', 'verify']
const STATE_ICON = { done: 'check', running: 'running', queued: 'queued', attention: 'attention', pending: 'pending', skipped: 'skipped' }

function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
const stripTag = t => String(t || '').replace(/^\s*\[[^\]]+\]\s*/, '')
const safePrUrl = pr => (pr && typeof pr.url === 'string' && /^https:\/\//i.test(pr.url)) ? pr.url : null

// 'pass' | 'reject' | 'warn' (inconclusive) | null — normalizes the API verdict for tones and resolution.
function verdictTone(v) {
  const s = String(v || '').toLowerCase()
  if (!s) return null
  if (REJECT_VERDICTS.has(s)) return 'reject'
  if (s === 'pass') return 'pass'
  if (s === 'inconclusive') return 'warn'
  return null
}

// A reject / inconclusive is resolved once a later card of the same verify phase in the same
// tenant passed. Peer lanes of the same verification panel never resolve each other.
function isUnresolvedVerdict(c, wsCards, tone) {
  if (verdictTone(c.verdict) !== tone) return false
  return !wsCards.some(o => o.phase === c.phase && verdictTone(o.verdict) === 'pass' &&
    (o.created_at || 0) > (c.created_at || 0) && !(c.panel_id && o.panel_id === c.panel_id))
}
const isUnresolvedReject = (c, wsCards) => isUnresolvedVerdict(c, wsCards, 'reject')
const isUnresolvedInconclusive = (c, wsCards) => isUnresolvedVerdict(c, wsCards, 'warn')
const isAttention = (c, wsCards) => c.status === 'blocked' || c.status === 'triage' || isUnresolvedReject(c, wsCards) || isUnresolvedInconclusive(c, wsCards)
const isCollapsibleDone = (c, wsCards) => c.status === 'done' && !isUnresolvedReject(c, wsCards) && !isUnresolvedInconclusive(c, wsCards)

function cardRank(c, wsCards) {
  if (isAttention(c, wsCards)) return 0
  if (c.status === 'running') return 1
  if (QUEUED.has(c.status)) return 2
  return 3
}
function stageState(cs, wsCards) {
  if (!cs.length) return 'pending'
  if (cs.some(c => isAttention(c, wsCards))) return 'attention'
  if (cs.some(c => c.status === 'running')) return 'running'
  if (cs.some(c => ACTIVE.has(c.status))) return 'queued'
  return 'done'
}
function stageWord(state, cs, wsCards = cs) {
  if (state !== 'attention') return state
  if (cs.some(c => c.status === 'blocked')) return 'blocked'
  if (cs.some(c => c.status === 'triage')) return 'triage'
  if (cs.some(c => isUnresolvedReject(c, wsCards))) return 'rejected'
  return 'inconclusive'
}
function mergeStates(a, b) {
  for (const s of ['attention', 'running', 'queued']) if (a === s || b === s) return s
  if (a === b) return a // both done or both pending
  return 'queued' // one done, one not started
}
// Newest card of a phase: highest created_at, ties broken deterministically by id.
function latestCard(cs) {
  let best = null
  for (const c of cs) {
    if (!best) { best = c; continue }
    const d = (c.created_at ?? 0) - (best.created_at ?? 0)
    if (d > 0 || (d === 0 && String(c.id ?? '') > String(best.id ?? ''))) best = c
  }
  return best
}
// Each stage resolves from its latest card only (spec §3): a newer fix-round card
// supersedes older blocked/rejected attempts of the same phase. Unresolved historical
// rejections still drive workstream/card attention via isAttention().
function computePipelineStages(wsCards) {
  const node = ph => {
    const phaseCards = wsCards.filter(c => c.phase === ph)
    const last = latestCard(phaseCards)
    // A verification panel's lanes run side by side: the stage reflects every lane of the newest panel.
    const cs = !last ? [] : last.panel_id ? phaseCards.filter(c => c.panel_id === last.panel_id) : [last]
    const state = stageState(cs, wsCards)
    return { state, word: stageWord(state, cs, wsCards) }
  }
  const define = node('define')
  const design = node('design')
  const implement = node('implement')
  const diff = node('verify-diff')
  const bb = node('verify-blackbox')
  if (design.state === 'pending' && [implement, diff, bb].some(n => n.state !== 'pending')) {
    design.state = 'skipped'
    design.word = 'skipped'
  }
  const vs = mergeStates(diff.state, bb.state)
  return { define, design, implement, verify: { state: vs, word: vs }, diff, bb }
}
function wsState(wsCards) {
  if (wsCards.some(c => isAttention(c, wsCards))) return 'attention'
  if (wsCards.some(c => ACTIVE.has(c.status))) return 'active'
  return 'completed'
}
function matchStatus(c, f, wsCards) {
  if (f === 'active') return ACTIVE.has(c.status)
  if (f === 'attention') return isAttention(c, wsCards)
  if (f === 'done') return c.status === 'done'
  return true
}
function matchPhase(c, f) {
  if (f === 'all') return true
  if (f === 'verify') return c.phase === 'verify-diff' || c.phase === 'verify-blackbox'
  return c.phase === f
}
function groupBy(list, key) {
  const m = new Map()
  for (const x of list) {
    const k = key(x)
    if (!m.has(k)) m.set(k, [])
    m.get(k).push(x)
  }
  return m
}
// "tozes-factonio-improve-factonio-sidebar" + repo "tozes/factonio" -> { repo: "factonio", slug: "improve-factonio-sidebar" }
function wsName(tenant, repos) {
  if (!tenant) return { repo: null, slug: 'Ungrouped' }
  for (const r of repos) {
    const [owner, name] = String(r).split('/')
    if (!owner || !name) continue
    const p = `${owner}-${name}-`
    if (tenant.startsWith(p) && tenant.length > p.length) return { repo: name, slug: tenant.slice(p.length) }
  }
  return { repo: null, slug: tenant }
}
function statusCounts(scoped, byTenant) {
  const n = { all: scoped.length, active: 0, attention: 0, done: 0 }
  for (const c of scoped) {
    const ws = byTenant.get(c.tenant || '') || []
    if (ACTIVE.has(c.status)) n.active++
    if (isAttention(c, ws)) n.attention++
    if (c.status === 'done') n.done++
  }
  return n
}
function filterCards(scoped, byTenant, status, phase) {
  return scoped.filter(c => matchStatus(c, status, byTenant.get(c.tenant || '') || []) && matchPhase(c, phase))
}
// Visible workstreams (>= 1 card passes the filters), ordered attention → active → completed,
// ties by most recent max(started_at || created_at). Pipelines use every scoped card.
function groupWorkstreams(byTenant, visible, repos) {
  const vis = groupBy(visible, c => c.tenant || '')
  const latest = cs => Math.max(0, ...cs.map(c => c.started_at || c.created_at || 0))
  return [...vis.keys()].map(t => {
    const wsCards = byTenant.get(t) || []
    return { tenant: t, wsCards, visible: vis.get(t), state: wsState(wsCards), name: wsName(t, repos) }
  }).sort((a, b) => WS_RANK[a.state] - WS_RANK[b.state] || latest(b.wsCards) - latest(a.wsCards))
}
function sortCards(list, wsCards) {
  return [...list].sort((a, b) =>
    cardRank(a, wsCards) - cardRank(b, wsCards) ||
    (PHASE_RANK[a.phase] ?? 5) - (PHASE_RANK[b.phase] ?? 5) ||
    (a.created_at || 0) - (b.created_at || 0))
}
function cardCallouts(c, wsCards) {
  const out = []
  if (c.status === 'blocked') {
    out.push({ tone: c.block_kind === 'needs_input' ? 'warn' : 'danger', kind: String(c.block_kind || 'blocked').replace(/_/g, ' '), text: c.block_reason || 'Blocked' })
  }
  if (c.status === 'triage') out.push({ tone: 'warn', kind: 'triage', text: c.block_reason || 'Waiting for triage' })
  if (isUnresolvedReject(c, wsCards)) out.push({ tone: 'danger', kind: 'rejected', text: c.summary || 'Verification rejected' })
  if (c.last_error) out.push({ tone: 'danger', kind: 'error', text: String(c.last_error) })
  return out
}
const domSafe = s => String(s || '').replace(/[^A-Za-z0-9_-]/g, '_')

// --- verification panels (rigor=high) ------------------------------------
// Client-side mirror of metrics.aggregate_panel_verdicts over the already-polled
// /cards lanes (`panel_id`, `panel_vendor`, `verdict`, `status`) joined with the
// newest `verify_panel_consensus` row from /trail. Fail closed: a recorded
// consensus and the live lanes are combined by severity, and a pass needs the
// orchestrator's recorded consensus.
const PANEL_SEVERITY = { reject: 3, inconclusive: 2, pending: 1, pass: 0 }
const PANEL_TONE = { pass: 'pass', reject: 'reject', inconclusive: 'warn', pending: 'info' }
const PANEL_GLYPH = { pass: 'check', reject: 'x', inconclusive: 'attention', pending: 'spin' }
const PASS_VERDICTS = new Set(['pass'])
const knownVendor = v => {
  const s = String(v || '').trim().toLowerCase()
  return s && s !== 'unknown' && s !== 'none' ? s : null
}

function laneBucket(verdict) {
  const s = String(verdict ?? '').trim().toLowerCase()
  if (!s) return null
  if (REJECT_VERDICTS.has(s)) return 'reject'
  if (PASS_VERDICTS.has(s)) return 'pass'
  return 'inconclusive'
}

function aggregatePanelLanes(lanes) {
  const buckets = { pass: [], reject: [], inconclusive: [], pending: [] }
  const vendors = []
  let incomplete = false
  for (const lane of lanes) {
    const v = knownVendor(lane.vendor)
    if (v && !vendors.includes(v)) vendors.push(v)
    const complete = lane.status === 'done' && lane.bucket != null
    if (!complete) incomplete = true
    if (lane.bucket != null && (complete || lane.bucket !== 'pass')) buckets[lane.bucket].push(lane)
    else buckets.pending.push(lane)
  }
  let consensus
  if (buckets.reject.length) consensus = 'reject'
  else if (buckets.inconclusive.length) consensus = 'inconclusive'
  else if (incomplete) consensus = 'pending'
  else if (lanes.length >= 2 && vendors.length >= 2) consensus = 'pass'
  else consensus = 'inconclusive'
  const decided = ['pass', 'reject', 'inconclusive'].filter(b => buckets[b].length)
  return { consensus, buckets, vendors, hasDisagreement: decided.length > 1 }
}

// "{dissenting vendor} {verdict} vs {other vendors} {verdict}: “{dissenting summary}”"
function panelDissent(buckets) {
  const worst = buckets.reject.length ? 'reject' : 'inconclusive'
  const dissent = buckets[worst]
  const others = ['pass', 'reject', 'inconclusive'].filter(b => b !== worst && buckets[b].length)
  if (!dissent.length || !others.length) return null
  const names = ls => [...new Set(ls.map(l => l.vendor || 'unknown'))].join(', ')
  const vs = others.map(b => `${names(buckets[b])} ${b}`).join(', ')
  const quote = dissent.map(l => l.summary).find(Boolean)
  return { lead: `${names(dissent)} ${worst} vs ${vs}`, quote: quote ? String(quote) : null }
}

const laneIdOf = l => {
  const id = Array.isArray(l) ? l[0] : l && typeof l === 'object' ? l.task_id ?? l.id : l
  return id == null || id === '' ? null : String(id)
}
const laneIdList = v => (Array.isArray(v) ? v.map(laneIdOf).filter(Boolean) : [])
const laneCount = v => (Number.isInteger(v) && v > 0 ? v : 0)

// Lanes a panel must contain before it can pass, mirroring backend summarize_panel's
// expected_lane_ids: every lane spawn_verify recorded plus every lane a recorded consensus
// covered. `recordedIds` is the newest consensus row's own lane set (null when unrecorded).
function expectedPanelLanes(spawnRows, row) {
  const ids = new Set()
  let count = 0
  for (const d of spawnRows) {
    const ev = d.evidence
    for (const l of Array.isArray(ev.panel_lanes) ? ev.panel_lanes : []) {
      if (!l || typeof l !== 'object') continue
      for (const k of ['verify_diff_task_id', 'verify_blackbox_task_id', 'task_id']) if (l[k]) ids.add(String(l[k]))
    }
    for (const id of [...laneIdList(ev.expected_lane_ids), ...laneIdList(ev.lanes)]) ids.add(id)
    count = Math.max(count, laneCount(ev.expected_lane_count))
  }
  let recordedIds = null
  if (row) {
    const ev = row.evidence
    const own = [...laneIdList(ev.lanes), ...laneIdList(ev.lane_fingerprint)]
    if (own.length) recordedIds = new Set(own)
    for (const id of [...own, ...laneIdList(ev.expected_lane_ids), ...laneIdList(ev.missing_lane_ids)]) ids.add(id)
    count = Math.max(count, laneCount(ev.expected_lane_count), laneCount(ev.lane_count))
  }
  return { ids: [...ids], count: Math.max(count, ids.size), recordedIds }
}

// Panels of one workstream, newest first: [{ id, consensus, lanes, decided, vendors,
// hasDisagreement, dissent, recorded, missingLaneIds, expectedLaneCount, reason, createdAt }].
// `trailDecisions` are that tenant's /trail rows (any order).
function computeWorkstreamPanels(wsCards, trailDecisions) {
  const groups = groupBy((wsCards || []).filter(c => c && c.panel_id), c => String(c.panel_id))
  const rows = (Array.isArray(trailDecisions) ? trailDecisions : []).filter(d => d && d.evidence && typeof d.evidence === 'object')
  const consensusRows = rows.filter(d => d.decision === 'verify_panel_consensus' && !d.is_superseded)
  const spawnRows = rows.filter(d => d.decision === 'spawn_verify')
  const out = []
  for (const [id, cards] of groups) {
    const ordered = [...cards].sort((a, b) => (a.created_at || 0) - (b.created_at || 0) || String(a.id).localeCompare(String(b.id)))
    const lanes = ordered.map(c => ({
      cardId: c.id,
      vendor: c.panel_vendor ? String(c.panel_vendor) : 'unknown',
      model: formatPhaseModelLabel(c),
      phase: c.phase,
      status: c.status,
      verdict: c.verdict ? String(c.verdict).toLowerCase() : null,
      bucket: laneBucket(c.verdict),
      summary: c.summary || null
    }))
    const live = aggregatePanelLanes(lanes)
    const row = consensusRows
      .filter(d => d.evidence.panel_id === id)
      .sort((a, b) => (trailMs(b.ts) ?? 0) - (trailMs(a.ts) ?? 0))[0] || null
    const recorded = row && Object.hasOwn(PANEL_SEVERITY, row.result) && row.result !== 'pending' ? row.result : null
    const expected = expectedPanelLanes(spawnRows.filter(d => String(d.evidence.panel_id ?? '') === id), row)
    const liveIds = new Set(lanes.map(l => String(l.cardId)))
    const missingLaneIds = expected.ids.filter(x => !liveIds.has(x))
    const short = missingLaneIds.length || expected.count > lanes.length
    // A recorded pass only vouches for the exact lane set it evaluated.
    const stale = recorded === 'pass' && expected.recordedIds &&
      (expected.recordedIds.size !== liveIds.size || [...liveIds].some(x => !expected.recordedIds.has(x)))
    let consensus = live.consensus === 'pass' && (!recorded || stale) ? 'pending' : live.consensus
    if (recorded && PANEL_SEVERITY[recorded] > PANEL_SEVERITY[consensus]) consensus = recorded
    let reason = null
    if (short && consensus !== 'reject') {
      consensus = 'inconclusive'
      const n = missingLaneIds.length || expected.count - lanes.length
      reason = `${n} expected lane(s) missing${missingLaneIds.length ? `: ${missingLaneIds.join(', ')}` : ''}`
    }
    const hasDisagreement = live.hasDisagreement || !!row?.evidence?.has_disagreement
    let dissent = null
    if (hasDisagreement) {
      const summary = row?.evidence?.disagreement_summary
      dissent = summary ? { lead: String(summary), quote: null } : panelDissent(live.buckets)
    }
    out.push({
      id,
      consensus,
      recorded,
      lanes,
      decided: lanes.filter(l => l.verdict).length,
      vendors: live.vendors,
      hasDisagreement,
      dissent,
      missingLaneIds,
      expectedLaneCount: expected.count || null,
      reason,
      createdAt: Math.max(0, ...ordered.map(c => c.created_at || 0))
    })
  }
  return out.sort((a, b) => b.createdAt - a.createdAt || b.id.localeCompare(a.id))
}

function VendorChip({ lane, mixed }) {
  const verdict = lane.bucket || 'pending'
  return jsxs('span', {
    className: 'fc-sb-vendor',
    'data-verdict': verdict,
    title: `${PHASE_LABEL[lane.phase] || lane.phase || 'lane'} · ${lane.model}: ${lane.verdict || 'running'}`,
    children: [jsx(Icon, { name: PANEL_GLYPH[verdict], size: 10 }), mixed ? `${lane.vendor} · ${PHASE_LABEL[lane.phase] || lane.phase}` : lane.vendor]
  })
}

const mixedPhases = lanes => new Set(lanes.map(l => l.phase)).size > 1

function PanelStrip({ ctx, panel: p, tenant }) {
  const queryClient = useQueryClient()
  const [action, run] = useAction()
  const reeval = () => run(async () => {
    const body = { panel_id: p.id }
    if (typeof tenant === 'string' && TENANT_SLUG_RE.test(tenant)) body.tenant = tenant
    const res = await ctx.rest('/panels/evaluate', { method: 'POST', body })
    invalidateKeys(queryClient, [['factonio', 'cards'], ['factonio', 'trail']])
    return res
  })
  const mixed = mixedPhases(p.lanes)
  const total = Math.max(p.lanes.length, p.expectedLaneCount || 0)
  const label = `Verification panel ${p.id}: ${p.consensus}, ${p.decided} of ${total} lanes${p.hasDisagreement ? ', lanes disagree' : ''}${p.reason ? `, ${p.reason}` : ''}`
  const full = p.dissent ? `${p.dissent.lead}${p.dissent.quote ? `: “${p.dissent.quote}”` : ''}` : ''
  return jsxs('div', {
    className: 'fc-sb-panel',
    role: 'group',
    'aria-label': label,
    'data-panel': p.id,
    'data-consensus': p.consensus,
    'data-disagree': String(!!p.hasDisagreement),
    children: [
      jsxs('div', {
        className: 'fc-sb-panel-top',
        children: [
          jsx('span', { className: 'fc-sb-panel-k', title: 'Multi-vendor verification panel', children: jsx(Icon, { name: 'panel', size: 11 }) }),
          jsx('span', { className: 'fc-sb-tag', 'data-tone': PANEL_TONE[p.consensus], 'data-consensus': p.consensus, children: p.consensus }),
          jsx('span', { className: 'fc-sb-panel-id fc-sb-mono', title: p.id, children: p.id }),
          jsx('span', { className: 'fc-sb-panel-lanes', children: `${p.decided}/${total} lanes` }),
          ctx ? jsx(ReevalButton, { key: 're', label: 'verification panel', state: action, onRun: reeval }) : null
        ]
      }),
      jsx('div', {
        className: 'fc-sb-vendors',
        'aria-label': 'Lanes by vendor',
        children: p.lanes.map(l => jsx(VendorChip, { key: l.cardId, lane: l, mixed }))
      }),
      p.reason
        ? jsxs('div', {
            className: 'fc-sb-callout',
            'data-tone': 'warn',
            'data-kind': 'missing',
            role: 'note',
            children: [
              jsx(Icon, { name: 'attention' }),
              jsxs('span', {
                className: 'fc-sb-callout-body',
                title: `${p.reason}; the panel cannot pass without every lane`,
                children: [jsx('span', { className: 'fc-sb-kind', children: 'missing' }), p.reason]
              })
            ]
          })
        : null,
      p.hasDisagreement && p.dissent
        ? jsxs('div', {
            className: 'fc-sb-callout',
            'data-tone': 'warn',
            'data-kind': 'disagree',
            role: 'note',
            children: [
              jsx(Icon, { name: 'split' }),
              jsxs('span', {
                className: 'fc-sb-callout-body',
                title: full,
                children: [
                  jsx('span', { className: 'fc-sb-kind', children: 'disagree' }),
                  p.dissent.lead,
                  p.dissent.quote ? [': ', jsx('q', { key: 'q', children: p.dissent.quote })] : null
                ]
              })
            ]
          })
        : null,
      ctx ? jsx(ReevalMessage, { key: 'msg', state: action, onRun: reeval, what: 'panel' }) : null
    ]
  })
}

// --- Wave 5: workstream modes and /arena ------------------------------------
const WORKSTREAM_KINDS = new Set(['feature', 'bug', 'quick', 'refactor', 'perf', 'investigate'])
const BADGED_MODES = new Set(['refactor', 'perf', 'investigate'])
const MODE_GLYPH = { perf: 'trend', refactor: 'retry', investigate: 'eye' }
const MODE_TITLE = {
  perf: 'Performance mode: benchmark checklist contract',
  refactor: 'Refactor mode: behaviour-preservation contract',
  investigate: 'Investigate mode · read-only: workers may not change the repository'
}
const isWsKind = k => typeof k === 'string' && WORKSTREAM_KINDS.has(k)
const newestFirst = (a, b) => (b.created_at || 0) - (a.created_at || 0) || String(b.id || '').localeCompare(String(a.id || ''))

// { kind, readOnly, reportName, reportExists } for one workstream. Sources, in order: GET /cards
// `workstreams`, the newest card `workstream_kind` (older backend), the newest start_workstream row.
function workstreamMode(tenant, workstreams, wsCards, trailRows) {
  const t = String(tenant || '')
  let kind = null
  let readOnly = false
  let reportPath = null
  let reportExists = false
  const ws = t && workstreams && typeof workstreams === 'object' && !Array.isArray(workstreams) ? workstreams[t] : null
  if (ws && typeof ws === 'object' && isWsKind(ws.kind)) {
    kind = ws.kind
    readOnly = ws.read_only === true
    reportPath = ws.report_path
    reportExists = ws.report_exists === true
  } else {
    const card = (Array.isArray(wsCards) ? wsCards : []).filter(c => c && isWsKind(c.workstream_kind)).sort(newestFirst)[0]
    if (card) {
      kind = card.workstream_kind
    } else {
      const row = (Array.isArray(trailRows) ? trailRows : [])
        .filter(d => d && d.decision === 'start_workstream' && (!t || !d.tenant || d.tenant === t) && d.evidence && isWsKind(d.evidence.kind))
        .sort((a, b) => (trailMs(b.ts) ?? 0) - (trailMs(a.ts) ?? 0))[0]
      if (row) {
        kind = row.evidence.kind
        readOnly = row.evidence.read_only === true
      }
    }
  }
  const investigate = kind === 'investigate'
  return { kind, readOnly: readOnly || investigate, reportName: investigate ? safeBasename(reportPath) : null, reportExists: investigate && reportExists }
}

function ModePill({ mode, suffix }) {
  if (!mode || !BADGED_MODES.has(mode.kind)) return null
  const ro = suffix && mode.kind === 'investigate'
  return jsxs('span', {
    className: 'fc-sb-mode',
    'data-mode': mode.kind,
    title: MODE_TITLE[mode.kind],
    children: [jsx(Icon, { key: 'i', name: MODE_GLYPH[mode.kind], size: 10 }), mode.kind, ro ? jsx('span', { key: 'ro', className: 'fc-sb-mode-ro', children: ' · read-only' }) : null]
  })
}

function ReportIndicator({ mode, onOpen }) {
  const ready = !!mode.reportExists
  if (ready && onOpen) {
    return jsxs('button', {
      type: 'button',
      className: 'fc-sb-report fc-sb-report-btn',
      'data-ready': 'true',
      'data-action': 'open-report',
      'aria-haspopup': 'dialog',
      title: 'Open investigation.md (read-only)',
      onClick: e => {
        e?.stopPropagation?.()
        onOpen()
      },
      children: [jsx(Icon, { key: 'i', name: 'doc', size: 10 }), 'report ready', jsx(Icon, { key: 'c', name: 'chev', size: 9 })]
    })
  }
  return jsxs('span', {
    className: 'fc-sb-report',
    'data-ready': String(ready),
    title: ready ? 'investigation.md ready' : 'investigation.md not written yet',
    children: [jsx(Icon, { key: 'i', name: ready ? 'check' : 'queued', size: 10 }), ready ? 'report ready' : 'report pending']
  })
}

const ARENA_STATUS = {
  candidates_in_progress: { label: 'in progress', tone: 'info' },
  ready_for_synthesis: { label: 'ready', tone: 'info' },
  judging: { label: 'judging', tone: 'info' },
  synthesized: { label: 'synthesized', tone: 'pass' },
  needs_attention: { label: 'attention', tone: 'warn', callout: 'advisory', copy: 'A candidate needs attention before the judge can run.' },
  degraded: { label: 'degraded', tone: 'warn', callout: 'degraded', copy: 'No cross-vendor judge was available; the judge shares a vendor with a candidate.' },
  blocked: { label: 'blocked', tone: 'reject', callout: 'blocked', copy: 'The arena cannot reach a judge. Unblock a candidate card or start a new arena.' }
}
const ARENA_RUN_ICON = { ready: 'check', done: 'check', running: 'spin', 'no-artifact': 'attention', failed: 'x' }
const ARENA_FAILED = new Set(['blocked', 'failed', 'error'])

function arenaRunState(status, artifactReady) {
  const s = String(status || '').toLowerCase()
  if (s === 'done') return artifactReady === true ? 'ready' : artifactReady === false ? 'no-artifact' : 'done'
  if (s === 'running') return 'running'
  if (ARENA_FAILED.has(s)) return 'failed'
  return null
}

const arenaStr = barrierStr
function arenaCandidate(c) {
  if (!c || typeof c !== 'object') return null
  return {
    slot: arenaStr(c.slot),
    vendor: arenaStr(c.vendor),
    model: arenaStr(c.model),
    task_id: arenaStr(c.task_id),
    status: arenaStr(c.status),
    artifact_ready: typeof c.artifact_ready === 'boolean' ? c.artifact_ready : null
  }
}
function arenaJudge(j) {
  if (!j || typeof j !== 'object') return null
  return {
    task_id: arenaStr(j.task_id),
    status: arenaStr(j.status),
    vendor: arenaStr(j.vendor),
    model: arenaStr(j.model),
    cross_vendor: typeof j.cross_vendor === 'boolean' ? j.cross_vendor : null
  }
}

// Newest-first arenas of one workstream from GET /cards `arenas`; when an older backend omits
// `arenas`, rebuilt from card `arena_*` tags (status unknown, marked `legacy`).
function computeWorkstreamArenas(tenant, wsCards, apiArenas) {
  const t = String(tenant || '')
  if (!t) return []
  if (Array.isArray(apiArenas)) {
    return apiArenas
      .filter(a => a && typeof a === 'object' && a.tenant === t && arenaStr(a.arena_id))
      .map(a => ({
        arena_id: a.arena_id,
        tenant: t,
        title: arenaStr(a.title),
        target: arenaStr(a.target),
        status: arenaStr(a.status),
        legacy: false,
        created_at: Number.isFinite(a.created_at) ? a.created_at : null,
        candidates: (Array.isArray(a.candidates) ? a.candidates : []).map(arenaCandidate).filter(Boolean),
        judge: arenaJudge(a.judge),
        advisories: (Array.isArray(a.advisories) ? a.advisories : []).filter(x => arenaStr(x))
      }))
  }
  const tagged = (Array.isArray(wsCards) ? wsCards : []).filter(c => c && arenaStr(c.arena_id) && (c.arena_role === 'candidate' || c.arena_role === 'judge'))
  const out = []
  for (const [id, cards] of groupBy(tagged, c => String(c.arena_id))) {
    const ordered = [...cards].sort((a, b) => (a.created_at || 0) - (b.created_at || 0) || String(a.id).localeCompare(String(b.id)))
    const judgeCard = ordered.filter(c => c.arena_role === 'judge').sort(newestFirst)[0]
    out.push({
      arena_id: id,
      tenant: t,
      title: null,
      target: arenaStr(ordered.find(c => arenaStr(c.arena_target))?.arena_target),
      status: null,
      legacy: true,
      created_at: Math.max(0, ...ordered.map(c => c.created_at || 0)),
      candidates: ordered.filter(c => c.arena_role === 'candidate').map(c => arenaCandidate({ slot: c.arena_slot, vendor: c.arena_vendor, model: formatPhaseModelLabel(c), task_id: c.id, status: c.status })),
      judge: judgeCard ? arenaJudge({ task_id: judgeCard.id, status: judgeCard.status, vendor: judgeCard.arena_vendor, model: formatPhaseModelLabel(judgeCard) }) : null,
      advisories: []
    })
  }
  return out.sort((a, b) => b.created_at - a.created_at || b.arena_id.localeCompare(a.arena_id))
}

function arenaStatusInfo(a) {
  if (a.legacy || !a.status) return { label: 'from cards', tone: undefined, title: 'Older backend: rebuilt from card tags; arena status and artifact readiness unknown' }
  const s = ARENA_STATUS[a.status]
  return s ? { ...s, title: `Status: ${a.status}` } : { label: a.status, tone: undefined, title: `Status: ${a.status}` }
}

function arenaCounts(a) {
  const n = a.candidates.length
  if (a.legacy) {
    const done = a.candidates.filter(c => c.status === 'done').length
    return { n, k: done, text: `${done}/${n} done`, title: `${done} of ${n} candidate cards done` }
  }
  const ready = a.candidates.filter(c => c.artifact_ready === true).length
  return { n, k: ready, text: `${ready}/${n} ready`, title: `${ready} of ${n} candidate artifacts ready` }
}

function ArenaCandidateChip({ cand }) {
  const run = arenaRunState(cand.status, cand.artifact_ready)
  const label = [cand.slot, cand.model].filter(Boolean).join(' · ') || 'candidate'
  return jsxs('span', {
    className: 'fc-sb-vendor',
    'data-run': run || undefined,
    title: `${label}: ${cand.status || 'queued'}${cand.artifact_ready === true ? ', artifact ready' : ''}`,
    children: [jsx(Icon, { key: 'i', name: ARENA_RUN_ICON[run] || 'queued', size: 10 }), cand.vendor || 'unknown']
  })
}

function ArenaJudgeChip({ judge }) {
  const run = judge?.task_id ? arenaRunState(judge.status, null) : null
  const cross = judge?.cross_vendor
  let glyph = judge?.task_id ? (run === 'done' ? 'check' : ARENA_RUN_ICON[run] || 'queued') : 'pending'
  if (cross === false) glyph = 'attention'
  const crossText = cross === true ? ', cross-vendor' : cross === false ? ', same vendor as a candidate' : ''
  return jsxs('span', {
    className: 'fc-sb-vendor',
    'data-role': 'judge',
    'data-cross': cross == null ? undefined : String(cross),
    'data-run': run || undefined,
    title: `Judge${judge?.model ? ` · ${judge.model}` : ''}: ${judge?.task_id ? judge.status || 'queued' : 'not spawned yet'}${crossText}`,
    children: [jsx(Icon, { key: 'i', name: glyph, size: 10 }), judge?.vendor ? `judge · ${judge.vendor}` : 'judge']
  })
}

function arenaCallout(a) {
  const s = a.legacy ? null : ARENA_STATUS[a.status]
  const advisories = a.advisories || []
  if (!s?.callout && !advisories.length) return null
  const kind = s?.callout || 'advisory'
  return { kind, tone: kind === 'blocked' ? undefined : 'warn', text: advisories[0] || s.copy, title: advisories.length ? advisories.join('\n') : s.copy }
}

function ArenaStrip({ ctx, arena: a }) {
  const queryClient = useQueryClient()
  const [action, run] = useAction()
  const canEval = !!ctx && /^ar_[0-9a-f]+$/i.test(String(a.arena_id || ''))
  const reeval = () => run(async () => {
    const body = { arena_id: a.arena_id }
    if (typeof a.tenant === 'string' && TENANT_SLUG_RE.test(a.tenant)) body.tenant = a.tenant
    const res = await ctx.rest('/arenas/evaluate', { method: 'POST', body })
    invalidateKeys(queryClient, [['factonio', 'cards']])
    return res
  })
  const st = arenaStatusInfo(a)
  const cnt = arenaCounts(a)
  const judge = a.judge
  const judgeLabel = judge
    ? ` Judge ${judge.vendor || 'unknown'} ${judge.task_id ? judge.status || 'queued' : 'not spawned'}${judge.cross_vendor === true ? ', cross-vendor' : judge.cross_vendor === false ? ', same vendor as a candidate' : ''}.`
    : ' No judge yet.'
  const label = a.legacy
    ? `Arena ${a.arena_id}${a.target ? ` on ${a.target}` : ''}: rebuilt from cards. ${cnt.k} of ${cnt.n} candidate cards done.${judgeLabel}`
    : `Arena ${a.arena_id}${a.target ? ` on ${a.target}` : ''}: ${st.label}. ${cnt.k} of ${cnt.n} candidate artifacts ready.${judgeLabel}`
  const co = arenaCallout(a)
  return jsxs('div', {
    className: 'fc-sb-panel fc-sb-arena',
    role: 'group',
    'aria-label': label,
    'data-arena': a.arena_id,
    'data-status': a.status || (a.legacy ? 'legacy' : ''),
    children: [
      jsxs('div', {
        key: 'top',
        className: 'fc-sb-panel-top',
        children: [
          jsxs('span', {
            key: 'k',
            className: 'fc-sb-arena-k',
            title: a.title ? `Arena · ${a.title}` : 'Arena',
            children: [jsx(Icon, { key: 'i', name: 'arena', size: 11 }), jsx('span', { key: 'w', className: 'fc-sb-arena-word', children: 'arena' })]
          }),
          a.target ? jsx('span', { key: 't', className: 'fc-sb-arena-target', title: 'Arena target', children: a.target }) : null,
          jsx('span', { key: 's', className: 'fc-sb-tag', 'data-tone': st.tone, title: st.title, children: st.label }),
          jsx('span', { key: 'id', className: 'fc-sb-panel-id fc-sb-mono', title: a.arena_id, children: a.arena_id }),
          jsx('span', { key: 'n', className: 'fc-sb-panel-lanes', title: cnt.title, children: cnt.text }),
          canEval ? jsx(ReevalButton, { key: 're', label: 'arena', state: action, onRun: reeval }) : null
        ]
      }),
      jsx('div', {
        key: 'v',
        className: 'fc-sb-vendors',
        'aria-label': 'Candidates and judge',
        children: [
          ...a.candidates.map((c, i) => jsx(ArenaCandidateChip, { key: c.task_id || `c${i}`, cand: c })),
          jsx(ArenaJudgeChip, { key: 'judge', judge })
        ]
      }),
      co
        ? jsxs('div', {
            key: 'co',
            className: 'fc-sb-callout',
            'data-tone': co.tone,
            'data-kind': co.kind,
            role: 'note',
            children: [
              jsx(Icon, { key: 'i', name: 'attention' }),
              jsxs('span', { key: 'b', className: 'fc-sb-callout-body', title: co.title, children: [jsx('span', { key: 'k', className: 'fc-sb-kind', children: co.kind }), co.text] })
            ]
          })
        : null,
      canEval ? jsx(ReevalMessage, { key: 'msg', state: action, onRun: reeval, what: 'arena' }) : null
    ]
  })
}

function openExternalUrl(ctx, url) {
  const fallback = () => {
    try { globalThis.window?.open?.(url, '_blank', 'noopener,noreferrer') } catch { /* ignore */ }
  }
  if (typeof ctx?.os?.openExternal === 'function') {
    Promise.resolve()
      .then(() => ctx.os.openExternal(url))
      .then(ok => { if (ok === false) fallback() }, fallback)
  } else {
    fallback()
  }
}

async function copyText(ctx, text, what) {
  let ok = false
  try {
    if (typeof ctx?.os?.writeClipboard === 'function') ok = (await ctx.os.writeClipboard(text)) !== false
  } catch { ok = false }
  if (!ok) {
    try {
      const clip = globalThis.navigator?.clipboard
      if (typeof clip?.writeText === 'function') {
        await clip.writeText(text)
        ok = true
      }
    } catch { ok = false }
  }
  host.notify(ok ? { kind: 'info', message: `Copied ${what}` } : { kind: 'error', message: `Failed to copy ${what}` })
  return ok
}
const copyCardId = (ctx, id) => copyText(ctx, id, 'card ID')

// Icons — 12px viewBox, 1.5 stroke, currentColor (ICON table from preview.html).
const ICON = {
  check: [['path', { d: 'M2.5 6.3 5 8.7l4.5-5.2' }]],
  x: [['path', { d: 'M3.5 3.5l5 5M8.5 3.5l-5 5' }]],
  running: [['circle', { cx: 6, cy: 6, r: 4.6 }], ['circle', { cx: 6, cy: 6, r: 1.8, fill: 'currentColor', stroke: 'none' }]],
  queued: [['circle', { cx: 6, cy: 6, r: 4.6 }], ['path', { d: 'M6 3.6V6l1.6 1.1' }]],
  attention: [['path', { d: 'M6 1.6 10.8 10H1.2z' }], ['path', { d: 'M6 5v2.1M6 8.6v.1' }]],
  pending: [['circle', { cx: 6, cy: 6, r: 4.2 }]],
  skipped: [['circle', { cx: 6, cy: 6, r: 4.4, strokeDasharray: '1.6 1.9' }], ['path', { d: 'M4 6h4' }]],
  chev: [['path', { d: 'M4.5 2.8 7.7 6 4.5 9.2' }]],
  copy: [['rect', { x: 4.2, y: 4.2, width: 6, height: 6, rx: 1.2 }], ['path', { d: 'M7.8 4.2V2.9c0-.6-.5-1.1-1.1-1.1H2.9c-.6 0-1.1.5-1.1 1.1v3.8c0 .6.5 1.1 1.1 1.1h1.3' }]],
  ext: [['path', { d: 'M5 2.5H2.5v7h7V7' }], ['path', { d: 'M7 2.5h2.5V5M9.5 2.5 5.6 6.4' }]],
  dash: [['rect', { x: 1.8, y: 1.8, width: 3.6, height: 4.2, rx: 0.8 }], ['rect', { x: 6.6, y: 1.8, width: 3.6, height: 2.6, rx: 0.8 }], ['rect', { x: 6.6, y: 5.6, width: 3.6, height: 4.6, rx: 0.8 }], ['rect', { x: 1.8, y: 7.2, width: 3.6, height: 3, rx: 0.8 }]],
  thread: [['path', { d: 'M2 2.5h8v5.5H5.2L3 10V8H2z' }]],
  filter: [['path', { d: 'M1.8 2.5h8.4L7 6.3v3.4L5 10.5V6.3z' }]],
  inbox: [['path', { d: 'M1.8 6.8 3.2 2.5h5.6l1.4 4.3v2.7H1.8z' }], ['path', { d: 'M1.8 6.8h2.6l.6 1.2h2l.6-1.2h2.6' }]],
  trail: [['circle', { cx: 2.6, cy: 2.8, r: 1.1 }], ['circle', { cx: 2.6, cy: 9.2, r: 1.1 }], ['path', { d: 'M2.6 3.9v4.2M5.2 2.8h5M5.2 6h5M5.2 9.2h3.4' }]],
  person: [['circle', { cx: 6, cy: 4, r: 2 }], ['path', { d: 'M2.2 10.4c.4-2 1.9-3.2 3.8-3.2s3.4 1.2 3.8 3.2' }]],
  link: [['path', { d: 'M5 7 7 5' }], ['path', { d: 'M4.3 5.4 3.1 6.6a1.7 1.7 0 0 0 2.4 2.4l1.2-1.2M7.7 6.6l1.2-1.2a1.7 1.7 0 0 0-2.4-2.4L5.3 4.2' }]],
  retry: [['path', { d: 'M9.6 5.2A3.8 3.8 0 1 0 9 8' }], ['path', { d: 'M9.8 2.4v2.9H6.9' }]],
  panel: [['circle', { cx: 4.4, cy: 6, r: 2.7 }], ['circle', { cx: 7.6, cy: 6, r: 2.7 }]],
  split: [['path', { d: 'M6 10.2V6M6 6 2.9 2.4M6 6l3.1-3.6' }]],
  up: [['path', { d: 'M6 9.6V2.6M3.4 5.2 6 2.6l2.6 2.6' }]],
  down: [['path', { d: 'M6 2.4v7M3.4 6.8 6 9.4l2.6-2.6' }]],
  trend: [['path', { d: 'M1.8 8.6l2.7-2.7 1.9 1.9 3.8-3.8' }], ['path', { d: 'M7.6 4h2.6v2.6' }]],
  dot: [['circle', { cx: 6, cy: 6, r: 2, fill: 'currentColor', stroke: 'none' }]],
  spin: [['circle', { cx: 6, cy: 6, r: 4.2, strokeDasharray: '15 11' }]],
  arena: [['path', { d: 'M1.8 2.6 5 6 1.8 9.4' }], ['path', { d: 'M10.2 2.6 7 6l3.2 3.4' }]],
  eye: [['path', { d: 'M1.2 6S3 2.8 6 2.8 10.8 6 10.8 6 9 9.2 6 9.2 1.2 6 1.2 6z' }], ['circle', { cx: 6, cy: 6, r: 1.5 }]],
  shield: [['path', { d: 'M6 1.5 9.8 3v3c0 2.2-1.6 3.8-3.8 4.5C3.8 9.8 2.2 8.2 2.2 6V3z' }]],
  shieldOk: [['path', { d: 'M6 1.5 9.8 3v3c0 2.2-1.6 3.8-3.8 4.5C3.8 9.8 2.2 8.2 2.2 6V3z' }], ['path', { d: 'M4.4 6.1 5.5 7.2 7.6 4.9' }]],
  plus: [['path', { d: 'M6 2.4v7.2M2.4 6h7.2' }]],
  search: [['circle', { cx: 5.2, cy: 5.2, r: 3.3 }], ['path', { d: 'm7.7 7.7 2.5 2.5' }]],
  doc: [['path', { d: 'M3 1.6h4l2 2v6.8H3z' }], ['path', { d: 'M7 1.6v2h2M4.6 6h2.8M4.6 8h2.8' }]]
}

function Icon({ name, size = 12, className = '' }) {
  return jsx('svg', {
    className: `fc-sb-ico ${className}`.trim(),
    width: size,
    height: size,
    viewBox: '0 0 12 12',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.5,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
    'aria-hidden': 'true',
    focusable: 'false',
    children: (ICON[name] || []).map(([tag, attrs], i) => jsx(tag, { ...attrs, key: i }))
  })
}

function Seg({ label, items, value, onSelect, fit }) {
  return jsx('div', {
    className: fit ? 'fc-sb-seg fc-sb-seg--fit' : 'fc-sb-seg',
    role: 'group',
    'aria-label': label,
    children: items.map(([v, l]) =>
      jsx('button', {
        key: v,
        type: 'button',
        'data-value': v,
        'aria-pressed': String(value === v),
        onClick: () => onSelect(v),
        children: l
      })
    )
  })
}

function SidebarHeader({ n, scope, setScope, status, setStatus, phase, setPhase }) {
  const stat = (v, l) => {
    const hot = v === 'attention' && !!n && n.attention > 0
    return jsxs('button', {
      key: v,
      type: 'button',
      className: 'fc-sb-stat',
      'data-value': v,
      'aria-pressed': String(status === v),
      'data-hot': hot ? 'true' : undefined,
      title: l,
      onClick: () => setStatus(status === v ? 'all' : v),
      children: [
        jsx('span', { className: 'fc-sb-stat-n', children: n ? String(n[v]) : '–' }),
        jsx('span', { className: 'fc-sb-stat-l', children: l })
      ]
    })
  }
  return jsxs('div', {
    className: 'fc-sb-head',
    children: [
      jsxs('div', {
        className: 'fc-sb-titlebar',
        children: [
          jsx('h2', { className: 'fc-sb-title', children: 'Factonio' }),
          jsx('span', { className: 'fc-sb-count', title: 'Cards in scope', children: n ? String(n.all) : '…' }),
          jsx('span', { className: 'fc-sb-spacer' }),
          jsxs('button', {
            type: 'button',
            className: 'fc-sb-btn',
            'data-action': 'dashboard',
            title: 'Open the Factonio dashboard',
            onClick: () => host.navigate(ROUTE_PATH),
            children: [jsx(Icon, { name: 'dash' }), 'Dashboard']
          })
        ]
      }),
      jsx(Seg, { label: 'Scope', items: [['thread', 'This thread'], ['all', 'All sessions']], value: scope, onSelect: setScope }),
      jsxs('div', {
        className: 'fc-sb-stats',
        role: 'group',
        'aria-label': 'Filter by status',
        children: [stat('all', 'All'), stat('active', 'Active'), stat('attention', 'Attention'), stat('done', 'Done')]
      }),
      jsx(Seg, {
        label: 'Filter by phase',
        fit: true,
        items: [['all', 'All'], ['define', 'Define'], ['design', 'Design'], ['implement', 'Implement'], ['verify', 'Verify']],
        value: phase,
        onSelect: setPhase
      })
    ]
  })
}

function SidebarEmpty({ icon, title, text, cta, onCta }) {
  return jsxs('div', {
    className: 'fc-sb-empty',
    children: [
      jsx('span', { className: 'fc-sb-empty-ico', children: jsx(Icon, { name: icon, size: 16 }) }),
      jsx('h3', { children: title }),
      jsx('p', { children: text }),
      cta ? jsx('button', { type: 'button', className: 'fc-sb-btn fc-sb-btn--primary', onClick: onCta, children: cta }) : null
    ]
  })
}

function SidebarSkeleton() {
  const one = (w, i) => jsxs('div', {
    key: i,
    className: 'fc-sb-skel',
    'aria-hidden': 'true',
    children: [
      jsx('span', { style: { width: `${w}%` } }),
      jsx('span', { style: { width: '90%', height: '3px' } }),
      jsx('span', { style: { width: '70%' } }),
      jsx('span', { style: { width: '45%' } })
    ]
  })
  return [one(62, 0), one(48, 1), one(55, 2), jsx('span', { key: 'sr', className: 'fc-sb-sr', children: 'Loading cards' })]
}

function PrLink({ ctx, pr, className, title, children }) {
  const url = safePrUrl(pr)
  if (!url) return null
  return jsx('a', {
    className,
    href: url,
    target: '_blank',
    rel: 'noopener noreferrer',
    title,
    'data-action': 'open-pr',
    onClick: e => {
      e?.stopPropagation?.()
      e?.preventDefault?.()
      openExternalUrl(ctx, url)
    },
    children
  })
}

function Pipeline({ pipe }) {
  const stage = (k, label) => {
    const s = pipe[k]
    if (k !== 'verify') {
      return jsxs('li', {
        key: k,
        className: 'fc-sb-stage',
        'data-stage': k,
        'data-state': s.state,
        'aria-label': `${label}: ${s.word}`,
        children: [
          jsx('span', { className: 'fc-sb-bar', children: jsx('span', { 'data-state': s.state }) }),
          jsx('span', { className: 'fc-sb-stage-name', children: label }),
          jsxs('span', {
            className: `fc-sb-stage-word fc-sb-tone-${s.state}`,
            children: [jsx(Icon, { name: STATE_ICON[s.state], size: 10 }), jsx('b', { children: s.word })]
          })
        ]
      })
    }
    const sub = (node, l) => jsxs('span', {
      key: l,
      className: `fc-sb-sub fc-sb-tone-${node.state}`,
      title: `${l === 'BB' ? 'Black-box' : 'Diff'}: ${node.word}`,
      children: [
        jsx('span', { className: 'fc-sb-sub-l', children: l }),
        jsx(Icon, { name: node.state === 'attention' && node.word === 'rejected' ? 'x' : STATE_ICON[node.state], size: 10 })
      ]
    })
    return jsxs('li', {
      key: k,
      className: 'fc-sb-stage',
      'data-stage': k,
      'data-state': s.state,
      'aria-label': `Verify: diff ${pipe.diff.word}, black-box ${pipe.bb.word}`,
      children: [
        jsxs('span', {
          className: 'fc-sb-bar',
          children: [jsx('span', { 'data-state': pipe.diff.state }), jsx('span', { 'data-state': pipe.bb.state })]
        }),
        jsx('span', { className: 'fc-sb-stage-name', children: label }),
        jsxs('span', { className: 'fc-sb-stage-word', children: [sub(pipe.diff, 'Diff'), sub(pipe.bb, 'BB')] })
      ]
    })
  }
  return jsxs('ol', {
    className: 'fc-sb-pipe',
    'aria-label': 'Phase progress',
    children: [stage('define', 'Define'), stage('design', 'Design'), stage('implement', 'Implement'), stage('verify', 'Verify')]
  })
}

function panelRows(c, panel, row) {
  const lane = panel.lanes.find(l => l.cardId === c.id)
  const peers = panel.lanes.filter(l => l.cardId !== c.id)
  const mixed = mixedPhases(panel.lanes)
  const pos = lane ? panel.lanes.indexOf(lane) + 1 : null
  return [
    ...row('panel', 'Panel', [
      jsx('span', { key: 'id', className: 'fc-sb-mono', children: panel.id }),
      jsx('span', { key: 'c', className: 'fc-sb-tag', 'data-tone': PANEL_TONE[panel.consensus], title: panel.reason || undefined, children: panel.consensus })
    ]),
    ...(lane
      ? row('vendor', 'Vendor', [
          jsx(VendorChip, { key: 'v', lane, mixed }),
          jsx('span', { key: 'n', className: 'fc-sb-lane', children: `lane ${pos}/${panel.lanes.length}` })
        ])
      : []),
    ...(peers.length ? row('peers', 'Peers', peers.map(l => jsx(VendorChip, { key: l.cardId, lane: l, mixed }))) : []),
    ...(panel.reason ? row('missing', 'Missing', panel.reason, { className: 'fc-sb-mono' }) : [])
  ]
}

function modeRows(mode, row, onReport) {
  if (!mode?.kind) return []
  const out = row('mode', 'Mode', BADGED_MODES.has(mode.kind)
    ? [
        jsx(ModePill, { key: 'p', mode }),
        mode.readOnly ? jsx('span', { key: 'ro', className: 'fc-sb-tag', 'data-tone': 'warn', children: 'read-only' }) : null
      ]
    : [
        jsx('span', { key: 'k', className: 'fc-sb-mono', children: mode.kind }),
        mode.readOnly ? jsx('span', { key: 'ro', className: 'fc-sb-tag', 'data-tone': 'warn', children: 'read-only' }) : null
      ])
  if (mode.kind !== 'investigate') return out
  return [
    ...out,
    ...row('report', 'Report', [
      jsx('span', { key: 's', className: 'fc-sb-tag', 'data-tone': mode.reportExists ? 'pass' : undefined, children: mode.reportExists ? 'ready' : 'pending' }),
      mode.reportExists && onReport
        ? jsx('button', {
            key: 'n',
            type: 'button',
            className: 'fc-sb-linkbtn fc-sb-mono',
            'data-action': 'open-report',
            'aria-haspopup': 'dialog',
            title: 'Open investigation.md (read-only)',
            onClick: e => { e?.stopPropagation?.(); onReport() },
            children: 'investigation.md'
          })
        : mode.reportName ? jsx('span', { key: 'n', className: 'fc-sb-mono', title: mode.reportName, children: mode.reportName }) : null
    ])
  ]
}

function arenaRows(c, arena, row) {
  const id = barrierStr(c.arena_id)
  if (!id || (c.arena_role !== 'candidate' && c.arena_role !== 'judge')) return []
  const target = arena?.target || barrierStr(c.arena_target)
  const st = arena && !arena.legacy ? arenaStatusInfo(arena) : null
  const slot = barrierStr(c.arena_slot)
  const out = [
    ...row('arena', 'Arena', [
      jsx('span', { key: 'id', className: 'fc-sb-mono', title: id, children: id }),
      target ? jsx('span', { key: 't', className: 'fc-sb-arena-target', title: 'Arena target', children: target }) : null,
      st ? jsx('span', { key: 's', className: 'fc-sb-tag', 'data-tone': st.tone, title: st.title, children: st.label }) : null
    ]),
    ...row('role', 'Role', c.arena_role === 'judge' ? 'judge' : `candidate${slot ? ` · ${slot}` : ''}`, { className: 'fc-sb-mono' })
  ]
  if (!arena || arena.legacy) return out
  return [
    ...out,
    ...(arena.candidates.length ? row('candidates', 'Candidates', arena.candidates.map((x, i) => jsx(ArenaCandidateChip, { key: x.task_id || `c${i}`, cand: x }))) : []),
    ...row('judge', 'Judge', jsx(ArenaJudgeChip, { judge: arena.judge }))
  ]
}

function localTargetRows(c, publish, row) {
  const t = c.local_target
  if (!t || typeof t !== 'object' || c.phase === 'verify-diff' || c.phase === 'verify-blackbox') return []
  const branch = barrierStr(t.branch)
  if (!branch) return []
  const base = barrierStr(t.base_branch)
  const sha = barrierStr(String(t.head_sha ?? ''))
  const v = publish?.verification
  return [
    ...row('branch', 'Branch', `${branch}${base ? ` → ${base}` : ''}`, { className: 'fc-sb-mono' }),
    ...(sha ? row('head', 'Head', jsx('span', { className: 'fc-sb-mono', title: sha, children: sha.slice(0, 7) })) : []),
    ...row('verify', 'Verify', v ? jsx('span', { className: 'fc-sb-tag', 'data-tone': v.verified ? 'pass' : undefined, title: v.reason || undefined, children: v.label }) : 'unknown'),
    ...row('pr', 'PR', 'local only', { className: 'fc-sb-mono' })
  ]
}

function CardDrawer({ card: c, id, trail, panel, mode, arena, publish, onReport }) {
  const rework = c.phase === 'implement' && c.runs > 1
  const tone = verdictTone(c.verdict)
  const row = (k, label, value, extra) => [
    jsx('dt', { key: `${k}-t`, className: extra?.full ? 'fc-sb-full' : undefined, children: label }),
    jsx('dd', { key: `${k}-d`, 'data-field': k, className: extra?.className, children: value })
  ]
  return jsxs('dl', {
    className: 'fc-sb-drawer',
    id,
    // Clicks inside the drawer never toggle the card, so its text stays selectable.
    onClick: e => e?.stopPropagation?.(),
    children: [
      ...modeRows(mode, row, onReport),
      ...row('model', 'Model', formatPhaseModelLabel(c), { className: 'fc-sb-mono' }),
      ...localTargetRows(c, publish, row),
      ...arenaRows(c, arena, row),
      ...(panel ? panelRows(c, panel, row) : []),
      ...row('runs', 'Runs', [
        jsx('span', { key: 'n', className: 'fc-sb-mono', children: String(c.runs ?? 1) }),
        rework ? jsx('span', { key: 'rw', className: 'fc-sb-tag', 'data-tone': 'warn', children: `Rework · ${c.runs} runs` }) : null
      ]),
      ...row('active', 'Active', fmtDur(c.active_duration_seconds), { className: 'fc-sb-mono' }),
      ...row('wall', 'Wall', fmtDur(c.elapsed_seconds), { className: 'fc-sb-mono' }),
      ...row('created', 'Created', fmtTime(c.created_at), { className: 'fc-sb-mono' }),
      ...row('started', 'Started', fmtTime(c.started_at), { className: 'fc-sb-mono' }),
      ...row('completed', 'Completed', fmtTime(c.completed_at), { className: 'fc-sb-mono' }),
      ...(Array.isArray(c.parents) && c.parents.length
        ? row('parents', 'Parents', c.parents.map(p => jsx('span', { key: p, className: 'fc-sb-chip', children: p })))
        : []),
      ...(c.verdict ? row('verdict', 'Verdict', jsx('span', { className: 'fc-sb-tag', 'data-tone': tone || undefined, children: String(c.verdict) })) : []),
      ...(c.last_outcome ? row('outcome', 'Outcome', String(c.last_outcome).replace(/_/g, ' '), { className: 'fc-sb-mono' }) : []),
      ...CardDecisions({ card: c, trail }),
      ...(c.summary ? row('summary', 'Worker summary', String(c.summary), { full: true, className: 'fc-sb-full fc-sb-summary' }) : [])
    ]
  })
}

// `arena:<vendor>` / `arena:judge` tag for arena candidate and judge cards.
function cardArenaTag(c) {
  const id = barrierStr(c.arena_id)
  if (!id) return null
  if (c.arena_role === 'judge') return { text: 'arena:judge', title: `Arena judge · ${id}` }
  if (c.arena_role !== 'candidate') return null
  const slot = barrierStr(c.arena_slot)
  return { text: `arena:${barrierStr(c.arena_vendor) || 'unknown'}`, title: `Arena candidate ${slot ? `${slot} ` : ''}· ${id}` }
}

function CardRow({ ctx, card: c, wsCards, open, onToggle, trail, panel, mode, arena, publish, design, onPublish, onReport }) {
  const rej = isUnresolvedReject(c, wsCards)
  const inc = !rej && isUnresolvedInconclusive(c, wsCards)
  const attn = isAttention(c, wsCards)
  const tone = verdictTone(c.verdict)
  const rework = c.phase === 'implement' && c.runs > 1
  const dur = c.active_duration_seconds ?? c.elapsed_seconds
  const drawerId = `fc-sb-card-${domSafe(c.id)}`
  const title = stripTag(c.title)
  const callouts = cardCallouts(c, wsCards)
  const dotStatus = rej ? 'reject' : inc ? 'inconclusive' : c.status
  const repoName = c.pr?.repo ? String(c.pr.repo).split('/').pop() : ''
  const arenaTag = cardArenaTag(c)
  return jsxs('article', {
    className: 'fc-sb-card',
    'data-id': c.id,
    'data-open': String(open),
    'data-attn': attn && inc && c.status !== 'blocked' && c.status !== 'triage' ? 'warn' : String(attn),
    // Mouse convenience: click anywhere outside the drawer/footer toggles; the
    // footer chevron button is the keyboard-accessible toggle.
    onClick: () => {
      const sel = globalThis.getSelection?.()
      if (sel && String(sel.toString?.() || '')) return
      onToggle()
    },
    children: [
      jsxs('div', {
        className: 'fc-sb-card-top',
        children: [
          jsx('span', { className: 'fc-sb-dot', 'data-status': dotStatus, role: 'img', 'aria-label': `Status: ${rej ? 'rejected' : inc ? 'inconclusive' : c.status}` }),
          jsx('span', { className: 'fc-sb-tag', 'data-phase': c.phase, children: PHASE_LABEL[c.phase] || c.phase || 'other' }),
          arenaTag
            ? jsx('span', { className: 'fc-sb-vendor fc-sb-arena-tag', 'data-arena-role': c.arena_role, title: arenaTag.title, children: arenaTag.text })
            : c.panel_vendor ? jsx('span', { className: 'fc-sb-vendor', 'data-panel-vendor': String(c.panel_vendor), title: 'Panel lane vendor', children: String(c.panel_vendor) }) : null,
          c.verdict ? jsx('span', { className: 'fc-sb-tag', 'data-tone': tone || undefined, 'data-verdict': c.verdict, children: String(c.verdict) }) : null,
          rework && !open ? jsx('span', { className: 'fc-sb-tag', 'data-tone': 'warn', title: `${c.runs} runs`, children: 'rework' }) : null,
          jsx('span', { className: 'fc-sb-dur', title: `Active ${fmtDur(c.active_duration_seconds)} · Wall ${fmtDur(c.elapsed_seconds)}`, children: fmtDur(dur) })
        ]
      }),
      jsx('h4', { className: 'fc-sb-card-title', title: c.title || '', children: title }),
      ...callouts.map((co, i) => jsxs('div', {
        key: `co-${i}`,
        className: 'fc-sb-callout',
        'data-tone': co.tone,
        'data-kind': co.kind,
        role: 'note',
        children: [
          jsx(Icon, { name: co.tone === 'warn' ? 'queued' : 'attention' }),
          jsxs('span', { className: 'fc-sb-callout-body', children: [jsx('span', { className: 'fc-sb-kind', children: co.kind }), co.text] })
        ]
      })),
      design ? jsx(DesignApproval, { key: `design:${c.id}`, ctx, card: c, state: design }) : null,
      open ? jsx(CardDrawer, { key: 'drawer', card: c, id: drawerId, trail, panel, mode, arena, publish, onReport }) : null,
      jsxs('div', {
        className: 'fc-sb-card-foot',
        children: [
          publish?.eligible && onPublish
            ? jsxs('button', {
                key: 'pub',
                type: 'button',
                className: 'fc-sb-btn fc-sb-btn--primary fc-sb-pub',
                'data-action': 'publish-pr',
                'aria-haspopup': 'dialog',
                title: `Publish ${publish.target.branch} at ${shortSha(publish.target.head_sha)}`,
                onClick: e => {
                  e?.stopPropagation?.()
                  onPublish(c, publish)
                },
                children: [jsx(Icon, { key: 'i', name: 'up', size: 11 }), 'Publish PR']
              })
            : null,
          jsx(PrLink, {
            ctx,
            pr: c.pr,
            className: 'fc-sb-pr',
            title: c.pr ? `Open ${c.pr.repo}#${c.pr.number}` : '',
            children: [`${repoName}#${c.pr?.number ?? ''}`, jsx(Icon, { key: 'ext', name: 'ext', size: 10 })]
          }),
          jsxs('button', {
            type: 'button',
            className: 'fc-sb-idbtn',
            'data-action': 'copy-id',
            'aria-label': `Copy card ID ${c.id}`,
            title: 'Copy card ID',
            onClick: e => {
              e?.stopPropagation?.()
              return copyCardId(ctx, c.id)
            },
            children: [c.id, jsx(Icon, { name: 'copy', size: 11 })]
          }),
          jsx('button', {
            type: 'button',
            className: 'fc-sb-iconbtn',
            'data-action': 'toggle-card',
            'aria-expanded': String(open),
            'aria-controls': drawerId,
            'aria-label': `${open ? 'Hide' : 'Show'} details for ${title}`,
            onClick: e => {
              e?.stopPropagation?.()
              onToggle()
            },
            children: jsx(Icon, { name: 'chev', className: 'fc-sb-chev' })
          })
        ]
      })
    ]
  })
}

function DoneGroup({ ctx, wsKey, cards, wsCards, open, onToggle, cardOpen, toggleCard, trail, panelOf, mode, arenaOf, extraOf }) {
  const listId = `fc-sb-done-${domSafe(wsKey)}`
  return [
    jsxs('button', {
      key: 'done-toggle',
      type: 'button',
      className: 'fc-sb-done-toggle',
      'aria-expanded': String(open),
      'aria-controls': listId,
      onClick: onToggle,
      children: [
        jsx(Icon, { name: 'chev', className: 'fc-sb-chev' }),
        jsx('span', { className: 'fc-sb-tone-done', children: jsx(Icon, { name: 'check' }) }),
        jsxs('span', { children: [jsx('span', { className: 'fc-sb-mono', children: String(cards.length) }), ' completed'] }),
        jsx('span', { className: 'fc-sb-done-phases', children: cards.map(c => PHASE_LABEL[c.phase] || c.phase).join(' · ') })
      ]
    }),
    open
      ? jsx('div', {
          key: 'done-list',
          className: 'fc-sb-done-list',
          id: listId,
          children: cards.map(c => jsx(CardRow, { key: c.id, ctx, card: c, wsCards, open: !!cardOpen[c.id], onToggle: () => toggleCard(c.id), trail, panel: panelOf(c), mode, arena: arenaOf(c), ...extraOf(c) }))
        })
      : null
  ]
}

function Workstream({ ctx, ws, ui }) {
  const [modal, setModal] = useState(null)
  const key = ws.tenant || '__ungrouped'
  const bodyId = `fc-sb-ws-${domSafe(key)}`
  const wsTrail = ui.trail?.status === 'ready' ? ui.trail.byTenant.get(ws.tenant || '') || [] : []
  const panels = computeWorkstreamPanels(ws.wsCards, wsTrail)
  const trailRows = ui.trail?.status === 'ready' ? wsTrail : null
  // Cards waiting on a human (publish or design approval) keep their workstream open and stay out
  // of the collapsed done group.
  const needsHuman = c => (c.local_target ? !!publishEligibility(c, ws.wsCards, panels).eligible : false) ||
    (trailRows ? designApprovalState(c, ws.wsCards, trailRows) === 'awaiting' : false)
  const humanTurn = ws.wsCards.some(needsHuman)
  const open = ui.wsOpen[key] ?? (ws.state !== 'completed' || humanTurn)
  const doneN = ws.wsCards.filter(c => c.status === 'done').length
  const attnN = ws.wsCards.filter(c => isAttention(c, ws.wsCards)).length
  const prCard = [...ws.wsCards]
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
    .find(c => safePrUrl(c.pr))
  const glyph = { attention: 'attention', active: 'running', completed: 'check' }[ws.state]
  const tone = { attention: 'attention', active: 'running', completed: 'done' }[ws.state]
  const mode = workstreamMode(ws.tenant, ui.workstreams, ws.wsCards, wsTrail)
  const badged = BADGED_MODES.has(mode.kind)
  const investigate = mode.kind === 'investigate'
  const meta = [
    ws.name.repo ? jsx('span', { key: 'repo', className: 'fc-sb-ws-repo', children: ws.name.repo }) : null,
    ws.name.repo ? jsx('span', { key: 'd1', 'aria-hidden': 'true', children: '·' }) : null,
    jsx('span', { key: 'n', className: 'fc-sb-mono', children: `${doneN}/${ws.wsCards.length}` }),
    jsx('span', { key: 'done', children: 'done' }),
    attnN ? jsx('span', { key: 'd2', 'aria-hidden': 'true', children: '·' }) : null,
    attnN ? jsx('span', { key: 'attn', className: 'fc-sb-attn-text', children: `${attnN} attn` }) : null,
    !open && ws.state === 'completed' ? jsx('span', { key: 'd3', 'aria-hidden': 'true', children: '·' }) : null,
    !open && ws.state === 'completed'
      ? jsxs('span', {
          key: 'mini',
          className: 'fc-sb-mini',
          role: 'img',
          'aria-label': 'Define, Design, Implement and Verify all done',
          children: [jsx('span', {}), jsx('span', {}), jsx('span', {}), jsx('span', {})]
        })
      : null,
    !open && ws.state === 'completed' ? jsx(Icon, { key: 'ok', name: 'check', size: 10, className: 'fc-sb-tone-done' }) : null,
    investigate ? jsx('span', { key: 'd4', 'aria-hidden': 'true', children: '·' }) : null,
    investigate ? jsx(ReportIndicator, { key: 'report', mode, onOpen: ws.tenant ? () => setModal({ kind: 'report' }) : null }) : null
  ]
  const head = jsxs('div', {
    key: 'head',
    className: 'fc-sb-ws-head',
    children: [
      jsxs('button', {
        type: 'button',
        className: 'fc-sb-ws-toggle',
        'data-action': 'toggle-ws',
        'aria-expanded': String(open),
        'aria-controls': bodyId,
        title: ws.tenant || 'Ungrouped',
        onClick: () => ui.setWsOpen({ ...ui.wsOpen, [key]: !open }),
        children: [
          jsx(Icon, { name: 'chev', className: 'fc-sb-chev' }),
          jsx('span', { className: `fc-sb-tone-${tone}`, children: jsx(Icon, { name: glyph }) }),
          jsxs('span', {
            className: 'fc-sb-ws-text',
            children: [
              jsxs('span', {
                className: 'fc-sb-ws-labelrow',
                children: [
                  jsx('span', { key: 'l', className: 'fc-sb-ws-label', children: ws.name.slug }),
                  badged ? jsx(ModePill, { key: 'm', mode, suffix: true }) : null
                ]
              }),
              jsx('span', { className: 'fc-sb-ws-meta', children: meta })
            ]
          })
        ]
      }),
      prCard
        ? jsx(PrLink, { ctx, pr: prCard.pr, className: 'fc-sb-pr', title: `${prCard.pr.repo}#${prCard.pr.number}`, children: `#${prCard.pr.number}` })
        : null
    ]
  })
  const sectionProps = {
    className: 'fc-sb-ws',
    'data-state': ws.state,
    'data-tenant': ws.tenant || '',
    'aria-label': `${ws.name.slug} workstream, ${ws.state}${badged ? `, ${mode.kind} mode` : ''}${investigate ? `, read-only, report ${mode.reportExists ? 'ready' : 'pending'}` : ''}`,
    'data-mode': badged ? mode.kind : undefined
  }
  const modalEl = !modal
    ? null
    : modal.kind === 'report'
      ? jsx(ReportModal, { key: 'modal', ctx, tenant: ws.tenant, label: ws.name.slug, onClose: () => setModal(null) })
      : jsx(PublishModal, { key: 'modal', ctx, card: modal.card, target: modal.publish.target, verification: modal.publish.verification, onClose: () => setModal(null) })
  if (!open) return jsx('section', { ...sectionProps, children: [head, modalEl] })

  const extraOf = c => ({
    publish: c.local_target ? publishEligibility(c, ws.wsCards, panels) : null,
    design: trailRows ? designApprovalState(c, ws.wsCards, trailRows) : null,
    onPublish: (card, publish) => setModal({ kind: 'publish', card, publish }),
    onReport: investigate && ws.tenant ? () => setModal({ kind: 'report' }) : null
  })
  const sorted = sortCards(ws.visible, ws.wsCards)
  let main = sorted
  let doneCards = []
  if (ws.state !== 'completed') {
    main = sorted.filter(c => !isCollapsibleDone(c, ws.wsCards) || needsHuman(c))
    doneCards = sorted.filter(c => isCollapsibleDone(c, ws.wsCards) && !needsHuman(c))
  }
  const doneOpen = ui.doneOpen[key] ?? (ui.status === 'done')
  const toggleCard = id => ui.setCardOpen({ ...ui.cardOpen, [id]: !ui.cardOpen[id] })
  const panelOf = c => (c.panel_id ? panels.find(p => p.id === String(c.panel_id)) || null : null)
  const arenas = computeWorkstreamArenas(ws.tenant, ws.wsCards, ui.arenas)
  const arenaOf = c => (c.arena_id && c.arena_role ? arenas.find(a => a.arena_id === String(c.arena_id)) || null : null)
  return jsxs('section', {
    ...sectionProps,
    children: [
      head,
      jsxs('div', {
        key: 'body',
        id: bodyId,
        children: [
          jsx(Pipeline, { pipe: computePipelineStages(ws.wsCards) }),
          arenas.length ? jsx(ArenaStrip, { key: 'arena', ctx, arena: arenas[0] }) : null,
          panels.length ? jsx(PanelStrip, { key: 'panel', ctx, panel: panels[0], tenant: ws.tenant }) : null,
          jsxs('div', {
            className: 'fc-sb-ws-cards',
            children: [
              ...main.map(c => jsx(CardRow, { key: c.id, ctx, card: c, wsCards: ws.wsCards, open: !!ui.cardOpen[c.id], onToggle: () => toggleCard(c.id), trail: ui.trail, panel: panelOf(c), mode, arena: arenaOf(c), ...extraOf(c) })),
              doneCards.length
                ? jsx(DoneGroup, {
                    key: 'done',
                    ctx,
                    wsKey: key,
                    cards: doneCards,
                    wsCards: ws.wsCards,
                    open: doneOpen,
                    onToggle: () => ui.setDoneOpen({ ...ui.doneOpen, [key]: !doneOpen }),
                    cardOpen: ui.cardOpen,
                    toggleCard,
                    trail: ui.trail,
                    panelOf,
                    mode,
                    arenaOf,
                    extraOf
                  })
                : null,
              jsx(TrailStrip, {
                key: 'trail',
                tenant: ws.tenant || '',
                trail: ui.trail,
                open: !!ui.trailOpen[key],
                onToggle: () => ui.setTrailOpen({ ...ui.trailOpen, [key]: !ui.trailOpen[key] })
              })
            ]
          })
        ]
      }),
      modalEl
    ]
  })
}

// Scope + filters persist through the curated ctx.storage door (feature-detected).
function usePersistedChoice(ctx, key, allowed, fallback) {
  const storage = ctx?.storage
  const [value, setValue] = useState(() => {
    let v = fallback
    try {
      if (typeof storage?.get === 'function') v = storage.get(key, fallback)
    } catch {
      v = fallback
    }
    return v === fallback || allowed.includes(v) ? v : fallback
  })
  const update = next => {
    setValue(next)
    try {
      if (typeof storage?.set === 'function') storage.set(key, next)
    } catch { /* storage is best-effort */ }
  }
  return [value, update]
}

function ThreadCardsPane({ ctx, visible = true }) {
  const sessionId = useValue(host.state.focusedStoredSessionId) || null
  const [scopeChoice, setScope] = usePersistedChoice(ctx, 'sidebar.scope', SCOPES, null)
  const [status, setStatusChoice] = usePersistedChoice(ctx, 'sidebar.status', STATUS_FILTERS, 'all')
  const [phase, setPhase] = usePersistedChoice(ctx, 'sidebar.phase', PHASE_FILTERS, 'all')
  const [wsOpen, setWsOpen] = useState({})
  const [doneOpenByMode, setDoneOpenByMode] = useState({ other: {}, done: {} })
  const [cardOpen, setCardOpen] = useState({})
  // "N completed" overrides are partitioned by filter mode (Done vs everything else), and the
  // Done partition is reset on entry, so entering Done always auto-expands completed cards
  // while returning to All/Active/Attention restores the user's earlier choices there.
  const doneMode = status === 'done' ? 'done' : 'other'
  const doneOpen = doneOpenByMode[doneMode] || {}
  const setDoneOpen = next => setDoneOpenByMode({ ...doneOpenByMode, [doneMode]: next })
  const setStatus = next => {
    if (next === 'done' && status !== 'done') setDoneOpenByMode({ ...doneOpenByMode, done: {} })
    setStatusChoice(next)
  }

  // Initial scope follows focus; an explicit user choice sticks even when focus changes.
  const scope = scopeChoice || (sessionId ? 'thread' : 'all')
  const thread = scope === 'thread'
  const enabled = !thread || !!sessionId
  const query = useQuery({
    queryKey: ['factonio', 'cards', thread ? (sessionId || '__unfocused__') : 'all'],
    queryFn: () => ctx.rest(thread ? `/cards?session_id=${encodeURIComponent(sessionId)}` : '/cards'),
    enabled,
    refetchInterval: visible ? CARDS_POLL_MS : false
  })
  const [trailOpen, setTrailOpen] = useState({})
  const trailQuery = useQuery({
    queryKey: TRAIL_QUERY_KEY,
    queryFn: () => ctx.rest(`/trail?limit=${TRAIL_LIMIT}`),
    refetchInterval: visible ? CARDS_POLL_MS : false
  })
  const trail = sidebarTrail(trailQuery, useQueryClient())

  const data = enabled ? query.data : undefined
  const scoped = Array.isArray(data?.cards) ? data.cards : []
  const isError = enabled && !!query.isError
  const loading = enabled && data == null && !isError
  const byTenant = groupBy(scoped, c => c.tenant || '')
  const n = loading ? null : statusCounts(scoped, byTenant)
  const clearFilters = () => {
    setStatus('all')
    setPhase('all')
  }

  let body
  if (thread && !sessionId) {
    body = jsx(SidebarEmpty, {
      icon: 'thread',
      title: 'No thread in focus',
      text: 'Open a chat thread to see its Factonio cards, or browse every session.',
      cta: 'Switch to All sessions',
      onCta: () => setScope('all')
    })
  } else if (loading) {
    body = jsx(SidebarSkeleton, {})
  } else if (!scoped.length && thread) {
    body = jsx(SidebarEmpty, {
      icon: 'inbox',
      title: 'No cards in this thread',
      text: 'Factonio hasn’t created cards for this thread yet.',
      cta: 'Show All sessions',
      onCta: () => setScope('all')
    })
  } else if (!scoped.length) {
    body = jsx(SidebarEmpty, { icon: 'inbox', title: 'No Factonio cards yet', text: 'Cards appear here once Factonio starts a workstream.' })
  } else {
    const visibleCards = filterCards(scoped, byTenant, status, phase)
    if (!visibleCards.length) {
      const s = { all: '', active: 'Active', attention: 'Attention', done: 'Done' }[status] || ''
      const p = phase === 'all' ? '' : phase[0].toUpperCase() + phase.slice(1)
      body = jsx(SidebarEmpty, {
        icon: 'filter',
        title: 'No cards match these filters',
        text: `${[s, p].filter(Boolean).join(' · ')} hides all ${scoped.length} cards in this scope.`,
        cta: 'Clear filters',
        onCta: clearFilters
      })
    } else {
      const repos = [...new Set(scoped.filter(c => c.pr?.repo).map(c => c.pr.repo))]
      const ui = { status, wsOpen, setWsOpen, doneOpen, setDoneOpen, cardOpen, setCardOpen, trail, trailOpen, setTrailOpen, workstreams: data?.workstreams, arenas: data?.arenas }
      body = groupWorkstreams(byTenant, visibleCards, repos).map(ws =>
        jsx(Workstream, { key: ws.tenant || '__ungrouped', ctx, ws, ui })
      )
    }
  }

  let banner = null
  if (isError) {
    const updated = Number(query.dataUpdatedAt) || 0
    const mins = updated ? Math.max(0, Math.round((Date.now() - updated) / 60000)) : null
    const text = data != null
      ? `Couldn’t refresh cards. Showing results from ${mins == null ? 'earlier' : `${mins} min ago`}; retrying every 15s.`
      : 'Couldn’t refresh cards; retrying every 15s.'
    banner = jsxs('div', {
      className: 'fc-sb-banner',
      role: 'status',
      children: [jsx(Icon, { name: 'attention' }), jsx('span', { children: text })]
    })
  }

  return jsxs('div', {
    className: 'fc-sb',
    'data-scope': scope,
    'data-visible': String(!!visible),
    children: [
      jsx(SidebarHeader, { n, scope, setScope, status, setStatus, phase, setPhase }),
      loading ? jsx('div', { className: 'fc-sb-progress', role: 'progressbar', 'aria-label': 'Loading cards' }) : null,
      banner,
      jsx('div', { className: 'fc-sb-body', 'aria-busy': String(loading), children: body })
    ]
  })
}

// Hook-safe visibility wrapper: only mounted when host.paneVisibility exists, so
// ThreadCardsPane's hook order never depends on the host version.
function LiveVisibleThreadCardsPane({ ctx }) {
  const visible = useValue(host.paneVisibility(PANE_ID))
  return jsx(ThreadCardsPane, { ctx, visible: visible === true })
}

function FactonioSidebar({ ctx }) {
  return typeof host.paneVisibility === 'function'
    ? jsx(LiveVisibleThreadCardsPane, { ctx })
    : jsx(ThreadCardsPane, { ctx, visible: true })
}

function StatusPill({ ctx }) {
  const query = useQuery({
    queryKey: ['factonio', 'status'],
    queryFn: () => ctx.rest('/status'),
    refetchInterval: 20000
  })
  const c = query.data?.counts || { running: 0, blocked: 0, triage: 0 }
  const active = (c.running || 0) + (c.blocked || 0) + (c.triage || 0)
  const dot = (c.blocked || c.triage) ? 'fc-dot fc-dot-red' : (c.running ? 'fc-dot fc-dot-green' : 'fc-dot fc-dot-muted')
  return jsxs('button', {
    type: 'button',
    className: 'fc-pill',
    onClick: revealSidebar,
    title: 'Show Factonio sidebar',
    children: [
      jsx('span', { className: dot }),
      jsx('span', { children: active ? `Factonio ${c.running} run · ${c.blocked + c.triage} attn` : 'Factonio idle' })
    ]
  })
}

function YourTurnCard({ attrs, sessionId }) {
  const [done, setDone] = useState(false)
  const step = attrs?.do || 'Complete manual step'
  const where = attrs?.where || ''
  const cmd = attrs?.cmd || ''

  const copyCmd = () => {
    if (!cmd) return
    navigator.clipboard.writeText(cmd).then(
      () => host.notify({ kind: 'info', message: 'Copied command' }),
      () => host.notify({ kind: 'error', message: 'Failed to copy command' })
    )
  }

  const markDone = () => {
    setDone(true)
    const msg = `Done: ${step}.`
    if (sessionId !== undefined) host.composer.submit(sessionId, msg)
    else host.composer.submit(null, msg)
  }

  return jsxs('div', {
    className: 'fc-step',
    children: [
      jsxs('div', {
        style: { display: 'flex', justifyContent: 'space-between', gap: '8px' },
        children: [
          jsx('strong', { children: step }),
          where ? jsx('span', { className: 'fc-badge', children: where }) : null
        ]
      }),
      cmd ? jsx('pre', { className: 'fc-step-cmd', children: cmd }) : null,
      jsxs('div', {
        className: 'fc-step-actions',
        children: [
          cmd ? jsx('button', { type: 'button', className: 'fc-btn', onClick: copyCmd, children: 'Copy' }) : null,
          jsx('button', {
            type: 'button',
            className: 'fc-btn',
            disabled: done,
            onClick: markDone,
            children: done ? 'Done' : 'Mark Done'
          })
        ]
      })
    ]
  })
}

// Pure helpers exposed for clean-room tests; the host only consumes the default export.
export {
  validateRepoDraft, repoDraftChanges, seedRepoDraft, describeSaveError, verdictTone, computeWorkstreamPanels, sparkPoints, sparkPath,
  workstreamMode, computeWorkstreamArenas, evidencePills, summarizeHarnessBadge,
  isRoutineDecision, isAttentionDecision, trailTier, decisionMatchesQuery, stripHoldout, foldRoutineDecisions, groupTrailRows,
  buildSupersessionChains, summarizeTrailAudit, scanEvidence, validateDecisionDraft, humanDecisionBody, trailPath,
  safeMarkdownHref, parseInlineMarkdown, parseMarkdownBlocks, reportView, localVerification, publishEligibility,
  publishRequestBody, formatHarnessAudit, designApprovalState
}

export default {
  id: 'factonio',
  name: 'Factonio',
  register(ctx) {
    const style = document.createElement('style')
    style.textContent = STYLES
    document.head.appendChild(style)
    ctx.onDispose(() => style.remove())

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE_PATH },
        render: () => jsx(FactonioPage, { ctx })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 52,
        data: { codicon: 'circuit-board', label: 'Factonio', path: ROUTE_PATH }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'factonio.open',
          label: 'Open Factonio',
          keywords: ['factonio', 'factory', 'metrics', 'pipeline', 'verify'],
          run: () => host.navigate(ROUTE_PATH)
        }
      },
      {
        id: 'show-pane',
        area: PALETTE_AREA,
        data: {
          id: 'factonio.showPane',
          label: 'Show Factonio Sidebar',
          keywords: ['factonio', 'sidebar', 'pane', 'cards', 'workstreams'],
          run: revealSidebar
        }
      },
      {
        id: 'pane',
        area: PANES_AREA,
        title: 'Factonio',
        data: {
          placement: 'right',
          width: '280px',
          collapsible: true,
          hideOnly: true,
          dock: { pane: 'files', pos: 'center', enforce: true }
        },
        render: () => jsx(FactonioSidebar, { ctx })
      },
      {
        id: 'status-pill',
        area: STATUSBAR_AREAS.right,
        order: 72,
        render: () => jsx(StatusPill, { ctx })
      },
      {
        id: 'your-turn',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: { name: 'your-turn', render: props => jsx(YourTurnCard, { ...props }) }
      }
    ])

    // Settings → Plugins → Factonio landing page. Feature-detected for older
    // hosts; config_schema is auto-folded beneath it as "Agent settings".
    ctx.registerSettingsPage?.({
      id: 'settings',
      title: 'Factonio',
      icon: 'circuit-board',
      render: () => jsx(FactonioSettingsPage, { ctx })
    })
  }
}
