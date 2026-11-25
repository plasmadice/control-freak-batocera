from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
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

    def dispatch(self, action: Action) -> None:
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
                message = "Disable web access? Browser sessions will disconnect, but local settings will remain available here."
            self.pending_confirmation = Confirmation(row.id, row.label, message)
            return
        self._apply(row)

    def _apply(self, row: SettingRow) -> None:
        if row.kind is RowKind.TOGGLE:
            row.value = not bool(row.value)
            if row.id == "web-access":
                enabled = bool(row.value)
                address = self.row("web-address")
                address.available = enabled and self.web_runtime_available
                address.value = self.web_address if address.available else "Unavailable" if enabled else "Not running"
                address.reason = "" if address.available else "Web service is not available in this build." if enabled else "Enable Web access first."
                tailscale = self.row("tailscale")
                tailscale.available = enabled and self.web_runtime_available and self.tailscale_address is not None
                tailscale.reason = (
                    "" if tailscale.available
                    else "Web service is not available in this build."
                    if enabled and not self.web_runtime_available
                    else "Tailscale is not installed or connected on this machine."
                    if self.tailscale_address is None
                    else "Web access must be enabled first."
                )
                if not enabled:
                    tailscale.value = False
                tailscale_address = self.row("tailscale-address")
                tailscale_address.available = False
                tailscale_address.value = "Hidden"
                tailscale_address.reason = "Enable Tailscale access to show this address."
            elif row.id == "tailscale":
                address = self.row("tailscale-address")
                address.available = bool(row.value)
                address.value = f"http://{self.tailscale_address}:8787" if row.value else "Hidden"
                address.reason = "" if row.value else "Enable Tailscale access to show this address."
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
    installed_version: str = "0.1.0-dev",
) -> AppModel:
    if tailscale_installed and tailscale_address is None:
        tailscale_address = "100.64.0.10"
    return AppModel(
        sections=[
            Section(
                "overview",
                "Overview",
                "Current machine state and the few things that need attention.",
                [
                    SettingRow("device", "Machine", "The Batocera system currently running Control Freak.", RowKind.READONLY, device_name),
                    SettingRow("health", "System health", "Core services are responding and persistent storage is writable.", RowKind.READONLY, "Healthy", level="good"),
                    SettingRow("storage", "Userdata storage", "Available capacity on /userdata.", RowKind.READONLY, "184 GB free"),

                ],
            ),
            Section(
                "display",
                "Display & Audio",
                "Cabinet-facing output settings with the active value always visible.",
                [

                    SettingRow("audio-output", "Audio output", "Cycle through outputs detected by Batocera.", RowKind.CHOICE, "HDMI", ("HDMI", "Analog", "Auto")),
                    SettingRow("volume", "System volume", "The master volume managed by Batocera.", RowKind.CHOICE, "80%", ("60%", "70%", "80%", "90%", "100%")),
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
                        "Web access",
                        "Run the optional Control Freak web interface on this machine. Core settings remain available here when it is off.",
                        RowKind.TOGGLE,
                        False,
                        confirm="Enable authenticated web access on the local network? You can turn it off here at any time.",
                    ),
                    SettingRow("web-address", "Web address", "Shown after web access starts successfully.", RowKind.READONLY, "Not running", available=False, reason="Enable Web access first."),
                    SettingRow("tailscale", "Tailscale access", "Allow the web interface on the existing tailnet without making it public.", RowKind.TOGGLE, False, available=False, reason="Tailscale is not installed or connected on this machine." if tailscale_address is None else "Web access must be enabled first."),
                    SettingRow("tailscale-address", "Tailscale address", "Shown only while Tailscale access is enabled.", RowKind.READONLY, "Hidden", available=False, reason="Enable Tailscale access to show this address."),
                ],
            ),
            Section(
                "updates",
                "Updates",
                "Release channel, installed version, and a safe path back.",
                [
                    SettingRow("installed-version", "Installed version", "The immutable application release currently active.", RowKind.READONLY, installed_version),
                    SettingRow("update-status", "Update status", "No release feed is configured in this build.", RowKind.READONLY, "Not checked"),
                    SettingRow("install-stable", "Install stable update", "Install the newest operator-staged stable release. Stable remains the default channel.", RowKind.ACTION, "Staged release", confirm="Install the staged stable release and use it on the next launch?", field_label="RELEASE ACTION", field_value="INSTALL STAGED STABLE"),
                    SettingRow("install-nightly", "Install nightly update", "Install the newest operator-staged nightly build. Nightly may contain incomplete behavior.", RowKind.ACTION, "Staged release", confirm="Install the staged nightly release and switch this machine to the nightly channel?", field_label="RELEASE ACTION", field_value="INSTALL STAGED NIGHTLY"),
                    SettingRow("rollback", "Roll back", "Restore the previously installed verified release.", RowKind.ACTION, "Unavailable", available=False, reason="No previous release is stored on this machine."),
                ],
            ),
            Section(
                "system",
                "System",
                "Diagnostics and carefully bounded machine actions.",
                [
                    SettingRow("diagnostics", "Export diagnostics", "Create a support archive that excludes credentials and game content.", RowKind.ACTION, "Create archive"),

                ],
            ),
        ],
        web_address=f"http://{ip_address}:8787",
        web_runtime_available=web_runtime_available,
        tailscale_address=tailscale_address,
    )
