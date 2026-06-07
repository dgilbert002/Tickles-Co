-- Phase B extension: disambiguate recall rows per armed signal source.
ALTER TABLE mem0_recall_log
    ADD COLUMN IF NOT EXISTS signal_source TEXT;

CREATE INDEX IF NOT EXISTS idx_mem0_recall_cid_source
    ON mem0_recall_log (correlation_id, actor_id, signal_source)
    WHERE correlation_id IS NOT NULL AND position_id IS NULL;
