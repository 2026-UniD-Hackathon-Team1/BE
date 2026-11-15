# 갈아탈까

> AI 도구의 변화를 놓치지 않고, 내 작업에 더 나은 방식으로 갈아탈 수 있도록 돕는 서비스

## 1. 서비스 소개

### 1.1 한 줄 정의

**갈아탈까**는 AI 도구와 스킬의 변화를 놓치지 않도록 내 작업 환경에 영향을 주는 변화만 골라 알려주고, 실제 작업 결과를 전후로 비교해 더 나은 방식으로 옮겨갈 수 있게 돕는 서비스입니다.

### 1.2 해결하는 문제

AI 도구는 몇 주 단위로 바뀌고, 새로운 스킬과 확장 기능도 빠르게 늘어납니다. 숙련자는 새 도구를 자신의 작업에 직접 적용하고 결과를 판단한 뒤 더 나은 방식으로 옮겨가지만, 입문자는 다음과 같은 문제를 겪습니다.

1. 무엇이 바뀌었는지 몰라 기존 방식으로 계속 작업합니다.
2. 변화를 알더라도 실제로 더 나은지 판단하기 어려워 바꾸지 못합니다.
3. 유행을 따라 도구를 바꿨다가 결과가 나빠져도 이를 알아채기 어렵습니다.

단순히 AI에게 질문하는 것만으로는 이 문제를 해결하기 어렵습니다. 모델은 학습 시점 이후의 변화를 알지 못할 수 있고, 무엇이 바뀌었는지 모르는 사람은 질문할 기회조차 얻기 어렵기 때문입니다.

결국 격차를 만드는 것은 **새 도구를 알고 있는가**가 아니라, **새로 나온 도구가 내 작업에서 실제로 더 나은지 판단해 본 경험이 있는가**입니다.

**갈아탈까**는 사용자에게 필요한 변화만 발견하고, 기존 방식과 새로운 방식을 실제 작업으로 비교하여 더 나은 선택으로 안전하게 옮겨갈 수 있도록 돕습니다.

## 2. 지금 동작하는 것

- 배포 API: https://be-production-7a68d5e8.up.railway.app (문서: `/docs`)
- 웹 화면: https://fe-production-4aa4dae4.up.railway.app ([FE 저장소](https://github.com/2026-UniD-Hackathon-Team1/FE))
- 연결 프로그램: `uv tool install galtalka-agent` ([PyPI](https://pypi.org/project/galtalka-agent/))

**코드 리뷰** 작업은 처음부터 끝까지 실제로 동작합니다. 회원가입 → 연결 프로그램 연결 → 환경 스캔 → 맞춤 추천 → 내 파일로 실제 Codex 비교 → 사용자 선택 → 백업 후 적용 → 되돌리기.
테스트 작성은 API가 받지만 연결 프로그램 실행은 아직 코드 리뷰만 지원합니다.

## 3. 구조

```
웹 화면(FE) ──API──▶ 서버(이 저장소, Railway) ◀──작업 받아 가기── 연결 프로그램(사용자 PC) ──▶ Codex CLI(사용자 PC)
```

| 부분 | 위치 | 하는 일 |
|---|---|---|
| 서버 | `backend/` | 계정·세션, 프로필, GitHub 후보 수집·분류(카탈로그), 개인화 추천, 비교·선택·적용·되돌리기 기록, 연결 프로그램 작업 큐. **서버는 사용자 코드를 실행하지 않습니다.** |
| 연결 프로그램 | `agent/` | `galtalka connect`로 기기 등록, `galtalka run`으로 작업 수신. 승인한 폴더만 읽고, 복사본에서 Codex를 기존 방식·후보 스킬로 각각 실행, 백업 후 스킬 설치·복원 |
| DB 스키마 | `migrations/` | Alembic. 배포 전에 `python -m backend.migrate`가 실행됩니다 |

서버가 판정하지 않습니다. 비교는 두 결과물(파일·줄·심각도·문제·근거·고치는 법)과 실행 시간을 저장하고, 고르는 것은 사용자입니다. 운영자가 승인한 후보(`comparable`, `applicable`)만 비교·적용할 수 있습니다.

## 4. 로컬 실행

Python 3.11, PostgreSQL.

```bash
uv venv -p 3.11 .venv && uv pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg://localhost:5432/galtalka_dev
export APP_LOCAL_TOKEN=$(openssl rand -hex 24)      # 32자 이상
export CORS_ORIGINS=http://localhost:5173
.venv/bin/python -m backend.migrate
.venv/bin/python -m backend.serve                    # http://127.0.0.1:8765
```

프로젝트 폴더의 `.env`도 읽습니다(셸에 지정한 값이 우선).

| 변수 | 설명 |
|---|---|
| `DATABASE_URL` | PostgreSQL 연결 주소 (필수) |
| `APP_LOCAL_TOKEN` | 로그인 없이 쓰는 공용 토큰. 배포에서는 32자 이상 필수 |
| `CORS_ORIGINS` | 허용할 웹 주소, 쉼표로 구분 (`*` 불가) |
| `PORT` | 서버 포트 (기본 8765) |
| `APP_ENV` | `staging`·`production`이면 배포 설정 검사와 시작 시 마이그레이션 |

### 연결 프로그램 (사용자 PC)

```bash
uv tool install galtalka-agent
galtalka connect <연결 코드> --server <서버 주소> --root <프로젝트 폴더>
galtalka run
```

연결 코드는 웹 온보딩의 [연결 명령 받기]에서 받습니다. 비교하려면 Codex CLI 로그인과 프로젝트의 `.codex/config.toml`(`model = "…"`)이 필요합니다.

## 5. 테스트

```bash
createdb galtalka_test
POSTGRES_TEST_URL=postgresql+psycopg://localhost:5432/galtalka_test .venv/bin/python -m pytest -q
```

실제 서버·연결 프로그램 프로세스로 사용자 흐름 전체를 돌려 보려면 별도 DB로 `scripts/simulate_user_flow.py --database-url …`를 실행합니다.

## 6. 배포 (Railway)

`railway.json`: Dockerfile로 빌드 → 배포 전 `python -m backend.migrate` → `python -m backend.serve`, 헬스체크 `/api/health`. `main`에 머지하면 다시 배포됩니다.

## 7. 문서

`docs/`에 연결 프로그램 비교 계약(`agent-comparison-contract.md`), 적용 계약(`apply-contract.md`), 실제 배포 환경 비교 검증 기록(`agent-code-review-comparison-2026-11-15.md`) 등이 있습니다.
