# Product

<!-- impeccable:product-schema 1 -->

## Platform

Batocera Linux appliance: full-screen Pygame application plus an optional web client backed by the same local service.

## Stack

Python and Pygame on Batocera. An always-on local service owns observation and mutation. The web interface is optional and disabled by default.

## Users

People who run a Batocera arcade cabinet or console and need to inspect or change machine settings from the couch with an existing controller or keyboard.

## Product Purpose

Control Freak makes routine Batocera configuration and maintenance usable from EmulationStation instead of requiring SSH or a desktop. Success means a user can understand current state, make a bounded change, recover from failure, and return cleanly to EmulationStation.

## Positioning

A settings-first Batocera application that runs locally on each machine. It is not a fleet dashboard, package ecosystem, controller mapper, or remote infrastructure control plane.

## Operating Context

- Launched from Batocera's EmulationStation menu.
- Operated from several feet away with a gamepad/controller or keyboard.
- Must remain useful offline and without the web interface.
- Uses Batocera's existing EmulationStation controller mappings.
- Installs below `/userdata`, preserves user state across updates, and returns cleanly to EmulationStation.
- Stable is the default release channel; nightly is an explicit opt-in.

## Capabilities and Constraints

- Settings screens are the primary product surface.
- A local overview, persisted settings, diagnostics, updates, rollback, web-access settings, and confirmed system actions form the initial product boundary.
- The always-on local service is the sole writer. Native and web clients do not edit Batocera files independently.
- Web access is disabled by default. Enabling it requires authentication and an explicit LAN or optional Tailscale exposure choice. Control Freak does not provide public-internet exposure.
- Existing Batocera controller mappings are consumed; controller remapping is out of scope.
- Fleet management, hub enrollment, cross-machine transfers, and homelab infrastructure controls are out of scope.
- GPL-3.0-or-later permits selective reuse from RomM-o-Cera with attribution and notices.
- Initial compatibility validation targets the existing x86_64 Batocera 43 and 44 appliances. The public support promise remains open until the hardware spike passes.

## Brand Commitments

- Product name: **Control Freak**, provisional until public-release name clearance.
- Repository slug: `control-freak-batocera`.
- RomM-o-Cera is the product-shape reference for installation, EmulationStation integration, input translation, logical-canvas scaling, and release channels—not the visual finish to copy.
- The interface should be precise and restrained rather than nostalgic, neon, handheld-like, or dashboard-heavy.

## Evidence on Hand

- RomM-o-Cera source and its installed nightly build on `bato` have been inspected.
- Batocera 43 and 44 on the target x86_64 appliances include Python, Pygame, SDL, and the required display stacks.
- The retired Arcade Control source remains archived as a reference for validated Batocera operations, not as the new architecture.
- No public compatibility claims, benchmark claims, testimonials, or final brand assets exist yet.

## Product Principles

1. Local operation before remote convenience.
2. One clear settings path instead of duplicated controls.
3. Visible focus and couch-readable information at all times.
4. Safe changes: validate, preview consequences, confirm when necessary, and preserve rollback.
5. Delete topology-specific complexity until real users require it.

## Accessibility & Inclusion

- Controller and keyboard navigation must reach every operation.
- Focus must always be visible.
- Important meaning cannot depend on color alone.
- Text must wrap by whole words and remain readable at television distance.
- High-contrast behavior is required even if theme customization is otherwise deferred.
