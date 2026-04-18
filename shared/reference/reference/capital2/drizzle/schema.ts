import { int, mysqlEnum, mysqlTable, text, timestamp, varchar, float, boolean, json, date, decimal, index, uniqueIndex } from "drizzle-orm/mysql-core";

/**
 * Core user table backing auth flow.
 */
export const users = mysqlTable("users", {
  id: int("id").autoincrement().primaryKey(),
  openId: varchar("openId", { length: 64 }).notNull().unique(),
  name: text("name"),
  email: varchar("email", { length: 320 }),
  loginMethod: varchar("loginMethod", { length: 64 }),
  role: mysqlEnum("role", ["user", "admin"]).default("user").notNull(),
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
  lastSignedIn: timestamp("lastSignedIn").defaultNow().notNull(),
});

export type User = typeof users.$inferSelect;
export type InsertUser = typeof users.$inferInsert;

/**
 * Epic data tracking - manages available market data
 */
export const epics = mysqlTable("epics", {
  id: int("id").autoincrement().primaryKey(),
  symbol: varchar("symbol", { length: 20 }).notNull().unique(), // e.g., SOXL, TECL
  name: varchar("name", { length: 255 }), // Full name
  
  // Data coverage
  desiredStartDate: varchar("desiredStartDate", { length: 10 }), // User-specified start date for backfilling (YYYY-MM-DD)
  startDate: varchar("startDate", { length: 10 }), // First available candle date (YYYY-MM-DD)
  endDate: varchar("endDate", { length: 10 }), // Last available candle date (YYYY-MM-DD)
  candleCount: int("candleCount").default(0).notNull(),
  lastCandleTime: timestamp("lastCandleTime"), // Exact timestamp of last candle
  
  // Data quality
  gapCount: int("gapCount").default(0).notNull(), // Number of gaps detected
  coverage: float("coverage").default(0).notNull(), // Percentage coverage (0-100)
  
  // Market hours (from Capital.com API)
  marketStatus: mysqlEnum("marketStatus", ["TRADEABLE", "CLOSED", "UNKNOWN"]).default("UNKNOWN").notNull(),
  tradingHoursType: mysqlEnum("tradingHoursType", ["regular", "extended"]).default("regular").notNull(), // Regular = 16:00 ET close, Extended = 20:00 ET close
  openingHours: json("openingHours").$type<{
    mon: string[];
    tue: string[];
    wed: string[];
    thu: string[];
    fri: string[];
    sat: string[];
    sun: string[];
    zone: string;
  } | null>(),
  nextOpen: timestamp("nextOpen"), // Next market open time (UTC)
  nextClose: timestamp("nextClose"), // Next market close time (UTC)
  marketHoursLastFetched: timestamp("marketHoursLastFetched"), // Last time market hours were fetched
  
  // Metadata
  dataSource: varchar("dataSource", { length: 50 }).default("alphavantage").notNull(),
  lastUpdated: timestamp("lastUpdated").defaultNow().onUpdateNow().notNull(),
  createdAt: timestamp("createdAt").defaultNow().notNull(),
});

export type Epic = typeof epics.$inferSelect;
export type InsertEpic = typeof epics.$inferInsert;

/**
 * Strategy templates - reusable indicator-based strategy configurations
 * (Renamed from 'bots' to avoid confusion with active_bots)
 */
export const strategyTemplates = mysqlTable("strategy_templates", {
  id: int("id").autoincrement().primaryKey(),
  userId: int("userId").notNull(),
  name: varchar("name", { length: 255 }).notNull(),
  description: text("description"),
  
  // Strategy configuration
  indicators: json("indicators").$type<string[]>().notNull(), // List of indicator names
  direction: mysqlEnum("direction", ["long", "short", "both"]).notNull(),
  
  // High-level settings (can be overridden per deployment)
  defaultLeverage: float("defaultLeverage").notNull(),
  defaultStopLoss: float("defaultStopLoss").notNull(),
  
  // Indicator parameters (JSON object)
  indicatorParams: json("indicatorParams").$type<Record<string, any>>().notNull(),
  
  // Timing configuration
  entrySeconds: int("entrySeconds").default(15).notNull(), // Seconds before close to enter
  exitSeconds: int("exitSeconds").default(30).notNull(), // Seconds before close to exit
  
  // Crash protection
  crashProtectionEnabled: boolean("crashProtectionEnabled").default(false).notNull(),
  crashProtectionParams: json("crashProtectionParams").$type<{
    timeframeDays: number;
    ddThreshold: number;
    rsiBottom: number;
    volSpike: number;
    recoveryThreshold: number;
  } | null>(),
  
  // Metadata
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
});

export type StrategyTemplate = typeof strategyTemplates.$inferSelect;
export type InsertStrategyTemplate = typeof strategyTemplates.$inferInsert;

/**
 * Capital.com accounts
 */
export const accounts = mysqlTable("accounts", {
  id: int("id").autoincrement().primaryKey(),
  userId: int("userId").notNull(),
  
  // Capital.com account details
  accountId: varchar("accountId", { length: 100 }).notNull().unique(),
  accountType: mysqlEnum("accountType", ["demo", "live"]).notNull(),
  accountName: varchar("accountName", { length: 255 }).notNull(),
  currency: varchar("currency", { length: 10 }).default("USD").notNull(),
  
  // Account state (synced from Capital.com)
  balance: float("balance").default(0).notNull(),
  equity: float("equity").default(0).notNull(),
  margin: float("margin").default(0).notNull(),
  available: float("available").default(0).notNull(),
  profitLoss: float("profitLoss").default(0).notNull(),
  
  // Bot assignment
  assignedStrategyId: int("assignedStrategyId"), // NULL if no bot assigned (references strategyCombos.id)
  botStatus: mysqlEnum("botStatus", ["stopped", "running", "paused", "error"]).default("stopped"),
  errorMessage: text("errorMessage"), // Error details when botStatus is 'error'
  allocationMode: mysqlEnum("allocationMode", ["per_timeslice", "pass_through"]).default("pass_through"),
  
  // Multi-window configuration (detected when strategy is assigned)
  windowConfig: json("windowConfig").$type<{
    windows: Array<{
      closeTime: string; // HH:MM:SS format (e.g., "16:00:00")
      marketName: string; // e.g., "Regular Hours", "Extended Hours"
      epics: string[]; // Epics that close at this time
      allocationPct: number; // Percentage of funds allocated to this window (0-100)
    }>;
    mode: 'carry_over' | 'manual_split'; // carry_over = HOLD passes funds to next window
  }>(),
  
  // Bot runtime state
  startedAt: timestamp("startedAt"),
  stoppedAt: timestamp("stoppedAt"),
  pausedAt: timestamp("pausedAt"),
  lastHeartbeat: timestamp("lastHeartbeat"),
  
  // Bot configuration overrides
  epic: varchar("epic", { length: 50 }),
  leverage: float("leverage"),
  stopLoss: float("stopLoss"),
  investmentPct: float("investmentPct").default(99),
  
  // Data window settings for brain preview
  dataWindowMode: mysqlEnum("dataWindowMode", ["dynamic", "fixed"]).default("dynamic").notNull(),
  dataWindowDays: int("dataWindowDays").default(365), // Only used when mode is 'fixed'
  
  // ═══════════════════════════════════════════════════════════════════════════
  // TRADING SESSION STATE
  // ═══════════════════════════════════════════════════════════════════════════
  // Tracks the current trading session state for this account
  // Designed to be extensible for different strategy types:
  // - "time_based": Windows with close times (current system)
  // - "indicator_group": Groups of indicators with their own closing logic (future)
  // - "discovery": Discovery-based strategies (future)
  // ═══════════════════════════════════════════════════════════════════════════
  tradingSessionState: json("tradingSessionState").$type<{
    // Strategy type determines how windows/groups are structured
    strategyType: 'time_based' | 'indicator_group' | 'discovery' | 'custom';
    
    // Session identifier (YYYY-MM-DD format, based on trading day start at 02:00 UTC)
    sessionId: string;
    
    // Last update timestamp (ISO string)
    lastUpdated: string;
    
    // Total allocation settings for this session
    totalAccountBalancePct: number; // e.g., 99 = use 99% of balance
    originalDailyBalance: number;   // Balance at session start (for % calculations)
    
    // ─────────────────────────────────────────────────────────────────────────
    // TIME-BASED STRATEGY: Windows with close times
    // ─────────────────────────────────────────────────────────────────────────
    windows?: {
      [windowCloseTime: string]: { // Key is "HH:MM:SS" format, e.g., "21:00:00"
        // Configuration (snapshot from strategy at session start)
        allocationPct: number;        // % of totalAccountBalancePct for this window
        carryOverEnabled: boolean;    // If HOLD, carry allocation to next window?
        tradingHoursType: 'regular' | 'extended' | 'crypto' | 'custom';
        epics: string[];              // Epics that can trade in this window
        
        // Runtime state (updated as trading happens)
        status: 'pending' | 'hold' | 'traded' | 'skipped' | 'failed';
        decision: 'BUY' | 'HOLD' | null;
        winningEpic: string | null;
        winningIndicator: string | null;
        indicatorValue: number | null;
        
        // Trade execution details
        tradeId: number | null;       // actual_trades.id
        dealReference: string | null; // Capital.com order ref (o_xxx)
        dealId: string | null;        // Capital.com position id (p_xxx)
        marginUsed: number;           // Margin consumed by this window's trade
        contracts: number;            // Position size
        entryPrice: number | null;    // Entry price
        
        // Timestamps
        decisionTime: string | null;  // When brain made decision (ISO)
        executionTime: string | null; // When trade was fired (ISO)
      };
    };
    
    // ─────────────────────────────────────────────────────────────────────────
    // INDICATOR-GROUP STRATEGY (Future extensibility)
    // Groups of indicators that compete, winner determines trade
    // ─────────────────────────────────────────────────────────────────────────
    indicatorGroups?: {
      [groupId: string]: {
        indicators: string[];
        allocationPct: number;
        status: 'pending' | 'hold' | 'traded' | 'skipped' | 'failed';
        winnerIndicator: string | null;
        winningEpic: string | null;
        marginUsed: number;
        tradeId: number | null;
      };
    };
    
    // ─────────────────────────────────────────────────────────────────────────
    // AGGREGATED METRICS (calculated from windows/groups)
    // ─────────────────────────────────────────────────────────────────────────
    usedPct: number;        // Total % of allocation used across all windows/groups
    carriedOverPct: number; // % carried from HOLD windows to later windows
    windowsTraded: string[]; // List of window close times or group IDs that traded
    windowsHold: string[];   // List that had HOLD decisions
  } | null>(),
  
  // Status
  isActive: boolean("isActive").default(true).notNull(),
  isArchived: boolean("isArchived").default(false).notNull(),
  lastSync: timestamp("lastSync"),
  
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
});

export type Account = typeof accounts.$inferSelect;
export type InsertAccount = typeof accounts.$inferInsert;

/**
 * Bot performance snapshots - track bot performance over time
 */
export const botPerformance = mysqlTable("botPerformance", {
  id: int("id").autoincrement().primaryKey(),
  accountId: int("accountId").notNull(),
  strategyId: int("strategyId").notNull(), // Reference to strategyCombos
  
  // Performance snapshot
  timestamp: timestamp("timestamp").defaultNow().notNull(),
  balance: float("balance").notNull(),
  equity: float("equity").notNull(),
  margin: float("margin").notNull(),
  available: float("available").notNull(),
  openPositions: int("openPositions").default(0).notNull(),
  
  // Cumulative statistics
  totalTrades: int("totalTrades").default(0).notNull(),
  winningTrades: int("winningTrades").default(0).notNull(),
  losingTrades: int("losingTrades").default(0).notNull(),
  winRate: float("winRate").default(0).notNull(),
  totalPnl: float("totalPnl").default(0).notNull(),
});

export type BotPerformance = typeof botPerformance.$inferSelect;
export type InsertBotPerformance = typeof botPerformance.$inferInsert;

/**
 * Backtest runs
 */
export const backtestRuns = mysqlTable("backtestRuns", {
  id: int("id").autoincrement().primaryKey(),
  userId: int("userId").notNull(),
  botId: int("botId"), // NULL if ad-hoc backtest
  
  // Configuration
  epic: varchar("epic", { length: 50 }).notNull(),
  startDate: varchar("startDate", { length: 10 }).notNull(),
  endDate: varchar("endDate", { length: 10 }).notNull(),
  
  // Financial settings
  initialBalance: float("initialBalance").notNull(),
  monthlyTopup: float("monthlyTopup").notNull(),
  investmentPct: float("investmentPct").notNull(),
  
  // Test parameters
  indicators: json("indicators").$type<string[]>().notNull(),
  numSamples: int("numSamples").notNull(),
  direction: mysqlEnum("direction", ["long", "short", "both"]).default("both").notNull(),
  
  // Advanced configuration (for resume)
  timingConfig: json("timingConfig").$type<{
    mode: 
      // Signal-based modes
      | 'SignalBased'
      // Auto-detect market close
      | 'MarketClose' | 'T5BeforeClose' | 'T60FakeCandle'
      // Fixed time modes  
      | 'USMarketClose' | 'ExtendedHoursClose'
      // Fake candle modes (T-60 brain calc)
      | 'Fake5min_3rdCandle_API' | 'Fake5min_4thCandle' | 'T60FakeCandle'
      // Random modes
      | 'random_morning' | 'random_afternoon'
      // Legacy modes
      | 'EpicClosingTimeBrainCalc' | 'EpicClosingTime' | 'USMarketClosingTime' 
      | 'ManusTime' | 'OriginalBotTime' | 'Random' | 'SpecificHour'
      // Custom
      | 'Custom'
      // Second last candle
      | 'SecondLastCandle';
    market_close?: string;
    calc_offset_seconds?: number;
    close_offset_seconds?: number;
    open_offset_seconds?: number;
    entry_range_start?: string;
    entry_range_end?: string;
    exit_range_start?: string;
    exit_range_end?: string;
    entry_time_specific?: string;
    exit_time_specific?: string;
  } | null>(),
  timeframeConfig: json("timeframeConfig").$type<{
    mode: 'default' | 'random' | 'multiple';
    selectedTimeframes: string[];
  } | null>(),
  stopConditions: json("stopConditions").$type<{
    maxDrawdown?: number;
    minWinRate?: number;
    minSharpe?: number;
    minProfitability?: number;
  } | null>(),
  crashProtectionMode: mysqlEnum("crashProtectionMode", ["without", "with", "both"]).default("without"),
  // HMH (Hold Means Hold) - set new SL on HOLD instead of closing
  hmhConfig: json("hmhConfig").$type<{
    enabled: boolean;
    offsets: number[];  // e.g., [0, -1, -2] for original, -1%, -2%
  } | null>(),
  optimizationStrategy: varchar("optimizationStrategy", { length: 50 }).default("random"),
  freeMemory: boolean("freeMemory").default(false),
  testOrder: mysqlEnum("testOrder", ["sequential", "random"]).default("sequential"),
  parallelCores: int("parallelCores").default(1).notNull(),
  
  // Status
  status: mysqlEnum("status", ["pending", "running", "paused", "completed", "failed", "stopped"]).default("pending").notNull(),
  progress: int("progress").default(0).notNull(), // 0-100
  
  // Pause/Resume support
  pausedAt: timestamp("pausedAt"),
  resumedAt: timestamp("resumedAt"),
  lastProcessedIndex: int("lastProcessedIndex").default(0), // Track progress for resume
  checkpointData: json("checkpointData").$type<{
    completedIndicators?: string[];
    currentIndicator?: string;
    completedTests?: number;
  } | null>(),
  
  // Results summary
  totalTests: int("totalTests"),
  completedTests: int("completedTests"),
  duplicateCount: int("duplicateCount").default(0), // Count of skipped duplicate tests
  bestResult: json("bestResult").$type<{
    indicatorName: string;
    finalBalance: number;
    totalReturn: number;
    winRate: number;
    sharpeRatio: number;
  } | null>(),
  
  // Process monitoring (for parallel execution)
  processStatus: json("processStatus").$type<Array<{
    pid: number;
    test_num: number;
    indicator: string;
    status: 'running' | 'completed';
    duration: number;
  }> | null>(),
  
  // Timing
  startedAt: timestamp("startedAt"),
  completedAt: timestamp("completedAt"),
  createdAt: timestamp("createdAt").defaultNow().notNull(),
});

export type BacktestRun = typeof backtestRuns.$inferSelect;
export type InsertBacktestRun = typeof backtestRuns.$inferInsert;

/**
 * Backtest Queue - persistent test queue for crash recovery and progress tracking
 * Each row represents a single backtest configuration to be run.
 * Tests are claimed by workers, run, and marked complete.
 * This enables:
 * - Crash recovery: Resume from where we left off
 * - Progress tracking: Know exactly which tests are done
 * - Race condition prevention: Workers atomically claim tests
 * - Memory management: Workers can restart fresh between batches
 */
export const backtestQueue = mysqlTable("backtestQueue", {
  id: int("id").autoincrement().primaryKey(),
  runId: int("runId").notNull(), // References backtestRuns.id
  
  // Test configuration (what to run)
  epic: varchar("epic", { length: 20 }).notNull(),
  indicatorName: varchar("indicatorName", { length: 100 }).notNull(),
  indicatorParams: json("indicatorParams").$type<Record<string, any>>().notNull(),
  leverage: float("leverage").notNull(),
  stopLoss: float("stopLoss").notNull(),
  timeframe: varchar("timeframe", { length: 10 }).notNull(),
  crashProtection: boolean("crashProtection").default(false).notNull(),
  
  // Hash for deduplication (unique constraint prevents race conditions)
  paramHash: varchar("paramHash", { length: 64 }).notNull().unique(),
  
  // Processing status
  status: mysqlEnum("status", ["pending", "processing", "completed", "failed"]).default("pending").notNull(),
  workerId: varchar("workerId", { length: 50 }), // Process ID that claimed this test
  claimedAt: timestamp("claimedAt"), // When worker claimed this test
  completedAt: timestamp("completedAt"), // When test finished
  
  // Result linking
  resultId: int("resultId"), // Links to backtestResults.id when complete
  errorMessage: text("errorMessage"), // Error details if failed
  
  // Timestamps
  createdAt: timestamp("createdAt").defaultNow().notNull(),
}, (table) => ({
  // Index for efficient queries by run and status
  runStatusIdx: index("idx_queue_run_status").on(table.runId, table.status),
  // Index for finding stale claims (for crash recovery)
  statusClaimedIdx: index("idx_queue_status_claimed").on(table.status, table.claimedAt),
}));

export type BacktestQueueItem = typeof backtestQueue.$inferSelect;
export type InsertBacktestQueueItem = typeof backtestQueue.$inferInsert;

/**
 * Core allocations - tracks which CPU cores are assigned to which tests
 */
export const coreAllocations = mysqlTable("coreAllocations", {
  id: int("id").autoincrement().primaryKey(),
  runId: int("runId").notNull(),
  coreId: int("coreId").notNull(),
  testNum: int("testNum").notNull(),
  indicator: varchar("indicator", { length: 100 }).notNull(),
  pid: int("pid"),
  status: mysqlEnum("status", ["allocated", "running", "completed", "failed"]).default("allocated").notNull(),
  startTime: timestamp("startTime").defaultNow().notNull(),
  endTime: timestamp("endTime"),
  errorMessage: text("errorMessage"),
  createdAt: timestamp("createdAt").defaultNow().notNull(),
});

export type CoreAllocation = typeof coreAllocations.$inferSelect;
export type InsertCoreAllocation = typeof coreAllocations.$inferInsert;

/**
 * Individual backtest results
 */
// Market Information Table - stores trading parameters from Capital.com
export const marketInfo = mysqlTable('marketInfo', {
  epic: varchar('epic', { length: 50 }).primaryKey(),
  name: varchar('name', { length: 255 }),
  instrumentType: varchar('instrumentType', { length: 50 }), // SHARES, CURRENCIES, INDICES, etc.
  
  // Spread and pricing
  spreadPercent: decimal('spreadPercent', { precision: 10, scale: 6 }).notNull(),
  
  // Contract sizes
  minContractSize: decimal('minContractSize', { precision: 10, scale: 2 }).notNull(),
  maxContractSize: decimal('maxContractSize', { precision: 15, scale: 2 }).notNull(),
  minSizeIncrement: decimal('minSizeIncrement', { precision: 10, scale: 4 }).notNull().default('1.0000'), // Minimum increment for position size
  lotSize: decimal('lotSize', { precision: 10, scale: 4 }).notNull().default('1.0000'),
  contractMultiplier: decimal('contractMultiplier', { precision: 10, scale: 2 }).notNull().default('1.00'),
  
  // Leverage options (JSON array of valid leverage values)
  // e.g., [1, 2, 3, 4, 5, 10, 20] for SHARES
  leverageOptions: json('leverageOptions').$type<number[]>(),
  marginFactor: decimal('marginFactor', { precision: 10, scale: 4 }), // Margin requirement percentage
  
  // Overnight funding rates
  overnightFundingLongPercent: decimal('overnightFundingLongPercent', { precision: 15, scale: 10 }).notNull(),
  overnightFundingShortPercent: decimal('overnightFundingShortPercent', { precision: 15, scale: 10 }).notNull(),
  
  // Market hours
  marketOpenTime: varchar('marketOpenTime', { length: 8 }).notNull(),
  marketCloseTime: varchar('marketCloseTime', { length: 8 }).notNull(),
  
  // Stop/profit rules
  minStopDistancePct: decimal('minStopDistancePct', { precision: 10, scale: 4 }), // Minimum stop loss distance %
  maxStopDistancePct: decimal('maxStopDistancePct', { precision: 10, scale: 4 }), // Maximum stop loss distance %
  minGuaranteedStopDistancePct: decimal('minGuaranteedStopDistancePct', { precision: 10, scale: 4 }),
  
  // Guaranteed Stop Loss (GSL) settings
  guaranteedStopAllowed: boolean('guaranteedStopAllowed').default(false), // Whether instrument supports GSL
  scalingFactor: int('scalingFactor').default(2), // Number of decimal places for price rounding (2 for USD equities, 3-5 for Forex)
  
  // Trading capabilities
  allowShort: boolean('allowShort').default(false), // Whether Capital.com allows shorting this epic (FALSE for leveraged ETFs)
  
  // General
  currency: varchar('currency', { length: 10 }).notNull().default('USD'),
  isActive: boolean('isActive').notNull().default(true),
  lastFetchedFromCapital: timestamp('lastFetchedFromCapital'), // When we last fetched from Capital.com API
  createdAt: timestamp('createdAt').notNull().defaultNow(),
  updatedAt: timestamp('updatedAt').notNull().defaultNow().onUpdateNow(),
});

export type MarketInfo = typeof marketInfo.$inferSelect;
export type InsertMarketInfo = typeof marketInfo.$inferInsert;

/**
 * Unified Candles Table
 * Stores all candle data from all sources (AV, Capital.com) and timeframes (1m, 5m)
 * Includes fake_5min_close for brain calculation accuracy tracking
 */
export const candles = mysqlTable('candles', {
  id: int("id").autoincrement().primaryKey(),
  
  // Identification
  epic: varchar("epic", { length: 20 }).notNull(),
  source: mysqlEnum("source", ["av", "capital"]).notNull().default("capital"),
  timeframe: mysqlEnum("timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"]).notNull().default("5m"),
  timestamp: timestamp("timestamp").notNull(),
  
  // OHLC Bid prices
  openBid: decimal("open_bid", { precision: 20, scale: 8 }).notNull(),
  highBid: decimal("high_bid", { precision: 20, scale: 8 }).notNull(),
  lowBid: decimal("low_bid", { precision: 20, scale: 8 }).notNull(),
  closeBid: decimal("close_bid", { precision: 20, scale: 8 }).notNull(),
  
  // OHLC Ask prices
  openAsk: decimal("open_ask", { precision: 20, scale: 8 }).notNull(),
  highAsk: decimal("high_ask", { precision: 20, scale: 8 }).notNull(),
  lowAsk: decimal("low_ask", { precision: 20, scale: 8 }).notNull(),
  closeAsk: decimal("close_ask", { precision: 20, scale: 8 }).notNull(),
  
  // Volume
  volume: int("volume").default(0),
  
  // Fake 5-min close (populated during brain calculation)
  fake5minClose: decimal("fake_5min_close", { precision: 20, scale: 8 }),
  fake5minSourceTimestamp: timestamp("fake_5min_source_timestamp"),
  fake5minComment: varchar("fake_5min_comment", { length: 500 }),
  fake5minCalculatedAt: timestamp("fake_5min_calculated_at"),
  
  // Data source tracking
  dataSourceType: mysqlEnum("data_source_type", ["api", "websocket", "backfill"]).default("api"),
  receivedAt: timestamp("received_at").defaultNow(),
}, (table) => ({
  // Unique constraint
  uniqueCandle: index("unique_candle").on(table.epic, table.source, table.timeframe, table.timestamp),
  // Indexes
  idxEpicTimeframe: index("idx_epic_timeframe").on(table.epic, table.timeframe),
  idxTimestamp: index("idx_timestamp").on(table.timestamp),
}));

export type Candle = typeof candles.$inferSelect;
export type InsertCandle = typeof candles.$inferInsert;

export const backtestResults = mysqlTable('backtestResults', {
  id: int("id").autoincrement().primaryKey(),
  runId: int("runId").notNull(),
  
  // Test configuration
  epic: varchar("epic", { length: 50 }).notNull(),
  timeframe: varchar("timeframe", { length: 10 }).notNull().default('5m'), // Candle interval (5m, 15m, 1h, etc.)
  crashProtectionEnabled: boolean("crashProtectionEnabled").notNull().default(false), // Whether crash protection was enabled for this test
  
  // Hold Means Hold (HMH) - trail stop loss on HOLD instead of closing position
  hmhEnabled: boolean("hmhEnabled").notNull().default(false), // Whether HMH strategy was used
  hmhStopLossOffset: float("hmhStopLossOffset"), // SL adjustment: 0 (original), -1 (orig-1%), -2 (orig-2%)
  
  // Strategy details
  indicatorName: varchar("indicatorName", { length: 255 }).notNull(),
  indicatorParams: json("indicatorParams").$type<Record<string, any>>().notNull(),
  leverage: float("leverage").notNull(),
  stopLoss: float("stopLoss").notNull(),
  
  // Timing configuration
  timingConfig: json("timingConfig").$type<{
    mode: 
      // Signal-based trading
      | 'SignalBased'
      // Auto-detect market close from data
      | 'MarketClose' | 'T5BeforeClose' | 'T60FakeCandle'
      // Fixed time modes
      | 'USMarketClose' | 'ExtendedHoursClose'
      // Random modes
      | 'random_morning' | 'random_afternoon'
      // Legacy modes
      | 'EpicClosingTimeBrainCalc' | 'EpicClosingTime' | 'USMarketClosingTime' 
      | 'ManusTime' | 'OriginalBotTime' | 'Custom' | 'Random' | 'SpecificHour';
    closeTradesOffset?: number;
    calculateOffset?: number;
    openTradesOffset?: number;
    calc_offset_seconds?: number;
    close_offset_seconds?: number;
    open_offset_seconds?: number;
  }>(),
  
  // Signal-based trading configuration (when mode='SignalBased')
  signalBasedConfig: json("signalBasedConfig").$type<{
    enabled: boolean;
    closeOnNextSignal: boolean;  // Close when indicator triggers again
    maxHoldPeriod: 'same_day' | 'next_day' | 'custom';
    customHoldDays?: number;  // Days to hold if maxHoldPeriod='custom'
  }>(),
  
  // Data source configuration
  dataSource: varchar("dataSource", { length: 20 }).default('av'),  // 'av' or 'capital'
  
  // Deduplication hash (SHA-256 of all input parameters)
  paramHash: varchar("paramHash", { length: 64 }).unique(),
  
  // Results
  initialBalance: float("initialBalance").notNull(),
  finalBalance: float("finalBalance").notNull(),
  totalContributions: float("totalContributions").notNull(),
  totalReturn: float("totalReturn").notNull(), // Percentage
  
  // Trade statistics
  totalTrades: int("totalTrades").notNull(),
  winningTrades: int("winningTrades").notNull(),
  losingTrades: int("losingTrades").notNull(),
  winRate: float("winRate").notNull(),
  
  // Risk metrics
  maxDrawdown: float("maxDrawdown").notNull(),
  sharpeRatio: float("sharpeRatio").notNull(),
  
  // Fee tracking
  totalFees: float("totalFees"), // Total trading costs (spread + overnight funding)
  totalSpreadCosts: float("totalSpreadCosts"), // Just spread costs
  totalOvernightCosts: float("totalOvernightCosts"), // Just overnight funding costs
  
  // Margin Level (ML) tracking - for simulating Capital.com forced liquidations
  minMarginLevel: float("minMarginLevel"), // Lowest ML % during backtest (null if not tracked)
  liquidationCount: int("liquidationCount").default(0), // Number of forced liquidations
  marginLiquidatedTrades: int("marginLiquidatedTrades").default(0), // Number of trades closed by ML breach
  totalLiquidationLoss: float("totalLiquidationLoss").default(0), // Extra loss from liquidations vs stop loss exits
  marginCloseoutLevel: float("marginCloseoutLevel"), // ML threshold used for this backtest (e.g., 55%)
  
  // Detailed data
  trades: json("trades").$type<any[]>(), // Full trade list
  dailyBalances: json("dailyBalances").$type<any[]>(), // Daily balance history
  
  // === VERSION 3: Enhanced Signal-Based Trading ===
  // Backtest type for filtering results (time_based = legacy window-based, signal_based = indicator-driven)
  backtestType: varchar("backtestType", { length: 20 }).default('time_based'),
  
  // Enhanced signal-based config (multi-indicator entry/exit, trust matrix)
  enhancedSignalConfig: json("enhancedSignalConfig").$type<{
    // Entry indicators (bullish signals)
    entryIndicators: string[];  // List of indicator names for entry
    // Exit indicators (bearish signals)
    exitIndicators: string[];   // List of indicator names for exit
    // Position handling
    allowShort: boolean;        // Can short if epic supports it
    reverseOnSignal: boolean;   // true = reverse direction, false = close and wait
    // Risk management
    stopLossMode: 'none' | 'fixed' | 'auto';  // auto = leverage-safe calculation
    stopLossPct: number;        // Fixed stop loss % (if mode='fixed')
    positionSizePct: number;    // % of available balance per trade
    minBalanceThreshold: number; // Early termination if balance falls below
    // Trust matrix
    useTrustMatrix: boolean;    // Use learned indicator pair relationships
  }>(),
  
  createdAt: timestamp("createdAt").defaultNow().notNull(),
});

export type BacktestResult = typeof backtestResults.$inferSelect;
export type InsertBacktestResult = typeof backtestResults.$inferInsert;

/**
 * Indicator Pair Trust Matrix - tracks performance relationships between entry/exit indicators
 * 
 * VERSION 3: Signal-Based Trading Enhancement
 * 
 * Scale: ~10,000 pairs per epic/timeframe (100 bullish × 100 bearish)
 * 
 * This table learns which entry/exit indicator combinations perform best.
 * During backtesting, ALL possible exit paths are simulated in parallel,
 * and trust scores are updated for every pair (not just the winner).
 * 
 * Live trading uses the trust matrix to:
 * 1. Select the best entry when multiple bullish indicators fire
 * 2. Predict which exit indicator will give the best outcome
 * 3. Set optimal leverage/stop-loss based on learned pair performance
 */
export const indicatorPairTrust = mysqlTable('indicatorPairTrust', {
  id: int("id").autoincrement().primaryKey(),
  
  // Pair identification
  epic: varchar("epic", { length: 50 }).notNull(),          // e.g., "SOXL", "TECL"
  timeframe: varchar("timeframe", { length: 10 }).notNull(), // e.g., "5m", "1h"
  entryIndicator: varchar("entryIndicator", { length: 100 }).notNull(),  // Bullish indicator name
  exitIndicator: varchar("exitIndicator", { length: 100 }).notNull(),    // Bearish indicator name
  
  // Performance metrics (updated incrementally)
  trades: int("trades").default(0),                          // Total simulated trades for this pair
  wins: int("wins").default(0),                              // Profitable trades
  totalPnl: decimal("totalPnl", { precision: 12, scale: 2 }).default('0'),  // Cumulative P&L
  avgPnl: decimal("avgPnl", { precision: 10, scale: 4 }),    // Average P&L per trade
  sharpe: decimal("sharpe", { precision: 6, scale: 3 }),     // Risk-adjusted return
  avgHoldBars: int("avgHoldBars"),                           // Average holding period in candles
  winRate: decimal("winRate", { precision: 5, scale: 4 }),   // Win percentage (0.0 to 1.0)
  
  // Conditional probability: P(this exit fires | this entry was used)
  probability: decimal("probability", { precision: 5, scale: 4 }),
  
  // Optimal settings learned from backtests
  optimalLeverage: int("optimalLeverage").default(1),        // Best leverage (1-20)
  optimalStopLoss: decimal("optimalStopLoss", { precision: 5, scale: 2 }),  // Best stop loss %
  
  // Selection tracking
  wasWinner: int("wasWinner").default(0),                    // Times this pair was the BEST option
  
  // Timestamps
  lastUpdated: timestamp("lastUpdated"),
  createdAt: timestamp("createdAt").defaultNow().notNull(),
}, (table) => ({
  // CRITICAL: Composite unique index for O(1) lookups during live trading
  // Query pattern: SELECT * FROM indicatorPairTrust WHERE epic=? AND timeframe=? AND entryIndicator=? AND exitIndicator=?
  uniquePair: index("idx_pair_unique").on(table.epic, table.timeframe, table.entryIndicator, table.exitIndicator),
  // Index for loading all pairs for an epic/timeframe (used to populate in-memory cache)
  idxEpicTf: index("idx_epic_tf").on(table.epic, table.timeframe),
}));

export type IndicatorPairTrust = typeof indicatorPairTrust.$inferSelect;
export type InsertIndicatorPairTrust = typeof indicatorPairTrust.$inferInsert;

/**
 * Indicator Relationships - Discovered optimal entry/exit pairs from chain backtest
 * 
 * This table stores the results of relationship discovery runs. Each row represents
 * an optimized entry/exit indicator pair with their best parameters.
 * 
 * Migration: 0012_add_chain_backtest_tables.sql
 */
export const indicatorRelationships = mysqlTable('indicator_relationships', {
  id: int("id").autoincrement().primaryKey(),
  epic: varchar("epic", { length: 20 }).notNull(),
  timeframe: varchar("timeframe", { length: 10 }).notNull(),
  directionMode: mysqlEnum("direction_mode", ["long_only", "short_only", "reverse"]).notNull(),
  
  // Entry indicator
  entryIndicator: varchar("entry_indicator", { length: 100 }).notNull(),
  entryParams: json("entry_params").$type<Record<string, any>>().notNull(),
  
  // Exit indicator
  exitIndicator: varchar("exit_indicator", { length: 100 }).notNull(),
  exitParams: json("exit_params").$type<Record<string, any>>().notNull(),
  
  // Optimal settings
  optimalLeverage: int("optimal_leverage").notNull().default(5),
  optimalStopLoss: float("optimal_stop_loss").notNull().default(2.0),
  
  // Performance metrics (from chain backtest)
  finalBalance: float("final_balance").notNull(),
  totalReturnPct: float("total_return_pct").notNull(),
  totalTrades: int("total_trades").notNull(),
  winRate: float("win_rate").notNull(),
  maxDrawdown: float("max_drawdown").notNull(),
  sharpeRatio: float("sharpe_ratio").notNull(),
  avgTradePnl: float("avg_trade_pnl").notNull(),
  avgHoldBars: int("avg_hold_bars").notNull(),
  
  // Discovery metadata
  discoveryStage: int("discovery_stage").notNull().default(1),
  discoveryRunId: varchar("discovery_run_id", { length: 50 }).notNull(),
  testsRun: int("tests_run").notNull().default(0),
  
  // Timestamps
  discoveredAt: timestamp("discovered_at").defaultNow(),
  lastValidated: timestamp("last_validated"),
  
  // Ranking
  rankScore: float("rank_score").notNull().default(0),
}, (table) => ({
  uniqueRelationship: index("unique_relationship").on(table.epic, table.timeframe, table.directionMode, table.entryIndicator, table.exitIndicator),
  idxEpicTimeframe: index("idx_epic_timeframe").on(table.epic, table.timeframe),
  idxRank: index("idx_rank").on(table.epic, table.timeframe, table.rankScore),
  idxDiscoveryRun: index("idx_discovery_run").on(table.discoveryRunId),
}));

export type IndicatorRelationship = typeof indicatorRelationships.$inferSelect;
export type InsertIndicatorRelationship = typeof indicatorRelationships.$inferInsert;

/**
 * Discovery Runs - Tracks relationship discovery progress
 * 
 * Each discovery run tests entry/exit indicator pairs across 3 stages:
 * - Stage 1: Coarse search (many samples, find promising pairs)
 * - Stage 2: Refinement (more samples on top pairs)
 * - Stage 3: Fine-tuning (grid search for exact optimal params)
 * 
 * Migration: 0012_add_chain_backtest_tables.sql
 */
export const discoveryRuns = mysqlTable('discovery_runs', {
  id: int("id").autoincrement().primaryKey(),
  runId: varchar("run_id", { length: 50 }).notNull().unique(),
  
  // Configuration
  epic: varchar("epic", { length: 20 }).notNull(),
  timeframe: varchar("timeframe", { length: 10 }).notNull(),
  directionMode: mysqlEnum("direction_mode", ["long_only", "short_only", "reverse"]).notNull(),
  
  // Entry/Exit indicators selected
  entryIndicators: json("entry_indicators").$type<string[]>().notNull(),
  exitIndicators: json("exit_indicators").$type<string[]>().notNull(),
  
  // Financial settings
  initialBalance: float("initial_balance").notNull().default(500),
  monthlyTopup: float("monthly_topup").notNull().default(100),
  positionSizePct: float("position_size_pct").notNull().default(50),
  
  // Discovery settings
  stage1Samples: int("stage1_samples").notNull().default(100),
  stage2Samples: int("stage2_samples").notNull().default(500),
  stage3GridSize: int("stage3_grid_size").notNull().default(20),
  topNPairs: int("top_n_pairs").notNull().default(10),
  topNFinal: int("top_n_final").notNull().default(3),
  
  // Leverage settings
  leverageMode: mysqlEnum("leverage_mode", ["auto", "fixed", "search"]).notNull().default("auto"),
  fixedLeverage: int("fixed_leverage"),
  leverageSearchRange: json("leverage_search_range").$type<[number, number]>(),
  
  // Stop loss settings
  stopLossMode: mysqlEnum("stop_loss_mode", ["auto", "fixed", "search"]).notNull().default("auto"),
  fixedStopLoss: float("fixed_stop_loss"),
  stopLossSearchRange: json("stop_loss_search_range").$type<[number, number]>(),
  
  // Progress tracking
  status: mysqlEnum("status", ["pending", "stage1", "stage2", "stage3", "completed", "failed", "cancelled"]).notNull().default("pending"),
  currentStage: int("current_stage").notNull().default(0),
  progressPct: int("progress_pct").notNull().default(0),
  
  // Stage 1 progress
  stage1TotalTests: int("stage1_total_tests"),
  stage1CompletedTests: int("stage1_completed_tests").default(0),
  stage1BestPairs: json("stage1_best_pairs").$type<Array<{entry: string, exit: string, score: number}>>(),
  stage1CompletedAt: timestamp("stage1_completed_at"),
  
  // Stage 2 progress
  stage2TotalTests: int("stage2_total_tests"),
  stage2CompletedTests: int("stage2_completed_tests").default(0),
  stage2BestPairs: json("stage2_best_pairs").$type<Array<{entry: string, exit: string, score: number, params: any}>>(),
  stage2CompletedAt: timestamp("stage2_completed_at"),
  
  // Stage 3 progress
  stage3TotalTests: int("stage3_total_tests"),
  stage3CompletedTests: int("stage3_completed_tests").default(0),
  stage3CompletedAt: timestamp("stage3_completed_at"),
  
  // Final results
  finalRelationshipsCount: int("final_relationships_count"),
  bestRelationshipId: int("best_relationship_id"),
  
  // Timing
  startedAt: timestamp("started_at"),
  completedAt: timestamp("completed_at"),
  errorMessage: text("error_message"),
  
  // Timestamps
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow().onUpdateNow(),
}, (table) => ({
  idxStatus: index("idx_status").on(table.status),
  idxEpic: index("idx_epic").on(table.epic, table.timeframe),
}));

export type DiscoveryRun = typeof discoveryRuns.$inferSelect;
export type InsertDiscoveryRun = typeof discoveryRuns.$inferInsert;

/**
 * Actual Trades - stores all live/simulated trades made by the system
 *
 * This is the PRIMARY table for tracking trades executed via Capital.com.
 * Each record represents a trade opened by the brain/orchestration system.
 * 
 * Key concepts:
 * - windowCloseTime: Which execution window this trade belongs to (e.g., "16:00:00", "20:00:00")
 * - isSimulation: true for simulation mode (demo accounts, forced BUY), false for live trading
 * - dealId: Capital.com's unique identifier, captured during reconciliation at T+1 second
 * - candleDataSnapshot: JSON of candles used for brain decision (for later validation)
 */
export const actualTrades = mysqlTable("actual_trades", {
  id: int("id").autoincrement().primaryKey(),
  accountId: int("accountId").notNull(), // References accounts.id
  strategyId: int("strategyId").notNull(), // References savedStrategies.id
  windowCloseTime: varchar("windowCloseTime", { length: 8 }).notNull(), // Resolved/actual window time
  storedWindowTime: varchar("storedWindowTime", { length: 8 }), // Original config time (for audit trail)
  epic: varchar("epic", { length: 50 }).notNull(),
  direction: mysqlEnum("direction", ["BUY", "SELL"]).notNull(),
  
  // Trade details from Capital.com
  dealId: varchar("dealId", { length: 100 }), // Capital.com deal ID (populated at reconciliation)
  dealReference: varchar("dealReference", { length: 100 }), // Capital.com deal reference
  entryPrice: float("entryPrice"), // Actual entry price from Capital.com (execution at T-15)
  
  // Brain calculation price tracking (for validation accuracy)
  // These fields allow comparing brain's T-60 API price vs backtest's 4th 1m candle
  brainCalcPrice: float("brainCalcPrice"), // T-60 API price that brain used for indicator calculation
  fake5minClose: float("fake5minClose"),   // 4th 1m candle close (what backtest would use)
  priceVariancePct: float("priceVariancePct"), // Variance between brainCalcPrice and fake5minClose (%)
  last1mCandleTime: timestamp("last1mCandleTime"), // Timestamp of the 4th 1m candle used
  
  // Trade source - how was this trade created?
  tradeSource: mysqlEnum("tradeSource", ["app", "manual", "stoploss", "system", "unknown"]).default("app"),
  exitPrice: float("exitPrice"), // Actual exit price
  contracts: float("contracts"), // Number of contracts traded
  notionalValue: float("notionalValue"), // Total notional value (contracts × price)
  marginUsed: float("marginUsed"), // Margin required for this position
  leverage: int("leverage").notNull(),
  stopLossPrice: float("stopLossPrice"), // Stop loss price level
  stopLossPercent: float("stopLossPercent"), // Stop loss % from DNA config (e.g., 2.15)
  stopLossType: mysqlEnum("stopLossType", ["standard", "guaranteed"]).default("standard"), // Type of stop loss used
  stopLossSlippage: float("stopLossSlippage"), // Slippage amount (expected SL price - actual exit price) * contracts
  guaranteedStopPremium: float("guaranteedStopPremium"), // Extra spread paid for guaranteed stop (if used)
  
  // Hold Means Hold (HMH) - trail stop loss on HOLD instead of closing
  hmhEnabled: boolean("hmhEnabled").default(false), // Whether this trade uses HMH strategy
  hmhStopLossOffset: float("hmhStopLossOffset"), // SL offset: 0 (original), -1, -2
  hmhTrailingStopPrice: float("hmhTrailingStopPrice"), // Current trailing SL price (updated each session)
  hmhIsContnuation: boolean("hmhIsContnuation").default(false), // True if this is a continued HMH position (not newly opened)
  hmhDaysHeld: int("hmhDaysHeld").default(1), // Number of trading sessions this HMH position has been held
  hmhOriginalEntryPrice: float("hmhOriginalEntryPrice"), // Original entry price when position was first opened (for multi-day HMH)
  
  timeframe: varchar("timeframe", { length: 10 }), // Winning DNA timeframe (e.g., "5m", "1h")
  timingConfig: json("timingConfig").$type<{ mode: string; [key: string]: any }>(), // Timing mode (e.g., { mode: "Fake5min_4thCandle" })
  dataSource: varchar("dataSource", { length: 20 }), // Data source: 'capital' or 'av'
  
  // P&L breakdown
  grossPnl: float("grossPnl"),
  spreadCost: float("spreadCost"),
  overnightCost: float("overnightCost"),
  netPnl: float("netPnl"),
  
  // Error tracking (for failed trades)
  errorMessage: text("errorMessage"), // Full error message if trade failed (e.g., "RISK_CHECK: Insufficient margin")
  errorCode: varchar("errorCode", { length: 100 }), // Capital.com error code (e.g., "error.service.risk-check")
  
  // Brain decision details
  // HOLD_TRAIL = HMH mode: don't close, just adjust stop loss
  brainDecision: mysqlEnum("brainDecision", ["BUY", "HOLD", "HOLD_TRAIL"]).notNull(),
  winningIndicatorName: varchar("winningIndicatorName", { length: 100 }), // e.g., "rsi_oversold"
  winningTestId: int("winningTestId"), // Reference to backtestResults.id
  indicatorParams: json("indicatorParams").$type<Record<string, any>>(), // Full indicator parameters
  conflictResolutionMetric: varchar("conflictResolutionMetric", { length: 50 }), // e.g., "sharpeRatio"
  
  // Candle range used for brain calculation (for validation)
  candleStartDate: timestamp("candleStartDate"), // First candle timestamp used by brain
  candleEndDate: timestamp("candleEndDate"),     // Last candle timestamp used by brain
  candleCount: int("candleCount"),               // Number of candles used
  
  // All DNA test results at trade time (for comprehensive validation)
  // Stores results of ALL DNA strands + conflict resolution decision
  allDnaResults: json("allDnaResults").$type<{
    dnaResults: Array<{
      testIndex: number;
      indicatorName: string;
      indicatorParams: Record<string, any>;
      epic: string;
      timeframe: string;
      signal: 'BUY' | 'HOLD';
      indicatorValue: number | null;
      // Historical performance metrics from backtest results
      sharpeRatio: number | null;
      totalReturn: number | null;
      winRate: number | null;
      maxDrawdown: number | null;
    }>;
    conflictResolution: {
      metric: string;           // e.g., "sharpeRatio", "totalReturn"
      winnerIndex: number;      // Index of winning DNA
      reason: string;           // e.g., "Highest Sharpe Ratio (2.1)"
      hadConflict: boolean;     // True if multiple DNAs signaled BUY
    };
  }>(),
  
  // Candle data used for decision (for validation)
  candleDataSnapshot: json("candleDataSnapshot").$type<{
    epic: string;
    candles: Array<{
      timestamp: string;
      open: number;
      high: number;
      low: number;
      close: number;
      volume?: number;
      source: 'av' | 'capital';
    }>;
    lastAvCandleTime: string;
    lastCapitalCandleTime?: string;
  }>(),
  lastAvCandleTime: timestamp("lastAvCandleTime"),
  lastCapitalCandleTime: timestamp("lastCapitalCandleTime"),
  
  // Status tracking
  // pending = brain calculated, waiting for trade execution
  // open = trade executed and confirmed with Capital.com
  // closed = position closed (manually, stop loss, or window close)
  // stopped_out = closed by stop loss
  // failed = Capital.com rejected the order (RISK_CHECK, market closed, etc.)
  // error = system error (API failure, database issue, etc.)
  status: mysqlEnum("status", ["pending", "open", "closed", "stopped_out", "failed", "error"]).default("pending").notNull(),
  openedAt: timestamp("openedAt"),
  capitalExecutedAt: timestamp("capitalExecutedAt"), // Capital.com's actual execution timestamp (from createdDateUTC)
  closedAt: timestamp("closedAt"),
  closeReason: mysqlEnum("closeReason", ["window_close", "stop_loss", "manual", "rebalance", "rejected", "safety_net_t30"]),
  
  // Simulation flag - CRITICAL: distinguishes simulation from live trading
  isSimulation: boolean("isSimulation").default(false).notNull(),
  
  // Timestamps
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
}, (table) => ({
  accountWindowIdx: index("idx_account_window").on(table.accountId, table.windowCloseTime),
  dealIdIdx: index("idx_deal_id").on(table.dealId),
  statusIdx: index("idx_status").on(table.status),
  createdIdx: index("idx_created").on(table.createdAt),
}));

export type ActualTrade = typeof actualTrades.$inferSelect;
export type InsertActualTrade = typeof actualTrades.$inferInsert;

/**
 * Account Transactions - stores ALL Capital.com transactions for balance tracking
 * 
 * Transaction Types from Capital.com:
 * - TRADE: Position P&L (profit/loss from closed trades)
 * - SWAP: Overnight funding fees
 * - DEPOSIT: Money deposited
 * - WITHDRAWAL: Money withdrawn
 * - TRANSFER: Money transferred between accounts
 * - DIVIDEND: Dividend payments
 * - CORRECTION: Account corrections/adjustments
 * 
 * This table enables:
 * - Running balance reconstruction
 * - Actual vs Expected P&L comparison
 * - Fee tracking (swap costs over time)
 * - Deposit/withdrawal history
 */
export const accountTransactions = mysqlTable("account_transactions", {
  id: int("id").autoincrement().primaryKey(),
  accountId: int("accountId").notNull(), // References accounts.id
  
  // Capital.com transaction data
  transactionType: mysqlEnum("transactionType", [
    "TRADE",       // Position P&L
    "SWAP",        // Overnight funding
    "DEPOSIT",     // Money in
    "WITHDRAWAL",  // Money out
    "TRANSFER",    // Between accounts
    "DIVIDEND",    // Dividend payments
    "CORRECTION",  // Adjustments
    "OTHER"        // Unknown types
  ]).notNull(),
  
  // Transaction details
  amount: float("amount").notNull(),           // +/- amount
  runningBalance: float("runningBalance"),     // Balance after transaction (if available)
  currency: varchar("currency", { length: 10 }).default("USD"),
  
  // Capital.com reference
  capitalReference: varchar("capitalReference", { length: 100 }), // Capital.com reference ID
  dealId: varchar("dealId", { length: 100 }),   // For TRADE/SWAP: associated dealId
  epic: varchar("epic", { length: 50 }),        // For TRADE/SWAP: instrument
  note: text("note"),                           // Capital.com note field
  
  // Date from Capital.com
  transactionDate: timestamp("transactionDate").notNull(),
  
  // Metadata
  rawData: json("rawData").$type<Record<string, any>>(), // Full raw response for debugging
  createdAt: timestamp("createdAt").defaultNow().notNull(),
}, (table) => ({
  accountDateIdx: index("idx_account_date").on(table.accountId, table.transactionDate),
  typeIdx: index("idx_transaction_type").on(table.transactionType),
  dealIdx: index("idx_transaction_deal").on(table.dealId),
}));

export type AccountTransaction = typeof accountTransactions.$inferSelect;
export type InsertAccountTransaction = typeof accountTransactions.$inferInsert;

/**
 * Validated Trades - stores DNA re-run validation results for actual trades
 * 
 * When user clicks "Validate" on an actual trade, we re-run the EXACT winning DNA
 * (same indicator, params, leverage, stop loss) using the LATEST Capital.com data
 * to verify that the brain calculation was accurate.
 * 
 * This validates:
 * - Did the DNA produce the same signal (BUY/HOLD) on that day?
 * - Was the entry price similar?
 * - Were the exact parameters the same?
 */
export const validatedTrades = mysqlTable("validated_trades", {
  id: int("id").autoincrement().primaryKey(),
  actualTradeId: int("actualTradeId").notNull(), // References actual_trades.id
  
  // Original trade details (copied for comparison)
  originalEpic: varchar("originalEpic", { length: 50 }),
  originalIndicatorName: varchar("originalIndicatorName", { length: 100 }),
  originalIndicatorParams: json("originalIndicatorParams").$type<Record<string, any>>(),
  originalLeverage: int("originalLeverage"),
  originalStopLoss: float("originalStopLoss"),
  originalEntryPrice: float("originalEntryPrice"),
  originalTradeDate: date("originalTradeDate"),
  
  // DNA re-run results using latest Capital.com data
  rerunSignal: mysqlEnum("rerunSignal", ["BUY", "HOLD"]), // What signal did DNA produce?
  rerunEntryPrice: float("rerunEntryPrice"), // Entry price from re-run
  rerunIndicatorValue: float("rerunIndicatorValue"), // Indicator value (e.g., RSI value)
  rerunWouldHaveTraded: boolean("rerunWouldHaveTraded"), // Would re-run have triggered trade?
  
  // Comparison results
  signalMatch: boolean("signalMatch"), // Did signal match? (both BUY or both HOLD)
  priceMatch: boolean("priceMatch"), // Entry price within 1%?
  priceDifferencePercent: float("priceDifferencePercent"), // Exact price difference %
  
  // Data used for validation
  dataSource: varchar("dataSource", { length: 20 }), // 'capital' - always Capital.com for validation
  candleCountUsed: int("candleCountUsed"), // Number of candles used in re-run
  lastCandleTimestamp: timestamp("lastCandleTimestamp"), // Most recent candle used
  
  // Overall validation status
  validationStatus: mysqlEnum("validationStatus", [
    "pending",        // Not yet validated
    "validated",      // Signal and trade match
    "signal_mismatch", // Signal doesn't match
    "winner_mismatch", // Different DNA won in re-run
    "price_mismatch",  // Signal matches but price too different
    "data_not_ready",  // Not enough data available
    "error"           // Validation failed
  ]).default("pending").notNull(),
  validationNotes: text("validationNotes"), // Additional notes/warnings
  
  // Timestamps
  validatedAt: timestamp("validatedAt"),
  createdAt: timestamp("createdAt").defaultNow().notNull(),
}, (table) => ({
  actualTradeIdx: index("idx_actual_trade").on(table.actualTradeId),
}));

export type ValidatedTrade = typeof validatedTrades.$inferSelect;
export type InsertValidatedTrade = typeof validatedTrades.$inferInsert;

/**
 * Trade Comparisons - aggregated view for comparing actual vs validated trades
 * 
 * This table provides a per-day, per-window summary for easy visualization
 * in the Trade History UI. It shows at a glance whether the live system
 * matched what the backtest would have done.
 */
export const tradeComparisons = mysqlTable("trade_comparisons", {
  id: int("id").autoincrement().primaryKey(),
  accountId: int("accountId").notNull(),
  tradeDate: date("tradeDate").notNull(),
  windowCloseTime: varchar("windowCloseTime", { length: 8 }).notNull(),
  
  // Actual trade summary
  actualTraded: boolean("actualTraded").notNull(),
  actualIndicator: varchar("actualIndicator", { length: 100 }),
  actualPnl: float("actualPnl"),
  actualTradeId: int("actualTradeId"), // Reference to actual_trades.id
  
  // Validated trade summary
  validatedWouldTrade: boolean("validatedWouldTrade"),
  validatedIndicator: varchar("validatedIndicator", { length: 100 }),
  validatedPnl: float("validatedPnl"),
  validatedTradeId: int("validatedTradeId"), // Reference to validated_trades.id
  
  // Comparison result
  matchStatus: mysqlEnum("matchStatus", ["match", "mismatch", "actual_only", "validated_only", "pending"]).default("pending").notNull(),
  pnlDifference: float("pnlDifference"),
  
  createdAt: timestamp("createdAt").defaultNow().notNull(),
}, (table) => ({
  uniqueComparison: index("idx_unique_comparison").on(table.accountId, table.tradeDate, table.windowCloseTime),
}));

export type TradeComparison = typeof tradeComparisons.$inferSelect;
export type InsertTradeComparison = typeof tradeComparisons.$inferInsert;

/**
 * Capital.com candles cache - stores gap-fill candles for validation
 * 
 * When brain runs, it fills gaps between AV data and current time using Capital.com API.
 * We store these candles so we can later validate trades by comparing:
 * - Live decision (AV + Capital.com candles)
 * - Validated decision (100% AV candles after refresh)
 */
export const capitalCandlesCache = mysqlTable("capital_candles_cache", {
  id: int("id").autoincrement().primaryKey(),
  
  // Candle identification
  epic: varchar("epic", { length: 20 }).notNull(),
  timestamp: timestamp("timestamp").notNull(),
  
  // OHLCV data
  openPrice: float("openPrice").notNull(),
  highPrice: float("highPrice").notNull(),
  lowPrice: float("lowPrice").notNull(),
  closePrice: float("closePrice").notNull(),
  volume: int("volume"),
  
  // Metadata
  fetchedAt: timestamp("fetchedAt").defaultNow().notNull(), // When we fetched from Capital.com
  usedByTradeId: int("usedByTradeId"), // Link to liveTrades (which trade used this candle)
  
  createdAt: timestamp("createdAt").defaultNow().notNull(),
}, (table) => {
  return {
    epicTimestampIdx: index("idx_epic_timestamp").on(table.epic, table.timestamp),
    tradeIdx: index("idx_trade").on(table.usedByTradeId),
  };
});

export type CapitalCandleCache = typeof capitalCandlesCache.$inferSelect;
export type InsertCapitalCandleCache = typeof capitalCandlesCache.$inferInsert;

/**
 * Bot execution logs
 */
export const botLogs = mysqlTable("botLogs", {
  id: int("id").autoincrement().primaryKey(),
  accountId: int("accountId").notNull(),
  botId: int("botId").notNull(),
  
  level: mysqlEnum("level", ["info", "warning", "error"]).notNull(),
  message: text("message").notNull(),
  details: json("details").$type<Record<string, any>>(),
  
  timestamp: timestamp("timestamp").defaultNow().notNull(),
});

export type BotLog = typeof botLogs.$inferSelect;
export type InsertBotLog = typeof botLogs.$inferInsert;



/**
 * Saved strategies - container for multiple indicator/backtest DNA strands
 * Stores all DNA and window configuration in JSON fields
 */
export const savedStrategies = mysqlTable("savedStrategies", {
  id: int("id").autoincrement().primaryKey(),
  userId: int("userId").notNull(),
  
  // Strategy identification
  name: varchar("name", { length: 255 }).notNull(),
  description: text("description"),
  
  // DNA Strands (array of indicator/backtest configurations)
  // Each DNA strand captures the EXACT configuration used in the backtest
  dnaStrands: json("dnaStrands").$type<Array<{
    id: string; // Unique ID for this DNA strand
    sourceType: 'indicator' | 'backtest';
    sourceTestId: number; // Reference to backtestResults.id
    sourceRunId?: number; // Reference to backtestRuns.id
    indicatorName: string;
    indicatorParams: Record<string, any>;
    epic: string;
    timeframe: string; // e.g., "5m", "15m", "1h"
    direction: 'long' | 'short' | 'both';
    leverage: number;
    stopLoss: number;
    
    // Crash protection (must match backtest for consistent signals)
    crashProtectionEnabled?: boolean;  // Whether crash protection was enabled in original backtest
    
    // Hold Means Hold (HMH) - set new SL on HOLD instead of closing position
    hmhEnabled?: boolean;  // Whether HMH strategy was used in original backtest
    hmhStopLossOffset?: number;  // SL adjustment: 0 (original), -1 (orig-1%), -2 (orig-2%)
    
    // Guaranteed Stop Loss - pays extra spread but no slippage
    guaranteedStopEnabled?: boolean;  // Default false (standard stop loss)
    
    // Data source configuration
    dataSource?: 'av' | 'capital';  // Which data source was used
    
    // Timing configuration (copied from backtest)
    timingConfig?: {
      mode: string;  // MarketClose, T5BeforeClose, T60FakeCandle, SignalBased, etc.
      calc_offset_seconds?: number;
      close_offset_seconds?: number;
      open_offset_seconds?: number;
    };
    
    // Signal-based trading configuration (if mode='SignalBased')
    signalBasedConfig?: {
      enabled: boolean;
      closeOnNextSignal: boolean;
      maxHoldPeriod: 'same_day' | 'next_day' | 'custom';
      customHoldDays?: number;
    };
    
    // Performance metrics from source backtest
    profitability: number;
    winRate: number;
    sharpeRatio: number;
    maxDrawdown: number;
    totalTrades: number;
    lastTestedDate: string; // ISO date
    // Status
    isActive: boolean; // false = paused/sleeping
    isPaused: boolean; // true = greyed out, excluded from calculations
    addedAt: string; // ISO date when added to strategy
  }>>().default([]),
  
  // Window Configuration
  windowConfig: json("windowConfig").$type<{
    windows: Array<{
      id: string; // Unique window ID
      closeTime: string; // HH:MM:SS format
      windowName: string; // e.g., "Regular Hours", "Extended Hours"
      allocationPct: number; // Percentage of total funds for this window (0-100)
      carryOver: boolean; // If true, HOLD passes funds to next window
      conflictResolutionMetric: string; // "profitability", "sharpe", "winRate", etc.
      dnaStrandIds: string[]; // IDs of DNA strands in this window
    }>;
    totalAccountBalancePct: number; // e.g., 99% of account balance to use
  }>(),
  
  // Configuration status
  isConfigured: boolean("isConfigured").default(false).notNull(),
  
  // User preferences
  isFavorite: boolean("isFavorite").default(false).notNull(),
  tags: json("tags").$type<string[]>(), // User-defined tags
  
  // VERSION 3: Enhanced Signal-Based Trading Configuration
  // Stored when a signal-based backtest is attached to the strategy
  enhancedSignalConfig: json("enhancedSignalConfig").$type<{
    enabled: boolean;
    entryIndicators: string[];  // List of bullish indicator names for entry
    exitIndicators: string[];   // List of bearish indicator names for exit
    entryParams: Record<string, Record<string, any>>;  // Params per entry indicator
    exitParams: Record<string, Record<string, any>>;   // Params per exit indicator
    allowShort: boolean;
    reverseOnSignal: boolean;
    stopLossMode: 'none' | 'fixed' | 'auto';
    stopLossPct?: number;
    positionSizePct: number;
    defaultLeverage: number;
    minBalanceThreshold: number;
    useTrustMatrix: boolean;
  }>(),
  
  // VERSION 3: Trust Matrix Snapshot
  // Frozen copy of indicator pair trust scores from the backtest
  // Used during live trading to select optimal entry/exit combinations
  trustMatrixSnapshot: json("trustMatrixSnapshot").$type<Array<{
    entryIndicator: string;
    exitIndicator: string;
    trades: number;
    wins: number;
    totalPnl: number;
    avgPnl: number;
    sharpe: number;
    avgHoldBars: number;
    winRate: number;
    probability: number;
    optimalLeverage: number;
    optimalStopLoss: number;
  }>>(),
  
  // Metadata
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
  
  // Archive functionality (soft delete)
  // Archived strategies are hidden but preserved for historical trade comparison
  isArchived: boolean("isArchived").default(false).notNull(),
  archivedAt: timestamp("archivedAt"),
  archivedBy: int("archivedBy"), // User ID who archived
});

export type SavedStrategy = typeof savedStrategies.$inferSelect;
export type InsertSavedStrategy = typeof savedStrategies.$inferInsert;

/**
 * Strategy Assignment History
 * Tracks when strategies are assigned/removed/edited on accounts.
 * This allows comparing historical trades against the strategy that was active at that time.
 */
export const strategyAssignmentHistory = mysqlTable("strategy_assignment_history", {
  id: int("id").autoincrement().primaryKey(),
  accountId: int("accountId").notNull(),
  strategyId: int("strategyId").notNull(),
  
  // What happened
  action: mysqlEnum("action", ["assigned", "removed", "edited"]).notNull(),
  previousStrategyId: int("previousStrategyId"), // For 'edited' action - what was replaced
  
  // Snapshot of strategy DNA at time of assignment
  // This preserves the exact configuration even if strategy is later modified
  dnaSnapshot: json("dnaSnapshot").$type<Array<{
    id: string;
    indicatorName: string;
    indicatorParams: Record<string, any>;
    epic: string;
    timeframe: string;
    leverage: number;
    stopLoss: number;
    timingConfig?: { mode: string };
    dataSource?: string;
    crashProtectionEnabled?: boolean;
    sharpeRatio?: number;
    totalReturn?: number;
    winRate?: number;
  }>>(),
  
  // Window config snapshot
  windowConfigSnapshot: json("windowConfigSnapshot").$type<{
    windows: Array<{
      id: string;
      closeTime: string;
      allocationPct: number;
      conflictResolutionMetric: string;
      dnaStrandIds: string[];
    }>;
    totalAccountBalancePct: number;
  }>(),
  
  // Metadata
  assignedAt: timestamp("assignedAt").defaultNow().notNull(),
  assignedBy: int("assignedBy"), // User ID who made the change
  notes: text("notes"),
});

export type StrategyAssignmentHistory = typeof strategyAssignmentHistory.$inferSelect;

/**
 * Strategy runs - execution history of strategy tests
 * Each run simulates the brains calculation on historical data
 */
export const strategyRuns = mysqlTable("strategyRuns", {
  id: int("id").autoincrement().primaryKey(),
  strategyId: int("strategyId").notNull(), // Reference to savedStrategies
  userId: int("userId").notNull(),
  
  // Test configuration
  startDate: varchar("startDate", { length: 10 }).notNull(), // YYYY-MM-DD
  endDate: varchar("endDate", { length: 10 }).notNull(), // YYYY-MM-DD
  initialBalance: float("initialBalance").notNull(),
  monthlyTopup: float("monthlyTopup").notNull(),
  
  // Strategy configuration snapshot (at time of run)
  configSnapshot: json("configSnapshot").$type<{
    dnaStrands: Array<{
      id: string;
      indicatorName: string;
      indicatorParams: Record<string, any>;
      epic: string;
      timeframe: string;
      leverage: number;
      stopLoss: number;
      isPaused: boolean;
    }>;
    windowConfig: {
      windows: Array<{
        id: string;
        closeTime: string;
        windowName: string;
        allocationPct: number;
        carryOver: boolean;
        conflictResolutionMetric: string;
        dnaStrandIds: string[];
      }>;
      totalAccountBalancePct: number;
    };
  }>().notNull(),
  
  // Results
  finalBalance: float("finalBalance").notNull(),
  totalContributions: float("totalContributions").notNull(),
  totalReturn: float("totalReturn").notNull(), // Percentage
  
  // Trade statistics
  totalTrades: int("totalTrades").notNull(),
  winningTrades: int("winningTrades").notNull(),
  losingTrades: int("losingTrades").notNull(),
  winRate: float("winRate").notNull(),
  
  // Risk metrics
  maxDrawdown: float("maxDrawdown").notNull(),
  sharpeRatio: float("sharpeRatio").notNull(),
  
  // Fee tracking
  totalFees: float("totalFees"), // Total trading costs (spread + overnight funding)
  totalSpreadCosts: float("totalSpreadCosts"), // Just spread costs
  totalOvernightCosts: float("totalOvernightCosts"), // Just overnight funding costs
  
  // Detailed data (same format as backtestResults for chart reuse)
  trades: json("trades").$type<any[]>(), // Full trade list with window info
  dailyBalances: json("dailyBalances").$type<any[]>(), // Daily balance history
  
  // Conflict resolution logs
  conflictLogs: json("conflictLogs").$type<Array<{
    timestamp: string;
    windowId: string;
    signals: Array<{
      dnaStrandId: string;
      indicatorName: string;
      signal: 'BUY' | 'HOLD';
      metric: number;
    }>;
    winner: string; // dnaStrandId that won
    resolution: string; // How it was resolved
  }>>(),
  
  // Execution metadata
  executionTimeMs: int("executionTimeMs"), // How long the test took
  
  createdAt: timestamp("createdAt").defaultNow().notNull(),
});

export type StrategyRun = typeof strategyRuns.$inferSelect;
export type InsertStrategyRun = typeof strategyRuns.$inferInsert;

/**
 * Strategy combinations - multiple strategies working together
 */
export const strategyCombos = mysqlTable("strategyCombos", {
  id: int("id").autoincrement().primaryKey(),
  userId: int("userId").notNull(),
  
  // Combo identification
  name: varchar("name", { length: 255 }).notNull(),
  description: text("description"),
  
  // Strategy IDs in this combo
  strategyIds: json("strategyIds").$type<number[]>().notNull(),
  
  // Conflict resolution configuration
  resolutionMode: varchar("resolutionMode", { length: 50 }).notNull(), // e.g., "voting", "weighted_score"
  resolutionMetrics: json("resolutionMetrics").$type<{
    metric: string;
    weight: number;
  }[]>(), // For weighted_score mode
  
  // Performance (if tested)
  hasBeenTested: boolean("hasBeenTested").default(false).notNull(),
  lastTestDate: timestamp("lastTestDate"),
  
  // Test results summary
  profitability: float("profitability"),
  winRate: float("winRate"),
  sharpeRatio: float("sharpeRatio"),
  maxDrawdown: float("maxDrawdown"),
  totalConflicts: int("totalConflicts"), // Number of conflicts encountered
  conflictResolutionRate: float("conflictResolutionRate"), // % of conflicts resolved successfully
  
  // Metadata
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
});

export type StrategyCombo = typeof strategyCombos.$inferSelect;
export type InsertStrategyCombo = typeof strategyCombos.$inferInsert;

/**
 * Combo backtest results
 */
export const comboResults = mysqlTable("comboResults", {
  id: int("id").autoincrement().primaryKey(),
  comboId: int("comboId").notNull(),
  runId: int("runId").notNull(), // Reference to backtestRuns
  
  // Configuration
  epic: varchar("epic", { length: 50 }).notNull(),
  startDate: varchar("startDate", { length: 10 }).notNull(),
  endDate: varchar("endDate", { length: 10 }).notNull(),
  
  // Results
  initialBalance: float("initialBalance").notNull(),
  finalBalance: float("finalBalance").notNull(),
  totalReturn: float("totalReturn").notNull(),
  
  // Trade statistics
  totalTrades: int("totalTrades").notNull(),
  winningTrades: int("winningTrades").notNull(),
  losingTrades: int("losingTrades").notNull(),
  winRate: float("winRate").notNull(),
  
  // Risk metrics
  maxDrawdown: float("maxDrawdown").notNull(),
  sharpeRatio: float("sharpeRatio").notNull(),
  
  // Conflict statistics
  totalSignals: int("totalSignals").notNull(), // Total signals from all strategies
  conflictCount: int("conflictCount").notNull(), // Number of times strategies disagreed
  conflictRate: float("conflictRate").notNull(), // % of signals that had conflicts
  
  // Detailed conflict log
  conflicts: json("conflicts").$type<{
    timestamp: string;
    signals: any[];
    resolution: any;
  }[]>(),
  
  // Comparison with individual strategies
  individualResults: json("individualResults").$type<{
    strategyId: number;
    finalBalance: number;
    totalReturn: number;
  }[]>(),
  
  // Detailed data
  trades: json("trades").$type<any[]>(),
  dailyBalances: json("dailyBalances").$type<any[]>(),
  
  createdAt: timestamp("createdAt").defaultNow().notNull(),
});

export type ComboResult = typeof comboResults.$inferSelect;
export type InsertComboResult = typeof comboResults.$inferInsert;

/**
 * API call logs - track all Capital.com API interactions
 */
export const apiCallLogs = mysqlTable("apiCallLogs", {
  id: int("id").autoincrement().primaryKey(),
  
  // Call identification
  callId: varchar("callId", { length: 100 }).notNull(),
  accountId: int("accountId"), // NULL for system-level calls
  botId: int("botId"), // NULL if not bot-related
  
  // Request details
  endpoint: varchar("endpoint", { length: 255 }).notNull(),
  method: varchar("method", { length: 10 }).notNull(), // GET, POST, DELETE, etc.
  priority: mysqlEnum("priority", ["critical", "high", "medium", "low"]).notNull(),
  
  // Response details
  statusCode: int("statusCode"),
  success: boolean("success").notNull(),
  rateLimited: boolean("rateLimited").default(false).notNull(),
  retryCount: int("retryCount").default(0).notNull(),
  durationMs: int("durationMs").notNull(),
  
  // Error information
  error: text("error"),
  errorDetails: json("errorDetails").$type<Record<string, any>>(),
  
  // Timing
  timestamp: timestamp("timestamp").defaultNow().notNull(),
});

export type ApiCallLog = typeof apiCallLogs.$inferSelect;
export type InsertApiCallLog = typeof apiCallLogs.$inferInsert;

/**
 * Market timing configurations - per epic timing settings
 */
export const marketTimings = mysqlTable("marketTimings", {
  id: int("id").autoincrement().primaryKey(),
  epic: varchar("epic", { length: 50 }).notNull().unique(),
  
  // Market hours
  marketOpenTime: varchar("marketOpenTime", { length: 8 }).notNull(), // HH:MM:SS
  marketCloseTime: varchar("marketCloseTime", { length: 8 }).notNull(), // HH:MM:SS
  timezone: varchar("timezone", { length: 50 }).default("America/New_York").notNull(),
  
  // Trading timing offsets (seconds before market close)
  closeTradesOffset: int("closeTradesOffset").default(30).notNull(), // Close all trades
  calculateOffset: int("calculateOffset").default(120).notNull(), // Brain calculation
  openTradesOffset: int("openTradesOffset").default(15).notNull(), // Open new trades
  
  // Timing mode
  timingMode: varchar("timingMode", { length: 50 }).default("ManusTime").notNull(), // ManusTime, OriginalBotTime, Custom
  
  // Custom timing (if timingMode = Custom)
  customTimingConfig: json("customTimingConfig").$type<{
    entryTime?: string; // Specific time or offset
    exitTime?: string;
    calculationTime?: string;
  }>(),
  
  // Metadata
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
});

export type MarketTiming = typeof marketTimings.$inferSelect;
export type InsertMarketTiming = typeof marketTimings.$inferInsert;



/**
 * Application settings - categorized configuration
 */
export const settings = mysqlTable("settings", {
  id: int("id").autoincrement().primaryKey(),
  category: varchar("category", { length: 50 }).notNull(), // e.g., "credentials", "bot_config", "timers"
  key: varchar("key", { length: 100 }).notNull(), // e.g., "api_key", "leverage", "keepalive_minutes"
  value: text("value").notNull(), // Stored as string, parsed as needed
  valueType: mysqlEnum("valueType", ["string", "number", "boolean", "json"]).default("string").notNull(),
  description: text("description"), // Human-readable description
  isSecret: boolean("isSecret").default(false).notNull(), // Whether to mask in UI
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
  createdAt: timestamp("createdAt").defaultNow().notNull(),
}, (table) => ({
  // CRITICAL: Unique constraint on category+key for upsert to work properly
  uniqueCategoryKey: uniqueIndex("idx_settings_unique").on(table.category, table.key),
}));

export type Setting = typeof settings.$inferSelect;
export type InsertSetting = typeof settings.$inferInsert;

/**
 * Indicator Parameters - configurable parameter ranges for backtesting
 * Stores min/max/step values for each indicator's parameters
 */
export const indicatorParams = mysqlTable("indicatorParams", {
  id: int("id").autoincrement().primaryKey(),
  indicatorName: varchar("indicatorName", { length: 100 }).notNull(), // e.g., "rsi_oversold", "macd_positive"
  paramName: varchar("paramName", { length: 100 }).notNull(), // e.g., "period", "threshold"
  displayName: varchar("displayName", { length: 100 }), // Human-readable name for UI
  minValue: float("minValue").notNull(), // Minimum value in range (or min for list display)
  maxValue: float("maxValue").notNull(), // Maximum value in range (or max for list display)
  stepValue: float("stepValue").notNull(), // Step increment (0 for list type)
  defaultValue: float("defaultValue").notNull(), // Default value when not varied
  discreteValues: text("discreteValues"), // JSON array for list type params e.g., "[1, 2, 3, 4, 5, 10, 20]"
  description: text("description"), // Explanation of what the parameter does
  paramType: mysqlEnum("paramType", ["int", "float", "list"]).default("float").notNull(), // int=round, float=decimal, list=discrete values
  isEnabled: boolean("isEnabled").default(true).notNull(), // Whether to include in backtest variations
  sortOrder: int("sortOrder").default(0).notNull(), // Order within indicator
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
  createdAt: timestamp("createdAt").defaultNow().notNull(),
}, (table) => ({
  uniqueParam: index("unique_indicator_param").on(table.indicatorName, table.paramName),
  idxIndicator: index("idx_indicator_name").on(table.indicatorName),
}));

export type IndicatorParam = typeof indicatorParams.$inferSelect;
export type InsertIndicatorParam = typeof indicatorParams.$inferInsert;

/**
 * Strategy test results - track individual strategy test executions
 */
export const strategyTestResults = mysqlTable("strategyTestResults", {
  id: int("id").autoincrement().primaryKey(),
  userId: int("userId").notNull(),
  strategyId: int("strategyId").notNull(), // Reference to bots table
  
  // Test configuration
  epic: varchar("epic", { length: 50 }).notNull(),
  startDate: varchar("startDate", { length: 10 }).notNull(),
  endDate: varchar("endDate", { length: 10 }).notNull(),
  
  // Financial settings
  initialBalance: float("initialBalance").notNull(),
  monthlyTopup: float("monthlyTopup").notNull(),
  
  // Strategy configuration snapshot
  conflictResolution: varchar("conflictResolution", { length: 50 }).notNull(),
  testCount: int("testCount").notNull(), // Number of tests in strategy
  
  // Results
  finalBalance: float("finalBalance").notNull(),
  totalContributions: float("totalContributions").notNull(),
  totalReturn: float("totalReturn").notNull(), // Percentage
  
  // Trade statistics
  totalTrades: int("totalTrades").notNull(),
  winningTrades: int("winningTrades").notNull(),
  losingTrades: int("losingTrades").notNull(),
  winRate: float("winRate").notNull(),
  
  // Risk metrics
  maxDrawdown: float("maxDrawdown").notNull(),
  sharpeRatio: float("sharpeRatio").notNull(),
  
  // Detailed data
  trades: json("trades").$type<any[]>(), // Full trade list
  dailyBalances: json("dailyBalances").$type<any[]>(), // Daily balance history
  
  // Execution metadata
  calculationMode: varchar("calculationMode", { length: 20 }).notNull().default('standard'),
  executionTimeMs: int("executionTimeMs"), // How long the test took
  
  createdAt: timestamp("createdAt").defaultNow().notNull(),
});

export type StrategyTestResult = typeof strategyTestResults.$inferSelect;
export type InsertStrategyTestResult = typeof strategyTestResults.$inferInsert;

// ============================================================================
// Bot Orchestration System
// ============================================================================

/**
 * Trades - stores all executed trades with validation status
 */
export const trades = mysqlTable("trades", {
  id: int("id").autoincrement().primaryKey(),
  accountId: int("accountId").notNull(), // References accounts.id
  strategyId: int("strategyId").notNull(), // References strategyCombos.id
  
  // Trade details
  epic: varchar("epic", { length: 50 }).notNull(),
  direction: mysqlEnum("direction", ["BUY", "SELL"]).notNull(),
  size: float("size").notNull(), // Number of contracts
  entryPrice: float("entryPrice").notNull(),
  exitPrice: float("exitPrice"),
  leverage: float("leverage").notNull(),
  profitLoss: float("profitLoss"),
  
  // Capital.com reference
  dealId: varchar("dealId", { length: 100 }).unique(), // Capital.com deal ID
  
  // Timestamps
  openedAt: timestamp("openedAt").notNull(),
  closedAt: timestamp("closedAt"),
  scheduledCloseTime: timestamp("scheduledCloseTime").notNull(), // When this trade should be closed
  
  // Window metadata (from strategy test configuration)
  entrySeconds: int("entrySeconds").notNull(), // Seconds before close when trade was opened
  exitSeconds: int("exitSeconds").notNull(), // Seconds before close when trade should close
  
  // Validation (compare live trade vs backtest)
  validationStatus: mysqlEnum("validationStatus", ["pending", "validated", "mismatch"]).default("pending").notNull(),
  validationMatchPercent: float("validationMatchPercent"),
  validatedAt: timestamp("validatedAt"),
  
  createdAt: timestamp("createdAt").defaultNow().notNull(),
});

export type Trade = typeof trades.$inferSelect;
export type InsertTrade = typeof trades.$inferInsert;

/**
 * Execution logs - REMOVED
 * Table dropped. All logging now goes to file logs in logs/closing/, logs/brain/, logs/strategy/
 * Kept here as comment for reference only.
 */
// export const executionLogs = mysqlTable("execution_logs", { ... });
// export type ExecutionLog = typeof executionLogs.$inferSelect;
// export type InsertExecutionLog = typeof executionLogs.$inferInsert;

/**
 * Strategy timeslice allocations - defines % allocation per closing time
 * (Used when allocationMode = 'per_timeslice')
 */
export const strategyTimesliceAllocations = mysqlTable("strategy_timeslice_allocations", {
  id: int("id").autoincrement().primaryKey(),
  strategyId: int("strategyId").notNull(), // References strategy_combinations.id
  
  // Time slice configuration
  closingTime: varchar("closingTime", { length: 8 }).notNull(), // e.g., '16:00:00' for 4 PM ET
  allocationPercent: float("allocationPercent").notNull(), // e.g., 50.00 for 50%
  
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
});

export type StrategyTimesliceAllocation = typeof strategyTimesliceAllocations.$inferSelect;
export type InsertStrategyTimesliceAllocation = typeof strategyTimesliceAllocations.$inferInsert;

/**
 * Phase executions - track time-based phase execution events
 * Supports T-4min, T-30s, T-15s phases bound to account strategy windows
 */
export const phaseExecutions = mysqlTable("phaseExecutions", {
  id: int("id").autoincrement().primaryKey(),
  
  // References
  accountId: int("accountId").notNull(),
  strategyId: int("strategyId"), // NULL for global epic-level phases
  windowId: varchar("windowId", { length: 100 }), // Window ID from account.windowConfig
  epicSymbol: varchar("epicSymbol", { length: 50 }).notNull(),
  
  // Phase information
  phase: mysqlEnum("phase", ["T-4min", "T-30s", "T-15s"]).notNull(),
  marketOpenTime: timestamp("marketOpenTime").notNull(), // The target market open time
  scheduledTime: timestamp("scheduledTime").notNull(), // When this phase should execute
  executedTime: timestamp("executedTime"), // When it actually executed (NULL if not yet executed)
  
  // Execution details
  action: varchar("action", { length: 255 }).notNull(), // Description of action taken
  status: mysqlEnum("status", ["scheduled", "executing", "completed", "failed", "skipped"]).default("scheduled").notNull(),
  result: json("result").$type<Record<string, any>>(), // Execution result data
  errorMessage: text("errorMessage"),
  
  // Metadata
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
}, (table) => ({
  // Index for querying upcoming phases
  scheduledTimeIdx: index("scheduled_time_idx").on(table.scheduledTime),
  // Index for querying by account and status
  accountStatusIdx: index("account_status_idx").on(table.accountId, table.status),
}));

export type PhaseExecution = typeof phaseExecutions.$inferSelect;
export type InsertPhaseExecution = typeof phaseExecutions.$inferInsert;

/**
 * Phase configurations - define actions for each phase per strategy
 */
export const phaseConfigurations = mysqlTable("phaseConfigurations", {
  id: int("id").autoincrement().primaryKey(),
  
  // References
  strategyId: int("strategyId").notNull(), // References strategyCombos.id
  accountId: int("accountId"), // NULL for strategy template, set when assigned to account
  
  // Phase settings
  phase: mysqlEnum("phase", ["T-4min", "T-30s", "T-15s"]).notNull(),
  
  // Action configuration
  actionType: varchar("actionType", { length: 100 }).notNull(), // e.g., "fetch_data", "calculate_signals", "place_orders"
  actionConfig: json("actionConfig").$type<Record<string, any>>().notNull(), // Configuration for this action
  
  // Execution settings
  isEnabled: boolean("isEnabled").default(true).notNull(),
  priority: int("priority").default(0).notNull(), // Higher priority executes first
  
  // Metadata
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
}, (table) => ({
  // Unique constraint: one config per strategy/phase/action
  uniqueConfig: index("unique_config_idx").on(table.strategyId, table.phase, table.actionType),
}));

export type PhaseConfiguration = typeof phaseConfigurations.$inferSelect;
export type InsertPhaseConfiguration = typeof phaseConfigurations.$inferInsert;

/**
 * Market schedules - track market open/close times for epics
 * Used by time checker service to calculate phase timings
 */
export const marketSchedules = mysqlTable("marketSchedules", {
  id: int("id").autoincrement().primaryKey(),
  
  // Epic reference
  epicSymbol: varchar("epicSymbol", { length: 50 }).notNull(),
  
  // Schedule information
  date: date("date").notNull(), // Trading date (YYYY-MM-DD)
  marketOpenTime: timestamp("marketOpenTime").notNull(), // Market open time (UTC)
  marketCloseTime: timestamp("marketCloseTime").notNull(), // Market close time (UTC)
  timezone: varchar("timezone", { length: 50 }).notNull(), // e.g., "America/New_York"
  
  // Market status
  isHoliday: boolean("isHoliday").default(false).notNull(),
  isEarlyClose: boolean("isEarlyClose").default(false).notNull(),
  notes: text("notes"), // e.g., "Thanksgiving - Early close at 1 PM ET"
  
  // Metadata
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
}, (table) => ({
  // Unique constraint: one schedule per epic per date
  uniqueSchedule: index("unique_schedule_idx").on(table.epicSymbol, table.date),
  // Index for querying by date range
  dateIdx: index("date_idx").on(table.date),
}));

export type MarketSchedule = typeof marketSchedules.$inferSelect;
export type InsertMarketSchedule = typeof marketSchedules.$inferInsert;

/**
 * Tested Hashes - Global deduplication table for ALL backtest parameter combinations
 * 
 * Purpose:
 * 1. NEVER re-run the same test twice (even across different runs)
 * 2. Track P&L so we can skip saving unprofitable results to backtestResults
 * 3. Configurable threshold via minProfitThreshold setting
 * 
 * Flow:
 * - Before running a test: Check if hash exists in testedHashes
 *   - If exists: Skip test entirely (already tested)
 * - After running a test: Save hash + P&L to testedHashes
 *   - If P&L >= minProfitThreshold: Also save full result to backtestResults
 *   - If P&L < minProfitThreshold: Only save hash (not full result)
 */
export const testedHashes = mysqlTable('testedHashes', {
  id: int("id").autoincrement().primaryKey(),
  
  // The hash of all test parameters (unique identifier)
  paramHash: varchar("paramHash", { length: 64 }).notNull().unique(),
  
  // P&L result (to decide if we need to save full result)
  totalReturn: float("totalReturn").notNull(),  // Percentage return
  
  // Quick reference info (no need to query backtestResults)
  epic: varchar("epic", { length: 20 }).notNull(),
  indicatorName: varchar("indicatorName", { length: 100 }).notNull(),
  leverage: float("leverage").notNull(),
  stopLoss: float("stopLoss").notNull(),
  
  // Hold Means Hold (HMH) flags
  hmhEnabled: boolean("hmhEnabled").default(false),
  hmhStopLossOffset: float("hmhStopLossOffset"),
  
  // Link to full result (NULL if unprofitable and not saved)
  resultId: int("resultId"),
  
  // Which run first tested this combination
  firstRunId: int("firstRunId").notNull(),
  
  // Timestamps
  createdAt: timestamp("createdAt").defaultNow().notNull(),
}, (table) => ({
  idxHash: index("idx_hash").on(table.paramHash),
  idxTotalReturn: index("idx_totalReturn").on(table.totalReturn),
  idxEpic: index("idx_epic").on(table.epic),
  idxIndicator: index("idx_indicator").on(table.indicatorName),
}));

export type TestedHash = typeof testedHashes.$inferSelect;
export type InsertTestedHash = typeof testedHashes.$inferInsert;
