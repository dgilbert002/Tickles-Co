--
-- PostgreSQL database dump
--

\restrict zJ6NZOkTXdKqoOU3P0f8guiX9hPwSF7b7pNeL9AfP5b317ObzmEGcOkVQu0DwaZ

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
-- Name: account_type_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.account_type_t AS ENUM (
    'demo',
    'live'
);


--
-- Name: agent_status_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.agent_status_t AS ENUM (
    'active',
    'paused',
    'error',
    'stopped'
);


--
-- Name: cost_type_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.cost_type_t AS ENUM (
    'maker_fee',
    'taker_fee',
    'spread',
    'overnight_funding',
    'swap',
    'commission',
    'guaranteed_stop',
    'slippage',
    'other'
);


--
-- Name: order_event_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.order_event_t AS ENUM (
    'submitted',
    'accepted',
    'partial_fill',
    'filled',
    'cancelled',
    'rejected',
    'amended',
    'expired'
);


--
-- Name: quantity_type_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.quantity_type_t AS ENUM (
    'units',
    'lots',
    'contracts'
);


--
-- Name: snapshot_source_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.snapshot_source_t AS ENUM (
    'exchange_api',
    'calculated',
    'manual'
);


--
-- Name: trade_direction_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.trade_direction_t AS ENUM (
    'long',
    'short'
);


--
-- Name: trade_status_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.trade_status_t AS ENUM (
    'pending',
    'open',
    'partial_close',
    'closed',
    'cancelled',
    'failed'
);


--
-- Name: trade_type_t; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.trade_type_t AS ENUM (
    'live',
    'paper',
    'shadow'
);


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
-- Name: accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts (
    id bigint NOT NULL,
    exchange character varying(50) NOT NULL,
    account_id_external character varying(100),
    account_type public.account_type_t DEFAULT 'demo'::public.account_type_t NOT NULL,
    api_key_ref character varying(100),
    balance numeric(20,8) DEFAULT 0,
    equity numeric(20,8) DEFAULT 0,
    margin_used numeric(20,8) DEFAULT 0,
    currency character varying(10) DEFAULT 'USD'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    session_state jsonb,
    session_state_version integer DEFAULT 0 NOT NULL,
    last_synced_at timestamp(3) with time zone,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: accounts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.accounts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: accounts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.accounts_id_seq OWNED BY public.accounts.id;


--
-- Name: agent_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_state (
    id bigint NOT NULL,
    agent_name character varying(100) NOT NULL,
    status public.agent_status_t DEFAULT 'stopped'::public.agent_status_t NOT NULL,
    last_heartbeat_at timestamp(3) with time zone,
    last_error text,
    state_data jsonb,
    state_version integer DEFAULT 0 NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: agent_state_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_state_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_state_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_state_id_seq OWNED BY public.agent_state.id;


--
-- Name: balance_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.balance_snapshots (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    balance numeric(20,8),
    equity numeric(20,8),
    margin_used numeric(20,8),
    unrealized_pnl numeric(20,8),
    snapshot_source public.snapshot_source_t DEFAULT 'exchange_api'::public.snapshot_source_t NOT NULL,
    snapshot_at timestamp(3) with time zone NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: balance_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.balance_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: balance_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.balance_snapshots_id_seq OWNED BY public.balance_snapshots.id;


--
-- Name: company_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company_config (
    id integer NOT NULL,
    config_key character varying(100) NOT NULL,
    config_value text,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: company_config_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.company_config_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: company_config_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.company_config_id_seq OWNED BY public.company_config.id;


--
-- Name: leverage_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leverage_history (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    old_leverage integer,
    new_leverage integer NOT NULL,
    changed_by character varying(50),
    reason character varying(200),
    changed_at timestamp(3) with time zone NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
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
-- Name: order_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_events (
    id bigint NOT NULL,
    trade_id bigint NOT NULL,
    event_type public.order_event_t NOT NULL,
    price numeric(20,8),
    quantity_filled numeric(20,8),
    exchange_timestamp timestamp(3) with time zone,
    raw_response jsonb,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
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
-- Name: signal_interpretations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.signal_interpretations (
    id bigint NOT NULL,
    news_item_id bigint NOT NULL,
    media_item_id bigint,
    trader_profile_id bigint NOT NULL,
    model_version character varying(100) NOT NULL,
    param_hash character(64) NOT NULL,
    candle_data_hash character(64),
    llm_direction character varying(8) NOT NULL,
    llm_confidence numeric(5,4) NOT NULL,
    llm_reasoning text,
    llm_levels jsonb,
    quant_direction character varying(8) NOT NULL,
    quant_confidence numeric(5,4) NOT NULL,
    quant_indicators jsonb,
    consensus_direction character varying(8) NOT NULL,
    consensus_confidence numeric(5,4) NOT NULL,
    consensus_method character varying(32) DEFAULT 'weighted_average'::character varying NOT NULL,
    instrument_symbol character varying(50),
    instrument_exchange character varying(50),
    market_data_fresh boolean DEFAULT false NOT NULL,
    market_data_at timestamp(3) with time zone,
    llm_cost_usd numeric(10,6) DEFAULT 0 NOT NULL,
    quant_cost_usd numeric(10,6) DEFAULT 0 NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT signal_interpretations_consensus_confidence_check CHECK (((consensus_confidence >= 0.0) AND (consensus_confidence <= 1.0))),
    CONSTRAINT signal_interpretations_consensus_direction_check CHECK (((consensus_direction)::text = ANY ((ARRAY['long'::character varying, 'short'::character varying, 'neutral'::character varying, 'unclear'::character varying, 'conflict'::character varying])::text[]))),
    CONSTRAINT signal_interpretations_consensus_method_check CHECK (((consensus_method)::text = ANY ((ARRAY['weighted_average'::character varying, 'llm_wins'::character varying, 'quant_wins'::character varying, 'veto'::character varying, 'unclear'::character varying])::text[]))),
    CONSTRAINT signal_interpretations_llm_confidence_check CHECK (((llm_confidence >= 0.0) AND (llm_confidence <= 1.0))),
    CONSTRAINT signal_interpretations_llm_direction_check CHECK (((llm_direction)::text = ANY ((ARRAY['long'::character varying, 'short'::character varying, 'neutral'::character varying, 'unclear'::character varying, 'conflict'::character varying])::text[]))),
    CONSTRAINT signal_interpretations_quant_confidence_check CHECK (((quant_confidence >= 0.0) AND (quant_confidence <= 1.0))),
    CONSTRAINT signal_interpretations_quant_direction_check CHECK (((quant_direction)::text = ANY ((ARRAY['long'::character varying, 'short'::character varying, 'neutral'::character varying, 'unclear'::character varying, 'conflict'::character varying])::text[])))
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
-- Name: strategy_lifecycle; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strategy_lifecycle (
    id bigint NOT NULL,
    strategy_id bigint NOT NULL,
    from_status character varying(30),
    to_status character varying(30) NOT NULL,
    changed_by character varying(100),
    reason text,
    changed_at timestamp(3) with time zone NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: strategy_lifecycle_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategy_lifecycle_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategy_lifecycle_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategy_lifecycle_id_seq OWNED BY public.strategy_lifecycle.id;


--
-- Name: trade_cost_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trade_cost_entries (
    id bigint NOT NULL,
    trade_id bigint NOT NULL,
    cost_type public.cost_type_t NOT NULL,
    amount numeric(20,8) NOT NULL,
    currency character varying(10) DEFAULT 'USD'::character varying NOT NULL,
    accrued_at timestamp(3) with time zone,
    description character varying(200),
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: trade_cost_entries_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trade_cost_entries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trade_cost_entries_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trade_cost_entries_id_seq OWNED BY public.trade_cost_entries.id;


--
-- Name: trade_validations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trade_validations (
    id bigint NOT NULL,
    trade_id bigint NOT NULL,
    strategy_id bigint NOT NULL,
    backtest_result_id bigint,
    signal_match boolean,
    entry_price_delta numeric(20,8),
    exit_price_delta numeric(20,8),
    pnl_delta numeric(20,8),
    pnl_delta_pct numeric(10,6),
    slippage_contribution numeric(20,8),
    fee_contribution numeric(20,8),
    data_drift_detected boolean DEFAULT false NOT NULL,
    original_candle_hash character(64),
    validation_candle_hash character(64),
    validated_at timestamp(3) with time zone,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: trade_validations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trade_validations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trade_validations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trade_validations_id_seq OWNED BY public.trade_validations.id;


--
-- Name: trader_performance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trader_performance (
    id bigint NOT NULL,
    trader_profile_id bigint NOT NULL,
    score_period character varying(16) DEFAULT 'rolling_30d'::character varying NOT NULL,
    scored_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    total_signals integer DEFAULT 0 NOT NULL,
    validated_signals integer DEFAULT 0 NOT NULL,
    correct_direction integer DEFAULT 0 NOT NULL,
    accuracy_pct numeric(5,4) DEFAULT NULL::numeric,
    avg_confidence numeric(5,4) DEFAULT NULL::numeric,
    confidence_calibration numeric(5,4) DEFAULT NULL::numeric,
    total_pnl_usd numeric(20,8) DEFAULT 0,
    avg_pnl_per_signal numeric(20,8) DEFAULT NULL::numeric,
    max_win_usd numeric(20,8) DEFAULT NULL::numeric,
    max_loss_usd numeric(20,8) DEFAULT NULL::numeric,
    sharpe_ratio numeric(10,4) DEFAULT NULL::numeric,
    max_drawdown_pct numeric(10,4) DEFAULT NULL::numeric,
    calculation_params jsonb,
    metadata jsonb,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT trader_performance_score_period_check CHECK (((score_period)::text = ANY ((ARRAY['rolling_7d'::character varying, 'rolling_30d'::character varying, 'rolling_90d'::character varying, 'all_time'::character varying])::text[])))
);


--
-- Name: trader_performance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trader_performance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trader_performance_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trader_performance_id_seq OWNED BY public.trader_performance.id;


--
-- Name: trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trades (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    strategy_id bigint,
    trade_type public.trade_type_t DEFAULT 'paper'::public.trade_type_t NOT NULL,
    direction public.trade_direction_t NOT NULL,
    status public.trade_status_t DEFAULT 'pending'::public.trade_status_t NOT NULL,
    quantity numeric(20,8),
    quantity_type public.quantity_type_t DEFAULT 'units'::public.quantity_type_t NOT NULL,
    contract_size numeric(20,8) DEFAULT 1.00000000 NOT NULL,
    leverage integer DEFAULT 1 NOT NULL,
    entry_price numeric(20,8),
    exit_price numeric(20,8),
    expected_entry_price numeric(20,8),
    expected_exit_price numeric(20,8),
    stop_loss_price numeric(20,8),
    take_profit_1 numeric(20,8),
    take_profit_2 numeric(20,8),
    take_profit_3 numeric(20,8),
    gross_pnl numeric(20,8),
    net_pnl numeric(20,8),
    entry_slippage numeric(20,8),
    exit_slippage numeric(20,8),
    entry_slippage_pct numeric(10,6),
    exit_slippage_pct numeric(10,6),
    signal_to_fill_ms integer,
    order_to_fill_ms integer,
    exchange_order_id character varying(100),
    exchange_deal_id character varying(100),
    winning_strand_id bigint,
    conflict_exists boolean,
    brain_calc_price numeric(20,8),
    fake_candle_close numeric(20,8),
    price_variance_pct numeric(10,6),
    brain_snapshot_detail jsonb,
    brain_snapshot_version integer DEFAULT 1 NOT NULL,
    candle_data_hash character(64),
    signal_params_hash character(64),
    window_close_time time without time zone,
    signal_at timestamp(3) with time zone,
    ordered_at timestamp(3) with time zone,
    opened_at timestamp(3) with time zone,
    closed_at timestamp(3) with time zone,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: trades_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trades_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trades_id_seq OWNED BY public.trades.id;


--
-- Name: accounts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts ALTER COLUMN id SET DEFAULT nextval('public.accounts_id_seq'::regclass);


--
-- Name: agent_state id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_state ALTER COLUMN id SET DEFAULT nextval('public.agent_state_id_seq'::regclass);


--
-- Name: balance_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.balance_snapshots ALTER COLUMN id SET DEFAULT nextval('public.balance_snapshots_id_seq'::regclass);


--
-- Name: company_config id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_config ALTER COLUMN id SET DEFAULT nextval('public.company_config_id_seq'::regclass);


--
-- Name: leverage_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leverage_history ALTER COLUMN id SET DEFAULT nextval('public.leverage_history_id_seq'::regclass);


--
-- Name: order_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_events ALTER COLUMN id SET DEFAULT nextval('public.order_events_id_seq'::regclass);


--
-- Name: signal_interpretations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_interpretations ALTER COLUMN id SET DEFAULT nextval('public.signal_interpretations_id_seq'::regclass);


--
-- Name: strategy_lifecycle id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_lifecycle ALTER COLUMN id SET DEFAULT nextval('public.strategy_lifecycle_id_seq'::regclass);


--
-- Name: trade_cost_entries id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_cost_entries ALTER COLUMN id SET DEFAULT nextval('public.trade_cost_entries_id_seq'::regclass);


--
-- Name: trade_validations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_validations ALTER COLUMN id SET DEFAULT nextval('public.trade_validations_id_seq'::regclass);


--
-- Name: trader_performance id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trader_performance ALTER COLUMN id SET DEFAULT nextval('public.trader_performance_id_seq'::regclass);


--
-- Name: trades id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trades ALTER COLUMN id SET DEFAULT nextval('public.trades_id_seq'::regclass);


--
-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);


--
-- Name: agent_state agent_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_state
    ADD CONSTRAINT agent_state_pkey PRIMARY KEY (id);


--
-- Name: balance_snapshots balance_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.balance_snapshots
    ADD CONSTRAINT balance_snapshots_pkey PRIMARY KEY (id);


--
-- Name: company_config company_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_config
    ADD CONSTRAINT company_config_pkey PRIMARY KEY (id);


--
-- Name: leverage_history leverage_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leverage_history
    ADD CONSTRAINT leverage_history_pkey PRIMARY KEY (id);


--
-- Name: order_events order_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_events
    ADD CONSTRAINT order_events_pkey PRIMARY KEY (id);


--
-- Name: signal_interpretations signal_interpretations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_interpretations
    ADD CONSTRAINT signal_interpretations_pkey PRIMARY KEY (id);


--
-- Name: strategy_lifecycle strategy_lifecycle_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_lifecycle
    ADD CONSTRAINT strategy_lifecycle_pkey PRIMARY KEY (id);


--
-- Name: trade_cost_entries trade_cost_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_cost_entries
    ADD CONSTRAINT trade_cost_entries_pkey PRIMARY KEY (id);


--
-- Name: trade_validations trade_validations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_validations
    ADD CONSTRAINT trade_validations_pkey PRIMARY KEY (id);


--
-- Name: trader_performance trader_performance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trader_performance
    ADD CONSTRAINT trader_performance_pkey PRIMARY KEY (id);


--
-- Name: trades trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT trades_pkey PRIMARY KEY (id);


--
-- Name: accounts uq_accounts_exchange_ext; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT uq_accounts_exchange_ext UNIQUE (exchange, account_id_external);


--
-- Name: agent_state uq_agent_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_state
    ADD CONSTRAINT uq_agent_name UNIQUE (agent_name);


--
-- Name: company_config uq_company_config_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_config
    ADD CONSTRAINT uq_company_config_key UNIQUE (config_key);


--
-- Name: signal_interpretations uq_interpretation_dedup; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_interpretations
    ADD CONSTRAINT uq_interpretation_dedup UNIQUE (news_item_id, model_version, param_hash);


--
-- Name: trader_performance uq_trader_performance_period; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trader_performance
    ADD CONSTRAINT uq_trader_performance_period UNIQUE (trader_profile_id, score_period);


--
-- Name: trades uq_trades_signal_dedup; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT uq_trades_signal_dedup UNIQUE (account_id, instrument_id, signal_params_hash);


--
-- Name: idx_balance_account_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_balance_account_time ON public.balance_snapshots USING btree (account_id, snapshot_at);


--
-- Name: idx_costs_trade; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_costs_trade ON public.trade_cost_entries USING btree (trade_id);


--
-- Name: idx_costs_type_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_costs_type_date ON public.trade_cost_entries USING btree (cost_type, accrued_at);


--
-- Name: idx_events_trade; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_trade ON public.order_events USING btree (trade_id, created_at);


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
-- Name: idx_leverage_account_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leverage_account_time ON public.leverage_history USING btree (account_id, changed_at);


--
-- Name: idx_lifecycle_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lifecycle_strategy ON public.strategy_lifecycle USING btree (strategy_id, changed_at);


--
-- Name: idx_perf_accuracy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_perf_accuracy ON public.trader_performance USING btree (accuracy_pct) WHERE (accuracy_pct IS NOT NULL);


--
-- Name: idx_perf_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_perf_period ON public.trader_performance USING btree (score_period);


--
-- Name: idx_perf_scored_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_perf_scored_at ON public.trader_performance USING btree (scored_at);


--
-- Name: idx_perf_trader; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_perf_trader ON public.trader_performance USING btree (trader_profile_id);


--
-- Name: idx_trades_account_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_account_status ON public.trades USING btree (account_id, status);


--
-- Name: idx_trades_candle_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_candle_hash ON public.trades USING btree (candle_data_hash);


--
-- Name: idx_trades_exchange_deal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_exchange_deal ON public.trades USING btree (exchange_deal_id);


--
-- Name: idx_trades_exchange_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_exchange_order ON public.trades USING btree (exchange_order_id);


--
-- Name: idx_trades_instrument_opened; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_instrument_opened ON public.trades USING btree (instrument_id, opened_at);


--
-- Name: idx_trades_open_only; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_open_only ON public.trades USING btree (account_id, instrument_id) WHERE (status = ANY (ARRAY['pending'::public.trade_status_t, 'open'::public.trade_status_t, 'partial_close'::public.trade_status_t]));


--
-- Name: idx_trades_strategy_closed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_strategy_closed ON public.trades USING btree (strategy_id, closed_at);


--
-- Name: idx_trades_type_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trades_type_status ON public.trades USING btree (trade_type, status);


--
-- Name: idx_validations_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_validations_strategy ON public.trade_validations USING btree (strategy_id, validated_at);


--
-- Name: idx_validations_trade; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_validations_trade ON public.trade_validations USING btree (trade_id);


--
-- Name: accounts trg_accounts_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_accounts_updated BEFORE UPDATE ON public.accounts FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: agent_state trg_agent_state_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agent_state_updated BEFORE UPDATE ON public.agent_state FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: company_config trg_company_config_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_company_config_updated BEFORE UPDATE ON public.company_config FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: trades trg_trades_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trades_updated BEFORE UPDATE ON public.trades FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: balance_snapshots balance_snapshots_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.balance_snapshots
    ADD CONSTRAINT balance_snapshots_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id);


--
-- Name: leverage_history leverage_history_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leverage_history
    ADD CONSTRAINT leverage_history_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id);


--
-- Name: order_events order_events_trade_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_events
    ADD CONSTRAINT order_events_trade_id_fkey FOREIGN KEY (trade_id) REFERENCES public.trades(id) ON DELETE CASCADE;


--
-- Name: trade_cost_entries trade_cost_entries_trade_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_cost_entries
    ADD CONSTRAINT trade_cost_entries_trade_id_fkey FOREIGN KEY (trade_id) REFERENCES public.trades(id) ON DELETE CASCADE;


--
-- Name: trade_validations trade_validations_trade_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_validations
    ADD CONSTRAINT trade_validations_trade_id_fkey FOREIGN KEY (trade_id) REFERENCES public.trades(id) ON DELETE CASCADE;


--
-- Name: trades trades_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT trades_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id);


--
-- PostgreSQL database dump complete
--

\unrestrict zJ6NZOkTXdKqoOU3P0f8guiX9hPwSF7b7pNeL9AfP5b317ObzmEGcOkVQu0DwaZ

