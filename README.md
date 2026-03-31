# video2text

Video/audio to subtitle tool with dual ASR backends (FunASR Paraformer & faster-whisper), URL download support, and AI-powered subtitle translation.

**[中文文档](docs/README_zh.md)**

**Version**: v0.3.0

## Features

- **Dual ASR Backends**: FunASR (Paraformer, best for Chinese) / faster-whisper (multilingual)
- **URL Download**: yt-dlp for YouTube, Bilibili, etc.; XHS-Downloader for watermark-free Xiaohongshu videos
- **Auto Subtitle Import**: Platform-provided subtitles are imported first, skipping ASR when available
- **AI Translation**: OpenAI-compatible API for subtitle translation (SiliconFlow, DeepSeek, etc.)
- **GPU Acceleration**: NVIDIA GPU (Pascal+), Intel GPU (FunASR only)
- **WSL2 Auto-Detection**: Automatically reads Windows Firefox cookies on WSL2 for authenticated downloads
- **Proxy Support**: Configurable HTTP proxy for yt-dlp (YouTube, etc.)
- **External API**: Unified `/api/external/process` endpoint for third-party integration
- **Smart Dedup**: Identical uploads are cached by content fingerprint,- **Batch Translate**: Parallel subtitle translation with configurable thread count

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
./main.sh auto
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
| `DEFAULT_BACKEND` | ASR backend | `FunASR（Paraformer）` |
| `DEFAULT_FUNASR_MODEL` | FunASR model | `paraformer-zh` |
| `DEFAULT_WHISPER_MODEL` | Whisper model | `medium` |
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
├── main.py                 # Gradio UI (alternative frontend)
├── main.sh                 # Launcher script
├── backend/
│   ├── funasr_backend.py   # FunASR Paraformer backend
│   └── whisper_backend.py  # faster-whisper backend
├── utils/
│   ├── audio.py            # FFmpeg audio extraction
│   ├── core.py             # Core orchestration logic
│   ├── subtitle.py          # SRT/VTT/TXT subtitle I/O
│   ├── translate.py         # Parallel subtitle translation
│   ├── online_models.py    # Translation model profile management
│   └── xhs_downloader.py   # Xiaohongshu watermark-free download
├── scripts/
│   └── download_models.py  # Model pre-download script
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
