# video2text

本项目是一个 Linux 本地 WebUI，用于将视频/音频转为字幕与纯文本，支持 GPU 加速、历史任务管理、HTTPS、手动翻译与打包下载。

## 主要功能

- 上传新文件或复用历史文件
- 双 ASR 后端：`FunASR` / `faster-whisper`
- 分片流式进度（提取音频、分片、识别、汇总）
- 转写/翻译进度统一为：`百分比 + 预计剩余 HH:MM:SS`
- 音频分片策略：每片 120 秒，相邻分片 10 秒重叠覆盖
- 一键停止当前转录
- 转录阶段仅生成原文字幕与原文纯文本
- 手动点击 `翻译` 按钮后生成中文译文字幕与中文译文纯文本
- `下载SRT字幕`、`下载纯文本` 按钮点击后自动下载 zip（含原文+译文）

## 环境要求

- Linux（推荐 Ubuntu）
- Python 3.12
- `ffmpeg` / `ffprobe`
- NVIDIA GPU（可选）
- Ollama（用于本地翻译，默认 `qwen3.5:4b`）

> ffmpeg 音频提取默认优先尝试 NVIDIA 硬件加速（CUDA/NVDEC），失败自动回退 CPU。

## 快速安装

```bash
cd /home/lym/projects/video2text

python3 -m venv .venv
source .venv/bin/activate

pip install -U pip
pip install -e .

# Tesla P4 / sm_61 推荐
pip install "torch==2.3.1+cu121" "torchaudio==2.3.1+cu121" --index-url https://download.pytorch.org/whl/cu121
```

## Ollama 翻译模型

项目默认使用：`qwen3.5:4b`

```bash
ollama pull qwen3.5:4b
```

若拉取报“需要更新 Ollama”，先升级 Ollama 再拉取。

## 启动

```bash
cd /home/lym/projects/video2text
chmod +x main.sh

./main.sh auto
./main.sh http
./main.sh https
```

默认端口：`7881`

## 使用流程（当前实现）

1. 点击 `开始转录`
2. 系统生成原文文件：`*.orig.srt`、`*.orig.txt`（并保留兼容 `*.srt`、`*.txt`）
3. 点击 `翻译`
4. 系统生成译文文件：`*.zh.srt`、`*.zh.txt`
5. 点击 `下载SRT字幕` 或 `下载纯文本`
6. 自动下载 zip，内含原文+译文对应文件

## 进度说明

- 转写状态：`转写进度：xx%｜预计剩余 HH:MM:SS`
- 翻译状态：`翻译进度：xx%｜预计剩余 HH:MM:SS`
- 剩余时间只显示时分秒，不显示毫秒

## HTTPS 证书（简版）

推荐 `mkcert`：

```bash
sudo apt update
sudo apt install -y libnss3-tools mkcert
mkcert -install

cd /home/lym/projects/video2text
mkcert -cert-file video2text.pem -key-file video2text-key.pem 192.168.1.2 localhost 127.0.0.1
./main.sh https
```

访问：`https://192.168.1.2:7881`

## systemd

```bash
sudo cp /home/lym/projects/video2text/video2text.service /etc/systemd/system/video2text.service
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
```

## 详细设计

- [design.md](design.md)
