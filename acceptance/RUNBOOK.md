# Acceptance runbook (U10: O8, D3)

Numbered commands per run, with the evidence to capture after each step. Evidence paths are relative to the run's project root. After a run, evaluate it from state alone:

```
python3 $ACC/check_run.py $PROJ PLAN_ID --expect $PROJ/expect.json [--json]
```

`expect.json` is git-ignored by the fixture; each run lists its contents. Never hand-edit `.overseer/`.

## 0. Variables and setup

```
ACC=<checkout>/acceptance
OV="python3 <installed skill dir>/scripts/overseer.py"
PROJ=<shared-storage dir, one per run; never /tmp, /var/tmp or $TMPDIR>
REPO=goodarzilab/<throwaway>         # one private throwaway repository for all runs
TGT=run1                             # one target branch per run: run1, d3, run2, run3
PLAN=accept-run1                     # one plan id per run: accept-run1, accept-d3, accept-run2, accept-run3
```

Needs `gh` with rights on the throwaway repository, `claude` (and `agy` for Run 3) logged in by subscription, and no `*_API_KEY` variable. Pass site values on the command line; none is committed here.

```
python3 $ACC/make_fixture.py $PROJ --plan-id $PLAN --target $TGT --repo $REPO --partition $PART [--account $ACCT] --time 00:30:00 --mem 8G --cpus 2
gh repo create $REPO --private --add-readme      # once, before the first run; orchestrator step, covered by O8
cd $PROJ && git remote add origin https://github.com/$REPO.git && git push -u origin $TGT
```

Evidence: `plan.json`; one commit on `$TGT`. The target is not the repository's default branch, so `Closes #N` does not auto-close issues; `merge` closes them explicitly, which the tracker claims check. The job needs both code units, so it runs last. Both code units add a line to `CHANGELOG.md` in separate sections, so the second merge integrates cleanly.

## 1. Run 1: Claude Code on the cluster, Slurm backend

Work in an interactive allocation on shared storage, never on a login node. Start Claude Code there with the skill installed; that session is the orchestrator.

1. `$OV survey --probe-auth`. Evidence: `.overseer/survey.json` (Slurm usable, gh authenticated, auth by subscription).
2. `$OV models --host claude`. Evidence: `.overseer/models.json` (author sonnet, reviewer newest Google flash).
3. `$OV probe-transport | tee .overseer/acceptance-probe.txt`.
4. `$OV plan validate plan.json`; show the user the plan and the three issue titles; `$OV plan approve plan.json --approver NAME`. Evidence: `plans/$PLAN/{plan,approval}.json`.
5. `$OV issues file --plan $PLAN` (`--dry-run` first). Evidence: `plans/$PLAN/issues.json`; paste `gh issue list -R $REPO`.
6. `$OV slurm-handshake --plan $PLAN`. It needs the approved plan, so it follows step 4. Evidence: `.overseer/handshake/*/reply.json`.
7. `$OV dispatch --unit slug` and `$OV dispatch --unit count` back to back. Paste `$OV status` showing two RUNNING authors. Evidence: `units/{slug,count}/attempts/a1/inv/01-author/{intent,launch,start,exit}.json`; claim `overlap` reads `start.json` and `exit.json`.
8. `$OV wait` (repeat on exit 3) until both exit.
9. `$OV judge slug`, `$OV judge count`. Evidence: `attempts/a1/judgment.json` (DONE, outputs ok). On FAILED, `dispatch --unit U` starts attempt 2; note why.
10. `$OV review slug`, `$OV review count`. Evidence: `attempts/a1/reviews/r1/{round,review}.json`: `reviewer_family` google, `same_family_fallback` false. A fallback fails the claim; record it.
11. Per finding worth acting on: `$OV reproduce U --finding ID --command CMD`; rule on all with `$OV dispositions U FILE.json`. If a fix is needed: `$OV fix U --prompt-file F`, `wait`, `judge`, `review`. Evidence: `reviews/r*/dispositions.json`.
12. `$OV nonvacuous slug`, `$OV nonvacuous count`. The prompts ask for `tests/test_*_extra.py`, so PASS is expected; NOT_APPLICABLE is accepted but weaker; note it in the report. Evidence: `attempts/a1/nonvacuous.json`.
13. `$OV pr slug`; `$OV merge slug`. Evidence: `units/slug/{pr,merge}.json` (`pr_head`, `merge_commit`); paste `gh pr view`.
14. `$OV pr count`; `$OV merge count`. The target advanced, so this merge integrates it. Evidence: `units/count/attempts/a1/integration.json`, `judgment.json` with `integrated: true`, `merge.json` with `integration_base`, and the `integration_*` lines of `events.jsonl`. A new review round after integration needs `review` and `nonvacuous` again.
15. `$OV next`, then `$OV dispatch --unit summarize`, `$OV wait`, `$OV judge summarize` (sbatch). Evidence: `units/summarize/attempts/a1/inv/01-job/{intent,launch,start,exit,evidence}.json`; `evidence.json` shows which of squeue, scontrol and sacct answered, and `sacct_ever_answered`. The supervisor runs the job with `OVERSEER_OUT` (the output directory) and runs each output validator with `OVERSEER_OUTPUT` (the path of one output file).
16. `$OV issues close summarize --comment "result.csv validated"`. Evidence: `units/summarize/unit.json` tracker closed. Paste `gh issue list -R $REPO --state all` (all three closed).
17. `$OV report --public` (and private, unshared). Evidence: `.overseer/report/public/{report.md,report.json}`.
18. `expect.json`: `{"run": "run1", "backend": "slurm", "job_sources": ["scontrol", "wrapper"], "sacct": "any", "author_family": "claude", "reviewer_family": "google"}`, then `check_run`. The two family keys default to these values for `run1` and `run2` (and to author google, reviewer claude for `run3`); a review round from any other family is FAIL. If sacct answered, rerun with `"sacct": "answered"` to record it; if it never answered that claim is INCOMPLETE and Section 2 supplies the fallback proof (D3).

## 2. Accounting fallback (D3, O9)

A job-only plan in its own project and repository, in an allocation:

```
python3 $ACC/make_fixture.py $PROJ2 --job-only --plan-id accept-d3 --target d3 --repo $REPO --partition $PART [--account $ACCT] --time 00:10:00 --mem 4G --cpus 1
cd $PROJ2 && PLAN=accept-d3 && git remote add origin https://github.com/$REPO.git && git push -u origin d3   # the repository already exists
```

1. In `$PROJ2` with `PLAN=accept-d3`, Run 1 steps 1, 4 and 6 (survey on the full PATH, so `survey.json` records Slurm usable).
2. `$OV dispatch --plan accept-d3`. Then do not run `wait`, `status`, `next` or `judge` for this plan until both jobs have been finished for longer than the cluster's `MinJobAge` (`scontrol show config | grep MinJobAge`); watch them only with `squeue -u $USER`. The supervisor caches a terminal scontrol answer and queries sacct only when scontrol has no record, so an earlier poll would let scontrol answer forever and the fallback would never run.
3. A PATH without `sacct`:

```
mkdir -p $PROJ2/.nosacct
for t in sbatch squeue scontrol sinfo scancel srun python3 git gh bash env sh date hostname uname cat head wc test; do
  p=$(command -v $t) && ln -sf "$p" $PROJ2/.nosacct/$t; done
```

4. `PATH=$PROJ2/.nosacct $OV judge summarize` (do not rerun `survey` in this shell). Expected: scontrol no longer knows the job, the sacct query is made and fails because the tool is missing, and the judgment is DONE from the wrapper `exit.json` and the outputs. The claim `sacct` passes only when `evidence.json` has a `sacct` entry showing the query was made and did not answer; if scontrol still answered (the job was polled or judged before `MinJobAge` passed), the entry is null and the claim is INCOMPLETE (report it as printed). Evidence: `inv/01-job/evidence.json` (sacct outcome not answered, `sacct_ever_answered` false), `judgment.json`.
5. Lost terminal evidence (a simulation; say so in the report): move the second job's wrapper receipt aside and hide the Slurm query tools.

```
INV=.overseer/plans/accept-d3/units/summarize-lost/attempts/a1/inv/01-job
mv $INV/exit.json $INV/exit.json.hold
mkdir -p $PROJ2/.noslurm; for t in python3 git bash env sh date hostname uname cat head wc test; do ln -sf "$(command -v $t)" $PROJ2/.noslurm/$t; done
PATH=$PROJ2/.noslurm $OV judge summarize-lost          # exit 4, UNCERTAIN
```

Evidence: `judgment.json` UNCERTAIN; `$OV status` shows the slot held. If a finished job's scheduler record still answered, the simulation is invalid: report INCOMPLETE, do not edit.
6. Inspect `$INV/../../outputs/result.csv`, then `$OV resolve summarize-lost --as done --reason "outputs checked: header and 2 rows" --evidence "result.csv under attempt a1 outputs; receipt moved aside by the acceptance simulation"`. Evidence: `judgment.json` (`orchestrator_resolved`, `reason`, `evidence`), `inv/01-job/resolved.json`.
7. `$OV report --public` lists the orchestrator-resolved item.
8. `expect.json`: `{"run": "d3", "backend": "slurm", "job_units": ["summarize", "summarize-lost"], "sacct": "unavailable", "uncertain_units": ["summarize-lost"]}`. `summarize-lost.sources` is INCOMPLETE by design; report it as printed. Tracker claims stay INCOMPLETE unless you filed and closed issues.

## 3. Run 2: Slurm hidden, local backend

Inside an allocation:

```
python3 $ACC/make_fixture.py $PROJ3 --no-slurm --plan-id accept-run2 --target run2 --repo $REPO ...
cd $PROJ3 && PLAN=accept-run2 && git remote add origin https://github.com/$REPO.git && git push -u origin run2   # the repository already exists
```

Every project-relative command below runs in `$PROJ3` with `PLAN=accept-run2`.

The login-node guard refuses a local agent launch only when `sbatch` resolves on `PATH` and `SLURM_JOB_ID` is unset. Inside an allocation `SLURM_JOB_ID` is set, so the guard allows the launch whatever `PATH` holds; with Slurm tools removed from `PATH`, `sbatch` does not resolve and there is nothing to refuse. `OVERSEER_DISABLE_SLURM=1` makes the backend local even when a cached `survey.json` said Slurm was usable.

1. Logging shims and a Slurm-free PATH:

```
BIN=$PROJ3/.bin; SH=$PROJ3/.shims; export SHIM_LOG=$PROJ3/slurm-calls.log; mkdir -p $BIN $SH; : > $SHIM_LOG
for t in python3 git gh claude agy node bash env sh date hostname uname cat head wc test grep sed awk tr tee mv cp rm mkdir ls find mktemp readlink dirname basename sleep ps kill id stat xargs; do
  p=$(command -v $t) && ln -sf "$p" $BIN/$t; done
for t in sbatch squeue scontrol sacct sinfo scancel srun salloc; do
  printf '#!/bin/sh\necho "$(date -u +%%FT%%TZ) %s $*" >> "$SHIM_LOG"\nexit 127\n' $t > $SH/$t; chmod +x $SH/$t; done
export OVERSEER_DISABLE_SLURM=1 PATH=$BIN:$SH
PATH=$BIN:$SH claude --version && PATH=$BIN:$SH agy --version   # must both succeed before continuing; add any missing tool (for example a node binary) to $BIN
```

Any Slurm call by the supervisor or a worker lands in `slurm-calls.log`.
2. Run 1 steps 1 to 5 and 7 to 17 with `PLAN=accept-run2` (only the handshake, step 6, is skipped). The job dispatches locally in step 15. Evidence: `survey.json` Slurm usable false; every `inv/*/intent.json` has `"backend": "local"`; no `evidence.json`, job id or `.overseer/handshake`.
3. `slurm-calls.log` is empty (`wc -c`). `check_run` reports `no_slurm` PASS only when the named log exists and is empty; a missing log is INCOMPLETE.
4. `expect.json`: `{"run": "run2", "backend": "local", "shim_log": "slurm-calls.log"}`.

## 4. Run 3: Antigravity host

1. As in Section 0 (`--no-slurm` off-cluster):

```
python3 $ACC/make_fixture.py $PROJ4 --plan-id accept-run3 --target run3 --repo $REPO ...
cd $PROJ4 && PLAN=accept-run3 && git remote add origin https://github.com/$REPO.git && git push -u origin run3   # the repository already exists
```
2. `$OV models --host google`. Evidence: `models.json` (argon-tier author at high, Claude `haiku` reviewer, dated).
3. Start the orchestrator as an `agy -p` session given SKILL.md and the Run 1 step list. The prompt must directly follow `-p` (P13):

```
agy -p "$(echo 'PLAN=accept-run3; all --plan values below use it'; cat <skill dir>/SKILL.md; echo; sed -n '/^## 1\./,/^## 2\./p' $ACC/RUNBOOK.md)" --model <argon-tier> --dangerously-skip-permissions --output-format json > $PROJ4/orchestrator.json
```

`--sandbox` mounts `.git` read-only (P5), which blocks `pr` and `merge`, so the orchestrator runs without it; the supervisor keeps it for workers.
4. `expect.json`: `{"run": "run3", "backend": "slurm", "author_family": "google", "reviewer_family": "claude"}` (on the cluster Run 3 uses the Slurm backend, as written; off-cluster use `"backend": "local"` and omit `shim_log`, so no `no_slurm` claim is made, because only Run 2 builds the logging shims). Author family google, reviewer Claude haiku; these two keys are the defaults for `run3`, and any other family is FAIL.
5. If the agy session cannot drive the loop, record Run 3 INCOMPLETE with the transcript tail from `orchestrator.json`, then run the same steps from Claude Code with `models --host google` as the partial path. The report states who drove each step.

## 5. Recovery: kill between `gh pr merge` and its record

The controller writes `merge.json` (state `intent`) before `gh pr merge` and records `merged` after reading the pull request back. It has no kill hook, so a `gh` shim logs calls, runs the real `gh`, and on `pr merge` kills its parent (the supervisor) before the record is written.

1. At the point before `merge slug` (steps 1 to 13 of Run 1 done up to `pr slug`):

```
REAL_GH=$(command -v gh); SHD=$PROJ/.gh-shim; mkdir -p $SHD
cat > $SHD/gh <<'EOF'
#!/bin/sh
echo "gh $*" >> "$GH_LOG"
"$REAL_GH" "$@"; rc=$?
case "$*" in "pr merge"*) [ -e "$KILL_FLAG" ] && { rm -f "$KILL_FLAG"; echo "KILLED $PPID" >> "$GH_LOG"; kill -9 $PPID; };; esac
exit $rc
EOF
chmod +x $SHD/gh; export REAL_GH GH_LOG=$PROJ/gh.log KILL_FLAG=$PROJ/.kill; touch $KILL_FLAG
PATH=$SHD:$PATH $OV merge slug; echo "exit $?"        # SIGKILL, exit 137
```

2. Evidence: `units/slug/merge.json` state `intent` while `gh pr view` says MERGED (paste both); `gh.log` has one `pr merge` line and `KILLED`.
3. Resume in a new shell (a dead holder's lock: `$OV unlock --force --reason "acceptance recovery"`): `PATH=$SHD:$PATH $OV status`, then `PATH=$SHD:$PATH $OV merge slug`. Expected: it reads the pull request back, sees MERGED, records the merge, closes the tracker, and makes no second `pr merge` call. Evidence: `merge.json` merged with `merge_commit`; `gh.log` still holds exactly one `pr merge` line; one `merged` event for `slug` in `events.jsonl`.
4. `expect.json` addition: `"recovery": {"unit": "slug", "merge_log": "gh.log"}`. The claim passes only when `gh.log` has exactly one `pr merge` line followed by `KILLED`, `merge.json` is merged with a `merge_commit`, and `events.jsonl` has one `merged` event for the unit; a log without `KILLED` is INCOMPLETE (recovery not exercised).

## 6. Allocation loss

1. In an allocation with an approved, handshaken Slurm plan: `$OV dispatch --unit slug` (a batch author). Record `inv/01-author/launch.json` and `$OV status`.
2. End the allocation and the orchestrator session while the author runs (never cancel the batch job). Evidence: `squeue` from elsewhere.
3. In a new allocation and a new session in the same project: `$OV status`, `$OV next`, `$OV wait`, `$OV judge slug`. Expected: the wrapper's `exit.json`, a DONE judgment, no manual edit. Evidence: `inv/01-author/exit.json`, `judgment.json`, `events.jsonl`.
4. `check_run` has no claim for this case; the evidence files above are the proof. If the author died with the allocation, report it INCOMPLETE and repeat with a longer walltime.

## 7. Finishing

Fill `REPORT_TEMPLATE.md` from each run's `check_run.py --json` and `report --public`. The owner laptop test (O5) stays pending.
