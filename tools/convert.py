"""Thin orchestration seam for the CHM/PDF Markdown export."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

import pdf_convert
from chm_convert import run_in_private_stage
from corpus_validation import validate_corpus
from output_fs import ExportLock, OutputPathError, OutputStaging, validate_output_paths
from reporting import Issue, Report, make_issue
from source_safety import discover_source_files

DEFAULT_SOURCE_REPO = "https://github.com/sillsdev/FwHelps"
DEFAULT_CHM_SOURCE_URL_BASE = "https://downloads.languagetechnology.org/fieldworks/Documentation/en"


def discover_chms(repo: Path) -> list[Path]:
    """Discover all CHMs at the repository root in stable case-folded order."""
    return discover_source_files(Path(repo), suffixes={".chm"}, recursive=False)


def _report_issue(code: str, item: object, default_path: str = ""):
    if code == "unmapped_span_classes" and isinstance(item, (list, tuple)):
        class_name = str(item[0]) if item else ""
        count = item[1] if len(item) > 1 else 0
        topics = [str(path) for path in item[2]] if len(item) > 2 else []
        path = topics[0] if topics else default_path
        detail = {"class": class_name, "count": count, "topics": topics}
        message = f"span class '{class_name}' reported {count} time(s)"
        return make_issue(code, message, path, detail)
    if code == "duplicate_titles" and isinstance(item, (list, tuple)):
        title = str(item[0]) if item else ""
        raw_topics = item[1] if len(item) > 1 else []
        topics = [str(path) for path in raw_topics] if isinstance(raw_topics, list) else [str(raw_topics)]
        path = topics[0] if topics else default_path
        detail = {"title": title, "topics": topics}
        message = f"duplicate title '{title}' appears in {len(topics)} topics"
        return make_issue(code, message, path, detail)
    path = item[0] if isinstance(item, (list, tuple)) and item else str(item)
    message = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else str(item)
    return make_issue(code, str(message), str(path or default_path), item)


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
        report.to_readme(), "",
        (
            "Full detail: [author-report.md](author-report.md) for authors; "
            "[author-report.json](author-report.json) for automation."
        ), "",
        "## CHM navigation", "",
    ]
    for result in chms:
        lines.append(f"### {result['chm']}")
        known_topics = {
            str(item).replace("\\", "/").casefold()
            for item in result.get("topics_paths", [])
        }
        for node in result.get("toc", []):
            if not node.get("title"):
                continue
            indent = "  " * max(0, int(node.get("depth", 1)) - 1)
            href = node.get("href", "")
            topic_path = Path(href.split("#", 1)[0]).as_posix()
            if href and topic_path.casefold() in known_topics:
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


def _build_locked(repo: Path, out: Path, work: Path, *, reuse: bool = False,
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
        report.add(make_issue("chm_discovery", "no repository-root CHM files found"))

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
                report.add(make_issue("destination_collision", f"CHM namespace collision: {stem}", chm.name))
                continue
            seen_names.add(stem.casefold())
            try:
                result = run_in_private_stage(chm, work, staging.path / "chm" / stem,
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
                        report.add(_report_issue(code, item, chm.name))
            except Exception as exc:  # noqa: BLE001 - isolate one corrupt CHM
                report.add(make_issue("chm_failure", f"{type(exc).__name__}: {exc}", chm.name))

        pdf_url = f"{source_repo.rstrip('/')}/blob/{source_ref}/{{path}}"
        try:
            pdf_result, _ = pdf_convert.run_in_private_stage(
                repo, staging.path / "pdf", update=False, source_url=pdf_url,
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
            report.add(make_issue(
                "source_replacement_character",
                f"source PDF replacement characters: {details}",
                str(source_rel), {
                    "source_pdf": str(source_rel),
                    "generated_markdown": emitted_rel,
                    "pages": details,
                },
            ))
        for code, items in pdf_report.items():
            if code == "pdf_source_replacements":
                continue
            for item in items:
                report.add(_report_issue(code, item))
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
        (staging.path / "author-report.md").write_text(
            "# Author quality report\n\nBuild validation is in progress.\n",
            encoding="utf-8",
        )
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
        (staging.path / "author-report.md").write_text(
            report.to_markdown(), encoding="utf-8"
        )
        if report.fatal:
            return {"report": report.as_dict(), "chms": chm_results,
                    "pdfs": pdf_result.get("converted", 0), "promoted": False}
        staging.promote()
    return {"report": report.as_dict(), "chms": chm_results,
            "pdfs": pdf_result.get("converted", 0), "promoted": True}


def _build(repo: Path, out: Path, work: Path, *, reuse: bool = False,
           limit: int = 0, source_ref: str = "develop",
           source_repo: str = DEFAULT_SOURCE_REPO,
           chm_source_url_base: str = DEFAULT_CHM_SOURCE_URL_BASE) -> dict:
    """Serialize one complete corpus mutation for cooperating exporters."""
    repo, out, work = Path(repo).resolve(), Path(out).resolve(), Path(work).resolve()
    validate_output_paths(out, work_dir=work, repo_root=repo, source_root=repo)
    with ExportLock(out):
        # The destination/link chain is checked again after lock acquisition,
        # immediately before staging and eventual promotion begin.
        validate_output_paths(out, work_dir=work, repo_root=repo, source_root=repo)
        return _build_locked(
            repo, out, work, reuse=reuse, limit=limit, source_ref=source_ref,
            source_repo=source_repo, chm_source_url_base=chm_source_url_base,
        )


def _path_overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_diagnostics_path(
    diagnostics: Path | str,
    *,
    repo: Path,
    out: Path,
    work: Path,
) -> Path:
    """Validate an external diagnostics destination before creating anything."""
    lexical = Path(os.path.abspath(os.fspath(Path(diagnostics).expanduser())))
    resolved = lexical.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise OutputPathError(f"refusing filesystem root as diagnostics: {resolved}")
    if lexical.is_symlink() or (lexical.exists() and not lexical.is_file()):
        raise OutputPathError(f"refusing non-regular diagnostics path: {lexical}")
    # Existing symlinked parents can redirect an apparently external path into
    # a protected tree.  Missing parents are created only after this check.
    current = lexical.parent
    while current != Path(current.anchor):
        if current.is_symlink() or (
            current.exists() and getattr(current, "is_junction", lambda: False)()
        ):
            raise OutputPathError(f"refusing symlink/junction in diagnostics parent: {current}")
        current = current.parent
    protected = {
        "repository": Path(repo).resolve(strict=False),
        "output": Path(out).resolve(strict=False),
        "work": Path(work).resolve(strict=False),
    }
    for label, root in protected.items():
        if _path_overlaps(resolved, root):
            raise OutputPathError(f"diagnostics must not overlap {label}: {resolved}")
    return resolved


def _sanitize_diagnostic_value(value: object, roots: dict[str, Path]) -> object:
    """Remove run-specific absolute paths while preserving report structure."""
    if isinstance(value, dict):
        return {key: _sanitize_diagnostic_value(item, roots) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_diagnostic_value(item, roots) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_diagnostic_value(item, roots) for item in value]
    if not isinstance(value, str):
        return value
    sanitized = value
    for label, root in sorted(roots.items(), key=lambda item: len(str(item[1])), reverse=True):
        for spelling in (str(root), str(root).replace("\\", "/")):
            sanitized = sanitized.replace(spelling, f"<{label}>")
    sanitized = re.sub(r"\.output-(?:stage|backup)-[0-9A-Za-z-]+", ".output-<temporary>", sanitized)
    return sanitized


def _write_diagnostics(path: Path, report: dict, *, roots: dict[str, Path]) -> None:
    """Atomically write a stable report without exposing staging filenames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stable_report = _sanitize_diagnostic_value(report, roots)
    payload = json.dumps(stable_report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def build(repo: Path, out: Path, work: Path, *, reuse: bool = False,
          limit: int = 0, source_ref: str = "develop",
          source_repo: str = DEFAULT_SOURCE_REPO,
          chm_source_url_base: str = DEFAULT_CHM_SOURCE_URL_BASE,
          diagnostics: Path | str | None = None) -> dict:
    """Build a corpus and optionally persist diagnostics outside generated trees."""
    repo_path, out_path, work_path = (
        Path(repo).resolve(), Path(out).resolve(), Path(work).resolve()
    )
    diagnostics_path = (
        _validate_diagnostics_path(
            diagnostics, repo=repo_path, out=out_path, work=work_path
        )
        if diagnostics is not None else None
    )
    try:
        result = _build(
            repo_path, out_path, work_path, reuse=reuse, limit=limit,
            source_ref=source_ref, source_repo=source_repo,
            chm_source_url_base=chm_source_url_base,
        )
    except Exception as exc:
        if diagnostics_path is not None:
            _write_diagnostics(
                diagnostics_path,
                Report([make_issue("unknown_issue", f"{type(exc).__name__}: {exc}")]).as_dict(),
                roots={"repo": repo_path, "out": out_path, "work": work_path},
            )
        raise
    if diagnostics_path is not None:
        _write_diagnostics(
            diagnostics_path,
            result["report"],
            roots={"repo": repo_path, "out": out_path, "work": work_path},
        )
    return result


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
    parser.add_argument("--diagnostics", default=None, type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    work = (args.work or repo / ".chm-work").resolve()
    result = build(repo, args.out.resolve(), work, reuse=args.reuse,
                   limit=args.limit, source_ref=args.source_ref,
                   source_repo=args.source_repo,
                   chm_source_url_base=args.chm_source_url_base,
                   diagnostics=args.diagnostics)
    print(Report(Issue(**{key: value for key, value in item.items()
                          if key in {"code", "message", "path", "fatal", "provenance", "detail"}})
                 for item in result["report"].get("issues", [])).to_console())
    return 1 if result["report"]["summary"]["fatal"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
