from pathlib import Path

from fastapi.testclient import TestClient

import fastapi_app


client = TestClient(fastapi_app.app)


def test_audio_upload_is_accepted_and_routed_to_worker(monkeypatch, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    temp_video = workspace / 'temp_video'
    temp_video.mkdir()

    monkeypatch.setattr(fastapi_app.core, 'WORKSPACE_DIR', workspace)
    monkeypatch.setattr(fastapi_app.core, 'TEMP_VIDEO_DIR', temp_video)
    monkeypatch.setattr(fastapi_app.core, '_prune_temp_video_dir', lambda *args, **kwargs: None)

    captured = {}

    def fake_worker(job, video_path, backend, language, whisper_model, funasr_model, device):
        captured['video_path'] = video_path
        captured['backend'] = backend
        job.running = False
        job.done = True

    class ImmediateThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            if self._target:
                self._target(*self._args)

    monkeypatch.setattr(fastapi_app, '_run_transcribe_worker', fake_worker)
    monkeypatch.setattr(fastapi_app.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(fastapi_app, '_RUNTIME_JOB', None)

    resp = client.post(
        '/api/transcribe/start',
        data={
            'history_video': '',
            'backend': 'FunASR（Paraformer）',
            'language': '自动检测',
            'whisper_model': 'medium',
            'funasr_model': 'paraformer-zh ⭐ 普通话精度推荐',
            'device': 'CPU',
        },
        files={'video_file': ('sample.mp3', b'fake-audio', 'audio/mpeg')},
    )

    assert resp.status_code == 200, resp.text
    assert captured['video_path'].endswith('sample.mp3')
    assert Path(captured['video_path']).parent == temp_video
    assert Path(captured['video_path']).exists()


def test_audio_upload_rejects_unsupported_extension():
    resp = client.post(
        '/api/transcribe/start',
        data={
            'history_video': '',
            'backend': 'FunASR（Paraformer）',
            'language': '自动检测',
            'whisper_model': 'medium',
            'funasr_model': 'paraformer-zh ⭐ 普通话精度推荐',
            'device': 'CPU',
        },
        files={'video_file': ('sample.txt', b'not-media', 'text/plain')},
    )

    assert resp.status_code == 400
    assert '仅支持视频或音频文件上传' in resp.text