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
.fc-col-repo { width: 22%; } .fc-col-branch { width: 10%; } .fc-col-merge { width: 10%; } .fc-col-rounds { width: 9%; }
.fc-col-prov { width: 14%; } .fc-col-appr { width: 12%; } .fc-col-rigor { width: 15%; } .fc-col-act { width: 8%; }
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
.fc-trail-sum { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.fc-trail-stat { display: inline-flex; align-items: baseline; gap: 5px; height: 24px; box-sizing: border-box; padding: 0 8px; border: 1px solid var(--ui-stroke-tertiary); border-radius: 6px; background: transparent; color: var(--ui-text-secondary); font: inherit; font-size: 11.5px; line-height: 22px; }
.fc-trail-stat b { font-family: var(--dt-font-mono); font-variant-numeric: tabular-nums; font-weight: 600; color: var(--ui-text-primary); }
button.fc-trail-stat { cursor: pointer; }
button.fc-trail-stat:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary); }
button.fc-trail-stat[aria-pressed="true"] { border-color: color-mix(in srgb, var(--ui-accent) 55%, transparent); background: color-mix(in srgb, var(--ui-accent) 12%, transparent); color: var(--ui-text-primary); }
.fc-trail-stat .fc-sb-ico { align-self: center; }

.fc-trail-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px; margin-bottom: 8px; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; background: var(--ui-bg-elevated); }
.fc-trail-bar[data-disabled="true"] { opacity: 0.58; }
.fc-trail-sel { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.fc-trail-sel label { font-size: 11px; color: var(--ui-text-secondary); white-space: nowrap; }
.fc-trail-sel .fc-input { width: auto; min-width: 140px; max-width: 240px; padding-right: 4px; }
.fc-trail-sel .fc-input[data-set="true"] { border-color: color-mix(in srgb, var(--ui-accent) 60%, transparent); }
.fc-trail-phases { display: flex; flex-wrap: wrap; gap: 2px; padding: 2px; border-radius: 7px; border: 1px solid var(--ui-stroke-tertiary); background: var(--ui-row-hover-background); }
.fc-trail-phases button { height: 22px; padding: 0 8px; border: 0; border-radius: 5px; background: transparent; color: var(--ui-text-secondary); font: inherit; font-family: var(--dt-font-mono); font-size: 11px; cursor: pointer; }
.fc-trail-phases button:hover:not(:disabled) { color: var(--ui-text-primary); }
.fc-trail-phases button:disabled, .fc-trail-sel .fc-input:disabled { opacity: 0.55; cursor: default; }
.fc-trail-phases button[aria-pressed="true"] { background: var(--ui-chat-surface-background); color: var(--ui-text-primary); font-weight: 600; box-shadow: 0 0 0 1px var(--ui-stroke-secondary); }
.fc-trail-phases button .fc-trail-n { margin-left: 4px; font-weight: 400; color: var(--ui-text-tertiary); }
.fc-trail-bar-end { margin-left: auto; display: inline-flex; align-items: center; gap: 8px; font-size: 11px; color: var(--ui-text-secondary); font-variant-numeric: tabular-nums; }

.fc-trail-list { list-style: none; margin: 0; padding: 0; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; overflow: hidden; }
.fc-trail-row { display: grid; grid-template-columns: 104px minmax(0, 1fr); gap: 4px 14px; padding: 11px 14px 12px; border-bottom: 1px solid var(--ui-stroke-tertiary); background: transparent; scroll-margin: 64px; outline: none; transition: background-color 160ms ease, box-shadow 160ms ease; }
.fc-trail-row:last-child { border-bottom: 0; }
.fc-trail-row:hover { background: var(--ui-row-hover-background); }
.fc-trail-row[data-superseded="true"] { background: color-mix(in srgb, var(--ui-text-tertiary) 6%, transparent); }
.fc-trail-row[data-superseded="true"] .fc-trail-title { color: var(--ui-text-secondary); text-decoration: line-through; text-decoration-color: var(--ui-text-tertiary); text-decoration-thickness: 1px; }
.fc-trail-row[data-superseded="true"] .fc-trail-why { opacity: 0.85; }
.fc-trail-row[data-flash="true"] { background: color-mix(in srgb, var(--ui-accent) 14%, transparent); box-shadow: inset 3px 0 0 var(--ui-accent); }
.fc-trail-row:focus-visible { box-shadow: inset 0 0 0 2px var(--ui-accent); }
.fc-trail-when { display: flex; flex-direction: column; gap: 2px; min-width: 0; padding-top: 1px; }
.fc-trail-time { font-family: var(--dt-font-mono); font-variant-numeric: tabular-nums; font-size: 11.5px; color: var(--ui-text-primary); }
.fc-trail-ago { font-size: 11px; color: var(--ui-text-secondary); }
.fc-trail-id { font-family: var(--dt-font-mono); font-size: 10.5px; color: var(--ui-text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc-trail-main { min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.fc-trail-line { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-width: 0; }
.fc-trail-title { margin: 0; font-size: 13px; font-weight: 600; line-height: 1.35; color: var(--ui-text-primary); }
.fc-trail-why { margin: 0; max-width: 72ch; font-size: 12.5px; line-height: 1.5; color: var(--ui-text-secondary); }
.fc-trail-end { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; }

.fc-trail-phase { display: inline-flex; align-items: center; height: 18px; padding: 0 6px; border-radius: 4px; font-family: var(--dt-font-mono); font-size: 10.5px; background: var(--ui-row-active-background); color: var(--ui-text-secondary); white-space: nowrap; }
.fc-trail-phase[data-phase="escape"], .fc-trail-phase[data-phase="watchdog"] { box-shadow: inset 0 0 0 1px var(--ui-stroke-secondary); color: var(--ui-text-primary); }
.fc-trail-actor { display: inline-flex; align-items: center; gap: 4px; height: 18px; padding: 0 6px 0 5px; border-radius: 999px; border: 1px solid var(--ui-stroke-secondary); font-size: 11px; color: var(--ui-text-secondary); white-space: nowrap; }
.fc-trail-actor[data-actor="human"] { border-color: color-mix(in srgb, var(--ui-accent) 50%, transparent); color: var(--fc-trail-info-text); }
.fc-trail-result { display: inline-flex; align-items: center; gap: 4px; height: 20px; padding: 0 7px; border-radius: 999px; font-family: var(--dt-font-mono); font-size: 11px; font-weight: 600; white-space: nowrap; background: var(--ui-row-active-background); color: var(--ui-text-secondary); }
.fc-trail-result[data-tone="ok"] { background: color-mix(in srgb, var(--ui-success) 15%, transparent); color: var(--fc-trail-ok-text); }
.fc-trail-result[data-tone="bad"] { background: color-mix(in srgb, var(--dt-destructive) 15%, transparent); color: var(--fc-trail-bad-text); }
.fc-trail-result[data-tone="warn"] { background: color-mix(in srgb, var(--fc-trail-warn) 17%, transparent); color: var(--fc-trail-warn-text); }
.fc-trail-result[data-tone="info"] { background: color-mix(in srgb, var(--ui-accent) 13%, transparent); color: var(--fc-trail-info-text); }

.fc-trail-ev { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-width: 0; }
.fc-trail-pill { display: inline-flex; align-items: center; gap: 4px; max-width: 100%; height: 20px; box-sizing: border-box; padding: 0 7px; border-radius: 999px; border: 1px solid var(--ui-stroke-secondary); background: transparent; font-family: var(--dt-font-mono); font-size: 11px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-decoration: none; }
.fc-trail-pill .fc-trail-k { font-family: var(--dt-font-sans); color: var(--ui-text-tertiary); }
.fc-trail-pill[data-tone="ok"] { border-color: color-mix(in srgb, var(--ui-success) 40%, transparent); color: var(--fc-trail-ok-text); }
.fc-trail-pill[data-tone="bad"] { border-color: color-mix(in srgb, var(--dt-destructive) 45%, transparent); color: var(--fc-trail-bad-text); }
a.fc-trail-pill:hover, button.fc-trail-pill:hover { background: var(--ui-row-hover-background); border-color: var(--ui-accent); color: var(--ui-text-primary); }
button.fc-trail-pill { font: inherit; font-family: var(--dt-font-mono); font-size: 11px; cursor: pointer; }
.fc-trail-link { display: inline-flex; align-items: center; gap: 4px; height: 20px; padding: 0 7px; border-radius: 999px; font: inherit; font-size: 11px; cursor: pointer; white-space: nowrap; }
.fc-trail-link .fc-sb-mono, .fc-trail-link code { font-family: var(--dt-font-mono); font-size: 10.5px; }
.fc-trail-link[data-kind="superseded-by"] { border: 1px dashed color-mix(in srgb, var(--ui-accent) 45%, var(--ui-stroke-primary, var(--ui-stroke-secondary))); background: transparent; color: var(--fc-trail-info-text); }
.fc-trail-link[data-kind="supersedes"] { border: 1px solid color-mix(in srgb, var(--ui-accent) 55%, transparent); background: color-mix(in srgb, var(--ui-accent) 12%, transparent); color: var(--fc-trail-info-text); }
.fc-trail-link:hover { border-color: var(--ui-accent); background: color-mix(in srgb, var(--ui-accent) 18%, transparent); }
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
.fc-trail-skel { display: grid; grid-template-columns: 104px minmax(0, 1fr); gap: 14px; padding: 13px 14px; border-bottom: 1px solid var(--ui-stroke-tertiary); }
.fc-trail-skel:last-child { border-bottom: 0; }
.fc-trail-skel span { display: block; height: 9px; border-radius: 4px; background: color-mix(in srgb, var(--ui-text-secondary) 20%, transparent); animation: fc-sb-pulse 1.6s ease-in-out infinite; }
.fc-trail-skel div { display: flex; flex-direction: column; gap: 8px; }
.fc-trail-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (max-width: 640px) {
  .fc-trail-row, .fc-trail-skel { grid-template-columns: minmax(0, 1fr); }
  .fc-trail-when { flex-direction: row; flex-wrap: wrap; gap: 8px; }
  .fc-trail-bar-end { margin-left: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .fc-trail *, .fc-trail-row { animation: none !important; transition: none !important; }
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
const RIGOR_OPTIONS = [['standard', 'Standard'], ['high', 'High (multi-vendor panel)']]
const RIGOR_HINT = {
  standard: 'Single cross-model diff check + black-box check.',
  high: 'Multi-vendor panel: needs ≥2 verify models from distinct vendor families; fails closed on disagreement.'
}
const REPO_FIELD_ORDER = ['default_branch', 'merge_policy', 'max_fix_rounds', 'design_provider', 'design_approval', 'default_rigor']
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

function seedRepoDraft(repo) {
  return {
    default_branch: String(repo.default_branch ?? ''),
    merge_policy: String(repo.merge_policy ?? 'auto'),
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
function repoDraftChanges(edits, repo) {
  const own = edits || {}
  const next = normalizeRepoDraft({ ...seedRepoDraft(repo || {}), ...own })
  const base = normalizeRepoDraft(seedRepoDraft(repo || {}))
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
        'data-field': 'default_rigor',
        children: jsx(RigorTag, { value: r.default_rigor, effective: r.effective_default_rigor ?? defaults?.default_rigor ?? 'standard' })
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
  // Explicit per-field edits: { [field]: userValue } only for fields the user changed.
  // Untouched fields always show the live (possibly refreshed) repo value and are never
  // PATCHed; edited fields keep the user's value across background refreshes.
  const [edits, setEdits] = useState({})
  const live = seedRepoDraft(repo)
  const draft = { ...live, ...edits }
  const [errors, setErrors] = useState({})
  const [apiError, setApiError] = useState(null)
  const branchRef = useRef(null)
  const roundsRef = useRef(null)

  // Focus moves to the Default branch input when the editor opens.
  useEffect(() => {
    branchRef.current?.focus?.()
  }, [])

  const changes = repoDraftChanges(edits, repo)
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
      colSpan: 8,
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
                  jsx('span', { className: 'fc-mono', children: defaults.design_approval || 'ask' }),
                  ' · rigor ',
                  jsx('span', { className: 'fc-mono', children: defaults.default_rigor || 'standard' })
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
            children: ['fc-col-repo', 'fc-col-branch', 'fc-col-merge', 'fc-col-rounds', 'fc-col-prov', 'fc-col-appr', 'fc-col-rigor', 'fc-col-act'].map(col)
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
                jsx('th', { scope: 'col', children: 'Rigor' }),
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

// ---------------------------------------------------------------------------
// Decision Trail (approved Phase 1b design: design/DESIGN.md). GET /trail feeds
// both the /factonio TrailSection and the sidebar strips / card drawers through
// one shared query key; filtering happens client-side.
// ---------------------------------------------------------------------------

const TRAIL_LIMIT = 200
const TRAIL_QUERY_KEY = ['factonio', 'trail', TRAIL_LIMIT]
const TRAIL_PHASE_ORDER = ['define', 'design', 'implement', 'verify', 'gate', 'merge', 'escape', 'watchdog']
const TRAIL_TONE = {
  merged: 'ok', pass: 'ok', approved: 'ok', ready: 'ok',
  reject: 'bad', stalled: 'bad', dead_worker: 'bad', blocked: 'bad', refused: 'bad', error: 'bad',
  inconclusive: 'warn', pivot: 'warn', escape: 'warn',
  spawned: 'info', recorded: 'info', queued: 'info'
}
const TRAIL_SB_TONE = { ok: 'pass', bad: 'reject', warn: 'warn', info: 'info' }
const TRAIL_ACTOR_ICON = { orchestrator: 'dash', worker: 'running', watchdog: 'queued', human: 'person' }
const TRAIL_KNOWN_EVIDENCE = new Set(['pr_number', 'head_sha', 'patch_id', 'tests_passed', 'tests_failed', 'escaped_from', 'side_effect_age_s', 'worker_pid', 'card_id'])
const GH_SLUG = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/

const resultTone = r => TRAIL_TONE[String(r || '').toLowerCase()] || null

// "pr_merged" -> "PR merged"
function humanDecision(code) {
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

// Ordered evidence pills: { key, text, label?, tone?, href?, title }.
function evidencePills(d, gh) {
  const e = d?.evidence && typeof d.evidence === 'object' && !Array.isArray(d.evidence) ? d.evidence : {}
  const has = k => e[k] != null && e[k] !== ''
  const out = []
  if (has('pr_number')) {
    const n = Number(e.pr_number)
    const href = gh && Number.isSafeInteger(n) && n > 0 ? `https://github.com/${gh}/pull/${n}` : null
    out.push({ key: 'pr_number', text: `PR #${e.pr_number}`, href, title: href ? `Open ${gh}#${n}` : `pr_number ${e.pr_number}` })
  }
  if (has('head_sha')) out.push({ key: 'head_sha', text: String(e.head_sha).slice(0, 7), title: `head_sha ${e.head_sha}` })
  if (has('patch_id')) out.push({ key: 'patch_id', label: 'patch-id', text: String(e.patch_id).slice(0, 7), title: `patch_id ${e.patch_id}` })
  if (has('tests_passed')) out.push({ key: 'tests_passed', text: `${e.tests_passed} pass`, tone: 'ok', title: `tests_passed ${e.tests_passed}` })
  if (has('tests_failed') && Number(e.tests_failed) !== 0) out.push({ key: 'tests_failed', text: `${e.tests_failed} fail`, tone: 'bad', title: `tests_failed ${e.tests_failed}` })
  if (has('escaped_from')) out.push({ key: 'escaped_from', label: 'escaped from', text: String(e.escaped_from), title: `escaped_from ${e.escaped_from}` })
  if (has('side_effect_age_s')) out.push({ key: 'side_effect_age_s', label: 'idle', text: fmtIdle(e.side_effect_age_s), tone: 'bad', title: `side_effect_age_s ${e.side_effect_age_s}` })
  if (has('worker_pid')) out.push({ key: 'worker_pid', label: 'pid', text: String(e.worker_pid), title: `worker_pid ${e.worker_pid}` })
  if (has('card_id')) out.push({ key: 'card_id', text: String(e.card_id), title: `card_id ${e.card_id}` })
  for (const [k, v] of Object.entries(e)) {
    if (TRAIL_KNOWN_EVIDENCE.has(k) || v == null || v === '' || typeof v === 'object') continue
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

function TrailPill({ ctx, pill }) {
  const kids = [pill.label ? jsx('span', { key: 'k', className: 'fc-trail-k', children: pill.label }) : null, pill.text]
  if (pill.href) {
    return jsxs('a', {
      className: 'fc-trail-pill',
      href: pill.href,
      target: '_blank',
      rel: 'noopener noreferrer',
      title: pill.title,
      'data-ev': pill.key,
      'data-action': 'open-pr',
      onClick: e => {
        e?.preventDefault?.()
        openExternalUrl(ctx, pill.href)
      },
      children: [...kids, jsx(Icon, { key: 'ext', name: 'ext', size: 10 })]
    })
  }
  return jsxs('span', { className: 'fc-trail-pill', 'data-ev': pill.key, 'data-tone': pill.tone || undefined, title: pill.title, children: kids })
}

function TrailRow({ ctx, d, gh, flash, showWs, wsLabel, onJump, rowRef }) {
  const tone = resultTone(d.result)
  const superseded = !!d.is_superseded
  const lineage = [
    d.supersedes_id
      ? jsxs('button', {
          key: 'sup',
          type: 'button',
          className: 'fc-trail-link',
          'data-kind': 'supersedes',
          'data-jump': d.supersedes_id,
          'aria-label': `Supersedes ${d.supersedes_id}, jump to it`,
          onClick: () => onJump(d.supersedes_id),
          children: [jsx(Icon, { key: 'i', name: 'link', size: 11 }), 'Supersedes ', jsx('code', { key: 'c', children: shortId(d.supersedes_id) })]
        })
      : null,
    ...(Array.isArray(d.superseded_by) ? d.superseded_by : []).map(id => jsxs('button', {
      key: `by-${id}`,
      type: 'button',
      className: 'fc-trail-link',
      'data-kind': 'superseded-by',
      'data-jump': id,
      'aria-label': `Superseded by ${id}, jump to it`,
      onClick: () => onJump(id),
      children: ['Superseded by ', jsx('code', { key: 'c', children: shortId(id) }), jsx(Icon, { key: 'i', name: 'chev', size: 10 })]
    }))
  ]
  const pills = evidencePills(d, gh).map(p => jsx(TrailPill, { key: p.key, ctx, pill: p }))
  const ws = showWs && d.tenant
    ? jsxs('span', { key: 'ws', className: 'fc-trail-pill', 'data-ev': 'tenant', title: d.tenant, children: [jsx('span', { className: 'fc-trail-k', children: 'ws' }), wsLabel] })
    : null
  return jsxs('li', {
    className: 'fc-trail-row',
    id: `trail-${domSafe(d.id)}`,
    tabIndex: -1,
    ref: rowRef,
    'data-id': d.id,
    'data-superseded': String(superseded),
    'data-flash': String(!!flash),
    children: [
      jsxs('div', {
        className: 'fc-trail-when',
        children: [
          jsx('time', { className: 'fc-trail-time', dateTime: d.ts, title: d.ts, children: fmtTrailWhen(d.ts) }),
          jsx('span', { className: 'fc-trail-ago', children: fmtAgo(d.ts) }),
          jsx('span', { className: 'fc-trail-id', title: d.id, children: d.id })
        ]
      }),
      jsxs('div', {
        className: 'fc-trail-main',
        children: [
          jsxs('div', {
            className: 'fc-trail-line',
            children: [
              jsx('span', { className: 'fc-trail-phase', 'data-phase': d.phase || '', children: d.phase || '—' }),
              jsxs('span', {
                className: 'fc-trail-actor',
                'data-actor': d.actor || '',
                children: [jsx(Icon, { name: TRAIL_ACTOR_ICON[d.actor] || 'running', size: 11 }), d.actor || 'unknown']
              }),
              jsxs('h3', {
                className: 'fc-trail-title',
                title: d.decision,
                children: [humanDecision(d.decision), superseded ? jsx('span', { key: 'sr', className: 'fc-trail-sr', children: ' (superseded)' }) : null]
              }),
              jsx('span', {
                className: 'fc-trail-end',
                children: jsx('span', { className: 'fc-trail-result', 'data-tone': tone || undefined, children: d.result || '—' })
              })
            ]
          }),
          d.why ? jsx('p', { className: 'fc-trail-why', children: d.why }) : null,
          jsx('div', { className: 'fc-trail-ev', children: [...lineage, ...pills, ws] })
        ]
      })
    ]
  })
}

function TrailSkeleton() {
  const sk = (i, a, b, c) => jsxs('div', {
    key: i,
    className: 'fc-trail-skel',
    'aria-hidden': 'true',
    children: [
      jsxs('div', { children: [jsx('span', { style: { width: '70%' } }), jsx('span', { style: { width: '52%' } })] }),
      jsxs('div', { children: [jsx('span', { style: { width: `${a}%` } }), jsx('span', { style: { width: `${b}%` } }), jsx('span', { style: { width: `${c}%` } })] })
    ]
  })
  return jsxs('div', {
    className: 'fc-trail-list',
    'aria-busy': 'true',
    'aria-label': 'Loading decisions',
    children: [sk(0, 38, 88, 46), sk(1, 30, 74, 52), sk(2, 42, 81, 36), jsx('span', { key: 'sr', className: 'fc-trail-sr', children: 'Loading decision trail…' })]
  })
}

function TrailSection({ ctx, reposQ }) {
  const routeTenant = routeQueryParam('tenant')
  const [repo, setRepo] = useState('')
  const [tenant, setTenant] = useState(routeTenant || '')
  const [phase, setPhase] = useState('')
  const [hideSup, setHideSup] = useState(false)
  const [flash, setFlash] = useState(null)
  const [msg, setMsg] = useState('')
  // A deep link (`/factonio?tenant=…`) scopes the fetch server-side until the
  // user picks another workstream or clears the filters.
  const [scoped, setScoped] = useState(routeTenant || null)
  // Lineage targets fetched on demand because they sit outside the loaded
  // window or server scope; merged into the ledger so a jump lands on a row.
  const [pinned, setPinned] = useState({})
  const headingRef = useRef(null)
  const rowEls = useRef({})
  const pendingJump = useRef(null)
  const flashTimer = useRef(null)
  const jumpSeq = useRef(0)
  const latest = useRef({})
  const queryClient = useQueryClient()
  const queryKey = scoped ? [...TRAIL_QUERY_KEY, 'tenant', scoped] : TRAIL_QUERY_KEY
  const query = useQuery({
    queryKey,
    queryFn: () => ctx.rest(scoped ? `/trail?tenant=${encodeURIComponent(scoped)}&limit=${TRAIL_LIMIT}` : `/trail?limit=${TRAIL_LIMIT}`),
    refetchInterval: 30000
  })

  useEffect(() => {
    if (!routeTenant) return
    setTenant(routeTenant)
    setScoped(routeTenant)
    setRepo('')
    setPhase('')
    setHideSup(false)
    headingRef.current?.scrollIntoView?.({ block: 'start', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
  }, [routeTenant])

  useEffect(() => {
    const id = pendingJump.current
    if (!id) return
    // Clearing a server scope refetches; keep the jump pending until the row renders.
    const el = visible.some(d => d.id === id) ? rowEls.current[id] : null
    if (!el) {
      if (!loading) pendingJump.current = null
      return
    }
    pendingJump.current = null
    el.scrollIntoView?.({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
    el.focus?.({ preventScroll: true })
    clearTimeout(flashTimer.current)
    flashTimer.current = setTimeout(() => setFlash(cur => (cur === id ? null : cur)), 1600)
  })

  const data = query.data
  const isError = !!query.isError
  const hasData = data != null
  const loading = !hasData && !isError
  const loaded = trailDecisions(data)
  const loadedIds = new Set(loaded.map(d => d.id))
  const extra = Object.values(pinned).filter(d => d && !loadedIds.has(d.id))
  const all = extra.length ? trailDecisions({ decisions: [...loaded, ...extra] }) : loaded
  const empty = hasData && all.length === 0 && !scoped

  const registered = Array.isArray(reposQ?.data?.repos) ? reposQ.data.repos : []
  const ghBySlug = Object.fromEntries(registered.filter(r => r?.slug && r?.repo).map(r => [r.slug, r.repo]))
  const ghRepos = [...new Set([...registered.map(r => r?.repo).filter(Boolean), ...all.map(d => trailGithubRepo(d.repo_slug, ghBySlug)).filter(Boolean)])]
  const summary = data?.summary && typeof data.summary === 'object' ? data.summary : {}
  const stat = (k, fallback) => (Number.isFinite(summary[k]) ? summary[k] : fallback)
  const total = stat('total', all.length)
  const supN = stat('superseded', all.filter(d => d.is_superseded).length)
  const activeN = stat('active', total - supN)

  const repoOpts = [...new Set([...all.map(d => d.repo_slug).filter(Boolean), ...registered.map(r => r?.slug).filter(Boolean), ...(repo ? [repo] : [])])].sort()
  const tenantOpts = [...new Set([
    ...Object.keys(summary.by_tenant && typeof summary.by_tenant === 'object' ? summary.by_tenant : {}).filter(t => t && t !== 'unknown'),
    ...all.map(d => d.tenant).filter(Boolean),
    ...(tenant ? [tenant] : [])
  ])].sort()
  const inScope = d => (!repo || d.repo_slug === repo) && (!tenant || d.tenant === tenant)
  const visibleOf = (r, t, p, h) => all.filter(d => (!r || d.repo_slug === r) && (!t || d.tenant === t) && (!p || d.phase === p) && !(h && d.is_superseded))
  const visible = visibleOf(repo, tenant, phase, hideSup)
  const anyFilter = !!(repo || tenant || phase)
  const present = new Set(all.map(d => d.phase).filter(Boolean))
  if (phase) present.add(phase)
  const phases = hasData
    ? [...TRAIL_PHASE_ORDER.filter(p => present.has(p)), ...[...present].filter(p => !TRAIL_PHASE_ORDER.includes(p)).sort()]
    : TRAIL_PHASE_ORDER
  const phaseN = p => all.filter(d => d.phase === p && inScope(d)).length
  const controlsOff = !hasData

  const announce = (r, t, p, h) => {
    const n = visibleOf(r, t, p, h).length
    setMsg(`${plural(n, 'decision', 'decisions')} shown.`)
  }
  const clearAll = () => {
    setRepo(''); setTenant(''); setPhase(''); setHideSup(false); setScoped(null)
    setMsg('Filters cleared.')
  }
  const onFilter = (key, value) => {
    const v = value || ''
    if (key === 'repo') { setRepo(v); announce(v, tenant, phase, hideSup) }
    if (key === 'tenant') {
      setTenant(v)
      if (scoped && v !== scoped) setScoped(null)
      announce(repo, v, phase, hideSup)
    }
  }
  const passes = (d, f) => (!f.repo || d.repo_slug === f.repo) && (!f.tenant || d.tenant === f.tenant) && (!f.phase || d.phase === f.phase) && !(f.hideSup && d.is_superseded)
  latest.current = { repo, tenant, phase, hideSup, scoped }
  const land = (id, row, f) => {
    if (!passes(row, f)) {
      setRepo(''); setTenant(''); setPhase(''); setHideSup(false); setScoped(null)
      setMsg(`Filters cleared to show ${id}.`)
    } else {
      setMsg(`Jumped to ${id}.`)
    }
    pendingJump.current = id
    setFlash(id)
  }
  // Single-row lookup; older hosts without GET /trail/{id} fall back to one
  // wide unscoped /trail page when the target may sit outside what is loaded.
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
  const jump = async id => {
    const seq = ++jumpSeq.current
    const known = all.find(d => d.id === id)
    if (known) {
      land(id, known, { repo, tenant, phase, hideSup })
      return
    }
    pendingJump.current = null
    setMsg(`Loading ${id}…`)
    const row = await fetchDecision(id, !!scoped || total > loaded.length).catch(() => null)
    if (seq !== jumpSeq.current) return
    if (!row) {
      setMsg(`Could not load decision ${id}.`)
      return
    }
    setPinned(cur => ({ ...cur, [row.id]: row }))
    land(id, row, latest.current)
  }

  const select = (id, key, label, allLabel, opts, value) => jsxs('span', {
    key,
    className: 'fc-trail-sel',
    children: [
      jsx('label', { htmlFor: id, children: label }),
      jsx('select', {
        id,
        className: 'fc-input',
        'data-filter': key,
        'data-set': String(!!value),
        disabled: controlsOff,
        value,
        onChange: ev => onFilter(key, ev?.target?.value),
        children: [jsx('option', { key: '__all', value: '', children: allLabel }), ...opts.map(o => jsx('option', { key: o.v, value: o.v, children: o.l }))]
      })
    ]
  })

  const head = jsxs('div', {
    className: 'fc-sec-head',
    children: [
      jsxs('div', {
        className: 'fc-trail-head-l',
        children: [
          jsx('h2', { id: 'fc-trail-h', ref: headingRef, children: 'Decision Trail' }),
          jsxs('p', {
            className: 'fc-sec-sub',
            children: ['Append-only ledger from ', jsx('span', { className: 'fc-mono', children: 'trail.db' }), ' — every gate, pivot and override, with the evidence behind it.']
          })
        ]
      }),
      loading
        ? jsxs('div', {
            className: 'fc-trail-sum',
            'aria-label': 'Trail summary',
            children: ['total', 'active', 'superseded'].map(k => jsxs('span', { key: k, className: 'fc-trail-stat', children: [jsx('b', { children: '–' }), ` ${k}`] }))
          })
        : hasData && !empty
          ? jsxs('div', {
              className: 'fc-trail-sum',
              'aria-label': 'Trail summary',
              children: [
                jsxs('span', { key: 't', className: 'fc-trail-stat', 'data-stat': 'total', children: [jsx('b', { children: String(total) }), ' total'] }),
                jsxs('span', { key: 'a', className: 'fc-trail-stat', 'data-stat': 'active', children: [jsx('b', { children: String(activeN) }), ' active'] }),
                jsxs('button', {
                  key: 's',
                  type: 'button',
                  className: 'fc-trail-stat',
                  'data-stat': 'superseded',
                  'data-action': 'toggle-sup',
                  'aria-pressed': String(hideSup),
                  title: `${hideSup ? 'Show' : 'Hide'} superseded decisions`,
                  onClick: () => {
                    const next = !hideSup
                    setHideSup(next)
                    setMsg(next ? 'Superseded decisions hidden.' : 'Superseded decisions shown.')
                  },
                  children: [jsx('b', { children: String(supN) }), hideSup ? ' superseded · hidden' : ' superseded']
                })
              ]
            })
          : null
    ]
  })

  const bar = empty
    ? null
    : jsxs('div', {
        className: 'fc-trail-bar',
        role: 'search',
        'aria-label': 'Filter decisions',
        'data-disabled': String(controlsOff),
        children: [
          select('fc-trail-repo', 'repo', 'Repository', 'All repos', repoOpts.map(r => ({ v: r, l: r })), repo),
          select('fc-trail-ws', 'tenant', 'Workstream', 'All workstreams', tenantOpts.map(t => ({ v: t, l: trailWsLabel(t, ghRepos) })), tenant),
          jsxs('div', {
            className: 'fc-trail-phases',
            role: 'group',
            'aria-label': 'Phase',
            children: [
              jsx('button', {
                key: '__all',
                type: 'button',
                'data-phase': '',
                'aria-pressed': String(!phase),
                disabled: controlsOff,
                onClick: () => { setPhase(''); announce(repo, tenant, '', hideSup) },
                children: 'All'
              }),
              ...phases.map(p => jsxs('button', {
                key: p,
                type: 'button',
                'data-phase': p,
                'aria-pressed': String(phase === p),
                disabled: controlsOff,
                onClick: () => { setPhase(p); announce(repo, tenant, p, hideSup) },
                children: [p, hasData ? jsx('span', { key: 'n', className: 'fc-trail-n', children: String(phaseN(p)) }) : null]
              }))
            ]
          }),
          jsxs('span', {
            className: 'fc-trail-bar-end',
            children: [
              hasData ? jsx('span', { key: 'n', 'data-field': 'shown', children: `${visible.length} of ${all.length}` }) : null,
              hasData && anyFilter
                ? jsx('button', { key: 'clear', type: 'button', className: 'fc-btn', 'data-action': 'clear', onClick: clearAll, children: 'Clear filters' })
                : null
            ]
          })
        ]
      })

  let errStatus = null
  if (isError) errStatus = describeSaveError(query.error).status
  const alert = isError
    ? jsxs('div', {
        className: 'fc-alert fc-trail-alert',
        role: 'alert',
        children: [
          jsx(Icon, { name: 'attention' }),
          jsxs('div', {
            className: 'fc-trail-alert-body',
            children: [
              jsx('b', { children: 'Couldn’t load the decision trail.' }),
              errStatus
                ? [' The trail endpoint returned ', jsx('span', { key: 's', className: 'fc-mono', children: String(errStatus) }), '.']
                : ' The trail endpoint could not be reached.',
              hasData ? ' Showing the last loaded decisions. Nothing was changed.' : ' Nothing was changed.'
            ]
          }),
          jsxs('button', {
            type: 'button',
            className: 'fc-btn',
            'data-action': 'retry',
            onClick: () => retryQuery(query, queryClient, queryKey),
            children: [jsx(Icon, { name: 'retry' }), 'Retry']
          })
        ]
      })
    : null

  let body
  if (loading) {
    body = jsx(TrailSkeleton, {})
  } else if (!hasData) {
    body = null
  } else if (empty) {
    body = jsxs('div', {
      className: 'fc-trail-state',
      'data-state': 'empty',
      children: [
        jsx('span', { className: 'fc-trail-state-ico', children: jsx(Icon, { name: 'trail', size: 16 }) }),
        jsx('h3', { children: 'No decisions recorded yet' }),
        jsx('p', { children: 'The orchestrator, workers and watchdog log each gate, pivot and override here automatically as cards move. You can also inspect it from a terminal:' }),
        jsx('div', { className: 'fc-step-cmd', children: 'hermes factonio trail --limit 20' })
      ]
    })
  } else if (!visible.length) {
    const chips = [
      repo && `repo ${repo}`,
      tenant && `workstream ${trailWsLabel(tenant, ghRepos)}`,
      phase && `phase ${phase}`,
      hideSup && 'superseded hidden'
    ].filter(Boolean)
    body = jsxs('div', {
      className: 'fc-trail-state',
      'data-state': 'filtered',
      children: [
        jsx('span', { className: 'fc-trail-state-ico', children: jsx(Icon, { name: 'filter', size: 16 }) }),
        jsx('h3', { children: 'No decisions match these filters' }),
        jsx('div', { className: 'fc-trail-active', children: chips.map(c => jsx('span', { key: c, className: 'fc-trail-pill', children: c })) }),
        jsx('p', { children: `${plural(all.length, 'decision exists', 'decisions exist')} outside this view.` }),
        jsx('button', { type: 'button', className: 'fc-btn fc-btn--primary', 'data-action': 'clear', onClick: clearAll, children: 'Clear filters' })
      ]
    })
  } else {
    const showWs = !tenant && (!!repo || !!phase)
    body = jsx('ol', {
      className: 'fc-trail-list',
      'aria-label': 'Decisions, newest first',
      children: visible.map(d => jsx(TrailRow, {
        key: d.id,
        ctx,
        d,
        gh: trailGithubRepo(d.repo_slug, ghBySlug),
        flash: flash === d.id,
        showWs,
        wsLabel: trailWsLabel(d.tenant, ghRepos),
        onJump: jump,
        rowRef: el => { rowEls.current[d.id] = el }
      }))
    })
  }

  return jsxs('section', {
    className: 'fc-sec fc-trail',
    'aria-labelledby': 'fc-trail-h',
    'aria-busy': String(loading),
    'data-state': loading ? 'loading' : isError && !hasData ? 'error' : empty ? 'empty' : hasData && !visible.length ? 'filtered' : 'populated',
    children: [head, bar, alert, body, jsx('p', { className: 'fc-trail-sr', 'aria-live': 'polite', children: msg })]
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

// Panels of one workstream, newest first: [{ id, consensus, lanes, decided, vendors,
// hasDisagreement, dissent, recorded, createdAt }]. `trailDecisions` are that tenant's
// /trail rows (any order).
function computeWorkstreamPanels(wsCards, trailDecisions) {
  const groups = groupBy((wsCards || []).filter(c => c && c.panel_id), c => String(c.panel_id))
  const consensusRows = (Array.isArray(trailDecisions) ? trailDecisions : [])
    .filter(d => d && d.decision === 'verify_panel_consensus' && !d.is_superseded && d.evidence && typeof d.evidence === 'object')
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
    let consensus = live.consensus === 'pass' && !recorded ? 'pending' : live.consensus
    if (recorded && PANEL_SEVERITY[recorded] > PANEL_SEVERITY[consensus]) consensus = recorded
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

function PanelStrip({ panel: p }) {
  const mixed = mixedPhases(p.lanes)
  const label = `Verification panel ${p.id}: ${p.consensus}, ${p.decided} of ${p.lanes.length} lanes${p.hasDisagreement ? ', lanes disagree' : ''}`
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
          jsx('span', { className: 'fc-sb-panel-lanes', children: `${p.decided}/${p.lanes.length} lanes` })
        ]
      }),
      jsx('div', {
        className: 'fc-sb-vendors',
        'aria-label': 'Lanes by vendor',
        children: p.lanes.map(l => jsx(VendorChip, { key: l.cardId, lane: l, mixed }))
      }),
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
        : null
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
  spin: [['circle', { cx: 6, cy: 6, r: 4.2, strokeDasharray: '15 11' }]]
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
      jsx('span', { key: 'c', className: 'fc-sb-tag', 'data-tone': PANEL_TONE[panel.consensus], children: panel.consensus })
    ]),
    ...(lane
      ? row('vendor', 'Vendor', [
          jsx(VendorChip, { key: 'v', lane, mixed }),
          jsx('span', { key: 'n', className: 'fc-sb-lane', children: `lane ${pos}/${panel.lanes.length}` })
        ])
      : []),
    ...(peers.length ? row('peers', 'Peers', peers.map(l => jsx(VendorChip, { key: l.cardId, lane: l, mixed }))) : [])
  ]
}

function CardDrawer({ card: c, id, trail, panel }) {
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

function CardRow({ ctx, card: c, wsCards, open, onToggle, trail, panel }) {
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
          c.panel_vendor ? jsx('span', { className: 'fc-sb-vendor', 'data-panel-vendor': String(c.panel_vendor), title: 'Panel lane vendor', children: String(c.panel_vendor) }) : null,
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
      open ? jsx(CardDrawer, { key: 'drawer', card: c, id: drawerId, trail, panel }) : null,
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

function DoneGroup({ ctx, wsKey, cards, wsCards, open, onToggle, cardOpen, toggleCard, trail, panelOf }) {
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
          children: cards.map(c => jsx(CardRow, { key: c.id, ctx, card: c, wsCards, open: !!cardOpen[c.id], onToggle: () => toggleCard(c.id), trail, panel: panelOf(c) }))
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
  const panels = computeWorkstreamPanels(ws.wsCards, ui.trail?.status === 'ready' ? ui.trail.byTenant.get(ws.tenant || '') : [])
  const panelOf = c => (c.panel_id ? panels.find(p => p.id === String(c.panel_id)) || null : null)
  return jsxs('section', {
    ...sectionProps,
    children: [
      head,
      jsxs('div', {
        key: 'body',
        id: bodyId,
        children: [
          jsx(Pipeline, { pipe: computePipelineStages(ws.wsCards) }),
          panels.length ? jsx(PanelStrip, { key: 'panel', panel: panels[0] }) : null,
          jsxs('div', {
            className: 'fc-sb-ws-cards',
            children: [
              ...main.map(c => jsx(CardRow, { key: c.id, ctx, card: c, wsCards: ws.wsCards, open: !!ui.cardOpen[c.id], onToggle: () => toggleCard(c.id), trail: ui.trail, panel: panelOf(c) })),
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
                    panelOf
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
      const ui = { status, wsOpen, setWsOpen, doneOpen, setDoneOpen, cardOpen, setCardOpen, trail, trailOpen, setTrailOpen }
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
export { validateRepoDraft, repoDraftChanges, seedRepoDraft, describeSaveError, verdictTone, computeWorkstreamPanels, sparkPoints, sparkPath }

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
