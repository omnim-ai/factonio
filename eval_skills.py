"""Deterministic, stdlib-only blind A/B evaluation for Factonio skills and models.

Blinds two candidate transcripts into ``Candidate X`` / ``Candidate Y`` packets (stripping
model/provider/vendor names, candidate labels, card ids and local home paths), audits
transcripts for Information Barrier leaks, measures skill prompt bloat, and scores
phase rubric compliance. Nothing here calls a model: a caller-supplied ``judge_fn``
is optional and the default scorer is fully offline and reproducible.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import posixpath
import re
import shlex
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Optional

from .config import _ROUTER_PROVIDERS, _VENDOR_ALIASES, infer_model_vendor

PHASES: tuple[str, ...] = ("define", "design", "implement", "verify-diff", "verify-blackbox", "orchestrator")
EVAL_ACTIONS: tuple[str, ...] = ("ab", "audit", "bloat")
SLOTS: tuple[str, str] = ("X", "Y")
DEFAULT_SOFT_TOKENS = 3000
DEFAULT_HARD_TOKENS = 4500
CHARS_PER_TOKEN = 4

_PHASE_ALIASES = {
    "verify_diff": "verify-diff",
    "verify:diff": "verify-diff",
    "diff": "verify-diff",
    "verify_blackbox": "verify-blackbox",
    "verify:blackbox": "verify-blackbox",
    "blackbox": "verify-blackbox",
    "black-box": "verify-blackbox",
    "implementation": "implement",
    "definition": "define",
}
_HOLDOUT_PHASES = frozenset({"implement", "design", "verify-diff"})

# --- Sanitization -----------------------------------------------------------------------

# Aliases that are also common English / Factonio words are only redacted in a model-id
# shape (`phi-4`, `command-r`, `hermes-3`) or as a `vendor/` path prefix.
_AMBIGUOUS_ALIASES = frozenset({"command", "hermes", "meta", "nova", "phi", "palm", "azure", "nous"})
_AMBIGUOUS_ROUTERS = frozenset({"together", "custom", "vertex", "nvidia", "vercel"})
_TAIL = r"(?:[-_.:@/][A-Za-z0-9](?:[\w.:@/+-]*[A-Za-z0-9])?)?"
# JSON-serialized transcripts carry newlines/tabs as the two characters `\n` / `\t`, so a
# token right after one of those escapes still starts a word.
_ESC_LEFT = r"(?<=\\[nrt])"
_LEFT = r"(?:(?<![A-Za-z0-9_])|" + _ESC_LEFT + ")"
_WORD_LEFT = r"(?:(?<![\w])|" + _ESC_LEFT + ")"
_RIGHT = r"(?![A-Za-z0-9_])"


def _alt(words: Any) -> str:
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


_PLAIN_ALIASES = [a for a in _VENDOR_ALIASES if a not in _AMBIGUOUS_ALIASES]
_PLAIN_ROUTERS = [r for r in _ROUTER_PROVIDERS if r not in _AMBIGUOUS_ROUTERS]
_MODEL_RE = re.compile(
    _LEFT
    + "(?:"
    + rf"(?:{_alt(_PLAIN_ROUTERS)}|{_alt(_PLAIN_ALIASES)}){_TAIL}"
    + rf"|(?:{_alt(_AMBIGUOUS_ROUTERS)}|{_alt(_AMBIGUOUS_ALIASES)})/[A-Za-z0-9][\w.:@/+-]*[A-Za-z0-9]"
    + rf"|(?:{_alt(_AMBIGUOUS_ALIASES)})[-_.:]?\d{_TAIL}"
    + rf"|command-r{_TAIL}"
    + ")"
    + _RIGHT,
    re.IGNORECASE,
)
_HOME_RE = re.compile(r"(?:/home|/Users)/[^/\s'\"`:;,)\]}]+|[A-Za-z]:\\+Users\\+[^\\\s'\"`:;,)\]}]+")
_CARD_RE = re.compile(_LEFT + r"t_[0-9a-f]{6,32}" + _RIGHT)

REDACTION_PLACEHOLDERS = {
    "home_path": "~",
    "card_id": "[REDACTED_CARD]",
    "candidate_label": "[REDACTED_LABEL]",
    "model_vendor": "[REDACTED_MODEL]",
}


_LABEL_CONTEXT = (
    "candidate", "option", "variant", "version", "arm", "label", "run", "model", "skill",
    "response", "answer", "submission", "transcript", "agent", "worker", "approach", "solution", "branch",
    "output",
)


def _is_weak_label(label: str) -> bool:
    """Single characters and short numbers are only redacted in label context ("Candidate A")."""
    return len(label) == 1 or (label.isdigit() and len(label) <= 4)


def _short_label_res(labels: list[str]) -> list[re.Pattern[str]]:
    # Bare "A" / "a" is an English article, so a weak label is redacted only after a context
    # word ("Candidate A", "variant: B", "candidate_a"), when quoted/bracketed ("A", (B), [A]),
    # or as a line-leading speaker header ("A: ...", "**B:**").
    alt = _alt(labels)
    context = _WORD_LEFT + r"(?i:" + "|".join(_LABEL_CONTEXT) + r")s?"
    return [
        re.compile(
            r"(?P<pre>" + context + r"[ \t_:#=-]{0,3})(?:" + alt + r")(?![\w])"
            r"|(?P<q>(?<=[\"'`(\[]))(?:" + alt + r")(?=[\"'`)\]])"
        ),
        re.compile(r"(?P<pre>" + context + r"[_-])(?i:" + alt + r")(?![\w])"),
        re.compile(
            r"(?P<pre>(?:^|(?<=[\"'])|(?<=\\n))[ \t]*(?:[#>*-]+[ \t]*)?(?:\*\*)?)(?:" + alt
            + r")(?=(?:\*\*)?[ \t]*:)",
            re.MULTILINE,
        ),
    ]


def _transcript_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)


def sanitize_transcript(payload: Any, *, extra_labels: Optional[Any] = None) -> dict[str, Any]:
    """Redact model/provider/vendor names, candidate labels, card ids and home paths.

    ``payload`` may be text or any JSON-serializable transcript structure. Returns
    ``{sanitized_text, redaction_count, redacted_categories, category_counts}``.
    """
    text = _transcript_text(payload)
    counts: dict[str, int] = {}

    def apply(category: str, pattern: re.Pattern[str], current: str) -> str:
        placeholder = REDACTION_PLACEHOLDERS[category]
        if "pre" in pattern.groupindex:
            out, n = pattern.subn(lambda m: (m.group("pre") or "") + placeholder, current)
        else:
            out, n = pattern.subn(placeholder, current)
        if n:
            counts[category] = counts.get(category, 0) + n
        return out

    text = apply("home_path", _HOME_RE, text)
    text = apply("card_id", _CARD_RE, text)
    labels = sorted({str(x).strip() for x in (extra_labels or []) if x is not None and str(x).strip()})
    long_labels = [x for x in labels if not _is_weak_label(x)]
    short_labels = [x for x in labels if _is_weak_label(x)]
    if long_labels:
        label_re = re.compile(_WORD_LEFT + r"(?:" + _alt(long_labels) + r")(?![\w])", re.IGNORECASE)
        text = apply("candidate_label", label_re, text)
    if short_labels:
        for pattern in _short_label_res(short_labels):
            text = apply("candidate_label", pattern, text)
    text = apply("model_vendor", _MODEL_RE, text)
    return {
        "sanitized_text": text,
        "redaction_count": sum(counts.values()),
        "redacted_categories": sorted(counts),
        "category_counts": dict(sorted(counts.items())),
    }


# --- Blinding ---------------------------------------------------------------------------

_PAYLOAD_KEYS = ("transcript", "messages", "events", "text", "content", "output", "skill")
# Keys that name who produced a candidate. Their string values are redacted everywhere and the
# keys never reach the judge packet. `name` / `id` are identity only on a candidate/transcript
# envelope, never on a tool call or message (where they name the tool or the call).
_LABEL_KEYS = ("label", "candidate_label", "candidate", "variant", "variant_name", "skill_variant", "run_name",
               "name", "id")
_STRONG_IDENTITY_KEYS = frozenset(
    {"label", "candidate_label", "candidate", "variant", "variant_name", "skill_variant", "run_name",
     "model", "model_name", "model_id", "provider", "provider_name", "vendor", "judge", "judge_model",
     "task_id", "card_id", "worker_session_id", "session_id", "worker_id", "author", "profile", "assignee"}
)
_WEAK_IDENTITY_KEYS = frozenset({"name", "id"})
_EVENT_SHAPE_KEYS = frozenset({"tool", "tool_name", "type", "function", "args", "arguments", "input", "role",
                               "command", "parameters", "params", "tool_calls"})


def _looks_like_event(obj: Mapping[str, Any]) -> bool:
    return any(k in obj for k in _EVENT_SHAPE_KEYS)


def _identity_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, Mapping):
        return [s for k in ("provider", "model", "name", "label") for s in _identity_strings(value.get(k))]
    return []


def _strip_identity(value: Any, terms: list[str], *, top: bool) -> Any:
    """Drop identity keys (recursively for strong keys, top-level only for `name`/`id`)."""
    if isinstance(value, Mapping):
        weak = top and not _looks_like_event(value)
        out: dict[str, Any] = {}
        for k, v in value.items():
            low = str(k).lower()
            if low in _STRONG_IDENTITY_KEYS or (weak and low in _WEAK_IDENTITY_KEYS):
                terms.extend(_identity_strings(v))
                continue
            out[k] = _strip_identity(v, terms, top=False)
        return out
    if isinstance(value, list):
        return [_strip_identity(v, terms, top=False) for v in value]
    return value


def _normalize_candidate(candidate: Any, default_label: str) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        return {"label": default_label, "explicit_label": False, "payload": candidate, "model": None,
                "provider": None, "identity_terms": []}
    label = ""
    for key in _LABEL_KEYS:
        if key in _WEAK_IDENTITY_KEYS and _looks_like_event(candidate):
            continue
        value = candidate.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            label = str(value).strip()
            break
    model = candidate.get("model") or candidate.get("model_name") or candidate.get("model_id")
    provider = candidate.get("provider") or candidate.get("provider_name") or candidate.get("vendor")
    if isinstance(model, Mapping):
        provider = provider or model.get("provider")
        model = model.get("model")
    terms: list[str] = []
    envelope = _strip_identity(candidate, terms, top=True)
    primary_key = next((k for k in _PAYLOAD_KEYS if k in envelope), None)
    if primary_key is None or _looks_like_event(envelope):
        payload: Any = envelope
    else:
        primary = envelope[primary_key]
        if isinstance(primary, Mapping):
            primary = _strip_identity(primary, terms, top=True)
        if primary_key == "messages":
            primary = {"messages": primary}
        # Substantive structured completion fields (verdict, findings, scenarios_run, ...) that sit
        # beside the transcript are kept for the judge and the rubric; they are never actions.
        completion = {k: v for k, v in envelope.items() if k != primary_key}
        payload = {"transcript": primary, "completion": completion} if completion else primary
    return {
        "label": label or default_label,
        "explicit_label": bool(label),
        "payload": payload,
        "model": str(model).strip() if model else None,
        "provider": str(provider).strip() if provider else None,
        "identity_terms": sorted({t for t in terms if t}),
    }


_DEFAULT_LABELS = ("A", "B")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redaction_terms(cands: list[dict[str, Any]]) -> list[str]:
    # The default slot names are always identities, even when no label metadata was supplied.
    terms: list[str] = list(_DEFAULT_LABELS)
    for c in cands:
        if c["explicit_label"]:
            terms.append(c["label"])
            stem = PurePosixPath(c["label"]).stem
            if stem and stem != c["label"]:
                terms.append(stem)
        for key in ("model", "provider"):
            if c.get(key):
                terms.append(c[key])
        terms.extend(c.get("identity_terms") or [])
    return terms


def _blind(cands: list[dict[str, Any]], seed: Any) -> dict[str, Any]:
    terms = _redaction_terms(cands)
    sanitized = [sanitize_transcript(c["payload"], extra_labels=terms) for c in cands]
    raw = [_sha256(s["sanitized_text"]) for s in sanitized]
    digests = sorted(raw)
    seed_str = str(seed) if seed is not None and str(seed).strip() else _sha256("\n".join(digests))[:16]
    # Canonical content order first, so swapping the arguments never changes the assignment.
    canonical = (0, 1) if raw[0] <= raw[1] else (1, 0)
    flip = int(_sha256(f"factonio-blind-ab:{seed_str}"), 16) & 1
    order = (canonical[1], canonical[0]) if flip else canonical
    key = {slot: cands[i]["label"] for slot, i in zip(SLOTS, order)}
    pair_id = "bp_" + _sha256(f"{seed_str}:{':'.join(digests)}")[:12]
    candidates = {
        slot: {"name": f"Candidate {slot}", **sanitized[i]} for slot, i in zip(SLOTS, order)
    }
    key_digest = _sha256(json.dumps({"pair_id": pair_id, "key": key}, sort_keys=True))
    return {
        "pair_id": pair_id,
        "seed": seed_str,
        "candidates": candidates,
        "key": key,
        "sealed_key": {"pair_id": pair_id, "key_digest": key_digest, "key": dict(key)},
        "_order": order,
    }


def build_blind_pair(candidate_a: Any, candidate_b: Any, *, seed: Any = None) -> dict[str, Any]:
    """Sanitize two candidates and assign them to ``Candidate X`` / ``Candidate Y``.

    Candidates are text, a transcript structure, or ``{label, model?, provider?, transcript}``.
    The assignment is a pure function of ``seed`` (default: a SHA-256 of both sanitized
    candidates, independent of argument order). ``candidates`` never carries labels; the
    unblinding ``key`` (``{"X": label, "Y": label}``) is returned separately and also as
    ``sealed_key`` with a ``key_digest`` a judge packet can cite without revealing it.
    """
    cands = [_normalize_candidate(c, d) for c, d in zip((candidate_a, candidate_b), _DEFAULT_LABELS)]
    if cands[0]["label"] == cands[1]["label"]:
        raise ValueError(f"candidate labels must differ (both are {cands[0]['label']!r})")
    pair = _blind(cands, seed)
    pair.pop("_order")
    return pair


def blind_packet(pair: Mapping[str, Any]) -> dict[str, Any]:
    """Return the judge-facing view of a blind pair: no unblinding key, only its digest."""
    return {
        "pair_id": pair["pair_id"],
        "key_digest": pair["sealed_key"]["key_digest"],
        "candidates": {slot: pair["candidates"][slot]["sanitized_text"] for slot in SLOTS},
    }


# --- Information Barrier audit ----------------------------------------------------------

_SOURCE_EXTS = frozenset(
    {
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
        ".kts", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".m",
        ".mm", ".vue", ".svelte", ".sh", ".bash", ".zsh", ".ps1", ".lua", ".dart", ".ex", ".exs",
        ".erl", ".hs", ".ml", ".clj",
    }
)
_BLACKBOX_ALLOWED_NAMES = frozenset({"spec.md", "readme.md", "pyproject.toml", "package.json", "makefile"})
_BLACKBOX_ALLOWED_DIRS = frozenset({".factonio", "holdout", "design"})
_HOLDOUT_TOKEN_RE = re.compile(r"(?:^|[/=*'\"])holdout(?:[/*'\"]|$)")
_MENTION_RE = re.compile(r"holdout|git\s+diff|git\s+show|gh\s+pr\s+diff", re.IGNORECASE)
_READ_VERBS = frozenset(
    {"cat", "less", "more", "head", "tail", "bat", "nl", "sed", "awk", "vi", "vim", "nvim", "nano",
     "view", "strings", "xxd", "od", "grep", "egrep", "rg", "ag", "ack", "code", "open", "cp", "tac"}
)
_GREP_VERBS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})
_GREP_OPTS_WITH_VALUE = frozenset({"-g", "--glob", "-t", "--type", "-A", "-B", "-C", "-m", "--max-count", "-f"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ash", "ksh"})
_WRAPPERS = frozenset({"sudo", "command", "time", "nohup", "exec", "builtin", "xargs"})
_GIT_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
_COMMAND_TOOL_HINTS = ("terminal", "shell", "bash", "exec", "run_command", "command")
_CODE_TOOL_HINTS = ("execute_code", "python", "code")
_READ_TOOL_HINTS = ("read", "view", "open", "cat", "get_file", "fetch_file", "search", "grep", "glob")
_PATH_KEYS = ("path", "file_path", "filepath", "file", "filename", "paths", "files", "target_path",
              "dir", "directory", "image_url", "relative_path", "absolute_path", "notebook_path")
_GLOB_KEYS = ("glob", "file_glob", "include", "includes")
_COMMAND_KEYS = ("command", "cmd", "script", "shell_command")
_TRACE_CMD_RE = re.compile(r"^\s*(?:\$|❯|>\s*\$)\s+(?P<cmd>\S.*)$")
_TRACE_TOOL_RE = re.compile(
    r"^\s*(?:\[(?:tool|tool_call|call)\]|(?:tool|tool_call|call)\s*:)\s*(?P<name>[\w.:-]+)\s*"
    r"(?:\((?P<args>.*)\)\s*$|(?P<rest>.*)$)",
    re.IGNORECASE,
)
_KV_RE = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)")
_NON_ACTION_ROLES = frozenset({"system", "user", "developer", "tool", "function", "prompt", "skill"})


def normalize_phase(phase: Any) -> str:
    norm = str(phase or "").strip().lower()
    norm = _PHASE_ALIASES.get(norm, norm)
    if norm not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; expected one of {', '.join(PHASES)}")
    return norm


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except ValueError:
            parsed = None
        if isinstance(parsed, Mapping):
            return dict(parsed)
        pairs = {k: v.strip("\"'") for k, v in _KV_RE.findall(s)}
        return pairs or {"_raw": s.strip("\"'")}
    return {}


def _tool_event(name: Any, args: Any, index: int) -> dict[str, Any]:
    return {"kind": "tool", "name": str(name or "").strip(), "args": _parse_args(args), "index": index}


def _text_events(text: str, role: Optional[str], index: int) -> Iterator[dict[str, Any]]:
    stripped = text.strip()
    if stripped[:1] in ("[", "{"):
        try:
            yield from _iter_events(json.loads(stripped), index, role)
            return
        except ValueError:
            pass
    lines = text.splitlines()
    jsonl = [ln for ln in lines if ln.strip()]
    if len(jsonl) > 1 and all(ln.lstrip()[:1] == "{" for ln in jsonl):
        try:
            parsed = [json.loads(ln) for ln in jsonl]
        except ValueError:
            parsed = None
        if parsed is not None:
            yield from _iter_events(parsed, index, role)
            return
    actionable = role not in _NON_ACTION_ROLES
    for line in lines:
        if actionable:
            m = _TRACE_CMD_RE.match(line)
            if m:
                yield {"kind": "command", "command": m.group("cmd"), "index": index}
                continue
            m = _TRACE_TOOL_RE.match(line)
            if m:
                yield _tool_event(m.group("name"), m.group("args") if m.group("args") is not None else m.group("rest"), index)
                continue
        if _MENTION_RE.search(line):
            yield {"kind": "mention", "text": line.strip(), "index": index}


def _iter_events(payload: Any, index: int = 0, role: Optional[str] = None) -> Iterator[dict[str, Any]]:
    if payload is None:
        return
    if isinstance(payload, str):
        yield from _text_events(payload, role, index)
        return
    if isinstance(payload, (list, tuple)):
        for i, item in enumerate(payload):
            yield from _iter_events(item, i, role)
        return
    if not isinstance(payload, Mapping):
        return
    # Resolve the effective role before descending into any container: a non-action enclosing
    # role (a user/system/tool envelope quoting a transcript) is never upgraded by a role nested
    # inside it, so quoted calls stay quotes at every depth.
    if role in _NON_ACTION_ROLES:
        item_role: Optional[str] = role
    else:
        item_role = str(payload.get("role") or role or "").strip().lower() or None
    containers = [k for k in ("messages", "events", "transcript", "steps", "entries", "turns") if k in payload]
    if containers:
        for k in containers:
            yield from _iter_events(payload[k], index, item_role)
        return
    actionable = item_role not in _NON_ACTION_ROLES
    item_type = str(payload.get("type") or "").strip().lower()
    if item_type in ("text", "message") and "text" in payload:
        yield from _text_events(str(payload["text"]), item_role, index)
        return
    if item_type in ("tool_result", "function_call_output", "tool_output"):
        return
    fn = payload.get("function")
    if actionable and isinstance(fn, Mapping):
        yield _tool_event(fn.get("name"), fn.get("arguments"), index)
    elif actionable:
        name = payload.get("tool") or payload.get("tool_name") or payload.get("name")
        args = next((payload[k] for k in ("arguments", "args", "input", "parameters", "params") if k in payload), None)
        if name and (args is not None or item_type in ("tool_use", "tool_call", "function_call")):
            yield _tool_event(name, args, index)
        elif any(isinstance(payload.get(k), str) for k in _COMMAND_KEYS):
            cmd = next(payload[k] for k in _COMMAND_KEYS if isinstance(payload.get(k), str))
            yield {"kind": "command", "command": cmd, "index": index}
    for call in (payload.get("tool_calls") or []) if actionable else []:
        yield from _iter_events(call, index, item_role)
    content = payload.get("content")
    if isinstance(content, str):
        yield from _text_events(content, item_role, index)
    elif isinstance(content, (list, tuple)):
        for block in content:
            yield from _iter_events(block, index, item_role)


_INPUT_REDIRECTS = frozenset({"<", "<>"})
_OUTPUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", ">&"})
_NON_FILE_REDIRECTS = frozenset({"<<", "<<-", "<<<", "<&"})


def _is_punct(tok: str) -> bool:
    return bool(tok) and set(tok) <= set(";&|()<>")


_HEREDOC_RE = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w.-]*))")
_HEREDOC_MARK = "FACTONIOHEREDOC"
_HEREDOC_MARK_RE = re.compile(_HEREDOC_MARK + r"(\d+)$")
_PIPES = frozenset({"|", "|&"})


def _quote_state(state: Optional[str], text: str, end: int) -> Optional[str]:
    """Shell quote state (``None``, ``'`` or ``"``) after scanning ``text[:end]`` from ``state``."""
    q, i = state, 0
    while i < end:
        c = text[i]
        if q == "'":
            if c == "'":
                q = None
        elif c == "\\":
            i += 2
            continue
        elif q == '"':
            if c == '"':
                q = None
        elif c in "'\"":
            q = c
        i += 1
    return q


def _extract_heredocs(command: str, expanding: Optional[list[bool]] = None) -> tuple[str, list[str]]:
    """Lift heredoc bodies out of a shell command.

    Each unquoted ``<<[-]DELIM`` becomes ``<< FACTONIOHEREDOC<n>`` and its body (up to the
    terminating delimiter line, or end of text as bash does) is returned separately, so body
    text is never split into argv words. Unquoted newlines become ``;`` command separators.
    ``expanding`` (when given) receives, per body, whether its delimiter was unquoted so the
    shell expands ``$(...)`` inside it.
    """
    lines = command.split("\n")
    out: list[str] = []
    bodies: list[str] = []
    state: Optional[str] = None
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        pending: list[tuple[str, bool]] = []
        pieces: list[str] = []
        last = 0
        if "<<" in line:
            for m in _HEREDOC_RE.finditer(line):
                if _quote_state(state, line, m.start()) is not None:
                    continue
                pieces.append(line[last:m.start()])
                pieces.append(f" << {_HEREDOC_MARK}{len(bodies) + len(pending)} ")
                pending.append((m.group(2) or m.group(3) or m.group(4), bool(m.group(1))))
                if expanding is not None:
                    expanding.append(bool(m.group(4)) and "\\" not in m.group(0))
                last = m.end()
        pieces.append(line[last:])
        rebuilt = "".join(pieces)
        state = _quote_state(state, line, len(line))
        for delim, dash in pending:
            body: list[str] = []
            while i < len(lines):
                cur = lines[i]
                i += 1
                if (cur.lstrip("\t") if dash else cur) == delim:
                    break
                body.append(cur)
            bodies.append("\n".join(body))
        if state is None and rebuilt.endswith("\\") and not rebuilt.endswith("\\\\"):
            out.append(rebuilt[:-1] + " ")
        elif state is None:
            out.append(rebuilt + "\n;\n")
        else:
            out.append(rebuilt + "\n")
    return "".join(out), bodies


def _subst_end(text: str, i: int) -> int:
    """Index of the ``)`` closing a ``$(`` whose body starts at ``text[i]`` (``len(text)`` if unclosed)."""
    depth, n = 1, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                elif text.startswith("$(", i):
                    i = _subst_end(text, i + 2)
                elif text[i] == "`":
                    j = text.find("`", i + 1)
                    i = n if j < 0 else j
                i += 1
            i += 1
            continue
        if c == "`":
            j = text.find("`", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _command_substitutions(command: str) -> list[str]:
    """Inner commands of every executed ``$(...)`` / backtick substitution in shell text.

    Quote-aware: single-quoted text (and quoted heredoc bodies) is literal, ``\\$(`` and
    ``\\``` are escaped, and ``#`` comments are skipped. Nested substitutions are left inside
    the returned inner command for the caller's recursive scan.
    """
    if "$(" not in command and "`" not in command:
        return []
    out: list[str] = []
    text = command
    if "<<" in command:
        expanding: list[bool] = []
        text, bodies = _extract_heredocs(command, expanding)
        for body, expands in zip(bodies, expanding):
            if expands:
                out.extend(_command_substitutions('"' + body.replace('"', '\\"') + '"'))
    n, i, dq = len(text), 0, False
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if not dq and c == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if not dq and c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == '"':
            dq = not dq
        elif text.startswith("$(", i):
            end = _subst_end(text, i + 2)
            out.append(text[i + 2:end])
            i = end + 1
            continue
        elif c == "`":
            j, buf = i + 1, []
            while j < n and text[j] != "`":
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1] if text[j + 1] in "$`\\" else text[j:j + 2])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
            continue
        i += 1
    return [sub for sub in out if sub.strip()]


def _new_segment(piped: bool = False) -> dict[str, Any]:
    return {"argv": [], "inputs": [], "outputs": [], "stdin": [], "piped": piped}


def _parse_command(command: str) -> list[dict[str, Any]]:
    """Split a shell command into simple commands, keeping redirection operands apart.

    Each segment is ``{"argv", "inputs", "outputs", "stdin", "piped"}``: ``< path`` /
    ``0< path`` / ``<> path`` operands are file reads, ``>`` / ``>>`` / ``&>`` targets are
    writes, heredoc bodies and here-strings are ``stdin`` text (never files), and ``piped``
    marks a segment whose stdin is the previous segment's stdout.
    """
    text, bodies = _extract_heredocs(command) if ("<<" in command or "\n" in command) else (command, [])
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = text.split()
    segments: list[dict[str, Any]] = [_new_segment()]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not _is_punct(tok):
            segments[-1]["argv"].append(tok)
            i += 1
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) and not _is_punct(tokens[i + 1]) else None
        redirect = tok in _INPUT_REDIRECTS or tok in _OUTPUT_REDIRECTS or tok in _NON_FILE_REDIRECTS
        if redirect:
            argv = segments[-1]["argv"]
            # shlex splits `0<file` / `2>&1` into a separate fd token; it belongs to the operator.
            if argv and argv[-1].isdigit():
                argv.pop()
            if nxt is not None:
                mark = _HEREDOC_MARK_RE.match(nxt) if tok in ("<<", "<<-") else None
                if tok in _INPUT_REDIRECTS:
                    segments[-1]["inputs"].append(nxt)
                elif tok in _OUTPUT_REDIRECTS and not (tok == ">&" and (nxt.isdigit() or nxt == "-")):
                    segments[-1]["outputs"].append(nxt)
                elif mark and int(mark.group(1)) < len(bodies):
                    segments[-1]["stdin"].append(bodies[int(mark.group(1))])
                elif tok == "<<<":
                    segments[-1]["stdin"].append(nxt)
                i += 2
            else:
                i += 1
            continue
        cur = segments[-1]
        if cur["argv"] or cur["inputs"] or cur["outputs"] or cur["stdin"]:
            segments.append(_new_segment(tok in _PIPES))
        elif tok in _PIPES:
            cur["piped"] = True
        i += 1
    return [s for s in segments if s["argv"] or s["inputs"] or s["outputs"] or s["stdin"]]


def _split_command(command: str) -> list[list[str]]:
    return [s["argv"] for s in _parse_command(command) if s["argv"]]


def _strip_prefix(seg: list[str]) -> list[str]:
    i = 0
    while i < len(seg):
        tok = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
        elif tok == "env":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 2 if seg[i] in ("-u", "--unset", "-C", "--chdir") else 1
        elif tok in _WRAPPERS:
            i += 1
        else:
            break
    return seg[i:]


def _is_excluded(seg: list[str], i: int) -> bool:
    tok = seg[i]
    prev = seg[i - 1] if i > 0 else ""
    prev2 = seg[i - 2] if i > 1 else ""
    nxt = seg[i + 1] if i + 1 < len(seg) else ""
    if tok.startswith("!") or tok.startswith(("--exclude", "--ignore", "--glob=!", "-g!", "--iglob=!")):
        return True
    # Only path-exclusion options exclude their operand. Display flags such as `cat -v` / `cat -n`
    # do not, and grep's `-v` inverts the *pattern* (handled by `_grep_parse`), never a file.
    if prev in ("--exclude", "--exclude-dir", "--ignore", "--ignore-dir"):
        return True
    if prev in ("-path", "-name", "-wholename", "-ipath", "-iname", "-regex") and prev2 in ("-not", "!"):
        return True
    return nxt == "-prune"


# Options that consume a value, per grep family. Unknown options are treated as flags.
_GREP_VALUE_OPTS = frozenset(
    {"-e", "-f", "-A", "-B", "-C", "-m", "-d", "-D", "--regexp", "--file", "--after-context",
     "--before-context", "--context", "--max-count", "--directories", "--devices", "--include", "--exclude",
     "--exclude-dir", "--exclude-from", "--label", "--binary-files"}
)
_RG_VALUE_OPTS = frozenset(
    {"-e", "-f", "-g", "-t", "-T", "-A", "-B", "-C", "-m", "-d", "-E", "-j", "-M", "-r", "--regexp", "--file",
     "--glob", "--iglob", "--type", "--type-not", "--type-add", "--type-clear", "--after-context",
     "--before-context", "--context", "--max-count", "--max-depth", "--encoding", "--threads", "--max-columns",
     "--replace", "--ignore-file", "--pre", "--pre-glob", "--sort", "--sortr", "--path-separator",
     "--max-filesize", "--colors", "--context-separator", "--field-match-separator", "--engine"}
)
_ACK_VALUE_OPTS = frozenset({"-e", "-f", "-g", "-G", "-A", "-B", "-C", "-m", "--file-search-regex", "--ignore-dir",
                             "--max-count", "--type", "--context", "--after-context", "--before-context"})
_PATTERN_OPTS = frozenset({"-e", "--regexp"})
_PATTERN_FILE_OPTS = frozenset({"-f", "--file"})
_SEARCH_GLOB_OPTS = frozenset({"-g", "--glob", "--iglob", "--include", "-G", "--file-search-regex"})


def _grep_parse(seg: list[str]) -> Optional[dict[str, Any]]:
    """Split a grep-family command into pattern tokens and the paths it actually reads.

    Returns ``{"skip": {token indices that are search patterns}, "files": [...], "globs": [...]}``
    or ``None`` when ``seg`` is not a grep-family command. ``-f FILE`` is a file read.
    """
    verb = PurePosixPath(seg[0]).name
    start = 1
    family = verb
    if verb == "git":
        sub, rest = _git_subcommand(seg)
        if sub != "grep":
            return None
        start, family = len(seg) - len(rest), "grep"
    elif verb not in _GREP_VERBS:
        return None
    value_opts = _RG_VALUE_OPTS if family == "rg" else _ACK_VALUE_OPTS if family in ("ag", "ack") else _GREP_VALUE_OPTS
    # rg / ag / ack and `git grep` search a tree by default; grep only with -r / -R.
    recursive = family in ("rg", "ag", "ack") or verb == "git"
    list_only = False
    skip: set[int] = set()
    files: list[str] = []
    globs: list[str] = []
    positional: list[int] = []
    explicit_pattern = False

    def take(opt: str, value: Optional[str], idx: int) -> None:
        nonlocal explicit_pattern
        if opt in _PATTERN_OPTS:
            explicit_pattern = True
            skip.add(idx)
        elif opt in _PATTERN_FILE_OPTS:
            explicit_pattern = True
            if value:
                files.append(value)
        elif opt in _SEARCH_GLOB_OPTS and value:
            globs.append(value)

    i, end_opts = start, False
    while i < len(seg):
        tok = seg[i]
        if not end_opts and tok == "--":
            end_opts = True
        elif not end_opts and tok.startswith("-") and tok != "-":
            if tok.startswith("--"):
                name, eq, val = tok.partition("=")
                if name in ("--recursive", "--dereference-recursive"):
                    recursive = True
                elif name == "--files" and family == "rg":
                    list_only = True
                if name in value_opts:
                    if eq:
                        take(name, val, i)
                    elif i + 1 < len(seg):
                        i += 1
                        take(name, seg[i], i)
            else:
                for j, ch in enumerate(tok[1:], 1):
                    opt = "-" + ch
                    if family in ("grep", "egrep", "fgrep") and ch in "rR":
                        recursive = True
                    if opt in value_opts:
                        attached = tok[j + 1:]
                        if attached:
                            take(opt, attached, i)
                        elif i + 1 < len(seg):
                            i += 1
                            take(opt, seg[i], i)
                        break
        else:
            positional.append(i)
        i += 1
    if not explicit_pattern and positional and not list_only:
        skip.add(positional.pop(0))
    targets = [seg[k] for k in positional]
    files.extend(targets)
    return {"skip": skip, "files": files, "globs": globs, "targets": targets,
            "recursive": recursive and not list_only}


_PY_INTERP_RE = re.compile(r"^(?:python|pypy)(?:\d+(?:\.\d+)*)?$")
_OTHER_INTERPRETERS = frozenset({"node", "nodejs", "bun", "deno", "ruby", "perl", "php", "osascript", "Rscript"})
_INTERP_CODE_FLAGS = frozenset({"-c", "-e", "-E", "-p", "-r", "--eval", "--print", "--command"})


_JS_INTERPRETERS = frozenset({"node", "nodejs", "bun", "deno"})


def _interp_lang(verb: str) -> Optional[str]:
    """Program language read by an interpreter verb: ``python`` | ``shell`` | ``js`` | ``other``."""
    if _PY_INTERP_RE.match(verb):
        return "python"
    if verb in _SHELLS:
        return "shell"
    if verb in _JS_INTERPRETERS:
        return "js"
    if verb in _OTHER_INTERPRETERS:
        return "php" if verb == "php" else "other"
    return None


def _interpreter_code_at(seg: list[str]) -> Optional[tuple[int, str]]:
    """``(argv index, inline program)`` of `python3 -c CODE`, `node -e CODE`, `deno eval CODE`, ..."""
    verb = PurePosixPath(seg[0]).name
    if not (_PY_INTERP_RE.match(verb) or verb in _OTHER_INTERPRETERS):
        return None
    if verb == "deno" and len(seg) > 2 and seg[1] == "eval":
        return 2, seg[2]
    for i in range(1, len(seg)):
        tok = seg[i]
        if tok in _INTERP_CODE_FLAGS and i + 1 < len(seg):
            return i + 1, seg[i + 1]
        if tok.startswith("--eval=") or tok.startswith("--print="):
            return i, tok.split("=", 1)[1]
        if _PY_INTERP_RE.match(verb) and tok.startswith("-c") and len(tok) > 2:
            return i, tok[2:]
        if not tok.startswith("-"):
            return None
    return None


_SHELL_OPTS_WITH_VALUE = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})


def _shell_command_string_at(seg: list[str]) -> Optional[tuple[int, str]]:
    """``(argv index, program)`` of `bash -c CMD`, `bash -lc CMD`, `sh -e -c CMD`, `zsh -o x -c CMD`.

    Any short option cluster before ``--`` that contains ``c`` (`-c`, `-lc`, `-ec`, `-xc`)
    makes the first non-option operand the command string; `-o` / `+o` consume a value.
    """
    if not seg or PurePosixPath(seg[0]).name not in _SHELLS:
        return None
    has_c = False
    i = 1
    while i < len(seg):
        tok = seg[i]
        if tok == "--":
            i += 1
            break
        if tok in _SHELL_OPTS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("--") or (tok.startswith("+") and len(tok) > 1):
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            has_c = has_c or "c" in tok[1:]
            i += 1
            continue
        break
    if has_c and i < len(seg):
        return i, seg[i]
    return None


def _interpreter_code(seg: list[str]) -> Optional[str]:
    """Inline program text of `python3 -c CODE`, `node -e CODE`, `deno eval CODE`, ... or ``None``."""
    found = _interpreter_code_at(seg)
    return found[1] if found else None


_PY_VALUE_OPTS = frozenset({"-W", "-X", "-Q"})
_INLINE_PROGRAM_OPTS = frozenset({"-c", "-e", "-E", "-p", "-r", "-m", "--eval", "--print", "--command"})


def _reads_program_from_stdin(seg: list[str]) -> bool:
    """True when an interpreter takes its program from stdin (`python3 -`, `bash`, `node -`, `bash -s`)."""
    verb = PurePosixPath(seg[0]).name
    i = 1
    while i < len(seg):
        tok = seg[i]
        if tok == "-":
            return True
        if tok in _INLINE_PROGRAM_OPTS or (_PY_INTERP_RE.match(verb) and tok.startswith(("-c", "-m")) and len(tok) > 2):
            return False
        if tok.startswith("-"):
            i += 2 if _PY_INTERP_RE.match(verb) and tok in _PY_VALUE_OPTS else 1
            continue
        if verb == "deno" and tok == "run":
            i += 1
            continue
        return False
    return True


def _stdin_program(parsed: list[dict[str, Any]], idx: int) -> Optional[tuple[str, str]]:
    """``(lang, program)`` an interpreter segment executes from a heredoc, here-string or pipe.

    Heredoc / here-string text fed to a passive command (`cat`, `tee`, ...) is data, never
    a program; it only becomes code when the receiving command is an interpreter that reads
    its program from stdin (directly, or through `cat <<EOF | python3` / `echo CODE | sh`).
    """
    seg = _strip_prefix(parsed[idx]["argv"])
    if not seg:
        return None
    lang = _interp_lang(PurePosixPath(seg[0]).name)
    if lang is None or not _reads_program_from_stdin(seg):
        return None
    bodies = list(parsed[idx]["stdin"])
    if not bodies and parsed[idx].get("piped") and idx > 0:
        prev = parsed[idx - 1]
        pseg = _strip_prefix(prev["argv"])
        pverb = PurePosixPath(pseg[0]).name if pseg else ""
        if not pseg or pverb in ("cat", "tee"):
            bodies = list(prev["stdin"])
        elif pverb in ("echo", "printf"):
            bodies = [" ".join(t for t in pseg[1:] if not t.startswith("-"))]
    return (lang, "\n".join(bodies)) if bodies else None


_STR = r"(?P<q>[\"'`])(?P<s>(?:(?!(?P=q))[^\n\\]|\\.){1,512}?)(?P=q)"
_CODE_READ_CALL_RE = re.compile(
    r"(?<![\w])(?:hermes_tools\.)?(?:read_file|open|io\.open|codecs\.open|readFileSync|readFile|"
    r"createReadStream|fopen|file_get_contents|File\.read|File\.open|File\.readlines|IO\.read|IO\.readlines|"
    r"linecache\.getlines|fs\.readFileSync|fs\.readFile|fs\.promises\.readFile|fsp\.readFile|"
    r"Deno\.(?:readTextFileSync|readTextFile|readFileSync|readFile|openSync|open)|Bun\.file|np\.loadtxt|pd\.read_csv|tokenize\.open|runpy\.run_path)\s*\(\s*"
    r"(?:(?:path|file|filename|file_path|filepath|name)\s*=\s*)?" + _STR
)
_CODE_PATH_READ_RE = re.compile(
    r"(?<![\w.])(?:pathlib\.)?(?:Path|PurePath|PosixPath)\s*\(\s*" + _STR
    + r"\s*\)\s*\.\s*(?:read_text|read_bytes|open|readlines)\b"
)
_CODE_TOOL_CALL_RE = re.compile(
    r"(?<![\w.])(?:hermes_tools\.)?(?P<fn>search_files|read_file|grep|glob|list_files|view_file)\s*\((?P<args>[^()]*)\)"
)
_CODE_PATH_KW_RE = re.compile(r"(?<![\w])(?:path|file_path|filepath|dir|directory|paths)\s*=\s*" + _STR)
_CODE_FILES_TARGET_RE = re.compile(r"target\s*=\s*[\"']files[\"']")
_CODE_CMD_CALL_RE = re.compile(
    r"(?<![\w])(?:hermes_tools\.)?(?:terminal|os\.system|os\.popen|subprocess\.(?:run|call|check_call|"
    r"check_output|Popen|getoutput|getstatusoutput)|child_process\.(?:execSync|exec|spawnSync)|execSync|"
    r"shell_exec|passthru|system|popen)\s*\(\s*(?:(?:command|cmd|args)\s*=\s*)?" + _STR
)
_CODE_CMD_LIST_RE = re.compile(
    r"(?<![\w])(?:subprocess\.(?:run|call|check_call|check_output|Popen)|spawnSync|execFileSync|spawn)\s*\(\s*"
    r"(?:(?:args\s*=\s*)?\[(?P<list>[^\]]*)\]|(?P<file>[\"'][^\"']+[\"'])\s*,\s*\[(?P<rest>[^\]]*)\])"
)
_CODE_STR_LITERAL_RE = re.compile(r"[\"']([^\"'\n]*)[\"']")


_CODE_FS_CALL_RE = re.compile(
    r"(?<![\w])(?:os\.(?:listdir|scandir|walk|stat|lstat|path\.(?:join|exists|isfile|isdir|getsize))|"
    r"glob\.i?glob|shutil\.\w+|(?:pathlib\.)?(?:Path|PurePath|PosixPath)|fs\.(?:readdirSync|readdir|"
    r"existsSync|statSync|copyFileSync)|readdirSync|Deno\.(?:readDirSync|readDir|statSync|stat)|"
    r"Dir\.(?:glob|entries|children)|scandir|opendir)\s*\((?P<args>[^()]*)\)"
)


def _code_fs_targets(code: str) -> list[str]:
    """String-literal path operands of filesystem calls (listing, globbing, path building, copying)."""
    return [lit for m in _CODE_FS_CALL_RE.finditer(code) for lit in _CODE_STR_LITERAL_RE.findall(m.group("args"))]


def _code_read_targets(code: str) -> list[tuple[str, bool]]:
    """Regex fallback: statically visible file paths read by code, ``[(path, is_search)]``."""
    out: list[tuple[str, bool]] = []
    for rx in (_CODE_READ_CALL_RE, _CODE_PATH_READ_RE):
        out.extend((m.group("s"), False) for m in rx.finditer(code))
    for m in _CODE_TOOL_CALL_RE.finditer(code):
        fn, args = m.group("fn"), m.group("args")
        if fn in ("search_files", "grep"):
            continue
        out.extend((k.group("s"), fn != "read_file") for k in _CODE_PATH_KW_RE.finditer(args))
    return out


_CODE_GLOB_KW_RE = re.compile(r"(?<![\w])(?:file_glob|glob|include)\s*=\s*" + _STR)


def _code_searches(code: str) -> list[tuple[list[str], list[str]]]:
    """Regex fallback: content searches (`search_files` / `grep` tool calls) as ``(roots, globs)``."""
    out: list[tuple[list[str], list[str]]] = []
    for m in _CODE_TOOL_CALL_RE.finditer(code):
        fn, args = m.group("fn"), m.group("args")
        if fn not in ("search_files", "grep") or _CODE_FILES_TARGET_RE.search(args):
            continue
        out.append(([k.group("s") for k in _CODE_PATH_KW_RE.finditer(args)],
                    [k.group("s") for k in _CODE_GLOB_KW_RE.finditer(args)]))
    return out


def _code_commands(code: str) -> list[str]:
    """Regex fallback: shell commands launched by code."""
    cmds = [m.group("s") for m in _CODE_CMD_CALL_RE.finditer(code)]
    for m in _CODE_CMD_LIST_RE.finditer(code):
        parts = []
        if m.group("file"):
            parts.append(m.group("file").strip("\"'"))
        parts.extend(_CODE_STR_LITERAL_RE.findall(m.group("list") or m.group("rest") or ""))
        if parts:
            cmds.append(shlex.join(parts))
    return cmds


_LIT = r"(?:\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\$]|\\.|\$(?!\{))*`)"
_LITS = _LIT + r"(?:\s*[,+.]\s*" + _LIT + r")*"
_PRINT_LITERAL_RE = re.compile(
    r"(?<![\w.$])(?:console\.(?:log|error|warn|info|debug)|print|println|printf|puts|say|echo|alert)"
    r"\s*\(?\s*" + _LITS + r"(?:\s*\))?"
)


def _strip_code_noise(code: str, lang: str) -> str:
    """Regex-fallback preprocessing: drop comments, then print/log calls whose only arguments
    are string literals (prohibition prose such as ``console.log("never read src/a.py")``)."""
    js = lang in ("js", "php")
    line_marks = ("//", "#") if lang == "php" else ("//",) if js else ("#",)
    quotes = "'\"`" if js else "'\""
    out: list[str] = []
    i, n, q = 0, len(code), None
    while i < n:
        c = code[i]
        if q:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(code[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in quotes:
            q = c
        elif js and code.startswith("/*", i):
            end = code.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue
        elif any(code.startswith(mark, i) for mark in line_marks):
            end = code.find("\n", i)
            i = n if end < 0 else end
            continue
        out.append(c)
        i += 1
    return _PRINT_LITERAL_RE.sub(" ", "".join(out))


_PY_READ_CALLS = frozenset(
    {"open", "io.open", "codecs.open", "os.open", "gzip.open", "bz2.open", "lzma.open", "tarfile.open",
     "zipfile.ZipFile", "read_file", "view_file", "linecache.getline", "linecache.getlines", "tokenize.open",
     "runpy.run_path", "fileinput.input", "numpy.loadtxt", "np.loadtxt", "pandas.read_csv", "pd.read_csv"}
)
_PY_COMMAND_CALLS = frozenset(
    {"terminal", "os.system", "os.popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
     "subprocess.check_output", "subprocess.Popen", "subprocess.getoutput", "subprocess.getstatusoutput",
     "os.execv", "os.execve", "os.execvp", "os.execvpe", "os.execl", "os.execle", "os.execlp", "os.execlpe",
     "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe", "os.spawnl", "os.spawnle", "os.spawnlp",
     "os.spawnlpe", "os.posix_spawn", "os.posix_spawnp", "pty.spawn", "asyncio.create_subprocess_shell",
     "asyncio.create_subprocess_exec"}
)
# Where each launcher takes its argv: ``("arg", i)`` is one sequence/string argument,
# ``("var", i)`` spreads ``args[i:]`` as argv (``os.execl(path, arg0, ...)``). Default ``("arg", 0)``.
_PY_ARGV_AT: dict[str, tuple[str, int]] = {
    **{f"os.exec{s}": ("arg", 1) for s in ("v", "ve", "vp", "vpe")},
    **{f"os.exec{s}": ("var", 1) for s in ("l", "le", "lp", "lpe")},
    **{f"os.spawn{s}": ("arg", 2) for s in ("v", "ve", "vp", "vpe")},
    **{f"os.spawn{s}": ("var", 2) for s in ("l", "le", "lp", "lpe")},
    "os.posix_spawn": ("arg", 1), "os.posix_spawnp": ("arg", 1), "asyncio.create_subprocess_exec": ("var", 0),
}
# ``open``-style calls whose ``mode`` (positional index) decides whether the file is read.
_PY_MODE_OPENS = {"open": 1, "io.open": 1, "codecs.open": 1, "gzip.open": 1, "bz2.open": 1, "lzma.open": 1}
_PY_SEQ_MUTATORS = frozenset({"append", "extend", "insert", "pop", "remove", "clear", "reverse"})
_PY_CONTENT_SEARCH_CALLS = frozenset({"search_files", "grep"})
_PY_LISTING_TOOLS = frozenset({"glob", "list_files"})
_PY_EVAL_CALLS = frozenset({"eval", "exec", "compile"})
_PY_PATH_CTORS = frozenset(
    {"Path", "PurePath", "PosixPath", "PurePosixPath", "WindowsPath", "pathlib.Path", "pathlib.PurePath",
     "pathlib.PosixPath", "pathlib.PurePosixPath", "pathlib.WindowsPath"}
)
_PY_PATH_JOINS = frozenset({"os.path.join", "posixpath.join", "os.path.abspath", "os.path.normpath",
                            "os.path.expanduser", "os.path.realpath"})
_PY_PATH_DERIVE = frozenset({"resolve", "absolute", "expanduser", "joinpath"})
_PY_PATH_READ_METHODS = frozenset({"read_text", "read_bytes", "readlines"})
_PY_FS_CALLS = frozenset(
    {"os.listdir", "os.scandir", "os.walk", "os.stat", "os.lstat", "os.chdir", "os.path.exists", "os.path.isfile",
     "os.path.isdir", "os.path.getsize", "glob.glob", "glob.iglob", *_PY_PATH_JOINS, *_PY_PATH_CTORS}
)
_PATH_ARG_NAMES = ("path", "file", "filename", "file_path", "filepath", "name")


# A name maps to every static value it may hold: a ``str``, or a ``tuple`` for a list/tuple
# of strings (``None`` marks an element whose value is unknown).
_Seq = tuple[Optional[str], ...]
_Env = dict[str, set[Any]]
_MAX_STATIC_VALUES = 64
_MAX_SEQ_LEN = 256
_MAX_LOOP_PASSES = 4
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, *_COMPREHENSIONS)


def _copy_env(env: _Env) -> _Env:
    return {k: set(v) for k, v in env.items()}


def _merge_envs(*envs: _Env) -> _Env:
    # A name may hold any value it holds on any incoming path (fail closed); a path on which
    # it is unknown contributes nothing.
    out: _Env = {}
    for env in envs:
        for k, v in env.items():
            out.setdefault(k, set()).update(v)
    return {k: _bounded(v) for k, v in out.items()}


def _value_order(value: Any) -> tuple[int, str]:
    if isinstance(value, str):
        return (0, value)
    return (1, repr(value)) if isinstance(value, tuple) else (2, repr(value))


class _StaticDict:
    """A statically known ``dict`` with string keys: ``key -> every value it may hold``.

    An empty value set marks a key whose value is unknown.
    """

    __slots__ = ("items",)

    def __init__(self, items: Any) -> None:
        self.items: tuple[tuple[str, frozenset[Any]], ...] = tuple(sorted((k, frozenset(v)) for k, v in items))

    def get(self, names: tuple[str, ...]) -> Optional[frozenset[Any]]:
        for key, values in self.items:
            if key in names:
                return values
        return None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _StaticDict) and other.items == self.items

    def __hash__(self) -> int:
        return hash(self.items)

    def __repr__(self) -> str:
        return f"_StaticDict({dict((k, sorted(v, key=_value_order)) for k, v in self.items)!r})"


class _Known(ast.expr):
    """Synthetic expression standing for a set of resolved values (strings and argv tuples)."""

    _fields = ()

    def __init__(self, values: Any) -> None:
        super().__init__()
        self.values = frozenset(values)


def _bounded(values: Any) -> set[Any]:
    values = set(values)
    return values if len(values) <= _MAX_STATIC_VALUES else set(sorted(values, key=_value_order)[:_MAX_STATIC_VALUES])


def _combine(parts: list[set[str]], join: Callable[[list[str]], str]) -> set[str]:
    combos: list[list[str]] = [[]]
    for options in parts:
        combos = [c + [o] for c in combos for o in sorted(options)][:_MAX_STATIC_VALUES]
    return {join(c) for c in combos}


def _concat(parts: list[set[_Seq]]) -> set[_Seq]:
    """Every concatenation choosing one sequence per part (bounded in count and length)."""
    combos: list[_Seq] = [()]
    for options in parts:
        combos = [(c + o)[:_MAX_SEQ_LEN] for c in combos for o in sorted(options, key=repr)][:_MAX_STATIC_VALUES]
    return set(combos)


_ABS_COMPONENT_RE = re.compile(r"^(?:[/\\]|[A-Za-z]:)")


def _path_join(parts: list[str]) -> str:
    """Join path components like ``PurePath`` / ``os.path.join``: an absolute component resets."""
    out = ""
    for part in parts:
        if not out or _ABS_COMPONENT_RE.match(part):
            out = part
        elif part:
            out += part if out.endswith("/") else "/" + part
    return out


def _argv_join(seq: _Seq) -> Optional[str]:
    if all(e is None for e in seq):
        return None
    return shlex.join("*" if e is None else e for e in seq)


def _local_names(args: Optional[ast.arguments], body: list[ast.stmt]) -> set[str]:
    """Names a function scope binds itself (parameters and local stores, minus global/nonlocal)."""
    names: set[str] = set()
    if args is not None:
        names.update(a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg) if a)
    declared: set[str] = set()
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, (ast.Lambda, *_COMPREHENSIONS)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(al.asname or al.name.split(".")[0] for al in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        for attr in ("name", "rest"):
            if isinstance(node, ast.pattern) and isinstance(getattr(node, attr, None), str):
                names.add(getattr(node, attr))
        stack.extend(ast.iter_child_nodes(node))
    return names - declared


class _PyCodeScan:
    """Executed operations in Python source, read from real ``ast.Call`` nodes.

    String literals are only inspected as arguments of calls that open, search, list or
    execute something, so ``print("never open('holdout/x')")``, docstrings and comments are
    inert. Arguments resolve through literals, f-strings, ``+`` / ``/`` joins, ``Path(...)`` /
    ``os.path.join(...)`` and variables. Variables are tracked flow-sensitively: statements run
    in execution order with ``env`` mapping each name to the set of static strings it may hold
    at that point, so a later reassignment never hides an earlier read. Branches and loop
    iterations merge by union, and function bodies see their own locals plus every value a
    free name is ever bound to (they may run at any later point).
    """

    def __init__(self, tree: ast.AST) -> None:
        self.aliases: dict[str, str] = {}
        self.path_vars: set[str] = set()
        self.out: dict[str, list[Any]] = {"reads": [], "writes": [], "searches": [], "commands": [], "codes": [],
                                          "fs": [], "opaque_reads": []}
        self._seen: set[tuple[int, str, str]] = set()
        self._raise_sinks: list[Optional[list[_Env]]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for al in node.names:
                    self.aliases[al.asname or al.name.split(".")[0]] = al.name if al.asname else al.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module:
                for al in node.names:
                    self.aliases[al.asname or al.name] = f"{node.module}.{al.name}"
        self.escaped: set[str] = {n for node in ast.walk(tree) if isinstance(node, (ast.Global, ast.Nonlocal))
                                  for n in node.names}
        # A dict variable is trusted for ``**name`` only when ``**name`` is its sole use: any other
        # load (``opts['k'] = v``, ``opts.update(...)``, ``alias = opts``) may mutate it.
        unpacked = {id(kw.value) for node in ast.walk(tree) if isinstance(node, ast.Call)
                    for kw in node.keywords if kw.arg is None and isinstance(kw.value, ast.Name)}
        self.unsafe_dicts: set[str] = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
                                       and isinstance(node.ctx, ast.Load) and id(node) not in unpacked}
        self.unsafe_dicts |= self.escaped
        # A function that mutates a free list in place (``cmd.append(...)``) may run before any use.
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                body: list[ast.AST] = list(fn.body) if isinstance(fn.body, list) else [fn.body]
                local = _local_names(fn.args, [b for b in body if isinstance(b, ast.stmt)])
                for node in (n for b in body for n in ast.walk(b)):
                    recv = self._mutated_name(node)
                    if recv and recv not in local:
                        self.escaped.add(recv)
        self.may: _Env = {}
        for _ in range(2):
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                        self._assign(target, node.value, self.may, union=True)
                elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                    self._bind(node.target, self._aug_values(node, self.may), self.may, union=True)
                elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                    self._assign_iter(node.target, node.iter, self.may, union=True)
                elif isinstance(node, ast.Call):
                    self._mutate(node, self.may, union=True)

    @staticmethod
    def _mutated_name(node: ast.AST) -> Optional[str]:
        """``cmd`` for ``cmd.append(...)``-style calls and ``cmd[i] = ...`` / ``del cmd[i]`` targets."""
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _PY_SEQ_MUTATORS and isinstance(node.func.value, ast.Name)):
            return node.func.value.id
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
                and isinstance(node.value, ast.Name)):
            return node.value.id
        return None

    # --- static value resolution ---------------------------------------------------------

    def pathish(self, node: ast.expr) -> bool:
        """``Path(...)``, a variable holding one, ``p / "x"`` or ``p.joinpath(...)`` / ``.resolve()``."""
        if isinstance(node, ast.Name):
            return node.id in self.path_vars
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self.pathish(node.left)
        if isinstance(node, ast.Call):
            if self.callee(node.func) in _PY_PATH_CTORS:
                return True
            func = node.func
            return isinstance(func, ast.Attribute) and func.attr in _PY_PATH_DERIVE and self.pathish(func.value)
        return False

    def callee(self, func: ast.expr) -> Optional[str]:
        if isinstance(func, ast.Name):
            name = self.aliases.get(func.id, func.id)
        elif isinstance(func, ast.Attribute):
            base = self.callee(func.value)
            if base is None:
                return None
            name = f"{base}.{func.attr}"
        else:
            return None
        for prefix in ("hermes_tools.", "functions.", "builtins."):
            if name.startswith(prefix):
                name = name[len(prefix):]
        return name

    def statics(self, node: Optional[ast.expr], env: _Env, depth: int = 0) -> set[str]:
        """Every static string ``node`` may evaluate to under ``env`` (empty: unknown)."""
        if node is None or depth > 12:
            return set()
        if isinstance(node, _Known):
            return {v for v in node.values if isinstance(v, str)}
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bytes):
                return {node.value.decode("utf-8", "replace")}
            return {node.value} if isinstance(node.value, str) else set()
        if isinstance(node, ast.JoinedStr):
            parts = [{str(v.value)} if isinstance(v, ast.Constant)
                     else self.statics(getattr(v, "value", None), env, depth + 1) or {"*"} for v in node.values]
            return _combine(parts, "".join)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            left, right = self.statics(node.left, env, depth + 1), self.statics(node.right, env, depth + 1)
            if not left and not right:
                return set()
            return _combine([left or {"*"}, right or {"*"}], "".join if isinstance(node.op, ast.Add) else _path_join)
        if isinstance(node, ast.IfExp):
            return _bounded(self.statics(node.body, env, depth + 1) | self.statics(node.orelse, env, depth + 1))
        if isinstance(node, ast.Name):
            return {v for v in self._name_values(node.id, env) if isinstance(v, str)}
        if isinstance(node, ast.Subscript):
            index = self._const_index(node.slice)
            if index is None:
                return set()
            items = (s[index] for s in self.seqs(node.value, env, depth + 1) if -len(s) <= index < len(s))
            return {v for v in items if isinstance(v, str)}
        if isinstance(node, ast.Call):
            name = self.callee(node.func)
            if name in _PY_PATH_CTORS or name in _PY_PATH_JOINS:
                parts = [self.statics(a, env, depth + 1) for a in node.args]
                if not parts:
                    return {"."} if name in _PY_PATH_CTORS else set()
                if not any(parts):
                    return set()
                return _combine([p or {"*"} for p in parts], _path_join)
            if isinstance(node.func, ast.Attribute) and node.func.attr in _PY_PATH_DERIVE:
                base = self.statics(node.func.value, env, depth + 1)
                if not base:
                    return set()
                return _combine([base, *(self.statics(a, env, depth + 1) or {"*"} for a in node.args)], _path_join)
        return set()

    def _name_values(self, name: str, env: _Env) -> set[Any]:
        values = set(env.get(name, ()))
        # A `global` / `nonlocal` write (or an in-place mutation) inside some function may
        # happen before any use.
        return _bounded(values | self.may.get(name, set())) if name in self.escaped else values

    @staticmethod
    def _const_index(node: Optional[ast.expr]) -> Optional[int]:
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = _PyCodeScan._const_index(node.operand)
            return None if inner is None else -inner
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return node.value
        return None

    def seqs(self, node: Optional[ast.expr], env: _Env, depth: int = 0) -> set[_Seq]:
        """Every static list/tuple of strings ``node`` may evaluate to (empty: unknown)."""
        if node is None or depth > 12:
            return set()
        if isinstance(node, _Known):
            return {v for v in node.values if isinstance(v, tuple)}
        if isinstance(node, (ast.List, ast.Tuple)):
            parts: list[set[_Seq]] = []
            for elt in node.elts:
                if isinstance(elt, ast.Starred):
                    parts.append(self.seqs(elt.value, env, depth + 1) or {(None,)})
                else:
                    parts.append({(v,) for v in self.statics(elt, env, depth + 1)} or {(None,)})
            return _concat(parts)
        if isinstance(node, ast.Name):
            return {v for v in self._name_values(node.id, env) if isinstance(v, tuple)}
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.seqs(node.left, env, depth + 1), self.seqs(node.right, env, depth + 1)
            return _concat([left, right]) if left and right else set()
        if isinstance(node, ast.IfExp):
            return _bounded(self.seqs(node.body, env, depth + 1) | self.seqs(node.orelse, env, depth + 1))
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            sl = node.slice
            bounds = [self._const_index(b) if b is not None else None for b in (sl.lower, sl.upper, sl.step)]
            if any(b is None and raw is not None for b, raw in zip(bounds, (sl.lower, sl.upper, sl.step))) or bounds[2] == 0:
                return set()
            return {s[slice(*bounds)] for s in self.seqs(node.value, env, depth + 1)}
        if isinstance(node, ast.Call):
            name = self.callee(node.func) or ""
            first = node.args[0] if node.args and not isinstance(node.args[0], ast.Starred) else None
            if name in ("list", "tuple") and first is not None:
                return self.seqs(first, env, depth + 1)
            if name == "shlex.split" and first is not None:
                out: set[_Seq] = set()
                for text in self.statics(first, env, depth + 1):
                    try:
                        out.add(tuple(shlex.split(text))[:_MAX_SEQ_LEN])
                    except ValueError:
                        continue
                return _bounded(out)
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "copy" and not node.args:
                return self.seqs(func.value, env, depth + 1)
            if isinstance(func, ast.Attribute) and func.attr == "split" and not node.keywords and len(node.args) <= 1:
                seps = self.statics(first, env, depth + 1) if node.args else {None}
                texts = self.statics(func.value, env, depth + 1)
                return _bounded({tuple(t.split(s) if s else t.split())[:_MAX_SEQ_LEN] for t in texts for s in seps})
        return set()

    def commands(self, node: Optional[ast.expr], env: _Env) -> set[str]:
        out = set(self.statics(node, env))
        for seq in self.seqs(node, env):
            joined = _argv_join(seq)
            if joined:
                out.add(joined)
        return out

    def dicts(self, node: Optional[ast.expr], env: _Env, depth: int = 0) -> set[_StaticDict]:
        """Every static string-keyed dict ``node`` may evaluate to (empty: unknown)."""
        if node is None or depth > 12:
            return set()
        if isinstance(node, ast.Dict):
            if not all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
                return set()  # ``{**other, ...}`` or a computed key
            return {_StaticDict((k.value, self._values(v, env)) for k, v in zip(node.keys, node.values))}
        if isinstance(node, ast.Call) and self.callee(node.func) == "dict":
            if node.args or any(kw.arg is None for kw in node.keywords):
                return set()
            return {_StaticDict((kw.arg, self._values(kw.value, env)) for kw in node.keywords)}
        if isinstance(node, ast.Name):
            if node.id in self.unsafe_dicts:
                return set()
            return {v for v in self._name_values(node.id, env) if isinstance(v, _StaticDict)}
        if isinstance(node, ast.IfExp):
            body, orelse = self.dicts(node.body, env, depth + 1), self.dicts(node.orelse, env, depth + 1)
            return _bounded(body | orelse) if body and orelse else set()
        return set()

    def arg(self, call: ast.Call, index: Optional[int], *names: str, env: Optional[_Env] = None,
            default: Optional[str] = None) -> Optional[ast.expr]:
        """The argument passed as keyword ``names`` / position ``index``, or through ``**mapping``.

        A statically known ``**{...}`` / ``**opts`` supplies its keys like explicit keywords
        (``default`` joins the values when some binding omits the key). An unknown mapping, or a
        known key with an unknown value, returns ``None`` so callers fall back to the omitted-
        argument default (content search at the repo root, read mode).
        """
        for kw in call.keywords:
            if kw.arg in names:
                return kw.value
        if index is not None and index < len(call.args) and not isinstance(call.args[index], ast.Starred):
            return call.args[index]
        unpacks = [kw.value for kw in call.keywords if kw.arg is None]
        if not unpacks:
            return None
        values: set[Any] = set()
        omitted = True
        for expr in unpacks:
            options = self.dicts(expr, env or {})
            if not options:
                return None
            lacks = False
            for option in options:
                found = option.get(names)
                if found is None:
                    lacks = True
                elif not found:
                    return None
                else:
                    values |= found
            omitted = omitted and lacks
        if not values:
            return None
        if omitted and default is not None:
            values.add(default)
        return _Known(values)

    # --- bindings --------------------------------------------------------------------------

    def _bind(self, target: ast.expr, values: set[Any], env: _Env, *, union: bool = False) -> None:
        if isinstance(target, ast.Name):
            if union:
                if values:
                    env[target.id] = _bounded(env.get(target.id, set()) | values)
            elif values:
                env[target.id] = _bounded(values)
            else:
                env.pop(target.id, None)
            return
        if not union:
            for n in ast.walk(target):
                if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    env.pop(n.id, None)

    def _values(self, node: ast.expr, env: _Env) -> set[Any]:
        """Static strings, string sequences and string-keyed dicts ``node`` may evaluate to."""
        return _bounded(self.statics(node, env) | self.seqs(node, env) | self.dicts(node, env))

    def _pairs(self, target: ast.expr, value: ast.expr, env: _Env) -> list[tuple[ast.expr, set[Any]]]:
        if (isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
                and len(target.elts) == len(value.elts)
                and not any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))):
            return [p for t, v in zip(target.elts, value.elts) for p in self._pairs(t, v, env)]
        if isinstance(target, (ast.Tuple, ast.List)) and not any(isinstance(e, ast.Starred) for e in target.elts):
            # ``a, b = cmd``: unpack every known sequence of the right length.
            n = len(target.elts)
            options = [s for s in self.seqs(value, env) if len(s) == n]
            if options:
                return [(t, {s[i] for s in options if s[i] is not None}) for i, t in enumerate(target.elts)]
        if isinstance(target, ast.Name) and self.pathish(value):
            self.path_vars.add(target.id)
        return [(target, self._values(value, env))]

    def _assign(self, target: ast.expr, value: ast.expr, env: _Env, *, union: bool = False) -> None:
        if isinstance(target, ast.Subscript):
            self._set_item(target, self.statics(value, env), env, union=union)
            return
        # Evaluate every right-hand value before binding any name (``a, b = b, a``).
        for t, values in self._pairs(target, value, env):
            self._bind(t, values, env, union=union)

    def _assign_iter(self, target: ast.expr, iterable: ast.expr, env: _Env, *, union: bool = False) -> None:
        values: set[Any] = set()
        if isinstance(iterable, (ast.List, ast.Tuple, ast.Set)):
            for elt in iterable.elts:
                if isinstance(elt, ast.Starred):
                    values |= {e for s in self.seqs(elt.value, env) for e in s if e is not None}
                else:
                    values |= self._values(elt, env)
        else:
            # ``paths = [...]; for p in paths:`` binds every known element.
            values |= {e for s in self.seqs(iterable, env) for e in s if e is not None}
        self._bind(target, _bounded(values), env, union=union)

    def _aug_values(self, node: ast.AugAssign, env: _Env) -> set[Any]:
        name = node.target.id if isinstance(node.target, ast.Name) else ""
        return self._values(ast.BinOp(left=ast.Name(id=name, ctx=ast.Load()), op=node.op, right=node.value), env)

    def _update_seqs(self, name: str, change: Callable[[_Seq], set[_Seq]], env: _Env, *, union: bool) -> None:
        """Apply an in-place list mutation to ``name`` and (weakly) to every alias of it."""
        before = {v for v in self._name_values(name, env) if isinstance(v, tuple)}
        if not before:
            return
        after: set[_Seq] = set()
        for seq in sorted(before, key=repr):
            after |= {s[:_MAX_SEQ_LEN] for s in change(seq)}
        after = _bounded(after)
        for other, values in list(env.items()):
            if other != name and values & before:
                env[other] = _bounded(values | after)  # an alias of the same list object
        kept = env.get(name, set()) if union else {v for v in env.get(name, set()) if not isinstance(v, tuple)}
        env[name] = _bounded(kept | after)

    def _set_item(self, target: ast.Subscript, values: set[str], env: _Env, *, union: bool = False) -> None:
        if not isinstance(target.value, ast.Name):
            return
        index = self._const_index(target.slice)
        options: set[Optional[str]] = set(values) or {None}

        def change(seq: _Seq) -> set[_Seq]:
            if index is None or not -len(seq) <= index < len(seq):
                return {seq}
            pos = index % len(seq)
            return {seq[:pos] + (v,) + seq[pos + 1:] for v in options}

        self._update_seqs(target.value.id, change, env, union=union)

    def _del_item(self, target: ast.expr, env: _Env) -> None:
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            index = self._const_index(target.slice)

            def change(seq: _Seq) -> set[_Seq]:
                if index is None or not -len(seq) <= index < len(seq):
                    return {seq}
                pos = index % len(seq)
                return {seq[:pos] + seq[pos + 1:]}

            self._update_seqs(target.value.id, change, env, union=False)

    def _mutate(self, node: ast.Call, env: _Env, *, union: bool = False) -> None:
        """``cmd.append(x)`` / ``extend`` / ``insert`` / ``pop`` / ``remove`` / ``clear`` / ``reverse``."""
        name = self._mutated_name(node)
        if name is None or not isinstance(node.func, ast.Attribute):
            return
        attr, args = node.func.attr, node.args
        if any(isinstance(a, ast.Starred) for a in args) or node.keywords:
            return

        def item(arg: ast.expr) -> set[Optional[str]]:
            return set(self.statics(arg, env)) or {None}

        def change(seq: _Seq) -> set[_Seq]:
            if attr == "append" and len(args) == 1:
                return {seq + (v,) for v in item(args[0])}
            if attr == "extend" and len(args) == 1:
                return {seq + s for s in self.seqs(args[0], env)} or {seq + (None,)}
            if attr == "insert" and len(args) == 2:
                index = self._const_index(args[0])
                if index is None:
                    spots = {0, len(seq)}  # an unknown index may put the item first or last
                else:
                    spots = {max(0, min(len(seq), index + len(seq) if index < 0 else index))}
                return {seq[:p] + (v,) + seq[p:] for p in spots for v in item(args[1])}
            if attr == "pop" and len(args) <= 1 and seq:
                index = self._const_index(args[0]) if args else -1
                spots = {0, len(seq) - 1} if index is None else (
                    {index % len(seq)} if -len(seq) <= index < len(seq) else set())
                return {seq[:p] + seq[p + 1:] for p in spots} or {seq}
            if attr == "remove" and len(args) == 1:
                out = set()
                for v in self.statics(args[0], env):
                    if v in seq:
                        p = seq.index(v)
                        out.add(seq[:p] + seq[p + 1:])
                return out or {seq}
            if attr == "clear" and not args:
                return {()}
            if attr == "reverse" and not args:
                return {seq[::-1]}
            return {seq}

        self._update_seqs(name, change, env, union=union)

    # --- execution-order walk ----------------------------------------------------------------

    def ops(self, tree: ast.AST) -> dict[str, list[Any]]:
        if isinstance(tree, ast.Module):
            self._block(tree.body, {})
        else:
            self._expr(tree, {})
        return self.out

    def _block(self, stmts: list[ast.stmt], env: _Env) -> _Env:
        for st in stmts:
            env = self._stmt(st, env)
            self._note_raising(env)
        return env

    def _note_raising(self, env: _Env) -> None:
        """Record ``env`` as a state an exception may leave from, for every enclosing ``try``."""
        for sink in reversed(self._raise_sinks):
            if sink is None:
                break  # a function / class body: its locals never reach the caller's handlers
            sink[0] = _merge_envs(sink[0], env)

    def _collect_raising(self, run: Callable[[], _Env], start: _Env) -> tuple[_Env, _Env]:
        """Run ``run()`` and return ``(end state, every state an exception may leave from)``."""
        sink = [_copy_env(start)]
        self._raise_sinks.append(sink)
        try:
            end = run()
        finally:
            self._raise_sinks.pop()
        return end, _merge_envs(sink[0], end)

    def _targets(self, target: ast.expr, env: _Env) -> None:
        """Scan the evaluated parts of an assignment target (``obj.attr`` / ``obj[key]``)."""
        if isinstance(target, ast.Attribute):
            self._expr(target.value, env)
        elif isinstance(target, ast.Subscript):
            self._expr(target.value, env)
            self._expr(target.slice, env)
        elif isinstance(target, ast.Starred):
            self._targets(target.value, env)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._targets(elt, env)

    def _stmt(self, st: ast.stmt, env: _Env) -> _Env:
        if isinstance(st, ast.Assign):
            self._expr(st.value, env)
            for target in st.targets:
                self._targets(target, env)
            for target in st.targets:
                self._assign(target, st.value, env)
            return env
        if isinstance(st, ast.AnnAssign):
            self._expr(st.value, env)
            self._targets(st.target, env)
            if st.value is not None:
                self._assign(st.target, st.value, env)
            return env
        if isinstance(st, ast.AugAssign):
            self._expr(st.value, env)
            self._targets(st.target, env)
            if isinstance(st.target, ast.Name):
                self._bind(st.target, self._aug_values(st, env), env)
            return env
        if isinstance(st, ast.If):
            self._expr(st.test, env)
            return _merge_envs(self._block(st.body, _copy_env(env)), self._block(st.orelse, _copy_env(env)))
        if isinstance(st, (ast.For, ast.AsyncFor)):
            self._expr(st.iter, env)
            return self._loop(st, env, lambda e: self._assign_iter(st.target, st.iter, e))
        if isinstance(st, ast.While):
            return self._loop(st, env, lambda e: self._expr(st.test, e), test=st.test)
        if isinstance(st, (ast.With, ast.AsyncWith)):
            for item in st.items:
                self._expr(item.context_expr, env)
                if item.optional_vars is not None:
                    self._targets(item.optional_vars, env)
                    self._bind(item.optional_vars, set(), env)
            return self._block(st.body, env)
        if isinstance(st, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            # Handlers start from the try-entry state merged with the state after every body
            # statement (at any nesting depth): a raise or a raising call may leave at any of them.
            body, raised = self._collect_raising(lambda: self._try_body(st.body, _copy_env(env)), env)

            def rest() -> _Env:
                outs = []
                for handler in st.handlers:
                    h_env = _copy_env(raised)
                    self._expr(handler.type, h_env)
                    if handler.name:
                        h_env.pop(handler.name, None)
                    outs.append(self._block(handler.body, h_env))
                return _merge_envs(self._block(st.orelse, body), *outs)

            # ``finally`` also runs when a handler or ``else`` block itself raises part-way.
            done, leaving = self._collect_raising(rest, raised)
            return self._block(st.finalbody, _merge_envs(raised, done, leaving))
        if isinstance(st, ast.Match):
            self._expr(st.subject, env)
            outs = [_copy_env(env)]
            for case in st.cases:
                c_env = _copy_env(env)
                for pat in ast.walk(case.pattern):
                    for attr in ("name", "rest"):
                        if isinstance(getattr(pat, attr, None), str):
                            c_env.pop(getattr(pat, attr), None)
                self._expr(case.guard, c_env)
                outs.append(self._block(case.body, c_env))
            return _merge_envs(*outs)
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for expr in (*st.decorator_list, *st.args.defaults, *st.args.kw_defaults):
                self._expr(expr, env)
            self._function(st.args, st.body, env)
            env.pop(st.name, None)
            return env
        if isinstance(st, ast.ClassDef):
            for expr in (*st.decorator_list, *st.bases, *(k.value for k in st.keywords)):
                self._expr(expr, env)
            self._raise_sinks.append(None)
            try:
                self._block(st.body, _copy_env(env))
            finally:
                self._raise_sinks.pop()
            env.pop(st.name, None)
            return env
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            for al in st.names:
                env.pop(al.asname or al.name.split(".")[0], None)
            return env
        if isinstance(st, ast.Delete):
            for target in st.targets:
                self._targets(target, env)
                self._del_item(target, env)
                self._bind(target, set(), env)
            return env
        # Any other statement: scan its expressions in order and run nested bodies as branches.
        branches: list[_Env] = []
        for _field, value in ast.iter_fields(st):
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.expr):
                    self._expr(child, env)
            if isinstance(value, list) and value and all(isinstance(c, ast.stmt) for c in value):
                branches.append(self._block(value, _copy_env(env)))
        return _merge_envs(env, *branches) if branches else env

    def _try_body(self, stmts: list[ast.stmt], env: _Env) -> _Env:
        """Walk a ``try`` body in order, stopping after a statement that always leaves it."""
        for st in stmts:
            env = self._stmt(st, env)
            self._note_raising(env)
            if isinstance(st, (ast.Raise, ast.Return, ast.Break, ast.Continue)):
                break
        return env

    def _loop(self, st: Any, env: _Env, enter: Callable[[_Env], None], test: Optional[ast.expr] = None) -> _Env:
        # Iterate to a fixpoint so values assigned late in the body reach earlier calls via the
        # back edge; the loop may also run zero times.
        state = _copy_env(env)
        for _ in range(_MAX_LOOP_PASSES):
            body_env = _copy_env(state)
            enter(body_env)
            merged = _merge_envs(state, self._block(st.body, body_env))
            if merged == state:
                break
            state = merged
        if test is not None:
            self._expr(test, state)
        return self._block(st.orelse, _merge_envs(env, state))

    def _function(self, args: ast.arguments, body: Any, env: _Env) -> None:
        local = _local_names(args, body if isinstance(body, list) else [])
        f_env = {k: set(v) for k, v in env.items() if k not in local}
        for k, v in self.may.items():
            if k not in local:
                f_env[k] = _bounded(f_env.get(k, set()) | v)
        self._raise_sinks.append(None)
        try:
            if isinstance(body, list):
                self._block(body, f_env)
            else:
                self._expr(body, f_env)
        finally:
            self._raise_sinks.pop()

    def _expr(self, node: Optional[ast.AST], env: _Env) -> None:
        if node is None:
            return
        if isinstance(node, _COMPREHENSIONS):
            local = _copy_env(env)
            for gen in node.generators:
                self._expr(gen.iter, local)
                self._assign_iter(gen.target, gen.iter, local)
                for cond in gen.ifs:
                    self._expr(cond, local)
            for part in (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,):
                self._expr(part, local)
            return
        if isinstance(node, ast.Lambda):
            for expr in (*node.args.defaults, *node.args.kw_defaults):
                self._expr(expr, env)
            self._function(node.args, node.body, env)
            return
        if isinstance(node, ast.NamedExpr):
            self._expr(node.value, env)
            self._assign(node.target, node.value, env)
            return
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.stmt):
                self._expr(child, env)
        if isinstance(node, ast.Call):
            self._call(node, env)

    def _emit(self, kind: str, node: ast.AST, item: Any) -> None:
        # A call revisited on another loop pass reports each distinct value once.
        key = (id(node), kind, repr(item))
        if key not in self._seen:
            self._seen.add(key)
            self.out[kind].append(item)

    def _write_only(self, mode: Optional[ast.expr], env: _Env) -> bool:
        """True only when ``mode`` is given and every value it may hold writes without reading.

        An omitted mode defaults to ``"r"``; an unresolvable mode or any ``r`` / ``+`` mode
        (``"r+"``, ``"w+"``, ``"a+"``) can read the file and fails closed.
        """
        modes = self.statics(mode, env) if mode is not None else set()
        return bool(modes) and all(
            m and set(m) <= set("waxbt") and any(c in m for c in "wax") for m in modes
        )

    @staticmethod
    def _path_from_unpack(node: ast.Call) -> bool:
        """The path argument can only come from a ``**mapping`` (no explicit path argument)."""
        if node.args or any(kw.arg in _PATH_ARG_NAMES for kw in node.keywords):
            return False
        return any(kw.arg is None for kw in node.keywords)

    def _call(self, node: ast.Call, env: _Env) -> None:
        name = self.callee(node.func) or ""
        if name in _PY_COMMAND_CALLS:
            how, at = _PY_ARGV_AT.get(name, ("arg", 0))
            if how == "var":
                cmds = self.commands(ast.List(elts=list(node.args[at:]), ctx=ast.Load()), env)
            else:
                cmds = self.commands(self.arg(node, at, "command", "cmd", "args", "argv", env=env), env)
            for cmd in sorted(cmds):
                if cmd:
                    self._emit("commands", node, cmd)
        elif name in _PY_EVAL_CALLS:
            for src in sorted(self.statics(self.arg(node, 0, "source", env=env), env)):
                if src:
                    self._emit("codes", node, src)
        elif name in _PY_READ_CALLS:
            kind = "reads"
            if name in _PY_MODE_OPENS and self._write_only(
                    self.arg(node, _PY_MODE_OPENS[name], "mode", env=env, default="r"), env):
                kind = "writes"
            paths = self.statics(self.arg(node, 0, *_PATH_ARG_NAMES, env=env), env)
            for path in sorted(paths):
                self._emit(kind, node, (path, False))
            if not paths and kind == "reads" and self._path_from_unpack(node):
                # ``read_file(**opts)`` whose mapping is unknown, mutated or holds an unknown path:
                # the target is opaque, so the read fails closed in every barrier phase.
                self._emit("opaque_reads", node, f"{name}(**kwargs)")
        elif name in _PY_CONTENT_SEARCH_CALLS:
            sf = name == "search_files"
            target_node = self.arg(node, 1 if sf else None, "target", env=env, default="content")
            targets = self.statics(target_node, env) or {"content"}
            if any(t.strip().lower() != "files" for t in targets):
                root_node = self.arg(node, 2 if sf else None, "path", "paths", "dir", "directory", env=env, default=".")
                roots = self.statics(root_node, env)
                globs = sorted(self.statics(self.arg(node, 3 if sf else None, "file_glob", "glob", "include", env=env),
                                            env) - {""})
                if root_node is None:
                    self._emit("searches", node, ([], globs))
                for root in sorted(roots):
                    self._emit("searches", node, ([root], globs))
        elif name in _PY_LISTING_TOOLS:
            for path in sorted(self.statics(self.arg(node, None, "path", "paths", "dir", "directory", env=env), env)):
                self._emit("reads", node, (path, True))
        if name in _PY_FS_CALLS or name.startswith("shutil."):
            for a in [*node.args, *(k.value for k in node.keywords)]:
                for lit in sorted(self.statics(a, env)):
                    self._emit("fs", node, lit)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("open", *_PY_PATH_READ_METHODS) and self.pathish(func.value):
            kind = "writes" if func.attr == "open" and self._write_only(self.arg(node, 0, "mode", env=env, default="r"), env) else "reads"
            for path in sorted(self.statics(func.value, env)):
                self._emit(kind, node, (path, False))
        self._mutate(node, env)


def _code_ops(code: str, lang: str = "python") -> dict[str, list[Any]]:
    """Executed operations in program text: AST for Python, comment/print-stripped regex otherwise."""
    if lang == "python":
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            tree = None
        if tree is not None:
            try:
                return _PyCodeScan(tree).ops(tree)
            except RecursionError:
                pass  # pathologically deep nesting: fall back to the literal regex scan below
    text = _strip_code_noise(code, lang)
    return {"reads": _code_read_targets(text), "writes": [], "searches": _code_searches(text),
            "commands": _code_commands(text), "codes": [], "fs": _code_fs_targets(text)}


def _code_findings(code: str, canon: str, depth: int = 0, lang: str = "python") -> list[tuple[str, str]]:
    """Barrier findings for program text (`execute_code`, `python3 -c`, `node -e`, ...)."""
    found: list[tuple[str, str]] = []
    if depth > 3:
        return found
    holdout = canon in _HOLDOUT_PHASES
    if lang == "shell":
        if holdout:
            hit = _holdout_in_command(code, depth + 1)
            return [("holdout_access", hit)] if hit else []
        return _blackbox_command_findings(code, depth + 1) if canon == "verify-blackbox" else []
    ops = _code_ops(code, lang)
    if canon in _HOLDOUT_PHASES or canon == "verify-blackbox":
        rule = "holdout_access" if holdout else "source_read"
        found.extend((rule, f"<unresolved path: {call}>") for call in ops.get("opaque_reads", ()))
    for path, search in ops["reads"]:
        if holdout and _HOLDOUT_TOKEN_RE.search(_norm_path(path)):
            found.append(("holdout_access", path))
        elif canon == "verify-blackbox" and _is_source_target(path, search=search):
            found.append(("source_read", path))
    # A write-only open never reads source, but creating files under holdout/ still touches it.
    if holdout:
        found.extend(("holdout_access", path) for path, _ in ops["writes"] if _HOLDOUT_TOKEN_RE.search(_norm_path(path)))
    for roots, globs in ops["searches"]:
        if holdout:
            found.extend(("holdout_access", v) for v in (*roots, *globs)
                         if _HOLDOUT_TOKEN_RE.search(_norm_path(v)) and not v.strip().startswith("!"))
        elif canon == "verify-blackbox":
            found.extend(("source_read", v) for v in _content_search_hits(roots, globs))
    for cmd in ops["commands"]:
        if holdout:
            hit = _holdout_in_command(cmd, depth + 1)
            if hit:
                found.append(("holdout_access", hit))
        elif canon == "verify-blackbox":
            found.extend(_blackbox_command_findings(cmd, depth + 1))
    for sub in ops["codes"]:
        found.extend(_code_findings(sub, canon, depth + 1, "python"))
    if holdout:
        hits = [_norm_path(p) for _, p in found]
        for path in ops["fs"]:
            norm = _norm_path(path)
            if _HOLDOUT_TOKEN_RE.search(norm) and not any(h == norm or h.startswith(norm + "/") for h in hits):
                found.append(("holdout_access", path))
                hits.append(norm)
    return found


def _holdout_in_command(command: str, depth: int = 0) -> Optional[str]:
    # Substitutions run before the command that receives their output (even ``echo``).
    for sub in _command_substitutions(command) if depth < 3 else ():
        hit = _holdout_in_command(sub, depth + 1)
        if hit:
            return hit
    plist = _parse_command(command)
    for idx, parsed in enumerate(plist):
        for target in (*parsed["inputs"], *parsed["outputs"]):
            if _HOLDOUT_TOKEN_RE.search(_norm_path(target)):
                return target
        prog = _stdin_program(plist, idx) if depth < 3 else None
        if prog is not None:
            lang, body = prog
            if lang == "shell":
                hit = _holdout_in_command(body, depth + 1)
            else:
                hits = _code_findings(body, "implement", depth + 1, lang)
                hit = hits[0][1] if hits else None
            if hit:
                return hit
        seg = _strip_prefix(parsed["argv"])
        if not seg:
            continue
        verb = PurePosixPath(seg[0]).name
        if verb in ("echo", "printf", "true", ":"):
            continue
        shell = _shell_command_string_at(seg)
        if shell is not None and depth < 3:
            hit = _holdout_in_command(shell[1], depth + 1)
            if hit:
                return hit
        inline = shell or _interpreter_code_at(seg)
        code_idx = inline[0] if inline else None
        if inline is not None and shell is None and depth < 3:
            hits = _code_findings(inline[1], "implement", depth + 1, _interp_lang(verb) or "other")
            if hits:
                return hits[0][1]
        grep = _grep_parse(seg)
        skip = grep["skip"] if grep else set()
        for i, tok in enumerate(seg):
            if i in skip or i == code_idx:
                continue
            if _HOLDOUT_TOKEN_RE.search(tok) and not _is_excluded(seg, i):
                return tok
    return None


def _git_subcommand(seg: list[str]) -> tuple[Optional[str], list[str]]:
    i = 1
    while i < len(seg):
        tok = seg[i]
        if tok in _GIT_OPTS_WITH_VALUE:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return tok, seg[i + 1:]
    return None, []


# ``gh`` global / inherited options that consume the next token (unless given as ``--opt=value``).
_GH_OPTS_WITH_VALUE = frozenset({"-R", "--repo", "--hostname", "--color", "-q", "--jq", "-t", "--template"})


def _gh_subcommands(seg: list[str]) -> list[str]:
    """Positional ``gh`` tokens (``pr``, ``diff``, ...) with options and their values skipped."""
    out: list[str] = []
    i = 1
    while i < len(seg):
        tok = seg[i]
        if tok == "--":
            out.extend(seg[i + 1:])
            break
        if tok.startswith("-") and len(tok) > 1:
            i += 2 if tok in _GH_OPTS_WITH_VALUE else 1
            continue
        out.append(tok)
        i += 1
    return out


_SOURCE_DIRS = frozenset({"src", "lib", "app", "pkg", "internal", "cmd", "source", "sources"})
_RUN_ARTIFACT_DIRS = frozenset({".factonio", "holdout"})


def _norm_path(path: str) -> str:
    """Lexically normalize a path (``\\`` -> ``/``, collapse ``.`` / ``..``) without touching disk."""
    p = str(path).strip().strip("\"'").replace("\\", "/")
    return posixpath.normpath(p) if p else ""


def _blackbox_allowed(path: str) -> bool:
    """True only when the *normalized* path is contained in a permitted artifact root.

    Relative paths must start with `.factonio/`, `holdout/` or `design/` (after collapsing
    `..`, so `.factonio/../src/app.py` is `src/app.py`). Absolute paths (and paths that climb
    out with leading `..`) point outside the checkout and are allowed only inside a run-dir
    `.factonio/` or `holdout/` directory.
    """
    norm = _norm_path(path)
    if not norm or norm == ".":
        return False
    if PurePosixPath(norm).name.lower() in _BLACKBOX_ALLOWED_NAMES:
        return True
    parts = PurePosixPath(norm).parts
    if norm.startswith("/") or re.match(r"^[A-Za-z]:/", norm) or parts[0] in ("..", "~"):
        return any(part in _RUN_ARTIFACT_DIRS for part in parts[:-1])
    return parts[0] in _BLACKBOX_ALLOWED_DIRS and len(parts) > 1


def _is_source_target(path: str, *, search: bool = False) -> bool:
    """A path whose read exposes implementation source (`search`: also whole source dirs)."""
    raw = str(path).strip().strip("\"'")
    if not raw or raw.startswith("-") or raw.startswith("!"):
        return False
    norm = _norm_path(raw)
    if _blackbox_allowed(norm):
        return False
    if PurePosixPath(norm).suffix.lower() in _SOURCE_EXTS:
        return True
    if search:
        parts = PurePosixPath(norm).parts
        return bool(parts) and parts[0] in _SOURCE_DIRS
    return False


def _is_source_path(path: str) -> bool:
    return _is_source_target(path)


_SEARCH_ALLOWED_ROOTS = frozenset({*_BLACKBOX_ALLOWED_DIRS, "docs", "doc"})


def _search_root_exposes_source(target: str) -> bool:
    """A recursive content-search root that may read implementation source.

    Repo-root operands (`.`, `./`, an omitted path) and directory-like operands outside the
    permitted black-box roots (`.factonio/`, `holdout/`, `design/`, `docs/`) search the
    implementation tree. Absolute or `..` / `~` roots are permitted only when they end in a
    permitted root (`/runs/x/holdout`, `/checkout/.factonio`); `/checkout/src` is source.
    """
    if _is_source_target(target, search=True):
        return True
    norm = _norm_path(target)
    if norm in ("", ".", "..", "*"):
        return True
    if _blackbox_allowed(norm):
        return False
    parts = PurePosixPath(norm).parts
    outside = norm.startswith("/") or bool(re.match(r"^[A-Za-z]:/", norm)) or parts[0] in ("..", "~")
    if outside:
        if parts[-1] in _SEARCH_ALLOWED_ROOTS:
            return False
    elif parts[0] in _SEARCH_ALLOWED_ROOTS:
        return False
    return str(target).rstrip().endswith("/") or not PurePosixPath(norm).suffix


def _glob_stays_permitted(glob: str) -> bool:
    """A search glob that restricts matches to non-source files (`.factonio/**`, `*.md`, `*.json`).

    Globs without a concrete extension (`**/*`), with wildcard or brace extensions
    (`*.{py,md}`, `*.p?`), or naming a source extension / source dir never restrict.
    """
    norm = _norm_path(glob)
    if not norm or norm == ".":
        return False
    if _blackbox_allowed(norm) or PurePosixPath(norm).parts[0] in _SEARCH_ALLOWED_ROOTS:
        return True
    suffix = PurePosixPath(norm).suffix.lower()
    if not suffix or "{" in norm or any(ch in suffix for ch in "*?["):
        return False
    return suffix not in _SOURCE_EXTS and not _is_source_target(norm, search=True)


def _content_search_hits(roots: Any, globs: Any) -> list[str]:
    """Roots/globs of a recursive *content* search that expose implementation source.

    Shared by shell `rg` / `grep -R` / `git grep`, structured `search_files(target="content")`
    calls and code-tool searches. No root means the current directory (the repo root). A
    root outside the permitted black-box roots is a source read unless every positive glob
    restricts matches to non-source files; explicit source files are always source reads.
    """
    explicit = [str(r) for r in roots if str(r).strip()]
    positive = [str(g) for g in globs if str(g).strip() and not str(g).strip().startswith("!")]
    restricted = bool(positive) and all(_glob_stays_permitted(g) for g in positive)
    source_globs = [g for g in positive if _is_source_target(g, search=True)]
    hits: list[str] = []
    exposing = False
    for root in explicit or ["."]:
        if _is_source_target(root):
            hits.append(root)
            exposing = True
        elif _search_root_exposes_source(root):
            exposing = True
            if not restricted and (explicit or not source_globs):
                hits.append(root)
    if exposing:
        hits.extend(source_globs)
    return hits


def _blackbox_command_findings(command: str, depth: int = 0) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    # Substitutions run before the command that receives their output (even ``echo``).
    for sub in _command_substitutions(command) if depth < 3 else ():
        found.extend(_blackbox_command_findings(sub, depth + 1))
    plist = _parse_command(command)
    for idx, parsed in enumerate(plist):
        for target in parsed["inputs"]:
            if _is_source_target(target):
                found.append(("source_read", target))
        prog = _stdin_program(plist, idx) if depth < 3 else None
        if prog is not None:
            lang, body = prog
            if lang == "shell":
                found.extend(_blackbox_command_findings(body, depth + 1))
            else:
                found.extend(_code_findings(body, "verify-blackbox", depth + 1, lang))
        seg = _strip_prefix(parsed["argv"])
        if not seg:
            continue
        verb = PurePosixPath(seg[0]).name
        shell = _shell_command_string_at(seg)
        if shell is not None:
            if depth < 3:
                found.extend(_blackbox_command_findings(shell[1], depth + 1))
            # Operands after the command string become "$0", "$1", ... of that program.
            found.extend(("source_read", tok) for tok in seg[shell[0] + 1:] if _is_source_target(tok))
            continue
        code = _interpreter_code(seg)
        if code is not None:
            if depth < 3:
                found.extend(_code_findings(code, "verify-blackbox", depth + 1, _interp_lang(verb) or "other"))
            continue
        if verb == "git":
            sub, rest = _git_subcommand(seg)
            diffy = sub in ("diff", "show", "difftool", "format-patch") or (
                sub in ("log", "whatchanged") and any(t in ("-p", "--patch", "-u") for t in rest)
            )
            if diffy:
                found.append(("diff_inspection", " ".join(seg)))
                continue
        if verb == "gh" and _gh_subcommands(seg)[:2] == ["pr", "diff"]:
            found.append(("diff_inspection", " ".join(seg)))
            continue
        grep = _grep_parse(seg)
        if grep is not None:
            if grep["recursive"]:
                targets = set(grep["targets"])
                found.extend(("source_read", v) for v in grep["files"]
                             if v not in targets and _is_source_target(v, search=True))
                found.extend(("source_read", v) for v in _content_search_hits(grep["targets"], grep["globs"]))
                continue
            found.extend(("source_read", v) for v in grep["files"] if _is_source_target(v, search=True))
            if not any(_blackbox_allowed(f) for f in grep["files"]):
                found.extend(("source_read", g) for g in grep["globs"] if _is_source_target(g))
            continue
        if verb in _READ_VERBS:
            for tok in seg[1:]:
                if _is_source_target(tok):
                    found.append(("source_read", tok))
    return found


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


def _tool_kind(name: str, args: Mapping[str, Any]) -> str:
    low = name.lower()
    if any(isinstance(args.get(k), str) for k in _COMMAND_KEYS) and any(h in low for h in _COMMAND_TOOL_HINTS):
        return "command"
    if isinstance(args.get("code"), str) and (any(h in low for h in _CODE_TOOL_HINTS) or "exec" in low or "run" in low):
        return "code"
    if any(h in low for h in _COMMAND_TOOL_HINTS) and isinstance(args.get("_raw"), str):
        return "command"
    return "file"


def _command_of(args: Mapping[str, Any]) -> str:
    for k in (*_COMMAND_KEYS, "_raw"):
        if isinstance(args.get(k), str):
            return args[k]
    return ""


def _tool_paths(args: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for k in (*_PATH_KEYS, "_raw"):
        out.extend(_string_values(args.get(k)))
    return out


def _tool_globs(name: str, args: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for k in _GLOB_KEYS:
        out.extend(_string_values(args.get(k)))
    if str(args.get("target") or "").lower() == "files" or "glob" in name.lower():
        out.extend(_string_values(args.get("pattern")))
    return out


_WRAPPER_LIST_KEYS = ("calls", "tool_calls", "tool_uses", "invocations", "requests")


def _expand_wrappers(event: dict[str, Any], depth: int = 0) -> Iterator[dict[str, Any]]:
    """Yield an event plus the tool calls nested in batching wrappers (`tool_call` `calls`,
    `multi_tool_use.parallel` `tool_uses`, ...), which execute just like direct calls."""
    yield event
    if event.get("kind") != "tool" or depth > 3:
        return
    for key in _WRAPPER_LIST_KEYS:
        items = event["args"].get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            fn = item.get("function") if isinstance(item.get("function"), Mapping) else {}
            name = item.get("name") or item.get("tool") or item.get("recipient_name") or fn.get("name")
            args = next((item[k] for k in ("arguments", "args", "parameters", "input", "params") if k in item),
                        fn.get("arguments"))
            if name:
                yield from _expand_wrappers(_tool_event(name, args, event["index"]), depth + 1)


_CODE_LANGS = {"python": "python", "python3": "python", "py": "python", "javascript": "js", "js": "js",
               "typescript": "js", "ts": "js", "node": "js", "deno": "js", "bun": "js", "php": "php",
               "bash": "shell", "sh": "shell", "shell": "shell"}
_CONTENT_SEARCH_NAMES = ("search_files", "file_search", "search_file", "search_code", "code_search", "codebase_search",
                         "grep", "ripgrep")
_SEARCH_SCOPE_KEYS = ("path", "paths", "file_glob", "glob", "include", "target", "dir", "directory")


def _is_content_search_tool(name: str, args: Mapping[str, Any]) -> bool:
    """A structured tool call that searches file *contents* (`search_files`, default `target="content"`)."""
    low = name.lower()
    if "web" in low or "glob" in low:
        return False
    if str(args.get("target") or "content").strip().lower() != "content":
        return False
    if any(n in low for n in _CONTENT_SEARCH_NAMES):
        return True
    return "search" in low and any(k in args for k in _SEARCH_SCOPE_KEYS)


def _evidence(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= 240 else text[:237] + "..."


def audit_barrier_leaks(transcript: Any, *, phase: str) -> dict[str, Any]:
    """Flag real Information Barrier violations in a worker transcript.

    Only executed actions count: structured tool calls, command fields, and explicit
    ``$ <command>`` / ``tool: <name> ...`` trace lines in assistant or unattributed text.
    System, user, tool-result and skill text (where prohibition rules are quoted) is
    never treated as an action; such mentions are counted in ``ignored_mentions``.
    """
    canon = normalize_phase(phase)
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    actions = 0
    mentions = 0

    def add(rule: str, source: str, tool: Optional[str], evidence: str, index: int, path: Optional[str] = None) -> None:
        key = (rule, _evidence(evidence), path or "")
        if key in seen:
            return
        seen.add(key)
        item = {"rule": rule, "source": source, "tool": tool, "evidence": _evidence(evidence), "index": index}
        if path is not None:
            item["path"] = path
        findings.append(item)

    def check_command(cmd: str, source: str, tool: Optional[str], index: int) -> None:
        if canon in _HOLDOUT_PHASES:
            hit = _holdout_in_command(cmd)
            if hit:
                add("holdout_access", source, tool, cmd, index, path=hit)
        elif canon == "verify-blackbox":
            for rule, detail in _blackbox_command_findings(cmd):
                add(rule, source, tool, cmd, index, path=detail if rule == "source_read" else None)

    for event in (e for top in _iter_events(transcript) for e in _expand_wrappers(top)):
        kind = event["kind"]
        if kind == "mention":
            mentions += 1
            continue
        actions += 1
        if kind == "command":
            check_command(event["command"], "command", None, event["index"])
            continue
        name, args = event["name"], event["args"]
        tkind = _tool_kind(name, args)
        if tkind == "command":
            check_command(_command_of(args), "tool_call", name, event["index"])
            continue
        if tkind == "code":
            code = str(args.get("code") or "")
            lang = _CODE_LANGS.get(str(args.get("language") or args.get("lang") or "python").strip().lower(), "other")
            for rule, detail in _code_findings(code, canon, 0, lang):
                add(rule, "tool_call", name, code, event["index"], path=None if rule == "diff_inspection" else detail)
            continue
        paths, globs = _tool_paths(args), _tool_globs(name, args)
        if canon in _HOLDOUT_PHASES:
            for value in (*paths, *globs):
                if _HOLDOUT_TOKEN_RE.search(_norm_path(value)) and not value.strip().startswith("!"):
                    add("holdout_access", "tool_call", name, f"{name} {value}", event["index"], path=value)
        elif canon == "verify-blackbox" and _is_content_search_tool(name, args):
            for value in _content_search_hits(paths, globs):
                add("source_read", "tool_call", name, f"{name} {value}", event["index"], path=value)
        elif canon == "verify-blackbox" and str(args.get("target") or "").strip().lower() == "files":
            continue  # filename-only search: lists names, never reads file contents
        elif canon == "verify-blackbox" and any(h in name.lower() for h in _READ_TOOL_HINTS):
            search = any(h in name.lower() for h in ("search", "grep", "glob")) and (
                str(args.get("target") or "content").lower() != "files"
            )
            for value in paths:
                if _is_source_target(value, search=search):
                    add("source_read", "tool_call", name, f"{name} {value}", event["index"], path=value)
            for value in globs:
                if _is_source_target(value):
                    add("source_read", "tool_call", name, f"{name} {value}", event["index"], path=value)

    rules = (
        ["holdout_access"] if canon in _HOLDOUT_PHASES
        else ["diff_inspection", "source_read"] if canon == "verify-blackbox"
        else []
    )
    return {
        "phase": canon,
        "rules": rules,
        "clean": not findings,
        "leaked": bool(findings),
        "findings": findings,
        "inspected_actions": actions,
        "ignored_mentions": mentions,
    }


# --- Skill bloat ------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _resolve_text(path_or_text: Any) -> tuple[str, str]:
    if isinstance(path_or_text, Path):
        path = path_or_text.expanduser()
        if path.is_dir() and (path / "SKILL.md").is_file():
            path = path / "SKILL.md"
        return path.read_text(encoding="utf-8"), str(path)
    text = str(path_or_text or "")
    if text and "\n" not in text and len(text) < 4096:
        candidate = Path(text).expanduser()
        try:
            if candidate.is_dir() and (candidate / "SKILL.md").is_file():
                candidate = candidate / "SKILL.md"
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8"), str(candidate)
        except OSError:
            pass
    return text, "<text>"


def _measure(text: str) -> dict[str, int]:
    headings = 0
    fenced = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced and _HEADING_RE.match(line):
            headings += 1
    return {
        "chars": len(text),
        "lines": len(text.splitlines()),
        "words": len(text.split()),
        "estimated_tokens": math.ceil(len(text) / CHARS_PER_TOKEN),
        "headings": headings,
    }


def measure_skill_bloat(
    path_or_text: Any,
    *,
    baseline: Any = None,
    soft_tokens: int = DEFAULT_SOFT_TOKENS,
    hard_tokens: int = DEFAULT_HARD_TOKENS,
) -> dict[str, Any]:
    """Measure a skill/prompt's size against a token budget (``ok`` | ``warn`` | ``over_budget``).

    Tokens are estimated as ``ceil(chars / 4)``. ``baseline`` (path, text, or a prior
    result) adds a ``delta`` block so a skill edit's growth is visible in review.
    """
    soft, hard = int(soft_tokens), int(hard_tokens)
    if soft <= 0 or hard <= 0 or soft > hard:
        raise ValueError("token budgets must be positive with soft_tokens <= hard_tokens")
    text, source = _resolve_text(path_or_text)
    stats = _measure(text)
    tokens = stats["estimated_tokens"]
    status = "over_budget" if tokens > hard else "warn" if tokens > soft else "ok"
    result: dict[str, Any] = {
        "source": source,
        **stats,
        "soft_tokens": soft,
        "hard_tokens": hard,
        "status": status,
        "baseline": None,
        "delta": None,
    }
    if baseline is not None:
        if isinstance(baseline, Mapping) and "estimated_tokens" in baseline:
            base = {k: int(baseline.get(k) or 0) for k in stats}
            base_source = str(baseline.get("source") or "<result>")
        else:
            base_text, base_source = _resolve_text(baseline)
            base = _measure(base_text)
        delta: dict[str, Any] = {k: stats[k] - base[k] for k in stats}
        delta["pct_tokens"] = (
            round(100.0 * delta["estimated_tokens"] / base["estimated_tokens"], 2) if base["estimated_tokens"] else None
        )
        result["baseline"] = {"source": base_source, **base}
        result["delta"] = delta
    return result


# --- Rubric compliance ------------------------------------------------------------------

_SHA_RE = r"\b[0-9a-f]{40}\b|head_sha"
_VERDICT_RE = r"\bverdict\b|\b(?:pass|reject|inconclusive)\b"
_RUBRICS: dict[str, tuple[tuple[str, str, Optional[str]], ...]] = {
    "define": (
        ("spec_written", "Writes spec.md", r"spec\.md"),
        ("acceptance_criteria", "States acceptance criteria", r"acceptance criteria|- \[ \]"),
        ("verification_commands", "Lists runnable verification commands", r"verification commands|unittest|pytest|npm test"),
        ("holdout_scenarios", "Authors hidden holdout/ scenarios", r"holdout/"),
        ("task_dag", "Decomposes into a Task DAG", r"task dag|depends on"),
        ("throughput_checkpoint", "Runs the Throughput Checkpoint", r"throughput checkpoint"),
    ),
    "design": (
        ("design_doc", "Writes DESIGN.md", r"design\.md"),
        ("previews", "Produces previews", r"preview"),
        ("tokens", "Uses host theme tokens", r"token"),
        ("states", "Covers empty/loading/error states", r"\bempty\b|\bloading\b|\berror\b"),
        ("no_barrier_leak", "Never accesses holdout/", None),
    ),
    "implement": (
        ("tests_run", "Runs the test suite", r"unittest|pytest|npm (?:run )?test|go test|cargo test|ran \d+ tests"),
        ("static_checks", "Runs build/lint/typecheck", r"compileall|ruff|eslint|flake8|mypy|\btsc\b|typecheck|node --check"),
        ("pr_published", "Publishes a pull request", r"gh pr create|/pull/\d+|pr_number"),
        ("head_sha_reported", "Reports the exact head SHA", _SHA_RE),
        ("handoff", "Completes with a structured handoff", r"kanban_complete|kanban_request_review|changed_files"),
        ("no_barrier_leak", "Never accesses holdout/", None),
    ),
    "verify-diff": (
        ("verdict", "Returns a tri-state verdict", _VERDICT_RE),
        ("diff_read", "Reviews the PR diff", r"git diff|gh pr diff"),
        ("tests_run", "Re-runs tests", r"unittest|pytest|npm (?:run )?test|go test|cargo test|ran \d+ tests"),
        ("blast_radius", "Proves blast radius", r"blast[- ]radius"),
        ("head_sha_reported", "Pins the verified head SHA", _SHA_RE),
        ("no_barrier_leak", "Never accesses holdout/", None),
    ),
    "verify-blackbox": (
        ("verdict", "Returns a tri-state verdict", _VERDICT_RE),
        ("isolated_home", "Pins an isolated HERMES_HOME / temp dir", r"hermes_home|mktemp"),
        ("scenarios", "Exercises holdout scenarios", r"scenario|holdout/"),
        ("harness", "Follows the .factonio/ harness", r"\.factonio/|verify\.md"),
        ("evidence", "Captures command evidence", r"exit code|exit \d|stdout|evidence"),
        ("head_sha_reported", "Pins the verified head SHA", _SHA_RE),
        ("no_barrier_leak", "Never reads source or git diff", None),
    ),
    "orchestrator": (
        ("pr_gate", "Checks the live PR gate", r"factonio_pr_gate|pr-gate"),
        ("verification", "Spawns isolated verification", r"factonio_spawn_verify|verify"),
        ("merge_gate", "Merges only the verified head", r"head_sha|factonio_merge_pr"),
        ("trail", "Logs decisions to the trail", r"factonio_log_decision|trail"),
        ("handoff", "Hands off decision-ready", r"kanban_complete|handoff"),
    ),
}


def rubric_checks(phase: str) -> list[dict[str, str]]:
    return [{"id": cid, "description": desc} for cid, desc, _ in _RUBRICS[normalize_phase(phase)]]


def score_rubric_compliance(transcript: Any, *, phase: str, audit: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Score a transcript against a deterministic phase checklist: ``{phase, score, passed, failed}``."""
    canon = normalize_phase(phase)
    text = _transcript_text(transcript).lower()
    barrier = audit if audit is not None else audit_barrier_leaks(transcript, phase=canon)
    checks = []
    for cid, desc, pattern in _RUBRICS[canon]:
        ok = bool(barrier.get("clean")) if pattern is None else bool(re.search(pattern, text))
        checks.append({"id": cid, "description": desc, "passed": ok})
    passed = [c["id"] for c in checks if c["passed"]]
    failed = [c["id"] for c in checks if not c["passed"]]
    return {
        "phase": canon,
        "score": round(len(passed) / len(checks), 4) if checks else 0.0,
        "passed": passed,
        "failed": failed,
        "total": len(checks),
        "checks": checks,
    }


# --- Blind A/B evaluation ---------------------------------------------------------------


def _model_vendor(model: Any, provider: Any = None) -> Optional[str]:
    """Vendor family from a model id and/or provider (provider alone is enough: `openai`)."""
    if isinstance(model, Mapping):
        provider = model.get("provider") or provider
        model = model.get("model")
    if not model and not provider:
        return None
    return infer_model_vendor(str(provider) if provider else None, str(model) if model else None)


def _judge_label(judge_model: Any, judge_provider: Any = None) -> Optional[str]:
    if isinstance(judge_model, Mapping):
        judge_provider = judge_model.get("provider") or judge_provider
        judge_model = judge_model.get("model")
    if judge_provider and judge_model:
        return f"{judge_provider}/{judge_model}"
    return str(judge_model or judge_provider) if (judge_model or judge_provider) else None


def _normalize_winner(value: Any) -> str:
    raw = value.get("winner") if isinstance(value, Mapping) else value
    norm = str(raw or "").strip().lower().replace("candidate", "").strip()
    if norm in ("x", "y"):
        return norm.upper()
    if norm in ("tie", "draw", "none", "neither"):
        return "tie"
    raise ValueError(f"judge_fn returned an invalid winner {raw!r}; expected 'X', 'Y' or 'tie'")


def evaluate_blind_ab(
    candidate_a: Any,
    candidate_b: Any,
    *,
    phase: str,
    seed: Any = None,
    judge_model: Any = None,
    strict_vendor: bool = False,
    judge_fn: Optional[Callable[[dict[str, Any]], Any]] = None,
    record_trail: bool = False,
    tenant: Optional[str] = None,
    repo: Optional[str] = None,
    judge_provider: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Blind, judge, and unblind an A/B comparison of two candidate transcripts.

    A candidate with an Information Barrier leak is disqualified regardless of the judge.
    Without ``judge_fn`` the winner is the higher rubric score (ties broken by fewer
    estimated tokens). ``strict_vendor=True`` fails closed with ``ValueError`` *before*
    ``judge_fn`` runs when any candidate's vendor family (from ``model`` or ``provider``)
    is unknown, or when a judge is in play (``judge_fn`` or a judge model/provider) and
    its family is unknown or matches a candidate's. ``dry_run=True`` never writes trail.db.
    """
    canon = normalize_phase(phase)
    cands = [_normalize_candidate(c, d) for c, d in zip((candidate_a, candidate_b), _DEFAULT_LABELS)]
    if cands[0]["label"] == cands[1]["label"]:
        raise ValueError(f"candidate labels must differ (both are {cands[0]['label']!r})")

    judge_vendor = _model_vendor(judge_model, judge_provider)
    judge_named = bool(judge_model or judge_provider)
    judge_in_play = judge_named or judge_fn is not None
    cand_vendors = [_model_vendor(c["model"], c["provider"]) for c in cands]
    known = sorted({v for v in cand_vendors if v})
    unresolved = sum(1 for v in cand_vendors if not v)
    conflict = bool(judge_vendor and judge_vendor in known)
    if not judge_in_play:
        independent: Optional[bool] = None
    else:
        independent = bool(judge_vendor) and not conflict and not unresolved
    advisories: list[str] = []
    if strict_vendor:
        if unresolved:
            raise ValueError(
                f"strict_vendor: cannot infer the vendor family of {unresolved} candidate(s); "
                "pass each candidate's model or provider"
            )
        if judge_in_play and not judge_vendor:
            what = repr(_judge_label(judge_model, judge_provider)) if judge_named else "a judge_fn with no judge model/provider"
            raise ValueError(f"strict_vendor: cannot infer the vendor family of {what}")
        if conflict:
            raise ValueError(
                f"strict_vendor: judge vendor {judge_vendor!r} matches a candidate vendor; "
                "use a judge from a different vendor family"
            )
    else:
        if conflict:
            advisories.append(f"Judge vendor {judge_vendor!r} matches a candidate vendor; the verdict is not independent.")
        if judge_in_play and unresolved:
            advisories.append(f"{unresolved} candidate vendor(s) unknown; cross-vendor independence is unverified.")
        if judge_named and not judge_vendor:
            advisories.append("Judge vendor family unknown; cross-vendor independence is unverified.")
    if judge_fn is not None and not judge_named:
        advisories.append("judge_fn supplied without judge_model; cross-vendor independence is unverified.")

    blind = _blind(cands, seed)
    order = blind.pop("_order")
    per_slot: dict[str, dict[str, Any]] = {}
    for slot, i in zip(SLOTS, order):
        audit = audit_barrier_leaks(cands[i]["payload"], phase=canon)
        per_slot[slot] = {
            "barrier": audit,
            "rubric": score_rubric_compliance(cands[i]["payload"], phase=canon, audit=audit),
            "bloat": measure_skill_bloat(blind["candidates"][slot]["sanitized_text"]),
        }
    disqualified = [s for s in SLOTS if per_slot[s]["barrier"]["leaked"]]

    packet = {
        **blind_packet(blind),
        "phase": canon,
        "instructions": (
            "Compare Candidate X and Candidate Y for the phase below against the rubric. "
            "Identities are hidden; judge only the content. Reply with winner X, Y, or tie."
        ),
        "rubric": rubric_checks(canon),
    }

    judge_winner: Optional[str] = None
    rationale = None
    if judge_fn is not None:
        verdict = judge_fn(json.loads(json.dumps(packet)))
        judge_winner = _normalize_winner(verdict)
        if isinstance(verdict, Mapping):
            rationale = verdict.get("rationale")

    alive = [s for s in SLOTS if s not in disqualified]
    overridden = False
    if not alive:
        winner, decided_by = "none", "disqualification"
    elif len(alive) == 1:
        winner, decided_by = alive[0], "disqualification"
        overridden = judge_winner is not None and judge_winner != winner
    elif judge_winner is not None:
        winner, decided_by = judge_winner, "judge"
    else:
        sx, sy = per_slot["X"]["rubric"]["score"], per_slot["Y"]["rubric"]["score"]
        tx, ty = per_slot["X"]["bloat"]["estimated_tokens"], per_slot["Y"]["bloat"]["estimated_tokens"]
        if sx != sy:
            winner, decided_by = ("X" if sx > sy else "Y"), "rubric"
        elif tx != ty:
            winner, decided_by = ("X" if tx < ty else "Y"), "bloat_tiebreak"
        else:
            winner, decided_by = "tie", "tie"

    key = blind["key"]
    result: dict[str, Any] = {
        "ok": True,
        "pair_id": blind["pair_id"],
        "seed": blind["seed"],
        "phase": canon,
        "mode": "judge" if judge_fn is not None else "offline",
        "winner": winner,
        "winner_label": key.get(winner),
        "decided_by": decided_by,
        "judge_winner": judge_winner,
        "judge_overridden": overridden,
        "rationale": rationale,
        "disqualified": disqualified,
        "scores": {s: per_slot[s]["rubric"]["score"] for s in SLOTS},
        "key": key,
        "key_digest": blind["sealed_key"]["key_digest"],
        "candidates": per_slot,
        "packet": packet,
        "judge": {
            "model": _judge_label(judge_model, judge_provider),
            "vendor": judge_vendor,
            "candidate_vendors": known,
            "unresolved_candidate_vendors": unresolved,
            "independent": independent,
            "strict_vendor": bool(strict_vendor),
        },
        "advisories": advisories,
        "dry_run": bool(dry_run),
        "trail_decision_id": None,
    }
    if record_trail and not dry_run:
        from .trail import record_transition

        row = record_transition(
            "skill_eval",
            tenant=tenant,
            repo_slug=repo,
            phase="eval",
            actor="eval",
            why=f"Blind A/B ({canon}, {result['mode']}): winner {winner} -> {result['winner_label'] or winner} ({decided_by})",
            evidence={
                "pair_id": result["pair_id"],
                "seed": result["seed"],
                "eval_phase": canon,
                "winner": winner,
                "winner_label": result["winner_label"],
                "verdict": winner,
                "decided_by": decided_by,
                "scores": result["scores"],
                "disqualified": disqualified,
                "key": key,
                "judge_vendor": judge_vendor,
            },
            result="no_winner" if winner == "none" else ("tie" if winner == "tie" else "decided"),
        )
        result["trail_decision_id"] = row.get("id") if row else None
    return result


# --- CLI (shared by `hermes factonio eval` and tools/eval_skills.py) --------------------


def add_eval_arguments(parser: argparse.ArgumentParser, *, json_default: Any = False) -> None:
    """Add the `eval {ab|audit|bloat}` arguments (positional action, no nested subparsers)."""
    parser.add_argument("action", choices=list(EVAL_ACTIONS), help="ab: blind A/B; audit: barrier leaks; bloat: skill size.")
    parser.add_argument("inputs", nargs="+",
                        help="ab: two candidate files; audit: transcript file(s); bloat: SKILL.md path(s) or skill directories.")
    parser.add_argument("--phase", default=None, help=f"Phase ({', '.join(PHASES)}); required for ab and audit.")
    parser.add_argument("--seed", default=None, help="ab: deterministic blinding seed (default: content hash).")
    parser.add_argument("--label-a", default=None, help="ab: label of the first candidate (default: file name).")
    parser.add_argument("--label-b", default=None, help="ab: label of the second candidate (default: file name).")
    parser.add_argument("--model-a", default=None, help="ab: model that produced the first candidate (vendor check).")
    parser.add_argument("--model-b", default=None, help="ab: model that produced the second candidate (vendor check).")
    parser.add_argument("--provider-a", default=None, help="ab: provider of the first candidate (vendor check).")
    parser.add_argument("--provider-b", default=None, help="ab: provider of the second candidate (vendor check).")
    parser.add_argument("--judge-model", default=None, help="ab: judge model checked for cross-vendor independence.")
    parser.add_argument("--judge-provider", default=None, help="ab: judge provider (vendor check when the model is ambiguous).")
    parser.add_argument("--strict-vendor", action="store_true",
                        help="ab: fail closed when any candidate or judge vendor is unknown or the judge shares a candidate vendor.")
    parser.add_argument("--record", "--record-trail", dest="record", action="store_true",
                        help="ab: log a skill_eval decision (phase=eval) to trail.db.")
    parser.add_argument("--dry-run", action="store_true",
                        help="ab: evaluate and print only; never write trail.db (overrides --record).")
    parser.add_argument("--baseline", default=None, help="bloat: baseline SKILL.md path for a size delta.")
    parser.add_argument("--soft-tokens", type=int, default=DEFAULT_SOFT_TOKENS, help="bloat: warn above this many tokens.")
    parser.add_argument("--hard-tokens", type=int, default=DEFAULT_HARD_TOKENS, help="bloat: over_budget above this many tokens.")
    parser.add_argument("--fail-on-over-budget", "--strict", dest="fail_on_over_budget", action="store_true",
                        help="bloat: exit 1 when any skill is over_budget (default: report only, exit 0).")
    parser.add_argument("--json", action="store_true", default=json_default, help="Output machine-readable JSON.")


def _load_input(path_str: str) -> Any:
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise ValueError(f"input file not found: {path_str}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".json", ".jsonl"):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _cli_candidate(path_str: str, label: Optional[str], model: Optional[str], provider: Optional[str] = None) -> dict[str, Any]:
    loaded = _load_input(path_str)
    cand: dict[str, Any]
    if isinstance(loaded, Mapping) and any(k in loaded for k in _PAYLOAD_KEYS):
        cand = dict(loaded)
    else:
        cand = {"transcript": loaded}
    if label:
        cand["label"] = label
    elif not any(
        isinstance(cand.get(k), (str, int)) and str(cand.get(k)).strip()
        for k in _LABEL_KEYS
        if k not in _WEAK_IDENTITY_KEYS or not _looks_like_event(cand)
    ):
        cand["label"] = Path(path_str).name
    if model:
        cand["model"] = model
    if provider:
        cand["provider"] = provider
    return cand


def discover_skill_files(path: Any) -> list[Path]:
    """Expand a bloat input: a file is itself; a directory yields its `SKILL.md` files
    (recursively, sorted), or every `*.md` under it when it holds no `SKILL.md`."""
    root = Path(path).expanduser()
    if not root.is_dir():
        return [root]
    found = sorted(p for p in root.rglob("SKILL.md") if p.is_file())
    if not found:
        found = sorted(p for p in root.rglob("*.md") if p.is_file())
    if not found:
        raise ValueError(f"no SKILL.md or *.md files under directory: {path}")
    return found


def _run(args: Any) -> tuple[dict[str, Any], int]:
    action = args.action
    if action != "bloat" and getattr(args, "fail_on_over_budget", False):
        raise ValueError("--fail-on-over-budget/--strict applies only to bloat (use --strict-vendor for ab)")
    if action == "ab":
        if len(args.inputs) != 2:
            raise ValueError("ab needs exactly two candidate files")
        if not args.phase:
            raise ValueError("ab needs --phase")
        res = evaluate_blind_ab(
            _cli_candidate(args.inputs[0], args.label_a, args.model_a, getattr(args, "provider_a", None)),
            _cli_candidate(args.inputs[1], args.label_b, args.model_b, getattr(args, "provider_b", None)),
            phase=args.phase,
            seed=args.seed,
            judge_model=args.judge_model,
            judge_provider=getattr(args, "judge_provider", None),
            strict_vendor=bool(args.strict_vendor),
            record_trail=bool(args.record),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        return res, 0
    if action == "audit":
        if not args.phase:
            raise ValueError("audit needs --phase")
        results = []
        for p in args.inputs:
            audit = audit_barrier_leaks(_load_input(p), phase=args.phase)
            results.append({"input": p, **audit})
        clean = all(r["clean"] for r in results)
        return {"ok": True, "action": "audit", "clean": clean, "results": results}, 0 if clean else 1
    results = []
    for p in args.inputs:
        if not Path(p).expanduser().exists():
            raise ValueError(f"input file not found: {p}")
        for skill_file in discover_skill_files(p):
            results.append(
                measure_skill_bloat(skill_file, baseline=Path(args.baseline) if args.baseline else None,
                                    soft_tokens=args.soft_tokens, hard_tokens=args.hard_tokens)
            )
    over = any(r["status"] == "over_budget" for r in results)
    gate = bool(getattr(args, "fail_on_over_budget", False))
    return ({"ok": True, "action": "bloat", "over_budget": over, "fail_on_over_budget": gate, "results": results},
            1 if over and gate else 0)


def _render(res: Mapping[str, Any]) -> str:
    action = res.get("action")
    if action == "audit":
        lines = []
        for r in res["results"]:
            lines.append(f"{'CLEAN' if r['clean'] else 'LEAK'}: {r['input']} ({r['phase']}, "
                         f"{r['inspected_actions']} actions, {r['ignored_mentions']} quoted mentions ignored)")
            for f in r["findings"]:
                lines.append(f"  [{f['rule']}] {f['evidence']}")
        return "\n".join(lines)
    if action == "bloat":
        lines = []
        for r in res["results"]:
            line = (f"{r['status'].upper()}: {r['source']} ~{r['estimated_tokens']} tokens "
                    f"({r['chars']} chars, {r['lines']} lines, {r['words']} words, {r['headings']} headings; "
                    f"soft {r['soft_tokens']} / hard {r['hard_tokens']})")
            if r.get("delta"):
                line += f" delta {r['delta']['estimated_tokens']:+d} tokens"
            lines.append(line)
        return "\n".join(lines)
    lines = [
        f"Blind A/B {res['pair_id']} ({res['phase']}, {res['mode']}, seed={res['seed']}): "
        f"winner {res['winner']} -> {res['winner_label'] or '-'} ({res['decided_by']})",
        f"  Scores: X={res['scores']['X']} Y={res['scores']['Y']}"
        + (f" | disqualified: {', '.join(res['disqualified'])}" if res["disqualified"] else ""),
        f"  Key: X={res['key']['X']} Y={res['key']['Y']}",
    ]
    lines.extend(f"  Advisory: {a}" for a in res.get("advisories") or [])
    if res.get("dry_run"):
        lines.append("  Dry run: trail.db not written.")
    return "\n".join(lines)


def run_eval_cli(args: Any) -> int:
    """Run `eval {ab|audit|bloat}`, print the result, and return the process exit code."""
    as_json = bool(getattr(args, "json", False))
    try:
        res, code = _run(args)
    except (ValueError, OSError) as exc:
        if as_json:
            print(json.dumps({"ok": False, "action": args.action, "error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"action": args.action, **res}, indent=2, default=str))
    else:
        print(_render({"action": args.action, **res}))
    return code
