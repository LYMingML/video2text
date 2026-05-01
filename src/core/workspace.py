"""
工作目录管理、文件指纹、历史记录

从 main.py 提取，去 Gradio 依赖。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from core.config import (
    WORKSPACE_DIR,
    TEMP_VIDEO_DIR,
    TEMP_VIDEO_KEEP_COUNT,
    _ALL_MEDIA_EXTS,
    _AUDIO_EXTS,
    _SOURCE_MEDIA_EXTS,
    _safe_media_name,
    get_transcribing_video,
)

logger = logging.getLogger("video2text")

# ---------------------------------------------------------------------------
# 文件指纹数据库
# ---------------------------------------------------------------------------

_FINGERPRINT_DB_PATH = WORKSPACE_DIR / "fingerprints.db"
_fingerprint_db_lock = threading.Lock()


def _init_fingerprint_db():
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


def _cleanup_fingerprint_db() -> int:
    """清理数据库中不存在于缓存目录的文件记录，返回删除条数。"""
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
        fingerprint = {"path": str(file_path), "size": stat.st_size}
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
            time.time(),
        ))
        conn.commit()
        conn.close()


def _find_duplicate_file(src_path: Path, search_dir: Path) -> Path | None:
    """在指定目录中查找与源文件内容相同的文件。"""
    if not src_path.exists() or not search_dir.exists():
        return None

    src_fp = _get_file_fingerprint(src_path)
    if not src_fp or src_fp.get("size", 0) == 0:
        return None

    _cleanup_fingerprint_db()

    for f in search_dir.iterdir():
        if f.is_file() and f.suffix.lower() in _SOURCE_MEDIA_EXTS:
            if f.resolve() != src_path.resolve():
                fp = _get_file_fingerprint(f)
                if fp:
                    _save_fingerprint_to_db(fp)

    with _fingerprint_db_lock:
        conn = sqlite3.connect(_FINGERPRINT_DB_PATH)
        cursor = conn.execute("""
            SELECT file_path FROM file_fingerprints
            WHERE file_size = ? AND file_path != ?
        """, (src_fp["size"], str(src_path)))
        candidates = [row[0] for row in cursor.fetchall()]
        conn.close()

    for candidate_path in candidates:
        candidate = Path(candidate_path)
        if not candidate.exists():
            continue
        if candidate.resolve() == src_path.resolve():
            continue

        cand_fp = _get_file_fingerprint(candidate)
        if not cand_fp:
            continue

        if (cand_fp.get("head_50") == src_fp.get("head_50") and
                cand_fp.get("tail_50") == src_fp.get("tail_50")):
            return candidate

    return None


# ---------------------------------------------------------------------------
# 工作目录辅助
# ---------------------------------------------------------------------------

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
    """返回文件夹列表，包含名称、修改时间和大小。"""
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
        result.append({"name": d.name, "mtime": mtime, "size": size_mb})
    return result


def _make_job_dir(original_path: str) -> Path:
    """根据上传文件名创建 workspace/<slug>/ 子目录。"""
    stem = Path(original_path).stem
    slug = re.sub(r'[^\w\u4e00-\u9fff]+', '_', stem).strip('_')[:20]
    slug = slug or "upload"
    job_dir = WORKSPACE_DIR / slug
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


# ---------------------------------------------------------------------------
# 任务元数据
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# SRT 解析
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 输出文件管理
# ---------------------------------------------------------------------------

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
    """将 job_dir 下属于 prefix 的最终输出 .srt/.txt 文件打包为 zip。"""
    files = sorted(
        p for p in job_dir.iterdir()
        if p.is_file() and _is_final_output_file(p.name, file_prefix)
    )
    if not files:
        raise FileNotFoundError(f"未找到可打包的 srt/txt 文件（prefix={file_prefix}）")
    bundle_path = job_dir / f"{file_prefix}.zip"
    import zipfile
    with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return str(bundle_path)


# ---------------------------------------------------------------------------
# 文件管理
# ---------------------------------------------------------------------------

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
    """清理临时视频目录：跳过正在转录的文件，清理一小时前的旧文件。"""
    TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    transcribing_video = get_transcribing_video()
    files = [p for p in TEMP_VIDEO_DIR.iterdir() if p.is_file()]

    if transcribing_video:
        transcribing_path = Path(transcribing_video).resolve()
        files = [f for f in files if f.resolve() != transcribing_path]

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    if len(files) > max_items:
        one_hour_ago = time.time() - 3600
        for old in files[max_items:]:
            try:
                if old.stat().st_mtime < one_hour_ago:
                    old.unlink()
            except OSError:
                pass


def _stage_source_media_to_temp_video(input_path: str, preferred_name: str | None = None) -> str:
    """将源媒体文件归档到 temp_video 目录。"""
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

    duplicate = _find_duplicate_file(src, TEMP_VIDEO_DIR)
    if duplicate:
        try:
            duplicate.touch()
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
    """清理 job 目录中的非 wav 源媒体文件。"""
    for entry in job_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in _SOURCE_MEDIA_EXTS:
            continue
        try:
            entry.unlink()
        except OSError:
            pass


def _schedule_video_deletion(video_path: str, delay_seconds: int = 60):
    """在延迟指定秒数后删除视频文件（仅限 temp_video 目录中的文件）。"""
    video = Path(video_path) if video_path else None
    if not video or not video.exists():
        return

    try:
        if not _is_temp_video_dir(video.parent):
            logger.debug(f"视频不在 temp_video 目录，不自动删除: {video_path}")
            return
    except Exception:
        return

    def _delete_after_delay():
        try:
            time.sleep(delay_seconds)
            if video.exists() and _is_temp_video_dir(video.parent):
                try:
                    video.unlink()
                    logger.info(f"[CLEANUP] 已删除临时视频文件: {video.name}")
                except OSError as e:
                    logger.warning(f"[CLEANUP] 删除临时视频失败: {e}")
        except Exception as e:
            logger.warning(f"[CLEANUP] 延迟删除线程异常: {e}")

    t = threading.Thread(target=_delete_after_delay, daemon=True)
    t.start()
    logger.info(f"[CLEANUP] 已安排 {delay_seconds} 秒后删除临时视频: {video.name}")


def _resolve_job_dir_for_input(input_path: str) -> Path:
    """根据输入文件路径确定或创建 job 目录。"""
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


def _resolve_file_prefix(job_dir: Path, current_prefix: str | None) -> str | None:
    """确定当前 job 的文件前缀。"""
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


def _resolve_current_job(current_job: str | None, history_video: str | None) -> Path | None:
    """根据 job 名或历史视频路径定位 job 目录。"""
    if current_job:
        p = WORKSPACE_DIR / current_job
        if p.exists() and p.is_dir():
            return p

    if not history_video:
        return None

    p = Path(history_video)
    if not p.is_absolute():
        p = (WORKSPACE_DIR.parent / p)
    if p.exists() and p.is_file() and p.parent.exists():
        return p.parent
    return None


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


def _delete_job_folder(folder_name: str | None) -> str:
    """删除 workspace 下的指定 job 文件夹，返回状态消息。"""
    if not folder_name:
        return "⚠️ 请先选择要删除的文件夹"

    if folder_name == TEMP_VIDEO_DIR.name:
        return "❌ temp_video 为系统保留目录，拒绝删除"

    workspace_root = WORKSPACE_DIR.resolve()
    target = (WORKSPACE_DIR / folder_name).resolve()

    if workspace_root not in target.parents:
        return "❌ 非法目录，拒绝删除"

    if not target.exists() or not target.is_dir():
        return "⚠️ 文件夹不存在或已删除"

    shutil.rmtree(target, ignore_errors=False)
    return f"✅ 已删除 workspace/{folder_name}"


def _workspace_history_text(max_jobs: int = 30) -> str:
    """生成 workspace 历史文件夹大小概览（纯文本版本，去 Gradio 依赖）。"""
    job_dirs = _iter_workspace_job_dirs()
    if not job_dirs:
        return "### 历史上传\n暂无历史记录。"

    lines = ["### 历史上上传", ""]
    for job_dir in job_dirs[:max_jobs]:
        size_mb = _dir_size_bytes(job_dir) / (1024 * 1024)
        lines.append(f"- **{job_dir.name}/** ({size_mb:.2f} MB)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 输入路径解析
# ---------------------------------------------------------------------------

def _resolve_input_path(video_file, history_video: str | None) -> str | None:
    """优先使用新上传文件；若未上传则使用历史文件选择。"""
    if video_file is not None:
        return video_file if isinstance(video_file, str) else video_file.name

    if not history_video:
        return None

    p = Path(history_video)
    resolved = p if p.is_absolute() else (WORKSPACE_DIR.parent / p)
    return str(resolved)


# ---------------------------------------------------------------------------
# 纯文本输出
# ---------------------------------------------------------------------------

def _finalize_plain_text_outputs(
    job_dir: Path,
    file_prefix: str,
    cleaned_segments: list[tuple[float, float, str]],
    plain_text: str,
) -> tuple[str, str, list[str]]:
    from utils.subtitle import save_plain, segments_to_plain

    raw_text = plain_text or segments_to_plain(cleaned_segments, normalize=False)

    raw_txt_path = save_plain(
        cleaned_segments,
        str(job_dir / f"{file_prefix}.txt"),
        normalize=False,
    )
    return raw_text, raw_text, [Path(raw_txt_path).name]
