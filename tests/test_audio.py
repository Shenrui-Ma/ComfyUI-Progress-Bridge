import hashlib
import os
import stat
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from comfyui_progress_bridge.desktop.audio import CompletionAudio, validate_wav
from comfyui_progress_bridge.desktop.settings import AudioConfig


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def wav_file(tmp_path, name="sound.wav"):
    path = tmp_path / name
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(bytes([sum(name.encode()) % 256, 0]) * 16)
    return path


class FakeApplication:
    def __init__(self):
        self.beeps = 0

    def beep(self):
        self.beeps += 1


class FakeSoundEffect:
    def __init__(self):
        self.sources = []
        self.plays = 0

    def setSource(self, source):
        self.sources.append(source)

    def play(self):
        self.plays += 1


def test_construction_and_disabled_modes_never_play_audio():
    application = FakeApplication()
    players = []

    def factory():
        player = FakeSoundEffect()
        players.append(player)
        return player

    audio = CompletionAudio(application=application, player_factory=factory)
    assert application.beeps == 0 and players == []
    assert audio.play(AudioConfig()).code == "disabled"
    assert audio.play(AudioConfig(enabled=False, mode="ding")).code == "disabled"
    assert application.beeps == 0 and players == []


def test_ding_uses_qapplication_beep_only():
    application = FakeApplication()
    created = []
    audio = CompletionAudio(
        application=application,
        player_factory=lambda: created.append(FakeSoundEffect()),
    )
    result = audio.play(AudioConfig(enabled=True, mode="ding"))
    assert result.ok
    assert application.beeps == 1
    assert created == []


def test_custom_wav_uses_reusable_qsoundeffect_and_immutable_cache(tmp_path):
    one = wav_file(tmp_path, "one.wav")
    two = wav_file(tmp_path, "two.wav")
    players = []

    def factory():
        player = FakeSoundEffect()
        players.append(player)
        return player

    cache = tmp_path / "cache"
    audio = CompletionAudio(application=FakeApplication(), player_factory=factory, cache_dir=cache)
    assert audio.play(AudioConfig(True, "custom", str(one))).ok
    assert audio.play(AudioConfig(True, "custom", str(one))).ok
    assert audio.play(AudioConfig(True, "custom", str(two))).ok

    assert len(players) == 1
    assert players[0].plays == 3
    assert len(players[0].sources) == 2
    assert all(Path(source.toLocalFile()).parent == cache for source in players[0].sources)
    assert all(
        (Path(source.toLocalFile()).stat().st_mode & 0o777) == 0o600
        for source in players[0].sources
    )


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "invalid_size"),
        (b"not-wave" + b"x" * 40, "invalid_format"),
        (b"RIFF" + b"\0" * 4 + b"NOPE" + b"\0" * 32, "invalid_format"),
    ],
)
def test_custom_wav_validation_rejects_bad_inputs_without_player(tmp_path, content, code):
    path = tmp_path / "bad.wav"
    path.write_bytes(content)
    created = []
    audio = CompletionAudio(
        application=FakeApplication(), player_factory=lambda: created.append(FakeSoundEffect())
    )
    result = audio.play(AudioConfig(True, "custom", str(path)))
    assert result.code == code
    assert created == []


def test_wav_validation_rejects_missing_symlink_directory_and_oversize(tmp_path, monkeypatch):
    assert validate_wav("").code == "missing_file"
    assert validate_wav(str(tmp_path / "missing.wav")).code == "invalid_file"
    assert validate_wav(str(tmp_path)).code == "invalid_file"

    target = wav_file(tmp_path)
    link = tmp_path / "linked.wav"
    link.symlink_to(target)
    assert validate_wav(str(link)).code == "unsafe_file"

    monkeypatch.setattr("comfyui_progress_bridge.desktop.audio.MAX_WAV_BYTES", 43)
    assert validate_wav(str(target)).code == "invalid_size"


def test_custom_playback_errors_are_sanitized(tmp_path):
    secret = "SECRET-WAV-DETAIL"
    path = wav_file(tmp_path)

    class BrokenPlayer(FakeSoundEffect):
        def play(self):
            raise RuntimeError(secret)

    result = CompletionAudio(application=FakeApplication(), player_factory=BrokenPlayer).play(
        AudioConfig(True, "custom", str(path))
    )
    assert result.code == "playback_error"
    assert secret not in result.message and secret not in repr(result)


def test_cached_wav_survives_source_replacement_race(tmp_path):
    source = wav_file(tmp_path, "race.wav")
    original = source.read_bytes()
    player = FakeSoundEffect()
    audio = CompletionAudio(
        application=FakeApplication(), player_factory=lambda: player, cache_dir=tmp_path / "cache"
    )
    real_cache = audio._cache_wav

    def replace_then_cache(data):
        source.write_bytes(b"attacker replacement")
        return real_cache(data)

    audio._cache_wav = replace_then_cache
    assert audio.play(AudioConfig(True, "custom", str(source))).ok
    cached = Path(player.sources[0].toLocalFile())
    assert cached.read_bytes() == original
    assert cached.read_bytes() != source.read_bytes()


def test_cache_rejects_existing_world_writable_directory(tmp_path):
    source = wav_file(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o777)
    cache.chmod(0o777)
    audio = CompletionAudio(
        application=FakeApplication(), player_factory=FakeSoundEffect, cache_dir=cache
    )
    assert audio.play(AudioConfig(True, "custom", str(source))).code == "playback_error"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o777


def test_cache_entry_swap_never_reads_or_chmods_external_file(tmp_path, monkeypatch):
    source = wav_file(tmp_path)
    data = source.read_bytes()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    destination = cache / f"{hashlib.sha256(data).hexdigest()}.wav"
    destination.write_bytes(data)
    external = tmp_path / "external.wav"
    external.write_bytes(data)
    external.chmod(0o777)

    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == destination.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            destination.unlink()
            destination.symlink_to(external)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    player = FakeSoundEffect()
    audio = CompletionAudio(
        application=FakeApplication(), player_factory=lambda: player, cache_dir=cache
    )
    assert audio.play(AudioConfig(True, "custom", str(source))).ok
    assert stat.S_IMODE(external.stat().st_mode) == 0o777
    assert not destination.is_symlink()
    assert destination.read_bytes() == data
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data[:-1],
        lambda data: data + b"trailing",
        lambda data: data[:40] + (10_000).to_bytes(4, "little") + data[44:],
    ],
)
def test_wav_rejects_truncated_appended_and_false_frame_declarations(tmp_path, mutate):
    path = wav_file(tmp_path, "malformed.wav")
    path.write_bytes(mutate(path.read_bytes()))
    assert validate_wav(str(path)).code == "invalid_format"


def test_real_qapplication_is_accepted_without_startup_or_disabled_audio(app):
    audio = CompletionAudio(application=app, player_factory=FakeSoundEffect)
    assert audio.play(AudioConfig()).code == "disabled"
