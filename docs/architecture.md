# video2text 项目架构文档

> **版本**: v0.4.0 | **语言**: Python 3.10+ | **许可**: MIT

---

## 目录

1. [项目概览](#1-项目概览)
2. [目录结构](#2-目录结构)
3. [数据流与核心流程](#3-数据流与核心流程)
4. [main.py — 核心编排层](#4-mainpy--核心编排层)
5. [fastapi_app.py — FastAPI 前端与 API 层](#5-fastapi_apppy--fastapi-前端与-api-层)
6. [backend/ — 双 ASR 后端](#6-backend--双-asr-后端)
7. [utils/ — 工具模块](#7-utils--工具模块)
8. [脚本与配置](#8-脚本与配置)
9. [前端架构](#9-前端架构)
10. [配置系统](#10-配置系统)
11. [架构模式与设计决策](#11-架构模式与设计决策)
12. [关键算法](#12-关键算法)
13. [安全考量](#13-安全考量)

---

## 1. 项目概览

video2text 是一个视频/音频转字幕工具，核心功能：

- **语音识别（ASR）**：双后端（FunASR Paraformer / faster-whisper），支持 GPU 加速
- **视频下载**：yt-dlp（通用）+ XHS-Downloader（小红书无水印），自动使用浏览器 Cookie
- **字幕翻译**：OpenAI 兼容 API，支持并行批量翻译
- **文件管理**：内容指纹去重、自动清理、ZIP 打包

提供两套前端：
- **FastAPI**（`fastapi_app.py`）：内嵌 HTML/JS 单页应用，推荐使用
- **Gradio**（`main.py`）：备选 Web UI

---

## 2. 目录结构

```
video2text/
├── fastapi_app.py              # FastAPI 应用 + 内嵌 HTML/JS（主前端）
├── main.py                     # Gradio UI（备选前端）+ 核心编排逻辑
├── main.sh                     # 启动脚本（HTTP/HTTPS 双端口）
├── install.sh                  # 安装脚本（系统依赖 + Python 环境）
├── .env                        # 运行时配置（API 密钥、模型选择等）
├── .env.example                # 配置模板
├── pyproject.toml              # 项目元数据与依赖声明
├── backend/
│   ├── __init__.py
│   ├── funasr_backend.py       # FunASR Paraformer 语音识别后端
│   └── whisper_backend.py      # faster-whisper 语音识别后端
├── utils/
│   ├── __init__.py
│   ├── audio.py                # FFmpeg 音频提取与分片
│   ├── core.py                 # (被 main.py 导入的核心函数)
│   ├── subtitle.py             # SRT/VTT/TXT 字幕读写与处理
│   ├── translate.py            # AI 字幕并行翻译
│   ├── online_models.py        # 翻译模型配置组管理
│   └── xhs_downloader.py       # 小红书无水印视频下载
├── scripts/
│   └── download_models.py      # FunASR 模型预下载脚本
├── docs/                       # 文档目录
├── workspace/                  # 任务输出目录
│   ├── temp_video/             # 临时视频缓存
│   └── fingerprints.db         # 文件指纹去重 SQLite 数据库
│   └── <task_dir>/             # 每个任务一个子目录
│       ├── <prefix>.wav        # 提取的音频
│       ├── <prefix>.srt        # 原始字幕
│       ├── <prefix>.txt        # 原始纯文本
│       ├── <prefix>.zh.srt     # 中文翻译字幕
│       ├── <prefix>.zh.txt     # 中文翻译纯文本
│       └── <prefix>.zip        # 打包下载
```

---

## 3. 数据流与核心流程

### 3.1 转录流程

```
用户输入（上传/URL/历史文件）
    │
    ▼
┌─────────────────────┐
│  输入解析与去重       │  main.py: _resolve_input_path()
│  内容指纹检测         │  main.py: _find_duplicate_file()
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  创建任务目录         │  main.py: _resolve_job_dir_for_input()
│  workspace/<name>/   │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  FFmpeg 提取音频      │  utils/audio.py: extract_audio()
│  → 16kHz 单声道 WAV  │  硬件加速优先：Intel QSV > NVIDIA CUDA > CPU
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  音频分片（>120s）    │  utils/audio.py: split_audio_chunks()
│  120s 片段 + 10s 重叠 │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  ASR 语音识别         │  backend/funasr_backend.py 或 whisper_backend.py
│  模型缓存 + GPU 加速  │  transcribe() → list[(start, end, text)]
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  后处理              │  utils/subtitle.py
│  去噪 → 时间线规范化  │  normalize_segments_timeline()
│  标点去重 → 文本换行  │  wrap_text()
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  输出保存             │
│  .srt / .txt / .zip  │  main.py: _finalize_plain_text_outputs()
└─────────────────────┘
```

### 3.2 翻译流程

```
原始 SRT 字幕文件
    │
    ▼
┌─────────────────────┐
│  解析 SRT            │  main.py: _parse_srt_segments()
│  → list[(start,end,text)] │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  加载翻译配置         │  utils/online_models.py: load_profiles()
│  base_url + api_key  │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  并行翻译            │  utils/translate.py: translate_segments()
│  ThreadPoolExecutor  │  可配置线程数（1-50，默认 5）
│  逐行翻译 + 缓存     │  _translate_line_with_siliconflow()
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  保存翻译结果         │
│  .<lang>.srt / .<lang>.txt │
│  重新打包 .zip       │
└─────────────────────┘
```

### 3.3 URL 下载流程

```
用户输入 URL
    │
    ▼
┌─────────────────────┐
│  平台检测             │  utils/xhs_downloader.py: is_xiaohongshu_url()
│  小红书？→ XHS下载    │  download_xhs_video()
│  其他 → yt-dlp       │
└─────────┬───────────┘
          │
          ▼ (yt-dlp 路径)
┌─────────────────────┐
│  Cookie 检测          │  fastapi_app.py: _detect_wsl_firefox_profile()
│  1. cookies.txt 上传  │
│  2. WSL2 Firefox     │  --cookies-from-browser "firefox:/path"
│  3. Chrome/Edge      │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  代理配置             │  --proxy http://127.0.0.1:7897
│  DOWNLOAD_PROXY      │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  下载 + 自动字幕检测  │  --write-auto-subs --sub-langs zh,en
│  平台字幕可用？→ 导入 │  _pick_downloaded_subtitle()
│  无字幕？→ ASR 转录   │
└─────────────────────┘
```

---

## 4. main.py — 核心编排层

`main.py` 既是 Gradio 前端，也是核心业务逻辑的载体。`fastapi_app.py` 通过 `import main as core` 引用其中的函数。

### 4.1 全局常量与状态

| 变量 | 类型 | 说明 |
|------|------|------|
| `WORKSPACE_DIR` | `Path` | 输出根目录 `workspace/` |
| `TEMP_VIDEO_DIR` | `Path` | 临时视频 `workspace/temp_video/` |
| `TEMP_VIDEO_KEEP_COUNT` | `int` | 临时文件保留数量（默认 5） |
| `STOP_EVENT` | `threading.Event` | 全局停止信号 |
| `SUPPORTED_EXTS` | `list[str]` | 支持的媒体扩展名 |
| `_TRANSCRIBING_VIDEO` | `str \| None` | 当前正在转录的视频路径（线程安全） |
| `_FINGERPRINT_DB_PATH` | `Path` | SQLite 指纹数据库路径 |

### 4.2 文件指纹去重

基于 SQLite 的内容指纹系统，避免重复上传相同文件。

**`_init_fingerprint_db()`** — 初始化数据库表 `file_fingerprints`，字段：`id`, `file_path`(UNIQUE), `file_size`, `head_50`(前50字节), `tail_50`(后50字节), `updated_at`

**`_find_duplicate_file(src_path, search_dir) -> Path | None`** — 在 `search_dir` 中查找与 `src_path` 内容相同的文件。比较策略：文件大小 → 前50字节 → 后50字节。

### 4.3 文件管理函数

| 函数 | 说明 |
|------|------|
| `_list_job_folders_meta()` | 返回 workspace 子目录列表，含名称、修改时间、大小(MB) |
| `_make_job_dir(original_path)` | 从文件名创建任务目录，sanitize 名称（保留中文/字母数字，限20字符） |
| `_unique_file_path(dir, filename)` | 避免文件名冲突，追加 `_2`, `_3` 等 |
| `_prune_temp_video_dir(max_items)` | 清理旧临时文件（超过数量限制时删除最旧的） |
| `_stage_source_media_to_temp_video(path, name)` | 将输入文件暂存到临时目录，自动去重 |
| `_cleanup_job_source_media(job_dir)` | 删除任务目录中的源媒体文件，只保留 wav/srt/txt/zip |
| `_list_uploaded_videos()` | 列出可用的视频/音频文件（优先显示 WAV） |
| `_resolve_input_path(video_file, history_video)` | 解析用户选择的输入文件路径 |

### 4.4 语言检测与模型选择

**`_parse_lang_code(choice) -> str`** — 从 UI 选项解析语言代码，如 `"zh（普通话）"` → `"zh"`

**`_looks_non_chinese_text(text) -> bool`** — 启发式判断文本是否非中文（中文字符占比 < 25%）

**`_guess_source_lang(lang_code, plain_text) -> str`** — 猜测源语言，基于正则匹配日/韩/西语特征

**`_pick_funasr_model_for_language(backend, lang_code, model, log_cb) -> str`** — 自动选择最佳 FunASR 模型：
- 非中文/粤语 → 切换到 SenseVoiceSmall
- 说话人分离模型 → 保持用户选择
- `auto` → 使用多语言模型

### 4.5 GPU 检测

**`_has_nvidia_gpu() -> bool`** — NVIDIA GPU 检测链：
1. 检查 `CUDA_VISIBLE_DEVICES`（跳过 `-1`/`none`）
2. `torch.cuda.is_available()`
3. `/dev/nvidiactl` 或 `/proc/driver/nvidia` 存在
4. 回退：`nvidia-smi -L`

### 4.6 核心转录编排

**`_do_transcribe_stream(video_path, backend, language, ...) -> Iterator`** — 核心转录生成器，逐步 yield `(status_text, segments)`：

1. 检查输入是否已是目标 WAV → 直接复用
2. FFmpeg 提取 16kHz 单声道 PCM 音频
3. 暂存源文件到 temp_video
4. ffprobe 获取音频时长
5. 按语言自动选择 FunASR 模型
6. 音频 >120s 时分片（120s 片段 + 10s 重叠）
7. 检测 GPU，必要时回退 CPU
8. 加载 ASR 模型，逐片段识别
9. 去重叠、调整时间戳
10. 修复 >5 分钟的时间跳变
11. 清理分片目录

### 4.7 字幕输出

**`_finalize_plain_text_outputs(job_dir, prefix, segments, text) -> (raw, display, files)`** — 生成最终的 txt 输出文件：
- 如果有说话人标签 → 使用 `format_speaker_script()` 生成剧本格式
- 否则 → 标准纯文本

**`_save_task_meta(job_dir, meta_dict)`** — 保存任务元数据到 `task_meta.json`，包含 `file_prefix`, `lang_code`, `source_lang`, `is_non_zh`

### 4.8 Gradio UI

**`build_ui() -> gr.Blocks`** — 构建多页面 Gradio 界面：
- 主页：视频上传、转录、翻译
- 文件管理：文件夹列表、删除、下载
- 模型配置：配置组管理、模型选择

**`main()`** — 入口函数，解析 `--port`, `--host`, `--share`, `--ssl-certfile`, `--ssl-keyfile`

---

## 5. fastapi_app.py — FastAPI 前端与 API 层

### 5.1 任务状态管理

**`JobState`** (dataclass) — 内存中的任务状态：

| 字段 | 类型 | 说明 |
|------|------|------|
| `job_id` | `str` | 唯一标识（uuid4 hex） |
| `status` | `str` | 显示状态文本 |
| `plain_text` | `str` | 当前识别/翻译文本 |
| `logs` | `list[str]` | 运行日志（最多 500 条） |
| `current_job` | `str` | 任务目录名 |
| `current_prefix` | `str` | 文件前缀 |
| `zip_bundle` | `str \| None` | ZIP 打包路径 |
| `done / failed / running` | `bool` | 状态标志 |
| `progress_pct` | `int` | 进度百分比 |
| `eta_seconds` | `int` | 预计剩余秒数 |
| `step_label` | `str` | 当前步骤标签 |
| `video_path` | `str` | 输入视频路径 |
| `translate_params` | `dict` | 转录参数 |
| `display_name` | `str` | 显示名称 |
| `auto_translate / auto_download` | `bool` | 自动翻译/下载标志 |

**全局状态**：
- `_RUNTIME_JOB: JobState | None` — 当前运行的任务
- `_RUNTIME_THREAD: Thread | None` — 当前工作线程
- `_TRANSCRIBE_QUEUE: list[str]` — 等待队列
- `_ALL_JOBS: dict[str, JobState]` — 所有任务索引

### 5.2 任务队列系统

**`_schedule_next_transcribe()`** — 任务完成后调度下一个：
1. 检查当前是否有运行中任务
2. 从 `_TRANSCRIBE_QUEUE` 弹出下一个
3. 启动工作线程

**`_set_job_progress(job, status, start_ts, ...)`** — 设置进度信息：
- 自动计算 ETA（基于进度百分比和已用时间）
- 装饰状态文本（追加百分比和剩余时间）

**`_estimate_pct_from_status(status) -> int`** — 从状态文本估算进度：
- 含 `N%` → 直接提取
- 关键词映射：`提取 WAV` → 5%, `加载模型` → 20%, `汇总结果` → 92%

### 5.3 工作线程

**`_run_transcribe_worker(job, video_path, backend, ...)`** — 转录工作线程：
1. 清理旧输出文件
2. 创建任务目录
3. 调用 `core._do_transcribe_stream()` 流式转录
4. 保存 SRT/TXT 输出
5. 猜测源语言，保存任务元数据
6. 打包 ZIP
7. 调度下一个排队任务

**`_run_translate_worker(job, profile, model, target_lang)`** — 翻译工作线程：
1. 加载翻译配置
2. 解析原始 SRT
3. 调用 `translate_segments()` 并行翻译
4. 保存翻译后的 SRT/TXT
5. 重新打包 ZIP

**`_run_subtitle_import_worker(job, media_path, subtitle_path)`** — 平台字幕导入：
1. 复制或转换字幕文件（SRT/VTT → SRT）
2. 生成纯文本
3. 猜测源语言
4. 保存元数据和打包

### 5.4 API 端点一览

#### 核心任务 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 返回 HTML/JS 单页应用 |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/transcribe/start` | 启动转录任务（FormData） |
| `POST` | `/api/jobs/{id}/stop` | 停止任务 |
| `GET` | `/api/jobs/{id}` | 查询任务状态 |
| `POST` | `/api/jobs/{id}/translate` | 启动翻译 |
| `GET` | `/api/jobs/{id}/files` | 列出任务输出文件 |
| `GET` | `/api/jobs/{id}/download/{kind}` | 下载 ZIP 包 |
| `GET` | `/api/jobs/{id}/download-file?file_name=` | 下载单个文件 |
| `POST` | `/api/jobs/import-subtitle` | 导入平台自动字幕 |

#### 队列 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/queue/status` | 获取队列状态（运行中/排队/历史） |

#### 历史/文件管理 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/history` | 获取上传视频列表和文件夹列表 |
| `POST` | `/api/folders/delete` | 删除单个文件夹 |
| `POST` | `/api/folders/delete-batch` | 批量删除文件夹 |
| `GET` | `/api/folders/output-files?folder_name=` | 单文件夹文件列表 |
| `GET` | `/api/folders/all-output-files` | 全部文件夹文件列表 |
| `POST` | `/api/folders/translate` | 翻译已有文件夹中的字幕 |
| `POST` | `/api/folders/download-multi` | 跨文件夹多文件下载 |
| `POST` | `/api/folders/download-output` | 单文件夹内多文件下载 |
| `GET` | `/api/folders/zip-files?folder_name=` | 列出 ZIP 文件 |
| `GET` | `/api/folders/download-text?folder_name=` | 下载所有 TXT 文件 |

#### 模型配置 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/model/profiles` | 列出配置组 + 应用设置 |
| `GET` | `/api/model/profile?name=` | 获取单个配置组详情 |
| `POST` | `/api/model/profiles/fetch-models` | 测试连通性并获取模型列表 |
| `POST` | `/api/model/profiles/save` | 保存配置组 |
| `POST` | `/api/model/profiles/delete` | 删除配置组 |
| `POST` | `/api/app-settings/subtitle-priority` | 保存字幕语言优先级 |

#### 下载 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/download_url` | 通过 yt-dlp/XHS 下载视频 |
| `GET` | `/api/download_file?path=` | 下载临时目录中的文件 |
| `POST` | `/api/upload_cookie` | 上传 Cookie 文件 |

#### 外部 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/external/process` | 第三方统一入口（同步，支持 base64/url/history 输入） |

#### 设置 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/settings/temp-files` | 获取临时文件设置 |
| `POST` | `/api/settings/temp-files` | 更新临时文件设置 |

### 5.5 URL 下载实现

**`_download_with_ytdlp(url, payload)`** — yt-dlp 下载核心逻辑：

1. **Cookie 检测链**：
   - 上传的 `cookies.txt` 优先
   - WSL2 下自动检测 Windows Firefox profile（`_detect_wsl_firefox_profile()`）
   - 浏览器 Cookie 回退链：`firefox → chrome → chromium → edge → 无Cookie`

2. **代理支持**：`--proxy` 参数从 `DOWNLOAD_PROXY` 配置读取

3. **自动字幕**：`--write-auto-subs --sub-langs <lang> --convert-subs srt`

4. **浏览器尝试顺序**：
   - WSL2 环境：`firefox → chrome → chromium → edge → 无`
   - 非 WSL2：`chrome → chromium → firefox → edge → 无`

5. **登录错误检测**：关键词匹配 `login`, `sign in`, `private`, `401`, `403` 等

### 5.6 字幕优先级预设

```python
SUBTITLE_PRIORITY_PRESETS = {
    "zh": "zh-Hans,zh-CN,zh,zh.*,yue,zh-HK,en,en.*",
    "en": "en,en.*,en-US,en-GB",
    "ja": "ja,ja.*",
    "ko": "ko,ko.*",
    # ... es, fr, de, ru, pt, ar, hi
    "none": "",  # 禁用自动字幕
}
```

---

## 6. backend/ — 双 ASR 后端

两个后端实现相同的接口签名：

```python
def transcribe(
    audio_path: str,
    model_name: str,
    language: str,
    device: str,
    progress_cb: Callable[[float, str], None] = None
) -> list[tuple[float, float, str]]  # [(start_s, end_s, text), ...]
```

### 6.1 funasr_backend.py

**模型缓存**：`_model_cache: dict[tuple, AutoModel]`，键为 `(model_name, device, speaker_mode)`

**设备检测**：`_detect_best_device() -> "xpu" | "cuda:0" | "cpu"`
- 优先级：Intel XPU > NVIDIA CUDA > CPU

**模型加载**：`_get_model(name, device, speaker_mode) -> AutoModel`
- 模型配置：`vad_model="fsmn-vad"`, `punc_model="ct-punc"`, `hub="ms"`
- 说话人分离：`spk_model="cam++"`
- 批处理：`max_single_segment_time=30000`（30秒）

**后处理管线**：
1. 去除情感/事件标签：`<|HAPPY|><|Speech|>` → 正则清除
2. 去除 emoji 字符
3. 去除重复标点
4. 文本分片（`_split_by_punctuation()`）：
   - 句末标点（。！？!?) → 立即分割
   - 停顿标点（，,；;）→ 超过 max_chars 时分割
   - 普通字符 → 强制 max_chars 处分割
5. 时间跳变修复（`_fix_time_gaps()`）：>300秒的跳变用字符数重新估算
6. 说话人回退（`_label_speaker_fallback()`）：基于 1.2 秒静默间隔切换角色

**支持的模型**：
| 模型 | 说明 |
|------|------|
| `paraformer-zh` | 普通话精度推荐 |
| `paraformer` | 普通话全量 |
| `paraformer-zh-streaming` | 流式低延迟 |
| `paraformer-zh-spk` | 说话人分离 |
| `paraformer-en` | 英文优化 |
| `iic/SenseVoiceSmall` | 中粤英日韩多语 |
| `EfficientParaformer-large-zh` | 长语音友好 |
| `iic/speech_seaco_paraformer_large_asr_nat-*` | 中文高精度 |

### 6.2 whisper_backend.py

**模型缓存**：`_model_cache: dict`，键为 `(model_name, device, compute_type)`

**模型加载**：`_get_model(name, device, compute_type) -> WhisperModel`
- compute_type 回退链：`float16 → int8_float16 → int8 → cpu int8`
- Tesla P4 (sm_61) 推荐 `int8`

**转录参数**：
- `beam_size=5`, `best_of=5`
- `vad_filter=True`，`min_silence_duration_ms=500`
- `condition_on_previous_text=True`
- 中文初始提示：`"以下是普通话的句子。"`

**幻觉检测**：`_clean_hallucinations(text)` 清除模式：
- `[cite: N]`, `[citation: N]`, `(cite: N)`
- `subtitle by ...`
- 独立连字符、URL、`[N]`

**支持的模型**：`tiny`, `base`, `small`, `medium`, `large-v3`

---

## 7. utils/ — 工具模块

### 7.1 audio.py — 音频处理

**`extract_audio(input_path, output_path, threads) -> str`**
- FFmpeg 参数：`-vn -ac 1 -ar 16000 -acodec pcm_s16le`
- 硬件加速：`-hwaccel qsv` > `-hwaccel cuda` > 无
- 线程数：`FFMPEG_THREADS` 环境变量（默认 4）

**`get_audio_duration(audio_path) -> float`**
- 使用 ffprobe 获取音频时长

**`split_audio_chunks(audio_path, output_dir, chunk_seconds, overlap_seconds) -> list[tuple]`**
- 分片 WAV 音频，返回 `[(chunk_path, start_s, end_s), ...]`

### 7.2 subtitle.py — 字幕 I/O

**噪声检测**：`_is_noise_text()` 使用多正则匹配（进度条、模型加载信息等），2+ 匹配判定为噪声

**文本换行**：
- `_wrap_chinese_text(text, max_chars=25)` — 按字符数换行
- `_wrap_english_text(text, max_words=20)` — 按单词数换行

**时间线规范化**：`normalize_segments_timeline(segments, min_s, max_s, continuous)`
1. 过滤噪声（空文本、噪声文本）
2. 按开始时间排序
3. 确保 end > start，否则设为 start + min_duration
4. 裁剪时长到 `[min_s, max_s]` 范围
5. `continuous=True` 时：调整每段 end 为下一段 start（无缝衔接）

**SRT 格式化**：`segments_to_srt()` 标准序号 + 时间轴 + 文本

**说话人剧本格式**：`format_speaker_script()` — 按 `角色N` 分组，显示 `【角色N】（HH:MM:SS）`

### 7.3 translate.py — 翻译

**并行翻译核心**：`_translate_segments_with_siliconflow()`
- `ThreadPoolExecutor(max_workers=N)` 并行处理
- 逐行翻译：构建 prompt → 调用 API → 缓存结果
- 进度追踪：实时 ETA 计算 `(total - done) × avg_time_per_segment`

**翻译 Prompt 结构**：
```
角色：专业字幕翻译助手
要求：仅输出翻译，保持语气，不要展开
目标语言：{target_name}
源语言：{source_name}
原文：{text}
```

**Ollama 兼容**：检测 `:11434` 或 `ollama`，使用 `/api/chat` 端点替代 OpenAI `/chat/completions`

**模型别名**：`_resolve_model_name()` 处理简写（如 `Kimi-K2.5` → `Pro/moonshotai/Kimi-K2.5`）

**二级缓存**：
- `_LINE_CACHE` — 逐行翻译结果缓存（避免重复翻译相同句子）
- `_MODEL_CACHE` — 模型列表缓存

### 7.4 online_models.py — 配置管理

**`.env` 读写**：`_read_env_map()` / `_write_env_map()` 直接操作 `.env` 文件

**配置组管理**：每个配置组存储为 `ONLINE_MODEL_PROFILE_{N}_{FIELD}` 格式：
- `NAME` — 配置名称
- `BASE_URL` — API 地址
- `API_KEY` — 密钥
- `DEFAULT_MODEL` — 默认模型
- `MODEL_LIST_JSON` — 可用模型列表（JSON 数组）

**`upsert_profile(profiles, profile)`** — 插入或更新配置组，确保名称唯一

### 7.5 xhs_downloader.py — 小红书下载

**`XHSDownloadResult`** (dataclass)：下载结果，含 `success`, `file_path`, `note_id`, `note_title`, `author_name`

**`XHSDownloaderClient`**：XHS-Downloader API 客户端
- `check_server()` — 检测 `/docs` 端点是否可用
- `download_video(url)` — 验证笔记类型 → 调用下载 API → 返回结果

**URL 匹配模式**：
- `xiaohongshu.com/explore/*`
- `xiaohongshu.com/discovery/item/*`
- `xhslink.com/*`

---

## 8. 脚本与配置

### 8.1 install.sh — 安装脚本

**安装流程**：
1. OS 检测（仅支持 Linux）
2. 系统依赖：Python 3.12（deadsnakes PPA）、ffmpeg、git、build-essential
3. Python 环境：`uv` 创建 `.venv`，安装项目依赖
4. PyTorch GPU 检测：
   - Pascal/Maxwell/Kepler (sm≤61) → `torch==2.3.1+cu121`
   - Volta+ (sm≥70) → 最新 torch + CUDA 12.4
   - 无 GPU → CPU 版本
5. yt-dlp 安装到 `~/.local/bin/`
6. 可选：mkcert HTTPS 证书
7. 可选：systemd 服务注册

### 8.2 main.sh — 启动脚本

- 杀死已有 fastapi_app.py 进程
- SSL 证书存在时：双端口启动（HTTP 7880 + HTTPS 7881）
- 否则：单 HTTP 端口 7881
- 信号处理：SIGTERM/SIGINT 清理子进程

### 8.3 scripts/download_models.py

预下载 FunASR `paraformer-zh` 核心模型（~1GB），用于 Docker 镜像构建。

---

## 9. 前端架构

### 9.1 FastAPI 内嵌前端

`fastapi_app.py` 的 `index()` 端点返回一个完整的 HTML 页面（~3400 行），包含：

**页面结构**（单页应用，4 个页面）：
- **主页（home）**：任务输入、转录/翻译操作、输出结果面板、任务进度
- **文件管理（file）**：历史文件夹表格、输出文件表格、批量下载
- **配置模型（model）**：配置组列表、API 凭证编辑、模型测试与保存
- **任务队列（queue）**：运行中任务、排队列表、历史记录

**JavaScript 关键组件**：

| 变量/函数 | 说明 |
|-----------|------|
| `currentJobId` | 当前跟踪的任务 ID |
| `startPoll()` | 每秒轮询 `/api/jobs/{id}` 更新 UI |
| `_prevJobState` | 上一帧状态，用于事件驱动刷新（避免全量刷新） |
| `pendingAutoFlags` | 自动翻译/下载标志 |
| `refreshHistory()` | 刷新视频列表和文件夹列表 |
| `refreshQueueStatus()` | 刷新队列页面 |
| `init()` | 页面加载时恢复任务状态（从 `/api/queue/status`） |

**UI 交互**：
- 文件夹列表：支持 Shift/Ctrl 多选、排序（名称/时间/大小）、搜索过滤
- 文件列表：同样支持多选、排序、过滤
- 任务进度条：实时更新百分比和 ETA
- 事件驱动：仅在 `current_job`、`step_label`、`done` 变化时触发相关刷新

---

## 10. 配置系统

### 10.1 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `APP_PORT` | `7881` | 服务端口 |
| `DEFAULT_BACKEND` | `FunASR（Paraformer）` | ASR 后端 |
| `DEFAULT_FUNASR_MODEL` | `paraformer-zh` | FunASR 模型 |
| `DEFAULT_WHISPER_MODEL` | `medium` | Whisper 模型 |
| `AUTO_SUBTITLE_LANG` | `zh` | 字幕优先语言 |
| `DOWNLOAD_PROXY` | (空) | yt-dlp HTTP 代理 |
| `FFMPEG_THREADS` | `4` | FFmpeg 线程数 |
| `FUNASR_BATCH_SIZE_S` | `300` | FunASR 批处理大小（秒） |
| `PREFER_INTEL_GPU` | `0` | 优先使用 Intel GPU |
| `TEMP_VIDEO_KEEP_COUNT` | `5` | 临时视频保留数量 |
| `TRANSLATE_PARALLEL_THREADS` | `3` | 翻译并行线程数 |
| `BROWSER_DEBUG_PORT` | `9222` | Chrome 远程调试端口 |

### 10.2 配置组格式

```
ONLINE_MODEL_PROFILE_COUNT=1
ONLINE_MODEL_PROFILE_1_NAME=default
ONLINE_MODEL_PROFILE_1_BASE_URL=https://api.siliconflow.cn/v1
ONLINE_MODEL_PROFILE_1_API_KEY=sk-xxx
ONLINE_MODEL_PROFILE_1_DEFAULT_MODEL=tencent/Hunyuan-MT-7B
ONLINE_MODEL_PROFILE_1_MODEL_LIST_JSON=["model1","model2",...]
ONLINE_MODEL_ACTIVE_PROFILE=default
```

---

## 11. 架构模式与设计决策

### 11.1 双前端共享核心

`fastapi_app.py` 通过 `import main as core` 引用 `main.py` 中的函数。这意味着：
- 核心逻辑只在 `main.py` 中维护
- FastAPI 前端是轻量封装
- 两套前端不能同时运行（共享全局状态）

### 11.2 模型缓存

两个 ASR 后端都使用字典缓存已加载模型实例，键包含模型名+设备+精度。避免重复加载，首次加载后后续调用直接复用。

### 11.3 任务队列

FastAPI 层实现简单的内存任务队列：
- 单线程处理：同一时间只有一个 ASR 任务运行
- 多任务排队：`_TRANSCRIBE_QUEUE` FIFO 队列
- `_ALL_JOBS` 字典索引所有任务
- 最大保留 200 条历史，超限时清理最旧的已完成任务

### 11.4 内容指纹去重

使用 SQLite 数据库存储文件指纹（大小 + 前50字节 + 后50字节），避免重复上传相同文件。三元组比较策略平衡了准确性和性能。

### 11.5 硬件加速自适应

三层回退：Intel XPU → NVIDIA CUDA → CPU。GPU 检测失败时自动降级，不中断用户操作。

### 11.6 翻译并行化

`ThreadPoolExecutor` 并行翻译字幕行，每行独立请求 API。进度追踪基于已完成行的平均耗时估算 ETA。

---

## 12. 关键算法

### 12.1 音频分片与去重叠

```
音频（>120s）→ 120s 分片（10s 重叠）
↓
逐片段识别 → 得到 segments
↓
去重叠：比较相邻片段边界区域的文本相似度
↓
时间戳调整：非首片段加上全局偏移量
↓
时间跳变修复：>300s 的跳变用字符数 × 0.15s 重新估算
```

### 12.2 字幕时间线规范化

```
原始 segments → 过滤噪声文本
→ 按 start 排序
→ 确保 end > start（否则 end = start + 0.8s）
→ 裁剪时长到 [0.8s, 12.0s]
→ continuous 模式：每段 end = 下一段 start（无缝）
```

### 12.3 WSL2 Firefox Cookie 检测

```
/mnt/c/Users/ → 遍历用户目录
→ AppData/Roaming/Mozilla/Firefox/Profiles/
→ 找到含 cookies.sqlite 的 profile
→ 优先 .default-release
→ 传递给 yt-dlp: --cookies-from-browser "firefox:/path/to/profile"
```

---

## 13. 安全考量

1. **API 密钥保护**：存储在 `.env` 中，`.gitignore` 排除
2. **路径验证**：多处检查防止目录遍历（`resolve().relative_to()` 验证）
3. **文件类型白名单**：上传和下载都限制扩展名
4. **Cookie 文件**：保存在项目根目录，建议不提交到版本控制
5. **外部 API 输入验证**：`_decode_media_base64_to_temp()` 验证 base64 合法性
6. **SQLite 注入**：使用参数化查询（`?` 占位符）
7. **日志安全**：不记录 API 密钥内容
