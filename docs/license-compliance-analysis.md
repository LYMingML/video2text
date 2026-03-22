# 许可证合规性分析报告

## 一、许可证分类

### 宽松许可证 (Permissive)
可以与任何许可证兼容，包括闭源商业使用。

| 工具 | 许可证 | 要求 |
|------|--------|------|
| faster-whisper | MIT | 保留版权声明 |
| FunASR | MIT | 保留版权声明 |
| yt-dlp | Unlicense | 无要求 |
| gradio | Apache-2.0 | 保留版权声明 + 说明修改 |
| fastapi | MIT | 保留版权声明 |
| ffmpeg-python | Apache-2.0 | 保留版权声明 |
| stable-ts | MIT | 保留版权声明 |
| you-get | MIT | 保留版权声明 |
| lux | MIT | 保留版权声明 |
| spotdl | MIT | 保留版权声明 |
| mitmproxy | MIT | 保留版权声明 |
| Douyin_TikTok_Download_API | Apache-2.0 | 保留版权声明 |

### Copyleft 许可证 (传染性)
如果分发包含这些代码的软件，整个软件也需要以相同许可证发布。

| 工具 | 许可证 | 影响 |
|------|--------|------|
| **XHS-Downloader** | GPL-3.0 | ⚠️ 如果嵌入代码，需要 GPL-3.0 |
| **gallery-dl** | GPL-2.0 | ⚠️ 如果嵌入代码，需要 GPL-2.0 |
| **MediaCrawler** | NC 学习许可 | ⚠️ 非商业使用 |

---

## 二、集成方式分析

### 关键问题：如何集成 GPL 工具？

GPL 的要求取决于**集成方式**：

#### 方式1: API 调用 (✅ 推荐)
```
你的项目 (MIT) → HTTP API → GPL 工具 (独立进程)
```
- **结论**: ✅ **不影响你的许可证**
- **原因**: 通过 API 通信不构成"派生作品"
- **你的实现**: `utils/xhs_downloader.py` 通过 HTTP 调用 XHS-Downloader API

#### 方式2: 子进程调用
```
你的项目 (MIT) → subprocess → GPL 工具 (独立进程)
```
- **结论**: ✅ **不影响你的许可证**
- **原因**: 独立进程不构成"链接"

#### 方式3: 直接导入/链接
```python
# ⚠️ 这种方式会触发 GPL 要求
from xhs_downloader import SomeClass
```
- **结论**: ❌ **需要将你的项目改为 GPL-3.0**

---

## 三、你的项目分析

### 当前集成方式

```python
# utils/xhs_downloader.py - 通过 HTTP API 调用
import httpx

class XHSDownloaderClient:
    def __init__(self, api_url: str = "http://127.0.0.1:5556"):
        self.api_url = api_url

    def download_note(self, url: str) -> dict:
        # 通过 HTTP API 调用独立的 XHS-Downloader 服务
        resp = client.post(f"{self.api_url}/xhs/detail", json=data)
```

**✅ 这是合规的集成方式！**

### 结论

| 场景 | 你的许可证选择 | 原因 |
|------|---------------|------|
| **当前实现** (API 调用) | ✅ **可以用 MIT** | GPL 工具独立运行 |
| **直接导入 GPL 代码** | ❌ 需要用 GPL-3.0 | 构成派生作品 |
| **分发自包含可执行文件** | ❌ 需要用 GPL-3.0 | 包含 GPL 代码 |

---

## 四、建议

### 推荐方案: 保持 MIT 许可证

你的项目可以继续使用 **MIT 许可证**，前提是：

1. ✅ **XHS-Downloader 独立运行** (API 模式)
2. ✅ **不将 GPL 代码嵌入项目**
3. ✅ **在文档中说明 XHS-Downloader 是可选外部依赖**

### 需要添加的说明

在 README 中添加：

```markdown
## 可选外部工具

本项目支持以下可选的外部下载工具：

| 工具 | 许可证 | 用途 | 使用方式 |
|------|--------|------|----------|
| XHS-Downloader | GPL-3.0 | 小红书下载 | 独立 API 服务 |
| gallery-dl | GPL-2.0 | 图片采集 | 命令行工具 |
| yt-dlp | Unlicense | 通用下载 | 命令行/API |

这些工具以**独立进程**方式运行，不包含在本项目代码中。
使用这些工具需遵守各自的许可证条款。
```

---

## 五、许可证兼容性图表

```
                    ┌─────────────────────────────────────┐
                    │         你的项目 (MIT)              │
                    │                                     │
                    │  ┌─────────────────────────────┐   │
                    │  │  Python 代码                │   │
                    │  │  - fastapi (MIT)            │   │
                    │  │  - gradio (Apache-2.0)      │   │
                    │  │  - faster-whisper (MIT)     │   │
                    │  │  - funasr (MIT)             │   │
                    │  └─────────────────────────────┘   │
                    │              ↓ HTTP API            │
                    │  ┌─────────────────────────────┐   │
                    │  │  外部工具 (独立进程)         │   │
                    │  │  - XHS-Downloader (GPL-3.0) │   │
                    │  │  - gallery-dl (GPL-2.0)     │   │
                    │  │  - yt-dlp (Unlicense)       │   │
                    │  └─────────────────────────────┘   │
                    └─────────────────────────────────────┘

结论: MIT ✅ (GPL 不"传染"因为是通过 API 调用独立服务)
```

---

## 六、如果你想要更严格的保护

### 方案 A: 添加 GPL 例外条款

```markdown
## 许可证说明

本项目主体采用 MIT 许可证。

当使用集成的 GPL 许可工具（如 XHS-Downloader）时：
- 这些工具作为独立的外部服务运行
- 用户需单独遵守各工具的许可证
- 本项目不包含 GPL 代码
```

### 方案 B: 双许可证

- **MIT 版本**: 不包含任何 GPL 工具集成
- **GPL-3.0 版本**: 包含完整的 GPL 工具集成

---

## 七、最终建议

### ✅ 你的项目可以继续使用 MIT 许可证

**理由**:
1. 所有 GPL 工具都是**独立运行**的外部服务
2. 通过 **HTTP API** 通信不构成"链接"
3. 没有将 GPL 代码**嵌入**项目

**需要做的**:
1. 在 README 中明确说明外部工具的许可证
2. 说明 XHS-Downloader 等是**可选依赖**
3. 提供不使用 GPL 工具的替代方案（如直接用 yt-dlp）

---

*分析时间: 2026-03-23*
