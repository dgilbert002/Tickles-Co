--
-- PostgreSQL database dump
--

\restrict UL3pTzdPlcKeN8Pydtf9eeaEQZUJJ0ieVey9WYLsfpEvcajGRq7XI7Q6sHln4fE

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


SET default_tablespace = '';

SET default_table_access_method = heap;

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
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
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
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


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
    instrument_symbol_normalised character varying(64),
    reason_agreement_score numeric(4,3),
    entry_reason_trader_embedding public.vector(384),
    actor_instance text DEFAULT ''::text NOT NULL,
    source_position_id bigint,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT tracked_positions_detection_method_check CHECK (((detection_method)::text = ANY ((ARRAY['manual'::character varying, 'llm_vision'::character varying, 'text_parser'::character varying, 'quant_pattern'::character varying, 'agent_override'::character varying])::text[]))),
    CONSTRAINT tracked_positions_direction_check CHECK (((direction)::text = ANY ((ARRAY['long'::character varying, 'short'::character varying])::text[]))),
    CONSTRAINT tracked_positions_outcome_check CHECK (((outcome)::text = ANY ((ARRAY['tp1_hit'::character varying, 'tp2_hit'::character varying, 'tp3_hit'::character varying, 'sl_hit'::character varying, 'breakeven'::character varying, 'expired'::character varying, 'manual_close'::character varying, 'invalidated'::character varying])::text[]))),
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
-- Name: api_cost_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_cost_log ALTER COLUMN id SET DEFAULT nextval('public.api_cost_log_id_seq'::regclass);


--
-- Name: backtest_queue id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_queue ALTER COLUMN id SET DEFAULT nextval('public.backtest_queue_id_seq'::regclass);


--
-- Name: backtest_results id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_results ALTER COLUMN id SET DEFAULT nextval('public.backtest_results_id_seq'::regclass);


--
-- Name: backtest_trade_details id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_trade_details ALTER COLUMN id SET DEFAULT nextval('public.backtest_trade_details_id_seq'::regclass);


--
-- Name: candles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candles ALTER COLUMN id SET DEFAULT nextval('public.candles_id_seq'::regclass);


--
-- Name: indicator_catalog id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicator_catalog ALTER COLUMN id SET DEFAULT nextval('public.indicator_catalog_id_seq'::regclass);


--
-- Name: indicators id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicators ALTER COLUMN id SET DEFAULT nextval('public.indicators_id_seq'::regclass);


--
-- Name: instruments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruments ALTER COLUMN id SET DEFAULT nextval('public.instruments_id_seq'::regclass);


--
-- Name: memu_outbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memu_outbox ALTER COLUMN id SET DEFAULT nextval('public.memu_outbox_id_seq'::regclass);


--
-- Name: news_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items ALTER COLUMN id SET DEFAULT nextval('public.news_items_id_seq'::regclass);


--
-- Name: prompt_versions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_versions ALTER COLUMN id SET DEFAULT nextval('public.prompt_versions_id_seq'::regclass);


--
-- Name: strategies id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategies ALTER COLUMN id SET DEFAULT nextval('public.strategies_id_seq'::regclass);


--
-- Name: strategy_dna_strands id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_dna_strands ALTER COLUMN id SET DEFAULT nextval('public.strategy_dna_strands_id_seq'::regclass);


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
-- Name: api_cost_log api_cost_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_cost_log
    ADD CONSTRAINT api_cost_log_pkey PRIMARY KEY (id);


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
-- Name: backtest_trade_details backtest_trade_details_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_trade_details
    ADD CONSTRAINT backtest_trade_details_pkey PRIMARY KEY (id);


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
-- Name: instruments instruments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruments
    ADD CONSTRAINT instruments_pkey PRIMARY KEY (id);


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
-- Name: strategies strategies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategies
    ADD CONSTRAINT strategies_pkey PRIMARY KEY (id);


--
-- Name: strategy_dna_strands strategy_dna_strands_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_dna_strands
    ADD CONSTRAINT strategy_dna_strands_pkey PRIMARY KEY (id);


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
-- Name: backtest_results uq_backtest_param_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_results
    ADD CONSTRAINT uq_backtest_param_hash UNIQUE (param_hash);


--
-- Name: system_config uq_config_ns_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_config
    ADD CONSTRAINT uq_config_ns_key UNIQUE (namespace, config_key);


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
-- Name: tracked_positions uq_position_dedup; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tracked_positions
    ADD CONSTRAINT uq_position_dedup UNIQUE (news_item_id, trader_profile_id, instrument_symbol, direction, actor_instance);


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
-- Name: strategy_windows uq_window_dedup; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_windows
    ADD CONSTRAINT uq_window_dedup UNIQUE (strategy_id, window_close_time);


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
-- Name: idx_bt_details_result; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bt_details_result ON public.backtest_trade_details USING btree (backtest_result_id);


--
-- Name: idx_cost_company_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cost_company_date ON public.api_cost_log USING btree (company_id, created_at);


--
-- Name: idx_cost_role_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cost_role_date ON public.api_cost_log USING btree (role, created_at);


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
-- Name: idx_instruments_asset_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_instruments_asset_class ON public.instruments USING btree (asset_class);


--
-- Name: idx_memu_outbox_unprocessed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memu_outbox_unprocessed ON public.memu_outbox USING btree (created_at) WHERE (processed_at IS NULL);


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
-- Name: idx_tp_entry_reason_embed_cosine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tp_entry_reason_embed_cosine ON public.tracked_positions USING ivfflat (entry_reason_trader_embedding public.vector_cosine_ops) WITH (lists='50');


--
-- Name: idx_tp_postmortem; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tp_postmortem ON public.tracked_positions USING btree (postmortem_status) WHERE ((postmortem_status)::text = 'pending'::text);


--
-- Name: idx_tp_symbol_norm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tp_symbol_norm ON public.tracked_positions USING btree (instrument_symbol_normalised) WHERE (instrument_symbol_normalised IS NOT NULL);


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
-- Name: uniq_tracked_positions_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uniq_tracked_positions_actor ON public.tracked_positions USING btree (actor_type, actor_id, actor_instance, source_position_id) WHERE (source_position_id IS NOT NULL);


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
-- Name: indicators indicators_instrument_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indicators
    ADD CONSTRAINT indicators_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.instruments(id) ON DELETE CASCADE;


--
-- Name: news_items news_items_duplicate_of_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items
    ADD CONSTRAINT news_items_duplicate_of_id_fkey FOREIGN KEY (duplicate_of_id) REFERENCES public.news_items(id);


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
-- PostgreSQL database dump complete
--

\unrestrict UL3pTzdPlcKeN8Pydtf9eeaEQZUJJ0ieVey9WYLsfpEvcajGRq7XI7Q6sHln4fE

