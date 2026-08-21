"""Thin orchestration seam for the CHM/PDF Markdown export."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import quote

import pdf_convert
from chm_convert import convert_chm
from corpus_validation import validate_corpus
from output_fs import OutputStaging, validate_output_paths
from reporting import Issue, Report

DEFAULT_SOURCE_REPO = "https://github.com/sillsdev/FwHelps"
DEFAULT_CHM_SOURCE_URL_BASE = "https://downloads.languagetechnology.org/fieldworks/Documentation/en"


def discover_chms(repo: Path) -> list[Path]:
    """Discover all CHMs at the repository root in stable case-folded order."""
    return sorted(
        (path for path in Path(repo).iterdir() if path.is_file() and path.suffix.lower() == ".chm"),
        key=lambda path: (path.name.casefold(), path.name),
    )


def _pdf_issue(code: str, item: object) -> Issue:
    mapping = {"pdf_failures": "pdf_failure", "outline_drift": "outline_drift",
               "outline_unpinned": "outline_unpinned", "destination_collisions": "destination_collision",
               "html_tables_kept": "raw_html",
               "pdf_export_replacements": "replacement_character"}
    mapped = mapping.get(code, code)
    fatal = code in {"pdf_failures", "outline_drift", "outline_unpinned",
                     "destination_collisions", "pdf_export_replacements"}
    path = item[0] if isinstance(item, (list, tuple)) and item else str(item)
    message = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else str(item)
    return Issue(mapped, message, str(path), fatal, "exporter")


def _write_readme(stage: Path, chms: list[dict], pdf_count: int,
                  source_ref: str, report: Report) -> None:
    inventory = (
        "- **Root CHMs (auto-discovered):** "
        + ", ".join(f"`{item['chm']}`" for item in chms)
        if chms else "- **Root CHMs (auto-discovered):** none found"
    )
    lines = [
        "# FieldWorks Help — Portable Markdown", "",
        "Generated documentation corpus. **Do not edit these files**; the tree is replaced as a set.", "",
        f"- **Source ref:** `{source_ref}`", f"- **CHMs:** {len(chms)}   **PDFs:** {pdf_count}", "",
        inventory,
        "",
        report.to_readme(), "", "Full detail is in [`author-report.json`](author-report.json).", "",
        "## CHM navigation", "",
    ]
    for result in chms:
        lines.append(f"### {result['chm']}")
        known_topics = {str(item).replace("\\", "/") for item in result.get("topics_paths", [])}
        for node in result.get("toc", []):
            if not node.get("title"):
                continue
            indent = "  " * max(0, int(node.get("depth", 1)) - 1)
            href = node.get("href", "")
            topic_path = Path(href.split("#", 1)[0]).as_posix()
            if href and topic_path in known_topics:
                target = quote(f"chm/{result['stem']}/{Path(href).with_suffix('.md').as_posix()}")
                lines.append(f"{indent}- [{node['title']}]({target})")
            else:
                lines.append(f"{indent}- **{node['title']}**")
    lines.extend(["", "## PDF navigation", ""])
    pdf_root = stage / "pdf"
    for path in sorted(pdf_root.rglob("*.md")) if pdf_root.exists() else []:
        rel = path.relative_to(stage).as_posix()
        lines.append(f"- [{path.stem.replace('_', ' ')}]({quote(rel)})")
    (stage / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (stage / ".nojekyll").write_text("", encoding="utf-8")


def build(repo: Path, out: Path, work: Path, *, reuse: bool = False,
          limit: int = 0, source_ref: str = "develop",
          source_repo: str = DEFAULT_SOURCE_REPO,
          chm_source_url_base: str = DEFAULT_CHM_SOURCE_URL_BASE) -> dict:
    """Build a complete corpus in private staging and promote only if valid."""
    repo, out, work = Path(repo).resolve(), Path(out).resolve(), Path(work).resolve()
    report = Report()
    advisory_links: set[tuple[str, str]] = set()
    advisory_images: set[tuple[str, str]] = set()
    source_replacement_paths: set[str] = set()
    chm_results: list[dict] = []
    chms = discover_chms(repo)
    if not chms:
        report.add(Issue("chm_discovery", "no repository-root CHM files found", "", True))

    # Validate the extraction workspace independently. The publication stage
    # must live beside the destination so promotion is one same-volume rename
    # even when --work is on another drive.
    validate_output_paths(out, work_dir=work, repo_root=repo, source_root=repo)
    with OutputStaging(out, repo_root=repo, source_root=repo) as staging:
        seen_names: set[str] = set()
        for chm in chms:
            from chm_metadata import safe_stem
            stem = safe_stem(chm.name)
            if stem.casefold() in seen_names:
                report.add(Issue("destination_collision", f"CHM namespace collision: {stem}", chm.name, True))
                continue
            seen_names.add(stem.casefold())
            try:
                result = convert_chm(chm, work, staging.path / "chm" / stem,
                                     reuse=reuse, limit=limit, source_ref=source_ref,
                                     source_url_base=chm_source_url_base)
                chm_results.append(result)
                stem = result.get("stem", "")
                for source_rel, raw_target in result.get("report", {}).get("broken_links", []):
                    output_rel = f"chm/{stem}/{Path(source_rel).with_suffix('.md').as_posix()}"
                    advisory_links.add((output_rel, raw_target))
                    if "#" in raw_target:
                        source_target, fragment = raw_target.split("#", 1)
                    else:
                        source_target, fragment = raw_target, ""
                    if re.search(r"\.html?$", source_target, re.IGNORECASE):
                        source_target = re.sub(r"\.html?$", ".md", source_target, flags=re.IGNORECASE)
                    emitted_target = source_target + (f"#{fragment}" if fragment else "")
                    advisory_links.add((output_rel, emitted_target))
                    advisory_links.add((output_rel, quote(emitted_target, safe="/#()%")))
                for source_rel, raw_target in result.get("report", {}).get("broken_images", []):
                    output_rel = f"chm/{stem}/{Path(source_rel).with_suffix('.md').as_posix()}"
                    advisory_images.add((output_rel, raw_target))
                    advisory_images.add((output_rel, quote(raw_target, safe="/#()%")))
                for source_rel in result.get("source_replacement_paths", []):
                    source_replacement_paths.add(
                        f"chm/{stem}/{Path(source_rel).with_suffix('.md').as_posix()}"
                    )
                for code, items in result.get("report", {}).items():
                    for item in items:
                        fatal = code in {"pandoc_failures", "unmapped_span_classes"}
                        chm_mapping = {
                            "pandoc_failures": "pandoc_failure",
                            "unmapped_span_classes": "unmapped_span",
                            "broken_links": "missing_link",
                            "broken_images": "missing_image",
                            "duplicate_titles": "duplicate_title",
                            "destination_collisions": "destination_collision",
                        }
                        report.add(Issue(
                            chm_mapping.get(code, code),
                            str(item), chm.name, fatal, "exporter" if fatal else "source",
                        ))
            except Exception as exc:  # noqa: BLE001 - isolate one corrupt CHM
                report.add(Issue("chm_failure", f"{type(exc).__name__}: {exc}", chm.name, True))

        pdf_url = f"{source_repo.rstrip('/')}/blob/{source_ref}/{{path}}"
        try:
            pdf_result, _ = pdf_convert.run(
                repo, staging.path / "pdf", update=False, source_url=pdf_url
            )
        except Exception as exc:  # noqa: BLE001 - isolate PDF backend failure
            pdf_result = {"converted": 0, "report": {
                "pdf_failures": [["<all>", f"{type(exc).__name__}: {exc}"]]
            }}
        pdf_report = pdf_result.get("report", {})
        pdf_export_paths = {
            str(item[0]) for item in pdf_report.get("pdf_export_replacements", [])
            if isinstance(item, (list, tuple)) and item
        }
        for item in pdf_report.get("pdf_source_replacements", []):
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            source_rel, details = item[0], item[1]
            emitted = Path(pdf_convert.slug_path(str(source_rel))).with_suffix(".md").as_posix()
            emitted_rel = f"pdf/{emitted}"
            # A page reporting both source and exporter replacements must not
            # be allowlisted: validator evidence must keep the exporter part
            # fatal even though the source risk is also reported.
            if str(source_rel) not in pdf_export_paths:
                source_replacement_paths.add(emitted_rel)
            report.add(Issue(
                "replacement_character",
                f"source PDF replacement characters: {details}",
                emitted_rel,
                False,
                "source",
                details,
            ))
        for code, items in pdf_report.items():
            if code == "pdf_source_replacements":
                continue
            for item in items:
                report.add(_pdf_issue(code, item))
        report.metadata = {
            "source_ref": source_ref,
            "source_repo": source_repo.rstrip("/"),
            "chms": [
                {"name": item.get("chm", ""), "stem": item.get("stem", ""),
                 "version": item.get("version", ""), "topics": item.get("topics", 0),
                 "images": item.get("images", 0)}
                for item in chm_results
            ],
            "chm_count": len(chm_results),
            "topic_count": sum(item.get("topics", 0) for item in chm_results),
            "image_count": sum(item.get("images", 0) for item in chm_results),
            "pdf_count": pdf_result.get("converted", 0),
        }
        # README navigation is part of the emitted corpus. Seed the linked
        # report target, render navigation, then validate all links before the
        # final report/count rewrite.
        (staging.path / "author-report.json").write_text("{}\n", encoding="utf-8")
        _write_readme(staging.path, chm_results, pdf_result.get("converted", 0), source_ref, report)
        report.extend(validate_corpus(
            staging.path,
            advisory_links=advisory_links,
            advisory_images=advisory_images,
            source_replacement_paths=source_replacement_paths,
        ))
        _write_readme(staging.path, chm_results, pdf_result.get("converted", 0), source_ref, report)
        (staging.path / "author-report.json").write_text(
            report.to_json(), encoding="utf-8"
        )
        if report.fatal:
            return {"report": report.as_dict(), "chms": chm_results,
                    "pdfs": pdf_result.get("converted", 0), "promoted": False}
        staging.promote()
    return {"report": report.as_dict(), "chms": chm_results,
            "pdfs": pdf_result.get("converted", 0), "promoted": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", type=Path)
    parser.add_argument("--out", default="out", type=Path)
    parser.add_argument("--work", default=None, type=Path)
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--source-ref", default="develop")
    parser.add_argument("--source-repo", default=DEFAULT_SOURCE_REPO)
    parser.add_argument("--chm-source-url-base", default=DEFAULT_CHM_SOURCE_URL_BASE)
    args = parser.parse_args()
    repo = args.repo.resolve()
    work = (args.work or repo / ".chm-work").resolve()
    result = build(repo, args.out.resolve(), work, reuse=args.reuse,
                   limit=args.limit, source_ref=args.source_ref,
                   source_repo=args.source_repo,
                   chm_source_url_base=args.chm_source_url_base)
    print(Report(Issue(**{key: value for key, value in item.items()
                          if key in {"code", "message", "path", "fatal", "provenance", "detail"}})
                 for item in result["report"].get("issues", [])).to_console())
    return 1 if result["report"]["summary"]["fatal"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
