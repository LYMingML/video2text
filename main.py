#!/usr/bin/env python3
"""
视频转字幕 WebUI
支持 FunASR（中文首选）和 faster-whisper（多语言）双后端
NVIDIA GPU 加速 | 局域网访问 | systemd 自启动

用法：
    python main.py              # 启动 WebUI (默认端口 7860)
    python main.py --port 8080  # 指定端口
"""

import os
import re
import sys
import shutil
import logging
import argparse
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterator

import gradio as gr

# 把项目根目录加入 sys.path，确保子模块可导入
sys.path.insert(0, str(Path(__file__).parent))

from utils.audio import extract_audio, get_audio_duration, cleanup
from utils.audio import split_audio_chunks
from utils.subtitle import (
    segments_to_srt,
    segments_to_plain,
    save_srt,
    save_plain,
    normalize_segments_timeline,
    collect_plain_text,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("video2text")

# ---------------------------------------------------------------------------
# 工作目录：每个上传文件对应一个子文件夹
# ---------------------------------------------------------------------------
WORKSPACE_DIR = Path(__file__).parent / "workspace"
WORKSPACE_DIR.mkdir(exist_ok=True)

STOP_EVENT = threading.Event()


def _dir_size_bytes(dir_path: Path) -> int:
    total = 0
    for f in dir_path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def _list_job_folders(max_items: int = 200) -> list[str]:
    job_dirs = [d for d in WORKSPACE_DIR.iterdir() if d.is_dir()]
    job_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [d.name for d in job_dirs[:max_items]]


def _folder_dropdown_update(current: str | None = None):
    choices = _list_job_folders()
    if current in choices:
        value = current
    else:
        value = choices[0] if choices else None
    return gr.update(choices=choices, value=value)


def _workspace_history_markdown(max_jobs: int = 30) -> str:
    """生成 workspace 历史文件夹大小概览（仅目录大小）。"""
    job_dirs = [d for d in WORKSPACE_DIR.iterdir() if d.is_dir()]
    if not job_dirs:
        return "### 📂 历史上传\n暂无历史记录。上传后会显示在这里。"

    job_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    job_dirs = job_dirs[:max_jobs]

    lines = ["### 📂 历史上传", ""]
    for job_dir in job_dirs:
        size_mb = _dir_size_bytes(job_dir) / (1024 * 1024)
        lines.append(f"- 📁 **{job_dir.name}/** ({size_mb:.2f} MB)")

    return "\n".join(lines)


def _delete_job_folder(folder_name: str | None):
    if not folder_name:
        return (
            "⚠️ 请先选择要删除的文件夹",
            _workspace_history_markdown(),
            _history_dropdown_update(None),
            _folder_dropdown_update(None),
        )

    workspace_root = WORKSPACE_DIR.resolve()
    target = (WORKSPACE_DIR / folder_name).resolve()

    if workspace_root not in target.parents:
        return (
            "❌ 非法目录，拒绝删除",
            _workspace_history_markdown(),
            _history_dropdown_update(None),
            _folder_dropdown_update(None),
        )

    if not target.exists() or not target.is_dir():
        return (
            "⚠️ 文件夹不存在或已删除",
            _workspace_history_markdown(),
            _history_dropdown_update(None),
            _folder_dropdown_update(None),
        )

    shutil.rmtree(target, ignore_errors=False)
    return (
        f"✅ 已删除 workspace/{folder_name}",
        _workspace_history_markdown(),
        _history_dropdown_update(None),
        _folder_dropdown_update(None),
    )


def _make_job_dir(original_path: str) -> Path:
    """根据上传文件名创建 workspace/<slug>/ 子目录，返回目录路径。"""
    stem = Path(original_path).stem
    # 保留中文、字母、数字，其余替换为下划线
    slug = re.sub(r'[^\w\u4e00-\u9fff]+', '_', stem).strip('_')[:60]
    slug = slug or "upload"
    job_dir = WORKSPACE_DIR / slug
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _parse_lang_code(choice: str) -> str:
    """从 'zh（普通话）' 形式的选项中提取语言代码 'zh'。"""
    if choice == "自动检测":
        return "auto"
    return choice.split("（")[0].split("(")[0].strip()


# ---------------------------------------------------------------------------
# 支持的视频/音频扩展名
# ---------------------------------------------------------------------------
SUPPORTED_EXTS = [
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg",
]

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"
}


def _list_uploaded_videos(max_items: int = 200) -> list[str]:
    """列出 workspace 中历史上传的视频文件（相对路径）。"""
    results: list[tuple[float, str]] = []
    for job_dir in WORKSPACE_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        for f in job_dir.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in VIDEO_EXTS:
                continue
            rel = f.relative_to(Path(__file__).parent).as_posix()
            results.append((f.stat().st_mtime, rel))

    results.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in results[:max_items]]


def _resolve_input_path(video_file, history_video: str | None) -> str | None:
    """优先使用新上传文件；若未上传则使用历史文件选择。"""
    if video_file is not None:
        return video_file if isinstance(video_file, str) else video_file.name

    if not history_video:
        return None

    base = Path(__file__).parent
    p = Path(history_video)
    resolved = p if p.is_absolute() else (base / p)
    return str(resolved)


def _history_dropdown_update(current: str | None = None):
    """生成历史视频下拉框的更新对象。"""
    choices = _list_uploaded_videos()
    if current in choices:
        value = current
    else:
        value = choices[0] if choices else None
    return gr.update(choices=choices, value=value)

# ---------------------------------------------------------------------------
# 核心转录函数
# ---------------------------------------------------------------------------

def _do_transcribe_stream(
    video_path: str,
    backend: str,
    language: str,
    whisper_model: str,
    funasr_model: str,
    file_prefix: str,
    device: str,
    job_dir: Path,
    log_cb: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, list[tuple[float, float, str]]]]:
    """提取音频并分片转录，逐步产出 segments。"""

    audio_path = str(job_dir / f"{file_prefix}.wav")
    lang_code = _parse_lang_code(language)

    try:
        # 1. 提取音频到 job 目录
        if log_cb:
            log_cb("[STEP] 提取音频...")
        extract_audio(video_path, audio_path)
        duration = get_audio_duration(audio_path)

        if backend == "FunASR（Paraformer）" and lang_code == "es":
            if log_cb:
                log_cb("[FALLBACK] FunASR 当前配置对西班牙语支持有限，自动切换 faster-whisper")
            backend = "faster-whisper（多语言）"

        logger.info(f"音频时长: {duration:.1f}s，后端: {backend}，语言: {lang_code}")
        if log_cb:
            log_cb(f"[ASR] 手动后端: {backend}")
            log_cb(f"[AUDIO] 时长: {duration:.1f}s")

        chunk_seconds = 60 if duration >= 180 else 30
        chunk_dir = job_dir / "chunks"
        if duration > chunk_seconds:
            chunk_items = split_audio_chunks(audio_path, str(chunk_dir), chunk_seconds=chunk_seconds)
        else:
            chunk_items = [(audio_path, 0.0, duration)]

        if not chunk_items:
            return []

        total_chunks = len(chunk_items)
        all_segments: list[tuple[float, float, str]] = []
        if log_cb:
            log_cb(f"[CHUNK] 分片数: {total_chunks}，粒度: {chunk_seconds}s")

        if backend == "FunASR（Paraformer）":
            from backend.funasr_backend import transcribe
            device_str = "cuda:0" if device == "CUDA" else "cpu"
            if log_cb:
                log_cb(f"[MODEL] FunASR: {funasr_model}")

            for idx, (chunk_path, start_s, end_s) in enumerate(chunk_items, start=1):
                if STOP_EVENT.is_set():
                    if log_cb:
                        log_cb("[STOP] 用户请求停止，结束转录")
                    yield "🛑 已停止转录", all_segments.copy()
                    return all_segments

                if log_cb:
                    log_cb(f"[CHUNK] {idx}/{total_chunks} 转写中: {start_s:.0f}s-{end_s:.0f}s")

                def _progress_cb(ratio: float, msg: str, _idx=idx, _total=total_chunks):
                    if log_cb:
                        log_cb(f"[PROGRESS][{_idx}/{_total}] {msg}")

                segs = transcribe(
                    chunk_path,
                    model_name=funasr_model,
                    language=lang_code,
                    device=device_str,
                    progress_cb=_progress_cb,
                )
                if start_s > 0:
                    segs = [(s + start_s, e + start_s, t) for s, e, t in segs]
                all_segments.extend(segs)

                progress_text = f"⏳ 转写进度：{idx}/{total_chunks} 块（约 {min(end_s, duration):.0f}s / {duration:.0f}s）"
                yield progress_text, all_segments.copy()

        else:  # faster-whisper
            from backend.whisper_backend import transcribe
            if log_cb:
                log_cb(f"[MODEL] Whisper: {whisper_model}")

            for idx, (chunk_path, start_s, end_s) in enumerate(chunk_items, start=1):
                if STOP_EVENT.is_set():
                    if log_cb:
                        log_cb("[STOP] 用户请求停止，结束转录")
                    yield "🛑 已停止转录", all_segments.copy()
                    return all_segments

                if log_cb:
                    log_cb(f"[CHUNK] {idx}/{total_chunks} 转写中: {start_s:.0f}s-{end_s:.0f}s")

                def _progress_cb(ratio: float, msg: str, _idx=idx, _total=total_chunks):
                    if log_cb:
                        log_cb(f"[PROGRESS][{_idx}/{_total}] {msg}")

                segs = transcribe(
                    chunk_path,
                    model_name=whisper_model,
                    language=lang_code if lang_code != "auto" else None,
                    device=device.lower(),
                    compute_type="int8",   # Tesla P4 (sm_61) 只支持 int8
                    progress_cb=_progress_cb,
                )
                if start_s > 0:
                    segs = [(s + start_s, e + start_s, t) for s, e, t in segs]
                all_segments.extend(segs)

                progress_text = f"⏳ 转写进度：{idx}/{total_chunks} 块（约 {min(end_s, duration):.0f}s / {duration:.0f}s）"
                yield progress_text, all_segments.copy()

        if log_cb:
            log_cb("[STEP] 生成字幕文件...")

        if chunk_dir.exists():
            shutil.rmtree(chunk_dir, ignore_errors=True)

        return all_segments

    except Exception:
        # 保留 audio.wav 供排查，不删除
        raise


# ---------------------------------------------------------------------------
# Gradio 处理函数
# ---------------------------------------------------------------------------

def process(
    video_file,
    history_video: str,
    backend: str,
    language: str,
    whisper_model: str,
    funasr_model: str,
    device: str,
):
    """
    Gradio 主处理函数。
    yield 顺序：(status_text, plain_text, srt_file_path, txt_file_path, history_markdown, history_dropdown, log_text)
    """
    logs: list[str] = []

    def push_log(message: str):
        logs.append(message)
        if len(logs) > 300:
            del logs[:100]

    def dump_log() -> str:
        return "\n".join(logs)

    STOP_EVENT.clear()
    push_log("[INIT] 请求开始")
    video_path = _resolve_input_path(video_file, history_video)
    if video_path is None:
        push_log("[ERROR] 未选择上传文件或历史文件")
        yield (
            "❌ 处理失败（详情见底部日志）",
            "",
            None,
            None,
            _workspace_history_markdown(),
            _history_dropdown_update(history_video),
            dump_log(),
        )
        return

    if not Path(video_path).exists():
        push_log(f"[ERROR] 文件不存在: {video_path}")
        yield (
            "❌ 处理失败（详情见底部日志）",
            "",
            None,
            None,
            _workspace_history_markdown(),
            _history_dropdown_update(history_video),
            dump_log(),
        )
        return

    try:
        push_log("[STEP] 初始化...")

        # 创建 job 目录，复制原始文件进去
        job_dir = _make_job_dir(video_path)
        orig_name = Path(video_path).name
        file_prefix = Path(orig_name).stem
        push_log(f"[INPUT] {orig_name}")
        push_log(f"[JOB] workspace/{job_dir.name}")
        dest = job_dir / orig_name
        if not dest.exists():
            shutil.copy2(video_path, dest)
            push_log(f"[COPY] 已复制原始文件到 {dest.name}")
        else:
            push_log(f"[COPY] 复用已存在原始文件 {dest.name}")
        logger.info(f"Job 目录: {job_dir}")

        yield (
            f"⏳ 处理中... 输出目录: workspace/{job_dir.name}",
            "",
            None,
            None,
            _workspace_history_markdown(),
            _history_dropdown_update(history_video),
            dump_log(),
        )

        push_log(f"[ASR] 后端={backend} 语言={language} 设备={device}")
        segments: list[tuple[float, float, str]] = []
        for progress_status, partial_segments in _do_transcribe_stream(
            video_path, backend, language, whisper_model, funasr_model,
            file_prefix, device, job_dir, push_log
        ):
            segments = partial_segments
            yield (
                progress_status,
                collect_plain_text(segments),
                None,
                None,
                _workspace_history_markdown(),
                _history_dropdown_update(history_video),
                dump_log(),
            )

        push_log(f"[ASR] 原始片段数: {len(segments)}")

        if STOP_EVENT.is_set():
            push_log("[STOP] 用户已停止，未生成字幕文件")
            yield (
                "🛑 已停止（未生成字幕文件）",
                collect_plain_text(segments),
                None,
                None,
                _workspace_history_markdown(),
                _history_dropdown_update(history_video),
                dump_log(),
            )
            return

        raw_plain_text = collect_plain_text(segments)
        cleaned_segments = normalize_segments_timeline(segments)
        push_log(f"[ASR] 清洗后片段数: {len(cleaned_segments)}")

        if not cleaned_segments:
            push_log("[WARN] 未识别到有效字幕片段")
            yield (
                "⚠️ 未识别到有效字幕（详情见底部日志）",
                "",
                None,
                None,
                _workspace_history_markdown(),
                _history_dropdown_update(history_video),
                dump_log(),
            )
            return

        # 保存到 job 目录
        display_plain_text = raw_plain_text or segments_to_plain(cleaned_segments, normalize=False)
        srt_path = save_srt(cleaned_segments, str(job_dir / f"{file_prefix}.srt"), normalize=False)
        txt_path = save_plain(cleaned_segments, str(job_dir / f"{file_prefix}.txt"), normalize=False)
        push_log(f"[OUT] SRT: {Path(srt_path).name}")
        push_log(f"[OUT] TXT: {Path(txt_path).name}")

        status = f"✅ 完成！共 {len(segments)} 条字幕 → workspace/{job_dir.name}/"
        push_log("[DONE] 任务完成")
        yield (
            status,
            display_plain_text,
            srt_path,
            txt_path,
            _workspace_history_markdown(),
            _history_dropdown_update(video_path if video_path.startswith("workspace/") else None),
            dump_log(),
        )

    except Exception as e:
        logger.exception("转录失败")
        push_log(f"[ERROR] {e}")
        yield (
            "❌ 处理失败（详情见底部日志）",
            "",
            None,
            None,
            _workspace_history_markdown(),
            _history_dropdown_update(history_video),
            dump_log(),
        )


# ---------------------------------------------------------------------------
# Gradio UI 布局
# ---------------------------------------------------------------------------

def build_ui() -> gr.Blocks:
    with gr.Blocks(
        title="视频转字幕",
    ) as demo:

        gr.Markdown(
            """
            # 🎬 视频转字幕
            支持 **FunASR Paraformer**（阿里，中文准确率高）和 **faster-whisper**（多语言）双后端，NVIDIA GPU 加速。
            每次处理结果保存至 `workspace/<文件名>/` 目录。
            """
        )

        with gr.Row():
            # ── 左侧：输入区 ──────────────────────────────────────────────
            with gr.Column(scale=1):
                video_input = gr.File(
                    label="上传视频 / 音频",
                    file_types=SUPPORTED_EXTS,
                    file_count="single",
                )

                history_video_select = gr.Dropdown(
                    label="或选择历史上传视频",
                    choices=_list_uploaded_videos(),
                    value=_list_uploaded_videos()[0] if _list_uploaded_videos() else None,
                    allow_custom_value=False,
                )

                history_md = gr.Markdown(
                    value=_workspace_history_markdown(),
                )
                refresh_history_btn = gr.Button("🔄 刷新历史列表")

                folder_manage_select = gr.Dropdown(
                    label="选择要删除的历史文件夹",
                    choices=_list_job_folders(),
                    value=_list_job_folders()[0] if _list_job_folders() else None,
                    allow_custom_value=False,
                )
                delete_folder_btn = gr.Button("🗑️ 删除选中文件夹", variant="stop")
                folder_manage_status = gr.Textbox(
                    label="文件管理状态",
                    value="",
                    interactive=False,
                    max_lines=2,
                )

                backend_select = gr.Radio(
                    label="识别后端",
                    choices=["FunASR（Paraformer）", "faster-whisper（多语言）"],
                    value="FunASR（Paraformer）",
                )

                language_select = gr.Dropdown(
                    label="语言",
                    choices=[
                        "自动检测",
                        "zh（普通话）",
                        "yue（粤语）",
                        "en（英语）",
                        "ja（日语）",
                        "ko（韩语）",
                        "es（西班牙语）",
                    ],
                    value="自动检测",
                )

                with gr.Accordion("高级选项", open=False):
                    funasr_model_select = gr.Dropdown(
                        label="FunASR 模型（仅 FunASR 后端生效）",
                        choices=[
                            "paraformer-zh ⭐ 普通话精度推荐",
                            "paraformer ⭐ 全量普通话大模型",
                            "paraformer-zh-streaming ▶ 低延迟流式",
                            "paraformer-zh-spk ▶ 角色区分优化",
                            "paraformer-en ▶ 英文优化",
                            "paraformer-en-spk ▶ 英文说话人区分",
                            "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch ▶ 中文全路径(推荐)",
                            "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online ▶ 中文流式全路径",
                            "iic/speech_paraformer-large-vad-punc_asr_nat-en-16k-common-vocab10020 ▶ 英文全路径",
                            "iic/SenseVoiceSmall ⭐ 多语言(中/粤/英/日/韩)",
                            "iic/SenseVoice-Small ▶ 多语言备用源",
                            "EfficientParaformer-large-zh ▶ 大模型长语音",
                            "EfficientParaformer-zh-en ▶ 中英双语场景",
                            "speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch ▶ 全路径（含 VAD/Punc）",
                        ],
                        value="paraformer-zh ⭐ 普通话精度推荐",
                    )
                    whisper_model_select = gr.Dropdown(
                        label="Whisper 模型（仅 faster-whisper 后端生效）",
                        choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"],
                        value="medium",
                    )
                    device_select = gr.Radio(
                        label="计算设备",
                        choices=["CUDA", "CPU"],
                        value="CUDA",
                    )

                submit_btn = gr.Button("🚀 开始转录", variant="primary", size="lg")
                stop_btn = gr.Button("⏹️ 停止转录", variant="secondary")

            # ── 右侧：输出区 ──────────────────────────────────────────────
            with gr.Column(scale=2):
                status_text = gr.Textbox(
                    label="状态",
                    value="等待上传文件...",
                    interactive=False,
                    max_lines=2,
                )
                plain_output = gr.Textbox(
                    label="识别文本（可直接复制）",
                    interactive=False,
                    lines=18,
                    max_lines=40,
                    elem_classes=["output-text"],
                )
                with gr.Row():
                    srt_download = gr.File(label="下载 SRT 字幕", interactive=False)
                    txt_download = gr.File(label="下载纯文本", interactive=False)

        with gr.Row():
            log_output = gr.Textbox(
                label="运行日志（统一输出，可滚动查看）",
                interactive=False,
                lines=12,
                max_lines=24,
                value="",
            )

        # ── 事件绑定 ──────────────────────────────────────────────────────
        submit_event = submit_btn.click(
            fn=process,
            inputs=[
                video_input,
                history_video_select,
                backend_select,
                language_select,
                whisper_model_select,
                funasr_model_select,
                device_select,
            ],
            outputs=[status_text, plain_output, srt_download, txt_download, history_md, history_video_select, log_output],
        )

        def request_stop(current_log: str):
            STOP_EVENT.set()
            logs = current_log.splitlines() if current_log else []
            logs.append("[USER] 收到停止请求，将在当前分片结束后停止")
            if len(logs) > 300:
                logs = logs[-300:]
            return "🛑 已请求停止（等待当前分片完成）", "\n".join(logs)

        stop_btn.click(
            fn=request_stop,
            inputs=[log_output],
            outputs=[status_text, log_output],
            cancels=[submit_event],
        )

        def _refresh_history_and_dropdown(current_video, current_folder):
            return (
                _workspace_history_markdown(),
                _history_dropdown_update(current_video),
                _folder_dropdown_update(current_folder),
            )

        refresh_history_btn.click(
            fn=_refresh_history_and_dropdown,
            inputs=[history_video_select, folder_manage_select],
            outputs=[history_md, history_video_select, folder_manage_select],
        )

        delete_folder_btn.click(
            fn=_delete_job_folder,
            inputs=[folder_manage_select],
            outputs=[folder_manage_status, history_md, history_video_select, folder_manage_select],
        )

    return demo


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="视频转字幕 WebUI")
    parser.add_argument("--port", type=int, default=7880, help="监听端口 (默认: 7880)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0 局域网可访问)")
    parser.add_argument("--share", action="store_true", help="生成 Gradio 公共链接")
    parser.add_argument("--ssl-certfile", default=None, help="HTTPS 证书路径（PEM）")
    parser.add_argument("--ssl-keyfile", default=None, help="HTTPS 私钥路径（PEM）")
    args = parser.parse_args()

    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("启用 HTTPS 时需同时提供 --ssl-certfile 与 --ssl-keyfile")

    demo = build_ui()
    ssl_verify = False if args.ssl_certfile and args.ssl_keyfile else True
    demo.queue(max_size=5).launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        show_error=False,
        quiet=False,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        ssl_verify=ssl_verify,
        theme=gr.themes.Soft(),
        css=".output-text textarea { font-family: 'PingFang SC', 'Microsoft YaHei', monospace; }",
    )


if __name__ == "__main__":
    main()
