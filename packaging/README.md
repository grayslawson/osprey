# Package integrations

Osprey ships as an OCI image, a Python distribution (`osprey-ptz`), a NixOS
flake, Proxmox LXC scripts, and a Home Assistant add-on. The directories here
hold the integrations that are not built by the main release workflow.

| Path | What it is | Status |
|---|---|---|
| `homebrew/osprey.rb` | Formula that installs the local `scripts/osprey` doctor launcher. It does not start a controller. | Copy into a tap; add the release archive checksum when publishing. |
| `proxmox/` | Community-scripts style LXC pair (`ct/` and `install/`). | Pending a real Proxmox run. |
| `homeassistant-repo/` | Scaffold for a separate Home Assistant app repository. | See its README. |
| `pypi/` | Launcher package that the root `pyproject.toml` wheel uses as its `osprey` entry point. | Transitional until Phase 0 (`specs/002-python-package-cli`). |

Removed: the npm (`@osprey-ptz/cli`) and Chocolatey wrappers. They only started
the container image for `osprey doctor` and added version upkeep without
adding capability.

The release version lives in the root `VERSION` file. `scripts/validate-repo.sh`
fails when the Home Assistant manifest, the Homebrew formula, or pyunifiwire
disagree with it.
