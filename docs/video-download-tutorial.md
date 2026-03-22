# 各平台视频下载教程

本文档介绍如何从各平台获取视频 URL，然后在 video2text 中下载并转录。

---

## 快速使用

1. **获取视频链接** - 从各平台复制视频 URL（见下方各平台教程）
2. **粘贴到 video2text** - 在「视频URL」输入框粘贴链接
3. **点击「下载目标URL」** - 视频将下载到临时目录
4. **选择是否保存** - 下载完成后可选择是否保存到本地
5. **点击「开始转录」** - 自动使用已下载的视频进行转录

---

## 各平台获取 URL 方法

### B站 (bilibili)

**获取方法**：
1. 打开视频页面
2. 复制浏览器地址栏链接
3. 链接格式：`https://www.bilibili.com/video/BV1xxx`

**支持格式**：
- `https://www.bilibili.com/video/BV1xxx`
- `https://www.bilibili.com/video/avxxx`
- `https://b23.tv/xxx`（短链接）

---

### 小红书

**获取方法**：
1. 打开小红书 App 或网页版
2. 点击分享按钮 → 复制链接
3. 链接格式：`https://www.xiaohongshu.com/explore/xxx`

**支持格式**：
- `https://www.xiaohongshu.com/explore/作品ID`
- `https://www.xiaohongshu.com/discovery/item/作品ID`
- `https://xhslink.com/xxx`（短链接）

---

### 抖音

**获取方法**：
1. 打开抖音 App
2. 点击分享按钮 → 复制链接
3. 链接格式：`https://www.douyin.com/video/xxx`

**支持格式**：
- `https://www.douyin.com/video/xxx`
- `https://v.douyin.com/xxx`（短链接）

**注意**：部分视频需要登录才能下载

---

### 快手

**获取方法**：
1. 打开快手 App
2. 点击分享按钮 → 复制链接
3. 链接格式：`https://www.kuaishou.com/short-video/xxx`

---

### 微博

**获取方法**：
1. 打开微博视频页面
2. 复制浏览器地址栏链接
3. 链接格式：`https://weibo.com/xxx/xxx` 或 `https://weibo.com/tv/show/1034:xxx`

---

### 知乎

**获取方法**：
1. 打开知乎视频页面
2. 复制浏览器地址栏链接
3. 链接格式：`https://www.zhihu.com/zvideo/xxx`

---

### YouTube

**获取方法**：
1. 打开 YouTube 视频页面
2. 复制浏览器地址栏链接
3. 链接格式：`https://www.youtube.com/watch?v=xxx`

**支持格式**：
- `https://www.youtube.com/watch?v=xxx`
- `https://youtu.be/xxx`（短链接）
- 播放列表：`https://www.youtube.com/playlist?list=xxx`

---

### TikTok

**获取方法**：
1. 打开 TikTok 视频页面
2. 点击分享 → 复制链接
3. 链接格式：`https://www.tiktok.com/@user/video/xxx`

**注意**：可能需要代理访问

---

### Instagram

**获取方法**：
1. 打开 Instagram 视频/Reels 页面
2. 复制浏览器地址栏链接
3. 链接格式：`https://www.instagram.com/reel/xxx` 或 `https://www.instagram.com/p/xxx`

**注意**：需要登录账号的 Cookie

---

### Twitter/X

**获取方法**：
1. 打开推文页面
2. 复制浏览器地址栏链接
3. 链接格式：`https://twitter.com/user/status/xxx` 或 `https://x.com/user/status/xxx`

---

### Facebook

**获取方法**：
1. 打开 Facebook 视频页面
2. 复制浏览器地址栏链接
3. 链接格式：`https://www.facebook.com/watch?v=xxx`

---

### 公众号文章视频

**获取方法**：
1. 在电脑浏览器打开公众号文章
2. 按 F12 打开开发者工具
3. 播放视频
4. 在 Network 标签搜索 `.mp4`
5. 右键复制视频链接

**或使用在线工具**：
- https://vtool.pro/wxmp.html
- https://greenvideo.cc/gzh

---

### 视频号

**获取方法**（需要 Windows）：
1. 安装 [wx_channels_download](https://github.com/ltaoo/wx_channels_download)
2. 以管理员身份运行
3. 在微信 PC 端播放视频号视频
4. 点击下载按钮获取视频

---

## 常见问题

### Q: 下载失败怎么办？

1. **检查网络** - 确保能访问目标平台
2. **更新 yt-dlp** - `pip install -U yt-dlp`
3. **使用代理** - 配置 `.env` 中的 `HTTP_PROXY`
4. **需要登录** - 部分平台需要 Cookie

### Q: 如何下载需要登录的视频？

在 `.env` 文件中配置：
```bash
# 使用浏览器 Cookie
YT_DLP_COOKIES_FROM_BROWSER=chrome
```

### Q: 下载的视频在哪里？

- **临时目录**：`workspace/temp_video/`
- **点击保存后**：浏览器默认下载目录

---

*更新时间: 2026-03-23*
