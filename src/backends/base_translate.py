"""
翻译后端抽象基类

所有翻译后端（SiliconFlow、Ollama 等）必须继承此基类并实现 translate_segments() 方法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


class TranslateBackend(ABC):
    """翻译后端抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """后端显示名称，如 'SiliconFlow Translate'"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """后端简短描述"""
        ...

    @abstractmethod
    def translate_segments(
        self,
        segments: list[tuple[float, float, str]],
        source_lang: str = "auto",
        target_lang: str = "zh",
        log_cb: Callable[[str], None] | None = None,
        progress_cb: Callable[[int, int, float], None] | None = None,
        **kwargs,
    ) -> list[tuple[float, float, str]]:
        """
        翻译字幕片段，保留原时间戳。

        Args:
            segments: [(start, end, text), ...]
            source_lang: 源语言
            target_lang: 目标语言
            log_cb: 日志回调
            progress_cb: 进度回调 (completed, total, eta_seconds)
            **kwargs: 后端特定参数（base_url, api_key, model_name 等）

        Returns:
            [(start, end, translated_text), ...]
        """
        ...

    def unload(self) -> None:
        """释放缓存（可选覆盖）"""
        pass
