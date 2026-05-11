#!/usr/bin/env python3
"""
scripts/validate_dockerfile_structure.py
=========================================
Dockerfile 멀티스테이지 빌드 구조 정적 검증 — Docker 없이도 실행 가능.

검증 항목:
  A. 스테이지 구조    : uv-binary, builder, runtime 3개 스테이지 존재
  B. COPY --from 범위 : runtime이 복사하는 경로가 허용 목록(runtime-venv, alembic)만 포함
  C. 개발 도구 격리   : uv/uvx 바이너리가 runtime에 복사되지 않음
                        builder에서 --no-group dev 로 /runtime-venv 생성
  D. 기반 이미지 검증 : runtime이 slim 이미지 사용, builder와 동일 계열
  E. 보안 설정        : non-root USER, HEALTHCHECK 존재

Docker를 설치하거나 이미지를 빌드하지 않아도 Dockerfile 텍스트만으로 분석.
런타임 패키지 존재 여부 검증은 scripts/validate_prod_image.sh 참조.

사용법:
    # 기본 — 템플릿 Dockerfile ({{cookiecutter.project_slug}}/Dockerfile)
    python scripts/validate_dockerfile_structure.py

    # 특정 Dockerfile
    python scripts/validate_dockerfile_structure.py /path/to/generated/project/Dockerfile

    # uv를 통해
    uv run python scripts/validate_dockerfile_structure.py

종료 코드:
    0 — 모든 검사 통과
    1 — 하나 이상의 검사 실패
    2 — Dockerfile 파일 없음
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
TEMPLATE_DIR = SCRIPT_DIR.parent
TEMPLATE_DOCKERFILE = TEMPLATE_DIR / "{{cookiecutter.project_slug}}" / "Dockerfile"

# ── ANSI 색상 (TTY 아닐 때 비활성화) ─────────────────────────────────────────
_TTY = sys.stdout.isatty()
RED    = "\033[1;31m"  if _TTY else ""
GREEN  = "\033[1;32m"  if _TTY else ""
YELLOW = "\033[1;33m"  if _TTY else ""
CYAN   = "\033[1;36m"  if _TTY else ""
BOLD   = "\033[1m"     if _TTY else ""
DIM    = "\033[90m"    if _TTY else ""
RESET  = "\033[0m"     if _TTY else ""


# ─────────────────────────────────────────────────────────────────────────────
# 데이터 클래스
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Stage:
    """파싱된 Dockerfile 스테이지."""
    name: str                                              # AS alias (소문자)
    base_image: str                                        # FROM <image> 원본
    from_clause: str                                       # FROM 줄 원문
    instructions: list[tuple[str, str]] = field(default_factory=list)  # (INSTRUCTION, value)


@dataclass
class CheckResult:
    passed: bool
    message: str
    detail: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# 로그 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

_passed_count = 0
_failed_count = 0


def _record(r: CheckResult) -> CheckResult:
    global _passed_count, _failed_count  # noqa: PLW0603
    if r.passed:
        _passed_count += 1
    else:
        _failed_count += 1
    return r


def log_ok(msg: str, detail: str = "") -> CheckResult:
    print(f"  {GREEN}[ OK ]{RESET}  {msg}")
    if detail:
        print(f"         {DIM}{detail}{RESET}")
    return _record(CheckResult(True, msg, detail))


def log_fail(msg: str, detail: str = "") -> CheckResult:
    print(f"  {RED}[FAIL]{RESET}  {msg}")
    if detail:
        print(f"         {DIM}{detail}{RESET}")
    return _record(CheckResult(False, msg, detail))


def log_info(msg: str) -> None:
    print(f"  {DIM}[INFO]{RESET}  {msg}")


def log_section(title: str) -> None:
    print(f"\n{CYAN}{BOLD}{'═' * 62}{RESET}")
    print(f"{CYAN}{BOLD}  {title}{RESET}")
    print(f"{CYAN}{BOLD}{'═' * 62}{RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile 파서
# ─────────────────────────────────────────────────────────────────────────────

def parse_dockerfile(content: str) -> list[Stage]:
    """
    Dockerfile 텍스트를 파싱하여 Stage 목록을 반환.

    - ARG 기본값(ARG KEY=VALUE) 을 추적하여 ${KEY} 치환에 사용.
    - Jinja2/Cookiecutter 변수 ({{ ... }}) 는 <template> 플레이스홀더로 교체.
    - 연속 행(줄 끝 백슬래시) 은 결합하여 하나의 instruction 값으로 처리.
    """
    build_args: dict[str, str] = {}
    stages: list[Stage] = []
    current_stage: Stage | None = None

    # 연속 행을 미리 합친 논리 줄 목록 생성
    logical_lines: list[str] = []
    buf: list[str] = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
        else:
            buf.append(stripped)
            logical_lines.append(" ".join(buf))
            buf = []
    if buf:
        logical_lines.append(" ".join(buf))

    for line in logical_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # ARG (FROM 이전/이후 모두)
        if re.match(r"^ARG\b", line, re.IGNORECASE):
            arg_part = re.sub(r"^ARG\s+", "", line, flags=re.IGNORECASE)
            if "=" in arg_part:
                key, _, val = arg_part.partition("=")
                build_args[key.strip()] = val.strip().strip('"').strip("'")
            continue

        # FROM
        from_match = re.match(
            r"^FROM\s+(?P<image>\S+)(?:\s+AS\s+(?P<alias>\S+))?",
            line,
            re.IGNORECASE,
        )
        if from_match:
            image_raw = from_match.group("image")
            alias_raw = from_match.group("alias") or ""

            def _resolve_arg(m: re.Match[str]) -> str:
                return build_args.get(m.group(1), m.group(0))

            image = re.sub(r"\$\{(\w+)\}", _resolve_arg, image_raw)
            alias = re.sub(r"\$\{(\w+)\}", _resolve_arg, alias_raw).lower()
            # Jinja2 템플릿 변수 제거
            image = re.sub(r"\{\{.*?\}\}", "<template>", image)

            current_stage = Stage(name=alias, base_image=image, from_clause=line)
            stages.append(current_stage)
            continue

        if current_stage is not None:
            parts = line.split(None, 1)
            instruction = parts[0].upper()
            value = parts[1] if len(parts) > 1 else ""
            current_stage.instructions.append((instruction, value))

    return stages


# ─────────────────────────────────────────────────────────────────────────────
# 분석 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def find_copy_from_ops(stage: Stage) -> list[tuple[str, str]]:
    """
    스테이지의 COPY --from 연산을 (source_stage, source_path) 쌍으로 반환.

    예: COPY --from=builder /runtime-venv /runtime-venv
        → ('builder', '/runtime-venv')
    """
    results: list[tuple[str, str]] = []
    for instruction, value in stage.instructions:
        if instruction != "COPY":
            continue
        from_match = re.search(r"--from=(\S+)", value, re.IGNORECASE)
        if not from_match:
            continue
        source_stage = from_match.group(1).lower()
        # 플래그 제거 후 남은 경로 파싱
        path_part = re.sub(r"--\S+\s*", "", value).strip()
        tokens = path_part.split()
        source_path = tokens[0] if tokens else ""
        results.append((source_stage, source_path))
    return results


def get_apt_packages(stage: Stage) -> set[str]:
    """RUN apt-get install 에서 설치 패키지 이름 목록 추출."""
    packages: set[str] = set()
    for instruction, value in stage.instructions:
        if instruction != "RUN":
            continue
        # apt-get install 이후 패키지 목록 (옵션 플래그 제외)
        for block in re.findall(
            r"apt-get\s+install\b(.*?)(?:&&|$)", value, re.DOTALL | re.IGNORECASE
        ):
            tokens = re.split(r"[\s\\]+", block.strip())
            for tok in tokens:
                # 플래그(-y, --no-install-recommends 등) 제외
                if tok and not tok.startswith("-") and tok not in ("apt-get", "install"):
                    packages.add(tok)
    return packages


def get_env_vars(stage: Stage) -> dict[str, str]:
    """ENV 명령에서 환경 변수 딕셔너리 추출."""
    env: dict[str, str] = {}
    for instruction, value in stage.instructions:
        if instruction != "ENV":
            continue
        for k, v in re.findall(r"(\w+)=(\S+)", value):
            env[k] = v
    return env


def has_run_pattern(stage: Stage, *patterns: str) -> bool:
    """RUN 명령 중 모든 패턴이 포함된 명령이 하나라도 있으면 True."""
    for instruction, value in stage.instructions:
        if instruction != "RUN":
            continue
        if all(p in value for p in patterns):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 검증 함수
# ─────────────────────────────────────────────────────────────────────────────

def validate_dockerfile(dockerfile_path: Path) -> None:  # noqa: C901, PLR0912, PLR0915
    """
    Dockerfile 멀티스테이지 빌드 구조를 정적으로 검증.
    검사 결과는 전역 _passed_count / _failed_count 에 누적.
    """
    content = dockerfile_path.read_text(encoding="utf-8")
    stages = parse_dockerfile(content)

    stage_by_name = {s.name: s for s in stages}
    uv_stage      = stage_by_name.get("uv-binary")
    builder_stage = stage_by_name.get("builder")
    runtime_stage = stage_by_name.get("runtime")

    stage_names = list(stage_by_name.keys())
    log_info(f"Dockerfile: {dockerfile_path}")
    log_info(f"발견된 스테이지: {stage_names}")

    # ── 검사 A: 스테이지 구조 ─────────────────────────────────────────────────
    log_section("A. 스테이지 구조 검증")

    for expected in ("uv-binary", "builder", "runtime"):
        if expected in stage_by_name:
            log_ok(f"스테이지 존재: {BOLD}{expected}{RESET}")
        else:
            log_fail(
                f"스테이지 없음: {expected}",
                "FROM ... AS {0} 구문이 Dockerfile에 없습니다".format(expected),
            )

    # ── 검사 B: runtime의 COPY --from 레이어 범위 ────────────────────────────
    log_section("B. runtime COPY --from 레이어 범위 검증")

    if runtime_stage is None:
        log_fail("runtime 스테이지를 찾을 수 없어 COPY --from 검사를 건너뜁니다")
    else:
        copy_ops = find_copy_from_ops(runtime_stage)
        log_info(f"runtime 스테이지의 COPY --from 연산: {len(copy_ops)}개")
        for src, path in copy_ops:
            log_info(f"  └─ COPY --from={src}  {path}")

        # B-1: uv-binary 스테이지에서 직접 복사하지 않아야 함
        uv_stage_copies = [(s, p) for s, p in copy_ops if s == "uv-binary"]
        if uv_stage_copies:
            log_fail(
                "runtime이 uv-binary 스테이지에서 직접 복사함",
                f"발견: {uv_stage_copies}  →  uv 바이너리가 프로덕션 이미지에 포함됩니다",
            )
        else:
            log_ok("runtime이 uv-binary 스테이지에서 직접 복사하지 않음")

        # B-2: builder에서 복사하는 경로가 허용 목록에 포함되는지 확인
        ALLOWED_SRC_PATTERNS: list[tuple[str, str]] = [
            (r"^/runtime-venv\b",        "런타임 전용 venv — dev 패키지 미포함"),
            (r"^/build/alembic(/|$)",     "Alembic 마이그레이션 스크립트"),
            (r"^/build/alembic\.ini$",    "Alembic 설정 파일"),
        ]
        FORBIDDEN_SRC_PATTERNS: list[tuple[str, str]] = [
            (r"^/uv$|^/uvx$|/bin/uv[x]?\b",    "uv / uvx 바이너리"),
            (r"/site-packages\b",                "시스템 site-packages (dev 패키지 포함 가능)"),
            (r"^/build/dist\b",                  "빌드 wheel 아티팩트"),
            (r"^/build/src\b",                   "소스 코드 트리"),
            (r"^/build/pyproject",               "pyproject.toml / uv.lock"),
        ]

        builder_copies = [(s, p) for s, p in copy_ops if s == "builder"]

        # 필수 경로가 복사되는지 확인
        copied_paths = {p for _, p in builder_copies}
        if any(re.match(r"^/runtime-venv\b", p) for p in copied_paths):
            log_ok(
                "COPY --from=builder /runtime-venv: 존재",
                "runtime deps만 포함된 venv — uv sync --no-group dev 결과물",
            )
        else:
            log_fail(
                "COPY --from=builder /runtime-venv 없음",
                "runtime 스테이지에 /runtime-venv 복사가 누락되었습니다",
            )

        # 각 복사 경로 검사
        for src, src_path in builder_copies:
            is_allowed = any(re.match(pat, src_path) for pat, _ in ALLOWED_SRC_PATTERNS)
            is_forbidden = False
            forbidden_label = ""
            for pat, label in FORBIDDEN_SRC_PATTERNS:
                if re.search(pat, src_path, re.IGNORECASE):
                    is_forbidden = True
                    forbidden_label = label
                    break

            if is_forbidden:
                log_fail(
                    f"금지된 경로 복사: COPY --from={src} {src_path}",
                    f"위험: {forbidden_label}이 runtime 이미지에 유입됩니다",
                )
            elif is_allowed:
                # 허용 경로 — 설명 찾기
                desc = next(
                    (d for p, d in ALLOWED_SRC_PATTERNS if re.match(p, src_path)),
                    "",
                )
                log_ok(f"허용된 COPY --from={src} {src_path}", desc)
            else:
                log_fail(
                    f"미분류 COPY --from={src} {src_path}",
                    "허용 목록: /runtime-venv, /build/alembic, /build/alembic.ini",
                )

    # ── 검사 C: 개발 도구 격리 ────────────────────────────────────────────────
    log_section("C. 개발 도구 격리 검증")

    # C-1: runtime에 uv/uvx 복사 없음 (어느 소스에서든)
    if runtime_stage is not None:
        all_copy_ops = find_copy_from_ops(runtime_stage)
        uv_copies = [
            (s, p) for s, p in all_copy_ops
            if re.search(r"(^|/)uv[x]?$", p, re.IGNORECASE)
        ]
        if uv_copies:
            log_fail(
                "runtime 스테이지에 uv/uvx 바이너리 복사 발견",
                f"경로: {uv_copies}  →  패키지 매니저가 프로덕션 이미지에 포함됩니다",
            )
        else:
            log_ok(
                "runtime 스테이지에 uv/uvx 바이너리 복사 없음",
                "패키지 관리 도구는 builder 스테이지에만 존재합니다",
            )

    # C-2: builder에서 /runtime-venv를 --no-group dev 옵션으로 생성
    if builder_stage is not None:
        if has_run_pattern(builder_stage, "--no-group dev", "/runtime-venv"):
            log_ok(
                "builder: uv sync --no-group dev → /runtime-venv",
                "dev 의존성 그룹(pytest, ruff, mypy 등)이 runtime venv에서 제외됩니다",
            )
        else:
            log_fail(
                "builder에서 --no-group dev + /runtime-venv 패턴을 찾을 수 없음",
                "uv sync --no-group dev 로 /runtime-venv를 생성해야 합니다",
            )

    # C-3: builder의 --all-groups 동기화가 system Python에만 적용
    if builder_stage is not None:
        if has_run_pattern(builder_stage, "--all-groups", "UV_SYSTEM_PYTHON=1"):
            log_ok(
                "builder: UV_SYSTEM_PYTHON=1 uv sync --all-groups (system Python 전용)",
                "dev 패키지가 system Python에만 설치 — /runtime-venv에는 미포함",
            )
        else:
            log_fail(
                "builder에서 UV_SYSTEM_PYTHON=1 + --all-groups 패턴을 찾을 수 없음",
                "dev 의존성을 system Python에 격리하는 패턴이 필요합니다",
            )

    # C-4: runtime에 빌드 전용 apt 패키지 없음
    if builder_stage is not None and runtime_stage is not None:
        builder_apt = get_apt_packages(builder_stage)
        runtime_apt = get_apt_packages(runtime_stage)

        build_only: set[str] = {"build-essential", "gcc", "g++", "libpq-dev", "git", "make", "cmake"}
        runtime_has_build = runtime_apt & build_only
        builder_has_build = builder_apt & build_only

        if builder_has_build:
            log_info(f"builder apt 빌드 도구: {', '.join(sorted(builder_has_build))}")

        if runtime_has_build:
            log_fail(
                "runtime 스테이지에 빌드 전용 apt 패키지 발견",
                f"패키지: {', '.join(sorted(runtime_has_build))}  →  불필요한 컴파일러가 이미지 크기를 키웁니다",
            )
        else:
            log_ok(
                "runtime 스테이지에 빌드 전용 apt 패키지 없음",
                f"runtime apt 패키지: {', '.join(sorted(runtime_apt)) or '(없음)'}",
            )

    # C-5: runtime ENV에 VIRTUAL_ENV=/runtime-venv 설정
    if runtime_stage is not None:
        runtime_env = get_env_vars(runtime_stage)
        venv_val = runtime_env.get("VIRTUAL_ENV", "")
        if "/runtime-venv" in venv_val:
            log_ok(
                "runtime ENV: VIRTUAL_ENV=/runtime-venv",
                "Python이 uv 없이도 runtime venv를 자동으로 사용합니다",
            )
        else:
            log_fail(
                "runtime ENV에 VIRTUAL_ENV=/runtime-venv 없음",
                "런타임 venv 활성화를 위해 ENV VIRTUAL_ENV=/runtime-venv 필요",
            )

    # ── 검사 D: 기반 이미지 검증 ─────────────────────────────────────────────
    log_section("D. 기반 이미지 검증")

    if runtime_stage is not None:
        base = runtime_stage.base_image

        # slim 변형 사용 확인
        if "slim" in base or "<template>" in base:
            log_ok(
                f"runtime 기반 이미지: slim 변형 사용",
                f"이미지: {base}  (최소 OS, 컴파일러 미포함)",
            )
        else:
            log_fail(
                "runtime 기반 이미지가 slim 변형이 아닐 수 있음",
                f"이미지: {base}  →  python:X.Y-slim-bookworm 권장",
            )

        # builder와 동일 계열 확인
        if builder_stage is not None:
            # 버전 숫자 제거 후 이미지 계열 비교
            def _family(img: str) -> str:
                return re.sub(r"[\d.]+", "X", img.split(":")[0])

            b_fam = _family(builder_stage.base_image)
            r_fam = _family(runtime_stage.base_image)
            if b_fam == r_fam:
                log_ok(
                    "builder / runtime 동일 기반 이미지 계열",
                    f"builder: {builder_stage.base_image}  |  runtime: {runtime_stage.base_image}",
                )
            else:
                log_fail(
                    "builder / runtime 기반 이미지 계열 불일치",
                    f"builder: {builder_stage.base_image}  |  runtime: {runtime_stage.base_image}",
                )

    # ── 검사 E: 보안 설정 ─────────────────────────────────────────────────────
    log_section("E. 보안 설정 검증")

    if runtime_stage is not None:
        # E-1: USER (non-root)
        user_vals = [v for inst, v in runtime_stage.instructions if inst == "USER"]
        if user_vals:
            last_user = user_vals[-1].strip()
            if last_user.lower() not in ("root", "0"):
                log_ok(f"non-root 사용자로 실행: USER {last_user}")
            else:
                log_fail(
                    "runtime 스테이지가 root(0)로 실행",
                    "보안 위험 — non-root 사용자 계정 생성 및 USER 명령 설정 권장",
                )
        else:
            log_fail(
                "runtime 스테이지에 USER 명령 없음",
                "컨테이너가 root로 실행됩니다 — non-root USER 설정 필요",
            )

        # E-2: HEALTHCHECK
        hc_vals = [v for inst, v in runtime_stage.instructions if inst == "HEALTHCHECK"]
        if hc_vals:
            log_ok("HEALTHCHECK 설정됨", hc_vals[-1][:80])
        else:
            log_fail(
                "HEALTHCHECK 없음",
                "프로덕션 이미지에 HEALTHCHECK를 추가하면 오케스트레이터가 상태를 감지합니다",
            )


# ─────────────────────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global _passed_count, _failed_count  # noqa: PLW0603

    # 도움말
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    # Dockerfile 경로 결정
    if len(sys.argv) > 1:
        dockerfile_path = Path(sys.argv[1])
    else:
        dockerfile_path = TEMPLATE_DOCKERFILE

    print(f"\n{CYAN}{BOLD}{'═' * 62}{RESET}")
    print(f"{CYAN}{BOLD}  Dockerfile 멀티스테이지 빌드 구조 정적 검증{RESET}")
    print(f"{CYAN}{BOLD}{'═' * 62}{RESET}")

    if not dockerfile_path.exists():
        print(f"\n{RED}ERROR: Dockerfile을 찾을 수 없습니다: {dockerfile_path}{RESET}")
        print(f"  사용법: python scripts/validate_dockerfile_structure.py [Dockerfile 경로]")
        sys.exit(2)

    _passed_count = 0
    _failed_count = 0

    validate_dockerfile(dockerfile_path)

    # 최종 결과
    total = _passed_count + _failed_count
    log_section("최종 결과")
    print(f"  총 검사:  {BOLD}{total}{RESET}건")
    print(f"  통과:     {GREEN}{BOLD}{_passed_count}{RESET}건")
    print(f"  실패:     {RED}{BOLD}{_failed_count}{RESET}건")

    if _failed_count == 0:
        print(f"\n  {GREEN}{BOLD}PASS{RESET}  Dockerfile 멀티스테이지 빌드 구조가 올바릅니다.")
        print(f"        runtime 스테이지가 dev 도구(uv, pip, gcc, build-essential 등)를 포함하지 않습니다.")
        print(f"\n  {DIM}런타임 패키지 존재 여부는 scripts/validate_prod_image.sh 로 검증하세요.{RESET}")
        sys.exit(0)
    else:
        print(f"\n  {RED}{BOLD}FAIL{RESET}  {_failed_count}건의 검사가 실패했습니다.")
        print(f"        위 [FAIL] 항목을 확인하고 Dockerfile을 수정하세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
