# glab-overseer v1 acceptance report

Date: 2026-11-02. Code under test: `main` at be004bb (U1 to U9 and the P16 fix, #20); the exceptions are listed under Run facts. Hosts: Claude Code 2.1.316 and Antigravity CLI 1.4.12, both logged in by subscription; every supervisor command ran with all `*_API_KEY` variables unset, and every Claude call reported `apiKeySource: none`. Repository: one private throwaway repository, one target branch per run (`run1`, `d3`, `run2`, `run3`, `alloc`).

Statuses are copied from `check_run.py --json` and `report --public` as printed; none is upgraded.

## Verdict

Run 1 (Claude Code host, Slurm) and Run 2 (Slurm hidden, with the recovery kill) passed every applicable claim, and the D3 fallback, allocation loss and recovery passed live. Run 3 passed every claim it checks with Google-family authors and Claude-family review, but an Antigravity session did not drive it: the orchestrator's host refused to launch an unsandboxed autonomous `agy` session, so the "Antigravity session drives the loop" claim is INCOMPLETE pending an owner decision. The `sacct` item is INCOMPLETE in Runs 1 and 3 because `sacct` was never queried there; the D3 run supplies the fallback proof.

| Item | Result |
|---|---|
| Run 1, Claude Code host, Slurm backend | 17 PASS, 1 INCOMPLETE (`summarize.sacct`, by design) |
| D3, accounting fallback | key claims PASS; 4 INCOMPLETE by design (simulated loss, no issues filed) |
| Run 2, Slurm hidden, local backend, recovery | 19 PASS, exit 0 |
| Run 3, Google authors and Claude review | 17 PASS, 1 INCOMPLETE (`summarize.sacct`, by design) |
| Run 3, an Antigravity session drives the loop | INCOMPLETE: launch refused by the orchestrator host's safety policy; owner decision pending |
| Recovery, kill between `gh pr merge` and its record | PASS (`recovery.slug` in Run 2) |
| Allocation loss | PASS (evidence below; `check_run` has no claim for it) |
| Owner laptop test (O5) | pending, not run by the orchestrator |

## Claims by run

### Run 1: Claude Code host, Slurm backend

`check_run.py` counts: FAIL 0, INCOMPLETE 1, PASS 17.

| Claim | Status | Evidence | Detail |
|---|---|---|---|
| overlap | PASS | `.overseer/plans/accept-run1/units/slug/attempts/a1/inv` | overlapping [('slug', 'count')]; slug ['1793594449..1793594466'], count ['1793594456..1793594470'] |
| slug.judgment | PASS | `.overseer/plans/accept-run1/units/slug/attempts/a1/judgment.json` | DONE, 2 outputs ok |
| slug.review | PASS | `.overseer/plans/accept-run1/units/slug/attempts/a1/reviews/r1/round.json` | 1 round(s), none a fallback; a1/r1 by google reviewed a claude author |
| slug.nonvacuity | PASS | `.overseer/plans/accept-run1/units/slug/attempts/a1/nonvacuous.json` | status PASS |
| slug.merge | PASS | `.overseer/plans/accept-run1/units/slug/merge.json` | state merged, pr_head 81923cd9b008, merge_commit ddde6aefca92, closure merged |
| slug.tracker | PASS | `.overseer/plans/accept-run1/units/slug/unit.json` | tracker closed, issue 1 |
| count.judgment | PASS | `.overseer/plans/accept-run1/units/count/attempts/a1/judgment.json` | DONE, 2 outputs ok |
| count.review | PASS | `.overseer/plans/accept-run1/units/count/attempts/a1/reviews/r1/round.json` | 1 round(s), none a fallback; a1/r1 by google reviewed a claude author |
| count.nonvacuity | PASS | `.overseer/plans/accept-run1/units/count/attempts/a1/nonvacuous.json` | status PASS |
| count.merge | PASS | `.overseer/plans/accept-run1/units/count/merge.json` | state merged, pr_head f1f3603a3bf4, merge_commit 585999fc8c34, closure merged |
| count.tracker | PASS | `.overseer/plans/accept-run1/units/count/unit.json` | tracker closed, issue 2 |
| count.integration | PASS | `.overseer/plans/accept-run1/units/count/attempts/a1/integration.json` | integration base ddde6aefca92 rejudged DONE and recorded in the merge |
| summarize.judgment | PASS | `.overseer/plans/accept-run1/units/summarize/attempts/a1/judgment.json` | DONE, 1 outputs ok |
| summarize.sources | PASS | `.overseer/plans/accept-run1/units/summarize/attempts/a1/inv/01-job` | answered ['scontrol', 'wrapper']; sacct ever answered: false |
| summarize.sacct | INCOMPLETE | `.overseer/plans/accept-run1/units/summarize/attempts/a1/inv/01-job` | sacct never answered: the accounting claim is incomplete; the fallback is proved by the D3 run |
| summarize.tracker | PASS | `.overseer/plans/accept-run1/units/summarize/unit.json` | tracker closed, issue 3 |
| backend | PASS | `.overseer/plans/accept-run1/units` | backends used ['slurm'], expected slurm |
| orchestrator_items | PASS | `.overseer/plans/accept-run1/units` | none |

### Accounting fallback (D3)

`check_run.py` counts: FAIL 0, INCOMPLETE 4, PASS 7.

| Claim | Status | Evidence | Detail |
|---|---|---|---|
| summarize.judgment | PASS | `.overseer/plans/accept-d3/units/summarize/attempts/a1/judgment.json` | DONE, 1 outputs ok |
| summarize.sources | PASS | `.overseer/plans/accept-d3/units/summarize/attempts/a1/inv/01-job` | answered ['wrapper']; sacct ever answered: false |
| summarize.sacct | PASS | `.overseer/plans/accept-d3/units/summarize/attempts/a1/inv/01-job` | judged DONE without sacct from ['wrapper'] |
| summarize.tracker | INCOMPLETE | `.overseer/plans/accept-d3/units/summarize/unit.json` | tracker None, issue None |
| summarize-lost.judgment | PASS | `.overseer/plans/accept-d3/units/summarize-lost/attempts/a1/judgment.json` | DONE, 1 outputs ok |
| summarize-lost.sources | INCOMPLETE | `.overseer/plans/accept-d3/units/summarize-lost/attempts/a1/inv/01-job` | no source answered or no evidence record |
| summarize-lost.sacct | INCOMPLETE | `.overseer/plans/accept-d3/units/summarize-lost/attempts/a1/inv/01-job` | no evidence.json recording that sacct did not answer, or sacct answered, or job not DONE, or no other source: fallback not shown |
| summarize-lost.tracker | INCOMPLETE | `.overseer/plans/accept-d3/units/summarize-lost/unit.json` | tracker None, issue None |
| summarize-lost.resolved | PASS | `.overseer/plans/accept-d3/units/summarize-lost/attempts/a1/judgment.json` | orchestrator-resolved DONE |
| backend | PASS | `.overseer/plans/accept-d3/units` | backends used ['slurm'], expected slurm |
| orchestrator_items | PASS | `.overseer/plans/accept-d3/units` | summarize-lost: resolved DONE: outputs checked: header name,words,lines and 2 rows matching README.md and CHANGELOG.md |

### Run 2: Slurm hidden, local backend (with the recovery kill)

`check_run.py` counts: FAIL 0, INCOMPLETE 0, PASS 19.

| Claim | Status | Evidence | Detail |
|---|---|---|---|
| overlap | PASS | `.overseer/plans/accept-run2/units/slug/attempts/a1/inv` | overlapping [('slug', 'count')]; slug ['1793598267..1793598282'], count ['1793598268..1793598283'] |
| slug.judgment | PASS | `.overseer/plans/accept-run2/units/slug/attempts/a1/judgment.json` | DONE, 2 outputs ok |
| slug.review | PASS | `.overseer/plans/accept-run2/units/slug/attempts/a1/reviews/r1/round.json` | 1 round(s), none a fallback; a1/r1 by google reviewed a claude author |
| slug.nonvacuity | PASS | `.overseer/plans/accept-run2/units/slug/attempts/a1/nonvacuous.json` | status PASS |
| slug.merge | PASS | `.overseer/plans/accept-run2/units/slug/merge.json` | state merged, pr_head 5e90b9a6ef4b, merge_commit 19e7181f5039, closure merged |
| slug.tracker | PASS | `.overseer/plans/accept-run2/units/slug/unit.json` | tracker closed, issue 6 |
| count.judgment | PASS | `.overseer/plans/accept-run2/units/count/attempts/a1/judgment.json` | DONE, 2 outputs ok |
| count.review | PASS | `.overseer/plans/accept-run2/units/count/attempts/a1/reviews/r1/round.json` | 1 round(s), none a fallback; a1/r1 by google reviewed a claude author |
| count.nonvacuity | PASS | `.overseer/plans/accept-run2/units/count/attempts/a1/nonvacuous.json` | status PASS |
| count.merge | PASS | `.overseer/plans/accept-run2/units/count/merge.json` | state merged, pr_head 47d8df731d9a, merge_commit cc958b4cc3d0, closure merged |
| count.tracker | PASS | `.overseer/plans/accept-run2/units/count/unit.json` | tracker closed, issue 7 |
| count.integration | PASS | `.overseer/plans/accept-run2/units/count/attempts/a1/integration.json` | integration base 19e7181f5039 rejudged DONE and recorded in the merge |
| summarize.judgment | PASS | `.overseer/plans/accept-run2/units/summarize/attempts/a1/judgment.json` | DONE, 1 outputs ok |
| summarize.sources | PASS | `.overseer/plans/accept-run2/units/summarize/attempts/a1/inv/01-job` | answered ['wrapper']; sacct ever answered: false |
| summarize.tracker | PASS | `.overseer/plans/accept-run2/units/summarize/unit.json` | tracker closed, issue 8 |
| backend | PASS | `.overseer/plans/accept-run2/units` | backends used ['local'], expected local |
| no_slurm | PASS | `slurm-calls.log` | no job ids, scheduler evidence or logged Slurm calls (shim_log exists and is empty) |
| recovery.slug | PASS | `gh.log` | one logged merge call then KILLED, one merged event, 1 intent event(s) |
| orchestrator_items | PASS | `.overseer/plans/accept-run2/units` | none |

### Run 3: Google-family authors, Claude-family reviewer (Claude Code drove; see Run facts)

`check_run.py` counts: FAIL 0, INCOMPLETE 1, PASS 17.

| Claim | Status | Evidence | Detail |
|---|---|---|---|
| overlap | PASS | `.overseer/plans/accept-run3/units/slug/attempts/a1/inv` | overlapping [('slug', 'count')]; slug ['1793599144..1793599327'], count ['1793599144..1793599215'] |
| slug.judgment | PASS | `.overseer/plans/accept-run3/units/slug/attempts/a1/judgment.json` | DONE, 2 outputs ok |
| slug.review | PASS | `.overseer/plans/accept-run3/units/slug/attempts/a1/reviews/r1/round.json` | 1 round(s), none a fallback; a1/r1 by claude reviewed a google author |
| slug.nonvacuity | PASS | `.overseer/plans/accept-run3/units/slug/attempts/a1/nonvacuous.json` | status PASS |
| slug.merge | PASS | `.overseer/plans/accept-run3/units/slug/merge.json` | state merged, pr_head 57d8c2f0336d, merge_commit 494165f9578d, closure merged |
| slug.tracker | PASS | `.overseer/plans/accept-run3/units/slug/unit.json` | tracker closed, issue 9 |
| count.judgment | PASS | `.overseer/plans/accept-run3/units/count/attempts/a1/judgment.json` | DONE, 2 outputs ok |
| count.review | PASS | `.overseer/plans/accept-run3/units/count/attempts/a1/reviews/r1/round.json` | 1 round(s), none a fallback; a1/r1 by claude reviewed a google author |
| count.nonvacuity | PASS | `.overseer/plans/accept-run3/units/count/attempts/a1/nonvacuous.json` | status PASS |
| count.merge | PASS | `.overseer/plans/accept-run3/units/count/merge.json` | state merged, pr_head 26327ed628f7, merge_commit 79c2ce7b20bc, closure merged |
| count.tracker | PASS | `.overseer/plans/accept-run3/units/count/unit.json` | tracker closed, issue 10 |
| count.integration | PASS | `.overseer/plans/accept-run3/units/count/attempts/a1/integration.json` | integration base 494165f9578d rejudged DONE and recorded in the merge |
| summarize.judgment | PASS | `.overseer/plans/accept-run3/units/summarize/attempts/a1/judgment.json` | DONE, 1 outputs ok |
| summarize.sources | PASS | `.overseer/plans/accept-run3/units/summarize/attempts/a1/inv/01-job` | answered ['scontrol', 'wrapper']; sacct ever answered: false |
| summarize.sacct | INCOMPLETE | `.overseer/plans/accept-run3/units/summarize/attempts/a1/inv/01-job` | sacct never answered: the accounting claim is incomplete; the fallback is proved by the D3 run |
| summarize.tracker | PASS | `.overseer/plans/accept-run3/units/summarize/unit.json` | tracker closed, issue 11 |
| backend | PASS | `.overseer/plans/accept-run3/units` | backends used ['slurm'], expected slurm |
| orchestrator_items | PASS | `.overseer/plans/accept-run3/units` | none |

### Allocation loss

No `check_run` claim covers this case. A session cannot end its own allocation, so the dispatch ran inside a short launcher batch job (the runbook's documented equivalent). The launcher started at 06:15:06Z, dispatched the `slug` author as a batch job and ended at 06:15:07Z with the author still queued. The author started at 06:15:22Z and exited rc 0 at 06:15:46Z, entirely after its launching allocation was gone. A different session then ran `status`, `next`, `wait` and `judge slug` with no manual edit: judgment DONE, test rc 0. Evidence: `units/slug/attempts/a1/inv/01-author/{launch,start,exit}.json`, `judgment.json`, `events.jsonl`, the launcher log.

## Run facts

| Run | Backend | Authors | Reviewer | PRs and issues | Driven by |
|---|---|---|---|---|---|
| Run 1 | Slurm batch | Claude `sonnet` (2 overlapping batch jobs) | Google `gemini-3.8-flash-high`, no fallback | PRs 4, 5; issues 1 to 3 | this Claude Code orchestrator session (Opus 5.5) |
| D3 | Slurm batch | 2 jobs | none (job units) | no issues filed | same session |
| Run 2 | local (Slurm tools replaced by logging shims, `OVERSEER_DISABLE_SLURM=1`) | Claude `sonnet` (2 overlapping local processes) | Google `gemini-3.8-flash-high`, no fallback | PRs 14, 15; issues 6 to 8 | same session |
| Run 3 | Slurm batch | Google `gemini-4-argon-high` (sandboxed `agy` batch jobs) | Claude `haiku`, no fallback | PRs 12, 13; issues 9 to 11 | same session, with `models --host google` (runbook fallback path) |

Every run used one attempt per unit and one review round per code unit. Every second code unit merged after an integration: the target had advanced, so the supervisor merged in the recorded target commit, re-judged the integrated candidate, reran non-vacuity, carried the review by unchanged patch id (D2), and squash-merged bound to the head.

Code-under-test exceptions: Run 1's survey, approval, issue filing, handshake and author dispatch, and all of D3 except its reports, ran at 641f57c. That commit differs from be004bb only in `report.py`, the code-output digest recording in `judge.py`, and the P16 `gh repo view` call; none of those is on the dispatch path or the job-judgment path. Run 1's judgments, reviews and `slug` merge ran at 37d3884 (the U9 merge), which differs from be004bb only by the P16 fix.

What the live runs found and how each was handled:

- **P16, a supervisor defect.** `gh repo view` takes the repository positionally, but the supervisor passed `-R`. After Run 1's `slug` merge, closing the issue on the non-default target therefore failed. The merge stood and the tracker stayed `pending`, as designed. The fix merged as #20 with a stricter test shim, and rerunning `merge slug` closed the issue without merging again.
- **Network timeouts to GitHub (several).** A `pr` attempt reported an ambiguous outcome and its rerun created exactly one PR. A blocked-by link read back as ambiguous and its rerun found it native. One `merge` failed before any merge call and its rerun succeeded.
- **Run 2's `slug` review timed out once.** The Google reviewer exceeded the call timeout under the restricted PATH. The supervisor recorded the failed call, and the one permitted `--retry` completed with PASS.
- **Run 3's Claude reviewer raised one blocking finding on `slug`.** The finding said the change alters `slugify` output for some inputs, which is the requested fix. The orchestrator reproduced the unit's unchanged specification test passing at the candidate (`evidence/rep-1.json`) and ruled the finding dismissed. Three nonblocking findings were ruled nonblocking. One of them, a CHANGELOG `grep` matching an existing heading, was a real fixture weakness, now fixed in the fixture.
- **Operator-side tooling in Run 2.** `command -v` returned bare names for `grep`, `find`, `kill` and `test` (shell functions or builtins in the operator's shell), so the first judgments failed on a missing `grep`. The links were remade with `type -P` and the same attempts were re-judged DONE. The runbook now uses `type -P` and lists every tool the steps need.

## Orchestrator-resolved and simulated items

- **D3 `summarize-lost` (simulation):** the wrapper's `exit.json` was moved aside and the Slurm query tools were hidden. Judgment returned UNCERTAIN (exit 4) and the slot stayed held. The orchestrator inspected `result.csv` (header `name,words,lines` and two rows) and ran `resolve --as done` with that reason and evidence. `report --public` labels the item orchestrator-resolved.
- **D3 `summarize`:** `sacct` was hidden after `scontrol` had forgotten the job (MinJobAge), so the query was made and failed (exit 127). The judgment was DONE from the wrapper receipt and the validated output.
- **Recovery (simulation):** a `gh` shim killed the supervisor right after `gh pr merge` returned. GitHub showed the PR merged while `merge.json` still said `intent`. In a new shell the dead holder's lock was removed with `unlock --force` and a stated reason, then `merge slug` read the PR back, recorded the merge and closed the issue. `gh.log` holds exactly one `pr merge` line.
- **Run 3 `slug`:** one blocking review finding was dismissed with reproduction evidence (above).

## Pending

- **The owner laptop test (O5):** pending, not run by the orchestrator.
- **Run 3 with an Antigravity session as orchestrator:** pending the owner's decision. Launching `agy -p ... --dangerously-skip-permissions` without `--sandbox` was refused by the orchestrator host's safety policy, and the sandbox cannot be kept because it mounts `.git` read-only (P5).

## Limits

- Workers run as the same Unix user; the boundary is cooperative, not containment (C20).
- The D3 lost-evidence case and the recovery kill are simulations. The natural accounting outage (P9) was not observed during these runs: `sacct` was never queried in Runs 1 and 3, because `scontrol` answered first.
- Quota and auth fallback were not exercised; there was no deliberate exhaustion.
- D3 filed no issues, so its tracker claims are INCOMPLETE by design.
- The overlap claims rest on one-second timestamps. The overlaps observed were 10 to 71 seconds long.
