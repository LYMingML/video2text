import os
from pathlib import Path

import core.config as _cfg
import core.workspace as _ws


def test_list_uploaded_videos_prefers_wav_and_excludes_temp_video_folder(monkeypatch, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    temp_video = workspace / 'temp_video'
    temp_video.mkdir()
    job_dir = workspace / 'job1'
    job_dir.mkdir()
    other_dir = workspace / 'job2'
    other_dir.mkdir()

    wav_file = job_dir / 'job1.wav'
    wav_file.write_text('wav', encoding='utf-8')
    old_video = temp_video / 'source.mp4'
    old_video.write_text('video', encoding='utf-8')
    newer_wav = other_dir / 'job2.wav'
    newer_wav.write_text('wav2', encoding='utf-8')
    os.utime(wav_file, (10, 10))
    os.utime(old_video, (5, 5))
    os.utime(newer_wav, (20, 20))

    monkeypatch.setattr(_ws, 'WORKSPACE_DIR', workspace)
    monkeypatch.setattr(_ws, 'TEMP_VIDEO_DIR', temp_video)

    videos = _ws._list_uploaded_videos()
    folders = _ws._list_job_folders()

    assert videos[0].endswith('job2/job2.wav')
    assert videos[1].endswith('job1/job1.wav')
    assert videos[-1].endswith('temp_video/source.mp4')
    assert 'temp_video' not in folders


def test_list_uploaded_videos_hides_same_named_video_when_audio_exists(monkeypatch, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    temp_video = workspace / 'temp_video'
    temp_video.mkdir()
    job_dir = workspace / 'lesson'
    job_dir.mkdir()

    wav_file = job_dir / 'lesson.wav'
    wav_file.write_text('wav', encoding='utf-8')
    same_name_video = temp_video / 'lesson.mp4'
    same_name_video.write_text('video2', encoding='utf-8')
    other_video = temp_video / 'other.mp4'
    other_video.write_text('video2', encoding='utf-8')

    monkeypatch.setattr(_ws, 'WORKSPACE_DIR', workspace)
    monkeypatch.setattr(_ws, 'TEMP_VIDEO_DIR', temp_video)

    videos = _ws._list_uploaded_videos()

    assert any(item.endswith('lesson/lesson.wav') for item in videos)
    assert not any(item.endswith('temp_video/lesson.mp4') for item in videos)
    assert any(item.endswith('temp_video/other.mp4') for item in videos)


def test_stage_source_media_moves_to_temp_video_and_prunes(monkeypatch, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    temp_video = workspace / 'temp_video'
    temp_video.mkdir()
    job_dir = workspace / 'job1'
    job_dir.mkdir()

    monkeypatch.setattr(_ws, 'WORKSPACE_DIR', workspace)
    monkeypatch.setattr(_ws, 'TEMP_VIDEO_DIR', temp_video)
    monkeypatch.setattr(_ws, 'TEMP_VIDEO_KEEP_COUNT', 10)
    monkeypatch.setattr(_ws, '_prune_temp_video_dir', lambda max_items=10: None)

    for idx in range(10):
        existing = temp_video / f'existing_{idx}.mp4'
        existing.write_text(str(idx), encoding='utf-8')

    source = job_dir / 'input.mp4'
    source.write_text('new video', encoding='utf-8')

    staged = Path(_ws._stage_source_media_to_temp_video(str(source)))

    assert staged.parent == temp_video
    assert staged.exists()
    assert not source.exists()
    assert staged.name in [p.name for p in temp_video.iterdir() if p.is_file()]


def test_resolve_job_dir_for_input_reuses_wav_parent_only(monkeypatch, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    temp_video = workspace / 'temp_video'
    temp_video.mkdir()
    job_dir = workspace / 'job1'
    job_dir.mkdir()
    wav_path = job_dir / 'job1.wav'
    wav_path.write_text('wav', encoding='utf-8')
    video_path = temp_video / 'clip.mp4'
    video_path.write_text('video', encoding='utf-8')

    monkeypatch.setattr(_ws, 'WORKSPACE_DIR', workspace)
    monkeypatch.setattr(_ws, 'TEMP_VIDEO_DIR', temp_video)

    assert _ws._resolve_job_dir_for_input(str(wav_path)) == job_dir
    resolved = _ws._resolve_job_dir_for_input(str(video_path))
    assert resolved != temp_video
    assert resolved.name == 'clip'
