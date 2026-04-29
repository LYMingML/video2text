#!/usr/bin/env python3
from __future__ import annotations

# ---------------------------------------------------------------------------
# Tesla P4 / Pascal GPU (sm_61) 兼容性补丁
# PyTorch >= 2.4 不再支持 sm_61，需锁定 2.3.x。
# 但 transformers >= 5.3 要求 PyTorch >= 2.4，否则禁用 torch 集成。
# 此补丁让 transformers 检测到兼容版本，绕过此限制。
# ---------------------------------------------------------------------------
import importlib.metadata as _ilm_meta
_ilm_version_orig = _ilm_meta.version
def _patched_version(name):
    if name == 'torch':
        import torch as _t
        v = _t.__version__
        if v.startswith('2.3'):
            return '2.4.0+cu121'
    return _ilm_version_orig(name)
_ilm_meta.version = _patched_version

# PyTorch 2.3.x 的 is_autocast_enabled() 不接受参数，
# 但 transformers >= 5.3 调用 is_autocast_enabled(device_type)。
# 此补丁让旧版 PyTorch 兼容新版调用方式。
import torch as _torch
_is_autocast_orig = _torch.is_autocast_enabled
def _patched_is_autocast_enabled(device_type=None):
    if device_type is not None:
        return _is_autocast_orig()
    return _is_autocast_orig()
_torch.is_autocast_enabled = _patched_is_autocast_enabled

# PyTorch 2.3.x 的 nn.Module 没有 set_submodule 方法（2.5+ 才有），
# 但 transformers 的 bitsandbytes 量化流程需要此方法。
# 此补丁为 nn.Module 添加 set_submodule，使 4-bit/8-bit 量化兼容旧版 PyTorch。
import torch.nn as _nn
if not hasattr(_nn.Module, 'set_submodule'):
    def _set_submodule_backport(self, target, module):
        atoms = target.split('.')
        mod = self
        for i, atom in enumerate(atoms[:-1]):
            if not hasattr(mod, atom):
                raise AttributeError(f"Module has no attribute '{atom}'")
            mod = getattr(mod, atom)
            if not isinstance(mod, _nn.Module):
                raise AttributeError(f"Intermediate attribute '{'.'.join(atoms[:i+1])}' is not a Module")
        last_atom = atoms[-1]
        if not hasattr(mod, last_atom):
            raise AttributeError(f"Module has no attribute '{last_atom}'")
        mod._modules[last_atom] = module
    _nn.Module.set_submodule = _set_submodule_backport


import asyncio
import base64
import queue as _queue_mod
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from core.config import (
    STOP_EVENT,
    TEMP_VIDEO_DIR,
    TEMP_VIDEO_KEEP_COUNT,
    WORKSPACE_DIR,
    _guess_source_lang,
    _has_nvidia_gpu,
    _is_supported_media_path,
    _looks_non_chinese_text,
    _parse_lang_code,
    get_transcribing_video,
    set_transcribing_video,
)
from core.workspace import (
    _cleanup_job_source_media,
    _delete_job_folder,
    _find_duplicate_file,
    _list_job_folders_meta,
    _list_uploaded_videos,
    _load_task_meta,
    _parse_srt_segments,
    _prune_temp_video_dir,
    _resolve_file_prefix,
    _resolve_job_dir_for_input,
    _resolve_current_job,
    _save_task_meta,
    _stage_source_media_to_temp_video,
    _unique_file_path,
)
# main.py 中仍有部分未迁移函数（_do_transcribe_stream, _finalize_plain_text_outputs 等），
# 保留 import 以兼容；长期目标是将这些也迁移到 core/ 后移除此行。
import main as core

from utils.online_models import delete_profile, load_app_settings, load_profiles, save_app_settings, save_profiles, upsert_profile
from utils.subtitle import collect_plain_text, normalize_segments_timeline, save_plain, save_srt, segments_to_plain
from utils.translate import is_ollama_base_url, list_available_models, translate_segments
from core.pipeline import PipelineTask, get_pipeline
from utils.xhs_downloader import (
    is_xiaohongshu_url,
    download_xhs_video,
    get_xhs_client,
    XHSDownloadResult,
)

app = FastAPI(title="video2text-fastapi")
logger = logging.getLogger("video2text-fastapi")


@dataclass
class JobState:
    job_id: str
    status: str = "等待中"
    plain_text: str = ""
    logs: list[str] = field(default_factory=list)
    current_job: str = ""
    current_prefix: str = ""
    zip_bundle: str | None = None
    done: bool = False
    failed: bool = False
    running: bool = False
    progress_pct: int = 0
    eta_seconds: int = 0
    step_label: str = ""
    updated_at: float = field(default_factory=time.time)
    # 队列相关
    video_path: str = ""
    translate_params: dict = field(default_factory=dict)
    display_name: str = ""
    auto_translate: bool = False
    auto_download: bool = False

    def add_log(self, msg: str):
        self.logs.append(msg)
        if len(self.logs) > 500:
            self.logs = self.logs[-350:]


_RUNTIME_LOCK = threading.RLock()  # 可重入锁避免死锁

# 任务注册表
_ALL_JOBS: dict[str, JobState] = {}

# SSE 推送系统
_job_subscribers: dict[str, list[queue.SimpleQueue]] = {}
_last_pushed_pct: dict[str, int] = {}  # job_id → 上次推送的 progress_pct

SUBTITLE_PRIORITY_PRESETS: dict[str, str] = {
    "zh": "zh-Hans,zh-CN,zh,zh.*,yue,zh-HK,en,en.*",
    "none": "",
    "en": "en,en.*,en-US,en-GB",
    "ja": "ja,ja.*",
    "ko": "ko,ko.*",
    "es": "es,es.*",
    "fr": "fr,fr.*",
    "de": "de,de.*",
    "ru": "ru,ru.*",
    "pt": "pt,pt.*",
    "ar": "ar,ar.*",
    "hi": "hi,hi.*",
}

# ---------------------------------------------------------------------------
# 后端显示名 → 类名映射
# ---------------------------------------------------------------------------

def _display_name_to_asr_cls(display_name: str) -> str:
    """将前端显示名映射为后端类名。支持精确匹配和前缀匹配。"""
    try:
        from backends import list_asr_backends, get_asr_backend
        for cls_name in list_asr_backends():
            backend_name = get_asr_backend(cls_name).name
            if backend_name == display_name or display_name.startswith(backend_name):
                return cls_name
    except Exception:
        pass
    return display_name


# ---------------------------------------------------------------------------
# Pipeline 桥接回调：PipelineTask 进度 → JobState 更新 + SSE 推送
# ---------------------------------------------------------------------------

def _pipeline_stage_from_ratio(ratio: float) -> str:
    if ratio <= 0.02:
        return "下载/上传"
    if ratio < 0.1:
        return "预处理"
    if ratio < 0.9:
        return "转录中"
    return "翻译"


def _on_pipeline_status(task_id: str, msg: str):
    """Pipeline 状态回调 → 更新 JobState"""
    job = _ALL_JOBS.get(task_id)
    if not job:
        return
    with _RUNTIME_LOCK:
        if msg == "处理中":
            job.running = True
            job.done = False
        elif msg == "完成" or msg == "全部完成":
            job.running = False
            job.done = True
            if job.current_job and job.current_prefix:
                try:
                    job_dir = core.WORKSPACE_DIR / job.current_job
                    job.zip_bundle = _build_all_bundle(job_dir, job.current_prefix)
                except Exception:
                    job.zip_bundle = str(core.WORKSPACE_DIR / job.current_job / f"{job.current_prefix}.zip")
        elif msg.startswith("失败"):
            job.running = False
            job.failed = True
            job.done = True


def _on_pipeline_log(task_id: str, line: str):
    job = _ALL_JOBS.get(task_id)
    if not job:
        return
    job.add_log(line)
    # 从 Pipeline 日志提取 job_dir 和 file_prefix
    with _RUNTIME_LOCK:
        if line.startswith("[JOB] "):
            job_name = line.replace("[JOB] workspace/", "").strip()
            if job_name:
                job.current_job = job_name
        if line.startswith("[INPUT] ") and not job.current_prefix:
            fname = line.replace("[INPUT] ", "").strip()
            if fname:
                from pathlib import Path as _P
                job.current_prefix = _P(fname).stem


def _on_pipeline_progress(task_id: str, ratio: float, msg: str):
    """Pipeline 进度回调 → 更新 JobState + SSE 推送"""
    job = _ALL_JOBS.get(task_id)
    if not job:
        return
    pct = max(0, min(100, int(ratio * 100)))
    _set_job_progress(job, msg, time.time(), progress_pct=pct, step_label=msg)



    raw = (value or "").strip()
    if not raw:
        return "zh"
    lowered = raw.lower()
    if lowered in {"none", "__none__"}:
        return "none"
    if lowered in SUBTITLE_PRIORITY_PRESETS:
        return lowered

    # 兼容旧格式：若传入的是完整的 --sub-langs 字符串，尽量推断主选项。
    if "zh-hans" in lowered or "zh-cn" in lowered or lowered.startswith("zh"):
        return "zh"
    if lowered.startswith("en"):
        return "en"
    for key in ("ja", "ko", "es", "fr", "de", "ru", "pt", "ar", "hi"):
        if lowered.startswith(key):
            return key
    return "zh"


def _decode_media_base64_to_temp(media_base64: str, filename: str) -> str:
    raw = (media_base64 or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="media_base64 不能为空")

    # 支持 data URI: data:video/mp4;base64,xxx
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1].strip()

    try:
        binary = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="media_base64 非法") from exc

    if not binary:
        raise HTTPException(status_code=400, detail="media_base64 解码后为空")

    name = Path(filename or "media.mp4").name
    if not core._is_supported_media_path(name):
        raise HTTPException(status_code=400, detail="filename 扩展名不受支持")

    core.TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    save_path = core._unique_file_path(core.TEMP_VIDEO_DIR, name)
    save_path.write_bytes(binary)
    core._prune_temp_video_dir()
    return str(save_path)


def _collect_job_outputs(job: JobState) -> tuple[Path, list[str]]:
    if not job.current_job:
        raise HTTPException(status_code=500, detail="任务未生成输出目录")

    job_dir = core.WORKSPACE_DIR / job.current_job
    if not job_dir.exists() or not job_dir.is_dir():
        raise HTTPException(status_code=500, detail="输出目录不存在")

    file_prefix = core._resolve_file_prefix(job_dir, job.current_prefix)
    if not file_prefix:
        raise HTTPException(status_code=500, detail="无法解析输出文件前缀")

    zip_path = Path(job.zip_bundle) if job.zip_bundle else None
    if not zip_path or not zip_path.exists():
        zip_path = Path(_build_all_bundle(job_dir, file_prefix))
        job.zip_bundle = str(zip_path)

    files = [
        p.name
        for p in sorted(job_dir.iterdir(), key=lambda x: x.name.lower())
        if p.is_file() and (
            _is_final_output_file(p.name, file_prefix)
            or p.suffix.lower() == ".zip"
        )
    ]
    return zip_path, files


def _resolve_external_input(payload: dict[str, Any], auto_subtitle_lang: str) -> tuple[str, str | None]:
    source_type = str(payload.get("source_type", "")).strip().lower() or "url"
    subtitle_path: str | None = None

    if source_type == "base64":
        filename = str(payload.get("filename", "media.mp4")).strip() or "media.mp4"
        media_base64 = str(payload.get("media_base64", ""))
        return _decode_media_base64_to_temp(media_base64, filename), None

    if source_type == "history":
        history_video = str(payload.get("history_video", "")).strip()
        media_path = core._resolve_input_path(None, history_video) or ""
        if not media_path:
            raise HTTPException(status_code=400, detail="history_video 无效")
        media = Path(media_path)
        if not media.exists() or not media.is_file():
            raise HTTPException(status_code=400, detail="history_video 对应文件不存在")
        if not core._is_supported_media_path(media_path):
            raise HTTPException(status_code=400, detail="history_video 不是受支持媒体类型")
        return media_path, None

    if source_type == "url":
        url = str(payload.get("url", "")).strip()
        if not url:
            raise HTTPException(status_code=400, detail="url 不能为空")
        dl = api_download_url({"url": url, "auto_subtitle_lang": auto_subtitle_lang})
        filepath = str(dl.get("filepath", "")).strip()
        if not filepath:
            raise HTTPException(status_code=500, detail="URL 下载未返回文件路径")
        media_path = core._resolve_input_path(None, filepath) or ""
        if not media_path:
            raise HTTPException(status_code=500, detail="URL 下载后的媒体文件无效")
        if dl.get("auto_subtitle") and dl.get("subtitle_path"):
            subtitle_abs = Path(__file__).resolve().parent / str(dl.get("subtitle_path"))
            subtitle_path = str(subtitle_abs.resolve())
        return media_path, subtitle_path

    raise HTTPException(status_code=400, detail="source_type 仅支持: base64/url/history")


def _get_job(job_id: str) -> JobState:
    with _RUNTIME_LOCK:
        job = _ALL_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _json_job(job: JobState) -> dict[str, Any]:
    # 翻译参数摘要：后端 + 模型名
    tp = job.translate_params or {}
    backend = tp.get("backend", "")
    model_name = ""
    backend_lower = backend.lower()
    if "funasr" in backend_lower:
        model_name = tp.get("funasr_model", "")
    elif "whisper" in backend_lower:
        model_name = tp.get("whisper_model", "")
    model_info = ""
    if backend:
        model_info = backend
        if model_name:
            model_info += " / " + model_name

    return {
        "job_id": job.job_id,
        "status": job.status,
        "plain_text": job.plain_text,
        "log_text": "\n".join(job.logs),
        "current_job": job.current_job,
        "current_prefix": job.current_prefix,
        "zip_ready": bool(job.zip_bundle and Path(job.zip_bundle).exists()),
        "done": job.done,
        "failed": job.failed,
        "running": job.running,
        "progress_pct": max(0, min(100, int(job.progress_pct))),
        "eta_seconds": max(0, int(job.eta_seconds)),
        "step_label": job.step_label,
        "updated_at": job.updated_at,
        "display_name": job.display_name,
        "model_info": model_info,
        "language": tp.get("language", ""),
        "device": tp.get("device", ""),
        "auto_translate": job.auto_translate,
        "auto_download": job.auto_download,
    }


def _get_queue_status() -> dict:
    """获取队列状态，包含所有任务（按 4 阶段分桶）"""
    with _RUNTIME_LOCK:
        # 清理过旧的已完成任务，最多保留 200 条
        if len(_ALL_JOBS) > 200:
            done_ids = [jid for jid, j in _ALL_JOBS.items() if j.done and not j.running]
            done_ids.sort(key=lambda jid: _ALL_JOBS[jid].updated_at)
            for old_id in done_ids[:len(_ALL_JOBS) - 200]:
                del _ALL_JOBS[old_id]

        all_jobs = sorted(_ALL_JOBS.values(), key=lambda j: j.updated_at, reverse=True)[:50]
        all_json = [_json_job(j) for j in all_jobs]

        # 按 step_label 分桶到 4 个阶段
        download_jobs = []
        extract_jobs = []
        transcribe_jobs = []
        translate_jobs = []
        done_jobs = []

        for j in all_json:
            if j.get("done"):
                done_jobs.append(j)
                continue
            if j.get("running"):
                pct = j.get("progress_pct", 0)
                if pct >= 90:
                    translate_jobs.append(j)
                elif pct >= 10:
                    transcribe_jobs.append(j)
                elif pct >= 2:
                    extract_jobs.append(j)
                else:
                    download_jobs.append(j)
            else:
                download_jobs.append(j)

        # Pipeline 队列深度
        p = get_pipeline()
        queue_depths = {}
        for name, q in [("download", p._download_queue), ("extract", p._extract_queue),
                        ("transcribe", p._transcribe_queue), ("translate", p._translate_queue)]:
            queue_depths[name] = q.qsize()

        return {
            "stages": {
                "download": {"jobs": download_jobs, "label": "下载/上传", "queue_depth": queue_depths["download"]},
                "extract": {"jobs": extract_jobs, "label": "预处理", "queue_depth": queue_depths["extract"]},
                "transcribe": {"jobs": transcribe_jobs, "label": "转录", "queue_depth": queue_depths["transcribe"]},
                "translate": {"jobs": translate_jobs, "label": "翻译", "queue_depth": queue_depths["translate"]},
            },
            "done_jobs": done_jobs,
            "all_jobs": all_json,
            # 兼容旧前端
            "running_job": next((j for j in all_json if j.get("running")), None),
            "transcribe_count": len(download_jobs) + len(extract_jobs) + len(transcribe_jobs) + len(translate_jobs),
            "total_count": len([j for j in all_json if not j.get("done")]),
        }




def _format_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _estimate_pct_from_status(status: str) -> int:
    match = re.search(r"(\d{1,3})%", status)
    if match:
        return max(0, min(100, int(match.group(1))))

    mapping = [
        ("提取 WAV", 5),
        ("读取音频时长", 8),
        ("分片音频", 12),
        ("识别配置", 15),
        ("加载 FunASR 模型", 20),
        ("加载 faster-whisper 模型", 20),
        ("汇总识别结果", 92),
    ]
    for key, pct in mapping:
        if key in status:
            return pct
    return 0


def _decorate_progress(status: str, start_ts: float) -> str:
    # 若状态已含百分比与剩余时间，保持原样。
    if "预计剩余" in status and re.search(r"\d{1,3}%", status):
        return status

    pct = _estimate_pct_from_status(status)
    if pct <= 0 or pct >= 100:
        return status

    elapsed = max(0.001, time.time() - start_ts)
    eta = elapsed * (100 - pct) / max(pct, 1)
    return f"{status}｜总进度 {pct}%｜预计剩余 {_format_hms(eta)}"


def _extract_step_label(status: str) -> str:
    step = str(status or "").split("｜", 1)[0].strip()
    return step or "处理中"


def _set_job_progress(
    job: JobState,
    status: str,
    start_ts: float,
    *,
    progress_pct: int | None = None,
    eta_seconds: float | None = None,
    step_label: str | None = None,
):
    pct = _estimate_pct_from_status(status) if progress_pct is None else int(progress_pct)
    pct = max(0, min(100, pct))

    if eta_seconds is None:
        if 0 < pct < 100:
            elapsed = max(0.001, time.time() - start_ts)
            eta_seconds = elapsed * (100 - pct) / max(pct, 1)
        else:
            eta_seconds = 0

    eta = max(0, int(eta_seconds))
    step = (step_label or _extract_step_label(status)).strip() or "处理中"

    decorated = status
    if 0 < pct < 100 and "预计剩余" not in status:
        decorated = f"{step}｜总进度 {pct}%｜预计剩余 {_format_hms(eta)}"

    job.status = decorated
    job.progress_pct = pct
    job.eta_seconds = eta
    job.step_label = step
    job.updated_at = time.time()
    _notify_sse(job)


# ---------------------------------------------------------------------------
# SSE 推送
# ---------------------------------------------------------------------------

def _notify_sse(job: JobState):
    """通知所有订阅该 job 的 SSE 连接。节流：进度变化 < 1% 且无关键状态变化时跳过。"""
    subscribers = _job_subscribers.get(job.job_id)
    if not subscribers:
        return

    last_pct = _last_pushed_pct.get(job.job_id, -1)
    cur_pct = int(job.progress_pct)
    progress_changed = abs(cur_pct - last_pct) >= 1
    state_changed = job.done or job.failed or not job.running

    if not progress_changed and not state_changed:
        return

    _last_pushed_pct[job.job_id] = cur_pct
    data = _json_job(job)

    for q in subscribers:
        try:
            q.put_nowait(data)
        except Exception:
            pass

    if job.done:
        _last_pushed_pct.pop(job.job_id, None)


def _sse_subscribe(job_id: str) -> _queue_mod.Queue:
    """创建线程安全的 SSE 订阅队列。"""
    q: _queue_mod.Queue = _queue_mod.Queue(maxsize=64)
    _job_subscribers.setdefault(job_id, []).append(q)
    return q


def _sse_unsubscribe(job_id: str, q: _queue_mod.Queue):
    """取消 SSE 订阅。"""
    subs = _job_subscribers.get(job_id)
    if subs and q in subs:
        subs.remove(q)
    if not subs:
        _job_subscribers.pop(job_id, None)
        _last_pushed_pct.pop(job_id, None)


def _cleanup_old_text_files():
    _KEEP_EXTS = {".zip"}
    _TEXT_EXTS = {".srt", ".txt", ".vtt", ".wav"}
    cutoff = time.time() - 86400  # 1 day
    cleaned = 0
    for folder in core.WORKSPACE_DIR.iterdir():
        if not folder.is_dir():
            continue
        for p in list(folder.iterdir()):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in _TEXT_EXTS:
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    cleaned += 1
            except OSError:
                pass
    return cleaned


def _resolve_workspace_folder(folder_name: str) -> Path:
    name = (folder_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="folder_name required")

    root = core.WORKSPACE_DIR.resolve()
    target = (core.WORKSPACE_DIR / name).resolve()
    if root not in target.parents:
        raise HTTPException(status_code=400, detail="invalid folder")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="folder not found")
    return target


def _run_transcribe_worker(
    job: JobState,
    video_path: str,
    backend: str,
    language: str,
    whisper_model: str,
    funasr_model: str,
    device: str,
):
    t0 = time.time()
    try:
        core.STOP_EVENT.clear()
        p = Path(video_path)
        if not p.exists():
            raise RuntimeError(f"输入文件不存在: {video_path}")

        # 统一由核心逻辑决定任务目录，确保 job_dir 在后续流程中始终已初始化。
        job_dir = core._resolve_job_dir_for_input(video_path)
        job_dir.mkdir(parents=True, exist_ok=True)
        orig_name = p.name
        file_prefix = p.stem
        # 清理本目录之前生成的全部文本/字幕/打包文件，避免旧内容混入
        for old in list(job_dir.iterdir()):
            if old.is_file() and old.suffix.lower() in {".srt", ".txt", ".zip"}:
                try:
                    old.unlink()
                except OSError:
                    pass
        core._cleanup_job_source_media(job_dir)

        job.current_job = job_dir.name
        job.current_prefix = file_prefix
        job.add_log(f"[INPUT] {orig_name}")
        job.add_log(f"[JOB] workspace/{job_dir.name}")

        segments: list[tuple[float, float, str]] = []
        for status, partial in core._do_transcribe_stream(
            video_path,
            backend,
            language,
            whisper_model,
            funasr_model,
            file_prefix,
            device,
            job_dir,
            log_cb=job.add_log,
        ):
            segments = partial
            _set_job_progress(job, status, t0)
            job.plain_text = collect_plain_text(segments)

        if core.STOP_EVENT.is_set():
            _set_job_progress(job, "🛑 已停止（未生成字幕文件）", t0, progress_pct=0, eta_seconds=0, step_label="已停止")
            with _RUNTIME_LOCK:
                job.done = True
                job.running = False
            return

        lang_code = core._parse_lang_code(language)
        plain_text = collect_plain_text(segments)
        cleaned_segments = normalize_segments_timeline(segments)
        if not cleaned_segments:
            raise RuntimeError("未识别到有效字幕")

        is_non_zh = lang_code in {"en", "ja", "ko", "es"} or core._looks_non_chinese_text(plain_text)
        source_lang = core._guess_source_lang(lang_code, plain_text) if is_non_zh else "zh"
        core._save_task_meta(
            job_dir,
            {
                "file_prefix": file_prefix,
                "lang_code": lang_code,
                "source_lang": source_lang,
                "is_non_zh": is_non_zh,
            },
        )

        plain_text = plain_text or segments_to_plain(cleaned_segments, normalize=False)
        save_srt(cleaned_segments, str(job_dir / f"{file_prefix}.srt"), normalize=False)

        _, display_plain_text, written_text_files = core._finalize_plain_text_outputs(
            job_dir,
            file_prefix,
            cleaned_segments,
            plain_text,
        )
        for written_name in written_text_files:
            job.add_log(f"[OUT] 文本文件: {written_name}")
        try:
            job.zip_bundle = _build_all_bundle(job_dir, file_prefix)
        except Exception:
            pass

        with _RUNTIME_LOCK:
            job.done = True
            job.running = False
        _set_job_progress(
            job,
            f"✅ 原文识别完成（100%）→ workspace/{job_dir.name}/（点击翻译并选择目标语言）",
            t0,
            progress_pct=100,
            eta_seconds=0,
            step_label="识别完成",
        )
        job.plain_text = display_plain_text
    except Exception as exc:
        with _RUNTIME_LOCK:
            job.failed = True
            job.done = True
            job.running = False
        _set_job_progress(job, f"❌ 转录失败: {exc}", t0, progress_pct=0, eta_seconds=0, step_label="转录失败")
        job.add_log(f"[ERROR] {exc}")


def _normalize_lang_code(lang: str) -> str:
    code = (lang or "zh").strip().lower()
    if code not in {"zh", "en", "ja", "ko", "es", "fr", "de", "ru"}:
        code = "zh"
    return code


def _is_final_output_file(filename: str, file_prefix: str) -> bool:
    allowed = {
        f"{file_prefix}.srt",
        f"{file_prefix}.txt",
    }
    for lang in {"zh", "en", "ja", "ko", "es", "fr", "de", "ru"}:
        allowed.add(f"{file_prefix}.{lang}.srt")
        allowed.add(f"{file_prefix}.{lang}.txt")
    return filename in allowed


def _build_all_bundle(job_dir: Path, file_prefix: str) -> str:
    """将 job_dir 下所有属于该 prefix 的最终输出 .srt/.txt 文件打包为单个 zip，然后删除已打包的源文件。"""
    files = sorted(
        p for p in job_dir.iterdir()
        if p.is_file()
        and _is_final_output_file(p.name, file_prefix)
    )
    if not files:
        raise FileNotFoundError(f"未找到可打包的 srt/txt 文件（prefix={file_prefix}）")
    bundle_path = job_dir / f"{file_prefix}.zip"
    with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return str(bundle_path)


def _pick_downloaded_subtitle(media_path: Path) -> Path | None:
    """优先挑选与媒体同 stem 的自动字幕，偏好中文/粤语，其次英文。"""
    if not media_path.exists():
        return None

    parent = media_path.parent
    stem = media_path.stem
    candidates: list[Path] = []
    for ext in (".srt", ".vtt"):
        candidates.extend(sorted(parent.glob(f"{stem}*.{ext.lstrip('.')}")))

    if not candidates:
        return None

    preferred = [
        "zh-hans", "zh-cn", "zh", "zh-hant", "yue", "en", "eng",
    ]

    def _score(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        lang = ""
        prefix = f"{stem}.".lower()
        if name.startswith(prefix):
            lang = name[len(prefix):]
            if lang.endswith(path.suffix.lower()):
                lang = lang[: -len(path.suffix)]
            lang = lang.strip(".")
        for idx, key in enumerate(preferred):
            if key in lang:
                return (idx, name)
        return (len(preferred), name)

    candidates.sort(key=_score)
    return candidates[0]


def _parse_webvtt_segments(vtt_path: Path) -> list[tuple[float, float, str]]:
    """简化版 WebVTT 解析，仅提取时间轴与文本。"""
    if not vtt_path.exists():
        return []

    text = vtt_path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", text.strip())
    segments: list[tuple[float, float, str]] = []

    def _to_seconds(ts: str) -> float:
        t = ts.strip().replace(",", ".")
        parts = t.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(parts[0])

    for block in blocks:
        lines = [ln.rstrip("\ufeff").strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if lines[0].upper() == "WEBVTT":
            continue

        ts_index = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if ts_index < 0:
            continue
        ts_line = lines[ts_index]
        start_s, end_s = [x.strip() for x in ts_line.split("-->", maxsplit=1)]
        try:
            s = _to_seconds(start_s)
            e = _to_seconds(end_s.split(" ", 1)[0])
        except Exception:
            continue

        content = "\n".join(lines[ts_index + 1:]).strip()
        if not content:
            continue
        segments.append((s, e, content))

    return segments


def _run_subtitle_import_worker(job: JobState, media_path: str, subtitle_path: str):
    t0 = time.time()
    try:
        media = Path(media_path)
        subtitle = Path(subtitle_path)
        if not media.exists():
            raise RuntimeError(f"媒体文件不存在: {media_path}")
        if not subtitle.exists():
            raise RuntimeError(f"字幕文件不存在: {subtitle_path}")

        job_dir = core._resolve_job_dir_for_input(str(media))
        job_dir.mkdir(parents=True, exist_ok=True)
        file_prefix = media.stem

        for old in list(job_dir.iterdir()):
            if old.is_file() and old.suffix.lower() in {".srt", ".txt", ".zip"}:
                try:
                    old.unlink()
                except OSError:
                    pass

        target_srt = job_dir / f"{file_prefix}.srt"
        ext = subtitle.suffix.lower()
        if ext == ".srt":
            shutil.copy2(subtitle, target_srt)
            segments = core._parse_srt_segments(target_srt)
        elif ext == ".vtt":
            segments = _parse_webvtt_segments(subtitle)
            save_srt(segments, str(target_srt), normalize=True)
        else:
            raise RuntimeError(f"不支持的字幕格式: {subtitle.suffix}")

        cleaned_segments = normalize_segments_timeline(segments)
        if not cleaned_segments:
            raise RuntimeError("自动字幕为空或解析失败")

        plain_text = collect_plain_text(cleaned_segments)
        save_plain(cleaned_segments, str(job_dir / f"{file_prefix}.txt"), normalize=False)

        lang_code = "auto"
        source_lang = core._guess_source_lang(lang_code, plain_text)
        core._save_task_meta(
            job_dir,
            {
                "file_prefix": file_prefix,
                "lang_code": lang_code,
                "source_lang": source_lang,
                "is_non_zh": source_lang != "zh",
            },
        )

        job.current_job = job_dir.name
        job.current_prefix = file_prefix
        job.plain_text = plain_text
        job.zip_bundle = _build_all_bundle(job_dir, file_prefix)
        job.add_log(f"[INPUT] 自动字幕: {subtitle.name}")
        job.add_log(f"[JOB] workspace/{job_dir.name}")
        _set_job_progress(
            job,
            f"✅ 已使用平台自动字幕（跳过语音识别）：workspace/{job_dir.name}/",
            t0,
            progress_pct=100,
            eta_seconds=0,
            step_label="自动字幕完成",
        )
        with _RUNTIME_LOCK:
            job.done = True
            job.running = False
    except Exception as exc:
        with _RUNTIME_LOCK:
            job.failed = True
            job.done = True
            job.running = False
        _set_job_progress(job, f"❌ 自动字幕导入失败: {exc}", t0, progress_pct=0, eta_seconds=0, step_label="导入失败")
        job.add_log(f"[ERROR] {exc}")


def _run_translate_worker(job: JobState, profile_name: str, model_name: str, target_lang: str):
    t0 = time.time()
    try:
        if not job.current_job:
            raise RuntimeError("当前任务不存在")

        job_dir = core.WORKSPACE_DIR / job.current_job
        file_prefix = core._resolve_file_prefix(job_dir, job.current_prefix)
        if not file_prefix:
            raise RuntimeError("未找到原文字幕")

        orig_srt = job_dir / f"{file_prefix}.srt"

        segments = core._parse_srt_segments(orig_srt)
        if not segments:
            raise RuntimeError("原文字幕为空")

        profiles, active = load_profiles()
        profile = next((p for p in profiles if p.get("name") == (profile_name or active)), profiles[0])
        use_base_url = str(profile.get("base_url", "")).strip()
        use_api_key = str(profile.get("api_key", "")).strip()
        use_model = (model_name or str(profile.get("default_model", "")).strip()).strip()
        use_target_lang = _normalize_lang_code(target_lang)

        meta = core._load_task_meta(job_dir)
        source_lang = str(meta.get("source_lang") or "auto")

        total = len(segments)
        _set_job_progress(job, "⏳ 翻译准备中", t0, progress_pct=3, step_label="翻译准备")

        def on_translate_progress(completed: int, total_count: int, eta: float):
            pct = int(completed * 100 / max(total_count, 1))
            h = int(eta) // 3600
            m = (int(eta) % 3600) // 60
            s = int(eta) % 60
            _set_job_progress(
                job,
                f"⏳ 翻译进度：{pct}%｜预计剩余 {h:02d}:{m:02d}:{s:02d}",
                t0,
                progress_pct=pct,
                eta_seconds=eta,
                step_label="翻译中",
            )

        translated = translate_segments(
            segments,
            source_lang=source_lang,
            target_lang=use_target_lang,
            log_cb=job.add_log,
            progress_cb=on_translate_progress,
            base_url=use_base_url,
            api_key=use_api_key,
            model_name=use_model,
        )
        job.plain_text = collect_plain_text(translated)

        out_segments = normalize_segments_timeline(translated)
        _set_job_progress(job, "⏳ 翻译结果写入文件", t0, progress_pct=98, step_label="写入翻译文件")
        save_srt(out_segments, str(job_dir / f"{file_prefix}.{use_target_lang}.srt"), normalize=False)
        save_plain(out_segments, str(job_dir / f"{file_prefix}.{use_target_lang}.txt"), normalize=False)

        # 兼容旧下载逻辑：中文目标仍写入 .zh.*
        if use_target_lang == "zh":
            save_srt(out_segments, str(job_dir / f"{file_prefix}.zh.srt"), normalize=False)
            save_plain(out_segments, str(job_dir / f"{file_prefix}.zh.txt"), normalize=False)

        job.zip_bundle = _build_all_bundle(job_dir, file_prefix)
        with _RUNTIME_LOCK:
            job.done = True
            job.running = False
        _set_job_progress(
            job,
            f"✅ 翻译完成（100%，目标语言: {use_target_lang}）：workspace/{job.current_job}/",
            t0,
            progress_pct=100,
            eta_seconds=0,
            step_label="翻译完成",
        )
    except Exception as exc:
        with _RUNTIME_LOCK:
            job.failed = True
            job.done = True
            job.running = False
        _set_job_progress(job, f"❌ 翻译失败: {exc}", t0, progress_pct=0, eta_seconds=0, step_label="翻译失败")
        job.add_log(f"[ERROR] {exc}")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    app_settings = load_app_settings()
    default_backend = app_settings["DEFAULT_BACKEND"]
    default_funasr_model = app_settings["DEFAULT_FUNASR_MODEL"]
    default_whisper_model = app_settings["DEFAULT_WHISPER_MODEL"]
    default_auto_subtitle_lang = app_settings["AUTO_SUBTITLE_LANG"]
    html = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>video2text - FastAPI</title>
<style>
:root{
    --bg-0:#f4f6f4;
    --bg-1:#f8fbf8;
    --bg-card:rgba(255,255,255,0.92);
    --text-0:#1f2a25;
    --text-1:#516157;
    --line:#d7e1da;
    --accent:#2d8f6f;
    --accent-soft:#e8f7f1;
    --danger:#c84a4a;
    --shadow:0 10px 30px rgba(40,70,55,0.08);
    --radius:16px;
}
*{box-sizing:border-box}
body{
    margin:0;
    color:var(--text-0);
    font-family:"Noto Sans SC","Source Han Sans SC","PingFang SC","Microsoft YaHei",sans-serif;
    background:
        radial-gradient(1200px 500px at 12% -10%, #dff1e8 0%, transparent 56%),
        radial-gradient(800px 400px at 100% 0%, #ebf5ec 0%, transparent 48%),
        linear-gradient(160deg, var(--bg-0), var(--bg-1));
}
.shell{
    max-width:1840px;
    margin:0 auto;
    padding:24px 28px 32px;
}
.hero{
    background:var(--bg-card);
    border:1px solid var(--line);
    border-radius:22px;
    padding:20px 22px;
    box-shadow:var(--shadow);
}
.hero h1{
    margin:0;
    font-size:30px;
    letter-spacing:0.2px;
}
.hero p{
    margin:8px 0 0;
    color:var(--text-1);
    font-size:14px;
}
.layout{display:flex;flex-direction:column;gap:12px;margin-top:16px}
.side-nav{
    background:var(--bg-card);
    border:1px solid var(--line);
    border-radius:var(--radius);
    box-shadow:var(--shadow);
    padding:6px 14px;
}
.nav{display:flex;gap:8px;flex-direction:row;align-items:center}
.nav button{
    min-height:36px;
    width:auto;
    text-align:center;
    padding:6px 28px;
    border:1px solid var(--line);
    border-radius:12px;
    background:#ffffff;
    color:var(--text-1);
    font-weight:600;
    cursor:pointer;
    transition:all .2s ease;
    white-space:nowrap;
}
.nav button:hover{transform:translateY(-1px);border-color:#b8cec0}
.nav button.active{background:var(--accent-soft);border-color:#9fd8c3;color:#1c5d47}
.cookie-upload-wrapper{margin-left:auto;display:flex;align-items:center}
.cookie-upload-label{
    min-height:36px;
    padding:6px 16px;
    border:1px dashed var(--line);
    border-radius:12px;
    background:#fff9e6;
    color:#8b6914;
    font-weight:600;
    cursor:pointer;
    transition:all .2s ease;
    white-space:nowrap;
    font-size:14px;
}
.cookie-upload-label:hover{background:#fff3cc;border-color:#d4a800}
.cookie-upload-wrapper input[type="file"]{display:none}
.page{display:none;opacity:0;transform:translateY(6px)}
.page.active{display:block;animation:fadeIn .28s ease forwards}
@keyframes fadeIn{to{opacity:1;transform:translateY(0)}}
.workspace{
    margin-top:16px;
    display:grid;
    grid-template-columns:repeat(12,minmax(0,1fr));
    gap:10px;
}
.card{
    background:var(--bg-card);
    border:1px solid var(--line);
    border-radius:var(--radius);
    box-shadow:var(--shadow);
    padding:12px;
}
.card h3{margin:0 0 12px;font-size:18px}
.card p.muted{margin:0 0 10px;color:var(--text-1);font-size:13px}
.span-6{grid-column:span 6}
.span-5{grid-column:span 5}
.span-7{grid-column:span 7}
.span-12{grid-column:span 12}
.panel-left{grid-column:span 6}
.panel-right{grid-column:span 6;display:flex;flex-direction:column;align-self:stretch}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.toolbar.tight button{min-width:0}
.toolbar-divider{width:1px;height:24px;background:var(--line);margin:0 4px}
.inline-label{display:flex;align-items:center;gap:4px;font-size:13px;color:#31473a}
.inline-label span{line-height:1.2;text-align:right}
.inline-label input{padding:4px 6px;font-size:13px;min-height:auto}
.stack{display:flex;flex-direction:column;gap:8px}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.dense-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;align-items:start}
.tri-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;align-items:start}
.span-all{grid-column:1/-1}
.list-split{display:grid;grid-template-columns:minmax(0,0.9fr) minmax(0,1.1fr);gap:10px;align-items:start}
.download-split{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,0.8fr);gap:10px;align-items:start}
.task-top-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(0,1fr);gap:10px;align-items:stretch}
.summary-panel{
    border:1px solid #d8e8de;
    background:#f7fcf9;
    border-radius:12px;
    padding:10px;
}
.summary-panel h4{margin:0 0 8px;font-size:14px}
.summary-kv{display:grid;grid-template-columns:88px minmax(0,1fr);gap:6px;font-size:12px;align-items:center}
.summary-kv b{color:#345545;font-size:12px}
.summary-kv span{color:#2b4638;font-weight:600;min-width:0;word-break:break-word}
.icon-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;align-items:start}
.icon-item{
    border:1px solid var(--line);
    border-radius:10px;
    background:#fbfefc;
    padding:9px;
    cursor:pointer;
    transition:all .2s ease;
    min-height:66px;
    display:flex;
    flex-direction:column;
    justify-content:space-between;
    gap:6px;
}
.icon-item:hover{border-color:#b8cec0;background:#f1faf5;transform:translateY(-1px)}
.icon-item.selected{border-color:#7dbfa6;background:#eaf8f2;box-shadow:0 0 0 2px rgba(45,143,111,0.1) inset}
.icon-item-title{font-size:12px;font-weight:700;color:#345545;word-break:break-word;line-height:1.3}
.icon-item-meta{font-size:11px;color:#5d7468;line-height:1.2}
/* 多选列表样式 */
.multi-select-list{display:flex;flex-direction:column;gap:4px;max-height:380px;overflow-y:auto;border:1px solid var(--line);border-radius:10px;padding:8px;background:#fbfefc}
.multi-select-item{
    display:flex;align-items:center;gap:8px;
    padding:8px 10px;border:1px solid transparent;border-radius:8px;
    background:#fff;cursor:pointer;transition:all .15s ease;
    user-select:none;
}
.multi-select-item:hover{background:#f1faf5;border-color:#d0e8dc}
.multi-select-item.selected{background:#eaf8f2;border-color:#7dbfa6}
.multi-select-item .checkbox{
    width:18px;height:18px;border:1.5px solid #9ab8a8;border-radius:4px;
    display:flex;align-items:center;justify-content:center;
    background:#fff;flex-shrink:0;transition:all .15s ease;
}
.multi-select-item.selected .checkbox{background:#3aa27f;border-color:#3aa27f}
.multi-select-item.selected .checkbox::after{
    content:'✓';color:#fff;font-size:11px;font-weight:bold;
}
.multi-select-item .item-label{flex:1;font-size:13px;color:#415548;word-break:break-word}
.multi-select-item .item-meta{font-size:11px;color:#7a8f82}
/* 文件表格样式 */
.file-table-container{flex:1;display:flex;flex-direction:column;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff;min-height:130px}
.file-table-header{display:flex;align-items:center;background:#f5f9f7;border-bottom:1px solid var(--line);padding:0 2px;flex-shrink:0}
.file-table-header .file-table-col{padding:3px 4px;font-size:11px;font-weight:700;color:#31473a;line-height:1.1}
.file-table-col.col-check{width:24px;text-align:center;flex-shrink:0}
.file-table-col.col-name{flex:2;min-width:100px}
.file-table-col.col-folder{flex:1;min-width:80px}
.file-table-col.col-time{width:110px;flex-shrink:0}
.file-table-col.col-size{width:80px;flex-shrink:0;text-align:right}
.file-table-col.sortable{cursor:pointer;user-select:none}
.file-table-col.sortable:hover{background:#e8f0ec}
.sort-indicator{margin-left:2px;opacity:0.4}
.sort-indicator.active{opacity:1}
.sort-indicator.asc::after{content:'▲';font-size:9px}
.sort-indicator.desc::after{content:'▼';font-size:9px}
.file-table-body{flex:1;overflow-y:auto}
.file-table-row{display:flex;align-items:center;padding:1px 2px;border-bottom:1px solid #f0f4f2;cursor:pointer;transition:background .15s ease;line-height:1}
.file-table-row:last-child{border-bottom:none}
.file-table-row:hover{background:#f5faf8}
.file-table-row.selected{background:#e8f5ef}
.file-table-row .file-table-col{padding:1px 4px;font-size:12px;color:#415548;line-height:1.1}
.file-table-row .col-name{overflow-wrap:break-word;word-break:break-all;white-space:normal}
.file-table-row .col-time{font-size:11px;color:#6b7f72}
.file-table-row .col-size{font-size:11px;color:#6b7f72;text-align:right}
.file-table-row .row-checkbox{width:12px;height:12px;cursor:pointer}
.settings-section{margin-top:12px;padding:12px;background:#f8fbf9;border-radius:10px;border:1px solid var(--line)}
.settings-section h3{margin:0 0 8px 0;font-size:14px;color:#31473a}
.settings-grid{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
.settings-grid .field.compact{min-width:120px}
.settings-grid input[type="number"]{width:100px}
.model-layout{display:grid;grid-template-columns:minmax(0,0.33fr) minmax(0,0.67fr);gap:10px}
.params-grid-3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.params-grid-2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.field{display:flex;flex-direction:column;gap:6px;min-width:0}
.field.compact > input,
.field.compact > select:not([size]){
    max-width:360px;
}
.field.compact-wide > input,
.field.compact-wide > select:not([size]){
    max-width:460px;
}
label{font-size:13px;font-weight:700;color:#31473a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block;max-width:100%}
input,select,textarea,button{
    font:inherit;
}
input,select,textarea{
    width:100%;
    min-height:36px;
    border:1px solid var(--line);
    border-radius:10px;
    background:#fff;
    padding:9px 11px;
    color:var(--text-0);
    transition:border-color .2s ease, box-shadow .2s ease;
}
textarea{min-height:136px;resize:vertical}
select[size]{min-height:152px;padding:6px 8px}
select[size] option:checked{
    background:linear-gradient(90deg,#d8f1e6,#e8f8f1);
    color:#1e5a45;
    font-weight:700;
}
select.has-selection{
    border-color:#7dbfa6;
    box-shadow:0 0 0 3px rgba(45,143,111,0.14);
}
.selection-hint{
    border:1px solid #cde4d7;
    background:#f3fbf7;
    color:#2f5a48;
    border-radius:10px;
    padding:8px 10px;
    font-size:13px;
    min-height:38px;
    display:flex;
    align-items:center;
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
}
input:focus,select:focus,textarea:focus{
    outline:none;
    border-color:#8dcab4;
    box-shadow:0 0 0 3px rgba(45,143,111,0.13);
}
input[readonly],textarea[readonly]{background:#f8fbf9;color:#516157}
button{
    min-height:40px;
    padding:9px 14px;
    border:1px solid #b8d8c7;
    border-radius:10px;
    background:#f6fffb;
    color:#1c5d47;
    font-weight:700;
    cursor:pointer;
    transition:all .2s ease;
    white-space:nowrap;
}
button:hover{transform:translateY(-1px);background:#ecfaf4}
.btn-danger{color:#8a2626;background:#fff4f4;border-color:#e8c4c4}
.status{font-size:13px}
.status.compact{border:none;background:transparent;padding:0;min-height:auto;font-size:12px;color:#5d7468}
.folder-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px}
.folder-item{padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:#fbfefc;color:#415548;font-size:13px}
.task-panel{
    border:1px solid var(--line);
    background:#fbfffd;
    border-radius:12px;
    padding:12px;
}
.task-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.task-kv{border:1px solid #e1eae4;background:#fff;border-radius:10px;padding:8px 10px}
.task-k{font-size:12px;color:var(--text-1);margin:0}
.task-v{font-size:14px;font-weight:700;margin:3px 0 0}
.task-progress{
    margin-top:10px;
    height:14px;
    border:1px solid #cfe0d6;
    border-radius:999px;
    overflow:hidden;
    background:#eff6f1;
}
.task-progress > i{
    display:block;
    height:100%;
    width:0%;
    background:linear-gradient(90deg,#3aa27f,#2d8f6f);
    transition:width .25s ease;
}
.task-state{font-size:12px;color:var(--text-1)}
.backend-model-select{font-size:12px;line-height:1.3}
.backend-model-select option{padding:3px 4px;white-space:normal}
.content-area{min-width:0}
.drag-zone{display:flex;flex-direction:column;gap:8px}
.drag-row{display:flex;gap:10px;align-items:start}
.drag-item{flex:1;min-width:0;position:relative;border-radius:10px;transition:outline .15s}
.drag-item[draggable="true"]{cursor:grab}
.drag-item.dragging{opacity:.35;outline:2px dashed var(--accent)}
.drag-item.drag-over{outline:2px dashed var(--accent);outline-offset:2px}
.home-action-bar{
    display:flex;
    align-items:center;
    gap:10px;
    flex-wrap:wrap;
}
.home-action-bar .action-spacer{
    flex:1;
    min-width:16px;
}
.home-action-bar .subtitle-lang-input{
    width:auto;
    min-width:fit-content;
    max-width:100%;
    flex:0 0 auto;
}
.home-action-bar .subtitle-lang-label{
    font-size:12px;
    color:var(--text-1);
    white-space:nowrap;
}
.inline-toggle{
    display:flex;
    align-items:center;
    gap:8px;
    min-height:40px;
    padding:0 10px;
    border:1px solid #cfe0d6;
    border-radius:10px;
    background:#fbfffd;
    color:#355646;
    font-size:13px;
    font-weight:600;
    white-space:nowrap;
}
.inline-toggle input{
    margin:0;
}
</style>
</head>
<body>
<div class="shell">
    <header class="hero">
        <h1>视频转字幕工作台</h1>
        <p>轻量、清晰、可持续维护的多页面工作流：主页、文件管理、配置模型。</p>
    </header>

    <div class="layout">
        <aside class="side-nav">
            <div class="nav">
                <button id="btn-home" class="active" onclick="showPage('home')">主页</button>
                <button id="btn-file" onclick="showPage('file')">文件管理</button>
                <button id="btn-model" onclick="showPage('model')">配置模型</button>
                <button id="btn-queue" onclick="showPage('queue')">任务队列</button>
                <div class="cookie-upload-wrapper">
                    <label for="cookieFile" class="cookie-upload-label" title="上传 Cookie 文件（用于需要登录的平台下载）">🍪 Cookie</label>
                    <input type="file" id="cookieFile" accept=".txt" onchange="uploadCookieFile(event)" />
                </div>
            </div>
        </aside>

        <main class="content-area">
    <div id="page-home" class="page active">
        <section class="workspace">
            <article class="card panel-left stack">
                <h3>任务输入与操作</h3>
                <p class="muted">上传新视频，或使用当前视频，按步骤执行转录与翻译。</p>
                <div class="drag-zone" id="dz-home-input">
                    <div class="drag-row">
                        <div class="drag-item toolbar tight home-action-bar" draggable="true" style="flex:1">
                            <button onclick="startTranscribe()">开始转录</button>
                            <button class="btn-danger" onclick="stopJob()">停止</button>
                            <button onclick="startTranslate()">翻译</button>
                            <button onclick="downloadOutputZip()">下载输出文件</button>
                            <label class="inline-toggle" title="转录完成后自动翻译">
                                <input type="checkbox" id="autoTranslate" onchange="saveUiPreferences()" />
                                <span>自动翻译</span>
                            </label>
                            <label class="inline-toggle" title="完成后自动下载 ZIP">
                                <input type="checkbox" id="autoDownload" onchange="saveUiPreferences()" />
                                <span>自动下载</span>
                            </label>
                            <label class="inline-toggle" title="自动保存到浏览器默认下载路径（无需弹窗确认）">
                                <input type="checkbox" id="directSave" onchange="saveUiPreferences()" />
                                <span>直接保存</span>
                            </label>
                            <span class="action-spacer"></span>
                        </div>
                    </div>
                    <div class="drag-row">
                        <div class="drag-item field" draggable="true">
                            <label for="videoFile">上传视频/音频</label>
                            <input type="file" id="videoFile" accept="video/*,audio/*,.mp4,.mkv,.avi,.mov,.wmv,.flv,.webm,.m4v,.ts,.mp3,.wav,.flac,.m4a,.aac,.ogg" />
                        </div>
                        <div class="drag-item field" draggable="true">
                            <label for="historyVideo">当前视频</label>
                            <select id="historyVideo"></select>
                        </div>
                        <div class="drag-item field" draggable="true">
                            <label for="statusText">状态</label>
                            <input id="statusText" class="status" readonly />
                        </div>
                    </div>
                    <div class="drag-row">
                        <div class="drag-item field" draggable="true" style="flex:1">
                            <label for="urlInput">视频URL</label>
                            <input type="url" id="urlInput" placeholder="https://www.youtube.com/watch?v=..." />
                        </div>
                        <div class="drag-item toolbar tight" draggable="true" style="flex:none;align-self:flex-end">
                            <button id="urlDownloadBtn" onclick="downloadVideoUrl()">下载目标URL</button>
                        </div>
                    </div>
                    <div class="drag-row">
                        <div class="drag-item field" draggable="true">
                            <label for="homeBackendSel">识别后端</label>
                            <select id="homeBackendSel" onchange="onHomeBackendChange(); saveUiPreferences()">
                                <option>VibeVoice ASR（长音频+说话人分离）</option>
                                <option>FunASR（Paraformer）</option>
                                <option>faster-whisper（多语言）</option>
                            </select>
                        </div>
                        <div class="drag-item field" draggable="true">
                            <label for="targetLangSel">翻译目标语言</label>
                            <select id="targetLangSel">
                                <option value="zh" selected>简体中文（zh）</option>
                                <option value="en">英语（en）</option>
                                <option value="ja">日语（ja）</option>
                                <option value="ko">韩语（ko）</option>
                                <option value="es">西班牙语（es）</option>
                                <option value="fr">法语（fr）</option>
                                <option value="de">德语（de）</option>
                                <option value="ru">俄语（ru）</option>
                            </select>
                        </div>
                    </div>
                    <div class="drag-row">
                        <div class="drag-item field" draggable="true">
                            <label for="homeBackendModelSel">后端模型（含特性）</label>
                            <input id="homeBackendModelSearch" placeholder="筛选后端模型" oninput="filterBackendModels()" />
                            <select id="homeBackendModelSel" class="backend-model-select" onchange="saveUiPreferences()"></select>
                        </div>
                    </div>
                    <div class="drag-row">
                        <div class="drag-item field" draggable="true">
                            <label for="homeModelSel">翻译模型</label>
                            <input id="homeModelSearch" placeholder="筛选翻译模型" oninput="filterModels('homeModelSearch','homeModelSel')" />
                            <select id="homeModelSel"></select>
                        </div>
                        <div class="drag-item field" draggable="true" style="flex:0 0 auto;max-width:140px">
                            <label for="parallelThreadSel">并行线程</label>
                            <select id="parallelThreadSel">
                                <option value="1">1 线程</option>
                                <option value="3">3 线程</option>
                                <option value="5" selected>5 线程</option>
                                <option value="10">10 线程</option>
                                <option value="20">20 线程</option>
                            </select>
                        </div>
                    </div>
                </div>
                <div id="homeSelectionState" class="selection-hint">当前已选：未选择后端模型与翻译模型</div>
            </article>
            <article class="card panel-right stack">
                <h3>输出结果</h3>
                <div class="task-top-grid">
                    <section class="task-panel" id="taskPanel">
                        <div class="task-grid">
                            <div class="task-kv"><p class="task-k">任务目录</p><p class="task-v" id="taskPanelJob">暂无</p></div>
                            <div class="task-kv"><p class="task-k">当前步骤</p><p class="task-v" id="taskPanelStep">待机</p></div>
                            <div class="task-kv"><p class="task-k">进度</p><p class="task-v" id="taskPanelPct">0%</p></div>
                            <div class="task-kv"><p class="task-k">预计剩余</p><p class="task-v" id="taskPanelEta">00:00:00</p></div>
                        </div>
                        <div class="task-progress"><i id="taskPanelBar"></i></div>
                        <div class="task-state" id="taskPanelState">暂无运行任务</div>
                    </section>
                    <section class="summary-panel">
                        <h4>当前选择摘要</h4>
                        <div class="summary-kv"><b>识别后端</b><span id="homeSummaryBackend">未选择</span></div>
                        <div class="summary-kv"><b>后端模型</b><span id="homeSummaryBackendModel">未选择</span></div>
                        <div class="summary-kv"><b>翻译模型</b><span id="homeSummaryTranslate">未选择</span></div>
                        <div class="summary-kv"><b>当前文件</b><span id="homeSummaryFile">未选择</span></div>
                    </section>
                </div>
                <div class="field" style="flex:1;display:flex;flex-direction:column;min-height:0">
                    <label for="plainText">识别文本</label>
                    <textarea id="plainText" readonly style="flex:1;min-height:0;resize:vertical"></textarea>
                </div>
                <div class="field" style="flex:0 0 auto">
                    <label for="logText">运行日志</label>
                    <textarea id="logText" readonly style="min-height:120px"></textarea>
                </div>
            </article>
        </section>
    </div>

    <div id="page-file" class="page">
        <section class="workspace">
            <article class="card panel-left stack">
                <h3>历史文件夹</h3>
                <p class="muted">统一管理历史任务目录，支持多选删除（Shift/Ctrl）与快速刷新。</p>
                <div class="toolbar" style="flex-wrap:wrap;gap:6px;align-items:center">
                    <button onclick="refreshHistory()">刷新历史</button>
                    <button onclick="toggleSelectAllFolders()">全选/取消</button>
                    <button class="btn-danger" onclick="deleteSelectedFolders()">删除选中</button>
                    <button class="btn-danger" onclick="deleteAllFolders()">删除全部</button>
                    <span class="toolbar-divider"></span>
                    <label class="inline-label" title="临时视频保留数量">
                        <span>临时视频<br/>保留数量</span>
                        <input type="number" id="tempVideoKeepCount" min="1" max="1000" value="5" onchange="saveTempFileSettings()" style="width:50px" />
                    </label>
                    <input id="tempFileStatus" class="status compact" style="width:80px" readonly />
                </div>
                <div class="field compact">
                    <label for="folderSearch">搜索历史文件夹</label>
                    <input id="folderSearch" placeholder="输入关键字筛选文件夹" oninput="applyFolderFilter()" />
                </div>
                <div id="fileFolderSelectionState" class="selection-hint">当前已选：未选择历史文件夹</div>
                <select id="folderSelect" onchange="refreshTextFiles()" style="display:none;"></select>
                <div class="field" style="flex:1;display:flex;flex-direction:column;min-height:0">
                    <label>全部历史文件夹</label>
                    <div class="file-table-container">
                        <div class="file-table-header">
                            <div class="file-table-col col-check"><input type="checkbox" id="folderCheckAll" onclick="toggleSelectAllFolders()"/></div>
                            <div class="file-table-col col-name sortable" onclick="sortFolders('name')">文件名 <span class="sort-indicator" id="folderSortName"></span></div>
                            <div class="file-table-col col-time sortable" onclick="sortFolders('time')">创建时间 <span class="sort-indicator" id="folderSortTime"></span></div>
                            <div class="file-table-col col-size sortable" onclick="sortFolders('size')">目录大小 <span class="sort-indicator" id="folderSortSize"></span></div>
                        </div>
                        <div id="folderListContainer" class="file-table-body"></div>
                    </div>
                </div>
            </article>

            <article class="card panel-right stack">
                <h3>下载输出文件</h3>
                <p class="muted">选中文件夹后浏览文件，或切换到全部文件夹视图查看所有 ZIP</p>
                <div class="toolbar" style="gap:6px;flex-wrap:wrap;align-items:center;">
                    <button id="toggleAllFilesBtn" onclick="toggleAllFilesView()">显示全部文件夹</button>
                    <button onclick="refreshCurrentFileView()">刷新文件列表</button>
                    <button onclick="downloadSelectedTextFiles()">下载选中的文件</button>
                </div>
                <div class="field compact">
                    <label for="textFileSearch">搜索输出文件</label>
                    <input id="textFileSearch" placeholder="输入关键字筛选文件" oninput="applyTextFileFilter()" />
                </div>
                <div id="fileTextSelectionState" class="selection-hint">当前已选：未选择输出文件</div>
                <select id="textFileSelect" style="display:none;"></select>
                <div class="field" style="flex:1;display:flex;flex-direction:column;min-height:0">
                    <label id="textFileListLabel">全部输出文件</label>
                    <div class="file-table-container">
                        <div class="file-table-header">
                            <div class="file-table-col col-check"><input type="checkbox" id="textFileCheckAll" onclick="toggleSelectAllTextFiles()"/></div>
                            <div id="textFileFolderCol" class="file-table-col col-folder" style="display:none;">文件夹 <span class="sort-indicator" id="textFileSortFolder"></span></div>
                            <div class="file-table-col col-name sortable" onclick="sortTextFiles('name')">文件名 <span class="sort-indicator" id="textFileSortName"></span></div>
                            <div class="file-table-col col-time sortable" onclick="sortTextFiles('time')">修改时间 <span class="sort-indicator" id="textFileSortTime"></span></div>
                            <div class="file-table-col col-size">大小</div>
                        </div>
                        <div id="textFileListContainer" class="file-table-body"></div>
                    </div>
                </div>
            </article>
        </section>
    </div>

    <div id="page-model" class="page">
        <section class="workspace model-layout">
            <article class="card stack">
                <h3>配置组列表</h3>
                <p class="muted">左侧切换配置组，右侧统一编辑凭证与模型。</p>
                <div class="field compact">
                    <label for="profileSearch">筛选配置组</label>
                    <input id="profileSearch" placeholder="输入关键字筛选配置组" oninput="applyProfileFilter()" />
                </div>
                <div id="modelSelectionState" class="selection-hint">当前已选：未选择配置组与模型</div>
                <select id="profileSel" onchange="onProfileSelected()" size="10" style="width:100%"></select>
            </article>

            <article class="card stack">
                <h3>在线模型配置</h3>
                <p class="muted">配置将写入 `.env`，支持测试连通性、保存与删除。</p>
                <div class="toolbar">
                    <button onclick="newProfile()">新建配置</button>
                    <button onclick="fetchModels()">测试当前配置</button>
                    <button onclick="saveProfile()">保存配置</button>
                    <button class="btn-danger" onclick="deleteProfile()">删除配置</button>
                </div>
                <div class="field compact-wide">
                    <label for="modelStatus">测试与保存状态</label>
                    <input id="modelStatus" class="status" readonly />
                </div>
                <input id="profileOriginalName" type="hidden" value="" />
                <div class="params-grid-3">
                    <div class="field compact">
                        <label for="backendSel">识别后端</label>
                        <select id="backendSel">
                            <option>VibeVoice ASR（长音频+说话人分离）</option>
                            <option>FunASR（Paraformer）</option>
                            <option>faster-whisper（多语言）</option>
                        </select>
                    </div>
                    <div class="field compact">
                        <label for="langSel">语言</label>
                        <select id="langSel" onchange="saveUiPreferences()">
                            <option>自动检测</option>
                            <option>zh（普通话）</option>
                            <option>yue（粤语）</option>
                            <option>en（英语）</option>
                            <option>ja（日语）</option>
                            <option>ko（韩语）</option>
                            <option>es（西班牙语）</option>
                        </select>
                    </div>
                    <div class="field compact">
                        <label for="deviceSel">推理设备</label>
                        <select id="deviceSel" onchange="saveUiPreferences()">
                            <option>CUDA</option>
                            <option>CPU</option>
                        </select>
                    </div>
                </div>
                <div class="params-grid-2">
                    <div class="field compact-wide">
                        <label for="funasrModel">FunASR 模型</label>
                        <select id="funasrModel">
                            <option value="paraformer-zh ⭐ 普通话精度推荐">paraformer-zh（普通话精度推荐）</option>
                            <option value="paraformer">paraformer（普通话全量）</option>
                            <option value="paraformer-zh-streaming">paraformer-zh-streaming（流式）</option>
                            <option value="paraformer-zh-spk">paraformer-zh-spk（说话人分离）</option>
                            <option value="paraformer-en">paraformer-en（英文优化）</option>
                            <option value="paraformer-en-spk">paraformer-en-spk（英文说话人分离）</option>
                            <option value="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch">seaco-paraformer-large（中文推荐）</option>
                            <option value="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online">speech_paraformer-large-online（中文流式）</option>
                            <option value="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch">speech_paraformer-large-vad-punc（中文全路径）</option>
                            <option value="iic/speech_paraformer-large-vad-punc_asr_nat-en-16k-common-vocab10020">speech_paraformer-large-vad-punc（英文）</option>
                            <option value="iic/SenseVoiceSmall">SenseVoiceSmall（中粤英日韩）</option>
                            <option value="iic/SenseVoice-Small">SenseVoice-Small（多语言备用）</option>
                            <option value="EfficientParaformer-large-zh">EfficientParaformer-large-zh（长语音）</option>
                            <option value="EfficientParaformer-zh-en">EfficientParaformer-zh-en（中英双语）</option>
                        </select>
                    </div>
                    <div class="field compact">
                        <label for="whisperModel">Whisper 模型</label>
                        <select id="whisperModel">
                            <option value="tiny">tiny（速度优先）</option>
                            <option value="base">base</option>
                            <option value="small">small</option>
                            <option value="medium" selected>medium（平衡推荐）</option>
                            <option value="large-v3">large-v3（精度优先）</option>
                        </select>
                    </div>
                </div>
                <div class="form-grid">
                    <div class="field compact">
                        <label for="profileName">配置名称</label>
                        <input id="profileName" />
                    </div>
                    <div class="field compact-wide">
                        <label for="baseUrl">base_url</label>
                        <input id="baseUrl" placeholder="https://api.siliconflow.cn/v1" />
                    </div>
                    <div class="field compact-wide">
                        <label for="apiKey">api_key</label>
                        <input id="apiKey" type="password" placeholder="sk-..." />
                    </div>
                    <div class="field compact-wide">
                        <label for="modelSearch">搜索在线模型</label>
                        <input id="modelSearch" placeholder="输入关键字筛选模型" oninput="filterModels('modelSearch','modelSel')" />
                    </div>
                    <div class="field span-all">
                        <label for="modelSel">在线模型（可设默认）</label>
                        <select id="modelSel" size="7"></select>
                    </div>
                </div>
            </article>
        </section>
    </div>

    <!-- 任务队列页面 -->
    <div id="page-queue" class="page">
        <section class="workspace">
            <article class="card span-12 stack">
                <h3>任务队列管理</h3>
                <p class="muted">查看所有任务的执行状态、文件和模型信息。</p>
                <div class="toolbar">
                    <button onclick="refreshQueueStatus()">刷新状态</button>
                </div>
                <div class="task-panel" style="margin-top:12px">
                    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px">
                        <div>
                            <h4 style="margin:0 0 6px;font-size:13px;color:#5b9bd5">下载/上传 <span id="stageCountDownload">(0)</span></h4>
                            <div id="stageDownload" style="min-height:60px;max-height:300px;overflow-y:auto;border:1px solid #e0e0e0;border-radius:6px;padding:6px">
                                <div class="selection-hint" style="padding:8px;text-align:center;font-size:11px;color:#aaa">暂无</div>
                            </div>
                        </div>
                        <div>
                            <h4 style="margin:0 0 6px;font-size:13px;color:#f0ad4e">预处理 <span id="stageCountExtract">(0)</span></h4>
                            <div id="stageExtract" style="min-height:60px;max-height:300px;overflow-y:auto;border:1px solid #e0e0e0;border-radius:6px;padding:6px">
                                <div class="selection-hint" style="padding:8px;text-align:center;font-size:11px;color:#aaa">暂无</div>
                            </div>
                        </div>
                        <div>
                            <h4 style="margin:0 0 6px;font-size:13px;color:#2abfa6">转录 <span id="stageCountTranscribe">(0)</span></h4>
                            <div id="stageTranscribe" style="min-height:60px;max-height:300px;overflow-y:auto;border:1px solid #e0e0e0;border-radius:6px;padding:6px">
                                <div class="selection-hint" style="padding:8px;text-align:center;font-size:11px;color:#aaa">暂无</div>
                            </div>
                        </div>
                        <div>
                            <h4 style="margin:0 0 6px;font-size:13px;color:#9b59b6">翻译 <span id="stageCountTranslate">(0)</span></h4>
                            <div id="stageTranslate" style="min-height:60px;max-height:300px;overflow-y:auto;border:1px solid #e0e0e0;border-radius:6px;padding:6px">
                                <div class="selection-hint" style="padding:8px;text-align:center;font-size:11px;color:#aaa">暂无</div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="task-panel" style="margin-top:16px">
                    <h4 style="margin:0 0 8px;font-size:13px">已完成</h4>
                    <div id="allJobsList" style="max-height:400px;overflow-y:auto">
                        <div class="selection-hint" style="padding:10px;text-align:center;font-size:12px">暂无任务记录</div>
                    </div>
                </div>
            </article>
        </section>
    </div>
        </main>
    </div>
</div>

<script>
const APP_DEFAULTS = {
    backend: __APP_DEFAULT_BACKEND__,
    funasrModel: __APP_DEFAULT_FUNASR_MODEL__,
    whisperModel: __APP_DEFAULT_WHISPER_MODEL__,
    autoSubtitleLang: __APP_DEFAULT_AUTO_SUBTITLE_LANG__
};

let currentJobId = "";
let pollTimer = null;
let eventSource = null;
let _prevJobState = { current_job:'', step_label:'', done:false };
let pendingAutoFlags = { download: false };
let foldersList = [];
let foldersListMeta = [];  // 文件夹元数据 [{name, mtime, size}]
let selectedFolders = new Set();
let lastClickedFolder = null;
let folderSortBy = 'time';  // 'name' | 'time' | 'size'
let folderSortOrder = 'desc';  // 'asc' | 'desc'
let textFilesMeta = [];
let selectedTextFiles = new Set();
let lastClickedTextFile = null;
let textFileSortBy = 'time';  // 'name' | 'time'
let textFileSortOrder = 'desc';  // 'asc' | 'desc'
let profileNamesList = [];
let folderFilterKeyword = '';
let textFileFilterKeyword = '';
let profileFilterKeyword = '';
let showAllFoldersFiles = false;  // 全部文件夹视图开关
let allFilesMeta = [];           // 全部文件夹文件元数据 [{folder, name, mtime, size}]
let lastSavedAutoSubtitleLang = APP_DEFAULTS.autoSubtitleLang;
const HOME_FUNASR_MODELS = [
    {value:'paraformer-zh', name:'paraformer-zh', feature:'普通话精度推荐'},
    {value:'paraformer', name:'paraformer', feature:'普通话全量'},
    {value:'paraformer-zh-streaming', name:'paraformer-zh-streaming', feature:'流式低延迟'},
    {value:'paraformer-zh-spk', name:'paraformer-zh-spk', feature:'说话人分离', speaker:true},
    {value:'paraformer-en', name:'paraformer-en', feature:'英文优化'},
    {value:'paraformer-en-spk', name:'paraformer-en-spk', feature:'英文说话人分离', speaker:true},
    {value:'iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch', name:'seaco-paraformer-large', feature:'中文高精度'},
    {value:'iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online', name:'speech_paraformer-large-online', feature:'中文流式'},
    {value:'iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch', name:'speech_paraformer-large-vad-punc', feature:'中文VAD+标点'},
    {value:'iic/speech_paraformer-large-vad-punc_asr_nat-en-16k-common-vocab10020', name:'speech_paraformer-large-vad-punc-en', feature:'英文VAD+标点'},
    {value:'iic/SenseVoiceSmall', name:'SenseVoiceSmall', feature:'中粤英日韩多语'},
    {value:'iic/SenseVoice-Small', name:'SenseVoice-Small', feature:'多语言备用'},
    {value:'EfficientParaformer-large-zh', name:'EfficientParaformer-large-zh', feature:'长语音友好'},
    {value:'EfficientParaformer-zh-en', name:'EfficientParaformer-zh-en', feature:'中英双语'},
];
const HOME_WHISPER_MODELS = [
    {value:'tiny', name:'tiny', feature:'极速'},
    {value:'base', name:'base', feature:'轻量'},
    {value:'small', name:'small', feature:'均衡'},
    {value:'medium', name:'medium', feature:'高质量推荐'},
    {value:'large-v3', name:'large-v3', feature:'精度优先'},
];
const HOME_VIBEVOICE_MODELS = [
    {value:'bezzam/VibeVoice-ASR-7B::4', name:'VibeVoice-ASR-7B · 4-bit', feature:'~5GB显存，8GB显卡推荐'},
    {value:'bezzam/VibeVoice-ASR-7B::8', name:'VibeVoice-ASR-7B · 8-bit', feature:'~8GB显存，较高质量'},
    {value:'microsoft/VibeVoice-ASR-HF::4', name:'VibeVoice-ASR-9B · 4-bit', feature:'~9GB显存，高精度+说话人分离'},
    {value:'microsoft/VibeVoice-ASR-HF::8', name:'VibeVoice-ASR-9B · 8-bit', feature:'~16GB显存，最高质量'},
];

function toHms(seconds){
    const sec = Math.max(0, Number(seconds || 0));
    const total = Math.floor(sec);
    const h = String(Math.floor(total / 3600)).padStart(2, '0');
    const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
    const s = String(total % 60).padStart(2, '0');
    return `${h}:${m}:${s}`;
}

function formatRelativeTime(ts){
    const num = Number(ts || 0);
    if(!num) return '未知时间';
    const now = Date.now() / 1000;
    const delta = Math.max(0, Math.floor(now - num));
    if(delta < 60) return `${delta}秒前`;
    if(delta < 3600) return `${Math.floor(delta / 60)}分钟前`;
    if(delta < 86400) return `${Math.floor(delta / 3600)}小时前`;
    return `${Math.floor(delta / 86400)}天前`;
}

function formatAbsoluteTime(ts){
    const num = Number(ts || 0);
    if(!num) return '未知时间';
    const d = new Date(num * 1000);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    return `${y}-${m}-${day} ${hh}:${mm}`;
}

function setHiddenSelectOptions(selectId, values, preferred=''){
    const select = document.getElementById(selectId);
    if(!select) return '';
    const list = [...new Set((values || []).map(v=>String(v || '').trim()).filter(Boolean))];
    const oldValue = select.value;
    select.innerHTML = '';
    list.forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=v; select.appendChild(o); });
    if(list.length){
        if(preferred && list.includes(preferred)){
            select.value = preferred;
        }else if(oldValue && list.includes(oldValue)){
            select.value = oldValue;
        }else{
            select.value = list[0];
        }
        return select.value;
    }
    select.value = '';
    return '';
}

function normalizeSubtitlePriority(value){
    const raw = String(value || '').trim();
    if(!raw){ return 'zh'; }
    const v = raw.toLowerCase();
    if(v === 'none' || v === '__none__'){ return 'none'; }
    if(v === 'zh' || v.startsWith('zh-') || raw.includes('zh-Hans') || raw.includes('zh-CN')){ return 'zh'; }
    if(v === 'en' || raw.includes('en-US') || raw.includes('en-GB')){ return 'en'; }
    const shortlist = ['ja','ko','es','fr','de','ru','pt','ar','hi'];
    if(shortlist.includes(v)){ return v; }
    return 'zh';
}

async function onAutoSubtitleLangChanged(){
    const sel = document.getElementById('autoSubtitleLangs');
    if(!sel) return;
    const value = normalizeSubtitlePriority(sel.value);
    if(value === lastSavedAutoSubtitleLang){
        return;
    }
    try{
        await api('/api/app-settings/subtitle-priority', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({auto_subtitle_lang: value})
        });
        lastSavedAutoSubtitleLang = value;
        APP_DEFAULTS.autoSubtitleLang = value;
    }catch(e){
        console.error(e);
    }
}

async function uploadCookieFile(event){
    const file = event.target.files[0];
    if(!file) return;
    const formData = new FormData();
    formData.append('cookie_file', file);
    try{
        const resp = await fetch('/api/upload_cookie', {method:'POST', body: formData});
        if(!resp.ok){
            const err = await resp.json();
            throw new Error(err.detail || '上传失败');
        }
        const data = await resp.json();
        alert('✅ Cookie 文件已上传：' + data.filename);
        document.getElementById('statusText').value = '✅ Cookie 已上传：' + data.filename;
    }catch(e){
        alert('❌ 上传 Cookie 失败：' + e.message);
    }
    event.target.value = '';
}

function renderIconGrid(containerId, items, selectedValue, onPick, keyword){
    const container = document.getElementById(containerId);
    if(!container) return;
    const kw = String(keyword || '').trim().toLowerCase();
    const filtered = (items || []).filter(item=>{
        const hay = `${item.value || ''} ${item.label || ''} ${item.meta || ''}`.toLowerCase();
        return !kw || hay.includes(kw);
    });

    container.innerHTML = '';
    if(!filtered.length){
        const empty = document.createElement('div');
        empty.className = 'selection-hint';
        empty.textContent = '暂无匹配项';
        container.appendChild(empty);
        return;
    }

    filtered.forEach(item=>{
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'icon-item' + (item.value === selectedValue ? ' selected' : '');
        if(item.title){ card.title = item.title; }
        const title = document.createElement('div');
        title.className = 'icon-item-title';
        title.textContent = item.label || item.value || '';
        card.appendChild(title);
        if(item.meta){
            const meta = document.createElement('div');
            meta.className = 'icon-item-meta';
            meta.textContent = item.meta;
            card.appendChild(meta);
        }
        card.onclick = ()=>onPick(item.value);
        container.appendChild(card);
    });
}

function applyFolderFilter(){
    folderFilterKeyword = (document.getElementById('folderSearch')?.value || '').trim().toLowerCase();
    renderFolderList();
    updateFolderSelectionState();
}

function sortFolders(by){
    if(folderSortBy === by){
        folderSortOrder = folderSortOrder === 'asc' ? 'desc' : 'asc';
    } else {
        folderSortBy = by;
        folderSortOrder = by === 'name' ? 'asc' : 'desc';
    }
    updateFolderSortIndicators();
    renderFolderList();
}

function updateFolderSortIndicators(){
    ['name', 'time', 'size'].forEach(col => {
        const el = document.getElementById('folderSort' + col.charAt(0).toUpperCase() + col.slice(1));
        if(el){
            el.className = 'sort-indicator';
            if(folderSortBy === col){
                el.classList.add('active', folderSortOrder);
            }
        }
    });
}

function renderFolderList(){
    const container = document.getElementById('folderListContainer');
    if(!container) return;
    const kw = folderFilterKeyword.toLowerCase();

    // 创建元数据映射
    const metaMap = new Map();
    foldersListMeta.forEach(f => {
        metaMap.set(f.name, { mtime: f.mtime, size: f.size });
    });

    // 过滤
    let filtered = foldersList.filter(name => !kw || name.toLowerCase().includes(kw));

    const filteredWithMeta = filtered.map(name => ({
        name,
        meta: metaMap.get(name) || { mtime: 0, size: 0 }
    }));

    // 排序
    const mult = folderSortOrder === 'asc' ? 1 : -1;
    if(folderSortBy === 'time'){
        filteredWithMeta.sort((a, b) => mult * ((a.meta?.mtime || 0) - (b.meta?.mtime || 0)));
    } else if(folderSortBy === 'size'){
        filteredWithMeta.sort((a, b) => mult * ((a.meta?.size || 0) - (b.meta?.size || 0)));
    } else {
        filteredWithMeta.sort((a, b) => mult * a.name.localeCompare(b.name, 'zh-Hans-CN'));
    }

    // 更新全选复选框状态
    const checkAll = document.getElementById('folderCheckAll');
    if(checkAll){
        checkAll.checked = filteredWithMeta.length > 0 && filteredWithMeta.every(item => selectedFolders.has(item.name));
    }

    container.innerHTML = '';
    if(!filteredWithMeta.length){
        const empty = document.createElement('div');
        empty.className = 'selection-hint';
        empty.style.padding = '20px';
        empty.style.textAlign = 'center';
        empty.textContent = '暂无匹配项';
        container.appendChild(empty);
        return;
    }

    filteredWithMeta.forEach((item, index) => {
        const folderName = item.name;
        const meta = item.meta;

        const row = document.createElement('div');
        row.className = 'file-table-row' + (selectedFolders.has(folderName) ? ' selected' : '');
        row.dataset.value = folderName;
        row.dataset.index = index;

        // 复选框列
        const checkCol = document.createElement('div');
        checkCol.className = 'file-table-col col-check';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'row-checkbox';
        checkbox.checked = selectedFolders.has(folderName);
        checkbox.onclick = (e) => e.stopPropagation();
        checkbox.onchange = (e) => {
            if(e.target.checked){
                selectedFolders.add(folderName);
            } else {
                selectedFolders.delete(folderName);
            }
            renderFolderList();
            updateFolderSelectionState();
        };
        checkCol.appendChild(checkbox);

        // 文件名列
        const nameCol = document.createElement('div');
        nameCol.className = 'file-table-col col-name';
        nameCol.textContent = folderName;
        nameCol.title = folderName;

        // 时间列
        const timeCol = document.createElement('div');
        timeCol.className = 'file-table-col col-time';
        timeCol.textContent = formatDateTime(meta?.mtime || 0);

        // 大小列
        const sizeCol = document.createElement('div');
        sizeCol.className = 'file-table-col col-size';
        sizeCol.textContent = formatSize(meta?.size || 0);

        row.appendChild(checkCol);
        row.appendChild(nameCol);
        row.appendChild(timeCol);
        row.appendChild(sizeCol);

        row.onclick = (e) => handleFolderClick(folderName, e, index);
        container.appendChild(row);
    });

    updateFolderSortIndicators();
}

function formatSize(sizeMb){
    if(!sizeMb || sizeMb <= 0) return '';
    if(sizeMb < 1) return `${(sizeMb * 1024).toFixed(0)} KB`;
    else if(sizeMb < 1024) return `${sizeMb.toFixed(1)} MB`;
    else return `${(sizeMb / 1024).toFixed(2)} GB`;
}
function formatDateTime(timestamp){
    if(!timestamp) return '';
    const date = new Date(timestamp * 1000);
    const now = new Date();
    const diff = Math.floor((now - date) / 1000);
    if(diff < 60) return '刚刚';
    else if(diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    else if(diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    else if(diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
    else {
        const y = date.getFullYear();
        const month = date.getMonth() + 1;
        const day = date.getDate();
        const hh = String(date.getHours()).padStart(2, '0');
        const mm = String(date.getMinutes()).padStart(2, '0');
        return `${y}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')} ${hh}:${mm}`;
    }
}

function handleFolderClick(folderName, event, index){
    const ctrl = event.ctrlKey || event.metaKey;
    const shift = event.shiftKey;

    if(shift && typeof lastClickedFolder === 'number' && lastClickedFolder >= 0){
        // Shift 多选：选中从上次点击到当前的所有项（基于显示顺序）
        const start = Math.min(lastClickedFolder, index);
        const end = Math.max(lastClickedFolder, index);
        const container = document.getElementById('folderListContainer');
        if(container){
            const rows = container.querySelectorAll('.file-table-row');
            for(let i = start; i <= end && i < rows.length; i++){
                selectedFolders.add(rows[i].dataset.value);
            }
        }
    } else if(ctrl){
        // Ctrl 切换单个选中状态
        if(selectedFolders.has(folderName)){
            selectedFolders.delete(folderName);
        } else {
            selectedFolders.add(folderName);
        }
    } else {
        // 普通点击：单选或取消
        if(selectedFolders.has(folderName) && selectedFolders.size === 1){
            selectedFolders.clear();
        } else {
            selectedFolders.clear();
            selectedFolders.add(folderName);
        }
    }

    lastClickedFolder = index;
    renderFolderList();
    updateFolderSelectionState();

    // 同步到隐藏的 select（用于下载等功能）
    const select = document.getElementById('folderSelect');
    if(select && selectedFolders.size === 1){
        select.value = [...selectedFolders][0];
        select.dispatchEvent(new Event('change', {bubbles:true}));
    }
}

function updateFolderSelectionState(){
    const stateEl = document.getElementById('fileFolderSelectionState');
    if(!stateEl) return;
    if(selectedFolders.size === 0){
        stateEl.textContent = '当前已选：未选择历史文件夹';
    } else if(selectedFolders.size === 1){
        stateEl.textContent = `当前已选：${[...selectedFolders][0]}`;
    } else {
        stateEl.textContent = `当前已选 ${selectedFolders.size} 个文件夹`;
    }
}

function toggleSelectAllFolders(){
    const kw = folderFilterKeyword.toLowerCase();
    const filtered = foldersList.filter(name => !kw || name.toLowerCase().includes(kw));
    const allSelected = filtered.every(name => selectedFolders.has(name));

    if(allSelected){
        filtered.forEach(name => selectedFolders.delete(name));
    } else {
        filtered.forEach(name => selectedFolders.add(name));
    }
    renderFolderList();
    updateFolderSelectionState();
}

async function deleteSelectedFolders(){
    if(selectedFolders.size === 0){
        alert('请先选择要删除的文件夹');
        return;
    }
    const count = selectedFolders.size;
    if(!confirm(`确定要删除选中的 ${count} 个文件夹吗？此操作不可恢复。`)){
        return;
    }

    const folders = [...selectedFolders];
    try{
        const data = await api('/api/folders/delete-batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({folder_names: folders})
        });
        alert(data.message || `成功删除 ${data.deleted_count || count} 个文件夹`);
    }catch(e){
        alert('删除失败: ' + e.message);
    }

    // 无论成功与否，都清除相关状态并刷新
    selectedFolders.clear();
    lastClickedFolder = null;
    textFilesMeta = [];
    selectedTextFiles.clear();
    lastClickedTextFile = null;
    await refreshHistory();
}

async function deleteAllFolders(){
    if(foldersList.length === 0){
        alert('没有可删除的文件夹');
        return;
    }
    const count = foldersList.length;
    if(!confirm(`确定要删除全部 ${count} 个文件夹吗？此操作不可恢复！`)){
        return;
    }
    if(!confirm(`再次确认：将删除所有 ${count} 个历史文件夹，此操作不可恢复！`)){
        return;
    }

    // 保存当前列表副本，用于删除
    const toDelete = [...foldersList];

    try{
        const data = await api('/api/folders/delete-batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({folder_names: toDelete})
        });
        alert(data.message || `成功删除 ${data.deleted_count || count} 个文件夹`);
    }catch(e){
        alert('删除失败: ' + e.message);
    }

    // 无论成功与否，都清除所有相关状态并刷新
    selectedFolders.clear();
    lastClickedFolder = null;
    foldersList = [];
    foldersListMeta = [];
    textFilesMeta = [];
    selectedTextFiles.clear();
    lastClickedTextFile = null;

    // 重新获取最新列表
    await refreshHistory();
}

function sortTextFiles(by){
    if(textFileSortBy === by){
        textFileSortOrder = textFileSortOrder === 'asc' ? 'desc' : 'asc';
    } else {
        textFileSortBy = by;
        textFileSortOrder = by === 'name' ? 'asc' : 'desc';
    }
    updateTextFileSortIndicators();
    renderTextFileList();
}

function updateTextFileSortIndicators(){
    ['name', 'time'].forEach(col => {
        const el = document.getElementById('textFileSort' + col.charAt(0).toUpperCase() + col.slice(1));
        if(el){
            el.className = 'sort-indicator';
            if(textFileSortBy === col){
                el.classList.add('active', textFileSortOrder);
            }
        }
    });
}

function applyTextFileFilter(){
    textFileFilterKeyword = (document.getElementById('textFileSearch')?.value || '').trim().toLowerCase();
    renderTextFileList();
    updateTextFileSelectionState();
}

function renderTextFileList(){
    const container = document.getElementById('textFileListContainer');
    if(!container) return;
    const kw = textFileFilterKeyword.toLowerCase();

    // 过滤
    let filtered = textFilesMeta.filter(item => !kw || item.name.toLowerCase().includes(kw));

    // 排序
    const mult = textFileSortOrder === 'asc' ? 1 : -1;
    if(textFileSortBy === 'time'){
        filtered.sort((a, b) => mult * ((a.mtime || 0) - (b.mtime || 0)));
    } else {
        filtered.sort((a, b) => mult * a.name.localeCompare(b.name, 'zh-Hans-CN'));
    }

    // 更新全选复选框状态
    const checkAll = document.getElementById('textFileCheckAll');
    if(checkAll){
        checkAll.checked = filtered.length > 0 && filtered.every(item => selectedTextFiles.has(item.name));
    }

    container.innerHTML = '';
    if(!filtered.length){
        const empty = document.createElement('div');
        empty.className = 'selection-hint';
        empty.style.padding = '20px';
        empty.style.textAlign = 'center';
        empty.textContent = '暂无匹配项';
        container.appendChild(empty);
        return;
    }

    filtered.forEach((item, index) => {
        const fileName = item.name;
        const folderName = item.folder || '';
        const fileKey = folderName ? (folderName + '/' + fileName) : fileName;
        const mtime = item.mtime || 0;

        const row = document.createElement('div');
        row.className = 'file-table-row' + (selectedTextFiles.has(fileKey)?' selected' : '');
        row.dataset.value = fileKey;
        row.dataset.folder = folderName;
        row.dataset.name = fileName;
        row.dataset.index = index;

        // 复选框列
        const checkCol = document.createElement('div');
        checkCol.className = 'file-table-col col-check';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'row-checkbox';
        checkbox.checked = selectedTextFiles.has(fileKey);
        checkbox.onclick = (e) => e.stopPropagation();
        checkbox.onchange = (e) => {
            if(e.target.checked){
                selectedTextFiles.add(fileKey);
            } else {
                selectedTextFiles.delete(fileKey);
            }
            renderTextFileList();
            updateTextFileSelectionState();
        };
        checkCol.appendChild(checkbox);

        // 文件夹列（仅全部视图显示）
        if(showAllFoldersFiles && folderName){
            const folderCol = document.createElement('div');
            folderCol.className = 'file-table-col col-name';
            folderCol.style.cssText = 'flex:1;font-size:12px;color:#888;min-width:0;';
            folderCol.textContent = folderName;
            folderCol.title = folderName;
            row.appendChild(folderCol);
        }

        // 文件名列
        const nameCol = document.createElement('div');
        nameCol.className = 'file-table-col col-name';
        nameCol.textContent = fileName;
        nameCol.title = fileName;

        // 时间列
        const timeCol = document.createElement('div');
        timeCol.className = 'file-table-col col-time';
        timeCol.textContent = formatDateTime(mtime);

        // 大小列
        const sizeCol = document.createElement('div');
        sizeCol.className = 'file-table-col col-size';
        sizeCol.textContent = formatSize(item.size || 0);

        row.appendChild(checkCol);
        row.appendChild(nameCol);
        row.appendChild(timeCol);
        row.appendChild(sizeCol);

        row.onclick = (e) => handleTextFileClick(fileName, e, index);
        container.appendChild(row);
    });

    updateTextFileSortIndicators();
}

function handleTextFileClick(fileKey, event, index){
    const ctrl = event.ctrlKey || event.metaKey;
    const shift = event.shiftKey;

    if(shift && typeof lastClickedTextFile === 'number' && lastClickedTextFile >= 0){
        // Shift 多选
        const start = Math.min(lastClickedTextFile, index);
        const end = Math.max(lastClickedTextFile, index);
        const container = document.getElementById('textFileListContainer');
        if(container){
            const rows = container.querySelectorAll('.file-table-row');
            for(let i = start; i <= end && i < rows.length; i++){
                selectedTextFiles.add(rows[i].dataset.value);
            }
        }
    } else if(ctrl){
        // Ctrl 切换
        if(selectedTextFiles.has(fileKey)){
            selectedTextFiles.delete(fileKey);
        } else {
            selectedTextFiles.add(fileKey);
        }
    } else {
        // 普通点击
        if(selectedTextFiles.has(fileKey) && selectedTextFiles.size === 1){
            selectedTextFiles.clear();
        } else {
            selectedTextFiles.clear();
            selectedTextFiles.add(fileKey);
        }
    }

    lastClickedTextFile = index;
    renderTextFileList();
    updateTextFileSelectionState();

    // 同步到隐藏的 select
    const select = document.getElementById('textFileSelect');
    if(select && selectedTextFiles.size === 1){
        select.value = [...selectedTextFiles][0];
    }
}

function updateTextFileSelectionState(){
    const stateEl = document.getElementById('fileTextSelectionState');
    if(!stateEl) return;
    if(selectedTextFiles.size === 0){
        stateEl.textContent = '当前已选：未选择输出文件';
    } else if(selectedTextFiles.size === 1){
        stateEl.textContent = `当前已选：${[...selectedTextFiles][0]}`;
    } else {
        stateEl.textContent = `当前已选 ${selectedTextFiles.size} 个文本文件`;
    }
}

function toggleSelectAllTextFiles(){
    const container = document.getElementById('textFileListContainer');
    if(!container) return;

    // 获取当前过滤后的文件列表
    const kw = textFileFilterKeyword.toLowerCase();
    const filtered = textFilesMeta.filter(item => !kw || item.name.toLowerCase().includes(kw));

    const allSelected = filtered.every(item => {
        const key = item.folder ? (item.folder+'/'+item.name):item.name;
        return selectedTextFiles.has(key);
    });

    if(allSelected){
        // 取消全选
        filtered.forEach(item => {
            const key = item.folder? (item.folder+'/'+item.name):item.name;
            selectedTextFiles.delete(key);
        });
    } else {
        // 全选
        filtered.forEach(item => {
            const key = item.folder? (item.folder+'/'+item.name):item.name;
            selectedTextFiles.add(key);
        });
    }

    renderTextFileList();
    updateTextFileSelectionState();
}

async function downloadSelectedTextFiles(){
    if(selectedTextFiles.size === 0){
        alert('请先选择要下载的文件');
        return;
    }

    const keys = [...selectedTextFiles];
    // 检测是否包含跨文件夹文件（key 格式为 "folder/name"）
    const hasFolderPrefix = keys.some(k => k.includes('/'));

    if(hasFolderPrefix){
        // 全部文件夹模式：使用 multi-download API (POST)
        const items = keys.map(k => {
            const idx = k.indexOf('/');
            return idx > 0 ? {folder: k.substring(0, idx), name: k.substring(idx + 1)} : null;
        }).filter(Boolean);
        // 先获取下载 URL，再触发下载
        try{
            const resp = await fetch('/api/folders/download-multi', {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({items})
            });
            if(!resp.ok){
                const err = await resp.json().catch(()=>({}));
                alert('下载失败：' + (err.detail || resp.statusText));
                return;
            }
            const blob = await resp.blob();
            const cd = resp.headers.get('content-disposition') || '';
            let fname = 'selected-files.zip';
            const m = cd.match(/filename="?(.+?)"?$/);
            if(m) fname = m[1];
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = fname;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(a.href);
        }catch(e){
            alert('下载失败：' + e.message);
        }
    } else {
        // 单文件夹模式
        const folder = document.getElementById('folderSelect')?.value;
        if(!folder){
            alert('请先选择文件夹');
            return;
        }
        try{
            const resp = await fetch('/api/folders/download-output', {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({folder_name: folder, files: keys})
            });
            if(!resp.ok){
                const err = await resp.json().catch(()=>({}));
                alert('下载失败：' + (err.detail || resp.statusText));
                return;
            }
            const blob = await resp.blob();
            const cd = resp.headers.get('content-disposition') || '';
            let fname = folder + '.zip';
            const m = cd.match(/filename="?(.+?)"?$/);
            if(m) fname = m[1];
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = fname;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(a.href);
        }catch(e){
            alert('下载失败：' + e.message);
        }
    }
}

function applyProfileFilter(){
    profileFilterKeyword = (document.getElementById('profileSearch')?.value || '').trim().toLowerCase();
    const kw = profileFilterKeyword;
    const filtered = kw ? profileNamesList.filter(v=>v.toLowerCase().includes(kw)) : [...profileNamesList];
    const select = document.getElementById('profileSel');
    if(!select) return;
    const cur = select.value;
    select.innerHTML = '';
    filtered.forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=v; select.appendChild(o); });
    if(filtered.includes(cur)){
        select.value = cur;
    } else if(filtered.length){
        select.value = filtered[0];
    }
    syncSelectionStates();
}

function newProfile(){
    createNewProfileFromCurrent().catch(e=>{
        document.getElementById('modelStatus').value = `❌ 新建失败: ${e.message}`;
    });
}

function normalizeProfileName(name){
    return String(name || '').trim();
}

function getUniqueProfileName(baseName, excludeName=''){
    const base = normalizeProfileName(baseName) || '新配置';
    const exclude = normalizeProfileName(excludeName);
    const existing = new Set((profileNamesList || []).map(v=>normalizeProfileName(v)).filter(Boolean));
    if(!existing.has(base) || base === exclude){
        return base;
    }
    let idx = 2;
    while(true){
        const candidate = `${base}${idx}`;
        if(!existing.has(candidate) || candidate === exclude){
            return candidate;
        }
        idx += 1;
    }
}

function getAllModelOptions(selectId){
    const select = document.getElementById(selectId);
    if(!select) return [];
    try{
        const all = JSON.parse(select.dataset.allModels || '[]');
        if(Array.isArray(all) && all.length){
            return sortModelNames(all);
        }
    }catch(_e){
        // ignore invalid cache
    }
    return sortModelNames(Array.from(select.options).map(opt=>opt.value));
}

function buildProfilePayload(overrideName=''){
    const originalName = normalizeProfileName(document.getElementById('profileOriginalName')?.value || '');
    const name = normalizeProfileName(overrideName || document.getElementById('profileName').value);
    return {
        original_name: originalName,
        name,
        base_url: document.getElementById('baseUrl').value || '',
        api_key: document.getElementById('apiKey').value || '',
        default_model: document.getElementById('modelSel').value || '',
        models: getAllModelOptions('modelSel'),
    };
}

async function createNewProfileFromCurrent(){
    const newName = getUniqueProfileName('新配置');
    const payload = buildProfilePayload(newName);
    payload.original_name = '';
    const data = await api('/api/model/profiles/save', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload)
    });
    document.getElementById('modelStatus').value = data.message || `✅ 已新增配置: ${newName}`;
    await loadProfiles();
    const select = document.getElementById('profileSel');
    if(select){
        select.value = newName;
    }
    await onProfileSelected();
    document.getElementById('profileName').focus();
    document.getElementById('profileName').select();
}

function selectTextFile(fileName){
    const select = document.getElementById('textFileSelect');
    if(!select) return;
    select.value = fileName;
    selectedTextFiles.clear();
    selectedTextFiles.add(fileName);
    applyTextFileFilter();
    syncSelectionStates();
}

function selectProfileGroup(name){
    const select = document.getElementById('profileSel');
    if(!select) return;
    select.value = name;
    select.dispatchEvent(new Event('change', {bubbles:true}));
    syncSelectionStates();
}

function updateTaskPanel(data){
    const panelJob = document.getElementById('taskPanelJob');
    const panelStep = document.getElementById('taskPanelStep');
    const panelPct = document.getElementById('taskPanelPct');
    const panelEta = document.getElementById('taskPanelEta');
    const panelBar = document.getElementById('taskPanelBar');
    const panelState = document.getElementById('taskPanelState');
    if(!panelJob || !panelStep || !panelPct || !panelEta || !panelBar || !panelState) return;

    const currentJob = data?.current_job || '暂无';
    const step = data?.step_label || (data?.running ? '处理中' : '待机');
    const pctNum = Math.max(0, Math.min(100, Number(data?.progress_pct || 0)));
    const etaNum = Math.max(0, Number(data?.eta_seconds || 0));

    panelJob.textContent = currentJob;
    panelStep.textContent = step;
    panelPct.textContent = `${Math.round(pctNum)}%`;
    panelEta.textContent = toHms(etaNum);
    panelBar.style.width = `${pctNum}%`;

    if(data?.running){
        panelState.textContent = `运行中：${step}`;
    }else if(data?.failed){
        panelState.textContent = `失败：${data?.status || '任务执行失败'}`;
    }else if(data?.done){
        panelState.textContent = '已完成，可在最终文件列表中下载结果';
    }else{
        panelState.textContent = '暂无运行任务';
    }
}

function updateSelectionClass(selectId){
    const el = document.getElementById(selectId);
    if(!el) return;
    el.classList.toggle('has-selection', !!el.value);
}

function setSelectionState(stateId, text){
    const el = document.getElementById(stateId);
    if(el){ el.textContent = text; }
}

async function loadTempFileSettings(){
    try{
        const data = await api('/api/settings/temp-files');
        const videoInput = document.getElementById('tempVideoKeepCount');
        if(videoInput) videoInput.value = data.temp_video_keep_count || 5;
    }catch(e){
        console.error('加载临时文件设置失败', e);
    }
}

async function saveTempFileSettings(){
    const videoCount = document.getElementById('tempVideoKeepCount')?.value;
    const statusEl = document.getElementById('tempFileStatus');

    try{
        const data = await api('/api/settings/temp-files', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                temp_video_keep_count: parseInt(videoCount) || 5
            })
        });
        if(statusEl) statusEl.value = data.message || '已保存';
    }catch(e){
        if(statusEl) statusEl.value = '失败';
    }
}

function syncSelectionStates(){
    const backend = document.getElementById('homeBackendSel')?.value || '';
    const backendModel = document.getElementById('homeBackendModelSel')?.value || '';
    const homeModel = document.getElementById('homeModelSel')?.value || '';
    const profile = document.getElementById('profileSel')?.value || '';
    const model = document.getElementById('modelSel')?.value || '';
    const uploadedFile = document.getElementById('videoFile')?.files?.[0]?.name || '';
    const historyVideo = document.getElementById('historyVideo')?.value || '';
    const currentVideo = uploadedFile || historyVideo;
    const currentFile = currentVideo ? currentVideo.replace(/^.*[\\/]/, '') : '';
    const folder = document.getElementById('folderSelect')?.value || '';
    const textFile = document.getElementById('textFileSelect')?.value || '';

    setSelectionState(
        'homeSelectionState',
        (backend || backendModel || homeModel)
            ? `当前已选后端：${backend || '未选择'} ｜ 后端模型：${backendModel || '未选择'} ｜ 翻译模型：${homeModel || '未选择'}`
            : '当前已选：未选择后端模型与翻译模型'
    );
    setSelectionState('modelSelectionState', (profile || model) ? `当前已选配置：${profile || '未选择'} ｜ 模型：${model || '未选择'}` : '当前已选：未选择配置组与模型');
    setSelectionState('fileFolderSelectionState', folder ? `当前已选文件夹：${folder}` : '当前已选：未选择历史文件夹');
    setSelectionState('fileTextSelectionState', textFile ? `当前已选文本：${textFile}` : '当前已选：未选择输出文件');
    setSelectionState('homeSummaryBackend', backend || '未选择');
    setSelectionState('homeSummaryBackendModel', backendModel || '未选择');
    setSelectionState('homeSummaryTranslate', homeModel || '未选择');
    setSelectionState('homeSummaryFile', currentFile || '未选择');

    ['homeBackendSel','homeBackendModelSel','homeModelSel','modelSel','profileSel','folderSelect','textFileSelect','historyVideo'].forEach(updateSelectionClass);
}

function bindSelectionListeners(){
    const ids = ['homeBackendSel','homeBackendModelSel','homeModelSel','modelSel','profileSel','folderSelect','textFileSelect','historyVideo','videoFile'];
    ids.forEach(id=>{
        const el = document.getElementById(id);
        if(!el || el.dataset.selectionBound === '1') return;
        el.addEventListener('change', syncSelectionStates);
        el.addEventListener('click', syncSelectionStates);
        el.dataset.selectionBound = '1';
    });
}

function getBackendModelCatalog(){
    const backend = document.getElementById('homeBackendSel')?.value || APP_DEFAULTS.backend;
    if(backend === 'faster-whisper（多语言）') return HOME_WHISPER_MODELS;
    if(backend.startsWith('VibeVoice')) return HOME_VIBEVOICE_MODELS;
    return HOME_FUNASR_MODELS;
}

function renderBackendModelText(item){
    const tags = [];
    if(item.speaker){
        tags.push('角色识别');
    }
    if(item.feature){
        tags.push(item.feature);
    }
    const suffix = tags.length ? `  |  ${tags.join(' / ')}` : '';
    return `${item.name}${suffix}`;
}

function setBackendModelOptions(preferred=''){
    const select = document.getElementById('homeBackendModelSel');
    const search = document.getElementById('homeBackendModelSearch');
    if(!select) return;

    const catalog = getBackendModelCatalog();
    select.dataset.modelCatalog = JSON.stringify(catalog);

    const keyword = ((search?.value) || '').trim().toLowerCase();
    const filtered = catalog.filter(item=>{
        const hay = `${item.value} ${item.name} ${item.feature || ''} ${item.speaker ? '角色识别' : ''}`.toLowerCase();
        return !keyword || hay.includes(keyword);
    });

    const oldValue = select.value;
    select.innerHTML = '';
    filtered.forEach(item=>{
        const o = document.createElement('option');
        o.value = item.value;
        o.textContent = renderBackendModelText(item);
        o.title = o.textContent;
        if(item.speaker){
            o.style.color = '#a14a2f';
            o.style.fontWeight = '700';
        }
        select.appendChild(o);
    });

    const next = preferred || oldValue;
    if(next && filtered.some(item=>item.value === next)){
        select.value = next;
    }else if(filtered.length){
        select.value = filtered[0].value;
    }

    if(search){
        search.placeholder = `筛选后端模型（共 ${catalog.length} 项）`;
    }
}

function filterBackendModels(){
    const select = document.getElementById('homeBackendModelSel');
    if(!select) return;
    setBackendModelOptions(select.value);
    syncSelectionStates();
}

function onHomeBackendChange(preferred=''){
    const prefer = preferred || document.getElementById('homeBackendModelSel')?.value || '';
    setBackendModelOptions(prefer);
    syncSelectionStates();
}

function sortModelNames(models){
    return [...new Set((models||[]).filter(Boolean).map(x=>String(x).trim()))]
        .sort((a,b)=>a.localeCompare(b, 'zh-Hans-CN', {sensitivity:'base'}));
}

function setSearchableModelOptions(searchId, selectId, models, preferred){
    const search = document.getElementById(searchId);
    const select = document.getElementById(selectId);
    if(!select) return;

    const sorted = sortModelNames(models || []);
    select.dataset.allModels = JSON.stringify(sorted);

    const keyword = ((search && search.value) || '').trim().toLowerCase();
    const filtered = keyword ? sorted.filter(m=>m.toLowerCase().includes(keyword)) : sorted;

    const oldValue = select.value;
    select.innerHTML = '';
    filtered.forEach(m=>{ const o=document.createElement('option'); o.value=m; o.textContent=m; select.appendChild(o); });

    if(filtered.length){
        if(preferred && filtered.includes(preferred)){
            select.value = preferred;
        }else if(oldValue && filtered.includes(oldValue)){
            select.value = oldValue;
        }else{
            select.value = filtered[0];
        }
    }

    if(search){
        search.placeholder = `输入关键字筛选模型（共 ${sorted.length} 个）`;
    }
    syncSelectionStates();
}

function filterModels(searchId, selectId){
    const select = document.getElementById(selectId);
    if(!select) return;
    let all = [];
    try{
        all = JSON.parse(select.dataset.allModels || '[]');
    }catch(_e){
        all = [];
    }
    setSearchableModelOptions(searchId, selectId, all, select.value);
}

function setSearchableOptions(searchId, selectId, items, preferred){
        const search = document.getElementById(searchId);
        const select = document.getElementById(selectId);
        if(!select) return;

        const list = [];
        for(const item of (items || [])){
            const v = String(item || '').trim();
            if(v && !list.includes(v)) list.push(v);
        }
        select.dataset.allOptions = JSON.stringify(list);

        const keyword = ((search && search.value) || '').trim().toLowerCase();
        const filtered = keyword ? list.filter(v=>v.toLowerCase().includes(keyword)) : list;
        const oldValue = select.value;
        select.innerHTML = '';
        filtered.forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=v; select.appendChild(o); });

        if(filtered.length){
            if(preferred && filtered.includes(preferred)){
                select.value = preferred;
            }else if(oldValue && filtered.includes(oldValue)){
                select.value = oldValue;
            }else{
                select.value = filtered[0];
            }
        }

        if(search){
            search.placeholder = `输入关键字筛选（共 ${list.length} 项）`;
        }
        syncSelectionStates();
}

function filterOptions(searchId, selectId){
        const select = document.getElementById(selectId);
        if(!select) return;
        let all = [];
        try{
            all = JSON.parse(select.dataset.allOptions || '[]');
        }catch(_e){
            all = [];
        }
        setSearchableOptions(searchId, selectId, all, select.value);
}

function showPage(name){
  for(const p of ['home','file','model','queue']){
    const el = document.getElementById('page-'+p);
    if(el) el.classList.toggle('active', p===name);
  }
  document.getElementById('btn-home').classList.toggle('active', name==='home');
  document.getElementById('btn-file').classList.toggle('active', name==='file');
  document.getElementById('btn-model').classList.toggle('active', name==='model');
  const btnQueue = document.getElementById('btn-queue');
  if(btnQueue) btnQueue.classList.toggle('active', name==='queue');
  if(name === 'queue'){
    refreshQueueStatus();
  }
}

async function refreshQueueStatus(){
  try{
    const data = await api('/api/queue/status');
    renderQueueStatus(data);
  }catch(e){
    console.error('获取队列状态失败', e);
  }
}

function renderQueueStatus(data){
  // 渲染 4 个阶段列
  const stageMap = {download:'stageDownload',extract:'stageExtract',transcribe:'stageTranscribe',translate:'stageTranslate'};
  const countMap = {download:'stageCountDownload',extract:'stageCountExtract',transcribe:'stageCountTranscribe',translate:'stageCountTranslate'};
  const stages = data.stages || {};

  for(const [key, containerId] of Object.entries(stageMap)){
    const container = document.getElementById(containerId);
    const countEl = document.getElementById(countMap[key]);
    const stage = stages[key] || {};
    const jobs = stage.jobs || [];
    const depth = stage.queue_depth || 0;
    if(countEl) countEl.textContent = '(' + jobs.length + ')';
    if(!container) continue;
    if(jobs.length === 0){
      container.innerHTML = '<div style="padding:8px;text-align:center;font-size:11px;color:#aaa">暂无</div>';
    }else{
      let html = '';
      let activeIdx = -1;
      jobs.forEach((j, i) => { if(j.running && activeIdx < 0) activeIdx = i; });
      jobs.forEach((job, idx) => {
        const fileName = (job.display_name || job.current_job || '未知文件');
        const shortName = fileName.length > 25 ? fileName.substring(0,25)+'...' : fileName;
        const pct = job.progress_pct || 0;
        const isActive = job.running;
        let stepText = '';
        if(isActive){
          stepText = pct > 0 ? (job.step_label||'处理中')+' '+pct+'%' : (job.step_label||'处理中');
        } else if(activeIdx >= 0){
          const waitPos = idx > activeIdx ? idx - activeIdx : idx + 1;
          stepText = '排队 #'+waitPos+' — 等待'+(stage.label||'')+'完成';
        } else {
          stepText = '等待处理';
        }
        const border = isActive ? 'border:1px solid #2abfa6;background:#f0faf7;' : 'border-bottom:1px solid #eee;';
        html += '<div style="padding:6px 4px;'+border+'border-radius:4px;margin-bottom:3px">' +
          '<div style="display:flex;justify-content:space-between;align-items:center">' +
          '<span style="font-size:12px;font-weight:'+(isActive?'600':'400')+';overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%">'+shortName+'</span>' +
          (isActive ? '<span style="font-size:10px;color:#2abfa6">● 运行中</span>' : '<span style="font-size:10px;color:#aaa">#'+(idx+1)+'</span>') +
          '</div>';
        if(isActive && pct > 0){
          html += '<div style="margin:3px 0"><div style="height:4px;background:#e0e0e0;border-radius:2px;overflow:hidden">' +
            '<div style="height:100%;width:'+pct+'%;background:#2abfa6"></div></div></div>';
        }
        html += '<div style="font-size:10px;color:'+(isActive?'#2abfa6':'#999')+'">'+stepText+'</div></div>';
      });
      container.innerHTML = html;
    }
  }

  // 已完成任务
  const allJobsContainer = document.getElementById('allJobsList');
  if(allJobsContainer){
    const doneJobs = data.done_jobs || [];
    if(doneJobs.length === 0){
      allJobsContainer.innerHTML = '<div class="selection-hint" style="padding:10px;text-align:center;font-size:12px">暂无任务记录</div>';
    }else{
      let html = '';
      doneJobs.forEach(job => {
        const fileName = job.display_name || '未知文件';
        const statusColor = job.failed ? '#e74c3c' : '#2abfa6';
        const statusText = job.failed ? '失败' : '已完成';
        const timeStr = job.updated_at ? new Date(job.updated_at * 1000).toLocaleTimeString() : '';
        html += '<div style="padding:6px 8px;border-bottom:1px solid #eee">' +
          '<div style="display:flex;justify-content:space-between;align-items:center">' +
          '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:65%;font-size:13px">' + fileName + '</span>' +
          '<div style="display:flex;align-items:center;gap:6px">' +
          '<span style="font-size:11px;color:' + statusColor + '">' + statusText + '</span>' +
          (timeStr ? '<span style="font-size:10px;color:#aaa">' + timeStr + '</span>' : '') +
          '</div></div></div>';
      });
      allJobsContainer.innerHTML = html;
    }
  }
}

function _buildMetaStr(job){
  const parts = [];
  if(job.model_info) parts.push(job.model_info);
  if(job.language) parts.push(job.language);
  if(job.device) parts.push(job.device);
  return parts.join(' · ') || '—';
}

async function api(url, opts={}){
  const r = await fetch(url, opts);
    if(!r.ok){
        const txt = await r.text();
        let msg = txt;
        try{
            const obj = JSON.parse(txt);
            msg = (obj && (obj.detail || obj.message)) ? (obj.detail || obj.message) : txt;
        }catch(_e){
            // keep raw text
        }
        throw new Error(msg);
    }
  return await r.json();
}

async function refreshHistory(preferredValue='', preferredFolder=''){
  const data = await api('/api/history');
  const hv = document.getElementById('historyVideo');
  const oldValue = hv.value;
  hv.innerHTML = '';
  (data.videos||[]).forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=v; hv.appendChild(o); });
        const values = Array.from(hv.options).map(opt => opt.value);
        const historyStem = (value='')=>{
            const parts = String(value || '').split('/');
            const fileName = parts[parts.length - 1] || '';
            const dot = fileName.lastIndexOf('.');
            return (dot > 0 ? fileName.slice(0, dot) : fileName).toLowerCase();
        };
        const isAudioHistoryValue = (value='')=>{
            const lower = String(value || '').toLowerCase();
            return !['.mp4','.mkv','.avi','.mov','.wmv','.flv','.webm','.m4v','.ts'].some(ext => lower.endsWith(ext));
        };
        const findSameStemAudio = (value='')=>{
            const stem = historyStem(value);
            if(!stem){ return ''; }
            return values.find(v => isAudioHistoryValue(v) && historyStem(v) === stem) || '';
        };
        const wavValue = values.find(v => v.toLowerCase().endsWith('.wav')) || '';
        if(preferredValue && values.includes(preferredValue)){
            hv.value = preferredValue;
        }else if(preferredValue && findSameStemAudio(preferredValue)){
            hv.value = findSameStemAudio(preferredValue);
        }else if(oldValue && values.includes(oldValue)){
            hv.value = oldValue;
        }else if(oldValue && findSameStemAudio(oldValue)){
            hv.value = findSameStemAudio(oldValue);
        }else if(wavValue){
            hv.value = wavValue;
        }else if(values.length){
            hv.value = values[0];
        }
        foldersList = data.folders || [];
        foldersListMeta = data.folders_meta || [];
        // 刷新时清除选中状态
        selectedFolders.clear();
        lastClickedFolder = null;
        const activeFolder = preferredFolder && foldersList.includes(preferredFolder)
            ? preferredFolder
            : (document.getElementById('folderSelect')?.value || '');
        setHiddenSelectOptions('folderSelect', foldersList, activeFolder);
        // 如果有优先文件夹，自动在可见列表中选中它
        if(preferredFolder && foldersList.includes(preferredFolder)){
            selectedFolders.add(preferredFolder);
            lastClickedFolder = foldersList.indexOf(preferredFolder);
        }
        applyFolderFilter();
        await refreshCurrentFileView();
        syncSelectionStates();
}

// ─── 全部文件夹视图切换 ───
function toggleAllFilesView(){
    showAllFoldersFiles = !showAllFoldersFiles;
    const btn = document.getElementById('toggleAllFilesBtn');
    const folderCol = document.getElementById('textFileFolderCol');
    if(btn) btn.textContent = showAllFoldersFiles ? '显示当前文件夹' : '显示全部文件夹';
    if(folderCol) folderCol.style.display = showAllFoldersFiles ? '' : 'none';
    refreshCurrentFileView();
}

async function refreshCurrentFileView(){
    if(showAllFoldersFiles){
        await refreshAllTextFiles();
    } else {
        await refreshTextFiles();
    }
}

async function refreshAllTextFiles(){
    try{
        const data = await api('/api/folders/all-output-files');
        allFilesMeta = Array.isArray(data.files)
            ? data.files.map(item=>({folder:item.folder, name:item.name, mtime:Number(item.mtime || 0), size:Number(item.size || 0)}))
            : [];
        // 使用 allFilesMeta 作为显示数据源
        textFilesMeta = allFilesMeta.map(item=>({folder:item.folder, name:item.name, mtime:item.mtime, size:item.size}));
        selectedTextFiles.clear();
        lastClickedTextFile = null;
        applyTextFileFilter();
        updateTextFileSelectionState();
        syncSelectionStates();
    }catch(e){
        console.error('全部文件列表获取失败', e);
        textFilesMeta = [];
        allFilesMeta = [];
        selectedTextFiles.clear();
        lastClickedTextFile = null;
        applyTextFileFilter();
        updateTextFileSelectionState();
        syncSelectionStates();
    }
}

async function refreshTextFiles(){
    const folder = document.getElementById('folderSelect').value;
    if(!folder){
        textFilesMeta = [];
        selectedTextFiles.clear();
        lastClickedTextFile = null;
        setHiddenSelectOptions('textFileSelect', [], '');
        applyTextFileFilter();
        updateTextFileSelectionState();
        syncSelectionStates();
        return;
    }
    try{
        const data = await api('/api/folders/output-files?folder_name='+encodeURIComponent(folder));
        textFilesMeta = Array.isArray(data.files)
            ? data.files.map(item=>({name:item.name, mtime:Number(item.mtime || 0), size:Number(item.size || 0)}))
            : [];
        const names = textFilesMeta.map(item=>item.name);
        setHiddenSelectOptions('textFileSelect', names, '');
        selectedTextFiles.clear();
        lastClickedTextFile = null;
        applyTextFileFilter();
        updateTextFileSelectionState();
        syncSelectionStates();
    }catch(e){
        console.error('文件列表获取失败', e);
        textFilesMeta = [];
        selectedTextFiles.clear();
        lastClickedTextFile = null;
        setHiddenSelectOptions('textFileSelect', [], '');
        applyTextFileFilter();
        updateTextFileSelectionState();
        syncSelectionStates();
    }
}

function downloadTextFile(){
    const folder = document.getElementById('folderSelect').value;
    if(!folder){
                alert('请先选择文件夹');
        return;
    }
    const url = '/api/folders/download-text?folder_name='+encodeURIComponent(folder);
    window.open(url, '_blank');
}

async function loadProfiles(){
  const data = await api('/api/model/profiles');
    profileNamesList = data.profile_names || [];
    setHiddenSelectOptions('profileSel', profileNamesList, data.active || profileNamesList[0] || '');
    applyProfileFilter();
    applyAppSettings(data.app_settings || APP_DEFAULTS);
  fillProfile(data.active_profile || {});
}

function applyAppSettings(settings){
        const merged = {
                backend: settings?.DEFAULT_BACKEND || settings?.backend || APP_DEFAULTS.backend,
                funasrModel: settings?.DEFAULT_FUNASR_MODEL || settings?.funasrModel || APP_DEFAULTS.funasrModel,
                whisperModel: settings?.DEFAULT_WHISPER_MODEL || settings?.whisperModel || APP_DEFAULTS.whisperModel,
        autoSubtitleLang: normalizeSubtitlePriority(settings?.AUTO_SUBTITLE_LANG || settings?.autoSubtitleLang || settings?.AUTO_SUBTITLE_LANGS || settings?.autoSubtitleLangs || APP_DEFAULTS.autoSubtitleLang),
        language: settings?.DEFAULT_LANGUAGE || settings?.language || '自动检测',
        device: settings?.DEFAULT_DEVICE || settings?.device || 'CUDA',
        autoTranslate: settings?.AUTO_TRANSLATE === '1',
        autoDownload: settings?.AUTO_DOWNLOAD === '1',
        directSave: settings?.DIRECT_SAVE !== '0',
        };
        const backendSel = document.getElementById('backendSel');
        const homeBackendSel = document.getElementById('homeBackendSel');
        const funasrSel = document.getElementById('funasrModel');
        const whisperSel = document.getElementById('whisperModel');
    const autoSubtitleLangs = document.getElementById('autoSubtitleLangs');
        if(backendSel){ backendSel.value = merged.backend; }
        if(homeBackendSel){ homeBackendSel.value = merged.backend; }
        if(funasrSel){ funasrSel.value = merged.funasrModel; }
        if(whisperSel){ whisperSel.value = merged.whisperModel; }
        if(autoSubtitleLangs){
            const hasValue = Array.from(autoSubtitleLangs.options).some(opt => opt.value === merged.autoSubtitleLang);
            autoSubtitleLangs.value = hasValue ? merged.autoSubtitleLang : APP_DEFAULTS.autoSubtitleLang;
        }
    const langSel = document.getElementById('langSel');
    if(langSel) langSel.value = merged.language;
    const deviceSel = document.getElementById('deviceSel');
    if(deviceSel) deviceSel.value = merged.device;
    const autoTranslateCb = document.getElementById('autoTranslate');
    if(autoTranslateCb) autoTranslateCb.checked = merged.autoTranslate;
    const autoDownloadCb = document.getElementById('autoDownload');
    if(autoDownloadCb) autoDownloadCb.checked = merged.autoDownload;
    const directSaveCb = document.getElementById('directSave');
    if(directSaveCb) directSaveCb.checked = merged.directSave;
    lastSavedAutoSubtitleLang = autoSubtitleLangs ? autoSubtitleLangs.value : '';
    onHomeBackendChange(merged.backend === 'faster-whisper（多语言）' ? merged.whisperModel : merged.funasrModel);
}

function saveUiPreferences(){
    const backend = document.getElementById('homeBackendSel')?.value || '';
    const funasrModel = document.getElementById('funasrModel')?.value || '';
    const whisperModel = document.getElementById('whisperModel')?.value || '';
    const language = document.getElementById('langSel')?.value || '自动检测';
    const device = document.getElementById('deviceSel')?.value || 'CUDA';
    const autoTranslate = document.getElementById('autoTranslate')?.checked ? '1' : '0';
    const autoDownload = document.getElementById('autoDownload')?.checked ? '1' : '0';
    const directSave = document.getElementById('directSave')?.checked ? '1' : '0';
    api('/api/app-settings/ui-preferences', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({backend, funasr_model:funasrModel, whisper_model:whisperModel, language, device, auto_translate:autoTranslate, auto_download:autoDownload, direct_save:directSave})
    }).catch(()=>{});
}

function fillProfile(p){
    document.getElementById('profileOriginalName').value = p.name || '';
  document.getElementById('profileName').value = p.name || '';
  document.getElementById('baseUrl').value = p.base_url || '';
  document.getElementById('apiKey').value = p.api_key || '';
    const models = sortModelNames(p.models || []);
    const defaultModel = p.default_model || '';
    setSearchableModelOptions('modelSearch', 'modelSel', models, defaultModel);
    setSearchableModelOptions('homeModelSearch', 'homeModelSel', models, defaultModel);
    syncSelectionStates();
}

async function onProfileSelected(){
  const name = document.getElementById('profileSel').value;
    if(!name){
        fillProfile({});
        syncSelectionStates();
        return;
    }
  const data = await api('/api/model/profile?name='+encodeURIComponent(name));
  fillProfile(data.profile || {});
        applyProfileFilter();
    syncSelectionStates();
}

async function fetchModels(){
    const payload = {
                original_name: normalizeProfileName(document.getElementById('profileOriginalName')?.value || ''),
                name: document.getElementById('profileName').value,
                base_url: document.getElementById('baseUrl').value,
                api_key: document.getElementById('apiKey').value
    };
    try{
        const data = await api('/api/model/profiles/fetch-models', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
        const models = sortModelNames(data.models||[]);
        fillProfile({
            name: document.getElementById('profileName').value,
            base_url: document.getElementById('baseUrl').value,
            api_key: document.getElementById('apiKey').value,
            models,
            default_model: data.default_model || ''
        });
        const preview = models.slice(0, 12).join(', ');
        document.getElementById('modelStatus').value = `✅ 测试通过，可用模型(${models.length})：${preview}${models.length > 12 ? ' ...' : ''}`;
    }catch(e){
        document.getElementById('modelStatus').value = `❌ 测试失败: ${e.message}`;
    }
}

async function saveProfile(){
    const payload = buildProfilePayload();
    if(!payload.name){
        document.getElementById('modelStatus').value = '❌ 配置名称不能为空';
        return;
    }
    payload.default_backend = document.getElementById('backendSel').value || APP_DEFAULTS.backend;
    payload.default_funasr_model = document.getElementById('funasrModel').value || APP_DEFAULTS.funasrModel;
    payload.default_whisper_model = document.getElementById('whisperModel').value || APP_DEFAULTS.whisperModel;
        payload.auto_subtitle_lang = document.getElementById('autoSubtitleLangs')?.value || APP_DEFAULTS.autoSubtitleLang;
  const data = await api('/api/model/profiles/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  document.getElementById('modelStatus').value = data.message || '';
  await loadProfiles();
        const select = document.getElementById('profileSel');
        if(select){
                select.value = payload.name;
        }
        await onProfileSelected();
    syncSelectionStates();
}

async function deleteProfile(){
  const payload = {name: document.getElementById('profileSel').value};
  const data = await api('/api/model/profiles/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  document.getElementById('modelStatus').value = data.message || '';
  await loadProfiles();
    syncSelectionStates();
}

function backendFormData(fd){
    const backend = document.getElementById('homeBackendSel')?.value || document.getElementById('backendSel').value;
    const backendModel = document.getElementById('homeBackendModelSel')?.value || '';
    fd.append('backend', backend);
  fd.append('language', document.getElementById('langSel').value);
    if(backend === 'faster-whisper（多语言）'){
        fd.append('whisper_model', backendModel || document.getElementById('whisperModel').value || 'medium');
        fd.append('funasr_model', document.getElementById('funasrModel').value || 'paraformer-zh ⭐ 普通话精度推荐');
    }else{
        fd.append('whisper_model', document.getElementById('whisperModel').value || 'medium');
        fd.append('funasr_model', backendModel || document.getElementById('funasrModel').value || 'paraformer-zh ⭐ 普通话精度推荐');
    }
  fd.append('device', document.getElementById('deviceSel').value || 'CUDA');
}

async function downloadOutputZip(forceBlob){
    if(!currentJobId){
        alert('请先开始并完成任务');
        return;
    }
    let zipName = '';
    try{
        const data = await api('/api/jobs/'+currentJobId+'/files');
        zipName = (data.files||[]).find(f=>f.endsWith('.zip')) || '';
    }catch(e){
        alert('获取文件列表失败: '+e.message);
        return;
    }
    if(!zipName){
        alert('暂无可下载的 ZIP 文件，请等待任务完成后再试。');
        return;
    }
    const url = '/api/jobs/'+currentJobId+'/download-file?file_name='+encodeURIComponent(zipName);
    if(forceBlob || document.getElementById('directSave')?.checked){
        // fetch+blob 方式：可靠地保存到浏览器默认下载路径（不受用户手势限制）
        const resp = await fetch(url);
        if(!resp.ok) throw new Error('下载失败: '+resp.status);
        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = zipName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(blobUrl);
    }else{
        const a = document.createElement('a');
        a.href = url;
        a.download = zipName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }
}

async function downloadVideoUrl(){
  const url = (document.getElementById('urlInput').value||'').trim();
  if(!url){ alert('请输入视频URL'); return; }
  const btn = document.getElementById('urlDownloadBtn');
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '下载中…';
  document.getElementById('statusText').value = '⏳ 正在下载视频…';
  try{
        const subtitleLang = document.getElementById('autoSubtitleLangs')?.value || APP_DEFAULTS.autoSubtitleLang;
    const r = await fetch('/api/download_url',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
            body: JSON.stringify({url, auto_subtitle_lang: subtitleLang})
    });
    if(r.status===401){
      const d = await r.json().catch(()=>({}));
      alert('⚠️ 需要登录：'+(d.detail||'请先在浏览器中登录目标网站，然后重试。'));
      document.getElementById('statusText').value = '⚠️ 需要登录';
      return;
    }
    if(!r.ok){
      const d = await r.json().catch(()=>({}));
      throw new Error(d.detail||'下载失败');
    }
    const data = await r.json();
    document.getElementById('urlInput').value = '';

    // 刷新历史列表并选中下载的视频
    await refreshHistory(data.filepath || '');
    if(typeof syncSelectionStates==='function') syncSelectionStates();

    // 弹窗询问用户是否下载到本地（默认不下载）
    const saveToLocal = confirm(`视频已下载到临时目录：\n${data.filename}\n\n是否保存到本地下载目录？\n\n点击「确定」保存到本地\n点击「取消」仅保留临时文件`);

    if(saveToLocal){
      // 用户选择保存到本地，触发浏览器下载
      document.getElementById('statusText').value = '⏳ 正在保存到本地…';
      const downloadUrl = '/api/download_file?path=' + encodeURIComponent(data.filepath);
      const a = document.createElement('a');
      a.href = downloadUrl;
      a.download = data.filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      document.getElementById('statusText').value = '✅ 已保存: '+data.filename+' (点击「开始转录」继续)';
    } else {
      document.getElementById('statusText').value = '✅ 已下载到临时目录: '+data.filename+' (点击「开始转录」继续)';
    }

        if(data.auto_subtitle && data.subtitle_path){
            document.getElementById('statusText').value = '⏳ 检测到平台自动字幕，正在导入…';
            const imported = await api('/api/jobs/import-subtitle', {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({
                    history_video: data.filepath || '',
                    subtitle_path: data.subtitle_path || ''
                })
            });
            currentJobId = imported.job_id;
            startPoll();
            return;
        }
        // 不自动开始转录，让用户手动点击「开始转录」
  }catch(e){
    alert('❌ '+e.message);
    document.getElementById('statusText').value = '❌ 下载失败';
  }finally{
    btn.disabled=false;
    btn.textContent=origText;
  }
}

async function startTranscribe(){
  // 清空识别文本框
  const plainText = document.getElementById('plainText');
  if(plainText) plainText.value = '';

  const fd = new FormData();
  const f = document.getElementById('videoFile').files[0];
  if(f){ fd.append('video_file', f); }
  fd.append('history_video', document.getElementById('historyVideo').value || '');
  fd.append('auto_translate', document.getElementById('autoTranslate').checked ? '1' : '0');
  fd.append('auto_download', document.getElementById('autoDownload').checked ? '1' : '0');
  pendingAutoFlags = {
    download: document.getElementById('autoDownload').checked,
  };
  backendFormData(fd);

  document.getElementById('statusText').value = '⏳ 正在启动转录任务...';

  const r = await fetch('/api/transcribe/start', {method:'POST', body:fd});
  if(!r.ok){ throw new Error(await r.text()); }
  const data = await r.json();
  currentJobId = data.job_id;

  // 如果返回了视频路径，更新当前视频选择框
  if(data.video_path){
    await refreshHistory(data.video_path);
  }

  // 如果任务被加入队列，显示提示
  if(data.queued){
    document.getElementById('statusText').value = '⏳ 任务已加入队列，位置 ' + data.queue_position;
  }

  startPoll();
  // 刷新队列状态
  refreshQueueStatus();
}

async function stopJob(){
  if(!currentJobId) return;
  await api('/api/jobs/'+currentJobId+'/stop', {method:'POST'});
}

async function startTranslate(){
  if(!currentJobId){ alert('请先完成一次转录任务'); return; }
    const chosenModel = document.getElementById('homeModelSel').value || document.getElementById('modelSel').value;
        const payload = {
                online_profile: document.getElementById('profileSel').value,
                online_model: chosenModel,
                target_lang: document.getElementById('targetLangSel').value || 'zh',
                parallel_threads: document.getElementById('parallelThreadSel')?.value || '5'
        };
  await api('/api/jobs/'+currentJobId+'/translate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  startPoll();
}

function startPoll(){
    _stopPoll();
    _prevJobState = { current_job:'', step_label:'', done:false };

    // 优先尝试 SSE
    try{
        eventSource = new EventSource('/api/jobs/'+currentJobId+'/stream');
        eventSource.onmessage = function(e){
            try{
                const data = JSON.parse(e.data);
                handleJobUpdate(data);
            }catch(err){ console.error('SSE parse error', err); }
        };
        eventSource.onerror = function(){
            // SSE 失败 → 关闭并降级到轮询
            console.warn('SSE 连接失败，降级到轮询');
            if(eventSource){ eventSource.close(); eventSource = null; }
            startFallbackPoll();
        };
    }catch(e){
        console.warn('EventSource 不可用，降级到轮询', e);
        startFallbackPoll();
    }
}

function startFallbackPoll(){
    if(pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async ()=>{
        if(!currentJobId) return;
        try{
            const data = await api('/api/jobs/'+currentJobId);
            handleJobUpdate(data);
        }catch(e){ console.error(e); }
    }, 1000);
}

function _stopPoll(){
    if(eventSource){ eventSource.close(); eventSource = null; }
    if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
}

async function handleJobUpdate(data){
    document.getElementById('statusText').value = data.status || '';
    document.getElementById('plainText').value = data.plain_text || '';
    document.getElementById('logText').value = data.log_text || '';
    updateTaskPanel(data);

    const jobChanged = data.current_job && data.current_job !== _prevJobState.current_job;
    const stepChanged = data.step_label && data.step_label !== _prevJobState.step_label;
    const justDone = data.done && !data.running && !_prevJobState.done;

    if(jobChanged && data.current_job && data.current_prefix){
        const expectedWav = `workspace/${data.current_job}/${data.current_prefix}.wav`;
        refreshHistory(expectedWav);
    }
    if(stepChanged){
        refreshQueueStatus();
    }

    _prevJobState = {
        current_job: data.current_job || '',
        step_label: data.step_label || '',
        done: !!data.done,
    };

    if(justDone){
        const preferredWav = (data.current_job && data.current_prefix)
            ? `workspace/${data.current_job}/${data.current_prefix}.wav`
            : '';
        await refreshHistory(preferredWav, data.current_job || '');
        refreshQueueStatus();

        // 任务完成时触发自动下载（翻译由 Pipeline 后端自动处理）
        if(pendingAutoFlags.download && !data.failed){
          pendingAutoFlags.download = false;
          _stopPoll();
          try{ await downloadOutputZip(true); }catch(e){ console.error('自动下载失败', e); }
        } else {
          _stopPoll();
        }
    }
}

function initDragZones() {
  document.querySelectorAll('.drag-zone').forEach(zone => {
    zone.querySelectorAll('.drag-item').forEach(item => {
      item.addEventListener('dragstart', e => {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', '');
        item._dragSrc = true;
        item.classList.add('dragging');
      });
      item.addEventListener('dragend', () => {
        item._dragSrc = false;
        item.classList.remove('dragging');
        zone.querySelectorAll('.drag-item').forEach(i => i.classList.remove('drag-over'));
      });
      item.addEventListener('dragover', e => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        item.classList.add('drag-over');
      });
      item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
      item.addEventListener('drop', e => {
        e.preventDefault();
        item.classList.remove('drag-over');
        const src = zone.querySelector('.drag-item[draggable="true"].dragging');
        if (!src || src === item) return;
        const srcRow = src.closest('.drag-row');
        const dstRow = item.closest('.drag-row');
        const srcIdx = Array.from(srcRow.children).indexOf(src);
        const dstIdx = Array.from(dstRow.children).indexOf(item);
        // swap DOM nodes
        const srcNext = src.nextSibling;
        const dstNext = item.nextSibling;
        if (srcNext === item) {
          srcRow.insertBefore(item, src);
        } else if (dstNext === src) {
          dstRow.insertBefore(src, item);
        } else {
          srcRow.insertBefore(item, srcNext);
          dstRow.insertBefore(src, dstNext);
        }
      });
    });
  });
}

(async function init(){
  await refreshHistory();
  await loadProfiles();
  await loadTempFileSettings();
    bindSelectionListeners();
    syncSelectionStates();
        updateTaskPanel({});
        initDragZones();
  // 恢复正在运行的任务或最近完成的任务
  try{
    const qs = await api('/api/queue/status');
    if(qs.running_job){
      currentJobId = qs.running_job.job_id;
      // 恢复自动翻译/自动下载标志（防止页面刷新后丢失）
      pendingAutoFlags.translate = !!qs.running_job.auto_translate;
      pendingAutoFlags.download = !!qs.running_job.auto_download;
      startPoll();
    } else if(qs.all_jobs && qs.all_jobs.length > 0){
      // 找到最近的已完成但未翻译的任务
      const recent = qs.all_jobs.find(j => j.done && !j.failed);
      if(recent){
        currentJobId = recent.job_id;
        // 直接填充 UI（不再轮询，因为已完成）
        document.getElementById('statusText').value = recent.status || '';
        document.getElementById('plainText').value = recent.plain_text || '';
        document.getElementById('logText').value = recent.log_text || '';
        updateTaskPanel(recent);
      }
    }
  }catch(e){ console.error('恢复任务状态失败', e); }
})();
</script>
</body>
</html>
"""
    html = html.replace("__APP_DEFAULT_BACKEND__", json.dumps(default_backend, ensure_ascii=False))
    html = html.replace("__APP_DEFAULT_FUNASR_MODEL__", json.dumps(default_funasr_model, ensure_ascii=False))
    html = html.replace("__APP_DEFAULT_WHISPER_MODEL__", json.dumps(default_whisper_model, ensure_ascii=False))
    html = html.replace("__APP_DEFAULT_AUTO_SUBTITLE_LANG__", json.dumps(default_auto_subtitle_lang, ensure_ascii=False))
    return HTMLResponse(content=html)


@app.get("/api/history")
def api_history():
    folders_meta = core._list_job_folders_meta()
    return {
        "videos": core._list_uploaded_videos(),
        "folders": [f["name"] for f in folders_meta],
        "folders_meta": folders_meta,
    }


@app.post("/api/folders/delete")
def api_delete_folder(payload: dict[str, str]):
    name = payload.get("folder_name", "")
    status, *_ = core._delete_job_folder(name)
    return {"message": status}


@app.post("/api/folders/delete-batch")
def api_delete_folders_batch(payload: dict):
    """批量删除多个文件夹"""
    names = payload.get("folder_names", [])
    if not isinstance(names, list) or not names:
        return {"message": "未提供要删除的文件夹", "deleted_count": 0}

    deleted = 0
    skipped = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            status, *_ = core._delete_job_folder(name.strip())
            # 检查删除成功的多种标识
            if "成功" in status or "已删除" in status or "deleted" in status.lower():
                deleted += 1
            elif "不存在" in status:
                skipped += 1
            else:
                errors.append(f"{name}: {status}")
        except Exception as e:
            err_str = str(e)
            if "不存在" in err_str:
                skipped += 1
            else:
                errors.append(f"{name}: {err_str}")

    # 构建结果消息
    parts = []
    if deleted > 0:
        parts.append(f"删除 {deleted} 个")
    if skipped > 0:
        parts.append(f"跳过 {skipped} 个（不存在）")
    if errors:
        parts.append(f"失败: {'; '.join(errors[:3])}")

    message = "，".join(parts) if parts else "无操作"
    return {"message": message, "deleted_count": deleted, "skipped_count": skipped}


@app.get("/api/folders/zip-files")
def api_folder_zip_files(folder_name: str):
    folder = _resolve_workspace_folder(folder_name)
    entries: list[tuple[str, float]] = []
    for p in folder.glob("*.zip"):
        if not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        entries.append((p.name, mtime))

    entries.sort(key=lambda item: item[1], reverse=True)
    return {
        "folder_name": folder.name,
        "zip_files": [name for name, _mtime in entries],
        "zip_files_meta": [{"name": name, "mtime": int(mtime)} for name, mtime in entries],
    }


@app.get("/api/folders/download-text")
def api_download_text_file(folder_name: str):
    folder = _resolve_workspace_folder(folder_name)
    text_files = sorted(p for p in folder.glob("*.txt") if p.is_file())
    if not text_files:
        raise HTTPException(status_code=404, detail="text files not found")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for text_file in text_files:
            zf.write(text_file, arcname=text_file.name)
    buffer.seek(0)

    zip_name = f"{folder.name}.texts.zip"
    headers = {"Content-Disposition": f'attachment; filename="{zip_name}"'}
    return StreamingResponse(buffer, media_type="application/zip", headers=headers)


@app.get("/api/folders/download-selected-text")
def api_download_selected_text_files(folder_name: str, files: str):
    folder = _resolve_workspace_folder(folder_name)

    # 解析文件列表
    file_names = [f.strip() for f in files.split(",") if f.strip()]
    if not file_names:
        raise HTTPException(status_code=400, detail="未指定要下载的文件")

    # 验证并收集文件
    text_files = []
    for name in file_names:
        file_path = folder / name
        if not file_path.exists() or not file_path.is_file():
            continue
        # 安全检查：确保文件在 folder 目录内
        try:
            file_path.resolve().relative_to(folder.resolve())
        except ValueError:
            continue
        if file_path.suffix.lower() == ".txt":
            text_files.append(file_path)

    if not text_files:
        raise HTTPException(status_code=404, detail="未找到有效的文本文件")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for text_file in text_files:
            zf.write(text_file, arcname=text_file.name)
    buffer.seek(0)

    zip_name = f"{folder.name}.selected.zip"
    headers = {"Content-Disposition": f'attachment; filename="{zip_name}"'}
    return StreamingResponse(buffer, media_type="application/zip", headers=headers)


@app.get("/api/folders/output-files")
def api_folder_output_files(folder_name: str):
    """返回选中文件夹内的输出文件列表（ZIP/SRT/TXT/WAV 等）"""
    folder = _resolve_workspace_folder(folder_name)
    _OUTPUT_EXTS = {".zip", ".srt", ".txt", ".wav", ".vtt"}
    entries = []
    for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file() or p.suffix.lower() not in _OUTPUT_EXTS:
            continue
        try:
            mtime = p.stat().st_mtime
            size = p.stat().st_size
            size_mb = size / (1024 * 1024)
        except OSError:
            mtime = 0.0
            size_mb = 0
        entries.append({"name": p.name, "mtime": int(mtime), "size": size_mb})
    return {"folder_name": folder.name, "files": entries}


@app.post("/api/folders/translate")
def api_folder_translate(payload: dict[str, str]):
    """对已有文件夹中的 SRT 字幕执行翻译"""
    folder_name = str(payload.get("folder_name", "")).strip()
    if not folder_name:
        raise HTTPException(status_code=400, detail="folder_name 不能为空")

    job_dir = _resolve_workspace_folder(folder_name)
    file_prefix = core._resolve_file_prefix(job_dir, None)
    if not file_prefix:
        raise HTTPException(status_code=400, detail="未找到原文字幕文件")

    orig_srt = job_dir / f"{file_prefix}.srt"
    if not orig_srt.exists():
        raise HTTPException(status_code=400, detail="原文字幕文件不存在")

    profile_name = str(payload.get("online_profile", "")).strip()
    model_name = str(payload.get("online_model", "")).strip()
    target_lang = _normalize_lang_code(str(payload.get("target_lang", "zh")).strip())

    app_settings = load_app_settings()
    if not profile_name:
        _, active = load_profiles()
        profile_name = active

    display_name = orig_srt.name
    job = JobState(
        job_id=uuid.uuid4().hex,
        running=False,
        status="⏳ 翻译准备中",
        video_path=str(orig_srt),
        display_name=display_name,
        auto_translate=True,
    )
    job.current_job = job_dir.name
    job.current_prefix = file_prefix
    with _RUNTIME_LOCK:
        _ALL_JOBS[job.job_id] = job
        job.running = True

    _set_job_progress(job, "⏳ 翻译准备中", time.time(), progress_pct=1, eta_seconds=0, step_label="翻译启动")

    t = threading.Thread(
        target=_run_translate_worker, args=(job, profile_name, model_name, target_lang), daemon=True
    )
    t.start()

    return {"job_id": job.job_id, "folder_name": folder_name}


@app.get("/api/folders/all-output-files")
def api_all_output_files():
    """返回所有文件夹内的输出文件列表（用于「全部输出文件」视图），同时自动清理旧文本文件"""
    _cleanup_old_text_files()
    _OUTPUT_EXTS = {".zip", ".srt", ".txt", ".wav", ".vtt"}
    all_entries = []
    for folder in sorted(core.WORKSPACE_DIR.iterdir(), key=lambda x: x.name.lower()):
        if not folder.is_dir():
            continue
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file() or p.suffix.lower() not in _OUTPUT_EXTS:
                continue
            try:
                mtime = p.stat().st_mtime
                size = p.stat().st_size
                size_mb = size / (1024 * 1024)
            except OSError:
                mtime = 0.0
                size_mb = 0
            all_entries.append({
                "folder": folder.name,
                "name": p.name,
                "mtime": int(mtime),
                "size": size_mb,
            })
    return {"files": all_entries}


@app.post("/api/folders/download-multi")
def api_download_multi_files(payload: dict[str, list]):
    """下载多个文件（支持跨文件夹），items: [{folder, name}, ...]"""
    _OUTPUT_EXTS = {".zip", ".srt", ".txt", ".wav", ".vtt"}
    items = payload.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="未指定要下载的文件")
    output_files = []
    for item in items:
        folder_name = str(item.get("folder", "")).strip()
        file_name = str(item.get("name", "")).strip()
        if not folder_name or not file_name:
            continue
        folder = _resolve_workspace_folder(folder_name)
        file_path = folder / file_name
        if not file_path.exists() or not file_path.is_file():
            continue
        if file_path.suffix.lower() not in _OUTPUT_EXTS:
            continue
        try:
            file_path.resolve().relative_to(folder.resolve())
        except ValueError:
            continue
        output_files.append((folder_name, file_path))
    if not output_files:
        raise HTTPException(status_code=404, detail="未找到有效的输出文件")
    if len(output_files) == 1:
        return FileResponse(str(output_files[0][1]), filename=output_files[0][1].name)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for folder_name, fp in output_files:
            zf.write(fp, arcname=f"{folder_name}/{fp.name}")
    buffer.seek(0)
    zip_name = "selected-files.zip"
    headers = {"Content-Disposition": f'attachment; filename="{zip_name}"'}
    return StreamingResponse(buffer, media_type="application/zip", headers=headers)


@app.post("/api/folders/download-output")
def api_download_output_files(payload: dict):
    """下载选中文件夹内指定的输出文件（多文件打包为 ZIP，单文件直接下载）"""
    folder_name = str(payload.get("folder_name", "")).strip()
    files = payload.get("files", [])
    folder = _resolve_workspace_folder(folder_name)
    _OUTPUT_EXTS = {".zip", ".srt", ".txt", ".wav", ".vtt"}

    file_names = [str(f).strip() for f in files if str(f).strip()]
    if not file_names:
        raise HTTPException(status_code=400, detail="未指定要下载的文件")

    output_files = []
    for name in file_names:
        file_path = folder / name
        if not file_path.exists() or not file_path.is_file():
            continue
        if file_path.suffix.lower() not in _OUTPUT_EXTS:
            continue
        try:
            file_path.resolve().relative_to(folder.resolve())
        except ValueError:
            continue
        output_files.append(file_path)

    if not output_files:
        raise HTTPException(status_code=404, detail="未找到有效的输出文件")

    # 单个文件直接下载
    if len(output_files) == 1:
        return FileResponse(str(output_files[0]), filename=output_files[0].name)

    # 多个文件打包为 ZIP
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in output_files:
            zf.write(f, arcname=f.name)
    buffer.seek(0)

    zip_name = f"{folder.name}.selected.zip"
    headers = {"Content-Disposition": f'attachment; filename="{zip_name}"'}
    return StreamingResponse(buffer, media_type="application/zip", headers=headers)
def api_get_temp_file_settings():
    """获取临时文件保留数量设置"""
    return {
        "temp_video_keep_count": core.TEMP_VIDEO_KEEP_COUNT,
    }


@app.post("/api/settings/temp-files")
def api_set_temp_file_settings(payload: dict):
    """更新临时文件保留数量设置"""
    video_count = payload.get("temp_video_keep_count")
    messages = []

    if video_count is not None:
        try:
            video_count = int(video_count)
            if video_count < 1:
                video_count = 1
            elif video_count > 100:
                video_count = 100
            core.TEMP_VIDEO_KEEP_COUNT = video_count
            os.environ["TEMP_VIDEO_KEEP_COUNT"] = str(video_count)
            messages.append(f"临时视频保留 {video_count} 个")
        except (ValueError, TypeError):
            pass

    # 保存到 .env 文件
    try:
        env_path = Path(__file__).parent / ".env"
        lines = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()

        updated = False
        new_lines = []
        for line in lines:
            if line.startswith("TEMP_VIDEO_KEEP_COUNT="):
                if video_count is not None:
                    new_lines.append(f"TEMP_VIDEO_KEEP_COUNT={video_count}")
                    updated = True
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        if video_count is not None and not updated:
            new_lines.append(f"TEMP_VIDEO_KEEP_COUNT={video_count}")

        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as e:
        messages.append(f"保存失败: {e}")

    return {
        "message": " | ".join(messages) if messages else "已保存",
        "temp_video_keep_count": core.TEMP_VIDEO_KEEP_COUNT,
    }


@app.get("/api/model/profiles")
def api_profiles():
    profiles, active = load_profiles()
    app_settings = load_app_settings()
    active_profile = next((p for p in profiles if p.get("name") == active), profiles[0] if profiles else {})
    if isinstance(active_profile, dict):
        active_profile = dict(active_profile)
        active_profile["models"] = sorted(
            {str(m).strip() for m in active_profile.get("models", []) if str(m).strip()},
            key=lambda x: x.lower(),
        )
    return {
        "profile_names": [p.get("name", "") for p in profiles if p.get("name")],
        "active": active,
        "active_profile": active_profile,
        "app_settings": app_settings,
    }


@app.get("/api/model/profile")
def api_profile(name: str):
    profiles, _ = load_profiles()
    p = next((x for x in profiles if x.get("name") == name), None)
    if not p:
        raise HTTPException(status_code=404, detail="profile not found")
    profile = dict(p)
    profile["models"] = sorted(
        {str(m).strip() for m in profile.get("models", []) if str(m).strip()},
        key=lambda x: x.lower(),
    )
    return {"profile": profile}


@app.post("/api/model/profiles/fetch-models")
def api_fetch_models(payload: dict[str, str]):
    name = payload.get("name", "").strip()
    original_name = payload.get("original_name", "").strip()
    base_url = payload.get("base_url", "").strip()
    api_key = payload.get("api_key", "").strip()
    if not name or not base_url:
        raise HTTPException(status_code=400, detail="name/base_url required")
    if not api_key and not is_ollama_base_url(base_url):
        raise HTTPException(status_code=400, detail="api_key required for non-Ollama profiles")

    try:
        models = list_available_models(base_url, api_key, raise_on_error=True)
    except Exception as exc:
        message = str(exc)
        if "401" in message or "Unauthorized" in message:
            message = "API Key 无效或已过期，请检查配置组中的 api_key 是否正确。"
        raise HTTPException(status_code=400, detail=f"测试失败: {message}") from exc

    models = sorted({str(m).strip() for m in models if str(m).strip()}, key=lambda x: x.lower())
    profiles, _ = load_profiles()
    if any(str(p.get("name", "")).strip() == name for p in profiles) and original_name != name:
        raise HTTPException(status_code=409, detail="配置名重复，请使用其他名称")
    if original_name and original_name != name:
        profiles = delete_profile(profiles, original_name)
    profile: dict[str, Any] = next((p for p in profiles if p.get("name") == name), {"name": name})
    profile["base_url"] = base_url
    profile["api_key"] = api_key
    profile["models"] = models
    if models and not str(profile.get("default_model", "")).strip():
        profile["default_model"] = models[0]

    profiles = upsert_profile(profiles, profile)
    save_profiles(profiles, active_profile=name)
    return {
        "message": f"✅ 测试通过，获取到 {len(models)} 个模型并已写入 .env",
        "models": models,
        "default_model": profile.get("default_model", ""),
    }


@app.post("/api/model/profiles/save")
def api_save_profile(payload: dict[str, Any]):
    name = payload.get("name", "").strip()
    original_name = str(payload.get("original_name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")

    profiles, _ = load_profiles()
    if any(str(p.get("name", "")).strip() == name for p in profiles) and original_name != name:
        raise HTTPException(status_code=409, detail="配置名重复，请使用其他名称")
    if original_name and original_name != name:
        profiles = delete_profile(profiles, original_name)

    profile = next((p for p in profiles if p.get("name") == name), {"name": name, "models": []})
    profile["base_url"] = payload.get("base_url", "").strip()
    profile["api_key"] = payload.get("api_key", "").strip()
    profile["default_model"] = payload.get("default_model", "").strip()
    payload_models = payload.get("models", [])
    if isinstance(payload_models, list):
        profile["models"] = sorted(
            {str(m).strip() for m in payload_models if str(m).strip()},
            key=lambda x: x.lower(),
        )
    else:
        profile["models"] = sorted(
            {str(m).strip() for m in profile.get("models", []) if str(m).strip()},
            key=lambda x: x.lower(),
        )
    if profile["default_model"] and profile["default_model"] not in profile.get("models", []):
        profile["models"] = [profile["default_model"], *profile.get("models", [])]
        profile["models"] = sorted(
            {str(m).strip() for m in profile.get("models", []) if str(m).strip()},
            key=lambda x: x.lower(),
        )

    profiles = upsert_profile(profiles, profile)
    save_profiles(profiles, active_profile=name)
    save_app_settings(
        {
            "DEFAULT_BACKEND": str(payload.get("default_backend", "")).strip(),
            "DEFAULT_FUNASR_MODEL": str(payload.get("default_funasr_model", "")).strip(),
            "DEFAULT_WHISPER_MODEL": str(payload.get("default_whisper_model", "")).strip(),
            "AUTO_SUBTITLE_LANG": _normalize_subtitle_priority(str(
                payload.get("auto_subtitle_lang", payload.get("auto_subtitle_langs", ""))
            ).strip()),
        }
    )
    return {"message": "✅ 配置已保存", "active": name}


@app.post("/api/app-settings/subtitle-priority")
def api_save_subtitle_priority(payload: dict[str, str]):
    value = _normalize_subtitle_priority(str(payload.get("auto_subtitle_lang", "")).strip() or "zh")
    save_app_settings({"AUTO_SUBTITLE_LANG": value})
    return {"message": "ok", "AUTO_SUBTITLE_LANG": value}


@app.post("/api/app-settings/ui-preferences")
def api_save_ui_preferences(payload: dict[str, str]):
    """保存主页 UI 偏好（后端、模型、语言、设备、复选框状态）到 .env"""
    mapping = {
        "backend": "DEFAULT_BACKEND",
        "funasr_model": "DEFAULT_FUNASR_MODEL",
        "whisper_model": "DEFAULT_WHISPER_MODEL",
        "language": "DEFAULT_LANGUAGE",
        "device": "DEFAULT_DEVICE",
        "auto_translate": "AUTO_TRANSLATE",
        "auto_download": "AUTO_DOWNLOAD",
        "direct_save": "DIRECT_SAVE",
    }
    updates = {}
    for js_key, env_key in mapping.items():
        if js_key in payload:
            val = str(payload[js_key]).strip()
            updates[env_key] = val
    if updates:
        save_app_settings(updates)
    return {"message": "ok"}


@app.post("/api/model/profiles/delete")
def api_delete_profile(payload: dict[str, str]):
    name = payload.get("name", "").strip()
    profiles, active = load_profiles()
    profiles = delete_profile(profiles, name)
    save_profiles(profiles, active_profile=(active if active != name else None))
    return {"message": f"✅ 已删除配置: {name}"}


def _find_ytdlp() -> str:
    """找到最合适的 yt-dlp 可执行文件，优先使用与当前 Python 同环境的新版本。"""
    home = Path.home()
    candidates = [
        Path(sys.executable).parent / "yt-dlp",      # 同 venv/env
        home / ".local" / "bin" / "yt-dlp",
        home / "miniconda3" / "bin" / "yt-dlp",
        home / "miniconda" / "bin" / "yt-dlp",
        home / "anaconda3" / "bin" / "yt-dlp",
        home / "miniforge3" / "bin" / "yt-dlp",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("yt-dlp") or "yt-dlp"


def _ytdlp_js_runtime_args() -> list[str]:
    """检测可用的 JS 运行时，返回 yt-dlp 需要的 --js-runtimes 参数。"""
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            return ["--js-runtimes", name]
    return []


@app.post("/api/download_url")
def api_download_url(payload: dict):
    """使用 XHS-Downloader（小红书）或 yt-dlp 下载视频或音频。"""
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL 不能为空")

    # 检测小红书链接，优先使用 XHS-Downloader
    if is_xiaohongshu_url(url):
        return _download_xiaohongshu(url)

    return _download_with_ytdlp(url, payload)


def _download_xiaohongshu(url: str) -> dict:
    """使用 XHS-Downloader 下载小红书视频"""
    project_root = Path(__file__).resolve().parent
    core.TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    # 检查 XHS-Downloader API 服务是否可用
    client = get_xhs_client()
    if not client.check_server():
        raise HTTPException(
            status_code=503,
            detail="XHS-Downloader 服务未启动。请先启动 XHS-Downloader API 服务:\n"
                   "cd /path/to/XHS-Downloader && python main.py api"
        )

    # 下载视频
    result = download_xhs_video(url, output_dir=core.TEMP_VIDEO_DIR, timeout=120)

    if not result.success:
        raise HTTPException(
            status_code=500,
            detail=f"小红书视频下载失败: {result.error}"
        )

    # 清理临时目录
    core._prune_temp_video_dir()

    media_path = Path(result.file_path)
    return {
        "filepath": media_path.relative_to(project_root).as_posix(),
        "filename": result.file_name,
        "auto_subtitle": False,
        "xhs_note_id": result.note_id,
        "xhs_title": result.note_title,
        "xhs_author": result.author_name,
    }


def _download_with_ytdlp(url: str, payload: dict) -> dict:
    """使用 yt-dlp 下载视频"""
    app_settings = load_app_settings()
    subtitle_priority = _normalize_subtitle_priority(
        str(payload.get("auto_subtitle_lang", "")).strip()
        or str(payload.get("auto_subtitle_langs", "")).strip()
        or app_settings["AUTO_SUBTITLE_LANG"]
    )
    sub_langs = SUBTITLE_PRIORITY_PRESETS.get(subtitle_priority, SUBTITLE_PRIORITY_PRESETS["zh"])
    disable_auto_subs = subtitle_priority == "none"

    ytdlp_bin = _find_ytdlp()
    if not Path(ytdlp_bin).is_file() and not shutil.which(ytdlp_bin):
        raise HTTPException(status_code=500, detail="yt-dlp 未安装，请运行: pip install yt-dlp")

    project_root = Path(__file__).resolve().parent

    login_kw = ["login", "sign in", "private", "not logged in",
                "authentication required", "subscriber", "members only",
                "premium", "http error 401", "http error 403"]

    def _is_login_err(text: str) -> bool:
        t = text.lower()
        return any(k in t for k in login_kw)

    def _detect_wsl_firefox_profile() -> str | None:
        """在 WSL2 环境下自动查找 Windows Firefox 的 default-release profile 路径"""
        mnt = Path("/mnt/c")
        if not mnt.is_dir():
            return None
        # 遍历 Windows 用户目录
        users_dir = mnt / "Users"
        if not users_dir.is_dir():
            return None
        for user in users_dir.iterdir():
            if user.name.startswith(".") or user.name in ("All Users", "Default", "Default User", "Public", "desktop.ini"):
                continue
            ff_dir = user / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
            if not ff_dir.is_dir():
                continue
            # 优先 default-release
            for prof in sorted(ff_dir.iterdir(), key=lambda p: (".default-release" not in p.name, p.name)):
                if (prof / "cookies.sqlite").is_file():
                    return str(prof)
        return None

    _wsl_ff_profile = _detect_wsl_firefox_profile()
    _proxy_url = app_settings.get("DOWNLOAD_PROXY", "").strip()

    def _proxy_args() -> list[str]:
        return ["--proxy", _proxy_url] if _proxy_url else []

    def _cookie_args(browser: str | None) -> list[str]:
        # 优先使用上传的 cookies.txt 文件
        cookie_file = project_root / "cookies.txt"
        if cookie_file.exists():
            return ["--cookies", str(cookie_file)]
        # WSL2 下优先使用 Windows Firefox cookie
        if _wsl_ff_profile and browser == "firefox":
            return ["--cookies-from-browser", f"firefox:{_wsl_ff_profile}"]
        return ["--cookies-from-browser", browser] if browser else []

    _js_args = _ytdlp_js_runtime_args()

    def _get_title(cookie_extra: list[str]) -> str | None:
        """先用 --simulate 拿 title，不产生任何文件。"""
        cmd = [ytdlp_bin, "--no-playlist", "--simulate", "--remote-components", "ejs:github",
               "--print", "title"] + _js_args + _proxy_args() + cookie_extra + [url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
            return lines[-1] if lines else None
        return None

    def _download(dest_dir: Path, cookie_extra: list[str]) -> tuple[int, str, str]:
        output_tmpl = str(dest_dir)
        cmd = [ytdlp_bin, "--no-playlist", "--remote-components", "ejs:github",
               "-f", "bestaudio[ext=m4a]/bestaudio/best"] + _js_args + _proxy_args()
        if not disable_auto_subs:
            cmd.extend([
                "--write-auto-subs",
                "--sub-langs", sub_langs,
                "--sub-format", "srt/best",
                "--convert-subs", "srt",
            ])
        cmd.extend([
            "--no-mtime",
            "-o", output_tmpl,
            "--print", "after_move:filepath",
        ])
        cmd = cmd + cookie_extra + [url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return r.returncode, r.stdout, r.stderr

    def _extract_path(stdout: str) -> str:
        lines = [l for l in stdout.strip().splitlines() if l.strip()]
        return lines[-1] if lines else ""

    def _title_to_dir(title: str) -> Path:
        safe = re.sub(r'[/\x00]', '', title).strip()[:120] or "media"
        core.TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        return core.TEMP_VIDEO_DIR / f"{safe}.%(id)s.%(ext)s"

    # 尝试顺序：WSL2 下 Firefox 优先；否则按原顺序
    if _wsl_ff_profile:
        browser_attempts: list[str | None] = ["firefox", "chrome", "chromium", "edge", None]
    else:
        browser_attempts: list[str | None] = ["chrome", "chromium", "firefox", "edge", None]

    for browser in browser_attempts:
        extra = _cookie_args(browser)
        # 先拿 title
        try:
            title = _get_title(extra)
        except subprocess.TimeoutExpired:
            continue
        if title is None:
            # simulate 失败，检查是否登录错误
            continue

        dest_dir = _title_to_dir(title)
        # 正式下载
        try:
            rc, out, err = _download(dest_dir, extra)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="下载超时（最多 10 分钟）")

        if rc == 0:
            fp = _extract_path(out)
            if fp and Path(fp).exists():
                core._prune_temp_video_dir()
                media_path = Path(fp)
                subtitle_path = _pick_downloaded_subtitle(media_path)
                payload = {
                    "filepath": media_path.relative_to(project_root).as_posix(),
                    "filename": media_path.name,
                    "auto_subtitle": bool(subtitle_path),
                }
                if subtitle_path:
                    payload["subtitle_path"] = subtitle_path.relative_to(project_root).as_posix()
                    payload["subtitle_name"] = subtitle_path.name
                return payload

        combined = out + err
        if _is_login_err(combined):
            raise HTTPException(status_code=401,
                detail="该视频需要登录才能访问，请先在浏览器中登录目标网站，然后重试。")

    # 全部尝试失败
    raise HTTPException(status_code=500, detail="下载失败：无法获取视频信息，请检查 URL 或网络")


@app.get("/api/download_file")
def api_download_file(path: str):
    """下载临时目录中的文件到本地"""
    if not path:
        raise HTTPException(status_code=400, detail="path 参数不能为空")

    project_root = Path(__file__).resolve().parent
    target = (project_root / path).resolve()

    # 安全检查：确保文件在项目目录内
    if project_root.resolve() not in target.parents and target.resolve() != project_root:
        raise HTTPException(status_code=403, detail="禁止访问项目目录外的文件")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(str(target), filename=target.name)


@app.post("/api/upload_cookie")
def api_upload_cookie(cookie_file: UploadFile = File(...)):
    """上传 Cookie 文件用于需要登录的平台下载"""
    if not cookie_file.filename:
        raise HTTPException(status_code=400, detail="未选择文件")

    # 只接受 .txt 文件
    if not cookie_file.filename.lower().endswith('.txt'):
        raise HTTPException(status_code=400, detail="只支持 .txt 格式的 Cookie 文件")

    # 保存到项目根目录
    project_root = Path(__file__).resolve().parent
    cookie_path = project_root / "cookies.txt"

    try:
        with cookie_path.open("wb") as f:
            shutil.copyfileobj(cookie_file.file, f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存 Cookie 文件失败：{e}")

    return {"filename": cookie_file.filename, "path": str(cookie_path)}


@app.post("/api/jobs/import-subtitle")
def api_job_import_subtitle(payload: dict[str, str]):

    history_video = (payload.get("history_video") or "").strip()
    subtitle_path_input = (payload.get("subtitle_path") or "").strip()
    if not history_video or not subtitle_path_input:
        raise HTTPException(status_code=400, detail="history_video 和 subtitle_path 必填")

    media_path = core._resolve_input_path(None, history_video) or ""
    if not media_path:
        raise HTTPException(status_code=400, detail="无效的媒体路径")

    base = Path(__file__).resolve().parent
    subtitle_path = Path(subtitle_path_input)
    if not subtitle_path.is_absolute():
        subtitle_path = (base / subtitle_path).resolve()
    media = Path(media_path).resolve()
    if not media.exists() or not media.is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    if not subtitle_path.exists() or not subtitle_path.is_file():
        raise HTTPException(status_code=404, detail="字幕文件不存在")

    job = JobState(job_id=uuid.uuid4().hex, running=True, status="⏳ 正在导入平台自动字幕")
    with _RUNTIME_LOCK:
        _ALL_JOBS[job.job_id] = job
    _set_job_progress(job, "⏳ 正在导入平台自动字幕", time.time(), progress_pct=10, eta_seconds=0, step_label="导入字幕")

    def runner():
        _run_subtitle_import_worker(job, str(media), str(subtitle_path))

    threading.Thread(target=runner, daemon=True).start()
    return {"job_id": job.job_id}


@app.post("/api/transcribe/start")
def api_transcribe_start(
    history_video: str = Form(default=""),
    backend: str = Form(default=""),
    language: str = Form(default="自动检测"),
    whisper_model: str = Form(default=""),
    funasr_model: str = Form(default=""),
    device: str = Form(default="CUDA"),
    video_file: UploadFile | None = File(default=None),
    auto_translate: str = Form(default="0"),
    auto_download: str = Form(default="0"),
):

    app_settings = load_app_settings()
    backend = backend or app_settings["DEFAULT_BACKEND"]
    whisper_model = whisper_model or app_settings["DEFAULT_WHISPER_MODEL"]
    funasr_model = funasr_model or app_settings["DEFAULT_FUNASR_MODEL"]

    video_path = ""
    temp_path: Path | None = None
    if video_file is not None and video_file.filename:
        orig_name = Path(video_file.filename).name
        if not core._is_supported_media_path(orig_name):
            raise HTTPException(status_code=400, detail="仅支持视频或音频文件上传")
        core.TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)

        temp_upload_path = core.TEMP_VIDEO_DIR / f".upload_{uuid.uuid4().hex}"
        try:
            with temp_upload_path.open("wb") as f:
                shutil.copyfileobj(video_file.file, f)

            duplicate = core._find_duplicate_file(temp_upload_path, core.TEMP_VIDEO_DIR)
            if duplicate:
                temp_upload_path.unlink()
                try:
                    duplicate.touch()
                except OSError:
                    pass
                video_path = str(duplicate)
            else:
                save_path = core._unique_file_path(core.TEMP_VIDEO_DIR, orig_name)
                temp_upload_path.rename(save_path)
                video_path = str(save_path)
        finally:
            if temp_upload_path.exists():
                temp_upload_path.unlink()

        core._prune_temp_video_dir()
    elif history_video:
        video_path = core._resolve_input_path(None, history_video) or ""
        if video_path and not core._is_supported_media_path(video_path):
            raise HTTPException(status_code=400, detail="当前文件不是受支持的视频或音频格式")

    if not video_path:
        raise HTTPException(status_code=400, detail="请上传文件或选择当前视频")

    display_name = Path(video_path).name if video_path else ""
    do_auto_translate = auto_translate in ("1", "true", "yes")
    do_auto_download = auto_download in ("1", "true", "yes")
    job = JobState(
        job_id=uuid.uuid4().hex,
        running=False,
        status="⏳ 任务准备中",
        video_path=video_path,
        display_name=display_name,
        translate_params={
            "backend": backend,
            "language": language,
            "whisper_model": whisper_model,
            "funasr_model": funasr_model,
            "device": device,
        },
        auto_translate=do_auto_translate,
        auto_download=do_auto_download,
    )

    with _RUNTIME_LOCK:
        _ALL_JOBS[job.job_id] = job

    # 解析翻译配置
    translate_base_url = ""
    translate_api_key = ""
    translate_model_name = ""
    target_lang = "zh"
    if do_auto_translate:
        try:
            profiles, active = load_profiles()
            profile = next((p for p in profiles if p.get("name") == active), profiles[0] if profiles else {})
            translate_base_url = str(profile.get("base_url", "")).strip()
            translate_api_key = str(profile.get("api_key", "")).strip()
            translate_model_name = str(profile.get("default_model", "")).strip()
            app_settings = load_app_settings()
            target_lang = _normalize_lang_code(app_settings.get("DEFAULT_TARGET_LANG", "zh"))
        except Exception:
            pass

    # 解析 ASR 后端类名
    asr_cls_name = _display_name_to_asr_cls(backend)
    # 解析模型名
    asr_model = ""
    if "FunASR" in backend:
        asr_model = funasr_model
    else:
        asr_model = whisper_model

    ptask = PipelineTask(
        task_id=job.job_id,
        video_path=video_path,
        asr_backend=asr_cls_name,
        model_name=asr_model,
        language=language,
        device=device,
        auto_translate=do_auto_translate,
        translate_backend="SiliconFlowTranslate",
        translate_model=translate_model_name,
        translate_base_url=translate_base_url,
        translate_api_key=translate_api_key,
        target_lang=target_lang,
        status_cb=_on_pipeline_status,
        log_cb=_on_pipeline_log,
        progress_cb=_on_pipeline_progress,
    )

    logger.info(f"任务 {job.job_id} 提交到 Pipeline, auto_translate={do_auto_translate}")
    get_pipeline().submit(ptask)
    # 返回相对路径供前端显示
    display_path = ""
    if video_path:
        try:
            display_path = str(Path(video_path).relative_to(Path(__file__).parent))
        except ValueError:
            display_path = video_path
    return {"job_id": job.job_id, "video_path": display_path, "queued": False}


@app.get("/api/queue/status")
def api_queue_status():
    return _get_queue_status()


@app.get("/api/backends")
def api_list_backends():
    """返回可用的 ASR/翻译后端列表及其元信息。"""
    from backends import list_asr_backends, list_translate_backends, get_asr_backend_info
    asr = []
    for cls_name in list_asr_backends():
        try:
            asr.append(get_asr_backend_info(cls_name))
        except Exception:
            asr.append({"class_name": cls_name, "error": "加载失败"})
    translate = list_translate_backends()
    return {"asr_backends": asr, "translate_backends": translate}


@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str):
    job = _get_job(job_id)
    return _json_job(job)


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str):
    """SSE 端点：任务状态变更时推送，进度 1% 门槛节流。"""
    job = _get_job(job_id)
    q = _sse_subscribe(job_id)

    async def _event_stream():
        try:
            # 先推送一次当前完整状态
            yield f"data: {json.dumps(_json_job(job), ensure_ascii=False)}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(
                        asyncio.to_thread(q.get, True, 30),
                        timeout=35,
                    )
                except (_queue_mod.Empty, asyncio.TimeoutError):
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                if data.get("done"):
                    break
        finally:
            _sse_unsubscribe(job_id, q)

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@app.post("/api/jobs/{job_id}/stop")
def api_job_stop(job_id: str):
    _ = _get_job(job_id)
    core.STOP_EVENT.set()
    return {"message": "stop requested"}


@app.post("/api/jobs/{job_id}/translate")
def api_job_translate(job_id: str, payload: dict[str, Any]):
    job = _get_job(job_id)
    with _RUNTIME_LOCK:
        if job.running:
            raise HTTPException(status_code=409, detail="当前任务仍在运行")
        job.running = True
        job.done = False
        job.failed = False
    _set_job_progress(job, "⏳ 正在开始翻译...", time.time(), progress_pct=2, eta_seconds=0, step_label="翻译启动")

    profile = payload.get("online_profile", "")
    model = payload.get("online_model", "")
    target_lang = _normalize_lang_code(payload.get("target_lang", "zh"))

    try:
        parallel_threads = int(payload.get("parallel_threads", 5))
    except (ValueError, TypeError):
        parallel_threads = 5
    from utils.translate import set_parallel_threads
    set_parallel_threads(parallel_threads)

    threading.Thread(target=_run_translate_worker, args=(job, profile, model, target_lang), daemon=True).start()
    return {"message": "translate started"}


@app.get("/api/jobs/{job_id}/download/{kind}")
def api_job_download(job_id: str, kind: str):
    """Legacy endpoint: redirect to single zip bundle."""
    job = _get_job(job_id)
    path = job.zip_bundle
    if not path or not Path(path).exists():
        if not job.current_job:
            raise HTTPException(status_code=404, detail="bundle not found")
        job_dir = core.WORKSPACE_DIR / job.current_job
        file_prefix = core._resolve_file_prefix(job_dir, job.current_prefix)
        if not file_prefix:
            raise HTTPException(status_code=404, detail="bundle not found")
        try:
            path = _build_all_bundle(job_dir, file_prefix)
            job.zip_bundle = path
        except Exception:
            raise HTTPException(status_code=404, detail="bundle not found")
    return FileResponse(path, filename=Path(path).name)


@app.get("/api/jobs/{job_id}/files")
def api_job_files(job_id: str):
    job = _get_job(job_id)
    if not job.current_job:
        return {"files": []}

    job_dir = core.WORKSPACE_DIR / job.current_job
    if not job_dir.exists() or not job_dir.is_dir():
        return {"files": []}

    file_prefix = core._resolve_file_prefix(job_dir, job.current_prefix)
    files = [
        p.name
        for p in sorted(job_dir.iterdir(), key=lambda x: x.name.lower())
        if p.is_file() and (
            (file_prefix and _is_final_output_file(p.name, file_prefix))
            or p.suffix.lower() == ".zip"
        )
    ]
    return {"files": files}


@app.get("/api/jobs/{job_id}/download-file")
def api_job_download_file(job_id: str, file_name: str):
    job = _get_job(job_id)
    if not job.current_job:
        raise HTTPException(status_code=404, detail="job output not found")

    clean_name = (file_name or "").strip()
    if not clean_name or Path(clean_name).name != clean_name:
        raise HTTPException(status_code=400, detail="invalid file_name")

    job_dir = core.WORKSPACE_DIR / job.current_job
    target = (job_dir / clean_name).resolve()
    if job_dir.resolve() not in target.parents or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    if target.suffix.lower() not in {".srt", ".txt", ".zip"}:
        raise HTTPException(status_code=400, detail="unsupported file type")

    return FileResponse(str(target), filename=target.name)


@app.post("/api/external/process")
def api_external_process(payload: dict[str, Any]):
    """
    第三方统一调用入口：
    - 输入: base64/url/history
    - 可指定: backend/funasr_model/whisper_model/device/auto_subtitle_lang/target_lang/online_profile/online_model
    - 输出: zip 文件（二进制）或 JSON（含 zip_base64）
    """
    app_settings = load_app_settings()

    auto_subtitle_lang = _normalize_subtitle_priority(
        str(payload.get("auto_subtitle_lang", "")).strip() or app_settings["AUTO_SUBTITLE_LANG"]
    )
    backend = str(payload.get("backend", "")).strip() or app_settings["DEFAULT_BACKEND"]
    whisper_model = str(payload.get("whisper_model", "")).strip() or app_settings["DEFAULT_WHISPER_MODEL"]
    funasr_model = str(payload.get("funasr_model", "")).strip() or app_settings["DEFAULT_FUNASR_MODEL"]
    language = str(payload.get("language", "自动检测")).strip() or "自动检测"
    device = str(payload.get("device", "CUDA")).strip() or "CUDA"

    target_lang_raw = str(payload.get("target_lang", "")).strip().lower()
    do_translate = target_lang_raw not in {"", "none", "off", "false", "no"}
    target_lang = _normalize_lang_code(target_lang_raw or "zh")
    profile_name = str(payload.get("online_profile", "")).strip()
    online_model = str(payload.get("online_model", "")).strip()

    output_mode = str(payload.get("output_mode", "binary")).strip().lower() or "binary"

    with _RUNTIME_LOCK:
        pass  # 允许并行处理

    media_path, subtitle_path = _resolve_external_input(payload, auto_subtitle_lang)

    job = JobState(job_id=uuid.uuid4().hex, running=True, status="⏳ 外部任务处理中")
    _set_job_progress(job, "⏳ 外部任务处理中", time.time(), progress_pct=2, eta_seconds=0, step_label="任务启动")

    # URL 命中自动字幕且未强制 ASR 时，直接导入字幕。
    force_asr = bool(payload.get("force_asr", False))
    if subtitle_path and not force_asr:
        _run_subtitle_import_worker(job, media_path, subtitle_path)
    else:
        _run_transcribe_worker(
            job,
            media_path,
            backend,
            language,
            whisper_model,
            funasr_model,
            device,
        )

    if job.failed:
        raise HTTPException(status_code=500, detail=f"转录失败: {job.status}")

    if do_translate:
        job.running = True
        job.done = False
        job.failed = False
        _run_translate_worker(job, profile_name, online_model, target_lang)
        if job.failed:
            raise HTTPException(status_code=500, detail=f"翻译失败: {job.status}")

    zip_path, files = _collect_job_outputs(job)

    if output_mode in {"binary", "zip", "download"}:
        return FileResponse(str(zip_path), filename=zip_path.name, media_type="application/zip")

    if output_mode in {"base64", "json-base64"}:
        zip_b64 = base64.b64encode(zip_path.read_bytes()).decode("ascii")
        return {
            "job_id": job.job_id,
            "status": job.status,
            "current_job": job.current_job,
            "files": files,
            "zip_name": zip_path.name,
            "zip_base64": zip_b64,
        }

    return {
        "job_id": job.job_id,
        "status": job.status,
        "current_job": job.current_job,
        "files": files,
        "zip_name": zip_path.name,
        "download_path": f"/api/jobs/{job.job_id}/download-file?file_name={zip_path.name}",
    }


def main():
    import argparse
    import uvicorn

    app_settings = load_app_settings()

    parser = argparse.ArgumentParser(description="video2text FastAPI")
    parser.add_argument("--port", type=int, default=int(app_settings["APP_PORT"]))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--ssl-certfile", default=None)
    parser.add_argument("--ssl-keyfile", default=None)
    args = parser.parse_args()

    uvicorn.run(
        "fastapi_app:app",
        host=args.host,
        port=args.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        reload=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
