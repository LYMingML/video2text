# 各平台音视频下载教程

本文档介绍如何从各主流平台下载音视频，配合 video2text 进行字幕转录。

---

## 一、支持平台概览

### 国内平台

| 平台 | 下载方式 | 难度 | 说明 |
|------|----------|------|------|
| B站 (bilibili) | yt-dlp / lux | ⭐ | 完全支持 |
| 抖音 | yt-dlp | ⭐⭐ | 需要 Cookie |
| 小红书 | XHS-Downloader | ⭐ | 无水印下载 |
| 快手 | yt-dlp | ⭐⭐ | 部分支持 |
| 微博 | yt-dlp | ⭐ | 完全支持 |
| 知乎 | yt-dlp | ⭐ | 视频支持 |
| 优酷 | yt-dlp | ⭐⭐ | 可能需要会员 |
| 爱奇艺 | yt-dlp | ⭐⭐ | 可能需要会员 |
| 腾讯视频 | yt-dlp | ⭐⭐ | 可能需要会员 |
| 公众号文章 | 手动/在线工具 | ⭐⭐⭐ | 见下方教程 |
| 视频号 | Windows 工具 | ⭐⭐⭐ | 仅 Windows |

### 国外平台

| 平台 | 下载方式 | 难度 | 说明 |
|------|----------|------|------|
| YouTube | yt-dlp | ⭐ | 完美支持 |
| TikTok | yt-dlp | ⭐⭐ | 可能被限制 |
| Instagram | yt-dlp | ⭐⭐ | 需要 Cookie |
| Twitter/X | yt-dlp | ⭐ | 完美支持 |
| Facebook | yt-dlp | ⭐ | 完美支持 |
| Vimeo | yt-dlp | ⭐ | 完美支持 |
| Spotify | spotdl | ⭐ | 仅音频 |

---

## 二、通用下载方法

### yt-dlp 基础命令

```bash
# 最简单的下载
yt-dlp "视频链接"

# 下载最佳质量
yt-dlp -f "bestvideo+bestaudio" --merge-format mp4 "视频链接"

# 下载并自动合并字幕
yt-dlp --write-subs --sub-langs zh-Hans,en "视频链接"

# 仅下载音频
yt-dlp -x --audio-format mp3 "视频链接"

# 使用浏览器 Cookie (需要登录的视频)
yt-dlp --cookies-from-browser chrome "视频链接"

# 查看可用格式
yt-dlp -F "视频链接"
```

### 在 video2text 中使用

直接在 Web 界面的「视频URL」输入框粘贴链接，点击「下载视频」即可。

---

## 三、各平台详细教程

### B站 (bilibili)

```bash
# 方法1: yt-dlp (推荐)
yt-dlp "https://www.bilibili.com/video/BV1xxx"

# 方法2: lux (速度更快)
lux "https://www.bilibili.com/video/BV1xxx"

# 下载指定清晰度
yt-dlp -F "B站链接"  # 先查看可用格式
yt-dlp -f "格式ID" "B站链接"
```

### 小红书

video2text 已集成 XHS-Downloader，支持无水印下载。

**启动服务**:
```bash
./start-xhs-downloader.sh
```

**使用**:
1. 在 video2text 界面粘贴小红书链接
2. 自动识别并使用 XHS-Downloader 下载
3. 获取无水印视频

**支持链接格式**:
- `https://www.xiaohongshu.com/explore/作品ID`
- `https://xhslink.com/分享码`

### 抖音

```bash
# 需要先登录获取 Cookie
yt-dlp --cookies-from-browser chrome "抖音链接"

# 或使用专用工具
# https://github.com/Evil0ctal/Douyin_TikTok_Download_API
```

### 微博

```bash
# 直接下载
yt-dlp "https://weibo.com/xxx/xxx"

# 或微博视频链接
yt-dlp "https://weibo.com/tv/show/1034:xxx"
```

### YouTube

```bash
# 下载视频
yt-dlp "https://www.youtube.com/watch?v=xxx"

# 下载播放列表
yt-dlp -o "%(playlist_index)s-%(title)s.%(ext)s" "播放列表链接"

# 下载 4K 视频
yt-dlp -f "bestvideo[height<=2160]+bestaudio" "链接"

# 下载自动生成字幕
yt-dlp --write-auto-subs --sub-langs zh-Hans "链接"
```

### TikTok

```bash
# 下载无水印
yt-dlp "https://www.tiktok.com/@user/video/xxx"

# 如果提示 IP 被限制，使用代理
yt-dlp --proxy "http://127.0.0.1:7890" "TikTok链接"
```

### Instagram

```bash
# 需要 Cookie (登录状态)
yt-dlp --cookies-from-browser chrome "Instagram链接"

# 下载 Reels
yt-dlp --cookies-from-browser chrome "https://www.instagram.com/reel/xxx"
```

### Twitter/X

```bash
# 直接下载
yt-dlp "https://twitter.com/user/status/xxx"
yt-dlp "https://x.com/user/status/xxx"
```

### Spotify (仅音频)

```bash
# 安装 spotdl
pip install spotdl

# 下载单曲
spotdl "https://open.spotify.com/track/xxx"

# 下载播放列表
spotdl "https://open.spotify.com/playlist/xxx"
```

---

## 四、微信公众号/视频号下载

### 公众号文章视频

**方法1: 在线工具** (推荐)
- https://vtool.pro/wxmp.html
- https://greenvideo.cc/gzh
- https://www.135editor.com/tools/tool/sptq

**方法2: F12 开发者工具**
```
1. 电脑浏览器打开公众号文章
2. 按 F12 → Network 标签
3. 播放视频
4. 搜索 ".mp4" 或 "video"
5. 右键复制链接
6. 使用 yt-dlp 下载
```

### 视频号下载

**Linux**: 需要手动抓包或使用 Windows 虚拟机

**Windows/macOS**: 使用专用工具
- [wx_channels_download](https://github.com/ltaoo/wx_channels_download)
- [res-downloader](https://github.com/putyy/res-downloader)

**使用步骤**:
```
1. 下载并安装工具
2. 以管理员身份运行
3. 打开微信 PC 端
4. 播放视频号视频
5. 点击下载按钮
```

---

## 五、常见问题

### Q1: 下载失败提示需要登录?

使用浏览器 Cookie:
```bash
yt-dlp --cookies-from-browser chrome "链接"
```

### Q2: 下载速度慢?

使用代理:
```bash
yt-dlp --proxy "http://127.0.0.1:7890" "链接"
```

### Q3: 如何下载会员视频?

需要有效会员账号的 Cookie:
```bash
yt-dlp --cookies-from-browser chrome "链接"
```

### Q4: 下载的视频没有声音?

使用 FFmpeg 合并:
```bash
ffmpeg -i video.mp4 -i audio.m4a -c copy output.mp4
```

或让 yt-dlp 自动合并:
```bash
yt-dlp -f "bestvideo+bestaudio" --merge-format mp4 "链接"
```

---

## 六、工具安装

```bash
# yt-dlp
pip install yt-dlp

# lux (B站推荐)
# 从 https://github.com/iawia002/lux/releases 下载

# spotdl (Spotify)
pip install spotdl

# FFmpeg (必需)
apt install ffmpeg  # Ubuntu/Debian
brew install ffmpeg # macOS
```

---

*更新时间: 2026-03-23*
