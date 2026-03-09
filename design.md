# video2text 设计文档

**版本**: v0.2.1

## 修复日志

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
- `main.py`：转录核心流程与工作目录管理
- `backend/funasr_backend.py`：FunASR 推理封装
- `backend/whisper_backend.py`：faster-whisper 推理封装
- `utils/subtitle.py`：字幕清洗与 SRT/TXT 写入
- `utils/online_models.py`：`.env` 与在线模型配置持久化
- `main.sh`：本机启动脚本（默认 `0.0.0.0`）
- `Dockerfile` / `docker-compose.yml`：容器部署

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

- 基础镜像：`python:3.12-slim-bookworm`
- 内置 ffmpeg 与 Python 依赖
- 默认暴露端口：`7881`
- 入口：`docker-entrypoint.sh`

### 5.2 compose

- 服务名：`video2text`
- 端口映射：`7881:7881`
- 挂载：`workspace/`、`.env`、模型缓存卷
- 支持 CPU/GPU 一体部署（GPU 需宿主机额外准备）

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
