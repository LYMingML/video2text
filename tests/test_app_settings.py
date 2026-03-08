from utils.online_models import ENV_PATH, load_app_settings, save_app_settings


def test_load_app_settings_defaults_when_missing():
    original = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else None
    try:
        ENV_PATH.write_text('', encoding='utf-8')
        settings = load_app_settings()
        assert settings['APP_PORT'] == '7881'
        assert settings['BROWSER_DEBUG_PORT'] == '9222'
        assert settings['DEFAULT_BACKEND'] == 'FunASR（Paraformer）'
        assert settings['DEFAULT_FUNASR_MODEL'] == 'paraformer-zh ⭐ 普通话精度推荐'
        assert settings['DEFAULT_WHISPER_MODEL'] == 'medium'
    finally:
        if original is None:
            ENV_PATH.unlink(missing_ok=True)
        else:
            ENV_PATH.write_text(original, encoding='utf-8')


def test_save_app_settings_persists_values():
    original = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else None
    try:
        ENV_PATH.write_text('', encoding='utf-8')
        save_app_settings(
            {
                'APP_PORT': '9001',
                'BROWSER_DEBUG_PORT': '9333',
                'DEFAULT_BACKEND': 'faster-whisper（多语言）',
                'DEFAULT_FUNASR_MODEL': 'iic/SenseVoiceSmall',
                'DEFAULT_WHISPER_MODEL': 'large-v3',
            }
        )
        settings = load_app_settings()
        assert settings['APP_PORT'] == '9001'
        assert settings['BROWSER_DEBUG_PORT'] == '9333'
        assert settings['DEFAULT_BACKEND'] == 'faster-whisper（多语言）'
        assert settings['DEFAULT_FUNASR_MODEL'] == 'iic/SenseVoiceSmall'
        assert settings['DEFAULT_WHISPER_MODEL'] == 'large-v3'
    finally:
        if original is None:
            ENV_PATH.unlink(missing_ok=True)
        else:
            ENV_PATH.write_text(original, encoding='utf-8')
