#!/usr/bin/env python3
"""
validate_template.py — Cookiecutter 템플릿 생성 검증 스크립트

지정된 기본값(및 토글 조합)으로 cookiecutter 템플릿을 생성한 뒤,
필수 파일/디렉터리 구조가 올바르게 생성되었는지 assert합니다.

사용법:
    # 템플릿 루트에서 실행
    python scripts/validate_template.py

    # 또는 uv를 통해
    uv run python scripts/validate_template.py

요구사항:
    cookiecutter >= 2.1  (pip install cookiecutter)
    Python >= 3.12

종료 코드:
    0 — 모든 시나리오 통과
    1 — 하나 이상의 시나리오 실패
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 경로 설정
# ---------------------------------------------------------------------------
SCRIPT_DIR: Path = Path(__file__).parent.resolve()
TEMPLATE_DIR: Path = SCRIPT_DIR.parent  # cookiecutter.json 이 있는 디렉터리

# ANSI 색상 (TTY 아닐 때는 비활성화)
_USE_COLOR: bool = (
    sys.platform != "win32"
    and hasattr(sys.stdout, "fileno")
    and os.isatty(sys.stdout.fileno())
)


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


PASS = _c("PASS", "1;32")
FAIL = _c("FAIL", "1;31")
SKIP = _c("SKIP", "1;33")


# ---------------------------------------------------------------------------
# 시나리오 정의
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """단일 템플릿 생성 시나리오와 검증 기준."""

    name: str
    description: str
    extra_context: dict[str, Any]

    # 반드시 존재해야 하는 경로 (파일 또는 디렉터리)
    required_paths: list[str] = field(default_factory=list)

    # 반드시 존재하지 말아야 하는 경로
    forbidden_paths: list[str] = field(default_factory=list)

    # 파일 내용 검사: {상대경로: [포함해야 할 문자열들]}
    content_contains: dict[str, list[str]] = field(default_factory=dict)

    # 파일 내용 검사: {상대경로: [포함하지 말아야 할 문자열들]}
    content_excludes: dict[str, list[str]] = field(default_factory=dict)


# 공통 필수 경로 (모든 시나리오에서 존재해야 함)
_COMMON_REQUIRED: list[str] = [
    # 루트 설정 파일
    "pyproject.toml",
    "docker-compose.yml",
    "Dockerfile",
    "Makefile",
    "README.md",
    "alembic.ini",
    ".env.example",
    ".dockerignore",
    ".gitignore",
    # Alembic
    "alembic",
    "alembic/versions",
    # 소스 패키지 구조
    "src",
    "src/{pkg}",
    "src/{pkg}/__init__.py",
    "src/{pkg}/core",
    "src/{pkg}/core/__init__.py",
    "src/{pkg}/domains",
    "src/{pkg}/domains/__init__.py",
    "src/{pkg}/domains/auth",
    "src/{pkg}/domains/auth/__init__.py",
    # 테스트
    "tests",
    "tests/__init__.py",
    "tests/auth",
    "tests/auth/__init__.py",
    # scripts
    "scripts",
    "scripts/wait_for_services.sh",
    "scripts/wait_for_services.py",
]

# Dockerfile 멀티스테이지 빌드 검증 — COPY --from 레이어 범위
# runtime 스테이지가 dev 도구를 복사하지 않는지 정적으로 확인
_DOCKERFILE_MULTISTAGE_CHECKS: dict[str, list[str]] = {
    "Dockerfile": [
        # 3개 스테이지 존재 확인
        "AS uv-binary",
        "AS builder",
        "AS runtime",
        # runtime COPY --from 허용 경로만 존재
        "COPY --from=builder /runtime-venv /runtime-venv",
        "COPY --from=builder /build/alembic/",
        "COPY --from=builder /build/alembic.ini",
        # builder에서 dev 패키지 격리
        "--no-group dev",
        "UV_SYSTEM_PYTHON=1",
        # 보안 설정
        "USER appuser",
        "HEALTHCHECK",
        # runtime에 slim 이미지 사용 (bookworm 기반)
        "slim-bookworm AS runtime",
    ],
}

# 기본 패키지 이름 (cookiecutter.json의 project_name → project_slug → package_name 변환)
# "FastAPI Bootstrap" → "fastapi-bootstrap" → "fastapi_bootstrap"
_DEFAULT_PKG = "fastapi_bootstrap"
_DEFAULT_SLUG = "fastapi-bootstrap"


def _resolve_paths(paths: list[str], pkg: str) -> list[str]:
    """경로 템플릿 내 {pkg} 를 실제 패키지명으로 치환."""
    return [p.format(pkg=pkg) for p in paths]


SCENARIOS: list[Scenario] = [
    # ------------------------------------------------------------------
    # 시나리오 1: 기본값 (모든 기능 활성화)
    # ------------------------------------------------------------------
    Scenario(
        name="default_all_features",
        description="기본값으로 생성 — chat 도메인 포함, google+kakao+naver OAuth, pre-commit 포함",
        extra_context={},  # 모든 기본값 사용
        required_paths=_COMMON_REQUIRED + [
            # chat 도메인
            "src/{pkg}/domains/chat",
            "src/{pkg}/domains/chat/__init__.py",
            "src/{pkg}/domains/chat/llm_factory.py",
            "src/{pkg}/domains/chat/llm_client.py",
            "tests/chat",
            "tests/chat/__init__.py",
            "tests/chat/test_llm_factory.py",
            "tests/chat/test_llm_client.py",
            # OAuth
            "src/{pkg}/domains/auth/oauth",
            "src/{pkg}/domains/auth/oauth/__init__.py",
            # pre-commit
            ".pre-commit-config.yaml",
        ],
        forbidden_paths=[],
        content_contains={
            "pyproject.toml": [
                "fastapi",
                "sqlalchemy",
                "alembic",
                "passlib",
                "argon2-cffi",
                "redis",
                "structlog",
                "sse-starlette",
                "langchain",           # chat 도메인 활성화
                "langchain-litellm",   # chat 도메인 활성화
            ],
            "docker-compose.yml": [
                "postgres",
                "redis",
                "mailpit",
                "postgres_data",
                "redis_data",
            ],
            "Makefile": [
                "docker compose",
                "uvicorn",
                "alembic upgrade head",
            ],
            ".env.example": [
                "DATABASE_URL",
                "REDIS_URL",
                "JWT_SECRET_KEY",
                "JWT_ACCESS_TOKEN_EXPIRE_MINUTES",
                "JWT_REFRESH_TOKEN_EXPIRE_DAYS",
            ],
            "alembic.ini": [
                "script_location = alembic",
            ],
            ".pre-commit-config.yaml": [
                "ruff",
                "mypy",
            ],
            # ── Dockerfile 멀티스테이지 빌드 구조 검증 (Sub-AC 3.1) ──────────────
            # runtime 스테이지가 builder의 dev 도구를 복사하지 않는지 확인:
            #   • 3개 스테이지(uv-binary / builder / runtime) 존재
            #   • runtime COPY --from 경로가 /runtime-venv + alembic 만
            #   • uv sync --no-group dev 로 /runtime-venv 생성
            #   • non-root USER + HEALTHCHECK 설정
            **_DOCKERFILE_MULTISTAGE_CHECKS,
        },
    ),

    # ------------------------------------------------------------------
    # 시나리오 2: chat 도메인 제외 (include_chat_domain=no)
    # ------------------------------------------------------------------
    Scenario(
        name="no_chat_domain",
        description="include_chat_domain=no — chat 패키지와 LLM 의존성 제거 검증",
        extra_context={"include_chat_domain": "no"},
        required_paths=_COMMON_REQUIRED,
        forbidden_paths=[
            # chat 도메인 소스 제거
            "src/{pkg}/domains/chat",
            "src/{pkg}/domains/chat/__init__.py",
            # chat 테스트 제거
            "tests/chat",
            "tests/chat/__init__.py",
        ],
        content_contains={
            # Dockerfile 멀티스테이지 구조는 chat 토글에 무관하게 동일해야 함
            # (Dockerfile은 Jinja2 conditional 없이 정적으로 동일한 3스테이지 구조 유지)
            **_DOCKERFILE_MULTISTAGE_CHECKS,
        },
        content_excludes={
            "pyproject.toml": [
                "langchain",
                "langchain-litellm",
                "litellm",
            ],
            # NOTE: Dockerfile에는 langchain 미포함 검사를 하지 않음.
            #       Dockerfile은 chat 토글을 위한 Jinja2 conditional 블록이 없고,
            #       "langchain" 단어가 주석에서 옵션 설명으로 등장하기 때문입니다.
            #       LLM 의존성 제거는 pyproject.toml에서 Jinja2 conditional로 처리됩니다.
        },
    ),

    # ------------------------------------------------------------------
    # 시나리오 3: google OAuth만 선택
    # ------------------------------------------------------------------
    Scenario(
        name="google_oauth_only",
        description="oauth_providers=google — kakao/naver 어댑터 파일 잔존 0건 검증",
        extra_context={"oauth_providers": "google"},
        required_paths=_COMMON_REQUIRED + [
            "src/{pkg}/domains/auth/oauth",
            "src/{pkg}/domains/auth/oauth/__init__.py",
        ],
        forbidden_paths=[
            # 미선택 provider 어댑터 파일 (구현 파일이 추가될 때 동작 확인)
            "src/{pkg}/domains/auth/oauth/kakao.py",
            "src/{pkg}/domains/auth/oauth/naver.py",
        ],
    ),

    # ------------------------------------------------------------------
    # 시나리오 4: OAuth 완전 제거 (oauth_providers=none)
    # ------------------------------------------------------------------
    Scenario(
        name="no_oauth",
        description="oauth_providers=none — oauth/ 디렉터리 전체 제거 검증",
        extra_context={"oauth_providers": "none"},
        required_paths=_COMMON_REQUIRED,
        forbidden_paths=[
            "src/{pkg}/domains/auth/oauth",
        ],
    ),

    # ------------------------------------------------------------------
    # 시나리오 5: pre-commit 제외 (use_pre_commit=no)
    # ------------------------------------------------------------------
    Scenario(
        name="no_pre_commit",
        description="use_pre_commit=no — .pre-commit-config.yaml 제거 검증",
        extra_context={"use_pre_commit": "no"},
        required_paths=_COMMON_REQUIRED,
        forbidden_paths=[
            ".pre-commit-config.yaml",
        ],
    ),

    # ------------------------------------------------------------------
    # 시나리오 6: chat 없음 + google OAuth 만 + pre-commit 없음 (복합)
    # ------------------------------------------------------------------
    Scenario(
        name="minimal_no_chat_no_precommit",
        description="최소 설정 — chat 없음, google OAuth 만, pre-commit 없음",
        extra_context={
            "include_chat_domain": "no",
            "oauth_providers": "google",
            "use_pre_commit": "no",
        },
        required_paths=_COMMON_REQUIRED + [
            "src/{pkg}/domains/auth/oauth",
            "src/{pkg}/domains/auth/oauth/__init__.py",
        ],
        forbidden_paths=[
            "src/{pkg}/domains/chat",
            "tests/chat",
            ".pre-commit-config.yaml",
            "src/{pkg}/domains/auth/oauth/kakao.py",
            "src/{pkg}/domains/auth/oauth/naver.py",
        ],
        content_excludes={
            "pyproject.toml": [
                "langchain",
                "langchain-litellm",
            ],
        },
    ),
]


# ---------------------------------------------------------------------------
# 생성 및 검증 함수
# ---------------------------------------------------------------------------

def _find_cookiecutter_cmd() -> list[str] | None:
    """
    사용 가능한 cookiecutter 실행 명령을 탐색합니다.

    우선순위:
    1. Python API (cookiecutter 패키지가 임포트 가능할 때)
    2. uvx cookiecutter
    3. PATH의 cookiecutter 바이너리
    """
    # 1) Python API
    try:
        import cookiecutter  # noqa: F401
        return ["__python_api__"]
    except ImportError:
        pass

    # 2) uvx
    if shutil.which("uvx"):
        return ["uvx", "cookiecutter"]

    # 3) cookiecutter CLI
    if shutil.which("cookiecutter"):
        return ["cookiecutter"]

    return None


def run_cookiecutter(
    template_dir: Path,
    output_dir: Path,
    extra_context: dict[str, Any],
) -> Path:
    """
    cookiecutter를 통해 프로젝트를 생성하고 생성된 프로젝트 경로를 반환.

    COOKIECUTTER_SKIP_HEAVY_OPS=1 환경변수를 설정해 uv sync / git init을 건너뜁니다.

    실행 방법 우선순위: Python API → uvx cookiecutter → cookiecutter CLI
    """
    import subprocess

    cmd_prefix = _find_cookiecutter_cmd()
    if cmd_prefix is None:
        print(
            _c(
                "ERROR: cookiecutter를 찾을 수 없습니다.\n"
                "  설치: pip install cookiecutter  또는  uv add --dev cookiecutter",
                "1;31",
            )
        )
        sys.exit(2)

    # 공통 환경변수: 훅에서 uv sync / git init 건너뜀
    env = {**os.environ, "COOKIECUTTER_SKIP_HEAVY_OPS": "1"}

    # --- Python API 경로 ---
    if cmd_prefix == ["__python_api__"]:
        from cookiecutter.main import cookiecutter  # type: ignore[import]

        # cookiecutter 는 os.environ을 직접 참조하므로 임시 패치
        _orig: dict[str, str | None] = {}
        for k, v in env.items():
            _orig[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            result: str = cookiecutter(
                template=str(template_dir),
                no_input=True,
                extra_context=extra_context or {},
                output_dir=str(output_dir),
                overwrite_if_exists=True,
            )
        finally:
            for k, v in _orig.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return Path(result)

    # --- CLI 경로 (uvx cookiecutter 또는 cookiecutter) ---
    cmd: list[str] = [
        *cmd_prefix,
        "--no-input",
        "--output-dir", str(output_dir),
        str(template_dir),
    ]
    # extra_context는 key=value 위치 인수로 전달
    for k, v in (extra_context or {}).items():
        cmd.append(f"{k}={v}")

    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"cookiecutter 실행 실패 (exit {proc.returncode}):\n"
            f"  stdout: {proc.stdout.strip()}\n"
            f"  stderr: {proc.stderr.strip()}"
        )

    # 생성된 프로젝트 디렉터리를 추측: output_dir 안에 새로 생긴 디렉터리
    # cookiecutter 출력에 생성된 경로가 나오면 파싱하고, 아니면 디렉터리를 탐색
    before = set(output_dir.iterdir()) if output_dir.exists() else set()
    # cookiecutter는 stdout 에 생성된 경로를 출력하기도 함
    for line in proc.stdout.splitlines():
        candidate = Path(line.strip())
        if candidate.exists() and candidate.parent == output_dir:
            return candidate

    # stdout에 없으면 output_dir 내 디렉터리 탐색
    dirs = [d for d in output_dir.iterdir() if d.is_dir()]
    if len(dirs) == 1:
        return dirs[0]
    if dirs:
        # 가장 최근 수정된 디렉터리 반환
        return max(dirs, key=lambda d: d.stat().st_mtime)

    raise RuntimeError(
        f"cookiecutter 실행 후 output_dir({output_dir})에서 생성된 디렉터리를 찾을 수 없습니다."
    )


@dataclass
class AssertionResult:
    """단일 assert 결과."""
    passed: bool
    message: str


def check_path(project_dir: Path, rel_path: str, should_exist: bool) -> AssertionResult:
    """경로 존재 여부를 검사."""
    p = project_dir / rel_path
    exists = p.exists()
    if should_exist:
        if exists:
            return AssertionResult(True, f"  {_c('✓', '32')} exists:   {rel_path}")
        else:
            return AssertionResult(False, f"  {_c('✗', '31')} MISSING:  {rel_path}")
    else:
        if not exists:
            return AssertionResult(True, f"  {_c('✓', '32')} absent:   {rel_path}")
        else:
            return AssertionResult(False, f"  {_c('✗', '31')} SHOULD NOT EXIST: {rel_path}")


def check_content(
    project_dir: Path,
    rel_path: str,
    expected_strings: list[str],
    must_contain: bool,
) -> list[AssertionResult]:
    """파일 내용 포함/미포함 여부를 검사."""
    p = project_dir / rel_path
    if not p.exists():
        return [AssertionResult(
            False,
            f"  {_c('✗', '31')} content-check skipped (file missing): {rel_path}",
        )]

    content = p.read_text(encoding="utf-8")
    results: list[AssertionResult] = []
    for needle in expected_strings:
        found = needle in content
        if must_contain:
            ok = found
            label = "contains" if ok else "MISSING_IN"
        else:
            ok = not found
            label = "absent_in" if ok else "FOUND_IN"
        verb = _c("✓", "32") if ok else _c("✗", "31")
        results.append(AssertionResult(
            ok,
            f"  {verb} {label}: '{needle}'  ({rel_path})",
        ))
    return results


def validate_scenario(scenario: Scenario, tmp_root: Path) -> tuple[int, int]:
    """
    단일 시나리오를 생성·검증한다.

    Returns:
        (passed_count, failed_count)
    """
    print(f"\n{'═' * 64}")
    print(f"  시나리오: {_c(scenario.name, '1;36')}")
    print(f"  {scenario.description}")
    print(f"{'─' * 64}")

    # 임시 출력 디렉터리 (시나리오별)
    output_dir = tmp_root / scenario.name
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. 생성 ---
    print(f"  {_c('[생성]', '33')} cookiecutter 실행 중...", end=" ", flush=True)
    try:
        project_dir = run_cookiecutter(
            template_dir=TEMPLATE_DIR,
            output_dir=output_dir,
            extra_context=scenario.extra_context,
        )
        print(_c("완료", "32"))
    except Exception:  # noqa: BLE001
        print(_c("실패", "31"))
        traceback.print_exc()
        return 0, 1

    # 실제 패키지명 파악 (src/ 디렉터리에서 직접 탐색 — pyproject.toml의 name 필드는
    # hyphens 를 사용하지만 Python 패키지 디렉터리명은 underscores 를 사용한다.
    # 예: name = "fastapi-bootstrap" → src/fastapi_bootstrap/
    pkg_name = _DEFAULT_PKG
    src_dir = project_dir / "src"
    if src_dir.is_dir():
        pkg_dirs = [d for d in src_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if pkg_dirs:
            pkg_name = pkg_dirs[0].name   # 첫 번째 패키지 디렉터리 사용
    elif (project_dir / "pyproject.toml").exists():
        # fallback: pyproject.toml의 name 필드에서 추출하고 hyphens → underscores 변환
        content = (project_dir / "pyproject.toml").read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("name") and "=" in stripped:
                raw_name = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                pkg_name = raw_name.replace("-", "_")
                break

    print(f"  {_c('[정보]', '90')} 생성된 프로젝트: {project_dir.name}  (패키지: {pkg_name})")

    # --- 2. 경로 검사 ---
    passed = 0
    failed = 0

    print(f"\n  {_c('── 필수 경로 ──', '33')}")
    for rel in _resolve_paths(scenario.required_paths, pkg_name):
        result = check_path(project_dir, rel, should_exist=True)
        print(result.message)
        if result.passed:
            passed += 1
        else:
            failed += 1

    if scenario.forbidden_paths:
        print(f"\n  {_c('── 금지 경로 (존재하면 안 됨) ──', '33')}")
        for rel in _resolve_paths(scenario.forbidden_paths, pkg_name):
            result = check_path(project_dir, rel, should_exist=False)
            print(result.message)
            if result.passed:
                passed += 1
            else:
                failed += 1

    # --- 3. 내용 검사 (포함) ---
    if scenario.content_contains:
        print(f"\n  {_c('── 파일 내용 포함 검사 ──', '33')}")
        for rel_path, needles in scenario.content_contains.items():
            resolved = rel_path.format(pkg=pkg_name)
            for r in check_content(project_dir, resolved, needles, must_contain=True):
                print(r.message)
                if r.passed:
                    passed += 1
                else:
                    failed += 1

    # --- 4. 내용 검사 (미포함) ---
    if scenario.content_excludes:
        print(f"\n  {_c('── 파일 내용 미포함 검사 ──', '33')}")
        for rel_path, needles in scenario.content_excludes.items():
            resolved = rel_path.format(pkg=pkg_name)
            for r in check_content(project_dir, resolved, needles, must_contain=False):
                print(r.message)
                if r.passed:
                    passed += 1
                else:
                    failed += 1

    # --- 소계 ---
    total = passed + failed
    status = PASS if failed == 0 else FAIL
    print(f"\n  결과: {status}  ({passed}/{total} 통과)")

    return passed, failed


# ---------------------------------------------------------------------------
# 진단 유틸리티
# ---------------------------------------------------------------------------

def _dump_tree(root: Path, max_depth: int = 4, indent: str = "  ") -> None:
    """실패 시 생성된 디렉터리 트리를 출력해 디버깅을 돕는다."""
    print(f"\n  {_c('[디버그] 생성된 파일 트리:', '90')}")

    def _walk(p: Path, depth: int, prefix: str) -> None:
        if depth > max_depth:
            return
        children = sorted(p.iterdir()) if p.is_dir() else []
        for i, child in enumerate(children):
            connector = "└── " if i == len(children) - 1 else "├── "
            print(f"  {prefix}{connector}{child.name}")
            if child.is_dir():
                extension = "    " if i == len(children) - 1 else "│   "
                _walk(child, depth + 1, prefix + extension)

    _walk(root, 1, indent)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    print(_c("=" * 64, "1;36"))
    print(_c("  Cookiecutter 템플릿 생성 검증", "1;32"))
    print(_c(f"  템플릿 위치: {TEMPLATE_DIR}", "90"))
    print(_c("=" * 64, "1;36"))

    # cookiecutter 사용 가능 여부 확인
    cmd_info = _find_cookiecutter_cmd()
    if cmd_info is None:
        print(_c(
            "\nERROR: cookiecutter를 찾을 수 없습니다.\n"
            "  pip install cookiecutter  또는  uv add --dev cookiecutter\n"
            "  또는 uvx(uv)가 설치되어 있으면 자동으로 uvx cookiecutter를 사용합니다.",
            "1;31",
        ))
        sys.exit(2)
    method = "Python API" if cmd_info == ["__python_api__"] else " ".join(cmd_info)
    print(f"  {_c('[실행 방법]', '90')} {method}")

    total_passed = 0
    total_failed = 0
    failed_scenarios: list[str] = []

    with tempfile.TemporaryDirectory(prefix="cc_validate_") as tmp:
        tmp_root = Path(tmp)

        for scenario in SCENARIOS:
            try:
                p, f = validate_scenario(scenario, tmp_root)
            except Exception:  # noqa: BLE001
                print(_c(f"\n  [예외] 시나리오 '{scenario.name}' 실행 중 예외 발생:", "1;31"))
                traceback.print_exc()
                p, f = 0, 1

            total_passed += p
            total_failed += f
            if f > 0:
                failed_scenarios.append(scenario.name)

            # 실패 시 파일 트리 출력 (디버깅용)
            if f > 0:
                scenario_dir = tmp_root / scenario.name
                if scenario_dir.exists():
                    subdirs = list(scenario_dir.iterdir())
                    if subdirs:
                        _dump_tree(subdirs[0])

    # ---------------------------------------------------------------------------
    # 최종 요약
    # ---------------------------------------------------------------------------
    print(f"\n{'═' * 64}")
    print(_c("  최종 결과", "1;36"))
    print(f"{'─' * 64}")
    total = total_passed + total_failed
    print(f"  총 검사: {total}  통과: {_c(str(total_passed), '32')}  실패: {_c(str(total_failed), '31')}")

    if failed_scenarios:
        print(f"\n  {_c('실패한 시나리오:', '1;31')}")
        for name in failed_scenarios:
            print(f"    - {name}")
        print(f"\n  {FAIL}")
        sys.exit(1)
    else:
        print(f"\n  {_c('모든 시나리오 통과 ✓', '1;32')}")
        print(f"\n  {PASS}")
        sys.exit(0)


if __name__ == "__main__":
    main()
