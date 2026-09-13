# 0.0.6 publication preparation

2026-09-13. [Repository governance](../../GOVERNANCE.md) ·
[Native qualification](FP_FINAL_NATIVE_QUALIFICATION.md).

**Preparation, not a completed release or public-visibility change.** No remote
main/history rewrite, tag replacement, or visibility change was performed in this
rehearsal. The running/installed Pulse and its databases were not modified.

## Candidate and exclusions

The candidate contains the latest qualified 0.0.6 source plus the administrator
review policy and publication documentation. Version remains 0.0.6; the Elastic
License 2.0 + SaaS/Branding Addendum remains unchanged.

An isolated, no-hardlinks mirror rehearsal removed exactly these operational
artifact paths from the relevant development history:

- `.playwright-mcp/`: 48 browser logs and page snapshots;
- `grafx-v004-runtime-1000-nodes.png`: a Pulse graph screenshot with operational
  node titles, confirmed by visual inspection.

The original mirror remains local and private for recovery. It must never be
pushed or published. The original developer checkout remains unchanged.
Deleting a file in a later commit alone would not provide this cleanup.

## Measured rewrite scope

A whole-history experiment was rejected: it unnecessarily changed existing
commit/tag identities. The selected rehearsal restricts filtering to the 12
commits from the artifact-introduction commit through the current 0.0.6 head,
preserving earlier ancestors and commit-message references.

| Check | Rehearsal result |
|---|---|
| Original remote branch that would require replacement | Only `feature/v0.0.6` |
| Rewritten development commits | 12 |
| Existing `main` | Unchanged |
| Existing tags `v0.0.3`, `v0.0.4`, `v0.0.5` | Unchanged, including tag-object identities |
| Existing GitHub PR heads and merge reference | Unchanged |
| Excluded paths reachable through candidate-mirror refs | 0 |
| Git tree identities for `src`, `tests`, `tools`, `docs`, `pyproject.toml`, `LICENSE` before governance additions | Identical to the qualified 0.0.6 source |
| Git object consistency | `git fsck --full` completed without corruption |

Partial filtering leaves an old dangling object in the **private rehearsal**;
unreachable-in-refs is not proof that GitHub caches or old objects have been erased.
The local commit map is retained privately to repair provenance references and
coordinate other clones. Do not merge an old development branch into the sanitized
one: doing so reintroduces the removed history.

The machine's Git 2.34.1 does not support `cat-file --batch-command`, required by
the installed filter tool's `--sensitive-data-removal` mode. That attempt failed
before rewriting. The successful local rehearsal used ordinary path filtering on
the bounded revision range. A production cleanup must either use a compatible
Git/tool pair for the sensitive-data-removal workflow or explicitly account for
its additional reference/cache reporting; this rehearsal does not claim to have
completed that workflow.

## Validation

- 108 focused governance, CLI attribution/parser and documentation tests passed,
  with zero failures/errors/skips, on the sanitized 0.0.6 candidate.
- Documentation links/anchors, configuration declarations and generated API
  reference checks passed.
- All 251 package payloads agree with the qualified wheel after normalizing
  checkout CRLF/LF differences; 154 files differ only in line endings. The wheel
  itself remains unchanged, SHA-256
  `d666704a14492d737c43f0293e71605aeaf279aad65b4086d08bcb0160020b70`.
- Prior complete native evidence remains 25,077 passes / 19 attributed skips;
  the prior required Cypher profile remains 3,896 passes. These are previously
  measured results, not reruns of the full suites for this documentation cleanup.
- Gitleaks 8.30.1 scanned 858 candidate ancestors / approximately 40.95 MB. Its
  five generic-key findings match the already inspected non-credential prose,
  fixture and example candidates. No new candidate was reported. A scanner's
  findings are not a complete confidential-data audit.
- GitHub Actions did not run the governance tests: annotations state that jobs
  could not start due to account payment/spending-limit restrictions. These CI
  failures are neither test failures nor passing CI evidence.

## Remaining execution order

1. Obtain administrative PR review of the sanitized release candidate and its
   governance policy. No self-approval or administrator bypass is implied.
2. Obtain explicit authorization to **apply** the limited remote history rewrite,
   not merely prepare it. Recheck remote heads and collaborators' work; use an
   exact expected-head lease for only the affected branch, not a mirror-wide
   force-push. Preserve the private backup and commit map.
3. Coordinate stale clones and confirm cleanup of other GitHub references, old
   commit views and caches. Involve GitHub Support if necessary, following
   [GitHub's sensitive-data removal procedure](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).
   Ordinary force-push alone cannot guarantee cache removal.
4. Merge the approved **sanitized** 0.0.6 candidate into `main` via PR; verify the
   merged package version, source identity and policy files. Do not merge the old
   unsanitized feature history. The standalone governance PR is superseded only
   when the combined candidate is accepted.
5. Create annotated `v0.0.6` at the verified merged `main` commit and push the tag.
   Never point it at a preparation branch or silently replace an existing tag.
6. Change visibility only when the data-removal checks are complete. Apply the
   main protection payload immediately in the coordinated no-push window, then
   read it back and verify administrator enforcement, owner approval, last-push
   review, conversation resolution, and force-push/deletion refusal. Confirm
   fork-workflow approval and read-only Actions defaults. Repository visibility
   and branch protection are separate, non-atomic API operations.

This document does not authorize a PyPI upload, a global reinstall, changing the
Pulse runtime, discarding private backups, or publishing operational evidence.
