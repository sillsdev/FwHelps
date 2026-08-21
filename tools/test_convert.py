import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chm_convert
import convert


class ConvertOrchestrationTests(unittest.TestCase):
    def test_discovers_all_root_chms_in_case_insensitive_order(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "Using_Help.chm").write_bytes(b"")
            (repo / "FieldWorks_Language_Explorer_Help.chm").write_bytes(b"")
            (repo / "nested").mkdir()
            (repo / "nested" / "ignored.chm").write_bytes(b"")
            self.assertEqual(
                ["FieldWorks_Language_Explorer_Help.chm", "Using_Help.chm"],
                [p.name for p in convert.discover_chms(repo)],
            )

    def test_source_url_template_contains_source_ref_for_pdf_call(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "Using_Help.chm").write_bytes(b"")
            with mock.patch.object(convert, "convert_chm", return_value={
                     "chm": "Using_Help.chm", "stem": "Using_Help", "toc": [],
                     "topics": 0, "images": 0, "report": {},
                 }) as chm, \
                 mock.patch.object(convert.pdf_convert, "run", return_value=(
                     {"converted": 0, "report": {}}, []
                 )) as pdf:
                result = convert.build(repo, repo / "export", repo / "work", source_ref="feature/x")
            self.assertEqual(1, chm.call_count)
            self.assertTrue(pdf.call_args.kwargs["source_url"].startswith(
                "https://github.com/sillsdev/FwHelps/blob/feature/x/"
            ))
            self.assertIn("report", result)
            self.assertEqual(1, result["report"]["corpus"]["chm_count"])

    def test_two_chm_fixture_is_emitted_under_separate_namespaces(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            for name in ("FieldWorks_Language_Explorer_Help.chm", "Using_Help.chm"):
                (repo / name).write_bytes(b"fixture")

            def fake_chm(chm, _work, destination, **_kwargs):
                (destination / "index.md").parent.mkdir(parents=True, exist_ok=True)
                (destination / "index.md").write_text(f"# {chm.stem}\n", encoding="utf-8")
                return {"chm": chm.name, "stem": destination.name, "toc": [],
                        "topics": 1, "images": 0, "report": {}}

            with mock.patch.object(convert, "convert_chm", side_effect=fake_chm), \
                 mock.patch.object(convert.pdf_convert, "run", return_value=(
                     {"converted": 0, "report": {}}, []
                 )):
                result = convert.build(repo, repo / "export", repo / "work")
            self.assertTrue(result["promoted"])
            self.assertTrue((repo / "export" / "chm" / "Using_Help" / "index.md").exists())
            self.assertTrue((repo / "export" / "chm" / "FieldWorks_Language_Explorer_Help" / "index.md").exists())
            readme = (repo / "export" / "README.md").read_text(encoding="utf-8")
            self.assertIn("Root CHMs (auto-discovered)", readme)
            self.assertIn("FieldWorks_Language_Explorer_Help.chm", readme)
            self.assertIn("Using_Help.chm", readme)

    def test_direct_chm_conversion_preserves_related_images_and_disambiguates_titles(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = root / "Using_Help.chm"
            chm.write_bytes(b"fixture")

            def fake_extract(_chm, extraction):
                extraction.mkdir(parents=True)
                (extraction / "a.htm").write_text(
                    "<title>Same</title><h2>First heading</h2>"
                    "<h3>Related Topics</h3><a href='b.htm'>Other</a>"
                    "<img src='pic.png'>", encoding="cp1252"
                )
                (extraction / "b.htm").write_text(
                    "<title>Same</title><h2>Second heading</h2>", encoding="cp1252"
                )
                (extraction / "pic.png").write_bytes(b"png")
                (extraction / "book.hhc").write_text(
                    '<ul><li><object><param name="Name" value="A">'
                    '<param name="Local" value="a.htm"></object></li></ul>', encoding="cp1252"
                )

            with mock.patch.object(chm_convert, "run_pandoc", return_value=(
                "# source\n\n- - child\n", []
            )):
                result = chm_convert.convert_chm(chm, root / "work", root / "out",
                                                 extractor=fake_extract,
                                                 source_url_base="https://docs.example/help")
            a = (root / "out" / "a.md").read_text(encoding="utf-8")
            b = (root / "out" / "b.md").read_text(encoding="utf-8")
            self.assertIn("# First heading", a)
            self.assertIn("# Second heading", b)
            self.assertIn("related:", a)
            self.assertIn(
                'source_url: "https://docs.example/help/index.htm#t=a.htm"', a
            )
            self.assertIn("b.md", a)
            self.assertNotIn("- -", a)
            self.assertIn("child", a)
            self.assertEqual(b"png", (root / "out" / "pic.png").read_bytes())
            self.assertEqual(2, result["topics"])

    def test_failed_validation_preserves_previous_output_and_stage_is_cleaned(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "Using_Help.chm").write_bytes(b"fixture")
            out, work = repo / "export", repo / "work"
            out.mkdir()
            (out / "sentinel.txt").write_text("previous", encoding="utf-8")

            def bad_chm(_chm, _work, destination, **_kwargs):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "bad.md").write_text("# One\n# Two\n", encoding="utf-8")
                return {"chm": "Using_Help.chm", "stem": "Using_Help", "toc": [],
                        "topics": 1, "images": 0, "report": {}}

            with mock.patch.object(convert, "convert_chm", side_effect=bad_chm), \
                 mock.patch.object(convert.pdf_convert, "run", return_value=(
                     {"converted": 0, "report": {}}, []
                 )):
                result = convert.build(repo, out, work)
            self.assertFalse(result["promoted"])
            self.assertEqual("previous", (out / "sentinel.txt").read_text(encoding="utf-8"))
            self.assertTrue(not work.exists() or not any(
                path.name.startswith(".output-stage-") for path in work.iterdir()
            ))

    def test_broken_readme_navigation_is_a_fatal_validation_issue(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "Using_Help.chm").write_bytes(b"fixture")
            out = repo / "export"

            def bad_nav(_chm, _work, destination, **_kwargs):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "index.md").write_text("# Page\n", encoding="utf-8")
                return {"chm": "Using_Help.chm", "stem": "Using_Help", "toc": [{
                    "title": "Missing", "href": "missing.htm", "depth": 1,
                }], "topics": 1, "images": 0, "topics_paths": ["missing.htm"], "report": {}}

            with mock.patch.object(convert, "convert_chm", side_effect=bad_nav), \
                 mock.patch.object(convert.pdf_convert, "run", return_value=(
                     {"converted": 0, "report": {}}, []
                 )):
                result = convert.build(repo, out, repo / "work")
            self.assertFalse(result["promoted"])
            self.assertTrue(any(issue["code"] == "missing_link" for issue in result["report"]["issues"]))

    def test_stale_toc_entry_is_safe_text_and_source_advisory(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "Using_Help.chm").write_bytes(b"fixture")

            def stale(_chm, _work, destination, **_kwargs):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "index.md").write_text("# Page\n", encoding="utf-8")
                return {"chm": "Using_Help.chm", "stem": "Using_Help", "toc": [{
                    "title": "About Strata Sequences", "href": "About_Strata_Sequences.htm", "depth": 1,
                }], "topics": 1, "images": 0, "topics_paths": ["index.htm"],
                        "report": {"stale_toc_entries": [["About_Strata_Sequences.htm", "missing"]]}}

            with mock.patch.object(convert, "convert_chm", side_effect=stale), \
                 mock.patch.object(convert.pdf_convert, "run", return_value=(
                     {"converted": 0, "report": {}}, []
                 )):
                result = convert.build(repo, repo / "export", repo / "work")
            self.assertTrue(result["promoted"])
            readme = (repo / "export" / "README.md").read_text(encoding="utf-8")
            self.assertIn("- **About Strata Sequences**", readme)
            self.assertNotIn("About_Strata_Sequences.htm)", readme)
            stale_issues = [issue for issue in result["report"]["issues"]
                            if issue["code"] == "stale_toc_entries"]
            self.assertTrue(stale_issues)

    def test_readme_renders_qualified_chm_href_as_safe_text(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "Using_Help.chm").write_bytes(b"fixture")

            def qualified(_chm, _work, destination, **_kwargs):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "index.md").write_text("# Page\n", encoding="utf-8")
                return {"chm": "Using_Help.chm", "stem": "Using_Help", "toc": [{
                    "title": "Qualified", "href": "Using_Help.chm::/Using_Help.hhc", "depth": 1,
                }], "topics": 1, "images": 0, "topics_paths": ["index.htm"], "report": {}}

            with mock.patch.object(convert, "convert_chm", side_effect=qualified), \
                 mock.patch.object(convert.pdf_convert, "run", return_value=(
                     {"converted": 0, "report": {}}, []
                 )):
                result = convert.build(repo, repo / "export", repo / "work")
            self.assertTrue(result["promoted"])
            readme = (repo / "export" / "README.md").read_text(encoding="utf-8")
            self.assertIn("- **Qualified**", readme)
            self.assertNotIn("Using_Help.chm%3A%3A", readme)

    def test_repeated_fixture_builds_have_identical_emitted_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "Using_Help.chm").write_bytes(b"fixture")

            def fake_chm(chm, _work, destination, **_kwargs):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "index.md").write_text(f"# {chm.stem}\n", encoding="utf-8")
                return {"chm": chm.name, "stem": destination.name, "toc": [],
                        "topics": 1, "images": 0, "report": {}}

            with mock.patch.object(convert, "convert_chm", side_effect=fake_chm), \
                 mock.patch.object(convert.pdf_convert, "run", return_value=(
                     {"converted": 0, "report": {}}, []
                 )):
                first = convert.build(repo, repo / "export", repo / "work")
                files = sorted(path.relative_to(repo / "export").as_posix() for path in (repo / "export").rglob("*") if path.is_file())
                before = {name: (repo / "export" / name).read_bytes() for name in files}
                second = convert.build(repo, repo / "export", repo / "work")
                after = {name: (repo / "export" / name).read_bytes() for name in files}
            self.assertTrue(first["promoted"] and second["promoted"])
            self.assertEqual(before, after)
            self.assertFalse((repo / ".export.staging").exists())

    def test_reuse_revalidates_and_preserves_extraction_advisory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = root / "Using_Help.chm"
            chm.write_bytes(b"fixture")
            extraction = root / "work" / "Using_Help"
            extraction.mkdir(parents=True)
            (extraction / "topic.htm").write_text("<title>Topic</title><h2>Topic</h2>", encoding="cp1252")
            (extraction / "book.hhc").write_text(
                '<param name="Local" value="missing.htm">', encoding="cp1252"
            )
            with mock.patch.object(chm_convert, "run_pandoc", return_value=("# Topic\n", [])):
                result = chm_convert.convert_chm(
                    chm, root / "work", root / "out", reuse=True
                )
            self.assertTrue(result["report"]["stale_toc_entries"])

    def test_chm_conversion_discovers_htm_and_html_topics_case_insensitively(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = root / "Using_Help.chm"
            chm.write_bytes(b"fixture")

            def fake_extract(_chm, extraction):
                extraction.mkdir(parents=True)
                (extraction / "one.HTML").write_text("<title>One</title><h2>One</h2>", encoding="cp1252")
                (extraction / "two.htm").write_text("<title>Two</title><h2>Two</h2>", encoding="cp1252")

            with mock.patch.object(chm_convert, "run_pandoc", return_value=("# body\n", [])):
                result = chm_convert.convert_chm(chm, root / "work", root / "out", extractor=fake_extract)
            self.assertEqual(2, result["topics"])
            self.assertTrue((root / "out" / "one.md").exists())
            self.assertTrue((root / "out" / "two.md").exists())

    def test_chm_conversion_records_exact_source_replacement_provenance(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = root / "Using_Help.chm"
            chm.write_bytes(b"fixture")

            def fake_extract(_chm, extraction):
                extraction.mkdir(parents=True)
                (extraction / "source.htm").write_bytes(
                    b"<title>Source</title><h2>Source</h2>\x81"
                )
                (extraction / "clean.htm").write_text(
                    "<title>Clean</title><h2>Clean</h2>", encoding="cp1252"
                )

            def fake_pandoc(source, _tmp):
                title = "Source" if "Source" in source else "Clean"
                return (f"# {title}\n\ncontains �\n" if "\ufffd" in source else "# Clean\n", [])

            with mock.patch.object(chm_convert, "run_pandoc", side_effect=fake_pandoc):
                result = chm_convert.convert_chm(
                    chm, root / "work", root / "out", extractor=fake_extract
                )
            self.assertEqual(["source.htm"], result["source_replacement_paths"])

    def test_chm_source_normalizes_robohelp_cp1252_nbsp_before_pandoc(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = root / "Using_Help.chm"
            chm.write_bytes(b"fixture")
            seen = []

            def fake_extract(_chm, extraction):
                extraction.mkdir(parents=True)
                (extraction / "topic.htm").write_text(
                    "<title>Topic</title><h2>Topic</h2>"
                    "<p>word\u00a0boundary</p><td>\u00a0</td>"
                    "<p class='TypedText'>K\u00a0</p><span class=Keyboard>&nbsp;</span>"
                    "<p>entity&nbsp;boundary&#160;again</p>",
                    encoding="cp1252",
                )

            def fake_pandoc(source, _tmp):
                seen.append(source)
                return ("# Topic\n", [])

            with mock.patch.object(chm_convert, "run_pandoc", side_effect=fake_pandoc):
                chm_convert.convert_chm(chm, root / "work", root / "out", extractor=fake_extract)
            self.assertEqual(1, len(seen))
            self.assertNotIn("\u00a0", seen[0])
            self.assertNotIn("&nbsp;", seen[0])
            self.assertNotIn("\ufffd", seen[0])
            self.assertIn("word boundary", seen[0])
            self.assertIn("entity boundary again", seen[0])

    def test_fwhelp_declares_robohelp_presentation_classes_and_note_marker(self):
        lua = Path(convert.__file__).with_name("fwhelp.lua").read_text(encoding="utf-8")
        for name in ("hcp1", "hcp2", "hcp3", "hcp4"):
            self.assertIn(f"{name}          = \"plain\"", lua)
        self.assertIn('note[_-]?icon%.gif', lua)

    def test_build_reports_allowlisted_source_replacement_as_advisory(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "Using_Help.chm").write_bytes(b"fixture")

            def source_chm(_chm, _work, destination, **_kwargs):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "source.md").write_text("# Source\n\ufffd\n", encoding="utf-8")
                return {"chm": "Using_Help.chm", "stem": "Using_Help", "toc": [],
                        "topics": 1, "images": 0, "topics_paths": ["source.htm"],
                        "source_replacement_paths": ["source.htm"], "report": {}}

            with mock.patch.object(convert, "convert_chm", side_effect=source_chm), \
                 mock.patch.object(convert.pdf_convert, "run", return_value=(
                     {"converted": 0, "report": {}}, []
                 )):
                result = convert.build(repo, repo / "export", repo / "work")
            self.assertTrue(result["promoted"])
            replacement = [issue for issue in result["report"]["issues"]
                           if issue["code"] == "replacement_character"]
            self.assertEqual(1, len(replacement))
            self.assertFalse(replacement[0]["fatal"])

    def test_pdf_replacement_provenance_maps_to_emitted_path_and_export_is_fatal(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "Using_Help.chm").write_bytes(b"fixture")

            def clean_chm(_chm, _work, destination, **_kwargs):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "index.md").write_text("# Page\n", encoding="utf-8")
                return {"chm": "Using_Help.chm", "stem": "Using_Help", "toc": [],
                        "topics": 1, "images": 0, "report": {}}

            pdf_result = {"converted": 2, "report": {
                "pdf_source_replacements": [[
                    "docs/Guide With Spaces.pdf",
                    [{"page": 3, "count": 1, "codepoints": ["U+001F"]}],
                ]],
                "pdf_export_replacements": [[
                    "docs/Generated.pdf", {"source_count": 0, "exporter_count": 1},
                ]],
            }}
            with mock.patch.object(convert, "convert_chm", side_effect=clean_chm), \
                 mock.patch.object(convert.pdf_convert, "run", return_value=(pdf_result, [])):
                result = convert.build(repo, repo / "export", repo / "work")
            source = [issue for issue in result["report"]["issues"]
                      if issue["code"] == "replacement_character"
                      and issue["path"] == "pdf/docs/Guide_With_Spaces.md"]
            self.assertEqual(1, len(source))
            self.assertFalse(source[0]["fatal"])
            self.assertEqual("source", source[0]["provenance"])
            exporter = [issue for issue in result["report"]["issues"]
                        if issue["path"] == "docs/Generated.pdf"]
            self.assertTrue(exporter)
            self.assertTrue(exporter[0]["fatal"])
            self.assertFalse(result["promoted"])

    @unittest.skipUnless(shutil.which("pandoc"), "pandoc is required")
    def test_pandoc_lua_keeps_nested_lists_and_parenthesized_image_targets(self):
        html = (
            "<h2>Topic</h2><ul><li>Parent<ul><li>Child</li></ul></li></ul>"
            "<p><img src='images/a (1).png'></p>"
        )
        result = subprocess.run(
            ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none",
             f"--lua-filter={Path(convert.__file__).with_name('fwhelp.lua')}"],
            input=html, text=True, encoding="utf-8", capture_output=True, check=True,
        )
        self.assertNotIn("- -", result.stdout)
        self.assertIn("Child", result.stdout)
        self.assertTrue("images/a%20(1).png" in result.stdout or "images/a\\ (1).png" in result.stdout)

    @unittest.skipUnless(shutil.which("pandoc"), "pandoc is required")
    def test_pandoc_lua_drops_decorative_note_icon_but_keeps_normal_images(self):
        html = (
            "<h4><img alt='' src='../Note_Icon.gif'/> Tip</h4>"
            "<p><img src='images/check.png'></p>"
        )
        result = subprocess.run(
            ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none",
             f"--lua-filter={Path(convert.__file__).with_name('fwhelp.lua')}"],
            input=html, text=True, encoding="utf-8", capture_output=True, check=True,
        )
        self.assertNotIn("\ufffd", result.stdout)
        self.assertIn("images/check.png", result.stdout)
        self.assertIn("[!TIP]", result.stdout)


if __name__ == "__main__":
    unittest.main()
