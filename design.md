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
# video2text 设计文档（Linux）

## 0. 快速重启

```bash
cd /home/lym/projects/video2text
source .venv/bin/activate

./main.sh auto
```

可选：

```bash
./main.sh http
./main.sh https
```

默认端口 `7880`。

---

## 1. 设计目标

本项目目标是在 Linux 环境提供稳定的视频转字幕服务：

- Web 页面可直接上传/复用历史文件
- 支持 FunASR 与 faster-whisper 双后端
- 长音频可流式显示转录进度
- 可中断正在进行的任务
- 支持 HTTPS 内网访问
- 提供历史目录空间管理与删除

---

## 2. 架构概览

### 2.1 主要模块

- `main.py`
   - 构建 Gradio 界面
   - 处理上传、参数、后端切换
   - 管理流式输出与停止事件
   - 启动 HTTP/HTTPS 服务

- `backend/funasr_backend.py`
   - FunASR 模型加载与缓存
   - 模型名别名归一化
   - 推理结果结构统一

- `backend/whisper_backend.py`
   - faster-whisper 推理封装
   - GPU/CPU 与计算精度控制

- `utils/audio.py`
   - 视频抽音频
   - 音频按片段切分（流式进度基础）

- `utils/subtitle.py`
   - 时间轴修正
   - SRT 与 TXT 输出

- `main.sh`
   - 启动模式封装（`auto/http/https`）
   - 清理旧进程
   - 注入 HTTPS 参数与 `NO_PROXY`

### 2.2 目录与数据

- `workspace/`：上传文件与输出结果
- 每个任务在对应目录内保存中间产物与最终字幕

---

## 3. 核心流程

### 3.1 转录流程

1. 选择输入（新上传或历史文件）
2. 提取音频并切分为固定时长片段
3. 逐片调用后端识别
4. 片段时间映射为全局时间轴
5. 页面持续刷新进度与日志
6. 汇总输出 SRT 与 TXT

### 3.2 停止机制

- UI 的“停止转录”触发全局停止事件
- 在分片边界立即中断后续推理
- 通过 Gradio 取消机制结束前端任务流

### 3.3 文件管理

- 历史列表按目录聚合显示空间（MB）
- 支持选定目录删除并实时刷新

---

## 4. 模型与性能策略

### 4.1 后端定位

- FunASR：中文场景速度优先
- faster-whisper：多语言稳定性优先

### 4.2 语言建议

- 中文：`paraformer-zh`
- 日语/韩语/粤语：`iic/SenseVoiceSmall`
- 西班牙语：优先 `faster-whisper`
- 高质量多语言：`faster-whisper large-v3`

### 4.3 Tesla P4 建议

- 使用 `int8`
- 避免 `float16` / `int8_float16`

---

## 5. Linux 安装与运行

```bash
cd /home/lym/projects/video2text

python3 -m venv .venv
source .venv/bin/activate

pip install -U pip
pip install -e .

pip install "torch==2.3.1+cu121" "torchaudio==2.3.1+cu121" --index-url https://download.pytorch.org/whl/cu121

./main.sh auto
```

---

## 6. HTTPS 方案（Linux）

### 6.1 证书文件约定

项目根目录使用：

- `video2text.pem`
- `video2text-key.pem`

### 6.2 mkcert（推荐）

```bash
sudo apt update
sudo apt install -y libnss3-tools mkcert
mkcert -install

cd /home/lym/projects/video2text
mkcert -cert-file video2text.pem -key-file video2text-key.pem 192.168.1.2 localhost 127.0.0.1

./main.sh https
```

### 6.3 openssl（备用）

```bash
cd /home/lym/projects/video2text
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
   -keyout video2text-key.pem \
   -out video2text.pem \
   -subj "/CN=192.168.1.2" \
   -addext "subjectAltName=IP:192.168.1.2,IP:127.0.0.1,DNS:localhost"

./main.sh https
```

客户端信任流程见 `installCert.md`。

---

## 7. 运维与排障

### 7.1 常用检查

```bash
ss -tlnp | grep 7880
pkill -f "main.py"
```

### 7.2 常见问题

- 浏览器提示不安全：客户端未导入证书/根证书，或证书 SAN 不含访问地址
- GPU 精度报错：切换到 `int8`
- 某语言效果差：切换后端到 `faster-whisper`
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
