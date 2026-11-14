from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

from run_eval import EXPERIMENT, RUNTIME, hash_tree, run_arm


def replace_tree(target: Path, source: Path) -> None:
    staged = target.parent / f".{target.name}.stage-{uuid.uuid4().hex}"
    displaced = target.parent / f".{target.name}.old-{uuid.uuid4().hex}"
    shutil.copytree(source, staged)
    if target.exists():
        os.replace(target, displaced)
    try:
        os.replace(staged, target)
    except Exception:
        if displaced.exists():
            os.replace(displaced, target)
        raise
    if displaced.exists():
        shutil.rmtree(displaced)


def has_marker(run_root: Path, arm: str, marker: str) -> bool:
    final = run_root / "artifacts" / arm / "final.json"
    events = run_root / "artifacts" / arm / "events.jsonl"
    return marker in final.read_text(encoding="utf-8") and "SKILL.md" in events.read_text(
        encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    run_root = RUNTIME / "switch-runs" / uuid.uuid4().hex
    workspace = run_root / "workspace"
    target = workspace / ".agents" / "skills" / "review-switch"
    backup = run_root / "backup" / "review-switch"
    target.parent.mkdir(parents=True)
    shutil.copytree(EXPERIMENT / "fixture", workspace / "fixture")
    shutil.copytree(EXPERIMENT / "baseline_skill", target)

    before_sha256 = hash_tree(target)
    before = run_arm(run_root, "before", workspace, args.model, args.timeout)

    shutil.copytree(target, backup)
    replace_tree(target, EXPERIMENT / "candidate_skill")
    applied_sha256 = hash_tree(target)
    applied = run_arm(run_root, "applied", workspace, args.model, args.timeout)

    replace_tree(target, backup)
    restored_sha256 = hash_tree(target)
    restored = run_arm(run_root, "restored", workspace, args.model, args.timeout)

    report = {
        "run_id": run_root.name,
        "target_path": str(target),
        "before_sha256": before_sha256,
        "applied_sha256": applied_sha256,
        "restored_sha256": restored_sha256,
        "hash_restored": before_sha256 == restored_sha256,
        "candidate_changed_target": before_sha256 != applied_sha256,
        "baseline_loaded_before": has_marker(
            run_root, "before", "SKILL_SWITCH_BASELINE_V1"
        ),
        "candidate_loaded_after_apply": has_marker(
            run_root, "applied", "SKILL_EVAL_PROBE_V1"
        ),
        "baseline_loaded_after_restore": has_marker(
            run_root, "restored", "SKILL_SWITCH_BASELINE_V1"
        ),
        "arms": [before, applied, restored],
    }
    report["passed"] = all(
        [
            report["hash_restored"],
            report["candidate_changed_target"],
            report["baseline_loaded_before"],
            report["candidate_loaded_after_apply"],
            report["baseline_loaded_after_restore"],
            all(arm["exit_code"] == 0 for arm in report["arms"]),
        ]
    )
    report_path = run_root / "switch-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_path)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
