"""
video2text MCP Server — 纯 HTTP 客户端

通过调用本地 FastAPI 服务（默认 http://127.0.0.1:7881）暴露全部功能为 MCP Tools。
不 import 任何重型模块（torch/funasr/transformers），所有 GPU 工作由 FastAPI 进程完成。

启动: uv run python mcp_server.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = os.environ.get("VIDEO2TEXT_URL", "https://127.0.0.1:7881")
_TIMEOUT = 600.0  # 转录/翻译可能很慢，10 分钟超时

mcp = FastMCP("video2text", instructions=(
    "video2text: 视频/音频转字幕工具。支持转录(ASR)、翻译、下载视频等功能。\n"
    "使用前请确保 FastAPI 服务已启动（默认 http://127.0.0.1:7881）。"
))


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------

async def _get(path: str, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as c:
        r = await c.get(f"{DEFAULT_BASE_URL}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, data: dict | None = None, files: dict | None = None,
                raw: bool = False) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as c:
        if files:
            r = await c.post(f"{DEFAULT_BASE_URL}{path}", data=data, files=files)
        else:
            r = await c.post(f"{DEFAULT_BASE_URL}{path}", json=data)
        r.raise_for_status()
        if raw:
            return r.content
        ct = r.headers.get("content-type", "")
        if "json" in ct:
            return r.json()
        return r.text


async def _post_form(path: str, fields: dict[str, str],
                     file_path: str | None = None,
                     file_field: str = "file") -> Any:
    """提交 multipart/form-data 请求。"""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as c:
        data = {k: (None, v) for k, v in fields.items()}
        if file_path:
            p = Path(file_path)
            data[file_field] = (p.name, p.read_bytes(), _guess_mime(p.name))
        r = await c.post(f"{DEFAULT_BASE_URL}{path}", files=data)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "json" in ct:
            return r.json()
        return r.text


def _guess_mime(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".webm": "video/webm", ".mov": "video/quicktime", ".flv": "video/x-flv",
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".flac": "audio/flac", ".ogg": "audio/ogg", ".aac": "audio/aac",
        ".srt": "text/plain", ".txt": "text/plain",
    }.get(ext, "application/octet-stream")


def _srt_segments_to_text(segments: list) -> str:
    """将 segments 列表格式化为可读文本。"""
    lines = []
    for i, (start, end, text) in enumerate(segments, 1):
        lines.append(f"{i}\n{_s_time(start)} --> {_s_time(end)}\n{text}")
    return "\n\n".join(lines)


def _s_time(s: float) -> str:
    h = int(s) // 3600
    m = (int(s) % 3600) // 60
    sec = int(s) % 60
    ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# Tools — 核心流水线
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_health() -> str:
    """检查 video2text 服务是否运行正常。"""
    try:
        r = await _get("/health")
        return json.dumps(r, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"❌ 服务不可用: {e}"


@mcp.tool()
async def v2t_transcribe_file(
    file_path: str,
    backend: str = "",
    language: str = "自动检测",
    device: str = "CUDA",
    auto_translate: bool = False,
    target_lang: str = "zh",
    online_profile: str = "",
    online_model: str = "",
) -> str:
    """
    上传并转录本地视频/音频文件，返回 SRT 内容。

    Args:
        file_path: 本地视频/音频文件绝对路径
        backend: ASR 后端（留空用默认）, 如 "FunASR（Paraformer）", "faster-whisper"
        language: 语言，如 "zh", "en", "ja", "自动检测"
        device: 计算设备 "CUDA" 或 "CPU"
        auto_translate: 是否自动翻译
        target_lang: 翻译目标语言（auto_translate=True 时生效）
        online_profile: 翻译配置组名
        online_model: 翻译模型名
    """
    p = Path(file_path)
    if not p.exists():
        return f"❌ 文件不存在: {file_path}"

    # 1. 上传文件并启动转录
    fields = {
        "backend": backend,
        "language": language,
        "device": device,
        "auto_translate": "1" if auto_translate else "0",
    }
    try:
        resp = await _post_form("/api/transcribe/start", fields, file_path=file_path, file_field="video_file")
    except httpx.HTTPStatusError as e:
        return f"❌ 启动转录失败: {e.response.text}"

    job_id = resp.get("job_id", "")
    if not job_id:
        return f"❌ 未获取到 job_id: {resp}"

    # 2. 轮询等待完成
    return await _poll_job(job_id, expect_translation=auto_translate)


@mcp.tool()
async def v2t_transcribe_url(
    url: str,
    backend: str = "",
    language: str = "自动检测",
    device: str = "CUDA",
    auto_translate: bool = False,
    target_lang: str = "zh",
    online_profile: str = "",
    online_model: str = "",
    auto_subtitle_lang: str = "zh",
) -> str:
    """
    下载 URL 视频并转录，返回 SRT 内容。

    支持 YouTube、Bilibili、小红书等 20+ 平台。

    Args:
        url: 视频 URL
        backend: ASR 后端（留空用默认）
        language: 语言
        device: 计算设备
        auto_translate: 是否自动翻译
        target_lang: 翻译目标语言
        online_profile: 翻译配置组名
        online_model: 翻译模型名
        auto_subtitle_lang: 平台字幕优先语言
    """
    # 1. 下载视频
    try:
        dl = await _post("/api/download_url", {"url": url})
    except httpx.HTTPStatusError as e:
        return f"❌ 下载失败: {e.response.text}"

    filepath = dl.get("filepath", "")
    subtitle_path = dl.get("subtitle_path", "")
    if not filepath:
        return f"❌ 下载失败，未获取到文件路径: {dl}"

    # 2. 如果有平台自动字幕，直接导入
    if subtitle_path:
        try:
            imp = await _post("/api/jobs/import-subtitle", {
                "media_path": filepath,
                "subtitle_path": subtitle_path,
            })
            job_id = imp.get("job_id", "")
            if job_id:
                return await _poll_job(job_id, expect_translation=auto_translate)
        except Exception:
            pass  # 导入失败，继续走 ASR

    # 3. 启动转录（用历史视频方式）
    fields = {
        "history_video": filepath,
        "backend": backend,
        "language": language,
        "device": device,
        "auto_translate": "1" if auto_translate else "0",
    }
    try:
        resp = await _post_form("/api/transcribe/start", fields)
    except httpx.HTTPStatusError as e:
        return f"❌ 启动转录失败: {e.response.text}"

    job_id = resp.get("job_id", "")
    if not job_id:
        return f"❌ 未获取到 job_id: {resp}"

    return await _poll_job(job_id, expect_translation=auto_translate)


@mcp.tool()
async def v2t_translate(
    job_id: str,
    target_lang: str = "zh",
    online_profile: str = "",
    online_model: str = "",
    parallel_threads: int = 5,
) -> str:
    """
    翻译已完成的转录结果。

    Args:
        job_id: 任务 ID（来自转录返回）
        target_lang: 目标语言（zh/en/ja/ko 等）
        online_profile: 翻译配置组名（留空用默认）
        online_model: 翻译模型名（留空用默认）
        parallel_threads: 并行线程数
    """
    try:
        await _post(f"/api/jobs/{job_id}/translate", {
            "target_lang": target_lang,
            "online_profile": online_profile,
            "online_model": online_model,
            "parallel_threads": str(parallel_threads),
        })
    except httpx.HTTPStatusError as e:
        return f"❌ 启动翻译失败: {e.response.text}"

    return await _poll_job(job_id, expect_translation=True)


@mcp.tool()
async def v2t_extract_audio(
    file_path: str,
    output_path: str = "",
) -> str:
    """
    从视频文件中提取 WAV 音频（通过 FastAPI 服务）。
    注意：此功能通过上传文件到服务端实现。

    Args:
        file_path: 本地视频/音频文件路径
        output_path: 输出 WAV 路径（留空自动生成）
    """
    # 直接上传文件触发转录，只返回 WAV 路径信息
    # 由于 FastAPI 没有单独的 extract-audio 端点，
    # 这里用外部 API 同步处理
    p = Path(file_path)
    if not p.exists():
        return f"❌ 文件不存在: {file_path}"

    with open(p, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    try:
        resp = await _post("/api/external/process", {
            "source_type": "base64",
            "base64_data": content,
            "filename": p.name,
            "output_mode": "json",
            "target_lang": "none",
        })
    except httpx.HTTPStatusError as e:
        return f"❌ 处理失败: {e.response.text}"

    if isinstance(resp, dict) and resp.get("files"):
        files = resp["files"]
        return json.dumps({
            "message": "转录完成（含音频提取）",
            "job_id": resp.get("job_id"),
            "files": files,
        }, ensure_ascii=False, indent=2)
    return json.dumps(resp, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tools — 外部 API 同步调用（一步到位）
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_process(
    source_type: str,
    url: str = "",
    file_path: str = "",
    history_video: str = "",
    backend: str = "",
    language: str = "自动检测",
    device: str = "CUDA",
    target_lang: str = "",
    online_profile: str = "",
    online_model: str = "",
    auto_subtitle_lang: str = "zh",
    force_asr: bool = False,
) -> str:
    """
    统一处理入口：上传/URL/历史文件 → 转录 → (可选)翻译，同步返回结果。

    最常用的工具，一步完成全部流程。

    Args:
        source_type: 输入类型 "url" / "base64" / "history"
        url: 视频 URL（source_type=url 时必填）
        file_path: 本地文件路径（会自动转 base64 上传）
        history_video: 历史视频相对路径（source_type=history 时使用）
        backend: ASR 后端（留空用默认）
        language: 语言
        device: "CUDA" 或 "CPU"
        target_lang: 翻译目标语言（留空则不翻译）
        online_profile: 翻译配置组名
        online_model: 翻译模型名
        auto_subtitle_lang: 平台字幕优先语言
        force_asr: 强制 ASR，忽略平台字幕
    """
    payload: dict[str, Any] = {
        "source_type": source_type,
        "backend": backend,
        "language": language,
        "device": device,
        "target_lang": target_lang,
        "online_profile": online_profile,
        "online_model": online_model,
        "auto_subtitle_lang": auto_subtitle_lang,
        "force_asr": force_asr,
        "output_mode": "json",
    }

    if source_type == "url" and url:
        payload["url"] = url
    elif source_type == "history" and history_video:
        payload["history_video"] = history_video
    elif file_path:
        p = Path(file_path)
        if not p.exists():
            return f"❌ 文件不存在: {file_path}"
        with open(p, "rb") as f:
            payload["source_type"] = "base64"
            payload["base64_data"] = base64.b64encode(f.read()).decode()
            payload["filename"] = p.name

    try:
        resp = await _post("/api/external/process", payload)
    except httpx.HTTPStatusError as e:
        detail = e.response.text
        try:
            detail = json.dumps(e.response.json(), ensure_ascii=False)
        except Exception:
            pass
        return f"❌ 处理失败: {detail}"

    if isinstance(resp, dict):
        # 读取输出文件内容
        files = resp.get("files", [])
        job_dir = resp.get("current_job", "")
        result = {
            "job_id": resp.get("job_id"),
            "status": resp.get("status"),
            "current_job": job_dir,
            "files": files,
        }
        # 尝试读取原文 SRT
        for f in files:
            if f.endswith(".srt") and ".zh." not in f and ".en." not in f:
                try:
                    content = await _get_file_content(job_dir, f)
                    result["srt_content"] = content
                    break
                except Exception:
                    pass
        return json.dumps(result, ensure_ascii=False, indent=2)

    return str(resp)


# ---------------------------------------------------------------------------
# Tools — 文件管理
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_list_folders(max_items: int = 50) -> str:
    """列出 workspace 中所有任务文件夹（名称、大小、修改时间）。"""
    try:
        resp = await _get("/api/history")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    folders = resp.get("folders", [])
    folders = folders[:max_items]
    if not folders:
        return "暂无任务文件夹"

    lines = ["## Workspace 文件夹列表\n"]
    for f in folders:
        name = f.get("name", "?")
        mtime = f.get("mtime", 0)
        size = f.get("size", 0)
        lines.append(f"- **{name}** ({size:.1f} MB)")
    return "\n".join(lines)


@mcp.tool()
async def v2t_list_files(folder_name: str) -> str:
    """
    列出指定任务文件夹内的输出文件。

    Args:
        folder_name: 文件夹名称（来自 v2t_list_folders）
    """
    try:
        resp = await _get("/api/folders/output-files", {"folder_name": folder_name})
    except httpx.HTTPStatusError as e:
        return f"❌ 获取失败: {e.response.text}"

    files = resp.get("files", [])
    if not files:
        return f"文件夹 '{folder_name}' 中无输出文件"

    lines = [f"## {folder_name} 输出文件\n"]
    for f in files:
        name = f.get("name", "?")
        size = f.get("size", 0)
        lines.append(f"- {name} ({size} bytes)")
    return "\n".join(lines)


@mcp.tool()
async def v2t_read_file(folder_name: str, file_name: str) -> str:
    """
    读取任务文件夹中指定文件的内容（SRT/TXT）。

    Args:
        folder_name: 文件夹名称
        file_name: 文件名（如 "video.srt", "video.zh.txt"）
    """
    return await _get_file_content(folder_name, file_name)


async def _get_file_content(folder_name: str, file_name: str) -> str:
    """读取 workspace 文件内容的内部函数。"""
    # 安全拼接路径并读取
    from core.config import WORKSPACE_DIR
    p = WORKSPACE_DIR / folder_name / file_name
    # 安全校验
    try:
        p.resolve().relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        return "❌ 非法路径"
    if not p.exists():
        return f"❌ 文件不存在: {folder_name}/{file_name}"
    return p.read_text(encoding="utf-8", errors="replace")


@mcp.tool()
async def v2t_delete_folder(folder_name: str) -> str:
    """
    删除 workspace 中的指定任务文件夹。

    Args:
        folder_name: 文件夹名称
    """
    try:
        resp = await _post("/api/folders/delete", {"folder_name": folder_name})
    except httpx.HTTPStatusError as e:
        return f"❌ 删除失败: {e.response.text}"
    return resp.get("message", "已删除")


@mcp.tool()
async def v2t_delete_folders(folder_names: list[str]) -> str:
    """
    批量删除 workspace 文件夹。

    Args:
        folder_names: 文件夹名称列表
    """
    try:
        resp = await _post("/api/folders/delete-batch", {"folder_names": folder_names})
    except httpx.HTTPStatusError as e:
        return f"❌ 批量删除失败: {e.response.text}"
    return json.dumps(resp, ensure_ascii=False, indent=2)


@mcp.tool()
async def v2t_download_file(folder_name: str, file_name: str, save_to: str = "") -> str:
    """
    下载 workspace 文件到本地。

    Args:
        folder_name: 文件夹名称
        file_name: 文件名
        save_to: 本地保存路径（留空则返回内容）
    """
    try:
        content = await _post(
            f"/api/jobs/dummy/download-file",
            raw=True,
            # 使用直接路径方式
        )
    except Exception:
        pass

    # 直接从磁盘读取
    from core.config import WORKSPACE_DIR
    p = WORKSPACE_DIR / folder_name / file_name
    try:
        p.resolve().relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        return "❌ 非法路径"
    if not p.exists():
        return f"❌ 文件不存在: {folder_name}/{file_name}"

    if save_to:
        import shutil
        shutil.copy2(str(p), save_to)
        return f"✅ 已保存到 {save_to}"

    return p.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Tools — 后端/配置
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_list_backends() -> str:
    """列出可用的 ASR 和翻译后端信息。"""
    try:
        resp = await _get("/api/backends")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    lines = ["## ASR 后端\n"]
    for b in resp.get("asr_backends", []):
        name = b.get("name", "?")
        desc = b.get("description", "")
        model = b.get("default_model", "")
        models = b.get("supported_models", [])
        sr = b.get("sample_rate", 16000)
        chunk = b.get("default_chunk_seconds", 120)
        lines.append(f"### {name}")
        lines.append(f"- 描述: {desc}")
        lines.append(f"- 默认模型: {model}")
        lines.append(f"- 采样率: {sr}Hz, 切片: {chunk}s")
        if models:
            lines.append(f"- 支持模型: {', '.join(models[:8])}")
        lines.append("")

    trans = resp.get("translate_backends", [])
    if trans:
        lines.append("## 翻译后端")
        for t in trans:
            lines.append(f"- {t}")

    return "\n".join(lines)


@mcp.tool()
async def v2t_list_translate_profiles() -> str:
    """列出翻译配置组及其模型列表。"""
    try:
        resp = await _get("/api/model/profiles")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    profiles = resp.get("profiles", [])
    active = resp.get("active_profile", "")
    app = resp.get("app_settings", {})

    lines = ["## 翻译配置组\n"]
    for p in profiles:
        marker = " ← 当前激活" if p.get("name") == active else ""
        lines.append(f"### {p.get('name', '?')}{marker}")
        lines.append(f"- API: {p.get('base_url', '')}")
        lines.append(f"- 默认模型: {p.get('default_model', '')}")
        models = p.get("models", [])
        if models:
            for m in models[:10]:
                lines.append(f"  - {m}")
        lines.append("")

    lines.append("## 应用设置")
    for k, v in app.items():
        if "KEY" in k.upper():
            v = v[:8] + "..." if v else "(空)"
        lines.append(f"- {k}: {v}")

    return "\n".join(lines)


@mcp.tool()
async def v2t_get_settings() -> str:
    """获取当前应用配置（端口、默认后端、代理等）。"""
    try:
        resp = await _get("/api/model/profiles")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    app = resp.get("app_settings", {})
    return json.dumps(app, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tools — 视频下载
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_download_video(url: str, auto_subtitle_lang: str = "zh") -> str:
    """
    仅下载视频（不转录），返回文件路径信息。

    支持 YouTube、Bilibili、小红书等平台。

    Args:
        url: 视频 URL
        auto_subtitle_lang: 平台字幕优先语言
    """
    try:
        resp = await _post("/api/download_url", {
            "url": url,
            "auto_subtitle_lang": auto_subtitle_lang,
        })
    except httpx.HTTPStatusError as e:
        return f"❌ 下载失败: {e.response.text}"

    lines = ["✅ 下载成功\n"]
    lines.append(f"- 文件: {resp.get('filename', '')}")
    lines.append(f"- 路径: {resp.get('filepath', '')}")
    if resp.get("auto_subtitle"):
        lines.append(f"- 平台字幕: {resp.get('subtitle_name', '已获取')}")
    if resp.get("xhs_title"):
        lines.append(f"- 标题: {resp['xhs_title']}")
        lines.append(f"- 作者: {resp.get('xhs_author', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools — 任务管理
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_job_status(job_id: str) -> str:
    """
    查询任务状态。

    Args:
        job_id: 任务 ID
    """
    try:
        resp = await _get(f"/api/jobs/{job_id}")
    except Exception as e:
        return f"❌ 查询失败: {e}"
    return json.dumps(resp, ensure_ascii=False, indent=2)


@mcp.tool()
async def v2t_stop_job(job_id: str) -> str:
    """
    停止运行中的任务。

    Args:
        job_id: 任务 ID
    """
    try:
        await _post(f"/api/jobs/{job_id}/stop", {})
    except Exception as e:
        return f"❌ 停止失败: {e}"
    return "✅ 已发送停止请求"


@mcp.tool()
async def v2t_queue_status() -> str:
    """查看任务队列状态（运行中/排队/历史）。"""
    try:
        resp = await _get("/api/queue/status")
    except Exception as e:
        return f"❌ 获取失败: {e}"
    return json.dumps(resp, ensure_ascii=False, indent=2)


@mcp.tool()
async def v2t_list_history() -> str:
    """列出可用的历史视频和所有文件夹。"""
    try:
        resp = await _get("/api/history")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    videos = resp.get("videos", [])
    folders = resp.get("folders", [])

    lines = []
    if videos:
        lines.append("## 可用视频/音频\n")
        for v in videos[:20]:
            lines.append(f"- {v}")

    if folders:
        lines.append("\n## 任务文件夹\n")
        for f in folders[:30]:
            name = f.get("name", "?")
            size = f.get("size", 0)
            lines.append(f"- **{name}** ({size:.1f} MB)")

    return "\n".join(lines) if lines else "暂无历史记录"


@mcp.tool()
async def v2t_folder_translate(
    folder_name: str,
    target_lang: str = "zh",
    online_profile: str = "",
    online_model: str = "",
) -> str:
    """
    翻译已有文件夹中的字幕文件。

    Args:
        folder_name: workspace 文件夹名
        target_lang: 目标语言
        online_profile: 翻译配置组名
        online_model: 翻译模型名
    """
    try:
        resp = await _post("/api/folders/translate", {
            "folder_name": folder_name,
            "target_lang": target_lang,
            "online_profile": online_profile,
            "online_model": online_model,
        })
    except httpx.HTTPStatusError as e:
        return f"❌ 翻译失败: {e.response.text}"

    # 翻译是同步的，直接返回结果
    return json.dumps(resp, ensure_ascii=False, indent=2)


@mcp.tool()
async def v2t_upload_cookie(file_path: str) -> str:
    """
    上传 Cookie 文件用于需要登录的平台下载。

    Args:
        file_path: cookies.txt 文件路径
    """
    p = Path(file_path)
    if not p.exists():
        return f"❌ 文件不存在: {file_path}"
    try:
        resp = await _post_form(
            "/api/upload_cookie",
            {},
            file_path=file_path,
            file_field="cookie_file",
        )
    except httpx.HTTPStatusError as e:
        return f"❌ 上传失败: {e.response.text}"
    return "✅ Cookie 文件已上传"


@mcp.tool()
async def v2t_all_output_files() -> str:
    """列出所有文件夹的全部输出文件（跨文件夹汇总）。"""
    try:
        resp = await _get("/api/folders/all-output-files")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    items = resp.get("files", [])
    if not items:
        return "暂无输出文件"

    lines = ["## 全部输出文件\n"]
    for item in items[:50]:
        folder = item.get("folder", "?")
        name = item.get("name", "?")
        size = item.get("size", 0)
        lines.append(f"- **{folder}**/{name} ({size} bytes)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 轮询辅助
# ---------------------------------------------------------------------------

async def _poll_job(job_id: str, expect_translation: bool = False,
                    max_wait: float = 600, interval: float = 2.0) -> str:
    """轮询任务直到完成，返回结果文本。"""
    elapsed = 0.0
    last_status = ""
    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval

        try:
            job = await _get(f"/api/jobs/{job_id}")
        except Exception:
            continue

        status = job.get("status", "")
        done = job.get("done", False)
        failed = job.get("failed", False)

        if status != last_status:
            last_status = status

        if failed:
            return f"❌ 任务失败: {status}"

        if done:
            # 读取输出文件
            current_job = job.get("current_job", "")
            prefix = job.get("current_prefix", "")

            result = {"job_id": job_id, "status": status, "current_job": current_job}

            if current_job:
                try:
                    files_resp = await _get(f"/api/jobs/{job_id}/files")
                    files = files_resp.get("files", [])
                    result["files"] = [f["name"] for f in files]
                except Exception:
                    pass

                # 读取 SRT 内容
                srt_name = f"{prefix}.srt" if prefix else None
                if srt_name:
                    try:
                        srt_content = await _get_file_content(current_job, srt_name)
                        result["srt_content"] = srt_content
                    except Exception:
                        pass

                # 读取翻译后的 SRT
                if expect_translation:
                    for lang in ["zh", "en", "ja", "ko"]:
                        translated_name = f"{prefix}.{lang}.srt" if prefix else None
                        if translated_name:
                            try:
                                content = await _get_file_content(current_job, translated_name)
                                result[f"{lang}_srt_content"] = content
                                break
                            except Exception:
                                pass

            return json.dumps(result, ensure_ascii=False, indent=2)

    return f"⏰ 任务超时（{max_wait}s），最后状态: {last_status}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
