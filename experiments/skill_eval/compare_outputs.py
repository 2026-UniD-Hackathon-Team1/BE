from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


CHOICES = ("baseline", "candidate", "abstain")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--choice", choices=CHOICES)
    parser.add_argument("--reason")
    args = parser.parse_args()

    if args.choice and not args.reason:
        parser.error("--reason is required with --choice")

    report_path = args.report.resolve(strict=True)
    run_root = report_path.parent
    report = load_json(report_path)
    arms = {arm["arm"]: arm for arm in report["arms"]}
    if set(arms) != {"baseline", "candidate"}:
        raise SystemExit("report must contain baseline and candidate arms")
    if any(arm["exit_code"] != 0 for arm in arms.values()):
        raise SystemExit("both arms must complete successfully")

    compared = {}
    for name in ("baseline", "candidate"):
        artifact_dir = run_root / "artifacts" / name
        result = load_json(artifact_dir / "final.json")
        events = (artifact_dir / "events.jsonl").read_text(encoding="utf-8")
        compared[name] = {
            "seconds": arms[name]["seconds"],
            "finding_count": len(result.get("findings", [])),
            "skill_loaded": ".agents/skills/" in events,
            "result": result,
        }

    comparison = {
        "comparison_id": report["run_id"],
        "conditions": {
            "model": report["model"],
            "input_sha256": report["input_sha256"],
            "prompt": report["prompt"],
            "candidate_sha256": report["candidate_sha256"],
            "candidate_source_commit": report["candidate_source_commit"],
        },
        "arms": compared,
        "automatic_winner": None,
        "user_decision": None,
    }
    comparison_path = run_root / "comparison.json"
    comparison_path.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.choice:
        decision = {
            "comparison_id": report["run_id"],
            "choice": args.choice,
            "reason": args.reason,
            "decided_at": datetime.now(UTC).isoformat(),
        }
        with (run_root / "decisions.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps(decision, ensure_ascii=False) + "\n")

    print(comparison_path)


if __name__ == "__main__":
    main()
