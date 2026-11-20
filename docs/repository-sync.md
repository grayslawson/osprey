# Repository ownership and mirror policy

Osprey uses one source of truth and one public distribution surface:

| Surface | Role | Changes accepted from |
| --- | --- | --- |
| GitHub `grayslawson/osprey` | Canonical development repository, review, and public issue intake | Maintainers and approved contributors |
| Forgejo `grays/osprey` | Private development mirror and internal branch visibility | GitHub mirror only, for code and tags |
| GHCR `ghcr.io/grayslawson/osprey` | Public OCI images and release distribution | GitHub release workflow |
| PyPI `osprey-ptz` | Python distribution of the controller | GitHub release workflow (trusted publishing) |

Git mirroring does not copy issues, pull requests, releases, secrets, branch
rules, Actions history, or package metadata. Decide which system owns each of
those separately. For Osprey, GitHub owns development review and the public
issue surface; Forgejo is the private mirror and internal branch view; GHCR and
PyPI are the distribution surfaces.

This policy was reversed on 2026-11-20. It previously named Forgejo as
canonical with GitHub as a read-only mirror. The Forgejo tree had stopped
receiving the product work — it still carried the pre-rename `cuckoo/`
package layout and lacked the probe, protect-guard, arbitration, and operator
web UI work — while GitHub held the full release pipeline and the commit the
live deployment actually runs. The two histories share an identical root tree
but have no common ancestor, so they are not interchangeable. GitHub is now
canonical and Forgejo mirrors from it.

## Configure the GitHub → Forgejo mirror

Configure this once in the Forgejo repository settings. The official [Forgejo
repository mirror guide](https://forgejo.org/docs/latest/user/repo-mirror/)
documents the current UI and API behavior.

1. Retain `grays/osprey` on Forgejo as the mirror target.
2. Create a GitHub fine-grained token restricted to this repository with only
   **Contents: read** permission.
3. In Forgejo, open **Repository settings → Mirrors → Add Pull Mirror**.
4. Use `https://github.com/grayslawson/osprey.git` as the source. Supply the
   token through the mirror authentication fields; never put it in a URL,
   workflow file, or checked-in config.
5. Add a branch/ref filter that pulls only the public `main` and release tags.
   Do not leave the filter empty: a full pull can overwrite mirror-local
   branches.
6. Synchronize once, then verify the Forgejo `main` commit and a test tag.
   Treat a mirror failure as a release-blocking alert.

The mirror is one-way in effect: the source of truth is GitHub, and the
Forgejo copy is read-only for everyone who works on the project. Developers
push to GitHub only. If a public contribution is accepted, review and merge it
on GitHub and let the mirror publish the result to Forgejo.

The historical `master` branch on Forgejo and the
`archive/pre-osprey-main-2026-09-15` branch on GitHub are frozen snapshots.
They are not updated by the mirror and must not be used as a base for new work.

## CI and releases

- GitHub runs the canonical tests, container builds, and release pipeline from
  `.github/workflows/`.
- Forgejo runs an equivalent test workflow from `.forgejo/workflows/` on its
  own runner, to catch failures before they reach the public surface.
- Only GitHub publishes GHCR images, PyPI distributions, GitHub release assets,
  and the Home Assistant app image. Forgejo must not publish a second artifact
  for the same tag.
- Keep the two workflows behaviorally aligned, but use platform-native action
  URLs and secrets for each host.

## If GitHub is removed later

Forgejo can replace the public surface entirely: move image publication to its
[package registry](https://forgejo.org/docs/latest/user/packages/), use
`.forgejo/workflows/` for builds, replace Dependabot with Renovate, and update
all installation and Home Assistant URLs. That is a deliberate migration, not
an implicit consequence of the mirror.
