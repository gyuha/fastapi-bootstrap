#!/usr/bin/env bash
# ============================================================
# wait_for_services.sh — docker-compose 서비스 기동 및 헬스체크 검증
# ============================================================
# 생성된 프로젝트 루트에서 실행합니다.
#
# 사용법:
#   ./scripts/wait_for_services.sh [TIMEOUT_SECONDS]
#
#   TIMEOUT_SECONDS: 헬스체크 폴링 최대 대기 시간 (기본값: 60초)
#
# 동작:
#   1. docker compose up -d postgres redis mailpit 실행
#   2. 정의된 각 서비스(postgres, redis, mailpit)가
#      Docker healthy 상태에 도달할 때까지 폴링
#   3. TIMEOUT_SECONDS 내 실패 시 — 실패 컨테이너 로그 출력 후 exit 1
#   4. 전체 성공 시 — 서비스 접속 정보 출력 후 exit 0
#
# 종료 코드:
#   0 — 모든 서비스 healthy
#   1 — 타임아웃 또는 서비스 비정상 종료
#   2 — 필수 의존성(docker / docker compose) 미설치
# ============================================================

set -euo pipefail

# ── 색상 코드 (TTY가 아니면 비활성화) ────────────────────────────────────────
if [[ -t 1 ]]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  CYAN='\033[0;36m'
  BOLD='\033[1m'
  RESET='\033[0m'
else
  RED='' GREEN='' YELLOW='' CYAN='' BOLD='' RESET=''
fi

# ── 설정 ────────────────────────────────────────────────────────────────────
TIMEOUT="${1:-60}"        # 헬스체크 폴링 최대 대기 시간 (초)
POLL_INTERVAL=2           # 폴링 간격 (초)
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

# 헬스체크 대상 서비스 목록 (docker-compose.yml 서비스명)
# healthcheck 블록이 정의된 서비스만 포함
SERVICES_WITH_HEALTHCHECK=(
  "postgres"
  "redis"
  "mailpit"
)

# ── 헬퍼 함수 ────────────────────────────────────────────────────────────────
log_info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
log_ok()      { echo -e "${GREEN}[OK]${RESET}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
log_section() { echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════${RESET}"; \
                echo -e "${BOLD}${CYAN}  $*${RESET}"; \
                echo -e "${BOLD}${CYAN}══════════════════════════════════════════${RESET}"; }

# 경과 시간 출력 (초 → mm:ss)
_elapsed() {
  local secs="$1"
  printf "%02d:%02d" $((secs / 60)) $((secs % 60))
}

# ── 의존성 확인 ────────────────────────────────────────────────────────────
check_dependencies() {
  local missing=0

  if ! command -v docker &>/dev/null; then
    log_error "docker 가 설치되어 있지 않습니다."
    log_error "  설치: https://docs.docker.com/get-docker/"
    missing=1
  fi

  # docker compose v2 (플러그인) 또는 docker-compose v1 확인
  if docker compose version &>/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
  elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
    log_warn "docker-compose v1 감지. Docker Compose v2 (플러그인)으로 업그레이드를 권장합니다."
  else
    log_error "docker compose 또는 docker-compose 가 설치되어 있지 않습니다."
    log_error "  설치: https://docs.docker.com/compose/install/"
    missing=1
  fi

  if [[ $missing -ne 0 ]]; then
    exit 2
  fi
}

# ── compose 파일 존재 확인 ───────────────────────────────────────────────────
check_compose_file() {
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    log_error "Compose 파일을 찾을 수 없습니다: $COMPOSE_FILE"
    log_error "프로젝트 루트 디렉터리에서 이 스크립트를 실행하세요."
    exit 1
  fi
}

# ── 컨테이너 헬스 상태 조회 ─────────────────────────────────────────────────
# 반환값: "healthy" | "unhealthy" | "starting" | "none" | "exited" | "unknown"
get_container_health() {
  local service="$1"

  # docker compose ps --format json 은 버전에 따라 다름 → docker inspect 사용
  local container_id
  container_id=$(${COMPOSE_CMD} -f "$COMPOSE_FILE" ps -q "$service" 2>/dev/null | head -1)

  if [[ -z "$container_id" ]]; then
    echo "none"
    return
  fi

  # 컨테이너 상태 확인. Docker Go-template 포맷을 쓰면 생성된 프로젝트에
  # Jinja처럼 보이는 이중 중괄호가 남으므로 JSON을 파싱한다.
  local state_json
  state_json=$(docker inspect "$container_id" 2>/dev/null | python3 -c 'import json, sys; data=json.load(sys.stdin); print(json.dumps(data[0].get("State", {})) if data else "{}")' 2>/dev/null || echo "{}")

  local status
  status=$(python3 -c 'import json, sys; print(json.load(sys.stdin).get("Status", "unknown"))' <<< "$state_json" 2>/dev/null || echo "unknown")

  if [[ "$status" == "exited" || "$status" == "dead" ]]; then
    echo "exited"
    return
  fi

  # 헬스체크 상태 확인
  local health
  health=$(python3 -c 'import json, sys; state=json.load(sys.stdin); health=state.get("Health"); print(health.get("Status", "none") if health else "none")' <<< "$state_json" 2>/dev/null || echo "unknown")

  echo "$health"
}

# ── 컨테이너 로그 출력 ──────────────────────────────────────────────────────
dump_service_logs() {
  local service="$1"
  local lines="${2:-50}"

  echo -e "\n${YELLOW}── $service 로그 (최근 ${lines}줄) ──${RESET}"
  ${COMPOSE_CMD} -f "$COMPOSE_FILE" logs --tail="$lines" "$service" 2>&1 || true
  echo -e "${YELLOW}────────────────────────────────────────${RESET}"
}

# ── 단일 서비스 헬스체크 대기 ───────────────────────────────────────────────
wait_for_service() {
  local service="$1"
  local deadline="$2"     # epoch seconds
  local spinner_chars=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
  local spin_idx=0

  while true; do
    local now
    now=$(date +%s)
    local remaining=$((deadline - now))

    if [[ $remaining -le 0 ]]; then
      # 타임아웃
      echo ""  # 스피너 줄 정리
      return 1
    fi

    local health
    health=$(get_container_health "$service")

    case "$health" in
      healthy)
        echo -e "\r${GREEN}[OK]${RESET}    ${service}: ${GREEN}healthy${RESET}                     "
        return 0
        ;;
      unhealthy|exited|dead)
        echo -e "\r${RED}[FAIL]${RESET}  ${service}: ${RED}${health}${RESET}                     "
        return 1
        ;;
      starting|none|unknown)
        # 아직 기동 중
        local elapsed_total
        elapsed_total=$(( $(date +%s) - (deadline - TIMEOUT) ))
        echo -ne "\r${YELLOW}[WAIT]${RESET}  ${service}: ${health}  " \
          "${spinner_chars[$spin_idx]}  " \
          "($(_elapsed $elapsed_total) / $(_elapsed $TIMEOUT))   "
        spin_idx=$(( (spin_idx + 1) % 10 ))
        sleep "$POLL_INTERVAL"
        ;;
    esac
  done
}

# ── 실패한 서비스들의 로그 일괄 출력 ───────────────────────────────────────
dump_failed_logs() {
  local -a failed_services=("$@")

  log_section "실패한 서비스 로그"
  for svc in "${failed_services[@]}"; do
    dump_service_logs "$svc" 100
  done
}

# ── docker compose ps 요약 출력 ─────────────────────────────────────────────
show_compose_status() {
  echo ""
  log_section "컨테이너 상태"
  ${COMPOSE_CMD} -f "$COMPOSE_FILE" ps 2>/dev/null || true
}

# ── 서비스 접속 정보 출력 ───────────────────────────────────────────────────
show_service_endpoints() {
  # .env 파일에서 포트 정보 로드 (없으면 기본값 사용)
  local pg_port="${POSTGRES_PORT:-5432}"
  local redis_port="${REDIS_PORT:-6379}"
  local mailpit_ui="${MAILPIT_UI_PORT:-8025}"
  local mailpit_smtp="${MAILPIT_SMTP_PORT:-1025}"

  if [[ -f ".env" ]]; then
    # shellcheck disable=SC1091
    source <(grep -E '^(POSTGRES_PORT|REDIS_PORT|MAILPIT_UI_PORT|MAILPIT_SMTP_PORT)=' .env 2>/dev/null || true)
    pg_port="${POSTGRES_PORT:-$pg_port}"
    redis_port="${REDIS_PORT:-$redis_port}"
    mailpit_ui="${MAILPIT_UI_PORT:-$mailpit_ui}"
    mailpit_smtp="${MAILPIT_SMTP_PORT:-$mailpit_smtp}"
  fi

  echo ""
  log_section "서비스 접속 정보"
  log_ok "PostgreSQL : localhost:${pg_port}"
  log_ok "Redis      : localhost:${redis_port}"
  log_ok "Mailpit    : http://localhost:${mailpit_ui}  (SMTP: ${mailpit_smtp})"
}

# ── 메인 ────────────────────────────────────────────────────────────────────
main() {
  log_section "FastAPI Bootstrap — 서비스 기동 및 헬스체크"
  log_info "타임아웃: ${TIMEOUT}초  |  폴링 간격: ${POLL_INTERVAL}초"
  log_info "Compose 파일: $COMPOSE_FILE"

  # 1. 의존성 확인
  check_dependencies
  check_compose_file

  # 2. docker compose up -d postgres redis mailpit
  log_section "컨테이너 기동"
  log_info "${COMPOSE_CMD} -f ${COMPOSE_FILE} up -d ${SERVICES_WITH_HEALTHCHECK[*]}"
  if ! ${COMPOSE_CMD} -f "$COMPOSE_FILE" up -d "${SERVICES_WITH_HEALTHCHECK[@]}"; then
    log_error "docker compose up -d ${SERVICES_WITH_HEALTHCHECK[*]} 실패"
    exit 1
  fi

  # 3. 각 서비스 헬스체크 폴링
  log_section "헬스체크 폴링 (최대 ${TIMEOUT}초)"

  local start_time
  start_time=$(date +%s)
  local deadline=$((start_time + TIMEOUT))

  local -a failed_services=()
  local -a success_services=()

  for service in "${SERVICES_WITH_HEALTHCHECK[@]}"; do
    # 해당 서비스가 compose 파일에 정의되어 있는지 확인
    if ! ${COMPOSE_CMD} -f "$COMPOSE_FILE" config --services 2>/dev/null | grep -q "^${service}$"; then
      log_warn "${service}: compose 파일에 정의되지 않음 — 건너뜁니다."
      continue
    fi

    echo -ne "[WAIT]  ${service}: 폴링 시작...   "
    if wait_for_service "$service" "$deadline"; then
      success_services+=("$service")
    else
      failed_services+=("$service")
    fi
  done

  # 4. 결과 판정
  show_compose_status
  local total_elapsed=$(( $(date +%s) - start_time ))

  if [[ "${failed_services[*]-}" != "" ]]; then
    echo ""
    log_error "다음 서비스가 ${TIMEOUT}초 내 healthy 상태에 도달하지 못했습니다:"
    for svc in "${failed_services[@]}"; do
      log_error "  ✗ ${svc}"
    done
    log_info "성공한 서비스: ${success_services[*]:-없음}"
    log_info "총 경과 시간: $(_elapsed $total_elapsed)"

    # 실패 서비스 로그 덤프
    dump_failed_logs "${failed_services[@]}"

    echo ""
    log_error "서비스 기동에 실패했습니다. 위 로그를 확인하세요."
    log_info "전체 로그 확인: ${COMPOSE_CMD} -f ${COMPOSE_FILE} logs"
    log_info "컨테이너 정리: ${COMPOSE_CMD} -f ${COMPOSE_FILE} down"
    exit 1
  fi

  # 5. 전체 성공
  echo ""
  log_ok "모든 서비스가 healthy 상태입니다 ✓"
  log_info "성공한 서비스: ${success_services[*]}"
  log_info "총 경과 시간: $(_elapsed $total_elapsed)"

  show_service_endpoints

  echo ""
  log_info "다음 단계:"
  log_info "  Alembic 마이그레이션 : make migrate"
  log_info "  FastAPI 개발 서버    : make dev"
  log_info "  헬스 엔드포인트      : curl http://localhost:8000/health"
}

main "$@"
