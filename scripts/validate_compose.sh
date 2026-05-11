#!/usr/bin/env bash
# =============================================================================
# scripts/validate_compose.sh — docker-compose 서비스 기동 검증
# =============================================================================
#
# 생성된 프로젝트 루트에서 docker compose up --build -d 를 실행하고,
# 정의된 모든 서비스가 healthy 상태에 도달하는지 polling합니다.
# 타임아웃 초과 또는 서비스 비정상 종료 시 — 실패 컨테이너 로그를 출력하고
# exit code 1 로 종료합니다.
#
# 사용법:
#   bash scripts/validate_compose.sh [OPTIONS]
#
# 옵션:
#   --project-dir DIR    검증할 프로젝트 디렉터리
#                        (생략 시 cookiecutter로 기본 시나리오 생성)
#   --scenario FILE      cookiecutter 생성 시 사용할 시나리오 JSON 파일
#                        (기본: scripts/scenarios/default_all_features.json)
#   --timeout SECS       헬스체크 폴링 최대 대기 시간 (기본: 120초)
#   --poll-interval SECS 폴링 간격 (기본: 3초)
#   --with-app           --profile app 포함 (FastAPI 앱 컨테이너도 검증)
#   --no-cleanup         완료 후 컨테이너·임시 디렉터리 유지 (디버깅용)
#   --no-color           ANSI 색상 비활성화
#   --help, -h           이 도움말 출력
#
# 종료 코드:
#   0  모든 서비스 healthy 도달 확인
#   1  하나 이상의 서비스가 healthy 미도달 또는 docker compose 실패
#   2  필수 사전 요구사항 누락 (docker 없음 등)
#
# 예시:
#   # 기본 실행 (임시 디렉터리에 프로젝트 생성 후 infra 검증)
#   bash scripts/validate_compose.sh
#
#   # 이미 생성된 프로젝트 디렉터리로 검증
#   bash scripts/validate_compose.sh --project-dir /tmp/my-project
#
#   # 앱 서비스 포함 풀스택 검증 (빌드 포함)
#   bash scripts/validate_compose.sh --with-app --timeout 300
#
#   # 디버깅 — 컨테이너를 종료하지 않고 유지
#   bash scripts/validate_compose.sh --no-cleanup
# =============================================================================
set -euo pipefail

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SCENARIOS_DIR="${SCRIPT_DIR}/scenarios"

# ── 기본값 ───────────────────────────────────────────────────────────────────
PROJECT_DIR=""
SCENARIO_FILE="${SCENARIOS_DIR}/default_all_features.json"
TIMEOUT=120
POLL_INTERVAL=3
WITH_APP=false
CLEANUP=true
USE_COLOR=true
TMP_DIR=""

# ── 색상 ─────────────────────────────────────────────────────────────────────
_init_colors() {
  if [[ "${USE_COLOR}" == "true" ]] && [[ -t 1 ]]; then
    RED='\033[1;31m'
    GREEN='\033[1;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[1;36m'
    BOLD='\033[1m'
    DIM='\033[90m'
    RESET='\033[0m'
  else
    RED='' GREEN='' YELLOW='' CYAN='' BOLD='' DIM='' RESET=''
  fi
}

# ── 로그 헬퍼 ────────────────────────────────────────────────────────────────
log_info()    { echo -e "${DIM}[INFO]${RESET}  $*"; }
log_ok()      { echo -e "${GREEN}[ OK ]${RESET}  $*"; }
log_fail()    { echo -e "${RED}[FAIL]${RESET}  $*" >&2; }
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

_elapsed() {
  local secs="$1"
  printf "%02d:%02d" $(( secs / 60 )) $(( secs % 60 ))
}

# ── 인수 파싱 ─────────────────────────────────────────────────────────────────
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --project-dir)
        if [[ -z "${2:-}" ]]; then
          echo "오류: --project-dir 에 경로가 필요합니다." >&2; exit 2
        fi
        PROJECT_DIR="$2"; shift 2 ;;
      --scenario)
        if [[ -z "${2:-}" ]]; then
          echo "오류: --scenario 에 JSON 파일 경로가 필요합니다." >&2; exit 2
        fi
        SCENARIO_FILE="$2"; shift 2 ;;
      --timeout)
        if [[ -z "${2:-}" ]]; then
          echo "오류: --timeout 에 초(秒) 값이 필요합니다." >&2; exit 2
        fi
        TIMEOUT="$2"; shift 2 ;;
      --poll-interval)
        if [[ -z "${2:-}" ]]; then
          echo "오류: --poll-interval 에 초(秒) 값이 필요합니다." >&2; exit 2
        fi
        POLL_INTERVAL="$2"; shift 2 ;;
      --with-app)
        WITH_APP=true; shift ;;
      --no-cleanup)
        CLEANUP=false; shift ;;
      --no-color)
        USE_COLOR=false; shift ;;
      --help|-h)
        sed -n '2,60p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
        exit 0 ;;
      *)
        echo "알 수 없는 옵션: $1" >&2
        echo "사용법: bash ${BASH_SOURCE[0]} [OPTIONS]" >&2
        exit 2 ;;
    esac
  done
}

# ── 종료 시 정리 ─────────────────────────────────────────────────────────────
_cleanup() {
  local exit_code=$?
  echo  # 스피너 줄 보장

  if [[ "${CLEANUP}" == true ]]; then
    # 프로젝트 디렉터리가 있으면 컨테이너 종료
    if [[ -n "${PROJECT_DIR:-}" ]] && [[ -f "${PROJECT_DIR}/docker-compose.yml" ]]; then
      log_info "컨테이너 종료 중..."
      local compose_flags=()
      if [[ "${WITH_APP}" == true ]]; then
        compose_flags=(--profile app)
      fi
      ${COMPOSE_CMD:-docker compose} \
        "${compose_flags[@]}" \
        -f "${PROJECT_DIR}/docker-compose.yml" \
        down --remove-orphans 2>/dev/null || true
    fi

    # cookiecutter로 생성된 임시 디렉터리 삭제
    if [[ -n "${TMP_DIR:-}" ]] && [[ -d "${TMP_DIR:-}" ]]; then
      log_info "임시 디렉터리 삭제 중: ${TMP_DIR}"
      rm -rf "${TMP_DIR}"
    fi
  else
    log_info "(--no-cleanup) 컨테이너와 디렉터리를 유지합니다."
    if [[ -n "${PROJECT_DIR:-}" ]]; then
      log_info "  프로젝트 디렉터리: ${PROJECT_DIR}"
      log_info "  컨테이너 확인:    docker compose -f ${PROJECT_DIR}/docker-compose.yml ps"
      log_info "  로그 확인:        docker compose -f ${PROJECT_DIR}/docker-compose.yml logs"
      log_info "  정리:             docker compose -f ${PROJECT_DIR}/docker-compose.yml down -v"
    fi
  fi

  return $exit_code
}
trap _cleanup EXIT

# ── 사전 요구사항 확인 ────────────────────────────────────────────────────────
check_prerequisites() {
  log_section "사전 요구사항 확인"
  local missing=0

  # docker 바이너리
  if ! command -v docker &>/dev/null; then
    log_fail "docker 가 설치되어 있지 않습니다."
    log_info "  설치: https://docs.docker.com/get-docker/"
    missing=1
  else
    local docker_ver
    docker_ver="$(docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo '?')"
    log_ok "docker ${docker_ver}"
  fi

  # Docker 데몬 실행 여부
  if command -v docker &>/dev/null && ! docker info &>/dev/null 2>&1; then
    log_fail "Docker 데몬이 실행 중이지 않습니다."
    log_info "  macOS: Docker Desktop 을 시작하세요."
    log_info "  Linux: sudo systemctl start docker"
    missing=1
  fi

  # docker compose v2 (플러그인) 또는 docker-compose v1
  if docker compose version &>/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    local compose_ver
    compose_ver="$(docker compose version --short 2>/dev/null || echo '?')"
    log_ok "docker compose ${compose_ver}"
  elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
    log_warn "docker-compose v1 감지. Docker Compose v2 (플러그인)으로 업그레이드를 권장합니다."
  else
    log_fail "docker compose 또는 docker-compose 가 설치되어 있지 않습니다."
    log_info "  설치: https://docs.docker.com/compose/install/"
    missing=1
  fi

  if [[ $missing -ne 0 ]]; then
    log_fail "사전 요구사항이 충족되지 않았습니다."
    exit 2
  fi

  log_ok "모든 사전 요구사항 충족"
}

# ── cookiecutter 프로젝트 생성 ────────────────────────────────────────────────
generate_project() {
  log_section "cookiecutter 프로젝트 생성"

  if [[ ! -f "${SCENARIO_FILE}" ]]; then
    log_fail "시나리오 파일을 찾을 수 없습니다: ${SCENARIO_FILE}"
    exit 1
  fi

  TMP_DIR="$(mktemp -d /tmp/cc_compose_validate_XXXXXX)"
  log_info "임시 디렉터리: ${TMP_DIR}"
  log_info "템플릿 위치:   ${TEMPLATE_DIR}"
  log_info "시나리오 파일: ${SCENARIO_FILE}"

  export COOKIECUTTER_SKIP_HEAVY_OPS=1

  # cookiecutter 실행 방법 탐색
  if python3 -c "import cookiecutter" 2>/dev/null; then
    log_info "Python API로 cookiecutter 실행 중..."
    python3 - <<PYEOF
import os, json
os.environ["COOKIECUTTER_SKIP_HEAVY_OPS"] = "1"
with open("${SCENARIO_FILE}") as f:
    data = json.load(f)
ctx = {k: v for k, v in data["cookiecutter"].items() if not k.startswith("_")}
from cookiecutter.main import cookiecutter
result = cookiecutter(
    template="${TEMPLATE_DIR}",
    no_input=True,
    extra_context=ctx,
    output_dir="${TMP_DIR}",
    overwrite_if_exists=True,
)
print(f"생성된 프로젝트: {result}")
PYEOF

  elif command -v uvx &>/dev/null; then
    log_info "uvx cookiecutter 로 실행 중..."
    COOKIECUTTER_SKIP_HEAVY_OPS=1 uvx cookiecutter \
      --no-input \
      --replay-file "${SCENARIO_FILE}" \
      --output-dir "${TMP_DIR}" \
      "${TEMPLATE_DIR}"

  elif command -v cookiecutter &>/dev/null; then
    log_info "cookiecutter CLI 로 실행 중..."
    COOKIECUTTER_SKIP_HEAVY_OPS=1 cookiecutter \
      --no-input \
      --replay-file "${SCENARIO_FILE}" \
      --output-dir "${TMP_DIR}" \
      "${TEMPLATE_DIR}"

  else
    log_fail "cookiecutter 를 찾을 수 없습니다."
    log_info "  설치: pip install cookiecutter"
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

# ── .env 파일 설정 ────────────────────────────────────────────────────────────
setup_env() {
  local project_dir="$1"

  if [[ -f "${project_dir}/.env" ]]; then
    log_info ".env 파일이 이미 존재합니다 — 기존 파일 사용"
    return
  fi

  if [[ -f "${project_dir}/.env.example" ]]; then
    cp "${project_dir}/.env.example" "${project_dir}/.env"
    log_ok ".env.example → .env 복사 완료"
  else
    log_warn ".env.example 파일을 찾을 수 없습니다. 환경변수가 설정되어 있지 않을 수 있습니다."
  fi
}

# ── docker compose up --build -d 실행 ────────────────────────────────────────
run_compose_up() {
  local project_dir="$1"

  log_section "docker compose up --build -d 실행"

  local compose_flags=()
  if [[ "${WITH_APP}" == true ]]; then
    compose_flags=(--profile app)
    log_info "모드: --profile app (FastAPI 앱 컨테이너 포함)"
  else
    log_info "모드: 인프라 전용 (postgres, redis, mailpit)"
  fi

  local compose_file="${project_dir}/docker-compose.yml"
  if [[ ! -f "${compose_file}" ]]; then
    log_fail "docker-compose.yml 을 찾을 수 없습니다: ${compose_file}"
    exit 1
  fi

  log_info "Compose 파일: ${compose_file}"
  log_info "명령: ${COMPOSE_CMD} ${compose_flags[*]:-} -f docker-compose.yml up --build -d"
  echo

  # PROJECT_DIR 기준으로 실행 (볼륨 이름 일관성 보장)
  if ! (
    cd "${project_dir}"
    # shellcheck disable=SC2086
    ${COMPOSE_CMD} \
      ${compose_flags[*]:-} \
      -f docker-compose.yml \
      up --build -d
  ); then
    log_fail "docker compose up --build -d 실패"
    log_info "  상세 로그 확인:"
    log_info "    cd ${project_dir}"
    log_info "    ${COMPOSE_CMD} ${compose_flags[*]:-} -f docker-compose.yml up --build -d"
    exit 1
  fi

  log_ok "docker compose up --build -d 명령 실행 완료"
}

# ── 컨테이너 헬스 상태 조회 ──────────────────────────────────────────────────
# 반환: "healthy" | "unhealthy" | "starting" | "none" | "exited" | "unknown"
get_container_health() {
  local project_dir="$1"
  local service="$2"
  local compose_file="${project_dir}/docker-compose.yml"

  # 컨테이너 ID 조회
  local container_id
  container_id=$(
    cd "${project_dir}"
    ${COMPOSE_CMD} -f docker-compose.yml ps -q "${service}" 2>/dev/null | head -1 || true
  )

  if [[ -z "${container_id}" ]]; then
    echo "none"
    return
  fi

  # 컨테이너 상태 확인
  local status
  status=$(docker inspect \
    --format='{{.State.Status}}' \
    "${container_id}" 2>/dev/null || echo "unknown")

  if [[ "${status}" == "exited" || "${status}" == "dead" ]]; then
    echo "exited"
    return
  fi

  # 헬스체크 상태 확인
  local health
  health=$(docker inspect \
    --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' \
    "${container_id}" 2>/dev/null || echo "unknown")

  if [[ "${health}" == "no-healthcheck" ]]; then
    # healthcheck 미정의 — running이면 healthy로 간주
    if [[ "${status}" == "running" ]]; then
      echo "healthy"
    else
      echo "starting"
    fi
    return
  fi

  echo "${health}"
}

# ── 컨테이너 로그 덤프 ───────────────────────────────────────────────────────
dump_service_logs() {
  local project_dir="$1"
  local service="$2"
  local lines="${3:-100}"

  echo
  echo -e "${YELLOW}${BOLD}── ${service} 로그 (최근 ${lines}줄) ──────────────────────────────────${RESET}"
  (
    cd "${project_dir}"
    ${COMPOSE_CMD} -f docker-compose.yml logs --tail="${lines}" "${service}" 2>&1
  ) || true
  echo -e "${YELLOW}${BOLD}─────────────────────────────────────────────────────────────${RESET}"
}

# ── 단일 서비스 헬스체크 폴링 ────────────────────────────────────────────────
# 반환: 0=healthy, 1=failed/timeout
wait_for_service() {
  local project_dir="$1"
  local service="$2"
  local deadline="$3"   # epoch seconds
  local start_time="$4" # epoch seconds

  local spinner=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
  local spin_idx=0

  while true; do
    local now
    now=$(date +%s)
    local remaining=$(( deadline - now ))
    local elapsed=$(( now - start_time ))

    if [[ $remaining -le 0 ]]; then
      echo -e "\r${RED}[TIMEOUT]${RESET} ${service}: 타임아웃 (${TIMEOUT}초 초과)                        "
      return 1
    fi

    local health
    health=$(get_container_health "${project_dir}" "${service}")

    case "${health}" in
      healthy)
        echo -e "\r${GREEN}[ OK ]${RESET}   ${service}: ${GREEN}healthy${RESET} ($(_elapsed ${elapsed}))                        "
        return 0
        ;;
      unhealthy|exited|dead)
        echo -e "\r${RED}[FAIL]${RESET}   ${service}: ${RED}${health}${RESET} ($(_elapsed ${elapsed}))                        "
        return 1
        ;;
      starting|none|unknown|no-healthcheck)
        local spin_char="${spinner[$spin_idx]}"
        spin_idx=$(( (spin_idx + 1) % ${#spinner[@]} ))
        printf "\r${YELLOW}[WAIT]${RESET}   ${service}: %-14s %s  ($(_elapsed ${elapsed}) / $(_elapsed ${TIMEOUT}))   " \
          "${health}" "${spin_char}"
        sleep "${POLL_INTERVAL}"
        ;;
      *)
        printf "\r${DIM}[????]${RESET}   ${service}: %-14s   ($(_elapsed ${elapsed}) / $(_elapsed ${TIMEOUT}))   " \
          "${health}"
        sleep "${POLL_INTERVAL}"
        ;;
    esac
  done
}

# ── compose 파일에 정의된 서비스 목록 조회 ───────────────────────────────────
get_defined_services() {
  local project_dir="$1"
  local profile_flags=()

  if [[ "${WITH_APP}" == true ]]; then
    profile_flags=(--profile app)
  fi

  (
    cd "${project_dir}"
    ${COMPOSE_CMD} \
      ${profile_flags[*]:-} \
      -f docker-compose.yml \
      config --services 2>/dev/null
  ) || true
}

# ── 컨테이너 상태 요약 출력 ──────────────────────────────────────────────────
show_compose_status() {
  local project_dir="$1"
  echo
  log_section "컨테이너 상태"
  (
    cd "${project_dir}"
    ${COMPOSE_CMD} -f docker-compose.yml ps 2>/dev/null
  ) || true
}

# ── 메인 ─────────────────────────────────────────────────────────────────────
main() {
  parse_args "$@"
  _init_colors

  echo
  echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════════${RESET}"
  echo -e "${CYAN}${BOLD}  docker-compose 서비스 기동 검증 스크립트${RESET}"
  echo -e "${CYAN}${BOLD}  템플릿: ${TEMPLATE_DIR}${RESET}"
  echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════════${RESET}"

  # ── 1. 사전 요구사항 확인 ────────────────────────────────────────────────
  check_prerequisites

  # ── 2. 프로젝트 디렉터리 결정 ────────────────────────────────────────────
  if [[ -n "${PROJECT_DIR}" ]]; then
    log_section "기존 프로젝트 디렉터리 사용"
    if [[ ! -d "${PROJECT_DIR}" ]]; then
      log_fail "디렉터리를 찾을 수 없습니다: ${PROJECT_DIR}"
      exit 1
    fi
    if [[ ! -f "${PROJECT_DIR}/docker-compose.yml" ]]; then
      log_fail "docker-compose.yml 이 없습니다: ${PROJECT_DIR}/docker-compose.yml"
      exit 1
    fi
    log_ok "프로젝트 디렉터리: ${PROJECT_DIR}"
  else
    generate_project
  fi

  log_divider
  log_info "검증 설정:"
  log_info "  프로젝트 디렉터리 : ${PROJECT_DIR}"
  log_info "  타임아웃          : ${TIMEOUT}초"
  log_info "  폴링 간격         : ${POLL_INTERVAL}초"
  log_info "  앱 프로파일       : ${WITH_APP}"

  # ── 3. .env 파일 설정 ────────────────────────────────────────────────────
  log_section ".env 파일 설정"
  setup_env "${PROJECT_DIR}"

  # ── 4. docker compose up --build -d 실행 ─────────────────────────────────
  run_compose_up "${PROJECT_DIR}"

  # ── 5. 헬스체크 대상 서비스 결정 ─────────────────────────────────────────
  # healthcheck 블록이 정의된 서비스 (우선순위 순)
  local base_services=("postgres" "redis" "mailpit")
  local app_services=("app")

  # compose 파일에 실제 정의된 서비스만 포함
  local defined
  defined="$(get_defined_services "${PROJECT_DIR}")"

  local -a target_services=()
  for svc in "${base_services[@]}"; do
    if echo "${defined}" | grep -q "^${svc}$"; then
      target_services+=("${svc}")
    else
      log_warn "${svc}: compose 파일에 정의되지 않음 — 건너뜁니다."
    fi
  done

  if [[ "${WITH_APP}" == true ]]; then
    for svc in "${app_services[@]}"; do
      if echo "${defined}" | grep -q "^${svc}$"; then
        target_services+=("${svc}")
      else
        log_warn "${svc}: compose 파일에 정의되지 않음 — 건너뜁니다."
      fi
    done
  fi

  if [[ ${#target_services[@]} -eq 0 ]]; then
    log_warn "헬스체크 대상 서비스가 없습니다. 검증을 종료합니다."
    show_compose_status "${PROJECT_DIR}"
    exit 0
  fi

  # ── 6. 서비스별 헬스체크 폴링 ────────────────────────────────────────────
  log_section "헬스체크 폴링 (최대 ${TIMEOUT}초)"
  log_info "대상 서비스: ${target_services[*]}"
  echo

  local start_time
  start_time=$(date +%s)
  local deadline=$(( start_time + TIMEOUT ))

  local -a failed_services=()
  local -a success_services=()

  for service in "${target_services[@]}"; do
    printf "${YELLOW}[WAIT]${RESET}   ${service}: 폴링 시작...   "
    if wait_for_service "${PROJECT_DIR}" "${service}" "${deadline}" "${start_time}"; then
      success_services+=("${service}")
    else
      failed_services+=("${service}")
    fi
  done

  # ── 7. 결과 판정 ─────────────────────────────────────────────────────────
  show_compose_status "${PROJECT_DIR}"

  local total_elapsed=$(( $(date +%s) - start_time ))

  if [[ ${#failed_services[@]} -gt 0 ]]; then
    # ── 실패: 로그 덤프 후 exit 1 ──────────────────────────────────────────
    echo
    log_fail "${TIMEOUT}초 내 healthy 상태에 도달하지 못한 서비스:"
    for svc in "${failed_services[@]}"; do
      log_fail "  ✗ ${svc}"
    done

    if [[ ${#success_services[@]} -gt 0 ]]; then
      log_info "성공한 서비스: ${success_services[*]}"
    fi
    log_info "총 경과 시간: $(_elapsed ${total_elapsed})"

    # 실패 서비스 로그 덤프
    log_section "실패한 서비스 컨테이너 로그"
    for svc in "${failed_services[@]}"; do
      dump_service_logs "${PROJECT_DIR}" "${svc}" 100
    done

    echo
    log_fail "서비스 기동 검증 실패. 위 로그를 확인하세요."
    log_divider
    log_info "추가 디버깅 명령:"
    log_info "  전체 로그:    cd ${PROJECT_DIR} && ${COMPOSE_CMD} logs"
    log_info "  컨테이너 상태: cd ${PROJECT_DIR} && ${COMPOSE_CMD} ps"
    log_info "  컨테이너 정리: cd ${PROJECT_DIR} && ${COMPOSE_CMD} down -v"
    echo
    exit 1
  fi

  # ── 성공 ────────────────────────────────────────────────────────────────
  echo
  log_section "검증 결과"
  log_divider
  echo -e "  ${GREEN}${BOLD}PASS${RESET}  모든 서비스가 healthy 상태입니다 ✓"
  echo -e ""
  echo -e "  성공한 서비스:"
  for svc in "${success_services[@]}"; do
    echo -e "    ${GREEN}✓${RESET}  ${svc}"
  done
  echo -e ""
  log_info "총 경과 시간: $(_elapsed ${total_elapsed})"
  log_divider
  echo
  log_info "다음 단계:"
  log_info "  cd ${PROJECT_DIR}"
  log_info "  make install    # uv sync + .env 생성"
  log_info "  make migrate    # Alembic 마이그레이션"
  log_info "  make serve      # FastAPI hot-reload 서버"
  log_info "  curl http://localhost:8000/health"
  echo
  exit 0
}

main "$@"
