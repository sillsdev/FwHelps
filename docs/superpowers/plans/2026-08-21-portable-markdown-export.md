# Portable Markdown Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the CHM/PDF exporter into a safe, reproducible, portable converter with clean and validated generated Markdown.

**Architecture:** Repository policy remains in a thin orchestration module while extraction, PDF conversion, output staging, and corpus validation expose small reusable interfaces. Every build is staged and validated before replacement, and CI publishes an ordinary incremental branch history.

**Tech Stack:** Python 3.13 managed by uv, Pandoc 3.9.0.2 with Lua, PyMuPDF, pymupdf4llm, unittest, Ruff, GitHub Actions.

---

### Task 1: Safe CHM extraction and output ownership

**Files:**
- Modify: `tools/chm_extract.py`
- Create: `tools/output_fs.py`
- Create: `tools/test_chm_extract.py`
- Create: `tools/test_output_fs.py`

- [ ] Write failing tests proving each extraction backend receives an empty private directory, failed/invalid attempts do not contaminate later attempts, and only a validated extraction is promoted.
- [ ] Write failing tests proving filesystem roots, repository roots, sources, and overlapping work/output paths are rejected before removal.
- [ ] Implement `extract(chm, destination)` with per-backend staging and validated promotion.
- [ ] Implement a small output-staging interface that owns its temporary directory and atomically promotes a successful tree.
- [ ] Run the new tests and the complete test suite under uv.

### Task 2: Complete and reproducible PDF conversion

**Files:**
- Modify: `tools/pdf_convert.py`
- Modify: `tools/pdf_outlines.json`
- Modify: `tools/test_pdf_convert.py`

- [ ] Write failing tests for complete normalized `(level, text)` outline comparison, fatal unpinned PDFs, traceability frontmatter, destination collisions, and removal of stale PDF outputs.
- [ ] Change outline locking to compare the complete post-normalization outline and make missing locks fatal outside `--update-outlines`.
- [ ] Add source hash, source URL, available metadata, and conversion identity to PDF frontmatter.
- [ ] Stage PDF output as a complete subtree or register all destinations before writing so stale outputs and collisions cannot survive.
- [ ] Regenerate outline locks explicitly and run all PDF tests.

### Task 3: uv-controlled CI and incremental publication

**Files:**
- Create: `.python-version`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Modify: `.github/workflows/markdown-export.yml`
- Modify: `README.md`

- [ ] Pin Python 3.13 and exact converter dependencies, including Ruff for development.
- [ ] Replace setup-python/pip installation with uv interpreter provisioning and locked requirement installation.
- [ ] Make CI tests, lint, and conversion run through the uv-managed environment.
- [ ] Replace orphan initialization and force-push with checkout/update of ordinary `markdown-export` history, including first-publication handling.
- [ ] Document the exact local uv setup, test, conversion, and outline-update commands.

### Task 4: Portable multi-input orchestration and emitted-corpus validation

**Files:**
- Modify: `tools/convert.py`
- Modify: `tools/fwhelp.lua`
- Create: `tools/corpus_validation.py`
- Create: `tools/reporting.py`
- Create: `tools/test_convert.py`
- Create: `tools/test_corpus_validation.py`

- [ ] Write failing tests for discovering both CHMs, collision rejection, disambiguating display titles, related frontmatter, proper nested lists, and validation of emitted Markdown links/images.
- [ ] Replace the hard-coded single-CHM flow with deterministic input discovery and format namespaces or explicit collision rejection.
- [ ] Move report definitions and fatality into one catalog consumed by JSON, README, console, and workflow summary.
- [ ] Fix list conversion and title selection without losing source content.
- [ ] Run post-generation validation over the staged corpus, fail on exporter-caused defects, then promote output.

### Task 5: Full-corpus verification and review

**Files:**
- Modify only files required by confirmed review defects.

- [ ] Run all tests and Ruff through uv from a clean process.
- [ ] Build the complete corpus from both CHMs and all PDFs into a new temporary destination.
- [ ] Audit Markdown count, H1 contract, duplicate titles, PDF hierarchy, local links, images, malformed lists, raw HTML, replacement characters, outline locks, and stale files.
- [ ] Run the same build twice with one controlled source/output change to verify deterministic and incremental behavior.
- [ ] Perform independent specification and architecture/clean-code reviews; fix every Critical or Important issue and rerun verification.

