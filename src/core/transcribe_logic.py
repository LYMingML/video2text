"""
转录编排逻辑

从 main.py 的 _do_transcribe_stream / process 提取，去 Gradio 依赖。
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Iterator

from core.config import (
    STOP_EVENT,
    _SOURCE_MEDIA_EXTS,
    _has_nvidia_gpu,
    _parse_lang_code,
    _pick_funasr_model_for_language,
)
from core.workspace import (
    _cleanup_job_source_media,
    _resolve_job_dir_for_input,
    _save_task_meta,
    _schedule_video_deletion,
    _stage_source_media_to_temp_video,
)
from utils.audio import extract_audio, get_audio_duration, split_audio_chunks

logger = logging.getLogger("video2text")


def _strip_fractional_time(text: str) -> str:
    """兜底去除时间字符串中的小数秒，如 00:03:12.345 -> 00:03:12。"""
    text = re.sub(r"(\d{2}:\d{2}:\d{2})\.\d+", r"\1", text)
    text = re.sub(r"(\d{1,2}:\d{2})\.\d+", r"\1", text)
    return text


def _format_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def do_transcribe(
    video_path: str,
    backend_cls_name: str,
    language: str,
    model_name: str,
    file_prefix: str,
    device: str,
    job_dir: Path,
    log_cb: Callable[[str], None] | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    pre_chunked_items: list[tuple[str, float, float]] | None = None,
    pre_duration: float | None = None,
) -> list[tuple[float, float, str]]:
    """
    提取音频并分片转录，使用注册表中的 ASR 后端。

    Args:
        video_path: 输入视频/音频文件路径
        backend_cls_name: ASR 后端类名（如 "VibeVoiceASR"）
        language: 语言代码或显示名称
        model_name: 模型名称
        file_prefix: 输出文件前缀
        device: 计算设备（"CUDA"/"CPU"/"auto"）
        job_dir: job 工作目录
        log_cb: 日志回调
        progress_cb: 进度回调 (ratio, message)
        pre_chunked_items: 预处理阶段已完成的分片列表，传入后跳过提取/分片
        pre_duration: 预处理阶段已获取的音频时长

    Returns:
        [(start, end, text), ...] 时间戳片段列表
    """
    from backends import get_asr_backend

    lang_code = _parse_lang_code(language)

    # ── 快速路径：已有预处理结果，直接进入转录 ──
    chunk_dir = None
    if pre_chunked_items is not None and pre_duration is not None:
        asr = get_asr_backend(backend_cls_name)
        if log_cb:
            log_cb(f"[ASR] 后端: {asr.name}, 模型: {model_name or asr.default_model}")

        actual_device = device
        if actual_device == "CUDA" and not _has_nvidia_gpu():
            if log_cb:
                log_cb("[DEVICE] 未检测到可用 NVIDIA GPU，强制回退 CPU")
            actual_device = "CPU"

        effective_model = model_name or asr.default_model
        effective_device = actual_device.lower()
        if effective_device == "cuda":
            effective_device = "cuda:0"

        if log_cb:
            log_cb(
                f"[ASR] 实际配置: backend={asr.name} model={effective_model} "
                f"device={effective_device} language={lang_code}"
            )

        chunk_seconds = asr.default_chunk_seconds
        overlap_seconds = asr.default_overlap_seconds
        duration = pre_duration
        chunk_items = pre_chunked_items
    else:
        # ── 完整路径：提取音频 + 分片 ──
        audio_path = str(job_dir / f"{file_prefix}.wav")

        # 1. 提取音频
        input_media = Path(video_path)
        target_audio = Path(audio_path)

        reuse_existing_wav = False
        try:
            reuse_existing_wav = (
                input_media.suffix.lower() == ".wav"
                and input_media.exists()
                and input_media.resolve() == target_audio.resolve()
            )
        except OSError:
            reuse_existing_wav = input_media.suffix.lower() == ".wav" and str(input_media) == str(target_audio)

        if reuse_existing_wav:
            if log_cb:
                log_cb("[STEP] 输入已是当前任务的 WAV 文件，直接复用...")
        else:
            if log_cb:
                log_cb("[STEP] 正在使用 ffmpeg 提取 WAV 文件...")
            if progress_cb:
                progress_cb(0.0, "提取 WAV 文件...")
            extract_audio(video_path, audio_path)
            _schedule_video_deletion(video_path, delay_seconds=60)

        staged_source_path = _stage_source_media_to_temp_video(video_path)
        if log_cb and Path(staged_source_path).suffix.lower() in _SOURCE_MEDIA_EXTS:
            log_cb(f"[MEDIA] 原始媒体已归档: {Path(staged_source_path).name}")
        _cleanup_job_source_media(job_dir)

        # 2. 读取时长
        if log_cb:
            log_cb("[STEP] 正在读取音频时长...")
        duration = get_audio_duration(audio_path)
        if log_cb:
            log_cb(f"[AUDIO] 时长: {duration:.1f}s")

        # 3. 加载后端
        asr = get_asr_backend(backend_cls_name)
        if log_cb:
            log_cb(f"[ASR] 后端: {asr.name}, 模型: {model_name or asr.default_model}")

        chunk_seconds = asr.default_chunk_seconds
        overlap_seconds = asr.default_overlap_seconds

        # 4. 获取实际设备
        actual_device = device
        if actual_device == "CUDA" and not _has_nvidia_gpu():
            if log_cb:
                log_cb("[DEVICE] 未检测到可用 NVIDIA GPU，强制回退 CPU")
            actual_device = "CPU"

        effective_model = model_name or asr.default_model
        effective_device = actual_device.lower()
        if effective_device == "cuda":
            effective_device = "cuda:0"

        if log_cb:
            log_cb(
                f"[ASR] 实际配置: backend={asr.name} model={effective_model} "
                f"device={effective_device} language={lang_code}"
            )

        # 5. 分片音频
        chunk_dir = job_dir / "chunks"

        if chunk_seconds > 0 and duration > chunk_seconds:
            if log_cb:
                log_cb(f"[STEP] 正在按 {chunk_seconds}s 分片音频（重叠 {overlap_seconds}s）...")
            if progress_cb:
                progress_cb(0.05, f"分片音频（每段 {chunk_seconds}s）...")

            chunk_items = split_audio_chunks(
                audio_path,
                str(chunk_dir),
                chunk_seconds=chunk_seconds,
                overlap_seconds=overlap_seconds,
            )
        else:
            if log_cb:
                log_cb("[CHUNK] 音频较短或后端自带切片，直接转写整段")
            chunk_items = [(audio_path, 0.0, duration)]

    if not chunk_items:
        return []

    total_chunks = len(chunk_items)
    all_segments: list[tuple[float, float, str]] = []
    if log_cb:
        log_cb(f"[CHUNK] 分片数: {total_chunks}，粒度: {chunk_seconds}s")

    # 6. 分片转录
    for idx, (chunk_path, start_s, end_s) in enumerate(chunk_items, start=1):
        if STOP_EVENT.is_set():
            if log_cb:
                log_cb("[STOP] 用户请求停止，结束转录")
            return all_segments

        if log_cb:
            log_cb(f"[CHUNK] {idx}/{total_chunks} 转写中: {start_s:.0f}s-{end_s:.0f}s")

        def _asr_progress(ratio: float, msg: str, _idx=idx, _total=total_chunks):
            if log_cb:
                log_cb(f"[PROGRESS][{_idx}/{_total}] {msg}")

        segs = asr.transcribe(
            chunk_path,
            model_name=effective_model,
            language=lang_code,
            device=effective_device,
            progress_cb=_asr_progress,
        )

        # 去重叠
        if total_chunks > 1 and idx > 1 and overlap_seconds > 0:
            cutoff = start_s + overlap_seconds
            deduped: list[tuple[float, float, str]] = []
            for s, e, t in segs:
                if e <= overlap_seconds:
                    continue
                if s < overlap_seconds:
                    s = overlap_seconds
                deduped.append((s, e, t))
            segs = deduped

        # 偏移到全局时间
        if start_s > 0:
            segs = [(s + start_s, e + start_s, t) for s, e, t in segs]
            if total_chunks > 1 and idx > 1 and overlap_seconds > 0:
                cutoff = start_s + overlap_seconds
                segs = [(max(s, cutoff), e, t) for s, e, t in segs if e > cutoff]

        all_segments.extend(segs)

        done_s = min(max(0.0, end_s), max(duration, 1e-6))
        ratio = max(0.0, min(0.95, done_s / max(duration, 1e-6)))
        eta_s = max(0, duration - done_s) if done_s > 0 else duration
        if progress_cb:
            progress_cb(
                ratio,
                f"转写进度：{int(ratio * 100)}% | 预计剩余 {_format_eta(eta_s)}",
            )

    # 7. 清理临时分片
    if chunk_dir is not None and chunk_dir.exists():
        shutil.rmtree(chunk_dir, ignore_errors=True)
        if log_cb:
            log_cb("[CLEANUP] 已清理临时分片目录")

    return all_segments
