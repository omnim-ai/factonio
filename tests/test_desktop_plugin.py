"""Clean-room tests for the Factonio Desktop plugin (desktop/plugin.js).

Covers the Settings → Plugins → Factonio landing page registration, the
Auxiliary Models deep-links on both the settings page and the /factonio
dashboard, older-host compatibility, and Desktop SDK surface lint.

The smoke test executes ``desktop/plugin.js`` under Node's ESM loader with
``node:module`` ``registerHooks`` stubbing ``@hermes/plugin-sdk``, ``react``
and ``react/jsx-runtime`` in memory — no build step, no node_modules, and no
edits to the plugin source.
"""

from __future__ import annotations

import json
import os
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
    export function useValue(v) { return v }
  `,
  'react': `
    export function useState(init) { return [typeof init === 'function' ? init() : init, () => {}] }
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
globalThis.__fcHost = {
  navigate: p => navigations.push(p),
  notify: () => {},
  composer: { submit: () => {} },
  state: { focusedStoredSessionId: null }
}
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
globalThis.__fcUseQuery = opts => {
  const key = opts.queryKey.join('/')
  if (key === 'factonio/models') {
    globalThis.__fcModelsQueries.push({ key: opts.queryKey, interval: opts.refetchInterval, fn: typeof opts.queryFn })
    return queryState
  }
  return { data: undefined, isLoading: true, isError: false }
}
globalThis.__fcModelsQueries = []

function expand(node) {
  if (node == null || node === false || node === true) return null
  if (Array.isArray(node)) return node.map(expand)
  if (typeof node !== 'object') return node
  if (typeof node.type === 'function') return expand(node.type(node.props || {}))
  const props = { ...(node.props || {}) }
  props.children = expand(props.children)
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


if __name__ == "__main__":
    unittest.main()
