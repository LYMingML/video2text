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


def _is_speaker_model(model_name: str) -> bool:
    raw = model_name.split(" ")[0].strip().lower()
    return "-spk" in raw or "speaker" in raw


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
        "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online",
        "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch": "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "paraformer-en": "paraformer-en",
        "iic/paraformer-en": "paraformer-en",
        # 部分环境无 spk 专用模型时，回退到基础识别模型并在后处理中做角色分段标注
        "iic/paraformer-zh-spk": "paraformer-zh",
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


def _get_model(model_name: str = "paraformer-zh", device: str = "cuda:0", speaker_mode: bool = False):
    """
    懒加载并缓存 FunASR AutoModel 实例。
    首次调用会从 ModelScope 下载模型（约 500MB）。
    """
    model_name = _normalize_model_name(model_name)
    cache_key = (model_name, device, speaker_mode)
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
    model_kwargs = dict(
        model=model_name,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},  # VAD 最长单段 30s
        punc_model="ct-punc",
        device=device,
        disable_update=True,  # 不检查更新，加快启动
        hub="ms",             # 从 ModelScope 下载
    )
    if speaker_mode:
        model_kwargs["spk_model"] = "cam++"
        logger.info("说话人分离模式：加载 cam++ 说话人模型")
    model = AutoModel(**model_kwargs)
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


def _label_speaker_fallback(segments: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """无显式说话人信息时，按停顿时长做简单角色分段。"""
    if not segments:
        return []

    labeled: list[tuple[float, float, str]] = []
    speaker = 1
    last_end = segments[0][0]
    for start, end, text in segments:
        if start - last_end >= 1.2:
            speaker = 2 if speaker == 1 else 1
        labeled.append((start, end, f"角色{speaker}: {text}"))
        last_end = end
    return labeled


def _parse_sentence_info(item: dict) -> list[tuple[float, float, str, str]]:
    """解析 FunASR 返回中的 sentence_info，说话人字段尽量兼容多种键名。"""
    info = item.get("sentence_info") or []
    parsed: list[tuple[float, float, str, str]] = []
    for seg in info:
        if not isinstance(seg, dict):
            continue
        txt = str(seg.get("text", "")).strip()
        if not txt:
            continue

        start_raw = seg.get("start", seg.get("start_time", seg.get("begin", 0)))
        end_raw = seg.get("end", seg.get("end_time", seg.get("finish", start_raw)))
        try:
            start = float(start_raw)
            end = float(end_raw)
        except Exception:
            continue

        # 兼容 ms / s 两种单位
        if start > 1000 or end > 1000:
            start /= 1000.0
            end /= 1000.0

        speaker = str(
            seg.get("spk", seg.get("speaker", seg.get("speaker_id", seg.get("spkid", ""))))
        ).strip()
        parsed.append((start, end, txt, speaker))
    return parsed


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

    requested_model_name = model_name.split(" ")[0].strip()
    actual_model_name = _normalize_model_name(model_name)
    speaker_mode = _is_speaker_model(requested_model_name)

    if progress_cb:
        progress_cb(0.0, f"加载模型: {actual_model_name}...")

    model = _get_model(model_name=actual_model_name, device=device, speaker_mode=speaker_mode)

    if progress_cb:
        progress_cb(0.1, f"{actual_model_name} 语音识别中...")

    # language="auto" 让模型自动检测语言
    lang = language if language != "auto" else "auto"

    gen_kwargs: dict = dict(
        input=audio_path,
        language=lang,
        use_itn=True,          # 逆文本规范化（数字/日期等）
        batch_size_s=300,      # 分批处理，避免显存溢出（每批最多 300s 音频）
    )
    if not speaker_mode:
        # 说话人分离时不合并 VAD，保留说话人边界
        gen_kwargs["merge_vad"] = True
        gen_kwargs["merge_length_s"] = 15
    res = model.generate(**gen_kwargs)

    if progress_cb:
        progress_cb(0.85, "解析时间戳...")

    if not res:
        return []

    # ── 诊断日志：speaker mode 时打印第一条原始结果，帮助排查 cam++ 是否生效 ──
    if speaker_mode:
        first = res[0]
        logger.info("[SPK-DIAG] res[0].keys() = %s", list(first.keys()))
        si = first.get("sentence_info")
        if si:
            logger.info("[SPK-DIAG] sentence_info[0] = %s", si[0] if si else '[]')
            logger.info("[SPK-DIAG] sentence_info count = %d", len(si))
        else:
            logger.warning("[SPK-DIAG] sentence_info 为空！cam++ 未生效或模型不支持说话人分离")
        logger.info("[SPK-DIAG] res[0]['text'][:80] = %r", first.get("text", "")[:80])

    segments: list[tuple[float, float, str]] = []
    speaker_id_map: dict[str, int] = {}
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

        if speaker_mode:
            sentence_info_segments = _parse_sentence_info(item)
            if sentence_info_segments:
                for start_s, end_s, sentence_text, spk_raw in sentence_info_segments:
                    spk_key = spk_raw or "unknown"
                    if spk_key not in speaker_id_map:
                        speaker_id_map[spk_key] = len(speaker_id_map) + 1
                    role = speaker_id_map[spk_key]
                    sentence_text = re.sub(r"\s+", " ", sentence_text).strip()
                    if sentence_text:
                        segments.append((start_s, end_s, f"角色{role}: {sentence_text}"))
                        fallback_cursor = max(fallback_cursor, end_s)
                # sentence_info 已提供了更细粒度时间戳，优先使用
                continue
            # sentence_info 为空时降级：按标点拆句后用停顿估算角色

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
        if speaker_mode:
            sub_segs = _label_speaker_fallback(sub_segs)
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
