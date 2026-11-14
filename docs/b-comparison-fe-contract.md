# B → FE 비교 결과 계약

현재 구현 기준이다. 타입 원본은 `/openapi.json`의 `ComparisonDetail`이다.
연결 프로그램의 입력 등록·비교 실행 흐름은 [agent-comparison-contract.md](agent-comparison-contract.md),
스킬 적용은 [apply-contract.md](apply-contract.md)를 참고한다.

## 결과 조회

`GET /api/comparisons/{comparison_id}`는 등록한 `input_ids` 순서대로 `inputs`와
각 `runs[].inputs[]`를 반환한다. FE는 같은 `input_id`의 `baseline`과 `candidate`를
나란히 표시한다.

```json
{
  "comparison_id": "cmp_01",
  "status": "done",
  "verdict": null,
  "verdict_reason": null,
  "baseline_label": "기존 방식",
  "candidate_label": "리뷰 스킬",
  "change_title": "리뷰 스킬",
  "rubric_version": "review-v1",
  "runs_per_arm": 1,
  "method_id": "wm_01",
  "method_version": 1,
  "candidate_version_id": "ver_01",
  "inputs": [{"input_id": "input_01", "relative_paths": ["app.py"], "task_type": "code_review", "input_sha256": "..."}],
  "plan": {"model_id": "고정 모델 ID", "runner_version": "CLI 버전", "timeout_seconds": 180},
  "runs": [
    {"arm": "baseline", "status": "completed",
     "result": {"inputs": [{"input_id": "input_01", "status": "completed",
       "result": {"findings": [], "summary": "기존 결과 요약"},
       "output_text": "기존 결과 요약", "seconds": 4.2, "metrics": {},
       "skill_usage_status": "not_expected", "failure_code": null}]},
     "output_text": null, "seconds": 4.2, "skill_usage_status": "not_expected",
     "failure_code": null,
     "inputs": [{"input_id": "input_01", "status": "completed",
       "result": {"findings": [], "summary": "기존 결과 요약"},
       "output_text": "기존 결과 요약", "seconds": 4.2, "metrics": {},
       "skill_usage_status": "not_expected", "failure_code": null}]},
    {"arm": "candidate", "status": "completed",
     "result": {"inputs": [{"input_id": "input_01", "status": "completed",
       "result": {"findings": [], "summary": "후보 결과 요약"},
       "output_text": "후보 결과 요약", "seconds": 5.1, "metrics": {},
       "skill_usage_status": "confirmed", "failure_code": null}]},
     "output_text": null, "seconds": 5.1, "skill_usage_status": "confirmed",
     "failure_code": null,
     "inputs": [{"input_id": "input_01", "status": "completed",
       "result": {"findings": [], "summary": "후보 결과 요약"},
       "output_text": "후보 결과 요약", "seconds": 5.1, "metrics": {},
       "skill_usage_status": "confirmed", "failure_code": null}]}
  ],
  "findings": [], "llm_judgment": null,
  "limits": ["각 방식 1회 실행", "자동 우열 판정 없음"],
  "is_prerecorded": false
}
```

위 JSON은 **형식 설명용 예시**다. 실행 시간과 결과물은 실제 호출값을 사용한다.
`runs[].inputs[].result`가 코드 리뷰의 전체 결과물이다. `output_text`는 그중
`summary`만 담고, `runs[].result`는 입력별 기록을 다시 묶은 저장 구조다.
`metrics`와 `findings`는 비어 있고 `llm_judgment`는 `null`일 수 있다.
실패 시 완료된 입력 결과는 남고, 남은 입력의 `status`는 `failed`로 표시된다.

## 사용자 선택

`PATCH /api/comparisons/{id}/decision`

```json
{"choice": "candidate", "reason": "이번 입력에서는 후보 결과가 더 명확합니다."}
```

`choice`는 `baseline | candidate | abstain`이다. 비교 `status=done`일 때만 저장한다.
`verdict`는 저장된 **사용자 선택**이며, 선택 전에는 `null`이다. 후보 선택만으로
스킬은 설치되지 않는다. 선택과 이유는 `GET /api/history`에도 기록된다.

## 현재 시연 범위

- 현재 `POST /api/comparisons`는 등록된 입력 1~3개를 받으며 `code_review`만 실행한다.
- `GET /api/inputs/suggestions`는 현재 코드 리뷰 fixture 1개만 제안한다.
- 연결 프로그램이 켜져 있고 Codex 설치·로그인이 확인돼야 PC 비교 작업을 시작할 수 있다.
- 실제 PC와 배포 Railway 사이의 전체 왕복은 아직 검증하지 않았다.
- 모든 개인 API에는 `X-Local-Token`이 필요하다. 오류는
  `{"error": {"code": "...", "message": "..."}}` 형식이다.
