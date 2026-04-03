#!/usr/bin/env python3
"""视频下载工具测试脚本"""
import subprocess
import shutil
import sys

def check_tool(name, cmd):
    """检查工具是否可用"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print(f"✅ {name}: 可用")
            return True
        else:
            print(f"❌ {name}: 不可用 (返回码 {result.returncode})")
            return False
    except FileNotFoundError:
        print(f"❌ {name}: 未安装")
        return False
    except subprocess.TimeoutExpired:
        print(f"❌ {name}: 超时")
        return False
    except Exception as e:
        print(f"❌ {name}: 错误 - {e}")
        return False

def main():
    print("=" * 60)
    print("视频下载工具测试")
    print("=" * 60)
    
    tools = [
        ("yt-dlp", ["yt-dlp", "--version"]),
        ("you-get", ["you-get", "--version"]),
        ("gallery-dl", ["gallery-dl", "--version"]),
        ("spotdl", ["spotdl", "--version"]),
        ("duckduckgo-mcp-server", ["duckduckgo-mcp-server", "--help"]),
        ("lux", ["lux", "--version"]),
        ("BBDown", ["BBDown", "--version"]),
        ("ffmpeg", ["ffmpeg", "-version"]),
    ]
    
    available = 0
    for name, cmd in tools:
        if check_tool(name, cmd):
            available += 1
    
    print("=" * 60)
    print(f"可用工具: {available}/{len(tools)}")
    print("=" * 60)

if __name__ == "__main__":
    main()
