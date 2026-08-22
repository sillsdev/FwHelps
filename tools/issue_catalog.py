"""The single issue vocabulary shared by every exporter stage."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class IssuePolicy:
    label: str
    fatal: bool
    provenance: str
    guidance: str = ""


def _policy(label: str, fatal: bool, provenance: str, guidance: str) -> IssuePolicy:
    return IssuePolicy(label, fatal, provenance, guidance)


ISSUE_CATALOG = MappingProxyType({
    "missing_link": _policy("Missing local link", True, "exporter",
        "Do not edit RoboHelp yet. Inspect the generated source path and target, then fix the exporter so a valid authored link remains valid."),
    "source_missing_link": _policy("Missing local link", False, "source",
        "Open the source topic in RoboHelp, find the hyperlink named in Evidence, and retarget or remove it; then rebuild the CHM."),
    "missing_image": _policy("Missing local image", True, "exporter",
        "Do not edit RoboHelp yet. Verify the source image exists, then fix exporter copying or link rewriting for the reported generated path."),
    "source_missing_image": _policy("Missing local image", False, "source",
        "Open the source topic in RoboHelp, find the image reference named in Evidence, and restore, retarget, or remove it; then rebuild the CHM."),
    "duplicate_title": _policy("Duplicate display title", False, "source",
        "Open the listed topics in RoboHelp and give each page a distinct, descriptive title or heading so search results identify the correct page."),
    "malformed_list": _policy("Malformed nested list", True, "exporter",
        "Inspect the reported generated page and its source topic, then correct list conversion while preserving the authored nesting."),
    "replacement_character": _policy("Replacement character", True, "exporter",
        "Compare the generated page with its CHM/PDF source and fix decoding or conversion where the replacement character was introduced."),
    "source_replacement_character": _policy("Replacement character", False, "source",
        "Open the reported source topic or PDF at the page in Evidence and replace the invalid or unsupported source character."),
    "raw_html": _policy("Raw HTML retained", False, "source",
        "Inspect the reported RoboHelp topic or PDF content and the tag names in Problem. Simplify unsupported source markup when practical; otherwise confirm the retained HTML is intentional."),
    "one_h1": _policy("Invalid H1 count", True, "exporter",
        "Compare the generated page headings with the source topic and fix title normalization so the Markdown has exactly one H1."),
    "destination_collision": _policy("Destination collision", True, "exporter",
        "Rename colliding source files/topics or adjust deterministic destination naming so every source maps to one unique output path."),
    "unsafe_uri": _policy("Unsafe URI", True, "exporter",
        "Inspect the generated path and URI in Evidence, then fix sanitization so unsafe source targets cannot be emitted as active links."),
    "path_escape": _policy("Local path escape", True, "exporter",
        "Inspect the generated link or image target and fix path normalization so it cannot resolve outside the published corpus."),
    "source_unsafe_uri": _policy("Source unsafe URI", False, "source",
        "Open the source topic in RoboHelp, find the URI in Evidence, and replace malformed, file:, or script-like targets with a valid safe link or remove them."),
    "source_path_escape": _policy("Source local path escape", False, "source",
        "Open the source topic in RoboHelp and retarget the local link or image so it stays inside the help project."),
    "pandoc_failure": _policy("Pandoc conversion failure", True, "exporter",
        "Reproduce conversion for the reported source topic, inspect the Pandoc error in Problem, and fix the exporter or unsupported source markup."),
    "unmapped_span": _policy("Unmapped span class", True, "exporter",
        "Find the reported CSS class in RoboHelp source, decide its intended semantics, and add an explicit exporter mapping before publishing."),
    "pdf_failure": _policy("PDF conversion failure", True, "exporter",
        "Open the reported PDF to confirm it is readable, then reproduce the error in Problem and fix the PDF conversion path."),
    "outline_drift": _policy("PDF outline drift", True, "exporter",
        "Review the reported PDF headings against the document, then either fix heading inference or deliberately repin the approved outline."),
    "outline_unpinned": _policy("PDF outline unpinned", True, "exporter",
        "Review the generated headings for the reported PDF and deliberately add its approved outline to pdf_outlines.json."),
    "stale_toc_entries": _policy("Stale TOC entry", False, "source",
        "Open the RoboHelp table of contents, locate the target in Evidence, and retarget or remove the entry before rebuilding the CHM."),
    "not_in_toc": _policy("Topic missing from TOC", False, "source",
        "Find the source topic path in RoboHelp and either add it to the appropriate table-of-contents location or remove the orphan topic."),
    "chm_failure": _policy("CHM conversion failure", True, "exporter",
        "Verify the named CHM opens normally, then reproduce the extraction/conversion error in Problem and correct the failing backend or source package."),
    "chm_discovery": _policy("CHM discovery failure", True, "exporter",
        "Place the intended CHM files at the repository root or correct discovery configuration, then rerun the exporter."),
    "unknown_issue": _policy("Unknown issue", True, "exporter",
        "Add the producer code shown in Problem to the canonical issue catalog with explicit severity, provenance, and repair guidance."),
})


# Producer report keys are deliberately aliases, so their severity and
# provenance are still selected by ISSUE_CATALOG rather than local mappings.
ISSUE_ALIASES = MappingProxyType({
    "broken_links": "source_missing_link",
    "broken_images": "source_missing_image",
    "duplicate_titles": "duplicate_title",
    "pandoc_failures": "pandoc_failure",
    "unmapped_span_classes": "unmapped_span",
    "destination_collisions": "destination_collision",
    "source_unsafe_uris": "source_unsafe_uri",
    "source_path_escapes": "source_path_escape",
    "pdf_failures": "pdf_failure",
    "html_tables_kept": "raw_html",
    "pdf_export_replacements": "replacement_character",
    "pdf_source_replacements": "source_replacement_character",
})


def canonical_code(code: str) -> str:
    """Return the catalog code for a producer report or issue code."""
    return ISSUE_ALIASES.get(code, code)


def policy_for(code: str) -> tuple[str, IssuePolicy]:
    """Return a safe catalog code and policy, including unknown integration codes."""
    canonical = canonical_code(str(code))
    policy = ISSUE_CATALOG.get(canonical)
    if policy is None:
        return "unknown_issue", ISSUE_CATALOG["unknown_issue"]
    return canonical, policy


__all__ = ["ISSUE_ALIASES", "ISSUE_CATALOG", "IssuePolicy", "canonical_code", "policy_for"]
