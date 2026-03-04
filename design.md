# video2text 设计文档

## 快速重启

推荐使用封装脚本 `main.sh` 启动/重启：

```bash
cd /home/lym/projects/video2text
chmod +x main.sh
./main.sh auto
```

脚本支持三种模式：

```bash
./main.sh auto   # 自动：有证书则 HTTPS，无证书则 HTTP
./main.sh https  # 强制 HTTPS
./main.sh http   # 强制 HTTP
```

局域网访问地址（按当前机器 IP）：

- HTTP: `http://192.168.1.2:7880`
- HTTPS: `https://192.168.1.2:7880`

## HTTPS（解决浏览器“不安全”）

推荐使用 `mkcert` 生成局域网自签证书。

### 1) 安装并初始化 mkcert（Linux）

```bash
sudo apt update
sudo apt install -y libnss3-tools mkcert
mkcert -install
```

### 2) 生成证书（IP + 主机名）

```bash
cd /home/lym/projects/video2text
mkdir -p certs
mkcert -cert-file video2text.pem -key-file video2text-key.pem 192.168.1.2 localhost 127.0.0.1
```

### 3) 使用 HTTPS 启动

```bash
cd /home/lym/projects/video2text
./main.sh https
```

HTTPS 访问地址：`https://192.168.1.2:7880`

> 若局域网其他设备首次访问仍提示证书不受信任，需要在对应设备导入并信任 mkcert 根证书。

## GitHub 登录与私有仓库发布

### 当前检查结果（2026-03-05）

- `gh` 已安装（`/usr/bin/gh`）
- 当前 **未登录 GitHub**（`gh auth status` 返回未认证）
- 当前仓库 **未配置远程地址**（`git remote -v` 无输出）
- 当前分支：`master`（且尚无首次提交）

结论：**现在不能直接创建私有仓库并推送**，需先登录。

### 一次性完成登录 + 创建私有仓库 + 推送

```bash
cd /home/lym/projects/video2text

# 1) 登录 GitHub（浏览器授权）
gh auth login

# 2) 可选：确认权限（需包含 repo）
gh auth status -h github.com

# 3) 先避免提交敏感证书私钥
echo "video2text-key.pem" >> .gitignore

# 4) 首次提交
git add .
git commit -m "init: video2text"

# 5) 创建私有仓库并推送
gh repo create video2text --private --source . --remote origin --push
```

### 已有私有仓库时（仅关联并推送）

```bash
cd /home/lym/projects/video2text
git remote add origin git@github.com:<你的用户名>/video2text.git
git push -u origin master
```

## 1. 项目目标

本项目提供一个可在局域网访问的本地 Web 服务，用于将视频/音频转录为字幕与纯文本，优先利用 NVIDIA GPU 加速。

核心目标：

- 本地部署、数据不出本机
- 支持中文优先（含普通话/粤语）与多语言场景
- 提供可视化 WebUI，支持文件上传、进度反馈、结果下载
- 每次任务独立落盘，便于追溯与排障

---

## 2. 约束与环境

- OS: Linux
- Python: 3.12（项目虚拟环境）
- GPU: Tesla P4 (sm_61)
- 服务监听: `0.0.0.0:7880`

### 2.1 GPU 兼容策略

- PyTorch 固定为 `2.3.1+cu121`（兼容 sm_61）
- `faster-whisper` 使用 CTranslate2，`compute_type=int8`
- 避免使用需要 Volta+ 的 `float16/int8_float16` 路径

---

## 3. 总体架构

```text
Browser (LAN)
   │
   ▼
Gradio WebUI (main.py)
   │
   ├─ 输入管理：上传文件、后端选择、模型选择、语言、设备
   ├─ 流程编排：提取音频 -> ASR -> 字幕生成 -> 文件输出
   ├─ 历史面板：展示 workspace 下已处理任务目录
   │
   ├─ Backend A: FunASR Paraformer (backend/funasr_backend.py)
   └─ Backend B: faster-whisper (backend/whisper_backend.py)

utils/audio.py      # ffmpeg 提取 16kHz 单声道 wav
utils/subtitle.py   # 生成 SRT/TXT
workspace/<job>/    # 单任务工作目录（输入、中间件、输出）
```

---

## 4. 模块设计

## 4.1 `main.py`

职责：

- Gradio 页面构建
- 任务目录管理（`workspace/<文件名主串>/`）
- 上传文件复制、音频提取、后端调用、结果写出
- 实时进度反馈
- 历史上传内容默认展示 + 手动刷新

关键函数：

- `_make_job_dir(original_path)`：按文件名生成任务目录
- `_workspace_history_markdown()`：扫描 `workspace/` 并生成历史展示
- `_do_transcribe(...)`：路由到不同 ASR 后端
- `process(...)`：Gradio 事件主流程（生成状态/文本/下载文件）

## 4.2 `backend/funasr_backend.py`

职责：

- 承载 FunASR Paraformer 家族模型推理
- 模型缓存（按 `(model_name, device)` 复用）
- 模型名归一化与兼容映射
- 时间戳后处理与标点切分

关键特性：

- 通过 `AutoModel(...)` 统一加载
- 无时间戳时使用估算时长降级，避免返回空结果
- 支持 `language=auto/zh/yue/en/ja/ko`

## 4.3 `backend/whisper_backend.py`

职责：

- `faster-whisper` 推理封装
- 根据设备与精度策略自动回退
- 输出标准化片段：`[(start_s, end_s, text), ...]`

关键特性：

- 针对 P4 默认使用 `int8`
- VAD 过滤与进度估算

## 4.4 `utils/audio.py`

职责：

- 使用 `ffmpeg` 将输入视频/音频转为 16kHz 单声道 WAV
- 查询音频时长
- 清理临时文件

## 4.5 `utils/subtitle.py`

职责：

- 时间格式转换
- 生成 SRT 字符串
- 生成纯文本（逐行）
- 保存到指定路径或临时文件

---

## 5. 处理流程

1. 用户上传视频/音频
2. 生成任务目录：`workspace/<slug>/`
3. 复制上传原文件到任务目录
4. 提取音频为 `workspace/<slug>/audio.wav`
5. 按后端配置执行 ASR（FunASR / faster-whisper）
6. 生成输出：
   - `workspace/<slug>/<原文件名>.srt`
   - `workspace/<slug>/<原文件名>.txt`
7. 页面返回：状态、识别文本、下载链接、历史列表刷新

---

## 6. 后端与模型策略

## 6.1 后端选择

- **FunASR（Paraformer）**：中文场景优先、支持多种 Paraformer 变体
- **faster-whisper（多语言）**：多语言泛化和生态成熟度更好

## 6.2 推荐模型

### FunASR 后端（当前环境实测可用）

- `paraformer-zh`（普通话推荐）
- `paraformer`（全量普通话大模型）
- `paraformer-zh-streaming`（低延迟）
- `paraformer-zh-spk`（角色区分，内部映射至普通话）
- `paraformer-en`（英文）
- `paraformer-en-spk`（英文说话人区分）
- `iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch`（中文全路径）
- `iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online`（中文流式全路径）
- `iic/speech_paraformer-large-vad-punc_asr_nat-en-16k-common-vocab10020`（英文全路径）
- `iic/SenseVoiceSmall`（多语言：中/粤/英/日/韩）
- `iic/SenseVoice-Small`（多语言备用源）
- `EfficientParaformer-large-zh`（大模型长语音）
- `EfficientParaformer-zh-en`（中英双语）
- `speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch`（全路径大模型）

> 说明：当前环境下 FunASR 对西班牙语没有稳定的官方快捷模型别名，系统对 `es` 会自动回退到 `faster-whisper`；日语建议优先尝试 `iic/SenseVoiceSmall`。

---

## 7. 目录与数据约定

```text
video2text/
├── main.py
├── backend/
│   ├── funasr_backend.py
│   └── whisper_backend.py
├── utils/
│   ├── audio.py
│   └── subtitle.py
├── workspace/
│   ├── <job1>/
│   │   ├── <upload-file>
│   │   ├── audio.wav
│   │   ├── <upload-file>.srt
│   │   └── <upload-file>.txt
│   └── <job2>/
└── video2text.service
```

命名策略：

- `<job>` 使用上传文件 stem 清洗后生成（保留中文/字母/数字）
- 默认复用同名目录，便于连续覆盖同一素材版本

---

## 8. 异常与降级

- 模型加载失败：页面状态栏显示错误信息
- 时间戳缺失：使用估算时长降级，保证有可下载结果
- 端口占用：启动时报错并提示更换端口
- GPU 不可用：可切换 CPU（性能下降）

---

## 9. 可观测性

- Python logging 输出关键步骤：
  - 任务目录
  - 音频时长
  - 模型加载与后端选择
  - 错误堆栈
- Gradio 状态框同步反馈用户可见进度

---

## 10. 运维与启动

开发启动：

```bash
cd /home/lym/projects/video2text
.venv/bin/python main.py --host 0.0.0.0 --port 7880
```

局域网访问：

- `http://192.168.1.2:7880`

systemd：

- 使用 `video2text.service` 管理开机自启（需 sudo 安装）

---

## 11. 后续优化建议

- 历史任务增加“点击打开目录/下载打包”
- 同名任务目录支持时间戳后缀避免覆盖
- 增加模型可用性预检查按钮
- 增加批处理队列（多文件上传）
- 增加任务耗时统计与 GPU 使用率显示
