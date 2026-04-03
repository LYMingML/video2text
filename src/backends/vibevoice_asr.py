"""
VibeVoice ASR 后端
基于微软 VibeVoice-ASR 模型，使用 HuggingFace Transformers 推理

模型选择:
  - VibeVoice-ASR-7B (bezzam/VibeVoice-ASR-7B): 7B 参数，低显存友好
  - VibeVoice-ASR-9B (microsoft/VibeVoice-ASR-HF): 9B 参数，官方模型

量化支持:
  - 4-bit (默认): ~5GB VRAM，适合 8GB 显存 GPU
  - 8-bit: ~8GB VRAM，适合 12GB+ 显存 GPU
  - 无量化: ~14GB VRAM

特性:
  - 单次推理最长 60 分钟音频（64K token 上下文窗口）
  - 内置说话人分离（Speaker Diarization）
  - 50+ 语言支持，含语码转换
  - 24kHz 采样率
  - 输出结构化 JSON：{Start, End, Speaker, Content}

依赖:
  pip install transformers>=5.3.0 accelerate torch bitsandbytes

环境变量:
  VIBEVOICE_QUANT_BITS: 量化位数 (4=默认, 8, 0=不量化)
  VIBEVOICE_MODEL: 默认模型名称
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Callable

from backends import register_asr
from backends.base_asr import ASRBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模型配置
# ---------------------------------------------------------------------------

MODEL_ALIASES: dict[str, str] = {
    "VibeVoice-ASR-7B": "bezzam/VibeVoice-ASR-7B",
    "VibeVoice-ASR-9B": "microsoft/VibeVoice-ASR-HF",
    "VibeVoice-ASR-HF": "microsoft/VibeVoice-ASR-HF",
    "bezzam/VibeVoice-ASR-7B": "bezzam/VibeVoice-ASR-7B",
    "microsoft/VibeVoice-ASR-HF": "microsoft/VibeVoice-ASR-HF",
    "microsoft/VibeVoice-ASR": "microsoft/VibeVoice-ASR-HF",
}


def _get_default_model() -> str:
    """从环境变量读取默认模型"""
    env_val = os.environ.get("VIBEVOICE_MODEL", "").strip()
    if env_val and env_val in MODEL_ALIASES:
        return MODEL_ALIASES[env_val]
    return "bezzam/VibeVoice-ASR-7B"


def _get_default_quant_bits() -> int:
    """从环境变量读取默认量化位数"""
    try:
        val = int(os.environ.get("VIBEVOICE_QUANT_BITS", "4"))
        return val if val in (0, 4, 8) else 4
    except (ValueError, TypeError):
        return 4


DEFAULT_MODEL = _get_default_model()
DEFAULT_QUANT_BITS = _get_default_quant_bits()

# 单例模型缓存
_model_instance: object | None = None
_processor_instance: object | None = None
_model_device: str = ""
_current_model_id: str = ""
_current_quant_bits: int = 0
_model_cache_lock = threading.Lock()


def _parse_model_and_quant(model_name: str) -> tuple[str, int]:
    """
    从 model_name 中解析模型 ID 和量化位数。

    支持格式:
      - "bezzam/VibeVoice-ASR-7B::4" → ("bezzam/VibeVoice-ASR-7B", 4)
      - "bezzam/VibeVoice-ASR-7B::8" → ("bezzam/VibeVoice-ASR-7B", 8)
      - "bezzam/VibeVoice-ASR-7B"     → ("bezzam/VibeVoice-ASR-7B", DEFAULT_QUANT_BITS)
      - "VibeVoice-ASR-7B::4"         → ("bezzam/VibeVoice-ASR-7B", 4)
    """
    raw = (model_name or "").strip()
    quant = DEFAULT_QUANT_BITS

    if "::" in raw:
        parts = raw.rsplit("::", 1)
        raw = parts[0].strip()
        try:
            q = int(parts[1].strip())
            if q in (0, 4, 8):
                quant = q
        except (ValueError, TypeError):
            pass

    model_id = _resolve_model_id(raw)
    return model_id or DEFAULT_MODEL, quant


def _resolve_model_id(model_name: str) -> str | None:
    """将用户输入的模型名称解析为 HuggingFace 模型 ID"""
    if not model_name or not model_name.strip():
        return DEFAULT_MODEL
    name = model_name.strip()
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    # 如果已经是完整的 HuggingFace ID (包含 /)，直接返回
    if "/" in name:
        return name
    return None


def _detect_device() -> str:
    """检测最佳计算设备"""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda:0"
    except ImportError:
        pass
    return "cpu"


def _get_model_and_processor(
    device: str = "auto",
    model_name: str = "",
    quant_bits: int = 0,
):
    """
    懒加载并缓存 VibeVoice 模型和处理器。

    Args:
        device: 计算设备
        model_name: 模型名称/别名
        quant_bits: 量化位数 (0=不量化, 4=INT4, 8=INT8)
    """
    global _model_instance, _processor_instance, _model_device
    global _current_model_id, _current_quant_bits

    with _model_cache_lock:
        actual_device = device
        if actual_device == "auto":
            actual_device = _detect_device()

        # 检查是否可用缓存（模型+量化+设备都匹配）
        if (_model_instance is not None
                and _model_device == actual_device
                and _current_model_id == model_name
                and _current_quant_bits == quant_bits):
            return _processor_instance, _model_instance

        try:
            from transformers import AutoProcessor, VibeVoiceAsrForConditionalGeneration
        except ImportError:
            raise ImportError(
                "transformers >= 5.3.0 未安装，请运行：\n"
                "  pip install 'transformers>=5.3.0' accelerate torch"
            )

        # 解析模型ID
        resolved_model_id = _resolve_model_id(model_name)
        if not resolved_model_id:
            resolved_model_id = DEFAULT_MODEL

        quant_label = f"{quant_bits}-bit" if quant_bits else "full"
        logger.info(f"加载 VibeVoice 模型: {resolved_model_id} on {actual_device} ({quant_label})")
        logger.info("（首次使用会下载模型，约 2-4GB）")

        processor = AutoProcessor.from_pretrained(resolved_model_id, trust_remote_code=True)

        try:
            import torch

            # 构建加载参数
            load_kwargs = {"trust_remote_code": True}

            if quant_bits in (4, 8) and "cuda" in actual_device:
                try:
                    from transformers import BitsAndBytesConfig
                except ImportError:
                    raise ImportError(
                        "量化需要 bitsandbytes，请运行：\n"
                        "  pip install bitsandbytes"
                    )

                quant_config_kwargs = {
                    "load_in_4bit": quant_bits == 4,
                    "load_in_8bit": quant_bits == 8,
                    "bnb_4bit_compute_dtype": torch.float16,
                }
                if quant_bits == 4:
                    quant_config_kwargs["bnb_4bit_quant_type"] = "nf4"
                load_kwargs["quantization_config"] = BitsAndBytesConfig(**quant_config_kwargs)
                load_kwargs["device_map"] = actual_device
                logger.info(f"使用 {quant_bits}-bit 量化加载模型")
            elif "cuda" in actual_device:
                load_kwargs["torch_dtype"] = torch.float16
                load_kwargs["device_map"] = actual_device
            else:
                load_kwargs["torch_dtype"] = torch.float32
                load_kwargs["device_map"] = "cpu"
                actual_device = "cpu"

            model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
                resolved_model_id,
                **load_kwargs,
            )
        except Exception as e:
            logger.warning(f"模型加载失败 ({e})，回退 CPU float32")
            model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
                resolved_model_id,
                device_map="cpu",
                torch_dtype=torch.float32,
                trust_remote_code=True,
            )
            actual_device = "cpu"

        _model_instance = model
        _processor_instance = processor
        _model_device = actual_device
        _current_model_id = model_name
        _current_quant_bits = quant_bits

        logger.info(f"VibeVoice 模型加载完成: device={actual_device}, quant={quant_label}")
        return processor, model


def _chunk_audio(
    audio_path: str,
    chunk_seconds: int = 3600,
    overlap_seconds: int = 600,
    sample_rate: int = 24000,
) -> list[tuple[str, float, float]]:
    """
    将长音频切分为带重叠的片段。

    Args:
        audio_path: 输入音频文件路径
        chunk_seconds: 每段时长（秒），默认 60 分钟
        overlap_seconds: 重叠时长（秒），默认 10 分钟
        sample_rate: 采样率

    Returns:
        [(temp_path, start_seconds, end_seconds), ...]
    """
    import subprocess

    # 获取音频时长
    probe_cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", audio_path,
    ]
    result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return [(audio_path, 0.0, -1.0)]

    try:
        info = json.loads(result.stdout)
        duration = float(info["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return [(audio_path, 0.0, -1.0)]

    # 不需要切分
    if duration <= chunk_seconds:
        return [(audio_path, 0.0, duration)]

    logger.info(
        f"音频时长 {duration:.0f}s ({duration/60:.1f}min)，"
        f"按 {chunk_seconds}s ({chunk_seconds/60:.0f}min) 切片，"
        f"重叠 {overlap_seconds}s ({overlap_seconds/60:.0f}min)"
    )

    chunks: list[tuple[str, float, float]] = []
    temp_dir = tempfile.mkdtemp(prefix="vibevoice_chunk_")

    idx = 0
    start = 0.0
    while start < duration:
        end = min(start + chunk_seconds, duration)
        chunk_path = str(Path(temp_dir) / f"chunk_{idx:04d}.wav")

        cmd = [
            "ffmpeg", "-y", "-v", "quiet",
            "-i", audio_path,
            "-ss", str(start),
            "-t", str(end - start),
            "-ar", str(sample_rate),
            "-ac", "1",
            "-f", "wav",
            chunk_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and Path(chunk_path).exists():
            chunks.append((chunk_path, start, end))
            idx += 1
        else:
            logger.warning(f"切片失败: start={start}, end={end}: {r.stderr[:200]}")

        # 下一个切片起点（减去重叠）
        start = end - overlap_seconds
        if start >= duration:
            break
        # 避免最后一段太短（<30秒）
        if duration - start < 30:
            break

    logger.info(f"切分为 {len(chunks)} 个片段")
    return chunks


def _merge_segments(
    all_segments: list[list[tuple[float, float, str]]],
    chunk_starts: list[float],
    overlap_seconds: float = 600.0,
) -> list[tuple[float, float, str]]:
    """
    合并多个切片的转录结果，去除重叠区域的重复部分。

    策略：每个切片丢弃前 overlap_seconds 区域的转录结果，
    因为它们与上一个切片的尾部重叠。
    """
    if not all_segments:
        return []

    merged: list[tuple[float, float, str]] = []

    for chunk_idx, segments in enumerate(all_segments):
        if not segments:
            continue

        offset = chunk_starts[chunk_idx] if chunk_idx < len(chunk_starts) else 0.0

        for seg_idx, (start, end, text) in enumerate(segments):
            # 偏移到全局时间
            abs_start = start + offset
            abs_end = end + offset

            # 非第一个切片：丢弃重叠区域
            if chunk_idx > 0:
                overlap_boundary = offset + overlap_seconds
                if abs_start < overlap_boundary - 1.0:
                    # 跳过重叠区域内的段落（留1秒容差）
                    continue

            merged.append((round(abs_start, 3), round(abs_end, 3), text))

    # 按时间排序
    merged.sort(key=lambda x: x[0])

    # 去除时间上完全重叠的段落
    deduplicated: list[tuple[float, float, str]] = []
    for seg in merged:
        if not deduplicated:
            deduplicated.append(seg)
            continue
        prev = deduplicated[-1]
        # 如果新段落开始时间在上一个段落结束之前，跳过
        if seg[0] < prev[1] - 0.5:
            continue
        deduplicated.append(seg)

    return deduplicated


@register_asr
class VibeVoiceASR(ASRBackend):
    """VibeVoice ASR 后端（微软开源，60 分钟长音频+说话人分离）"""

    @property
    def name(self) -> str:
        return "VibeVoice ASR"

    @property
    def description(self) -> str:
        return "微软开源语音识别，60分钟长音频、内置说话人分离、50+语言"

    @property
    def default_model(self) -> str:
        return DEFAULT_MODEL

    @property
    def supported_models(self) -> list[str]:
        return [
            "bezzam/VibeVoice-ASR-7B",
            "microsoft/VibeVoice-ASR-HF",
        ]

    @property
    def default_chunk_seconds(self) -> int:
        return 3600  # 60 分钟

    @property
    def default_overlap_seconds(self) -> int:
        return 600  # 10 分钟

    @property
    def sample_rate(self) -> int:
        return 24000

    def transcribe(
        self,
        audio_path: str,
        model_name: str = "",
        language: str = "auto",
        device: str = "auto",
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> list[tuple[float, float, str]]:
        model_name = model_name or self.default_model

        if progress_cb:
            progress_cb(0.0, f"加载 VibeVoice 模型...")

        resolved_model, quant_bits = _parse_model_and_quant(model_name)
        processor, model = _get_model_and_processor(
            device, model_name=resolved_model, quant_bits=quant_bits,
        )

        if progress_cb:
            progress_cb(0.05, "切分音频...")

        # 切分音频（>60 分钟的音频需要切片）
        chunks = _chunk_audio(
            audio_path,
            chunk_seconds=self.default_chunk_seconds,
            overlap_seconds=self.default_overlap_seconds,
            sample_rate=self.sample_rate,
        )

        total_chunks = len(chunks)
        all_segments: list[list[tuple[float, float, str]]] = []
        chunk_starts: list[float] = []

        for idx, (chunk_path, start_s, end_s) in enumerate(chunks):
            chunk_label = f"片段 {idx + 1}/{total_chunks}"
            if progress_cb:
                pct = 0.1 + 0.8 * (idx / max(total_chunks, 1))
                progress_cb(pct, f"VibeVoice 识别中: {chunk_label}...")

            logger.info(f"转录 {chunk_label}: {start_s:.0f}s - {end_s:.0f}s")

            # 准备输入
            try:
                import torch
                inputs = processor.apply_transcription_request(
                    audio=chunk_path,
                    language=language if language != "auto" else None,
                ).to(model.device, model.dtype if hasattr(model, "dtype") else torch.float32)
            except Exception as e:
                logger.error(f"输入准备失败: {e}")
                continue

            # 推理
            try:
                # 设置 acoustic_tokenizer_chunk_size 避免长音频 OOM
                gen_kwargs = {}
                if "cuda" in str(getattr(model, "device", "")):
                    gen_kwargs["acoustic_tokenizer_chunk_size"] = 1440000  # 60s

                output_ids = model.generate(**inputs, **gen_kwargs)
                generated_ids = output_ids[:, inputs["input_ids"].shape[1]:]
            except RuntimeError as e:
                if "out of memory" in str(e).lower():
                    logger.warning("GPU OOM，尝试减小 chunk_size 重试...")
                    try:
                        import torch
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                    gen_kwargs["acoustic_tokenizer_chunk_size"] = 720000  # 30s
                    try:
                        output_ids = model.generate(**inputs, **gen_kwargs)
                        generated_ids = output_ids[:, inputs["input_ids"].shape[1]:]
                    except Exception as e2:
                        logger.error(f"重试也失败: {e2}")
                        continue
                else:
                    logger.error(f"推理失败: {e}")
                    continue

            # 解析输出
            try:
                parsed = processor.decode(generated_ids, return_format="parsed")
                if isinstance(parsed, list) and parsed:
                    parsed = parsed[0]

                chunk_segs: list[tuple[float, float, str]] = []
                if isinstance(parsed, list):
                    for entry in parsed:
                        if not isinstance(entry, dict):
                            continue
                        content = str(entry.get("Content", "")).strip()
                        if not content:
                            continue

                        start = float(entry.get("Start", 0))
                        end = float(entry.get("End", 0))
                        speaker = entry.get("Speaker", "")

                        if speaker and str(speaker) != "":
                            text = f"角色{int(speaker) + 1}: {content}"
                        else:
                            text = content

                        chunk_segs.append((round(start, 3), round(end, 3), text))
                elif isinstance(parsed, str):
                    text = parsed.strip()
                    if text:
                        chunk_segs.append((0.0, end_s - start_s, text))

                all_segments.append(chunk_segs)
                chunk_starts.append(start_s)
                logger.info(f"{chunk_label}: {len(chunk_segs)} 条字幕")

            except Exception as e:
                logger.error(f"输出解析失败: {e}")
                try:
                    raw_text = processor.decode(generated_ids[0], skip_special_tokens=True)
                    if raw_text.strip():
                        all_segments.append([(0.0, end_s - start_s, raw_text.strip())])
                        chunk_starts.append(start_s)
                except Exception:
                    pass

        if progress_cb:
            progress_cb(0.95, "合并结果...")

        # 合并切片结果
        if total_chunks <= 1:
            segments = all_segments[0] if all_segments else []
        else:
            segments = _merge_segments(
                all_segments, chunk_starts,
                overlap_seconds=float(self.default_overlap_seconds),
            )

        # 清理临时切片文件
        if total_chunks > 1:
            try:
                import shutil
                temp_dir = Path(chunks[0][0]).parent
                if temp_dir.name.startswith("vibevoice_chunk_"):
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

        if progress_cb:
            progress_cb(1.0, f"完成，共 {len(segments)} 条字幕")

        return segments

    def unload(self) -> None:
        global _model_instance, _processor_instance, _model_device
        global _current_model_id, _current_quant_bits
        with _model_cache_lock:
            if _model_instance is not None:
                del _model_instance
                _model_instance = None
            if _processor_instance is not None:
                del _processor_instance
                _processor_instance = None
            _model_device = ""
            _current_model_id = ""
            _current_quant_bits = 0
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
