#!/usr/bin/env python3
"""Static repository integrity checks for Factonio.

Validates:
- plugin.yaml manifest structure, required keys, and declared skills/tools
- dashboard/manifest.json structure
- All bundled skills/*/SKILL.md YAML frontmatter
- Local Markdown links and image paths in README.md, CONTRIBUTING.md, SECURITY.md, AGENTS.md, and docs/*.md
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_SKILLS = (
    "orchestrator",
    "define",
    "design",
    "implement",
    "verify-diff",
    "verify-blackbox",
)

EXPECTED_CONTRIBUTOR_SKILLS = (
    "update-readme-media",
)

MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HTML_SRC_RE = re.compile(r"""(?:src|srcset|href)=["']([^"']+)["']""")


def check_plugin_manifest(errors: list[str]) -> None:
    path = REPO_ROOT / "plugin.yaml"
    if not path.is_file():
        errors.append("Missing plugin.yaml")
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        errors.append("plugin.yaml did not parse as a YAML mapping")
        return
    for key in ("name", "label", "version", "manifest_version", "description", "license", "provides_tools"):
        if not data.get(key):
            errors.append(f"plugin.yaml missing required field: {key}")
    if data.get("name") != "factonio":
        errors.append(f"plugin.yaml name must be 'factonio', got {data.get('name')!r}")


def check_dashboard_manifest(errors: list[str]) -> None:
    path = REPO_ROOT / "dashboard" / "manifest.json"
    if not path.is_file():
        errors.append("Missing dashboard/manifest.json")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        errors.append("dashboard/manifest.json must be a JSON object")
        return
    for key in ("name", "version"):
        if not data.get(key):
            errors.append(f"dashboard/manifest.json missing required field: {key}")
    api_path = REPO_ROOT / "dashboard" / "plugin_api.py"
    if not api_path.is_file():
        errors.append("Missing dashboard/plugin_api.py")


def _check_skill_file(skill_md: Path, expected_name: str, errors: list[str]) -> None:
    if not skill_md.is_file():
        errors.append(f"Missing skill file: {skill_md.relative_to(REPO_ROOT)}")
        return
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        errors.append(f"{skill_md.relative_to(REPO_ROOT)} missing YAML frontmatter")
        return
    parts = text.split("---", 2)
    if len(parts) < 3:
        errors.append(f"{skill_md.relative_to(REPO_ROOT)} has unterminated YAML frontmatter")
        return
    fm = yaml.safe_load(parts[1]) or {}
    if not isinstance(fm, dict):
        errors.append(f"{skill_md.relative_to(REPO_ROOT)} frontmatter is not a mapping")
        return
    if fm.get("name") != expected_name:
        errors.append(f"{skill_md.relative_to(REPO_ROOT)} frontmatter name={fm.get('name')!r} != {expected_name!r}")
    if not str(fm.get("description") or "").strip():
        errors.append(f"{skill_md.relative_to(REPO_ROOT)} frontmatter missing description")


def check_skills(errors: list[str]) -> None:
    for skill in EXPECTED_SKILLS:
        _check_skill_file(REPO_ROOT / "skills" / skill / "SKILL.md", skill, errors)
    for skill in EXPECTED_CONTRIBUTOR_SKILLS:
        _check_skill_file(REPO_ROOT / ".agents" / "skills" / skill / "SKILL.md", skill, errors)


def _check_target(md_file: Path, raw_target: str, errors: list[str]) -> None:
    target = raw_target.strip().split()[0]
    if not target or target.startswith(("#", "http://", "https://", "mailto:", "data:")):
        return
    rel_part = target.split("#", 1)[0].split("?", 1)[0]
    if not rel_part:
        return
    resolved = (md_file.parent / rel_part).resolve()
    if not resolved.exists():
        errors.append(f"Broken local link in {md_file.relative_to(REPO_ROOT)}: {raw_target!r}")


def check_markdown_links(errors: list[str]) -> None:
    md_files = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "SECURITY.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "CHANGELOG.md",
        *sorted((REPO_ROOT / "docs").glob("*.md")),
        *sorted((REPO_ROOT / ".agents" / "skills").glob("*/SKILL.md")),
    ]
    for md_file in md_files:
        if not md_file.is_file():
            continue
        text = md_file.read_text(encoding="utf-8")
        # Strip fenced code blocks so example snippets are not checked as links
        stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for match in MD_LINK_RE.finditer(stripped):
            _check_target(md_file, match.group(1), errors)
        for match in HTML_SRC_RE.finditer(stripped):
            _check_target(md_file, match.group(1), errors)


def main() -> int:
    errors: list[str] = []
    check_plugin_manifest(errors)
    check_dashboard_manifest(errors)
    check_skills(errors)
    check_markdown_links(errors)

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print("OK: plugin manifest, dashboard manifest, 6 skills, and all local documentation links verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
