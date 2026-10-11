"""Exercise documentation drift and malformed translations in isolated copies."""

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_markdown as markdown


class DocumentationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="autojs6-plugin-index-docs-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in (".readme", ".changelog"):
            shutil.copytree(markdown.ROOT / name, self.root / name)
        shutil.copy2(markdown.ROOT / "official-repositories.json", self.root / "official-repositories.json")
        with contextlib.redirect_stdout(io.StringIO()):
            markdown.process_artifacts(self.root, markdown.build_artifacts(self.root), check=False)

    def change_json(self, relative, change):
        path = self.root / relative
        content = json.loads(path.read_text(encoding="utf-8"))
        change(content)
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def snapshot(self):
        return {path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def check(self):
        markdown.process_artifacts(self.root, markdown.build_artifacts(self.root), check=True)

    def test_check_is_read_only_and_only_readme_is_generated_at_root(self):
        before = self.snapshot()
        self.check()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(markdown.generated_inventory(self.root)), 21)
        readme = (self.root / ".readme/README-zh-Hans.md").read_bytes()
        self.assertEqual((self.root / "README.md").read_bytes(), readme)
        self.assertFalse((self.root / "README-zh-Hans.md").exists())
        self.assertFalse((self.root / "CHANGELOG.md").exists())

    def test_removed_root_aliases_are_rejected_as_orphans(self):
        for name in ("README-zh-Hans.md", "CHANGELOG.md"):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text("Obsolete root alias\n", encoding="utf-8")
                with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Orphan"):
                    self.check()
                path.unlink()

    def test_stale_and_missing_documents_fail_without_repairing_them(self):
        path = self.root / ".readme/README-fr.md"
        for exists in (True, False):
            with self.subTest(exists=exists):
                if exists:
                    path.write_text("Edited manually\n", encoding="utf-8")
                else:
                    path.unlink()
                before = self.snapshot()
                with self.assertRaisesRegex(markdown.MarkdownGenerationError, "stale:|missing:"):
                    self.check()
                self.assertEqual(before, self.snapshot())

    def test_orphans_fail_in_both_modes_before_any_write(self):
        (self.root / ".changelog/CHANGELOG-de.md").write_text("Old language\n", encoding="utf-8")
        before = self.snapshot()
        artifacts = markdown.build_artifacts(self.root)
        for check in (True, False):
            with self.subTest(check=check):
                with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Orphan"):
                    markdown.process_artifacts(self.root, artifacts, check)
                self.assertEqual(before, self.snapshot())

    def test_language_file_inventory_is_exact(self):
        path = self.root / ".readme/lang_de.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "extra=.*lang_de"):
            self.check()
        path.unlink()
        (self.root / ".changelog/lang_ar.json").unlink()
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "missing=.*lang_ar"):
            self.check()

    def test_missing_key_and_list_item_are_rejected(self):
        self.change_json(".readme/lang_fr.json", lambda data: data.pop("p_intro"))
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "keys, types, or list lengths"):
            self.check()
        shutil.copy2(markdown.ROOT / ".readme/lang_fr.json", self.root / ".readme/lang_fr.json")
        self.change_json(".readme/lang_fr.json", lambda data: data["runtime_field_descriptions"].pop())
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "keys, types, or list lengths"):
            self.check()

    def test_duplicate_json_keys_are_rejected(self):
        path = self.root / ".readme/common.json"
        path.write_text('{"repo_url": "one", "repo_url": "two"}', encoding="utf-8")
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Duplicate JSON key"):
            self.check()

    def test_escaped_non_ascii_punctuation_is_rejected_after_json_decode(self):
        path = self.root / ".readme/lang_en.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for punctuation in ("\uff0c", "\u2019", "\u2026", "\u060c", "\u061f", "\u3000", "\u2192"):
            with self.subTest(codepoint=f"U+{ord(punctuation):04X}"):
                data["p_intro"] = f"Text{punctuation}"
                path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
                with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Non-ASCII punctuation"):
                    self.check()

    def test_untranslated_marker_is_rejected(self):
        self.change_json(".readme/lang_ja.json", lambda data: data.update(p_intro="TODO_TRANSLATION"))
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Translation placeholder"):
            self.check()

    def test_missing_and_cyclic_template_references_are_rejected(self):
        path = ".readme/common.json"
        self.change_json(path, lambda data: data.update(license_url="{{ unknown_key }}"))
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Missing template value"):
            self.check()
        self.change_json(path, lambda data: data.update(license_url="{{ repo_url }}", repo_url="{{ license_url }}"))
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Cyclic template value"):
            self.check()

    def test_malformed_placeholder_in_template_is_rejected(self):
        path = self.root / ".readme/template_readme.md"
        path.write_text(path.read_text(encoding="utf-8") + "{{ not a valid key }}\n", encoding="utf-8")
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Unresolved placeholder"):
            self.check()

    def test_project_count_tracks_inventory_without_changing_index_data(self):
        index = self.root / "plugins.official.generated.json"
        index.write_text('{"sentinel": true}\n', encoding="utf-8")
        receipt = (index.read_bytes(), index.stat().st_mtime_ns)
        inventory = json.loads((self.root / "official-repositories.json").read_text(encoding="utf-8"))
        self.change_json("official-repositories.json", lambda data: data.append("AutoJs6-Plugin-Documentation-Test"))
        for code in markdown.LANGUAGE_CODES:
            self.change_json(f".readme/lang_{code}.json", lambda data: data.update(p_inventory="Count: {{ expected_project_count }}"))
        artifacts = markdown.build_artifacts(self.root)
        for code in markdown.LANGUAGE_CODES:
            self.assertIn(f"Count: {len(inventory) + 1}", artifacts[self.root / f".readme/README-{code}.md"])
        with contextlib.redirect_stdout(io.StringIO()):
            markdown.process_artifacts(self.root, artifacts, check=False)
        self.check()
        self.assertEqual(receipt, (index.read_bytes(), index.stat().st_mtime_ns))

    def test_duplicate_official_repositories_are_rejected(self):
        self.change_json("official-repositories.json", lambda data: data.append(data[0]))
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Duplicate official repository"):
            self.check()

    def test_changelog_date_must_match_its_heading(self):
        self.change_json(".changelog/lang_en.json", lambda data: data["$data"]["2026-10-12"].update(released_date="2026/10/13"))
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "released_date must match"):
            self.check()

    def test_changelog_order_must_match_and_be_newest_first(self):
        for code in markdown.LANGUAGE_CODES:
            self.change_json(f".changelog/lang_{code}.json", lambda data: data["$data"].update({"2026-10-11": {"released_date": "2026/10/11", "improvement": ["Earlier change"]}}))
        self.change_json(".changelog/lang_en.json", lambda data: data.update({"$data": dict(reversed(list(data["$data"].items())))}))
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "date order differs"):
            self.check()
        self.change_json(".changelog/lang_zh-Hans.json", lambda data: data.update({"$data": dict(reversed(list(data["$data"].items())))}))
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "newest first"):
            self.check()

    def test_invalid_calendar_date_is_rejected(self):
        for code in markdown.LANGUAGE_CODES:
            self.change_json(f".changelog/lang_{code}.json", lambda data: data.update({"$data": {"2026-02-30": {"released_date": "2026/02/30", "feature": ["Invalid date fixture"]}}}))
        with self.assertRaisesRegex(markdown.MarkdownGenerationError, "Invalid changelog date"):
            self.check()

    def test_generated_links_use_main_and_target_existing_files(self):
        for relative in ("LICENSE", "docs/native-page-alignment.md"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(markdown.ROOT / relative, target)
        common = json.loads((self.root / ".readme/common.json").read_text(encoding="utf-8"))
        prefix = common["repo_url"] + "/blob/main/"
        for path, output in markdown.build_artifacts(self.root).items():
            for url in re.findall(r"\]\(([^)]+)\)", output):
                if url.startswith(common["repo_url"]):
                    self.assertTrue(url.startswith(prefix), f"Wrong branch in {path}: {url}")
                    self.assertTrue((self.root / url.removeprefix(prefix)).is_file(), url)

    def test_admission_examples_are_valid_json_in_every_language(self):
        examples = []
        for code in markdown.LANGUAGE_CODES:
            text = (self.root / f".readme/README-{code}.md").read_text(encoding="utf-8")
            blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
            self.assertEqual(len(blocks), 1)
            examples.append(json.loads(blocks[0]))
        self.assertTrue(all(example == examples[0] for example in examples))
        self.assertEqual(examples[0]["schemaVersion"], 1)

    def test_check_cli_works_outside_repository_and_returns_failure_on_drift(self):
        script = self.root / ".python/generate_markdown.py"
        script.parent.mkdir()
        shutil.copy2(markdown.ROOT / ".python/generate_markdown.py", script)
        command = [sys.executable, str(script), "--check"]
        before = self.snapshot()
        result = subprocess.run(command, cwd=self.root.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MARKDOWN_OK languages=10 artifacts=21 mode=check", result.stdout)
        self.assertEqual(before, self.snapshot())
        (self.root / "README.md").write_text("Manual edit\n", encoding="utf-8")
        before = self.snapshot()
        result = subprocess.run(command, cwd=self.root.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("MARKDOWN_ERROR", result.stderr)
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
