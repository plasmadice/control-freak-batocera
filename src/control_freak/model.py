from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
import ipaddress
import re
import textwrap
from typing import Any


class Action(Enum):
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    SELECT = auto()
    BACK = auto()


class RowKind(Enum):
    READONLY = auto()
    TOGGLE = auto()
    CHOICE = auto()
    ACTION = auto()


@dataclass
class SettingRow:
    id: str
    label: str
    description: str
    kind: RowKind
    value: Any = None
    choices: tuple[str, ...] = ()
    available: bool = True
    reason: str = ""
    level: str = "normal"
    confirm: str = ""
    field_label: str = "CURRENT"
    field_value: str | None = None
    qr_value: str | None = None


@dataclass
class Section:
    id: str
    title: str
    summary: str
    rows: list[SettingRow] = field(default_factory=list)


@dataclass
class Confirmation:
    row_id: str
    title: str
    message: str


def _audio_output_type(label: str) -> tuple[str, int | None]:
    if match := re.search(r"\b(HDMI|DisplayPort)(?:\s+(\d+))?\b", label, re.IGNORECASE):
        return ("DisplayPort" if match.group(1).lower() == "displayport" else "HDMI", int(match.group(2)) if match.group(2) else None)
    for name in ("Headphones", "Speakers", "Analog"):
        if name.lower().rstrip("s") in label.lower():
            return name, None
    return "Output", None


@dataclass
class AppModel:
    sections: list[Section]
    section_index: int = 0
    row_index: int = 0
    rows_focused: bool = False
    pending_confirmation: Confirmation | None = None
    last_action: str | None = None
    running: bool = True
    web_address: str = ""
    web_runtime_available: bool = False
    tailscale_address: str | None = None
    web_token: str = ""
    overlay_title: str = ""
    overlay_pages: list[list[str]] = field(default_factory=list)
    overlay_page: int = 0

    @property
    def current_section(self) -> Section:
        return self.sections[self.section_index]

    @property
    def current_row(self) -> SettingRow:
        return self.current_section.rows[self.row_index]

    def row(self, row_id: str) -> SettingRow:
        for section in self.sections:
            for row in section.rows:
                if row.id == row_id:
                    return row
        raise KeyError(row_id)

    def select(self, row_id: str) -> None:
        for section_index, section in enumerate(self.sections):
            for row_index, row in enumerate(section.rows):
                if row.id == row_id:
                    self.section_index = section_index
                    self.row_index = row_index
                    self.rows_focused = True
                    return
        raise KeyError(row_id)

    def show_overlay(self, title: str, text: str) -> None:
        lines = [line for paragraph in text.splitlines() for line in (textwrap.wrap(paragraph, width=58) or [""])]
        self.overlay_title = title
        self.overlay_pages = [lines[index:index + 11] for index in range(0, len(lines), 11)] or [[""]]
        self.overlay_page = 0

    def sync_web(self, enabled: bool, tailscale_enabled: bool, runtime_available: bool, token: str) -> None:
        self.web_runtime_available = runtime_available
        self.web_token = token
        web = self.row("web-access")
        web.value = enabled
        address = self.row("web-address")
        address.available = enabled and runtime_available and bool(self.web_address)
        address.value = self.web_address if address.available else "Unavailable" if enabled else "Not running"
        address.qr_value = f"{self.web_address}/pair?token={token}" if address.available and token else None
        address.reason = "" if address.available else "No usable LAN address is available." if enabled and runtime_available else "The Web app did not start." if enabled else "Enable the Web app first."
        tailscale = self.row("tailscale")
        tailscale.available = enabled and runtime_available and self.tailscale_address is not None
        tailscale.value = bool(tailscale_enabled and tailscale.available)
        tailscale.reason = (
            "" if tailscale.available
            else "The Web app did not start." if enabled and not runtime_available
            else "Tailscale is not installed or connected on this machine." if self.tailscale_address is None
            else "The Web app must be enabled first."
        )
        tailscale_address = self.row("tailscale-address")
        tailscale_address.available = bool(tailscale.value)
        tailscale_address.value = f"https://{self.tailscale_address}:8787" if tailscale.value else "Hidden"
        tailscale_address.qr_value = f"{tailscale_address.value}/pair?token={token}" if tailscale.value and token else None
        tailscale_address.reason = "" if tailscale.value else "Enable Tailscale access to show this address."

    def dispatch(self, action: Action) -> None:
        if self.overlay_pages:
            if action in (Action.BACK, Action.SELECT):
                self.overlay_pages = []
            elif action in (Action.DOWN, Action.RIGHT):
                self.overlay_page = min(len(self.overlay_pages) - 1, self.overlay_page + 1)
            elif action in (Action.UP, Action.LEFT):
                self.overlay_page = max(0, self.overlay_page - 1)
            return
        if self.pending_confirmation:
            if action is Action.SELECT:
                row = self.row(self.pending_confirmation.row_id)
                self.pending_confirmation = None
                self._apply(row)
            elif action is Action.BACK:
                self.pending_confirmation = None
            return

        if action is Action.BACK:
            if self.rows_focused:
                self.rows_focused = False
                self.row_index = 0
            else:
                self.running = False
            return

        if not self.rows_focused:
            if action is Action.UP:
                self.section_index = (self.section_index - 1) % len(self.sections)
                self.row_index = 0
            elif action is Action.DOWN:
                self.section_index = (self.section_index + 1) % len(self.sections)
                self.row_index = 0
            elif action in (Action.RIGHT, Action.SELECT):
                self.rows_focused = True
            return

        if action is Action.UP:
            self.row_index = max(0, self.row_index - 1)
        elif action is Action.DOWN:
            self.row_index = min(len(self.current_section.rows) - 1, self.row_index + 1)
        elif action is Action.LEFT:
            self.rows_focused = False
            self.row_index = 0
        elif action is Action.SELECT:
            self._activate(self.current_row)

    def _activate(self, row: SettingRow) -> None:
        if not row.available or row.kind is RowKind.READONLY:
            return
        if row.confirm:
            message = row.confirm
            if row.id == "web-access" and bool(row.value):
                message = "Disable the Web app? Browser sessions will disconnect, but local settings will remain available here."
            elif row.id == "batocera-service" and bool(row.value):
                message = "Disable the Batocera service? The Web app and background audio-output blacklist enforcement will stop. The local Control Freak app will remain available."
            self.pending_confirmation = Confirmation(row.id, row.label, message)
            return
        self._apply(row)

    def _apply(self, row: SettingRow) -> None:
        if row.kind is RowKind.TOGGLE:
            row.value = not bool(row.value)
            if row.id == "web-access":
                self.sync_web(bool(row.value), False, self.web_runtime_available, self.web_token)
            elif row.id == "tailscale":
                self.sync_web(bool(self.row("web-access").value), bool(row.value), self.web_runtime_available, self.web_token)
        elif row.kind is RowKind.CHOICE and row.choices:
            index = row.choices.index(str(row.value))
            row.value = row.choices[(index + 1) % len(row.choices)]
        elif row.kind is RowKind.ACTION:
            self.last_action = row.id


def build_fixture_model(
    device_name: str = "BATMINI",
    ip_address: str = "192.168.1.87",
    connection_type: str = "Ethernet",
    tailscale_installed: bool = False,
    tailscale_address: str | None = None,
    web_runtime_available: bool = False,
    web_token: str = "",
    web_enabled: bool = False,
    tailscale_enabled: bool = False,
    installed_version: str = "0.1.0-dev",
    update_channel: str = "stable",
    check_updates_daily: bool = True,
    update_status: str = "Check now",
    update_release: str | None = None,
    notes_available: bool = False,
    previous_version: str | None = None,
    service_enabled: bool = True,
    audio_outputs: list[dict[str, str]] | None = None,
    blocked_audio: list[str] | None = None,
) -> AppModel:
    try:
        lan_address = str(ipaddress.IPv4Address(ip_address))
    except ipaddress.AddressValueError:
        lan_address = ""
    if tailscale_installed and tailscale_address is None:
        tailscale_address = "100.64.0.10"
    try:
        if ipaddress.IPv4Address(tailscale_address or "") not in ipaddress.IPv4Network("100.64.0.0/10"):
            tailscale_address = None
    except ipaddress.AddressValueError:
        tailscale_address = None
    blocked = set(blocked_audio or [])
    output_types = [_audio_output_type(item["label"]) for item in audio_outputs or []]
    type_counts = {name: sum(output_type == name for output_type, _number in output_types) for name, _number in output_types}
    reserved_numbers = {
        name: {number for output_type, number in output_types if output_type == name and number is not None}
        for name, _number in output_types
    }
    assigned_numbers: dict[int, int] = {}
    for output_type, count in type_counts.items():
        if count == 1:
            continue
        used: set[int] = set()
        group = [index for index, value in enumerate(output_types) if value[0] == output_type]
        explicit = {
            number: min(
                (index for index in group if output_types[index][1] == number),
                key=lambda index: (audio_outputs or [])[index]["value"],
            )
            for number in reserved_numbers[output_type]
        }
        indexes = sorted(
            group,
            key=lambda index: (
                0 if index in explicit.values() else 1 if output_types[index][1] is None else 2,
                (audio_outputs or [])[index]["value"],
            ),
        )
        for index in indexes:
            assigned = output_types[index][1]
            if assigned is None or assigned in used:
                assigned = 1
                while assigned in used or assigned in reserved_numbers[output_type]:
                    assigned += 1
            used.add(assigned)
            assigned_numbers[index] = assigned
    audio_rows = []
    for index, (item, (output_type, number)) in enumerate(zip(audio_outputs or [], output_types)):
        if type_counts[output_type] == 1:
            suffix = "" if number is None else f" {number}"
        else:
            suffix = f" {assigned_numbers[index]}"
        audio_rows.append(
            SettingRow(
                f"audio-block:{item['value']}",
                f"Block audio from {output_type}{suffix}",
                item["label"],
                RowKind.TOGGLE,
                item["value"] in blocked,
                field_label="OUTPUT",
                field_value=item["label"],
            )
        )
    model = AppModel(
        sections=[
            Section(
                "overview",
                "Overview",
                "Current machine state and the few things that need attention.",
                [
                    SettingRow("device", "Machine", "The Batocera system currently running Control Freak.", RowKind.READONLY, device_name),
                ],
            ),
            Section(
                "display",
                "Display & Audio",
                "Cabinet-facing output settings with the active value always visible.",
                [
                    *audio_rows,
                    SettingRow("crt-mode", "CRT output profile", "Requires a compatible analog video configuration.", RowKind.CHOICE, "Off", ("Off",), False, "No compatible CRT profile was detected."),
                ],
            ),
            Section(
                "network",
                "Network & Web",
                "Local connectivity and optional browser access to this machine.",
                [
                    SettingRow("network-status", "Network", "Current local connection and address.", RowKind.READONLY, f"{connection_type} · {ip_address}" if ip_address != "Offline" else "Offline", level="good"),
                    SettingRow(
                        "web-access",
                        "Web app",
                        "Run the optional Control Freak browser interface on this machine. Core settings remain available here when it is off.",
                        RowKind.TOGGLE,
                        False,
                        confirm="Enable the authenticated Web app on the local network? You can turn it off here at any time.",
                    ),
                    SettingRow("web-address", "Web address", "Shown with a pairing QR code after the Web app starts successfully.", RowKind.READONLY, "Not running", available=False, reason="Enable the Web app first."),
                    SettingRow("tailscale", "Tailscale access", "Allow the Web app on the existing tailnet without making it public.", RowKind.TOGGLE, False, available=False, reason="Tailscale is not installed or connected on this machine." if tailscale_address is None else "The Web app must be enabled first."),
                    SettingRow("tailscale-address", "Tailscale address", "Shown with a pairing QR code only while Tailscale access is enabled.", RowKind.READONLY, "Hidden", available=False, reason="Enable Tailscale access to show this address."),
                ],
            ),
            Section(
                "updates",
                "Updates",
                "Release channel, installed version, and a safe path back.",
                [
                    SettingRow("installed-version", "Installed version", "The immutable application release currently active.", RowKind.READONLY, installed_version),
                    SettingRow("update-channel", "Update channel", "Stable receives tested releases. Nightly receives newer changes sooner, with less testing.", RowKind.CHOICE, "Nightly" if update_channel == "nightly" else "Stable", choices=("Stable", "Nightly")),
                    SettingRow("check-updates-daily", "Check for updates daily", "Check the public signed release feed once per day. Daily checks never install.", RowKind.TOGGLE, check_updates_daily),
                    SettingRow("check-updates", "Check for updates", "Discover a signed release now. Installation requires a separate confirmation.", RowKind.ACTION, f"{update_release} available" if update_release else update_status, field_label="UPDATE ACTION", field_value="CHECK FOR UPDATES"),
                    SettingRow("whats-new", "What's new in this version", "Show authenticated release notes cached with the installed version.", RowKind.ACTION, "View notes" if notes_available else "Unavailable", available=notes_available, reason="" if notes_available else "This installed release has no verified offline release notes."),
                    SettingRow(
                        "rollback",
                        "Go back to the previous version",
                        "Restore the previous verified version. The current version is retained so this can be undone.",
                        RowKind.ACTION,
                        previous_version or "Unavailable",
                        available=previous_version is not None,
                        reason="" if previous_version is not None else "No previous version is stored on this machine.",
                        confirm=f"Go back to {previous_version}? The current version will become the previous version." if previous_version else "",
                    ),
                ],
            ),
            Section(
                "system",
                "System",
                "Diagnostics and carefully bounded machine actions.",
                [
                    SettingRow("batocera-service", "Batocera service", "Start Control Freak automatically with Batocera. This keeps the Web app and audio-output blacklist enforcement available in the background.", RowKind.TOGGLE, service_enabled, confirm="Enable the Control Freak Batocera service at startup?"),
                ],
            ),
        ],
        web_address=f"https://{lan_address}:8787" if lan_address else "",
        web_runtime_available=web_runtime_available,
        tailscale_address=tailscale_address,
        web_token=web_token,
    )
    model.sync_web(web_enabled, tailscale_enabled, web_runtime_available, web_token)
    return model
