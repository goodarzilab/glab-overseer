# glab-overseer

A Goodarzi Lab skill for Claude Code and Antigravity that turns a repository and an intention into planned, dispatched, reviewed and merged work. The host session (Claude Code or Antigravity) is the orchestrator. A small stdlib Python supervisor, `overseer.py`, holds the state on disk, launches headless workers, records evidence and refuses any closure the evidence does not support. It runs on a laptop and on a Slurm cluster, uses the subscriptions lab members already have, and needs no API keys and no daemon.

## Requirements

`git`, `gh` (logged in), Python 3.9 or newer (standard library only), `claude` (Claude Code) and `agy` (Antigravity), both logged in with the subscription. Either host works as the orchestrator; code written by one family is reviewed by the other, so both CLIs are needed for the full loop. Linux or macOS.

## Install

```
./install.sh
```

This copies the skill to `~/.claude/skills/glab-overseer` and `~/.gemini/config/skills/glab-overseer`. Options (`--claude-dir`, `--agy-dir`, `--only`, `--link`, `--dry-run`, `--uninstall`) and the safety rules are in `skills/glab-overseer/references/install.md`.

## Quick start

In a project directory, start Claude Code or Antigravity and ask it to plan the project with glab-overseer. The skill will run:

```
overseer.py survey
overseer.py models --host claude          # or --host google in Antigravity
overseer.py probe-transport
overseer.py slurm-handshake --plan P      # cluster only, inside an interactive allocation
```

then interview you, write `plan.json`, validate it, show you the plan and every issue title, and ask for one named approval. After that it files the issues and runs the loop (`next`, `dispatch`, `wait`, `judge`, `review`, `pr`, `merge`), stopping only where you must decide. Everything is in `.overseer/`, so a new session resumes with `overseer.py status` and `overseer.py next`. When the run ends, `overseer.py report --public` writes a shareable `report.md` and `report.json`.

Run the tests with `python3 -m unittest discover -s tests`.

## Warning: API keys

If an LLM API-key variable (`ANTHROPIC_API_KEY` and similar) is set in the environment, `claude` bills that key instead of your subscription. Do not set one. The supervisor removes such variables from worker environments and `survey` warns when one is present, but the host session you start yourself is your responsibility.

## Limits

Workers run as your user. Worktrees and sandboxes reduce accidents; they are not containment. The public report is built from an allowlist, and the tracked-file scan in the tests is defense in depth, not proof of confidentiality. The design and decisions are in `PLAN.md`.
