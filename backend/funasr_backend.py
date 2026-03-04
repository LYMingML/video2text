"""
FunASR Paraformer 后端
使用阿里达摩院 Paraformer + fsmn-vad + ct-punc 管线
中文识别准确率优于 Whisper，推理速度快 5-15 倍

CUDA 兼容性：建议 torch==2.3.1+cu121（兼容 Tesla P4 sm_61）
"""

from __future__ import annotations
import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)

# 延迟导入，避免未安装 funasr 时影响其他后端
_model_cache: dict[tuple[str, str], object] = {}


def _normalize_model_name(model_name: str) -> str:
    """从 UI 文本中提取真实模型名。"""
    raw = model_name.split(" ")[0].strip()
    alias_map = {
        "paraformer": "paraformer",
        "iic/paraformer": "paraformer",
        "paraformer-zh": "paraformer-zh",
        "iic/paraformer-zh": "paraformer-zh",
        "paraformer-zh-streaming": "paraformer-zh-streaming",
        "iic/paraformer-zh-streaming": "paraformer-zh-streaming",
        "paraformer-en": "paraformer-en",
        "iic/paraformer-en": "paraformer-en",
        "iic/paraformer-zh-spk": "paraformer-zh",  # 当前版本无 spk 别名，回退普通话模型
        "paraformer-zh-spk": "paraformer-zh",
        "paraformer-en-spk": "paraformer-en",
        "iic/paraformer-en-spk": "paraformer-en",
        "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch": "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online",
        "iic/speech_paraformer-large-vad-punc_asr_nat-en-16k-common-vocab10020": "iic/speech_paraformer-large-vad-punc_asr_nat-en-16k-common-vocab10020",
        "iic/SenseVoiceSmall": "iic/SenseVoiceSmall",
        "iic/SenseVoice-Small": "iic/SenseVoice-Small",
        "iic/EfficientParaformer-large-zh": "EfficientParaformer-large-zh",
        "iic/EfficientParaformer-zh-en": "EfficientParaformer-zh-en",
        "EfficientParaformer-large-zh": "EfficientParaformer-large-zh",
        "EfficientParaformer-zh-en": "EfficientParaformer-zh-en",
        "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch": "speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch": "speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    }
    return alias_map.get(raw, raw)


def _get_model(model_name: str = "paraformer-zh", device: str = "cuda:0"):
    """
    懒加载并缓存 FunASR AutoModel 实例。
    首次调用会从 ModelScope 下载模型（约 500MB）。
    """
    model_name = _normalize_model_name(model_name)
    cache_key = (model_name, device)
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    try:
        from funasr import AutoModel
    except ImportError:
        raise ImportError(
            "FunASR 未安装，请运行：\n"
            "  pip install funasr modelscope"
        )

    logger.info(f"加载 FunASR 模型: {model_name}（首次使用会下载模型）...")
    model = AutoModel(
        model=model_name,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},  # VAD 最长单段 30s
        punc_model="ct-punc",
        device=device,
        disable_update=True,  # 不检查更新，加快启动
        hub="ms",             # 从 ModelScope 下载
    )
    logger.info(f"FunASR 模型加载完成: {model_name}")
    _model_cache[cache_key] = model
    return model


def _split_by_punctuation(
    text: str, timestamps: list[list[int]]
) -> list[tuple[float, float, str]]:
    """
    按中文句末标点将文本拆分为多个字幕条目。
    timestamps[i] = [start_ms, end_ms] 对应 text[i] 的字符级时间戳。
    """
    ENDINGS = set("。！？…!?")

    if not timestamps or len(timestamps) < len(text):
        # 时间戳不完整，整段作为一个条目
        if timestamps:
            return [(timestamps[0][0] / 1000, timestamps[-1][1] / 1000, text)]
        return []

    segments: list[tuple[float, float, str]] = []
    seg_start = 0

    for i, char in enumerate(text):
        is_last = (i == len(text) - 1)
        if char in ENDINGS or is_last:
            sentence = text[seg_start: i + 1].strip()
            if sentence:
                start_s = timestamps[seg_start][0] / 1000
                end_s = timestamps[i][1] / 1000
                segments.append((start_s, end_s, sentence))
            seg_start = i + 1

    return segments


def transcribe(
    audio_path: str,
    model_name: str = "paraformer-zh",
    language: str = "auto",
    device: str = "cuda:0",
    progress_cb: Callable[[float, str], None] | None = None,
) -> list[tuple[float, float, str]]:
    """
    使用 FunASR Paraformer 转录音频。

    Args:
        audio_path: WAV 文件路径
        model_name: FunASR 模型名
        language:   "auto" / "zh" / "en" / "yue" / "ja" / "ko" / "es"
        device:     "cuda:0" 或 "cpu"
        progress_cb: 可选进度回调 (progress_ratio, message)

    Returns:
        [(start_s, end_s, text), ...] 时间戳单位为秒
    """
    try:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
    except ImportError:
        def rich_transcription_postprocess(text):
            return text

    actual_model_name = _normalize_model_name(model_name)

    if progress_cb:
        progress_cb(0.0, f"加载模型: {actual_model_name}...")

    model = _get_model(model_name=actual_model_name, device=device)

    if progress_cb:
        progress_cb(0.1, f"{actual_model_name} 语音识别中...")

    # language="auto" 让模型自动检测语言
    lang = language if language != "auto" else "auto"

    res = model.generate(
        input=audio_path,
        language=lang,
        use_itn=True,          # 逆文本规范化（数字/日期等）
        merge_vad=True,        # 合并相邻短 VAD 段
        merge_length_s=15,     # 合并阈值：≤15s 的相邻段合并
        batch_size_s=300,      # 分批处理，避免显存溢出（每批最多 300s 音频）
    )

    if progress_cb:
        progress_cb(0.85, "解析时间戳...")

    if not res:
        return []

    segments: list[tuple[float, float, str]] = []
    fallback_cursor = 0.0
    for item in res:
        raw_text = item.get("text", "").strip()
        if not raw_text:
            continue

        # 去掉情感/事件标签（如 <|HAPPY|><|Speech|>）
        text = rich_transcription_postprocess(raw_text)
        if not text:
            text = raw_text

        # 二次清理残余标签，防止全是 token 导致空字幕
        text = re.sub(r"<\|[^|>]+\|>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        timestamps: list[list[int]] = item.get("timestamp", [])

        if not timestamps:
            # 没有时间戳时降级：按文本长度估算时长，避免返回空结果
            est_duration = max(1.5, min(12.0, len(text) / 4.0))
            start_s = fallback_cursor
            end_s = start_s + est_duration
            segments.append((start_s, end_s, text))
            fallback_cursor = end_s
            logger.warning(f"FunASR: 段落无时间戳，使用估算时间: {text[:30]}")
            continue

        # 按标点拆句，生成更精细的字幕条目
        sub_segs = _split_by_punctuation(text, timestamps)
        segments.extend(sub_segs)
        if sub_segs:
            fallback_cursor = max(fallback_cursor, sub_segs[-1][1])

    if progress_cb:
        progress_cb(1.0, f"完成，共 {len(segments)} 条字幕")

    return segments


def unload():
    """释放模型显存（可选调用）"""
    global _model_cache
    for model in _model_cache.values():
        del model
    _model_cache.clear()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass
