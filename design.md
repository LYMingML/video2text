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

- `main.py`
   - UI 与事件绑定
   - 转录流式处理
   - 手动翻译处理
   - 打包下载逻辑
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

UI 结构（当前）：

- 标题与介绍下方：横向页面选项（单行）`主页`、`文件管理`、`配置模型`
- 主页面：上传与选择视频入口 + 转写结果与日志
- 文件管理页面：历史目录查看、刷新、删除
- 配置模型页面：识别后端、语言、高级选项、在线模型配置管理

## 3. 业务流程

### 3.1 转录（自动）

1. 上传或选择历史视频
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

### 3.2 翻译（手动）

1. 点击 `翻译` 按钮
2. 读取原文 SRT
3. 使用硅基流动 API 流式输出翻译并显示进度
4. 生成中文译文字幕/文本

输出文件：

- `<prefix>.zh.srt`
- `<prefix>.zh.txt`

### 3.3 下载（手动）

- 点击 `下载SRT字幕`：生成 zip，包含原文 SRT + 中文 SRT
- 点击 `下载纯文本`：生成 zip，包含原文 TXT + 中文 TXT

## 4. 翻译策略

默认配置（`.env`）：

- `SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1`
- `SILICONFLOW_API_KEY=<你的key>`
- `SILICONFLOW_MODEL=Kimi-K2.5`

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

- 原始输入媒体
- `<prefix>.wav`
- `<prefix>.orig.srt` / `<prefix>.orig.txt`
- `<prefix>.zh.srt` / `<prefix>.zh.txt`（点击翻译后）
- `<prefix>.srt.bundle.zip` / `<prefix>.txt.bundle.zip`（点击下载按钮后）
- `task_meta.json`（记录源语言、前缀等）

## 9. 异常处理

- 端口占用：`main.sh` 与 `ExecStartPre` 会清理残留进程
- 后台挂起：ffmpeg 使用 `-nostdin`，主进程 stdin 重定向 `/dev/null`
- 翻译失败：状态栏与日志输出错误原因，不影响已有原文文件

## 10. 后续可选优化

- 翻译按钮执行中禁用，避免重复触发
- 增加“仅翻译新增段”缓存策略
- 增加下载历史 zip 清理策略
