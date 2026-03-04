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
# Win11 证书安装指南（video2text HTTPS）

本文说明如何让 Win11 浏览器信任 Linux 端 `video2text` 的 HTTPS 证书。

---

## 1. 先确认你使用哪种证书

### 场景 A：使用 mkcert（推荐）

- Linux 生成服务证书时使用了 `mkcert`
- Win11 需要导入 **mkcert 根证书**（Root CA）

### 场景 B：使用 openssl 自签

- Linux 直接生成了自签 `video2text.pem`
- Win11 需要导入 **video2text.pem** 到根证书仓库

---

## 2. 从 Linux 拷贝证书到 Win11

只复制公钥证书，不复制私钥。

- 允许复制：`video2text.pem`、`rootCA.pem`
- 禁止复制：`video2text-key.pem`、`rootCA-key.pem`

建议放在 Win11 本机：

`C:\Users\<你的用户名>\Downloads\`

---

## 3. Win11 导入证书（管理员 PowerShell）

### 3.1 导入 mkcert 根证书（场景 A）

```powershell
$rootCert = "C:\Users\<你的用户名>\Downloads\rootCA.pem"
Import-Certificate -FilePath $rootCert -CertStoreLocation Cert:\LocalMachine\Root
```

### 3.2 导入自签服务证书（场景 B）

```powershell
$serverCert = "C:\Users\<你的用户名>\Downloads\video2text.pem"
Import-Certificate -FilePath $serverCert -CertStoreLocation Cert:\LocalMachine\Root
```

---

## 4. 验证是否导入成功

```powershell
Get-ChildItem Cert:\LocalMachine\Root |
	Where-Object { $_.Subject -like "*192.168.1.2*" -or $_.Subject -like "*mkcert*" } |
	Select-Object Subject, Thumbprint, NotAfter
```

如果使用域名访问，把匹配条件改为你的域名关键字。

---

## 5. 刷新浏览器并访问

1. 关闭所有浏览器窗口后重开
2. 访问：`https://192.168.1.2:7880`
3. 若仍提示不安全，执行：
	 - `Win + R` → `inetcpl.cpl`
	 - 内容 → 清除 SSL 状态

---

## 6. 常见问题

### Q1：证书已导入，仍不安全

- 访问地址不在证书 SAN 中（最常见）
- 导入位置错误（应为 `LocalMachine\Root`）
- 浏览器缓存未刷新

### Q2：mkcert 场景为什么要导入 rootCA，而不是只导入 video2text.pem？

因为服务证书是由 mkcert 根证书签发，浏览器要先信任签发者。

### Q3：为什么绝不能复制 `*-key.pem`？

私钥泄露后，任何人都可伪造你的 HTTPS 站点证书。
