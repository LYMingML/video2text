#!/usr/bin/env python3
"""
ML 推理封装模块
被 Rust 服务通过子进程调用，返回 JSON 结果
"""

import json
import sys
import os
import re
import logging
from pathlib import Path
from typing import List, Tuple, Optional, Callable

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 模型缓存
_model_cache = {}


def extract_audio(video_path: str, output_path: str) -> bool:
    """使用 ffmpeg 提取音频"""
    import subprocess
    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-acodec", "pcm_s16le",
        "-loglevel", "error",
        output_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0
    except Exception as e:
        logger.error(f"ffmpeg error: {e}")
        return False


def get_audio_duration(audio_path: str) -> float:
    """获取音频时长"""
    import subprocess
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except:
        pass
    return 0.0


def funasr_transcribe(
    audio_path: str,
    model_name: str = "paraformer-zh",
    language: str = "auto",
    device: str = "cuda:0"
) -> List[Tuple[float, float, str]]:
    """使用 FunASR 转录音频"""
    from funasr import AutoModel
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    cache_key = (model_name, device)
    if cache_key not in _model_cache:
        logger.info(f"Loading FunASR model: {model_name}")
        model = AutoModel(
            model=model_name,
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            punc_model="ct-punc",
            device=device,
            disable_update=True,
            hub="ms",
        )
        _model_cache[cache_key] = model
    else:
        model = _model_cache[cache_key]

    lang = language if language != "auto" else "auto"
    res = model.generate(
        input=audio_path,
        language=lang,
        use_itn=True,
        batch_size_s=300,
        merge_vad=True,
        merge_length_s=15,
    )

    segments = []
    for item in res:
        text = item.get("text", "").strip()
        if not text:
            continue

        # 清理文本
        text = rich_transcription_postprocess(text)
        text = re.sub(r"<\|[^|>]+\|>", " ", text)
        text = re.sub(r"[\U0001F300-\U0001F9FF\U00002600-\U000027BF]", "", text)
        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            continue

        timestamps = item.get("timestamp", [])
        if timestamps and len(timestamps) >= len(text):
            # 按标点拆句
            ENDINGS = set("。！？…!?")
            seg_start = 0
            for i, char in enumerate(text):
                is_last = (i == len(text) - 1)
                if char in ENDINGS or is_last:
                    sentence = text[seg_start:i + 1].strip()
                    if sentence:
                        start_s = timestamps[seg_start][0] / 1000
                        end_s = timestamps[i][1] / 1000
                        segments.append((start_s, end_s, sentence))
                    seg_start = i + 1
        else:
            # 没有时间戳，估算
            start = segments[-1][1] if segments else 0.0
            duration = max(1.5, min(12.0, len(text) / 4.0))
            segments.append((start, start + duration, text))

    return segments


def whisper_transcribe(
    audio_path: str,
    model_name: str = "medium",
    language: str = "auto",
    device: str = "cuda"
) -> List[Tuple[float, float, str]]:
    """使用 faster-whisper 转录音频"""
    from faster_whisper import WhisperModel

    cache_key = (model_name, device, "int8")
    if cache_key not in _model_cache:
        logger.info(f"Loading Whisper model: {model_name}")
        compute_type = "int8" if device == "cuda" else "int8"
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _model_cache[cache_key] = model
    else:
        model = _model_cache[cache_key]

    lang = None if language == "auto" else language
    segments_iter, info = model.transcribe(
        audio_path,
        beam_size=5,
        best_of=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        language=lang,
        initial_prompt="以下是普通话的句子。" if (lang in ("zh", None)) else None,
    )

    segments = []
    for seg in segments_iter:
        text = seg.text.strip()
        if text:
            segments.append((seg.start, seg.end, text))

    return segments


def translate_segments(
    segments: List[Tuple[float, float, str]],
    target_lang: str = "zh",
    base_url: str = "https://api.siliconflow.cn/v1",
    api_key: str = "",
    model_name: str = "Pro/moonshotai/Kimi-K2.5"
) -> List[Tuple[float, float, str]]:
    """翻译字幕段落"""
    import urllib.request
    import urllib.error

    LANG_NAME = {
        "zh": "简体中文",
        "en": "英语",
        "ja": "日语",
        "ko": "韩语",
        "es": "西班牙语",
    }

    target_name = LANG_NAME.get(target_lang, target_lang)

    def translate_text(text: str) -> str:
        prompt = (
            f"你是专业字幕翻译助手。请把下面原文翻译成{target_name}。\n"
            "要求：\n"
            "1. 只输出译文，不要解释。\n"
            "2. 保持原句语气与信息，不要扩写。\n"
            f"3. 如果原文已是{target_name}，直接输出原文。\n"
            f"原文: {text}"
        )

        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "你是专业翻译助手。"},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "temperature": 0.1,
            "top_p": 0.9,
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
                    if not line or not line.startswith("data:"):
                        continue
                    line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    try:
                        obj = json.loads(line)
                        choices = obj.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                chunks.append(content)
                    except:
                        continue
        except urllib.error.HTTPError as e:
            logger.error(f"Translation API error: {e}")
            return text

        return "".join(chunks).strip() or text

    results = []
    for start, end, text in segments:
        translated = translate_text(text)
        results.append((start, end, translated))

    return results


def download_url(
    url: str,
    output_dir: str,
    subtitle_langs: str = "zh-Hans,zh-CN,zh,en"
) -> dict:
    """下载 URL 视频，返回下载的文件信息"""
    import subprocess
    import yt_dlp

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 先获取标题
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title", "video")

    # 清理标题作为文件名
    safe_title = re.sub(r'[^\w\s-]', '', title).strip()[:50]
    if not safe_title:
        safe_title = "video"

    title_dir = output_dir / safe_title
    title_dir.mkdir(exist_ok=True)

    # 下载视频
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(title_dir / "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # 查找下载的文件
    media_files = list(title_dir.glob("*"))
    media_file = None
    for f in media_files:
        if f.suffix.lower() in [".mp4", ".webm", ".mkv", ".mp3", ".wav", ".m4a"]:
            media_file = f
            break

    if not media_file:
        return {"error": "No media file downloaded"}

    # 尝试下载字幕
    subtitle_file = None
    if subtitle_langs:
        ydl_opts_sub = {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": subtitle_langs.split(","),
            "skip_download": True,
            "outtmpl": str(title_dir / "%(title)s"),
            "quiet": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts_sub) as ydl:
                ydl.download([url])
            # 查找字幕文件
            for ext in [".srt", ".vtt"]:
                subs = list(title_dir.glob(f"*{ext}"))
                if subs:
                    subtitle_file = subs[0]
                    break
        except:
            pass

    return {
        "media_path": str(media_file),
        "subtitle_path": str(subtitle_file) if subtitle_file else None,
        "title": title,
    }


def main():
    """命令行入口，通过 JSON 参数调用"""
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No command specified"}))
        sys.exit(1)

    command = sys.argv[1]

    # 读取 stdin 的 JSON 参数
    try:
        params = json.loads(sys.stdin.read())
    except:
        params = {}

    try:
        if command == "funasr":
            result = funasr_transcribe(
                params["audio_path"],
                params.get("model", "paraformer-zh"),
                params.get("language", "auto"),
                params.get("device", "cuda:0"),
            )
            print(json.dumps({"segments": result}))

        elif command == "whisper":
            result = whisper_transcribe(
                params["audio_path"],
                params.get("model", "medium"),
                params.get("language", "auto"),
                params.get("device", "cuda"),
            )
            print(json.dumps({"segments": result}))

        elif command == "translate":
            result = translate_segments(
                params["segments"],
                params.get("target_lang", "zh"),
                params.get("base_url", "https://api.siliconflow.cn/v1"),
                params.get("api_key", ""),
                params.get("model_name", "Pro/moonshotai/Kimi-K2.5"),
            )
            print(json.dumps({"segments": result}))

        elif command == "download":
            result = download_url(
                params["url"],
                params["output_dir"],
                params.get("subtitle_langs", "zh-Hans,zh-CN,zh,en"),
            )
            print(json.dumps(result))

        elif command == "extract_audio":
            success = extract_audio(params["video_path"], params["output_path"])
            print(json.dumps({"success": success}))

        elif command == "get_duration":
            duration = get_audio_duration(params["audio_path"])
            print(json.dumps({"duration": duration}))

        else:
            print(json.dumps({"error": f"Unknown command: {command}"}))
            sys.exit(1)

    except Exception as e:
        logger.exception("Error executing command")
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
