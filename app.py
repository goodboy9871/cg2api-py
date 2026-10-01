#!/usr/bin/env python3
"""
High-performance bootstrap for yukkcat/chatgpt2api on Pterodactyl Panel.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import secrets
import shutil
import site
import string
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants & Paths
# ---------------------------------------------------------------------------

REPOSITORY = "yukkcat/chatgpt2api"
GIT_URL = f"https://github.com/{REPOSITORY}.git"

HOME = Path(os.environ.get("HOME", "/home/container"))
STATE_DIR = HOME / ".chatgpt2api-runtime"
REPO_DIR = STATE_DIR / "app"
LOCK_FILE = STATE_DIR / ".bootstrap.lock"
KEY_FILE = STATE_DIR / ".api_key"

PIP_TIMEOUT = 300.0


class BootstrapError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(f"command timed out after {timeout}s: {' '.join(args)}") from exc
    except OSError as exc:
        raise BootstrapError(f"cannot execute {' '.join(args)}: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stdout or "").strip()
        if len(detail) > 2000:
            detail = "...\n" + detail[-2000:]
        raise BootstrapError(
            f"command failed (exit {result.returncode}): {' '.join(args)}\n{detail}"
        )

    return result


def resolve_port() -> int:
    raw = (os.environ.get("SERVER_PORT") or os.environ.get("PORT", "")).strip()
    if not raw:
        raise BootstrapError("No port found. Set SERVER_PORT or PORT.")
    try:
        value = int(raw)
    except ValueError as exc:
        raise BootstrapError(f"Invalid port: {raw!r}") from exc
    return value


def generate_api_key(prefix: str = "sk", length: int = 48) -> str:
    alphabet = string.ascii_letters + string.digits
    token = "".join(secrets.choice(alphabet) for _ in range(length))
    return f"{prefix}-{token}"


def load_or_create_api_key() -> tuple[str, bool]:
    if KEY_FILE.is_file():
        key = KEY_FILE.read_text().strip()
        if key:
            return key, False
    key = generate_api_key()
    KEY_FILE.write_text(key + "\n")
    KEY_FILE.chmod(0o600)
    return key, True


def print_key_banner(key: str, is_new: bool) -> None:
    label = "GENERATED NEW API KEY" if is_new else "EXISTING API KEY LOADED"
    border = "=" * 60
    print(flush=True)
    print(border, flush=True)
    print(f"  {label}", flush=True)
    print(f"  Key : {key}", flush=True)
    print(f"  File: {KEY_FILE}", flush=True)
    print(border, flush=True)
    print(flush=True)


def _git(*args: str, cwd: Path | None = None, timeout: float = 120.0) -> None:
    run(["git", *args], cwd=cwd, timeout=timeout)


def sync_repository() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not (REPO_DIR / ".git").is_dir():
        log(f"cloning {REPOSITORY} → {REPO_DIR}")
        _git("clone", "--depth=1", GIT_URL, str(REPO_DIR), timeout=180.0)
    else:
        log("repository present — updating (git pull --ff-only)")
        try:
            _git("pull", "--ff-only", cwd=REPO_DIR)
        except BootstrapError as exc:
            log(f"WARNING: git pull failed (continuing with existing checkout): {exc}")


def download_prebuilt_frontend() -> None:
    # 项目期望的前端目录是 web_dist/，由 api/support.py 中的 WEB_DIST_DIR 决定
    web_dist_dir = REPO_DIR / "web_dist"

    if web_dist_dir.is_dir():
        log("frontend web_dist already exists, skipping download.")
        return

    log("attempting to download prebuilt frontend dist...")

    dist_url = "https://github.com/wszpwu1/chatgpt2api-katabump/releases/download/frontend-20260826-072818/dist.tar.gz"
    dist_archive = STATE_DIR / "dist.tar.gz"
    temp_extract = STATE_DIR / "temp_dist"

    try:
        log(f"downloading from {dist_url}...")
        run(["curl", "-fsSL", "-o", str(dist_archive), dist_url], timeout=180.0)

        log("extracting dist.tar.gz...")
        temp_extract.mkdir(parents=True, exist_ok=True)
        run(["tar", "-xzf", str(dist_archive), "-C", str(temp_extract)], timeout=60.0)

        # tar 解压后内容是 dist/ 目录，移动到 web_dist/
        extracted_dist = temp_extract / "dist"
        if extracted_dist.is_dir():
            shutil.move(str(extracted_dist), str(web_dist_dir))
            log("frontend dist moved to web_dist/ successfully!")
        else:
            # 有些打包方式直接是文件在根层，尝试直接重命名
            shutil.move(str(temp_extract), str(web_dist_dir))
            log("frontend moved to web_dist/ successfully!")

    except BootstrapError as exc:
        log(f"WARNING: could not download prebuilt frontend: {exc}")
        log("Admin UI will be unavailable, but API endpoints will work.")
    finally:
        if dist_archive.exists():
            dist_archive.unlink()
        shutil.rmtree(temp_extract, ignore_errors=True)


def install_dependencies() -> None:
    pip_base = [sys.executable, "-m", "pip"]

    log("upgrading pip, setuptools, and wheel")
    with contextlib.suppress(Exception):
        run(
            [*pip_base, "install", "--user", "--upgrade", "pip", "setuptools", "wheel"],
            timeout=120.0,
        )

    pip_cmd = [
        sys.executable, "-m", "pip", "install",
        "--user",
        "--no-warn-script-location",
        "--prefer-binary",
    ]

    req_file = REPO_DIR / "requirements.txt"
    if req_file.is_file():
        log(f"installing dependencies from existing {req_file.name}")
        run([*pip_cmd, "-r", str(req_file)], timeout=PIP_TIMEOUT)
    else:
        log("requirements.txt not found, installing core packages")
        core_packages = [
            "fastapi",
            "uvicorn[standard]",
            "sqlalchemy",
            "pydantic",
            "httpx",
            "requests",
            "curl_cffi",
            "pydantic-settings",
            "loguru",
            "pillow",
            "PyYAML",
            "aiohttp",
            "pybase64",
            "tiktoken",
        ]
        fallback_req = STATE_DIR / "requirements-fallback.txt"
        fallback_req.write_text("\n".join(core_packages) + "\n")
        run([*pip_cmd, "-r", str(fallback_req)], timeout=PIP_TIMEOUT)

    log("dependencies ready")


_UVICORN_RUN_RE = re.compile(r"uvicorn\.run\s*\([^)]*\)", re.DOTALL)


def patch_main_py(host: str, port: int) -> None:
    main_file = REPO_DIR / "main.py"
    if not main_file.is_file():
        return

    content = main_file.read_text(encoding="utf-8")

    if "uvicorn.run" in content:
        log("patching main.py to bind to correct host/port")
        patched = _UVICORN_RUN_RE.sub(
            f'uvicorn.run("api.app:create_app", factory=True, host="{host}", port={port})',
            content,
        )
        if patched != content:
            main_file.write_text(patched, encoding="utf-8")
            log("main.py successfully patched!")


def main() -> None:
    if sys.platform != "linux":
        raise BootstrapError("Linux only.")

    port = resolve_port()
    host = "0.0.0.0"

    log(f"chatgpt2api bootstrap starting — target {host}:{port}")

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BootstrapError("Another instance is running. Aborting.")

        sync_repository()
        download_prebuilt_frontend()
        install_dependencies()
        patch_main_py(host, port)

        for sub_dir in ["data", "database", "var", "storage"]:
            (REPO_DIR / sub_dir).mkdir(parents=True, exist_ok=True)

        api_key, is_new = load_or_create_api_key()
        print_key_banner(api_key, is_new)

        env = os.environ.copy()
        env["PORT"] = str(port)
        env["HOST"] = host
        env["APP_DIR"] = str(REPO_DIR)

        env["CHATGPT2API_AUTH_KEY"] = api_key
        env["API_KEY"] = api_key
        env["OPENAI_API_KEY"] = api_key

        if "DATABASE_URL" not in env:
            db_path = REPO_DIR / "data" / "database.db"
            env["DATABASE_URL"] = f"sqlite:///{db_path}"

        user_site = site.getusersitepackages()
        existing_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{user_site}:{existing_path}" if existing_path else str(user_site)

        log(f"exec → python main.py (host={host}, port={port})")
        sys.stdout.flush()
        sys.stderr.flush()

        os.chdir(str(REPO_DIR))
        os.execve(
            sys.executable,
            [sys.executable, "main.py"],
            env,
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        raise SystemExit(130)
    except BootstrapError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    except Exception as exc:
        print(f"[FATAL] unexpected error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)