#!/usr/bin/env python3
"""Index raw/ and wiki/ sources into MemPalace as lightweight pointers."""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
TITLE_RE = re.compile(r"^title:[ \t]*([^\r\n]*?)[ \t]*$", re.MULTILINE)
INLINE_TAGS_RE = re.compile(r"(?:^|\s)#([\w/-]+)", re.MULTILINE)
TAG_ITEM_RE = re.compile(r"^\s*-\s*(.*?)\s*$")
TIMESTAMP_RE = re.compile(
    r"^\s*(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->.*$"
)
INDEX_VERSION = "2"
INDEXER_NAME = "index-vault"
SUPPORTED_SUFFIXES = {".md", ".vtt", ".srt"}

INCLUDE_DIRS = {"raw", "wiki"}
EXCLUDE_DIRS = {
    ".obsidian",
    ".git",
    ".agents",
    ".claude",
    "skills",
    "scripts",
    "docs",
    "__pycache__",
}


@dataclass(frozen=True)
class SourcePointer:
    rel_path: str
    content_hash: str
    document: str


class VaultScanError(RuntimeError):
    """The vault could not be completely read, so indexing must not proceed."""


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    fm_text = m.group(1)
    rest = content[m.end() :]
    tags = parse_frontmatter_tags(fm_text)
    title = None
    titlem = TITLE_RE.search(fm_text)
    if titlem:
        title = titlem.group(1).strip().strip("\"'") or None
    return {"title": title, "tags": [t for t in tags if t]}, rest


def parse_frontmatter_tags(frontmatter: str) -> list[str]:
    """Parse common inline and block tag lists without requiring a YAML package."""
    lines = frontmatter.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^tags\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        value = match.group(1)
        if value.startswith("["):
            value = value[1:].split("]", 1)[0]
            value = re.sub(r"\s+#.*$", "", value)
            return [clean_tag(item) for item in value.split(",") if clean_tag(item)]
        if value:
            tag = clean_tag(value)
            return [tag] if tag else []

        tags = []
        for following in lines[index + 1 :]:
            if not following.strip():
                continue
            item = TAG_ITEM_RE.match(following)
            if item:
                tag = clean_tag(item.group(1))
                if tag:
                    tags.append(tag)
                continue
            if following[:1].isspace():
                continue
            break
        return tags
    return []


def clean_tag(value: str) -> str:
    return value.strip().strip("\"'").strip()


def build_document(rel_path: str, title: str, tags: list[str], snippet: str) -> str:
    tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
    parts = []
    if title:
        parts.append(f"Title: {title}")
    parts.append(f"Path: {rel_path}")
    if tag_str:
        parts.append(f"Tags: {tag_str}")
    if snippet:
        parts.append(snippet[:300])
    return " | ".join(parts)


def make_id(vault_id: str, rel_path: str) -> str:
    h = hashlib.sha256(f"{vault_id}\0{rel_path}".encode("utf-8")).hexdigest()[:24]
    safe = re.sub(r"[^a-z0-9_]", "_", rel_path.lower())[:40]
    return f"obsidian_{safe}_{h}"


def vault_identity(vault_root: str) -> str:
    normalized = os.path.normcase(os.path.realpath(vault_root))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def is_excluded_relative_path(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    return any(part in EXCLUDE_DIRS or part.startswith(".") for part in parts)


def read_source(path: str) -> bytes:
    return Path(path).read_bytes()


def subtitle_text(content: str) -> str:
    lines = []
    for block in re.split(r"\r?\n\s*\r?\n", content):
        block_lines = block.splitlines()
        if not block_lines:
            continue
        first = block_lines[0].strip()
        if first.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timestamp_index = next(
            (index for index, line in enumerate(block_lines) if TIMESTAMP_RE.match(line)),
            None,
        )
        if timestamp_index is None:
            continue
        for line in block_lines[timestamp_index + 1 :]:
            text = re.sub(r"<[^>]*>", "", line.strip())
            text = html.unescape(text).strip()
            if text:
                lines.append(text)
    return " ".join(lines)


def is_link_or_junction(path: str) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    return os.path.islink(path) or is_junction(path)


def scan_vault(vault_root: str) -> list[SourcePointer]:
    """Read all indexable files before any collection writes or pruning."""
    root = os.path.realpath(vault_root)
    if not os.path.isdir(root):
        raise VaultScanError(f"Vault directory does not exist or is not a directory: {vault_root}")
    missing_dirs = [name for name in INCLUDE_DIRS if not os.path.isdir(os.path.join(root, name))]
    if missing_dirs:
        raise VaultScanError(
            "Vault is incomplete; expected directories are missing: " + ", ".join(sorted(missing_dirs))
        )
    linked_roots = [name for name in INCLUDE_DIRS if is_link_or_junction(os.path.join(root, name))]
    if linked_roots:
        raise VaultScanError(
            "Refusing to scan symlinked source directories: " + ", ".join(sorted(linked_roots))
        )

    errors: list[str] = []
    pointers: list[SourcePointer] = []

    def on_walk_error(error: OSError) -> None:
        errors.append(str(error))

    for top in sorted(INCLUDE_DIRS):
        start = os.path.join(root, top)
        for dirpath, dirnames, filenames in os.walk(start, onerror=on_walk_error, followlinks=False):
            relative_dir = os.path.relpath(dirpath, root)
            if is_link_or_junction(dirpath):
                errors.append(f"Refusing to follow symlink directory: {relative_dir}")
                dirnames[:] = []
                continue
            kept_dirs = []
            for dirname in dirnames:
                relative = os.path.join(relative_dir, dirname)
                if is_excluded_relative_path(relative):
                    continue
                if is_link_or_junction(os.path.join(dirpath, dirname)):
                    errors.append(f"Refusing to follow symlink directory: {relative}")
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for filename in filenames:
                suffix = Path(filename).suffix.lower()
                if suffix not in SUPPORTED_SUFFIXES:
                    continue
                full_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(full_path, root).replace("\\", "/")
                if is_excluded_relative_path(rel_path):
                    continue
                if is_link_or_junction(full_path):
                    errors.append(f"Refusing to index symlink file: {rel_path}")
                    continue
                try:
                    raw_content = read_source(full_path)
                    content = raw_content.decode("utf-8-sig")
                except (OSError, UnicodeError) as error:
                    errors.append(f"Could not read {rel_path}: {error}")
                    continue

                fm, body = parse_frontmatter(content)
                if suffix in {".vtt", ".srt"}:
                    snippet = subtitle_text(body)
                else:
                    snippet = body.strip()
                title = fm.get("title") or Path(filename).stem
                tags = fm.get("tags", [])
                inline = INLINE_TAGS_RE.findall(snippet[:500])
                all_tags = list(dict.fromkeys(tags + inline))
                document = build_document(rel_path, title, all_tags, snippet)
                pointers.append(
                    SourcePointer(
                        rel_path=rel_path,
                        content_hash=hashlib.sha256(raw_content).hexdigest(),
                        document=document,
                    )
                )

    if errors:
        details = "\n".join(f"  - {error}" for error in errors[:20])
        if len(errors) > 20:
            details += f"\n  - ... and {len(errors) - 20} more error(s)"
        raise VaultScanError(
            f"Vault scan incomplete ({len(errors)} error(s)); no pointers were changed and pruning was skipped:\n{details}"
        )
    return pointers


def index_vault(
    vault_root: str,
    palace_path: str,
    wing: str,
    room: str,
    sync_only: bool,
    prune: bool,
    force_prune: bool,
    collection: Any | None = None,
) -> None:
    pointers = scan_vault(vault_root)
    if collection is None:
        try:
            from mempalace.palace import get_collection
        except ImportError:
            print(
                "MemPalace is not importable. Install it, then re-run. "
                "This script does not index by itself.",
                file=sys.stderr,
            )
            sys.exit(1)
        collection = get_collection(palace_path, create=True)

    index_scanned_sources(
        pointers=pointers,
        collection=collection,
        vault_root=vault_root,
        wing=wing,
        room=room,
        sync_only=sync_only,
        prune=prune,
        force_prune=force_prune,
    )


def index_scanned_sources(
    pointers: list[SourcePointer],
    collection: Any,
    vault_root: str,
    wing: str,
    room: str,
    sync_only: bool,
    prune: bool,
    force_prune: bool,
) -> None:
    vault_id = vault_identity(vault_root)
    base_filter = {"$and": [{"wing": {"$eq": wing}}, {"room": {"$eq": room}}]}
    results = collection.get(where=base_filter)
    existing: dict[str, dict[str, Any]] = {}
    legacy_count = 0
    result_ids = results.get("ids", [])
    result_metadata = results.get("metadatas") or [{} for _ in result_ids]
    for i, doc_id in enumerate(result_ids):
        meta = result_metadata[i] or {}
        if meta.get("added_by") == INDEXER_NAME and not meta.get("vault_id"):
            legacy_count += 1
        if meta.get("vault_id") != vault_id or meta.get("added_by") != INDEXER_NAME:
            continue
        src = meta.get("source_file", "")
        entry = existing.setdefault(src, {"ids": [], "content_hash": "", "index_version": ""})
        entry["ids"].append(doc_id)
        entry["content_hash"] = meta.get("content_hash", "")
        entry["index_version"] = meta.get("index_version", "")
    if sync_only:
        print(f"Existing indexed sources for this vault: {len(existing)}")
    if legacy_count:
        print(f"Leaving {legacy_count} legacy pointer(s) without a vault identity untouched.")

    now = datetime.now(timezone.utc).isoformat()
    new_count = updated_count = skipped_count = 0
    seen_paths = {pointer.rel_path for pointer in pointers}
    for pointer in pointers:
        old = existing.get(pointer.rel_path)
        if (
            sync_only
            and old
            and old["content_hash"] == pointer.content_hash
            and old["index_version"] == INDEX_VERSION
        ):
            skipped_count += 1
            continue
        metadata = {
            "wing": wing,
            "room": room,
            "added_by": INDEXER_NAME,
            "vault_id": vault_id,
            "source_file": pointer.rel_path,
            "content_hash": pointer.content_hash,
            "index_version": INDEX_VERSION,
            "filed_at": now,
            "chunk_index": 0,
        }
        collection.upsert(
            documents=[pointer.document],
            ids=[make_id(vault_id, pointer.rel_path)],
            metadatas=[metadata],
        )
        if old:
            updated_count += 1
        else:
            new_count += 1

    pruned_count = 0
    if prune:
        orphans = sorted(p for p in existing if p and p not in seen_paths)
        limit = max(25, int(len(existing) * 0.10))
        if len(orphans) > limit and not force_prune:
            print(
                f"\n!! Prune SKIPPED: {len(orphans)} orphans exceed the safety "
                f"limit of {limit}."
            )
            print("   Re-run with --force-prune only after checking the vault is complete.")
        elif orphans:
            ids = [i for p in orphans for i in existing[p]["ids"]]
            for i in range(0, len(ids), 500):
                collection.delete(ids=ids[i : i + 500])
            pruned_count = len(ids)
            print(f"\nPruned {pruned_count} stale pointer(s):")
            for p in orphans[:20]:
                print(f"  - {p}")
            if len(orphans) > 20:
                print(f"  ... and {len(orphans) - 20} more")

    print(
        f"\nDone. New: {new_count} | Updated: {updated_count} | "
        f"Skipped: {skipped_count} | Pruned: {pruned_count}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index raw/ and wiki/ sources into MemPalace. Not a file watcher."
    )
    parser.add_argument("--vault", required=True, help="Knowledge vault root")
    parser.add_argument(
        "--palace",
        required=True,
        help="MemPalace path, exactly as the server uses it",
    )
    parser.add_argument("--wing", default="vault")
    parser.add_argument("--room", default="obsidian-map")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Only update changed files",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Keep pointers whose note is gone",
    )
    parser.add_argument(
        "--force-prune",
        action="store_true",
        help="Prune even when orphan count trips the safety limit",
    )
    args = parser.parse_args()
    vault = os.path.realpath(os.path.abspath(args.vault))
    print(f"Indexing vault: {vault}")
    print(f"Palace: {args.palace}")
    print(
        f"Mode: {'sync (changed files only)' if args.sync else 'full index'}"
        f"{' | prune off' if args.no_prune else ''}"
    )
    print("MemPalace does not watch files. Re-run this after any vault write.")
    print()
    try:
        index_vault(
            vault_root=vault,
            palace_path=args.palace,
            wing=args.wing,
            room=args.room,
            sync_only=args.sync,
            prune=not args.no_prune,
            force_prune=args.force_prune,
        )
    except VaultScanError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
