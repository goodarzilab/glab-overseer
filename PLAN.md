# glab-overseer: build plan

Status: draft for owner approval. Dated 2026-11-01.

## What is being built

One skill, `glab-overseer`, that a Goodarzi Lab member loads in Claude Code or
Antigravity to take a repository from an intention to planned, dispatched,
reviewed and merged work. It uses only what every lab member has: a Claude Code
subscription and an Antigravity subscription, invoked as the `claude` and `agy`
command-line tools. It uses no LLM API keys and no agent daemon. It runs on the
lab's Slurm cluster and on laptops.

The host session (Claude Code or Antigravity) is the orchestrator. It holds the
judgment. A small stdlib Python supervisor, `overseer.py`, holds the state: it
enforces the approved boundary, launches workers, records evidence, and refuses
any closure the evidence does not support. Every fact the orchestrator acts on
lives on disk, so any new session resumes from `overseer.py status`.

`hanig/brood-lord` is the reference design. This is a rewrite, not a fork.

## Architecture

### Layout

```text
skills/glab-overseer/SKILL.md           front door, operating loop, authority bounds
skills/glab-overseer/references/*.md    plan format, operating loop, backends, review, install
skills/glab-overseer/scripts/overseer.py         entry point
skills/glab-overseer/scripts/overseer/*.py       stdlib modules (below)
install.sh                              install into Claude Code and Antigravity skill roots
tests/                                  unittest; external CLIs replaced by PATH shims
.github/workflows/ci.yml                unittest on Python 3.9 and 3.12
```

Modules: `cli`, `paths`, `store`, `plan` (U1); `survey`, `env`, `models`,
`adapters` (U2); `execution` (U3); `slurm` (U4); `workspace`, `judge` (U5);
`review` (U6); `github` (U7); `controller` (U8); `report` (U9).

### State

All state lives under `<project>/.overseer/`, which the supervisor creates with
its own `.gitignore` containing `*`, so it never dirties a repository's tracked
ignore rules. Writes are atomic (temporary file plus `os.replace`). Every
state-mutating command holds one project mutation lock; there is one
orchestrator per project.

```text
.overseer/
  lock                          mutation lock (holder, host, pid, time)
  survey.json  models.json      runtime survey and dated model resolution
  events.jsonl                  append-only action log
  plans/<plan_id>/
    plan.json                   normalized approved semantic plan (immutable)
    approval.json               approver, time, digest of plan + issue drafts + boundary
    issues.json                 drafts and filed issue numbers (operational)
    units/<unit>/unit.json      operational facets: authorization, execution,
                                evidence, closure, tracker; lineage; counters
    units/<unit>/attempts/a<N>/ exclusive attempt root, never reused
      inv/<seq>-<kind>/         one directory per invocation (author, fixer, job):
                                intent.json launch.json start.json exit.json, logs
      worktree/ (code)  outputs/ (job)  scratch/
      judgment.json
      reviews/r<R>/             input, invocation, raw output, review, dispositions
```

### Plan format

A plan names one GitHub repository and target branch, an approved write scope
(repository-relative globs), caps, optional Slurm defaults, and units. A unit is
`code` or `job`.

- `code`: `id`, `title`, `prompt`, `claims`, `test_command`, `outputs`
  (repository-relative predicates), `scope`, `needs`.
- `job`: `id`, `title`, `command`, `outputs` (relative to the attempt's
  `outputs/` directory, exported as `$OVERSEER_OUT`), `needs`, optional
  Slurm resources.

An output predicate is a required regular file plus optional `min_bytes`,
`sha256`, and an approved validator command. Absolute paths, `..`, and symlinks
that escape the root are refused.

### Commands

```text
survey [--probe-auth]  probe-transport  models --host claude|google [--refresh]
plan validate|approve|show|split  unlock
issues file
slurm-handshake  dispatch  status  wait  abort  resolve
judge  review  reproduce  dispositions  fix  nonvacuous
pr  merge  next
report [--public]
```

Plan and unit ids match `^[a-z0-9][a-z0-9_-]{0,63}$` because they become path
components.

### Workers and launchers

Workers are headless invocations: `claude -p --permission-mode auto --model M
--output-format json` and `agy -p --model M --dangerously-skip-permissions
--sandbox --output-format json`. Both run in an environment built from scratch
that excludes LLM API keys, model-routing variables and GitHub tokens. On a
laptop the launcher detaches them with a new session; on the cluster it submits
them with `sbatch` to the batch CPU partition named in the runtime
configuration. A shell wrapper records `start.json` before the payload and
`exit.json` after it, keyed by the attempt token and command digest.

Workers edit and test. They never push. The supervisor commits whatever
eligible changes remain, pushes, opens pull requests and merges.

### Model roles

| Role | Claude family | Google family |
|---|---|---|
| plan, orchestrate | `opus` | newest argon-tier at high |
| write code | `sonnet` | newest argon-tier at high |
| review | `haiku` | newest flash |

The family that writes code is the orchestrator's host family. Review uses the
other family. Google names are resolved from `agy models` by tier pattern and
numeric version, cached with a date, refreshed by `models --refresh`. Claude
models that `agy` also lists never count as Google-family.

### Judgment

A code attempt is DONE when the worker has terminated, the branch and ancestry
are intact, every commit that would be pushed stays inside scope and contains no
prohibited artifact, the declared outputs pass, and the declared test command
passes against the sealed candidate head.

A job attempt is DONE when terminal evidence shows success and every declared
output passes. Terminal evidence is gathered from `squeue`, `scontrol show job`,
`sacct` (5 s timeout) and the wrapper's `exit.json`. Known failure evidence
always wins. Scheduler logs can mark FAILED, never DONE. Without terminal
evidence the attempt is UNCERTAIN.

## Decision ledger

### Owner decisions

| ID | Decision |
|---|---|
| O1 | Rewrite from scratch; brood-lord is reference only. |
| O2 | Tracker is GitHub Issues through `gh`. |
| O3 | One plan approval. Afterwards the orchestrator dispatches, retries, splits units within scope, adjudicates findings, pushes, opens and merges PRs to the target branch. It stops and asks before adding scope, writing to shared paths, destructive git (force push, history rewrite, deleting the target branch), or exceeding caps. |
| O4 | Claude: opus plans and orchestrates, sonnet writes code, haiku reviews. Google: newest argon-tier at high plans and writes code, newest flash reviews. The host family writes; the other family reviews. On confirmed quota or auth failure, fall back to the same family's small model in a fresh session and label it. Google names resolved dynamically. |
| O5 | Runs on the cluster and on laptops; the owner tests laptops. |
| O6 | Public repository, MIT. No cluster names, accounts, paths or credentials committed. |
| O7 | Workers never push; Claude workers use `--permission-mode auto`; Antigravity workers use `--dangerously-skip-permissions --sandbox`. |
| O8 | v1 acceptance: one end-to-end run on a throwaway goodarzilab repository from Claude Code on the cluster (one approval, issues filed, two parallel code units, cross-family review, supervisor merge, issues closed by merged PRs, evidence report, one batch job unit), repeated with Slurm hidden and with Antigravity as orchestrator. |
| O9 | `sacct` is unreliable on the cluster; a backup judgment path is required. |
| O10 | Unit kinds are `code` and `job`. |
| O11 | Laptop workers run as local detached processes; cluster workers run as batch jobs. |
| O12 | Caps: 3 concurrent workers, 2 attempts per unit, 3 review rounds per PR, then the orchestrator decides alone. |
| O13 | Report at each merged PR and immediately when the owner is needed. No findings to publish. |
| O14 | One skill named `glab-overseer` in `goodarzilab/glab-overseer`. |
| D1 | The worker cap counts background executions only (authors, fixers, job units, including queued and uncertain). Reviews and tests run one at a time outside the cap. |
| D2 | After merging an advanced target, always retest. Reuse the prior review only when `git patch-id` shows the unit's own change is identical; otherwise run a new round within the cap. |
| D3 | Acceptance proves the `sacct` fallback live. If `sacct` never answers during acceptance, the `sacct`-judged item is reported incomplete. |
| D4 | A job with no terminal evidence holds its slot as UNCERTAIN. The orchestrator inspects the outputs itself and, with sufficient conviction, marks it done; otherwise it releases it as failed. Both go through `resolve` with a recorded reason and are labeled orchestrator-resolved. |
| D5 | Ten build units; the controller is its own unit. |
| D6 | Cache files are never deleted. Untracked files on the exclusion list (`__pycache__`, `*.pyc`, `.pytest_cache`) are not staged; tracked files are never touched. |
| D7 | Parallel units declaring the same output path get a validation warning, not a rejection. |
| B1 | This build runs in one Claude Code session, not through brood-lord's swarm. |
| B2 | This build is tracked as GitHub Issues on `goodarzilab/glab-overseer`. |
| B3 | The design committee uses hanig-project's specification and its OpenAI and OpenRouter seats. |

### Measured facts that set requirements

| ID | Observation (2026-11-01) |
|---|---|
| P1 | `claude -p --permission-mode auto` in a never-trusted repository created a file and committed it; output is a JSON event array ending in a `result` event. |
| P2 | With `ANTHROPIC_API_KEY` set, `claude` billed the key (`apiKeySource: ANTHROPIC_API_KEY`); with it unset, `apiKeySource: none`. |
| P3 | `claude --bg` refused a never-trusted workspace. |
| P4 | `agy -p --output-format json` returns one object with `status: SUCCESS`, `response`, `conversation_id`. |
| P5 | `agy --sandbox` mounts `.git` read-only; the worker's commit failed. |
| P6 | Sandboxed `agy` in a linked worktree fixed a bug and passed tests, leaving an uncommitted diff. |
| P7 | `claude -p` and `agy -p` both authenticated by subscription inside a batch job; a shell trap wrote `exit.json`. |
| P8 | A batch job whose output path was under `/tmp` reported COMPLETED with exit 0:0 and produced nothing visible: the job had a private `/tmp`. |
| P9 | `sacct` timed out to the accounting database while `scontrol show job` answered. |
| P10 | `agy models` lists Claude models alongside Gemini models and listed no carbon tier. |
| P11 | Both `claude --help` and `agy --help` list a `--json-schema` option for print mode; its behavior is not yet measured. |

### Committee decisions

Planning committee `glab-overseer-design` (astra, gemini-argon), one design
round and one cross-critique round. Synthesis returned OWNER because this build
has no mandate file; the owner settled the remaining splits as D1 to D7.

| ID | Decision |
|---|---|
| C1 | The supervisor owns commits. Valid worker commits are accepted without rewriting; remaining eligible changes get one supervisor commit. |
| C2 | Integration merges the target into the unit branch; never rebase or force push. |
| C3 | Each unit tracks authorization, execution, evidence, closure and tracker facets separately. |
| C4 | Persist an operation intent before every external side effect. An ambiguous outcome is reconciled or held, never repeated blindly. |
| C5 | Before batch execution, a nonce round trip proves the controller and the batch job see the same attempt root. The wrapper verifies its attempt identity before running the payload. |
| C6 | Job terminal evidence comes from `squeue`, `scontrol`, `sacct` and the wrapper receipt; failure evidence beats success; logs never establish DONE. |
| C7 | `wait` polls in Python; `abort` distinguishes cancellation requested from termination confirmed. |
| C8 | Attempts and review rounds are counted separately; fixes run in the same worktree. |
| C9 | No per-unit model overrides. |
| C10 | The failure classifier separates confirmed quota, confirmed auth, transient service, configuration and unknown. Only confirmed quota or auth permits reviewer fallback. Authors never switch family. |
| C11 | Reviews and dispositions bind to the exact candidate head. Acted-on findings carry reproduction evidence; reproduction runs in disposable space; inconclusive is a valid record. |
| C12 | Raw evidence stays private; the public report is allowlisted Markdown plus JSON. |
| C13 | One approval binds the plan, the issue drafts and the boundary. Operational state is stored separately. Splits keep lineage and never reset counters. |
| C14 | One mutation lock per project. |
| C15 | Review input travels by stdin or a referenced file, never truncated in argv; the transport is probed. |
| C16 | Output predicates: required file, minimum size, optional digest, optional approved validator. Job outputs live under the attempt root. |
| C17 | Before merge, record original base, integration base, judged head, remote PR head and merge commit separately. |
| C18 | Tracked files are scanned for private identifiers supplied at test time and for credential patterns. |
| C19 | Never run an agent on a login node. Finite walltime; `--mem` or `--mem-per-cpu`, never both. |
| C20 | The worker boundary is cooperative. Same-user isolation is documented as a limit, not claimed as containment. |
| C21 | Worker environments exclude LLM keys, routing variables and GitHub tokens. |
| C22 | Non-vacuity: in a scratch checkout, revert the production change, keep the tests, and require a failure. |

### Open disagreements

- `--signal=B:USR1@30` on batch jobs. Argon wanted it as a correctness
  dependency; Astra wanted it optional. Plan default: optional diagnostic,
  never a source of terminal evidence.
- The hygiene scanner. Argon wanted the cluster name written into the scanner;
  Astra wanted private identifiers supplied at test time. Plan default: Astra's,
  because a committed cluster name violates O6.

### Plan review

`review.py --kind plan` ran once on this file and the unit contracts with the
`plan` panel (luna, kimi-k3); authors excluded: the orchestrator, astra and
gemini-argon. Both seats returned `refuted`. The review is advisory input, not a
gate. Dispositions, all by the orchestrator under O3:

| Finding | Disposition |
|---|---|
| luna:1 unit and plan ids can escape `.overseer/` | Fixed: ids must match a safe pattern (U1). |
| luna:2 a split can drop a parent output | Fixed: children must cover every parent output (U1). |
| luna:3 fixer records collide with the author's | Fixed: one `inv/<seq>-<kind>/` directory per invocation (U3, layout). |
| luna:4 `inconclusive` on a blocking finding passes the gate | Fixed: blocking findings need `dismissed`, `nonblocking` with evidence, or a reviewed fix (U6). |
| luna:5 Slurm chosen with no resources declared | Fixed: dispatch refuses and names the missing values (U4). |
| luna:6 empty diff fails a unit already satisfied at base | Kept as designed. kimi-k3 refuted it: a no-op author is indistinguishable from a broken one, and GitHub refuses an empty PR. A misplanned unit goes to the owner. |
| luna:7, kimi-k3:6 home path in the build's plan.json | Already handled: `.swarm/` is git-ignored and holds host-specific planning state; nothing in it is committed. |
| kimi-k3:1 U7 uses U2's `supervisor_env` | Fixed: U7 needs U2 and U5 (U5 owns the judgment record `pr` reads). |
| kimi-k3:2 bare `sk-` pattern matches `.overseer-task.md` | Fixed: credential patterns are token-shaped (U9). |
| kimi-k3:3 `--json-schema` unmeasured | Fixed: P11 records the flag in both `--help` outputs; U2 probes its behavior; local schema validation is authoritative and the flag is used only when the probe passed. |
| kimi-k3:4 commands list incomplete | Fixed: the list above now names every subcommand (refuted by luna as non-contradictory; fixed anyway). |
| kimi-k3:5 who runs tests | Clarified below: workers run tests and report; the orchestrator re-runs them as the evidence (refuted by luna; clarified anyway). |
| kimi-k3:new:1 non-vacuity not wired into any gate | Fixed: `merge` requires a PASS or NOT_APPLICABLE non-vacuity record for the candidate, or a FAIL overruled at the review cap (U6, U8). |

kimi-k3 also noted that a split child inherits an exhausted parent's counters
and so may never run. That is intended: a split must not reset caps (C13); an
exhausted unit goes to the owner.

## Build units

Each unit is one pull request to `main`, under 100,000 characters of review
input including tests and docs. A Sonnet 5.5 subagent writes each unit in its
own worktree and runs the tests; its report is a submission. The Opus 5.5
orchestrator re-runs the tests as the evidence, runs the size check and
hanig-review-gate's implementation review, rules on findings, then pushes and
merges. `.swarm/plan.json` (git-ignored) carries the unit contracts in the
schema hanig-project validates; it is not dispatched through brood-lord's swarm.

| Unit | Title | Needs |
|---|---|---|
| U1 | Core: state, authorization and plan contracts | none |
| U2 | Survey, subscription environment, model roles and CLI adapters | U1 |
| U3 | Execution core and local backend | U2 |
| U4 | Slurm backend and shared-storage verification | U3 |
| U5 | Worktrees, candidate normalization and judgment | U4 |
| U6 | Review, reproduction and adjudication | U5 |
| U7 | GitHub issues and pull-request transport | U2, U5 |
| U8 | Controller, integration and closure | U6, U7 |
| U9 | Skill, installer and reports | U8 |
| U10 | End-to-end acceptance on a throwaway repository | U9 |

U10 creates a throwaway repository under `goodarzilab` for the acceptance runs.
That creation is covered by O8.

## Out of scope

- Any LLM API client, Paseo, or resident daemon.
- Linear or any tracker other than GitHub Issues.
- A separate `pipeline` kind; Nextflow or Snakemake run as `job` commands.
- Containment of a hostile worker; workers are cooperative (C20).
- Windows.
- Estimating remaining subscription quota.
- HTML reports.
- Installing into agents other than Claude Code and Antigravity.
- Deliberately exhausting a subscription to capture quota errors.
