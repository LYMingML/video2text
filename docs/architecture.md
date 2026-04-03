# video2text 项目架构文档

> **版本**: v0.4.2 | **语言**: Python 3.10+ | **许可**: MIT

---

## 目录

1. [项目概览](#1-项目概览)
2. [目录结构](#2-目录结构)
3. [整体架构](#3-整体架构)
4. [入口层](#4-入口层)
5. [核心模块 core/](#5-核心模块-core)
6. [插件化后端 backends/](#6-插件化后端-backends)
7. [工具模块 utils/](#7-工具模块-utils)
8. [前端架构](#8-前端架构)
9. [MCP Server](#9-mcp-server)
10. [配置系统](#10-配置系统)
11. [关键算法](#11-关键算法)
12. [架构模式与设计决策](#12-架构模式与设计决策)
13. [安全考量](#13-安全考量)

---

## 1. 项目概览

video2text 是一个视频/音频转字幕工具，核心功能：

- **语音识别（ASR）**：三后端架构 — FunASR Paraformer / faster-whisper / VibeVoice，支持 NVIDIA GPU 加速和量化
- **插件化架构**：装饰器注册系统，新增后端无需修改既有代码
- **四阶段流水线**：下载 → WAV 提取 → ASR 转录 → 翻译，队列化执行
- **视频下载**：yt-dlp（通用）+ XHS-Downloader（小红书），自动使用浏览器 Cookie
- **字幕翻译**：SiliconFlow / Ollama / OpenAI 兼容 API，并行批量翻译
- **文件管理**：内容指纹去重、自动清理、ZIP 打包
- **实时推送**：SSE（Server-Sent Events）替代轮询，事件驱动 UI 更新
- **MCP 集成**：可通过 MCP 协议被 Claude 等 LLM 调用

提供两套前端：
- **FastAPI**（`fastapi_app.py`）：内嵌 HTML/JS 单页应用，推荐使用
- **Gradio**（`main.py`）：备选 Web UI

---

## 2. 目录结构

```
video2text/
├── fastapi_app.py              # FastAPI 应用 + 内嵌 HTML/JS（主前端）
│                                # ~4900 行，包含 Tesla P4 兼容性补丁
├── main.py                     # Gradio UI（备选前端）+ 遗留核心编排逻辑
│                                # ~2150 行，v0.4.x 后核心逻辑已迁移到 core/
├── main.sh                     # 启动脚本（HTTP/HTTPS 双端口）
├── install.sh                  # 安装脚本（系统依赖 + Python 环境）
├── mcp_server.py               # MCP Server（纯 HTTP 客户端，不做 GPU 工作）
│                                # ~900 行，暴露全部功能为 MCP Tools
├── .env                        # 运行时配置（API 密钥、模型选择等）
├── .env.example                # 配置模板
├── pyproject.toml              # 项目元数据与依赖声明
│
├── core/                       # 核心业务逻辑（去 Gradio 依赖）
│   ├── __init__.py            # 模块说明
│   ├── config.py              # 全局常量、语言工具、GPU 检测
│   ├── workspace.py            # 工作目录管理、文件指纹、任务元数据
│   ├── pipeline.py             # 四阶段流水线引擎（queue.Queue + 守护线程）
│   └── transcribe_logic.py     # 转录编排逻辑（音频提取、分片、转录）
│
├── backends/                   # 插件化 ASR/翻译后端（@register_* 装饰器）
│   ├── __init__.py            # 后端注册表 + 自动导入
│   ├── base_asr.py            # ASR 抽象基类 ASRBackend
│   ├── base_translate.py      # 翻译抽象基类 TranslateBackend
│   ├── funasr_asr.py         # FunASR Paraformer 实现
│   ├── whisper_asr.py         # faster-whisper CTranslate2 实现
│   ├── vibevoice_asr.py       # Microsoft VibeVoice ASR 实现
│   └── siliconflow_translate.py # SiliconFlow / Ollama 翻译实现
│
├── backend/                    # 遗留后端（旧版 v0.3.x，仍可用）
│   ├── funasr_backend.py
│   └── whisper_backend.py
│
├── utils/                      # 工具模块
│   ├── __init__.py
│   ├── audio.py               # FFmpeg 音频提取与分片
│   ├── subtitle.py            # SRT/TXT 字幕读写与处理
│   ├── translate.py           # AI 字幕并行翻译（legacy，backends 用）
│   ├── online_models.py       # 翻译模型配置组管理（.env 读写）
│   └── xhs_downloader.py      # 小红书无水印视频下载
│
├── scripts/
│   └── download_models.py      # FunASR 模型预下载脚本
│
├── docs/                       # 文档目录
├── tests/                      # 测试文件
├── docker-compose.yml          # Docker 部署配置
├── docker-entrypoint.sh         # Docker 入口脚本
│
└── workspace/                  # 任务输出目录
    ├── temp_video/             # 临时视频缓存
    ├── fingerprints.db          # 文件指纹去重 SQLite 数据库
    └── <task_dir>/             # 每个任务一个子目录
        ├── <prefix>.wav        # 提取的音频
        ├── <prefix>.srt        # 原始字幕
        ├── <prefix>.txt        # 原始纯文本
        ├── <prefix>.zh.srt     # 中文翻译字幕
        ├── <prefix>.zh.txt     # 中文翻译纯文本
        └── <prefix>.zip        # 打包下载
```

---

## 3. 整体架构

### 3.1 系统分层

```
┌─────────────────────────────────────────────────────┐
│                    用户交互层                         │
│  ┌──────────────┐    ┌──────────────┐              │
│  │ FastAPI SPA  │    │ Gradio UI    │              │
│  │ (内嵌 HTML)  │    │ (备选前端)    │              │
│  └──────┬───────┘    └──────┬───────┘              │
│         │  SSE / HTTP / Form │                      │
├─────────┼────────────────────┼──────────────────────┤
│         │        API 层       │                      │
│  ┌──────▼────────────────────▼──────┐              │
│  │       fastapi_app.py              │              │
│  │  任务队列 · SSE 推送 · 文件管理   │              │
│  │  模型配置 · URL 下载 · 外部 API   │              │
│  └──────┬────────────────────┬──────┘              │
│         │  import core as    │                      │
├─────────┼────────────────────┼──────────────────────┤
│         │       核心层        │                      │
│  ┌──────▼───────┐  ┌────────▼────────┐             │
│  │  core/       │  │  core/pipeline.py│             │
│  │  workspace.py │  │  四阶段流水线    │             │
│  │  config.py   │  │  下载→WAV→ASR→翻译│             │
│  │  transcribe_ │  │                  │             │
│  │  logic.py    │  └────────┬────────┘             │
│  └──────┬───────┘           │                       │
│         │                    │                       │
├─────────┼────────────────────┼──────────────────────┤
│         │       插件后端层    │                      │
│  ┌──────▼────────────────────▼──────┐              │
│  │         backends/                │              │
│  │  @register_asr  @register_translate│             │
│  │  ┌─────────┐  ┌─────────┐       │              │
│  │  │ FunASR  │  │ Whisper │       │              │
│  │  │ VibeVoice│  │SiliconFlow│    │              │
│  │  └─────────┘  └─────────┘       │              │
│  └──────┬────────────────────┬──────┘              │
│         │                    │                      │
├─────────┼────────────────────┼──────────────────────┤
│         │       工具层        │                      │
│  ┌──────▼────────────────────▼──────┐              │
│  │           utils/                  │              │
│  │  audio.py  subtitle.py  translate.py│            │
│  └──────┬────────────────────┬──────┘              │
│         │                    │                      │
├─────────┼────────────────────┼──────────────────────┤
│    ffmpeg    torch/funasr/whisper  HTTP API        │
│    (音频处理)  (GPU 计算)         (翻译服务)         │
└─────────────────────────────────────────────────────┘

         ┌──────────────────────────────┐
         │       MCP Server 层          │
         │  mcp_server.py (纯 HTTP 客户端)│
         │  不 import 任何 GPU 模块      │
         │  通过 httpx 调用本地 FastAPI   │
         └──────────────┬───────────────┘
                        │ httpx / SSE
         ┌──────────────▼───────────────┐
         │    FastAPI (同一进程)         │
         └──────────────────────────────┘
```

### 3.2 四阶段流水线数据流

```
PipelineTask
    │
    ▼
[Stage 1] Download Worker
    │ queue.Queue[PipelineTask]
    │ 职责：归档源文件到 temp_video、创建 job 目录
    ▼
[Stage 2] Extract Worker
    │ queue.Queue[PipelineTask]
    │ 职责：ffmpeg 提取 WAV（按后端要求设置采样率）
    ▼
[Stage 3] Transcribe Worker
    │ queue.Queue[PipelineTask]
    │ 职责：加载 ASR 后端、分片转录、保存原文 SRT/TXT
    ▼
[Stage 4] Translate Worker
    │ queue.Queue[PipelineTask]
    │ 职责：加载翻译后端、并行翻译、保存翻译 SRT/TXT
    ▼
PipelineTask.done = True
    │
    ▼
FastAPI 触发 SSE 推送 → 浏览器更新 UI
```

### 3.3 插件化后端注册机制

```
backends/__init__.py
    │
    ├── @register_asr(FunASRASR)   → _ASR_REGISTRY["FunASRASR"]
    ├── @register_asr(WhisperASR)   → _ASR_REGISTRY["WhisperASR"]
    ├── @register_asr(VibeVoiceASR) → _ASR_REGISTRY["VibeVoiceASR"]
    ├── @register_translate(SiliconFlowTranslate)
    │                                 → _TRANSLATE_REGISTRY["SiliconFlowTranslate"]
    │
    ├── get_asr_backend("FunASRASR")  → FunASRASR()
    ├── get_asr_backend("VibeVoiceASR") → VibeVoiceASR()
    ├── get_translate_backend("SiliconFlowTranslate") → SiliconFlowTranslate()
    │
    └── list_asr_backends() → ["FunASRASR", "WhisperASR", "VibeVoiceASR"]
```

---

## 4. 入口层

### 4.1 fastapi_app.py — 主入口（~4900 行）

主入口函数 `main()` 解析 `--port`（默认从 `APP_PORT` 环境变量读取）、`--ssl-certfile` / `--ssl-keyfile`（可选 HTTPS）、`--host 0.0.0.0`。

**Tesla P4 / sm_61 兼容性补丁**（文件头部）：

| 补丁 | 问题 | 方案 |
|------|------|------|
| `torch` 版本欺骗 | transformers >= 5.3 要求 PyTorch >= 2.4，但 2.3 才支持 sm_61 | 拦截 `importlib.metadata.version`，torch 2.3.x 返回 2.4.0+cu121 |
| `is_autocast_enabled()` 参数 | transformers >= 5.3 调用 `is_autocast_enabled(device_type)` | 包装函数，忽略意外参数 |
| `nn.Module.set_submodule()` | PyTorch 2.3.x 没有此方法（2.5+ 才有） | 手写 backport 实现 |

### 4.2 main.py — Gradio 备选入口（~2150 行）

`main.py` 原有核心编排逻辑已迁移到 `core/` 目录，但以下函数仍被 `fastapi_app.py` 通过 `import main as core` 引用：

- `_parse_srt_segments()` — SRT 解析
- `_build_all_bundle()` — ZIP 打包
- `_save_task_meta()` / `_load_task_meta()` — 任务元数据
- `_resolve_current_job()` — 任务目录解析
- `_workspace_history_text()` — 历史文本生成
- `_list_job_folders_meta()` / `_list_uploaded_videos()` — 历史列表
- `_stage_source_media_to_temp_video()` — 源文件归档
- `_find_duplicate_file()` — 去重
- `_resolve_file_prefix()` — 文件前缀解析

### 4.3 mcp_server.py — MCP 入口（~900 行）

独立进程，不 import 任何 GPU 模块（torch/funasr/transformers），通过 HTTP 调用 FastAPI。

---

## 5. 核心模块 core/

### 5.1 config.py — 全局配置与工具

**工作目录常量**：
```python
WORKSPACE_DIR = workspace/          # 任务输出根目录
TEMP_VIDEO_DIR = workspace/temp_video/  # 临时视频缓存
TEMP_VIDEO_KEEP_COUNT = 5           # 临时文件保留数量（可配置）
SUPPORTED_EXTS = [.mp4, .mkv, .avi, .mov, .mp3, .wav, ...]  # 支持的媒体格式
```

**停止事件**：`STOP_EVENT = threading.Event()` — 用户取消任务的全局信号

**语言解析**：

| 函数 | 说明 |
|------|------|
| `_parse_lang_code(choice)` | 从 `"zh（普通话）"` → `"zh"`，`"自动检测"` → `"auto"` |
| `_looks_non_chinese_text(text)` | 中文字符占比 < 25% 且含 12+ 拉丁/日韩字符 → 非中文 |
| `_guess_source_lang(lang_code, text)` | lang=auto 时，从文本特征推断：日文正则→ja，韩文正则→ko，西班牙语标记→es，否则 en |

**FunASR 模型自动选择** `_pick_funasr_model_for_language()`：
- 非中文/日/韩/英 → 自动切换为 `iic/SenseVoiceSmall`（多语言）
- 自动检测语言场景 → 同上
- 说话人分离模型 → 保持用户选择

**GPU 检测** `_has_nvidia_gpu()` — 检测链：
1. `CUDA_VISIBLE_DEVICES` 是否为 `-1`/`none`（跳过）
2. `torch.cuda.is_available()`
3. `/dev/nvidiactl` 或 `/proc/driver/nvidia` 存在
4. `nvidia-smi -L`

### 5.2 workspace.py — 文件管理

**文件指纹去重**（SQLite `workspace/fingerprints.db`）：

```sql
CREATE TABLE file_fingerprints (
    id INTEGER PRIMARY KEY,
    file_path TEXT UNIQUE,
    file_size INTEGER,
    head_50 BLOB,    -- 前50字节
    tail_50 BLOB,    -- 后50字节
    updated_at REAL
);
```

去重流程：`get_file_fingerprint()` → `size + head_50 + tail_50` 三元组比较 → 数据库查询匹配

**任务元数据**（`workspace/<task>/task_meta.json`）：
```json
{
  "file_prefix": "video",
  "lang_code": "zh",
  "source_lang": "zh",
  "is_non_zh": false
}
```

**核心函数**：

| 函数 | 说明 |
|------|------|
| `_stage_source_media_to_temp_video()` | 归档源媒体到 temp_video，自动去重 |
| `_prune_temp_video_dir()` | 清理超量临时文件，跳过正在转录的文件 |
| `_schedule_video_deletion()` | 延迟 60 秒后删除临时视频（daemon 线程） |
| `_resolve_job_dir_for_input()` | 根据输入文件确定 job 目录（WAV 文件复用已有目录） |
| `_cleanup_job_source_media()` | 删除 job 目录中的非 wav 源媒体文件 |
| `_delete_job_folder()` | 删除整个 job 文件夹（含目录遍历安全检查） |
| `_is_final_output_file()` | 判断是否为最终输出文件（防误删 ZIP 等中间产物） |

### 5.3 pipeline.py — 四阶段流水线引擎

**`PipelineTask`** dataclass 贯穿四个阶段，包含所有配置和回调：

```python
@dataclass
class PipelineTask:
    task_id: str
    # 输入
    video_path: str = ""
    audio_path: str = ""
    # ASR 配置
    asr_backend: str = ""          # 后端类名
    model_name: str = ""
    language: str = "auto"
    device: str = "auto"
    # 转录结果
    segments: list[tuple[float, float, str]] = field(default_factory=list)
    # 翻译配置
    auto_translate: bool = False
    translate_backend: str = "SiliconFlowTranslate"
    translate_model: str = ""
    target_lang: str = "zh"
    # 翻译结果
    translated_segments: list[tuple] = field(default_factory=list)
    # 工作目录
    job_dir: str = ""
    file_prefix: str = ""
    # 回调 (task_id, msg/ratio) — 线程安全
    status_cb, log_cb, progress_cb: Callable
    # 结果
    error: str = ""
    done: bool = False
```

**全局单例**：`get_pipeline() → Pipeline`，懒初始化，线程安全。

**阶段进度分配**：
- Stage 1（下载）：0 ~ 5%
- Stage 2（WAV 提取）：5%
- Stage 3（ASR 转录）：10% ~ 90%
  - 后端加载：10%
  - 分片转录：10% ~ 85%（按实际进度）
  - 保存原文：85% ~ 90%
- Stage 4（翻译）：92% ~ 99%
  - 翻译中：按段数实时更新
  - 完成后：100%

### 5.4 transcribe_logic.py — 转录编排逻辑

`do_transcribe()` 函数实现音频提取和分片转录的编排：

```
输入视频 → 是否已是目标 WAV？
  ├── 是 → 直接复用
  └── 否 → ffmpeg 提取（16kHz/24kHz 按后端）
       → 60s 后安排删除原始视频

读取音频时长 → 获取后端配置（chunk_seconds, overlap_seconds, sample_rate）

是否需要分片？时长 > chunk_seconds？
  ├── 是 → split_audio_chunks() 外部分片
  └── 否 → 整段转录 [(path, 0, duration)]

逐分片转录：
  → asr.transcribe(chunk_path, ...)
  → 去重叠（重叠区域 > overlap_seconds 的片段丢弃）
  → 全局时间偏移
  → all_segments.extend()

清理临时分片目录 → 返回 all_segments
```

---

## 6. 插件化后端 backends/

### 6.1 ASR 基类 `ASRBackend`

所有 ASR 后端必须实现 `transcribe()` 方法，提供以下属性：

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 显示名称，如 `"FunASR ASR"` |
| `description` | `str` | 简短描述 |
| `default_model` | `str` | 默认模型名 |
| `supported_models` | `list[str]` | 支持的模型列表 |
| `default_chunk_seconds` | `int` | 默认分片时长（0=不分片） |
| `default_overlap_seconds` | `int` | 默认重叠时长 |
| `sample_rate` | `int` | 音频采样率（FunASR: 16000, VibeVoice: 24000） |

**接口**：
```python
def transcribe(
    audio_path: str,
    model_name: str = "",
    language: str = "auto",
    device: str = "auto",
    progress_cb: Callable[[float, str], None] = None,
) -> list[tuple[float, float, str]]:  # [(start, end, text), ...]
```

### 6.2 FunASR ASR

**模型支持**（从 ModelScope 下载）：

| 模型 | 语言 | 说明 |
|------|------|------|
| `paraformer-zh` | 中文 | 普通话精度推荐 |
| `paraformer` | 中文 | 普通话全量 |
| `paraformer-zh-streaming` | 中文 | 流式低延迟 |
| `paraformer-zh-spk` | 中文 | 说话人分离 |
| `paraformer-en` | 英文 | 英文优化 |
| `paraformer-en-spk` | 英文 | 英文说话人分离 |
| `iic/SenseVoiceSmall` | 中粤英日韩 | 多语言模型 |
| `EfficientParaformer-large-zh` | 中文 | 长语音优化 |
| `iic/speech_seaco_paraformer_large_asr_nat-*` | 中文 | 高精度 |

**后处理管线**：
1. 去情感标签：`<|HAPPY|><|Speech|>` → 正则清除
2. 去标签：`<|...|>` → 空
3. 去 emoji
4. 去重标点
5. 文本分片：句末标点立即分割，停顿标点超限分割，普通字符强制分割
6. 时间跳变修复：>300s 跳变用 `字符数 × 0.15s` 估算时长
7. 说话人回退：基于 1.2 秒静默间隔切换角色

### 6.3 Whisper ASR

**模型**：`tiny` / `base` / `small` / `medium` / `large-v3`

**量化回退链**：
- RTX/专业卡 → `float16`
- 通用 CUDA → `float16` → `int8_float16` → `int8`
- Tesla P4 (sm_61) → `int8`（强制）

**VAD 过滤**：`vad_filter=True`，`min_silence_duration_ms=500`

**中文提示**：`"以下是普通话的句子。"` — 提升中文识别准确率

**幻觉清理**：`[cite: N]` / `[citation: N]` / `subtitle by ...` / `www.*.com` 等模式

### 6.4 VibeVoice ASR

**微软开源 Whisper 变种**，支持更长的上下文和更好的质量。

**模型**：

| 模型 | 量化 | VRAM | 说明 |
|------|------|------|------|
| `bezzam/VibeVoice-ASR-7B::4` | 4-bit | ~5GB | 轻量推荐 |
| `bezzam/VibeVoice-ASR-7B::8` | 8-bit | ~8GB | 更高质量 |
| `microsoft/VibeVoice-ASR-HF::4` | 4-bit | ~9GB | 9B 模型 |
| `microsoft/VibeVoice-ASR-HF::8` | 8-bit | ~16GB | 最高质量 |

**特点**：
- 60 分钟分片（10 分钟重叠），远超其他后端
- 24kHz 采样率（其他后端 16kHz）
- 自带切片逻辑，不需要外部 `split_audio_chunks()`

### 6.5 翻译基类 `TranslateBackend`

```python
def translate_segments(
    segments: list[tuple[float, float, str]],
    source_lang: str = "auto",
    target_lang: str = "zh",
    log_cb: Callable[[str], None] = None,
    progress_cb: Callable[[int, int, float], None] = None,
    **kwargs,  # base_url, api_key, model_name 等
) -> list[tuple[float, float, str]]
```

### 6.6 SiliconFlow Translate

**多后端兼容**：
- SiliconFlow：`/chat/completions` 流式 API
- Ollama：`/api/chat` 流式 API（自动检测 `:11434` 或 `ollama`）
- 任何 OpenAI 兼容 API

**翻译 Prompt**：
```
你是专业字幕翻译助手。请把下面原文翻译成{target_name}。
要求：
1. 只输出译文，不要解释。
2. 保持原句语气与信息，不要扩写。
3. 如果原文已是{target_name}，直接输出原文。
源语言提示: {source_lang}
目标语言代码: {target_code}
原文: {text}
```

**缓存策略**：
- `_LINE_CACHE`：逐行翻译结果缓存 `(base_url, model, text)` → `translated`
- `_MODEL_CACHE`：模型列表缓存 `(base_url, api_key_prefix)` → `[models]`
- 并行线程数：默认 5（`set_parallel_threads()` 可调）

---

## 7. 工具模块 utils/

### 7.1 audio.py — FFmpeg 音频处理

**硬件加速优先级**：`Intel QSV` > `NVIDIA CUDA` > `CPU`

**FFmpeg 参数**：
```
ffmpeg -hwaccel qsv -qsv_device /dev/dri/renderD128 \
       -i input -vn -ac 1 -ar 16000 -acodec pcm_s16le output.wav
```

**`extract_audio(input, output, sr=16000)`** — 提取 WAV
**`get_audio_duration(audio)`** — ffprobe 获取时长
**`split_audio_chunks(audio, output_dir, chunk_s, overlap_s)`** — 分片

### 7.2 subtitle.py — 字幕 I/O

**时间线规范化** `normalize_segments_timeline(segments, continuous=True)`：
1. 过滤空文本和噪声文本
2. 排序，按 start 去重
3. 确保 `end > start`（最小 0.8s）
4. 限制时长 [0.8s, 12.0s]
5. `continuous=True`：每段 `end = 下一段 start`（字幕无缝衔接）

**段落合并** `collect_plain_text(segments)`：
- 2 秒间隔 → 分段
- 累积超过 200 字符且句末标点 → 分段

**说话人脚本** `format_speaker_script(segments)`：
- 按 `角色N` 分组，输出剧本格式 `【角色N】（HH:MM:SS）文本`

**换行处理**：
- 中文：每 25 字符换行
- 外文：每 20 词换行

### 7.3 online_models.py — 配置管理

`.env` 中的配置组存储格式：
```
ONLINE_MODEL_PROFILE_1_NAME=默认配置
ONLINE_MODEL_PROFILE_1_BASE_URL=https://api.siliconflow.cn/v1
ONLINE_MODEL_PROFILE_1_API_KEY=sk-xxx
ONLINE_MODEL_PROFILE_1_DEFAULT_MODEL=Pro/moonshotai/Kimi-K2.5
ONLINE_MODEL_PROFILE_1_MODEL_LIST_JSON=["model1",...]
ONLINE_MODEL_ACTIVE_PROFILE=默认配置
```

**App 设置**（也是 .env）：
```
DEFAULT_BACKEND=Funasr ASR（Paraformer）
DEFAULT_FUNASR_MODEL=paraformer-zh
DEFAULT_WHISPER_MODEL=medium
AUTO_SUBTITLE_LANG=zh
APP_PORT=7881
```

---

## 8. 前端架构

### 8.1 FastAPI 内嵌 SPA

`fastapi_app.py` 的 `index()` 返回完整 HTML 页面（~3400 行，内嵌 CSS/JS）。

**四页面结构**：
- **主页（home）**：视频上传/URL 输入 → 转录 → 翻译 → 输出展示 + 实时进度条
- **文件管理（file）**：历史文件夹表格（Shift/Ctrl 多选、排序、搜索）+ 输出文件表格 + 批量下载
- **配置模型（model）**：翻译配置组 CRUD、API 测试、模型列表获取
- **任务队列（queue）**：运行中任务 + 排队列表 + 历史记录

### 8.2 SSE 事件驱动更新

**端点**：`GET /api/jobs/{job_id}/stream`

```
Client 端：EventSource("/api/jobs/xxx/stream")
    │
    ▼
Server 端：StreamingResponse + queue.Queue
    │
    ├── 立即推送初始状态
    ├── 30s 心跳：": heartbeat\n\n"
    └── 状态变化推送："data: {json}\n\n"
        └── job.done → 关闭连接
```

**推送节流**（`_notify_sse()`）：
- 进度变化阈值：1%（避免过度推送）
- 状态变化：`done` / `failed` / `running` 任一改变

**前端轮询回退**：SSE 不可用时，每秒轮询 `/api/jobs/{id}`。

### 8.3 外部 API

**统一入口** `POST /api/external/process`：

```python
{
    "source_type": "url" | "base64" | "history",
    "url": "https://...",
    "file_path": "/path/to/file",
    "history_video": "relative/path",
    "backend": "FunASR ASR",
    "language": "自动检测",
    "device": "CUDA",
    "target_lang": "zh",
    "online_profile": "默认配置",
    "online_model": "Kimi-K2.5",
    "auto_subtitle_lang": "zh",
    "force_asr": false,
    "output_mode": "json" | "binary" | "base64"
}
```

---

## 9. MCP Server

### 9.1 设计原则

`mcp_server.py` 是一个**纯 HTTP 客户端进程**：
- 不 import `torch` / `funasr` / `transformers` 等 GPU 模块
- 所有重型计算由 FastAPI 进程完成
- 通过 `httpx.AsyncClient` 调用 FastAPI 的 REST API

### 9.2 MCP Tools

共 23 个工具，分为 6 类：

**转录/翻译**：
| 工具 | 说明 |
|------|------|
| `v2t_transcribe_file` | 上传本地文件并转录 |
| `v2t_transcribe_url` | 下载 URL 视频并转录 |
| `v2t_process` | 统一入口（base64/url/history） |
| `v2t_translate` | 翻译已完成的转录结果 |
| `v2t_folder_translate` | 翻译已有文件夹中的字幕 |

**视频下载**：
| 工具 | 说明 |
|------|------|
| `v2t_download_video` | 仅下载视频，不转录 |
| `v2t_extract_audio` | 从视频提取 WAV 音频 |

**文件管理**：
| 工具 | 说明 |
|------|------|
| `v2t_list_folders` | 列出所有任务文件夹 |
| `v2t_list_files` | 列出文件夹内文件 |
| `v2t_read_file` | 读取文件内容（SRT/TXT） |
| `v2t_download_file` | 下载文件到本地 |
| `v2t_delete_folder` | 删除任务文件夹 |
| `v2t_delete_folders` | 批量删除 |
| `v2t_all_output_files` | 跨文件夹汇总输出文件 |

**任务管理**：
| 工具 | 说明 |
|------|------|
| `v2t_job_status` | 查询任务状态 |
| `v2t_stop_job` | 停止运行中的任务 |
| `v2t_queue_status` | 队列状态 |

**配置/信息**：
| 工具 | 说明 |
|------|------|
| `v2t_health` | 健康检查 |
| `v2t_list_backends` | 列出可用后端 |
| `v2t_list_translate_profiles` | 翻译配置组列表 |
| `v2t_get_settings` | 应用设置 |
| `v2t_list_history` | 历史视频和文件夹 |
| `v2t_upload_cookie` | 上传 Cookie |
| `v2t_job_status` | 任务状态 |

---

## 10. 配置系统

### 10.1 环境变量汇总

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `APP_PORT` | `7881` | 服务端口 |
| `DEFAULT_BACKEND` | `FunASR（Paraformer）` | ASR 后端 |
| `DEFAULT_FUNASR_MODEL` | `paraformer-zh` | FunASR 模型 |
| `DEFAULT_WHISPER_MODEL` | `medium` | Whisper 模型 |
| `AUTO_SUBTITLE_LANG` | `zh` | 字幕优先语言 |
| `DOWNLOAD_PROXY` | (空) | yt-dlp HTTP 代理 |
| `FFMPEG_THREADS` | `4` | FFmpeg 线程数 |
| `FUNASR_BATCH_SIZE_S` | `300` | FunASR 批处理大小 |
| `PREFER_INTEL_GPU` | `0` | 优先使用 Intel GPU |
| `TEMP_VIDEO_KEEP_COUNT` | `5` | 临时视频保留数量 |
| `TRANSLATE_PARALLEL_THREADS` | `3` | 翻译并行线程数 |
| `BROWSER_DEBUG_PORT` | `9222` | Chrome 远程调试端口 |

### 10.2 GPU 兼容性策略

```
Tesla P4 / Pascal (sm_61):
  → torch==2.3.1+cu121 (torch 2.4+ 不支持 sm_61)
  → transformers 通过版本欺骗兼容
  → Whisper 强制 int8 量化
  → PyTorch 2.3 缺少 set_submodule() → backport 实现

Ampere+ (sm_80/86/89/90):
  → torch 2.4+ + CUDA 12.4
  → float16 量化

无 GPU:
  → torch CPU 版本
```

---

## 11. 关键算法

### 11.1 音频分片与去重叠

```
原始音频 → ffmpeg 分片 → [chunk_001.wav, chunk_002.wav, ...]
    │
    ├── chunk_001 (0-120s): 直接识别
    └── chunk_002 (110-230s): 识别 → 去重叠
         ├── 重叠区域 110-120s 内片段 → 丢弃
         ├── 其余片段时间 +110s 偏移
         └── cutoff = 120s（去除重叠边界模糊区）
```

### 11.2 字幕时间线无缝衔接

```
原始 segments（可能有间隙）：
  [0, 3] 你好
  [4, 7] 世界
  [10, 14] 今天天气

continuous 规范化后（end = 下一段 start）：
  [0, 4] 你好
  [4, 10] 世界
  [10, ...] 今天天气

→ 字幕在播放器中无缝衔接，无闪烁
```

### 11.3 WSL2 Firefox Cookie 检测

```
WSL2 路径：/mnt/c/Users/<username>/...
  → AppData/Roaming/Mozilla/Firefox/Profiles/<profile>/cookies.sqlite
  → 优先 .default-release
  → yt-dlp: --cookies-from-browser "firefox:/absolute/path/to/profile"
```

### 11.4 SSE 推送节流

```
每次 _set_job_progress() 调用时：
  │
  ├── 新进度 - 上次推送进度 < 1%？
  │     └── 是 → 跳过推送（节流）
  ├── job.done / job.failed / job.running 变化？
  │     └── 是 → 立即推送
  └── 其他状态字段变化？
        └── 是 → 立即推送
```

---

## 12. 架构模式与设计决策

### 12.1 核心逻辑与界面分离

`main.py` 中的核心函数通过 `import main as core` 被 `fastapi_app.py` 引用，而 `core/` 目录中新建的模块不依赖 Gradio，保持纯粹的业务逻辑。这使得：
- 核心逻辑可独立测试
- FastAPI 和 Gradio 前端可共存（但不能同时运行，因共享全局状态）

### 12.2 插件化后端注册

`@register_asr` / `@register_translate` 装饰器 + 自动导入机制（`backends/__init__.py` 底部 `_auto_import_backends()`），确保：
- 新增后端只需创建文件，无需修改注册表
- 缺失依赖的后端静默跳过，不影响其他后端

### 12.3 任务队列与守护线程

四阶段流水线每阶段一个 `queue.Queue` + 守护线程：
- 阶段间解耦：一个阶段阻塞不影响其他阶段
- 单个 ASR 任务独占 GPU，但流水线支持队列堆积
- 守护线程在进程退出时自动终止

### 12.4 SSE 替代轮询

v0.4.2 引入 SSE 事件驱动更新：
- 浏览器：`new EventSource(url)` → 自动接收推送
- 服务器：`StreamingResponse` + `queue.Queue` 订阅模型
- 节流：1% 进度阈值，减少无效推送

### 12.5 MCP 隔离设计

MCP Server 作为独立进程，不 import GPU 模块：
- MCP 协议启动快（无需等待模型加载）
- FastAPI 进程负责所有计算，单一 GPU 内存占用
- MCP 可同时服务多个 LLM 客户端

---

## 13. 安全考量

1. **API 密钥保护**：存储在 `.env` 中，`.gitignore` 排除
2. **路径验证**：多处使用 `resolve().relative_to()` 防止目录遍历
3. **文件类型白名单**：上传和下载限制扩展名
4. **Cookie 文件**：项目根目录保存，建议不提交版本控制
5. **外部 API 输入验证**：`base64` 合法性验证
6. **SQLite 注入**：使用参数化查询（`?` 占位符）
7. **日志安全**：不记录 API 密钥完整内容
8. **任务隔离**：每个任务在独立目录，避免文件冲突
