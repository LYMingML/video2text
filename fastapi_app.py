#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import main as core
from utils.online_models import delete_profile, load_profiles, save_profiles, upsert_profile
from utils.subtitle import collect_plain_text, normalize_segments_timeline, save_plain, save_srt, segments_to_plain
from utils.translate import list_available_models, translate_segments

app = FastAPI(title="video2text-fastapi")


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

    def add_log(self, msg: str):
        self.logs.append(msg)
        if len(self.logs) > 500:
            self.logs = self.logs[-350:]


_RUNTIME_LOCK = threading.Lock()
_RUNTIME_JOB: JobState | None = None
_RUNTIME_THREAD: threading.Thread | None = None


def _get_job(job_id: str) -> JobState:
    global _RUNTIME_JOB
    if not _RUNTIME_JOB or _RUNTIME_JOB.job_id != job_id:
        raise HTTPException(status_code=404, detail="job not found")
    return _RUNTIME_JOB


def _set_job_state(job: JobState):
    global _RUNTIME_JOB
    with _RUNTIME_LOCK:
        _RUNTIME_JOB = job


def _json_job(job: JobState) -> dict[str, Any]:
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

        job_dir = core._make_job_dir(video_path)
        orig_name = p.name
        file_prefix = p.stem
        dest = job_dir / orig_name
        if not dest.exists():
            shutil.copy2(video_path, dest)

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

        save_srt(cleaned_segments, str(job_dir / f"{file_prefix}.orig.srt"), normalize=False)
        save_plain(cleaned_segments, str(job_dir / f"{file_prefix}.orig.txt"), normalize=False)
        save_srt(cleaned_segments, str(job_dir / f"{file_prefix}.srt"), normalize=False)
        save_plain(cleaned_segments, str(job_dir / f"{file_prefix}.txt"), normalize=False)
        try:
            job.zip_bundle = _build_all_bundle(job_dir, file_prefix)
        except Exception:
            pass

        _set_job_progress(
            job,
            f"✅ 原文识别完成（100%）→ workspace/{job_dir.name}/（点击翻译并选择目标语言）",
            t0,
            progress_pct=100,
            eta_seconds=0,
            step_label="识别完成",
        )
        job.plain_text = plain_text or segments_to_plain(cleaned_segments, normalize=False)
        job.done = True
        job.running = False
    except Exception as exc:
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


def _build_all_bundle(job_dir: Path, file_prefix: str) -> str:
    """将 job_dir 下所有属于该 prefix 的 .srt/.txt 文件打包为单个 zip。"""
    files = sorted(
        p for p in job_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in {".srt", ".txt"}
        and p.stem.startswith(file_prefix)
    )
    if not files:
        raise FileNotFoundError(f"未找到可打包的 srt/txt 文件（prefix={file_prefix}）")
    bundle_path = job_dir / f"{file_prefix}.zip"
    with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return str(bundle_path)


def _run_translate_worker(job: JobState, profile_name: str, model_name: str, target_lang: str):
    t0 = time.time()
    try:
        if not job.current_job:
            raise RuntimeError("当前任务不存在")

        job_dir = core.WORKSPACE_DIR / job.current_job
        file_prefix = core._resolve_file_prefix(job_dir, job.current_prefix)
        if not file_prefix:
            raise RuntimeError("未找到原文字幕")

        orig_srt = job_dir / f"{file_prefix}.orig.srt"
        if not orig_srt.exists():
            fallback = job_dir / f"{file_prefix}.srt"
            orig_srt = fallback if fallback.exists() else orig_srt

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

        translated: list[tuple[float, float, str]] = []
        total = len(segments)
        start_ts = time.time()
        _set_job_progress(job, "⏳ 翻译准备中", t0, progress_pct=3, step_label="翻译准备")

        for idx, seg in enumerate(segments, start=1):
            part = translate_segments(
                [seg],
                source_lang=source_lang,
                target_lang=use_target_lang,
                log_cb=job.add_log,
                base_url=use_base_url,
                api_key=use_api_key,
                model_name=use_model,
            )
            translated.extend(part)
            elapsed = max(0.001, time.time() - start_ts)
            eta = max(0.0, (total - idx) * (elapsed / idx))
            h = int(eta) // 3600
            m = (int(eta) % 3600) // 60
            s = int(eta) % 60
            pct = int(idx * 100 / max(total, 1))
            _set_job_progress(
                job,
                f"⏳ 翻译进度：{pct}%｜预计剩余 {h:02d}:{m:02d}:{s:02d}",
                t0,
                progress_pct=pct,
                eta_seconds=eta,
                step_label="翻译中",
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
        _set_job_progress(
            job,
            f"✅ 翻译完成（100%，目标语言: {use_target_lang}）：workspace/{job.current_job}/",
            t0,
            progress_pct=100,
            eta_seconds=0,
            step_label="翻译完成",
        )
        job.done = True
        job.running = False
    except Exception as exc:
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
.toolbar{display:flex;gap:8px;flex-wrap:wrap}
.toolbar.tight button{min-width:0}
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
            </div>
        </aside>

        <main class="content-area">
    <div id="page-home" class="page active">
        <section class="workspace">
            <article class="card panel-left stack">
                <h3>任务输入与操作</h3>
                <p class="muted">上传新视频，或复用历史视频，按步骤执行转录与翻译。</p>
                <div class="drag-zone" id="dz-home-input">
                    <div class="drag-row">
                        <div class="drag-item toolbar tight" draggable="true" style="flex:none">
                            <button onclick="startTranscribe()">开始转录</button>
                            <button class="btn-danger" onclick="stopJob()">停止</button>
                            <button onclick="startTranslate()">翻译</button>
                        </div>
                    </div>
                    <div class="drag-row">
                        <div class="drag-item field" draggable="true">
                            <label for="videoFile">上传视频/音频</label>
                            <input type="file" id="videoFile" />
                        </div>
                        <div class="drag-item field" draggable="true">
                            <label for="historyVideo">历史视频</label>
                            <select id="historyVideo"></select>
                        </div>
                        <div class="drag-item field" draggable="true">
                            <label for="statusText">状态</label>
                            <input id="statusText" class="status" readonly />
                        </div>
                    </div>
                    <div class="drag-row">
                        <div class="drag-item field" draggable="true">
                            <label for="homeBackendSel">识别后端</label>
                            <select id="homeBackendSel" onchange="onHomeBackendChange()">
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
                            <select id="homeBackendModelSel" class="backend-model-select"></select>
                        </div>
                    </div>
                    <div class="drag-row">
                        <div class="drag-item field" draggable="true">
                            <label for="homeModelSel">翻译模型</label>
                            <input id="homeModelSearch" placeholder="筛选翻译模型" oninput="filterModels('homeModelSearch','homeModelSel')" />
                            <select id="homeModelSel"></select>
                        </div>
                    </div>
                    <div class="drag-row">
                        <div class="drag-item toolbar tight" draggable="true" style="flex:none">
                            <button onclick="downloadOutputZip()">下载输出文件</button>
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
                        <div class="summary-kv"><b>历史文件夹</b><span id="homeSummaryFolder">未选择</span></div>
                        <div class="summary-kv"><b>历史文本</b><span id="homeSummaryText">未选择</span></div>
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
                <p class="muted">统一管理历史任务目录，支持删除与快速刷新。</p>
                <div class="toolbar">
                    <button onclick="refreshHistory()">刷新历史</button>
                    <button class="btn-danger" onclick="deleteFolder()">删除选中</button>
                </div>
                <div class="field compact">
                    <label for="folderSearch">搜索历史文件夹</label>
                    <input id="folderSearch" placeholder="输入关键字筛选文件夹" oninput="applyFolderFilter()" />
                </div>
                <div id="fileFolderSelectionState" class="selection-hint">当前已选：未选择历史文件夹</div>
                <select id="folderSelect" onchange="refreshTextFiles()" style="display:none;"></select>
                <div class="field">
                    <label for="folderIconGrid">全部历史文件夹（图标位）</label>
                    <div id="folderIconGrid" class="icon-grid"></div>
                </div>
            </article>

            <article class="card panel-right stack">
                <h3>下载历史文本</h3>
                <p class="muted">选中文件夹后，可浏览并下载其中的 `.txt` 文件。</p>
                <div class="field compact">
                    <label for="textFileSearch">搜索历史文本</label>
                    <input id="textFileSearch" placeholder="输入关键字筛选文本文件" oninput="applyTextFileFilter()" />
                </div>
                <div id="fileTextSelectionState" class="selection-hint">当前已选：未选择历史文本</div>
                <select id="textFileSelect" style="display:none;"></select>
                <div class="field">
                    <label for="textFileIconGrid">全部历史文本（图标位）</label>
                    <div id="textFileIconGrid" class="icon-grid"></div>
                </div>
                <div class="toolbar">
                    <button onclick="refreshTextFiles()">刷新文本列表</button>
                    <button onclick="downloadTextFile()">下载选中文本</button>
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
                <div class="params-grid-3">
                    <div class="field compact">
                        <label for="backendSel">识别后端</label>
                        <select id="backendSel">
                            <option>FunASR（Paraformer）</option>
                            <option>faster-whisper（多语言）</option>
                        </select>
                    </div>
                    <div class="field compact">
                        <label for="langSel">语言</label>
                        <select id="langSel">
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
                        <select id="deviceSel">
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
        </main>
    </div>
</div>

<script>
let currentJobId = "";
let pollTimer = null;
let foldersList = [];
let textFilesMeta = [];
let profileNamesList = [];
let folderFilterKeyword = '';
let textFileFilterKeyword = '';
let profileFilterKeyword = '';
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
    const selected = document.getElementById('folderSelect')?.value || '';
    renderIconGrid(
        'folderIconGrid',
        foldersList.map(v=>({value:v, label:v, meta:'任务目录'})),
        selected,
        selectFolder,
        folderFilterKeyword
    );
}

function applyTextFileFilter(){
    textFileFilterKeyword = (document.getElementById('textFileSearch')?.value || '').trim().toLowerCase();
    const selected = document.getElementById('textFileSelect')?.value || '';
    renderIconGrid(
        'textFileIconGrid',
        textFilesMeta.map(item=>({
            value:item.name,
            label:item.name,
            meta:item.mtime ? formatRelativeTime(item.mtime) : '未知时间',
            title:item.mtime ? `修改时间: ${formatAbsoluteTime(item.mtime)}` : '修改时间: 未知'
        })),
        selected,
        selectTextFile,
        textFileFilterKeyword
    );
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
    document.getElementById('profileName').value = '';
    document.getElementById('baseUrl').value = '';
    document.getElementById('apiKey').value = '';
    setSearchableModelOptions('modelSearch', 'modelSel', [], '');
    document.getElementById('modelStatus').value = '';
    document.getElementById('profileName').focus();
}

function selectFolder(folderName){
    const select = document.getElementById('folderSelect');
    if(!select) return;
    select.value = folderName;
    select.dispatchEvent(new Event('change', {bubbles:true}));
    syncSelectionStates();
}

function selectTextFile(fileName){
    const select = document.getElementById('textFileSelect');
    if(!select) return;
    select.value = fileName;
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

function syncSelectionStates(){
    const backend = document.getElementById('homeBackendSel')?.value || '';
    const backendModel = document.getElementById('homeBackendModelSel')?.value || '';
    const homeModel = document.getElementById('homeModelSel')?.value || '';
    const profile = document.getElementById('profileSel')?.value || '';
    const model = document.getElementById('modelSel')?.value || '';
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
    setSelectionState('fileTextSelectionState', textFile ? `当前已选文本：${textFile}` : '当前已选：未选择历史文本');
    setSelectionState('homeSummaryBackend', backend || '未选择');
    setSelectionState('homeSummaryBackendModel', backendModel || '未选择');
    setSelectionState('homeSummaryTranslate', homeModel || '未选择');
    setSelectionState('homeSummaryFolder', folder || '未选择');
    setSelectionState('homeSummaryText', textFile || '未选择');

    ['homeBackendSel','homeBackendModelSel','homeModelSel','modelSel','profileSel','folderSelect','textFileSelect'].forEach(updateSelectionClass);
}

function bindSelectionListeners(){
    const ids = ['homeBackendSel','homeBackendModelSel','homeModelSel','modelSel','profileSel','folderSelect','textFileSelect'];
    ids.forEach(id=>{
        const el = document.getElementById(id);
        if(!el || el.dataset.selectionBound === '1') return;
        el.addEventListener('change', syncSelectionStates);
        el.addEventListener('click', syncSelectionStates);
        el.dataset.selectionBound = '1';
    });
}

function getBackendModelCatalog(){
    const backend = document.getElementById('homeBackendSel')?.value || 'FunASR（Paraformer）';
    return backend === 'faster-whisper（多语言）' ? HOME_WHISPER_MODELS : HOME_FUNASR_MODELS;
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
  for(const p of ['home','file','model']){
    document.getElementById('page-'+p).classList.toggle('active', p===name);
  }
  document.getElementById('btn-home').classList.toggle('active', name==='home');
  document.getElementById('btn-file').classList.toggle('active', name==='file');
  document.getElementById('btn-model').classList.toggle('active', name==='model');
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

async function refreshHistory(){
  const data = await api('/api/history');
  const hv = document.getElementById('historyVideo');
  hv.innerHTML = '';
  (data.videos||[]).forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=v; hv.appendChild(o); });
        foldersList = data.folders || [];
        setHiddenSelectOptions('folderSelect', foldersList, document.getElementById('folderSelect')?.value || '');
        applyFolderFilter();
    await refreshTextFiles();
    syncSelectionStates();
}

async function deleteFolder(){
  const name = document.getElementById('folderSelect').value;
    if(!name){
        alert('请先选择要删除的历史文件夹');
        return;
    }
  const data = await api('/api/folders/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({folder_name:name})});
    alert(data.message || '删除完成');
  await refreshHistory();
}

async function refreshTextFiles(){
    const folder = document.getElementById('folderSelect').value;
    if(!folder){
        textFilesMeta = [];
        setHiddenSelectOptions('textFileSelect', [], '');
        applyTextFileFilter();
        syncSelectionStates();
        return;
    }
    try{
        const data = await api('/api/folders/text-files?folder_name='+encodeURIComponent(folder));
        textFilesMeta = Array.isArray(data.text_files_meta)
            ? data.text_files_meta.map(item=>({name:item.name, mtime:Number(item.mtime || 0)}))
            : (data.text_files || []).map(name=>({name, mtime:0}));
        const names = textFilesMeta.map(item=>item.name);
        setHiddenSelectOptions('textFileSelect', names, '');
        applyTextFileFilter();
        syncSelectionStates();
    }catch(e){
        console.error('文本列表获取失败', e);
        textFilesMeta = [];
        setHiddenSelectOptions('textFileSelect', [], '');
        applyTextFileFilter();
        syncSelectionStates();
    }
}

function downloadTextFile(){
    const folder = document.getElementById('folderSelect').value;
    const file = document.getElementById('textFileSelect').value;
    if(!folder || !file){
                alert('请先选择文件夹和文本文件');
        return;
    }
    const url = '/api/folders/download-text?folder_name='+encodeURIComponent(folder)+'&file_name='+encodeURIComponent(file);
    window.open(url, '_blank');
}

async function loadProfiles(){
  const data = await api('/api/model/profiles');
    profileNamesList = data.profile_names || [];
    setHiddenSelectOptions('profileSel', profileNamesList, data.active || profileNamesList[0] || '');
    applyProfileFilter();
  fillProfile(data.active_profile || {});
}

function fillProfile(p){
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
  const payload = {name: document.getElementById('profileName').value, base_url: document.getElementById('baseUrl').value, api_key: document.getElementById('apiKey').value};
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
  const payload = {name: document.getElementById('profileName').value, base_url: document.getElementById('baseUrl').value, api_key: document.getElementById('apiKey').value, default_model: document.getElementById('modelSel').value};
  const data = await api('/api/model/profiles/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  document.getElementById('modelStatus').value = data.message || '';
  await loadProfiles();
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

async function downloadOutputZip(){
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
    const a = document.createElement('a');
    a.href = url;
    a.download = zipName;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

async function startTranscribe(){
  const fd = new FormData();
  const f = document.getElementById('videoFile').files[0];
  if(f){ fd.append('video_file', f); }
  fd.append('history_video', document.getElementById('historyVideo').value || '');
  backendFormData(fd);

  const r = await fetch('/api/transcribe/start', {method:'POST', body:fd});
  if(!r.ok){ throw new Error(await r.text()); }
  const data = await r.json();
  currentJobId = data.job_id;
  startPoll();
}

async function stopJob(){
  if(!currentJobId) return;
  await api('/api/jobs/'+currentJobId+'/stop', {method:'POST'});
}

async function startTranslate(){
  if(!currentJobId) return;
    const chosenModel = document.getElementById('homeModelSel').value || document.getElementById('modelSel').value;
        const payload = {
                online_profile: document.getElementById('profileSel').value,
                online_model: chosenModel,
                target_lang: document.getElementById('targetLangSel').value || 'zh'
        };
  await api('/api/jobs/'+currentJobId+'/translate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  startPoll();
}

function startPoll(){
  if(pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async ()=>{
    if(!currentJobId) return;
    try{
      const data = await api('/api/jobs/'+currentJobId);
      document.getElementById('statusText').value = data.status || '';
      document.getElementById('plainText').value = data.plain_text || '';
      document.getElementById('logText').value = data.log_text || '';
    updateTaskPanel(data);
            if(data.done && !data.running){
                clearInterval(pollTimer);
                pollTimer = null;
            }
    }catch(e){ console.error(e); }
  }, 1000);
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
    onHomeBackendChange('paraformer-zh');
    bindSelectionListeners();
    syncSelectionStates();
        updateTaskPanel({});
        initDragZones();
})();
</script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@app.get("/api/history")
def api_history():
    return {
        "videos": core._list_uploaded_videos(),
        "folders": core._list_job_folders(),
    }


@app.post("/api/folders/delete")
def api_delete_folder(payload: dict[str, str]):
    name = payload.get("folder_name", "")
    status, *_ = core._delete_job_folder(name)
    return {"message": status}


@app.get("/api/folders/text-files")
def api_folder_text_files(folder_name: str):
    folder = _resolve_workspace_folder(folder_name)
    entries: list[tuple[str, float]] = []
    for p in folder.glob("*.txt"):
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
        "text_files": [name for name, _mtime in entries],
        "text_files_meta": [{"name": name, "mtime": int(mtime)} for name, mtime in entries],
    }


@app.get("/api/folders/download-text")
def api_download_text_file(folder_name: str, file_name: str):
    folder = _resolve_workspace_folder(folder_name)
    filename = (file_name or "").strip()
    if not filename or Path(filename).name != filename or not filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="invalid file_name")

    target = (folder / filename).resolve()
    if folder.resolve() not in target.parents or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="text file not found")

    return FileResponse(str(target), filename=target.name)


@app.get("/api/model/profiles")
def api_profiles():
    profiles, active = load_profiles()
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
    base_url = payload.get("base_url", "").strip()
    api_key = payload.get("api_key", "").strip()
    if not name or not base_url or not api_key:
        raise HTTPException(status_code=400, detail="name/base_url/api_key required")

    try:
        models = list_available_models(base_url, api_key, raise_on_error=True)
    except Exception as exc:
        message = str(exc)
        if "401" in message or "Unauthorized" in message:
            message = "API Key 无效或已过期，请检查配置组中的 api_key 是否正确。"
        raise HTTPException(status_code=400, detail=f"测试失败: {message}") from exc

    models = sorted({str(m).strip() for m in models if str(m).strip()}, key=lambda x: x.lower())
    profiles, _ = load_profiles()
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
def api_save_profile(payload: dict[str, str]):
    name = payload.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")

    profiles, _ = load_profiles()
    profile = next((p for p in profiles if p.get("name") == name), {"name": name, "models": []})
    profile["base_url"] = payload.get("base_url", "").strip()
    profile["api_key"] = payload.get("api_key", "").strip()
    profile["default_model"] = payload.get("default_model", "").strip()
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
    return {"message": "✅ 配置已保存"}


@app.post("/api/model/profiles/delete")
def api_delete_profile(payload: dict[str, str]):
    name = payload.get("name", "").strip()
    profiles, active = load_profiles()
    profiles = delete_profile(profiles, name)
    save_profiles(profiles, active_profile=(active if active != name else None))
    return {"message": f"✅ 已删除配置: {name}"}


@app.post("/api/transcribe/start")
def api_transcribe_start(
    history_video: str = Form(default=""),
    backend: str = Form(default="FunASR（Paraformer）"),
    language: str = Form(default="自动检测"),
    whisper_model: str = Form(default="medium"),
    funasr_model: str = Form(default="paraformer-zh ⭐ 普通话精度推荐"),
    device: str = Form(default="CUDA"),
    video_file: UploadFile | None = File(default=None),
):
    global _RUNTIME_THREAD

    with _RUNTIME_LOCK:
        if _RUNTIME_JOB and _RUNTIME_JOB.running:
            raise HTTPException(status_code=409, detail="已有任务运行中")

    video_path = ""
    temp_path: Path | None = None
    if video_file is not None and video_file.filename:
        suffix = Path(video_file.filename).suffix or ".bin"
        temp_path = Path(core.WORKSPACE_DIR) / f"upload_{uuid.uuid4().hex}{suffix}"
        with temp_path.open("wb") as f:
            shutil.copyfileobj(video_file.file, f)
        video_path = str(temp_path)
    elif history_video:
        video_path = core._resolve_input_path(None, history_video) or ""

    if not video_path:
        raise HTTPException(status_code=400, detail="请上传文件或选择历史视频")

    job = JobState(job_id=uuid.uuid4().hex, running=True, status="⏳ 任务已启动")
    _set_job_progress(job, "⏳ 任务已启动", time.time(), progress_pct=1, eta_seconds=0, step_label="任务启动")
    _set_job_state(job)

    def runner():
        _run_transcribe_worker(job, video_path, backend, language, whisper_model, funasr_model, device)
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass

    _RUNTIME_THREAD = threading.Thread(target=runner, daemon=True)
    _RUNTIME_THREAD.start()
    return {"job_id": job.job_id}


@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str):
    job = _get_job(job_id)
    return _json_job(job)


@app.post("/api/jobs/{job_id}/stop")
def api_job_stop(job_id: str):
    _ = _get_job(job_id)
    core.STOP_EVENT.set()
    return {"message": "stop requested"}


@app.post("/api/jobs/{job_id}/translate")
def api_job_translate(job_id: str, payload: dict[str, str]):
    global _RUNTIME_THREAD
    job = _get_job(job_id)
    if job.running:
        raise HTTPException(status_code=409, detail="当前任务仍在运行")

    job.running = True
    job.done = False
    job.failed = False
    _set_job_progress(job, "⏳ 正在开始翻译...", time.time(), progress_pct=2, eta_seconds=0, step_label="翻译启动")

    profile = payload.get("online_profile", "")
    model = payload.get("online_model", "")
    target_lang = _normalize_lang_code(payload.get("target_lang", "zh"))

    _RUNTIME_THREAD = threading.Thread(target=_run_translate_worker, args=(job, profile, model, target_lang), daemon=True)
    _RUNTIME_THREAD.start()
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

    canonical_zip = f"{job.current_job}.zip"
    files = [
        p.name
        for p in sorted(job_dir.iterdir(), key=lambda x: x.name.lower())
        if p.is_file() and (
            p.suffix.lower() in {".srt", ".txt"}
            or p.name == canonical_zip
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


def main():
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="video2text FastAPI")
    parser.add_argument("--port", type=int, default=7881)
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
