import io
import socket
import pytest
from app.model_transport import ModelTransportError, PinnedHTTPConnection, post_model_json, resolve_endpoint


def resolved(*addresses):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]

@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1"])
def test_private_and_transition_addresses_blocked(monkeypatch, address):
    monkeypatch.setattr("app.model_transport.socket.getaddrinfo", lambda *args, **kwargs: resolved(address))
    with pytest.raises(ModelTransportError, match="Private model"):
        resolve_endpoint("https://model.example/v1", allow_private=False)


def test_mixed_dns_answer_rejected(monkeypatch):
    monkeypatch.setattr("app.model_transport.socket.getaddrinfo", lambda *args, **kwargs: resolved("1.1.1.1", "127.0.0.1"))
    with pytest.raises(ModelTransportError):
        resolve_endpoint("https://model.example/v1", allow_private=False)


def test_dns_checked_once_and_actual_connection_pinned(monkeypatch):
    lookups, calls = [], []
    def lookup(*args, **kwargs):
        lookups.append(args[0])
        return resolved("1.1.1.1" if len(lookups) == 1 else "127.0.0.1")
    class Response:
        status = 200
        data = io.BytesIO(b'{"ok": true}')
        def getheader(self, key, default=None):
            return default
        def read(self, limit):
            return self.data.read(limit)
    class Connection:
        sock = None
        def __init__(self, host, port, address, scheme, timeout):
            calls.append((host, port, address, scheme))
        def request(self, method, path, **kwargs):
            assert method == "POST" and path == "/v1/chat/completions"
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr("app.model_transport.socket.getaddrinfo", lookup)
    monkeypatch.setattr("app.model_transport.PinnedHTTPConnection", Connection)
    assert post_model_json("https://model.example/v1/chat/completions", headers={}, payload={}, allow_private=False) == (200, {"ok": True})
    assert lookups == ["model.example"]
    assert calls == [("model.example", 443, "1.1.1.1", "https")]


def test_socket_dials_numeric_checked_address_with_original_tls_sni(monkeypatch):
    seen = []
    class Socket:
        def settimeout(self, value):
            pass
        def connect(self, target):
            seen.append(("connect", target))
        def close(self):
            pass
    class Context:
        def wrap_socket(self, sock, server_hostname):
            seen.append(("sni", server_hostname))
            return sock
    monkeypatch.setattr("app.model_transport.socket.socket", lambda *args: Socket())
    monkeypatch.setattr("app.model_transport.ssl.create_default_context", lambda: Context())
    monkeypatch.setattr("app.model_transport.socket.getaddrinfo", lambda *args, **kwargs: pytest.fail("A pinned connection must not resolve DNS again"))
    connection = PinnedHTTPConnection("model.example", 443, "1.1.1.1", "https", 5)
    connection.connect()
    assert seen == [("connect", ("1.1.1.1", 443)), ("sni", "model.example")]


def test_redirect_is_not_followed(monkeypatch):
    targets = []
    monkeypatch.setattr("app.model_transport.socket.getaddrinfo", lambda *args, **kwargs: resolved("1.1.1.1"))
    class Response:
        status = 302
    class Connection:
        def __init__(self, host, *args):
            targets.append(host)
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr("app.model_transport.PinnedHTTPConnection", Connection)
    assert post_model_json("https://model.example/v1", headers={"Authorization": "fixture"}, payload={}, allow_private=False) == (302, None)
    assert targets == ["model.example"]
