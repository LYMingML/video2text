"""
文本翻译工具
使用硅基流动（SiliconFlow）API 将文本翻译为目标语言。

配置来源：项目根目录 .env
- ONLINE_MODEL_PROFILE_*
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

from utils.online_models import load_profiles

logger = logging.getLogger(__name__)

_LINE_CACHE: dict[tuple[str, str, str], str] = {}
_MODEL_CACHE: dict[tuple[str, str], list[str]] = {}

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")


def _load_dotenv(path: str):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                key = k.strip()
                value = v.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as exc:
        logger.warning("加载 .env 失败: %s", exc)


_load_dotenv(_ENV_PATH)

def _load_default_online_config() -> tuple[str, str, str]:
    try:
        profiles, active = load_profiles()
        profile = next((p for p in profiles if p.get("name") == active), profiles[0] if profiles else None)
        if profile:
            base_url = str(profile.get("base_url", "https://api.siliconflow.cn/v1")).strip() or "https://api.siliconflow.cn/v1"
            api_key = str(profile.get("api_key", "")).strip()
            model = str(profile.get("default_model", "Pro/moonshotai/Kimi-K2.5")).strip() or "Pro/moonshotai/Kimi-K2.5"
            return base_url.rstrip("/"), api_key, model
    except Exception as exc:
        logger.warning("读取在线模型默认配置失败: %s", exc)
    return "https://api.siliconflow.cn/v1", "", "Pro/moonshotai/Kimi-K2.5"


_DEFAULT_BASE_URL, _DEFAULT_API_KEY, _DEFAULT_MODEL = _load_default_online_config()

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




def get_default_online_config() -> dict[str, str]:
    base_url, api_key, model_name = _load_default_online_config()
    return {
        "base_url": base_url,
        "api_key": api_key,
        "model_name": model_name,
    }


def _model_cache_key(base_url: str, api_key: str) -> tuple[str, str]:
    # 缓存键只保存 key 前缀，避免在内存中复制完整密钥。
    return base_url, (api_key[:12] if api_key else "")


def list_available_models(
    base_url: str | None = None,
    api_key: str | None = None,
    raise_on_error: bool = False,
) -> list[str]:
    base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    key = (api_key if api_key is not None else _DEFAULT_API_KEY).strip()
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
        logger.warning("获取硅基流动模型列表失败: %s", exc)
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

    # 模糊匹配，允许用户写简写模型名
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
            "options": {
                "temperature": temperature,
                "top_p": top_p,
            },
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

    chunks: list[str] = []
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


def _translate_line_with_siliconflow(
    text: str,
    source_lang: str,
    target_lang: str,
    model_name: str,
    base_url: str,
    api_key: str,
) -> str:
    cache_key = (base_url, model_name, text)
    if cache_key in _LINE_CACHE:
        return _LINE_CACHE[cache_key]

    if not api_key:
        raise RuntimeError("ONLINE_MODEL_PROFILE 的 API_KEY 为空，请在“配置模型”页设置")

    prompt = _build_translation_prompt(source_lang, target_lang, text)
    translated = _stream_chat_completion(
        system_prompt="你是专业翻译助手。",
        user_prompt=prompt,
        model_name=model_name,
        base_url=base_url,
        api_key=api_key,
        temperature=0.1,
        top_p=0.9,
    )
    _LINE_CACHE[cache_key] = translated
    return translated


def _normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()




def _translate_segments_with_siliconflow(
    segments: list[tuple[float, float, str]],
    source_lang: str,
    target_lang: str,
    log_cb: Callable[[str], None] | None,
    progress_cb: Callable[[int, int, float], None] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
) -> list[tuple[float, float, str]]:
    use_base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    use_api_key = (api_key if api_key is not None else _DEFAULT_API_KEY).strip()
    use_model_name = _resolve_model_name(model_name or _DEFAULT_MODEL, use_base_url, use_api_key)

    if log_cb:
        log_cb(f"[TRANS] 使用在线模型: {use_model_name}")

    translated: list[tuple[float, float, str]] = []
    total = len(segments)
    t0 = time.time()
    for idx, (start, end, text) in enumerate(segments, start=1):
        line = _normalize_line(text)
        if not line:
            translated.append((start, end, ""))
            continue

        try:
            zh_text = _translate_line_with_siliconflow(
                line,
                source_lang=source_lang,
                target_lang=target_lang,
                model_name=use_model_name,
                base_url=use_base_url,
                api_key=use_api_key,
            )
        except Exception as exc:
            raise RuntimeError(f"硅基流动翻译调用失败: {exc}") from exc

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


def translate_segments(
    segments: list[tuple[float, float, str]],
    source_lang: str = "auto",
    target_lang: str = "zh",
    log_cb: Callable[[str], None] | None = None,
    progress_cb: Callable[[int, int, float], None] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
) -> list[tuple[float, float, str]]:
    """将时间轴片段翻译为目标语言，保留原时间戳。"""
    if not segments:
        return []

    return _translate_segments_with_siliconflow(
        segments,
        source_lang,
        target_lang,
        log_cb,
        progress_cb=progress_cb,
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
    )


def translate_segments_to_chinese(
    segments: list[tuple[float, float, str]],
    source_lang: str = "auto",
    log_cb: Callable[[str], None] | None = None,
    progress_cb: Callable[[int, int, float], None] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
) -> list[tuple[float, float, str]]:
    """兼容旧调用：翻译为中文。"""
    return translate_segments(
        segments,
        source_lang=source_lang,
        target_lang="zh",
        log_cb=log_cb,
        progress_cb=progress_cb,
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
    )
