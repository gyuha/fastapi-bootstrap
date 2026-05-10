#!/usr/bin/env bash
# ============================================================
# scripts/validate_prod_image.sh — Production Docker Image 검증
# ============================================================
# cookiecutter 템플릿에서 프로젝트를 생성하고 --target runtime 으로
# Docker 이미지를 빌드한 뒤, 컨테이너 내부에서 dev 전용 패키지
# (pytest, ruff, mypy, black 등)가 설치되어 있지 않음을 확인합니다.
# 동시에 런타임에 필요한 패키지(fastapi, uvicorn, sqlalchemy 등)가
# 정상적으로 설치되어 있는지도 검증합니다.
#
# 사용법:
#   scripts/validate_prod_image.sh [OPTIONS]
#
# 옵션:
#   --project-dir DIR    이미 생성된 프로젝트 디렉터리 사용 (cookiecutter 생략)
#   --image-tag TAG      사용할 Docker 이미지 태그 (기본값: validate-prod-image:test-<PID>)
#   --no-cleanup         테스트 후 이미지/임시 디렉터리 유지 (디버깅용)
#   --help, -h           도움말 출력
#
# 종료 코드:
#   0  모든 검사 통과
#   1  하나 이상의 검사 실패 또는 빌드 실패
#   2  사전 요구사항 누락 (docker 없음 등)
#
# 예시:
#   # 기본 실행 (임시 디렉터리에 프로젝트 생성 + 이미지 빌드 + 검증 + 자동 정리)
#   bash scripts/validate_prod_image.sh
#
#   # 이미 생성된 프로젝트 디렉터리를 사용
#   bash scripts/validate_prod_image.sh --project-dir /tmp/my-fastapi-project
#
#   # 이미지와 임시 파일을 유지하며 실행 (디버깅)
#   bash scripts/validate_prod_image.sh --no-cleanup
#
#   # 커스텀 이미지 태그
#   bash scripts/validate_prod_image.sh --image-tag myapp:prod-test
# ============================================================

set -euo pipefail

# ── 경로 설정 ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── 기본값 ───────────────────────────────────────────────────────────────────
IMAGE_TAG="validate-prod-image:test-$$"
PROJECT_DIR=""
CLEANUP=true
TMP_DIR=""

# ── 색상 (TTY 아닐 때 비활성화) ──────────────────────────────────────────────
if [[ -t 1 ]]; then
  RED='\033[1;31m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'
  CYAN='\033[1;36m'; BOLD='\033[1m'; DIM='\033[90m'; RESET='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; DIM=''; RESET=''
fi

# ── 로그 헬퍼 ────────────────────────────────────────────────────────────────
log_info()    { echo -e "${DIM}[INFO]${RESET}  $*"; }
log_ok()      { echo -e "${GREEN}[ OK ]${RESET}  $*"; }
log_fail()    { echo -e "${RED}[FAIL]${RESET}  $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
log_section() {
  echo
  echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════════${RESET}"
  echo -e "${CYAN}${BOLD}  $*${RESET}"
  echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════════${RESET}"
}
log_divider() {
  echo -e "${DIM}──────────────────────────────────────────────────────────${RESET}"
}

# ── 인수 파싱 ────────────────────────────────────────────────────────────────
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --project-dir)
        if [[ -z "${2:-}" ]]; then
          echo "오류: --project-dir에 디렉터리 경로가 필요합니다." >&2
          exit 2
        fi
        PROJECT_DIR="$(realpath "$2")"
        shift 2
        ;;
      --image-tag)
        if [[ -z "${2:-}" ]]; then
          echo "오류: --image-tag에 태그 값이 필요합니다." >&2
          exit 2
        fi
        IMAGE_TAG="$2"
        shift 2
        ;;
      --no-cleanup)
        CLEANUP=false
        shift
        ;;
      --help|-h)
        # 헤더 주석 추출 (첫 번째 비-주석 줄 이전까지)
        sed -n '/^#!/d; /^# ====/,/^# ====/{p}' "$0" | sed 's/^# \?//'
        exit 0
        ;;
      *)
        echo "알 수 없는 옵션: $1" >&2
        echo "사용법: $0 [--project-dir DIR] [--image-tag TAG] [--no-cleanup] [--help]" >&2
        exit 2
        ;;
    esac
  done
}

# ── 종료 시 정리 ─────────────────────────────────────────────────────────────
_cleanup() {
  local exit_code=$?
  echo  # 줄바꿈 보장
  if [[ "${CLEANUP}" == true ]]; then
    if [[ -n "${IMAGE_TAG:-}" ]]; then
      log_info "Docker 이미지 삭제 중: ${IMAGE_TAG}"
      docker image rm -f "${IMAGE_TAG}" 2>/dev/null || true
    fi
    if [[ -n "${TMP_DIR:-}" && -d "${TMP_DIR:-}" ]]; then
      log_info "임시 디렉터리 삭제 중: ${TMP_DIR}"
      rm -rf "${TMP_DIR}"
    fi
  else
    if [[ -n "${IMAGE_TAG:-}" ]]; then
      log_info "(--no-cleanup) Docker 이미지 보존됨: ${IMAGE_TAG}"
      log_info "  검사 명령: docker run --rm -e VIRTUAL_ENV=/runtime-venv ${IMAGE_TAG} uv pip list"
    fi
    if [[ -n "${TMP_DIR:-}" && -d "${TMP_DIR:-}" ]]; then
      log_info "(--no-cleanup) 임시 프로젝트 보존됨: ${TMP_DIR}"
    fi
  fi
  return $exit_code
}
trap _cleanup EXIT

# ── 사전 요구사항 확인 ────────────────────────────────────────────────────────
check_prerequisites() {
  log_section "사전 요구사항 확인"
  local missing=0

  if ! command -v docker &>/dev/null; then
    log_fail "docker를 찾을 수 없습니다."
    log_info "  설치: https://docs.docker.com/get-docker/"
    missing=1
  else
    local docker_version
    docker_version="$(docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo '?')"
    log_ok "docker ${docker_version}"
  fi

  # Docker 데몬 실행 여부 확인
  if command -v docker &>/dev/null && ! docker info &>/dev/null 2>&1; then
    log_fail "Docker 데몬이 실행 중이지 않습니다."
    log_info "  macOS: Docker Desktop을 시작하세요."
    log_info "  Linux: sudo systemctl start docker"
    missing=1
  fi

  if [[ $missing -ne 0 ]]; then
    log_fail "사전 요구사항이 충족되지 않았습니다."
    exit 2
  fi

  log_ok "모든 사전 요구사항 충족"
}

# ── cookiecutter로 프로젝트 생성 ──────────────────────────────────────────────
generate_project() {
  log_section "cookiecutter 프로젝트 생성"

  TMP_DIR="$(mktemp -d /tmp/cc_prod_validate_XXXXXX)"
  log_info "임시 디렉터리: ${TMP_DIR}"
  log_info "템플릿 위치:   ${TEMPLATE_DIR}"

  # cookiecutter 실행 방법 탐색 (Python API → uvx → PATH)
  if python3 -c "import cookiecutter" 2>/dev/null; then
    log_info "Python API로 cookiecutter 실행 중..."
    COOKIECUTTER_SKIP_HEAVY_OPS=1 python3 - <<PYEOF
import sys, os
# 환경변수가 이미 설정되어 있으므로 그대로 진행
from cookiecutter.main import cookiecutter
result = cookiecutter(
    template="${TEMPLATE_DIR}",
    no_input=True,
    extra_context={},
    output_dir="${TMP_DIR}",
    overwrite_if_exists=True,
)
print(f"생성된 프로젝트: {result}")
PYEOF

  elif command -v uvx &>/dev/null; then
    log_info "uvx cookiecutter로 실행 중..."
    COOKIECUTTER_SKIP_HEAVY_OPS=1 uvx cookiecutter \
      --no-input \
      --output-dir "${TMP_DIR}" \
      "${TEMPLATE_DIR}"

  elif command -v cookiecutter &>/dev/null; then
    log_info "cookiecutter CLI로 실행 중..."
    COOKIECUTTER_SKIP_HEAVY_OPS=1 cookiecutter \
      --no-input \
      --output-dir "${TMP_DIR}" \
      "${TEMPLATE_DIR}"

  else
    log_fail "cookiecutter를 찾을 수 없습니다."
    log_info "  설치: pip install cookiecutter"
    log_info "  또는 uv가 설치된 경우 uvx를 통해 자동으로 사용됩니다."
    exit 2
  fi

  # 생성된 프로젝트 디렉터리 탐색
  PROJECT_DIR="$(find "${TMP_DIR}" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort | head -1 || true)"
  if [[ -z "${PROJECT_DIR}" ]]; then
    log_fail "생성된 프로젝트 디렉터리를 찾을 수 없습니다: ${TMP_DIR}"
    exit 1
  fi

  log_ok "프로젝트 생성 완료: $(basename "${PROJECT_DIR}")"
  log_info "  경로: ${PROJECT_DIR}"
}

# ── Docker 이미지 빌드 ────────────────────────────────────────────────────────
build_image() {
  log_section "Docker 이미지 빌드 (--target runtime)"
  log_info "이미지 태그:       ${IMAGE_TAG}"
  log_info "빌드 컨텍스트:     ${PROJECT_DIR}"
  log_info "Dockerfile:        ${PROJECT_DIR}/Dockerfile"
  log_divider
  log_info "빌드 시작 (패키지 다운로드로 수 분 소요될 수 있습니다)..."
  echo

  if ! docker build \
      --target runtime \
      --tag "${IMAGE_TAG}" \
      --file "${PROJECT_DIR}/Dockerfile" \
      "${PROJECT_DIR}"; then
    log_fail "Docker 이미지 빌드 실패"
    log_info "  전체 빌드 로그 확인:"
    log_info "    docker build --target runtime --progress=plain -t dbg:test ."
    exit 1
  fi

  echo
  log_ok "Docker 이미지 빌드 완료: ${IMAGE_TAG}"

  # 이미지 크기 출력
  local image_size
  image_size="$(docker image inspect "${IMAGE_TAG}" --format='{{.Size}}' 2>/dev/null || echo '?')"
  if [[ "${image_size}" != "?" ]]; then
    local size_mb
    size_mb=$(( image_size / 1024 / 1024 ))
    log_info "  이미지 크기: ${size_mb} MB"
  fi
}

# ── 컨테이너 내 패키지 설치 여부 확인 ───────────────────────────────────────
# 사용법: pkg_is_installed <distribution-name>
# 반환:   0 = 설치됨,  1 = 설치 안 됨
# 설명:   런타임 이미지의 /runtime-venv 에서 uv pip show 를 사용하여
#         패키지 존재 여부를 확인합니다. uv는 runtime 이미지에 포함되어 있습니다.
pkg_is_installed() {
  local pkg="$1"
  # VIRTUAL_ENV=/runtime-venv 는 이미 이미지 ENV에 설정되어 있으나,
  # 명시적으로 전달하여 환경 독립성을 보장합니다.
  docker run --rm \
    -e VIRTUAL_ENV=/runtime-venv \
    "${IMAGE_TAG}" \
    uv pip show "${pkg}" >/dev/null 2>&1
}

# ── absent 검사: dev 전용 패키지가 없어야 함 ─────────────────────────────────
check_absent() {
  local pkg="$1"
  local description="${2:-}"

  local label="${pkg}"
  if [[ -n "${description}" ]]; then
    label="${pkg}  ${DIM}(${description})${RESET}"
  fi

  if pkg_is_installed "${pkg}"; then
    log_fail "dev 패키지가 런타임 이미지에 존재함: ${label}"
    return 1
  else
    log_ok "absent: ${label}"
    return 0
  fi
}

# ── present 검사: 런타임 패키지가 있어야 함 ──────────────────────────────────
check_present() {
  local pkg="$1"
  local description="${2:-}"

  local label="${pkg}"
  if [[ -n "${description}" ]]; then
    label="${pkg}  ${DIM}(${description})${RESET}"
  fi

  if pkg_is_installed "${pkg}"; then
    log_ok "present: ${label}"
    return 0
  else
    log_fail "런타임 패키지가 없음: ${label}"
    return 1
  fi
}

# ── 패키지 목록 전체 출력 (--no-cleanup 디버깅 모드에서만) ───────────────────
dump_installed_packages() {
  log_section "런타임 이미지 설치 패키지 전체 목록 (디버깅)"
  docker run --rm \
    -e VIRTUAL_ENV=/runtime-venv \
    "${IMAGE_TAG}" \
    uv pip list 2>/dev/null || {
      log_warn "패키지 목록 출력 실패 (uv pip list)"
    }
}

# ── 메인 ─────────────────────────────────────────────────────────────────────
main() {
  parse_args "$@"

  echo
  echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════════${RESET}"
  echo -e "${CYAN}${BOLD}  Production Docker Image 검증 스크립트${RESET}"
  echo -e "${CYAN}${BOLD}  템플릿: ${TEMPLATE_DIR}${RESET}"
  echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════════${RESET}"

  # ── 1. 사전 요구사항 확인 ───────────────────────────────────────────────
  check_prerequisites

  # ── 2. 프로젝트 디렉터리 결정 ──────────────────────────────────────────
  if [[ -n "${PROJECT_DIR}" ]]; then
    log_section "기존 프로젝트 사용"
    log_info "경로: ${PROJECT_DIR}"
    if [[ ! -f "${PROJECT_DIR}/Dockerfile" ]]; then
      log_fail "Dockerfile을 찾을 수 없습니다: ${PROJECT_DIR}/Dockerfile"
      exit 1
    fi
    log_ok "Dockerfile 존재 확인"
  else
    generate_project
  fi

  # ── 3. Docker 이미지 빌드 ───────────────────────────────────────────────
  build_image

  # ── 4. 검사 실행 ────────────────────────────────────────────────────────
  local passed=0
  local failed=0

  # ─────────────────────────────────────────────────────────────────────────
  # 검사 A: dev 전용 패키지 absent (런타임 이미지에 없어야 함)
  # 이 패키지들은 pyproject.toml의 [dependency-groups.dev] 에만 선언되며
  # `uv sync --no-group dev` 명령으로 /runtime-venv 에서 제외됩니다.
  # ─────────────────────────────────────────────────────────────────────────
  log_section "검사 A: dev 전용 패키지 absent (없어야 함)"

  # 패키지명(distribution name) + 설명
  declare -a DEV_PACKAGES=(
    "pytest:테스트 프레임워크"
    "pytest-asyncio:비동기 테스트 플러그인"
    "pytest-cov:커버리지 플러그인"
    "ruff:linter + formatter"
    "mypy:정적 타입 체커"
    "pre-commit:git 훅 관리자"
    "fakeredis:Redis 인메모리 테스트 스텁"
    "types-passlib:passlib 타입 스텁"
    "types-python-jose:python-jose 타입 스텁"
    "black:Python 코드 포매터 (의존성에 없으나 검증 포함)"
  )

  for entry in "${DEV_PACKAGES[@]}"; do
    local pkg="${entry%%:*}"
    local desc="${entry##*:}"
    if check_absent "${pkg}" "${desc}"; then
      passed=$(( passed + 1 ))
    else
      failed=$(( failed + 1 ))
    fi
  done

  # ─────────────────────────────────────────────────────────────────────────
  # 검사 B: 런타임 패키지 present (있어야 함)
  # 이 패키지들은 pyproject.toml의 [project].dependencies 에 선언되며
  # `uv sync --no-group dev` 명령으로 /runtime-venv 에 설치됩니다.
  # ─────────────────────────────────────────────────────────────────────────
  log_section "검사 B: 런타임 패키지 present (있어야 함)"

  declare -a RUNTIME_PACKAGES=(
    "fastapi:웹 프레임워크"
    "uvicorn:ASGI 서버"
    "SQLAlchemy:ORM (async)"
    "alembic:DB 마이그레이션"
    "passlib:비밀번호 해시 (argon2)"
    "argon2-cffi:Argon2 C 확장"
    "python-jose:JWT 인코딩/디코딩"
    "redis:Redis 클라이언트 (hiredis)"
    "httpx:HTTP 클라이언트 (OAuth 플로우)"
    "structlog:구조화 JSON 로깅"
    "sse-starlette:SSE 스트리밍"
    "pydantic:데이터 검증"
    "pydantic-settings:설정 관리"
    "asyncpg:Async PostgreSQL 드라이버"
    "fastapi-mail:이메일 전송"
  )

  for entry in "${RUNTIME_PACKAGES[@]}"; do
    local pkg="${entry%%:*}"
    local desc="${entry##*:}"
    if check_present "${pkg}" "${desc}"; then
      passed=$(( passed + 1 ))
    else
      failed=$(( failed + 1 ))
    fi
  done

  # ─────────────────────────────────────────────────────────────────────────
  # 검사 C: 런타임 진입점 확인
  # uvicorn 바이너리가 /runtime-venv/bin/uvicorn 에 존재해야 합니다.
  # ─────────────────────────────────────────────────────────────────────────
  log_section "검사 C: 런타임 진입점 확인"

  log_info "uvicorn 진입점 확인 중..."
  if docker run --rm "${IMAGE_TAG}" \
      /runtime-venv/bin/python -m uvicorn --version >/dev/null 2>&1; then
    log_ok "uvicorn 진입점 정상: /runtime-venv/bin/python -m uvicorn"
    passed=$(( passed + 1 ))
  else
    log_fail "uvicorn 진입점 실패: /runtime-venv/bin/python -m uvicorn --version"
    failed=$(( failed + 1 ))
  fi

  log_info "alembic 진입점 확인 중..."
  if docker run --rm "${IMAGE_TAG}" \
      /runtime-venv/bin/alembic --version >/dev/null 2>&1; then
    log_ok "alembic 진입점 정상: /runtime-venv/bin/alembic"
    passed=$(( passed + 1 ))
  else
    log_fail "alembic 진입점 실패: /runtime-venv/bin/alembic --version"
    failed=$(( failed + 1 ))
  fi

  # ── 5. 디버깅 모드: 설치 패키지 전체 목록 출력 ─────────────────────────
  if [[ "${CLEANUP}" == false ]]; then
    dump_installed_packages
  fi

  # ── 6. 최종 결과 ────────────────────────────────────────────────────────
  local total=$(( passed + failed ))
  log_section "최종 결과"
  log_divider
  echo -e "  총 검사:  ${BOLD}${total}${RESET}건"
  echo -e "  통과:     ${GREEN}${BOLD}${passed}${RESET}건"
  echo -e "  실패:     ${RED}${BOLD}${failed}${RESET}건"
  log_divider

  if [[ $failed -eq 0 ]]; then
    echo
    echo -e "  ${GREEN}${BOLD}PASS${RESET}  프로덕션 이미지가 올바르게 빌드되었습니다."
    echo -e "        dev 전용 패키지가 포함되지 않았고,"
    echo -e "        런타임 패키지가 모두 존재합니다."
    exit 0
  else
    echo
    echo -e "  ${RED}${BOLD}FAIL${RESET}  ${failed}건의 검사가 실패했습니다."
    echo -e "        위 로그에서 FAIL 항목을 확인하세요."
    echo
    echo -e "  ${DIM}디버깅 팁:${RESET}"
    echo -e "    이미지 내 패키지 목록 확인:"
    echo -e "      docker run --rm -e VIRTUAL_ENV=/runtime-venv ${IMAGE_TAG} uv pip list"
    echo -e "    전체 빌드 로그 확인:"
    echo -e "      docker build --target runtime --progress=plain -t dbg:test ${PROJECT_DIR}"
    exit 1
  fi
}

main "$@"
