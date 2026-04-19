-- Phase 36: Owner Dashboard + Telegram OTP + Mobile.
--
-- Three tables back the owner-facing dashboard:
--
--   dashboard_users     : allowlist of Telegram chat_ids that may
--                         request OTPs. We never trust the chat_id
--                         alone - the OTP has to be delivered to
--                         that chat and echoed back.
--   dashboard_otps      : one-time codes (hashed) with an expiry and
--                         single-use marker. A code is consumed the
--                         moment a matching verify succeeds.
--   dashboard_sessions  : signed session tokens (hashed) issued
--                         after a successful verify. Short-lived by
--                         default (12h) and revocable.
--
-- Ownership: tickles_shared.

BEGIN;

CREATE TABLE IF NOT EXISTS public.dashboard_users (
    id             BIGSERIAL PRIMARY KEY,
    chat_id        TEXT NOT NULL UNIQUE,
    display_name   TEXT,
    role           TEXT NOT NULL DEFAULT 'owner',   -- owner | viewer
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS public.dashboard_otps (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      TEXT NOT NULL REFERENCES public.dashboard_users(chat_id)
                 ON DELETE CASCADE,
    code_hash    TEXT NOT NULL,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    consumed_at  TIMESTAMPTZ,
    attempts     INT NOT NULL DEFAULT 0,
    client_ip    TEXT
);

CREATE INDEX IF NOT EXISTS dashboard_otps_chat_expires_idx
    ON public.dashboard_otps (chat_id, expires_at DESC);

CREATE TABLE IF NOT EXISTS public.dashboard_sessions (
    id            BIGSERIAL PRIMARY KEY,
    chat_id       TEXT NOT NULL REFERENCES public.dashboard_users(chat_id)
                  ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ,
    last_seen_at  TIMESTAMPTZ,
    user_agent    TEXT,
    client_ip     TEXT
);

CREATE INDEX IF NOT EXISTS dashboard_sessions_chat_idx
    ON public.dashboard_sessions (chat_id, issued_at DESC);

CREATE OR REPLACE VIEW public.dashboard_sessions_active AS
SELECT *
FROM public.dashboard_sessions
WHERE revoked_at IS NULL AND expires_at > NOW();

COMMIT;

-- Rollback:
-- BEGIN;
-- DROP VIEW IF EXISTS public.dashboard_sessions_active;
-- DROP TABLE IF EXISTS public.dashboard_sessions;
-- DROP TABLE IF EXISTS public.dashboard_otps;
-- DROP TABLE IF EXISTS public.dashboard_users;
-- COMMIT;
