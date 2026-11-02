# Install

Requirements: `git`, `gh` (logged in), Python 3.9 or newer, and at least one of `claude` (Claude Code) or `agy` (Antigravity), each logged in with the lab subscription. Linux or macOS.

```
git clone https://github.com/goodarzilab/glab-overseer
cd glab-overseer
./install.sh
```

Options: `--claude-dir D` and `--agy-dir D` name a skills root (the skill lands in `D/glab-overseer`); defaults are `~/.claude/skills` and `~/.gemini/config/skills`. `--only claude` or `--only agy` installs one. `--link` installs a directory of symlinks into the checkout, so a `git pull` updates the skill; the default copies. `--dry-run` prints what would happen and writes nothing. `--uninstall` removes the installed directories.

Each install writes a `.glab-overseer-install` marker. The installer refuses to overwrite or remove a directory that lacks it (it prints the path and exits 1, before changing anything), and rerunning with no changes prints `unchanged`. Rerunning after a `git pull` updates marked directories in place.

Check the install: start a new Claude Code or Antigravity session in a project and ask it to plan a project with glab-overseer, or run `python3 ~/.claude/skills/glab-overseer/scripts/overseer.py --help`.

First use in a project: `survey`, `models --host claude|google`, `probe-transport`, and on a cluster `slurm-handshake --plan PLAN_ID` from inside an interactive allocation. The supervisor keeps its state in `.overseer/`, which carries its own `.gitignore`.

Do not set an LLM API-key variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` and similar) in the environment where you start the host: `claude` then bills the key, not the subscription. `survey` warns when one is present.
