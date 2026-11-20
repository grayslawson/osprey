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

This is configured on the Forgejo instance: `grays/osprey` is a **pull
mirror** of `https://github.com/grayslawson/osprey.git`, synchronised
hourly, and Forgejo refuses pushes to a mirror, so the copy is read-only
for everyone who works on the project. The official [Forgejo repository
mirror guide](https://forgejo.org/docs/latest/user/repo-mirror/) documents
the current UI and API behaviour.

The mirror was created on 2026-11-20 with the Forgejo migration API:

```sh
curl -X POST https://git.possumden.net/api/v1/repos/migrate \
  -H "Authorization: token $FORGEJO_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"clone_addr":"https://github.com/grayslawson/osprey.git",
       "service":"git","mirror":true,"mirror_interval":"1h",
       "private":true,"repo_owner":"grays","repo_name":"osprey"}'
```

The source repository is public, so the mirror needs no credentials. A
private source would need a GitHub fine-grained token restricted to that
repository with only **Contents: read** permission, supplied through the
mirror authentication fields — never in a URL, a workflow file, or a
checked-in config.

Synchronise on demand with:

```sh
curl -X POST https://git.possumden.net/api/v1/repos/grays/osprey/mirror-sync \
  -H "Authorization: token $FORGEJO_TOKEN" -d '{}'
```

A mirror failure is a release-blocking alert.

### The frozen Forgejo tree

The Forgejo repository that preceded the mirror carried the pre-rename
`cuckoo/` package layout. It is kept as `grays/osprey-legacy`, private and
frozen, so that history is not lost when the name `grays/osprey` was taken
by the mirror. It is not updated by the mirror and must not be used as a
base for new work.

The historical `master` branch on Forgejo and the
`archive/pre-osprey-main-2026-09-15` branch on GitHub are frozen snapshots
for the same reason. The mirror carries every GitHub branch, including the
`dependabot/` ones and that archive branch, so a filter is not needed to
keep them out of the copy.

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
