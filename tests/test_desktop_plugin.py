"""Clean-room tests for the Factonio Desktop plugin (desktop/plugin.js).

Covers the Settings → Plugins → Factonio landing page registration, the
editable Registered Repositories table on /factonio (GET/PATCH /repos), the
Auxiliary Models deep-links on both the settings page and the /factonio
dashboard, the hideable right-sidebar Factonio tab docked beside Files
(registration, reveal, visibility-aware polling, scope/status/phase filters, workstream pipeline,
card details and quick actions), the Decision Trail (dashboard section,
sidebar strips and card-drawer decisions), older-host compatibility, and
Desktop SDK surface lint.

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
import tempfile
import unittest
from pathlib import Path

try:
    import hermes_yaml as yaml
except ImportError:
    import yaml

from tests._env import PLUGIN_ROOT, isolated_hermes_home

PLUGIN_JS = PLUGIN_ROOT / "desktop" / "plugin.js"
PHASES = ["define", "design", "implement", "verify", "watchdog"]


def aux_route(phase: str) -> str:
    return f"/settings?tab=config:model&aux=factonio_{phase}"


SMOKE_SCRIPT = r"""
import { registerHooks } from 'node:module'
import { readFileSync } from 'node:fs'

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
    export function useQueryClient() { return globalThis.__fcQueryClient }
    export function useValue(v) { return v && typeof v === 'object' && typeof v.get === 'function' ? v.get() : v }
  `,
  'react': `
    export function useState(init) { return globalThis.__fcHooks.useState(init) }
    export function useRef(init) { return globalThis.__fcHooks.useRef(init) }
    export function useEffect(fn, deps) { return globalThis.__fcHooks.useEffect(fn, deps) }
    export default { useState, useRef, useEffect }
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
  const visited = new Set()
  let pending = []
  let path = 'root'
  let idx = 0
  return {
    store,
    visited,
    useState(init) {
      const k = path + '#' + (idx++)
      if (!store.has(k)) store.set(k, typeof init === 'function' ? init() : init)
      return [store.get(k), v => store.set(k, typeof v === 'function' ? v(store.get(k)) : v)]
    },
    useRef(init) {
      const k = path + '#' + (idx++)
      if (!store.has(k)) store.set(k, { current: init })
      return store.get(k)
    },
    useEffect(fn, deps) {
      const k = path + '#' + (idx++)
      const prev = store.get(k)
      const changed = !prev || !deps || !prev.deps || deps.length !== prev.deps.length || deps.some((d, i) => !Object.is(d, prev.deps[i]))
      store.set(k, { deps })
      if (changed) pending.push(fn)
    },
    // Run queued effects (after refs are attached), like React's commit phase.
    flush() { const q = pending; pending = []; q.forEach(fn => fn()) },
    // Drop hook state of components not rendered this pass (React unmount semantics).
    gc() {
      for (const k of [...store.keys()]) if (!visited.has(k.slice(0, k.lastIndexOf('#')))) store.delete(k)
      visited.clear()
    },
    enter(p) { const prev = [path, idx]; path = p; idx = 0; visited.add(p); return prev },
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
let trailState = { data: undefined, isLoading: true, isError: false }
const trailQueries = []
globalThis.__fcUseQuery = opts => {
  const key = opts.queryKey.join('/')
  if (key === 'factonio/models') {
    globalThis.__fcModelsQueries.push({ key: opts.queryKey, interval: opts.refetchInterval, fn: typeof opts.queryFn })
    return queryState
  }
  if (opts.queryKey[1] === 'metrics-history') {
    historyQueries.push({ key: opts.queryKey, interval: opts.refetchInterval, fn: opts.queryFn })
    return globalThis.__fcHistoryState || { data: undefined, isLoading: true, isError: false }
  }
  if (opts.queryKey[1] === 'metrics' && globalThis.__fcMetricsState) {
    return globalThis.__fcMetricsState
  }
  if (key === 'factonio/repos' && globalThis.__fcReposState) {
    return globalThis.__fcReposState
  }
  if (opts.queryKey[1] === 'trail') {
    trailQueries.push({ key: opts.queryKey, interval: opts.refetchInterval, fn: opts.queryFn })
    return typeof trailState === 'function' ? trailState(opts) : trailState
  }
  if (opts.queryKey[1] === 'cards') {
    cardsQueries.push({ key: opts.queryKey, enabled: opts.enabled !== false, interval: opts.refetchInterval, fn: opts.queryFn })
    return typeof cardsState === 'function' ? cardsState(opts) : cardsState
  }
  otherQueries.push(key)
  return { data: undefined, isLoading: true, isError: false }
}
const otherQueries = []
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

// ---------------------------------------------------------------------------
// Registered Repositories editor harness (/factonio page, GET/PATCH /repos)
// ---------------------------------------------------------------------------
const REPO_DEFAULTS = { merge_policy: 'auto', publish_policy: 'ask', max_fix_rounds: 3, design_provider: 'auto', design_approval: 'ask' }
const REPOS = [
  { slug: 'tozes-factonio', repo: 'tozes/factonio', url: 'https://github.com/tozes/factonio', path: '/home/tozes/dev/factonio', default_branch: 'main', merge_policy: 'auto', publish_policy: 'auto', max_fix_rounds: 3, design_provider: null, design_approval: null, effective_design_provider: 'auto', effective_design_approval: 'ask', resolved_design_provider: 'claude-design', has_required_checks: false, models: {} },
  { slug: 'acme-web-dashboard', repo: 'acme/web-dashboard', url: 'https://github.com/acme/web-dashboard', path: '/home/tozes/workspace/projects/acme-web-dashboard', default_branch: 'develop', merge_policy: 'ask', publish_policy: 'ask', max_fix_rounds: 5, design_provider: 'hermes', design_approval: 'auto', effective_design_provider: 'hermes', effective_design_approval: 'auto', resolved_design_provider: 'hermes', has_required_checks: true, models: {} },
  { slug: 'acme-cli-tools', repo: 'acme/cli-tools', url: 'https://github.com/acme/cli-tools', path: '/home/tozes/workspace/acme-cli-tools', default_branch: 'release/v2', merge_policy: 'auto', publish_policy: 'auto', max_fix_rounds: 2, design_provider: 'off', design_approval: null, effective_design_provider: 'off', effective_design_approval: 'ask', resolved_design_provider: 'off', has_required_checks: false, models: {} }
]
function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
const scrollLog = []
function attachRefs(tree, focusLog) {
  walk(tree, n => {
    const ref = n.props.ref
    if (!ref) return
    const name = n.props.role === 'dialog' ? 'dialog' : n.props.id || n.props['aria-label'] || text(n).trim()
    const el = { tag: n.type, name, disabled: !!n.props.disabled, focus: () => focusLog.push(name), scrollIntoView: opts => scrollLog.push([name, opts || null]) }
    if (typeof ref === 'function') ref(el)
    else ref.current = el
  })
}
function mountRepos({ data, client }) {
  const regs = []
  const rests = []
  let restImpl = async () => ({ ok: true })
  const ctx = {
    rest: (p, opts) => { rests.push({ path: p, opts: opts ? JSON.parse(JSON.stringify(opts)) : null }); return restImpl(p, opts) },
    onDispose: () => {},
    registerMany: items => regs.push(...items)
  }
  plugin.register(ctx)
  const page = regs.find(r => r.id === 'page')
  const hooks = makeHooks()
  const focusLog = []
  const refetches = []
  const render = () => {
    globalThis.__fcHooks = hooks
    globalThis.__fcQueryClient = client
    globalThis.__fcReposState = { data, isLoading: false, isError: false, refetch: () => refetches.push(1) }
    const tree = expand(page.render())
    hooks.gc()
    attachRefs(tree, focusLog)
    hooks.flush()
    return tree
  }
  return { rests, render, focusLog, refetches, setRest: fn => { restImpl = fn }, setData: next => { data = next } }
}
const reposSec = tree => find(tree, n => n.type === 'table' && String(n.props.className).includes('fc-repos'))
function readRepos(tree) {
  const tbl = reposSec(tree)
  if (!tbl) return null
  const rows = findAll(tbl.node, n => n.type === 'tr' && n.props['data-slug'] && n.props.className !== 'fc-repo-ed').map(h => {
    const tr = h.node
    const tds = tr.props.children.filter(Boolean)
    const tag = td => { const t = find(td, n => String(n.props.className || '').startsWith('fc-tag fc-tag--')); return t ? { cls: t.node.props.className, text: text(t.node).replace(/\s+/g, ' ').trim(), title: t.node.props.title } : null }
    const btn = find(tr, n => n.type === 'button' && n.props['data-action'] === 'edit-repo').node
    const pol = td => { const p = find(td, n => n.props.className === 'fc-pol'); return { field: td.props['data-field'], text: text(td).trim(), pol: p ? p.node.props['data-pol'] ?? null : null, title: p ? p.node.props.title : null, tag: !!find(td, n => String(n.props.className || '').includes('fc-tag')) } }
    return {
      slug: tr.props['data-slug'],
      editing: tr.props['data-editing'] || null,
      repo: text(find(tds[0], n => n.props.className === 'fc-trunc fc-repo-name').node).trim(),
      path: find(tds[0], n => n.props.className === 'fc-trunc fc-repo-path').node.props.title,
      branch: text(tds[1]).trim(),
      branchTitle: find(tds[1], n => n.type === 'span').node.props.title,
      cells: tds.length,
      merge: text(tds[2]).trim(),
      mergeCell: pol(tds[2]),
      publish: pol(tds[3]),
      rounds: text(tds[4]).trim(),
      roundsClass: tds[4].props.className,
      provider: tag(tds[5]),
      approval: tag(tds[6]),
      edit: { label: text(btn).trim(), ariaLabel: btn.props['aria-label'], expanded: btn.props['aria-expanded'], controls: btn.props['aria-controls'], active: btn.props['data-active'] || null, disabled: !!btn.props.disabled }
    }
  })
  const wrap = find(tree, n => n.props.className === 'fc-repos-wrap')
  const order = findAll(find(tbl.node, n => n.type === 'tbody').node, n => n.type === 'tr').map(h => (h.node.props.className === 'fc-repo-ed' ? 'editor:' : 'row:') + h.node.props['data-slug'])
  return {
    tableClass: tbl.node.props.className,
    wrapped: !!(wrap && find(wrap.node, n => n === tbl.node)),
    cols: findAll(tbl.node, n => n.type === 'col').map(h => h.node.props.className),
    headers: findAll(find(tbl.node, n => n.type === 'thead').node, n => n.type === 'th').map(h => ({ text: text(h.node).replace(/\s+/g, ' ').trim(), className: h.node.props.className || null })),
    rows,
    order
  }
}
function readEditor(tree) {
  const tr = find(tree, n => n.type === 'tr' && n.props.className === 'fc-repo-ed')
  if (!tr) return null
  const td = tr.node.props.children
  const form = find(tr.node, n => n.type === 'form').node
  const fs = find(form, n => n.type === 'fieldset').node
  const fields = {}
  for (const h of findAll(fs, n => n.props.className === 'fc-field')) {
    const f = h.node
    const ctl = find(f, n => n.type === 'input' || n.type === 'select').node
    const err = find(f, n => n.props.className === 'fc-field-err')
    const hint = find(f, n => n.props.className === 'fc-field-hint')
    fields[f.props['data-field']] = {
      label: text(find(f, n => n.type === 'label').node).trim(),
      labelFor: find(f, n => n.type === 'label').node.props.htmlFor,
      id: ctl.props.id,
      tag: ctl.type,
      type: ctl.props.type || null,
      value: ctl.props.value,
      invalid: ctl.props['aria-invalid'] || null,
      describedBy: ctl.props['aria-describedby'],
      err: err ? { id: err.node.props.id, text: text(err.node).trim() } : null,
      hint: hint ? { id: hint.node.props.id, text: text(hint.node).replace(/\s+/g, ' ').trim() } : null,
      options: ctl.type === 'select' ? findAll(ctl, n => n.type === 'option').map(o => [o.node.props.value, text(o.node).trim()]) : null,
      disabledOptions: ctl.type === 'select' ? findAll(ctl, n => n.type === 'option' && n.props.disabled).map(o => [o.node.props.value, text(o.node).trim()]) : null
    }
  }
  const msg = find(form, n => n.props['aria-live'] === 'polite').node
  const alert = find(form, n => n.props.role === 'alert')
  const btn = a => find(form, n => n.type === 'button' && n.props['data-action'] === a).node
  return {
    slug: tr.node.props['data-slug'],
    editing: tr.node.props['data-editing'],
    colSpan: td.props.colSpan,
    formId: form.props.id,
    formLabel: form.props['aria-label'],
    busy: form.props['aria-busy'] || null,
    noValidate: form.props.noValidate,
    identity: text(find(form, n => n.props.className === 'fc-ed-id').node).replace(/\s+/g, ' ').trim(),
    fieldsetClass: fs.props.className,
    fieldsetDisabled: !!fs.props.disabled,
    fieldOrder: findAll(fs, n => n.props.className === 'fc-field').map(h => h.node.props['data-field']),
    fields,
    msg: { text: text(msg).replace(/\s+/g, ' ').trim(), className: msg.props.className },
    alert: alert ? text(alert.node).replace(/\s+/g, ' ').trim() : null,
    cancel: { disabled: !!btn('cancel-edit').props.disabled, type: btn('cancel-edit').props.type },
    save: { label: text(btn('save-repo')).trim(), disabled: !!btn('save-repo').props.disabled, type: btn('save-repo').props.type, spin: !!find(btn('save-repo'), n => n.props.className === 'fc-spin') }
  }
}
const editBtnHit = (tree, slug) => find(tree, n => n.type === 'button' && n.props['data-action'] === 'edit-repo' && n.props['aria-label'] === `Edit ${REPOS.find(r => r.slug === slug).repo}`)
const formOf = tree => find(tree, n => n.type === 'form').node
function change(tree, field, value) {
  const ctl = find(tree, n => (n.type === 'input' || n.type === 'select') && n.props.name === field).node
  ctl.props.onChange({ target: { value } })
}
const submitForm = tree => formOf(tree).props.onSubmit({ preventDefault() {} })
const keyDown = (tree, key) => formOf(tree).props.onKeyDown({ key, preventDefault() {}, stopPropagation() {} })

async function runReposScenarios(report) {
  const r = report.repos = {}
  const validate = mod.validateRepoDraft
  r.exportsValidator = typeof validate
  const v = (default_branch, max_fix_rounds) => Object.keys(validate({ default_branch, max_fix_rounds }))
  const msgs = (default_branch, max_fix_rounds) => validate({ default_branch, max_fix_rounds })
  r.validate = {
    valid: [['main', '3'], ['  feature/x  ', ' 2 '], ['release/v2', 1], ['a.b', '10'], ['x', 99]].map(([b, n]) => v(b, n)),
    branch: Object.fromEntries([['empty', ''], ['blank', '   '], ['null', null], ['space', 'feat x'], ['tab', 'feat\tx'], ['dots', 'feature..bad'], ['dash', '-main'], ['ctrl', 'ma\u0001in'], ['del', 'ma\u007fin']].map(([k, b]) => [k, msgs(b, '3').default_branch || null])),
    rounds: Object.fromEntries([['zero', '0'], ['neg', '-1'], ['float', '1.5'], ['floatNum', 2.5], ['zeroNum', 0], ['text', 'abc'], ['empty', ''], ['null', null], ['bool', true], ['plus', '+3'], ['exp', '1e2'], ['infNum', Infinity], ['nanNum', NaN], ['zeros', '000']].map(([k, n]) => [k, msgs('main', n).max_fix_rounds || null])),
    unsafe: Object.fromEntries([['pow53', '9007199254740992'], ['pow53plus1', '9007199254740993'], ['overflow309', '9'.repeat(309)], ['pow53Num', 9007199254740992], ['bigNum', 1e300]].map(([k, n]) => [k, msgs('main', n).max_fix_rounds || null])),
    safeMax: [['9007199254740991'], [Number.MAX_SAFE_INTEGER], ['0009007199254740991'], ['007']].map(([n]) => v('main', n))
  }
  const refreshedRepo0 = { ...REPOS[0], merge_policy: 'ask', design_provider: 'hermes' }
  r.changesEdits = {
    onlyEdited: mod.repoDraftChanges({ default_branch: 'user-feature' }, refreshedRepo0),
    explicitOriginal: mod.repoDraftChanges({ merge_policy: 'auto', design_provider: '' }, refreshedRepo0),
    equalLive: mod.repoDraftChanges({ merge_policy: 'ask', design_provider: 'hermes', default_branch: ' main ', max_fix_rounds: '003' }, refreshedRepo0),
    none: mod.repoDraftChanges({}, refreshedRepo0)
  }
  r.changes = mod.repoDraftChanges({ default_branch: ' main ', merge_policy: 'ask', max_fix_rounds: '7', design_provider: '', design_approval: 'auto' }, REPOS[1])
  r.describe = {
    ipc422: mod.describeSaveError(new Error("Error invoking remote method 'hermes:api': Error: 422: {\"detail\":\"max_fix_rounds must be >= 1\"}")),
    plain404: mod.describeSaveError(new Error('404: {"detail":"Repository \'x\' is not registered."}')),
    pydantic: mod.describeSaveError(new Error('422: {"detail":[{"msg":"field required"},{"msg":"bad"}]}')),
    noBody: mod.describeSaveError(Object.assign(new Error('boom'), { statusCode: 500 })),
    network: mod.describeSaveError(new Error('fetch failed: connect ECONNREFUSED 127.0.0.1:9119'))
  }

  // Empty state
  let m = mountRepos({ data: { repos: [], defaults: REPO_DEFAULTS } })
  let tree = m.render()
  const reposBlock = find(tree, n => n.props.className === 'fc-sec' && !!find(n, byText('h2', 'Registered Repositories')))
  r.empty = { text: text(reposBlock.node).replace(/\s+/g, ' '), table: !!reposSec(tree), legend: !!find(reposBlock.node, n => n.props.className === 'fc-legend'), sub: !!find(reposBlock.node, n => n.props.className === 'fc-sec-sub') }

  // Populated read state
  const notified = () => [...notifications]
  const invalidations = []
  const client = { invalidateQueries: q => { invalidations.push(JSON.parse(JSON.stringify(q))); return Promise.resolve() } }
  m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
  tree = m.render()
  const head = find(tree, n => n.props.className === 'fc-sec fc-repos-sec')
  r.read = {
    ...readRepos(tree),
    sectionClass: head ? head.node.props.className : null,
    title: text(find(head.node, n => n.type === 'h2').node).trim(),
    sub: text(find(head.node, n => n.props.className === 'fc-sec-sub').node).replace(/\s+/g, ' ').trim(),
    subs: findAll(head.node, n => n.props.className === 'fc-sec-sub').map(h => text(h.node).replace(/\s+/g, ' ').trim()),
    legend: text(find(head.node, n => n.props.className === 'fc-legend').node).replace(/\s+/g, ' ').trim(),
    legendTags: findAll(find(head.node, n => n.props.className === 'fc-legend').node, n => String(n.props.className || '').startsWith('fc-tag ')).map(h => h.node.props.className),
    buttons: allButtonLabels(head.node),
    editor: readEditor(tree)
  }

  // Lifecycle on tozes/factonio
  notifications.length = 0
  const L = r.lifecycle = {}
  fire(editBtnHit(tree, 'tozes-factonio'))
  tree = m.render()
  L.opened = { table: readRepos(tree), editor: readEditor(tree), focus: [...m.focusLog] }
  change(tree, 'design_provider', 'hermes'); tree = m.render()
  L.providerHermes = readEditor(tree)
  change(tree, 'design_provider', 'off'); tree = m.render()
  L.providerOff = readEditor(tree).fields.design_provider.hint
  change(tree, 'design_provider', 'auto'); tree = m.render()
  L.providerAuto = readEditor(tree).fields.design_provider.hint
  change(tree, 'design_provider', ''); tree = m.render()
  L.providerInherit = { hint: readEditor(tree).fields.design_provider.hint, msg: readEditor(tree).msg, save: readEditor(tree).save }
  change(tree, 'default_branch', 'feature..bad'); change(tree, 'max_fix_rounds', '0')
  tree = m.render()
  L.dirtyInvalid = readEditor(tree)
  m.focusLog.length = 0
  await submitForm(tree)
  tree = m.render()
  L.invalidSubmit = { editor: readEditor(tree), rests: [...m.rests], focus: [...m.focusLog] }
  change(tree, 'default_branch', ' develop '); tree = m.render()
  L.liveFixBranch = readEditor(tree)
  change(tree, 'max_fix_rounds', '4'); change(tree, 'design_provider', 'hermes'); tree = m.render()
  L.allFixed = readEditor(tree)

  // Saving state (PATCH in flight), then API error
  let d = deferred()
  m.setRest(() => d.promise)
  const p1 = submitForm(tree)
  tree = m.render()
  L.saving = { editor: readEditor(tree), table: readRepos(tree), rests: [...m.rests] }
  keyDown(tree, 'Escape'); tree = m.render()
  L.escapeWhileSaving = !!readEditor(tree)
  d.reject(new Error("Error invoking remote method 'hermes:api': Error: 422: {\"detail\":\"Invalid default_branch 'develop'.\"}"))
  await p1; tree = m.render()
  L.apiError = { editor: readEditor(tree), table: readRepos(tree), notifications: notified(), invalidations: [...invalidations] }
  // Network error on retry
  m.setRest(async () => { throw new Error('fetch failed') })
  await submitForm(tree); tree = m.render()
  L.networkError = readEditor(tree)
  // Successful retry
  notifications.length = 0
  m.focusLog.length = 0
  d = deferred()
  m.setRest(() => d.promise)
  const p2 = submitForm(tree)
  d.resolve({ ok: true, repo: { ...REPOS[0], default_branch: 'develop' } })
  await p2; tree = m.render()
  L.saved = { editor: readEditor(tree), table: readRepos(tree), notifications: notified(), invalidations: [...invalidations], rests: [...m.rests], focus: [...m.focusLog], refetches: m.refetches.length }

  // Clearing explicit overrides -> null, refetch fallback without invalidateQueries
  const C = r.clear = {}
  m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client: {} })
  tree = m.render()
  fire(editBtnHit(tree, 'acme-web-dashboard')); tree = m.render()
  C.opened = readEditor(tree)
  change(tree, 'design_provider', ''); change(tree, 'design_approval', ''); tree = m.render()
  C.dirty = readEditor(tree)
  notifications.length = 0
  await submitForm(tree); tree = m.render()
  C.after = { rests: [...m.rests], refetches: m.refetches.length, editor: readEditor(tree), notifications: notified() }

  // Cancel / Escape discard unsaved edits; one editor at a time; Edit toggles closed
  const X = r.cancel = {}
  m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
  tree = m.render()
  fire(editBtnHit(tree, 'acme-cli-tools')); tree = m.render()
  X.openedProvider = readEditor(tree).fields.design_provider
  change(tree, 'default_branch', 'scratch'); change(tree, 'merge_policy', 'ask'); tree = m.render()
  X.dirtyMsg = readEditor(tree).msg.text
  m.focusLog.length = 0
  fire(find(formOf(tree), n => n.type === 'button' && n.props['data-action'] === 'cancel-edit')); tree = m.render()
  X.afterCancel = { editor: readEditor(tree), focus: [...m.focusLog], expanded: readRepos(tree).rows[2].edit.expanded }
  fire(editBtnHit(tree, 'acme-cli-tools')); tree = m.render()
  X.reopened = { branch: readEditor(tree).fields.default_branch.value, merge: readEditor(tree).fields.merge_policy.value, msg: readEditor(tree).msg.text, save: readEditor(tree).save }
  change(tree, 'max_fix_rounds', '9'); tree = m.render()
  m.focusLog.length = 0
  keyDown(tree, 'Enter'); tree = m.render()
  X.enterKeepsOpen = !!readEditor(tree)
  keyDown(tree, 'Escape'); tree = m.render()
  X.afterEscape = { editor: readEditor(tree), focus: [...m.focusLog] }
  fire(editBtnHit(tree, 'acme-cli-tools')); tree = m.render()
  X.reopenedAfterEscape = readEditor(tree).fields.max_fix_rounds.value
  change(tree, 'default_branch', 'other'); tree = m.render()
  fire(editBtnHit(tree, 'tozes-factonio')); tree = m.render()
  X.switched = { slug: readEditor(tree).slug, editors: findAll(tree, n => n.type === 'tr' && n.props.className === 'fc-repo-ed').length, order: readRepos(tree).order }
  fire(editBtnHit(tree, 'acme-cli-tools')); tree = m.render()
  X.switchedBackPristine = readEditor(tree).fields.default_branch.value
  fire(editBtnHit(tree, 'acme-cli-tools')); tree = m.render()
  X.toggleClosed = readEditor(tree)
  X.rests = [...m.rests]

  // Background refresh while editing: only user-edited fields are dirty / PATCHed;
  // untouched fields reconcile to refreshed values and are never reverted.
  const B = r.refresh = {}
  m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
  tree = m.render()
  fire(editBtnHit(tree, 'tozes-factonio')); tree = m.render()
  change(tree, 'default_branch', 'user-feature'); tree = m.render()
  B.before = readEditor(tree)
  const refreshed = { ...REPOS[0], merge_policy: 'ask', design_provider: 'hermes', effective_design_provider: 'hermes', resolved_design_provider: 'hermes' }
  m.setData({ repos: [refreshed, REPOS[1], REPOS[2]], defaults: REPO_DEFAULTS })
  tree = m.render()
  B.after = readEditor(tree)
  m.setRest(async () => ({ ok: true }))
  await submitForm(tree); tree = m.render()
  B.rests = [...m.rests]
  B.closed = readEditor(tree) === null
  // Editing a field, then refresh changes that same field: the user's edit wins.
  m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
  tree = m.render()
  fire(editBtnHit(tree, 'tozes-factonio')); tree = m.render()
  change(tree, 'merge_policy', 'ask'); tree = m.render()
  m.setData({ repos: [{ ...REPOS[0], merge_policy: 'auto', max_fix_rounds: 6 }, REPOS[1], REPOS[2]], defaults: REPO_DEFAULTS })
  tree = m.render()
  B.keepEdit = readEditor(tree)
  await submitForm(tree)
  B.keepEditRests = [...m.rests]

  // Post-refresh: explicitly choosing the edit-start value again is a real edit.
  const E = r.editBack = {}
  const openTozes = data => {
    m = mountRepos({ data: { repos: [data || REPOS[0], REPOS[1], REPOS[2]], defaults: REPO_DEFAULTS }, client })
    tree = m.render()
    fire(editBtnHit(tree, 'tozes-factonio')); tree = m.render()
  }
  const refreshTo = patch => {
    m.setData({ repos: [{ ...REPOS[0], ...patch }, REPOS[1], REPOS[2]], defaults: REPO_DEFAULTS })
    tree = m.render()
  }
  const saveOk = async () => {
    m.setRest(async () => ({ ok: true }))
    await submitForm(tree); tree = m.render()
    return [...m.rests]
  }
  // 1. merge_policy auto -> refresh ask -> choose auto
  openTozes(); refreshTo({ merge_policy: 'ask' })
  E.merge = { refreshed: readEditor(tree) }
  change(tree, 'merge_policy', 'auto'); tree = m.render()
  E.merge.chosen = readEditor(tree)
  E.merge.rests = await saveOk()
  // 2. design_provider inherited -> refresh hermes -> choose Inherit global
  openTozes(); refreshTo({ design_provider: 'hermes', effective_design_provider: 'hermes', resolved_design_provider: 'hermes' })
  E.provider = { refreshed: readEditor(tree) }
  change(tree, 'design_provider', ''); tree = m.render()
  E.provider.chosen = readEditor(tree)
  E.provider.rests = await saveOk()
  // 3. branch/rounds main/3 -> refresh develop/5 -> type main/3
  openTozes(); refreshTo({ default_branch: 'develop', max_fix_rounds: 5 })
  E.text = { refreshed: readEditor(tree) }
  change(tree, 'default_branch', 'main'); change(tree, 'max_fix_rounds', '3'); tree = m.render()
  E.text.chosen = readEditor(tree)
  E.text.rests = await saveOk()
  // 4. edit only branch, refresh others, then explicitly choose original values
  openTozes()
  change(tree, 'default_branch', 'user-feature'); tree = m.render()
  refreshTo({ merge_policy: 'ask', design_provider: 'hermes', effective_design_provider: 'hermes', resolved_design_provider: 'hermes' })
  E.mixed = { refreshed: readEditor(tree) }
  m.setRest(async () => ({ ok: true }))
  await submitForm(tree); tree = m.render()
  E.mixed.firstRests = [...m.rests]
  openTozes({ ...REPOS[0] })
  change(tree, 'default_branch', 'user-feature'); tree = m.render()
  refreshTo({ merge_policy: 'ask', design_provider: 'hermes', effective_design_provider: 'hermes', resolved_design_provider: 'hermes' })
  change(tree, 'merge_policy', 'auto'); change(tree, 'design_provider', ''); tree = m.render()
  E.mixed.chosen = readEditor(tree)
  E.mixed.allRests = await saveOk()
  // Reverting an edit to the live value drops it; the field then follows later refreshes.
  openTozes()
  change(tree, 'merge_policy', 'ask'); tree = m.render()
  change(tree, 'merge_policy', 'auto'); tree = m.render()
  E.revert = { msg: readEditor(tree).msg.text, save: readEditor(tree).save }
  refreshTo({ merge_policy: 'ask' })
  E.revert.after = readEditor(tree)

  // Unsafe max_fix_rounds are rejected in the editor: no PATCH, draft preserved.
  const U = r.unsafe = {}
  for (const [k, n] of [['pow53', '9007199254740992'], ['pow53plus1', '9007199254740993'], ['overflow309', '9'.repeat(309)]]) {
    m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
    tree = m.render()
    fire(editBtnHit(tree, 'tozes-factonio')); tree = m.render()
    change(tree, 'max_fix_rounds', n); tree = m.render()
    m.focusLog.length = 0
    await submitForm(tree); tree = m.render()
    const ed = readEditor(tree)
    U[k] = { rests: [...m.rests], value: ed.fields.max_fix_rounds.value, expected: n, invalid: ed.fields.max_fix_rounds.invalid, err: ed.fields.max_fix_rounds.err && ed.fields.max_fix_rounds.err.text, focus: [...m.focusLog], roundsId: ed.fields.max_fix_rounds.id }
  }
  // Numeric unsafe value (e.g. a programmatic draft) is equally rejected.
  m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
  tree = m.render()
  fire(editBtnHit(tree, 'tozes-factonio')); tree = m.render()
  change(tree, 'max_fix_rounds', 9007199254740992); tree = m.render()
  await submitForm(tree); tree = m.render()
  { const ed = readEditor(tree); U.pow53Num = { rests: [...m.rests], value: ed.fields.max_fix_rounds.value, expected: 9007199254740992, invalid: ed.fields.max_fix_rounds.invalid, err: ed.fields.max_fix_rounds.err && ed.fields.max_fix_rounds.err.text } }

  // Publish policy: one-field partial diff, revert drops it, PATCH holds only publish_policy.
  const P = r.publish = {}
  m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
  tree = m.render()
  fire(editBtnHit(tree, 'tozes-factonio')); tree = m.render()
  P.opened = readEditor(tree).fields.publish_policy
  change(tree, 'publish_policy', 'ask'); tree = m.render()
  P.dirty = readEditor(tree)
  change(tree, 'publish_policy', 'auto'); tree = m.render()
  P.reverted = readEditor(tree)
  change(tree, 'publish_policy', 'ask'); tree = m.render()
  m.setRest(async () => ({ ok: true }))
  await submitForm(tree); tree = m.render()
  P.rests = [...m.rests]
  P.closed = readEditor(tree) === null
  P.changes = mod.repoDraftChanges({ publish_policy: 'auto' }, REPOS[1])
  P.legacySeed = mod.seedRepoDraft({ ...REPOS[2], publish_policy: undefined }).publish_policy
  P.unknownSeed = mod.seedRepoDraft({ ...REPOS[2], publish_policy: null }).publish_policy
  P.fixUnknown = mod.repoDraftChanges({ publish_policy: 'auto' }, { ...REPOS[2], publish_policy: null })
  const { publish_policy: _omit, ...legacyRepo2 } = REPOS[2]
  P.seeds = {
    omittedNoDefaults: mod.seedRepoDraft(legacyRepo2).publish_policy,
    omittedDefaultAsk: mod.seedRepoDraft(legacyRepo2, { publish_policy: 'ask' }).publish_policy,
    omittedDefaultNull: mod.seedRepoDraft(legacyRepo2, { publish_policy: null }).publish_policy,
    omittedDefaultBogus: mod.seedRepoDraft(legacyRepo2, { publish_policy: 'bogus' }).publish_policy,
    nullWithDefaultAuto: mod.seedRepoDraft({ ...REPOS[2], publish_policy: null }, { publish_policy: 'auto' }).publish_policy,
    bogus: mod.seedRepoDraft({ ...REPOS[2], publish_policy: 'bogus' }, { publish_policy: 'auto' }).publish_policy,
    explicitAsk: mod.seedRepoDraft({ ...REPOS[2], publish_policy: 'ask' }, { publish_policy: 'auto' }).publish_policy
  }
  P.omittedNoSpurious = mod.repoDraftChanges({ default_branch: 'main' }, legacyRepo2, { publish_policy: 'auto' })
  { // Valid ask repo: still only the two policy options, no unknown placeholder.
    m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
    tree = m.render()
    fire(editBtnHit(tree, 'acme-web-dashboard')); tree = m.render()
    P.openedAsk = readEditor(tree).fields.publish_policy
  }
  const subsOf = t => findAll(find(t, n => n.props.className === 'fc-sec fc-repos-sec').node, n => n.props.className === 'fc-sec-sub').map(h => text(h.node).replace(/\s+/g, ' ').trim())

  // Explicit null policy (repo + global default): unknown, never auto; choosing auto is an edit.
  const N = r.publishNull = {}
  m = mountRepos({ data: { repos: REPOS.map((x, i) => (i === 0 ? { ...x, publish_policy: null } : i === 2 ? { ...x, publish_policy: 'bogus' } : x)), defaults: { ...REPO_DEFAULTS, publish_policy: null } }, client })
  tree = m.render()
  N.subs = subsOf(tree)
  N.cells = readRepos(tree).rows.map(x => x.publish)
  fire(editBtnHit(tree, 'tozes-factonio')); tree = m.render()
  N.opened = readEditor(tree)
  change(tree, 'publish_policy', 'auto'); tree = m.render()
  N.dirty = readEditor(tree)
  await submitForm(tree); tree = m.render()
  N.rests = [...m.rests]
  m = mountRepos({ data: { repos: REPOS, defaults: { ...REPO_DEFAULTS, publish_policy: 'bogus' } }, client })
  N.bogusDefaultSubs = subsOf(m.render())
  { const { publish_policy: _p, ...noPolicyDefaults } = REPO_DEFAULTS
    m = mountRepos({ data: { repos: REPOS, defaults: noPolicyDefaults }, client })
    N.omittedDefaultSubs = subsOf(m.render()) }

  // Older backend: no publish_policy key on any repo; falls back to the global default cleanly.
  const O = r.publishOmitted = {}
  const legacyRepos = REPOS.map(({ publish_policy: _x, ...x }) => x)
  m = mountRepos({ data: { repos: legacyRepos, defaults: { ...REPO_DEFAULTS, publish_policy: 'auto' } }, client })
  tree = m.render()
  O.cells = readRepos(tree).rows.map(x => x.publish)
  fire(editBtnHit(tree, 'tozes-factonio')); tree = m.render()
  O.opened = readEditor(tree)
  change(tree, 'default_branch', 'trunk'); tree = m.render()
  O.dirty = readEditor(tree)
  await submitForm(tree); tree = m.render()
  O.rests = [...m.rests]
  m = mountRepos({ data: { repos: legacyRepos, defaults: REPO_DEFAULTS }, client })
  O.cellsAskDefault = readRepos(m.render()).rows.map(x => x.publish.text)

  globalThis.__fcReposState = undefined
  globalThis.__fcQueryClient = undefined
}

// ---------------------------------------------------------------------------
// Decision Trail harness (/factonio TrailSection + sidebar strips / card drawers)
// ---------------------------------------------------------------------------
const W2 = 'omnim-ai-factonio-wave-2-watchdog-liveness'
const ACME_T = 'acme-api-i42'
const dec = o => ({ tenant: W2, repo_slug: 'omnim-ai-factonio', supersedes_id: null, superseded_by: [], is_superseded: false, evidence: {}, ...o })
// Same sample ledger as the approved design/preview.html (API order: newest first).
const TRAIL = [
  dec({ id: 'd_f012c8a47b99', ts: '2026-11-25T16:40:00.000000Z', phase: 'merge', card_id: 't_bcf5502a', actor: 'orchestrator', decision: 'pr_merged', why: 'Squash-merged PR #12 after GraphQL readback confirmed all four gate tiers green.', evidence: { pr_number: 12, head_sha: '5d178304', tests_passed: 114 }, result: 'merged' }),
  dec({ id: 'd_7e30f91b5c04', ts: '2026-11-25T16:25:00.000000Z', phase: 'escape', card_id: 't_1658b10a', actor: 'orchestrator', decision: 'escape_gap_opened', why: 'A liveness bug from #9 reached main despite a passing verify.', evidence: { escaped_from: 'omnim-ai/factonio#9', card_id: 't_1658b10a' }, result: 'recorded' }),
  dec({ id: 'd_c4f08e2a91b7', ts: '2026-11-25T16:12:00.000000Z', phase: 'verify', card_id: 't_bcf5502a', actor: 'orchestrator', decision: 'verify_carryover', why: 'Reused the prior verify-diff pass after git patch-id matched the unchanged hunks.', evidence: { pr_number: 12, head_sha: '5d178304', patch_id: '91e4f0a2c7b1', tests_passed: 114, tests_failed: 0 }, result: 'pass', supersedes_id: 'd_9a1e4c0b2f31' }),
  dec({ id: 'd_2d67a4e8f012', ts: '2026-11-25T15:58:00.000000Z', phase: 'watchdog', card_id: 't_caf57a3b', actor: 'watchdog', decision: 'stall_detected', why: 'Worker 48219 is alive but has made no side effect for 27 minutes.', evidence: { card_id: 't_caf57a3b', worker_pid: 48219, side_effect_age_s: 1620 }, result: 'stalled' }),
  dec({ id: 'd_9a1e4c0b2f31', ts: '2026-11-25T15:41:00.000000Z', phase: 'verify', card_id: 't_bcf5502a', actor: 'orchestrator', decision: 'pr_gate_wait', why: 'Held the PR gate: e2e-linux failed once on a known-flaky timeout.', evidence: { pr_number: 12, head_sha: '5d178304', failing_check: 'e2e-linux', nested: { a: 1 } }, result: 'inconclusive', superseded_by: ['d_c4f08e2a91b7'], is_superseded: true }),
  dec({ id: 'd_11aa82b90c3e', ts: '2026-11-25T15:02:00.000000Z', tenant: ACME_T, repo_slug: 'acme-api', phase: 'implement', card_id: 't_77d0e3a1', actor: 'worker', decision: 'spawn_implement', why: 'Spawned the implement worker for issue #42.', evidence: { card_id: 't_77d0e3a1', pr_number: 7 }, result: 'spawned' }),
  dec({ id: 'd_6f19b3d28e45', ts: '2026-11-25T14:38:00.000000Z', phase: 'design', card_id: 't_8bee4979', actor: 'human', decision: 'design_approved', why: 'Approved the Decision Trail UI.', evidence: { card_id: 't_8bee4979', design_provider: 'claude-design' }, result: 'approved' }),
  dec({ id: 'd_4b81e2c0a119', ts: '2026-11-25T14:05:00.123456Z', phase: 'define', card_id: 't_5a2c91e0', actor: 'worker', decision: 'attack_the_premise_pivot', why: 'Pivoted to a dedicated append-only trail.db.', evidence: { card_id: 't_5a2c91e0', rejected: 'kanban.db table' }, result: 'pivot' })
]
const TRAIL_SUMMARY = { total: 8, active: 7, superseded: 1, by_phase: {}, by_actor: {}, by_tenant: { [W2]: 7, [ACME_T]: 1 }, latest_ts: TRAIL[0].ts }
const TRAIL_DATA = { decisions: TRAIL, summary: TRAIL_SUMMARY, filters: { tenant: null, repo: null, phase: null, card_id: null, limit: 200 } }
const TRAIL_REPOS = [
  { ...REPOS[0], slug: 'omnim-ai-factonio', repo: 'omnim-ai/factonio' },
  { ...REPOS[1], slug: 'acme-api', repo: 'acme/api' }
]
const norm = n => text(n).replace(/\s+/g, ' ').trim()
const dhSection = tree => find(tree, n => n.type === 'section' && n.props['aria-labelledby'] === 'fc-trail-h')
function readDhRow(h) {
  const r = h.node
  const main = find(r, hasCls('fc-dh-main')).node
  const res = find(main, hasCls('fc-dh-result')).node
  const why = find(main, hasCls('fc-dh-why'))
  const chain = find(r, hasCls('fc-dh-chain'))
  const insp = find(r, hasCls('fc-dh-insp'))
  return {
    id: r.props['data-id'],
    domId: r.props.id,
    tier: r.props['data-tier'],
    accent: r.props['data-accent'] ?? null,
    human: r.props['data-human'] ?? null,
    superseded: r.props['data-superseded'],
    flash: r.props['data-flash'],
    expanded: r.props['aria-expanded'],
    inFold: h.anc.some(hasCls('fc-dh-foldrows')),
    datetime: find(main, n => n.type === 'time').node.props.dateTime,
    clock: norm(find(main, hasCls('fc-dh-time')).node),
    phase: norm(find(main, hasCls('fc-dh-phase')).node),
    actor: find(main, hasCls('fc-dh-actor')).node.props['data-actor'],
    title: norm(find(main, n => n.type === 'h3').node.props.children.filter(c => !(c && typeof c === 'object' && hasCls('fc-dh-actor')(c)))),
    rawTitle: find(main, n => n.type === 'h3').node.props.title,
    result: norm(res),
    tone: res.props['data-tone'] ?? null,
    why: why ? norm(why.node) : null,
    chev: (() => { const c = find(main, byAttr('data-action', 'toggle-row')).node; return { expanded: c.props['aria-expanded'], controls: c.props['aria-controls'], label: c.props['aria-label'] } })(),
    pills: findAll(main, n => hasCls('fc-chip')(n) && n.props['data-ev']).map(pl => ({ ev: pl.node.props['data-ev'], text: norm(pl.node), tone: pl.node.props['data-tone'] ?? null, href: pl.node.props.href ?? null, title: pl.node.props.title ?? null, type: pl.node.type })),
    chain: chain ? {
      text: norm(find(chain.node, byAttr('data-action', 'toggle-chain')).node),
      expanded: find(chain.node, byAttr('data-action', 'toggle-chain')).node.props['aria-expanded'],
      items: findAll(chain.node, hasCls('fc-dh-chain-item')).map(i => ({ id: i.node.props['data-id'] ?? null, missing: i.node.props['data-missing'] ?? null, text: norm(i.node) }))
    } : null,
    insp: insp ? {
      id: insp.node.props.id,
      tabs: findAll(insp.node, n => n.type === 'button' && n.props['data-tab']).map(b => [b.node.props['data-tab'], b.node.props['aria-pressed']]),
      kv: (() => { const dl = find(insp.node, hasCls('fc-dh-kv')); if (!dl) return null; const out = {}; for (const dd of findAll(dl.node, n => n.type === 'dd')) out[dd.node.props['data-k']] = norm(dd.node); return out })(),
      json: (() => { const j = find(insp.node, hasCls('fc-dh-json')); return j ? text(j.node) : null })(),
      supersede: !!find(insp.node, byAttr('data-action', 'supersede'))
    } : null
  }
}
function readTrail(tree) {
  const sec = dhSection(tree)
  if (!sec) return null
  const s = sec.node
  const q = pred => find(s, pred)
  const bar = q(hasCls('fc-dh-bar'))
  const stateBox = q(n => n.props.className === 'fc-trail-state')
  const alert = q(n => n.props.role === 'alert' && hasCls('fc-trail-alert')(n))
  const live = q(n => n.props.className === 'fc-trail-sr' && n.props['aria-live'] === 'polite')
  const auditBox = q(hasCls('fc-dh-audit'))
  const logBtn = q(byAttr('data-action', 'log-decision'))
  return {
    state: s.props['data-state'],
    preset: s.props['data-preset'],
    busy: s.props['aria-busy'],
    labelledBy: s.props['aria-labelledby'],
    heading: { text: norm(q(n => n.type === 'h2').node), id: q(n => n.type === 'h2').node.props.id },
    sub: norm(q(hasCls('fc-sec-sub')).node),
    total: norm(q(byAttr('data-field', 'total')).node),
    auditClean: (() => { const c = q(byAttr('data-field', 'audit-clean')); return c ? norm(c.node) : null })(),
    logBtn: logBtn ? { text: norm(logBtn.node), disabled: !!logBtn.node.props.disabled } : null,
    audit: auditBox ? {
      head: norm(find(auditBox.node, byAttr('data-action', 'toggle-audit')).node),
      expanded: find(auditBox.node, byAttr('data-action', 'toggle-audit')).node.props['aria-expanded'],
      cats: findAll(auditBox.node, n => n.props['data-kind']).map(h => [h.node.props['data-kind'], norm(h.node), h.node.props['data-tone']]),
      items: findAll(auditBox.node, hasCls('fc-dh-audit-item')).map(h => ({ id: h.node.props['data-id'], text: norm(h.node) }))
    } : null,
    bar: bar ? {
      disabled: bar.node.props['data-disabled'],
      role: bar.node.props.role,
      presets: findAll(bar.node, byAttr('data-action', 'preset')).map(h => ({ value: h.node.props['data-value'], pressed: h.node.props['aria-pressed'], disabled: !!h.node.props.disabled, text: norm(h.node) })),
      q: (() => { const i = find(bar.node, byAttr('data-filter', 'q')).node; return { value: i.props.value, placeholder: i.props.placeholder, disabled: !!i.props.disabled } })(),
      selects: Object.fromEntries(findAll(bar.node, n => n.type === 'select').map(h => [h.node.props['data-filter'], {
        value: h.node.props.value, set: h.node.props['data-set'], disabled: !!h.node.props.disabled,
        options: findAll(h.node, n => n.type === 'option').map(o => [o.node.props.value, norm(o.node)]),
        groups: findAll(h.node, n => n.type === 'optgroup').map(g => [g.node.props.label, findAll(g.node, n => n.type === 'option').map(o => [o.node.props.value, norm(o.node), !!o.node.props.disabled])])
      }])),
      sup: (() => { const c = find(bar.node, byAttr('data-filter', 'sup')).node; return { checked: c.props.checked, disabled: !!c.props.disabled } })(),
      meta: norm(find(bar.node, byAttr('data-field', 'shown')).node)
    } : null,
    scroll: !!q(hasCls('fc-dh-scroll')),
    days: findAll(s, hasCls('fc-dh-day')).map(h => norm(h.node)),
    folds: findAll(s, hasCls('fc-dh-foldwrap')).map(h => {
      const b = find(h.node, byAttr('data-action', 'toggle-fold')).node
      return { key: h.node.props['data-fold'], expanded: b.props['aria-expanded'], text: norm(b), phase: norm(find(b, hasCls('fc-dh-phase')).node), rows: findAll(h.node, hasCls('fc-dh-row')).map(r => r.node.props['data-id']) }
    }),
    rows: findAll(s, hasCls('fc-dh-row')).map(readDhRow),
    skeletons: findAll(s, hasCls('fc-dh-skel')).length,
    srLoading: !!q(n => n.props.className === 'fc-trail-sr' && norm(n) === 'Loading decision history…'),
    alert: alert ? { text: norm(alert.node), retry: !!find(alert.node, byAttr('data-action', 'retry')) } : null,
    box: stateBox ? {
      kind: stateBox.node.props['data-state'],
      title: norm(find(stateBox.node, n => n.type === 'h3').node),
      text: norm(stateBox.node),
      chips: findAll(stateBox.node, hasCls('fc-chip')).map(h => norm(h.node)),
      cmd: (() => { const c = find(stateBox.node, n => n.props.className === 'fc-step-cmd'); return c ? norm(c.node) : null })(),
      clear: (() => { const b = find(stateBox.node, byAttr('data-action', 'clear')); return b ? { cls: b.node.props.className, text: norm(b.node) } : null })()
    } : null,
    more: (() => { const mo = q(hasCls('fc-dh-more')); return mo ? norm(mo.node) : null })(),
    live: live ? norm(live.node) : null,
    modal: (() => { const md = q(hasCls('fc-m')); return md ? md.node.props['data-modal'] : null })()
  }
}
const trailQueryBy = (...key) => { const k = JSON.stringify(key); const hits = trailQueries.filter(x => JSON.stringify(x.key) === k); const qq = hits[hits.length - 1]; return qq ? { key: qq.key, interval: qq.interval ?? null, fn: qq.fn } : null }
const lastTrailQuery = () => { const q = trailQueryBy('factonio', 'trail', 200) || trailQueries.filter(x => x.key[2] !== 'audit' && !x.key.includes('signal')).slice(-1).map(x => ({ key: x.key, interval: x.interval, fn: x.fn }))[0]; return q ? { key: q.key, interval: q.interval } : null }
const baseTrailQuery = () => { const qs = trailQueries.filter(x => x.key[2] !== 'audit' && !x.key.includes('signal')); const q = qs[qs.length - 1]; return q ? { key: q.key, interval: q.interval ?? null } : null }
const trailSelect = (tree, filter, value) => find(dhSection(tree).node, n => n.type === 'select' && n.props['data-filter'] === filter).node.props.onChange({ target: { value } })
const trailPhase = (tree, phase) => trailSelect(tree, 'phase', phase)
const trailAction = (tree, action) => fire(find(dhSection(tree).node, n => n.type === 'button' && n.props['data-action'] === action))
const trailPreset = (tree, v) => fire(find(dhSection(tree).node, n => n.props['data-action'] === 'preset' && n.props['data-value'] === v))
const trailSup = (tree, checked) => find(dhSection(tree).node, byAttr('data-filter', 'sup')).node.props.onChange({ target: { checked } })
const trailQ = (tree, value) => find(dhSection(tree).node, byAttr('data-filter', 'q')).node.props.onChange({ target: { value } })
const dhRowHit = (tree, id) => find(dhSection(tree).node, n => hasCls('fc-dh-row')(n) && n.props['data-id'] === id)
const rowAction = (tree, id, action) => fire(find(dhRowHit(tree, id).node, byAttr('data-action', action)))
const settle = () => new Promise(r => setTimeout(r, 300))
const AUDIT_CLEAN = { ok: true, clean: true, inspected: 8, active_inspected: 7, counts: {}, findings: [], filters: {} }
const AUDIT_FINDINGS = {
  ok: true, clean: false, inspected: 8, active_inspected: 7, counts: { gate_loosening: 1, missing_evidence: 1, weak_evidence: 1 }, filters: {},
  findings: [
    { kind: 'gate_loosening', decision_id: 'd_f012c8a47b99', severity: 'high', message: 'A required hook was removed without a human countersign.' },
    { kind: 'missing_evidence', decision_id: 'd_9a1e4c0b2f31', message: 'pr_gate_wait carries no head_sha proof.' },
    { kind: 'weak_evidence', decision_id: 'd_6f19b3d28e45', message: 'See holdout/scenario-3.md for the proof.' },
    { kind: 'unknown_kind', decision_id: 'd_4b81e2c0a119', message: 'dropped' }
  ]
}
const routeTrail = (trail, audit) => opts => (opts.queryKey[2] === 'audit'
  ? (audit instanceof Error ? { data: undefined, isLoading: false, isError: true, error: audit } : { data: audit, isLoading: false, isError: false })
  : { data: trail, isLoading: false, isError: false })

async function runTrailScenarios(report) {
  const T = report.trail = {}
  const client = { invalidated: [], invalidateQueries(o) { this.invalidated.push(o.queryKey); return Promise.resolve() } }
  const mountDash = () => mountRepos({ data: { repos: TRAIL_REPOS, defaults: REPO_DEFAULTS }, client })
  const dash = T.dashboard = {}

  // Loading (first load): skeletons, disabled controls; base query polls, signal + audit queries never do.
  trailState = { data: undefined, isLoading: true, isError: false }
  trailQueries.length = 0
  let m = mountDash()
  let tree = m.render()
  dash.loading = readTrail(tree)
  dash.query = lastTrailQuery()
  const sig = trailQueryBy('factonio', 'trail', 200, 'signal', 'key')
  const aud = trailQueryBy('factonio', 'trail', 'audit', 'all')
  dash.queries = { signal: sig && { key: sig.key, interval: sig.interval }, audit: aud && { key: aud.key, interval: aud.interval }, polling: [...new Set(trailQueries.filter(x => x.interval).map(x => x.key.join('/')))] }
  m.rests.length = 0
  await trailQueryBy('factonio', 'trail', 200).fn(); await sig.fn(); await aud.fn()
  dash.restPaths = m.rests.map(r => r.path)
  dash.restPath = dash.restPaths[0]
  dash.sectionOrder = findAll(tree, n => n.type === 'h2').map(h => norm(h.node))

  // Populated, audit clean.
  trailState = routeTrail(TRAIL_DATA, AUDIT_CLEAN)
  m = mountDash()
  tree = m.render()
  dash.populated = readTrail(tree)
  windowOpens.length = 0
  const prPill = find(dhSection(tree).node, n => hasCls('fc-chip')(n) && n.type === 'a')
  const prFire = fire(prPill)
  await tick()
  dash.prClick = { prevented: prFire.prevented, stopped: prFire.stopped, windowOpens: windowOpens.map(a => a[0]) }
  windowOpens.length = 0
  // Row expand -> Fields inspector; JSON tab; chain disclosure.
  rowAction(tree, 'd_f012c8a47b99', 'toggle-row'); tree = m.render()
  dash.expanded = readTrail(tree).rows.find(r => r.id === 'd_f012c8a47b99')
  fire(find(dhRowHit(tree, 'd_f012c8a47b99').node, n => n.props['data-tab'] === 'json')); tree = m.render()
  dash.expandedJson = readTrail(tree).rows.find(r => r.id === 'd_f012c8a47b99').insp
  rowAction(tree, 'd_c4f08e2a91b7', 'toggle-chain'); tree = m.render()
  dash.chainOpen = readTrail(tree).rows.find(r => r.id === 'd_c4f08e2a91b7').chain
  // Selecting text inside a row never toggles it.
  globalThis.getSelection = () => ({ toString: () => 'selected' })
  fire(find(dhRowHit(tree, 'd_7e30f91b5c04').node, hasCls('fc-dh-main'))); tree = m.render()
  dash.selectNoToggle = readTrail(tree).rows.find(r => r.id === 'd_7e30f91b5c04').expanded
  delete globalThis.getSelection

  // Presets, search and filters.
  m = mountDash(); tree = m.render()
  trailPreset(tree, 'attention'); tree = m.render()
  dash.attention = readTrail(tree)
  trailPreset(tree, 'all'); tree = m.render()
  dash.all = readTrail(tree)
  trailSup(tree, true); tree = m.render()
  dash.supShown = readTrail(tree)
  trailSup(tree, false); tree = m.render()
  trailQ(tree, 'graphql'); tree = m.render()
  dash.search = readTrail(tree)
  await settle()
  tree = m.render()
  const sq = trailQueryBy('factonio', 'trail', 200, 'signal', 'all', 'q', 'graphql')
  m.rests.length = 0
  if (sq) await sq.fn()
  dash.searchServer = { key: sq ? sq.key : null, interval: sq ? sq.interval : null, rest: m.rests.map(r => r.path) }
  trailQ(tree, 'holdout'); tree = m.render()
  dash.searchBarrier = readTrail(tree)
  trailQ(tree, ''); tree = m.render()
  trailSelect(tree, 'repo', 'acme-api'); tree = m.render()
  dash.repoFiltered = readTrail(tree)
  trailPhase(tree, 'merge'); tree = m.render()
  dash.noMatch = readTrail(tree)
  fire(find(find(tree, n => n.props.className === 'fc-trail-state').node, byAttr('data-action', 'clear'))); tree = m.render()
  dash.cleared = readTrail(tree)
  trailSelect(tree, 'tenant', W2); tree = m.render()
  dash.tenantFiltered = readTrail(tree)

  // Superseded-by chip on a shown superseded row jumps to the correction.
  m = mountDash(); tree = m.render()
  trailSup(tree, true); tree = m.render()
  scrollLog.length = 0; m.focusLog.length = 0
  await Promise.all(fire(find(dhRowHit(tree, 'd_9a1e4c0b2f31').node, byAttr('data-action', 'jump-correction'))).results)
  tree = m.render()
  dash.jumpCorrection = { trail: readTrail(tree), scroll: JSON.parse(JSON.stringify(scrollLog)), focus: [...m.focusLog] }

  // Attention banner with findings: collapsed by default, counts per kind, Show decision deep-link.
  trailState = routeTrail(TRAIL_DATA, AUDIT_FINDINGS)
  m = mountDash(); tree = m.render()
  dash.auditCollapsed = readTrail(tree)
  trailAction(tree, 'toggle-audit'); tree = m.render()
  dash.auditOpen = readTrail(tree)
  trailSelect(tree, 'repo', 'acme-api'); tree = m.render()
  scrollLog.length = 0; m.focusLog.length = 0
  await Promise.all(fire(find(dhSection(tree).node, n => n.props['data-action'] === 'show-decision' && n.props['data-id'] === 'd_9a1e4c0b2f31')).results)
  tree = m.render()
  dash.showDecision = { trail: readTrail(tree), scroll: JSON.parse(JSON.stringify(scrollLog)), focus: [...m.focusLog] }
  dash.auditBarrier = allStrings(dhSection(tree).node).filter(x => /holdout/i.test(x))
  // Older backend: /trail/audit 404 -> no banner, no clean pill; ledger unaffected.
  trailState = routeTrail(TRAIL_DATA, new Error('404: {"detail":"Not Found"}'))
  m = mountDash(); tree = m.render()
  dash.audit404 = readTrail(tree)

  // Routine folding: consecutive pr_gate / bootstrap_verify ticks fold under All; Key hides them.
  const tick_ = (i, o = {}) => dec({ id: `d_rt${String(i).padStart(2, '0')}`, ts: `2026-11-25T17:${String(58 - i * 6).padStart(2, '0')}:00.000000Z`, phase: 'gate', actor: 'orchestrator', decision: 'pr_gate', why: 'Gate tick.', result: 'pass', evidence: { pr_number: 12 }, ...o })
  const ROUTINE = [
    dec({ id: 'd_rk01', ts: '2026-11-25T17:59:00.000000Z', phase: 'merge', actor: 'orchestrator', decision: 'merge_pr', why: 'Merged.', result: 'merged', evidence: { pr_number: 12 } }),
    tick_(1), tick_(2, { result: 'blocked' }), tick_(3), tick_(4),
    dec({ id: 'd_rk02', ts: '2026-11-25T17:30:00.000000Z', phase: 'gate', actor: 'human', decision: 'override_gate', why: 'Accepted the flake.', result: 'approved' }),
    tick_(7, { decision: 'bootstrap_verify', phase: 'verify' }),
    tick_(8, { result: 'proof_failed' })
  ]
  trailState = routeTrail({ decisions: ROUTINE, summary: { total: ROUTINE.length, active: ROUTINE.length, superseded: 0, by_tenant: { [W2]: ROUTINE.length } } }, AUDIT_CLEAN)
  m = mountDash(); tree = m.render()
  dash.routineKey = readTrail(tree)
  trailPreset(tree, 'all'); tree = m.render()
  dash.routineAll = readTrail(tree)
  fire(find(dhSection(tree).node, byAttr('data-action', 'toggle-fold'))); tree = m.render()
  dash.routineUnfolded = readTrail(tree)

  // Log Human Decision modal: validation, explicit confirmation, POST body without actor.
  trailState = routeTrail(TRAIL_DATA, AUDIT_CLEAN)
  m = mountDash(); tree = m.render()
  m.focusLog.length = 0
  trailAction(tree, 'log-decision'); tree = m.render()
  const modalOf = t => find(t, hasCls('fc-m'))
  const readModal = t => {
    const md = modalOf(t)
    if (!md) return null
    const go = find(md.node, n => n.type === 'button' && hasCls('fc-btn--primary')(n))
    return {
      id: md.node.props['data-modal'], role: md.node.props.role, modal: md.node.props['aria-modal'], labelledBy: md.node.props['aria-labelledby'],
      title: norm(find(md.node, n => n.type === 'h2').node), busy: md.node.props['aria-busy'] ?? null,
      fields: Object.fromEntries(findAll(md.node, n => n.props['data-field'] && (n.type === 'div')).map(h => {
        const ctl = find(h.node, n => ['input', 'select', 'textarea'].includes(n.type)).node
        const err = find(h.node, hasCls('fc-field-err'))
        return [h.node.props['data-field'], { value: ctl.props.value, invalid: ctl.props['aria-invalid'] ?? null, err: err ? norm(err.node) : null, label: norm(find(h.node, n => n.type === 'label').node) }]
      })),
      confirm: (() => { const c = find(md.node, n => n.type === 'input' && n.props['data-field'] === 'confirm'); return c ? c.node.props.checked : null })(),
      quote: (() => { const qn = find(md.node, hasCls('fc-m-quote')); return qn ? { target: qn.node.props['data-target'], text: norm(qn.node) } : null })(),
      banner: (() => { const b = find(md.node, hasCls('fc-banner')); return b ? { tone: b.node.props['data-tone'], text: norm(b.node) } : null })(),
      go: go ? { text: norm(go.node), disabled: !!go.node.props.disabled, action: go.node.props['data-action'] } : null,
      text: norm(md.node)
    }
  }
  const setField = (t, name, value) => {
    const md = modalOf(t).node
    const ctl = find(md, n => n.props.name === name || (n.type === 'input' && n.props['data-field'] === name))
    if (name === 'confirm' || name === 'draft') return ctl.node.props.onChange({ target: { checked: value } })
    return ctl.node.props.onChange({ target: { value } })
  }
  const modalAction = (t, action) => fire(find(modalOf(t).node, n => n.type === 'button' && n.props['data-action'] === action))
  const lg = dash.logModal = {}
  lg.open = readModal(tree)
  lg.initialFocus = [...m.focusLog]
  // Focus guards: Shift+Tab past the first control lands on the last ENABLED one; Tab past the last on the
  // first; with every control disabled (busy) the dialog itself takes focus. Native focus() ignores disabled.
  const trapProbe = t => {
    const md = modalOf(t).node
    const out = {}
    for (const edge of ['start', 'end']) {
      m.focusLog.length = 0
      find(md, n => n.props['data-focus-guard'] === edge).node.props.onFocus()
      out[edge] = [...m.focusLog]
    }
    for (const shiftKey of [false, true]) {
      m.focusLog.length = 0
      let prevented = false
      md.props.onKeyDown({ key: 'Tab', shiftKey, target: md.props.ref.current, preventDefault() { prevented = true }, stopPropagation() {} })
      out[shiftKey ? 'shiftTabOnDialog' : 'tabOnDialog'] = { focus: [...m.focusLog], prevented }
    }
    return out
  }
  lg.trapUnchecked = trapProbe(tree)
  modalAction(tree, 'append-decision'); tree = m.render()
  lg.unconfirmed = readModal(tree)
  setField(tree, 'confirm', true); tree = m.render()
  lg.trapChecked = trapProbe(tree)
  setField(tree, 'evidence', '[1, 2]'); tree = m.render()
  await Promise.all(modalAction(tree, 'append-decision').results); tree = m.render()
  lg.invalid = readModal(tree)
  lg.restsInvalid = m.rests.filter(r => r.path === '/trail/decisions').length
  setField(tree, 'decision', 'override_gate'); setField(tree, 'why', 'Flaky e2e accepted by owner.'); setField(tree, 'evidence', '{"pr_number": 12}'); setField(tree, 'card_id', 't_bcf5502a'); tree = m.render()
  let release
  m.setRest(async (p, opts) => {
    if (p === '/trail/decisions') {
      await new Promise(r => { release = r })
      return { ok: true, decision: dec({ id: 'd_human00001', ts: '2026-11-25T18:00:00.000000Z', phase: opts.body.phase, actor: 'human', decision: opts.body.decision, why: opts.body.why, result: 'approved', card_id: opts.body.card_id, evidence: opts.body.evidence }) }
    }
    return { ok: true }
  })
  m.focusLog.length = 0
  const pend = Promise.all(modalAction(tree, 'append-decision').results); tree = m.render()
  lg.busy = readModal(tree)
  lg.busyFocus = [...m.focusLog]
  lg.trapBusy = trapProbe(tree)
  const esc = { key: 'Escape', preventDefault() {}, stopPropagation() {} }
  modalOf(tree).node.props.onKeyDown(esc); tree = m.render()
  lg.escWhileBusy = !!modalOf(tree)
  release(); await pend; tree = m.render()
  lg.body = m.rests.filter(r => r.path === '/trail/decisions').map(r => r.opts)
  lg.after = readTrail(tree)
  lg.notified = notifications.slice(-1)
  lg.invalidated = client.invalidated.map(k => k.join('/'))
  // Error keeps entries; Escape closes and restores focus.
  m = mountDash(); tree = m.render()
  trailAction(tree, 'log-decision'); tree = m.render()
  setField(tree, 'decision', 'note'); setField(tree, 'why', 'Context.'); setField(tree, 'confirm', true); tree = m.render()
  m.setRest(async p => { if (p === '/trail/decisions') throw new Error('422: {"detail":"phase is not allowed"}'); return {} })
  await Promise.all(modalAction(tree, 'append-decision').results); tree = m.render()
  lg.error = readModal(tree)
  modalOf(tree).node.props.onKeyDown(esc); tree = m.render()
  lg.closed = readTrail(tree).modal
  // Supersede… from the inspector prefills supersedes_id and quotes the target.
  m = mountDash(); tree = m.render()
  rowAction(tree, 'd_2d67a4e8f012', 'toggle-row'); tree = m.render()
  rowAction(tree, 'd_2d67a4e8f012', 'supersede'); tree = m.render()
  lg.supersede = readModal(tree)
  setField(tree, 'why', 'Worker was paused on purpose.'); setField(tree, 'confirm', true); tree = m.render()
  m.setRest(async (p, opts) => (p === '/trail/decisions' ? { ok: true, decision: dec({ id: 'd_human00002', ts: '2026-11-25T18:01:00.000000Z', phase: opts.body.phase, actor: 'human', decision: opts.body.decision, why: opts.body.why, result: 'overridden', supersedes_id: opts.body.supersedes_id }) } : {}))
  await Promise.all(modalAction(tree, 'append-decision').results); tree = m.render()
  lg.supersedeBody = m.rests.filter(r => r.path === '/trail/decisions').map(r => r.opts.body)
  lg.supersedeAfter = readTrail(tree)

  // Empty.
  trailState = routeTrail({ decisions: [], summary: { total: 0, active: 0, superseded: 0, by_tenant: {} }, filters: {} }, AUDIT_CLEAN)
  m = mountDash(); tree = m.render()
  dash.empty = readTrail(tree)

  // API error without data, then with stale rows; Retry refetches.
  const refetches = []
  trailState = { data: undefined, isLoading: false, isError: true, error: new Error('503: {"detail":"trail.db is locked"}'), refetch: () => refetches.push('dash') }
  m = mountDash(); tree = m.render()
  dash.error = readTrail(tree)
  trailAction(tree, 'retry')
  dash.errorRetry = [...refetches]
  trailState = opts => (opts.queryKey[2] === 'audit' ? { data: AUDIT_CLEAN, isLoading: false, isError: false } : { data: TRAIL_DATA, isLoading: false, isError: true, error: new Error('fetch failed') })
  m = mountDash(); tree = m.render()
  dash.errorStale = readTrail(tree)

  // Publish rows keep the branch / sha pills (max 3 + "+N in inspector").
  const PUB = [
    dec({ id: 'dec_9a8c4f10b2', ts: '2026-11-25T17:10:00.000000Z', phase: 'publish', card_id: 't_pub1', actor: 'human', decision: 'publish_pr', why: 'Published the verified branch.', evidence: { pr_number: 42, head_sha: '8f2c1a9e77', branch: 'feat/publish-policy', base_branch: 'main', draft: false }, result: 'published', supersedes_id: 'dec_7f2e1d84c1' }),
    dec({ id: 'dec_7f2e1d84c1', ts: '2026-11-25T17:00:00.000000Z', phase: 'publish', card_id: 't_pub1', actor: 'orchestrator', decision: 'publish_pr', why: 'Refused: publish_policy is ask.', evidence: { head_sha: '3b9e014c2d', branch: 'feat/publish-policy', base_branch: 'main' }, result: 'refused', superseded_by: ['dec_9a8c4f10b2'], is_superseded: true }),
    dec({ id: 'dec_draftlong01', ts: '2026-11-25T16:55:00.000000Z', phase: 'publish', card_id: 't_pub2', actor: 'orchestrator', decision: 'publish_pr', why: 'Draft PR.', evidence: { branch: 'b'.repeat(60), base_branch: 'develop', draft: true }, result: 'published' }),
    TRAIL[0]
  ]
  trailState = routeTrail({ decisions: PUB, summary: { total: PUB.length, active: PUB.length - 1, superseded: 1, by_tenant: { [W2]: PUB.length } }, filters: {} }, AUDIT_CLEAN)
  m = mountDash(); tree = m.render()
  dash.publish = readTrail(tree)
  trailPhase(tree, 'publish'); tree = m.render()
  dash.publishOnly = readTrail(tree)

  // Deep link from the sidebar: /factonio?tenant=… scopes the fetch and the filter.
  trailQueries.length = 0
  trailState = opts => (opts.queryKey[2] === 'audit' ? { data: AUDIT_CLEAN, isLoading: false, isError: false } : opts.queryKey.includes('tenant') ? { data: { ...TRAIL_DATA, decisions: TRAIL.filter(d => d.tenant === ACME_T), summary: { total: 1, active: 1, superseded: 0, by_tenant: { [ACME_T]: 1 } }, filters: { tenant: ACME_T } }, isLoading: false, isError: false } : { data: TRAIL_DATA, isLoading: false, isError: false })
  globalThis.location = { hash: `#/factonio?tenant=${encodeURIComponent(ACME_T)}`, search: '' }
  scrollLog.length = 0
  m = mountDash(); tree = m.render()
  const scopedQuery = baseTrailQuery()
  m.rests.length = 0
  await trailQueries.filter(x => x.key[2] !== 'audit' && !x.key.includes('signal')).slice(-1)[0].fn()
  await trailQueries.filter(x => x.key[2] === 'audit').slice(-1)[0].fn()
  dash.deepLink = { query: scopedQuery, rests: m.rests.map(r => r.path), trail: readTrail(tree), scroll: JSON.parse(JSON.stringify(scrollLog)) }
  delete globalThis.location
  trailSelect(tree, 'tenant', ''); tree = m.render()
  dash.deepLinkCleared = { query: baseTrailQuery(), rows: readTrail(tree).rows.length }

  // ---- sidebar -------------------------------------------------------------
  const sb = T.sidebar = {}
  const SB_TRAIL = [
    dec({ id: 'd_m7', ts: '2026-11-25T16:40:00.000000Z', tenant: META, repo_slug: 'tozes-factonio', phase: 'verify', card_id: 't_a1b2c3e0', actor: 'watchdog', decision: 'stall_detected', why: 'No side effect for 27 minutes.', evidence: { worker_pid: 4821, side_effect_age_s: 1620, card_id: 't_a1b2c3e0' }, result: 'stalled' }),
    dec({ id: 'd_m6', ts: '2026-11-25T16:30:00.000000Z', tenant: META, repo_slug: 'tozes-factonio', phase: 'gate', card_id: 't_a1b2c3d4', actor: 'orchestrator', decision: 'pr_gate', why: 'All merge gate tiers clear.', evidence: { pr_number: 42, head_sha: 'abcdef1234', patch_id: '91e4f0a2c7', tests_passed: 88, card_id: 't_a1b2c3d4' }, result: 'ready', supersedes_id: 'd_m3' }),
    dec({ id: 'd_m5', ts: '2026-11-25T16:20:00.000000Z', tenant: META, repo_slug: 'tozes-factonio', phase: 'implement', card_id: 't_a1b2c3d4', actor: 'worker', decision: 'fix_round', why: 'Rework after diff review.', evidence: {}, result: 'recorded' }),
    dec({ id: 'd_m4', ts: '2026-11-25T16:10:00.000000Z', tenant: META, repo_slug: 'tozes-factonio', phase: 'design', card_id: 't_x', actor: 'human', decision: 'design_approved', why: '', evidence: {}, result: 'approved' }),
    dec({ id: 'd_m3', ts: '2026-11-25T16:00:00.000000Z', tenant: META, repo_slug: 'tozes-factonio', phase: 'gate', card_id: 't_a1b2c3d4', actor: 'orchestrator', decision: 'pr_gate', why: 'e2e flaked.', evidence: { pr_number: 42 }, result: 'blocked', superseded_by: ['d_m6'], is_superseded: true }),
    dec({ id: 'd_m2', ts: '2026-11-25T15:50:00.000000Z', tenant: META, repo_slug: 'tozes-factonio', phase: 'define', card_id: 't_7d21c0e5', actor: 'orchestrator', decision: 'start_workstream', why: 'Started.', evidence: {}, result: 'spawned' }),
    dec({ id: 'd_m1', ts: '2026-11-25T15:40:00.000000Z', tenant: META, repo_slug: 'tozes-factonio', phase: 'define', card_id: 't_7d21c0e5', actor: 'orchestrator', decision: 'escape_noted', why: '', evidence: {}, result: 'recorded' }),
    dec({ id: 'd_s1', ts: '2026-11-25T14:00:00.000000Z', tenant: SIDEBAR, repo_slug: 'tozes-factonio', phase: 'verify', card_id: 't_4ab36a11', actor: 'orchestrator', decision: 'spawn_verify', why: 'PR #44 opened.', evidence: { pr_number: 44 }, result: 'spawned' })
  ]
  const SB_DATA = { decisions: SB_TRAIL, summary: { total: 8 }, filters: {} }
  const secNode = (tree, tenant) => find(tree, n => n.type === 'section' && n.props['data-tenant'] === tenant)
  const stripOf = (tree, tenant) => {
    const sec = secNode(tree, tenant)
    if (!sec) return null
    const st = find(sec.node, n => n.props.className === 'fc-sb-trail')
    if (!st) return null
    const btn = find(st.node, n => n.props.className === 'fc-sb-trail-toggle').node
    const list = find(st.node, n => n.props.className === 'fc-sb-trail-list')
    const cards = find(sec.node, n => n.props.className === 'fc-sb-ws-cards').node.props.children.filter(Boolean)
    const last = cards[cards.length - 1]
    return {
      isLastChild: !!last && (last === st.node || (last.props && last.props.className === 'fc-sb-trail')),
      action: btn.props['data-action'],
      text: norm(btn),
      expanded: btn.props['aria-expanded'] ?? null,
      controls: btn.props['aria-controls'] ?? null,
      disabled: !!btn.props.disabled,
      busy: btn.props['aria-busy'] ?? null,
      phases: (() => { const p = find(btn, n => n.props.className === 'fc-sb-trail-phases'); return p ? norm(p.node) : null })(),
      flag: (() => { const f = find(btn, n => n.props.className === 'fc-sb-trail-flag'); return f ? norm(f.node) : null })(),
      list: list ? {
        id: list.node.props.id,
        label: list.node.props['aria-label'],
        items: findAll(list.node, n => n.props.className === 'fc-sb-trail-item').map(h => readSbItem(h.node)),
        more: (() => { const mo = find(list.node, n => n.props.className === 'fc-sb-trail-more'); return mo ? norm(mo.node) : null })()
      } : null
    }
  }
  const readSbItem = it => ({
    id: it.props['data-id'],
    superseded: it.props['data-superseded'],
    tags: findAll(find(it, n => n.props.className === 'fc-sb-trail-top').node, n => n.props.className === 'fc-sb-tag').map(h => ({ text: norm(h.node), tone: h.node.props['data-tone'] ?? null })),
    supTag: !!find(it, n => n.props.className === 'fc-sb-trail-sup'),
    time: find(it, n => n.type === 'time').node.props.dateTime,
    title: norm(find(it, n => n.props.className === 'fc-sb-trail-title').node),
    why: (() => { const w = find(it, n => n.props.className === 'fc-sb-trail-why'); return w ? norm(w.node) : null })(),
    chips: findAll(it, n => n.props.className === 'fc-sb-chip').map(h => ({ ev: h.node.props['data-ev'], text: norm(h.node), tone: h.node.props['data-tone'] ?? null, title: h.node.props.title }))
  })
  const drawerDecisions = (tree, id) => {
    const art = find(tree, n => n.type === 'article' && n.props['data-id'] === id)
    const dl = art && find(art.node, n => n.type === 'dl')
    if (!dl) return null
    const dt = find(dl.node, n => n.type === 'dt' && n.props.className === 'fc-sb-full fc-sb-trail-dt')
    const dd = find(dl.node, n => n.type === 'dd' && n.props['data-field'] === 'decisions')
    const dds = findAll(dl.node, n => n.type === 'dd').map(h => h.node.props['data-field'])
    return { dt: dt ? norm(dt.node) : null, ddClass: dd?.node.props.className, text: dd ? norm(dd.node) : null, items: dd ? findAll(dd.node, n => n.props.className === 'fc-sb-trail-item').map(h => readSbItem(h.node)) : [], fieldOrder: dds }
  }
  const openCard = (m, tree, id) => { fire(find(find(tree, n => n.type === 'article' && n.props['data-id'] === id).node, byAttr('data-action', 'toggle-card'))); return m.render() }

  cardsState = { data: { cards: CARDS }, isLoading: false, isError: false }
  // Loading.
  trailState = { data: undefined, isLoading: true, isError: false }
  trailQueries.length = 0
  paneVisible = true
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'all' }), focused: null })
  tree = m.render()
  sb.query = lastTrailQuery()
  await trailQueries[trailQueries.length - 1].fn()
  sb.restPath = m.rests[m.rests.length - 1]
  tree = openCard(m, tree, 't_a1b2c3d4')
  sb.loading = { strip: stripOf(tree, META), drawer: drawerDecisions(tree, 't_a1b2c3d4') }
  paneVisible = false
  m.render(); sb.hiddenQuery = lastTrailQuery()
  paneVisible = true

  // Populated.
  trailState = { data: SB_DATA, isLoading: false, isError: false }
  navigations.length = 0
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'all' }), focused: null })
  tree = m.render()
  sb.collapsed = { meta: stripOf(tree, META), sidebar: stripOf(tree, SIDEBAR), pillWsCollapsed: secNode(tree, PILL) ? stripOf(tree, PILL) : 'absent', stages: sectionsOf(tree).find(x => x.tenant === META).stages }
  fire(find(secNode(tree, META).node, byAttr('data-action', 'toggle-trail'))); tree = m.render()
  sb.expanded = stripOf(tree, META)
  fire(find(secNode(tree, META).node, byAttr('data-action', 'open-trail')))
  sb.openTrailNav = [...navigations]
  fire(find(secNode(tree, SIDEBAR).node, byAttr('data-action', 'toggle-trail'))); tree = m.render()
  sb.sidebarExpanded = stripOf(tree, SIDEBAR)
  fire(find(secNode(tree, META).node, byAttr('data-action', 'toggle-trail'))); tree = m.render()
  sb.recollapsed = stripOf(tree, META).expanded
  tree = openCard(m, tree, 't_a1b2c3d4')
  sb.drawer = drawerDecisions(tree, 't_a1b2c3d4')
  tree = openCard(m, tree, 't_a1b2c3e1')
  sb.drawerNone = drawerDecisions(tree, 't_a1b2c3e1')
  // Card filters narrow cards, never the workstream ledger; a mismatch hides every strip.
  fire(find(find(tree, n => n.props['aria-label'] === 'Filter by phase').node, byText('button', 'Verify'))); tree = m.render()
  sb.phaseFiltered = stripOf(tree, META)
  fire(find(find(tree, n => n.props['aria-label'] === 'Filter by phase').node, byText('button', 'Design'))); tree = m.render()
  fire(find(tree, n => n.props.className === 'fc-sb-stat' && n.props['data-value'] === 'attention')); tree = m.render()
  sb.noMatch = { empty: emptyOf(tree), strips: findAll(tree, n => n.props.className === 'fc-sb-trail').length }

  // Empty trail: no strips; drawers say "None recorded".
  trailState = { data: { decisions: [], summary: { total: 0 }, filters: {} }, isLoading: false, isError: false }
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'all' }), focused: null })
  tree = m.render()
  sb.emptyStrips = findAll(tree, n => n.props.className === 'fc-sb-trail').length
  tree = openCard(m, tree, 't_a1b2c3d4')
  sb.emptyDrawer = drawerDecisions(tree, 't_a1b2c3d4')

  // API error: strip offers Retry (refetch), drawer reads "Unavailable".
  const sbRefetch = []
  trailState = { data: undefined, isLoading: false, isError: true, error: new Error('500: boom'), refetch: () => sbRefetch.push(1) }
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'all' }), focused: null })
  tree = m.render()
  sb.error = { strip: stripOf(tree, META) }
  const retryHit = fire(find(secNode(tree, META).node, byAttr('data-action', 'retry-trail')))
  sb.error.retry = { refetches: sbRefetch.length, stopped: retryHit.stopped }
  tree = openCard(m, tree, 't_a1b2c3d4')
  sb.error.drawer = drawerDecisions(tree, 't_a1b2c3d4')

  trailState = { data: undefined, isLoading: true, isError: false }
  cardsState = { data: undefined, isLoading: true, isError: false }
  globalThis.__fcReposState = undefined
  globalThis.__fcQueryClient = undefined
}

// Supersession chains whose prior decision is outside the loaded window / server scope, fed by
// real trail.db payloads that the Python side writes to FC_LINEAGE_FIXTURE.
async function runTrailLineageScenarios(report) {
  const L = report.trail.lineage = {}
  const fx = JSON.parse(readFileSync(process.env.FC_LINEAGE_FIXTURE, 'utf8'))
  const client = { invalidateQueries: () => Promise.resolve() }
  const notFound = id => new Error(`404: {"detail":"Decision '${id}' not found."}`)
  const mockRest = (responses, extra = {}) => async p => {
    if (Object.hasOwn(extra, p)) return extra[p](p)
    if (Object.hasOwn(responses, p)) return JSON.parse(JSON.stringify(responses[p]))
    if (p.startsWith('/trail/')) throw notFound(p.slice(7))
    throw new Error(`500: unexpected ${p}`)
  }
  const mount = (state, rest) => {
    trailState = state
    trailQueries.length = 0
    const m = mountRepos({ data: { repos: TRAIL_REPOS, defaults: REPO_DEFAULTS }, client })
    m.setRest(rest)
    return m
  }
  const chainOf = (tree, id) => readTrail(tree).rows.find(r => r.id === id)?.chain ?? null
  const toggleChain = (tree, id) => Promise.all(rowAction(tree, id, 'toggle-chain').results.filter(Boolean))
  const snap = (m, tree, id) => ({ chain: chainOf(tree, id), rows: readTrail(tree).rows.length, rests: m.rests.map(r => r.path), query: baseTrailQuery() })

  // >200 rows: the original is not in /trail?limit=200; opening the chain loads it by id.
  {
    const w = fx.window
    const m = mount({ data: w.responses['/trail?limit=200'], isLoading: false, isError: false }, mockRest(w.responses))
    let tree = m.render()
    const before = chainOf(tree, w.correction)
    m.rests.length = 0
    const pending = toggleChain(tree, w.correction)
    tree = m.render()
    const loading = chainOf(tree, w.correction)
    await pending; await tick()
    tree = m.render()
    L.window = { api: w.api, original: w.original, before, loading, after: snap(m, tree, w.correction) }
    // Closing and reopening needs no refetch: the prior is pinned.
    rowAction(tree, w.correction, 'toggle-chain'); tree = m.render()
    m.rests.length = 0
    await toggleChain(tree, w.correction); tree = m.render()
    L.windowAgain = snap(m, tree, w.correction)
  }

  // Older host / capture mock: /trail/{id} answers with a list payload -> one wide unscoped page.
  {
    const w = fx.window
    const m = mount({ data: w.responses['/trail?limit=200'], isLoading: false, isError: false },
      mockRest(w.responses, { [`/trail/${w.original}`]: () => w.responses['/trail?limit=200'] }))
    let tree = m.render()
    m.rests.length = 0
    await toggleChain(tree, w.correction); await tick()
    tree = m.render()
    L.fallback = snap(m, tree, w.correction)
  }

  // Server scope: /factonio?tenant=<b>; the superseded original lives in another tenant.
  {
    const s = fx.scope
    const state = opts => (opts.queryKey.includes('tenant')
      ? { data: s.responses[`/trail?tenant=${encodeURIComponent(s.tenant)}&limit=200`], isLoading: false, isError: false }
      : { data: s.responses['/trail?limit=200'], isLoading: false, isError: false })
    globalThis.location = { hash: `#/factonio?tenant=${encodeURIComponent(s.tenant)}`, search: '' }
    const m = mount(state, mockRest(s.responses))
    let tree = m.render()
    const before = { chain: chainOf(tree, s.correction), rows: readTrail(tree).rows.map(r => r.id), tenant: readTrail(tree).bar.selects.tenant.value, query: baseTrailQuery() }
    delete globalThis.location
    m.rests.length = 0
    await toggleChain(tree, s.correction); await tick()
    tree = m.render()
    L.scope = { api: s.api, original: s.original, before, after: { ...snap(m, tree, s.correction), tenant: readTrail(tree).bar.selects.tenant.value } }
  }

  // Genuinely missing prior (404): stays listed as outside the window; nothing thrown, no wide refetch.
  {
    const rows = [dec({ id: 'd_dangling0001', ts: '2026-11-25T16:40:00.000000Z', phase: 'verify', actor: 'orchestrator', decision: 'verify_carryover', result: 'pass', supersedes_id: 'd_missing00000' })]
    const m = mount({ data: { decisions: rows, summary: { total: 1, active: 1, superseded: 0, by_tenant: { [W2]: 1 } }, filters: {} }, isLoading: false, isError: false }, mockRest({}))
    let tree = m.render()
    m.rests.length = 0
    let threw = null
    try { await toggleChain(tree, 'd_dangling0001') } catch (e) { threw = String(e) }
    await tick()
    tree = m.render()
    L.missing = { threw, ...snap(m, tree, 'd_dangling0001') }
  }

  // Race: Show decision A is still loading when B is followed; A's late answer is dropped.
  {
    const rows = [dec({ id: 'd_live_000001', ts: '2026-11-25T16:40:00.000000Z', phase: 'verify', actor: 'orchestrator', decision: 'retry_a', result: 'pass' })]
    const orig = (id, ts) => dec({ id, ts, phase: 'verify', actor: 'orchestrator', decision: 'first_try', result: 'reject' })
    const audit = { ok: true, clean: false, inspected: 3, findings: [
      { kind: 'missing_evidence', decision_id: 'd_orig_a00001', message: 'A has no proof.' },
      { kind: 'weak_evidence', decision_id: 'd_orig_b00001', message: 'B is weak.' }
    ] }
    const slowA = deferred()
    const state = opts => (opts.queryKey[2] === 'audit' ? { data: audit, isLoading: false, isError: false } : { data: { decisions: rows, summary: { total: 3, active: 3, superseded: 0, by_tenant: { [W2]: 3 } }, filters: {} }, isLoading: false, isError: false })
    const m = mount(state, mockRest({ '/trail/d_orig_b00001': { decision: orig('d_orig_b00001', '2026-11-20T10:00:00.000000Z') } }, { '/trail/d_orig_a00001': () => slowA.promise }))
    let tree = m.render()
    trailAction(tree, 'toggle-audit'); tree = m.render()
    m.rests.length = 0; scrollLog.length = 0; m.focusLog.length = 0
    const show = id => Promise.all(fire(find(dhSection(tree).node, n => n.props['data-action'] === 'show-decision' && n.props['data-id'] === id)).results)
    const a = show('d_orig_a00001')
    tree = m.render()
    const loadingMsg = readTrail(tree).live
    await show('d_orig_b00001'); await tick()
    tree = m.render()
    slowA.resolve({ decision: orig('d_orig_a00001', '2026-11-21T10:00:00.000000Z') })
    await a; await tick()
    tree = m.render()
    const t = readTrail(tree)
    L.race = { loadingMsg, rests: m.rests.map(r => r.path), rows: t.rows.map(r => [r.id, r.flash, r.expanded]), live: t.live, scroll: JSON.parse(JSON.stringify(scrollLog)), focus: [...m.focusLog] }
  }

  // Show decision beyond the first rendered page: (a) loaded row 90 of 100; (b) an old row fetched by id
  // outside the 200-row window. The page grows to include it, then it is scrolled to and focused.
  {
    const mk = (n, extra = {}) => dec({ id: `d_probe_${String(n).padStart(3, '0')}`, ts: new Date(Date.UTC(2026, 10, 25, 12) - n * 60000).toISOString(), phase: 'verify', actor: 'orchestrator', decision: 'design_approved', result: 'approved', ...extra })
    const deep = async (count, total, target, rest) => {
      const rows = Array.from({ length: count }, (_, i) => mk(i))
      const audit = { ok: true, clean: false, inspected: total, findings: [{ kind: 'missing_evidence', decision_id: target, message: 'No proof.' }] }
      const m = mount(opts => (opts.queryKey[2] === 'audit' ? { data: audit, isLoading: false, isError: false } : { data: { decisions: rows, summary: { total, active: total, superseded: 0, by_tenant: { [W2]: total } }, filters: {} }, isLoading: false, isError: false }), mockRest(rest))
      let tree = m.render()
      const before = readTrail(tree).rows.length
      trailAction(tree, 'toggle-audit'); tree = m.render()
      m.rests.length = 0; scrollLog.length = 0; m.focusLog.length = 0
      await Promise.all(fire(find(dhSection(tree).node, n => n.props['data-action'] === 'show-decision' && n.props['data-id'] === target)).results); await tick()
      for (let i = 0; i < 3; i++) tree = m.render()
      const t = readTrail(tree)
      const row = t.rows.find(r => r.id === target)
      return { before, rendered: t.rows.length, targetIndex: t.rows.findIndex(r => r.id === target), row: row ? [row.id, row.flash, row.expanded] : null, rests: m.rests.map(r => r.path), scroll: JSON.parse(JSON.stringify(scrollLog)), focus: [...m.focusLog], live: t.live }
    }
    L.deepLoaded = await deep(100, 100, 'd_probe_090', {})
    L.deepFetched = await deep(200, 500, 'd_probe_450', { '/trail/d_probe_450': { decision: mk(450) } })
  }

  // The 30s base poll also refreshes the audit and the active server-filtered page; no new timers, no loop.
  {
    const rows = TRAIL.slice(0, 4)
    let stamp = 1000
    let auditData = AUDIT_CLEAN
    let sigRows = [rows[0]]
    const refetched = []
    const intervals = []
    const realSetInterval = globalThis.setInterval
    globalThis.setInterval = (...a) => { intervals.push(a[1]); return realSetInterval(...a) }
    const newRow = dec({ id: 'd_sig_new0001', ts: '2026-11-25T17:00:00.000000Z', phase: 'verify', actor: 'orchestrator', decision: 'pr_merged', why: 'GraphQL readback after the poll.', result: 'merged' })
    const state = opts => {
      if (opts.queryKey[2] === 'audit') return { data: auditData, isLoading: false, isError: false, refetch: () => { refetched.push('audit'); auditData = AUDIT_FINDINGS } }
      if (opts.queryKey.includes('signal')) return { data: { decisions: sigRows, summary: {}, filters: {} }, isLoading: false, isError: false, refetch: () => { refetched.push(opts.queryKey.slice(3).join('/')); sigRows = [newRow, rows[0]] } }
      return { data: { decisions: rows, summary: { total: 4, active: 4, superseded: 0, by_tenant: { [W2]: 4 } }, filters: {} }, isLoading: false, isError: false, dataUpdatedAt: stamp }
    }
    const m = mount(state, mockRest({}))
    let tree = m.render()
    trailQ(tree, 'graphql'); tree = m.render()
    await settle(); tree = m.render(); tree = m.render()
    const P = L.poll = { before: { auditClean: readTrail(tree).auditClean, audit: readTrail(tree).audit, rows: readTrail(tree).rows.map(r => r.id) }, refetchedBefore: [...refetched] }
    stamp = 31000
    tree = m.render()
    tree = m.render()
    const t = readTrail(tree)
    P.after = { auditClean: t.auditClean, audit: t.audit ? t.audit.head : null, rows: t.rows.map(r => r.id) }
    for (let i = 0; i < 4; i++) tree = m.render()
    P.refetched = [...refetched]
    P.intervals = [...intervals]
    P.queryIntervals = [...new Set(trailQueries.filter(x => x.interval).map(x => `${x.key.join('/')}=${x.interval}`))]
    globalThis.setInterval = realSetInterval
  }

  trailState = { data: undefined, isLoading: true, isError: false }
  globalThis.__fcReposState = undefined
  globalThis.__fcQueryClient = undefined
}
// ---------------------------------------------------------------------------
// v0.2.0 polish: default_rigor, verification panels, /metrics/history trends
// ---------------------------------------------------------------------------
const historyQueries = []
const RIGOR_DEFAULTS = { ...REPO_DEFAULTS, default_rigor: 'standard' }
const RIGOR_REPOS = [
  { ...REPOS[0], default_rigor: null, effective_default_rigor: 'standard' },
  { ...REPOS[1], default_rigor: 'high', effective_default_rigor: 'high' },
  { ...REPOS[2], default_rigor: 'standard', effective_default_rigor: 'standard' }
]
const readRigorCells = tree => Object.fromEntries(findAll(reposSec(tree).node, n => n.type === 'td' && n.props['data-field'] === 'default_rigor').map(h => {
  const tr = findAll(reposSec(tree).node, n => n.type === 'tr' && n.props['data-slug'] && n.props.className !== 'fc-repo-ed').find(r => find(r.node, x => x === h.node))
  const t = find(h.node, n => String(n.props.className || '').startsWith('fc-tag fc-tag--')).node
  return [tr.node.props['data-slug'], { cls: t.props.className, text: norm(t), title: t.props.title, rigor: t.props['data-rigor'] ?? null, glyph: !!find(t, n => n.type === 'svg') }]
}))
const cls = c => n => n.props && n.props.className === c
const clsStarts = c => n => n.props && String(n.props.className || '').startsWith(c)

function readKpi(tree) {
  const k = find(tree, clsStarts('fc-card fc-kpi-vp'))
  if (!k) return null
  const bar = find(k.node, cls('fc-kpi-bar'))
  const flag = find(k.node, cls('fc-kpi-flag'))
  return {
    flag: k.node.props['data-flag'] ?? null,
    state: k.node.props['data-state'] ?? null,
    text: norm(k.node),
    flagText: flag ? norm(flag.node) : null,
    bar: bar ? { role: bar.node.props.role, label: bar.node.props['aria-label'], segs: bar.node.props.children.map(s => [s.props['data-tone'], s.props.style.flex]) } : null,
    grid: find(tree, n => n.props && String(n.props.className || '').startsWith('fc-grid'))?.node.props.className
  }
}
function readVp(tree) {
  const v = find(tree, clsStarts('fc-card fc-vp'))
  if (!v) return null
  const tag = find(v.node, clsStarts('fc-tag fc-rigor'))
  const ready = find(v.node, cls('fc-ready'))
  const src = find(v.node, cls('fc-vp-src'))
  const warn = find(v.node, cls('fc-vp-warn'))
  const empty = find(v.node, cls('fc-vp-empty'))
  return {
    state: v.node.props['data-state'],
    busy: v.node.props['aria-busy'] ?? null,
    label: v.node.props['aria-label'] ?? null,
    skel: !!find(v.node, cls('fc-vp-skel')),
    rigor: tag ? { rigor: tag.node.props['data-rigor'], text: norm(tag.node) } : null,
    ready: ready ? { tone: ready.node.props['data-tone'], text: norm(ready.node) } : null,
    src: src ? norm(src.node) : null,
    members: findAll(v.node, cls('fc-vp-member')).map(h => ({ idx: norm(find(h.node, cls('fc-vp-idx')).node), label: norm(find(h.node, cls('fc-mono')).node), vendor: norm(find(h.node, cls('fc-vendor')).node), dup: find(h.node, cls('fc-vendor')).node.props['data-dup'] })),
    warn: warn ? { role: warn.node.props.role, text: norm(warn.node), advisories: findAll(warn.node, n => n.type === 'li').map(h => norm(h.node)) } : null,
    empty: empty ? norm(empty.node) : null
  }
}
function readSpark(h) {
  const n = h.node
  const plot = find(n, cls('fc-spark-plot')).node
  const svg = find(n, n2 => n2.type === 'svg' && n2.props.className === 'fc-spark-svg')?.node
  const delta = find(n, cls('fc-spark-delta')).node
  const read = find(n, cls('fc-spark-read')).node
  const hover = svg ? find(svg, cls('fc-spark-hover')) : null
  return {
    metric: n.props['data-metric'],
    title: norm(find(n, cls('fc-k')).node),
    value: norm(find(n, cls('fc-spark-v')).node),
    delta: { tone: delta.props['data-tone'], text: norm(delta), arrow: !!find(delta, x => x.type === 'svg') },
    plot: { tabIndex: plot.props.tabIndex, role: plot.props.role, label: plot.props['aria-label'] },
    svg: svg ? {
      viewBox: svg.props.viewBox,
      par: svg.props.preserveAspectRatio,
      hidden: svg.props['aria-hidden'],
      paths: findAll(svg, x => x.type === 'path').map(p => [p.node.props.className, p.node.props.d]),
      circles: findAll(svg, x => x.type === 'circle').map(c => [c.node.props.className, c.node.props.cx, c.node.props.cy, c.node.props.r]),
      lines: findAll(svg, x => x.type === 'line').map(l => [l.node.props.className, l.node.props.y1, l.node.props.strokeDasharray ?? null]),
      hover: hover ? hover.node.props.visibility : null
    } : null,
    legend: (() => { const l = find(n, cls('fc-spark-legend')); return l ? norm(l.node) : null })(),
    read: { live: read.props['aria-live'], text: norm(read) }
  }
}
function readTrend(tree) {
  const s = find(tree, n => n.type === 'section' && n.props.className === 'fc-trend')
  if (!s) return null
  const sec = s.node
  const sub = find(sec, cls('fc-sec-sub'))
  const alert = find(sec, n => n.props.role === 'alert')
  const box = find(sec, cls('fc-trail-state'))
  return {
    state: sec.props['data-state'],
    busy: sec.props['aria-busy'] ?? null,
    stale: sec.props['data-stale'] ?? null,
    labelledBy: sec.props['aria-labelledby'],
    heading: norm(find(sec, n => n.type === 'h2').node),
    sub: sub ? norm(sub.node) : null,
    srcs: findAll(sec, n => n.props.className === 'fc-trail-pill' && !!find(n, cls('fc-trail-k'))).map(h => norm(h.node)),
    alert: alert ? { text: norm(alert.node), retry: !!find(alert.node, byAttr('data-action', 'retry-history')) } : null,
    empty: box ? { kind: box.node.props['data-state'], title: norm(find(box.node, n => n.type === 'h3').node), cmd: norm(find(box.node, cls('fc-trend-cmd')).node), text: norm(box.node) } : null,
    skeletons: (() => { const k = find(sec, cls('fc-trend-skel')); return k ? k.node.props.children.length : 0 })(),
    grid: !!find(sec, cls('fc-trend-grid')),
    cards: findAll(sec, cls('fc-card fc-spark')).map(readSpark)
  }
}
const TREND_TS = i => `2026-11-${String(1 + i * 2).padStart(2, '0')}T12:00:00.000000+00:00`
const SNAP_SRC = ['merge', 'merge', 'retro', 'merge', 'panel_consensus', 'merge', 'merge', 'retro', 'merge', 'panel_consensus', 'merge', 'merge', 'panel_consensus', 'merge']
const SNAPS = Array.from({ length: 14 }, (_, i) => ({
  id: `ms_${String(i).padStart(12, '0')}`,
  ts: TREND_TS(i),
  source: SNAP_SRC[i],
  diff_pass_rate: [0.78, 0.8, 0.79, 0.82, 0.83, 0.81, 0.84, 0.85, 0.84, 0.86, 0.87, 0.86, 0.88, 0.88][i],
  blackbox_pass_rate: [0.7, 0.72, 0.73, 0.72, 0.75, 0.76, 0.75, 0.78, 0.79, 0.78, 0.8, 0.81, 0.8, 0.82][i],
  escape_rate: [0.15, 0.14, 0.14, 0.13, 0.12, 0.12, 0.11, 0.11, 0.1, 0.1, 0.09, 0.09, 0.08, 0.08][i],
  workstreams_total: [4, 4, 5, 6, 6, 7, 8, 8, 9, 10, 10, 11, 12, 12][i],
  cards_done: [12, 14, 17, 19, 21, 29, 34, 36, 39, 48, 52, 55, 58, 61][i]
}))
const HISTORY = snaps => ({ snapshots: snaps, summary: { total: snaps.length, snapshot_count: snaps.length, first_ts: snaps[0]?.ts ?? null, latest_ts: snaps[snaps.length - 1]?.ts ?? null, by_source: snaps.reduce((a, s) => ({ ...a, [s.source]: (a[s.source] || 0) + 1 }), {}) }, filters: { since: '30d', limit: 100 } })
const PANEL_OK = { entries: [{ label: 'openai/gpt-5.4', vendor: 'openai', index: 0 }, { label: 'google/gemini-3-pro', vendor: 'google', index: 1 }], vendors: ['openai', 'google'], distinct_vendor_count: 2, duplicate_vendors: [], unknown_vendor_models: [], valid: true, warning: null, source: 'settings.verify_panel', advisories: [] }
const PANEL_BAD = { entries: [{ label: 'openai/gpt-5.4', vendor: 'openai', index: 0 }, { label: 'openai/o4-mini', vendor: 'openai', index: 1 }, { label: 'mystery-model', vendor: null, index: 2 }], vendors: ['openai'], distinct_vendor_count: 1, duplicate_vendors: ['openai'], unknown_vendor_models: ['mystery-model'], valid: false, warning: 'rigor=high verify panel collapses to a single vendor family (openai).', source: 'auxiliary.factonio_verify_panel', advisories: ['Ignored 1 malformed panel entry.'] }
const PANEL_NONE = { entries: [], vendors: [], distinct_vendor_count: 0, duplicate_vendors: [], unknown_vendor_models: [], valid: false, warning: 'rigor=high requires a verification panel of at least 2 models from distinct vendor families, but no verify_panel is configured.', source: 'unset', advisories: [] }
const POLISH_METRICS = (vp, extra = {}) => ({
  range: '30d', tenant: null,
  totals: { cards: 9, done: 7, running: 1, blocked: 1, triage: 0, workstreams: 3 },
  phases: {},
  verification: { diff: { pass: 4, reject: 1, inconclusive: 0, pass_rate: 0.8 }, blackbox: { pass: 3, reject: 1, inconclusive: 1, pass_rate: 0.6 } },
  rework: { avg_runs_per_implement_card: 1.2, cards_with_rework: 1, first_pass_rate: 0.8 },
  escapes: { count: 0, verified_total: 4, escape_rate: 0 },
  ...(vp === undefined ? {} : { verification_panels: vp }),
  ...extra
})

const lane = o => card({ status: 'done', phase: 'verify-diff', title: '[verify-diff] lane', created_at: 500, ...o })
const consensusRow = (tenant, panel_id, result, evidence = {}, extra = {}) => dec({ id: `d_vpc_${panel_id}_${result}`, ts: '2026-11-25T17:00:00.000000Z', tenant, phase: 'verify', actor: 'orchestrator', decision: 'verify_panel_consensus', why: 'panel', result, evidence: { panel_id, has_disagreement: false, disagreement_summary: null, ...evidence }, ...extra })

async function runPolishScenarios(report) {
  const P = report.polish = {}
  // --- pure helpers ---------------------------------------------------------
  P.verdictTone = Object.fromEntries(['pass', 'reject', 'fail', 'inconclusive', 'INCONCLUSIVE', 'weird', null].map(v => [String(v), mod.verdictTone(v)]))
  P.spark = {
    one: mod.sparkPoints([0.5], 0, 1),
    fourteen: mod.sparkPoints(SNAPS.map(s => s.diff_pass_rate), 0.6, 1),
    flat: mod.sparkPoints([3, 3], 3, 3),
    gapPath: mod.sparkPath(mod.sparkPoints([0.1, null, 0.3, 0.4], 0, 1)),
    path14: mod.sparkPath(mod.sparkPoints(SNAPS.map(s => s.diff_pass_rate), 0.6, 1))
  }
  const T0 = 'acme-api-panel'
  const two = (a, b) => [
    lane({ id: 't_l1', tenant: T0, panel_id: 'vp_aa', panel_vendor: 'openai', provider: 'openai', model: 'gpt-5.4', ...a }),
    lane({ id: 't_l2', tenant: T0, panel_id: 'vp_aa', panel_vendor: 'google', provider: 'google', model: 'gemini-3-pro', ...b })
  ]
  const cw = (cards, trail) => JSON.parse(JSON.stringify(mod.computeWorkstreamPanels(cards, trail)))
  P.panels = {
    passNoTrail: cw(two({ verdict: 'pass' }, { verdict: 'pass' }), []),
    passRecorded: cw(two({ verdict: 'pass' }, { verdict: 'pass' }), [consensusRow(T0, 'vp_aa', 'pass')]),
    rejectDisagree: cw(two({ verdict: 'pass' }, { verdict: 'reject', summary: 'migration drops idx_cards_tenant without a backfill' }), []),
    inconclusive: cw(two({ verdict: 'pass' }, { verdict: 'inconclusive', summary: 'No browser available' }), []),
    pending: cw(two({ verdict: 'pass' }, { status: 'running', verdict: null }), []),
    pendingRecordedReject: cw(two({ verdict: 'pass' }, { status: 'running', verdict: null }), [consensusRow(T0, 'vp_aa', 'reject', { has_disagreement: true, disagreement_summary: 'Panel lanes disagree -- pass: verify-diff/openai; reject: verify-diff/google.' })]),
    singleVendor: cw([lane({ id: 't_s1', panel_id: 'vp_sv', panel_vendor: 'openai', verdict: 'pass' }), lane({ id: 't_s2', panel_id: 'vp_sv', panel_vendor: 'openai', verdict: 'pass' })], [consensusRow(T0, 'vp_sv', 'pass')]),
    supersededIgnored: cw(two({ verdict: 'pass' }, { verdict: 'pass' }), [consensusRow(T0, 'vp_aa', 'pass', {}, { is_superseded: true })]),
    newestFirst: cw([...two({ verdict: 'pass' }, { verdict: 'pass' }), lane({ id: 't_n1', panel_id: 'vp_bb', panel_vendor: 'openai', verdict: null, status: 'running', created_at: 900 })], []).map(p => p.id),
    none: cw([card({ id: 't_x', tenant: T0, phase: 'implement', status: 'done' })], []),
    nullInputs: cw(null, null)
  }
  // Expected-lane membership: a recorded/spawned 4-lane panel shrunk to its 2 diff lanes.
  const four = ['openai', 'google', 'openai', 'google'].map((v, i) => lane({ id: 't_' + i, tenant: T0, panel_id: 'vp_m4', panel_vendor: v, phase: i < 2 ? 'verify-diff' : 'verify-blackbox', verdict: 'pass' }))
  const rec4 = (result = 'pass', ev = {}) => consensusRow(T0, 'vp_m4', result, {
    lanes: four.map(c => ({ task_id: c.id, phase: c.phase, vendor: c.panel_vendor, status: 'done', verdict: 'pass' })),
    lane_fingerprint: four.map(c => [c.id, 'pass']), ...ev
  })
  const spawn4 = dec({ id: 'd_sv_m4', ts: '2026-11-25T16:00:00.000000Z', tenant: T0, phase: 'verify', actor: 'orchestrator', decision: 'spawn_verify', why: 'panel', result: 'spawned', evidence: { panel_id: 'vp_m4', panel_lanes: [
    { vendor: 'openai', label: 'openai', verify_diff_task_id: 't_0', verify_blackbox_task_id: 't_2' },
    { vendor: 'google', label: 'google', verify_diff_task_id: 't_1', verify_blackbox_task_id: 't_3' }
  ] } })
  const diff2 = four.slice(0, 2)
  P.panels.missing = {
    recordedLanes: cw(diff2, [rec4()]),
    recordedStringLanes: cw(diff2, [consensusRow(T0, 'vp_m4', 'pass', { lanes: ['t_0', 't_1', 't_2', 't_3'] })]),
    spawned: cw(diff2, [spawn4, consensusRow(T0, 'vp_m4', 'pass')]),
    spawnedNoConsensus: cw(diff2, [spawn4]),
    countOnly: cw(diff2, [consensusRow(T0, 'vp_m4', 'pass', { expected_lane_count: 4 })]),
    rejectWins: cw([diff2[0], { ...diff2[1], verdict: 'reject' }], [rec4()]),
    recordedRejectWins: cw(diff2, [rec4('reject')]),
    full: cw(four, [spawn4, rec4()]),
    staleSubsetPass: cw([...four, lane({ id: 't_9', tenant: T0, panel_id: 'vp_m4', panel_vendor: 'openai', verdict: 'pass' })], [rec4()])
  }

  // --- Registered Repositories: Rigor column + Default rigor editor field ----
  const client = { invalidateQueries: () => Promise.resolve() }
  const R = P.rigor = {}
  let m = mountRepos({ data: { repos: RIGOR_REPOS, defaults: RIGOR_DEFAULTS }, client })
  let tree = m.render()
  R.cells = readRigorCells(tree)
  R.sub = norm(find(tree, n => n.props.className === 'fc-sec-sub' && norm(n).startsWith('Global defaults')).node)
  // inherit -> high
  fire(find(tree, n => n.type === 'button' && n.props['aria-label'] === 'Edit tozes/factonio')); tree = m.render()
  R.opened = readEditor(tree).fields.default_rigor
  change(tree, 'default_rigor', 'high'); tree = m.render()
  R.high = { field: readEditor(tree).fields.default_rigor, msg: readEditor(tree).msg.text, save: readEditor(tree).save }
  change(tree, 'default_rigor', ''); tree = m.render()
  R.revert = { field: readEditor(tree).fields.default_rigor, msg: readEditor(tree).msg.text, save: readEditor(tree).save }
  change(tree, 'default_rigor', 'standard'); tree = m.render()
  R.standardHint = readEditor(tree).fields.default_rigor.hint
  change(tree, 'default_rigor', 'high'); tree = m.render()
  await submitForm(tree); tree = m.render()
  R.saveHigh = { rests: [...m.rests], closed: readEditor(tree) === null }
  // high override -> inherit (null)
  m = mountRepos({ data: { repos: RIGOR_REPOS, defaults: RIGOR_DEFAULTS }, client })
  tree = m.render()
  fire(find(tree, n => n.type === 'button' && n.props['aria-label'] === 'Edit acme/web-dashboard')); tree = m.render()
  R.openedHigh = readEditor(tree).fields.default_rigor
  change(tree, 'default_rigor', ''); tree = m.render()
  R.inheritHint = readEditor(tree).fields.default_rigor.hint
  await submitForm(tree); tree = m.render()
  R.clearRests = [...m.rests]
  // standard override -> high, together with another field
  m = mountRepos({ data: { repos: RIGOR_REPOS, defaults: RIGOR_DEFAULTS }, client })
  tree = m.render()
  fire(find(tree, n => n.type === 'button' && n.props['aria-label'] === 'Edit acme/cli-tools')); tree = m.render()
  change(tree, 'merge_policy', 'ask'); change(tree, 'default_rigor', 'high'); tree = m.render()
  R.twoDirty = readEditor(tree).msg.text
  await submitForm(tree); tree = m.render()
  R.twoRests = [...m.rests]
  // older backend without default_rigor fields: inherited standard, nothing PATCHed for rigor
  m = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
  tree = m.render()
  R.legacyCells = readRigorCells(tree)
  R.changesHelper = {
    toHigh: mod.repoDraftChanges({ default_rigor: 'high' }, RIGOR_REPOS[0]),
    toInherit: mod.repoDraftChanges({ default_rigor: '' }, RIGOR_REPOS[1]),
    untouched: mod.repoDraftChanges({ merge_policy: 'ask' }, RIGOR_REPOS[1]),
    same: mod.repoDraftChanges({ default_rigor: 'high' }, RIGOR_REPOS[1])
  }

  // --- /factonio dashboard: KPI card, verify_panel status, trend strip -------
  const D = P.dash = {}
  const savedModels = queryState
  const dash = (metrics, models, history) => {
    globalThis.__fcMetricsState = metrics
    queryState = models
    globalThis.__fcHistoryState = history
    historyQueries.length = 0
    const mm = mountRepos({ data: { repos: RIGOR_REPOS, defaults: RIGOR_DEFAULTS }, client })
    return { m: mm, tree: mm.render() }
  }
  const ok = data => ({ data, isLoading: false, isError: false })
  let r = dash(ok(POLISH_METRICS({ total: 5, pass: 2, reject: 1, inconclusive: 1, pending: 1, disagreements: 2 })), ok({ ...MODELS, verify_panel: PANEL_OK, default_rigor: 'standard' }), ok(HISTORY(SNAPS)))
  D.populated = { kpi: readKpi(tree = r.tree), vp: readVp(tree), trend: readTrend(tree), order: findAll(tree, n => n.type === 'h2').map(h => norm(h.node)) }
  D.historyQuery = historyQueries.map(q => ({ key: q.key, interval: q.interval }))[0]
  await historyQueries[0].fn()
  D.historyRest = r.m.rests[r.m.rests.length - 1].path
  // keyboard + focus readout on the pass-rate chart
  const plotOf = (t, metric) => find(find(t, n => n.props.className === 'fc-card fc-spark' && n.props['data-metric'] === metric).node, cls('fc-spark-plot')).node
  const key = (metric, k) => { let prevented = false; plotOf(tree, metric).props.onKeyDown({ key: k, preventDefault() { prevented = true }, currentTarget: { blur() {} } }); tree = r.m.render(); return prevented }
  D.keys = {}
  D.keys.homePrevented = key('pass', 'Home')
  D.keys.home = readTrend(tree).cards[0]
  key('pass', 'ArrowRight')
  D.keys.right = readTrend(tree).cards[0].read.text
  key('pass', 'ArrowLeft'); key('pass', 'ArrowLeft')
  D.keys.leftClamped = readTrend(tree).cards[0].read.text
  key('pass', 'End')
  D.keys.end = readTrend(tree).cards[0].read.text
  D.keys.otherKeyPrevented = key('pass', 'a')
  plotOf(tree, 'escape').props.onFocus(); tree = r.m.render()
  D.keys.focusEscape = readTrend(tree).cards[1].svg.hover
  plotOf(tree, 'escape').props.onBlur(); tree = r.m.render()
  D.keys.blurEscape = readTrend(tree).cards[1].svg.hover
  plotOf(tree, 'throughput').props.onPointerMove({ clientX: 10, currentTarget: { getBoundingClientRect: () => ({ left: 0, width: 240 }) } }); tree = r.m.render()
  D.keys.pointer = readTrend(tree).cards[2].read.text
  plotOf(tree, 'throughput').props.onPointerLeave({ currentTarget: {} }); tree = r.m.render()
  D.keys.pointerLeave = readTrend(tree).cards[2].read.text

  r = dash(ok(POLISH_METRICS({ total: 3, pass: 3, reject: 0, inconclusive: 0, pending: 0, disagreements: 0 })), ok({ ...MODELS, verify_panel: PANEL_BAD, default_rigor: 'high' }), ok(HISTORY(SNAPS.slice(-1))))
  D.single = { kpi: readKpi(r.tree), vp: readVp(r.tree), trend: readTrend(r.tree) }
  r = dash(ok(POLISH_METRICS({ total: 0, pass: 0, reject: 0, inconclusive: 0, pending: 0, disagreements: 0 })), ok({ ...MODELS, verify_panel: PANEL_NONE, default_rigor: 'standard' }), ok(HISTORY([])))
  D.empty = { kpi: readKpi(r.tree), vp: readVp(r.tree), trend: readTrend(r.tree) }
  r = dash({ data: undefined, isLoading: true, isError: false }, { data: undefined, isLoading: true, isError: false }, { data: undefined, isLoading: true, isError: false })
  D.loading = { kpi: readKpi(r.tree), vp: readVp(r.tree), trend: readTrend(r.tree), text: norm(r.tree) }
  const refetches = []
  r = dash(ok(POLISH_METRICS(undefined)), { data: undefined, isLoading: false, isError: true, error: new Error('500') }, { data: undefined, isLoading: false, isError: true, error: new Error('503: {"detail":"trail.db is locked"}'), refetch: () => refetches.push('h') })
  D.errorNoData = { kpi: readKpi(r.tree), vp: readVp(r.tree), trend: readTrend(r.tree) }
  fire(find(r.tree, byAttr('data-action', 'retry-history')))
  D.retry = [...refetches]
  r = dash(ok(POLISH_METRICS({ total: 1, pass: 0, reject: 0, inconclusive: 0, pending: 1, disagreements: 0 })), ok({ ...MODELS }), { data: HISTORY(SNAPS.slice(0, 4)), isLoading: false, isError: true, error: new Error('fetch failed'), dataUpdatedAt: Date.now() - 4 * 60000 })
  D.errorStale = { kpi: readKpi(r.tree), vp: readVp(r.tree), trend: readTrend(r.tree) }
  // Partial / null KPI values and unsorted snapshots never crash the strip.
  const partial = [
    { id: 'ms_c', ts: '2026-11-20T00:00:00Z', source: 'retro', diff_pass_rate: null, blackbox_pass_rate: 0.5, escape_rate: null, workstreams_total: 3, cards_done: null },
    { id: 'ms_a', ts: '2026-11-10T00:00:00Z', source: 'merge', diff_pass_rate: 0.9, blackbox_pass_rate: null, escape_rate: 0.25, workstreams_total: null, cards_done: 4 },
    { id: 'ms_b', ts: '2026-11-15T00:00:00Z', source: 'merge' }
  ]
  try {
    r = dash(ok(POLISH_METRICS(null)), ok({ ...MODELS, verify_panel: null }), ok({ snapshots: partial, summary: {}, filters: {} }))
    D.partial = { kpi: readKpi(r.tree), vp: readVp(r.tree), trend: readTrend(r.tree) }
  } catch (e) { D.partialError = String(e && e.stack || e) }
  queryState = savedModels
  globalThis.__fcMetricsState = undefined
  globalThis.__fcHistoryState = undefined

  // --- sidebar: panel strip, vendor chips, inconclusive tones, drawer rows ---
  const S = P.sidebar = {}
  const WS_DIS = 'omnim-ai-factonio-tenant-card-index'
  const WS_PEND = 'omnim-ai-factonio-watchdog-liveness'
  const WS_REC = 'omnim-ai-factonio-recorded-panel'
  const WS_MISS = 'omnim-ai-factonio-shrunk-panel'
  const PR214 = { repo: 'omnim-ai/factonio', number: 214, url: 'https://github.com/omnim-ai/factonio/pull/214' }
  const PANEL_CARDS = [
    card({ id: 't_imp', tenant: WS_DIS, phase: 'implement', status: 'done', title: '[implement] index migration', pr: PR214, created_at: 100 }),
    lane({ id: 't_8f2a', tenant: WS_DIS, panel_id: 'vp_9a4c1e', panel_vendor: 'google', provider: 'google', model: 'gemini-3-pro', verdict: 'reject', pr: PR214, title: '[verify-diff] lane google', summary: 'migration drops idx_cards_tenant without a backfill; rollback leaves orphaned rows', created_at: 500 }),
    lane({ id: 't_8f2b', tenant: WS_DIS, panel_id: 'vp_9a4c1e', panel_vendor: 'openai', provider: 'openai', model: 'gpt-5.4', verdict: 'pass', pr: PR214, title: '[verify-diff] lane openai', created_at: 500 }),
    lane({ id: 't_8f2c', tenant: WS_DIS, panel_id: 'vp_9a4c1e', panel_vendor: 'anthropic', provider: 'anthropic', model: 'claude-opus-5-5', verdict: 'inconclusive', pr: PR214, title: '[verify-diff] lane anthropic', summary: 'Could not reproduce locally', created_at: 501 }),
    card({ id: 't_w1', tenant: WS_PEND, phase: 'implement', status: 'done', title: '[implement] liveness', created_at: 100 }),
    lane({ id: 't_w2', tenant: WS_PEND, panel_id: 'vp_c20d55', panel_vendor: 'openai', provider: 'openai', model: 'gpt-5.4', verdict: 'pass', title: '[verify-blackbox] lane openai', phase: 'verify-blackbox', created_at: 500 }),
    lane({ id: 't_w3', tenant: WS_PEND, panel_id: 'vp_c20d55', panel_vendor: 'google', provider: 'google', model: 'gemini-3-pro', verdict: null, status: 'running', title: '[verify-blackbox] lane google', phase: 'verify-blackbox', created_at: 500 }),
    lane({ id: 't_r1', tenant: WS_REC, panel_id: 'vp_3f71b0', panel_vendor: 'openai', verdict: 'pass', created_at: 500 }),
    lane({ id: 't_r2', tenant: WS_REC, panel_id: 'vp_3f71b0', panel_vendor: 'google', verdict: 'pass', created_at: 500 }),
    lane({ id: 't_r3', tenant: WS_REC, panel_id: 'vp_3f71b0', panel_vendor: 'openai', verdict: 'pass', phase: 'verify-blackbox', created_at: 500 }),
    lane({ id: 't_r4', tenant: WS_REC, panel_id: 'vp_3f71b0', panel_vendor: 'google', verdict: 'pass', phase: 'verify-blackbox', created_at: 500 }),
    lane({ id: 't_m0', tenant: WS_MISS, panel_id: 'vp_5e0d12', panel_vendor: 'openai', verdict: 'pass', created_at: 500 }),
    lane({ id: 't_m1', tenant: WS_MISS, panel_id: 'vp_5e0d12', panel_vendor: 'google', verdict: 'pass', created_at: 500 })
  ]
  const MISS_LANES = ['t_m0', 't_m1', 't_m2', 't_m3'].map(task_id => ({ task_id, status: 'done', verdict: 'pass' }))
  const PANEL_TRAIL = { decisions: [consensusRow(WS_REC, 'vp_3f71b0', 'pass'), consensusRow(WS_MISS, 'vp_5e0d12', 'pass', { lanes: MISS_LANES })], summary: { total: 2 }, filters: {} }
  const readStrip = (t, tenant) => {
    const sec = find(t, n => n.type === 'section' && n.props['data-tenant'] === tenant)
    const st = sec && find(sec.node, cls('fc-sb-panel'))
    if (!st) return null
    const tag = find(st.node, n => n.props.className === 'fc-sb-tag')
    const callout = find(st.node, cls('fc-sb-callout'))
    const body = sec.node.props.children[1].props.children.filter(Boolean).map(ch => ch.props.className || ch.type)
    return {
      label: st.node.props['aria-label'], role: st.node.props.role,
      consensus: st.node.props['data-consensus'], disagree: st.node.props['data-disagree'],
      tag: { tone: tag.node.props['data-tone'], text: norm(tag.node) },
      id: norm(find(st.node, cls('fc-sb-panel-id fc-sb-mono')).node),
      lanes: norm(find(st.node, cls('fc-sb-panel-lanes')).node),
      vendors: findAll(st.node, cls('fc-sb-vendor')).map(h => [h.node.props['data-verdict'], norm(h.node), h.node.props.title]),
      callout: callout ? { tone: callout.node.props['data-tone'], kind: callout.node.props['data-kind'], role: callout.node.props.role, text: norm(callout.node), title: find(callout.node, cls('fc-sb-callout-body')).node.props.title, quote: !!find(callout.node, n => n.type === 'q') } : null,
      bodyOrder: body
    }
  }
  const cardTop = (t, id) => {
    const a = find(t, n => n.type === 'article' && n.props['data-id'] === id).node
    return {
      attn: a.props['data-attn'],
      dot: find(a, cls('fc-sb-dot')).node.props['data-status'],
      dotLabel: find(a, cls('fc-sb-dot')).node.props['aria-label'],
      vendor: (() => { const v = find(find(a, cls('fc-sb-card-top')).node, cls('fc-sb-vendor')); return v ? { text: norm(v.node), title: v.node.props.title } : null })(),
      verdictTone: find(a, n => n.props['data-verdict'] != null)?.node.props['data-tone'] ?? null
    }
  }
  cardsState = { data: { cards: PANEL_CARDS }, isLoading: false, isError: false }
  trailState = { data: PANEL_TRAIL, isLoading: false, isError: false }
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'all' }), focused: null })
  tree = m.render()
  for (const sec of sectionsOf(tree)) {
    if (sec.expanded === 'false') { fire(find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === sec.tenant).node, byAttr('data-action', 'toggle-ws'))); tree = m.render() }
  }
  for (const sec of sectionsOf(tree)) {
    const tg = find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === sec.tenant).node, cls('fc-sb-done-toggle'))
    if (tg && tg.node.props['aria-expanded'] === 'false') { fire(tg); tree = m.render() }
  }
  S.sections = sectionsOf(tree).map(x => ({ tenant: x.tenant, state: x.state, stages: x.stages }))
  S.header = headerOf(tree).tiles
  S.dis = readStrip(tree, WS_DIS)
  S.pend = readStrip(tree, WS_PEND)
  S.rec = readStrip(tree, WS_REC)
  S.miss = readStrip(tree, WS_MISS)
  fire(find(find(tree, n => n.type === 'article' && n.props['data-id'] === 't_m0').node, byAttr('data-action', 'toggle-card'))); tree = m.render()
  const mdl = find(find(tree, n => n.type === 'article' && n.props['data-id'] === 't_m0').node, n => n.type === 'dl').node
  S.missDrawer = Object.fromEntries(findAll(mdl, n => n.type === 'dd').map(h => [h.node.props['data-field'], norm(h.node)]))
  S.cards = Object.fromEntries(['t_8f2a', 't_8f2b', 't_8f2c', 't_w3', 't_imp'].map(id => [id, cardTop(tree, id)]))
  fire(find(find(tree, n => n.type === 'article' && n.props['data-id'] === 't_8f2a').node, byAttr('data-action', 'toggle-card'))); tree = m.render()
  const dl = find(find(tree, n => n.type === 'article' && n.props['data-id'] === 't_8f2a').node, n => n.type === 'dl').node
  S.drawer = {
    order: findAll(dl, n => n.type === 'dd').map(h => h.node.props['data-field']),
    fields: Object.fromEntries(findAll(dl, n => n.type === 'dd').map(h => [h.node.props['data-field'], norm(h.node)])),
    labels: findAll(dl, n => n.type === 'dt').map(h => norm(h.node)),
    panelTone: find(findAll(dl, n => n.type === 'dd' && n.props['data-field'] === 'panel')[0].node, cls('fc-sb-tag')).node.props['data-tone'],
    peers: findAll(findAll(dl, n => n.type === 'dd' && n.props['data-field'] === 'peers')[0].node, cls('fc-sb-vendor')).map(h => h.node.props['data-verdict'])
  }
  fire(find(find(tree, n => n.type === 'article' && n.props['data-id'] === 't_imp').node, byAttr('data-action', 'toggle-card'))); tree = m.render()
  S.nonPanelDrawer = findAll(find(find(tree, n => n.type === 'article' && n.props['data-id'] === 't_imp').node, n => n.type === 'dl').node, n => n.type === 'dd').map(h => h.node.props['data-field'])
  // trail still loading: panel strip renders from /cards alone (no extra requests)
  trailState = { data: undefined, isLoading: true, isError: false }
  m = mountPane({ storage: makeStorage({ 'sidebar.scope': 'all' }), focused: null })
  tree = m.render()
  fire(find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === WS_REC).node, byAttr('data-action', 'toggle-ws'))); tree = m.render()
  S.trailLoading = { rec: readStrip(tree, WS_REC) }
  otherQueries.length = 0
  trailQueries.length = 0
  tree = m.render()
  S.paneQueries = { cards: cardsQueries.map(q => q.key.join('/')), trail: trailQueries.map(q => q.key.join('/')), other: [...otherQueries] }
  cardsState = { data: undefined, isLoading: true, isError: false }
  trailState = { data: undefined, isLoading: true, isError: false }
  globalThis.__fcReposState = undefined
  globalThis.__fcQueryClient = undefined
}
// ---------------------------------------------------------------------------
// Wave 5: workstream modes, /arena strip + drawer, 12-phase trail, harness badge
// ---------------------------------------------------------------------------
const ICON_SIG = [
  ['attention', 'M6 1.6 10.8 10H1.2z'], ['check', 'M2.5 6.3 5 8.7l4.5-5.2'], ['x', 'M3.5 3.5l5 5M8.5 3.5l-5 5'],
  ['queued', 'M6 3.6V6l1.6 1.1'], ['trend', 'M1.8 8.6l2.7-2.7 1.9 1.9 3.8-3.8'], ['retry', 'M9.6 5.2A3.8 3.8 0 1 0 9 8'],
  ['eye', 'M1.2 6S3 2.8 6 2.8 10.8 6 10.8 6 9 9.2 6 9.2 1.2 6 1.2 6z'], ['arena', 'M1.8 2.6 5 6 1.8 9.4'],
  ['shieldOk', 'M4.4 6.1 5.5 7.2 7.6 4.9'], ['shield', 'M6 1.5 9.8 3v3c0 2.2-1.6 3.8-3.8 4.5C3.8 9.8 2.2 8.2 2.2 6V3z'],
  ['doc', 'M3 1.6h4l2 2v6.8H3z']
]
function iconOf(node) {
  const svg = node && node.type === 'svg' ? node : find(node, n => n.type === 'svg')?.node
  if (!svg) return null
  const kids = (Array.isArray(svg.props.children) ? svg.props.children : [svg.props.children]).filter(Boolean)
  const ds = kids.map(k => k.props.d).filter(Boolean)
  for (const [name, d] of ICON_SIG) if (ds.includes(d)) return name
  const circle = kids.find(k => k.type === 'circle')
  if (circle && circle.props.strokeDasharray === '15 11') return 'spin'
  if (circle && kids.length === 1 && circle.props.r === 4.2) return 'pending'
  return 'other'
}
// Every string reachable in a rendered tree: text and every string prop (titles, aria labels, hrefs).
function allStrings(node, out = []) {
  if (node == null || typeof node === 'boolean') return out
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (Array.isArray(node)) { node.forEach(n => allStrings(n, out)); return out }
  if (typeof node !== 'object') return out
  for (const [k, v] of Object.entries(node.props || {})) {
    if (k === 'children') continue
    if (typeof v === 'string') out.push(v)
    else if (v && typeof v === 'object' && !Array.isArray(v) && k !== 'style') out.push(JSON.stringify(v))
  }
  allStrings(node.props && node.props.children, out)
  return out
}
const classOf = n => String(n.props.className || '')
const hasCls = c => n => classOf(n).split(/\s+/).includes(c)

async function runWave5Scenarios(report) {
  const W = report.wave5 = {}
  const J = v => JSON.parse(JSON.stringify(v))

  // --- pure helpers ---------------------------------------------------------
  const TN = 'omnim-ai-factonio-wave-5-ui'
  const startRow = (tenant, kind, readOnly, ts) => ({ id: `d_sw_${kind}`, ts, tenant, decision: 'start_workstream', phase: 'define', evidence: { kind, read_only: readOnly } })
  W.mode = {
    perfApi: mod.workstreamMode(TN, { [TN]: { kind: 'perf', read_only: false, report_path: null, report_exists: false } }, [], []),
    investigateReady: mod.workstreamMode(TN, { [TN]: { kind: 'investigate', read_only: true, report_path: '/home/u/.hermes/plugin-data/factonio/runs/x/investigation.md', report_exists: true } }, [], []),
    investigateWinPath: mod.workstreamMode(TN, { [TN]: { kind: 'investigate', read_only: true, report_path: 'C:\\runs\\x\\report.md', report_exists: false } }, [], []),
    investigateHoldoutPath: mod.workstreamMode(TN, { [TN]: { kind: 'investigate', read_only: true, report_path: '/runs/x/holdout/investigation.md', report_exists: true } }, [], []),
    refactorWithPath: mod.workstreamMode(TN, { [TN]: { kind: 'refactor', read_only: false, report_path: '/runs/x/investigation.md', report_exists: true } }, [], []),
    legacyNull: mod.workstreamMode(TN, { [TN]: { kind: null, read_only: false, report_path: null, report_exists: false } }, [{ id: 't1', workstream_kind: 'refactor', created_at: 5 }], []),
    cardFallback: mod.workstreamMode(TN, undefined, [{ id: 't1', workstream_kind: 'bug', created_at: 1 }, { id: 't2', workstream_kind: 'perf', created_at: 9 }, { id: 't3', created_at: 20 }], []),
    trailFallback: mod.workstreamMode(TN, undefined, [{ id: 't1', created_at: 1 }], [startRow(TN, 'refactor', false, '2026-11-01T10:00:00Z'), startRow(TN, 'investigate', false, '2026-11-02T10:00:00Z')]),
    trailOtherTenant: mod.workstreamMode(TN, undefined, [], [startRow('other-tenant', 'perf', false, '2026-11-01T10:00:00Z')]),
    unknownKind: mod.workstreamMode(TN, { [TN]: { kind: 'chaos' } }, [{ id: 't', workstream_kind: 'zzz' }], []),
    none: mod.workstreamMode(TN, undefined, undefined, undefined),
    emptyTenant: mod.workstreamMode('', { '': { kind: 'perf' } }, [], [])
  }

  const cand = (slot, vendor, status, ready) => ({ slot, vendor, model: `${vendor}/m`, task_id: `t_${slot}`, status, artifact_ready: ready })
  const arena = (o) => ({ arena_id: 'ar_7f2c9a10', tenant: TN, repo: 'omnim-ai/factonio', target: 'design', title: 'Sidebar layout', status: 'judging', created_at: 100,
    candidates: [cand('c1-anthropic', 'anthropic', 'done', true), cand('c2-openai', 'openai', 'done', true)],
    judge: { task_id: 't_judge', status: 'running', vendor: 'google', model: 'google/gemini', cross_vendor: true },
    ready_for_synthesis: true, synthesized: false, canonical_artifact_exists: false, advisories: [], ...o })
  const tagged = (id, role, extra) => ({ id, tenant: TN, status: 'done', created_at: 10, arena_id: 'ar_legacy01', arena_role: role, arena_target: 'define', ...extra })
  W.arenas = {
    api: J(mod.computeWorkstreamArenas(TN, [], [arena({ arena_id: 'ar_new', created_at: 200 }), arena({ arena_id: 'ar_other', tenant: 'someone-else' }), arena({ arena_id: 'ar_old', created_at: 100 })])),
    apiEmptyIgnoresTags: J(mod.computeWorkstreamArenas(TN, [tagged('t_a', 'candidate', { arena_vendor: 'openai' })], [])),
    sanitized: J(mod.computeWorkstreamArenas(TN, [], [arena({
      title: 'Read holdout/s1.md', advisories: ['see holdout/s2.md', 'Judge waiting on c2-openai'],
      candidates: [{ slot: 'c1', vendor: 'anthropic', model: { nested: true }, task_id: 7, status: 'done', artifact_ready: 'yes', holdout_files: 3 }],
      judge: { task_id: 't_j', status: 'running', vendor: 'HOLDOUT-vendor', model: null, cross_vendor: 'true' }
    }), arena({ arena_id: 'ar_holdout_x' }), { tenant: TN }, null, 'junk'])),
    legacy: J(mod.computeWorkstreamArenas(TN, [
      tagged('t_c1', 'candidate', { arena_slot: 'c1-anthropic', arena_vendor: 'anthropic', status: 'done', created_at: 10 }),
      tagged('t_c2', 'candidate', { arena_slot: 'c2-openai', arena_vendor: 'openai', status: 'running', created_at: 11 }),
      tagged('t_j', 'judge', { arena_vendor: 'google', status: 'todo', created_at: 12 }),
      { id: 't_plain', tenant: TN, status: 'done', created_at: 13 }
    ], undefined)),
    noTenant: J(mod.computeWorkstreamArenas('', [], [arena({ tenant: '' })]))
  }

  const pills = (decision, evidence, gh = 'omnim-ai/factonio') => J(mod.evidencePills({ decision, evidence }, gh)).map(p => ({ key: p.key, label: p.label || null, text: p.text, tone: p.tone || null, href: p.href || null, title: p.title }))
  const ESC = String.fromCharCode(27)
  W.pills = {
    start: pills('start_workstream', { kind: 'investigate', read_only: true, skill: 'factonio:define', skip_define: false }),
    startRw: pills('start_workstream', { kind: 'perf', read_only: false }),
    spawnArena: pills('spawn_arena', { arena_id: 'ar_7f2c9a10', target: 'define', candidates: [{ slot: 'c1', vendor: 'anthropic' }, { slot: 'c2', vendor: 'openai' }], judge_task_id: 't_j' }),
    arenaMany: pills('arena_synthesis', { arena_id: 'ar_1', candidates: ['a', 'b', 'c', 'd'], judge_vendor: 'google' }),
    triage: pills('issue_intake_triage', { repo: 'omnim-ai/factonio', issue_numbers: [412, 413, 414], counts: { stop_the_line: 1, actionable: 1, no_action: 1 }, stop_the_line: [412], actionable: [413], verify_existing_fix: [] }),
    verifyFix: pills('issue_intake_verify_fix', { pr_number: 44, head_sha: 'abcdef1234', issue_number: 412, verdict: 'verified_fix' }),
    verifyFixNoGh: pills('issue_intake_verify_fix', { issue_number: 412, verdict: 'fix_incomplete' }, null),
    skillEval: pills('skill_eval', { pair_id: 'p1', eval_phase: 'define', winner: 'B', winner_label: 'define-v3', verdict: 'B', decided_by: 'judge', disqualified: ['A'], scores: { A: 1 } }),
    otherKind: pills('spawn_verify', { kind: 'diff', verdict: 'pass' }),
    holdout: pills('arena_synthesis', {
      holdout_files: 3, path: '/runs/x/holdout/a.md', note: 'HOLDOUT scenario 2', list: ['ok', 'holdout'], nested: { deep: ['x', { p: 'holdout' }] },
      wide: 'ｈｏｌｄｏｕｔ', ansi: `hold${ESC}[31mout`, zw: 'hold\u200bout', ctrl: 'hold\u0007out', ['hold\u200dout_key']: 1
    }),
    holdoutKeyOnly: pills('start_workstream', { holdout_dir: 'x' }),
    holdoutValueOnly: pills('start_workstream', { kind: 'see holdout/' })
  }
  W.pills.noWinner = pills('skill_eval', { winner: 'none', disqualified: ['A', 'B'] })
  W.pills.tie = pills('skill_eval', { winner: 'tie' })

  const hb = h => J(mod.summarizeHarnessBadge(h))
  const full = { present: true, mode: 'full', required_hooks: 4, optional_hooks: 1, invariant_hooks: 2, visual_diff_hooks: 2, frozen_visual_hooks: 1, has_manifest_error: false }
  W.harness = {
    full: hb(full),
    oneHook: hb({ ...full, required_hooks: 1, optional_hooks: 0, frozen_visual_hooks: 0, visual_diff_hooks: 0 }),
    mdOnly: hb({ ...full, mode: 'verify_md_only', required_hooks: 0, optional_hooks: 0 }),
    error: hb({ ...full, has_manifest_error: true }),
    absent: hb({ present: false, mode: 'absent', required_hooks: 0, optional_hooks: 0, invariant_hooks: 0, visual_diff_hooks: 0, frozen_visual_hooks: 0, has_manifest_error: false }),
    garbageCounts: hb({ present: true, mode: '<b>', required_hooks: -1, optional_hooks: 'x', frozen_visual_hooks: 1.5 }),
    legacy: hb(undefined),
    nullish: hb(null)
  }

  // --- sidebar render ---------------------------------------------------------
  const PERF = 'omnim-ai-factonio-wave-5-desktop-ui'
  const INV = 'omnim-ai-factonio-cache-miss-spike'
  const REF = 'omnim-ai-factonio-split-orchestrator'
  const BUG = 'omnim-ai-factonio-fix-login'
  const PR = { repo: 'omnim-ai/factonio', number: 141, url: 'https://github.com/omnim-ai/factonio/pull/141' }
  const wcard = o => card({ status: 'done', created_at: 100, ...o })
  const W5_CARDS = [
    wcard({ id: 't_p_def', tenant: PERF, phase: 'define', title: '[define] Spec', created_at: 100, workstream_kind: 'perf' }),
    wcard({ id: 't_p_c1', tenant: PERF, phase: 'design', title: '[design] Candidate: sidebar layout', created_at: 110, workstream_kind: 'perf', model: 'claude-opus-4-1', arena_id: 'ar_7f2c9a10', arena_role: 'candidate', arena_slot: 'c1-anthropic', arena_vendor: 'anthropic', arena_target: 'design' }),
    wcard({ id: 't_p_c2', tenant: PERF, phase: 'design', title: '[design] Candidate: sidebar layout', created_at: 111, workstream_kind: 'perf', arena_id: 'ar_7f2c9a10', arena_role: 'candidate', arena_slot: 'c2-openai', arena_vendor: 'openai', arena_target: 'design', panel_vendor: 'openai' }),
    wcard({ id: 't_p_j', tenant: PERF, phase: 'design', status: 'running', title: '[design] Judge arena candidates', created_at: 120, workstream_kind: 'perf', arena_id: 'ar_7f2c9a10', arena_role: 'judge', arena_vendor: 'google', arena_target: 'design' }),
    wcard({ id: 't_p_v1', tenant: PERF, phase: 'verify-diff', status: 'running', title: '[verify:diff] panel', created_at: 130, workstream_kind: 'perf', panel_id: 'vp_3e91a7', panel_vendor: 'openai', pr: PR }),
    wcard({ id: 't_i_def', tenant: INV, phase: 'define', status: 'running', title: '[define] [investigate] Why do /cards cache misses spike?', created_at: 200, workstream_kind: 'investigate' }),
    wcard({ id: 't_r_imp', tenant: REF, phase: 'implement', status: 'running', title: '[implement] Extract scheduler', created_at: 300, workstream_kind: 'refactor' }),
    wcard({ id: 't_b_imp', tenant: BUG, phase: 'implement', status: 'running', title: '[implement] Fix login', created_at: 400, workstream_kind: 'bug' })
  ]
  const W5_WS = {
    [PERF]: { kind: 'perf', read_only: false, report_path: null, report_exists: false },
    [INV]: { kind: 'investigate', read_only: true, report_path: '/home/u/.hermes/plugin-data/factonio/runs/x/investigation.md', report_exists: false },
    [REF]: { kind: 'refactor', read_only: false, report_path: null, report_exists: false },
    [BUG]: { kind: 'bug', read_only: false, report_path: null, report_exists: false }
  }
  const W5_ARENAS = [arena({ tenant: PERF, candidates: [cand('c1-anthropic', 'anthropic', 'done', true), cand('c2-openai', 'openai', 'done', false)], judge: { task_id: 't_p_j', status: 'running', vendor: 'google', model: 'google/gemini-3-pro', cross_vendor: true } })]
  W5_ARENAS[0].candidates[0].task_id = 't_p_c1'
  W5_ARENAS[0].candidates[1].task_id = 't_p_c2'

  const readArena = s => {
    if (!s) return null
    const top = find(s.node, hasCls('fc-sb-panel-top')).node
    const co = find(s.node, hasCls('fc-sb-callout'))
    return {
      label: s.node.props['aria-label'],
      role: s.node.props.role,
      status: s.node.props['data-status'],
      lead: { title: find(top, hasCls('fc-sb-arena-k')).node.props.title, word: norm(find(top, hasCls('fc-sb-arena-word')).node), icon: iconOf(find(top, hasCls('fc-sb-arena-k')).node) },
      target: (() => { const t = find(top, hasCls('fc-sb-arena-target')); return t ? norm(t.node) : null })(),
      tag: (() => { const t = find(top, hasCls('fc-sb-tag')).node; return { text: norm(t), tone: t.props['data-tone'] ?? null, title: t.props.title } })(),
      id: norm(find(top, hasCls('fc-sb-panel-id')).node),
      count: (() => { const c = find(top, hasCls('fc-sb-panel-lanes')).node; return { text: norm(c), title: c.props.title } })(),
      topOrder: top.props.children.filter(Boolean).map(n => classOf(n)),
      chips: findAll(find(s.node, hasCls('fc-sb-vendors')).node, hasCls('fc-sb-vendor')).map(h => ({ text: norm(h.node), run: h.node.props['data-run'] ?? null, role: h.node.props['data-role'] ?? null, cross: h.node.props['data-cross'] ?? null, icon: iconOf(h.node), title: h.node.props.title })),
      callout: co ? { kind: co.node.props['data-kind'], tone: co.node.props['data-tone'] ?? null, role: co.node.props.role, text: norm(co.node), title: find(co.node, hasCls('fc-sb-callout-body')).node.props.title } : null
    }
  }
  const readSection = h => {
    const sec = h.node
    const mode = find(sec, hasCls('fc-sb-mode'))
    const rep = find(sec, hasCls('fc-sb-report'))
    const body = find(sec, n => typeof n.props.id === 'string' && n.props.id.startsWith('fc-sb-ws-'))
    return {
      tenant: sec.props['data-tenant'],
      label: sec.props['aria-label'],
      dataMode: sec.props['data-mode'] ?? null,
      labelRow: (() => { const r = find(sec, hasCls('fc-sb-ws-labelrow')); return r ? r.node.props.children.filter(Boolean).map(n => classOf(n)) : null })(),
      mode: mode ? { kind: mode.node.props['data-mode'], text: norm(mode.node), title: mode.node.props.title, icon: iconOf(mode.node), ro: !!find(mode.node, hasCls('fc-sb-mode-ro')) } : null,
      report: rep ? { ready: rep.node.props['data-ready'], text: norm(rep.node), title: rep.node.props.title, icon: iconOf(rep.node) } : null,
      meta: norm(find(sec, hasCls('fc-sb-ws-meta')).node),
      bodyOrder: body ? body.node.props.children.filter(Boolean).map(n => classOf(n).split(' ').filter(c => c !== 'fc-sb-panel').join(' ') || classOf(n)) : null,
      arena: readArena(find(sec, hasCls('fc-sb-arena'))),
      cards: Object.fromEntries(findAll(sec, n => n.type === 'article').map(a => {
        const tag = find(a.node, hasCls('fc-sb-arena-tag'))
        return [a.node.props['data-id'], {
          arenaTag: tag ? { text: norm(tag.node), title: tag.node.props.title, role: tag.node.props['data-arena-role'] } : null,
          panelChip: !!find(a.node, n => n.props['data-panel-vendor'] != null),
          topOrder: find(a.node, hasCls('fc-sb-card-top')).node.props.children.filter(Boolean).map(n => classOf(n).split(' ')[classOf(n).split(' ').length - 1])
        }]
      }))
    }
  }
  const readDrawer = (tree, id) => {
    const art = find(tree, n => n.type === 'article' && n.props['data-id'] === id)
    const dl = art && find(art.node, n => n.type === 'dl')
    if (!dl) return null
    const dds = findAll(dl.node, n => n.type === 'dd')
    return {
      order: dds.map(h => h.node.props['data-field']),
      labels: findAll(dl.node, n => n.type === 'dt').map(h => norm(h.node)),
      fields: Object.fromEntries(dds.map(h => [h.node.props['data-field'], norm(h.node)])),
      tags: Object.fromEntries(dds.map(h => [h.node.props['data-field'], findAll(h.node, hasCls('fc-sb-tag')).map(t => ({ text: norm(t.node), tone: t.node.props['data-tone'] ?? null }))])),
      chips: Object.fromEntries(dds.map(h => [h.node.props['data-field'], findAll(h.node, hasCls('fc-sb-vendor')).map(t => ({ text: norm(t.node), run: t.node.props['data-run'] ?? null, cross: t.node.props['data-cross'] ?? null, icon: iconOf(t.node) }))])),
      modePill: (() => { const p = find(dl.node, hasCls('fc-sb-mode')); return p ? { kind: p.node.props['data-mode'], ro: !!find(p.node, hasCls('fc-sb-mode-ro')) } : null })(),
      reportTitle: (() => { const d = dds.find(h => h.node.props['data-field'] === 'report'); const m = d && find(d.node, hasCls('fc-sb-mono')); return m ? m.node.props.title : null })()
    }
  }
  let m5Render = null
  const renderPane = async (data, trail, openIds = []) => {
    cardsState = { data, isLoading: false, isError: false }
    trailState = trail
    const m = mountPane({ focused: null })
    m5Render = m.render
    let tree = m.render()
    for (const d of findAll(tree, n => n.props.className === 'fc-sb-done-toggle' && n.props['aria-expanded'] === 'false')) {
      const ws = d.anc.find(a => a.type === 'section')
      fire(find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === ws.props['data-tenant']).node, n => n.props.className === 'fc-sb-done-toggle'))
      tree = m.render()
    }
    for (const id of openIds) {
      const art = find(tree, n => n.type === 'article' && n.props['data-id'] === id)
      if (!art) continue
      fire(find(art.node, byAttr('data-action', 'toggle-card')))
      tree = m.render()
    }
    return tree
  }
  const TRAIL_READY = { data: { decisions: [], summary: {} }, isLoading: false, isError: false }
  const SB = W.sidebar = {}
  try {
    cardsQueries.length = 0; trailQueries.length = 0; otherQueries.length = 0
    let tree = await renderPane({ cards: W5_CARDS, counts: {}, workstreams: W5_WS, arenas: W5_ARENAS }, TRAIL_READY, ['t_p_c1', 't_p_j', 't_i_def', 't_b_imp', 't_p_v1'])
    SB.sections = Object.fromEntries(findAll(tree, n => n.type === 'section').map(h => [h.node.props['data-tenant'], readSection(h)]))
    SB.drawers = Object.fromEntries(['t_p_c1', 't_p_j', 't_i_def', 't_b_imp', 't_p_v1'].map(id => [id, readDrawer(tree, id)]))
    SB.queries = { cards: [...new Set(cardsQueries.map(q => q.key.join('/')))], trail: [...new Set(trailQueries.map(q => q.key.join('/')))], other: [...otherQueries] }

    // Report ready + all 7 arena statuses (one workstream per status) + legacy-only workstream.
    const STATUSES = ['candidates_in_progress', 'ready_for_synthesis', 'judging', 'synthesized', 'needs_attention', 'degraded', 'blocked']
    const statusCards = STATUSES.map((s, i) => wcard({ id: `t_s_${i}`, tenant: `acme-api-arena-${s}`, phase: 'design', status: 'running', title: '[design] x', created_at: 500 + i }))
    const statusArenas = STATUSES.map(s => arena({
      tenant: `acme-api-arena-${s}`, status: s, arena_id: `ar_${s}`,
      candidates: s === 'candidates_in_progress'
        ? [cand('c1-anthropic', 'anthropic', 'running', false), cand('c2-openai', 'openai', 'todo', false)]
        : s === 'needs_attention'
          ? [cand('c1-anthropic', 'anthropic', 'done', false), cand('c2-openai', 'openai', 'blocked', false)]
          : [cand('c1-anthropic', 'anthropic', 'done', true), cand('c2-openai', 'openai', 'done', true)],
      judge: s === 'candidates_in_progress' || s === 'blocked' ? null
        : { task_id: 't_judge', status: s === 'synthesized' ? 'done' : 'running', vendor: s === 'degraded' ? 'openai' : 'google', model: 'm', cross_vendor: s !== 'degraded' },
      advisories: s === 'judging' ? ['Judge waiting on canonical artifact'] : []
    }))
    tree = await renderPane({
      cards: [...statusCards, wcard({ id: 't_inv2', tenant: INV, phase: 'define', status: 'running', title: '[define] [investigate] q', created_at: 900 })],
      counts: {},
      workstreams: { [INV]: { kind: 'investigate', read_only: true, report_path: '/x/runs/inv/investigation.md', report_exists: true } },
      arenas: statusArenas
    }, TRAIL_READY)
    SB.statuses = Object.fromEntries(findAll(tree, n => n.type === 'section').filter(h => h.node.props['data-tenant'].startsWith('acme-api-arena-')).map(h => [h.node.props['data-tenant'].slice('acme-api-arena-'.length), readArena(find(h.node, hasCls('fc-sb-arena')))]))
    SB.reportReady = readSection(find(tree, n => n.type === 'section' && n.props['data-tenant'] === INV))

    // Older backend: no `workstreams` / `arenas` keys; card tags + start_workstream rows drive the UI.
    const LEG = 'omnim-ai-factonio-legacy-arena'
    const LEG2 = 'omnim-ai-factonio-legacy-trail'
    const legacyCards = [
      wcard({ id: 't_l_c1', tenant: LEG, phase: 'define', status: 'done', title: '[define] cand', created_at: 10, workstream_kind: 'refactor', arena_id: 'ar_legacy01', arena_role: 'candidate', arena_slot: 'c1-anthropic', arena_vendor: 'anthropic', arena_target: 'define' }),
      wcard({ id: 't_l_c2', tenant: LEG, phase: 'define', status: 'running', title: '[define] cand', created_at: 11, arena_id: 'ar_legacy01', arena_role: 'candidate', arena_slot: 'c2-openai', arena_vendor: 'openai', arena_target: 'define' }),
      wcard({ id: 't_l_t', tenant: LEG2, phase: 'define', status: 'running', title: '[define] x', created_at: 12 })
    ]
    tree = await renderPane({ cards: legacyCards, counts: {} }, { data: { decisions: [{ id: 'd_l1', ts: '2026-11-27T10:00:00Z', tenant: LEG2, decision: 'start_workstream', phase: 'define', evidence: { kind: 'investigate', read_only: true } }], summary: {} }, isLoading: false, isError: false }, ['t_l_c1'])
    SB.legacy = Object.fromEntries(findAll(tree, n => n.type === 'section').map(h => [h.node.props['data-tenant'], readSection(h)]))
    SB.legacyDrawer = readDrawer(tree, 't_l_c1')

    // Pre-Wave-5 cards (no mode/arena fields at all) render exactly as before.
    tree = await renderPane({ cards: [wcard({ id: 't_old', tenant: 'acme-api-old', phase: 'implement', status: 'running', title: '[implement] old' })], counts: {} }, TRAIL_READY, ['t_old'])
    SB.plain = { section: readSection(find(tree, n => n.type === 'section')), drawer: readDrawer(tree, 't_old') }

    // Barrier: holdout strings in every Wave 5 input never reach the rendered sidebar.
    const HT = 'omnim-ai-factonio-barrier'
    tree = await renderPane({
      cards: [wcard({ id: 't_h1', tenant: HT, phase: 'define', status: 'running', title: '[define] x', created_at: 10, arena_id: 'ar_bar01', arena_role: 'candidate', arena_slot: 'holdout-slot', arena_vendor: 'holdout', arena_target: 'define' })],
      counts: {},
      workstreams: { [HT]: { kind: 'investigate', read_only: true, report_path: '/runs/holdout/investigation.md', report_exists: true } },
      arenas: [arena({ tenant: HT, arena_id: 'ar_bar01', title: 'holdout title', advisories: ['Read holdout/scenario-1.md'], candidates: [cand('holdout-c1', 'anthropic', 'done', true)], judge: { task_id: 't_j', status: 'running', vendor: 'holdout', model: 'holdout/m', cross_vendor: true } })]
    }, { data: { decisions: [{ id: 'd_h1', ts: '2026-11-27T10:00:00Z', tenant: HT, decision: 'arena_synthesis', phase: 'define', result: 'synthesized', evidence: { arena_id: 'ar_bar01', holdout_files: 4, note: 'holdout/x' } }], summary: {} }, isLoading: false, isError: false }, ['t_h1'])
    const trailToggle = find(tree, byAttr('data-action', 'toggle-trail'))
    if (trailToggle) { fire(trailToggle) }
    SB.barrierStrings = allStrings(tree).filter(s => /holdout/i.test(s))
    SB.barrierSection = readSection(find(tree, n => n.type === 'section'))

    // Sidebar trail strip chips reuse evidencePills, including the warn/info tones.
    const ST = 'omnim-ai-factonio-strip-chips'
    tree = await renderPane({ cards: [wcard({ id: 't_st', tenant: ST, phase: 'define', status: 'running', title: '[define] x' })], counts: {} }, {
      data: { decisions: [{ id: 'd_st1', ts: '2026-11-27T10:00:00Z', tenant: ST, decision: 'start_workstream', phase: 'define', result: 'spawned', evidence: { kind: 'investigate', read_only: true, holdout_dir: '/x' } }], summary: {} }, isLoading: false, isError: false
    })
    fire(find(tree, byAttr('data-action', 'toggle-trail')))
    tree = m5Render()
    SB.stripChips = findAll(tree, n => n.props.className === 'fc-sb-chip').map(h => ({ ev: h.node.props['data-ev'], text: norm(h.node), tone: h.node.props['data-tone'] ?? null }))
  } catch (e) { SB.error = String(e && e.stack || e) }

  // --- dashboard: harness badges + 12-phase Wave 5 trail ------------------------
  const D = W.dashboard = {}
  try {
    const client = { invalidateQueries: () => Promise.resolve() }
    const repos = [
      { ...REPOS[0], slug: 'omnim-ai-factonio', repo: 'omnim-ai/factonio', harness: full },
      { ...REPOS[1], slug: 'acme-api', repo: 'acme/api', harness: { ...full, mode: 'verify_md_only', required_hooks: 0, optional_hooks: 0, frozen_visual_hooks: 0 } },
      { ...REPOS[0], slug: 'acme-web', repo: 'acme/web', harness: { ...full, has_manifest_error: true } },
      { ...REPOS[1], slug: 'acme-cli', repo: 'acme/cli', harness: { present: false, mode: 'absent', required_hooks: 0, optional_hooks: 0, invariant_hooks: 0, visual_diff_hooks: 0, frozen_visual_hooks: 0, has_manifest_error: false } },
      { ...REPOS[0], slug: 'acme-old', repo: 'acme/old' }
    ]
    const TW = 'omnim-ai-factonio-wave-5-desktop-ui'
    const TI = 'omnim-ai-factonio-cache-miss-spike'
    const row = (id, ts, o) => ({ id, ts, actor: 'orchestrator', repo_slug: 'omnim-ai-factonio', why: '', result: 'recorded', is_superseded: false, superseded_by: [], supersedes_id: null, card_id: null, ...o })
    const decisions = [
      row('d_w01', '2026-11-27T10:00:00.000000+00:00', { tenant: TW, decision: 'start_workstream', phase: 'define', result: 'spawned', evidence: { kind: 'perf', read_only: false } }),
      row('d_w02', '2026-11-27T10:05:00.000000+00:00', { tenant: TI, decision: 'start_workstream', phase: 'define', result: 'spawned', evidence: { kind: 'investigate', read_only: true } }),
      row('d_w03', '2026-11-27T10:10:00.000000+00:00', { tenant: TW, decision: 'arena_synthesis', phase: 'design', result: 'synthesized', evidence: { arena_id: 'ar_7f2c9a10', target: 'design', candidates: [{ vendor: 'anthropic' }, { vendor: 'openai' }], judge_vendor: 'google', holdout_files: 3 } }),
      row('d_w04', '2026-11-27T10:15:00.000000+00:00', { tenant: TW, decision: 'issue_intake_triage', phase: 'intake', result: 'stop_the_line', evidence: { repo: 'omnim-ai/factonio', issue_numbers: [412, 413], counts: { stop_the_line: 1 }, stop_the_line: [412], actionable: [] } }),
      row('d_w05', '2026-11-27T10:20:00.000000+00:00', { tenant: TW, decision: 'issue_intake_verify_fix', phase: 'intake', result: 'verified_fix', evidence: { issue_number: 412, verdict: 'verified_fix', note: 'see holdout/s1' } }),
      row('d_w06', '2026-11-27T10:25:00.000000+00:00', { tenant: TW, decision: 'harness_audit', phase: 'harness', result: 'doc_drift', evidence: { drifted: true } }),
      row('d_w07', '2026-11-27T10:30:00.000000+00:00', { tenant: TW, decision: 'skill_eval', phase: 'eval', actor: 'eval', result: 'decided', evidence: { eval_phase: 'define', winner: 'B', winner_label: 'define-v3', verdict: 'B', decided_by: 'judge', disqualified: [] } }),
      row('d_w08', '2026-11-27T10:35:00.000000+00:00', { tenant: TW, decision: 'skill_eval', phase: 'eval', actor: 'eval', result: 'no_winner', evidence: { eval_phase: 'verify', winner: 'none', disqualified: ['A', 'B'] } }),
      row('d_w09', '2026-11-27T10:40:00.000000+00:00', { tenant: TI, decision: 'spawn_implement', phase: 'implement', result: 'flaky', evidence: {} })
    ]
    trailState = { data: { decisions, summary: { total: decisions.length, active: decisions.length, superseded: 0, by_tenant: { [TW]: 7, [TI]: 2 } } }, isLoading: false, isError: false }
    const m = mountRepos({ data: { repos, defaults: REPO_DEFAULTS }, client })
    let tree = m.render()
    const tbl = reposSec(tree).node
    D.colgroup = findAll(tbl, n => n.type === 'col').length
    D.headers = findAll(tbl, n => n.type === 'th').length
    D.repos = Object.fromEntries(findAll(tbl, n => n.type === 'tr' && n.props['data-slug']).map(h => {
      const tds = h.node.props.children.filter(Boolean)
      const b = find(tds[tds.length - 1], hasCls('fc-harness'))
      return [h.node.props['data-slug'], {
        cells: tds.length,
        firstCell: tds[0].props.children.filter(Boolean).map(n => classOf(n)),
        actions: find(tds[tds.length - 1], hasCls('fc-act-btns')).node.props.children.filter(Boolean).map(n => classOf(n)),
        badge: b ? { state: b.node.props['data-state'], text: norm(find(b.node, hasCls('fc-harness-t')).node), key: (() => { const k = find(b.node, hasCls('fc-harness-k')); return k ? norm(k.node) : null })(), title: b.node.props.title, sr: norm(find(b.node, hasCls('fc-sr')).node), icon: iconOf(b.node) } : null
      }]
    }))
    const t = readTrail(tree)
    D.trail = { phases: t.bar.selects.phase.groups, phaseAll: t.bar.selects.phase.options[0], tenantOptions: t.bar.selects.tenant.options, rows: Object.fromEntries(t.rows.map(r => [r.id, r])) }
    // Phase filter "eval" then rows show the ws pill with its mode annotation.
    trailPhase(tree, 'eval'); tree = m.render()
    const te = readTrail(tree)
    D.evalRows = te.rows.map(r => r.id)
    D.evalWsPills = te.rows.map(r => r.pills.find(p => p.ev === 'tenant')?.text ?? null)
    trailPhase(tree, 'define'); tree = m.render()
    D.defineWsPills = Object.fromEntries(readTrail(tree).rows.map(r => [r.id, r.pills.find(p => p.ev === 'tenant')?.text ?? null]))
    // Workstream filter + an empty phase -> the "No decisions match" chips carry the mode.
    trailSelect(tree, 'tenant', TI); tree = m.render()
    trailPhase(tree, 'eval'); tree = m.render()
    D.noMatch = readTrail(tree).box
    D.barrierStrings = allStrings(tree).filter(s => /holdout/i.test(s))
    trailAction(tree, 'clear'); tree = m.render()
    D.barrierStringsAll = allStrings(tree).filter(s => /holdout/i.test(s))

    const TONE_CODES = ['synthesized', 'verified_fix', 'decided', 'clean', 'stop_the_line', 'fix_incomplete', 'no_winner', 'proof_failed', 'product_regression', 'flaky', 'not_reproduced', 'tie', 'harness_gap', 'doc_drift', 'actionable', 'no_action']
    trailState = { data: { decisions: TONE_CODES.map((r, i) => row(`d_t${i}`, `2026-11-26T10:${String(i).padStart(2, '0')}:00Z`, { tenant: TW, decision: 'wave5_result', phase: 'eval', result: r, evidence: {} })), summary: {} }, isLoading: false, isError: false }
    D.tones = Object.fromEntries(readTrail(mountRepos({ data: { repos, defaults: REPO_DEFAULTS }, client }).render()).rows.map(r => [r.result, r.tone]))
  } catch (e) { D.error = String(e && e.stack || e) }
  trailState = { data: undefined, isLoading: true, isError: false }
  cardsState = { data: undefined, isLoading: true, isError: false }
  globalThis.__fcReposState = undefined
  globalThis.__fcQueryClient = undefined
}

// ---------------------------------------------------------------------------
// Control plane (Unit 2): pure helpers, Publish PR, report viewer, harness audit,
// design approval and panel / arena Re-evaluate.
// ---------------------------------------------------------------------------
async function runControlPlaneScenarios(report) {
  const C = report.cp = {}
  const J = v => JSON.parse(JSON.stringify(v, (k, x) => (x instanceof Set ? [...x] : x instanceof Map ? Object.fromEntries(x) : x)))
  const NOWMS = Date.parse('2026-11-27T18:00:00Z')

  // --- pure helpers ---------------------------------------------------------
  const r = (id, ts, o = {}) => ({ id, ts, phase: 'gate', actor: 'orchestrator', decision: 'pr_gate', result: 'pass', supersedes_id: null, evidence: {}, ...o })
  const rows = [
    r('a', '2026-11-27T17:00:00Z', { decision: 'merge_pr', phase: 'merge', result: 'merged' }),
    r('b', '2026-11-27T16:55:00Z'), r('c', '2026-11-27T16:50:00Z', { result: 'blocked' }), r('d', '2026-11-27T16:45:00Z'),
    r('e', '2026-11-27T16:40:00Z', { decision: 'bootstrap_verify', phase: 'verify' }),
    r('f', '2026-11-27T16:35:00Z', { actor: 'human' }),
    r('g', '2026-11-27T00:10:00Z'), r('h', '2026-11-26T23:50:00Z'),
    r('i', '2026-11-26T23:40:00Z', { result: 'proof_failed' })
  ]
  const fold = items => items.map(x => (x.kind === 'row' ? x.d.id : `[${x.count}:${x.rows.map(y => y.id).join('')}|${x.passed}/${x.blocked}/${x.other}|${x.decision}]`))
  C.fold = fold(mod.foldRoutineDecisions(rows))
  C.foldEmpty = J(mod.foldRoutineDecisions(null))
  C.groups = mod.groupTrailRows(rows, NOWMS).map(g => ({ key: g.key, label: g.label, count: g.count, items: fold(g.items) }))
  C.routine = {
    gate: mod.isRoutineDecision(r('x', 'z')),
    human: mod.isRoutineDecision(r('x', 'z', { actor: 'human' })),
    supersedes: mod.isRoutineDecision(r('x', 'z', { supersedes_id: 'y' })),
    escape: mod.isRoutineDecision(r('x', 'z', { phase: 'escape' })),
    proofFailed: mod.isRoutineDecision(r('x', 'z', { result: 'proof_failed' })),
    blocked: mod.isRoutineDecision(r('x', 'z', { result: 'blocked' })),
    merge: mod.isRoutineDecision(r('x', 'z', { decision: 'merge_pr' }))
  }
  const ids = new Set(['flagged'])
  C.attention = {
    watchdog: mod.isAttentionDecision(r('x', 'z', { decision: 'stall_detected', phase: 'watchdog', result: 'stalled' })),
    superseded: mod.isAttentionDecision(r('x', 'z', { decision: 'x', result: 'reject', is_superseded: true })),
    audit: mod.isAttentionDecision(r('flagged', 'z'), ids),
    routineBlocked: mod.isAttentionDecision(r('x', 'z', { result: 'blocked' })),
    reject: mod.isAttentionDecision(r('x', 'z', { decision: 'verify', result: 'reject' })),
    pass: mod.isAttentionDecision(r('x', 'z', { decision: 'verify', result: 'pass' }))
  }
  C.tiers = [r('x', 'z', { phase: 'escape', decision: 'escape_detected' }), r('flagged', 'z', { decision: 'merge_pr' }), r('x', 'z', { decision: 'override_gate', actor: 'human' }), r('x', 'z'), r('x', 'z', { decision: 'merge_pr' })].map(d => mod.trailTier(d, ids))
  const chainRows = [
    r('c3', 'z', { decision: 'fix2', supersedes_id: 'c2' }), r('c2', 'z', { decision: 'fix1', supersedes_id: 'c1' }), r('c1', 'z', { decision: 'orig' }),
    r('m1', 'z', { decision: 'x', supersedes_id: 'gone' }),
    r('y1', 'z', { decision: 'x', supersedes_id: 'y2' }), r('y2', 'z', { decision: 'x', supersedes_id: 'y1' })
  ]
  C.chains = Object.fromEntries(Object.entries(mod.buildSupersessionChains(chainRows)).map(([k, v]) => [k, { priors: v.priors.map(p => p.id), missing: v.missing }]))
  const qd = { id: 'd_1', decision: 'pr_gate', why: 'Flaky E2E', result: 'pass', card_id: 't_9', tenant: 'ws-a', repo_slug: 'acme', evidence: { head_sha: 'abc1234', note: 'see holdout/x.md' } }
  C.query = ['flaky', 'ABC1234', 't_9', 'PR gate check', 'holdout', 'nothing', '', 'see holdout'].map(q => mod.decisionMatchesQuery(qd, q))
  C.strip = J(mod.stripHoldout({ a: 1, holdout_files: 2, b: ['ok', 'holdout/x'], c: { d: 'HOLDOUT', e: 'fine' } }))
  C.audit = {
    unavailable404: J(mod.summarizeTrailAudit(undefined, true)),
    notAudit: mod.summarizeTrailAudit({ decisions: [] }).available,
    clean: J(mod.summarizeTrailAudit({ ok: true, clean: true, findings: [], inspected: 1167 })),
    findings: J(mod.summarizeTrailAudit({ ok: true, findings: [
      { kind: 'gate_loosening', decision_id: 'd_1', message: 'Removed a hook' },
      { kind: 'weak_evidence', decision_id: 'd_2', message: 'Read holdout/s1.md' },
      { kind: 'bogus', decision_id: 'd_3', message: 'x' },
      { kind: 'missing_evidence', decision_id: 'holdout_d', message: 'y' },
      { kind: 'unclosed_escalation', decision_id: 'd_2', severity: 'high', message: 'Open' }
    ] }))
  }
  C.scan = J(mod.scanEvidence({ decision: 'publish_pr', evidence: { tests_passed: 4, head_sha: '8f2c1a9e77', pr_number: 42, branch: 'feat/x', base_branch: 'main', patch_id: '91e4f0a2c7', note: 'n' } }, 'omnim-ai/factonio')).top.map(p => p.key)
  C.scanMore = mod.scanEvidence({ decision: 'publish_pr', evidence: { tests_passed: 4, head_sha: '8f2c1a9e77', pr_number: 42, branch: 'feat/x', base_branch: 'main', patch_id: '91e4f0a2c7', note: 'n', holdout_x: 1 } }, 'omnim-ai/factonio').more
  C.draft = {
    empty: mod.validateDecisionDraft({}),
    spaces: mod.validateDecisionDraft({ decision: 'two words', why: 'w', phase: 'gate' }),
    badTenant: mod.validateDecisionDraft({ decision: 'x', why: 'w', phase: 'gate', tenant: '../etc' }),
    array: mod.validateDecisionDraft({ decision: 'x', why: 'w', phase: 'gate', evidence: '[1]' }),
    big: mod.validateDecisionDraft({ decision: 'x', why: 'w', phase: 'gate', evidence: JSON.stringify({ a: 'x'.repeat(17000) }) }),
    ok: mod.validateDecisionDraft({ decision: 'override_gate', why: 'w', phase: 'retro', evidence: '{"a":1}' })
  }
  C.body = mod.humanDecisionBody({ decision: ' override_gate ', why: ' because ', phase: 'gate', result: '', repo: 'acme', tenant: '', card_id: 't_1', evidence: '{"pr_number":1}', actor: 'robot' }, { id: 'd_old' })
  C.paths = [mod.trailPath({}), mod.trailPath({ tenant: 'a b', limit: 250, signal: 'key', q: 'x&y' }), mod.trailPath({ signal: 'all' })]

  // Markdown: plain elements only, https links only.
  C.href = ['https://github.com/o/r/pull/1', 'http://x.test', 'javascript:alert(1)', 'JAVASCRIPT:alert(1)', 'data:text/html,x', '/etc/passwd', 'file:///etc/passwd', 'https://user:pw@x.test', 'https://x.test/holdout/a', ' https://ok.test/a '].map(mod.safeMarkdownHref)
  C.inline = J(mod.parseInlineMarkdown('Use `code` and **bold** and *em* [ok](https://a.test/x) [bad](javascript:alert(1)) <img src=x onerror=alert(1)>'))
  C.blocks = J(mod.parseMarkdownBlocks([
    '# Cache misses', '', 'Para one', 'continues here.', '', '- item a', '- item b', '1. first', '2. second', '',
    '> quoted **text**', '', '```js', 'const x = "<script>"', '```', '', '| key | value |', '|---|:--:|', '| a | `b` |', '| c | d |', '', '---', '#### Deep', '<b>raw html</b>'
  ].join('\n')))
  C.report = {
    missing: J(mod.reportView({ ok: true, exists: false, content: null })),
    junk: J(mod.reportView(null)),
    view: J(mod.reportView({ ok: true, exists: true, content: '# R\nline holdout/a.md\nok', truncated: true, redacted: false, size_bytes: 300000, updated_at: '2026-11-27T10:00:00Z', filename: '/abs/path/investigation.md' }))
  }

  // Publish eligibility + request body.
  const SHA = 'a'.repeat(40)
  const LT = { repo: 'omnim-ai/factonio', branch: 'feat/x', base_branch: 'main', head_sha: SHA, source: 'metadata', pr_title: 'Add X', pr_body: 'Body' }
  const impl = card({ id: 't_impl', tenant: 'ws-pub', phase: 'implement', status: 'done', title: '[implement] X', local_target: LT, created_at: 10 })
  const vd = (o = {}) => card({ id: 't_vd', tenant: 'ws-pub', phase: 'verify-diff', status: 'done', verdict: 'pass', local_target: { ...LT }, created_at: 20, ...o })
  const vb = (o = {}) => card({ id: 't_vb', tenant: 'ws-pub', phase: 'verify-blackbox', status: 'done', verdict: 'pass', local_target: { ...LT }, created_at: 21, ...o })
  const el = (c, ws, panels) => { const e = mod.publishEligibility(c, ws, panels); return { eligible: e.eligible, reason: e.reason, label: e.verification?.label ?? null } }
  C.publish = {
    ok: el(impl, [impl, vd(), vb()]),
    noTarget: el({ ...impl, local_target: null }, []),
    hasPr: el({ ...impl, pr: PR42 }, [impl, vd(), vb()]),
    verifyCard: el(vd(), [impl, vd(), vb()]),
    shortSha: el({ ...impl, local_target: { ...LT, head_sha: 'abc1234' } }, []),
    noRepo: el({ ...impl, local_target: { ...LT, repo: null } }, []),
    onlyDiff: el(impl, [impl, vd()]),
    reject: el(impl, [impl, vd(), vb({ verdict: 'reject' })]),
    otherSha: el(impl, [impl, vd({ local_target: { ...LT, head_sha: 'b'.repeat(40) } }), vb()]),
    running: el(impl, [impl, vd(), vb({ status: 'running', verdict: null })]),
    panelPass: el(impl, [impl, vd({ panel_id: 'vp_1' }), vb({ panel_id: 'vp_1' })], [{ id: 'vp_1', consensus: 'pass', vendors: ['openai', 'google'], lanes: [] }]),
    panelPending: el(impl, [impl, vd({ panel_id: 'vp_1' })], [{ id: 'vp_1', consensus: 'pending', vendors: [], lanes: [] }])
  }
  C.publishBody = mod.publishRequestBody({ repo: 'omnim-ai/factonio', branch: 'feat/x', base_branch: 'main', head_sha: SHA }, { title: ' T ', body: '', draft: true }, { id: 't_impl', tenant: 'ws-pub' })

  // Harness audit drawer view model.
  C.harness = {
    stale: J(mod.formatHarnessAudit({ ok: true, repo_path: '/home/u/dev/x', severity: 'stale', mode: 'advisory', drifted: true, drift_classification: { classification: 'harness_gap' },
      uncovered_changed_files: ['desktop/plugin.js', 'holdout/s.md'], visual_baseline_changes: ['docs/images/dashboard.png'], stale_invariants: [{ name: 'skill-bloat', changed_files: ['skills/a/SKILL.md'] }],
      gate_loosening: { checked: true, loosened: true, findings: [{ message: 'Required hook desktop-syntax removed' }] }, manifest_errors: [], recommendations: ['Run hermes factonio harness sync', 'Run hermes factonio harness sync'] })),
    clean: J(mod.formatHarnessAudit({ ok: true, severity: 'clean', mode: 'enforced', drifted: false, drift_classification: { classification: 'clean' }, head_commit: '9c4e1a2f60', gate_loosening: { checked: true, loosened: false, findings: [] } })),
    missing: J(mod.formatHarnessAudit({ ok: false, present: false, severity: 'missing', manifest_errors: ['.factonio/harness.json missing'] })),
    junk: J(mod.formatHarnessAudit(null))
  }

  // Design approval state.
  const des = card({ id: 't_des', tenant: 'ws-d', phase: 'design', status: 'done', title: '[design] Previews', created_at: 50 })
  const appr = (decision, o = {}) => ({ id: `d_${decision}`, ts: '2026-11-27T10:00:00Z', tenant: 'ws-d', phase: 'design', card_id: 't_des', decision, actor: 'human', is_superseded: false, ...o })
  C.design = {
    awaiting: mod.designApprovalState(des, [des], []),
    running: mod.designApprovalState({ ...des, status: 'running' }, [des], []),
    approved: mod.designApprovalState(des, [des], [appr('approve_design')]),
    revising: mod.designApprovalState(des, [des], [appr('approve_design', { ts: '2026-11-27T09:00:00Z' }), appr('request_design_revision')]),
    supersededRevision: mod.designApprovalState(des, [des], [appr('request_design_revision', { is_superseded: true })]),
    implementExists: mod.designApprovalState(des, [des, card({ id: 't_i', phase: 'implement', status: 'running', created_at: 60 })], []),
    candidate: mod.designApprovalState({ ...des, arena_role: 'candidate' }, [des], []),
    notDesign: mod.designApprovalState({ ...des, phase: 'define' }, [des], [])
  }

  // --- rendered: sidebar ------------------------------------------------------
  const INV = 'omnim-ai-factonio-cache-miss-spike'
  const PUBT = 'omnim-ai-factonio-local-publish'
  const DEST = 'omnim-ai-factonio-design-review'
  const pubImpl = card({ id: 't_pimpl', tenant: PUBT, phase: 'implement', status: 'done', title: '[implement] Control-plane desktop UI', local_target: { ...LT, pr_title: 'feat(desktop): control-plane actions', pr_body: 'Verified locally.' }, created_at: 100 })
  const pubVd = card({ id: 't_pvd', tenant: PUBT, phase: 'verify-diff', status: 'done', verdict: 'pass', local_target: { ...LT }, panel_id: 'vp_9a1', panel_vendor: 'openai', created_at: 110 })
  const pubVb = card({ id: 't_pvb', tenant: PUBT, phase: 'verify-blackbox', status: 'done', verdict: 'pass', local_target: { ...LT }, panel_id: 'vp_9a1', panel_vendor: 'google', created_at: 111 })
  const desCard = card({ id: 't_dsg', tenant: DEST, phase: 'design', status: 'done', title: '[design] Decision History redesign', created_at: 200 })
  const desDef = card({ id: 't_dsd', tenant: DEST, phase: 'define', status: 'done', title: '[define] Spec', created_at: 150 })
  const invCard = card({ id: 't_inv', tenant: INV, phase: 'define', status: 'done', title: '[define] [investigate] Why do cache misses spike?', created_at: 300, workstream_kind: 'investigate' })
  const arenaCards = [
    card({ id: 't_ac1', tenant: DEST, phase: 'design', status: 'done', title: '[design] Candidate', created_at: 160, arena_id: 'ar_7f2c9a10', arena_role: 'candidate', arena_slot: 'c1-anthropic', arena_vendor: 'anthropic', arena_target: 'design' })
  ]
  const ARENAS = [{ arena_id: 'ar_7f2c9a10', tenant: DEST, repo: 'omnim-ai/factonio', target: 'design', title: 'Decision History', status: 'judging', created_at: 100,
    candidates: [{ slot: 'c1-anthropic', vendor: 'anthropic', model: 'm', task_id: 't_ac1', status: 'done', artifact_ready: true }],
    judge: { task_id: 't_j', status: 'running', vendor: 'google', model: 'g', cross_vendor: true }, ready_for_synthesis: false, synthesized: false, canonical_artifact_exists: false, advisories: [] }]
  const CP_CARDS = { cards: [pubImpl, pubVd, pubVb, desDef, desCard, ...arenaCards, invCard], counts: {}, arenas: ARENAS,
    workstreams: { [INV]: { kind: 'investigate', read_only: true, report_path: '/home/u/.hermes/plugin-data/factonio/runs/x/investigation.md', report_exists: true } } }
  const REPORT = { ok: true, exists: true, tenant: INV, filename: 'investigation.md', updated_at: '2026-11-27T10:00:00Z', size_bytes: 262144 + 4000, truncated: true, redacted: true,
    content: '# Cache-miss spike\n\n## Findings\n- Misses follow deploys\n- See [run](https://github.com/omnim-ai/factonio/actions/runs/1) and [bad](javascript:alert(1))\n\n| metric | value |\n|---|---|\n| p95 | `41ms` |\n\n```\n<script>alert(1)</script>\n```\n\n> Root cause: cold cache\n\nSee holdout/s1.md for proof\n<img src=x onerror=alert(1)>' }
  const mountCp = (rest) => {
    const regs = []
    const rests = []
    const ctx = { rest: async (p, opts) => { rests.push({ path: p, opts: opts ? J(opts) : null }); return rest(p, opts) }, onDispose: () => {}, registerMany: items => regs.push(...items), os: makeOs() }
    plugin.register(ctx)
    const pane = regs.find(x => x.id === 'pane')
    const hooks = makeHooks()
    const focusLog = []
    const client = { invalidated: [], invalidateQueries(o) { this.invalidated.push(o.queryKey.join('/')); return Promise.resolve() } }
    const render = () => {
      globalThis.__fcHooks = hooks
      globalThis.__fcQueryClient = client
      cardsQueries.length = 0
      const tree = expand(pane.render())
      hooks.gc()
      attachRefs(tree, focusLog)
      hooks.flush()
      return tree
    }
    return { ctx, rests, render, focusLog, client }
  }
  const secOf = (tree, t) => find(tree, n => n.type === 'section' && n.props['data-tenant'] === t)
  const artOf = (tree, id) => find(tree, n => n.type === 'article' && n.props['data-id'] === id)
  const modalOf = tree => find(tree, hasCls('fc-m'))
  const readModal = tree => {
    const md = modalOf(tree)
    if (!md) return null
    return {
      id: md.node.props['data-modal'], role: md.node.props.role, modal: md.node.props['aria-modal'], size: md.node.props['data-size'] ?? null, busy: md.node.props['aria-busy'] ?? null,
      title: norm(find(md.node, n => n.type === 'h2').node),
      state: (() => { const s = find(md.node, hasCls('fc-m-report')); return s ? s.node.props['data-state'] : null })(),
      facts: (() => { const f = find(md.node, hasCls('fc-m-facts')); return f ? findAll(f.node, n => n.type === 'dd').map(h => norm(h.node)) : null })(),
      banners: findAll(md.node, hasCls('fc-banner')).map(h => ({ tone: h.node.props['data-tone'], role: h.node.props.role, text: norm(h.node) })),
      title_input: (() => { const i = find(md.node, n => n.props.id === 'fc-pp-title'); return i ? i.node.props.value : null })(),
      body_input: (() => { const i = find(md.node, n => n.props.id === 'fc-pp-body'); return i ? i.node.props.value : null })(),
      draft: (() => { const i = find(md.node, n => n.props['data-field'] === 'draft' && n.type === 'input'); return i ? i.node.props.checked : null })(),
      shield: (() => { const s = find(md.node, hasCls('fc-m-shield')); return s ? norm(s.node) : null })(),
      go: (() => { const g = find(md.node, n => n.type === 'button' && hasCls('fc-btn--primary')(n)); return g ? { text: norm(g.node), disabled: !!g.node.props.disabled, action: g.node.props['data-action'] } : null })(),
      md: (() => {
        const a = find(md.node, hasCls('fc-md'))
        if (!a) return null
        const tags = []
        walk(a.node, n => tags.push(n.type))
        return {
          tags: [...new Set(tags)].sort(),
          links: findAll(a.node, n => n.type === 'a').map(h => ({ href: h.node.props.href, rel: h.node.props.rel, text: norm(h.node) })),
          text: norm(a.node),
          dangerous: findAll(a.node, n => Object.keys(n.props).some(k => /dangerously|^on(?!Click$)/i.test(k))).length,
          code: findAll(a.node, n => n.type === 'pre').map(h => text(h.node))
        }
      })(),
      text: norm(md.node)
    }
  }
  const S = C.sidebar = {}
  cardsState = { data: CP_CARDS, isLoading: false, isError: false }
  let pubBodies = []
  let pubImplRest = async () => ({ ok: true, published: true, pr_number: 47, url: 'https://github.com/omnim-ai/factonio/pull/47', head_sha: SHA })
  const restFor = async (p, opts) => {
    if (p === `/workstreams/${INV}/report`) return J(REPORT)
    if (p === '/publish-pr') { pubBodies.push(opts.body); return pubImplRest(p, opts) }
    if (p === '/panels/evaluate') throw new Error('404: {"detail":"panel not found"}')
    if (p === '/arenas/evaluate') return { ok: true, arena_id: 'ar_7f2c9a10', status: 'judging' }
    if (p === '/trail/decisions') return { ok: true, decision: { id: 'd_appr0001', decision: opts.body.decision, actor: 'human' } }
    return { cards: [] }
  }
  trailState = { data: { decisions: [consensusRow(PUBT, 'vp_9a1', 'pass')], summary: {} }, isLoading: false, isError: false }
  let m = mountCp(restFor)
  let tree = m.render()
  // Expand every collapsed workstream and done group so finished cards render.
  for (const w of findAll(tree, n => n.props['data-action'] === 'toggle-ws' && n.props['aria-expanded'] === 'false')) {
    const ws = w.anc.find(a => a.type === 'section')
    fire(find(secOf(tree, ws.props['data-tenant']).node, byAttr('data-action', 'toggle-ws'))); tree = m.render()
  }
  for (const d of findAll(tree, n => n.props.className === 'fc-sb-done-toggle' && n.props['aria-expanded'] === 'false')) {
    const ws = d.anc.find(a => a.type === 'section')
    fire(find(secOf(tree, ws.props['data-tenant']).node, n => n.props.className === 'fc-sb-done-toggle')); tree = m.render()
  }
  const pubBtn = t => find(artOf(t, 't_pimpl').node, byAttr('data-action', 'publish-pr'))
  S.publishButtons = findAll(tree, byAttr('data-action', 'publish-pr')).map(h => h.anc.find(a => a.type === 'article').props['data-id'])
  S.mainCards = findAll(secOf(tree, PUBT).node, n => n.type === 'article' && !n.props.className?.includes?.('done')).map(h => h.node.props['data-id'])
  S.publishBtn = { text: norm(pubBtn(tree).node), title: pubBtn(tree).node.props.title, popup: pubBtn(tree).node.props['aria-haspopup'] }
  fire(find(artOf(tree, 't_pimpl').node, byAttr('data-action', 'toggle-card'))); tree = m.render()
  S.localRows = (() => { const dl = find(artOf(tree, 't_pimpl').node, n => n.type === 'dl'); return Object.fromEntries(findAll(dl.node, n => n.type === 'dd' && ['branch', 'head', 'verify', 'pr'].includes(n.props['data-field'])).map(h => [h.node.props['data-field'], norm(h.node)])) })()
  m.focusLog.length = 0
  const pf = fire(pubBtn(tree)); tree = m.render()
  S.publishOpen = { stopped: pf.stopped, modal: readModal(tree), focus: [...m.focusLog] }
  // Edit title, then a 409 drift refusal keeps the edits.
  find(modalOf(tree).node, n => n.props.id === 'fc-pp-title').node.props.onChange({ target: { value: 'feat(desktop): control plane' } })
  find(modalOf(tree).node, n => n.props.id === 'fc-pp-body').node.props.onChange({ target: { value: 'Verified locally.\n\nEdited body.' } })
  find(modalOf(tree).node, n => n.props['data-field'] === 'draft' && n.type === 'input').node.props.onChange({ target: { checked: true } }); tree = m.render()
  pubImplRest = async () => { throw new Error(`409: ${JSON.stringify({ detail: { ok: false, refused: true, error: `Not locally verified at ${SHA}: head drifted` } })}`) }
  await Promise.all(fire(find(modalOf(tree).node, byAttr('data-action', 'publish-go'))).results); tree = m.render()
  S.publish409 = readModal(tree)
  // 409 drift carrying the observed head: verified vs observed SHA (12 chars) plus the server detail.
  const NEW_SHA = 'b'.repeat(40)
  const drift409 = error => async () => { throw new Error(`409: ${JSON.stringify({ detail: { ok: false, refused: true, pushed: false, head_sha: NEW_SHA, expected_head_sha: SHA, verification: { verified: true }, error } })}`) }
  const readDrift = t => {
    const b = find(modalOf(t).node, hasCls('fc-banner')).node
    const f = k => { const h = find(b, byAttr('data-field', k)); return h ? { text: norm(h.node), title: h.node.props.title ?? null } : null }
    return { modal: readModal(t), observed: f('observed-head'), verified: f('verified-head'), detail: f('server-detail') }
  }
  pubImplRest = drift409(`HEAD drifted from ${SHA} to ${NEW_SHA}; re-verify before publishing.`)
  await Promise.all(fire(find(modalOf(tree).node, byAttr('data-action', 'publish-go'))).results); tree = m.render()
  S.publishDrift = readDrift(tree)
  // A server message that names the holdout barrier is scrubbed; the SHAs still show.
  pubImplRest = drift409('HEAD drifted; see holdout/scenario-1.md')
  await Promise.all(fire(find(modalOf(tree).node, byAttr('data-action', 'publish-go'))).results); tree = m.render()
  S.publishDriftScrubbed = readDrift(tree)
  // Retry succeeds -> live PR link; caches invalidated; exact SHA pinned.
  let releasePub
  pubImplRest = () => new Promise(res => { releasePub = () => res({ ok: true, published: true, pr_number: 47, url: 'https://github.com/omnim-ai/factonio/pull/47', head_sha: SHA }) })
  const pubPending = Promise.all(fire(find(modalOf(tree).node, byAttr('data-action', 'publish-go'))).results); tree = m.render()
  S.publishBusy = readModal(tree)
  modalOf(tree).node.props.onKeyDown({ key: 'Escape', preventDefault() {}, stopPropagation() {} }); tree = m.render()
  S.publishEscBusy = !!modalOf(tree)
  releasePub(); await pubPending; tree = m.render()
  S.publishDone = readModal(tree)
  S.publishBodies = J(pubBodies)
  S.publishInvalidated = [...m.client.invalidated]
  windowOpens.length = 0
  fire(find(modalOf(tree).node, byAttr('data-action', 'open-pr'))); await tick()
  S.publishOpened = [...m.ctx.os.opened]
  fire(find(modalOf(tree).node, byAttr('data-action', 'close-modal'))); tree = m.render()
  S.publishClosed = !modalOf(tree)

  // report ready -> inline Markdown viewer (safe elements only).
  const repBtn = find(secOf(tree, INV).node, n => n.type === 'button' && n.props['data-action'] === 'open-report')
  S.reportBtn = repBtn ? { text: norm(repBtn.node), cls: repBtn.node.props.className, popup: repBtn.node.props['aria-haspopup'], title: repBtn.node.props.title } : null
  m.rests.length = 0
  fire(repBtn); tree = m.render()
  otherQueries.length = 0
  S.reportQueryLoading = readModal(tree)
  // The report query goes through the stubbed useQuery (other bucket): resolve it via the queryFn.
  const reportQ = []
  const prevUseQuery = globalThis.__fcUseQuery
  globalThis.__fcUseQuery = opts => {
    if (opts.queryKey[1] === 'report') { reportQ.push({ key: opts.queryKey, interval: opts.refetchInterval ?? null, fn: opts.queryFn }); return globalThis.__fcReportState || { data: undefined, isLoading: true, isError: false } }
    return prevUseQuery(opts)
  }
  tree = m.render()
  const rq = reportQ[reportQ.length - 1]
  const reportData = await rq.fn()
  globalThis.__fcReportState = { data: reportData, isLoading: false, isError: false }
  tree = m.render()
  S.report = { query: { key: rq.key, interval: rq.interval }, rests: m.rests.map(x => x.path), modal: readModal(tree) }
  const mdLink = find(find(modalOf(tree).node, hasCls('fc-md')).node, n => n.type === 'a')
  fire(mdLink); await tick()
  S.reportLinkOpened = [...m.ctx.os.opened].slice(-1)
  S.reportBarrier = allStrings(modalOf(tree).node).filter(x => /holdout/i.test(x))
  fire(find(modalOf(tree).node, byAttr('data-action', 'close-report'))); tree = m.render()
  globalThis.__fcReportState = { data: { ok: true, exists: false, content: null, filename: 'investigation.md' }, isLoading: false, isError: false }
  fire(find(secOf(tree, INV).node, byAttr('data-action', 'open-report'))); tree = m.render()
  S.reportMissing = readModal(tree)
  fire(find(modalOf(tree).node, byAttr('data-action', 'close-report'))); tree = m.render()
  globalThis.__fcReportState = { data: undefined, isLoading: false, isError: true, error: new Error('403: {"detail":"not a regular file"}'), refetch: () => {} }
  fire(find(secOf(tree, INV).node, byAttr('data-action', 'open-report'))); tree = m.render()
  S.reportError = readModal(tree)
  fire(find(modalOf(tree).node, byAttr('data-action', 'close-report'))); tree = m.render()
  delete globalThis.__fcReportState
  // Drawer Report row also opens it.
  fire(find(artOf(tree, 't_inv').node, byAttr('data-action', 'toggle-card'))); tree = m.render()
  const drawerRep = find(artOf(tree, 't_inv').node, n => n.type === 'button' && n.props['data-action'] === 'open-report')
  S.drawerReport = drawerRep ? norm(drawerRep.node) : null
  fire(drawerRep); tree = m.render()
  S.drawerReportOpens = readModal(tree)?.id ?? null
  fire(find(modalOf(tree).node, byAttr('data-action', 'close-report'))); tree = m.render()
  globalThis.__fcUseQuery = prevUseQuery

  // Design approval strip: Approve -> confirm group -> POST /trail/decisions (no actor).
  const strip = t => find(artOf(t, 't_dsg').node, hasCls('fc-sb-appr'))
  S.designStrip = { state: strip(tree).node.props['data-state'], text: norm(strip(tree).node) }
  S.designOnlyDesign = findAll(tree, hasCls('fc-sb-appr')).map(h => h.anc.find(a => a.type === 'article').props['data-id'])
  fire(find(strip(tree).node, byAttr('data-action', 'design-revise'))); tree = m.render()
  S.reviseOpen = { text: norm(strip(tree).node), confirmDisabled: !!find(strip(tree).node, byAttr('data-action', 'design-confirm')).node.props.disabled }
  fire(find(strip(tree).node, byAttr('data-action', 'design-cancel'))); tree = m.render()
  fire(find(strip(tree).node, byAttr('data-action', 'design-approve'))); tree = m.render()
  S.approveOpen = norm(strip(tree).node)
  find(strip(tree).node, byAttr('data-field', 'design-note')).node.props.onChange({ target: { value: 'Ship it.' } }); tree = m.render()
  m.rests.length = 0
  await Promise.all(fire(find(strip(tree).node, byAttr('data-action', 'design-confirm'))).results); tree = m.render()
  S.approved = { state: strip(tree).node.props['data-state'], text: norm(strip(tree).node), rests: m.rests.filter(x => x.path === '/trail/decisions').map(x => x.opts.body), invalidated: m.client.invalidated.slice(-1) }

  // Re-evaluate: arena ok -> "Updated just now"; panel 404 -> inline error with Retry.
  const arenaStrip = t => find(secOf(t, DEST).node, hasCls('fc-sb-arena'))
  const reBtn = h => find(h.node, byAttr('data-action', 're-evaluate'))
  S.arenaReeval = { label: reBtn(arenaStrip(tree)).node.props['aria-label'], text: norm(reBtn(arenaStrip(tree)).node), inTop: !!find(find(arenaStrip(tree).node, hasCls('fc-sb-panel-top')).node, byAttr('data-action', 're-evaluate')) }
  m.rests.length = 0
  const ra = fire(reBtn(arenaStrip(tree)))
  tree = m.render()
  S.arenaBusy = { text: norm(reBtn(arenaStrip(tree)).node), disabled: !!reBtn(arenaStrip(tree)).node.props.disabled, stopped: ra.stopped }
  await Promise.all(ra.results); tree = m.render()
  S.arenaDone = { msg: norm(find(arenaStrip(tree).node, hasCls('fc-sb-strip-msg')).node), rests: m.rests.map(x => ({ path: x.path, body: x.opts?.body })) }
  const panelStrip = t => find(secOf(t, PUBT).node, n => hasCls('fc-sb-panel')(n) && !hasCls('fc-sb-arena')(n))
  m.rests.length = 0
  await Promise.all(fire(reBtn(panelStrip(tree))).results); tree = m.render()
  const pmsg = find(panelStrip(tree).node, hasCls('fc-sb-strip-msg'))
  S.panelError = { text: norm(pmsg.node), role: pmsg.node.props.role, retry: !!find(pmsg.node, byAttr('data-action', 're-evaluate-retry')), rests: m.rests.map(x => ({ path: x.path, body: x.opts?.body })) }
  S.queries = { other: [...new Set(otherQueries)], polling: [...new Set(cardsQueries.filter(x => x.interval).map(x => x.key.join('/')))] }
  S.barrier = allStrings(tree).filter(x => /holdout/i.test(x))

  // Older host without design approval / local_target support: nothing extra renders.
  cardsState = { data: { cards: [card({ id: 't_old', tenant: 'old-ws', phase: 'implement', status: 'running', title: '[implement] old' })], counts: {} }, isLoading: false, isError: false }
  m = mountCp(async () => ({ cards: [] })); tree = m.render()
  S.oldHost = { publish: findAll(tree, byAttr('data-action', 'publish-pr')).length, appr: findAll(tree, hasCls('fc-sb-appr')).length }

  // --- rendered: repo harness audit drawer -----------------------------------
  const A = C.repoAudit = {}
  const client = { invalidated: [], invalidateQueries(o) { this.invalidated.push(o.queryKey.join('/')); return Promise.resolve() } }
  trailState = { data: { decisions: [], summary: {} }, isLoading: false, isError: false }
  const rm = mountRepos({ data: { repos: REPOS, defaults: REPO_DEFAULTS }, client })
  let auditRelease
  const STALE = { ok: true, repo: 'tozes/factonio', repo_path: '/home/tozes/dev/factonio', severity: 'stale', mode: 'advisory', drifted: true, drift_classification: { classification: 'harness_gap' },
    uncovered_changed_files: ['desktop/plugin.js'], stale_invariants: ['skill-bloat'], gate_loosening: { checked: true, loosened: false, findings: [] }, manifest_errors: [], recommendations: ['Run `hermes factonio harness sync`.'], head_commit: '9c4e1a2f60' }
  rm.setRest(async p => {
    if (p === '/repos/tozes-factonio/harness/audit') return new Promise(res => { auditRelease = () => res(J(STALE)) })
    if (p === '/repos/acme-cli-tools/harness/audit') throw new Error('409: {"detail":"Local checkout missing"}')
    return { ok: true }
  })
  let rt = rm.render()
  const auditHit = (t, slug) => find(reposSec(t).node, n => n.type === 'button' && n.props['aria-label'] === `Audit harness for ${REPOS.find(x => x.slug === slug).repo}`)
  const rowOf = (t, slug) => find(reposSec(t).node, n => n.type === 'tr' && n.props['data-slug'] === slug && !hasCls('fc-au-row')(n))
  const drawerOf = (t, slug) => find(reposSec(t).node, n => n.type === 'tr' && hasCls('fc-au-row')(n) && n.props['data-slug'] === slug)
  const readAu = (t, slug) => {
    const d = drawerOf(t, slug)
    if (!d) return null
    const td = find(d.node, n => n.type === 'td').node
    const box = find(td, hasCls('fc-au')).node
    return {
      colSpan: td.props.colSpan, status: box.props['data-status'], id: box.props.id, label: box.props['aria-label'],
      chips: findAll(box, n => n.props['data-field']).map(h => [h.node.props['data-field'], norm(h.node), h.node.props['data-tone'] ?? null]),
      sections: Object.fromEntries(findAll(box, n => n.type === 'section').map(h => [h.node.props['data-section'], { title: norm(find(h.node, n => n.type === 'h4').node), items: findAll(h.node, n => n.type === 'li').map(x => norm(x.node)) }])),
      banner: (() => { const b = find(box, hasCls('fc-banner')); return b ? norm(b.node) : null })(),
      text: norm(box)
    }
  }
  A.headers = findAll(reposSec(rt).node, n => n.type === 'th').map(h => norm(h.node)).slice(-1)
  A.actions = findAll(rowOf(rt, 'tozes-factonio').node, n => n.type === 'td' && hasCls('fc-act')(n)).map(h => findAll(h.node, n => n.type === 'button').map(b => b.node.props['data-action']))
  A.note = norm(find(rt, byAttr('data-field', 'audit-note')).node)
  fire(auditHit(rt, 'tozes-factonio')); rt = rm.render()
  A.busy = { drawer: readAu(rt, 'tozes-factonio'), btn: { disabled: !!auditHit(rt, 'tozes-factonio').node.props.disabled, expanded: auditHit(rt, 'tozes-factonio').node.props['aria-expanded'], controls: auditHit(rt, 'tozes-factonio').node.props['aria-controls'] }, rowAudit: rowOf(rt, 'tozes-factonio').node.props['data-audit'] }
  auditRelease(); await tick(); await tick(); rt = rm.render()
  A.stale = readAu(rt, 'tozes-factonio')
  A.rests = rm.rests.map(x => ({ path: x.path, method: x.opts?.method, body: x.opts?.body }))
  A.invalidated = [...client.invalidated]
  A.barrierPath = allStrings(drawerOf(rt, 'tozes-factonio').node).filter(x => x.includes('/home/tozes'))
  fire(auditHit(rt, 'acme-cli-tools')); await tick(); await tick(); rt = rm.render()
  A.missing = readAu(rt, 'acme-cli-tools')
  A.rowOrder = findAll(reposSec(rt).node, n => n.type === 'tr' && n.props['data-slug']).map(h => `${h.node.props['data-slug']}${hasCls('fc-au-row')(h.node) ? ':audit' : ''}`)
  fire(find(drawerOf(rt, 'tozes-factonio').node, byAttr('data-action', 'close-audit'))); rt = rm.render()
  A.closed = { drawer: !!drawerOf(rt, 'tozes-factonio'), expanded: auditHit(rt, 'tozes-factonio').node.props['aria-expanded'] }

  cardsState = { data: undefined, isLoading: true, isError: false }
  trailState = { data: undefined, isLoading: true, isError: false }
  globalThis.__fcReposState = undefined
  globalThis.__fcQueryClient = undefined
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
await runReposScenarios(report)
await runTrailScenarios(report)
await runTrailLineageScenarios(report)

// Tri-state verification verdicts: additive `inconclusive` fields from /metrics and /cards.
{
  const tri = report.triState = {}
  const TRI = 'acme-api-tri-state'
  const TRI_METRICS = {
    range: '30d', tenant: null,
    totals: { cards: 6, done: 5, running: 0, blocked: 1, triage: 0, workstreams: 1 },
    phases: { define: { count: 0, done: 0, median_seconds: null, p90_seconds: null } },
    verification: {
      diff: { pass: 1, reject: 0, inconclusive: 2, pass_rate: 0.333 },
      blackbox: { pass: 1, reject: 1, inconclusive: 1, pass_rate: 0.333 }
    },
    rework: { avg_runs_per_implement_card: null, cards_with_rework: 0 },
    escapes: { count: 0, verified_total: 1, escape_rate: 0 }
  }
  globalThis.__fcMetricsState = { data: TRI_METRICS, isLoading: false, isError: false }
  try { tri.dashboard = snapshot(page.render).text } catch (e) { tri.dashboardError = String(e && e.stack || e) }
  globalThis.__fcMetricsState = undefined
  const TRI_CARDS = [
    card({ id: 't_tri_imp', tenant: TRI, phase: 'implement', status: 'done', title: '[implement] i', created_at: 100 }),
    card({ id: 't_tri_d', tenant: TRI, phase: 'verify-diff', status: 'done', verdict: 'pass', title: '[verify:diff] d', created_at: 200 }),
    card({ id: 't_tri_b', tenant: TRI, phase: 'verify-blackbox', status: 'done', verdict: 'inconclusive', title: '[verify:blackbox] b', summary: 'No browser available', created_at: 200 })
  ]
  const TRI_COUNTS = { running: 0, blocked: 0, triage: 0, ready: 0, done: 3, todo: 0, review: 0, rejected: 0, inconclusive: 1, attention: 1, total: 3 }
  cardsState = { data: { cards: TRI_CARDS, counts: TRI_COUNTS }, isLoading: false, isError: false }
  try {
    const m = mountPane({ focused: null })
    let tree = m.render()
    for (const sec of sectionsOf(tree)) {
      if (sec.expanded === 'false') {
        fire(find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === sec.tenant).node, byAttr('data-action', 'toggle-ws')))
        tree = m.render()
      }
    }
    for (const sec of sectionsOf(tree)) {
      const toggle = find(find(tree, n => n.type === 'section' && n.props['data-tenant'] === sec.tenant).node, n => n.props.className === 'fc-sb-done-toggle')
      if (toggle && toggle.node.props['aria-expanded'] === 'false') { fire(toggle); tree = m.render() }
    }
    tri.sections = sectionsOf(tree).map(x => ({ tenant: x.tenant, state: x.state, cards: x.cards }))
    tri.verdictTags = findAll(tree, n => n.props && n.props['data-verdict']).map(h => ({ verdict: h.node.props['data-verdict'], tone: h.node.props['data-tone'] ?? null, text: text(h.node).trim() }))
  } catch (e) { tri.sidebarError = String(e && e.stack || e) }
  cardsState = { data: undefined, isLoading: true, isError: false }
}

await runPolishScenarios(report)
await runWave5Scenarios(report)
await runControlPlaneScenarios(report)

process.stdout.write(JSON.stringify(report))
"""



def _lineage_fixture(tmp: Path) -> dict:
    """Real trail.db payloads for lineage targets outside the loaded window or server scope."""
    from factonio import trail

    with isolated_hermes_home():
        win_db = tmp / "window-trail.db"
        def log(decision: str, **kw):
            return trail.log_decision(decision, db_path=win_db, actor="orchestrator", **kw)

        original = log("pr_gate_wait", tenant="lineage-window", repo_slug="omnim-ai-factonio", phase="verify",
                       why="Held the gate on a flaky check.", evidence={"pr_number": 16}, result="inconclusive")
        for i in range(200):
            log("heartbeat_ok", tenant="lineage-window", repo_slug="omnim-ai-factonio", phase="watchdog", evidence={"n": i}, result="recorded")
        correction = log("verify_carryover", tenant="lineage-window", repo_slug="omnim-ai-factonio", phase="verify",
                         why="Re-ran the flaky check green.", result="pass", supersedes_id=original["id"])
        page = trail.trail_payload(limit=200, db_path=win_db)
        window = {
            "original": original["id"],
            "correction": correction["id"],
            "api": {"total": page["summary"]["total"], "loaded": len(page["decisions"]),
                    "target_loaded": any(d["id"] == original["id"] for d in page["decisions"])},
            "responses": {
                "/trail?limit=200": page,
                "/trail?limit=1000": trail.trail_payload(limit=1000, db_path=win_db),
                f"/trail/{original['id']}": {"decision": trail.get_decision(original["id"], db_path=win_db)},
            },
        }

        scope_db = tmp / "scope-trail.db"
        other = trail.log_decision("pr_gate_wait", db_path=scope_db, tenant="lineage-a", repo_slug="acme-api", phase="gate",
                                   actor="orchestrator", result="blocked")
        trail.log_decision("spawn_implement", db_path=scope_db, tenant="lineage-a", repo_slug="acme-api", phase="implement",
                           actor="worker", result="spawned")
        fix = trail.log_decision("pr_gate", db_path=scope_db, tenant="lineage-b", repo_slug="acme-api", phase="gate",
                                 actor="human", why="Override recorded in the follow-up workstream.", result="ready",
                                 supersedes_id=other["id"])
        scoped = trail.trail_payload(tenant="lineage-b", limit=200, db_path=scope_db)
        scope = {
            "tenant": "lineage-b",
            "original": other["id"],
            "correction": fix["id"],
            "api": {"scoped_ids": [d["id"] for d in scoped["decisions"]], "filters": scoped["filters"]},
            "responses": {
                "/trail?tenant=lineage-b&limit=200": scoped,
                "/trail?limit=200": trail.trail_payload(limit=200, db_path=scope_db),
                f"/trail/{other['id']}": {"decision": trail.get_decision(other["id"], db_path=scope_db)},
            },
        }
    return {"window": window, "scope": scope}


@unittest.skipUnless(shutil.which("node"), "node is required for desktop plugin checks")
class TestDesktopPlugin(unittest.TestCase):
    report: dict

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory(prefix="factonio-lineage-") as tmp:
            fixture = Path(tmp) / "lineage.json"
            fixture.write_text(json.dumps(_lineage_fixture(Path(tmp))))
            # Piped via stdin: the script outgrew Linux's 128 KiB per-argument limit (MAX_ARG_STRLEN).
            proc = subprocess.run(
                ["node", "--input-type=module"],
                input=SMOKE_SCRIPT,
                capture_output=True,
                text=True,
                timeout=60,
                env={"PATH": os.environ.get("PATH", ""), "FC_PLUGIN_URL": PLUGIN_JS.as_uri(), "FC_LINEAGE_FIXTURE": str(fixture)},
            )
        if proc.returncode != 0:
            raise AssertionError(f"node smoke failed ({proc.returncode}):\n{proc.stderr}\n{proc.stdout}")
        cls.report = json.loads(proc.stdout)

    def test_tri_state_inconclusive_fields_render_cleanly(self):
        tri = self.report["triState"]
        self.assertNotIn("dashboardError", tri)
        self.assertNotIn("sidebarError", tri)
        # /metrics additive `inconclusive` counts do not break the dashboard cards.
        self.assertIn("Diff Check Pass", tri["dashboard"])
        self.assertIn("33%", tri["dashboard"])
        self.assertIn("1 pass · 1 reject", tri["dashboard"])
        # /cards `verdict: "inconclusive"` renders as a neutral (never pass-toned) tag.
        self.assertEqual(len(tri["sections"]), 1)
        self.assertIn("t_tri_b", tri["sections"][0]["cards"])
        tags = {t["verdict"]: t for t in tri["verdictTags"]}
        self.assertEqual(tags["inconclusive"]["text"], "inconclusive")
        self.assertNotEqual(tags["inconclusive"]["tone"], "pass")
        self.assertEqual(tags["pass"]["tone"], "pass")

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

    # --- right-sidebar tab (beside Files): registration & reveal ----------
    def test_pane_registers_as_hideable_right_sidebar_tab_beside_files(self):
        reg = self.report["sidebar"]["paneRegistration"]
        self.assertEqual(reg["title"], "Factonio")
        self.assertEqual(reg["area"], "panes")
        self.assertEqual(reg["renderType"], "function")
        self.assertEqual(
            reg["data"],
            {
                "placement": "right",
                "width": "280px",
                "collapsible": True,
                "hideOnly": True,
                "dock": {"pane": "files", "pos": "center", "enforce": True},
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


    # --- Decision History: /factonio TrailSection --------------------------------
    def _rows(self, t):
        return [r["id"] for r in t["rows"]]

    def _row(self, t, rid):
        return next(r for r in t["rows"] if r["id"] == rid)

    def test_trail_dashboard_query_and_placement(self):
        d = self.report["trail"]["dashboard"]
        self.assertEqual(d["query"], {"key": ["factonio", "trail", 200], "interval": 30000})
        # Zero extra polling: the signal page and the audit summary refresh only on key change / invalidation.
        self.assertEqual(d["queries"]["signal"], {"key": ["factonio", "trail", 200, "signal", "key"], "interval": None})
        self.assertEqual(d["queries"]["audit"], {"key": ["factonio", "trail", "audit", "all"], "interval": None})
        self.assertEqual(d["queries"]["polling"], ["factonio/trail/200"])
        self.assertEqual(d["restPaths"], ["/trail?limit=200", "/trail?limit=200&signal=key", "/trail/audit"])
        self.assertEqual(d["sectionOrder"], ["Trends", "Phase Durations", "Decision History", "Phase Model Configuration", "Registered Repositories"])

    def test_trail_dashboard_loading_state(self):
        t = self.report["trail"]["dashboard"]["loading"]
        self.assertEqual((t["state"], t["busy"], t["preset"]), ("loading", "true", "key"))
        self.assertEqual(t["labelledBy"], "fc-trail-h")
        self.assertEqual(t["heading"], {"text": "Decision History", "id": "fc-trail-h"})
        self.assertEqual(t["sub"], "Append-only ledger from trail.db . Key decisions by default; routine gate ticks are folded.")
        self.assertEqual(t["skeletons"], 5)
        self.assertTrue(t["srLoading"])
        self.assertTrue(t["scroll"])
        self.assertEqual(t["total"], "– decisions")
        self.assertIsNone(t["auditClean"])
        self.assertTrue(t["logBtn"]["disabled"])
        bar = t["bar"]
        self.assertEqual(bar["disabled"], "true")
        self.assertTrue(all(p["disabled"] for p in bar["presets"]))
        self.assertTrue(bar["q"]["disabled"])
        self.assertTrue(all(s["disabled"] for s in bar["selects"].values()))
        self.assertTrue(bar["sup"]["disabled"])
        self.assertEqual(bar["meta"], "Loading…")
        self.assertEqual(t["rows"], [])
        self.assertIsNone(t["alert"])

    def test_trail_dashboard_signal_presets_search_and_grouped_phase_filter(self):
        t = self.report["trail"]["dashboard"]["populated"]
        bar = t["bar"]
        self.assertEqual(bar["role"], "search")
        self.assertEqual([(p["value"], p["pressed"], p["text"]) for p in bar["presets"]], [("key", "true", "Key Decisions"), ("attention", "false", "Attention 2"), ("all", "false", "All")])
        self.assertEqual(bar["q"]["placeholder"], "Search decision, why, card, PR, sha…")
        # 12 phases grouped into a compact <select>, never a 12-button wall; empty phases are disabled.
        groups = bar["selects"]["phase"]["groups"]
        self.assertEqual([g[0] for g in groups], ["Delivery", "Factory", "Signals"])
        self.assertEqual([o[0] for g in groups for o in g[1]], ["define", "design", "implement", "verify", "publish", "gate", "merge", "intake", "harness", "eval", "escape", "watchdog"])
        self.assertIn(["verify", "verify (2)", False], groups[0][1])
        self.assertIn(["publish", "publish (0)", True], groups[0][1])
        self.assertEqual(bar["selects"]["phase"]["options"][0], ["", "All phases (8)"])
        self.assertEqual(bar["sup"], {"checked": False, "disabled": False})
        self.assertEqual(bar["meta"], "7 shown")
        d = self.report["trail"]["dashboard"]
        self.assertEqual(self._rows(d["attention"]), ["d_7e30f91b5c04", "d_2d67a4e8f012"])
        self.assertEqual(d["attention"]["live"], "Attention: 2 decisions shown.")
        self.assertEqual(len(d["all"]["rows"]), 7)
        self.assertEqual(d["all"]["preset"], "all")
        sup = d["supShown"]
        self.assertIn("d_9a1e4c0b2f31", self._rows(sup))
        self.assertEqual(self._row(sup, "d_9a1e4c0b2f31")["superseded"], "true")
        self.assertEqual(self._row(sup, "d_9a1e4c0b2f31")["title"], "PR gate wait (superseded)")
        self.assertEqual(self._row(sup, "d_c4f08e2a91b7")["chain"]["expanded"], "true", "Show superseded opens chains")
        self.assertEqual(self._rows(d["search"]), ["d_f012c8a47b99"])
        self.assertEqual(d["searchServer"]["key"], ["factonio", "trail", 200, "signal", "all", "q", "graphql"])
        self.assertIsNone(d["searchServer"]["interval"])
        self.assertEqual(d["searchServer"]["rest"], ["/trail?limit=200&q=graphql"])  # signal=all is the server default
        # A query naming holdout never matches and is not echoed back.
        sb = d["searchBarrier"]
        self.assertEqual((sb["state"], sb["rows"]), ("filtered", []))
        self.assertEqual(sb["box"]["chips"], ["“[redacted]”"])

    def test_trail_dashboard_populated_rows_tiers_and_evidence(self):
        t = self.report["trail"]["dashboard"]["populated"]
        self.assertEqual((t["state"], t["busy"]), ("populated", "false"))
        self.assertEqual(t["total"], "8 decisions")
        self.assertEqual(t["auditClean"], "Audit clean")
        self.assertIsNone(t["audit"])
        self.assertEqual(t["logBtn"], {"text": "Log Human Decision", "disabled": False})
        self.assertEqual(len(t["days"]), 1)
        self.assertRegex(t["days"][0], r"^(2026-11-25|Today · Nov 25|Yesterday · Nov 25) 7 decisions$")
        self.assertEqual(t["more"], "Start of loaded window · server limit 200")
        # Superseded rows are hidden by default and nested under their correction instead.
        self.assertEqual(self._rows(t), ["d_f012c8a47b99", "d_7e30f91b5c04", "d_c4f08e2a91b7", "d_2d67a4e8f012", "d_11aa82b90c3e", "d_6f19b3d28e45", "d_4b81e2c0a119"])
        merged = self._row(t, "d_f012c8a47b99")
        self.assertEqual((merged["phase"], merged["actor"], merged["title"], merged["rawTitle"]), ("merge", "orchestrator", "PR merged", "pr_merged"))
        self.assertEqual((merged["result"], merged["tone"], merged["tier"]), ("merged", "ok", "2"))
        self.assertEqual(merged["domId"], "trail-d_f012c8a47b99")
        self.assertEqual(merged["datetime"], "2026-11-25T16:40:00.000000Z")
        self.assertEqual(
            [(p["ev"], p["text"], p["tone"], p["href"]) for p in merged["pills"]],
            [("pr_number", "PR #12", None, "https://github.com/omnim-ai/factonio/pull/12"), ("head_sha", "5d17830", None, None), ("tests_passed", "114 pass", "ok", None)],
        )
        self.assertEqual(merged["chev"], {"expanded": "false", "controls": "insp-d_f012c8a47b99", "label": "Expand evidence for PR merged"})
        # At most 3 scan pills, the rest counted for the inspector.
        carry = self._row(t, "d_c4f08e2a91b7")
        self.assertEqual([p["text"] for p in carry["pills"]], ["PR #12", "5d17830", "patch-id 91e4f0a", "+2 in inspector"])
        for r in t["rows"]:
            self.assertLessEqual(len([p for p in r["pills"] if p["ev"] not in ("more", "audit", "superseded-by")]), 3, r["id"])
            self.assertNotIn("tenant", [p["ev"] for p in r["pills"]])  # no ws pill without a filter
        # Tier 1: escape and watchdog stalls carry the destructive accent; humans get the accent rail.
        self.assertEqual((self._row(t, "d_7e30f91b5c04")["tier"], self._row(t, "d_7e30f91b5c04")["accent"]), ("1", "bad"))
        self.assertEqual((self._row(t, "d_2d67a4e8f012")["tier"], self._row(t, "d_2d67a4e8f012")["tone"]), ("1", "bad"))
        self.assertEqual(self._row(t, "d_6f19b3d28e45")["human"], "true")
        self.assertEqual(self._row(t, "d_4b81e2c0a119")["tone"], "warn")
        self.assertEqual(self._row(t, "d_11aa82b90c3e")["title"], "Spawn implementation")
        self.assertEqual(self._row(t, "d_11aa82b90c3e")["pills"][0]["href"], "https://github.com/acme/api/pull/7")
        pr = self.report["trail"]["dashboard"]["prClick"]
        self.assertTrue(pr["prevented"])
        self.assertTrue(pr["stopped"], "PR link never toggles the row")
        self.assertEqual(pr["windowOpens"], ["https://github.com/omnim-ai/factonio/pull/12"])

    def test_trail_dashboard_evidence_inspector_and_chain(self):
        d = self.report["trail"]["dashboard"]
        e = d["expanded"]
        self.assertEqual(e["expanded"], "true")
        self.assertEqual(e["chev"]["label"], "Collapse evidence for PR merged")
        insp = e["insp"]
        self.assertEqual(insp["id"], "insp-d_f012c8a47b99")
        self.assertEqual(insp["tabs"], [["fields", "true"], ["json", "false"]])
        self.assertEqual(insp["kv"]["why"], "Squash-merged PR #12 after GraphQL readback confirmed all four gate tiers green.")
        self.assertEqual(insp["kv"]["evidence.tests_passed"], "114")
        self.assertEqual(insp["kv"]["decision"], "pr_merged")
        self.assertTrue(insp["supersede"])
        self.assertEqual(d["expandedJson"]["tabs"], [["fields", "false"], ["json", "true"]])
        self.assertIn('"id": "d_f012c8a47b99"', d["expandedJson"]["json"])
        self.assertIsNone(d["expandedJson"]["kv"])
        chain = d["chainOpen"]
        self.assertEqual((chain["text"], chain["expanded"]), ("Supersedes 1 prior decision · original → corrected", "true"))
        self.assertEqual([i["id"] for i in chain["items"]], ["d_9a1e4c0b2f31"])
        self.assertIn("PR gate wait — inconclusive (superseded)", chain["items"][0]["text"])
        self.assertIn("→ corrected: pass at 5d17830", chain["items"][0]["text"])
        self.assertEqual(d["populated"]["rows"][2]["chain"]["expanded"], "false")
        self.assertEqual(d["selectNoToggle"], "false", "selecting text in a row never toggles it")

    def test_trail_dashboard_filters_and_no_match(self):
        d = self.report["trail"]["dashboard"]
        f = d["repoFiltered"]
        self.assertEqual(self._rows(f), ["d_11aa82b90c3e"])
        self.assertEqual((f["bar"]["selects"]["repo"]["value"], f["bar"]["selects"]["repo"]["set"]), ("acme-api", "true"))
        self.assertEqual(f["bar"]["selects"]["phase"]["options"][0], ["", "All phases (1)"])  # counts respect the repo filter
        self.assertEqual([p["text"] for p in f["rows"][0]["pills"] if p["ev"] == "tenant"], ["ws i42"])
        self.assertEqual(f["live"], "1 decision shown.")
        nm = d["noMatch"]
        self.assertEqual((nm["state"], nm["rows"]), ("filtered", []))
        self.assertIsNotNone(nm["bar"])
        self.assertEqual(nm["box"]["title"], "No decisions match")
        self.assertEqual(nm["box"]["chips"], ["repo acme-api", "phase merge"])
        self.assertIn("8 decisions are loaded outside this view.", nm["box"]["text"])
        self.assertEqual(nm["box"]["clear"], {"cls": "fc-btn fc-btn--primary", "text": "Clear filters"})
        c = d["cleared"]
        self.assertEqual((len(c["rows"]), c["preset"], c["bar"]["selects"]["repo"]["value"], c["live"]), (7, "all", "", "Filters cleared."))
        tf = d["tenantFiltered"]
        self.assertEqual(len(tf["rows"]), 6)
        self.assertNotIn("d_11aa82b90c3e", self._rows(tf))

    def test_trail_dashboard_jump_to_correction(self):
        j = self.report["trail"]["dashboard"]["jumpCorrection"]
        flashed = [(r["id"], r["expanded"]) for r in j["trail"]["rows"] if r["flash"] == "true"]
        self.assertEqual(flashed, [("d_c4f08e2a91b7", "true")])
        self.assertEqual(j["trail"]["bar"]["sup"]["checked"], True, "jumping keeps superseded rows visible")
        self.assertEqual(j["scroll"], [["trail-d_c4f08e2a91b7", {"block": "center", "behavior": "smooth"}]])
        self.assertEqual(j["focus"], ["Collapse evidence for Verify carryover"])
        self.assertEqual(j["trail"]["live"], "Jumped to d_c4f08e2a91b7.")

    def test_trail_dashboard_attention_banner_and_show_decision(self):
        d = self.report["trail"]["dashboard"]
        c = d["auditCollapsed"]
        self.assertIsNone(c["auditClean"])
        a = c["audit"]
        self.assertEqual(a["expanded"], "false")
        self.assertTrue(a["head"].startswith("3 audit findings need attention"))
        self.assertTrue(a["head"].endswith("trail --audit · 8 inspected"))
        self.assertEqual(a["cats"], [["gate_loosening", "gate loosening 1", "bad"], ["weak_evidence", "weak evidence 1", "warn"], ["missing_evidence", "missing evidence 1", "warn"]])
        self.assertEqual(a["items"], [])
        flagged = self._row(c, "d_f012c8a47b99")
        self.assertEqual((flagged["tier"], flagged["accent"]), ("1", "bad"))
        self.assertEqual([p["text"] for p in flagged["pills"] if p["ev"] == "audit"], ["audit · gate loosening"])
        self.assertEqual(c["bar"]["presets"][1]["text"], "Attention 4")
        o = d["auditOpen"]["audit"]
        self.assertEqual(o["expanded"], "true")
        self.assertEqual([i["id"] for i in o["items"]], ["d_f012c8a47b99", "d_9a1e4c0b2f31", "d_6f19b3d28e45"])
        self.assertTrue(o["items"][0]["text"].startswith("high A required hook was removed"))
        # A holdout-referencing message is replaced by its category label.
        self.assertEqual(o["items"][2]["text"], "medium weak evidence weak evidence · d_6f19b3d28e45 Show decision")
        self.assertEqual(d["auditBarrier"], [])
        s = d["showDecision"]
        self.assertEqual(s["trail"]["live"], "Filters cleared · showing the flagged decision")
        self.assertEqual(s["trail"]["preset"], "all")
        self.assertEqual(s["trail"]["bar"]["selects"]["repo"]["value"], "")
        self.assertTrue(s["trail"]["bar"]["sup"]["checked"], "a superseded target turns Show superseded on")
        self.assertEqual([(r["id"], r["expanded"]) for r in s["trail"]["rows"] if r["flash"] == "true"], [("d_9a1e4c0b2f31", "true")])
        self.assertEqual(s["scroll"], [["trail-d_9a1e4c0b2f31", {"block": "center", "behavior": "smooth"}]])
        self.assertEqual(s["focus"], ["Collapse evidence for PR gate wait"])
        # Older backend without /trail/audit: no banner, no clean pill, ledger unaffected.
        old = d["audit404"]
        self.assertIsNone(old["audit"])
        self.assertIsNone(old["auditClean"])
        self.assertEqual(len(old["rows"]), 7)
        self.assertEqual(self._row(old, "d_f012c8a47b99")["tier"], "2")

    def test_trail_dashboard_routine_ticks_fold(self):
        d = self.report["trail"]["dashboard"]
        k = d["routineKey"]
        self.assertEqual(self._rows(k), ["d_rk01", "d_rk02", "d_rt08"])  # failing routine codes stay visible
        self.assertEqual(k["bar"]["meta"], "3 shown · 5 routine ticks hidden")
        self.assertEqual(k["folds"], [])
        self.assertEqual(self._row(k, "d_rk02")["title"], "Human gate override")
        a = d["routineAll"]
        self.assertEqual(a["folds"], [{"key": "fold:d_rt01", "expanded": "false", "text": "gate 4 routine pr_gate checks · 3 passed, 1 blocked · 17:34–17:52 Expand", "phase": "gate", "rows": []}])
        self.assertEqual(self._rows(a), ["d_rk01", "d_rk02", "d_rt07", "d_rt08"])
        self.assertEqual(self._row(a, "d_rt07")["tier"], "3")
        u = d["routineUnfolded"]
        self.assertEqual(u["folds"][0]["expanded"], "true")
        self.assertEqual(u["folds"][0]["rows"], ["d_rt01", "d_rt02", "d_rt03", "d_rt04"])
        self.assertTrue(all(self._row(u, rid)["inFold"] for rid in u["folds"][0]["rows"]))
        self.assertTrue(all(self._row(u, rid)["pills"] == [] for rid in u["folds"][0]["rows"]), "tier-3 rows carry no scan pills")

    def test_trail_dashboard_log_human_decision_modal(self):
        lg = self.report["trail"]["dashboard"]["logModal"]
        o = lg["open"]
        self.assertEqual((o["id"], o["role"], o["modal"], o["labelledBy"], o["title"]), ("fc-hd", "dialog", "true", "fc-hd-t", "Log human decision"))
        self.assertEqual(list(o["fields"]), ["phase", "decision", "why", "result", "repo", "tenant", "card_id", "evidence"])
        self.assertEqual(o["fields"]["phase"]["value"], "gate")
        self.assertIn("retro", o["text"])
        self.assertFalse(o["confirm"])
        self.assertEqual(o["go"], {"text": "Confirm & append to trail.db", "disabled": True, "action": "append-decision"})
        self.assertEqual(lg["initialFocus"], ["fc-hd-phase"])
        # Disabled Confirm is skipped: Shift+Tab wraps to Cancel, never stalls on the guard and escapes.
        self.assertEqual(lg["trapUnchecked"]["start"], ["Cancel"])
        self.assertEqual(lg["trapUnchecked"]["end"], ["Close dialog"])
        self.assertEqual(lg["trapChecked"]["start"], ["Confirm & append to trail.db"])
        self.assertEqual(lg["trapChecked"]["end"], ["Close dialog"])
        self.assertEqual(lg["trapChecked"]["shiftTabOnDialog"], {"focus": ["Confirm & append to trail.db"], "prevented": True})
        self.assertEqual(lg["trapChecked"]["tabOnDialog"], {"focus": ["Close dialog"], "prevented": True})
        # Busy: every control is disabled, so focus stays on the dialog (tabIndex -1) in both directions.
        self.assertEqual(lg["busyFocus"], ["dialog"], "going busy disables the focused button; focus moves to the dialog")
        for k in ("start", "end"):
            self.assertEqual(lg["trapBusy"][k], ["dialog"], k)
        for k in ("shiftTabOnDialog", "tabOnDialog"):
            self.assertEqual(lg["trapBusy"][k], {"focus": ["dialog"], "prevented": True}, k)
        u = lg["unconfirmed"]
        self.assertEqual(u["fields"]["decision"]["err"], "A decision code is required.")
        self.assertEqual(u["fields"]["why"]["invalid"], "true")
        self.assertEqual(lg["invalid"]["fields"]["evidence"]["err"], "Evidence must be a JSON object.")
        self.assertEqual(lg["restsInvalid"], 0, "nothing is sent until the form is valid and confirmed")
        b = lg["busy"]
        self.assertEqual((b["busy"], b["go"]["text"], b["go"]["disabled"]), ("true", "Appending…", True))
        self.assertTrue(lg["escWhileBusy"], "Escape cannot close an in-flight modal")
        self.assertEqual(lg["body"], [{"method": "POST", "body": {"decision": "override_gate", "phase": "gate", "why": "Flaky e2e accepted by owner.", "confirmed": True, "card_id": "t_bcf5502a", "evidence": {"pr_number": 12}}}])
        self.assertNotIn("actor", lg["body"][0]["body"])
        after = lg["after"]
        self.assertIsNone(after["modal"])
        self.assertEqual(after["live"], "Appended override_gate (actor: human)")
        self.assertEqual((after["rows"][0]["id"], after["rows"][0]["flash"]), ("d_human00001", "true"))
        self.assertEqual(lg["notified"], [{"kind": "info", "message": "Appended override_gate to trail.db"}])
        self.assertIn("factonio/trail", lg["invalidated"])
        err = lg["error"]
        self.assertEqual(err["banner"], {"tone": "bad", "text": "Not recorded (422). phase is not allowed Nothing was appended. Your entries are kept."})
        self.assertEqual((err["fields"]["decision"]["value"], err["fields"]["why"]["value"]), ("note", "Context."))
        self.assertIsNone(lg["closed"])
        sp = lg["supersede"]
        self.assertEqual(sp["title"], "Supersede decision")
        self.assertEqual(sp["quote"]["target"], "d_2d67a4e8f012")
        self.assertEqual((sp["fields"]["phase"]["value"], sp["fields"]["decision"]["value"], sp["fields"]["card_id"]["value"]), ("watchdog", "override_stall_detected", "t_caf57a3b"))
        self.assertEqual(lg["supersedeBody"][0]["supersedes_id"], "d_2d67a4e8f012")
        self.assertTrue(lg["supersedeBody"][0]["confirmed"])
        sa = lg["supersedeAfter"]
        self.assertNotIn("d_2d67a4e8f012", self._rows(sa), "the original folds under its correction")
        top = sa["rows"][0]
        self.assertEqual((top["id"], top["chain"]["expanded"], [i["id"] for i in top["chain"]["items"]]), ("d_human00002", "true", ["d_2d67a4e8f012"]))

    def test_trail_lineage_chain_loads_prior_beyond_loaded_window(self):
        lin = self.report["trail"]["lineage"]
        w = lin["window"]
        self.assertEqual(w["api"], {"total": 202, "loaded": 200, "target_loaded": False})
        self.assertEqual((w["before"]["text"], w["before"]["expanded"]), ("Supersedes 1 prior decision · original → corrected", "false"))
        self.assertEqual(w["loading"]["items"], [{"id": None, "missing": w["original"], "text": f"Loading prior decision {w['original']} …"}])
        a = w["after"]
        self.assertEqual(a["rests"], [f"/trail/{w['original']}"])
        self.assertEqual([i["id"] for i in a["chain"]["items"]], [w["original"]])
        self.assertIn("PR gate wait — inconclusive (superseded)", a["chain"]["items"][0]["text"])
        self.assertEqual(a["query"]["key"], ["factonio", "trail", 200])
        self.assertEqual(a["rows"], 60, "the ledger paginates the loaded window")
        self.assertEqual(lin["windowAgain"]["rests"], [], "a pinned prior needs no refetch")

    def test_trail_lineage_chain_falls_back_to_wide_page_on_older_hosts(self):
        lin = self.report["trail"]["lineage"]
        f = lin["fallback"]
        self.assertEqual(f["rests"], [f"/trail/{lin['window']['original']}", "/trail?limit=1000"])
        self.assertEqual([i["id"] for i in f["chain"]["items"]], [lin["window"]["original"]])

    def test_trail_lineage_chain_resolves_prior_from_another_tenant_without_leaving_scope(self):
        s = self.report["trail"]["lineage"]["scope"]
        self.assertNotIn(s["original"], s["api"]["scoped_ids"])
        self.assertEqual(s["before"]["query"]["key"], ["factonio", "trail", 200, "tenant", "lineage-b"])
        self.assertEqual(s["before"]["tenant"], "lineage-b")
        a = s["after"]
        self.assertEqual(a["rests"], [f"/trail/{s['original']}"])
        self.assertEqual([i["id"] for i in a["chain"]["items"]], [s["original"]])
        self.assertEqual(a["query"]["key"], ["factonio", "trail", 200, "tenant", "lineage-b"])
        self.assertEqual(a["tenant"], "lineage-b")

    def test_trail_lineage_chain_missing_prior_announces_cleanly(self):
        m = self.report["trail"]["lineage"]["missing"]
        self.assertIsNone(m["threw"])
        self.assertEqual(m["rests"], ["/trail/d_missing00000"])
        self.assertEqual(m["chain"]["items"], [{"id": None, "missing": "d_missing00000", "text": "Prior decision d_missing00000 could not be loaded."}])

    def test_trail_show_decision_beyond_first_page_renders_scrolls_and_focuses(self):
        lin = self.report["trail"]["lineage"]
        a = lin["deepLoaded"]
        self.assertEqual(a["before"], 60, "only the first page renders initially")
        self.assertEqual(a["rests"], [], "a loaded target needs no fetch")
        self.assertEqual(a["rendered"], 91, "the page grows to include row 90")
        self.assertEqual(a["targetIndex"], 90)
        self.assertEqual(a["row"], ["d_probe_090", "true", "true"])
        self.assertEqual(a["scroll"], [["trail-d_probe_090", {"block": "center", "behavior": "smooth"}]])
        self.assertEqual(a["focus"], ["Collapse evidence for Design approved"], "the row's open chevron takes focus")
        self.assertEqual(a["live"], "Filters cleared · showing the flagged decision")
        b = lin["deepFetched"]
        self.assertEqual(b["before"], 60)
        self.assertEqual(b["rests"], ["/trail/d_probe_450"])
        self.assertEqual((b["rendered"], b["targetIndex"]), (201, 200), "the fetched row renders after the 200 loaded ones")
        self.assertEqual(b["row"], ["d_probe_450", "true", "true"])
        self.assertEqual(b["scroll"], [["trail-d_probe_450", {"block": "center", "behavior": "smooth"}]])
        self.assertEqual(b["focus"], ["Collapse evidence for Design approved"], "the row's open chevron takes focus")

    def test_trail_base_poll_refreshes_audit_and_filtered_page_without_timers(self):
        p = self.report["trail"]["lineage"]["poll"]
        self.assertEqual(p["before"]["auditClean"], "Audit clean")
        self.assertIsNone(p["before"]["audit"])
        self.assertEqual(p["before"]["rows"], ["d_f012c8a47b99"])
        self.assertEqual(p["refetchedBefore"], [], "the first load triggers no extra refresh")
        self.assertIsNone(p["after"]["auditClean"])
        self.assertIn("audit findings need attention", p["after"]["audit"])
        self.assertEqual(p["after"]["rows"], ["d_sig_new0001", "d_f012c8a47b99"])
        self.assertEqual(sorted(p["refetched"]), ["audit", "signal/key/q/graphql"], "exactly one refresh each; no loop")
        self.assertEqual(p["intervals"], [], "no setInterval")
        self.assertEqual(p["queryIntervals"], ["factonio/trail/200=30000"], "the base query keeps the only interval")

    def test_trail_show_decision_drops_stale_lookup(self):
        r = self.report["trail"]["lineage"]["race"]
        self.assertEqual(r["loadingMsg"], "Loading d_orig_a00001…")
        self.assertEqual(r["rests"], ["/trail/d_orig_a00001", "/trail/d_orig_b00001"])
        self.assertEqual(r["rows"], [["d_live_000001", "false", "false"], ["d_orig_b00001", "true", "true"]])
        self.assertEqual(r["scroll"], [["trail-d_orig_b00001", {"block": "center", "behavior": "smooth"}]])
        self.assertEqual(r["live"], "Filters cleared · showing the flagged decision")

    def test_trail_dashboard_empty_state(self):
        e = self.report["trail"]["dashboard"]["empty"]
        self.assertEqual(e["state"], "empty")
        self.assertIsNone(e["bar"])
        self.assertEqual(e["box"]["kind"], "empty")
        self.assertEqual(e["box"]["title"], "No decisions yet")
        self.assertEqual(e["box"]["cmd"], "hermes factonio trail --limit 20")
        self.assertIsNone(e["box"]["clear"])
        self.assertFalse(e["logBtn"]["disabled"], "a human decision can be logged into an empty ledger")

    def test_trail_dashboard_api_error_states(self):
        d = self.report["trail"]["dashboard"]
        e = d["error"]
        self.assertEqual((e["state"], e["rows"], e["skeletons"]), ("error", [], 0))
        self.assertEqual(e["bar"]["disabled"], "true")
        self.assertIn("Couldn’t load the ledger.", e["alert"]["text"])
        self.assertIn("GET /trail returned 503", e["alert"]["text"])
        self.assertIn("Nothing was changed.", e["alert"]["text"])
        self.assertTrue(e["alert"]["retry"])
        self.assertEqual(d["errorRetry"], ["dash"])
        s = d["errorStale"]
        self.assertEqual(len(s["rows"]), 7)  # stale rows stay visible under the alert
        self.assertIn("Couldn’t refresh the ledger.", s["alert"]["text"])
        self.assertIn("could not be reached", s["alert"]["text"])
        self.assertEqual(s["bar"]["disabled"], "false")

    def test_trail_dashboard_deep_link_scopes_tenant(self):
        d = self.report["trail"]["dashboard"]
        dl = d["deepLink"]
        self.assertEqual(dl["query"]["key"], ["factonio", "trail", 200, "tenant", "acme-api-i42"])
        self.assertEqual(dl["rests"], ["/trail?tenant=acme-api-i42&limit=200", "/trail/audit?tenant=acme-api-i42"])
        self.assertEqual(dl["trail"]["bar"]["selects"]["tenant"]["value"], "acme-api-i42")
        self.assertEqual(self._rows(dl["trail"]), ["d_11aa82b90c3e"])
        self.assertEqual(dl["scroll"][0][0], "fc-trail-h")
        self.assertEqual(d["deepLinkCleared"], {"query": {"key": ["factonio", "trail", 200], "interval": 30000}, "rows": 7})

    # --- Decision Trail: sidebar strips + CardDrawer -----------------------------
    def test_trail_sidebar_query_is_visibility_aware(self):
        sb = self.report["trail"]["sidebar"]
        self.assertEqual(sb["query"], {"key": ["factonio", "trail", 200], "interval": 15000})
        self.assertEqual(sb["restPath"], "/trail?limit=200")
        self.assertEqual(sb["hiddenQuery"]["interval"], False)

    def test_trail_sidebar_loading_state(self):
        ld = self.report["trail"]["sidebar"]["loading"]
        self.assertEqual(ld["strip"]["text"], "Loading decisions…")
        self.assertTrue(ld["strip"]["disabled"])
        self.assertEqual(ld["strip"]["busy"], "true")
        self.assertTrue(ld["strip"]["isLastChild"])
        self.assertEqual(ld["drawer"]["dt"], "Decisions")
        self.assertEqual(ld["drawer"]["text"], "Loading…")

    def test_trail_sidebar_workstream_counts_and_expandable_rows(self):
        sb = self.report["trail"]["sidebar"]
        c = sb["collapsed"]
        self.assertEqual(c["meta"]["text"], "7 decisions verify · gate · implement · +2 1")
        self.assertEqual(c["meta"]["expanded"], "false")
        self.assertEqual(c["meta"]["flag"], "1")  # one active bad-tone decision; the superseded blocked one is excluded
        self.assertTrue(c["meta"]["isLastChild"])
        self.assertIsNone(c["meta"]["list"])
        self.assertEqual(c["sidebar"]["text"], "1 decision verify")
        self.assertIsNone(c["pillWsCollapsed"])  # collapsed workstream shows no strip
        self.assertEqual(len(c["stages"]), 4)  # strip adds no pipeline stages
        e = sb["expanded"]
        self.assertEqual(e["expanded"], "true")
        self.assertEqual(e["controls"], e["list"]["id"])
        self.assertEqual(e["list"]["label"], "Decisions for this workstream, newest first")
        self.assertEqual([i["id"] for i in e["list"]["items"]], ["d_m7", "d_m6", "d_m5", "d_m4", "d_m3"])
        self.assertEqual(e["list"]["more"], "+2 older Open in Decision Trail ›")
        items = {i["id"]: i for i in e["list"]["items"]}
        self.assertEqual(items["d_m7"]["tags"], [{"text": "verify", "tone": None}, {"text": "stalled", "tone": "reject"}])
        self.assertEqual(items["d_m6"]["tags"][1], {"text": "ready", "tone": "pass"})
        self.assertEqual([ch["text"] for ch in items["d_m6"]["chips"]], ["PR #42", "abcdef1", "patch-id 91e4f0a", "+2"])
        self.assertEqual(items["d_m6"]["chips"][3]["title"], "tests_passed 88, card_id t_a1b2c3d4")
        self.assertIsNone(items["d_m6"]["why"])  # no why text in the workstream list
        self.assertEqual(items["d_m5"]["tags"][1]["tone"], "info")
        self.assertEqual(sb["openTrailNav"], ["/factonio?tenant=tozes-factonio-enrich-cards-metadata"])
        self.assertEqual(sb["sidebarExpanded"]["list"]["more"], "Full ledger Open in Decision Trail ›")
        self.assertEqual(sb["recollapsed"], "false")

    def test_trail_sidebar_superseded_rows(self):
        items = {i["id"]: i for i in self.report["trail"]["sidebar"]["expanded"]["list"]["items"]}
        self.assertEqual(items["d_m3"]["superseded"], "true")
        self.assertTrue(items["d_m3"]["supTag"])
        self.assertEqual(items["d_m6"]["superseded"], "false")
        self.assertFalse(items["d_m6"]["supTag"])

    def test_trail_card_drawer_decisions(self):
        sb = self.report["trail"]["sidebar"]
        dr = sb["drawer"]
        self.assertEqual(dr["dt"], "Decisions · 3")
        self.assertEqual(dr["ddClass"], "fc-sb-full fc-sb-trail-dd")
        self.assertEqual(dr["fieldOrder"][-2:], ["decisions", "summary"])  # before Worker summary
        self.assertEqual([i["id"] for i in dr["items"]], ["d_m6", "d_m5", "d_m3"])
        self.assertEqual(dr["items"][0]["why"], "All merge gate tiers clear.")
        self.assertNotIn("card_id", [ch["ev"] for i in dr["items"] for ch in i["chips"]])
        self.assertEqual(dr["items"][0]["chips"][-1], {"ev": "more", "text": "+1", "tone": None, "title": "tests_passed 88"})
        self.assertTrue(dr["items"][2]["supTag"])
        self.assertEqual(sb["drawerNone"]["dt"], "Decisions · 0")
        self.assertEqual(sb["drawerNone"]["text"], "None recorded")

    def test_trail_sidebar_filtered_empty_and_error_states(self):
        sb = self.report["trail"]["sidebar"]
        # Card filters narrow cards but never the workstream ledger count.
        self.assertEqual(sb["phaseFiltered"]["text"], "7 decisions verify · gate · implement · +2 1")
        self.assertEqual(sb["noMatch"]["empty"]["title"], "No cards match these filters")
        self.assertEqual(sb["noMatch"]["strips"], 0)
        self.assertEqual(sb["emptyStrips"], 0)
        self.assertEqual(sb["emptyDrawer"]["text"], "None recorded")
        err = sb["error"]
        self.assertEqual(err["strip"]["text"], "Decisions unavailable Retry")
        self.assertEqual(err["strip"]["action"], "retry-trail")
        self.assertEqual(err["retry"], {"refetches": 1, "stopped": True})
        self.assertEqual(err["drawer"]["dt"], "Decisions")
        self.assertEqual(err["drawer"]["text"], "Unavailable")

    def test_trail_styles_use_theme_tokens_only(self):
        source = PLUGIN_JS.read_text(encoding="utf-8")
        start = source.index("const STYLES = `")
        styles = source[start : source.index("`", start + len("const STYLES = `"))]
        trail_rules = [line for line in styles.splitlines() if "fc-trail" in line or "fc-sb-trail" in line or 'fc-sb-tag[data-tone="info"]' in line]
        self.assertGreater(len(trail_rules), 60)
        for line in trail_rules:
            self.assertNotRegex(line, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", line)
        block = styles[styles.index("/* Decision Trail — dashboard") :]
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
        self.assertIn(".fc-trail * { animation: none !important; transition: none !important; }", block)

    # --- Registered Repositories editor ----------------------------------------
    def _ed_vals(self, editor):
        return {f: v["value"] for f, v in editor["fields"].items()}

    def test_repos_empty_state_keeps_heading_and_empty_state_only(self):
        e = self.report["repos"]["empty"]
        self.assertIn("Registered Repositories", e["text"])
        self.assertIn("No repositories registered", e["text"])
        self.assertIn("hermes factonio add owner/repo", e["text"])
        self.assertFalse(e["table"])
        self.assertFalse(e["legend"])
        self.assertFalse(e["sub"])
        self.assertNotIn("Global defaults", e["text"])

    def test_repos_table_layout_headers_and_legend(self):
        r = self.report["repos"]["read"]
        self.assertEqual(r["sectionClass"], "fc-sec fc-repos-sec")
        self.assertEqual(r["title"], "Registered Repositories")
        self.assertEqual(r["sub"], "Global defaults: design provider auto · design approval ask · rigor standard")
        # Seeded (not inherited) policy defaults sit on their own second line.
        self.assertEqual(
            r["subs"],
            ["Global defaults: design provider auto · design approval ask · rigor standard",
             "New repos start with merge auto · publish ask , set per repo.",
         "Audit runs the harness check live, read-only. It never edits repo files or .git/index ."],
        )
        self.assertTrue(r["wrapped"])  # .fc-repos-wrap scrolls the 992px table on narrow containers
        self.assertEqual(r["legend"], "value · global inherited value override")
        self.assertEqual(r["legendTags"], ["fc-tag fc-tag--inherit", "fc-tag fc-tag--set"])
        self.assertEqual(r["tableClass"], "fc-table fc-repos")
        self.assertEqual(
            r["cols"],
            ["fc-col-repo", "fc-col-branch", "fc-col-merge", "fc-col-publish", "fc-col-rounds", "fc-col-prov", "fc-col-appr", "fc-col-rigor", "fc-col-act"],
        )
        self.assertEqual(
            [h["text"] for h in r["headers"]],
            ["Repository Local path", "Branch", "Merge policy", "Publish policy", "Max fix rounds", "Design provider", "Design approval", "Rigor", "Harness & actions"],
        )
        self.assertEqual(r["headers"][4]["className"], "fc-num")
        self.assertEqual(r["headers"][8]["className"], "fc-act")
        self.assertEqual(r["order"], ["row:tozes-factonio", "row:acme-web-dashboard", "row:acme-cli-tools"])
        self.assertIsNone(r["editor"])
        # No Add / Remove repository controls: only one Edit button per row.
        self.assertEqual(r["buttons"], ["Audit", "Edit"] * 3)

    def test_repos_rows_show_all_parameters_and_inherited_vs_override(self):
        rows = {row["slug"]: row for row in self.report["repos"]["read"]["rows"]}
        a = rows["tozes-factonio"]
        self.assertEqual((a["repo"], a["path"], a["branch"], a["merge"], a["rounds"]), ("tozes/factonio", "/home/tozes/dev/factonio", "main", "auto", "3"))
        self.assertEqual(a["roundsClass"], "fc-num")
        self.assertEqual(a["branchTitle"], "main")
        self.assertEqual(a["provider"], {"cls": "fc-tag fc-tag--inherit", "text": "auto · global", "title": "Inherited from global default; resolves to claude-design"})
        self.assertEqual(a["approval"], {"cls": "fc-tag fc-tag--inherit", "text": "ask · global", "title": "Inherited from global default"})
        self.assertEqual(a["cells"], 9)
        self.assertEqual(a["mergeCell"], {"field": "merge_policy", "text": "auto", "pol": "auto", "title": "auto: merges automatically", "tag": False})
        self.assertEqual(a["publish"], {"field": "publish_policy", "text": "auto", "pol": "auto", "title": "auto: publishes automatically", "tag": False})
        b = rows["acme-web-dashboard"]
        self.assertEqual(b["mergeCell"], {"field": "merge_policy", "text": "ask", "pol": "ask", "title": "ask: waits for your approval to merge", "tag": False})
        self.assertEqual(b["publish"], {"field": "publish_policy", "text": "ask", "pol": "ask", "title": "ask: waits for your approval to publish", "tag": False})
        self.assertEqual(b["provider"], {"cls": "fc-tag fc-tag--set", "text": "hermes", "title": "Repository override"})
        self.assertEqual(b["approval"], {"cls": "fc-tag fc-tag--set", "text": "auto", "title": "Repository override"})
        c = rows["acme-cli-tools"]
        self.assertEqual(c["provider"]["cls"], "fc-tag fc-tag--set")
        self.assertEqual(c["provider"]["text"], "off")
        self.assertEqual(c["approval"]["cls"], "fc-tag fc-tag--inherit")
        for slug, row in rows.items():
            with self.subTest(slug=slug):
                self.assertEqual(row["edit"]["label"], "Edit")
                self.assertEqual(row["edit"]["ariaLabel"], f"Edit {row['repo']}")
                self.assertEqual(row["edit"]["expanded"], "false")
                self.assertIsNone(row["edit"].get("controls"))
                self.assertIsNone(row["edit"].get("active"))
                self.assertFalse(row["edit"]["disabled"])
                self.assertIsNone(row["editing"])

    def test_validate_repo_draft(self):
        v = self.report["repos"]["validate"]
        self.assertEqual(self.report["repos"]["exportsValidator"], "function")
        self.assertEqual(v["valid"], [[], [], [], [], []])
        self.assertEqual(
            v["branch"],
            {
                "empty": "Branch is required.",
                "blank": "Branch is required.",
                "null": "Branch is required.",
                "space": "Branch can’t contain spaces.",
                "tab": "Branch can’t contain spaces.",
                "dots": "Branch can’t contain “..”.",
                "dash": "Branch can’t start with “-”.",
                "ctrl": "Branch has invalid characters.",
                "del": "Branch has invalid characters.",
            },
        )
        for case, msg in v["rounds"].items():
            with self.subTest(case=case):
                self.assertEqual(msg, "Must be a whole number, 1 or more.")

    def test_validate_repo_draft_rejects_unsafe_max_fix_rounds(self):
        v = self.report["repos"]["validate"]
        rng = "Must be 9007199254740991 or less."
        self.assertEqual(v["unsafe"], {"pow53": rng, "pow53plus1": rng, "overflow309": rng, "pow53Num": rng, "bigNum": rng})
        self.assertEqual(v["safeMax"], [[], [], [], []])

    def test_repo_editor_rejects_unsafe_max_fix_rounds(self):
        for case, u in self.report["repos"]["unsafe"].items():
            with self.subTest(case=case):
                self.assertEqual(u["rests"], [])
                self.assertEqual(u["value"], u["expected"])  # draft preserved
                self.assertEqual(u["invalid"], "true")
                self.assertEqual(u["err"], "Must be 9007199254740991 or less.")
                if "focus" in u:
                    self.assertEqual(u["focus"], [u["roundsId"]])

    def test_repo_draft_changes_only_explicit_edits_against_live_repo(self):
        c = self.report["repos"]["changesEdits"]
        self.assertEqual(c["onlyEdited"], {"default_branch": "user-feature"})
        self.assertEqual(c["explicitOriginal"], {"merge_policy": "auto", "design_provider": None})
        self.assertEqual(c["equalLive"], {})
        self.assertEqual(c["none"], {})

    def _patch(self, body):
        return [{"path": "/repos/tozes-factonio", "opts": {"method": "PATCH", "body": body}}]

    def test_refresh_then_explicit_original_merge_policy_is_kept_and_sent(self):
        e = self.report["repos"]["editBack"]["merge"]
        self.assertEqual(self._ed_vals(e["refreshed"])["merge_policy"], "ask")
        self.assertTrue(e["refreshed"]["save"]["disabled"])
        self.assertEqual(self._ed_vals(e["chosen"])["merge_policy"], "auto")
        self.assertEqual(e["chosen"]["msg"]["text"], "1 unsaved change")
        self.assertFalse(e["chosen"]["save"]["disabled"])
        self.assertEqual(e["rests"], self._patch({"merge_policy": "auto"}))

    def test_refresh_then_explicit_inherit_global_provider_is_kept_and_sent(self):
        e = self.report["repos"]["editBack"]["provider"]
        self.assertEqual(self._ed_vals(e["refreshed"])["design_provider"], "hermes")
        self.assertEqual(self._ed_vals(e["chosen"])["design_provider"], "")
        self.assertEqual(e["chosen"]["msg"]["text"], "1 unsaved change")
        self.assertFalse(e["chosen"]["save"]["disabled"])
        self.assertEqual(e["rests"], self._patch({"design_provider": None}))

    def test_refresh_then_explicit_original_branch_and_rounds_are_kept_and_sent(self):
        e = self.report["repos"]["editBack"]["text"]
        self.assertEqual(self._ed_vals(e["refreshed"])["default_branch"], "develop")
        self.assertEqual(self._ed_vals(e["refreshed"])["max_fix_rounds"], "5")
        self.assertEqual(self._ed_vals(e["chosen"])["default_branch"], "main")
        self.assertEqual(self._ed_vals(e["chosen"])["max_fix_rounds"], "3")
        self.assertEqual(e["chosen"]["msg"]["text"], "2 unsaved changes")
        self.assertFalse(e["chosen"]["save"]["disabled"])
        self.assertEqual(e["rests"], self._patch({"default_branch": "main", "max_fix_rounds": 3}))

    def test_refresh_untouched_fields_follow_live_then_explicit_originals_are_sent(self):
        e = self.report["repos"]["editBack"]["mixed"]
        vals = self._ed_vals(e["refreshed"])
        self.assertEqual((vals["default_branch"], vals["merge_policy"], vals["design_provider"]), ("user-feature", "ask", "hermes"))
        self.assertEqual(e["refreshed"]["msg"]["text"], "1 unsaved change")
        self.assertEqual(e["firstRests"], self._patch({"default_branch": "user-feature"}))
        vals = self._ed_vals(e["chosen"])
        self.assertEqual((vals["default_branch"], vals["merge_policy"], vals["design_provider"]), ("user-feature", "auto", ""))
        self.assertEqual(e["chosen"]["msg"]["text"], "3 unsaved changes")
        self.assertEqual(
            e["allRests"],
            self._patch({"default_branch": "user-feature", "merge_policy": "auto", "design_provider": None}),
        )

    def test_reverting_edit_to_live_value_drops_it_and_follows_refresh(self):
        e = self.report["repos"]["editBack"]["revert"]
        self.assertTrue(e["save"]["disabled"])
        self.assertEqual(self._ed_vals(e["after"])["merge_policy"], "ask")
        self.assertTrue(e["after"]["save"]["disabled"])

    def test_repo_editor_background_refresh_does_not_revert_untouched_fields(self):
        b = self.report["repos"]["refresh"]
        self.assertEqual(b["before"]["msg"]["text"], "1 unsaved change")
        self.assertEqual(b["after"]["msg"]["text"], "1 unsaved change")
        # Untouched fields reconcile to refreshed values; the user's edit is preserved.
        self.assertEqual(
            self._ed_vals(b["after"]),
            {"default_branch": "user-feature", "merge_policy": "ask", "publish_policy": "auto", "max_fix_rounds": "3", "design_provider": "hermes", "design_approval": "", "default_rigor": ""},
        )
        self.assertEqual(
            b["rests"],
            [{"path": "/repos/tozes-factonio", "opts": {"method": "PATCH", "body": {"default_branch": "user-feature"}}}],
        )
        self.assertTrue(b["closed"])
        k = b["keepEdit"]
        self.assertEqual(k["msg"]["text"], "1 unsaved change")
        self.assertEqual(self._ed_vals(k)["merge_policy"], "ask")
        self.assertEqual(self._ed_vals(k)["max_fix_rounds"], "6")
        self.assertEqual(
            b["keepEditRests"],
            [{"path": "/repos/tozes-factonio", "opts": {"method": "PATCH", "body": {"merge_policy": "ask"}}}],
        )

    def test_patch_body_only_changed_fields_and_error_parsing(self):
        rep = self.report["repos"]
        # Trimmed branch equal to the original is not a change; "" -> null; rounds -> Number.
        self.assertEqual(rep["changes"], {"default_branch": "main", "max_fix_rounds": 7, "design_provider": None})
        d = rep["describe"]
        self.assertEqual(d["ipc422"], {"status": 422, "detail": "max_fix_rounds must be >= 1"})
        self.assertEqual(d["plain404"], {"status": 404, "detail": "Repository 'x' is not registered."})
        self.assertEqual(d["pydantic"], {"status": 422, "detail": "field required; bad"})
        self.assertEqual(d["noBody"], {"status": 500, "detail": "HTTP 500"})
        self.assertEqual(d["network"]["status"], None)

    def test_repo_editor_opens_beneath_row_with_focus_and_fields(self):
        o = self.report["repos"]["lifecycle"]["opened"]
        ed = o["editor"]
        self.assertEqual(o["table"]["order"], ["row:tozes-factonio", "editor:tozes-factonio", "row:acme-web-dashboard", "row:acme-cli-tools"])
        row = o["table"]["rows"][0]
        self.assertEqual(row["editing"], "true")
        self.assertEqual(row["edit"]["expanded"], "true")
        self.assertEqual(row["edit"]["active"], "true")
        self.assertEqual(row["edit"]["controls"], ed["formId"])
        self.assertEqual(ed["editing"], "true")
        self.assertEqual(ed["colSpan"], 9)
        self.assertEqual(ed["formLabel"], "Edit settings for tozes/factonio")
        self.assertTrue(ed["noValidate"])
        self.assertEqual(ed["identity"], "Editing tozes-factonio Path /home/tozes/dev/factonio")
        self.assertEqual(ed["fieldsetClass"], "fc-ed-grid")
        self.assertEqual(ed["fieldOrder"], ["default_branch", "merge_policy", "publish_policy", "max_fix_rounds", "design_provider", "design_approval", "default_rigor"])
        self.assertEqual(self._ed_vals(ed), {"default_branch": "main", "merge_policy": "auto", "publish_policy": "auto", "max_fix_rounds": "3", "design_provider": "", "design_approval": "", "default_rigor": ""})
        for name, f in ed["fields"].items():
            with self.subTest(field=name):
                self.assertEqual(f["labelFor"], f["id"])
                self.assertEqual(f["describedBy"], f["hint"]["id"])
                self.assertIsNone(f["invalid"])
        self.assertEqual(ed["fields"]["max_fix_rounds"]["type"], "number")
        self.assertEqual(ed["fields"]["design_provider"]["options"][0], ["", "Inherit global (auto)"])
        self.assertEqual(ed["fields"]["design_approval"]["options"], [["", "Inherit global (ask)"], ["ask", "ask"], ["auto", "auto"]])
        self.assertEqual(ed["fields"]["design_provider"]["hint"]["text"], "Currently resolves to claude-design .")
        # Focus moves to the Default branch input; Save disabled until dirty.
        self.assertEqual(o["focus"], [ed["fields"]["default_branch"]["id"]])
        self.assertEqual(ed["save"], {"label": "Save changes", "disabled": True, "type": "submit", "spin": False})
        self.assertEqual(ed["cancel"], {"disabled": False, "type": "button"})
        self.assertEqual(ed["msg"], {"text": "", "className": "fc-ed-msg"})

    def test_repo_editor_dirty_tracking_and_provider_hints(self):
        L = self.report["repos"]["lifecycle"]
        h = L["providerHermes"]
        self.assertEqual(h["msg"]["text"], "1 unsaved change")
        self.assertFalse(h["save"]["disabled"])
        self.assertEqual(h["fields"]["design_provider"]["hint"]["text"], "Resolves to hermes (overrides global auto ).")
        self.assertEqual(L["providerOff"]["text"], "Design phase is skipped.")
        self.assertEqual(L["providerAuto"]["text"], "Currently resolves to claude-design .")
        # Reverting to the original value clears the dirty state.
        self.assertEqual(L["providerInherit"]["msg"]["text"], "")
        self.assertTrue(L["providerInherit"]["save"]["disabled"])
        self.assertEqual(L["dirtyInvalid"]["msg"]["text"], "2 unsaved changes")
        self.assertEqual(L["allFixed"]["msg"]["text"], "3 unsaved changes")

    def test_repo_editor_validation_blocks_patch(self):
        L = self.report["repos"]["lifecycle"]
        inv = L["invalidSubmit"]
        self.assertEqual(inv["rests"], [])
        ed = inv["editor"]
        b, n = ed["fields"]["default_branch"], ed["fields"]["max_fix_rounds"]
        self.assertEqual(b["invalid"], "true")
        self.assertEqual(b["describedBy"], b["err"]["id"])
        self.assertEqual(b["err"]["text"], "Branch can’t contain “..”.")
        self.assertIsNone(b["hint"])
        self.assertEqual(n["invalid"], "true")
        self.assertEqual(n["err"]["text"], "Must be a whole number, 1 or more.")
        self.assertEqual(ed["msg"], {"text": "Fix 2 fields to save.", "className": "fc-ed-msg fc-ed-msg--err"})
        self.assertEqual(inv["focus"], [b["id"]])  # first invalid field
        self.assertEqual(self._ed_vals(ed)["default_branch"], "feature..bad")  # draft untouched
        # Live re-validation of already-flagged fields.
        live = L["liveFixBranch"]
        self.assertIsNone(live["fields"]["default_branch"]["invalid"])
        self.assertEqual(live["fields"]["max_fix_rounds"]["invalid"], "true")
        self.assertEqual(live["msg"]["text"], "Fix 1 field to save.")
        self.assertIsNone(L["allFixed"]["fields"]["max_fix_rounds"]["invalid"])

    def test_repo_editor_saving_state_and_patch_body(self):
        sv = self.report["repos"]["lifecycle"]["saving"]
        self.assertEqual(
            sv["rests"],
            [{"path": "/repos/tozes-factonio", "opts": {"method": "PATCH", "body": {"default_branch": "develop", "max_fix_rounds": 4, "design_provider": "hermes"}}}],
        )
        ed = sv["editor"]
        self.assertEqual(ed["busy"], "true")
        self.assertTrue(ed["fieldsetDisabled"])
        self.assertTrue(ed["cancel"]["disabled"])
        self.assertEqual(ed["save"], {"label": "Saving…", "disabled": True, "type": "submit", "spin": True})
        self.assertEqual(ed["msg"]["text"], "Saving 3 changes…")
        self.assertEqual([r["edit"]["disabled"] for r in sv["table"]["rows"]], [True, True, True])
        self.assertTrue(self.report["repos"]["lifecycle"]["escapeWhileSaving"])  # Escape ignored while saving

    def test_repo_editor_api_and_network_errors_preserve_draft(self):
        L = self.report["repos"]["lifecycle"]
        ae = L["apiError"]
        ed = ae["editor"]
        self.assertIsNotNone(ed)
        self.assertEqual(ed["alert"], "Couldn’t save (422). Invalid default_branch 'develop'. Your changes are still here.")
        self.assertEqual(ed["save"]["label"], "Retry save")
        self.assertFalse(ed["save"]["disabled"])
        self.assertIsNone(ed["busy"])
        self.assertFalse(ed["fieldsetDisabled"])
        self.assertEqual(self._ed_vals(ed), {"default_branch": " develop ", "merge_policy": "auto", "publish_policy": "auto", "max_fix_rounds": "4", "design_provider": "hermes", "design_approval": "", "default_rigor": ""})
        self.assertEqual(ae["notifications"], [])
        self.assertEqual(ae["invalidations"], [])
        self.assertFalse(ae["table"]["rows"][0]["edit"]["disabled"])
        ne = L["networkError"]
        self.assertEqual(ne["alert"], "Couldn’t save. Network error. Your changes are still here.")
        self.assertEqual(ne["save"]["label"], "Retry save")
        self.assertEqual(self._ed_vals(ne)["max_fix_rounds"], "4")

    def test_repo_editor_successful_save(self):
        s = self.report["repos"]["lifecycle"]["saved"]
        self.assertIsNone(s["editor"])
        self.assertEqual(s["invalidations"], [{"queryKey": ["factonio", "repos"]}])
        self.assertEqual(s["refetches"], 0)
        self.assertEqual(s["notifications"], [{"kind": "info", "message": "Saved settings for tozes/factonio"}])
        self.assertEqual(s["focus"], ["Edit tozes/factonio"])
        self.assertEqual(s["table"]["rows"][0]["edit"]["expanded"], "false")
        self.assertEqual(len(s["rests"]), 3)  # 422, network error, success
        c = self.report["repos"]["clear"]
        self.assertEqual(self._ed_vals(c["opened"])["design_provider"], "hermes")
        self.assertEqual(c["dirty"]["msg"]["text"], "2 unsaved changes")
        self.assertEqual(c["dirty"]["fields"]["design_provider"]["hint"]["text"], "Currently resolves to claude-design .")
        self.assertEqual(
            c["after"]["rests"],
            [{"path": "/repos/acme-web-dashboard", "opts": {"method": "PATCH", "body": {"design_provider": None, "design_approval": None}}}],
        )
        self.assertEqual(c["after"]["refetches"], 1)  # refetch fallback without invalidateQueries
        self.assertIsNone(c["after"]["editor"])
        self.assertEqual(c["after"]["notifications"], [{"kind": "info", "message": "Saved settings for acme/web-dashboard"}])

    def test_repo_editor_cancel_escape_and_single_editor(self):
        x = self.report["repos"]["cancel"]
        self.assertEqual(x["openedProvider"]["value"], "off")
        self.assertEqual(x["openedProvider"]["hint"]["text"], "Design phase is skipped.")
        self.assertEqual(x["dirtyMsg"], "2 unsaved changes")
        self.assertIsNone(x["afterCancel"]["editor"])
        self.assertEqual(x["afterCancel"]["focus"], ["Edit acme/cli-tools"])
        self.assertEqual(x["afterCancel"]["expanded"], "false")
        self.assertEqual(x["reopened"]["branch"], "release/v2")
        self.assertEqual(x["reopened"]["merge"], "auto")
        self.assertEqual(x["reopened"]["msg"], "")
        self.assertTrue(x["reopened"]["save"]["disabled"])
        self.assertTrue(x["enterKeepsOpen"])
        self.assertIsNone(x["afterEscape"]["editor"])
        self.assertEqual(x["afterEscape"]["focus"], ["Edit acme/cli-tools"])
        self.assertEqual(x["reopenedAfterEscape"], "2")
        self.assertEqual(x["switched"]["slug"], "tozes-factonio")
        self.assertEqual(x["switched"]["editors"], 1)
        self.assertEqual(x["switched"]["order"][:2], ["row:tozes-factonio", "editor:tozes-factonio"])
        self.assertEqual(x["switchedBackPristine"], "release/v2")
        self.assertIsNone(x["toggleClosed"])
        self.assertEqual(x["rests"], [])

    def test_repo_editor_publish_policy_select_and_partial_patch(self):
        p = self.report["repos"]["publish"]
        f = p["opened"]
        self.assertEqual((f["label"], f["tag"], f["value"]), ("Publish policy", "select", "auto"))
        self.assertEqual(f["options"], [["auto", "auto"], ["ask", "ask"]])  # no inherit option
        self.assertEqual(f["hint"]["text"], "auto pushes and opens a PR once verified; ask keeps work local until you approve.")
        self.assertEqual(f["describedBy"], f["hint"]["id"])
        self.assertTrue(f["id"].endswith("-publish_policy"))
        self.assertEqual(p["dirty"]["msg"]["text"], "1 unsaved change")
        self.assertFalse(p["dirty"]["save"]["disabled"])
        self.assertEqual(self._ed_vals(p["dirty"])["publish_policy"], "ask")
        self.assertEqual(p["reverted"]["msg"]["text"], "")
        self.assertTrue(p["reverted"]["save"]["disabled"])
        self.assertEqual(p["rests"], self._patch({"publish_policy": "ask"}))
        self.assertTrue(p["closed"])
        self.assertEqual(p["changes"], {"publish_policy": "auto"})
        # An unresolvable policy (API null) is never presented as auto, so choosing auto is a real edit.
        # An omitted key (older backend) inherits the global default, auto when that is omitted too.
        self.assertEqual(p["legacySeed"], "auto")
        self.assertEqual(p["unknownSeed"], "")
        self.assertEqual(p["fixUnknown"], {"publish_policy": "auto"})
        self.assertEqual(f["disabledOptions"], [])
        ask = p["openedAsk"]
        self.assertEqual(ask["value"], "ask")
        self.assertEqual(ask["options"], [["auto", "auto"], ["ask", "ask"]])
        self.assertEqual(ask["disabledOptions"], [])
        self.assertEqual(
            p["seeds"],
            {"omittedNoDefaults": "auto", "omittedDefaultAsk": "ask", "omittedDefaultNull": "", "omittedDefaultBogus": "",
             "nullWithDefaultAuto": "", "bogus": "", "explicitAsk": "ask"},
        )
        self.assertEqual(p["omittedNoSpurious"], {"default_branch": "main"})

    def test_repo_editor_explicit_null_publish_policy_is_unknown_never_auto(self):
        n = self.report["repos"]["publishNull"]
        self.assertEqual(n["subs"][1], "New repos start with merge auto · publish unknown , set per repo.")
        self.assertEqual(n["bogusDefaultSubs"][1], "New repos start with merge auto · publish unknown , set per repo.")
        self.assertEqual(n["omittedDefaultSubs"][1], "New repos start with merge auto · publish auto , set per repo.")
        null_cell, ask_cell, bogus_cell = n["cells"]
        for cell in (null_cell, bogus_cell):
            self.assertEqual((cell["text"], cell["pol"], cell["title"]), ("—", None, "publish policy: unknown"))
        self.assertEqual((ask_cell["text"], ask_cell["pol"]), ("ask", "ask"))
        f = n["opened"]["fields"]["publish_policy"]
        # Real DOM <select> needs a matching '' option, else it shows the first option (auto).
        self.assertEqual(f["value"], "")
        self.assertEqual(f["options"], [["", "Invalid / unknown — choose policy"], ["auto", "auto"], ["ask", "ask"]])
        self.assertEqual(f["disabledOptions"], [["", "Invalid / unknown — choose policy"]])
        self.assertEqual(n["opened"]["msg"]["text"], "")
        self.assertTrue(n["opened"]["save"]["disabled"])
        d = n["dirty"]
        self.assertEqual(d["fields"]["publish_policy"]["value"], "auto")
        self.assertEqual(d["fields"]["publish_policy"]["options"], [["auto", "auto"], ["ask", "ask"]])
        self.assertEqual(d["msg"]["text"], "1 unsaved change")
        self.assertFalse(d["save"]["disabled"])
        self.assertEqual(n["rests"], self._patch({"publish_policy": "auto"}))

    def test_repo_editor_omitted_publish_policy_falls_back_to_default(self):
        o = self.report["repos"]["publishOmitted"]
        for cell in o["cells"]:
            self.assertEqual((cell["text"], cell["pol"], cell["title"]), ("auto", "auto", "auto: publishes automatically"))
        self.assertEqual(o["cellsAskDefault"], ["ask", "ask", "ask"])
        f = o["opened"]["fields"]["publish_policy"]
        self.assertEqual(f["value"], "auto")
        self.assertEqual(f["options"], [["auto", "auto"], ["ask", "ask"]])
        self.assertEqual(f["disabledOptions"], [])
        self.assertEqual(o["opened"]["msg"]["text"], "")
        self.assertTrue(o["opened"]["save"]["disabled"])
        self.assertEqual(o["dirty"]["msg"]["text"], "1 unsaved change")
        self.assertEqual(o["rests"], self._patch({"default_branch": "trunk"}))

    def test_trail_publish_phase_tone_and_branch_pill(self):
        t = self.report["trail"]["dashboard"]["publish"]
        self.assertIn(["publish", "publish (3)", False], t["bar"]["selects"]["phase"]["groups"][0][1])
        rows = {r["id"]: r for r in t["rows"]}
        self.assertNotIn("dec_7f2e1d84c1", rows, "the refused attempt nests under its correction")
        pub = rows["dec_9a8c4f10b2"]
        self.assertEqual((pub["phase"], pub["result"], pub["tone"], pub["title"], pub["human"]), ("publish", "published", "ok", "Publish PR", "true"))
        self.assertEqual(
            [(p["ev"], p["text"], p["title"]) for p in pub["pills"]],
            [
                ("pr_number", "PR #42", "Open omnim-ai/factonio#42"),
                ("branch", "branch feat/publish-policy → main", "branch feat/publish-policy → base main"),
                ("head_sha", "8f2c1a9", "head_sha 8f2c1a9e77"),
                ("more", "+1 in inspector", None),
            ],
        )  # fixed scan order: PR, branch → base, sha; base_branch folds into the branch pill
        self.assertEqual([i["id"] for i in pub["chain"]["items"]], [])
        long_ = rows["dec_draftlong01"]
        self.assertEqual(long_["pills"][0]["text"], "branch " + "b" * 48 + " → develop")
        self.assertEqual(long_["pills"][0]["title"], "branch " + "b" * 60 + " → base develop")
        self.assertEqual((long_["pills"][1]["ev"], long_["pills"][1]["text"]), ("draft", "draft"))
        only = self.report["trail"]["dashboard"]["publishOnly"]
        self.assertEqual([r["phase"] for r in only["rows"]], ["publish"] * 2)
        self.assertEqual(only["bar"]["selects"]["phase"]["value"], "publish")

    def test_publish_policy_styles_and_layout_rules(self):
        source = PLUGIN_JS.read_text(encoding="utf-8")
        start = source.index("const STYLES = `")
        styles = source[start : source.index("`", start + len("const STYLES = `"))]
        self.assertNotRegex(styles, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
        for needed in (
            ".fc-repos-wrap { overflow-x: auto; max-width: 100%; }",
            ".fc-repos { table-layout: fixed; min-width: 992px; }",
            ".fc-col-act { width: 15%; }",  # harness badge + Audit + Edit share one line
            "position: sticky; left: 0; max-width: 100cqi; box-sizing: border-box;",
            '@container (max-width: 640px) { .fc-ed-grid > .fc-field[data-field="default_branch"] { grid-column: 1 / -1; } }',
            ".fc-pol::before",
            '.fc-pol[data-pol="ask"] { color: var(--ui-text-primary); font-weight: 600; }',
            '.fc-pol[data-pol="ask"]::before { border-color: var(--ui-accent); background: var(--ui-accent); }',
            "color-mix(in srgb, var(--ui-text-secondary) 58%, transparent)",
        ):
            self.assertIn(needed, styles)
        widths = dict(re.findall(r"\.fc-col-([a-z]+) \{ width: (\d+)%; \}", styles))
        self.assertEqual(
            widths,
            {"repo": "19", "branch": "8", "merge": "7", "publish": "7", "rounds": "6", "prov": "12", "appr": "12", "rigor": "14", "act": "15"},
        )
        self.assertIn("const TRAIL_TONE = {\n  merged: 'ok', pass: 'ok', approved: 'ok', ready: 'ok', published: 'ok',", source)
        self.assertIn(
            "const REPO_FIELD_ORDER = ['default_branch', 'merge_policy', 'publish_policy', 'max_fix_rounds', 'design_provider', 'design_approval', 'default_rigor']",
            source,
        )

    def test_repos_editor_styles_use_theme_tokens_only(self):
        source = PLUGIN_JS.read_text(encoding="utf-8")
        start = source.index("const STYLES = `")
        styles = source[start : source.index("`", start + len("const STYLES = `"))]
        prefixes = (".fc-repos", ".fc-repo-", ".fc-tag", ".fc-ed", ".fc-field", ".fc-input", "select.fc-input", ".fc-alert", ".fc-spin", ".fc-legend", ".fc-sec-sub", ".fc-btn", ".fc-col-", ".fc-th-sub", ".fc-trunc", ".fc-num", ".fc-sr")
        rules = [line for line in styles.splitlines() if line.strip().startswith(prefixes) or line.strip().startswith("@container")]
        self.assertGreater(len(rules), 30)
        for line in rules:
            self.assertNotRegex(line, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", line)
        for needed in (".fc-tag--inherit", ".fc-tag--set", "border: 1px dashed", ".fc-ed-grid", "@container (max-width: 640px)", '.fc-input[aria-invalid="true"]', ".fc-spin", ".fc-alert"):
            self.assertIn(needed, styles)

    # --- v0.2.0 polish: default_rigor ------------------------------------------
    def test_rigor_column_inherited_vs_override_badges(self):
        r = self.report["polish"]["rigor"]
        self.assertEqual(r["sub"], "Global defaults: design provider auto · design approval ask · rigor standard")
        cells = r["cells"]
        self.assertEqual(cells["tozes-factonio"], {"cls": "fc-tag fc-tag--inherit", "text": "standard · global", "title": "Inherited from global default", "rigor": None, "glyph": False})
        self.assertEqual(cells["acme-web-dashboard"], {"cls": "fc-tag fc-tag--set", "text": "high", "title": "Repository override · multi-vendor panel", "rigor": "high", "glyph": True})
        self.assertEqual(cells["acme-cli-tools"], {"cls": "fc-tag fc-tag--set", "text": "standard", "title": "Repository override", "rigor": "standard", "glyph": False})
        # Older backends without default_rigor still render an inherited badge.
        for slug, cell in r["legacyCells"].items():
            with self.subTest(slug=slug):
                self.assertEqual(cell["cls"], "fc-tag fc-tag--inherit")
                self.assertEqual(cell["text"], "standard · global")

    def test_repo_editor_default_rigor_field_dirty_tracking_and_hints(self):
        r = self.report["polish"]["rigor"]
        f = r["opened"]
        self.assertEqual(f["label"], "Default rigor")
        self.assertEqual(f["tag"], "select")
        self.assertEqual(f["labelFor"], f["id"])
        self.assertEqual(f["describedBy"], f["hint"]["id"])
        self.assertEqual(f["value"], "")
        self.assertEqual(f["options"], [["", "Inherit global (standard)"], ["standard", "Standard"], ["high", "High (multi-vendor panel)"]])
        self.assertEqual(f["hint"]["text"], "Single cross-model diff check + black-box check. (inherited)")
        h = r["high"]
        self.assertEqual(h["field"]["value"], "high")
        self.assertEqual(h["field"]["hint"]["text"], "Multi-vendor panel: needs ≥2 verify models from distinct vendor families; fails closed on disagreement.")
        self.assertEqual(h["msg"], "1 unsaved change")
        self.assertFalse(h["save"]["disabled"])
        # Choosing the live value again drops the edit.
        self.assertEqual(r["revert"]["msg"], "")
        self.assertTrue(r["revert"]["save"]["disabled"])
        self.assertEqual(r["standardHint"]["text"], "Single cross-model diff check + black-box check.")
        self.assertEqual(r["openedHigh"]["value"], "high")
        self.assertEqual(r["inheritHint"]["text"], "Single cross-model diff check + black-box check. (inherited)")
        self.assertEqual(r["twoDirty"], "2 unsaved changes")

    def test_repo_editor_default_rigor_patch_bodies(self):
        r = self.report["polish"]["rigor"]
        self.assertEqual(r["saveHigh"]["rests"], [{"path": "/repos/tozes-factonio", "opts": {"method": "PATCH", "body": {"default_rigor": "high"}}}])
        self.assertTrue(r["saveHigh"]["closed"])
        self.assertEqual(r["clearRests"], [{"path": "/repos/acme-web-dashboard", "opts": {"method": "PATCH", "body": {"default_rigor": None}}}])
        self.assertEqual(r["twoRests"], [{"path": "/repos/acme-cli-tools", "opts": {"method": "PATCH", "body": {"merge_policy": "ask", "default_rigor": "high"}}}])
        c = r["changesHelper"]
        self.assertEqual(c["toHigh"], {"default_rigor": "high"})
        self.assertEqual(c["toInherit"], {"default_rigor": None})  # "" -> null
        self.assertEqual(c["untouched"], {})  # an edit equal to the live value is not sent; rigor untouched
        self.assertEqual(c["same"], {})

    # --- v0.2.0 polish: verification panels on /factonio ------------------------
    def test_verification_panels_kpi_card(self):
        d = self.report["polish"]["dash"]
        k = d["populated"]["kpi"]
        self.assertEqual(k["grid"], "fc-grid fc-grid--6")
        self.assertEqual(k["flag"], "disagree")
        self.assertEqual(k["flagText"], "2 disagreements")
        self.assertEqual(k["bar"]["role"], "img")
        self.assertEqual(k["bar"]["label"], "2 pass, 1 reject, 1 inconclusive, 1 pending")
        self.assertEqual(k["bar"]["segs"], [["ok", 2], ["bad", 1], ["warn", 1], ["pending", 1]])
        self.assertIn("5 panels", k["text"])
        self.assertIn("2 pass 1 reject 1 inconcl. 1 pending", k["text"])
        one = d["single"]["kpi"]
        self.assertIsNone(one["flag"])  # no amber flag without disagreements
        self.assertEqual(one["bar"]["segs"], [["ok", 3]])  # zero counts omitted
        self.assertEqual(d["errorStale"]["kpi"]["text"], "Verification Panels 1 panel 0 pass 0 reject 0 inconcl. 1 pending")
        for state in ("empty", "errorNoData", "partial"):
            with self.subTest(state=state):
                self.assertEqual(d[state]["kpi"]["text"], "Verification Panels 0 No panels yet · repos with rigor high run one")
                self.assertIsNone(d[state]["kpi"]["bar"])
        self.assertEqual(d["loading"]["kpi"]["text"], "Verification Panels — Loading")

    def test_phase_models_verify_panel_status(self):
        d = self.report["polish"]["dash"]
        ok = d["populated"]["vp"]
        self.assertEqual(ok["label"], "Verification panel and rigor status")
        self.assertEqual(ok["state"], "ready")
        self.assertEqual(ok["rigor"], {"rigor": "standard", "text": "standard"})
        self.assertEqual(ok["ready"], {"tone": "ok", "text": "Ready · 2 vendors"})
        self.assertEqual(ok["src"], "settings.verify_panel")
        self.assertEqual(ok["members"], [
            {"idx": "1", "label": "openai/gpt-5.4", "vendor": "openai", "dup": "false"},
            {"idx": "2", "label": "google/gemini-3-pro", "vendor": "google", "dup": "false"},
        ])
        self.assertIsNone(ok["warn"])
        bad = d["single"]["vp"]
        self.assertEqual(bad["state"], "incomplete")
        self.assertEqual(bad["rigor"], {"rigor": "high", "text": "high"})
        self.assertEqual(bad["ready"], {"tone": "warn", "text": "Incomplete"})
        self.assertEqual([m["dup"] for m in bad["members"]], ["true", "true", "true"])
        self.assertEqual(bad["members"][2]["vendor"], "unknown")
        self.assertEqual(bad["warn"]["role"], "status")
        self.assertIn("collapses to a single vendor family", bad["warn"]["text"])
        self.assertEqual(bad["warn"]["advisories"], ["Ignored 1 malformed panel entry."])
        none = d["empty"]["vp"]
        self.assertEqual(none["ready"], {"tone": "muted", "text": "Unconfigured"})
        self.assertIsNone(none["src"])  # "unset" source is not shown
        self.assertIn("No panel members", none["empty"])
        self.assertIn("no verify_panel is configured", none["warn"]["text"])
        lo = d["loading"]["vp"]
        self.assertEqual((lo["busy"], lo["skel"], lo["state"]), ("true", True, "loading"))
        # Query error or an older backend without verify_panel renders nothing (ModelsQueryStatus covers it).
        self.assertIsNone(d["errorNoData"]["vp"])
        self.assertIsNone(d["errorStale"]["vp"])
        self.assertIsNone(d["partial"]["vp"])

    # --- v0.2.0 polish: /metrics/history trend strip --------------------------
    def test_trend_strip_query_and_placement(self):
        d = self.report["polish"]["dash"]
        self.assertEqual(d["historyQuery"], {"key": ["factonio", "metrics-history", "30d"], "interval": 30000})
        self.assertEqual(d["historyRest"], "/metrics/history?since=30d&limit=100")
        self.assertEqual(d["populated"]["order"], ["Trends", "Phase Durations", "Decision History", "Phase Model Configuration", "Registered Repositories"])

    def test_trend_strip_populated_inline_svg(self):
        t = self.report["polish"]["dash"]["populated"]["trend"]
        self.assertEqual((t["state"], t["labelledBy"], t["heading"]), ("populated", "fc-trend-h", "Trends"))
        self.assertIsNone(t["busy"])
        self.assertIsNone(t["alert"])
        self.assertRegex(t["sub"], r"^14 snapshots · \w+ \d+ – \w+ \d+$")
        self.assertEqual(t["srcs"], ["merge 9", "retro 2", "panel_consensus 3"])
        cards = {c["metric"]: c for c in t["cards"]}
        self.assertEqual(list(cards), ["pass", "escape", "throughput"])
        p = cards["pass"]
        self.assertEqual(p["title"], "Verification Pass Rate")
        self.assertEqual(p["value"], "88% BB 82%")
        self.assertEqual(p["delta"], {"tone": "ok", "text": "+10 pts", "arrow": True})
        self.assertEqual(p["svg"]["viewBox"], "0 0 240 56")
        self.assertEqual(p["svg"]["par"], "none")
        self.assertEqual(p["svg"]["hidden"], "true")
        self.assertEqual(p["svg"]["hover"], "hidden")
        self.assertEqual([c for c, _ in p["svg"]["paths"]], ["fc-spark-line fc-spark-line--2", "fc-spark-line"])
        self.assertTrue(p["svg"]["paths"][1][1].startswith("M4,30.2 L21.8,28 "))
        self.assertTrue(p["svg"]["paths"][1][1].endswith(" L236,19.2"))
        self.assertEqual(p["svg"]["circles"], [["fc-spark-end", 236, 19.2, 2.75]])
        self.assertEqual(p["svg"]["lines"][0], ["fc-spark-base", 50, None])  # baseline at H - PB
        self.assertEqual(p["plot"], {"tabIndex": 0, "role": "group", "label": "Verification Pass Rate: 14 snapshots, latest 88%. Use left and right arrow keys to read each snapshot."})
        self.assertEqual(p["legend"], "Diff check Black-box")
        self.assertEqual(p["read"]["live"], "polite")
        self.assertRegex(p["read"]["text"], r"^Latest · \w+ \d+ merge Diff 88% BB 82%$")
        e = cards["escape"]
        self.assertEqual(e["delta"], {"tone": "ok", "text": "−7 pts", "arrow": True})  # escape going down is good
        self.assertEqual([c for c, _ in e["svg"]["paths"]], ["fc-spark-area", "fc-spark-line"])
        self.assertTrue(e["svg"]["paths"][0][1].endswith(" L236,50 L4,50 Z"))
        w = cards["throughput"]
        self.assertEqual(w["value"], "12 61 cards done")
        self.assertEqual(w["delta"], {"tone": "ok", "text": "+8", "arrow": True})
        self.assertEqual(w["legend"], "Workstreams Cards done")

    def test_trend_strip_keyboard_pointer_readout(self):
        k = self.report["polish"]["dash"]["keys"]
        self.assertTrue(k["homePrevented"])
        self.assertEqual(k["home"]["svg"]["hover"], "visible")
        self.assertIn(["fc-spark-hot", 4, 30.2, 3.25], k["home"]["svg"]["circles"])
        self.assertRegex(k["home"]["read"]["text"], r"^\w+ \d+ merge Diff 78% BB 70%$")
        self.assertRegex(k["right"], r"^\w+ \d+ merge Diff 80% BB 72%$")
        self.assertRegex(k["leftClamped"], r"Diff 78% BB 70%$")
        self.assertRegex(k["end"], r"^Latest · .* Diff 88% BB 82%$")
        self.assertFalse(k["otherKeyPrevented"])
        self.assertEqual((k["focusEscape"], k["blurEscape"]), ("visible", "hidden"))
        self.assertRegex(k["pointer"], r"WS 4 Cards 12$")
        self.assertRegex(k["pointerLeave"], r"^Latest · .* WS 12 Cards 61$")

    def test_trend_strip_single_snapshot(self):
        t = self.report["polish"]["dash"]["single"]["trend"]
        self.assertRegex(t["sub"], r"^1 snapshot · \w+ \d+$")
        for c in t["cards"]:
            with self.subTest(metric=c["metric"]):
                self.assertEqual(c["delta"], {"tone": "flat", "text": "first snapshot", "arrow": False})
                self.assertEqual(c["svg"]["paths"], [])  # never a 1-point path
                self.assertEqual(len(c["svg"]["circles"]), 1)
                cls, cx, cy, r = c["svg"]["circles"][0]
                self.assertEqual((cls, cx, r), ("fc-spark-end", 120, 3.5))
                self.assertEqual(c["svg"]["lines"], [["fc-spark-base", cy, "2 3"]])

    def test_trend_strip_empty_loading_and_error_states(self):
        d = self.report["polish"]["dash"]
        e = d["empty"]["trend"]
        self.assertEqual(e["state"], "empty")
        self.assertEqual(e["empty"]["title"], "Snapshots appear automatically as PRs merge")
        self.assertEqual(e["empty"]["cmd"], "hermes factonio metrics --snapshot")
        self.assertEqual(e["cards"], [])
        lo = d["loading"]["trend"]
        self.assertEqual((lo["state"], lo["busy"], lo["sub"], lo["skeletons"]), ("loading", "true", "Loading snapshots…", 3))
        self.assertEqual(lo["cards"], [])
        er = d["errorNoData"]["trend"]
        self.assertEqual(er["state"], "error")
        self.assertEqual(er["alert"], {"text": "Couldn’t refresh snapshots (HTTP 503). The history endpoint could not be loaded. Retry", "retry": True})
        self.assertEqual(er["cards"], [])
        self.assertEqual(d["retry"], ["h"])
        st = d["errorStale"]["trend"]
        self.assertEqual((st["state"], st["stale"]), ("populated", "true"))
        self.assertEqual(st["alert"]["text"], "Couldn’t refresh snapshots. Showing data from 4 min ago. Retry")
        self.assertEqual(len(st["cards"]), 3)

    def test_trend_strip_partial_null_kpis_do_not_crash(self):
        d = self.report["polish"]["dash"]
        self.assertNotIn("partialError", d)
        t = d["partial"]["trend"]
        self.assertEqual(t["state"], "populated")
        self.assertRegex(t["sub"], r"^3 snapshots · ")
        cards = {c["metric"]: c for c in t["cards"]}
        self.assertEqual(cards["pass"]["value"], "— BB 50%")
        self.assertEqual(cards["escape"]["value"], "—")
        self.assertEqual(cards["throughput"]["value"], "3 — cards done")
        for c in t["cards"]:
            with self.subTest(metric=c["metric"]):
                self.assertEqual(c["delta"]["tone"], "flat")
                for _, path in c["svg"]["paths"]:
                    self.assertNotIn("NaN", path)
                    self.assertNotIn("undefined", path)
        # Snapshots are re-sorted oldest -> newest before plotting.
        self.assertRegex(cards["pass"]["read"]["text"], r"^Latest · \w+ 20 retro")

    def test_sparkline_math_and_verdict_tone_helpers(self):
        p = self.report["polish"]
        self.assertEqual(p["verdictTone"], {"pass": "pass", "reject": "reject", "fail": "reject", "inconclusive": "warn", "INCONCLUSIVE": "warn", "weird": None, "null": None})
        sp = p["spark"]
        self.assertEqual(sp["one"], [[120, 28]])  # n=1: centred x, y(v) mid-plot
        pts = sp["fourteen"]
        self.assertEqual(len(pts), 14)
        self.assertEqual(pts[0], [4, 30.2])
        self.assertEqual(pts[-1], [236, 19.2])
        xs = [x for x, _ in pts]
        self.assertEqual(xs, sorted(xs))
        self.assertTrue(all(6 <= y <= 50 for _, y in pts))
        self.assertEqual(sp["flat"], [[4, 50], [236, 50]])  # hi == lo never divides by zero
        self.assertEqual(sp["gapPath"], "M4,45.6 M158.7,36.8 L236,32.4")  # a missing value breaks the line
        self.assertTrue(sp["path14"].startswith("M4,30.2 L"))
        self.assertEqual(sp["path14"].count(" L"), 13)

    # --- v0.2.0 polish: sidebar verification panels ----------------------------
    def test_compute_workstream_panels_fail_closed_consensus(self):
        pn = self.report["polish"]["panels"]
        self.assertEqual(pn["passNoTrail"][0]["consensus"], "pending")  # a pass needs the recorded consensus
        self.assertEqual(pn["passRecorded"][0]["consensus"], "pass")
        self.assertEqual(pn["passRecorded"][0]["vendors"], ["openai", "google"])
        rj = pn["rejectDisagree"][0]
        self.assertEqual((rj["consensus"], rj["hasDisagreement"]), ("reject", True))
        self.assertEqual(rj["dissent"], {"lead": "google reject vs openai pass", "quote": "migration drops idx_cards_tenant without a backfill"})
        inc = pn["inconclusive"][0]
        self.assertEqual((inc["consensus"], inc["hasDisagreement"]), ("inconclusive", True))
        self.assertEqual(inc["dissent"]["lead"], "google inconclusive vs openai pass")
        pe = pn["pending"][0]
        self.assertEqual((pe["consensus"], pe["hasDisagreement"], pe["decided"], len(pe["lanes"])), ("pending", False, 1, 2))
        rr = pn["pendingRecordedReject"][0]
        self.assertEqual((rr["consensus"], rr["recorded"], rr["hasDisagreement"]), ("reject", "reject", True))
        self.assertEqual(rr["dissent"], {"lead": "Panel lanes disagree -- pass: verify-diff/openai; reject: verify-diff/google.", "quote": None})
        # Degenerate single-vendor panel stays inconclusive even with a recorded pass.
        self.assertEqual(pn["singleVendor"][0]["consensus"], "inconclusive")
        self.assertEqual(pn["supersededIgnored"][0]["consensus"], "pending")
        self.assertEqual(pn["newestFirst"], ["vp_bb", "vp_aa"])
        self.assertEqual(pn["none"], [])
        self.assertEqual(pn["nullInputs"], [])

    def test_sidebar_panel_strip_badges_and_disagreement_callout(self):
        sb = self.report["polish"]["sidebar"]
        dis = sb["dis"]
        self.assertEqual(dis["bodyOrder"], ["fc-sb-pipe", "fc-sb-panel", "fc-sb-ws-cards"])
        self.assertEqual(dis["role"], "group")
        self.assertEqual(dis["label"], "Verification panel vp_9a4c1e: reject, 3 of 3 lanes, lanes disagree")
        self.assertEqual((dis["consensus"], dis["disagree"]), ("reject", "true"))
        self.assertEqual(dis["tag"], {"tone": "reject", "text": "reject"})
        self.assertEqual((dis["id"], dis["lanes"]), ("vp_9a4c1e", "3/3 lanes"))
        self.assertEqual([v[:2] for v in dis["vendors"]], [["reject", "google"], ["pass", "openai"], ["inconclusive", "anthropic"]])
        self.assertEqual(dis["vendors"][0][2], "diff · google/gemini-3-pro: reject")
        co = dis["callout"]
        self.assertEqual((co["tone"], co["kind"], co["role"]), ("warn", "disagree", "note"))
        self.assertTrue(co["quote"])
        self.assertIn("google reject vs openai pass, anthropic inconclusive", co["text"])
        self.assertIn("migration drops idx_cards_tenant", co["text"])
        self.assertEqual(co["title"], "google reject vs openai pass, anthropic inconclusive: “migration drops idx_cards_tenant without a backfill; rollback leaves orphaned rows”")
        pend = sb["pend"]
        self.assertEqual(pend["tag"], {"tone": "info", "text": "pending"})
        self.assertEqual(pend["lanes"], "1/2 lanes")
        self.assertEqual([v[0] for v in pend["vendors"]], ["pass", "pending"])
        self.assertIsNone(pend["callout"])
        rec = sb["rec"]
        self.assertEqual(rec["tag"], {"tone": "pass", "text": "pass"})
        # Mixed diff + black-box lanes name the phase on each chip.
        self.assertEqual([v[1] for v in rec["vendors"]], ["openai · diff", "google · diff", "openai · black-box", "google · black-box"])
        # While /trail is still loading a pass is never claimed from /cards alone.
        self.assertEqual(sb["trailLoading"]["rec"]["consensus"], "pending")
        stages = {s["tenant"]: s["stages"][3] for s in sb["sections"]}
        self.assertEqual(stages["omnim-ai-factonio-tenant-card-index"], "Verify: diff rejected, black-box pending")
        # No new polling endpoints: the pane only queries /cards and /trail.
        self.assertEqual(sb["paneQueries"]["cards"], ["factonio/cards/all"])
        self.assertEqual(sb["paneQueries"]["trail"], ["factonio/trail/200"])
        self.assertEqual(sb["paneQueries"]["other"], [])

    def test_sidebar_card_vendor_badges_and_inconclusive_tone(self):
        c = self.report["polish"]["sidebar"]["cards"]
        self.assertEqual(c["t_8f2a"]["vendor"], {"text": "google", "title": "Panel lane vendor"})
        self.assertEqual((c["t_8f2a"]["attn"], c["t_8f2a"]["dot"], c["t_8f2a"]["verdictTone"]), ("true", "reject", "reject"))
        self.assertEqual((c["t_8f2b"]["attn"], c["t_8f2b"]["verdictTone"]), ("false", "pass"))
        inc = c["t_8f2c"]
        self.assertEqual((inc["attn"], inc["dot"], inc["dotLabel"], inc["verdictTone"]), ("warn", "inconclusive", "Status: inconclusive", "warn"))
        self.assertIsNone(c["t_imp"]["vendor"])
        self.assertEqual(c["t_w3"]["dot"], "running")
        sb = self.report["polish"]["sidebar"]
        # inconclusive counts as attention alongside the unresolved reject
        self.assertEqual(sb["header"]["attention"]["n"], "2")
        tri = {t["verdict"]: t for t in self.report["triState"]["verdictTags"]}
        self.assertEqual(tri["inconclusive"]["tone"], "warn")

    def test_card_drawer_panel_rows(self):
        sb = self.report["polish"]["sidebar"]
        d = sb["drawer"]
        self.assertEqual(d["order"][:4], ["model", "panel", "vendor", "peers"])
        self.assertEqual(d["labels"][:4], ["Model", "Panel", "Vendor", "Peers"])
        self.assertTrue(all(len(lbl) <= 7 for lbl in d["labels"][1:4]))
        self.assertEqual(d["fields"]["panel"], "vp_9a4c1e reject")
        self.assertEqual(d["panelTone"], "reject")
        self.assertEqual(d["fields"]["vendor"], "google lane 1/3")
        self.assertEqual(d["fields"]["peers"], "openai anthropic")
        self.assertEqual(d["peers"], ["pass", "inconclusive"])
        for field in ("panel", "vendor", "peers"):
            self.assertNotIn(field, sb["nonPanelDrawer"])
        self.assertNotIn("missing", d["fields"])

    # --- fix round 1: expected-lane membership fails closed (mirrors summarize_panel) ---
    def test_compute_workstream_panels_missing_expected_lanes_from_recorded_consensus(self):
        ms = self.report["polish"]["panels"]["missing"]
        for key in ("recordedLanes", "recordedStringLanes"):
            p = ms[key][0]
            self.assertEqual(p["consensus"], "inconclusive", key)  # never the stale recorded pass
            self.assertEqual(p["missingLaneIds"], ["t_2", "t_3"], key)
            self.assertEqual(p["expectedLaneCount"], 4, key)
            self.assertEqual(p["reason"], "2 expected lane(s) missing: t_2, t_3", key)
            self.assertEqual(len(p["lanes"]), 2, key)
        cnt = ms["countOnly"][0]
        self.assertEqual((cnt["consensus"], cnt["missingLaneIds"], cnt["expectedLaneCount"]), ("inconclusive", [], 4))
        self.assertEqual(cnt["reason"], "2 expected lane(s) missing")

    def test_compute_workstream_panels_missing_expected_lanes_from_spawn_verify(self):
        ms = self.report["polish"]["panels"]["missing"]
        for key in ("spawned", "spawnedNoConsensus"):
            p = ms[key][0]
            self.assertEqual(p["consensus"], "inconclusive", key)
            self.assertEqual((p["missingLaneIds"], p["expectedLaneCount"]), (["t_2", "t_3"], 4), key)

    def test_compute_workstream_panels_reject_beats_missing_lanes(self):
        ms = self.report["polish"]["panels"]["missing"]
        for key in ("rejectWins", "recordedRejectWins"):
            p = ms[key][0]
            self.assertEqual(p["consensus"], "reject", key)
            self.assertEqual(p["missingLaneIds"], ["t_2", "t_3"], key)
            self.assertIsNone(p["reason"], key)

    def test_compute_workstream_panels_full_and_grown_lane_sets(self):
        ms = self.report["polish"]["panels"]["missing"]
        full = ms["full"][0]
        self.assertEqual((full["consensus"], full["missingLaneIds"], full["expectedLaneCount"], full["reason"]), ("pass", [], 4, None))
        # A recorded pass covers only the lanes it evaluated; a new lane waits for re-evaluation.
        self.assertEqual(ms["staleSubsetPass"][0]["consensus"], "pending")
        self.assertIsNone(self.report["polish"]["panels"]["passRecorded"][0]["expectedLaneCount"])

    def test_sidebar_panel_strip_and_drawer_surface_missing_lanes(self):
        sb = self.report["polish"]["sidebar"]
        miss = sb["miss"]
        self.assertEqual(miss["consensus"], "inconclusive")
        self.assertEqual(miss["tag"], {"tone": "warn", "text": "inconclusive"})
        self.assertEqual(miss["lanes"], "2/4 lanes")
        self.assertEqual(miss["label"], "Verification panel vp_5e0d12: inconclusive, 2 of 4 lanes, 2 expected lane(s) missing: t_m2, t_m3")
        co = miss["callout"]
        self.assertEqual((co["tone"], co["kind"], co["role"]), ("warn", "missing", "note"))
        self.assertEqual(co["text"], "missing 2 expected lane(s) missing: t_m2, t_m3")
        self.assertEqual(co["title"], "2 expected lane(s) missing: t_m2, t_m3; the panel cannot pass without every lane")
        self.assertEqual(sb["missDrawer"]["panel"], "vp_5e0d12 inconclusive")
        self.assertEqual(sb["missDrawer"]["missing"], "2 expected lane(s) missing: t_m2, t_m3")
        self.assertIsNone(sb["rec"]["callout"])
        self.assertEqual(sb["rec"]["lanes"], "4/4 lanes")

    def test_polish_styles_use_theme_tokens_only(self):
        source = PLUGIN_JS.read_text(encoding="utf-8")
        start = source.index("const STYLES = `")
        styles = source[start : source.index("`", start + len("const STYLES = `"))]
        block = styles[styles.index("/* v0.2.0 polish") :]
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
        # The sidebar's warn tone now resolves through the shared host-token alias.
        self.assertIn("--fc-sb-warn: var(--fc-warn);", styles)
        self.assertNotIn("#f59e0b", styles)
        for needed in (".fc-grid--6", ".fc-kpi-bar", ".fc-trend-grid", ".fc-spark-line", "vector-effect: non-scaling-stroke", ".fc-vp-warn",
                       ".fc-col-rigor { width: 14%; }", ".fc-sb-panel", '.fc-sb-dot[data-status="inconclusive"]', '.fc-sb-card[data-attn="warn"]',
                       "@container (max-width: 700px)", ".fc-trend-skel span, .fc-vp-skel { animation: none !important; }"):
            self.assertIn(needed, styles)
        cols = re.findall(r"\.fc-col-[a-z]+ \{ width: (\d+)%; \}", styles)
        self.assertEqual(sum(int(c) for c in cols), 100)
        for imp in re.findall(r"^import .* from '([^']+)'", source, re.M):
            self.assertIn(imp, ("@hermes/plugin-sdk", "react", "react/jsx-runtime"))

    # --- Wave 5: workstream modes, /arena, 12-phase trail, harness badge ---------
    def test_workstream_mode_resolution_and_fallbacks(self):
        m = self.report["wave5"]["mode"]
        self.assertEqual(m["perfApi"], {"kind": "perf", "readOnly": False, "reportName": None, "reportExists": False})
        # report_path renders as its basename only (POSIX or Windows separators).
        self.assertEqual(m["investigateReady"], {"kind": "investigate", "readOnly": True, "reportName": "investigation.md", "reportExists": True})
        self.assertEqual(m["investigateWinPath"]["reportName"], "report.md")
        self.assertIsNone(m["investigateHoldoutPath"]["reportName"])
        # Only investigate workstreams expose a report.
        self.assertEqual((m["refactorWithPath"]["reportName"], m["refactorWithPath"]["reportExists"]), (None, False))
        # Fallbacks: API kind null -> newest card workstream_kind -> newest start_workstream row.
        self.assertEqual(m["legacyNull"]["kind"], "refactor")
        self.assertEqual(m["cardFallback"]["kind"], "perf")
        self.assertEqual((m["trailFallback"]["kind"], m["trailFallback"]["readOnly"]), ("investigate", True))
        for key in ("trailOtherTenant", "unknownKind", "none", "emptyTenant"):
            self.assertEqual(m[key], {"kind": None, "readOnly": False, "reportName": None, "reportExists": False}, key)

    def test_compute_workstream_arenas_api_legacy_and_sanitized(self):
        a = self.report["wave5"]["arenas"]
        self.assertEqual([x["arena_id"] for x in a["api"]], ["ar_new", "ar_old"])  # tenant-scoped, API order kept
        self.assertFalse(a["api"][0]["legacy"])
        # An explicit (even empty) API list wins over card tags.
        self.assertEqual(a["apiEmptyIgnoresTags"], [])
        self.assertEqual(a["noTenant"], [])
        san = a["sanitized"]
        self.assertEqual([x["arena_id"] for x in san], ["ar_7f2c9a10"])  # holdout id and junk entries dropped
        self.assertIsNone(san[0]["title"])
        self.assertEqual(san[0]["advisories"], ["Judge waiting on c2-openai"])
        self.assertEqual(san[0]["candidates"], [{"slot": "c1", "vendor": "anthropic", "model": None, "task_id": None, "status": "done", "artifact_ready": None}])
        self.assertEqual(san[0]["judge"]["cross_vendor"], None)  # "true" string is not a bool
        self.assertIsNone(san[0]["judge"]["vendor"])
        self.assertNotIn("holdout", json.dumps(san).lower())
        leg = a["legacy"]
        self.assertEqual(len(leg), 1)
        self.assertEqual((leg[0]["arena_id"], leg[0]["target"], leg[0]["status"], leg[0]["legacy"]), ("ar_legacy01", "define", None, True))
        self.assertEqual([(c["slot"], c["vendor"], c["status"]) for c in leg[0]["candidates"]], [("c1-anthropic", "anthropic", "done"), ("c2-openai", "openai", "running")])
        self.assertEqual((leg[0]["judge"]["task_id"], leg[0]["judge"]["vendor"], leg[0]["judge"]["cross_vendor"]), ("t_j", "google", None))

    def test_evidence_pills_wave5_decisions(self):
        p = self.report["wave5"]["pills"]

        def brief(ps):
            return [(x["key"], x["label"], x["text"], x["tone"]) for x in ps]

        self.assertEqual(brief(p["start"])[:2], [("kind", "mode", "investigate", "info"), ("read_only", None, "read-only", "warn")])
        self.assertEqual(brief(p["startRw"]), [("kind", "mode", "perf", "info")])  # read_only false -> no pill
        self.assertEqual(brief(p["spawnArena"])[:3], [("arena_id", "arena", "ar_7f2c9a10", None), ("target", "target", "define", None), ("candidates", "candidates", "anthropic · openai", None)])
        self.assertEqual(brief(p["arenaMany"]), [("arena_id", "arena", "ar_1", None), ("candidates", "candidates", "4", None), ("judge_vendor", "judge", "google", None)])
        tri = brief(p["triage"])
        self.assertEqual(tri[:3], [("stop_the_line", None, "stop the line 1", "bad"), ("actionable", None, "actionable 1", "info"), ("issue_numbers", "issues", "3", None)])
        self.assertNotIn("counts", [x[0] for x in tri])
        vf = {x["key"]: x for x in p["verifyFix"]}
        self.assertEqual(vf["issue_number"]["href"], "https://github.com/omnim-ai/factonio/issues/412")
        self.assertEqual(vf["issue_number"]["text"], "#412")
        self.assertEqual(vf["verdict"]["tone"], "ok")
        self.assertEqual(vf["pr_number"]["href"], "https://github.com/omnim-ai/factonio/pull/44")
        no_gh = {x["key"]: x for x in p["verifyFixNoGh"]}
        self.assertEqual((no_gh["issue_number"]["href"], no_gh["issue_number"]["label"], no_gh["issue_number"]["text"]), (None, "issue", "#412"))
        self.assertEqual(no_gh["verdict"]["tone"], "bad")
        self.assertEqual(brief(p["skillEval"])[:4], [("winner_label", "winner", "define-v3", "ok"), ("disqualified", "disqualified", "A", "bad"), ("eval_phase", "eval phase", "define", None), ("decided_by", "decided by", "judge", None)])
        self.assertNotIn("verdict", [x["key"] for x in p["skillEval"]])  # duplicate of winner
        self.assertEqual(brief(p["noWinner"]), [("disqualified", "disqualified", "A, B", "bad")])
        self.assertEqual(brief(p["tie"]), [("winner", "winner", "tie", "warn")])
        # Keys outside their decision family still render through the generic loop.
        self.assertEqual(brief(p["otherKind"]), [("kind", "kind", "diff", None), ("verdict", "verdict", "pass", None)])

    def test_evidence_pills_drop_every_holdout_reference(self):
        p = self.report["wave5"]["pills"]
        # Keys, values, arrays, nested objects, fullwidth, ANSI-split, zero-width and control-char spellings.
        self.assertEqual(p["holdout"], [])
        self.assertEqual(p["holdoutKeyOnly"], [])
        self.assertEqual(p["holdoutValueOnly"], [])

    def test_summarize_harness_badge_states(self):
        h = self.report["wave5"]["harness"]
        self.assertEqual((h["full"]["state"], h["full"]["key"], h["full"]["text"]), ("ok", "harness", "5 hooks · 1 frozen"))
        self.assertEqual(h["full"]["title"].split("\n"), ["Harness · full mode", "4 required + 1 optional hooks", "2 invariant · 2 visual_diff (1 frozen baseline)", "Read from .factonio/harness.json"])
        self.assertEqual(h["oneHook"]["text"], "1 hook")
        self.assertEqual((h["mdOnly"]["state"], h["mdOnly"]["text"]), ("ok", "md only"))
        self.assertEqual((h["error"]["state"], h["error"]["text"]), ("error", "error"))  # error wins over present
        self.assertIn("Run: hermes factonio harness check", h["error"]["title"])
        self.assertEqual((h["absent"]["state"], h["absent"]["key"], h["absent"]["text"]), ("absent", None, "no harness"))
        self.assertEqual(h["garbageCounts"]["text"], "0 hooks")
        self.assertIn("Harness · unknown mode", h["garbageCounts"]["title"])
        self.assertIsNone(h["legacy"])
        self.assertIsNone(h["nullish"])
        for state in ("full", "mdOnly", "error", "absent"):
            self.assertTrue(h[state]["sr"], state)

    def test_sidebar_workstream_mode_badges_and_report_indicator(self):
        sb = self.report["wave5"]["sidebar"]
        self.assertNotIn("error", sb)
        s = sb["sections"]
        perf = s["omnim-ai-factonio-wave-5-desktop-ui"]
        self.assertEqual(perf["labelRow"], ["fc-sb-ws-label", "fc-sb-mode"])
        self.assertEqual(perf["mode"], {"kind": "perf", "text": "perf", "title": "Performance mode: benchmark checklist contract", "icon": "trend", "ro": False})
        self.assertEqual(perf["label"], "wave-5-desktop-ui workstream, active, perf mode")
        ref = s["omnim-ai-factonio-split-orchestrator"]
        self.assertEqual((ref["mode"]["text"], ref["mode"]["icon"]), ("refactor", "retry"))
        inv = s["omnim-ai-factonio-cache-miss-spike"]
        self.assertEqual((inv["mode"]["text"], inv["mode"]["icon"], inv["mode"]["ro"]), ("investigate · read-only", "eye", True))
        self.assertEqual(inv["report"], {"ready": "false", "text": "report pending", "title": "investigation.md not written yet", "icon": "queued"})
        self.assertTrue(inv["meta"].endswith("· report pending"))
        self.assertEqual(inv["label"], "cache-miss-spike workstream, active, investigate mode, read-only, report pending")
        ready = sb["reportReady"]
        # A ready report is a button opening the inline viewer (doc icon + chevron).
        self.assertEqual((ready["report"]["ready"], ready["report"]["text"], ready["report"]["icon"]), ("true", "report ready", "doc"))
        self.assertEqual(ready["report"]["title"], "Open investigation.md (read-only)")
        # bug / feature / quick / legacy stay unbadged.
        bug = s["omnim-ai-factonio-fix-login"]
        self.assertEqual((bug["mode"], bug["report"], bug["dataMode"], bug["labelRow"]), (None, None, None, ["fc-sb-ws-label"]))
        self.assertEqual(bug["label"], "fix-login workstream, active")
        plain = sb["plain"]["section"]
        self.assertEqual((plain["mode"], plain["arena"], plain["label"]), (None, None, "acme-api-old workstream, active"))
        # No new polling: the pane still only queries /cards and /trail.
        self.assertEqual(sb["queries"], {"cards": ["factonio/cards/all"], "trail": ["factonio/trail/200"], "other": []})

    def test_sidebar_arena_strip_anatomy_and_placement(self):
        perf = self.report["wave5"]["sidebar"]["sections"]["omnim-ai-factonio-wave-5-desktop-ui"]
        self.assertEqual(perf["bodyOrder"], ["fc-sb-pipe", "fc-sb-arena", "fc-sb-panel", "fc-sb-ws-cards"])
        a = perf["arena"]
        self.assertEqual(a["role"], "group")
        self.assertEqual(a["label"], "Arena ar_7f2c9a10 on design: judging. 1 of 2 candidate artifacts ready. Judge google running, cross-vendor.")
        self.assertEqual(a["topOrder"], ["fc-sb-arena-k", "fc-sb-arena-target", "fc-sb-tag", "fc-sb-panel-id fc-sb-mono", "fc-sb-panel-lanes", "fc-sb-btn fc-sb-reeval"])
        self.assertEqual(a["lead"], {"title": "Arena · Sidebar layout", "word": "arena", "icon": "arena"})
        self.assertEqual((a["target"], a["tag"]["text"], a["tag"]["tone"], a["id"]), ("design", "judging", "info", "ar_7f2c9a10"))
        self.assertEqual(a["count"], {"text": "1/2 ready", "title": "1 of 2 candidate artifacts ready"})
        self.assertEqual([(c["text"], c["run"], c["icon"]) for c in a["chips"]], [("anthropic", "ready", "check"), ("openai", "no-artifact", "attention"), ("judge · google", "running", "spin")])
        self.assertEqual((a["chips"][2]["role"], a["chips"][2]["cross"]), ("judge", "true"))
        self.assertEqual(a["chips"][0]["title"], "c1-anthropic · anthropic/m: done, artifact ready")
        self.assertIsNone(a["callout"])

    def test_sidebar_arena_strip_all_statuses_and_callouts(self):
        st = self.report["wave5"]["sidebar"]["statuses"]
        expected = {
            "candidates_in_progress": ("in progress", "info", None),
            "ready_for_synthesis": ("ready", "info", None),
            "judging": ("judging", "info", ("advisory", "warn")),
            "synthesized": ("synthesized", "pass", None),
            "needs_attention": ("attention", "warn", ("advisory", "warn")),
            "degraded": ("degraded", "warn", ("degraded", "warn")),
            "blocked": ("blocked", "reject", ("blocked", None)),
        }
        self.assertEqual(set(st), set(expected))
        for status, (label, tone, callout) in expected.items():
            a = st[status]
            self.assertEqual((a["tag"]["text"], a["tag"]["tone"], a["tag"]["title"]), (label, tone, f"Status: {status}"), status)
            self.assertEqual(a["status"], status)
            self.assertEqual((a["callout"]["kind"], a["callout"]["tone"]) if a["callout"] else None, callout, status)
            if a["callout"]:
                self.assertEqual(a["callout"]["role"], "note")
        self.assertIn("Judge waiting on canonical artifact", st["judging"]["callout"]["text"])
        self.assertIn("A candidate needs attention before the judge can run.", st["needs_attention"]["callout"]["text"])
        self.assertIn("The arena cannot reach a judge.", st["blocked"]["callout"]["text"])
        # A same-vendor judge never relies on hue alone: attention glyph + data-cross="false".
        judge = st["degraded"]["chips"][-1]
        self.assertEqual((judge["cross"], judge["icon"], judge["text"]), ("false", "attention", "judge · openai"))
        self.assertIn("same vendor as a candidate", st["degraded"]["label"])
        self.assertEqual([(c["run"], c["icon"]) for c in st["needs_attention"]["chips"][:2]], [("no-artifact", "attention"), ("failed", "x")])
        self.assertEqual([(c["run"], c["icon"]) for c in st["candidates_in_progress"]["chips"]], [("running", "spin"), (None, "queued"), (None, "pending")])
        self.assertTrue(st["blocked"]["label"].endswith("No judge yet."))

    def test_sidebar_arena_card_tags_and_drawer_rows(self):
        sb = self.report["wave5"]["sidebar"]
        cards = sb["sections"]["omnim-ai-factonio-wave-5-desktop-ui"]["cards"]
        self.assertEqual(cards["t_p_c1"]["arenaTag"], {"text": "arena:anthropic", "title": "Arena candidate c1-anthropic · ar_7f2c9a10", "role": "candidate"})
        self.assertEqual(cards["t_p_j"]["arenaTag"], {"text": "arena:judge", "title": "Arena judge · ar_7f2c9a10", "role": "judge"})
        self.assertEqual(cards["t_p_c1"]["topOrder"], ["fc-sb-dot", "fc-sb-tag", "fc-sb-arena-tag", "fc-sb-dur"])
        # The arena tag wins over a panel chip on the same card; plain panel lanes keep theirs.
        self.assertEqual((cards["t_p_c2"]["arenaTag"]["text"], cards["t_p_c2"]["panelChip"]), ("arena:openai", False))
        self.assertEqual((cards["t_p_v1"]["arenaTag"], cards["t_p_v1"]["panelChip"]), (None, True))
        d = sb["drawers"]["t_p_c1"]
        self.assertEqual(d["order"][:6], ["mode", "model", "arena", "role", "candidates", "judge"])
        self.assertEqual(d["labels"][:6], ["Mode", "Model", "Arena", "Role", "Candidates", "Judge"])
        self.assertEqual(d["fields"]["arena"], "ar_7f2c9a10 design judging")
        self.assertEqual(d["tags"]["arena"], [{"text": "judging", "tone": "info"}])
        self.assertEqual(d["fields"]["role"], "candidate · c1-anthropic")
        self.assertEqual([c["text"] for c in d["chips"]["candidates"]], ["anthropic", "openai"])
        self.assertEqual(d["chips"]["judge"], [{"text": "judge · google", "run": "running", "cross": "true", "icon": "spin"}])
        self.assertEqual(sb["drawers"]["t_p_j"]["fields"]["role"], "judge")
        # Panel lanes keep their panel rows after Mode/Model; no arena rows.
        self.assertEqual(sb["drawers"]["t_p_v1"]["order"][:4], ["mode", "model", "panel", "vendor"])

    def test_sidebar_card_drawer_mode_and_report_rows(self):
        dr = self.report["wave5"]["sidebar"]["drawers"]
        inv = dr["t_i_def"]
        self.assertEqual(inv["order"][:3], ["mode", "report", "model"])
        self.assertEqual(inv["modePill"], {"kind": "investigate", "ro": False})  # no suffix in the drawer pill
        self.assertEqual(inv["tags"]["mode"], [{"text": "read-only", "tone": "warn"}])
        self.assertEqual(inv["tags"]["report"], [{"text": "pending", "tone": None}])
        self.assertEqual(inv["fields"]["report"], "pending investigation.md")  # basename only, never a directory
        self.assertEqual(inv["reportTitle"], "investigation.md")
        self.assertEqual(dr["t_p_c1"]["modePill"], {"kind": "perf", "ro": False})
        bug = dr["t_b_imp"]
        self.assertEqual((bug["fields"]["mode"], bug["modePill"]), ("bug", None))  # unbadged kinds: plain mono text
        self.assertNotIn("report", bug["order"])
        self.assertNotIn("mode", self.report["wave5"]["sidebar"]["plain"]["drawer"]["order"])  # legacy: row omitted

    def test_sidebar_older_backend_without_workstreams_or_arenas(self):
        sb = self.report["wave5"]["sidebar"]
        leg = sb["legacy"]
        arena_ws = leg["omnim-ai-factonio-legacy-arena"]
        self.assertEqual(arena_ws["mode"]["kind"], "refactor")  # from card workstream_kind
        self.assertEqual(arena_ws["bodyOrder"], ["fc-sb-pipe", "fc-sb-arena", "fc-sb-ws-cards"])
        a = arena_ws["arena"]
        self.assertEqual((a["status"], a["tag"]["text"], a["tag"]["tone"]), ("legacy", "from cards", None))
        self.assertEqual(a["tag"]["title"], "Older backend: rebuilt from card tags; arena status and artifact readiness unknown")
        self.assertEqual(a["count"]["text"], "1/2 done")
        self.assertIsNone(a["callout"])
        trail_ws = leg["omnim-ai-factonio-legacy-trail"]
        self.assertEqual((trail_ws["mode"]["kind"], trail_ws["report"]["ready"]), ("investigate", "false"))  # from start_workstream
        d = sb["legacyDrawer"]
        self.assertEqual(d["order"][:4], ["mode", "model", "arena", "role"])
        self.assertNotIn("candidates", d["order"])
        self.assertEqual(d["fields"]["arena"], "ar_legacy01 define")

    def test_sidebar_never_renders_holdout_text(self):
        sb = self.report["wave5"]["sidebar"]
        self.assertEqual(sb["barrierStrings"], [])
        bar = sb["barrierSection"]
        self.assertEqual((bar["report"]["ready"], bar["arena"]["callout"]), ("true", None))
        self.assertEqual(bar["cards"]["t_h1"]["arenaTag"], {"text": "arena:unknown", "title": "Arena candidate · ar_bar01", "role": "candidate"})
        self.assertEqual(sb["stripChips"], [{"ev": "kind", "text": "mode investigate", "tone": "info"}, {"ev": "read_only", "text": "read-only", "tone": "warn"}])

    def test_dashboard_repo_harness_badge_in_actions_cell(self):
        d = self.report["wave5"]["dashboard"]
        self.assertNotIn("error", d)
        self.assertEqual((d["colgroup"], d["headers"]), (9, 9))
        r = d["repos"]
        for slug, row in r.items():
            self.assertEqual(row["cells"], 9, slug)
        # Approved Unit 2 design: the last cell holds the harness badge, Audit and Edit.
        self.assertEqual(r["omnim-ai-factonio"]["firstCell"], ["fc-trunc fc-repo-name", "fc-trunc fc-repo-path"])
        self.assertEqual(r["omnim-ai-factonio"]["actions"], ["fc-harness", "fc-btn fc-btn--s", "fc-btn fc-btn--s"])
        b = r["omnim-ai-factonio"]["badge"]
        self.assertEqual((b["state"], b["key"], b["text"], b["icon"]), ("ok", "harness", "5 hooks · 1 frozen", "shieldOk"))
        self.assertEqual((r["acme-api"]["badge"]["text"], r["acme-api"]["badge"]["icon"]), ("md only", "shieldOk"))
        self.assertEqual((r["acme-web"]["badge"]["state"], r["acme-web"]["badge"]["icon"]), ("error", "attention"))
        self.assertEqual((r["acme-cli"]["badge"]["state"], r["acme-cli"]["badge"]["key"], r["acme-cli"]["badge"]["icon"]), ("absent", None, "shield"))
        self.assertIsNone(r["acme-old"]["badge"])  # legacy backend: nothing rendered
        self.assertEqual(r["acme-old"]["actions"], ["fc-btn fc-btn--s", "fc-btn fc-btn--s"])

    def test_dashboard_trail_twelve_phase_bar_tones_and_modes(self):
        d = self.report["wave5"]["dashboard"]
        self.assertEqual(
            d["trail"]["phases"],
            [
                ["Delivery", [["define", "define (2)", False], ["design", "design (1)", False], ["implement", "implement (1)", False], ["verify", "verify (0)", True], ["publish", "publish (0)", True], ["gate", "gate (0)", True], ["merge", "merge (0)", True]]],
                ["Factory", [["intake", "intake (2)", False], ["harness", "harness (1)", False], ["eval", "eval (2)", False]]],
                ["Signals", [["escape", "escape (0)", True], ["watchdog", "watchdog (0)", True]]],
            ],
        )
        self.assertEqual(d["trail"]["tenantOptions"][1:], [["omnim-ai-factonio-cache-miss-spike", "cache-miss-spike · investigate · read-only"], ["omnim-ai-factonio-wave-5-desktop-ui", "wave-5-desktop-ui · perf"]])
        self.assertEqual(d["evalRows"], ["d_w08", "d_w07"])
        self.assertEqual(d["evalWsPills"], ["ws wave-5-desktop-ui perf", "ws wave-5-desktop-ui perf"])
        # start_workstream rows already carry a mode pill, so the ws pill omits it.
        self.assertEqual(d["defineWsPills"], {"d_w02": "ws cache-miss-spike", "d_w01": "ws wave-5-desktop-ui"})
        self.assertEqual(d["noMatch"]["chips"], ["preset: Key Decisions", "workstream cache-miss-spike investigate", "phase eval"])
        rows = d["trail"]["rows"]
        self.assertEqual(rows["d_w07"]["actor"], "eval")
        self.assertEqual(rows["d_w05"]["pills"][0]["href"], "https://github.com/omnim-ai/factonio/issues/412")
        self.assertEqual([p["ev"] for p in rows["d_w03"]["pills"]], ["arena_id", "target", "candidates", "more"])  # max 3 + inspector count
        self.assertEqual(
            d["tones"],
            {
                "synthesized": "ok", "verified_fix": "ok", "decided": "ok", "clean": "ok",
                "stop_the_line": "bad", "fix_incomplete": "bad", "no_winner": "bad", "proof_failed": "bad", "product_regression": "bad",
                "flaky": "warn", "not_reproduced": "warn", "tie": "warn", "harness_gap": "warn", "doc_drift": "warn",
                "actionable": "info", "no_action": "info",
            },
        )
        self.assertEqual(d["barrierStrings"], [])
        self.assertEqual(d["barrierStringsAll"], [])

    def test_wave5_styles_tokens_only_and_responsive_rules(self):
        source = PLUGIN_JS.read_text(encoding="utf-8")
        start = source.index("const STYLES = `")
        styles = source[start : source.index("`", start + len("const STYLES = `"))]
        block = styles[styles.index("/* v0.4.0 Wave 5") :]
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
        self.assertNotIn(".pv-", block)  # preview scaffolding is never ported
        for needed in (
            "@container (max-width: 230px) { .fc-sb-mode-ro { display: none; } }",
            "@container (max-width: 270px) { .fc-sb-arena-word { display: none; } }",
            "@container (max-width: 640px) {",
            ".fc-sb-arena .fc-sb-panel-top { flex-wrap: nowrap; }",
            '.fc-sb-vendor[data-role="judge"][data-cross="false"]',
            '.fc-sb-arena .fc-sb-callout[data-kind="blocked"] { color: color-mix(in srgb, var(--dt-destructive) 48%, var(--ui-text-primary)); }',
            '.fc-sb-drawer dd[data-field="arena"] { flex-wrap: wrap;',
            '.fc-harness[data-state="error"]',
            '.fc-harness[data-state="absent"] { border-style: dashed;',
        ):
            self.assertIn(needed, block)
        # The full STYLES sheet stays free of colour literals too.
        self.assertNotRegex(styles, r"rgba?\(|hsla?\(")
        self.assertEqual(source.count("setInterval("), 0)
        for name in ("arena", "eye", "shield", "shieldOk"):
            self.assertRegex(source, rf"\n  {name}: \[\[")

    # --- Control plane (Unit 2) ----------------------------------------------------
    def test_cp_routine_fold_and_day_group_helpers(self):
        c = self.report["cp"]
        self.assertEqual(c["fold"], ["a", "[3:bcd|2/1/0|pr_gate]", "e", "f", "g", "h", "i"])
        self.assertEqual(c["foldEmpty"], [])
        self.assertEqual(c["groups"], [
            {"key": "2026-11-27", "label": "Today · Nov 27", "count": 7, "items": ["a", "[3:bcd|2/1/0|pr_gate]", "e", "f", "g"]},
            {"key": "2026-11-26", "label": "Yesterday · Nov 26", "count": 2, "items": ["h", "i"]},
        ])
        self.assertEqual(c["routine"], {"gate": True, "human": False, "supersedes": False, "escape": False, "proofFailed": False, "blocked": True, "merge": False})
        self.assertEqual(c["attention"], {"watchdog": True, "superseded": False, "audit": True, "routineBlocked": False, "reject": True, "pass": False})
        self.assertEqual(c["tiers"], [1, 1, 1, 3, 2])

    def test_cp_supersession_chains_are_transitive_and_cycle_safe(self):
        self.assertEqual(self.report["cp"]["chains"], {
            "c3": {"priors": ["c2", "c1"], "missing": []},
            "c2": {"priors": ["c1"], "missing": []},
            "m1": {"priors": [], "missing": ["gone"]},
            "y1": {"priors": ["y2"], "missing": []},
            "y2": {"priors": ["y1"], "missing": []},
        })

    def test_cp_search_audit_and_barrier_helpers(self):
        c = self.report["cp"]
        self.assertEqual(c["query"], [True, True, True, True, False, False, True, False])
        self.assertEqual(c["strip"], {"a": 1, "b": ["ok"], "c": {"e": "fine"}})
        self.assertFalse(c["audit"]["unavailable404"]["available"])
        self.assertFalse(c["audit"]["notAudit"])
        self.assertEqual((c["audit"]["clean"]["available"], c["audit"]["clean"]["clean"], c["audit"]["clean"]["inspected"]), (True, True, 1167))
        f = c["audit"]["findings"]
        self.assertEqual([(x["kind"], x["decisionId"], x["message"], x["severity"]) for x in f["findings"]], [
            ("gate_loosening", "d_1", "Removed a hook", "high"),
            ("weak_evidence", "d_2", "weak evidence", "medium"),
            ("unclosed_escalation", "d_2", "Open", "high"),
        ])
        self.assertEqual(f["counts"], [["gate_loosening", 1], ["unclosed_escalation", 1], ["weak_evidence", 1]])
        self.assertEqual(f["ids"], ["d_1", "d_2"])
        self.assertEqual(c["scan"], ["pr_number", "branch", "head_sha"])
        self.assertEqual(c["scanMore"], 3)
        self.assertEqual(c["paths"], ["/trail?limit=200", "/trail?tenant=a%20b&limit=250&signal=key&q=x%26y", "/trail?limit=200"])

    def test_cp_human_decision_validation_and_body(self):
        c = self.report["cp"]
        self.assertEqual(set(c["draft"]["empty"]), {"decision", "why", "phase"})
        self.assertIn("decision", c["draft"]["spaces"])
        self.assertIn("tenant", c["draft"]["badTenant"])
        self.assertEqual(c["draft"]["array"], {"evidence": "Evidence must be a JSON object."})
        self.assertEqual(c["draft"]["big"], {"evidence": "Evidence must be 16 KiB or less."})
        self.assertEqual(c["draft"]["ok"], {})
        self.assertEqual(c["body"], {"decision": "override_gate", "phase": "gate", "why": "because", "confirmed": True, "repo": "acme", "card_id": "t_1", "supersedes_id": "d_old", "evidence": {"pr_number": 1}})

    def test_cp_markdown_parser_is_safe(self):
        c = self.report["cp"]
        self.assertEqual(c["href"], ["https://github.com/o/r/pull/1", None, None, None, None, None, None, None, None, "https://ok.test/a"])
        types = [t["type"] for t in c["inline"]]
        self.assertEqual(types[:8], ["text", "code", "text", "strong", "text", "em", "text", "link"])
        self.assertEqual([t for t in c["inline"] if t["type"] == "link"], [{"type": "link", "text": "ok", "href": "https://a.test/x"}])
        self.assertIn("<img src=x onerror=alert(1)>", "".join(t["text"] for t in c["inline"]))
        self.assertEqual([b["type"] for b in c["blocks"]], ["heading", "paragraph", "list", "list", "quote", "code", "table", "rule", "heading", "paragraph"])
        self.assertEqual(c["blocks"][1]["text"], "Para one continues here.")
        self.assertEqual(c["blocks"][3], {"type": "list", "ordered": True, "items": ["first", "second"]})
        self.assertEqual(c["blocks"][5], {"type": "code", "lang": "js", "text": 'const x = "<script>"'})
        self.assertEqual(c["blocks"][6], {"type": "table", "head": ["key", "value"], "rows": [["a", "`b`"], ["c", "d"]]})
        self.assertEqual(c["blocks"][8]["level"], 4)
        self.assertEqual(c["report"]["missing"], {"exists": False})
        self.assertEqual(c["report"]["junk"], {"exists": False})
        v = c["report"]["view"]
        self.assertEqual(v["content"], "# R\n[redacted]\nok")
        self.assertEqual((v["truncated"], v["redacted"], v["sizeBytes"]), (True, True, 300000))
        self.assertNotIn("filename", v)
        source = PLUGIN_JS.read_text(encoding="utf-8")
        self.assertNotIn("dangerouslySetInnerHTML", source)
        self.assertNotIn("innerHTML", source)

    def test_cp_publish_eligibility_and_request(self):
        p = self.report["cp"]["publish"]
        self.assertEqual(p["ok"], {"eligible": True, "reason": None, "label": "Diff PASS · Black-Box PASS"})
        self.assertEqual(p["panelPass"], {"eligible": True, "reason": None, "label": "Panel PASS · 2 vendors"})
        for key, reason in (("noTarget", "no-local-target"), ("hasPr", "has-pr"), ("verifyCard", "verify-card"), ("shortSha", "invalid-target"), ("noRepo", "unknown-repo")):
            self.assertEqual((p[key]["eligible"], p[key]["reason"]), (False, reason), key)
        for key in ("onlyDiff", "reject", "otherSha", "running", "panelPending"):
            self.assertEqual((p[key]["eligible"], p[key]["reason"]), (False, "unverified"), key)
        self.assertEqual(p["reject"]["label"], "rejected")
        body = self.report["cp"]["publishBody"]
        self.assertEqual(body, {"repo": "omnim-ai/factonio", "branch": "feat/x", "expected_head_sha": "a" * 40, "title": "T", "draft": True, "confirmed": True, "base_branch": "main", "tenant": "ws-pub", "card_id": "t_impl"})
        self.assertNotIn("body_file", body)
        self.assertNotIn("remote", body)

    def test_cp_harness_audit_view_model_fails_closed(self):
        h = self.report["cp"]["harness"]
        st = h["stale"]
        self.assertEqual((st["severity"], st["tone"], st["mode"], st["classification"], st["classTone"]), ("stale", "warn", "advisory", "harness_gap", "bad"))
        self.assertEqual(st["driftedFiles"], ["desktop/plugin.js", "docs/images/dashboard.png"])
        self.assertEqual(st["staleInvariants"], ["skill-bloat — skills/a/SKILL.md"])
        self.assertEqual(st["gateLoosening"], ["Required hook desktop-syntax removed"])
        self.assertEqual(st["recommendations"], ["Run hermes factonio harness sync"])
        self.assertNotIn("/home/u/dev/x", json.dumps(st))
        self.assertEqual((h["clean"]["severity"], h["clean"]["tone"], h["clean"]["head"]), ("clean", "ok", "9c4e1a2"))
        self.assertEqual((h["missing"]["severity"], h["missing"]["tone"], h["missing"]["mode"], h["missing"]["manifestErrors"]), ("missing", "bad", "absent", [".factonio/harness.json missing"]))
        self.assertEqual((h["junk"]["severity"], h["junk"]["classification"]), ("unknown", "unknown"), "an unknown payload never reads as clean")

    def test_cp_design_approval_state(self):
        self.assertEqual(self.report["cp"]["design"], {
            "awaiting": "awaiting", "running": None, "approved": "approved", "revising": "revising",
            "supersededRevision": "awaiting", "implementExists": None, "candidate": None, "notDesign": None,
        })

    def test_cp_sidebar_publish_pr_flow(self):
        s = self.report["cp"]["sidebar"]
        self.assertEqual(s["publishButtons"], ["t_pimpl"], "only the verified local-only card offers Publish PR")
        self.assertEqual(s["publishBtn"], {"text": "Publish PR", "title": "Publish feat/x at aaaaaaaaaaaa", "popup": "dialog"})
        self.assertEqual(s["localRows"], {"branch": "feat/x → main", "head": "aaaaaaa", "verify": "Panel PASS · 2 vendors", "pr": "local only"})
        o = s["publishOpen"]
        self.assertTrue(o["stopped"])
        m = o["modal"]
        self.assertEqual((m["id"], m["role"], m["modal"], m["title"]), ("fc-publish", "dialog", "true", "Publish PR to GitHub"))
        self.assertEqual(m["facts"], ["omnim-ai/factonio", "feat/x → main", "aaaaaaaaaaaa", "Panel PASS · 2 vendors"])
        self.assertEqual((m["title_input"], m["body_input"], m["draft"]), ("feat(desktop): control-plane actions", "Verified locally.", False))
        # The same 12-character SHA everywhere it is shown.
        self.assertEqual(m["shield"], "The server re-checks that aaaaaaaaaaaa is still the verified head. If the branch moved, nothing is pushed.")
        self.assertEqual(m["go"], {"text": "Publish to GitHub", "disabled": False, "action": "publish-go"})
        self.assertEqual(o["focus"], ["fc-pp-title"])
        e = s["publish409"]
        self.assertEqual(e["banners"], [{"tone": "bad", "role": "alert", "text": "409 · Publish refused Nothing was pushed: aaaaaaaaaaaa is no longer the verified head, or its verification changed. Re-verify before publishing. Your title and body are kept. Not locally verified at " + "a" * 40 + ": head drifted"}])
        self.assertEqual((e["title_input"], e["body_input"], e["draft"]), ("feat(desktop): control plane", "Verified locally.\n\nEdited body.", True), "edits survive a 409")
        self.assertEqual(e["go"]["text"], "Retry publish")
        # 409 head drift: observed and verified heads (same 12-char truncation as the modal) plus the server detail.
        dr = s["publishDrift"]
        self.assertEqual(dr["observed"], {"text": "b" * 12, "title": "b" * 40})
        self.assertEqual(dr["verified"], {"text": "a" * 12, "title": "a" * 40})
        self.assertEqual(dr["detail"], {"text": f"HEAD drifted from {'a' * 40} to {'b' * 40}; re-verify before publishing.", "title": None})
        self.assertEqual(dr["modal"]["banners"], [{"tone": "bad", "role": "alert", "text": f"409 · Head drifted The branch is now at {'b' * 12} , not the verified {'a' * 12} . Re-verify before publishing. Your title and body are kept. HEAD drifted from {'a' * 40} to {'b' * 40}; re-verify before publishing."}])
        self.assertEqual((dr["modal"]["title_input"], dr["modal"]["body_input"], dr["modal"]["draft"]), ("feat(desktop): control plane", "Verified locally.\n\nEdited body.", True))
        self.assertEqual(dr["modal"]["facts"][2], "a" * 12, "the verified-head fact is unchanged")
        self.assertEqual(dr["modal"]["go"]["text"], "Retry publish")
        sc = s["publishDriftScrubbed"]
        self.assertEqual((sc["observed"]["text"], sc["verified"]["text"], sc["detail"]), ("b" * 12, "a" * 12, None))
        self.assertNotIn("holdout", json.dumps(sc["modal"]))
        b = s["publishBusy"]
        self.assertEqual((b["busy"], b["go"]["text"], b["go"]["disabled"]), ("true", "Publishing…", True))
        self.assertTrue(s["publishEscBusy"])
        d = s["publishDone"]
        self.assertEqual(d["title"], "PR published")
        self.assertEqual(d["facts"], ["github.com/omnim-ai/factonio/pull/47", "aaaaaaaaaaaa (unchanged)", "publish_pr · actor human"])
        self.assertEqual(len(s["publishBodies"]), 4)
        for body in s["publishBodies"]:
            self.assertEqual(body, {"repo": "omnim-ai/factonio", "branch": "feat/x", "expected_head_sha": "a" * 40, "title": "feat(desktop): control plane", "draft": True, "confirmed": True, "body": "Verified locally.\n\nEdited body.", "base_branch": "main", "tenant": "omnim-ai-factonio-local-publish", "card_id": "t_pimpl"})
        self.assertEqual(s["publishInvalidated"], ["factonio/cards", "factonio/trail"])
        self.assertEqual(s["publishOpened"], ["https://github.com/omnim-ai/factonio/pull/47"])
        self.assertTrue(s["publishClosed"])
        self.assertEqual(s["oldHost"], {"publish": 0, "appr": 0})

    def test_cp_sidebar_investigation_report_viewer(self):
        s = self.report["cp"]["sidebar"]
        self.assertEqual(s["reportBtn"], {"text": "report ready", "cls": "fc-sb-report fc-sb-report-btn", "popup": "dialog", "title": "Open investigation.md (read-only)"})
        self.assertEqual(s["reportQueryLoading"]["state"], "loading")
        r = s["report"]
        self.assertEqual(r["query"], {"key": ["factonio", "report", "omnim-ai-factonio-cache-miss-spike"], "interval": None})
        self.assertEqual(r["rests"], ["/workstreams/omnim-ai-factonio-cache-miss-spike/report"])
        m = r["modal"]
        self.assertEqual((m["id"], m["size"], m["state"]), ("fc-report", "wide", "truncated"))
        self.assertEqual([b["text"] for b in m["banners"]], ["Truncated at the 256 KiB cap. The end of the report isn’t shown.", "Redacted. Lines the information barrier restricts were withheld before display."])
        md = m["md"]
        self.assertEqual(md["dangerous"], 0)
        self.assertEqual(md["links"], [{"href": "https://github.com/omnim-ai/factonio/actions/runs/1", "rel": "noopener noreferrer", "text": "run"}])
        self.assertTrue({"h1", "h2", "ul", "li", "table", "th", "td", "pre", "code", "blockquote", "a"} <= set(md["tags"]))
        self.assertNotIn("img", md["tags"])
        self.assertNotIn("script", md["tags"])
        self.assertEqual(md["code"], ["<script>alert(1)</script>"])
        self.assertIn("[redacted]", md["text"])
        self.assertIn("<img src=x onerror=alert(1)>", md["text"], "raw HTML is shown as text")
        self.assertIn("investigation.md", m["text"])
        self.assertNotIn("/home/u/", m["text"])
        self.assertEqual(s["reportLinkOpened"], ["https://github.com/omnim-ai/factonio/actions/runs/1"])
        self.assertEqual(s["reportBarrier"], [])
        self.assertEqual(s["reportMissing"]["state"], "missing")
        self.assertIn("Report not written yet", s["reportMissing"]["text"])
        self.assertEqual(s["reportError"]["state"], "error")
        self.assertEqual(s["reportError"]["banners"][0]["text"], "Couldn’t load the report (403). The file is not a regular file, so it was not read. Retry")
        self.assertEqual((s["drawerReport"], s["drawerReportOpens"]), ("investigation.md", "fc-report"))

    def test_cp_sidebar_design_approval_strip(self):
        s = self.report["cp"]["sidebar"]
        self.assertEqual(s["designOnlyDesign"], ["t_dsg"])
        self.assertEqual(s["designStrip"], {"state": "awaiting", "text": "awaiting approval Approve Design Request Revision"})
        self.assertEqual(s["reviseOpen"], {"text": "awaiting approval What needs to change? (required) Appends an immutable human decision · request_design_revision Send revision request Cancel", "confirmDisabled": True})
        self.assertEqual(s["approveOpen"], "awaiting approval Note (optional) Appends an immutable human decision · approve_design Confirm approval Cancel")
        a = s["approved"]
        self.assertEqual(a["state"], "approved")
        self.assertEqual(a["text"], "approved Design approved by you · just now · d_appr0001 appended to trail.db")
        self.assertEqual(a["rests"], [{"decision": "approve_design", "phase": "design", "why": "Ship it.", "confirmed": True, "tenant": "omnim-ai-factonio-design-review", "card_id": "t_dsg"}])
        self.assertEqual(a["invalidated"], ["factonio/trail"])

    def test_cp_sidebar_panel_and_arena_reevaluate(self):
        s = self.report["cp"]["sidebar"]
        self.assertEqual(s["arenaReeval"], {"label": "Re-evaluate arena", "text": "Re-evaluate", "inTop": True})
        self.assertEqual(s["arenaBusy"], {"text": "Evaluating", "disabled": True, "stopped": True})
        self.assertEqual(s["arenaDone"], {"msg": "Updated just now", "rests": [{"path": "/arenas/evaluate", "body": {"arena_id": "ar_7f2c9a10", "tenant": "omnim-ai-factonio-design-review"}}]})
        self.assertEqual(s["panelError"], {"text": "Evaluate failed (404: panel not found). Retry", "role": "alert", "retry": True, "rests": [{"path": "/panels/evaluate", "body": {"panel_id": "vp_9a1", "tenant": "omnim-ai-factonio-local-publish"}}]})
        self.assertEqual(s["queries"]["other"], [], "no new polled queries")
        self.assertEqual(s["queries"]["polling"], ["factonio/cards/all"])
        self.assertEqual(s["barrier"], [])

    def test_cp_repo_harness_audit_drawer(self):
        a = self.report["cp"]["repoAudit"]
        self.assertEqual(a["headers"], ["Harness & actions"])
        self.assertEqual(a["actions"], [["audit-repo", "edit-repo"]])
        self.assertEqual(a["note"], "Audit runs the harness check live, read-only. It never edits repo files or .git/index .")
        b = a["busy"]
        self.assertEqual(b["drawer"]["colSpan"], 9)
        self.assertEqual(b["drawer"]["status"], "busy")
        self.assertIn("read-only, no repo files touched", b["drawer"]["text"])
        self.assertEqual(b["btn"], {"disabled": True, "expanded": "true", "controls": "fc-repo-au-tozes-factonio"})
        self.assertEqual(b["rowAudit"], "true")
        st = a["stale"]
        self.assertEqual((st["status"], st["id"], st["label"]), ("done", "fc-repo-au-tozes-factonio", "Harness audit result for tozes/factonio"))
        self.assertEqual(st["chips"], [["severity", "stale", "warn"], ["mode", "mode advisory", None], ["drift", "drift harness_gap", "bad"]])
        self.assertEqual(st["sections"]["drifted"], {"title": "Drifted files · 1", "items": ["desktop/plugin.js"]})
        self.assertEqual(st["sections"]["stale"]["items"], ["skill-bloat"])
        self.assertEqual(st["sections"]["gate"]["items"], [])
        self.assertEqual(st["sections"]["recs"], {"title": "Recommendations", "items": ["Run `hermes factonio harness sync`."]})
        self.assertEqual(a["rests"], [{"path": "/repos/tozes-factonio/harness/audit", "method": "POST", "body": {}}])
        self.assertIn("factonio/repos", a["invalidated"])
        self.assertIn("factonio/trail", a["invalidated"])
        self.assertEqual(a["barrierPath"], [], "repo_path is never rendered")
        self.assertEqual(a["missing"]["status"], "error")
        self.assertIn("409 · Local checkout missing.", a["missing"]["banner"])
        self.assertEqual(a["rowOrder"], ["tozes-factonio", "tozes-factonio:audit", "acme-web-dashboard", "acme-cli-tools", "acme-cli-tools:audit"])
        self.assertEqual(a["closed"], {"drawer": False, "expanded": "false"})

    def test_cp_styles_tokens_only_and_design_rules(self):
        source = PLUGIN_JS.read_text(encoding="utf-8")
        start = source.index("const STYLES = `")
        styles = source[start : source.index("`", start + len("const STYLES = `"))]
        block = styles[styles.index("/* Decision History + control plane") :]
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
        self.assertNotIn(".pv-", block)
        for needed in (
            ".fc-dh-scroll { max-height: 720px; overflow: auto;",
            ".fc-dh-day { position: sticky; top: 0;",
            ".fc-dh-main { display: grid; grid-template-columns: 52px 78px minmax(0, 1fr) auto;",
            ".fc-dh-fold { display: grid; grid-template-columns: 52px 78px minmax(0, 1fr) auto;",
            ".fc-dh-foldrows {",
            ".fc-repos tr[data-audit=\"true\"] > td:first-child { box-shadow: inset 2px 0 0 var(--ui-accent); }",
            "@container (max-width: 270px) { .fc-sb-reeval-l { display: none; } }",
            ".fc-sb-report-btn {",
            ".fc-btn--primary:disabled { opacity: 1;",
            "@container (max-width: 760px) {",
            "@container (max-width: 640px) {",
            "@media (prefers-reduced-motion: reduce) {",
        ):
            self.assertIn(needed, block)
        self.assertEqual(source.count("setInterval("), 0)
        imports = re.findall(r"^import .* from '([^']+)'", source, re.M) + re.findall(r"^} from '([^']+)'", source, re.M)
        self.assertEqual(sorted(set(imports)), ["@hermes/plugin-sdk", "react", "react/jsx-runtime"])


if __name__ == "__main__":
    unittest.main()
