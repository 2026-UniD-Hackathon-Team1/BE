# Skill evaluation spike

같은 fixture와 prompt를 Codex CLI로 두 번 실행한다.

- `baseline`: 프로젝트 스킬 없음
- `candidate`: 검토한 프로젝트 스킬 한 개를 `.agents/skills/`에 추가

결과와 JSONL 이벤트는 Git에 포함되지 않는 `runtime/skill-eval/`에 저장한다.
후보 스킬은 신뢰하지 않는 입력으로 취급하고, 전체 내용을 검토한 뒤 읽기 전용
Codex sandbox에서만 사용한다.

```bash
python experiments/skill_eval/run_eval.py \
  --candidate-skill /path/to/review-code \
  --source-commit <git-commit> \
  --model <fixed-model-id>
```

실행 결과를 자동 판정 없이 비교 계약으로 변환하고 사용자 선택 이력을 남긴다.

```bash
python experiments/skill_eval/compare_outputs.py \
  runtime/skill-eval/runs/<run-id>/report.json

python experiments/skill_eval/compare_outputs.py \
  runtime/skill-eval/runs/<run-id>/report.json \
  --choice candidate \
  --reason "결과 구조가 더 이해하기 쉬움"
```

관리 대상 테스트 경로에서 기존 스킬 → 후보 스킬 → 기존 스킬 복원의 전체 왕복을
검증한다. 실제 사용자 스킬 폴더는 변경하지 않는다.

```bash
python experiments/skill_eval/switch_roundtrip.py \
  --model <fixed-model-id>
```

첫 fixture의 알려진 결함은 SQL injection 2건과 닫히지 않는 SQLite connection 2건이다.
단일 실행 결과는 성능 보장이 아니라 실행기와 평가 방식의 성립 여부만 확인한다.
