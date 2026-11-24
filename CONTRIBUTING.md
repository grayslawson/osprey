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
Commit subjects; the changelog and the release notes are generated from them.

Write the subject for a user who will read it in the release notes, not for
the reviewer. "Fix the setup wizard form posting the password in the URL" is
a release note. "Fix wizard bug" is not. Squash merges keep the pull request
title as the subject, so the title is what counts. The types `feat`, `fix`,
`perf`, `docs`, `build` and `packaging` appear in the notes. `chore`, `ci`,
`test`, `style`, `refactor` and dependency bumps do not. A fix that closes a
security problem uses the scope `security` and gets its own heading, and a
`!` after the type (`feat(onvif)!:`) puts the change under Breaking changes.

Security reports should follow [`SECURITY.md`](SECURITY.md), not a public issue.

## Releasing

Releases are tag-driven. The tag must be `vMAJOR.MINOR.PATCH`, or a PEP 440
pre-release such as `v0.9.0b1`, equal to the `VERSION` file, and point at a
commit on `main`. Pre-release tags publish a GitHub pre-release and image
tags `0.9.0b1`/`v0.9.0b1` (plus a PyPI pre-release once PyPI publishing is on),
without moving `latest`.

1. Set `VERSION`, and the same version in
   `homeassistant/addon/osprey/config.yaml` (also its image tag),
   `packaging/homebrew/osprey.rb`, `packaging/npm/package.json`,
   `packaging/chocolatey/osprey.nuspec`, `pyunifiwire/pyproject.toml` and
   `pyunifiwire/src/unifiwire/__init__.py`. `make validate` fails if they
   differ.
2. Preview the notes with `git-cliff --tag vX.Y.Z --unreleased`. If a line is
   unclear, fix the commit subject or its pull request title rather than the
   output, because the same text is what the release shows. Then write the
   entry into the changelog:
   `git-cliff --tag vX.Y.Z --unreleased --prepend CHANGELOG.md` (git-cliff is
   pinned in `requirements-ci.txt`). Do not edit the generated section by hand.
3. Run `make check`, merge to `main`, then tag the merge commit `vX.Y.Z` and push the tag.

`.github/workflows/release.yml` then runs the tests, builds the sdist and
wheel, builds and signs the multi-architecture image (keyless cosign, with SBOM
and provenance), publishes `osprey-ptz` to PyPI and `@osprey-ptz/cli` to npm
through trusted publishing when those are switched on (see below), creates
the GitHub release with package assets and the notes git-cliff generates for the tag (`CLIFF_NOTES=1 git-cliff --latest`,
the same text as the changelog section without its heading), and builds the
Home Assistant app image. The Homebrew tap, the Chocolatey feed and the Home Assistant app repository pick up
the release from there.

### Switching on PyPI and npm publishing

Neither registry is set up yet, so the `pypi` and `npm` release jobs are
skipped unless a repository variable turns them on. With both off, a release
still creates the GitHub Release, the images and the Home Assistant app.

1. PyPI: add a trusted publisher for the project `osprey-ptz` with owner
   `grayslawson`, repository `osprey`, workflow `release.yml` and environment
   `pypi`. The `pypi` environment already exists on GitHub.
2. npm: in the `@osprey-ptz/cli` package settings, add a trusted publisher
   for `grayslawson/osprey` with workflow `release.yml`. If npm only offers
   this for a package that already exists, publish the first version by hand.
3. Turn the jobs on:

   ```sh
   gh variable set PUBLISH_PYPI --body true --repo grayslawson/osprey
   gh variable set PUBLISH_NPM --body true --repo grayslawson/osprey
   ```

4. For a release that has already been tagged, publish it afterwards with
   `gh workflow run release.yml --ref vX.Y.Z`.

Delete a variable (`gh variable delete PUBLISH_NPM`), or set it to anything
other than `true`, to turn that job off again.

Verify an image signature with:

```sh
cosign verify ghcr.io/grayslawson/osprey@sha256:DIGEST \
  --certificate-identity-regexp 'https://github.com/grayslawson/osprey/\.github/workflows/release\.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```
