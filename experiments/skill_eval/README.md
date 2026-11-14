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

## 검증 기록

2026-11-14 로컬 환경에서 Codex CLI 0.169.0과 고정 모델 `gpt-5.6-sol`로
실행했다. 원본 JSONL에는 로컬 경로와 실행 환경이 포함될 수 있어 Git에 올리지 않고
아래 재현에 필요한 사실만 기록한다.

- 입력 SHA-256: `86334649763556aff93f66ae2b659010d69844bd5b2e57e67bff2784cfb05b05`
- 후보 SHA-256: `baf80ccce07b660fe68b418c59b4e027c72cf075ece3944049d21e04d5e7a5e7`
- baseline/candidate 종료 코드: 모두 `0`
- candidate 이벤트에서 프로젝트 `SKILL.md` 읽기와 `SKILL_EVAL_PROBE_V1` 확인
- baseline/candidate 모두 알려진 결함 4건을 찾아 성능 우위는 판정하지 않음
- 전환 전 해시: `4ad2aa1d52d76196157d0615dac6039cecac94dee03e4d69cf9f2574d8767a9c`
- 후보 적용 후 해시: `baf80ccce07b660fe68b418c59b4e027c72cf075ece3944049d21e04d5e7a5e7`
- 복원 후 해시: 전환 전 해시와 일치
- 전환 전·후·복원 후 새 Codex 세션에서 기대한 스킬 마커 확인
- 왕복 검증 최종 결과: `passed: true`

이 기록은 후보 스킬의 성능 보장이 아니라 비교·선택·적용·복원 흐름의 기술적
실행 가능성을 확인한 결과다.
