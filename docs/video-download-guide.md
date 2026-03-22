# 视频平台下载工具完整指南

## 更新时间
2026-03-22

---

## 一、主流视频/音频平台分类

### 国内平台

| 平台 | 类型 | 特点 | 推荐工具 |
|------|------|------|----------|
| **B站 (Bilibili)** | 长视频 | 动漫、知识、游戏 | yt-dlp, lux, BBDown |
| **抖音** | 短视频 | 短视频、直播 | TikTokDownloader, yt-dlp |
| **快手** | 短视频 | 短视频、直播 | MediaCrawler, yt-dlp |
| **小红书** | 图文/视频 | 生活分享 | XHS-Downloader, MediaCrawler |
| **微博** | 综合 | 视频、图片、文字 | yt-dlp, weibo-crawler |
| **微信公众号** | 图文 | 文章、视频 | 公众号文章导出工具 |
| **视频号** | 短视频 | 微信生态 | wx_channels_download |
| **知乎** | 知识 | 视频、文章 | yt-dlp |
| **西瓜视频** | 长视频 | 中长视频 | yt-dlp |
| **优酷** | 长视频 | 影视剧 | yt-dlp |
| **爱奇艺** | 长视频 | 影视剧 | yt-dlp |
| **腾讯视频** | 长视频 | 影视剧 | yt-dlp |
| **芒果TV** | 长视频 | 综艺 | yt-dlp |
| **搜狐视频** | 长视频 | 影视剧 | yt-dlp |
| **AcFun** | 长视频 | 动漫 | yt-dlp |
| **喜马拉雅** | 音频 | 有声书、播客 | yt-dlp, xmly-fetcher |
| **网易云音乐** | 音频 | 音乐 | yt-dlp, NeteaseCloudMusicApi |
| **QQ音乐** | 音频 | 音乐 | yt-dlp |

### 国外平台

| 平台 | 类型 | 特点 | 推荐工具 |
|------|------|------|----------|
| **YouTube** | 长视频 | 全球最大视频平台 | yt-dlp, 4K Video Downloader |
| **TikTok** | 短视频 | 全球短视频 | yt-dlp, TikTokDownloader |
| **Instagram** | 综合 | 图片、视频、Reels | yt-dlp, gallery-dl |
| **Twitter/X** | 综合 | 视频、图片 | yt-dlp, gallery-dl |
| **Facebook** | 综合 | 视频、图片 | yt-dlp |
| **Reddit** | 综合 | 视频、图片 | yt-dlp, gallery-dl |
| **Vimeo** | 长视频 | 专业视频 | yt-dlp |
| **Twitch** | 直播 | 游戏直播 | yt-dlp |
| **Dailymotion** | 长视频 | 欧洲视频平台 | yt-dlp |
| **Pinterest** | 图片 | 图片分享 | gallery-dl |
| **Spotify** | 音频 | 音乐流媒体 | spotdl |
| **SoundCloud** | 音频 | 音乐、播客 | yt-dlp, spotdl |
| **Apple Music** | 音频 | 音乐 | spotdl |

---

## 二、下载工具详解

### 1. yt-dlp ⭐⭐⭐⭐⭐ (核心推荐)

**GitHub**: https://github.com/yt-dlp/yt-dlp

**支持平台**: 1800+ 网站

**安装**:
```bash
pip install yt-dlp
# 或
pipx install yt-dlp
```

**常用命令**:
```bash
# 基础下载
yt-dlp "https://www.youtube.com/watch?v=xxx"

# 下载最佳质量
yt-dlp -f "bestvideo+bestaudio" "URL"

# 下载并合并为 mp4
yt-dlp -f "bestvideo+bestaudio" --merge-format mp4 "URL"

# 下载播放列表
yt-dlp -o "%(playlist_index)s-%(title)s.%(ext)s" "播放列表URL"

# 下载字幕
yt-dlp --write-subs --sub-langs zh-Hans,en "URL"

# 使用浏览器 Cookie（需要登录的视频）
yt-dlp --cookies-from-browser chrome "URL"

# 下载 B 站视频
yt-dlp "https://www.bilibili.com/video/BVxxx"

# 下载抖音视频
yt-dlp "https://www.douyin.com/video/xxx"
```

**Python 调用**:
```python
import subprocess

def download_video(url: str, output_path: str = ".") -> str:
    """使用 yt-dlp 下载视频"""
    cmd = [
        "yt-dlp",
        "-f", "bestvideo+bestaudio",
        "--merge-format", "mp4",
        "-o", f"{output_path}/%(title)s.%(ext)s",
        url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout
```

---

### 2. XHS-Downloader (小红书专用)

**GitHub**: https://github.com/JoeanAmier/XHS-Downloader

**安装**:
```bash
git clone https://github.com/JoeanAmier/XHS-Downloader.git
cd XHS-Downloader
pip install -r requirements.txt
```

**使用**:
```bash
# API 模式
python main.py api

# 命令行模式
python main.py --url "https://www.xiaohongshu.com/explore/xxx"
```

---

### 3. TikTokDownloader (抖音/TikTok)

**GitHub**: https://github.com/JoeanAmier/TikTok-Downloader (DouK-Downloader)

**功能**:
- 抖音/TikTok 无水印视频下载
- 批量下载
- 数据采集

---

### 4. MediaCrawler (多平台爬虫)

**GitHub**: https://github.com/NanmiCoder/MediaCrawler

**支持平台**: 小红书、抖音、快手、B站、微博、知乎、贴吧

**安装**:
```bash
git clone https://github.com/NanmiCoder/MediaCrawler.git
cd MediaCrawler
pip install -r requirements.txt
playwright install
```

---

### 5. gallery-dl (图片/视频)

**GitHub**: https://github.com/mikf/gallery-dl

**支持平台**: 1000+ 图片/视频网站

**安装**:
```bash
pip install gallery-dl
```

**使用**:
```bash
# 下载 Instagram
gallery-dl "https://www.instagram.com/user/xxx"

# 下载 Twitter
gallery-dl "https://twitter.com/user/status/xxx"

# 下载 Pinterest
gallery-dl "https://www.pinterest.com/pin/xxx"
```

---

### 6. you-get

**GitHub**: https://github.com/soimort/you-get

**安装**:
```bash
pip install you-get
```

**使用**:
```bash
you-get "https://www.bilibili.com/video/BVxxx"
```

---

### 7. lux (Go 语言)

**GitHub**: https://github.com/iawia002/lux

**安装**:
```bash
# macOS
brew install lux

# Linux
go install github.com/iawia002/lux@latest
```

**使用**:
```bash
lux "https://www.bilibili.com/video/BVxxx"
```

---

### 8. BBDown (B站专用)

**GitHub**: https://github.com/nilaoda/BBDown

**特点**: 专为 B 站设计，支持大会员视频

**安装**:
```bash
# 下载 release 版本
# https://github.com/nilaoda/BBDown/releases
```

---

### 9. spotdl (Spotify)

**GitHub**: https://github.com/spotDL/spotify-downloader

**安装**:
```bash
pip install spotdl
```

**使用**:
```bash
# 下载单曲
spotdl "https://open.spotify.com/track/xxx"

# 下载播放列表
spotdl "https://open.spotify.com/playlist/xxx"
```

---

### 10. 微信视频号下载

**工具**: wx_channels_download

**GitHub**: https://github.com/ltaoo/wx_channels_download

**特点**: 需要在 Windows 上配合微信 PC 端使用

---

## 三、工具对比与选择建议

### 按平台选择

| 平台 | 首选工具 | 备选工具 |
|------|----------|----------|
| YouTube | yt-dlp | 4K Video Downloader |
| B站 | yt-dlp / lux | BBDown, bilibili-dl |
| 抖音 | TikTokDownloader | yt-dlp |
| 快手 | yt-dlp | MediaCrawler |
| 小红书 | XHS-Downloader | MediaCrawler |
| 微博 | yt-dlp | weibo-crawler |
| Instagram | yt-dlp / gallery-dl | - |
| Twitter/X | yt-dlp / gallery-dl | - |
| TikTok | yt-dlp | TikTokDownloader |
| Spotify | spotdl | yt-dlp |
| 微信视频号 | wx_channels_download | - |

### 按需求选择

| 需求 | 推荐工具 |
|------|----------|
| 单文件下载 | yt-dlp |
| 批量下载 | yt-dlp + 脚本 / MediaCrawler |
| 数据采集 | MediaCrawler |
| 图片下载 | gallery-dl |
| 音乐下载 | spotdl / yt-dlp |
| 最高画质 | yt-dlp (指定 -f 参数) |
| 需要登录 | yt-dlp (--cookies-from-browser) |

---

## 四、video2text 项目集成方案

### 当前集成状态

| 平台 | 工具 | 状态 |
|------|------|------|
| 通用 (1000+ 平台) | yt-dlp | ✅ 已集成 |
| 小红书 | XHS-Downloader | ✅ 已集成 |

### 待集成工具

```python
# 建议的下载器优先级
DOWNLOAD_PRIORITY = {
    # 小红书优先用专用工具
    "xiaohongshu.com": ["xhs-downloader", "yt-dlp"],
    "xhslink.com": ["xhs-downloader", "yt-dlp"],

    # B站可以用多种工具
    "bilibili.com": ["yt-dlp", "lux", "you-get"],

    # 抖音
    "douyin.com": ["yt-dlp", "tiktok-downloader"],

    # 微博
    "weibo.com": ["yt-dlp"],

    # 其他平台默认用 yt-dlp
    "default": ["yt-dlp"],
}
```

---

## 五、快速安装脚本

```bash
#!/bin/bash
# 安装所有视频下载工具

# Python 工具
pip install yt-dlp you-get gallery-dl spotdl

# 克隆专用工具
mkdir -p ~/tools
cd ~/tools

# XHS-Downloader (小红书)
git clone https://github.com/JoeanAmier/XHS-Downloader.git
cd XHS-Downloader && pip install -r requirements.txt && cd ..

# MediaCrawler (多平台)
git clone https://github.com/NanmiCoder/MediaCrawler.git
cd MediaCrawler && pip install -r requirements.txt && cd ..

# TikTok-Downloader (抖音)
git clone https://github.com/JoeanAmier/TikTok-Downloader.git
cd TikTok-Downloader && pip install -r requirements.txt && cd ..

# lux (Go 工具)
go install github.com/iawia002/lux@latest

echo "所有工具安装完成！"
```

---

## 六、注意事项

1. **法律合规**: 仅下载您有权访问的内容，尊重版权
2. **平台条款**: 遵守各平台的使用条款
3. **登录视频**: 部分需要登录的视频需要使用 `--cookies-from-browser` 参数
4. **水印问题**:
   - 抖音/快手/TikTok: 使用专用工具可下载无水印版本
   - 小红书: XHS-Downloader 支持无水印下载
5. **更新频率**: yt-dlp 更新频繁，建议定期 `pip install -U yt-dlp`

---

## 七、常见问题

### Q1: yt-dlp 下载失败怎么办？
```bash
# 1. 更新 yt-dlp
pip install -U yt-dlp

# 2. 使用浏览器 Cookie
yt-dlp --cookies-from-browser chrome "URL"

# 3. 尝试不同格式
yt-dlp -F "URL"  # 查看可用格式
yt-dlp -f "best" "URL"
```

### Q2: 如何下载会员视频？
需要使用浏览器 Cookie：
```bash
yt-dlp --cookies-from-browser chrome "URL"
```

### Q3: 如何批量下载？
```bash
# 从文件读取链接
yt-dlp -a links.txt

# 下载播放列表
yt-dlp "播放列表URL"
```

### Q4: 下载速度慢怎么办？
```bash
# 使用 aria2c 加速
pip install aria2p
yt-dlp --downloader aria2c "URL"
```
