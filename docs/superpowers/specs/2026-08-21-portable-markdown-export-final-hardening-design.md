# Portable Markdown Export: Final Hardening Design

**Date:** 2026-08-21
**Status:** Approved for planning
**Scope:** Resolve every confirmed finding from the final cross-cutting review of PR #3.

## Objective

Make the exporter safe to reuse outside FwHelps, reproducible in CI, observable on failure, and resistant to malformed or adversarial source files without changing the validated Markdown produced by the current corpus.

## Safety and source identity

- CHM extraction reuse will require a converter-owned manifest whose source SHA-256 matches the current CHM. Missing or mismatched manifests force a fresh extraction.
- Direct extraction will reject destinations equal to, containing, or contained by the source CHM directory, in addition to the existing root and output protections.
- CHM and PDF discovery will reject symlinks and any resolved input outside the repository root.
- Absolute local Markdown and image targets will be fatal validation errors rather than silently ignored.
- PDF cleanup will accept prior manifest entries only when they match paths derivable from the manifest's recorded source PDF and remain under the PDF output root. A corrupt manifest will fail before deletion.

## Conversion correctness

- CHM topic and image destinations will share case-insensitive collision detection before any output is written.
- Lua span conversion will inventory every class before applying the first supported semantic transformation, so mixed known/unknown classes remain build-breaking.
- CHM TOC parsing will use an HTML parser and support attribute order, quoting, case, and escaped targets.
- Extraction validation will validate TOC targets and known truncation patterns without rejecting legitimate asset extensions solely because they are new.
- Frontmatter will use one safe serializer that rejects or escapes control characters and multiline scalar injection.
- Emitted links will use an allowlist of safe schemes. Unsafe schemes such as `javascript:` and `file:` will be reported as fatal and removed or neutralized before publication.

## Reporting policy

A single issue catalog will define canonical code, label, severity, and default provenance. CHM conversion, PDF conversion, corpus validation, console rendering, README rendering, and workflow summaries will consume this catalog. Source-retained PDF HTML will be reported with source provenance. Unknown issue codes remain fatal by default at integration boundaries.

## CI and publication

- Pull requests will run lint, unit/integration tests, and a dry-run corpus conversion with read-only permissions. PR jobs will never publish or tag.
- Publication remains limited to trusted pushes, release tags, and non-dry-run manual dispatches.
- Failure summaries and diagnostic artifacts will upload with `if: always()` when any report or staged diagnostics exist.
- GitHub Actions will be pinned by immutable commit SHA.
- Python will be pinned to an exact patch release.
- Python dependencies, including transitive dependencies, will be captured in a uv lockfile and installed frozen.
- The downloaded Pandoc package will be verified against a committed expected SHA-256. System packages that cannot be version-pinned reliably on the runner will be explicitly identified as the remaining platform dependency.
- Workflow write permission will be scoped to the publishing job; validation jobs use read-only contents permission.

## Testing and verification

Each behavioral fix begins with a failing regression test. Coverage will include stale reuse, source/output overlap, symlink escape, corrupt manifests, absolute targets, image collisions, mixed span classes, HHC variants, new asset types, unsafe schemes, YAML control content, issue-catalog consistency, PR non-publication, action/checksum pins, frozen dependency installation, and failed-build artifact behavior.

Completion requires:

1. all unit and integration tests pass;
2. repository-wide Ruff passes;
3. workflow static tests pass;
4. a complete two-CHM/13-PDF export has zero fatal issues;
5. an independent final review has no unresolved Critical or Important findings;
6. the code branch is committed and fast-forward pushed;
7. the `markdown-export` branch is regenerated from that commit and fast-forward pushed.

## Non-goals and retained decisions

- Existing source-quality advisories remain advisories unless they create unsafe or invalid generated output.
- The exporter remains in FwHelps for this PR, but reusable modules contain no FwHelps-specific policy.
- GitHub repository branch-protection settings are documented and verified where visible, but are not changed by this code patch without separate authorization.
- No force pushes or history replacement are allowed.
