from __future__ import annotations

import hashlib
import hmac
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os

from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import subprocess
from threading import BoundedSemaphore, Thread
import time
from typing import cast

from urllib.parse import parse_qs, urlsplit

from .audio import enforce as enforce_audio, outputs as audio_outputs, read_state as read_audio_state, set_sink_blocked
from .stateio import locked, opened, read_json, write_json

STATE_PATH = Path("/userdata/system/control-freak/web.json")
TAILSCALE = Path("/usr/bin/tailscale")
CURRENT_PATH = Path("/userdata/system/control-freak/app/current.json")
HASH = re.compile(r"[0-9a-f]{64}")
TAILSCALE_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")
PAIRING_LIFETIME = 300
OPENSSL = "/usr/bin/openssl"
TLS_CERTIFICATE = "web-tls.crt"
TLS_KEY = "web-tls.key"
TLS_STATE = "web-tls.json"


def _private_regular(directory_fd: int, name: str) -> bool:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o777 == 0o600
    finally:
        os.close(fd)


def ensure_tls(state_path: Path, addresses: list[str]) -> tuple[Path, Path]:
    sans = ["DNS:localhost", "IP:127.0.0.1", *(f"IP:{ipaddress.ip_address(value)}" for value in addresses)]
    sans = list(dict.fromkeys(sans))
    metadata_path = state_path.with_name(TLS_STATE)
    with locked(metadata_path, lock_name="web-tls.lock") as (directory_fd, metadata_name):
        try:
            metadata = read_json(directory_fd, metadata_name)
        except (OSError, ValueError, json.JSONDecodeError):
            metadata = {}
        key_exists = _private_regular(directory_fd, TLS_KEY)
        if metadata == {"version": 1, "sans": sans} and key_exists and _private_regular(directory_fd, TLS_CERTIFICATE):
            return state_path.with_name(TLS_CERTIFICATE), state_path.with_name(TLS_KEY)

        temporary_certificate = f".{TLS_CERTIFICATE}.{secrets.token_hex(8)}"
        temporary_key = f".{TLS_KEY}.{secrets.token_hex(8)}"
        certificate_target = f"/proc/self/fd/{directory_fd}/{temporary_certificate}"
        command = [OPENSSL, "req", "-x509", "-sha256", "-days", "3650", "-subj", "/CN=Control Freak", "-addext", f"subjectAltName={','.join(sans)}"]
        if key_exists:
            command += ["-new", "-key", f"/proc/self/fd/{directory_fd}/{TLS_KEY}", "-out", certificate_target]
        else:
            command += ["-newkey", "rsa:2048", "-nodes", "-keyout", f"/proc/self/fd/{directory_fd}/{temporary_key}", "-out", certificate_target]
        try:
            subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, pass_fds=(directory_fd,), timeout=15)
            os.chmod(temporary_certificate, 0o600, dir_fd=directory_fd, follow_symlinks=False)
            if not key_exists:
                os.chmod(temporary_key, 0o600, dir_fd=directory_fd, follow_symlinks=False)
                os.replace(temporary_key, TLS_KEY, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.replace(temporary_certificate, TLS_CERTIFICATE, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
            write_json(directory_fd, metadata_name, {"version": 1, "sans": sans})
        finally:
            for name in (temporary_certificate, temporary_key):
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        return state_path.with_name(TLS_CERTIFICATE), state_path.with_name(TLS_KEY)


def _disabled_state() -> dict:
    return {
        "version": 2, "enabled": False, "tailscale": False,
        "pair_salt": "", "pair_hash": "", "pair_expires": 0,
        "session_salt": "", "session_hash": "",
    }


def _credential_hash(salt: str, credential: str) -> str:
    return hashlib.sha256(f"{salt}:{credential}".encode()).hexdigest()


def _read_state(directory_fd: int, name: str) -> dict:
    try:
        value = read_json(directory_fd, name)
    except (OSError, ValueError, json.JSONDecodeError):
        return _disabled_state()
    if (
        not isinstance(value, dict)
        or set(value) != set(_disabled_state())
        or type(value.get("version")) is not int
        or value["version"] != 2
        or type(value.get("enabled")) is not bool
        or type(value.get("tailscale")) is not bool
        or type(value.get("pair_expires")) not in {int, float}
        or any(not isinstance(value.get(key), str) for key in ("pair_salt", "pair_hash", "session_salt", "session_hash"))
        or any(value.get(key) and not HASH.fullmatch(value[key]) for key in ("pair_hash", "session_hash"))
        or (value.get("enabled") and not (value["pair_hash"] or value["session_hash"]))
    ):
        return _disabled_state()
    return value


def read_state(path: Path) -> dict:
    with opened(path) as (directory_fd, name):
        return _read_state(directory_fd, name)


def _update_state(directory_fd: int, name: str, *, enabled: bool, tailscale: bool) -> dict:
    current = _read_state(directory_fd, name)
    pairing_token = secrets.token_urlsafe(24) if enabled else ""
    pair_salt = secrets.token_hex(16) if enabled else ""
    state = {
        **_disabled_state(),
        "enabled": enabled,
        "tailscale": enabled and tailscale,
        "pair_salt": pair_salt,
        "pair_hash": _credential_hash(pair_salt, pairing_token) if enabled else "",
        "pair_expires": time.time() + PAIRING_LIFETIME if enabled else 0,
        "session_salt": current["session_salt"] if enabled else "",
        "session_hash": current["session_hash"] if enabled else "",
    }
    write_json(directory_fd, name, state)
    return {**state, "pairing_token": pairing_token}


def update_state(path: Path, *, enabled: bool, tailscale: bool) -> dict:
    with locked(path) as (directory_fd, name):
        return _update_state(directory_fd, name, enabled=enabled, tailscale=tailscale)


def csrf_token(token: str) -> str:
    return hmac.new(token.encode(), b"control-freak-csrf", hashlib.sha256).hexdigest()


def request_allowed(destination: str, state: dict, tailscale_ip: str | None, lan_ip: str | None) -> bool:
    if not state.get("enabled"):
        return False
    try:
        address = ipaddress.IPv4Address(destination)
    except ipaddress.AddressValueError:
        return False
    if address.is_loopback:
        return True
    if address in TAILSCALE_NETWORK:
        return bool(state.get("tailscale") and destination == tailscale_ip)
    return destination == lan_ip


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("192.0.2.1", 9))
            return str(ipaddress.IPv4Address(connection.getsockname()[0]))
    except (OSError, ipaddress.AddressValueError):
        return None


def _tailscale_ip() -> str | None:
    if not TAILSCALE.exists():
        return None
    try:
        result = subprocess.run([str(TAILSCALE), "ip", "-4"], capture_output=True, text=True, timeout=2, check=False)
        return str(ipaddress.IPv4Address(result.stdout.strip())) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ipaddress.AddressValueError):
        return None


def _status() -> dict[str, str]:
    try:
        release = json.loads(CURRENT_PATH.read_text()).get("release_id", "Unknown")
    except (OSError, json.JSONDecodeError):
        release = "Unknown"
    return {"machine": socket.gethostname().upper(), "release": str(release)}


def _page(token: str) -> bytes:
    status = _status()
    try:
        policy = read_audio_state()
        devices = audio_outputs()
        live = {item["value"] for item in devices}
        devices += [{"value": value, "label": f"{value} (currently unavailable)"} for value in policy["blocked"] if value not in live]
        blocked = set(policy["blocked"])
        audio = "".join(
            f'<form class="audio-row" method="post" action="/audio"><input type="hidden" name="csrf" value="{csrf_token(token)}">'
            f'<input type="hidden" name="sink" value="{escape(item["value"], quote=True)}"><input type="hidden" name="blocked" value="{0 if item["value"] in blocked else 1}">'
            f'<span><strong>{escape(item["label"])}</strong><small>{"Blocked policy; enforcement status unavailable" if item["value"] in blocked else "Allowed by policy"}</small></span>'
            f'<button type="submit">{"Allow" if item["value"] in blocked else "Block"}</button></form>'
            for item in devices
        ) or "<p>No audio outputs are currently available.</p>"
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        audio = "<p>Audio policy is unavailable. Use the Batocera app to inspect it.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Control Freak</title>
<style>
:root{{--ink:#071019;--panel:#111f2b;--line:#294052;--text:#f4f0e7;--muted:#afc0cb;--amber:#f2b84b;--teal:#4fc8b5;color-scheme:dark}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--ink);color:var(--text);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:900px;margin:auto;padding:clamp(24px,6vw,72px)}} header{{display:flex;align-items:end;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:24px}}
h1{{font-size:clamp(2.4rem,8vw,5rem);line-height:.95;letter-spacing:-.04em;margin:0}} .state{{color:var(--teal);font-weight:700}}
section{{padding:32px 0}} h2{{font-size:1.1rem;margin:0 0 18px;color:var(--amber)}} dl{{margin:0}} div{{display:grid;grid-template-columns:minmax(9rem,1fr) 2fr;gap:24px;padding:18px 0;border-top:1px solid var(--line)}}
dt{{color:var(--muted)}} dd{{margin:0;font-weight:700;overflow-wrap:anywhere}} .audio-row{{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:18px 0;border-top:1px solid var(--line)}} .audio-row span{{display:grid}} small{{color:var(--muted)}} button{{min-width:5rem;min-height:44px;border:1px solid var(--amber);background:transparent;color:var(--amber);font:inherit;font-weight:700;border-radius:4px}} button:focus-visible{{outline:3px solid var(--amber);outline-offset:4px}}
@media(max-width:560px){{header{{display:block}} .state{{display:block;margin-top:18px}} div{{grid-template-columns:1fr;gap:4px}}}}
</style></head><body><main><header><h1>Control<br>Freak</h1><span class="state">Web app online</span></header>
<section><h2>Machine status</h2><dl><div><dt>Machine</dt><dd>{escape(status['machine'])}</dd></div><div><dt>Installed release</dt><dd>{escape(status['release'])}</dd></div><div><dt>Access</dt><dd>Authenticated local session</dd></div></dl></section>
<section><h2>Blocked audio outputs</h2><p>This is the desired blacklist policy. Mute enforcement can fail independently.</p>{audio}</section>
</main></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    state_path: Path

    def log_message(self, format: str, *args) -> None:
        pass

    def _send(self, status: HTTPStatus, body: bytes = b"", content_type: str = "text/html; charset=utf-8", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _allowed(self, state: dict) -> bool:
        destination = self.connection.getsockname()[0]
        tailscale_ip = _tailscale_ip() if ipaddress.IPv4Address(destination) in TAILSCALE_NETWORK else None
        return request_allowed(destination, state, tailscale_ip, _lan_ip())

    def _session(self, state: dict) -> str:
        if not self._allowed(state):
            return ""
        cookie = next((part.strip().split("=", 1)[1] for part in self.headers.get("Cookie", "").split(";") if part.strip().startswith("cf_session=")), "")
        if not cookie or not state["session_hash"]:
            return ""
        return cookie if hmac.compare_digest(_credential_hash(state["session_salt"], cookie), state["session_hash"]) else ""

    def do_GET(self) -> None:
        state = read_state(self.state_path)
        route = urlsplit(self.path)
        if route.path == "/health":
            if not ipaddress.ip_address(self.connection.getsockname()[0]).is_loopback:
                self._send(HTTPStatus.FORBIDDEN)
                return
            status = HTTPStatus.OK if state["enabled"] else HTTPStatus.SERVICE_UNAVAILABLE
            self._send(status, b'{"status":"ready"}' if state["enabled"] else b'{"status":"disabled"}', "application/json")
            return
        if not state["enabled"]:
            self._send(HTTPStatus.NOT_FOUND)
            return
        if route.path == "/pair":
            if not self._allowed(state):
                self._send(HTTPStatus.FORBIDDEN)
                return
            supplied = parse_qs(route.query).get("token", [""])[0]
            session = ""
            with locked(self.state_path) as (directory_fd, name):
                state = _read_state(directory_fd, name)
                valid = bool(
                    supplied and state["pair_hash"] and time.time() <= state["pair_expires"]
                    and hmac.compare_digest(_credential_hash(state["pair_salt"], supplied), state["pair_hash"])
                )
                if valid:
                    session = secrets.token_urlsafe(32)
                    salt = secrets.token_hex(16)
                    state = {**state, "pair_salt": "", "pair_hash": "", "pair_expires": 0, "session_salt": salt, "session_hash": _credential_hash(salt, session)}
                    write_json(directory_fd, name, state)
            if valid:
                cookie = f"cf_session={session}; Path=/; Secure; HttpOnly; SameSite=Strict"
                self._send(HTTPStatus.SEE_OTHER, headers={"Location": "/", "Set-Cookie": cookie})
            else:
                self._send(HTTPStatus.UNAUTHORIZED, b"Pairing link is invalid.", "text/plain; charset=utf-8")
            return
        session = self._session(state)
        if not session:
            self._send(HTTPStatus.UNAUTHORIZED, b"Open the pairing QR code from the Control Freak Batocera app.", "text/plain; charset=utf-8")
            return
        if route.path != "/":
            self._send(HTTPStatus.NOT_FOUND)
            return
        self._send(HTTPStatus.OK, _page(session))

    def do_POST(self) -> None:
        state = read_state(self.state_path)
        session = self._session(state)
        if not session or urlsplit(self.path).path != "/audio":
            self._send(HTTPStatus.UNAUTHORIZED)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if not 0 <= length <= 4096 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self._send(HTTPStatus.BAD_REQUEST)
            return
        try:
            form = parse_qs(self.rfile.read(length).decode("utf-8", "strict"), keep_blank_values=True)
        except UnicodeDecodeError:
            self._send(HTTPStatus.BAD_REQUEST)
            return
        supplied = form.get("csrf", [""])[0]
        sink = form.get("sink", [""])[0]
        desired = form.get("blocked", [""])[0]
        if not hmac.compare_digest(supplied, csrf_token(session)) or desired not in {"0", "1"}:
            self._send(HTTPStatus.BAD_REQUEST)
            return
        try:
            set_sink_blocked(sink, desired == "1")
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            self._send(HTTPStatus.CONFLICT, b"Audio policy could not be updated.", "text/plain; charset=utf-8")
            return
        self._send(HTTPStatus.SEE_OTHER, headers={"Location": "/"})


class BoundedTLSServer(ThreadingHTTPServer):
    daemon_threads = True
    request_timeout = 5.0
    maximum_handlers = 16

    def __init__(self, address, handler, context: ssl.SSLContext):
        self.tls_context = context
        self.handlers = BoundedSemaphore(self.maximum_handlers)
        super().__init__(address, handler)

    def get_request(self):
        request, address = self.socket.accept()
        request.settimeout(self.request_timeout)
        return request, address

    def process_request(self, request, client_address) -> None:
        if not self.handlers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.handlers.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            wrapped = self.tls_context.wrap_socket(cast(socket.socket, request), server_side=True)
            super().process_request_thread(wrapped, client_address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
        finally:
            self.handlers.release()


def make_server(address: tuple[str, int], state_path: Path = STATE_PATH) -> BoundedTLSServer:
    class ConfiguredHandler(Handler):
        pass

    ConfiguredHandler.state_path = state_path
    certificate, key = ensure_tls(state_path, [value for value in (_lan_ip(), _tailscale_ip()) if value])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    server = BoundedTLSServer(address, ConfiguredHandler, context)
    return server


def main() -> None:
    server = None
    thread = None
    while True:
        enabled = read_state(STATE_PATH)["enabled"]
        if enabled and server is None:
            try:
                server = make_server(("0.0.0.0", 8787))
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
            except OSError:
                server = None
                time.sleep(3)
                continue
        elif not enabled and server is not None:
            server.shutdown()
            server.server_close()
            if thread:
                thread.join(timeout=2)
            server = None
            thread = None
        try:
            enforce_audio()
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            pass
        time.sleep(3)


if __name__ == "__main__":
    main()
