# video2text

本项目是一个 Linux 本地 FastAPI WebUI，用于将视频/音频转为字幕与纯文本，支持 GPU 加速、历史任务管理、HTTPS、手动翻译与打包下载。

## 主要功能

- 上传新文件或复用历史文件
- 双 ASR 后端：`FunASR` / `faster-whisper`
- 分片流式进度（提取音频、分片、识别、汇总）
- 转写/翻译进度统一为：`百分比 + 预计剩余 HH:MM:SS`
- 音频分片策略：每片 120 秒，相邻分片 10 秒重叠覆盖
- 一键停止当前转录
- 转录阶段仅生成原文字幕与原文纯文本
- 手动点击 `翻译` 按钮后，调用硅基流动模型生成中文译文字幕与中文译文纯文本
- `下载SRT字幕`、`下载纯文本` 按钮点击后自动下载 zip（含原文+译文）

## 环境要求

- Linux（推荐 Ubuntu）
- Python 3.12
- `ffmpeg` / `ffprobe`
- NVIDIA GPU（可选）
- 可访问硅基流动 API（用于翻译）

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

## 翻译配置

翻译功能需要一个兼容 OpenAI API 的在线模型服务（如硅基流动）。启动服务后，在 WebUI 的 **配置模型** 页面中，通过 `新建配置` 填写 `base_url`、`api_key` 并点击 `保存配置` 即可，无需手动编辑 `.env` 文件。

## 启动

```bash
cd /home/lym/projects/video2text
chmod +x main.sh

./main.sh auto
./main.sh http
./main.sh https

# 或直接启动 FastAPI
.venv/bin/python fastapi_app.py --host 0.0.0.0 --port 7881
```

默认端口：`7881`

## 使用流程（当前实现）

1. 点击 `开始转录`
2. 系统生成原文文件：`*.orig.srt`、`*.orig.txt`（并保留兼容 `*.srt`、`*.txt`）
3. 点击 `翻译`
4. 选择目标语言（`zh/en/ja/ko/es/fr/de/ru`）后生成译文文件：`*.<lang>.srt`、`*.<lang>.txt`
5. 在“最终文件列表”中选择目标文件
6. 点击 `下载此文件` 单文件下载（保留旧 zip 接口兼容）

## 页面结构

- 页面采用 PC 专用宽屏布局（`max-width: 1840px`），不做移动端适配
- 页面切换入口：顶部横向 tab 按钮，`主页`、`文件管理`、`配置模型`
- 三个页面统一采用左右等宽分区（6:6），避免单栏拥挤
- 所有 label / button / 提示文字均设置 `white-space:nowrap`，禁止换行截断
- `主页` 左卡：第一行为 `开始转录 / 停止 / 翻译` 操作按钮；其余内容采用拖拽行布局（drag-zone > drag-row > drag-item，各行元素可跨行拖拽重排）
- `主页` 左卡：后端模型与翻译模型改为上下排版的可搜索组合框（搜索 input + select 垂直堆叠，全宽）
- `主页` 右卡："识别文本"自动拉伸填满剩余垂直空间；"运行日志"固定在底部；顶部保留"当前任务"面板
- `主页`：最终文件统一展示在单独列表框中，选择后点击"下载此文件"
- `主页`：新增 `识别后端` 与 `后端模型`，后端模型候选会随识别后端自动切换；带说话人能力的模型显式标记 `角色识别`
- `主页`：翻译模型支持搜索与选中态提示，翻译时优先使用主页选中的模型
- `主页`：翻译支持选择目标语言（zh/en/ja/ko/es/fr/de/ru），按目标语言生成对应译文文件
- `文件管理`：左侧历史文件夹列表（搜索 + 删除），右侧历史 `.txt` 列表（搜索 + 下载）
- `配置模型`：左侧配置组列表改为可滚动 `<select size=10>`，每行一个配置项；搜索框实时过滤
- `配置模型`：在线模型配置第一行新增 `新建配置` 按钮（清空表单、光标定位到配置名）
- `配置模型`：支持"测试当前配置"，失败显示具体错误原因，成功显示可用模型列表并可设置默认模型
- `FunASR 模型` 与 `Whisper 模型` 均为下拉列表选择；FunASR 覆盖中文/多语言/流式/长语音场景；选 `-spk` 模型时输出自动添加角色标记
- 输出文本自动去除冗余标点（如 `！！！`、`。。。。`）
- 在线模型列表按名称排序显示，支持关键字搜索筛选
- 模型/配置/历史文件点击选中后持续高亮，并在页面内显示"当前已选"提示文本

## FastAPI 接口（核心）

- `GET /health` 健康检查
- `GET /api/history` 历史视频与任务目录
- `GET /api/folders/text-files` 获取指定任务目录的文本文件列表
- `GET /api/folders/download-text` 下载指定文本文件
- `POST /api/transcribe/start` 启动转写任务
- `POST /api/jobs/{job_id}/stop` 停止转写任务
- `POST /api/jobs/{job_id}/translate` 启动翻译任务
- `GET /api/jobs/{job_id}` 轮询任务状态（含结构化字段：`progress_pct`、`eta_seconds`、`step_label`）
- `GET /api/jobs/{job_id}/download/{kind}` 下载打包结果
- `GET /api/jobs/{job_id}/files` 获取当前任务生成的最终文件列表
- `GET /api/jobs/{job_id}/download-file` 下载列表中选中的单个最终文件
- `GET/POST /api/model/*` 在线模型配置管理
- `POST /api/model/profiles/fetch-models` 测试当前配置并返回可用模型列表

## 进度说明

- 后端状态接口提供结构化字段：`progress_pct`（0-100）、`eta_seconds`、`step_label`
- 前端任务面板直接渲染结构化字段，避免依赖状态字符串解析
- 兼容保留文本状态：`步骤｜总进度 xx%｜预计剩余 HH:MM:SS`

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

## 维护约定

- 每次代码或配置变更后，默认执行服务重启以确保新设置生效：`sudo systemctl restart video2text`
- 每次功能调整后，默认同步更新文档（`README.md`、`design.md`）

## 详细设计

- [design.md](design.md)
