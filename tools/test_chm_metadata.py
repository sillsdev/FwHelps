import tempfile
import unittest
from pathlib import Path

from chm_metadata import TopicMeta, parse_toc, safe_stem


class ChmMetadataTests(unittest.TestCase):
    def test_safe_stem_is_stable_and_filesystem_safe(self):
        self.assertEqual("Using_Help", safe_stem("Using Help.chm"))
        self.assertEqual("FieldWorks_Language_Explorer_Help", safe_stem("FieldWorks_Language_Explorer_Help.chm"))

    def test_topic_meta_collects_heading_links_images_and_related_links(self):
        meta = TopicMeta()
        meta.feed("""<html><title>Title</title><body>
        <h2>Reader heading</h2><a href='next.htm'>Next</a>
        <a href='https://example.test'>Site</a><img src='img/a.png'>
        <h3>Related Topics</h3><a href='other.htm'>Other</a>
        </body></html>""")
        self.assertEqual("Title", meta.title)
        self.assertEqual("Reader heading", meta.page_heading)
        self.assertEqual(["next.htm", "https://example.test", "other.htm"], meta.links)
        self.assertEqual(["img/a.png"], meta.images)
        self.assertEqual([("Other", "other.htm")], meta.related)

    def test_parse_toc_returns_deterministic_breadcrumbs(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "book.hhc"
            path.write_text(
                '<ul><li><object><param name="Name" value="Root">'
                '<param name="Local" value="root.htm"></object><ul>'
                '<li><object><param name="Name" value="Child">'
                '<param name="Local" value="child.htm"></object></ul></li></ul>',
                encoding="cp1252",
            )
            nodes = parse_toc(path)
        self.assertEqual(["Root"], nodes[0]["breadcrumb"])
        self.assertEqual(["Root", "Child"], nodes[1]["breadcrumb"])

    def test_parse_toc_accepts_reversed_casefolded_param_attributes_and_escaped_local(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "book.hhc"
            path.write_text(
                "<ul><li><object><param VALUE='Texts_&amp;_Words.htm' data-x='1' "
                "NaMe='nAmE'><param value='Texts_%26_Words.htm' name='LOCAL'>"
                "</object></li></ul>", encoding="cp1252"
            )
            nodes = parse_toc(path)
        self.assertEqual("Texts_&_Words.htm", nodes[0]["href"])


if __name__ == "__main__":
    unittest.main()
