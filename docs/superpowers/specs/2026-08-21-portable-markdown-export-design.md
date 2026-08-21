# Portable Markdown Export Hardening Design

## Goal

Turn the current FwHelps CHM/PDF converter into a safe, reproducible, portable tool that produces reviewable Markdown and can later move to a shared repository without redesigning its core interfaces.

## Scope and ownership

The implementation remains in `FwHelps/tools` for this pass. Repository-specific policy—input discovery, source URLs, workflow triggers, and publication branch—stays at the outer orchestration seam. Extraction, document conversion, output staging, and corpus validation must not depend on the FwHelps repository name or on one hard-coded CHM.

Both CHM files and every repository PDF are inputs. Generated destinations must be registered before writing so two inputs cannot silently claim the same path. A future extraction into a standalone repository should therefore move the converter modules with minimal changes while leaving a thin FwHelps adapter behind.

## Safety and publication

Conversion builds into a converter-owned staging directory. It rejects repository roots, source directories, filesystem roots, and overlapping work/output paths before any recursive removal. A successful build replaces the destination; a failed build leaves the previous destination intact. CHM extraction backends likewise use isolated temporary directories and promote only a validated result.

The publication branch retains parent history. CI checks out the existing export branch when present, replaces only its generated tree, commits the resulting diff, and pushes normally. It must not use orphan commits or force-pushes, so ordinary branch protection remains compatible.

## Reproducibility

`.python-version` pins Python 3.13 for uv. Exact runtime versions live in `requirements.txt`; development-only tools live in `requirements-dev.txt`. CI installs uv, provisions the pinned interpreter, installs the locked requirements, runs tests and lint, then converts. Pandoc remains pinned.

## Conversion contracts

CHM conversion preserves the authored hierarchy while using disambiguating page headings when titles collide. Related-topic metadata is emitted explicitly. Nested lists must render as real Markdown lists.

PDF conversion records the complete normalized outline `(level, text)`, not only a count. Every discovered PDF must have a lock entry in normal mode; updating locks requires the explicit update command. PDF frontmatter includes source path, stable source URL, source content hash, available PDF metadata, heading strategy, and normalized outline count. Converter-owned PDF outputs are replaced as a set so deleted inputs leave no stale files.

## Validation and reporting

Validation runs against the emitted corpus after all conversion steps. It verifies local links and images, destination collisions, unique output paths, duplicate display titles, malformed list markers, replacement characters, raw HTML inventory, and PDF outline locks. Build-breaking checks and advisory checks are defined once and rendered consistently in the README, JSON report, console output, and workflow summary.

Source-document defects remain visible as advisories with provenance; exporter regressions fail the build. Complex tables may remain raw HTML when GFM cannot represent them without information loss, but every retained table is counted.

## Tests and acceptance

Every behavior change follows red-green-refactor. Unit tests cover path rejection, backend isolation, destination collisions, complete outline locks, unpinned PDFs, stale-output removal, multi-CHM discovery, related metadata, title disambiguation, list conversion, and emitted-corpus link/image checks.

Acceptance requires:

- all unit tests and focused lint passing under uv;
- a complete build of both CHMs and all PDFs;
- no exporter-caused unresolved links or images;
- no silent destination overwrites or stale generated files;
- no malformed `- -` nested-list output;
- all PDFs pinned by complete normalized outlines;
- a clean incremental publication design with no force-push;
- an audited `author-report.json` whose counts match the generated tree.

