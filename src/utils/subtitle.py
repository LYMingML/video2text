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


TAG_PATTERNS = [
    re.compile(r"<\|[^|>]+\|>", re.IGNORECASE),  # e.g. <|Speech|>
    re.compile(r"<[^>]+>", re.IGNORECASE),        # e.g. <speech>
]

# Whisper 幻觉/水印清理模式
WHISPER_HALLUCINATION_PATTERNS = [
    re.compile(r"\[cite:\s*\d+\]", re.IGNORECASE),       # [cite: 35]
    re.compile(r"\[citation:\s*\d+\]", re.IGNORECASE),   # [citation: 35]
    re.compile(r"\[\d+\]", re.IGNORECASE),               # [1], [35] - 纯数字引用
    re.compile(r"\(cite:\s*\d+\)", re.IGNORECASE),       # (cite: 35)
    re.compile(r"subtitle\s*by\s*.*$", re.IGNORECASE),   # subtitle by xxx
    re.compile(r"www\.\w+\.(com|net|org|cn)", re.IGNORECASE),  # 网址水印
    re.compile(r"^\s*-\s*$"),                            # 单独的短横线
]


def _dedupe_punctuation(text: str) -> str:
    """清理冗余标点与异常组合，保留自然停顿。"""
    line = text
    # 连续重复标点压缩（!!! -> !，。。。 -> 。）
    line = re.sub(r"([。！？!?，,；;：:、…])\1+", r"\1", line)
    # 统一中英文混合省略号
    line = re.sub(r"(\.{3,}|…{2,})", "…", line)
    # 移除明显冗余的混合组合标点
    line = re.sub(r"[，,]{2,}", "，", line)
    line = re.sub(r"[。\.]{2,}", "。", line)
    line = re.sub(r"[!?！？]{2,}", "！", line)
    # 去除角色前缀与正文之间多余空白
    line = re.sub(r"^(角色\d+\s*:\s*)", lambda m: m.group(1).replace(" ", ""), line)
    return line.strip()


def _wrap_chinese_text(text: str, max_chars: int = 25) -> str:
    """中文文本按字符数换行，每行最多 max_chars 个字符。"""
    if not text:
        return text
    # 移除现有换行，重新按字符数换行
    text = text.replace("\n", " ")
    lines = []
    current_line = ""
    for char in text:
        current_line += char
        if len(current_line) >= max_chars:
            lines.append(current_line)
            current_line = ""
    if current_line:
        lines.append(current_line)
    return "\n".join(lines)


def _wrap_english_text(text: str, max_words: int = 20) -> str:
    """英文/外文本按单词数换行，每行最多 max_words 个单词。"""
    if not text:
        return text
    # 移除现有换行，重新按单词数换行
    text = text.replace("\n", " ")
    words = text.split()
    lines = []
    current_line_words = []
    for word in words:
        current_line_words.append(word)
        if len(current_line_words) >= max_words:
            lines.append(" ".join(current_line_words))
            current_line_words = []
    if current_line_words:
        lines.append(" ".join(current_line_words))
    return "\n".join(lines)


def _is_chinese_text(text: str) -> bool:
    """判断文本是否主要为中文（中文字符占比超过50%）。"""
    if not text:
        return False
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return chinese_chars / len(text) > 0.5


def wrap_text(text: str, is_translated: bool = False) -> str:
    """
    根据文本类型进行换行处理。
    - 翻译后的文本（中文）：每25个字符换行
    - 外文原文：每20个词换行
    """
    if not text:
        return text
    if is_translated or _is_chinese_text(text):
        return _wrap_chinese_text(text, max_chars=25)
    else:
        return _wrap_english_text(text, max_words=20)


def wrap_segments_text(
    segments: list[tuple], is_translated: bool = False
) -> list[tuple]:
    """
    为所有字幕片段的文本应用换行处理。

    Args:
        segments: [(start_s, end_s, text), ...]
        is_translated: 是否为翻译后的文本

    Returns:
        处理后的 segments
    """
    wrapped = []
    for start, end, text in segments:
        wrapped_text = wrap_text(text, is_translated=is_translated)
        wrapped.append((start, end, wrapped_text))
    return wrapped


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
    line = text
    # 清理 Whisper 幻觉/水印
    for pattern in WHISPER_HALLUCINATION_PATTERNS:
        line = pattern.sub("", line)
    # 清理标签
    for pattern in TAG_PATTERNS:
        line = pattern.sub(" ", line)
    line = re.sub(r"\s+", " ", line).strip()
    line = _dedupe_punctuation(line)
    if _is_noise_text(line):
        return ""
    return line


def _merge_lines_into_paragraphs(
    items: list[tuple[float, float, str]],
    gap_threshold: float = 2.0,
) -> str:
    """
    将逐句文本合并为段落。

    分段规则：
    - 两条之间时间间隔 > gap_threshold 秒 → 分段
    - 累积超过一定长度（200字符）且当前句以句末标点结尾 → 分段

    Args:
        items: [(start, end, text), ...] 或 [(0, 0, text), ...]
        gap_threshold: 时间间隔阈值（秒）

    Returns:
        段落文本，段间用空行分隔
    """
    if not items:
        return ""

    _SENTENCE_END = set("。！？!?.")

    paragraphs: list[str] = []
    cur_parts: list[str] = []
    prev_end: float | None = None

    for start, end, text in items:
        # 检测时间间隔
        if prev_end is not None and start - prev_end > gap_threshold and cur_parts:
            paragraphs.append("".join(cur_parts))
            cur_parts = []

        cur_parts.append(text)
        prev_end = end

        # 长段落分割：累积超过 200 字符且以句末标点结尾
        cur_text = "".join(cur_parts)
        if len(cur_text) >= 200 and text and text[-1] in _SENTENCE_END:
            paragraphs.append(cur_text)
            cur_parts = []
            prev_end = end

    if cur_parts:
        paragraphs.append("".join(cur_parts))

    return "\n\n".join(paragraphs) + "\n"


def collect_plain_text(segments: list[tuple]) -> str:
    """直接串联 ASR 结果文本，按段落合并输出。"""
    items: list[tuple[float, float, str]] = []
    for start, end, text in segments:
        line = _normalize_plain_line(text)
        if line:
            items.append((float(start), float(end), line))
    if not items:
        return ""
    return _merge_lines_into_paragraphs(items)


def normalize_segments_timeline(
    segments: list[tuple],
    min_duration_s: float = 0.8,
    max_duration_s: float = 12.0,
    continuous: bool = True,
) -> list[tuple[float, float, str]]:
    """
    清洗并归一化字幕时间轴：
    - 过滤噪声文本
    - 保证时间单调递增
    - 修正异常区间（end <= start）
    - 限制过短/过长片段时长
    - continuous=True 时，让每段字幕延续到下一段开始（无缝衔接）

    Args:
        segments: [(start, end, text), ...]
        min_duration_s: 最小片段时长
        max_duration_s: 最大片段时长
        continuous: 是否让字幕连续显示（前一段 end = 下一段 start）
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

    for i, (start, end, text) in enumerate(cleaned):
        start = max(start, cursor)
        duration = end - start

        if duration < min_duration_s:
            end = start + min_duration_s
        elif duration > max_duration_s:
            end = start + max_duration_s

        normalized.append((round(start, 3), round(end, 3), text))
        cursor = end

    # 让字幕连续显示：每段的 end = 下一段的 start
    if continuous and len(normalized) > 1:
        continuous_segments = []
        for i, (start, end, text) in enumerate(normalized):
            if i < len(normalized) - 1:
                # 非最后一段：end 设为下一段的 start
                next_start = normalized[i + 1][0]
                # 确保 end 不超过下一段 start，且不小于当前 start
                end = max(start + min_duration_s, min(end, next_start))
                # 如果 end 小于 next_start，扩展到 next_start
                if end < next_start:
                    end = next_start
            continuous_segments.append((round(start, 3), round(end, 3), text))
        normalized = continuous_segments

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


def segments_to_srt(segments: list[tuple], normalize: bool = True, wrap: bool = False, is_translated: bool = False) -> str:
    """
    将 segments 转为 SRT 字符串。

    Args:
        segments: [(start_s, end_s, text), ...] start/end 单位为秒
        normalize: 是否规范化时间轴
        wrap: 是否启用自动换行（中文25字/外文20词）
        is_translated: 是否为翻译后的文本（影响换行判断）

    Returns:
        SRT 格式字符串
    """
    segments = normalize_segments_timeline(segments) if normalize else segments
    if wrap:
        segments = wrap_segments_text(segments, is_translated=is_translated)
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
    将 segments 转为纯文本（去掉时间轴），按段落合并。

    Args:
        segments: [(start_s, end_s, text), ...]

    Returns:
        纯文本字符串，段落间空行分隔
    """
    segments = normalize_segments_timeline(segments) if normalize else segments
    items = [(float(s), float(e), t) for s, e, t in segments if t and _normalize_plain_line(t)]
    if not items:
        return ""
    return _merge_lines_into_paragraphs(items)


def _extract_role(text: str) -> str | None:
    """从 '角色N: ...' 中提取角色标识，失败返回 None。"""
    m = re.match(r'^(角色\d+)[:：]', text.strip())
    return m.group(1) if m else None


def _strip_role_prefix(text: str) -> str:
    """去掉 '角色N: ' 前缀，返回纯文字内容。"""
    return re.sub(r'^角色\d+[:：]\s*', '', text.strip())


def is_speaker_segments(segments: list[tuple]) -> bool:
    """判断 segments 是否含有说话人标注。"""
    for _, _, t in segments:
        if _extract_role(t):
            return True
    return False


def format_speaker_script(segments: list[tuple]) -> str:
    """
    将含说话人标注的 segments 格式化为脚本样式：
    同一角色的连续台词合并在同一块下，换角色时换行分块。
    每块显示角色名称和起始时间戳。
    """
    if not segments:
        return ""

    lines: list[str] = []
    cur_role: str | None = None
    cur_lines: list[str] = []
    cur_start: float = 0.0

    def flush():
        if cur_role and cur_lines:
            h = int(cur_start) // 3600
            m = (int(cur_start) % 3600) // 60
            s = int(cur_start) % 60
            ts = f"{h:02d}:{m:02d}:{s:02d}"
            lines.append(f"【{cur_role}】（{ts}）")
            for l in cur_lines:
                lines.append(l)
            lines.append("")

    for start, _end, text in segments:
        normalized = _normalize_plain_line(text)
        if not normalized:
            continue
        role = _extract_role(normalized)
        content = _strip_role_prefix(normalized) if role else normalized
        if not content:
            continue
        if role != cur_role:
            flush()
            cur_role = role or "旁白"
            cur_lines = [content]
            cur_start = start
        else:
            cur_lines.append(content)
    flush()

    return "\n".join(lines).rstrip() + "\n"


def save_srt(
    segments: list[tuple], output_path: str = None, normalize: bool = True, wrap: bool = False, is_translated: bool = False
) -> str:
    """保存 SRT 文件，返回文件路径

    Args:
        segments: 字幕片段列表
        output_path: 输出路径，None 则创建临时文件
        normalize: 是否规范化时间轴
        wrap: 是否启用自动换行（中文25字/外文20词）
        is_translated: 是否为翻译后的文本
    """
    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".srt", prefix="v2t_", delete=False, mode="w", encoding="utf-8"
        )
        output_path = tmp.name
        tmp.write(segments_to_srt(segments, normalize=normalize, wrap=wrap, is_translated=is_translated))
        tmp.close()
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(segments_to_srt(segments, normalize=normalize, wrap=wrap, is_translated=is_translated))
    return output_path


def save_plain(
    segments: list[tuple], output_path: str = None, normalize: bool = True
) -> str:
    """保存纯文本文件，返回文件路径。
    若 segments 含说话人标注，自动使用脚本格式按角色分块输出。
    """
    if is_speaker_segments(segments):
        content = format_speaker_script(segments)
    else:
        content = segments_to_plain(segments, normalize=normalize)

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="v2t_", delete=False, mode="w", encoding="utf-8"
        )
        output_path = tmp.name
        tmp.write(content)
        tmp.close()
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
    return output_path
