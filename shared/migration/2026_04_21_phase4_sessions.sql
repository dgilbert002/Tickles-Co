-- Phase 4: Time/Sessions Service
-- Location: shared/migration/2026_04_21_phase4_sessions.sql

BEGIN;

-- Session registry
CREATE TABLE IF NOT EXISTS public.sessions (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) UNIQUE NOT NULL,
    description TEXT,
    timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Session definitions (day-of-week specific hours)
CREATE TABLE IF NOT EXISTS public.session_definitions (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
    day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    open_time TIME NOT NULL,
    close_time TIME NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (session_id, day_of_week)
);

-- Pre-seeded sessions
INSERT INTO public.sessions (name, description, timezone) VALUES
('crypto_24_7', 'Always open crypto markets', 'UTC'),
('london_equity', 'London Stock Exchange hours', 'Europe/London'),
('ny_equity', 'New York Stock Exchange hours', 'America/New_York'),
('tokyo_equity', 'Tokyo Stock Exchange hours', 'Asia/Tokyo'),
('london_open_range', 'First hour of London session', 'Europe/London'),
('ny_open_range', 'First hour of NY session', 'America/New_York'),
('capital_com_close', 'Capital.com daily close window', 'UTC')
ON CONFLICT (name) DO NOTHING;

-- Seed definitions for crypto_24_7 (0-6, 00:00 to 23:59:59)
DO $$
DECLARE
    sid INTEGER;
    i INTEGER;
BEGIN
    SELECT id INTO sid FROM public.sessions WHERE name = 'crypto_24_7';
    FOR i IN 0..6 LOOP
        INSERT INTO public.session_definitions (session_id, day_of_week, open_time, close_time)
        VALUES (sid, i, '00:00:00', '23:59:59')
        ON CONFLICT (session_id, day_of_week) DO NOTHING;
    END LOOP;
END $$;

-- Seed definitions for london_equity (Mon-Fri, 08:00 to 16:30)
DO $$
DECLARE
    sid INTEGER;
    i INTEGER;
BEGIN
    SELECT id INTO sid FROM public.sessions WHERE name = 'london_equity';
    FOR i IN 0..4 LOOP
        INSERT INTO public.session_definitions (session_id, day_of_week, open_time, close_time)
        VALUES (sid, i, '08:00:00', '16:30:00')
        ON CONFLICT (session_id, day_of_week) DO NOTHING;
    END LOOP;
END $$;

-- Seed definitions for ny_equity (Mon-Fri, 09:30 to 16:00)
DO $$
DECLARE
    sid INTEGER;
    i INTEGER;
BEGIN
    SELECT id INTO sid FROM public.sessions WHERE name = 'ny_equity';
    FOR i IN 0..4 LOOP
        INSERT INTO public.session_definitions (session_id, day_of_week, open_time, close_time)
        VALUES (sid, i, '09:30:00', '16:00:00')
        ON CONFLICT (session_id, day_of_week) DO NOTHING;
    END LOOP;
END $$;

-- Seed definitions for tokyo_equity (Mon-Fri, 09:00 to 15:00)
DO $$
DECLARE
    sid INTEGER;
    i INTEGER;
BEGIN
    SELECT id INTO sid FROM public.sessions WHERE name = 'tokyo_equity';
    FOR i IN 0..4 LOOP
        INSERT INTO public.session_definitions (session_id, day_of_week, open_time, close_time)
        VALUES (sid, i, '09:00:00', '15:00:00')
        ON CONFLICT (session_id, day_of_week) DO NOTHING;
    END LOOP;
END $$;

-- Seed definitions for london_open_range (Mon-Fri, 08:00 to 09:00)
DO $$
DECLARE
    sid INTEGER;
    i INTEGER;
BEGIN
    SELECT id INTO sid FROM public.sessions WHERE name = 'london_open_range';
    FOR i IN 0..4 LOOP
        INSERT INTO public.session_definitions (session_id, day_of_week, open_time, close_time)
        VALUES (sid, i, '08:00:00', '09:00:00')
        ON CONFLICT (session_id, day_of_week) DO NOTHING;
    END LOOP;
END $$;

-- Seed definitions for ny_open_range (Mon-Fri, 09:30 to 10:30)
DO $$
DECLARE
    sid INTEGER;
    i INTEGER;
BEGIN
    SELECT id INTO sid FROM public.sessions WHERE name = 'ny_open_range';
    FOR i IN 0..4 LOOP
        INSERT INTO public.session_definitions (session_id, day_of_week, open_time, close_time)
        VALUES (sid, i, '09:30:00', '10:30:00')
        ON CONFLICT (session_id, day_of_week) DO NOTHING;
    END LOOP;
END $$;

-- Seed definitions for capital_com_close (Mon-Fri, 20:55 to 21:00 UTC)
DO $$
DECLARE
    sid INTEGER;
    i INTEGER;
BEGIN
    SELECT id INTO sid FROM public.sessions WHERE name = 'capital_com_close';
    FOR i IN 0..4 LOOP
        INSERT INTO public.session_definitions (session_id, day_of_week, open_time, close_time)
        VALUES (sid, i, '20:55:00', '21:00:00')
        ON CONFLICT (session_id, day_of_week) DO NOTHING;
    END LOOP;
END $$;

COMMIT;
