# video2text 设计文档

**版本**: v0.6.0

## 修复日志

### v0.6.0
- 移除 `main.py`（2151 行），核心逻辑全部迁移到 `src/core/`
- 移除遗留 `backend/` 目录，统一使用 `src/backends/` 插件注册表
- `fastapi_app.py` 解除 `import main` 依赖，全部从 `src/core/` 直接导入
- 移除 `scripts/main.sh`，`run.sh` 为唯一启动脚本
- `_do_transcribe_stream` 生成器迁移到 `transcribe_logic.py`，使用统一后端注册表
- 修复 `pyproject.toml` 构建配置适配 `src/` 布局
- Dockerfile 改用 `run.sh` 替代 `main.sh`

### v0.5.1
- **run.sh 启动脚本**：支持 HTTP+HTTPS 双端口、后台模式、--status/--stop/--restart/--log 管理命令
- **chunks 自动清理**：转录完成后 Pipeline 自动 `shutil.rmtree(chunks/)`
- **fix**：`transcribe_logic.py` 快速路径 `chunk_dir` 未定义导致 NameError
- **fix**：Dockerfile 移除不存在的 `video2text.service` COPY

### v0.5.0
- **四阶段流水线集成**：Pipeline 引擎集成到 FastAPI，4 个独立队列 + 4 个守护线程
  - 阶段 1（下载）：源文件归档、job 目录创建
  - 阶段 2（预处理）：ffmpeg WAV 提取 + 音频分片
  - 阶段 3（转录）：纯 GPU 转录（使用预分片结果）
  - 阶段 4（翻译）：翻译后端调用
- **队列页 4 列显示**：每个阶段一列，实时 step_label 进度
- **音频分片前移**：从转录阶段移到预处理阶段，转录阶段仅做 GPU 计算
- **自动翻译后端化**：勾选自动翻译后，Pipeline 在转录完成自动推入翻译队列
- **自动下载**：fetch+blob 方式下载 ZIP 到浏览器默认路径，无需确认弹窗
- **UI 偏好持久化**：自动翻译/下载、后端、语言、设备等选择保存到 `.env`
- **workspace 路径修复**：`src/workspace` 符号链接到共享 workspace
- **faster-whisper GPU 修复**：`cuda:0` 规范化为 `cuda`

### v0.4.4
- 文件结构重组：`src/` 目录统一代码，根级 `backends/`/`core/`/`utils/` 为符号链接
- MCP server 参数名、文件读取、翻译轮询修复
- 启动脚本修复

### v0.4.3
- 移除 Gradio UI 依赖，FastAPI 为唯一前端

### v0.4.2
- SSE 替代 1 秒轮询
- 修复自动翻译/自动下载标志丢失
- "直接保存"功能

### v0.2.3

### v0.2.3
- 文档重构：精简 Docker 教程，统一使用单一镜像
- 删除 `Dockerfile.cpu` 和 `docker-push.sh`，简化镜像管理
- `.env.example` 添加配置项注释

### v0.2.2
- **文件去重**：上传相同内容的视频/音频时自动复用缓存，避免重复复制
  - 使用 SQLite 数据库 (`workspace/fingerprints.db`) 存储文件指纹
  - 指纹比较：文件大小 + 文件头50字节 + 文件尾50字节
  - 不依赖文件名，仅比较内容

### v0.2.1
- 修复 GPU 检测：`_has_nvidia_gpu()` 不再将未设置的 `CUDA_VISIBLE_DEVICES` 视为禁用
- 修复 URL 下载：`refreshHistory()` 参数传递，确保相同 stem 音频文件自动回退
- 修复 systemd：`video2text.service` 的 `StartLimitIntervalSec` 移至 `[Unit]` section

## 1. 目标

提供一个可在 Linux 与局域网环境稳定运行的音视频转字幕服务，支持：

- 本地文件上传转录
- 在线 URL 下载转录
- 自动字幕优先导入
- 多后端 ASR（FunASR / faster-whisper）
- 可选翻译与结果文件下载
- 本机与 Docker 两种部署方式

## 2. 核心架构

- `fastapi_app.py`：Web 页面与 API、任务调度、URL 下载、文件下载
- `src/core/config.py`：全局配置与常量
- `src/core/workspace.py`：工作目录管理、文件指纹、任务元数据
- `src/core/transcribe_logic.py`：转录编排（含生成器版本）
- `src/core/pipeline.py`：四阶段流水线引擎
- `src/backends/`：插件化 ASR/翻译后端（注册表模式）
- `src/utils/subtitle.py`：字幕清洗与 SRT/TXT 写入
- `src/utils/online_models.py`：`.env` 与在线模型配置持久化
- `run.sh`：启动脚本（start/stop/restart/status/log）
- `Dockerfile`：Docker 镜像（支持 GPU 和 CPU，无 GPU 时自动回退）

## 3. 关键能力

### 3.1 局域网访问

- 启动脚本默认 `HOST=0.0.0.0`
- 端口来自 `.env` 的 `APP_PORT`（默认 `7881`）
- 局域网终端可通过 `http://<server-ip>:7881` 访问

### 3.2 本地与在线音视频解析

- 本地上传：`POST /api/transcribe/start` 通过 `video_file` 接收媒体文件
- 在线解析：`POST /api/download_url` 使用 `yt-dlp` 下载媒体
- 自动字幕优先：若下载到平台字幕，先导入字幕并可跳过 ASR

### 3.3 默认字幕优先项

- `.env` 使用单值键：`AUTO_SUBTITLE_LANG`
- 值域：`zh`/`none`/`en`/`ja`/`ko`/`es`/`fr`/`de`/`ru`/`pt`/`ar`/`hi`
- 主页下拉修改后会同步更新 `.env`

## 4. API 摘要

- `GET /health`：健康检查
- `GET /api/history`：历史任务与媒体列表
- `POST /api/transcribe/start`：启动转录
- `POST /api/download_url`：下载 URL 媒体
- `POST /api/external/process`：第三方统一处理入口（base64/url/history）
- `POST /api/jobs/import-subtitle`：导入自动字幕任务
- `POST /api/jobs/{id}/translate`：翻译任务
- `GET /api/jobs/{id}`：任务状态轮询
- `GET /api/jobs/{id}/files`：输出文件列表
- `GET /api/jobs/{id}/download-file`：单文件下载
- `GET/POST /api/model/profiles*`：在线模型配置

## 5. Docker 部署设计

### 5.1 镜像

- 镜像：`adolyming/video2text:latest`
  - 基于 PyTorch CUDA runtime 镜像
  - 有 GPU 时自动使用 GPU 加速
  - 无 GPU 时自动回退 CPU 运行
- 默认暴露端口：`7881`
- 入口：`docker-entrypoint.sh`

### 5.2 compose

- 服务：`video2text`
- 端口映射：`7881:7881`
- 挂载：`workspace/`、`.env`、模型缓存卷
- 有 GPU 时配置 `gpus: all`，要求宿主机安装 NVIDIA Container Toolkit

## 6. 简明流程

1. 启动服务（本机或 Docker）
2. 选择本地上传或 URL 下载
3. 执行转录
4. 可选执行翻译
5. 下载 `srt/txt/zip`

## 7. 运维约定

- 每次代码或配置改动后，重启 `video2text.service`
- 文档（`README.md`、`design.md`）随功能变更同步更新
- `.env` 含敏感信息，不进入版本控制
