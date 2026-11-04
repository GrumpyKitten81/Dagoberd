# GitHub Actions

Workflows verify source quality, installed release artifacts and security separately. Local gates remain required; CI adds reproducibility and platform coverage.

| Workflow | File | Triggers and purpose |
| --- | --- | --- |
| Quality | `.github/workflows/quality.yml` | Every PR, main push and manual dispatch; no path filters; stable `quality` gate |
| Release validation | `.github/workflows/ci.yml` | Matching main pushes/PRs, `v*` tags and manual dispatch; installed wheel/sdist and platform/browser tests |
| Release security | `.github/workflows/security.yml` | Matching pushes/PRs/tags, weekly and manual; secrets baseline and production dependency audit |
| Publish release assets | `.github/workflows/release.yml` | Published GitHub release or explicit manual dispatch; build, SBOM and provenance |
| Dependabot | `.github/dependabot.yml` | Weekly runtime/development dependency and Action updates |

## Quality

Ubuntu/Python 3.12/Poetry 2.2+ run `poetry sync`, all-file Pre-commit, full pytest, `poetry check` and `poetry build`. Ruff, formatting and Mypy run through Pre-commit. Documentation PRs also receive the stable `quality` status. Configure it as a required branch check at the actual publication repository; workflow presence does not configure protection.

Symlink escape tests skip locally when link creation lacks filesystem permissions.
Other creation errors remain failures. The Linux Quality job sets
`ACCOUNT_EVAL_REQUIRE_SYMLINKS=1`, which requires those security tests to execute
and fails if link creation is denied. Ordinary report-artifact checks run
independently of symlink permissions.

## Staged release validation

| Event | Installed artifact checks | Windows full suite | Chromium |
| --- | --- | --- | --- |
| Main push | Linux sdist 3.12 and wheel 3.14 | No | No |
| Developer PR, `v*` tag, manual | Linux/macOS/Windows and wheel 3.14 | Yes | Yes |
| Dependabot PR | Linux | No | No |
| Dependabot PR with `ready-for-merge` | Full matrix | Yes | Yes |

Path filters cover the validation workflow, metadata, lockfile, source and tests. Documentation alone does not start this additional matrix. A manual validation runs the full matrix; it does not publish.

Installed tests cover metadata, CLI/MCP, initialization/migrations, scoped imports, reports and encrypted backups without real bank/model/SMTP access. The activated browser suite covers the GUI, including language behavior. Source tests alone do not establish artifact isolation.

## Security and publication

All Actions use verified full commit SHAs with version comments. Dependabot maintains them. Checkout does not retain credentials. Verification workflows have read-only contents permission, jobs have explicit timeouts and only the publication job receives write/OIDC/attestation permissions. Release tags enter shell commands through environment variables.

Release security checks tracked sources against `.secrets.baseline` and exported production requirements with pip-audit. Preserve baseline assessments when updating line numbers. CodeQL is not configured. Verify available scanning, push protection, Dependabot and branch protection at the target repository; these files do not prove activation. Security reports use public GitHub Issues and the security form, without private reporting channels.

Publishing creates packages, SBOM and provenance. Only a published-release event uploads artifacts to that release; manual dispatch runs generation/attestation without that upload. Neither ordinary pushes nor verification workflows publish release assets. Both release creation and manual publishing workflow execution require an explicit instruction.

Each workflow has separate branch concurrency. New pushes cancel obsolete runs of that workflow. Changes to triggers/jobs must preserve the unfiltered Quality check, deliberate validation tiers, least privilege, secrets/dependency checks and the [Runbook release acceptance](RUNBOOK.md#14-manual-release-acceptance).
