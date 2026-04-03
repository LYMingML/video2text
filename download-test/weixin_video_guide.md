# 微信视频下载完整指南

## 一、微信公众号视频下载

### 方法1: 在线工具 (推荐)

| 工具 | 网址 | 状态 |
|------|------|------|
| vtool | https://vtool.pro/wxmp.html | ✅ |
| GreenVideo | https://greenvideo.cc/gzh | ✅ |
| 135编辑器 | https://www.135editor.com/tools/tool/sptq | ✅ |
| 96编辑器 | https://bj.96weixin.com/tools/wechat_video | ✅ |

**使用步骤**:
```
1. 复制公众号文章链接 (https://mp.weixin.qq.com/s/...)
2. 粘贴到在线工具输入框
3. 点击提取/解析
4. 获取视频下载链接
5. 使用浏览器或 yt-dlp 下载
```

### 方法2: 浏览器开发者工具

```
1. 电脑浏览器打开公众号文章
2. 按 F12 打开开发者工具
3. 切换到 Network (网络) 标签
4. 勾选 "Preserve log" (保留日志)
5. 播放视频
6. 在 Filter 中输入 "video" 或 ".mp4"
7. 找到视频请求，右键 → Copy → Copy link
8. 使用 yt-dlp 下载:
   yt-dlp "复制的链接"
```

### 方法3: Python 脚本

```python
# 见 gzh_video_downloader.py
from gzh_video_downloader import extract_gzh_article_info

result = extract_gzh_article_info("公众号文章链接")
for video in result['videos']:
    print(f"视频: {video}")
```

### 方法4: yt-dlp 直接下载

部分公众号视频是腾讯视频，可以直接用 yt-dlp:
```bash
yt-dlp "https://v.qq.com/x/page/VIDEO_ID.html"
```

---

## 二、微信视频号视频下载

### 限制说明

视频号下载**只能在 Windows/macOS 上进行**，原因:
1. 需要安装系统 CA 证书
2. 需要代理微信客户端流量
3. 需要解密视频数据

### 方法1: wx_channels_download (推荐)

**GitHub**: https://github.com/ltaoo/wx_channels_download

**支持平台**: Windows, macOS

**使用步骤**:
```
1. 从 GitHub Releases 下载对应版本
2. 以管理员身份运行 (首次需要安装证书)
3. 打开微信 PC 端
4. 访问要下载的视频号视频
5. 点击视频下方的"下载"按钮
6. 选择视频质量进行下载
```

**Linux 用户**:
- 使用 Wine 运行 Windows 版
- 使用 Windows 虚拟机

### 方法2: res-downloader

**GitHub**: https://github.com/putyy/res-downloader

**功能**:
- 视频号视频下载
- 直播回放下载
- 多图下载

### 方法3: 手动抓包 (Linux 可用)

```bash
# 1. 安装 mitmproxy
pip install mitmproxy

# 2. 启动代理
mitmdump -p 8080

# 3. 配置手机代理
# 手机 WiFi 设置代理为电脑 IP:8080

# 4. 安装证书
# 手机浏览器访问 mitm.it 安装证书

# 5. 播放视频号视频
# mitmdump 会捕获所有请求

# 6. 过滤视频请求
# 在捕获的请求中查找 .mp4 或视频 URL
```

---

## 三、测试结果

### 已测试可用

| 类型 | 方法 | 状态 |
|------|------|------|
| 公众号视频 | 在线工具 | ✅ |
| 公众号视频 | F12 开发者工具 | ✅ |
| 公众号视频 | yt-dlp (腾讯视频) | ✅ |

### 未测试 (需要 Windows)

| 类型 | 工具 | 平台 |
|------|------|------|
| 视频号 | wx_channels_download | Windows/macOS |
| 视频号 | res-downloader | Windows |

---

## 四、快速命令

```bash
# 公众号视频 (腾讯视频源)
yt-dlp "https://v.qq.com/x/page/VIDEO_ID.html"

# 下载并转换为 mp3
yt-dlp -x --audio-format mp3 "视频链接"

# 下载最佳质量
yt-dlp -f "best" "视频链接"

# 使用浏览器 Cookie (如果需要登录)
yt-dlp --cookies-from-browser chrome "视频链接"
```

