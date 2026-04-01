"""
全局配置常量、语言工具、设备检测

从 main.py 提取，去 Gradio 依赖。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger("video2text")

# ---------------------------------------------------------------------------
# 工作目录
# ---------------------------------------------------------------------------

WORKSPACE_DIR = Path(__file__).resolve().parent.parent / "workspace"
WORKSPACE_DIR.mkdir(exist_ok=True)

TEMP_VIDEO_DIR = WORKSPACE_DIR / "temp_video"
TEMP_VIDEO_DIR.mkdir(exist_ok=True)


def _get_temp_video_keep_count() -> int:
    try:
        return int(os.environ.get("TEMP_VIDEO_KEEP_COUNT", "5"))
    except (ValueError, TypeError):
        return 5


TEMP_VIDEO_KEEP_COUNT = _get_temp_video_keep_count()

# ---------------------------------------------------------------------------
# 正在转录的视频路径（用于清理临时文件时跳过）
# ---------------------------------------------------------------------------

_TRANSCRIBING_VIDEO: str | None = None
_TRANSCRIBING_VIDEO_LOCK = threading.Lock()


def set_transcribing_video(path: str | None):
    global _TRANSCRIBING_VIDEO
    with _TRANSCRIBING_VIDEO_LOCK:
        _TRANSCRIBING_VIDEO = path


def get_transcribing_video() -> str | None:
    with _TRANSCRIBING_VIDEO_LOCK:
        return _TRANSCRIBING_VIDEO


# ---------------------------------------------------------------------------
# 停止事件
# ---------------------------------------------------------------------------

STOP_EVENT = threading.Event()

# ---------------------------------------------------------------------------
# 支持的媒体格式
# ---------------------------------------------------------------------------

SUPPORTED_EXTS = [
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg",
]

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"
}

_ALL_MEDIA_EXTS = {ext.lower() for ext in SUPPORTED_EXTS}
_AUDIO_EXTS = _ALL_MEDIA_EXTS - VIDEO_EXTS
_SOURCE_MEDIA_EXTS = {ext for ext in _ALL_MEDIA_EXTS if ext != ".wav"}


def _is_supported_media_path(path_like: str | Path) -> bool:
    return Path(path_like).suffix.lower() in _ALL_MEDIA_EXTS


def _safe_media_name(filename: str) -> str:
    raw_name = Path(filename or "media").name
    stem = re.sub(r'[/\\\x00]', '', Path(raw_name).stem).strip()[:120] or "media"
    suffix = Path(raw_name).suffix[:20]
    return f"{stem}{suffix}"


# ---------------------------------------------------------------------------
# 语言工具
# ---------------------------------------------------------------------------

def _parse_lang_code(choice: str) -> str:
    """从 'zh（普通话）' 形式的选项中提取语言代码 'zh'。"""
    if choice == "自动检测":
        return "auto"
    return choice.split("（")[0].split("(")[0].strip()


def _looks_non_chinese_text(text: str) -> bool:
    """基于字符分布的轻量规则，判断文本是否主要为非中文。"""
    normalized = re.sub(r"\s+", "", text)
    if not normalized:
        return False

    zh_chars = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    ja_chars = len(re.findall(r"[\u3040-\u30ff]", normalized))
    ko_chars = len(re.findall(r"[\uac00-\ud7af]", normalized))
    latin_chars = len(re.findall(r"[A-Za-z]", normalized))

    total = len(normalized)
    zh_ratio = zh_chars / total

    if zh_ratio < 0.25 and (ja_chars + ko_chars + latin_chars) >= 12:
        return True
    return False


def _guess_source_lang(lang_code: str, plain_text: str) -> str:
    """推断翻译源语言，供模型选择使用。"""
    if lang_code != "auto":
        return lang_code

    if re.search(r"[\u3040-\u30ff]", plain_text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", plain_text):
        return "ko"

    lower_text = plain_text.lower()
    es_markers = [" que ", " de ", " la ", " el ", " y ", " por ", " para "]
    if any(token in lower_text for token in es_markers):
        return "es"

    return "en"


# ---------------------------------------------------------------------------
# FunASR 模型选择
# ---------------------------------------------------------------------------

def _is_funasr_multilingual_model(model_name: str) -> bool:
    normalized = model_name.split(" ")[0].strip().lower()
    multilingual_markers = ["sensevoice", "zh-en", "seaco"]
    return any(marker in normalized for marker in multilingual_markers)


def _pick_funasr_model_for_language(
    backend: str,
    lang_code: str,
    selected_model: str,
    log_cb: Callable[[str], None] | None,
) -> str:
    """
    当用户选择 FunASR 时，根据语言自动选择更合适的 FunASR 模型。
    多语言场景优先使用 SenseVoiceSmall，避免切换到其他后端。
    """
    if backend != "FunASR（Paraformer）":
        return selected_model

    model = selected_model
    multilingual_best = "iic/SenseVoiceSmall"

    non_zh_langs = {"en", "ja", "ko", "es"}
    if lang_code in non_zh_langs:
        model_key = model.split(" ")[0].strip().lower()
        if model_key.split("/")[-1].split(" ")[0] != "sensevoicesmall" and not any(k in model_key for k in ("-spk", "speaker")):
            if log_cb:
                log_cb(
                    f"[MODEL-AUTO] 检测到 {lang_code} 语言，FunASR 自动切换为 {multilingual_best}"
                )
            model = multilingual_best
        return model

    model_key = model.split(" ")[0].strip().lower()
    if any(k in model_key for k in ("-spk", "speaker")):
        return model

    if lang_code == "auto" and not _is_funasr_multilingual_model(model):
        if log_cb:
            log_cb(
                f"[MODEL-AUTO] 自动检测语言场景，FunASR 自动切换为 {multilingual_best}"
            )
        model = multilingual_best

    return model


# ---------------------------------------------------------------------------
# GPU 检测
# ---------------------------------------------------------------------------

def _has_nvidia_gpu() -> bool:
    """检测 NVIDIA GPU 是否可用（兼容 WSL2）。"""
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    if visible in {"-1", "none", "None"}:
        logger.info(f"[GPU] CUDA_VISIBLE_DEVICES={visible!r}，跳过 GPU 检测")
        return False

    try:
        import torch
        avail = torch.cuda.is_available()
        if avail:
            device_name = torch.cuda.get_device_name(0) if torch.cuda.device_count() > 0 else "unknown"
            logger.info(f"[GPU] torch.cuda 检测到 GPU: {device_name}")
            return True
        else:
            logger.info("[GPU] torch.cuda.is_available()=False")
    except ImportError:
        logger.info("[GPU] torch 未安装，跳过 torch.cuda 检测")

    if os.path.exists("/dev/nvidiactl") or os.path.exists("/proc/driver/nvidia"):
        logger.info("[GPU] 检测到 /dev/nvidiactl 或 /proc/driver/nvidia")
        return True
    else:
        logger.info("[GPU] /dev/nvidiactl 和 /proc/driver/nvidia 均不存在")

    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if result.returncode == 0:
            gpu_list = result.stdout.decode(errors="replace").strip()
            logger.info(f"[GPU] nvidia-smi 检测到 GPU: {gpu_list.splitlines()[0] if gpu_list else 'unknown'}")
            return True
        else:
            logger.info(f"[GPU] nvidia-smi 返回非零: {result.returncode}")
    except FileNotFoundError:
        logger.info("[GPU] nvidia-smi 未找到")
    except Exception as e:
        logger.info(f"[GPU] nvidia-smi 检测异常: {e}")

    logger.info("[GPU] 所有检测均未发现 NVIDIA GPU，将使用 CPU")
    return False
