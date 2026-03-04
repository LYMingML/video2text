"""
字幕格式化工具
支持输出 SRT 文件和纯文本
"""

import tempfile
import os
from pathlib import Path
import re


NOISE_PATTERNS = [
    re.compile(r"\b\d{1,3}\.\d+%\b", re.IGNORECASE),
    re.compile(r"\b\d+\/\d+\s+steps\b", re.IGNORECASE),
    re.compile(r"\bsteps\b", re.IGNORECASE),
    re.compile(r"\|"),
    re.compile(r"加载模型", re.IGNORECASE),
    re.compile(r"modelscope", re.IGNORECASE),
]


def _is_noise_text(text: str) -> bool:
    """判断是否为非字幕噪声文本（进度条/日志等）。"""
    stripped = text.strip()
    if not stripped:
        return True

    hit_count = sum(1 for p in NOISE_PATTERNS if p.search(stripped))
    if hit_count >= 2:
        return True

    if stripped.count("|") >= 2 and re.search(r"\d", stripped):
        return True

    return False


def _normalize_plain_line(text: str) -> str:
    """纯文本行归一化：去噪、折叠空白。"""
    line = re.sub(r"\s+", " ", text).strip()
    if _is_noise_text(line):
        return ""
    return line


def collect_plain_text(segments: list[tuple]) -> str:
    """直接串联 ASR 结果文本，不调整时间。"""
    lines: list[str] = []
    for _, _, text in segments:
        line = _normalize_plain_line(text)
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def normalize_segments_timeline(
    segments: list[tuple],
    min_duration_s: float = 0.8,
    max_duration_s: float = 12.0,
) -> list[tuple[float, float, str]]:
    """
    清洗并归一化字幕时间轴：
    - 过滤噪声文本
    - 保证时间单调递增
    - 修正异常区间（end <= start）
    - 限制过短/过长片段时长
    """
    cleaned: list[tuple[float, float, str]] = []
    for start, end, text in segments:
        line = _normalize_plain_line(text)
        if not line:
            continue
        try:
            s = float(start)
            e = float(end)
        except Exception:
            continue
        if e <= s:
            e = s + min_duration_s
        cleaned.append((s, e, line))

    if not cleaned:
        return []

    cleaned.sort(key=lambda x: (x[0], x[1]))

    normalized: list[tuple[float, float, str]] = []
    cursor = max(0.0, cleaned[0][0])

    for start, end, text in cleaned:
        start = max(start, cursor)
        duration = end - start

        if duration < min_duration_s:
            end = start + min_duration_s
        elif duration > max_duration_s:
            end = start + max_duration_s

        normalized.append((round(start, 3), round(end, 3), text))
        cursor = end

    return normalized


def ms_to_srt_time(ms: float) -> str:
    """毫秒 → SRT 时间格式 HH:MM:SS,mmm"""
    ms = int(ms)
    h = ms // 3_600_000
    m = (ms % 3_600_000) // 60_000
    s = (ms % 60_000) // 1_000
    millis = ms % 1_000
    return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"


def s_to_srt_time(seconds: float) -> str:
    """秒 → SRT 时间格式 HH:MM:SS,mmm"""
    return ms_to_srt_time(seconds * 1000)


def segments_to_srt(segments: list[tuple], normalize: bool = True) -> str:
    """
    将 segments 转为 SRT 字符串。

    Args:
        segments: [(start_s, end_s, text), ...] start/end 单位为秒

    Returns:
        SRT 格式字符串
    """
    segments = normalize_segments_timeline(segments) if normalize else segments
    lines = []
    index = 1
    for start, end, text in segments:
        text = text.strip()
        if not text:
            continue
        start_t = s_to_srt_time(start)
        end_t = s_to_srt_time(end)
        lines.append(f"{index}\n{start_t} --> {end_t}\n{text}")
        index += 1
    return "\n\n".join(lines) + "\n"


def segments_to_plain(segments: list[tuple], normalize: bool = True) -> str:
    """
    将 segments 转为纯文本（去掉时间轴）。

    Args:
        segments: [(start_s, end_s, text), ...]

    Returns:
        纯文本字符串，每句一行
    """
    segments = normalize_segments_timeline(segments) if normalize else segments
    lines = [text for _, _, text in segments]
    return "\n".join(lines) + "\n"


def save_srt(
    segments: list[tuple], output_path: str = None, normalize: bool = True
) -> str:
    """保存 SRT 文件，返回文件路径"""
    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".srt", prefix="v2t_", delete=False, mode="w", encoding="utf-8"
        )
        output_path = tmp.name
        tmp.write(segments_to_srt(segments, normalize=normalize))
        tmp.close()
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(segments_to_srt(segments, normalize=normalize))
    return output_path


def save_plain(
    segments: list[tuple], output_path: str = None, normalize: bool = True
) -> str:
    """保存纯文本文件，返回文件路径"""
    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="v2t_", delete=False, mode="w", encoding="utf-8"
        )
        output_path = tmp.name
        tmp.write(segments_to_plain(segments, normalize=normalize))
        tmp.close()
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(segments_to_plain(segments, normalize=normalize))
    return output_path
