# glab-overseer v1 acceptance report

Date: <date>. Commit under test: <sha>. Hosts: <claude version>, <agy version>. Repositories: <throwaway names>.

Fill every cell from `check_run.py --json` (claims) and `report --public` (`.overseer/report/public/report.md`). Copy statuses as printed; never upgrade one. A run is complete only when no claim is INCOMPLETE and its public report lists no incomplete or uncertain item.

## Verdict

<One sentence: which of O8's three runs passed, which are partial or incomplete.>

## Claims by run

One table per run: Run 1 (Claude Code, Slurm), D3 fallback, Run 2 (Slurm hidden), Run 3 (Antigravity), Recovery, Allocation loss.

| Claim | Status | Evidence path | Detail |
|---|---|---|---|
| overlap | PASS, FAIL or INCOMPLETE | `.overseer/plans/<plan>/units/<u>/attempts/a1/inv/` | <windows> |
| <unit>.judgment, .review, .nonvacuity, .merge, .tracker | | | |
| <unit>.integration | | | |
| <job>.judgment, .sources, .sacct, .tracker, .resolved | | | |
| backend, no_slurm | | | |
| recovery.<unit> | | | |
| orchestrator_items | | | <orchestrator-resolved items> |

## Run facts

Per run: backend, models used (author and reviewer, with family), attempts and review rounds, pull request and issue numbers, whether a reviewer fallback occurred, who drove each step (the orchestrator session or the operator), and the `report --public` path.

## Orchestrator-resolved and simulated items

Every `resolve`, orchestrator decision and simulated condition (moved receipt, hidden tools, killed process), each with its reason.

## Pending

The owner laptop test (O5): pending, not run by the orchestrator.

## Limits

- Workers run as the same user; the boundary is cooperative, not containment (C20).
- The D3 lost-evidence case is simulated; the natural accounting outage was <observed or not observed> (P9).
- The `merge` kill is simulated with a `gh` shim, not an organic crash.
- Allocation loss has no `check_run` claim; its evidence is the files listed in the runbook.
- Quota and auth fallback were not exercised (no deliberate exhaustion).
- <Any INCOMPLETE claim and why.>
