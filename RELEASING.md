# Releasing Ontos

Cutting a release is: raise a release PR that bumps the version + CHANGELOG, merge it, then push a git tag. The tag triggers `.github/workflows/release.yml`, which builds the PyPI wheel/sdist, builds and pushes the GHCR image, and creates a GitHub Release with the CHANGELOG excerpt.

## One-time setup (do this once per fresh repo)

### 1. PyPI trusted publishing

The release workflow uses **OIDC trusted publishing** — no API token in the repo secrets.

1. Create the `ontos` project on PyPI (upload any wheel manually the first time, or ask a maintainer with pending-project privilege).
2. On PyPI → **Manage** → **Publishing** → **Add a new publisher**:
   - Owner: `Enclave-Labs-Inc`
   - Repository: `Ontos`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`
3. In GitHub → **Settings** → **Environments** → create an environment named `pypi`. No secrets needed; the environment gates the PyPI publish job so a rogue push to a fork can't trigger it.

### 2. GHCR

No setup needed — `secrets.GITHUB_TOKEN` (auto-provided) has `packages:write` when the workflow declares it. First push to GHCR creates the package under `ghcr.io/enclave-labs-inc/ontos`; make it Public in the package settings if that's the intent.

### 3. Branch protection

Ensure `main` requires the `lint · type-check · test` check (from `ci.yml`) before merge. Nothing else in this workflow gates on branch protection.

## Cutting a release

1. **Prepare a release PR** on a branch named `release/vX.Y.Z`:
   - Bump `ontos/_version.py` `__version__` to `X.Y.Z`.
   - In `CHANGELOG.md`: move the `[Unreleased]` bullets under a new `## [X.Y.Z] - YYYY-MM-DD` heading; reset `[Unreleased]` to empty.
   - Bump `deploy/helm/ontos/Chart.yaml` `appVersion` and (usually) `version`.
   - Bump `deploy/helm/ontos/values.yaml` `image.tag`.
   - Bump `deploy/helm/README.md`'s sample install command if it references a specific tag.
   - Do NOT edit the version in `tests/unit/test_smoke.py` — that check is version-string-agnostic.
2. **Get review + merge**. CI must be green; branch protection enforces it.
3. **Tag main and push**:
   ```bash
   git checkout main
   git pull --ff-only
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
4. **Watch the workflow** at Actions → Release. The `build` job runs first and hard-fails if `ontos/_version.py` doesn't match the tag; then `publish-pypi`, `publish-ghcr`, and `gh-release` run in parallel from the built artifacts.
5. **Verify the outputs**:
   - PyPI: `pip install --upgrade enclave-ontos==X.Y.Z` from a clean venv. Import path stays `import ontos`.
   - GHCR: `docker pull ghcr.io/enclave-labs-inc/ontos:X.Y.Z`.
   - GitHub Releases page shows the new release with the CHANGELOG excerpt and the sdist + wheel attached.
6. **Post-release announcement** (optional): update whatever public docs, Slack channel, or Discord announcement channel needs it.

## Dry-run

To validate the workflow without actually publishing:

- Trigger `release.yml` manually via **Actions** → **Release** → **Run workflow**. Leave `dry_run` checked (it defaults to true).
- The `build` job runs; the publish + release jobs skip.

## Rolling back a bad release

**Do NOT delete the git tag or the GHCR image if the release has been out for more than a few minutes.** Consumers may have pulled it; deleting under them causes cache-poisoning-shaped confusion.

The right recovery is a **new patch release** that fixes the issue:

1. Cut `vX.Y.(Z+1)` per the normal process, with a CHANGELOG entry noting what the previous release broke and how the patch fixes it.
2. On PyPI, mark the broken version as **yanked** (Project → Manage → Releases → Yank). Yanking hides it from `pip install enclave-ontos` (unless the user pins the exact version) without deleting the record.
3. On GHCR, do NOT delete the tag. Push `:latest` to the patched image so `docker pull ontos:latest` picks up the fix.
4. Announce the yank in the CHANGELOG entry for the new release and (if warranted) as a pinned GitHub issue.

If the release has been out for less than a minute and no one could plausibly have pulled it (verify by checking PyPI download stats and GHCR pull metrics), you can:
- Delete the git tag (`git push origin :vX.Y.Z`, then `git tag -d vX.Y.Z`).
- Yank the PyPI version.
- Delete the GHCR tag.
- Push a new tag with the fix.

Always prefer forward-fixing over rollback when consumers exist.

## Version bump conventions

Ontos follows SemVer:

- **PATCH** (`0.1.0 → 0.1.1`) — bug fixes, no MCP-surface or CLI-surface changes, no schema changes to `Fact` / `Provenance` / `AuditRecord`, no new required env vars.
- **MINOR** (`0.1.0 → 0.2.0`) — new MCP tools, new CLI subcommands, new optional env vars, new additive fields on the models. Existing behavior preserved.
- **MAJOR** (`0.x → 1.0`) — anything that breaks a caller: renamed / removed tools, changed argument signatures, removed model fields, removed env vars, changed authz or audit semantics.

While pre-1.0, minor bumps MAY include small breaking changes if they're worth it; call them out clearly in the CHANGELOG.

Reserved reasons for a MAJOR bump before 1.0:

- Changing the Article-12 field contract (any change to `ArticleTwelveField` enum values).
- Changing the `Fact` on-disk / on-wire shape.
- Changing the `Plan` schema in a way that invalidates persisted plans.

## What the release workflow does NOT sign

- **PyPI wheels are not signed.** PyPI's own package integrity (via file hashes on the index) is what consumers rely on; adding sigstore / GPG would be a follow-up.
- **GHCR images are not signed.** Cosign integration when a customer asks for it.
- **CHANGELOG excerpts are not cryptographically attested.** The audit chain inside the runtime is; the CHANGELOG is release-note polish.

If a regulated customer needs signed artifacts, open an issue — this is one of those things to do once a customer names it, not speculatively.
