"""Local SendKey storage: private plaintext on POSIX, user-bound DPAPI on Windows.

Use a dedicated secrets directory. The UI can query metadata without loading the
key; only delivery or an explicit credential change should access the secret.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import uuid
from pathlib import Path

_ERROR = "SendKey storage is unavailable or unsafe"
_MAX_BYTES = 16_384
_DPAPI_PREFIX = b"CPB-DPAPI-1\x00"


def validate_sendkey(key: str) -> str:
    """Accept only Turbo SCT keys; never include the submitted value in errors."""
    if not isinstance(key, str) or len(key) > 1024 or "\n" in key or "\r" in key:
        raise ValueError("Enter a valid ServerChan Turbo SendKey")
    value = key.strip()
    if not re.fullmatch(r"SCT[A-Za-z0-9]+", value):
        raise ValueError("Enter a valid ServerChan Turbo SendKey")
    return value


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    """DPAPI defaults to this Windows user, not the machine-wide scope."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    result = Blob()
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError(_ERROR)
    try:
        if result.size > _MAX_BYTES:
            raise ValueError(_ERROR)
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def _is_link(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


class SendKeyStore:
    def __init__(self, path: str | Path):
        # Do not resolve(): following a symlink would hide an unsafe input path.
        original = Path(path).expanduser()
        if ".." in original.parts:
            raise ValueError(_ERROR)
        self.path = original.absolute()

    @contextlib.contextmanager
    def _directory(self, *, create: bool = False):
        """Anchor POSIX operations to an opened, symlink-free parent directory."""
        descriptor = None
        try:
            parent = self.path.parent
            if os.name == "nt":
                current = Path(parent.anchor)
                for part in parent.parts[1:]:
                    current /= part
                    try:
                        metadata = current.lstat()
                    except FileNotFoundError:
                        if not create:
                            yield -1
                            return
                        current.mkdir(mode=0o700)
                        metadata = current.lstat()
                    if _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
                        raise ValueError(_ERROR)
                yield None
                return
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(parent.anchor, flags)
            for part in parent.parts[1:]:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        yield -1
                        return
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            metadata = os.fstat(descriptor)
            if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
                raise ValueError(_ERROR)
            yield descriptor
        except (OSError, ValueError):
            raise ValueError(_ERROR) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _target(self, descriptor: int | None, name: str | None = None):
        filename = self.path.name if name is None else name
        if descriptor is None:
            return self.path.parent / filename, {}
        return filename, {"dir_fd": descriptor}

    def _metadata(self, descriptor: int | None):
        if descriptor == -1:
            return None
        target, arguments = self._target(descriptor)
        try:
            metadata = os.stat(target, follow_symlinks=False, **arguments)
        except FileNotFoundError:
            return None
        self._validate_metadata(metadata)
        return metadata

    @staticmethod
    def _validate_metadata(metadata: os.stat_result):
        if (
            _is_link(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= (_MAX_BYTES if os.name == "nt" else 1024)
        ):
            raise ValueError(_ERROR)
        if os.name != "nt" and (
            stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid()
        ):
            raise ValueError(_ERROR)

    def has_key(self) -> bool:
        """Check safe file presence only: no read, decoding, or DPAPI operation."""
        with self._directory() as descriptor:
            return self._metadata(descriptor) is not None

    def load(self) -> str:
        with self._directory() as directory:
            metadata = self._metadata(directory)
            if metadata is None:
                return ""
            target, arguments = self._target(directory)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(target, flags, **arguments)
            try:
                opened = os.fstat(descriptor)
                self._validate_metadata(opened)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise ValueError(_ERROR)
                data = bytearray()
                while len(data) <= _MAX_BYTES:
                    chunk = os.read(descriptor, min(4096, _MAX_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
            finally:
                os.close(descriptor)
            if len(data) > _MAX_BYTES:
                raise ValueError(_ERROR)
            payload = bytes(data)
            if os.name == "nt":
                if not payload.startswith(_DPAPI_PREFIX):
                    raise ValueError(_ERROR)
                payload = _dpapi(payload[len(_DPAPI_PREFIX):], decrypt=True)
            try:
                return validate_sendkey(payload.decode("ascii"))
            except (UnicodeError, ValueError):
                raise ValueError(_ERROR) from None

    def save(self, key: str) -> None:
        value = validate_sendkey(key)
        with self._directory(create=True) as directory:
            self._metadata(directory)
            # No secret bytes are written before validating the parent and target.
            data = value.encode("ascii")
            if os.name == "nt":
                data = _DPAPI_PREFIX + _dpapi(data, decrypt=False)
            temporary = f".sendkey-{uuid.uuid4().hex}.tmp"
            target, arguments = self._target(directory, temporary)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(target, flags, 0o600, **arguments)
            try:
                try:
                    if os.name != "nt":
                        os.fchmod(descriptor, 0o600)
                    remaining = memoryview(data)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if not written:
                            raise ValueError(_ERROR)
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._metadata(directory)
                destination, _ = self._target(directory)
                if directory is None:
                    os.replace(target, destination)
                else:
                    os.replace(target, destination, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(target, **arguments)
                except FileNotFoundError:
                    pass

    def delete(self) -> None:
        with self._directory() as directory:
            if self._metadata(directory) is not None:
                target, arguments = self._target(directory)
                os.unlink(target, **arguments)
