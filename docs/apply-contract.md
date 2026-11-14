# 스킬 적용·복원 API 계약 (작업자 B)

이 API는 연결 프로그램이 사용자 PC에서 실행 중일 때만 작업을 접수합니다. 모든 웹 요청에는 `X-Local-Token`이 필요합니다. 후보를 선택한 것만으로 설치하지 않습니다.

1. `PATCH /api/comparisons/{id}/decision`에 `{"choice":"candidate","reason":"..."}`을 저장합니다.
2. `POST /api/apply/preview`에 `{"comparison_id":"cmp_..."}`을 보냅니다. 응답 `202`: `{"preview_id":"job_...","status":"queued"}`.
3. `GET /api/apply/preview/{preview_id}`를 `done`까지 조회합니다. 완료 시 `target_path`(승인 프로젝트 기준 상대 경로), `changes[]`, `current_state_sha256`, `candidate_sha256`, `change_set_sha256`을 반환합니다. 이 단계에서 PC 파일은 바뀌지 않습니다.
4. 사용자가 변경 목록을 확인하면 `POST /api/apply`에 `{"preview_id":"job_...","change_set_sha256":"..."}`을 보냅니다. 응답 `202`: `{"apply_id":"apply_...","status":"applying","job_id":"job_..."}`.
5. `GET /api/apply/{apply_id}`를 조회해 `status: applied`를 확인한 뒤 완료로 표시합니다. `recovery_required`는 자동 완료나 재시도를 표시하지 말고 상태 확인을 안내합니다.
6. 복원 시 `POST /api/apply/{apply_id}/rollback`에 `{"confirmed":true}`를 보냅니다. 응답 `202`와 `job_id`를 받은 뒤 같은 `GET /api/apply/{apply_id}`에서 `rolled_back`을 확인합니다.

적용 조건은 완료된 비교, 사용자의 후보 선택, 확인된 후보 스킬 사용, `applicable`과 검토 완료된 고정 번들입니다. 에이전트가 오프라인이면 `AGENT_OFFLINE` 409를 반환합니다. 적용 직전 PC의 스킬 내용이 미리보기와 달라졌으면 파일을 덮어쓰지 않고 작업 실패 및 `recovery_required`로 기록합니다. 이 상태는 사용자가 PC 내용을 확인하고 새 미리보기부터 다시 시작해야 합니다.

`GET /api/apply/{apply_id}`는 에이전트 작업 결과를 적용 이력에 반영합니다. 따라서 적용·복원 결과와 `/api/history`를 표시하기 전에 이 조회를 수행해 주세요. `status`만으로 자동 우열을 판단하지 않습니다.
