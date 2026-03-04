"""
音频提取工具
使用 ffmpeg 从视频中提取 16kHz 单声道 WAV
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path


def extract_audio(input_path: str, output_path: str = None) -> str:
    """
    从视频/音频文件提取 16kHz 单声道 WAV。

    Args:
        input_path: 输入文件路径（视频或音频）
        output_path: 输出 WAV 路径，None 则自动创建临时文件

    Returns:
        输出 WAV 文件路径
    """
    if output_path is None:
        suffix = Path(input_path).stem
        tmp = tempfile.NamedTemporaryFile(
            suffix=".wav", prefix=f"v2t_{suffix}_", delete=False
        )
        output_path = tmp.name
        tmp.close()

    base_args = [
        "-y",                   # 覆盖已有文件
        "-nostdin",             # 后台运行时禁止从终端读取输入，避免被 shell 挂起
        "-i", input_path,
        "-vn",                  # 去掉视频流
        "-ac", "1",            # 单声道
        "-ar", "16000",        # 16kHz（ASR 模型标准采样率）
        "-acodec", "pcm_s16le",# 16-bit PCM
        "-loglevel", "error",  # 只显示错误
        output_path,
    ]

    # 优先尝试 NVIDIA 硬件解码路径（CUDA/NVDEC），失败自动回退 CPU。
    cmd_candidates = [
        ["ffmpeg", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", *base_args],
        ["ffmpeg", *base_args],
    ]

    last_err = ""
    for cmd in cmd_candidates:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return output_path
        last_err = result.stderr or result.stdout or "unknown error"

    raise RuntimeError(f"ffmpeg 提取音频失败:\n{last_err}")


def get_audio_duration(audio_path: str) -> float:
    """获取音频时长（秒）"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def cleanup(path: str):
    """删除临时文件（忽略错误）"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def split_audio_chunks(
    audio_path: str,
    output_dir: str,
    chunk_seconds: int = 60,
    overlap_seconds: int = 0,
) -> list[tuple[str, float, float]]:
    """
    将 WAV 音频切分为固定时长分片，返回 [(chunk_path, start_s, end_s), ...]。
    """
    os.makedirs(output_dir, exist_ok=True)
    total_duration = get_audio_duration(audio_path)
    if total_duration <= 0:
        return []

    step_seconds = max(1, chunk_seconds - max(0, overlap_seconds))
    starts: list[float] = []
    cursor = 0.0
    while cursor < total_duration:
        starts.append(cursor)
        cursor += step_seconds

    chunks: list[Path] = []
    for idx, start in enumerate(starts, start=1):
        out_path = Path(output_dir) / f"chunk_{idx:04d}.wav"
        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",  # 后台运行时禁止从终端读取输入，避免被 shell 挂起
            "-ss",
            f"{start:.3f}",
            "-i",
            audio_path,
            "-t",
            str(chunk_seconds),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            "-loglevel",
            "error",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 切分音频失败:\n{result.stderr}")
        if out_path.exists() and out_path.stat().st_size > 0:
            chunks.append(out_path)

    items: list[tuple[str, float, float]] = []
    for chunk in chunks:
        duration = get_audio_duration(str(chunk))
        m = re.search(r"chunk_(\d+)\.wav$", chunk.name)
        if m:
            idx = int(m.group(1)) - 1
        else:
            idx = len(items)
        start = idx * step_seconds
        end = start + max(0.0, duration)
        items.append((str(chunk), start, end))

    return items
