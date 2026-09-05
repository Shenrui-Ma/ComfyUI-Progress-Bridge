from __future__ import annotations

import os
import stat

import pytest

from comfyui_progress_bridge.desktop.secret_store import SendKeyStore, validate_sendkey


@pytest.fixture
def store(tmp_path):
    # macOS /var is a symlink; use pytest's actual private directory in this fixture.
    return SendKeyStore(tmp_path.resolve() / "secrets" / "serverchan.key")


def test_sendkey_lifecycle_and_metadata_only_presence(store, monkeypatch):
    assert not store.has_key()
    assert store.load() == ""
    store.delete()
    store.save(" SCT123secret ")
    assert store.load() == "SCT123secret"
    with monkeypatch.context() as patch:
        patch.setattr(os, "read", lambda *_: pytest.fail("presence must not read the key"))
        assert store.has_key()
    store.save("SCT456replacement")
    assert store.load() == "SCT456replacement"
    store.delete()
    assert not store.has_key()
    assert store.load() == ""


@pytest.mark.parametrize(
    "key",
    ["", " ", "SCT", "SC3secret", "sctpsecret", "sctsecret", "SCTa/b", "SCTa b",
     "SCTa\n", "SCTa\r", "SCTa\tsecret", "SCT" + "a" * 1022, None],
)
def test_invalid_keys_never_create_files_or_echo_submitted_secret(store, key):
    with pytest.raises(ValueError) as caught:
        store.save(key)
    assert str(caught.value) == "Enter a valid ServerChan Turbo SendKey"
    assert not store.path.parent.exists()


def test_validation_accepts_bounded_turbo_key():
    assert validate_sendkey("SCT" + "a" * 1021) == "SCT" + "a" * 1021


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_new_directory_and_file_are_owner_private(store):
    store.save("SCTsecret")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
@pytest.mark.parametrize("mode", [0o400, 0o644, 0o666, 0o700])
def test_unsafe_file_permissions_rejected_by_every_operation(store, mode):
    store.save("SCTsecret")
    store.path.chmod(mode)
    for operation in (store.has_key, store.load, store.delete, lambda: store.save("SCTnew")):
        with pytest.raises(ValueError, match="^SendKey storage is unavailable or unsafe$"):
            operation()
    assert store.path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_shared_parent_rejected_without_changing_permissions(store):
    store.path.parent.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        store.save("SCTsecret")
    assert not store.path.exists()
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks, hardlinks and FIFO")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_non_private_regular_targets_are_rejected(store, tmp_path, kind):
    store.path.parent.mkdir(mode=0o700)
    if kind in ("symlink", "hardlink"):
        original = tmp_path / "original"
        original.write_text("SCTsecret")
        original.chmod(0o600)
        if kind == "symlink":
            store.path.symlink_to(original)
        else:
            os.link(original, store.path)
    elif kind == "directory":
        store.path.mkdir()
    else:
        os.mkfifo(store.path, mode=0o600)
    for operation in (store.has_key, store.load, store.delete, lambda: store.save("SCTnew")):
        with pytest.raises(ValueError):
            operation()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")
def test_symlinked_ancestor_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    store = SendKeyStore(alias / "nested" / "key")
    with pytest.raises(ValueError):
        store.save("SCTsecret")
    assert list(real.iterdir()) == []


def test_parent_traversal_rejected(tmp_path):
    with pytest.raises(ValueError):
        SendKeyStore(tmp_path / ".." / "key")


@pytest.mark.skipif(os.name == "nt", reason="POSIX plaintext storage")
@pytest.mark.parametrize("payload", [b"", b"SCT" + b"a" * 1022, b"SCT\xffsecret", b"invalid"])
def test_invalid_stored_payload_is_sanitized(store, payload):
    store.path.parent.mkdir(mode=0o700)
    store.path.write_bytes(payload)
    store.path.chmod(0o600)
    with pytest.raises(ValueError) as caught:
        store.load()
    assert str(caught.value) == "SendKey storage is unavailable or unsafe"


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_atomic_failure_preserves_existing_key_and_cleans_temp(store, monkeypatch, failure):
    store.save("SCToriginal")

    def fail(*args, **kwargs):
        raise OSError("sensitive details must not escape")

    with monkeypatch.context() as patch:
        patch.setattr(os, failure, fail)
        with pytest.raises(ValueError) as caught:
            store.save("SCTreplacement")
        assert str(caught.value) == "SendKey storage is unavailable or unsafe"
    assert store.load() == "SCToriginal"
    assert list(store.path.parent.iterdir()) == [store.path]


def test_short_reads_and_writes_work(store, monkeypatch):
    original_read, original_write = os.read, os.write
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", lambda fd, data: original_write(fd, data[:2]))
        store.save("SCTsecret")
        patch.setattr(os, "read", lambda fd, size: original_read(fd, min(size, 2)))
        assert store.load() == "SCTsecret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX nonblocking FIFO protection")
def test_file_swapped_to_fifo_before_open_cannot_block(store, monkeypatch):
    store.save("SCTsecret")
    original_open = os.open

    def swapped_open(path, flags, *args, **kwargs):
        if path == store.path.name:
            assert flags & os.O_NONBLOCK
            assert flags & os.O_NOFOLLOW
            store.path.unlink()
            os.mkfifo(store.path, mode=0o600)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapped_open)
    with pytest.raises(ValueError, match="^SendKey storage is unavailable or unsafe$"):
        store.load()


@pytest.mark.skipif(os.name == "nt", reason="POSIX anchored parent directory")
def test_parent_swap_cannot_redirect_secret_write(store, tmp_path, monkeypatch):
    store.save("SCToriginal")
    alternate = tmp_path / "alternate"
    alternate.mkdir(mode=0o700)
    retained = store.path.parent.with_name("retained")
    original_open = os.open

    def swapped_open(path, flags, *args, **kwargs):
        if str(path).startswith(".sendkey-"):
            store.path.parent.rename(retained)
            store.path.parent.symlink_to(alternate, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapped_open)
    store.save("SCTreplacement")
    assert list(alternate.iterdir()) == []
    assert (retained / store.path.name).read_text() == "SCTreplacement"
    with pytest.raises(ValueError):
        store.load()


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows user-bound DPAPI")
def test_windows_disk_payload_is_encrypted(store):
    store.save("SCTsecret")
    assert b"SCTsecret" not in store.path.read_bytes()
    assert store.load() == "SCTsecret"
