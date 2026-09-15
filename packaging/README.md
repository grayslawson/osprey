# Package integrations

Osprey is distributed as an OCI image. These integrations provide lightweight
launchers and package-manager discovery; they do not contain a native Osprey
daemon and require Docker or Podman. The launcher exposes the read-only
`doctor` diagnostic and points operators to `compose.release.yaml` for the
controller, as described in the installation guide.

The npm package is published as `@osprey-ptz/cli`. The Homebrew formula and
Chocolatey package are release assets maintained by the project and should be
updated to the matching release tag. Verify the release archive checksum when
publishing a formula to a tap.
