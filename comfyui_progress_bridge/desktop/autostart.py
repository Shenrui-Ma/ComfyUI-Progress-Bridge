"""Single-instance bootstrap used when ComfyUI imports the custom node."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

_LOCK_DIR_NAME = "comfyui-progress-bridge"


def _default_runtime_dir() -> Path:
    """Return a user-private, persistent runtime directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / _LOCK_DIR_NAME / "runtime"
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI when available
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "ComfyUIProgressBridge" / "runtime"
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / _LOCK_DIR_NAME
    return Path.home() / ".cache" / _LOCK_DIR_NAME / "runtime"


def _prepare_runtime_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = path.stat()
    if not stat.S_ISDIR(details.st_mode):
        raise OSError(f"runtime path is not a directory: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise PermissionError(f"runtime directory is not owned by this user: {path}")
    if os.name != "nt":
        path.chmod(0o700)


def acquire_singleton_lock(
    endpoint: str, *, runtime_dir: Path | None = None
) -> BinaryIO | None:
    """Acquire a secure, user-local advisory lock for one ComfyUI endpoint."""
    directory = _default_runtime_dir() if runtime_dir is None else runtime_dir
    try:
        _prepare_runtime_dir(directory)
        endpoint_key = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
        path = directory / f"desktop-{endpoint_key}.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise OSError(f"lock path is not a regular file: {path}")
            if hasattr(os, "getuid") and details.st_uid != os.getuid():
                raise PermissionError(f"lock file is not owned by this user: {path}")
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - exercised on Windows CI when available
                import msvcrt

                if details.st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return os.fdopen(descriptor, "r+b", closefd=True)
        except Exception:
            os.close(descriptor)
            raise
    except (OSError, PermissionError):
        return None


def run(
    endpoint: str,
    *,
    acquire_lock: Callable[[str], Any | None] = acquire_singleton_lock,
    app_main: Callable[[list[str]], int] | None = None,
) -> int:
    """Run one dock per endpoint, retaining its lock until the UI exits."""
    lock = acquire_lock(endpoint)
    if lock is None:
        return 0
    if app_main is None:
        from .app import main

        app_main = main
    try:
        return app_main(["--show", "--endpoint", endpoint])
    finally:
        close = getattr(lock, "close", None)
        if callable(close):
            close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autostart the ComfyUI progress dock")
    parser.add_argument("endpoint", help="numeric ComfyUI endpoint, for example 127.0.0.1:8188")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return run(arguments.endpoint)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
