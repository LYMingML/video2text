#!/usr/bin/env python3
"""
微信公众号视频下载工具
支持从公众号文章链接中提取视频
"""

import re
import json
import requests
from urllib.parse import urlparse, parse_qs


def extract_gzh_article_info(url: str) -> dict:
    """提取公众号文章信息"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        
        html = resp.text
        
        # 提取视频信息的方法
        videos = []
        
        # 方法1: 查找 data-src 或 data-mpvid
        # 公众号视频通常以 <mpvideo> 或 iframe 形式嵌入
        
        # 查找腾讯视频 ID
        tencent_pattern = r'vid=(\w+)'
        for match in re.finditer(tencent_pattern, html):
            vid = match.group(1)
            if len(vid) > 5:  # 过滤掉短的无意义匹配
                videos.append({
                    'type': 'tencent',
                    'vid': vid,
                    'url': f'https://v.qq.com/x/page/{vid}.html'
                })
        
        # 查找 data-mpvid
        mpvid_pattern = r'data-mpvid\s*=\s*["\']([^"\']+)["\']'
        for match in re.finditer(mpvid_pattern, html):
            mpvid = match.group(1)
            videos.append({
                'type': 'mpvideo',
                'mpvid': mpvid,
            })
        
        # 查找 iframe 嵌入的视频
        iframe_pattern = r'<iframe[^>]+src=["\']([^"\']*(?:video|v\.qq|mpvideo)[^"\']*)["\']'
        for match in re.finditer(iframe_pattern, html, re.IGNORECASE):
            iframe_url = match.group(1)
            videos.append({
                'type': 'iframe',
                'url': iframe_url
            })
        
        # 提取文章标题
        title_match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
        title = title_match.group(1) if title_match else "未知标题"
        
        return {
            'title': title,
            'videos': videos,
            'success': True
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


def get_tencent_video_url(vid: str) -> str:
    """获取腾讯视频下载地址"""
    # 腾讯视频信息接口
    api_url = f'https://vv.video.qq.com/getinfo?vids={vid}&platform=101001&charge=0&otype=json'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        resp = requests.get(api_url, headers=headers, timeout=10)
        # 返回的是 JSONP 格式，需要解析
        text = resp.text
        # QZOutputJson=... 格式
        if text.startswith('QZOutputJson='):
            json_str = text[14:-1]  # 去掉 QZOutputJson= 和最后的分号
            data = json.loads(json_str)
            
            if 'vl' in data and 'vi' in data['vl'] and data['vl']['vi']:
                video_info = data['vl']['vi'][0]
                return {
                    'title': video_info.get('ti', ''),
                    'duration': video_info.get('td', 0),
                    'vid': vid,
                }
    except Exception as e:
        return None
    
    return None


def main():
    print("=" * 60)
    print("微信公众号视频提取工具测试")
    print("=" * 60)
    
    # 测试用的公众号文章链接 (示例)
    test_urls = [
        # 这里需要真实的公众号文章链接
        # "https://mp.weixin.qq.com/s/xxxxxx",
    ]
    
    print("\n使用方法:")
    print("1. 获取公众号文章链接 (https://mp.weixin.qq.com/s/...)")
    print("2. 调用 extract_gzh_article_info(url) 提取视频信息")
    print("3. 使用 yt-dlp 下载视频")
    
    print("\n" + "=" * 60)
    print("在线工具推荐:")
    print("=" * 60)
    print("1. https://vtool.pro/wxmp.html")
    print("2. https://greenvideo.cc/gzh")
    print("3. https://www.135editor.com/tools/tool/sptq")
    
    print("\n" + "=" * 60)
    print("手动提取方法:")
    print("=" * 60)
    print("""
1. 在电脑浏览器打开公众号文章
2. 按 F12 打开开发者工具
3. 切换到 Network 标签
4. 播放视频
5. 在网络请求中搜索 .mp4 或 'video'
6. 找到视频真实地址后右键复制
7. 使用 yt-dlp 下载:
   yt-dlp "视频地址"
""")


if __name__ == "__main__":
    main()
