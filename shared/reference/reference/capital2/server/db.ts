import { eq, inArray, and, or, desc, asc, sql, lt, gt, gte, lte } from "drizzle-orm";
import { drizzle } from "drizzle-orm/mysql2";
import { 
  InsertUser, users, 
  StrategyTemplate, InsertStrategyTemplate, strategyTemplates,
  Account, InsertAccount, accounts,
  BacktestRun, InsertBacktestRun, backtestRuns,
  BacktestResult, InsertBacktestResult, backtestResults,
  // NEW: actual_trades replaces liveTrades
  ActualTrade, InsertActualTrade, actualTrades,
  ValidatedTrade, InsertValidatedTrade, validatedTrades,
  TradeComparison, InsertTradeComparison, tradeComparisons,
  BotLog, InsertBotLog, botLogs,
  Epic, InsertEpic, epics,
  MarketInfo, InsertMarketInfo, marketInfo,
  Setting, InsertSetting, settings,
  StrategyTestResult, InsertStrategyTestResult, strategyTestResults,
  candles,
  // Backtest Queue for crash recovery
  BacktestQueueItem, InsertBacktestQueueItem, backtestQueue,
  // Indicator Parameters for backtesting
  IndicatorParam, InsertIndicatorParam, indicatorParams,
  // Account Transactions for balance tracking
  AccountTransaction, InsertAccountTransaction, accountTransactions,
  // VERSION 3: Trust Matrix for signal-based trading
  indicatorPairTrust,
  savedStrategies,
  // Tested Hashes for global deduplication
  TestedHash, InsertTestedHash, testedHashes,
} from "../drizzle/schema";

// Extended type for strategy test results with computed fields
export type StrategyTestResultWithExtras = StrategyTestResult & {
  strategyName: string | null;
  totalCosts: number;
  crashProtectionEnabled?: boolean;
};
import { ENV } from './_core/env';

let _db: ReturnType<typeof drizzle> | null = null;

// Lazily create the drizzle instance so local tooling can run without a DB.
export async function getDb() {
  if (!_db && process.env.DATABASE_URL) {
    try {
      // Configure MySQL connection pool with SSL handling
      const mysql2 = await import('mysql2/promise');
      const pool = mysql2.createPool({
        uri: process.env.DATABASE_URL,
        ssl: {
          rejectUnauthorized: false // Allow self-signed certificates
        },
        waitForConnections: true,
        // HIGH LIMIT: Brain calculations are burst operations (once per window close)
        // No need to be conservative - let MySQL handle max connections
        // PlanetScale free tier: 1000 connections, paid: 10000+
        connectionLimit: 100,
        queueLimit: 0, // 0 = unlimited queue (wait for connection if all busy)
        // CRITICAL: Force UTC timezone for all connections
        // Without this, timestamps are interpreted as server local time (UTC+4)
        timezone: '+00:00',
      });
      _db = drizzle(pool);
    } catch (error) {
      console.warn("[Database] Failed to connect:", error);
      _db = null;
    }
  }
  return _db;
}

// ============================================================================
// User Management
// ============================================================================

export async function upsertUser(user: InsertUser): Promise<void> {
  if (!user.openId) {
    throw new Error("User openId is required for upsert");
  }

  const db = await getDb();
  if (!db) {
    console.warn("[Database] Cannot upsert user: database not available");
    return;
  }

  try {
    const values: InsertUser = {
      openId: user.openId,
    };
    const updateSet: Record<string, unknown> = {};

    const textFields = ["name", "email", "loginMethod"] as const;
    type TextField = (typeof textFields)[number];

    const assignNullable = (field: TextField) => {
      const value = user[field];
      if (value === undefined) return;
      const normalized = value ?? null;
      values[field] = normalized;
      updateSet[field] = normalized;
    };

    textFields.forEach(assignNullable);

    if (user.lastSignedIn !== undefined) {
      values.lastSignedIn = user.lastSignedIn;
      updateSet.lastSignedIn = user.lastSignedIn;
    }
    if (user.role !== undefined) {
      values.role = user.role;
      updateSet.role = user.role;
    } else if (user.openId === ENV.ownerId) {
      values.role = 'admin';
      updateSet.role = 'admin';
    }

    if (!values.lastSignedIn) {
      values.lastSignedIn = new Date();
    }

    if (Object.keys(updateSet).length === 0) {
      updateSet.lastSignedIn = new Date();
    }

    await db.insert(users).values(values).onDuplicateKeyUpdate({
      set: updateSet,
    });
  } catch (error) {
    console.error("[Database] Failed to upsert user:", error);
    throw error;
  }
}

export async function getUser(openId: string) {
  const db = await getDb();
  if (!db) {
    console.warn("[Database] Cannot get user: database not available");
    return undefined;
  }

  const result = await db.select().from(users).where(eq(users.openId, openId)).limit(1);

  return result.length > 0 ? result[0] : undefined;
}

// ============================================================================
// Bot Management
// ============================================================================

export async function createBot(bot: InsertStrategyTemplate): Promise<number> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(strategyTemplates).values(bot);
  return result[0].insertId;
}

export async function getUserBots(userId: number): Promise<StrategyTemplate[]> {
  const db = await getDb();
  if (!db) return [];

  return await db.select().from(strategyTemplates).where(eq(strategyTemplates.userId, userId));
}

export async function getBot(botId: number): Promise<StrategyTemplate | undefined> {
  const db = await getDb();
  if (!db) return undefined;

  const result = await db.select().from(strategyTemplates).where(eq(strategyTemplates.id, botId)).limit(1);
  return result.length > 0 ? result[0] : undefined;
}

export async function updateBot(botId: number, updates: Partial<InsertStrategyTemplate>): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.update(strategyTemplates).set(updates).where(eq(strategyTemplates.id, botId));
}

export async function deleteBot(botId: number): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.delete(strategyTemplates).where(eq(strategyTemplates.id, botId));
}

// ============================================================================
// Account Management
// ============================================================================

export async function createAccount(account: InsertAccount): Promise<number> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(accounts).values(account);
  return result[0].insertId;
}

export async function getUserAccounts(userId: number): Promise<Account[]> {
  const db = await getDb();
  if (!db) return [];

  return await db.select().from(accounts).where(eq(accounts.userId, userId));
}

export async function getAccount(accountId: number): Promise<Account | undefined> {
  const db = await getDb();
  if (!db) return undefined;

  const result = await db.select().from(accounts).where(eq(accounts.id, accountId)).limit(1);
  return result.length > 0 ? result[0] : undefined;
}

export async function updateAccount(accountId: number, updates: Partial<InsertAccount>): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.update(accounts).set(updates).where(eq(accounts.id, accountId));
}

export async function assignBotToAccount(accountId: number, botId: number | null): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  // If removing strategy (botId is null), also deactivate the account
  // An account cannot be active without a strategy
  if (botId === null) {
    await db.update(accounts).set({ 
      assignedStrategyId: null,
      botStatus: 'stopped',
      isActive: false,
      stoppedAt: new Date(),
      updatedAt: new Date(),
    }).where(eq(accounts.id, accountId));
    console.log(`[DB] Account ${accountId} deactivated - strategy removed`);
  } else {
    await db.update(accounts).set({ assignedStrategyId: botId }).where(eq(accounts.id, accountId));
  }
}

// ============================================================================
// Backtest Management
// ============================================================================

export async function createBacktestRun(run: InsertBacktestRun): Promise<number> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(backtestRuns).values(run);
  return result[0].insertId;
}

export async function getBacktestRun(runId: number): Promise<BacktestRun | undefined> {
  const db = await getDb();
  if (!db) return undefined;

  const result = await db.select().from(backtestRuns).where(eq(backtestRuns.id, runId)).limit(1);
  return result.length > 0 ? result[0] : undefined;
}

/**
 * Get the best result for a run (highest finalBalance)
 * Used when stopping a run to roll up the best result
 */
export async function getBestResultForRun(runId: number): Promise<{
  indicatorName: string;
  finalBalance: number;
  totalReturn: number;
  winRate: number;
  sharpeRatio: number;
} | null> {
  const db = await getDb();
  if (!db) return null;

  const result = await db
    .select({
      indicatorName: backtestResults.indicatorName,
      finalBalance: backtestResults.finalBalance,
      totalReturn: backtestResults.totalReturn,
      winRate: backtestResults.winRate,
      sharpeRatio: backtestResults.sharpeRatio,
    })
    .from(backtestResults)
    .where(eq(backtestResults.runId, runId))
    .orderBy(desc(backtestResults.finalBalance))
    .limit(1);

  return result.length > 0 ? result[0] : null;
}

export async function getActiveBacktestRuns(userId: number): Promise<BacktestRun[]> {
  const db = await getDb();
  if (!db) return [];

  // Get runs that are running or paused
  const result = await db
    .select()
    .from(backtestRuns)
    .where(
      and(
        eq(backtestRuns.userId, userId),
        or(
          eq(backtestRuns.status, 'running'),
          eq(backtestRuns.status, 'paused')
        )
      )
    )
    .orderBy(backtestRuns.createdAt);
  
  return result;
}

export async function recoverStuckBacktests(): Promise<number> {
  const db = await getDb();
  if (!db) return 0;

  try {
    // Import the running backtests tracker from backtest_bridge_v2
    const { runningBacktests } = await import('./backtest_bridge_v2');
    
    // Find all backtests stuck in 'running' status
    // (These are from server crashes/restarts)
    const potentiallyStuckRuns = await db
      .select()
      .from(backtestRuns)
      .where(eq(backtestRuns.status, 'running'));

    if (potentiallyStuckRuns.length === 0) {
      console.log('[Recovery] No stuck backtests found');
      return 0;
    }

    // Filter out runs that are actually still running
    // Check both in-memory map AND system processes
    const { execSync } = await import('child_process');
    const stuckRuns = potentiallyStuckRuns.filter(run => {
      // Check in-memory map first
      if (runningBacktests.has(run.id)) {
        return false;
      }
      
      // Check if Python process is actually running
      try {
        const psOutput = execSync(`ps aux | grep "batch_runner.py.*${run.id}" | grep -v grep`, { encoding: 'utf-8' });
        if (psOutput.trim().length > 0) {
          console.log(`[Recovery] Run #${run.id} has active Python process, not pausing`);
          return false; // Process is running, not stuck
        }
      } catch (error) {
        // ps command returns non-zero if no match found, which is expected
      }
      
      return true; // No in-memory record AND no Python process = stuck
    });
    
    if (stuckRuns.length === 0) {
      console.log('[Recovery] No stuck backtests found (all are actively running)');
      return 0;
    }

    console.log(`[Recovery] Found ${stuckRuns.length} stuck backtest(s), marking as paused...`);

    // Mark them as paused so they can be resumed
    for (const run of stuckRuns) {
      await db
        .update(backtestRuns)
        .set({
          status: 'paused',
          pausedAt: new Date(),
        })
        .where(eq(backtestRuns.id, run.id));
      
      console.log(`[Recovery] Paused run #${run.id} (${run.epic})`);
    }

    return stuckRuns.length;
  } catch (error) {
    console.error('[Recovery] Error recovering stuck backtests:', error);
    return 0;
  }
}

export async function getUserBacktestRuns(userId: number): Promise<any[]> {
  const db = await getDb();
  if (!db) return [];

  // OPTIMIZED: Single query instead of N+1 queries
  // Previously: 1 query for runs + N queries for results (N+1 problem)
  // Now: 1 query for runs + 1 query for ALL results (2 queries total)
  
  const startTime = Date.now();
  
  // Query 1: Get all runs for the user
  const runs = await db.select().from(backtestRuns).where(eq(backtestRuns.userId, userId));
  
  if (runs.length === 0) return [];
  
  // Query 2: Get ALL results for ALL runs in a single query
  const runIds = runs.map(r => r.id);
  const allResults = await db.select({
    id: backtestResults.id,
    runId: backtestResults.runId,
    epic: backtestResults.epic,
    timeframe: backtestResults.timeframe,
    crashProtectionEnabled: backtestResults.crashProtectionEnabled,
    indicatorName: backtestResults.indicatorName,
    indicatorParams: backtestResults.indicatorParams,
    leverage: backtestResults.leverage,
    stopLoss: backtestResults.stopLoss,
    timingConfig: backtestResults.timingConfig,
    signalBasedConfig: backtestResults.signalBasedConfig,
    dataSource: backtestResults.dataSource,
    paramHash: backtestResults.paramHash,
    initialBalance: backtestResults.initialBalance,
    finalBalance: backtestResults.finalBalance,
    totalContributions: backtestResults.totalContributions,
    totalReturn: backtestResults.totalReturn,
    totalTrades: backtestResults.totalTrades,
    winningTrades: backtestResults.winningTrades,
    losingTrades: backtestResults.losingTrades,
    winRate: backtestResults.winRate,
    maxDrawdown: backtestResults.maxDrawdown,
    sharpeRatio: backtestResults.sharpeRatio,
    totalFees: backtestResults.totalFees,
    totalSpreadCosts: backtestResults.totalSpreadCosts,
    totalOvernightCosts: backtestResults.totalOvernightCosts,
    createdAt: backtestResults.createdAt,
    // Margin level columns for Liqs and ML% display
    liquidationCount: backtestResults.liquidationCount,
    minMarginLevel: backtestResults.minMarginLevel,
    // Excluded: trades, dailyBalances (heavy JSON arrays)
  }).from(backtestResults).where(inArray(backtestResults.runId, runIds));
  
  // Group results by runId (in-memory, fast)
  const resultsByRunId = new Map<number, typeof allResults>();
  for (const result of allResults) {
    if (!resultsByRunId.has(result.runId)) {
      resultsByRunId.set(result.runId, []);
    }
    resultsByRunId.get(result.runId)!.push(result);
  }
  
  // Combine runs with their results
  const runsWithResults = runs.map(run => ({
    ...run,
    results: resultsByRunId.get(run.id) || []
  }));
  
  const duration = Date.now() - startTime;
  console.log(`[DB] getUserBacktestRuns: ${runs.length} runs, ${allResults.length} results in ${duration}ms (2 queries)`);
  
  return runsWithResults;
}

/**
 * Get paginated backtest results for the results history page
 * Returns lightweight results without trades/dailyBalances arrays
 * Supports server-side sorting and filtering for fast loading
 */
export interface PaginatedResultsOptions {
  runId?: number;  // Optional: filter to specific run (used by Results page)
  limit?: number;
  offset?: number;
  sortBy?: 'totalReturn' | 'winRate' | 'sharpeRatio' | 'maxDrawdown' | 'totalTrades' | 'finalBalance' | 'createdAt' | 'leverage' | 'indicatorName'
    | 'epic' | 'liquidationCount' | 'minMarginLevel' | 'crashProtectionEnabled' | 'initialBalance' | 'monthlyTopup';  // Additional columns for sorting
  sortDir?: 'asc' | 'desc';
  // Filters
  epic?: string;
  indicatorName?: string;
  minReturn?: number;
  minWinRate?: number;
  minSharpe?: number;
  maxDrawdown?: number;
  minTrades?: number;
  maxLeverage?: number;
  // Liquidation and Margin Level filters
  maxLiquidations?: number;  // Show results with Liqs ≤ this
  minMarginLevel?: number;   // Show results with MinML ≥ this
}

export async function getPaginatedBacktestResults(
  userId: number,
  options: PaginatedResultsOptions = {}
): Promise<{ results: any[]; total: number; hasMore: boolean; queryTimeMs: number }> {
  const startTime = Date.now();
  const db = await getDb();
  if (!db) return { results: [], total: 0, hasMore: false, queryTimeMs: 0 };

  const {
    runId,  // Filter to specific run if provided
    limit = 100,
    offset = 0,
    sortBy = 'totalReturn',
    sortDir = 'desc',
    epic,
    indicatorName,
    minReturn,
    minWinRate,
    minSharpe,
    maxDrawdown,
    minTrades,
    maxLeverage,
  } = options;

  // Build filter conditions
  const conditions: any[] = [];
  
  // =========================================================================
  // GLOBAL FILTER: Only show results above minProfitThreshold setting
  // This ensures the UI only displays profitable results per user's setting
  // =========================================================================
  const minProfitThreshold = await getMinProfitThreshold();
  conditions.push(gte(backtestResults.totalReturn, minProfitThreshold));
  
  // If runId is provided, filter by that specific run (Results page)
  // Otherwise, filter by all runs for this user (ResultsHistory page)
  if (runId) {
    conditions.push(eq(backtestResults.runId, runId));
  } else {
    // Get runs for this user
    const userRuns = await db.select({ id: backtestRuns.id }).from(backtestRuns).where(eq(backtestRuns.userId, userId));
    const runIds = userRuns.map(r => r.id);
    
    if (runIds.length === 0) {
      return { results: [], total: 0, hasMore: false, queryTimeMs: Date.now() - startTime };
    }
    conditions.push(inArray(backtestResults.runId, runIds));
  }
  
  if (epic) {
    conditions.push(eq(backtestResults.epic, epic));
  }
  if (indicatorName) {
    conditions.push(eq(backtestResults.indicatorName, indicatorName));
  }
  if (minReturn !== undefined) {
    conditions.push(sql`${backtestResults.totalReturn} >= ${minReturn}`);
  }
  if (minWinRate !== undefined) {
    conditions.push(sql`${backtestResults.winRate} >= ${minWinRate}`);
  }
  if (minSharpe !== undefined) {
    conditions.push(sql`${backtestResults.sharpeRatio} >= ${minSharpe}`);
  }
  if (maxDrawdown !== undefined) {
    conditions.push(sql`ABS(${backtestResults.maxDrawdown}) <= ${maxDrawdown}`);
  }
  if (minTrades !== undefined) {
    conditions.push(sql`${backtestResults.totalTrades} >= ${minTrades}`);
  }
  if (maxLeverage !== undefined) {
    conditions.push(sql`${backtestResults.leverage} <= ${maxLeverage}`);
  }
  
  // Liquidation and Margin Level filters
  if ((options as any).maxLiquidations !== undefined) {
    conditions.push(sql`COALESCE(${backtestResults.liquidationCount}, 0) <= ${(options as any).maxLiquidations}`);
  }
  if ((options as any).minMarginLevel !== undefined) {
    conditions.push(sql`${backtestResults.minMarginLevel} >= ${(options as any).minMarginLevel}`);
  }

  const whereClause = and(...conditions);

  // Get total count (with filters applied)
  const [countResult] = await db
    .select({ count: sql<number>`COUNT(*)` })
    .from(backtestResults)
    .where(whereClause);
  const total = countResult?.count || 0;

  // Build sort column
  const sortColumns: Record<string, any> = {
    totalReturn: backtestResults.totalReturn,
    winRate: backtestResults.winRate,
    sharpeRatio: backtestResults.sharpeRatio,
    maxDrawdown: backtestResults.maxDrawdown,
    totalTrades: backtestResults.totalTrades,
    finalBalance: backtestResults.finalBalance,
    createdAt: backtestResults.createdAt,
    leverage: backtestResults.leverage,
    indicatorName: backtestResults.indicatorName,
    // Additional columns for VirtualizedHistoryTable sorting
    epic: backtestResults.epic,
    liquidationCount: backtestResults.liquidationCount,
    minMarginLevel: backtestResults.minMarginLevel,
    crashProtectionEnabled: backtestResults.crashProtectionEnabled,
    initialBalance: backtestResults.initialBalance,
    monthlyTopup: sql`0`,  // Not stored in backtestResults, use default
    // Date columns from backtestRuns (joined table)
    startDate: backtestRuns.startDate,
    endDate: backtestRuns.endDate,
    // Stop loss column
    stopLoss: backtestResults.stopLoss,
  };
  const orderByCol = sortColumns[sortBy] || backtestResults.totalReturn;
  const orderByDir = sortDir === 'asc' ? sql`ASC` : sql`DESC`;

  // Get paginated results (lightweight - no trades/dailyBalances)
  // JOIN with backtestRuns to get monthlyTopup, startDate, endDate (stored at run level, not result level)
  const results = await db.select({
    id: backtestResults.id,
    runId: backtestResults.runId,
    epic: backtestResults.epic,
    timeframe: backtestResults.timeframe,
    crashProtectionEnabled: backtestResults.crashProtectionEnabled,
    // HMH (Hold Means Hold) columns
    hmhEnabled: backtestResults.hmhEnabled,
    hmhStopLossOffset: backtestResults.hmhStopLossOffset,
    indicatorName: backtestResults.indicatorName,
    indicatorParams: backtestResults.indicatorParams,
    leverage: backtestResults.leverage,
    stopLoss: backtestResults.stopLoss,
    timingConfig: backtestResults.timingConfig,
    signalBasedConfig: backtestResults.signalBasedConfig,
    dataSource: backtestResults.dataSource,
    paramHash: backtestResults.paramHash,
    initialBalance: backtestResults.initialBalance,
    finalBalance: backtestResults.finalBalance,
    totalContributions: backtestResults.totalContributions,
    totalReturn: backtestResults.totalReturn,
    totalTrades: backtestResults.totalTrades,
    winningTrades: backtestResults.winningTrades,
    losingTrades: backtestResults.losingTrades,
    winRate: backtestResults.winRate,
    maxDrawdown: backtestResults.maxDrawdown,
    sharpeRatio: backtestResults.sharpeRatio,
    totalFees: backtestResults.totalFees,
    totalSpreadCosts: backtestResults.totalSpreadCosts,
    totalOvernightCosts: backtestResults.totalOvernightCosts,
    createdAt: backtestResults.createdAt,
    // Margin level columns for Liqs and ML% display
    liquidationCount: backtestResults.liquidationCount,
    minMarginLevel: backtestResults.minMarginLevel,
    // Backtest type
    backtestType: backtestResults.backtestType,
    // Get monthlyTopup and date range from parent run (not stored in backtestResults)
    monthlyTopup: backtestRuns.monthlyTopup,
    startDate: backtestRuns.startDate,
    endDate: backtestRuns.endDate,
  })
  .from(backtestResults)
  .leftJoin(backtestRuns, eq(backtestResults.runId, backtestRuns.id))
  .where(whereClause)
  .orderBy(sortDir === 'desc' ? desc(orderByCol) : orderByCol)
  .limit(limit)
  .offset(offset);

  const queryTimeMs = Date.now() - startTime;
  console.log(`[DB] getPaginatedBacktestResults: ${results.length}/${total} results in ${queryTimeMs}ms (runId=${runId || 'all'}, offset=${offset}, filters=${conditions.length - 1})`);

  return {
    results,
    total,
    hasMore: offset + results.length < total,
    queryTimeMs
  };
}

/**
 * Get filter metadata for Results History page
 * Returns unique epics, indicators, leverages, and min/max ranges
 * Lightweight query - no full result data
 */
export async function getBacktestFilterMetadata(
  userId: number,
  runId?: number
): Promise<{
  epics: string[];
  indicators: string[];
  leverages: number[];
  ranges: {
    return: { min: number; max: number };
    winRate: { min: number; max: number };
    sharpe: { min: number; max: number };
    drawdown: { min: number; max: number };
    trades: { min: number; max: number };
    balance: { min: number; max: number };
    liquidations: { min: number; max: number };
    marginLevel: { min: number; max: number };
  };
  totalCount: number;
}> {
  const startTime = Date.now();
  const db = await getDb();
  if (!db) {
    return {
      epics: [],
      indicators: [],
      leverages: [],
      ranges: {
        return: { min: -100, max: 500 },
        winRate: { min: 0, max: 100 },
        sharpe: { min: -5, max: 5 },
        drawdown: { min: 0, max: 100 },
        trades: { min: 0, max: 100 },
        balance: { min: 0, max: 10000 },
        liquidations: { min: 0, max: 0 },
        marginLevel: { min: 0, max: 100 },
      },
      totalCount: 0,
    };
  }

  // Build filter conditions
  const conditions: any[] = [];
  
  // =========================================================================
  // GLOBAL FILTER: Only show results above minProfitThreshold setting
  // =========================================================================
  const minProfitThreshold = await getMinProfitThreshold();
  conditions.push(gte(backtestResults.totalReturn, minProfitThreshold));

  if (runId) {
    conditions.push(eq(backtestResults.runId, runId));
  } else {
    // Get runs for this user
    const userRuns = await db.select({ id: backtestRuns.id }).from(backtestRuns).where(eq(backtestRuns.userId, userId));
    const runIds = userRuns.map(r => r.id);
    if (runIds.length === 0) {
      return {
        epics: [],
        indicators: [],
        leverages: [],
        ranges: {
          return: { min: -100, max: 500 },
          winRate: { min: 0, max: 100 },
          sharpe: { min: -5, max: 5 },
          drawdown: { min: 0, max: 100 },
          trades: { min: 0, max: 100 },
          balance: { min: 0, max: 10000 },
          liquidations: { min: 0, max: 0 },
          marginLevel: { min: 0, max: 100 },
        },
        totalCount: 0,
      };
    }
    conditions.push(inArray(backtestResults.runId, runIds));
  }

  const whereClause = conditions.length > 0 ? and(...conditions) : undefined;

  // Get unique epics
  const epicResults = await db.selectDistinct({ epic: backtestResults.epic })
    .from(backtestResults)
    .where(whereClause);
  const epics = epicResults.map(r => r.epic).filter(Boolean).sort();

  // Get unique indicators
  const indicatorResults = await db.selectDistinct({ indicator: backtestResults.indicatorName })
    .from(backtestResults)
    .where(whereClause);
  const indicators = indicatorResults.map(r => r.indicator).filter(Boolean).sort();

  // Get unique leverages
  const leverageResults = await db.selectDistinct({ leverage: backtestResults.leverage })
    .from(backtestResults)
    .where(whereClause);
  const leverages = leverageResults.map(r => r.leverage).filter(Boolean).sort((a, b) => b - a);

  // Get min/max ranges and total count in one query
  const [aggregates] = await db.select({
    count: sql<number>`COUNT(*)`,
    minReturn: sql<number>`MIN(${backtestResults.totalReturn})`,
    maxReturn: sql<number>`MAX(${backtestResults.totalReturn})`,
    minWinRate: sql<number>`MIN(${backtestResults.winRate})`,
    maxWinRate: sql<number>`MAX(${backtestResults.winRate})`,
    minSharpe: sql<number>`MIN(${backtestResults.sharpeRatio})`,
    maxSharpe: sql<number>`MAX(${backtestResults.sharpeRatio})`,
    minDrawdown: sql<number>`MIN(ABS(${backtestResults.maxDrawdown}))`,
    maxDrawdown: sql<number>`MAX(ABS(${backtestResults.maxDrawdown}))`,
    minTrades: sql<number>`MIN(${backtestResults.totalTrades})`,
    maxTrades: sql<number>`MAX(${backtestResults.totalTrades})`,
    minBalance: sql<number>`MIN(${backtestResults.finalBalance})`,
    maxBalance: sql<number>`MAX(${backtestResults.finalBalance})`,
    // Liquidation and Margin Level ranges
    minLiqs: sql<number>`MIN(COALESCE(${backtestResults.liquidationCount}, 0))`,
    maxLiqs: sql<number>`MAX(COALESCE(${backtestResults.liquidationCount}, 0))`,
    minML: sql<number>`MIN(${backtestResults.minMarginLevel})`,
    maxML: sql<number>`MAX(${backtestResults.minMarginLevel})`,
  }).from(backtestResults).where(whereClause);

  const queryTimeMs = Date.now() - startTime;
  console.log(`[DB] getBacktestFilterMetadata: ${epics.length} epics, ${indicators.length} indicators, ${aggregates?.count || 0} total in ${queryTimeMs}ms`);

  return {
    epics,
    indicators,
    leverages,
    ranges: {
      return: { 
        min: Math.floor(aggregates?.minReturn ?? -100), 
        max: Math.ceil(aggregates?.maxReturn ?? 500) 
      },
      winRate: { 
        min: Math.floor(aggregates?.minWinRate ?? 0), 
        max: Math.ceil(aggregates?.maxWinRate ?? 100) 
      },
      sharpe: { 
        min: Math.floor((aggregates?.minSharpe ?? -5) * 10) / 10, 
        max: Math.ceil((aggregates?.maxSharpe ?? 5) * 10) / 10 
      },
      drawdown: { 
        min: 0, 
        max: Math.ceil(aggregates?.maxDrawdown ?? 100) 
      },
      trades: { 
        min: 0, 
        max: Math.ceil(aggregates?.maxTrades ?? 100) 
      },
      balance: { 
        min: 0, 
        max: Math.ceil(aggregates?.maxBalance ?? 10000) 
      },
      // Liquidation and Margin Level ranges
      liquidations: {
        min: aggregates?.minLiqs ?? 0,
        max: aggregates?.maxLiqs ?? 0,
      },
      marginLevel: {
        min: Math.floor(aggregates?.minML ?? 0),
        max: Math.ceil(aggregates?.maxML ?? 100),
      },
    },
    totalCount: aggregates?.count ?? 0,
  };
}

export async function updateBacktestRun(runId: number, updates: Partial<InsertBacktestRun>): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.update(backtestRuns).set(updates).where(eq(backtestRuns.id, runId));
}

/**
 * Check if any backtest results from a run are used in strategy DNA strands.
 * Returns list of strategies that reference results from this run.
 */
export async function getStrategiesUsingRunResults(runId: number): Promise<Array<{
  strategyId: number;
  strategyName: string;
  usedTestIds: number[];
}>> {
  const db = await getDb();
  if (!db) return [];

  // Get all result IDs for this run
  const runResults = await db
    .select({ id: backtestResults.id })
    .from(backtestResults)
    .where(eq(backtestResults.runId, runId));
  
  if (runResults.length === 0) return [];
  
  const resultIds = new Set(runResults.map(r => r.id));
  
  // Get all strategies and check their dnaStrands
  const strategies = await db.select().from(savedStrategies);
  
  const usingStrategies: Array<{
    strategyId: number;
    strategyName: string;
    usedTestIds: number[];
  }> = [];
  
  for (const strategy of strategies) {
    try {
      const dnaStrands = strategy.dnaStrands as Array<{ sourceTestId?: number }> | null;
      if (!dnaStrands || !Array.isArray(dnaStrands)) continue;
      
      const usedTestIds = dnaStrands
        .filter(dna => dna.sourceTestId && resultIds.has(dna.sourceTestId))
        .map(dna => dna.sourceTestId as number);
      
      if (usedTestIds.length > 0) {
        usingStrategies.push({
          strategyId: strategy.id,
          strategyName: strategy.name,
          usedTestIds,
        });
      }
    } catch (e) {
      // Skip strategies with invalid JSON
      continue;
    }
  }
  
  return usingStrategies;
}

/**
 * Delete a backtest run and all its results.
 * 
 * PROTECTION: Will throw an error if any results are used in strategy DNA strands.
 * User must remove results from strategies first before deleting.
 * 
 * @param runId - The run ID to delete
 * @param force - If true, skip protection check (dangerous!)
 * @throws Error if results are used in strategies (unless force=true)
 */
/**
 * Get all protected test IDs (results used in any strategy DNA)
 */
export async function getProtectedTestIds(): Promise<Set<number>> {
  const db = await getDb();
  if (!db) return new Set();
  
  const strategies = await db.select().from(savedStrategies);
  const protectedIds = new Set<number>();
  
  for (const strategy of strategies) {
    try {
      const dnaStrands = strategy.dnaStrands as Array<{ sourceTestId?: number }> | null;
      if (!dnaStrands || !Array.isArray(dnaStrands)) continue;
      
      for (const dna of dnaStrands) {
        if (dna.sourceTestId) {
          protectedIds.add(dna.sourceTestId);
        }
      }
    } catch (e) {
      continue;
    }
  }
  
  return protectedIds;
}

/**
 * Delete a backtest run with different modes:
 * - 'check': Return info about protected results (don't delete anything)
 * - 'except_protected': Delete all results EXCEPT those used in strategies
 * - 'force': Delete everything including strategy-linked results
 * 
 * @returns Object with deletion stats
 */
export async function deleteBacktestRun(
  runId: number, 
  mode: 'check' | 'except_protected' | 'force' = 'check'
): Promise<{
  deleted: boolean;
  totalResults: number;
  deletedResults: number;
  protectedResults: number;
  protectedTestIds: number[];
  strategies: Array<{ strategyId: number; strategyName: string; usedTestIds: number[] }>;
  runDeleted: boolean;
}> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  // Get all results for this run
  const runResults = await db
    .select({ id: backtestResults.id })
    .from(backtestResults)
    .where(eq(backtestResults.runId, runId));
  
  const totalResults = runResults.length;
  const resultIds = new Set(runResults.map(r => r.id));
  
  // Get protected test IDs (used in strategies)
  const allProtectedIds = await getProtectedTestIds();
  
  // Find which of THIS run's results are protected
  const protectedTestIds = runResults
    .filter(r => allProtectedIds.has(r.id))
    .map(r => r.id);
  
  const protectedResults = protectedTestIds.length;
  const deletableResults = totalResults - protectedResults;
  
  // Get strategies using this run's results
  const strategies = await getStrategiesUsingRunResults(runId);
  
  // If just checking, return info
  if (mode === 'check') {
    return {
      deleted: false,
      totalResults,
      deletedResults: 0,
      protectedResults,
      protectedTestIds,
      strategies,
      runDeleted: false,
    };
  }
  
  // Delete queue entries for this run
  await db.delete(backtestQueue).where(eq(backtestQueue.runId, runId));
  console.log(`[DB] Deleted queue entries for run ${runId}`);
  
  if (mode === 'force') {
    // Delete ALL results (including protected)
    await db.delete(backtestResults).where(eq(backtestResults.runId, runId));
    console.log(`[DB] Force deleted ALL ${totalResults} results for run ${runId}`);
    
    // Delete the run itself
    await db.delete(backtestRuns).where(eq(backtestRuns.id, runId));
    console.log(`[DB] Deleted backtest run ${runId}`);
    
    return {
      deleted: true,
      totalResults,
      deletedResults: totalResults,
      protectedResults: 0, // All deleted
      protectedTestIds: [],
      strategies,
      runDeleted: true,
    };
  }
  
  if (mode === 'except_protected') {
    // Delete only non-protected results
    const idsToDelete = runResults
      .filter(r => !allProtectedIds.has(r.id))
      .map(r => r.id);
    
    if (idsToDelete.length > 0) {
      await db.delete(backtestResults).where(inArray(backtestResults.id, idsToDelete));
      console.log(`[DB] Deleted ${idsToDelete.length} unprotected results for run ${runId}, kept ${protectedResults} protected`);
    }
    
    // Only delete the run if ALL results were deleted (none protected)
    const runDeleted = protectedResults === 0;
    if (runDeleted) {
      await db.delete(backtestRuns).where(eq(backtestRuns.id, runId));
      console.log(`[DB] Deleted backtest run ${runId} (no protected results)`);
    } else {
      // Update the run to reflect reduced result count
      console.log(`[DB] Kept backtest run ${runId} (has ${protectedResults} protected results in strategies)`);
    }
    
    return {
      deleted: true,
      totalResults,
      deletedResults: idsToDelete.length,
      protectedResults,
      protectedTestIds,
      strategies,
      runDeleted,
    };
  }
  
  throw new Error(`Invalid delete mode: ${mode}`);
}

export async function createBacktestResult(result: InsertBacktestResult): Promise<number> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const insertResult = await db.insert(backtestResults).values(result);
  return insertResult[0].insertId;
}

/**
 * Batch insert multiple backtest results in a single transaction.
 * 
 * This is MUCH faster than individual inserts:
 * - 100 individual inserts: ~5-10 seconds
 * - 1 batch insert with 100 results: ~0.1-0.2 seconds
 * 
 * @param results Array of results to insert
 * @returns Number of results inserted
 */
export async function createBacktestResultsBatch(results: InsertBacktestResult[]): Promise<number> {
  if (results.length === 0) return 0;
  
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  try {
    await db.insert(backtestResults).values(results);
    console.log(`[DB] Batch inserted ${results.length} backtest results`);
    return results.length;
  } catch (error) {
    console.error(`[DB] Batch insert failed:`, error);
    throw error;
  }
}

/**
 * Get backtest results for a run - EXCLUDES trades/dailyBalances for performance
 * Use getBacktestResultFull() when you need the full trade data for detail views
 * 
 * FILTER: Only returns results above minProfitThreshold setting to keep UI clean
 * Unprofitable results are still saved (for hash deduplication) but not displayed
 */
export async function getBacktestResults(runId: number): Promise<Omit<BacktestResult, 'trades' | 'dailyBalances'>[]> {
  const db = await getDb();
  if (!db) return [];

  // Get threshold from settings
  const minProfitThreshold = await getMinProfitThreshold();

  // Select all columns EXCEPT trades and dailyBalances (which can be 100KB+ each)
  // FILTER: Only return results >= minProfitThreshold setting
  return await db.select({
    id: backtestResults.id,
    runId: backtestResults.runId,
    epic: backtestResults.epic,
    timeframe: backtestResults.timeframe,
    crashProtectionEnabled: backtestResults.crashProtectionEnabled,
    // HMH (Hold Means Hold) columns
    hmhEnabled: backtestResults.hmhEnabled,
    hmhStopLossOffset: backtestResults.hmhStopLossOffset,
    indicatorName: backtestResults.indicatorName,
    indicatorParams: backtestResults.indicatorParams,
    leverage: backtestResults.leverage,
    stopLoss: backtestResults.stopLoss,
    timingConfig: backtestResults.timingConfig,
    signalBasedConfig: backtestResults.signalBasedConfig,
    dataSource: backtestResults.dataSource,
    paramHash: backtestResults.paramHash,
    initialBalance: backtestResults.initialBalance,
    finalBalance: backtestResults.finalBalance,
    totalContributions: backtestResults.totalContributions,
    totalReturn: backtestResults.totalReturn,
    totalTrades: backtestResults.totalTrades,
    winningTrades: backtestResults.winningTrades,
    losingTrades: backtestResults.losingTrades,
    winRate: backtestResults.winRate,
    maxDrawdown: backtestResults.maxDrawdown,
    sharpeRatio: backtestResults.sharpeRatio,
    totalFees: backtestResults.totalFees,
    totalSpreadCosts: backtestResults.totalSpreadCosts,
    totalOvernightCosts: backtestResults.totalOvernightCosts,
    // Margin level columns
    minMarginLevel: backtestResults.minMarginLevel,
    liquidationCount: backtestResults.liquidationCount,
    marginLiquidatedTrades: backtestResults.marginLiquidatedTrades,
    totalLiquidationLoss: backtestResults.totalLiquidationLoss,
    marginCloseoutLevel: backtestResults.marginCloseoutLevel,
    // VERSION 3: Backtest type filtering
    backtestType: backtestResults.backtestType,
    enhancedSignalConfig: backtestResults.enhancedSignalConfig,
    createdAt: backtestResults.createdAt,
  }).from(backtestResults)
    .where(and(
      eq(backtestResults.runId, runId),
      gte(backtestResults.totalReturn, minProfitThreshold)  // Only results >= threshold
    ));
}

/**
 * VERSION 3: Get backtest result with enhanced signal config
 * Used when attaching a signal-based backtest to a strategy
 */
export async function getBacktestResultWithEnhancedConfig(id: number) {
  const db = await getDb();
  if (!db) return null;

  const results = await db.select({
    id: backtestResults.id,
    epic: backtestResults.epic,
    timeframe: backtestResults.timeframe,
    backtestType: backtestResults.backtestType,
    enhancedSignalConfig: backtestResults.enhancedSignalConfig,
  })
  .from(backtestResults)
  .where(eq(backtestResults.id, id))
  .limit(1);

  return results.length > 0 ? results[0] : null;
}

/**
 * Get FULL backtest result including trades/dailyBalances - use for detail views only
 * Also joins with backtestRuns to get monthlyTopup (stored at run level)
 */
export async function getBacktestResultFull(id: number): Promise<(BacktestResult & { monthlyTopup?: number }) | null> {
  const db = await getDb();
  if (!db) return null;

  // Join with backtestRuns to get monthlyTopup
  const results = await db.select({
    // All backtestResults columns (no startDate/endDate - they don't exist in this table)
    id: backtestResults.id,
    runId: backtestResults.runId,
    epic: backtestResults.epic,
    timeframe: backtestResults.timeframe,
    crashProtectionEnabled: backtestResults.crashProtectionEnabled,
    // HMH (Hold Means Hold) columns
    hmhEnabled: backtestResults.hmhEnabled,
    hmhStopLossOffset: backtestResults.hmhStopLossOffset,
    indicatorName: backtestResults.indicatorName,
    indicatorParams: backtestResults.indicatorParams,
    leverage: backtestResults.leverage,
    stopLoss: backtestResults.stopLoss,
    timingConfig: backtestResults.timingConfig,
    signalBasedConfig: backtestResults.signalBasedConfig,
    dataSource: backtestResults.dataSource,
    paramHash: backtestResults.paramHash,
    initialBalance: backtestResults.initialBalance,
    finalBalance: backtestResults.finalBalance,
    totalContributions: backtestResults.totalContributions,
    totalReturn: backtestResults.totalReturn,
    totalTrades: backtestResults.totalTrades,
    winningTrades: backtestResults.winningTrades,
    losingTrades: backtestResults.losingTrades,
    winRate: backtestResults.winRate,
    maxDrawdown: backtestResults.maxDrawdown,
    sharpeRatio: backtestResults.sharpeRatio,
    totalFees: backtestResults.totalFees,
    totalSpreadCosts: backtestResults.totalSpreadCosts,
    totalOvernightCosts: backtestResults.totalOvernightCosts,
    minMarginLevel: backtestResults.minMarginLevel,
    liquidationCount: backtestResults.liquidationCount,
    marginLiquidatedTrades: backtestResults.marginLiquidatedTrades,
    totalLiquidationLoss: backtestResults.totalLiquidationLoss,
    marginCloseoutLevel: backtestResults.marginCloseoutLevel,
    trades: backtestResults.trades,
    dailyBalances: backtestResults.dailyBalances,
    createdAt: backtestResults.createdAt,
    // Get monthlyTopup and date range from parent run
    monthlyTopup: backtestRuns.monthlyTopup,
    startDate: backtestRuns.startDate,
    endDate: backtestRuns.endDate,
  })
  .from(backtestResults)
  .leftJoin(backtestRuns, eq(backtestResults.runId, backtestRuns.id))
  .where(eq(backtestResults.id, id))
  .limit(1);
  
  return results.length > 0 ? results[0] : null;
}

export async function getBacktestResultByHash(paramHash: string): Promise<BacktestResult | null> {
  const db = await getDb();
  if (!db) return null;

  const results = await db.select().from(backtestResults).where(eq(backtestResults.paramHash, paramHash)).limit(1);
  return results.length > 0 ? results[0] : null;
}

/**
 * Get single backtest result - EXCLUDES trades/dailyBalances for performance
 * Use getBacktestResultFull() when you need trade data for detail views
 */
export async function getBacktestResult(id: number): Promise<Omit<BacktestResult, 'trades' | 'dailyBalances'> | null> {
  const db = await getDb();
  if (!db) return null;

  const results = await db.select({
    id: backtestResults.id,
    runId: backtestResults.runId,
    epic: backtestResults.epic,
    timeframe: backtestResults.timeframe,
    crashProtectionEnabled: backtestResults.crashProtectionEnabled,
    // HMH (Hold Means Hold) columns
    hmhEnabled: backtestResults.hmhEnabled,
    hmhStopLossOffset: backtestResults.hmhStopLossOffset,
    indicatorName: backtestResults.indicatorName,
    indicatorParams: backtestResults.indicatorParams,
    leverage: backtestResults.leverage,
    stopLoss: backtestResults.stopLoss,
    timingConfig: backtestResults.timingConfig,
    signalBasedConfig: backtestResults.signalBasedConfig,
    dataSource: backtestResults.dataSource,
    paramHash: backtestResults.paramHash,
    initialBalance: backtestResults.initialBalance,
    finalBalance: backtestResults.finalBalance,
    totalContributions: backtestResults.totalContributions,
    totalReturn: backtestResults.totalReturn,
    totalTrades: backtestResults.totalTrades,
    winningTrades: backtestResults.winningTrades,
    losingTrades: backtestResults.losingTrades,
    winRate: backtestResults.winRate,
    maxDrawdown: backtestResults.maxDrawdown,
    sharpeRatio: backtestResults.sharpeRatio,
    totalFees: backtestResults.totalFees,
    totalSpreadCosts: backtestResults.totalSpreadCosts,
    totalOvernightCosts: backtestResults.totalOvernightCosts,
    minMarginLevel: backtestResults.minMarginLevel,
    liquidationCount: backtestResults.liquidationCount,
    marginLiquidatedTrades: backtestResults.marginLiquidatedTrades,
    totalLiquidationLoss: backtestResults.totalLiquidationLoss,
    marginCloseoutLevel: backtestResults.marginCloseoutLevel,
    createdAt: backtestResults.createdAt,
  }).from(backtestResults).where(eq(backtestResults.id, id)).limit(1);
  return results.length > 0 ? results[0] : null;
}

export async function getBacktestResultsByIds(ids: number[]): Promise<BacktestResult[]> {
  const db = await getDb();
  if (!db) return [];

  if (ids.length === 0) return [];
  
  // Use inArray for multiple IDs
  return await db.select().from(backtestResults).where(inArray(backtestResults.id, ids));
}

// ============================================================================
// Actual Trade Management (NEW - replaces liveTrades)
// ============================================================================

/**
 * Create a new actual trade record
 * Called when brain decides to BUY - creates pending trade before execution
 */
export async function createActualTrade(trade: InsertActualTrade): Promise<number> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(actualTrades).values(trade);
  return result[0].insertId;
}

/**
 * Get all trades for an account
 */
export async function getAccountActualTrades(accountId: number): Promise<ActualTrade[]> {
  const db = await getDb();
  if (!db) return [];

  return await db.select().from(actualTrades)
    .where(eq(actualTrades.accountId, accountId))
    .orderBy(desc(actualTrades.createdAt));
}

/**
 * Get open trades for an account
 */
export async function getOpenActualTrades(accountId: number): Promise<ActualTrade[]> {
  const db = await getDb();
  if (!db) return [];

  return await db.select().from(actualTrades)
    .where(and(
      eq(actualTrades.accountId, accountId),
      eq(actualTrades.status, 'open')
    ));
}

/**
 * Get open trade for a specific window
 */
export async function getOpenTradeForWindow(accountId: number, windowCloseTime: string): Promise<ActualTrade | null> {
  const db = await getDb();
  if (!db) return null;

  const results = await db.select().from(actualTrades)
    .where(and(
      eq(actualTrades.accountId, accountId),
      eq(actualTrades.windowCloseTime, windowCloseTime),
      eq(actualTrades.status, 'open')
    ))
    .limit(1);
  
  return results[0] || null;
}

/**
 * Get trade by deal ID (Capital.com reference)
 */
export async function getActualTradeByDealId(dealId: string): Promise<ActualTrade | null> {
  const db = await getDb();
  if (!db) return null;

  const results = await db.select().from(actualTrades)
    .where(eq(actualTrades.dealId, dealId))
    .limit(1);
  
  return results[0] || null;
}

/**
 * Update an actual trade
 */
export async function updateActualTrade(tradeId: number, updates: Partial<InsertActualTrade>): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.update(actualTrades)
    .set({ ...updates, updatedAt: new Date() })
    .where(eq(actualTrades.id, tradeId));
}

/**
 * Close an actual trade
 */
export async function closeActualTrade(
  tradeId: number,
  exitPrice: number,
  closeReason: 'window_close' | 'stop_loss' | 'manual' | 'rebalance',
  pnlData: { grossPnl: number; spreadCost: number; overnightCost: number; netPnl: number }
): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.update(actualTrades)
    .set({
      exitPrice,
      closedAt: new Date(),
      closeReason,
      grossPnl: pnlData.grossPnl,
      spreadCost: pnlData.spreadCost,
      overnightCost: pnlData.overnightCost,
      netPnl: pnlData.netPnl,
      status: 'closed',
      updatedAt: new Date(),
    })
    .where(eq(actualTrades.id, tradeId));
}

/**
 * Mark trade as stopped out (detected via hourly polling)
 */
export async function markTradeStoppedOut(
  tradeId: number,
  exitPrice: number,
  pnlData: { grossPnl: number; netPnl: number }
): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.update(actualTrades)
    .set({
      exitPrice,
      closedAt: new Date(),
      closeReason: 'stop_loss',
      grossPnl: pnlData.grossPnl,
      netPnl: pnlData.netPnl,
      status: 'stopped_out',
      updatedAt: new Date(),
    })
    .where(eq(actualTrades.id, tradeId));
}

/**
 * Update trade with deal ID after reconciliation
 */
export async function reconcileActualTrade(
  tradeId: number,
  dealId: string,
  dealReference: string,
  actualEntryPrice: number,
  contracts: number,
  notionalValue: number,
  marginUsed: number
): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.update(actualTrades)
    .set({
      dealId,
      dealReference,
      entryPrice: actualEntryPrice,
      contracts,
      notionalValue,
      marginUsed,
      status: 'open',
      openedAt: new Date(),
      updatedAt: new Date(),
    })
    .where(eq(actualTrades.id, tradeId));
}

// ============================================================================
// Validated Trade Management
// ============================================================================

/**
 * Create a validation record for an actual trade
 */
export async function createValidatedTrade(validation: InsertValidatedTrade): Promise<number> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(validatedTrades).values(validation);
  return result[0].insertId;
}

/**
 * Get validation for an actual trade
 */
export async function getValidationForTrade(actualTradeId: number): Promise<ValidatedTrade | null> {
  const db = await getDb();
  if (!db) return null;

  const results = await db.select().from(validatedTrades)
    .where(eq(validatedTrades.actualTradeId, actualTradeId))
    .limit(1);
  
  return results[0] || null;
}

/**
 * Get trades pending validation
 */
export async function getTradesPendingValidation(accountId?: number): Promise<ActualTrade[]> {
  const db = await getDb();
  if (!db) return [];

  // Get actual trades that are closed but don't have a validation record yet
  const conditions = [
    eq(actualTrades.status, 'closed')
  ];

  if (accountId) {
    conditions.push(eq(actualTrades.accountId, accountId));
  }

  const trades = await db.select().from(actualTrades)
    .where(and(...conditions))
    .orderBy(desc(actualTrades.closedAt));

  // Filter to only those without validation records
  const tradesWithoutValidation: ActualTrade[] = [];
  for (const trade of trades) {
    const validation = await getValidationForTrade(trade.id);
    if (!validation || validation.validationStatus === 'pending') {
      tradesWithoutValidation.push(trade);
    }
  }

  return tradesWithoutValidation;
}

// ============================================================================
// Trade Comparison Management
// ============================================================================

/**
 * Create or update a trade comparison record
 */
export async function upsertTradeComparison(comparison: InsertTradeComparison): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  // Check if comparison exists
  const existing = await db.select().from(tradeComparisons)
    .where(and(
      eq(tradeComparisons.accountId, comparison.accountId!),
      eq(tradeComparisons.tradeDate, comparison.tradeDate!),
      eq(tradeComparisons.windowCloseTime, comparison.windowCloseTime!)
    ))
    .limit(1);

  if (existing.length > 0) {
    await db.update(tradeComparisons)
      .set(comparison)
      .where(eq(tradeComparisons.id, existing[0].id));
  } else {
    await db.insert(tradeComparisons).values(comparison);
  }
}

/**
 * Get trade comparisons for an account
 */
export async function getTradeComparisons(accountId: number, limit: number = 100): Promise<TradeComparison[]> {
  const db = await getDb();
  if (!db) return [];

  return await db.select().from(tradeComparisons)
    .where(eq(tradeComparisons.accountId, accountId))
    .orderBy(desc(tradeComparisons.tradeDate))
    .limit(limit);
}

// ============================================================================
// Bot Logging
// ============================================================================

export async function createBotLog(log: InsertBotLog): Promise<void> {
  const db = await getDb();
  if (!db) return;

  await db.insert(botLogs).values(log);
}

export async function getBotLogs(accountId: number, limit: number = 100): Promise<BotLog[]> {
  const db = await getDb();
  if (!db) return [];

  return await db.select().from(botLogs)
    .where(eq(botLogs.accountId, accountId))
    .limit(limit);
}



// ============================================================================
// Epic Data Management
// ============================================================================

export async function listEpics(): Promise<Epic[]> {
  const db = await getDb();
  if (!db) return [];
  
  return await db.select().from(epics);
}

export async function getEpic(symbol: string): Promise<Epic | undefined> {
  const db = await getDb();
  if (!db) return undefined;
  
  const results = await db.select().from(epics).where(eq(epics.symbol, symbol)).limit(1);
  return results[0];
}

export async function createEpic(epic: InsertEpic): Promise<number> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  const result = await db.insert(epics).values(epic);
  return result[0].insertId;
}

export async function updateEpic(id: number, updates: Partial<InsertEpic>): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  await db.update(epics).set(updates).where(eq(epics.id, id));
}

export async function deleteEpic(id: number): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  // First get the epic symbol to delete related candle data
  const epicRow = await db.select().from(epics).where(eq(epics.id, id)).limit(1);
  const epicSymbol = epicRow[0]?.symbol;
  
  // Delete the epic from the epics table
  await db.delete(epics).where(eq(epics.id, id));
  
  // Also delete all candle data for this epic
  if (epicSymbol) {
    console.log(`[DB] Deleting candle data for epic: ${epicSymbol}`);
    await db.delete(candles).where(eq(candles.epic, epicSymbol));
    console.log(`[DB] Deleted candle data for ${epicSymbol}`);
  }
}



// ============================================================================
// Market Info Management
// ============================================================================

export async function getAllMarketInfo(): Promise<MarketInfo[]> {
  const db = await getDb();
  if (!db) return [];
  
  return await db.select().from(marketInfo);
}

export async function getMarketInfo(epic: string): Promise<MarketInfo | null> {
  const db = await getDb();
  if (!db) return null;
  
  const results = await db.select().from(marketInfo).where(eq(marketInfo.epic, epic)).limit(1);
  return results[0] || null;
}

export async function createMarketInfo(info: InsertMarketInfo): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  await db.insert(marketInfo).values(info);
}

export async function updateMarketInfo(epic: string, updates: Partial<InsertMarketInfo>): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  await db.update(marketInfo).set(updates).where(eq(marketInfo.epic, epic));
}

export async function deleteMarketInfo(epic: string): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  await db.delete(marketInfo).where(eq(marketInfo.epic, epic));
}



// ============================================================================
// Strategy Management
// ============================================================================
// DELETE_PROTECTION: Backtest-Strategy Relationship - IMPLEMENTED Dec 2025
// ============================================================================
// Smart delete with 3 modes:
// - 'check': Returns info about which results are protected (default)
// - 'except_protected': Deletes all EXCEPT results used in strategy DNA
// - 'force': Deletes everything including strategy-linked results
// 
// Key functions:
// - getProtectedTestIds() - Returns Set of all test IDs used in ANY strategy
// - getStrategiesUsingRunResults(runId) - Returns which strategies use this run's results
// - deleteBacktestRun(runId, mode) - Smart delete with modes above
// 
// Behavior:
// - Run is only deleted if ALL its results are deleted (none protected)
// - Stop button cleans up queue (stopped runs can't resume)
// - Pause keeps queue intact (paused runs can resume)
// 
// Keywords: PURGE, TRUNCATE, DELETE, CASCADE, ORPHAN_CHECK
// ============================================================================

import { strategyCombos, InsertStrategyCombo, savedStrategies } from "../drizzle/schema";

export async function getUserStrategies(userId: number) {
  const db = await getDb();
  if (!db) return [];
  
  const strategies = await db.select().from(strategyCombos).where(eq(strategyCombos.userId, userId));
  
  // Fetch latest test results for each strategy
  const strategiesWithResults = await Promise.all(strategies.map(async (strategy) => {
    let tests = [];
    let descriptionText = '';
    try {
      const parsed = JSON.parse(strategy.description || '{}');
      tests = parsed.tests || [];
      descriptionText = parsed.description || '';
    } catch (e) {
      // Legacy strategy without tests
      descriptionText = strategy.description || '';
    }

    // Get latest test result for this strategy
    const latestResults = await db
      .select()
      .from(strategyTestResults)
      .where(eq(strategyTestResults.strategyId, strategy.id))
      .orderBy(desc(strategyTestResults.createdAt))
      .limit(1);
    
    const latestResult = latestResults.length > 0 ? latestResults[0] : null;

    return {
      id: strategy.id,
      name: strategy.name,
      description: descriptionText,
      tests,
      conflictResolution: {
        mode: strategy.resolutionMode,
        metrics: strategy.resolutionMetrics,
      },
      performance: {
        profitability: latestResult?.totalReturn || 0,
        winRate: latestResult?.winRate || 0,
        sharpeRatio: latestResult?.sharpeRatio || 0,
        maxDrawdown: latestResult?.maxDrawdown || 0,
        finalBalance: latestResult?.finalBalance || 0,
        initialBalance: latestResult?.initialBalance || 0,
      },
      hasBeenTested: !!latestResult,
      lastTestDate: latestResult?.createdAt || strategy.lastTestDate,
      createdAt: strategy.createdAt,
    };
  }));
  
  return strategiesWithResults;
}

export async function getStrategy(id: number) {
  const db = await getDb();
  if (!db) return undefined;
  
  // Try strategyCombos first (legacy table)
  const comboResults = await db.select().from(strategyCombos).where(eq(strategyCombos.id, id)).limit(1);
  if (comboResults.length > 0) {
    const strategy = comboResults[0];
    let tests = [];
    let descriptionText = '';
    try {
      const parsed = JSON.parse(strategy.description || '{}');
      tests = parsed.tests || [];
      descriptionText = parsed.description || '';
    } catch (e) {
      // Legacy strategy without tests
      descriptionText = strategy.description || '';
    }

    return {
      id: strategy.id,
      name: strategy.name,
      description: descriptionText,
      tests,
      conflictResolution: {
        mode: strategy.resolutionMode,
        metrics: strategy.resolutionMetrics,
      },
      performance: {
        profitability: strategy.profitability,
        winRate: strategy.winRate,
        sharpeRatio: strategy.sharpeRatio,
        maxDrawdown: strategy.maxDrawdown,
      },
      hasBeenTested: strategy.hasBeenTested,
      lastTestDate: strategy.lastTestDate,
      createdAt: strategy.createdAt,
    };
  }
  
  // Try savedStrategies table (new format)
  const savedResults = await db.select().from(savedStrategies).where(eq(savedStrategies.id, id)).limit(1);
  if (savedResults.length === 0) return undefined;
  
  const strategy = savedResults[0];
  const dnaStrands = strategy.dnaStrands as any[] || [];
  
  // Convert DNA strands to tests format for brain compatibility
  const tests = dnaStrands
    .filter((strand: any) => strand.isActive && !strand.isPaused)
    .map((strand: any) => ({
      epic: strand.epic,
      timeframe: strand.timeframe,
      indicatorName: strand.indicatorName,  // Fixed: was 'indicator'
      indicatorParams: strand.indicatorParams,  // Fixed: was 'params'
      direction: strand.direction,
      leverage: strand.leverage,
      stopLoss: strand.stopLoss,
      timingConfig: strand.timingConfig || { mode: 'ManusTime' },  // Added: required by brain
      performance: {
        profitability: strand.profitability,
        winRate: strand.winRate,
        sharpeRatio: strand.sharpeRatio,
        maxDrawdown: strand.maxDrawdown,
        totalTrades: strand.totalTrades,
      },
    }));
  
  // Get conflict resolution from windowConfig
  // windowConfig has TWO levels:
  //   1. Per-window: windows[].conflictResolutionMetric - resolves conflicts within a window
  //   2. Global: windowConfig.conflictResolution - higher level conflicts (rarely used)
  // Both should use same options: sharpeRatio, profitability, winRate, maxDrawdown
  const windowConfig = strategy.windowConfig as any || {};
  const globalConflictResolution = windowConfig.conflictResolution || {};
  
  // FIX: First try global conflictResolution.mode, then fall back to per-window metric
  // Most strategies use per-window config, so we need to read from windows[0].conflictResolutionMetric
  let conflictMode = globalConflictResolution.mode;
  
  if (!conflictMode && windowConfig.windows?.length > 0) {
    // Use the first window's conflictResolutionMetric as default
    // NOTE: If strategy has multiple windows with DIFFERENT metrics, brain should ideally
    // look up the correct window based on windowCloseTime. For now, use first window's metric.
    const firstWindowMetric = windowConfig.windows[0]?.conflictResolutionMetric;
    if (firstWindowMetric) {
      conflictMode = firstWindowMetric;
    }
  }
  
  // Final default to 'sharpeRatio' to match Strategy Configuration UI
  conflictMode = conflictMode || 'sharpeRatio';
  
  return {
    id: strategy.id,
    name: strategy.name,
    description: strategy.description || '',
    tests,
    conflictResolution: {
      mode: conflictMode,
      metrics: globalConflictResolution.weights || null,
    },
    // Also include windowConfig so brain can access per-window settings
    windowConfig: windowConfig,
    performance: {
      profitability: 0, // Calculate from DNA strands if needed
      winRate: 0,
      sharpeRatio: 0,
      maxDrawdown: 0,
    },
    hasBeenTested: true,
    lastTestDate: strategy.updatedAt,
    createdAt: strategy.createdAt,
  };
}

export async function createStrategy(data: {
  userId: number;
  name: string;
  description: string | null;
  tests: any[];
  conflictResolution: {
    mode: string;
    weights?: Record<string, number> | null;
  };
}): Promise<number> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  // Validate allocation percentages sum to 100%
  const totalAllocation = data.tests.reduce((sum: number, test: any) => sum + test.allocationPercent, 0);
  if (Math.abs(totalAllocation - 100) > 0.01) {
    throw new Error(`Test allocations must sum to 100%, got ${totalAllocation}%`);
  }

  const result = await db.insert(strategyCombos).values({
    userId: data.userId,
    name: data.name,
    description: JSON.stringify({
      description: data.description,
      tests: data.tests,
    }),
    strategyIds: [], // Not using saved strategies, using inline tests
    resolutionMode: data.conflictResolution.mode,
    resolutionMetrics: data.conflictResolution.weights 
      ? Object.entries(data.conflictResolution.weights).map(([metric, weight]) => ({
          metric,
          weight,
        }))
      : undefined,
    hasBeenTested: false,
  });

  return result[0].insertId;
}

export async function addTestToStrategy(strategyId: number, newTest: any): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  // Get current strategy
  const strategy = await getStrategy(strategyId);
  if (!strategy) throw new Error("Strategy not found");
  
  // Add new test to tests array
  const updatedTests = [...strategy.tests, newTest];
  
  // Update strategy
  await db.update(strategyCombos)
    .set({
      description: JSON.stringify({
        description: strategy.description,
        tests: updatedTests,
      }),
    })
    .where(eq(strategyCombos.id, strategyId));
}

export async function updateStrategy(id: number, updates: {
  description?: string;
  conflictResolution?: { mode: string; weights?: Record<string, number> };
  tests?: any[];
}): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  // Get current strategy
  const strategy = await getStrategy(id);
  if (!strategy) throw new Error("Strategy not found");
  
  // Merge updates
  const updatedDescription = updates.description !== undefined ? updates.description : strategy.description;
  const updatedConflictResolution = updates.conflictResolution || strategy.conflictResolution;
  const updatedTests = updates.tests || strategy.tests;
  
  await db.update(strategyCombos)
    .set({
      description: JSON.stringify({
        description: updatedDescription,
        tests: updatedTests,
      }),
      resolutionMode: updatedConflictResolution.mode,
      resolutionMetrics: ('weights' in updatedConflictResolution && updatedConflictResolution.weights) ? Object.entries(updatedConflictResolution.weights).map(([metric, weight]) => ({ metric, weight: weight as number })) : undefined,
    })
    .where(eq(strategyCombos.id, id));
}

export async function deleteStrategy(id: number): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  await db.delete(strategyCombos).where(eq(strategyCombos.id, id));
}

export async function createStrategyTestResult(result: InsertStrategyTestResult): Promise<number> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");
  
  const insertResult = await db.insert(strategyTestResults).values(result);
  return insertResult[0].insertId;
}

export async function getStrategyTestResults(strategyId: number, limit: number = 10): Promise<StrategyTestResult[]> {
  const db = await getDb();
  if (!db) return [];
  
  try {
    return await db.select()
      .from(strategyTestResults)
      .where(eq(strategyTestResults.strategyId, strategyId))
      .orderBy(desc(strategyTestResults.createdAt))
      .limit(limit);
  } catch (error) {
    console.error(`[Database] Error fetching strategy test results for strategy ${strategyId}:`, error);
    return [];
  }
}

export async function getAllStrategyTestResults(userId: number): Promise<StrategyTestResultWithExtras[]> {
  const db = await getDb();
  if (!db) return [];
  
  try {
    const results = await db.select({
      id: strategyTestResults.id,
      userId: strategyTestResults.userId,
      strategyId: strategyTestResults.strategyId,
      strategyName: strategyCombos.name,
      epic: strategyTestResults.epic,
      startDate: strategyTestResults.startDate,
      endDate: strategyTestResults.endDate,
      initialBalance: strategyTestResults.initialBalance,
      monthlyTopup: strategyTestResults.monthlyTopup,
      conflictResolution: strategyTestResults.conflictResolution,
      testCount: strategyTestResults.testCount,
      finalBalance: strategyTestResults.finalBalance,
      totalContributions: strategyTestResults.totalContributions,
      totalReturn: strategyTestResults.totalReturn,
      totalTrades: strategyTestResults.totalTrades,
      winningTrades: strategyTestResults.winningTrades,
      losingTrades: strategyTestResults.losingTrades,
      winRate: strategyTestResults.winRate,
      maxDrawdown: strategyTestResults.maxDrawdown,
      sharpeRatio: strategyTestResults.sharpeRatio,
      trades: strategyTestResults.trades,
      dailyBalances: strategyTestResults.dailyBalances,
      calculationMode: strategyTestResults.calculationMode,
      executionTimeMs: strategyTestResults.executionTimeMs,
      createdAt: strategyTestResults.createdAt,
    })
      .from(strategyTestResults)
      .leftJoin(strategyCombos, eq(strategyTestResults.strategyId, strategyCombos.id))
      .where(eq(strategyTestResults.userId, userId))
      .orderBy(desc(strategyTestResults.createdAt));
    
    // Calculate totalCosts from trades for each result
    return results.map(result => {
      let totalCosts = 0;
      if (result.trades && Array.isArray(result.trades)) {
        totalCosts = result.trades.reduce((sum: number, trade: any) => {
          const overnight = trade.overnight_costs || 0;
          const spread = trade.spread_cost || 0;
          return sum + overnight + spread;
        }, 0);
      }
      return {
        ...result,
        totalCosts,
      } as any;
    });
  } catch (error) {
    console.error(`[Database] Error fetching all strategy test results for user ${userId}:`, error);
    return [];
  }
}

export async function getStrategyTestResult(id: number): Promise<StrategyTestResult | null> {
  const db = await getDb();
  if (!db) return null;
  
  try {
    const results = await db.select()
      .from(strategyTestResults)
      .where(eq(strategyTestResults.id, id))
      .limit(1);
    return results[0] || null;
  } catch (error) {
    console.error(`[Database] Error fetching strategy test result ${id}:`, error);
    return null;
  }
}

export async function deleteStrategyTestResult(id: number): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.delete(strategyTestResults).where(eq(strategyTestResults.id, id));
}

// ============================================================================
// VERSION 3: Trust Matrix Functions
// ============================================================================

/**
 * Load trust matrix scores for a specific epic/timeframe combination
 * Used when attaching a signal-based backtest to a strategy
 * @param epic - The trading epic (e.g., "SOXL", "TECL")
 * @param timeframe - The candle timeframe (e.g., "5m", "15m")
 * @returns Array of trust matrix entries for the epic/timeframe
 */
export async function getTrustMatrixSnapshot(epic: string, timeframe: string) {
  const db = await getDb();
  if (!db) return [];

  try {
    const results = await db.select({
      entryIndicator: indicatorPairTrust.entryIndicator,
      exitIndicator: indicatorPairTrust.exitIndicator,
      trades: indicatorPairTrust.trades,
      wins: indicatorPairTrust.wins,
      totalPnl: indicatorPairTrust.totalPnl,
      avgPnl: indicatorPairTrust.avgPnl,
      sharpe: indicatorPairTrust.sharpe,
      avgHoldBars: indicatorPairTrust.avgHoldBars,
      winRate: indicatorPairTrust.winRate,
      probability: indicatorPairTrust.probability,
      optimalLeverage: indicatorPairTrust.optimalLeverage,
      optimalStopLoss: indicatorPairTrust.optimalStopLoss,
    })
    .from(indicatorPairTrust)
    .where(and(
      eq(indicatorPairTrust.epic, epic),
      eq(indicatorPairTrust.timeframe, timeframe)
    ));

    console.log(`[DB] Loaded ${results.length} trust matrix entries for ${epic}/${timeframe}`);
    return results;
  } catch (error) {
    console.error(`[DB] Error loading trust matrix for ${epic}/${timeframe}:`, error);
    return [];
  }
}

/**
 * Update a saved strategy with enhanced signal config and trust matrix snapshot
 * Called when attaching a signal-based backtest to a strategy
 * @param strategyId - ID of the savedStrategy to update
 * @param enhancedSignalConfig - The signal-based configuration from the backtest
 * @param trustMatrix - Array of trust matrix entries to snapshot
 */
export async function updateStrategyWithTrustMatrix(
  strategyId: number,
  enhancedSignalConfig: any,
  trustMatrix: any[]
): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  try {
    await db.update(savedStrategies)
      .set({
        enhancedSignalConfig: enhancedSignalConfig,
        trustMatrixSnapshot: trustMatrix,
      })
      .where(eq(savedStrategies.id, strategyId));

    console.log(`[DB] Updated strategy ${strategyId} with trust matrix (${trustMatrix.length} pairs)`);
  } catch (error) {
    console.error(`[DB] Error updating strategy ${strategyId} with trust matrix:`, error);
    throw error;
  }
}

/**
 * Get a saved strategy with its trust matrix snapshot
 * Used during live trading to load the frozen trust scores
 * @param strategyId - ID of the savedStrategy to load
 */
export async function getSavedStrategyWithTrust(strategyId: number) {
  const db = await getDb();
  if (!db) return null;

  try {
    const results = await db.select()
      .from(savedStrategies)
      .where(eq(savedStrategies.id, strategyId))
      .limit(1);

    if (results.length === 0) return null;

    const strategy = results[0];
    console.log(`[DB] Loaded strategy ${strategyId}: ${strategy.name} (trust matrix: ${strategy.trustMatrixSnapshot?.length || 0} pairs)`);
    return strategy;
  } catch (error) {
    console.error(`[DB] Error loading saved strategy ${strategyId}:`, error);
    return null;
  }
}

// ============================================================================
// Settings Management
// ============================================================================

export async function getAllSettings(): Promise<Setting[]> {
  const db = await getDb();
  if (!db) return [];
  
  try {
    return await db.select().from(settings);
  } catch (error) {
    console.error("[Database] Error fetching settings:", error);
    return [];
  }
}

export async function getSettingsByCategory(category: string): Promise<Setting[]> {
  const db = await getDb();
  if (!db) return [];
  
  try {
    return await db.select().from(settings).where(eq(settings.category, category));
  } catch (error) {
    console.error(`[Database] Error fetching settings for category ${category}:`, error);
    return [];
  }
}

export async function getSetting(category: string, key: string): Promise<Setting | null> {
  const db = await getDb();
  if (!db) return null;
  
  try {
    const results = await db.select().from(settings)
      .where(and(eq(settings.category, category), eq(settings.key, key)))
      .limit(1);
    return results[0] || null;
  } catch (error) {
    console.error(`[Database] Error fetching setting ${category}.${key}:`, error);
    return null;
  }
}

export async function upsertSetting(setting: InsertSetting): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  try {
    await db.insert(settings).values(setting)
      .onDuplicateKeyUpdate({
        set: {
          value: setting.value,
          valueType: setting.valueType,
          description: setting.description,
          isSecret: setting.isSecret,
        }
      });
  } catch (error) {
    console.error("[Database] Error upserting setting:", error);
    throw error;
  }
}

export async function deleteSetting(category: string, key: string): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  try {
    await db.delete(settings)
      .where(and(eq(settings.category, category), eq(settings.key, key)));
  } catch (error) {
    console.error(`[Database] Error deleting setting ${category}.${key}:`, error);
    throw error;
  }
}



// ============================================================================
// Core Allocation Management
// ============================================================================

export async function getCoreAllocations(runId: number) {
  const db = await getDb();
  if (!db) return [];
  
  try {
    const { coreAllocations } = await import("../drizzle/schema");
    const results = await db
      .select()
      .from(coreAllocations)
      .where(eq(coreAllocations.runId, runId));
    return results;
  } catch (error) {
    console.error("[Database] Failed to get core allocations:", error);
    return [];
  }
}

export async function getAllCoreAllocations() {
  const db = await getDb();
  if (!db) return [];
  
  try {
    const { coreAllocations } = await import("../drizzle/schema");
    const results = await db.select().from(coreAllocations);
    return results;
  } catch (error) {
    console.error("[Database] Failed to get all core allocations:", error);
    return [];
  }
}

// ============================================================================
// Backtest Queue Management (for crash recovery and progress tracking)
// ============================================================================

/**
 * Insert multiple items into the backtest queue
 * Uses INSERT IGNORE to skip duplicates (paramHash is unique)
 */
export async function insertQueueItems(items: InsertBacktestQueueItem[]): Promise<{ inserted: number; skipped: number }> {
  const db = await getDb();
  if (!db || items.length === 0) return { inserted: 0, skipped: 0 };
  
  const startTime = Date.now();
  let inserted = 0;
  let skipped = 0;
  
  // Insert in batches of 100 to avoid huge queries
  const batchSize = 100;
  for (let i = 0; i < items.length; i += batchSize) {
    const batch = items.slice(i, i + batchSize);
    try {
      // Use onDuplicateKeyUpdate with a no-op to effectively do INSERT IGNORE
      const result = await db.insert(backtestQueue)
        .values(batch)
        .onDuplicateKeyUpdate({ set: { id: sql`id` } }); // No-op update
      
      // MySQL returns affectedRows = 1 for insert, 2 for update
      // Since we're doing no-op updates, we count as skipped
      inserted += batch.length; // Approximate - actual might differ
    } catch (error: any) {
      console.error(`[DB] Error inserting queue batch ${i}-${i + batch.length}:`, error.message);
      skipped += batch.length;
    }
  }
  
  const duration = Date.now() - startTime;
  console.log(`[DB] insertQueueItems: ${inserted} inserted, ${skipped} skipped in ${duration}ms`);
  return { inserted, skipped };
}

/**
 * Atomically claim a batch of pending tests for a worker
 * Uses UPDATE with LIMIT to prevent race conditions
 */
export async function claimQueueItems(runId: number, workerId: string, limit: number): Promise<BacktestQueueItem[]> {
  const db = await getDb();
  if (!db) return [];
  
  try {
    // First, update pending items to processing for this worker
    await db.execute(sql`
      UPDATE backtestQueue 
      SET status = 'processing', 
          workerId = ${workerId}, 
          claimedAt = NOW()
      WHERE runId = ${runId} 
        AND status = 'pending'
      ORDER BY id
      LIMIT ${limit}
    `);
    
    // Then fetch the claimed items
    const claimed = await db.select()
      .from(backtestQueue)
      .where(and(
        eq(backtestQueue.runId, runId),
        eq(backtestQueue.workerId, workerId),
        eq(backtestQueue.status, 'processing')
      ));
    
    console.log(`[DB] Worker ${workerId} claimed ${claimed.length} tests for run ${runId}`);
    return claimed;
  } catch (error) {
    console.error(`[DB] Error claiming queue items:`, error);
    return [];
  }
}

/**
 * Mark a queue item as completed and link to result
 */
export async function markQueueItemComplete(queueId: number, resultId: number): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  await db.update(backtestQueue)
    .set({ 
      status: 'completed', 
      completedAt: new Date(),
      resultId: resultId 
    })
    .where(eq(backtestQueue.id, queueId));
}

/**
 * Mark a queue item as completed by hash and link to result
 */
export async function markQueueItemCompleteByHash(paramHash: string, resultId: number): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  await db.update(backtestQueue)
    .set({ 
      status: 'completed', 
      completedAt: new Date(),
      resultId: resultId 
    })
    .where(eq(backtestQueue.paramHash, paramHash));
}

/**
 * Mark a queue item as failed with error message
 */
export async function markQueueItemFailed(queueId: number, errorMessage: string): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  await db.update(backtestQueue)
    .set({ 
      status: 'failed', 
      completedAt: new Date(),
      errorMessage: errorMessage 
    })
    .where(eq(backtestQueue.id, queueId));
}

/**
 * Get queue statistics for a run
 */
export async function getQueueStats(runId: number): Promise<{
  pending: number;
  processing: number;
  completed: number;
  failed: number;
  total: number;
}> {
  const db = await getDb();
  if (!db) return { pending: 0, processing: 0, completed: 0, failed: 0, total: 0 };
  
  const [result] = await db.select({
    pending: sql<number>`SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)`,
    processing: sql<number>`SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END)`,
    completed: sql<number>`SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)`,
    failed: sql<number>`SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)`,
    total: sql<number>`COUNT(*)`,
  })
  .from(backtestQueue)
  .where(eq(backtestQueue.runId, runId));
  
  return {
    pending: Number(result?.pending || 0),
    processing: Number(result?.processing || 0),
    completed: Number(result?.completed || 0),
    failed: Number(result?.failed || 0),
    total: Number(result?.total || 0),
  };
}

/**
 * Reset stale claimed items back to pending (for crash recovery)
 * Items that have been processing for longer than timeoutMinutes are reset
 */
export async function resetStaleQueueItems(runId: number, timeoutMinutes: number): Promise<number> {
  const db = await getDb();
  if (!db) return 0;
  
  const cutoffTime = new Date(Date.now() - timeoutMinutes * 60 * 1000);
  
  const result = await db.update(backtestQueue)
    .set({ 
      status: 'pending', 
      workerId: null, 
      claimedAt: null 
    })
    .where(and(
      eq(backtestQueue.runId, runId),
      eq(backtestQueue.status, 'processing'),
      lt(backtestQueue.claimedAt, cutoffTime)
    ));
  
  // Note: Drizzle doesn't return affected rows easily, so we estimate
  console.log(`[DB] Reset stale queue items for run ${runId} (timeout: ${timeoutMinutes}min)`);
  return 0; // Can't easily get affected count
}

/**
 * Delete all queue items for a run (cleanup)
 */
export async function deleteQueueItems(runId: number): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  await db.delete(backtestQueue)
    .where(eq(backtestQueue.runId, runId));
  
  console.log(`[DB] Deleted queue items for run ${runId}`);
}

/**
 * Get pending queue items for a run (for Python to process)
 */
export async function getPendingQueueItems(runId: number, limit: number = 1000): Promise<BacktestQueueItem[]> {
  const db = await getDb();
  if (!db) return [];
  
  return await db.select()
    .from(backtestQueue)
    .where(and(
      eq(backtestQueue.runId, runId),
      eq(backtestQueue.status, 'pending')
    ))
    .orderBy(backtestQueue.id)
    .limit(limit);
}

// ==================== INDICATOR PARAMETERS ====================

/**
 * Get all indicator parameters grouped by indicator name
 */
export async function getAllIndicatorParams(): Promise<IndicatorParam[]> {
  const db = await getDb();
  if (!db) return [];
  
  return await db.select()
    .from(indicatorParams)
    .orderBy(indicatorParams.indicatorName, indicatorParams.sortOrder);
}

/**
 * Get parameters for a specific indicator
 */
export async function getIndicatorParams(indicatorName: string): Promise<IndicatorParam[]> {
  const db = await getDb();
  if (!db) return [];
  
  return await db.select()
    .from(indicatorParams)
    .where(eq(indicatorParams.indicatorName, indicatorName))
    .orderBy(indicatorParams.sortOrder);
}

/**
 * Get all unique indicator names
 */
export async function getIndicatorNames(): Promise<string[]> {
  const db = await getDb();
  if (!db) return [];
  
  const result = await db.selectDistinct({ name: indicatorParams.indicatorName })
    .from(indicatorParams)
    .orderBy(indicatorParams.indicatorName);
  
  return result.map(r => r.name);
}

/**
 * Update a single parameter
 */
export async function updateIndicatorParam(
  id: number, 
  updates: Partial<InsertIndicatorParam>
): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  await db.update(indicatorParams)
    .set(updates)
    .where(eq(indicatorParams.id, id));
  
  console.log(`[DB] Updated indicator param ${id}`);
}

/**
 * Create a new parameter
 */
export async function createIndicatorParam(param: InsertIndicatorParam): Promise<number> {
  const db = await getDb();
  if (!db) return 0;
  
  const [result] = await db.insert(indicatorParams).values(param);
  console.log(`[DB] Created indicator param for ${param.indicatorName}.${param.paramName}`);
  return (result as any).insertId;
}

/**
 * Delete a parameter
 */
export async function deleteIndicatorParam(id: number): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  await db.delete(indicatorParams).where(eq(indicatorParams.id, id));
  console.log(`[DB] Deleted indicator param ${id}`);
}

/**
 * Reset parameters for an indicator to defaults (delete all params for indicator)
 */
export async function deleteIndicatorParamsByIndicator(indicatorName: string): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  await db.delete(indicatorParams).where(eq(indicatorParams.indicatorName, indicatorName));
  console.log(`[DB] Deleted all params for indicator ${indicatorName}`);
}

/**
 * Bulk upsert parameters for an indicator
 */
export async function upsertIndicatorParams(params: InsertIndicatorParam[]): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  for (const param of params) {
    await db.insert(indicatorParams)
      .values(param)
      .onDuplicateKeyUpdate({
        set: {
          displayName: param.displayName,
          minValue: param.minValue,
          maxValue: param.maxValue,
          stepValue: param.stepValue,
          defaultValue: param.defaultValue,
          description: param.description,
          paramType: param.paramType,
          isEnabled: param.isEnabled,
          sortOrder: param.sortOrder,
        }
      });
  }
  console.log(`[DB] Upserted ${params.length} indicator params`);
}

/**
 * Get universal parameters (applies to all indicators)
 */
export async function getUniversalParams(): Promise<IndicatorParam[]> {
  const db = await getDb();
  if (!db) return [];
  
  return await db.select()
    .from(indicatorParams)
    .where(eq(indicatorParams.indicatorName, '_universal'))
    .orderBy(indicatorParams.sortOrder);
}

// =============================================================================
// ACCOUNT TRANSACTIONS
// =============================================================================

/**
 * Get all transactions for an account, ordered by date
 */
export async function getAccountTransactions(accountId: number): Promise<AccountTransaction[]> {
  const db = await getDb();
  if (!db) return [];
  
  return await db.select()
    .from(accountTransactions)
    .where(eq(accountTransactions.accountId, accountId))
    .orderBy(asc(accountTransactions.transactionDate));
}

/**
 * Get transactions within a date range
 */
export async function getAccountTransactionsInRange(
  accountId: number, 
  fromDate: Date, 
  toDate: Date
): Promise<AccountTransaction[]> {
  const db = await getDb();
  if (!db) return [];
  
  return await db.select()
    .from(accountTransactions)
    .where(and(
      eq(accountTransactions.accountId, accountId),
      gte(accountTransactions.transactionDate, fromDate),
      lte(accountTransactions.transactionDate, toDate)
    ))
    .orderBy(asc(accountTransactions.transactionDate));
}

/**
 * Get latest transaction date for an account (to know where to resume sync)
 */
export async function getLatestTransactionDate(accountId: number): Promise<Date | null> {
  const db = await getDb();
  if (!db) return null;
  
  const [result] = await db.select({ maxDate: sql<Date>`MAX(transactionDate)` })
    .from(accountTransactions)
    .where(eq(accountTransactions.accountId, accountId));
  
  return result?.maxDate || null;
}

/**
 * Insert a single transaction (with duplicate check by capitalReference)
 */
export async function insertAccountTransaction(tx: InsertAccountTransaction): Promise<number | null> {
  const db = await getDb();
  if (!db) return null;
  
  // Check for duplicate by reference
  if (tx.capitalReference) {
    const [existing] = await db.select({ id: accountTransactions.id })
      .from(accountTransactions)
      .where(and(
        eq(accountTransactions.accountId, tx.accountId),
        eq(accountTransactions.capitalReference, tx.capitalReference)
      ))
      .limit(1);
    
    if (existing) {
      return null; // Already exists
    }
  }
  
  const [result] = await db.insert(accountTransactions).values(tx);
  return (result as any).insertId;
}

/**
 * Batch insert transactions (skips duplicates)
 */
export async function insertAccountTransactionsBatch(
  txs: InsertAccountTransaction[]
): Promise<{ inserted: number; skipped: number }> {
  const db = await getDb();
  if (!db) return { inserted: 0, skipped: 0 };
  
  let inserted = 0;
  let skipped = 0;
  
  for (const tx of txs) {
    const id = await insertAccountTransaction(tx);
    if (id) {
      inserted++;
    } else {
      skipped++;
    }
  }
  
  return { inserted, skipped };
}

/**
 * Get transaction summary by type for an account
 */
export async function getTransactionSummaryByType(accountId: number): Promise<{
  type: string;
  count: number;
  totalAmount: number;
}[]> {
  const db = await getDb();
  if (!db) return [];
  
  const results = await db.select({
    type: accountTransactions.transactionType,
    count: sql<number>`COUNT(*)`,
    totalAmount: sql<number>`SUM(amount)`,
  })
    .from(accountTransactions)
    .where(eq(accountTransactions.accountId, accountId))
    .groupBy(accountTransactions.transactionType);
  
  return results.map(r => ({
    type: r.type,
    count: Number(r.count),
    totalAmount: Number(r.totalAmount) || 0,
  }));
}

/**
 * Delete all transactions for an account (for re-sync)
 */
export async function deleteAccountTransactions(accountId: number): Promise<number> {
  const db = await getDb();
  if (!db) return 0;
  
  const [result] = await db.delete(accountTransactions)
    .where(eq(accountTransactions.accountId, accountId));
  
  return (result as any).affectedRows || 0;
}

// ============================================================================
// Tested Hashes - Global Deduplication
// ============================================================================

/**
 * Check if a parameter hash has already been tested
 * Returns the existing record if found, null if not tested yet
 */
export async function getTestedHash(paramHash: string): Promise<TestedHash | null> {
  const db = await getDb();
  if (!db) return null;
  
  const results = await db.select()
    .from(testedHashes)
    .where(eq(testedHashes.paramHash, paramHash))
    .limit(1);
  
  return results.length > 0 ? results[0] : null;
}

/**
 * Save a tested hash after running a backtest
 * Called BEFORE deciding whether to save the full result
 */
export async function saveTestedHash(data: {
  paramHash: string;
  totalReturn: number;
  epic: string;
  indicatorName: string;
  leverage: number;
  stopLoss: number;
  resultId: number | null;
  firstRunId: number;
}): Promise<number | null> {
  const db = await getDb();
  if (!db) return null;
  
  try {
    const [result] = await db.insert(testedHashes).values({
      paramHash: data.paramHash,
      totalReturn: data.totalReturn,
      epic: data.epic,
      indicatorName: data.indicatorName,
      leverage: data.leverage,
      stopLoss: data.stopLoss,
      resultId: data.resultId,
      firstRunId: data.firstRunId,
    });
    
    return (result as any).insertId;
  } catch (error: any) {
    // Silently handle duplicate hash (race condition)
    if (error?.code === 'ER_DUP_ENTRY') {
      return null;
    }
    throw error;
  }
}

/**
 * Update a tested hash with the resultId after saving full result
 */
export async function updateTestedHashResultId(paramHash: string, resultId: number): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  await db.update(testedHashes)
    .set({ resultId })
    .where(eq(testedHashes.paramHash, paramHash));
}

/**
 * Get statistics about tested hashes
 */
export async function getTestedHashStats(): Promise<{
  total: number;
  profitable: number;
  unprofitable: number;
  avgReturn: number;
}> {
  const db = await getDb();
  if (!db) return { total: 0, profitable: 0, unprofitable: 0, avgReturn: 0 };
  
  const [stats] = await db.select({
    total: sql<number>`COUNT(*)`,
    profitable: sql<number>`SUM(CASE WHEN totalReturn > 0 THEN 1 ELSE 0 END)`,
    unprofitable: sql<number>`SUM(CASE WHEN totalReturn <= 0 THEN 1 ELSE 0 END)`,
    avgReturn: sql<number>`AVG(totalReturn)`,
  }).from(testedHashes);
  
  return {
    total: Number(stats?.total) || 0,
    profitable: Number(stats?.profitable) || 0,
    unprofitable: Number(stats?.unprofitable) || 0,
    avgReturn: Number(stats?.avgReturn) || 0,
  };
}

/**
 * Get the minProfitThreshold setting
 */
export async function getMinProfitThreshold(): Promise<number> {
  const db = await getDb();
  if (!db) return 0;
  
  const results = await db.select()
    .from(settings)
    .where(and(
      eq(settings.category, 'backtest'),
      eq(settings.key, 'minProfitThreshold')
    ))
    .limit(1);
  
  if (results.length === 0) return 0;
  return parseFloat(results[0].value) || 0;
}

/**
 * Set the minProfitThreshold setting
 */
export async function setMinProfitThreshold(value: number): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  // Try to update first
  const [updateResult] = await db.update(settings)
    .set({ value: value.toString() })
    .where(and(
      eq(settings.category, 'backtest'),
      eq(settings.key, 'minProfitThreshold')
    ));
  
  // If no rows updated, insert
  if ((updateResult as any).affectedRows === 0) {
    await db.insert(settings).values({
      category: 'backtest',
      key: 'minProfitThreshold',
      value: value.toString(),
      valueType: 'number',
      description: 'Minimum profit % threshold for saving full backtest results',
    });
  }
}

/**
 * Get the worker timeout setting (in seconds)
 * This controls how long a Python worker process can run before being killed
 * 
 * Different defaults per backtest type:
 * - timing: 60 seconds (simple end-of-day trades)
 * - indicator: 90 seconds (signal-based, more complex)
 * - discovery: 300 seconds (multi-stage, many iterations)
 * 
 * @param backtestType - 'timing' | 'indicator' | 'discovery'
 */
export async function getWorkerTimeoutSeconds(backtestType: 'timing' | 'indicator' | 'discovery' = 'timing'): Promise<number> {
  // Default timeouts per backtest type
  const defaults: Record<string, number> = {
    timing: 60,
    indicator: 90,
    discovery: 300,
  };
  const defaultValue = defaults[backtestType] || 60;
  
  const db = await getDb();
  if (!db) return defaultValue;
  
  const results = await db.select()
    .from(settings)
    .where(and(
      eq(settings.category, 'backtest'),
      eq(settings.key, 'workerTimeoutSeconds')
    ))
    .limit(1);
  
  if (results.length === 0) return defaultValue;
  const value = parseInt(results[0].value);
  // Clamp between 30 and 1200 seconds (20 minutes max)
  return Math.max(30, Math.min(1200, value)) || defaultValue;
}
