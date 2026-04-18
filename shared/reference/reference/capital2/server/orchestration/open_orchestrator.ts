/**
 * Open Trades Orchestrator (Enhanced for Simulation/Live Trading)
 * 
 * Called at T-15 seconds before market close:
 * 1. Receive BrainDecisions from T-4 brain calculations
 * 2. Receive bid/ask data from T-30 close orchestrator
 * 3. Calculate position size using mid-price
 * 4. Fire trades via API queue (200ms apart, critical priority)
 * 5. Update actual_trades with dealReference
 * 6. Queue for reconciliation at T+1
 * 
 * IMPORTANT:
 * - Uses mid-price (average of bid/ask) for order placement
 * - Position size = (availableFunds * leverage) / midPrice
 * - All trades are BUY direction (strategy is long-only)
 */

import { orchestrationLogger } from './logger';
import { apiQueue } from './api_queue';
import { fundTracker } from './fund_tracker';
import { confirmDeal, type DealConfirmationResult } from '../services/deal_confirmation_service';
import { connectionManager } from './connection_manager';
import { getDb } from '../db';
import { accounts, actualTrades, marketInfo } from '../../drizzle/schema';
import { eq, and, isNotNull, desc, gte } from 'drizzle-orm';
import type { BrainDecision } from './brain_orchestrator';
import type { CloseResult } from './close_orchestrator';
import { orchestrationTimer } from './timer';
import { getMarketInfoForEpic } from '../services/market_info_service';

interface TradeToFire {
  accountId: number;
  strategyId: number;
  tradeId: number;           // actual_trades.id from brain orchestrator
  epic: string;
  direction: 'BUY' | 'SELL';
  leverage: number;
  stopLoss: number | null;
  guaranteedStopEnabled: boolean; // Whether to use guaranteed stop loss
  indicatorName: string;
  windowCloseTime: string;
  availableFunds: number;
  bidAsk: { bid: number; ask: number; minSize: number; minSizeIncrement: number };
  positionSize: number;
  midPrice: number;
  client: any;
  accountType: string;
  capitalAccountId: string;  // Capital.com sub-account ID for switching
  wasSimulationOverride: boolean;
}

interface FiredTrade {
  trade: TradeToFire;
  dealReference: string | null;
  dealId: string | null;
  entryPrice: number | null;
  error?: string;
  errorCode?: string;  // Capital.com error code (e.g., "error.service.risk-check")
}

/**
 * Result of opening trades for a window
 */
export interface OpenResult {
  windowCloseTime: string;
  tradesAttempted: number;
  tradesSucceeded: number;
  tradesFailed: number;
  firedTrades: FiredTrade[];
  errors: string[];
}

/**
 * Execute open trades for a specific window
 * Called at T-15 seconds before market close
 * 
 * @param windowCloseTime - The window close time string (e.g., "20:00:00")
 * @param brainDecisions - BUY decisions from T-4 brain calculations
 * @param closeResults - Results from T-30 close orchestrator (includes bid/ask)
 * @param isSimulation - If true, running in simulation mode
 */
export async function executeOpenTrades(
  windowCloseTime: string,
  brainDecisions: BrainDecision[],
  closeResults: CloseResult[],
  isSimulation: boolean = false
): Promise<OpenResult> {
  const modeLabel = isSimulation ? '[SIMULATION] ' : '';
  const result: OpenResult = {
    windowCloseTime,
    tradesAttempted: 0,
    tradesSucceeded: 0,
    tradesFailed: 0,
    firedTrades: [],
    errors: [],
  };
  
  await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
    `${modeLabel}Starting open trades for window ${windowCloseTime}`
  );
  
  try {
    // Phase 3: During T-15 open window, give OPEN(LIVE) calls priority inside the CRITICAL API queue.
    // This ensures that if LIVE trades are ready, we start them immediately even if close/leverage is still catching up.
    apiQueue.startOpenWindowPriority(windowCloseTime, 120);

    const db = await getDb();
    if (!db) {
      result.errors.push('Database not available');
      return result;
    }
    
    // 1. Filter to BUY decisions for this window
    const buyDecisions = brainDecisions.filter(
      d => d.decision === 'buy' && d.windowCloseTime === windowCloseTime
    );
    
    await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
      `${modeLabel}Found ${buyDecisions.length} BUY decisions for window ${windowCloseTime}`
    );
    
    if (buyDecisions.length === 0) {
      await orchestrationLogger.info('POSITION_OPEN_SUCCESS', 
        `${modeLabel}No BUY decisions - all accounts HOLD`
      );
      return result;
    }
    
    // 2. Combine bid/ask data from close results (legacy path)
    const bidAskMap = new Map<string, { bid: number; ask: number; minSize: number; minSizeIncrement: number }>();
    for (const closeResult of closeResults) {
      closeResult.bidAskFetched.forEach((data, epic) => {
        bidAskMap.set(epic, data);
      });
    }
    
    // 2b. Fetch bid/ask directly for any epics not in closeResults
    // This is the PRIMARY path now that T-30s close window is removed
    const epicsNeedingBidAsk = [...new Set(buyDecisions.map(d => d.epic))].filter(epic => !bidAskMap.has(epic));
    
    if (epicsNeedingBidAsk.length > 0) {
      await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
        `${modeLabel}Fetching fresh bid/ask for ${epicsNeedingBidAsk.length} epics: ${epicsNeedingBidAsk.join(', ')}`
      );
      
      // Get a client - try demo first, then live
      const client = connectionManager.getClient('demo') || connectionManager.getClient('live');
      
      if (client) {
        for (const epic of epicsNeedingBidAsk) {
          try {
            const marketInfo = await client.getMarketInfo(epic);
            
            if (marketInfo) {
              // Capital.com API returns snapshot.bid and snapshot.offer (not ask)
              const bid = marketInfo.snapshot?.bid;
              const ask = marketInfo.snapshot?.offer;
              const minSize = marketInfo.dealingRules?.minDealSize?.value || 1;
              const minSizeIncrement = marketInfo.dealingRules?.minSizeIncrement?.value || 0.01;
              
              if (bid && ask) {
                bidAskMap.set(epic, { bid, ask, minSize, minSizeIncrement });
                await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
                  `${modeLabel}Fetched bid/ask for ${epic}: ${bid}/${ask} (min: ${minSize}, increment: ${minSizeIncrement})`
                );
              } else {
                await orchestrationLogger.warn('POSITION_OPEN_FAILED', 
                  `${modeLabel}No bid/ask available for ${epic} - market may be closed`,
                  { epic }
                );
              }
            }
          } catch (error: any) {
            await orchestrationLogger.warn('POSITION_OPEN_FAILED', 
              `${modeLabel}Failed to fetch bid/ask for ${epic}: ${error.message}`,
              { epic }
            );
          }
        }
      } else {
        await orchestrationLogger.warn('POSITION_OPEN_FAILED', 
          `${modeLabel}No API client available to fetch bid/ask`
        );
      }
    }
    
    // 3. Prepare trades to fire
    const tradesToFire: TradeToFire[] = [];
    
    for (const decision of buyDecisions) {
      const trade = await prepareTradeFromDecision(decision, bidAskMap, isSimulation);
      if (trade) {
        tradesToFire.push(trade);
      } else {
        result.errors.push(`Failed to prepare trade for account ${decision.accountId}`);
      }
    }
    
    result.tradesAttempted = tradesToFire.length;
    
    await orchestrationLogger.info('POSITION_OPEN_REQUESTED',
      `${modeLabel}Prepared ${tradesToFire.length} trades to fire`,
      { data: { count: tradesToFire.length } }
    );
    
    if (tradesToFire.length === 0) {
      result.errors.push('No trades could be prepared');
      return result;
    }
    
    // 4. TRADE CANNON: Fire all trades (200ms apart)
    const firedTrades = await fireAllTrades(tradesToFire, isSimulation);
    result.firedTrades = firedTrades;
    
    // 5. Wait 500ms for orders to execute
    await orchestrationLogger.info('POSITION_OPEN_REQUESTED',
      `${modeLabel}Waiting 500ms for ${firedTrades.length} orders to execute`
    );
    await new Promise(resolve => setTimeout(resolve, 500));
    
    // 6. Update actual_trades with deal info
    await updateActualTrades(firedTrades, isSimulation);
    
    // Count successes and failures
    result.tradesSucceeded = firedTrades.filter(t => t.dealReference !== null).length;
    result.tradesFailed = firedTrades.filter(t => t.dealReference === null).length;
    
    await orchestrationLogger.info('POSITION_OPEN_SUCCESS',
      `${modeLabel}Open sequence complete: ${result.tradesSucceeded} succeeded, ${result.tradesFailed} failed`,
      { data: result }
    );
    
    return result;
    
  } catch (error: any) {
    result.errors.push(`Open orchestration failed: ${error.message}`);
    await orchestrationLogger.logError('POSITION_OPEN_FAILED', 'Open trades orchestration failed', error);
    return result;
  } finally {
    // End the open-window boost after orchestration completes.
    // (A TTL still exists as a safety net in api_queue.ts.)
    apiQueue.endOpenWindowPriority();
  }
}

/**
 * Prepare trade from a BrainDecision
 */
async function prepareTradeFromDecision(
  decision: BrainDecision,
  bidAskMap: Map<string, { bid: number; ask: number; minSize: number; minSizeIncrement: number }>,
  isSimulation: boolean
): Promise<TradeToFire | null> {
  const { accountId, strategyId, epic, leverage, stopLoss, guaranteedStopEnabled, indicatorName, windowCloseTime, tradeId, wasSimulationOverride } = decision;
  
  try {
    // 0. SAFETY CHECK: Verify the epic closes at the expected window time
    // This prevents firing trades for wrong epics (e.g., SOXL in 21:00 TECL window)
    const epicCloseTime = orchestrationTimer.getEpicCloseTime(epic);
    if (epicCloseTime) {
      const epicCloseTimeStr = epicCloseTime.toISOString().slice(11, 19); // "HH:MM:SS"
      
      // Allow 2-minute tolerance for timing differences
      const decisionTime = windowCloseTime.split(':').map(Number);
      const epicTime = epicCloseTimeStr.split(':').map(Number);
      const decisionMinutes = decisionTime[0] * 60 + decisionTime[1];
      const epicMinutes = epicTime[0] * 60 + epicTime[1];
      const diffMinutes = Math.abs(decisionMinutes - epicMinutes);
      
      if (diffMinutes > 2) {
        await orchestrationLogger.warn('POSITION_OPEN_FAILED', 
          `⛔ BLOCKED: Epic ${epic} closes at ${epicCloseTimeStr}, not window ${windowCloseTime} (diff: ${diffMinutes}min)`,
          { accountId, epic, data: { windowCloseTime, epicCloseTimeStr, diffMinutes } }
        );
        
        // Log to closing file for visibility
        const closingLogger = orchestrationTimer.getClosingLogger();
        closingLogger?.log(
          `⛔ BLOCKED TRADE: Account ${accountId} tried to open ${epic} in window ${windowCloseTime}, but ${epic} closes at ${epicCloseTimeStr}`
        );
        
        return null;
      }
    }
    
    // 1. Get bid/ask for this epic
    const bidAsk = bidAskMap.get(epic);
    
    if (!bidAsk) {
      await orchestrationLogger.warn('POSITION_OPEN_FAILED', 
        `No bid/ask data for ${epic} - cannot open position`,
        { accountId, epic }
      );
      return null;
    }
    
    // 2. Get account details
    const db = await getDb();
    if (!db) return null;
    
    const accountRecords = await db
      .select()
      .from(accounts)
      .where(eq(accounts.id, accountId))
      .limit(1);
    
    if (accountRecords.length === 0) {
      await orchestrationLogger.warn('POSITION_OPEN_FAILED', 
        `Account ${accountId} not found`,
        { accountId }
      );
      return null;
    }
    
    const account = accountRecords[0];
    
    // ═══════════════════════════════════════════════════════════════════════════
    // 3. Get available funds using SIMPLE approach:
    //    - Get fresh balance from Capital.com
    //    - Check tradingSessionState for prior windows
    //    - Calculate allocation % based on carryOver logic
    // ═══════════════════════════════════════════════════════════════════════════
    
    // Get fresh balance from Capital.com
    const client = connectionManager.getClient(account.accountType as 'demo' | 'live');
    let freshBalance = account.available || account.balance || 0;
    
    if (client) {
      try {
        await client.switchAccount(account.accountId);
        const capitalAccounts = await client.getAccounts();
        const thisAccount = capitalAccounts?.find((a: any) => a.accountId === account.accountId);
        if (thisAccount?.balance?.available) {
          freshBalance = thisAccount.balance.available;
        }
      } catch (balErr: any) {
        // Use DB balance as fallback
        console.warn(`[OpenOrchestrator] Failed to get fresh balance for ${accountId}: ${balErr.message}`);
      }
    }
    
    // Get strategy's totalAccountBalancePct
    let totalAccountBalancePct = 99; // Default
    if (account.assignedStrategyId) {
      try {
        const { savedStrategies } = await import('../../drizzle/schema');
        const [strategy] = await db
          .select({ windowConfig: savedStrategies.windowConfig })
          .from(savedStrategies)
          .where(eq(savedStrategies.id, account.assignedStrategyId))
          .limit(1);
        if (strategy?.windowConfig?.totalAccountBalancePct) {
          totalAccountBalancePct = strategy.windowConfig.totalAccountBalancePct;
        }
      } catch (stratErr) {
        // Use default
      }
    }
    
    // Calculate allocation using tradingSessionState (simple approach)
    const { tradingSessionState } = await import('../services/trading_session_state');
    const allocation = await tradingSessionState.calculateAllocationAtT15(
      accountId,
      windowCloseTime,
      freshBalance,
      totalAccountBalancePct
    );
    
    const availableFunds = allocation.availableFunds;
    const availablePct = allocation.allocationPct;
    
    // Also update fundTracker for consistency (deprecated but kept for logging)
    const ftDebug = fundTracker.getDebugState(accountId);
    
    await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
      `Account ${accountId}: ${availablePct.toFixed(1)}% available ($${availableFunds.toFixed(2)}) [window: ${windowCloseTime}]`,
      { accountId, data: { 
        freshBalance,
        totalAccountBalancePct,
        allocationPct: availablePct, 
        availableFunds, 
        windowCloseTime, 
        breakdown: allocation.breakdown,
        ftDebug 
      }}
    );
    
    if (availableFunds <= 0) {
      await orchestrationLogger.warn('POSITION_OPEN_FAILED', 
        `Account ${accountId}: No funds available for window ${windowCloseTime}. Breakdown: ${allocation.breakdown}`,
        { accountId, data: { windowCloseTime, allocation, ftDebug } }
      );
      // Also log to the closing sequence file for fast troubleshooting (users primarily inspect logs/closing/*)
      const closingLoggerNoFunds = orchestrationTimer.getClosingLogger();
      closingLoggerNoFunds?.log(
        `⚠️ SKIP OPEN: Account ${accountId} has $0.00 available for window ${windowCloseTime}. Allocation: ${allocation.breakdown}`
      );
      return null;
    }
    
    // 4. Verify API client (already obtained above for balance check)
    if (!client) {
      await orchestrationLogger.warn('POSITION_OPEN_FAILED', 
        `No ${account.accountType} API client available for account ${accountId}`,
        { accountId }
      );
      return null;
    }
    
    // 5. Calculate mid-price and position size
    const midPrice = (bidAsk.bid + bidAsk.ask) / 2;
    const effectiveLeverage = leverage || 1;
    const positionSize = calculatePositionSize(availableFunds, midPrice, effectiveLeverage, bidAsk.minSize, bidAsk.minSizeIncrement);
    
    // Log to closing sequence file if available
    const closingLogger = orchestrationTimer.getClosingLogger();
    closingLogger?.tradeCalculation(accountId, account.accountName || `Account_${accountId}`, {
      epic,
      direction: 'BUY',
      availableBalance: availableFunds,
      bid: bidAsk.bid,
      ask: bidAsk.ask,
      midPrice,
      leverage: effectiveLeverage,
      stopLoss: stopLoss || 0,
      contracts: positionSize,
      notionalValue: positionSize * midPrice,
    });
    
    await orchestrationLogger.info('POSITION_SIZE_CALCULATED', 
      `Position size: ${positionSize} (mid: ${midPrice.toFixed(4)}, leverage: ${effectiveLeverage}x)`,
      { 
        accountId, 
        epic,
        data: { 
          availableFunds, 
          bid: bidAsk.bid,
          ask: bidAsk.ask,
          midPrice,
          leverage: effectiveLeverage,
          positionSize,
          minSize: bidAsk.minSize,
          minSizeIncrement: bidAsk.minSizeIncrement,
        }
      }
    );
    
    if (positionSize < bidAsk.minSize) {
      await orchestrationLogger.warn('POSITION_OPEN_FAILED', 
        `Position size ${positionSize} below minimum ${bidAsk.minSize}`,
        { accountId, epic }
      );
      return null;
    }
    
    // FIX: If tradeId is null/undefined, try to find the trade by accountId + window + epic
    let resolvedTradeId = tradeId;
    if (!resolvedTradeId || resolvedTradeId === 0) {
      const { gte } = await import('drizzle-orm');
      const todayStart = new Date();
      todayStart.setUTCHours(0, 0, 0, 0);
      
      const existingTrade = await db
        .select({ id: actualTrades.id })
        .from(actualTrades)
        .where(
          and(
            eq(actualTrades.accountId, accountId),
            eq(actualTrades.windowCloseTime, windowCloseTime),
            eq(actualTrades.epic, epic),
            gte(actualTrades.createdAt, todayStart)
          )
        )
        .orderBy(desc(actualTrades.createdAt))
        .limit(1);
      
      if (existingTrade.length > 0) {
        resolvedTradeId = existingTrade[0].id;
        await orchestrationLogger.info('POSITION_OPEN_REQUESTED',
          `Resolved missing tradeId: Found trade ${resolvedTradeId} for account ${accountId}, window ${windowCloseTime}, epic ${epic}`,
          { accountId, epic, data: { resolvedTradeId, originalTradeId: tradeId } }
        );
      } else {
        await orchestrationLogger.warn('POSITION_OPEN_FAILED',
          `Cannot find trade record for account ${accountId}, window ${windowCloseTime}, epic ${epic}. TradeId was ${tradeId}. Skipping trade update.`,
          { accountId, epic, data: { tradeId, windowCloseTime } }
        );
        // Still return the trade object, but updateActualTrades will handle the missing tradeId
        resolvedTradeId = null;
      }
    }
    
    return {
      accountId,
      strategyId,
      tradeId: resolvedTradeId,
      epic,
      direction: 'BUY',
      leverage: effectiveLeverage,
      stopLoss,
      guaranteedStopEnabled: guaranteedStopEnabled || false,
      indicatorName,
      windowCloseTime,
      availableFunds,
      bidAsk,
      positionSize,
      midPrice,
      client,
      accountType: account.accountType,
      capitalAccountId: account.accountId, // Capital.com sub-account ID
      wasSimulationOverride,
    };
    
  } catch (error: any) {
    await orchestrationLogger.logError('POSITION_OPEN_FAILED', 
      `Failed to prepare trade for account ${accountId}`,
      error,
      { accountId }
    );
    return null;
  }
}

/**
 * Fire all trades sequentially via API queue (200ms apart)
 */
async function fireAllTrades(tradesToFire: TradeToFire[], isSimulation: boolean): Promise<FiredTrade[]> {
  const firedTrades: FiredTrade[] = [];
  const modeLabel = isSimulation ? '[SIMULATION] ' : '';
  
  await orchestrationLogger.info('POSITION_OPEN_REQUESTED',
    `${modeLabel}Firing ${tradesToFire.length} trades (trade cannon - 200ms spacing)`,
    { data: { count: tradesToFire.length } }
  );
  
  for (let i = 0; i < tradesToFire.length; i++) {
    const trade = tradesToFire[i];
      const { accountId, epic, client, positionSize, stopLoss, guaranteedStopEnabled, midPrice, capitalAccountId } = trade;
    
    try {
      // Fire the trade via API queue (critical priority, bypasses quiet period)
      // NOTE: Account switch happens INSIDE the queue callback to avoid race conditions
      // when multiple trades are enqueued in rapid succession
      const result = await new Promise<{ dealReference: string; dealId?: string }>((resolve, reject) => {
        apiQueue.enqueue({
          priority: 'critical',
          isCritical: true, // Bypass quiet period
          environment: trade.accountType === 'live' ? 'live' : 'demo',
          operation: 'open',
          description: `OPEN ${trade.epic} accountId=${trade.accountId} env=${trade.accountType}`,
          fn: async () => {
            // Switch to the correct Capital.com sub-account INSIDE the callback
            // This ensures we're on the right account when the trade actually fires
            // CRITICAL: The client is shared across all accounts, so we MUST switch before each trade
            if (capitalAccountId) {
              console.log(`[OpenOrchestrator] Account ${accountId}: Switching to Capital.com ${capitalAccountId}`);
              const switchSuccess = await client.switchAccount(capitalAccountId);
              if (!switchSuccess) {
                throw new Error(`Failed to switch to Capital.com account ${capitalAccountId}`);
              }
              console.log(`[OpenOrchestrator] Account ${accountId}: ✓ Now on Capital.com ${capitalAccountId}`);
            } else {
              console.warn(`[OpenOrchestrator] Account ${accountId}: No capitalAccountId provided!`);
            }
            
            // ===============================================================
            // GSL (Guaranteed Stop Loss) PRE-FLIGHT VALIDATION
            // ===============================================================
            
            // Fetch market info to get GSL settings and price precision
            const epicMarketInfo = await getMarketInfoForEpic(epic);
            const gslAllowed = epicMarketInfo?.guaranteedStopAllowed ?? false;
            const scalingFactor = epicMarketInfo?.scalingFactor ?? 2; // Default 2 decimals for USD
            const minGslDistancePct = parseFloat(epicMarketInfo?.minGuaranteedStopDistancePct?.toString() || '0');
            
            // Determine final GSL usage after validation
            let finalGslUsed = guaranteedStopEnabled || false;
            let gslChangeReason: string | undefined;
            
            if (guaranteedStopEnabled) {
              // Check 1: Instrument supports GSL
              if (!gslAllowed) {
                finalGslUsed = false;
                gslChangeReason = `GSL not available for ${epic}`;
                console.warn(`[OpenOrchestrator] Account ${accountId}: ${gslChangeReason} - falling back to standard stop`);
              }
              // Check 2: Stop loss distance sufficient
              else if (stopLoss !== null && stopLoss < minGslDistancePct) {
                finalGslUsed = false;
                gslChangeReason = `Stop loss ${stopLoss}% < minimum GSL distance ${minGslDistancePct}%`;
                console.warn(`[OpenOrchestrator] Account ${accountId}: ${gslChangeReason} - falling back to standard stop`);
              }
            }
            
            // Calculate stop level as absolute price with DYNAMIC precision
            // stopLoss is a percentage (e.g., 7 = 7% below entry)
            // stopLevel is an absolute price (e.g., $37.20)
            let calculatedStopLevel: number | undefined;
            if (stopLoss !== null && stopLoss > 0 && midPrice > 0) {
              calculatedStopLevel = midPrice * (1 - stopLoss / 100);
              // Dynamic rounding based on instrument's scaling factor
              const multiplier = Math.pow(10, scalingFactor);
              calculatedStopLevel = Math.round(calculatedStopLevel * multiplier) / multiplier;
            }
            
            // Log GSL decision to closing sequence file
            const closingLoggerGsl = orchestrationTimer.getClosingLogger();
            closingLoggerGsl?.gslDecision(accountId, `Account_${accountId}`, {
              epic,
              dnaRequestedGsl: guaranteedStopEnabled || false,
              finalGslUsed,
              reason: gslChangeReason,
              dnaStopLossPct: stopLoss || 0,
              minGslDistancePct,
              calculatedStopLevel: calculatedStopLevel || 0,
              scalingFactor,
              gslAllowed,
            });
            
            const createResult = await client.createPosition({
              epic,
              direction: 'BUY',
              size: positionSize,
              guaranteedStop: finalGslUsed,
              stopLevel: calculatedStopLevel,
            });
            
            console.log(`[OpenOrchestrator] Account ${accountId}: Trade fired with ${finalGslUsed ? 'GUARANTEED' : 'STANDARD'} stop loss (${scalingFactor} decimals)`);
            
            if (!createResult || !createResult.dealReference) {
              throw new Error('Failed to create position - no dealReference returned');
            }
            
            await orchestrationLogger.info('POSITION_OPEN_SUCCESS',
              `${modeLabel}Trade ${i + 1}/${tradesToFire.length} fired: ${createResult.dealReference}`,
              {
                accountId,
                epic,
                data: { 
                  dealReference: createResult.dealReference,
                  dealId: createResult.dealId,
                  size: positionSize,
                }
              }
            );
            
            // Log to closing sequence file
            const closingLoggerSuccess = orchestrationTimer.getClosingLogger();
            closingLoggerSuccess?.tradeExecution(accountId, `Account_${accountId}`, createResult.dealReference, true);
            
            return { 
              dealReference: createResult.dealReference,
              dealId: createResult.dealId,
            };
          },
          maxRetries: 3,
          onSuccess: (res) => resolve(res as { dealReference: string; dealId?: string }),
          onError: reject,
        });
      });
      
      firedTrades.push({
        trade,
        dealReference: result.dealReference,
        dealId: result.dealId || null,
        entryPrice: midPrice, // Use mid-price as estimated entry
      });
      
    } catch (error: any) {
      // Extract Capital.com error details if available
      const capitalErrorCode = (error as any).capitalErrorCode || 'unknown';
      const capitalErrorMessage = (error as any).capitalErrorMessage || error.message;
      const httpStatus = (error as any).httpStatus || 'N/A';
      const errorMeaning = (error as any).errorMeaning || '';
      const errorFix = (error as any).errorFix || '';
      
      const detailedError = `[${capitalErrorCode}] ${capitalErrorMessage}`;
      
      await orchestrationLogger.logError('POSITION_OPEN_FAILED',
        `${modeLabel}Failed to fire trade ${i + 1}/${tradesToFire.length}: ${detailedError}`,
        error,
        { accountId, data: { errorCode: capitalErrorCode, httpStatus, errorMeaning, errorFix } }
      );
      
      // Log error to closing sequence file with Capital.com error code AND fix suggestion
      const closingLoggerError = orchestrationTimer.getClosingLogger();
      if (closingLoggerError) {
        closingLoggerError.tradeExecution(accountId, `Account_${accountId}`, '', false, detailedError);
        if (errorFix) {
          closingLoggerError.log(`         💡 Fix: ${errorFix}`);
        }
      }
      
      // Store the full error details for later analysis (including fix suggestion)
      const fullErrorMessage = errorFix 
        ? `${detailedError} | Fix: ${errorFix}`
        : detailedError;
      
      firedTrades.push({
        trade,
        dealReference: null,
        dealId: null,
        entryPrice: null,
        error: fullErrorMessage,
        errorCode: capitalErrorCode,
      });
    }
    
    // Wait 200ms before firing next trade (trade cannon spacing)
    if (i < tradesToFire.length - 1) {
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  }
  
  const successCount = firedTrades.filter(t => t.dealReference !== null).length;
  await orchestrationLogger.info('POSITION_OPEN_SUCCESS',
    `${modeLabel}Trade cannon complete: ${successCount}/${tradesToFire.length} trades fired`,
    { data: { total: tradesToFire.length, success: successCount } }
  );
  
  return firedTrades;
}

/**
 * Update actual_trades table with deal info from fired trades
 * This updates the pending records created by brain orchestrator
 */
async function updateActualTrades(firedTrades: FiredTrade[], isSimulation: boolean): Promise<void> {
  const db = await getDb();
  if (!db) {
    await orchestrationLogger.warn('POSITION_OPEN_FAILED', 'Database not available for updating trades');
    return;
  }
  
  const modeLabel = isSimulation ? '[SIMULATION] ' : '';
  let updatedCount = 0;
  let failedCount = 0;
  
  for (const firedTrade of firedTrades) {
    const { trade, dealReference, dealId, entryPrice, error } = firedTrade;
    const { accountId, tradeId, epic, positionSize, leverage, windowCloseTime, availableFunds } = trade;
    
    try {
      // FIX: If tradeId is null/0, try to find trade by accountId + window + epic
      let resolvedTradeId = tradeId;
      if (!resolvedTradeId || resolvedTradeId === 0) {
        const todayStart = new Date();
        todayStart.setUTCHours(0, 0, 0, 0);
        
        const existingTrade = await db
          .select({ id: actualTrades.id })
          .from(actualTrades)
          .where(
            and(
              eq(actualTrades.accountId, accountId),
              eq(actualTrades.windowCloseTime, windowCloseTime),
              eq(actualTrades.epic, epic),
              gte(actualTrades.createdAt, todayStart)
            )
          )
          .orderBy(desc(actualTrades.createdAt))
          .limit(1);
        
        if (existingTrade.length > 0) {
          resolvedTradeId = existingTrade[0].id;
          await orchestrationLogger.info('POSITION_OPEN_SUCCESS',
            `Resolved missing tradeId in updateActualTrades: Found trade ${resolvedTradeId} for account ${accountId}, window ${windowCloseTime}, epic ${epic}`,
            { accountId, epic, data: { resolvedTradeId, originalTradeId: tradeId } }
          );
        } else {
          await orchestrationLogger.warn('POSITION_OPEN_FAILED',
            `Cannot update trade: No trade record found for account ${accountId}, window ${windowCloseTime}, epic ${epic}. TradeId was ${tradeId}. DealReference: ${dealReference || 'N/A'}`,
            { accountId, epic, data: { tradeId, windowCloseTime, dealReference } }
          );
          failedCount++;
          continue; // Skip this trade
        }
      }
      
      if (dealReference) {
        // Success - update actual_trade with deal info
        // Status 'open' means the trade was successfully executed and is now open
        await db
          .update(actualTrades)
          .set({
            dealReference,
            dealId: dealId || null,
            entryPrice,
            contracts: positionSize,
            status: 'open',
            openedAt: new Date(),
          })
          .where(eq(actualTrades.id, resolvedTradeId));
        
        // Mark funds as used in fund tracker
        fundTracker.markUsed(accountId, windowCloseTime, availableFunds);
        
        // Update tradingSessionState with trade details (async, non-blocking)
        fundTracker.updateTradeDetails(accountId, windowCloseTime, {
          tradeId: resolvedTradeId,
          dealReference,
          dealId: dealId || undefined,
          marginUsed: availableFunds,
          contracts: positionSize,
          entryPrice,
        }).catch(err => {
          console.warn(`[OpenOrchestrator] tradingSessionState update warning: ${err.message}`);
        });
        
        updatedCount++;
        
        await orchestrationLogger.info('POSITION_OPEN_SUCCESS',
          `${modeLabel}Updated actual_trade ${resolvedTradeId} with dealReference ${dealReference}`,
          {
            accountId,
            epic,
            data: {
              tradeId: resolvedTradeId,
              dealReference,
              dealId,
              entryPrice,
              size: positionSize,
              leverage,
            }
          }
        );
        
      } else {
        // Failed - update actual_trade with error details
        // Status 'error' means the trade failed to execute
        // Now we store the FULL error message and Capital.com error code!
        await db
          .update(actualTrades)
          .set({
            status: 'error',
            errorMessage: error || 'Unknown error',
            errorCode: firedTrade.errorCode || null,
          })
          .where(eq(actualTrades.id, resolvedTradeId));
        
        // Log the error for debugging
        console.error(`[OpenOrchestrator] Trade ${resolvedTradeId} failed: ${error}`);
        console.error(`[OpenOrchestrator] Error code: ${firedTrade.errorCode || 'none'}`);
        
        failedCount++;
        
        await orchestrationLogger.warn('POSITION_OPEN_FAILED',
          `${modeLabel}Trade ${resolvedTradeId} failed: ${error}`,
          { accountId, epic, data: { errorCode: firedTrade.errorCode } }
        );
      }
      
    } catch (dbError: any) {
      await orchestrationLogger.logError('POSITION_OPEN_FAILED',
        `${modeLabel}Failed to update actual_trade ${resolvedTradeId || tradeId}`,
        dbError,
        { accountId, data: { tradeId, resolvedTradeId } }
      );
    }
  }
  
  await orchestrationLogger.info('POSITION_OPEN_SUCCESS',
    `${modeLabel}Updated ${updatedCount} trades, ${failedCount} failed`,
    { data: { updated: updatedCount, failed: failedCount } }
  );
}

/**
 * Calculate position size based on balance, price, leverage, and minimum size
 * Uses mid-price for accurate sizing
 * 
 * NOTE: No hidden safety margins here - the strategy's investmentPct controls allocation.
 * If trades are rejected due to RISK_CHECK, adjust the strategy's allocation % manually.
 * 
 * @param balance - Available funds (already adjusted by strategy's investmentPct)
 * @param midPrice - Mid-point of bid/ask
 * @param leverage - Leverage multiplier (from strategy, not modified)
 * @param minSize - Minimum contract size from market info
 * @param minSizeIncrement - Minimum size increment for rounding (e.g., 0.01, 0.1, 1)
 */
function calculatePositionSize(
  balance: number, 
  midPrice: number, 
  leverage: number,
  minSize: number = 1,
  minSizeIncrement: number = 0.01
): number {
  // Position size = (balance * leverage) / price
  const rawSize = (balance * leverage) / midPrice;
  
  // Round DOWN to the nearest minSizeIncrement
  // This ensures we respect the market's minimum increment (e.g., 0.01, 0.1, 1)
  // Using floor ensures we don't exceed available balance
  const incrementMultiplier = 1 / minSizeIncrement;
  let size = Math.floor(rawSize * incrementMultiplier) / incrementMultiplier;
  
  // Handle floating point precision (e.g., 0.74999999 -> 0.74)
  size = parseFloat(size.toFixed(10));
  
  // Ensure at least minimum size
  if (size < minSize) {
    // Only use minimum if we can afford it (balance check)
    const minCost = minSize * midPrice / leverage;
    if (balance >= minCost) {
      size = minSize;
    } else {
      // Can't even afford minimum - will be rejected
      size = 0;
    }
  }
  
  return size;
}
