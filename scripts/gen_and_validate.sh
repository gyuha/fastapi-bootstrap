#!/usr/bin/env bash
# =============================================================================
# gen_and_validate.sh — Cookiecutter 비대화형 생성 및 구조 검증 스크립트
# =============================================================================
#
# 사용법:
#   bash scripts/gen_and_validate.sh [--scenario <json_file>] [--output-dir <dir>]
#                                    [--keep] [--no-color] [--help]
#
# 옵션:
#   --scenario  <json>  사전 정의된 시나리오 JSON 파일 (기본: 모든 시나리오 순차 실행)
#   --output-dir <dir>  생성된 프로젝트 출력 디렉터리 (기본: /tmp/cc_validate_<pid>)
#   --keep              검증 후 생성된 프로젝트 디렉터리를 삭제하지 않고 유지
#   --no-color          ANSI 색상 비활성화
#   --help              이 도움말을 출력
#
# 동작:
#   1. scripts/scenarios/*.json 파일(들)을 읽어 cookiecutter --replay-file 로 생성
#   2. 생성된 디렉터리 구조와 핵심 파일 존재 여부를 assert
#   3. 성공/실패 요약을 출력하고 적절한 종료 코드(0=성공, 1=실패)를 반환
#
# 요구사항:
#   - cookiecutter >= 2.1  (pip install cookiecutter 또는 uvx cookiecutter)
#   - jq                   (JSON 파싱용)
#   - bash >= 4.0
#
# 종료 코드:
#   0 — 모든 시나리오 통과
#   1 — 하나 이상의 시나리오 실패
#   2 — 필수 도구(cookiecutter/jq) 미설치
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 경로 설정
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"   # cookiecutter.json 위치
SCENARIOS_DIR="${SCRIPT_DIR}/scenarios"

# ---------------------------------------------------------------------------
# 기본 옵션
# ---------------------------------------------------------------------------
SCENARIO_FILE=""          # 빈 문자열 = 모든 시나리오 실행
OUTPUT_DIR=""             # 빈 문자열 = 임시 디렉터리 자동 생성
KEEP_OUTPUT=false
USE_COLOR=true

# ---------------------------------------------------------------------------
# 인수 파싱
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario)
            SCENARIO_FILE="$2"; shift 2 ;;
        --output-dir)
            OUTPUT_DIR="$2"; shift 2 ;;
        --keep)
            KEEP_OUTPUT=true; shift ;;
        --no-color)
            USE_COLOR=false; shift ;;
        --help|-h)
            head -35 "${BASH_SOURCE[0]}" | tail -30   # 사용법 출력
            exit 0 ;;
        *)
            echo "알 수 없는 옵션: $1" >&2
            exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# ANSI 색상 헬퍼
# ---------------------------------------------------------------------------
_color() {
    local text="$1" code="$2"
    if [[ "${USE_COLOR}" == "true" ]] && [[ -t 1 ]]; then
        echo -e "\033[${code}m${text}\033[0m"
    else
        echo "${text}"
    fi
}

ok()   { echo "  $(_color "✓" "1;32")  $1"; }
fail() { echo "  $(_color "✗" "1;31")  $1"; }
info() { echo "  $(_color "[정보]" "0;90")  $1"; }
warn() { echo "  $(_color "[경고]" "1;33")  $1"; }
sep()  { echo "$(_color "$(printf '─%.0s' {1..64})" "0;90")"; }
sep2() { echo "$(_color "$(printf '═%.0s' {1..64})" "1;36")"; }

# ---------------------------------------------------------------------------
# 도구 가용성 확인
# ---------------------------------------------------------------------------
_check_dependencies() {
    local missing=()

    # cookiecutter 확인 (Python API > uvx > CLI 순서)
    if python3 -c "import cookiecutter" 2>/dev/null; then
        CC_CMD="python_api"
    elif command -v uvx &>/dev/null; then
        CC_CMD="uvx cookiecutter"
    elif command -v cookiecutter &>/dev/null; then
        CC_CMD="cookiecutter"
    else
        missing+=("cookiecutter (pip install cookiecutter 또는 uvx 설치)")
    fi

    # jq 확인 (JSON 파싱)
    if ! command -v jq &>/dev/null; then
        # jq 없어도 python3으로 대체 가능
        if python3 -c "import json,sys" 2>/dev/null; then
            HAVE_JQ=false
        else
            missing+=("jq 또는 python3 (JSON 파싱용)")
        fi
    else
        HAVE_JQ=true
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo ""
        _color "오류: 필수 도구가 없습니다:" "1;31"
        for m in "${missing[@]}"; do
            echo "  - ${m}"
        done
        echo ""
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# JSON 파싱 헬퍼: jq 또는 python3 사용
# ---------------------------------------------------------------------------
_json_get() {
    local file="$1" key="$2"
    if [[ "${HAVE_JQ:-false}" == "true" ]]; then
        jq -r "${key}" "${file}" 2>/dev/null || echo ""
    else
        python3 -c "
import json, sys
with open('${file}') as f:
    data = json.load(f)
keys = '${key}'.lstrip('.').split('.')
result = data
for k in keys:
    if isinstance(result, dict):
        result = result.get(k, '')
    else:
        result = ''
print(result if result is not None else '')
"
    fi
}

_json_keys() {
    local file="$1" path="$2"
    if [[ "${HAVE_JQ:-false}" == "true" ]]; then
        jq -r "${path} | keys[]" "${file}" 2>/dev/null || true
    else
        python3 -c "
import json
with open('${file}') as f:
    data = json.load(f)
keys = '${path}'.lstrip('.').split('.')
result = data
for k in keys:
    if k and isinstance(result, dict):
        result = result.get(k, {})
for key in result.keys():
    print(key)
"
    fi
}

# ---------------------------------------------------------------------------
# 핵심 파일/디렉터리 assert 함수들
# ---------------------------------------------------------------------------

# 필수 경로 목록 (공통 — 모든 시나리오에서 존재해야 함)
REQUIRED_COMMON=(
    "pyproject.toml"
    "docker-compose.yml"
    "Makefile"
    "README.md"
    "alembic.ini"
    ".env.example"
    ".gitignore"
    "alembic"
    "alembic/versions"
    "src"
    "src/{pkg}/__init__.py"
    "src/{pkg}/core/__init__.py"
    "src/{pkg}/domains/__init__.py"
    "src/{pkg}/domains/auth/__init__.py"
    "tests/__init__.py"
    "tests/auth/__init__.py"
    "scripts/wait_for_services.sh"
    "scripts/wait_for_services.py"
)

# chat 도메인 포함 시 필수 경로
REQUIRED_CHAT=(
    "src/{pkg}/domains/chat/__init__.py"
    "src/{pkg}/domains/chat/llm_factory.py"
    "src/{pkg}/domains/chat/llm_client.py"
    "tests/chat/__init__.py"
    "tests/chat/test_llm_factory.py"
    "tests/chat/test_llm_client.py"
)

# OAuth 포함 시 필수 경로
REQUIRED_OAUTH=(
    "src/{pkg}/domains/auth/oauth/__init__.py"
)

# chat 도메인 제외 시 금지 경로
FORBIDDEN_NO_CHAT=(
    "src/{pkg}/domains/chat"
    "tests/chat"
)

# OAuth 없음 시 금지 경로
FORBIDDEN_NO_OAUTH=(
    "src/{pkg}/domains/auth/oauth"
)

# pre-commit 없음 시 금지 경로
FORBIDDEN_NO_PRECOMMIT=(
    ".pre-commit-config.yaml"
)

# ---------------------------------------------------------------------------
# assert_path: 경로 존재 여부 검사
# returns 0=pass, 1=fail
# ---------------------------------------------------------------------------
PASS_COUNT=0
FAIL_COUNT=0

assert_exists() {
    local base="$1" rel="$2"
    local full="${base}/${rel}"
    if [[ -e "${full}" ]]; then
        ok "exists:  ${rel}"
        ((PASS_COUNT++)) || true
        return 0
    else
        fail "MISSING: ${rel}"
        ((FAIL_COUNT++)) || true
        return 1
    fi
}

assert_absent() {
    local base="$1" rel="$2"
    local full="${base}/${rel}"
    if [[ ! -e "${full}" ]]; then
        ok "absent:  ${rel}"
        ((PASS_COUNT++)) || true
        return 0
    else
        fail "SHOULD NOT EXIST: ${rel}"
        ((FAIL_COUNT++)) || true
        return 1
    fi
}

# 파일 내용 포함 검사
assert_contains() {
    local file="$1" needle="$2"
    if [[ ! -f "${file}" ]]; then
        fail "content-check skipped (file missing): $(basename "${file}")"
        ((FAIL_COUNT++)) || true
        return 1
    fi
    if grep -qF "${needle}" "${file}" 2>/dev/null; then
        ok "contains: '${needle}' in $(basename "${file}")"
        ((PASS_COUNT++)) || true
        return 0
    else
        fail "MISSING '${needle}' in $(basename "${file}")"
        ((FAIL_COUNT++)) || true
        return 1
    fi
}

# 파일 내용 미포함 검사
assert_not_contains() {
    local file="$1" needle="$2"
    if [[ ! -f "${file}" ]]; then
        fail "content-check skipped (file missing): $(basename "${file}")"
        ((FAIL_COUNT++)) || true
        return 1
    fi
    if ! grep -qF "${needle}" "${file}" 2>/dev/null; then
        ok "absent: '${needle}' not in $(basename "${file}")"
        ((PASS_COUNT++)) || true
        return 0
    else
        fail "FOUND '${needle}' in $(basename "${file}' (should be absent)")"
        ((FAIL_COUNT++)) || true
        return 1
    fi
}

# ---------------------------------------------------------------------------
# cookiecutter 실행: --replay-file 사용
# ---------------------------------------------------------------------------
_run_cookiecutter() {
    local replay_file="$1" output_dir="$2"

    # COOKIECUTTER_SKIP_HEAVY_OPS=1 → post-gen hook에서 uv sync/git init 건너뜀
    export COOKIECUTTER_SKIP_HEAVY_OPS=1

    if [[ "${CC_CMD}" == "python_api" ]]; then
        # Python API 경로 — 가장 안정적
        python3 - <<PYEOF
import os, sys, json
from pathlib import Path

# 환경변수 설정
os.environ["COOKIECUTTER_SKIP_HEAVY_OPS"] = "1"

with open("${replay_file}") as f:
    data = json.load(f)

# _comment 키 제거 후 cookiecutter 컨텍스트 추출
ctx = {k: v for k, v in data["cookiecutter"].items() if not k.startswith("_")}

from cookiecutter.main import cookiecutter
result = cookiecutter(
    template="${TEMPLATE_DIR}",
    no_input=True,
    extra_context=ctx,
    output_dir="${output_dir}",
    overwrite_if_exists=True,
)
print(result)
PYEOF
    else
        # CLI 경로 (uvx cookiecutter 또는 cookiecutter CLI)
        # replay-file을 사용해 비대화형 실행
        local cmd_arr
        read -ra cmd_arr <<< "${CC_CMD}"

        # JSON에서 key=value 인수 추출 (jq 또는 python3)
        local kv_args=()
        while IFS= read -r key; do
            [[ "${key}" == _* ]] && continue   # _comment 등 내부 키 건너뜀
            local val
            val="$(_json_get "${replay_file}" ".cookiecutter.${key}")"
            if [[ -n "${val}" ]]; then
                kv_args+=("${key}=${val}")
            fi
        done < <(_json_keys "${replay_file}" ".cookiecutter")

        "${cmd_arr[@]}" \
            --no-input \
            --output-dir "${output_dir}" \
            "${TEMPLATE_DIR}" \
            "${kv_args[@]}"
    fi
}

# ---------------------------------------------------------------------------
# 단일 시나리오 검증
# ---------------------------------------------------------------------------
validate_scenario() {
    local scenario_file="$1"
    local scenario_name
    scenario_name="$(basename "${scenario_file}" .json)"
    local output_dir="${BASE_OUTPUT_DIR}/${scenario_name}"

    local description
    description="$(_json_get "${scenario_file}" "._comment" 2>/dev/null || echo "${scenario_name}")"

    # 시나리오 헤더
    echo ""
    sep2
    echo "  $(_color "시나리오: ${scenario_name}" "1;36")"
    echo "  ${description}"
    sep

    # 시나리오 변수 읽기
    local include_chat oauth_providers use_pre_commit
    include_chat="$(_json_get "${scenario_file}" ".cookiecutter.include_chat_domain")"
    oauth_providers="$(_json_get "${scenario_file}" ".cookiecutter.oauth_providers")"
    use_pre_commit="$(_json_get "${scenario_file}" ".cookiecutter.use_pre_commit")"
    local pkg_name
    pkg_name="$(_json_get "${scenario_file}" ".cookiecutter.package_name")"

    info "include_chat_domain : ${include_chat}"
    info "oauth_providers     : ${oauth_providers}"
    info "use_pre_commit      : ${use_pre_commit}"
    info "package_name        : ${pkg_name}"
    echo ""

    # cookiecutter 실행
    echo "  $(_color "[생성]" "1;33") cookiecutter --replay-file 실행 중 ..."
    mkdir -p "${output_dir}"

    local gen_err=0
    if ! _run_cookiecutter "${scenario_file}" "${output_dir}" > /tmp/cc_gen_stdout_$$.txt 2>/tmp/cc_gen_stderr_$$.txt; then
        gen_err=1
    fi

    local project_dir="${output_dir}/fastapi-bootstrap"
    # 생성된 project_slug를 JSON에서 읽어 경로 확인
    local project_slug
    project_slug="$(_json_get "${scenario_file}" ".cookiecutter.project_slug")"
    project_dir="${output_dir}/${project_slug}"

    if [[ ! -d "${project_dir}" ]] || [[ "${gen_err}" -ne 0 ]]; then
        fail "cookiecutter 실행 실패 또는 생성 디렉터리 없음: ${project_dir}"
        if [[ -f /tmp/cc_gen_stderr_$$.txt ]]; then
            echo "  --- stderr ---"
            cat /tmp/cc_gen_stderr_$$.txt | head -20
        fi
        rm -f /tmp/cc_gen_stdout_$$.txt /tmp/cc_gen_stderr_$$.txt
        ((FAIL_COUNT++)) || true
        return 1
    fi

    ok "생성 완료: ${project_dir}"
    rm -f /tmp/cc_gen_stdout_$$.txt /tmp/cc_gen_stderr_$$.txt
    echo ""

    # ── 1. 공통 필수 경로 검사 ──────────────────────────────────────────────
    echo "  $(_color "── 공통 필수 경로 ──" "1;33")"
    for rel in "${REQUIRED_COMMON[@]}"; do
        local resolved="${rel//\{pkg\}/${pkg_name}}"
        assert_exists "${project_dir}" "${resolved}"
    done

    # ── 2. chat 도메인 조건부 검사 ───────────────────────────────────────────
    echo ""
    echo "  $(_color "── chat 도메인 경로 ──" "1;33")"
    if [[ "${include_chat}" == "yes" ]]; then
        for rel in "${REQUIRED_CHAT[@]}"; do
            local resolved="${rel//\{pkg\}/${pkg_name}}"
            assert_exists "${project_dir}" "${resolved}"
        done
    else
        for rel in "${FORBIDDEN_NO_CHAT[@]}"; do
            local resolved="${rel//\{pkg\}/${pkg_name}}"
            assert_absent "${project_dir}" "${resolved}"
        done
        echo ""
        echo "  $(_color "── pyproject.toml LLM 의존성 미포함 검사 ──" "1;33")"
        assert_not_contains "${project_dir}/pyproject.toml" "langchain"
        assert_not_contains "${project_dir}/pyproject.toml" "langchain-litellm"
        assert_not_contains "${project_dir}/pyproject.toml" "litellm"
    fi

    # ── 3. OAuth 조건부 검사 ─────────────────────────────────────────────────
    echo ""
    echo "  $(_color "── OAuth 경로 ──" "1;33")"
    local oauth_lower
    oauth_lower="$(echo "${oauth_providers}" | tr '[:upper:]' '[:lower:]')"

    if [[ "${oauth_lower}" == "none" ]]; then
        for rel in "${FORBIDDEN_NO_OAUTH[@]}"; do
            local resolved="${rel//\{pkg\}/${pkg_name}}"
            assert_absent "${project_dir}" "${resolved}"
        done
    else
        for rel in "${REQUIRED_OAUTH[@]}"; do
            local resolved="${rel//\{pkg\}/${pkg_name}}"
            assert_exists "${project_dir}" "${resolved}"
        done
        # 미선택 provider 파일 없음 검사
        local all_providers=("google" "kakao" "naver")
        local oauth_dir="src/${pkg_name}/domains/auth/oauth"
        for provider in "${all_providers[@]}"; do
            if [[ "${oauth_lower}" != *"${provider}"* ]]; then
                assert_absent "${project_dir}" "${oauth_dir}/${provider}.py"
            fi
        done
    fi

    # ── 4. pre-commit 조건부 검사 ────────────────────────────────────────────
    echo ""
    echo "  $(_color "── pre-commit 설정 ──" "1;33")"
    if [[ "${use_pre_commit}" == "no" ]]; then
        for rel in "${FORBIDDEN_NO_PRECOMMIT[@]}"; do
            assert_absent "${project_dir}" "${rel}"
        done
    else
        assert_exists "${project_dir}" ".pre-commit-config.yaml"
        assert_contains "${project_dir}/.pre-commit-config.yaml" "ruff"
        assert_contains "${project_dir}/.pre-commit-config.yaml" "mypy"
    fi

    # ── 5. 핵심 파일 내용 검사 ──────────────────────────────────────────────
    echo ""
    echo "  $(_color "── 핵심 파일 내용 검사 ──" "1;33")"

    # pyproject.toml 필수 의존성 (공통 — 모든 시나리오)
    local pyproject="${project_dir}/pyproject.toml"
    for dep in "fastapi" "sqlalchemy" "alembic" "passlib" "argon2-cffi" "redis" "structlog"; do
        assert_contains "${pyproject}" "${dep}"
    done

    # pyproject.toml — chat 도메인 활성화 시 LLM 의존성 검사
    if [[ "${include_chat}" == "yes" ]]; then
        echo ""
        echo "  $(_color "── chat 도메인 LLM 의존성 (include_chat_domain=yes) ──" "1;33")"
        for dep in "sse-starlette" "langchain" "langchain-litellm" "litellm"; do
            assert_contains "${pyproject}" "${dep}"
        done
    else
        echo ""
        echo "  $(_color "── chat 도메인 LLM 의존성 미포함 검사 (include_chat_domain=no) ──" "1;33")"
        for dep in "langchain" "langchain-litellm" "litellm" "sse-starlette"; do
            assert_not_contains "${pyproject}" "${dep}"
        done
    fi

    # docker-compose.yml 필수 서비스
    local compose="${project_dir}/docker-compose.yml"
    for svc in "postgres" "redis" "mailpit" "postgres_data" "redis_data"; do
        assert_contains "${compose}" "${svc}"
    done

    # Makefile 필수 명령어
    local makefile="${project_dir}/Makefile"
    for cmd in "docker compose" "uvicorn" "alembic upgrade head"; do
        assert_contains "${makefile}" "${cmd}"
    done

    # .env.example 필수 변수
    local env_example="${project_dir}/.env.example"
    for var in "DATABASE_URL" "REDIS_URL" "JWT_SECRET_KEY" "JWT_ACCESS_TOKEN_EXPIRE_MINUTES" "JWT_REFRESH_TOKEN_EXPIRE_DAYS"; do
        assert_contains "${env_example}" "${var}"
    done

    # alembic.ini
    assert_contains "${project_dir}/alembic.ini" "script_location = alembic"

    # ── 소계 ─────────────────────────────────────────────────────────────────
    echo ""
    local scenario_pass="${PASS_COUNT}" scenario_fail="${FAIL_COUNT}"
    if [[ "${scenario_fail}" -gt 0 ]]; then
        echo "  $(_color "결과: FAIL  (일부 검사 실패)" "1;31")"
        return 1
    else
        echo "  $(_color "결과: PASS ✓" "1;32")"
        return 0
    fi
}

# ---------------------------------------------------------------------------
# 파일 트리 출력 (실패 시 디버깅용)
# ---------------------------------------------------------------------------
_dump_tree() {
    local root="$1" max_depth="${2:-4}"
    echo ""
    echo "  $(_color "[디버그] 생성된 파일 트리:" "0;90")"
    if command -v tree &>/dev/null; then
        tree -L "${max_depth}" "${root}" 2>/dev/null | head -60 | sed 's/^/  /'
    else
        find "${root}" -maxdepth "${max_depth}" | sort | head -80 | sed 's/^/  /'
    fi
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    sep2
    echo "  $(_color "Cookiecutter 비대화형 생성 및 구조 검증" "1;32")"
    echo "  $(_color "템플릿 위치: ${TEMPLATE_DIR}" "0;90")"
    sep2

    # 도구 가용성 확인
    _check_dependencies
    local method_label
    case "${CC_CMD}" in
        python_api) method_label="Python API (cookiecutter 패키지)" ;;
        uvx*)       method_label="uvx cookiecutter" ;;
        *)          method_label="${CC_CMD}" ;;
    esac
    info "실행 방법: ${method_label}"
    info "JSON 형식: 각 시나리오 파일은 {\"cookiecutter\": {...}} 구조를 사용합니다"
    info "COOKIECUTTER_SKIP_HEAVY_OPS=1 (uv sync / git init 건너뜀)"
    echo ""

    # 출력 디렉터리 설정
    if [[ -z "${OUTPUT_DIR}" ]]; then
        BASE_OUTPUT_DIR="$(mktemp -d /tmp/cc_validate_XXXXXX)"
        _CLEANUP_DIR="${BASE_OUTPUT_DIR}"
    else
        BASE_OUTPUT_DIR="${OUTPUT_DIR}"
        _CLEANUP_DIR=""
        mkdir -p "${BASE_OUTPUT_DIR}"
    fi
    info "출력 디렉터리: ${BASE_OUTPUT_DIR}"

    # 실행할 시나리오 목록 결정
    local scenarios=()
    if [[ -n "${SCENARIO_FILE}" ]]; then
        if [[ ! -f "${SCENARIO_FILE}" ]]; then
            echo ""
            fail "시나리오 파일을 찾을 수 없습니다: ${SCENARIO_FILE}"
            exit 1
        fi
        scenarios=("${SCENARIO_FILE}")
    else
        # 기본: scenarios/ 디렉터리의 모든 JSON 파일
        while IFS= read -r -d '' f; do
            scenarios+=("$f")
        done < <(find "${SCENARIOS_DIR}" -maxdepth 1 -name "*.json" -print0 | sort -z)

        if [[ ${#scenarios[@]} -eq 0 ]]; then
            echo ""
            fail "시나리오 파일이 없습니다: ${SCENARIOS_DIR}/*.json"
            exit 1
        fi
    fi

    info "검증할 시나리오: ${#scenarios[@]}개"

    # 총 카운터 (개별 시나리오 카운터와 별개)
    local total_scenarios=0
    local passed_scenarios=0
    local failed_scenarios_list=()

    for scenario_file in "${scenarios[@]}"; do
        PASS_COUNT=0
        FAIL_COUNT=0

        if validate_scenario "${scenario_file}"; then
            ((passed_scenarios++)) || true
        else
            failed_scenarios_list+=("$(basename "${scenario_file}" .json)")
            # 실패 시 파일 트리 출력
            local slug
            slug="$(_json_get "${scenario_file}" ".cookiecutter.project_slug")"
            local proj_dir="${BASE_OUTPUT_DIR}/$(basename "${scenario_file}" .json)/${slug}"
            [[ -d "${proj_dir}" ]] && _dump_tree "${proj_dir}" 3
        fi

        ((total_scenarios++)) || true
    done

    # ---------------------------------------------------------------------------
    # 최종 요약
    # ---------------------------------------------------------------------------
    echo ""
    sep2
    echo "  $(_color "최종 결과" "1;36")"
    sep
    echo "  시나리오 총수   : ${total_scenarios}"
    echo "  통과           : $(_color "${passed_scenarios}" "1;32")"
    echo "  실패           : $(_color "$((total_scenarios - passed_scenarios))" "1;31")"

    if [[ ${#failed_scenarios_list[@]} -gt 0 ]]; then
        echo ""
        echo "  $(_color "실패한 시나리오:" "1;31")"
        for name in "${failed_scenarios_list[@]}"; do
            echo "    - ${name}"
        done
    fi
    echo ""

    # 생성된 디렉터리 정리
    if [[ "${KEEP_OUTPUT}" == "false" ]] && [[ -n "${_CLEANUP_DIR}" ]]; then
        rm -rf "${_CLEANUP_DIR}"
        info "임시 디렉터리 삭제 완료: ${_CLEANUP_DIR}"
    elif [[ "${KEEP_OUTPUT}" == "true" ]]; then
        info "생성된 프로젝트 유지: ${BASE_OUTPUT_DIR}  (--keep 옵션)"
    fi

    if [[ ${#failed_scenarios_list[@]} -gt 0 ]]; then
        echo "  $(_color "FAIL" "1;31")"
        sep2
        exit 1
    else
        echo "  $(_color "모든 시나리오 통과 ✓  PASS" "1;32")"
        sep2
        exit 0
    fi
}

main "$@"
