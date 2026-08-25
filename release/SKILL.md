---
name: release
description: Cut and publish a release of a Go library — verify tree state, finalise CHANGELOG and VERSION, tag, push, prime the Go module proxy, and create a GitHub release. Tailored to the kfet/{pinoauth, pinexec, covgate, …} style of small stdlib-only Go libs.
---

## Inputs

Ask the user for the **target version** (e.g. `v0.2.2`) if not given. If
this is a follow-up to an aborted release with cached-bad proxy
entries, ask whether to add `retract` directives in `go.mod`.

## Preflight (abort on any failure)

1. **Working tree clean** — `git status --porcelain` empty. If not,
   ask the user to commit, stash, or revert before continuing.
2. **On `main`** — `git rev-parse --abbrev-ref HEAD` == `main` (or the
   project's release branch). Never tag from a feature branch.
3. **Up to date with remote** — `git fetch origin && git status -uno`.
   If `main` is behind `origin/main`, pull first.
4. **Build & tests green** — `make all` exits 0. This must include the
   coverage gate; do not skip it. If `make all` fails the release is
   off.
5. **Target version is new** — `git tag -l "<version>"` empty AND
   `curl -s -o /dev/null -w "%{http_code}" "https://proxy.golang.org/<module>/@v/<version>.info"`
   returns `404` (or `410`). If the proxy already has it, the tag was
   used before — pick a new version or use `retract`.

## Finalise release content

1. **`VERSION` file** — overwrite with the bare version (no leading `v`,
   trailing newline).
2. **`CHANGELOG.md`**:
   - Move everything under `## [Unreleased]` into a new
     `## [<version>] - YYYY-MM-DD` section (today's date, local time).
   - Leave `## [Unreleased]` as an empty header above it.
   - Do **not** rewrite or re-date existing released sections.
3. **Sanity-check docs** — `doc.go`, `README.md`, `AGENTS.md`. If the
   release includes API changes, confirm those files agree with the
   current code. If not, fix them and include in the release commit.

## Commit + tag + push

1. Commit:
   ```
   git add -A
   git commit -m "release <version>: <one-line summary>"
   ```
2. Annotated tag:
   ```
   git tag -a <version> -m "<version>"
   ```
3. Push main, **then** the tag (separate pushes — CI on `main` should
   go green before the tag triggers any release workflow):
   ```
   git push origin main
   git push origin <version>
   ```

**If the `main` push is rejected (another agent released concurrently):**
discard the **release**, keep the **work** — `git tag -d <version>`, unwind
the release commit, `git rebase origin/main`, then re-enter Preflight from
step 1 and re-derive version, CHANGELOG, tests and tag against the merged
tree. Never rebase a finalised release commit.

## Prime the Go module proxy

Fetch each artifact so `proxy.golang.org` and `sum.golang.org` cache the
new version immediately. This both validates the publish and makes
`go get` work without a wait:

```
MOD=github.com/<owner>/<repo>
VER=<version>
for path in "@v/${VER}.info" "@v/${VER}.mod" "@v/${VER}.zip" "@v/list" "@latest"; do
  curl -s -o /dev/null -w "$path  %{http_code}\n" "https://proxy.golang.org/${MOD}/${path}"
done
```

All five should be `200`. If `.info` is `404` after a minute, the tag
didn't actually reach GitHub — re-check `git ls-remote origin`.

`pkg.go.dev` indexing lags a few minutes behind the proxy; don't poll
it tightly. A 404 from `https://pkg.go.dev/<module>@<version>` right
after release is normal.

## GitHub release

Create a release with notes drawn from the new CHANGELOG section:

```
gh release create <version> \
  --title "<version>" \
  --notes-file <path-with-extracted-changelog-section>
```

If the user wants release notes generated from commit messages instead,
use `gh release create <version> --generate-notes` — but the CHANGELOG
is usually the better source for a curated lib.

## Handling botched tags (retract)

If a tag was already pushed and the Go proxy cached pre-fix content
under it, the proxy entry is **immutable**. Don't try to fight it. The
standard Go remedy is:

1. Cut a new patch version with the corrected code.
2. Add `retract` directives in `go.mod` for the bad versions:
   ```go
   // v0.2.0 and v0.2.1 were tagged then re-tagged at different commits
   // while the public release was being settled. Use v0.2.2 or later.
   retract (
       v0.2.0
       v0.2.1
   )
   ```
3. Call out the retract in the CHANGELOG and GitHub release notes.

Re-tagging the same version at a new commit is technically possible
(`git tag -d` + `git push :refs/tags/<ver>` + retag + push) but the
proxy will keep serving the old cached content forever for that
version. Only do it if no one has fetched it through the proxy yet
(`curl …@v/<ver>.info` returns 404).

## Sibling repos to crib from

If unsure about repo conventions, look at one of:

- `github.com/kfet/pinexec` — same release flow, several tags shipped
- `github.com/kfet/pinoauth` — has retract directives in `go.mod` for
  v0.2.0/v0.2.1; useful real-world example of botched-tag recovery
- `github.com/kfet/covgate` — single-purpose tool, simple layout

## Output

Report back:

- New tag pushed (with the commit hash it points at)
- Proxy status (all four artifacts cached, with HTTP codes)
- GitHub release URL
- Anything left for the user (`pkg.go.dev` indexing lag note,
  retract follow-ups, etc.)
