# Security Policy

## Reporting

Please report vulnerabilities privately to the repository maintainers through Forgejo's private security contact or security advisory mechanism. Do not include signing keys, credentials, exploit details, or affected-user data in a public issue.

## Trust model

Forgejo is the source of truth. Public GitHub Releases at `plasmadice/control-freak-batocera` are an anonymous distribution mirror, not a trust root. The updater accepts only canonical metadata authenticated by the pinned release key and verifies exact artifact size/hash before bounded extraction. GitHub API fields and release-page text are untrusted discovery hints.

The root-owned, non-symlinked, non-group/world-writable EmulationStation overlay executes `/userdata/system/services/control_freak launch`. Executable and release content lives below `/userdata/system/control-freak/app`; `/userdata/system/control-freak/library` contains only an inert marker, metadata, and artwork. The service wrapper verifies its own signed relationship, the managed registration files, and every runtime payload file before execution. Private signing material must never be stored in this repository, GitHub, release artifacts, or GitHub Actions secrets.

Updates use HTTPS with fixed repository/API paths, bounded GitHub-owned redirects, timeouts, response limits, detached signatures, safe ZIP extraction, and the existing journaled installer. Rollback verifies the complete retained predecessor and preserves service enablement/running state.

The optional Web app uses a persistent device-local self-signed certificate and HTTPS only. This protects pairing tokens and session cookies from passive network capture, but it does not provide browser-trusted identity or prevent active first-use interception unless the user independently verifies and trusts the certificate.

## Supported versions

Only releases whose signed metadata lists the running Batocera major version and Python `>=3.12,<3.15` (3.12 through 3.14) are supported. Development snapshots are unsupported.
