# 视频平台下载工具调研与集成报告

## 更新时间
2026-03-22

---

## 一、调研结论

### 主流通用下载工具

| 工具 | 支持网站数 | 语言 | 特点 |
|------|-----------|------|------|
| **yt-dlp** | 1800+ | Python | 最强大，支持最多平台 |
| **you-get** | 80+ | Python | 国内平台友好 |
| **gallery-dl** | 1000+ | Python | 图片/视频采集 |
| **lux** | 50+ | Go | B站效果好 |
| **spotdl** | Spotify | Python | Spotify 专用 |

### 国内平台专用工具

| 平台 | 专用工具 | GitHub |
|------|----------|--------|
| 小红书 | XHS-Downloader | JoeanAmier/XHS-Downloader |
| 抖音/TikTok | TikTokDownloader | JoeanAmier/TikTok-Downloader |
| 多平台 | MediaCrawler | NanmiCoder/MediaCrawler |
| B站 | BBDown | nilaoda/BBDown |
| 微信视频号 | wx_channels_download | ltaoo/wx_channels_download |

---

## 二、已安装的工具

```bash
# 检查已安装工具
yt-dlp --version      # 2026.03.13 ✅
you-get --version     # 0.4.1743 ✅
gallery-dl --version  # 1.31.10 ✅
spotdl --version      # 4.4.3 ✅
duckduckgo-mcp-server # 0.1.2 ✅ (MCP 搜索)
```

---

## 三、yt-dlp 支持的主要平台

### 国内平台 ✅
- B站 (Bilibili) - 完整支持
- 抖音 (Douyin) - 完整支持
- 快手 (部分)
- 小红书 (XiaoHongShu) - 支持
- 微博 (Weibo) - 完整支持
- 知乎 (Zhihu) - 支持
- 优酷 (Youku) - 支持
- 爱奇艺 (iQIYI) - 支持
- 腾讯视频 - 支持
- 芒果TV - 支持
- 搜狐视频 - 支持
- AcFun - 完整支持
- 喜马拉雅 - 支持
- 网易云音乐 - 支持
- QQ音乐 - 支持

### 国外平台 ✅
- YouTube - 完整支持
- TikTok - 完整支持
- Instagram - 支持
- Twitter/X - 完整支持
- Facebook - 支持
- Reddit - 支持
- Vimeo - 完整支持
- Twitch - 完整支持
- Dailymotion - 支持
- Pinterest - 支持
- SoundCloud - 支持

---

## 四、video2text 项目集成状态

### 已完成集成

| 平台/工具 | 状态 | 说明 |
|-----------|------|------|
| yt-dlp | ✅ | 核心下载器，支持 1800+ 平台 |
| XHS-Downloader | ✅ | 小红书专用，无水印下载 |

### 工作流程

```
用户输入 URL
    ↓
检测 URL 类型
    ↓
├─ 小红书链接 → XHS-Downloader API → 下载无水印视频
├─ 其他链接 → yt-dlp → 下载视频
    ↓
返回下载结果
```

---

## 五、DuckDuckGo MCP 配置

### 已添加到 ~/.claude/settings.json

```json
{
  "mcpServers": {
    "duckduckgo": {
      "command": "duckduckgo-mcp-server"
    }
  }
}
```

### 功能
- DuckDuckGo 网页搜索
- 内容获取和解析
- 适合隐私保护需求的搜索

---

## 六、快速使用指南

### 1. 启动小红书下载服务

```bash
cd /home/lym/projects/xhs-test/XHS-Downloader
python main.py api
```

### 2. 使用 yt-dlp 下载视频

```python
# 在 video2text web 界面输入 URL
# 自动识别平台并下载
```

### 3. 命令行下载

```bash
# B站
yt-dlp "https://www.bilibili.com/video/BVxxx"

# YouTube
yt-dlp "https://www.youtube.com/watch?v=xxx"

# 抖音
yt-dlp "https://www.douyin.com/video/xxx"

# 微博
yt-dlp "https://weibo.com/xxx"

# Instagram
yt-dlp "https://www.instagram.com/p/xxx"
```

---

## 七、相关文件

| 文件 | 说明 |
|------|------|
| `utils/xhs_downloader.py` | 小红书下载客户端 |
| `start-xhs-downloader.sh` | XHS-Downloader 启动脚本 |
| `docs/video-download-guide.md` | 完整下载工具指南 |
| `~/.claude/settings.json` | MCP 配置（含 DuckDuckGo） |

---

## 八、后续建议

1. **添加更多专用工具**:
   - 抖音专用工具 (TikTokDownloader)
   - 微信视频号下载工具

2. **优化下载流程**:
   - 添加下载重试机制
   - 支持批量下载

3. **添加下载队列**:
   - 支持多任务并行下载
   - 下载进度显示

---

*报告生成时间: 2026-03-22*
