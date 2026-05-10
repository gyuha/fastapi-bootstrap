# {{ cookiecutter.project_name }}

> {{ cookiecutter.project_description }}

[![Python](https://img.shields.io/badge/python-%3E%3D{{ cookiecutter.python_version }}-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com)
[![uv](https://img.shields.io/badge/uv-package%20manager-purple)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/badge/linter-ruff-red)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/badge/license-{{ cookiecutter.license }}-green)](LICENSE)

---

## 목차

- [프로젝트 목적](#프로젝트-목적)
- [기술 스택](#기술-스택)
- [아키텍처](#아키텍처)
- [로컬 개발 환경 부팅](#로컬-개발-환경-부팅)
- [환경변수 목록](#환경변수-목록)
- [디렉토리 구조](#디렉토리-구조)
- [API 문서](#api-문서)
- [테스트 실행](#테스트-실행)
- [코드 품질 도구](#코드-품질-도구)
- [DB 마이그레이션](#db-마이그레이션)

---

## 프로젝트 목적

**{{ cookiecutter.project_name }}** 은 FastAPI 기반의 Production-grade 백엔드 서버입니다.

- **Light 모듈러 모놀리스 DDD** 구조로 도메인 경계를 명확히 유지합니다.
- **Auth 도메인**: JWT(Bearer) + OAuth({{ cookiecutter.oauth_providers }}) + RBAC 기반의 완전한 인증·인가 시스템을 제공합니다.
{% if cookiecutter.include_chat_domain == "yes" %}
- **Chat 도메인**: LangChain + langchain-litellm 기반 LLM 채팅 프록시와 SSE 스트리밍을 지원합니다.
{% endif %}
- **uv + docker-compose** 조합으로 로컬 환경을 1분 내에 부팅합니다.

---

## 기술 스택

### 코어 프레임워크

| 분류 | 기술 | 버전 |
|------|------|------|
| 언어 | Python | >= {{ cookiecutter.python_version }} |
| 웹 프레임워크 | FastAPI | >= 0.115 |
| ASGI 서버 | Uvicorn | >= 0.30 |
| 패키지 매니저 | uv | latest |

### 데이터 계층

| 분류 | 기술 | 버전 |
|------|------|------|
| ORM | SQLAlchemy (async) | >= 2.0.36 |
| 마이그레이션 | Alembic | >= 1.14 |
| DB 드라이버 | asyncpg | >= 0.30 |
| 데이터베이스 | PostgreSQL | >= 16 |
| 캐시/Pub-Sub | Redis | >= 7 |

### 인증·인가

| 분류 | 기술 |
|------|------|
| JWT | python-jose (cryptography) |
| 비밀번호 해시 | passlib + argon2-cffi |
| OAuth 프로바이더 | {{ cookiecutter.oauth_providers }} |
| RBAC | Role + Permission 2테이블 + M:N 조인 |

{% if cookiecutter.include_chat_domain == "yes" %}
### LLM / Chat 도메인

| 분류 | 기술 |
|------|------|
| LLM 오케스트레이션 | LangChain >= 0.3 |
| LLM 어댑터 | langchain-litellm >= 0.2 |
| LLM 프로바이더 | {{ cookiecutter.llm_provider }} (litellm 기반 교체 가능) |
| 스트리밍 | sse-starlette |

{% endif %}
### 인프라 / 도구

| 분류 | 기술 |
|------|------|
| 로컬 인프라 | docker-compose (postgres / redis / mailpit) |
| 이메일 (dev) | Mailpit (SMTP mock) |
| 이메일 (prod) | SMTP (env 설정) — fastapi-mail |
| 로깅 | structlog (JSON) + correlation_id |
| HTTP 클라이언트 | httpx |

### 코드 품질

| 도구 | 목적 |
|------|------|
| Ruff | 린트 + 포맷 |
| Mypy (strict) | 정적 타입 검사 |
| pytest + pytest-asyncio | 테스트 (async 지원) |
| pre-commit | 커밋 전 자동 검사 |

---

## 아키텍처

```
Light Modular Monolith (DDD)
└─ 각 도메인은 자기 완결적 구조를 가집니다 (router / service / repository / models / schemas)
└─ 도메인 간 직접 DB 모델 import 금지 — 인터페이스 또는 이벤트를 통해 통신
```

```
클라이언트
    │
    ├─ POST /api/v1/auth/...     ← Auth 도메인
    │       JWT Bearer 인증
    │       OAuth 소셜 로그인
    │       RBAC 권한 체크
    │
{% if cookiecutter.include_chat_domain == "yes" %}
    ├─ POST /api/v1/chat/...     ← Chat 도메인 (SSE 스트리밍)
    │       LangChain → litellm → {{ cookiecutter.llm_provider }}
    │
{% endif %}
    └─ GET  /health              ← 헬스체크
```

**JWT 전략**

- Access Token: `Authorization: Bearer <token>` 헤더 전용 (TTL {{ cookiecutter.jwt_access_ttl_minutes }}분)
- Refresh Token: TTL {{ cookiecutter.jwt_refresh_ttl_days }}일, rotation + reuse detection 적용
- Blacklist: Redis `jti` 기반 (로그아웃·재사용 감지 시 즉시 폐기)

---

## 로컬 개발 환경 부팅

### 사전 요구사항

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) 또는 Docker Engine
- [uv](https://github.com/astral-sh/uv#installation) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Python >= {{ cookiecutter.python_version }} (`uv python install {{ cookiecutter.python_version }}`)

### 1단계 — 인프라 컨테이너 시작

```bash
# postgres + redis + mailpit 컨테이너 시작
docker-compose up -d

# 헬스 확인
docker-compose ps
```

### 2단계 — Python 의존성 설치

```bash
# uv로 가상환경 생성 + 의존성 설치 (dev 그룹 포함)
uv sync
```

### 3단계 — 환경변수 설정

```bash
cp .env.example .env
# .env 파일을 열어 SECRET_KEY 등 필수 값을 채웁니다
```

### 4단계 — DB 마이그레이션

```bash
uv run alembic upgrade head
```

### 5단계 — 개발 서버 시작

```bash
# hot-reload 활성화
uv run uvicorn {{ cookiecutter.package_name }}.main:app \
    --host {{ cookiecutter.fastapi_host }} \
    --port {{ cookiecutter.fastapi_port }} \
    --reload
```

또는 Makefile이 있다면:

```bash
make dev        # docker-compose up -d + uv sync + alembic upgrade + uvicorn --reload
make stop       # docker-compose stop
make test       # pytest
make lint       # ruff check + mypy
```

### 헬스체크 확인

```bash
curl http://localhost:{{ cookiecutter.fastapi_port }}/health
# → {"status": "ok"}
```

### 메일 확인 (Mailpit)

브라우저에서 `http://localhost:{{ cookiecutter.mailpit_ui_port }}` 접속 → 회원가입 인증 메일 등을 확인할 수 있습니다.

---

## 환경변수 목록

`.env.example` 파일을 복사해 `.env`를 만들고 아래 항목을 채워야 합니다.

### 앱 기본 설정

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `APP_ENV` | `development` | 실행 환경 (`development` / `production`) |
| `DEBUG` | `true` | 디버그 모드 |
| `SECRET_KEY` | *(필수 변경)* | JWT 서명 키 — 최소 32바이트 랜덤 문자열 |
| `ALLOWED_HOSTS` | `*` | 허용 호스트 (`,` 구분) |
| `CORS_ORIGINS` | `http://localhost:3000` | CORS 허용 오리진 (`,` 구분) |

### 데이터베이스

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `POSTGRES_HOST` | `{{ cookiecutter.postgres_host }}` | PostgreSQL 호스트 |
| `POSTGRES_PORT` | `{{ cookiecutter.postgres_port }}` | PostgreSQL 포트 |
| `POSTGRES_USER` | `{{ cookiecutter.postgres_user }}` | DB 사용자 |
| `POSTGRES_PASSWORD` | `{{ cookiecutter.postgres_password }}` | DB 비밀번호 |
| `POSTGRES_DB` | `{{ cookiecutter.postgres_db }}` | DB 이름 |
| `DATABASE_URL` | *(자동 조합)* | `postgresql+asyncpg://...` (명시 시 우선 적용) |

### Redis

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `REDIS_HOST` | `{{ cookiecutter.redis_host }}` | Redis 호스트 |
| `REDIS_PORT` | `{{ cookiecutter.redis_port }}` | Redis 포트 |
| `REDIS_DB` | `{{ cookiecutter.redis_db }}` | Redis DB 번호 |
| `REDIS_URL` | *(자동 조합)* | `redis://...` (명시 시 우선 적용) |

### JWT / 인증

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `JWT_ALGORITHM` | `HS256` | JWT 서명 알고리즘 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `{{ cookiecutter.jwt_access_ttl_minutes }}` | Access Token TTL (분) |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `{{ cookiecutter.jwt_refresh_ttl_days }}` | Refresh Token TTL (일) |

### OAuth 프로바이더

{% if "google" in cookiecutter.oauth_providers %}
| 변수 | 기본값 | 설명 |
|------|--------|------|
| `GOOGLE_CLIENT_ID` | *(필수)* | Google OAuth Client ID |
| `GOOGLE_CLIENT_SECRET` | *(필수)* | Google OAuth Client Secret |
| `GOOGLE_REDIRECT_URI` | `http://localhost:{{ cookiecutter.fastapi_port }}/api/v1/auth/oauth/google/callback` | Google OAuth 콜백 URL |
{% endif %}
{% if "kakao" in cookiecutter.oauth_providers %}
| 변수 | 기본값 | 설명 |
|------|--------|------|
| `KAKAO_CLIENT_ID` | *(필수)* | Kakao REST API 키 |
| `KAKAO_CLIENT_SECRET` | *(선택)* | Kakao Client Secret |
| `KAKAO_REDIRECT_URI` | `http://localhost:{{ cookiecutter.fastapi_port }}/api/v1/auth/oauth/kakao/callback` | Kakao OAuth 콜백 URL |
{% endif %}
{% if "naver" in cookiecutter.oauth_providers %}
| 변수 | 기본값 | 설명 |
|------|--------|------|
| `NAVER_CLIENT_ID` | *(필수)* | Naver Client ID |
| `NAVER_CLIENT_SECRET` | *(필수)* | Naver Client Secret |
| `NAVER_REDIRECT_URI` | `http://localhost:{{ cookiecutter.fastapi_port }}/api/v1/auth/oauth/naver/callback` | Naver OAuth 콜백 URL |
{% endif %}

### 이메일

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `MAIL_SERVER` | `localhost` | SMTP 서버 (dev: mailpit) |
| `MAIL_PORT` | `{{ cookiecutter.mailpit_smtp_port }}` | SMTP 포트 |
| `MAIL_USERNAME` | *(선택)* | SMTP 사용자 |
| `MAIL_PASSWORD` | *(선택)* | SMTP 비밀번호 |
| `MAIL_FROM` | `noreply@{{ cookiecutter.project_slug }}.local` | 발신 이메일 주소 |
| `MAIL_STARTTLS` | `false` | STARTTLS 사용 여부 (prod: `true`) |
| `MAIL_SSL_TLS` | `false` | SSL/TLS 사용 여부 (prod: `true`) |

{% if cookiecutter.include_chat_domain == "yes" %}
### LLM / Chat 도메인

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `LLM_PROVIDER` | `{{ cookiecutter.llm_provider }}` | LLM 프로바이더 (litellm prefix 사용) |
| `LLM_DEFAULT_MODEL` | `{{ cookiecutter.llm_default_model }}` | 기본 모델 이름 |
| `LLM_MAX_TOKENS` | `4096` | 최대 응답 토큰 수 |
| `LLM_TEMPERATURE` | `0.7` | 모델 temperature |
| `OPENAI_API_KEY` | *(provider=openai 시 필수)* | OpenAI API 키 |
| `ANTHROPIC_API_KEY` | *(provider=anthropic 시 필수)* | Anthropic API 키 |
| `GEMINI_API_KEY` | *(provider=gemini 시 필수)* | Google Gemini API 키 |
| `AZURE_API_KEY` | *(provider=azure 시 필수)* | Azure OpenAI API 키 |
| `AZURE_API_BASE` | *(provider=azure 시 필수)* | Azure OpenAI endpoint URL |
| `AZURE_API_VERSION` | `2024-02-01` | Azure OpenAI API 버전 |

> **LLM 프로바이더 교체**: `LLM_PROVIDER` 와 해당 API 키 env만 변경하면 됩니다. 코드 수정 불필요.

{% endif %}

---

## 디렉토리 구조

```
{{ cookiecutter.project_slug }}/
│
├── src/
│   └── {{ cookiecutter.package_name }}/          # 메인 Python 패키지 (src-layout)
│       │
│       ├── main.py                               # FastAPI app 팩토리 + 라이프사이클
│       ├── __init__.py
│       │
│       ├── core/                                 # 횡단 관심사 (도메인 무관 공통 코드)
│       │   ├── config.py                         # Pydantic Settings (env 로딩)
│       │   ├── database.py                       # SQLAlchemy async engine / session
│       │   ├── redis.py                          # Redis 커넥션 풀
│       │   ├── security.py                       # JWT encode/decode, argon2 해시
│       │   ├── middleware.py                     # correlation_id + structlog 미들웨어
│       │   ├── exceptions.py                     # 공통 HTTPException 핸들러
│       │   └── deps.py                           # FastAPI Depends 공통 (get_db, get_current_user 등)
│       │
│       └── domains/                              # DDD Bounded Contexts
│           │
│           ├── auth/                             # 인증·인가 도메인
│           │   ├── router.py                     # /api/v1/auth/ 엔드포인트
│           │   ├── service.py                    # 비즈니스 로직
│           │   ├── repository.py                 # DB 쿼리 (User, RefreshToken 등)
│           │   ├── models.py                     # SQLAlchemy ORM 모델
│           │   ├── schemas.py                    # Pydantic 요청/응답 스키마
│           │   ├── permissions.py                # require_permission 데코레이터
│           │   └── oauth/                        # OAuth 어댑터 (프로바이더별 파일)
{% if "google" in cookiecutter.oauth_providers %}
│           │       ├── google.py
{% endif %}
{% if "kakao" in cookiecutter.oauth_providers %}
│           │       ├── kakao.py
{% endif %}
{% if "naver" in cookiecutter.oauth_providers %}
│           │       └── naver.py
{% endif %}
│           │
{% if cookiecutter.include_chat_domain == "yes" %}
│           └── chat/                             # LLM 채팅 프록시 도메인
│               ├── router.py                     # /api/v1/chat/ 엔드포인트 (SSE)
│               ├── service.py                    # LangChain runnable 오케스트레이션
│               ├── repository.py                 # Conversation / Message DB 쿼리
│               ├── models.py                     # SQLAlchemy ORM 모델
│               └── schemas.py                    # Pydantic 요청/응답 스키마
│
{% endif %}
├── tests/                                        # pytest 테스트
│   ├── conftest.py                               # 공통 fixture (DB, Redis, 앱 클라이언트)
│   ├── auth/                                     # Auth 도메인 통합 테스트
│   │   └── test_auth_flow.py                     # signup → verify → login → refresh → logout
{% if cookiecutter.include_chat_domain == "yes" %}
│   └── chat/                                     # Chat 도메인 통합 테스트
│       └── test_chat_stream.py                   # SSE 스트리밍 + DB 영속화 검증
{% endif %}
│
├── alembic/                                      # DB 마이그레이션
│   ├── env.py                                    # async 마이그레이션 설정
│   ├── script.py.mako                            # 리비전 파일 템플릿
│   └── versions/                                 # 생성된 마이그레이션 파일들
│
├── scripts/                                      # 유틸리티 스크립트
│   └── create_superuser.py                       # 초기 슈퍼유저 생성
│
├── docker-compose.yml                            # 로컬 인프라 (postgres / redis / mailpit)
├── .env.example                                  # 환경변수 템플릿
├── alembic.ini                                   # Alembic 설정
├── pyproject.toml                                # 프로젝트 메타데이터 + 도구 설정
├── Makefile                                      # 개발 편의 명령어
{% if cookiecutter.use_pre_commit == "yes" %}
└── .pre-commit-config.yaml                       # pre-commit 훅 (ruff + mypy)
{% endif %}
```

---

## API 문서

개발 서버 실행 후 브라우저에서 접근:

- **Swagger UI**: `http://localhost:{{ cookiecutter.fastapi_port }}/docs`
- **ReDoc**: `http://localhost:{{ cookiecutter.fastapi_port }}/redoc`
- **OpenAPI JSON**: `http://localhost:{{ cookiecutter.fastapi_port }}/openapi.json`

### 주요 엔드포인트

#### Auth

| Method | Path | 설명 |
|--------|------|------|
| `POST` | `/api/v1/auth/signup` | 이메일 회원가입 |
| `POST` | `/api/v1/auth/verify-email` | 이메일 인증 |
| `POST` | `/api/v1/auth/login` | 로그인 (Access + Refresh 토큰 발급) |
| `POST` | `/api/v1/auth/refresh` | Access 토큰 갱신 (Refresh rotation) |
| `POST` | `/api/v1/auth/logout` | 로그아웃 (Refresh 토큰 폐기) |
| `POST` | `/api/v1/auth/password/reset-request` | 비밀번호 재설정 요청 |
| `POST` | `/api/v1/auth/password/reset` | 비밀번호 재설정 |
| `GET`  | `/api/v1/auth/me` | 내 정보 조회 |
{% if "google" in cookiecutter.oauth_providers %}
| `GET`  | `/api/v1/auth/oauth/google/authorize` | Google OAuth 시작 |
| `GET`  | `/api/v1/auth/oauth/google/callback` | Google OAuth 콜백 |
{% endif %}
{% if "kakao" in cookiecutter.oauth_providers %}
| `GET`  | `/api/v1/auth/oauth/kakao/authorize` | Kakao OAuth 시작 |
| `GET`  | `/api/v1/auth/oauth/kakao/callback` | Kakao OAuth 콜백 |
{% endif %}
{% if "naver" in cookiecutter.oauth_providers %}
| `GET`  | `/api/v1/auth/oauth/naver/authorize` | Naver OAuth 시작 |
| `GET`  | `/api/v1/auth/oauth/naver/callback` | Naver OAuth 콜백 |
{% endif %}

{% if cookiecutter.include_chat_domain == "yes" %}
#### Chat

| Method | Path | 설명 |
|--------|------|------|
| `GET`  | `/api/v1/chat/conversations` | 대화 목록 조회 |
| `POST` | `/api/v1/chat/conversations` | 새 대화 생성 |
| `GET`  | `/api/v1/chat/conversations/{id}` | 대화 상세 조회 |
| `DELETE` | `/api/v1/chat/conversations/{id}` | 대화 삭제 |
| `GET`  | `/api/v1/chat/conversations/{id}/messages` | 메시지 목록 |
| `POST` | `/api/v1/chat/conversations/{id}/messages` | 메시지 전송 (SSE 스트리밍) |

> **SSE 스트리밍**: `Accept: text/event-stream` 헤더를 포함하면 LLM 응답이 토큰 단위로 스트리밍됩니다.

{% endif %}

---

## 테스트 실행

```bash
# 전체 테스트 (커버리지 포함)
uv run pytest

# 특정 마커만 실행
uv run pytest -m unit          # 단위 테스트
uv run pytest -m integration   # DB/Redis 연동 테스트
uv run pytest -m e2e           # E2E 테스트

# 특정 파일
uv run pytest tests/auth/test_auth_flow.py -v

# 커버리지 HTML 리포트 (htmlcov/index.html)
uv run pytest --cov-report=html
```

> 통합 테스트 실행 전 `docker-compose up -d`로 postgres와 redis가 기동되어 있어야 합니다.

---

## 코드 품질 도구

```bash
# 린트 (ruff check)
uv run ruff check src/ tests/

# 포맷 (ruff format)
uv run ruff format src/ tests/

# 린트 + 자동 수정
uv run ruff check --fix src/ tests/

# 타입 검사 (mypy strict)
uv run mypy src/

# 전체 품질 검사 (Makefile)
make lint

{% if cookiecutter.use_pre_commit == "yes" %}
# pre-commit 설치 (최초 1회)
uv run pre-commit install

# 수동 실행
uv run pre-commit run --all-files
{% endif %}
```

---

## DB 마이그레이션

```bash
# 현재 상태 확인
uv run alembic current

# 최신 마이그레이션 적용
uv run alembic upgrade head

# 새 마이그레이션 파일 생성 (모델 변경 후)
uv run alembic revision --autogenerate -m "add_new_field"

# 한 단계 롤백
uv run alembic downgrade -1

# 특정 리비전으로 롤백
uv run alembic downgrade <revision_id>
```

> 마이그레이션 파일은 항상 코드 리뷰를 거쳐 커밋합니다. 자동 생성된 SQL을 반드시 검토하세요.

---

## 로깅

모든 로그는 **structlog JSON** 형식으로 출력되며, 각 요청에 `correlation_id`가 자동으로 부여됩니다.

```json
{
  "timestamp": "2024-01-01T00:00:00Z",
  "level": "info",
  "event": "request_finished",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "method": "POST",
  "path": "/api/v1/auth/login",
  "status_code": 200,
  "duration_ms": 45
}
```

---

## 라이선스

{{ cookiecutter.license }} — [{{ cookiecutter.author_name }}](mailto:{{ cookiecutter.author_email }})
