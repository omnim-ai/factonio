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

## Core Principles

- **Data shape first (model the domain):** Decide the core types, records, and state transitions before naming functions or files. A correct data model makes most of the code obvious; a wrong one leaks into every unit.
- **Boundary discipline:** Each unit owns its state and talks to others through a narrow, explicit interface (function signature, schema, wire format). Validate at the boundary; trust inside it.
- **Smallest change, deletion bias:** Prefer the smallest change that meets the acceptance criteria. Removing or reusing code beats adding a parallel path; new abstractions need a second concrete caller.

## Procedure

### 1. Ground in the Live Repository

1. Inspect the repository structure, `README.md`, `AGENTS.md` (if present), package manifests, test runner commands, and CI workflows under `.github/workflows/`.
2. For `mode: bug`, diagnose before writing anything:
   1. Reproduce the failure with an actual command or test trace. Save the smallest failing repro.
   2. **Binary-search hypothesis elimination:** list candidate causes, then halve the search space with each probe (bisect inputs, code paths, config, or commits with `git bisect`) until one hypothesis survives. Record each eliminated hypothesis and the probe that killed it.
   3. **Check git/PR history for intent:** run `git log -L`, `git log -S`, or `git blame` on the failing lines and read the PR that introduced them (`gh pr view`). Decide whether the behavior is a regression or a deliberate choice before proposing a fix.
   4. Isolate the exact root cause (file, symbol, and failing condition).
   5. **Failing repro first:** when cheap, order the Task DAG so the failing regression test lands before (or in the same commit ahead of) the fix, proving the test fails on the base.
3. **Empirical prototype rule:** when a fork in the design depends on a fact you can measure (library behavior, API response shape, performance, CLI output), settle it with a quick runnable probe in your scratch workspace instead of guessing or blocking on the user. Record the probe command and result in `spec.md`.
4. If a material product decision cannot be resolved from the request, codebase, or a probe, call `kanban_block(kind="needs_input", reason=...)` with one focused question and a recommended option.

### 1b. Architect Before Locking the Approach

Before writing the Technical approach, sketch **2 candidate shapes**. For each, note the core types, which component owns each piece of state, and the module/API boundaries it introduces or touches. Pick one against the Core Principles and record the rejected shape in one line (why it lost) so implementers do not rediscover it.

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

**Throughput Checkpoint** — before emitting the Task DAG, answer all four:

1. **Blocking first steps:** Which unit must land first because others build on it (shared types, schema, migration)? Make only those true dependencies.
2. **Independent disjoint workstreams:** Which units touch disjoint files and can run in parallel worktrees without merge conflicts?
3. **Shared mutable state:** Where do units touch the same file, schema, config, or global? Separate that state behind an explicit interface (or extract it into its own blocking unit) before deciding to serialize.
4. **Smallest safe decomposition:** Is each unit the smallest independently verifiable, mergeable PR? Merge units that cannot be verified alone; split units that mix unrelated concerns.

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
  "has_ui": false,
  "holdout_count": 3,
  "units": [
    {"slug": "unit-1", "title": "...", "depends_on": []}
  ],
  "run_hint": "<command to build/run for black-box verification>"
}
```

Set `"has_ui": true` whenever the change introduces or visibly alters user-facing UI, layouts, styling, or interactive views so the orchestrator can dispatch Phase 1b (Design) before UI implementation.

## Error Handling

- **Unreproducible bug:** Record exact commands tried, observed output, and hypotheses in `spec.md`, then block with `needs_input` asking for reproduction steps or logs.
- **Repository has no test harness:** Document how Black-Box Check can verify the change directly (CLI invocation, script, or smoke server) under Verification commands.
