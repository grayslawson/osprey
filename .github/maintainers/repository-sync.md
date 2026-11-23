# Repository and mirrors

| Surface | Role | Changes come from |
| --- | --- | --- |
| GitHub `grayslawson/osprey` | Canonical repository, review and public issues | Maintainers and contributors |
| Forgejo `<owner>/osprey` | Private, read-only pull mirror | The GitHub repository, hourly |
| GHCR `ghcr.io/grayslawson/osprey` | Container images | The GitHub release workflow |
| PyPI `osprey-ptz`, npm `@osprey-ptz/cli` | Packages | The GitHub release workflow, through trusted publishing |
| `grayslawson/osprey-ha-apps` | Home Assistant app repository | A scheduled sync; see [`packaging/homeassistant-repo`](../../packaging/homeassistant-repo/README.md) |

A Git mirror copies code and tags only. Issues, pull requests, releases,
secrets, branch rules and Actions history stay on GitHub.

## The Forgejo mirror

The Forgejo repository is a pull mirror of
`https://github.com/grayslawson/osprey.git`, synchronised every hour. Forgejo
refuses pushes to a mirror, so it is read-only for everyone. The
[Forgejo mirror guide](https://forgejo.org/docs/latest/user/repo-mirror/)
covers the settings. It was created with the migration API:

```sh
curl -X POST https://forgejo.example.com/api/v1/repos/migrate \
  -H "Authorization: token $FORGEJO_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"clone_addr":"https://github.com/grayslawson/osprey.git",
       "service":"git","mirror":true,"mirror_interval":"1h",
       "private":true,"repo_owner":"<owner>","repo_name":"osprey"}'
```

To sync on demand:

```sh
curl -X POST https://forgejo.example.com/api/v1/repos/<owner>/osprey/mirror-sync \
  -H "Authorization: token $FORGEJO_TOKEN" -d '{}'
```

The source is public, so the mirror needs no credentials. A private source
would need a fine-grained GitHub token with only **Contents: read** on that
repository, entered in the mirror's authentication fields and never in a URL
or a checked-in file. A mirror failure blocks a release.

The Forgejo tree from before the `cuckoo/` to `osprey/` rename is kept, frozen
and private, as `<owner>/osprey-legacy`. The `archive/pre-osprey-main-2026-09-15`
branch is a frozen snapshot too. Neither is a base for new work.

## CI and releases

- GitHub runs the canonical tests, image builds and releases from
  `.github/workflows/`.
- Forgejo runs an equivalent test workflow from `.forgejo/workflows/` on its
  own runner. It never publishes artifacts.
- Keep the two test workflows aligned, using each platform's own actions and
  secrets.

Moving the public surface to Forgejo would be a deliberate migration: its
package registry for images, `.forgejo/workflows/` for builds, Renovate in
place of Dependabot, and new installation and Home Assistant URLs.
