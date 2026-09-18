#!/usr/bin/env python3
"""Index raw/ and wiki/ markdown into MemPalace as title+tags+snippet pointers."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
TAGS_RE = re.compile(
    r"^tags:\s*[\[\n](.+?)(?:\]|\n(?=\S))", re.DOTALL | re.MULTILINE
)
TITLE_RE = re.compile(r"^title:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
INLINE_TAGS_RE = re.compile(r"(?:^|\s)#([\w/-]+)", re.MULTILINE)

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


def parse_frontmatter(content: str):
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    fm_text = m.group(1)
    rest = content[m.end() :]
    tags = []
    tm = TAGS_RE.search(fm_text)
    if tm:
        raw = tm.group(1)
        tags = [
            t.strip().strip("\"'").lstrip("-").strip()
            for t in re.split(r"[,\n]", raw)
            if t.strip()
        ]
    title = None
    titlem = TITLE_RE.search(fm_text)
    if titlem:
        title = titlem.group(1).strip()
    return {"title": title, "tags": [t for t in tags if t]}, rest


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


def make_id(rel_path: str) -> str:
    h = hashlib.md5(rel_path.encode()).hexdigest()[:16]
    safe = re.sub(r"[^a-z0-9_]", "_", rel_path.lower())[:40]
    return f"obsidian_{safe}_{h}"


def should_include(vault_root: str, path: str) -> bool:
    parts = os.path.normpath(path).split(os.sep)
    if any(part in EXCLUDE_DIRS or part.startswith(".") for part in parts):
        return False
    rel = os.path.relpath(path, vault_root)
    top = rel.split(os.sep)[0]
    return top in INCLUDE_DIRS


def index_vault(
    vault_root: str,
    palace_path: str,
    wing: str,
    room: str,
    sync_only: bool,
    prune: bool,
    force_prune: bool,
) -> None:
    try:
        from mempalace.palace import get_collection
    except ImportError:
        print(
            "MemPalace is not importable. Install it, then re-run. "
            "This script does not index by itself.",
            file=sys.stderr,
        )
        sys.exit(1)

    col = get_collection(palace_path, create=True)
    existing: dict[str, dict] = {}
    results = col.get(where={"$and": [{"wing": {"$eq": wing}}, {"room": {"$eq": room}}]})
    for i, doc_id in enumerate(results.get("ids", [])):
        meta = results["metadatas"][i]
        src = meta.get("source_file", "")
        entry = existing.setdefault(src, {"ids": [], "indexed_at": ""})
        entry["ids"].append(doc_id)
        entry["indexed_at"] = max(entry["indexed_at"], meta.get("filed_at", ""))
    if sync_only:
        print(f"Existing indexed notes: {len(existing)}")

    seen_paths: set[str] = set()
    new_count = updated_count = skipped_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for dirpath, dirnames, filenames in os.walk(vault_root):
        dirnames[:] = [
            d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            full_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(full_path, vault_root).replace("\\", "/")
            if not should_include(vault_root, full_path):
                skipped_count += 1
                continue
            seen_paths.add(rel_path)
            if sync_only and rel_path in existing:
                mtime = datetime.fromtimestamp(
                    os.path.getmtime(full_path), tz=timezone.utc
                ).isoformat()
                if existing[rel_path]["indexed_at"] >= mtime:
                    skipped_count += 1
                    continue
            try:
                with open(full_path, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError:
                skipped_count += 1
                continue
            fm, body = parse_frontmatter(content)
            title = fm.get("title") or os.path.splitext(fname)[0]
            tags = fm.get("tags", [])
            inline = INLINE_TAGS_RE.findall(body[:500])
            all_tags = list(dict.fromkeys(tags + inline))
            snippet = body.strip()[:300]
            doc = build_document(rel_path, title, all_tags, snippet)
            doc_id = make_id(rel_path)
            metadata = {
                "wing": wing,
                "room": room,
                "added_by": "index-vault",
                "source_file": rel_path,
                "filed_at": now,
                "chunk_index": 0,
            }
            col.upsert(documents=[doc], ids=[doc_id], metadatas=[metadata])
            if sync_only and rel_path in existing:
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
                col.delete(ids=ids[i : i + 500])
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
        description="Index raw/ and wiki/ into MemPalace. Not a file watcher."
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
    vault = os.path.abspath(args.vault)
    print(f"Indexing vault: {vault}")
    print(f"Palace: {args.palace}")
    print(
        f"Mode: {'sync (changed files only)' if args.sync else 'full index'}"
        f"{' | prune off' if args.no_prune else ''}"
    )
    print("MemPalace does not watch files. Re-run this after any vault write.")
    print()
    index_vault(
        vault_root=vault,
        palace_path=args.palace,
        wing=args.wing,
        room=args.room,
        sync_only=args.sync,
        prune=not args.no_prune,
        force_prune=args.force_prune,
    )


if __name__ == "__main__":
    main()
