--
-- PostgreSQL database dump
--

\restrict qQ8Ylf20yodkaK6ahFRWuyCr6ZFcOfCtcQtWukDCYiNhdLnqLeq0DTq3TW0Cbak

-- Dumped from database version 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: asset_class_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.asset_class_t AS ENUM (
    'crypto',
    'cfd',
    'stock',
    'forex',
    'commodity',
    'index'
);


--
-- Name: backtest_status_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.backtest_status_t AS ENUM (
    'pending',
    'claimed',
    'running',
    'completed',
    'failed'
);


--
-- Name: conflict_resolution_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.conflict_resolution_t AS ENUM (
    'sharpe',
    'return',
    'win_rate',
    'first_signal'
);


--
-- Name: direction_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.direction_t AS ENUM (
    'long',
    'short'
);


--
-- Name: indicator_category_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.indicator_category_t AS ENUM (
    'momentum',
    'trend',
    'volatility',
    'volume',
    'smart_money',
    'breakout',
    'pullback',
    'crash_protection',
    'combination'
);


--
-- Name: indicator_direction_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.indicator_direction_t AS ENUM (
    'bullish',
    'bearish',
    'neutral'
);


--
-- Name: sentiment_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.sentiment_t AS ENUM (
    'bullish',
    'bearish',
    'neutral',
    'mixed'
);


--
-- Name: timeframe_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.timeframe_t AS ENUM (
    '1m',
    '5m',
    '15m',
    '30m',
    '1h',
    '4h',
    '1d',
    '1w'
);


--
-- Name: fn_freeze_entry_reasons(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn_freeze_entry_reasons() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.entry_reason_frozen_at IS NOT NULL THEN
        IF NEW.entry_reason_trader IS DISTINCT FROM OLD.entry_reason_trader
           OR NEW.entry_reason_llm IS DISTINCT FROM OLD.entry_reason_llm
           OR NEW.entry_reason_agent IS DISTINCT FROM OLD.entry_reason_agent
           OR NEW.entry_reason_frozen_at IS DISTINCT FROM OLD.entry_reason_frozen_at THEN
            RAISE EXCEPTION 'entry_reason_* fields are frozen on tracked_positions.id=% (frozen_at=%)',
                OLD.id, OLD.entry_reason_frozen_at;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$;


--
-- Name: trg_set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_decisions (
    id bigint NOT NULL,
    persona_id bigint NOT NULL,
    company_id text,
    correlation_id text NOT NULL,
    mode text NOT NULL,
    verdict text NOT NULL,
    confidence numeric(6,4) DEFAULT 0 NOT NULL,
    rationale text,
    inputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    outputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    decided_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_decisions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_decisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_decisions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_decisions_id_seq OWNED BY public.agent_decisions.id;


--
-- Name: agent_personas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_personas (
    id bigint NOT NULL,
    name text NOT NULL,
    role text NOT NULL,
    description text,
    default_llm text,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_decisions_latest; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.agent_decisions_latest AS
 SELECT DISTINCT ON (d.persona_id, d.correlation_id) d.id,
    d.persona_id,
    p.name AS persona_name,
    d.company_id,
    d.correlation_id,
    d.mode,
    d.verdict,
    d.confidence,
    d.rationale,
    d.inputs,
    d.outputs,
    d.metadata,
    d.decided_at
   FROM (public.agent_decisions d
     JOIN public.agent_personas p ON ((p.id = d.persona_id)))
  ORDER BY d.persona_id, d.correlation_id, d.decided_at DESC;


--
-- Name: agent_opinions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_opinions (
    id bigint NOT NULL,
    position_id bigint NOT NULL,
    agent_name character varying(100) DEFAULT 'ChartHacker'::character varying NOT NULL,
    agent_version character varying(50) DEFAULT '1.0'::character varying NOT NULL,
    would_take_trade boolean NOT NULL,
    agent_direction character varying(8),
    agent_entry_price numeric(20,8),
    agent_stop_loss numeric(20,8),
    agent_take_profit numeric(20,8),
    agent_confidence numeric(5,4) DEFAULT 0.0 NOT NULL,
    reasoning text,
    pattern_detected character varying(255),
    indicators_used jsonb DEFAULT '[]'::jsonb NOT NULL,
    agent_pnl_pct numeric(10,4),
    trader_pnl_pct numeric(10,4),
    performance_delta numeric(10,4),
    lessons_learned text,
    similar_trades_found integer,
    historical_win_rate numeric(5,4),
    opinion_source character varying(32) DEFAULT 'daemon'::character varying NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT agent_opinions_agent_direction_check CHECK (((agent_direction)::text = ANY ((ARRAY['long'::character varying, 'short'::character varying, 'neutral'::character varying])::text[]))),
    CONSTRAINT agent_opinions_opinion_source_check CHECK (((opinion_source)::text = ANY ((ARRAY['daemon'::character varying, 'mcp_tool'::character varying, 'manual_review'::character varying, 'scheduled'::character varying])::text[])))
);


--
-- Name: agent_opinions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_opinions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_opinions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_opinions_id_seq OWNED BY public.agent_opinions.id;


--
-- Name: agent_personas_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_personas_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_personas_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_personas_id_seq OWNED BY public.agent_personas.id;


--
-- Name: agent_prompts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_prompts (
    id bigint NOT NULL,
    persona_id bigint NOT NULL,
    version integer NOT NULL,
    template text NOT NULL,
    variables jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_prompts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_prompts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_prompts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_prompts_id_seq OWNED BY public.agent_prompts.id;


--
-- Name: alt_data_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alt_data_items (
    id bigint NOT NULL,
    source text NOT NULL,
    provider text NOT NULL,
    universe text,
    exchange text,
    symbol text,
    scope_key text NOT NULL,
    metric text NOT NULL,
    value_numeric numeric(30,12),
    value_text text,
    unit text,
    as_of timestamp with time zone NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: alt_data_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.alt_data_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: alt_data_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.alt_data_items_id_seq OWNED BY public.alt_data_items.id;


--
-- Name: alt_data_latest; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.alt_data_latest AS
 SELECT DISTINCT ON (scope_key, metric) id,
    source,
    provider,
    universe,
    exchange,
    symbol,
    scope_key,
    metric,
    value_numeric,
    value_text,
    unit,
    as_of,
    ingested_at,
    payload,
    metadata
   FROM public.alt_data_items
  ORDER BY scope_key, metric, as_of DESC;


--
-- Name: api_cost_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_cost_log (
    id bigint NOT NULL,
    provider character varying(50) NOT NULL,
    model character varying(100),
    role character varying(50),
    context character varying(100),
    tokens_in integer DEFAULT 0,
    tokens_out integer DEFAULT 0,
    cost_usd numeric(10,6) DEFAULT 0,
    latency_ms integer,
    company_id character varying(50),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    operation character varying(100) DEFAULT ''::character varying NOT NULL,
    agent_id character varying(100) DEFAULT ''::character varying NOT NULL,
    temperature numeric(4,2),
    correlation_id character varying(36) DEFAULT ''::character varying NOT NULL,
    request_path character varying(200) DEFAULT ''::character varying NOT NULL,
    response_path character varying(200) DEFAULT ''::character varying NOT NULL,
    success boolean DEFAULT true NOT NULL,
    http_status integer DEFAULT 200 NOT NULL,
    extra jsonb
);


--
-- Name: api_cost_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.api_cost_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: api_cost_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.api_cost_log_id_seq OWNED BY public.api_cost_log.id;


--
-- Name: arb_opportunities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.arb_opportunities (
    id bigint NOT NULL,
    company_id text,
    symbol text NOT NULL,
    buy_venue text NOT NULL,
    sell_venue text NOT NULL,
    buy_ask numeric(20,10) NOT NULL,
    sell_bid numeric(20,10) NOT NULL,
    size_base numeric(20,10) DEFAULT 0 NOT NULL,
    gross_bps numeric(12,4) NOT NULL,
    net_bps numeric(12,4) NOT NULL,
    est_profit_usd numeric(16,4) DEFAULT 0 NOT NULL,
    fees_bps numeric(10,4) DEFAULT 0 NOT NULL,
    correlation_id text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    observed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT arb_opportunities_net_bps_check CHECK ((net_bps >= (0)::numeric))
);


--
-- Name: arb_opportunities_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.arb_opportunities_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: arb_opportunities_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.arb_opportunities_id_seq OWNED BY public.arb_opportunities.id;


--
-- Name: arb_venues; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.arb_venues (
    id bigint NOT NULL,
    company_id text,
    name text NOT NULL,
    kind text DEFAULT 'spot'::text NOT NULL,
    taker_fee_bps numeric(8,4) DEFAULT 10 NOT NULL,
    maker_fee_bps numeric(8,4) DEFAULT 2 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: arb_venues_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.arb_venues_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: arb_venues_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.arb_venues_id_seq OWNED BY public.arb_venues.id;


--
-- Name: assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assets (
    id integer NOT NULL,
    symbol character varying(32) NOT NULL,
    display_name character varying(128) NOT NULL,
    asset_class public.asset_class_t NOT NULL,
    alias_of_id integer,
    auto_seeded boolean DEFAULT true NOT NULL,
    curation_notes text,
    metadata jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: TABLE assets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.assets IS 'Phase 14: logical asset layer. Many instruments (BTC-spot on Binance, BTC-perp on Bybit, BTC-CFD on Capital) roll up to one asset row. alias_of_id supports LLM-proposed dedup without losing history.';


--
-- Name: assets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.assets_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: assets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.assets_id_seq OWNED BY public.assets.id;


--
-- Name: backtest_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.backtest_queue (
    id bigint NOT NULL,
    param_hash character(64) NOT NULL,
    instrument_id bigint,
    indicator_name character varying(100),
    params jsonb,
    status public.backtest_status_t DEFAULT 'pending'::public.backtest_status_t NOT NULL,
    worker_id character varying(50),
    claimed_at timestamp(3) with time zone,
    completed_at timestamp(3) with time zone,
    result_id bigint,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: backtest_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.backtest_queue_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: backtest_queue_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.backtest_queue_id_seq OWNED BY public.backtest_queue.id;


--
-- Name: backtest_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.backtest_results (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    indicator_name character varying(100) NOT NULL,
    param_hash character(64) NOT NULL,
    params jsonb NOT NULL,
    timeframe public.timeframe_t DEFAULT '5m'::public.timeframe_t NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    initial_balance numeric(20,8),
    final_balance numeric(20,8),
    total_return_pct numeric(10,4),
    total_trades integer,
    win_rate_pct numeric(5,2),
    sharpe_ratio numeric(10,4),
    max_drawdown_pct numeric(10,4),
    profit_factor numeric(10,4),
    total_fees numeric(20,8) DEFAULT 0,
    total_spread_costs numeric(20,8) DEFAULT 0,
    total_overnight_costs numeric(20,8) DEFAULT 0,
    candle_data_hash character(64),
    engine_version character varying(20),
    run_duration_ms integer,
    promotion_status character varying(30) DEFAULT 'candidate'::character varying,
    parent_strategy_id bigint,
    deflated_sharpe numeric(10,4),
    oos_sharpe numeric(10,4),
    oos_return_pct numeric(10,4),
    verified_by character varying(100),
    verified_at timestamp(3) with time zone,
    clickhouse_run_id uuid,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: backtest_results_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.backtest_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: backtest_results_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.backtest_results_id_seq OWNED BY public.backtest_results.id;


--
-- Name: backtest_submissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.backtest_submissions (
    id bigint NOT NULL,
    company_id text,
    client_id text,
    spec jsonb NOT NULL,
    spec_hash text NOT NULL,
    status text DEFAULT 'submitted'::text NOT NULL,
    queue_job_id text,
    result_summary jsonb,
    artefacts jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    queued_at timestamp with time zone,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: backtest_submissions_active; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.backtest_submissions_active AS
 SELECT id,
    company_id,
    client_id,
    spec,
    spec_hash,
    status,
    queue_job_id,
    result_summary,
    artefacts,
    error,
    metadata,
    submitted_at,
    queued_at,
    started_at,
    completed_at,
    updated_at
   FROM public.backtest_submissions
  WHERE (status = ANY (ARRAY['submitted'::text, 'queued'::text, 'running'::text]));


--
-- Name: backtest_submissions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.backtest_submissions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: backtest_submissions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.backtest_submissions_id_seq OWNED BY public.backtest_submissions.id;


--
-- Name: backtest_trade_details; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.backtest_trade_details (
    id bigint NOT NULL,
    backtest_result_id bigint NOT NULL,
    trade_index integer NOT NULL,
    entry_price numeric(20,8) NOT NULL,
    exit_price numeric(20,8),
    direction public.direction_t NOT NULL,
    entry_at timestamp(3) with time zone,
    exit_at timestamp(3) with time zone,
    quantity numeric(20,8),
    gross_pnl numeric(20,8),
    spread_cost numeric(20,8) DEFAULT 0,
    overnight_cost numeric(20,8) DEFAULT 0,
    net_pnl numeric(20,8),
    is_winner boolean,
    window_close_time time without time zone,
    signal_candle_hash character(64)
);


--
-- Name: backtest_trade_details_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.backtest_trade_details_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: backtest_trade_details_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.backtest_trade_details_id_seq OWNED BY public.backtest_trade_details.id;


--
-- Name: banker_balances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.banker_balances (
    id bigint NOT NULL,
    company_id text NOT NULL,
    exchange text NOT NULL,
    account_id_external text NOT NULL,
    account_type text DEFAULT 'demo'::text NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    balance numeric(20,8) NOT NULL,
    equity numeric(20,8),
    margin_used numeric(20,8),
    free_margin numeric(20,8),
    unrealised_pnl numeric(20,8),
    source text DEFAULT 'ccxt'::text NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: banker_balances_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.banker_balances_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: banker_balances_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.banker_balances_id_seq OWNED BY public.banker_balances.id;


--
-- Name: banker_balances_latest; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.banker_balances_latest AS
 SELECT DISTINCT ON (company_id, exchange, account_id_external, currency) id,
    company_id,
    exchange,
    account_id_external,
    account_type,
    currency,
    balance,
    equity,
    margin_used,
    free_margin,
    unrealised_pnl,
    source,
    ts,
    metadata
   FROM public.banker_balances
  ORDER BY company_id, exchange, account_id_external, currency, ts DESC;


--
-- Name: candles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
)
PARTITION BY RANGE ("timestamp");


--
-- Name: candles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.candles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: candles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.candles_id_seq OWNED BY public.candles.id;


--
-- Name: candles_2024_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_01 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_02 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_03 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_04 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_05 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_06 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_07 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_08 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_09 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_10; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_10 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_11; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_11 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2024_12; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2024_12 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_01 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_02 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_03 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_04 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_05 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_06 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_07 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_08 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_09 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_10; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_10 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_11; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_11 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2025_12; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2025_12 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_01 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_02 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_03 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_04 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_05 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_06 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_07 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_08 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_09 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_10; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_10 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_11; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_11 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_2026_12; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_2026_12 (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: candles_future; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candles_future (
    id bigint DEFAULT nextval('public.candles_id_seq'::regclass) NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    source character varying(30) NOT NULL,
    "timestamp" timestamp(3) with time zone NOT NULL,
    open numeric(20,8) NOT NULL,
    high numeric(20,8) NOT NULL,
    low numeric(20,8) NOT NULL,
    close numeric(20,8) NOT NULL,
    volume numeric(30,8),
    open_bid numeric(20,8),
    close_ask numeric(20,8),
    is_fake boolean DEFAULT false NOT NULL,
    fake_source_timestamp timestamp(3) with time zone,
    fake_comment character varying(200),
    data_hash character(64),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: capabilities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.capabilities (
    id bigint NOT NULL,
    company_id text NOT NULL,
    scope_kind text NOT NULL,
    scope_id text NOT NULL,
    max_notional_usd numeric(20,8),
    max_leverage integer,
    max_daily_loss_usd numeric(20,8),
    max_open_positions integer,
    allow_venues text[] DEFAULT '{}'::text[] NOT NULL,
    deny_venues text[] DEFAULT '{}'::text[] NOT NULL,
    allow_symbols text[] DEFAULT '{}'::text[] NOT NULL,
    deny_symbols text[] DEFAULT '{}'::text[] NOT NULL,
    allow_directions text[] DEFAULT '{long,short}'::text[] NOT NULL,
    allow_order_types text[] DEFAULT '{market,limit}'::text[] NOT NULL,
    active boolean DEFAULT true NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: capabilities_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.capabilities_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: capabilities_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.capabilities_id_seq OWNED BY public.capabilities.id;


--
-- Name: collector_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collector_catalog (
    id bigint NOT NULL,
    source_type character varying(32) NOT NULL,
    source_name character varying(255) NOT NULL,
    source_slug character varying(100) NOT NULL,
    connection_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    primary_asset_class character varying(32),
    notes text,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT collector_catalog_primary_asset_class_check CHECK (((primary_asset_class)::text = ANY ((ARRAY['crypto'::character varying, 'cfd'::character varying, 'stock'::character varying, 'forex'::character varying, 'commodity'::character varying, 'index'::character varying, 'mixed'::character varying])::text[]))),
    CONSTRAINT collector_catalog_source_type_check CHECK (((source_type)::text = ANY ((ARRAY['discord'::character varying, 'telegram'::character varying, 'rss'::character varying, 'tradingview'::character varying, 'twitter'::character varying, 'api'::character varying, 'webhook'::character varying])::text[])))
);


--
-- Name: collector_catalog_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.collector_catalog_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: collector_catalog_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.collector_catalog_id_seq OWNED BY public.collector_catalog.id;


--
-- Name: collector_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collector_sources (
    id bigint NOT NULL,
    parent_id bigint,
    source_type character varying(32) NOT NULL,
    entity_type character varying(32) NOT NULL,
    platform_id character varying(128) NOT NULL,
    name character varying(255) DEFAULT ''::character varying NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    priority smallint DEFAULT 5 NOT NULL,
    collection_interval_seconds integer DEFAULT 120 NOT NULL,
    max_messages_per_cycle integer DEFAULT 200 NOT NULL,
    group_window_seconds integer DEFAULT 60 NOT NULL,
    media_policy character varying(64) DEFAULT 'reference_only'::character varying NOT NULL,
    allowed_users jsonb,
    blocked_users jsonb,
    last_collected_at timestamp with time zone,
    last_error text,
    error_count integer DEFAULT 0 NOT NULL,
    items_collected bigint DEFAULT 0 NOT NULL,
    platform_config jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    zone_filter_enabled boolean DEFAULT true NOT NULL,
    zone_filter_threshold numeric(4,3),
    rate_limit_msgs_per_sec integer,
    CONSTRAINT collector_sources_entity_type_check CHECK (((entity_type)::text = ANY ((ARRAY['server'::character varying, 'group'::character varying, 'channel'::character varying, 'user'::character varying, 'feed'::character varying, 'topic'::character varying])::text[]))),
    CONSTRAINT collector_sources_media_policy_check CHECK (((media_policy)::text = ANY ((ARRAY['ignore'::character varying, 'reference_only'::character varying, 'download_keep'::character varying, 'download_analyze_discard'::character varying, 'download_analyze_keep'::character varying])::text[]))),
    CONSTRAINT collector_sources_priority_check CHECK (((priority >= 1) AND (priority <= 10))),
    CONSTRAINT collector_sources_source_type_check CHECK (((source_type)::text = ANY ((ARRAY['telegram'::character varying, 'discord'::character varying, 'rss'::character varying, 'tradingview'::character varying, 'api'::character varying])::text[])))
);


--
-- Name: collector_sources_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.collector_sources_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: collector_sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.collector_sources_id_seq OWNED BY public.collector_sources.id;


--
-- Name: contest_participants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contest_participants (
    contest_id text NOT NULL,
    company_id text NOT NULL,
    agent_id text NOT NULL,
    strategy_ref text,
    joined_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: contests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contests (
    id text NOT NULL,
    name text NOT NULL,
    venues text[] NOT NULL,
    coins text[] NOT NULL,
    starting_balance_usd numeric(20,8) NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: copy_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.copy_sources (
    id bigint NOT NULL,
    company_id text,
    name text NOT NULL,
    kind text NOT NULL,
    venue text,
    identifier text NOT NULL,
    size_mode text DEFAULT 'ratio'::text NOT NULL,
    size_value numeric(20,6) DEFAULT 0.1 NOT NULL,
    max_notional_usd numeric(20,4),
    symbol_whitelist jsonb DEFAULT '[]'::jsonb NOT NULL,
    symbol_blacklist jsonb DEFAULT '[]'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_checked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: copy_sources_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.copy_sources_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: copy_sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.copy_sources_id_seq OWNED BY public.copy_sources.id;


--
-- Name: copy_trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.copy_trades (
    id bigint NOT NULL,
    source_id bigint NOT NULL,
    company_id text,
    source_fill_id text NOT NULL,
    source_trade_ts timestamp with time zone NOT NULL,
    symbol text NOT NULL,
    side text NOT NULL,
    source_price numeric(20,10),
    source_qty_base numeric(20,10),
    source_notional_usd numeric(20,4),
    mapped_qty_base numeric(20,10) NOT NULL,
    mapped_notional_usd numeric(20,4) NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    skip_reason text,
    correlation_id text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: copy_trades_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.copy_trades_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: copy_trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.copy_trades_id_seq OWNED BY public.copy_trades.id;


--
-- Name: crash_protection_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.crash_protection_events (
    id bigint NOT NULL,
    rule_id bigint,
    company_id text,
    universe text,
    exchange text,
    symbol text,
    rule_type text NOT NULL,
    action text NOT NULL,
    status text NOT NULL,
    severity text NOT NULL,
    reason text,
    metric numeric(20,8),
    threshold numeric(20,8),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: crash_protection_active; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.crash_protection_active AS
 SELECT DISTINCT ON (COALESCE(rule_id, (0)::bigint), COALESCE(company_id, ''::text), COALESCE(universe, ''::text), COALESCE(exchange, ''::text), COALESCE(symbol, ''::text)) id,
    rule_id,
    company_id,
    universe,
    exchange,
    symbol,
    rule_type,
    action,
    status,
    severity,
    reason,
    metric,
    threshold,
    metadata,
    ts
   FROM public.crash_protection_events
  ORDER BY COALESCE(rule_id, (0)::bigint), COALESCE(company_id, ''::text), COALESCE(universe, ''::text), COALESCE(exchange, ''::text), COALESCE(symbol, ''::text), ts DESC;


--
-- Name: crash_protection_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.crash_protection_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: crash_protection_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.crash_protection_events_id_seq OWNED BY public.crash_protection_events.id;


--
-- Name: crash_protection_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.crash_protection_rules (
    id bigint NOT NULL,
    company_id text,
    universe text,
    exchange text,
    symbol text,
    rule_type text NOT NULL,
    action text NOT NULL,
    threshold numeric(20,8),
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    severity text DEFAULT 'warning'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: crash_protection_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.crash_protection_rules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: crash_protection_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.crash_protection_rules_id_seq OWNED BY public.crash_protection_rules.id;


--
-- Name: cron_heartbeats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cron_heartbeats (
    agent_id text NOT NULL,
    last_run_at timestamp with time zone NOT NULL,
    last_status text NOT NULL,
    last_message text,
    expected_interval_seconds integer NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cron_heartbeats_last_status_check CHECK ((last_status = ANY (ARRAY['ok'::text, 'error'::text, 'partial'::text])))
);


--
-- Name: TABLE cron_heartbeats; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.cron_heartbeats IS 'Phase R [BM] — every cron-driven agent UPSERTs on each fire. cron_canary daemon polls and alerts when stale.';


--
-- Name: dashboard_otps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_otps (
    id bigint NOT NULL,
    chat_id text NOT NULL,
    code_hash text NOT NULL,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    client_ip text
);


--
-- Name: dashboard_otps_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.dashboard_otps_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dashboard_otps_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.dashboard_otps_id_seq OWNED BY public.dashboard_otps.id;


--
-- Name: dashboard_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_sessions (
    id bigint NOT NULL,
    chat_id text NOT NULL,
    token_hash text NOT NULL,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    last_seen_at timestamp with time zone,
    user_agent text,
    client_ip text
);


--
-- Name: dashboard_sessions_active; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.dashboard_sessions_active AS
 SELECT id,
    chat_id,
    token_hash,
    issued_at,
    expires_at,
    revoked_at,
    last_seen_at,
    user_agent,
    client_ip
   FROM public.dashboard_sessions
  WHERE ((revoked_at IS NULL) AND (expires_at > now()));


--
-- Name: dashboard_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.dashboard_sessions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dashboard_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.dashboard_sessions_id_seq OWNED BY public.dashboard_sessions.id;


--
-- Name: dashboard_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_users (
    id bigint NOT NULL,
    chat_id text NOT NULL,
    display_name text,
    role text DEFAULT 'owner'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login_at timestamp with time zone
);


--
-- Name: dashboard_users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.dashboard_users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dashboard_users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.dashboard_users_id_seq OWNED BY public.dashboard_users.id;


--
-- Name: data_sufficiency_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.data_sufficiency_reports (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    profile_name character varying(64) NOT NULL,
    verdict character varying(24) NOT NULL,
    bars integer DEFAULT 0 NOT NULL,
    first_ts timestamp(3) with time zone,
    last_ts timestamp(3) with time zone,
    gap_ratio numeric(10,6) DEFAULT 0 NOT NULL,
    max_gap_minutes integer DEFAULT 0 NOT NULL,
    fresh_lag_minutes integer,
    report_json jsonb NOT NULL,
    computed_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    ttl_seconds integer DEFAULT 300 NOT NULL,
    CONSTRAINT data_sufficiency_reports_verdict_check CHECK (((verdict)::text = ANY ((ARRAY['pass'::character varying, 'pass_with_warnings'::character varying, 'fail'::character varying])::text[])))
);


--
-- Name: TABLE data_sufficiency_reports; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.data_sufficiency_reports IS 'Phase 15 cache: most recent sufficiency verdict per (instrument, timeframe, profile).';


--
-- Name: COLUMN data_sufficiency_reports.verdict; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.data_sufficiency_reports.verdict IS 'pass | pass_with_warnings | fail';


--
-- Name: COLUMN data_sufficiency_reports.report_json; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.data_sufficiency_reports.report_json IS 'Full SufficiencyReport pydantic payload (coverage stats, reasons, integrity issues).';


--
-- Name: COLUMN data_sufficiency_reports.ttl_seconds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.data_sufficiency_reports.ttl_seconds IS 'Cache lifetime; reads older than this trigger a fresh scan.';


--
-- Name: data_sufficiency_reports_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.data_sufficiency_reports_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: data_sufficiency_reports_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.data_sufficiency_reports_id_seq OWNED BY public.data_sufficiency_reports.id;


--
-- Name: derivatives_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.derivatives_snapshots (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    snapshot_at timestamp(3) with time zone NOT NULL,
    open_interest numeric(30,8),
    funding_rate numeric(15,10),
    long_short_ratio numeric(10,4),
    liquidation_volume_24h numeric(30,8),
    source character varying(50) NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: derivatives_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.derivatives_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: derivatives_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.derivatives_snapshots_id_seq OWNED BY public.derivatives_snapshots.id;


--
-- Name: events_calendar; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.events_calendar (
    id bigint NOT NULL,
    kind text NOT NULL,
    provider text NOT NULL,
    name text NOT NULL,
    universe text,
    exchange text,
    symbol text,
    country text,
    importance smallint DEFAULT 1 NOT NULL,
    event_time timestamp with time zone NOT NULL,
    window_before_minutes integer DEFAULT 0 NOT NULL,
    window_after_minutes integer DEFAULT 0 NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    dedupe_key text NOT NULL
);


--
-- Name: events_active; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.events_active AS
 SELECT id,
    kind,
    provider,
    name,
    universe,
    exchange,
    symbol,
    country,
    importance,
    event_time,
    window_before_minutes,
    window_after_minutes,
    payload,
    metadata,
    created_at,
    updated_at,
    dedupe_key
   FROM public.events_calendar
  WHERE ((now() >= (event_time - make_interval(mins => window_before_minutes))) AND (now() <= (event_time + make_interval(mins => window_after_minutes))));


--
-- Name: events_calendar_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.events_calendar_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: events_calendar_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.events_calendar_id_seq OWNED BY public.events_calendar.id;


--
-- Name: events_upcoming; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.events_upcoming AS
 SELECT id,
    kind,
    provider,
    name,
    universe,
    exchange,
    symbol,
    country,
    importance,
    event_time,
    window_before_minutes,
    window_after_minutes,
    payload,
    metadata,
    created_at,
    updated_at,
    dedupe_key
   FROM public.events_calendar
  WHERE (event_time >= now())
  ORDER BY event_time;


--
-- Name: fills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fills (
    id bigint NOT NULL,
    order_id bigint NOT NULL,
    company_id text NOT NULL,
    adapter text NOT NULL,
    exchange text NOT NULL,
    account_id_external text NOT NULL,
    symbol text NOT NULL,
    direction text NOT NULL,
    quantity numeric(20,8) NOT NULL,
    price numeric(20,8) NOT NULL,
    notional_usd numeric(20,8) NOT NULL,
    fee_usd numeric(20,8) DEFAULT 0 NOT NULL,
    fee_currency text,
    is_maker boolean,
    liquidity text,
    realized_pnl_usd numeric(20,8),
    external_fill_id text,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: fills_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fills_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fills_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fills_id_seq OWNED BY public.fills.id;


--
-- Name: indicator_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.indicator_catalog (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    category public.indicator_category_t NOT NULL,
    direction public.indicator_direction_t NOT NULL,
    description text,
    default_params jsonb,
    param_ranges jsonb,
    source_system character varying(30),
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: indicator_catalog_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.indicator_catalog_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: indicator_catalog_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.indicator_catalog_id_seq OWNED BY public.indicator_catalog.id;


--
-- Name: indicators; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.indicators (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    timeframe public.timeframe_t NOT NULL,
    indicator_name character varying(100) NOT NULL,
    params_hash character(64),
    params jsonb,
    signal boolean,
    value numeric(20,8),
    metadata jsonb,
    calculated_at timestamp(3) with time zone NOT NULL,
    candle_timestamp timestamp(3) with time zone NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: indicators_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.indicators_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: indicators_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.indicators_id_seq OWNED BY public.indicators.id;


--
-- Name: instrument_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instrument_aliases (
    id integer NOT NULL,
    instrument_id bigint NOT NULL,
    alias_type character varying(24) NOT NULL,
    alias_value character varying(64) NOT NULL,
    source character varying(24),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: TABLE instrument_aliases; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.instrument_aliases IS 'Phase 14: lookup table so "BTCUSDT", "BTC/USDT", "BTC-USDT-SWAP", "tradingview:BTCUSDT" all resolve to one instrument row.';


--
-- Name: instrument_aliases_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.instrument_aliases_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: instrument_aliases_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.instrument_aliases_id_seq OWNED BY public.instrument_aliases.id;


--
-- Name: instruments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instruments (
    id bigint NOT NULL,
    symbol character varying(50) NOT NULL,
    exchange character varying(50) NOT NULL,
    asset_class public.asset_class_t NOT NULL,
    base_currency character varying(20),
    quote_currency character varying(20),
    min_size numeric(20,8),
    max_size numeric(20,8),
    size_increment numeric(20,8),
    contract_multiplier numeric(20,8) DEFAULT 1.00000000 NOT NULL,
    spread_pct numeric(10,6),
    maker_fee_pct numeric(10,6),
    taker_fee_pct numeric(10,6),
    overnight_funding_long_pct numeric(15,10),
    overnight_funding_short_pct numeric(15,10),
    margin_factor numeric(10,4),
    max_leverage integer,
    opening_hours jsonb,
    is_active boolean DEFAULT true NOT NULL,
    last_synced_at timestamp(3) with time zone,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    asset_id integer,
    venue_id integer
);


--
-- Name: COLUMN instruments.asset_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.instruments.asset_id IS 'Phase 14: FK to the logical asset. Nullable until backfill / loader populates. Candles keep joining on instrument_id exactly as before.';


--
-- Name: COLUMN instruments.venue_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.instruments.venue_id IS 'Phase 14: FK to the venue. Nullable until backfill / loader populates.';


--
-- Name: instruments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.instruments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: instruments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.instruments_id_seq OWNED BY public.instruments.id;


--
-- Name: leverage_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leverage_history (
    id bigint NOT NULL,
    company_id text NOT NULL,
    exchange text NOT NULL,
    symbol text NOT NULL,
    direction text,
    leverage_requested integer NOT NULL,
    leverage_applied integer NOT NULL,
    requested_by text DEFAULT 'system'::text NOT NULL,
    ok boolean DEFAULT true NOT NULL,
    reason text,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: leverage_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.leverage_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: leverage_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.leverage_history_id_seq OWNED BY public.leverage_history.id;


--
-- Name: mcp_invocations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_invocations (
    id bigint NOT NULL,
    tool_name text NOT NULL,
    tool_version text,
    caller text,
    transport text,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'ok'::text NOT NULL,
    result jsonb,
    error text,
    latency_ms integer,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);


--
-- Name: mcp_invocations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mcp_invocations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mcp_invocations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mcp_invocations_id_seq OWNED BY public.mcp_invocations.id;


--
-- Name: mcp_invocations_recent; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.mcp_invocations_recent AS
 SELECT id,
    tool_name,
    caller,
    transport,
    status,
    latency_ms,
    started_at,
    completed_at
   FROM public.mcp_invocations
  ORDER BY started_at DESC
 LIMIT 500;


--
-- Name: mcp_tool_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_tool_requests (
    id bigint NOT NULL,
    name text NOT NULL,
    rationale text NOT NULL,
    example_input jsonb,
    example_output jsonb,
    requested_by text DEFAULT 'anonymous'::text NOT NULL,
    content_hash text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_at timestamp with time zone,
    review_note text
);


--
-- Name: mcp_tool_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mcp_tool_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mcp_tool_requests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mcp_tool_requests_id_seq OWNED BY public.mcp_tool_requests.id;


--
-- Name: mcp_tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_tools (
    id bigint NOT NULL,
    name text NOT NULL,
    version text DEFAULT '1'::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    input_schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    read_only boolean DEFAULT true NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: mcp_tools_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mcp_tools_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mcp_tools_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mcp_tools_id_seq OWNED BY public.mcp_tools.id;


--
-- Name: media_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.media_items (
    id bigint NOT NULL,
    news_item_id bigint NOT NULL,
    source_id bigint,
    media_type character varying(32) NOT NULL,
    extraction_method character varying(32) NOT NULL,
    source_url text,
    resolved_url text,
    local_path character varying(512),
    thumbnail_path character varying(512),
    mime_type character varying(128),
    file_size_bytes bigint,
    file_hash character(64),
    duration_seconds integer,
    dimensions character varying(32),
    processing_status character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    processing_result jsonb,
    processing_error text,
    processed_at timestamp with time zone,
    platform_file_id character varying(255),
    metadata jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT media_items_extraction_method_check CHECK (((extraction_method)::text = ANY ((ARRAY['attached'::character varying, 'embedded'::character varying, 'linked_in_text'::character varying, 'linked_external'::character varying, 'cdn_hosted'::character varying, 'forwarded'::character varying])::text[]))),
    CONSTRAINT media_items_media_type_check CHECK (((media_type)::text = ANY ((ARRAY['image'::character varying, 'video'::character varying, 'audio'::character varying, 'document'::character varying, 'voice'::character varying, 'link'::character varying, 'embed'::character varying, 'webpage'::character varying, 'stream'::character varying])::text[]))),
    CONSTRAINT media_items_processing_status_check CHECK (((processing_status)::text = ANY ((ARRAY['pending'::character varying, 'downloading'::character varying, 'downloaded'::character varying, 'analyzing'::character varying, 'analyzed'::character varying, 'discarded'::character varying, 'failed'::character varying, 'skipped'::character varying, 'skipped_vision_unavailable'::character varying])::text[])))
);


--
-- Name: media_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.media_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: media_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.media_items_id_seq OWNED BY public.media_items.id;


--
-- Name: memu_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memu_outbox (
    id bigint NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone,
    last_error text,
    attempt_count integer DEFAULT 0 NOT NULL
);


--
-- Name: memu_outbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.memu_outbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: memu_outbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.memu_outbox_id_seq OWNED BY public.memu_outbox.id;


--
-- Name: news_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_items (
    id bigint NOT NULL,
    hash_key character(64) NOT NULL,
    source character varying(50) NOT NULL,
    headline text,
    content text,
    sentiment public.sentiment_t,
    instruments jsonb,
    published_at timestamp(3) with time zone,
    collected_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    source_id bigint,
    channel_name character varying(255),
    author character varying(255),
    author_id character varying(128),
    message_id character varying(128),
    metadata jsonb,
    has_media boolean DEFAULT false NOT NULL,
    media_count smallint DEFAULT 0 NOT NULL,
    enrichment jsonb,
    enriched_at timestamp with time zone,
    enrichment_status text DEFAULT 'pending'::text NOT NULL,
    context_window jsonb,
    zone_filter_confidence numeric(4,3),
    zone_filter_reason text,
    image_phash character(16),
    duplicate_of_id bigint
);


--
-- Name: news_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.news_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: news_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.news_items_id_seq OWNED BY public.news_items.id;


--
-- Name: news_items_pending_enrichment; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.news_items_pending_enrichment AS
 SELECT id,
    hash_key,
    source,
    headline,
    content,
    collected_at
   FROM public.news_items
  WHERE (enriched_at IS NULL)
  ORDER BY collected_at;


--
-- Name: optimiser_candidates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.optimiser_candidates (
    id bigint NOT NULL,
    strategy text NOT NULL,
    company_id text,
    params jsonb NOT NULL,
    score numeric(12,6),
    status text DEFAULT 'pending'::text NOT NULL,
    correlation_id text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: optimiser_candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.optimiser_candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: optimiser_candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.optimiser_candidates_id_seq OWNED BY public.optimiser_candidates.id;


--
-- Name: order_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_events (
    id bigint NOT NULL,
    order_id bigint NOT NULL,
    event_type text NOT NULL,
    severity text DEFAULT 'info'::text NOT NULL,
    message text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: order_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.order_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: order_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.order_events_id_seq OWNED BY public.order_events.id;


--
-- Name: orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orders (
    id bigint NOT NULL,
    company_id text NOT NULL,
    strategy_id text,
    agent_id text,
    intent_hash text NOT NULL,
    treasury_decision_id bigint,
    adapter text NOT NULL,
    exchange text NOT NULL,
    account_id_external text NOT NULL,
    symbol text NOT NULL,
    direction text NOT NULL,
    order_type text NOT NULL,
    quantity numeric(20,8) NOT NULL,
    requested_notional_usd numeric(20,8),
    requested_price numeric(20,8),
    time_in_force text,
    client_order_id text NOT NULL,
    external_order_id text,
    status text NOT NULL,
    filled_quantity numeric(20,8) DEFAULT 0 NOT NULL,
    average_fill_price numeric(20,8),
    fees_paid_usd numeric(20,8) DEFAULT 0 NOT NULL,
    reason text,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: orders_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.orders_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: orders_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.orders_id_seq OWNED BY public.orders.id;


--
-- Name: paper_wallets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.paper_wallets (
    id bigint NOT NULL,
    company_id text NOT NULL,
    agent_id text NOT NULL,
    exchange text DEFAULT 'bybit'::text NOT NULL,
    account_id_external text NOT NULL,
    starting_balance_usd numeric(20,8) NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    account_type text DEFAULT 'paper'::text NOT NULL,
    contest_id text,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: paper_wallets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.paper_wallets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: paper_wallets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.paper_wallets_id_seq OWNED BY public.paper_wallets.id;


--
-- Name: position_postmortems; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.position_postmortems (
    id bigint NOT NULL,
    position_id bigint NOT NULL,
    postmortem_version character varying(32) NOT NULL,
    postmortem_provider character varying(32) NOT NULL,
    postmortem_model character varying(128) NOT NULL,
    param_hash character(16) NOT NULL,
    candle_data_hash character(16),
    what_happened text NOT NULL,
    why_it_worked text,
    why_it_failed text,
    trader_thesis_validated boolean,
    llm_thesis_validated boolean,
    pattern_confirmed jsonb,
    pattern_failed jsonb,
    regime_at_entry character varying(64),
    regime_at_exit character varying(64),
    lessons_for_actor text,
    lessons_for_company text,
    cost_usd numeric(20,8) DEFAULT 0,
    latency_ms integer,
    llm_raw_request_path text,
    llm_raw_response_path text,
    prompt_version character varying(32) NOT NULL,
    correlation_id character varying(36),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: position_postmortems_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.position_postmortems_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: position_postmortems_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.position_postmortems_id_seq OWNED BY public.position_postmortems.id;


--
-- Name: position_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.position_snapshots (
    id bigint NOT NULL,
    company_id text NOT NULL,
    adapter text NOT NULL,
    exchange text NOT NULL,
    account_id_external text NOT NULL,
    symbol text NOT NULL,
    direction text NOT NULL,
    quantity numeric(20,8) NOT NULL,
    average_entry_price numeric(20,8),
    notional_usd numeric(20,8),
    unrealised_pnl_usd numeric(20,8),
    realized_pnl_usd numeric(20,8) DEFAULT 0 NOT NULL,
    leverage integer DEFAULT 1 NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    source text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: position_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.position_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: position_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.position_snapshots_id_seq OWNED BY public.position_snapshots.id;


--
-- Name: position_updates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.position_updates (
    id bigint NOT NULL,
    position_id bigint NOT NULL,
    price numeric(20,8) NOT NULL,
    "timestamp" timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    unrealized_pnl_pct numeric(10,4) NOT NULL,
    unrealized_pnl_usd numeric(20,8) NOT NULL,
    distance_to_entry_pct numeric(10,4) NOT NULL,
    distance_to_sl_pct numeric(10,4),
    distance_to_tp1_pct numeric(10,4),
    time_in_trade_minutes integer NOT NULL,
    update_source character varying(32) DEFAULT 'candle_poll'::character varying NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT position_updates_update_source_check CHECK (((update_source)::text = ANY ((ARRAY['candle_poll'::character varying, 'tick'::character varying, 'manual'::character varying, 'agent_review'::character varying])::text[])))
);


--
-- Name: position_updates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.position_updates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: position_updates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.position_updates_id_seq OWNED BY public.position_updates.id;


--
-- Name: positions_current; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.positions_current AS
 SELECT DISTINCT ON (company_id, adapter, exchange, account_id_external, symbol) id,
    company_id,
    adapter,
    exchange,
    account_id_external,
    symbol,
    direction,
    quantity,
    average_entry_price,
    notional_usd,
    unrealised_pnl_usd,
    realized_pnl_usd,
    leverage,
    ts,
    source,
    metadata
   FROM public.position_snapshots
  ORDER BY company_id, adapter, exchange, account_id_external, symbol, ts DESC;


--
-- Name: prompt_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.prompt_versions (
    id bigint NOT NULL,
    name text NOT NULL,
    version character varying(32) NOT NULL,
    prompt_hash character(16) NOT NULL,
    system text,
    body text NOT NULL,
    taxonomy_rule text,
    model_hint text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    notes text
);


--
-- Name: prompt_versions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.prompt_versions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: prompt_versions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.prompt_versions_id_seq OWNED BY public.prompt_versions.id;


--
-- Name: regime_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.regime_config (
    id bigint NOT NULL,
    universe text NOT NULL,
    exchange text,
    symbol text,
    timeframe text NOT NULL,
    classifier text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: regime_config_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.regime_config_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: regime_config_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.regime_config_id_seq OWNED BY public.regime_config.id;


--
-- Name: regime_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.regime_states (
    id bigint NOT NULL,
    universe text NOT NULL,
    exchange text NOT NULL,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    classifier text NOT NULL,
    regime text NOT NULL,
    confidence numeric(6,4) DEFAULT 0 NOT NULL,
    trend_score numeric(12,6),
    volatility numeric(12,6),
    drawdown numeric(12,6),
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    sample_size integer DEFAULT 0 NOT NULL,
    as_of timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    reason text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: regime_current; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.regime_current AS
 SELECT DISTINCT ON (universe, exchange, symbol, timeframe, classifier) id,
    universe,
    exchange,
    symbol,
    timeframe,
    classifier,
    regime,
    confidence,
    trend_score,
    volatility,
    drawdown,
    features,
    sample_size,
    as_of,
    recorded_at,
    reason,
    metadata
   FROM public.regime_states
  ORDER BY universe, exchange, symbol, timeframe, classifier, as_of DESC;


--
-- Name: regime_states_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.regime_states_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: regime_states_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.regime_states_id_seq OWNED BY public.regime_states.id;


--
-- Name: regime_transitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.regime_transitions (
    id bigint NOT NULL,
    universe text,
    exchange text NOT NULL,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    from_regime text,
    to_regime text NOT NULL,
    transitioned_at timestamp with time zone NOT NULL,
    confidence numeric(6,4) DEFAULT 0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: regime_transitions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.regime_transitions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: regime_transitions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.regime_transitions_id_seq OWNED BY public.regime_transitions.id;


--
-- Name: scout_candidates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scout_candidates (
    id bigint NOT NULL,
    company_id text,
    universe text,
    exchange text NOT NULL,
    symbol text NOT NULL,
    score numeric(10,4) DEFAULT 0 NOT NULL,
    reason text,
    status text DEFAULT 'proposed'::text NOT NULL,
    correlation_id text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scout_candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.scout_candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scout_candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.scout_candidates_id_seq OWNED BY public.scout_candidates.id;


--
-- Name: services_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.services_catalog (
    name text NOT NULL,
    kind text NOT NULL,
    module text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    systemd_unit text NOT NULL,
    enabled_on_vps boolean DEFAULT false NOT NULL,
    has_factory boolean DEFAULT false NOT NULL,
    phase text,
    tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    first_registered_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_systemd_state text,
    last_systemd_substate text,
    last_systemd_active_enter_ts timestamp with time zone,
    last_heartbeat_ts timestamp with time zone,
    last_heartbeat_severity text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: services_catalog_current; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.services_catalog_current AS
 SELECT name,
    kind,
    module,
    description,
    systemd_unit,
    enabled_on_vps,
    has_factory,
    phase,
    tags,
    last_seen_at,
    last_systemd_state,
    last_systemd_substate,
    last_systemd_active_enter_ts,
    last_heartbeat_ts,
    last_heartbeat_severity,
        CASE
            WHEN (last_heartbeat_ts IS NULL) THEN 'no-heartbeat'::text
            WHEN (last_heartbeat_ts < (now() - '00:05:00'::interval)) THEN 'stale'::text
            WHEN (last_heartbeat_severity = ANY (ARRAY['breach'::text, 'critical'::text])) THEN 'degraded'::text
            WHEN (last_heartbeat_severity = 'warning'::text) THEN 'warning'::text
            ELSE 'healthy'::text
        END AS health
   FROM public.services_catalog;


--
-- Name: services_catalog_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.services_catalog_snapshots (
    id bigint NOT NULL,
    name text NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    systemd_state text,
    systemd_substate text,
    systemd_active_enter_ts timestamp with time zone,
    last_heartbeat_ts timestamp with time zone,
    last_heartbeat_severity text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: services_catalog_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.services_catalog_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: services_catalog_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.services_catalog_snapshots_id_seq OWNED BY public.services_catalog_snapshots.id;


--
-- Name: signal_interpretations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.signal_interpretations (
    id bigint NOT NULL,
    news_item_id bigint NOT NULL,
    media_item_id bigint,
    trader_profile_id bigint,
    consensus_direction character varying(20) NOT NULL,
    consensus_confidence numeric(5,4) DEFAULT 0.0 NOT NULL,
    llm_direction character varying(20),
    llm_confidence numeric(5,4),
    llm_raw jsonb,
    quant_direction character varying(20),
    quant_confidence numeric(5,4),
    quant_indicators jsonb,
    instrument_symbol character varying(50),
    instrument_exchange character varying(50),
    market_data_fresh boolean DEFAULT false,
    market_data_at timestamp with time zone,
    model_version character varying(100),
    param_hash character varying(64),
    candle_data_hash character varying(64),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    exchange character varying(50),
    llm_reasoning character varying(500),
    llm_levels jsonb,
    consensus_method character varying(50),
    llm_cost_usd numeric(12,6) DEFAULT 0.0,
    quant_cost_usd numeric(12,6) DEFAULT 0.0,
    prefilter_provider character varying(32),
    prefilter_model character varying(128),
    prefilter_temperature numeric(4,2),
    prefilter_result character varying(32),
    prefilter_cost_usd numeric(20,8) DEFAULT 0,
    vision_provider character varying(32),
    vision_model_requested character varying(128),
    vision_model_resolved character varying(128),
    vision_temperature numeric(4,2),
    prompt_version character varying(32),
    prompt_hash character(16),
    llm_raw_request_path text,
    llm_raw_response_path text,
    trader_stated_thesis text,
    llm_inferred_thesis text,
    reason_agreement_score numeric(4,3),
    pattern_tags jsonb,
    setup_tags jsonb,
    regime_tags jsonb,
    session_tags jsonb,
    instrument_symbol_normalised character varying(64),
    correlation_id character varying(36),
    timeframe character varying(8),
    chart_analysis jsonb,
    trader_trades jsonb,
    chart_hacker_trades jsonb,
    ai_agreement_score numeric(3,2),
    ai_comment text,
    CONSTRAINT si_chart_hacker_trades_array_check CHECK (((chart_hacker_trades IS NULL) OR (jsonb_typeof(chart_hacker_trades) = 'array'::text))),
    CONSTRAINT si_trader_trades_array_check CHECK (((trader_trades IS NULL) OR (jsonb_typeof(trader_trades) = 'array'::text))),
    CONSTRAINT signal_interpretations_consensus_direction_check CHECK (((consensus_direction)::text = ANY ((ARRAY['long'::character varying, 'short'::character varying, 'unclear'::character varying])::text[])))
);


--
-- Name: signal_interpretations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.signal_interpretations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: signal_interpretations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.signal_interpretations_id_seq OWNED BY public.signal_interpretations.id;


--
-- Name: strategies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strategies (
    id bigint NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    instrument_id bigint,
    asset_class public.asset_class_t,
    conflict_resolution public.conflict_resolution_t DEFAULT 'sharpe'::public.conflict_resolution_t NOT NULL,
    halt_threshold_pct numeric(5,2) DEFAULT 80.00 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    is_archived boolean DEFAULT false NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: strategies_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategies_id_seq OWNED BY public.strategies.id;


--
-- Name: strategy_descriptors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strategy_descriptors (
    id bigint NOT NULL,
    company_id text,
    name text NOT NULL,
    kind text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: strategy_descriptors_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategy_descriptors_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategy_descriptors_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategy_descriptors_id_seq OWNED BY public.strategy_descriptors.id;


--
-- Name: strategy_dna_strands; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strategy_dna_strands (
    id bigint NOT NULL,
    strategy_id bigint NOT NULL,
    indicator_catalog_id integer NOT NULL,
    timeframe public.timeframe_t DEFAULT '5m'::public.timeframe_t NOT NULL,
    params jsonb,
    params_hash character(64),
    source_backtest_id bigint,
    priority integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: strategy_dna_strands_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategy_dna_strands_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategy_dna_strands_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategy_dna_strands_id_seq OWNED BY public.strategy_dna_strands.id;


--
-- Name: strategy_intents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strategy_intents (
    id bigint NOT NULL,
    company_id text,
    strategy_name text NOT NULL,
    strategy_kind text NOT NULL,
    symbol text NOT NULL,
    side text NOT NULL,
    venue text,
    size_base numeric(20,10) NOT NULL,
    notional_usd numeric(20,4) DEFAULT 0 NOT NULL,
    reference_price numeric(20,10),
    status text DEFAULT 'pending'::text NOT NULL,
    decision_reason text,
    order_id bigint,
    correlation_id text,
    source_ref text,
    priority_score numeric(12,4) DEFAULT 0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    proposed_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    submitted_at timestamp with time zone
);


--
-- Name: strategy_intents_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategy_intents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategy_intents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategy_intents_id_seq OWNED BY public.strategy_intents.id;


--
-- Name: strategy_intents_latest; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.strategy_intents_latest AS
 SELECT DISTINCT ON (strategy_name, symbol, side) id,
    company_id,
    strategy_name,
    strategy_kind,
    symbol,
    side,
    venue,
    size_base,
    notional_usd,
    reference_price,
    status,
    decision_reason,
    order_id,
    correlation_id,
    source_ref,
    priority_score,
    metadata,
    proposed_at,
    decided_at,
    submitted_at
   FROM public.strategy_intents
  ORDER BY strategy_name, symbol, side, proposed_at DESC;


--
-- Name: strategy_windows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strategy_windows (
    id bigint NOT NULL,
    strategy_id bigint NOT NULL,
    window_close_time time without time zone NOT NULL,
    allocation_pct numeric(5,2) NOT NULL,
    carry_over_enabled boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: strategy_windows_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategy_windows_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategy_windows_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategy_windows_id_seq OWNED BY public.strategy_windows.id;


--
-- Name: system_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_config (
    id integer NOT NULL,
    namespace character varying(50) NOT NULL,
    config_key character varying(100) NOT NULL,
    config_value text,
    is_secret boolean DEFAULT false NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: system_config_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.system_config_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: system_config_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.system_config_id_seq OWNED BY public.system_config.id;


--
-- Name: table_writers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.table_writers (
    table_name text NOT NULL,
    allowed_writer_services text[] NOT NULL,
    notes text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tracked_positions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tracked_positions (
    id bigint NOT NULL,
    news_item_id bigint NOT NULL,
    media_item_id bigint,
    trader_profile_id bigint NOT NULL,
    signal_interpretation_id bigint,
    instrument_symbol character varying(50) NOT NULL,
    instrument_exchange character varying(50) DEFAULT 'bybit'::character varying NOT NULL,
    epic_code character varying(50),
    direction character varying(8) NOT NULL,
    entry_price numeric(20,8),
    stop_loss numeric(20,8),
    take_profit_1 numeric(20,8),
    take_profit_2 numeric(20,8),
    take_profit_3 numeric(20,8),
    position_size numeric(20,8),
    leverage numeric(5,2),
    detection_method character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    detection_confidence numeric(5,4) DEFAULT 0.0 NOT NULL,
    raw_signal_text text,
    signal_timestamp timestamp(3) with time zone NOT NULL,
    status character varying(16) DEFAULT 'open'::character varying NOT NULL,
    status_reason character varying(100),
    current_price numeric(20,8),
    price_updated_at timestamp(3) with time zone,
    highest_price numeric(20,8),
    lowest_price numeric(20,8),
    unrealized_pnl_pct numeric(10,4),
    unrealized_pnl_usd numeric(20,8),
    realized_pnl_pct numeric(10,4) DEFAULT 0 NOT NULL,
    realized_pnl_usd numeric(20,8) DEFAULT 0 NOT NULL,
    max_drawdown_pct numeric(10,4) DEFAULT 0 NOT NULL,
    max_profit_pct numeric(10,4) DEFAULT 0 NOT NULL,
    distance_to_entry_pct numeric(10,4),
    distance_to_sl_pct numeric(10,4),
    distance_to_tp1_pct numeric(10,4),
    risk_reward_ratio numeric(10,4),
    time_in_trade_minutes integer DEFAULT 0 NOT NULL,
    time_to_tp1_minutes integer,
    time_to_sl_minutes integer,
    expiry_at timestamp(3) with time zone,
    outcome character varying(16),
    exit_price numeric(20,8),
    exit_timestamp timestamp(3) with time zone,
    exit_reason text,
    notional_usd numeric(20,8) DEFAULT 1000.0 NOT NULL,
    company_id character varying(50) NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    actor_type character varying(32),
    actor_id character varying(128),
    department character varying(64),
    position_kind character varying(32),
    asset_class character varying(32),
    venue character varying(64),
    legs jsonb,
    sl_history jsonb,
    partial_closes jsonb,
    entry_reason_trader text,
    entry_reason_llm text,
    entry_reason_agent text,
    entry_reason_frozen_at timestamp(3) with time zone,
    exit_reason_trader text,
    exit_reason_llm text,
    exit_reason_system text,
    closed_at timestamp(3) with time zone,
    realized_pnl_usd_final numeric(20,8),
    postmortem_status character varying(32) DEFAULT 'pending'::character varying,
    correlation_id character varying(36),
    signal_source character varying(20) DEFAULT 'trader'::character varying NOT NULL,
    trade_type character varying(20),
    timeframe character varying(8),
    take_profit_4 numeric(20,8),
    take_profit_5 numeric(20,8),
    take_profit_6 numeric(20,8),
    CONSTRAINT tracked_positions_detection_method_check CHECK (((detection_method)::text = ANY ((ARRAY['manual'::character varying, 'llm_vision'::character varying, 'text_parser'::character varying, 'quant_pattern'::character varying, 'agent_override'::character varying])::text[]))),
    CONSTRAINT tracked_positions_direction_check CHECK (((direction)::text = ANY ((ARRAY['long'::character varying, 'short'::character varying])::text[]))),
    CONSTRAINT tracked_positions_outcome_check CHECK (((outcome)::text = ANY ((ARRAY['tp1_hit'::character varying, 'tp2_hit'::character varying, 'tp3_hit'::character varying, 'sl_hit'::character varying, 'breakeven'::character varying, 'expired'::character varying, 'manual_close'::character varying, 'invalidated'::character varying])::text[]))),
    CONSTRAINT tracked_positions_signal_source_check CHECK (((signal_source)::text = ANY ((ARRAY['trader'::character varying, 'chart_hacker'::character varying])::text[]))),
    CONSTRAINT tracked_positions_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'partial_exit'::character varying, 'closed'::character varying, 'expired'::character varying, 'invalidated'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: tracked_positions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tracked_positions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: tracked_positions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tracked_positions_id_seq OWNED BY public.tracked_positions.id;


--
-- Name: trader_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trader_profiles (
    id bigint NOT NULL,
    platform character varying(32) NOT NULL,
    handle_raw character varying(255) NOT NULL,
    handle_normalized character varying(255) NOT NULL,
    display_name character varying(255),
    trader_type character varying(32) DEFAULT 'unknown'::character varying NOT NULL,
    primary_asset_class character varying(32),
    primary_timeframe character varying(16),
    accuracy_score numeric(5,4) DEFAULT NULL::numeric,
    accuracy_samples integer DEFAULT 0 NOT NULL,
    avg_confidence numeric(5,4) DEFAULT NULL::numeric,
    last_scored_at timestamp(3) with time zone,
    first_seen_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_seen_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    notes text,
    metadata jsonb,
    CONSTRAINT trader_profiles_platform_check CHECK (((platform)::text = ANY ((ARRAY['discord'::character varying, 'telegram'::character varying, 'twitter'::character varying, 'tradingview'::character varying, 'rss'::character varying, 'api'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT trader_profiles_primary_asset_class_check CHECK (((primary_asset_class)::text = ANY ((ARRAY['crypto'::character varying, 'cfd'::character varying, 'stock'::character varying, 'forex'::character varying, 'commodity'::character varying, 'index'::character varying])::text[]))),
    CONSTRAINT trader_profiles_primary_timeframe_check CHECK (((primary_timeframe)::text = ANY ((ARRAY['scalping'::character varying, 'intraday'::character varying, 'swing'::character varying, 'position'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT trader_profiles_trader_type_check CHECK (((trader_type)::text = ANY ((ARRAY['pro'::character varying, 'amateur'::character varying, 'bot'::character varying, 'news'::character varying, 'unknown'::character varying])::text[])))
);


--
-- Name: trader_profiles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trader_profiles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trader_profiles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trader_profiles_id_seq OWNED BY public.trader_profiles.id;


--
-- Name: treasury_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.treasury_decisions (
    id bigint NOT NULL,
    company_id text NOT NULL,
    strategy_id text,
    agent_id text,
    exchange text NOT NULL,
    symbol text NOT NULL,
    direction text NOT NULL,
    intent_hash text NOT NULL,
    approved boolean NOT NULL,
    reasons text[] DEFAULT '{}'::text[] NOT NULL,
    capability_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    requested_notional_usd numeric(20,8),
    approved_notional_usd numeric(20,8),
    available_capital_usd numeric(20,8),
    ts timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: treasury_decisions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.treasury_decisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: treasury_decisions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.treasury_decisions_id_seq OWNED BY public.treasury_decisions.id;


--
-- Name: venues; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.venues (
    id integer NOT NULL,
    code character varying(32) NOT NULL,
    display_name character varying(128) NOT NULL,
    venue_type character varying(24) NOT NULL,
    adapter character varying(32) NOT NULL,
    ccxt_id character varying(32),
    supports_spot boolean DEFAULT false NOT NULL,
    supports_perp boolean DEFAULT false NOT NULL,
    supports_margin boolean DEFAULT false NOT NULL,
    api_base_url text,
    ws_base_url text,
    priority smallint DEFAULT 100 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    notes jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: TABLE venues; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.venues IS 'Phase 14: exchange / broker / data-provider registry. One row per source we can read or trade on. Loader looks up by code.';


--
-- Name: v_asset_venues; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_asset_venues AS
 SELECT a.id AS asset_id,
    a.symbol AS asset_symbol,
    a.display_name AS asset_name,
    a.asset_class,
    v.id AS venue_id,
    v.code AS venue_code,
    v.display_name AS venue_name,
    v.venue_type,
    v.adapter,
    v.priority AS venue_priority,
    i.id AS instrument_id,
    i.symbol AS venue_symbol,
    i.min_size,
    i.size_increment,
    i.contract_multiplier,
    i.spread_pct,
    i.maker_fee_pct,
    i.taker_fee_pct,
    i.overnight_funding_long_pct,
    i.overnight_funding_short_pct,
    i.max_leverage,
    i.is_active AS instrument_active,
    v.is_active AS venue_active
   FROM ((public.instruments i
     JOIN public.assets a ON ((a.id = i.asset_id)))
     JOIN public.venues v ON ((v.id = i.venue_id)))
  WHERE (i.is_active AND v.is_active AND (a.alias_of_id IS NULL));


--
-- Name: VIEW v_asset_venues; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.v_asset_venues IS 'Phase 14: one row per asset x venue for arbitrage price-spread analysis. Filters out inactive rows and asset aliases.';


--
-- Name: venues_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.venues_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: venues_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.venues_id_seq OWNED BY public.venues.id;


--
-- Name: watched_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.watched_channels (
    id bigint NOT NULL,
    collector_id bigint NOT NULL,
    platform_channel_id character varying(100) NOT NULL,
    channel_name character varying(255) NOT NULL,
    channel_slug character varying(100) NOT NULL,
    collect_text boolean DEFAULT true NOT NULL,
    collect_images boolean DEFAULT true NOT NULL,
    collect_charts boolean DEFAULT true NOT NULL,
    collect_videos boolean DEFAULT false NOT NULL,
    collect_voice boolean DEFAULT false NOT NULL,
    min_confidence numeric(3,2) DEFAULT 0.0 NOT NULL,
    allowed_keywords text[],
    blocked_keywords text[],
    instrument_patterns jsonb DEFAULT '[]'::jsonb NOT NULL,
    default_instrument character varying(50),
    default_exchange character varying(50),
    max_messages_per_run integer DEFAULT 200 NOT NULL,
    group_window_seconds integer DEFAULT 60 NOT NULL,
    download_media boolean DEFAULT true NOT NULL,
    media_retention_days integer DEFAULT 30 NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: watched_channels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.watched_channels_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: watched_channels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.watched_channels_id_seq OWNED BY public.watched_channels.id;


--
-- Name: watched_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.watched_users (
    id bigint NOT NULL,
    channel_id bigint NOT NULL,
    trader_profile_id bigint,
    platform_user_id character varying(100) NOT NULL,
    username_raw character varying(255) NOT NULL,
    username_normalized character varying(255) NOT NULL,
    display_name character varying(255),
    track_trades boolean DEFAULT true NOT NULL,
    track_charts boolean DEFAULT true NOT NULL,
    track_commentary boolean DEFAULT true NOT NULL,
    track_advice boolean DEFAULT true NOT NULL,
    trade_detection_confidence numeric(3,2) DEFAULT 0.7 NOT NULL,
    trader_type character varying(32),
    primary_asset_class character varying(32),
    primary_timeframe character varying(16),
    signal_weight numeric(5,4) DEFAULT 1.0 NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    first_seen_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_seen_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT watched_users_primary_asset_class_check CHECK (((primary_asset_class)::text = ANY ((ARRAY['crypto'::character varying, 'cfd'::character varying, 'stock'::character varying, 'forex'::character varying, 'commodity'::character varying, 'index'::character varying])::text[]))),
    CONSTRAINT watched_users_primary_timeframe_check CHECK (((primary_timeframe)::text = ANY ((ARRAY['scalping'::character varying, 'intraday'::character varying, 'swing'::character varying, 'position'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT watched_users_trader_type_check CHECK (((trader_type)::text = ANY ((ARRAY['pro'::character varying, 'amateur'::character varying, 'bot'::character varying, 'news'::character varying, 'unknown'::character varying])::text[])))
);


--
-- Name: watched_users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.watched_users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: watched_users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.watched_users_id_seq OWNED BY public.watched_users.id;


--
-- Name: candles_2024_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_01 FOR VALUES FROM ('2024-01-01 00:00:00+01') TO ('2024-02-01 00:00:00+01');


--
-- Name: candles_2024_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_02 FOR VALUES FROM ('2024-02-01 00:00:00+01') TO ('2024-03-01 00:00:00+01');


--
-- Name: candles_2024_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_03 FOR VALUES FROM ('2024-03-01 00:00:00+01') TO ('2024-04-01 00:00:00+02');


--
-- Name: candles_2024_04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_04 FOR VALUES FROM ('2024-04-01 00:00:00+02') TO ('2024-05-01 00:00:00+02');


--
-- Name: candles_2024_05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_05 FOR VALUES FROM ('2024-05-01 00:00:00+02') TO ('2024-06-01 00:00:00+02');


--
-- Name: candles_2024_06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_06 FOR VALUES FROM ('2024-06-01 00:00:00+02') TO ('2024-07-01 00:00:00+02');


--
-- Name: candles_2024_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_07 FOR VALUES FROM ('2024-07-01 00:00:00+02') TO ('2024-08-01 00:00:00+02');


--
-- Name: candles_2024_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_08 FOR VALUES FROM ('2024-08-01 00:00:00+02') TO ('2024-09-01 00:00:00+02');


--
-- Name: candles_2024_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_09 FOR VALUES FROM ('2024-09-01 00:00:00+02') TO ('2024-10-01 00:00:00+02');


--
-- Name: candles_2024_10; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_10 FOR VALUES FROM ('2024-10-01 00:00:00+02') TO ('2024-11-01 00:00:00+01');


--
-- Name: candles_2024_11; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_11 FOR VALUES FROM ('2024-11-01 00:00:00+01') TO ('2024-12-01 00:00:00+01');


--
-- Name: candles_2024_12; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2024_12 FOR VALUES FROM ('2024-12-01 00:00:00+01') TO ('2025-01-01 00:00:00+01');


--
-- Name: candles_2025_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_01 FOR VALUES FROM ('2025-01-01 00:00:00+01') TO ('2025-02-01 00:00:00+01');


--
-- Name: candles_2025_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_02 FOR VALUES FROM ('2025-02-01 00:00:00+01') TO ('2025-03-01 00:00:00+01');


--
-- Name: candles_2025_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_03 FOR VALUES FROM ('2025-03-01 00:00:00+01') TO ('2025-04-01 00:00:00+02');


--
-- Name: candles_2025_04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_04 FOR VALUES FROM ('2025-04-01 00:00:00+02') TO ('2025-05-01 00:00:00+02');


--
-- Name: candles_2025_05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_05 FOR VALUES FROM ('2025-05-01 00:00:00+02') TO ('2025-06-01 00:00:00+02');


--
-- Name: candles_2025_06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_06 FOR VALUES FROM ('2025-06-01 00:00:00+02') TO ('2025-07-01 00:00:00+02');


--
-- Name: candles_2025_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_07 FOR VALUES FROM ('2025-07-01 00:00:00+02') TO ('2025-08-01 00:00:00+02');


--
-- Name: candles_2025_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_08 FOR VALUES FROM ('2025-08-01 00:00:00+02') TO ('2025-09-01 00:00:00+02');


--
-- Name: candles_2025_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_09 FOR VALUES FROM ('2025-09-01 00:00:00+02') TO ('2025-10-01 00:00:00+02');


--
-- Name: candles_2025_10; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_10 FOR VALUES FROM ('2025-10-01 00:00:00+02') TO ('2025-11-01 00:00:00+01');


--
-- Name: candles_2025_11; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_11 FOR VALUES FROM ('2025-11-01 00:00:00+01') TO ('2025-12-01 00:00:00+01');


--
-- Name: candles_2025_12; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2025_12 FOR VALUES FROM ('2025-12-01 00:00:00+01') TO ('2026-01-01 00:00:00+01');


--
-- Name: candles_2026_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_01 FOR VALUES FROM ('2026-01-01 00:00:00+01') TO ('2026-02-01 00:00:00+01');


--
-- Name: candles_2026_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_02 FOR VALUES FROM ('2026-02-01 00:00:00+01') TO ('2026-03-01 00:00:00+01');


--
-- Name: candles_2026_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_03 FOR VALUES FROM ('2026-03-01 00:00:00+01') TO ('2026-04-01 00:00:00+02');


--
-- Name: candles_2026_04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_04 FOR VALUES FROM ('2026-04-01 00:00:00+02') TO ('2026-05-01 00:00:00+02');


--
-- Name: candles_2026_05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_05 FOR VALUES FROM ('2026-05-01 00:00:00+02') TO ('2026-06-01 00:00:00+02');


--
-- Name: candles_2026_06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_06 FOR VALUES FROM ('2026-06-01 00:00:00+02') TO ('2026-07-01 00:00:00+02');


--
-- Name: candles_2026_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_07 FOR VALUES FROM ('2026-07-01 00:00:00+02') TO ('2026-08-01 00:00:00+02');


--
-- Name: candles_2026_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_08 FOR VALUES FROM ('2026-08-01 00:00:00+02') TO ('2026-09-01 00:00:00+02');


--
-- Name: candles_2026_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_09 FOR VALUES FROM ('2026-09-01 00:00:00+02') TO ('2026-10-01 00:00:00+02');


--
-- Name: candles_2026_10; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_10 FOR VALUES FROM ('2026-10-01 00:00:00+02') TO ('2026-11-01 00:00:00+01');


--
-- Name: candles_2026_11; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_11 FOR VALUES FROM ('2026-11-01 00:00:00+01') TO ('2026-12-01 00:00:00+01');


--
-- Name: candles_2026_12; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_2026_12 FOR VALUES FROM ('2026-12-01 00:00:00+01') TO ('2027-01-01 00:00:00+01');


--
-- Name: candles_future; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ATTACH PARTITION public.candles_future FOR VALUES FROM ('2027-01-01 00:00:00+01') TO ('9999-12-31 00:00:00+01');


--
-- Name: agent_decisions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_decisions ALTER COLUMN id SET DEFAULT nextval('public.agent_decisions_id_seq'::regclass);


--
-- Name: agent_opinions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_opinions ALTER COLUMN id SET DEFAULT nextval('public.agent_opinions_id_seq'::regclass);


--
-- Name: agent_personas id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_personas ALTER COLUMN id SET DEFAULT nextval('public.agent_personas_id_seq'::regclass);


--
-- Name: agent_prompts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_prompts ALTER COLUMN id SET DEFAULT nextval('public.agent_prompts_id_seq'::regclass);


--
-- Name: alt_data_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alt_data_items ALTER COLUMN id SET DEFAULT nextval('public.alt_data_items_id_seq'::regclass);


--
-- Name: api_cost_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_cost_log ALTER COLUMN id SET DEFAULT nextval('public.api_cost_log_id_seq'::regclass);


--
-- Name: arb_opportunities id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.arb_opportunities ALTER COLUMN id SET DEFAULT nextval('public.arb_opportunities_id_seq'::regclass);


--
-- Name: arb_venues id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.arb_venues ALTER COLUMN id SET DEFAULT nextval('public.arb_venues_id_seq'::regclass);


--
-- Name: assets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets ALTER COLUMN id SET DEFAULT nextval('public.assets_id_seq'::regclass);


--
-- Name: backtest_queue id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_queue ALTER COLUMN id SET DEFAULT nextval('public.backtest_queue_id_seq'::regclass);


--
-- Name: backtest_results id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_results ALTER COLUMN id SET DEFAULT nextval('public.backtest_results_id_seq'::regclass);


--
-- Name: backtest_submissions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_submissions ALTER COLUMN id SET DEFAULT nextval('public.backtest_submissions_id_seq'::regclass);


--
-- Name: backtest_trade_details id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_trade_details ALTER COLUMN id SET DEFAULT nextval('public.backtest_trade_details_id_seq'::regclass);


--
-- Name: banker_balances id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banker_balances ALTER COLUMN id SET DEFAULT nextval('public.banker_balances_id_seq'::regclass);


--
-- Name: candles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ALTER COLUMN id SET DEFAULT nextval('public.candles_id_seq'::regclass);


--
-- Name: capabilities id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.capabilities ALTER COLUMN id SET DEFAULT nextval('public.capabilities_id_seq'::regclass);


--
-- Name: collector_catalog id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collector_catalog ALTER COLUMN id SET DEFAULT nextval('public.collector_catalog_id_seq'::regclass);


--
-- Name: collector_sources id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collector_sources ALTER COLUMN id SET DEFAULT nextval('public.collector_sources_id_seq'::regclass);


--
-- Name: copy_sources id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.copy_sources ALTER COLUMN id SET DEFAULT nextval('public.copy_sources_id_seq'::regclass);


--
-- Name: copy_trades id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.copy_trades ALTER COLUMN id SET DEFAULT nextval('public.copy_trades_id_seq'::regclass);


--
-- Name: crash_protection_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crash_protection_events ALTER COLUMN id SET DEFAULT nextval('public.crash_protection_events_id_seq'::regclass);


--
-- Name: crash_protection_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crash_protection_rules ALTER COLUMN id SET DEFAULT nextval('public.crash_protection_rules_id_seq'::regclass);


--
-- Name: dashboard_otps id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_otps ALTER COLUMN id SET DEFAULT nextval('public.dashboard_otps_id_seq'::regclass);


--
-- Name: dashboard_sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_sessions ALTER COLUMN id SET DEFAULT nextval('public.dashboard_sessions_id_seq'::regclass);


--
-- Name: dashboard_users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_users ALTER COLUMN id SET DEFAULT nextval('public.dashboard_users_id_seq'::regclass);


--
-- Name: data_sufficiency_reports id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_sufficiency_reports ALTER COLUMN id SET DEFAULT nextval('public.data_sufficiency_reports_id_seq'::regclass);


--
-- Name: derivatives_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.derivatives_snapshots ALTER COLUMN id SET DEFAULT nextval('public.derivatives_snapshots_id_seq'::regclass);


--
-- Name: events_calendar id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events_calendar ALTER COLUMN id SET DEFAULT nextval('public.events_calendar_id_seq'::regclass);


--
-- Name: fills id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fills ALTER COLUMN id SET DEFAULT nextval('public.fills_id_seq'::regclass);


--
-- Name: indicator_catalog id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicator_catalog ALTER COLUMN id SET DEFAULT nextval('public.indicator_catalog_id_seq'::regclass);


--
-- Name: indicators id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicators ALTER COLUMN id SET DEFAULT nextval('public.indicators_id_seq'::regclass);


--
-- Name: instrument_aliases id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instrument_aliases ALTER COLUMN id SET DEFAULT nextval('public.instrument_aliases_id_seq'::regclass);


--
-- Name: instruments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruments ALTER COLUMN id SET DEFAULT nextval('public.instruments_id_seq'::regclass);


--
-- Name: leverage_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leverage_history ALTER COLUMN id SET DEFAULT nextval('public.leverage_history_id_seq'::regclass);


--
-- Name: mcp_invocations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_invocations ALTER COLUMN id SET DEFAULT nextval('public.mcp_invocations_id_seq'::regclass);


--
-- Name: mcp_tool_requests id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_tool_requests ALTER COLUMN id SET DEFAULT nextval('public.mcp_tool_requests_id_seq'::regclass);


--
-- Name: mcp_tools id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_tools ALTER COLUMN id SET DEFAULT nextval('public.mcp_tools_id_seq'::regclass);


--
-- Name: media_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_items ALTER COLUMN id SET DEFAULT nextval('public.media_items_id_seq'::regclass);


--
-- Name: memu_outbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memu_outbox ALTER COLUMN id SET DEFAULT nextval('public.memu_outbox_id_seq'::regclass);


--
-- Name: news_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items ALTER COLUMN id SET DEFAULT nextval('public.news_items_id_seq'::regclass);


--
-- Name: optimiser_candidates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.optimiser_candidates ALTER COLUMN id SET DEFAULT nextval('public.optimiser_candidates_id_seq'::regclass);


--
-- Name: order_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_events ALTER COLUMN id SET DEFAULT nextval('public.order_events_id_seq'::regclass);


--
-- Name: orders id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders ALTER COLUMN id SET DEFAULT nextval('public.orders_id_seq'::regclass);


--
-- Name: paper_wallets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_wallets ALTER COLUMN id SET DEFAULT nextval('public.paper_wallets_id_seq'::regclass);


--
-- Name: position_postmortems id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_postmortems ALTER COLUMN id SET DEFAULT nextval('public.position_postmortems_id_seq'::regclass);


--
-- Name: position_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_snapshots ALTER COLUMN id SET DEFAULT nextval('public.position_snapshots_id_seq'::regclass);


--
-- Name: position_updates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_updates ALTER COLUMN id SET DEFAULT nextval('public.position_updates_id_seq'::regclass);


--
-- Name: prompt_versions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_versions ALTER COLUMN id SET DEFAULT nextval('public.prompt_versions_id_seq'::regclass);


--
-- Name: regime_config id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_config ALTER COLUMN id SET DEFAULT nextval('public.regime_config_id_seq'::regclass);


--
-- Name: regime_states id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_states ALTER COLUMN id SET DEFAULT nextval('public.regime_states_id_seq'::regclass);


--
-- Name: regime_transitions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_transitions ALTER COLUMN id SET DEFAULT nextval('public.regime_transitions_id_seq'::regclass);


--
-- Name: scout_candidates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scout_candidates ALTER COLUMN id SET DEFAULT nextval('public.scout_candidates_id_seq'::regclass);


--
-- Name: services_catalog_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.services_catalog_snapshots ALTER COLUMN id SET DEFAULT nextval('public.services_catalog_snapshots_id_seq'::regclass);


--
-- Name: signal_interpretations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_interpretations ALTER COLUMN id SET DEFAULT nextval('public.signal_interpretations_id_seq'::regclass);


--
-- Name: strategies id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategies ALTER COLUMN id SET DEFAULT nextval('public.strategies_id_seq'::regclass);


--
-- Name: strategy_descriptors id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_descriptors ALTER COLUMN id SET DEFAULT nextval('public.strategy_descriptors_id_seq'::regclass);


--
-- Name: strategy_dna_strands id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_dna_strands ALTER COLUMN id SET DEFAULT nextval('public.strategy_dna_strands_id_seq'::regclass);


--
-- Name: strategy_intents id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_intents ALTER COLUMN id SET DEFAULT nextval('public.strategy_intents_id_seq'::regclass);


--
-- Name: strategy_windows id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_windows ALTER COLUMN id SET DEFAULT nextval('public.strategy_windows_id_seq'::regclass);


--
-- Name: system_config id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_config ALTER COLUMN id SET DEFAULT nextval('public.system_config_id_seq'::regclass);


--
-- Name: tracked_positions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tracked_positions ALTER COLUMN id SET DEFAULT nextval('public.tracked_positions_id_seq'::regclass);


--
-- Name: trader_profiles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trader_profiles ALTER COLUMN id SET DEFAULT nextval('public.trader_profiles_id_seq'::regclass);


--
-- Name: treasury_decisions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.treasury_decisions ALTER COLUMN id SET DEFAULT nextval('public.treasury_decisions_id_seq'::regclass);


--
-- Name: venues id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.venues ALTER COLUMN id SET DEFAULT nextval('public.venues_id_seq'::regclass);


--
-- Name: watched_channels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watched_channels ALTER COLUMN id SET DEFAULT nextval('public.watched_channels_id_seq'::regclass);


--
-- Name: watched_users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watched_users ALTER COLUMN id SET DEFAULT nextval('public.watched_users_id_seq'::regclass);


--
-- Name: agent_decisions agent_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_decisions
    ADD CONSTRAINT agent_decisions_pkey PRIMARY KEY (id);


--
-- Name: agent_opinions agent_opinions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_opinions
    ADD CONSTRAINT agent_opinions_pkey PRIMARY KEY (id);


--
-- Name: agent_personas agent_personas_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_personas
    ADD CONSTRAINT agent_personas_name_key UNIQUE (name);


--
-- Name: agent_personas agent_personas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_personas
    ADD CONSTRAINT agent_personas_pkey PRIMARY KEY (id);


--
-- Name: agent_prompts agent_prompts_persona_id_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_prompts
    ADD CONSTRAINT agent_prompts_persona_id_version_key UNIQUE (persona_id, version);


--
-- Name: agent_prompts agent_prompts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_prompts
    ADD CONSTRAINT agent_prompts_pkey PRIMARY KEY (id);


--
-- Name: alt_data_items alt_data_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alt_data_items
    ADD CONSTRAINT alt_data_items_pkey PRIMARY KEY (id);


--
-- Name: alt_data_items alt_data_items_source_provider_scope_key_metric_as_of_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alt_data_items
    ADD CONSTRAINT alt_data_items_source_provider_scope_key_metric_as_of_key UNIQUE (source, provider, scope_key, metric, as_of);


--
-- Name: api_cost_log api_cost_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_cost_log
    ADD CONSTRAINT api_cost_log_pkey PRIMARY KEY (id);


--
-- Name: arb_opportunities arb_opportunities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.arb_opportunities
    ADD CONSTRAINT arb_opportunities_pkey PRIMARY KEY (id);


--
-- Name: arb_venues arb_venues_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.arb_venues
    ADD CONSTRAINT arb_venues_pkey PRIMARY KEY (id);


--
-- Name: assets assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_pkey PRIMARY KEY (id);


--
-- Name: assets assets_symbol_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_symbol_key UNIQUE (symbol);


--
-- Name: backtest_queue backtest_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_queue
    ADD CONSTRAINT backtest_queue_pkey PRIMARY KEY (id);


--
-- Name: backtest_results backtest_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_results
    ADD CONSTRAINT backtest_results_pkey PRIMARY KEY (id);


--
-- Name: backtest_submissions backtest_submissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_submissions
    ADD CONSTRAINT backtest_submissions_pkey PRIMARY KEY (id);


--
-- Name: backtest_trade_details backtest_trade_details_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_trade_details
    ADD CONSTRAINT backtest_trade_details_pkey PRIMARY KEY (id);


--
-- Name: banker_balances banker_balances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banker_balances
    ADD CONSTRAINT banker_balances_pkey PRIMARY KEY (id);


--
-- Name: candles uq_candles_composite; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles
    ADD CONSTRAINT uq_candles_composite UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_01 candles_2024_01_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_01
    ADD CONSTRAINT candles_2024_01_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles candles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles
    ADD CONSTRAINT candles_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_01 candles_2024_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_01
    ADD CONSTRAINT candles_2024_01_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_02 candles_2024_02_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_02
    ADD CONSTRAINT candles_2024_02_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_02 candles_2024_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_02
    ADD CONSTRAINT candles_2024_02_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_03 candles_2024_03_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_03
    ADD CONSTRAINT candles_2024_03_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_03 candles_2024_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_03
    ADD CONSTRAINT candles_2024_03_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_04 candles_2024_04_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_04
    ADD CONSTRAINT candles_2024_04_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_04 candles_2024_04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_04
    ADD CONSTRAINT candles_2024_04_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_05 candles_2024_05_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_05
    ADD CONSTRAINT candles_2024_05_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_05 candles_2024_05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_05
    ADD CONSTRAINT candles_2024_05_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_06 candles_2024_06_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_06
    ADD CONSTRAINT candles_2024_06_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_06 candles_2024_06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_06
    ADD CONSTRAINT candles_2024_06_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_07 candles_2024_07_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_07
    ADD CONSTRAINT candles_2024_07_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_07 candles_2024_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_07
    ADD CONSTRAINT candles_2024_07_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_08 candles_2024_08_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_08
    ADD CONSTRAINT candles_2024_08_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_08 candles_2024_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_08
    ADD CONSTRAINT candles_2024_08_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_09 candles_2024_09_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_09
    ADD CONSTRAINT candles_2024_09_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_09 candles_2024_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_09
    ADD CONSTRAINT candles_2024_09_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_10 candles_2024_10_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_10
    ADD CONSTRAINT candles_2024_10_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_10 candles_2024_10_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_10
    ADD CONSTRAINT candles_2024_10_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_11 candles_2024_11_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_11
    ADD CONSTRAINT candles_2024_11_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_11 candles_2024_11_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_11
    ADD CONSTRAINT candles_2024_11_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2024_12 candles_2024_12_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_12
    ADD CONSTRAINT candles_2024_12_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2024_12 candles_2024_12_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2024_12
    ADD CONSTRAINT candles_2024_12_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_01 candles_2025_01_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_01
    ADD CONSTRAINT candles_2025_01_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_01 candles_2025_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_01
    ADD CONSTRAINT candles_2025_01_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_02 candles_2025_02_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_02
    ADD CONSTRAINT candles_2025_02_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_02 candles_2025_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_02
    ADD CONSTRAINT candles_2025_02_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_03 candles_2025_03_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_03
    ADD CONSTRAINT candles_2025_03_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_03 candles_2025_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_03
    ADD CONSTRAINT candles_2025_03_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_04 candles_2025_04_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_04
    ADD CONSTRAINT candles_2025_04_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_04 candles_2025_04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_04
    ADD CONSTRAINT candles_2025_04_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_05 candles_2025_05_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_05
    ADD CONSTRAINT candles_2025_05_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_05 candles_2025_05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_05
    ADD CONSTRAINT candles_2025_05_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_06 candles_2025_06_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_06
    ADD CONSTRAINT candles_2025_06_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_06 candles_2025_06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_06
    ADD CONSTRAINT candles_2025_06_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_07 candles_2025_07_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_07
    ADD CONSTRAINT candles_2025_07_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_07 candles_2025_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_07
    ADD CONSTRAINT candles_2025_07_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_08 candles_2025_08_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_08
    ADD CONSTRAINT candles_2025_08_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_08 candles_2025_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_08
    ADD CONSTRAINT candles_2025_08_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_09 candles_2025_09_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_09
    ADD CONSTRAINT candles_2025_09_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_09 candles_2025_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_09
    ADD CONSTRAINT candles_2025_09_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_10 candles_2025_10_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_10
    ADD CONSTRAINT candles_2025_10_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_10 candles_2025_10_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_10
    ADD CONSTRAINT candles_2025_10_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_11 candles_2025_11_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_11
    ADD CONSTRAINT candles_2025_11_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_11 candles_2025_11_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_11
    ADD CONSTRAINT candles_2025_11_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2025_12 candles_2025_12_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_12
    ADD CONSTRAINT candles_2025_12_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2025_12 candles_2025_12_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2025_12
    ADD CONSTRAINT candles_2025_12_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_01 candles_2026_01_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_01
    ADD CONSTRAINT candles_2026_01_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_01 candles_2026_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_01
    ADD CONSTRAINT candles_2026_01_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_02 candles_2026_02_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_02
    ADD CONSTRAINT candles_2026_02_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_02 candles_2026_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_02
    ADD CONSTRAINT candles_2026_02_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_03 candles_2026_03_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_03
    ADD CONSTRAINT candles_2026_03_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_03 candles_2026_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_03
    ADD CONSTRAINT candles_2026_03_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_04 candles_2026_04_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_04
    ADD CONSTRAINT candles_2026_04_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_04 candles_2026_04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_04
    ADD CONSTRAINT candles_2026_04_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_05 candles_2026_05_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_05
    ADD CONSTRAINT candles_2026_05_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_05 candles_2026_05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_05
    ADD CONSTRAINT candles_2026_05_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_06 candles_2026_06_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_06
    ADD CONSTRAINT candles_2026_06_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_06 candles_2026_06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_06
    ADD CONSTRAINT candles_2026_06_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_07 candles_2026_07_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_07
    ADD CONSTRAINT candles_2026_07_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_07 candles_2026_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_07
    ADD CONSTRAINT candles_2026_07_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_08 candles_2026_08_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_08
    ADD CONSTRAINT candles_2026_08_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_08 candles_2026_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_08
    ADD CONSTRAINT candles_2026_08_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_09 candles_2026_09_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_09
    ADD CONSTRAINT candles_2026_09_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_09 candles_2026_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_09
    ADD CONSTRAINT candles_2026_09_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_10 candles_2026_10_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_10
    ADD CONSTRAINT candles_2026_10_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_10 candles_2026_10_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_10
    ADD CONSTRAINT candles_2026_10_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_11 candles_2026_11_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_11
    ADD CONSTRAINT candles_2026_11_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_11 candles_2026_11_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_11
    ADD CONSTRAINT candles_2026_11_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_2026_12 candles_2026_12_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_12
    ADD CONSTRAINT candles_2026_12_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_2026_12 candles_2026_12_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_2026_12
    ADD CONSTRAINT candles_2026_12_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: candles_future candles_future_instrument_id_source_timeframe_timestamp_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_future
    ADD CONSTRAINT candles_future_instrument_id_source_timeframe_timestamp_key UNIQUE (instrument_id, source, timeframe, "timestamp");


--
-- Name: candles_future candles_future_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles_future
    ADD CONSTRAINT candles_future_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: capabilities capabilities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.capabilities
    ADD CONSTRAINT capabilities_pkey PRIMARY KEY (id);


--
-- Name: collector_catalog collector_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collector_catalog
    ADD CONSTRAINT collector_catalog_pkey PRIMARY KEY (id);


--
-- Name: collector_sources collector_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collector_sources
    ADD CONSTRAINT collector_sources_pkey PRIMARY KEY (id);


--
-- Name: contest_participants contest_participants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contest_participants
    ADD CONSTRAINT contest_participants_pkey PRIMARY KEY (contest_id, company_id, agent_id);


--
-- Name: contests contests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contests
    ADD CONSTRAINT contests_pkey PRIMARY KEY (id);


--
-- Name: copy_sources copy_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.copy_sources
    ADD CONSTRAINT copy_sources_pkey PRIMARY KEY (id);


--
-- Name: copy_trades copy_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.copy_trades
    ADD CONSTRAINT copy_trades_pkey PRIMARY KEY (id);


--
-- Name: copy_trades copy_trades_source_id_source_fill_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.copy_trades
    ADD CONSTRAINT copy_trades_source_id_source_fill_id_key UNIQUE (source_id, source_fill_id);


--
-- Name: crash_protection_events crash_protection_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crash_protection_events
    ADD CONSTRAINT crash_protection_events_pkey PRIMARY KEY (id);


--
-- Name: crash_protection_rules crash_protection_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crash_protection_rules
    ADD CONSTRAINT crash_protection_rules_pkey PRIMARY KEY (id);


--
-- Name: cron_heartbeats cron_heartbeats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cron_heartbeats
    ADD CONSTRAINT cron_heartbeats_pkey PRIMARY KEY (agent_id);


--
-- Name: dashboard_otps dashboard_otps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_otps
    ADD CONSTRAINT dashboard_otps_pkey PRIMARY KEY (id);


--
-- Name: dashboard_sessions dashboard_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_sessions
    ADD CONSTRAINT dashboard_sessions_pkey PRIMARY KEY (id);


--
-- Name: dashboard_sessions dashboard_sessions_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_sessions
    ADD CONSTRAINT dashboard_sessions_token_hash_key UNIQUE (token_hash);


--
-- Name: dashboard_users dashboard_users_chat_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_users
    ADD CONSTRAINT dashboard_users_chat_id_key UNIQUE (chat_id);


--
-- Name: dashboard_users dashboard_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_users
    ADD CONSTRAINT dashboard_users_pkey PRIMARY KEY (id);


--
-- Name: data_sufficiency_reports data_sufficiency_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_sufficiency_reports
    ADD CONSTRAINT data_sufficiency_reports_pkey PRIMARY KEY (id);


--
-- Name: derivatives_snapshots derivatives_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.derivatives_snapshots
    ADD CONSTRAINT derivatives_snapshots_pkey PRIMARY KEY (id);


--
-- Name: events_calendar events_calendar_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events_calendar
    ADD CONSTRAINT events_calendar_pkey PRIMARY KEY (id);


--
-- Name: events_calendar events_calendar_provider_dedupe_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events_calendar
    ADD CONSTRAINT events_calendar_provider_dedupe_key_key UNIQUE (provider, dedupe_key);


--
-- Name: fills fills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fills
    ADD CONSTRAINT fills_pkey PRIMARY KEY (id);


--
-- Name: indicator_catalog indicator_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicator_catalog
    ADD CONSTRAINT indicator_catalog_pkey PRIMARY KEY (id);


--
-- Name: indicators indicators_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicators
    ADD CONSTRAINT indicators_pkey PRIMARY KEY (id);


--
-- Name: instrument_aliases instrument_aliases_alias_type_alias_value_instrument_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instrument_aliases
    ADD CONSTRAINT instrument_aliases_alias_type_alias_value_instrument_id_key UNIQUE (alias_type, alias_value, instrument_id);


--
-- Name: instrument_aliases instrument_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instrument_aliases
    ADD CONSTRAINT instrument_aliases_pkey PRIMARY KEY (id);


--
-- Name: instruments instruments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruments
    ADD CONSTRAINT instruments_pkey PRIMARY KEY (id);


--
-- Name: leverage_history leverage_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leverage_history
    ADD CONSTRAINT leverage_history_pkey PRIMARY KEY (id);


--
-- Name: mcp_invocations mcp_invocations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_invocations
    ADD CONSTRAINT mcp_invocations_pkey PRIMARY KEY (id);


--
-- Name: mcp_tool_requests mcp_tool_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_tool_requests
    ADD CONSTRAINT mcp_tool_requests_pkey PRIMARY KEY (id);


--
-- Name: mcp_tools mcp_tools_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_tools
    ADD CONSTRAINT mcp_tools_name_key UNIQUE (name);


--
-- Name: mcp_tools mcp_tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_tools
    ADD CONSTRAINT mcp_tools_pkey PRIMARY KEY (id);


--
-- Name: media_items media_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_items
    ADD CONSTRAINT media_items_pkey PRIMARY KEY (id);


--
-- Name: memu_outbox memu_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memu_outbox
    ADD CONSTRAINT memu_outbox_pkey PRIMARY KEY (id);


--
-- Name: news_items news_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items
    ADD CONSTRAINT news_items_pkey PRIMARY KEY (id);


--
-- Name: optimiser_candidates optimiser_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.optimiser_candidates
    ADD CONSTRAINT optimiser_candidates_pkey PRIMARY KEY (id);


--
-- Name: order_events order_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_events
    ADD CONSTRAINT order_events_pkey PRIMARY KEY (id);


--
-- Name: orders orders_adapter_client_order_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_adapter_client_order_id_key UNIQUE (adapter, client_order_id);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: paper_wallets paper_wallets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_wallets
    ADD CONSTRAINT paper_wallets_pkey PRIMARY KEY (id);


--
-- Name: position_postmortems position_postmortems_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_postmortems
    ADD CONSTRAINT position_postmortems_pkey PRIMARY KEY (id);


--
-- Name: position_snapshots position_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_snapshots
    ADD CONSTRAINT position_snapshots_pkey PRIMARY KEY (id);


--
-- Name: position_updates position_updates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_updates
    ADD CONSTRAINT position_updates_pkey PRIMARY KEY (id);


--
-- Name: prompt_versions prompt_versions_name_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_versions
    ADD CONSTRAINT prompt_versions_name_version_key UNIQUE (name, version);


--
-- Name: prompt_versions prompt_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_versions
    ADD CONSTRAINT prompt_versions_pkey PRIMARY KEY (id);


--
-- Name: regime_config regime_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_config
    ADD CONSTRAINT regime_config_pkey PRIMARY KEY (id);


--
-- Name: regime_config regime_config_universe_exchange_symbol_timeframe_classifier_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_config
    ADD CONSTRAINT regime_config_universe_exchange_symbol_timeframe_classifier_key UNIQUE (universe, exchange, symbol, timeframe, classifier);


--
-- Name: regime_states regime_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_states
    ADD CONSTRAINT regime_states_pkey PRIMARY KEY (id);


--
-- Name: regime_transitions regime_transitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_transitions
    ADD CONSTRAINT regime_transitions_pkey PRIMARY KEY (id);


--
-- Name: scout_candidates scout_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scout_candidates
    ADD CONSTRAINT scout_candidates_pkey PRIMARY KEY (id);


--
-- Name: services_catalog services_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.services_catalog
    ADD CONSTRAINT services_catalog_pkey PRIMARY KEY (name);


--
-- Name: services_catalog_snapshots services_catalog_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.services_catalog_snapshots
    ADD CONSTRAINT services_catalog_snapshots_pkey PRIMARY KEY (id);


--
-- Name: signal_interpretations signal_interpretations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_interpretations
    ADD CONSTRAINT signal_interpretations_pkey PRIMARY KEY (id);


--
-- Name: strategies strategies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategies
    ADD CONSTRAINT strategies_pkey PRIMARY KEY (id);


--
-- Name: strategy_descriptors strategy_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_descriptors
    ADD CONSTRAINT strategy_descriptors_pkey PRIMARY KEY (id);


--
-- Name: strategy_dna_strands strategy_dna_strands_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_dna_strands
    ADD CONSTRAINT strategy_dna_strands_pkey PRIMARY KEY (id);


--
-- Name: strategy_intents strategy_intents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_intents
    ADD CONSTRAINT strategy_intents_pkey PRIMARY KEY (id);


--
-- Name: strategy_windows strategy_windows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_windows
    ADD CONSTRAINT strategy_windows_pkey PRIMARY KEY (id);


--
-- Name: system_config system_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_config
    ADD CONSTRAINT system_config_pkey PRIMARY KEY (id);


--
-- Name: table_writers table_writers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_writers
    ADD CONSTRAINT table_writers_pkey PRIMARY KEY (table_name);


--
-- Name: tracked_positions tracked_positions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tracked_positions
    ADD CONSTRAINT tracked_positions_pkey PRIMARY KEY (id);


--
-- Name: trader_profiles trader_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trader_profiles
    ADD CONSTRAINT trader_profiles_pkey PRIMARY KEY (id);


--
-- Name: treasury_decisions treasury_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.treasury_decisions
    ADD CONSTRAINT treasury_decisions_pkey PRIMARY KEY (id);


--
-- Name: agent_opinions uq_agent_opinion; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_opinions
    ADD CONSTRAINT uq_agent_opinion UNIQUE (position_id, agent_name);


--
-- Name: backtest_results uq_backtest_param_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_results
    ADD CONSTRAINT uq_backtest_param_hash UNIQUE (param_hash);


--
-- Name: capabilities uq_capabilities_scope; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.capabilities
    ADD CONSTRAINT uq_capabilities_scope UNIQUE (company_id, scope_kind, scope_id);


--
-- Name: watched_channels uq_channel_collector_platform; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watched_channels
    ADD CONSTRAINT uq_channel_collector_platform UNIQUE (collector_id, platform_channel_id);


--
-- Name: collector_catalog uq_collector_slug; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collector_catalog
    ADD CONSTRAINT uq_collector_slug UNIQUE (source_slug);


--
-- Name: system_config uq_config_ns_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_config
    ADD CONSTRAINT uq_config_ns_key UNIQUE (namespace, config_key);


--
-- Name: data_sufficiency_reports uq_data_suff_triplet; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_sufficiency_reports
    ADD CONSTRAINT uq_data_suff_triplet UNIQUE (instrument_id, timeframe, profile_name);


--
-- Name: derivatives_snapshots uq_deriv_snapshot; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.derivatives_snapshots
    ADD CONSTRAINT uq_deriv_snapshot UNIQUE (instrument_id, source, snapshot_at);


--
-- Name: indicator_catalog uq_indicator_catalog_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicator_catalog
    ADD CONSTRAINT uq_indicator_catalog_name UNIQUE (name);


--
-- Name: indicators uq_indicator_lookup; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicators
    ADD CONSTRAINT uq_indicator_lookup UNIQUE (instrument_id, timeframe, indicator_name, params_hash);


--
-- Name: instruments uq_instruments_symbol_exchange; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruments
    ADD CONSTRAINT uq_instruments_symbol_exchange UNIQUE (symbol, exchange);


--
-- Name: news_items uq_news_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items
    ADD CONSTRAINT uq_news_hash UNIQUE (hash_key);


--
-- Name: paper_wallets uq_paper_wallets_owner; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_wallets
    ADD CONSTRAINT uq_paper_wallets_owner UNIQUE (company_id, agent_id, exchange);


--
-- Name: tracked_positions uq_position_dedup; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tracked_positions
    ADD CONSTRAINT uq_position_dedup UNIQUE (news_item_id, trader_profile_id, instrument_symbol, direction);


--
-- Name: position_postmortems uq_postmortem_composite; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_postmortems
    ADD CONSTRAINT uq_postmortem_composite UNIQUE (position_id, postmortem_version, prompt_version);


--
-- Name: backtest_queue uq_queue_param_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_queue
    ADD CONSTRAINT uq_queue_param_hash UNIQUE (param_hash);


--
-- Name: strategy_dna_strands uq_strand_dedup; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_dna_strands
    ADD CONSTRAINT uq_strand_dedup UNIQUE (strategy_id, indicator_catalog_id, timeframe, params_hash);


--
-- Name: trader_profiles uq_trader_platform_handle; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trader_profiles
    ADD CONSTRAINT uq_trader_platform_handle UNIQUE (platform, handle_normalized);


--
-- Name: watched_users uq_watched_user_channel_platform; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watched_users
    ADD CONSTRAINT uq_watched_user_channel_platform UNIQUE (channel_id, platform_user_id);


--
-- Name: strategy_windows uq_window_dedup; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_windows
    ADD CONSTRAINT uq_window_dedup UNIQUE (strategy_id, window_close_time);


--
-- Name: venues venues_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.venues
    ADD CONSTRAINT venues_code_key UNIQUE (code);


--
-- Name: venues venues_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.venues
    ADD CONSTRAINT venues_pkey PRIMARY KEY (id);


--
-- Name: watched_channels watched_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watched_channels
    ADD CONSTRAINT watched_channels_pkey PRIMARY KEY (id);


--
-- Name: watched_users watched_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watched_users
    ADD CONSTRAINT watched_users_pkey PRIMARY KEY (id);


--
-- Name: agent_decisions_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_decisions_company_idx ON public.agent_decisions USING btree (company_id, decided_at DESC) WHERE (company_id IS NOT NULL);


--
-- Name: agent_decisions_corr_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_decisions_corr_idx ON public.agent_decisions USING btree (correlation_id, decided_at DESC);


--
-- Name: agent_decisions_persona_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_decisions_persona_idx ON public.agent_decisions USING btree (persona_id, decided_at DESC);


--
-- Name: agent_decisions_verdict_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_decisions_verdict_idx ON public.agent_decisions USING btree (verdict, decided_at DESC);


--
-- Name: alt_data_metric_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX alt_data_metric_ts_idx ON public.alt_data_items USING btree (metric, as_of DESC);


--
-- Name: alt_data_provider_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX alt_data_provider_ts_idx ON public.alt_data_items USING btree (provider, as_of DESC);


--
-- Name: alt_data_scope_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX alt_data_scope_ts_idx ON public.alt_data_items USING btree (scope_key, as_of DESC);


--
-- Name: alt_data_source_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX alt_data_source_ts_idx ON public.alt_data_items USING btree (source, as_of DESC);


--
-- Name: alt_data_symbol_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX alt_data_symbol_ts_idx ON public.alt_data_items USING btree (exchange, symbol, as_of DESC) WHERE (symbol IS NOT NULL);


--
-- Name: arb_opp_score_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX arb_opp_score_idx ON public.arb_opportunities USING btree (net_bps DESC, observed_at DESC);


--
-- Name: arb_opp_symbol_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX arb_opp_symbol_idx ON public.arb_opportunities USING btree (symbol, observed_at DESC);


--
-- Name: arb_venues_unique_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX arb_venues_unique_idx ON public.arb_venues USING btree (name, kind, COALESCE(company_id, ''::text));


--
-- Name: backtest_submissions_client_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX backtest_submissions_client_idx ON public.backtest_submissions USING btree (client_id, submitted_at DESC);


--
-- Name: backtest_submissions_hash_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX backtest_submissions_hash_active_idx ON public.backtest_submissions USING btree (spec_hash) WHERE (status = ANY (ARRAY['submitted'::text, 'queued'::text, 'running'::text, 'completed'::text]));


--
-- Name: backtest_submissions_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX backtest_submissions_status_idx ON public.backtest_submissions USING btree (status, submitted_at DESC);


--
-- Name: idx_candles_instrument_tf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_candles_instrument_tf ON ONLY public.candles USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_01_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_01_instrument_id_timeframe_idx ON public.candles_2024_01 USING btree (instrument_id, timeframe);


--
-- Name: idx_candles_timestamp_brin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_candles_timestamp_brin ON ONLY public.candles USING brin ("timestamp");


--
-- Name: candles_2024_01_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_01_timestamp_idx ON public.candles_2024_01 USING brin ("timestamp");


--
-- Name: candles_2024_02_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_02_instrument_id_timeframe_idx ON public.candles_2024_02 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_02_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_02_timestamp_idx ON public.candles_2024_02 USING brin ("timestamp");


--
-- Name: candles_2024_03_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_03_instrument_id_timeframe_idx ON public.candles_2024_03 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_03_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_03_timestamp_idx ON public.candles_2024_03 USING brin ("timestamp");


--
-- Name: candles_2024_04_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_04_instrument_id_timeframe_idx ON public.candles_2024_04 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_04_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_04_timestamp_idx ON public.candles_2024_04 USING brin ("timestamp");


--
-- Name: candles_2024_05_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_05_instrument_id_timeframe_idx ON public.candles_2024_05 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_05_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_05_timestamp_idx ON public.candles_2024_05 USING brin ("timestamp");


--
-- Name: candles_2024_06_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_06_instrument_id_timeframe_idx ON public.candles_2024_06 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_06_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_06_timestamp_idx ON public.candles_2024_06 USING brin ("timestamp");


--
-- Name: candles_2024_07_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_07_instrument_id_timeframe_idx ON public.candles_2024_07 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_07_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_07_timestamp_idx ON public.candles_2024_07 USING brin ("timestamp");


--
-- Name: candles_2024_08_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_08_instrument_id_timeframe_idx ON public.candles_2024_08 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_08_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_08_timestamp_idx ON public.candles_2024_08 USING brin ("timestamp");


--
-- Name: candles_2024_09_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_09_instrument_id_timeframe_idx ON public.candles_2024_09 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_09_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_09_timestamp_idx ON public.candles_2024_09 USING brin ("timestamp");


--
-- Name: candles_2024_10_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_10_instrument_id_timeframe_idx ON public.candles_2024_10 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_10_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_10_timestamp_idx ON public.candles_2024_10 USING brin ("timestamp");


--
-- Name: candles_2024_11_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_11_instrument_id_timeframe_idx ON public.candles_2024_11 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_11_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_11_timestamp_idx ON public.candles_2024_11 USING brin ("timestamp");


--
-- Name: candles_2024_12_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_12_instrument_id_timeframe_idx ON public.candles_2024_12 USING btree (instrument_id, timeframe);


--
-- Name: candles_2024_12_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2024_12_timestamp_idx ON public.candles_2024_12 USING brin ("timestamp");


--
-- Name: candles_2025_01_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_01_instrument_id_timeframe_idx ON public.candles_2025_01 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_01_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_01_timestamp_idx ON public.candles_2025_01 USING brin ("timestamp");


--
-- Name: candles_2025_02_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_02_instrument_id_timeframe_idx ON public.candles_2025_02 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_02_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_02_timestamp_idx ON public.candles_2025_02 USING brin ("timestamp");


--
-- Name: candles_2025_03_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_03_instrument_id_timeframe_idx ON public.candles_2025_03 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_03_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_03_timestamp_idx ON public.candles_2025_03 USING brin ("timestamp");


--
-- Name: candles_2025_04_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_04_instrument_id_timeframe_idx ON public.candles_2025_04 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_04_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_04_timestamp_idx ON public.candles_2025_04 USING brin ("timestamp");


--
-- Name: candles_2025_05_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_05_instrument_id_timeframe_idx ON public.candles_2025_05 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_05_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_05_timestamp_idx ON public.candles_2025_05 USING brin ("timestamp");


--
-- Name: candles_2025_06_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_06_instrument_id_timeframe_idx ON public.candles_2025_06 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_06_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_06_timestamp_idx ON public.candles_2025_06 USING brin ("timestamp");


--
-- Name: candles_2025_07_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_07_instrument_id_timeframe_idx ON public.candles_2025_07 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_07_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_07_timestamp_idx ON public.candles_2025_07 USING brin ("timestamp");


--
-- Name: candles_2025_08_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_08_instrument_id_timeframe_idx ON public.candles_2025_08 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_08_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_08_timestamp_idx ON public.candles_2025_08 USING brin ("timestamp");


--
-- Name: candles_2025_09_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_09_instrument_id_timeframe_idx ON public.candles_2025_09 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_09_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_09_timestamp_idx ON public.candles_2025_09 USING brin ("timestamp");


--
-- Name: candles_2025_10_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_10_instrument_id_timeframe_idx ON public.candles_2025_10 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_10_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_10_timestamp_idx ON public.candles_2025_10 USING brin ("timestamp");


--
-- Name: candles_2025_11_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_11_instrument_id_timeframe_idx ON public.candles_2025_11 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_11_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_11_timestamp_idx ON public.candles_2025_11 USING brin ("timestamp");


--
-- Name: candles_2025_12_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_12_instrument_id_timeframe_idx ON public.candles_2025_12 USING btree (instrument_id, timeframe);


--
-- Name: candles_2025_12_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2025_12_timestamp_idx ON public.candles_2025_12 USING brin ("timestamp");


--
-- Name: candles_2026_01_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_01_instrument_id_timeframe_idx ON public.candles_2026_01 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_01_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_01_timestamp_idx ON public.candles_2026_01 USING brin ("timestamp");


--
-- Name: candles_2026_02_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_02_instrument_id_timeframe_idx ON public.candles_2026_02 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_02_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_02_timestamp_idx ON public.candles_2026_02 USING brin ("timestamp");


--
-- Name: candles_2026_03_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_03_instrument_id_timeframe_idx ON public.candles_2026_03 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_03_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_03_timestamp_idx ON public.candles_2026_03 USING brin ("timestamp");


--
-- Name: candles_2026_04_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_04_instrument_id_timeframe_idx ON public.candles_2026_04 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_04_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_04_timestamp_idx ON public.candles_2026_04 USING brin ("timestamp");


--
-- Name: candles_2026_05_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_05_instrument_id_timeframe_idx ON public.candles_2026_05 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_05_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_05_timestamp_idx ON public.candles_2026_05 USING brin ("timestamp");


--
-- Name: candles_2026_06_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_06_instrument_id_timeframe_idx ON public.candles_2026_06 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_06_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_06_timestamp_idx ON public.candles_2026_06 USING brin ("timestamp");


--
-- Name: candles_2026_07_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_07_instrument_id_timeframe_idx ON public.candles_2026_07 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_07_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_07_timestamp_idx ON public.candles_2026_07 USING brin ("timestamp");


--
-- Name: candles_2026_08_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_08_instrument_id_timeframe_idx ON public.candles_2026_08 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_08_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_08_timestamp_idx ON public.candles_2026_08 USING brin ("timestamp");


--
-- Name: candles_2026_09_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_09_instrument_id_timeframe_idx ON public.candles_2026_09 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_09_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_09_timestamp_idx ON public.candles_2026_09 USING brin ("timestamp");


--
-- Name: candles_2026_10_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_10_instrument_id_timeframe_idx ON public.candles_2026_10 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_10_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_10_timestamp_idx ON public.candles_2026_10 USING brin ("timestamp");


--
-- Name: candles_2026_11_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_11_instrument_id_timeframe_idx ON public.candles_2026_11 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_11_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_11_timestamp_idx ON public.candles_2026_11 USING brin ("timestamp");


--
-- Name: candles_2026_12_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_12_instrument_id_timeframe_idx ON public.candles_2026_12 USING btree (instrument_id, timeframe);


--
-- Name: candles_2026_12_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_2026_12_timestamp_idx ON public.candles_2026_12 USING brin ("timestamp");


--
-- Name: candles_future_instrument_id_timeframe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_future_instrument_id_timeframe_idx ON public.candles_future USING btree (instrument_id, timeframe);


--
-- Name: candles_future_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX candles_future_timestamp_idx ON public.candles_future USING brin ("timestamp");


--
-- Name: contest_participants_agent_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX contest_participants_agent_idx ON public.contest_participants USING btree (company_id, agent_id);


--
-- Name: contests_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX contests_status_idx ON public.contests USING btree (status, ends_at);


--
-- Name: copy_sources_unique_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX copy_sources_unique_idx ON public.copy_sources USING btree (kind, COALESCE(venue, ''::text), identifier, COALESCE(company_id, ''::text));


--
-- Name: copy_trades_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX copy_trades_status_idx ON public.copy_trades USING btree (status, created_at DESC);


--
-- Name: crash_events_rule_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX crash_events_rule_ts_idx ON public.crash_protection_events USING btree (rule_id, ts DESC);


--
-- Name: crash_events_scope_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX crash_events_scope_status_idx ON public.crash_protection_events USING btree (company_id, universe, exchange, symbol, status, ts DESC);


--
-- Name: crash_events_status_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX crash_events_status_ts_idx ON public.crash_protection_events USING btree (status, ts DESC);


--
-- Name: crash_rules_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX crash_rules_lookup_idx ON public.crash_protection_rules USING btree (company_id, universe, exchange, symbol, rule_type) WHERE enabled;


--
-- Name: crash_rules_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX crash_rules_type_idx ON public.crash_protection_rules USING btree (rule_type, enabled);


--
-- Name: dashboard_otps_chat_expires_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dashboard_otps_chat_expires_idx ON public.dashboard_otps USING btree (chat_id, expires_at DESC);


--
-- Name: dashboard_sessions_chat_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dashboard_sessions_chat_idx ON public.dashboard_sessions USING btree (chat_id, issued_at DESC);


--
-- Name: events_calendar_importance_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_calendar_importance_idx ON public.events_calendar USING btree (importance, event_time);


--
-- Name: events_calendar_kind_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_calendar_kind_time_idx ON public.events_calendar USING btree (kind, event_time);


--
-- Name: events_calendar_symbol_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_calendar_symbol_time_idx ON public.events_calendar USING btree (exchange, symbol, event_time) WHERE (symbol IS NOT NULL);


--
-- Name: events_calendar_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_calendar_time_idx ON public.events_calendar USING btree (event_time);


--
-- Name: events_calendar_universe_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_calendar_universe_time_idx ON public.events_calendar USING btree (universe, event_time) WHERE (universe IS NOT NULL);


--
-- Name: fills_company_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fills_company_ts_idx ON public.fills USING btree (company_id, ts DESC);


--
-- Name: fills_order_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fills_order_idx ON public.fills USING btree (order_id);


--
-- Name: fills_symbol_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fills_symbol_ts_idx ON public.fills USING btree (exchange, symbol, ts DESC);


--
-- Name: idx_agent_opinion_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_opinion_agent ON public.agent_opinions USING btree (agent_name, created_at DESC);


--
-- Name: idx_agent_opinion_delta; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_opinion_delta ON public.agent_opinions USING btree (performance_delta) WHERE (performance_delta IS NOT NULL);


--
-- Name: idx_agent_opinion_position; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_opinion_position ON public.agent_opinions USING btree (position_id);


--
-- Name: idx_aliases_type_value; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aliases_type_value ON public.instrument_aliases USING btree (alias_type, alias_value);


--
-- Name: idx_aliases_value; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aliases_value ON public.instrument_aliases USING btree (alias_value);


--
-- Name: idx_assets_alias_of; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_alias_of ON public.assets USING btree (alias_of_id) WHERE (alias_of_id IS NOT NULL);


--
-- Name: idx_assets_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_class ON public.assets USING btree (asset_class);


--
-- Name: idx_backtest_instrument; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backtest_instrument ON public.backtest_results USING btree (instrument_id, indicator_name);


--
-- Name: idx_backtest_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backtest_parent ON public.backtest_results USING btree (parent_strategy_id);


--
-- Name: idx_backtest_promotion; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backtest_promotion ON public.backtest_results USING btree (promotion_status, sharpe_ratio);


--
-- Name: idx_backtest_return; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backtest_return ON public.backtest_results USING btree (total_return_pct);


--
-- Name: idx_backtest_sharpe; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backtest_sharpe ON public.backtest_results USING btree (sharpe_ratio);


--
-- Name: idx_banker_balances_account_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_banker_balances_account_ts ON public.banker_balances USING btree (company_id, exchange, account_id_external, ts DESC);


--
-- Name: idx_banker_balances_company_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_banker_balances_company_ts ON public.banker_balances USING btree (company_id, ts DESC);


--
-- Name: idx_bt_details_result; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bt_details_result ON public.backtest_trade_details USING btree (backtest_result_id);


--
-- Name: idx_capabilities_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_capabilities_company ON public.capabilities USING btree (company_id);


--
-- Name: idx_capabilities_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_capabilities_scope ON public.capabilities USING btree (scope_kind, scope_id);


--
-- Name: idx_collector_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_collector_enabled ON public.collector_catalog USING btree (is_enabled);


--
-- Name: idx_collector_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_collector_type ON public.collector_catalog USING btree (source_type);


--
-- Name: idx_cost_company_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cost_company_date ON public.api_cost_log USING btree (company_id, created_at);


--
-- Name: idx_cost_role_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cost_role_date ON public.api_cost_log USING btree (role, created_at);


--
-- Name: idx_cron_heartbeats_stale; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cron_heartbeats_stale ON public.cron_heartbeats USING btree (last_run_at) WHERE (last_status <> 'ok'::text);


--
-- Name: idx_deriv_instrument_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_deriv_instrument_time ON public.derivatives_snapshots USING btree (instrument_id, snapshot_at);


--
-- Name: idx_indicators_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_indicators_hash ON public.indicators USING btree (params_hash);


--
-- Name: idx_indicators_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_indicators_lookup ON public.indicators USING btree (instrument_id, indicator_name, timeframe);


--
-- Name: idx_instruments_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_instruments_active ON public.instruments USING btree (is_active) WHERE is_active;


--
-- Name: idx_instruments_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_instruments_asset ON public.instruments USING btree (asset_id) WHERE (asset_id IS NOT NULL);


--
-- Name: idx_instruments_asset_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_instruments_asset_class ON public.instruments USING btree (asset_class);


--
-- Name: idx_instruments_venue; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_instruments_venue ON public.instruments USING btree (venue_id) WHERE (venue_id IS NOT NULL);


--
-- Name: idx_interp_consensus_dir; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interp_consensus_dir ON public.signal_interpretations USING btree (consensus_direction);


--
-- Name: idx_interp_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interp_created ON public.signal_interpretations USING btree (created_at);


--
-- Name: idx_interp_instrument; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interp_instrument ON public.signal_interpretations USING btree (instrument_symbol, instrument_exchange);


--
-- Name: idx_interp_media_item; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interp_media_item ON public.signal_interpretations USING btree (media_item_id) WHERE (media_item_id IS NOT NULL);


--
-- Name: idx_interp_model_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interp_model_hash ON public.signal_interpretations USING btree (model_version, param_hash);


--
-- Name: idx_interp_news_item; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interp_news_item ON public.signal_interpretations USING btree (news_item_id);


--
-- Name: idx_interp_trader; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interp_trader ON public.signal_interpretations USING btree (trader_profile_id);


--
-- Name: idx_leverage_history_company_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leverage_history_company_ts ON public.leverage_history USING btree (company_id, ts DESC);


--
-- Name: idx_leverage_history_instrument_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leverage_history_instrument_ts ON public.leverage_history USING btree (exchange, symbol, ts DESC);


--
-- Name: idx_media_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_media_created ON public.media_items USING btree (created_at);


--
-- Name: idx_media_extraction; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_media_extraction ON public.media_items USING btree (extraction_method);


--
-- Name: idx_media_file_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_media_file_hash ON public.media_items USING btree (file_hash);


--
-- Name: idx_media_news_item; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_media_news_item ON public.media_items USING btree (news_item_id);


--
-- Name: idx_media_processing; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_media_processing ON public.media_items USING btree (processing_status);


--
-- Name: idx_media_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_media_source ON public.media_items USING btree (source_id);


--
-- Name: idx_media_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_media_type ON public.media_items USING btree (media_type);


--
-- Name: idx_memu_outbox_unprocessed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memu_outbox_unprocessed ON public.memu_outbox USING btree (created_at) WHERE (processed_at IS NULL);


--
-- Name: idx_news_author; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_author ON public.news_items USING btree (author);


--
-- Name: idx_news_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_channel ON public.news_items USING btree (channel_name);


--
-- Name: idx_news_enriched_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_enriched_at ON public.news_items USING btree (enriched_at);


--
-- Name: idx_news_enrichment_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_enrichment_gin ON public.news_items USING gin (enrichment);


--
-- Name: idx_news_has_media; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_has_media ON public.news_items USING btree (has_media);


--
-- Name: idx_news_instruments; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_instruments ON public.news_items USING gin (instruments);


--
-- Name: idx_news_items_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_items_pending ON public.news_items USING btree (collected_at) WHERE (enrichment_status = 'pending'::text);


--
-- Name: idx_news_items_phash_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_items_phash_recent ON public.news_items USING btree (image_phash, collected_at DESC) WHERE ((image_phash IS NOT NULL) AND (duplicate_of_id IS NULL));


--
-- Name: idx_news_published; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_published ON public.news_items USING btree (published_at);


--
-- Name: idx_news_source_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_source_date ON public.news_items USING btree (source, collected_at);


--
-- Name: idx_news_source_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_source_id ON public.news_items USING btree (source_id);


--
-- Name: idx_pm_correlation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_correlation_id ON public.position_postmortems USING btree (correlation_id) WHERE (correlation_id IS NOT NULL);


--
-- Name: idx_pm_param_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_param_hash ON public.position_postmortems USING btree (param_hash);


--
-- Name: idx_pm_position_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_position_id ON public.position_postmortems USING btree (position_id);


--
-- Name: idx_pm_postmortem_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_postmortem_version ON public.position_postmortems USING btree (postmortem_version);


--
-- Name: idx_pos_update_position; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pos_update_position ON public.position_updates USING btree (position_id, "timestamp" DESC);


--
-- Name: idx_prompt_versions_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prompt_versions_hash ON public.prompt_versions USING btree (prompt_hash);


--
-- Name: idx_prompt_versions_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prompt_versions_name ON public.prompt_versions USING btree (name, created_at DESC);


--
-- Name: idx_queue_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_queue_status ON public.backtest_queue USING btree (status, claimed_at);


--
-- Name: idx_services_catalog_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_services_catalog_enabled ON public.services_catalog USING btree (enabled_on_vps);


--
-- Name: idx_services_catalog_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_services_catalog_kind ON public.services_catalog USING btree (kind);


--
-- Name: idx_services_catalog_last_heartbeat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_services_catalog_last_heartbeat ON public.services_catalog USING btree (last_heartbeat_ts DESC NULLS LAST);


--
-- Name: idx_services_catalog_snapshots_name_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_services_catalog_snapshots_name_ts ON public.services_catalog_snapshots USING btree (name, ts DESC);


--
-- Name: idx_si_correlation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_si_correlation_id ON public.signal_interpretations USING btree (correlation_id) WHERE (correlation_id IS NOT NULL);


--
-- Name: idx_si_exchange; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_si_exchange ON public.signal_interpretations USING btree (instrument_exchange);


--
-- Name: idx_si_pattern_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_si_pattern_tags ON public.signal_interpretations USING gin (pattern_tags);


--
-- Name: idx_si_prompt_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_si_prompt_version ON public.signal_interpretations USING btree (prompt_version);


--
-- Name: idx_si_regime_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_si_regime_tags ON public.signal_interpretations USING gin (regime_tags);


--
-- Name: idx_si_session_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_si_session_tags ON public.signal_interpretations USING gin (session_tags);


--
-- Name: idx_si_setup_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_si_setup_tags ON public.signal_interpretations USING gin (setup_tags);


--
-- Name: idx_si_symbol_norm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_si_symbol_norm ON public.signal_interpretations USING btree (instrument_symbol_normalised);


--
-- Name: idx_signal_interp_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_signal_interp_unique ON public.signal_interpretations USING btree (news_item_id, model_version, param_hash);


--
-- Name: idx_sources_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_enabled ON public.collector_sources USING btree (enabled, source_type);


--
-- Name: idx_sources_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_entity ON public.collector_sources USING btree (entity_type);


--
-- Name: idx_sources_last_col; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_last_col ON public.collector_sources USING btree (last_collected_at);


--
-- Name: idx_sources_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_parent ON public.collector_sources USING btree (parent_id);


--
-- Name: idx_sources_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_type ON public.collector_sources USING btree (source_type);


--
-- Name: idx_strands_indicator; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strands_indicator ON public.strategy_dna_strands USING btree (indicator_catalog_id);


--
-- Name: idx_strands_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strands_strategy ON public.strategy_dna_strands USING btree (strategy_id);


--
-- Name: idx_strategies_active_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strategies_active_class ON public.strategies USING btree (is_active, asset_class) WHERE is_active;


--
-- Name: idx_tp_actor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tp_actor_id ON public.tracked_positions USING btree (actor_id);


--
-- Name: idx_tp_actor_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tp_actor_type ON public.tracked_positions USING btree (actor_type);


--
-- Name: idx_tp_closed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tp_closed_at ON public.tracked_positions USING btree (closed_at) WHERE (closed_at IS NOT NULL);


--
-- Name: idx_tp_correlation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tp_correlation_id ON public.tracked_positions USING btree (correlation_id) WHERE (correlation_id IS NOT NULL);


--
-- Name: idx_tp_postmortem; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tp_postmortem ON public.tracked_positions USING btree (postmortem_status) WHERE ((postmortem_status)::text = 'pending'::text);


--
-- Name: idx_tracked_pos_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tracked_pos_company ON public.tracked_positions USING btree (company_id);


--
-- Name: idx_tracked_pos_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tracked_pos_open ON public.tracked_positions USING btree (status, trader_profile_id) WHERE ((status)::text = 'open'::text);


--
-- Name: idx_tracked_pos_signal_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tracked_pos_signal_ts ON public.tracked_positions USING btree (signal_timestamp);


--
-- Name: idx_tracked_pos_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tracked_pos_status ON public.tracked_positions USING btree (status);


--
-- Name: idx_tracked_pos_symbol; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tracked_pos_symbol ON public.tracked_positions USING btree (instrument_symbol, instrument_exchange);


--
-- Name: idx_tracked_pos_trader; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tracked_pos_trader ON public.tracked_positions USING btree (trader_profile_id);


--
-- Name: idx_tracked_positions_actor_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tracked_positions_actor_company ON public.tracked_positions USING btree (actor_id, company_id) WHERE (actor_id IS NOT NULL);


--
-- Name: idx_tracked_positions_signal_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tracked_positions_signal_source ON public.tracked_positions USING btree (signal_source);


--
-- Name: idx_trader_accuracy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trader_accuracy ON public.trader_profiles USING btree (accuracy_score) WHERE (accuracy_score IS NOT NULL);


--
-- Name: idx_trader_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trader_last_seen ON public.trader_profiles USING btree (last_seen_at);


--
-- Name: idx_trader_platform; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trader_platform ON public.trader_profiles USING btree (platform);


--
-- Name: idx_trader_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trader_type ON public.trader_profiles USING btree (trader_type);


--
-- Name: idx_treasury_decisions_company_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_treasury_decisions_company_ts ON public.treasury_decisions USING btree (company_id, ts DESC);


--
-- Name: idx_treasury_decisions_intent_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_treasury_decisions_intent_hash ON public.treasury_decisions USING btree (intent_hash);


--
-- Name: idx_venues_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_venues_active ON public.venues USING btree (is_active) WHERE is_active;


--
-- Name: idx_venues_adapter; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_venues_adapter ON public.venues USING btree (adapter);


--
-- Name: idx_watched_channel_collector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_watched_channel_collector ON public.watched_channels USING btree (collector_id);


--
-- Name: idx_watched_channel_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_watched_channel_enabled ON public.watched_channels USING btree (is_enabled);


--
-- Name: idx_watched_channel_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_watched_channel_slug ON public.watched_channels USING btree (channel_slug);


--
-- Name: idx_watched_user_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_watched_user_channel ON public.watched_users USING btree (channel_id);


--
-- Name: idx_watched_user_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_watched_user_enabled ON public.watched_users USING btree (is_enabled);


--
-- Name: idx_watched_user_normalized; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_watched_user_normalized ON public.watched_users USING btree (username_normalized);


--
-- Name: idx_watched_user_profile; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_watched_user_profile ON public.watched_users USING btree (trader_profile_id);


--
-- Name: ix_data_suff_computed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_data_suff_computed_at ON public.data_sufficiency_reports USING btree (computed_at);


--
-- Name: ix_data_suff_instr_tf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_data_suff_instr_tf ON public.data_sufficiency_reports USING btree (instrument_id, timeframe);


--
-- Name: ix_data_suff_verdict; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_data_suff_verdict ON public.data_sufficiency_reports USING btree (verdict);


--
-- Name: mcp_invocations_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mcp_invocations_status_idx ON public.mcp_invocations USING btree (status, started_at DESC);


--
-- Name: mcp_invocations_tool_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mcp_invocations_tool_idx ON public.mcp_invocations USING btree (tool_name, started_at DESC);


--
-- Name: mcp_tool_requests_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mcp_tool_requests_hash_idx ON public.mcp_tool_requests USING btree (content_hash);


--
-- Name: mcp_tool_requests_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mcp_tool_requests_status_idx ON public.mcp_tool_requests USING btree (status, created_at DESC);


--
-- Name: mcp_tools_enabled_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mcp_tools_enabled_idx ON public.mcp_tools USING btree (enabled);


--
-- Name: optimiser_strategy_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX optimiser_strategy_idx ON public.optimiser_candidates USING btree (strategy, status, created_at DESC);


--
-- Name: order_events_order_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX order_events_order_ts_idx ON public.order_events USING btree (order_id, ts);


--
-- Name: order_events_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX order_events_type_idx ON public.order_events USING btree (event_type, ts DESC);


--
-- Name: orders_company_submitted_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_company_submitted_idx ON public.orders USING btree (company_id, submitted_at DESC);


--
-- Name: orders_external_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_external_id_idx ON public.orders USING btree (adapter, exchange, external_order_id) WHERE (external_order_id IS NOT NULL);


--
-- Name: orders_intent_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_intent_hash_idx ON public.orders USING btree (intent_hash);


--
-- Name: orders_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_status_idx ON public.orders USING btree (status) WHERE (status = ANY (ARRAY['new'::text, 'accepted'::text, 'partially_filled'::text]));


--
-- Name: paper_wallets_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_wallets_active_idx ON public.paper_wallets USING btree (company_id, is_active) WHERE (is_active = true);


--
-- Name: paper_wallets_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_wallets_company_idx ON public.paper_wallets USING btree (company_id);


--
-- Name: position_snapshots_key_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX position_snapshots_key_ts_idx ON public.position_snapshots USING btree (company_id, adapter, exchange, account_id_external, symbol, ts DESC);


--
-- Name: regime_config_universe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX regime_config_universe_idx ON public.regime_config USING btree (universe, enabled);


--
-- Name: regime_states_key_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX regime_states_key_ts_idx ON public.regime_states USING btree (universe, exchange, symbol, timeframe, classifier, as_of DESC);


--
-- Name: regime_states_recorded_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX regime_states_recorded_idx ON public.regime_states USING btree (recorded_at DESC);


--
-- Name: regime_states_regime_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX regime_states_regime_idx ON public.regime_states USING btree (regime, as_of DESC);


--
-- Name: regime_transitions_symbol_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX regime_transitions_symbol_idx ON public.regime_transitions USING btree (exchange, symbol, timeframe, transitioned_at DESC);


--
-- Name: scout_candidates_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX scout_candidates_status_idx ON public.scout_candidates USING btree (status, created_at DESC);


--
-- Name: scout_candidates_unique_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX scout_candidates_unique_idx ON public.scout_candidates USING btree (exchange, symbol, COALESCE(universe, ''::text), COALESCE(company_id, ''::text));


--
-- Name: strategy_descriptors_unique_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX strategy_descriptors_unique_idx ON public.strategy_descriptors USING btree (kind, name, COALESCE(company_id, ''::text));


--
-- Name: strategy_intents_source_unique_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX strategy_intents_source_unique_idx ON public.strategy_intents USING btree (strategy_name, source_ref) WHERE (source_ref IS NOT NULL);


--
-- Name: strategy_intents_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX strategy_intents_status_idx ON public.strategy_intents USING btree (status, proposed_at DESC);


--
-- Name: strategy_intents_strategy_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX strategy_intents_strategy_idx ON public.strategy_intents USING btree (strategy_name, proposed_at DESC);


--
-- Name: uniq_api_cost_log_corr_op; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uniq_api_cost_log_corr_op ON public.api_cost_log USING btree (correlation_id, operation);


--
-- Name: uq_sources_platform; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_sources_platform ON public.collector_sources USING btree (source_type, platform_id);


--
-- Name: candles_2024_01_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_01_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_01_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_01_instrument_id_timeframe_idx;


--
-- Name: candles_2024_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_01_pkey;


--
-- Name: candles_2024_01_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_01_timestamp_idx;


--
-- Name: candles_2024_02_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_02_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_02_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_02_instrument_id_timeframe_idx;


--
-- Name: candles_2024_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_02_pkey;


--
-- Name: candles_2024_02_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_02_timestamp_idx;


--
-- Name: candles_2024_03_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_03_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_03_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_03_instrument_id_timeframe_idx;


--
-- Name: candles_2024_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_03_pkey;


--
-- Name: candles_2024_03_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_03_timestamp_idx;


--
-- Name: candles_2024_04_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_04_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_04_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_04_instrument_id_timeframe_idx;


--
-- Name: candles_2024_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_04_pkey;


--
-- Name: candles_2024_04_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_04_timestamp_idx;


--
-- Name: candles_2024_05_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_05_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_05_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_05_instrument_id_timeframe_idx;


--
-- Name: candles_2024_05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_05_pkey;


--
-- Name: candles_2024_05_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_05_timestamp_idx;


--
-- Name: candles_2024_06_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_06_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_06_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_06_instrument_id_timeframe_idx;


--
-- Name: candles_2024_06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_06_pkey;


--
-- Name: candles_2024_06_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_06_timestamp_idx;


--
-- Name: candles_2024_07_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_07_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_07_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_07_instrument_id_timeframe_idx;


--
-- Name: candles_2024_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_07_pkey;


--
-- Name: candles_2024_07_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_07_timestamp_idx;


--
-- Name: candles_2024_08_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_08_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_08_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_08_instrument_id_timeframe_idx;


--
-- Name: candles_2024_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_08_pkey;


--
-- Name: candles_2024_08_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_08_timestamp_idx;


--
-- Name: candles_2024_09_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_09_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_09_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_09_instrument_id_timeframe_idx;


--
-- Name: candles_2024_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_09_pkey;


--
-- Name: candles_2024_09_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_09_timestamp_idx;


--
-- Name: candles_2024_10_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_10_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_10_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_10_instrument_id_timeframe_idx;


--
-- Name: candles_2024_10_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_10_pkey;


--
-- Name: candles_2024_10_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_10_timestamp_idx;


--
-- Name: candles_2024_11_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_11_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_11_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_11_instrument_id_timeframe_idx;


--
-- Name: candles_2024_11_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_11_pkey;


--
-- Name: candles_2024_11_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_11_timestamp_idx;


--
-- Name: candles_2024_12_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2024_12_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2024_12_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2024_12_instrument_id_timeframe_idx;


--
-- Name: candles_2024_12_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2024_12_pkey;


--
-- Name: candles_2024_12_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2024_12_timestamp_idx;


--
-- Name: candles_2025_01_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_01_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_01_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_01_instrument_id_timeframe_idx;


--
-- Name: candles_2025_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_01_pkey;


--
-- Name: candles_2025_01_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_01_timestamp_idx;


--
-- Name: candles_2025_02_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_02_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_02_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_02_instrument_id_timeframe_idx;


--
-- Name: candles_2025_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_02_pkey;


--
-- Name: candles_2025_02_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_02_timestamp_idx;


--
-- Name: candles_2025_03_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_03_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_03_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_03_instrument_id_timeframe_idx;


--
-- Name: candles_2025_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_03_pkey;


--
-- Name: candles_2025_03_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_03_timestamp_idx;


--
-- Name: candles_2025_04_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_04_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_04_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_04_instrument_id_timeframe_idx;


--
-- Name: candles_2025_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_04_pkey;


--
-- Name: candles_2025_04_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_04_timestamp_idx;


--
-- Name: candles_2025_05_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_05_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_05_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_05_instrument_id_timeframe_idx;


--
-- Name: candles_2025_05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_05_pkey;


--
-- Name: candles_2025_05_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_05_timestamp_idx;


--
-- Name: candles_2025_06_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_06_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_06_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_06_instrument_id_timeframe_idx;


--
-- Name: candles_2025_06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_06_pkey;


--
-- Name: candles_2025_06_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_06_timestamp_idx;


--
-- Name: candles_2025_07_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_07_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_07_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_07_instrument_id_timeframe_idx;


--
-- Name: candles_2025_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_07_pkey;


--
-- Name: candles_2025_07_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_07_timestamp_idx;


--
-- Name: candles_2025_08_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_08_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_08_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_08_instrument_id_timeframe_idx;


--
-- Name: candles_2025_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_08_pkey;


--
-- Name: candles_2025_08_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_08_timestamp_idx;


--
-- Name: candles_2025_09_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_09_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_09_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_09_instrument_id_timeframe_idx;


--
-- Name: candles_2025_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_09_pkey;


--
-- Name: candles_2025_09_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_09_timestamp_idx;


--
-- Name: candles_2025_10_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_10_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_10_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_10_instrument_id_timeframe_idx;


--
-- Name: candles_2025_10_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_10_pkey;


--
-- Name: candles_2025_10_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_10_timestamp_idx;


--
-- Name: candles_2025_11_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_11_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_11_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_11_instrument_id_timeframe_idx;


--
-- Name: candles_2025_11_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_11_pkey;


--
-- Name: candles_2025_11_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_11_timestamp_idx;


--
-- Name: candles_2025_12_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2025_12_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2025_12_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2025_12_instrument_id_timeframe_idx;


--
-- Name: candles_2025_12_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2025_12_pkey;


--
-- Name: candles_2025_12_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2025_12_timestamp_idx;


--
-- Name: candles_2026_01_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_01_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_01_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_01_instrument_id_timeframe_idx;


--
-- Name: candles_2026_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_01_pkey;


--
-- Name: candles_2026_01_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_01_timestamp_idx;


--
-- Name: candles_2026_02_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_02_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_02_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_02_instrument_id_timeframe_idx;


--
-- Name: candles_2026_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_02_pkey;


--
-- Name: candles_2026_02_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_02_timestamp_idx;


--
-- Name: candles_2026_03_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_03_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_03_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_03_instrument_id_timeframe_idx;


--
-- Name: candles_2026_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_03_pkey;


--
-- Name: candles_2026_03_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_03_timestamp_idx;


--
-- Name: candles_2026_04_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_04_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_04_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_04_instrument_id_timeframe_idx;


--
-- Name: candles_2026_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_04_pkey;


--
-- Name: candles_2026_04_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_04_timestamp_idx;


--
-- Name: candles_2026_05_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_05_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_05_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_05_instrument_id_timeframe_idx;


--
-- Name: candles_2026_05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_05_pkey;


--
-- Name: candles_2026_05_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_05_timestamp_idx;


--
-- Name: candles_2026_06_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_06_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_06_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_06_instrument_id_timeframe_idx;


--
-- Name: candles_2026_06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_06_pkey;


--
-- Name: candles_2026_06_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_06_timestamp_idx;


--
-- Name: candles_2026_07_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_07_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_07_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_07_instrument_id_timeframe_idx;


--
-- Name: candles_2026_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_07_pkey;


--
-- Name: candles_2026_07_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_07_timestamp_idx;


--
-- Name: candles_2026_08_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_08_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_08_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_08_instrument_id_timeframe_idx;


--
-- Name: candles_2026_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_08_pkey;


--
-- Name: candles_2026_08_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_08_timestamp_idx;


--
-- Name: candles_2026_09_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_09_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_09_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_09_instrument_id_timeframe_idx;


--
-- Name: candles_2026_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_09_pkey;


--
-- Name: candles_2026_09_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_09_timestamp_idx;


--
-- Name: candles_2026_10_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_10_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_10_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_10_instrument_id_timeframe_idx;


--
-- Name: candles_2026_10_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_10_pkey;


--
-- Name: candles_2026_10_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_10_timestamp_idx;


--
-- Name: candles_2026_11_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_11_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_11_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_11_instrument_id_timeframe_idx;


--
-- Name: candles_2026_11_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_11_pkey;


--
-- Name: candles_2026_11_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_11_timestamp_idx;


--
-- Name: candles_2026_12_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_2026_12_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_2026_12_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_2026_12_instrument_id_timeframe_idx;


--
-- Name: candles_2026_12_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_2026_12_pkey;


--
-- Name: candles_2026_12_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_2026_12_timestamp_idx;


--
-- Name: candles_future_instrument_id_source_timeframe_timestamp_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.uq_candles_composite ATTACH PARTITION public.candles_future_instrument_id_source_timeframe_timestamp_key;


--
-- Name: candles_future_instrument_id_timeframe_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_instrument_tf ATTACH PARTITION public.candles_future_instrument_id_timeframe_idx;


--
-- Name: candles_future_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.candles_pkey ATTACH PARTITION public.candles_future_pkey;


--
-- Name: candles_future_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_candles_timestamp_brin ATTACH PARTITION public.candles_future_timestamp_idx;


--
-- Name: collector_sources trg_collector_sources_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_collector_sources_updated_at BEFORE UPDATE ON public.collector_sources FOR EACH ROW EXECUTE FUNCTION public.trg_set_updated_at();


--
-- Name: indicator_catalog trg_indicator_catalog_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_indicator_catalog_updated BEFORE UPDATE ON public.indicator_catalog FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: instruments trg_instruments_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_instruments_updated BEFORE UPDATE ON public.instruments FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: strategies trg_strategies_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_strategies_updated BEFORE UPDATE ON public.strategies FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: system_config trg_system_config_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_config_updated BEFORE UPDATE ON public.system_config FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: tracked_positions trg_tp_freeze_entry_reasons; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_tp_freeze_entry_reasons BEFORE UPDATE ON public.tracked_positions FOR EACH ROW EXECUTE FUNCTION public.fn_freeze_entry_reasons();


--
-- Name: tracked_positions trg_tracked_positions_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_tracked_positions_updated BEFORE UPDATE ON public.tracked_positions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: agent_decisions agent_decisions_persona_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_decisions
    ADD CONSTRAINT agent_decisions_persona_id_fkey FOREIGN KEY (persona_id) REFERENCES public.agent_personas(id);


--
-- Name: agent_opinions agent_opinions_position_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_opinions
    ADD CONSTRAINT agent_opinions_position_id_fkey FOREIGN KEY (position_id) REFERENCES public.tracked_positions(id) ON DELETE CASCADE;


--
-- Name: agent_prompts agent_prompts_persona_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_prompts
    ADD CONSTRAINT agent_prompts_persona_id_fkey FOREIGN KEY (persona_id) REFERENCES public.agent_personas(id);


--
-- Name: assets assets_alias_of_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_alias_of_id_fkey FOREIGN KEY (alias_of_id) REFERENCES public.assets(id) ON DELETE SET NULL;


--
-- Name: backtest_trade_details backtest_trade_details_backtest_result_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_trade_details
    ADD CONSTRAINT backtest_trade_details_backtest_result_id_fkey FOREIGN KEY (backtest_result_id) REFERENCES public.backtest_results(id) ON DELETE CASCADE;


--
-- Name: candles candles_instrument_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.candles
    ADD CONSTRAINT candles_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.instruments(id) ON DELETE CASCADE;


--
-- Name: collector_sources collector_sources_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collector_sources
    ADD CONSTRAINT collector_sources_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.collector_sources(id) ON DELETE SET NULL;


--
-- Name: contest_participants contest_participants_contest_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contest_participants
    ADD CONSTRAINT contest_participants_contest_id_fkey FOREIGN KEY (contest_id) REFERENCES public.contests(id) ON DELETE CASCADE;


--
-- Name: copy_trades copy_trades_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.copy_trades
    ADD CONSTRAINT copy_trades_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.copy_sources(id) ON DELETE CASCADE;


--
-- Name: crash_protection_events crash_protection_events_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crash_protection_events
    ADD CONSTRAINT crash_protection_events_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.crash_protection_rules(id);


--
-- Name: dashboard_otps dashboard_otps_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_otps
    ADD CONSTRAINT dashboard_otps_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.dashboard_users(chat_id) ON DELETE CASCADE;


--
-- Name: dashboard_sessions dashboard_sessions_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_sessions
    ADD CONSTRAINT dashboard_sessions_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.dashboard_users(chat_id) ON DELETE CASCADE;


--
-- Name: data_sufficiency_reports data_sufficiency_reports_instrument_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_sufficiency_reports
    ADD CONSTRAINT data_sufficiency_reports_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.instruments(id) ON DELETE CASCADE;


--
-- Name: fills fills_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fills
    ADD CONSTRAINT fills_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id);


--
-- Name: indicators indicators_instrument_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicators
    ADD CONSTRAINT indicators_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.instruments(id) ON DELETE CASCADE;


--
-- Name: instrument_aliases instrument_aliases_instrument_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instrument_aliases
    ADD CONSTRAINT instrument_aliases_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.instruments(id) ON DELETE CASCADE;


--
-- Name: instruments instruments_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruments
    ADD CONSTRAINT instruments_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE SET NULL;


--
-- Name: instruments instruments_venue_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruments
    ADD CONSTRAINT instruments_venue_id_fkey FOREIGN KEY (venue_id) REFERENCES public.venues(id) ON DELETE SET NULL;


--
-- Name: media_items media_items_news_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_items
    ADD CONSTRAINT media_items_news_item_id_fkey FOREIGN KEY (news_item_id) REFERENCES public.news_items(id) ON DELETE CASCADE;


--
-- Name: media_items media_items_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_items
    ADD CONSTRAINT media_items_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.collector_sources(id) ON DELETE SET NULL;


--
-- Name: news_items news_items_duplicate_of_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items
    ADD CONSTRAINT news_items_duplicate_of_id_fkey FOREIGN KEY (duplicate_of_id) REFERENCES public.news_items(id);


--
-- Name: news_items news_items_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items
    ADD CONSTRAINT news_items_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.collector_sources(id) ON DELETE SET NULL;


--
-- Name: order_events order_events_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_events
    ADD CONSTRAINT order_events_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id);


--
-- Name: orders orders_treasury_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_treasury_decision_id_fkey FOREIGN KEY (treasury_decision_id) REFERENCES public.treasury_decisions(id);


--
-- Name: position_updates position_updates_position_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_updates
    ADD CONSTRAINT position_updates_position_id_fkey FOREIGN KEY (position_id) REFERENCES public.tracked_positions(id) ON DELETE CASCADE;


--
-- Name: signal_interpretations signal_interpretations_media_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_interpretations
    ADD CONSTRAINT signal_interpretations_media_item_id_fkey FOREIGN KEY (media_item_id) REFERENCES public.media_items(id) ON DELETE SET NULL;


--
-- Name: signal_interpretations signal_interpretations_news_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_interpretations
    ADD CONSTRAINT signal_interpretations_news_item_id_fkey FOREIGN KEY (news_item_id) REFERENCES public.news_items(id) ON DELETE CASCADE;


--
-- Name: signal_interpretations signal_interpretations_trader_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_interpretations
    ADD CONSTRAINT signal_interpretations_trader_profile_id_fkey FOREIGN KEY (trader_profile_id) REFERENCES public.trader_profiles(id) ON DELETE SET NULL;


--
-- Name: strategy_dna_strands strategy_dna_strands_indicator_catalog_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_dna_strands
    ADD CONSTRAINT strategy_dna_strands_indicator_catalog_id_fkey FOREIGN KEY (indicator_catalog_id) REFERENCES public.indicator_catalog(id) ON DELETE RESTRICT;


--
-- Name: strategy_dna_strands strategy_dna_strands_strategy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_dna_strands
    ADD CONSTRAINT strategy_dna_strands_strategy_id_fkey FOREIGN KEY (strategy_id) REFERENCES public.strategies(id) ON DELETE CASCADE;


--
-- Name: strategy_windows strategy_windows_strategy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_windows
    ADD CONSTRAINT strategy_windows_strategy_id_fkey FOREIGN KEY (strategy_id) REFERENCES public.strategies(id) ON DELETE CASCADE;


--
-- Name: watched_channels watched_channels_collector_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watched_channels
    ADD CONSTRAINT watched_channels_collector_id_fkey FOREIGN KEY (collector_id) REFERENCES public.collector_catalog(id) ON DELETE CASCADE;


--
-- Name: watched_users watched_users_channel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watched_users
    ADD CONSTRAINT watched_users_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.watched_channels(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict qQ8Ylf20yodkaK6ahFRWuyCr6ZFcOfCtcQtWukDCYiNhdLnqLeq0DTq3TW0Cbak

