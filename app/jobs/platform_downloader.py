from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import re
import selectors
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
from threading import Thread
import time
from urllib.parse import urlencode, urljoin, urlsplit

from app.errors import InvalidUploadError
from app.jobs.downloader import _open, download_video


PLATFORM_DOMAINS = {
    "youtube": ("youtube.com", "youtu.be"),
    "bilibili": ("bilibili.com", "b23.tv"),
    "douyin": ("douyin.com", "iesdouyin.com"),
}
DOWNLOAD_TIMEOUT_SECONDS = 600
logger = logging.getLogger("depth-studio.platform-downloader")
BILIBILI_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://www.bilibili.com",
    "Referer": "https://www.bilibili.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


@dataclass(frozen=True)
class PlatformDownloadResult:
    filename: str
    source_path: Path
    platform: str


def extract_url(value: str) -> str:
    match = re.search(r"https?://[^\s<>\"']+", value.strip())
    if not match:
        raise InvalidUploadError("没有找到有效的视频链接")
    return match.group(0).rstrip(".,，。！？!?、；;）)]}")


def detect_platform(url: str) -> str | None:
    try:
        parsed = urlsplit(extract_url(url))
        hostname = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    for platform, domains in PLATFORM_DOMAINS.items():
        if any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains):
            return platform
    return None


def _resolve(hostname: str, port: int, deadline: float) -> list[tuple]:
    result: Queue[list[tuple] | BaseException] = Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put(socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM))
        except BaseException as exc:
            result.put(exc)

    Thread(target=resolve, daemon=True).start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OSError("平台视频下载超过 10 分钟")
    try:
        resolved = result.get(timeout=remaining)
    except Empty as exc:
        raise OSError("平台视频下载超过 10 分钟") from exc
    if isinstance(resolved, BaseException):
        raise OSError("目标域名无法解析") from resolved
    return resolved


def validate_platform_url(url: str, deadline: float | None = None) -> str:
    platform = detect_platform(url)
    if platform is None:
        raise InvalidUploadError("仅支持 YouTube、B站、抖音页面或公开视频直链")
    parsed = urlsplit(extract_url(url))
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise InvalidUploadError("视频页面链接格式不正确") from exc
    if parsed.username or parsed.password or port not in {80, 443}:
        raise InvalidUploadError("视频页面链接格式不正确")
    try:
        records = _resolve(
            parsed.hostname,
            port,
            deadline or (time.monotonic() + 10),
        )
    except OSError as exc:
        raise InvalidUploadError("无法解析视频平台域名") from exc
    if not records:
        raise InvalidUploadError("视频平台域名没有可用地址")
    for record in records:
        if not ipaddress.ip_address(record[4][0]).is_global:
            raise InvalidUploadError("视频页面不能指向本机或内网地址")
    return platform


def _public_addresses(hostname: str, port: int, deadline: float) -> list[str]:
    records = _resolve(hostname, port, deadline)
    addresses: list[str] = []
    for record in records:
        address = record[4][0]
        if not ipaddress.ip_address(address).is_global:
            raise OSError("目标地址不是公网地址")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise OSError("目标域名没有公网地址")
    return addresses


def _connect_public(hostname: str, port: int, deadline: float) -> socket.socket:
    last_error: OSError | None = None
    for address in _public_addresses(hostname, port, deadline):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError("平台视频下载超过 10 分钟")
            return socket.create_connection((address, port), timeout=min(20, remaining))
        except OSError as exc:
            last_error = exc
    raise OSError("无法连接公网目标") from last_error


class _SafeProxyHandler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self) -> None:
        request_line = self.rfile.readline(8193)
        if not request_line or len(request_line) > 8192:
            return
        try:
            method, target, version = request_line.decode("latin-1").strip().split(" ", 2)
            headers: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(8193)
                total += len(line)
                if not line or line in {b"\r\n", b"\n"}:
                    break
                if total > 65536:
                    raise OSError("请求头过大")
                headers.append(line)
            if method.upper() == "CONNECT":
                parsed = urlsplit(f"//{target}")
                host = parsed.hostname
                port = parsed.port or 443
                if not host or port != 443:
                    raise OSError("仅允许标准 HTTPS 端口")
                upstream = _connect_public(host, port, self.server.deadline)
                self.wfile.write(f"{version} 200 Connection Established\r\n\r\n".encode("latin-1"))
            else:
                parsed = urlsplit(target)
                host = parsed.hostname
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if parsed.scheme != "http" or not host or port != 80:
                    raise OSError("仅允许标准 HTTP 代理请求")
                upstream = _connect_public(host, port, self.server.deadline)
                path = parsed.path or "/"
                if parsed.query:
                    path = f"{path}?{parsed.query}"
                upstream.sendall(f"{method} {path} {version}\r\n".encode("latin-1"))
                for header in headers:
                    if not header.lower().startswith((b"proxy-connection:", b"connection:")):
                        upstream.sendall(header)
                upstream.sendall(b"Connection: close\r\n\r\n")
            try:
                self._relay(upstream)
            finally:
                upstream.close()
        except (OSError, ValueError, UnicodeError):
            try:
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass

    def _relay(self, upstream: socket.socket) -> None:
        selector = selectors.DefaultSelector()
        selector.register(self.connection, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, self.connection)
        try:
            while True:
                events = selector.select(timeout=30)
                if not events:
                    return
                for key, _ in events:
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    key.data.sendall(data)
        finally:
            selector.close()


class _SafeProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, deadline: float):
        self.deadline = deadline
        super().__init__(server_address, handler_class)


@contextmanager
def _safe_proxy(deadline: float):
    proxy = _SafeProxyServer(("127.0.0.1", 0), _SafeProxyHandler, deadline)
    thread = Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{proxy.server_address[1]}"
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=5)


def _clean_directory(directory: Path) -> None:
    for path in directory.iterdir():
        if path.name == "job.json":
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _directory_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        if path.is_file() and path.name != "job.json":
            total += path.stat().st_size
    return total


def _fetch_json(
    url: str,
    deadline: float,
    max_bytes: int = 2 * 1024 * 1024,
) -> dict:
    connection, response = _open(url, deadline, BILIBILI_HEADERS)
    try:
        if response.status < 200 or response.status >= 300:
            raise InvalidUploadError(f"B站公开接口返回 HTTP {response.status}")
        chunks: list[bytes] = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InvalidUploadError("B站公开接口响应超时")
            if connection.sock:
                connection.sock.settimeout(min(30, remaining))
            chunk = response.read1(min(64 * 1024, max_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise InvalidUploadError("B站公开接口响应过大")
            chunks.append(chunk)
        data = json.loads(b"".join(chunks))
        if not isinstance(data, dict):
            raise InvalidUploadError("B站公开接口响应无法识别")
        return data
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidUploadError("B站公开接口响应无法识别") from exc
    finally:
        connection.close()


def _expand_bilibili_url(page_url: str, deadline: float) -> str:
    current_url = page_url
    for _ in range(4):
        parsed = urlsplit(current_url)
        if parsed.hostname != "b23.tv":
            if detect_platform(current_url) != "bilibili":
                raise InvalidUploadError("B站短链跳转到了不支持的域名")
            return current_url
        connection, response = _open(current_url, deadline, BILIBILI_HEADERS)
        try:
            if response.status not in {301, 302, 303, 307, 308}:
                raise InvalidUploadError("B站短链没有返回有效跳转")
            location = response.getheader("Location")
            if not location:
                raise InvalidUploadError("B站短链没有返回跳转地址")
            current_url = urljoin(current_url, location)
        finally:
            connection.close()
    raise InvalidUploadError("B站短链重定向次数过多")


def _bilibili_video_reference(page_url: str, deadline: float) -> tuple[str, int]:
    parsed = urlsplit(_expand_bilibili_url(page_url, deadline))
    match = re.match(r"^/video/(BV[0-9A-Za-z]{10})(?:/|$)", parsed.path)
    if not match:
        raise InvalidUploadError("B站链接中没有可识别的视频编号")
    page = 1
    for name, value in (part.split("=", 1) for part in parsed.query.split("&") if "=" in part):
        if name == "p":
            try:
                page = int(value)
            except ValueError as exc:
                raise InvalidUploadError("B站分P编号格式不正确") from exc
            if page < 1:
                raise InvalidUploadError("B站分P编号格式不正确")
    return match.group(1), page


def _bilibili_bvid(page_url: str, deadline: float | None = None) -> str:
    return _bilibili_video_reference(page_url, deadline or (time.monotonic() + 60))[0]


def _download_bilibili_fallback(
    page_url: str,
    destination_dir: Path,
    max_bytes: int,
    deadline: float | None = None,
) -> PlatformDownloadResult:
    deadline = deadline or (time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS)
    bvid, page_number = _bilibili_video_reference(page_url, deadline)
    view_query = urlencode({"bvid": bvid})
    view = _fetch_json(f"https://api.bilibili.com/x/web-interface/view?{view_query}", deadline)
    data = view.get("data")
    if view.get("code") != 0 or not isinstance(data, dict):
        raise InvalidUploadError("B站视频不是可访问的公开内容")
    pages = data.get("pages")
    if not isinstance(pages, list) or page_number > len(pages):
        raise InvalidUploadError("B站视频没有对应的分P")
    page = pages[page_number - 1]
    cid = page.get("cid") if isinstance(page, dict) else None
    if not isinstance(cid, int):
        raise InvalidUploadError("B站视频没有可用分集")

    play_query = urlencode({"bvid": bvid, "cid": cid, "qn": 64, "fnval": 0, "fourk": 0})
    play = _fetch_json(f"https://api.bilibili.com/x/player/playurl?{play_query}", deadline)
    play_data = play.get("data")
    streams = play_data.get("durl") if isinstance(play_data, dict) else None
    if play.get("code") != 0 or not isinstance(streams, list) or not streams:
        raise InvalidUploadError("B站视频没有匿名可用的 MP4 清晰度")
    if len(streams) != 1:
        raise InvalidUploadError("B站多段旧视频暂不支持")
    media_url = streams[0].get("url") if isinstance(streams[0], dict) else None
    if not isinstance(media_url, str) or not media_url:
        raise InvalidUploadError("B站视频播放地址无法识别")

    result = download_video(
        media_url,
        destination_dir,
        max_bytes,
        headers=BILIBILI_HEADERS,
        deadline=deadline,
    )
    title = str(data.get("title") or bvid).strip()[:120]
    return PlatformDownloadResult(
        filename=f"{title}.mp4",
        source_path=destination_dir / f"source{result.suffix}",
        platform="bilibili",
    )


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _run_downloader(
    command: list[str],
    destination_dir: Path,
    max_bytes: int,
    deadline: float,
) -> tuple[int, str]:
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as log:
        popen_options: dict = {
            "cwd": destination_dir,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True
        process = subprocess.Popen(command, **popen_options)
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise InvalidUploadError("平台视频下载超过 10 分钟")
                if _directory_size(destination_dir) > max_bytes:
                    raise InvalidUploadError("平台视频超过 500 MB")
                time.sleep(1)
            log.seek(0)
            return process.returncode or 0, log.read()
        except Exception:
            _stop_process_group(process)
            _clean_directory(destination_dir)
            raise
        finally:
            _stop_process_group(process)


def _prepare_douyin_session(
    page_url: str,
    destination_dir: Path,
    proxy_url: str,
    deadline: float,
) -> tuple[Path, dict]:
    cookie_path = destination_dir / "douyin-cookies.txt"
    result_path = destination_dir / "douyin-session.json"
    command = [
        sys.executable,
        str(Path(__file__).with_name("douyin_browser.py")),
        "--proxy",
        proxy_url,
        "--cookies",
        str(cookie_path),
        "--result",
        str(result_path),
        page_url,
    ]
    returncode, output = _run_downloader(command, destination_dir, 4 * 1024 * 1024, deadline)
    if returncode != 0 or not cookie_path.is_file() or not result_path.is_file():
        logger.warning("douyin_browser_session_failed detail=%s", output.strip()[-1000:])
        raise InvalidUploadError("抖音匿名浏览器验证失败")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidUploadError("抖音匿名浏览器结果无法识别") from exc
    if not isinstance(result, dict):
        raise InvalidUploadError("抖音匿名浏览器结果无法识别")
    return cookie_path, result


def _download_douyin_browser_result(
    result: dict,
    destination_dir: Path,
    max_bytes: int,
    deadline: float,
) -> PlatformDownloadResult:
    page_url = result.get("page_url")
    if not isinstance(page_url, str) or detect_platform(page_url) != "douyin":
        raise InvalidUploadError("抖音浏览器跳转结果不安全")
    media_urls = result.get("media_urls")
    if not isinstance(media_urls, list) or not media_urls:
        raise InvalidUploadError("抖音浏览器没有返回公开视频地址")
    headers = {
        "Referer": "https://www.douyin.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    last_error: InvalidUploadError | None = None
    for media_url in media_urls[:8]:
        if not isinstance(media_url, str):
            continue
        try:
            download = download_video(
                media_url,
                destination_dir,
                max_bytes,
                headers=headers,
                deadline=deadline,
            )
            title = str(result.get("title") or "douyin-video").strip()[:120]
            return PlatformDownloadResult(
                filename=f"{title}.mp4",
                source_path=destination_dir / f"source{download.suffix}",
                platform="douyin",
            )
        except InvalidUploadError as exc:
            last_error = exc
    raise InvalidUploadError("抖音公开视频下载失败") from last_error


def _platform_error_message(platform: str, raw_message: str) -> str:
    lower = raw_message.lower()
    if platform == "youtube" and ("timed out" in lower or "proxy" in lower):
        return "当前服务器无法连接 YouTube"
    if platform == "douyin" and "fresh cookies" in lower:
        return "该抖音公开页面暂时无法解析，请稍后重试"
    if "login" in lower or "cookies" in lower:
        return "该视频需要登录授权，当前仅支持公开内容"
    if "private" in lower or "members" in lower:
        return "该视频不是公开内容"
    return "平台页面解析失败，请确认链接公开且可访问"


def download_platform_video(url: str, destination_dir: Path, max_bytes: int) -> PlatformDownloadResult:
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    page_url = extract_url(url)
    platform = validate_platform_url(page_url, deadline)
    output_template = str(destination_dir / "platform-%(id).80s.%(ext)s")
    max_size = str(max_bytes)
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-playlist",
        "--playlist-items",
        "1",
        "--max-filesize",
        max_size,
        "--socket-timeout",
        "20",
        "--retries",
        "1",
        "--fragment-retries",
        "1",
        "--restrict-filenames",
        "--no-write-comments",
        "--no-write-thumbnail",
        "--no-write-subs",
        "--no-write-auto-subs",
        "--no-write-playlist-metafiles",
        "--no-write-info-json",
        "--no-cache-dir",
        "--proxy",
        "PROXY_URL",
        "--merge-output-format",
        "mp4",
        "--remux-video",
        "mp4",
        "--format",
        "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--print",
        "after_move:{\"path\":%(filepath)j,\"title\":%(title)j}",
        "--output",
        output_template,
        "--",
        page_url,
    ]
    douyin_result: dict | None = None
    try:
        with _safe_proxy(deadline) as proxy_url:
            command[command.index("PROXY_URL")] = proxy_url
            if platform == "douyin":
                cookie_path, douyin_result = _prepare_douyin_session(
                    page_url,
                    destination_dir,
                    proxy_url,
                    deadline,
                )
                command[command.index("--merge-output-format"):command.index("--merge-output-format")] = [
                    "--cookies",
                    str(cookie_path),
                ]
            returncode, output = _run_downloader(command, destination_dir, max_bytes, deadline)
    finally:
        for temporary in (destination_dir / "douyin-cookies.txt", destination_dir / "douyin-session.json"):
            temporary.unlink(missing_ok=True)

    if returncode != 0:
        _clean_directory(destination_dir)
        detail = output.strip().splitlines()
        raw_message = detail[-1] if detail else "平台页面解析失败"
        logger.warning("platform_download_failed platform=%s detail=%s", platform, raw_message[:1000])
        if platform == "bilibili":
            try:
                return _download_bilibili_fallback(page_url, destination_dir, max_bytes, deadline)
            except Exception as exc:
                _clean_directory(destination_dir)
                logger.warning("bilibili_fallback_failed detail=%s", str(exc)[:1000])
        if platform == "douyin" and douyin_result:
            try:
                return _download_douyin_browser_result(
                    douyin_result,
                    destination_dir,
                    max_bytes,
                    deadline,
                )
            except Exception as exc:
                _clean_directory(destination_dir)
                diagnostics = douyin_result.get("diagnostics") if isinstance(douyin_result, dict) else None
                logger.warning(
                    "douyin_browser_fallback_failed detail=%s diagnostics=%s",
                    str(exc)[:1000],
                    json.dumps(diagnostics, ensure_ascii=False)[:1000],
                )
        raise InvalidUploadError(_platform_error_message(platform, raw_message))

    lines = [line for line in output.splitlines() if line.startswith("{")]
    if not lines:
        _clean_directory(destination_dir)
        raise InvalidUploadError("平台页面没有返回可用视频")
    try:
        result = json.loads(lines[-1])
        source_path = Path(result["path"]).resolve()
        title = str(result.get("title") or source_path.name)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _clean_directory(destination_dir)
        raise InvalidUploadError("平台视频下载结果无法识别") from exc

    root = destination_dir.resolve()
    if source_path.parent != root or not source_path.is_file():
        _clean_directory(destination_dir)
        raise InvalidUploadError("平台视频输出路径不安全")
    if source_path.stat().st_size <= 0 or source_path.stat().st_size > max_bytes:
        _clean_directory(destination_dir)
        raise InvalidUploadError("平台视频为空或超过 500 MB")
    return PlatformDownloadResult(filename=f"{title[:120]}.mp4", source_path=source_path, platform=platform)
