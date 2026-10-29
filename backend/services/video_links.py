"""Public Bilibili/Douyin adapters, with an allowlisted, IP-pinned egress proxy.

yt-dlp only extracts metadata. We download bounded HTTPS files ourselves; no
cookies, browser sessions, generic extractors, playlists, HLS or remote FFmpeg.
"""
from contextlib import contextmanager
import ipaddress
import json
import logging
from pathlib import Path
import re
import select
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit, parse_qs
from http.server import BaseHTTPRequestHandler

import httpx

from ..core.video_errors import VideoError
from .video_media import ffmpeg_executable, run_media

HOSTS = ("bilibili.com", "b23.tv", "bilivideo.com", "bilivideo.cn", "hdslb.com", "biliapi.net",
         "douyin.com", "iesdouyin.com", "douyinvod.com", "bytecdn.cn", "byteimg.com",
         "ibytedtos.com", "toutiaovod.com", "pstatp.com", "snssdk.com", "amemv.com")
logger = logging.getLogger(__name__)


def allowed_host(host):
    host = (host or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in HOSTS)


def public_addresses(host):
    if not allowed_host(host):
        raise VideoError("平台返回了不受支持的媒体域名，请上传视频文件")
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError:
        raise VideoError("无法解析视频平台域名", 502) from None
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise VideoError("视频链接指向非公网地址，已阻止访问")
    return addresses


def normalize_link(value):
    if not isinstance(value, str) or len(value) > 4000:
        raise VideoError("请输入 B站或抖音视频链接")
    found = re.search(r"https?://[^\s<>\"'，。；]+", value)
    if not found:
        raise VideoError("请输入 B站或抖音的 HTTPS 视频链接")
    try:
        url = urlsplit(found.group(0).rstrip("/)）】]"))
        if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
            raise ValueError()
        host, path = (url.hostname or "").lower(), url.path.rstrip("/")
        query = ""
        if host in ("www.bilibili.com", "m.bilibili.com", "bilibili.com") and re.fullmatch(r"/video/(?:BV[0-9A-Za-z]+|av\d+)", path):
            host = "www.bilibili.com"
            part = parse_qs(url.query).get("p", ["1"])[0]
            if not part.isdigit() or not 1 <= int(part) <= 1000:
                raise ValueError()
            query = "p=" + str(int(part))
        elif host == "b23.tv" and re.fullmatch(r"/[A-Za-z0-9]+", path):
            pass
        elif host in ("www.douyin.com", "douyin.com") and re.fullmatch(r"/video/\d+", path):
            host = "www.douyin.com"
        elif host == "v.douyin.com" and re.fullmatch(r"/[A-Za-z0-9_-]+", path):
            pass
        elif host == "www.iesdouyin.com" and re.fullmatch(r"/share/video/\d+", path):
            host, path = "www.douyin.com", "/video/" + path.rsplit("/", 1)[1]
        else:
            raise ValueError()
        return urlunsplit(("https", host, path + "/" if host in ("v.douyin.com", "b23.tv") else path, query, ""))
    except ValueError:
        raise VideoError("目前支持 B站视频页/b23.tv 和抖音视频页/分享链接；合集、直播和登录受限内容请上传文件") from None


def resolve_share_link(url, proxy):
    if urlsplit(url).hostname not in ("b23.tv", "v.douyin.com"):
        return url
    with httpx.Client(proxy=proxy, trust_env=False, follow_redirects=False, timeout=10) as client:
        for _ in range(6):
            with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                if not response.is_redirect:
                    raise VideoError("分享链接未跳转到视频，请复制完整视频页地址或上传文件")
                target = str(response.url.join(response.headers.get("location", "")))
                url = normalize_link(target)
                if urlsplit(url).hostname not in ("b23.tv", "v.douyin.com"):
                    return url
    raise VideoError("分享链接跳转次数过多")


class _Proxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Tunnel(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *_args):
        pass  # URLs, signed media queries and headers must never enter logs.

    def do_CONNECT(self):
        upstream = None
        try:
            host, port = self.path.rsplit(":", 1)
            if port != "443" or not self.server.alive():
                raise ValueError()
            addresses = public_addresses(host)
            # Resolve exactly once, then connect to the validated literal address.
            deadline = time.monotonic() + 10
            for address in sorted(addresses, key=lambda value: ":" in value):
                try:
                    upstream = socket.create_connection((address, 443), timeout=min(3, max(.1, deadline - time.monotonic())))
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
            if upstream is None:
                raise OSError("platform unavailable")
            upstream.settimeout(20)
            self.connection.settimeout(20)
            self.send_response(200)
            self.end_headers()
            started = time.monotonic()
            while self.server.alive() and time.monotonic() - started < 180:
                ready, _, _ = select.select([self.connection, upstream], [], [], 0.5)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    with self.server.budget_lock:
                        self.server.remaining -= len(data)
                        if self.server.remaining < 0:
                            logger.debug("Platform egress byte budget exhausted")
                            return
                    target = upstream if source is self.connection else self.connection
                    target.sendall(data)
        except (OSError, ValueError, VideoError) as exc:
            # Do not forward raw upstream errors, signed URLs or arbitrary HTTP headers.
            logger.debug("Platform tunnel ended (%s)", type(exc).__name__)
        finally:
            if upstream:
                upstream.close()

    def do_GET(self):
        self.send_error(403)  # Only HTTPS tunnels to approved platforms are supported.


@contextmanager
def platform_proxy(max_bytes, alive):
    with _Proxy(("127.0.0.1", 0), _Tunnel) as server:
        server.remaining = max_bytes + 8 * 1024 * 1024
        server.budget_lock = threading.Lock()
        server.alive = alive
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:" + str(server.server_address[1])
        finally:
            server.shutdown()
            thread.join(timeout=2)


def _metadata(url, proxy, directory, alive):
    with tempfile.TemporaryFile() as output:
        command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-cache-dir", "--no-playlist", "--skip-download",
                   "--dump-single-json", "--no-warnings", "--socket-timeout", "10", "--retries", "0", "--extractor-retries", "0",
                   "--use-extractors", "bilibili.*,douyin", "--proxy", proxy, "--", url]
        process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = time.monotonic()
        try:
            while process.poll() is None:
                if not alive():
                    raise VideoError("任务已取消", 409)
                if time.monotonic() - started > 90 or output.tell() > 4 * 1024 * 1024:
                    raise VideoError("视频链接解析超时或返回过大，请上传本地视频")
                time.sleep(0.2)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if process.returncode:
            raise VideoError("平台未提供可访问的视频：可能需要登录、触发风控或解析器暂不兼容。请上传本地视频")
        output.seek(0)
        try:
            info = json.loads(output.read(4 * 1024 * 1024))
        except (ValueError, UnicodeDecodeError):
            raise VideoError("无法解析平台视频信息，请上传本地视频") from None
        if info.get("_type") in ("playlist", "multi_video") or info.get("is_live"):
            raise VideoError("暂不支持合集和直播，请选择单个视频或分 P 链接")
        return info


def choose_formats(info, max_bytes=104857600):
    formats = [value for value in info.get("formats", []) if value.get("url") and value.get("protocol") in ("https", "http")
               and value.get("ext") in ("mp4", "m4a", "webm") and not value.get("has_drm")]
    video = [value for value in formats if value.get("vcodec") not in (None, "none") and (value.get("height") or 0) <= 720]
    if not video:
        raise VideoError("平台没有可用的普通视频文件，请上传本地视频")
    audio = [value for value in formats if value.get("vcodec") == "none" and value.get("acodec") not in (None, "none")]
    def estimate(item):
        return item.get("filesize") or item.get("filesize_approx") or (item.get("tbr") or 0) * 1000 / 8 * (info.get("duration") or 0)
    candidates = []
    for item in video:
        tracks = [[item]] if item.get("acodec") not in (None, "none") else [[item, sound] for sound in audio]
        candidates.extend(group for group in tracks if sum(estimate(track) for track in group) <= max_bytes)
    if not candidates and not audio and all(item.get("acodec") in (None, "none") for item in video):
        # Do not silently interpret missing audio as a genuinely silent source.
        raise VideoError("平台未提供完整音轨，请上传包含声音的视频文件")
    if not candidates:
        raise VideoError("平台可用视频和音轨超过大小限制，请上传较小的视频文件")
    # Prefer resolution first, then easily decoded AVC and speech-quality audio.
    # The size budget applies to both tracks together, not just the video stream.
    return max(candidates, key=lambda group: (group[0].get("height") or 0,
        str(group[0].get("vcodec", "")).startswith("avc"),
        -abs((group[-1].get("abr") or 96) - 96), group[0].get("tbr") or 0))


def download_platform(url, directory: Path, settings, alive=lambda: True):
    try:
        return _download_platform(url, directory, settings, alive)
    except httpx.HTTPError:
        raise VideoError("平台视频连接中断或等待超时，请手动重试或上传本地文件", 502) from None


def _download_platform(url, directory: Path, settings, alive):
    with platform_proxy(settings.video_max_upload_bytes, alive) as proxy:
        url = resolve_share_link(url, proxy)
        info = _metadata(url, proxy, directory, alive)
        if not 0 < (info.get("duration") or 0) <= settings.video_max_duration_seconds:
            raise VideoError("平台视频时长未知或超出限制，请上传本地文件")
        total = 0
        paths = []
        with httpx.Client(proxy=proxy, trust_env=False, follow_redirects=False, timeout=15) as client:
            for index, media in enumerate(choose_formats(info, settings.video_max_upload_bytes)):
                media_url = media["url"]
                parsed = urlsplit(media_url)
                if parsed.scheme not in ("http", "https") or not allowed_host(parsed.hostname) or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                    raise VideoError("平台返回不受支持的媒体地址")
                # Never let an HTTP CDN URL bypass the CONNECT-only proxy.
                media_url = urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))
                path = directory / f"track-{index}.{media['ext']}"
                with path.open("wb") as output:
                    for redirect in range(6):
                        with client.stream("GET", media_url, headers={"Referer": url, "User-Agent": "Mozilla/5.0"}) as response:
                            if response.is_redirect:
                                media_url = str(response.url.join(response.headers.get("location", "")))
                                target = urlsplit(media_url)
                                if target.scheme != "https" or not allowed_host(target.hostname) or target.username or target.password or target.port not in (None, 443):
                                    raise VideoError("视频重定向地址已被阻止")
                                continue
                            if response.status_code != 200:
                                raise VideoError("平台视频下载失败，请上传本地视频")
                            if int(response.headers.get("content-length", "0")) + total > settings.video_max_upload_bytes:
                                raise VideoError("平台视频超过上传大小限制")
                            started = time.monotonic()
                            for block in response.iter_bytes(65536):
                                if not alive():
                                    raise VideoError("任务已取消", 409)
                                total += len(block)
                                if total > settings.video_max_upload_bytes or time.monotonic() - started > 180:
                                    raise VideoError("平台视频过大或下载超时")
                                output.write(block)
                            break
                    else:
                        raise VideoError("平台重定向次数过多")
                paths.append(path)
        if len(paths) == 1:
            return paths[0], str(info.get("title") or "视频总结")[:160]
        merged = directory / "merged.mp4"
        run_media([ffmpeg_executable(settings), "-nostdin", "-loglevel", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(paths[0]),
                   "-protocol_whitelist", "file,pipe", "-i", str(paths[1]), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(merged)],
                  timeout=60, alive=alive)
        return merged, str(info.get("title") or "视频总结")[:160]
