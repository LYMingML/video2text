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
import json
import shutil
import zipfile
import sqlite3
import logging
import argparse
import subprocess
import tempfile
import time
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
from utils.translate import translate_segments_to_chinese, list_available_models
from utils.online_models import load_profiles, save_profiles, upsert_profile, delete_profile

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
TEMP_VIDEO_DIR = WORKSPACE_DIR / "temp_video"
TEMP_VIDEO_DIR.mkdir(exist_ok=True)


def _get_temp_video_keep_count():
    """从环境变量读取临时视频保留数量"""
    try:
        return int(os.environ.get("TEMP_VIDEO_KEEP_COUNT", "5"))
    except (ValueError, TypeError):
        return 5


TEMP_VIDEO_KEEP_COUNT = _get_temp_video_keep_count()

# 正在转录的视频路径（用于清理临时文件时跳过）
_TRANSCRIBING_VIDEO: str | None = None
_TRANSCRIBING_VIDEO_LOCK = threading.Lock()


def set_transcribing_video(path: str | None):
    """设置当前正在转录的视频路径"""
    global _TRANSCRIBING_VIDEO
    with _TRANSCRIBING_VIDEO_LOCK:
        _TRANSCRIBING_VIDEO = path


def get_transcribing_video() -> str | None:
    """获取当前正在转录的视频路径"""
    with _TRANSCRIBING_VIDEO_LOCK:
        return _TRANSCRIBING_VIDEO


# 文件指纹数据库
_FINGERPRINT_DB_PATH = WORKSPACE_DIR / "fingerprints.db"
_fingerprint_db_lock = threading.Lock()


def _init_fingerprint_db():
    """初始化文件指纹数据库"""
    conn = sqlite3.connect(_FINGERPRINT_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_fingerprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            file_size INTEGER NOT NULL,
            head_50 BLOB,
            tail_50 BLOB,
            updated_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_size ON file_fingerprints(file_size)")
    conn.commit()
    conn.close()


_init_fingerprint_db()


def _cleanup_fingerprint_db():
    """清理数据库中不存在于缓存目录的文件记录"""
    with _fingerprint_db_lock:
        conn = sqlite3.connect(_FINGERPRINT_DB_PATH)
        cursor = conn.execute("SELECT file_path FROM file_fingerprints")
        existing_paths = [row[0] for row in cursor.fetchall()]

        deleted = 0
        for path_str in existing_paths:
            if not Path(path_str).exists():
                conn.execute("DELETE FROM file_fingerprints WHERE file_path = ?", (path_str,))
                deleted += 1

        conn.commit()
        conn.close()
        return deleted


def _get_file_fingerprint(file_path: Path) -> dict:
    """获取文件指纹：大小、文件头50字节、文件尾50字节"""
    try:
        stat = file_path.stat()
        fingerprint = {
            "path": str(file_path),
            "size": stat.st_size,
        }
        if stat.st_size > 0:
            try:
                with open(file_path, "rb") as f:
                    head = f.read(50)
                    fingerprint["head_50"] = head
                    if stat.st_size > 50:
                        f.seek(-50, 2)
                        tail = f.read(50)
                        fingerprint["tail_50"] = tail
            except OSError:
                pass
        return fingerprint
    except OSError:
        return {}


def _save_fingerprint_to_db(fingerprint: dict):
    """保存文件指纹到数据库"""
    if not fingerprint or "path" not in fingerprint:
        return
    with _fingerprint_db_lock:
        conn = sqlite3.connect(_FINGERPRINT_DB_PATH)
        conn.execute("""
            INSERT OR REPLACE INTO file_fingerprints
            (file_path, file_size, head_50, tail_50, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            fingerprint["path"],
            fingerprint.get("size", 0),
            fingerprint.get("head_50"),
            fingerprint.get("tail_50"),
            time.time()
        ))
        conn.commit()
        conn.close()


def _find_duplicate_file(src_path: Path, search_dir: Path) -> Path | None:
    """在指定目录中查找与源文件内容相同的文件（比较大小、文件头50字节、文件尾50字节）"""
    if not src_path.exists() or not search_dir.exists():
        return None

    src_fp = _get_file_fingerprint(src_path)
    if not src_fp or src_fp.get("size", 0) == 0:
        return None

    # 先清理数据库中的无效记录
    _cleanup_fingerprint_db()

    # 确保缓存目录中的文件指纹都已入库
    for f in search_dir.iterdir():
        if f.is_file() and f.suffix.lower() in _SOURCE_MEDIA_EXTS:
            if f.resolve() != src_path.resolve():
                fp = _get_file_fingerprint(f)
                if fp:
                    _save_fingerprint_to_db(fp)

    # 在数据库中查找匹配
    with _fingerprint_db_lock:
        conn = sqlite3.connect(_FINGERPRINT_DB_PATH)
        cursor = conn.execute("""
            SELECT file_path FROM file_fingerprints
            WHERE file_size = ? AND file_path != ?
        """, (src_fp["size"], str(src_path)))
        candidates = [row[0] for row in cursor.fetchall()]
        conn.close()

    # 对候选文件进行精确匹配（比较文件头和文件尾）
    for candidate_path in candidates:
        candidate = Path(candidate_path)
        if not candidate.exists():
            continue
        if candidate.resolve() == src_path.resolve():
            continue

        cand_fp = _get_file_fingerprint(candidate)
        if not cand_fp:
            continue

        # 比较文件头50字节和文件尾50字节
        if (cand_fp.get("head_50") == src_fp.get("head_50") and
            cand_fp.get("tail_50") == src_fp.get("tail_50")):
            return candidate

    return None


STOP_EVENT = threading.Event()


def _dir_size_bytes(dir_path: Path) -> int:
    total = 0
    for f in dir_path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def _is_temp_video_dir(path: Path) -> bool:
    try:
        return path.resolve() == TEMP_VIDEO_DIR.resolve()
    except OSError:
        return False


def _iter_workspace_job_dirs() -> list[Path]:
    job_dirs = [d for d in WORKSPACE_DIR.iterdir() if d.is_dir() and not _is_temp_video_dir(d)]
    job_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return job_dirs


def _list_job_folders(max_items: int = 200) -> list[str]:
    return [d.name for d in _iter_workspace_job_dirs()[:max_items]]


def _list_job_folders_meta(max_items: int = 200) -> list[dict]:
    """返回文件夹列表，包含名称、修改时间和大小"""
    job_dirs = list(_iter_workspace_job_dirs())
    if not job_dirs:
        return []
    job_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for d in job_dirs[:max_items]:
        try:
            mtime = int(d.stat().st_mtime)
            size = _dir_size_bytes(d)
            size_mb = size / (1024 * 1024)
        except OSError:
            mtime = 0
            size_mb = 0
        result.append({
            "name": d.name,
            "mtime": mtime,
            "size": size_mb,
        })
    return result


def _folder_dropdown_update(current: str | None = None):
    choices = _list_job_folders()
    if current in choices:
        value = current
    else:
        value = choices[0] if choices else None
    return gr.update(choices=choices, value=value)


def _workspace_history_markdown(max_jobs: int = 30) -> str:
    """生成 workspace 历史文件夹大小概览（仅目录大小）。"""
    job_dirs = _iter_workspace_job_dirs()
    if not job_dirs:
        return "### 📂 历史上传\n暂无历史记录。上传后会显示在这里。"

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

    if folder_name == TEMP_VIDEO_DIR.name:
        return (
            "❌ temp_video 为系统保留目录，拒绝删除",
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
    # 保留中文、字母、数字，其余替换为下划线，最多取前 20 字符
    slug = re.sub(r'[^\w\u4e00-\u9fff]+', '_', stem).strip('_')[:20]
    slug = slug or "upload"
    job_dir = WORKSPACE_DIR / slug
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _parse_lang_code(choice: str) -> str:
    """从 'zh（普通话）' 形式的选项中提取语言代码 'zh'。"""
    if choice == "自动检测":
        return "auto"
    return choice.split("（")[0].split("(")[0].strip()


def _is_funasr_multilingual_model(model_name: str) -> bool:
    """判断 FunASR 模型是否具备较好的多语言能力。"""
    normalized = model_name.split(" ")[0].strip().lower()
    multilingual_markers = [
        "sensevoice",
        "zh-en",
        "seaco",
    ]
    return any(marker in normalized for marker in multilingual_markers)


def _looks_non_chinese_text(text: str) -> bool:
    """基于字符分布的轻量规则，判断文本是否主要为非中文。"""
    normalized = re.sub(r"\s+", "", text)
    if not normalized:
        return False

    zh_chars = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    ja_chars = len(re.findall(r"[\u3040-\u30ff]", normalized))
    ko_chars = len(re.findall(r"[\uac00-\ud7af]", normalized))
    latin_chars = len(re.findall(r"[A-Za-z]", normalized))

    total = len(normalized)
    zh_ratio = zh_chars / total

    # 明显日/韩/拉丁语系，且中文占比很低，视为非中文。
    if zh_ratio < 0.25 and (ja_chars + ko_chars + latin_chars) >= 12:
        return True
    return False


def _guess_source_lang(lang_code: str, plain_text: str) -> str:
    """推断翻译源语言，供模型选择使用。"""
    if lang_code != "auto":
        return lang_code

    if re.search(r"[\u3040-\u30ff]", plain_text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", plain_text):
        return "ko"

    lower_text = plain_text.lower()
    es_markers = [" que ", " de ", " la ", " el ", " y ", " por ", " para "]
    if any(token in lower_text for token in es_markers):
        return "es"

    # 自动场景默认按英语兜底。
    return "en"


def _pick_funasr_model_for_language(
    backend: str,
    lang_code: str,
    selected_model: str,
    log_cb: Callable[[str], None] | None,
) -> str:
    """
    当用户选择 FunASR 时，根据语言自动选择更合适的 FunASR 模型。
    多语言场景优先使用 SenseVoiceSmall，避免切换到其他后端。
    """
    if backend != "FunASR（Paraformer）":
        return selected_model

    model = selected_model
    multilingual_best = "iic/SenseVoiceSmall"

    # 非中文语言：自动切换到 FunASR 多语言强模型（说话人分离模型不切换）。
    non_zh_langs = {"en", "ja", "ko", "es"}
    if lang_code in non_zh_langs:
        model_key = model.split(" ")[0].strip().lower()
        if model_key.split("/")[-1].split(" ")[0] != "sensevoicesmall" and not any(k in model_key for k in ("-spk", "speaker")):
            if log_cb:
                log_cb(
                    f"[MODEL-AUTO] 检测到 {lang_code} 语言，FunASR 自动切换为 {multilingual_best}"
                )
            model = multilingual_best
        return model

    # 说话人分离模型不做自动切换，保留用户的选择。
    model_key = model.split(" ")[0].strip().lower()
    if any(k in model_key for k in ("-spk", "speaker")):
        return model

    # 自动检测时优先多语言模型，减少跨语种误配导致的慢响应。
    if lang_code == "auto" and not _is_funasr_multilingual_model(model):
        if log_cb:
            log_cb(
                f"[MODEL-AUTO] 自动检测语言场景，FunASR 自动切换为 {multilingual_best}"
            )
        model = multilingual_best

    return model


def _has_nvidia_gpu() -> bool:
    """检测 NVIDIA GPU 是否可用（兼容 WSL2）。"""
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    # 空字符串表示"未设置"（应继续探测硬件），只有明确禁用值才跳过
    if visible in {"-1", "none", "None"}:
        return False

    # 优先检查 torch.cuda（最可靠）
    try:
        import torch
        if torch.cuda.is_available():
            return True
    except ImportError:
        pass

    # 原生 Linux 路径检查
    if os.path.exists("/dev/nvidiactl") or os.path.exists("/proc/driver/nvidia"):
        return True

    # nvidia-smi fallback（WSL2 兼容）
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return result.returncode == 0
    except Exception:
        return False


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


_ALL_MEDIA_EXTS = {ext.lower() for ext in SUPPORTED_EXTS}
_AUDIO_EXTS = _ALL_MEDIA_EXTS - VIDEO_EXTS
_SOURCE_MEDIA_EXTS = {ext for ext in _ALL_MEDIA_EXTS if ext != ".wav"}


def _is_supported_media_path(path_like: str | Path) -> bool:
    return Path(path_like).suffix.lower() in _ALL_MEDIA_EXTS


def _safe_media_name(filename: str) -> str:
    raw_name = Path(filename or "media").name
    stem = re.sub(r'[/\\\x00]', '', Path(raw_name).stem).strip()[:120] or "media"
    suffix = Path(raw_name).suffix[:20]
    return f"{stem}{suffix}"


def _unique_file_path(dir_path: Path, filename: str) -> Path:
    candidate = dir_path / _safe_media_name(filename)
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        alt = dir_path / f"{stem}_{index}{suffix}"
        if not alt.exists():
            return alt
        index += 1


def _prune_temp_video_dir(max_items: int = TEMP_VIDEO_KEEP_COUNT):
    """清理临时视频目录

    规则：
    1. 不清理正在转录的视频
    2. 文件数量超过 max_items 时，清理一小时前的文件
    """
    TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    # 获取正在转录的视频路径
    transcribing_video = get_transcribing_video()

    files = [p for p in TEMP_VIDEO_DIR.iterdir() if p.is_file()]

    # 过滤掉正在转录的视频
    if transcribing_video:
        transcribing_path = Path(transcribing_video).resolve()
        files = [f for f in files if f.resolve() != transcribing_path]

    # 按修改时间排序（最新的在前）
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # 如果文件数量超过限制，清理一小时前的文件
    if len(files) > max_items:
        import time
        one_hour_ago = time.time() - 3600  # 1小时 = 3600秒

        # 保留最新的 max_items 个文件，清理一小时前的旧文件
        for old in files[max_items:]:
            try:
                if old.stat().st_mtime < one_hour_ago:
                    old.unlink()
            except OSError:
                pass



def _stage_source_media_to_temp_video(input_path: str, preferred_name: str | None = None) -> str:
    src = Path(input_path)
    ext = src.suffix.lower()
    if ext not in _SOURCE_MEDIA_EXTS:
        return str(src)

    TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    if src.exists() and _is_temp_video_dir(src.parent):
        try:
            src.touch()
        except OSError:
            pass
        _prune_temp_video_dir()
        return str(src)

    # 检查是否已存在相同文件
    duplicate = _find_duplicate_file(src, TEMP_VIDEO_DIR)
    if duplicate:
        try:
            duplicate.touch()  # 更新访问时间
        except OSError:
            pass
        _prune_temp_video_dir()
        return str(duplicate)

    target = _unique_file_path(TEMP_VIDEO_DIR, preferred_name or src.name)
    if src.exists():
        try:
            if src.resolve().is_relative_to(WORKSPACE_DIR.resolve()):
                shutil.move(str(src), str(target))
            else:
                shutil.copy2(src, target)
        except AttributeError:
            src_resolved = src.resolve()
            workspace_resolved = WORKSPACE_DIR.resolve()
            if workspace_resolved in src_resolved.parents:
                shutil.move(str(src), str(target))
            else:
                shutil.copy2(src, target)
    _prune_temp_video_dir()
    return str(target)


def _cleanup_job_source_media(job_dir: Path):
    for entry in job_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in _SOURCE_MEDIA_EXTS:
            continue
        try:
            entry.unlink()
        except OSError:
            pass


def _resolve_job_dir_for_input(input_path: str) -> Path:
    src = Path(input_path)
    if src.exists() and src.suffix.lower() == ".wav":
        try:
            if src.resolve().is_relative_to(WORKSPACE_DIR.resolve()) and not _is_temp_video_dir(src.parent):
                return src.parent
        except AttributeError:
            src_resolved = src.resolve()
            workspace_resolved = WORKSPACE_DIR.resolve()
            if workspace_resolved in src_resolved.parents and not _is_temp_video_dir(src.parent):
                return src.parent
    return _make_job_dir(input_path)


def _list_uploaded_videos(max_items: int = 200) -> list[str]:
    """列出 workspace 中历史上传的视频/音频文件（相对路径）。"""
    results: list[tuple[int, float, str]] = []
    workspace_root = WORKSPACE_DIR.parent
    for job_dir in _iter_workspace_job_dirs():
        for f in job_dir.iterdir():
            if not f.is_file() or f.suffix.lower() != ".wav":
                continue
            rel = f.relative_to(workspace_root).as_posix()
            results.append((0, f.stat().st_mtime, rel))

    if TEMP_VIDEO_DIR.exists():
        for f in TEMP_VIDEO_DIR.iterdir():
            if not f.is_file() or f.suffix.lower() not in _SOURCE_MEDIA_EXTS:
                continue
            ext = f.suffix.lower()
            rel = f.relative_to(workspace_root).as_posix()
            media_rank = 1 if ext in _AUDIO_EXTS else 2
            results.append((media_rank, f.stat().st_mtime, rel))

    results.sort(key=lambda item: (item[0], -item[1], item[2]))
    deduped: list[str] = []
    seen_stems: set[str] = set()
    for _rank, _mtime, rel in results:
        stem_key = Path(rel).stem.lower()
        if stem_key in seen_stems:
            continue
        seen_stems.add(stem_key)
        deduped.append(rel)
        if len(deduped) >= max_items:
            break
    return deduped


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


def _strip_fractional_time(text: str) -> str:
    """兜底去除时间字符串中的小数秒，如 00:03:12.345 -> 00:03:12。"""
    text = re.sub(r"(\d{2}:\d{2}:\d{2})\.\d+", r"\1", text)
    text = re.sub(r"(\d{1,2}:\d{2})\.\d+", r"\1", text)
    return text


def _meta_path(job_dir: Path) -> Path:
    return job_dir / "task_meta.json"


def _save_task_meta(job_dir: Path, meta: dict):
    _meta_path(job_dir).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_task_meta(job_dir: Path) -> dict:
    p = _meta_path(job_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _profile_names(profiles: list[dict]) -> list[str]:
    names = [str(p.get("name", "")).strip() for p in profiles]
    return [n for n in names if n]


def _find_profile(profiles: list[dict], name: str | None) -> dict | None:
    if not profiles:
        return None
    name = (name or "").strip()
    for p in profiles:
        if p.get("name") == name:
            return p
    return profiles[0]


def _model_dropdown_from_profile(profile: dict | None):
    if not profile:
        return gr.update(choices=[], value=None)
    models = profile.get("models", []) or []
    default_model = str(profile.get("default_model", "")).strip()
    if default_model and default_model not in models:
        models = [default_model, *models]
    value = default_model or (models[0] if models else None)
    return gr.update(choices=models, value=value)


def _profile_dropdown_update(profiles: list[dict], current: str | None):
    names = _profile_names(profiles)
    value = current if current in names else (names[0] if names else None)
    return gr.update(choices=names, value=value)


def _save_profiles_with_active(profiles: list[dict], active: str | None):
    save_profiles(profiles, active_profile=active)
    fresh_profiles, fresh_active = load_profiles()
    return fresh_profiles, fresh_active


def _fetch_models_and_persist(profile_name: str, base_url: str, api_key: str, profiles: list[dict]):
    name = (profile_name or "").strip()
    if not name:
        return "⚠️ 请先填写配置名称", profiles, gr.update(choices=[], value=None), gr.update(choices=[], value=None), gr.update(), gr.update()

    use_base = (base_url or "").strip()
    use_key = (api_key or "").strip()
    if not use_base or not use_key:
        return "⚠️ 请先填写 base_url 和 api_key", profiles, gr.update(choices=[], value=None), gr.update(choices=[], value=None), gr.update(), gr.update()

    try:
        models = list_available_models(use_base, use_key)
    except Exception as exc:
        return f"❌ 获取模型失败: {exc}", profiles, gr.update(choices=[], value=None), gr.update(choices=[], value=None), gr.update(), gr.update()

    profile = _find_profile(profiles, name) or {"name": name, "base_url": use_base, "api_key": use_key}
    profile["name"] = name
    profile["base_url"] = use_base
    profile["api_key"] = use_key
    profile["models"] = models
    if not str(profile.get("default_model", "")).strip() and models:
        profile["default_model"] = models[0]

    profiles = upsert_profile(profiles, profile)
    profiles, active = _save_profiles_with_active(profiles, name)
    selected = _find_profile(profiles, active)
    model_update = _model_dropdown_from_profile(selected)

    msg = f"✅ 获取到 {len(models)} 个可用模型，并已写入 .env"
    return (
        msg,
        profiles,
        model_update,
        model_update,
        _profile_dropdown_update(profiles, active),
        _profile_dropdown_update(profiles, active),
    )


def _save_profile_config(profile_name: str, base_url: str, api_key: str, default_model: str, profiles: list[dict]):
    name = (profile_name or "").strip()
    if not name:
        return "⚠️ 配置名称不能为空", profiles, gr.update(), gr.update(), gr.update(), gr.update()

    profile = _find_profile(profiles, name) or {}
    profile["name"] = name
    profile["base_url"] = (base_url or "").strip()
    profile["api_key"] = (api_key or "").strip()
    profile["default_model"] = (default_model or "").strip()
    if not isinstance(profile.get("models", []), list):
        profile["models"] = []
    if profile["default_model"] and profile["default_model"] not in profile["models"]:
        profile["models"] = [profile["default_model"], *profile["models"]]

    profiles = upsert_profile(profiles, profile)
    profiles, active = _save_profiles_with_active(profiles, name)
    selected = _find_profile(profiles, active)

    return (
        "✅ 配置已保存并同步到 .env",
        profiles,
        _profile_dropdown_update(profiles, active),
        _profile_dropdown_update(profiles, active),
        _model_dropdown_from_profile(selected),
        _model_dropdown_from_profile(selected),
    )


def _delete_profile_config(profile_name: str, profiles: list[dict], current_main_profile: str):
    name = (profile_name or "").strip()
    if not name:
        return "⚠️ 请先选择要删除的配置", profiles, gr.update(), gr.update(), gr.update(), gr.update(), ""

    profiles = delete_profile(profiles, name)
    keep_active = current_main_profile if current_main_profile != name else None
    profiles, active = _save_profiles_with_active(profiles, keep_active)
    selected = _find_profile(profiles, active)
    selected_name = selected.get("name", "") if selected else ""
    return (
        f"✅ 已删除配置: {name}（已写入 .env）",
        profiles,
        _profile_dropdown_update(profiles, active),
        _profile_dropdown_update(profiles, active),
        _model_dropdown_from_profile(selected),
        _model_dropdown_from_profile(selected),
        selected_name,
    )


def _on_profile_selected(profile_name: str, profiles: list[dict]):
    profile = _find_profile(profiles, profile_name)
    if not profile:
        return "", "", "", gr.update(choices=[], value=None), gr.update(choices=[], value=None)
    return (
        str(profile.get("name", "")),
        str(profile.get("base_url", "")),
        str(profile.get("api_key", "")),
        _model_dropdown_from_profile(profile),
        _model_dropdown_from_profile(profile),
    )


def _parse_srt_segments(srt_path: Path) -> list[tuple[float, float, str]]:
    """从 SRT 文件读取字幕段。"""
    if not srt_path.exists():
        return []

    text = srt_path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", text.strip())
    segments: list[tuple[float, float, str]] = []

    def _to_seconds(srt_ts: str) -> float:
        hms, ms = srt_ts.split(",")
        h, m, s = hms.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        ts_line = lines[1]
        if "-->" not in ts_line:
            continue
        start_srt, end_srt = [x.strip() for x in ts_line.split("-->", maxsplit=1)]
        try:
            start_s = _to_seconds(start_srt)
            end_s = _to_seconds(end_srt)
        except Exception:
            continue
        subtitle_text = "\n".join(lines[2:]).strip()
        if subtitle_text:
            segments.append((start_s, end_s, subtitle_text))

    return segments


OUTPUT_LANG_SUFFIXES = {"zh", "en", "ja", "ko", "es", "fr", "de", "ru"}


def _is_final_output_file(filename: str, file_prefix: str) -> bool:
    allowed = {
        f"{file_prefix}.srt",
        f"{file_prefix}.txt",
    }
    for lang in OUTPUT_LANG_SUFFIXES:
        allowed.add(f"{file_prefix}.{lang}.srt")
        allowed.add(f"{file_prefix}.{lang}.txt")
    return filename in allowed


def _build_all_bundle(job_dir: Path, file_prefix: str) -> str:
    """将 job_dir 下属于 prefix 的最终输出 .srt/.txt 文件打包为单个 zip。"""
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


def _finalize_plain_text_outputs(
    job_dir: Path,
    file_prefix: str,
    cleaned_segments: list[tuple[float, float, str]],
    plain_text: str,
) -> tuple[str, str, list[str]]:
    raw_text = plain_text or segments_to_plain(cleaned_segments, normalize=False)

    raw_txt_path = save_plain(
        cleaned_segments,
        str(job_dir / f"{file_prefix}.txt"),
        normalize=False,
    )
    return raw_text, raw_text, [Path(raw_txt_path).name]


def _resolve_current_job(current_job: str | None, history_video: str | None) -> Path | None:
    if current_job:
        p = WORKSPACE_DIR / current_job
        if p.exists() and p.is_dir():
            return p

    if not history_video:
        return None

    p = Path(history_video)
    if not p.is_absolute():
        p = (Path(__file__).parent / p)
    if p.exists() and p.is_file() and p.parent.exists():
        return p.parent
    return None


def _resolve_file_prefix(job_dir: Path, current_prefix: str | None) -> str | None:
    if current_prefix:
        return current_prefix

    meta = _load_task_meta(job_dir)
    if meta.get("file_prefix"):
        return str(meta["file_prefix"])

    for candidate in sorted(job_dir.glob("*.srt")):
        parts = candidate.name.split(".")
        if len(parts) == 2:
            return candidate.stem
        if len(parts) == 3 and parts[1] not in OUTPUT_LANG_SUFFIXES:
            return candidate.stem
    return None


def translate_current_job(
    history_video: str,
    current_job: str,
    current_prefix: str,
    current_log: str,
    online_profile_name: str,
    online_model_name: str,
):
    """
    手动翻译当前任务：读取原文字幕，生成中文字幕，再生成下载压缩包。
    yield 顺序：(status_text, plain_text, log_text, srt_bundle, txt_bundle, current_job, current_prefix)
    """
    logs = current_log.splitlines() if current_log else []
    latest_progress = ""

    def push_log(message: str):
        logs.append(message)
        if len(logs) > 400:
            del logs[:120]

    def dump_log() -> str:
        return "\n".join(logs)

    def _format_hms(seconds: float) -> str:
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    job_dir = _resolve_current_job(current_job, history_video)
    if not job_dir:
        push_log("[ERROR] 未找到当前任务目录，请先完成一次转录")
        yield (
            "❌ 翻译失败：请先转录一个视频",
            "",
            dump_log(),
            None,
            None,
            current_job,
            current_prefix,
        )
        return

    file_prefix = _resolve_file_prefix(job_dir, current_prefix)
    if not file_prefix:
        push_log("[ERROR] 未找到原文字幕文件")
        yield (
            "❌ 翻译失败：未找到原文字幕",
            "",
            dump_log(),
            None,
            None,
            job_dir.name,
            current_prefix,
        )
        return

    orig_srt = job_dir / f"{file_prefix}.srt"

    segments = _parse_srt_segments(orig_srt)
    if not segments:
        push_log("[ERROR] 原文字幕为空或解析失败")
        yield (
            "❌ 翻译失败：原文字幕为空",
            "",
            dump_log(),
            None,
            None,
            job_dir.name,
            file_prefix,
        )
        return

    meta = _load_task_meta(job_dir)
    source_lang = str(meta.get("source_lang") or "auto")
    profiles, active = load_profiles()
    profile = _find_profile(profiles, online_profile_name or active)
    use_base_url = str((profile or {}).get("base_url", "")).strip()
    use_api_key = str((profile or {}).get("api_key", "")).strip()
    use_model_name = (online_model_name or str((profile or {}).get("default_model", "")).strip()).strip()

    push_log(f"[TRANS] 任务: workspace/{job_dir.name}，源语言: {source_lang}")
    push_log(
        f"[TRANS] 在线配置: profile={str((profile or {}).get('name', '')) or 'N/A'} model={use_model_name or 'N/A'}"
    )
    yield (
        "⏳ 正在开始翻译...",
        "",
        dump_log(),
        None,
        None,
        job_dir.name,
        file_prefix,
    )

    translated: list[tuple[float, float, str]] = []
    total = len(segments)
    start_ts = time.time()

    for idx, seg in enumerate(segments, start=1):
        try:
            part = translate_segments_to_chinese(
                [seg],
                source_lang=source_lang,
                log_cb=push_log,
                base_url=use_base_url,
                api_key=use_api_key,
                model_name=use_model_name,
            )
        except Exception as exc:
            push_log(f"[ERROR] 翻译失败: {exc}")
            yield (
                "❌ 翻译失败（详情见底部日志）",
                collect_plain_text(translated),
                dump_log(),
                None,
                None,
                job_dir.name,
                file_prefix,
            )
            return

        translated.extend(part)
        elapsed = max(0.001, time.time() - start_ts)
        avg = elapsed / idx
        eta = max(0.0, (total - idx) * avg)
        pct = int(idx * 100 / max(total, 1))
        latest_progress = _strip_fractional_time(
            f"⏳ 翻译进度：{pct}%｜预计剩余 {_format_hms(eta)}"
        )

        yield (
            latest_progress,
            collect_plain_text(translated),
            dump_log(),
            None,
            None,
            job_dir.name,
            file_prefix,
        )

    zh_segments = normalize_segments_timeline(translated)
    zh_srt_path = save_srt(zh_segments, str(job_dir / f"{file_prefix}.zh.srt"), normalize=False)
    zh_txt_path = save_plain(zh_segments, str(job_dir / f"{file_prefix}.zh.txt"), normalize=False)
    push_log(f"[OUT] 中文 SRT: {Path(zh_srt_path).name}")
    push_log(f"[OUT] 中文 TXT: {Path(zh_txt_path).name}")

    srt_bundle = _build_all_bundle(job_dir, file_prefix)
    push_log(f"[OUT] 打包: {Path(srt_bundle).name}")

    yield (
        f"✅ 翻译完成：workspace/{job_dir.name}/（可下载打包文件）",
        segments_to_plain(zh_segments, normalize=False),
        dump_log(),
        srt_bundle,
        srt_bundle,
        job_dir.name,
        file_prefix,
    )


def prepare_download_bundle(history_video: str, current_job: str, current_prefix: str, kind: str = ""):
    """下载按钮点击后动态打包当前任务文件。"""
    job_dir = _resolve_current_job(current_job, history_video)
    if not job_dir:
        return None
    file_prefix = _resolve_file_prefix(job_dir, current_prefix)
    if not file_prefix:
        return None
    try:
        return _build_all_bundle(job_dir, file_prefix)
    except Exception:
        return None

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
        def _format_eta(seconds: float) -> str:
            seconds = max(0, int(seconds))
            m, s = divmod(seconds, 60)
            h, m = divmod(m, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"

        input_media = Path(video_path)
        target_audio = Path(audio_path)
        reuse_existing_wav = False
        try:
            reuse_existing_wav = (
                input_media.suffix.lower() == ".wav"
                and input_media.exists()
                and input_media.resolve() == target_audio.resolve()
            )
        except OSError:
            reuse_existing_wav = input_media.suffix.lower() == ".wav" and str(input_media) == str(target_audio)

        # 1. 提取音频到 job 目录；若输入本身就是目标 WAV，则直接复用。
        if reuse_existing_wav:
            if log_cb:
                log_cb("[STEP] 输入已是当前任务的 WAV 文件，直接复用，无需再次提取...")
            yield "⏳ 复用现有 WAV 文件...", []
        else:
            if log_cb:
                log_cb("[STEP] 正在使用 ffmpeg 提取 WAV 文件（16kHz/单声道）...")
            yield "⏳ 正在使用 ffmpeg 提取 WAV 文件...", []
            extract_audio(video_path, audio_path)
        staged_source_path = _stage_source_media_to_temp_video(video_path)
        if log_cb and Path(staged_source_path).suffix.lower() in _SOURCE_MEDIA_EXTS:
            log_cb(f"[MEDIA] 原始媒体已归档到 workspace/temp_video/{Path(staged_source_path).name}")
        _cleanup_job_source_media(job_dir)

        if log_cb:
            log_cb("[STEP] 正在使用 ffprobe 读取音频时长...")
        yield "⏳ 正在读取音频时长（ffprobe）...", []
        duration = get_audio_duration(audio_path)

        funasr_model = _pick_funasr_model_for_language(backend, lang_code, funasr_model, log_cb)

        logger.info(f"音频时长: {duration:.1f}s，后端: {backend}，语言: {lang_code}")
        if log_cb:
            log_cb(f"[ASR] 手动后端: {backend}")
            log_cb(f"[AUDIO] 时长: {duration:.1f}s")

        chunk_seconds = 120
        overlap_seconds = 10
        chunk_dir = job_dir / "chunks"
        if duration > chunk_seconds:
            if log_cb:
                log_cb(f"[STEP] 正在使用 ffmpeg 按 {chunk_seconds}s 分片音频（重叠 {overlap_seconds}s）...")
            yield f"⏳ 正在分片音频（每段 {chunk_seconds}s，重叠 {overlap_seconds}s）...", []
            chunk_items = split_audio_chunks(
                audio_path,
                str(chunk_dir),
                chunk_seconds=chunk_seconds,
                overlap_seconds=overlap_seconds,
            )
        else:
            if log_cb:
                log_cb("[CHUNK] 音频较短，无需分片，直接转写整段")
            chunk_items = [(audio_path, 0.0, duration)]

        if not chunk_items:
            return []

        total_chunks = len(chunk_items)
        all_segments: list[tuple[float, float, str]] = []
        if log_cb:
            log_cb(f"[CHUNK] 分片数: {total_chunks}，粒度: {chunk_seconds}s")
        yield f"⏳ 音频准备完成，共 {total_chunks} 个分片，开始识别...", []

        if device == "CUDA" and not _has_nvidia_gpu():
            if log_cb:
                log_cb("[DEVICE] 未检测到可用 NVIDIA GPU，强制回退 CPU")
            device = "CPU"

        if backend == "FunASR（Paraformer）":
            effective_model = funasr_model.split(" ")[0].strip()
            effective_device = "cuda:0" if device == "CUDA" else "cpu"
        else:
            effective_model = whisper_model
            effective_device = device.lower()

        if log_cb:
            log_cb(
                f"[ASR] 实际配置: backend={backend} model={effective_model} device={effective_device} language={lang_code}"
            )
        yield (
            f"⏳ 识别配置：后端={backend} | 模型={effective_model} | 设备={effective_device} | 语言={lang_code}",
            [],
        )

        if backend == "FunASR（Paraformer）":
            from backend.funasr_backend import transcribe
            device_str = "cuda:0" if device == "CUDA" else "cpu"
            if log_cb:
                log_cb(f"[MODEL] FunASR: {funasr_model}")
                log_cb("[STEP] 正在加载 FunASR 模型并预热...")
            yield "⏳ 正在加载 FunASR 模型...", []

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

                if total_chunks > 1 and idx > 1:
                    cutoff = start_s + overlap_seconds
                    deduped: list[tuple[float, float, str]] = []
                    for s, e, t in segs:
                        if e <= overlap_seconds:
                            continue
                        if s < overlap_seconds:
                            s = overlap_seconds
                        deduped.append((s, e, t))
                    segs = deduped

                if start_s > 0:
                    segs = [(s + start_s, e + start_s, t) for s, e, t in segs]
                    # 再次防护：避免重叠区重复字幕
                    if total_chunks > 1 and idx > 1:
                        segs = [(max(s, cutoff), e, t) for s, e, t in segs if e > cutoff]
                all_segments.extend(segs)

                done_s = min(max(0.0, end_s), max(duration, 1e-6))
                ratio = max(0.0, min(1.0, done_s / max(duration, 1e-6)))
                pct = int(ratio * 100)
                eta_s = (duration - done_s) if done_s > 0 else duration
                progress_text = _strip_fractional_time(
                    f"⏳ 转写进度：{pct}%｜预计剩余 {_format_eta(eta_s)}"
                )
                yield progress_text, all_segments.copy()

        else:  # faster-whisper
            from backend.whisper_backend import transcribe
            if log_cb:
                log_cb(f"[MODEL] Whisper: {whisper_model}")
                log_cb("[STEP] 正在加载 faster-whisper 模型并预热...")
            yield "⏳ 正在加载 faster-whisper 模型...", []

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

                if total_chunks > 1 and idx > 1:
                    cutoff = start_s + overlap_seconds
                    deduped: list[tuple[float, float, str]] = []
                    for s, e, t in segs:
                        if e <= overlap_seconds:
                            continue
                        if s < overlap_seconds:
                            s = overlap_seconds
                        deduped.append((s, e, t))
                    segs = deduped

                if start_s > 0:
                    segs = [(s + start_s, e + start_s, t) for s, e, t in segs]
                    if total_chunks > 1 and idx > 1:
                        segs = [(max(s, cutoff), e, t) for s, e, t in segs if e > cutoff]
                all_segments.extend(segs)

                done_s = min(max(0.0, end_s), max(duration, 1e-6))
                ratio = max(0.0, min(1.0, done_s / max(duration, 1e-6)))
                pct = int(ratio * 100)
                eta_s = (duration - done_s) if done_s > 0 else duration
                progress_text = _strip_fractional_time(
                    f"⏳ 转写进度：{pct}%｜预计剩余 {_format_eta(eta_s)}"
                )
                yield progress_text, all_segments.copy()

        if log_cb:
            log_cb("[STEP] 分片转写完成，准备汇总字幕片段...")
        yield "⏳ 正在汇总识别结果...", all_segments.copy()

        if chunk_dir.exists():
            shutil.rmtree(chunk_dir, ignore_errors=True)
            if log_cb:
                log_cb("[CLEANUP] 已清理临时分片目录")

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
    yield 顺序：(status_text, plain_text, history_markdown, history_dropdown, log_text, current_job, current_prefix, srt_bundle, txt_bundle)
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
            dump_log(),
            "",
            "",
            None,
            None,
        )
        return

    if not Path(video_path).exists():
        push_log(f"[ERROR] 文件不存在: {video_path}")
        yield (
            "❌ 处理失败（详情见底部日志）",
            "",
            None,
            None,
            dump_log(),
            "",
            "",
            None,
            None,
        )
        return

    try:
        push_log("[STEP] 初始化...")
        lang_code = _parse_lang_code(language)

        # 任务目录只保留 wav / srt / txt / zip，不保留原始视频。
        video_path = _stage_source_media_to_temp_video(video_path)
        job_dir = _resolve_job_dir_for_input(video_path)
        _cleanup_job_source_media(job_dir)
        orig_name = Path(video_path).name
        file_prefix = Path(orig_name).stem
        push_log(f"[INPUT] {orig_name}")
        push_log(f"[JOB] workspace/{job_dir.name}")
        logger.info(f"Job 目录: {job_dir}")

        yield (
            f"⏳ 处理中... 输出目录: workspace/{job_dir.name}",
            "",
            _workspace_history_markdown(),
            _history_dropdown_update(history_video),
            dump_log(),
            job_dir.name,
            file_prefix,
            None,
            None,
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
                _workspace_history_markdown(),
                _history_dropdown_update(history_video),
                dump_log(),
                job_dir.name,
                file_prefix,
                None,
                None,
            )

        push_log(f"[ASR] 原始片段数: {len(segments)}")

        if STOP_EVENT.is_set():
            push_log("[STOP] 用户已停止，未生成字幕文件")
            yield (
                "🛑 已停止（未生成字幕文件）",
                collect_plain_text(segments),
                _workspace_history_markdown(),
                _history_dropdown_update(history_video),
                dump_log(),
                job_dir.name,
                file_prefix,
                None,
                None,
            )
            return

        raw_plain_text = collect_plain_text(segments)
        push_log("[STEP] 正在归一化字幕时间轴...")
        cleaned_segments = normalize_segments_timeline(segments)
        push_log(f"[ASR] 清洗后片段数: {len(cleaned_segments)}")

        if not cleaned_segments:
            push_log("[WARN] 未识别到有效字幕片段")
            yield (
                "⚠️ 未识别到有效字幕（详情见底部日志）",
                "",
                _workspace_history_markdown(),
                _history_dropdown_update(history_video),
                dump_log(),
                job_dir.name,
                file_prefix,
                None,
                None,
            )
            return

        # 仅生成原文；翻译改为手动点击“翻译”按钮触发。
        plain_text = raw_plain_text or segments_to_plain(cleaned_segments, normalize=False)
        is_non_zh = lang_code in {"en", "ja", "ko", "es"} or _looks_non_chinese_text(plain_text)

        source_lang = _guess_source_lang(lang_code, plain_text) if is_non_zh else "zh"
        _save_task_meta(
            job_dir,
            {
                "file_prefix": file_prefix,
                "lang_code": lang_code,
                "source_lang": source_lang,
                "is_non_zh": is_non_zh,
            },
        )

        push_log("[STEP] 正在写出原文 SRT/TXT 文件...")
        orig_srt_path = save_srt(
            cleaned_segments,
            str(job_dir / f"{file_prefix}.srt"),
            normalize=False,
        )
        _, display_plain_text, written_text_files = _finalize_plain_text_outputs(
            job_dir,
            file_prefix,
            cleaned_segments,
            plain_text,
        )
        push_log(f"[OUT] 原文 SRT: {Path(orig_srt_path).name}")
        for written_name in written_text_files:
            push_log(f"[OUT] 文本文件: {written_name}")

        status = (
            f"✅ 原文识别完成 → workspace/{job_dir.name}/ "
            "（点击“翻译”按钮生成中文字幕与中文文本）"
        )
        push_log("[DONE] 任务完成")
        yield (
            status,
            display_plain_text,
            _workspace_history_markdown(),
            _history_dropdown_update(video_path if video_path.startswith("workspace/") else None),
            dump_log(),
            job_dir.name,
            file_prefix,
            None,
            None,
        )

    except Exception as e:
        logger.exception("转录失败")
        push_log(f"[ERROR] {e}")
        yield (
            "❌ 处理失败（详情见底部日志）",
            "",
            _workspace_history_markdown(),
            _history_dropdown_update(history_video),
            dump_log(),
            "",
            "",
            None,
            None,
        )


# ---------------------------------------------------------------------------
# Gradio UI 布局
# ---------------------------------------------------------------------------

def build_ui() -> gr.Blocks:
    with gr.Blocks(
        title="视频转字幕",
    ) as demo:
        profiles_init, active_profile_init = load_profiles()
        active_profile_obj = _find_profile(profiles_init, active_profile_init)
        profile_names_init = _profile_names(profiles_init)
        model_choices_init = (active_profile_obj or {}).get("models", []) if active_profile_obj else []
        model_value_init = (
            str((active_profile_obj or {}).get("default_model", "")).strip()
            or (model_choices_init[0] if model_choices_init else None)
        )

        gr.Markdown(
            """
            # 🎬 视频转字幕
            支持 **FunASR Paraformer**（阿里，中文准确率高）和 **faster-whisper**（多语言）双后端，NVIDIA GPU 加速。
            每次处理结果保存至 `workspace/<文件名>/` 目录。
            """
        )

        with gr.Row():
            page_home_btn = gr.Button("主页", variant="primary")
            page_file_btn = gr.Button("文件管理", variant="secondary")
            page_model_btn = gr.Button("配置模型", variant="secondary")

        with gr.Column(visible=True) as home_page:
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

                # ── 右侧：输出区 ──────────────────────────────────────────────
                with gr.Column(scale=2):
                    with gr.Row():
                        submit_btn = gr.Button("🚀 开始转录", variant="primary", size="lg")
                        translate_btn = gr.Button("🌐 翻译", variant="secondary")
                        stop_btn = gr.Button("⏹️ 停止转录", variant="secondary")
                    with gr.Row():
                        srt_download = gr.DownloadButton("下载SRT字幕", value=None)
                        txt_download = gr.DownloadButton("下载纯文本", value=None)

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
                    profiles_state = gr.State(value=profiles_init)
                    current_job_state = gr.State(value="")
                    current_prefix_state = gr.State(value="")

            with gr.Row():
                log_output = gr.Textbox(
                    label="运行日志（统一输出，可滚动查看）",
                    interactive=False,
                    lines=12,
                    max_lines=24,
                    value="",
                )

        with gr.Column(visible=False) as file_manage_page:
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

        with gr.Column(visible=False) as model_config_page:
            gr.Markdown("### 配置模型\n识别参数与在线模型配置统一在本页维护，配置会自动写入项目根目录 `.env`。")

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
                online_profile_select = gr.Dropdown(
                    label="在线模型配置组",
                    choices=profile_names_init,
                    value=active_profile_init if active_profile_init in profile_names_init else (profile_names_init[0] if profile_names_init else None),
                    allow_custom_value=False,
                )
                online_model_select = gr.Dropdown(
                    label="在线模型（来自配置组可用模型）",
                    choices=model_choices_init,
                    value=model_value_init,
                    allow_custom_value=True,
                )
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

            config_profile_select = gr.Dropdown(
                label="选择已有配置",
                choices=profile_names_init,
                value=active_profile_init if active_profile_init in profile_names_init else (profile_names_init[0] if profile_names_init else None),
                allow_custom_value=False,
            )
            config_profile_name = gr.Textbox(
                label="配置名称（新增或编辑）",
                value=str((active_profile_obj or {}).get("name", "")),
            )
            config_base_url = gr.Textbox(
                label="base_url",
                value=str((active_profile_obj or {}).get("base_url", "https://api.siliconflow.cn/v1")),
            )
            config_api_key = gr.Textbox(
                label="api_key",
                value=str((active_profile_obj or {}).get("api_key", "")),
                type="password",
            )
            config_model_select = gr.Dropdown(
                label="该配置默认模型",
                choices=model_choices_init,
                value=model_value_init,
                allow_custom_value=True,
            )
            with gr.Row():
                fetch_models_btn = gr.Button("获取可用模型列表", variant="secondary")
                save_profile_btn = gr.Button("保存配置", variant="primary")
                delete_profile_btn = gr.Button("删除配置", variant="stop")

            config_status = gr.Textbox(label="配置状态", value="", interactive=False, max_lines=3)

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
            outputs=[
                status_text,
                plain_output,
                history_md,
                history_video_select,
                log_output,
                current_job_state,
                current_prefix_state,
                srt_download,
                txt_download,
            ],
        )

        translate_btn.click(
            fn=translate_current_job,
            inputs=[
                history_video_select,
                current_job_state,
                current_prefix_state,
                log_output,
                online_profile_select,
                online_model_select,
            ],
            outputs=[
                status_text,
                plain_output,
                log_output,
                srt_download,
                txt_download,
                current_job_state,
                current_prefix_state,
            ],
        )

        srt_download.click(
            fn=lambda hv, cj, cp: prepare_download_bundle(hv, cj, cp, "srt"),
            inputs=[history_video_select, current_job_state, current_prefix_state],
            outputs=[srt_download],
        )

        txt_download.click(
            fn=lambda hv, cj, cp: prepare_download_bundle(hv, cj, cp, "txt"),
            inputs=[history_video_select, current_job_state, current_prefix_state],
            outputs=[txt_download],
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

        def _switch_subpage(page_name: str):
            return (
                gr.update(visible=page_name == "主页"),
                gr.update(visible=page_name == "文件管理"),
                gr.update(visible=page_name == "配置模型"),
                gr.update(variant="primary" if page_name == "主页" else "secondary"),
                gr.update(variant="primary" if page_name == "文件管理" else "secondary"),
                gr.update(variant="primary" if page_name == "配置模型" else "secondary"),
            )

        page_home_btn.click(
            fn=lambda: _switch_subpage("主页"),
            outputs=[home_page, file_manage_page, model_config_page, page_home_btn, page_file_btn, page_model_btn],
        )

        page_file_btn.click(
            fn=lambda: _switch_subpage("文件管理"),
            outputs=[home_page, file_manage_page, model_config_page, page_home_btn, page_file_btn, page_model_btn],
        )

        page_model_btn.click(
            fn=lambda: _switch_subpage("配置模型"),
            outputs=[home_page, file_manage_page, model_config_page, page_home_btn, page_file_btn, page_model_btn],
        )

        online_profile_select.change(
            fn=lambda name: gr.update(value=name),
            inputs=[online_profile_select],
            outputs=[config_profile_select],
        )

        online_profile_select.change(
            fn=_on_profile_selected,
            inputs=[online_profile_select, profiles_state],
            outputs=[
                config_profile_name,
                config_base_url,
                config_api_key,
                online_model_select,
                config_model_select,
            ],
        )

        config_profile_select.change(
            fn=lambda name: gr.update(value=name),
            inputs=[config_profile_select],
            outputs=[online_profile_select],
        )

        config_profile_select.change(
            fn=_on_profile_selected,
            inputs=[config_profile_select, profiles_state],
            outputs=[
                config_profile_name,
                config_base_url,
                config_api_key,
                online_model_select,
                config_model_select,
            ],
        )

        config_model_select.change(
            fn=lambda model_name: gr.update(value=model_name),
            inputs=[config_model_select],
            outputs=[online_model_select],
        )

        fetch_models_btn.click(
            fn=_fetch_models_and_persist,
            inputs=[config_profile_name, config_base_url, config_api_key, profiles_state],
            outputs=[
                config_status,
                profiles_state,
                config_model_select,
                online_model_select,
                config_profile_select,
                online_profile_select,
            ],
        )

        save_profile_btn.click(
            fn=_save_profile_config,
            inputs=[config_profile_name, config_base_url, config_api_key, config_model_select, profiles_state],
            outputs=[
                config_status,
                profiles_state,
                config_profile_select,
                online_profile_select,
                config_model_select,
                online_model_select,
            ],
        )

        delete_profile_btn.click(
            fn=_delete_profile_config,
            inputs=[config_profile_select, profiles_state, online_profile_select],
            outputs=[
                config_status,
                profiles_state,
                config_profile_select,
                online_profile_select,
                config_model_select,
                online_model_select,
                config_profile_name,
            ],
        )

    return demo


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="视频转字幕 WebUI")
    parser.add_argument("--port", type=int, default=7881, help="监听端口 (默认: 7881)")
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
