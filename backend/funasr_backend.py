"""
FunASR Paraformer 后端
使用阿里达摩院 Paraformer + fsmn-vad + ct-punc 管线
中文识别准确率优于 Whisper，推理速度快 5-15 倍

GPU 加速选项：
  - CUDA:  NVIDIA GPU (Tesla P4 sm_61 需要 PyTorch 2.5.x 或更早)
  - XPU:   Intel GPU (需要 Intel oneAPI + intel-extension-for-pytorch)
  - CPU:   通用回退

安装 Intel GPU 支持：
  # 1. 安装 oneAPI Base Toolkit
  wget https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB
  sudo apt-key add GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB
  echo "deb https://apt.repos.intel.com/oneapi all main" | sudo tee /etc/apt/sources.list.d/oneAPI.list
  sudo apt update && sudo apt install -y intel-oneapi-base-toolkit

  # 2. 安装 Intel Extension for PyTorch
  source /opt/intel/oneapi/setvars.sh
  pip install intel-extension-for-pytorch

  # 3. 在 .env 中设置 PREFER_INTEL_GPU=1
"""

from __future__ import annotations
import logging
import os
import re
from typing import Callable

logger = logging.getLogger(__name__)

# 环境变量配置
PREFER_INTEL_GPU = os.environ.get("PREFER_INTEL_GPU", "0") == "1"

# 延迟导入，避免未安装 funasr 时影响其他后端
_model_cache: dict[tuple[str, str], object] = {}


def _detect_best_device() -> str:
    """
    检测最佳计算设备。

    优先级（可配置）：
    1. Intel XPU (如果 PREFER_INTEL_GPU=1 且 intel-extension-for-pytorch 可用)
    2. NVIDIA CUDA (如果可用且兼容)
    3. CPU (通用回退)

    Returns:
        设备字符串: "xpu", "cuda:0", 或 "cpu"
    """
    # 尝试 Intel XPU
    if PREFER_INTEL_GPU:
        try:
            import torch
            if hasattr(torch, 'xpu') and torch.xpu.is_available():
                logger.info("使用 Intel XPU (Intel GPU)")
                return "xpu"
        except Exception as e:
            logger.debug(f"Intel XPU 检测失败: {e}")

    # 尝试 NVIDIA CUDA
    try:
        import torch
        if torch.cuda.is_available():
            try:
                major, minor = torch.cuda.get_device_capability(0)
                # 实际测试 GPU 是否可用（PyTorch 2.5.x 支持 sm_61）
                test_tensor = torch.zeros(1, device="cuda:0")
                del test_tensor
                torch.cuda.empty_cache()
                logger.info(f"使用 NVIDIA CUDA (sm_{major}{minor})")
                return "cuda:0"
            except Exception as e:
                logger.warning(f"CUDA 设备测试失败: {e}，回退到 CPU")
    except Exception as e:
        logger.debug(f"CUDA 检测失败: {e}")

    logger.info("使用 CPU")
    return "cpu"


def _is_sensevoice_model(model_name: str) -> bool:
    """SenseVoice 系列模型自带标点和情感标注，不需要额外 punc_model。"""
    name = model_name.lower()
    return "sensevoice" in name or "sense_voice" in name or "sense-voice" in name


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


def _get_model(model_name: str = "paraformer-zh", device: str = "auto", speaker_mode: bool = False):
    """
    懒加载并缓存 FunASR AutoModel 实例。
    首次调用会从 ModelScope 下载模型（约 500MB）。

    Args:
        model_name: 模型名称
        device: 设备类型 ("auto", "xpu", "cuda:0", "cpu")
                "auto" 自动检测最佳设备
        speaker_mode: 是否启用说话人分离
    """
    model_name = _normalize_model_name(model_name)

    # 自动检测设备
    if device == "auto":
        device = _detect_best_device()
        logger.info(f"自动选择设备: {device}")

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

    # Intel XPU 需要额外初始化
    if device == "xpu":
        try:
            import intel_extension_for_pytorch as ipex
            logger.info("Intel Extension for PyTorch 已加载")
        except ImportError:
            logger.warning("intel-extension-for-pytorch 未安装，回退到 CPU")
            device = "cpu"

    logger.info(f"加载 FunASR 模型: {model_name} on {device}（首次使用会下载模型）...")
    is_sv = _is_sensevoice_model(model_name)
    model_kwargs = dict(
        model=model_name,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},  # VAD 最长单段 30s
        device=device,
        disable_update=True,  # 不检查更新，加快启动
        hub="ms",             # 从 ModelScope 下载
    )
    if not is_sv:
        # SenseVoice 自带标点，加 ct-punc 反而重复加标点
        model_kwargs["punc_model"] = "ct-punc"
    if speaker_mode:
        model_kwargs["spk_model"] = "cam++"
        logger.info("说话人分离模式：加载 cam++ 说话人模型")
    model = AutoModel(**model_kwargs)
    logger.info(f"FunASR 模型加载完成: {model_name}")
    _model_cache[cache_key] = model
    return model


def _split_by_punctuation(
    text: str, timestamps: list[list[int]], max_chars: int = 15
) -> list[tuple[float, float, str]]:
    """
    将文本拆分为多个字幕条目，优先按句末标点，其次按逗号等停顿标点。
    每句尽量不超过 max_chars 字符。

    timestamps[i] = [start_ms, end_ms] 对应 text[i] 的字符级时间戳。
    """
    SENTENCE_END = set("。！？…!?")
    PAUSE_MARKS = set("，,；;：:、")  # 停顿标点

    if not timestamps or len(timestamps) < len(text):
        # 时间戳不完整，整段作为一个条目
        if timestamps:
            return [(timestamps[0][0] / 1000, timestamps[-1][1] / 1000, text)]
        return []

    segments: list[tuple[float, float, str]] = []
    seg_start = 0

    def add_segment(seg_end: int):
        """添加一个片段到 segments"""
        nonlocal seg_start
        if seg_end <= seg_start:
            return
        sentence = text[seg_start: seg_end + 1].strip()
        if sentence:
            start_s = timestamps[seg_start][0] / 1000
            end_s = timestamps[min(seg_end, len(timestamps) - 1)][1] / 1000
            segments.append((start_s, end_s, sentence))
        seg_start = seg_end + 1

    i = seg_start
    last_pause = -1  # 上一个停顿标点位置

    while i < len(text):
        char = text[i]

        # 句末标点：立即断句
        if char in SENTENCE_END:
            add_segment(i)
            last_pause = -1
        # 停顿标点：记录位置，可能用于断句
        elif char in PAUSE_MARKS:
            last_pause = i
            # 如果已经超过 max_chars，在停顿处断句
            if i - seg_start + 1 >= max_chars:
                add_segment(i)
                last_pause = -1
        # 普通字符：检查长度
        else:
            current_len = i - seg_start + 1
            if current_len >= max_chars:
                # 优先在最近的停顿标点处断句
                if last_pause >= seg_start:
                    add_segment(last_pause)
                    i = last_pause  # 下次循环会 +1
                else:
                    # 没有停顿标点，强制在当前位置断句（避免破坏词语）
                    add_segment(i)
                last_pause = -1

        i += 1

    # 处理剩余文本
    if seg_start < len(text):
        sentence = text[seg_start:].strip()
        if sentence:
            start_s = timestamps[seg_start][0] / 1000
            end_s = timestamps[-1][1] / 1000
            segments.append((start_s, end_s, sentence))

    return segments


def _split_text_without_timestamps(
    text: str, start_time: float, max_chars: int = 12, char_duration: float = 0.15
) -> list[tuple[float, float, str]]:
    """
    当没有时间戳时，智能拆分文本并估算时间戳。

    Args:
        text: 要拆分的文本
        start_time: 起始时间（秒）
        max_chars: 每段最大字符数（默认 12，适合快速阅读）
        char_duration: 每字符平均时长（秒，默认 0.15s 约 6-7 字/秒）

    Returns:
        [(start_s, end_s, text), ...]
    """
    SENTENCE_END = set("。！？…!?")
    PAUSE_MARKS = set("，,；;：:、")

    segments: list[tuple[float, float, str]] = []
    seg_start = 0
    cursor = start_time

    def add_segment(seg_end: int, pause_after: float = 0.0):
        """添加一个片段并估算时间戳"""
        nonlocal seg_start, cursor
        if seg_end <= seg_start:
            return
        sentence = text[seg_start: seg_end + 1].strip()
        if sentence:
            # 根据字符数估算时长，加上停顿时间
            duration = len(sentence) * char_duration + pause_after
            # 限制时长范围
            duration = max(0.8, min(duration, 5.0))
            end_time = cursor + duration
            segments.append((round(cursor, 3), round(end_time, 3), sentence))
            cursor = end_time
        seg_start = seg_end + 1

    i = seg_start
    last_pause = -1

    while i < len(text):
        char = text[i]

        if char in SENTENCE_END:
            # 句末标点：断句，加稍长停顿
            add_segment(i, pause_after=0.3)
            last_pause = -1
        elif char in PAUSE_MARKS:
            last_pause = i
            # 超过 max_chars 时断句
            if i - seg_start + 1 >= max_chars:
                add_segment(i, pause_after=0.15)
                last_pause = -1
        else:
            current_len = i - seg_start + 1
            if current_len >= max_chars:
                if last_pause >= seg_start:
                    add_segment(last_pause, pause_after=0.15)
                    i = last_pause
                else:
                    add_segment(i, pause_after=0.1)
                last_pause = -1

        i += 1

    # 处理剩余文本
    if seg_start < len(text):
        sentence = text[seg_start:].strip()
        if sentence:
            duration = max(0.8, len(sentence) * char_duration)
            segments.append((round(cursor, 3), round(cursor + duration, 3), sentence))

    return segments


def _fix_time_gaps(
    segments: list[tuple[float, float, str]],
    max_gap_seconds: float = 300.0,  # 5 分钟
    avg_char_duration: float = 0.15,
) -> list[tuple[float, float, str]]:
    """
    修复时间跳跃过大的问题。

    当检测到相邻字幕之间有超过 max_gap_seconds 的跳跃时：
    1. 记录警告日志
    2. 尝试重新估算时间戳，使其连续

    Args:
        segments: 原始字幕片段 [(start, end, text), ...]
        max_gap_seconds: 允许的最大时间跳跃（默认 5 分钟）
        avg_char_duration: 平均每字符时长（用于估算）

    Returns:
        修复后的字幕片段
    """
    if len(segments) < 2:
        return segments

    fixed: list[tuple[float, float, str]] = []
    cursor = segments[0][0]  # 从第一条字幕的开始时间开始

    for i, (start, end, text) in enumerate(segments):
        # 检测时间跳跃
        gap = start - cursor

        if gap > max_gap_seconds:
            # 超过允许的最大跳跃，重新估算时间
            logger.warning(
                f"检测到时间跳跃: {gap:.0f}s ({gap/60:.1f}min)，"
                f"从 {cursor:.1f}s 跳到 {start:.1f}s，重新估算时间戳"
            )
            # 使用估算时长
            est_duration = max(1.0, len(text) * avg_char_duration)
            est_duration = min(est_duration, 10.0)  # 限制最大 10 秒
            fixed_start = cursor
            fixed_end = cursor + est_duration
            fixed.append((round(fixed_start, 3), round(fixed_end, 3), text))
            cursor = fixed_end
        else:
            # 正常范围，保留原始时间（但确保连续）
            if start < cursor:
                # 时间倒退，修正为当前游标
                start = cursor
                duration = max(1.0, len(text) * avg_char_duration)
                end = start + duration
            fixed.append((round(start, 3), round(end, 3), text))
            cursor = max(cursor, end)

    return fixed


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
    device: str = "auto",
    progress_cb: Callable[[float, str], None] | None = None,
) -> list[tuple[float, float, str]]:
    """
    使用 FunASR Paraformer 转录音频。

    Args:
        audio_path: WAV 文件路径
        model_name: FunASR 模型名
        language:   "auto" / "zh" / "en" / "yue" / "ja" / "ko" / "es"
        device:     "auto" / "xpu" / "cuda:0" / "cpu"
                    "auto" 自动检测最佳设备（优先级：Intel XPU > CUDA > CPU）
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

    segments: list[tuple[float, float, str]] = []
    speaker_id_map: dict[str, int] = {}
    fallback_cursor = 0.0
    for item in res:
        raw_text = item.get("text", "").strip()
        if not raw_text:
            continue

        # 去掉情感/事件标签（如 <|HAPPY|><|Speech|>）
        # rich_transcription_postprocess 会将 SenseVoice 的 <|HAPPY|> 等转为 emoji
        text = rich_transcription_postprocess(raw_text)
        if not text:
            text = raw_text

        # 二次清理残余标签
        text = re.sub(r"<\|[^|>]+\|>", " ", text)
        # 去掉 SenseVoice 情感 emoji（😊😡🎼 等），字幕只保留文字
        text = re.sub(r"[\U0001F300-\U0001F9FF\U00002600-\U000027BF]", "", text)
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
            # 没有时间戳时，按标点拆分后估算时间
            sub_segs = _split_text_without_timestamps(text, fallback_cursor)
            if sub_segs:
                segments.extend(sub_segs)
                fallback_cursor = sub_segs[-1][1]
                logger.debug(f"FunASR: 无时间戳，智能拆分为 {len(sub_segs)} 段")
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

    # 修复时间跳跃（超过 5 分钟的跳跃）
    segments = _fix_time_gaps(segments, max_gap_seconds=300.0)

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
