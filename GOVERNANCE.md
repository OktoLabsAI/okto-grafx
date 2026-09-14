# Repository governance

[README](README.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

## License and public source

Okto Grafx retains **Elastic License 2.0 + SaaS/Branding Addendum**, exactly as
specified in [LICENSE](LICENSE). The addendum has not been removed or weakened.
Package metadata and CLI notices must name these combined terms, not the SPDX
identifier `Elastic-2.0` alone. Public visibility is not relicensing, permission
to ignore the addendum, or a claim of OSI-approved open source. Older published
artifacts and third-party components keep their own accompanying terms.

The [official ELv2 text](https://www.elastic.co/licensing/elastic-license) is a
reference, not a substitute for this repository's custom LICENSE.

## Main-branch policy

Every change must reach `main` through a pull request, including changes authored
by administrators. No direct pushes, force pushes or branch deletion are permitted
under the configured policy. Review conversations must be resolved.

At least one approval must come from a designated administrator in
[CODEOWNERS](.github/CODEOWNERS): `@jpbraga`, `@Maheidem`, or
`@oktolabsai-developer`. These accounts held the GitHub administrator role when
checked on 2026-09-13. Revalidate the roster when repository roles change; GitHub
does not dynamically interpret a list of usernames as "all administrators".
Other reviewers may comment or approve, but their approval does not satisfy the
code-owner requirement. The PR author cannot approve their own PR.

Stale approvals are dismissed on reviewable changes. The most recent reviewable
push must be approved by someone other than its pusher. Administrators must not
disable protection to bypass review. Administrators with settings access can
technically change GitHub policy; branch protection is not an immutable control
against those who administer the repository itself.

## Nexus baseline and deliberate differences

The reference is [Okto Nexus](https://github.com/OktoLabsAI/okto-nexus), inspected
through the GitHub API on 2026-09-13. It uses classic branch protection rather than
a repository ruleset.

| Setting | Nexus observed | Grafx target |
|---|---|---|
| PR approval count | 1 | 1 |
| Code-owner approval | Required, same three accounts | Required |
| Dismiss stale reviews | Yes | Yes |
| Last reviewable push approved by another person | Yes | Yes |
| Resolve conversations | Yes | Yes |
| Force pushes / branch deletion | Disabled | Disabled |
| Enforce rules for administrators | **No** | **Yes**, to prohibit direct administrator pushes |
| Required status checks | Not configured | Not configured; review actual CI evidence before merge |
| Linear history / signed commits | Not required | Not required |
| Default Actions token | Read-only; cannot approve PRs | Same, already observed |
| Fork workflow approval | First-time contributors | Same when public |
| Merge / squash / rebase | Enabled | Same, already observed |
| Auto-merge / automatic branch deletion | Disabled | Same, already observed |

Existing collaborator roles are not expanded by this preparation. In particular,
a read-only collaborator is not promoted merely because Nexus grants that account
write access. CODEOWNERS and a JSON file are declarations, **not active server-side
enforcement**. GitHub enforces the review rules only after protection is applied.

## Activation status and publication prerequisites

The owner authorized eventual public visibility and **preparation** of history
sanitization on 2026-09-13. The [0.0.6 sanitation rehearsal](docs/reports/V006_PUBLICATION_PREPARATION.md)
records the isolated candidate, limited rewrite impact and remaining execution
requirements. This authorization has not been treated as permission to bypass
administrator review or force-rewrite the original remote branch.

**The repository remains private during this preparation.** On 2026-09-13 GitHub
returned HTTP 403 for Grafx branch protection/rulesets, requiring an eligible paid
plan or public visibility. Fork-contributor workflow approval similarly returned
HTTP 422 while private. Do not claim that the target protection is active.

Before changing visibility:

1. Review and merge this governance PR through an administrator review, without
   bypassing the intended policy. It was merged together with the 0.0.6 release candidate in PR #5 (tag `v0.0.6`).
2. Resolve the privacy findings in all published branches/tags and relevant PR
   references. The local audit found 48 tracked browser logs/snapshots in a
   development branch, containing Pulse operational content. Deleting them from
   the latest tree or adding `.gitignore` does **not** remove Git history.
3. Decide explicitly whether to sanitize/rewrite history or authorize public
   release of that material. A rewrite changes commit IDs and can invalidate
   provenance references, tags and downstream clones; it requires a separate
   migration plan and authorization. Do not silently force-push a rewritten main.
4. Repeat secret and privacy checks against the exact references that will become
   public. Also review releases, binaries/assets, issues, PR discussions, Actions
   logs/artifacts, and GitHub-hosted references not represented by a normal clone.
   A secret scan does not detect all confidential business content.
5. Obtain approval to change visibility. Prefer enabling the necessary paid-plan
   protection while still private. Otherwise use a coordinated no-push maintenance
   window: changing visibility and applying protection are separate GitHub API
   operations, not an atomic transition.

The preparation audit used checksum-verified Gitleaks 8.30.1 with redacted output
and `git --all`: 996 commits / approximately 44.4 MB at that snapshot, five generic
key candidates. Inspection of their exact historical source classified all five
as non-credential false positives: two prose fragments, two occurrences of an
example idempotency key, and a local import test fixture key. No real credential
was identified in those five findings; this is not a guarantee that the repository
contains no secrets. The browser-data finding is independent of secret detection.
Local scanner reports are not
committed because audit artifacts can themselves contain sensitive information.

## Apply and verify protection

An authorized administrator runs the following from the approved checkout only
after GitHub makes the feature available. These commands do not change visibility:

```sh
gh api --method PUT repos/OktoLabsAI/okto-grafx/branches/main/protection --input .github/main-protection.json
gh api --method PUT repos/OktoLabsAI/okto-grafx/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
gh api --method PUT repos/OktoLabsAI/okto-grafx/actions/permissions/fork-pr-contributor-approval -f approval_policy=first_time_contributors
gh api repos/OktoLabsAI/okto-grafx/branches/main/protection
gh api repos/OktoLabsAI/okto-grafx/actions/permissions/workflow
gh api repos/OktoLabsAI/okto-grafx/actions/permissions/fork-pr-contributor-approval
gh api repos/OktoLabsAI/okto-grafx/codeowners/errors
```

Compare the returned protection with [.github/main-protection.json](.github/main-protection.json),
especially `enforce_admins.enabled`, all four review fields, force-push/deletion
refusals, and conversation resolution. Verify the three owners still have
administrator roles. Do not test protection by pushing a throwaway commit to main.
GitHub's [protected-branch documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
describes plan availability and the administrator bypass default.

Do not copy Nexus's disabled security scanning as a security recommendation.
Once available, administrators should review private vulnerability reporting,
secret scanning/push protection and Dependabot independently. Never attach raw
scan results, databases, tokens or browser snapshots to a public issue.
