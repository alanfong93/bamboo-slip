# Architecture

## Vault pointer index

The Markdown vault remains the source of truth. `scripts/index_vault.py` reads Markdown, VTT, and SRT files under `raw/` and Markdown files under `wiki/`, then stores one lightweight pointer per source in MemPalace. The pointer contains a relative path and a short excerpt, not the complete source.

The indexer scans and reads all expected source files before writing anything. An incomplete scan, unreadable source, or source-directory symlink stops the run before collection mutation. After a successful scan, it upserts changed pointers and optionally prunes missing pointers belonging to that vault and indexer.

Each indexed pointer records:

- `vault_id`: SHA-256 prefix of the normalized vault path
- `source_file`: vault-relative source path
- `content_hash`: SHA-256 of the original file bytes
- `index_version`: parser/index format version, to refresh pointers after parser changes
- `wing`, `room`, and `added_by`: MemPalace routing and ownership metadata

The pointer ID is derived from `vault_id` and `source_file`; the content hash is metadata, so content changes update the same pointer. A moved vault has a different identity. Legacy pointers without `vault_id` are left untouched rather than guessed to belong to a particular vault.

```mermaid
sequenceDiagram
  participant User
  participant Script as index_vault.py
  participant Vault as Markdown vault
  participant Palace as MemPalace
  User->>Script: Run --sync with vault and palace paths
  Script->>Vault: Validate raw/ and wiki/, scan supported sources
  Vault-->>Script: Source bytes, relative paths, excerpts, hashes
  alt Scan incomplete
    Script-->>User: Report errors; perform no pointer writes or pruning
  else Scan complete
    Script->>Palace: Read indexer-owned pointers for this vault
    Palace-->>Script: Existing pointer metadata
    Script->>Palace: Upsert changed pointers
    opt Pruning enabled and within safety limit
      Script->>Palace: Delete missing pointers for this vault only
    end
    Script-->>User: Report added, updated, skipped, and pruned counts
  end
```
