#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import main as core
from utils.online_models import delete_profile, load_profiles, save_profiles, upsert_profile
from utils.subtitle import collect_plain_text, normalize_segments_timeline, save_plain, save_srt, segments_to_plain
from utils.translate import list_available_models, translate_segments_to_chinese

app = FastAPI(title="video2text-fastapi")


@dataclass
class JobState:
    job_id: str
    status: str = "等待中"
    plain_text: str = ""
    logs: list[str] = field(default_factory=list)
    current_job: str = ""
    current_prefix: str = ""
    srt_bundle: str | None = None
    txt_bundle: str | None = None
    done: bool = False
    failed: bool = False
    running: bool = False

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
        "srt_ready": bool(job.srt_bundle and Path(job.srt_bundle).exists()),
        "txt_ready": bool(job.txt_bundle and Path(job.txt_bundle).exists()),
        "done": job.done,
        "failed": job.failed,
        "running": job.running,
    }


def _run_transcribe_worker(
    job: JobState,
    video_path: str,
    backend: str,
    language: str,
    whisper_model: str,
    funasr_model: str,
    device: str,
):
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
            job.status = status
            job.plain_text = collect_plain_text(segments)

        if core.STOP_EVENT.is_set():
            job.status = "🛑 已停止（未生成字幕文件）"
            job.done = True
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

        job.status = f"✅ 原文识别完成 → workspace/{job_dir.name}/（点击翻译生成中文）"
        job.plain_text = plain_text or segments_to_plain(cleaned_segments, normalize=False)
        job.done = True
        job.running = False
    except Exception as exc:
        job.failed = True
        job.done = True
        job.running = False
        job.status = f"❌ 转录失败: {exc}"
        job.add_log(f"[ERROR] {exc}")


def _run_translate_worker(job: JobState, profile_name: str, model_name: str):
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

        meta = core._load_task_meta(job_dir)
        source_lang = str(meta.get("source_lang") or "auto")

        translated: list[tuple[float, float, str]] = []
        total = len(segments)
        start_ts = time.time()

        for idx, seg in enumerate(segments, start=1):
            part = translate_segments_to_chinese(
                [seg],
                source_lang=source_lang,
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
            job.status = f"⏳ 翻译进度：{pct}%｜预计剩余 {h:02d}:{m:02d}:{s:02d}"
            job.plain_text = collect_plain_text(translated)

        zh_segments = normalize_segments_timeline(translated)
        save_srt(zh_segments, str(job_dir / f"{file_prefix}.zh.srt"), normalize=False)
        save_plain(zh_segments, str(job_dir / f"{file_prefix}.zh.txt"), normalize=False)

        job.srt_bundle = core._build_download_bundle(job_dir, file_prefix, "srt")
        job.txt_bundle = core._build_download_bundle(job_dir, file_prefix, "txt")
        job.status = f"✅ 翻译完成：workspace/{job.current_job}/"
        job.done = True
        job.running = False
    except Exception as exc:
        job.failed = True
        job.done = True
        job.running = False
        job.status = f"❌ 翻译失败: {exc}"
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
body{font-family:Arial,sans-serif;max-width:1200px;margin:0 auto;padding:16px}
.nav{display:flex;gap:8px;margin:12px 0}
.nav button{padding:8px 14px;border:1px solid #ccc;background:#f3f3f3;cursor:pointer}
.nav button.active{background:#222;color:#fff}
.page{display:none}
.page.active{display:block}
.row{display:flex;gap:10px;flex-wrap:wrap}
.col{flex:1;min-width:320px}
textarea{width:100%;height:220px}
input,select,button{padding:8px}
label{display:block;margin-top:8px;font-weight:600}
</style>
</head>
<body>
<h1>🎬 视频转字幕（FastAPI）</h1>
<p>支持主页、文件管理、配置模型三页面切换。</p>
<div class="nav">
  <button id="btn-home" class="active" onclick="showPage('home')">主页</button>
  <button id="btn-file" onclick="showPage('file')">文件管理</button>
  <button id="btn-model" onclick="showPage('model')">配置模型</button>
</div>

<div id="page-home" class="page active">
  <div class="row">
    <div class="col">
      <label>上传视频/音频</label>
      <input type="file" id="videoFile" />
      <label>或选择历史视频</label>
      <select id="historyVideo"></select>
      <div style="margin-top:10px" class="row">
        <button onclick="startTranscribe()">开始转录</button>
        <button onclick="stopJob()">停止</button>
        <button onclick="startTranslate()">翻译</button>
      </div>
      <div style="margin-top:10px" class="row">
        <button onclick="downloadKind('srt')">下载SRT字幕</button>
        <button onclick="downloadKind('txt')">下载纯文本</button>
      </div>
      <label>状态</label>
      <input id="statusText" readonly style="width:100%" />
    </div>
    <div class="col">
      <label>识别文本</label>
      <textarea id="plainText" readonly></textarea>
      <label>运行日志</label>
      <textarea id="logText" readonly></textarea>
    </div>
  </div>
</div>

<div id="page-file" class="page">
  <h3>文件管理</h3>
  <button onclick="refreshHistory()">刷新历史</button>
  <div id="folderList" style="margin:10px 0"></div>
  <label>删除历史文件夹</label>
  <select id="folderSelect"></select>
  <button onclick="deleteFolder()">删除选中</button>
  <input id="folderStatus" readonly style="width:100%;margin-top:8px" />
</div>

<div id="page-model" class="page">
  <h3>配置模型</h3>
  <label>识别后端</label>
  <select id="backendSel">
    <option>FunASR（Paraformer）</option>
    <option>faster-whisper（多语言）</option>
  </select>
  <label>语言</label>
  <select id="langSel">
    <option>自动检测</option><option>zh（普通话）</option><option>yue（粤语）</option><option>en（英语）</option><option>ja（日语）</option><option>ko（韩语）</option><option>es（西班牙语）</option>
  </select>
  <label>FunASR 模型</label>
  <input id="funasrModel" value="paraformer-zh ⭐ 普通话精度推荐" style="width:100%" />
  <label>Whisper 模型</label>
  <input id="whisperModel" value="medium" style="width:100%" />
  <label>设备</label>
  <select id="deviceSel"><option>CUDA</option><option>CPU</option></select>

  <hr />
  <label>配置组</label>
  <select id="profileSel" onchange="onProfileSelected()"></select>
  <label>配置名称</label>
  <input id="profileName" style="width:100%" />
  <label>base_url</label>
  <input id="baseUrl" style="width:100%" />
  <label>api_key</label>
  <input id="apiKey" style="width:100%" />
  <label>在线模型</label>
  <select id="modelSel"></select>
  <div class="row" style="margin-top:10px">
    <button onclick="fetchModels()">获取可用模型列表</button>
    <button onclick="saveProfile()">保存配置</button>
    <button onclick="deleteProfile()">删除配置</button>
  </div>
  <input id="modelStatus" readonly style="width:100%;margin-top:8px" />
</div>

<script>
let currentJobId = "";
let pollTimer = null;

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
  if(!r.ok){ throw new Error(await r.text()); }
  return await r.json();
}

async function refreshHistory(){
  const data = await api('/api/history');
  const hv = document.getElementById('historyVideo');
  hv.innerHTML = '';
  (data.videos||[]).forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=v; hv.appendChild(o); });
  const fs = document.getElementById('folderSelect');
  fs.innerHTML='';
  (data.folders||[]).forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=v; fs.appendChild(o); });
  const fl = document.getElementById('folderList');
  fl.innerHTML = (data.folders||[]).map(x=>`<div>📁 ${x}</div>`).join('');
}

async function deleteFolder(){
  const name = document.getElementById('folderSelect').value;
  const data = await api('/api/folders/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({folder_name:name})});
  document.getElementById('folderStatus').value = data.message || '';
  await refreshHistory();
}

async function loadProfiles(){
  const data = await api('/api/model/profiles');
  const sel = document.getElementById('profileSel');
  sel.innerHTML='';
  (data.profile_names||[]).forEach(n=>{ const o=document.createElement('option'); o.value=n; o.textContent=n; sel.appendChild(o); });
  sel.value = data.active || (data.profile_names||[])[0] || '';
  fillProfile(data.active_profile || {});
}

function fillProfile(p){
  document.getElementById('profileName').value = p.name || '';
  document.getElementById('baseUrl').value = p.base_url || '';
  document.getElementById('apiKey').value = p.api_key || '';
  const modelSel = document.getElementById('modelSel');
  modelSel.innerHTML='';
  (p.models||[]).forEach(m=>{ const o=document.createElement('option'); o.value=m; o.textContent=m; modelSel.appendChild(o); });
  if (p.default_model){ modelSel.value = p.default_model; }
}

async function onProfileSelected(){
  const name = document.getElementById('profileSel').value;
  const data = await api('/api/model/profile?name='+encodeURIComponent(name));
  fillProfile(data.profile || {});
}

async function fetchModels(){
  const payload = {name: document.getElementById('profileName').value, base_url: document.getElementById('baseUrl').value, api_key: document.getElementById('apiKey').value};
  const data = await api('/api/model/profiles/fetch-models', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  document.getElementById('modelStatus').value = data.message || '';
  await loadProfiles();
}

async function saveProfile(){
  const payload = {name: document.getElementById('profileName').value, base_url: document.getElementById('baseUrl').value, api_key: document.getElementById('apiKey').value, default_model: document.getElementById('modelSel').value};
  const data = await api('/api/model/profiles/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  document.getElementById('modelStatus').value = data.message || '';
  await loadProfiles();
}

async function deleteProfile(){
  const payload = {name: document.getElementById('profileSel').value};
  const data = await api('/api/model/profiles/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  document.getElementById('modelStatus').value = data.message || '';
  await loadProfiles();
}

function backendFormData(fd){
  fd.append('backend', document.getElementById('backendSel').value);
  fd.append('language', document.getElementById('langSel').value);
  fd.append('whisper_model', document.getElementById('whisperModel').value || 'medium');
  fd.append('funasr_model', document.getElementById('funasrModel').value || 'paraformer-zh ⭐ 普通话精度推荐');
  fd.append('device', document.getElementById('deviceSel').value || 'CUDA');
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
  const payload = {online_profile: document.getElementById('profileSel').value, online_model: document.getElementById('modelSel').value};
  await api('/api/jobs/'+currentJobId+'/translate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  startPoll();
}

function downloadKind(kind){
  if(!currentJobId) return;
  window.open('/api/jobs/'+currentJobId+'/download/'+kind, '_blank');
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
      if(data.done && !data.running){ clearInterval(pollTimer); pollTimer = null; }
    }catch(e){ console.error(e); }
  }, 1000);
}

(async function init(){
  await refreshHistory();
  await loadProfiles();
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


@app.get("/api/model/profiles")
def api_profiles():
    profiles, active = load_profiles()
    active_profile = next((p for p in profiles if p.get("name") == active), profiles[0] if profiles else {})
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
    return {"profile": p}


@app.post("/api/model/profiles/fetch-models")
def api_fetch_models(payload: dict[str, str]):
    name = payload.get("name", "").strip()
    base_url = payload.get("base_url", "").strip()
    api_key = payload.get("api_key", "").strip()
    if not name or not base_url or not api_key:
        raise HTTPException(status_code=400, detail="name/base_url/api_key required")

    models = list_available_models(base_url, api_key)
    profiles, _ = load_profiles()
    profile = next((p for p in profiles if p.get("name") == name), {"name": name})
    profile["base_url"] = base_url
    profile["api_key"] = api_key
    profile["models"] = models
    if models and not str(profile.get("default_model", "")).strip():
        profile["default_model"] = models[0]

    profiles = upsert_profile(profiles, profile)
    save_profiles(profiles, active_profile=name)
    return {"message": f"✅ 获取到 {len(models)} 个模型并已写入 .env"}


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
    if profile["default_model"] and profile["default_model"] not in profile.get("models", []):
        profile["models"] = [profile["default_model"], *profile.get("models", [])]

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
    job.status = "⏳ 正在开始翻译..."

    profile = payload.get("online_profile", "")
    model = payload.get("online_model", "")

    _RUNTIME_THREAD = threading.Thread(target=_run_translate_worker, args=(job, profile, model), daemon=True)
    _RUNTIME_THREAD.start()
    return {"message": "translate started"}


@app.get("/api/jobs/{job_id}/download/{kind}")
def api_job_download(job_id: str, kind: str):
    job = _get_job(job_id)
    if kind not in {"srt", "txt"}:
        raise HTTPException(status_code=400, detail="kind must be srt/txt")

    if kind == "srt":
        path = job.srt_bundle
    else:
        path = job.txt_bundle

    if not path or not Path(path).exists():
        # 动态尝试构建
        if not job.current_job:
            raise HTTPException(status_code=404, detail="bundle not found")
        p = core.prepare_download_bundle("", job.current_job, job.current_prefix, kind)
        if not p or not Path(p).exists():
            raise HTTPException(status_code=404, detail="bundle not found")
        path = p

    return FileResponse(path, filename=Path(path).name)


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
