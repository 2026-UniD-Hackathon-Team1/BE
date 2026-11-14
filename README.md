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

## 개발 실행

Python 3.11 이상을 사용합니다. Windows PowerShell 기준:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m backend.seed
.\.venv\Scripts\python -m pytest -q
```

seed는 실제 수집 결과가 아닌 오프라인 개발용 데모입니다.

## GitHub 후보 수집

`config/catalog.example.json`에 검색어와 조회 상한을 설정합니다. 지정 저장소가 없어도 공개 검색으로 후보를 발견하며, 스킬과 일반 저장소를 구분합니다.

```powershell
# 설정 검증만 수행하며 네트워크에 연결하지 않음
.\.venv\Scripts\python -m backend.catalog --config config/catalog.example.json

# 실제 공개 GitHub API 호출. 실행 전 대상과 호출 제한 확인 필요
.\.venv\Scripts\python -m backend.catalog --config config/catalog.example.json --execute-network
```

현재 CLI는 토큰을 자동으로 읽지 않고 무인증 공개 조회만 사용합니다. 검색은 설정당 첫 결과 묶음만 확인하며 전체 GitHub를 빠짐없이 수집하는 방식은 아닙니다. 요청 제한에 도달하면 실행을 중단하고 재시도 정보를 반환합니다. 외부 스킬은 실행하지 않습니다.

고정 커밋의 스킬 파일 전체를 해시하고 원본을 DB의 JSON 번들로 보관합니다. 파일과 응답 크기, 파일 수에 상한이 있으며 잘린 트리나 링크가 있는 스킬은 실패로 처리합니다. 버전은 중복 저장하지 않고 실패 시 기존 카탈로그를 유지합니다. 과거 콘텐츠로 복귀하면 기존 버전을 다시 가리키며 복귀 알림 이벤트는 아직 생성하지 않습니다.

새 후보는 `information_only`, 태깅은 `pending` 상태로 저장합니다. 실제 수집 API 연동, 분류와 개인화 추천, PostgreSQL 및 배포 검증은 아직 완료되지 않았습니다.

## 프론트용 API 문서

DB 마이그레이션과 선택적 데모 seed 실행 후 서버를 시작합니다.

```powershell
$env:CORS_ORIGINS = 'http://localhost:5173'
.\.venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

- Swagger UI: http://127.0.0.1:8765/docs
- OpenAPI JSON: http://127.0.0.1:8765/openapi.json
- 상태 확인: `GET /api/health`
- 현재 후보 목록: `GET /api/catalog/candidates?kind=skill&limit=20&offset=0`
- 수집 상태: `GET /api/catalog/sources`

현재는 공통 공개 카탈로그 조회만 제공합니다. 개인화 추천과 사용자 정보 및 비교 API는 구현 후 문서에 추가합니다. 후보 목록은 미검증 정보이며 데모 여부를 표시합니다. 원본 파일과 로컬 경로는 목록에 포함하지 않습니다. 오류는 `{"error":{"code":"...","message":"..."}}` 형식입니다.

개발 서버는 기본적으로 로컬에만 연결하고 CORS는 `CORS_ORIGINS`에 지정한 주소만 허용합니다. 공개 배포 주소와 PostgreSQL 검증은 아직 준비되지 않았습니다.
