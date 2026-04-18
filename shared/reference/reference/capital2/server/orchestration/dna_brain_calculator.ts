/**
 * DNA Brain Calculator
 * 
 * Calculates brain signal for a SINGLE DNA strand at its specific timing trigger.
 * This is the per-DNA equivalent of brainPreview.
 * 
 * Called by timer.ts when a specific timing mode triggers:
 * - T-300s: T5BeforeClose DNA strands
 * - T-240s: T4BeforeClose DNA strands  
 * - ~T-120s: Fake5min_3rdCandle_API (WebSocket triggered)
 * - T-60s: Fake5min_4thCandle (REST API triggered)
 */

import { getCurrentSignal, SignalConfig } from '../signal_bridge';
import { getDb } from '../db';
import { savedStrategies, backtestResults, accounts } from '../../drizzle/schema';
import { eq, inArray } from 'drizzle-orm';
import { orchestrationLogger } from './logger';
import { dnaBrainAccumulator, DNABrainResult } from './dna_brain_accumulator';
import { fundTracker } from './fund_tracker';
import { tradeQueue } from './trade_queue';
import type { BrainDecision } from './brain_orchestrator';
import { storeBrainResult } from './brain_orchestrator';
// ============================================================================
// PHASE 2.2: Import window resolver for dynamic close time resolution
// ============================================================================
import { resolveWindowCloseTime, resolveCloseTimesForEpics, closeTimesMatch } from '../services/window_resolver';

/**
 * Normalize time string to HH:MM:SS format
 * "21:00" -> "21:00:00"
 * "21:00:00" -> "21:00:00"
 */
function normalizeTimeFormat(time: string): string {
  if (!time) return '21:00:00';
  const parts = time.split(':');
  if (parts.length === 2) {
    return `${time}:00`;
  }
  return time;
}

export interface DNAStrandConfig {
  dnaStrandId: string;           // Unique ID within strategy
  indicatorName: string;
  epic: string;
  timeframe: string;
  timingMode: string;            // T5BeforeClose, Fake5min_4thCandle, etc.
  dataSource: string;            // 'capital' or 'av'
  crashProtectionEnabled: boolean;
  guaranteedStopEnabled?: boolean; // Use guaranteed stop loss (pays premium, no slippage)
  leverage: number;
  stopLoss: number;
  indicatorParams: Record<string, any>;
  
  // Historical performance (for conflict resolution)
  sourceTestId?: number;         // ID in backtestResults table
  sharpe?: number;
  totalReturn?: number;
  winRate?: number;
  maxDrawdown?: number;
}

/**
 * Calculate signal for a single DNA strand
 * 
 * @param dna - DNA strand configuration
 * @param fake5minClose - API price for Fake5min_4thCandle mode (optional)
 * @returns DNABrainResult with signal and metrics
 */
export async function calculateDNASignal(
  dna: DNAStrandConfig,
  fake5minClose?: number
): Promise<DNABrainResult> {
  const startTime = Date.now();
  
  try {
    // Prepare signal config for Python
    const signalConfig: SignalConfig = {
      db_path: '', // Not used - MySQL now
      epic: dna.epic,
      indicator_name: dna.indicatorName,
      indicator_params: dna.indicatorParams || {},
      timeframe: dna.timeframe,
      data_source: dna.dataSource as 'av' | 'capital',
      timing_config: { mode: dna.timingMode },
      crash_protection_enabled: dna.crashProtectionEnabled,
      // Fake 5-min close for T-60 mode
      fake_5min_close: fake5minClose,
      fake_5min_timestamp: fake5minClose ? new Date().toISOString() : undefined,
    };
    
    orchestrationLogger.debug('DNA_CALC', 
      `Calculating signal for ${dna.indicatorName} on ${dna.epic}`, {
        data: {
          dnaStrandId: dna.dnaStrandId,
          timingMode: dna.timingMode,
          dataSource: dna.dataSource,
          crashProtection: dna.crashProtectionEnabled,
          fake5minClose,
        }
      }
    );
    
    // Call Python signal calculator
    const signalResult = await getCurrentSignal(signalConfig);
    
    const executionMs = Date.now() - startTime;
    
    // Create brain result
    const result: DNABrainResult = {
      dnaStrandId: dna.dnaStrandId,
      indicatorName: dna.indicatorName,
      epic: dna.epic,
      timeframe: dna.timeframe,
      timingMode: dna.timingMode,
      dataSource: dna.dataSource,
      
      // Signal: 1 = BUY, 0 = HOLD, -1 = SELL (treat as HOLD)
      signal: signalResult.signal === 1 ? 'BUY' : 'HOLD',
      triggeredAt: new Date(),
      
      // Historical performance
      sharpe: dna.sharpe,
      totalReturn: dna.totalReturn,
      winRate: dna.winRate,
      maxDrawdown: dna.maxDrawdown,
      
      // Trade parameters
      leverage: dna.leverage,
      stopLoss: dna.stopLoss,
      crashProtectionEnabled: dna.crashProtectionEnabled,
      
      // Raw output
      indicatorValue: signalResult.indicator_value,
      indicatorParams: dna.indicatorParams,
    };
    
    orchestrationLogger.info('DNA_CALC', 
      `${dna.indicatorName}: ${result.signal} (${executionMs}ms)`, {
        data: {
          dnaStrandId: dna.dnaStrandId,
          signal: result.signal,
          indicatorValue: signalResult.indicator_value,
          crashBlocked: signalResult.crash_blocked,
          executionMs,
        }
      }
    );
    
    return result;
    
  } catch (error: any) {
    orchestrationLogger.logError('DNA_CALC', 
      `Failed to calculate signal for ${dna.indicatorName}`, error
    );
    
    // Return HOLD on error
    return {
      dnaStrandId: dna.dnaStrandId,
      indicatorName: dna.indicatorName,
      epic: dna.epic,
      timeframe: dna.timeframe,
      timingMode: dna.timingMode,
      dataSource: dna.dataSource,
      signal: 'HOLD',
      triggeredAt: new Date(),
    };
  }
}

/**
 * ============================================================================
 * PHASE 2.2: Load DNA strands with DYNAMIC close time resolution
 * ============================================================================
 * 
 * Returns:
 * - windowConfigs: DNA strands organized by their window
 *   - windowCloseTime: DYNAMICALLY resolved from current market data (USE THIS!)
 *   - storedCloseTime: Original stored value (for debugging/rollback only)
 * - allDnaStrands: ALL DNA strands (for single-window day mode)
 * 
 * ROLLBACK: To revert to static behavior, change `windowCloseTime` assignment
 *           back to use `storedCloseTime` instead of `resolvedCloseTime`
 * ============================================================================
 */
export async function loadStrategyDNAStrands(strategyId: number): Promise<{
  windowConfigs: Array<{
    windowCloseTime: string;      // DYNAMIC: Resolved from current market data
    storedCloseTime: string;      // STATIC: Original stored value (for rollback)
    dnaStrands: DNAStrandConfig[];
    conflictMode: string;
    allocationPct: number;
  }>;
  allDnaStrands: DNAStrandConfig[];
}> {
  const db = await getDb();
  if (!db) {
    return { windowConfigs: [], allDnaStrands: [] };
  }
  
  // Get strategy
  const [strategy] = await db
    .select()
    .from(savedStrategies)
    .where(eq(savedStrategies.id, strategyId));
  
  if (!strategy) {
    orchestrationLogger.warn('DNA_CALC', `Strategy ${strategyId} not found`);
    return { windowConfigs: [], allDnaStrands: [] };
  }
  
  // Parse window config
  const windowConfig = strategy.windowConfig as any || {};
  const windows = windowConfig.windows || [];
  
  // Parse DNA strands
  const dnaStrands = (strategy.dnaStrands || []) as Array<{
    id?: string;
    indicatorName?: string;
    epic?: string;
    timeframe?: string;
    timingConfig?: { mode?: string };
    dataSource?: string;
    crashProtectionEnabled?: boolean;
    guaranteedStopEnabled?: boolean;
    leverage?: number;
    stopLoss?: number;
    indicatorParams?: Record<string, any>;
    sourceTestId?: number;
    sharpeRatio?: number;
    totalReturn?: number;
    winRate?: number;
    maxDrawdown?: number;
  }>;
  
  // Build allDnaStrands for single-window day mode
  const allDnaStrands: DNAStrandConfig[] = dnaStrands.map((dna, i) => ({
    dnaStrandId: dna.id || `dna_${i}`,
    indicatorName: dna.indicatorName || 'unknown',
    epic: dna.epic || 'SOXL',
    timeframe: dna.timeframe || '5m',
    timingMode: dna.timingConfig?.mode || 'Fake5min_4thCandle',
    dataSource: dna.dataSource || 'capital',
    crashProtectionEnabled: dna.crashProtectionEnabled || false,
    guaranteedStopEnabled: dna.guaranteedStopEnabled || false,
    leverage: dna.leverage || 1,
    stopLoss: dna.stopLoss || 2,
    indicatorParams: dna.indicatorParams || {},
    sourceTestId: dna.sourceTestId,
    sharpe: dna.sharpeRatio,
    totalReturn: dna.totalReturn,
    winRate: dna.winRate,
    maxDrawdown: dna.maxDrawdown,
  }));
  
  // ============================================================================
  // PHASE 2.2: Group DNA strands by window with DYNAMIC close time resolution
  // ============================================================================
  const windowConfigs: Array<{
    windowCloseTime: string;      // DYNAMIC: Resolved from current market data
    storedCloseTime: string;      // STATIC: Original stored value (for rollback)
    dnaStrands: DNAStrandConfig[];
    conflictMode: string;
    allocationPct: number;
  }> = [];
  
  for (const window of windows) {
    const windowDnaIds = window.dnaStrandIds || [];
    
    // Get DNA strands for this window
    const windowDnas: DNAStrandConfig[] = [];
    
    for (let i = 0; i < dnaStrands.length; i++) {
      const dna = dnaStrands[i];
      const dnaId = dna.id || `dna_${i}`;
      
      // Check if this DNA is in this window
      if (windowDnaIds.length > 0 && !windowDnaIds.includes(dnaId)) {
        continue;
      }
      
      windowDnas.push({
        dnaStrandId: dnaId,
        indicatorName: dna.indicatorName || 'unknown',
        epic: dna.epic || 'SOXL',
        timeframe: dna.timeframe || '5m',
        timingMode: dna.timingConfig?.mode || 'Fake5min_4thCandle',
        dataSource: dna.dataSource || 'capital',
        crashProtectionEnabled: dna.crashProtectionEnabled || false,
        guaranteedStopEnabled: dna.guaranteedStopEnabled || false,
        leverage: dna.leverage || 1,
        stopLoss: dna.stopLoss || 2,
        indicatorParams: dna.indicatorParams || {},
        sourceTestId: dna.sourceTestId,
        sharpe: dna.sharpeRatio,
        totalReturn: dna.totalReturn,
        winRate: dna.winRate,
        maxDrawdown: dna.maxDrawdown,
      });
    }
    
    // ========================================================================
    // PHASE 2.2: DYNAMIC CLOSE TIME RESOLUTION
    // ========================================================================
    // Extract unique epics from this window's DNA strands
    const windowEpics = [...new Set(windowDnas.map(d => d.epic))];
    
    // Resolve close time DYNAMICALLY from current market data
    // This is the KEY FIX - no more stale close times!
    const resolvedCloseTime = await resolveWindowCloseTime(windowEpics);
    
    // Keep the stored time for debugging/rollback
    const storedCloseTime = normalizeTimeFormat(window.closeTime || '21:00:00');
    
    // Log if there's a mismatch (helps identify stale strategies)
    if (resolvedCloseTime !== storedCloseTime) {
      orchestrationLogger.info('DNA_CALC', 
        `Window close time UPDATED: stored=${storedCloseTime}, resolved=${resolvedCloseTime} (epics: ${windowEpics.join(', ')})`, {
          data: { strategyId, storedCloseTime, resolvedCloseTime, windowEpics }
        }
      );
    }
    
    windowConfigs.push({
      // PHASE 2.2: Use DYNAMIC resolved time instead of stored time
      windowCloseTime: resolvedCloseTime,  // ← DYNAMIC! This is the fix!
      storedCloseTime: storedCloseTime,    // ← Keep for rollback/debugging
      dnaStrands: windowDnas,
      conflictMode: window.conflictResolution || 'sharpe',
      allocationPct: window.allocationPct || 100,
    });
  }
  
  // If no windows defined, create a default one with all DNA strands
  if (windowConfigs.length === 0 && dnaStrands.length > 0) {
    // PHASE 2.2: Resolve close time from all DNA strand epics
    const allEpics = [...new Set(dnaStrands.map(d => d.epic).filter(Boolean))] as string[];
    const resolvedCloseTime = allEpics.length > 0 
      ? await resolveWindowCloseTime(allEpics)
      : '21:00:00';
    
    windowConfigs.push({
      windowCloseTime: resolvedCloseTime,  // DYNAMIC!
      storedCloseTime: '21:00:00',         // Default stored value
      dnaStrands: dnaStrands.map((dna, i) => ({
        dnaStrandId: dna.id || `dna_${i}`,
        indicatorName: dna.indicatorName || 'unknown',
        epic: dna.epic || 'SOXL',
        timeframe: dna.timeframe || '5m',
        timingMode: dna.timingConfig?.mode || 'Fake5min_4thCandle',
        dataSource: dna.dataSource || 'capital',
        crashProtectionEnabled: dna.crashProtectionEnabled || false,
        guaranteedStopEnabled: dna.guaranteedStopEnabled || false,
        leverage: dna.leverage || 1,
        stopLoss: dna.stopLoss || 2,
        indicatorParams: dna.indicatorParams || {},
        sourceTestId: dna.sourceTestId,
        sharpe: dna.sharpeRatio,
        totalReturn: dna.totalReturn,
        winRate: dna.winRate,
        maxDrawdown: dna.maxDrawdown,
      })),
      conflictMode: 'sharpe',
      allocationPct: 100,
    });
  }
  
  return { windowConfigs, allDnaStrands };
}

/**
 * Get all timing modes present in an account's DNA strands
 */
export async function getAccountTimingModes(accountId: number): Promise<{
  timingModes: Set<string>;
  dnaByTiming: Map<string, DNAStrandConfig[]>;
}> {
  const db = await getDb();
  if (!db) {
    return { timingModes: new Set(), dnaByTiming: new Map() };
  }
  
  // Get account's strategy
  const { accounts } = await import('../../drizzle/schema');
  const [account] = await db
    .select()
    .from(accounts)
    .where(eq(accounts.id, accountId));
  
  if (!account || !account.assignedStrategyId) {
    return { timingModes: new Set(), dnaByTiming: new Map() };
  }
  
  // Load DNA strands
  const { windowConfigs } = await loadStrategyDNAStrands(account.assignedStrategyId);
  
  // Collect timing modes
  const timingModes = new Set<string>();
  const dnaByTiming = new Map<string, DNAStrandConfig[]>();
  
  for (const window of windowConfigs) {
    for (const dna of window.dnaStrands) {
      timingModes.add(dna.timingMode);
      
      if (!dnaByTiming.has(dna.timingMode)) {
        dnaByTiming.set(dna.timingMode, []);
      }
      dnaByTiming.get(dna.timingMode)!.push(dna);
    }
  }
  
  return { timingModes, dnaByTiming };
}

/**
 * ============================================================================
 * PHASE 2.3: Initialize accumulators with DYNAMIC window matching
 * ============================================================================
 * Called at T-300s (quiet_start)
 * 
 * KEY CHANGE: Now uses DYNAMICALLY RESOLVED close times from loadStrategyDNAStrands()
 * instead of comparing against stale stored values.
 * 
 * Before: window.windowCloseTime was stale stored value → mismatches → skipped accounts
 * After:  window.windowCloseTime is dynamically resolved → always matches current time
 * 
 * ROLLBACK: If this causes issues, compare against window.storedCloseTime instead
 * ============================================================================
 */
export async function initializeWindowAccumulators(
  marketCloseTime: Date,
  windowCloseTime: string
): Promise<number> {
  const db = await getDb();
  if (!db) return 0;
  
  const { accounts } = await import('../../drizzle/schema');
  const { and, isNotNull } = await import('drizzle-orm');
  
  // Check if it's a single-window day (all DNA strands compete)
  const isSingleWindowDay = fundTracker.isSingleWindowDay();
  
  // Get all active accounts with strategies (exclude archived)
  const activeAccounts = await db
    .select()
    .from(accounts)
    .where(
      and(
        eq(accounts.isActive, true),
        eq(accounts.isArchived, false),  // PHASE 2.3: Explicitly exclude archived
        isNotNull(accounts.assignedStrategyId)
      )
    );
  
  let initialized = 0;
  let skippedNoMatch = 0;
  
  orchestrationLogger.info('DNA_ACCUMULATOR', 
    `Initializing accumulators for ${activeAccounts.length} active accounts, target window: ${windowCloseTime}`, {
      data: { 
        targetWindow: windowCloseTime,
        accountCount: activeAccounts.length,
        isSingleWindowDay 
      }
    }
  );
  
  for (const account of activeAccounts) {
    // PHASE 2.2: loadStrategyDNAStrands now returns DYNAMICALLY RESOLVED close times!
    const { windowConfigs, allDnaStrands } = await loadStrategyDNAStrands(account.assignedStrategyId!);
    
    if (isSingleWindowDay) {
      // SINGLE-WINDOW DAY: Include ALL DNA strands from ALL windows
      // This allows conflict resolution to pick the best across the entire strategy
      const expectedDnas = allDnaStrands.map(dna => ({
        dnaStrandId: dna.dnaStrandId,
        indicatorName: dna.indicatorName,
        epic: dna.epic,
        timingMode: dna.timingMode,
        sourceTestId: dna.sourceTestId,  // For validation - links to backtestResults table
      }));
      
      if (expectedDnas.length > 0) {
        dnaBrainAccumulator.getOrCreate(
          account.id,
          account.assignedStrategyId!,
          windowCloseTime,
          marketCloseTime,
          expectedDnas
        );
        
        initialized++;
        orchestrationLogger.info('DNA_ACCUMULATOR', 
          `⚡ SINGLE-WINDOW: Account ${account.id} initialized with ALL ${expectedDnas.length} DNA strands`, {
            data: {
              accountId: account.id,
              dnaCount: expectedDnas.length,
              epics: [...new Set(expectedDnas.map(d => d.epic))],
            }
          }
        );
      }
    } else {
      // NORMAL DAY: Only include DNA strands for matching window
      const normalizedWindowTime = normalizeTimeFormat(windowCloseTime);
      let accountMatched = false;
      
      for (const window of windowConfigs) {
        // ====================================================================
        // PHASE 2.3: DYNAMIC MATCHING
        // ====================================================================
        // window.windowCloseTime is now DYNAMICALLY RESOLVED from current market data
        // This fixes the stale window problem!
        // 
        // Use closeTimesMatch() for tolerance (handles minor time differences)
        // ====================================================================
        const matches = closeTimesMatch(window.windowCloseTime, normalizedWindowTime, 1);
        
        if (matches) {
          accountMatched = true;
          
          // Create accumulator with expected DNA strands
          const expectedDnas = window.dnaStrands.map(dna => ({
            dnaStrandId: dna.dnaStrandId,
            indicatorName: dna.indicatorName,
            epic: dna.epic,
            timingMode: dna.timingMode,
            sourceTestId: dna.sourceTestId,  // For validation - links to backtestResults table
          }));
          
          dnaBrainAccumulator.getOrCreate(
            account.id,
            account.assignedStrategyId!,
            windowCloseTime,
            marketCloseTime,
            expectedDnas
          );
          
          initialized++;
          
          // Log with both resolved and stored times for debugging
          orchestrationLogger.info('DNA_ACCUMULATOR', 
            `✅ Account ${account.id} (${account.accountName}) matched window ${normalizedWindowTime}`, {
              data: {
                accountId: account.id,
                accountName: account.accountName,
                resolvedCloseTime: window.windowCloseTime,
                storedCloseTime: window.storedCloseTime,
                targetWindow: normalizedWindowTime,
                dnaCount: expectedDnas.length,
                epics: [...new Set(expectedDnas.map(d => d.epic))],
              }
            }
          );
        }
      }
      
      // PHASE 2.3: Log when account doesn't match (helps debug stale window issues)
      if (!accountMatched && windowConfigs.length > 0) {
        skippedNoMatch++;
        const windowTimes = windowConfigs.map(w => `${w.windowCloseTime} (stored: ${w.storedCloseTime})`);
        orchestrationLogger.warn('DNA_ACCUMULATOR', 
          `⚠️ Account ${account.id} (${account.accountName}) NO MATCH for ${normalizedWindowTime}`, {
            data: {
              accountId: account.id,
              accountName: account.accountName,
              targetWindow: normalizedWindowTime,
              accountWindows: windowTimes,
              windowCount: windowConfigs.length,
            }
          }
        );
      }
    }
  }
  
  orchestrationLogger.info('DNA_ACCUMULATOR', 
    `Initialized ${initialized} window accumulators for ${windowCloseTime}${isSingleWindowDay ? ' (SINGLE-WINDOW DAY)' : ''}, skipped ${skippedNoMatch} (no match)`, {
      data: {
        windowCloseTime,
        marketCloseTime: marketCloseTime.toISOString(),
        accountCount: activeAccounts.length,
        initialized,
        skippedNoMatch,
        isSingleWindowDay,
      }
    }
  );
  
  return initialized;
}

/**
 * Run brain calculations for all DNA strands matching a timing mode
 * Called when a timing trigger fires (T-300s, T-240s, T-60s, etc.)
 */
export async function runBrainForTimingMode(
  timingMode: string,
  windowCloseTime: string,
  epicPrices?: Record<string, number>  // For Fake5min_4thCandle mode
): Promise<{
  calculated: number;
  buySignals: number;
  completedAccounts: number[];  // Account IDs that have all DNA strands complete
}> {
  let calculated = 0;
  let buySignals = 0;
  const completedAccounts: number[] = [];
  
  // Get pending DNA strands for this timing mode
  const pending = dnaBrainAccumulator.getPendingForTimingMode(timingMode);
  
  if (pending.length === 0) {
    orchestrationLogger.debug('DNA_TRIGGER', 
      `No pending DNA strands for timing mode: ${timingMode}`
    );
    return { calculated: 0, buySignals: 0, completedAccounts: [] };
  }
  
  orchestrationLogger.info('DNA_TRIGGER', 
    `Running brain for ${pending.length} DNA strands (${timingMode})`, {
      data: {
        timingMode,
        windowCloseTime,
        pendingCount: pending.length,
        accounts: [...new Set(pending.map(p => p.accountId))],
      }
    }
  );
  
  // Group by account for efficient processing
  const byAccount = new Map<number, typeof pending>();
  for (const item of pending) {
    if (!byAccount.has(item.accountId)) {
      byAccount.set(item.accountId, []);
    }
    byAccount.get(item.accountId)!.push(item);
  }
  
  // Process each account - prepare all DNA calculations
  interface PendingCalc {
    accountId: number;
    dnaConfig: DNAStrandConfig;
    fake5minClose?: number;
  }
  
  const allPendingCalcs: PendingCalc[] = [];
  
  for (const [accountId, accountPending] of byAccount) {
    // Load full DNA configs
    const { windowConfigs } = await loadStrategyDNAStrands(accountPending[0].strategyId);
    
    // Find DNA configs matching pending items
    for (const window of windowConfigs) {
      for (const pendingDna of accountPending) {
        const dnaConfig = window.dnaStrands.find(
          d => d.dnaStrandId === pendingDna.dnaStrand.dnaStrandId
        );
        
        if (!dnaConfig) continue;
        
        // Get API price for this epic (for Fake5min_4thCandle mode)
        const fake5minClose = epicPrices?.[dnaConfig.epic];
        
        allPendingCalcs.push({ accountId, dnaConfig, fake5minClose });
      }
    }
  }
  
  // Run ALL DNA signal calculations in PARALLEL
  orchestrationLogger.info('DNA_TRIGGER', 
    `Running ${allPendingCalcs.length} DNA calculations in PARALLEL`, {
      data: { timingMode, calcCount: allPendingCalcs.length }
    }
  );
  
  const calcPromises = allPendingCalcs.map(async ({ accountId, dnaConfig, fake5minClose }) => {
    const result = await calculateDNASignal(dnaConfig, fake5minClose);
    return { accountId, result };
  });
  
  // Wait for ALL calculations to complete before processing results
  const calcResults = await Promise.all(calcPromises);
  
  // Process results and store in accumulators
  for (const { accountId, result } of calcResults) {
    calculated++;
    
    if (result.signal === 'BUY') {
      buySignals++;
    }
    
    // Store result in accumulator
    const { allComplete, accumulator } = dnaBrainAccumulator.storeResult(
      accountId,
      windowCloseTime,
      result
    );
    
    // Track accounts that are now complete
    if (allComplete && accumulator && !completedAccounts.includes(accountId)) {
      completedAccounts.push(accountId);
    }
  }
  
  orchestrationLogger.info('DNA_TRIGGER', 
    `Completed ${calculated} calculations: ${buySignals} BUY, ${calculated - buySignals} HOLD`, {
      data: {
        timingMode,
        calculated,
        buySignals,
        completedAccountIds: completedAccounts,
      }
    }
  );
  
  return { calculated, buySignals, completedAccounts };
}

/**
 * Run final conflict resolution for all completed accounts
 * Called at T-60s after all DNA strands have calculated
 */
export async function runFinalConflictResolution(windowCloseTime: string): Promise<{
  resolved: number;
  buyDecisions: number;
  holdDecisions: number;
  winners: Array<{ accountId: number; winner: DNABrainResult | null }>;
}> {
  const accumulators = dnaBrainAccumulator.getAllForWindow(windowCloseTime);
  
  let resolved = 0;
  let buyDecisions = 0;
  let holdDecisions = 0;
  const winners: Array<{ accountId: number; winner: DNABrainResult | null }> = [];
  
  for (const accumulator of accumulators) {
    if (!accumulator.allComplete) {
      orchestrationLogger.warn('DNA_CONFLICT', 
        `Account ${accumulator.accountId} not complete yet, skipping conflict resolution`, {
          data: {
            accountId: accumulator.accountId,
            completed: accumulator.dnaResults.size,
            expected: accumulator.expectedDnaStrands.length,
          }
        }
      );
      continue;
    }
    
    // Run conflict resolution
    // Get conflict mode from strategy (default to sharpe)
    // PHASE 2.3: windowConfigs now have dynamically resolved close times
    const { windowConfigs } = await loadStrategyDNAStrands(accumulator.strategyId);
    const normalizedWindowTime = normalizeTimeFormat(windowCloseTime);
    // Use closeTimesMatch() for tolerance-based matching
    const window = windowConfigs.find(w => closeTimesMatch(w.windowCloseTime, normalizedWindowTime, 1));
    const conflictMode = (window?.conflictMode || 'sharpe') as 'sharpe' | 'return' | 'winRate' | 'first_signal';
    
    const winner = dnaBrainAccumulator.runFinalConflictResolution(
      accumulator.accountId,
      windowCloseTime,
      conflictMode
    );
    
    resolved++;
    winners.push({ accountId: accumulator.accountId, winner });
    
    if (winner) {
      buyDecisions++;
    } else {
      holdDecisions++;
    }
  }
  
  orchestrationLogger.info('DNA_CONFLICT', 
    `Final conflict resolution: ${resolved} accounts, ${buyDecisions} BUY, ${holdDecisions} HOLD`, {
      data: {
        windowCloseTime,
        resolved,
        buyDecisions,
        holdDecisions,
      }
    }
  );
  
  return { resolved, buyDecisions, holdDecisions, winners };
}

/**
 * Process a single account that has completed ALL its DNA calculations
 * Runs conflict resolution and immediately triggers close/leverage operations
 * 
 * @returns BrainDecision for the account
 */
export async function processCompletedAccount(
  accountId: number,
  windowCloseTime: string,
  marketCloseTime: Date,
  accountInfo: {
    capitalAccountId: string;
    accountType: 'demo' | 'live';
    accountName: string;
    strategyId: number;
  },
  epicPrices?: Record<string, number>
): Promise<BrainDecision | null> {
  const accumulator = dnaBrainAccumulator.getAccumulator(accountId, windowCloseTime);
  
  if (!accumulator || !accumulator.allComplete) {
    orchestrationLogger.warn('DNA_PROCESS', 
      `Account ${accountId} not complete or no accumulator found`, {
        data: { accountId, windowCloseTime, hasAccumulator: !!accumulator }
      }
    );
    return null;
  }
  
  // Run conflict resolution for THIS account only
  // PHASE 2.3: windowConfigs now have dynamically resolved close times
  const { windowConfigs } = await loadStrategyDNAStrands(accountInfo.strategyId);
  const normalizedWindowTime = normalizeTimeFormat(windowCloseTime);
  // Use closeTimesMatch() for tolerance-based matching
  const window = windowConfigs.find(w => closeTimesMatch(w.windowCloseTime, normalizedWindowTime, 1));
  const conflictMode = (window?.conflictMode || 'sharpe') as 'sharpe' | 'return' | 'winRate' | 'first_signal';
  
  const winner = dnaBrainAccumulator.runFinalConflictResolution(
    accountId,
    windowCloseTime,
    conflictMode
  );
  
  // Convert to BrainDecision format
  const decision: BrainDecision = {
    accountId,
    strategyId: accountInfo.strategyId,
    decision: winner ? 'buy' : 'hold',
    epic: winner?.epic || '',
    timeframe: winner?.timeframe || '5m',
    leverage: winner?.leverage || null,
    stopLoss: winner?.stopLoss || null,
    guaranteedStopEnabled: winner?.guaranteedStopEnabled || false,
    indicatorName: winner?.indicatorName || '',
    conflictMode,
    calculatedAt: new Date(),
    executionTimeMs: 0,
    windowCloseTime,
    tradeId: null,
    wasSimulationOverride: false,
    rebalanceNeeded: false,
  };
  
  orchestrationLogger.info('DNA_PROCESS', 
    `Account ${accountId} (${accountInfo.accountName}): ${decision.decision.toUpperCase()} ${decision.epic || 'N/A'}`, {
      data: {
        accountId,
        accountType: accountInfo.accountType,
        decision: decision.decision,
        epic: decision.epic,
        indicator: decision.indicatorName,
      }
    }
  );
  
  // ============================================================================
  // CRITICAL FIX: Store brain result to database (was missing in Dec 27 refactor)
  // This creates the actual_trades record with full DNA data for validation
  // ============================================================================
  if (decision.decision === 'buy' && winner) {
    try {
      // Build allDnaResults from accumulator for validation
      const dnaResultsArray = Array.from(accumulator.dnaResults.values()).map((dna, idx) => ({
        testIndex: idx,
        indicatorName: dna.indicatorName,
        indicatorParams: dna.indicatorParams || {},
        epic: dna.epic,
        timeframe: dna.timeframe,
        signal: dna.signal,
        indicatorValue: dna.indicatorValue ?? null,
        sharpeRatio: dna.sharpe ?? null,
        totalReturn: dna.totalReturn ?? null,
        winRate: dna.winRate ?? null,
        maxDrawdown: dna.maxDrawdown ?? null,
      }));
      
      // Find winner index
      const winnerIndex = dnaResultsArray.findIndex(d => 
        d.indicatorName === winner.indicatorName && d.epic === winner.epic
      );
      
      // Look up sourceTestId from expectedDnaStrands (not available on DNABrainResult)
      const expectedDna = accumulator.expectedDnaStrands.find(d => d.dnaStrandId === winner.dnaStrandId);
      const sourceTestId = expectedDna?.sourceTestId || 0;
      
      // === CALCULATE PRICE TRACKING (T-60 vs 4th 1m candle variance) ===
      let brainCalcPrice: number | undefined;
      let fake5minClose: number | undefined;
      let priceVariancePct: number | undefined;
      let last1mCandleTime: string | undefined;

      if (epicPrices && winner.epic && epicPrices[winner.epic]) {
        brainCalcPrice = epicPrices[winner.epic];
        
        try {
          const { get4th1mCandleClose, calculateVariance } = await import('../services/candle_variance_service');
          const candleResult = await get4th1mCandleClose(
            winner.epic,
            marketCloseTime,
            windowCloseTime,
            winner.timingMode || 'Fake5min_4thCandle'
          );
          
          if (candleResult.fake5minClose) {
            fake5minClose = candleResult.fake5minClose;
            const variance = calculateVariance(brainCalcPrice, fake5minClose);
            priceVariancePct = variance.priceVariancePct;
            last1mCandleTime = candleResult.last1mCandleTime?.toISOString();
            
            orchestrationLogger.info('DNA_PROCESS', 
              `Price tracking for ${winner.epic}: T-60=$${brainCalcPrice.toFixed(4)}, 4th1m=$${fake5minClose.toFixed(4)}, variance=${priceVariancePct?.toFixed(4)}%`
            );
          }
        } catch (err: any) {
          orchestrationLogger.warn('DNA_PROCESS', `Variance calc failed: ${err.message}`);
        }
      }
      
      const brainResult = {
        decision: 'buy' as const,
        winningEpic: winner.epic,
        winningIndicator: winner.indicatorName,
        winningTestId: sourceTestId,
        winningTimeframe: winner.timeframe,
        winningDataSource: winner.dataSource || 'capital',
        indicatorParams: winner.indicatorParams || {},
        leverage: winner.leverage,
        stopLoss: winner.stopLoss,
        stopLossPercent: winner.stopLoss,
        conflictMode,
        timingConfig: { mode: winner.timingMode || 'Fake5min_4thCandle' },
        allDnaResults: {
          dnaResults: dnaResultsArray,
          conflictResolution: {
            metric: conflictMode,
            winnerIndex: winnerIndex >= 0 ? winnerIndex : 0,
            reason: `Won by ${conflictMode}`,
            hadConflict: dnaResultsArray.filter(d => d.signal === 'BUY').length > 1,
          },
        },
        // Brain calculation price tracking (T-60 vs 4th 1m candle)
        brainCalcPrice,
        fake5minClose,
        priceVariancePct,
        last1mCandleTime,
        // Candle data (approximate - we don't have exact dates in accumulator)
        candleStartDate: null,
        candleEndDate: null,
        candleCount: null,
        candleDataSnapshot: null,
        lastAvCandleTime: null,
        lastCapitalCandleTime: null,
      };
      
      const tradeId = await storeBrainResult(
        accountId,
        accountInfo.strategyId,
        windowCloseTime,
        brainResult,
        false // Not simulation
      );
      
      if (tradeId) {
        decision.tradeId = tradeId;
        orchestrationLogger.info('DNA_PROCESS', 
          `✅ Stored brain result for account ${accountId}, tradeId ${tradeId}`, {
            data: { accountId, tradeId, epic: winner.epic, indicator: winner.indicatorName }
          }
        );
      } else {
        orchestrationLogger.warn('DNA_PROCESS', 
          `⚠️ storeBrainResult returned null for account ${accountId}`, {
            data: { accountId, epic: winner.epic }
          }
        );
      }
    } catch (storeError: any) {
      orchestrationLogger.logError('DNA_PROCESS', 
        `Failed to store brain result for account ${accountId}: ${storeError.message}`, 
        storeError
      );
      // Continue - we'll try to open the trade anyway, but it won't have a database record
    }
  }
  
  // IMMEDIATELY process close/leverage via trade queue
  // This is the critical optimization - don't wait for other accounts!
  await tradeQueue.processAccountBrainComplete(
    marketCloseTime,
    accountId,
    decision,
    {
      capitalAccountId: accountInfo.capitalAccountId,
      accountType: accountInfo.accountType,
      accountName: accountInfo.accountName,
    }
  );
  
  return decision;
}

/**
 * Run brain calculations with IMMEDIATE processing as each account completes
 * 
 * This is the optimized version that:
 * 1. Starts all DNA calculations in PARALLEL
 * 2. As EACH account's DNA strands ALL complete → immediately processes close/leverage
 * 3. LIVE accounts are processed FIRST (as they complete)
 * 4. DEMO accounts are processed AFTER all LIVE accounts are done
 * 
 * This ensures minimal latency for live trades - no waiting for slow calculations!
 */
export async function runBrainWithImmediateProcessing(
  timingMode: string,
  windowCloseTime: string,
  marketCloseTime: Date,
  epicPrices?: Record<string, number>
): Promise<{
  decisions: BrainDecision[];
  liveProcessed: number;
  demoProcessed: number;
  totalCalcTimeMs: number;
}> {
  const startTime = Date.now();
  const decisions: BrainDecision[] = [];
  
  // Get pending DNA strands for this timing mode
  const pending = dnaBrainAccumulator.getPendingForTimingMode(timingMode);
  
  if (pending.length === 0) {
    // ENHANCED LOGGING: Log WHY there are no pending DNA strands
    const status = dnaBrainAccumulator.getStatus();
    orchestrationLogger.warn('DNA_IMMEDIATE', 
      `⚠️ No pending DNA strands for timing mode: ${timingMode}. This means initializeWindowAccumulators() either:
       1. Didn't match any accounts to this window, OR
       2. DNA strands have different timing modes, OR
       3. initializeWindowAccumulators() wasn't called before this.
       Accumulator status: ${status.activeAccumulators} active, windows: ${JSON.stringify(status.byWindow)}`
    );
    
    return { decisions: [], liveProcessed: 0, demoProcessed: 0, totalCalcTimeMs: 0 };
  }
  
  // Get account info (type, capitalAccountId) for all unique accounts
  const db = await getDb();
  const uniqueAccountIds = [...new Set(pending.map(p => p.accountId))];
  const accountsData = await db!.select().from(accounts).where(inArray(accounts.id, uniqueAccountIds));
  
  const accountInfoMap = new Map<number, {
    capitalAccountId: string;
    accountType: 'demo' | 'live';
    accountName: string;
    strategyId: number;
  }>();
  
  for (const acc of accountsData) {
    accountInfoMap.set(acc.id, {
      capitalAccountId: acc.capitalAccountId || acc.accountId,
      accountType: (acc.accountType || 'demo') as 'demo' | 'live',
      accountName: acc.accountName || `Account_${acc.id}`,
      strategyId: acc.assignedStrategyId!,
    });
  }
  
  // Group by account
  const byAccount = new Map<number, typeof pending>();
  for (const item of pending) {
    if (!byAccount.has(item.accountId)) {
      byAccount.set(item.accountId, []);
    }
    byAccount.get(item.accountId)!.push(item);
  }
  
  // Prepare all calculations
  interface PendingCalc {
    accountId: number;
    dnaConfig: DNAStrandConfig;
    fake5minClose?: number;
    accountType: 'demo' | 'live';
  }
  
  const allPendingCalcs: PendingCalc[] = [];
  
  // ============================================================================
  // FIX: Add accounts to trade queue BEFORE brain calculations start
  // This ensures processAccountBrainComplete() can find them for leverage adjustment
  // ============================================================================
  for (const [accountId, accountInfo] of accountInfoMap) {
    tradeQueue.addToQueue(
      marketCloseTime,
      accountId,
      accountInfo.accountName,
      accountInfo.capitalAccountId,
      accountInfo.accountType,
      windowCloseTime,
      0,  // balance - will be updated later from fundTracker
      0,  // pnl
      0,  // sharpe
      0   // winRate
    );
  }
  
  orchestrationLogger.info('DNA_IMMEDIATE', 
    `📋 Added ${accountInfoMap.size} accounts to trade queue`, {
      data: { 
        marketCloseTime: marketCloseTime.toISOString(),
        windowCloseTime,
        accountIds: [...accountInfoMap.keys()],
      }
    }
  );
  
  for (const [accountId, accountPending] of byAccount) {
    const accountInfo = accountInfoMap.get(accountId);
    if (!accountInfo) continue;
    
    const { windowConfigs } = await loadStrategyDNAStrands(accountPending[0].strategyId);
    
    for (const window of windowConfigs) {
      for (const pendingDna of accountPending) {
        const dnaConfig = window.dnaStrands.find(
          d => d.dnaStrandId === pendingDna.dnaStrand.dnaStrandId
        );
        
        if (!dnaConfig) continue;
        
        const fake5minClose = epicPrices?.[dnaConfig.epic];
        allPendingCalcs.push({ 
          accountId, 
          dnaConfig, 
          fake5minClose,
          accountType: accountInfo.accountType 
        });
      }
    }
  }
  
  orchestrationLogger.info('DNA_IMMEDIATE', 
    `🔥 Starting ${allPendingCalcs.length} DNA calculations with IMMEDIATE processing`, {
      data: { 
        timingMode, 
        calcCount: allPendingCalcs.length,
        liveAccounts: [...accountInfoMap.values()].filter(a => a.accountType === 'live').length,
        demoAccounts: [...accountInfoMap.values()].filter(a => a.accountType === 'demo').length,
      }
    }
  );
  
  // Create promises for all calculations
  // Each promise resolves when its DNA strand completes
  interface CalcResult {
    accountId: number;
    accountType: 'demo' | 'live';
    result: DNABrainResult;
    calcIndex: number;
  }
  
  const calcPromises: Promise<CalcResult>[] = allPendingCalcs.map(async (calc, index) => {
    const result = await calculateDNASignal(calc.dnaConfig, calc.fake5minClose);
    return {
      accountId: calc.accountId,
      accountType: calc.accountType,
      result,
      calcIndex: index,
    };
  });
  
  // Track completed accounts and their decisions
  const completedAccountIds = new Set<number>();
  const pendingDemoAccounts: number[] = [];
  let liveProcessed = 0;
  let demoProcessed = 0;
  
  // === PROCESS AS THEY COMPLETE using Promise.race() ===
  // This is the key optimization - we don't wait for all calculations!
  
  let remainingPromises = calcPromises.map((promise, idx) => ({
    promise: promise.then(result => ({ ...result, promiseIndex: idx })),
    promiseIndex: idx,
  }));
  
  while (remainingPromises.length > 0) {
    // Wait for the NEXT calculation to complete (not all of them!)
    const completed = await Promise.race(remainingPromises.map(p => p.promise));
    
    // Remove completed promise from remaining
    remainingPromises = remainingPromises.filter(p => p.promiseIndex !== completed.promiseIndex);
    
    // Store result in accumulator
    const { allComplete, accumulator } = dnaBrainAccumulator.storeResult(
      completed.accountId,
      windowCloseTime,
      completed.result
    );
    
    // Log progress
    const accountInfo = accountInfoMap.get(completed.accountId);
    console.log(`[DNAQueue] ${new Date().toISOString()} DNA ${completed.result.indicatorName} → ${completed.result.signal} (${completed.accountType}) [${remainingPromises.length} remaining]`);
    
    // Check if this account is now fully complete
    if (allComplete && !completedAccountIds.has(completed.accountId)) {
      completedAccountIds.add(completed.accountId);
      
      if (completed.accountType === 'live') {
        // === LIVE ACCOUNT: Process IMMEDIATELY! ===
        console.log(`[DNAQueue] ${new Date().toISOString()} Account ${completed.accountId} (${accountInfo?.accountName}) → COMPLETE [LIVE] - Processing NOW!`);
        
        const decision = await processCompletedAccount(
          completed.accountId,
          windowCloseTime,
          marketCloseTime,
          accountInfo!,
          epicPrices
        );
        
        if (decision) {
          decisions.push(decision);
          liveProcessed++;
        }
        
        console.log(`[DNAQueue] ${new Date().toISOString()} Account ${completed.accountId} (${accountInfo?.accountName}) → READY_TO_BUY [waiting for T-15s]`);
        
      } else {
        // === DEMO ACCOUNT: Queue for later (after all LIVE) ===
        console.log(`[DNAQueue] ${new Date().toISOString()} Account ${completed.accountId} (${accountInfo?.accountName}) → COMPLETE [DEMO] - Queued for later`);
        pendingDemoAccounts.push(completed.accountId);
      }
    }
  }
  
  // === PHASE 2: Process DEMO accounts (after ALL live are done) ===
  if (pendingDemoAccounts.length > 0) {
    orchestrationLogger.info('DNA_IMMEDIATE', 
      `✅ All LIVE accounts done. Processing ${pendingDemoAccounts.length} DEMO accounts...`
    );
    
    for (const accountId of pendingDemoAccounts) {
      const accountInfo = accountInfoMap.get(accountId);
      if (!accountInfo) continue;
      
      console.log(`[DNAQueue] ${new Date().toISOString()} Account ${accountId} (${accountInfo.accountName}) → Processing [DEMO]`);
      
      const decision = await processCompletedAccount(
        accountId,
        windowCloseTime,
        marketCloseTime,
        accountInfo,
        epicPrices
      );
      
      if (decision) {
        decisions.push(decision);
        demoProcessed++;
      }
      
      console.log(`[DNAQueue] ${new Date().toISOString()} Account ${accountId} (${accountInfo.accountName}) → READY_TO_BUY [waiting for T-15s]`);
    }
  }
  
  const totalCalcTimeMs = Date.now() - startTime;
  
  orchestrationLogger.info('DNA_IMMEDIATE', 
    `✅ T-60s complete: ${liveProcessed} LIVE, ${demoProcessed} DEMO processed in ${totalCalcTimeMs}ms`, {
      data: {
        timingMode,
        liveProcessed,
        demoProcessed,
        totalDecisions: decisions.length,
        totalCalcTimeMs,
      }
    }
  );
  
  return { decisions, liveProcessed, demoProcessed, totalCalcTimeMs };
}

