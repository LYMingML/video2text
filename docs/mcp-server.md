# video2text MCP Server

## 概述

MCP Server 将 video2text 的全部功能暴露为 [Model Context Protocol](https://modelcontextprotocol.io/) 工具，使 Claude Code、OpenClaw 等 AI 客户端可以直接调用视频下载、转录、翻译等功能。

**架构原则：纯 HTTP 客户端，零 GPU 占用。**

MCP Server 不导入任何重型模块（torch/funasr/transformers），所有 GPU 工作由已运行的 FastAPI 服务完成。MCP 只负责提交请求、轮询结果、返回数据。

## 架构

```
┌──────────────────┐     MCP/stdio      ┌──────────────────┐
│  Claude Code /   │ ◄──────────────► │   MCP Server     │
│  OpenClaw / ...  │   (tool calls)    │  (mcp_server.py) │
└──────────────────┘                   └────────┬─────────┘
                                                │ HTTP (httpx)
                                                ▼
                                       ┌──────────────────┐
                                       │  FastAPI 服务     │
                                       │  (127.0.0.1:7881)│
                                       │                  │
                                       │  任务队列 → GPU   │
                                       │  文件管理         │
                                       └──────────────────┘
```

### 任务流程

1. MCP Tool 接收参数 → 通过 HTTP 提交到 FastAPI
2. FastAPI 将任务加入队列，返回 `job_id`
3. MCP 轮询 `GET /api/jobs/{job_id}`（每 2 秒，最长 10 分钟）
4. 任务完成后，MCP 读取输出文件（SRT/TXT），返回给客户端

## 工具列表（23 个）

### 核心流水线

| 工具 | 功能 | 说明 |
|------|------|------|
| `v2t_process` | 统一处理入口 | 一步完成：上传/URL/历史文件 → 转录 → 翻译，最常用 |
| `v2t_transcribe_file` | 转录本地文件 | 上传视频/音频，返回 SRT 内容 |
| `v2t_transcribe_url` | 下载 URL 并转录 | 支持 YouTube/Bilibili/小红书等 20+ 平台 |
| `v2t_translate` | 翻译已有任务 | 对已完成的转录结果进行翻译 |
| `v2t_extract_audio` | 提取音频 | 从视频提取 WAV，返回转录结果 |

### 视频下载

| 工具 | 功能 |
|------|------|
| `v2t_download_video` | 仅下载视频（不转录），返回文件路径 |

### 文件管理

| 工具 | 功能 |
|------|------|
| `v2t_list_folders` | 列出 workspace 中所有任务文件夹 |
| `v2t_list_files` | 列出指定文件夹内的输出文件 |
| `v2t_read_file` | 读取 SRT/TXT 文件内容 |
| `v2t_download_file` | 下载文件到本地 |
| `v2t_delete_folder` | 删除单个文件夹 |
| `v2t_delete_folders` | 批量删除文件夹 |
| `v2t_all_output_files` | 列出全部输出文件（跨文件夹） |
| `v2t_list_history` | 列出历史视频和文件夹 |

### 任务管理

| 工具 | 功能 |
|------|------|
| `v2t_job_status` | 查询任务状态 |
| `v2t_stop_job` | 停止运行中的任务 |
| `v2t_queue_status` | 查看任务队列状态 |

### 配置与后端

| 工具 | 功能 |
|------|------|
| `v2t_list_backends` | 列出可用的 ASR 和翻译后端 |
| `v2t_list_translate_profiles` | 列出翻译配置组和模型 |
| `v2t_get_settings` | 获取应用配置（端口/代理等） |
| `v2t_folder_translate` | 翻译已有文件夹中的字幕 |
| `v2t_upload_cookie` | 上传 Cookie 文件（用于需要登录的平台） |

### 健康检查

| 工具 | 功能 |
|------|------|
| `v2t_health` | 检查 FastAPI 服务是否运行 |

## 使用示例

### 转录 URL 视频

```
用户：帮我转录这个视频 https://www.bilibili.com/video/BV1xx411c7mD

Claude：好的，我来下载并转录这个视频。
→ 调用 v2t_transcribe_url(url="https://www.bilibili.com/video/BV1xx411c7mD")

返回：SRT 字幕内容 + job_id
```

### 转录本地文件并翻译

```
用户：转录 /home/lym/Downloads/meeting.mp4 并翻译成中文

Claude：
→ 调用 v2t_transcribe_file(
    file_path="/home/lym/Downloads/meeting.mp4",
    auto_translate=True,
    target_lang="zh"
  )

返回：原文 SRT + 中文翻译 SRT
```

### 使用统一入口

```
用户：处理这个链接的视频，英文转中文字幕

Claude：
→ 调用 v2t_process(
    source_type="url",
    url="https://youtube.com/watch?v=xxx",
    language="en",
    target_lang="zh"
  )

返回：job_id + SRT 内容 + 翻译内容
```

### 查看历史和管理文件

```
用户：列出所有已处理的视频

Claude：
→ 调用 v2t_list_folders()
→ 调用 v2t_all_output_files()

返回：文件夹列表和文件详情
```

## 配置

### 前置条件

1. **FastAPI 服务已启动**（默认 `http://127.0.0.1:7881`）
2. **Python 依赖已安装**：`uv sync`（自动安装 `mcp` 和 `httpx`）

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VIDEO2TEXT_URL` | `http://127.0.0.1:7881` | FastAPI 服务地址 |

### 注册方式

#### 1. 项目级（推荐）

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "video2text": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "mcp_server.py"],
      "cwd": "/home/lym/projects/video2text"
    }
  }
}
```

#### 2. 全局级（Claude Code）

在 `~/.claude.json` 的 `mcpServers` 中添加相同配置，所有项目均可使用。

#### 3. 跨项目引用

在其他项目（如 OpenClaw）的 `.mcp.json` 中：

```json
{
  "mcpServers": {
    "video2text": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/home/lym/projects/video2text", "python", "/home/lym/projects/video2text/mcp_server.py"],
      "cwd": "/home/lym/projects/video2text"
    }
  }
}
```

## 参数说明

### ASR 后端

| 后端名 | 说明 | 适用场景 |
|--------|------|---------|
| `FunASR（Paraformer）` | 中文普通话最佳 | 中文视频/音频 |
| `faster-whisper` | 多语言通用 | 英文/日文/多语言 |
| 留空 | 使用默认后端 | 一般用途 |

### 语言参数

支持 `"zh"`, `"en"`, `"ja"`, `"ko"` 等，或 `"自动检测"`。

### 设备参数

- `"CUDA"` — GPU 加速（默认）
- `"CPU"` — CPU 推理

### 翻译参数

- `target_lang`：目标语言代码（`"zh"`, `"en"`, `"ja"` 等）
- `online_profile`：翻译配置组名（留空用默认）
- `online_model`：翻译模型名（留空用默认）

## 注意事项

1. **FastAPI 必须先启动**：MCP Server 是纯客户端，不执行 GPU 计算。使用前需确保 FastAPI 服务正在运行。
2. **长任务超时**：转录/翻译可能耗时数分钟，MCP 默认 10 分钟超时轮询。
3. **GPU 资源共享**：MCP 与 FastAPI 共享 GPU，不要同时运行多个 GPU 任务。
4. **文件路径**：`file_path` 参数必须是绝对路径，MCP Server 运行在项目目录下。
5. **Cookie 上传**：下载需要登录的平台视频时，先用 `v2t_upload_cookie` 上传 cookies.txt。
