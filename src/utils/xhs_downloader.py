"""
小红书视频下载工具 - 基于 XHS-Downloader API

用于从小红书平台下载无水印视频和图片。
支持通过 API 模式连接 XHS-Downloader 服务。
"""

import re
import time
import subprocess
import threading
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import httpx


# 小红书链接正则匹配
XHS_URL_PATTERNS = [
    # 标准链接
    r"https?://www\.xiaohongshu\.com/explore/[a-zA-Z0-9]+",
    r"https?://www\.xiaohongshu\.com/discovery/item/[a-zA-Z0-9]+",
    r"https?://www\.xiaohongshu\.com/user/profile/[a-zA-Z0-9]+/[a-zA-Z0-9]+",
    # 短链接
    r"https?://xhslink\.com/[a-zA-Z0-9]+",
    # 带 xsec_token 的链接
    r"https?://www\.xiaohongshu\.com/explore/[a-zA-Z0-9]+\?xsec_token=[a-zA-Z0-9_=]+",
]


@dataclass
class XHSDownloadResult:
    """下载结果"""
    success: bool
    file_path: Optional[str] = None
    file_name: Optional[str] = None
    error: Optional[str] = None
    note_id: Optional[str] = None
    note_title: Optional[str] = None
    author_name: Optional[str] = None


class XHSDownloaderClient:
    """XHS-Downloader API 客户端"""

    def __init__(self, api_url: str = "http://127.0.0.1:5556", timeout: int = 60):
        """
        初始化客户端

        Args:
            api_url: XHS-Downloader API 地址
            timeout: 请求超时时间（秒）
        """
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._server_process: Optional[subprocess.Popen] = None
        self._server_lock = threading.Lock()

    @staticmethod
    def is_xhs_url(url: str) -> bool:
        """
        判断 URL 是否为小红书链接

        Args:
            url: 待检测的 URL

        Returns:
            是否为小红书链接
        """
        url = url.strip()
        for pattern in XHS_URL_PATTERNS:
            if re.match(pattern, url, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def extract_note_id(url: str) -> Optional[str]:
        """
        从 URL 中提取笔记 ID

        Args:
            url: 小红书链接

        Returns:
            笔记 ID 或 None
        """
        url = url.strip()
        # 匹配 explore/ID 或 discovery/item/ID
        match = re.search(r"(?:explore/|discovery/item/|user/profile/[a-zA-Z0-9]+/)([a-zA-Z0-9]+)", url)
        if match:
            return match.group(1)
        return None

    def check_server(self) -> bool:
        """
        检查 API 服务是否可用

        Returns:
            服务是否可用
        """
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{self.api_url}/docs")
                return resp.status_code == 200
        except Exception:
            return False

    def get_note_info(self, url: str) -> dict:
        """
        获取笔记信息（不下载文件）

        Args:
            url: 小红书链接

        Returns:
            笔记信息字典
        """
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.api_url}/xhs/detail",
                json={"url": url, "download": False}
            )
            resp.raise_for_status()
            return resp.json()

    def download_note(
        self,
        url: str,
        download: bool = True,
        index: Optional[list[int]] = None,
        work_path: Optional[str] = None,
    ) -> dict:
        """
        下载笔记

        Args:
            url: 小红书链接
            download: 是否下载文件
            index: 指定下载的图片序号（仅对图文作品有效）
            work_path: 下载路径

        Returns:
            下载结果字典
        """
        data = {"url": url, "download": download}
        if index:
            data["index"] = index
        if work_path:
            data["work_path"] = work_path

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.api_url}/xhs/detail",
                json=data
            )
            resp.raise_for_status()
            return resp.json()

    def download_video(
        self,
        url: str,
        output_dir: Optional[Path] = None,
    ) -> XHSDownloadResult:
        """
        下载小红书视频

        Args:
            url: 小红书链接
            output_dir: 输出目录（默认使用 XHS-Downloader 配置）

        Returns:
            下载结果
        """
        if not self.is_xhs_url(url):
            return XHSDownloadResult(
                success=False,
                error="不是有效的小红书链接"
            )

        try:
            # 获取笔记信息
            info = self.get_note_info(url)

            # 检查是否为视频笔记
            note_type = info.get("type", "")
            if note_type not in ("视频", "video"):
                return XHSDownloadResult(
                    success=False,
                    error=f"该笔记不是视频类型，而是: {note_type}",
                    note_id=info.get("note_id"),
                    note_title=info.get("title"),
                    author_name=info.get("nickname"),
                )

            # 下载视频
            work_path = str(output_dir) if output_dir else None
            result = self.download_note(url, download=True, work_path=work_path)

            # 解析下载结果
            if result.get("download"):
                download_info = result["download"]
                # 获取下载的文件路径
                files = download_info if isinstance(download_info, list) else [download_info]

                if files and files[0].get("path"):
                    file_path = files[0]["path"]
                    return XHSDownloadResult(
                        success=True,
                        file_path=file_path,
                        file_name=Path(file_path).name,
                        note_id=result.get("note_id"),
                        note_title=result.get("title"),
                        author_name=result.get("nickname"),
                    )

            return XHSDownloadResult(
                success=False,
                error="下载失败：无法获取文件路径",
                note_id=result.get("note_id"),
                note_title=result.get("title"),
            )

        except httpx.HTTPStatusError as e:
            return XHSDownloadResult(
                success=False,
                error=f"HTTP 错误: {e.response.status_code}"
            )
        except httpx.RequestError as e:
            return XHSDownloadResult(
                success=False,
                error=f"网络错误: {str(e)}"
            )
        except Exception as e:
            return XHSDownloadResult(
                success=False,
                error=f"未知错误: {str(e)}"
            )


class XHSDownloaderServer:
    """XHS-Downloader 服务器管理"""

    def __init__(self, xhs_downloader_path: str, port: int = 5556):
        """
        初始化服务器管理器

        Args:
            xhs_downloader_path: XHS-Downloader 项目路径
            port: API 服务端口
        """
        self.xhs_downloader_path = Path(xhs_downloader_path)
        self.port = port
        self._process: Optional[subprocess.Popen] = None

    def is_installed(self) -> bool:
        """检查 XHS-Downloader 是否已安装"""
        main_py = self.xhs_downloader_path / "main.py"
        return main_py.exists()

    def start(self, background: bool = True) -> bool:
        """
        启动 XHS-Downloader API 服务

        Args:
            background: 是否后台运行

        Returns:
            是否启动成功
        """
        if not self.is_installed():
            return False

        if self._process and self._process.poll() is None:
            return True  # 已经在运行

        try:
            cmd = ["python", "main.py", "api"]
            if background:
                self._process = subprocess.Popen(
                    cmd,
                    cwd=str(self.xhs_downloader_path),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                self._process = subprocess.Popen(
                    cmd,
                    cwd=str(self.xhs_downloader_path),
                )

            # 等待服务启动
            time.sleep(2)

            # 检查服务是否可用
            client = XHSDownloaderClient(api_url=f"http://127.0.0.1:{self.port}")
            return client.check_server()

        except Exception:
            return False

    def stop(self):
        """停止服务"""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def is_running(self) -> bool:
        """检查服务是否在运行"""
        if self._process is None:
            return False
        return self._process.poll() is None


# 全局客户端实例（懒加载）
_xhs_client: Optional[XHSDownloaderClient] = None
_xhs_server: Optional[XHSDownloaderServer] = None


def get_xhs_client(
    api_url: str = "http://127.0.0.1:5556",
    timeout: int = 60
) -> XHSDownloaderClient:
    """
    获取 XHS-Downloader 客户端实例（单例）

    Args:
        api_url: API 地址
        timeout: 超时时间

    Returns:
        客户端实例
    """
    global _xhs_client
    if _xhs_client is None:
        _xhs_client = XHSDownloaderClient(api_url=api_url, timeout=timeout)
    return _xhs_client


def get_xhs_server(xhs_downloader_path: Optional[str] = None) -> XHSDownloaderServer:
    """
    获取 XHS-Downloader 服务器管理实例

    Args:
        xhs_downloader_path: XHS-Downloader 项目路径

    Returns:
        服务器管理实例
    """
    global _xhs_server
    if _xhs_server is None:
        if xhs_downloader_path is None:
            # 默认路径
            xhs_downloader_path = str(Path(__file__).parent.parent.parent / "xhs-test" / "XHS-Downloader")
        _xhs_server = XHSDownloaderServer(xhs_downloader_path)
    return _xhs_server


def download_xhs_video(
    url: str,
    output_dir: Optional[Path] = None,
    api_url: str = "http://127.0.0.1:5556",
    timeout: int = 120,
) -> XHSDownloadResult:
    """
    下载小红书视频的便捷函数

    Args:
        url: 小红书链接
        output_dir: 输出目录
        api_url: XHS-Downloader API 地址
        timeout: 超时时间

    Returns:
        下载结果
    """
    client = XHSDownloaderClient(api_url=api_url, timeout=timeout)
    return client.download_video(url, output_dir)


def is_xiaohongshu_url(url: str) -> bool:
    """
    判断 URL 是否为小红书链接的便捷函数

    Args:
        url: 待检测的 URL

    Returns:
        是否为小红书链接
    """
    return XHSDownloaderClient.is_xhs_url(url)
