# Contributing

Forgejo is the authoritative repository. Open changes there; GitHub is a public mirror and release endpoint.

## Development

1. Create a focused branch from Forgejo `main`.
2. Add one failing test for each behavior before production code.
3. Keep the launcher exactly two lines and do not place a `.pygame` file in the services directory.
4. Run `python -m pytest -q`, Pyright, and `python -m compileall -q src deploy scripts tests`.
5. Run `git diff --check`; do not regenerate release signatures during ordinary feature work.

Never commit credentials, private signing keys, appliance state, ROM data, or generated release signatures. Security-boundary changes need tests for malformed types, traversal/symlinks, byte limits, interrupted lifecycle transitions, and exact service-state restoration.
