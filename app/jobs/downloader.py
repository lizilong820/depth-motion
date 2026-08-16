from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
from pathlib import Path
from queue import Empty, Queue
import socket
import ssl
from threading import Thread
import time
from urllib.parse import unquote, urljoin, urlsplit

from app.errors import InvalidUploadError


ALLOWED_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
VIDEO_CONTENT_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
    "video/x-msvideo",
    "video/avi",
}
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30
TOTAL_DOWNLOAD_TIMEOUT_SECONDS = 600
MAX_RESOLVED_ADDRESSES = 4


@dataclass(frozen=True)
class DownloadResult:
    filename: str
    suffix: str
    size: int


def _resolve_public_addresses(hostname: str, port: int, deadline: float) -> list[str]:
    result: Queue[list[tuple] | BaseException] = Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put(socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM))
        except BaseException as exc:
            result.put(exc)

    Thread(target=resolve, daemon=True).start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InvalidUploadError("远程视频下载超过 10 分钟")
    try:
        resolved = result.get(timeout=remaining)
    except Empty as exc:
        raise InvalidUploadError("远程视频下载超过 10 分钟") from exc
    if isinstance(resolved, BaseException):
        raise InvalidUploadError("无法解析视频链接的域名") from resolved
    records = resolved

    addresses: list[str] = []
    for record in records:
        address = record[4][0]
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise InvalidUploadError("视频链接不能指向本机或内网地址")
        if address not in addresses and len(addresses) < MAX_RESOLVED_ADDRESSES:
            addresses.append(address)
    if not addresses:
        raise InvalidUploadError("视频链接没有可用的公网地址")
    return addresses


def validate_remote_url(url: str) -> None:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise InvalidUploadError("视频链接仅支持 HTTP 或 HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise InvalidUploadError("视频链接格式不正确")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise InvalidUploadError("视频链接端口不正确") from exc
    if port not in {80, 443}:
        raise InvalidUploadError("视频链接仅支持标准 HTTP/HTTPS 端口")


def _safe_target(url: str, deadline: float | None = None) -> tuple[str, str, int, str, list[str]]:
    validate_remote_url(url)
    parsed = urlsplit(url.strip())
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    resolve_deadline = deadline or (time.monotonic() + CONNECT_TIMEOUT_SECONDS)
    return parsed.scheme, parsed.hostname, port, path, _resolve_public_addresses(parsed.hostname, port, resolve_deadline)


def validate_remote_target(url: str) -> None:
    _safe_target(url)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _open(
    url: str,
    deadline: float,
    headers: dict[str, str] | None = None,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    scheme, host, port, path, addresses = _safe_target(url, deadline)
    last_error: OSError | None = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InvalidUploadError("远程视频下载超过 10 分钟")
        timeout = min(CONNECT_TIMEOUT_SECONDS, remaining)
        connection: http.client.HTTPConnection
        if scheme == "https":
            connection = _PinnedHTTPSConnection(host, port, address, timeout)
        else:
            connection = _PinnedHTTPConnection(host, port, address, timeout)
        try:
            request_headers = {
                "Host": host if port in {80, 443} else f"{host}:{port}",
                "Accept": "video/*,application/octet-stream;q=0.8",
                "User-Agent": "DepthMotion/1.0",
                "Connection": "close",
            }
            if headers:
                for name, value in headers.items():
                    if name.lower() not in {"host", "connection"}:
                        request_headers[name] = value
            connection.request("GET", path, headers=request_headers)
            response = connection.getresponse()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                connection.close()
                raise InvalidUploadError("远程视频下载超过 10 分钟")
            if connection.sock:
                connection.sock.settimeout(min(READ_TIMEOUT_SECONDS, remaining))
            return connection, response
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            connection.close()
            last_error = exc
    raise InvalidUploadError("无法连接视频链接") from last_error


def _filename(url: str, content_disposition: str | None) -> tuple[str, str]:
    candidate = Path(unquote(urlsplit(url).path)).name
    if content_disposition and "filename=" in content_disposition.lower():
        candidate = content_disposition.split("filename=", 1)[1].split(";", 1)[0].strip(' "')
    candidate = Path(candidate).name or "remote-video.mp4"
    suffix = Path(candidate).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        candidate = f"{Path(candidate).stem or 'remote-video'}.mp4"
        suffix = ".mp4"
    return candidate, suffix


def download_video(
    url: str,
    destination_dir: Path,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    deadline: float | None = None,
) -> DownloadResult:
    current_url = url.strip()
    if len(current_url) > 2048:
        raise InvalidUploadError("视频链接过长")
    deadline = deadline or (time.monotonic() + TOTAL_DOWNLOAD_TIMEOUT_SECONDS)
    destination: Path | None = None

    for redirect_count in range(MAX_REDIRECTS + 1):
        if time.monotonic() > deadline:
            raise InvalidUploadError("远程视频下载超过 10 分钟")
        connection, response = _open(current_url, deadline, headers)
        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirect_count == MAX_REDIRECTS:
                    raise InvalidUploadError("视频链接重定向次数过多")
                current_url = urljoin(current_url, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise InvalidUploadError(f"视频链接返回 HTTP {response.status}")

            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise InvalidUploadError("远程视频超过 500 MB")
                except ValueError:
                    pass

            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].lower()
            filename, suffix = _filename(current_url, response.getheader("Content-Disposition"))
            if content_type and content_type not in VIDEO_CONTENT_TYPES and content_type != "application/octet-stream":
                raise InvalidUploadError("链接返回的内容不是支持的视频格式")

            destination = destination_dir / f"source{suffix}"
            size = 0
            try:
                with destination.open("wb") as output:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise InvalidUploadError("远程视频下载超过 10 分钟")
                        if connection.sock:
                            connection.sock.settimeout(min(READ_TIMEOUT_SECONDS, remaining))
                        chunk = response.read1(64 * 1024)
                        if not chunk:
                            break
                        if time.monotonic() > deadline:
                            raise InvalidUploadError("远程视频下载超过 10 分钟")
                        size += len(chunk)
                        if size > max_bytes:
                            raise InvalidUploadError("远程视频超过 500 MB")
                        output.write(chunk)
                if size == 0:
                    raise InvalidUploadError("远程视频内容为空")
                return DownloadResult(filename=filename, suffix=suffix, size=size)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            if destination is not None:
                destination.unlink(missing_ok=True)
            raise InvalidUploadError("下载视频链接超时或连接中断") from exc
        finally:
            connection.close()

    raise InvalidUploadError("视频链接重定向次数过多")
