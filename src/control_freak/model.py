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
                address.available = enabled
                address.value = "Starting…" if enabled else "Not running"
                address.reason = "" if enabled else "Enable Web access first."
                tailscale = self.row("tailscale")
                tailscale.available = enabled
                tailscale.reason = "" if enabled else "Web access must be enabled first."
                if not enabled:
                    tailscale.value = False
        elif row.kind is RowKind.CHOICE and row.choices:
            index = row.choices.index(str(row.value))
            row.value = row.choices[(index + 1) % len(row.choices)]
        elif row.kind is RowKind.ACTION:
            self.last_action = row.id


def build_fixture_model() -> AppModel:
    return AppModel(
        sections=[
            Section(
                "overview",
                "Overview",
                "Current machine state and the few things that need attention.",
                [
                    SettingRow("device", "Machine", "The Batocera system currently running Control Freak.", RowKind.READONLY, "BATMINI"),
                    SettingRow("health", "System health", "Core services are responding and persistent storage is writable.", RowKind.READONLY, "Healthy", level="good"),
                    SettingRow("storage", "Userdata storage", "Available capacity on /userdata.", RowKind.READONLY, "184 GB free"),
                    SettingRow("rom-scan", "Game library", "One library source could not be reached during the last scan.", RowKind.READONLY, "Needs attention", level="error"),
                ],
            ),
            Section(
                "display",
                "Display & Audio",
                "Cabinet-facing output settings with the active value always visible.",
                [
                    SettingRow("resolution", "Display mode", "Resolution selected by Batocera for the current output.", RowKind.READONLY, "1920 × 1080 · 60 Hz"),
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
                    SettingRow("network-status", "Network", "Current local connection and address.", RowKind.READONLY, "Ethernet · 192.168.1.87", level="good"),
                    SettingRow(
                        "web-access",
                        "Web access",
                        "Run the optional Control Freak web interface on this machine. Core settings remain available here when it is off.",
                        RowKind.TOGGLE,
                        False,
                        confirm="Enable authenticated web access on the local network? You can turn it off here at any time.",
                    ),
                    SettingRow("web-address", "Web address", "Shown after web access starts successfully.", RowKind.READONLY, "Not running", available=False, reason="Enable Web access first."),
                    SettingRow("tailscale", "Tailscale access", "Allow the web interface on the existing tailnet without making it public.", RowKind.TOGGLE, False, available=False, reason="Web access must be enabled first."),
                ],
            ),
            Section(
                "updates",
                "Updates",
                "Release channel, installed version, and a safe path back.",
                [
                    SettingRow("installed-version", "Installed version", "The immutable application release currently active.", RowKind.READONLY, "0.1.0-dev"),
                    SettingRow("release-channel", "Release channel", "Stable receives tested releases. Nightly receives the newest build and may contain incomplete behavior.", RowKind.CHOICE, "Stable", ("Stable", "Nightly")),
                    SettingRow("check-updates", "Check for updates", "Look for a release without installing it.", RowKind.ACTION, "Check now"),
                    SettingRow("rollback", "Roll back", "Restore the previously installed verified release.", RowKind.ACTION, "Unavailable", available=False, reason="No previous release is stored on this machine."),
                ],
            ),
            Section(
                "system",
                "System",
                "Diagnostics and carefully bounded machine actions.",
                [
                    SettingRow("diagnostics", "Export diagnostics", "Create a support archive that excludes credentials and game content.", RowKind.ACTION, "Create archive"),
                    SettingRow("restart-emulationstation", "Restart EmulationStation", "Close and reopen the Batocera front end. Running games are never interrupted by this action.", RowKind.ACTION, "Restart", confirm="Restart EmulationStation now? Control Freak will close while the front end restarts."),
                    SettingRow("reboot", "Reboot machine", "Restart Batocera after a final confirmation.", RowKind.ACTION, "Reboot", confirm="Reboot this machine now?"),
                ],
            ),
        ]
    )
