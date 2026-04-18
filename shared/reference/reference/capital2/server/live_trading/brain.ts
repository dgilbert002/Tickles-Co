/**
 * Brain Preview & Simulate
 * 
 * This module implements the "brain" logic for live trading - the same calculation
 * that runs when a bot needs to decide whether to buy or hold.
 * 
 * KEY CONCEPTS:
 * 1. A strategy contains multiple tests (indicators), each with its own epic and timeframe
 * 2. We load historical candle data from Capital.com (5min candles) for each unique epic
 * 3. At T-60s, we fetch current price via API to create the "fake 5min" candle
 * 4. We aggregate 5min candles to target timeframes (15m, 1h, 4h, 1d) as needed
 * 5. We run the SAME Python strategy executor that backtest uses
 * 6. We return individual test results to show which indicators passed/failed
 * 
 * DATA SOURCE:
 * - ALL data comes from Capital.com (stored in UTC)
 * - NO timezone conversion is needed - everything is UTC
 * - T-60 API call provides the "current" price to form the fake 5min candle
 * 
 * IMPORTANT: We do NOT modify any strategy or indicator logic - we just provide
 * the data and let the existing backtest engine do its job.
 */

// SQLite removed - now using MySQL via Drizzle ORM
import { sql } from 'drizzle-orm';
import { getStrategy } from '../db';
import { createCapitalAPIClient } from './credentials';
import { executeStrategy } from '../strategy_bridge';
import { getCurrentSignal, SignalConfig } from '../signal_bridge';
import { orchestrationLogger } from '../orchestration/logger';
import { resolveConflictSync, type ConflictSignal, type ConflictMode } from '../conflict_bridge';
import { createBrainLog, BrainLogger } from '../logging/file_logger';
// DB_PATH no longer needed - using MySQL

/**
 * Calculate required days of data based on strategy timeframes AND indicator parameters
 * 
 * Different indicators need different amounts of historical data:
 * - RSI with period=14 needs at least 14 candles
 * - SMA with period=200 needs at least 200 candles
 * - EMA crossover with slow_period=200 needs at least 200 candles
 * 
 * This function:
 * 1. Finds the longest timeframe in the strategy
 * 2. Finds the largest period/lookback parameter across all indicators
 * 3. Calculates days needed to get enough candles for the most demanding indicator
 */
function calculateRequiredDays(tests: any[]): number {
  const BASE_CANDLES = 300; // Minimum safe buffer (increased from 200)
  const SAFETY_MARGIN = 2.0; // 100% extra for safety (increased from 1.5)
  const MIN_DAYS = 5; // Absolute minimum days to load
  
  // Find the longest timeframe in the strategy
  let maxMinutes = 5; // Default to 5m
  
  // Find the largest period parameter across all indicators
  let maxPeriod = BASE_CANDLES;
  
  for (const test of tests) {
    // Check timeframe
    const timeframe = test.timeframe || '5m';
    const minutes = parseTimeframeToMinutes(timeframe);
    if (minutes > maxMinutes) {
      maxMinutes = minutes;
    }
    
    // Check indicator parameters for period-related values
    // These parameters indicate how many candles the indicator needs
    const params = test.indicatorParams || test.params || {};
    
    const periodParams = [
      'period',           // RSI, SMA, EMA, CCI, etc.
      'slow_period',      // Moving average crossovers
      'long_period',      // MACD, etc.
      'lookback',         // Various indicators
      'lookback_period',  // Some custom indicators
      'rank_period',      // ConnorsRSI
      'atr_period',       // Keltner, ATR-based indicators
      'bb_period',        // Bollinger Bands
      'kc_period',        // Keltner Channel
      'dc_period',        // Donchian Channel
      'window',           // Rolling window indicators
    ];
    
    for (const paramName of periodParams) {
      const value = params[paramName];
      if (typeof value === 'number' && value > maxPeriod) {
        maxPeriod = value;
        console.log(`[Brain] Found large period param: ${paramName}=${value} in ${test.indicatorName}`);
      }
    }
  }
  
  // Add buffer for indicator warmup (some indicators need extra candles to stabilize)
  // Use 3x the period to ensure indicator has fully stabilized
  const candlesNeeded = Math.max(maxPeriod * 3, BASE_CANDLES);
  
  // Calculate days needed: (candles × minutes per candle) / (minutes per day)
  const minutesNeeded = candlesNeeded * maxMinutes;
  const daysNeeded = minutesNeeded / (60 * 24); // Convert to days
  const daysWithSafety = Math.max(Math.ceil(daysNeeded * SAFETY_MARGIN), MIN_DAYS);
  
  console.log(`[Brain] Calculated data window:`);
  console.log(`  - Max timeframe: ${maxMinutes}m`);
  console.log(`  - Max period param: ${maxPeriod}`);
  console.log(`  - Candles needed: ${candlesNeeded} (${maxPeriod} × 3 or ${BASE_CANDLES} minimum)`);
  console.log(`  - Days to load: ${daysWithSafety} (with ${SAFETY_MARGIN}x safety margin, min ${MIN_DAYS} days)`);
  
  return daysWithSafety;
}

/**
 * Parse timeframe string to minutes
 */
function parseTimeframeToMinutes(timeframe: string): number {
  const match = timeframe.match(/(\d+)([mhd])/);
  if (!match) return 5; // Default to 5m
  
  const value = parseInt(match[1]);
  const unit = match[2];
  
  switch (unit) {
    case 'm': return value;
    case 'h': return value * 60;
    case 'd': return value * 60 * 24;
    default: return 5;
  }
}

/**
 * Brain result interface - what we return to the frontend
 */
export interface BrainResult {
  decision: 'buy' | 'hold';
  winningIndicator: string;
  winningTestId: number;
  winningEpic: string;
  winningTimeframe: string;
  winningReason: string; // "Best Sharpe Ratio (9.82)" or "Best Return (+8734.38%)"
  conflictMode: string; // "single_best_sharpe", "single_best_return", etc.
  confidence: number;
  leverage: number | null;
  stopLoss: number | null;
  stopLossPercent: number | null; // Stop loss percentage from winning DNA config (e.g., 2.15)
  guaranteedStopEnabled: boolean; // Whether to use guaranteed stop loss (pays premium, no slippage)
  // HMH (Hold Means Hold) - set new SL on HOLD instead of closing
  hmhEnabled: boolean; // Whether HMH is enabled for winning DNA strand
  hmhStopLossOffset: number | null; // SL offset: 0 (original), -1 (orig-1%), -2 (orig-2%)
  timingConfig?: { mode: string; [key: string]: any }; // Winning DNA timing config (e.g., { mode: "Fake5min_4thCandle" })
  winningDataSource?: string; // Winning DNA data source ('capital' or 'av')
  indicatorValue?: number;
  indicatorParams?: Record<string, any>; // Parameters used by the winning indicator (for validation)
  lastCapitalCandle?: string; // Last candle from Capital.com (UTC)
  usedT60ApiPrice: boolean; // Whether the T-60s API price was used to create fake 5min candle
  executionTimeMs: number; // Time taken to calculate in milliseconds
  
  // Brain calculation price tracking (for validation accuracy)
  // These allow comparing brain's T-60 API price vs backtest's 4th 1m candle
  brainCalcPrice?: number;      // T-60 API price used for indicator calculation
  fake5minClose?: number;       // 4th 1m candle close (what backtest would use)
  priceVariancePct?: number;    // Variance between brainCalcPrice and fake5minClose (%)
  last1mCandleTime?: string;    // Timestamp of the 4th 1m candle used (ISO string)
  
  // Candle range used for calculation (for validation)
  candleStartDate?: string; // ISO timestamp of first candle
  candleEndDate?: string;   // ISO timestamp of last candle
  candleCount?: number;     // Number of candles used
  
  // All DNA test results (for comprehensive validation)
  allDnaResults?: {
    dnaResults: Array<{
      testIndex: number;
      indicatorName: string;
      indicatorParams: Record<string, any>;
      epic: string;
      timeframe: string;
      signal: 'BUY' | 'HOLD';
      indicatorValue: number | null;
      sharpeRatio: number | null;
      totalReturn: number | null;
      winRate: number | null;
      maxDrawdown: number | null;
    }>;
    conflictResolution: {
      metric: string;
      winnerIndex: number;
      reason: string;
      hadConflict: boolean;
    };
  };
  
  allTests: {
    testId: number;
    indicatorName: string;
    epic: string;
    timeframe: string;
    passed: boolean;
    value?: number;
    threshold?: number;
    sharpe?: number;
    return?: number;
    leverage?: number;
    stopLoss?: number;
    winRate?: number;
    maxDrawdown?: number;
    maxProfit?: number;
  }[];
  marketData: {
    epic: string;
    bid: number;
    ask: number;
    mid: number;
    timestamp: Date;
  };
  dataSource: 'capital.com' | 'websocket';
  candlesLoaded: number; // Total candles loaded from database
  analyzedAt: Date;
  completedIn: string;
  executionLogs: string[]; // Detailed logs of brain execution for debugging
  dataStaleError?: string; // Error message when data is too old for trading (>10 min)
  marketClosed?: boolean; // True if the hold decision is due to market being closed
}

/**
 * Get setting value from MySQL database
 */
async function getSetting(category: string, key: string, defaultValue: any): Promise<any> {
  try {
    const { getDb } = await import('../db');
    const { settings } = await import('../../drizzle/schema_settings');
    const { eq, and } = await import('drizzle-orm');
    
    const db = await getDb();
    if (!db) return defaultValue;
    
    const results = await db
      .select()
      .from(settings)
      .where(and(
        eq(settings.category, category),
        eq(settings.key, key)
      ))
      .limit(1);
    
    if (results.length === 0) return defaultValue;
    
    const result = results[0];
    const { value, valueType } = result;
    if (valueType === 'number') return parseFloat(value);
    if (valueType === 'boolean') return value === 'true';
    if (valueType === 'json') return JSON.parse(value);
    return value;
  } catch (error: any) {
    console.error('[Brain] Error getting setting:', error.message);
    return defaultValue;
  }
}

/**
 * Load last N days of 5min candle data for an epic from MySQL database
 * 
 * Loads Capital.com candle data from the unified 'candles' table.
 * ALL DATA IS STORED IN UTC - no timezone conversion needed.
 * 
 * @param epic - The epic symbol (e.g., 'SOXL', 'PLTR')
 * @param days - Number of days of historical data to load
 * @returns Array of candle objects with timestamp, closePrice, openPrice, highPrice, lowPrice, volume
 * 
 * NOTE: Function was previously named 'loadAVData' when it loaded AlphaVantage data.
 * Now loads exclusively from Capital.com data (source='capital', timeframe='5m').
 * 
 * IMPORTANT: All data is stored as 5min candles. If a test needs 15min/1h/4h/1d,
 * we aggregate the 5min candles later using the same logic as backtest.
 */
export async function loadCandleData(epic: string, days: number): Promise<any[]> {
  const { getDb } = await import('../db');
  const db = await getDb();
  if (!db) {
    console.error('[Brain] Database not available');
    return [];
  }
  
  // Calculate approximate number of candles needed
  // 5min candles: 12 per hour × 24 hours = 288 per day
  const candlesNeeded = Math.ceil(days * 288 * 1.5); // 1.5x safety margin
  
  console.log(`[Brain] Loading last ${candlesNeeded} 5min candles for ${epic} (${days} days worth)`);
  
  // Try loading from unified candles table first (Capital.com data)
  try {
    const result: any = await db.execute(
      sql.raw(`
        SELECT 
          timestamp,
          CAST(close_bid AS DECIMAL(10,4)) as closePrice,
          CAST(open_bid AS DECIMAL(10,4)) as openPrice,
          CAST(high_bid AS DECIMAL(10,4)) as highPrice,
          CAST(low_bid AS DECIMAL(10,4)) as lowPrice,
          0 as volume
        FROM candles
        WHERE epic = '${epic}' 
          AND source = 'capital' 
          AND timeframe = '5m'
        ORDER BY timestamp DESC
        LIMIT ${candlesNeeded}
      `)
    );
    
    const rows = Array.isArray(result) && result.length > 0 ? result[0] : result;
    const candles = Array.isArray(rows) ? rows.reverse() : [];
    
    if (candles.length > 0) {
      const formattedCandles = candles.map((c: any) => {
        // CRITICAL: MySQL timestamps are stored in UTC, but Date() interprets them as local time
        // We need to force UTC interpretation by appending 'Z' or using UTC constructor
        let ts: string;
        if (c.timestamp instanceof Date) {
          // Already a Date - convert to ISO string (but it might be in local TZ)
          // Create a new Date treating the components as UTC
          const d = c.timestamp;
          ts = new Date(Date.UTC(
            d.getFullYear(), d.getMonth(), d.getDate(),
            d.getHours(), d.getMinutes(), d.getSeconds()
          )).toISOString();
        } else {
          // String timestamp - append Z to force UTC interpretation
          const tsStr = String(c.timestamp).replace(' ', 'T');
          ts = tsStr.endsWith('Z') ? tsStr : `${tsStr}Z`;
          ts = new Date(ts).toISOString();
        }
        return {
          ...c,
          timestamp: ts
        };
      });
      
      console.log(`[Brain] Loaded ${formattedCandles.length} 5min candles for ${epic} from Capital.com data`);
      if (formattedCandles.length > 0) {
        console.log(`[Brain] First candle: ${formattedCandles[0].timestamp}, Last candle: ${formattedCandles[formattedCandles.length - 1].timestamp}`);
      }
      return formattedCandles;
    }
  } catch (error: any) {
    console.error(`[Brain] Error loading data for ${epic}:`, error.message);
    return [];
  }
}

/**
 * In-memory cache for Capital.com candles
 * Key: {epic}:{lastTimestamp}
 * TTL: 5 minutes (candles don't change once closed)
 */
const capitalCandleCache = new Map<string, { data: any[]; fetchedAt: Date }>();

/**
 * Store Capital.com candles in database for validation
 */
async function storeCapitalCandles(epic: string, candles: any[]): Promise<void> {
  try {
    const { getDb } = await import('../db');
    const { capitalCandlesCache } = await import('../../drizzle/schema');
    
    const db = await getDb();
    if (!db || candles.length === 0) return;
    
    // Insert candles (ignore duplicates)
    for (const candle of candles) {
      try {
        await db.insert(capitalCandlesCache).values({
          epic,
          timestamp: new Date(candle.timestamp),
          openPrice: candle.openPrice,
          highPrice: candle.highPrice,
          lowPrice: candle.lowPrice,
          closePrice: candle.closePrice,
          volume: candle.volume,
        }).onDuplicateKeyUpdate({ set: { epic } }); // Ignore duplicates
      } catch (err: any) {
        // Ignore duplicate key errors
        if (!err.message?.includes('Duplicate')) {
          console.error('[Brain] Failed to store candle:', err.message);
        }
      }
    }
  } catch (error: any) {
    console.error('[Brain] Failed to store Capital.com candles:', error.message);
  }
}

/**
 * Fill gap between last timestamp and now using Capital.com API
 * 
 * ALL TIMESTAMPS ARE IN UTC - no timezone conversion needed.
 * Capital.com data is stored in UTC in the database.
 * 
 * Features:
 * - In-memory caching (5min TTL) to avoid redundant API calls
 * - Comprehensive logging (gap detection, API calls, cache hits)
 * - Database storage for validation
 */
export async function fillGapWithCapitalCom(
  epic: string,
  lastTimestamp: string,
  environment: 'demo' | 'live'
): Promise<{ candles: any[]; count: number; cached?: boolean }> {
  const startTime = Date.now();
  
  try {
    // ALL timestamps are in UTC - parse directly
    const lastDate = new Date(lastTimestamp.endsWith('Z') ? lastTimestamp : `${lastTimestamp}Z`);
    const now = new Date();
    
    // Calculate gap in hours
    const diffMs = now.getTime() - lastDate.getTime();
    const gapHours = diffMs / (1000 * 60 * 60);
    
    // Calculate expected candles: 12 five-minute candles per hour
    const expectedCandles = Math.floor(gapHours * 12);
    
    if (expectedCandles <= 0) {
      console.log('[Brain] No gap to fill');
      return { candles: [], count: 0, cached: false };
    }
    
    // Log gap detection
    await orchestrationLogger.info('BRAIN_DATA_FETCHED', 
      `Gap detected for ${epic}: ${gapHours.toFixed(1)} hours (~${expectedCandles} candles expected)`,
      {
        epic,
        data: {
          lastTimestamp,
          gapHours: gapHours.toFixed(1),
          expectedCandles,
        }
      }
    );
    
    // Check cache first
    const cacheKey = `${epic}:${lastTimestamp}`;
    const cached = capitalCandleCache.get(cacheKey);
    const CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes
    
    if (cached && (Date.now() - cached.fetchedAt.getTime()) < CACHE_TTL_MS) {
      const duration = Date.now() - startTime;
      await orchestrationLogger.info('BRAIN_DATA_FETCHED', 
        `Using cached Capital.com data for ${epic} (${cached.data.length} candles, ${duration}ms)`,
        {
          epic,
          data: {
            cached: true,
            candleCount: cached.data.length,
            cacheAge: Math.floor((Date.now() - cached.fetchedAt.getTime()) / 1000),
            durationMs: duration,
          }
        }
      );
      return { candles: cached.data, count: cached.data.length, cached: true };
    }
    
    // Cache miss - fetch from Capital.com
    const api = await createCapitalAPIClient(environment);
    if (!api) {
      await orchestrationLogger.warn('BRAIN_ERROR', 
        `No API client available for gap filling (${epic})`,
        { epic }
      );
      return { candles: [], count: 0, cached: false };
    }
    
    // Fetch prices from Capital.com (5min resolution)
    // Cap at 1000 candles to avoid Capital.com API limit (error.invalid.max)
    const MAX_CAPITAL_CANDLES = 1000;
    const requestCandles = Math.min(expectedCandles + 10, MAX_CAPITAL_CANDLES);
    console.log(`[Brain] Requesting ${requestCandles} candles from Capital.com (expected: ${expectedCandles}, capped at ${MAX_CAPITAL_CANDLES})`);
    const prices = await api.getHistoricalPrices(epic, 'MINUTE_5', requestCandles);
    
    if (!prices || prices.length === 0) {
      await orchestrationLogger.warn('BRAIN_ERROR', 
        `No prices returned from Capital.com for ${epic}`,
        { epic }
      );
      return { candles: [], count: 0, cached: false };
    }
    
    // ═══════════════════════════════════════════════════════════════════════════
    // ALL TIMESTAMPS IN UTC - NO TIMEZONE CONVERSION
    // Capital.com returns snapshotTimeUTC which we use directly
    // ═══════════════════════════════════════════════════════════════════════════
    
    console.log(`[Brain] ═══════════════════════════════════════════════════════════`);
    console.log(`[Brain] Gap Fill for ${epic} (ALL UTC)`);
    console.log(`[Brain] ═══════════════════════════════════════════════════════════`);
    console.log(`[Brain] Last timestamp (UTC): ${lastDate.toISOString()}`);
    console.log(`[Brain] Capital.com prices received: ${prices.length}`);
    
    // Log first few Capital.com candles for verification
    if (prices.length > 0) {
      console.log(`[Brain] Sample Capital.com candles (first 3):`);
      prices.slice(0, 3).forEach((p: any, i: number) => {
        console.log(`[Brain]   [${i}] UTC: ${p.snapshotTimeUTC}`);
      });
    }
    
    const newCandles = prices
      .filter((p: any) => {
        // Filter candles newer than our last timestamp (both in UTC)
        const candleTimeUTC = new Date(p.snapshotTimeUTC || p.snapshotTime || p.timestamp);
        return candleTimeUTC > lastDate;
      })
      .map((p: any) => {
        // Keep timestamp in UTC (ISO format)
        const utcTimestamp = p.snapshotTimeUTC || p.snapshotTime || p.timestamp;
        const utcDate = new Date(utcTimestamp);
        
        return {
          timestamp: utcDate.toISOString(), // Store as ISO UTC string
          openPrice: parseFloat(String(p.openPrice?.bid || p.open || 0)),
          highPrice: parseFloat(String(p.highPrice?.bid || p.high || 0)),
          lowPrice: parseFloat(String(p.lowPrice?.bid || p.low || 0)),
          closePrice: parseFloat(String(p.closePrice?.bid || p.close || 0)),
          volume: parseInt(String(p.lastTradedVolume || p.volume || 0)),
        };
      })
      .sort((a: any, b: any) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
    
    // Log results
    console.log(`[Brain] Candles after filtering: ${newCandles.length}`);
    if (newCandles.length > 0) {
      console.log(`[Brain] First gap-fill candle (UTC): ${newCandles[0].timestamp}`);
      console.log(`[Brain] Last gap-fill candle (UTC):  ${newCandles[newCandles.length - 1].timestamp}`);
    }
    console.log(`[Brain] ═══════════════════════════════════════════════════════════`);
    
    // Cache the result
    capitalCandleCache.set(cacheKey, { data: newCandles, fetchedAt: new Date() });
    
    // Store in database for validation (async, don't await)
    storeCapitalCandles(epic, newCandles).catch(err => 
      console.error('[Brain] Failed to store Capital.com candles:', err.message)
    );
    
    const duration = Date.now() - startTime;
    await orchestrationLogger.info('BRAIN_DATA_FETCHED', 
      `Fetched ${newCandles.length} candles from Capital.com for ${epic} (${duration}ms)`,
      {
        epic,
        data: {
          cached: false,
          candleCount: newCandles.length,
          timestampRange: newCandles.length > 0 ? {
            first: newCandles[0].timestamp,
            last: newCandles[newCandles.length - 1].timestamp,
          } : null,
          durationMs: duration,
        }
      }
    );
    
    return { candles: newCandles, count: newCandles.length, cached: false };
  } catch (error: any) {
    await orchestrationLogger.logError('BRAIN_ERROR', 
      `Error filling gap for ${epic}`, 
      error,
      { epic }
    );
    return { candles: [], count: 0, cached: false };
  }
}

/**
 * Aggregate 5min candles to target timeframe
 * 
 * This mimics the Python aggregate_timeframe.py logic:
 * - 15m = 3x 5min candles
 * - 1h = 12x 5min candles
 * - 4h = 48x 5min candles
 * - 1d = 288x 5min candles (assuming 24h trading)
 */
function aggregateCandles(candles: any[], timeframe: string): any[] {
  if (timeframe === '5m' || timeframe === '5min') {
    return candles; // No aggregation needed
  }
  
  const timeframeMap: Record<string, number> = {
    '15m': 3,
    '15min': 3,
    '1h': 12,
    '1hour': 12,
    '4h': 48,
    '4hour': 48,
    '1d': 288,
    '1day': 288,
  };
  
  const candlesPerPeriod = timeframeMap[timeframe.toLowerCase()];
  if (!candlesPerPeriod) {
    console.warn(`[Brain] Unknown timeframe ${timeframe}, using 5min`);
    return candles;
  }
  
  console.log(`[Brain] Aggregating ${candles.length} 5min candles to ${timeframe} (${candlesPerPeriod} candles per period)`);
  
  const aggregated = [];
  for (let i = 0; i < candles.length; i += candlesPerPeriod) {
    const periodCandles = candles.slice(i, i + candlesPerPeriod);
    if (periodCandles.length === 0) continue;
    
    // Aggregate: open=first, close=last, high=max, low=min, volume=sum
    aggregated.push({
      timestamp: periodCandles[0].timestamp,
      openPrice: periodCandles[0].openPrice,
      closePrice: periodCandles[periodCandles.length - 1].closePrice,
      highPrice: Math.max(...periodCandles.map(c => c.highPrice)),
      lowPrice: Math.min(...periodCandles.map(c => c.lowPrice)),
      volume: periodCandles.reduce((sum, c) => sum + (c.volume || 0), 0),
    });
  }
  
  console.log(`[Brain] Aggregated to ${aggregated.length} ${timeframe} candles`);
  return aggregated;
}

/**
 * Load historical test results from database for a given test configuration
 * 
 * This matches test configs against backtestResults by:
 * - epic
 * - indicatorName
 * - indicatorParams (JSON match)
 * - leverage
 * - stopLoss
 * - timeframe
 * 
 * Returns the BEST result (highest Sharpe ratio) if multiple matches found
 */
export async function loadHistoricalTestResult(test: any): Promise<any | null> {
  // Extract query parameters for logging
  const queryParams = {
    epic: test.epic || 'SOXL',
    indicatorName: test.indicatorName,
    timeframe: test.timeframe || '5m',
    leverage: test.leverage || 1,
    stopLoss: test.stopLoss || 10,
  };
  
  // Retry configuration for connection pool exhaustion
  const MAX_RETRIES = 3;
  const RETRY_DELAY_MS = 100; // Start with 100ms, doubles each retry
  
  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      const { getDb } = await import('../db');
      const { backtestResults } = await import('../../drizzle/schema');
      const { eq, and, desc } = await import('drizzle-orm');
      
      const db = await getDb();
      if (!db) {
        console.error(`[Brain] ERROR: Database not available for loadHistoricalTestResult`);
        console.error(`[Brain] Query would have been: ${JSON.stringify(queryParams)}`);
        return null;
      }
      
      // Query for matching test results
      // Note: We can't easily match on JSON params, so we'll get all results for this indicator+epic+timeframe
      // and filter in memory
      const results = await db
        .select()
        .from(backtestResults)
        .where(and(
          eq(backtestResults.epic, queryParams.epic),
          eq(backtestResults.indicatorName, queryParams.indicatorName),
          eq(backtestResults.timeframe, queryParams.timeframe),
          eq(backtestResults.leverage, queryParams.leverage),
          eq(backtestResults.stopLoss, queryParams.stopLoss)
        ))
        .orderBy(desc(backtestResults.sharpeRatio)) // Get best result first
        .limit(10); // Get top 10 to filter by params
      
      if (results.length === 0) {
        // More detailed logging for missing data
        console.warn(`[Brain] ⚠️ No backtest results found for: ${queryParams.indicatorName} | ${queryParams.epic} | ${queryParams.timeframe} | ${queryParams.leverage}x | ${queryParams.stopLoss}% SL`);
        console.warn(`[Brain]    This DNA strand will return HOLD (no historical data to compare)`);
        return null;
      }
      
      // Filter by params match (in memory since JSON comparison is tricky in SQL)
      const testParams = JSON.stringify(test.indicatorParams || {});
      const matchingResult = results.find(r => {
        const resultParams = JSON.stringify(r.indicatorParams || {});
        return resultParams === testParams;
      });
      
      if (matchingResult) {
        console.log(`[Brain] ✅ Found historical result for ${test.indicatorName}: Sharpe=${matchingResult.sharpeRatio?.toFixed(2)}, Return=${matchingResult.totalReturn?.toFixed(1)}%`);
        return matchingResult;
      }
      
      // If no exact param match, use the best result (first one due to orderBy)
      console.log(`[Brain] ⚠️ No exact param match for ${test.indicatorName}, using best available (Sharpe=${results[0].sharpeRatio?.toFixed(2)})`);
      return results[0];
    } catch (error: any) {
      // Check if this is a connection pool exhaustion error (retryable)
      const isRetryable = error.message?.includes('Failed query') || 
                          error.message?.includes('ETIMEDOUT') ||
                          error.message?.includes('ECONNREFUSED') ||
                          error.code === 'PROTOCOL_CONNECTION_LOST';
      
      if (isRetryable && attempt < MAX_RETRIES) {
        const delay = RETRY_DELAY_MS * Math.pow(2, attempt - 1); // Exponential backoff
        console.warn(`[Brain] ⚠️ Query failed (attempt ${attempt}/${MAX_RETRIES}), retrying in ${delay}ms...`);
        await new Promise(resolve => setTimeout(resolve, delay));
        continue; // Retry
      }
      
      // Max retries reached or non-retryable error
      console.error(`[Brain] ❌ ERROR loading historical test result (after ${attempt} attempts)`);
      console.error(`[Brain]    Query params: ${JSON.stringify(queryParams)}`);
      console.error(`[Brain]    Error: ${error.message}`);
      if (error.stack) {
        console.error(`[Brain]    Stack: ${error.stack.split('\n').slice(0, 3).join('\n')}`);
      }
      return null;
    }
  }
  
  return null; // Should never reach here
}

/**
 * Brain Preview - Calculate what the strategy would do RIGHT NOW
 * 
 * This is the main function that:
 * 1. Loads the strategy and its tests
 * 2. Loads historical test results from database for each test
 * 3. Identifies all unique (epic, timeframe) pairs
 * 4. Loads historical candle data from Capital.com (all UTC)
 * 5. Uses T-60 API price to create fake 5min candle (if provided)
 * 6. Aggregates to target timeframes
 * 7. Runs the Python strategy executor
 * 8. Parses individual test results
 * 9. Returns buy/hold decision with historical metrics
 */
export async function brainPreview(
  strategyId: number,
  accountId: number,
  environment: 'demo' | 'live',
  filterEpics?: string[], // Optional: filter tests to only these epics
  epicDataCache?: Record<string, any[]>, // Optional: pre-fetched epic data to avoid redundant API calls
  epicPrices?: Record<string, number>, // Optional: T-60s API prices for Fake5min_4thCandle mode
  executionMode: 'parallel' | 'sequential' = 'parallel' // Track execution mode for logging
): Promise<BrainResult> {
  const executionStartTime = Date.now();
  const executionLogs: string[] = [];
  executionLogs.push(`=== BRAIN PREVIEW EXECUTION ===`);
  
  // Create file logger for detailed logging
  let fileLogger: BrainLogger | null = null;
  
  try {
    console.log(`[Brain] Starting preview for strategy ${strategyId}, account ${accountId}, environment ${environment}`);
    
    // 1. Load strategy configuration
    const strategy = await getStrategy(strategyId);
    if (!strategy) {
      throw new Error('Strategy not found');
    }
    
    // Get strategy tests (already parsed by getStrategy)
    let tests = strategy.tests || [];
    console.log(`[Brain] Strategy has ${tests.length} tests`);
    
    // Initialize file logger with account and strategy info
    // Get account name for logging
    const { accounts: accountsTable } = await import('../../drizzle/schema');
    const { eq: eqOp } = await import('drizzle-orm');
    const { getDb: getDbFn } = await import('../db');
    const dbForLog = await getDbFn();
    let accountName = `Account_${accountId}`;
    if (dbForLog) {
      const [acc] = await dbForLog.select().from(accountsTable).where(eqOp(accountsTable.id, accountId)).limit(1);
      if (acc) accountName = acc.accountName || accountName;
    }
    
    fileLogger = createBrainLog(accountId, accountName, strategyId, strategy.name || `Strategy_${strategyId}`, executionMode);
    fileLogger.log(`Environment: ${environment}`);
    fileLogger.log(`Filter Epics: ${filterEpics?.join(', ') || 'ALL'}`);
    fileLogger.log(`Epic Prices (T-60): ${epicPrices ? JSON.stringify(epicPrices) : 'None'}`);
    fileLogger.log(`Using Pre-fetched Data: ${epicDataCache ? 'YES' : 'NO'}`);
    
    // Filter tests by epic if filterEpics is provided
    if (filterEpics && filterEpics.length > 0) {
      const originalCount = tests.length;
      tests = tests.filter((t: any) => {
        const testEpic = t.epic || 'SOXL';
        return filterEpics.includes(testEpic);
      });
      console.log(`[Brain] Filtered tests from ${originalCount} to ${tests.length} (epics: ${filterEpics.join(', ')})`);
      executionLogs.push(`Filtered to epics: ${filterEpics.join(', ')} (${tests.length} tests)`);
    }
    
    if (tests.length === 0) {
      throw new Error('No tests match the selected window epics. Please check window configuration.');
    }
    
    // 2. Load historical test results from database for each test
    // TODO: Add fallback to strategy.tests[i].performance if backtestResults not found
    // This would allow brain to work even if original backtest is deleted
    // Current behavior: Queries backtestResults table by matching epic, indicator, params, etc.
    // Future enhancement: If no match found, use tests[i].performance (stored in strategy)
    console.log('[Brain] Loading historical test results from database (PARALLEL)...');
    const historicalResults: Record<number, any> = {}; // Map test index to historical result
    const historicalStartTime = Date.now();
    
    fileLogger?.historicalResultsStart(tests.length);
    
    // Run all historical result lookups in PARALLEL
    const historicalPromises = tests.map((test: any, i: number) => 
      loadHistoricalTestResult(test).then(result => ({ index: i, result, test }))
    );
    const historicalResultsArray = await Promise.all(historicalPromises);
    
    for (const { index, result, test } of historicalResultsArray) {
      if (result) {
        historicalResults[index] = result;
        fileLogger?.historicalResult(index, test.indicatorName, true, result.sharpeRatio, result.totalReturn);
      } else {
        fileLogger?.historicalResult(index, test.indicatorName, false);
      }
      // TODO: Fallback if not found:
      // if (!result && tests[index].performance) {
      //   historicalResults[index] = {
      //     sharpeRatio: tests[index].performance.sharpeRatio,
      //     totalReturn: tests[index].performance.totalReturn,
      //     winRate: tests[index].performance.winRate,
      //     maxDrawdown: tests[index].performance.maxDrawdown,
      //   };
      //   console.log(`[Brain] Using stored performance for test ${index}`);
      // }
    }
    const historicalDuration = Date.now() - historicalStartTime;
    console.log(`[Brain] Loaded ${Object.keys(historicalResults).length} historical results (parallel) in ${historicalDuration}ms`);
    fileLogger?.historicalResultsComplete(Object.keys(historicalResults).length, historicalDuration);
    
    // 3. Load account to get data window settings
    const { getAccount } = await import('../db');
    const account = await getAccount(accountId);
    if (!account) {
      throw new Error('Account not found');
    }
    
    // Calculate days to load based on account settings
    let daysToLoad: number;
    if (account.dataWindowMode === 'fixed' && account.dataWindowDays) {
      daysToLoad = account.dataWindowDays;
      console.log(`[Brain] Using fixed data window: ${daysToLoad} days`);
    } else {
      daysToLoad = calculateRequiredDays(tests);
      console.log(`[Brain] Using dynamic data window: ${daysToLoad} days (calculated from strategy timeframes)`);
    }
    
    // 3. Extract unique (epic, timeframe) pairs from tests
    const epicTimeframePairs = new Set<string>();
    tests.forEach((t: any) => {
      const epic = t.epic || 'SOXL';
      const timeframe = t.timeframe || '5m';
      epicTimeframePairs.add(`${epic}:${timeframe}`);
    });
    
    console.log(`[Brain] Unique (epic, timeframe) pairs:`, Array.from(epicTimeframePairs));
    
    // 4. Load and merge data for each unique epic
    // Note: We load 5min data for each epic, then aggregate to target timeframes later
    const epicDataMap: Record<string, any[]> = {};
    const lastCandleTimestamps: Record<string, string> = {}; // Track last candle timestamp per epic (UTC)
    const uniqueEpics = Array.from(new Set(tests.map((t: any) => t.epic || 'SOXL')));
    
    let totalGapCandles = 0;
    let lastCapitalTimestamp: string | null = null;
    
    // Check if we have pre-fetched data
    let usingPrefetchedCache = false;
    if (epicDataCache) {
      console.log('[Brain] Using pre-fetched epic data cache');
      executionLogs.push('Using pre-fetched epic data (global optimization)');
      usingPrefetchedCache = true;
      
      for (const epic of uniqueEpics) {
        const epicStr = String(epic);
        if (epicDataCache[epicStr]) {
          epicDataMap[epicStr] = epicDataCache[epicStr];
          // Extract last timestamp from cached data (all Capital.com UTC data)
          if (epicDataCache[epicStr].length > 0) {
            const lastCandle = epicDataCache[epicStr][epicDataCache[epicStr].length - 1];
            lastCandleTimestamps[epicStr] = lastCandle.timestamp;
            // Check data freshness
            const lastTimestamp = new Date(lastCandle.timestamp);
            const now = new Date();
            const ageMinutes = (now.getTime() - lastTimestamp.getTime()) / (1000 * 60);
            // If data is recent (< 30 min), assume gap-fill worked
            if (ageMinutes < 30) {
              totalGapCandles += 1; // Mark that we have gap-filled data
              console.log(`[Brain] Cached data for ${epic} is fresh (${ageMinutes.toFixed(1)} min old, last: ${lastCandle.timestamp})`);
            } else {
              console.log(`[Brain] Cached data for ${epic} is ${ageMinutes.toFixed(1)} min old (last: ${lastCandle.timestamp})`);
            }
          }
          console.log(`[Brain] Using cached data for ${epic}: ${epicDataCache[epicStr].length} candles`);
        } else {
          console.warn(`[Brain] No cached data for ${epic}, will fetch`);
        }
      }
    }
    
    // Fetch any missing epics that weren't in the cache
    for (const epic of uniqueEpics) {
      const epicStr = String(epic);
      if (epicDataMap[epicStr]) {
        continue; // Already have data from cache
      }
      
      // Load 5min data from unified candles table (Capital.com data)
      const candleData = await loadCandleData(String(epic), daysToLoad);
      if (candleData.length === 0) {
        console.warn(`[Brain] No data for ${epic}, skipping`);
        continue;
      }
      
      // Get last timestamp (data from candles table is already up-to-date)
      const lastTimestamp = candleData[candleData.length - 1].timestamp;
      lastCandleTimestamps[String(epic)] = lastTimestamp;
      
      // Store the data
      epicDataMap[String(epic)] = candleData;
      totalGapCandles = candleData.length; // Track total candles loaded
      lastCapitalTimestamp = lastTimestamp;
      
      console.log(`[Brain] ═══════════════════════════════════════════════════════════`);
      console.log(`[Brain] DATA LOADED for ${epic}`);
      console.log(`[Brain] ═══════════════════════════════════════════════════════════`);
      console.log(`[Brain] Total candles: ${candleData.length}`);
      console.log(`[Brain] First candle: ${candleData[0]?.timestamp}`);
      console.log(`[Brain] Last candle: ${lastTimestamp}`);
      
      // Check data freshness
      const lastTime = new Date(lastTimestamp);
      const now = new Date();
      const ageMinutes = (now.getTime() - lastTime.getTime()) / (1000 * 60);
      
      executionLogs.push(``);
      executionLogs.push(`=== DATA: ${epic} ===`);
      executionLogs.push(`Candles loaded: ${candleData.length}`);
      executionLogs.push(`Last candle: ${lastTimestamp}`);
      executionLogs.push(`Data age: ${ageMinutes.toFixed(1)} minutes`);
      
      if (ageMinutes < 10) {
        console.log(`[Brain] ✅ Data is fresh (${ageMinutes.toFixed(1)} min old)`);
        executionLogs.push(`✅ Data is fresh`);
      } else {
        console.log(`[Brain] ⚠️ Data is ${ageMinutes.toFixed(1)} min old`);
        executionLogs.push(`⚠️ Data may be stale`);
      }
      console.log(`[Brain] ═══════════════════════════════════════════════════════════`);
    }
    
    // 5. Check market hours status for each epic
    console.log('[Brain] Checking market hours status...');
    const now = new Date();
    
    // Get primary epic (first epic) for market data and error messages
    const primaryEpic = uniqueEpics[0] || 'SOXL';
    
    // Load market status from database
    const { getDb } = await import('../db');
    const { epics: epicsTable } = await import('../../drizzle/schema');
    const { eq, inArray } = await import('drizzle-orm');
    const db = await getDb();
    
    let marketStatusMap: Record<string, { status: string; nextOpen: Date | null; nextClose: Date | null }> = {};
    let anyMarketClosed = false;
    let marketClosedEpics: string[] = [];
    
    if (db) {
      const epicRecords = await db.select().from(epicsTable).where(inArray(epicsTable.symbol, uniqueEpics.map(String)));
      for (const epic of epicRecords) {
        marketStatusMap[epic.symbol] = {
          status: epic.marketStatus || 'UNKNOWN',
          nextOpen: epic.nextOpen,
          nextClose: epic.nextClose,
        };
        
        if (epic.marketStatus === 'CLOSED') {
          anyMarketClosed = true;
          marketClosedEpics.push(epic.symbol);
        }
        
        console.log(`[Brain] ${epic.symbol} market status: ${epic.marketStatus}, nextOpen: ${epic.nextOpen?.toISOString() || 'N/A'}`);
        executionLogs.push(`${epic.symbol} market: ${epic.marketStatus}${epic.nextOpen ? `, opens at ${epic.nextOpen.toISOString()}` : ''}`);
      }
    }
    
    // 6. Validate data freshness
    // KEY INSIGHT: If we have epicPrices (T-60 API prices), we can create a "fake" candle
    // with fresh data, so historical data age is less critical.
    // Only block if market is CLOSED and we have NO real-time prices.
    console.log('[Brain] Validating data freshness...');
    const FRESH_DATA_THRESHOLD_MINUTES = 10; // Warn if data is >10 min old
    let dataSource = 'capital.com'; // All data from Capital.com (UTC)
    let dataAgeWarning = '';
    let hasT60Prices = epicPrices && Object.keys(epicPrices).length > 0;
    
    for (const epic of uniqueEpics) {
      const epicData = epicDataMap[String(epic)];
      if (!epicData || epicData.length === 0) {
        const errorMsg = `No data available for ${epic}. Cannot run brain preview.`;
        console.error(`[Brain] ❌ ${errorMsg}`);
        executionLogs.push(`❌ ERROR: ${errorMsg}`);
        throw new Error(errorMsg);
      }
      
      // Check if last candle is recent enough
      const lastCandle = epicData[epicData.length - 1];
      const lastTimestamp = new Date(lastCandle.timestamp);
      const ageMinutes = (now.getTime() - lastTimestamp.getTime()) / (1000 * 60);
      const ageHours = ageMinutes / 60;
      
      console.log(`[Brain] ${epic} last candle: ${lastCandle.timestamp} (${ageMinutes.toFixed(1)} min old)`);
      executionLogs.push(`${epic} last candle: ${lastCandle.timestamp} (${ageMinutes.toFixed(1)} min old)`);
      
      const marketInfo = marketStatusMap[String(epic)];
      const isMarketClosed = marketInfo?.status === 'CLOSED';
      
      if (ageMinutes > FRESH_DATA_THRESHOLD_MINUTES) {
        // Data is older than threshold
        if (hasT60Prices && epicPrices![String(epic)]) {
          // We have T-60 API price for this epic - we'll create a fake candle with fresh data
          // This makes the effective data fresh, so just log a note
          const t60Price = epicPrices![String(epic)];
          console.log(`[Brain] ✓ ${epic} historical data is ${ageMinutes.toFixed(1)} min old, but using T-60 API price (${t60Price}) for fresh signal`);
          executionLogs.push(`✓ ${epic}: Using T-60 API price ${t60Price} (historical: ${ageMinutes.toFixed(1)} min old)`);
          dataSource = 'Capital.com (API + T-60 price)';
        } else if (isMarketClosed) {
          // Market is closed AND no T-60 prices - only then do we block
          const nextOpenStr = marketInfo?.nextOpen 
            ? `Market opens at ${marketInfo.nextOpen.toISOString()}`
            : 'Next open time unknown';
          
          dataAgeWarning = `MARKET CLOSED: ${epic} market is currently closed. ${nextOpenStr}. Brain preview should only be run during market hours for accurate signals.`;
          console.log(`[Brain] ⚠️ ${dataAgeWarning}`);
          executionLogs.push(`⚠️ ${dataAgeWarning}`);
          // Note: We still continue with calculations - the signal will reflect "at close" state
        } else {
          // Market is open but we don't have T-60 prices - log warning but CONTINUE
          // The signal calculation will use historical data which is still valid for preview
          dataAgeWarning = `DATA NOTE: ${epic} data is ${ageMinutes.toFixed(0)} min old. Signal reflects last available data.`;
          console.log(`[Brain] ⚠️ ${dataAgeWarning}`);
          executionLogs.push(`⚠️ ${dataAgeWarning}`);
          dataSource = 'Capital.com (historical)';
        }
      } else if (ageMinutes > FRESH_DATA_THRESHOLD_MINUTES - 5) {
        // Data is 5-10 min old - good but approaching stale
        console.log(`[Brain] ✓ ${epic} data is ${ageMinutes.toFixed(1)} min old - fresh`);
        executionLogs.push(`✓ ${epic} data is fresh (${ageMinutes.toFixed(1)} min old)`);
        dataSource = 'Capital.com (WebSocket + API)';
      } else {
        // Data is fresh (<5 min old)
        console.log(`[Brain] ✓ ${epic} data is very fresh (${ageMinutes.toFixed(1)} min old)`);
        executionLogs.push(`✓ ${epic} data is very fresh (${ageMinutes.toFixed(1)} min old)`);
        dataSource = 'Capital.com (WebSocket + API)';
      }
    }
    
    // 6. Get current market data for display (use primary epic)
    const api = await createCapitalAPIClient(environment);
    let bid = 0, ask = 0, mid = 0;
    if (api) {
      const marketInfo = await api.getMarketInfo(String(primaryEpic));
      if (marketInfo) {
        bid = parseFloat(String(marketInfo.snapshot?.bid || 0));
        ask = parseFloat(String(marketInfo.snapshot?.offer || 0));
        mid = (bid + ask) / 2;
      }
    }
    
    // 6. Check current signals for ALL tests (PARALLEL)
    console.log(`[Brain] Checking current signals for ${tests.length} tests (${executionMode.toUpperCase()})...`);
    const signalCalcStartTime = Date.now();
    fileLogger?.signalCalculationStart(tests.length, executionMode === 'parallel');
    
    const startDate = new Date();
    startDate.setDate(startDate.getDate() - daysToLoad);
    const endDate = new Date();
    
    // Add strategy info to execution logs
    executionLogs.push(`Strategy: ${strategy.name}`);
    executionLogs.push(`Total Tests: ${tests.length}`);
    executionLogs.push(`Conflict Mode: ${strategy.conflictResolution?.mode || 'single_best_sharpe'}`);
    executionLogs.push(`Date Range: ${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}`);
    executionLogs.push('');
    executionLogs.push(`=== CHECKING ALL TESTS FOR BUY SIGNALS (PARALLEL) ===`);
    
    // Run ALL signal calculations in PARALLEL for speed
    // Each test spawns a Python process - running in parallel drastically reduces total time
    const signalPromises = tests.map(async (test: any, i: number) => {
      try {
        // Get per-DNA configuration (MUST match original backtest for consistent signals)
        const dnaTimingConfig = test.timingConfig || { mode: 'Fake5min_3rdCandle_API' };
        const dnaDataSource = (test as any).dataSource || 'capital';
        const dnaCrashProtection = (test as any).crashProtectionEnabled || false;
        
        // Use getCurrentSignal for efficient indicator calculation
        // This is the SAME indicator logic as backtest_runner.py but just checks the last candle
        const testEpic = String(test.epic || primaryEpic);
        const timingMode = dnaTimingConfig.mode || 'Fake5min_3rdCandle_API';
        
        // CRITICAL: For Fake5min_4thCandle mode, use the T-60s API price
        // This ensures brain uses the same "fake 5-min close" as backtest runner would
        let fake5minClose: number | undefined;
        let fake5minTimestamp: string | undefined;
        
        if (timingMode === 'Fake5min_4thCandle' && epicPrices && epicPrices[testEpic]) {
          fake5minClose = epicPrices[testEpic];
          fake5minTimestamp = new Date().toISOString();
          // Log outside of parallel execution to avoid interleaved logs
        }
        
        const signalConfig: SignalConfig = {
          db_path: '', // Not used, kept for compatibility
          epic: testEpic,
          start_date: startDate.toISOString().split('T')[0],
          end_date: endDate.toISOString().split('T')[0],
          indicator_name: test.indicatorName,
          indicator_params: test.indicatorParams || {},
          // Per-DNA configuration (MUST match original backtest for consistent signals)
          data_source: dnaDataSource,
          timing_config: dnaTimingConfig,
          crash_protection_enabled: dnaCrashProtection,  // Match original backtest
          // CRITICAL: Pass fake_5min_close for Fake5min_4thCandle mode
          // This makes brain calculation match backtest runner exactly
          fake_5min_close: fake5minClose,
          fake_5min_timestamp: fake5minTimestamp,
        };
        
        const signalResult = await getCurrentSignal(signalConfig);
        
        return {
          testIndex: i,
          test,
          signalResult,
          dnaTimingConfig,
          dnaDataSource,
          dnaCrashProtection,
          fake5minClose,
          error: null as string | null,
        };
      } catch (error: any) {
        return {
          testIndex: i,
          test,
          signalResult: null,
          dnaTimingConfig: test.timingConfig || { mode: 'Fake5min_3rdCandle_API' },
          dnaDataSource: (test as any).dataSource || 'capital',
          dnaCrashProtection: (test as any).crashProtectionEnabled || false,
          fake5minClose: undefined,
          error: error.message,
        };
      }
    });
    
    // Wait for ALL signals to complete before continuing
    const signalResultsArray = await Promise.all(signalPromises);
    
    // Process results in order and build currentSignals array
    const currentSignals: Array<{testIndex: number, signal: number, indicatorValue: number | null}> = [];
    
    for (const result of signalResultsArray) {
      const { testIndex: i, test, signalResult, dnaTimingConfig, dnaDataSource, dnaCrashProtection, fake5minClose, error } = result;
      
      if (error) {
        const errorLog = `Test ${i}: ${test.indicatorName} → ERROR: ${error}`;
        executionLogs.push(errorLog);
        console.error(`[Brain] ${errorLog}`);
        currentSignals.push({
          testIndex: i,
          signal: 0,
          indicatorValue: null,
        });
        continue;
      }
      
      if (!signalResult) {
        currentSignals.push({
          testIndex: i,
          signal: 0,
          indicatorValue: null,
        });
        continue;
      }
      
      const hasBuySignal = signalResult.signal === 1;
      const signalText = hasBuySignal ? 'BUY' : (signalResult.crash_blocked ? 'BLOCKED' : 'HOLD');
      const indicatorValueStr = signalResult.indicator_value !== null 
        ? ` | Value: ${signalResult.indicator_value.toFixed(2)}` 
        : '';
      const crashLabel = dnaCrashProtection ? ', crashProt=ON' : '';
      const timingLabel = dnaTimingConfig.mode || 'default';
      const priceLabel = fake5minClose ? ` [T-60 price: ${fake5minClose}]` : '';
      const logLine = `Test ${i}: ${test.indicatorName} (${test.epic || primaryEpic}, ${test.timeframe || '5m'}, ${timingLabel}, ${dnaDataSource}${crashLabel})${priceLabel} → ${signalText}${indicatorValueStr} | Params: ${JSON.stringify(test.indicatorParams || {})}`;
      executionLogs.push(logLine);
      console.log(`[Brain] ${logLine}`);
      
      // Log to file
      fileLogger?.signalResult(i, test.indicatorName, test.epic || primaryEpic, signalText, signalResult.indicator_value);
      
      // Log crash protection blocking
      if (signalResult.crash_blocked) {
        const crashLog = `  🛡️ Signal BLOCKED by crash protection: ${signalResult.crash_reason}`;
        executionLogs.push(crashLog);
        console.log(`[Brain] ${crashLog}`);
      }
      
      if (signalResult.data_warning) {
        executionLogs.push(`  ⚠️ ${signalResult.data_warning}`);
        console.warn(`[Brain] ${signalResult.data_warning}`);
      }
      
      currentSignals.push({
        testIndex: i,
        signal: signalResult.signal,
        indicatorValue: signalResult.indicator_value,
      });
    }
    
    // 7. Filter tests that signal BUY
    const buySignals = currentSignals.filter(s => s.signal === 1);
    const signalCalcDuration = Date.now() - signalCalcStartTime;
    
    // Log signal calculation summary
    fileLogger?.signalCalculationsComplete(buySignals.length, tests.length - buySignals.length, signalCalcDuration);
    
    executionLogs.push(``);
    executionLogs.push(`=== DECISION LOGIC ===`);
    executionLogs.push(`Buy Signals: ${buySignals.length} out of ${tests.length} tests`);
    
    if (buySignals.length > 0) {
      executionLogs.push(`Tests signaling BUY:`);
      buySignals.forEach(sig => {
        const test = tests[sig.testIndex];
        const historical = historicalResults[sig.testIndex];
        executionLogs.push(`  - Test ${sig.testIndex}: ${test.indicatorName} (Sharpe: ${historical?.sharpeRatio?.toFixed(2) || 'N/A'}, Return: ${historical?.totalReturn?.toFixed(2) || 'N/A'}%)`);
      });
    }
    
    console.log(`[Brain] Buy signals: ${buySignals.length} out of ${tests.length} tests`);
    
    let decision: 'buy' | 'hold' = 'hold';
    let winningTestIndex = 0;
    let winningHistorical = historicalResults[0];
    let winningTest = tests[0];
    
    if (buySignals.length === 0) {
      // No tests say BUY → HOLD
      // === USE UNIFIED CONFLICT RESOLUTION for best historical performer reference ===
      executionLogs.push(``);
      executionLogs.push(`DECISION: HOLD`);
      executionLogs.push(`Reason: No tests are currently signaling BUY`);
      console.log('[Brain] No buy signals → HOLD');
      decision = 'hold';
      
      // Still show best historical performer for reference using unified conflict resolution
      const holdConflictMode = (strategy.conflictResolution?.mode || 'sharpeRatio') as ConflictMode;
      const allSignals: ConflictSignal[] = tests.map((test: any, idx: number) => ({
        index: idx,
        indicatorName: test.indicatorName || 'unknown',
        sharpe: historicalResults[idx]?.sharpeRatio || 0,
        totalReturn: historicalResults[idx]?.totalReturn || 0,
        winRate: historicalResults[idx]?.winRate || 0,
        maxDrawdown: historicalResults[idx]?.maxDrawdown || 0,
      }));
      
      winningTestIndex = resolveConflictSync(allSignals, holdConflictMode);
      winningTest = tests[winningTestIndex];
      winningHistorical = historicalResults[winningTestIndex];
    } else if (buySignals.length === 1) {
      // One test says BUY → BUY with that test
      executionLogs.push(``);
      executionLogs.push(`DECISION: BUY`);
      executionLogs.push(`Reason: Only one test is signaling BUY`);
      console.log('[Brain] One buy signal → BUY');
      decision = 'buy';
      winningTestIndex = buySignals[0].testIndex;
      winningTest = tests[winningTestIndex];
      winningHistorical = historicalResults[winningTestIndex];
      executionLogs.push(`Winner: Test ${winningTestIndex} (${winningTest.indicatorName})`);
    } else {
      // Multiple tests say BUY → Use conflict resolution
      // === USE UNIFIED CONFLICT RESOLUTION (conflict_bridge.ts) ===
      // This ensures identical logic with Python conflict_resolver.py
      executionLogs.push(``);
      executionLogs.push(`DECISION: BUY`);
      executionLogs.push(`Reason: ${buySignals.length} tests are signaling BUY`);
      
      const conflictMode = (strategy.conflictResolution?.mode || 'sharpeRatio') as ConflictMode;
      executionLogs.push(`Applying conflict resolution: ${conflictMode}`);
      console.log(`[Brain] ${buySignals.length} buy signals → Conflict resolution (${conflictMode})`);
      decision = 'buy';
      
      // Build conflict signals array from buy signals
      // IMPORTANT: Use historical results from DB, but FALLBACK to DNA strand's stored performance
      // DNA strands store 'profitability', conflict resolver expects 'totalReturn' for 'profitability' mode
      const conflictSignals: ConflictSignal[] = buySignals.map((signal: any) => {
        const testIndex = signal.testIndex;
        const historical = historicalResults[testIndex];
        const test = tests[testIndex];
        const testPerf = test?.performance;
        
        // For totalReturn: prefer historicalResults, fallback to DNA's profitability
        const totalReturn = historical?.totalReturn ?? testPerf?.profitability ?? 0;
        
        return {
          index: testIndex,
          indicatorName: test?.indicatorName || 'unknown',
          sharpe: historical?.sharpeRatio ?? testPerf?.sharpeRatio ?? 0,
          totalReturn,
          winRate: historical?.winRate ?? testPerf?.winRate ?? 0,
          maxDrawdown: historical?.maxDrawdown ?? testPerf?.maxDrawdown ?? 0,
          leverage: test?.leverage,
          stopLoss: test?.stopLoss,
          epic: test?.epic,
          timeframe: test?.timeframe,
        };
      });
      
      // Resolve conflict using unified bridge (same logic as Python)
      const winnerLocalIndex = resolveConflictSync(conflictSignals, conflictMode);
      winningTestIndex = conflictSignals[winnerLocalIndex]?.index || buySignals[0].testIndex;
      
      winningTest = tests[winningTestIndex];
      winningHistorical = historicalResults[winningTestIndex];
      executionLogs.push(`Winner: Test ${winningTestIndex} (${winningTest.indicatorName})`);
      executionLogs.push(`  Historical Sharpe: ${winningHistorical?.sharpeRatio?.toFixed(2) || 'N/A'}`);
      executionLogs.push(`  Historical Return: ${winningHistorical?.totalReturn?.toFixed(2) || 'N/A'}%`);
      executionLogs.push(`  Historical Win Rate: ${winningHistorical?.winRate?.toFixed(1) || 'N/A'}%`);
    }
    
    // === MARKET HOURS CHECK ===
    // Override BUY → HOLD if the winning epic's market is closed
    // This prevents attempting trades that will be rejected by Capital.com
    let marketClosedOverride = false;
    let marketClosedReason = '';
    
    if (decision === 'buy') {
      const winningEpic = String(winningTest.epic || primaryEpic);
      const marketInfo = marketStatusMap[winningEpic];
      
      if (marketInfo?.status === 'CLOSED') {
        marketClosedOverride = true;
        const nextOpenStr = marketInfo.nextOpen 
          ? `Opens at ${marketInfo.nextOpen.toISOString()}`
          : 'Next open time unknown';
        marketClosedReason = `${winningEpic} market is CLOSED. ${nextOpenStr}`;
        
        console.log(`[Brain] ⚠️ MARKET CLOSED OVERRIDE: ${winningEpic} is closed, changing BUY → HOLD`);
        executionLogs.push(``);
        executionLogs.push(`=== MARKET HOURS CHECK ===`);
        executionLogs.push(`⚠️ ${marketClosedReason}`);
        executionLogs.push(`Overriding decision: BUY → HOLD`);
        
        decision = 'hold';
      }
    }
    
    executionLogs.push(``);
    executionLogs.push(`=== FINAL RESULT ===`);
    executionLogs.push(`Decision: ${decision.toUpperCase()}${marketClosedOverride ? ' (market closed override)' : ''}`);
    
    // Only show winner details if decision is BUY
    if (decision === 'buy') {
      executionLogs.push(`Winner: Test ${winningTestIndex} - ${winningTest.indicatorName}`);
      executionLogs.push(`Epic: ${winningTest.epic || primaryEpic}`);
      executionLogs.push(`Timeframe: ${winningTest.timeframe || '5m'}`);
      executionLogs.push(`Leverage: ${winningTest.leverage || 'N/A'}x`);
      executionLogs.push(`Stop Loss: ${winningTest.stopLoss || 'N/A'}%`);
    } else {
      executionLogs.push(`No winner - all tests are signaling HOLD`);
    }
    
    console.log(`[Brain] Winner: Test ${winningTestIndex} (${winningTest.indicatorName}) - Historical Sharpe: ${winningHistorical?.sharpeRatio || 0}`);
    console.log(`[Brain] Final decision: ${decision.toUpperCase()}`);
    
    // Log conflict resolution and final decision
    const conflictModeForLog = strategy.conflictResolution?.mode || 'single_best_sharpe';
    fileLogger?.conflictResolution(conflictModeForLog, decision === 'buy' ? {
      testIndex: winningTestIndex,
      indicatorName: winningTest.indicatorName,
      sharpe: winningHistorical?.sharpeRatio,
    } : undefined);
    
    fileLogger?.finalDecision(
      decision === 'buy' ? 'BUY' : 'HOLD',
      decision === 'buy' ? (winningTest?.epic || primaryEpic) : undefined,
      decision === 'buy' ? winningTest?.leverage : undefined,
      decision === 'buy' ? winningTest?.stopLoss : undefined
    );
    
    // Get last candle timestamps
    const lastCandle = lastCandleTimestamps[String(primaryEpic)] || 'N/A';
    const lastCapitalCandle = totalGapCandles > 0 ? epicDataMap[String(primaryEpic)]?.[epicDataMap[String(primaryEpic)].length - 1]?.timestamp : 'N/A';
    
    // Calculate execution time
    const executionTimeMs = Date.now() - executionStartTime;
    
    // Format winning reason based on conflict mode and historical data
    const conflictMode = strategy.conflictResolution?.mode || 'single_best_sharpe';
    let winningReason = '';
    // Get performance from historical results with fallback to DNA strand's stored performance
    const winningTestPerf = winningTest?.performance;
    const winningMetrics = {
      sharpe: winningHistorical?.sharpeRatio ?? winningTestPerf?.sharpeRatio ?? 0,
      totalReturn: winningHistorical?.totalReturn ?? winningTestPerf?.profitability ?? 0,
      winRate: winningHistorical?.winRate ?? winningTestPerf?.winRate ?? 0,
    };
    
    if (conflictMode === 'single_best_sharpe' || conflictMode === 'sharpeRatio' || conflictMode === 'sharpe') {
      winningReason = `Best Sharpe Ratio (${winningMetrics.sharpe.toFixed(2)})`;
    } else if (conflictMode === 'single_best_return' || conflictMode === 'profitability' || conflictMode === 'totalReturn') {
      winningReason = `Best Return (+${winningMetrics.totalReturn.toFixed(2)}%)`;
    } else if (conflictMode === 'winRate') {
      winningReason = `Best Win Rate (${winningMetrics.winRate.toFixed(1)}%)`;
    } else {
      winningReason = conflictMode;
    }
    
    // Build comprehensive DNA results for validation
    // Use same fallback logic as conflict resolution: historicalResults → DNA performance
    const buySignalCount = currentSignals.filter((s: any) => s?.signal === 1).length;
    const allDnaResults = {
      dnaResults: tests.map((test: any, i: number) => {
        const historical = historicalResults[i];
        const testPerf = test?.performance;
        const signal = currentSignals[i];
        
        // Fallback chain: historical results from DB → DNA strand's stored performance
        return {
          testIndex: i,
          indicatorName: test.indicatorName,
          indicatorParams: test.indicatorParams || test.params || {},
          epic: test.epic || primaryEpic,
          timeframe: test.timeframe || '5m',
          signal: (signal?.signal === 1 ? 'BUY' : 'HOLD') as 'BUY' | 'HOLD',
          indicatorValue: signal?.indicatorValue || null,
          sharpeRatio: historical?.sharpeRatio ?? testPerf?.sharpeRatio ?? null,
          // For totalReturn: fallback to profitability from DNA strand (same metric, different name)
          totalReturn: historical?.totalReturn ?? testPerf?.profitability ?? null,
          winRate: historical?.winRate ?? testPerf?.winRate ?? null,
          maxDrawdown: historical?.maxDrawdown ?? testPerf?.maxDrawdown ?? null,
        };
      }),
      conflictResolution: {
        metric: conflictMode,
        winnerIndex: winningTestIndex,
        reason: winningReason,
        hadConflict: buySignalCount > 1,
      },
    };
    
    // Get candle date range from the first epic's data
    const firstEpicData = epicDataMap[String(primaryEpic)] || [];
    const candleStartDate = firstEpicData.length > 0 ? firstEpicData[0].timestamp : undefined;
    const candleEndDate = firstEpicData.length > 0 ? firstEpicData[firstEpicData.length - 1].timestamp : undefined;
    const candleCount = firstEpicData.length;
    
    const returnValue = {
      decision,
      winningIndicator: winningTest?.indicatorName || tests[0]?.indicatorName || 'unknown',
      winningTestId: winningTestIndex,
      winningEpic: String(winningTest?.epic || primaryEpic),
      winningTimeframe: winningTest?.timeframe || '5m',
      winningReason,
      conflictMode,
      confidence: winningHistorical?.winRate || 0,
      leverage: winningTest?.leverage || tests[0]?.leverage || null,
      stopLoss: winningTest?.stopLoss || tests[0]?.stopLoss || null,
      stopLossPercent: winningTest?.stopLoss || tests[0]?.stopLoss || null, // Same as stopLoss - it's already the %
      guaranteedStopEnabled: winningTest?.guaranteedStopEnabled || tests[0]?.guaranteedStopEnabled || false,
      // HMH (Hold Means Hold) - set new SL on HOLD instead of closing
      hmhEnabled: winningTest?.hmhEnabled || tests[0]?.hmhEnabled || false,
      hmhStopLossOffset: winningTest?.hmhStopLossOffset ?? tests[0]?.hmhStopLossOffset ?? null,
      timingConfig: winningTest?.timingConfig || tests[0]?.timingConfig || { mode: 'Fake5min_4thCandle' },
      winningDataSource: (winningTest as any)?.dataSource || (tests[0] as any)?.dataSource || 'capital',
      indicatorValue: currentSignals[winningTestIndex]?.indicatorValue || undefined,
      // CRITICAL: Store indicator params for validation - without these we can't reproduce the signal
      indicatorParams: winningTest?.indicatorParams || winningTest?.params || {},
      // Candle range for validation (so we can reproduce the exact calculation)
      candleStartDate,
      candleEndDate,
      candleCount,
      // All DNA results for comprehensive validation
      allDnaResults,
      executionTimeMs,
      executionLogs,
      allTests: tests.map((test: any, i: number) => {
        const historical = historicalResults[i];
        if (historical) {
          // Use historical backtest data
          return {
            testId: i,
            indicatorName: test.indicatorName,
            epic: test.epic || primaryEpic,
            timeframe: test.timeframe || '5m',
            passed: currentSignals[i]?.signal === 1, // PASS = currently signals BUY
            value: historical.finalBalance || 0,
            threshold: historical.initialBalance || 500,
            sharpe: historical.sharpeRatio || 0,
            return: historical.totalReturn || 0,
            leverage: historical.leverage || 0,
            stopLoss: historical.stopLoss || 0,
            winRate: historical.winRate || 0,
            maxDrawdown: historical.maxDrawdown || 0,
            maxProfit: historical.finalBalance - historical.initialBalance || 0,
          };
        } else {
          // No historical data - show zeros
          console.warn(`[Brain] No historical data for test ${i}: ${test.indicatorName}`);
          return {
            testId: i,
            indicatorName: test.indicatorName,
            epic: test.epic || primaryEpic,
            timeframe: test.timeframe || '5m',
            passed: false,
            value: 0,
            threshold: 500,
            sharpe: 0,
            return: 0,
            leverage: 0,
            stopLoss: 0,
            winRate: 0,
            maxDrawdown: 0,
            maxProfit: 0,
          };
        }
      }),
      marketData: {
        epic: String(winningTest?.epic || primaryEpic),
        bid,
        ask,
        mid,
        timestamp: new Date(),
      },
      dataSource: 'capital.com' as 'capital.com' | 'websocket',
      candlesLoaded: totalGapCandles,
      lastCapitalCandle: lastCapitalTimestamp || lastCandleTimestamps[String(primaryEpic)] || undefined,
      usedT60ApiPrice: !!epicPrices && Object.keys(epicPrices).length > 0,
      analyzedAt: new Date(),
      completedIn: (executionTimeMs / 1000).toFixed(2) + ' seconds',
      
      // Brain calculation price tracking (for validation accuracy)
      // brainCalcPrice = T-60 API price used for indicator calculation
      brainCalcPrice: epicPrices && winningTest?.epic ? epicPrices[String(winningTest.epic)] : undefined,
      // NOTE: fake5minClose and priceVariancePct are calculated below
    };
    
    // COMPREHENSIVE DEBUG LOGGING: Calculate variance immediately for logging
    // This fetches the 4th 1m candle to compare with T-60 API price
    if (returnValue.brainCalcPrice && winningTest?.epic) {
      try {
        const { get4th1mCandleClose, calculateVariance } = await import('../services/candle_variance_service');
        
        // Determine window close time from context
        const now = new Date();
        const windowCloseTime = returnValue.windowCloseTime || '21:00:00';
        
        // Get the 4th 1m candle
        const winningEpic = String(winningTest.epic);
        const timingMode = (winningTest as any).timingConfig?.mode || 'Fake5min_4thCandle';
        const candleResult = await get4th1mCandleClose(winningEpic, now, windowCloseTime, timingMode);
        
        if (candleResult.fake5minClose) {
          const variance = calculateVariance(returnValue.brainCalcPrice, candleResult.fake5minClose);
          
          // Populate the return value with calculated variance
          returnValue.fake5minClose = candleResult.fake5minClose;
          returnValue.priceVariancePct = variance.priceVariancePct || 0;
          returnValue.last1mCandleTime = candleResult.last1mCandleTime?.toISOString();
          
          // DETAILED DEBUG LOG
          const variancePct = variance.priceVariancePct || 0;
          const absVariance = variance.absoluteVariance || 0;
          
          console.log('\n[Brain] ========================================================');
          console.log('[Brain]           CANDLE PRICE COMPARISON DEBUG LOG            ');
          console.log('[Brain] ========================================================');
          console.log(`[Brain]   Epic: ${winningEpic} | Timing Mode: ${timingMode}`);
          console.log('[Brain] --------------------------------------------------------');
          console.log(`[Brain]   T-60 API Price (brain uses):     $${returnValue.brainCalcPrice.toFixed(4)}`);
          console.log(`[Brain]   4th 1m Candle (backtest uses):   $${candleResult.fake5minClose.toFixed(4)}`);
          console.log(`[Brain]     Candle timestamp: ${candleResult.last1mCandleTime?.toISOString() || 'N/A'}`);
          console.log('[Brain] --------------------------------------------------------');
          const varianceEmoji = Math.abs(variancePct) < 0.1 ? '[MATCH]' : '[DIFF]';
          console.log(`[Brain]   Variance: ${variancePct.toFixed(4)}% (${(variancePct * 100).toFixed(2)} bps) ${varianceEmoji}`);
          console.log(`[Brain]   Absolute Diff: $${absVariance.toFixed(6)}`);
          console.log('[Brain] --------------------------------------------------------');
          console.log(`[Brain]   Minutes of 1m data used: ${candleResult.minutesUsed}`);
          console.log('[Brain] ========================================================\n');
        } else {
          console.log(`[Brain] WARNING: Could not fetch 4th 1m candle for ${winningEpic} - variance not calculated`);
          console.log(`[Brain] T-60 API Price (brainCalcPrice): $${returnValue.brainCalcPrice.toFixed(4)}`);
        }
      } catch (varianceError: any) {
        console.warn(`[Brain] Variance calculation failed: ${varianceError.message}`);
        console.log(`[Brain] T-60 API Price (brainCalcPrice): $${returnValue.brainCalcPrice?.toFixed(4) || 'N/A'}`);
      }
    }
    
    console.log('[Brain] === FINAL RETURN VALUES ===');
    console.log('primaryEpic:', primaryEpic);
    console.log('lastCandleTimestamps:', JSON.stringify(lastCandleTimestamps));
    console.log('lastCapitalCandle being returned:', lastCapitalTimestamp || lastCandleTimestamps[String(primaryEpic)]);
    console.log('candlesLoaded:', totalGapCandles);
    console.log('usedT60ApiPrice:', !!epicPrices && Object.keys(epicPrices).length > 0);
    console.log('===========================');
    
    // Close file logger with timing
    fileLogger?.timing('Total execution time');
    fileLogger?.close();
    
    return returnValue;
  } catch (error: any) {
    console.error('[Brain] Error:', error);
    // Log error to file and close
    fileLogger?.error('Brain preview failed', error);
    fileLogger?.close();
    throw error;
  }
}

/**
 * Brain Simulate - Same as preview but with full API calls
 * 
 * The difference between preview and simulate:
 * - Preview: Calculation only, no side effects
 * - Simulate: Full sequence including API calls, market data fetching, etc.
 * 
 * For now, they're the same implementation. In the future, simulate could
 * include additional steps like validating account balance, checking positions, etc.
 */
export async function brainSimulate(
  strategyId: number,
  accountId: number,
  environment: 'demo' | 'live'
): Promise<BrainResult> {
  console.log('[Brain] Simulate is same as preview for now');
  const result = await brainPreview(strategyId, accountId, environment);
  return {
    ...result,
    dataSource: 'simulate',
  };
}

