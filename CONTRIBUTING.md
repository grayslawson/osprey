# Contributing to Osprey

Osprey controls UniFi PTZ cameras for a Frigate that someone else runs. Keep
changes inside that boundary: Osprey never installs or configures Frigate.
[`AGENTS.md`](AGENTS.md) has the full change protocol.

## Development setup

Python 3.13 is required. The controller uses only the standard library. Test,
lint and build tools are pinned in `requirements-ci.txt`, and the wire library
is installed from the in-tree `pyunifiwire/`.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-ci.txt -e pyunifiwire
make check
```

The default checks need no camera, Frigate, MQTT broker or internet
connection. Run the [harnesses](osprey/harness/README.md) only against a test
device on a network you control.

## Pull requests

Describe the user-visible outcome, the interfaces you touched, the tests you
ran and any remaining risk. A configuration change needs a neutral example and
an upgrade or rollback note. Never attach credentials, private addresses, MAC
addresses, recordings or unredacted logs.

Before you ask for review, run `make check`, read your diff, and update
`docs/` for any change users will notice. If you touched Docker, Home
Assistant, NixOS or package metadata, check that path too. Use Conventional
Commit subjects; the changelog is generated from them.

Security reports should follow [`SECURITY.md`](SECURITY.md), not a public issue.

## Releasing

Releases are tag-driven. The tag must be `vMAJOR.MINOR.PATCH`, or a PEP 440
pre-release such as `v0.9.0b1`, equal to the `VERSION` file, and point at a
commit on `main`. Pre-release tags publish a GitHub pre-release, PyPI
pre-release and image tags `0.9.0b1`/`v0.9.0b1`, without moving `latest`.

1. Set `VERSION`, and the same version in
   `homeassistant/addon/osprey/config.yaml` (also its image tag),
   `packaging/homebrew/osprey.rb`, `packaging/npm/package.json`,
   `packaging/chocolatey/osprey.nuspec`, `pyunifiwire/pyproject.toml` and
   `pyunifiwire/src/unifiwire/__init__.py`. `make validate` fails if they
   differ.
2. Prepend the changelog entry: `git-cliff --tag vX.Y.Z --unreleased --prepend CHANGELOG.md`
   (git-cliff is pinned in `requirements-ci.txt`).
3. Run `make check`, merge to `main`, then tag the merge commit `vX.Y.Z` and push the tag.

`.github/workflows/release.yml` then runs the tests, builds the sdist and
wheel, builds and signs the multi-architecture image (keyless cosign, with SBOM
and provenance), publishes `osprey-ptz` to PyPI and `@osprey-ptz/cli` to npm
through trusted publishing, creates the GitHub release with the changelog
notes and package assets, and builds the Home Assistant app image. The
Homebrew tap, the Chocolatey feed and the Home Assistant app repository pick up
the release from there.

Verify an image signature with:

```sh
cosign verify ghcr.io/grayslawson/osprey@sha256:DIGEST \
  --certificate-identity-regexp 'https://github.com/grayslawson/osprey/\.github/workflows/release\.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```
