#!/usr/bin/env python
"""
post_gen_project.py — Cookiecutter post-generation cleanup hook.

Runs immediately after template rendering in the generated project directory.
Removes files/directories that correspond to disabled features based on the
user's template variable choices:

  - include_chat_domain=no   → remove chat domain + chat tests + LLM deps note
  - oauth_providers=<subset> → remove adapter files for unselected providers
  - oauth_providers=none     → remove entire oauth/ directory
  - use_pre_commit=no        → remove .pre-commit-config.yaml

Also performs one-time bootstrap:
  - .env.example → .env  (copy, skip if .env already exists)
  - uv sync              (install all deps into .venv; skip gracefully if uv absent)
  - git init + initial commit
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Rendered template variables
# Cookiecutter substitutes these before the script is executed.
# ---------------------------------------------------------------------------
PACKAGE_NAME: str = "{{ cookiecutter.package_name }}"
PROJECT_SLUG: str = "{{ cookiecutter.project_slug }}"
FASTAPI_HOST: str = "{{ cookiecutter.fastapi_host }}"
FASTAPI_PORT: str = "{{ cookiecutter.fastapi_port }}"
INCLUDE_CHAT_DOMAIN: str = "{{ cookiecutter.include_chat_domain }}"   # "yes" | "no"
LLM_PROVIDER: str = "{{ cookiecutter.llm_provider }}"                 # openai | anthropic | gemini | azure | ollama
OAUTH_PROVIDERS_RAW: str = "{{ cookiecutter.oauth_providers }}"        # "google,kakao,naver" | "none" | ...
USE_PRE_COMMIT: str = "{{ cookiecutter.use_pre_commit }}"              # "yes" | "no"
MAILPIT_UI_PORT: str = "{{ cookiecutter.mailpit_ui_port }}"            # default 8025

# Cookiecutter sets CWD to the generated project root before running hooks.
PROJECT_ROOT: Path = Path.cwd().resolve()

# All supported OAuth providers (must match adapter filenames in oauth/)
ALL_OAUTH_PROVIDERS: frozenset[str] = frozenset({"google", "kakao", "naver"})

# ---------------------------------------------------------------------------
# Test-mode gate
# ---------------------------------------------------------------------------
# Set COOKIECUTTER_SKIP_HEAVY_OPS=1 to skip uv sync and git init.
# This speeds up template validation/CI runs while still running cleanup hooks.
_SKIP_HEAVY_OPS: bool = os.environ.get("COOKIECUTTER_SKIP_HEAVY_OPS", "0") == "1"

# ANSI colors — disabled on Windows or when not a tty
_USE_COLOR: bool = sys.platform != "win32" and (
    os.isatty(sys.stdout.fileno()) if hasattr(sys.stdout, "fileno") else False
)


def _c(text: str, code: str) -> str:
    """Wrap text in an ANSI escape code if color output is enabled."""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _ok(msg: str) -> str:
    return _c(f"  ✓  {msg}", "32")   # green


def _rm(msg: str) -> str:
    return _c(f"  ✗  {msg}", "33")   # yellow


def _warn(msg: str) -> str:
    return _c(f"  !  {msg}", "31")   # red


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def remove_path(path: Path) -> None:
    """Remove a file or directory tree.  Silently skips non-existent paths."""
    try:
        rel = path.relative_to(PROJECT_ROOT)
    except ValueError:
        rel = path

    if path.is_dir():
        shutil.rmtree(path)
        print(_rm(f"removed dir   {rel}"))
    elif path.is_file():
        path.unlink()
        print(_rm(f"removed file  {rel}"))
    # else: path doesn't exist — nothing to do (idempotent)


def remove_paths(*paths: Path) -> None:
    for p in paths:
        remove_path(p)


# ---------------------------------------------------------------------------
# Step 1 — Chat domain
# ---------------------------------------------------------------------------

def cleanup_chat_domain() -> None:
    """Remove chat domain code and tests when include_chat_domain=no."""
    if INCLUDE_CHAT_DOMAIN == "yes":
        return

    print("\n[step 1/6] include_chat_domain=no  →  removing chat domain …")

    # Domain source files
    remove_path(PROJECT_ROOT / "src" / PACKAGE_NAME / "domains" / "chat")

    # Test directory
    remove_path(PROJECT_ROOT / "tests" / "chat")

    print(_ok("Chat domain removed.  LangChain/litellm deps already excluded via pyproject.toml conditional."))


# ---------------------------------------------------------------------------
# Step 2 — OAuth providers
# ---------------------------------------------------------------------------

def cleanup_oauth_providers() -> None:
    """Remove adapter files for unselected OAuth providers."""
    oauth_src_dir: Path = (
        PROJECT_ROOT / "src" / PACKAGE_NAME / "domains" / "auth" / "oauth"
    )
    oauth_test_dir: Path = PROJECT_ROOT / "tests" / "auth"

    normalized: str = OAUTH_PROVIDERS_RAW.strip().lower()

    # ── 2a. No OAuth at all ────────────────────────────────────────────────
    if normalized == "none":
        print("\n[step 2/6] oauth_providers=none  →  removing entire oauth directory …")
        remove_path(oauth_src_dir)
        # Remove per-provider test files
        for provider in ALL_OAUTH_PROVIDERS:
            remove_path(oauth_test_dir / f"test_oauth_{provider}.py")
        print(_ok("OAuth directory removed."))
        return

    # ── 2b. Partial selection ──────────────────────────────────────────────
    selected: set[str] = {p.strip().lower() for p in normalized.split(",") if p.strip()}
    to_remove: set[str] = ALL_OAUTH_PROVIDERS - selected

    if not to_remove:
        return   # All providers kept — nothing to remove

    print(
        f"\n[step 2/6] oauth_providers={OAUTH_PROVIDERS_RAW!r}"
        f"  →  removing unselected: {sorted(to_remove)} …"
    )

    for provider in sorted(to_remove):
        # Source adapter
        remove_path(oauth_src_dir / f"{provider}.py")
        # Corresponding test helper / test file (if present)
        remove_path(oauth_test_dir / f"test_oauth_{provider}.py")

    print(_ok(f"Kept OAuth adapters: {sorted(selected)}"))


# ---------------------------------------------------------------------------
# Step 3 — Pre-commit configuration
# ---------------------------------------------------------------------------

def cleanup_pre_commit() -> None:
    """Remove .pre-commit-config.yaml when use_pre_commit=no."""
    if USE_PRE_COMMIT == "yes":
        return

    print("\n[step 3/6] use_pre_commit=no  →  removing .pre-commit-config.yaml …")
    remove_path(PROJECT_ROOT / ".pre-commit-config.yaml")
    print(_ok("pre-commit config removed."))


# ---------------------------------------------------------------------------
# Step 4 — Environment file bootstrap
# ---------------------------------------------------------------------------

def setup_env_file() -> None:
    """Copy .env.example → .env for the first run.

    Rules
    -----
    * Skip (warn) if .env.example is missing — template was incomplete.
    * Skip (warn) if .env already exists — respect existing secrets.
    * On success, remind the developer to fill in real credentials.
    """
    env_example: Path = PROJECT_ROOT / ".env.example"
    env_file: Path = PROJECT_ROOT / ".env"

    print("\n[step 4/6] Bootstrapping .env from .env.example …")

    if not env_example.exists():
        print(_warn(".env.example not found — skipping .env creation."))
        print(_warn("Create .env manually before starting the server."))
        return

    if env_file.exists():
        print(_warn(".env already exists — skipping copy to avoid overwriting existing secrets."))
        print(_warn("Diff .env.example against .env to pick up any new variables."))
        return

    try:
        shutil.copy2(env_example, env_file)
        print(_ok(".env created from .env.example"))
        print(_ok("Review .env and replace placeholder secrets before running in production:"))
        print("       SECRET_KEY, JWT_SECRET_KEY, database credentials, OAuth keys, etc.")
    except OSError as exc:
        print(_warn(f"Failed to copy .env.example → .env: {exc}"))
        print(_warn("Create it manually:  cp .env.example .env"))


# ---------------------------------------------------------------------------
# Step 5 — Dependency installation (uv sync)
# ---------------------------------------------------------------------------

def run_uv_sync() -> None:
    """Install all project dependencies into the local .venv via ``uv sync``.

    Behaviour
    ---------
    * If *uv* is not on PATH → warn and skip.  Project generation continues.
    * If ``uv sync`` exits non-zero → warn and continue.
    * stdout/stderr are streamed to the terminal in real-time so the user
      can see download progress and identify any errors immediately.

    Why run this in the hook?
    -------------------------
    Running ``uv sync`` here means that ``make dev`` (which calls ``uvicorn``
    via ``uv run``) works immediately without an extra manual step, satisfying
    the "boot in < 60 seconds" DX requirement.
    """
    print("\n[step 5/6] Installing dependencies via uv sync …")
    print("  (First run may take 30–90 s while packages are downloaded)")

    uv_bin: str | None = shutil.which("uv")
    if uv_bin is None:
        print(_warn("uv not found on PATH — skipping dependency installation."))
        print(_warn("Install uv:  https://docs.astral.sh/uv/getting-started/installation/"))
        print(_warn("Then run:    uv sync   (inside the project directory)"))
        return

    try:
        result = subprocess.run(
            [uv_bin, "sync"],
            cwd=PROJECT_ROOT,
            check=False,
            # Stream output directly; do NOT capture — let the user see progress
            stdout=None,
            stderr=None,
        )
    except OSError as exc:
        print(_warn(f"Failed to invoke uv: {exc}"))
        print(_warn("Run `uv sync` manually to install dependencies."))
        return

    if result.returncode == 0:
        print(_ok("uv sync completed — .venv is ready."))
        print(_ok(".venv/ is excluded from git via .gitignore."))
    else:
        print(_warn(f"uv sync exited with code {result.returncode}."))
        print(_warn("Review the output above, then run `uv sync` manually to retry."))


# ---------------------------------------------------------------------------
# Step 6 — Git initialisation
# ---------------------------------------------------------------------------

def _run_git(*args: str, cwd: Path = PROJECT_ROOT) -> "subprocess.CompletedProcess[str]":
    """Run a git sub-command inside *cwd*.

    Returns the CompletedProcess result.  Raises *RuntimeError* with a
    human-readable message if the command exits non-zero.
    """
    cmd: list[str] = ["git", *args]
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr_summary = result.stderr.strip().splitlines()
        first_line = stderr_summary[0] if stderr_summary else "(no stderr)"
        raise RuntimeError(
            f"`git {' '.join(args)}` failed (exit {result.returncode}): {first_line}"
        )
    return result


def init_git_repo() -> None:
    """Initialise a fresh git repository and create the initial commit.

    Steps
    -----
    1. ``git init`` — create a bare .git/ in the project root.
    2. ``git add -A`` — stage every generated file.
    3. ``git commit`` — record the initial snapshot.

    The function is intentionally lenient:
    * If git is not installed it prints a warning and returns — it must NOT
      abort project generation for a missing optional tool.
    * If ``git init`` fails for any other reason it warns and continues.
    """
    print("\n[step 6/6] Initialising git repository …")

    # ── 4a. Check git availability ────────────────────────────────────────
    git_bin: str | None = shutil.which("git")
    if git_bin is None:
        print(_warn("git not found on PATH — skipping git init."))
        print(_warn("Run `git init && git add -A && git commit -m 'Initial commit'` manually."))
        return

    # ── 4b. Skip if already inside a git repo ────────────────────────────
    inside_git = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if inside_git.returncode == 0 and inside_git.stdout.strip() == "true":
        # PROJECT_ROOT is already inside a parent git repo (unlikely but safe).
        print(_warn("Already inside a git work-tree; skipping git init."))
        return

    # ── 4c. git init ─────────────────────────────────────────────────────
    try:
        result_init = _run_git("init")
        # Print the first non-empty line from git init output (e.g.
        # "Initialized empty Git repository in …")
        first_output = next(
            (ln for ln in result_init.stdout.splitlines() if ln.strip()),
            "Initialized empty Git repository",
        )
        print(_ok(first_output))
    except RuntimeError as exc:
        print(_warn(f"git init failed: {exc}"))
        print(_warn("Skipping initial commit.  Initialise git manually."))
        return

    # ── 4d. Rename default branch to 'main' (best-effort) ────────────────
    # `git init -b main` requires git ≥ 2.28; fall back to `git branch -m main`.
    try:
        _run_git("checkout", "-b", "main")
    except RuntimeError:
        # Branch may already be named 'main', or flag unsupported — ignore.
        try:
            _run_git("branch", "-m", "main")
        except RuntimeError:
            pass   # Non-fatal; master branch is fine.

    # ── 4e. Configure a local identity when global config is absent ───────
    # Required in CI / Docker environments where no global git user is set.
    _ensure_git_identity()

    # ── 4f. Stage all files ───────────────────────────────────────────────
    try:
        _run_git("add", "--all")
        print(_ok("Staged all project files."))
    except RuntimeError as exc:
        print(_warn(f"git add failed: {exc}"))
        print(_warn("Skipping initial commit.  Stage files manually."))
        return

    # ── 4g. Initial commit ────────────────────────────────────────────────
    commit_message: str = (
        f"chore: initial scaffold — {PROJECT_SLUG}\n\n"
        "Generated by cookiecutter-fastapi-bootstrap.\n"
        f"  package       : {PACKAGE_NAME}\n"
        f"  chat domain   : {INCLUDE_CHAT_DOMAIN}\n"
        f"  oauth providers: {OAUTH_PROVIDERS_RAW}\n"
        f"  pre-commit    : {USE_PRE_COMMIT}\n"
    )
    try:
        _run_git("commit", "--message", commit_message)
        # Retrieve the short hash for display
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        short_hash: str = rev.stdout.strip() if rev.returncode == 0 else "??????"
        print(_ok(f"Initial commit created: {short_hash}"))
    except RuntimeError as exc:
        print(_warn(f"git commit failed: {exc}"))
        print(_warn("Files are staged — run `git commit` manually."))


def _ensure_git_identity() -> None:
    """Set a local git user.name / user.email when no global identity exists.

    This prevents ``git commit`` from failing in environments where the user
    has never run ``git config --global user.email``.
    """
    for key, value in (
        ("user.email", "scaffold@cookiecutter.local"),
        ("user.name", "Cookiecutter Scaffold"),
    ):
        # Check whether the key is already configured (global or local)
        check = subprocess.run(
            ["git", "config", key],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode != 0 or not check.stdout.strip():
            # Not set anywhere — configure locally inside the new repo
            subprocess.run(
                ["git", "config", "--local", key, value],
                cwd=PROJECT_ROOT,
                capture_output=True,
                check=False,
            )


# ---------------------------------------------------------------------------
# Final summary — context-aware next-steps guide
# ---------------------------------------------------------------------------

# LLM provider → env-var name for API key
_LLM_KEY_MAP: dict[str, str] = {
    "openai":    "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini":    "GEMINI_API_KEY",
    "azure":     "AZURE_OPENAI_API_KEY  (+ AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT)",
    "ollama":    "(no key required — set OLLAMA_BASE_URL)",
}

# OAuth provider → console/dashboard URL
_OAUTH_CONSOLE_MAP: dict[str, str] = {
    "google": "https://console.cloud.google.com/apis/credentials",
    "kakao":  "https://developers.kakao.com/console/app",
    "naver":  "https://developers.naver.com/apps/#/list",
}


def print_summary() -> None:
    """Print a comprehensive, context-aware next-steps guide.

    Sections
    --------
    1. Configuration recap  — what was scaffolded
    2. Bootstrap recap      — what the hook did automatically
    3. Numbered next steps  — ordered, configuration-specific actions
    4. Service URLs         — all endpoints once `make dev` is running
    5. Useful make targets  — quick reference
    6. Closing banner       — single command to get going
    """
    has_chat: bool = INCLUDE_CHAT_DOMAIN == "yes"
    has_pre_commit: bool = USE_PRE_COMMIT == "yes"
    normalized_oauth: str = OAUTH_PROVIDERS_RAW.strip().lower()
    has_oauth: bool = normalized_oauth != "none"
    selected_providers: list[str] = (
        [p.strip() for p in normalized_oauth.split(",") if p.strip()]
        if has_oauth
        else []
    )

    W: int = 64                              # banner width
    sep_double: str = "═" * W
    sep_single: str = "─" * W

    def _banner(text: str) -> None:
        print(_c(sep_double, "1;36"))
        pad = (W - len(text)) // 2
        print(_c(" " * pad + text, "1;32"))
        print(_c(sep_double, "1;36"))

    def _section(title: str) -> None:
        print()
        print(_c(f"  {title}", "1;33"))
        print(_c(f"  {sep_single}", "33"))

    def _step(n: int, title: str) -> None:
        print()
        print(_c(f"  Step {n}:  {title}", "1;37"))

    def _cmd(line: str) -> None:
        print(f"    {_c(line, '36')}")

    def _note(line: str) -> None:
        print(f"    {_c('# ' + line, '90')}")  # dim/grey

    # ── Opening banner ────────────────────────────────────────────────────
    print()
    _banner(f"🎉  {PROJECT_SLUG}  —  Generated Successfully!")
    print()

    # ── 1. Configuration recap ────────────────────────────────────────────
    _section("Configuration")
    print(f"    Project      : {_c(PROJECT_SLUG, '1')}")
    print(f"    Package      : {_c(PACKAGE_NAME, '1')}")
    print(f"    API port     : {_c(FASTAPI_PORT, '1')}")
    chat_label = _c("✓  included", "32") if has_chat else _c("✗  excluded", "33")
    print(f"    Chat domain  : {chat_label}")
    if has_chat:
        print(f"    LLM provider : {_c(LLM_PROVIDER, '1')}")
    oauth_label = _c(OAUTH_PROVIDERS_RAW, "32") if has_oauth else _c("none", "33")
    print(f"    OAuth        : {oauth_label}")
    print(f"    Pre-commit   : {_c('yes', '32') if has_pre_commit else _c('no', '33')}")

    # ── 2. Bootstrap recap ────────────────────────────────────────────────
    _section("What the hook did automatically")
    print(_ok(".env created from .env.example"))
    print(_ok("uv sync — .venv is ready (or `uv sync` after installing uv)"))
    print(_ok("git repository initialised with initial commit"))

    # ── 3. Numbered next steps ────────────────────────────────────────────
    _section("Next Steps")
    step_n: int = 0

    # Step: cd
    step_n += 1
    _step(step_n, "Enter the project directory")
    _cmd(f"cd {PROJECT_SLUG}")

    # Step: edit .env
    step_n += 1
    _step(step_n, "Configure secrets in .env")
    _cmd("$EDITOR .env")
    _note("Mandatory secrets (generate with `openssl rand -hex 32`):")
    _note("  SECRET_KEY=<random-hex>")
    _note("  JWT_SECRET_KEY=<random-hex>")
    if has_chat:
        llm_key = _LLM_KEY_MAP.get(LLM_PROVIDER, f"{LLM_PROVIDER.upper()}_API_KEY")
        _note(f"LLM key for provider={LLM_PROVIDER!r}:")
        _note(f"  {llm_key}")
        if LLM_PROVIDER == "ollama":
            _note("  (Ollama: make sure the Ollama daemon is running locally)")
    if has_oauth:
        _note("OAuth credentials (obtain from each provider's developer console):")
        for provider in selected_providers:
            console_url = _OAUTH_CONSOLE_MAP.get(provider, f"# {provider} developer console")
            pup = provider.upper()
            _note(f"  {pup}_CLIENT_ID / {pup}_CLIENT_SECRET  →  {console_url}")

    # Step: start infra + server
    step_n += 1
    _step(step_n, "Start infrastructure + dev server")
    _cmd("make dev")
    _note("Starts: PostgreSQL · Redis · Mailpit (via docker-compose)")
    _note("Runs  : alembic upgrade head · uvicorn --reload")

    # Step: health check
    step_n += 1
    _step(step_n, "Verify the server is running")
    _cmd(f"curl -s http://localhost:{FASTAPI_PORT}/health | python3 -m json.tool")
    _note('Expected → {"status": "ok", "env": "development"}')

    # Step: pre-commit (conditional)
    if has_pre_commit:
        step_n += 1
        _step(step_n, "Install pre-commit hooks (ruff + mypy on every commit)")
        _cmd("make pre-commit-install")

    # Step: run tests
    step_n += 1
    _step(step_n, "Run the test suite")
    _cmd("make test")
    _note("Requires running infra (make infra) — integration tests use real DB/Redis")

    # ── 4. Service URLs ───────────────────────────────────────────────────
    _section(f"Service URLs  (once `make dev` is running on port {FASTAPI_PORT})")
    base = f"http://localhost:{FASTAPI_PORT}"
    print(f"    Swagger UI   :  {_c(f'{base}/docs', '36')}")
    print(f"    ReDoc        :  {_c(f'{base}/redoc', '36')}")
    print(f"    Health       :  {_c(f'{base}/health', '36')}")
    print(f"    Auth API     :  {_c(f'{base}/api/v1/auth/', '36')}")
    if has_chat:
        print(f"    Chat API     :  {_c(f'{base}/api/v1/chat/', '36')}")
    print(f"    Mailpit UI   :  {_c(f'http://localhost:{MAILPIT_UI_PORT}', '36')}  (dev email inbox)")

    # ── 5. Useful make targets ────────────────────────────────────────────
    _section("Useful Make Targets")
    targets = [
        ("make help",          "list all available targets"),
        ("make dev",           "infra + migrations + uvicorn --reload"),
        ("make test",          "pytest with verbose output"),
        ("make test-cov",      "pytest + HTML coverage report"),
        ("make lint",          "ruff check + mypy"),
        ("make format",        "ruff format + ruff check --fix"),
        ("make migrate",       "alembic upgrade head"),
        ("make revision",      "alembic autogenerate new revision"),
        ("make infra",         "docker-compose up -d (infra only)"),
        ("make infra-down",    "docker-compose down"),
        ("make clean",         "remove __pycache__, .pyc, build artifacts"),
    ]
    if has_pre_commit:
        targets.append(("make pre-commit-install", "git pre-commit hooks (ruff + mypy)"))
        targets.append(("make pre-commit-run",     "run all hooks on every file"))
    col = max(len(t[0]) for t in targets) + 2
    for cmd_name, desc in targets:
        padded = cmd_name.ljust(col)
        print(f"    {_c(padded, '36')} {_c(desc, '90')}")

    # ── 6. Git remote (optional) ─────────────────────────────────────────
    _section("Push to Remote (optional)")
    print(f"    {_c('git log --oneline', '36')}  {_c('# verify initial commit', '90')}")
    print(f"    {_c('git remote add origin <url>', '36')}")
    print(f"    {_c('git push -u origin main', '36')}")

    # ── Closing banner ────────────────────────────────────────────────────
    print()
    print(_c(sep_double, "1;36"))
    closing = f"🚀  cd {PROJECT_SLUG} && make dev"
    pad = (W - len(closing)) // 2
    print(_c(" " * pad + closing, "1;32"))
    print(_c(sep_double, "1;36"))
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def ensure_script_permissions() -> None:
    """Ensure shell scripts in scripts/ are executable.

    Cookiecutter may not always preserve the executable bit from the template's
    git objects, especially on Windows or when extracted from an archive.
    This step guarantees scripts/wait_for_services.sh is always executable.
    """
    scripts_dir: Path = PROJECT_ROOT / "scripts"
    if not scripts_dir.is_dir():
        return

    shell_scripts = list(scripts_dir.glob("*.sh"))
    if not shell_scripts:
        return

    print("\n[chmod] Ensuring shell scripts are executable …")
    _execute_bit = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

    for script in shell_scripts:
        try:
            current_mode = script.stat().st_mode
            new_mode = current_mode | _execute_bit
            if new_mode != current_mode:
                script.chmod(new_mode)
                print(_ok(f"chmod +x  {script.relative_to(PROJECT_ROOT)}"))
            else:
                print(_ok(f"already executable: {script.relative_to(PROJECT_ROOT)}"))
        except OSError as exc:
            print(_warn(f"Could not chmod {script.name}: {exc}"))
            print(_warn(f"Run manually:  chmod +x scripts/{script.name}"))


if __name__ == "__main__":
    cleanup_chat_domain()         # step 1/6
    cleanup_oauth_providers()     # step 2/6
    cleanup_pre_commit()          # step 3/6
    setup_env_file()              # step 4/6
    ensure_script_permissions()   # always — ensure shell scripts are executable

    if _SKIP_HEAVY_OPS:
        print("\n[skip] COOKIECUTTER_SKIP_HEAVY_OPS=1 — skipping uv sync and git init.")
    else:
        run_uv_sync()          # step 5/6
        init_git_repo()        # step 6/6

    print_summary()
