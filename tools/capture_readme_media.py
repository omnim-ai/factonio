#!/usr/bin/env python3
"""Render the real Factonio Desktop plugin (`desktop/plugin.js`) to generate README screenshots and GIFs.

Usage:
    # Preview every scene live in any browser (zero external dependencies):
    python3 tools/capture_readme_media.py --serve-only --port 18432

    # Build GIFs from captured frame directories (requires ffmpeg):
    python3 tools/capture_readme_media.py --encode-gifs --frames-dir /tmp/factonio-frames --out docs/images

The harness loads the unmodified ES module `desktop/plugin.js` in Chromium using an
import map pointing `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` to small
zero-dependency local shims, themed with authentic Hermes Desktop dark-mode tokens.
"""

from __future__ import annotations

import argparse
import glob
import http.server
import mimetypes
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Minimal offline React + jsx-runtime ES module shim (supports desktop/plugin.js)
# ---------------------------------------------------------------------------

REACT_SHIM_JS = r"""
const SVG_NS = 'http://www.w3.org/2000/svg';
const SVG_TAGS = new Set([
  'svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'g', 'defs', 'use'
]);

let rootContainer = null;
let rootComponent = null;
let hookStore = new Map();
let refStore = new Map();
let effectStore = new Map();
let activePath = 'root';
let hookIdx = 0;
let pendingEffects = [];
let rerenderQueued = false;

function scheduleRender() {
  if (rerenderQueued) return;
  rerenderQueued = true;
  Promise.resolve().then(() => {
    rerenderQueued = false;
    renderNow();
  });
}

export function __rerender() {
  renderNow();
}

export function __reset() {
  hookStore = new Map();
  refStore = new Map();
  effectStore = new Map();
}

export function useState(init) {
  const key = activePath + '#s' + (hookIdx++);
  if (!hookStore.has(key)) {
    hookStore.set(key, typeof init === 'function' ? init() : init);
  }
  const setState = (next) => {
    const prev = hookStore.get(key);
    const value = typeof next === 'function' ? next(prev) : next;
    if (!Object.is(prev, value)) {
      hookStore.set(key, value);
      scheduleRender();
    }
  };
  return [hookStore.get(key), setState];
}

export function useRef(init) {
  const key = activePath + '#r' + (hookIdx++);
  if (!refStore.has(key)) {
    refStore.set(key, { current: init });
  }
  return refStore.get(key);
}

export function useEffect(fn, deps) {
  const key = activePath + '#e' + (hookIdx++);
  const prev = effectStore.get(key);
  let changed = true;
  if (prev && Array.isArray(deps) && Array.isArray(prev.deps) && deps.length === prev.deps.length) {
    changed = deps.some((d, i) => !Object.is(d, prev.deps[i]));
  }
  if (changed) {
    pendingEffects.push({ key, fn, deps: Array.isArray(deps) ? [...deps] : null });
  }
}

function setRef(ref, el) {
  if (!ref) return;
  if (typeof ref === 'function') ref(el);
  else if (typeof ref === 'object') ref.current = el;
}

function applyProps(el, props, isSvg) {
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'children' || k === 'key' || k === 'ref' || v === undefined || v === null) continue;
    if (k === 'className') {
      if (isSvg) el.setAttribute('class', String(v));
      else el.className = String(v);
    } else if (k === 'style' && typeof v === 'object') {
      for (const [sk, sv] of Object.entries(v)) {
        if (sv !== undefined && sv !== null) el.style[sk] = String(sv);
      }
    } else if (k === 'htmlFor') {
      el.setAttribute('for', String(v));
    } else if (k === 'colSpan') {
      el.setAttribute('colspan', String(v));
    } else if (k === 'noValidate') {
      if (v) el.setAttribute('novalidate', '');
    } else if (k === 'spellCheck') {
      el.setAttribute('spellcheck', String(v));
    } else if (k === 'autoComplete') {
      el.setAttribute('autocomplete', String(v));
    } else if (k === 'inputMode') {
      el.setAttribute('inputmode', String(v));
    } else if (k === 'strokeWidth') {
      el.setAttribute('stroke-width', String(v));
    } else if (k === 'strokeLinecap') {
      el.setAttribute('stroke-linecap', String(v));
    } else if (k === 'strokeLinejoin') {
      el.setAttribute('stroke-linejoin', String(v));
    } else if (k === 'fillRule') {
      el.setAttribute('fill-rule', String(v));
    } else if (k === 'clipRule') {
      el.setAttribute('clip-rule', String(v));
    } else if (k === 'strokeDasharray') {
      el.setAttribute('stroke-dasharray', String(v));
    } else if (k === 'viewBox') {
      el.setAttribute('viewBox', String(v));
    } else if (k === 'onChange') {
      const tag = el.tagName.toLowerCase();
      el.addEventListener(tag === 'select' ? 'change' : 'input', v);
    } else if (k.startsWith('on') && typeof v === 'function') {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'disabled' || k === 'checked' || k === 'selected' || k === 'readOnly') {
      el[k] = Boolean(v);
      if (v) el.setAttribute(k.toLowerCase(), '');
    } else if (k === 'value') {
      el.value = String(v);
    } else if (typeof v === 'boolean') {
      if (v) el.setAttribute(k, '');
    } else {
      el.setAttribute(k, String(v));
    }
  }
}

function buildDom(vnode, path, inSvg = false) {
  if (vnode === null || vnode === undefined || typeof vnode === 'boolean') {
    return document.createTextNode('');
  }
  if (typeof vnode === 'string' || typeof vnode === 'number') {
    return document.createTextNode(String(vnode));
  }
  if (Array.isArray(vnode)) {
    const frag = document.createDocumentFragment();
    vnode.forEach((child, i) => {
      const ck = child && typeof child === 'object' && child.key != null ? `k:${child.key}` : `i:${i}`;
      frag.appendChild(buildDom(child, `${path}/${ck}`, inSvg));
    });
    return frag;
  }
  if (vnode.type === 'fragment') {
    return buildDom(vnode.props?.children, path, inSvg);
  }
  if (typeof vnode.type === 'function') {
    const prevPath = activePath;
    const prevIdx = hookIdx;
    const name = vnode.type.name || 'Fn';
    const keyPart = vnode.key != null ? `:${vnode.key}` : '';
    activePath = `${path}/${name}${keyPart}`;
    hookIdx = 0;
    const rendered = vnode.type(vnode.props || {});
    const dom = buildDom(rendered, activePath, inSvg);
    activePath = prevPath;
    hookIdx = prevIdx;
    return dom;
  }
  const tag = String(vnode.type);
  const isSvg = inSvg || SVG_TAGS.has(tag);
  const el = isSvg ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  const props = vnode.props || {};
  applyProps(el, props, isSvg);
  if (props.children !== undefined) {
    el.appendChild(buildDom(props.children, `${path}/${tag}`, isSvg));
  }
  // Re-apply value after <option> children exist for <select>
  if (tag === 'select' && props.value !== undefined && props.value !== null) {
    el.value = String(props.value);
  }
  if (props.ref) setRef(props.ref, el);
  return el;
}

function renderNow() {
  if (!rootContainer || !rootComponent) return;
  pendingEffects = [];
  const dom = buildDom(rootComponent(), 'root', false);
  rootContainer.replaceChildren(dom);
  const effects = pendingEffects;
  pendingEffects = [];
  for (const eff of effects) {
    const prev = effectStore.get(eff.key);
    if (prev && typeof prev.cleanup === 'function') {
      try { prev.cleanup(); } catch {}
    }
    const cleanup = eff.fn();
    effectStore.set(eff.key, { deps: eff.deps, cleanup: typeof cleanup === 'function' ? cleanup : null });
  }
}

export function __mount(container, componentFn) {
  rootContainer = container;
  rootComponent = componentFn;
  renderNow();
}

export default { useState, useRef, useEffect };
"""

JSX_RUNTIME_SHIM_JS = r"""
export const Fragment = 'fragment';
export function jsx(type, props, key) {
  return { type, props: props || {}, key: key !== undefined ? key : (props && props.key !== undefined ? props.key : null) };
}
export const jsxs = jsx;
"""

PLUGIN_SDK_SHIM_JS = r"""
import { __rerender } from 'react';
import { jsx, jsxs } from 'react/jsx-runtime';

export const ROUTES_AREA = 'routes';
export const SIDEBAR_NAV_AREA = 'sidebar-nav';
export const PALETTE_AREA = 'palette';
export const PANES_AREA = 'panes';
export const STATUSBAR_AREAS = { left: 'statusbar-left', right: 'statusbar-right' };
export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript-directive';

export function EmptyState({ title, description }) {
  return jsxs('div', {
    className: 'fc-empty-fallback',
    style: { padding: '24px 16px', textAlign: 'center', color: 'var(--ui-text-secondary)' },
    children: [
      jsx('div', { style: { fontWeight: 600, color: 'var(--ui-text-primary)', marginBottom: '4px' }, children: title }),
      jsx('div', { style: { fontSize: '12px' }, children: description })
    ]
  });
}

const state = window.__fcMock || {
  sessionId: 'sess_demo',
  visible: true,
  routes: {},
  toasts: [],
  onPatchRepo: null,
};
window.__fcMock = state;

const cache = new Map();

export const host = {
  navigate(path) {
    state.lastNav = path;
  },
  notify(n) {
    state.toasts = [...(state.toasts || []), n];
    if (typeof window.__fcOnNotify === 'function') window.__fcOnNotify(n);
  },
  composer: {
    submit(sid, msg) {
      state.lastSubmit = { sid, msg };
    }
  },
  state: {
    focusedStoredSessionId: { get: () => state.sessionId }
  },
  revealPane(id) {
    state.lastReveal = id;
  },
  paneVisibility() {
    return { get: () => state.visible !== false };
  }
};

export function useValue(v) {
  return v && typeof v === 'object' && typeof v.get === 'function' ? v.get() : v;
}

export function useQuery({ queryKey, queryFn, enabled = true }) {
  if (!enabled) {
    return { data: undefined, isLoading: false, isError: false, dataUpdatedAt: 0 };
  }
  const key = JSON.stringify(queryKey);
  if (!cache.has(key) || state.__version !== cache.get(key).version) {
    try {
      const res = queryFn();
      if (res && typeof res.then === 'function') {
        const prev = cache.get(key)?.data;
        cache.set(key, { data: prev, version: state.__version, isLoading: prev === undefined, isError: false, updated: Date.now() });
        res.then((val) => {
          cache.set(key, { data: val, version: state.__version, isLoading: false, isError: false, updated: Date.now() });
          __rerender();
        }).catch(() => {
          cache.set(key, { data: prev, version: state.__version, isLoading: false, isError: true, updated: Date.now() });
          __rerender();
        });
      } else {
        cache.set(key, { data: res, version: state.__version, isLoading: false, isError: false, updated: Date.now() });
      }
    } catch {
      cache.set(key, { data: undefined, version: state.__version, isLoading: false, isError: true, updated: Date.now() });
    }
  }
  const hit = cache.get(key);
  return { data: hit.data, isLoading: hit.isLoading, isError: hit.isError, dataUpdatedAt: hit.updated };
}

export function useQueryClient() {
  return {
    setQueryData(queryKey, updater) {
      const key = JSON.stringify(queryKey);
      const prev = cache.get(key)?.data;
      const next = typeof updater === 'function' ? updater(prev) : updater;
      cache.set(key, { data: next, version: state.__version, isLoading: false, isError: false, updated: Date.now() });
      __rerender();
    },
    invalidateQueries({ queryKey } = {}) {
      if (!queryKey) {
        cache.clear();
      } else {
        const prefix = JSON.stringify(queryKey).slice(0, -1);
        for (const k of [...cache.keys()]) {
          if (k.startsWith(prefix)) cache.delete(k);
        }
      }
      __rerender();
      return Promise.resolve();
    }
  };
}
"""

# ---------------------------------------------------------------------------
# Shared Hermes Desktop Dark Theme CSS + Shell Chrome
# ---------------------------------------------------------------------------

DESKTOP_SHELL_CSS = r"""
:root {
  color-scheme: dark;
  --ui-chat-surface-background: #13151b;
  --ui-bg-chrome: #13151b;
  --ui-bg-sidebar: #0e1015;
  --ui-bg-elevated: #1b1e28;
  --ui-bg-card: #181b24;
  --ui-text-primary: #eef1f7;
  --ui-text-secondary: #a0abbe;
  --ui-text-tertiary: #78849a;
  --ui-stroke-primary: rgba(148, 163, 184, 0.30);
  --ui-stroke-secondary: rgba(148, 163, 184, 0.20);
  --ui-stroke-tertiary: rgba(148, 163, 184, 0.12);
  --ui-accent: #7c8cf8;
  --ui-success: #2cb67d;
  --dt-destructive: #e5486d;
  --ui-row-hover-background: rgba(124, 140, 248, 0.08);
  --ui-row-active-background: rgba(124, 140, 248, 0.16);
  --dt-font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  --dt-font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

/* Dark themed subtle scrollbars across every pane */
*::-webkit-scrollbar { width: 6px; height: 6px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.25); border-radius: 999px; }

/* Pause infinite CSS animations during deterministic frame capture */
*, *::before, *::after { animation: none !important; transition: none !important; }

* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; width: 100%; height: 100%;
  background: #0a0b0f;
  color: var(--ui-text-primary);
  font-family: var(--dt-font-sans);
  -webkit-font-smoothing: antialiased;
  overflow: hidden;
}

.hd-frame {
  width: 100%; height: 100%;
  display: flex; flex-direction: column;
  background: var(--ui-bg-chrome);
}

.hd-titlebar {
  height: 36px; flex: none;
  display: flex; align-items: center; gap: 10px;
  padding: 0 14px;
  background: var(--ui-bg-sidebar);
  border-bottom: 1px solid var(--ui-stroke-tertiary);
  font-size: 12px; color: var(--ui-text-secondary);
}

.hd-dots { display: flex; gap: 7px; margin-right: 6px; }
.hd-dots span { width: 11px; height: 11px; border-radius: 999px; display: block; }
.hd-dots span:nth-child(1) { background: #ff5f57; }
.hd-dots span:nth-child(2) { background: #febc2e; }
.hd-dots span:nth-child(3) { background: #28c840; }

.hd-crumb { font-weight: 600; color: var(--ui-text-primary); }
.hd-crumb-sep { opacity: 0.45; }
.hd-profile-pill {
  margin-left: auto;
  font-family: var(--dt-font-mono); font-size: 10.5px;
  padding: 2px 8px; border-radius: 999px;
  background: rgba(124, 140, 248, 0.14);
  border: 1px solid rgba(124, 140, 248, 0.35);
  color: #b7c2ff;
}

.hd-main {
  flex: 1; min-height: 0; display: flex; overflow: hidden;
}

.hd-nav {
  width: 172px; flex: none;
  background: var(--ui-bg-sidebar);
  border-right: 1px solid var(--ui-stroke-tertiary);
  padding: 10px 8px;
  display: flex; flex-direction: column; gap: 2px;
}

.hd-nav-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-radius: 6px;
  font-size: 12px; color: var(--ui-text-secondary);
}
.hd-nav-item.active {
  background: var(--ui-row-active-background);
  color: var(--ui-text-primary); font-weight: 600;
}
.hd-nav-sec {
  font-size: 10px; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--ui-text-tertiary);
  padding: 10px 10px 4px;
}

.hd-content {
  flex: 1; min-width: 0; min-height: 0; overflow: hidden;
  display: flex; flex-direction: column;
  background: var(--ui-chat-surface-background);
}

.hd-right-pane {
  width: 344px; flex: none;
  display: flex; flex-direction: column;
  background: var(--ui-bg-sidebar);
  border-left: 1px solid var(--ui-stroke-secondary);
}

.hd-tabs {
  height: 33px; flex: none;
  display: flex; align-items: stretch; gap: 2px;
  padding: 0 8px;
  border-bottom: 1px solid var(--ui-stroke-tertiary);
  background: var(--ui-bg-sidebar);
}
.hd-tab {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 0 10px; font-size: 11.5px; color: var(--ui-text-secondary);
  border-bottom: 2px solid transparent;
}
.hd-tab.active {
  color: var(--ui-text-primary); font-weight: 600;
  border-bottom-color: var(--ui-accent);
}
.hd-pane-mount { flex: 1; min-height: 0; overflow: hidden; }

.hd-statusbar {
  height: 28px; flex: none;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 12px;
  background: var(--ui-bg-sidebar);
  border-top: 1px solid var(--ui-stroke-tertiary);
  font-size: 11px; color: var(--ui-text-secondary);
}
.hd-statusbar-left { display: flex; align-items: center; gap: 12px; }
.hd-statusbar-right { display: flex; align-items: center; gap: 10px; }

/* Chat column for split-view / workflow demo */
.hd-chat {
  flex: 1; min-width: 0; min-height: 0;
  display: flex; flex-direction: column;
  padding: 18px 22px 14px; gap: 12px; overflow: hidden;
}
.hd-msg { display: flex; flex-direction: column; gap: 6px; max-width: 100%; }
.hd-msg-user {
  align-self: flex-end; max-width: 86%;
  background: rgba(124, 140, 248, 0.14);
  border: 1px solid rgba(124, 140, 248, 0.30);
  border-radius: 10px; padding: 9px 12px;
  font-size: 12.5px; line-height: 1.45; color: var(--ui-text-primary);
}
.hd-msg-role {
  font-size: 10.5px; font-weight: 600; color: var(--ui-text-tertiary);
  text-transform: uppercase; letter-spacing: 0.05em;
}
.hd-msg-bot {
  font-size: 12.5px; line-height: 1.5; color: #dbe2f0;
  display: flex; flex-direction: column; gap: 8px;
}
.hd-tool-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 9px; border-radius: 6px; width: fit-content;
  background: var(--ui-bg-elevated);
  border: 1px solid var(--ui-stroke-secondary);
  font-family: var(--dt-font-mono); font-size: 11px; color: #b7c2ff;
}
.hd-tool-chip .dot { width: 6px; height: 6px; border-radius: 999px; background: #2cb67d; }
.hd-tool-chip .dot.run { background: #7c8cf8; }
.hd-tool-chip .dot.err { background: #e5486d; }
.hd-step-banner {
  margin-top: auto;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 9px 12px; border-radius: 8px;
  background: linear-gradient(90deg, rgba(124, 140, 248, 0.16), rgba(44, 182, 125, 0.10));
  border: 1px solid rgba(124, 140, 248, 0.36);
  font-size: 12px; color: var(--ui-text-primary);
}
.hd-step-badge {
  font-family: var(--dt-font-mono); font-size: 10.5px; font-weight: 700;
  padding: 2px 7px; border-radius: 999px;
  background: rgba(124, 140, 248, 0.25); color: #cdd6ff;
  flex: none;
}

/* Toast notification overlay for repo editor demo */
.hd-toast {
  position: fixed; bottom: 38px; right: 20px; z-index: 99;
  display: flex; align-items: center; gap: 8px;
  padding: 8px 13px; border-radius: 8px;
  background: #1b2824;
  border: 1px solid rgba(44, 182, 125, 0.55);
  color: #a7f3d0; font-size: 12px; font-weight: 500;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}

/* Settings shell */
.hd-settings-wrap {
  max-width: 820px; margin: 0 auto; padding: 22px 28px; width: 100%;
}
.hd-settings-h1 { font-size: 17px; font-weight: 600; margin: 0 0 4px; }
.hd-settings-sub { font-size: 12px; color: var(--ui-text-secondary); margin: 0 0 16px; }
.hd-agent-settings {
  margin-top: 10px; padding-top: 14px;
  border-top: 1px solid var(--ui-stroke-tertiary);
}
.hd-agent-settings h3 { font-size: 12.5px; font-weight: 600; margin: 0 0 10px; color: var(--ui-text-primary); }
.hd-kv-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.hd-kv {
  border: 1px solid var(--ui-stroke-tertiary); border-radius: 7px;
  padding: 8px 10px; background: var(--ui-bg-elevated);
  display: flex; justify-content: space-between; align-items: center; font-size: 12px;
}
.hd-kv span:first-child { color: var(--ui-text-secondary); }
.hd-kv code { font-family: var(--dt-font-mono); font-size: 11px; color: var(--ui-text-primary); }
"""

# ---------------------------------------------------------------------------
# Shared Boot Helper (loads real /desktop/plugin.js and collects registrations)
# ---------------------------------------------------------------------------

BOOT_SNIPPET = r"""
  <script type="importmap">
  {
    "imports": {
      "@hermes/plugin-sdk": "/__harness/plugin-sdk.js",
      "react": "/__harness/react.js",
      "react/jsx-runtime": "/__harness/jsx-runtime.js"
    }
  }
  </script>
"""

SHARED_MOCK_DATA_JS = r"""
export const MODELS_DATA = {
  models: {
    define:    { provider: 'anthropic', model: 'claude-opus-4-1',   source: 'auxiliary.factonio_define' },
    design:    { provider: 'anthropic', model: 'claude-sonnet-4-5', source: 'auxiliary.factonio_design' },
    implement: { provider: 'anthropic', model: 'claude-sonnet-4-5', source: 'auxiliary.factonio_implement' },
    verify:    { provider: 'openai',    model: 'gpt-5',             source: 'auxiliary.factonio_verify' },
    watchdog:  { provider: 'google',    model: 'gemini-2.5-flash',  source: 'auxiliary.factonio_watchdog' },
  },
  cross_model_warning: null,
};

export const METRICS_BY_RANGE = {
  '30d': {
    range: '30d',
    totals: { workstreams: 18, done: 74, running: 2, blocked: 0 },
    verification: {
      diff:     { pass: 22, reject: 3, pass_rate: 0.88 },
      blackbox: { pass: 20, reject: 5, pass_rate: 0.80 },
    },
    escapes: { count: 1, verified_total: 20, escape_rate: 0.05 },
    rework: { avg_runs_per_implement_card: 1.28, cards_with_rework: 5 },
    phases: {
      define:            { count: 18, done: 18, median_seconds: 195,  p90_seconds: 380 },
      design:            { count: 7,  done: 7,  median_seconds: 410,  p90_seconds: 690 },
      implement:         { count: 24, done: 23, median_seconds: 640,  p90_seconds: 1290 },
      'verify-diff':     { count: 25, done: 24, median_seconds: 145,  p90_seconds: 280 },
      'verify-blackbox': { count: 25, done: 24, median_seconds: 265,  p90_seconds: 520 },
    },
  },
  '7d': {
    range: '7d',
    totals: { workstreams: 6, done: 26, running: 2, blocked: 0 },
    verification: {
      diff:     { pass: 8, reject: 1, pass_rate: 0.889 },
      blackbox: { pass: 7, reject: 2, pass_rate: 0.778 },
    },
    escapes: { count: 0, verified_total: 7, escape_rate: 0.0 },
    rework: { avg_runs_per_implement_card: 1.17, cards_with_rework: 1 },
    phases: {
      define:            { count: 6, done: 6, median_seconds: 175, p90_seconds: 310 },
      design:            { count: 3, done: 3, median_seconds: 390, p90_seconds: 560 },
      implement:         { count: 8, done: 7, median_seconds: 580, p90_seconds: 1120 },
      'verify-diff':     { count: 9, done: 8, median_seconds: 130, p90_seconds: 240 },
      'verify-blackbox': { count: 9, done: 8, median_seconds: 240, p90_seconds: 440 },
    },
  },
};

export function initialReposState() {
  return {
    defaults: {
      merge_policy: 'auto',
      max_fix_rounds: 3,
      design_provider: 'auto',
      design_approval: 'ask',
    },
    repos: [
      {
        slug: 'omnim-ai-factonio',
        repo: 'omnim-ai/factonio',
        url: 'https://github.com/omnim-ai/factonio',
        path: '~/dev/factonio',
        default_branch: 'main',
        merge_policy: 'auto',
        max_fix_rounds: 3,
        design_provider: null,
        design_approval: null,
        effective_design_provider: 'auto',
        effective_design_approval: 'ask',
        resolved_design_provider: 'claude-design',
        has_required_checks: true,
        models: {},
      },
      {
        slug: 'acme-payments-api',
        repo: 'acme/payments-api',
        url: 'https://github.com/acme/payments-api',
        path: '~/dev/payments-api',
        default_branch: 'main',
        merge_policy: 'ask',
        max_fix_rounds: 5,
        design_provider: 'off',
        design_approval: null,
        effective_design_provider: 'off',
        effective_design_approval: 'ask',
        resolved_design_provider: 'off',
        has_required_checks: true,
        models: {},
      },
      {
        slug: 'acme-web-console',
        repo: 'acme/web-console',
        url: 'https://github.com/acme/web-console',
        path: '~/dev/web-console',
        default_branch: 'develop',
        merge_policy: 'auto',
        max_fix_rounds: 3,
        design_provider: 'hermes',
        design_approval: 'auto',
        effective_design_provider: 'hermes',
        effective_design_approval: 'auto',
        resolved_design_provider: 'hermes',
        has_required_checks: true,
        models: {},
      },
    ],
  };
}

export async function bootPlugin(restHandler) {
  const mod = await import('/desktop/plugin.js');
  const plugin = mod.default;
  const regs = {};
  let settingsPage = null;
  const storage = new Map();
  const ctx = {
    rest: restHandler,
    storage: {
      get: (k) => storage.get(k),
      set: (k, v) => storage.set(k, v),
    },
    os: {
      openExternal: () => Promise.resolve(),
      writeClipboard: () => Promise.resolve(true),
    },
    onDispose: () => {},
    registerMany(items) {
      for (const item of items) regs[item.id] = item;
    },
    registerSettingsPage(cfg) {
      settingsPage = cfg;
    },
  };
  plugin.register(ctx);
  return { regs, settingsPage, ctx };
}
"""

# ---------------------------------------------------------------------------
# Scene 1: Dashboard screenshot (/factonio)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Factonio Dashboard</title>
  <style>{DESKTOP_SHELL_CSS}</style>
  {BOOT_SNIPPET}
</head>
<body>
  <div class="hd-frame">
    <div class="hd-titlebar">
      <div class="hd-dots"><span></span><span></span><span></span></div>
      <span>Hermes Desktop</span>
      <span class="hd-crumb-sep">/</span>
      <span class="hd-crumb">Factonio</span>
      <span class="hd-profile-pill">worker: factonio · cross-model verify</span>
    </div>
    <div class="hd-main">
      <aside class="hd-nav">
        <div class="hd-nav-sec">Workspace</div>
        <div class="hd-nav-item">Chat</div>
        <div class="hd-nav-item">Kanban</div>
        <div class="hd-nav-item">Sessions</div>
        <div class="hd-nav-sec">Plugins</div>
        <div class="hd-nav-item active">Factonio</div>
        <div class="hd-nav-sec">System</div>
        <div class="hd-nav-item">Capabilities</div>
        <div class="hd-nav-item">Settings</div>
      </aside>
      <main class="hd-content" id="page-mount"></main>
    </div>
    <div class="hd-statusbar">
      <div class="hd-statusbar-left">
        <span>omnim-ai/factonio</span>
        <span>branch: main</span>
      </div>
      <div class="hd-statusbar-right" id="pill-mount"></div>
    </div>
  </div>
  <script type="module">
    import {{ __mount }} from 'react';
    import {{ MODELS_DATA, METRICS_BY_RANGE, initialReposState, bootPlugin }} from '/__harness/mock-data.js';

    const reposState = initialReposState();
    window.__fcMock = {{ sessionId: 'sess_dash', visible: true, __version: 1 }};

    const {{ regs }} = await bootPlugin((path, opts) => {{
      if (path.startsWith('/metrics')) {{
        const m = /[?&]range=([^&]+)/.exec(path);
        const key = m ? decodeURIComponent(m[1]) : '30d';
        return METRICS_BY_RANGE[key] || METRICS_BY_RANGE['30d'];
      }}
      if (path === '/models') return MODELS_DATA;
      if (path === '/repos') return reposState;
      if (path === '/status') return {{ ok: true, repos_count: 3, counts: {{ running: 2, blocked: 0, triage: 0 }} }};
      return {{}};
    }});

    __mount(document.getElementById('page-mount'), () => regs.page.render());
    // Also render status pill separately
    const pillEl = document.getElementById('pill-mount');
    pillEl.innerHTML = '<button type="button" class="fc-pill"><span class="fc-dot fc-dot-green"></span><span>Factonio 2 run · 0 attn</span></button>';
    window.__ready = true;
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Scene 2: Sidebar screenshot (Chat + docked Factonio right-sidebar tab)
# ---------------------------------------------------------------------------

SIDEBAR_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Factonio Right Sidebar</title>
  <style>{DESKTOP_SHELL_CSS}</style>
  {BOOT_SNIPPET}
</head>
<body>
  <div class="hd-frame">
    <div class="hd-titlebar">
      <div class="hd-dots"><span></span><span></span><span></span></div>
      <span>Hermes Desktop</span>
      <span class="hd-crumb-sep">/</span>
      <span class="hd-crumb">omnim-ai/factonio</span>
      <span class="hd-profile-pill">Factonio Orchestrator</span>
    </div>
    <div class="hd-main">
      <div class="hd-chat">
        <div class="hd-msg">
          <div class="hd-msg-user">
            Add an editable Registered Repositories table on <code>/factonio</code> and dock the Factonio sidebar tab beside Files.
          </div>
        </div>
        <div class="hd-msg">
          <div class="hd-msg-role">Hermes · factonio:orchestrator</div>
          <div class="hd-msg-bot">
            <div class="hd-tool-chip"><span class="dot"></span>factonio_start · repo=omnim-ai/factonio · tenant=omnim-ai-factonio-sidebar</div>
            <div>Definition finished with <code>spec.md</code> and hidden black-box checks under <code>holdout/</code>. Phase 1b Design previews were approved, and Implementation opened <strong>PR #44</strong>.</div>
            <div class="hd-tool-chip"><span class="dot"></span>factonio_spawn_verify · PR #44 · Diff Check (PASS) + Black-Box Check (REJECT)</div>
            <div>Isolated Black-Box Verification caught a keyboard focus-order bug on collapsed workstreams that Diff Check could not see from static review. Fix Round 1 is paused on one product question before re-verification:</div>
            <div style="border:1px solid var(--ui-stroke-secondary);border-radius:8px;padding:10px 12px;background:var(--ui-bg-elevated);font-size:12px;">
              <div style="font-weight:600;margin-bottom:4px;">Decision needed · card <span style="font-family:var(--dt-font-mono);color:#b7c2ff;">t_4ab36a14</span></div>
              <div style="color:var(--ui-text-primary);">Should expanded/collapsed workstreams persist across app restarts via <code>ctx.storage</code>?</div>
            </div>
            <div class="hd-tool-chip"><span class="dot run"></span>omnim-ai/factonio · PR #42 · Re-verifying /cards enrichment (Diff Check running)</div>
          </div>
        </div>
      </div>
      <aside class="hd-right-pane">
        <div class="hd-tabs">
          <div class="hd-tab">Files</div>
          <div class="hd-tab active">Factonio</div>
        </div>
        <div class="hd-pane-mount" id="pane-mount"></div>
      </aside>
    </div>
    <div class="hd-statusbar">
      <div class="hd-statusbar-left">
        <span>omnim-ai/factonio</span>
        <span>model: anthropic/claude-sonnet-4-5</span>
      </div>
      <div class="hd-statusbar-right">
        <button type="button" class="fc-pill"><span class="fc-dot fc-dot-red"></span><span>Factonio 1 run · 2 attn</span></button>
      </div>
    </div>
  </div>
  <script type="module">
    import {{ __mount }} from 'react';
    import {{ bootPlugin }} from '/__harness/mock-data.js';

    const NOW = Math.floor(Date.now() / 1000);
    const T = s => NOW - s;
    const SIDEBAR = 'omnim-ai-factonio-docked-sidebar-tab';
    const META = 'omnim-ai-factonio-enrich-cards-metadata';
    const PILL = 'omnim-ai-factonio-statusbar-pill-counts';
    const PR44 = {{ repo: 'omnim-ai/factonio', number: 44, url: 'https://github.com/omnim-ai/factonio/pull/44' }};
    const PR42 = {{ repo: 'omnim-ai/factonio', number: 42, url: 'https://github.com/omnim-ai/factonio/pull/42' }};
    const PR39 = {{ repo: 'omnim-ai/factonio', number: 39, url: 'https://github.com/omnim-ai/factonio/pull/39' }};
    const base = {{
      provider: 'anthropic', model: 'claude-sonnet-4-5', parents: [],
      block_kind: null, block_reason: null, pr: null, runs: 1, verdict: null,
      last_outcome: 'completed', last_error: null, summary: null, completed_at: null,
    }};
    const c = o => ({{ ...base, ...o }});
    const CARDS = [
      c({{ id: 't_4ab36a09', tenant: SIDEBAR, session_id: 'sess_demo', phase: 'define', status: 'done', title: '[define] Spec: docked Factonio sidebar tab', model: 'claude-opus-4-1', created_at: T(9000), started_at: T(8980), completed_at: T(8420), elapsed_seconds: 560, active_duration_seconds: 318, summary: 'Acceptance criteria & private holdout/ checks authored.' }}),
      c({{ id: 't_4ab36a0b', tenant: SIDEBAR, session_id: 'sess_demo', phase: 'design', status: 'done', title: '[design] Phase 1b sidebar previews', parents: ['t_4ab36a09'], created_at: T(8400), started_at: T(8390), completed_at: T(7100), elapsed_seconds: 1290, active_duration_seconds: 744 }}),
      c({{ id: 't_4ab36a0d', tenant: SIDEBAR, session_id: 'sess_demo', phase: 'implement', status: 'done', title: '[implement] Tabbed sidebar pane with filters', parents: ['t_4ab36a0b'], pr: PR44, created_at: T(7000), started_at: T(6990), completed_at: T(4600), elapsed_seconds: 2390, active_duration_seconds: 1505 }}),
      c({{ id: 't_4ab36a10', tenant: SIDEBAR, session_id: 'sess_demo', phase: 'verify-diff', status: 'done', verdict: 'pass', title: '[verify-diff] Review PR #44 against spec.md', model: 'gpt-5', provider: 'openai', parents: ['t_4ab36a0d'], pr: PR44, created_at: T(4500), started_at: T(4490), completed_at: T(4010), elapsed_seconds: 480, active_duration_seconds: 402 }}),
      c({{ id: 't_4ab36a11', tenant: SIDEBAR, session_id: 'sess_demo', phase: 'verify-blackbox', status: 'done', verdict: 'reject', last_outcome: 'changes_requested', title: '[verify-blackbox] Keyboard and theme acceptance run', model: 'gpt-5', provider: 'openai', parents: ['t_4ab36a0d'], pr: PR44, created_at: T(4500), started_at: T(4480), completed_at: T(3300), elapsed_seconds: 1180, active_duration_seconds: 960, summary: 'Collapsed workstream header is skipped by Tab key.' }}),
      c({{ id: 't_4ab36a14', tenant: SIDEBAR, session_id: 'sess_demo', phase: 'implement', status: 'blocked', block_kind: 'needs_input', block_reason: 'Should expanded/collapsed workstreams persist across app restarts?', last_outcome: null, title: '[implement] Fix round 1: focus order and callout contrast', parents: ['t_4ab36a11'], pr: PR44, created_at: T(3200), started_at: T(3190), elapsed_seconds: 3190, active_duration_seconds: 212 }}),
      c({{ id: 't_7d21c0e5', tenant: META, session_id: 'sess_demo', phase: 'define', status: 'done', title: '[define] Spec: additive /cards metadata fields', created_at: T(6200), started_at: T(6190), completed_at: T(5900), elapsed_seconds: 290, active_duration_seconds: 240 }}),
      c({{ id: 't_a1b2c3d4', tenant: META, session_id: 'sess_demo', phase: 'implement', status: 'done', runs: 2, title: '[implement] Enrich /cards metadata in metrics.py', parents: ['t_7d21c0e5'], pr: PR42, created_at: T(5100), started_at: T(1200), completed_at: T(800), elapsed_seconds: 412, active_duration_seconds: 185, summary: 'Added additive card fields and updated unit tests.' }}),
      c({{ id: 't_a1b2c3e0', tenant: META, session_id: 'sess_demo', phase: 'verify-diff', status: 'running', runs: 1, title: '[verify-diff] Review PR #42 against spec.md', model: 'gpt-5', provider: 'openai', parents: ['t_a1b2c3d4'], pr: PR42, created_at: T(700), started_at: T(220), elapsed_seconds: 220, active_duration_seconds: 96 }}),
      c({{ id: 't_a1b2c3e1', tenant: META, session_id: 'sess_demo', phase: 'verify-blackbox', status: 'running', title: '[verify-blackbox] API contract run for /cards', model: 'gpt-5', provider: 'openai', parents: ['t_a1b2c3d4'], pr: PR42, created_at: T(700), started_at: T(210), elapsed_seconds: 210, active_duration_seconds: 108 }}),
      c({{ id: 't_39c1aa01', tenant: PILL, session_id: 'sess_demo', phase: 'define', status: 'done', title: '[define] Status pill counts spec', created_at: T(90000), started_at: T(89990), completed_at: T(89700), elapsed_seconds: 290, active_duration_seconds: 210 }}),
      c({{ id: 't_39c1aa03', tenant: PILL, session_id: 'sess_demo', phase: 'implement', status: 'done', pr: PR39, title: '[implement] Running/blocked counts in status bar', created_at: T(88900), started_at: T(88890), completed_at: T(87000), elapsed_seconds: 1890, active_duration_seconds: 1220 }}),
      c({{ id: 't_39c1aa04', tenant: PILL, session_id: 'sess_demo', phase: 'verify-diff', status: 'done', verdict: 'pass', pr: PR39, title: '[verify-diff] Review PR #39', created_at: T(86900), started_at: T(86890), completed_at: T(86500), elapsed_seconds: 390, active_duration_seconds: 330 }}),
      c({{ id: 't_39c1aa05', tenant: PILL, session_id: 'sess_demo', phase: 'verify-blackbox', status: 'done', verdict: 'pass', pr: PR39, title: '[verify-blackbox] Acceptance run for PR #39', created_at: T(86900), started_at: T(86880), completed_at: T(86100), elapsed_seconds: 780, active_duration_seconds: 640 }}),
    ];

    window.__fcMock = {{ sessionId: 'sess_demo', visible: true, __version: 1 }};
    const {{ regs }} = await bootPlugin((path) => {{
      if (path.startsWith('/cards')) return {{ cards: CARDS, counts: {{ running: 2, blocked: 1, triage: 0, rejected: 1, attention: 2, done: 11, total: 14 }} }};
      return {{}};
    }});

    __mount(document.getElementById('pane-mount'), () => regs.pane.render());
    // Expand the rejected blackbox card so its detail drawer is visible
    await new Promise(r => setTimeout(r, 60));
    const cardBtn = document.querySelector('article[data-id="t_4ab36a11"] button[data-action="toggle-card"]');
    if (cardBtn) cardBtn.click();
    await new Promise(r => setTimeout(r, 60));
    window.__ready = true;
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Scene 3: Settings -> Plugins -> Factonio screenshot
# ---------------------------------------------------------------------------

SETTINGS_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Settings -> Plugins -> Factonio</title>
  <style>{DESKTOP_SHELL_CSS}</style>
  {BOOT_SNIPPET}
</head>
<body>
  <div class="hd-frame">
    <div class="hd-titlebar">
      <div class="hd-dots"><span></span><span></span><span></span></div>
      <span>Settings</span>
      <span class="hd-crumb-sep">/</span>
      <span>Plugins</span>
      <span class="hd-crumb-sep">/</span>
      <span class="hd-crumb">Factonio</span>
    </div>
    <div class="hd-main">
      <aside class="hd-nav">
        <div class="hd-nav-sec">Settings</div>
        <div class="hd-nav-item">General</div>
        <div class="hd-nav-item">Model</div>
        <div class="hd-nav-item">Appearance</div>
        <div class="hd-nav-sec">Plugins</div>
        <div class="hd-nav-item active">Factonio</div>
      </aside>
      <main class="hd-content" style="overflow:auto;">
        <div class="hd-settings-wrap">
          <h1 class="hd-settings-h1">Factonio</h1>
          <p class="hd-settings-sub">Pure-Hermes, phase-based software factory plugin for any GitHub repository.</p>
          <div id="settings-mount"></div>
          <div class="hd-agent-settings">
            <h3>Agent settings</h3>
            <div class="hd-kv-grid">
              <div class="hd-kv"><span>Repositories directory</span><code>~/dev</code></div>
              <div class="hd-kv"><span>Max concurrent workers</span><code>4</code></div>
              <div class="hd-kv"><span>Default PR merge policy</span><code>auto</code></div>
              <div class="hd-kv"><span>Default max fix rounds</span><code>3</code></div>
              <div class="hd-kv"><span>Design subphase provider</span><code>auto</code></div>
              <div class="hd-kv"><span>Design approval policy</span><code>ask</code></div>
            </div>
          </div>
        </div>
      </main>
    </div>
  </div>
  <script type="module">
    import {{ __mount }} from 'react';
    import {{ MODELS_DATA, bootPlugin }} from '/__harness/mock-data.js';

    window.__fcMock = {{ sessionId: 'sess_set', visible: true, __version: 1 }};
    const {{ settingsPage }} = await bootPlugin((path) => {{
      if (path === '/models') return MODELS_DATA;
      return {{}};
    }});

    __mount(document.getElementById('settings-mount'), () => settingsPage.render());
    window.__ready = true;
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Scene 4: Animated Workflow Demo (driven step-by-step via window.__demo.setStep)
# ---------------------------------------------------------------------------

WORKFLOW_DEMO_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Factonio End-to-End Workflow Demo</title>
  <style>{DESKTOP_SHELL_CSS}</style>
  {BOOT_SNIPPET}
</head>
<body>
  <div class="hd-frame">
    <div class="hd-titlebar">
      <div class="hd-dots"><span></span><span></span><span></span></div>
      <span>Hermes Desktop</span>
      <span class="hd-crumb-sep">/</span>
      <span class="hd-crumb">acme/payments-api</span>
      <span class="hd-profile-pill">Factonio Software Factory</span>
    </div>
    <div class="hd-main">
      <div class="hd-chat" id="chat-area"></div>
      <aside class="hd-right-pane">
        <div class="hd-tabs">
          <div class="hd-tab">Files</div>
          <div class="hd-tab active">Factonio</div>
        </div>
        <div class="hd-pane-mount" id="pane-mount"></div>
      </aside>
    </div>
    <div class="hd-statusbar">
      <div class="hd-statusbar-left">
        <span>acme/payments-api</span>
        <span>cross-model: claude-sonnet-4-5 → gpt-5</span>
      </div>
      <div class="hd-statusbar-right" id="pill-mount"></div>
    </div>
  </div>
  <script type="module">
    import {{ __mount, __rerender }} from 'react';
    import {{ bootPlugin }} from '/__harness/mock-data.js';

    const NOW = Math.floor(Date.now() / 1000);
    const T = s => NOW - s;
    const TENANT = 'acme-payments-api-rate-limit-i42';
    const PR = {{ repo: 'acme/payments-api', number: 42, url: 'https://github.com/acme/payments-api/pull/42' }};
    const base = {{
      tenant: TENANT, session_id: 'sess_demo',
      provider: 'anthropic', model: 'claude-sonnet-4-5', parents: [],
      block_kind: null, block_reason: null, pr: null, runs: 1, verdict: null,
      last_outcome: 'completed', last_error: null, summary: null, completed_at: null,
    }};
    const c = o => ({{ ...base, ...o }});

    const STEPS = [
      {{
        badge: 'STEP 1 / 8',
        caption: 'Phase 1 · Definition explores the repo, writes spec.md + hidden holdout/ checks',
        pill: {{ dot: 'fc-dot-green', text: 'Factonio 1 run · 0 attn' }},
        chat: `
          <div class="hd-msg"><div class="hd-msg-user">Add token-bucket rate limiting to <code>POST /v1/events</code> with a status header and UI quota badge (issue #42).</div></div>
          <div class="hd-msg"><div class="hd-msg-role">Hermes · factonio:orchestrator</div><div class="hd-msg-bot">
            <div class="hd-tool-chip"><span class="dot run"></span>factonio_start · repo=acme/payments-api · kind=feature · issue=#42</div>
            <div>Spawned <strong>Phase 1 Definition</strong> on the headless <code>factonio</code> worker profile (<code>claude-opus-4-1</code>).</div>
            <div style="color:var(--ui-text-secondary);">Authoring <code>spec.md</code> (acceptance criteria + parallel task DAG) and private black-box tests under <code>holdout/</code>.</div>
          </div></div>`,
        cards: [
          c({{ id: 't_def42', phase: 'define', status: 'running', model: 'claude-opus-4-1', title: '[define] Spec & holdout checks for /v1/events rate limiting', created_at: T(180), started_at: T(175), elapsed_seconds: 175, active_duration_seconds: 142 }}),
        ],
      }},
      {{
        badge: 'STEP 2 / 8',
        caption: 'Phase 1b · Design generates UI prototypes (Claude Design or pure-Hermes) without seeing holdout/',
        pill: {{ dot: 'fc-dot-green', text: 'Factonio 1 run · 0 attn' }},
        chat: `
          <div class="hd-msg"><div class="hd-msg-user">Add token-bucket rate limiting to <code>POST /v1/events</code> with a status header and UI quota badge (issue #42).</div></div>
          <div class="hd-msg"><div class="hd-msg-role">Hermes · factonio:orchestrator</div><div class="hd-msg-bot">
            <div class="hd-tool-chip"><span class="dot"></span>Definition complete · spec.md + private holdout/ ready · has_ui=true</div>
            <div class="hd-tool-chip"><span class="dot run"></span>factonio_spawn_design · provider=claude-design</div>
            <div>Generating <code>DESIGN.md</code> and interactive HTML previews for the quota badge. <code>holdout/</code> remains strictly hidden.</div>
          </div></div>`,
        cards: [
          c({{ id: 't_def42', phase: 'define', status: 'done', model: 'claude-opus-4-1', title: '[define] Spec & holdout checks for /v1/events rate limiting', created_at: T(600), started_at: T(595), completed_at: T(410), elapsed_seconds: 185, active_duration_seconds: 185, summary: 'Wrote spec.md (2 parallel units) and 4 holdout checks.' }}),
          c({{ id: 't_des42', phase: 'design', status: 'running', parents: ['t_def42'], title: '[design] Quota badge states & warning threshold preview', created_at: T(400), started_at: T(395), elapsed_seconds: 210, active_duration_seconds: 168 }}),
        ],
      }},
      {{
        badge: 'STEP 3 / 8',
        caption: 'Phase 2 · Parallel Implementation runs in isolated git worktrees and opens PR #42',
        pill: {{ dot: 'fc-dot-green', text: 'Factonio 2 run · 0 attn' }},
        chat: `
          <div class="hd-msg"><div class="hd-msg-user">Add token-bucket rate limiting to <code>POST /v1/events</code> with a status header and UI quota badge (issue #42).</div></div>
          <div class="hd-msg"><div class="hd-msg-role">Hermes · factonio:orchestrator</div><div class="hd-msg-bot">
            <div class="hd-tool-chip"><span class="dot"></span>Design approved · attached design/DESIGN.md</div>
            <div class="hd-tool-chip"><span class="dot run"></span>factonio_spawn_implement × 2 (parallel git worktrees)</div>
            <div>Two scoped units are building concurrently from <code>spec.md</code> and <code>design/</code> — zero access to <code>holdout/</code>.</div>
          </div></div>`,
        cards: [
          c({{ id: 't_def42', phase: 'define', status: 'done', model: 'claude-opus-4-1', title: '[define] Spec & holdout checks for /v1/events rate limiting', created_at: T(900), started_at: T(895), completed_at: T(710), elapsed_seconds: 185, active_duration_seconds: 185 }}),
          c({{ id: 't_des42', phase: 'design', status: 'done', parents: ['t_def42'], title: '[design] Quota badge states & warning threshold preview', created_at: T(700), started_at: T(695), completed_at: T(450), elapsed_seconds: 245, active_duration_seconds: 210 }}),
          c({{ id: 't_imp42a', phase: 'implement', status: 'running', parents: ['t_def42'], pr: PR, title: '[implement] Token-bucket middleware & Retry-After headers', created_at: T(440), started_at: T(435), elapsed_seconds: 290, active_duration_seconds: 245 }}),
          c({{ id: 't_imp42b', phase: 'implement', status: 'running', parents: ['t_des42'], title: '[implement] Live quota badge component & unit tests', created_at: T(440), started_at: T(432), elapsed_seconds: 280, active_duration_seconds: 198 }}),
        ],
      }},
      {{
        badge: 'STEP 4 / 8',
        caption: 'Phase 3 · Isolated Parallel Verification launches Diff Check and Black-Box Check on a separate model',
        pill: {{ dot: 'fc-dot-green', text: 'Factonio 2 run · 0 attn' }},
        chat: `
          <div class="hd-msg"><div class="hd-msg-user">Add token-bucket rate limiting to <code>POST /v1/events</code> with a status header and UI quota badge (issue #42).</div></div>
          <div class="hd-msg"><div class="hd-msg-role">Hermes · factonio:orchestrator</div><div class="hd-msg-bot">
            <div class="hd-tool-chip"><span class="dot"></span>PR #42 opened · unit tests green (head 9c4e1a2)</div>
            <div class="hd-tool-chip"><span class="dot run"></span>factonio_spawn_verify · cross-model openai/gpt-5</div>
            <div><strong>Information Barrier active:</strong><br/>• <code>verify-diff</code> inspects <code>spec.md</code> + <code>git diff</code> (blind to <code>holdout/</code>)<br/>• <code>verify-blackbox</code> runs the live server + <code>holdout/</code> (blind to <code>git diff</code>)</div>
          </div></div>`,
        cards: [
          c({{ id: 't_def42', phase: 'define', status: 'done', model: 'claude-opus-4-1', title: '[define] Spec & holdout checks for /v1/events rate limiting', created_at: T(1400), started_at: T(1395), completed_at: T(1210), active_duration_seconds: 185 }}),
          c({{ id: 't_des42', phase: 'design', status: 'done', parents: ['t_def42'], title: '[design] Quota badge states & warning threshold preview', created_at: T(1200), started_at: T(1195), completed_at: T(950), active_duration_seconds: 210 }}),
          c({{ id: 't_imp42a', phase: 'implement', status: 'done', parents: ['t_def42', 't_des42'], pr: PR, title: '[implement] Token-bucket middleware & UI quota badge', created_at: T(940), started_at: T(935), completed_at: T(300), active_duration_seconds: 512 }}),
          c({{ id: 't_vd42', phase: 'verify-diff', status: 'running', provider: 'openai', model: 'gpt-5', parents: ['t_imp42a'], pr: PR, title: '[verify-diff] Review PR #42 against spec.md', created_at: T(290), started_at: T(285), active_duration_seconds: 84 }}),
          c({{ id: 't_vb42', phase: 'verify-blackbox', status: 'running', provider: 'openai', model: 'gpt-5', parents: ['t_imp42a'], pr: PR, title: '[verify-blackbox] Run holdout/ suite against live build', created_at: T(290), started_at: T(285), active_duration_seconds: 102 }}),
        ],
      }},
      {{
        badge: 'STEP 5 / 8',
        caption: 'Information Barrier in action: Diff Check PASSES, while Black-Box catches a burst-concurrency bug!',
        pill: {{ dot: 'fc-dot-red', text: 'Factonio 0 run · 1 attn' }},
        chat: `
          <div class="hd-msg"><div class="hd-msg-user">Add token-bucket rate limiting to <code>POST /v1/events</code> with a status header and UI quota badge (issue #42).</div></div>
          <div class="hd-msg"><div class="hd-msg-role">Hermes · factonio:orchestrator</div><div class="hd-msg-bot">
            <div class="hd-tool-chip"><span class="dot"></span>verify-diff · PASS (PR #42)</div>
            <div class="hd-tool-chip"><span class="dot err"></span>verify-blackbox · REJECT (observable failure on concurrent burst)</div>
            <div>Black-Box Verification caught a race condition under simultaneous requests: two burst calls both received <code>200 OK</code> when only 1 token remained, and <code>Retry-After</code> was <code>0</code>.</div>
            <div style="color:var(--ui-text-secondary);">Dispatching bounded Fix Round 1 (reporting observable symptom only — never leaking <code>holdout/</code>).</div>
          </div></div>`,
        cards: [
          c({{ id: 't_def42', phase: 'define', status: 'done', model: 'claude-opus-4-1', title: '[define] Spec & holdout checks for /v1/events rate limiting', created_at: T(1700), active_duration_seconds: 185 }}),
          c({{ id: 't_des42', phase: 'design', status: 'done', parents: ['t_def42'], title: '[design] Quota badge states & warning threshold preview', created_at: T(1500), active_duration_seconds: 210 }}),
          c({{ id: 't_imp42a', phase: 'implement', status: 'done', parents: ['t_def42', 't_des42'], pr: PR, title: '[implement] Token-bucket middleware & UI quota badge', created_at: T(1200), active_duration_seconds: 512 }}),
          c({{ id: 't_vd42', phase: 'verify-diff', status: 'done', verdict: 'pass', provider: 'openai', model: 'gpt-5', parents: ['t_imp42a'], pr: PR, title: '[verify-diff] Review PR #42 against spec.md', created_at: T(600), active_duration_seconds: 128 }}),
          c({{ id: 't_vb42', phase: 'verify-blackbox', status: 'done', verdict: 'reject', last_outcome: 'changes_requested', provider: 'openai', model: 'gpt-5', parents: ['t_imp42a'], pr: PR, title: '[verify-blackbox] Run holdout/ suite against live build', created_at: T(600), active_duration_seconds: 214, summary: 'Concurrent burst overshoots quota by +1 and returns Retry-After: 0.' }}),
        ],
      }},
      {{
        badge: 'STEP 6 / 8',
        caption: 'Bounded Fix Round 1 fixes the atomic token debit on PR #42 and pushes a new HEAD',
        pill: {{ dot: 'fc-dot-green', text: 'Factonio 1 run · 1 attn' }},
        chat: `
          <div class="hd-msg"><div class="hd-msg-user">Add token-bucket rate limiting to <code>POST /v1/events</code> with a status header and UI quota badge (issue #42).</div></div>
          <div class="hd-msg"><div class="hd-msg-role">Hermes · factonio:orchestrator</div><div class="hd-msg-bot">
            <div class="hd-tool-chip"><span class="dot run"></span>factonio_spawn_implement · Fix Round 1 / 3 · PR #42</div>
            <div>Worker is patching token consumption with an atomic lock + regression test for concurrent burst requests and clamping <code>Retry-After &gt;= 1</code>.</div>
          </div></div>`,
        cards: [
          c({{ id: 't_def42', phase: 'define', status: 'done', model: 'claude-opus-4-1', title: '[define] Spec & holdout checks for /v1/events rate limiting', created_at: T(1900), active_duration_seconds: 185 }}),
          c({{ id: 't_des42', phase: 'design', status: 'done', parents: ['t_def42'], title: '[design] Quota badge states & warning threshold preview', created_at: T(1700), active_duration_seconds: 210 }}),
          c({{ id: 't_imp42a', phase: 'implement', status: 'done', parents: ['t_def42', 't_des42'], pr: PR, title: '[implement] Token-bucket middleware & UI quota badge', created_at: T(1400), active_duration_seconds: 512 }}),
          c({{ id: 't_vd42', phase: 'verify-diff', status: 'done', verdict: 'pass', provider: 'openai', model: 'gpt-5', parents: ['t_imp42a'], pr: PR, title: '[verify-diff] Review PR #42 (round 1)', created_at: T(800), active_duration_seconds: 128 }}),
          c({{ id: 't_vb42', phase: 'verify-blackbox', status: 'done', verdict: 'reject', last_outcome: 'changes_requested', provider: 'openai', model: 'gpt-5', parents: ['t_imp42a'], pr: PR, title: '[verify-blackbox] Holdout acceptance (round 1)', created_at: T(800), active_duration_seconds: 214, summary: 'Concurrent burst overshoots quota by +1 and returns Retry-After: 0.' }}),
          c({{ id: 't_imp42fix', phase: 'implement', status: 'running', runs: 2, parents: ['t_vb42'], pr: PR, title: '[implement] Fix round 1: atomic debit & Retry-After clamp', created_at: T(300), started_at: T(295), active_duration_seconds: 145 }}),
        ],
      }},
      {{
        badge: 'STEP 7 / 8',
        caption: 'Re-Verify: Both Diff Check and Black-Box Check PASS on exact HEAD → CI green → Auto squash-merged!',
        pill: {{ dot: 'fc-dot-muted', text: 'Factonio idle' }},
        chat: `
          <div class="hd-msg"><div class="hd-msg-user">Add token-bucket rate limiting to <code>POST /v1/events</code> with a status header and UI quota badge (issue #42).</div></div>
          <div class="hd-msg"><div class="hd-msg-role">Hermes · factonio:orchestrator</div><div class="hd-msg-bot">
            <div class="hd-tool-chip"><span class="dot"></span>verify-diff PASS + verify-blackbox PASS (round 2 · head f8b30d9)</div>
            <div class="hd-tool-chip"><span class="dot"></span>factonio_pr_gate → ready_to_merge: true (CI green, exact HEAD match)</div>
            <div class="hd-tool-chip"><span class="dot"></span>factonio_merge_pr → squash-merged acme/payments-api#42</div>
            <div><strong>Shipped!</strong> PR #42 passed GitHub CI, independent Diff Check, and hidden Black-Box Check on commit <code>f8b30d9</code> and was squash-merged to <code>main</code>.</div>
          </div></div>`,
        cards: [
          c({{ id: 't_def42', phase: 'define', status: 'done', model: 'claude-opus-4-1', title: '[define] Spec & holdout checks for /v1/events rate limiting', created_at: T(2400), active_duration_seconds: 185 }}),
          c({{ id: 't_des42', phase: 'design', status: 'done', parents: ['t_def42'], title: '[design] Quota badge states & warning threshold preview', created_at: T(2200), active_duration_seconds: 210 }}),
          c({{ id: 't_imp42a', phase: 'implement', status: 'done', parents: ['t_def42', 't_des42'], pr: PR, title: '[implement] Token-bucket middleware & UI quota badge', created_at: T(1900), active_duration_seconds: 512 }}),
          c({{ id: 't_vd42', phase: 'verify-diff', status: 'done', verdict: 'pass', provider: 'openai', model: 'gpt-5', parents: ['t_imp42a'], pr: PR, title: '[verify-diff] Review PR #42 (round 1)', created_at: T(1300), active_duration_seconds: 128 }}),
          c({{ id: 't_vb42', phase: 'verify-blackbox', status: 'done', verdict: 'reject', last_outcome: 'changes_requested', provider: 'openai', model: 'gpt-5', parents: ['t_imp42a'], pr: PR, title: '[verify-blackbox] Holdout acceptance (round 1)', created_at: T(1300), active_duration_seconds: 214 }}),
          c({{ id: 't_imp42fix', phase: 'implement', status: 'done', runs: 2, parents: ['t_vb42'], pr: PR, title: '[implement] Fix round 1: atomic debit & Retry-After clamp', created_at: T(800), active_duration_seconds: 192, summary: 'Serialized bucket debit + added concurrency test.' }}),
          c({{ id: 't_vd42r2', phase: 'verify-diff', status: 'done', verdict: 'pass', provider: 'openai', model: 'gpt-5', parents: ['t_imp42fix'], pr: PR, title: '[verify-diff] Re-check PR #42 (head f8b30d9)', created_at: T(400), active_duration_seconds: 94, summary: 'Clean minimal diff; lock scope and headers match spec.md.' }}),
          c({{ id: 't_vb42r2', phase: 'verify-blackbox', status: 'done', verdict: 'pass', provider: 'openai', model: 'gpt-5', parents: ['t_imp42fix'], pr: PR, title: '[verify-blackbox] Holdout acceptance (round 2)', created_at: T(400), started_at: T(392), completed_at: T(206), elapsed_seconds: 186, active_duration_seconds: 186, summary: 'All 4 holdout checks pass, including 32-way concurrent burst.' }}),
        ],
      }},
      {{
        badge: 'STEP 8 / 8',
        caption: 'Inspect every card’s PR link, cross-model verdict, runs, duration & summary in the sidebar drawer',
        pill: {{ dot: 'fc-dot-muted', text: 'Factonio idle' }},
        expandCompleted: true,
        openCardId: 't_vb42r2',
        chat: `
          <div class="hd-msg"><div class="hd-msg-user">Add token-bucket rate limiting to <code>POST /v1/events</code> with a status header and UI quota badge (issue #42).</div></div>
          <div class="hd-msg"><div class="hd-msg-role">Hermes · factonio:orchestrator</div><div class="hd-msg-bot">
            <div class="hd-tool-chip"><span class="dot"></span>verify-diff PASS + verify-blackbox PASS (round 2 · head f8b30d9)</div>
            <div class="hd-tool-chip"><span class="dot"></span>factonio_pr_gate → ready_to_merge: true (CI green, exact HEAD match)</div>
            <div class="hd-tool-chip"><span class="dot"></span>factonio_merge_pr → squash-merged acme/payments-api#42</div>
            <div><strong>Shipped!</strong> Click any card in the Factonio sidebar to inspect its full phase history, verification verdict, active duration, and direct PR link.</div>
          </div></div>`,
        cards: null, // reuses step 7 cards
      }},
    ];
    STEPS[7].cards = STEPS[6].cards;

    let currentCards = STEPS[0].cards;
    window.__fcMock = {{ sessionId: 'sess_demo', visible: true, __version: 1 }};

    const {{ regs }} = await bootPlugin((path) => {{
      if (path.startsWith('/cards')) return {{ cards: currentCards, counts: {{}} }};
      return {{}};
    }});

    __mount(document.getElementById('pane-mount'), () => regs.pane.render());

    function renderChatAndPill(step) {{
      const chat = document.getElementById('chat-area');
      chat.innerHTML = step.chat + `<div class="hd-step-banner"><span>${{step.caption}}</span><span class="hd-step-badge">${{step.badge}}</span></div>`;
      const pill = document.getElementById('pill-mount');
      pill.innerHTML = `<button type="button" class="fc-pill"><span class="fc-dot ${{step.pill.dot}}"></span><span>${{step.pill.text}}</span></button>`;
    }}

    window.__demo = {{
      count: STEPS.length,
      async setStep(i) {{
        const step = STEPS[i];
        currentCards = step.cards;
        window.__fcMock.__version++;
        renderChatAndPill(step);
        __rerender();
        await new Promise(r => setTimeout(r, 40));
        if (step.expandCompleted) {{
          // Click the Verify phase filter so the verification cards + expanded drawer sit front-and-center
          const verifyBtn = [...document.querySelectorAll('.fc-sb-seg button')].find(b => b.textContent.trim() === 'Verify');
          if (verifyBtn) verifyBtn.click();
          await new Promise(r => setTimeout(r, 50));
          const wsToggle = document.querySelector('[data-action="toggle-ws"]');
          if (wsToggle && wsToggle.getAttribute('aria-expanded') === 'false') wsToggle.click();
          await new Promise(r => setTimeout(r, 50));
          const doneBtn = document.querySelector('[data-action="toggle-done"]');
          if (doneBtn && doneBtn.getAttribute('aria-expanded') === 'false') doneBtn.click();
          await new Promise(r => setTimeout(r, 50));
        }}
        if (step.openCardId) {{
          const cardEl = document.querySelector(`article[data-id="${{step.openCardId}}"]`);
          const toggleBtn = cardEl?.querySelector('button[data-action="toggle-card"]');
          if (toggleBtn && toggleBtn.getAttribute('aria-expanded') !== 'true') toggleBtn.click();
          await new Promise(r => setTimeout(r, 80));
          const sbBody = document.querySelector('.fc-sb-body');
          if (sbBody) sbBody.scrollTop = sbBody.scrollHeight;
        }}
        await new Promise(r => setTimeout(r, 30));
      }}
    }};

    const stepParam = Number(new URLSearchParams(location.search).get('step') ?? '0');
    await window.__demo.setStep(Number.isFinite(stepParam) ? Math.max(0, Math.min(STEPS.length - 1, stepParam)) : 0);
    window.__ready = true;
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Scene 5: Animated Inline Repo Editor Demo (real desktop/plugin.js PATCH flow)
# ---------------------------------------------------------------------------

REPO_EDITOR_DEMO_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Factonio Inline Repo Settings Demo</title>
  <style>{DESKTOP_SHELL_CSS}
    .fc-head, .fc-grid, .fc-sec:not(.fc-repos-sec) {{ display: none !important; }}
    .fc-wrap {{ padding: 18px 24px 28px !important; }}
    .fc-repos-sec {{ margin-top: 0 !important; }}
  </style>
  {BOOT_SNIPPET}
</head>
<body>
  <div class="hd-frame">
    <div class="hd-titlebar">
      <div class="hd-dots"><span></span><span></span><span></span></div>
      <span>Hermes Desktop</span>
      <span class="hd-crumb-sep">/</span>
      <span class="hd-crumb">Factonio · Registered Repositories</span>
      <span class="hd-profile-pill">PATCH /api/plugins/factonio/repos/:slug</span>
    </div>
    <div class="hd-main">
      <main class="hd-content" id="page-mount"></main>
    </div>
    <div class="hd-statusbar">
      <div class="hd-statusbar-left">
        <span id="demo-caption">Live per-repository settings · dashed = global default · solid = override</span>
      </div>
      <div class="hd-statusbar-right">
        <span id="demo-badge" class="hd-step-badge">STEP 1 / 6</span>
      </div>
    </div>
  </div>
  <div id="toast-root"></div>
  <script type="module">
    import {{ __mount, __rerender }} from 'react';
    import {{ MODELS_DATA, METRICS_BY_RANGE, initialReposState, bootPlugin }} from '/__harness/mock-data.js';

    let reposState = initialReposState();
    window.__fcMock = {{ sessionId: 'sess_ed', visible: true, __version: 1 }};

    window.__fcOnNotify = (n) => {{
      const el = document.getElementById('toast-root');
      el.innerHTML = `<div class="hd-toast">✓ ${{n.message}}</div>`;
    }};

    const {{ regs }} = await bootPlugin((path, opts) => {{
      if (path.startsWith('/metrics')) return METRICS_BY_RANGE['30d'];
      if (path === '/models') return MODELS_DATA;
      if (path === '/repos' && (!opts || opts.method !== 'PATCH')) return reposState;
      if (path.startsWith('/repos/') && opts?.method === 'PATCH') {{
        const slug = decodeURIComponent(path.slice('/repos/'.length));
        const body = opts.body || {{}};
        let updated = null;
        reposState = {{
          ...reposState,
          repos: reposState.repos.map(r => {{
            if (r.slug !== slug) return r;
            const next = {{ ...r, ...body }};
            next.effective_design_provider = next.design_provider || reposState.defaults.design_provider;
            next.effective_design_approval = next.design_approval || reposState.defaults.design_approval;
            next.resolved_design_provider = next.effective_design_provider === 'auto' ? 'claude-design' : next.effective_design_provider;
            updated = next;
            return next;
          }})
        }};
        window.__fcMock.__version++;
        return Promise.resolve({{ ok: true, repo: updated }});
      }}
      return {{}};
    }});

    __mount(document.getElementById('page-mount'), () => regs.page.render());

    function scrollToRepos() {{
      const sec = document.querySelector('.fc-repos-sec');
      const page = document.querySelector('.fc-page');
      if (sec && page) {{
        page.scrollTop = Math.max(0, sec.offsetTop - 20);
      }}
    }}

    function setCaption(step, text) {{
      document.getElementById('demo-badge').textContent = `STEP ${{step}} / 6`;
      document.getElementById('demo-caption').textContent = text;
    }}

    function setControlValue(name, value) {{
      const form = document.querySelector('form.fc-ed');
      if (!form) return;
      const el = form.querySelector(`[id$="-${{name}}"]`);
      if (!el) return;
      el.value = value;
      el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', {{ bubbles: true }}));
    }}

    window.__demo = {{
      count: 6,
      async setStep(i) {{
        await new Promise(r => setTimeout(r, 40));
        if (i === 0) {{
          document.getElementById('toast-root').innerHTML = '';
          setCaption(1, 'Inspect per-repository settings — dashed pills inherit global defaults, solid pills are repo overrides');
          scrollToRepos();
        }} else if (i === 1) {{
          setCaption(2, 'Click Edit on omnim-ai/factonio to open the inline editor beneath the row');
          const btn = document.querySelector('tr[data-slug="omnim-ai-factonio"] button[data-action="edit-repo"]');
          if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click();
          await new Promise(r => setTimeout(r, 40));
          scrollToRepos();
        }} else if (i === 2) {{
          setCaption(3, 'Change Merge policy from auto → ask (wait for human sign-off before squash-merging)');
          setControlValue('merge_policy', 'ask');
          await new Promise(r => setTimeout(r, 40));
          scrollToRepos();
        }} else if (i === 3) {{
          setCaption(4, 'Raise Max fix rounds from 3 → 5 for bounded verification repair loops');
          setControlValue('max_fix_rounds', '5');
          await new Promise(r => setTimeout(r, 40));
          scrollToRepos();
        }} else if (i === 4) {{
          setCaption(5, 'Override Design provider to hermes — live counter tracks 3 unsaved changes');
          setControlValue('design_provider', 'hermes');
          await new Promise(r => setTimeout(r, 40));
          scrollToRepos();
        }} else if (i === 5) {{
          setCaption(6, 'Save sends PATCH with only changed fields — table updates live with solid override badge');
          const saveBtn = document.querySelector('button[data-action="save-repo"]');
          if (saveBtn) saveBtn.click();
          await new Promise(r => setTimeout(r, 80));
          scrollToRepos();
        }}
        await new Promise(r => setTimeout(r, 30));
      }}
    }};

    const targetStep = Number(new URLSearchParams(location.search).get('step') ?? '0');
    const clamped = Number.isFinite(targetStep) ? Math.max(0, Math.min(5, targetStep)) : 0;
    for (let s = 0; s <= clamped; s++) {{
      await window.__demo.setStep(s);
    }}
    window.__ready = true;
  </script>
</body>
</html>
"""

VIRTUAL_ROUTES: dict[str, tuple[str, str]] = {
    "/__harness/react.js": ("application/javascript; charset=utf-8", REACT_SHIM_JS),
    "/__harness/jsx-runtime.js": ("application/javascript; charset=utf-8", JSX_RUNTIME_SHIM_JS),
    "/__harness/plugin-sdk.js": ("application/javascript; charset=utf-8", PLUGIN_SDK_SHIM_JS),
    "/__harness/mock-data.js": ("application/javascript; charset=utf-8", SHARED_MOCK_DATA_JS),
    "/__harness/dashboard.html": ("text/html; charset=utf-8", DASHBOARD_HTML),
    "/__harness/sidebar.html": ("text/html; charset=utf-8", SIDEBAR_HTML),
    "/__harness/settings.html": ("text/html; charset=utf-8", SETTINGS_HTML),
    "/__harness/workflow-demo.html": ("text/html; charset=utf-8", WORKFLOW_DEMO_HTML),
    "/__harness/repo-editor-demo.html": ("text/html; charset=utf-8", REPO_EDITOR_DEMO_HTML),
}


class HarnessHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in VIRTUAL_ROUTES:
            ctype, body = VIRTUAL_ROUTES[path]
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        rel = path.lstrip("/")
        target = (REPO_ROOT / rel).resolve()
        if not str(target).startswith(str(REPO_ROOT)) or not target.is_file():
            self.send_error(404)
            return
        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def encode_gif(frame_paths: list[Path], durations_s: list[float], out_gif: Path, width: int = 960) -> None:
    """Encode PNG frames into a high-quality, compact 2-pass palette GIF via ffmpeg."""
    concat_file = out_gif.with_suffix(".concat.txt")
    palette_file = out_gif.with_suffix(".palette.png")
    lines: list[str] = []
    for p, dur in zip(frame_paths, durations_s):
        lines.append(f"file '{p.resolve()}'")
        lines.append(f"duration {dur:.2f}")
    # Repeat last frame per ffmpeg concat demuxer requirement
    lines.append(f"file '{frame_paths[-1].resolve()}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-vf", f"scale={width}:-1:flags=lanczos,palettegen=max_colors=256:stats_mode=diff",
                str(palette_file),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-i", str(palette_file),
                "-lavfi", f"scale={width}:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=sierra2_4a",
                "-loop", "0",
                str(out_gif),
            ],
            check=True,
            capture_output=True,
        )
    finally:
        concat_file.unlink(missing_ok=True)
        palette_file.unlink(missing_ok=True)


def serve(port: int) -> None:
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), HarnessHandler) as httpd:
        print(f"Serving Factonio Desktop capture harness on http://127.0.0.1:{port}", flush=True)
        for route in sorted(VIRTUAL_ROUTES):
            if route.endswith(".html"):
                print(f"  http://127.0.0.1:{port}{route}", flush=True)
        httpd.serve_forever()


VALID_TARGETS = ("dashboard", "sidebar", "settings", "workflow", "repo-editor", "architecture")
DEFAULT_CAPTURE_TARGETS = ("dashboard", "sidebar", "settings", "workflow", "repo-editor")
GIF_TARGETS = frozenset({"workflow", "repo-editor"})


def find_chrome() -> str:
    """Locate a headless Chromium / Chrome binary across Linux, macOS, and Playwright caches."""
    env_chrome = os.environ.get("CHROME_BIN", "").strip()
    if env_chrome:
        p = Path(env_chrome).expanduser()
        if p.is_file():
            return str(p)
        raise FileNotFoundError(f"CHROME_BIN points to a missing file: {env_chrome}")

    for name in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome-headless-shell",
        "brave-browser",
        "microsoft-edge",
    ):
        hit = shutil.which(name)
        if hit:
            return hit

    mac_paths = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    )
    for mp in mac_paths:
        if Path(mp).is_file():
            return mp

    cache_roots = (
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
    )
    candidates: list[str] = []
    for root in cache_roots:
        if root.is_dir():
            candidates.extend(glob.glob(str(root / "chromium*" / "**" / "chrome-headless-shell"), recursive=True))
            candidates.extend(glob.glob(str(root / "chromium*" / "**" / "chrome"), recursive=True))
            candidates.extend(glob.glob(str(root / "chromium*" / "**" / "Google Chrome for Testing"), recursive=True))
    for path in sorted(candidates, reverse=True):
        if Path(path).is_file():
            return path
    raise FileNotFoundError(
        "Could not find Chrome/Chromium. Install chromium/google-chrome, run `npx playwright install chromium`, "
        "or set CHROME_BIN=/path/to/chrome."
    )


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def check_deps() -> int:
    """Report availability of headless Chrome/Chromium and ffmpeg."""
    ok = True
    try:
        chrome = find_chrome()
        print(f"  [OK  ] chrome: {chrome}")
    except FileNotFoundError as exc:
        print(f"  [FAIL] chrome: {exc}")
        ok = False

    ffmpeg = find_ffmpeg()
    if ffmpeg:
        print(f"  [OK  ] ffmpeg: {ffmpeg}")
    else:
        print("  [WARN] ffmpeg: not found on PATH (needed only for GIF targets: workflow, repo-editor)")
        print("         Install with: brew install ffmpeg | sudo apt install ffmpeg | sudo dnf install ffmpeg")
    return 0 if ok else 1


def _verify_image(path: Path, expected_magic: bytes) -> None:
    data = path.read_bytes()
    if len(data) < len(expected_magic) or not data.startswith(expected_magic):
        raise RuntimeError(f"Generated file {path} failed {expected_magic!r} header validation ({len(data)} bytes)")


def parse_targets(raw_only: list[str] | None) -> list[str]:
    if not raw_only:
        return list(DEFAULT_CAPTURE_TARGETS)
    selected: list[str] = []
    for item in raw_only:
        for tok in item.split(","):
            name = tok.strip().lower()
            if not name:
                continue
            if name == "all":
                for t in DEFAULT_CAPTURE_TARGETS:
                    if t not in selected:
                        selected.append(t)
                continue
            if name not in VALID_TARGETS:
                raise ValueError(
                    f"Unknown target {name!r}. Valid targets: all, {', '.join(VALID_TARGETS)}"
                )
            if name not in selected:
                selected.append(name)
    return selected or list(DEFAULT_CAPTURE_TARGETS)


def capture_url(chrome: str, url: str, dest: Path, width: int, height: int, scale: int = 2) -> None:
    """Render a URL in headless Chromium and save a crisp PNG screenshot."""
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--force-device-scale-factor={scale}",
            f"--window-size={width},{height}",
            "--virtual-time-budget=2500",
            f"--screenshot={dest}",
            url,
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    _verify_image(dest, b"\x89PNG")


def capture_selected(out_dir: Path, targets: list[str], qa_dir: Path | None = None) -> None:
    """Render the requested static screenshots and animated GIFs into `out_dir`."""
    chrome = find_chrome()
    if any(t in GIF_TARGETS for t in targets) and not find_ffmpeg():
        raise FileNotFoundError(
            "ffmpeg is required to encode GIF targets (workflow, repo-editor). "
            "Install ffmpeg or restrict capture to static PNGs with --only dashboard,sidebar,settings."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    if qa_dir is not None:
        qa_dir.mkdir(parents=True, exist_ok=True)

    class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = ThreadedServer(("127.0.0.1", 0), HarnessHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        if "dashboard" in targets:
            dest = out_dir / "dashboard.png"
            capture_url(chrome, f"{base}/__harness/dashboard.html", dest, 1240, 1100, scale=2)
            print(f"Wrote {dest} ({dest.stat().st_size} bytes)")
        if "sidebar" in targets:
            dest = out_dir / "sidebar.png"
            capture_url(chrome, f"{base}/__harness/sidebar.html", dest, 1100, 770, scale=2)
            print(f"Wrote {dest} ({dest.stat().st_size} bytes)")
        if "settings" in targets:
            dest = out_dir / "settings.png"
            capture_url(chrome, f"{base}/__harness/settings.html", dest, 980, 680, scale=2)
            print(f"Wrote {dest} ({dest.stat().st_size} bytes)")
        if "architecture" in targets or qa_dir is not None:
            arch_dest = (qa_dir or out_dir) / "architecture.png"
            if "architecture" in targets or (REPO_ROOT / "docs" / "images" / "architecture.svg").is_file():
                capture_url(chrome, f"{base}/docs/images/architecture.svg", arch_dest, 1080, 530, scale=2)
                print(f"Wrote QA preview {arch_dest} ({arch_dest.stat().st_size} bytes)")

        if any(t in GIF_TARGETS for t in targets):
            with tempfile.TemporaryDirectory(prefix="factonio-media-") as tmp:
                tmp_path = Path(tmp)

                if "workflow" in targets:
                    wf_dir = tmp_path / "workflow"
                    wf_dir.mkdir()
                    wf_frames: list[Path] = []
                    for i in range(8):
                        frame = wf_dir / f"frame_{i:02d}.png"
                        capture_url(chrome, f"{base}/__harness/workflow-demo.html?step={i}", frame, 960, 660, scale=2)
                        wf_frames.append(frame)
                    out_gif = out_dir / "demo-workflow.gif"
                    encode_gif(wf_frames, [2.2, 2.0, 2.0, 2.2, 3.0, 2.0, 2.5, 3.2], out_gif, width=960)
                    _verify_image(out_gif, b"GIF89a")
                    print(f"Wrote {out_gif} ({out_gif.stat().st_size} bytes)")
                    if qa_dir is not None:
                        for idx in (0, 4, 7):
                            qa_dest = qa_dir / f"workflow_step{idx}.png"
                            shutil.copy2(wf_frames[idx], qa_dest)
                            print(f"Wrote QA keyframe {qa_dest}")

                if "repo-editor" in targets:
                    re_dir = tmp_path / "repo-editor"
                    re_dir.mkdir()
                    re_frames: list[Path] = []
                    for i in range(6):
                        frame = re_dir / f"frame_{i:02d}.png"
                        capture_url(chrome, f"{base}/__harness/repo-editor-demo.html?step={i}", frame, 960, 580, scale=2)
                        re_frames.append(frame)
                    out_gif = out_dir / "demo-repo-editor.gif"
                    encode_gif(re_frames, [2.0, 1.8, 1.5, 1.5, 2.0, 3.0], out_gif, width=960)
                    _verify_image(out_gif, b"GIF89a")
                    print(f"Wrote {out_gif} ({out_gif.stat().st_size} bytes)")
                    if qa_dir is not None:
                        for idx in (0, 4, 5):
                            qa_dest = qa_dir / f"repo-editor_step{idx}.png"
                            shutil.copy2(re_frames[idx], qa_dest)
                            print(f"Wrote QA keyframe {qa_dest}")
    finally:
        httpd.shutdown()


def capture_all(out_dir: Path) -> None:
    """Backwards-compatible helper capturing all README PNGs and GIFs."""
    capture_selected(out_dir, list(DEFAULT_CAPTURE_TARGETS))


def main() -> int:
    parser = argparse.ArgumentParser(description="Factonio README media harness and GIF encoder.")
    parser.add_argument("--check-deps", action="store_true", help="Check Chrome/Chromium and ffmpeg availability.")
    parser.add_argument("--capture", action="store_true", help="Capture README screenshots and animated GIFs.")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="Comma-separated target(s) for --capture: all, dashboard, sidebar, settings, workflow, repo-editor, architecture.",
    )
    parser.add_argument(
        "--qa-dir",
        type=Path,
        default=None,
        help="Optional scratch directory where GIF keyframe PNGs and architecture.svg PNG preview are exported for visual QA.",
    )
    parser.add_argument("--serve-only", action="store_true", help="Serve the live HTML harness for browser inspection/capture.")
    parser.add_argument("--port", type=int, default=18432, help="Loopback port (default: 18432).")
    parser.add_argument("--encode-gifs", action="store_true", help="Encode GIFs from captured PNG frames.")
    parser.add_argument("--frames-dir", type=Path, default=None, help="Directory containing workflow/ and repo-editor/ PNG frames.")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "images", help="Output directory for PNGs and GIFs.")
    args = parser.parse_args()

    if args.check_deps:
        return check_deps()

    if args.capture:
        try:
            targets = parse_targets(args.only)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        try:
            capture_selected(args.out, targets, qa_dir=args.qa_dir)
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.serve_only:
        serve(args.port)
        return 0

    if args.encode_gifs:
        if not args.frames_dir:
            print("Error: --frames-dir is required with --encode-gifs", file=sys.stderr)
            return 2
        args.out.mkdir(parents=True, exist_ok=True)
        wf_frames = sorted((args.frames_dir / "workflow").glob("frame_*.png"))
        if wf_frames:
            wf_durs = [2.1, 2.0, 2.0, 2.2, 2.8, 2.0, 2.4, 3.0][: len(wf_frames)]
            encode_gif(wf_frames, wf_durs, args.out / "demo-workflow.gif", width=960)
            print(f"Wrote {args.out / 'demo-workflow.gif'} ({(args.out / 'demo-workflow.gif').stat().st_size} bytes)")
        re_frames = sorted((args.frames_dir / "repo-editor").glob("frame_*.png"))
        if re_frames:
            re_durs = [2.0, 1.8, 1.5, 1.5, 2.0, 2.8][: len(re_frames)]
            encode_gif(re_frames, re_durs, args.out / "demo-repo-editor.gif", width=960)
            print(f"Wrote {args.out / 'demo-repo-editor.gif'} ({(args.out / 'demo-repo-editor.gif').stat().st_size} bytes)")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
