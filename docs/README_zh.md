# video2text

视频/音频转字幕工具，支持 FunASR Paraformer 和 faster-whisper 双 ASR 后端、URL 下载及 AI 字幕翻译。

**[English Documentation](../README.md)**

**版本**: v0.3.0

## 功能特性

- **双 ASR 后端**：FunASR（Paraformer，中文最佳）/ faster-whisper（多语言）
- **URL 下载**：yt-dlp 支持 YouTube、Bilibili 等；XHS-Downloader 支持小红书无水印视频
- **自动字幕导入**：优先使用平台提供的字幕，跳过语音识别
- **AI 翻译**：通过 OpenAI 兼容 API 翻译字幕（SiliconFlow、DeepSeek 等）
- **GPU 加速**：NVIDIA GPU（Pascal+）、Intel GPU（仅 FunASR）
- **WSL2 自动检测**：自动读取 Windows Firefox Cookie 实现认证下载
- **代理支持**：可配置 HTTP 代理（yt-dlp，用于 YouTube 等）
- **外部 API**：统一的 `/api/external/process` 端点供第三方集成
- **智能去重**：相同文件通过内容指纹缓存，避免重复处理
- **批量翻译**：并行字幕翻译，可配置线程数

## 快速开始

### Docker（推荐）

```bash
docker pull adolyming/video2text:latest
mkdir -p ~/video2text-data/workspace && cd ~/video2text-data

# 下载配置模板
curl -fsSL https://raw.githubusercontent.com/LYMingML/video2text/master/.env.example -o .env

# GPU 版本
docker run -d --name video2text --gpus all --restart unless-stopped \
  -p 7881:7881 \
  -v $(pwd)/workspace:/app/workspace \
  -v $(pwd)/.env:/app/.env \
  adolyming/video2text:latest

# CPU 版本：去掉 --gpus all
```

访问 `http://<IP>:7881`

### 本地安装

**前置条件**：Linux（Ubuntu 20.04+）、Python 3.10+、ffmpeg

```bash
git clone https://github.com/LYMingML/video2text.git
cd video2text
bash install.sh

# 启动
./main.sh auto
```

注册为 systemd 服务：
```bash
SETUP_SYSTEMD=1 bash install.sh
sudo systemctl status video2text
```

## 配置说明

`.env` 中的关键配置：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `APP_PORT` | 服务端口 | `7881` |
| `DEFAULT_BACKEND` | ASR 后端 | `FunASR（Paraformer）` |
| `DEFAULT_FUNASR_MODEL` | FunASR 模型 | `paraformer-zh` |
| `DEFAULT_WHISPER_MODEL` | Whisper 模型 | `medium` |
| `AUTO_SUBTITLE_LANG` | 字幕优先语言 | `zh` |
| `DOWNLOAD_PROXY` | yt-dlp HTTP 代理 | （空） |
| `FFMPEG_THREADS` | FFmpeg 线程数 | `4` |
| `FUNASR_BATCH_SIZE_S` | FunASR 批处理大小（秒） | `300` |
| `PREFER_INTEL_GPU` | 优先使用 Intel GPU | `0` |
| `ONLINE_MODEL_*` | 翻译模型配置 | - |

> 安全提示：`.env` 包含 API 密钥，请勿提交到版本控制。

## 外部 API

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

输入类型：`base64`、`url`、`history`

## 支持平台

| 区域 | 平台 |
|------|------|
| 中国 | Bilibili、小红书、抖音、快手、微博、知乎、优酷、爱奇艺、腾讯视频 |
| 国际 | YouTube、TikTok、Instagram、Twitter/X、Facebook、Vimeo、Spotify |

> 下载教程：[docs/video-download-tutorial.md](video-download-tutorial.md)

## 项目结构

```
video2text/
├── fastapi_app.py          # FastAPI 应用 + 内嵌 HTML/JS 前端
├── main.py                 # Gradio 界面（备选前端）
├── main.sh                 # 启动脚本
├── backend/
│   ├── funasr_backend.py   # FunASR Paraformer 后端
│   └── whisper_backend.py  # faster-whisper 后端
├── utils/
│   ├── audio.py            # FFmpeg 音频提取
│   ├── core.py             # 核心编排逻辑
│   ├── subtitle.py          # SRT/VTT/TXT 字幕 I/O
│   ├── translate.py         # 并行字幕翻译
│   ├── online_models.py    # 翻译模型配置组管理
│   └── xhs_downloader.py   # 小红书无水印下载
├── scripts/
│   └── download_models.py  # 模型预下载脚本
├── docs/                   # 文档
└── workspace/              # 任务输出目录
```

## 硬件加速

### NVIDIA GPU

- 架构：Pascal+（计算能力 >= 6.0）
- 需要：NVIDIA 驱动 + NVIDIA Container Toolkit（Docker）

```bash
nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader
```

### Intel GPU（仅 FunASR）

```bash
# 安装 oneAPI + Intel Extension for PyTorch
pip install intel-extension-for-pytorch
echo "PREFER_INTEL_GPU=1" >> .env
```

## 更新日志

### v0.3.0
- feat: WSL2 自动检测 Windows Firefox Cookie，实现认证下载
- feat: 可配置 HTTP 代理（`DOWNLOAD_PROXY`）用于 yt-dlp
- feat: 新增 `/api/folders/translate` 端点，支持翻译已有 workspace 文件夹
- feat: 页面初始化时恢复上次运行中/已完成的任务状态
- fix: download-multi API 从 GET+逗号分隔改为 POST+JSON body（修复文件名含逗号/冒号的问题）
- fix: download-output API 改为 POST+JSON body
- fix: "翻译"按钮在未选择任务时显示提示

### v0.2.3
- 文档：Docker 指南，包含中国镜像源和代理配置
- 文档：统一 Docker Hub 镜像 `adolyming/video2text:latest`
- 文档：NVIDIA Container Toolkit 安装步骤

### v0.2.2
- feat: 按内容指纹智能去重（基于 SQLite）
- feat: Docker 双镜像拆分（CPU / GPU）

### v0.2.1
- fix: `CUDA_VISIBLE_DEVICES` 未设置时的 GPU 检测
- fix: URL 下载 → 历史列表选择
- feat: Docker 多阶段构建，预装模型

## 许可证

MIT License
