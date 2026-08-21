import pytest

from comfyui_progress_bridge.desktop.i18n import LANGUAGES, REQUIRED_KEYS, Translator


@pytest.mark.parametrize("language", ["zh-CN", "ja-JP", "en-US", "ko-KR"])
def test_every_language_has_complete_nonempty_catalog(language):
    translator = Translator(language)
    assert set(REQUIRED_KEYS) <= set(LANGUAGES[language])
    assert all(translator(key) and translator(key) != key for key in REQUIRED_KEYS)
    assert translator("queue_counts", running=2, pending=3).count("2") == 1


def test_unknown_language_or_key_is_rejected():
    with pytest.raises(ValueError):
        Translator("fr-FR")
    with pytest.raises(KeyError):
        Translator("en-US")("not-real")
