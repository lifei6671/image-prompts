import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import add_prompt


class GalleryParser(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.columns = []
        self.links = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "td":
            self.columns.append([])
        elif tag == "img":
            self.columns[-1].append(attrs)
        elif tag == "a":
            self.links.append(attrs["href"])


class GalleryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patched = patch.multiple(
            add_prompt, ROOT=self.root, PROMPTS_DIR=self.root / "prompts",
            README_PATH=self.root / "README.md", METADATA_PATH=self.root / "metadata.json",
        )
        patched.start()
        self.addCleanup(patched.stop)

    def metadata(self, record_id, title="示例", created_at="2026-09-11"):
        return dict(id=record_id, title=title, category="分类", tags=["手绘"],
                    model="ChatGPT Image", aspect_ratio="16:9", created_at=created_at)

    def seed(self, metadata):
        directory = self.root / "prompts" / metadata["id"]
        directory.mkdir(parents=True)
        add_prompt.write_record(directory / "index.md", metadata, "保留原始 Prompt")
        (directory / "preview.webp").write_bytes(b"unchanged preview")

    def readme(self):
        return (self.root / "README.md").read_text(encoding="utf-8")

    def test_empty_gallery(self):
        add_prompt.rebuild()
        self.assertIn("暂无素材。", self.readme())
        self.assertNotIn("<table>", self.readme())

    def test_two_continuous_columns_order_links_and_escaping(self):
        special = self.metadata("newest", '标题 <图> & "引用"', "2026-09-12")
        special.update(category="A & B", tags=["<tag>"], model="M <2>")
        for metadata in (self.metadata("older", "B"), special, self.metadata("middle", "A")):
            self.seed(metadata)
        before = {p: p.read_bytes() for p in (self.root / "prompts").rglob("*") if p.is_file()}
        add_prompt.rebuild()
        text = self.readme()
        gallery = GalleryParser(text)
        self.assertEqual([[image["src"] for image in column] for column in gallery.columns], [
            ["prompts/newest/preview.webp", "prompts/older/preview.webp"],
            ["prompts/middle/preview.webp"],
        ])
        self.assertEqual(gallery.columns[0][0]["alt"], special["title"])
        self.assertIn("A &amp; B", text)
        self.assertIn("&lt;tag&gt;", text)
        self.assertIn("M &lt;2&gt;", text)
        for record_id in ("newest", "middle", "older"):
            self.assertEqual(gallery.links.count(f"prompts/{record_id}/index.md"), 2)
        self.assertEqual(text.count("<tr>"), 1)
        for column in gallery.columns:
            for image in column:
                self.assertNotIn("height", image)
        add_prompt.rebuild()
        self.assertEqual(text, self.readme())
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_create_record_automatically_updates_gallery(self):
        image = self.root / "source.png"
        add_prompt.Image.new("RGB", (40, 60), "white").save(image)
        add_prompt.create_record(image, self.metadata("first"), "First prompt")
        first = self.root / "prompts" / "first"
        before = {p: p.read_bytes() for p in first.iterdir()}
        self.assertEqual([len(c) for c in GalleryParser(self.readme()).columns], [1, 0])
        add_prompt.create_record(image, self.metadata("second", created_at="2026-09-12"), "Second prompt")
        self.assertEqual([len(c) for c in GalleryParser(self.readme()).columns], [1, 1])
        self.assertIn("prompts/second/index.md", self.readme())
        self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
