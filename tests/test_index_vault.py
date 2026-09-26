import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import index_vault


class FakeCollection:
    def __init__(self, records=None):
        self.records = records or {}
        self.upserts = []
        self.deletes = []

    def get(self, where=None):
        return {
            "ids": list(self.records),
            "metadatas": [self.records[doc_id]["metadata"] for doc_id in self.records],
        }

    def upsert(self, documents, ids, metadatas):
        self.upserts.append((documents, ids, metadatas))
        for document, doc_id, metadata in zip(documents, ids, metadatas):
            self.records[doc_id] = {"document": document, "metadata": metadata}

    def delete(self, ids):
        self.deletes.extend(ids)
        for doc_id in ids:
            self.records.pop(doc_id, None)


def make_vault(root: Path):
    (root / "raw").mkdir(parents=True)
    (root / "wiki").mkdir()


def metadata(vault_id, source_file, content_hash, index_version="2"):
    return {
        "wing": "vault",
        "room": "obsidian-map",
        "added_by": "index-vault",
        "vault_id": vault_id,
        "source_file": source_file,
        "content_hash": content_hash,
        "index_version": index_version,
    }


class ParseTests(unittest.TestCase):
    def test_parses_inline_and_block_tags(self):
        inline, _ = index_vault.parse_frontmatter(
            "---\ntitle: Example\ntags: [one, two]\n---\nBody"
        )
        block, _ = index_vault.parse_frontmatter(
            "---\ntitle: Example\ntags:\n  - one\n  - two\n---\nBody"
        )
        self.assertEqual(inline["tags"], ["one", "two"])
        self.assertEqual(block["tags"], ["one", "two"])

    def test_subtitle_excerpt_skips_cue_metadata(self):
        text = (
            "WEBVTT - Film\n\n"
            "NOTE commentary\nNot a spoken line\n\n"
            "STYLE\n::cue { color: yellow; }\n\n"
            "cue-id\n00:00:01.000 --> 00:00:02.000 align:start\n"
            "<c.voice>First line.</c>\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\nSecond &amp; final."
        )
        self.assertEqual(
            index_vault.subtitle_text(text), "First line. Second & final."
        )

    def test_empty_title_falls_back_and_inline_tag_comment_is_ignored(self):
        fm, _ = index_vault.parse_frontmatter(
            "---\ntitle:\ntags: [one, two] # topic tags\n---\nBody"
        )
        self.assertIsNone(fm["title"])
        self.assertEqual(fm["tags"], ["one", "two"])


class IndexingTests(unittest.TestCase):
    def test_parent_directory_names_do_not_trigger_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "docs" / ".local" / "scripts" / "vault"
            make_vault(root)
            (root / "raw" / "source.md").write_text("A source", encoding="utf-8")
            pointers = index_vault.scan_vault(str(root))
        self.assertEqual([p.rel_path for p in pointers], ["raw/source.md"])

    def test_vtt_and_srt_are_indexed_as_clean_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_vault(root)
            (root / "raw" / "talk.vtt").write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello world.",
                encoding="utf-8",
            )
            (root / "raw" / "film.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nA line.", encoding="utf-8"
            )
            pointers = index_vault.scan_vault(str(root))
        self.assertEqual({p.rel_path for p in pointers}, {"raw/talk.vtt", "raw/film.srt"})
        self.assertTrue(all("00:00:" not in p.document for p in pointers))

    def test_scan_failure_prevents_collection_changes_even_with_force_prune(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_vault(root)
            (root / "raw" / "unreadable.md").write_text("source", encoding="utf-8")
            collection = FakeCollection()
            with patch.object(index_vault, "read_source", side_effect=OSError("denied")):
                with self.assertRaises(index_vault.VaultScanError):
                    index_vault.index_vault(
                        str(root), "unused", "vault", "obsidian-map", True, True, True,
                        collection=collection,
                    )
        self.assertEqual(collection.upserts, [])
        self.assertEqual(collection.deletes, [])

    def test_missing_source_directory_prevents_collection_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "raw").mkdir()
            collection = FakeCollection()
            with self.assertRaises(index_vault.VaultScanError):
                index_vault.index_vault(
                    str(root), "unused", "vault", "obsidian-map", True, True, True,
                    collection=collection,
                )
        self.assertEqual(collection.upserts, [])
        self.assertEqual(collection.deletes, [])

    def test_junction_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_vault(root)
            target = root / "target"
            target.mkdir()
            raw_link = root / "raw" / "linked"
            raw_link.mkdir()
            real_check = index_vault.is_link_or_junction
            with patch.object(
                index_vault,
                "is_link_or_junction",
                side_effect=lambda path: str(path) == str(raw_link) or real_check(path),
            ):
                with self.assertRaises(index_vault.VaultScanError):
                    index_vault.scan_vault(str(root))

    def test_pointers_are_scoped_to_vault_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root_a = Path(temp) / "a"
            root_b = Path(temp) / "b"
            make_vault(root_a)
            make_vault(root_b)
            (root_a / "raw" / "same.md").write_text("A", encoding="utf-8")
            (root_b / "raw" / "same.md").write_text("B", encoding="utf-8")
            pointers_a = index_vault.scan_vault(str(root_a))
            pointers_b = index_vault.scan_vault(str(root_b))
            collection = FakeCollection()
            for root, pointers in ((root_a, pointers_a), (root_b, pointers_b)):
                index_vault.index_scanned_sources(
                    pointers, collection, str(root), "vault", "obsidian-map", False, False, False
                )
        self.assertEqual(len(collection.records), 2)
        self.assertNotEqual(
            index_vault.make_id(index_vault.vault_identity(str(root_a)), "raw/same.md"),
            index_vault.make_id(index_vault.vault_identity(str(root_b)), "raw/same.md"),
        )

    def test_sync_uses_content_hash_not_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault_id = index_vault.vault_identity(str(root))
            source = "raw/source.md"
            doc_id = index_vault.make_id(vault_id, source)
            old_hash = "old-content-hash"
            collection = FakeCollection({
                doc_id: {"metadata": metadata(vault_id, source, old_hash)}
            })
            pointer = index_vault.SourcePointer(source, "new-content-hash", "new content")
            index_vault.index_scanned_sources(
                [pointer], collection, str(root), "vault", "obsidian-map", True, False, False
            )
        self.assertEqual(len(collection.upserts), 1)
        self.assertEqual(collection.upserts[0][2][0]["content_hash"], "new-content-hash")

    def test_legacy_unscoped_pointers_are_not_pruned(self):
        legacy_id = "old-pointer"
        collection = FakeCollection({
            legacy_id: {"metadata": {"wing": "vault", "room": "obsidian-map", "added_by": "index-vault", "source_file": "raw/old.md"}}
        })
        index_vault.index_scanned_sources(
            [], collection, "C:/vault", "vault", "obsidian-map", True, True, True
        )
        self.assertEqual(collection.deletes, [])
        self.assertIn(legacy_id, collection.records)


if __name__ == "__main__":
    unittest.main()
