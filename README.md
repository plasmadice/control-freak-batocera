# Control Freak

A controller-first settings and management application for Batocera.

Control Freak is being rebuilt as a local Batocera application rather than a remote fleet dashboard. The primary interface is a full-screen Pygame application launched from EmulationStation. An optional authenticated web interface will use the same local service and remain disabled by default.

> [!IMPORTANT]
> This repository currently contains a fixture-backed interaction prototype. It does **not** mutate Batocera settings yet.

## Prototype scope

- 1280×720 logical canvas that scales to the active display
- controller/keyboard semantic navigation
- settings index and contextual help
- read-only, toggle, choice, action, unavailable, error, and confirmation states
- fixture screens for overview, display/audio, network/web, updates, rollback, diagnostics, and system actions
- deterministic screenshot rendering for visual review

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/control-freak --windowed
```

Render the review fixture without opening a window:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \
  .venv/bin/control-freak --screenshot artifacts/control-freak-network.png
```

## Current controls

- D-pad / arrow keys: move
- `A` / Enter / Space: select
- `B` / Escape / Backspace: back

The shipping implementation will translate Batocera's existing EmulationStation mappings. It will not add a controller-remapping workflow.

## Deliberately absent

No fleet hub, cross-machine transfer queue, controller mapper, public-internet exposure, plugin system, theming system, or live Batocera mutation layer exists in this first spike.

## License

GPL-3.0-or-later. The product name is provisional pending public-release name clearance.
