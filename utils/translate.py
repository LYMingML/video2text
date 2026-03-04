"""
文本翻译工具
优先使用本地 Ollama 大模型翻译为中文，失败时回退 ModelScope。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable

logger = logging.getLogger(__name__)

_TRANSLATOR_CACHE: dict[str, object] = {}
_OLLAMA_LINE_CACHE: dict[tuple[str, str], str] = {}

_DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
_DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_TRANSLATE_MODEL", "qwen3.5:4b").strip()
_DEFAULT_TRANSLATE_BACKEND = os.getenv("TRANSLATE_BACKEND", "ollama").strip().lower()
_ALLOW_MODELSCOPE_FALLBACK = os.getenv("ALLOW_MODELSCOPE_FALLBACK", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_OLLAMA_MODEL_CANDIDATES = [
    # 先尝试翻译/多语言表现更好的中小模型，适配 8GB 显存设备
    "qwen3.5:4b",
    "qwen3.5:2b",
    "translategemma:4b",
    "qwen3:4b",
    "qwen2.5:3b",
    "qwen3.5:9b",
    "qwen3:8b",
    "qwen2.5:7b",
    # 大模型放后面，避免在小显存设备上默认命中导致吞吐过低
    "qwen3:30b-a3b",
    "llama3.1:8b",
]

# 常见语种到中文的模型候选列表（按优先级）
_TRANSLATION_MODELS: dict[str, list[str]] = {
    "en": [
        "damo/nlp_csanmt_translation_en2zh",
    ],
    "ja": [
        "damo/nlp_csanmt_translation_ja2zh",
    ],
    "ko": [
        "damo/nlp_csanmt_translation_ko2zh",
    ],
    "es": [
        "damo/nlp_csanmt_translation_es2zh",
    ],
    # 自动推断场景下使用通用候选
    "auto": [
        "damo/nlp_csanmt_translation_en2zh",
    ],
}


def _http_post_json(url: str, payload: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _http_get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _list_ollama_models() -> list[str]:
    try:
        payload = _http_get_json(f"{_DEFAULT_OLLAMA_URL}/api/tags", timeout=10)
        models = payload.get("models", [])
        names = [m.get("name", "").strip() for m in models if isinstance(m, dict)]
        return [n for n in names if n]
    except Exception as exc:
        logger.warning("获取 Ollama 模型列表失败: %s", exc)
        return []


def _pick_ollama_model() -> str:
    if _DEFAULT_OLLAMA_MODEL:
        return _DEFAULT_OLLAMA_MODEL

    installed = _list_ollama_models()
    if not installed:
        return "qwen2.5:7b"

    for candidate in _OLLAMA_MODEL_CANDIDATES:
        if candidate in installed:
            return candidate

    return installed[0]


def _build_translation_prompt(source_lang: str, text: str) -> str:
    lang_hint = source_lang if source_lang and source_lang != "auto" else "unknown"
    return (
        "你是专业字幕翻译助手。请把下面原文翻译成简体中文。\n"
        "要求：\n"
        "1. 只输出译文，不要解释。\n"
        "2. 保持原句语气与信息，不要扩写。\n"
        "3. 如果原文已是中文，直接输出原文。\n"
        f"源语言提示: {lang_hint}\n"
        f"原文: {text}"
    )


def _translate_line_with_ollama(text: str, source_lang: str, model_name: str) -> str:
    cache_key = (model_name, text)
    if cache_key in _OLLAMA_LINE_CACHE:
        return _OLLAMA_LINE_CACHE[cache_key]

    prompt = _build_translation_prompt(source_lang, text)
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": True,
        "options": {
            "temperature": 0.1,
            "top_p": 0.9,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_DEFAULT_OLLAMA_URL}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    chunks: list[str] = []
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            token = str(obj.get("response", ""))
            if token:
                chunks.append(token)
            if obj.get("done") is True:
                break

    translated = "".join(chunks).strip()
    _OLLAMA_LINE_CACHE[cache_key] = translated
    return translated


def _extract_translated_text(result: object) -> str:
    """从不同返回结构中提取翻译文本。"""
    if result is None:
        return ""

    if isinstance(result, str):
        return result.strip()

    if isinstance(result, list):
        texts: list[str] = []
        for item in result:
            line = _extract_translated_text(item)
            if line:
                texts.append(line)
        return "\n".join(texts).strip()

    if isinstance(result, dict):
        for key in ("translation", "text", "output", "result"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                merged = "\n".join(str(v).strip() for v in value if str(v).strip()).strip()
                if merged:
                    return merged

    return ""


def _get_translator(source_lang: str):
    """懒加载并缓存翻译 pipeline。"""
    candidates = _TRANSLATION_MODELS.get(source_lang) or _TRANSLATION_MODELS["auto"]

    for model_name in candidates:
        if model_name in _TRANSLATOR_CACHE:
            return _TRANSLATOR_CACHE[model_name], model_name

        try:
            from modelscope.pipelines import pipeline
            from modelscope.utils.constant import Tasks

            logger.info("加载翻译模型: %s", model_name)
            translator = pipeline(task=Tasks.translation, model=model_name)
            _TRANSLATOR_CACHE[model_name] = translator
            return translator, model_name
        except Exception as exc:
            logger.warning("翻译模型加载失败 %s: %s", model_name, exc)
            continue

    raise RuntimeError(
        "中文翻译模型加载失败，请检查网络或 ModelScope 模型可用性。"
    )


def _normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _translate_segments_with_ollama(
    segments: list[tuple[float, float, str]],
    source_lang: str,
    log_cb: Callable[[str], None] | None,
    progress_cb: Callable[[int, int, float], None] | None = None,
) -> list[tuple[float, float, str]]:
    model_name = _pick_ollama_model()
    installed = set(_list_ollama_models())
    if installed and model_name not in installed:
        raise RuntimeError(
            f"未找到 Ollama 模型 {model_name}。请先执行: ollama pull {model_name}"
        )

    if log_cb:
        log_cb(f"[TRANS] 使用 Ollama 模型: {model_name}")

    translated: list[tuple[float, float, str]] = []
    total = len(segments)
    t0 = time.time()
    for idx, (start, end, text) in enumerate(segments, start=1):
        line = _normalize_line(text)
        if not line:
            translated.append((start, end, ""))
            continue

        try:
            zh_text = _translate_line_with_ollama(line, source_lang=source_lang, model_name=model_name)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise RuntimeError(f"Ollama 翻译调用失败: {exc}") from exc

        zh_line = _normalize_line(zh_text) or line
        translated.append((start, end, zh_line))

        elapsed = max(0.001, time.time() - t0)
        avg_per_seg = elapsed / idx
        eta = max(0.0, (total - idx) * avg_per_seg)
        if progress_cb:
            progress_cb(idx, total, eta)

        if log_cb and (idx == total or idx % 20 == 0):
            log_cb(f"[TRANS] 翻译进度: {idx}/{total}")

    return translated


def translate_segments_to_chinese(
    segments: list[tuple[float, float, str]],
    source_lang: str = "auto",
    log_cb: Callable[[str], None] | None = None,
    progress_cb: Callable[[int, int, float], None] | None = None,
) -> list[tuple[float, float, str]]:
    """将时间轴片段翻译为中文，保留原时间戳。"""
    if not segments:
        return []

    # 默认固定使用 Ollama qwen3.5:4b；仅在显式开启时回退 ModelScope。
    if _DEFAULT_TRANSLATE_BACKEND in ("ollama", "auto"):
        try:
            return _translate_segments_with_ollama(
                segments,
                source_lang,
                log_cb,
                progress_cb=progress_cb,
            )
        except Exception as exc:
            if not _ALLOW_MODELSCOPE_FALLBACK:
                raise RuntimeError(
                    f"Ollama 翻译失败: {exc}。"
                    "请确认已安装并可运行 qwen3.5:4b（必要时先升级 Ollama）。"
                ) from exc
            logger.warning("Ollama 翻译失败，回退 ModelScope: %s", exc)
            if log_cb:
                log_cb(f"[WARN] Ollama 翻译失败，回退 ModelScope: {exc}")

    translator, model_name = _get_translator(source_lang)
    if log_cb:
        log_cb(f"[TRANS] 使用 ModelScope 翻译模型: {model_name}")

    translated: list[tuple[float, float, str]] = []
    total = len(segments)
    t0 = time.time()

    for idx, (start, end, text) in enumerate(segments, start=1):
        line = _normalize_line(text)
        if not line:
            translated.append((start, end, ""))
            continue

        try:
            result = translator(line)
            zh_text = _extract_translated_text(result)
        except Exception as exc:
            logger.warning("翻译失败（第 %s 行）: %s", idx, exc)
            zh_text = ""

        if not zh_text:
            zh_text = line

        translated.append((start, end, _normalize_line(zh_text)))

        elapsed = max(0.001, time.time() - t0)
        avg_per_seg = elapsed / idx
        eta = max(0.0, (total - idx) * avg_per_seg)
        if progress_cb:
            progress_cb(idx, total, eta)

        if log_cb and (idx == total or idx % 20 == 0):
            log_cb(f"[TRANS] 翻译进度: {idx}/{total}")

    return translated
