from pathlib import Path

from fastapi.testclient import TestClient

import fastapi_app
from utils import online_models


client = TestClient(fastapi_app.app)


def write_env(text: str) -> None:
    online_models.ENV_PATH.write_text(text, encoding='utf-8')


def read_env() -> str:
    return online_models.ENV_PATH.read_text(encoding='utf-8')


def test_create_new_profile_and_selectable(tmp_path):
    original = online_models.ENV_PATH.read_text(encoding='utf-8') if online_models.ENV_PATH.exists() else None
    try:
        write_env(
            'ONLINE_MODEL_ACTIVE_PROFILE=default\n'
            'ONLINE_MODEL_PROFILE_COUNT=1\n'
            'ONLINE_MODEL_PROFILE_1_NAME=default\n'
            'ONLINE_MODEL_PROFILE_1_BASE_URL=https://api.example.com\n'
            'ONLINE_MODEL_PROFILE_1_API_KEY=key1\n'
            'ONLINE_MODEL_PROFILE_1_DEFAULT_MODEL=model-a\n'
            'ONLINE_MODEL_PROFILE_1_MODEL_LIST_JSON=["model-a","model-b"]\n'
        )

        resp = client.post(
            '/api/model/profiles/save',
            json={
                'original_name': '',
                'name': '新配置',
                'base_url': 'https://api.example.com/v2',
                'api_key': 'key2',
                'default_model': 'model-b',
                'models': ['model-a', 'model-b'],
            },
        )
        assert resp.status_code == 200, resp.text

        profiles = client.get('/api/model/profiles').json()
        assert profiles['active'] == '新配置'
        assert 'default' in profiles['profile_names']
        assert '新配置' in profiles['profile_names']

        selected = client.get('/api/model/profile', params={'name': '新配置'})
        assert selected.status_code == 200
        payload = selected.json()['profile']
        assert payload['name'] == '新配置'
        assert payload['base_url'] == 'https://api.example.com/v2'
        assert payload['api_key'] == 'key2'
        assert payload['default_model'] == 'model-b'
        assert payload['models'] == ['model-a', 'model-b']
    finally:
        if original is None:
            online_models.ENV_PATH.unlink(missing_ok=True)
        else:
            online_models.ENV_PATH.write_text(original, encoding='utf-8')


def test_duplicate_profile_name_is_rejected(tmp_path):
    original = online_models.ENV_PATH.read_text(encoding='utf-8') if online_models.ENV_PATH.exists() else None
    try:
        write_env(
            'ONLINE_MODEL_ACTIVE_PROFILE=default\n'
            'ONLINE_MODEL_PROFILE_COUNT=2\n'
            'ONLINE_MODEL_PROFILE_1_NAME=default\n'
            'ONLINE_MODEL_PROFILE_1_BASE_URL=https://api.example.com\n'
            'ONLINE_MODEL_PROFILE_1_API_KEY=key1\n'
            'ONLINE_MODEL_PROFILE_1_DEFAULT_MODEL=model-a\n'
            'ONLINE_MODEL_PROFILE_1_MODEL_LIST_JSON=["model-a"]\n'
            'ONLINE_MODEL_PROFILE_2_NAME=新配置\n'
            'ONLINE_MODEL_PROFILE_2_BASE_URL=https://api.example.com/v2\n'
            'ONLINE_MODEL_PROFILE_2_API_KEY=key2\n'
            'ONLINE_MODEL_PROFILE_2_DEFAULT_MODEL=model-b\n'
            'ONLINE_MODEL_PROFILE_2_MODEL_LIST_JSON=["model-b"]\n'
        )

        resp = client.post(
            '/api/model/profiles/save',
            json={
                'original_name': 'default',
                'name': '新配置',
                'base_url': 'https://api.example.com/v3',
                'api_key': 'key3',
                'default_model': 'model-c',
                'models': ['model-c'],
            },
        )
        assert resp.status_code == 409
        assert '配置名重复' in resp.text
    finally:
        if original is None:
            online_models.ENV_PATH.unlink(missing_ok=True)
        else:
            online_models.ENV_PATH.write_text(original, encoding='utf-8')


def test_fetch_models_allows_ollama_without_api_key(monkeypatch):
    original = online_models.ENV_PATH.read_text(encoding='utf-8') if online_models.ENV_PATH.exists() else None
    try:
        write_env('')
        monkeypatch.setattr(fastapi_app, 'list_available_models', lambda *args, **kwargs: ['Qwen3.5:4b-q8_0'])

        resp = client.post(
            '/api/model/profiles/fetch-models',
            json={
                'original_name': '',
                'name': 'ollama-local',
                'base_url': 'http://127.0.0.1:11434',
                'api_key': '',
            },
        )

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload['models'] == ['Qwen3.5:4b-q8_0']
        saved = client.get('/api/model/profile', params={'name': 'ollama-local'})
        assert saved.status_code == 200
        profile = saved.json()['profile']
        assert profile['api_key'] == ''
        assert profile['models'] == ['Qwen3.5:4b-q8_0']
    finally:
        if original is None:
            online_models.ENV_PATH.unlink(missing_ok=True)
        else:
            online_models.ENV_PATH.write_text(original, encoding='utf-8')
