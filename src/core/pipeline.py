"""
四阶段流水线引擎

阶段1: 下载/上传  →  阶段2: WAV 提取  →  阶段3: ASR 转录  →  阶段4: 翻译
每个阶段一个 queue.Queue + 一个守护线程，任务通过 PipelineTask 对象在阶段间传递。
"""

from __future__ import annotations

import logging
import queue
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.config import (
    STOP_EVENT,
    WORKSPACE_DIR,
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
from backends import get_asr_backend, get_translate_backend

logger = logging.getLogger("video2text.pipeline")

# ---------------------------------------------------------------------------
# 任务数据
# ---------------------------------------------------------------------------

OUTPUT_LANG_SUFFIXES = {"zh", "en", "ja", "ko", "es", "fr", "de", "ru"}


@dataclass
class PipelineTask:
    """在流水线阶段间传递的任务对象。"""

    task_id: str

    # 输入
    video_path: str = ""
    audio_path: str = ""

    # ASR 配置
    asr_backend: str = ""        # 后端类名（如 "FunASRASR"）
    model_name: str = ""
    language: str = "auto"
    device: str = "auto"

    # 预处理结果
    duration: float = 0.0
    chunk_items: list[tuple[str, float, float]] = field(default_factory=list)
    chunk_seconds: int = 0
    overlap_seconds: int = 0

    # 转录结果
    segments: list[tuple[float, float, str]] = field(default_factory=list)

    # 翻译配置
    auto_translate: bool = False
    translate_backend: str = "SiliconFlowTranslate"
    translate_model: str = ""
    translate_base_url: str = ""
    translate_api_key: str = ""
    target_lang: str = "zh"

    # 翻译结果
    translated_segments: list[tuple[float, float, str]] = field(default_factory=list)

    # 工作目录
    job_dir: str = ""
    file_prefix: str = ""

    # 回调
    status_cb: Callable[[str, str], None] | None = None   # (task_id, status_msg)
    log_cb: Callable[[str, str], None] | None = None       # (task_id, log_line)
    progress_cb: Callable[[str, float, str], None] | None = None  # (task_id, ratio, msg)

    # 结果
    error: str = ""
    done: bool = False


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _notify_status(task: PipelineTask, msg: str):
    if task.status_cb:
        try:
            task.status_cb(task.task_id, msg)
        except Exception:
            pass


def _notify_log(task: PipelineTask, line: str):
    if task.log_cb:
        try:
            task.log_cb(task.task_id, line)
        except Exception:
            pass


def _notify_progress(task: PipelineTask, ratio: float, msg: str):
    if task.progress_cb:
        try:
            task.progress_cb(task.task_id, ratio, msg)
        except Exception:
            pass


def _log(task: PipelineTask, line: str):
    """同时写 logger 和通知回调。"""
    logger.info(f"[{task.task_id}] {line}")
    _notify_log(task, line)


# ---------------------------------------------------------------------------
# 流水线引擎
# ---------------------------------------------------------------------------

class Pipeline:
    """四阶段流水线：下载/上传 → WAV 提取 → 转录 → 翻译"""

    def __init__(self):
        self._download_queue: queue.Queue[PipelineTask | None] = queue.Queue()
        self._extract_queue: queue.Queue[PipelineTask | None] = queue.Queue()
        self._transcribe_queue: queue.Queue[PipelineTask | None] = queue.Queue()
        self._translate_queue: queue.Queue[PipelineTask | None] = queue.Queue()

        self._threads: list[threading.Thread] = []
        self._running = True
        self._start_workers()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def submit(self, task: PipelineTask):
        """提交任务到下载/上传队列。"""
        self._download_queue.put(task)

    def shutdown(self):
        """优雅关闭所有工作线程。"""
        self._running = False
        for _ in range(4):
            self._download_queue.put(None)
        for t in self._threads:
            t.join(timeout=5)

    # ------------------------------------------------------------------
    # 启动工作线程
    # ------------------------------------------------------------------

    def _start_workers(self):
        targets = [
            self._download_worker,
            self._extract_worker,
            self._transcribe_worker,
            self._translate_worker,
        ]
        for target in targets:
            t = threading.Thread(target=target, daemon=True, name=f"pipeline-{target.__name__}")
            t.start()
            self._threads.append(t)

    # ------------------------------------------------------------------
    # 阶段 1：下载/上传
    # ------------------------------------------------------------------

    def _download_worker(self):
        """处理文件准备、job 目录创建等。"""
        while self._running:
            task = self._download_queue.get()
            if task is None:
                break
            try:
                _log(task, "[STEP] 初始化任务...")
                _notify_progress(task, 0.0, "归档源文件...")
                _notify_status(task, "处理中")

                # 归档源文件到 temp_video
                staged = _stage_source_media_to_temp_video(task.video_path)
                task.video_path = staged

                # 创建 job 目录
                job_dir = _resolve_job_dir_for_input(staged)
                task.job_dir = str(job_dir)
                task.file_prefix = Path(staged).stem

                _cleanup_job_source_media(job_dir)
                _log(task, f"[INPUT] {Path(staged).name}")
                _log(task, f"[JOB] workspace/{job_dir.name}")

                _notify_progress(task, 0.02, "等待 ffmpeg 提取音频...")
                self._extract_queue.put(task)

            except Exception as e:
                logger.exception(f"[{task.task_id}] 下载阶段失败")
                task.error = str(e)
                task.done = True
                _notify_status(task, "失败")

    # ------------------------------------------------------------------
    # 阶段 2：WAV 提取
    # ------------------------------------------------------------------

    def _extract_worker(self):
        """ffmpeg 提取 WAV + 分片音频。"""
        while self._running:
            task = self._extract_queue.get()
            if task is None:
                break
            try:
                from utils.audio import extract_audio, get_audio_duration, split_audio_chunks
                from backends import get_asr_backend

                job_dir = Path(task.job_dir)
                audio_path = str(job_dir / f"{task.file_prefix}.wav")
                task.audio_path = audio_path

                input_media = Path(task.video_path)
                target_audio = Path(audio_path)

                # 检测是否可以复用现有 WAV
                reuse = False
                try:
                    reuse = (
                        input_media.suffix.lower() == ".wav"
                        and input_media.exists()
                        and input_media.resolve() == target_audio.resolve()
                    )
                except OSError:
                    reuse = (
                        input_media.suffix.lower() == ".wav"
                        and str(input_media) == str(target_audio)
                    )

                if reuse:
                    _log(task, "[STEP] 输入已是当前任务的 WAV 文件，直接复用")
                    _notify_progress(task, 0.05, "复用现有 WAV...")
                else:
                    _log(task, "[STEP] 正在使用 ffmpeg 提取 WAV 文件...")
                    _notify_progress(task, 0.03, "ffmpeg 提取音频中...")

                    extract_audio(task.video_path, audio_path)
                    _schedule_video_deletion(task.video_path, delay_seconds=60)

                _notify_progress(task, 0.05, "读取音频时长...")
                duration = get_audio_duration(audio_path)
                task.duration = duration
                _log(task, f"[AUDIO] 时长: {duration:.1f}s")

                # 安排 60 秒后删除临时视频文件
                if Path(task.video_path).suffix.lower() in _SOURCE_MEDIA_EXTS:
                    _log(task, f"[MEDIA] 原始媒体已归档: {Path(task.video_path).name}")

                # 分片音频
                asr = get_asr_backend(task.asr_backend)
                chunk_seconds = asr.default_chunk_seconds
                overlap_seconds = asr.default_overlap_seconds
                task.chunk_seconds = chunk_seconds
                task.overlap_seconds = overlap_seconds

                chunk_dir = job_dir / "chunks"
                if chunk_seconds > 0 and duration > chunk_seconds:
                    _log(task, f"[STEP] 正在按 {chunk_seconds}s 分片音频（重叠 {overlap_seconds}s）...")
                    _notify_progress(task, 0.06, f"分片音频（每段 {chunk_seconds}s）...")
                    task.chunk_items = split_audio_chunks(
                        audio_path,
                        str(chunk_dir),
                        chunk_seconds=chunk_seconds,
                        overlap_seconds=overlap_seconds,
                    )
                    _log(task, f"[CHUNK] 分片数: {len(task.chunk_items)}，粒度: {chunk_seconds}s")
                else:
                    _log(task, "[CHUNK] 音频较短或后端自带切片，直接转写整段")
                    task.chunk_items = [(audio_path, 0.0, duration)]

                _notify_progress(task, 0.08, "等待转录...")
                self._transcribe_queue.put(task)

            except Exception as e:
                logger.exception(f"[{task.task_id}] 提取阶段失败")
                task.error = str(e)
                task.done = True
                _notify_status(task, "失败")

    # ------------------------------------------------------------------
    # 阶段 3：ASR 转录
    # ------------------------------------------------------------------

    def _transcribe_worker(self):
        """ASR 转录，完成后决定是否投入翻译队列。"""
        while self._running:
            task = self._transcribe_queue.get()
            if task is None:
                break
            try:
                from core.transcribe_logic import do_transcribe

                _notify_progress(task, 0.1, "开始转录...")
                _log(task, f"[ASR] 后端={task.asr_backend} 模型={task.model_name} 语言={task.language} 设备={task.device}")

                def _log_hook(line: str):
                    _notify_log(task, line)

                def _progress_hook(ratio: float, msg: str):
                    _notify_progress(task, 0.1 + 0.7 * ratio, msg)

                segments = do_transcribe(
                    video_path=task.video_path,
                    backend_cls_name=task.asr_backend,
                    language=task.language,
                    model_name=task.model_name,
                    file_prefix=task.file_prefix,
                    device=task.device,
                    job_dir=Path(task.job_dir),
                    log_cb=_log_hook,
                    progress_cb=_progress_hook,
                    pre_chunked_items=task.chunk_items if task.chunk_items else None,
                    pre_duration=task.duration if task.duration > 0 else None,
                )
                task.segments = segments

                if STOP_EVENT.is_set():
                    _log(task, "[STOP] 用户请求停止")
                    task.done = True
                    _notify_status(task, "已停止")
                    continue

                _log(task, f"[ASR] 原始片段数: {len(segments)}")

                # 清理 chunks 目录
                chunk_dir = Path(task.job_dir) / "chunks"
                if chunk_dir.exists():
                    shutil.rmtree(chunk_dir, ignore_errors=True)
                    _log(task, "[CLEANUP] 已清理临时分片目录")

                # 保存原文 SRT/TXT
                _notify_progress(task, 0.85, "保存原文...")
                self._save_original_outputs(task)

                _log(task, "[DONE] 转录完成")
                _notify_progress(task, 0.9, "转录完成")

                if task.auto_translate:
                    self._translate_queue.put(task)
                else:
                    task.done = True
                    _notify_status(task, "完成")

            except Exception as e:
                logger.exception(f"[{task.task_id}] 转录阶段失败")
                task.error = str(e)
                task.done = True
                _notify_status(task, "失败")

    # ------------------------------------------------------------------
    # 阶段 4：翻译
    # ------------------------------------------------------------------

    def _translate_worker(self):
        """翻译字幕。"""
        while self._running:
            task = self._translate_queue.get()
            if task is None:
                break
            try:
                _notify_progress(task, 0.92, "开始翻译...")
                _log(task, f"[TRANS] 目标语言: {task.target_lang}")

                backend = get_translate_backend(task.translate_backend)

                def _t_log(line: str):
                    _notify_log(task, line)

                def _t_progress(completed: int, total: int, eta: float):
                    ratio = 0.92 + 0.07 * (completed / max(total, 1))
                    _notify_progress(task, ratio, f"翻译: {completed}/{total}")

                translated = backend.translate_segments(
                    segments=task.segments,
                    source_lang=_parse_lang_code(task.language),
                    target_lang=task.target_lang,
                    log_cb=_t_log,
                    progress_cb=_t_progress,
                    base_url=task.translate_base_url,
                    api_key=task.translate_api_key,
                    model_name=task.translate_model,
                )
                task.translated_segments = translated

                # 保存翻译后的 SRT/TXT
                self._save_translated_outputs(task)

                _log(task, f"[TRANS] 翻译完成: {len(translated)} 段")
                task.done = True
                _notify_status(task, "完成")
                _notify_progress(task, 1.0, "全部完成")

            except Exception as e:
                logger.exception(f"[{task.task_id}] 翻译阶段失败")
                task.error = str(e)
                task.done = True
                _notify_status(task, "失败（翻译）")

    # ------------------------------------------------------------------
    # 保存输出
    # ------------------------------------------------------------------

    def _save_original_outputs(self, task: PipelineTask):
        """保存原文 SRT 和 TXT。"""
        from utils.subtitle import (
            normalize_segments_timeline,
            save_srt,
            save_plain,
            segments_to_plain,
            collect_plain_text,
        )

        if not task.segments:
            return

        job_dir = Path(task.job_dir)
        prefix = task.file_prefix

        cleaned = normalize_segments_timeline(task.segments)
        _log(task, f"[ASR] 清洗后片段数: {len(cleaned)}")

        if not cleaned:
            return

        # 保存任务元数据
        from core.config import _guess_source_lang, _looks_non_chinese_text
        plain_text = collect_plain_text(cleaned)
        lang_code = _parse_lang_code(task.language)
        is_non_zh = lang_code in {"en", "ja", "ko", "es"} or _looks_non_chinese_text(plain_text)
        source_lang = _guess_source_lang(lang_code, plain_text) if is_non_zh else "zh"

        _save_task_meta(job_dir, {
            "file_prefix": prefix,
            "lang_code": lang_code,
            "source_lang": source_lang,
            "is_non_zh": is_non_zh,
        })

        srt_path = save_srt(cleaned, str(job_dir / f"{prefix}.srt"), normalize=False)
        txt_path = save_plain(cleaned, str(job_dir / f"{prefix}.txt"), normalize=False)
        _log(task, f"[OUT] 原文 SRT: {Path(srt_path).name}")
        _log(task, f"[OUT] 原文 TXT: {Path(txt_path).name}")

    def _save_translated_outputs(self, task: PipelineTask):
        """保存翻译后的 SRT 和 TXT。"""
        from utils.subtitle import normalize_segments_timeline, save_srt, save_plain

        if not task.translated_segments:
            return

        job_dir = Path(task.job_dir)
        prefix = task.file_prefix
        target = (task.target_lang or "zh").strip().lower() or "zh"

        cleaned = normalize_segments_timeline(task.translated_segments)
        srt_path = save_srt(cleaned, str(job_dir / f"{prefix}.{target}.srt"), normalize=False, wrap=True, is_translated=True)
        txt_path = save_plain(cleaned, str(job_dir / f"{prefix}.{target}.txt"), normalize=False)
        _log(task, f"[OUT] 翻译 SRT: {Path(srt_path).name}")
        _log(task, f"[OUT] 翻译 TXT: {Path(txt_path).name}")


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_pipeline_instance: Pipeline | None = None
_pipeline_lock = threading.Lock()


def get_pipeline() -> Pipeline:
    """获取全局流水线实例（懒初始化）。"""
    global _pipeline_instance
    if _pipeline_instance is None:
        with _pipeline_lock:
            if _pipeline_instance is None:
                _pipeline_instance = Pipeline()
    return _pipeline_instance
