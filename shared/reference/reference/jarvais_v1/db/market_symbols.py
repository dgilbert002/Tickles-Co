"""
market_symbols.py — Dynamic symbol registry for JarvAIs.

When Signal AI detects a new symbol, it's auto-registered here.
The Markets UI in Config tab reads from this table for per-symbol controls.
The YAHOO_MAP in signal_ai.py is replaced by a dynamic lookup from this table.
"""
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Shared symbol blocklist — used by signal_ai, alpha_analysis,
# trade_dossier, and trading_floor to reject bad symbols early.
# ─────────────────────────────────────────────────────────────────
BLOCKED_SYMBOLS = {
    "UNKNOWN", "N/A", "NA", "NONE", "TBD", "?", "",
    "NIKKEI", "NIKKE",                          # index name, not a ticker — use JPN225/NI225
    "FVG", "BOS", "CHOCH", "OB", "OTE",         # trading jargon mis-extractions
    "UNIDENTIFIED", "GENERAL", "MARKET", "INDEX",
}

import re
_VALID_SYMBOL_RE = re.compile(r'^[A-Z0-9./]{2,20}$')


def is_valid_symbol(symbol: str) -> bool:
    """Check if a symbol passes format validation and is not blocked."""
    s = (symbol or "").upper().strip()
    if not s or s in BLOCKED_SYMBOLS:
        return False
    if " " in s:
        return False
    if not _VALID_SYMBOL_RE.match(s):
        return False
    return True


# ─────────────────────────────────────────────────────────────────
# Symbol Alias Map — common names → canonical tradeable tickers
# ─────────────────────────────────────────────────────────────────
SYMBOL_ALIASES = {
    # Commodities
    "GOLD": "XAUUSD", "XAU": "XAUUSD",
    "SILVER": "XAGUSD", "XAG": "XAGUSD", "XADUSD": "XAGUSD",  # XADUSD common typo for Silver
    "OIL": "USOIL", "CRUDEOIL": "USOIL", "WTI": "USOIL", "CRUDE": "USOIL",
    "BRENT": "UKOIL",
    "PLATINUM": "XPTUSD", "PALLADIUM": "XPDUSD",
    "NATGAS": "NGAS", "NATURALGAS": "NGAS",

    # Indices
    "NASDAQ": "NAS100", "NDX": "NAS100", "QQQ": "NAS100", "USTEC": "NAS100",
    "DOW": "US30", "DJIA": "US30", "DJI": "US30", "DOW JONES": "US30",
    "SPX": "SPX500", "SP500": "SPX500",
    "NIKKEI": "JPN225", "NI225": "JPN225", "NIKKE": "JPN225",
    "FTSE": "UK100", "DAX": "GER40",

    # Stock aliases
    "GOOGL": "GOOG",

    # Crypto shorthand → USDT perpetual (BTCUSD→BTCUSDT for CCXT/Yahoo compatibility)
    "BTC": "BTCUSDT", "BITCOIN": "BTCUSDT", "BTCUSD": "BTCUSDT",
    "ETH": "ETHUSDT", "ETHEREUM": "ETHUSDT",
    "SOL": "SOLUSDT", "SOLANA": "SOLUSDT",
    "BNB": "BNBUSDT",
    "XRP": "XRPUSDT", "RIPPLE": "XRPUSDT",
    "ADA": "ADAUSDT", "CARDANO": "ADAUSDT",
    "DOGE": "DOGEUSDT", "DOGECOIN": "DOGEUSDT",
    "DOT": "DOTUSDT", "POLKADOT": "DOTUSDT",
    "AVAX": "AVAXUSDT", "AVALANCHE": "AVAXUSDT",
    "LINK": "LINKUSDT", "CHAINLINK": "LINKUSDT",
    "MATIC": "MATICUSDT", "POLYGON": "MATICUSDT",
    "LTC": "LTCUSDT", "LITECOIN": "LTCUSDT",
    "UNI": "UNIUSDT", "UNISWAP": "UNIUSDT",
    "ATOM": "ATOMUSDT", "COSMOS": "ATOMUSDT",
    "NEAR": "NEARUSDT",
    "FTM": "FTMUSDT", "FANTOM": "FTMUSDT",
    "INJ": "INJUSDT", "INJECTIVE": "INJUSDT",
    "SUI": "SUIUSDT",
    "SEI": "SEIUSDT",
    "TIA": "TIAUSDT", "CELESTIA": "TIAUSDT",
    "TAO": "TAOUSDT", "BITTENSOR": "TAOUSDT",
    "WIF": "WIFUSDT",
    "PEPE": "PEPEUSDT",
    "BONK": "BONKUSDT",
    "ARB": "ARBUSDT", "ARBITRUM": "ARBUSDT",
    "OP": "OPUSDT", "OPTIMISM": "OPUSDT",
    "PENDLE": "PENDLEUSDT",
    "RENDER": "RENDERUSDT", "RNDR": "RENDERUSDT",
    "FET": "FETUSDT",
    "JUP": "JUPUSDT", "JUPITER": "JUPUSDT",
    "HYPE": "HYPEUSDT", "HYPERLIQUID": "HYPEUSDT",
    "ENA": "ENAUSDT", "ETHENA": "ENAUSDT",
    "PUMP": "PUMPUSDT",
    "RIVER": "RIVERUSDT",
    "GRASS": "GRASSUSDT",
    "ONDO": "ONDOUSDT",
    "AERO": "AEROUSDT", "AERODROME": "AEROUSDT",
    "SPX": "SPXUSDT", "SPX6900": "SPXUSDT",
    "CL": "CLUSDT", "CLUSD": "CLUSDT",
    "JTO": "JTOUSDT", "JITO": "JTOUSDT",
    "IOST": "IOSTUSDT",
    "KAITO": "KAITOUSDT",
    "IP": "IPUSDT",
    "SPEC": "SPECUSDT",
    "CAT": "CATUSDT",
    "ENS": "ENSUSDT",
    "AAVE": "AAVEUSDT",
    "MKR": "MKRUSDT", "MAKER": "MKRUSDT",
    "CRV": "CRVUSDT", "CURVE": "CRVUSDT",
    "GRT": "GRTUSDT", "THEGRAPH": "GRTUSDT",
    "STX": "STXUSDT", "STACKS": "STXUSDT",
    "APT": "APTUSDT", "APTOS": "APTUSDT",
    "TRX": "TRXUSDT", "TRON": "TRXUSDT",
    "TON": "TONUSDT", "TONCOIN": "TONUSDT",
    "HBAR": "HBARUSDT", "HEDERA": "HBARUSDT",
    "VET": "VETUSDT", "VECHAIN": "VETUSDT",
    "FIL": "FILUSDT", "FILECOIN": "FILUSDT",
    "ICP": "ICPUSDT",
    "RUNE": "RUNEUSDT", "THORCHAIN": "RUNEUSDT",
    "BCH": "BCHUSDT",
    "EOS": "EOSUSDT",
    "XLM": "XLMUSDT", "STELLAR": "XLMUSDT",
    "ALGO": "ALGOUSDT", "ALGORAND": "ALGOUSDT",
    "SAND": "SANDUSDT", "SANDBOX": "SANDUSDT",
    "MANA": "MANAUSDT", "DECENTRALAND": "MANAUSDT",
    "AXS": "AXSUSDT", "AXIE": "AXSUSDT",
    "GALA": "GALAUSDT",
    "XMR": "XMRUSDT", "MONERO": "XMRUSDT",
    "ZEC": "ZECUSDT", "ZCASH": "ZECUSDT",
    "SHIB": "SHIBUSDT", "SHIBAINU": "SHIBUSDT",
    "APE": "APEUSDT", "APECOIN": "APEUSDT",
    "WLD": "WLDUSDT", "WORLDCOIN": "WLDUSDT",
    "PYTH": "PYTHUSDT",
    "MOVE": "MOVEUSDT",
    "S": "SUSDT",
    # Arabian Panda / chart mis-parses
    "JELLYJELLYUSDT": "JELLYUSDT", "JELLYJELLY": "JELLYUSDT",
    "JELLYUSDT.P": "JELLYUSDT",
    "HIPPO": "HIPPOUSDT",
    "SAHARA": "SAHARAUSDT", "SAHARAUSDT.P": "SAHARAUSDT",
}

_llm_resolve_cache: Dict[str, Optional[str]] = {}

# Extracted crypto base tokens for use by resolve_symbol auto-USDT logic.
# Must stay in sync with the set inside _classify_symbol.
CRYPTO_BASES: set = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOT", "DOGE", "AVAX",
    "MATIC", "LINK", "LTC", "UNI", "AAVE", "ATOM", "NEAR", "FTM",
    "ALGO", "SAND", "MANA", "AXS", "SHIB", "APE", "OP", "ARB",
    "SUI", "SEI", "TIA", "JUP", "WIF", "PEPE", "BONK", "FLOKI",
    "INJ", "TRX", "TON", "RENDER", "FET", "AGIX", "OCEAN",
    "JASMY", "GRT", "FIL", "ICP", "HBAR", "VET", "EOS", "XLM",
    "THETA", "EGLD", "FLOW", "ROSE", "ZIL", "ONE", "CRV", "COMP",
    "MKR", "SNX", "LDO", "RPL", "ENS", "SUSHI", "YFI", "BAL",
    "1INCH", "DYDX", "GMX", "PENDLE", "RUNE", "OSMO", "KAVA",
    "CFX", "CKB", "ACH", "ACT", "ALICE", "ALT", "ANKR", "ANT",
    "API3", "AUDIO", "BAND", "BAT", "BLUR", "C98", "CAKE",
    "CELO", "CHZ", "COS", "CTSI", "CVX", "DASH", "DENT", "DGB",
    "ENJ", "GALA", "GLM", "GNO", "HOOK", "HOT", "ICX", "IOTA",
    "IOTX", "JST", "KDA", "KNC", "KSM", "LINA", "LOOM", "LRC",
    "LUNC", "MASK", "MINA", "MTL", "NMR", "NULS", "OGN",
    "OMG", "ONT", "PAXG", "PEOPLE", "PERP", "PHB", "QNT", "QTUM",
    "RAY", "REN", "REQ", "RLC", "RSR", "RVN", "SC", "SKL",
    "STORJ", "STX", "SXP", "TFUEL", "TLM", "TWT", "UMA",
    "WAVES", "WLD", "WOO", "XEC", "XEM", "XMR", "XTZ",
    "YGG", "ZEC", "ZEN", "ZRX", "BABYDOGE", "AIXBT", "ADX",
    "REEF", "COCO", "HNT", "HYPE", "BRETT", "CELR", "CYBER",
    "ENA", "EVX", "FARTCOIN", "FUN", "GUN", "HOODRAT", "JOE",
    "LA", "LQTY", "LUNAI", "MOVE", "MUBARAK", "NAS", "NIL",
    "ORN", "PARTI", "POL", "PYTH", "BOND", "RNDR", "S",
    "SQD", "STMX", "VIRTUAL", "WRX", "XVG", "SOLANA",
    "AERO", "SPX", "CL", "GRASS", "RIVER", "ONDO", "JTO",
    "TAO", "KAITO", "IP", "IOST", "CAT", "SPEC",
}

_QUOTE_SUFFIXES = ("USDT", "FDUSD", "BUSD", "USDC", "USD", "PERP")


def _normalize_crypto_to_usdt(s: str) -> Optional[str]:
    """If `s` is a bare crypto base or non-USDT crypto pair, return the USDT form.
    Returns None if not recognized as crypto."""
    # Strip .P suffix (Bybit perpetual notation)
    clean = re.sub(r'[.\-_]?P(?:ERP)?$', '', s)

    # Already ends in USDT — good
    if clean.endswith("USDT") and len(clean) > 4:
        return clean

    # Extract base by stripping known quote currencies
    base = clean
    for suffix in _QUOTE_SUFFIXES:
        if clean.endswith(suffix) and len(clean) > len(suffix):
            base = clean[:-len(suffix)]
            break

    if base in CRYPTO_BASES:
        return f"{base}USDT"

    # Check if the entire symbol is a known crypto base (bare "ICX", "ALICE")
    if clean in CRYPTO_BASES:
        return f"{clean}USDT"

    return None


def resolve_symbol(symbol: str, db=None) -> str:
    """Resolve a symbol alias to its canonical tradeable ticker.

    Priority:
      1. Static SYMBOL_ALIASES map (instant)
      2. DB market_symbols canonical_symbol column
      3. Auto-USDT: bare crypto bases (ICX→ICXUSDT, ALICE→ALICEUSDT)
      4. Return original symbol unchanged
    """
    s = (symbol or "").upper().strip()
    if not s:
        return s

    if s in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[s]

    if db:
        try:
            row = db.fetch_one(
                "SELECT canonical_symbol FROM market_symbols "
                "WHERE symbol = %s AND canonical_symbol IS NOT NULL",
                (s,))
            if row and row.get("canonical_symbol"):
                return row["canonical_symbol"]
        except Exception:
            pass

    if s in _llm_resolve_cache:
        return _llm_resolve_cache[s] or s

    # Auto-USDT for recognized crypto bases not in SYMBOL_ALIASES
    usdt = _normalize_crypto_to_usdt(s)
    if usdt and usdt != s:
        return usdt

    return s


_SYMBOL_RESOLVER_IDENTITY = (
    "You are a financial symbol resolver. Given ticker(s), return the canonical "
    "exchange ticker for each. Rules:\n"
    "- Crypto: return the USDT perpetual pair (e.g. BTCUSDT, ETHUSDT)\n"
    "- Commodities: return the forex pair (e.g. XAUUSD for gold)\n"
    "- Indices: return the standard code (e.g. NAS100, US30)\n"
    "- Stocks: return the primary ticker (e.g. AAPL, GOOG)\n"
    "If you cannot identify a symbol, return UNKNOWN for that symbol.\n"
    "Respond ONLY as JSON: {\"INPUTSYM\": \"RESOLVED\", ...}"
)


def _batch_resolve_symbols_with_llm(symbols: list, db=None) -> dict:
    """Resolve multiple symbols in a single LLM call. Returns {input: resolved}."""
    if not symbols:
        return {}
    try:
        from core.model_interface import get_model_interface
        import json as _json
        mi = get_model_interface()
        _resolver_db = db
        if _resolver_db is None:
            from db.database import get_db
            _resolver_db = get_db()
        from core.config_loader import load_prompt
        identity = load_prompt(
            _resolver_db, "symbol_resolver_identity",
            _SYMBOL_RESOLVER_IDENTITY, min_length=10)

        sym_list = ", ".join(symbols[:50])
        resp = mi.query(
            role="symbol_resolver",
            system_prompt=identity,
            user_prompt=f"Resolve these symbols: {sym_list}",
            max_tokens=max(100, len(symbols) * 25), temperature=0,
            context="symbol_resolution_batch", source="market_symbols")

        if not resp or not resp.success:
            return {}
        text = (resp.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        results = _json.loads(text)
        mapped = {}
        for inp, resolved in results.items():
            inp_u = inp.upper().strip()
            res_u = (resolved or "").upper().strip()
            if res_u and res_u != "UNKNOWN" and len(res_u) <= 20:
                mapped[inp_u] = res_u
                _llm_resolve_cache[inp_u] = res_u
                logger.info(f"[SYMBOL] LLM batch resolved '{inp_u}' -> '{res_u}'")
                if db:
                    try:
                        db.execute(
                            "UPDATE market_symbols SET canonical_symbol = %s "
                            "WHERE symbol = %s", (res_u, inp_u))
                    except Exception:
                        pass
            else:
                _llm_resolve_cache[inp_u] = None
                logger.info(f"[SYMBOL] LLM batch: '{inp_u}' -> UNKNOWN")
        return mapped
    except Exception as e:
        logger.debug(f"[SYMBOL] Batch LLM resolve error: {e}")
        for s in symbols:
            _llm_resolve_cache[s.upper().strip()] = None
        return {}


def resolve_symbol_with_llm(symbol: str, db=None) -> str:
    """Try static + DB resolution first, then fall back to LLM for unknown symbols.
    Results are cached and stored in DB for future lookups."""
    resolved = resolve_symbol(symbol, db)
    if resolved != symbol.upper().strip():
        return resolved

    s = symbol.upper().strip()
    if s in _llm_resolve_cache:
        return _llm_resolve_cache[s] or s

    results = _batch_resolve_symbols_with_llm([s], db)
    return results.get(s, s)


# ─────────────────────────────────────────────────────────────────
# Exchange tradability check (Blofin, Bybit, etc.)
# ─────────────────────────────────────────────────────────────────

_NON_CRYPTO_EXCHANGES = {"mt5"}
_CRYPTO_ONLY_EXCHANGES = {"blofin", "bybit", "bitget"}


_CRYPTO_REBRANDS = {
    "LUNA": ["LUNA2", "LUNC"],
    "SHIB": ["SHIB1000", "1000SHIB"],
    "MATIC": ["POL"],
    "FTM": ["S"],
}

def _resolve_symbol_on_exchange(base: str, exchange_obj) -> Optional[str]:
    """Try to find a USDT perpetual for `base` on a loaded CCXT exchange.
    Prefers swap (:USDT) over spot, and checks known rebrands (e.g.
    LUNA→LUNA2, SHIB→SHIB1000) when the original base isn't found."""
    if not exchange_obj or not getattr(exchange_obj, 'markets', None):
        return None

    bases_to_try = [base] + _CRYPTO_REBRANDS.get(base.upper(), [])

    for b in bases_to_try:
        swap_key = f"{b}/USDT:USDT"
        if swap_key in exchange_obj.markets:
            return swap_key

    # Fuzzy match: only match swap/perp markets (containing :USDT).
    # Spot tickers (BASE/USDT without :USDT) fail on set_leverage,
    # set_margin_mode, etc. — unusable for futures trading.
    import re
    for b in bases_to_try:
        b_clean = re.sub(r'[/:\-_\s.]', '', b.upper())
        if len(b_clean) >= 2:
            for mkt_key in exchange_obj.markets:
                if ':USDT' not in mkt_key:
                    continue
                mkt_clean = re.sub(r'[/:\-_\s.]', '', mkt_key.upper())
                if mkt_clean.startswith(b_clean + "USDT"):
                    return mkt_key
    return None


def resolve_on_demand(symbol: str, db=None) -> Dict[str, Optional[str]]:
    """On-demand mini-recon: resolve a single symbol to exchange tickers.

    Checks candle_collector's cached exchange markets, executor cache, and
    PriceStreamer connections. Updates market_symbols if found.

    Returns {"bybit": "BTC/USDT:USDT", "blofin": "BTC/USDT:USDT"} or None values.
    """
    _EXCHANGES = ("bybit", "blofin", "bitget")

    s = (symbol or "").upper().strip()
    result: Dict[str, Optional[str]] = {eid: None for eid in _EXCHANGES}

    base = s
    for suffix in ("USDT", "FDUSD", "BUSD", "USDC", "USD", "PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            base = s[:-len(suffix)]
            break

    exchange_objs: Dict[str, Any] = {}

    # Source 1: candle_collector's cached markets (fastest, already loaded)
    try:
        from services.candle_collector import _get_exchange_markets
        for eid in _EXCHANGES:
            mkts = _get_exchange_markets(eid)
            if mkts:
                exchange_objs[eid] = type('_Proxy', (), {'markets': mkts})()
    except Exception:
        pass

    # Source 2: CCXT executor cache (trading accounts)
    if len(exchange_objs) < len(_EXCHANGES):
        try:
            from core.ccxt_executor import _executor_cache
            for aid, ex in _executor_cache.items():
                eid = ex.exchange_id
                if eid in exchange_objs:
                    continue
                if ex.connected and ex._exchange and getattr(ex._exchange, 'markets', None):
                    exchange_objs[eid] = ex._exchange
        except Exception:
            pass

    # Source 3: Direct CCXT load as last resort — with explicit timeout
    if len(exchange_objs) < len(_EXCHANGES):
        try:
            import ccxt as _ccxt
            for eid in _EXCHANGES:
                if eid in exchange_objs:
                    continue
                ex_cls = getattr(_ccxt, eid, None)
                if ex_cls:
                    ex = ex_cls({"enableRateLimit": True, "timeout": 15000})
                    mkts = ex.load_markets()
                    if mkts:
                        exchange_objs[eid] = ex
                        logger.debug(f"[SymbolResolve] Direct-loaded {len(mkts)} "
                                     f"markets from {eid} for on-demand resolution")
        except Exception:
            pass

    for eid in _EXCHANGES:
        ex = exchange_objs.get(eid)
        if not ex:
            continue
        ticker = _resolve_symbol_on_exchange(base, ex)
        if ticker:
            result[eid] = ticker

    any_found = any(result[eid] for eid in _EXCHANGES)
    if db and any_found:
        row = db.fetch_one(
            "SELECT symbol FROM market_symbols WHERE symbol = %s", (s,))
        if row:
            sets, params = [], []
            for eid in _EXCHANGES:
                if result[eid]:
                    sets.append(f"{eid}_ticker = %s")
                    params.append(result[eid])
            pref = next((eid for eid in _EXCHANGES if result[eid]), "bybit")
            sets.append("preferred_exchange = %s")
            params.append(pref)
            params.append(s)
            db.execute(
                f"UPDATE market_symbols SET {', '.join(sets)} WHERE symbol = %s",
                tuple(params))
            logger.info(f"[SymbolResolve] {s}: on-demand recon -> "
                        f"bybit={result['bybit']}, blofin={result['blofin']}, "
                        f"bitget={result.get('bitget')}")

    return result


def _is_perpetual(ticker: str, exchange_markets: dict = None) -> bool:
    """Return True only if the ticker represents a perpetual futures contract.
    Perpetual tickers use the CCXT format BASE/QUOTE:SETTLE (e.g. BTC/USDT:USDT).
    Spot tickers like BTC/USDT (no colon) are NOT perpetual."""
    if not ticker:
        return False
    if ":" in ticker:
        return True
    if exchange_markets and ticker in exchange_markets:
        mkt = exchange_markets[ticker]
        return mkt.get("type") in ("swap", "future")
    return False


def resolve_for_exchange(symbol: str, exchange_id: str, db=None,
                         exchange_markets: dict = None,
                         allow_spot: bool = False) -> Optional[str]:
    """Unified symbol resolution for a specific exchange.

    This is the SINGLE entry point that all code paths should use to convert
    a user-facing symbol (e.g. BTCUSDT) into an exchange-specific ticker
    (e.g. BTC/USDT:USDT).

    Resolution chain:
      1. Static aliases (SYMBOL_ALIASES)
      2. DB market_symbols table (bybit_ticker / blofin_ticker column)
      3. On-demand exchange market scan
      4. Direct market match with suffix derivation
      5. LLM fallback (for truly unknown tickers)

    IMPORTANT: By default only returns perpetual/swap tickers (BASE/QUOTE:SETTLE).
    Spot tickers (BASE/QUOTE without :SETTLE) are rejected unless allow_spot=True.
    This prevents accidentally buying spot assets when only futures are intended.

    Returns the exchange-specific ticker or None if unresolvable.
    """
    import re as _re
    import logging as _logging
    _log = _logging.getLogger("jarvais.market_symbols")

    s = (symbol or "").upper().strip()
    eid = (exchange_id or "").lower().strip()
    if not s or not eid:
        return None

    canonical = resolve_symbol(s, db)

    # Collect exchange markets for perpetual verification
    markets = exchange_markets or {}
    if not markets:
        try:
            from core.ccxt_executor import _executor_cache
            for aid, ex in _executor_cache.items():
                if ex.exchange_id == eid and ex.connected and ex._exchange:
                    markets = getattr(ex._exchange, 'markets', {})
                    break
        except Exception:
            pass

    def _guard(ticker):
        """Reject spot tickers unless allow_spot=True."""
        if not ticker:
            return None
        if allow_spot or _is_perpetual(ticker, markets):
            return ticker
        _log.warning(f"[MarketSymbols] Rejected SPOT ticker '{ticker}' for "
                     f"'{symbol}' on {eid} — only perpetual futures allowed")
        return None

    # Step 1: DB lookup for this exchange's ticker
    col = f"{eid}_ticker"
    if db and col in ("bybit_ticker", "blofin_ticker", "bitget_ticker"):
        for try_sym in ([canonical, s] if canonical != s else [s]):
            try:
                row = db.fetch_one(
                    f"SELECT {col} FROM market_symbols WHERE symbol = %s",
                    (try_sym,))
                if row and row.get(col):
                    result = _guard(row[col])
                    if result:
                        return result
            except Exception:
                pass

    # Step 2: On-demand recon (exchange_markets or resolve_on_demand)
    recon = resolve_on_demand(s, db)
    if recon.get(eid):
        result = _guard(recon[eid])
        if result:
            return result

    # Step 3: Direct market scan with suffix derivation
    if markets:
        if canonical in markets:
            result = _guard(canonical)
            if result:
                return result
        cleaned = _re.sub(r'[\.\-_]?P(?:ERP)?$', '', canonical)
        cleaned = _re.sub(r'\s+', '', cleaned)
        for base_sym in dict.fromkeys([canonical, cleaned]):
            for suffix in ("USDT", "USD", "BUSD", "USDC"):
                if base_sym.endswith(suffix) and len(base_sym) > len(suffix):
                    base = base_sym[:-len(suffix)]
                    # Try perpetual first (with settle currency), then spot only if allowed
                    for c in (f"{base}/{suffix}:{suffix}", f"{base}/{suffix}"):
                        if c in markets:
                            result = _guard(c)
                            if result:
                                return result
            for c in (f"{base_sym}/USDT:USDT", f"{base_sym}/USDT"):
                if c in markets:
                    result = _guard(c)
                    if result:
                        return result
        base_clean = _re.sub(r'[/:\-_\s.]', '', cleaned)
        if len(base_clean) >= 2:
            for mkt_key in markets:
                mkt_clean = _re.sub(r'[/:\-_\s.]', '', mkt_key.upper())
                if mkt_clean.startswith(base_clean) and 'USDT' in mkt_clean:
                    result = _guard(mkt_key)
                    if result:
                        return result

    # Step 4: LLM fallback
    try:
        llm_result = resolve_symbol_with_llm(s, db)
        if llm_result and llm_result != s:
            if markets and llm_result in markets:
                result = _guard(llm_result)
                if result:
                    return result
            recon2 = resolve_on_demand(llm_result, db)
            if recon2.get(eid):
                result = _guard(recon2[eid])
                if result:
                    return result
    except Exception:
        pass

    return None


def can_trade_on_exchange(symbol: str, exchange: str, db=None) -> tuple:
    """Check if symbol is tradeable on the given exchange.

    Returns (can_trade: bool, exchange_ticker_or_reason: str).
    Crypto assets (BTC, ETH, etc.) map to Blofin/Bybit.
    Commodities, forex, indices, stocks route to MT5.

    IMPORTANT: For crypto exchanges, only perpetual/swap tickers are accepted.
    Spot-only assets (e.g. PRIME/USDT without :USDT) are rejected.
    """
    import logging as _logging
    _log = _logging.getLogger("jarvais.market_symbols")

    s = (symbol or "").upper().strip()
    exchange = (exchange or "").lower().strip()

    if not s:
        return (False, "Empty symbol")
    if not exchange:
        return (False, "No exchange specified")

    # Asset class filtering: non-crypto assets can't trade on crypto exchanges
    classification = _classify_symbol(s)
    asset_class = classification.get("asset_class", "unknown")

    if exchange in _CRYPTO_ONLY_EXCHANGES:
        if asset_class in ("forex", "index", "stock", "etf"):
            return (False, f"{s} is {asset_class}, not available on {exchange}")
        if asset_class == "commodity" and s not in ("PAXGUSDT",):
            return (False, f"{s} is a commodity, not available on {exchange}")

    if exchange in _NON_CRYPTO_EXCHANGES:
        if asset_class == "cryptocurrency":
            pass  # some MT5 brokers support crypto

    def _reject_spot(ticker, source=""):
        """Reject spot-only tickers for crypto exchanges."""
        if not ticker:
            return None
        if _is_perpetual(ticker):
            return ticker
        _log.warning(f"[MarketSymbols] can_trade_on_exchange: rejected SPOT "
                     f"ticker '{ticker}' for '{symbol}' on {exchange} ({source})")
        return None

    # DB lookup for exchange-specific ticker
    col_map = {"bybit": "bybit_ticker", "blofin": "blofin_ticker", "bitget": "bitget_ticker"}
    col = col_map.get(exchange)
    if col and db:
        resolved = resolve_symbol(s, db)
        for try_sym in ([resolved, s] if resolved != s else [s]):
            try:
                row = db.fetch_one(
                    f"SELECT {col} FROM market_symbols WHERE symbol = %s", (try_sym,))
                if row and row.get(col):
                    ticker = _reject_spot(row[col], "DB")
                    if ticker:
                        return (True, ticker)
            except Exception:
                pass

    # Inline CCXT market check (uses get_executor cache if available)
    try:
        from core.ccxt_executor import _executor_cache
        for aid, ex in _executor_cache.items():
            if ex.exchange_id == exchange and ex.connected:
                ticker = ex.resolve_symbol(s, db)
                if ticker:
                    ticker = _reject_spot(ticker, "CCXT cache")
                    if ticker:
                        return (True, ticker)
                break
    except Exception:
        pass

    # LLM fallback
    try:
        llm_resolved = resolve_symbol_with_llm(s, db)
        if llm_resolved and llm_resolved != s:
            if col and db:
                for try_sym in (llm_resolved,):
                    row = db.fetch_one(
                        f"SELECT {col} FROM market_symbols WHERE symbol = %s", (try_sym,))
                    if row and row.get(col):
                        ticker = _reject_spot(row[col], "LLM+DB")
                        if ticker:
                            return (True, ticker)
    except Exception:
        pass

    # On-demand mini-recon: check actual exchange markets for this symbol
    if asset_class == "cryptocurrency" and exchange in _CRYPTO_ONLY_EXCHANGES:
        try:
            recon = resolve_on_demand(s, db)
            col_key = exchange
            if recon.get(col_key):
                ticker = _reject_spot(recon[col_key], "recon")
                if ticker:
                    return (True, ticker)
        except Exception:
            pass
        return (False, f"Symbol {s} not found on {exchange} (recon checked, not listed)")

    return (False, f"Symbol {s} not available on {exchange}")


# ─────────────────────────────────────────────────────────────────
# Default Yahoo Finance ticker mappings for known symbols
# ─────────────────────────────────────────────────────────────────
DEFAULT_YAHOO_MAP = {
    # Forex majors
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "CAD=X", "USDCHF": "CHF=X",
    "NZDUSD": "NZDUSD=X",
    # Forex crosses
    "GBPNZD": "GBPNZD=X", "GBPJPY": "GBPJPY=X", "GBPCAD": "GBPCAD=X",
    "GBPCHF": "GBPCHF=X", "GBPAUD": "GBPAUD=X",
    "EURJPY": "EURJPY=X", "EURAUD": "EURAUD=X", "EURGBP": "EURGBP=X",
    "EURCHF": "EURCHF=X", "EURCAD": "EURCAD=X", "EURNZD": "EURNZD=X",
    "AUDCAD": "AUDCAD=X", "AUDNZD": "AUDNZD=X", "AUDJPY": "AUDJPY=X",
    "AUDCHF": "AUDCHF=X",
    "NZDCAD": "NZDCAD=X", "NZDJPY": "NZDJPY=X", "NZDCHF": "NZDCHF=X",
    "CADJPY": "CADJPY=X", "CADCHF": "CADCHF=X", "CHFJPY": "CHFJPY=X",
    # Commodities
    "XAUUSD": "GC=F", "XAGUSD": "SI=F", "USOIL": "CL=F",
    "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    # Indices
    "NAS100": "NQ=F", "US100": "NQ=F", "US30": "YM=F", "SPX500": "ES=F",
    "US500": "ES=F", "UK100": "^FTSE", "GER40": "^GDAXI", "JPN225": "^N225",
    # Crypto
    "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD", "BNBUSD": "BNB-USD",
    "SOLUSD": "SOL-USD", "XRPUSD": "XRP-USD", "ADAUSD": "ADA-USD",
    "DOTUSD": "DOT-USD", "DOGEUSD": "DOGE-USD", "AVAXUSD": "AVAX-USD",
    "MATICUSD": "MATIC-USD", "LINKUSD": "LINK-USD", "LTCUSD": "LTC-USD",
}

# ─────────────────────────────────────────────────────────────────
# Asset class classification rules (used when AI identification is unavailable)
# ─────────────────────────────────────────────────────────────────
def _classify_symbol(symbol: str) -> Dict[str, str]:
    """Classify a symbol into asset_class and category based on naming conventions.
    Handles MT5-style (BTCUSD), Binance-style (BTCUSDT), shorthand (BTC),
    and perpetual suffixes (BTCUSDT.P, ETHPERP, etc.).
    """
    s = symbol.upper()

    # Strip perpetual notation before classification (.P, -PERP, _PERP, PERP)
    s = re.sub(r'[.\-_]?P(?:ERP)?$', '', s)

    # ── Commodities (check first — specific symbols) ──
    if s in ("XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"):
        return {"asset_class": "commodity", "category": "precious_metals"}
    if s in ("USOIL", "UKOIL", "BRENT", "WTI", "NATGAS", "NGAS"):
        return {"asset_class": "commodity", "category": "energy"}
    if s in ("XAUEUR", "XAUAUD", "XAUCHF", "XAUGBP"):
        return {"asset_class": "commodity", "category": "precious_metals"}

    # ── Indices ──
    indices = {"NAS100", "US100", "US30", "SPX500", "US500", "UK100", "GER40",
               "JPN225", "AUS200", "FRA40", "ESP35", "HK50", "CHINA50",
               "VIX", "DXY", "USTEC", "USDX"}
    if s in indices:
        return {"asset_class": "index", "category": "indices"}

    # ── Forex: 6-char pairs where both halves are 3-char currency codes ──
    major_ccys = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
                  "SEK", "NOK", "DKK", "SGD", "HKD", "ZAR", "MXN", "TRY",
                  "PLN", "CZK", "HUF", "CNH", "CNY", "THB", "ILS", "INR"}
    if len(s) == 6:
        base, quote = s[:3], s[3:]
        if base in major_ccys and quote in major_ccys:
            if "USD" in s and (base == "USD" or quote == "USD"):
                return {"asset_class": "forex", "category": "forex_major"}
            return {"asset_class": "forex", "category": "forex_cross"}

    # ── Crypto: uses module-level CRYPTO_BASES set ──

    # Extract base token by stripping ALL known quote suffixes (longest first)
    base_token = None
    quote_suffixes = ["USDT", "FDUSD", "BUSD", "USDC", "USD", "BTC", "ETH", "BNB"]
    for suffix in quote_suffixes:
        if s.endswith(suffix) and len(s) > len(suffix):
            base_token = s[:-len(suffix)]
            break
    if not base_token and "_" in s:
        base_token = s.split("_")[0]
    if not base_token:
        base_token = s

    if base_token in CRYPTO_BASES:
        return {"asset_class": "cryptocurrency", "category": "crypto"}

    # Catch-all: anything ending in USDT/USDC/BUSD/BNB/BTC is almost certainly crypto
    if s.endswith("USDT") and len(s) > 4:
        return {"asset_class": "cryptocurrency", "category": "crypto"}
    if s.endswith("USDC") and len(s) > 4:
        return {"asset_class": "cryptocurrency", "category": "crypto"}
    if s.endswith("BUSD") and len(s) > 4:
        return {"asset_class": "cryptocurrency", "category": "crypto"}
    if s.endswith("FDUSD") and len(s) > 5:
        return {"asset_class": "cryptocurrency", "category": "crypto"}
    if s.endswith("BNB") and len(s) > 3:
        return {"asset_class": "cryptocurrency", "category": "crypto"}
    if s.endswith("BTC") and len(s) > 3:
        return {"asset_class": "cryptocurrency", "category": "crypto"}

    # Default: unknown
    return {"asset_class": "unknown", "category": "other"}


def _get_display_name(symbol: str) -> str:
    """Generate a human-readable display name for a symbol."""
    names = {
        "XAUUSD": "Gold / USD", "XAGUSD": "Silver / USD",
        "USOIL": "WTI Crude Oil", "UKOIL": "Brent Crude Oil",
        "NAS100": "Nasdaq 100", "US100": "Nasdaq 100",
        "US30": "Dow Jones 30", "SPX500": "S&P 500", "US500": "S&P 500",
        "UK100": "FTSE 100", "GER40": "DAX 40", "JPN225": "Nikkei 225",
        "BTCUSD": "Bitcoin / USD", "ETHUSD": "Ethereum / USD",
        "BNBUSD": "BNB / USD", "SOLUSD": "Solana / USD",
        "XRPUSD": "XRP / USD", "ADAUSD": "Cardano / USD",
        "DOGEUSD": "Dogecoin / USD", "LTCUSD": "Litecoin / USD",
    }
    if symbol in names:
        return names[symbol]
    s = symbol.upper()
    if len(s) == 6:
        return f"{s[:3]} / {s[3:]}"
    return symbol


# ─────────────────────────────────────────────────────────────────
# Database operations
# ─────────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS market_symbols (
    id INT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL UNIQUE,
    display_name VARCHAR(100) DEFAULT NULL,
    asset_class ENUM('forex','commodity','index','cryptocurrency','stock','etf','unknown') DEFAULT 'unknown',
    category VARCHAR(50) DEFAULT 'other',
    yahoo_ticker VARCHAR(30) DEFAULT NULL,
    mt5_symbol VARCHAR(30) DEFAULT NULL,
    first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    first_seen_source VARCHAR(100) DEFAULT NULL,
    signal_count INT DEFAULT 0,
    last_signal_at DATETIME DEFAULT NULL,
    show_in_alpha TINYINT(1) DEFAULT 1,
    submit_to_ai TINYINT(1) DEFAULT 1,
    detect_signals TINYINT(1) DEFAULT 1,
    tradable TINYINT(1) DEFAULT 0,
    notes TEXT DEFAULT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_asset_class (asset_class),
    INDEX idx_category (category),
    INDEX idx_tradable (tradable)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def ensure_market_symbols_table(db) -> None:
    """Create the market_symbols table if it doesn't exist."""
    try:
        db.execute(CREATE_TABLE_SQL)
        logger.info("[OK] market_symbols table ensured")
    except Exception as e:
        logger.error(f"Failed to create market_symbols table: {e}")


def register_symbol(db, symbol: str, source: str = None,
                     detect_signals: bool = None) -> Optional[Dict]:
    """
    Register a new symbol in the market_symbols table.
    If it already exists, increment signal_count and update last_signal_at.
    If detect_signals=True is passed and the symbol exists but has detect_signals=0,
    it will be upgraded (used for mentor bypass).

    For crypto symbols: auto-normalizes to USDT canonical and immediately attempts
    exchange ticker resolution (bybit_ticker, blofin_ticker).
    """
    symbol = symbol.upper().strip()
    if not is_valid_symbol(symbol):
        logger.warning(f"[SYMBOL] Rejected registration of invalid symbol: '{symbol}'")
        return None

    existing = db.fetch_one(
        "SELECT * FROM market_symbols WHERE symbol = %s", (symbol,)
    )

    if existing:
        db.execute(
            "UPDATE market_symbols SET signal_count = signal_count + 1, "
            "last_signal_at = NOW() WHERE symbol = %s",
            (symbol,)
        )
        if detect_signals is True and not existing.get('detect_signals'):
            db.execute(
                "UPDATE market_symbols SET detect_signals = 1, submit_to_ai = 1 "
                "WHERE symbol = %s", (symbol,))
            logger.info(f"[SYMBOL UPGRADE] {symbol}: detect_signals enabled (mentor bypass)")

        # Backfill: if crypto and missing canonical/exchange tickers, fix now
        _backfill_crypto_tickers(db, symbol, existing)
        return dict(existing)

    # --- New symbol ---
    canonical = SYMBOL_ALIASES.get(symbol)
    if not canonical:
        canonical = _normalize_crypto_to_usdt(symbol)

    classification = _classify_symbol(canonical or symbol)
    display_name = _get_display_name(canonical or symbol)
    yahoo_ticker = DEFAULT_YAHOO_MAP.get(canonical or symbol) or DEFAULT_YAHOO_MAP.get(symbol)

    is_unknown = classification["asset_class"] == "unknown"
    submit_to_ai = 0 if is_unknown else 1
    ds = 1 if detect_signals else (0 if is_unknown else 1)

    try:
        db.execute(
            "INSERT INTO market_symbols "
            "(symbol, display_name, asset_class, category, yahoo_ticker, canonical_symbol, "
            "first_seen_source, signal_count, last_signal_at, submit_to_ai, detect_signals) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, NOW(), %s, %s)",
            (symbol, display_name, classification["asset_class"],
             classification["category"], yahoo_ticker, canonical, source, submit_to_ai, ds)
        )
        logger.info(f"[NEW SYMBOL] {symbol} -> canonical={canonical} "
                     f"({classification['asset_class']}/{classification['category']}) "
                     f"(yahoo: {yahoo_ticker}, source: {source})")
    except Exception as e:
        if "Duplicate" in str(e):
            db.execute(
                "UPDATE market_symbols SET signal_count = signal_count + 1, "
                "last_signal_at = NOW() WHERE symbol = %s",
                (symbol,)
            )
        else:
            logger.error(f"Failed to register symbol {symbol}: {e}")
            return None

    row = db.fetch_one("SELECT * FROM market_symbols WHERE symbol = %s", (symbol,))

    # Immediately resolve exchange tickers for crypto
    if classification["asset_class"] == "cryptocurrency":
        _resolve_exchange_tickers_for_row(db, row)

    return row


def _backfill_crypto_tickers(db, symbol: str, row: Dict) -> None:
    """For existing crypto rows missing canonical_symbol or exchange tickers, fix them now."""
    if row.get("asset_class") != "cryptocurrency":
        return

    updates, params = [], []

    if not row.get("canonical_symbol"):
        usdt = _normalize_crypto_to_usdt(symbol)
        if usdt:
            updates.append("canonical_symbol = %s")
            params.append(usdt)

    if not row.get("bybit_ticker") or not row.get("blofin_ticker") or not row.get("bitget_ticker"):
        recon = resolve_on_demand(symbol, db)
        for eid in ("bybit", "blofin", "bitget"):
            col = f"{eid}_ticker"
            if recon.get(eid) and not row.get(col):
                updates.append(f"{col} = %s")
                params.append(recon[eid])
        if any(recon.get(eid) for eid in ("bybit", "blofin", "bitget")):
            pref = next((eid for eid in ("bybit", "blofin", "bitget") if recon.get(eid)), "bybit")
            if not row.get("preferred_exchange"):
                updates.append("preferred_exchange = %s")
                params.append(pref)

    if updates:
        params.append(symbol)
        db.execute(f"UPDATE market_symbols SET {', '.join(updates)} WHERE symbol = %s",
                   tuple(params))
        logger.info(f"[SYMBOL BACKFILL] {symbol}: {', '.join(updates)}")


def _resolve_exchange_tickers_for_row(db, row: Optional[Dict]) -> None:
    """Try to populate bybit_ticker/blofin_ticker/bitget_ticker for a crypto symbol row."""
    if not row:
        return
    symbol = row["symbol"]
    try:
        recon = resolve_on_demand(symbol, db)
        if any(recon.get(eid) for eid in ("bybit", "blofin", "bitget")):
            logger.info(f"[SYMBOL RESOLVE] {symbol}: bybit={recon.get('bybit')} "
                        f"blofin={recon.get('blofin')} bitget={recon.get('bitget')}")
    except Exception as e:
        logger.debug(f"[SYMBOL RESOLVE] {symbol}: on-demand resolve failed: {e}")


def normalize_and_resolve_all_symbols(db) -> Dict[str, Any]:
    """Bulk operation: scan ALL crypto symbols in market_symbols and ensure they have:
    1. canonical_symbol ending in USDT
    2. bybit_ticker and/or blofin_ticker populated
    3. LLM resolution attempted for truly unknown symbols

    Call this daily or after exchange recon to catch stragglers.
    Returns a report dict.
    """
    report = {"total": 0, "canonical_fixed": 0, "tickers_resolved": 0,
              "llm_resolved": 0, "still_unresolved": [], "errors": 0}

    rows = db.fetch_all(
        "SELECT symbol, canonical_symbol, bybit_ticker, blofin_ticker, bitget_ticker, asset_class "
        "FROM market_symbols WHERE asset_class = 'cryptocurrency'")
    if not rows:
        return report

    report["total"] = len(rows)
    needs_llm = []

    for row in rows:
        sym = row["symbol"]
        try:
            canonical = row.get("canonical_symbol")
            if not canonical or not canonical.endswith("USDT"):
                usdt = _normalize_crypto_to_usdt(sym)
                if not usdt:
                    usdt = SYMBOL_ALIASES.get(sym)
                if usdt and usdt.endswith("USDT"):
                    db.execute("UPDATE market_symbols SET canonical_symbol = %s "
                               "WHERE symbol = %s", (usdt, sym))
                    report["canonical_fixed"] += 1

            if not row.get("bybit_ticker") or not row.get("blofin_ticker") or not row.get("bitget_ticker"):
                recon = resolve_on_demand(sym, db)
                if any(recon.get(eid) for eid in ("bybit", "blofin", "bitget")):
                    report["tickers_resolved"] += 1
                elif sym.upper() not in _llm_resolve_cache:
                    needs_llm.append(sym)
        except Exception as e:
            logger.debug(f"[NORMALIZE] Error processing {sym}: {e}")
            report["errors"] += 1

    if needs_llm:
        logger.info(f"[NORMALIZE] Batch-resolving {len(needs_llm)} symbols via LLM")
        for i in range(0, len(needs_llm), 40):
            batch = needs_llm[i:i + 40]
            resolved = _batch_resolve_symbols_with_llm(batch, db)
            for sym in batch:
                s = sym.upper().strip()
                llm_result = resolved.get(s)
                if llm_result and llm_result != s:
                    recon2 = resolve_on_demand(llm_result, db)
                    if any(recon2.get(eid) for eid in ("bybit", "blofin", "bitget")):
                        db.execute(
                            "UPDATE market_symbols SET canonical_symbol = %s "
                            "WHERE symbol = %s", (llm_result, s))
                        _updated = db.fetch_one(
                            "SELECT * FROM market_symbols WHERE symbol = %s", (s,))
                        if _updated:
                            _backfill_crypto_tickers(db, s, dict(_updated))
                        report["llm_resolved"] += 1
                    else:
                        report["still_unresolved"].append(s)
                else:
                    report["still_unresolved"].append(s)

    logger.info(f"[NORMALIZE] Complete: {report['total']} crypto symbols checked, "
                f"{report['canonical_fixed']} canonical fixed, "
                f"{report['tickers_resolved']} tickers resolved, "
                f"{report['llm_resolved']} LLM resolved, "
                f"{len(report['still_unresolved'])} still unresolved")
    return report


# ─────────────────────────────────────────────────────────────────
# Waterfall-first resolution — grep the symbol against connected
# trading accounts in priority order so we know the exchange mapping
# from the moment a dossier is created or a signal is tested.
# ─────────────────────────────────────────────────────────────────

def resolve_via_waterfall(symbol: str, db=None) -> Dict[str, Optional[str]]:
    """Resolve a symbol against the actual connected waterfall trading accounts.

    Checks each account's executor (in waterfall_priority order) for a matching
    market. Returns the first hit per exchange, plus which account matched.

    Returns {"bybit": ticker|None, "blofin": ticker|None,
             "matched_account": account_id|None, "matched_exchange": exchange_id|None}.
    """
    result: Dict[str, Optional[str]] = {
        "bybit": None, "blofin": None, "bitget": None,
        "matched_account": None, "matched_exchange": None,
    }
    if not db:
        return result

    s = (symbol or "").upper().strip()
    base = s
    for suffix in ("USDT", "FDUSD", "BUSD", "USDC", "USD", "PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            base = s[:-len(suffix)]
            break

    try:
        accounts = db.fetch_all(
            "SELECT account_id, exchange FROM trading_accounts "
            "WHERE enabled = 1 "
            "ORDER BY waterfall_priority ASC, id ASC")
    except Exception:
        return result

    if not accounts:
        return result

    try:
        from core.ccxt_executor import _executor_cache
    except ImportError:
        return result

    for acct in accounts:
        aid = acct["account_id"]
        eid = (acct.get("exchange") or "").lower()

        executor = _executor_cache.get(aid)
        if not executor or not executor.connected:
            continue
        ex_obj = getattr(executor, '_exchange', None)
        if not ex_obj or not getattr(ex_obj, 'markets', None):
            continue

        ticker = _resolve_symbol_on_exchange(base, ex_obj)
        if not ticker:
            continue

        if eid in ("bybit", "blofin", "bitget") and not result[eid]:
            result[eid] = ticker

        if not result["matched_account"]:
            result["matched_account"] = aid
            result["matched_exchange"] = eid

        if result["bybit"] and result["blofin"] and result["bitget"]:
            break

    if result["bybit"] or result["blofin"] or result["bitget"]:
        logger.info(f"[WaterfallResolve] {s}: bybit={result['bybit']}, "
                    f"blofin={result['blofin']}, bitget={result['bitget']} "
                    f"(first match: {result['matched_account']})")
    else:
        logger.debug(f"[WaterfallResolve] {s}: not found on any "
                     f"waterfall account ({len(accounts)} checked)")

    if db and (result["bybit"] or result["blofin"] or result["bitget"]):
        try:
            row = db.fetch_one(
                "SELECT symbol FROM market_symbols WHERE symbol = %s", (s,))
            sets, params = [], []
            for eid in ("bybit", "blofin", "bitget"):
                if result[eid]:
                    sets.append(f"{eid}_ticker = COALESCE({eid}_ticker, %s)")
                    params.append(result[eid])
            if row:
                sets.append("asset_class = 'cryptocurrency'")
                sets.append("preferred_exchange = %s")
                params.append(result["matched_exchange"] or "bybit")
                params.append(s)
                db.execute(
                    f"UPDATE market_symbols SET {', '.join(sets)} "
                    f"WHERE symbol = %s", tuple(params))
            else:
                db.execute(
                    "INSERT INTO market_symbols "
                    "(symbol, asset_class, bybit_ticker, blofin_ticker, bitget_ticker, preferred_exchange) "
                    "VALUES (%s, 'cryptocurrency', %s, %s, %s, %s)",
                    (s, result["bybit"], result["blofin"], result["bitget"],
                     result["matched_exchange"] or "bybit"))
        except Exception as e:
            logger.debug(f"[WaterfallResolve] DB persist error for {s}: {e}")

    return result


# ─────────────────────────────────────────────────────────────────
# Golden normalization — THE single entry point for symbol cleanup
# ─────────────────────────────────────────────────────────────────

def normalize_for_dossier(symbol: str, db=None) -> Dict[str, Any]:
    """Normalize ANY incoming symbol to its canonical exchange-tradeable form.

    This is the AUTHORITATIVE function called before creating dossiers,
    registering signals, or subscribing to prices.

    Returns:
        {
            "normalized": str,       # Canonical symbol (e.g. "BTCUSDT")
            "raw": str,              # Original input (e.g. "BTC" or "BTCUSD.P")
            "exchange_verified": bool,  # True if found on Bybit or Blofin
            "bybit_ticker": str|None,
            "blofin_ticker": str|None,
            "asset_class": str,
            "method": str,           # How it was resolved
        }
    """
    raw = (symbol or "").upper().strip()
    result = {
        "normalized": raw, "raw": raw, "exchange_verified": False,
        "bybit_ticker": None, "blofin_ticker": None, "bitget_ticker": None,
        "asset_class": "unknown", "method": "passthrough",
    }
    if not raw:
        return result

    # Step 1: Static alias map (instant, handles known names/typos)
    if raw in SYMBOL_ALIASES:
        result["normalized"] = SYMBOL_ALIASES[raw]
        result["method"] = "alias"

    # Step 2: Crypto normalization — strip .P/PERP, convert USD/BUSD/USDC to USDT
    usdt_form = _normalize_crypto_to_usdt(result["normalized"])
    if usdt_form:
        result["normalized"] = usdt_form
        if result["method"] == "passthrough":
            result["method"] = "crypto_normalize"

    # Step 3: DB canonical_symbol lookup
    if db:
        try:
            row = db.fetch_one(
                "SELECT canonical_symbol, bybit_ticker, blofin_ticker, bitget_ticker, asset_class "
                "FROM market_symbols WHERE symbol = %s",
                (raw,))
            if row:
                if row.get("canonical_symbol"):
                    result["normalized"] = row["canonical_symbol"]
                    if result["method"] == "passthrough":
                        result["method"] = "db_canonical"
                for eid in ("bybit", "blofin", "bitget"):
                    col = f"{eid}_ticker"
                    if row.get(col):
                        result[col] = row[col]
                        result["exchange_verified"] = True
                if row.get("asset_class"):
                    result["asset_class"] = row["asset_class"]
        except Exception:
            pass

    # Step 4: Classify if still unknown
    if result["asset_class"] == "unknown":
        cls = _classify_symbol(result["normalized"])
        result["asset_class"] = cls["asset_class"]

    # Step 5: Waterfall-first resolution — check the actual connected trading
    # accounts (in priority order) before generic recon. This resolves the
    # exchange ticker at dossier creation time using real account data.
    if result["asset_class"] == "cryptocurrency" and not result["exchange_verified"]:
        try:
            wf = resolve_via_waterfall(result["normalized"], db)
            for eid in ("bybit", "blofin", "bitget"):
                if wf.get(eid):
                    result[f"{eid}_ticker"] = wf[eid]
                    result["exchange_verified"] = True
            if wf.get("matched_account"):
                result["waterfall_account"] = wf["matched_account"]
                result["waterfall_exchange"] = wf["matched_exchange"]
            if result["exchange_verified"] and result["method"] in ("passthrough", "crypto_normalize"):
                result["method"] = "waterfall"
        except Exception:
            pass

    # Step 5b: Fallback to generic exchange recon if waterfall didn't resolve
    if result["asset_class"] == "cryptocurrency" and not result["exchange_verified"]:
        try:
            recon = resolve_on_demand(result["normalized"], db)
            for eid in ("bybit", "blofin", "bitget"):
                if recon.get(eid):
                    result[f"{eid}_ticker"] = recon[eid]
                    result["exchange_verified"] = True
            if result["exchange_verified"] and result["method"] in ("passthrough", "crypto_normalize"):
                result["method"] = "exchange_recon"
        except Exception:
            pass

    # Step 6: LLM fallback for crypto symbols still not verified
    if result["asset_class"] == "cryptocurrency" and not result["exchange_verified"]:
        try:
            llm_result = resolve_symbol_with_llm(raw, db)
            if llm_result and llm_result != raw:
                llm_usdt = _normalize_crypto_to_usdt(llm_result)
                if llm_usdt:
                    llm_result = llm_usdt
                recon2 = resolve_on_demand(llm_result, db)
                if any(recon2.get(eid) for eid in ("bybit", "blofin", "bitget")):
                    result["normalized"] = llm_result
                    for eid in ("bybit", "blofin", "bitget"):
                        result[f"{eid}_ticker"] = recon2.get(eid)
                    result["exchange_verified"] = True
                    result["method"] = "llm"
                elif llm_result.endswith("USDT"):
                    result["normalized"] = llm_result
                    result["method"] = "llm_unverified"
        except Exception:
            pass

    # Step 6.5: Enforce USDT suffix — if exchange resolution confirmed a ticker
    # like ARC/USDT:USDT but the normalized symbol is still bare "ARC", append USDT.
    # This ensures the Trading Floor always shows USDT variants.
    if result["exchange_verified"] and result["asset_class"] == "cryptocurrency":
        norm = result["normalized"]
        if not any(norm.endswith(q) for q in ("USDT", "USDC", "BUSD", "USD")):
            result["normalized"] = norm + "USDT"

    # Step 7: Persist resolved data back to market_symbols for future lookups
    # Always persist when we have tickers OR the name changed
    has_new_tickers = any(result.get(f"{eid}_ticker") for eid in ("bybit", "blofin", "bitget"))
    name_changed = result["normalized"] != raw
    if db and (name_changed or has_new_tickers):
        try:
            updates, params = [], []
            if name_changed and not result.get("_db_canonical"):
                updates.append("canonical_symbol = %s")
                params.append(result["normalized"])
            for eid in ("bybit", "blofin", "bitget"):
                col = f"{eid}_ticker"
                if result.get(col):
                    updates.append(f"{col} = COALESCE({col}, %s)")
                    params.append(result[col])
            if updates:
                params.append(raw)
                db.execute(
                    f"UPDATE market_symbols SET {', '.join(updates)} WHERE symbol = %s",
                    tuple(params))
        except Exception:
            pass

    logger.info(f"[NORMALIZE] '{raw}' -> '{result['normalized']}' "
                f"(method={result['method']}, verified={result['exchange_verified']}, "
                f"class={result['asset_class']})")
    return result


def refresh_exchange_markets() -> Dict[str, int]:
    """Force-refresh Bybit and Blofin market listings from CCXT.

    Clears the cached markets in candle_collector so the next resolve_on_demand
    call will fetch fresh listings. Also refreshes the ccxt_executor cache.

    Returns market counts per exchange.
    """
    counts = {}
    try:
        from services.candle_collector import (
            _exchange_markets_cache, _exchange_markets_ts)
        import ccxt

        _bitget_opts = {"defaultType": "swap", "defaultSubType": "linear"}
        for eid in ("bybit", "blofin", "bitget"):
            try:
                cls = getattr(ccxt, eid, None)
                if not cls:
                    continue
                opts = {"enableRateLimit": True, "timeout": 15000}
                if eid == "bitget":
                    opts["options"] = _bitget_opts
                ex = cls(opts)
                mkts = ex.load_markets()
                _exchange_markets_cache[eid] = mkts
                _exchange_markets_ts[eid] = __import__("time").time()
                counts[eid] = len(mkts)
                logger.info(f"[REFRESH] {eid}: loaded {len(mkts)} markets (forced)")
            except Exception as e:
                logger.warning(f"[REFRESH] {eid} market refresh failed: {e}")
                counts[eid] = 0
    except ImportError:
        logger.warning("[REFRESH] candle_collector not available for market refresh")

    return counts


def _ensure_dossier_symbols_exist(db):
    """Make sure every active dossier symbol has a market_symbols row.
    Symbols like LUNAUSDT might never have been inserted."""
    try:
        missing = db.fetch_all("""
            SELECT DISTINCT d.symbol
            FROM trade_dossiers d
            LEFT JOIN market_symbols ms
              ON ms.symbol = d.symbol COLLATE utf8mb4_unicode_ci
            WHERE d.status IN ('proposed','monitoring','open_order','live')
              AND ms.symbol IS NULL
        """)
        for row in (missing or []):
            sym = row["symbol"]
            db.execute(
                "INSERT INTO market_symbols (symbol, asset_class) "
                "VALUES (%s, 'unknown') "
                "ON DUPLICATE KEY UPDATE symbol = symbol",
                (sym,))
            logger.info(f"[BACKFILL] Inserted missing market_symbols row "
                        f"for dossier symbol {sym}")
    except Exception as e:
        logger.debug(f"[BACKFILL] _ensure_dossier_symbols_exist error: {e}")


def _reclassify_unknown_with_tickers(db, report: dict):
    """Symbols with asset_class='unknown' but valid exchange tickers are
    definitely crypto. Reclassify them so the price streamer routes them
    to the crypto stream instead of Yahoo."""
    try:
        rows = db.fetch_all("""
            SELECT symbol FROM market_symbols
            WHERE asset_class = 'unknown'
              AND (bybit_ticker IS NOT NULL OR blofin_ticker IS NOT NULL OR bitget_ticker IS NOT NULL)
        """)
        for row in (rows or []):
            sym = row["symbol"]
            db.execute(
                "UPDATE market_symbols SET asset_class = 'cryptocurrency' "
                "WHERE symbol = %s", (sym,))
            report["reclassified"] = report.get("reclassified", 0) + 1
            logger.info(f"[BACKFILL] Reclassified {sym}: "
                        f"unknown → cryptocurrency (has exchange tickers)")
    except Exception as e:
        logger.debug(f"[BACKFILL] _reclassify_unknown error: {e}")


def _upgrade_spot_to_swap_tickers(db, report: dict):
    """Spot tickers (e.g. SHIB/USDT) are less useful than swap/linear tickers
    (SHIB1000/USDT:USDT) because the price streamer primarily connects to
    derivatives feeds.  Re-resolve symbols whose tickers lack ':USDT' to see
    if a swap alternative exists (using rebrand-aware resolution)."""
    try:
        rows = db.fetch_all("""
            SELECT symbol, bybit_ticker, blofin_ticker, bitget_ticker
            FROM market_symbols
            WHERE (bybit_ticker IS NOT NULL AND bybit_ticker NOT LIKE '%%:%%')
               OR (blofin_ticker IS NOT NULL AND blofin_ticker NOT LIKE '%%:%%')
               OR (bitget_ticker IS NOT NULL AND bitget_ticker NOT LIKE '%%:%%')
        """)
        if not rows:
            return
        import ccxt as _ccxt
        exchange_objs = {}
        for eid in ("bybit", "blofin", "bitget"):
            try:
                from services.candle_collector import _get_exchange_markets
                mkts = _get_exchange_markets(eid)
                if mkts:
                    exchange_objs[eid] = type('_P', (), {'markets': mkts})()
            except Exception:
                pass
            if eid not in exchange_objs:
                try:
                    ex_cls = getattr(_ccxt, eid, None)
                    if ex_cls:
                        ex = ex_cls({"enableRateLimit": True})
                        mkts = ex.load_markets()
                        if mkts:
                            exchange_objs[eid] = ex
                except Exception:
                    pass

        for row in rows:
            sym = row["symbol"]
            base = sym.upper()
            for sfx in ("USDT", "USDC", "USD"):
                if base.endswith(sfx) and len(base) > len(sfx):
                    base = base[:-len(sfx)]
                    break
            for eid in ("bybit", "blofin", "bitget"):
                col = f"{eid}_ticker"
                current = row.get(col)
                if not current or ":" in current:
                    continue
                ex = exchange_objs.get(eid)
                if not ex:
                    continue
                better = _resolve_symbol_on_exchange(base, ex)
                if better and ":" in better and better != current:
                    db.execute(
                        f"UPDATE market_symbols SET {col} = %s "
                        f"WHERE symbol = %s", (better, sym))
                    logger.info(f"[BACKFILL] {sym}: upgraded {eid} "
                                f"ticker {current} → {better} (swap)")
    except Exception as e:
        logger.debug(f"[BACKFILL] _upgrade_spot_to_swap error: {e}")


def backfill_exchange_tickers(db) -> Dict[str, Any]:
    """Resolve ALL crypto symbols with NULL bybit/blofin tickers against
    live exchange market listings.

    This fixes symbols that were inserted without exchange resolution
    (e.g. from signal seeding, or when the persistence bug skipped them).
    Also catches 'unknown' asset_class symbols that already have partial
    tickers and reclassifies them as crypto.

    Returns a report: {"total", "resolved", "failed", "details": [...]}.
    """
    report = {"total": 0, "resolved": 0, "already_ok": 0, "failed": 0,
              "reclassified": 0, "details": []}

    _ensure_dossier_symbols_exist(db)

    _reclassify_unknown_with_tickers(db, report)

    _upgrade_spot_to_swap_tickers(db, report)

    rows = db.fetch_all("""
        SELECT symbol, bybit_ticker, blofin_ticker, bitget_ticker, asset_class
        FROM market_symbols
        WHERE (asset_class = 'cryptocurrency' OR asset_class = 'unknown')
          AND (bybit_ticker IS NULL OR blofin_ticker IS NULL OR bitget_ticker IS NULL)
        ORDER BY symbol
    """)
    if not rows:
        logger.info("[BACKFILL] All crypto symbols already have exchange tickers")
        return report

    report["total"] = len(rows)
    logger.info(f"[BACKFILL] {len(rows)} crypto symbols with missing exchange tickers")

    for row in rows:
        sym = row["symbol"]
        had = {eid: bool(row.get(f"{eid}_ticker")) for eid in ("bybit", "blofin", "bitget")}

        try:
            recon = resolve_on_demand(sym, db)
            found = {eid: recon.get(eid) for eid in ("bybit", "blofin", "bitget")}
            newly_resolved = {eid: found[eid] for eid in found if found[eid] and not had[eid]}

            if newly_resolved:
                report["resolved"] += 1
                detail = {"symbol": sym, **newly_resolved}
                report["details"].append(detail)
                if row.get("asset_class") == "unknown":
                    db.execute(
                        "UPDATE market_symbols SET asset_class = 'cryptocurrency'"
                        " WHERE symbol = %s", (sym,))
                    report["reclassified"] = report.get("reclassified", 0) + 1
                    logger.info(f"[BACKFILL] {sym}: resolved tickers → "
                                f"reclassified as cryptocurrency")
            elif any(had.values()):
                report["already_ok"] += 1
            else:
                report["failed"] += 1
        except Exception as e:
            report["failed"] += 1
            logger.debug(f"[BACKFILL] {sym}: resolution error: {e}")

    logger.info(f"[BACKFILL] Done: {report['resolved']}/{report['total']} resolved, "
                f"{report['failed']} not found on exchanges")
    return report


def normalize_dossier_symbols(db) -> Dict[str, Any]:
    """Bulk-normalize all active dossier symbols to USDT canonical form.

    Scans trade_dossiers for symbols that don't end in USDT (for crypto) and
    normalizes them. Stores original in raw_symbol column. Non-crypto symbols
    are left unchanged.

    Returns a report of changes made.
    """
    report = {"checked": 0, "normalized": 0, "already_ok": 0, "skipped": 0,
              "changes": []}

    rows = db.fetch_all("""
        SELECT id, symbol, raw_symbol FROM trade_dossiers
        WHERE status IN ('proposed', 'monitoring', 'open_order', 'live',
                         'won', 'lost', 'expired', 'abandoned')
    """)
    if not rows:
        return report

    for row in rows:
        report["checked"] += 1
        sym = row["symbol"]
        dossier_id = row["id"]

        norm = normalize_for_dossier(sym, db)
        new_sym = norm["normalized"]

        if new_sym == sym:
            report["already_ok"] += 1
            continue

        if norm["asset_class"] not in ("cryptocurrency",):
            report["skipped"] += 1
            continue

        try:
            db.execute("""
                UPDATE trade_dossiers
                SET symbol = %s, raw_symbol = COALESCE(raw_symbol, %s)
                WHERE id = %s
            """, (new_sym, sym, dossier_id))
            report["normalized"] += 1
            report["changes"].append({"id": dossier_id, "old": sym, "new": new_sym})
            logger.info(f"[NORMALIZE] Dossier #{dossier_id}: {sym} -> {new_sym}")
        except Exception as e:
            logger.debug(f"[NORMALIZE] Dossier #{dossier_id} update failed: {e}")

    logger.info(f"[NORMALIZE] Dossier bulk: {report['checked']} checked, "
                f"{report['normalized']} normalized, {report['already_ok']} already ok, "
                f"{report['skipped']} skipped (non-crypto)")
    return report


def get_yahoo_ticker(db, symbol: str) -> Optional[str]:
    """
    Get the Yahoo Finance ticker for a symbol.
    First checks market_symbols table, falls back to DEFAULT_YAHOO_MAP.
    """
    symbol = symbol.upper().strip()

    row = db.fetch_one(
        "SELECT yahoo_ticker FROM market_symbols WHERE symbol = %s", (symbol,)
    )
    if row and row.get("yahoo_ticker"):
        return row["yahoo_ticker"]

    # Fallback to default map
    return DEFAULT_YAHOO_MAP.get(symbol, f"{symbol}=X")


def get_all_symbols(db) -> List[Dict]:
    """Get all registered symbols for the Markets UI."""
    rows = db.fetch_all(
        "SELECT * FROM market_symbols ORDER BY asset_class, symbol"
    )
    return [dict(r) for r in (rows or [])]


def update_symbol_settings(db, symbol: str, settings: Dict[str, Any]) -> bool:
    """Update per-symbol settings (show_in_alpha, submit_to_ai, detect_signals, tradable, notes)."""
    allowed_fields = {"show_in_alpha", "submit_to_ai", "detect_signals",
                      "tradable", "notes", "yahoo_ticker", "mt5_symbol",
                      "display_name", "category",
                      "bybit_ticker", "blofin_ticker", "bitget_ticker",
                      "preferred_exchange", "fallback_exchange"}
    updates = {k: v for k, v in settings.items() if k in allowed_fields}
    if not updates:
        return False

    set_parts = [f"{k} = %s" for k in updates]
    values = list(updates.values()) + [symbol.upper()]

    db.execute(
        f"UPDATE market_symbols SET {', '.join(set_parts)} WHERE symbol = %s",
        tuple(values)
    )
    return True


def seed_from_existing_signals(db) -> int:
    """
    Seed market_symbols from existing parsed_signals.
    Called once during bootstrap to populate from historical data.
    Returns count of new symbols added.
    """
    rows = db.fetch_all(
        "SELECT symbol, COUNT(*) as cnt, MIN(parsed_at) as first_seen, "
        "MAX(parsed_at) as last_seen, "
        "GROUP_CONCAT(DISTINCT source ORDER BY source) as sources "
        "FROM parsed_signals WHERE symbol IS NOT NULL AND symbol != '' "
        "GROUP BY symbol"
    )
    if not rows:
        return 0

    count = 0
    for row in rows:
        symbol = row["symbol"].upper().strip()
        if not symbol or not is_valid_symbol(symbol):
            continue

        existing = db.fetch_one(
            "SELECT id FROM market_symbols WHERE symbol = %s", (symbol,)
        )
        if existing:
            # Update counts
            db.execute(
                "UPDATE market_symbols SET signal_count = %s, last_signal_at = %s "
                "WHERE symbol = %s",
                (row["cnt"], row["last_seen"], symbol)
            )
            continue

        classification = _classify_symbol(symbol)
        display_name = _get_display_name(symbol)
        yahoo_ticker = DEFAULT_YAHOO_MAP.get(symbol)

        try:
            db.execute(
                "INSERT IGNORE INTO market_symbols "
                "(symbol, display_name, asset_class, category, yahoo_ticker, "
                "first_seen_at, first_seen_source, signal_count, last_signal_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (symbol, display_name, classification["asset_class"],
                 classification["category"], yahoo_ticker,
                 row["first_seen"], row.get("sources", "historical"),
                 row["cnt"], row["last_seen"])
            )
            count += 1
        except Exception as e:
            logger.error(f"Failed to seed symbol {symbol}: {e}")

    if count > 0:
        logger.info(f"[SEED] Added {count} symbols from existing signals")
    return count


def reclassify_unknown_symbols(db) -> int:
    """
    Re-run rule-based classification on symbols still marked as 'unknown'.
    Only determines asset_class (forex, commodity, index, cryptocurrency).
    Crypto sub-categories (memecoin, layer1, defi, etc.) are left for AI to classify.
    Called during bootstrap. Returns count of reclassified symbols.
    """
    rows = db.fetch_all(
        "SELECT symbol, asset_class, category FROM market_symbols WHERE asset_class = 'unknown'"
    )
    if not rows:
        return 0

    count = 0
    for row in rows:
        symbol = row["symbol"].upper().strip()
        classification = _classify_symbol(symbol)
        if classification["asset_class"] == "unknown":
            continue

        display_name = _get_display_name(symbol)
        db.execute(
            "UPDATE market_symbols SET asset_class = %s, category = %s, display_name = %s "
            "WHERE symbol = %s",
            (classification["asset_class"], classification["category"], display_name, symbol)
        )
        count += 1
        logger.debug(f"[RECLASSIFY] {symbol} -> {classification['asset_class']}/{classification['category']}")

    if count > 0:
        logger.info(f"[RECLASSIFY] Classified {count} previously-unknown symbols")
    return count
