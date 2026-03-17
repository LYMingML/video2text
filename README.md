# video2text

视频/音频转字幕工具，支持本地文件和在线 URL，基于 FunASR（Paraformer）或 faster-whisper 双后端。

**版本**: v0.2.3

## 功能特性

- **双识别后端**：FunASR（Paraformer，中文精度高）/ faster-whisper（多语言）
- **URL 下载**：通过 yt-dlp 下载在线视频并自动转录
- **自动字幕优先**：若平台有自动字幕可优先导入跳过 ASR
- **翻译功能**：基于 OpenAI 兼容接口
- **GPU 加速**：支持 NVIDIA GPU（Pascal 及以上）、Intel GPU（仅 FunASR）
- **外部 API**：提供 `/api/external/process` 统一接口
- **智能去重**：上传相同内容的视频/音频自动复用缓存，节省存储空间

## 快速开始

### 方式一：Docker 部署（推荐）

**前置条件**：已安装 Docker

```bash
# 下载项目
git clone <repo_url> /path/to/video2text
cd /path/to/video2text

# CPU 版（默认）
bash docker-install.sh cpu

# GPU 版（需要 NVIDIA GPU + Container Toolkit）
bash docker-install.sh gpu

# 或本地构建
bash docker-install.sh build cpu
bash docker-install.sh build gpu
```

安装完成后按提示命令启动服务，访问 `http://<IP>:7881`

### 方式二：本机安装

**前置条件**：Linux（Ubuntu 20.04/22.04/24.04）、Python 3.12、ffmpeg

```bash
# 克隆并安装
git clone <repo_url> /path/to/video2text
cd /path/to/video2text
bash install.sh

# 启动
./main.sh auto   # 自动选择 http/https
./main.sh http   # 强制 HTTP
./main.sh https  # 强制 HTTPS
```

**可选**：注册为 systemd 服务

```bash
SETUP_SYSTEMD=1 bash install.sh
sudo systemctl status video2text
```

## 镜像说明

**预置模型**（镜像已包含，首次启动即可使用）：
- `paraformer-zh` - FunASR 中文 Paraformer（推荐中文识别）
- `iic/SenseVoiceSmall` - SenseVoice 小模型
- `faster-whisper small` - Whisper small 模型

**按需下载**（首次使用时自动下载）：
- 其他 FunASR 模型（paraformer-en、large 等）
- 其他 Whisper 模型（medium、large-v3 等）

**镜像体积**：
- CPU 版：约 2.3 GB（含预置模型约 1 GB）
- GPU 版：约 3.5 GB（含预置模型约 1 GB）

**模型缓存**：
- 通过 volume 持久化，重启容器无需重新下载
- FunASR 模型 → `~/.cache/modelscope`
- Whisper 模型 → `~/.cache/huggingface`

## Docker 安装教程

### 前置条件

安装 Docker 和 Docker Compose：

```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# 重新登录以生效

# 验证
docker --version
docker compose version
```

### GPU 版额外要求

如需 GPU 加速，需安装 NVIDIA Container Toolkit：

```bash
# 添加 NVIDIA 仓库
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# 安装
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 验证
sudo docker run --rm --gpus all nvidia/cuda:12.1-base nvidia-smi
```

## Docker 代理设置（Proxy）

国内网络环境下，建议配置代理以加速镜像拉取和模型下载。

### 方式一：Docker Daemon 代理（推荐）

配置 Docker 守护进程使用代理，对所有 `docker pull` 生效：

```bash
# 创建 systemd 配置目录
sudo mkdir -p /etc/systemd/system/docker.service.d

# 写入代理配置（替换为你的代理地址）
sudo tee /etc/systemd/system/docker.service.d/proxy.conf << 'EOF'
[Service]
Environment="HTTP_PROXY=http://127.0.0.1:7890"
Environment="HTTPS_PROXY=http://127.0.0.1:7890"
Environment="NO_PROXY=localhost,127.0.0.1,*.cn,mirrors.tuna.tsinghua.edu.cn"
EOF

# 重载并重启
sudo systemctl daemon-reload
sudo systemctl restart docker

# 验证
sudo systemctl show --property=Environment docker
```

### 方式二：Docker Build 代理

构建镜像时指定代理参数：

```bash
# 构建时传入代理
docker build \
  --build-arg HTTP_PROXY=http://127.0.0.1:7890 \
  --build-arg HTTPS_PROXY=http://127.0.0.1:7890 \
  -f Dockerfile -t video2text:cu121 .
```

或修改 Dockerfile 添加 ARG（可选）：

```dockerfile
ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV http_proxy=$HTTP_PROXY \
    https_proxy=$HTTPS_PROXY
```

### 方式三：Docker Compose 代理

在 `docker-compose.yml` 中为容器配置代理环境变量：

```yaml
services:
  video2text-gpu:
    # ...
    environment:
      - HTTP_PROXY=http://host.docker.internal:7890
      - HTTPS_PROXY=http://host.docker.internal:7890
      - NO_PROXY=localhost,127.0.0.1
```

> **注意**：`host.docker.internal` 在 Linux 上需要添加 `--add-host` 参数，或使用宿主机 IP。

### 方式四：运行时代理

`docker run` 时通过 `-e` 参数指定：

```bash
docker run -d \
  --name video2text-gpu \
  --gpus all \
  -e HTTP_PROXY=http://172.17.0.1:7890 \
  -e HTTPS_PROXY=http://172.17.0.1:7890 \
  -e NO_PROXY=localhost,127.0.0.1,*.cn \
  ... \
  video2text:cu121
```

> `172.17.0.1` 是 Docker 默认网桥的网关 IP，可通过 `docker network inspect bridge` 查看。

### 常用代理配置汇总

| 场景 | 配置方式 |
|------|----------|
| 拉取镜像 | Docker Daemon 代理 |
| 构建镜像 | `--build-arg` 或 Dockerfile ARG |
| 容器内下载模型 | 容器环境变量 `HTTP_PROXY` |
| 全局生效 | Docker Daemon 代理 + 容器环境变量 |

## Docker 详细说明

### docker compose 启动

```bash
cd /path/to/video2text
cp .env.example .env
# 编辑 .env 配置

# CPU 版
docker compose --profile cpu up -d --build

# GPU 版
docker compose --profile gpu up -d --build
```

### docker run 启动

**CPU 版**：

```bash
docker build -f Dockerfile.cpu -t video2text:cpu .

docker run -d \
  --name video2text-cpu \
  --restart unless-stopped \
  -p 7881:7881 \
  -v $(pwd)/workspace:/app/workspace \
  -v $(pwd)/.env:/app/.env \
  video2text:cpu
```

**GPU 版**：

```bash
docker build -f Dockerfile -t video2text:cu121 .

docker run -d \
  --name video2text-gpu \
  --gpus all \
  --restart unless-stopped \
  -p 7881:7881 \
  -v $(pwd)/workspace:/app/workspace \
  -v $(pwd)/.env:/app/.env \
  video2text:cu121
```

### Docker 清理

如不再使用 Docker 部署，可删除容器和镜像：

```bash
# 停止并删除容器
docker rm -f video2text-gpu video2text-cpu

# 删除镜像
docker rmi video2text:cu121 video2text:cpu

# 清理未使用的资源（可选）
docker system prune -f
```

## 硬件加速

### NVIDIA GPU

- 架构要求：Pascal 及以上（compute capability >= 6.0）
- 宿主机需安装：NVIDIA 驱动 + NVIDIA Container Toolkit

检查命令：

```bash
nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader
```

### Intel GPU（仅 FunASR 后端）

FunASR 支持 Intel 核显/Arc 显卡加速，faster-whisper 不支持。

**本机安装配置**：

```bash
# 1. 安装 Intel oneAPI Base Toolkit
wget https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB
sudo apt-key add GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB
echo "deb https://apt.repos.intel.com/oneapi all main" | sudo tee /etc/apt/sources.list.d/oneAPI.list
sudo apt update && sudo apt install -y intel-oneapi-base-toolkit

# 2. 安装 Intel Extension for PyTorch
source /opt/intel/oneapi/setvars.sh
pip install intel-extension-for-pytorch

# 3. 在 .env 中启用
echo "PREFER_INTEL_GPU=1" >> .env
```

**验证**：

```bash
python3 -c "import torch; print('XPU available:', torch.xpu.is_available())"
```

**FFmpeg 硬件加速**：

FFmpeg 自动检测 Intel QSV（需 `/dev/dri/renderD128` 存在），优先级：
1. Intel QSV
2. NVIDIA CUDA
3. CPU 回退

## 配置说明

核心配置项（`.env` 文件）：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `APP_PORT` | 服务端口 | `7881` |
| `DEFAULT_BACKEND` | 默认识别后端 | `FunASR（Paraformer）` |
| `DEFAULT_FUNASR_MODEL` | FunASR 模型 | `paraformer-zh` |
| `DEFAULT_WHISPER_MODEL` | Whisper 模型 | `medium` |
| `AUTO_SUBTITLE_LANG` | 字幕优先语言 | `zh` |
| `FFMPEG_THREADS` | FFmpeg 线程数 | `4` |
| `FUNASR_BATCH_SIZE_S` | FunASR 批处理秒数 | `300` |
| `PREFER_INTEL_GPU` | 优先使用 Intel GPU | `0` |
| `ONLINE_MODEL_*` | 翻译模型配置 | - |

> 安全建议：`.env` 含密钥，禁止提交到版本控制。

## 使用教程

### 场景 A：本地文件转字幕

1. 打开主页，上传视频/音频文件
2. 选择识别后端与模型
3. 点击「开始转录」
4. 下载 `.srt` / `.txt` 文件

### 场景 B：在线视频转字幕

1. 在「视频URL」输入链接
2. 点击「下载视频」
3. 自动检测字幕或进入语音识别
4. 下载结果

### 场景 C：翻译字幕

1. 在「配置模型」页面配置翻译 API
2. 回到主页选择目标语言
3. 点击「翻译」
4. 下载翻译后的字幕

## 外部 API

### POST /api/external/process

支持输入类型：
- `source_type=base64`：本地音视频 base64
- `source_type=url`：在线视频 URL
- `source_type=history`：历史媒体路径

示例（URL 转 ZIP）：

```bash
curl -X POST "http://127.0.0.1:7881/api/external/process" \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "url",
    "url": "https://www.youtube.com/watch?v=xxxx",
    "backend": "FunASR（Paraformer）",
    "auto_subtitle_lang": "zh",
    "output_mode": "binary"
  }' \
  --output result.zip
```

## 目录结构

```
video2text/
├── Dockerfile              # GPU 版（cu121）
├── Dockerfile.cpu          # CPU 版
├── docker-compose.yml      # Docker Compose 配置
├── docker-install.sh       # Docker 一键安装脚本
├── docker-entrypoint.sh    # Docker 入口脚本
├── install.sh              # 本机安装脚本
├── pyproject.toml          # Python 项目配置
├── main.py / main.sh       # 启动入口
├── fastapi_app.py          # FastAPI 应用
├── backend/
│   ├── funasr_backend.py   # FunASR 后端
│   └── whisper_backend.py  # faster-whisper 后端
├── utils/
│   ├── audio.py            # 音频处理
│   ├── subtitle.py         # 字幕格式化
│   └── online_models.py    # 在线模型配置
├── scripts/
│   └── download_models.py  # 模型预下载脚本
└── workspace/              # 任务输出目录
```

## 常见问题

| 问题 | 解决方案 |
|------|----------|
| 局域网无法访问 | 检查防火墙/云安全组是否放行 7881 端口 |
| URL 下载失败 | 确认 yt-dlp 可用且网络可达目标站点 |
| GPU 未生效 | 检查 `nvidia-smi` 和容器 GPU 配置 |
| Intel GPU 未生效 | 确认 oneAPI 已安装，检查 `torch.xpu.is_available()` |
| 翻译失败 | 检查 API base_url/api_key 和网络连通性 |
| 模型下载慢 | 使用国内镜像或配置代理 |

## 更新日志

### v0.2.3
- 📖 文档完善：添加 Docker 安装教程和代理配置指南
- 📖 添加 NVIDIA Container Toolkit 安装步骤
- 📖 添加 Docker 清理命令说明

### v0.2.2
- ✨ 智能文件去重：上传相同内容的视频/音频自动复用缓存
  - 基于文件大小 + 文件头50字节 + 文件尾50字节判断
  - 使用 SQLite 存储文件指纹，不依赖文件名
- 🔧 Docker 拆分双镜像：CPU 版与 GPU 版独立构建

### v0.2.1
- 🔧 GPU 检测修复：修复 `CUDA_VISIBLE_DEVICES` 未设置时无法检测 GPU
- 🔧 URL 下载修复：修复下载视频后历史列表选择失败问题
- 🔧 systemd 配置修复：修正 `video2text.service` 配置
- ✨ Docker 优化：多阶段构建，预置核心模型
- ✨ 新增 `docker-install.sh` 一键安装脚本

## 许可证

MIT
