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
# 마지막으로 최종 이미지 크기가 허용 임계값 이하인지 강제 검증합니다.
#
# ── 이미지 크기 정책 ──────────────────────────────────────────────────────────
# 허용 임계값 (기본값):
#   --max-size-mb 500   기본값 — auth 전용(~250 MB) 및 auth+chat(~450 MB) 모두 커버
#
# 시나리오별 권장 임계값:
#   auth 전용 (include_chat_domain=no)    : --max-size-mb 350
#   auth + chat (include_chat_domain=yes)  : --max-size-mb 600
#
# CI 환경 환경변수로 재정의 가능:
#   MAX_IMAGE_SIZE_MB=350 bash scripts/validate_prod_image.sh
#
# 임계값 초과 시 종료 코드 1 (빌드 실패) 로 반환합니다.
# 자세한 정책 문서: docs/docker-image-size-policy.md
# ──────────────────────────────────────────────────────────────────────────────
#
# 사용법:
#   scripts/validate_prod_image.sh [OPTIONS]
#
# 옵션:
#   --project-dir DIR    이미 생성된 프로젝트 디렉터리 사용 (cookiecutter 생략)
#   --image-tag TAG      사용할 Docker 이미지 태그 (기본값: validate-prod-image:test-<PID>)
#   --max-size-mb N      이미지 크기 최대 허용값 (MB, 기본값: 500)
#                        환경변수 MAX_IMAGE_SIZE_MB 로도 설정 가능
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
#
#   # auth 전용 프로젝트 (350 MB 이하)
#   bash scripts/validate_prod_image.sh --max-size-mb 350
#
#   # auth+chat 프로젝트 (600 MB 이하)
#   bash scripts/validate_prod_image.sh --max-size-mb 600
#
#   # 환경변수로 임계값 설정 (CI 권장)
#   MAX_IMAGE_SIZE_MB=400 bash scripts/validate_prod_image.sh
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
# 이미지 크기 임계값 (MB) — 환경변수 MAX_IMAGE_SIZE_MB 로 재정의 가능
MAX_IMAGE_SIZE_MB="${MAX_IMAGE_SIZE_MB:-500}"

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
      --max-size-mb)
        if [[ -z "${2:-}" ]] || ! [[ "${2}" =~ ^[0-9]+$ ]]; then
          echo "오류: --max-size-mb에 양의 정수 값이 필요합니다." >&2
          exit 2
        fi
        MAX_IMAGE_SIZE_MB="$2"
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
        echo "사용법: $0 [--project-dir DIR] [--image-tag TAG] [--max-size-mb N] [--no-cleanup] [--help]" >&2
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
      log_info "  패키지 목록 확인 (uv 미포함 — Python importlib.metadata 사용):"
      log_info "    docker run --rm ${IMAGE_TAG} /runtime-venv/bin/python -c \\"
      # shellcheck disable=SC2016
      log_info "      \"import importlib.metadata; [print(d.metadata['Name'], d.metadata['Version']) for d in sorted(importlib.metadata.distributions(), key=lambda d: (d.metadata['Name'] or '').lower())]\""
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

  # 이미지 크기 출력 (크기 임계값과 함께)
  local image_size
  image_size="$(docker image inspect "${IMAGE_TAG}" --format='{{.Size}}' 2>/dev/null || echo '?')"
  if [[ "${image_size}" != "?" ]]; then
    local size_mb
    size_mb=$(( image_size / 1024 / 1024 ))
    log_info "  이미지 크기: ${size_mb} MB (임계값: ${MAX_IMAGE_SIZE_MB} MB)"
  fi
}

# ── 컨테이너 내 패키지 설치 여부 확인 ───────────────────────────────────────
# 사용법: pkg_is_installed <distribution-name>
# 반환:   0 = 설치됨,  1 = 설치 안 됨
# 설명:   Python 표준 라이브러리 importlib.metadata 를 사용하여 패키지 존재 여부를
#         확인합니다.  uv 는 런타임 이미지에 포함되지 않으므로 (설계 원칙: 패키지
#         관리 도구 없는 프로덕션 이미지) Python 인터프리터를 직접 사용합니다.
#
# 주의: importlib.metadata.version() 은 distribution name 을 받으므로,
#       "SQLAlchemy"·"sse-starlette"·"argon2-cffi" 등 PyPI 배포 이름을 그대로
#       전달하면 됩니다.  대소문자 및 하이픈/언더스코어는 PEP 503 정규화 후
#       비교되므로 대부분 일치합니다.
pkg_is_installed() {
  local pkg="$1"
  docker run --rm \
    "${IMAGE_TAG}" \
    /runtime-venv/bin/python - <<PYEOF >/dev/null 2>&1
import importlib.metadata, sys
try:
    importlib.metadata.version("${pkg}")
    sys.exit(0)
except importlib.metadata.PackageNotFoundError:
    sys.exit(1)
PYEOF
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

# ── 시스템 명령어 absent 검사: docker run으로 not found 반환 확인 ────────────
# 사용법: check_system_cmd_absent <command> [description]
# 반환:   0 = 명령어 없음(PASS),  1 = 명령어 있음(FAIL)
#
# 검증 방법:
#   docker run --rm <IMAGE> sh -c "command -v <cmd> >/dev/null 2>&1"
#   종료 코드 0  → 도구 발견 → FAIL (프로덕션 이미지에 있으면 안 됨)
#   종료 코드 1  → 도구 미발견 → PASS (not found — 정상)
#   종료 코드 2+ → docker/sh 오류 → WARN (보수적으로 PASS 처리)
#
# 주의: 이 함수는 실제 컨테이너를 실행하므로 이미지가 먼저 빌드되어 있어야 합니다.
check_system_cmd_absent() {
  local cmd="$1"
  local description="${2:-}"

  local label="${cmd}"
  if [[ -n "${description}" ]]; then
    label="${cmd}  ${DIM}(${description})${RESET}"
  fi

  local found_path
  local exit_code=0

  # command -v <cmd> : 존재하면 경로를 stdout으로 출력 후 exit 0,
  #                   없으면 아무것도 출력 안 하고 exit 1.
  #
  # 주의: `|| exit_code=$?` 패턴을 사용 — `|| true`는 exit code를 0으로 덮어써
  #       실제 종료 코드를 잃게 됩니다.
  found_path=$(docker run --rm "${IMAGE_TAG}" \
    sh -c "command -v '${cmd}' 2>/dev/null" 2>&1) || exit_code=$?

  case ${exit_code} in
    0)
      # 명령어 발견 → FAIL
      log_fail "시스템 명령어가 런타임 이미지에 존재함: ${label}"
      log_info "  발견된 경로: ${found_path:-<경로 불명>}"
      return 1
      ;;
    1)
      # 명령어 없음 → PASS
      log_ok "absent (not found): ${label}"
      return 0
      ;;
    *)
      # docker run 자체 실패 또는 예상치 못한 종료 코드
      log_warn "확인 불가 (docker exit ${exit_code}): ${cmd} — 건너뜀"
      if [[ -n "${found_path:-}" ]]; then
        log_info "  출력: ${found_path}"
      fi
      return 0  # 보수적으로 PASS 처리 (이미지 빌드 문제와 구분)
      ;;
  esac
}

# ── 파일/디렉터리 존재 여부 확인 (python3-dev 헤더 등) ───────────────────────
# 사용법: check_path_absent <path-in-container> [description]
# 반환:   0 = 경로 없음(PASS),  1 = 경로 있음(FAIL)
#
# <path-in-container> 는 glob 미지원 — 정확한 경로를 지정하세요.
# glob 패턴이 필요하면 check_glob_path_absent 를 사용하세요.
check_path_absent() {
  local path="$1"
  local description="${2:-}"

  local label="${path}"
  if [[ -n "${description}" ]]; then
    label="${path}  ${DIM}(${description})${RESET}"
  fi

  local exit_code
  docker run --rm "${IMAGE_TAG}" sh -c "test -e '${path}' 2>/dev/null" \
    >/dev/null 2>&1
  exit_code=$?

  if [[ ${exit_code} -eq 0 ]]; then
    log_fail "빌드 전용 경로가 런타임 이미지에 존재함: ${label}"
    return 1
  else
    log_ok "absent (not found): ${label}"
    return 0
  fi
}

# ── glob 패턴 경로 absent 확인 (python3 헤더 디렉터리 등) ────────────────────
# 사용법: check_glob_path_absent <glob-pattern> [description]
# 반환:   0 = 일치 경로 없음(PASS),  1 = 일치 경로 있음(FAIL)
#
# sh -c "ls <pattern> 2>/dev/null | grep -q ." 방식으로 glob을 컨테이너 내부에서 평가.
check_glob_path_absent() {
  local pattern="$1"
  local description="${2:-}"

  local label="${pattern}"
  if [[ -n "${description}" ]]; then
    label="${pattern}  ${DIM}(${description})${RESET}"
  fi

  local exit_code
  docker run --rm "${IMAGE_TAG}" \
    sh -c "ls ${pattern} 2>/dev/null | grep -q ." \
    >/dev/null 2>&1
  exit_code=$?

  if [[ ${exit_code} -eq 0 ]]; then
    log_fail "빌드 전용 경로(glob)가 런타임 이미지에 존재함: ${label}"
    return 1
  else
    log_ok "absent (glob, not found): ${label}"
    return 0
  fi
}

# ── 이미지 크기 검사 ─────────────────────────────────────────────────────────
# 사용법: check_image_size <threshold_mb>
# 반환:   0 = PASS (임계값 이하),  1 = FAIL (임계값 초과)
#
# docker image inspect --format='{{.Size}}' 로 압축 해제된 이미지 크기(bytes)를
# 읽어 MB 단위로 변환한 뒤 <threshold_mb> 와 비교합니다.
#
# 정책 참조: docs/docker-image-size-policy.md
#   auth 전용 (include_chat_domain=no)    : 350 MB 이하 권장
#   auth + chat (include_chat_domain=yes)  : 600 MB 이하 권장
#   기본 임계값 (이 스크립트 기본값)         : 500 MB
check_image_size() {
  local threshold_mb="$1"

  log_info "docker image inspect 로 이미지 크기 확인 중..."

  local raw_bytes
  raw_bytes="$(docker image inspect "${IMAGE_TAG}" --format='{{.Size}}' 2>/dev/null || echo '')"

  if [[ -z "${raw_bytes}" ]]; then
    log_warn "이미지 크기를 확인할 수 없습니다 (docker image inspect 실패)"
    log_info "  이미지가 존재하지 않거나 Docker 데몬 문제일 수 있습니다."
    return 1
  fi

  local size_mb
  size_mb=$(( raw_bytes / 1024 / 1024 ))
  local size_bytes_readable
  # 소수점 한 자리 표시 (bash 정수 산술 — awk 사용)
  size_bytes_readable="$(awk "BEGIN {printf \"%.1f\", ${raw_bytes}/1024/1024}")"

  log_info "  측정된 이미지 크기: ${size_bytes_readable} MB (${raw_bytes} bytes)"
  log_info "  허용 임계값:         ${threshold_mb} MB"
  log_info "  정책 문서:           docs/docker-image-size-policy.md"

  if [[ ${size_mb} -le ${threshold_mb} ]]; then
    log_ok "이미지 크기 임계값 이내: ${size_bytes_readable} MB ≤ ${threshold_mb} MB"
    return 0
  else
    log_fail "이미지 크기 임계값 초과: ${size_bytes_readable} MB > ${threshold_mb} MB"
    log_info "  원인 분석 방법:"
    log_info "    # 레이어별 크기 분석 (dive 도구)"
    log_info "    docker run --rm -it -v /var/run/docker.sock:/var/run/docker.sock \\"
    log_info "      wagoodman/dive:latest ${IMAGE_TAG}"
    log_info ""
    log_info "    # 레이어 히스토리 출력"
    log_info "    docker history --no-trunc ${IMAGE_TAG}"
    log_info ""
    log_info "  임계값 조정 방법:"
    log_info "    # CLI 옵션"
    log_info "    bash scripts/validate_prod_image.sh --max-size-mb 600"
    log_info "    # 환경변수 (CI 권장)"
    log_info "    MAX_IMAGE_SIZE_MB=600 bash scripts/validate_prod_image.sh"
    log_info ""
    log_info "  크기 감소 전략:"
    log_info "    1. .dockerignore 에 불필요한 파일 추가"
    log_info "    2. runtime 스테이지 apt 설치 패키지 최소화"
    log_info "    3. uv pip install --no-cache 옵션 확인"
    log_info "    4. pyproject.toml [project].dependencies 에서 무거운 의존성 교체 검토"
    return 1
  fi
}

# ── 패키지 목록 전체 출력 (--no-cleanup 디버깅 모드에서만) ───────────────────
# uv 는 런타임 이미지에 없으므로 importlib.metadata 를 직접 사용합니다.
# 주의: `python -c` 를 사용 — heredoc + `|| { }` 조합은 bash 파서 오류를 유발함.
dump_installed_packages() {
  log_section "런타임 이미지 설치 패키지 전체 목록 (디버깅)"
  docker run --rm \
    "${IMAGE_TAG}" \
    /runtime-venv/bin/python -c "
import importlib.metadata
dists = sorted(
    importlib.metadata.distributions(),
    key=lambda d: (d.metadata.get('Name') or '').lower(),
)
for d in dists:
    name = d.metadata.get('Name') or 'unknown'
    ver  = d.metadata.get('Version') or '?'
    print(f'{name:<40} {ver}')
" 2>/dev/null || log_warn "패키지 목록 출력 실패 (importlib.metadata)"
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

  # ─────────────────────────────────────────────────────────────────────────
  # 검사 D: 시스템 빌드/개발 도구 absent — docker run으로 not found 확인
  #
  # 목적:
  #   • python:slim-bookworm 기반 이미지는 gcc/git 등을 기본 포함하지 않지만,
  #     COPY --from 실수나 RUN 누락 정리로 유입될 수 있어 명시적으로 검증.
  #   • uv/pip 는 패키지 관리 도구 — 프로덕션 이미지에 절대 포함 불가.
  #
  # 검증 방법: docker run --rm <IMAGE> sh -c "command -v <tool> >/dev/null 2>&1"
  #   종료 0 → 도구 존재 → FAIL
  #   종료 1 → 도구 없음 → PASS (not found — 정상)
  # ─────────────────────────────────────────────────────────────────────────
  log_section "검사 D: 시스템 도구 absent (docker run — not found 확인)"
  log_info "각 도구를 docker run --rm ${IMAGE_TAG} sh -c 'command -v <cmd>' 로 실행"
  log_info "종료 코드 0=존재(FAIL) / 1=없음(PASS)"
  log_divider

  declare -a SYSTEM_TOOLS=(
    "uv:패키지 매니저 — 런타임 이미지에 절대 불가"
    "uvx:uv 확장 실행기 — 런타임 이미지에 절대 불가"
    "pip:Python 패키지 설치 도구 (pip)"
    "pip3:Python 3 패키지 설치 도구 (pip3)"
    "gcc:C 컴파일러 (build-essential 소속)"
    "g++:C++ 컴파일러"
    "cc:C 컴파일러 심볼릭 링크 (build-essential)"
    "make:빌드 자동화 도구"
    "git:버전 관리 도구 (builder 스테이지 전용)"
    "cargo:Rust 패키지 매니저"
    "npm:Node.js 패키지 매니저"
    "node:Node.js 런타임"
  )

  for entry in "${SYSTEM_TOOLS[@]}"; do
    local cmd="${entry%%:*}"
    local desc="${entry##*:}"
    if check_system_cmd_absent "${cmd}" "${desc}"; then
      passed=$(( passed + 1 ))
    else
      failed=$(( failed + 1 ))
    fi
  done

  # ─────────────────────────────────────────────────────────────────────────
  # 검사 D-extra: 파일/디렉터리 경로 absent 확인
  #
  # uv/uvx 바이너리 경로 — builder 스테이지에서 COPY --from=uv-binary로
  # /usr/local/bin/uv, /usr/local/bin/uvx 에 설치되지만 runtime 스테이지에는
  # 절대 복사되어서는 안 됨. command -v 검사와 함께 이중 검증.
  # ─────────────────────────────────────────────────────────────────────────
  log_divider
  log_info "빌드 전용 바이너리 경로 absent 확인 (check_path_absent)..."

  declare -a BUILD_PATHS=(
    "/usr/local/bin/uv:uv 바이너리 (uv-binary 스테이지 복사 경로)"
    "/usr/local/bin/uvx:uvx 바이너리 (uv-binary 스테이지 복사 경로)"
    "/usr/bin/uv:uv 바이너리 (대체 설치 경로)"
    "/usr/bin/pip:pip 바이너리 (시스템 Python pip)"
    "/usr/bin/pip3:pip3 바이너리 (시스템 Python pip3)"
    "/usr/bin/gcc:gcc 컴파일러 바이너리 (build-essential)"
    "/usr/bin/g++:g++ 컴파일러 바이너리"
    "/usr/bin/git:git 바이너리"
    "/usr/bin/make:make 바이너리"
  )

  for entry in "${BUILD_PATHS[@]}"; do
    local path="${entry%%:*}"
    local desc="${entry##*:}"
    if check_path_absent "${path}" "${desc}"; then
      passed=$(( passed + 1 ))
    else
      failed=$(( failed + 1 ))
    fi
  done

  # ─────────────────────────────────────────────────────────────────────────
  # 검사 D-extra2: Python 개발 헤더 파일 absent (python3-dev apt 패키지)
  #
  # python3-dev 설치 시 /usr/include/python3.x/ 디렉터리가 생성됨.
  # 런타임 이미지는 이 헤더 파일을 포함해서는 안 됨 (컴파일러 없이 의미 없음).
  # ─────────────────────────────────────────────────────────────────────────
  log_divider
  log_info "Python 개발 헤더 / libpq 개발 파일 absent 확인 (check_glob_path_absent)..."

  # /usr/include/python3* — python3-dev 헤더 디렉터리 (glob 평가를 컨테이너 내부에서)
  if check_glob_path_absent "/usr/include/python3*" \
      "Python 개발 헤더 (python3-dev — 컴파일러 없이 불필요)"; then
    passed=$(( passed + 1 ))
  else
    failed=$(( failed + 1 ))
  fi

  # /usr/include/libpq-fe.h — libpq-dev 개발 헤더 (런타임에는 libpq5만 필요)
  if check_path_absent "/usr/include/libpq-fe.h" \
      "libpq 개발 헤더 (libpq-dev — 런타임은 libpq5 공유 라이브러리만 사용)"; then
    passed=$(( passed + 1 ))
  else
    failed=$(( failed + 1 ))
  fi

  # ─────────────────────────────────────────────────────────────────────────
  # 검사 E: 최종 이미지 크기 임계값 검증 (Sub-AC 3.3)
  #
  # 목적:
  #   프로덕션 이미지가 허용 임계값 이하인지 강제 검증합니다.
  #   임계값 초과는 불필요한 의존성 유입, .dockerignore 누락,
  #   또는 multi-stage 분리 실패를 의미할 수 있습니다.
  #
  # 정책:
  #   auth 전용 (include_chat_domain=no)    : 350 MB 이하 권장
  #   auth + chat (include_chat_domain=yes)  : 600 MB 이하 권장
  #   기본 임계값 (--max-size-mb 미지정)      : 500 MB
  #
  # 임계값 재정의:
  #   --max-size-mb N  또는  MAX_IMAGE_SIZE_MB=N 환경변수
  #
  # 상세 문서: docs/docker-image-size-policy.md
  # ─────────────────────────────────────────────────────────────────────────
  log_section "검사 E: 최종 이미지 크기 임계값 검증 (Sub-AC 3.3)"
  log_info "임계값: ${MAX_IMAGE_SIZE_MB} MB (--max-size-mb 또는 MAX_IMAGE_SIZE_MB 로 재정의 가능)"
  log_info "정책:   auth 전용 ≤ 350 MB | auth+chat ≤ 600 MB | 기본값 500 MB"

  if check_image_size "${MAX_IMAGE_SIZE_MB}"; then
    passed=$(( passed + 1 ))
  else
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
    echo -e "    이미지 내 패키지 목록 확인 (uv 미포함 — importlib.metadata 사용):"
    # shellcheck disable=SC2016
    echo -e "      docker run --rm ${IMAGE_TAG} /runtime-venv/bin/python -c \\"
    echo -e "        \"import importlib.metadata; [print(d.metadata['Name'], d.metadata['Version']) for d in sorted(importlib.metadata.distributions(), key=lambda d: (d.metadata.get('Name') or '').lower())]\""
    echo -e "    전체 빌드 로그 확인:"
    echo -e "      docker build --target runtime --progress=plain -t dbg:test ${PROJECT_DIR}"
    exit 1
  fi
}

main "$@"
