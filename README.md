# Control Freak

Control Freak is a controller-first local settings and management application for Batocera. The primary interface is a full-screen Pygame application launched from EmulationStation; the optional authenticated web interface is disabled by default.

Control Freak is an independent community project. It is not affiliated with, endorsed by, or supported by Batocera.linux or GitHub.

## Current functionality

- controller navigation using Batocera's EmulationStation mappings (South confirms, East goes back; Start/Select do nothing)
- authenticated local Web app and optional existing-tailnet access
- persistent audio-output blacklist enforcement without changing physical audio configuration
- Batocera service enablement and lifecycle management
- anonymous signed updates from public GitHub Releases, authenticated offline release notes, and one verified rollback target

## Compatibility

The current release format requires Python 3.12 through 3.14, OpenSSL, `qrcode`, `pygame-ce`, Linux `memfd` sealing, and Batocera's `batocera-services`/`start-stop-daemon` environment. Each signed release metadata file lists the exact supported two-digit Batocera versions; the updater refuses an incompatible release. Hardware compatibility still depends on Batocera's SDL, display, PulseAudio, and controller support.

## Batocera installation

The Web app uses a persistent device-local self-signed HTTPS certificate. Browsers will warn until that certificate is explicitly trusted; HTTPS encrypts pairing and session credentials, but accepting an unverified certificate does not authenticate the device against an active first-use network attacker.

The manifest-driven installer registers Control Freak in Batocera's built-in `pygame` EmulationStation system when the shared filesystem supports atomic path exchange. Native registration places only a minimal service dispatcher at `/userdata/roms/pygame/control-freak-launcher/control-freak.pygame` so EmulationStation's scanner can discover it. Otherwise the installer uses a dedicated root-owned local EmulationStation system and leaves both the shared Pygame gamelist and ROM launcher tree untouched. Immutable releases live below root-owned `/userdata/system/control-freak/app/releases`; an atomic `current.json` selects the active release.

```bash
python3 deploy/install_batocera.py --source . --release-id 0.1.8 --channel stable
```

The scanner-visible launcher invokes `/userdata/system/services/control_freak launch` and contains no application code or trust metadata. The application, launcher, Control Freak library metadata, registration strategy, and root-owned service wrapper must pass strict validation. The platform-managed native Pygame gamelist may be platform-owned and group-writable, but must be a non-symlinked regular file that is not world-writable. Cryptographic verification starts in the service wrapper, which validates the selected registration strategy and signed runtime before executing it. The installer migrates the former `/userdata/roms/pygame/control-freak` and dedicated `controlfreak` layouts while preserving unrelated Pygame games and mutable play metadata.

## Updates

The Updates screen supports Stable and Nightly channels. Manual checks always perform fresh discovery and require confirmation before installation. Daily checks only mark availability. Installed release notes are cached with their detached signature and remain available offline. Rollback is shown only when the installer can completely verify a retained predecessor.

Public discovery is credential-free at `plasmadice/control-freak-batocera` GitHub Releases. Forgejo remains the authoritative source repository; GitHub is a public mirror/publication endpoint. GitHub API fields and GitHub-rendered notes are never security authority—the pinned project key authenticates canonical metadata that binds the artifact, compatibility, notes, and runtime trust statement.

See [RELEASING.md](RELEASING.md) for deterministic build/sign/publish steps and [SECURITY.md](SECURITY.md) for the trust model.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/control-freak --windowed
```

## Controls

- D-pad / arrow keys: move
- South / Enter / Space: confirm
- East / Escape / Backspace: back
- Start and Select: no action

## License

GPL-3.0-or-later.
