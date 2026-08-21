"""The single issue vocabulary shared by every exporter stage."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class IssuePolicy:
    label: str
    fatal: bool
    provenance: str


def _policy(label: str, fatal: bool, provenance: str) -> IssuePolicy:
    return IssuePolicy(label, fatal, provenance)


ISSUE_CATALOG = MappingProxyType({
    "missing_link": _policy("Missing local link", True, "exporter"),
    "source_missing_link": _policy("Missing local link", False, "source"),
    "missing_image": _policy("Missing local image", True, "exporter"),
    "source_missing_image": _policy("Missing local image", False, "source"),
    "duplicate_title": _policy("Duplicate display title", False, "source"),
    "malformed_list": _policy("Malformed nested list", True, "exporter"),
    "replacement_character": _policy("Replacement character", True, "exporter"),
    "source_replacement_character": _policy("Replacement character", False, "source"),
    "raw_html": _policy("Raw HTML retained", False, "source"),
    "one_h1": _policy("Invalid H1 count", True, "exporter"),
    "destination_collision": _policy("Destination collision", True, "exporter"),
    "unsafe_uri": _policy("Unsafe URI", True, "exporter"),
    "path_escape": _policy("Local path escape", True, "exporter"),
    "source_unsafe_uri": _policy("Source unsafe URI", False, "source"),
    "source_path_escape": _policy("Source local path escape", False, "source"),
    "pandoc_failure": _policy("Pandoc conversion failure", True, "exporter"),
    "unmapped_span": _policy("Unmapped span class", True, "exporter"),
    "pdf_failure": _policy("PDF conversion failure", True, "exporter"),
    "outline_drift": _policy("PDF outline drift", True, "exporter"),
    "outline_unpinned": _policy("PDF outline unpinned", True, "exporter"),
    "stale_toc_entries": _policy("Stale TOC entry", False, "source"),
    "not_in_toc": _policy("Topic missing from TOC", False, "source"),
    "chm_failure": _policy("CHM conversion failure", True, "exporter"),
    "chm_discovery": _policy("CHM discovery failure", True, "exporter"),
    "unknown_issue": _policy("Unknown issue", True, "exporter"),
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
