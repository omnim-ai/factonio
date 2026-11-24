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
.fc-warn { border: 1px solid rgba(245, 158, 11, 0.45); background: rgba(245, 158, 11, 0.09); border-radius: 8px; padding: 10px 12px; margin-bottom: 16px; font-size: 12px; }
.fc-pill { display: inline-flex; align-items: center; gap: 6px; border: 0; background: transparent; color: var(--ui-text-secondary); font-size: 11px; padding: 2px 6px; border-radius: 4px; cursor: pointer; font-variant-numeric: tabular-nums; }
.fc-pill:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary); }
.fc-dot { width: 7px; height: 7px; border-radius: 999px; display: inline-block; }
.fc-dot-green { background: #34d399; }
.fc-dot-amber { background: #fbbf24; }
.fc-dot-red { background: #f87171; }
.fc-dot-muted { background: var(--ui-text-tertiary); }
.fc-badge { font-size: 10px; padding: 1px 6px; border-radius: 999px; border: 1px solid var(--ui-stroke-secondary); color: var(--ui-text-secondary); }
.fc-step { border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; padding: 10px 12px; margin: 8px 0; display: flex; flex-direction: column; gap: 8px; font-size: 12px; }
.fc-step-cmd { background: var(--ui-bg-elevated, rgba(127,127,127,0.08)); padding: 6px 8px; border-radius: 5px; font-family: var(--dt-font-mono, ui-monospace, monospace); font-size: 11px; overflow-x: auto; }
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
.fc-repos { table-layout: fixed; }
.fc-repos th { white-space: normal; line-height: 1.25; vertical-align: bottom; }
.fc-repos td { vertical-align: middle; }
.fc-repos tbody tr:not(.fc-repo-ed):hover > td { background: var(--ui-row-hover-background); }
.fc-col-repo { width: 27%; } .fc-col-branch { width: 12%; } .fc-col-merge { width: 11%; } .fc-col-rounds { width: 12%; }
.fc-col-prov { width: 15%; } .fc-col-appr { width: 14%; } .fc-col-act { width: 9%; }
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
.fc-ed { display: flex; flex-direction: column; gap: 12px; margin: 0; padding: 2px 14px 14px 16px; border: 0; min-width: 0; }
.fc-ed-id { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 11px; color: var(--ui-text-secondary); }
.fc-ed-id .fc-mono { color: var(--ui-text-primary); }
.fc-ed-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 16px; border: 0; margin: 0; padding: 0; min-width: 0; }
@container (max-width: 640px) { .fc-ed-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
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
  --fc-sb-warn: #f59e0b;
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
const DESIGN_PROVIDER_OPTIONS = ['auto', 'claude-design', 'hermes', 'off']
const DESIGN_APPROVAL_OPTIONS = ['ask', 'auto']
const REPO_FIELD_ORDER = ['default_branch', 'merge_policy', 'max_fix_rounds', 'design_provider', 'design_approval']
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

  const rounds = d.max_fix_rounds
  let roundsOk
  if (typeof rounds === 'number') roundsOk = Number.isInteger(rounds) && rounds >= 1
  else if (typeof rounds === 'string') roundsOk = /^[0-9]+$/.test(rounds.trim()) && Number(rounds.trim()) >= 1
  else roundsOk = false
  if (!roundsOk) errors.max_fix_rounds = 'Must be a whole number, 1 or more.'
  return errors
}

function seedRepoDraft(repo) {
  return {
    default_branch: String(repo.default_branch ?? ''),
    merge_policy: String(repo.merge_policy ?? 'auto'),
    max_fix_rounds: repo.max_fix_rounds == null ? '' : String(repo.max_fix_rounds),
    design_provider: repo.design_provider ? String(repo.design_provider) : '',
    design_approval: repo.design_approval ? String(repo.design_approval) : ''
  }
}

// The PATCH body: only fields whose (normalized) draft value differs from the repo.
function repoDraftChanges(draft, repo) {
  const body = {}
  const branch = String(draft.default_branch ?? '').trim()
  if (branch !== String(repo.default_branch ?? '')) body.default_branch = branch
  if (draft.merge_policy !== String(repo.merge_policy ?? '')) body.merge_policy = draft.merge_policy
  const rounds = String(draft.max_fix_rounds ?? '').trim()
  if (rounds !== String(repo.max_fix_rounds ?? '')) body.max_fix_rounds = Number(rounds)
  for (const k of ['design_provider', 'design_approval']) {
    const next = draft[k] || null
    if (next !== (repo[k] || null)) body[k] = next
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
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) detail = detail.detail ?? detail.error ?? detail.message ?? ''
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

function RepoRow({ repo: r, defaults, editing, busy, editorId, onEdit, buttonRef }) {
  return jsxs('tr', {
    'data-slug': r.slug,
    'data-editing': editing ? 'true' : undefined,
    children: [
      jsxs('td', {
        children: [
          jsx('span', { className: 'fc-trunc fc-repo-name', title: r.repo || r.slug, children: r.repo || r.slug }),
          jsx('span', { className: 'fc-trunc fc-repo-path', title: r.path || '', children: r.path || '—' })
        ]
      }),
      jsx('td', { children: jsx('span', { className: 'fc-trunc fc-mono', title: r.default_branch || '', children: r.default_branch || '—' }) }),
      jsx('td', { children: r.merge_policy ?? '—' }),
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
        className: 'fc-act',
        children: jsx('button', {
          type: 'button',
          className: 'fc-btn',
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
  const [draft, setDraft] = useState(() => seedRepoDraft(repo))
  const [errors, setErrors] = useState({})
  const [apiError, setApiError] = useState(null)
  const branchRef = useRef(null)
  const roundsRef = useRef(null)

  // Focus moves to the Default branch input when the editor opens.
  useEffect(() => {
    branchRef.current?.focus?.()
  }, [])

  const changes = repoDraftChanges(draft, repo)
  const count = Object.keys(changes).length
  const errorCount = Object.keys(errors).length
  const fid = name => `${editorId}-${name}`

  const setField = (name, value) => {
    setDraft(prev => ({ ...prev, [name]: value }))
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
  const select = (options, inheritLabel) => props => jsx('select', {
    ...props,
    className: 'fc-input',
    children: [
      inheritLabel ? jsx('option', { key: '__inherit', value: '', children: inheritLabel }) : null,
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
      colSpan: 7,
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
  const editButtons = useRef({})
  const focusAfterClose = useRef(null)

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
  const editorIdFor = slug => `fc-repo-ed-${domSafe(slug)}`
  const editingSlug = repos.some(r => r.slug === editing) ? editing : null
  // `auto` resolves the same for every repo (a host capability probe), so any
  // repo currently on `auto` tells us what inheriting/choosing `auto` resolves to.
  const autoResolved = repos.find(r => r.effective_design_provider === 'auto' && r.resolved_design_provider)?.resolved_design_provider || null

  const rows = []
  for (const r of repos) {
    const open = editingSlug === r.slug
    rows.push(jsx(RepoRow, {
      key: `row:${r.slug}`,
      repo: r,
      defaults,
      editing: open,
      busy: saving,
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
                  jsx('span', { className: 'fc-mono', children: defaults.design_approval || 'ask' })
                ]
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
      jsxs('table', {
        className: 'fc-table fc-repos',
        children: [
          jsxs('colgroup', {
            children: ['fc-col-repo', 'fc-col-branch', 'fc-col-merge', 'fc-col-rounds', 'fc-col-prov', 'fc-col-appr', 'fc-col-act'].map(col)
          }),
          jsx('thead', {
            children: jsxs('tr', {
              children: [
                jsxs('th', { scope: 'col', children: ['Repository', jsx('span', { className: 'fc-th-sub', children: 'Local path' })] }),
                jsx('th', { scope: 'col', children: 'Branch' }),
                jsx('th', { scope: 'col', children: 'Merge policy' }),
                jsx('th', { scope: 'col', className: 'fc-num', children: 'Max fix rounds' }),
                jsx('th', { scope: 'col', children: 'Design provider' }),
                jsx('th', { scope: 'col', children: 'Design approval' }),
                jsx('th', { scope: 'col', className: 'fc-act', children: jsx('span', { className: 'fc-sr', children: 'Actions' }) })
              ]
            })
          }),
          jsx('tbody', { children: rows })
        ]
      })
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
            className: 'fc-grid',
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
              })
            ]
          }),
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

// 'pass' | 'reject' | null — normalizes the API verdict for tones and resolution.
function verdictTone(v) {
  const s = String(v || '').toLowerCase()
  if (!s) return null
  if (REJECT_VERDICTS.has(s)) return 'reject'
  if (s === 'pass') return 'pass'
  return null
}

// A reject is resolved once a later card of the same verify phase in the same tenant passed.
function isUnresolvedReject(c, wsCards) {
  if (verdictTone(c.verdict) !== 'reject') return false
  return !wsCards.some(o => o.phase === c.phase && verdictTone(o.verdict) === 'pass' && (o.created_at || 0) > (c.created_at || 0))
}
const isAttention = (c, wsCards) => c.status === 'blocked' || c.status === 'triage' || isUnresolvedReject(c, wsCards)
const isCollapsibleDone = (c, wsCards) => c.status === 'done' && !isUnresolvedReject(c, wsCards)

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
function stageWord(state, cs) {
  if (state !== 'attention') return state
  if (cs.some(c => c.status === 'blocked')) return 'blocked'
  if (cs.some(c => c.status === 'triage')) return 'triage'
  return 'rejected'
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
    const last = latestCard(wsCards.filter(c => c.phase === ph))
    const cs = last ? [last] : []
    const state = stageState(cs, wsCards)
    return { state, word: stageWord(state, cs) }
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

async function copyCardId(ctx, id) {
  let ok = false
  try {
    if (typeof ctx?.os?.writeClipboard === 'function') ok = (await ctx.os.writeClipboard(id)) !== false
  } catch { ok = false }
  if (!ok) {
    try {
      const clip = globalThis.navigator?.clipboard
      if (typeof clip?.writeText === 'function') {
        await clip.writeText(id)
        ok = true
      }
    } catch { ok = false }
  }
  host.notify(ok ? { kind: 'info', message: 'Copied card ID' } : { kind: 'error', message: 'Failed to copy card ID' })
  return ok
}

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
  inbox: [['path', { d: 'M1.8 6.8 3.2 2.5h5.6l1.4 4.3v2.7H1.8z' }], ['path', { d: 'M1.8 6.8h2.6l.6 1.2h2l.6-1.2h2.6' }]]
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

function CardDrawer({ card: c, id }) {
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
      ...row('model', 'Model', formatPhaseModelLabel(c), { className: 'fc-sb-mono' }),
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
      ...(c.summary ? row('summary', 'Worker summary', String(c.summary), { full: true, className: 'fc-sb-full fc-sb-summary' }) : [])
    ]
  })
}

function CardRow({ ctx, card: c, wsCards, open, onToggle }) {
  const rej = isUnresolvedReject(c, wsCards)
  const attn = isAttention(c, wsCards)
  const tone = verdictTone(c.verdict)
  const rework = c.phase === 'implement' && c.runs > 1
  const dur = c.active_duration_seconds ?? c.elapsed_seconds
  const drawerId = `fc-sb-card-${domSafe(c.id)}`
  const title = stripTag(c.title)
  const callouts = cardCallouts(c, wsCards)
  const dotStatus = rej ? 'reject' : c.status
  const repoName = c.pr?.repo ? String(c.pr.repo).split('/').pop() : ''
  return jsxs('article', {
    className: 'fc-sb-card',
    'data-id': c.id,
    'data-open': String(open),
    'data-attn': String(attn),
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
          jsx('span', { className: 'fc-sb-dot', 'data-status': dotStatus, role: 'img', 'aria-label': `Status: ${rej ? 'rejected' : c.status}` }),
          jsx('span', { className: 'fc-sb-tag', 'data-phase': c.phase, children: PHASE_LABEL[c.phase] || c.phase || 'other' }),
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
      open ? jsx(CardDrawer, { key: 'drawer', card: c, id: drawerId }) : null,
      jsxs('div', {
        className: 'fc-sb-card-foot',
        children: [
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

function DoneGroup({ ctx, wsKey, cards, wsCards, open, onToggle, cardOpen, toggleCard }) {
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
          children: cards.map(c => jsx(CardRow, { key: c.id, ctx, card: c, wsCards, open: !!cardOpen[c.id], onToggle: () => toggleCard(c.id) }))
        })
      : null
  ]
}

function Workstream({ ctx, ws, ui }) {
  const key = ws.tenant || '__ungrouped'
  const bodyId = `fc-sb-ws-${domSafe(key)}`
  const open = ui.wsOpen[key] ?? (ws.state !== 'completed')
  const doneN = ws.wsCards.filter(c => c.status === 'done').length
  const attnN = ws.wsCards.filter(c => isAttention(c, ws.wsCards)).length
  const prCard = [...ws.wsCards]
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
    .find(c => safePrUrl(c.pr))
  const glyph = { attention: 'attention', active: 'running', completed: 'check' }[ws.state]
  const tone = { attention: 'attention', active: 'running', completed: 'done' }[ws.state]
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
    !open && ws.state === 'completed' ? jsx(Icon, { key: 'ok', name: 'check', size: 10, className: 'fc-sb-tone-done' }) : null
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
              jsx('span', { className: 'fc-sb-ws-label', children: ws.name.slug }),
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
    'aria-label': `${ws.name.slug} workstream, ${ws.state}`
  }
  if (!open) return jsx('section', { ...sectionProps, children: head })

  const sorted = sortCards(ws.visible, ws.wsCards)
  let main = sorted
  let doneCards = []
  if (ws.state !== 'completed') {
    main = sorted.filter(c => !isCollapsibleDone(c, ws.wsCards))
    doneCards = sorted.filter(c => isCollapsibleDone(c, ws.wsCards))
  }
  const doneOpen = ui.doneOpen[key] ?? (ui.status === 'done')
  const toggleCard = id => ui.setCardOpen({ ...ui.cardOpen, [id]: !ui.cardOpen[id] })
  return jsxs('section', {
    ...sectionProps,
    children: [
      head,
      jsxs('div', {
        key: 'body',
        id: bodyId,
        children: [
          jsx(Pipeline, { pipe: computePipelineStages(ws.wsCards) }),
          jsxs('div', {
            className: 'fc-sb-ws-cards',
            children: [
              ...main.map(c => jsx(CardRow, { key: c.id, ctx, card: c, wsCards: ws.wsCards, open: !!ui.cardOpen[c.id], onToggle: () => toggleCard(c.id) })),
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
                    toggleCard
                  })
                : null
            ]
          })
        ]
      })
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
      const ui = { status, wsOpen, setWsOpen, doneOpen, setDoneOpen, cardOpen, setCardOpen }
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
export { validateRepoDraft, repoDraftChanges, describeSaveError }

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
