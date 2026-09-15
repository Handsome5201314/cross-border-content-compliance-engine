"""Bounded image decoding and HTTPS download with DNS-pinned public connections."""
import base64
import http.client
import io
import ipaddress
import socket
import ssl
import time
import warnings
from urllib.parse import urlsplit

from PIL import Image

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000


def validate_image(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image byte limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in {"PNG", "JPEG", "WEBP"} or im.width * im.height > MAX_IMAGE_PIXELS:
                raise ValueError("unsupported image or pixel limit")
            im.verify()
    return data


def decode_image(encoded):
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError("base64 image limit")
    return validate_image(base64.b64decode(encoded, validate=True))


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def download_image(url, *, timeout=30.0):
    if not isinstance(url, str) or len(url) > 8192 or "\\" in url or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("invalid image URL")
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.port not in (None, 443) or parts.fragment):
        raise ValueError("image URL requires public HTTPS without credentials")
    host = parts.hostname.encode("idna").decode("ascii")
    addresses = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("non-public image destination")
    # The validated numeric IP is used for the socket; TLS still verifies the URL hostname.
    conn = _PinnedHTTPS(host, sorted(addresses)[0], timeout)
    deadline = time.monotonic() + timeout
    try:
        conn.request("GET", (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                     headers={"Accept": "image/png,image/jpeg,image/webp", "Accept-Encoding": "identity"})
        response = conn.getresponse()
        if response.status != 200:  # No redirects, including redirects to another public host.
            raise ValueError("image download status rejected")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_IMAGE_BYTES):
            raise ValueError("image byte limit")
        data = bytearray()
        while True:
            if response.isclosed():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("image download deadline")
            # HTTP/1.0 may detach conn.sock; the response owns the remaining socket.
            response.fp.raw._sock.settimeout(remaining)
            block = response.read1(min(64 * 1024, MAX_IMAGE_BYTES + 1 - len(data)))
            if not block:
                break
            data.extend(block)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("image byte limit")
        if length is not None and len(data) != int(length):
            raise ValueError("incomplete image download")
        return validate_image(bytes(data))
    finally:
        conn.close()
