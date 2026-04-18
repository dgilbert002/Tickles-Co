/**
 * Strategy Router - Handles strategy library management
 * Supports DNA strand management, window configuration, and strategy runs
 */

import { router, publicProcedure } from '../_core/trpc';
import { z } from 'zod';
import { getDb } from '../db';
import { savedStrategies, strategyRuns, backtestResults, backtestRuns, marketInfo, epics, accounts } from '../../drizzle/schema';
import { eq, desc, and, inArray } from 'drizzle-orm';
import type { MySql2Database } from 'drizzle-orm/mysql2';
import { refreshWebSocketSubscriptions } from '../services/candle_data_service';

/**
 * Normalize time string to HH:MM:SS format
 * "21:00" -> "21:00:00"
 * "21:00:00" -> "21:00:00"
 * "1:00" -> "01:00:00"
 */
function normalizeTimeFormat(time: string): string {
  if (!time) return '21:00:00';
  
  const parts = time.split(':');
  const hours = parts[0].padStart(2, '0');
  const minutes = (parts[1] || '00').padStart(2, '0');
  const seconds = (parts[2] || '00').padStart(2, '0');
  
  return `${hours}:${minutes}:${seconds}`;
}

/**
 * Calculate actual trade close time based on timing config
 */
function calculateActualCloseTime(marketCloseTime: string, timingConfig: any): string {
  // Parse market close time (HH:MM:SS)
  const [hours, minutes, seconds] = marketCloseTime.split(':').map(Number);
  let totalSeconds = hours * 3600 + minutes * 60 + seconds;

  // Get offset based on timing mode
  let offsetSeconds = 30; // Default ManusTime offset
  if (timingConfig.mode === 'OriginalBotTime') {
    offsetSeconds = 30; // T-30s close
  } else if (timingConfig.mode === 'Custom' && timingConfig.close_offset_seconds !== undefined) {
    offsetSeconds = timingConfig.close_offset_seconds;
  } else if (timingConfig.closeTradesOffset !== undefined) {
    offsetSeconds = timingConfig.closeTradesOffset;
  }

  // Subtract offset
  totalSeconds -= offsetSeconds;
  if (totalSeconds < 0) totalSeconds += 86400; // Handle day wrap

  // Convert back to HH:MM:SS
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/**
 * ============================================================================
 * Auto-detect trading windows at STRATEGY CREATION/UPDATE TIME
 * ============================================================================
 * 
 * WHEN CALLED: This function runs when:
 * - Creating a new strategy with DNA strands
 * - Adding DNA strands to an existing strategy
 * - Removing DNA strands from a strategy
 * 
 * DATA SOURCE: Uses marketInfo.marketCloseTime (updated hourly from Capital.com)
 * This gives a "snapshot" of close times at strategy creation time.
 * 
 * ⚠️ IMPORTANT - RUNTIME RESOLUTION:
 * At actual trade execution time, close times are resolved DYNAMICALLY by:
 * - server/services/window_resolver.ts (using epics.nextClose)
 * - server/orchestration/dna_brain_calculator.ts::loadStrategyDNAStrands()
 * 
 * This means even if THIS function saves "20:00:00", runtime will resolve
 * the CURRENT close time (e.g., "01:00:00" for extended hours).
 * 
 * All times are in UTC (Capital.com's server time).
 * ============================================================================
 */
async function detectWindows(
  dnaStrands: any[],
  db: MySql2Database<Record<string, unknown>>
): Promise<any> {
  if (dnaStrands.length === 0) {
    return { windows: [] };
  }

  // Get unique epics from DNA strands
  const uniqueEpics = Array.from(new Set(dnaStrands.map(d => d.epic)));

  // Fetch ACTUAL market close times from marketInfo table (updated hourly from Capital.com)
  const marketInfoRecords = await db
    .select({
      epic: marketInfo.epic,
      marketCloseTime: marketInfo.marketCloseTime,
    })
    .from(marketInfo)
    .where(inArray(marketInfo.epic, uniqueEpics));

  // Also get tradingHoursType from epics table for window naming
  const epicRecords = await db
    .select({
      symbol: epics.symbol,
      tradingHoursType: epics.tradingHoursType,
    })
    .from(epics)
    .where(inArray(epics.symbol, uniqueEpics));

  // Create epic -> actual close time map
  const epicCloseTimeMap = new Map<string, string>();
  marketInfoRecords.forEach(info => {
    epicCloseTimeMap.set(info.epic, info.marketCloseTime);
  });

  // Create epic -> tradingHoursType map (for naming only)
  const epicHoursTypeMap = new Map<string, string>();
  epicRecords.forEach(epic => {
    epicHoursTypeMap.set(epic.symbol, epic.tradingHoursType || 'regular');
  });

  // Group DNA strands by their ACTUAL close time (not hardcoded!)
  const windowGroups = new Map<string, { strands: any[]; epics: Set<string> }>();
  
  dnaStrands.forEach(strand => {
    // Get actual close time from marketInfo, or use a sensible default
    // ALWAYS normalize to HH:MM:SS for consistent lookups
    const rawCloseTime = epicCloseTimeMap.get(strand.epic) || '21:00:00';
    const closeTime = normalizeTimeFormat(rawCloseTime);
    
    if (!windowGroups.has(closeTime)) {
      windowGroups.set(closeTime, { strands: [], epics: new Set() });
    }
    windowGroups.get(closeTime)!.strands.push(strand);
    windowGroups.get(closeTime)!.epics.add(strand.epic);
  });

  // Sort windows by close time (chronologically)
  const sortedCloseTimes = Array.from(windowGroups.keys()).sort();

  // Generate window configuration with ACTUAL close times
  const windows = sortedCloseTimes.map((closeTime, index) => {
    const group = windowGroups.get(closeTime)!;
    const epicsInWindow = Array.from(group.epics);
    
    // Determine trading hours type from first epic (for naming)
    const firstEpicType = epicHoursTypeMap.get(epicsInWindow[0]) || 'regular';
    
    // Generate descriptive window name based on close time
    const hour = parseInt(closeTime.split(':')[0]);
    let windowName = `Window ${closeTime} UTC`;
    if (hour === 21) windowName = 'Regular Hours Close (4pm ET)';
    else if (hour === 1 || hour === 0) windowName = 'Extended Hours Close (8pm ET)';
    else if (hour === 22) windowName = 'Crypto Close';
    
    return {
      id: `window_${index + 1}`,
      closeTime: closeTime, // ACTUAL close time from marketInfo!
      tradingHoursType: firstEpicType,
      windowName,
      allocationPct: Math.floor(100 / sortedCloseTimes.length), // Equal split
      carryOver: index < sortedCloseTimes.length - 1,
      conflictResolutionMetric: 'sharpeRatio',
      dnaStrandIds: group.strands.map(s => s.id),
      epics: epicsInWindow, // Include epics for debugging
    };
  });

  console.log(`[detectWindows] Detected ${windows.length} windows from marketInfo:`, 
    windows.map(w => `${w.closeTime} (${w.epics?.join(', ')})`));

  return { 
    windows,
    totalAccountBalancePct: 99,
  };
}

// DNA Strand schema for validation
const dnaStrandSchema = z.object({
  id: z.string(),
  sourceType: z.enum(['indicator', 'backtest']),
  sourceTestId: z.number(),
  sourceRunId: z.number().optional(),
  indicatorName: z.string(),
  indicatorParams: z.record(z.string(), z.any()),
  epic: z.string(),
  timeframe: z.string(),
  direction: z.enum(['long', 'short', 'both']),
  leverage: z.number(),
  stopLoss: z.number(),
  profitability: z.number(),
  winRate: z.number(),
  sharpeRatio: z.number(),
  maxDrawdown: z.number(),
  totalTrades: z.number(),
  lastTestedDate: z.string(),
  isActive: z.boolean(),
  isPaused: z.boolean(),
  addedAt: z.string(),
});

// Window configuration schema
const windowConfigSchema = z.object({
  windows: z.array(z.object({
    id: z.string(),
    closeTime: z.string(),
    windowName: z.string(),
    allocationPct: z.number(),
    carryOver: z.boolean(),
    conflictResolutionMetric: z.string(),
    dnaStrandIds: z.array(z.string()),
  })),
  totalAccountBalancePct: z.number(),
});

export const strategiesRouter = router({
  // Get all strategies for current user
  getAll: publicProcedure.query(async ({ ctx }) => {
    const db = await getDb();
    if (!db || !ctx.user) return [];

    const strategies = await db
      .select()
      .from(savedStrategies)
      .where(eq(savedStrategies.userId, ctx.user.id))
      .orderBy(desc(savedStrategies.updatedAt));

    return strategies;
  }),

  // Get single strategy by ID
  getById: publicProcedure
    .input(z.object({ id: z.number() }))
    .query(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) return null;

      const result = await db
        .select()
        .from(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.id),
            eq(savedStrategies.userId, ctx.user.id)
          )
        )
        .limit(1);

      return result.length > 0 ? result[0] : null;
    }),

  // Create new strategy
  create: publicProcedure
    .input(z.object({
      name: z.string(),
      description: z.string().optional(),
      resultIds: z.array(z.number()).optional(), // Optional array of backtest result IDs to add as DNA strands
    }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Create empty strategy first
      const result = await db.insert(savedStrategies).values({
        userId: ctx.user.id,
        name: input.name,
        description: input.description || null,
        dnaStrands: [],
        windowConfig: null,
        isConfigured: false,
        isFavorite: false,
        tags: null,
      });

      // Drizzle returns insertId in the result object
      const insertId = Number((result as any).insertId || (result as any)[0]?.insertId);

      // If resultIds provided, add them as DNA strands
      if (input.resultIds && input.resultIds.length > 0) {
        // Fetch all backtest results
        const backtests = await db
          .select()
          .from(backtestResults)
          .where(inArray(backtestResults.id, input.resultIds));

        if (backtests.length === 0) {
          throw new Error('No valid backtest results found');
        }

        // Create DNA strands from backtests with FULL configuration
        const dnaStrands = backtests.map(source => ({
          id: `dna_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
          sourceType: 'backtest' as const,
          sourceTestId: source.id,
          sourceRunId: source.runId,
          indicatorName: source.indicatorName,
          indicatorParams: source.indicatorParams,
          epic: source.epic,
          timeframe: source.timeframe,
          direction: 'long' as 'long' | 'short' | 'both',
          leverage: source.leverage,
          stopLoss: source.stopLoss,
          // Original backtest date range - preserved for display
          startDate: source.startDate,
          endDate: source.endDate,
          // Data source configuration (default to Capital.com for realistic backtesting)
          dataSource: (source as any).dataSource || 'capital',
          // Timing configuration (default to most realistic mode)
          timingConfig: source.timingConfig || { mode: 'Fake5min_3rdCandle_API' },
          // Signal-based configuration (if enabled)
          signalBasedConfig: (source as any).signalBasedConfig || null,
          // Performance metrics
          profitability: source.finalBalance - source.initialBalance,
          winRate: source.winRate,
          sharpeRatio: source.sharpeRatio,
          maxDrawdown: source.maxDrawdown,
          totalTrades: source.totalTrades,
          lastTestedDate: source.createdAt.toISOString(),
          isActive: true,
          isPaused: false,
          addedAt: new Date().toISOString(),
        }));

        // Auto-detect windows from DNA strands
        const windowConfig = await detectWindows(dnaStrands, db);

        // Update strategy with DNA strands and windows
        await db
          .update(savedStrategies)
          .set({
            dnaStrands: dnaStrands,
            windowConfig: windowConfig,
            isConfigured: false,
          })
          .where(
            and(
              eq(savedStrategies.id, insertId),
              eq(savedStrategies.userId, ctx.user.id)
            )
          );
      }

      return { id: insertId, success: true };
    }),

  // Update strategy (name, description, favorite, tags, archived)
  update: publicProcedure
    .input(z.object({
      id: z.number(),
      name: z.string().optional(),
      description: z.string().optional(),
      isFavorite: z.boolean().optional(),
      tags: z.array(z.string()).optional(),
      isArchived: z.boolean().optional(),
    }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      const updateData: any = {};
      if (input.name !== undefined) updateData.name = input.name;
      if (input.description !== undefined) updateData.description = input.description;
      if (input.isFavorite !== undefined) updateData.isFavorite = input.isFavorite;
      if (input.tags !== undefined) updateData.tags = input.tags;
      if (input.isArchived !== undefined) {
        updateData.isArchived = input.isArchived;
        updateData.archivedAt = input.isArchived ? new Date() : null;
        updateData.archivedBy = input.isArchived ? ctx.user.id : null;
      }

      await db
        .update(savedStrategies)
        .set(updateData)
        .where(
          and(
            eq(savedStrategies.id, input.id),
            eq(savedStrategies.userId, ctx.user.id)
          )
        );

      return { success: true };
    }),

  // Archive strategy (soft delete - preserves DNA for historical comparison)
  archive: publicProcedure
    .input(z.object({ id: z.number() }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Import accounts schema for checking assignments
      const { accounts } = await import('../../drizzle/schema');

      // Check if strategy is assigned to any account
      const assignedAccounts = await db
        .select({ id: accounts.id, accountName: accounts.accountName })
        .from(accounts)
        .where(eq(accounts.assignedStrategyId, input.id));

      if (assignedAccounts.length > 0) {
        const accountNames = assignedAccounts.map(a => a.accountName).join(', ');
        throw new Error(
          `Cannot archive strategy: It is currently assigned to ${assignedAccounts.length} account(s): ${accountNames}. ` +
          `Please remove the strategy from these accounts in the Bot Dashboard first.`
        );
      }

      // Archive (soft delete) - preserves DNA for historical trade comparison
      await db
        .update(savedStrategies)
        .set({
          isArchived: true,
          archivedAt: new Date(),
          archivedBy: ctx.user.id,
        })
        .where(
          and(
            eq(savedStrategies.id, input.id),
            eq(savedStrategies.userId, ctx.user.id)
          )
        );

      return { success: true };
    }),

  // Delete strategy
  delete: publicProcedure
    .input(z.object({ id: z.number() }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Import accounts schema for checking assignments
      const { accounts } = await import('../../drizzle/schema');

      // Check if strategy is assigned to any account
      const assignedAccounts = await db
        .select({ id: accounts.id, accountName: accounts.accountName })
        .from(accounts)
        .where(eq(accounts.assignedStrategyId, input.id));

      if (assignedAccounts.length > 0) {
        const accountNames = assignedAccounts.map(a => a.accountName).join(', ');
        throw new Error(
          `Cannot delete strategy: It is currently assigned to ${assignedAccounts.length} account(s): ${accountNames}. ` +
          `Please remove the strategy from these accounts in the Bot Dashboard first.`
        );
      }

      // Safe to delete - strategy is not assigned to any accounts
      await db
        .delete(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.id),
            eq(savedStrategies.userId, ctx.user.id)
          )
        );

      return { success: true };
    }),

  // Delete a strategy test result (strategyRuns)
  deleteTestResult: publicProcedure
    .input(z.object({ id: z.number() }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Delete the strategy run
      await db
        .delete(strategyRuns)
        .where(
          and(
            eq(strategyRuns.id, input.id),
            eq(strategyRuns.userId, ctx.user.id)
          )
        );

      return { success: true };
    }),

  // Add test to existing strategy (alias for addDnaStrand for backward compatibility)
  addTest: publicProcedure
    .input(z.object({
      strategyId: z.number(),
      resultId: z.number(), // backtestResults.id
    }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Get the source backtest result
      const backtest = await db
        .select()
        .from(backtestResults)
        .where(eq(backtestResults.id, input.resultId))
        .limit(1);

      if (backtest.length === 0) {
        throw new Error('Source backtest not found');
      }

      const source = backtest[0];

      // Get the backtest run to fetch investmentPct
      const backtestRun = await db
        .select()
        .from(backtestRuns)
        .where(eq(backtestRuns.id, source.runId))
        .limit(1);

      const runConfig = backtestRun[0] || {};

      // Get current strategy
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        )
        .limit(1);

      if (strategy.length === 0) {
        throw new Error('Strategy not found');
      }

      const currentDnaStrands = strategy[0].dnaStrands || [];

      // Create new DNA strand with FULL configuration from backtest
      const newDnaStrand = {
        id: `dna_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
        sourceType: 'backtest' as const,
        sourceTestId: source.id,
        sourceRunId: source.runId,
        indicatorName: source.indicatorName,
        indicatorParams: source.indicatorParams,
        epic: source.epic,
        timeframe: source.timeframe,
        direction: 'long' as 'long' | 'short' | 'both',
        leverage: source.leverage,
        stopLoss: source.stopLoss,
        // Investment percentage - CRITICAL for matching backtest results
        investmentPct: runConfig.investmentPct || 99.0,
        // Crash protection - MUST match backtest for consistent signals
        crashProtectionEnabled: source.crashProtectionEnabled || false,
        // HMH (Hold Means Hold) - if enabled, HOLD signals set new SL instead of closing
        hmhEnabled: source.hmhEnabled || false,
        hmhStopLossOffset: source.hmhStopLossOffset ?? null,  // 0=original, -1=SL-1%, -2=SL-2%
        // Guaranteed stop loss - pays extra spread but no slippage (default OFF)
        guaranteedStopEnabled: false,
        // Original backtest date range - preserved for display
        startDate: source.startDate,
        endDate: source.endDate,
        // Data source configuration (default to Capital.com for realistic backtesting)
        dataSource: (source as any).dataSource || 'capital',
        // Timing configuration (default to most realistic mode)
        timingConfig: source.timingConfig || { mode: 'Fake5min_3rdCandle_API' },
        // Signal-based configuration (if enabled)
        signalBasedConfig: (source as any).signalBasedConfig || null,
        // Performance metrics
        profitability: source.finalBalance - source.initialBalance,
        winRate: source.winRate,
        sharpeRatio: source.sharpeRatio,
        maxDrawdown: source.maxDrawdown,
        totalTrades: source.totalTrades,
        lastTestedDate: source.createdAt.toISOString(),
        isActive: true,
        isPaused: false,
        addedAt: new Date().toISOString(),
      };

      // Add to DNA strands array
      const updatedDnaStrands = [...currentDnaStrands, newDnaStrand];

      // Auto-detect windows from all DNA strands
      const windowConfig = await detectWindows(updatedDnaStrands, db);

      // Update strategy (mark as not configured since we added new DNA)
      await db
        .update(savedStrategies)
        .set({
          dnaStrands: updatedDnaStrands,
          windowConfig: windowConfig,
          isConfigured: false,
        })
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        );

      return { success: true, dnaStrandId: newDnaStrand.id };
    }),

  // Add DNA strand to strategy
  addDnaStrand: publicProcedure
    .input(z.object({
      strategyId: z.number(),
      sourceTestId: z.number(), // backtestResults.id
    }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Get the source backtest result
      const backtest = await db
        .select()
        .from(backtestResults)
        .where(eq(backtestResults.id, input.sourceTestId))
        .limit(1);

      if (backtest.length === 0) {
        throw new Error('Source backtest not found');
      }

      const source = backtest[0];

      // Get the backtest run to fetch investmentPct
      const backtestRun = await db
        .select()
        .from(backtestRuns)
        .where(eq(backtestRuns.id, source.runId))
        .limit(1);

      const runConfig = backtestRun[0] || {};

      // Get current strategy
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        )
        .limit(1);

      if (strategy.length === 0) {
        throw new Error('Strategy not found');
      }

      const currentDnaStrands = strategy[0].dnaStrands || [];

      // Create new DNA strand with FULL configuration from backtest
      const newDnaStrand = {
        id: `dna_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
        sourceType: 'backtest' as const,
        sourceTestId: source.id,
        sourceRunId: source.runId,
        indicatorName: source.indicatorName,
        indicatorParams: source.indicatorParams,
        epic: source.epic,
        timeframe: source.timeframe,
        direction: 'long' as 'long' | 'short' | 'both',
        leverage: source.leverage,
        stopLoss: source.stopLoss,
        // Investment percentage - CRITICAL for matching backtest results
        investmentPct: runConfig.investmentPct || 99.0,
        // Crash protection - MUST match backtest for consistent signals
        crashProtectionEnabled: source.crashProtectionEnabled || false,
        // HMH (Hold Means Hold) - if enabled, HOLD signals set new SL instead of closing
        hmhEnabled: source.hmhEnabled || false,
        hmhStopLossOffset: source.hmhStopLossOffset ?? null,  // 0=original, -1=SL-1%, -2=SL-2%
        // Guaranteed stop loss - pays extra spread but no slippage (default OFF)
        guaranteedStopEnabled: false,
        // Original backtest date range - preserved for display
        startDate: source.startDate,
        endDate: source.endDate,
        // Data source configuration (default to Capital.com for realistic backtesting)
        dataSource: (source as any).dataSource || 'capital',
        // Timing configuration (default to most realistic mode)
        timingConfig: source.timingConfig || { mode: 'Fake5min_3rdCandle_API' },
        // Signal-based configuration (if enabled)
        signalBasedConfig: (source as any).signalBasedConfig || null,
        // Performance metrics
        profitability: source.finalBalance - source.initialBalance,
        winRate: source.winRate,
        sharpeRatio: source.sharpeRatio,
        maxDrawdown: source.maxDrawdown,
        totalTrades: source.totalTrades,
        lastTestedDate: source.createdAt.toISOString(),
        isActive: true,
        isPaused: false,
        addedAt: new Date().toISOString(),
      };

      // Add to DNA strands array
      const updatedDnaStrands = [...currentDnaStrands, newDnaStrand];

      // Auto-detect windows from all DNA strands
      const windowConfig = await detectWindows(updatedDnaStrands, db);

      // Update strategy (mark as not configured since we added new DNA)
      await db
        .update(savedStrategies)
        .set({
          dnaStrands: updatedDnaStrands,
          windowConfig: windowConfig,
          isConfigured: false, // Needs reconfiguration
        })
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        );

      // Refresh WebSocket subscriptions if this strategy is assigned to active accounts
      // (new epic added might need subscription)
      await refreshWebSocketSubscriptions();
      return { success: true, dnaStrandId: newDnaStrand.id };
    }),

  // Update DNA strand settings (e.g., guaranteedStopEnabled)
  updateDnaStrand: publicProcedure
    .input(z.object({
      strategyId: z.number(),
      dnaStrandId: z.string(),
      updates: z.object({
        guaranteedStopEnabled: z.boolean().optional(),
        leverage: z.number().optional(),
        stopLoss: z.number().optional(),
        isActive: z.boolean().optional(),
        isPaused: z.boolean().optional(),
      }),
    }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Get current strategy
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        )
        .limit(1);

      if (strategy.length === 0) {
        throw new Error('Strategy not found');
      }

      const currentDnaStrands = strategy[0].dnaStrands || [];

      // Find and update the DNA strand
      const updatedDnaStrands = currentDnaStrands.map((dna: any) => {
        if (dna.id === input.dnaStrandId) {
          return {
            ...dna,
            ...input.updates,
          };
        }
        return dna;
      });

      // Check if DNA strand was found
      const dnaFound = updatedDnaStrands.some((dna: any) => dna.id === input.dnaStrandId);
      if (!dnaFound) {
        throw new Error('DNA strand not found');
      }

      // Update strategy
      await db
        .update(savedStrategies)
        .set({
          dnaStrands: updatedDnaStrands,
        })
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        );

      console.log(`[Strategies] Updated DNA strand ${input.dnaStrandId} with:`, input.updates);

      return { success: true };
    }),

  // Remove DNA strand from strategy
  removeDnaStrand: publicProcedure
    .input(z.object({
      strategyId: z.number(),
      dnaStrandId: z.string(),
    }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Get current strategy
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        )
        .limit(1);

      if (strategy.length === 0) {
        throw new Error('Strategy not found');
      }

      const currentDnaStrands = strategy[0].dnaStrands || [];
      const currentWindowConfig = strategy[0].windowConfig as any;

      // Filter out the DNA strand
      const updatedDnaStrands = currentDnaStrands.filter(
        (strand: any) => strand.id !== input.dnaStrandId
      );

      // Also update windowConfig to remove the DNA strand ID from all windows
      let updatedWindowConfig: any = currentWindowConfig;
      let hasWindows = false;
      
      if (currentWindowConfig?.windows) {
        // First, remove the DNA strand ID from all windows
        let updatedWindows = currentWindowConfig.windows.map((window: any) => ({
          ...window,
          dnaStrandIds: (window.dnaStrandIds || []).filter(
            (id: string) => id !== input.dnaStrandId
          ),
        }));
        
        // Remove any windows that have no DNA strands left (ghost windows)
        const nonEmptyWindows = updatedWindows.filter(
          (window: any) => window.dnaStrandIds && window.dnaStrandIds.length > 0
        );
        
        // Log if we're removing empty windows
        if (nonEmptyWindows.length < updatedWindows.length) {
          console.log(`[RemoveDnaStrand] Removing ${updatedWindows.length - nonEmptyWindows.length} empty window(s) from strategy ${input.strategyId}`);
        }
        
        // Redistribute allocation % to remaining windows
        if (nonEmptyWindows.length > 0 && nonEmptyWindows.length < updatedWindows.length) {
          // Calculate equal split with remainder going to last window
          const pctPerWindow = Math.floor(100 / nonEmptyWindows.length);
          const remainder = 100 - (pctPerWindow * nonEmptyWindows.length);
          
          nonEmptyWindows.forEach((window: any, idx: number) => {
            window.allocationPct = idx === nonEmptyWindows.length - 1 
              ? pctPerWindow + remainder  // Last window gets remainder
              : pctPerWindow;
          });
          
          console.log(`[RemoveDnaStrand] Redistributed allocation: ${nonEmptyWindows.map((w: any) => w.allocationPct + '%').join(', ')}`);
        }
        
        hasWindows = nonEmptyWindows.length > 0;
        
        if (hasWindows) {
          updatedWindowConfig = {
            ...currentWindowConfig,
            windows: nonEmptyWindows,
          };
        } else {
          // All windows removed - clear windowConfig entirely
          updatedWindowConfig = null;
          console.log(`[RemoveDnaStrand] All windows removed from strategy ${input.strategyId}`);
        }
      }

      // Update strategy
      await db
        .update(savedStrategies)
        .set({
          dnaStrands: updatedDnaStrands,
          windowConfig: updatedWindowConfig,
          isConfigured: false, // Needs reconfiguration after DNA change
        })
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        );

      // Refresh WebSocket subscriptions if this strategy is assigned to active accounts
      // (epic removed might need unsubscription)
      await refreshWebSocketSubscriptions();
      return { success: true };
    }),

  // Toggle DNA strand pause status
  toggleDnaStrandPause: publicProcedure
    .input(z.object({
      strategyId: z.number(),
      dnaStrandId: z.string(),
    }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Get current strategy
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        )
        .limit(1);

      if (strategy.length === 0) {
        throw new Error('Strategy not found');
      }

      const currentDnaStrands = strategy[0].dnaStrands || [];

      // Toggle pause status
      const updatedDnaStrands = currentDnaStrands.map((strand: any) => {
        if (strand.id === input.dnaStrandId) {
          return { ...strand, isPaused: !strand.isPaused };
        }
        return strand;
      });

      // Update strategy
      await db
        .update(savedStrategies)
        .set({ dnaStrands: updatedDnaStrands })
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        );

      return { success: true };
    }),

  // Detect windows from DNA strands using actual epic close times
  detectWindows: publicProcedure
    .input(z.object({ strategyId: z.number() }))
    .query(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Get strategy with DNA strands
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        )
        .limit(1);

      if (strategy.length === 0) {
        throw new Error('Strategy not found');
      }

      const dnaStrands = strategy[0].dnaStrands || [];
      if (dnaStrands.length === 0) {
        return {
          windows: [],
          totalAccountBalancePct: 99,
        };
      }

      // Get unique epics from DNA strands
      const uniqueEpics = Array.from(new Set(dnaStrands.map((s: any) => s.epic)));

      // Fetch market info for all epics
      const marketInfoRecords = await db
        .select()
        .from(marketInfo)
        .where(inArray(marketInfo.epic, uniqueEpics));

      // Create epic -> closeTime map
      const epicCloseTimeMap = new Map<string, string>();
      marketInfoRecords.forEach(info => {
        epicCloseTimeMap.set(info.epic, info.marketCloseTime);
      });

      // Group DNA strands by close time
      const closeTimeGroups = new Map<string, any[]>();
      dnaStrands.forEach((strand: any) => {
        // Default to 21:00 UTC (4pm ET) if no close time found - must be UTC!
        const closeTime = epicCloseTimeMap.get(strand.epic) || '21:00:00';
        if (!closeTimeGroups.has(closeTime)) {
          closeTimeGroups.set(closeTime, []);
        }
        closeTimeGroups.get(closeTime)!.push(strand);
      });

      // Create windows from groups
      const windows = Array.from(closeTimeGroups.entries())
        .sort((a, b) => a[0].localeCompare(b[0])) // Sort by close time
        .map(([closeTime, strands], index, allWindows) => {
          // Determine window name based on close time
          let windowName = `Window ${index + 1}`;
          const hour = parseInt(closeTime.split(':')[0]);
          
          if (hour === 16 || hour === 4) {
            windowName = 'Regular Hours (4:00 PM ET)';
          } else if (hour === 20 || hour === 8) {
            windowName = 'Extended Hours (8:00 PM ET)';
          } else if (hour === 13 || hour === 1) {
            windowName = 'Early Close (1:00 PM ET)';
          }

          // Last window (W4) cannot carry over (market closes, new day starts)
          const isLastWindow = index === allWindows.length - 1;

          return {
            id: `window-${closeTime.replace(/:/g, '')}`,
            closeTime,
            windowName,
            allocationPct: Math.floor(100 / closeTimeGroups.size),
            carryOver: !isLastWindow, // Default ON except for last window
            conflictResolutionMetric: 'sharpeRatio',
            dnaStrandIds: strands.map((s: any) => s.id),
          };
        });

      // Adjust last window to make total exactly 100%
      if (windows.length > 0) {
        const totalSoFar = windows.slice(0, -1).reduce((sum, w) => sum + w.allocationPct, 0);
        windows[windows.length - 1].allocationPct = 100 - totalSoFar;
      }

      return {
        windows,
        totalAccountBalancePct: 99,
      };
    }),

  // Save window configuration
  saveWindowConfig: publicProcedure
    .input(z.object({
      strategyId: z.number(),
      windowConfig: windowConfigSchema,
    }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Validate total allocation
      const totalAllocation = input.windowConfig.windows.reduce((sum, w) => sum + w.allocationPct, 0);
      if (Math.abs(totalAllocation - 100) > 0.01) {
        throw new Error(`Total window allocation must equal 100% (got ${totalAllocation}%)`);
      }

      // NORMALIZE all closeTime values to HH:MM:SS format for consistency
      const normalizedWindows = input.windowConfig.windows.map(w => ({
        ...w,
        closeTime: normalizeTimeFormat(w.closeTime),
      }));
      
      const dbWindowConfig = {
        ...input.windowConfig,
        windows: normalizedWindows,
      };

      // Update strategy with window config
      await db
        .update(savedStrategies)
        .set({
          windowConfig: dbWindowConfig as any,
          isConfigured: true, // Mark as configured
        })
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        );

      return { success: true };
    }),

  // Get all strategy runs for current user (for results history page)
  // NOTE: Excludes large JSON columns (trades, dailyBalances) to avoid MySQL sort memory issues
  getAllRuns: publicProcedure
    .query(async ({ ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) return [];

      // Select summary columns with strategy name from JOIN
      const runs = await db
        .select({
          id: strategyRuns.id,
          strategyId: strategyRuns.strategyId,
          strategyName: savedStrategies.name, // JOIN to get strategy name
          userId: strategyRuns.userId,
          startDate: strategyRuns.startDate,
          endDate: strategyRuns.endDate,
          initialBalance: strategyRuns.initialBalance,
          monthlyTopup: strategyRuns.monthlyTopup,
          configSnapshot: strategyRuns.configSnapshot,
          finalBalance: strategyRuns.finalBalance,
          totalContributions: strategyRuns.totalContributions,
          totalReturn: strategyRuns.totalReturn,
          totalTrades: strategyRuns.totalTrades,
          winningTrades: strategyRuns.winningTrades,
          losingTrades: strategyRuns.losingTrades,
          winRate: strategyRuns.winRate,
          maxDrawdown: strategyRuns.maxDrawdown,
          sharpeRatio: strategyRuns.sharpeRatio,
          totalFees: strategyRuns.totalFees,
          totalSpreadCosts: strategyRuns.totalSpreadCosts,
          totalOvernightCosts: strategyRuns.totalOvernightCosts,
          // EXCLUDE: trades, dailyBalances (too large for sorting)
          conflictLogs: strategyRuns.conflictLogs,
          executionTimeMs: strategyRuns.executionTimeMs,
          createdAt: strategyRuns.createdAt,
        })
        .from(strategyRuns)
        .leftJoin(savedStrategies, eq(strategyRuns.strategyId, savedStrategies.id))
        .where(eq(strategyRuns.userId, ctx.user.id))
        .orderBy(desc(strategyRuns.createdAt))
        .limit(100); // Limit results for performance

      return runs;
    }),

  // Get strategy runs (test history) for a specific strategy
  // NOTE: Excludes large JSON columns (trades, dailyBalances) to avoid MySQL sort memory issues
  getRuns: publicProcedure
    .input(z.object({ strategyId: z.number() }))
    .query(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) return [];

      // Select only summary columns, exclude large JSON blobs
      const runs = await db
        .select({
          id: strategyRuns.id,
          strategyId: strategyRuns.strategyId,
          userId: strategyRuns.userId,
          startDate: strategyRuns.startDate,
          endDate: strategyRuns.endDate,
          initialBalance: strategyRuns.initialBalance,
          monthlyTopup: strategyRuns.monthlyTopup,
          configSnapshot: strategyRuns.configSnapshot,
          finalBalance: strategyRuns.finalBalance,
          totalContributions: strategyRuns.totalContributions,
          totalReturn: strategyRuns.totalReturn,
          totalTrades: strategyRuns.totalTrades,
          winningTrades: strategyRuns.winningTrades,
          losingTrades: strategyRuns.losingTrades,
          winRate: strategyRuns.winRate,
          maxDrawdown: strategyRuns.maxDrawdown,
          sharpeRatio: strategyRuns.sharpeRatio,
          totalFees: strategyRuns.totalFees,
          totalSpreadCosts: strategyRuns.totalSpreadCosts,
          totalOvernightCosts: strategyRuns.totalOvernightCosts,
          // EXCLUDE: trades, dailyBalances (too large for sorting)
          conflictLogs: strategyRuns.conflictLogs,
          executionTimeMs: strategyRuns.executionTimeMs,
          createdAt: strategyRuns.createdAt,
        })
        .from(strategyRuns)
        .where(
          and(
            eq(strategyRuns.strategyId, input.strategyId),
            eq(strategyRuns.userId, ctx.user.id)
          )
        )
        .orderBy(desc(strategyRuns.createdAt))
        .limit(50); // Limit results for performance

      return runs;
    }),

  // Test strategy with historical data
  testStrategy: publicProcedure
    .input(z.object({
      strategyId: z.number(),
      startAmount: z.number().min(50),
      monthlyTopup: z.number().min(0),
      dateFrom: z.string(), // YYYY-MM-DD
      dateTo: z.string(), // YYYY-MM-DD
      windowConfig: z.object({
        windows: z.array(z.object({
          id: z.string(),
          closeTime: z.string(),
          allocationPct: z.number(),
          carryOver: z.boolean(),
          conflictResolutionMetric: z.string(),
          dnaStrandIds: z.array(z.string()),
        })),
        totalAccountBalancePct: z.number(),
      }),
    }))
    .mutation(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Get strategy with DNA strands
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        )
        .limit(1);

      if (strategy.length === 0) {
        throw new Error('Strategy not found');
      }

      // Create strategy run record with placeholder results
      // In Phase 5, we'll update this with actual Python execution results
      const [insertResult] = await db
        .insert(strategyRuns)
        .values({
          userId: ctx.user.id,
          strategyId: input.strategyId,
          initialBalance: input.startAmount,
          monthlyTopup: input.monthlyTopup,
          startDate: input.dateFrom,
          endDate: input.dateTo,
          configSnapshot: {
            dnaStrands: strategy[0].dnaStrands || [],
            windowConfig: input.windowConfig,
          } as any,
          // Placeholder results (will be updated by Python executor)
          finalBalance: input.startAmount,
          totalContributions: input.startAmount,
          totalReturn: 0,
          totalTrades: 0,
          winningTrades: 0,
          losingTrades: 0,
          winRate: 0,
          maxDrawdown: 0,
          sharpeRatio: 0,
        });

      const runId = insertResult.insertId;

      // Call Python strategy executor with DNA strands
      const { spawnPython } = await import('../python_spawn');
      const path = await import('path');
      const PYTHON_ENGINE_DIR = path.join(process.cwd(), 'python_engine');
      const STRATEGY_EXECUTOR = path.join(PYTHON_ENGINE_DIR, 'strategy_executor.py');

      // Build config for Python executor
      const pythonConfig = {
        start_date: input.dateFrom,
        end_date: input.dateTo,
        initial_balance: input.startAmount,
        monthly_topup: input.monthlyTopup,
        dna_strands: strategy[0].dnaStrands || [],
        window_config: input.windowConfig,
        calculation_mode: 'standard',
      };

      const configJson = JSON.stringify(pythonConfig);

      // Execute Python strategy
      const startTime = Date.now();
      let pythonResult: any;

      try {
        pythonResult = await new Promise((resolve, reject) => {
          const pythonProcess = spawnPython(STRATEGY_EXECUTOR, {
            args: [configJson],
            cwd: PYTHON_ENGINE_DIR,
            env: { ...process.env, PYTHONUNBUFFERED: '1' },
          });

          let stdout = '';
          let stderr = '';

          pythonProcess.stdout?.on('data', (data: Buffer) => {
            stdout += data.toString();
          });

          pythonProcess.stderr?.on('data', (data: Buffer) => {
            stderr += data.toString();
          });

          pythonProcess.on('close', (code: number) => {
            if (code !== 0) {
              reject(new Error(`Python executor failed: ${stderr}`));
              return;
            }

            // Parse result from stdout
            const resultMatch = stdout.match(/RESULT:(.+)/);
            if (!resultMatch) {
              reject(new Error('No result found in Python output'));
              return;
            }

            try {
              const result = JSON.parse(resultMatch[1]);
              resolve(result);
            } catch (e) {
              reject(new Error(`Failed to parse Python result: ${e}`));
            }
          });

          pythonProcess.on('error', (err: Error) => {
            reject(err);
          });
        });
      } catch (error: any) {
        console.error('[Strategy Test] Python execution failed:', error);
        throw new Error(`Strategy execution failed: ${error.message}`);
      }

      const executionTime = Date.now() - startTime;

      // Update strategy run with actual results
      await db
        .update(strategyRuns)
        .set({
          finalBalance: pythonResult.final_balance,
          totalContributions: pythonResult.total_contributions,
          totalReturn: pythonResult.total_return,
          totalTrades: pythonResult.total_trades,
          winningTrades: pythonResult.winning_trades,
          losingTrades: pythonResult.losing_trades,
          winRate: pythonResult.win_rate,
          maxDrawdown: pythonResult.max_drawdown,
          sharpeRatio: pythonResult.sharpe_ratio,
          totalFees: pythonResult.total_fees || 0,
          totalSpreadCosts: pythonResult.total_spread_costs || 0,
          totalOvernightCosts: pythonResult.total_overnight_costs || 0,
          trades: pythonResult.trades || [],
          executionTimeMs: executionTime,
        })
        .where(eq(strategyRuns.id, runId));

      return {
        runId,
        finalBalance: pythonResult.final_balance,
        totalReturn: pythonResult.total_return,
        winRate: pythonResult.win_rate,
        sharpeRatio: pythonResult.sharpe_ratio,
        maxDrawdown: pythonResult.max_drawdown,
      };
    }),

  // Get indicator trades for comparison
  getIndicatorTrades: publicProcedure
    .input(z.object({
      testIds: z.array(z.number()),
    }))
    .query(async ({ input }) => {
      const db = await getDb();
      if (!db) throw new Error('Database not available');

      // Fetch all trades from the specified backtest results
      const results = await db
        .select()
        .from(backtestResults)
        .where(inArray(backtestResults.id, input.testIds));

      // Extract trades from each result
      const allTrades: any[] = [];
      for (const result of results) {
        const trades = result.trades as any[] || [];
        trades.forEach(trade => {
          allTrades.push({
            id: result.id,
            timestamp: trade.timestamp || trade.date,
            action: trade.action,
            price: trade.price,
            indicatorName: result.indicatorName,
            testId: result.id,
          });
        });
      }

      return allTrades.sort((a, b) => 
        new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      );
    }),

  // Get strategy test result by ID
  getTestResult: publicProcedure
    .input(z.object({ id: z.number() }))
    .query(async ({ input }) => {
      const db = await getDb();
      if (!db) throw new Error('Database not available');

      const [result] = await db
        .select()
        .from(strategyRuns)
        .where(eq(strategyRuns.id, input.id))
        .limit(1);

      if (!result) {
        return null;
      }

      // Ensure trades and dailyBalances are arrays (parse if string)
      const trades = Array.isArray(result.trades) 
        ? result.trades 
        : (typeof result.trades === 'string' ? JSON.parse(result.trades) : []);
      
      const dailyBalances = Array.isArray(result.dailyBalances)
        ? result.dailyBalances
        : (typeof result.dailyBalances === 'string' ? JSON.parse(result.dailyBalances) : []);

      return {
        ...result,
        trades,
        dailyBalances,
      };
    }),

  // Get strategy test progress for live updates
  getTestProgress: publicProcedure
    .input(z.object({
      runId: z.number(),
    }))
    .query(async ({ input }) => {
      const db = await getDb();
      if (!db) throw new Error('Database not available');

      const [run] = await db
        .select()
        .from(strategyRuns)
        .where(eq(strategyRuns.id, input.runId))
        .limit(1);

      if (!run) {
        throw new Error('Strategy run not found');
      }

      // Extract trades and conflicts from the run data
      const trades = run.trades as any[] || [];
      
      // Parse conflicts from trades (when multiple indicators signal at same time)
      const conflicts: any[] = [];
      const tradesByTimestamp = new Map<string, any[]>();
      
      trades.forEach(trade => {
        const timestamp = trade.timestamp || trade.date;
        if (!tradesByTimestamp.has(timestamp)) {
          tradesByTimestamp.set(timestamp, []);
        }
        tradesByTimestamp.get(timestamp)!.push(trade);
      });

      // Identify conflicts (multiple trades at same timestamp)
      tradesByTimestamp.forEach((tradesAtTime, timestamp) => {
        if (tradesAtTime.length > 1 && tradesAtTime.some(t => t.action === 'buy')) {
          const buyTrades = tradesAtTime.filter(t => t.action === 'buy');
          if (buyTrades.length > 1) {
            conflicts.push({
              timestamp,
              indicators: buyTrades.map(t => t.indicator || 'Unknown'),
              winner: buyTrades[0].indicator || 'Unknown',
              reason: 'Highest Sharpe Ratio',
            });
          }
        }
      });

      return {
        status: (run as any).status || 'completed',
        trades: trades.map(t => ({
          timestamp: t.timestamp || t.date,
          action: t.action,
          price: t.price,
          balance: t.balance,
          indicator: t.indicator,
          epic: t.epic,
        })),
        conflicts,
        currentBalance: run.finalBalance || run.initialBalance,
      };
    }),

  // ============================================================================
  // GSL (Guaranteed Stop Loss) Validation Endpoints
  // ============================================================================

  /**
   * Validate if GSL can be enabled for a DNA strand
   * Checks: instrument support, minimum distance, account hedging mode
   */
  validateGslForDna: publicProcedure
    .input(z.object({
      epic: z.string(),
      stopLossPercent: z.number(),
    }))
    .query(async ({ input }) => {
      const db = await getDb();
      if (!db) throw new Error('Database not available');

      // 1. Get market info for the epic
      const marketInfoRecord = await db
        .select()
        .from(marketInfo)
        .where(eq(marketInfo.epic, input.epic))
        .limit(1);

      if (marketInfoRecord.length === 0) {
        return {
          canEnableGsl: false,
          gslAllowed: false,
          minGslDistancePct: 0,
          dnaStopLossPct: input.stopLossPercent,
          stopLossTooSmall: false,
          errors: [`Market info not found for ${input.epic}. Please refresh market data.`],
        };
      }

      const info = marketInfoRecord[0];
      const gslAllowed = info.guaranteedStopAllowed ?? false;
      const minGslDistancePct = parseFloat(info.minGuaranteedStopDistancePct?.toString() || '0');
      const stopLossTooSmall = input.stopLossPercent < minGslDistancePct;
      
      // Build errors array
      const errors: string[] = [];
      
      if (!gslAllowed) {
        errors.push(`GSL is not available for ${input.epic}`);
      }
      
      if (stopLossTooSmall) {
        errors.push(`Stop loss (${input.stopLossPercent}%) is below minimum GSL distance (${minGslDistancePct}%)`);
      }

      const canEnableGsl = gslAllowed && !stopLossTooSmall;

      console.log(`[Strategies] GSL validation for ${input.epic}: allowed=${gslAllowed}, minDist=${minGslDistancePct}%, stopLoss=${input.stopLossPercent}%, canEnable=${canEnableGsl}`);

      return {
        canEnableGsl,
        gslAllowed,
        minGslDistancePct,
        dnaStopLossPct: input.stopLossPercent,
        stopLossTooSmall,
        scalingFactor: info.scalingFactor ?? 2,
        errors,
      };
    }),

  /**
   * Get GSL info for all DNA strands in a strategy
   * Returns validation status for each DNA strand
   */
  getGslInfoForStrategy: publicProcedure
    .input(z.object({
      strategyId: z.number(),
    }))
    .query(async ({ input, ctx }) => {
      const db = await getDb();
      if (!db || !ctx.user) throw new Error('Database not available or user not authenticated');

      // Get strategy
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(
          and(
            eq(savedStrategies.id, input.strategyId),
            eq(savedStrategies.userId, ctx.user.id)
          )
        )
        .limit(1);

      if (strategy.length === 0) {
        throw new Error('Strategy not found');
      }

      const dnaStrands = strategy[0].dnaStrands || [];
      if (dnaStrands.length === 0) {
        return { gslInfo: {} };
      }

      // Get unique epics
      const uniqueEpics = Array.from(new Set(dnaStrands.map((s: any) => s.epic)));

      // Fetch market info for all epics
      const marketInfoRecords = await db
        .select()
        .from(marketInfo)
        .where(inArray(marketInfo.epic, uniqueEpics));

      // Create epic -> market info map
      const epicInfoMap = new Map<string, typeof marketInfoRecords[0]>();
      marketInfoRecords.forEach(info => {
        epicInfoMap.set(info.epic, info);
      });

      // Build GSL info for each DNA strand
      const gslInfo: Record<string, {
        dnaStrandId: string;
        epic: string;
        stopLossPercent: number;
        guaranteedStopEnabled: boolean;
        gslAllowed: boolean;
        minGslDistancePct: number;
        stopLossTooSmall: boolean;
        canEnableGsl: boolean;
        errors: string[];
      }> = {};

      for (const dna of dnaStrands as any[]) {
        const info = epicInfoMap.get(dna.epic);
        const gslAllowed = info?.guaranteedStopAllowed ?? false;
        const minGslDistancePct = parseFloat(info?.minGuaranteedStopDistancePct?.toString() || '0');
        const stopLossTooSmall = (dna.stopLoss || 0) < minGslDistancePct;
        
        const errors: string[] = [];
        if (!gslAllowed) {
          errors.push(`GSL not available for ${dna.epic}`);
        }
        if (stopLossTooSmall) {
          errors.push(`SL ${dna.stopLoss}% < min ${minGslDistancePct}%`);
        }

        gslInfo[dna.id] = {
          dnaStrandId: dna.id,
          epic: dna.epic,
          stopLossPercent: dna.stopLoss || 0,
          guaranteedStopEnabled: dna.guaranteedStopEnabled || false,
          gslAllowed,
          minGslDistancePct,
          stopLossTooSmall,
          canEnableGsl: gslAllowed && !stopLossTooSmall,
          errors,
        };
      }

      return { gslInfo };
    }),
});
