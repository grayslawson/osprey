# Contributing to Osprey

Osprey is an integration layer for supported Ubiquiti PTZ cameras and an
existing Frigate installation. Contributions should keep that boundary clear.

## Development setup

Python 3.13 is required. The controller uses the standard library. The test,
lint, and build tools are pinned in `requirements-ci.txt`. The wire library is tested from
the sibling `pyunifiwire/` directory.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-ci.txt -e pyunifiwire
make check
```

No camera, Frigate instance, MQTT broker, or internet connection is needed for
the default checks. Use the harness scripts only when you explicitly have a
controlled test device and network.

## Pull requests

Include the user-visible outcome, affected interfaces, tests run, and any
residual risk. Configuration changes must include a neutral example and an
upgrade or rollback note. Never attach credentials, private addresses, MAC
addresses, recordings, or unredacted logs.

Before requesting review:

- run `make check`;
- review `git diff --check` and the list of changed files;
- update `docs/`, `README.md`, or release notes for user-facing behavior;
- verify Docker/Podman, Home Assistant, NixOS, and package metadata when those
  paths are affected.

Security reports should follow [`SECURITY.md`](SECURITY.md), not a public issue.

## Releasing

Releases are tag-driven. The tag must be `vMAJOR.MINOR.PATCH`, equal to the
`VERSION` file, and point at a commit on `main`.

1. Set `VERSION`, and the matching `version:` in `homeassistant/addon/osprey/config.yaml`,
   `packaging/homebrew/osprey.rb`, `pyunifiwire/pyproject.toml` and
   `pyunifiwire/src/unifiwire/__init__.py`. `make validate` fails if they differ.
2. Prepend the changelog entry: `git-cliff --tag vX.Y.Z --unreleased --prepend CHANGELOG.md`
   (git-cliff is pinned in `requirements-ci.txt`).
3. Run `make check`, merge to `main`, then tag the merge commit `vX.Y.Z` and push the tag.

`.github/workflows/release.yml` then runs the test suite, builds the sdist and
wheel, builds and signs the multi-architecture image (keyless cosign, with SBOM
and provenance), publishes `osprey-ptz` to PyPI through trusted publishing
(GitHub environment `pypi`), creates the GitHub Release with the changelog notes,
and builds the Home Assistant add-on image.

Consumers can verify the image signature with:

```sh
cosign verify ghcr.io/grayslawson/osprey@sha256:DIGEST \
  --certificate-identity-regexp 'https://github.com/grayslawson/osprey/\.github/workflows/release\.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```
