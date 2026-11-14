# PC 연결 비교 실행 계약

`code_review` 입력 1~3건에 대해 동일한 PC와 Codex 모델로 기존 방식과 후보 스킬을 실행한다.
시스템은 승자를 정하지 않으며, FE는 두 결과를 나란히 표시한 뒤 사용자의 선택을 저장한다.

## 입력 등록

1. 연결 프로그램을 `galtalka run`으로 실행한다. heartbeat의 `codex_ready=true`가 비교 시작 조건이다.
2. `POST /api/inputs`에 `relative_paths`(승인 프로젝트 기준 상대 경로)와 `task_type="code_review"`를 보낸다.
3. 응답이 `202`면 `GET /api/inputs/{input_id}`를 다시 조회한다. 완료하면 입력 ID, 경로 목록, SHA-256을 받는다.

선택한 파일의 스냅샷은 PC의 `<승인 프로젝트>/.galtalka/inputs/`에 남는다. 서버 DB의
`snapshot_inputs` 작업 결과에는 manifest만 저장한다. 원본 입력 파일은 서버에 업로드하지 않는다.
현재 입력은 총 64 KiB 이하여야 한다.

## 비교와 사용자 선택

`POST /api/comparisons` 요청은 기존 [FE 비교 계약](b-comparison-fe-contract.md)의
`method_id`, `method_version`, `candidate_version_id`, `task_type`, `input_ids`,
`confirmed_upload_scope`, `idempotency_key`를 사용한다. 응답 `202`의 `comparison_id`로
`GET /api/comparisons/{id}`를 조회한다. `runs[].inputs[]`에 같은 입력의 양쪽 결과가 들어간다.
`status=done`일 때 `PATCH /api/comparisons/{id}/decision`으로
`baseline | candidate | abstain`과 이유를 저장한다.

비교 작업은 입력을 만든 기기에 고정한다. 다른 기기로 기본 기기를 바꾼 경우 새 기기에서 입력을
다시 등록해야 한다. Codex 버전이 환경 스캔 때와 달라졌거나 로그인되지 않았으면 비교를 시작하지 않는다.
후보 스킬 파일은 승인된 번들 SHA-256과 대조하며 작업 전달 크기 상한은 128 KiB다.

Codex 실행 작업공간과 원본 JSONL은 PC의 `<승인 프로젝트>/.galtalka/comparisons/`에 둔다.
서버에는 화면에 필요한 구조화된 결과만 보고하며 결과 보고 상한은 256 KiB다. 결과의
`summary`·`findings[].evidence`에는 모델이 인용한 코드 일부가 포함될 수 있다.
한쪽 실행이 실패하거나 후보 스킬 사용이 확인되지 않으면 `status=failed`이고 사용자 선택을 막는다.
완료된 쪽의 결과는 상세 조회에 남긴다. 대기 중인 비교를 취소하면 PC 작업 큐에서도 취소된다.

## 확인 범위

PostgreSQL API ↔ 연결 프로그램 ↔ 가짜 Codex 왕복 테스트와 로컬 PC의 실제 Codex 두 실행을
확인했다. 배포 Railway와 사용자 PC 사이의 실기기 왕복은 아직 확인하지 않았다.
