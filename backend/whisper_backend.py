"""
faster-whisper 后端
使用 CTranslate2 CUDA 推理，支持 Tesla P4 sm_61

CTranslate2 在 sm_61 (Pascal) 上支持的 compute_type：
  ✅ int8           - 推荐，最节省显存
  ✅ int8_float32   - 兼容 sm_61
  ✅ float32        - 精度最高但最慢
  ❌ float16        - 需要 Volta+ (sm_70)
  ❌ int8_float16   - 需要 Volta+ (sm_70)

PyTorch 要求：torch==2.3.1+cu121（支持 sm_61；2.4+ 最低 sm_70）
"""

from __future__ import annotations
import logging
from typing import Callable

logger = logging.getLogger(__name__)

_model_cache: dict = {}  # {(model_name, device, compute_type): WhisperModel}


def _get_model(
    model_name: str = "medium",
    device: str = "cuda",
    compute_type: str = "int8",
):
    """懒加载并缓存 WhisperModel 实例"""
    key = (model_name, device, compute_type)
    if key in _model_cache:
        return _model_cache[key]

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ImportError(
            "faster-whisper 未安装，请运行：\n"
            "  pip install faster-whisper stable-ts"
        )

    logger.info(f"加载 Whisper 模型: {model_name} ({compute_type}) on {device}")

    # Tesla P4 (sm_61) 只支持 int8，不支持 float16 / int8_float16
    # 自动回退策略：float16 → int8_float16 → int8 → cpu int8
    fallback_chain = [compute_type]
    if compute_type == "float16" and device == "cuda":
        fallback_chain = ["float16", "int8_float16", "int8"]

    model = None
    used_type = compute_type
    for ct in fallback_chain:
        try:
            model = WhisperModel(model_name, device=device, compute_type=ct)
            used_type = ct
            break
        except Exception as e:
            logger.warning(f"compute_type={ct} 加载失败: {e}，尝试下一个...")

    if model is None:
        logger.warning("GPU 模式全部失败，回退 CPU int8")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        key = (model_name, "cpu", "int8")
        used_type = "int8"

    logger.info(f"Whisper 模型加载完成 (实际 compute_type={used_type})")
    _model_cache[key] = model
    return model


def transcribe(
    audio_path: str,
    model_name: str = "medium",
    language: str | None = None,
    device: str = "cuda",
    compute_type: str = "int8",
    progress_cb: Callable[[float, str], None] | None = None,
) -> list[tuple[float, float, str]]:
    """
    使用 faster-whisper 转录音频。

    Args:
        audio_path:   WAV 文件路径
        model_name:   Whisper 模型名称（tiny/base/small/medium/large-v3 等）
        language:     语言代码，None 表示自动检测
        device:       "cuda" 或 "cpu"
        compute_type: "int8"（P4 推荐）/ "float16" / "int8_float16"
        progress_cb:  可选进度回调 (progress_ratio, message)

    Returns:
        [(start_s, end_s, text), ...] 时间戳单位为秒
    """
    if progress_cb:
        progress_cb(0.0, f"加载 Whisper {model_name} 模型...")

    model = _get_model(model_name, device, compute_type)

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
        if text:
            results.append((seg.start, seg.end, text))
        # 粗略进度：按已处理时长估算
        if progress_cb and total_duration > 0:
            ratio = min(0.9, 0.15 + 0.75 * (seg.end / total_duration))
            progress_cb(ratio, f"已处理 {seg.end:.0f}s / {total_duration:.0f}s")

    if progress_cb:
        progress_cb(1.0, f"完成，共 {len(results)} 条字幕")

    return results


def unload():
    """释放所有缓存的模型显存"""
    global _model_cache
    for model in _model_cache.values():
        del model
    _model_cache.clear()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass
