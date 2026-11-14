from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime" / "skill-eval"
PROMPT = """Review fixture/app.py for correctness, security, and resource-safety problems.
Do not modify files or run the program. Return only findings supported by the source.
"""


def hash_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(file.relative_to(path).as_posix().encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def prepare_workspace(run_root: Path, name: str, skill: Path | None) -> Path:
    workspace = run_root / "workspaces" / name
    shutil.copytree(EXPERIMENT / "fixture", workspace / "fixture")
    if skill:
        shutil.copytree(skill, workspace / ".agents" / "skills" / skill.name)
    return workspace


def run_arm(run_root: Path, name: str, workspace: Path, model: str, timeout: int) -> dict:
    artifact_dir = run_root / "artifacts" / name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    events_path = artifact_dir / "events.jsonl"
    final_path = artifact_dir / "final.json"
    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--model",
        model,
        "--output-schema",
        str(EXPERIMENT / "review.schema.json"),
        "--output-last-message",
        str(final_path),
        "-C",
        str(workspace),
        "-",
    ]
    started = time.monotonic()
    with events_path.open("w", encoding="utf-8") as events:
        completed = subprocess.run(
            command,
            input=PROMPT,
            text=True,
            stdout=events,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    return {
        "arm": name,
        "exit_code": completed.returncode,
        "seconds": round(time.monotonic() - started, 3),
        "stderr": completed.stderr[-2000:],
        "events_path": str(events_path.relative_to(ROOT)),
        "final_path": str(final_path.relative_to(ROOT)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-skill", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    skill = args.candidate_skill.resolve(strict=True)
    if not (skill / "SKILL.md").is_file():
        raise SystemExit("candidate skill must contain SKILL.md")

    run_root = RUNTIME / "runs" / uuid.uuid4().hex
    run_root.mkdir(parents=True)
    baseline = prepare_workspace(run_root, "baseline", None)
    candidate = prepare_workspace(run_root, "candidate", skill)
    report = {
        "run_id": run_root.name,
        "model": args.model,
        "input_sha256": hash_tree(EXPERIMENT / "fixture"),
        "candidate_sha256": hash_tree(skill),
        "candidate_source_commit": args.source_commit,
        "prompt": PROMPT,
        "arms": [
            run_arm(run_root, "baseline", baseline, args.model, args.timeout),
            run_arm(run_root, "candidate", candidate, args.model, args.timeout),
        ],
    }
    report_path = run_root / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_path)


if __name__ == "__main__":
    main()
