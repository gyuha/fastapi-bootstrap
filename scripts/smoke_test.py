#!/usr/bin/env python3
"""smoke_test.py — 핵심 API 엔드포인트 스모크 테스트

실행 중인 FastAPI 서버를 대상으로 핵심 API 흐름을 HTTP 요청으로 검증합니다.
실패 시 명확한 오류 메시지와 함께 exit code 1로 종료합니다.

검증 흐름:
  1. 헬스체크         : GET  /health + GET /ready (PostgreSQL/Redis/Mailpit 연결 검증)
  2. 회원가입         : POST /api/v1/auth/signup
  3. 이메일 인증      : Mailpit API → 토큰 파싱 → POST /api/v1/auth/verify-email/{token}
  4. 로그인           : POST /api/v1/auth/login  → access_token / refresh_token
  5. 보호된 엔드포인트 : GET  /api/v1/auth/me  (Bearer <access_token>)
  6. 토큰 갱신        : POST /api/v1/auth/refresh
  7. 로그아웃         : POST /api/v1/auth/logout
  8. 로그아웃 후 차단  : GET  /api/v1/auth/me → 401 확인

  9. 대화 생성        : POST /api/v1/chat/conversations
 10. 메시지 스트리밍   : POST /api/v1/chat/conversations/{id}/messages (SSE)


사용법:
    # 기본 (서버가 localhost:8000에서 실행 중이어야 함)
    python scripts/smoke_test.py

    # 사용자 지정 호스트/포트
    python scripts/smoke_test.py --host localhost --port 8000

    # 이메일 인증 건너뛰기 (is_verified=True 로 계정을 직접 만드는 경우)
    python scripts/smoke_test.py --skip-email-verify

    # 채팅 도메인 테스트 건너뛰기
    python scripts/smoke_test.py --skip-chat

    # 타임아웃 지정 (초)
    python scripts/smoke_test.py --timeout 30

종료 코드:
    0 — 모든 테스트 통과
    1 — 테스트 실패 (오류 내용 출력 후 종료)
    2 — 서버 미응답 (연결 거부)
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

# ── Python 버전 확인 ─────────────────────────────────────────────────────────
if sys.version_info < (3, 12):
    print("[ERROR] Python 3.12 이상이 필요합니다.", file=sys.stderr)
    sys.exit(1)

# ── ANSI 색상 ────────────────────────────────────────────────────────────────
_USE_COLOR = sys.platform != "win32" and hasattr(sys.stdout, "fileno") and sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _ok(msg: str) -> None:
    print(f"  {_c('✓', '1;32')}  {msg}")


def _info(msg: str) -> None:
    print(f"  {_c('·', '36')}  {msg}")


def _section(title: str) -> None:
    bar = "─" * 52
    print(f"\n{_c(bar, '1;36')}")
    print(f"{_c(f'  {title}', '1;36')}")
    print(_c(bar, "1;36"))


def _die(msg: str, *, hint: str = "") -> None:
    """오류 메시지를 출력하고 exit 1로 종료합니다."""
    print(f"\n{_c('[FAIL]', '1;31')} {msg}", file=sys.stderr)
    if hint:
        print(f"       {_c('→', '33')} {hint}", file=sys.stderr)
    sys.exit(1)


# ── HTTP 헬퍼 ────────────────────────────────────────────────────────────────


def _build_request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode()

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    return req


def http_request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
    allow_error: bool = False,
) -> tuple[int, dict[str, Any] | str]:
    """
    단순 HTTP 요청을 수행하고 (status_code, response_body)를 반환합니다.

    allow_error=True이면 4xx/5xx 응답도 예외 없이 반환합니다.
    """
    req = _build_request(url, method=method, body=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        if allow_error:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw
        raise
    except urllib.error.URLError as exc:
        _die(
            f"서버에 연결할 수 없습니다: {url}",
            hint=f"원인: {exc.reason}\n       서버가 실행 중인지 확인하세요: make dev",
        )


def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
    allow_error: bool = False,
) -> tuple[int, dict[str, Any] | str]:
    return http_request(
        url, method="GET", headers=headers, timeout=timeout, allow_error=allow_error
    )


def http_post(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
    allow_error: bool = False,
) -> tuple[int, dict[str, Any] | str]:
    return http_request(
        url, method="POST", body=body, headers=headers, timeout=timeout, allow_error=allow_error
    )


def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── SSE 스트리밍 헬퍼 ─────────────────────────────────────────────────────────


def read_sse_chunks(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    max_chunks: int = 5,
) -> list[str]:
    """
    SSE 엔드포인트에 POST 요청을 보내고 최대 max_chunks 개의
    data: 줄을 수집하여 반환합니다.

    연결 직후 첫 청크가 도착하면 성공으로 간주합니다.
    """
    req = _build_request(url, method="POST", body=body, headers=headers)
    req.add_header("Accept", "text/event-stream")

    chunks: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 201, 202):
                raw = resp.read().decode("utf-8", errors="replace")
                _die(
                    f"SSE 엔드포인트가 {resp.status}를 반환했습니다: {url}",
                    hint=f"응답: {raw[:300]}",
                )

            # SSE 스트림에서 data 줄 읽기
            buffer = ""
            while len(chunks) < max_chunks:
                chunk = resp.read(512)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                lines = buffer.split("\n")
                buffer = lines[-1]  # 마지막 불완전한 줄 보존
                for line in lines[:-1]:
                    if line.startswith("data:"):
                        data_val = line[5:].strip()
                        if data_val and data_val != "[DONE]":
                            chunks.append(data_val)
                        if data_val == "[DONE]":
                            return chunks
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        _die(
            f"SSE 요청 실패 {exc.code}: {url}",
            hint=f"응답: {raw[:300]}",
        )
    except urllib.error.URLError as exc:
        _die(
            f"SSE 연결 실패: {url}",
            hint=f"원인: {exc.reason}",
        )

    return chunks


# ── Mailpit API 헬퍼 ─────────────────────────────────────────────────────────


def mailpit_wait_for_email(
    mailpit_url: str,
    to_address: str,
    *,
    timeout: int = 20,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """
    Mailpit에서 특정 수신자의 최신 이메일을 기다립니다.

    성공하면 메시지 dict를 반환합니다.
    timeout 초 내에 이메일이 도착하지 않으면 _die()로 종료합니다.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, data = http_get(
                f"{mailpit_url}/api/v1/messages",
                timeout=5,
                allow_error=True,
            )
        except SystemExit:
            # Mailpit이 아직 시작되지 않았을 수 있음 — 재시도
            time.sleep(poll_interval)
            continue

        if status == 200 and isinstance(data, dict):
            messages: list[dict[str, Any]] = data.get("messages", [])
            # 수신자 주소로 필터링
            for msg in messages:
                to_list: list[dict[str, str]] = msg.get("To", [])
                if any(to_address.lower() in t.get("Address", "").lower() for t in to_list):
                    return msg

        time.sleep(poll_interval)

    _die(
        f"Mailpit에서 {to_address} 수신 이메일을 {timeout}초 내에 찾지 못했습니다.",
        hint=f"Mailpit UI: {mailpit_url}  — 이메일 도착 여부를 확인하세요.",
    )


def mailpit_get_message_body(mailpit_url: str, message_id: str) -> str:
    """Mailpit에서 메시지 본문(텍스트)을 가져옵니다."""
    status, data = http_get(
        f"{mailpit_url}/api/v1/message/{message_id}",
        allow_error=True,
    )
    if status != 200 or not isinstance(data, dict):
        _die(
            f"Mailpit 메시지 조회 실패 ({status}): {message_id}",
            hint=f"Mailpit UI: {mailpit_url}",
        )
    # 텍스트 본문 또는 HTML 본문 반환
    return str(data.get("Text", data.get("HTML", "")))


def extract_verification_token(body: str, base_url: str) -> str:
    """
    이메일 본문에서 이메일 인증 토큰을 추출합니다.

    다음 패턴을 순서대로 시도합니다:
    1. /verify-email?token=<TOKEN>
    2. /verify-email/<TOKEN>
    3. token=<TOKEN> (쿼리 파라미터 단독)
    """
    # 패턴 1: URL 내 쿼리 파라미터
    match = re.search(
        r"/(?:api/v1/)?auth/verify[\-_]?email\?token=([A-Za-z0-9._\-]+)",
        body,
    )
    if match:
        return match.group(1)

    # 패턴 2: 경로 파라미터
    match = re.search(
        r"/(?:api/v1/)?auth/verify[\-_]?email/([A-Za-z0-9._\-]+)",
        body,
    )
    if match:
        return match.group(1)

    # 패턴 3: 범용 token= 파라미터
    match = re.search(r"[?&]token=([A-Za-z0-9._\-]+)", body)
    if match:
        return match.group(1)

    _die(
        "이메일 본문에서 인증 토큰을 찾을 수 없습니다.",
        hint="이메일 본문을 확인하세요. 인증 URL 형식이 다를 수 있습니다.\n"
        "       --skip-email-verify 플래그로 이 단계를 건너뛸 수 있습니다.",
    )


# ── 테스트 단계들 ─────────────────────────────────────────────────────────────


@dataclass
class SmokeTestConfig:
    base_url: str
    mailpit_url: str
    timeout: int = 15
    skip_email_verify: bool = False
    skip_chat: bool = False


@dataclass
class TestState:
    """테스트 단계들 사이에서 공유되는 상태."""

    email: str = ""
    password: str = ""
    access_token: str = ""
    refresh_token: str = ""
    user_id: str = ""
    conversation_id: str = ""


def step_health_check(cfg: SmokeTestConfig) -> None:
    """GET /health and /ready → server and local infra are reachable."""
    _section("단계 1 — 헬스체크 및 로컬 인프라 연결 검증")

    health_url = f"{cfg.base_url}/health"
    _info(f"GET {health_url}")

    status, data = http_get(health_url, timeout=cfg.timeout, allow_error=True)
    if status != 200:
        _die(
            f"헬스체크 실패: HTTP {status}",
            hint=f"응답: {str(data)[:200]}\n       서버가 실행 중인지 확인: make dev",
        )

    if isinstance(data, dict) and data.get("status") != "ok":
        _die(
            "헬스체크 응답에 'status: ok'가 없습니다.",
            hint=f"실제 응답: {data}",
        )

    _ok(f"헬스체크 통과 (HTTP {status})")
    if isinstance(data, dict):
        _info(f"응답: {json.dumps(data, ensure_ascii=False)}")

    ready_url = f"{cfg.base_url}/ready"
    _info(f"GET {ready_url}")
    status, data = http_get(ready_url, timeout=cfg.timeout, allow_error=True)
    if status != 200:
        _die(
            f"레디니스 체크 실패: HTTP {status}",
            hint=f"응답: {str(data)[:300]}\n"
            "       postgres/redis/mailpit 컨테이너가 healthy인지 확인: make infra-health",
        )

    if not isinstance(data, dict):
        _die(
            "레디니스 응답이 JSON 객체가 아닙니다.",
            hint=f"실제 응답: {str(data)[:200]}",
        )

    required_checks = ("postgres", "redis", "mailpit")
    failed = {name: data.get(name) for name in required_checks if data.get(name) != "ok"}
    if data.get("status") != "ready" or failed:
        _die(
            "로컬 인프라 연결 검증에 실패했습니다.",
            hint=f"실제 응답: {json.dumps(data, ensure_ascii=False)}\n"
            "       FastAPI 서버는 host에서 실행되어야 하며, docker-compose의 "
            "postgres/redis/mailpit 포트가 localhost로 노출되어야 합니다.",
        )

    _ok("PostgreSQL / Redis / Mailpit 연결 검증 통과")
    _info(f"응답: {json.dumps(data, ensure_ascii=False)}")


def step_register(cfg: SmokeTestConfig, state: TestState) -> None:
    """POST /api/v1/auth/signup → 201 and trigger a Mailpit verification email."""
    _section("단계 2 — 회원가입")

    # 중복 없는 테스트용 이메일
    suffix = secrets.token_hex(6)
    state.email = f"smoke_test_{suffix}@example.com"
    state.password = f"SmokeT3st!{suffix}"

    url = f"{cfg.base_url}/api/v1/auth/signup"
    _info(f"POST {url}")
    _info(f"이메일: {state.email}")

    status, data = http_post(
        url,
        body={
            "email": state.email,
            "password": state.password,
            "display_name": f"Smoke Tester {suffix}",
        },
        timeout=cfg.timeout,
        allow_error=True,
    )

    if status not in (200, 201):
        _die(
            f"회원가입 실패: HTTP {status}",
            hint=f"응답: {str(data)[:300]}",
        )

    _ok(f"회원가입 성공 (HTTP {status})")
    if isinstance(data, dict):
        state.user_id = str(data.get("id", data.get("user_id", "")))
        _info(f"사용자 ID: {state.user_id or '(응답에서 미확인)'}")


def step_verify_email(cfg: SmokeTestConfig, state: TestState) -> None:
    """Mailpit API → token extraction → POST /api/v1/auth/verify-email/{token}."""
    if cfg.skip_email_verify:
        _section("단계 3 — 이메일 인증 (건너뜀)")
        _info("--skip-email-verify 플래그로 건너뜁니다.")
        return

    _section("단계 3 — 이메일 인증")
    _info(f"Mailpit에서 {state.email} 수신 이메일 대기 중...")

    # Mailpit에서 인증 이메일 대기
    msg = mailpit_wait_for_email(
        cfg.mailpit_url,
        state.email,
        timeout=20,
    )
    message_id: str = msg.get("ID", "")
    _ok(f"이메일 수신 확인 (ID: {message_id})")

    # 메시지 본문에서 토큰 추출
    body = mailpit_get_message_body(cfg.mailpit_url, message_id)
    token = extract_verification_token(body, cfg.base_url)
    _ok(f"인증 토큰 추출 성공 (토큰 앞 8자: {token[:8]}...)")

    # 인증 엔드포인트 호출 — generated router exposes POST /verify-email/{token}
    verify_url = f"{cfg.base_url}/api/v1/auth/verify-email/{token}"
    _info("POST /api/v1/auth/verify-email/<TOKEN>")

    status, data = http_request(
        verify_url,
        method="POST",
        timeout=cfg.timeout,
        allow_error=True,
    )

    if status not in (200, 201, 204):
        _die(
            f"이메일 인증 실패: HTTP {status}",
            hint=f"응답: {str(data)[:300]}\n       --skip-email-verify 로 이 단계를 건너뛸 수 있습니다.",
        )

    _ok(f"이메일 인증 성공 (HTTP {status})")


def step_login(cfg: SmokeTestConfig, state: TestState) -> None:
    """POST /api/v1/auth/login → access_token, refresh_token"""
    _section("단계 4 — 로그인 (JWT 토큰 발급)")
    url = f"{cfg.base_url}/api/v1/auth/login"
    _info(f"POST {url}")

    status, data = http_post(
        url,
        body={"email": state.email, "password": state.password},
        timeout=cfg.timeout,
        allow_error=True,
    )

    if status not in (200, 201):
        _die(
            f"로그인 실패: HTTP {status}",
            hint=f"응답: {str(data)[:300]}",
        )

    if not isinstance(data, dict):
        _die(
            "로그인 응답이 JSON 객체가 아닙니다.",
            hint=f"실제 응답: {str(data)[:200]}",
        )

    # access_token 추출
    state.access_token = str(data.get("access_token", data.get("accessToken", "")))
    if not state.access_token:
        _die(
            "로그인 응답에 access_token이 없습니다.",
            hint=f"실제 응답 키: {list(data.keys())}",
        )

    # refresh_token 추출
    state.refresh_token = str(data.get("refresh_token", data.get("refreshToken", "")))
    if not state.refresh_token:
        _die(
            "로그인 응답에 refresh_token이 없습니다.",
            hint=f"실제 응답 키: {list(data.keys())}",
        )

    _ok(f"로그인 성공 (HTTP {status})")
    _info(f"access_token  : ...{state.access_token[-12:]}")
    _info(f"refresh_token : ...{state.refresh_token[-12:]}")


def step_protected_endpoint(cfg: SmokeTestConfig, state: TestState) -> None:
    """GET /api/v1/auth/me — Bearer 토큰으로 보호된 엔드포인트 접근"""
    _section("단계 5 — 보호된 엔드포인트 접근 (GET /api/v1/auth/me)")
    url = f"{cfg.base_url}/api/v1/auth/me"
    _info(f"GET {url}")
    _info("헤더: Authorization: Bearer <access_token>")

    status, data = http_get(
        url,
        headers=bearer_headers(state.access_token),
        timeout=cfg.timeout,
        allow_error=True,
    )

    if status != 200:
        _die(
            f"보호된 엔드포인트 접근 실패: HTTP {status}",
            hint=f"응답: {str(data)[:300]}",
        )

    if not isinstance(data, dict):
        _die(
            "/api/v1/auth/me 응답이 JSON 객체가 아닙니다.",
            hint=f"실제 응답: {str(data)[:200]}",
        )

    # 이메일 일치 확인
    resp_email = str(data.get("email", ""))
    if resp_email and resp_email.lower() != state.email.lower():
        _die(
            f"me 엔드포인트 이메일 불일치: 예상={state.email}, 실제={resp_email}",
        )

    _ok(f"보호된 엔드포인트 접근 성공 (HTTP {status})")
    _info(f"이메일 확인: {resp_email or '(응답에 email 필드 없음)'}")


def step_refresh_token(cfg: SmokeTestConfig, state: TestState) -> None:
    """POST /api/v1/auth/refresh — refresh rotation 검증"""
    _section("단계 6 — 토큰 갱신 (Refresh Rotation)")
    url = f"{cfg.base_url}/api/v1/auth/refresh"
    _info(f"POST {url}")

    status, data = http_post(
        url,
        body={"refresh_token": state.refresh_token},
        timeout=cfg.timeout,
        allow_error=True,
    )

    if status not in (200, 201):
        _die(
            f"토큰 갱신 실패: HTTP {status}",
            hint=f"응답: {str(data)[:300]}",
        )

    if not isinstance(data, dict):
        _die(
            "토큰 갱신 응답이 JSON 객체가 아닙니다.",
            hint=f"실제 응답: {str(data)[:200]}",
        )

    new_access = str(data.get("access_token", data.get("accessToken", "")))
    new_refresh = str(data.get("refresh_token", data.get("refreshToken", "")))

    if not new_access:
        _die(
            "토큰 갱신 응답에 새 access_token이 없습니다.",
            hint=f"실제 응답 키: {list(data.keys())}",
        )

    # 갱신된 토큰으로 상태 업데이트
    state.access_token = new_access
    if new_refresh:
        state.refresh_token = new_refresh

    _ok(f"토큰 갱신 성공 (HTTP {status})")
    _info(f"새 access_token  : ...{state.access_token[-12:]}")
    _info(f"새 refresh_token : ...{state.refresh_token[-12:] if new_refresh else '(미변경)'}")


def step_logout(cfg: SmokeTestConfig, state: TestState) -> None:
    """POST /api/v1/auth/logout — JWT blacklist 등록"""
    _section("단계 7 — 로그아웃")
    url = f"{cfg.base_url}/api/v1/auth/logout"
    _info(f"POST {url}")

    status, data = http_post(
        url,
        body={"refresh_token": state.refresh_token},
        headers=bearer_headers(state.access_token),
        timeout=cfg.timeout,
        allow_error=True,
    )

    if status not in (200, 201, 204):
        _die(
            f"로그아웃 실패: HTTP {status}",
            hint=f"응답: {str(data)[:300]}",
        )

    _ok(f"로그아웃 성공 (HTTP {status})")


def step_post_logout_blocked(cfg: SmokeTestConfig, state: TestState) -> None:
    """GET /api/v1/auth/me — 로그아웃 후 401 확인"""
    _section("단계 8 — 로그아웃 후 접근 차단 검증 (401 기대)")
    url = f"{cfg.base_url}/api/v1/auth/me"
    _info(f"GET {url}  (만료된 토큰 사용)")

    status, data = http_get(
        url,
        headers=bearer_headers(state.access_token),
        timeout=cfg.timeout,
        allow_error=True,
    )

    if status not in (401, 403):
        _die(
            f"로그아웃 후 보호된 엔드포인트가 {status}를 반환했습니다. 401 또는 403이 기대됩니다.",
            hint=f"응답: {str(data)[:300]}\n"
            "       JWT blacklist가 올바르게 작동하지 않을 수 있습니다.",
        )

    _ok(f"로그아웃 후 접근 차단 확인 (HTTP {status})")


def step_create_conversation(cfg: SmokeTestConfig, state: TestState) -> None:
    """POST /api/v1/chat/conversations — 대화 생성"""
    _section("단계 9 — 대화 생성")
    url = f"{cfg.base_url}/api/v1/chat/conversations"
    _info(f"POST {url}")

    status, data = http_post(
        url,
        body={
            "title": "스모크 테스트 대화",
            "system_prompt": "You are a helpful assistant.",
        },
        headers=bearer_headers(state.access_token),
        timeout=cfg.timeout,
        allow_error=True,
    )

    if status not in (200, 201):
        _die(
            f"대화 생성 실패: HTTP {status}",
            hint=f"응답: {str(data)[:300]}",
        )

    if not isinstance(data, dict):
        _die(
            "대화 생성 응답이 JSON 객체가 아닙니다.",
            hint=f"실제 응답: {str(data)[:200]}",
        )

    state.conversation_id = str(data.get("id", data.get("conversation_id", "")))
    if not state.conversation_id:
        _die(
            "대화 생성 응답에 id가 없습니다.",
            hint=f"실제 응답 키: {list(data.keys())}",
        )

    _ok(f"대화 생성 성공 (HTTP {status})")
    _info(f"대화 ID: {state.conversation_id}")


def step_chat_streaming(cfg: SmokeTestConfig, state: TestState) -> None:
    """POST /api/v1/chat/conversations/{id}/messages — SSE 스트리밍 검증"""
    _section("단계 10 — LLM 채팅 SSE 스트리밍")
    url = f"{cfg.base_url}/api/v1/chat/conversations/{state.conversation_id}/messages"
    _info(f"POST {url}  (SSE 스트리밍 기대)")

    chunks = read_sse_chunks(
        url,
        body={"content": "안녕하세요! 간단히 인사해주세요."},
        headers={
            **bearer_headers(state.access_token),
            "Accept": "text/event-stream",
        },
        timeout=30,
        max_chunks=3,
    )

    if not chunks:
        _die(
            "SSE 스트리밍 응답에서 data 청크를 받지 못했습니다.",
            hint="LLM API 키 설정을 확인하세요 (.env 파일의 OPENAI_API_KEY 등).\n"
            "       또는 --skip-chat 플래그로 이 단계를 건너뛸 수 있습니다.",
        )

    _ok(f"SSE 스트리밍 성공 ({len(chunks)}개 청크 수신)")
    for i, chunk in enumerate(chunks[:3], 1):
        preview = chunk[:60] + ("..." if len(chunk) > 60 else "")
        _info(f"  청크 {i}: {preview}")


# ── 메인 ─────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FastAPI Bootstrap — 핵심 API 스모크 테스트",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="FastAPI 서버 호스트 (기본값: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="FastAPI 서버 포트 (기본값: 8000)",
    )
    parser.add_argument(
        "--mailpit-host",
        default="localhost",
        help="Mailpit 호스트 (기본값: localhost)",
    )
    parser.add_argument(
        "--mailpit-port",
        type=int,
        default=8025,
        help="Mailpit UI 포트 (기본값: 8025)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="각 HTTP 요청의 타임아웃 (초, 기본값: 15)",
    )
    parser.add_argument(
        "--skip-email-verify",
        action="store_true",
        help="이메일 인증 단계 건너뛰기 (Mailpit 미사용 환경)",
    )
    parser.add_argument(
        "--skip-chat",
        action="store_true",
        help="채팅 도메인 테스트 건너뛰기 (LLM API 키 미설정 환경)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # fastapi_host가 0.0.0.0이면 localhost로 변환
    host = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    base_url = f"http://{host}:{args.port}"
    mailpit_url = f"http://{args.mailpit_host}:{args.mailpit_port}"

    cfg = SmokeTestConfig(
        base_url=base_url,
        mailpit_url=mailpit_url,
        timeout=args.timeout,
        skip_email_verify=args.skip_email_verify,
        skip_chat=args.skip_chat,
    )
    state = TestState()

    # 헤더 출력
    W = 56
    sep = "═" * W
    print(f"\n{_c(sep, '1;36')}")
    print(_c("  FastAPI Bootstrap — API 스모크 테스트", "1;32"))
    print(_c(sep, "1;36"))
    print(f"  서버        : {_c(base_url, '36')}")
    print(f"  Mailpit     : {_c(mailpit_url, '36')}")
    print(f"  이메일 인증  : {_c('건너뜀', '33') if cfg.skip_email_verify else _c('활성화', '32')}")
    print(f"  채팅 테스트  : {_c('건너뜀', '33') if cfg.skip_chat else _c('활성화', '32')}")

    # ── 테스트 실행 ─────────────────────────────────────────────────────────

    # 1. 헬스체크
    step_health_check(cfg)

    # 2. 회원가입
    step_register(cfg, state)

    # 3. 이메일 인증
    step_verify_email(cfg, state)

    # 4. 로그인
    step_login(cfg, state)

    # 5. 보호된 엔드포인트
    step_protected_endpoint(cfg, state)

    # 6. 토큰 갱신
    step_refresh_token(cfg, state)

    # 7. 로그아웃
    step_logout(cfg, state)

    # 8. 로그아웃 후 차단 확인
    step_post_logout_blocked(cfg, state)

    # 9-10. 채팅 도메인 (조건부)

    if not cfg.skip_chat:
        step_create_conversation(cfg, state)
        step_chat_streaming(cfg, state)
    else:
        _section("단계 9-10 — 채팅 도메인 (건너뜀)")
        _info("--skip-chat 플래그로 건너뜁니다.")

    # ── 최종 요약 ────────────────────────────────────────────────────────────
    print(f"\n{_c(sep, '1;36')}")
    print(_c("  ✅  모든 스모크 테스트를 통과했습니다!", "1;32"))
    print(_c(sep, "1;36"))
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
