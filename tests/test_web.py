from __future__ import annotations

import http.client
from pathlib import Path
import socket
import ssl
import subprocess
from threading import Thread
from typing import Any, cast
from urllib.parse import urlencode

import control_freak.web as web
import pytest
from control_freak.web import BoundedTLSServer, csrf_token, ensure_tls, make_server, read_state, request_allowed, update_state


def test_tls_material_is_persistent_private_and_covers_device_addresses(tmp_path: Path) -> None:
    state_path = tmp_path / "web.json"

    certificate, key = ensure_tls(state_path, ["192.168.1.50", "100.64.0.50"])
    first_key = key.read_bytes()
    certificate2, key2 = ensure_tls(state_path, ["192.168.1.50", "100.64.0.50"])

    assert (certificate2, key2) == (certificate, key)
    assert key.read_bytes() == first_key
    assert key.stat().st_mode & 0o777 == 0o600
    assert certificate.stat().st_mode & 0o777 == 0o600
    details = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "IP Address:192.168.1.50" in details
    assert "IP Address:100.64.0.50" in details
    assert "IP Address:127.0.0.1" in details

    ensure_tls(state_path, ["192.168.1.51"])
    assert key.read_bytes() == first_key
    changed_details = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "IP Address:192.168.1.51" in changed_details
    assert "IP Address:192.168.1.50" not in changed_details


def test_bounded_tls_server_times_out_raw_clients_before_request_parsing() -> None:
    server = object.__new__(BoundedTLSServer)
    left, right = socket.socketpair()
    server.socket = cast(Any, type("Listener", (), {"accept": lambda self: (left, ("local", 1))})())
    try:
        request, _address = server.get_request()
        assert request.gettimeout() == BoundedTLSServer.request_timeout
    finally:
        left.close()
        right.close()


def test_web_state_rejects_symlinked_parent(tmp_path: Path) -> None:
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    parent = tmp_path / "control-freak"
    parent.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(OSError):
        update_state(parent / "web.json", enabled=True, tailscale=False)

    assert not (redirected / "web.json").exists()


def request(server, path: str, cookie: str = "") -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPSConnection(
        "127.0.0.1", server.server_port, timeout=2, context=ssl._create_unverified_context()
    )
    headers = {"Cookie": cookie} if cookie else {}
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    result = response.status, {key.lower(): value for key, value in response.getheaders()}, body
    connection.close()
    return result


def test_pairing_is_one_time_and_session_is_independent_and_hashed(tmp_path: Path) -> None:
    state_path = tmp_path / "web.json"
    state = update_state(state_path, enabled=True, tailscale=False)
    server = make_server(("127.0.0.1", 0), state_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, _body = request(server, "/")
        assert status == 401

        pairing_token = state["pairing_token"]
        persisted = state_path.read_text()
        assert pairing_token not in persisted

        status, headers, _body = request(server, f"/pair?token={pairing_token}")
        assert status == 303
        assert headers["location"] == "/"
        assert "Secure" in headers["set-cookie"]
        assert "HttpOnly" in headers["set-cookie"]
        assert "SameSite=Strict" in headers["set-cookie"]
        session = headers["set-cookie"].split(";", 1)[0]
        assert pairing_token not in session
        assert session.split("=", 1)[1] not in state_path.read_text()

        status, _headers, body = request(server, "/", session)
        assert status == 200
        assert b"Control Freak" in body
        assert b"Web app" in body

        status, _headers, _body = request(server, f"/pair?token={pairing_token}")
        assert status == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_expired_pairing_credential_is_rejected(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "web.json"
    monkeypatch.setattr(web.time, "time", lambda: 1000)
    state = update_state(state_path, enabled=True, tailscale=False)
    server = make_server(("127.0.0.1", 0), state_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(web.time, "time", lambda: 1000 + web.PAIRING_LIFETIME + 1)
        assert request(server, f"/pair?token={state['pairing_token']}")[0] == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_tailscale_destination_requires_explicit_exposure() -> None:
    state = {"enabled": True, "tailscale": False}
    assert request_allowed("192.168.1.50", state, "100.64.0.50", "192.168.1.50") is True
    assert request_allowed("192.168.1.51", state, "100.64.0.50", "192.168.1.50") is False
    assert request_allowed("100.64.0.50", state, "100.64.0.50", "192.168.1.50") is False

    state["tailscale"] = True
    assert request_allowed("100.64.0.50", state, "100.64.0.50", "192.168.1.50") is True
    assert request_allowed("100.64.0.51", state, "100.64.0.50", "192.168.1.50") is False
    assert request_allowed("100.64.0.50", state, None, "192.168.1.50") is False


def test_tailscale_destination_is_not_accepted_as_lan() -> None:
    state = {"enabled": True, "tailscale": False}

    assert request_allowed("100.64.0.50", state, "100.64.0.50", "100.64.0.50") is False


def test_enabled_state_requires_valid_hashed_credentials(tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    path.write_text('{"enabled":true,"tailscale":false,"token":"","version":1}')

    assert read_state(path)["enabled"] is False


def test_web_state_rejects_boolean_version(tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    path.write_text('{"version":true,"enabled":true,"tailscale":false,"token":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}')

    assert read_state(path)["enabled"] is False


def test_authenticated_audio_policy_update_requires_csrf(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "web.json"
    state = update_state(state_path, enabled=True, tailscale=False)
    policy = {"version": 1, "blocked": [], "managed": []}
    monkeypatch.setattr(web, "read_audio_state", lambda: policy)
    monkeypatch.setattr(web, "audio_outputs", lambda: [{"value": "sink-1", "label": "HDMI"}])
    monkeypatch.setattr(web, "set_sink_blocked", lambda sink, blocked: policy.update(blocked=[sink] if blocked else []))
    server = make_server(("127.0.0.1", 0), state_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    status, headers, _body = request(server, f"/pair?token={state['pairing_token']}")
    assert status == 303
    cookie = headers["set-cookie"].split(";", 1)[0]
    try:
        connection = http.client.HTTPSConnection(
            "127.0.0.1", server.server_port, timeout=2, context=ssl._create_unverified_context()
        )
        body = urlencode({"csrf": "wrong", "sink": "sink-1", "blocked": "1"})
        connection.request("POST", "/audio", body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
        assert connection.getresponse().status == 400
        connection.close()

        connection = http.client.HTTPSConnection(
            "127.0.0.1", server.server_port, timeout=2, context=ssl._create_unverified_context()
        )
        body = urlencode({"csrf": csrf_token(cookie.split("=", 1)[1]), "sink": "sink-1", "blocked": "1"})
        connection.request("POST", "/audio", body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        assert response.status == 303
        assert response.getheader("Location") == "/"
        assert policy["blocked"] == ["sink-1"]
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_audio_status_separates_policy_from_enforcement(monkeypatch) -> None:
    monkeypatch.setattr(web, "read_audio_state", lambda: {"version": 1, "blocked": ["sink-1"], "managed": []})
    monkeypatch.setattr(web, "audio_outputs", lambda: [{"value": "sink-1", "label": "HDMI"}])

    page = web._page("session")

    assert b"Blocked policy" in page
    assert b"kept muted" not in page
