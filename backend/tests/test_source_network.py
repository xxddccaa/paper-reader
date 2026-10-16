"""Proxy boundary tests use a real local CONNECT server and isolated settings."""
from contextlib import contextmanager
import socketserver
import threading

import pytest
from pydantic import ValidationError

from app import source_tools
from app.source_network import SourceNetwork, load_source_network, network_path, save_source_network


@contextmanager
def proxy_server(response=b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nPDF"):
    observed = []
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            def headers():
                lines = []
                while line := self.rfile.readline():
                    lines.append(line)
                    if line == b"\r\n":
                        break
                return b"".join(lines)
            observed.append(headers())
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            observed.append(headers())
            self.wfile.write(response)
            self.wfile.flush()
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}", observed
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_proxy_tunnels_to_validated_ip_and_preserves_origin(monkeypatch, tmp_path, scheme):
    monkeypatch.setenv("PAPER_READER_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(source_tools, "validate_public_url", lambda url: ("paper.example", 443 if scheme == "https" else 80, ["8.8.8.8"]))
    sni = []
    class TLSFixture:
        def wrap_socket(self, sock, *, server_hostname):
            sni.append(server_hostname)
            return sock
    monkeypatch.setattr(source_tools.ssl, "create_default_context", TLSFixture)
    with proxy_server() as (proxy, requests):
        save_source_network(SourceNetwork(proxy_enabled=True, proxy_url=proxy))
        result = source_tools.download_public(f"{scheme}://paper.example/paper.pdf")
    port = 443 if scheme == "https" else 80
    assert requests[0].startswith(f"CONNECT 8.8.8.8:{port} ".encode())
    assert requests[1].startswith(b"GET /paper.pdf HTTP/1.1")
    assert f"Host: paper.example:{port}\r\n".encode() in requests[1]
    assert b"Authorization" not in requests[0] + requests[1]
    assert sni == (["paper.example"] if scheme == "https" else [])
    assert result.data == b"PDF"


def test_proxy_redirect_rechecks_target_and_never_uses_direct_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_READER_RUNTIME_DIR", str(tmp_path))
    def validated(url):
        if "127.0.0.1" in url:
            raise source_tools.SourceError("Private source addresses are not allowed")
        return "paper.example", 80, ["8.8.8.8"]
    monkeypatch.setattr(source_tools, "validate_public_url", validated)
    monkeypatch.setattr(source_tools, "_PinnedHTTP", lambda *a: pytest.fail("Proxy must never fall back to direct"))
    with proxy_server(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n") as (proxy, requests):
        network = SourceNetwork(proxy_enabled=True, proxy_url=proxy)
        with pytest.raises(source_tools.SourceError, match="Private"):
            source_tools.download_public("http://paper.example/paper", network=network)
        assert len(requests) == 2
    with pytest.raises(source_tools.SourceError, match="proxy connection failed"):
        source_tools.download_public("http://paper.example/paper", network=network)


def test_proxy_preserves_download_size_bound(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_READER_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(source_tools, "validate_public_url", lambda url: ("paper.example", 80, ["8.8.8.8"]))
    with proxy_server(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n") as (proxy, _):
        with pytest.raises(source_tools.SourceError, match="size limit"):
            source_tools.download_public("http://paper.example/paper", max_bytes=32,
                                         network=SourceNetwork(proxy_enabled=True, proxy_url=proxy))


def test_network_settings_persist_and_reload_without_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_READER_RUNTIME_DIR", str(tmp_path))
    assert not load_source_network().proxy_enabled
    settings = SourceNetwork(proxy_enabled=True, proxy_url="http://127.0.0.1:10826/", timeout_seconds=90)
    save_source_network(settings)
    assert load_source_network() == settings
    assert network_path().stat().st_mode & 0o777 == 0o600
    save_source_network(SourceNetwork())
    assert not load_source_network().proxy_enabled
    network_path().write_text("invalid configuration")
    with pytest.raises(ValueError, match="could not be read"):
        load_source_network()


@pytest.mark.parametrize("url", ["socks5://127.0.0.1:10826", "http://user:secret@proxy.example:8080", "http://proxy.example/path", "http://proxy.example:0", "http://proxy.example:99999", "http://proxy.example?x=1", "http://proxy.example\r\nX:bad"])
def test_proxy_settings_reject_unsupported_or_unsafe_urls(url):
    with pytest.raises(ValidationError):
        SourceNetwork(proxy_enabled=True, proxy_url=url)
