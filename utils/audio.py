"""
音频提取工具
使用 ffmpeg 从视频中提取 16kHz 单声道 WAV
"""

import os
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

    cmd = [
        "ffmpeg",
        "-y",                   # 覆盖已有文件
        "-i", input_path,
        "-vn",                  # 去掉视频流
        "-ac", "1",             # 单声道
        "-ar", "16000",         # 16kHz（ASR 模型标准采样率）
        "-acodec", "pcm_s16le", # 16-bit PCM
        "-loglevel", "error",   # 只显示错误
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 提取音频失败:\n{result.stderr}")

    return output_path


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
) -> list[tuple[str, float, float]]:
    """
    将 WAV 音频切分为固定时长分片，返回 [(chunk_path, start_s, end_s), ...]。
    """
    os.makedirs(output_dir, exist_ok=True)
    pattern = str(Path(output_dir) / "chunk_%04d.wav")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        audio_path,
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-c",
        "copy",
        "-reset_timestamps",
        "1",
        "-loglevel",
        "error",
        pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 切分音频失败:\n{result.stderr}")

    chunks = sorted(Path(output_dir).glob("chunk_*.wav"))
    items: list[tuple[str, float, float]] = []
    cursor = 0.0
    for chunk in chunks:
        duration = get_audio_duration(str(chunk))
        start = cursor
        end = start + max(0.0, duration)
        items.append((str(chunk), start, end))
        cursor = end

    return items
