"""
后端注册表

通过 @register_asr / @register_translate 装饰器注册后端，
通过 get_asr_backend() / get_translate_backend() 获取实例。
"""

from __future__ import annotations

import logging

from backends.base_asr import ASRBackend
from backends.base_translate import TranslateBackend

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_ASR_REGISTRY: dict[str, type[ASRBackend]] = {}
_TRANSLATE_REGISTRY: dict[str, type[TranslateBackend]] = {}


def register_asr(cls: type[ASRBackend]) -> type[ASRBackend]:
    """装饰器：注册 ASR 后端"""
    _ASR_REGISTRY[cls.__name__] = cls
    return cls


def register_translate(cls: type[TranslateBackend]) -> type[TranslateBackend]:
    """装饰器：注册翻译后端"""
    _TRANSLATE_REGISTRY[cls.__name__] = cls
    return cls


def get_asr_backend(name: str) -> ASRBackend:
    """根据类名获取 ASR 后端实例"""
    if name not in _ASR_REGISTRY:
        raise ValueError(f"未知 ASR 后端: {name}，可用: {list(_ASR_REGISTRY.keys())}")
    return _ASR_REGISTRY[name]()


def list_asr_backends() -> list[str]:
    """列出所有已注册的 ASR 后端类名"""
    return list(_ASR_REGISTRY.keys())


def get_asr_backend_info(name: str) -> dict:
    """获取 ASR 后端元信息"""
    instance = get_asr_backend(name)
    return {
        "class_name": name,
        "name": instance.name,
        "description": instance.description,
        "default_model": instance.default_model,
        "supported_models": instance.supported_models,
        "default_chunk_seconds": instance.default_chunk_seconds,
        "default_overlap_seconds": instance.default_overlap_seconds,
        "sample_rate": instance.sample_rate,
    }


def get_translate_backend(name: str) -> TranslateBackend:
    """根据类名获取翻译后端实例"""
    if name not in _TRANSLATE_REGISTRY:
        raise ValueError(f"未知翻译后端: {name}，可用: {list(_TRANSLATE_REGISTRY.keys())}")
    return _TRANSLATE_REGISTRY[name]()


def list_translate_backends() -> list[str]:
    """列出所有已注册的翻译后端类名"""
    return list(_TRANSLATE_REGISTRY.keys())


# ---------------------------------------------------------------------------
# 自动导入所有后端模块，触发 @register_* 装饰器
# ---------------------------------------------------------------------------

_BACKEND_MODULES = [
    ("backends.funasr_asr", "FunASR ASR"),
    ("backends.whisper_asr", "Whisper ASR"),
    ("backends.vibevoice_asr", "VibeVoice ASR"),
    ("backends.siliconflow_translate", "SiliconFlow Translate"),
]


def _auto_import_backends():
    """自动导入所有后端模块，缺失依赖的模块静默跳过"""
    for module_name, display_name in _BACKEND_MODULES:
        try:
            __import__(module_name)
            _logger.info(f"后端已加载: {display_name}")
        except ImportError as e:
            _logger.info(f"后端跳过（缺少依赖）: {display_name} — {e}")
        except Exception as e:
            _logger.warning(f"后端加载异常: {display_name} — {e}")


_auto_import_backends()
