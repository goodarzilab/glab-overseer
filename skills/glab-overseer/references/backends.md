# Backends: laptop and Slurm

The supervisor picks the backend per launch: `slurm` when `survey` found all of `sbatch`, `squeue`, `scontrol`, `sacct` and `sinfo` and `sinfo` answered, and the environment variable `OVERSEER_DISABLE_SLURM` is not `1`, `true` or `yes`; otherwise `local`. Run `overseer.py survey` again after the environment changes.

## Local (laptop or any non-Slurm host)

Workers are detached local processes started in their own session. `wait` and `status` track them by process identity, not just pid. Nothing else to set up. `claude` and `agy` must be installed and logged in with the subscription; do not export an API key.

## Slurm (the lab cluster)

Never run an agent on a login node. The supervisor refuses a local launch on a host that has `sbatch` unless a job allocation is active (`SLURM_JOB_ID`). Take an interactive allocation first (the site's interactive wrapper or `srun --pty`), run the orchestrator there, and let the supervisor submit workers with `sbatch`.

1. Put the project on shared storage that compute nodes see at the same path. A project under `/tmp`, `/var/tmp` or `$TMPDIR` is refused: a batch job can have a private `/tmp` and report COMPLETED with nothing visible.
2. Give the plan a `slurm` block (`partition`, `time`, and one of `mem` or `mem_per_cpu`; `account`, `qos`, `gres`, `cpus` optional). Walltime must be finite. Never set both `mem` and `mem_per_cpu`. Code units need the block too, because their authors run as batch jobs.
3. From inside the allocation run `overseer.py slurm-handshake --plan PLAN_ID`. It submits a small job that writes a nonce under the project and checks the controller sees it. A handshake is valid for 7 days; dispatch refuses without one. `--timeout S` and `--poll S` tune the wait.
4. Dispatch as usual. Each worker's wrapper records `start.json` before the payload and `exit.json` after it, keyed by an attempt token.

## Terminal evidence and UNCERTAIN

For a batch job, the supervisor reads `squeue`, `scontrol show job`, `sacct` (5 second timeout; the accounting database can be slow or down) and the wrapper's `exit.json`. Failure evidence beats success. Scheduler logs can mark FAILED but never DONE. With no terminal evidence the attempt is UNCERTAIN and holds its slot until you `resolve` it. A non-zero `sbatch` exit is not proof of rejection (a socket timeout can follow a successful submission); the supervisor reconciles through `squeue` before it launches again.

## Cancelling

`abort UNIT` requests `scancel` and then checks. It reports `UNCONFIRMED` when it cannot verify termination; treat that unit as still running.

## Boundary

Workers run as your user. Sandboxing (`agy --sandbox`) and per-attempt worktrees reduce accidents; they do not contain a hostile process (C20). Worker environments exclude LLM keys, routing variables and GitHub tokens; workers never push.
