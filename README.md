# video2text

Linux 本地 FastAPI WebUI，将视频/音频转为字幕与纯文本。支持 GPU 加速、说话人分离、在线视频下载、历史任务管理、HTTPS、手动翻译与打包下载。

## 主要功能

- **上传或下载**：直接上传本地视频/音频，或在 WebUI 粘贴视频 URL（由 yt-dlp 后台下载），下载完成后自动启动转录
- **双 ASR 后端**：`FunASR（Paraformer）` / `faster-whisper`，可在 WebUI 实时切换
- **说话人分离**：选择 `-spk` 模型（如 `paraformer-zh-spk`）时，自动以 `【角色N】` 分段输出对话脚本
- **分片流式进度**：提取音频 → 分片识别 → 汇总，进度统一为 `百分比 + 预计剩余 HH:MM:SS`
- **一键停止**：任务运行中可随时打断
- **多语言翻译**：完成转录后手动点击 `翻译`，调用在线模型（如硅基流动）生成译文字幕与纯文本，支持 zh/en/ja/ko/es/fr/de/ru
- **工作区管理**：`workspace/<任务名>/` 按视频标题/文件名命名，支持历史文件夹浏览与删除

## 环境要求

- Linux（推荐 Ubuntu 20.04 / 22.04 / 24.04）
- Python 3.12
- `ffmpeg`（由安装脚本自动安装）
- NVIDIA GPU（可选，CPU 亦可运行）
- `yt-dlp`（下载在线视频，由安装脚本自动安装）
- 可访问的 OpenAI 兼容 API（用于翻译，如硅基流动）

> ffmpeg 音频提取优先使用 NVIDIA 硬件加速（CUDA/NVDEC），失败自动回退 CPU。

## 快速安装

```bash
git clone <repo_url> /path/to/video2text
cd /path/to/video2text
bash install.sh
```

安装脚本会自动完成以下所有步骤：

| 步骤 | 内容 |
|------|------|
| 系统包 | `python3.12`、`ffmpeg`、`build-essential` 等（apt） |
| uv | Python 包管理器，若未安装则自动下载 |
| 虚拟环境 | `uv venv .venv --python 3.12` |
| Python 依赖 | `uv pip install -e .`（含 funasr、faster-whisper 等） |
| PyTorch | 按 GPU 算力自动选择版本（Pascal sm_61 → 2.3.1+cu121；Volta+ → 最新 cu124；无 GPU → CPU） |
| yt-dlp | 从 GitHub 下载最新二进制到 `~/.local/bin/yt-dlp` |
| workspace | 创建 `workspace/` 任务目录 |

### 可选步骤

```bash
# 同时生成 mkcert HTTPS 证书（局域网 HTTPS 访问）
SETUP_HTTPS=1 bash install.sh

# 同时注册并启动 systemd 服务
SETUP_SYSTEMD=1 bash install.sh

# 两者同时开启
SETUP_HTTPS=1 SETUP_SYSTEMD=1 bash install.sh
```

### 手动安装（如需精细控制）

```bash
# 1. 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 2. 创建虚拟环境
uv venv .venv --python 3.12

# 3. 安装 Python 依赖
uv pip install -e .

# 4. PyTorch：Tesla P4 / Pascal GPU（sm_61，最高兼容版）
uv pip install "torch==2.3.1+cu121" "torchaudio==2.3.1+cu121" \
    --index-url https://download.pytorch.org/whl/cu121
# PyTorch：Volta+ GPU
# uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124

# 5. yt-dlp
curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o ~/.local/bin/yt-dlp && chmod +x ~/.local/bin/yt-dlp
```

## 翻译配置

翻译功能需要一个兼容 OpenAI API 的在线模型服务（如硅基流动）。启动服务后，在 WebUI 的 **配置模型** 页面中，通过 `新建配置` 填写 `base_url`、`api_key` 并点击 `保存配置` 即可，无需手动编辑 `.env` 文件。

## 启动

```bash
cd /path/to/video2text
chmod +x main.sh

./main.sh auto   # 自动选择 HTTP/HTTPS
./main.sh http   # 强制 HTTP
./main.sh https  # 强制 HTTPS（需先生成证书）

# 或直接调 FastAPI
.venv/bin/python fastapi_app.py --host 0.0.0.0 --port 7881
```

默认端口：`7881`

## 使用流程

1. **开始转录**：上传文件或粘贴视频 URL（URL 下载后自动启动转录）
2. 系统生成原文文件：`*.orig.srt`、`*.orig.txt`（并保留兼容 `*.srt`、`*.txt`）
3. **翻译（可选）**：选择目标语言后点击 `翻译`，生成 `*.<lang>.srt`、`*.<lang>.txt`
4. 在"最终文件列表"中选择文件，点击 `下载此文件`

### 说话人分离

选择 `paraformer-zh-spk` 或 `paraformer-en-spk` 等 `-spk` 模型时，识别结果按说话人分组，纯文本以如下格式输出：

```
【角色1】（00:00:03）
你好，欢迎来到节目。

【角色2】（00:00:07）
谢谢邀请，很高兴来这里。
```

### 在线视频下载（yt-dlp）

主页左侧粘贴视频 URL（支持 YouTube、B 站等 yt-dlp 支持的平台），点击 `下载视频`，下载完成后自动启动转录。视频保存至 `workspace/<标题前20字符>/`。

系统优先使用 `~/.local/bin/yt-dlp`（由安装脚本写入），其次搜索 miniconda/conda 路径，最后回退系统 `yt-dlp`。若目标网站需要登录，请先在本机浏览器中登录，yt-dlp 会自动读取 cookie。

## 工作区结构

```
workspace/
├── 标题前20字符/          # 在线下载：取视频标题前 20 字符
│   ├── video.m4a          # 原始媒体文件
│   ├── video.orig.srt
│   ├── video.orig.txt
│   ├── video.zh.srt       # 翻译后（中文）
│   └── video.zh.txt
└── 文件名前20字符/        # 本地上传：取文件名前 20 字符
    └── ...
```

目录名仅去除 `/` 和空字节，保留中文、标点等所有字符，最多取前 20 字符。

## 页面结构

- 页面采用 PC 专用宽屏布局（`max-width: 1840px`），不做移动端适配
- 页面切换入口：顶部横向 tab 按钮，`主页`、`文件管理`、`配置模型`
- 三个页面统一采用左右等宽分区（6:6），避免单栏拥挤
- `主页` 左卡：`开始转录 / 停止 / 翻译` 操作按钮 + 上传/历史/URL 输入行 + 可拖拽重排的参数区
- `主页` 左卡：后端模型与翻译模型改为可搜索组合框（搜索 input + select 垂直堆叠，全宽）
- `主页` 右卡："识别文本"自动拉伸填满剩余垂直空间；"运行日志"固定在底部；顶部保留"当前任务"面板
- `主页`：后端模型候选随识别后端自动切换；`-spk` 模型显式标记 `角色识别`
- `主页`：翻译支持选择目标语言（zh/en/ja/ko/es/fr/de/ru）
- `文件管理`：左侧历史文件夹列表（搜索 + 删除），右侧历史 `.txt` 列表（搜索 + 下载）
- `配置模型`：左侧配置组列表（可滚动 select + 搜索过滤），右侧配置编辑与在线模型测试

## FastAPI 接口（核心）

- `GET /health` 健康检查
- `GET /api/history` 历史视频与任务目录
- `GET /api/folders/text-files` 获取指定任务目录的文本文件列表
- `GET /api/folders/download-text` 下载指定文本文件
- `POST /api/transcribe/start` 启动转写任务（支持直接上传文件或指定历史路径）
- `POST /api/jobs/{job_id}/stop` 停止转写任务
- `POST /api/jobs/{job_id}/translate` 启动翻译任务
- `GET /api/jobs/{job_id}` 轮询任务状态（含结构化字段：`progress_pct`、`eta_seconds`、`step_label`）
- `GET /api/jobs/{job_id}/download/{kind}` 下载打包结果
- `GET /api/jobs/{job_id}/files` 获取当前任务生成的最终文件列表
- `GET /api/jobs/{job_id}/download-file` 下载列表中选中的单个最终文件
- `POST /api/download_url` 使用 yt-dlp 下载在线视频
- `GET/POST /api/model/*` 在线模型配置管理
- `POST /api/model/profiles/fetch-models` 测试当前配置并返回可用模型列表

## 进度说明

- 后端状态接口提供结构化字段：`progress_pct`（0-100）、`eta_seconds`、`step_label`
- 前端任务面板直接渲染结构化字段，避免依赖状态字符串解析
- 兼容保留文本状态：`步骤｜总进度 xx%｜预计剩余 HH:MM:SS`

## HTTPS 证书

推荐 `mkcert`（`SETUP_HTTPS=1 bash install.sh` 已自动完成此步骤）：

```bash
sudo apt install -y libnss3-tools
curl -fsSL https://github.com/FiloSottile/mkcert/releases/latest/download/mkcert-linux-amd64 \
    -o /usr/local/bin/mkcert && chmod +x /usr/local/bin/mkcert
mkcert -install

cd /path/to/video2text
mkcert -cert-file video2text.pem -key-file video2text-key.pem 192.168.1.x localhost 127.0.0.1
./main.sh https
```

访问：`https://192.168.1.x:7881`

## systemd 服务

```bash
# 方式一：通过安装脚本（推荐，自动替换用户名与路径）
SETUP_SYSTEMD=1 bash install.sh

# 方式二：手动（注意服务文件中的路径与用户名需与实际一致）
sudo cp video2text.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now video2text
```

查看状态与日志：

```bash
systemctl status video2text --no-pager -l
journalctl -u video2text -f
```

## 常见命令

```bash
# 语法检查
.venv/bin/python -m py_compile main.py backend/funasr_backend.py backend/whisper_backend.py utils/audio.py utils/subtitle.py utils/translate.py

# 查看监听端口
ss -tlnp | grep 7881

# 重启服务
sudo systemctl restart video2text

# 更新 yt-dlp
curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o ~/.local/bin/yt-dlp && chmod +x ~/.local/bin/yt-dlp
```

## 维护约定

- 每次代码或配置变更后，默认执行服务重启：`sudo systemctl restart video2text`
- 每次功能调整后，默认同步更新文档（`README.md`、`design.md`）

## 详细设计

- [design.md](design.md)
