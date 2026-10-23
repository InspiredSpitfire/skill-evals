# skill-evals

A/B harness for Claude Code skills: each case runs twice under identical conditions — with the skill injected (candidate) vs without (baseline) — judged blind on the weighted rubric in `rubric.md`. The release gate refuses a candidate if correctness or safety slipped, even when the weighted total improved.

## Layout
- `suites/` — eval cases per skill
- `runners.json` — run configuration
- `rubric.md` — judging rubric (weighted)
- `results/` — run outputs; never hand-edit
- `scripts/` — runner tooling

## Rules
- This repo is the quality gate for `~/.claude/skills/` and `~/dev/seo-aeo-toolkit/skills/` — when a skill edit matters, run its suite here before shipping.
- Never tune the rubric and the candidate in the same change; that's grading your own homework.
- Adapted from ayghri/i-have-adhd (MIT) — keep the attribution.
