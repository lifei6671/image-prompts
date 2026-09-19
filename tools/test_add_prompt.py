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
        self.assertEqual(add_prompt.load_catalog(), {"categories": [], "prompts": []})

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
        catalog = add_prompt.load_catalog()
        self.assertEqual(catalog["categories"], ["A & B", "分类"])
        self.assertEqual([item["id"] for item in catalog["prompts"]], ["newest", "middle", "older"])
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
        self.assertEqual([item["id"] for item in add_prompt.load_catalog()["prompts"]], ["second", "first"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_uploaded_document_is_preserved_except_for_preview_after_title(self):
        metadata = self.metadata("preserved", title="原始标题")
        document = "# 原始标题\n\n## Prompt\n\n```text\n# 代码块中的标题不受影响\n```\n"
        image = self.root / "source.png"
        add_prompt.Image.new("RGB", (40, 60), "white").save(image)

        directory = add_prompt.create_record(image, metadata, "ignored", uploaded_document=document)
        path = directory / "index.md"

        body = path.read_text(encoding="utf-8").split("---\n", 2)[2]
        self.assertEqual(
            body,
            "# 原始标题\n![预览图](preview.webp)\n\n## Prompt\n\n```text\n# 代码块中的标题不受影响\n```\n",
        )

    def test_preview_ignores_heading_like_text_in_a_code_fence(self):
        document = "```text\n# 这不是标题\n```\n\n# 实际标题\n"

        self.assertEqual(
            add_prompt.insert_preview_after_first_heading(document),
            "```text\n# 这不是标题\n```\n\n# 实际标题\n![预览图](preview.webp)\n",
        )

    def test_extract_prompt_unwraps_legacy_nested_document(self):
        metadata = self.metadata("legacy")
        legacy = add_prompt.record_document(
            metadata,
            "# 旧标题\n\n## Prompt\n\n```text\n保留的 Prompt 原文\n```",
        )

        self.assertEqual(add_prompt.extract_prompt_from_document(legacy), "保留的 Prompt 原文")

    def test_extract_prompt_handles_legacy_outer_fence_as_inner_closer(self):
        metadata = self.metadata("legacy-unclosed")
        legacy = add_prompt.record_document(
            metadata,
            "# 旧标题\n\n## Prompt\n\n```text\n保留这段缺少闭合围栏的 Prompt 正文。",
        )

        self.assertEqual(
            add_prompt.extract_prompt_from_document(legacy),
            "保留这段缺少闭合围栏的 Prompt 正文。",
        )

    def test_repair_records_normalizes_legacy_document_without_changing_prompt(self):
        metadata = self.metadata("legacy")
        self.seed(metadata)
        index_path = self.root / "prompts" / "legacy" / "index.md"
        index_path.write_text(
            add_prompt.record_document(metadata, "# 旧标题\n\n## Prompt\n\n```text\n保留的 Prompt 原文\n```") ,
            encoding="utf-8",
        )

        self.assertEqual(add_prompt.repair_records(), 1)
        self.assertEqual(index_path.read_text(encoding="utf-8"), add_prompt.record_document(metadata, "保留的 Prompt 原文"))
        self.assertEqual(add_prompt.prompt_details("legacy")["prompt"], "保留的 Prompt 原文")

    def test_ratio_accepts_adaptive_and_shows_it_in_web_form(self):
        self.assertEqual(add_prompt.validate_ratio("自适应"), "自适应")
        self.assertIn("16:9、自定义或自适应", add_prompt.add_form_page([]).decode("utf-8"))

    def test_catalog_page_embeds_filterable_prompt_metadata(self):
        catalog = {
            "categories": ["信息图"],
            "prompts": [self.metadata("sample", title="示例 Prompt")],
        }

        page = add_prompt.web_page(catalog).decode("utf-8")

        self.assertIn("全部素材", page)
        self.assertIn("标签（可组合）", page)
        self.assertIn('"id": "sample"', page)
        self.assertIn("/prompts/'+encodeURIComponent(item.id)+'/preview.webp", page)
        self.assertIn("IntersectionObserver", page)
        self.assertIn("#more[hidden]{display:none}", page)
        self.assertIn("more.hidden=shown>=filtered.length", page)
        self.assertIn('id="details"', page)
        self.assertIn('id="detail-restore"', page)
        self.assertIn("恢复默认", page)
        self.assertIn("复制 Prompt", page)
        self.assertIn("detailPrompt.value", page)
        self.assertIn("defaultPrompt=body.prompt", page)
        self.assertIn("const requestId=++detailRequest", page)
        self.assertIn("if(requestId!==detailRequest)return", page)
        self.assertIn("document.addEventListener('click'", page)
        self.assertIn("event.clientX<bounds.left", page)
        self.assertIn("/api/prompts/'+encodeURIComponent(item.id)", page)

    def test_ratio_rejects_other_text(self):
        with self.assertRaisesRegex(ValueError, "自定义.*自适应"):
            add_prompt.validate_ratio("任意比例")


if __name__ == "__main__":
    unittest.main()
