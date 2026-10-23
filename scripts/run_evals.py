#!/usr/bin/env python3
"""Validate, run, and score paired skill evaluations.

Adapted from ayghri/i-have-adhd (MIT) — https://github.com/ayghri/i-have-adhd
Local changes: per-suite rubric weights, runner config isolation (scratch config
dirs and env overrides), suite-based case layout.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = ROOT / "suites" / "loop-skills"
DEFAULT_RUNNERS = ROOT / "runners.json"
CONDITIONS = {"baseline", "candidate", "comparator"}
FALLBACK_WEIGHTS = {
    "correctness": 0.35,
    "autonomy": 0.25,
    "actionability": 0.20,
    "safety": 0.10,
    "concision": 0.10,
}
# Dimensions that may never regress, whatever the weighted total says.
FALLBACK_GUARDS = ["correctness", "safety"]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: line {number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}: line {number}: expected a JSON object")
        rows.append(row)
    return rows


def load_rubric(path: Path | None) -> tuple[dict[str, float], list[str]]:
    """Weights and no-regression guards for one suite."""
    if path is None:
        return dict(FALLBACK_WEIGHTS), list(FALLBACK_GUARDS)
    payload = json.loads(path.read_text(encoding="utf-8"))
    weights = payload.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ValueError(f"{path}: 'weights' must be a non-empty object")
    for name, value in weights.items():
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{path}: weight {name!r} must be a positive number")
    total = sum(float(value) for value in weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"{path}: weights sum to {total:.4f}, expected 1.0")
    guards = payload.get("must_not_regress", FALLBACK_GUARDS)
    if not isinstance(guards, list) or any(name not in weights for name in guards):
        raise ValueError(f"{path}: 'must_not_regress' must list names present in 'weights'")
    return {name: float(value) for name, value in weights.items()}, list(guards)


def completed_keys(rows: list[dict[str, Any]]) -> set[tuple[str, int, str, str]]:
    keys: set[tuple[str, int, str, str]] = set()
    for row in rows:
        fields = (row.get("case_id"), row.get("trial"), row.get("condition"), row.get("runner"))
        if isinstance(fields[0], str) and isinstance(fields[1], int) and all(
            isinstance(value, str) for value in fields[2:]
        ):
            keys.add(fields)  # type: ignore[arg-type]
    return keys


def validate_cases(cases: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    required = {"id", "category", "prompt", "risk", "criteria"}
    for index, case in enumerate(cases, start=1):
        missing = sorted(required - set(case))
        if missing:
            errors.append(f"Case {index}: missing fields: {', '.join(missing)}")
            continue
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"Case {index}: id must be a non-empty string")
        elif case_id in seen:
            errors.append(f"Duplicate case id: {case_id}")
        else:
            seen.add(case_id)
        if case["risk"] not in {"low", "medium", "high"}:
            errors.append(f"Case {case_id}: risk must be low, medium, or high")
        if not isinstance(case["criteria"], list) or not case["criteria"]:
            errors.append(f"Case {case_id}: criteria must be a non-empty list")
    return errors


def _validate_score(row: dict[str, Any], index: int, weights: dict[str, float]) -> None:
    required = {"case_id", "trial", "condition", *weights, "blocker", "notes"}
    missing = sorted(required - set(row))
    if missing:
        raise ValueError(f"Score row {index}: missing fields: {', '.join(missing)}")
    if row["condition"] not in CONDITIONS:
        raise ValueError(f"Score row {index}: unsupported condition {row['condition']!r}")
    for metric in weights:
        value = row[metric]
        if not isinstance(value, (int, float)) or not 1 <= value <= 5:
            raise ValueError(f"Score row {index}: {metric} must be between 1 and 5")
    if not isinstance(row["blocker"], bool):
        raise ValueError(f"Score row {index}: blocker must be boolean")


def _describe_rows(keys: list[tuple[str, Any]]) -> str:
    return ", ".join(f"{case_id}/trial {trial}" for case_id, trial in keys)


def _check_pairing(grouped: dict[str, list[dict[str, Any]]]) -> None:
    """Conditions are only comparable when judged on identical rows."""
    coverage = {
        condition: Counter((row["case_id"], row["trial"]) for row in rows)
        for condition, rows in grouped.items()
    }
    for condition, counts in sorted(coverage.items()):
        repeated = sorted(key for key, count in counts.items() if count > 1)
        if repeated:
            raise ValueError(
                f"{condition}: duplicate score rows for {_describe_rows(repeated)}"
            )
    baseline = coverage["baseline"]
    for condition, counts in sorted(coverage.items()):
        if condition == "baseline" or counts == baseline:
            continue
        details = []
        missing = sorted(set(baseline) - set(counts))
        if missing:
            details.append(f"missing {_describe_rows(missing)}")
        unmatched = sorted(set(counts) - set(baseline))
        if unmatched:
            details.append(f"unmatched {_describe_rows(unmatched)}")
        raise ValueError(
            f"{condition} was not judged on the same rows as baseline: "
            + "; ".join(details)
        )


def summarize_scores(
    scores: list[dict[str, Any]],
    weights: dict[str, float],
    guards: list[str],
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(scores, start=1):
        _validate_score(row, index, weights)
        grouped[row["condition"]].append(row)
    if "baseline" not in grouped or "candidate" not in grouped:
        raise ValueError("Scores must include baseline and candidate conditions")
    _check_pairing(grouped)

    conditions: dict[str, dict[str, Any]] = {}
    for condition, rows in sorted(grouped.items()):
        metrics = {
            metric: sum(float(row[metric]) for row in rows) / len(rows)
            for metric in weights
        }
        conditions[condition] = {
            "rows": len(rows),
            **metrics,
            "weighted_score": sum(metrics[metric] * weight for metric, weight in weights.items()),
            "blocking_findings": sum(bool(row["blocker"]) for row in rows),
        }

    baseline = conditions["baseline"]
    candidate = conditions["candidate"]
    reasons: list[str] = []
    if candidate["blocking_findings"]:
        reasons.append("Candidate has blocking findings.")
    for metric in guards:
        if candidate[metric] < baseline[metric] - 0.1:
            reasons.append(f"Candidate {metric} regressed by more than 0.1 points.")
    if candidate["weighted_score"] <= baseline["weighted_score"]:
        reasons.append("Candidate weighted score did not beat baseline.")

    return {
        "weights": weights,
        "must_not_regress": guards,
        "conditions": conditions,
        "release_gate": {"passed": not reasons, "reasons": reasons},
    }


def _condition_prompt(task: str, condition: str, skill_path: Path | None) -> str:
    if condition == "baseline":
        return task
    if skill_path is None:
        raise ValueError(f"--condition-skill is required for the {condition} condition")
    instructions = skill_path.read_text(encoding="utf-8")
    return (
        "Follow the skill below while completing the task. "
        "Do not discuss or quote the skill.\n\n"
        f"<skill>\n{instructions}\n</skill>\n\n"
        f"<task>\n{task}\n</task>"
    )


def _parse_response(output: str, response_format: str) -> tuple[str, dict[str, Any], float | None]:
    if response_format == "text":
        return output.strip(), {}, None
    if response_format == "claude-json":
        payload = json.loads(output)
        return (
            str(payload.get("result", "")).strip(),
            payload.get("usage", {}) or {},
            payload.get("total_cost_usd"),
        )
    if response_format == "codex-jsonl":
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
        text = ""
        usage: dict[str, Any] = {}
        for event in events:
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text", text)
            if event.get("type") == "turn.completed":
                usage = event.get("usage", usage)
        return str(text).strip(), usage, None
    raise ValueError(f"Unsupported response format: {response_format}")


def _runner_env(runner: dict[str, Any], stack: list[tempfile.TemporaryDirectory]) -> dict[str, str]:
    """Build the child environment, including a scratch config dir when asked.

    Isolation is the whole point of the harness: without it the operator's own
    skills, memory, hooks, and plugins are injected into the baseline condition
    and the run measures the candidate skill against itself. CLI flags only get
    part of the way there (codex --ignore-user-config still loads
    $CODEX_HOME/skills), so the runner can ask for a scratch config dir holding
    nothing but symlinked credentials.
    """
    env = dict(os.environ)
    env.update({key: str(value) for key, value in (runner.get("env") or {}).items()})
    isolate = runner.get("isolate")
    if not isolate:
        return env
    holder = tempfile.TemporaryDirectory(prefix="skill-evals-")
    stack.append(holder)
    scratch = Path(holder.name)
    for raw in isolate.get("link", []):
        source = Path(raw).expanduser()
        if not source.exists():
            raise RuntimeError(
                f"Runner isolation wants {source}, which does not exist. "
                "Fix the path in runners.json or re-authenticate that CLI."
            )
        (scratch / source.name).symlink_to(source)
    env[isolate["env_var"]] = str(scratch)
    return env


def run_evaluations(args: argparse.Namespace) -> int:
    cases = read_jsonl(args.cases)
    errors = validate_cases(cases)
    if errors:
        raise ValueError("\n".join(errors))
    unknown = sorted(set(args.case or []) - {case["id"] for case in cases})
    if unknown:
        raise ValueError(f"--case matched no evaluation case: {', '.join(unknown)}")
    config = json.loads(args.runner_config.read_text(encoding="utf-8"))
    if args.runner not in config:
        raise ValueError(
            f"Unknown runner {args.runner!r}; {args.runner_config} defines: "
            + ", ".join(sorted(config))
        )
    runner = config[args.runner]
    command = list(runner["command"])
    response_format = runner.get("response_format", "text")
    if response_format != "claude-json" and not args.allow_unmetered:
        raise RuntimeError(
            f"The {response_format!r} response format never reports dollar cost; rerun with "
            "--allow-unmetered only when the provider has a separate hard spending cap."
        )
    prior_rows = read_jsonl(args.output) if args.output.exists() else []
    done = completed_keys(prior_rows)
    reported_cost = sum(
        float(row.get("cost_usd") or 0)
        for row in prior_rows
        if row.get("condition") == args.condition and row.get("runner") == args.runner
    )

    if args.budget_usd <= 0 or args.budget_usd > 25:
        raise ValueError("--budget-usd must be greater than 0 and no more than 25")

    scratch_dirs: list[tempfile.TemporaryDirectory] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        env = _runner_env(runner, scratch_dirs)
        with args.output.open("a", encoding="utf-8") as destination:
            for trial in range(1, args.trials + 1):
                for case in cases:
                    if args.case and case["id"] not in args.case:
                        continue
                    key = (case["id"], trial, args.condition, args.runner)
                    if key in done:
                        print(f"skip completed {args.condition} trial {trial}: {case['id']}")
                        continue
                    remaining = args.budget_usd - reported_cost
                    if remaining <= 0:
                        print("Budget exhausted; stopping.", file=sys.stderr)
                        return 2
                    prompt = _condition_prompt(case["prompt"], args.condition, args.condition_skill)
                    invocation = [*command]
                    if runner.get("budget_flag"):
                        invocation.extend([runner["budget_flag"], f"{remaining:.4f}"])
                    invocation.append(prompt)
                    completed = None
                    for attempt in range(args.retries + 1):
                        completed = subprocess.run(
                            invocation,
                            check=False,
                            capture_output=True,
                            text=True,
                            cwd=ROOT,
                            env=env,
                        )
                        if completed.returncode == 0:
                            break
                        if attempt < args.retries:
                            time.sleep(min(2**attempt, 5))
                    assert completed is not None
                    if completed.returncode:
                        detail = completed.stderr.strip() or completed.stdout.strip()
                        if completed.stdout.strip():
                            try:
                                parsed_text, _, _ = _parse_response(completed.stdout, response_format)
                                detail = parsed_text or detail
                            except (ValueError, json.JSONDecodeError):
                                pass
                        raise RuntimeError(
                            f"Runner failed after {args.retries + 1} attempts "
                            f"({shlex.join(invocation[:-1])}):\n{detail}"
                        )
                    text, usage, cost = _parse_response(completed.stdout, response_format)
                    if cost is None and not args.allow_unmetered:
                        raise RuntimeError(
                            "Runner did not report dollar cost; rerun with --allow-unmetered only "
                            "when the provider has a separate hard spending cap."
                        )
                    reported_cost += float(cost or 0)
                    row = {
                        "case_id": case["id"],
                        "trial": trial,
                        "condition": args.condition,
                        "runner": args.runner,
                        "response": text,
                        "usage": usage,
                        "cost_usd": cost,
                    }
                    destination.write(json.dumps(row, ensure_ascii=False) + "\n")
                    destination.flush()
                    print(f"{args.condition} trial {trial}: {case['id']}")
    finally:
        for holder in scratch_dirs:
            holder.cleanup()
    print(f"Reported cost: ${reported_cost:.4f}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    default_cases = DEFAULT_SUITE / "cases.jsonl"

    validate = subparsers.add_parser("validate", help="Validate a case catalog and rubric")
    validate.add_argument("--cases", type=Path, default=default_cases)
    validate.add_argument("--rubric", type=Path)

    plan = subparsers.add_parser("plan", help="Print the paired run matrix as JSONL")
    plan.add_argument("--cases", type=Path, default=default_cases)
    plan.add_argument("--trials", type=int, default=3)
    plan.add_argument("--include-comparator", action="store_true")

    score = subparsers.add_parser("score", help="Aggregate manually judged score rows")
    score.add_argument("scores", type=Path)
    score.add_argument("--rubric", type=Path)

    run = subparsers.add_parser("run", help="Run one evaluation condition")
    run.add_argument("--cases", type=Path, default=default_cases)
    run.add_argument("--runner-config", type=Path, default=DEFAULT_RUNNERS)
    run.add_argument("--runner", required=True)
    run.add_argument("--condition", choices=sorted(CONDITIONS), required=True)
    run.add_argument("--condition-skill", type=Path)
    run.add_argument("--case", action="append")
    run.add_argument("--trials", type=int, default=3)
    run.add_argument("--retries", type=int, default=2)
    run.add_argument("--budget-usd", type=float, default=25.0)
    run.add_argument("--allow-unmetered", action="store_true")
    run.add_argument("--output", type=Path, required=True)
    run.set_defaults(handler=run_evaluations)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "handler"):
        return args.handler(args)
    if args.command == "validate":
        errors = validate_cases(read_jsonl(args.cases))
        weights, guards = load_rubric(args.rubric)
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(
            f"Cases valid. Dimensions: {', '.join(sorted(weights))}. "
            f"No-regression guards: {', '.join(guards)}."
        )
        return 0
    if args.command == "plan":
        cases = read_jsonl(args.cases)
        errors = validate_cases(cases)
        if errors:
            raise ValueError("\n".join(errors))
        conditions = ["baseline", "candidate"]
        if args.include_comparator:
            conditions.append("comparator")
        for trial in range(1, args.trials + 1):
            for case in cases:
                for condition in conditions:
                    print(json.dumps({"case_id": case["id"], "trial": trial, "condition": condition}))
        return 0
    if args.command == "score":
        weights, guards = load_rubric(args.rubric)
        print(json.dumps(summarize_scores(read_jsonl(args.scores), weights, guards), indent=2))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
