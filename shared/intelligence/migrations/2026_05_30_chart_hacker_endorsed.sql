-- ChartHacker agreed with a trader leg — one book row, CH copy agents still mirror.
ALTER TABLE tracked_positions
    ADD COLUMN IF NOT EXISTS chart_hacker_endorsed BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_tp_ch_endorsed
    ON tracked_positions (chart_hacker_endorsed)
    WHERE chart_hacker_endorsed = TRUE;
