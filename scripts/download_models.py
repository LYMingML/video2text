#!/usr/bin/env python
"""
预下载核心模型到缓存目录。

在 Docker 构建时运行，下载三个核心模型：
- paraformer-zh: FunASR 中文 Paraformer
- iic/SenseVoiceSmall: SenseVoice 小模型
- faster-whisper small: Whisper small 模型

其他模型在首次使用时按需下载。
"""
import os
import sys

def download_funasr_models():
    """下载 FunASR 核心模型"""
    print("=" * 50)
    print("下载 FunASR 核心模型...")
    print("=" * 50)

    from funasr import AutoModel

    models = [
        ("paraformer-zh", "中文 Paraformer"),
        ("iic/SenseVoiceSmall", "SenseVoice 小模型"),
    ]

    for model_name, desc in models:
        print(f"\n[1/2] 下载 {model_name} ({desc})...")
        try:
            # 加载模型会自动下载到 MODELSCOPE_CACHE
            model = AutoModel(
                model=model_name,
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
            print(f"✓ {model_name} 下载完成")
        except Exception as e:
            print(f"✗ {model_name} 下载失败: {e}")
            return False

    return True


def download_whisper_models():
    """下载 faster-whisper 核心模型"""
    print("\n" + "=" * 50)
    print("下载 faster-whisper 核心模型...")
    print("=" * 50)

    from faster_whisper import WhisperModel

    models = ["small"]

    for model_name in models:
        print(f"\n下载 Whisper {model_name}...")
        try:
            # 加载模型会自动下载到 HF_HOME
            model = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
            )
            del model
            print(f"✓ Whisper {model_name} 下载完成")
        except Exception as e:
            print(f"✗ Whisper {model_name} 下载失败: {e}")
            return False

    return True


def main():
    print("=" * 50)
    print("video2text 模型预下载脚本")
    print("=" * 50)

    # 显示缓存目录
    print(f"\n缓存目录:")
    print(f"  MODELSCOPE_CACHE: {os.environ.get('MODELSCOPE_CACHE', '~/.cache/modelscope')}")
    print(f"  HF_HOME: {os.environ.get('HF_HOME', '~/.cache/huggingface')}")

    success = True

    # 下载 FunASR 模型
    try:
        if not download_funasr_models():
            success = False
    except ImportError as e:
        print(f"FunASR 未安装，跳过: {e}")

    # 下载 Whisper 模型
    try:
        if not download_whisper_models():
            success = False
    except ImportError as e:
        print(f"faster-whisper 未安装，跳过: {e}")

    print("\n" + "=" * 50)
    if success:
        print("✓ 核心模型下载完成")
    else:
        print("✗ 部分模型下载失败")
    print("=" * 50)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
