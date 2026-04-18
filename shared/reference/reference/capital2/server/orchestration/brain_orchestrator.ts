/**
 * Brain Orchestrator - Calls existing brainPreview for each active account
 * 
 * This module:
 * 1. Fetches all active accounts with assigned strategies
 * 2. Calls existing brainPreview() function for each account
 * 3. Stores decisions in memory for later execution (close/open windows)
 * 4. Logs all decisions to execution_logs table
 * 5. Integrates with FundTracker for rebalancing
 * 6. Supports SIMULATION mode (override HOLD with random BUY)
 * 
 * SIMULATION MODE (isSimulation=true):
 * - If brain returns HOLD, pick a random winning indicator
 * - This ensures trades are made for testing purposes
 * - CRITICAL: This override MUST NOT happen in production!
 */

import { orchestrationLogger } from './logger';
import { brainPreview } from '../live_trading/brain';
import type { BrainResult } from '../live_trading/brain';
import { getDb, getSetting } from '../db';
import { accounts, actualTrades, savedStrategies } from '../../drizzle/schema';
import { orchestrationTimer } from './timer';

/**
 * Get the API call delay from settings
 * Default: 200ms (5 req/s, well below Capital.com's 10 req/s limit)
 * Configurable in Settings > System > api_call_interval_ms
 */
async function getApiCallDelay(): Promise<number> {
  try {
    const setting = await getSetting('system', 'api_call_interval_ms');
    if (setting?.value) {
      const delay = parseInt(setting.value, 10);
      if (delay >= 100 && delay <= 500) {
        return delay;
      }
    }
  } catch (error) {
    console.warn('[BrainOrchestrator] Failed to load API delay setting, using default');
  }
  return 200; // Default 200ms
}
import { eq, and, isNotNull, inArray, isNull } from 'drizzle-orm';
import { getAccountsForCloseTime } from './epic_filter';
import { checkAndAdjustLeverage } from './leverage_checker';
import { connectionManager } from './connection_manager';
import { botStateManager } from './bot_state_manager';
import { fundTracker } from './fund_tracker';
import { apiQueue } from './api_queue';
import { tradeQueue } from './trade_queue';

/**
 * Store complete brain result in database for validation
 * This allows us to compare live decisions (AV + Capital.com) with validated decisions (100% AV)
 * 
 * Creates a PENDING actual_trade record that will be updated:
 * - At T-15s: When trade is executed (gets dealId from Capital.com)
 * - At T+1s: During reconciliation (confirms entry price, contracts, etc.)
 */
export async function storeBrainResult(
  accountId: number, 
  strategyId: number, 
  windowCloseTime: string,
  result: BrainResult,
  isSimulation: boolean = false
): Promise<number | null> {
  console.log(`[BrainOrchestrator] storeBrainResult called: account=${accountId}, strategy=${strategyId}, window=${windowCloseTime}, decision=${result.decision}, epic=${result.winningEpic}`);
  
  try {
    const db = await getDb();
    if (!db) {
      console.error(`[BrainOrchestrator] storeBrainResult FAILED: Database not available for account ${accountId}`);
      return null;
    }
    
    // Only store if decision is BUY (we don't create trade records for HOLD)
    if (result.decision !== 'buy') {
      console.log(`[BrainOrchestrator] storeBrainResult: Skipping HOLD decision for account ${accountId}`);
      return null;
    }
    
    // DUPLICATE CHECK: Check if a trade already exists for this account + window
    // This prevents duplicates when both DNA path and legacy path run brain calculations
    const { and, eq, gte } = await import('drizzle-orm');
    
    // Calculate today's start for the window (trades are per-day)
    // FIX: Use UTC to avoid timezone issues (server might be in different timezone than market)
    const now = new Date();
    const todayStart = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 0, 0, 0, 0));
    console.log(`[BrainOrchestrator] Duplicate check: account=${accountId}, window=${windowCloseTime}, todayStart=${todayStart.toISOString()}`);
    
    const existingTrade = await db
      .select({ id: actualTrades.id, status: actualTrades.status })
      .from(actualTrades)
      .where(
        and(
          eq(actualTrades.accountId, accountId),
          eq(actualTrades.windowCloseTime, windowCloseTime),
          gte(actualTrades.createdAt, todayStart)
        )
      )
      .limit(1);
    
    if (existingTrade.length > 0) {
      console.log(`[BrainOrchestrator] Trade already exists for account ${accountId}, window ${windowCloseTime} (tradeId: ${existingTrade[0].id}, status: ${existingTrade[0].status}) - skipping duplicate insert`);
      return existingTrade[0].id;
    }
    
    // Create a pending actual trade record
    // This will be updated with actual deal ID when trade is executed at T-15s
    console.log(`[BrainOrchestrator] Inserting new trade: account=${accountId}, strategy=${strategyId}, epic=${result.winningEpic}, indicator=${result.winningIndicator}`);
    
    // Log brain calculation price tracking info
    if (result.brainCalcPrice !== undefined) {
      console.log(`[BrainOrchestrator] Price tracking for ${result.winningEpic}:`);
      console.log(`  - T-60 API Price (brainCalcPrice): $${result.brainCalcPrice?.toFixed(4) || 'N/A'}`);
      console.log(`  - 4th 1m Candle (fake5minClose): $${result.fake5minClose?.toFixed(4) || 'N/A'}`);
      console.log(`  - Variance: ${result.priceVariancePct?.toFixed(4) || 'N/A'}%`);
      console.log(`  - Last 1m Candle Time: ${result.last1mCandleTime || 'N/A'}`);
    }
    
    const insertResult = await db.insert(actualTrades).values({
      accountId,
      strategyId,
      windowCloseTime,
      storedWindowTime: windowCloseTime, // Store same as windowCloseTime for now (dynamic resolution happens at runtime)
      epic: result.winningEpic,
      direction: 'BUY',
      leverage: result.leverage || 1,
      stopLossPrice: result.stopLoss,
      // NEW: Store stop loss %, timeframe, timing config, and data source for permanent record
      stopLossPercent: result.stopLossPercent || result.stopLoss || null,
      timeframe: result.winningTimeframe || '5m',
      timingConfig: result.timingConfig as any,
      dataSource: result.winningDataSource || 'capital',
      brainDecision: 'BUY',
      winningIndicatorName: result.winningIndicator,
      winningTestId: result.winningTestId,
      indicatorParams: result.indicatorParams as any,
      conflictResolutionMetric: result.conflictMode,
      // Store candle range for validation (so we can reproduce the exact calculation)
      candleStartDate: result.candleStartDate ? new Date(result.candleStartDate) : null,
      candleEndDate: result.candleEndDate ? new Date(result.candleEndDate) : null,
      candleCount: result.candleCount || null,
      // Store ALL DNA results for comprehensive validation
      allDnaResults: result.allDnaResults as any,
      // Legacy candle data snapshot (kept for compatibility)
      candleDataSnapshot: result.candleDataSnapshot as any,
      lastAvCandleTime: result.lastAvCandleTime ? new Date(result.lastAvCandleTime) : null,
      lastCapitalCandleTime: result.lastCapitalCandleTime ? new Date(result.lastCapitalCandleTime) : null,
      
      // NEW: Brain calculation price tracking (for validation accuracy)
      // These fields allow comparing brain's T-60 API price vs backtest's 4th 1m candle
      brainCalcPrice: result.brainCalcPrice || null,
      fake5minClose: result.fake5minClose || null,
      priceVariancePct: result.priceVariancePct || null,
      last1mCandleTime: result.last1mCandleTime ? new Date(result.last1mCandleTime) : null,
      
      // HMH (Hold Means Hold) - set new SL on HOLD instead of closing
      hmhEnabled: result.hmhEnabled || false,
      hmhStopLossOffset: result.hmhStopLossOffset ?? null,
      hmhIsContinuation: false,  // This is a new trade, not a continuation
      hmhDaysHeld: 1,            // Day 1 of holding
      hmhOriginalEntryPrice: null, // Will be set when entry price is known
      
      status: 'pending',
      isSimulation,
    });
    
    // FIX: Validate insertId is valid before using it
    // Note: mysql2 returns [ResultSetHeader, ...] where ResultSetHeader has insertId
    console.log(`[BrainOrchestrator] INSERT result:`, JSON.stringify(insertResult).substring(0, 200));
    
    // Try multiple ways to get insertId (different DB drivers return it differently)
    let insertId: number | bigint | undefined;
    if (Array.isArray(insertResult) && insertResult[0]) {
      insertId = insertResult[0].insertId;
    } else if ((insertResult as any)?.insertId) {
      insertId = (insertResult as any).insertId;
    }
    
    if (!insertId || (typeof insertId === 'number' && isNaN(insertId))) {
      console.error(`[BrainOrchestrator] ❌ Invalid insertId ${insertId} for account ${accountId}, epic ${result.winningEpic}`);
      console.error(`[BrainOrchestrator] Full insert result:`, JSON.stringify(insertResult));
      throw new Error(`Database insert failed: Invalid insertId ${insertId}`);
    }
    
    const tradeId = Number(insertId);
    console.log(`[BrainOrchestrator] ✅ Stored brain result for account ${accountId}, epic ${result.winningEpic}, tradeId ${tradeId}`);
    
    // Update tradingSessionState with BUY decision (async, non-blocking)
    try {
      const { fundTracker } = await import('./fund_tracker');
      await fundTracker.markBuyDecision(accountId, windowCloseTime, {
        epic: result.winningEpic!,
        indicator: result.winningIndicator!,
        indicatorValue: result.winningValue,
        tradeId,
      });
    } catch (tssErr: any) {
      console.warn(`[BrainOrchestrator] tradingSessionState update warning: ${tssErr.message}`);
    }
    
    return tradeId;
  } catch (error: any) {
    console.error(`[BrainOrchestrator] ❌ FAILED to store brain result for account ${accountId}:`, error.message);
    console.error(`[BrainOrchestrator] Error details:`, error.code || 'no code', error.sqlMessage || 'no SQL message');
    console.error(`[BrainOrchestrator] Stack:`, error.stack?.split('\n').slice(0, 5).join('\n'));
    return null;
  }
}

/**
 * Execute immediate close/leverage operations for T-60 trigger mode
 * This is called as each brain calculation completes (first-come-first-served)
 * 
 * CRITICAL: Close operations MUST COMPLETE before returning.
 * This ensures no race condition where open trades get closed by pending close operations.
 * 
 * Flow: Brain Done → Close Yesterday's Position (AWAIT) → Ready for T-15s Open
 */
async function queueImmediateOperations(
  account: any,
  decision: BrainDecision,
  result: BrainResult,
  windowCloseTime: string
): Promise<void> {
  const db = await getDb();
  if (!db) return;
  
  try {
    // ═══════════════════════════════════════════════════════════════════════════
    // CRITICAL FIX: Query Capital.com API directly for positions, NOT database
    // ═══════════════════════════════════════════════════════════════════════════
    // Previous bug: Database query missed positions from previous sessions or
    // positions opened outside the app (manual trades, etc.)
    // 
    // Now: We query GET /positions from Capital.com to get ALL current positions
    // for this account, regardless of when/how they were opened.
    // ═══════════════════════════════════════════════════════════════════════════
    
    const client = connectionManager.getClient(account.accountType as 'demo' | 'live');
    if (!client) {
      await orchestrationLogger.warn('T60_CLOSE', 
        `No ${account.accountType} client available for account ${account.id}`,
        { accountId: account.id }
      );
      return;
    }
    
    // Switch to correct account first
    const capitalAccountId = account.capitalAccountId || account.accountId;
    await client.switchAccount(capitalAccountId);
    
    // Query Capital.com for ALL current positions on this account
    let capitalPositions: any[] = [];
    try {
      capitalPositions = await client.getPositions();
      await orchestrationLogger.info('T60_CLOSE', 
        `Queried Capital.com for positions: found ${capitalPositions?.length || 0} open positions`,
        { accountId: account.id, data: { positionCount: capitalPositions?.length || 0 } }
      );
    } catch (posError: any) {
      await orchestrationLogger.warn('T60_CLOSE', 
        `Failed to query Capital.com positions for account ${account.id}: ${posError.message}`,
        { accountId: account.id }
      );
      // Fall back to database query if API fails
      capitalPositions = [];
    }
    
    // Close ALL positions for this account (any epic, any session)
    if (capitalPositions && capitalPositions.length > 0) {
      for (const position of capitalPositions) {
        const dealId = position.position?.dealId || position.dealId;
        const epic = position.market?.epic || position.epic;
        const size = position.position?.size || position.size;
        
        if (!dealId) {
          await orchestrationLogger.warn('T60_CLOSE', 
            `Position missing dealId, skipping: ${JSON.stringify(position).substring(0, 200)}`,
            { accountId: account.id }
          );
          continue;
        }
        
        await orchestrationLogger.info('T60_CLOSE', 
          `Closing position from Capital.com: dealId=${dealId}, epic=${epic}, size=${size}`, {
            accountId: account.id,
            data: { windowCloseTime, dealId, epic, size }
          }
        );
        
        // AWAIT the close operation - DO NOT proceed until close is COMPLETE
        const closeSuccess = await new Promise<boolean>((resolve) => {
          apiQueue.enqueue({
            priority: 'critical',
            isCritical: true,
            environment: account.accountType as 'demo' | 'live',
            operation: 'close',
            description: `T60_CLOSE ${epic} dealId=${dealId} accountId=${account.id} env=${account.accountType}`,
            fn: async () => {
              const closeClient = connectionManager.getClient(account.accountType as 'demo' | 'live');
              if (!closeClient) {
                throw new Error(`No ${account.accountType} client available`);
              }
              
              // Switch to correct account (in case context changed)
              await closeClient.switchAccount(capitalAccountId);
              
              // Close the position
              const success = await closeClient.closePosition(dealId);
              
              if (success) {
                // Try to update database if we have a matching trade record
                // This is best-effort - the position is closed regardless
                try {
                  const matchingTrades = await db
                    .select({ id: actualTrades.id })
                    .from(actualTrades)
                    .where(
                      and(
                        eq(actualTrades.accountId, account.id),
                        eq(actualTrades.dealId, dealId),
                        eq(actualTrades.status, 'open')
                      )
                    )
                    .limit(1);
                  
                  if (matchingTrades.length > 0) {
                    await db
                      .update(actualTrades)
                      .set({
                        closedAt: new Date(),
                        status: 'closed',
                        closeReason: 'window_close',
                      })
                      .where(eq(actualTrades.id, matchingTrades[0].id));
                  }
                } catch (dbErr: any) {
                  // Non-fatal - position is closed, DB update is optional
                  console.warn(`[T60_CLOSE] DB update warning for dealId ${dealId}: ${dbErr.message}`);
                }
              }
              
              return success;
            },
            maxRetries: 3,
            onSuccess: (success) => {
              resolve(Boolean(success));
            },
            onError: (error) => {
              orchestrationLogger.logError('T60_CLOSE', `Close failed for ${dealId}`, error as Error);
              resolve(false);
            },
          });
        });
        
        if (closeSuccess) {
          await orchestrationLogger.info('T60_CLOSE_SUCCESS', 
            `✅ Closed position ${dealId} for account ${account.id}`,
            { accountId: account.id, data: { dealId, epic } }
          );
        } else {
          await orchestrationLogger.warn('T60_CLOSE_FAILED', 
            `❌ Failed to close position ${dealId} for account ${account.id}`,
            { accountId: account.id, data: { dealId, epic } }
          );
        }
      }
    } else {
      await orchestrationLogger.debug('T60_CLOSE', 
        `No open positions found on Capital.com for account ${account.id}`,
        { accountId: account.id }
      );
    }
    
    // 3. If decision is BUY, queue leverage adjustment if needed
    if (decision.decision === 'buy' && decision.leverage) {
      await orchestrationLogger.info('T60_QUEUE', 
        `Queueing LEVERAGE check for account ${account.id} (${decision.leverage}x)`, {
          accountId: account.id,
          data: { windowCloseTime, leverage: decision.leverage, epic: decision.epic }
        }
      );
      
      // Queue leverage adjustment with CRITICAL priority
      apiQueue.enqueue({
        priority: 'critical',
        isCritical: true,
        environment: account.accountType as 'demo' | 'live',
        operation: 'leverage',
        description: `T60_LEVERAGE ${decision.leverage}x epic=${decision.epic} accountId=${account.id} env=${account.accountType}`,
        fn: async () => {
          const client = connectionManager.getClient(account.accountType as 'demo' | 'live');
          if (!client) {
            throw new Error(`No ${account.accountType} client available`);
          }
          
          // Switch to correct account
          const capitalAccountId = account.capitalAccountId || account.accountId;
          await client.switchAccount(capitalAccountId);
          
          // Get actual leverage state from timer (T-5m fetch)
          const actualLeverageState = orchestrationTimer.getActualLeverageState();
          
          // Check and adjust leverage using T-5m ground truth
          await checkAndAdjustLeverage(
            client, 
            account.id, 
            decision.epic, 
            decision.leverage!, 
            capitalAccountId,
            actualLeverageState  // Pass T-5m actual state
          );
          
          return true;
        },
        maxRetries: 2,
        onSuccess: () => {
          orchestrationLogger.info('T60_QUEUE', `Leverage check queued for account ${account.id}`);
        },
        onError: (error) => {
          orchestrationLogger.logError('T60_QUEUE', `Leverage check failed for account ${account.id}`, error as Error);
        },
      });
    }
    
  } catch (error: any) {
    await orchestrationLogger.logError('T60_QUEUE', 
      `Failed to queue operations for account ${account.id}`, error,
      { accountId: account.id, data: { windowCloseTime } }
    );
  }
}

export interface BrainDecision {
  accountId: number;
  strategyId: number;
  decision: 'buy' | 'hold' | 'hold_trail';  // hold_trail = HMH mode, set new SL instead of closing
  epic: string;
  timeframe: string;
  leverage: number | null;
  stopLoss: number | null;
  guaranteedStopEnabled: boolean;    // Whether to use guaranteed stop loss (pays premium, no slippage)
  // HMH (Hold Means Hold) - set new SL on HOLD instead of closing
  hmhEnabled: boolean;               // Whether HMH is enabled for this DNA
  hmhStopLossOffset: number | null;  // SL offset: 0 (original), -1 (orig-1%), -2 (orig-2%)
  indicatorName: string;
  conflictMode: string;
  calculatedAt: Date;
  executionTimeMs: number;
  windowCloseTime: string;           // The window this decision belongs to
  tradeId: number | null;            // ID of the actual_trade record (if BUY)
  wasSimulationOverride: boolean;    // True if HOLD was overridden to BUY in simulation
  rebalanceNeeded: boolean;          // True if rebalancing is needed before this trade
}

/**
 * Execute brain calculations for accounts with epics closing at the specified time
 * Returns array of decisions (buy/hold) for each account
 * 
 * @param marketCloseTime - Market close time to filter accounts by
 * @param accountIdsOrWindowClose - Either specific account IDs (number[]) or window close time string
 * @param optionsOrSimulation - Either options object or boolean for isSimulation
 */
export interface BrainExecutionOptions {
  isSimulation?: boolean;
  triggerMode?: 'timer' | 'candle';  // How the brain was triggered
  fakeCandleClose?: number;          // Close price from candle trigger
  isT60Trigger?: boolean;            // True if triggered at T-60s (Fake5min_4thCandle mode)
  epicPrices?: Record<string, number>; // Instant prices from REST API at T-60s
  epicDataCache?: Record<string, any[]>; // Pre-fetched candle data for all epics (1 DB read, shared across accounts)
  windowCloseTime?: string;          // The window close time (HH:MM:SS) - used when passing accountIds separately
  marketCloseDate?: Date;            // Market close date for trade queue (when marketCloseTime param is undefined)
}

export async function executeBrainCalculations(
  marketCloseTime?: Date,
  accountIdsOrWindowClose?: number[] | string,
  optionsOrSimulation: BrainExecutionOptions | boolean = false
): Promise<BrainDecision[]> {
  // Parse overloaded parameters
  const accountIds = Array.isArray(accountIdsOrWindowClose) ? accountIdsOrWindowClose : undefined;
  const options: BrainExecutionOptions = typeof optionsOrSimulation === 'boolean' 
    ? { isSimulation: optionsOrSimulation } 
    : optionsOrSimulation;
  // windowCloseTime can come from 2nd arg (string) OR from options (for backwards compatibility)
  const windowCloseTime = typeof accountIdsOrWindowClose === 'string' 
    ? accountIdsOrWindowClose 
    : options.windowCloseTime;
  const isSimulation = options.isSimulation || false;
  const triggerMode = options.triggerMode || 'timer';
  const fakeCandleClose = options.fakeCandleClose;
  
  const modeLabel = isSimulation ? '[SIMULATION] ' : '';
  const triggerLabel = triggerMode === 'candle' ? '⚡[CANDLE-TRIGGERED] ' : '';
  await orchestrationLogger.info('BRAIN_STARTED', `${modeLabel}${triggerLabel}Starting brain calculations${marketCloseTime ? ` for close time ${marketCloseTime.toISOString()}` : ' for all active accounts'}${accountIds ? ` (accounts: ${accountIds.join(', ')})` : ''}`);
  
  if (fakeCandleClose) {
    await orchestrationLogger.info('BRAIN_STARTED', `Using fake candle close price: ${fakeCandleClose}`);
  }
  
  if (isSimulation) {
    await orchestrationLogger.warn('BRAIN_STARTED', 
      '⚠️ SIMULATION MODE: HOLD decisions will be overridden with random BUY. DO NOT USE IN PRODUCTION!'
    );
  }
  
  const decisions: BrainDecision[] = [];
  
  try {
    // 1. Get all active accounts with assigned strategies
    const db = await getDb();
    if (!db) {
      await orchestrationLogger.warn('BRAIN_ERROR', 'Database not available');
      return decisions;
    }
    
    // Filter accounts based on provided parameters
    let activeAccounts;
    
    if (accountIds && accountIds.length > 0) {
      // Specific accounts provided (e.g., from candle trigger)
      activeAccounts = await db
        .select()
        .from(accounts)
        .where(
          and(
            inArray(accounts.id, accountIds),
            isNotNull(accounts.assignedStrategyId),
            eq(accounts.isActive, true)
          )
        );
      
      await orchestrationLogger.info('BRAIN_STARTED', `Running for ${activeAccounts.length} specified accounts`);
    } else if (marketCloseTime) {
      // Get accounts with epics closing at this specific time
      const accountsWithEpics = await getAccountsForCloseTime(marketCloseTime);
      
      if (accountsWithEpics.length === 0) {
        await orchestrationLogger.info('BRAIN_STARTED', 'No accounts with epics closing at this time');
        return decisions;
      }
      
      // Fetch full account records
      const closeTimeAccountIds = accountsWithEpics.map(a => a.accountId);
      activeAccounts = await db
        .select()
        .from(accounts)
        .where(
          and(
            inArray(accounts.id, closeTimeAccountIds),
            isNotNull(accounts.assignedStrategyId),
            eq(accounts.isActive, true)
          )
        );
      
      await orchestrationLogger.info('BRAIN_STARTED', `Found ${activeAccounts.length} accounts with epics closing at ${marketCloseTime.toISOString()}`);
    } else {
      // No filter - get all active accounts
      activeAccounts = await db
        .select()
        .from(accounts)
        .where(
          and(
            isNotNull(accounts.assignedStrategyId),
            eq(accounts.isActive, true)
          )
        );
    }
    
    await orchestrationLogger.info('BRAIN_STARTED', `Found ${activeAccounts.length} active accounts with strategies`);

    // LIVE-FIRST: ensure live accounts start brain calculations first.
    // NOTE: For T-60 trigger we run brain in parallel, but ordering still matters for:
    // - slight scheduling advantage
    // - sequential close/leverage processing after brain completes
    activeAccounts.sort((a: any, b: any) => {
      const aLive = a.accountType === 'live';
      const bLive = b.accountType === 'live';
      if (aLive !== bLive) return aLive ? -1 : 1;
      return (a.id || 0) - (b.id || 0);
    });
    
    // CRITICAL: Ensure fresh candles are available before brain calculations
    // Collect all unique epics from all active account strategies
    const allEpics = new Set<string>();
    for (const account of activeAccounts) {
      if (account.assignedStrategyId) {
        const strategy = await db
          .select({ dnaStrands: savedStrategies.dnaStrands })
          .from(savedStrategies)
          .where(eq(savedStrategies.id, account.assignedStrategyId))
          .limit(1);
        
        if (strategy.length > 0 && strategy[0].dnaStrands) {
          const dnaStrands = strategy[0].dnaStrands as any[];
          for (const dna of dnaStrands) {
            allEpics.add(dna.epic || 'SOXL');
          }
        }
      }
    }
    
    // NOTE: Gap-fill is now done in simulation_controller.ts BEFORE T-60 prices
    // This mirrors live trading where gap-fill happens during quiet period (T-5m)
    // and T-60 prices are fetched at T-60s
    
    // Check if this is a T-60 trigger (parallel execution with immediate close/leverage)
    const isT60Trigger = options.isT60Trigger || false;
    const epicPrices = options.epicPrices || {};
    const epicDataCache = options.epicDataCache; // Pre-fetched candle data (optional - if not provided, brainPreview loads from DB)
    
    // CRITICAL FIX: Determine which epics close at THIS window time
    // This prevents brain from calculating for epics that close at a different window
    let filterEpicsForWindow: string[] | undefined = undefined;
    
    if (marketCloseTime) {
      // Get marketInfo to find epics closing at this time
      const { marketInfo } = await import('../../drizzle/schema');
      const { eq } = await import('drizzle-orm');
      
      // Get time string for comparison (HH:MM:SS)
      const targetHours = marketCloseTime.getUTCHours().toString().padStart(2, '0');
      const targetMinutes = marketCloseTime.getUTCMinutes().toString().padStart(2, '0');
      const targetTimeStr = `${targetHours}:${targetMinutes}`;
      
      const allMarkets = await db.select().from(marketInfo).where(eq(marketInfo.isActive, true));
      
      filterEpicsForWindow = allMarkets
        .filter(m => {
          if (!m.marketCloseTime) return false;
          // Compare HH:MM (ignore seconds)
          const closeTimeStr = m.marketCloseTime.substring(0, 5);
          return closeTimeStr === targetTimeStr;
        })
        .map(m => m.epic);
      
      if (filterEpicsForWindow.length > 0) {
        await orchestrationLogger.info('BRAIN_STARTED', 
          `🎯 Window ${targetTimeStr} - filtering brain to epics: ${filterEpicsForWindow.join(', ')}`);
      } else {
        await orchestrationLogger.warn('BRAIN_STARTED', 
          `⚠️ No epics found with close time ${targetTimeStr} - will calculate for all epics`);
        filterEpicsForWindow = undefined;
      }
    }
    
    if (isT60Trigger) {
      await orchestrationLogger.info('BRAIN_STARTED', '🔥 T-60 TRIGGER: Running brain calculations in PARALLEL');
    }
    
    // 2. Execute brain for all accounts - PARALLEL for T-60, sequential otherwise
    const brainPromises = activeAccounts.map(async (account) => {
      try {
        const startTime = Date.now();
        
        // Add to trade queue with BRAIN_CALCULATING state
        // Use options.marketCloseDate if marketCloseTime is not provided (for test mode)
        const effectiveMarketCloseTime = marketCloseTime || options.marketCloseDate;
        if (isT60Trigger && effectiveMarketCloseTime) {
          tradeQueue.addToQueue(
            effectiveMarketCloseTime,
            account.id,
            account.accountName || `Account_${account.id}`,
            account.capitalAccountId || account.accountId,
            account.accountType as 'demo' | 'live',
            windowCloseTime || 'unknown',
            account.balance || 0,
            // highest_pnl metric (from accounts.profitLoss)
            account.profitLoss || 0
          );
        }
        
        await orchestrationLogger.debug('BRAIN_STARTED', `Calculating for account ${account.id}`, {
          accountId: account.id,
          strategyId: account.assignedStrategyId!,
          data: {
            accountName: account.accountName,
            environment: account.accountType,
          },
        });
        
        // Call existing brainPreview function
        // Pass epicDataCache for efficient 1-DB-read pattern (shared across all accounts)
        // Pass epicPrices for Fake5min_4thCandle mode (T-60 trigger)
        // CRITICAL: Pass filterEpicsForWindow to only calculate for epics closing at THIS window
        let result = await brainPreview(
          account.assignedStrategyId!,
          account.id,
          account.accountType,
          filterEpicsForWindow, // Only calculate for epics closing at this window
          epicDataCache, // Pre-fetched candle data (1 DB read shared across accounts)
          isT60Trigger ? epicPrices : undefined // Pass T-60s API prices if this is a T-60 trigger
        );
        
        // SIMULATION MODE: Override HOLD with random BUY
        // CRITICAL: This MUST NOT happen in production!
        let wasSimulationOverride = false;
        if (isSimulation && result.decision === 'hold') {
          await orchestrationLogger.warn('BRAIN_DECISION',
            `[SIMULATION] Account ${account.id}: Overriding HOLD with random BUY`
          );
          
          // Get strategy to find available DNA strands
          const strategy = await db
            .select()
            .from(savedStrategies)
            .where(eq(savedStrategies.id, account.assignedStrategyId!))
            .limit(1);
          
          if (strategy.length > 0 && strategy[0].dnaStrands) {
            let dnaStrands = strategy[0].dnaStrands as any[];
            
            // CRITICAL: Filter DNA strands to only epics closing at this window
            if (filterEpicsForWindow && filterEpicsForWindow.length > 0) {
              dnaStrands = dnaStrands.filter((dna: any) => 
                filterEpicsForWindow!.includes(dna.epic || 'SOXL')
              );
            }
            
            if (dnaStrands.length > 0) {
              // Pick a random DNA strand from filtered list
              const randomIndex = Math.floor(Math.random() * dnaStrands.length);
              const randomDna = dnaStrands[randomIndex];
              
              // Override the result
              result = {
                ...result,
                decision: 'buy',
                winningEpic: randomDna.epic || result.winningEpic,
                winningIndicator: randomDna.indicatorName || 'SimulationOverride',
                winningTestId: randomDna.sourceTestId || null,
                leverage: randomDna.leverage || 1,
                stopLoss: randomDna.stopLoss || null,
                winningTimeframe: randomDna.timeframe || '5min',
                // CRITICAL: Include indicator params so validation can reproduce the signal
                indicatorParams: randomDna.indicatorParams || randomDna.params || {},
              };
              wasSimulationOverride = true;
              
              await orchestrationLogger.info('BRAIN_DECISION',
                `[SIMULATION] Picked random DNA: ${result.winningIndicator} on ${result.winningEpic}`
              );
            }
          }
        }
        
        // Determine windowCloseTime for this account
        // Calculate from marketCloseTime if not provided explicitly
        let effectiveWindowCloseTime = windowCloseTime;
        if (!effectiveWindowCloseTime && marketCloseTime) {
          const hours = marketCloseTime.getUTCHours().toString().padStart(2, '0');
          const minutes = marketCloseTime.getUTCMinutes().toString().padStart(2, '0');
          const seconds = marketCloseTime.getUTCSeconds().toString().padStart(2, '0');
          effectiveWindowCloseTime = `${hours}:${minutes}:${seconds}`;
        }
        effectiveWindowCloseTime = effectiveWindowCloseTime || 'unknown';
        
        // Check if rebalancing is needed for this account/window
        const rebalanceResult = fundTracker.calculateRebalanceNeeded(
          account.id,
          effectiveWindowCloseTime
        );
        
        // Store decision
        // Determine if this is HMH hold_trail: HMH enabled + HOLD decision + existing position
        // For now, just pass the base decision - the trade_queue will determine hold_trail
        const decision: BrainDecision = {
          accountId: account.id,
          strategyId: account.assignedStrategyId!,
          decision: result.decision,  // Will be updated to 'hold_trail' in trade_queue if HMH applies
          epic: result.winningEpic,
          timeframe: result.winningTimeframe,
          leverage: result.leverage,
          stopLoss: result.stopLoss,
          guaranteedStopEnabled: result.guaranteedStopEnabled || false,
          // HMH (Hold Means Hold) - set new SL on HOLD instead of closing
          hmhEnabled: result.hmhEnabled || false,
          hmhStopLossOffset: result.hmhStopLossOffset ?? null,
          indicatorName: result.winningIndicator,
          conflictMode: result.conflictMode,
          calculatedAt: new Date(),
          executionTimeMs: result.executionTimeMs,
          windowCloseTime: effectiveWindowCloseTime,
          tradeId: null, // Will be set after storeBrainResult
          wasSimulationOverride,
          rebalanceNeeded: rebalanceResult.needed,
        };

        // Update trade queue metrics used by Settings dropdown ordering at T-15s.
        // - highest_pnl -> accounts.profitLoss
        // - highest_sharpe / highest_winrate -> winning test metrics from the brain result
        if (isT60Trigger && effectiveMarketCloseTime && result.decision === 'buy') {
          try {
            const winner = result.allTests?.find((t: any) => t.testId === result.winningTestId);
            const sharpe = typeof winner?.sharpe === 'number' ? winner.sharpe : 0;
            const winRate = typeof winner?.winRate === 'number' ? winner.winRate : 0;
            const pnl = typeof account.profitLoss === 'number' ? account.profitLoss : 0;

            tradeQueue.updateMetrics(effectiveMarketCloseTime, account.id, { sharpe, winRate, pnl });
          } catch (metricError: any) {
            await orchestrationLogger.warn('TRADE_QUEUE',
              `[executeBrainCalculations] Failed to update queue metrics for account ${account.id}: ${metricError.message}`,
              { accountId: account.id }
            );
          }
        }
        
        // Update bot state with last brain decision
        botStateManager.setLastBrainDecision(account.id, {
          timestamp: new Date(),
          decision: result.decision,
          winningIndicator: result.winningIndicator,
          leverage: result.leverage,
          stopLoss: result.stopLoss,
          epic: result.winningEpic,
          timeframe: result.winningTimeframe,
        });
        
        // Store complete brain result for validation
        const tradeId = await storeBrainResult(
          account.id, 
          account.assignedStrategyId!, 
          effectiveWindowCloseTime,
          result,
          isSimulation
        );
        
        // Update decision with tradeId
        if (tradeId) {
          decision.tradeId = tradeId;
        }
        
        const executionMs = Date.now() - startTime;
        
        // Log decision
        const overrideLabel = wasSimulationOverride ? ' [SIM-OVERRIDE]' : '';
        const rebalanceLabel = rebalanceResult.needed ? ' [REBALANCE]' : '';
        const t60Label = isT60Trigger ? ' [T-60]' : '';
        await orchestrationLogger.info('BRAIN_DECISION', 
          `Account ${account.id}: ${result.decision.toUpperCase()} ${result.winningEpic}${t60Label}${overrideLabel}${rebalanceLabel} (${executionMs}ms)`, 
          {
            accountId: account.id,
            strategyId: account.assignedStrategyId!,
            epic: result.winningEpic,
            data: {
              decision: result.decision,
              indicator: result.winningIndicator,
              timeframe: result.winningTimeframe,
              leverage: result.leverage,
              stopLoss: result.stopLoss,
              conflictMode: result.conflictMode,
              executionTimeMs: executionMs,
              windowCloseTime: effectiveWindowCloseTime,
              wasSimulationOverride,
              rebalanceNeeded: rebalanceResult.needed,
              rebalanceFromWindow: rebalanceResult.fromWindow,
              rebalanceExcessPct: rebalanceResult.excessPct,
              tradeId,
              isT60Trigger,
            },
          }
        );
        
        // NOTE: processAccountBrainComplete is now called AFTER all brain calculations complete
        // to ensure close/leverage operations happen SEQUENTIALLY, not in parallel
        // (The parallel API calls were hitting rate limits and causing account switch conflicts)
        
        return { 
          decision, 
          account,
          effectiveMarketCloseTime 
        };
        
      } catch (error: any) {
        await orchestrationLogger.logError('BRAIN_ERROR', 
          `Failed to calculate for account ${account.id}`, 
          error,
          {
            accountId: account.id,
            strategyId: account.assignedStrategyId!,
          }
        );
        return null;
      }
    });
    
    // Execute all brain calculations - parallel for T-60, sequential otherwise
    // Brain calculations are CPU-bound (Python), so parallel is safe here
    type BrainResult = { decision: BrainDecision; account: any; effectiveMarketCloseTime: Date | undefined } | null;
    let results: BrainResult[];
    
    if (isT60Trigger) {
      // === LIVE-FIRST OPTIMIZATION with IMMEDIATE PROCESSING ===
      // Each brain result is processed (close/leverage) AS SOON AS it completes
      // Live accounts run first; demo accounts start AFTER all live are done
      // This prevents a slow brain from blocking others in the same group
      
      const apiCallDelayMs = await getApiCallDelay();
      results = [];
      
      // Separate promises by account type (maintaining original order within each group)
      const livePromiseIndices: number[] = [];
      const demoPromiseIndices: number[] = [];
      
      activeAccounts.forEach((account: any, index: number) => {
        if (account.accountType === 'live') {
          livePromiseIndices.push(index);
        } else {
          demoPromiseIndices.push(index);
        }
      });
      
      /**
       * Process brain results AS THEY COMPLETE (not waiting for all)
       * Uses Promise.race pattern to handle each completion immediately
       */
      const processAsTheyComplete = async (
        promiseIndices: number[],
        accountType: 'live' | 'demo'
      ): Promise<BrainResult[]> => {
        const completedResults: BrainResult[] = [];
        
        // Create wrapper promises that include their index
        const pendingPromises = promiseIndices.map((originalIdx) => ({
          originalIdx,
          promise: brainPromises[originalIdx].then(result => ({ originalIdx, result }))
        }));
        
        let remaining = [...pendingPromises];
        
        while (remaining.length > 0) {
          // Wait for the NEXT one to complete (not all)
          const completed = await Promise.race(remaining.map(p => p.promise));
          
          // Remove it from pending
          remaining = remaining.filter(p => p.originalIdx !== completed.originalIdx);
          
          const result = completed.result;
          const account = activeAccounts[completed.originalIdx];
          const brainStartTime = Date.now();
          
          if (result?.decision) {
            decisions.push(result.decision);
            completedResults.push(result);
            
            // Log brain completion with timestamp
            const brainEndTime = new Date().toISOString();
            console.log(`[BrainQueue] ${brainEndTime} Account ${account.id} (${account.accountName}) → BRAIN_COMPLETE (${result.decision.decision.toUpperCase()}) [${accountType}]`);
            
            // Process close/leverage IMMEDIATELY for this account
            if (result.effectiveMarketCloseTime) {
              const closeStartTime = new Date().toISOString();
              console.log(`[BrainQueue] ${closeStartTime} Account ${account.id} (${account.accountName}) → CLOSING_POSITION`);
              
              await tradeQueue.processAccountBrainComplete(
                result.effectiveMarketCloseTime,
                result.account.id,
                result.decision,
                {
                  capitalAccountId: result.account.capitalAccountId || result.account.accountId,
                  accountType,
                  accountName: result.account.accountName || `Account_${result.account.id}`,
                }
              );
              
              const readyTime = new Date().toISOString();
              console.log(`[BrainQueue] ${readyTime} Account ${account.id} (${account.accountName}) → READY_TO_BUY [waiting for T-15s]`);
              
              if (apiCallDelayMs > 0) {
                await new Promise(resolve => setTimeout(resolve, apiCallDelayMs));
              }
            }
          } else {
            completedResults.push(result);
            console.log(`[BrainQueue] ${new Date().toISOString()} Account ${account.id} (${account.accountName}) → BRAIN_COMPLETE (null/error) [${accountType}]`);
          }
        }
        
        return completedResults;
      };
      
      // --- PHASE 1: LIVE ACCOUNTS - process each as it completes ---
      if (livePromiseIndices.length > 0) {
        await orchestrationLogger.info('BRAIN_STARTED', 
          `🔥 LIVE-FIRST: Processing ${livePromiseIndices.length} live account(s) - each processed immediately as it completes`
        );
        
        const liveResults = await processAsTheyComplete(livePromiseIndices, 'live');
        results.push(...liveResults);
        
        const liveBuyCount = liveResults.filter(r => r?.decision?.decision === 'buy').length;
        await orchestrationLogger.info('BRAIN_DECISION', 
          `✅ Live accounts ALL ready: ${liveBuyCount} BUY, ${livePromiseIndices.length - liveBuyCount} HOLD`
        );
      }
      
      // --- PHASE 2: DEMO ACCOUNTS - process each as it completes (after ALL live done) ---
      if (demoPromiseIndices.length > 0) {
        await orchestrationLogger.info('BRAIN_STARTED', 
          `Processing ${demoPromiseIndices.length} demo account(s) - each processed immediately as it completes`
        );
        
        const demoResults = await processAsTheyComplete(demoPromiseIndices, 'demo');
        results.push(...demoResults);
        
        const demoBuyCount = demoResults.filter(r => r?.decision?.decision === 'buy').length;
        await orchestrationLogger.info('BRAIN_DECISION', 
          `✅ Demo accounts ALL ready: ${demoBuyCount} BUY, ${demoPromiseIndices.length - demoBuyCount} HOLD`
        );
      }
      
    } else {
      // SEQUENTIAL execution for other modes (backwards compatible)
      results = [];
      for (const promise of brainPromises) {
        results.push(await promise);
      }
    }
    
    // Collect any remaining successful decisions (for non-T60 mode)
    // Note: For T-60 mode, decisions are already populated above
    if (!isT60Trigger) {
      for (const result of results) {
        if (result?.decision) {
          decisions.push(result.decision);
        }
      }
    }
    
    const buyCount = decisions.filter(d => d.decision === 'buy').length;
    const holdCount = decisions.filter(d => d.decision === 'hold').length;
    const overrideCount = decisions.filter(d => d.wasSimulationOverride).length;
    const rebalanceCount = decisions.filter(d => d.rebalanceNeeded).length;
    
    let summary = `Brain calculations complete: ${buyCount} BUY, ${holdCount} HOLD`;
    if (overrideCount > 0) summary += ` (${overrideCount} simulation overrides)`;
    if (rebalanceCount > 0) summary += ` (${rebalanceCount} need rebalancing)`;
    
    await orchestrationLogger.info('BRAIN_DECISION', summary);
    
    // === WRITE CONSOLIDATED BRAIN SUMMARY TO CLOSING LOG ===
    const closingLogger = orchestrationTimer.getClosingLogger();
    if (closingLogger) {
      closingLogger.log('');
      closingLogger.log('╔══════════════════════════════════════════════════════════════════════════════╗');
      closingLogger.log('║                     BRAIN CALCULATIONS SUMMARY                               ║');
      closingLogger.log('╚══════════════════════════════════════════════════════════════════════════════╝');
      closingLogger.log(`Total accounts processed: ${decisions.length}`);
      closingLogger.log(`BUY signals: ${buyCount}`);
      closingLogger.log(`HOLD signals: ${holdCount}`);
      if (overrideCount > 0) closingLogger.log(`Simulation overrides: ${overrideCount}`);
      if (rebalanceCount > 0) closingLogger.log(`Rebalancing needed: ${rebalanceCount}`);
      closingLogger.log('');
      closingLogger.log('Per-Account Decisions:');
      for (const d of decisions) {
        const decision = d.decision.toUpperCase();
        const indicator = d.indicatorName || 'N/A';
        const epic = d.epic || 'N/A';
        const leverage = d.leverage ?? 'N/A';
        const marketClosed = d.marketClosedOverride ? ' [MARKET CLOSED]' : '';
        closingLogger.log(`  Account ${d.accountId}: ${decision} - ${indicator} on ${epic} (leverage: ${leverage})${marketClosed}`);
        if (d.decision === 'hold' && d.marketClosedReason) {
          closingLogger.log(`    Reason: ${d.marketClosedReason}`);
        }
      }
      closingLogger.log('================================================================================');
      closingLogger.log('');
    }
    
    // NOTE: Leverage checking is now handled INSIDE processAccountBrainComplete (trade_queue.ts)
    // with caching to skip redundant API calls. The old duplicate check here has been removed.
    // See: trade_queue.ts -> processAccountBrainComplete -> checkAndAdjustLeverage (with capitalAccountId)
    
    return decisions;
    
  } catch (error: any) {
    await orchestrationLogger.logError('BRAIN_ERROR', 'Brain orchestration failed', error);
    return decisions;
  }
}

/**
 * Get brain decision for a specific account (from stored decisions)
 */
export function getBrainDecision(decisions: BrainDecision[], accountId: number): BrainDecision | undefined {
  return decisions.find(d => d.accountId === accountId);
}

/**
 * Get all BUY decisions (for opening trades)
 */
export function getBuyDecisions(decisions: BrainDecision[]): BrainDecision[] {
  return decisions.filter(d => d.decision === 'buy');
}
