"""Layer 0 canonical vocabularies and holdout-barrier text helpers (stdlib only, no package imports).

Every Factonio module (and the standalone ``python3 -S tools/eval_skills.py`` loader) imports these
names from here, so each vocabulary has exactly one definition. Legacy modules re-export them.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Mapping, Optional

PLUGIN_ID = "factonio"
WORKER_PROFILE = "factonio"

PHASES: tuple[str, ...] = ("define", "design", "implement", "verify", "watchdog")

DESIGN_PROVIDERS: tuple[str, ...] = ("auto", "claude-design", "hermes", "off")
DESIGN_APPROVAL_POLICIES: tuple[str, ...] = ("ask", "auto")
MERGE_POLICIES: tuple[str, ...] = ("auto", "ask")
PUBLISH_POLICIES: tuple[str, ...] = ("auto", "ask")
RIGOR_LEVELS: tuple[str, ...] = ("standard", "high")
WORKSTREAM_KINDS: tuple[str, ...] = ("feature", "bug", "quick", "refactor", "perf", "investigate")
ARENA_TARGETS: tuple[str, ...] = ("define", "design")
HOOK_KINDS: tuple[str, ...] = ("visual_diff", "invariant", "custom")

# Every `trail.db` decision phase a human (or tool) may record.
TRAIL_PHASES: tuple[str, ...] = (
    "define", "design", "implement", "verify", "publish", "gate", "merge", "intake", "harness", "eval",
    "escape", "watchdog", "retro",
)
TRAIL_SIGNALS: tuple[str, ...] = ("all", "key", "attention")
AUDIT_FINDING_KINDS: tuple[str, ...] = ("missing_evidence", "weak_evidence", "unclosed_escalation", "gate_loosening")
RECALL_CATEGORIES: tuple[str, ...] = ("escapes", "superseded", "rejects_and_blocks", "keyword")

TRIAGE_ACTIONS: tuple[str, ...] = ("triage", "verify_existing_fix")
FIX_VERDICTS: tuple[str, ...] = ("verified_fix", "fix_incomplete", "not_reproduced", "flaky")
WORKTREE_BUCKETS: tuple[str, ...] = ("hold-wip", "hold-active-card", "hold-open-pr", "safe", "review")
INACTIVE_CARD_STATUSES: tuple[str, ...] = ("done", "archived", "cancelled")

VERIFY_PHASES: frozenset[str] = frozenset({"verify-diff", "verify-blackbox"})
PASS_VERDICTS: frozenset[str] = frozenset({"pass"})
REJECT_VERDICTS: frozenset[str] = frozenset({"reject", "fail", "block", "changes_requested"})
# Tri-state verification: `inconclusive` is never a pass and always needs attention.
INCONCLUSIVE_VERDICTS: frozenset[str] = frozenset({"inconclusive"})

GATE_LOOSENING_TOKENS: tuple[str, ...] = (
    "gate_loosening", "skip_test", "disable_ci", "continue-on-error", "downgrade_hook",
    "allow_stacked_merge", "allow_patch_continuity", "force_merge", "bypass_gate", "skip_gate",
    "weaken_assertion", "raise_timeout",
)

# Vendor families keyed by canonical vendor; values are model-name / provider prefixes.
VENDOR_ALIASES: dict[str, str] = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "openai": "openai",
    "gpt": "openai",
    "chatgpt": "openai",
    "codex": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "azure": "openai",
    "google": "google",
    "gemini": "google",
    "gemma": "google",
    "palm": "google",
    "meta": "meta",
    "llama": "meta",
    "mistral": "mistral",
    "mistralai": "mistral",
    "mixtral": "mistral",
    "codestral": "mistral",
    "devstral": "mistral",
    "magistral": "mistral",
    "ministral": "mistral",
    "pixtral": "mistral",
    "xai": "xai",
    "x-ai": "xai",
    "grok": "xai",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "qwq": "qwen",
    "alibaba": "qwen",
    "dashscope": "qwen",
    "moonshot": "moonshot",
    "moonshotai": "moonshot",
    "kimi": "moonshot",
    "zai": "zhipu",
    "z-ai": "zhipu",
    "zhipu": "zhipu",
    "glm": "zhipu",
    "cohere": "cohere",
    "command": "cohere",
    "amazon": "amazon",
    "nova": "amazon",
    "nous": "nous",
    "nousresearch": "nous",
    "hermes": "nous",
    "minimax": "minimax",
    "microsoft": "microsoft",
    "phi": "microsoft",
    "xiaomi": "xiaomi",
    "mimo": "xiaomi",
    "tencent": "tencent",
    "hunyuan": "tencent",
    "hy3": "tencent",
    "hy4": "tencent",
    "stepfun": "stepfun",
    "step": "stepfun",
    "nvidia": "nvidia",
    "nemotron": "nvidia",
    "sakana": "sakana",
    "fugu": "sakana",
    "upstage": "upstage",
    "solar": "upstage",
    "arcee": "arcee",
    "trinity": "arcee",
}

# Providers that front many vendors; the model id (not the provider) identifies the family.
ROUTER_PROVIDERS: frozenset[str] = frozenset(
    {
        "nous",
        "nous-portal",
        "openrouter",
        "vertex",
        "vertex-ai",
        "vertexai",
        "google-vertex",
        "bedrock",
        "aws-bedrock",
        "azure-foundry",
        "together",
        "fireworks",
        "groq",
        "deepinfra",
        "huggingface",
        "nebius",
        "nvidia",
        "kilocode",
        "gmi",
        "novita",
        "opencode-go",
        "opencode-zen",
        "chutes",
        "cerebras",
        "sambanova",
        "siliconflow",
        "portkey",
        "ollama",
        "lmstudio",
        "litellm",
        "requesty",
        "copilot",
        "github-copilot",
        "ai-gateway",
        "vercel",
        "custom",
    }
)


# --- holdout information barrier ---------------------------------------------------------------

# Recall text can be injected into Definition cards: never echo holdout paths or scenario names.
# Matching runs after NFKC folding with format/control characters removed, so zero-width joiners,
# full-width letters or "hold-out" spellings cannot smuggle a reference past the barrier.
# Whitespace-interleaved spellings ("hol\tdout/x") count only when followed by a path separator, so
# prose such as "hold out for review" stays readable.
HOLDOUT_RE = re.compile(
    r"hold[_./\\-]*outs?|h\s*o\s*l\s*d[\s_./\\-]*o\s*u\s*t\s*s?\s*[/\\]", re.IGNORECASE
)
HOLDOUT_REDACTED = "[redacted]"


# Default-ignorable code points outside the ``C*`` categories (variation selectors, fillers, joiners).
_INVISIBLE = frozenset(
    [0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0, *range(0x180B, 0x1810), *range(0xFE00, 0xFE10),
     *range(0xE0100, 0xE01F0)]
)


# Complete terminal escape sequences (7-bit ESC and 8-bit C1 forms). Deleting only the ESC byte
# would leave printable parameters (``ho[31mldout``) splitting the marker the matcher looks for.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)|\x9d[^\x07\x9c]*(?:\x07|\x9c|$)"  # OSC
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\|$)|[\x90\x98\x9e\x9f][^\x9c]*(?:\x9c|$)"  # DCS/SOS/PM/APC
    r"|\x1b[ -/]*[0-~]"  # other ESC sequences (two-byte Fe/Fp/Fs, nF charset designations)
)
# Bytes that introduce or terminate escape sequences; kept until sequences are stripped.
_ANSI_SYNTAX = frozenset("\x07\x1b\x90\x98\x9b\x9c\x9d\x9e\x9f")


def _drop_controls(text: str, keep: frozenset = frozenset()) -> str:
    out = []
    for ch in text:
        if ch in keep:
            out.append(ch)
        elif ch in "\t\n\r":
            out.append(" " if not keep else ch)
        elif unicodedata.category(ch) in ("Zl", "Zp"):
            out.append(" ")
        elif unicodedata.category(ch).startswith("C") or ord(ch) in _INVISIBLE:
            continue
        else:
            out.append(ch)
    return "".join(out)


def _strip_ansi(text: str) -> str:
    # Repeat until stable: removing one sequence (or an interleaved NUL) can complete another.
    prev = None
    while prev != text:
        prev = text
        text = _ANSI_ESCAPE_RE.sub("", _drop_controls(text, _ANSI_SYNTAX))
    return text


def fold_text(text: str) -> str:
    """Strip terminal escape sequences, NFKC-fold, delete control/format/invisible characters, and
    turn whitespace controls into spaces.

    Deleting (not spacing) ``h\\x00o\\x1bldout`` rejoins the word, and stripping whole sequences
    rejoins ``ho\\x1b[31mldout``, so the redactor and the barrier both see ``holdout``.
    """
    text = _strip_ansi(str(text))
    text = _strip_ansi(unicodedata.normalize("NFKC", text))
    return _drop_controls(text)


def fold_text_plain(text: str) -> str:
    """Fold without sequence stripping (only deletes ESC/C1 bytes); matched alongside ``fold_text``."""
    return _drop_controls(unicodedata.normalize("NFKC", str(text)))


def has_holdout_reference(text: str, folded: Optional[str] = None) -> bool:
    # Fail closed if EITHER reading names holdout: stripping a sequence must never hide a marker
    # that byte-deletion alone would have exposed (e.g. an unterminated OSC swallowing the path).
    folded = fold_text(text) if folded is None else folded
    return bool(HOLDOUT_RE.search(folded) or HOLDOUT_RE.search(fold_text_plain(text)))


def redact_holdout_text(text: str) -> str:
    """Fold ``text``; if it references holdout anywhere, replace the WHOLE string.

    Partial substitution cannot know where a path ends (quoted paths may contain spaces), so any
    trailing fragment could be a scenario name: fail closed on the entire field or line.
    """
    folded = fold_text(text)
    return HOLDOUT_REDACTED if has_holdout_reference(text, folded) else folded


def contains_holdout_reference(text: Any) -> bool:
    """True when ``text`` mentions a holdout path or name (after the same folding the redactor uses)."""
    return has_holdout_reference(str(text or ""))


def scrub_public(value: Any) -> Any:
    """Public serializer: drop holdout-named keys, redact any string referencing holdout, null non-finite floats.

    Strings without a holdout reference pass through byte-for-byte, so ordinary rows are unchanged.
    """
    if isinstance(value, str):
        return HOLDOUT_REDACTED if contains_holdout_reference(value) else value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {k: scrub_public(v) for k, v in value.items() if not contains_holdout_reference(str(k))}
    if isinstance(value, (list, tuple)):
        return [scrub_public(v) for v in value]
    return value


# --- gate-loosening vocabulary -------------------------------------------------------------------

_TOKEN_SEP_RE = re.compile(r"[\s\-]+")


def norm_token_text(text: str) -> str:
    # "skip test", "skip-test" and "skip_test" all name the same gate change.
    return _TOKEN_SEP_RE.sub("_", text.lower())


def scrub_loosening_hints(why: str, fallback: str) -> str:
    """Drop clauses that merely *mention* a gate-loosening knob (e.g. a refusal's override hint).

    `trail.audit_trail_evidence` flags `GATE_LOOSENING_TOKENS` anywhere in `why`; real overrides are
    recorded in `evidence`, so a hint in refusal prose would be a permanent false positive.
    """
    tokens = [norm_token_text(t) for t in GATE_LOOSENING_TOKENS]
    clauses = re.split(r"\s*;\s*|,\s+or\s+", why)
    kept = [c for c in clauses if c and not any(t in norm_token_text(c) for t in tokens)]
    return "; ".join(kept) or fallback
