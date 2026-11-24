"""Clean-room tests for the Factonio Desktop plugin (desktop/plugin.js).

Covers the Settings → Plugins → Factonio landing page registration, the
Auxiliary Models deep-links on both the settings page and the /factonio
dashboard, the hideable left-sidebar Factonio tab (registration, reveal,
visibility-aware polling, scope/status/phase filters, workstream pipeline,
card details and quick actions), older-host compatibility, and Desktop SDK
surface lint.

The smoke test executes ``desktop/plugin.js`` under Node's ESM loader with
``node:module`` ``registerHooks`` stubbing ``@hermes/plugin-sdk``, ``react``
and ``react/jsx-runtime`` in memory — no build step, no node_modules, and no
edits to the plugin source.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

from tests._env import PLUGIN_ROOT

PLUGIN_JS = PLUGIN_ROOT / "desktop" / "plugin.js"
PHASES = ["define", "design", "implement", "verify", "watchdog"]


def aux_route(phase: str) -> str:
    return f"/settings?tab=config:model&aux=factonio_{phase}"


SMOKE_SCRIPT = r"""
import { registerHooks } from 'node:module'

const STUBS = {
  '@hermes/plugin-sdk': `
    export const ROUTES_AREA = 'routes'
    export const SIDEBAR_NAV_AREA = 'sidebar-nav'
    export const PALETTE_AREA = 'palette'
    export const PANES_AREA = 'panes'
    export const STATUSBAR_AREAS = { left: 'statusbar-left', right: 'statusbar-right' }
    export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript-directive'
    export function EmptyState(props) { return { type: 'div', props: { children: [props.title, props.description] } } }
    export const host = globalThis.__fcHost
    export function useQuery(opts) { return globalThis.__fcUseQuery(opts) }
    export function useValue(v) { return v && typeof v === 'object' && typeof v.get === 'function' ? v.get() : v }
  `,
  'react': `
    export function useState(init) { return globalThis.__fcHooks.useState(init) }
    export default { useState }
  `,
  'react/jsx-runtime': `
    export function jsx(type, props, key) { return { type, props: props || {}, key } }
    export const jsxs = jsx
    export const Fragment = 'fragment'
  `
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (Object.hasOwn(STUBS, specifier)) return { url: 'stub:' + specifier, shortCircuit: true, format: 'module' }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.startsWith('stub:')) return { format: 'module', source: STUBS[url.slice(5)], shortCircuit: true }
    return nextLoad(url, context)
  }
})

const navigations = []
const notifications = []
const reveals = []
let paneVisible = true
const visibilityCalls = []
globalThis.__fcHost = {
  navigate: p => navigations.push(p),
  notify: n => notifications.push(n),
  composer: { submit: () => {} },
  state: { focusedStoredSessionId: null },
  revealPane: id => reveals.push(id),
  paneVisibility: id => { visibilityCalls.push(id); return { get: () => paneVisible } }
}
const host = globalThis.__fcHost
const windowOpens = []
globalThis.window = { open: (...args) => windowOpens.push(args) }
const navClipboard = []
Object.defineProperty(globalThis, 'navigator', { value: { clipboard: { writeText: async t => { navClipboard.push(t) } } }, configurable: true, writable: true })

// Minimal hooks runtime: useState slots keyed by component path, persisted
// across re-renders of the same renderer (mirrors React state identity).
function makeHooks() {
  const store = new Map()
  let path = 'root'
  let idx = 0
  return {
    store,
    useState(init) {
      const k = path + '#' + (idx++)
      if (!store.has(k)) store.set(k, typeof init === 'function' ? init() : init)
      return [store.get(k), v => store.set(k, typeof v === 'function' ? v(store.get(k)) : v)]
    },
    enter(p) { const prev = [path, idx]; path = p; idx = 0; return prev },
    leave(prev) { [path, idx] = prev }
  }
}
globalThis.__fcHooks = makeHooks()
globalThis.document = {
  createElement: () => ({ textContent: '', remove() {} }),
  head: { appendChild() {} }
}

const MODELS = {
  models: {
    define: { provider: 'anthropic', model: 'claude-x', source: 'auxiliary.factonio_define' },
    design: { provider: 'anthropic', model: 'claude-design-m', source: 'auxiliary.factonio_design' },
    implement: { provider: 'openai', model: 'gpt-y', source: 'auxiliary.factonio_implement' },
    verify: { provider: '', model: 'solo-model', source: 'auxiliary.factonio_verify' },
    watchdog: {}
  },
  cross_model_warning: 'Verification uses the same model as Implementation!'
}
let queryState = { data: MODELS, isLoading: false, isError: false }
let cardsState = { data: undefined, isLoading: true, isError: false }
const cardsQueries = []
globalThis.__fcUseQuery = opts => {
  const key = opts.queryKey.join('/')
  if (key === 'factonio/models') {
    globalThis.__fcModelsQueries.push({ key: opts.queryKey, interval: opts.refetchInterval, fn: typeof opts.queryFn })
    return queryState
  }
  if (opts.queryKey[1] === 'cards') {
    cardsQueries.push({ key: opts.queryKey, enabled: opts.enabled !== false, interval: opts.refetchInterval, fn: opts.queryFn })
    return typeof cardsState === 'function' ? cardsState(opts) : cardsState
  }
  return { data: undefined, isLoading: true, isError: false }
}
globalThis.__fcModelsQueries = []

function expand(node, p = 'root') {
  if (node == null || node === false || node === true) return null
  if (Array.isArray(node)) return node.map((n, i) => expand(n, p + '[' + (n && typeof n === 'object' && (n.key ?? n.props?.key) != null ? 'k:' + (n.key ?? n.props.key) : i) + ']'))
  if (typeof node !== 'object') return node
  if (typeof node.type === 'function') {
    const myPath = p + '/' + (node.type.name || 'anon')
    const prev = globalThis.__fcHooks.enter(myPath)
    let out
    try { out = node.type(node.props || {}) } finally { globalThis.__fcHooks.leave(prev) }
    return expand(out, myPath)
  }
  const props = { ...(node.props || {}) }
  props.children = expand(props.children, p + '/' + node.type)
  return { type: node.type, props }
}
function text(node) {
  if (node == null) return ''
  if (Array.isArray(node)) return node.map(text).join(' ')
  if (typeof node !== 'object') return String(node)
  return text(node.props.children)
}
function buttons(node, out = []) {
  if (node == null || typeof node !== 'object') return out
  if (Array.isArray(node)) { node.forEach(n => buttons(n, out)); return out }
  if (node.type === 'button') out.push(node)
  buttons(node.props.children, out)
  return out
}
function clickAll(tree) {
  const res = []
  for (const b of buttons(tree)) {
    navigations.length = 0
    if (typeof b.props.onClick === 'function') b.props.onClick()
    res.push({ label: text(b).trim(), navigated: [...navigations] })
  }
  return res
}

// ---------------------------------------------------------------------------
// Sidebar tab harness (PANES_AREA 'pane', PALETTE_AREA 'show-pane', StatusPill)
// ---------------------------------------------------------------------------
const NOW = Math.floor(Date.now() / 1000)
const T = s => NOW - s
const SIDEBAR = 'tozes-factonio-improve-factonio-sidebar'
const META = 'tozes-factonio-enrich-cards-metadata'
const PILL = 'tozes-factonio-statusbar-pill-counts'
const PR44 = { repo: 'tozes/factonio', number: 44, url: 'https://github.com/tozes/factonio/pull/44' }
const PR42 = { repo: 'tozes/factonio', number: 42, url: 'https://github.com/tozes/factonio/pull/42' }
const PR39 = { repo: 'tozes/factonio', number: 39, url: 'https://github.com/tozes/factonio/pull/39' }
const base = { provider: 'anthropic', model: 'claude-sonnet-4-5', parents: [], block_kind: null, block_reason: null, pr: null, runs: 1, verdict: null, last_outcome: 'completed', last_error: null, summary: null, completed_at: null }
const card = o => ({ ...base, ...o })
// Same fixture as the approved design/preview.html.
const CARDS = [
  card({ id: 't_4ab36a09', tenant: SIDEBAR, session_id: 'sess_9c41e2', phase: 'define', status: 'done', title: '[define] Spec: hideable Factonio tab in the left sidebar', model: 'claude-opus-4-1', created_at: T(9000), started_at: T(8980), completed_at: T(8420), elapsed_seconds: 560, active_duration_seconds: 318, summary: 'Acceptance criteria written.' }),
  card({ id: 't_4ab36a0b', tenant: SIDEBAR, session_id: 'sess_9c41e2', phase: 'design', status: 'done', title: '[design] Phase 1b sidebar previews', parents: ['t_4ab36a09'], created_at: T(8400), started_at: T(8390), completed_at: T(7100), elapsed_seconds: 1290, active_duration_seconds: 744 }),
  card({ id: 't_4ab36a0d', tenant: SIDEBAR, session_id: 'sess_9c41e2', phase: 'implement', status: 'done', title: '[implement] Tabbed sidebar pane with scope, status and phase filters', parents: ['t_4ab36a0b'], pr: PR44, created_at: T(7000), started_at: T(6990), completed_at: T(4600), elapsed_seconds: 2390, active_duration_seconds: 1505 }),
  card({ id: 't_4ab36a10', tenant: SIDEBAR, session_id: 'sess_9c41e2', phase: 'verify-diff', status: 'done', verdict: 'pass', title: '[verify-diff] Review PR #44 against SPEC.md', model: 'gpt-5', provider: 'openai', parents: ['t_4ab36a0d'], pr: PR44, created_at: T(4500), started_at: T(4490), completed_at: T(4010), elapsed_seconds: 480, active_duration_seconds: 402 }),
  card({ id: 't_4ab36a11', tenant: SIDEBAR, session_id: 'sess_9c41e2', phase: 'verify-blackbox', status: 'done', verdict: 'reject', last_outcome: 'changes_requested', title: '[verify-blackbox] Keyboard and theme acceptance run', model: 'gpt-5', provider: 'openai', parents: ['t_4ab36a0d'], pr: PR44, created_at: T(4500), started_at: T(4480), completed_at: T(3300), elapsed_seconds: 1180, active_duration_seconds: 960, summary: 'Collapsed workstream header is skipped by Tab.' }),
  card({ id: 't_4ab36a14', tenant: SIDEBAR, session_id: 'sess_9c41e2', phase: 'implement', status: 'blocked', block_kind: 'needs_input', block_reason: 'Should expanded/collapsed workstreams persist across app restarts?', last_outcome: null, title: '[implement] Fix round 1: focus order and callout contrast', parents: ['t_4ab36a11'], created_at: T(3200), started_at: T(3190), elapsed_seconds: 3190, active_duration_seconds: 212 }),
  card({ id: 't_7d21c0e5', tenant: META, session_id: 'sess_51d0aa', phase: 'define', status: 'done', title: '[define] Spec: additive /cards metadata fields', created_at: T(6200), started_at: T(6190), completed_at: T(5900), elapsed_seconds: 290, active_duration_seconds: 240 }),
  card({ id: 't_7d21c0e6', tenant: META, session_id: 'sess_51d0aa', phase: 'implement', status: 'done', title: '[implement] Add tenant index to kanban card query', parents: ['t_7d21c0e5'], pr: PR42, created_at: T(5800), started_at: T(5790), completed_at: T(5200), elapsed_seconds: 590, active_duration_seconds: 410 }),
  card({ id: 't_a1b2c3d4', tenant: META, session_id: 'sess_51d0aa', phase: 'implement', status: 'review', runs: 2, title: '[implement] Enrich /cards metadata in metrics.py', parents: ['t_7d21c0e5', 't_7d21c0e6'], pr: PR42, created_at: T(5100), started_at: T(1200), completed_at: null, elapsed_seconds: 412, active_duration_seconds: 185, summary: 'Added additive card fields and updated unit tests.', last_outcome: 'review_requested' }),
  card({ id: 't_a1b2c3e0', tenant: META, session_id: 'sess_51d0aa', phase: 'verify-diff', status: 'running', runs: 2, last_outcome: 'failed', last_error: 'Run 1 exited: GitHub API 502 while fetching the PR diff.', title: '[verify-diff] Re-check PR #42 after rework', model: 'gpt-5', provider: 'openai', parents: ['t_a1b2c3d4'], pr: PR42, created_at: T(700), started_at: T(220), elapsed_seconds: 700, active_duration_seconds: 96 }),
  card({ id: 't_a1b2c3e1', tenant: META, session_id: 'sess_51d0aa', phase: 'verify-blackbox', status: 'todo', last_outcome: null, title: '[verify-blackbox] API contract run for /cards', model: null, provider: null, parents: ['t_a1b2c3d4'], created_at: T(700), started_at: null, elapsed_seconds: 0, active_duration_seconds: 0 }),
  card({ id: 't_39c1aa01', tenant: PILL, session_id: 'sess_0b77f3', phase: 'define', status: 'done', title: '[define] Status pill counts spec', created_at: T(90000), started_at: T(89990), completed_at: T(89700), elapsed_seconds: 290, active_duration_seconds: 210 }),
  card({ id: 't_39c1aa02', tenant: PILL, session_id: 'sess_0b77f3', phase: 'design', status: 'done', title: '[design] Status pill states', created_at: T(89600), started_at: T(89590), completed_at: T(89000), elapsed_seconds: 590, active_duration_seconds: 380 }),
  card({ id: 't_39c1aa03', tenant: PILL, session_id: 'sess_0b77f3', phase: 'implement', status: 'done', pr: PR39, title: '[implement] Running/blocked counts in the status bar', created_at: T(88900), started_at: T(88890), completed_at: T(87000), elapsed_seconds: 1890, active_duration_seconds: 1220 }),
  card({ id: 't_39c1aa04', tenant: PILL, session_id: 'sess_0b77f3', phase: 'verify-diff', status: 'done', verdict: 'pass', pr: PR39, title: '[verify-diff] Review PR #39', created_at: T(86900), started_at: T(86890), completed_at: T(86500), elapsed_seconds: 390, active_duration_seconds: 330 }),
  card({ id: 't_39c1aa05', tenant: PILL, session_id: 'sess_0b77f3', phase: 'verify-blackbox', status: 'done', verdict: 'pass', pr: PR39, title: '[verify-blackbox] Status pill acceptance run', created_at: T(86900), started_at: T(86880), completed_at: T(86100), elapsed_seconds: 780, active_duration_seconds: 640 })
]
// Multi-round fix loop: BB round 1 rejected, fix, BB round 2 passed. Plus an unsafe PR URL + null tenant.
const RECOVERY = [
  card({ id: 't_r1', tenant: 'tozes-factonio-recover', phase: 'define', status: 'done', title: '[define] d', created_at: 100 }),
  card({ id: 't_r2', tenant: 'tozes-factonio-recover', phase: 'implement', status: 'done', title: '[implement] i', pr: PR42, created_at: 200 }),
  card({ id: 't_r3', tenant: 'tozes-factonio-recover', phase: 'verify-diff', status: 'done', verdict: 'pass', title: '[verify:diff] d', created_at: 300 }),
  card({ id: 't_r4', tenant: 'tozes-factonio-recover', phase: 'verify-blackbox', status: 'done', verdict: 'reject', last_outcome: 'changes_requested', title: '[verify:blackbox] round 1', created_at: 300 }),
  card({ id: 't_r5', tenant: 'tozes-factonio-recover', phase: 'implement', status: 'done', runs: 2, title: '[implement] fix round 1', created_at: 400 }),
  card({ id: 't_r6', tenant: 'tozes-factonio-recover', phase: 'verify-blackbox', status: 'done', verdict: 'pass', title: '[verify:blackbox] round 2', created_at: 500 }),
  card({ id: 't_u1', tenant: null, phase: 'other', status: 'triage', title: 'Loose card', block_reason: null, pr: { repo: 'evil/x', number: 1, url: 'javascript:alert(1)' }, created_at: 600 }),
  card({ id: 't_u2', tenant: null, phase: 'implement', status: 'running', title: '[implement] http pr', pr: { repo: 'evil/y', number: 2, url: 'http://example.com/pull/2' }, created_at: 650 })
]

// Superseded phase cards: each pipeline stage resolves from its latest card (spec §3).
const SUP = 'tozes-factonio-sup-'
const supRound = (t, latest) => [
  card({ id: `t_${t}1`, tenant: SUP + t, phase: 'define', status: 'done', title: '[define] d', created_at: 100 }),
  card({ id: `t_${t}2`, tenant: SUP + t, phase: 'implement', status: 'done', title: '[implement] i', created_at: 200 }),
  card({ id: `t_${t}3`, tenant: SUP + t, phase: 'verify-diff', status: 'done', verdict: 'pass', title: '[verify:diff] r1', created_at: 300 }),
  card({ id: `t_${t}4`, tenant: SUP + t, phase: 'verify-blackbox', status: 'done', verdict: 'reject', last_outcome: 'changes_requested', title: '[verify:blackbox] r1', created_at: 300 }),
  card({ id: `t_${t}5`, tenant: SUP + t, phase: 'verify-blackbox', title: '[verify:blackbox] r2', created_at: 500, ...latest })
]
const SUPERSEDED = [
  ...supRound('rq', { status: 'todo', last_outcome: null }),
  ...supRound('rr', { status: 'running', last_outcome: null }),
  ...supRound('rp', { status: 'done', verdict: 'pass' }),
  ...supRound('rj', { status: 'done', verdict: 'reject', last_outcome: 'changes_requested' }),
  // Same created_at: deterministic id tie-break (t_tie9 > t_tie1) picks the running card.
  card({ id: 't_tie1', tenant: SUP + 'tie', phase: 'verify-diff', status: 'done', verdict: 'reject', title: '[verify:diff] a', created_at: 700 }),
  card({ id: 't_tie9', tenant: SUP + 'tie', phase: 'verify-diff', status: 'running', title: '[verify:diff] b', created_at: 700 }),
  // Non-verify: old blocked implement superseded by a newer running / done implement.
  card({ id: 't_ib1', tenant: SUP + 'ibr', phase: 'implement', status: 'blocked', block_kind: 'needs_input', block_reason: 'q', title: '[implement] old', created_at: 200 }),
  card({ id: 't_ib2', tenant: SUP + 'ibr', phase: 'implement', status: 'running', title: '[implement] new', created_at: 400 }),
  card({ id: 't_id1', tenant: SUP + 'ibd', phase: 'implement', status: 'triage', title: '[implement] old', created_at: 200 }),
  card({ id: 't_id2', tenant: SUP + 'ibd', phase: 'implement', status: 'done', title: '[implement] new', created_at: 400 }),
  card({ id: 't_ir1', tenant: SUP + 'irb', phase: 'implement', status: 'done', title: '[implement] old', created_at: 200 }),
  card({ id: 't_ir2', tenant: SUP + 'irb', phase: 'implement', status: 'blocked', block_kind: 'capability', block_reason: 'no access', title: '[implement] new', created_at: 400 })
]
// Duration fallback: active=0 is a valid value; only null/missing falls back to wall time.
const DURATION = [
  card({ id: 't_dz', tenant: 'tozes-factonio-dur', phase: 'implement', status: 'running', title: '[implement] zero', created_at: 100, active_duration_seconds: 0, elapsed_seconds: 3600 }),
  card({ id: 't_dn', tenant: 'tozes-factonio-dur', phase: 'implement', status: 'running', title: '[implement] null', created_at: 110, active_duration_seconds: null, elapsed_seconds: 3600 }),
  (() => { const c = card({ id: 't_dm', tenant: 'tozes-factonio-dur', phase: 'implement', status: 'running', title: '[implement] missing', created_at: 120, elapsed_seconds: 125 }); delete c.active_duration_seconds; return c })(),
  card({ id: 't_dp', tenant: 'tozes-factonio-dur', phase: 'implement', status: 'running', title: '[implement] positive', created_at: 130, active_duration_seconds: 65, elapsed_seconds: 3600 })
]

function walk(node, visit, anc = []) {
  if (node == null || typeof node !== 'object') return
  if (Array.isArray(node)) { node.forEach(n => walk(n, visit, anc)); return }
  visit(node, anc)
  walk(node.props.children, visit, [...anc, node])
}
function findAll(tree, pred) {
  const out = []
  walk(tree, (n, anc) => { if (pred(n)) out.push({ node: n, anc }) })
  return out
}
function find(tree, pred) { return findAll(tree, pred)[0] || null }
const byText = (type, t) => n => n.type === type && text(n).trim() === t
const byAttr = (k, v) => n => n.props[k] === v
function fire(hit) {
  if (!hit) throw new Error('click target not found')
  const ev = { stopped: false, prevented: false, stopPropagation() { this.stopped = true }, preventDefault() { this.prevented = true } }
  const results = []
  if (typeof hit.node.props.onClick === 'function') results.push(hit.node.props.onClick(ev))
  for (const a of [...hit.anc].reverse()) {
    if (ev.stopped) break
    if (typeof a.props.onClick === 'function') results.push(a.props.onClick(ev))
  }
  return { stopped: ev.stopped, prevented: ev.prevented, results }
}
const tick = () => new Promise(r => setTimeout(r, 0))

function makeStorage(init = {}) {
  const data = { ...init }
  const sets = []
  return { data, sets, get: (k, fb) => (Object.hasOwn(data, k) ? data[k] : fb), set: (k, v) => { data[k] = v; sets.push([k, v]) }, remove: k => { delete data[k] } }
}
function makeOs() {
  const os = { opened: [], clipboard: [], openExternal: async u => { os.opened.push(u); return true }, writeClipboard: async t => { os.clipboard.push(t); return true } }
  return os
}
function mountPane({ storage, os, focused = null } = {}) {
  host.state.focusedStoredSessionId = focused
  const regs = []
  const rests = []
  const ctx = { rest: async p => { rests.push(p); return { cards: [] } }, onDispose: () => {}, registerMany: items => regs.push(...items) }
  if (storage) ctx.storage = storage
  if (os) ctx.os = os
  plugin.register(ctx)
  const pane = regs.find(r => r.id === 'pane')
  const hooks = makeHooks()
  const render = () => { globalThis.__fcHooks = hooks; cardsQueries.length = 0; return expand(pane.render()) }
  return { ctx, rests, render, hooks }
}
const sectionsOf = tree => findAll(tree, n => n.type === 'section').map(h => ({
  label: h.node.props['aria-label'],
  state: h.node.props['data-state'],
  tenant: h.node.props['data-tenant'],
  expanded: find(h.node, n => n.props['data-action'] === 'toggle-ws')?.node.props['aria-expanded'],
  stages: findAll(h.node, n => n.type === 'li').map(s => s.node.props['aria-label']),
  cards: findAll(h.node, n => n.type === 'article').map(a => a.node.props['data-id']),
  mini: !!find(h.node, n => n.props.className === 'fc-sb-mini'),
  prShortcut: find(find(h.node, n => n.props.className === 'fc-sb-ws-head').node, n => n.type === 'a')?.node.props.href || null,
  doneToggle: (() => { const d = find(h.node, n => n.props.className === 'fc-sb-done-toggle'); return d ? { expanded: d.node.props['aria-expanded'], text: text(d.node).replace(/\s+/g, ' ').trim() } : null })(),
  text: text(h.node).replace(/\s+/g, ' ')
}))
const tilesOf = tree => Object.fromEntries(findAll(tree, n => n.props.className === 'fc-sb-stat').map(h => [h.node.props['data-value'], { n: text(find(h.node, x => x.props.className === 'fc-sb-stat-n').node).trim(), pressed: h.node.props['aria-pressed'], hot: h.node.props['data-hot'] || null }]))
const segOf = (tree, label) => Object.fromEntries(find(tree, n => n.props.role === 'group' && n.props['aria-label'] === label).node.props.children.filter(Boolean).map(b => [b.props['data-value'], b.props['aria-pressed']]))
function headerOf(tree) {
  const head = find(tree, n => n.props.className === 'fc-sb-head')
  if (!head) return null
  return {
    title: text(find(head.node, n => n.type === 'h2').node).trim(),
    count: text(find(head.node, n => n.props.className === 'fc-sb-count').node).trim(),
    dashboard: !!find(head.node, byAttr('data-action', 'dashboard')),
    scope: segOf(head.node, 'Scope'),
    phase: segOf(head.node, 'Filter by phase'),
    tiles: tilesOf(head.node)
  }
}
const emptyOf = tree => {
  const e = find(tree, n => n.props.className === 'fc-sb-empty')
  if (!e) return null
  const b = find(e.node, n => n.type === 'button')
  return { title: text(find(e.node, n => n.type === 'h3').node).trim(), text: text(e.node).replace(/\s+/g, ' ').trim(), cta: b ? text(b.node).trim() : null }
}
const lastCardsQuery = () => { const q = cardsQueries[cardsQueries.length - 1]; return q ? { key: q.key, enabled: q.enabled, interval: q.interval } : null }
const allButtonLabels = tree => findAll(tree, n => n.type === 'button').map(h => text(h.node).trim())

async function runSidebarScenarios(report, registered, oldRegistered) {
  const sb = report.sidebar = {}
  const pane = registered.find(r => r.id === 'pane')
  sb.paneRegistration = { title: pane.title, area: pane.area, data: pane.data, renderType: typeof pane.render }
  const pal = registered.find(r => r.id === 'show-pane')
  sb.palette = pal ? { area: pal.area, id: pal.data.id, label: pal.data.label, runType: typeof pal.data.run } : null
  sb.oldHostHasPane = oldRegistered.some(r => r.id === 'pane') && oldRegistered.some(r => r.id === 'show-pane')

  // Palette + StatusPill reveal (modern) and /factonio fallback (older host).
  const pill = registered.find(r => r.id === 'status-pill')
  const revealCase = () => {
    reveals.length = 0; navigations.length = 0
    pal.data.run()
    const paletteRes = { reveals: [...reveals], navigations: [...navigations] }
    reveals.length = 0; navigations.length = 0
    globalThis.__fcHooks = makeHooks()
    const tree = expand(pill.render())
    fire(find(tree, n => n.type === 'button'))
    return { palette: paletteRes, pill: { reveals: [...reveals], navigations: [...navigations] } }
  }
  sb.revealModern = revealCase()
  const savedReveal = host.revealPane
  delete host.revealPane
  sb.revealOld = revealCase()
  host.revealPane = savedReveal

  // Visibility-aware polling.
  cardsState = { data: { cards: CARDS }, isLoading: false, isError: false }
  const vis = {}
  paneVisible = true; visibilityCalls.length = 0
  let m = mountPane({ focused: 'sess_9c41e2' })
  m.render(); vis.visible = lastCardsQuery(); vis.calls = [...visibilityCalls]
  paneVisible = false
  m.render(); vis.hidden = lastCardsQuery()
  paneVisible = true
  const savedVis = host.paneVisibility
  delete host.paneVisibility
  m = mountPane({ focused: 'sess_9c41e2' })
  m.render(); vis.olderHost = lastCardsQuery()
  host.paneVisibility = savedVis
  sb.visibility = vis

  // Scope: defaults to This thread when a session is focused; switching persists via ctx.storage.
  const scope = {}
  let storage = makeStorage()
  m = mountPane({ storage, focused: 'sess_9c41e2' })
  let tree = m.render()
  scope.initial = { header: headerOf(tree), query: lastCardsQuery() }
  tree = m.render()
  const q = cardsQueries[cardsQueries.length - 1]
  await q.fn(); scope.restCalls = [...m.rests]
  fire(find(tree, byText('button', 'All sessions')))
  tree = m.render()
  scope.afterAll = { header: headerOf(tree), query: lastCardsQuery(), storageSets: [...storage.sets] }
  await cardsQueries[cardsQueries.length - 1].fn(); scope.restCallsAfter = [...m.rests]
  // focus change keeps the explicit choice
  host.state.focusedStoredSessionId = 'sess_other'
  tree = m.render(); scope.afterFocusChange = headerOf(tree).scope
  // remount restores persisted choice
  m = mountPane({ storage, focused: 'sess_9c41e2' })
  tree = m.render(); scope.restored = headerOf(tree).scope
  // invalid persisted values fall back safely
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'bogus', 'sidebar.status': 42, 'sidebar.phase': 'nope' }), focused: null })
  tree = m.render(); scope.invalid = { scope: headerOf(tree).scope, phase: headerOf(tree).phase, tiles: headerOf(tree).tiles }
  sb.scope = scope

  // Populated, All sessions.
  const pop = {}
  storage = makeStorage({ 'sidebar.scope': 'all' })
  const os = makeOs()
  m = mountPane({ storage, os, focused: 'sess_9c41e2' })
  tree = m.render()
  pop.header = headerOf(tree)
  pop.sections = sectionsOf(tree)
  pop.noMutation = allButtonLabels(tree).filter(l => /unblock|retry|delete|cancel/i.test(l))
  pop.calloutsByCard = Object.fromEntries(findAll(tree, n => n.type === 'article').map(h => [h.node.props['data-id'], findAll(h.node, n => n.props.className === 'fc-sb-callout').map(c => ({ tone: c.node.props['data-tone'], kind: c.node.props['data-kind'], role: c.node.props.role, text: text(c.node).replace(/\s+/g, ' ').trim() }))]))
  pop.cardTops = Object.fromEntries(findAll(tree, n => n.type === 'article').map(h => [h.node.props['data-id'], { attn: h.node.props['data-attn'], open: h.node.props['data-open'], top: text(find(h.node, n => n.props.className === 'fc-sb-card-top').node).replace(/\s+/g, ' ').trim(), title: text(find(h.node, n => n.type === 'h4').node).trim(), fullTitle: find(h.node, n => n.type === 'h4').node.props.title, dot: find(h.node, n => n.props.className === 'fc-sb-dot').node.props['data-status'] }]))

  // Dashboard button
  navigations.length = 0
  fire(find(tree, byAttr('data-action', 'dashboard')))
  pop.dashboardNav = [...navigations]

  // "N completed" disclosure expands on click
  const sidebarSec = () => find(tree, n => n.type === 'section' && n.props['data-tenant'] === SIDEBAR)
  fire(find(sidebarSec().node, n => n.props.className === 'fc-sb-done-toggle'))
  tree = m.render()
  pop.afterDisclosure = sectionsOf(tree).find(s => s.tenant === SIDEBAR)
  // Completed workstream expands on click, showing pipeline + every card, no disclosure
  fire(find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === PILL).node, byAttr('data-action', 'toggle-ws')))
  tree = m.render()
  pop.completedExpanded = sectionsOf(tree).find(s => s.tenant === PILL)

  // Expandable card details (click the card body)
  const art = id => find(tree, n => n.type === 'article' && n.props['data-id'] === id)
  fire(find(art('t_a1b2c3d4').node, n => n.type === 'h4'))
  tree = m.render()
  const drawer = find(art('t_a1b2c3d4').node, n => n.type === 'dl')
  pop.drawer = {
    open: art('t_a1b2c3d4').node.props['data-open'],
    toggleExpanded: find(art('t_a1b2c3d4').node, byAttr('data-action', 'toggle-card')).node.props['aria-expanded'],
    ariaControls: find(art('t_a1b2c3d4').node, byAttr('data-action', 'toggle-card')).node.props['aria-controls'],
    id: drawer?.node.props.id,
    fields: drawer ? Object.fromEntries(findAll(drawer.node, n => n.type === 'dd').map(h => [h.node.props['data-field'], text(h.node).replace(/\s+/g, ' ').trim()])) : null,
    topHasRework: text(find(art('t_a1b2c3d4').node, n => n.props.className === 'fc-sb-card-top').node).includes('rework')
  }
  // click inside the drawer does not collapse the card
  fire(find(drawer.node, n => n.type === 'dd'))
  tree = m.render()
  pop.drawerClickKeepsOpen = art('t_a1b2c3d4').node.props['data-open']
  // keyboard toggle (chevron) collapses it
  const chev = fire(find(art('t_a1b2c3d4').node, byAttr('data-action', 'toggle-card')))
  tree = m.render()
  pop.chevronCollapses = { open: art('t_a1b2c3d4').node.props['data-open'], stopped: chev.stopped }
  // blocked card drawer: parents + block callout unclamped
  fire(find(art('t_4ab36a14').node, byAttr('data-action', 'toggle-card')))
  tree = m.render()
  pop.blockedDrawer = Object.fromEntries(findAll(find(art('t_4ab36a14').node, n => n.type === 'dl').node, n => n.type === 'dd').map(h => [h.node.props['data-field'], text(h.node).replace(/\s+/g, ' ').trim()]))

  // PR link: https only, opens via ctx.os.openExternal, does not toggle the card.
  const prHit = find(art('t_a1b2c3e0').node, n => n.type === 'a')
  pop.prLink = { href: prHit.node.props.href, target: prHit.node.props.target, rel: prHit.node.props.rel, label: text(prHit.node).trim() }
  const prFire = fire(prHit)
  await tick()
  tree = m.render()
  pop.prLink.stopped = prFire.stopped
  pop.prLink.prevented = prFire.prevented
  pop.prLink.opened = [...os.opened]
  pop.prLink.cardOpenAfter = art('t_a1b2c3e0').node.props['data-open']
  pop.prLink.windowOpens = windowOpens.length

  // Copy card ID: ctx.os.writeClipboard + host.notify, no toggle.
  notifications.length = 0
  const cp = fire(find(art('t_a1b2c3e0').node, byAttr('data-action', 'copy-id')))
  await Promise.all(cp.results); await tick()
  tree = m.render()
  pop.copy = { stopped: cp.stopped, clipboard: [...os.clipboard], notifications: [...notifications], cardOpenAfter: art('t_a1b2c3e0').node.props['data-open'], label: find(art('t_a1b2c3e0').node, byAttr('data-action', 'copy-id')).node.props['aria-label'] }

  // Status filter: Done auto-expands the completed disclosure; clicking the pressed tile resets to All.
  const filters = {}
  fire(find(tree, n => n.props.className === 'fc-sb-stat' && n.props['data-value'] === 'done'))
  tree = m.render()
  filters.done = { tiles: headerOf(tree).tiles, sections: sectionsOf(tree).map(s => ({ tenant: s.tenant, cards: s.cards, doneToggle: s.doneToggle })) }
  fire(find(tree, n => n.props.className === 'fc-sb-stat' && n.props['data-value'] === 'done'))
  tree = m.render()
  filters.doneToggledOff = headerOf(tree).tiles.all.pressed
  fire(find(tree, n => n.props.className === 'fc-sb-stat' && n.props['data-value'] === 'attention'))
  tree = m.render()
  filters.attention = sectionsOf(tree).map(s => ({ tenant: s.tenant, cards: s.cards }))
  fire(find(tree, n => n.props.className === 'fc-sb-stat' && n.props['data-value'] === 'active'))
  tree = m.render()
  filters.active = sectionsOf(tree).map(s => ({ tenant: s.tenant, cards: s.cards }))
  fire(find(tree, n => n.props.className === 'fc-sb-stat' && n.props['data-value'] === 'all'))
  fire(find(find(tree, n => n.props['aria-label'] === 'Filter by phase').node, byText('button', 'Verify')))
  tree = m.render()
  filters.verify = { phase: headerOf(tree).phase, sections: sectionsOf(tree).map(s => ({ tenant: s.tenant, cards: s.cards, doneToggle: s.doneToggle })) }
  // Filter mismatch: Attention · Design
  fire(find(find(tree, n => n.props['aria-label'] === 'Filter by phase').node, byText('button', 'Design')))
  tree = m.render()
  fire(find(tree, n => n.props.className === 'fc-sb-stat' && n.props['data-value'] === 'attention'))
  tree = m.render()
  filters.mismatch = { empty: emptyOf(tree), header: headerOf(tree), sections: sectionsOf(tree).length }
  filters.storageSets = [...storage.sets]
  fire(find(tree, byText('button', 'Clear filters')))
  tree = m.render()
  filters.cleared = { header: headerOf(tree), sections: sectionsOf(tree).length, storage: { status: storage.data['sidebar.status'], phase: storage.data['sidebar.phase'] } }
  // remount restores persisted filters
  storage.set('sidebar.status', 'attention'); storage.set('sidebar.phase', 'implement')
  m = mountPane({ storage, os, focused: 'sess_9c41e2' })
  tree = m.render()
  filters.restored = { header: headerOf(tree), sections: sectionsOf(tree).map(s => ({ tenant: s.tenant, cards: s.cards })) }
  // Regression: a disclosure collapsed under All must not stay collapsed when entering Done.
  const reentry = {}
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'all' }), os, focused: 'sess_9c41e2' })
  tree = m.render()
  const secOf = tenant => sectionsOf(tree).find(s => s.tenant === tenant)
  const toggleDone = tenant => { fire(find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === tenant).node, n => n.props.className === 'fc-sb-done-toggle')); tree = m.render() }
  const statusTile = v => { fire(find(tree, n => n.props.className === 'fc-sb-stat' && n.props['data-value'] === v)); tree = m.render() }
  toggleDone(SIDEBAR); reentry.allExpanded = secOf(SIDEBAR).doneToggle
  toggleDone(SIDEBAR); reentry.allCollapsed = { doneToggle: secOf(SIDEBAR).doneToggle, cards: secOf(SIDEBAR).cards }
  statusTile('done'); reentry.done = { doneToggle: secOf(SIDEBAR).doneToggle, cards: secOf(SIDEBAR).cards }
  toggleDone(SIDEBAR); reentry.doneCollapsed = { doneToggle: secOf(SIDEBAR).doneToggle, cards: secOf(SIDEBAR).cards }
  toggleDone(SIDEBAR); reentry.doneReexpanded = secOf(SIDEBAR).doneToggle
  toggleDone(SIDEBAR)
  statusTile('all'); reentry.backToAll = { doneToggle: secOf(SIDEBAR).doneToggle, cards: secOf(SIDEBAR).cards }
  statusTile('done'); reentry.doneAgain = secOf(SIDEBAR).doneToggle
  filters.reentry = reentry
  sb.populated = pop
  sb.filters = filters

  // Fallbacks: no ctx.os -> window.open + navigator.clipboard; no ctx.storage -> in-memory state.
  const fb = {}
  windowOpens.length = 0; navClipboard.length = 0; notifications.length = 0
  m = mountPane({ focused: null })
  tree = m.render()
  const fbArt = id => find(tree, n => n.type === 'article' && n.props['data-id'] === id)
  fire(find(fbArt('t_a1b2c3e0').node, n => n.type === 'a'))
  const fcp = fire(find(fbArt('t_a1b2c3e0').node, byAttr('data-action', 'copy-id')))
  await Promise.all(fcp.results); await tick()
  fb.windowOpens = windowOpens.map(a => a)
  fb.navClipboard = [...navClipboard]
  fb.notifications = [...notifications]
  fire(find(find(tree, n => n.props['aria-label'] === 'Filter by phase').node, byText('button', 'Define')))
  tree = m.render()
  fb.noStoragePhase = headerOf(tree).phase
  // Copy failure -> error toast
  notifications.length = 0
  const failOs = { openExternal: async () => false, writeClipboard: async () => false }
  const savedNav = globalThis.navigator
  globalThis.navigator = {}
  windowOpens.length = 0
  m = mountPane({ os: failOs, focused: null })
  tree = m.render()
  const fart = find(tree, n => n.type === 'article' && n.props['data-id'] === 't_a1b2c3e0')
  const ff = fire(find(fart.node, byAttr('data-action', 'copy-id')))
  await Promise.all(ff.results)
  fire(find(fart.node, n => n.type === 'a')); await tick(); await tick()
  fb.copyFailure = [...notifications]
  fb.openExternalFalseFallsBack = windowOpens.length
  globalThis.navigator = savedNav
  sb.fallbacks = fb

  // Empty / loading / error states — header always rendered.
  const st = {}
  cardsState = { data: { cards: CARDS }, isLoading: false, isError: false }
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'thread' }), focused: null })
  tree = m.render()
  st.unfocused = { header: headerOf(tree), empty: emptyOf(tree), query: lastCardsQuery(), sections: sectionsOf(tree).length }
  fire(find(tree, byText('button', 'Switch to All sessions')))
  tree = m.render()
  st.unfocusedAfterCta = { scope: headerOf(tree).scope, sections: sectionsOf(tree).length, query: lastCardsQuery() }

  cardsState = { data: { cards: [], counts: {} }, isLoading: false, isError: false }
  m = mountPane({ focused: 'sess_e0e0e0' })
  tree = m.render()
  st.threadEmpty = { header: headerOf(tree), empty: emptyOf(tree), query: lastCardsQuery() }
  fire(find(tree, byText('button', 'Show All sessions')))
  tree = m.render()
  st.threadEmptyAfterCta = { scope: headerOf(tree).scope, empty: emptyOf(tree), query: lastCardsQuery() }

  cardsState = { data: undefined, isLoading: true, isError: false }
  m = mountPane({ focused: 'sess_9c41e2' })
  tree = m.render()
  st.loading = {
    header: headerOf(tree),
    progress: !!find(tree, n => n.props.className === 'fc-sb-progress' && n.props.role === 'progressbar'),
    busy: find(tree, n => n.props.className === 'fc-sb-body').node.props['aria-busy'],
    skeletons: findAll(tree, n => n.props.className === 'fc-sb-skel').length,
    empty: emptyOf(tree)
  }

  cardsState = { data: { cards: CARDS }, isLoading: false, isError: true, error: new Error('boom'), dataUpdatedAt: Date.now() - 2 * 60000 }
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'all' }), focused: 'sess_9c41e2' })
  tree = m.render()
  const banner = find(tree, n => n.props.className === 'fc-sb-banner')
  st.errorStale = { header: headerOf(tree), banner: banner ? { role: banner.node.props.role, text: text(banner.node).trim() } : null, sections: sectionsOf(tree).length, progress: !!find(tree, n => n.props.className === 'fc-sb-progress') }
  cardsState = { data: undefined, isLoading: false, isError: true, error: new Error('boom') }
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'all' }), focused: null })
  tree = m.render()
  const banner2 = find(tree, n => n.props.className === 'fc-sb-banner')
  st.errorNoData = { header: headerOf(tree), banner: banner2 ? text(banner2.node).trim() : null, empty: emptyOf(tree) }
  cardsState = { data: { cards: [] }, isLoading: false, isError: false }
  m = mountPane({ focused: null })
  tree = m.render()
  st.allEmpty = { header: headerOf(tree), empty: emptyOf(tree) }
  sb.states = st

  // Multi-round recovery, Ungrouped bucket, unsafe PR URLs.
  cardsState = { data: { cards: RECOVERY }, isLoading: false, isError: false }
  m = mountPane({ focused: null })
  tree = m.render()
  const rec = { header: headerOf(tree), sections: sectionsOf(tree) }
  const u = find(tree, n => n.type === 'section' && n.props['data-tenant'] === '')
  rec.ungroupedAnchors = u ? findAll(u.node, n => n.type === 'a').map(h => h.node.props.href) : null
  rec.ungroupedCallouts = u ? findAll(u.node, n => n.props.className === 'fc-sb-callout').map(c => text(c.node).replace(/\s+/g, ' ').trim()) : null
  fire(find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === 'tozes-factonio-recover').node, byAttr('data-action', 'toggle-ws')))
  tree = m.render()
  rec.recoverExpanded = sectionsOf(tree).find(s => s.tenant === 'tozes-factonio-recover')
  sb.recovery = rec

  // Superseded phase cards: expand every workstream and read its pipeline stages.
  cardsState = { data: { cards: SUPERSEDED }, isLoading: false, isError: false }
  m = mountPane({ focused: null })
  tree = m.render()
  for (const s of sectionsOf(tree)) {
    if (s.expanded === 'false') {
      fire(find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === s.tenant).node, byAttr('data-action', 'toggle-ws')))
      tree = m.render()
    }
  }
  sb.superseded = Object.fromEntries(sectionsOf(tree).map(s => [s.tenant.slice(SUP.length), { state: s.state, stages: s.stages }]))

  // Duration rendering on card rows.
  cardsState = { data: { cards: DURATION }, isLoading: false, isError: false }
  m = mountPane({ focused: null })
  tree = m.render()
  sb.durations = Object.fromEntries(findAll(tree, n => n.type === 'article').map(h => {
    const d = find(h.node, n => n.props.className === 'fc-sb-dur').node
    return [h.node.props['data-id'], { text: text(d).trim(), title: d.props.title }]
  }))
}
const mod = await import(process.env.FC_PLUGIN_URL)
const plugin = mod.default
const report = { id: plugin.id, name: plugin.name }

// Modern host
const registered = []
const settingsPages = []
const ctx = {
  rest: async () => MODELS,
  onDispose: () => {},
  registerMany: items => registered.push(...items),
  registerSettingsPage: page => settingsPages.push(page)
}
plugin.register(ctx)
report.settingsPages = settingsPages.map(p => ({
  id: p.id, title: p.title, icon: p.icon, renderType: typeof p.render, keys: Object.keys(p).sort()
}))
report.registeredIds = registered.map(r => r.id)

const page = registered.find(r => r.id === 'page')
function snapshot(renderFn) {
  globalThis.__fcHooks = makeHooks()
  const tree = expand(renderFn())
  return { text: text(tree), clicks: clickAll(tree) }
}
const settingsRender = settingsPages[0]?.render
report.settings = { full: snapshot(settingsRender) }
report.dashboard = { full: snapshot(page.render) }

queryState = { data: { models: MODELS.models, cross_model_warning: null }, isLoading: false, isError: false }
report.settings.noWarn = snapshot(settingsRender)
report.dashboard.noWarn = snapshot(page.render)

queryState = { data: undefined, isLoading: true, isError: false }
report.settings.loading = snapshot(settingsRender)
report.dashboard.loading = snapshot(page.render)

queryState = { data: undefined, isLoading: false, isError: true, error: new Error('boom') }
report.settings.error = snapshot(settingsRender)
report.dashboard.error = snapshot(page.render)
report.modelsQueries = globalThis.__fcModelsQueries.slice(0, 2)

// Older host without registerSettingsPage
const oldRegistered = []
let oldErr = null
try {
  plugin.register({ rest: async () => MODELS, onDispose: () => {}, registerMany: items => oldRegistered.push(...items) })
} catch (e) { oldErr = String(e && e.stack || e) }
report.oldHost = { error: oldErr, registeredIds: oldRegistered.map(r => r.id) }

await runSidebarScenarios(report, registered, oldRegistered)

process.stdout.write(JSON.stringify(report))
"""


@unittest.skipUnless(shutil.which("node"), "node is required for desktop plugin checks")
class TestDesktopPlugin(unittest.TestCase):
    report: dict

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", SMOKE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": os.environ.get("PATH", ""), "FC_PLUGIN_URL": PLUGIN_JS.as_uri()},
        )
        if proc.returncode != 0:
            raise AssertionError(f"node smoke failed ({proc.returncode}):\n{proc.stderr}\n{proc.stdout}")
        cls.report = json.loads(proc.stdout)

    # --- static checks -------------------------------------------------
    def test_node_check_syntax(self):
        proc = subprocess.run(["node", "--check", str(PLUGIN_JS)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_desktop_sdk_surface_lint_clean(self):
        from hermes_cli.plugin_validate_desktop import desktop_review_findings, desktop_surface_findings

        source = PLUGIN_JS.read_text(encoding="utf-8")
        self.assertEqual(desktop_surface_findings(source), [])
        self.assertEqual(desktop_review_findings(source), [])
        for banned in ("window.hermesDesktop", "data-slot", "data-tour", "data-sidebar", "data-testid", "querySelector"):
            self.assertNotIn(banned, source)

    # --- registration --------------------------------------------------
    def test_plugin_id_matches_manifest_and_schema_intact(self):
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "factonio")
        self.assertEqual(self.report["id"], manifest["name"])
        schema = manifest.get("config_schema")
        self.assertTrue(schema, "config_schema must remain present")
        self.assertIsInstance(schema, dict)
        keys = set(schema)
        for k in (
            "repos_root",
            "max_concurrent_workers",
            "default_merge_policy",
            "default_max_fix_rounds",
            "watchdog_interval_minutes",
            "stale_heartbeat_minutes",
            "blocked_reminder_minutes",
            "repos",
        ):
            self.assertIn(k, keys)

    def test_settings_page_registration_contract(self):
        pages = self.report["settingsPages"]
        self.assertEqual(len(pages), 1)
        page = pages[0]
        self.assertEqual(page["id"], "settings")
        self.assertEqual(page["title"], "Factonio")
        self.assertEqual(page["icon"], "circuit-board")
        self.assertEqual(page["renderType"], "function")
        self.assertNotIn("children", page["keys"])  # no manual Agent settings duplicate
        for rid in ("page", "nav", "open", "pane", "status-pill", "your-turn"):
            self.assertIn(rid, self.report["registeredIds"])

    def test_older_host_without_register_settings_page(self):
        old = self.report["oldHost"]
        self.assertIsNone(old["error"])
        self.assertIn("page", old["registeredIds"])

    # --- settings page rendering ----------------------------------------
    def test_settings_page_renders_note_models_and_warning(self):
        t = self.report["settings"]["full"]["text"]
        self.assertIn("Auxiliary Models", t)
        self.assertIn("Settings → Model → Auxiliary", t)
        for phase in PHASES:
            self.assertIn(phase, t)
        self.assertIn("anthropic/claude-x", t)
        self.assertIn("openai/gpt-y", t)
        self.assertIn("solo-model", t)
        self.assertIn("(profile default)", t)
        self.assertIn("auxiliary.factonio_define", t)
        self.assertIn("—", t)
        self.assertIn("Verification uses the same model as Implementation!", t)
        self.assertNotIn("Verification uses the same model", self.report["settings"]["noWarn"]["text"])
        self.assertNotIn("Verification uses the same model", self.report["dashboard"]["noWarn"]["text"])
        self.assertIn("Verification uses the same model", self.report["dashboard"]["full"]["text"])

    def test_models_query_contract(self):
        q = self.report["modelsQueries"][0]
        self.assertEqual(q["key"], ["factonio", "models"])
        self.assertEqual(q["interval"], 30000)
        self.assertEqual(q["fn"], "function")

    # --- deep links ------------------------------------------------------
    def _assert_deep_links(self, clicks):
        navs = [c["navigated"] for c in clicks if c["navigated"]]
        top = [c for c in clicks if c["label"] == "Configure phase models"]
        self.assertEqual(len(top), 1, clicks)
        self.assertEqual(top[0]["navigated"], [aux_route("define")])
        flat = [n for nav in navs for n in nav]
        for phase in PHASES:
            self.assertIn(aux_route(phase), flat)
        # one per-phase button per phase plus the top-level button
        aux = [n for n in flat if n.startswith("/settings?tab=config:model&aux=factonio_")]
        self.assertEqual(len(aux), len(PHASES) + 1, flat)

    def test_settings_page_deep_links(self):
        for state in ("full", "noWarn", "loading", "error"):
            with self.subTest(state=state):
                self._assert_deep_links(self.report["settings"][state]["clicks"])

    def test_dashboard_deep_links(self):
        for state in ("full", "noWarn", "loading", "error"):
            with self.subTest(state=state):
                self._assert_deep_links(self.report["dashboard"][state]["clicks"])
                self.assertIn("Phase Model Configuration", self.report["dashboard"][state]["text"])

    def test_loading_and_error_states_degrade_gracefully(self):
        for state in ("loading", "error"):
            t = self.report["settings"][state]["text"]
            self.assertIn("Auxiliary Models", t)
            for phase in PHASES:
                self.assertIn(phase, t)
            self.assertIn("(profile default)", t)
        self.assertIn("Could not load", self.report["settings"]["error"]["text"])

    def test_dashboard_retains_aux_models_note_in_all_states(self):
        for state in ("full", "noWarn", "loading", "error"):
            with self.subTest(state=state):
                t = self.report["dashboard"][state]["text"]
                self.assertIn("Hermes Auxiliary Models", t)
                self.assertIn("Settings → Model → Auxiliary", t)
                for phase in PHASES:
                    self.assertIn(phase, t)
                self.assertIn("(profile default)" if state in ("loading", "error") else "anthropic/claude-x", t)
        self.assertIn("Could not load", self.report["dashboard"]["error"]["text"])
        self.assertNotIn("Could not load", self.report["dashboard"]["loading"]["text"])
        self.assertNotIn("Could not load", self.report["dashboard"]["full"]["text"])

    # --- left-sidebar tab: registration & reveal --------------------------
    def test_pane_registers_as_hideable_left_sidebar_tab(self):
        reg = self.report["sidebar"]["paneRegistration"]
        self.assertEqual(reg["title"], "Factonio")
        self.assertEqual(reg["area"], "panes")
        self.assertEqual(reg["renderType"], "function")
        self.assertEqual(
            reg["data"],
            {
                "placement": "left",
                "width": "280px",
                "collapsible": True,
                "hideOnly": True,
                "dock": {"pane": "sessions", "pos": "center", "enforce": True},
            },
        )
        self.assertIn("show-pane", self.report["registeredIds"])
        self.assertTrue(self.report["sidebar"]["oldHostHasPane"])

    def test_show_pane_palette_command(self):
        pal = self.report["sidebar"]["palette"]
        self.assertEqual(pal, {"area": "palette", "id": "factonio.showPane", "label": "Show Factonio Sidebar", "runType": "function"})
        self.assertEqual(self.report["sidebar"]["revealModern"]["palette"], {"reveals": ["factonio:pane"], "navigations": []})
        self.assertEqual(self.report["sidebar"]["revealOld"]["palette"], {"reveals": [], "navigations": ["/factonio"]})

    def test_status_pill_reveals_sidebar_with_fallback(self):
        self.assertEqual(self.report["sidebar"]["revealModern"]["pill"], {"reveals": ["factonio:pane"], "navigations": []})
        self.assertEqual(self.report["sidebar"]["revealOld"]["pill"], {"reveals": [], "navigations": ["/factonio"]})

    def test_visibility_aware_polling(self):
        vis = self.report["sidebar"]["visibility"]
        self.assertEqual(vis["calls"], ["factonio:pane"])
        self.assertEqual(vis["visible"]["interval"], 15000)
        self.assertTrue(vis["visible"]["enabled"])
        self.assertIs(vis["hidden"]["interval"], False)
        self.assertTrue(vis["hidden"]["enabled"])
        # older host without host.paneVisibility: always polling
        self.assertEqual(vis["olderHost"]["interval"], 15000)
        self.assertTrue(vis["olderHost"]["enabled"])

    # --- scope & filters ----------------------------------------------------
    def test_scope_defaults_to_thread_and_persists(self):
        sc = self.report["sidebar"]["scope"]
        self.assertEqual(sc["initial"]["header"]["scope"], {"thread": "true", "all": "false"})
        self.assertEqual(sc["initial"]["query"]["key"], ["factonio", "cards", "sess_9c41e2"])
        self.assertEqual(sc["restCalls"], ["/cards?session_id=sess_9c41e2"])
        self.assertEqual(sc["afterAll"]["header"]["scope"], {"thread": "false", "all": "true"})
        self.assertEqual(sc["afterAll"]["query"]["key"], ["factonio", "cards", "all"])
        self.assertEqual(sc["afterAll"]["storageSets"], [["sidebar.scope", "all"]])
        self.assertEqual(sc["restCallsAfter"][-1], "/cards")
        self.assertEqual(sc["afterFocusChange"], {"thread": "false", "all": "true"})
        self.assertEqual(sc["restored"], {"thread": "false", "all": "true"})
        # invalid persisted values fall back safely
        self.assertEqual(sc["invalid"]["scope"], {"thread": "false", "all": "true"})
        self.assertEqual(sc["invalid"]["phase"]["all"], "true")
        self.assertEqual(sc["invalid"]["tiles"]["all"]["pressed"], "true")

    def test_status_counts_and_header(self):
        h = self.report["sidebar"]["populated"]["header"]
        self.assertEqual(h["title"], "Factonio")
        self.assertEqual(h["count"], "16")
        self.assertTrue(h["dashboard"])
        self.assertEqual({k: v["n"] for k, v in h["tiles"].items()}, {"all": "16", "active": "3", "attention": "2", "done": "12"})
        self.assertEqual(h["tiles"]["attention"]["hot"], "true")
        self.assertEqual(list(h["phase"]), ["all", "define", "design", "implement", "verify"])
        self.assertEqual(self.report["sidebar"]["populated"]["dashboardNav"], ["/factonio"])

    def test_status_and_phase_filters(self):
        f = self.report["sidebar"]["filters"]
        self.assertEqual(f["attention"], [{"tenant": "tozes-factonio-improve-factonio-sidebar", "cards": ["t_4ab36a14", "t_4ab36a11"]}])
        self.assertEqual(f["active"], [{"tenant": "tozes-factonio-enrich-cards-metadata", "cards": ["t_a1b2c3e0", "t_a1b2c3d4", "t_a1b2c3e1"]}])
        self.assertEqual(f["doneToggledOff"], "true")  # clicking the pressed tile returns to All
        verify_cards = [c for sec in f["verify"]["sections"] for c in sec["cards"]]
        self.assertEqual(sorted(verify_cards), sorted(["t_4ab36a11", "t_4ab36a10", "t_a1b2c3e0", "t_a1b2c3e1", "t_39c1aa04", "t_39c1aa05"]))
        self.assertEqual(f["verify"]["phase"]["verify"], "true")
        # Done filter auto-expands the "N completed" disclosure
        done = {s["tenant"]: s for s in f["done"]["sections"]}
        self.assertEqual(done["tozes-factonio-improve-factonio-sidebar"]["doneToggle"]["expanded"], "true")
        self.assertEqual(done["tozes-factonio-enrich-cards-metadata"]["doneToggle"]["expanded"], "true")
        self.assertEqual(f["done"]["tiles"]["done"]["pressed"], "true")

    def test_done_filter_expands_after_disclosure_collapsed_under_all(self):
        r = self.report["sidebar"]["filters"]["reentry"]
        completed = ["t_4ab36a09", "t_4ab36a0b", "t_4ab36a0d", "t_4ab36a10"]
        self.assertEqual(r["allExpanded"]["expanded"], "true")
        self.assertEqual(r["allCollapsed"]["doneToggle"]["expanded"], "false")
        self.assertTrue(set(completed).isdisjoint(r["allCollapsed"]["cards"]))
        # entering Done auto-expands despite the earlier collapse under All
        self.assertEqual(r["done"]["doneToggle"]["expanded"], "true")
        for cid in completed:
            self.assertIn(cid, r["done"]["cards"])
        # the user can still collapse / re-expand while in Done
        self.assertEqual(r["doneCollapsed"]["doneToggle"]["expanded"], "false")
        self.assertTrue(set(completed).isdisjoint(r["doneCollapsed"]["cards"]))
        self.assertEqual(r["doneReexpanded"]["expanded"], "true")
        # returning to All restores the All choice (collapsed); re-entering Done auto-expands again
        self.assertEqual(r["backToAll"]["doneToggle"]["expanded"], "false")
        self.assertTrue(set(completed).isdisjoint(r["backToAll"]["cards"]))
        self.assertEqual(r["doneAgain"]["expanded"], "true")

    def test_filters_persist_via_ctx_storage(self):
        f = self.report["sidebar"]["filters"]
        self.assertIn(["sidebar.status", "done"], f["storageSets"])
        self.assertIn(["sidebar.phase", "verify"], f["storageSets"])
        self.assertEqual(f["cleared"]["storage"], {"status": "all", "phase": "all"})
        self.assertEqual(f["restored"]["header"]["tiles"]["attention"]["pressed"], "true")
        self.assertEqual(f["restored"]["header"]["phase"]["implement"], "true")
        self.assertEqual(f["restored"]["sections"], [{"tenant": "tozes-factonio-improve-factonio-sidebar", "cards": ["t_4ab36a14"]}])
        # older host without ctx.storage still filters in memory
        self.assertEqual(self.report["sidebar"]["fallbacks"]["noStoragePhase"]["define"], "true")

    # --- empty, loading & error states ---------------------------------------
    def _assert_header(self, header):
        self.assertIsNotNone(header)
        self.assertEqual(header["title"], "Factonio")
        self.assertTrue(header["dashboard"])
        self.assertEqual(set(header["scope"]), {"thread", "all"})
        self.assertEqual(set(header["tiles"]), {"all", "active", "attention", "done"})
        self.assertEqual(set(header["phase"]), {"all", "define", "design", "implement", "verify"})

    def test_header_always_rendered(self):
        st = self.report["sidebar"]["states"]
        for name in ("unfocused", "threadEmpty", "loading", "errorStale", "errorNoData", "allEmpty"):
            with self.subTest(state=name):
                self._assert_header(st[name]["header"])
        self._assert_header(self.report["sidebar"]["filters"]["mismatch"]["header"])

    def test_unfocused_thread_empty_state(self):
        st = self.report["sidebar"]["states"]
        self.assertEqual(st["unfocused"]["empty"]["title"], "No thread in focus")
        self.assertEqual(st["unfocused"]["empty"]["cta"], "Switch to All sessions")
        self.assertFalse(st["unfocused"]["query"]["enabled"])
        self.assertEqual(st["unfocused"]["sections"], 0)
        self.assertEqual(st["unfocusedAfterCta"]["scope"], {"thread": "false", "all": "true"})
        self.assertEqual(st["unfocusedAfterCta"]["sections"], 3)

    def test_thread_zero_cards_empty_state(self):
        st = self.report["sidebar"]["states"]
        self.assertEqual(st["threadEmpty"]["empty"]["title"], "No cards in this thread")
        self.assertEqual(st["threadEmpty"]["empty"]["cta"], "Show All sessions")
        self.assertEqual(st["threadEmptyAfterCta"]["query"]["key"], ["factonio", "cards", "all"])
        self.assertEqual(st["allEmpty"]["empty"]["title"], "No Factonio cards yet")
        self.assertIsNone(st["allEmpty"]["empty"]["cta"])

    def test_filter_mismatch_empty_state(self):
        f = self.report["sidebar"]["filters"]
        self.assertEqual(f["mismatch"]["empty"]["title"], "No cards match these filters")
        self.assertEqual(f["mismatch"]["empty"]["cta"], "Clear filters")
        self.assertIn("Attention · Design hides all 16 cards", f["mismatch"]["empty"]["text"])
        self.assertEqual(f["mismatch"]["sections"], 0)
        self.assertEqual(f["cleared"]["sections"], 3)

    def test_loading_and_error_states(self):
        st = self.report["sidebar"]["states"]
        self.assertTrue(st["loading"]["progress"])
        self.assertEqual(st["loading"]["busy"], "true")
        self.assertEqual(st["loading"]["skeletons"], 3)
        self.assertEqual(st["loading"]["header"]["count"], "…")
        self.assertEqual(st["loading"]["header"]["tiles"]["all"]["n"], "–")
        self.assertEqual(st["errorStale"]["banner"]["role"], "status")
        self.assertIn("Couldn’t refresh cards", st["errorStale"]["banner"]["text"])
        self.assertIn("2 min ago", st["errorStale"]["banner"]["text"])
        self.assertEqual(st["errorStale"]["sections"], 3)  # stale data preserved
        self.assertIn("Couldn’t refresh cards", st["errorNoData"]["banner"])
        self.assertEqual(st["errorNoData"]["empty"]["title"], "No Factonio cards yet")

    # --- workstreams, pipeline & cards ---------------------------------------
    def test_workstream_grouping_ordering_and_pipeline(self):
        secs = self.report["sidebar"]["populated"]["sections"]
        self.assertEqual(
            [s["label"] for s in secs],
            [
                "improve-factonio-sidebar workstream, attention",
                "enrich-cards-metadata workstream, active",
                "statusbar-pill-counts workstream, completed",
            ],
        )
        self.assertEqual(secs[0]["stages"], ["Define: done", "Design: done", "Implement: blocked", "Verify: diff done, black-box rejected"])
        self.assertEqual(secs[1]["stages"], ["Define: done", "Design: skipped", "Implement: queued", "Verify: diff running, black-box queued"])
        self.assertIn("factonio · 5/6 done", secs[0]["text"])
        self.assertIn("2 attn", secs[0]["text"])
        self.assertEqual(secs[0]["prShortcut"], "https://github.com/tozes/factonio/pull/44")
        # attention first, then running, then queued within a workstream
        self.assertEqual(secs[0]["cards"], ["t_4ab36a14", "t_4ab36a11"])
        self.assertEqual(secs[1]["cards"], ["t_a1b2c3e0", "t_a1b2c3d4", "t_a1b2c3e1"])

    def test_completed_workstream_collapsed_by_default(self):
        pop = self.report["sidebar"]["populated"]
        done_ws = pop["sections"][2]
        self.assertEqual(done_ws["expanded"], "false")
        self.assertTrue(done_ws["mini"])
        self.assertEqual(done_ws["cards"], [])
        self.assertEqual(done_ws["stages"], [])
        self.assertEqual(done_ws["prShortcut"], "https://github.com/tozes/factonio/pull/39")
        exp = pop["completedExpanded"]
        self.assertEqual(exp["expanded"], "true")
        self.assertEqual(len(exp["cards"]), 5)
        self.assertIsNone(exp["doneToggle"])
        self.assertEqual(exp["stages"], ["Define: done", "Design: done", "Implement: done", "Verify: diff done, black-box done"])

    def test_completed_cards_disclosure(self):
        pop = self.report["sidebar"]["populated"]
        ws = pop["sections"][0]
        self.assertEqual(ws["doneToggle"], {"expanded": "false", "text": "4 completed define · design · implement · diff"})
        self.assertEqual(pop["sections"][1]["doneToggle"]["text"], "2 completed define · implement")
        after = pop["afterDisclosure"]
        self.assertEqual(after["doneToggle"]["expanded"], "true")
        self.assertEqual(after["cards"], ["t_4ab36a14", "t_4ab36a11", "t_4ab36a09", "t_4ab36a0b", "t_4ab36a0d", "t_4ab36a10"])

    def test_multi_round_verify_recovery_and_ungrouped(self):
        rec = self.report["sidebar"]["recovery"]
        labels = [s["label"] for s in rec["sections"]]
        self.assertEqual(labels, ["Ungrouped workstream, attention", "recover workstream, completed"])
        self.assertEqual(
            rec["recoverExpanded"]["stages"],
            ["Define: done", "Design: skipped", "Implement: done", "Verify: diff done, black-box done"],
        )
        self.assertEqual(rec["header"]["tiles"]["attention"]["n"], "1")  # resolved reject is not attention
        self.assertEqual(rec["ungroupedCallouts"], ["triage Waiting for triage"])

    def test_pipeline_resolves_latest_card_per_phase(self):
        sup = self.report["sidebar"]["superseded"]
        verify = {k: sup[k]["stages"][3] for k in ("rq", "rr", "rp", "rj")}
        self.assertEqual(verify["rq"], "Verify: diff done, black-box queued")
        self.assertEqual(verify["rr"], "Verify: diff done, black-box running")
        self.assertEqual(verify["rp"], "Verify: diff done, black-box done")
        self.assertEqual(verify["rj"], "Verify: diff done, black-box rejected")
        for k in ("rq", "rr", "rp", "rj"):
            self.assertEqual(sup[k]["stages"][:3], ["Define: done", "Design: skipped", "Implement: done"])
        # Historical rejection stays workstream attention until a newer verification passes.
        self.assertEqual(sup["rq"]["state"], "attention")
        self.assertEqual(sup["rr"]["state"], "attention")
        self.assertEqual(sup["rp"]["state"], "completed")
        self.assertEqual(sup["rj"]["state"], "attention")
        self.assertIn("diff running", sup["tie"]["stages"][-1])

    def test_pipeline_superseded_non_verify_cards(self):
        sup = self.report["sidebar"]["superseded"]
        self.assertIn("Implement: running", sup["ibr"]["stages"])
        self.assertIn("Implement: done", sup["ibd"]["stages"])
        self.assertIn("Implement: blocked", sup["irb"]["stages"])

    def test_zero_active_duration_is_not_replaced_by_wall_time(self):
        d = self.report["sidebar"]["durations"]
        self.assertEqual(d["t_dz"]["text"], "0s")
        self.assertEqual(d["t_dz"]["title"], "Active 0s · Wall 1h 0m")
        self.assertEqual(d["t_dn"]["text"], "1h 0m")
        self.assertEqual(d["t_dm"]["text"], "2m 5s")
        self.assertEqual(d["t_dp"]["text"], "1m 5s")

    def test_card_rows_and_callouts(self):
        pop = self.report["sidebar"]["populated"]
        tops = pop["cardTops"]
        self.assertEqual(tops["t_4ab36a11"]["dot"], "reject")
        self.assertEqual(tops["t_4ab36a11"]["attn"], "true")
        self.assertEqual(tops["t_4ab36a11"]["title"], "Keyboard and theme acceptance run")
        self.assertEqual(tops["t_4ab36a11"]["fullTitle"], "[verify-blackbox] Keyboard and theme acceptance run")
        self.assertIn("black-box", tops["t_4ab36a11"]["top"])
        self.assertIn("reject", tops["t_4ab36a11"]["top"])
        self.assertIn("rework", tops["t_a1b2c3d4"]["top"])
        co = pop["calloutsByCard"]
        self.assertEqual(co["t_4ab36a14"][0]["tone"], "warn")
        self.assertEqual(co["t_4ab36a14"][0]["kind"], "needs input")
        self.assertEqual(co["t_4ab36a14"][0]["role"], "note")
        self.assertIn("persist across app restarts", co["t_4ab36a14"][0]["text"])
        self.assertEqual(co["t_4ab36a11"][0]["kind"], "rejected")
        self.assertEqual(co["t_4ab36a11"][0]["tone"], "danger")
        self.assertEqual(co["t_a1b2c3e0"][0]["kind"], "error")
        self.assertEqual(co["t_a1b2c3d4"], [])
        self.assertEqual(pop["noMutation"], [])

    def test_expandable_card_detail_drawer(self):
        pop = self.report["sidebar"]["populated"]
        d = pop["drawer"]
        self.assertEqual(d["open"], "true")
        self.assertEqual(d["toggleExpanded"], "true")
        self.assertEqual(d["ariaControls"], d["id"])
        f = d["fields"]
        self.assertEqual(f["model"], "anthropic/claude-sonnet-4-5")
        self.assertIn("Rework · 2 runs", f["runs"])
        self.assertEqual(f["active"], "3m 5s")
        self.assertEqual(f["wall"], "6m 52s")
        self.assertEqual(f["completed"], "—")
        self.assertNotEqual(f["created"], "—")
        self.assertEqual(f["parents"], "t_7d21c0e5 t_7d21c0e6")
        self.assertEqual(f["outcome"], "review requested")
        self.assertIn("Added additive card fields", f["summary"])
        self.assertFalse(d["topHasRework"])  # the drawer carries the rework tag when open
        self.assertEqual(pop["drawerClickKeepsOpen"], "true")
        self.assertEqual(pop["chevronCollapses"], {"open": "false", "stopped": True})
        self.assertEqual(pop["blockedDrawer"]["parents"], "t_4ab36a11")
        self.assertEqual(pop["blockedDrawer"]["runs"], "1")

    def test_pr_link_opens_external_https_only(self):
        pop = self.report["sidebar"]["populated"]
        pr = pop["prLink"]
        self.assertEqual(pr["href"], "https://github.com/tozes/factonio/pull/42")
        self.assertEqual(pr["rel"], "noopener noreferrer")
        self.assertEqual(pr["label"], "factonio#42")
        self.assertTrue(pr["stopped"])
        self.assertTrue(pr["prevented"])
        self.assertEqual(pr["opened"], ["https://github.com/tozes/factonio/pull/42"])
        self.assertEqual(pr["cardOpenAfter"], "false")
        self.assertEqual(pr["windowOpens"], 0)
        fb = self.report["sidebar"]["fallbacks"]
        self.assertEqual(fb["windowOpens"], [["https://github.com/tozes/factonio/pull/42", "_blank", "noopener,noreferrer"]])
        self.assertEqual(fb["openExternalFalseFallsBack"], 1)
        # javascript: and http: PR URLs never render a link
        self.assertEqual(self.report["sidebar"]["recovery"]["ungroupedAnchors"], [])

    def test_copy_card_id(self):
        cp = self.report["sidebar"]["populated"]["copy"]
        self.assertTrue(cp["stopped"])
        self.assertEqual(cp["clipboard"], ["t_a1b2c3e0"])
        self.assertEqual(cp["notifications"], [{"kind": "info", "message": "Copied card ID"}])
        self.assertEqual(cp["cardOpenAfter"], "false")
        self.assertEqual(cp["label"], "Copy card ID t_a1b2c3e0")
        fb = self.report["sidebar"]["fallbacks"]
        self.assertEqual(fb["navClipboard"], ["t_a1b2c3e0"])
        self.assertEqual(fb["notifications"], [{"kind": "info", "message": "Copied card ID"}])
        self.assertEqual(fb["copyFailure"], [{"kind": "error", "message": "Failed to copy card ID"}])

    def test_sidebar_styles_use_theme_tokens_only(self):
        source = PLUGIN_JS.read_text(encoding="utf-8")
        start = source.index("const STYLES = `")
        styles = source[start : source.index("`", start + len("const STYLES = `"))]
        sb_rules = [line for line in styles.splitlines() if ".fc-sb" in line]
        self.assertTrue(sb_rules)
        for line in sb_rules:
            for m in re.finditer(r"background(?:-color)?\s*:\s*([^;]+);", line):
                value = m.group(1)
                self.assertNotRegex(value, r"#[0-9a-fA-F]{3,8}\b|rgba?\(", line)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)
        self.assertNotIn(".fc-pane {", styles)
        for kept in (".fc-page", ".fc-settings", ".fc-step", ".fc-pill"):
            self.assertIn(kept, styles)
        for imp in re.findall(r"^import .* from '([^']+)'", source, re.M):
            self.assertIn(imp, ("@hermes/plugin-sdk", "react", "react/jsx-runtime"))


if __name__ == "__main__":
    unittest.main()
