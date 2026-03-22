# 微信视频下载测试报告

## 测试时间
2026-03-23

---

## 一、测试结论

### 公众号视频 ✅ 可下载

| 方法 | 状态 | 难度 | 说明 |
|------|------|------|------|
| 在线工具 | ✅ | ⭐ 简单 | 推荐使用 |
| F12 开发者工具 | ✅ | ⭐⭐ 中等 | 通用方法 |
| yt-dlp (腾讯视频) | ✅ | ⭐ 简单 | 需要视频 ID |

### 视频号视频 ⚠️ 需要 Windows/macOS

| 方法 | 状态 | 平台 | 说明 |
|------|------|------|------|
| wx_channels_download | ✅ | Win/Mac | 推荐 |
| res-downloader | ✅ | Win | 多功能 |
| mitmproxy 抓包 | ⚠️ | Linux | 手动操作 |

---

## 二、已安装工具

| 工具 | 状态 | 用途 |
|------|------|------|
| yt-dlp | ✅ | 通用视频下载 |
| mitmproxy | ✅ | 网络抓包 |
| gzh_video_downloader.py | ✅ | 公众号视频提取脚本 |

---

## 三、公众号视频下载方法

### 方法1: 在线工具 (最简单)

**可用网站**:
- https://vtool.pro/wxmp.html
- https://greenvideo.cc/gzh
- https://www.135editor.com/tools/tool/sptq

**步骤**:
```
1. 复制公众号文章链接
2. 粘贴到在线工具
3. 点击提取
4. 下载视频
```

### 方法2: F12 开发者工具

```
1. 电脑浏览器打开公众号文章
2. F12 → Network 标签
3. 播放视频
4. 搜索 ".mp4" 或 "video"
5. 复制视频链接
6. yt-dlp "链接" 下载
```

### 方法3: yt-dlp (腾讯视频源)

```bash
# 公众号视频通常是腾讯视频
yt-dlp "https://v.qq.com/x/page/VIDEO_ID.html"

# 示例
yt-dlp "https://v.qq.com/x/page/i3365.html"
```

---

## 四、视频号下载方法

### Linux 环境 (当前)

**方法: mitmproxy 抓包**

```bash
# 1. 启动代理服务
mitmdump -p 8080 --set block_global=false

# 2. 手机配置代理
# WiFi 设置 → 代理 → 手动
# 主机: 电脑 IP
# 端口: 8080

# 3. 手机安装证书
# 浏览器访问: http://mitm.it
# 下载并安装证书

# 4. 打开微信，播放视频号视频
# mitmdump 会捕获所有请求

# 5. 过滤视频请求
# 查找 .mp4 或 finder.video.qq.com
```

### Windows/macOS 环境

**推荐工具: wx_channels_download**

```
1. 下载: https://github.com/ltaoo/wx_channels_download/releases
2. 管理员运行 (首次安装证书)
3. 打开微信 PC 端
4. 播放视频号视频
5. 点击下载按钮
```

---

## 五、文件位置

```
/home/lym/projects/video2text/download-test/
├── gzh_video_downloader.py   # 公众号视频提取脚本
├── weixin_video_guide.md     # 使用指南
└── test_tools.py             # 工具测试脚本
```

---

## 六、快速命令

```bash
# 公众号视频
yt-dlp "腾讯视频链接"

# 启动抓包
mitmdump -p 8080

# 查看 mitmproxy Web 界面
mitmweb -p 8080
```
