# video2text 设计文档（Linux）

## 1. 目标

项目提供一个可在局域网稳定运行的视频转字幕 WebUI，支持：

- 双 ASR 后端（FunASR / faster-whisper）
- 流式进度与可中断任务
- HTTPS 访问
- 原文与译文分离产出
- 一键下载原文+译文打包文件

默认端口：`7881`

## 2. 系统架构

核心模块：

- `fastapi_app.py`
   - FastAPI 路由与页面
   - 任务状态管理与轮询接口
   - 转写/翻译/下载 API
- `main.py`
   - 复用核心转写流程函数（无 UI 入口）
- `backend/funasr_backend.py`
   - FunASR 推理封装与模型缓存
- `backend/whisper_backend.py`
   - faster-whisper 推理封装
- `utils/audio.py`
   - ffmpeg 提取音频、分片
- `utils/subtitle.py`
   - 字幕清洗、时间轴归一化、SRT/TXT 序列化
- `utils/translate.py`
   - 翻译入口（硅基流动 API）
- `main.sh`
   - 启动与旧进程清理
- `video2text.service`
   - systemd 服务定义

UI 结构（当前，FastAPI 页面）：

- 视觉方向：文档工作台风（柔和背景、卡片分区、轻量动效）
- 页面导航：顶部横向 tab 按钮，`主页`、`文件管理`、`配置模型`（PC 专用，无移动端适配）
- 页面宽度：`max-width: 1840px`，适配大屏工作台
- 三页内容区统一左右等宽分区（6:6），避免单栏拥挤
- 所有 label / button / 提示文字均禁止文本换行
- 主页左卡第一行：`开始转录 / 停止 / 翻译` 操作按钮独占一行
- 主页左卡：采用 drag-zone 拖拽行布局（drag-zone > drag-row > drag-item），各行元素可跨行拖拽重排
- 主页左卡：后端模型与翻译模型改为上下排版的可搜索组合框（搜索 input + select 垂直堆叠，全宽）
- 主页右卡："识别文本"自动拉伸填满右卡剩余垂直空间；"运行日志"固定在底部
- 主页面：右侧输出区顶部保留"当前任务"面板（任务目录、当前步骤、进度、剩余时间、进度条）
- 主页面：最终文件通过单列表框展示，用户选择目标文件后点击下载
- 主页面：识别后端与后端模型随后端自动更新候选并支持搜索
- 主页面：后端模型使用小字号显示"模型名 + 特性"，说话人模型显式标记"角色识别"
- 文件管理页面：左右各自搜索+滚动列表（历史文件夹 / 历史文本文件）
- 配置模型页面：左侧配置组列表改为可滚动 `<select size=10>`，每行一个配置名；搜索框实时过滤
- 配置模型页面：右侧在线模型配置管理，第一行新增 `新建配置` 按钮（清空表单）
- 识别参数中 `FunASR 模型` 与 `Whisper 模型` 使用 `select` 下拉，FunASR 预置扩展模型列表
- 选择 `-spk` 角色模型时输出添加角色标记；字幕文本归一化增加冗余标点清理
- 主页翻译入口支持目标语言选择，翻译任务按目标语言输出对应字幕/文本
- 模型、配置组、历史文件夹与历史文本均提供持续选中视觉效果与"当前已选"提示

API 结构（当前）：

- `/api/history` 历史数据
- `/api/folders/text-files` 与 `/api/folders/download-text` 历史文本下载
- `/api/transcribe/start` / `/api/jobs/{id}` / `/api/jobs/{id}/stop`
- `/api/jobs/{id}/translate` / `/api/jobs/{id}/download/{kind}`
- `/api/jobs/{id}` 额外提供结构化进度字段：`progress_pct`、`eta_seconds`、`step_label`、`updated_at`
- `/api/model/profiles*` 在线模型配置 CRUD

## 3. 业务流程

### 3.1 转录（自动）

1. 上传文件 **或** 粘贴视频 URL（yt-dlp 后台下载后自动启动转录）
2. ffmpeg 提取 WAV
3. 按时长分片并识别
4. 生成并保存原文字幕/文本

当前分片参数：

- 分片长度：120 秒
- 分片重叠：10 秒（用于跨片段上下文连续性）
- 汇总时对重叠区做去重，避免重复字幕

输出文件：

- `<prefix>.orig.srt`
- `<prefix>.orig.txt`
- 兼容保留：`<prefix>.srt`、`<prefix>.txt`

### 3.2 在线视频下载（yt-dlp）

`/api/download_url` 端点接收视频 URL，两步完成下载：

1. **获取标题**：`yt-dlp --simulate --print title <url>` — 快速，不下载媒体，仅返回视频标题
2. **直接下载**：`yt-dlp -o <title_20>/<title>.%(ext)s <url>` — 直接写入最终目录，无临时目录

工作区目录名 = `re.sub(r'[/\x00]', '', title).strip()[:20] or "media"`。

Cookie 策略：依次尝试 chrome → chromium → firefox → edge 读取浏览器 cookie，全部失败则以无 cookie 方式重试（适用于公开视频）。

yt-dlp 查找顺序（`_find_ytdlp()`）：
1. `sys.executable` 同级目录（venv 内）
2. `~/.local/bin/yt-dlp`
3. `~/miniconda3/bin/yt-dlp`
4. `~/anaconda3/bin/yt-dlp`
5. `shutil.which("yt-dlp")`（系统 PATH）

### 3.3 说话人分离

选择 `-spk` 后缀模型（如 `paraformer-zh-spk`）时，FunASR 返回带角色标记的 segments（`角色N:` 前缀）。`utils/subtitle.py` 的 `save_plain()` 检测到说话人 segments 后，调用 `format_speaker_script()` 以如下格式输出：

```
【角色1】（00:00:03）
你好，欢迎来到节目。

【角色2】（00:00:07）
谢谢邀请，很高兴来这里。
```

SRT 保留原始时间轴与角色前缀，纯文本按角色分组。MODEL-AUTO 推理不会将 `-spk` 模型替换为其他模型。

### 3.4 工作区目录命名

| 场景 | 目录名规则 |
|------|-----------|
| 本地上传 | `re.sub(r'[/\x00]', '', filename_stem).strip()[:20]` |
| URL 下载 | `re.sub(r'[/\x00]', '', title).strip()[:20]` |

仅去除 `/` 和空字节，保留中文、标点等全部字符，最多取前 20 字符，空则回退 `"media"` / `"upload"`。

`_run_transcribe_worker` 中，若文件已在 `workspace/` 子目录内，直接复用 `p.parent` 作为 `job_dir`，不再调用 `_make_job_dir`，避免因 slug 规则差异生成冗余目录。

### 3.5 翻译（手动）

1. 点击 `翻译` 按钮
2. 读取原文 SRT
3. 使用硅基流动 API 流式输出翻译并显示进度
4. 按目标语言生成译文字幕/文本

输出文件：

- `<prefix>.<target_lang>.srt`
- `<prefix>.<target_lang>.txt`

### 3.3 下载（手动）

- 在“最终文件列表”中选中文件后点击 `下载此文件`，按单文件下载
- 兼容保留原有 zip 下载接口，便于旧流程回退

## 4. 翻译策略

默认配置（`.env`）：

- `ONLINE_MODEL_ACTIVE_PROFILE=default`
- `ONLINE_MODEL_PROFILE_COUNT=1`
- `ONLINE_MODEL_PROFILE_1_NAME=default`
- `ONLINE_MODEL_PROFILE_1_BASE_URL=https://api.siliconflow.cn/v1`
- `ONLINE_MODEL_PROFILE_1_API_KEY=<你的key>`
- `ONLINE_MODEL_PROFILE_1_DEFAULT_MODEL=Kimi-K2.5`
- `ONLINE_MODEL_PROFILE_1_MODEL_LIST_JSON=["Kimi-K2.5"]`

说明：

- 翻译按钮触发时按行调用硅基流动流式接口
- 自动累计翻译进度并输出 ETA
- API 失败时会在状态栏提示错误，不影响已生成原文文件

## 5. 运行与运维

### 5.1 脚本启动

```bash
cd /home/lym/projects/video2text
./main.sh auto
```

模式：

- `./main.sh auto`
- `./main.sh http`
- `./main.sh https`

### 5.2 systemd 启动

```bash
sudo cp /home/lym/projects/video2text/video2text.service /etc/systemd/system/video2text.service
sudo systemctl daemon-reload
sudo systemctl enable --now video2text
```

### 5.3 常用检查

```bash
systemctl status video2text --no-pager -l
journalctl -u video2text -f
ss -tlnp | grep 7881
```

### 5.4 变更后执行约定

- 每次代码或配置修改后，默认重启服务以使新设置生效：`sudo systemctl restart video2text`
- 每次功能变更后，默认同步更新 `README.md` 与 `design.md`

## 6. 进度展示规则

- 转写与翻译都采用：`百分比 + 预计剩余 HH:MM:SS`
- 剩余时间精度仅到秒

## 7. ffmpeg 加速策略

- 音频提取优先尝试 NVIDIA 硬件解码路径（CUDA/NVDEC）
- 若硬件路径不可用，自动回退 CPU 提取
- 该策略对稳定性无破坏：失败会自动降级，不中断任务

## 8. 数据约定

任务目录：`workspace/<job>/`

典型文件：

- 原始输入媒体（`.mp4`、`.m4a` 等，文件名保持原始名或视频标题）
- `<prefix>.wav`（ffmpeg 提取的音频，识别后可保留）
- `<prefix>.orig.srt` / `<prefix>.orig.txt`（转录原文）
- `<prefix>.<lang>.srt` / `<prefix>.<lang>.txt`（翻译后，如 `.zh.srt`、`.en.srt`）
- `<prefix>.srt` / `<prefix>.txt`（兼容保留，同 orig）
- `<prefix>.zip`（打包 bundle，点击下载后生成）
- `task_meta.json`（记录 `file_prefix`、`lang_code`、`source_lang`、`is_non_zh`）

目录名规则（上传/下载均相同）：

```python
safe = re.sub(r'[/\x00]', '', name).strip()[:20] or fallback
```

## 9. 异常处理

- 端口占用：`main.sh` 与 `ExecStartPre` 会清理残留进程
- 后台挂起：ffmpeg 使用 `-nostdin`，主进程 stdin 重定向 `/dev/null`
- 翻译失败：状态栏与日志输出错误原因，不影响已有原文文件

## 10. 安装与依赖

### 安装脚本（`install.sh`）

`install.sh` 全自动完成以下步骤，支持 Ubuntu 20.04/22.04/24.04：

| 步骤 | 内容 |
|------|------|
| 系统包 | `apt install python3.12 ffmpeg build-essential ...` |
| uv | 从 `https://astral.sh/uv/install.sh` 安装 |
| .venv | `uv venv .venv --python 3.12` |
| 依赖 | `uv pip install -e .` |
| PyTorch | 按 `nvidia-smi` 返回的 compute_cap 选版本（sm < 70 → 2.3.1+cu121；sm 7x+ → 最新 cu124；无 GPU → CPU） |
| yt-dlp | `curl` 下载最新二进制到 `~/.local/bin/yt-dlp` |
| workspace | `mkdir -p workspace/` |
| mkcert（可选） | `SETUP_HTTPS=1` 时，安装 mkcert 并生成证书 |
| systemd（可选） | `SETUP_SYSTEMD=1` 时，替换服务文件中的用户名/路径并注册服务 |

### PyTorch 版本约束

- Tesla P4 / Pascal（sm_61）最高支持 `torch==2.3.1+cu121`
- PyTorch ≥ 2.4 要求最低 sm_70（Volta）
- `install.sh` 自动检测，无需手动指定

### yt-dlp

- 安装位置：`~/.local/bin/yt-dlp`（由 `install.sh` 写入）
- 更新：`curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o ~/.local/bin/yt-dlp`
- 服务内查找顺序：venv bin → ~/.local/bin → miniconda → anaconda → system PATH

## 11. 后续可选优化

- 翻译按钮执行中禁用，避免重复触发
- 增加“仅翻译新增段”缓存策略
- 增加下载历史 zip 清理策略
