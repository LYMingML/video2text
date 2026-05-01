# video2text

视频/音频转字幕工具，支持 VibeVoice ASR、FunASR Paraformer、faster-whisper 三大 ASR 后端、插件化架构、URL 下载及 AI 字幕翻译。

**[English Documentation](../README.md)**

**版本**: v0.6.0

## 功能特性

- **三大 ASR 后端**：VibeVoice ASR（7B/9B，说话人分离，默认）/ FunASR（Paraformer，中文最佳）/ faster-whisper（多语言）
- **插件化架构**：抽象基类 + 注册表模式，轻松添加新 ASR/翻译后端
- **四阶段流水线**：下载 → 预处理（WAV 提取 + 音频分片）→ ASR 转录（GPU）→ 翻译，阶段间并行提升吞吐
- **Tesla P4 / Pascal GPU 支持**：PyTorch 2.3.1 兼容补丁，支持 4-bit/8-bit 量化 VibeVoice 模型（~6GB 显存）
- **URL 下载**：yt-dlp 支持 YouTube、Bilibili 等；XHS-Downloader 支持小红书无水印视频
- **自动字幕导入**：优先使用平台提供的字幕，跳过语音识别
- **AI 翻译**：通过 OpenAI 兼容 API 翻译字幕（SiliconFlow、DeepSeek 等）
- **GPU 加速**：NVIDIA GPU（Pascal+）、Intel GPU（仅 FunASR）
- **UI 偏好持久化**：自动翻译、自动下载、后端选择、语言/设备偏好保存到 `.env`，刷新页面自动恢复
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
bash run.sh start
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
| `DEFAULT_BACKEND` | ASR 后端 | `VibeVoice ASR（长音频+说话人分离）` |
| `DEFAULT_FUNASR_MODEL` | FunASR 模型 | `paraformer-zh` |
| `DEFAULT_WHISPER_MODEL` | Whisper 模型 | `large-v3` |
| `VIBEVOICE_MODEL` | VibeVoice 模型 | `VibeVoice-ASR-7B` |
| `VIBEVOICE_QUANT_BITS` | VibeVoice 量化位数 (4/8) | `4` |
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
├── fastapi_app.py          # FastAPI 应用 + 内嵌 HTML/JS 前端（唯一前端）
├── run.sh                  # 启动脚本 (start/stop/restart/status/log)
├── src/
│   ├── backends/           # 插件化 ASR/翻译后端
│   │   ├── __init__.py     # 注册表 + 工厂函数
│   │   ├── base_asr.py     # ASR 抽象基类
│   │   ├── base_translate.py # 翻译抽象基类
│   │   ├── vibevoice_asr.py  # VibeVoice ASR 后端（默认，说话人分离）
│   │   ├── funasr_asr.py   # FunASR Paraformer 后端
│   │   ├── whisper_asr.py  # faster-whisper 后端
│   │   └── siliconflow_translate.py  # OpenAI 兼容翻译后端
│   ├── core/               # 业务逻辑
│   │   ├── config.py       # 全局配置与常量
│   │   ├── workspace.py    # 工作目录管理
│   │   ├── transcribe_logic.py # 转录编排（含生成器版本）
│   │   └── pipeline.py     # 四阶段流水线引擎
│   ├── utils/
│   │   ├── audio.py        # FFmpeg 音频提取与分片
│   │   ├── subtitle.py     # SRT/VTT/TXT 字幕读写
│   │   ├── translate.py    # 并行字幕翻译
│   │   ├── online_models.py # 翻译模型配置组管理
│   │   └── xhs_downloader.py # 小红书无水印下载
│   └── mcp_server.py       # MCP Server（HTTP 客户端，不做 GPU 计算）
├── scripts/
│   └── download_models.py  # 模型预下载脚本
├── docker/
│   ├── Dockerfile          # GPU Docker 镜像（PyTorch + CUDA）
│   └── docker-entrypoint.sh
├── docs/                   # 文档
└── workspace/              # 任务输出目录
```

## 硬件加速

### NVIDIA GPU

- 架构：Pascal+（计算能力 >= 6.0），Tesla P4/P40 等 Pascal 卡通过量化补丁支持 VibeVoice
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

### v0.5.1
- feat: `run.sh` 完善启动脚本（HTTP+HTTPS 双端口、--status/--stop/--log、后台模式）
- feat: 转录完成后自动清理 chunks 临时目录
- fix: `transcribe_logic.py` 快速路径 `chunk_dir` 未定义的 NameError
- fix: `docker/Dockerfile` 移除不存在的 `video2text.service` COPY

### v0.5.0
- feat: 四阶段流水线集成到 FastAPI — 下载 → 预处理（WAV + 分片）→ ASR（GPU）→ 翻译
- feat: 队列页 4 列显示，实时阶段进度和具体步骤描述
- feat: 音频分片移到预处理阶段，转录阶段仅做 GPU 计算
- feat: 自动翻译由后端流水线处理（无需前端 JS 触发）
- feat: 自动下载 ZIP 到浏览器默认路径，无需确认弹窗
- feat: UI 偏好（自动翻译、自动下载、后端、语言、设备）持久化到 `.env`，刷新页面自动恢复
- fix: workspace 路径解析 — `src/workspace` 符号链接到共享 workspace 目录
- fix: faster-whisper GPU — `cuda:0` 规范化为 `cuda` 以兼容
- fix: 后端显示名称映射使用前缀匹配

### v0.4.4
- feat: 文件结构重组，路径优化，启动脚本修复
- fix: MCP server 参数名、文件读取、翻译轮询

### v0.4.3
- fix: 移除 Gradio 依赖，清理冗余代码

### v0.4.2
- fix: 修复页面刷新后自动翻译/自动下载标志丢失 — 从服务端 `auto_translate`/`auto_download` 字段恢复
- feat: SSE 替代 1 秒轮询 — 事件驱动 UI 更新，1% 进度门槛节流，失败自动降级轮询
- feat: 新增"直接保存"勾选项 — fetch+blob 方式下载到浏览器默认路径，无需弹窗确认

### v0.4.1
- fix: yt-dlp 兼容性修复 — 添加 `--remote-components ejs:github` 参数，自动检测 JS 运行时（deno/node/bun）
- fix: 纯文本输出改为按时间间隔分段合并，提升可读性
- fix: 将 mcp、httpx、faster-whisper、stable-ts 从可选依赖移入核心依赖

### v0.6.0
- refactor: 移除 `main.py`（2151 行）— 核心逻辑全部迁移到 `src/core/`
- refactor: 移除遗留 `backend/` 目录 — 统一使用 `src/backends/` 插件注册表
- refactor: `fastapi_app.py` 解除 `import main` 依赖，全部从 `src/core/` 直接导入
- refactor: 移除 `scripts/main.sh` — `run.sh` 为唯一启动脚本
- feat: `_do_transcribe_stream` 生成器迁移到 `transcribe_logic.py`，使用统一后端注册表
- fix: `pyproject.toml` 构建配置适配 `src/` 布局
- fix: Dockerfile 改用 `run.sh` 替代 `main.sh`
- chore: 从 git 追踪中移除 8MB 测试产物

### v0.5.1
- feat: VibeVoice ASR 后端（7B/9B 模型，说话人分离，4-bit/8-bit 量化，默认后端）
- feat: 插件化后端架构（`backends/` 目录，抽象基类 + 注册表模式）
- feat: `core/` 模块从 main.py 提取（config、workspace、transcribe_logic、pipeline）
- feat: Tesla P4 / Pascal GPU 兼容性补丁（PyTorch 版本欺骗、is_autocast_enabled 签名修复、nn.Module.set_submodule 回移植入）
- feat: 前端模型选择器支持 `::N` 量化后缀（如 `VibeVoice-ASR-7B::4`）
- refactor: 移除 Gradio UI，FastAPI 为唯一前端
- refactor: `backend/` → `backends/`，完整 ASR/翻译抽象

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
