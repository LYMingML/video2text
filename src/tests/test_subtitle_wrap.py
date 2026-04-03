#!/usr/bin/env python3
"""测试字幕换行功能"""

import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))

from utils.subtitle import wrap_text, wrap_segments_text, _wrap_chinese_text, _wrap_english_text, _is_chinese_text


def test_chinese_wrap():
    """测试中文换行（每25字符）"""
    text = "这是一个非常长的中文字符串，用来测试换行功能是否正常工作，每25个字符应该换行一次。"
    result = _wrap_chinese_text(text, max_chars=25)
    print("=== 中文换行测试 ===")
    print(f"原文: {text}")
    print(f"换行后:\n{result}")
    lines = result.split("\n")
    for i, line in enumerate(lines):
        print(f"  行{i+1}: {len(line)}字符 - {line}")
    print()


def test_english_wrap():
    """测试英文换行（每20词）"""
    text = "This is a very long English sentence used to test the line wrapping functionality. It should wrap every 20 words."
    result = _wrap_english_text(text, max_words=20)
    print("=== 英文换行测试 ===")
    print(f"原文: {text}")
    print(f"换行后:\n{result}")
    lines = result.split("\n")
    for i, line in enumerate(lines):
        word_count = len(line.split())
        print(f"  行{i+1}: {word_count}词 - {line}")
    print()


def test_is_chinese():
    """测试中文检测"""
    print("=== 中文检测测试 ===")
    test_cases = [
        ("这是一个中文文本", True),
        ("This is English text", False),
        ("Hello 世界", False),  # 中英混合，中文占比不足50%
        ("你好世界这是中文 text", True),   # 中英混合，中文占比超过50% (8个中文字符 / 14总字符 = 57%)
        ("你好世界这是中文", True),  # 纯中文
    ]
    for text, expected in test_cases:
        result = _is_chinese_text(text)
        status = "✓" if result == expected else "✗"
        chinese_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        ratio = chinese_count / len(text) if text else 0
        print(f"{status} '{text}' -> {result} (期望: {expected}, 中文占比: {ratio:.1%})")
    print()


def test_wrap_text():
    """测试 wrap_text 函数"""
    print("=== wrap_text 综合测试 ===")

    # 测试翻译后的中文文本
    chinese_text = "这是一个非常长的中文字符串，用来测试翻译后的字幕换行功能是否正常工作，每25个字符应该换行一次。"
    result = wrap_text(chinese_text, is_translated=True)
    print(f"翻译后中文:\n{result}\n")

    # 测试外文原文
    english_text = "This is a very long English sentence used to test the line wrapping functionality for foreign language subtitles. It should wrap every 20 words."
    result = wrap_text(english_text, is_translated=False)
    print(f"外文原文:\n{result}\n")


def test_wrap_segments():
    """测试 segments 换行"""
    print("=== Segments 换行测试 ===")
    segments = [
        (0.0, 5.0, "This is the first segment with some English text that should be wrapped."),
        (5.0, 10.0, "这是第二个中文片段，用于测试翻译后的字幕换行功能。"),
    ]

    # 原文（英文）换行 - 每20词
    wrapped_original = wrap_segments_text(segments, is_translated=False)
    print("原文换行（英文20词）:")
    for start, end, text in wrapped_original:
        print(f"  [{start:.1f} - {end:.1f}]\n{text}\n")

    # 翻译后（中文）换行 - 每25字
    wrapped_translated = wrap_segments_text(segments, is_translated=True)
    print("翻译后换行（中文25字）:")
    for start, end, text in wrapped_translated:
        print(f"  [{start:.1f} - {end:.1f}]\n{text}\n")


if __name__ == "__main__":
    test_chinese_wrap()
    test_english_wrap()
    test_is_chinese()
    test_wrap_text()
    test_wrap_segments()
    print("所有测试完成！")
