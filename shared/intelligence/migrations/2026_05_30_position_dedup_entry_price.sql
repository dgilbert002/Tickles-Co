-- Allow multiple same-direction legs per signal (different entry prices).
ALTER TABLE public.tracked_positions
    DROP CONSTRAINT IF EXISTS uq_position_dedup;

ALTER TABLE public.tracked_positions
    ADD CONSTRAINT uq_position_dedup
    UNIQUE (news_item_id, trader_profile_id, instrument_symbol, direction, entry_price);
