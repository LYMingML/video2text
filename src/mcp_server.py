"""
video2text MCP Server — 纯 HTTP 客户端

通过调用本地 FastAPI 服务（默认 http://127.0.0.1:7881）暴露全部功能为 MCP Tools。
不 import 任何重型模块（torch/funasr/transformers），所有 GPU 工作由 FastAPI 进程完成。

启动: uv run python src/mcp_server.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = os.environ.get("VIDEO2TEXT_URL", "http://127.0.0.1:7881")
_TIMEOUT = 600.0  # 转录/翻译可能很慢，10 分钟超时


@asynccontextmanager
async def _app_lifespan(app: FastMCP):
    """管理全局 HTTP Client 生命周期。"""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        yield {"client": client}


mcp = FastMCP(
    "video2text",
    lifespan=_app_lifespan,
    instructions=(
        "video2text: 视频/音频转字幕工具。支持转录(ASR)、翻译、下载视频等功能。\n"
        "使用前请确保 FastAPI 服务已启动（默认 http://127.0.0.1:7881）。"
    ),
)


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------

def _client(ctx: Context) -> httpx.AsyncClient:
    return ctx.request_context.lifespan_context["client"]


async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
    r = await _client(ctx).get(f"{DEFAULT_BASE_URL}{path}", params=params)
    r.raise_for_status()
    return r.json()


async def _post(
    ctx: Context,
    path: str,
    data: dict | None = None,
    files: dict | None = None,
    raw: bool = False,
) -> Any:
    if files:
        r = await _client(ctx).post(f"{DEFAULT_BASE_URL}{path}", data=data, files=files)
    else:
        r = await _client(ctx).post(f"{DEFAULT_BASE_URL}{path}", json=data)
    r.raise_for_status()
    if raw:
        return r.content
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        return r.json()
    return r.text


async def _post_form(
    ctx: Context,
    path: str,
    fields: dict[str, str],
    file_path: str | None = None,
    file_field: str = "file",
) -> Any:
    data = {k: (None, v) for k, v in fields.items()}
    if file_path:
        p = Path(file_path)
        data[file_field] = (p.name, p.read_bytes(), _guess_mime(p.name))
    r = await _client(ctx).post(f"{DEFAULT_BASE_URL}{path}", files=data)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        return r.json()
    return r.text


async def _read_text_from_api(ctx: Context, path: str, params: dict | None = None) -> str:
    """通过 API 读取文本内容。"""
    r = await _client(ctx).get(f"{DEFAULT_BASE_URL}{path}", params=params)
    r.raise_for_status()
    return r.text


def _guess_mime(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".flv": "video/x-flv",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
        ".srt": "text/plain",
        ".txt": "text/plain",
        ".vtt": "text/plain",
        ".zip": "application/zip",
    }.get(ext, "application/octet-stream")


def _fmt_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _http_err(e: httpx.HTTPStatusError) -> str:
    try:
        detail = e.response.json()
        return _fmt_json(detail)
    except Exception:
        return e.response.text or str(e)


# ---------------------------------------------------------------------------
# Tools — 核心流水线
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_health(ctx: Context) -> str:
    """检查 video2text 服务是否运行正常。"""
    try:
        r = await _get(ctx, "/health")
        return _fmt_json(r)
    except Exception as e:
        return f"❌ 服务不可用: {e}"


@mcp.tool()
async def v2t_transcribe_file(
    ctx: Context,
    file_path: str,
    backend: str = "",
    language: str = "自动检测",
    device: str = "CUDA",
    whisper_model: str = "",
    funasr_model: str = "",
    auto_translate: bool = False,
    target_lang: str = "zh",
    online_profile: str = "",
    online_model: str = "",
) -> str:
    """
    上传并转录本地视频/音频文件，返回 SRT 内容。

    Args:
        file_path: 本地视频/音频文件绝对路径
        backend: ASR 后端（留空用默认），如 "FunASR(Paraformer)", "faster-whisper"
        language: 语言，如 "zh", "en", "ja", "自动检测"
        device: 计算设备 "CUDA" 或 "CPU"
        whisper_model: Whisper 模型名（留空用默认）
        funasr_model: FunASR 模型名（留空用默认）
        auto_translate: 是否自动翻译
        target_lang: 翻译目标语言（auto_translate=True 时生效）
        online_profile: 翻译配置组名
        online_model: 翻译模型名
    """
    p = Path(file_path)
    if not p.exists():
        return f"❌ 文件不存在: {file_path}"

    fields = {
        "backend": backend,
        "language": language,
        "device": device,
        "whisper_model": whisper_model,
        "funasr_model": funasr_model,
        "auto_translate": "1" if auto_translate else "0",
    }
    try:
        resp = await _post_form(ctx, "/api/transcribe/start", fields, file_path=file_path, file_field="video_file")
    except httpx.HTTPStatusError as e:
        return f"❌ 启动转录失败: {_http_err(e)}"

    job_id = resp.get("job_id", "")
    if not job_id:
        return f"❌ 未获取到 job_id: {resp}"

    return await _poll_job(ctx, job_id, expect_translation=auto_translate)


@mcp.tool()
async def v2t_transcribe_url(
    ctx: Context,
    url: str,
    backend: str = "",
    language: str = "自动检测",
    device: str = "CUDA",
    whisper_model: str = "",
    funasr_model: str = "",
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
        whisper_model: Whisper 模型名（留空用默认）
        funasr_model: FunASR 模型名（留空用默认）
        auto_translate: 是否自动翻译
        target_lang: 翻译目标语言
        online_profile: 翻译配置组名
        online_model: 翻译模型名
        auto_subtitle_lang: 平台字幕优先语言
    """
    try:
        dl = await _post(ctx, "/api/download_url", {"url": url, "auto_subtitle_lang": auto_subtitle_lang})
    except httpx.HTTPStatusError as e:
        return f"❌ 下载失败: {_http_err(e)}"

    filepath = dl.get("filepath", "")
    subtitle_path = dl.get("subtitle_path", "")
    if not filepath:
        return f"❌ 下载失败，未获取到文件路径: {dl}"

    # 如果有平台自动字幕，直接导入
    if subtitle_path:
        try:
            imp = await _post(ctx, "/api/jobs/import-subtitle", {
                "history_video": filepath,
                "subtitle_path": subtitle_path,
            })
            job_id = imp.get("job_id", "")
            if job_id:
                # 等待导入完成
                result = await _poll_job(ctx, job_id, expect_translation=False)
                if auto_translate:
                    # 导入完成后启动翻译
                    trans_result = await _do_translate(ctx, job_id, target_lang, online_profile, online_model)
                    if trans_result.startswith("❌"):
                        return trans_result
                    return await _poll_job(ctx, job_id, expect_translation=True)
                return result
        except httpx.HTTPStatusError:
            pass  # 导入失败，继续走 ASR

    # 走 ASR 转录
    fields = {
        "history_video": filepath,
        "backend": backend,
        "language": language,
        "device": device,
        "whisper_model": whisper_model,
        "funasr_model": funasr_model,
        "auto_translate": "1" if auto_translate else "0",
    }
    try:
        resp = await _post_form(ctx, "/api/transcribe/start", fields)
    except httpx.HTTPStatusError as e:
        return f"❌ 启动转录失败: {_http_err(e)}"

    job_id = resp.get("job_id", "")
    if not job_id:
        return f"❌ 未获取到 job_id: {resp}"

    return await _poll_job(ctx, job_id, expect_translation=auto_translate)


@mcp.tool()
async def v2t_translate(
    ctx: Context,
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
    trans = await _do_translate(ctx, job_id, target_lang, online_profile, online_model, parallel_threads)
    if trans.startswith("❌"):
        return trans
    return await _poll_job(ctx, job_id, expect_translation=True)


async def _do_translate(
    ctx: Context,
    job_id: str,
    target_lang: str,
    online_profile: str,
    online_model: str,
    parallel_threads: int = 5,
) -> str:
    try:
        await _post(ctx, f"/api/jobs/{job_id}/translate", {
            "target_lang": target_lang,
            "online_profile": online_profile,
            "online_model": online_model,
            "parallel_threads": str(parallel_threads),
        })
    except httpx.HTTPStatusError as e:
        return f"❌ 启动翻译失败: {_http_err(e)}"
    return "✅ 已启动翻译"


@mcp.tool()
async def v2t_extract_audio(
    ctx: Context,
    file_path: str,
    output_path: str = "",
) -> str:
    """
    从视频文件中提取 WAV 音频。
    通过统一处理入口完成，返回音频文件信息。

    Args:
        file_path: 本地视频/音频文件路径
        output_path: 输出 WAV 路径（留空则仅返回任务信息）
    """
    p = Path(file_path)
    if not p.exists():
        return f"❌ 文件不存在: {file_path}"

    with open(p, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    try:
        resp = await _post(ctx, "/api/external/process", {
            "source_type": "base64",
            "media_base64": content,
            "filename": p.name,
            "output_mode": "json-base64",
            "target_lang": "none",
        })
    except httpx.HTTPStatusError as e:
        return f"❌ 处理失败: {_http_err(e)}"

    if isinstance(resp, dict) and resp.get("files"):
        files = resp["files"]
        wav_files = [f for f in files if f.lower().endswith(".wav")]
        result = {
            "message": "处理完成",
            "job_id": resp.get("job_id"),
            "current_job": resp.get("current_job"),
            "files": files,
            "wav_files": wav_files,
        }
        if output_path and wav_files:
            # 下载第一个 wav 文件到本地
            job_dir = resp.get("current_job", "")
            if job_dir:
                try:
                    wav_bytes = await _post(
                        ctx,
                        "/api/folders/download-output",
                        {"folder_name": job_dir, "files": [wav_files[0]]},
                        raw=True,
                    )
                    Path(output_path).write_bytes(wav_bytes)
                    result["saved_to"] = output_path
                except Exception as e:
                    result["save_error"] = str(e)
        return _fmt_json(result)
    return _fmt_json(resp)


@mcp.tool()
async def v2t_process(
    ctx: Context,
    source_type: str,
    url: str = "",
    file_path: str = "",
    history_video: str = "",
    backend: str = "",
    language: str = "自动检测",
    device: str = "CUDA",
    whisper_model: str = "",
    funasr_model: str = "",
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
        whisper_model: Whisper 模型名（留空用默认）
        funasr_model: FunASR 模型名（留空用默认）
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
        "whisper_model": whisper_model,
        "funasr_model": funasr_model,
        "target_lang": target_lang,
        "online_profile": online_profile,
        "online_model": online_model,
        "auto_subtitle_lang": auto_subtitle_lang,
        "force_asr": force_asr,
        "output_mode": "json-base64",
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
            payload["media_base64"] = base64.b64encode(f.read()).decode()
            payload["filename"] = p.name

    try:
        resp = await _post(ctx, "/api/external/process", payload)
    except httpx.HTTPStatusError as e:
        return f"❌ 处理失败: {_http_err(e)}"

    if isinstance(resp, dict):
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
            if f.endswith(".srt") and ".zh." not in f and ".en." not in f and ".ja." not in f and ".ko." not in f:
                try:
                    content = await _read_text_from_api(
                        ctx, f"/api/jobs/{resp.get('job_id')}/download-file", {"file_name": f}
                    )
                    result["srt_content"] = content
                    break
                except Exception:
                    pass
        return _fmt_json(result)

    return str(resp)


# ---------------------------------------------------------------------------
# Tools — 文件管理
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_list_folders(ctx: Context, max_items: int = 50) -> str:
    """列出 workspace 中所有任务文件夹（名称、大小、修改时间）。"""
    try:
        resp = await _get(ctx, "/api/history")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    folders = resp.get("folders_meta", resp.get("folders", []))
    folders = folders[:max_items]
    if not folders:
        return "暂无任务文件夹"

    lines = ["## Workspace 文件夹列表\n"]
    for f in folders:
        if isinstance(f, dict):
            name = f.get("name", "?")
            size = f.get("size", 0)
            lines.append(f"- **{name}** ({size:.1f} MB)")
        else:
            lines.append(f"- **{f}**")
    return "\n".join(lines)


@mcp.tool()
async def v2t_list_files(ctx: Context, folder_name: str) -> str:
    """
    列出指定任务文件夹内的输出文件。

    Args:
        folder_name: 文件夹名称（来自 v2t_list_folders）
    """
    try:
        resp = await _get(ctx, "/api/folders/output-files", {"folder_name": folder_name})
    except httpx.HTTPStatusError as e:
        return f"❌ 获取失败: {_http_err(e)}"

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
async def v2t_read_file(ctx: Context, folder_name: str, file_name: str) -> str:
    """
    读取任务文件夹中指定文件的内容（SRT/TXT）。

    Args:
        folder_name: 文件夹名称
        file_name: 文件名（如 "video.srt", "video.zh.txt"）
    """
    # 通过 folders/download-output 下载单文件，后端对单文件直接返回 FileResponse
    try:
        content = await _post(
            ctx,
            "/api/folders/download-output",
            {"folder_name": folder_name, "files": [file_name]},
            raw=True,
        )
        # 尝试按文本解码
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return f"❌ 文件不是文本类型，大小 {len(content)} bytes"
    except httpx.HTTPStatusError as e:
        return f"❌ 读取失败: {_http_err(e)}"
    except Exception as e:
        return f"❌ 读取失败: {e}"


@mcp.tool()
async def v2t_delete_folder(ctx: Context, folder_name: str) -> str:
    """
    删除 workspace 中的指定任务文件夹。

    Args:
        folder_name: 文件夹名称
    """
    try:
        resp = await _post(ctx, "/api/folders/delete", {"folder_name": folder_name})
    except httpx.HTTPStatusError as e:
        return f"❌ 删除失败: {_http_err(e)}"
    return resp.get("message", "已删除")


@mcp.tool()
async def v2t_delete_folders(ctx: Context, folder_names: list[str]) -> str:
    """
    批量删除 workspace 文件夹。

    Args:
        folder_names: 文件夹名称列表
    """
    try:
        resp = await _post(ctx, "/api/folders/delete-batch", {"folder_names": folder_names})
    except httpx.HTTPStatusError as e:
        return f"❌ 批量删除失败: {_http_err(e)}"
    return _fmt_json(resp)


@mcp.tool()
async def v2t_download_file(ctx: Context, folder_name: str, file_name: str, save_to: str = "") -> str:
    """
    下载 workspace 文件到本地。

    Args:
        folder_name: 文件夹名称
        file_name: 文件名
        save_to: 本地保存路径（留空则返回文本内容，二进制返回 base64）
    """
    try:
        content = await _post(
            ctx,
            "/api/folders/download-output",
            {"folder_name": folder_name, "files": [file_name]},
            raw=True,
        )
    except httpx.HTTPStatusError as e:
        return f"❌ 下载失败: {_http_err(e)}"

    if save_to:
        Path(save_to).write_bytes(content)
        return f"✅ 已保存到 {save_to} ({len(content)} bytes)"

    # 尝试文本解码
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(content).decode("ascii")


# ---------------------------------------------------------------------------
# Tools — 后端/配置
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_list_backends(ctx: Context) -> str:
    """列出可用的 ASR 和翻译后端信息。"""
    try:
        resp = await _get(ctx, "/api/backends")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    lines = ["## ASR 后端\n"]
    for b in resp.get("asr_backends", []):
        name = b.get("name", "?")
        desc = b.get("description", "")
        model = b.get("default_model", "")
        sr = b.get("sample_rate", 16000)
        chunk = b.get("default_chunk_seconds", 120)
        lines.append(f"### {name}")
        lines.append(f"- 描述: {desc}")
        lines.append(f"- 默认模型: {model}")
        lines.append(f"- 采样率: {sr}Hz, 切片: {chunk}s")
        models = b.get("supported_models", [])
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
async def v2t_list_translate_profiles(ctx: Context) -> str:
    """列出翻译配置组及其模型列表。"""
    try:
        resp = await _get(ctx, "/api/model/profiles")
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
async def v2t_get_settings(ctx: Context) -> str:
    """获取当前应用配置（端口、默认后端、代理等）。"""
    try:
        resp = await _get(ctx, "/api/model/profiles")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    app = resp.get("app_settings", {})
    return _fmt_json(app)


# ---------------------------------------------------------------------------
# Tools — 视频下载
# ---------------------------------------------------------------------------

@mcp.tool()
async def v2t_download_video(ctx: Context, url: str, auto_subtitle_lang: str = "zh") -> str:
    """
    仅下载视频（不转录），返回文件路径信息。

    支持 YouTube、Bilibili、小红书等平台。

    Args:
        url: 视频 URL
        auto_subtitle_lang: 平台字幕优先语言
    """
    try:
        resp = await _post(ctx, "/api/download_url", {
            "url": url,
            "auto_subtitle_lang": auto_subtitle_lang,
        })
    except httpx.HTTPStatusError as e:
        return f"❌ 下载失败: {_http_err(e)}"

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
async def v2t_job_status(ctx: Context, job_id: str) -> str:
    """
    查询任务状态。

    Args:
        job_id: 任务 ID
    """
    try:
        resp = await _get(ctx, f"/api/jobs/{job_id}")
    except Exception as e:
        return f"❌ 查询失败: {e}"
    return _fmt_json(resp)


@mcp.tool()
async def v2t_stop_job(ctx: Context, job_id: str) -> str:
    """
    停止运行中的任务。

    Args:
        job_id: 任务 ID
    """
    try:
        await _post(ctx, f"/api/jobs/{job_id}/stop", {})
    except Exception as e:
        return f"❌ 停止失败: {e}"
    return "✅ 已发送停止请求"


@mcp.tool()
async def v2t_queue_status(ctx: Context) -> str:
    """查看任务队列状态（运行中/排队/历史）。"""
    try:
        resp = await _get(ctx, "/api/queue/status")
    except Exception as e:
        return f"❌ 获取失败: {e}"
    return _fmt_json(resp)


@mcp.tool()
async def v2t_list_history(ctx: Context) -> str:
    """列出可用的历史视频和所有文件夹。"""
    try:
        resp = await _get(ctx, "/api/history")
    except Exception as e:
        return f"❌ 获取失败: {e}"

    videos = resp.get("videos", [])
    folders = resp.get("folders_meta", resp.get("folders", []))

    lines = []
    if videos:
        lines.append("## 可用视频/音频\n")
        for v in videos[:20]:
            lines.append(f"- {v}")

    if folders:
        lines.append("\n## 任务文件夹\n")
        for f in folders[:30]:
            if isinstance(f, dict):
                name = f.get("name", "?")
                size = f.get("size", 0)
                lines.append(f"- **{name}** ({size:.1f} MB)")
            else:
                lines.append(f"- **{f}**")

    return "\n".join(lines) if lines else "暂无历史记录"


@mcp.tool()
async def v2t_folder_translate(
    ctx: Context,
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
        resp = await _post(ctx, "/api/folders/translate", {
            "folder_name": folder_name,
            "target_lang": target_lang,
            "online_profile": online_profile,
            "online_model": online_model,
        })
    except httpx.HTTPStatusError as e:
        return f"❌ 翻译失败: {_http_err(e)}"

    job_id = resp.get("job_id", "")
    if job_id:
        return await _poll_job(ctx, job_id, expect_translation=True)

    return _fmt_json(resp)


@mcp.tool()
async def v2t_upload_cookie(ctx: Context, file_path: str) -> str:
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
            ctx,
            "/api/upload_cookie",
            {},
            file_path=file_path,
            file_field="cookie_file",
        )
    except httpx.HTTPStatusError as e:
        return f"❌ 上传失败: {_http_err(e)}"
    return "✅ Cookie 文件已上传"


@mcp.tool()
async def v2t_all_output_files(ctx: Context) -> str:
    """列出所有文件夹的全部输出文件（跨文件夹汇总）。"""
    try:
        resp = await _get(ctx, "/api/folders/all-output-files")
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

async def _poll_job(
    ctx: Context,
    job_id: str,
    expect_translation: bool = False,
    max_wait: float = 600,
    interval: float = 2.0,
) -> str:
    """轮询任务直到完成，返回结果文本。"""
    elapsed = 0.0
    last_status = ""
    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval

        try:
            job = await _get(ctx, f"/api/jobs/{job_id}")
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
            current_job = job.get("current_job", "")
            prefix = job.get("current_prefix", "")

            result = {"job_id": job_id, "status": status, "current_job": current_job}

            if current_job:
                try:
                    files_resp = await _get(ctx, f"/api/jobs/{job_id}/files")
                    files = files_resp.get("files", [])
                    result["files"] = files
                except Exception:
                    files = []

                # 读取 SRT 内容（通过 API）
                srt_name = f"{prefix}.srt" if prefix else None
                if srt_name and srt_name in files:
                    try:
                        result["srt_content"] = await _read_text_from_api(
                            ctx, f"/api/jobs/{job_id}/download-file", {"file_name": srt_name}
                        )
                    except Exception:
                        pass

                # 读取翻译后的 SRT
                if expect_translation:
                    for lang in ["zh", "en", "ja", "ko"]:
                        translated_name = f"{prefix}.{lang}.srt" if prefix else None
                        if translated_name and translated_name in files:
                            try:
                                result[f"{lang}_srt_content"] = await _read_text_from_api(
                                    ctx, f"/api/jobs/{job_id}/download-file", {"file_name": translated_name}
                                )
                            except Exception:
                                pass

            return _fmt_json(result)

    return f"⏰ 任务超时（{max_wait}s），最后状态: {last_status}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
