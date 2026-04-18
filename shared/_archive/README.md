# _archive/

Superseded code and one-shot scripts. **Never edited in place. Never
deleted.**

## Structure

Subfolders named `YYYY-MM-DD_<reason>/`:

- `YYYY-MM-DD` — date the archive was created.
- `<reason>` — short kebab-case reason, e.g. `phase1a-scaffold`,
  `treasury-replaces-legacy-db-keys`, `janitor-run-2026-05-01`.

Each subfolder contains a `MANIFEST.md` explaining:

- What moved here
- From where
- Why
- Who moved it (agent id / human)
- What replaced it (if anything)
- How to restore if needed (`git mv _archive/...` path back to original)

## Rules

1. Nothing in `_archive/` is ever re-imported from live code. If you find
   yourself reaching in here, you're doing it wrong — port the logic fresh
   to its new feature folder instead.
2. Anything moved here must be moved with `git mv` so history is preserved.
3. The Janitor agent (`agents/janitor.py`) is the primary writer to
   `_archive/`. Humans can also move files here manually; if they do, they
   write the `MANIFEST.md` themselves.
4. `_archive/` is **never** purged or compressed. Disk is cheap, context is
   precious.
