"""
faster-whisper ASR 后端
使用 CTranslate2 推理，支持 CUDA 和 CPU

CTranslate2 在 sm_61 (Pascal, 如 Tesla P4) 上支持的 compute_type：
  ✅ int8           - 推荐，最节省显存
  ✅ int8_float32   - 兼容 sm_61
  ✅ float32        - 精度最高但最慢
  ❌ float16        - 需要 Volta+ (sm_70)
  ❌ int8_float16   - 需要 Volta+ (sm_70)
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from backends import register_asr
from backends.base_asr import ASRBackend

logger = logging.getLogger(__name__)

# Whisper 幻觉/水印清理模式
_HALLUCINATION_PATTERNS = [
    re.compile(r"\[cite:\s*\d+\]", re.IGNORECASE),
    re.compile(r"\[citation:\s*\d+\]", re.IGNORECASE),
    re.compile(r"\(cite:\s*\d+\)", re.IGNORECASE),
    re.compile(r"subtitle\s*by\s*.*$", re.IGNORECASE),
]


def _clean_hallucinations(text: str) -> str:
    """清理 Whisper 幻觉输出（水印、引用标记等）。"""
    cleaned = text
    for pattern in _HALLUCINATION_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip()


def _fix_time_gaps(
    segments: list[tuple[float, float, str]],
    max_gap_seconds: float = 300.0,
    avg_char_duration: float = 0.15,
) -> list[tuple[float, float, str]]:
    """修复时间跳跃过大的问题。"""
    if len(segments) < 2:
        return segments

    fixed: list[tuple[float, float, str]] = []
    cursor = segments[0][0]

    for start, end, text in segments:
        gap = start - cursor
        if gap > max_gap_seconds:
            logger.warning(
                f"检测到时间跳跃: {gap:.0f}s ({gap/60:.1f}min)，"
                f"从 {cursor:.1f}s 跳到 {start:.1f}s，重新估算"
            )
            est_duration = max(1.0, min(10.0, len(text) * avg_char_duration))
            fixed.append((round(cursor, 3), round(cursor + est_duration, 3), text))
            cursor = cursor + est_duration
        else:
            if start < cursor:
                start = cursor
                duration = max(1.0, len(text) * avg_char_duration)
                end = start + duration
            fixed.append((round(start, 3), round(end, 3), text))
            cursor = max(cursor, end)

    return fixed


_model_cache: dict = {}  # {(model_name, device, compute_type): WhisperModel}


def _get_model(
    model_name: str = "medium",
    device: str = "cuda",
    compute_type: str = "int8",
):
    """懒加载并缓存 WhisperModel 实例"""
    key = (model_name, device, compute_type)
    if key in _model_cache:
        logger.info(f"[Whisper] 使用缓存模型: {model_name} on {device} ({compute_type})")
        return _model_cache[key]

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ImportError(
            "faster-whisper 未安装，请运行：\n"
            "  pip install faster-whisper stable-ts"
        )

    logger.info(f"[Whisper] 加载模型: {model_name}, device={device}, compute_type={compute_type}")

    # Tesla P4 (sm_61) 只支持 int8，不支持 float16 / int8_float16
    fallback_chain = [compute_type]
    if compute_type == "float16" and device == "cuda":
        fallback_chain = ["float16", "int8_float16", "int8"]

    model = None
    used_type = compute_type
    used_device = device
    for ct in fallback_chain:
        try:
            logger.info(f"[Whisper] 尝试: device={device}, compute_type={ct}")
            model = WhisperModel(model_name, device=device, compute_type=ct)
            used_type = ct
            break
        except Exception as e:
            logger.warning(f"[Whisper] device={device}, compute_type={ct} 加载失败: {e}")

    if model is None:
        logger.warning("[Whisper] GPU 加载全部失败，回退 CPU int8")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        key = (model_name, "cpu", "int8")
        used_type = "int8"
        used_device = "cpu"

    actual_device = used_device
    try:
        if hasattr(model, "device"):
            actual_device = str(model.device)
    except Exception:
        pass

    is_gpu = "cuda" in actual_device.lower() or "gpu" in actual_device.lower()
    logger.info(f"[Whisper] 模型加载完成: device={actual_device}, compute_type={used_type}, 实际设备={'GPU' if is_gpu else 'CPU'}")
    _model_cache[key] = model
    return model


@register_asr
class WhisperASR(ASRBackend):
    """faster-whisper ASR 后端（CTranslate2 推理）"""

    @property
    def name(self) -> str:
        return "faster-whisper（多语言）"

    @property
    def description(self) -> str:
        return "基于 CTranslate2 的 Whisper 推理，支持 99 种语言，Tesla P4 兼容"

    @property
    def default_model(self) -> str:
        return "medium"

    @property
    def supported_models(self) -> list[str]:
        return ["tiny", "base", "small", "medium", "large-v3"]

    @property
    def default_chunk_seconds(self) -> int:
        return 120

    @property
    def default_overlap_seconds(self) -> int:
        return 10

    @property
    def sample_rate(self) -> int:
        return 16000

    def transcribe(
        self,
        audio_path: str,
        model_name: str = "",
        language: str = "auto",
        device: str = "auto",
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> list[tuple[float, float, str]]:
        model_name = model_name or self.default_model
        compute_type = "int8"
        actual_device = device
        if actual_device == "auto":
            actual_device = "cuda"

        if progress_cb:
            progress_cb(0.0, f"加载 Whisper {model_name} 模型...")

        model = _get_model(model_name, actual_device, compute_type)

        if progress_cb:
            progress_cb(0.1, "Whisper 语音识别中...")

        segments_iter, info = model.transcribe(
            audio_path,
            beam_size=5,
            best_of=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            word_timestamps=False,
            condition_on_previous_text=True,
            language=language if language != "auto" else None,
            initial_prompt="以下是普通话的句子。" if (language in ("zh", None)) else None,
        )

        lang_str = info.language or "unknown"
        lang_prob = getattr(info, "language_probability", 0.0)
        logger.info(f"检测到语言: {lang_str} ({lang_prob:.1%})")

        if progress_cb:
            progress_cb(0.15, f"识别语言: {lang_str}，转录中...")

        total_duration = getattr(info, "duration", None) or 1.0
        results: list[tuple[float, float, str]] = []

        for seg in segments_iter:
            text = seg.text.strip()
            text = _clean_hallucinations(text)
            if text:
                results.append((seg.start, seg.end, text))
            if progress_cb and total_duration > 0:
                ratio = min(0.9, 0.15 + 0.75 * (seg.end / total_duration))
                progress_cb(ratio, f"已处理 {seg.end:.0f}s / {total_duration:.0f}s")

        if progress_cb:
            progress_cb(1.0, f"完成，共 {len(results)} 条字幕")

        results = _fix_time_gaps(results, max_gap_seconds=300.0)
        return results

    def unload(self) -> None:
        global _model_cache
        for model in _model_cache.values():
            del model
        _model_cache.clear()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
