# video2text

视频/音频转字幕工具，支持 ASR 自动语音识别和 AI 翻译。

## 主要特性

- **三 ASR 后端**: VibeVoice (说话人分离) / FunASR (中文优化) / faster-whisper (多语言)
- **AI 翻译**: OpenAI 兼容 API，支持 SiliconFlow、DeepSeek 等翻译服务
- **视频下载**: 支持 YouTube、Bilibili、小红书、抖音、快手、微博、知乎、优酷、爱奇艺、腾讯视频、 TikTok、Instagram、Twitter/X、Facebook、Vimeo、Spotify 等国内外平台
- **GPU 加速**: 支持 NVIDIA GPU (Pascal+) 和 Intel GPU (FunASR)
- **自动字幕**: 优先使用平台提供的字幕，自动跳过 ASR
- **说话人分离**: VibeVoice 后端支持多说话人识别和分离

## 硬件要求

- GPU: NVIDIA Pascal+ (推荐 ≥6GB VRAM)
- 内存: 8GB+
- 系统: Linux (Ubuntu 20.04+)

## 支持的 ASR 模型

| 后端 | 模型 | 特点 |
|------|------|------|
| VibeVoice | VibeVoice-ASR-7B/9B | 说话人分离，4-bit/8-bit 量化 |
| FunASR | paraformer-zh | 中文优化 |
| faster-whisper | medium | 多语言支持 |

## 许可

MIT License
