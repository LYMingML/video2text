#!/usr/bin/env python
"""
预下载核心模型到缓存目录。

优化版：只下载 paraformer-zh 核心模型（~1GB）
其他模型（SenseVoice、Whisper）运行时按需下载
"""
import os
import sys


def download_funasr_core():
    """只下载 paraformer-zh 核心模型"""
    print("=" * 50)
    print("下载 paraformer-zh 核心模型...")
    print("=" * 50)

    from funasr import AutoModel

    try:
        # 加载模型会自动下载到 MODELSCOPE_CACHE
        model = AutoModel(
            model="paraformer-zh",
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            punc_model="ct-punc",
            device="cpu",
            disable_update=True,
            hub="ms",
        )
        # 触发模型加载
        _ = model.model
        del model
        print("✓ paraformer-zh 核心模型下载完成")
        return True
    except Exception as e:
        print(f"✗ 模型下载失败: {e}")
        # 允许失败，运行时可以再次尝试下载
        return True


def main():
    print("=" * 50)
    print("video2text 模型预下载脚本 (优化版)")
    print("=" * 50)

    # 显示缓存目录
    print(f"\n缓存目录:")
    print(f"  MODELSCOPE_CACHE: {os.environ.get('MODELSCOPE_CACHE', '~/.cache/modelscope')}")
    print(f"  HF_HOME: {os.environ.get('HF_HOME', '~/.cache/huggingface')}")

    # 只下载 FunASR 核心模型
    try:
        download_funasr_core()
    except ImportError as e:
        print(f"FunASR 未安装，跳过: {e}")

    print("\n" + "=" * 50)
    print("✓ 核心模型下载完成")
    print("提示: SenseVoice/Whisper 模型将在运行时按需下载")
    print("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
