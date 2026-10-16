"""Model probe transport with DNS policy enforced on the addresses actually dialed."""
from __future__ import annotations
import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit

class ModelTransportError(ValueError):
    """Safe transport error; never carries credentials or upstream response bodies."""


def resolve_endpoint(url: str, *, allow_private: bool) -> tuple[str, str, int, list[str]]:
    if len(url) > 4096 or any(ord(char) < 32 for char in url) or "\\" in url:
        raise ModelTransportError("Invalid model URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        raise ModelTransportError("Invalid model URL") from None
    if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or "%" in host:
        raise ModelTransportError("Model URL must be HTTP(S) without credentials, query or fragment")
    try:
        addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError:
        raise ModelTransportError("Model endpoint hostname cannot be resolved") from None
    if not addresses:
        raise ModelTransportError("Model endpoint hostname cannot be resolved")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise ModelTransportError("Model endpoint returned an invalid address") from None
        if allow_private:
            continue
        allowed = ip.is_global and not ip.is_multicast and not ip.is_reserved and not ip.is_unspecified
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped:
                allowed = allowed and ip.ipv4_mapped.is_global
            if ip.sixtofour:
                allowed = allowed and ip.sixtofour.is_global
            if ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
                allowed = False
        if not allowed:
            raise ModelTransportError("Private model endpoints are disabled by deployment policy")
    return parsed.scheme, host, port, addresses


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, scheme: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.address, self.scheme = address, scheme

    def connect(self):
        # Numeric inet address goes directly to connect: no second hostname lookup.
        family = socket.AF_INET6 if ipaddress.ip_address(self.address).version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect((self.address, self.port))
            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host) if self.scheme == "https" else sock
        except BaseException:
            sock.close()
            raise


def post_model_json(url: str, *, headers: dict, payload: dict, allow_private: bool, timeout: float = 20, max_bytes: int = 1024 * 1024) -> tuple[int, object | None]:
    scheme, host, port, addresses = resolve_endpoint(url, allow_private=allow_private)
    connection = PinnedHTTPConnection(host, port, addresses[0], scheme, timeout)
    deadline = time.monotonic() + timeout
    try:
        request_headers = {**headers, "Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"}
        connection.request("POST", urlsplit(url).path or "/", body=json.dumps(payload).encode(), headers=request_headers)
        response = connection.getresponse()
        # Never follow model redirects carrying authentication headers.
        if not 200 <= response.status < 300:
            return response.status, None
        if response.getheader("Content-Encoding", "identity") not in {"", "identity"}:
            raise ModelTransportError("Unsupported compressed model response")
        length = response.getheader("Content-Length")
        if length and (not length.isdigit() or int(length) > max_bytes):
            raise ModelTransportError("Model probe response is too large")
        chunks, count = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelTransportError("Model connection timed out")
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read(min(65536, max_bytes + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > max_bytes:
                raise ModelTransportError("Model probe response is too large")
            chunks.append(chunk)
        return response.status, json.loads(b"".join(chunks))
    except ModelTransportError:
        raise
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        raise ModelTransportError("Model connection failed or returned an unsupported response") from None
    finally:
        connection.close()
