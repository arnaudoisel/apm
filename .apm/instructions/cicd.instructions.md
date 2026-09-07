---
applyTo: ".github/workflows/**"
description: "CI/CD Pipeline configuration for PyInstaller binary packaging and release workflow"
---

# CI/CD Pipeline Instructions

## Workflow Architecture (Tiered + Merge Queue)
Core workflows split by trigger and tier. PRs get fast feedback; the heavy
integration suite runs only at merge time via GitHub Merge Queue
(microsoft/apm#770).

1. **`ci.yml`** - Tier 1, runs on `pull_request`, `merge_group`, and `workflow_call`
   - Linux unit shards and combined coverage, lint, architecture ratchets,
     self-check and lifecycle smoke; a bounded Windows compatibility lane.
     No production secrets needed.
   - PR-only binary smoke provides early packaging feedback. Merge queue
     builds its own binary; full release candidates call the source checks
     and build each native archive separately.
   - Runs in both PR context (fast feedback for contributors) and merge_group
     context (against the tentative merge commit before queue auto-merges).
2. **`ci-integration.yml`** - Tier 2, `merge_group` trigger only
   - **Linux-only**. Builds binary inline, then runs smoke + integration +
     release-validation against the tentative merge commit.
   - Trust boundary is the write-access grant (only users with write can
     enqueue a PR). No environment approval gate.
   - Inlines the binary build instead of fetching from `ci.yml` to avoid
     cross-workflow artifact plumbing across triggers.
   - **Never add a `pull_request` or `pull_request_target` trigger here.**
     This file holds production secrets (`GH_CLI_PAT`, `ADO_APM_PAT`).
     Required-check satisfaction at PR time is handled by `merge-gate.yml`,
     which aggregates all required signals into a single `gate` check.
3. **`merge-gate.yml`** - single-authority PR and merge-queue aggregator
   - Uses event-specific required checks. Never add a parallel
     `pull_request_target` gate, which can create duplicate check-run names.
   - One job named `gate`. Polls the Checks API for all entries in the
     workflow's `EXPECTED_CHECKS` env var; aggregates pass/fail into a
     single check-run.
   - Branch protection requires ONLY this one check (`gate`). Adding,
     renaming, or removing an underlying check is a `merge-gate.yml` edit,
     never a ruleset edit. Tide / bors single-authority pattern.
   - Required results must succeed; skipped or neutral is not passing
     evidence. Lint and Test Architecture Ratchets are required in both tiers.
   - `.github/CODEOWNERS` requires Lead Maintainer review for any change
     to `.github/workflows/**`.
4. **`build-release.yml`** - main push, tags, schedule, default-branch `repository_dispatch`
   - Calls **`release-platform.yml`** using the canonical platform catalog
     in `scripts/release-platforms.json`. Native unit tests and builds are
     independent. Integration, isolated validation and the Windows installer
     depend only on their own platform's build.
   - Ordinary main pushes retain four lanes, including macOS Intel.
     Full tag/schedule/manual qualification adds macOS ARM and source CI.
     Intel retains `lifecycle_smoke and not live`; ARM retains full non-live
     integration coverage. Neither platform drops native unit coverage.
   - Tags can promote an exact-SHA, fully qualified trusted candidate;
     partial main builds and PR artifacts are not release evidence.
     Missing matching candidates require a fresh full build.
   - Publication jobs do not receive `ADO_APM_PAT`; live ADO PAT acceptance is an explicit `ado_pat_e2e` mode in `auth-acceptance.yml`. Full 5-platform binary output (linux x86_64/arm64, darwin x86_64/arm64, windows x86_64).
5. **`ci-runtime.yml`** - nightly schedule, default-branch repository dispatch, path-filtered push
   - **Linux x86_64 only**. Live inference smoke tests (`apm run`) isolated from release pipeline.
   - Uses `GH_MODELS_PAT` for GitHub Models API access.
   - Failures do not block releases - annotated as warnings.

## Platform Testing Strategy
- **PR time**: Linux source checks and packaging smoke, plus a bounded
  Windows compatibility gate. Platform-specific failures remain possible.
- **Post-merge**: Four native unit/build lanes; full qualification includes
  all five architectures and every applicable integration/validation gate.
- **Publication**: Requires successful source and native qualification.
  Parallelizing these checks must never remove a required result.

## PyInstaller Binary Packaging
- **CRITICAL**: Uses `--onedir` mode (NOT `--onefile`) for faster CLI startup performance
- **Binary Structure**: Creates `dist/{binary_name}/apm` (nested directory containing executable + dependencies)
- **Platform Naming**: `apm-{platform}-{arch}` (e.g., `apm-darwin-arm64`, `apm-linux-x86_64`)
- **Spec File**: `build/apm.spec` handles data bundling and hidden imports.
  Do not provision UPX: the pinned PyInstaller disables it on non-Windows,
  while the spec disables it on Windows to avoid antivirus false positives.

## Artifact Flow Quirks
- **Package once**: `scripts/package_release.py` creates each final archive
  after signing and verifies the embedded version/build SHA.
- **Upload**: Each native artifact contains `release-assets/` (archive,
  checksum, metadata) and selected validation scripts.
- **Native consumers**: Verify hashes and identity, then extract the same
  archive into `dist/{binary_name}`. Do not test an installed older release.
- **Promotion**: Verify complete immutable run/artifact evidence and copy
  the ten public archive/checksum files without repackaging. The write-token
  publisher has no checkout and executes no candidate scripts.
- **Reruns**: Candidate artifacts are attempt-scoped and promotion downloads
  exact artifact IDs. Use **Re-run all jobs** to regenerate qualification;
  a partial rerun must not combine old archives with new test evidence.

## Critical Testing Phases
1. **Integration Tests**: Full source code access for comprehensive testing
2. **Release Validation**: ISOLATION testing - no source checkout, validates exact shipped binary experience
3. **Path Resolution**: Set `APM_BINARY_PATH` explicitly for pytest, so an
   editable install or ambient PATH cannot silently replace the candidate
4. **Installer Tests**: New installs and upgrade destinations use the
   current candidate; an older binary may only seed the upgrade source

## Inference Testing (Decoupled)
- Live inference tests (`apm run`) are **isolated** in `ci-runtime.yml` - they do NOT gate releases
- `APM_RUN_INFERENCE_TESTS=1` env var enables inference in test scripts; absent = skipped
- `GH_MODELS_PAT` is only used in `ci-runtime.yml` and Tier 2 smoke-test job - NOT in integration-tests or release-validation
- Rationale: 8 inference executions x 2% failure rate = 14.9% false-negative per release; APM core UVPs require zero live inference

## Release Flow Dependencies
- **PR workflow**: Tier 1 provides fast feedback; Tier 2 waits for enqueue.
- **Merge queue workflow**: Tier 1 plus Tier 2's own build. Integration and
  isolated validation no longer form a serial fan-in; the gate still
  requires all applicable results.
- **Fresh release**: Independent native units/build -> per-platform
  integration/validation/installer -> complete qualification -> verified
  assets -> publication. Source CI runs alongside the native lanes.
- **Qualified same-SHA release**: Trusted candidate lookup -> verification
  -> publication. Qualification cost occurred before tagging, not vanished.
- **Homebrew distribution**: `microsoft/homebrew-apm` polls the latest public APM release and updates its formula with its own `GITHUB_TOKEN`; this workflow must not send a cross-repository dispatch or hold a tap credential.
- **Live ADO PAT acceptance**: manual `auth-acceptance.yml` dispatch with `ado_pat_e2e: true` -> exact `live and requires_ado_pat` marker intersection. Invalid credentials fail with production auth diagnostics but do not block publication.
- **Tag Triggers**: `v*` tags enter release planning; promotion must verify
  that the tag and candidate version identify the same release
- **Artifact Retention**: 30 days for debugging failed releases
- **Cross-run artifacts**: Only the release candidate protocol may reuse
  release artifacts, bound to the exact trusted source run and commit.
  `ci-integration.yml` continues to build its own binary.

## Branch Protection & Required Checks
- **Single required check**: branch protection (`main-protection` ruleset id 9294522) requires exactly one status check context: `gate` from `merge-gate.yml`. All other PR-time signals are aggregated by that workflow's poll loop.
- **CRITICAL ruleset gotcha**: the ruleset `context` must be the literal check-run name `gate`. `Merge Gate / gate` is only how GitHub may render the workflow and job together in the UI; it is not the context value to store in the ruleset. If the ruleset stores `Merge Gate / gate`, GitHub waits forever with "Expected - Waiting for status to be reported" because no check-run with that literal name is posted.
- **How the name is derived**: GitHub matches the check by `integration_id` (`15368` = github-actions) plus the emitted check-run name. That emitted name comes from the job `name:` if one is set; otherwise it falls back to the job id. In `merge-gate.yml` the job id is `gate` and `name: gate`, so the emitted check-run name is `gate` -- that is the exact string the ruleset must require.
- **Adding a new aggregated check**: add it to `EXPECTED_CHECKS` in `merge-gate.yml`. Do not change the ruleset unless you intentionally rename the merge gate job's emitted check-run name, in which case the ruleset `context` must be updated to the new exact name.

## Trust Model
- **PR push (any contributor, including forks)**: Runs Tier 1 only. No CI secrets exposed. PR code is checked out and tested in an unprivileged context.
- **merge_group (write access required)**: Runs Tier 1 + Tier 2. Tier 2 sees secrets. The `gh-readonly-queue/main/*` ref is created by GitHub from the PR merged into main; only users with write access can trigger this by enqueueing a PR.
- **Trust boundary = write-access grant**, managed in repo Settings -> Collaborators. Write access is granted only to vetted contributors.
- **No environment approval gate** is required because the act of enqueueing IS the trust assertion. This replaces the previous `integration-tests` environment approval flow.

## Key Environment Variables
- `PYTHON_VERSION: '3.12'` - Standardized across all jobs
- `GITHUB_TOKEN` - Fallback token for compatibility (GitHub Actions built-in)
- `APM_RUN_INFERENCE_TESTS` - When `1`, enables live inference tests in validation scripts

## Performance Considerations
- **Critical path**: Unit tests do not delay native builds. A slow platform
  does not delay another platform's integration or isolated validation.
  Account for added runner setup and queue waits when measuring savings.
- **Native uv caching**: `setup-uv` action with `enable-cache: true` replaces manual `actions/cache@v3` blocks.
- **Runtime provisioning**: `APM_TEST_RUNTIMES` limits setup to selected
  prerequisites. Strict collection fails rather than silently skipping a
  selected test when a required runtime is missing.
- **Duration history**: Persist pytest timings and JUnit outcomes. Timing
  caches are scheduling hints, never cached pass results or test allowlists.
  Preserve complete shard coverage and existing fixture-affinity groups.
- **Tier 2 runs once per merged PR**, not per WIP push, since it triggers on `merge_group` only. Saves the bulk of integration minutes that the previous per-push flow burned.
- Python optimization level 2 in PyInstaller
- Aggressive module exclusions (tkinter, matplotlib, etc.)