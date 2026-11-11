# Review, reproduction and adjudication

A review is bound to one judged candidate head. Reviews and tests run one at a time and do not count against the worker cap.

## Roles

The family that wrote the code does not review it. Claude host: a Google flash model reviews. Antigravity host: Claude `haiku` reviews. On a confirmed quota or auth failure of the reviewer, the supervisor retries once with the host family's small model in a fresh session, and the round is labeled `same_family_fallback` in `round.json`, and the report. Other failures (timeout, transient service) do not trigger a fallback; use `review U --retry` (once per round).

## Commands

`review UNIT [--retry] [--timeout S] [--max-input-chars N]` builds the input (claims, test command and result, earlier findings with their dispositions, the diff against the base) and runs one round. The input is never truncated: if it exceeds the limit, split the unit with `plan split`. Output is a verdict (`PASS` or `FAIL`) and findings, each with `id`, `severity` (`blocking` or `nonblocking`), `claim`, `file`, `line`, `description`, `reproduction`. Exit 0 when the round completed, 1 otherwise.

`reproduce UNIT --finding ID --command CMD [--timeout S]` runs CMD in a disposable worktree at the candidate and stores the result under `evidence/rep-N.json` in the attempt. A reproduction that does not fail is a valid record (`inconclusive` or `dismissed`).

`dispositions UNIT FILE` records rulings for every finding of the latest round. FILE is a JSON object keyed by finding id:

```json
{"f1": {"ruling": "dismissed", "evidence": ["evidence/rep-1.json"], "note": "does not reproduce"}}
```

Rulings: `dismissed`, `fixed`, `nonblocking`, `blocked`, `inconclusive`. Evidence entries are paths relative to the attempt root and must exist. A blocking finding ruled `dismissed`, `fixed` or `nonblocking` needs at least one evidence path. A blocking finding ruled `inconclusive` or `blocked` keeps the unit from merging. A blocking finding ruled `fixed` still needs the fix reviewed in a later round. `dispositions UNIT --state` prints eligibility and the reasons it is not eligible (exit 0 only when ELIGIBLE).

`fix UNIT --prompt-file F` starts a fixer in the same worktree with the host's code model; its prompt is yours plus the unit's scope and test command. A fixer counts toward the worker cap, not toward attempts. After it ends: `judge`, then `review` again (a new round).

`nonvacuous UNIT` reverts the non-test changes in a scratch checkout, keeps the tests and runs the test command. PASS when it fails (the tests really exercise the change), FAIL when it still passes, NOT_APPLICABLE when no test file or no non-test file changed. `merge` requires PASS or NOT_APPLICABLE for the current candidate, or a FAIL overruled by an orchestrator decision at the cap.

## Adjudication and the cap

You adjudicate. Reproduce before acting on a blocking finding; do not accept or dismiss on the reviewer's say-so. At most 3 rounds per unit, counted across its attempts and inherited by split children from the parent (`lineage.review_rounds`); a retry or a split never resets the budget. A new attempt (a retry, or a split child's first attempt) always gets at least one round, even when the budget is already used, so it may use `max(3 - used before it, 1)` rounds. When this attempt has used its rounds and findings are still open, decide alone and record it:

```
overseer.py dispositions UNIT --orchestrator-decision "why this is safe to merge"
```

It is accepted only at the cap, bound to the current candidate, and labeled orchestrator-decision in the report. After a merge-in of an advanced target, the review carries forward only when the unit's own patch id is unchanged.
