---
name: define
description: "Use inside a Factonio Definition Kanban card to explore a repository, write spec.md with acceptance criteria and a parallel task DAG, and author hidden holdout verification scenarios. Do not use for writing production code or opening pull requests."
license: MIT
---

# Factonio Definition Phase

Produce a codebase-grounded specification, a parallelizable task breakdown, and hidden black-box verification checks before any implementation code is written.

## When to Use

- Preloaded on `[define]` Kanban cards running under the `factonio` worker profile.
- Handles both feature design (`mode: feature`) and bug root-cause diagnosis (`mode: bug`).
- Do not use to implement features, commit code, or open pull requests.

## Inputs

Read from the Kanban card body:
- Target repository path and `owner/repo`
- Mode (`feature` or `bug`)
- Output path for `spec.md`
- Output path for the hidden `holdout/` directory

## Procedure

### 1. Ground in the Live Repository

1. Inspect the repository structure, `README.md`, `AGENTS.md` (if present), package manifests, test runner commands, and CI workflows under `.github/workflows/`.
2. For `mode: bug`, reproduce the failure with an actual command or test trace and isolate the exact root cause (file, symbol, and failing condition).
3. If a material product decision cannot be resolved from the request or codebase, call `kanban_block(kind="needs_input", reason=...)` with one focused question and a recommended option.

### 2. Write `spec.md`

Write a concise, concrete Markdown file at the exact `spec.md` path named in the card body using this structure:

```markdown
# <Title>

## Goals and acceptance criteria
- [ ] <Observable, testable outcome 1>
- [ ] <Observable, testable outcome 2>

## Non-goals
- <Explicit exclusion>

## Technical approach
<Key files, interfaces, data flow, and constraints grounded in verified repo paths>

## Verification commands
- Build / typecheck: `<command>`
- Unit / integration tests: `<command>`
- Runnable target (CLI/server/preview): `<command or URL>`

## Task DAG
1. `<unit-slug>`: <scoped deliverable> (depends on: none)
2. `<unit-slug>`: <scoped deliverable> (depends on: none)
3. `<unit-slug>`: <integration deliverable> (depends on: 1, 2)
```

Keep independent units parallel (`depends on: none`). Never mention or leak `holdout/` scenarios inside `spec.md`.

### 3. Author Hidden Holdout Checks

Write 2 to 5 concrete black-box verification scenarios under the `holdout/` directory (for example `holdout/01-happy-path.md`, `holdout/02-edge-case.md`). Each file must contain:
- **Setup:** exact precondition or input payload.
- **Action:** exact user-visible command, HTTP request, or UI interaction.
- **Expected result:** observable stdout, exit code, HTTP status/body, or rendered behavior.
- **Failure signal:** what indicates a regression or broken contract.

Focus holdout scenarios on edge cases, failure modes, and integration seams that unit tests often miss.

### 4. Complete the Card

Call `kanban_complete` with a 2-sentence `summary` and structured `metadata`:

```json
{
  "phase": "define",
  "spec_path": "<absolute path to spec.md>",
  "holdout_count": 3,
  "units": [
    {"slug": "unit-1", "title": "...", "depends_on": []}
  ],
  "run_hint": "<command to build/run for black-box verification>"
}
```

## Error Handling

- **Unreproducible bug:** Record exact commands tried, observed output, and hypotheses in `spec.md`, then block with `needs_input` asking for reproduction steps or logs.
- **Repository has no test harness:** Document how Black-Box Check can verify the change directly (CLI invocation, script, or smoke server) under Verification commands.
