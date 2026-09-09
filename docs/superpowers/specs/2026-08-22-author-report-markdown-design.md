# Author Report Markdown Design

## Goal

Generate a human-readable `author-report.md` beside `author-report.json`. A
RoboHelp author must be able to identify the affected source topic or PDF,
understand the evidence, and know the appropriate repair action without
reading exporter code.

## Design

`Report` remains the single reporting model and JSON remains the stable
machine-readable format. The issue catalog gains canonical repair guidance so
labels, severity, provenance, and advice cannot drift between renderers.
`Report.to_markdown()` renders:

- corpus source ref and CHM/PDF/topic/image counts;
- fatal and advisory totals plus counts by issue type;
- one section per issue code with severity, provenance, and repair guidance;
- every finding with its exact source/output path, message, and structured
  evidence, escaped for deterministic Markdown tables;
- a link to the JSON report for automation and complete structured data.

Source issues use the authored `.htm` or PDF path already retained in the
canonical issue. Generated-tree validation findings retain their emitted
`chm/.../*.md` or `pdf/.../*.md` path, making exporter failures reproducible.
Repair guidance explains whether the correction belongs in RoboHelp/PDF
source or exporter code.

The corpus README links to both report formats. Both reports are written only
inside the private output stage and are promoted atomically with the corpus.

## Error Handling and Safety

Markdown rendering is pure and deterministic. Table cells escape pipes and
line breaks; structured evidence is serialized as compact Unicode JSON. An
unknown issue uses the existing fatal fallback policy and guidance directing
maintainers to add it to the canonical catalog.

## Tests

Tests verify the Markdown report contains corpus identity, summary totals,
canonical guidance, exact RoboHelp source paths, problematic targets,
structured evidence, escaping, and the JSON link. An orchestration test
verifies successful builds emit both report files and README links. The full
unit suite, Ruff, uv lock check, and a real corpus build remain release gates.
