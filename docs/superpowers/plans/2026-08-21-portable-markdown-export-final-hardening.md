# Portable Markdown Export Final Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every confirmed safety, correctness, portability, reporting, and CI gap from the final cross-cutting review without changing valid current-corpus content.

**Architecture:** Harden source discovery and extraction at their input boundaries, centralize issue policy in one module, and split CI validation from privileged publication. Converter-owned manifests authenticate reusable or removable state; validation treats ambiguous or unsafe output as fatal.

**Tech Stack:** Python 3.13.5, uv/`uv.lock`, `unittest`, Pandoc 3.9.0.2, Lua filters, GitHub Actions, PowerShell/Linux shell verification.

---

## File responsibilities

- `tools/source_safety.py`: repository-containment and non-symlink source discovery helpers shared by CHM and PDF tracks.
- `tools/frontmatter.py`: shared JSON-compatible YAML scalar serialization for CHM and PDF metadata.
- `tools/issue_catalog.py`: canonical issue code, label, severity, and provenance policy.
- `tools/output_fs.py`: staged promotion plus cooperative cross-process destination locking.
- `tools/chm_extract.py`: isolated extraction, parsed reference validation, and destructive-target rejection.
- `tools/chm_convert.py`: authenticated extraction reuse, destination collision checks, safe frontmatter, and unsafe-link reporting.
- `tools/pdf_convert.py`: safe PDF discovery and authenticated converter-manifest cleanup.
- `tools/corpus_validation.py`: emitted-corpus path and URI policy.
- `tools/fwhelp.lua`: AST transformations that inventory all authored classes and neutralize unsafe URI schemes.
- `tools/convert.py`: thin orchestration and diagnostic-report persistence.
- `.github/workflows/markdown-export.yml`: read-only validation job and separately privileged publication job.
- `pyproject.toml`, `uv.lock`, `.python-version`: exact Python and complete dependency lock.

### Task 1: Source identity, symlinks, and extraction safety

**Files:**
- Create: `tools/source_safety.py`
- Create: `tools/test_source_safety.py`
- Modify: `tools/chm_extract.py`
- Modify: `tools/chm_convert.py`
- Modify: `tools/pdf_convert.py`
- Test: `tools/test_chm_extract.py`
- Test: `tools/test_convert.py`
- Test: `tools/test_pdf_convert.py`

- [ ] **Step 1: Write failing source-boundary tests**

Add tests with this behavior:

```python
def test_source_file_rejects_symlink_outside_repository(self):
    outside = self.root.parent / "outside.pdf"
    outside.write_bytes(b"pdf")
    link = self.root / "linked.pdf"
    link.symlink_to(outside)
    with self.assertRaises(SourceSafetyError):
        discover_source_files(self.root, suffixes={".pdf"}, recursive=True)

def test_extract_rejects_source_directory_as_destination(self):
    chm = self.root / "Help.chm"
    chm.write_bytes(b"chm")
    with self.assertRaises(OutputPathError):
        extract(chm, self.root)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.review-venv\Scripts\python.exe -m unittest discover -s tools -p 'test_*.py'
```

Expected: failures for missing `SourceSafetyError`, symlink acceptance, source-directory extraction, and stale reuse.

- [ ] **Step 3: Implement shared source discovery and authenticated reuse**

Create a helper with this contract:

```python
class SourceSafetyError(ValueError):
    pass

def discover_source_files(
    root: Path, *, suffixes: set[str], recursive: bool
) -> list[Path]:
    """Return stable, regular, non-symlink files resolving beneath root."""
```

Write `.chm-extraction-manifest.json` only after validated extraction promotion:

```json
{
  "schema": 1,
  "source_name": "FieldWorks_Language_Explorer_Help.chm",
  "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Reuse only when schema, name, and hash match; otherwise perform fresh isolated extraction. Reject `outdir.resolve() == chm.parent.resolve()` and any destination that contains the CHM path.

- [ ] **Step 4: Verify GREEN**

Run the focused command from Step 2. Expected: all focused tests pass.

### Task 2: CHM parsing, collisions, frontmatter, and URI safety

**Files:**
- Modify: `tools/chm_extract.py`
- Modify: `tools/chm_convert.py`
- Modify: `tools/chm_metadata.py`
- Modify: `tools/corpus_validation.py`
- Modify: `tools/fwhelp.lua`
- Test: `tools/test_chm_extract.py`
- Test: `tools/test_chm_metadata.py`
- Test: `tools/test_convert.py`
- Test: `tools/test_corpus_validation.py`

- [ ] **Step 1: Write failing conversion-boundary tests**

Cover these exact cases:

```python
def test_absolute_local_target_is_fatal(self):
    (self.root / "topic.md").write_text("# Topic\n\n[bad](/outside.md)\n", encoding="utf-8")
    issues = validate_corpus(self.root)
    self.assertTrue(any(i.code == "path_escape" and i.fatal for i in issues))

def test_mixed_known_and_unknown_span_class_reports_unknown(self):
    markdown, unmapped = run_pandoc('<span class="Strong NewSemantic">text</span>')
    self.assertIn("NewSemantic", unmapped)

def test_casefolded_image_collision_is_fatal(self):
    # Icon.png and icon.PNG must claim one normalized destination.
    self.assertIn("destination_collisions", result["report"])
```

Also add HHC fixtures using single quotes, reversed `value`/`name` attributes, escaped paths, `.woff`, `.webp`, `.json`, and `.map`; YAML values with newlines/control characters; and `javascript:`, `file:`, `data:`, `https:`, and `mailto:` links.

- [ ] **Step 2: Verify RED**

Run:

```powershell
.review-venv\Scripts\python.exe -m unittest discover -s tools -p 'test_*.py'
```

Expected: failures for each new boundary behavior.

- [ ] **Step 3: Implement parsed references and allowlisted output**

Use `html.parser.HTMLParser` to collect `param` elements whose case-folded `name` is `local`, independent of attribute ordering or quote style. Validate local targets referenced by TOC and HTML `href`/`src`; permit unrelated asset extensions. Detect truncation only when an expected target is missing and a prefix sibling exists.

Build a shared case-folded claim table for every topic and image destination before writing. Inventory every span class before selecting the first supported transformation. Permit only `http`, `https`, and `mailto` external schemes; fragments and relative paths remain local. Neutralize source-authored unsafe targets and report them with source provenance; emitted unsafe targets remain fatal.

Serialize frontmatter scalars through one JSON-compatible YAML quoting function:

```python
def yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
```

JSON quoting escapes newlines and C0 controls, preventing YAML injection while preserving adversarial source metadata as data rather than rejecting an otherwise convertible help file.

- [ ] **Step 4: Verify GREEN**

Run the focused command from Step 2. Expected: all focused tests pass and ordinary images/links remain unchanged.

### Task 3: PDF manifest ownership and canonical issue policy

**Files:**
- Create: `tools/issue_catalog.py`
- Create: `tools/test_issue_catalog.py`
- Modify: `tools/pdf_convert.py`
- Modify: `tools/reporting.py`
- Modify: `tools/convert.py`
- Modify: `tools/corpus_validation.py`
- Test: `tools/test_pdf_convert.py`
- Test: `tools/test_reporting.py`
- Test: `tools/test_convert.py`

- [ ] **Step 1: Write failing manifest and catalog tests**

```python
def test_corrupt_manifest_cannot_delete_unrelated_output(self):
    unrelated = self.out / "keep.md"
    unrelated.write_text("keep", encoding="utf-8")
    previous = {"source.pdf": {"markdown": "keep.md", "images": None}}
    with self.assertRaises(ManifestError):
        promote_pdf_outputs(self.out, self.stage, previous, {})
    self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

def test_every_emitted_issue_code_has_one_policy(self):
    for code in emitted_issue_codes():
        self.assertIn(code, ISSUE_CATALOG)
```

- [ ] **Step 2: Verify RED**

Run:

```powershell
.review-venv\Scripts\python.exe -m unittest discover -s tools -p 'test_*.py'
```

Expected: corrupt-manifest and missing-central-policy failures.

- [ ] **Step 3: Authenticate removable PDF paths and centralize policy**

Version the PDF manifest and record source plus expected derived destinations:

```json
{
  "schema": 2,
  "files": {
    "Language Explorer/Training/source.pdf": {
      "markdown": "Language_Explorer/Training/source.md",
      "images": "Language_Explorer/Training/source_images"
    }
  }
}
```

Before backup or deletion, recompute `slug_path(source)` and require exact equality with both manifest destinations. Reject schema 1 or corrupt entries before mutation; the next successful run may replace a valid schema-2 set.

Define `IssuePolicy(label, fatal, provenance)` once. Make report creation use `make_issue(code, ...)`; unknown codes at integration boundaries become fatal exporter issues. Mark retained PDF HTML as source provenance.

- [ ] **Step 4: Verify GREEN**

Run the focused command from Step 2. Expected: all tests pass, including rollback tests.

### Task 4: Reproducible, observable, least-privilege CI

**Files:**
- Create: `pyproject.toml`
- Create: `uv.lock`
- Modify: `.python-version`
- Modify: `requirements.txt`
- Modify: `requirements-dev.txt`
- Modify: `.github/workflows/markdown-export.yml`
- Modify: `tools/convert.py`
- Modify: `README.md`
- Test: `tools/test_workflow.py`
- Test: `tools/test_convert.py`

- [ ] **Step 1: Write failing workflow and diagnostics tests**

Assert the workflow contains:

```python
self.assertIn("pull_request:", workflow)
self.assertIn("permissions:\n  contents: read", workflow)
self.assertIn("uv sync --frozen", workflow)
self.assertIn("ce4ac48f48aa7eadc1f5dbdf3449a1739f188ecb8c5421c5adc070fe7479e567", workflow)
self.assertNotRegex(workflow, r"uses:\s+[^\s]+@v\d+")
self.assertRegex(workflow, r"publish:\s*[\s\S]+permissions:\s*\n\s+contents: write")
```

Add a converter test proving `--diagnostics audit-diagnostics.json` writes canonical JSON even when corpus promotion is rejected for a fatal issue.

- [ ] **Step 2: Verify RED**

Run:

```powershell
.review-venv\Scripts\python.exe -m unittest discover -s tools -p 'test_*.py'
```

Expected: failures for missing PR trigger, mutable actions, missing digest/frozen lock, broad write permission, and absent failure diagnostics.

- [ ] **Step 3: Lock dependencies and split workflow privileges**

Set `.python-version` to `3.13.5`. Declare runtime dependencies and Ruff in `pyproject.toml`, then run:

```powershell
uv lock --python 3.13.5
uv sync --frozen
```

Keep `requirements.txt` and `requirements-dev.txt` as compatibility exports generated from the lock and document that `uv.lock` is authoritative.

Use these immutable pins:

```yaml
actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09
astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e
actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02
actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0
```

Verify Pandoc before installation:

```bash
echo "ce4ac48f48aa7eadc1f5dbdf3449a1739f188ecb8c5421c5adc070fe7479e567  /tmp/pandoc.deb" | sha256sum --check --strict
```

The `validate` job uses `contents: read`, runs on `pull_request`, trusted pushes/tags, and dispatch, writes diagnostics, and always uploads them. A separate `publish` job has `contents: write`, downloads the successful export artifact, and runs only for trusted push/tag or non-dry-run dispatch events. Document `p7zip-full` as the remaining runner-image package dependency.

- [ ] **Step 4: Verify GREEN**

Run:

```powershell
.review-venv\Scripts\python.exe -m unittest discover -s tools -p 'test_*.py'
.review-venv\Scripts\ruff.exe check tools
uv lock --check
uv sync --frozen
```

Expected: all commands exit zero.

### Task 5: Full verification and independent review

**Files:**
- Modify only files required by verified review findings.

- [ ] **Step 1: Run the complete local gates**

```powershell
.review-venv\Scripts\python.exe -m unittest discover -s tools -p 'test_*.py'
.review-venv\Scripts\ruff.exe check tools
git diff --check
uv lock --check
```

Expected: all tests pass, Ruff reports `All checks passed!`, and both remaining commands exit zero.

- [ ] **Step 2: Run a real corpus export**

Use fresh verified temporary directories and run:

```powershell
$sourceRef = git rev-parse --short HEAD
.review-venv\Scripts\python.exe tools\convert.py --repo . --out $auditOut --work $auditWork --source-ref $sourceRef --diagnostics $auditDiagnostics
```

Expected: exit zero; report has two CHMs, 1,630 topics, 13 PDFs, and zero fatal issues.

- [ ] **Step 3: Dispatch independent Luna spec and quality reviews**

Review `git diff 1ecb705...HEAD` against the final-hardening design. Resolve every Critical or Important finding and repeat the relevant test/review gate.

- [ ] **Step 4: Commit and push one implementation commit**

```powershell
git add -- .github/workflows/markdown-export.yml .python-version README.md requirements.txt requirements-dev.txt pyproject.toml uv.lock tools docs/superpowers/specs/2026-08-21-portable-markdown-export-final-hardening-design.md docs/superpowers/plans/2026-08-21-portable-markdown-export-final-hardening.md
git commit -m "Close final Markdown export hardening gaps"
git push fork tools/markdown-export
```

Expected: ordinary fast-forward push; PR #3 updates without force.

- [ ] **Step 5: Regenerate and publish Markdown incrementally**

Generate from the committed SHA, update the existing `johnml1135/FwHelps:markdown-export` tree using tracked-file deletion plus ordinary commit/push, and verify the remote report has zero fatal issues and 2,356 files unless intentional output changes alter that count.
