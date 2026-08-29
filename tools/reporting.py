"""Canonical quality report model and renderers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from types import MappingProxyType

from issue_catalog import ISSUE_CATALOG, policy_for

LABELS = MappingProxyType({code: policy.label for code, policy in ISSUE_CATALOG.items()})


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    path: str = ""
    fatal: bool | None = None
    provenance: str | None = None
    detail: object = None

    def __post_init__(self) -> None:
        original_code = self.code
        code, policy = policy_for(original_code)
        unknown = code == "unknown_issue" and original_code != code
        if unknown:
            object.__setattr__(self, "message", f"[{original_code}] {self.message}")
        object.__setattr__(self, "code", code)
        # The legacy constructor accepts these fields for source compatibility,
        # but policy is always selected solely by the catalog code.
        object.__setattr__(self, "fatal", policy.fatal)
        object.__setattr__(self, "provenance", policy.provenance)

    @property
    def severity(self) -> str:
        return "error" if self.fatal else "warning"

    @property
    def label(self) -> str:
        return ISSUE_CATALOG[self.code].label

    def as_dict(self) -> dict:
        value = asdict(self)
        value.update(label=self.label, severity=self.severity)
        return value


class Report:
    def __init__(self, issues: Iterable[Issue] = (), metadata: dict | None = None) -> None:
        self.issues = list(issues)
        self.metadata = dict(metadata or {})

    def add(self, issue: Issue) -> Issue:
        self.issues.append(issue)
        return issue

    def extend(self, issues: Iterable[Issue]) -> None:
        self.issues.extend(issues)

    @property
    def fatal(self) -> bool:
        return any(issue.fatal for issue in self.issues)

    def as_dict(self) -> dict:
        by_code: dict[str, int] = {}
        for issue in self.issues:
            by_code[issue.code] = by_code.get(issue.code, 0) + 1
        return {
            "corpus": self.metadata,
            "summary": {
                "total": len(self.issues),
                "fatal": sum(issue.fatal for issue in self.issues),
                "advisory": sum(not issue.fatal for issue in self.issues),
                "by_code": by_code,
            },
            "issues": [issue.as_dict() for issue in self.issues],
        }

    def to_readme(self) -> str:
        lines = ["## Quality report", "", "| Check | Severity | Count |", "| --- | --- | ---: |"]
        counts: dict[tuple[str, str], int] = {}
        for issue in self.issues:
            key = (issue.label, issue.severity)
            counts[key] = counts.get(key, 0) + 1
        for (label, severity), count in sorted(counts.items()):
            lines.append(f"| {label} | {severity} | {count} |")
        if not counts:
            lines.append("| None | — | 0 |")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def _markdown_cell(value: object, *, code: bool = False) -> str:
        if value is None or value == "":
            return "—"
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        escaped = (value.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;").replace("\\", "&#92;")
                   .replace("`", "&#96;").replace("[", "&#91;")
                   .replace("]", "&#93;").replace("|", "\\|")
                   .replace("\r\n", "<br>").replace("\r", "<br>")
                   .replace("\n", "<br>"))
        if code:
            return f"`{escaped}`"
        return escaped

    @staticmethod
    def _repair_context(issue: Issue) -> str:
        if issue.provenance != "source":
            return "exporter"
        normalized = issue.path.replace("\\", "/").casefold()
        if normalized.startswith("pdf/") or normalized.endswith(".pdf"):
            return "PDF source"
        return "RoboHelp"

    def to_markdown(self) -> str:
        """Render the canonical report for source authors and maintainers."""
        data = self.as_dict()
        corpus = data["corpus"]
        summary = data["summary"]
        lines = [
            "# Author quality report", "",
            (
                "This report explains every source or export finding and how to repair it. "
                "Machine-readable detail is in [author-report.json](author-report.json)."
            ), "",
            "## Corpus", "", "| Item | Value |", "| --- | ---: |",
            f"| Source ref | {self._markdown_cell(corpus.get('source_ref'), code=True)} |",
            f"| CHMs | {corpus.get('chm_count', 0)} |",
            f"| Topics | {corpus.get('topic_count', 0)} |",
            f"| Images | {corpus.get('image_count', 0)} |",
            f"| PDFs | {corpus.get('pdf_count', 0)} |", "",
            "## Summary", "", "| Severity | Count |", "| --- | ---: |",
            f"| Fatal errors | {summary['fatal']} |",
            f"| Advisories | {summary['advisory']} |",
            f"| Total | {summary['total']} |", "",
        ]
        grouped: dict[str, list[Issue]] = {}
        for issue in self.issues:
            grouped.setdefault(issue.code, []).append(issue)
        for code in sorted(
            grouped,
            key=lambda item: (
                not ISSUE_CATALOG[item].fatal, ISSUE_CATALOG[item].label.casefold(), item,
            ),
        ):
            policy = ISSUE_CATALOG[code]
            issues = grouped[code]
            contexts = {self._repair_context(issue) for issue in issues}
            where = "/".join(sorted(contexts))
            lines.extend([
                f"## {policy.label} (`{code}`)", "",
                f"- **Severity:** {'fatal error' if policy.fatal else 'advisory'}",
                f"- **Owner:** {where}",
                f"- **Count:** {len(issues)}",
                f"- **How to fix in {where}:** {policy.guidance}", "",
                "| Source or generated path | Problem | Evidence |",
                "| --- | --- | --- |",
            ])
            for issue in issues:
                lines.append(
                    f"| {self._markdown_cell(issue.path, code=True)} "
                    f"| {self._markdown_cell(issue.message)} "
                    f"| {self._markdown_cell(issue.detail)} |"
                )
            lines.append("")
        if not grouped:
            lines.extend(["## Findings", "", "No findings.", ""])
        return "\n".join(lines)

    def to_console(self) -> str:
        fatal = [issue for issue in self.issues if issue.fatal]
        advisory = [issue for issue in self.issues if not issue.fatal]
        lines = [(
            f"quality report: {len(self.issues)} issue(s), "
            f"{len(fatal)} fatal, {len(advisory)} advisory"
        )]
        lines.append(f"fatal issues ({len(fatal)}):")
        if not fatal:
            lines.append("  none")
        for issue in fatal:
            where = f" [{issue.path}]" if issue.path else ""
            lines.append(f"  FATAL {issue.label}{where}: {issue.message}")

        counts: dict[tuple[str, str], int] = {}
        for issue in advisory:
            key = (issue.code, issue.label)
            counts[key] = counts.get(key, 0) + 1
        if counts:
            lines.append(
                f"advisories: {len(advisory)} issue(s) in {len(counts)} kind(s); "
                "see author-report.json for details"
            )
            for (_, label), count in sorted(
                counts.items(), key=lambda item: (item[0][1], item[0][0])
            ):
                lines.append(f"  WARN  {label}: {count}")
        else:
            lines.append("advisories: none")
        return "\n".join(lines)


def make_issue(code: str, message: str, path: str = "", detail: object = None) -> Issue:
    """Construct an issue using the catalog, safely handling new producer codes."""
    return Issue(str(code), str(message), str(path), detail=detail)


__all__ = ["LABELS", "Issue", "Report", "make_issue"]
