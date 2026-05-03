--
-- PostgreSQL database dump
--

\restrict ezps75d77we8HBHgJR23731lOAi1doxyRxMxTmaeewA3k4bsKIWshpvhtadNg17

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
-- Name: actor_performance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.actor_performance (
    id bigint NOT NULL,
    actor_type text NOT NULL,
    actor_id text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    closed_position_count integer NOT NULL,
    edge_score numeric(5,4) NOT NULL,
    components_jsonb jsonb NOT NULL,
    weights_used_jsonb jsonb NOT NULL,
    confidence_low boolean DEFAULT false NOT NULL,
    formula_version integer NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: actor_leaderboard; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.actor_leaderboard AS
 SELECT actor_type,
    actor_id,
    period_start,
    period_end,
    closed_position_count,
    edge_score,
    confidence_low,
    components_jsonb,
    formula_version,
    rank() OVER (PARTITION BY period_start, period_end ORDER BY edge_score DESC, closed_position_count DESC) AS rank
   FROM public.actor_performance
  WHERE (closed_position_count >= 3);


--
-- Name: actor_performance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.actor_performance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: actor_performance_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.actor_performance_id_seq OWNED BY public.actor_performance.id;


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
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    actor_instance text DEFAULT ''::text NOT NULL
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
-- Name: edge_score_changes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.edge_score_changes (
    id bigint NOT NULL,
    actor_type text NOT NULL,
    actor_id text NOT NULL,
    period_end date NOT NULL,
    score_before numeric(5,4),
    score_after numeric(5,4) NOT NULL,
    delta numeric(6,4) NOT NULL,
    components_before jsonb,
    components_after jsonb NOT NULL,
    note text,
    logged_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: edge_score_changes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.edge_score_changes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: edge_score_changes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.edge_score_changes_id_seq OWNED BY public.edge_score_changes.id;


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
-- Name: position_updates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.position_updates (
    id bigint NOT NULL,
    position_id bigint NOT NULL,
    status character varying(16) DEFAULT 'open'::character varying NOT NULL,
    current_price numeric(20,8),
    unrealized_pnl_pct numeric(10,4),
    unrealized_pnl_usd numeric(20,8),
    realized_pnl_pct numeric(10,4) DEFAULT 0 NOT NULL,
    realized_pnl_usd numeric(20,8) DEFAULT 0 NOT NULL,
    max_drawdown_pct numeric(10,4) DEFAULT 0 NOT NULL,
    max_profit_pct numeric(10,4) DEFAULT 0 NOT NULL,
    distance_to_entry_pct numeric(10,4),
    distance_to_sl_pct numeric(10,4),
    distance_to_tp1_pct numeric(10,4),
    time_in_trade_minutes integer DEFAULT 0 NOT NULL,
    snapshot_at timestamp(3) with time zone NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
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
-- Name: prompt_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.prompt_assignments (
    id bigint NOT NULL,
    actor_id text NOT NULL,
    assignment_day date NOT NULL,
    prompt_name text NOT NULL,
    variant text NOT NULL,
    prompt_hash character(16) NOT NULL
);


--
-- Name: prompt_assignments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.prompt_assignments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: prompt_assignments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.prompt_assignments_id_seq OWNED BY public.prompt_assignments.id;


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
    total_signals integer DEFAULT 0 NOT NULL,
    total_positions integer DEFAULT 0 NOT NULL,
    win_count integer DEFAULT 0 NOT NULL,
    loss_count integer DEFAULT 0 NOT NULL,
    breakeven_count integer DEFAULT 0 NOT NULL,
    total_pnl_pct numeric(10,4) DEFAULT 0 NOT NULL,
    total_pnl_usd numeric(20,8) DEFAULT 0 NOT NULL,
    avg_win_pct numeric(10,4) DEFAULT 0 NOT NULL,
    avg_loss_pct numeric(10,4) DEFAULT 0 NOT NULL,
    max_drawdown_pct numeric(10,4) DEFAULT 0 NOT NULL,
    sharpe_ratio numeric(10,4),
    win_rate numeric(5,4) DEFAULT 0 NOT NULL,
    profit_factor numeric(10,4) DEFAULT 0 NOT NULL,
    avg_risk_reward numeric(10,4) DEFAULT 0 NOT NULL,
    best_trade_pnl_pct numeric(10,4) DEFAULT 0 NOT NULL,
    worst_trade_pnl_pct numeric(10,4) DEFAULT 0 NOT NULL,
    streak_current integer DEFAULT 0 NOT NULL,
    streak_max_win integer DEFAULT 0 NOT NULL,
    streak_max_loss integer DEFAULT 0 NOT NULL,
    last_trade_at timestamp(3) with time zone,
    calculated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
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
-- Name: actor_performance id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.actor_performance ALTER COLUMN id SET DEFAULT nextval('public.actor_performance_id_seq'::regclass);


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
-- Name: edge_score_changes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edge_score_changes ALTER COLUMN id SET DEFAULT nextval('public.edge_score_changes_id_seq'::regclass);


--
-- Name: leverage_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leverage_history ALTER COLUMN id SET DEFAULT nextval('public.leverage_history_id_seq'::regclass);


--
-- Name: order_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_events ALTER COLUMN id SET DEFAULT nextval('public.order_events_id_seq'::regclass);


--
-- Name: position_postmortems id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_postmortems ALTER COLUMN id SET DEFAULT nextval('public.position_postmortems_id_seq'::regclass);


--
-- Name: position_updates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_updates ALTER COLUMN id SET DEFAULT nextval('public.position_updates_id_seq'::regclass);


--
-- Name: prompt_assignments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_assignments ALTER COLUMN id SET DEFAULT nextval('public.prompt_assignments_id_seq'::regclass);


--
-- Name: strategy_lifecycle id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_lifecycle ALTER COLUMN id SET DEFAULT nextval('public.strategy_lifecycle_id_seq'::regclass);


--
-- Name: tracked_positions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tracked_positions ALTER COLUMN id SET DEFAULT nextval('public.tracked_positions_id_seq'::regclass);


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
-- Name: actor_performance actor_performance_actor_type_actor_id_period_start_period_e_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.actor_performance
    ADD CONSTRAINT actor_performance_actor_type_actor_id_period_start_period_e_key UNIQUE (actor_type, actor_id, period_start, period_end, formula_version);


--
-- Name: actor_performance actor_performance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.actor_performance
    ADD CONSTRAINT actor_performance_pkey PRIMARY KEY (id);


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
-- Name: edge_score_changes edge_score_changes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edge_score_changes
    ADD CONSTRAINT edge_score_changes_pkey PRIMARY KEY (id);


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
-- Name: position_postmortems position_postmortems_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_postmortems
    ADD CONSTRAINT position_postmortems_pkey PRIMARY KEY (id);


--
-- Name: position_updates position_updates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_updates
    ADD CONSTRAINT position_updates_pkey PRIMARY KEY (id);


--
-- Name: prompt_assignments prompt_assignments_actor_id_assignment_day_prompt_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_assignments
    ADD CONSTRAINT prompt_assignments_actor_id_assignment_day_prompt_name_key UNIQUE (actor_id, assignment_day, prompt_name);


--
-- Name: prompt_assignments prompt_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_assignments
    ADD CONSTRAINT prompt_assignments_pkey PRIMARY KEY (id);


--
-- Name: strategy_lifecycle strategy_lifecycle_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_lifecycle
    ADD CONSTRAINT strategy_lifecycle_pkey PRIMARY KEY (id);


--
-- Name: tracked_positions tracked_positions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tracked_positions
    ADD CONSTRAINT tracked_positions_pkey PRIMARY KEY (id);


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
    ADD CONSTRAINT uq_agent_name UNIQUE (agent_name, actor_instance);


--
-- Name: company_config uq_company_config_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_config
    ADD CONSTRAINT uq_company_config_key UNIQUE (config_key);


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
-- Name: trader_performance uq_trader_perf_profile; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trader_performance
    ADD CONSTRAINT uq_trader_perf_profile UNIQUE (trader_profile_id);


--
-- Name: trades uq_trades_signal_dedup; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT uq_trades_signal_dedup UNIQUE (account_id, instrument_id, signal_params_hash);


--
-- Name: idx_actor_perf_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_actor_perf_lookup ON public.actor_performance USING btree (actor_type, actor_id, period_end DESC);


--
-- Name: idx_actor_perf_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_actor_perf_period ON public.actor_performance USING btree (period_start, period_end, edge_score DESC);


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
-- Name: idx_edge_score_changes_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_edge_score_changes_actor ON public.edge_score_changes USING btree (actor_type, actor_id, period_end DESC);


--
-- Name: idx_events_trade; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_trade ON public.order_events USING btree (trade_id, created_at);


--
-- Name: idx_leverage_account_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leverage_account_time ON public.leverage_history USING btree (account_id, changed_at);


--
-- Name: idx_lifecycle_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lifecycle_strategy ON public.strategy_lifecycle USING btree (strategy_id, changed_at);


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
-- Name: idx_pos_updates_position; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pos_updates_position ON public.position_updates USING btree (position_id, snapshot_at);


--
-- Name: idx_prompt_assignments_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prompt_assignments_lookup ON public.prompt_assignments USING btree (actor_id, prompt_name, assignment_day DESC);


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
-- Name: tracked_positions trg_tp_freeze_entry_reasons; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_tp_freeze_entry_reasons BEFORE UPDATE ON public.tracked_positions FOR EACH ROW EXECUTE FUNCTION public.fn_freeze_entry_reasons();


--
-- Name: tracked_positions trg_tracked_positions_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_tracked_positions_updated BEFORE UPDATE ON public.tracked_positions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: trader_performance trg_trader_performance_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trader_performance_updated BEFORE UPDATE ON public.trader_performance FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


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
-- Name: position_updates position_updates_position_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_updates
    ADD CONSTRAINT position_updates_position_id_fkey FOREIGN KEY (position_id) REFERENCES public.tracked_positions(id) ON DELETE CASCADE;


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

\unrestrict ezps75d77we8HBHgJR23731lOAi1doxyRxMxTmaeewA3k4bsKIWshpvhtadNg17

