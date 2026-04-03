"""
ASR 后端抽象基类

所有 ASR 后端（FunASR、Whisper、VibeVoice 等）必须继承此基类并实现 transcribe() 方法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


class ASRBackend(ABC):
    """ASR 后端抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """后端显示名称，如 'VibeVoice ASR'"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """后端简短描述"""
        ...

    @property
    def default_model(self) -> str:
        """默认模型名称"""
        return ""

    @property
    def supported_models(self) -> list[str]:
        """支持的模型列表"""
        return []

    @property
    def default_chunk_seconds(self) -> int:
        """默认切片时长（秒）"""
        return 120

    @property
    def default_overlap_seconds(self) -> int:
        """默认重叠时长（秒）"""
        return 10

    @property
    def sample_rate(self) -> int:
        """输出音频采样率"""
        return 16000

    @abstractmethod
    def transcribe(
        self,
        audio_path: str,
        model_name: str = "",
        language: str = "auto",
        device: str = "auto",
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> list[tuple[float, float, str]]:
        """
        转录音频文件。

        Args:
            audio_path: 音频文件路径
            model_name: 模型名称
            language: 语言代码（auto/zh/en/ja/ko 等）
            device: 计算设备（auto/cuda:0/cpu）
            progress_cb: 进度回调 (ratio: float, msg: str)

        Returns:
            [(start, end, text), ...] 时间戳片段列表
        """
        ...

    def unload(self) -> None:
        """释放模型缓存（可选覆盖）"""
        pass
