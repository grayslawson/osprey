# Repository ownership and mirror policy

Osprey uses one source of truth and one public distribution surface:

| Surface | Role | Changes accepted from |
| --- | --- | --- |
| Forgejo `grays/osprey` | Canonical development repository, internal branches, and review | Maintainers and approved contributors |
| GitHub `grayslawson/osprey` | Public read-only source mirror, discovery, and public issue intake | Forgejo mirror only for code and tags |
| GHCR `ghcr.io/grayslawson/osprey` | Public OCI images and release distribution | GitHub release workflow |

Git mirroring does not copy issues, pull requests, releases, secrets, branch
rules, Actions history, or package metadata. Decide which system owns each of
those separately. For Osprey, Forgejo owns development review; GitHub is the
public issue and discovery surface; GHCR is the container distribution surface.

## Configure the Forgejo → GitHub mirror

Configure this once in the Forgejo repository settings. The official [Forgejo
repository mirror guide](https://forgejo.org/docs/latest/user/repo-mirror/)
documents the current UI and API behavior.

1. Create or retain `grayslawson/osprey` on GitHub with the same initial history.
2. Create a GitHub fine-grained token restricted to this repository with only
   **Contents: read and write** and **Workflows: read and write** permissions.
3. In Forgejo, open **Repository settings → Mirrors → Add Push Mirror**.
4. Use `https://github.com/grayslawson/osprey.git` as the destination. Supply
   the token through the mirror authentication fields; never put it in a URL,
   workflow file, or checked-in config.
5. Add a branch/ref filter that publishes only the sanitized public `main` and
   release tags. Do not leave the filter empty: Forgejo otherwise uses a
   force-pushing `git push --mirror` behavior.
6. Synchronize once, then verify the GitHub `main` commit and a test tag. Treat
   a mirror failure as a release-blocking alert.

The mirror must be one-way. Developers should push to Forgejo only; GitHub
direct pushes and GitHub pull requests must not become alternate code history.
If a public contribution is accepted, import it into Forgejo, review and merge
it there, and let the mirror publish the result.

## CI and releases

- Forgejo runs canonical tests from `.forgejo/workflows/` using its own runner.
- GitHub runs the public validation and release workflows from
  `.github/workflows/` after the mirror updates `main` or a release tag.
- Only GitHub publishes GHCR images and GitHub release assets. Forgejo must not
  publish a second image for the same tag.
- Keep the two workflows behaviorally aligned, but use platform-native action
  URLs and secrets for each host.

## If GitHub is removed later

Forgejo can replace the public surface entirely: move image publication to its
[package registry](https://forgejo.org/docs/latest/user/packages/), use
`.forgejo/workflows/` for builds, replace Dependabot with Renovate, and update
all installation and Home Assistant URLs. That is a deliberate migration, not
an implicit consequence of enabling the mirror.
