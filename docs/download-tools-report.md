# 视频下载工具测试报告

## 测试时间
2026-03-22

## 测试环境
- Python 3.13
- Linux WSL2
- 已安装工具目录: `/home/lym/tools/`

---

## 一、工具安装状态

### 已安装并可用

| 工具 | 版本 | 大小 | 状态 | 测试结果 |
|------|------|------|------|----------|
| yt-dlp | 2026.03.17 | 26MB | ✅ | YouTube/B站/微博 正常 |
| you-get | 0.4.1743 | 1.8MB | ✅ | B站正常 |
| gallery-dl | 1.31.10 | 7MB | ✅ | 图片采集正常 |
| spotdl | 4.4.3 | 1.1MB | ✅ | Spotify 专用 |
| lux | 0.24.1 | 8MB | ✅ | B站正常 |
| duckduckgo-mcp-server | 0.1.2 | 68KB | ✅ | MCP 搜索 |
| ffmpeg | - | - | ✅ | 转码/合并 |
| XHS-Downloader | - | 11MB | ✅ | 小红书专用 |
| MediaCrawler | - | 42MB | ⚠️ | 需配置 |
| Douyin_TikTok_Download_API | - | 8MB | ⚠️ | 需启动服务 |

### 未安装

| 工具 | 原因 | 替代方案 |
|------|------|----------|
| BBDown | 需要 .NET 运行时 | 使用 yt-dlp/lux |
| wx_channels_download | 仅支持 Windows/Mac | 抓包方式 |

---

## 二、平台下载测试结果

### 国内平台

| 平台 | yt-dlp | you-get | lux | 专用工具 | 备注 |
|------|--------|---------|-----|----------|------|
| **B站** | ✅ | ✅ | ✅ | BBDown | 全工具支持 |
| **抖音** | ⚠️ | - | - | Douyin_TikTok_Download_API | 需 Cookie |
| **小红书** | ⚠️ | - | - | XHS-Downloader ✅ | 专用工具更稳定 |
| **快手** | ✅ | - | - | MediaCrawler | yt-dlp 可用 |
| **微博** | ✅ | - | - | - | yt-dlp 正常 |
| **知乎** | ✅ | - | - | - | 视频可用 |
| **优酷** | ✅ | ✅ | - | - | 可能需要 Cookie |
| **爱奇艺** | ✅ | - | - | - | 可能需要会员 |
| **腾讯视频** | ✅ | - | - | - | 可能需要会员 |
| **公众号文章** | ⚠️ | - | - | 手动提取 | 见下方方法 |
| **视频号** | ⚠️ | - | - | wx_channels_download | 仅 Windows |

### 国外平台

| 平台 | yt-dlp | gallery-dl | 备注 |
|------|--------|------------|------|
| **YouTube** | ✅ | - | 完美支持 |
| **TikTok** | ⚠️ | - | IP 可能被限制 |
| **Instagram** | ✅ | ✅ | 需登录更佳 |
| **Twitter/X** | ✅ | ✅ | 完美支持 |
| **Facebook** | ✅ | - | 完美支持 |
| **Vimeo** | ✅ | - | 完美支持 |
| **Reddit** | ✅ | ✅ | 完美支持 |
| **Pinterest** | - | ✅ | gallery-dl 专用 |
| **Spotify** | spotdl ✅ | - | spotdl 专用 |

---

## 三、微信视频下载方法

### 方法1：视频号下载 (Windows/Mac)

**工具**: [wx_channels_download](https://github.com/ltaoo/wx_channels_download)

**步骤**:
```bash
# 1. 从 GitHub Releases 下载
# 2. 以管理员身份运行
# 3. 打开微信 PC 端，访问视频号
# 4. 点击视频下方的下载按钮
```

### 方法2：公众号视频提取 (通用)

**原理**: 公众号视频通常来自腾讯视频

**步骤**:
```
1. 在电脑浏览器打开公众号文章
2. 按 F12 打开开发者工具
3. 切换到 Network 标签
4. 播放视频
5. 搜索 .mp4 或 vkey
6. 找到视频真实地址
7. 右键复制链接，8. 使用 yt-dlp 下载:
   yt-dlp "复制的链接"
```

**自动化脚本** (油猴):
```javascript
// ==UserScript==
// @name         公众号视频下载
// @match        https://mp.weixin.qq.com/*
// @grant        none
// ==/UserScript==

document.querySelectorAll('iframe').forEach(iframe => {
    const src = iframe.src;
    if (src.includes('video') || src.includes('v.qq.com')) {
        console.log('视频链接:', src);
    }
});
```

### 方法3：res-downloader (通用资源嗅探)

**GitHub**: https://github.com/putyy/res-downloader

```
# 1. 安装
pip install res-downloader

# 2. 运行，监听网络
res-downloader

# 3. 在微信中播放视频
# 4. 自动捕获下载链接
```

---

## 四、磁盘空间占用

| 工具 | 本体 | 依赖 | 总计 |
|------|------|------|------|
| yt-dlp | 26MB | ~20MB | ~46MB |
| you-get | 1.8MB | ~5MB | ~7MB |
| gallery-dl | 7MB | ~15MB | ~22MB |
| spotdl | 1.1MB | ~30MB | ~31MB |
| lux | 8MB | - | 8MB |
| XHS-Downloader | 11MB | ~20MB | ~31MB |
| MediaCrawler | 42MB | ~50MB | ~92MB |
| Douyin_TikTok_Download_API | 8MB | ~30MB | ~38MB |
| **总计** | - | - | **~275MB** |

---

## 五、推荐使用方案

### 按平台选择

```python
DOWNLOAD_TOOLS = {
    # 国内平台
    "bilibili.com": ["yt-dlp", "lux", "you-get"],
    "douyin.com": ["Douyin_TikTok_Download_API"],
    "xiaohongshu.com": ["XHS-Downloader"],
    "xhslink.com": ["XHS-Downloader"],
    "kuaishou.com": ["yt-dlp", "MediaCrawler"],
    "weibo.com": ["yt-dlp"],
    "zhihu.com": ["yt-dlp"],
    "youku.com": ["yt-dlp", "you-get"],
    "iqiyi.com": ["yt-dlp"],
    "v.qq.com": ["yt-dlp"],

    # 国外平台
    "youtube.com": ["yt-dlp"],
    "youtu.be": ["yt-dlp"],
    "tiktok.com": ["yt-dlp", "Douyin_TikTok_Download_API"],
    "instagram.com": ["yt-dlp", "gallery-dl"],
    "twitter.com": ["yt-dlp", "gallery-dl"],
    "x.com": ["yt-dlp", "gallery-dl"],
    "facebook.com": ["yt-dlp"],
    "vimeo.com": ["yt-dlp"],
    "reddit.com": ["yt-dlp", "gallery-dl"],
    "pinterest.com": ["gallery-dl"],
    "open.spotify.com": ["spotdl"],
    "soundcloud.com": ["yt-dlp"],

    # 微信
    "mp.weixin.qq.com": ["manual"],  # 公众号手动提取
    "channels": ["wx_channels_download"],  # 视频号
}
```

### 默认回退

如果平台不在列表中，使用 **yt-dlp** 尝试。

---

## 六、快速命令参考

```bash
# ============ 基础下载 ============

# YouTube
yt-dlp -f "bestvideo+bestaudio" --merge-format mp4 "URL"

# B站
yt-dlp "B站链接"
lux "B站链接"

# 小红书 (需启动 API 服务)
cd /home/lym/projects/xhs-test/XHS-Downloader
python main.py --url "小红书链接"

# 微博
yt-dlp "微博链接"

# 抖音 (需 Cookie)
yt-dlp --cookies-from-browser chrome "抖音链接"

# Instagram
yt-dlp --cookies-from-browser chrome "Instagram链接"

# Twitter/X
yt-dlp "推特链接"

# TikTok
yt-dlp "TikTok链接"

# Spotify
spotdl "Spotify链接"

# 图片采集
gallery-dl "图片页面链接"


# ============ 高级选项 ============

# 下载播放列表
yt-dlp -o "%(playlist_index)s-%(title)s.%(ext)s" "播放列表URL"

# 下载字幕
yt-dlp --write-subs --sub-langs zh-Hans,en "URL"

# 使用代理
yt-dlp --proxy "http://127.0.0.1:7890" "URL"

# 使用浏览器 Cookie
yt-dlp --cookies-from-browser chrome "URL"

# 限速下载
yt-dlp -r 1M "URL"
```

---

## 七、集成到 video2text

已集成的工具:
- ✅ yt-dlp (通用下载)
- ✅ XHS-Downloader (小红书专用)

待集成的工具:
- [ ] Douyin_TikTok_Download_API (抖音)
- [ ] gallery-dl (图片)

---

*报告生成时间: 2026-03-22*
