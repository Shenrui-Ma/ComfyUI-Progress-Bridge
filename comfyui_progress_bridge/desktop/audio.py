"""Non-blocking Qt completion audio with race-safe custom-WAV caching."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QApplication

from .notifications import SafeResult
from .settings import AudioConfig

MAX_WAV_BYTES = 16 * 1024 * 1024
MAX_CACHE_SCAN = 256
MAX_CACHE_ENTRIES = 32
CACHE_MAX_AGE = 30 * 24 * 60 * 60

try:
    from PyQt6.QtMultimedia import QSoundEffect
except ImportError:  # pragma: no cover - optional Qt component gate
    QSoundEffect = None  # type: ignore[assignment,misc]


def _read_valid_wav(path: str) -> tuple[SafeResult, bytes | None]:
    if not path:
        return SafeResult(False, "missing_file", "Choose a local WAV file", "audio"), None
    candidate = Path(path).expanduser()
    try:
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return SafeResult(
                False, "unsafe_file", "Custom WAV may not be a symlink", "audio"
            ), None
        if not stat.S_ISREG(metadata.st_mode):
            return SafeResult(
                False, "invalid_file", "Custom WAV must be a readable file", "audio"
            ), None
        if not 44 <= metadata.st_size <= MAX_WAV_BYTES:
            return SafeResult(
                False, "invalid_size", "Custom WAV has an invalid size", "audio"
            ), None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not 44 <= opened.st_size <= MAX_WAV_BYTES
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                return SafeResult(
                    False, "unsafe_file", "Custom WAV changed while opening", "audio"
                ), None
            chunks: list[bytes] = []
            remaining = MAX_WAV_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError:
        return SafeResult(False, "invalid_file", "Custom WAV is not readable", "audio"), None
    if not 44 <= len(data) <= MAX_WAV_BYTES:
        return SafeResult(False, "invalid_size", "Custom WAV has an invalid size", "audio"), None
    # RIFF's declared extent must exactly cover the file. This rejects truncation,
    # appended payloads and RF64 structures that Python's wave parser cannot verify.
    if (
        data[:4] != b"RIFF"
        or data[8:12] != b"WAVE"
        or int.from_bytes(data[4:8], "little") + 8 != len(data)
    ):
        return SafeResult(
            False, "invalid_format", "Custom audio must be a complete PCM WAV", "audio"
        ), None
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            rate = reader.getframerate()
            frame_count = reader.getnframes()
            if (
                reader.getcomptype() != "NONE"
                or channels not in (1, 2)
                or sample_width not in (1, 2, 3, 4)
                or not 1 <= rate <= 384_000
                or frame_count <= 0
            ):
                raise wave.Error("unsupported PCM parameters")
            frames = reader.readframes(frame_count)
            if len(frames) != frame_count * channels * sample_width:
                raise wave.Error("truncated PCM data")
            if reader.readframes(1):
                raise wave.Error("undeclared PCM frames")
    except (EOFError, wave.Error):
        return SafeResult(
            False, "invalid_format", "Custom audio must be a complete PCM WAV", "audio"
        ), None
    return SafeResult(True, "valid", "Custom WAV is valid", "audio"), data


def validate_wav(path: str) -> SafeResult:
    return _read_valid_wav(path)[0]


class CompletionAudio:
    """Reusable Qt player that only loads validated, content-addressed cache files."""

    def __init__(
        self,
        *,
        application: QApplication | None = None,
        player_factory: Callable[[], Any] | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.application = application or QApplication.instance()
        self._factory = player_factory or (QSoundEffect if QSoundEffect is not None else None)
        self._player: Any = None
        self._loaded_path = ""
        self._cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else Path.home() / ".cache" / "comfyui-progress-bridge" / "audio"
        )

    def _open_cache_dir(self) -> int | None:
        """Open a private cache directory without ever following the leaf path."""
        try:
            self._cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            path_metadata = self._cache_dir.lstat()
            if (
                not stat.S_ISDIR(path_metadata.st_mode)
                or stat.S_ISLNK(path_metadata.st_mode)
                or path_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(path_metadata.st_mode) != 0o700
            ):
                return None
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(self._cache_dir, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o700
                or (opened.st_dev, opened.st_ino)
                != (path_metadata.st_dev, path_metadata.st_ino)
            ):
                os.close(descriptor)
                return None
            return descriptor
        except OSError:
            return None

    @staticmethod
    def _read_cache_entry(directory_fd: int, name: str, expected: bytes) -> bool:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_size != len(expected)
            ):
                return False
            chunks: list[bytes] = []
            remaining = len(expected) + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if b"".join(chunks) != expected:
                return False
            # Change permissions only through the descriptor that was validated.
            os.fchmod(descriptor, 0o600)
            return True
        finally:
            os.close(descriptor)

    def _verified_cache_path(self, directory_fd: int, name: str, data: bytes) -> Path | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or opened.st_nlink != 1
                    or opened.st_size != len(data)
                ):
                    return None
                chunks: list[bytes] = []
                remaining = len(data) + 1
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if b"".join(chunks) != data:
                    return None
                os.fchmod(descriptor, 0o600)
                opened = os.fstat(descriptor)
                # This is deliberately the final path operation before returning:
                # pair a non-following lstat with the still-open validated file.
                path_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(path_metadata.st_mode)
                    or path_metadata.st_uid != os.geteuid()
                    or path_metadata.st_nlink != 1
                    or stat.S_IMODE(path_metadata.st_mode) != 0o600
                    or (opened.st_dev, opened.st_ino)
                    != (path_metadata.st_dev, path_metadata.st_ino)
                    or stat.S_IMODE(opened.st_mode) != 0o600
                ):
                    return None
            finally:
                os.close(descriptor)
            return self._cache_dir / name
        except OSError:
            return None

    def _cache_wav(self, data: bytes) -> Path | None:
        directory_fd = self._open_cache_dir()
        if directory_fd is None:
            return None
        digest_name = f"{hashlib.sha256(data).hexdigest()}.wav"
        temporary = ""
        try:
            try:
                if self._read_cache_entry(directory_fd, digest_name, data):
                    return self._verified_cache_path(directory_fd, digest_name, data)
            except OSError:
                pass

            # A name that is stale, malformed, or a symlink is removed relative to
            # the already-open private directory; no target is ever followed.
            try:
                os.unlink(digest_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

            for _ in range(128):
                temporary = f".wav-{os.urandom(16).hex()}"
                try:
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=directory_fd,
                    )
                    break
                except FileExistsError:
                    continue
            else:
                return None
            try:
                os.fchmod(descriptor, 0o600)
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short cache write")
                    view = view[written:]
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1
                    or metadata.st_size != len(data)
                ):
                    return None
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                temporary,
                digest_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary = ""
            self._cleanup_cache(directory_fd, digest_name)
            return self._verified_cache_path(directory_fd, digest_name, data)
        except OSError:
            return None
        finally:
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)

    def _cleanup_cache(self, directory_fd: int, keep_name: str) -> None:
        now = time.time()
        entries: list[tuple[float, str]] = []
        try:
            with os.scandir(directory_fd) as iterator:
                for index, entry in enumerate(iterator):
                    if index >= MAX_CACHE_SCAN:
                        break
                    if entry.name == keep_name or not entry.name.endswith(".wav"):
                        continue
                    try:
                        metadata = os.stat(
                            entry.name, dir_fd=directory_fd, follow_symlinks=False
                        )
                    except OSError:
                        continue
                    if (
                        stat.S_ISREG(metadata.st_mode)
                        and metadata.st_uid == os.geteuid()
                        and metadata.st_nlink == 1
                    ):
                        entries.append((metadata.st_mtime, entry.name))
            entries.sort(reverse=True)
            for index, (modified, name) in enumerate(entries):
                if index >= MAX_CACHE_ENTRIES or now - modified > CACHE_MAX_AGE:
                    try:
                        os.unlink(name, dir_fd=directory_fd)
                    except OSError:
                        pass
        except OSError:
            pass

    def play(self, config: AudioConfig) -> SafeResult:
        if not config.enabled or config.mode == "disabled":
            return SafeResult(False, "disabled", "Completion audio is disabled", "audio")
        if config.mode == "ding":
            if self.application is None:
                return SafeResult(False, "unavailable", "Qt application is unavailable", "audio")
            self.application.beep()
            return SafeResult(True, "played", "Completion ding played", "audio")
        validation, data = _read_valid_wav(config.wav_path)
        if not validation.ok or data is None:
            return validation
        if self._factory is None:
            return SafeResult(False, "unavailable", "Qt multimedia is unavailable", "audio")
        cached = self._cache_wav(data)
        if cached is None:
            return SafeResult(False, "playback_error", "Audio cache is unavailable", "audio")
        try:
            if self._player is None:
                self._player = self._factory()
            cached_path = os.path.abspath(cached)
            if cached_path != self._loaded_path:
                self._player.setSource(QUrl.fromLocalFile(cached_path))
                self._loaded_path = cached_path
            self._player.play()
        except Exception as exc:
            return SafeResult(
                False, "playback_error", f"Audio playback failed ({type(exc).__name__})", "audio"
            )
        return SafeResult(True, "played", "Custom completion audio played", "audio")
