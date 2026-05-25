-- shared/intelligence/migrations/2026_05_24_round6_writer_registry_extension.sql
-- Round-6 sweep (CA2 #7, CA2 #8): extend the writer-registry allow-list
-- so the static-analysis gate (`shared/scripts/writer_registry_grep.py`)
-- covers tables that legitimate services have always written to but
-- weren't enumerated in the original Phase-10 seed.
--
-- - `position_monitor` writes `tracked_positions` (status transitions:
--   pending → open, open → closed, exits, expiries). Without this entry,
--   the gate either ignores the file (if path is unmapped) or flags every
--   monitor UPDATE as a violation.
-- - `surgeon_position_reconciler` writes `tracked_positions` (matches
--   shadow signal-positions to real fills). Same rationale.
-- - `media_items` is written by `interpretation_service` (claim → terminal
--   status) and by every collector (download → ready). Without this entry,
--   the gate silently ignores those writes.
--
-- This migration is forward-compatible: it merges new services into the
-- existing array via a SELECT-DISTINCT-UNNEST pattern so no service is
-- ever removed.

INSERT INTO public.table_writers (table_name, allowed_writer_services, notes)
VALUES
  ('tracked_positions',
   ARRAY['interpretation_service','surgeon2_trader','chart_hacker_trader',
         'postmortem_service','position_monitor','surgeon_position_reconciler']::TEXT[],
   'postmortem updates close fields only; position_monitor + surgeon_position_reconciler added in round-6'),
  ('media_items',
   ARRAY['interpretation_service','discord_collector','telegram_collector',
         'rss_collector','tradingview_monitor']::TEXT[],
   'interpretation owns claim/status; collectors write download rows')
ON CONFLICT (table_name) DO UPDATE
   SET allowed_writer_services = (
         SELECT ARRAY_AGG(DISTINCT x)
         FROM unnest(public.table_writers.allowed_writer_services
                     || EXCLUDED.allowed_writer_services) AS x
       ),
       notes = EXCLUDED.notes,
       updated_at = now();
