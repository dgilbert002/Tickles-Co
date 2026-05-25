-- ROLLBACK for Round 10 prompt_version widen.
-- WARNING: rolling back will FAIL on any row whose prompt_version is >32 chars.
-- Run a UPDATE first to truncate or you'll error out.

-- Defensive: truncate any over-long values before shrinking the column.
UPDATE public.signal_interpretations
   SET prompt_version = LEFT(prompt_version, 32)
 WHERE LENGTH(prompt_version) > 32;

UPDATE public.signal_interpretations
   SET prefilter_provider = LEFT(prefilter_provider, 32)
 WHERE prefilter_provider IS NOT NULL AND LENGTH(prefilter_provider) > 32;

UPDATE public.signal_interpretations
   SET prefilter_result = LEFT(prefilter_result, 32)
 WHERE prefilter_result IS NOT NULL AND LENGTH(prefilter_result) > 32;

UPDATE public.signal_interpretations
   SET vision_provider = LEFT(vision_provider, 32)
 WHERE vision_provider IS NOT NULL AND LENGTH(vision_provider) > 32;

ALTER TABLE public.signal_interpretations
    ALTER COLUMN prompt_version     TYPE VARCHAR(32),
    ALTER COLUMN prefilter_provider TYPE VARCHAR(32),
    ALTER COLUMN prefilter_result   TYPE VARCHAR(32),
    ALTER COLUMN vision_provider    TYPE VARCHAR(32);
