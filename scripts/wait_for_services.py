#!/usr/bin/env python3
"""
wait_for_services.py — docker-compose 서비스 기동 및 헬스체크 검증 스크립트

생성된 프로젝트 루트 디렉터리에서 실행합니다.

동작:
    1. docker compose up -d 실행
    2. 각 서비스(postgres, redis, mailpit)가 Docker healthy 상태에
       도달할 때까지 폴링
    3. TIMEOUT 내 실패 시 — 실패 컨테이너 로그 출력 후 exit 1
    4. 전체 성공 시 — 서비스 접속 정보 출력 후 exit 0

사용법:
    # 프로젝트 루트에서 실행 (기본 60초 타임아웃)
    python scripts/wait_for_services.py

    # 타임아웃 지정 (초)
    python scripts/wait_for_services.py 120

    # uv를 통해
    uv run python scripts/wait_for_services.py

종료 코드:
    0 — 모든 서비스 healthy
    1 — 타임아웃 또는 서비스 비정상 종료
    2 — 필수 의존성(docker / docker compose) 미설치
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ── 설정 ────────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT = 60  # 기본 헬스체크 폴링 최대 대기 시간 (초)
POLL_INTERVAL = 2  # 폴링 간격 (초)
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "docker-compose.yml")

# 헬스체크 대상 서비스 (healthcheck 블록이 정의된 서비스)
SERVICES_WITH_HEALTHCHECK = ["postgres", "redis", "mailpit"]

HealthStatus = Literal["healthy", "unhealthy", "starting", "none", "exited", "unknown"]

# ── ANSI 색상 ────────────────────────────────────────────────────────────────
_USE_COLOR = (
    sys.platform != "win32" and hasattr(sys.stdout, "fileno") and os.isatty(sys.stdout.fileno())
)


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def log_info(msg: str) -> None:
    print(f"{_c('[INFO]', '36')}  {msg}")


def log_ok(msg: str) -> None:
    print(f"{_c('[OK]', '1;32')}    {msg}")


def log_warn(msg: str) -> None:
    print(f"{_c('[WARN]', '1;33')}  {msg}")


def log_error(msg: str) -> None:
    print(f"{_c('[ERROR]', '1;31')} {msg}", file=sys.stderr)


def log_section(title: str) -> None:
    bar = "═" * 44
    print(f"\n{_c(bar, '1;36')}")
    print(f"{_c(f'  {title}', '1;36')}")
    print(_c(bar, "1;36"))


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


# ── 의존성 확인 ────────────────────────────────────────────────────────────
def find_compose_cmd() -> list[str]:
    """사용 가능한 docker compose 명령을 반환합니다."""
    if not shutil.which("docker"):
        log_error("docker 가 설치되어 있지 않습니다.")
        log_error("  설치: https://docs.docker.com/get-docker/")
        sys.exit(2)

    # docker compose v2 플러그인 확인
    result = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return ["docker", "compose"]

    # docker-compose v1 확인
    if shutil.which("docker-compose"):
        log_warn("docker-compose v1 감지. Docker Compose v2 플러그인으로 업그레이드를 권장합니다.")
        return ["docker-compose"]

    log_error("docker compose 또는 docker-compose 가 설치되어 있지 않습니다.")
    log_error("  설치: https://docs.docker.com/compose/install/")
    sys.exit(2)


def check_compose_file() -> None:
    if not Path(COMPOSE_FILE).exists():
        log_error(f"Compose 파일을 찾을 수 없습니다: {COMPOSE_FILE}")
        log_error("프로젝트 루트 디렉터리에서 이 스크립트를 실행하세요.")
        sys.exit(1)


# ── 컨테이너 헬스 상태 조회 ─────────────────────────────────────────────────
def get_container_health(compose_cmd: list[str], service: str) -> HealthStatus:
    """서비스의 Docker 헬스 상태를 반환합니다."""
    # 컨테이너 ID 조회
    result = subprocess.run(
        [*compose_cmd, "-f", COMPOSE_FILE, "ps", "-q", service],
        capture_output=True,
        text=True,
        check=False,
    )
    container_ids = result.stdout.strip().splitlines()
    if not container_ids:
        return "none"

    container_id = container_ids[0].strip()
    if not container_id:
        return "none"

    # docker inspect로 상태 조회. Docker Go-template 포맷은 생성된 프로젝트에서
    # Jinja 미해결 플레이스홀더처럼 보일 수 있어 원본 JSON을 파싱한다.
    inspect_result = subprocess.run(
        ["docker", "inspect", container_id],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect_result.returncode != 0:
        return "unknown"

    try:
        inspect_payload = json.loads(inspect_result.stdout.strip())
    except json.JSONDecodeError:
        return "unknown"

    if not isinstance(inspect_payload, list) or not inspect_payload:
        return "unknown"

    state = inspect_payload[0].get("State", {})
    if not isinstance(state, dict):
        return "unknown"

    status = state.get("Status", "unknown")
    if status in ("exited", "dead"):
        return "exited"  # type: ignore[return-value]

    # healthcheck 상태
    health = state.get("Health")
    if health is None:
        # healthcheck 미정의 서비스는 running == healthy로 간주
        return "healthy" if status == "running" else "starting"  # type: ignore[return-value]

    health_status = health.get("Status", "unknown")
    return health_status  # type: ignore[return-value]


# ── compose 파일 정의 서비스 목록 조회 ──────────────────────────────────────
def get_defined_services(compose_cmd: list[str]) -> list[str]:
    result = subprocess.run(
        [*compose_cmd, "-f", COMPOSE_FILE, "config", "--services"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [s.strip() for s in result.stdout.strip().splitlines() if s.strip()]


# ── 서비스 로그 덤프 ─────────────────────────────────────────────────────────
def dump_service_logs(compose_cmd: list[str], service: str, lines: int = 100) -> None:
    print(f"\n{_c(f'── {service} 로그 (최근 {lines}줄) ──', '1;33')}")
    subprocess.run(
        [*compose_cmd, "-f", COMPOSE_FILE, "logs", "--tail", str(lines), service],
        check=False,
    )
    print(_c("─" * 44, "1;33"))


# ── docker compose ps 출력 ───────────────────────────────────────────────────
def show_compose_status(compose_cmd: list[str]) -> None:
    log_section("컨테이너 상태")
    subprocess.run(
        [*compose_cmd, "-f", COMPOSE_FILE, "ps"],
        check=False,
    )


# ── 서비스 접속 정보 ─────────────────────────────────────────────────────────
def show_service_endpoints() -> None:
    # .env 파일에서 포트 로드
    env_vars: dict[str, str] = {}
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env_vars[key.strip()] = val.strip()

    pg_port = env_vars.get("POSTGRES_PORT", "5432")
    redis_port = env_vars.get("REDIS_PORT", "6379")
    mailpit_ui = env_vars.get("MAILPIT_UI_PORT", "8025")
    mailpit_smtp = env_vars.get("MAILPIT_SMTP_PORT", "1025")
    app_port = env_vars.get("PORT", "8000")

    log_section("서비스 접속 정보")
    log_ok(f"PostgreSQL : localhost:{pg_port}")
    log_ok(f"Redis      : localhost:{redis_port}")
    log_ok(f"Mailpit    : http://localhost:{mailpit_ui}  (SMTP: {mailpit_smtp})")
    print()
    log_info("다음 단계:")
    log_info("  Alembic 마이그레이션 : make migrate")
    log_info("  FastAPI 개발 서버    : make dev")
    log_info(f"  헬스 엔드포인트      : curl http://localhost:{app_port}/health")


# ── 단일 서비스 헬스체크 대기 ───────────────────────────────────────────────
@dataclass
class WaitResult:
    service: str
    success: bool
    final_status: HealthStatus
    elapsed: float


def wait_for_service(
    compose_cmd: list[str],
    service: str,
    deadline: float,
    start_time: float,
) -> WaitResult:
    """서비스가 healthy 상태에 도달할 때까지 폴링합니다."""
    spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    spin_idx = 0
    last_status: HealthStatus = "unknown"

    while True:
        now = time.monotonic()
        remaining = deadline - now
        elapsed = now - start_time

        if remaining <= 0:
            # 타임아웃
            print(
                f"\r{_c('[TIMEOUT]', '1;31')} {service}: {last_status:<12}  "
                f"({_fmt_elapsed(elapsed)} / {_fmt_elapsed(DEFAULT_TIMEOUT)})  "
            )
            return WaitResult(
                service=service,
                success=False,
                final_status=last_status,
                elapsed=elapsed,
            )

        health = get_container_health(compose_cmd, service)
        last_status = health

        if health == "healthy":
            print(
                f"\r{_c('[OK]', '1;32')}    {service}: {_c('healthy', '1;32'):<22}  "
                f"({_fmt_elapsed(elapsed)})"
            )
            return WaitResult(
                service=service,
                success=True,
                final_status=health,
                elapsed=elapsed,
            )

        if health in ("unhealthy", "exited", "dead"):
            print(
                f"\r{_c('[FAIL]', '1;31')}  {service}: {_c(health, '1;31'):<22}  "
                f"({_fmt_elapsed(elapsed)})"
            )
            return WaitResult(
                service=service,
                success=False,
                final_status=health,
                elapsed=elapsed,
            )

        # 아직 기동 중 (starting / none / unknown)
        spin_char = spinner[spin_idx % len(spinner)]
        spin_idx += 1
        print(
            f"\r{_c('[WAIT]', '1;33')}  {service}: {health:<12}  "
            f"{spin_char}  ({_fmt_elapsed(elapsed)} / {_fmt_elapsed(DEFAULT_TIMEOUT)})   ",
            end="",
            flush=True,
        )
        time.sleep(POLL_INTERVAL)


# ── 메인 ────────────────────────────────────────────────────────────────────
def main() -> None:
    # 타임아웃 인수 파싱
    timeout = DEFAULT_TIMEOUT
    if len(sys.argv) > 1:
        try:
            timeout = int(sys.argv[1])
            if timeout <= 0:
                raise ValueError
        except ValueError:
            log_error(f"유효하지 않은 타임아웃 값: {sys.argv[1]} (양의 정수여야 합니다)")
            sys.exit(1)

    log_section("FastAPI Bootstrap — 서비스 기동 및 헬스체크")
    log_info(f"타임아웃: {timeout}초  |  폴링 간격: {POLL_INTERVAL}초")
    log_info(f"Compose 파일: {COMPOSE_FILE}")

    # 1. 의존성 확인
    compose_cmd = find_compose_cmd()
    log_info(f"Compose 명령: {' '.join(compose_cmd)}")
    check_compose_file()

    # 2. docker compose up -d
    log_section("컨테이너 기동")
    log_info(f"{' '.join(compose_cmd)} -f {COMPOSE_FILE} up -d")
    result = subprocess.run(
        [*compose_cmd, "-f", COMPOSE_FILE, "up", "-d"],
        check=False,
    )
    if result.returncode != 0:
        log_error("docker compose up -d 실패")
        sys.exit(1)

    # 3. 정의된 서비스 목록 조회 및 필터링
    defined_services = get_defined_services(compose_cmd)
    target_services = [svc for svc in SERVICES_WITH_HEALTHCHECK if svc in defined_services]
    skipped = [svc for svc in SERVICES_WITH_HEALTHCHECK if svc not in defined_services]
    if skipped:
        log_warn(f"compose 파일에 없는 서비스 (건너뜀): {', '.join(skipped)}")

    if not target_services:
        log_warn("헬스체크 대상 서비스가 없습니다.")
        show_compose_status(compose_cmd)
        sys.exit(0)

    # 4. 각 서비스 헬스체크 폴링
    log_section(f"헬스체크 폴링 (최대 {timeout}초)")

    start_time = time.monotonic()
    deadline = start_time + timeout

    wait_results: list[WaitResult] = []
    for service in target_services:
        print(f"{_c('[WAIT]', '1;33')}  {service}: 폴링 시작...   ", end="", flush=True)
        r = wait_for_service(compose_cmd, service, deadline, start_time)
        wait_results.append(r)

        # 한 서비스가 unhealthy/exited이면 즉시 중단하지 않고 전체 확인
        # (모든 실패 서비스의 로그를 한 번에 출력하기 위해)

    # 5. 결과 판정
    show_compose_status(compose_cmd)

    total_elapsed = time.monotonic() - start_time
    failed = [r for r in wait_results if not r.success]
    succeeded = [r for r in wait_results if r.success]

    if failed:
        print()
        log_error(f"{timeout}초 내 healthy 상태에 도달하지 못한 서비스:")
        for r in failed:
            log_error(f"  ✗ {r.service}  (최종 상태: {r.final_status})")
        if succeeded:
            log_info(f"성공한 서비스: {', '.join(r.service for r in succeeded)}")
        log_info(f"총 경과 시간: {_fmt_elapsed(total_elapsed)}")

        # 실패 서비스 로그 덤프
        log_section("실패한 서비스 로그")
        for r in failed:
            dump_service_logs(compose_cmd, r.service, lines=100)

        print()
        log_error("서비스 기동에 실패했습니다. 위 로그를 확인하세요.")
        log_info(f"전체 로그 확인: {' '.join(compose_cmd)} -f {COMPOSE_FILE} logs")
        log_info(f"컨테이너 정리: {' '.join(compose_cmd)} -f {COMPOSE_FILE} down")
        sys.exit(1)

    # 6. 전체 성공
    print()
    log_ok("모든 서비스가 healthy 상태입니다 ✓")
    log_info(f"성공한 서비스: {', '.join(r.service for r in succeeded)}")
    log_info(f"총 경과 시간: {_fmt_elapsed(total_elapsed)}")

    show_service_endpoints()


if __name__ == "__main__":
    main()
