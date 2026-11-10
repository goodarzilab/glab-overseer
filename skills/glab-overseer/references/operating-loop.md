# Operating loop

State lives under `.overseer/` in the project root. Every command below acts on it; none depends on a running session. Mutating commands hold one project lock, so there is one orchestrator per project. A second orchestrator gets a lock error naming the holder.

## Reading the state

`overseer.py next` prints one line per eligible action, derived from disk with no network call: `ACTION UNIT: detail`. `next --json` gives objects with `plan`, `unit`, `action`, `detail`. `next --plan PLAN_ID` limits it to one plan. `overseer.py status` shows each unit's facets (authorization, execution, closure, tracker), attempts and active invocations, and slot use against the caps. Add `--json` for scripts.

A unit is READY when every unit in `needs` is closed. A code unit is closed when its pull request is merged. A job unit is closed when its judgment is DONE, including an orchestrator-resolved DONE. A worker process exiting closes nothing.

## Actions

`dispatch [--unit U] [--plan P] [--after-quota]`. Without `--unit`, dispatches every READY unit while slots remain (3 per project). A code unit gets a worktree on a branch `overseer/PLAN/UNIT/aN` cut from the freshly fetched target and an author invocation. A job unit gets its command with `OVERSEER_OUT` and runs in the project checkout, so `dispatch` refuses a job while that checkout lacks the merge commit of any code unit in its `needs` (transitively); update the checkout to the target (`git pull --ff-only origin TARGET` on the target branch) and dispatch again. `--after-quota` allows a new attempt after a QUOTA or AUTH failure and needs the owner's agreement. Each dispatch uses an attempt; at most 2 per unit.

`wait [--timeout S] [--poll S]` blocks, polling in Python, until an active invocation changes state. Exit 0 on a change, 3 on timeout (default 1800 s); call it again. The wait itself spends no model turns, but each call is one, so set S just under the host's command limit (570 under Claude Code's 600 s Bash limit) rather than polling with short timeouts.

`judge UNIT [--test-timeout S]` decides the current attempt. Code is DONE when the worker ended, the branch and ancestry are intact, every commit is inside scope with no prohibited artifact, the declared outputs pass at the sealed commit, and the unit's test command passes in a clean checkout of that commit. Job is DONE when terminal evidence shows success and every output predicate passes; known failure evidence always wins and logs alone never produce DONE. Exit 0 DONE, 1 FAILED, 4 UNCERTAIN. A new commit after judgment invalidates it; judge again.

`resolve UNIT --as done|failed --reason TEXT --evidence TEXT` settles an UNCERTAIN attempt (see SKILL.md section 6). It refuses `done` for code units and when predicates fail or failure evidence exists.

`review`, `reproduce`, `dispositions`, `fix`, `nonvacuous`: see `review.md`.

`pr UNIT` pushes the judged candidate and opens or updates its pull request, then reads the head, base and body back. It never force pushes. `merge UNIT` first fetches the target; when the target advanced it merges the target into the unit branch, rejudges that merge, and carries the review forward only when `git patch-id` shows the unit's own change is identical (otherwise a new review round is needed, within the cap). It then pushes, squash-merges with `--match-head-commit`, reads back the merge, marks the unit merged and checks that the issue closed. It records original base, integration base, judged head, remote PR head and merge commit separately. Exit codes: 0 merged, 1 not ready or refused, 3 ambiguous (a call may or may not have taken effect; rerun the same command to reconcile, never repeat it blindly), 5 integration conflict (the merge was aborted; the owner decides).

`issues close UNIT --comment TEXT` closes a job unit's issue after a DONE judgment. Code units' issues close through their merged pull request or, when the target is not the default branch, an explicit close inside `merge`.

`abort UNIT [--grace S]` stops a unit's active invocations and says whether termination was confirmed. `plan split` is in `plan-format.md`. `unlock --force --reason TEXT` removes a stale lock and records an event; it refuses a live local holder unless `--even-if-alive` is given.

## Owner stops

`next` and the commands refuse, and the refusal is logged as an `OWNER_STOP` event, when: the approved plan's digest no longer matches; a cap in the stored plan exceeds the approved value; a unit's scope widens the approved scope; a counter exceeds its cap; more slots are in use than the workers cap. Stop and tell the user. Do not edit files under `.overseer/` to get past a stop.

## Files worth knowing

`events.jsonl` is the append-only action log. Under `plans/PLAN/`: `plan.json`, `approval.json`, `issues.json`, `revisions.json` (splits) and `units/UNIT/unit.json` with facets and counters. Under `units/UNIT/attempts/aN/`: `workspace.json`, `inv/NN-author|fixer|job/` (intent, launch, start, exit, logs), `judgment.json`, `reviews/rK/`, `nonvacuous.json`. Under `units/UNIT/`: `pr.json`, `merge.json`, `stop.json`. `report` reads all of these.
