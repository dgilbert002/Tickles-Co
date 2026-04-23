-- Phase M7: Paper trading contests
--
-- A contest groups multiple agents competing with paper wallets.
-- Leaderboards are calculated from the fills and positions associated
-- with the contest's paper wallets.
--
BEGIN;

CREATE TABLE IF NOT EXISTS public.contests (
    id                   TEXT         PRIMARY KEY,
    name                 TEXT         NOT NULL,
    venues               TEXT[]       NOT NULL,
    coins                TEXT[]       NOT NULL,
    starting_balance_usd NUMERIC(20,8) NOT NULL,
    status               TEXT         NOT NULL DEFAULT 'active', -- active, ended
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ends_at              TIMESTAMPTZ  NOT NULL,
    metadata             JSONB        NOT NULL DEFAULT '{}'::JSONB
);

CREATE TABLE IF NOT EXISTS public.contest_participants (
    contest_id           TEXT         NOT NULL REFERENCES public.contests(id) ON DELETE CASCADE,
    company_id           TEXT         NOT NULL,
    agent_id             TEXT         NOT NULL,
    strategy_ref         TEXT,
    joined_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contest_id, company_id, agent_id)
);

CREATE INDEX IF NOT EXISTS contests_status_idx ON public.contests (status, ends_at);
CREATE INDEX IF NOT EXISTS contest_participants_agent_idx ON public.contest_participants (company_id, agent_id);

COMMIT;

-- =====================================================================
-- ROLLBACK:
-- BEGIN;
-- DROP TABLE IF EXISTS public.contest_participants;
-- DROP TABLE IF EXISTS public.contests;
-- COMMIT;
