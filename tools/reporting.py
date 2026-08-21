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
