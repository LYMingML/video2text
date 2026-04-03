"""
SiliconFlow / OpenAI 兼容翻译后端
使用 SiliconFlow、Ollama 或任何 OpenAI 兼容 API 进行字幕翻译

支持：
  - SiliconFlow（硅基流动）
  - Ollama（本地推理）
  - 任何 OpenAI 兼容 API（通义千问、DeepSeek 等）
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from backends import register_translate
from backends.base_translate import TranslateBackend

logger = logging.getLogger(__name__)

_LINE_CACHE: dict[tuple[str, str, str], str] = {}
_MODEL_CACHE: dict[tuple[str, str], list[str]] = {}
_PARALLEL_THREADS = 5

_LANG_NAME = {
    "zh": "简体中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "es": "西班牙语",
    "fr": "法语",
    "de": "德语",
    "ru": "俄语",
}


def is_ollama_base_url(base_url: str | None) -> bool:
    normalized = (base_url or "").strip().lower()
    if not normalized:
        return False
    return ":11434" in normalized or "ollama" in normalized


def _normalize_ollama_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    lowered = normalized.lower()
    for suffix in ("/v1", "/api"):
        if lowered.endswith(suffix):
            return normalized[:-len(suffix)].rstrip("/")
    return normalized


def _build_translation_prompt(source_lang: str, target_lang: str, text: str) -> str:
    lang_hint = source_lang if source_lang and source_lang != "auto" else "unknown"
    target_code = (target_lang or "zh").strip().lower() or "zh"
    target_name = _LANG_NAME.get(target_code, target_code)
    return (
        f"你是专业字幕翻译助手。请把下面原文翻译成{target_name}。\n"
        "要求：\n"
        "1. 只输出译文，不要解释。\n"
        "2. 保持原句语气与信息，不要扩写。\n"
        f"3. 如果原文已是{target_name}，直接输出原文。\n"
        f"源语言提示: {lang_hint}\n"
        f"目标语言代码: {target_code}\n"
        f"原文: {text}"
    )


def _model_cache_key(base_url: str, api_key: str) -> tuple[str, str]:
    return base_url, (api_key[:12] if api_key else "")


def list_available_models(
    base_url: str | None = None,
    api_key: str | None = None,
    raise_on_error: bool = False,
) -> list[str]:
    """获取可用模型列表（SiliconFlow 或 Ollama）。"""
    base = (base_url or "").rstrip("/")
    key = (api_key or "").strip()
    if not base:
        return []

    cache_key = _model_cache_key(base, key)
    if cache_key in _MODEL_CACHE:
        return _MODEL_CACHE[cache_key]

    if is_ollama_base_url(base):
        ollama_base = _normalize_ollama_base_url(base)
        req = urllib.request.Request(
            f"{ollama_base}/api/tags",
            headers={"Content-Type": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            obj = json.loads(raw)
            data = obj.get("models", [])
            _MODEL_CACHE[cache_key] = [m.get("name", "") for m in data if isinstance(m, dict) and m.get("name")]
        except Exception as exc:
            if raise_on_error:
                raise RuntimeError(f"获取 Ollama 模型失败: {exc}") from exc
            logger.warning("获取 Ollama 模型列表失败: %s", exc)
            _MODEL_CACHE[cache_key] = []
        return _MODEL_CACHE[cache_key]

    if not key:
        _MODEL_CACHE[cache_key] = []
        return _MODEL_CACHE[cache_key]

    req = urllib.request.Request(
        f"{base}/models",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        obj = json.loads(raw)
        data = obj.get("data", [])
        _MODEL_CACHE[cache_key] = [m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")]
    except Exception as exc:
        if raise_on_error:
            raise RuntimeError(f"获取在线模型失败: {exc}") from exc
        logger.warning("获取模型列表失败: %s", exc)
        _MODEL_CACHE[cache_key] = []
    return _MODEL_CACHE[cache_key]


def _resolve_model_name(model_name: str, base_url: str, api_key: str) -> str:
    wanted = (model_name or "").strip() or "Pro/moonshotai/Kimi-K2.5"
    remote = list_available_models(base_url, api_key) if base_url and api_key else []
    if not remote:
        return wanted
    if wanted in remote:
        return wanted
    aliases = {
        "Kimi-K2.5": "Pro/moonshotai/Kimi-K2.5",
        "moonshotai/Kimi-K2.5": "Pro/moonshotai/Kimi-K2.5",
    }
    if wanted in aliases and aliases[wanted] in remote:
        return aliases[wanted]
    wanted_l = wanted.lower()
    for rid in remote:
        if wanted_l in rid.lower():
            return rid
    return wanted


def _stream_chat_completion(
    system_prompt: str,
    user_prompt: str,
    model_name: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.1,
    top_p: float = 0.9,
) -> str:
    if is_ollama_base_url(base_url):
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": True,
            "options": {"temperature": temperature, "top_p": top_p},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{_normalize_ollama_base_url(base_url)}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        chunks: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    message = obj.get("message", {}) if isinstance(obj, dict) else {}
                    token = message.get("content", "") if isinstance(message, dict) else ""
                    if isinstance(token, str) and token:
                        chunks.append(token)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = str(exc)
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        return "".join(chunks).strip()

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "temperature": temperature,
        "top_p": top_p,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    chunks = []
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                choices = obj.get("choices", [])
                token = ""
                if choices and isinstance(choices, list):
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    token = content if isinstance(content, str) else ""
                if token:
                    chunks.append(token)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    return "".join(chunks).strip()


def _normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def set_parallel_threads(n: int):
    """动态设置翻译并行线程数。"""
    global _PARALLEL_THREADS
    _PARALLEL_THREADS = max(1, min(50, int(n)))


def get_parallel_threads() -> int:
    return _PARALLEL_THREADS


@register_translate
class SiliconFlowTranslate(TranslateBackend):
    """SiliconFlow / OpenAI 兼容翻译后端"""

    @property
    def name(self) -> str:
        return "SiliconFlow Translate"

    @property
    def description(self) -> str:
        return "使用 SiliconFlow / Ollama / OpenAI 兼容 API 进行字幕翻译"

    def translate_segments(
        self,
        segments: list[tuple[float, float, str]],
        source_lang: str = "auto",
        target_lang: str = "zh",
        log_cb: Callable[[str], None] | None = None,
        progress_cb: Callable[[int, int, float], None] | None = None,
        **kwargs,
    ) -> list[tuple[float, float, str]]:
        if not segments:
            return []

        base_url = (kwargs.get("base_url") or "").rstrip()
        api_key = (kwargs.get("api_key") or "").strip()
        model_name = (kwargs.get("model_name") or "").strip()

        use_model = _resolve_model_name(model_name or "Pro/moonshotai/Kimi-K2.5", base_url, api_key)

        if log_cb:
            log_cb(f"[TRANS] 使用模型: {use_model}, 并行线程: {_PARALLEL_THREADS}")

        total = len(segments)
        t0 = time.time()
        results: list[tuple[int, float, float, str] | None] = [None] * total
        completed_count = [0]

        def translate_one(idx: int, start: float, end: float, text: str):
            line = _normalize_line(text)
            if not line:
                return (idx, start, end, "")

            cache_key = (base_url, use_model, line)
            if cache_key in _LINE_CACHE:
                return (idx, start, end, _LINE_CACHE[cache_key])

            prompt = _build_translation_prompt(source_lang, target_lang, line)
            translated = _stream_chat_completion(
                system_prompt="你是专业翻译助手。",
                user_prompt=prompt,
                model_name=use_model,
                base_url=base_url,
                api_key=api_key,
            )
            translated = _normalize_line(translated) or line
            _LINE_CACHE[cache_key] = translated
            return (idx, start, end, translated)

        with ThreadPoolExecutor(max_workers=_PARALLEL_THREADS) as executor:
            futures = {}
            for idx, (start, end, text) in enumerate(segments):
                future = executor.submit(translate_one, idx, start, end, text)
                futures[future] = idx

            for future in as_completed(futures):
                try:
                    result = future.result()
                    results[result[0]] = result
                    completed_count[0] += 1

                    elapsed = max(0.001, time.time() - t0)
                    avg_per_seg = elapsed / completed_count[0]
                    eta = max(0.0, (total - completed_count[0]) * avg_per_seg)
                    if progress_cb:
                        progress_cb(completed_count[0], total, eta)

                    if log_cb and (completed_count[0] == total or completed_count[0] % 20 == 0):
                        log_cb(f"[TRANS] 翻译进度: {completed_count[0]}/{total}")
                except Exception as exc:
                    raise RuntimeError(f"翻译调用失败: {exc}") from exc

        translated: list[tuple[float, float, str]] = []
        for r in results:
            if r is not None:
                translated.append((r[1], r[2], r[3]))
        return translated

    def unload(self) -> None:
        global _LINE_CACHE, _MODEL_CACHE
        _LINE_CACHE.clear()
        _MODEL_CACHE.clear()
