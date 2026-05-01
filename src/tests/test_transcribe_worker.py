import types
from pathlib import Path
from unittest.mock import MagicMock

import fastapi_app
import core.config as _cfg
import core.workspace as _ws
import core.transcribe_logic as _tl


def _patch_common(monkeypatch, workspace, job_dir):
    monkeypatch.setattr(fastapi_app, 'WORKSPACE_DIR', workspace)
    monkeypatch.setattr(fastapi_app, '_resolve_job_dir_for_input', lambda _path: job_dir)
    monkeypatch.setattr(
        fastapi_app,
        '_do_transcribe_stream',
        lambda *args, **kwargs: iter([('⏳ 转写中', [(0.0, 1.0, '第一句'), (1.0, 2.0, '第二句')])]),
    )
    monkeypatch.setattr(fastapi_app, '_parse_lang_code', lambda _language: 'zh')
    monkeypatch.setattr(fastapi_app, '_looks_non_chinese_text', lambda _text: False)
    monkeypatch.setattr(fastapi_app, '_guess_source_lang', lambda _lang, _text: 'zh')
    monkeypatch.setattr(fastapi_app, '_save_task_meta', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fastapi_app, '_cleanup_job_source_media', lambda *_args, **_kwargs: None)


def test_run_transcribe_worker_initializes_job_dir(monkeypatch, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    job_dir = workspace / 'job1'
    job_dir.mkdir()
    input_file = tmp_path / 'input.mp4'
    input_file.write_text('video', encoding='utf-8')

    _patch_common(monkeypatch, workspace, job_dir)
    job = fastapi_app.JobState(job_id='job-test', running=True)

    fastapi_app._run_transcribe_worker(
        job,
        str(input_file),
        'FunASR（Paraformer）',
        '自动检测',
        'medium',
        'paraformer-zh ⭐ 普通话精度推荐',
        'CPU',
    )

    assert job.failed is False
    assert job.done is True
    assert job.running is False
    assert job.current_job == 'job1'
    assert job.current_prefix == 'input'
    assert (job_dir / 'input.srt').exists()
    assert (job_dir / 'input.txt').exists()
    assert '第一句' in job.plain_text


def test_run_transcribe_worker_writes_plain_text_output(monkeypatch, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    job_dir = workspace / 'job1'
    job_dir.mkdir()
    input_file = tmp_path / 'input.mp4'
    input_file.write_text('video', encoding='utf-8')

    _patch_common(monkeypatch, workspace, job_dir)

    job = fastapi_app.JobState(job_id='job-plain', running=True)

    fastapi_app._run_transcribe_worker(
        job,
        str(input_file),
        'FunASR（Paraformer）',
        '自动检测',
        'medium',
        'paraformer-zh ⭐ 普通话精度推荐',
        'CPU',
    )

    assert job.failed is False
    assert (job_dir / 'input.txt').exists()
    assert not (job_dir / 'input.ori.txt').exists()
    assert not (job_dir / 'input.矫正.txt').exists()


def test_do_transcribe_stream_reuses_existing_wav_without_extract(monkeypatch, tmp_path):
    job_dir = tmp_path / 'job1'
    job_dir.mkdir()
    wav_path = job_dir / 'job1.wav'
    wav_path.write_text('wav', encoding='utf-8')

    def fail_extract(*_args, **_kwargs):
        raise AssertionError('extract_audio should not be called when input is already the target wav')

    # Mock the ASR backend registry to return a mock backend
    mock_asr = MagicMock()
    mock_asr.name = "FunASR"
    mock_asr.default_model = "paraformer-zh"
    mock_asr.default_chunk_seconds = 120
    mock_asr.default_overlap_seconds = 10
    mock_asr.transcribe.return_value = [(0.0, 1.0, '第一句')]
    monkeypatch.setattr(
        'core.transcribe_logic.get_asr_backend',
        lambda name: mock_asr,
    )

    monkeypatch.setattr(_tl, 'extract_audio', fail_extract)
    monkeypatch.setattr(_tl, 'get_audio_duration', lambda _path: 1.0)
    monkeypatch.setattr(_tl, '_stage_source_media_to_temp_video', lambda path, preferred_name=None: path)
    monkeypatch.setattr(_tl, '_cleanup_job_source_media', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_tl, '_pick_funasr_model_for_language', lambda *_args, **_kwargs: 'paraformer-zh')
    monkeypatch.setattr(_cfg, 'STOP_EVENT', types.SimpleNamespace(is_set=lambda: False))

    outputs = list(
        _tl._do_transcribe_stream(
            str(wav_path),
            'FunASR（Paraformer）',
            '自动检测',
            'medium',
            'paraformer-zh ⭐ 普通话精度推荐',
            'job1',
            'CPU',
            job_dir,
            log_cb=None,
        )
    )

    assert outputs
    statuses = [status for status, _segments in outputs]
    assert any('复用现有 WAV 文件' in status for status in statuses)
    assert any('转写进度' in status for status in statuses)
