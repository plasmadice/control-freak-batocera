# Releasing

Forgejo remains source of truth. GitHub Releases at `plasmadice/control-freak-batocera` are the credential-free public update origin.

## 1. Freeze and verify

Run the full tests, Pyright, compileall, and diff checks. Regenerate the existing runtime trust statement/signature only after the release tree is frozen, using the BWS-controlled signing key outside GitHub.

## 2. Build deterministic inputs

```bash
python3 scripts/build_release.py \
  --source . --output dist/release \
  --release-id 1.2.3 --channel stable \
  --batocera 43 44 --sequence 1 --notes-file release-notes.txt
```

This creates deterministic `control-freak-release.zip` and canonical `control-freak-release.json`. Repeating the command from identical bytes produces identical outputs.

## 3. Sign externally

Use the BWS-controlled private key outside this repository and CI to create the detached signature:

```bash
openssl dgst -sha256 -sign /secure/path/release-private.pem \
  -out dist/release/control-freak-release.sig \
  dist/release/control-freak-release.json
openssl dgst -sha256 -verify deploy/release-public.pem \
  -signature dist/release/control-freak-release.sig \
  dist/release/control-freak-release.json
```

Do not upload or configure the private key in GitHub or GitHub Actions. CI may build unsigned deterministic inputs and accept externally supplied already-signed metadata/signature as publication artifacts.

## 4. Publish the three exact assets

After the tag has mirrored from Forgejo, an authenticated maintainer may run:

```bash
python3 scripts/publish_github_release.py 1.2.3 dist/release
# add --nightly for a prerelease/nightly
```

The publisher requires an existing verified tag and uploads only the canonical metadata, detached signature, and artifact. GitHub's displayed notes are deliberately non-authoritative.
