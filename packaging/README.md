# Packaging

Distribution sources that the main image build does not cover. User-facing
install steps are in [`docs/installation.md`](../docs/installation.md).

| Path | What it builds | Published to |
|---|---|---|
| `pypi/` | The `osprey` entry point of the `osprey-ptz` wheel built from the root `pyproject.toml`. A transitional launcher until `specs/002-python-package-cli` lands. | PyPI |
| `npm/` | `@osprey-ptz/cli`, a launcher that runs `doctor`, `probe`, `protect-guard` and `version` from the release image. | npm, by the release workflow |
| `homebrew/osprey.rb` | A formula that installs the `osprey doctor` launcher. | The `grayslawson/homebrew-osprey` tap, via `scripts/sync-homebrew-tap.sh` |
| `chocolatey/` | A package that copies `compose.release.yaml` to `%ProgramData%\osprey` and starts nothing. | A Chocolatey feed, via `scripts/sync-chocolatey-feed.sh` |
| `homeassistant-repo/` | The root files of the Home Assistant app repository. | `grayslawson/osprey-ha-apps`; see its [README](homeassistant-repo/README.md) |
| `proxmox/` | A community-scripts style LXC pair. | Not yet; see its [README](proxmox/README.md) |

None of the launchers start the controller. Deploy it from the image with
`compose.release.yaml`.

The release version lives in `VERSION`. `scripts/validate-repo.sh` fails if
the Home Assistant manifest, the Homebrew formula, the npm manifest, the
Chocolatey manifest or pyunifiwire disagree with it. Chocolatey needs a
SemVer pre-release, so the feed script turns `0.9.0b1` into `0.9.0-beta1`.
