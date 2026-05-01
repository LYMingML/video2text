# video2text

Video/audio to subtitle tool with triple ASR backends (VibeVoice, FunASR, faster-whisper), pluggable architecture, and AI-powered subtitle translation.

**[中文文档](docs/README_zh.md)**

**Version**: v0.6.0

## Features

- **Triple ASR Backends**: VibeVoice ASR (7B/9B, speaker diarization, default) / FunASR (Paraformer, best for Chinese) / faster-whisper (multilingual)
- **Pluggable Architecture**: Abstract base classes + registry pattern, easy to add new ASR/translate backends
- **4-Stage Pipeline**: Download → Preprocess (WAV extract + audio chunking) → ASR Transcribe (GPU) → Translate — stages run in parallel for throughput
- **Tesla P4 / Pascal GPU Support**: PyTorch 2.3.1 compatibility patches for 4-bit/8-bit quantized VibeVoice models (~6GB VRAM)
- **URL Download**: yt-dlp for YouTube, Bilibili, etc.; XHS-Downloader for watermark-free Xiaohongshu videos
- **Auto Subtitle Import**: Platform-provided subtitles are imported first, skipping ASR when available
- **AI Translation**: OpenAI-compatible API for subtitle translation (SiliconFlow, DeepSeek, etc.)
- **GPU Acceleration**: NVIDIA GPU (Pascal+), Intel GPU (FunASR only)
- **UI Preferences Persistence**: Auto-translate, auto-download, backend selection, language/device preferences saved to `.env` and restored on page load
- **WSL2 Auto-Detection**: Automatically reads Windows Firefox cookies on WSL2 for authenticated downloads
- **Proxy Support**: Configurable HTTP proxy for yt-dlp (YouTube, etc.)
- **External API**: Unified `/api/external/process` endpoint for third-party integration
- **Smart Dedup**: Identical uploads are cached by content fingerprint
- **Batch Translate**: Parallel subtitle translation with configurable thread count

## Quick Start

### Docker (Recommended)

```bash
docker pull adolyming/video2text:latest
mkdir -p ~/video2text-data/workspace && cd ~/video2text-data

# Download config template
curl -fsSL https://raw.githubusercontent.com/LYMingML/video2text/master/.env.example -o .env

# GPU
docker run -d --name video2text --gpus all --restart unless-stopped \
  -p 7881:7881 \
  -v $(pwd)/workspace:/app/workspace \
  -v $(pwd)/.env:/app/.env \
  adolyming/video2text:latest

# CPU only: remove --gpus all
```

Visit `http://<IP>:7881`

### Local Install

**Prerequisites**: Linux (Ubuntu 20.04+), Python 3.10+, ffmpeg

```bash
git clone https://github.com/LYMingML/video2text.git
cd video2text
bash install.sh

# Start
bash run.sh start
```

Register as systemd service:
```bash
SETUP_SYSTEMD=1 bash install.sh
sudo systemctl status video2text
```

## Configuration

Key settings in `.env`:

| Setting | Description | Default |
|--------|-------------|---------|
| `APP_PORT` | Service port | `7881` |
| `DEFAULT_BACKEND` | ASR backend | `VibeVoice ASR（长音频+说话人分离）` |
| `DEFAULT_FUNASR_MODEL` | FunASR model | `paraformer-zh` |
| `DEFAULT_WHISPER_MODEL` | Whisper model | `medium` |
| `VIBEVOICE_MODEL` | VibeVoice model | `VibeVoice-ASR-7B` |
| `VIBEVOICE_QUANT_BITS` | VibeVoice quantization (4/8) | `4` |
| `AUTO_SUBTITLE_LANG` | Subtitle priority language | `zh` |
| `DOWNLOAD_PROXY` | yt-dlp HTTP proxy | (empty) |
| `FFMPEG_THREADS` | FFmpeg thread count | `4` |
| `FUNASR_BATCH_SIZE_S` | FunASR batch size (seconds) | `300` |
| `PREFER_INTEL_GPU` | Prefer Intel GPU | `0` |
| `ONLINE_MODEL_*` | Translation model config | - |

> Security: `.env` contains API keys — never commit to version control.

## External API

### POST /api/external/process

```bash
curl -X POST "http://127.0.0.1:7881/api/external/process" \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "url",
    "url": "https://www.youtube.com/watch?v=xxxx",
    "backend": "FunASR（Paraformer）",
    "auto_subtitle_lang": "zh",
    "target_lang": "zh",
    "online_profile": "default",
    "online_model": "tencent/Hunyuan-MT-7B",
    "output_mode": "binary"
  }' --output result.zip
```

Input types: `base64`, `url`, `history`

## Supported Platforms

| Region | Platforms |
|--------|-----------|
| China | Bilibili, Xiaohongshu, Douyin, Kuaishou, Weibo, Zhihu, Youku, iQiyi, Tencent Video |
| Global | YouTube, TikTok, Instagram, Twitter/X, Facebook, Vimeo, Spotify |

> Download guide: [docs/video-download-tutorial.md](docs/video-download-tutorial.md)

## Project Structure

```
video2text/
├── fastapi_app.py          # FastAPI app + embedded HTML/JS frontend
├── run.sh                  # Launcher script (start/stop/restart/status/log)
├── src/
│   ├── backends/           # Pluggable ASR/translate backends
│   │   ├── base_asr.py     # ASR abstract base class
│   │   ├── base_translate.py # Translate abstract base class
│   │   ├── vibevoice_asr.py  # VibeVoice ASR (default, speaker diarization)
│   │   ├── funasr_asr.py   # FunASR Paraformer backend
│   │   ├── whisper_asr.py  # faster-whisper backend
│   │   └── siliconflow_translate.py  # OpenAI-compatible translation
│   ├── core/               # Business logic
│   │   ├── config.py       # Global config & constants
│   │   ├── workspace.py    # Workspace & file management
│   │   ├── transcribe_logic.py # Transcription orchestration
│   │   └── pipeline.py     # 4-stage pipeline engine
│   ├── utils/
│   │   ├── audio.py        # FFmpeg audio extraction
│   │   ├── subtitle.py     # SRT/VTT/TXT subtitle I/O
│   │   ├── translate.py    # Parallel subtitle translation
│   │   ├── online_models.py # Translation model profile management
│   │   └── xhs_downloader.py # Xiaohongshu watermark-free download
│   └── mcp_server.py       # MCP Server (HTTP client, no GPU work)
├── scripts/
│   └── download_models.py  # Model pre-download script
├── docker/
│   ├── Dockerfile          # GPU Docker image (PyTorch + CUDA)
│   └── docker-entrypoint.sh
├── docs/                   # Documentation
└── workspace/              # Task output directory
```

## Hardware Acceleration

### NVIDIA GPU

- Architecture: Pascal+ (compute capability >= 6.0)
- Requires: NVIDIA driver + NVIDIA Container Toolkit (Docker)

```bash
nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader
```

### Intel GPU (FunASR only)

```bash
# Install oneAPI + Intel Extension for PyTorch
pip install intel-extension-for-pytorch
echo "PREFER_INTEL_GPU=1" >> .env
```

## Changelog

### v0.5.1
- feat: `run.sh` — comprehensive startup script (HTTP+HTTPS dual-port, --status/--stop/--log, background mode)
- feat: Auto-cleanup chunk directories after transcription
- fix: `transcribe_logic.py` fast-path NameError when `chunk_dir` undefined
- fix: `docker/Dockerfile` removed missing `video2text.service` COPY

### v0.5.0
- feat: 4-stage pipeline integrated into FastAPI — Download → Preprocess (WAV + chunking) → ASR (GPU) → Translate
- feat: Queue page shows 4 columns with real-time stage progress and specific step descriptions
- feat: Audio chunking moved to preprocessing stage, transcription stage only does GPU work
- feat: Auto-translate handled by backend pipeline (no frontend JS triggers needed)
- feat: Auto-download ZIP to browser default path without confirmation dialog
- feat: UI preferences (auto-translate, auto-download, backend, language, device) persisted to `.env`, restored on page load
- fix: Workspace path resolution — `src/workspace` symlinked to shared workspace directory
- fix: faster-whisper GPU — normalize `cuda:0` → `cuda` for compatibility
- fix: Backend display name mapping with prefix matching

### v0.4.4
- feat: File structure reorganization, path optimization, startup script fixes
- fix: MCP server parameter names, file reading, translate polling

### v0.4.3
- fix: Remove Gradio dependency, cleanup redundant code

### v0.4.2
- fix: auto-translate/auto-download flags lost after page refresh — restore from server-side `auto_translate`/`auto_download` fields
- feat: SSE replaces 1s polling — event-driven UI updates with 1% progress threshold, auto-fallback to polling on failure
- feat: "直接保存" checkbox — fetch+blob download to browser default path without dialog

### v0.6.0
- refactor: Remove `main.py` (2151 lines) — all core logic migrated to `src/core/`
- refactor: Remove legacy `backend/` directory — unified under `src/backends/` plugin registry
- refactor: `fastapi_app.py` decoupled from `main.py`, all imports from `src/core/` directly
- refactor: Remove `scripts/main.sh` — `run.sh` is the sole launcher
- feat: `_do_transcribe_stream` generator migrated to `transcribe_logic.py` with unified backend registry
- fix: `pyproject.toml` build config for `src/` layout
- fix: Dockerfile uses `run.sh` instead of `main.sh`
- chore: Remove 8MB test artifact from git tracking

### v0.5.1
- fix: yt-dlp compatibility — add `--remote-components ejs:github` and auto-detect JS runtime (deno/node/bun)
- fix: plain text output now merges lines into paragraphs by time gap (readability improvement)
- fix: move mcp, httpx, faster-whisper, stable-ts from optional to core dependencies

### v0.4.0
- feat: VibeVoice ASR backend (7B/9B model, speaker diarization, 4-bit/8-bit quantization)
- feat: Pluggable backend architecture (`backends/` with abstract base classes + registry)
- feat: `core/` module extracted from `main.py` (config, workspace, transcribe logic)
- feat: VibeVoice quantization works on Tesla P4 (8GB VRAM, Pascal sm_61)
- feat: PyTorch 2.3.1 compatibility patches (version spoof, is_autocast_enabled, set_submodule backport)
- feat: Frontend model selector with `::N` quantization suffix (e.g., `VibeVoice-ASR-7B::4`)
- refactor: Remove Gradio UI, FastAPI is the sole frontend
- refactor: `backend/` → `backends/` with proper ASR/translate abstraction

### v0.3.0
- feat: WSL2 auto-detection of Windows Firefox cookies for authenticated downloads
- feat: configurable HTTP proxy (`DOWNLOAD_PROXY`) for yt-dlp
- feat: new `/api/folders/translate` endpoint for translating existing workspace folders
- feat: page init restores last running/completed job state after refresh
- fix: download-multi API changed from GET+comma-separated to POST+JSON body (fixes filenames with commas/colons)
- fix: download-output API changed to POST+JSON body
- fix: "Translate" button shows alert when no job is selected

### v0.2.3
- Docs: Docker guide with China mirror sources and proxy config
- Docs: Unified Docker Hub image `adolyming/video2text:latest`
- Docs: NVIDIA Container Toolkit install steps

### v0.2.2
- feat: Smart file dedup by content fingerprint (SQLite-based)
- feat: Docker dual-image split (CPU / GPU)

### v0.2.1
- fix: GPU detection when `CUDA_VISIBLE_DEVICES` is unset
- fix: URL download → history list selection
- feat: Docker multi-stage build with pre-bundled models

## License

MIT License
