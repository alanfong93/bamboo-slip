# System flow

After any write under `raw/` or `wiki/`, the user runs `index-vault`. The script reads the full source set before touching MemPalace. A scan failure stops the run without changing pointers. A successful scan refreshes changed pointers and prunes missing pointers only within the same vault identity.

```mermaid
flowchart TD
  Write[Write or change a source in raw/ or wiki/]
  Run[Run index-vault with the exact MemPalace path]
  Validate[Validate vault root and expected source directories]
  Scan[Read Markdown, VTT, and SRT sources; compute hashes and excerpts]
  Complete{Was the scan complete?}
  Error[Report errors; leave MemPalace unchanged]
  Load[Load indexer pointers for this vault identity]
  Compare[Compare content hash and index version]
  Upsert[Upsert new or changed pointers]
  Prune{Pruning enabled and under safety limit?}
  Delete[Delete missing pointers for this vault only]
  Done[Report indexing totals]

  Write --> Run --> Validate --> Scan --> Complete
  Complete -->|No| Error
  Complete -->|Yes| Load --> Compare --> Upsert --> Prune
  Prune -->|Yes| Delete --> Done
  Prune -->|No| Done
```
