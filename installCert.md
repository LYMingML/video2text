# mkcert 安装与证书导入记录

## 已完成工作

- 已确认系统可用 `winget`。
- 已安装 `mkcert`（版本 `v1.4.4`）。
- 已执行 `mkcert -install`，完成本机本地 CA 信任安装。
- 已检查证书文件 `./Download/video2text.pem` 并确认其为 CA 证书。
- 已将证书导入“当前用户 -> 受信任的根证书颁发机构（Root）”。
- 已完成结果验证（`mkcert --version` 与证书存储查询）。

## 一次性执行命令（可直接粘贴）

> 在当前 Windows 用户目录（`$HOME`）下执行；命令路径均为相对路径。

```powershell
# ===== 0) 可选：确保当前目录为当前 Windows 用户目录 =====
Set-Location $HOME

# ===== 1) 检查环境 =====
winget --version
Get-Command mkcert -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source

# ===== 2) 安装 mkcert =====
winget install --id FiloSottile.mkcert -e --accept-source-agreements --accept-package-agreements

# ===== 3) 初始化本机信任链（安装本地 CA） =====
mkcert -install

# ===== 3.1) 可选兜底：若当前会话无法识别 mkcert，则使用相对路径执行 =====
.\AppData\Local\Microsoft\WinGet\Packages\FiloSottile.mkcert_Microsoft.Winget.Source_8wekyb3d8bbwe\mkcert.exe -install

# ===== 4) 检查证书文件并查看内容 =====
Test-Path ".\Download\video2text.pem"
certutil -dump ".\Download\video2text.pem"

# ===== 5) 导入证书到当前用户 Root 存储 =====
certutil -user -addstore Root ".\Download\video2text.pem"

# ===== 6) 验证安装结果 =====
mkcert --version
certutil -user -store Root 17bfebadb1d03e07214f4758eaa5419c42733236
```
