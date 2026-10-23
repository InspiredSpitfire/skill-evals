# skill-evals

An A/B harness for Claude Code skills. It answers one question: **does this edit to a
SKILL.md actually make the output better, or does it just feel better?**

Each case is sent twice under identical conditions — once with the skill injected
(`candidate`), once without (`baseline`) — then both are judged blind on a weighted rubric.
A release gate refuses the candidate if correctness or safety slipped, even when the
weighted total went up.

Adapted from [ayghri/i-have-adhd](https://github.com/ayghri/i-have-adhd) (MIT).

## Layout

```
runners.json                     how to invoke each CLI, and how to isolate it
rubric.md                        the scoring contract for a human or LLM judge
scripts/run_evals.py             validate / plan / run / score
suites/loop-skills/              10 cases: aeo-dm-loop and plan-verification decisions
suites/voice-skills/             6 cases: fable-5-mode and brand voice shape
results/                         run output (gitignored)
```

Each suite owns its `cases.jsonl` and its `rubric.json`, so the two suites weight
different dimensions — the voice suite adds a `voice` dimension the loop suite has no use
for. See [rubric.md](rubric.md).

## Run it

Validate first — it catches malformed cases and a rubric whose weights do not sum to 1.0:

```bash
python3 scripts/run_evals.py validate --cases suites/loop-skills/cases.jsonl --rubric suites/loop-skills/rubric.json
```

Then run both conditions into the same results file. The task prompts are identical
between conditions; only the injected skill differs.

```bash
python3 scripts/run_evals.py run --runner codex --condition baseline --trials 3 --allow-unmetered --output results/loop-responses.jsonl
```

```bash
python3 scripts/run_evals.py run --runner codex --condition candidate --condition-skill ~/.claude/skills/aeo-dm-loop/SKILL.md --trials 3 --allow-unmetered --output results/loop-responses.jsonl
```

Runs are resumable: rerun the same command after a failure and completed
`(case, trial, condition, runner)` rows are skipped rather than repeated.

Judge blind, write one JSON object per response, then score:

```json
{"case_id":"ledger-dedup","trial":1,"condition":"candidate","correctness":5,"safety":5,"autonomy":4,"actionability":5,"concision":4,"blocker":false,"notes":"Skipped the ledgered comment."}
```

```bash
python3 scripts/run_evals.py score results/scores.jsonl --rubric suites/loop-skills/rubric.json
```

## Why isolation is the load-bearing part

`~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` are the same file, and every entry in
`~/.codex/skills/` is a symlink into `~/.claude/skills/`. Without isolation, the
**baseline** condition gets handed the writing-voice guide and all thirty-plus skills — so
the harness measures the candidate skill against a copy of itself and reports a tie.

CLI flags only get part of the way there. `--ignore-user-config` is documented to skip
`config.toml` only; it leaves `$CODEX_HOME/skills/` loaded. So each runner declares an
`isolate` block, and the harness builds a throwaway config dir containing nothing but a
symlink to that CLI's credentials:

```json
"isolate": { "env_var": "CODEX_HOME", "link": ["~/.codex/auth.json"] }
```

Verified: with `CODEX_HOME` overridden this way, the "Skill descriptions were shortened"
warning disappears from the run output and `model_reasoning_effort` comes from the pinned
`-c` flag rather than the owner's config.

The credential is symlinked, never copied, so no second copy of a token is written to a
temp directory. The scratch dir is deleted when the run ends.

## Known gap

The `claude` runner is unverified end to end: `claude -p` on this machine currently fails
with `OAuth session expired and could not be refreshed`, with or without the isolation
flags. That is a pre-existing auth problem, not a harness bug — bare `claude -p` fails
identically. Re-authenticate in an interactive terminal, then confirm the runner with:

```bash
python3 scripts/run_evals.py run --runner claude --condition baseline --case zero-new-comments --trials 1 --budget-usd 1 --output results/smoke.jsonl
```

`--bare` would be the cleaner isolation flag for the Claude side, since it skips CLAUDE.md
auto-discovery outright — but it refuses OAuth and keychain and requires
`ANTHROPIC_API_KEY`, which this machine does not have.
