# video2text

video2text 是一个运行在 Linux 上的 FastAPI WebUI，用于把本地或在线视频/音频转换为字幕与纯文本，并支持可选翻译。

## 当前能力确认

是的，当前项目可以从局域网直接访问，并支持解析本地与在线音视频：

- 局域网访问：服务默认监听 `0.0.0.0`（见 `main.sh`），可通过 `http://<服务器IP>:7881` 或 `https://<服务器IP>:7881` 访问。
- 本地音视频：主页支持上传本地 `video/*, audio/*` 文件并转录。
- 在线音视频：支持在主页输入 URL，通过 `yt-dlp` 下载后自动转录；若平台有自动字幕可优先导入并跳过 ASR。

说明：若局域网无法访问，通常是服务器防火墙或路由策略未放行 `7881` 端口。

## 功能概览

- 双识别后端：`FunASR（Paraformer）`、`faster-whisper`
- URL 下载：`/api/download_url` + `yt-dlp`
- 自动字幕优先：可配置默认字幕优先项（`AUTO_SUBTITLE_LANG`）
- 任务进度：百分比、预计剩余时间、步骤状态
- 翻译功能：基于 OpenAI 兼容接口（配置模型页面维护）
- 历史管理：`workspace/` 任务目录浏览与文件下载

## 环境要求

- Linux（推荐 Ubuntu 20.04/22.04/24.04）
- Python 3.12
- ffmpeg
- 可选 NVIDIA GPU（无 GPU 也可运行）
- `yt-dlp`
- OpenAI 兼容翻译服务（可选）

## 一、本机安装与启动

### 1. 自动安装

```bash
git clone <repo_url> /path/to/video2text
cd /path/to/video2text
bash install.sh
```

### 2. 启动

```bash
cd /path/to/video2text
chmod +x main.sh

./main.sh auto
# 或
./main.sh http
./main.sh https
```

默认端口：`7881`

### 3. systemd 服务（可选）

```bash
sudo cp video2text.service /etc/systemd/system/video2text.service
sudo systemctl daemon-reload
sudo systemctl enable --now video2text
```

常用检查：

```bash
systemctl status --no-pager video2text.service
journalctl -u video2text -f
```

## 二、Docker 部署教程（安装教程）

当前仓库提供完整 Docker 方案：`Dockerfile` + `docker-compose.yml`。

### 1. 准备配置

```bash
cd /path/to/video2text
cp .env.example .env
```

按需修改 `.env`（至少确认端口和默认后端）。

### 2. 使用 docker compose 启动（推荐）

```bash
docker compose up -d --build
```

查看状态与日志：

```bash
docker compose ps
docker compose logs -f video2text
```

停止与清理：

```bash
docker compose down
```

### 3. 访问地址

- 本机：`http://127.0.0.1:7881`
- 局域网：`http://<服务器IP>:7881`

### 4. GPU 启用说明

在宿主机安装 NVIDIA 驱动和 NVIDIA Container Toolkit 后，可在 `docker-compose.yml` 中启用：

```yaml
# gpus: all
```

然后重建：

```bash
docker compose up -d --build
```

### 5. docker run（可选）

```bash
docker build -t video2text:cu121 .

docker run -d \
  --name video2text \
  -p 7881:7881 \
  -e HOST=0.0.0.0 \
  -e PORT=7881 \
  -e SSL_MODE=http \
  -v $(pwd)/workspace:/app/workspace \
  -v $(pwd)/.env:/app/.env \
  video2text:cu121
```

### 6. 发布到 Docker Hub（可选）

```bash
# 1) 登录（首次）
docker login

# 2) 构建与打标签
docker build -t <dockerhub_user>/video2text:cu121 .
docker tag <dockerhub_user>/video2text:cu121 <dockerhub_user>/video2text:latest

# 3) 推送
docker push <dockerhub_user>/video2text:cu121
docker push <dockerhub_user>/video2text:latest
```

## 三、配置说明（.env）

核心配置项：

- `APP_PORT`：服务端口（默认 `7881`）
- `BROWSER_DEBUG_PORT`：Win11 浏览器调试端口（默认 `9222`）
- `DEFAULT_BACKEND`：默认识别后端
- `DEFAULT_FUNASR_MODEL`：默认 FunASR 模型
- `DEFAULT_WHISPER_MODEL`：默认 Whisper 模型
- `AUTO_SUBTITLE_LANG`：默认字幕优先项（`zh`/`none`/`en`/`ja`/`ko`/`es`/`fr`/`de`/`ru`/`pt`/`ar`/`hi`）
- `ONLINE_MODEL_*`：在线模型配置组

安全建议：`.env` 含密钥，禁止提交到仓库。

## 四、简单使用教程

### 场景 A：本地文件转字幕

1. 打开主页，点击 `上传视频/音频` 选择本地文件。
2. 选择识别后端与后端模型。
3. 点击 `开始转录`。
4. 等待任务完成，查看 `识别文本` 和 `运行日志`。
5. 下载 `*.srt` 与 `*.txt`（或 ZIP）。

### 场景 B：在线视频转字幕

1. 在 `视频URL` 输入链接。
2. 点击 `下载视频`。
3. 若检测到平台自动字幕，会优先导入并跳过 ASR。
4. 未命中自动字幕时，自动进入语音识别流程。
5. 完成后下载结果文件。

### 场景 C：翻译字幕

1. 在 `配置模型` 页面配置可用翻译模型（base_url/api_key）。
2. 回到主页，选择目标语言。
3. 点击 `翻译`。
4. 下载 `*.<lang>.srt` 与 `*.<lang>.txt`。

## 五、第三方 HTTP 调用（统一 API）

已提供统一 FastAPI 接口：`POST /api/external/process`

支持输入：

- `source_type=base64`：传本地音视频 base64
- `source_type=url`：传在线视频 URL
- `source_type=history`：传历史媒体相对路径

支持参数：

- 识别：`backend`、`funasr_model`、`whisper_model`、`device`、`language`
- 字幕优先：`auto_subtitle_lang`（`zh`/`none`/`en`/`ja`/`ko`/`es`/`fr`/`de`/`ru`/`pt`/`ar`/`hi`）
- 翻译：`target_lang`（传 `none` 表示不翻译）、`online_profile`、`online_model`
- 输出：`output_mode`（`binary`/`base64`/`json`）
- 强制识别：`force_asr`（`true` 时即使 URL 有自动字幕也走 ASR）

返回输出：

- `output_mode=binary`：直接返回 `application/zip`
- `output_mode=base64`：返回 JSON，含 `zip_base64`
- `output_mode=json`：返回 JSON 元数据和 `download_path`

### 1. 解析 URL 并直接返回 ZIP（二进制）

```bash
curl -X POST "http://127.0.0.1:7881/api/external/process" \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "url",
    "url": "https://www.youtube.com/watch?v=xxxx",
    "backend": "FunASR（Paraformer）",
    "funasr_model": "paraformer-zh ⭐ 普通话精度推荐",
    "auto_subtitle_lang": "zh",
    "target_lang": "zh",
    "output_mode": "binary"
  }' \
  --output result.zip
```

### 2. 解析 base64 本地媒体并返回 ZIP(base64)

```bash
curl -X POST "http://127.0.0.1:7881/api/external/process" \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "base64",
    "filename": "demo.mp4",
    "media_base64": "<BASE64_STRING>",
    "backend": "faster-whisper（多语言）",
    "whisper_model": "medium",
    "auto_subtitle_lang": "none",
    "target_lang": "none",
    "output_mode": "base64"
  }'
```

## 六、目录结构

```text
video2text/
├── fastapi_app.py
├── main.py
├── main.sh
├── docker-compose.yml
├── Dockerfile
├── video2text.service
├── backend/
├── utils/
└── workspace/
```

## 七、常见问题

- 无法局域网访问：检查防火墙、云安全组、路由 ACL，确认 `7881` 已放行。
- URL 下载失败：确认 `yt-dlp` 可用且网络可达目标站点。
- GPU 未生效：检查宿主机 `nvidia-smi` 和容器 GPU 暴露配置。
- 翻译失败：检查 `base_url/api_key`、模型名和外网连通性。
