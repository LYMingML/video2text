# video2text

本项目是一个在 Linux 服务器运行的本地 WebUI，用于将视频/音频转为字幕与纯文本，支持 GPU 加速、历史任务管理、停止转录、HTTPS 访问。

## 主要功能

- 视频/音频上传与历史文件复用
- 双后端：FunASR / faster-whisper（手动切换）
- 分片流式转录进度显示（长音频可见实时进度）
- 一键停止当前转录任务
- 输出 SRT 与纯文本，时间轴自动清洗
- 历史文件夹空间统计与删除
- 支持 HTTP/HTTPS 启动（`main.sh`）

## 环境要求

- Linux（推荐 Ubuntu）
- Python 3.12
- ffmpeg / ffprobe
- NVIDIA GPU（可选）

> Tesla P4 建议：`faster-whisper` 使用 `int8`。

## 快速安装

```bash
cd /home/lym/projects/video2text

# 1) 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2) 安装依赖
pip install -U pip
pip install -e .

# 3) 安装 PyTorch（Tesla P4 / sm_61 推荐）
pip install "torch==2.3.1+cu121" "torchaudio==2.3.1+cu121" --index-url https://download.pytorch.org/whl/cu121
```

## 启动与重启

项目已封装启动脚本：`main.sh`

```bash
cd /home/lym/projects/video2text
chmod +x main.sh

./main.sh auto    # 自动：有证书走 HTTPS，无证书走 HTTP
./main.sh http    # 强制 HTTP
./main.sh https   # 强制 HTTPS（需证书）
```

默认端口：`7880`

## HTTPS 证书生成（Linux 侧）

项目支持两种方式：

### 方式 A：mkcert（推荐）

```bash
sudo apt update
sudo apt install -y libnss3-tools mkcert
mkcert -install

cd /home/lym/projects/video2text
mkcert -cert-file video2text.pem -key-file video2text-key.pem 192.168.1.2 localhost 127.0.0.1
./main.sh https
```

### 方式 B：openssl（临时自签）

```bash
cd /home/lym/projects/video2text
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
	-keyout video2text-key.pem \
	-out video2text.pem \
	-subj "/CN=192.168.1.2" \
	-addext "subjectAltName=IP:192.168.1.2,IP:127.0.0.1,DNS:localhost"

./main.sh https
```

## Win11 证书安装（HTTPS 信任）

本文档已内置 Win11 客户端证书信任步骤，用于访问 Linux 端 `video2text` 的 HTTPS 页面。

### 1) 先确认你的证书类型

- 场景 A（推荐）：Linux 使用 `mkcert` 生成服务证书
	- Win11 需导入 `mkcert` 的根证书 `rootCA.pem`
- 场景 B：Linux 使用 `openssl` 自签 `video2text.pem`
	- Win11 需导入 `video2text.pem` 到根证书仓库

### 2) 从 Linux 拷贝证书到 Win11

只复制公钥证书，不复制私钥：

- 可复制：`video2text.pem`、`rootCA.pem`
- 禁止复制：`video2text-key.pem`、`rootCA-key.pem`

建议放在：`C:\Users\<你的用户名>\Downloads\`

### 3) 在 Win11 导入证书（管理员 PowerShell）

导入 mkcert 根证书（场景 A）：

```powershell
$rootCert = "C:\Users\<你的用户名>\Downloads\rootCA.pem"
Import-Certificate -FilePath $rootCert -CertStoreLocation Cert:\LocalMachine\Root
```

导入 openssl 自签证书（场景 B）：

```powershell
$serverCert = "C:\Users\<你的用户名>\Downloads\video2text.pem"
Import-Certificate -FilePath $serverCert -CertStoreLocation Cert:\LocalMachine\Root
```

### 4) 验证导入成功

```powershell
Get-ChildItem Cert:\LocalMachine\Root |
	Where-Object { $_.Subject -like "*192.168.1.2*" -or $_.Subject -like "*mkcert*" } |
	Select-Object Subject, Thumbprint, NotAfter
```

若你使用域名访问，请改为匹配域名关键字。

### 5) 刷新浏览器并访问

1. 关闭浏览器并重开
2. 访问：`https://192.168.1.2:7880`
3. 若仍提示不安全：
	 - `Win + R` → `inetcpl.cpl`
	 - 内容 → 清除 SSL 状态

### 6) 常见问题

- 已导入但仍不安全：通常是访问地址不在证书 SAN，或导入仓库错误
- mkcert 为何导入 `rootCA.pem`：服务证书由根证书签发，需先信任签发者
- 为何不能复制 `*-key.pem`：私钥泄露会导致证书可被伪造

## 模型建议（简版）

- 中文优先：`paraformer-zh`
- 日语/韩语/粤语：优先尝试 `iic/SenseVoiceSmall`
- 西班牙语：建议 `faster-whisper`（项目内已做自动回退）
- 多语言高质量：`faster-whisper large-v3`（更慢）

## 常见命令

```bash
# 语法检查
.venv/bin/python -m py_compile main.py backend/funasr_backend.py backend/whisper_backend.py utils/audio.py utils/subtitle.py

# 查看监听端口
ss -tlnp | grep 7880

# 停止服务
pkill -f "main.py"
```

## 仓库结构

```text
video2text/
├── main.py
├── main.sh
├── backend/
├── utils/
├── workspace/
├── design.md
└── video2text.service
```

## 详细设计

- [design.md](design.md)
