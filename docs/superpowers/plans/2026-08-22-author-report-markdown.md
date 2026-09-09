# Author Report Markdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a repair-oriented Markdown author report beside the canonical JSON report.

**Architecture:** Extend the canonical issue catalog with repair guidance and add a pure `Report.to_markdown()` renderer. The corpus orchestrator writes and links both formats inside its existing atomic staging boundary.

**Tech Stack:** Python 3.13, standard-library `json`, `unittest`, uv, Ruff.

---

### Task 1: Canonical Markdown renderer

**Files:**
- Modify: `tools/issue_catalog.py`
- Modify: `tools/reporting.py`
- Test: `tools/test_reporting.py`
- Test: `tools/test_issue_catalog.py`

- [ ] **Step 1: Write failing renderer and catalog tests**

Add representative source findings with a `.htm` path, broken target, pipe,
newline, and structured detail. Assert that `to_markdown()` includes corpus
counts, code, severity, provenance, canonical repair guidance, exact evidence,
safe table escaping, and the JSON link. Assert every issue policy has nonempty
repair guidance.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run --frozen python -m unittest discover -s tools -p 'test_reporting.py'`

Expected: failure because `IssuePolicy.guidance` and `Report.to_markdown()` do
not exist.

- [ ] **Step 3: Implement the catalog guidance and Markdown renderer**

Add `guidance: str` to `IssuePolicy`, populate it for every canonical issue,
and implement deterministic grouping plus safe cell formatting in
`Report.to_markdown()`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run --frozen python -m unittest discover -s tools -p 'test_reporting.py'`

Expected: all reporting tests pass.

### Task 2: Emit and link the report

**Files:**
- Modify: `tools/convert.py`
- Modify: `tools/test_convert.py`
- Modify: `README.md`

- [ ] **Step 1: Write a failing orchestration test**

Assert that a successful staged build contains `author-report.md` and
`author-report.json`, and that the generated README links both files.

- [ ] **Step 2: Run the focused orchestration test and verify RED**

Run: `uv run --frozen python -m unittest discover -s tools -p 'test_convert.py'`

Expected: failure because `author-report.md` is absent.

- [ ] **Step 3: Write both reports within the atomic stage**

Seed both linked files before corpus link validation, rewrite both after final
validation, update the generated README links, and document both formats in
the repository README.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run --frozen python -m unittest discover -s tools -p 'test_convert.py'`

Expected: all orchestration tests pass.

### Task 3: Verify, publish, and hand off

**Files:**
- Generated: `author-report.md` on branch `markdown-export`

- [ ] **Step 1: Run all local gates**

Run: `uv run --frozen python -m unittest discover -s tools -p 'test_*.py'`
Run: `uv run --frozen ruff check tools`
Run: `uv lock --check`
Run: `git diff --check`

Expected: all commands exit zero.

- [ ] **Step 2: Commit and push the tool branch**

Commit only the feature, tests, and approved design/plan. Keep the imported
conversation transcript untracked. Push `tools/markdown-export` to the fork.

- [ ] **Step 3: Regenerate and verify the real corpus**

Run the exporter from the committed SHA using the authenticated short-path
work directory. Verify 0 fatal issues, expected corpus counts, both report
formats, valid README links, and no internal lock artifacts.

- [ ] **Step 4: Publish and verify the generated branch**

Replace the contents of the fork's `markdown-export` branch in a temporary
clone, commit, push normally, and verify the remote file URL.
