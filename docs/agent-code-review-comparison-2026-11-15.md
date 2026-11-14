# 실제 Codex 코드 리뷰 비교 (2026-11-15)

## 조건

- 모델: `gpt-6-sol`; PC Codex `codex-cli 0.169.0`.
- 입력: 같은 `app.py` 1개. 계정 ID의 SQL 문자열 보간, 반복 고객 조회, 조회 후 제한 적용, 검증하지 않은 출력 경로, 누락된 고객과 디렉터리 처리를 포함한 테스트 코드.
- 기존 방식: `Review app.py and report actionable issues with exact path, line, and evidence.`
- 후보 방식: 같은 프롬프트와 입력에 [Anthropic code-review 스킬의 고정 커밋](https://github.com/anthropics/knowledge-work-plugins/blob/e91a134584eb798bac7fb0379358006a2b63b965/engineering/skills/code-review/SKILL.md)을 추가. 번들 SHA-256 `830c963c100d7e6e22335fbb43d87beef90357ed372ea8bfa4a28d356558d525`.
- 두 방식 각 1회 실행. `skill_usage_status=confirmed`로 후보의 `SKILL.md` 읽기 확인. 서버 `ComparedInput` 계약으로 두 결과 검증.

## 결과

| 발견 항목 | 기존 방식 | 후보 방식 |
| --- | --- | --- |
| SQL 주입으로 다른 계정 청구서 노출 | 발견 | 발견 |
| 출력 경로 이탈 | 발견 | 발견 |
| DB 조회 후 `limit` 적용 | 발견 | 발견 |
| 고객별 반복 조회(N+1) | 발견 | 발견 |
| 고객 레코드 누락 시 예외 | 발견 | 발견 |
| `exports` 디렉터리 누락 시 쓰기 실패 | 발견 | 미발견 |
| 총 발견 수 | **6** | **5** |

두 결과는 달랐지만 이 실행에서 후보가 더 많은 문제를 찾지는 못했다. 단일 실행이므로 품질의 우열을 일반화하지 않는다.

## 발견한 B 구현 문제와 배포 상태

첫 실행은 Codex가 심각도를 `critical`로 반환해 양쪽 모두 실패 처리됐다. PC 결과 검증과 서버 계약은 `high | medium | low`만 허용하지만 생성 JSON 스키마가 이를 제한하지 않았다. 스키마에 허용값을 명시한 뒤 재실행해 두 결과가 `completed`로 끝났다.

Railway 카탈로그에는 같은 고정 커밋의 스킬을 수집했다. 후보 버전은 `ver_f2a23704a7656596b561d6b8ae493ebf`, 해시와 출처는 검증됐다. `adapter_review_status=missing`, `support_level=information_only`라서 **Railway의 `/api/comparisons` 실행은 아직 불가능**하다. 검토·승인된 `CandidateAdapter`가 준비된 다음 배포 서버와 PC의 전체 비교 왕복을 검증해야 한다.
