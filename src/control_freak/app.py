from __future__ import annotations

import argparse
from concurrent.futures import Future
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
from threading import Thread
from typing import Any

import pygame

from .audio import outputs as audio_outputs, read_state as read_audio_state, set_blocked
from .input import EventRouter, configure_sdl_controllers
from .model import Action, Confirmation, build_fixture_model
from .preferences import read_state as read_preferences, update_state as update_preferences
from .runtime import configure_web, read_service_enabled, set_service_enabled, web_health
from .updates import UpdateError, cached_notes, check_for_update, daily_check, install_candidate
from .ui import HEIGHT, WIDTH, clear_font_cache, render
from .web import read_state, update_state as update_web_state

TAILSCALE = Path("/usr/bin/tailscale")


ES_INPUT_CONFIG = Path("/userdata/system/configs/emulationstation/es_input.cfg")
UPDATE_STATE = Path("/userdata/system/control-freak/updates.json")
NOTES = Path("/userdata/system/control-freak/release-notes")
USERDATA = Path("/userdata")
INSTALLER = Path("/userdata/system/control-freak/app/install_batocera.py")


def save_screenshot(path: Path) -> None:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    clear_font_cache()
    pygame.init()
    surface = pygame.Surface((WIDTH, HEIGHT))
    model = build_fixture_model()
    model.select("web-access")
    render(surface, model)
    path.parent.mkdir(parents=True, exist_ok=True)
    pygame.image.save(surface, path)
    clear_font_cache()
    pygame.quit()


def _display_mode(windowed: bool, desktop_sizes: list[tuple[int, int]]) -> tuple[tuple[int, int], int]:
    if windowed:
        return (WIDTH, HEIGHT), pygame.RESIZABLE
    return desktop_sizes[0], pygame.FULLSCREEN


def _apply_web_change(model, before: tuple[bool, bool], after: tuple[bool, bool], configure=configure_web) -> bool:
    try:
        state = configure(after[0], after[1])
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        model.sync_web(before[0], before[1], model.web_runtime_available, model.web_token)
        message = f"Could not update Web app: {exc}"
        model.row("web-access").description = message
        model.current_row.description = message
        return False
    model.sync_web(state["enabled"], state["tailscale"], state["running"], state.get("pairing_token", model.web_token))
    return True


def _apply_preferences_change(model, save=update_preferences) -> bool:
    try:
        save(
            update_channel=str(model.row("update-channel").value).lower(),
            check_updates_daily=bool(model.row("check-updates-daily").value),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        model.current_row.description = f"Could not save update settings: {exc}"
        return False
    return True


def _apply_service_change(model, before: bool, after: bool, configure=set_service_enabled) -> bool:
    try:
        model.row("batocera-service").value = configure(after)
    except (OSError, RuntimeError) as exc:
        model.row("batocera-service").value = before
        model.row("batocera-service").description = f"Could not update Batocera service: {exc}"
        return False
    return True


def _check_update(model, installed: str, public_key: bytes, check=check_for_update) -> dict | None:
    row = model.row("check-updates")
    try:
        result = check(str(model.row("update-channel").value).lower(), installed, public_key, UPDATE_STATE)
    except (UpdateError, OSError, ValueError) as exc:
        row.available = True
        row.value = "Check failed"
        row.description = f"Could not check for a signed update: {exc}"
        return None
    row.available = True
    if not result["available"]:
        row.value = "Up to date"
        row.description = "The installed release is current for this channel."
        return None
    row.value = f"{result['release_id']} available"
    model.pending_confirmation = Confirmation("check-updates", "Install update", f"Install verified {result['release_id']} now? The current version will be retained for rollback.")
    return result["_candidate"]


def _start_update_check(model, installed: str, public_key: bytes, check=check_for_update) -> Future:
    row = model.row("check-updates")
    channel = str(model.row("update-channel").value).lower()
    row.value = "Checking…"
    row.description = "Checking the signed release feed…"
    future = Future()

    def run_check() -> None:
        try:
            future.set_result((channel, check(channel, installed, public_key, UPDATE_STATE)))
        except BaseException as exc:
            future.set_exception(exc)

    Thread(target=run_check, daemon=True).start()
    return future


def _request_update_check(model, pending: Future | None, installed: str, public_key: bytes) -> Future | None:
    if pending is not None:
        return pending
    if not public_key:
        model.row("check-updates").description = "The signed update key is unavailable."
        return None
    return _start_update_check(model, installed, public_key)


def _finish_update_check(model, future: Future) -> dict | None:
    try:
        channel, result = future.result()
    except (UpdateError, OSError, ValueError):
        return _check_update(model, "", b"", check=lambda *_args: future.result()[1])
    if channel != str(model.row("update-channel").value).lower():
        row = model.row("check-updates")
        row.value = "Check again"
        row.description = "The update channel changed while the previous check was running."
        return None
    return _check_update(model, "", b"", check=lambda *_args: result)


def _show_release_notes(model, installed: str, public_key: bytes, load=cached_notes) -> bool:
    try:
        notes = load(installed, public_key, NOTES)
    except (UpdateError, OSError) as exc:
        model.row("whats-new").description = str(exc)
        return False
    model.show_overlay(f"What's new in {installed}", notes)
    return True


def _update_key() -> bytes:
    try:
        return os.pread(int(os.environ.get("CONTROL_FREAK_UPDATE_KEY_FD", "")), 16384, 0)
    except (OSError, ValueError) as exc:
        raise UpdateError("The signed update key is unavailable") from exc


def _local_ipv4(socket_factory: Any = socket.socket) -> str:
    try:
        with socket_factory(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("192.0.2.1", 9))
            return str(ipaddress.IPv4Address(connection.getsockname()[0]))
    except (OSError, ipaddress.AddressValueError):
        return "Offline"


def _connection_type(route_text: str | None = None, interfaces: Path = Path("/sys/class/net")) -> str:
    if route_text is None:
        try:
            route_text = Path("/proc/net/route").read_text()
        except OSError:
            return "Network"
    for line in route_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == "00000000" and fields[0] != "lo":
            return "Wireless" if (interfaces / fields[0] / "wireless").is_dir() else "Ethernet"
    return "Network"


def _tailscale_ipv4() -> str | None:
    if not TAILSCALE.exists():
        return None
    try:
        result = subprocess.run([str(TAILSCALE), "ip", "-4"], capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    candidate = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
    try:
        return str(ipaddress.IPv4Address(candidate))
    except ipaddress.AddressValueError:
        return None


def _refresh_pairing(path: Path, state: dict, issue=update_web_state) -> str:
    return issue(path, enabled=state["enabled"], tailscale=state["tailscale"])["pairing_token"]


def _installed_version(path: Path = Path("/userdata/system/control-freak/app/current.json")) -> str:
    try:
        value = json.loads(path.read_text()).get("release_id")
    except (OSError, json.JSONDecodeError):
        return "Unknown"
    return value if isinstance(value, str) else "Unknown"


def _previous_version(run=subprocess.run) -> str | None:
    try:
        result = run(["/usr/bin/python3", str(INSTALLER), "--root", str(USERDATA), "--rollback-info"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value and len(value) <= 80 and all(character.isalnum() or character in ".-_" for character in value) else None


def run(windowed: bool = False) -> None:
    pygame.init()
    controller_handles, mapped_ids = configure_sdl_controllers(ES_INPUT_CONFIG)
    router = EventRouter(mapped_ids, controller_handles)
    size, flags = _display_mode(windowed, pygame.display.get_desktop_sizes())
    screen = pygame.display.set_mode(size, flags)
    pygame.display.set_caption("Control Freak")
    canvas = pygame.Surface((WIDTH, HEIGHT))
    clock = pygame.time.Clock()
    ip_address = _local_ipv4()
    tailscale_address = _tailscale_ipv4()
    web_state_path = Path("/userdata/system/control-freak/web.json")
    web_state = read_state(web_state_path)
    preferences = read_preferences()
    web_running = bool(web_state["enabled"] and web_health())
    web_token = ""
    if web_running:
        try:
            web_token = _refresh_pairing(web_state_path, web_state)
        except (OSError, ValueError, json.JSONDecodeError):
            web_running = False
    try:
        audio_policy = read_audio_state()
        audio_devices = audio_outputs()
        live_audio = {item["value"] for item in audio_devices}
        audio_devices += [
            {"value": value, "label": f"{value} (currently unavailable)"}
            for value in audio_policy["blocked"]
            if value not in live_audio
        ]
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        audio_policy = {"blocked": []}
        audio_devices = []
    installed = _installed_version()
    update_key = b""
    update_release = None
    notes_available = False
    try:
        update_key = _update_key()
        cached_notes(installed, update_key, NOTES)
        notes_available = True
    except (UpdateError, OSError):
        pass
    if preferences["check_updates_daily"] and update_key:
        try:
            daily = daily_check(preferences["update_channel"], installed, update_key, UPDATE_STATE)
            update_release = daily["release_id"] if daily["available"] else None
        except (UpdateError, OSError, ValueError):
            pass
    model = build_fixture_model(
        socket.gethostname().upper(),
        ip_address,
        _connection_type(),
        tailscale_address=tailscale_address,
        web_runtime_available=web_running,
        web_token=web_token,
        web_enabled=web_state["enabled"],
        tailscale_enabled=web_state["tailscale"],
        installed_version=installed,
        update_channel=preferences["update_channel"],
        check_updates_daily=preferences["check_updates_daily"],
        update_release=update_release,
        notes_available=notes_available,
        previous_version=_previous_version(),
        service_enabled=read_service_enabled(),
        audio_outputs=audio_devices,
        blocked_audio=audio_policy["blocked"],
    )

    pending_update = None
    pending_update_check = None
    while model.running:
        events = pygame.event.get()
        for event in events:
            if event.type == pygame.QUIT:
                model.running = False
        for action in router.actions(events):
            confirmation_before = model.pending_confirmation is not None
            before = (bool(model.row("web-access").value), bool(model.row("tailscale").value))
            before_audio = {
                row.id: bool(row.value)
                for section in model.sections
                for row in section.rows
                if row.id.startswith("audio-block:")
            }
            before_preferences = (
                str(model.row("update-channel").value),
                bool(model.row("check-updates-daily").value),
            )
            before_service = bool(model.row("batocera-service").value)
            model.dispatch(action)
            after = (bool(model.row("web-access").value), bool(model.row("tailscale").value))
            if after != before:
                _apply_web_change(model, before, after)
            after_audio = {
                row.id: bool(row.value)
                for section in model.sections
                for row in section.rows
                if row.id.startswith("audio-block:")
            }
            if after_audio != before_audio:
                try:
                    policy = set_blocked([row_id.split(":", 1)[1] for row_id, blocked in after_audio.items() if blocked])
                except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
                    for row_id, value in before_audio.items():
                        model.row(row_id).value = value
                    model.current_row.description = f"Could not update audio policy: {exc}"
                else:
                    for row_id in after_audio:
                        model.row(row_id).value = row_id.split(":", 1)[1] in policy["blocked"]
            after_preferences = (
                str(model.row("update-channel").value),
                bool(model.row("check-updates-daily").value),
            )
            if after_preferences != before_preferences:
                _apply_preferences_change(model)
            after_service = bool(model.row("batocera-service").value)
            if after_service != before_service:
                _apply_service_change(model, before_service, after_service)
            requested = model.last_action
            model.last_action = None
            if requested == "check-updates":
                if confirmation_before and action is Action.SELECT and pending_update is not None:
                    try:
                        installed = install_candidate(pending_update, USERDATA, NOTES)
                        model.row("installed-version").value = installed
                        model.row("check-updates").value = "Installed"
                        model.row("whats-new").available = True
                        model.row("whats-new").value = "View notes"
                        previous = _previous_version()
                        model.row("rollback").available = previous is not None
                        model.row("rollback").value = previous or "Unavailable"
                    except (UpdateError, OSError, ValueError) as exc:
                        model.row("check-updates").value = "Install failed"
                        model.row("check-updates").description = f"Verified update was not installed: {exc}"
                    pending_update = None
                else:
                    pending_update_check = _request_update_check(model, pending_update_check, installed, update_key)
            elif requested == "whats-new":
                _show_release_notes(model, installed, update_key)
            elif requested == "rollback" and confirmation_before:
                try:
                    result = subprocess.run(["/usr/bin/python3", str(INSTALLER), "--root", str(USERDATA), "--rollback"], timeout=300, check=False)
                    if result.returncode:
                        raise RuntimeError("rollback installer failed safely")
                    installed = _installed_version()
                    model.row("installed-version").value = installed
                    previous = _previous_version()
                    model.row("rollback").available = previous is not None
                    model.row("rollback").value = previous or "Unavailable"
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    model.row("rollback").description = f"Rollback failed safely: {exc}"

        if pending_update_check is not None and pending_update_check.done():
            pending_update = _finish_update_check(model, pending_update_check)
            pending_update_check = None

        render(canvas, model)
        destination = screen.get_rect()
        scale = min(destination.w / WIDTH, destination.h / HEIGHT)
        size = (round(WIDTH * scale), round(HEIGHT * scale))
        scaled = pygame.transform.smoothscale(canvas, size)
        screen.fill((0, 0, 0))
        screen.blit(scaled, scaled.get_rect(center=destination.center))
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Control Freak fixture-backed UI prototype")
    parser.add_argument("--windowed", action="store_true", help="run in a resizable window")
    parser.add_argument("--screenshot", type=Path, help="render a deterministic 1280x720 screenshot and exit")
    args = parser.parse_args()
    if args.screenshot:
        save_screenshot(args.screenshot)
    else:
        run(args.windowed)


if __name__ == "__main__":
    main()
