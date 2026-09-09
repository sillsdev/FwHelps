# FwHelps

Documentation for [FieldWorks Language Explorer](https://software.sil.org/fieldworks/)
(FLEx). Help content is authored in Adobe RoboHelp and committed here as a
compiled CHM, alongside training and technical-note PDFs.

| File | Contents |
| --- | --- |
| `FieldWorks_Language_Explorer_Help.chm` | The main help system — 1,599 topics |
| `Language Explorer/Training/` | Technical notes: Send-Receive, imports, Word export |
| `Language Explorer/Utilities/` | AlloGen, PcPatr, ToneParsFLEx, VarGen documentation |
| `WW-ConceptualIntro/` | Conceptual Introduction to FLEx |

The FieldWorks installer consumes this repo directly: `patch-installer-cd.yml`
in [sillsdev/FieldWorks](https://github.com/sillsdev/FieldWorks) checks it out
via a `helps_ref` input, and `Build/releaseTagger.py` tags it `FieldWorks<version>`
at release time.

## Markdown export

The CHM is also published as markdown on the
[**`markdown-export`**](../../tree/markdown-export) branch — one file per help
topic, with images, YAML frontmatter, and a full table of contents.

It exists for two reasons:

- **AI retrieval.** The FieldWorks AI bot previously ingested raw RoboHelp
  HTML, where roughly two thirds of every topic is markup rather than
  documentation. The markdown corpus is about 65% smaller in tokens
  (~2.14M → ~759K) with the prose intact.
- **Reviewable diffs.** A help change is otherwise a 5&nbsp;MB opaque binary.
  On the export branch it is a readable text diff, one changed file per
  edited topic.

Built automatically by
[`markdown-export.yml`](.github/workflows/markdown-export.yml) on every push to
`develop`. Nothing there is hand-edited — edit the help in RoboHelp and commit
the CHM.

Each build also produces `author-report.md` for RoboHelp/PDF authors and
`author-report.json` for automation. Both cover broken links, topics missing
from the table of contents, and other source/export quality findings. The
Markdown report preserves the exact source path and evidence for every finding
and gives issue-specific repair guidance; JSON retains the stable
`{corpus, summary, issues}` schema.

Exporter mutation boundaries use a non-blocking native advisory lock on a
deterministic sibling lockfile, so cooperating invocations targeting the same
destination fail clearly instead of racing. The lock is a coordination aid,
not protection against a local process that deliberately ignores file locks;
stale lockfiles are harmless because ownership is held by the OS handle.

### Versions

Each FieldWorks release is tagged here by `releaseTagger.py`; the matching
export is tagged `markdown-export/<tag>`.

| FieldWorks | Released | Markdown export |
| --- | --- | --- |
| 9.3.7-beta | 2026-02-25 | [`markdown-export/FieldWorks9.3.7-beta`](../../tree/markdown-export/FieldWorks9.3.7-beta) |
| 9.3.6-beta | 2026-01-29 | [`markdown-export/FieldWorks9.3.6-beta`](../../tree/markdown-export/FieldWorks9.3.6-beta) |
| 9.3.4 | 2025-10-30 | [`markdown-export/FieldWorks9.3.4`](../../tree/markdown-export/FieldWorks9.3.4) |
| 9.3.1 | 2025-07-25 | [`markdown-export/FieldWorks9.3.1`](../../tree/markdown-export/FieldWorks9.3.1) |
| 9.3.0 | 2025-06-17 | [`markdown-export/FieldWorks9.3.0`](../../tree/markdown-export/FieldWorks9.3.0) |

> [!NOTE]
> Export tags are created going forward, as each release is tagged. Rows above
> that have no corresponding export tag yet can be backfilled by re-running the
> workflow against that tag.

## Tools

| Script | Purpose |
| --- | --- |
| [`tools/convert.py`](tools/convert.py) | CHM → markdown corpus (the build) |
| [`tools/fwhelp.lua`](tools/fwhelp.lua) | Pandoc filter: RoboHelp semantics → clean GFM |
| [`tools/chm_extract.py`](tools/chm_extract.py) | Cross-platform CHM extraction, with validation |
| [`tools/pdf_convert.py`](tools/pdf_convert.py) | PDF → markdown (bookmarks or font inference) |
| [`tools/pdf_outlines.json`](tools/pdf_outlines.json) | Pinned PDF outlines; drift fails the build |
| [`tools/survey.py`](tools/survey.py) | Read-only census of the corpus |

Local build (needs `pandoc` 3.x, and `7z` or Windows' built-in `hh.exe`):

```sh
uv run --frozen tools/convert.py --repo . --out export
```

### Reproducible local setup

The converter uses uv with the exact Python version in
[`.python-version`](.python-version). `uv.lock` is the authoritative dependency
file; `requirements.txt` and `requirements-dev.txt` are deterministic
lock-derived compatibility exports for older tooling and should not be edited
independently. The CI workflow installs only from the frozen lock:

```sh
uv python install
uv lock --check
uv sync --frozen
uv run --frozen \
  -m unittest discover -s tools -p 'test_*.py' -v
uv run --frozen ruff check tools
uv run --frozen \
  tools/convert.py --repo . --out export
uv run --frozen \
  tools/pdf_convert.py --repo . --out export --update-outlines
```

To regenerate the compatibility exports after changing project dependencies:

```sh
uv export --frozen --no-dev --no-hashes --format requirements.txt \
  --output-file requirements.txt
uv export --frozen --only-group dev --no-hashes --format requirements.txt \
  --output-file requirements-dev.txt
```

On PowerShell, the same `uv run --frozen` commands work unchanged. Updating
outline locks is intentional and should be reviewed with the resulting
`tools/pdf_outlines.json` change.

The workflow still installs the runner-image package `p7zip-full` for CHM
extraction; it is intentionally outside the Python lock. Pandoc is downloaded
as the pinned 3.9.0.2 amd64 package and its SHA-256 is checked before install.

> [!IMPORTANT]
> `hh.exe -decompile` silently truncates filenames when the output path exceeds
> Windows' 260-character limit — no error, no non-zero exit. `chm_extract.py`
> refuses to run it into a too-long path and validates every extraction against
> the CHM's own table of contents. Use a short `--work` directory on Windows,
> or install 7-Zip.
