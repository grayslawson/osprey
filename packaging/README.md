# Package integrations

Osprey is distributed as an OCI image. These integrations provide lightweight
launchers and package-manager discovery; they do not contain a native Osprey
daemon and require Docker or Podman. The launcher exposes the read-only
`doctor` diagnostic and points operators to `compose.release.yaml` for the
controller, as described in the installation guide.

The npm package is named `@osprey-ptz/cli`. The Homebrew formula and Chocolatey
manifest are maintained with the source release and can be copied into the
registry or tap used by your organization. Update all three to the matching
release tag before publishing; verify the release archive checksum when
publishing a formula to a tap.
