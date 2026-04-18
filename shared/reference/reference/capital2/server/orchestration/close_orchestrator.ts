/**
 * Close Trades Orchestrator
 * 
 * Called at T-60 after brain calculations complete.
 * 
 * CRITICAL FIX (Jan 2026): Now queries Capital.com GET /positions directly
 * instead of relying on database. This ensures we close ALL positions for
 * the account, regardless of:
 * - When they were opened (could be days ago if server was offline)
 * - What session they're from
 * - Whether they match the current windowCloseTime
 * 
 * Flow:
 * 1. Query Capital.com GET /positions for all open positions
 * 2. Close ALL positions for strategy epics (not just database-tracked ones)
 * 3. Update actual_trades if matching record exists (best-effort)
 * 4. Execute rebalancing if needed
 * 5. Fetch bid/ask for epics that need new trades
 * 
 * NOTE: User's manual trades for NON-strategy epics are protected.
 */

import { orchestrationLogger } from './logger';
import { apiQueue } from './api_queue';
import { connectionManager } from './connection_manager';
import { getDb } from '../db';
import { actualTrades, accounts } from '../../drizzle/schema';
import { eq, and, isNull, isNotNull, inArray } from 'drizzle-orm';
import { fundTracker, type RebalanceResult } from './fund_tracker';
import type { BrainDecision } from './brain_orchestrator';

/**
 * Normalize time string to HH:MM:SS format
 * "21:00" -> "21:00:00"
 * "21:00:00" -> "21:00:00"
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
 * Result of closing trades for a window
 */
export interface CloseResult {
  accountId: number;
  windowCloseTime: string;
  tradesClosedCount: number;
  pendingOrdersCancelled: number;
  rebalanceExecuted: boolean;
  bidAskFetched: Map<string, { bid: number; ask: number; minSize: number; minSizeIncrement: number }>;
  freshBalance: number | null;  // Fresh available balance after closing
  errors: string[];
}

/**
 * Execute close trades for a specific window
 * Called at T-60 after brain calculations
 * 
 * CRITICAL: Queries Capital.com GET /positions directly to find ALL open positions,
 * not just database-tracked ones. This ensures we close positions even if:
 * - Server was offline for days
 * - Trade record has wrong status in DB
 * - Position was opened manually
 * 
 * @param windowCloseTime - The window close time (e.g., "20:00:00")
 * @param brainDecisions - Brain decisions from T-60 (for epic info)
 * @param isSimulation - If true, running in simulation mode
 */
export async function executeCloseTrades(
  windowCloseTime: string,
  brainDecisions: BrainDecision[],
  isSimulation: boolean = false
): Promise<CloseResult[]> {
  const results: CloseResult[] = [];
  const modeLabel = isSimulation ? '[SIMULATION] ' : '';
  
  try {
    await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
      `${modeLabel}Starting close sequence for window ${windowCloseTime}`
    );
    
    const db = await getDb();
    if (!db) {
      await orchestrationLogger.warn('POSITION_CLOSE_FAILED', 'Database not available');
      return results;
    }
    
    // Get unique accounts from brain decisions (includes both BUY and HOLD)
    const accountIds = [...new Set(brainDecisions.map(d => d.accountId))];
    
    if (accountIds.length === 0) {
      await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
        `${modeLabel}No accounts to process for window ${windowCloseTime}`
      );
      return results;
    }
    
    // Get account details from database (with strategy for window config)
    const { savedStrategies } = await import('../../drizzle/schema');
    
    const accountRecords = await db
      .select()
      .from(accounts)
      .where(inArray(accounts.id, accountIds));
    
    await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
      `${modeLabel}Processing ${accountRecords.length} accounts for close operations`
    );
    
    // Process each account - only close THAT window's epics
    for (const account of accountRecords) {
      const decision = brainDecisions.find(d => d.accountId === account.id);
      
      // Get epics for THIS window from strategy config
      let windowEpics: string[] = [];
      if (account.assignedStrategyId) {
        try {
          const [strategy] = await db
            .select()
            .from(savedStrategies)
            .where(eq(savedStrategies.id, account.assignedStrategyId))
            .limit(1);
          
          if (strategy?.windowConfig?.windows) {
            // Find the window that matches this windowCloseTime (normalize both for comparison)
            const normalizedInputTime = normalizeTimeFormat(windowCloseTime);
            const matchingWindow = strategy.windowConfig.windows.find((w: any) => {
              const normalizedWindowTime = normalizeTimeFormat(w.closeTime);
              return normalizedWindowTime === normalizedInputTime;
            });
            
            if (matchingWindow?.epics) {
              windowEpics = matchingWindow.epics;
            }
          }
          
          // If no epics in window config, try to get from DNA strands for this window
          if (windowEpics.length === 0 && strategy?.dnaStrands) {
            for (const strand of strategy.dnaStrands) {
              if (strand.epic) {
                // Check if this DNA belongs to the current window
                // (DNA may have windowCloseTime or tradingHoursType info)
                windowEpics.push(strand.epic);
              }
            }
            // Dedupe
            windowEpics = [...new Set(windowEpics)];
          }
        } catch (stratErr: any) {
          await orchestrationLogger.warn('POSITION_CLOSE_REQUESTED', 
            `${modeLabel}Failed to get strategy for account ${account.id}: ${stratErr.message}`
          );
        }
      }
      
      // Fall back to decision epic if no window epics found
      if (windowEpics.length === 0 && decision?.epic) {
        windowEpics = [decision.epic];
      }
      
      await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
        `${modeLabel}Account ${account.id}: Window ${windowCloseTime} epics: [${windowEpics.join(', ')}]`
      );
      
      const result = await closeAccountPositionsFromCapital(
        account,
        windowCloseTime,
        decision,
        isSimulation,
        windowEpics
      );
      results.push(result);
    }
    
    const totalClosed = results.reduce((sum, r) => sum + r.tradesClosedCount, 0);
    
    await orchestrationLogger.info('POSITION_CLOSE_SUCCESS', 
      `${modeLabel}Close sequence complete: ${totalClosed} positions closed across ${results.length} accounts`
    );
    
    return results;
    
  } catch (error: any) {
    await orchestrationLogger.logError('POSITION_CLOSE_FAILED', 'Close trades orchestration failed', error);
    return results;
  }
}

/**
 * Close positions for ONLY the window's epic(s) by querying Capital.com directly
 * 
 * CRITICAL: Only closes positions for THIS window's epic(s)!
 * - Window 1 (21:00) trades TECL → only close TECL positions
 * - Window 2 (01:00) trades SOXL → only close SOXL positions
 * - Other epics are left untouched (they belong to other windows)
 */
async function closeAccountPositionsFromCapital(
  account: any,
  windowCloseTime: string,
  decision: BrainDecision | undefined,
  isSimulation: boolean,
  windowEpics?: string[]  // Epics for this specific window
): Promise<CloseResult> {
  const modeLabel = isSimulation ? '[SIMULATION] ' : '';
  
  const result: CloseResult = {
    accountId: account.id,
    windowCloseTime,
    tradesClosedCount: 0,
    pendingOrdersCancelled: 0,
    rebalanceExecuted: false,
    bidAskFetched: new Map(),
    freshBalance: null,
    errors: [],
  };
  
  // Determine which epics to close for THIS window
  // Priority: windowEpics param > decision.epic > all (fallback)
  const epicsToClose: Set<string> = new Set();
  if (windowEpics && windowEpics.length > 0) {
    windowEpics.forEach(e => epicsToClose.add(e));
  } else if (decision?.epic) {
    epicsToClose.add(decision.epic);
  }
  
  if (epicsToClose.size === 0) {
    await orchestrationLogger.warn('POSITION_CLOSE_REQUESTED', 
      `${modeLabel}Account ${account.id}: No epics specified for window ${windowCloseTime} - skipping close`
    );
    return result;
  }
  
  await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
    `${modeLabel}Account ${account.id}: Closing positions for epics [${Array.from(epicsToClose).join(', ')}] only`
  );
  
  try {
    const client = connectionManager.getClient(account.accountType as 'demo' | 'live');
    if (!client) {
      result.errors.push(`No ${account.accountType} client available`);
      return result;
    }
    
    // Switch to this account
    const capitalAccountId = account.accountId || account.capitalAccountId;
    await client.switchAccount(capitalAccountId);
    
    // Query Capital.com for ALL open positions on this account
    let capitalPositions: any[] = [];
    try {
      capitalPositions = await client.getPositions();
    } catch (posError: any) {
      result.errors.push(`Failed to query positions: ${posError.message}`);
      return result;
    }
    
    await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
      `${modeLabel}Account ${account.accountName || account.id}: Found ${capitalPositions?.length || 0} total positions on Capital.com`
    );
    
    // Filter to only positions for THIS window's epic(s)
    if (capitalPositions && capitalPositions.length > 0) {
      for (const position of capitalPositions) {
        const dealId = position.position?.dealId || position.dealId;
        const epic = position.market?.epic || position.epic;
        const size = position.position?.size || position.size;
        
        if (!dealId) {
          await orchestrationLogger.warn('POSITION_CLOSE_REQUESTED', 
            `${modeLabel}Position missing dealId, skipping`
          );
          continue;
        }
        
        // CRITICAL: Only close positions for THIS window's epic(s)
        if (!epicsToClose.has(epic)) {
          await orchestrationLogger.debug('POSITION_CLOSE_REQUESTED', 
            `${modeLabel}Position ${dealId} (${epic}) is NOT for this window's epics [${Array.from(epicsToClose).join(', ')}] - SKIPPING`
          );
          continue;
        }
        
        // =======================================================================
        // HMH (Hold Means Hold) - UPDATE STOP LOSS INSTEAD OF CLOSING
        // If HMH is enabled for this DNA and brain says HOLD, don't close.
        // Instead, calculate and set a new fixed stop loss based on current price.
        // =======================================================================
        if (decision?.hmhEnabled && decision?.decision === 'hold') {
          try {
            // Get current market price
            const marketInfo = await client.getMarketInfo(epic);
            const currentBid = marketInfo?.snapshot?.bid || marketInfo?.bid;
            const currentAsk = marketInfo?.snapshot?.offer || marketInfo?.offer || marketInfo?.ask;
            const currentMidPrice = (currentBid + currentAsk) / 2;
            
            // Calculate new stop loss based on current price
            // stopLoss = the SL percentage from DNA config (e.g., 2%)
            // hmhStopLossOffset = 0 (original), -1 (orig-1%), -2 (orig-2%)
            const baseSLPercent = decision.stopLoss || 2;
            const effectiveSLPercent = baseSLPercent + (decision.hmhStopLossOffset || 0);
            
            // For long positions: new SL = current price * (1 - SL%)
            // For short positions: new SL = current price * (1 + SL%)
            const positionDirection = position.position?.direction || position.direction;
            let newStopLossPrice: number;
            
            if (positionDirection === 'BUY' || positionDirection === 'LONG') {
              newStopLossPrice = currentMidPrice * (1 - effectiveSLPercent / 100);
            } else {
              newStopLossPrice = currentMidPrice * (1 + effectiveSLPercent / 100);
            }
            
            // Round to reasonable precision
            newStopLossPrice = Math.round(newStopLossPrice * 100) / 100;
            
            await orchestrationLogger.info('HMH_STOP_LOSS_UPDATE', 
              `${modeLabel}HMH enabled for ${epic}: HOLD decision - updating SL instead of closing. ` +
              `DealId=${dealId}, CurrentPrice=${currentMidPrice.toFixed(2)}, BaseSL=${baseSLPercent}%, ` +
              `Offset=${decision.hmhStopLossOffset || 0}%, EffectiveSL=${effectiveSLPercent}%, ` +
              `NewStopLossPrice=${newStopLossPrice.toFixed(2)}`
            );
            
            // Update the position's stop loss via Capital.com API
            const updateSuccess = await client.updatePosition(dealId, newStopLossPrice);
            
            if (updateSuccess) {
              await orchestrationLogger.info('HMH_STOP_LOSS_UPDATE', 
                `${modeLabel}✅ HMH stop loss updated for ${epic}: ${newStopLossPrice.toFixed(2)}`
              );
              
              // Update database record with HMH trailing stop info
              const db = await getDb();
              if (db) {
                try {
                  await db
                    .update(actualTrades)
                    .set({ 
                      hmhTrailingStopPrice: newStopLossPrice,
                      hmhDaysHeld: db.sql`hmhDaysHeld + 1`,
                    })
                    .where(
                      and(
                        eq(actualTrades.accountId, account.id),
                        eq(actualTrades.dealId, dealId)
                      )
                    );
                } catch (dbErr: any) {
                  await orchestrationLogger.warn('HMH_STOP_LOSS_UPDATE', 
                    `Failed to update HMH fields in DB: ${dbErr.message}`
                  );
                }
              }
            } else {
              await orchestrationLogger.error('HMH_STOP_LOSS_UPDATE', 
                `${modeLabel}❌ Failed to update HMH stop loss for ${epic} dealId=${dealId}`,
                new Error('updatePosition returned false')
              );
              result.errors.push(`Failed to update HMH stop loss for ${epic}`);
            }
          } catch (hmhError: any) {
            await orchestrationLogger.error('HMH_STOP_LOSS_UPDATE', 
              `${modeLabel}HMH stop loss update error for ${epic}: ${hmhError.message}`,
              hmhError
            );
            result.errors.push(`HMH SL update error for ${epic}: ${hmhError.message}`);
          }
          
          // Skip closing this position - HMH keeps it open
          continue;
        }
        
        try {
          await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
            `${modeLabel}Closing position for ${epic}: dealId=${dealId}, size=${size}`
          );
          
          // Close the position
          const closeSuccess = await apiQueue.enqueue({
            fn: () => client.closePosition(dealId),
            priority: 'critical',
            isCritical: true,
            description: `${modeLabel}Close ${epic} dealId=${dealId} account=${account.id}`,
          });
          
          if (closeSuccess) {
            result.tradesClosedCount++;
            
            // Best-effort: Update database if we have a matching record
            try {
              const db = await getDb();
              if (db) {
                await db
                  .update(actualTrades)
                  .set({
                    closedAt: new Date(),
                    status: 'closed',
                    closeReason: 'window_close',
                  })
                  .where(
                    and(
                      eq(actualTrades.accountId, account.id),
                      eq(actualTrades.dealId, dealId),
                      eq(actualTrades.status, 'open')
                    )
                  );
              }
            } catch (dbErr: any) {
              // Non-fatal - position is closed regardless
              console.warn(`[CloseOrchestrator] DB update warning: ${dbErr.message}`);
            }
            
            await orchestrationLogger.info('POSITION_CLOSE_SUCCESS', 
              `${modeLabel}✅ Closed position ${dealId} (${epic})`
            );
          } else {
            result.errors.push(`Failed to close ${dealId}`);
          }
        } catch (closeErr: any) {
          result.errors.push(`Error closing ${dealId}: ${closeErr.message}`);
        }
      }
    }
    
    // Fetch fresh balance after closing
    try {
      const accountInfo = await client.getAccounts();
      const thisAccount = accountInfo.find((a: any) => a.accountId === capitalAccountId);
      if (thisAccount) {
        result.freshBalance = thisAccount.balance?.available || thisAccount.balance?.balance || null;
      }
    } catch (balErr: any) {
      // Non-fatal
    }
    
    // Fetch bid/ask for epic if BUY decision (needed for position sizing)
    if (decision?.decision === 'buy' && decision.epic) {
      try {
        const marketInfo = await client.getMarketInfo(decision.epic);
        if (marketInfo) {
          result.bidAskFetched.set(decision.epic, {
            bid: marketInfo.bid,
            ask: marketInfo.ask,
            minSize: marketInfo.minDealSize,
            minSizeIncrement: marketInfo.minSizeIncrement || 1,
          });
        }
      } catch (marketErr: any) {
        result.errors.push(`Failed to get market info for ${decision.epic}: ${marketErr.message}`);
      }
    }
    
    return result;
    
  } catch (error: any) {
    result.errors.push(`Account processing error: ${error.message}`);
    return result;
  }
}

/**
 * @deprecated Use closeAccountPositionsFromCapital instead (queries Capital.com directly)
 * 
 * OLD: Close all trades for a single account (database-based)
 * KEPT FOR ROLLBACK - do not delete
 */
async function closeAccountTrades(
  accountId: number, 
  windowCloseTime: string,
  accountTrades: any[],
  brainDecision: BrainDecision | undefined,
  isSimulation: boolean
): Promise<CloseResult> {
  const result: CloseResult = {
    accountId,
    windowCloseTime,
    tradesClosedCount: 0,
    pendingOrdersCancelled: 0,
    rebalanceExecuted: false,
    bidAskFetched: new Map(),
    freshBalance: null,
    errors: [],
  };
  
  try {
    await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
      `Processing account ${accountId}: ${accountTrades.length} trades to close`,
      { accountId }
    );
    
    // 1. Get account details
    const db = await getDb();
    if (!db) {
      result.errors.push('Database not available');
      return result;
    }
    
    const accountRecords = await db
      .select()
      .from(accounts)
      .where(eq(accounts.id, accountId))
      .limit(1);
    
    if (accountRecords.length === 0) {
      result.errors.push(`Account ${accountId} not found`);
      return result;
    }
    
    const account = accountRecords[0];
    
    // 2. Get API client from connection manager (reuses authenticated session)
    const client = connectionManager.getClient(account.accountType as 'demo' | 'live');
    
    if (!client) {
      result.errors.push(`No ${account.accountType} API client available for account ${accountId}`);
      return result;
    }
    
    // 2b+3. Switch + fetch positions INSIDE the API queue as a single atomic unit.
    // IMPORTANT: The Capital.com client is shared per environment (demo/live), so switching must not interleave.
    const capitalAccountId = account.accountId; // Capital.com account ID string

    await orchestrationLogger.info('POSITION_CLOSE_REQUESTED',
      `Queueing switch+positions for Capital.com account ${capitalAccountId} (our account ${accountId})`,
      { accountId, capitalAccountId }
    );

    const positions = await new Promise<any[] | null>((resolve) => {
      apiQueue.enqueue({
        priority: 'critical',
        isCritical: true,
        environment: account.accountType as 'demo' | 'live',
        operation: 'close',
        description: `CLOSE_PREP switch+positions accountId=${accountId} env=${account.accountType}`,
        fn: async () => {
          const switchSuccess = await client.switchAccount(capitalAccountId);
          if (!switchSuccess) {
            throw new Error(`Failed to switch to Capital.com account ${capitalAccountId}`);
          }

          // Small delay after switch to ensure Capital.com registers the switch
          await new Promise(r => setTimeout(r, 100));

          return await client.getPositions();
        },
        maxRetries: 3,
        onSuccess: (res) => resolve(res as any[]),
        onError: () => resolve(null),
      });
    });

    if (!positions) {
      result.errors.push(`Failed to fetch positions for account ${accountId}`);
      await orchestrationLogger.warn('POSITION_CLOSE_FAILED',
        `Failed to fetch positions (switch+positions) for account ${accountId}`,
        { accountId, capitalAccountId }
      );
      return result;
    }
    
    // Log detailed position info for debugging
    console.log(`[CloseOrchestrator] Account ${accountId}: Found ${positions?.length || 0} positions on Capital.com`);
    if (positions && positions.length > 0) {
      for (const pos of positions) {
        console.log(`[CloseOrchestrator]   - ${pos.epic}: size=${pos.size}, dealId=${pos.dealId}, level=${pos.level}`);
      }
    }
    
    await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
      `Fetched ${positions?.length || 0} positions from Capital.com`,
      { accountId, capitalAccountId, data: { positionCount: positions?.length || 0, positions: positions?.map(p => ({ epic: p.epic, size: p.size, dealId: p.dealId })) } }
    );
    
    // Update fund tracker with current position values
    if (positions && positions.length > 0) {
      for (const pos of positions) {
        // Try to match position to a window based on our actual_trades
        const matchingTrade = accountTrades.find(t => t.dealId === pos.dealId);
        if (matchingTrade) {
          const positionValue = pos.size * pos.level; // Approximate position value
          fundTracker.updateWindowPosition(
            accountId,
            matchingTrade.windowCloseTime,
            positionValue,
            pos.dealId
          );
        }
      }
    }
    
    // 4. Build set of epics to close (from trades + brain decision)
    // OPTIMIZATION: Removed getWorkingOrders call - we don't use pending orders
    // If we ever start using limit orders, add the cancelAllOrdersForEpic back
    const epicsToClose = new Set(accountTrades.map(t => t.epic));
    
    // Also include the epic from brain decision (if we're about to trade on it)
    if (brainDecision?.epic) {
      epicsToClose.add(brainDecision.epic);
    }
    
    // 5. Close ALL positions for epics we're about to trade (not just from our database)
    // This handles: manual trades, rejected trades, unreconciled trades from previous simulations
    // Without this, RISK_CHECK errors occur because Capital.com has existing positions
    await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
      `Checking for ANY positions to close on epics: ${[...epicsToClose].join(', ')}`,
      { accountId, epics: [...epicsToClose] }
    );
    
    for (const epic of epicsToClose) {
      const positionForEpic = positions?.find(p => p.epic === epic);
      
      if (positionForEpic) {
        // SAFETY CHECK: Do NOT close positions opened in the last 60 seconds
        // This prevents the race condition where we open a position and then immediately close it
        const positionCreatedDate = positionForEpic.createdDateUTC || positionForEpic.createdDate;
        if (positionCreatedDate) {
          const createdAt = new Date(positionCreatedDate);
          const ageSeconds = (Date.now() - createdAt.getTime()) / 1000;
          
          if (ageSeconds < 60) {
            await orchestrationLogger.warn('POSITION_CLOSE_SKIPPED', 
              `⚠️ SKIPPING close for ${epic} - position is only ${ageSeconds.toFixed(1)}s old (dealId: ${positionForEpic.dealId})`,
              { accountId, epic, data: { dealId: positionForEpic.dealId, ageSeconds, createdAt: createdAt.toISOString() } }
            );
            continue; // Skip this position - it was just opened in the current window
          }
        }
        
        await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
          `Found existing position for ${epic} (dealId: ${positionForEpic.dealId}, size: ${positionForEpic.size}) - CLOSING`,
          { accountId, epic, data: { dealId: positionForEpic.dealId, size: positionForEpic.size, level: positionForEpic.level } }
        );
        
        // Check if this position is tracked in our database
        const matchingDbTrade = accountTrades.find(t => t.epic === epic);
        
        try {
          // Close via apiQueue (critical) - awaits completion before returning
          const closeSuccess = await new Promise<boolean>((resolve) => {
            apiQueue.enqueue({
              priority: 'critical',
              isCritical: true,
              environment: account.accountType as 'demo' | 'live',
              operation: 'close',
              description: `CLOSE ${epic} dealId=${positionForEpic.dealId} accountId=${accountId} env=${account.accountType}`,
              fn: async () => {
                // Switch inside callback to ensure correct sub-account at execution time.
                const switched = await client.switchAccount(capitalAccountId);
                if (!switched) {
                  throw new Error(`Failed to switch to Capital.com account ${capitalAccountId}`);
                }
                return await client.closePosition(positionForEpic.dealId);
              },
              maxRetries: 3,
              onSuccess: (res) => resolve(Boolean(res)),
              onError: () => resolve(false),
            });
          });
          
          if (closeSuccess) {
            result.tradesClosedCount++;
            await orchestrationLogger.info('POSITION_CLOSE_SUCCESS', 
              `Successfully closed position for ${epic} (dealId: ${positionForEpic.dealId})`,
              { accountId, epic }
            );
            
            // Update database if we have a matching trade record
            if (matchingDbTrade) {
              await db
                .update(actualTrades)
                .set({ 
                  status: 'closed', 
                  closedAt: new Date(), 
                  closeReason: 'window_close',
                  exitPrice: positionForEpic.level,
                  // Update dealId if it was wrong (order ID vs position ID mismatch)
                  dealId: positionForEpic.dealId,
                })
                .where(eq(actualTrades.id, matchingDbTrade.id));
            } else {
              await orchestrationLogger.warn('POSITION_CLOSE_SUCCESS', 
                `Closed position for ${epic} that was NOT in our database (manual trade or untracked)`,
                { accountId, epic, data: { positionDealId: positionForEpic.dealId } }
              );
            }
          } else {
            result.errors.push(`Failed to close ${epic} position ${positionForEpic.dealId}`);
          }
        } catch (closeError: any) {
          await orchestrationLogger.warn('POSITION_CLOSE_FAILED', 
            `Error closing position for ${epic}: ${closeError.message}`,
            { accountId, epic, data: { dealId: positionForEpic.dealId, error: closeError.message } }
          );
          result.errors.push(`Close ${epic}: ${closeError.message}`);
        }
      } else {
        await orchestrationLogger.debug('POSITION_CLOSE_REQUESTED', 
          `No position found for ${epic} - nothing to close`,
          { accountId, epic }
        );
        
        // Mark any database trade as closed since there's nothing on Capital.com
        const matchingDbTrade = accountTrades.find(t => t.epic === epic);
        if (matchingDbTrade && matchingDbTrade.status === 'open') {
          await db
            .update(actualTrades)
            .set({ status: 'closed', closedAt: new Date(), closeReason: 'manual' })
            .where(eq(actualTrades.id, matchingDbTrade.id));
        }
      }
    }
    
    // LEGACY: The old per-trade close logic (kept for backwards compatibility with exact dealId matches)
    // This handles edge cases where a trade is in our DB but wasn't caught by epic-based closing above
    for (const trade of accountTrades) {
      // Skip if we already closed this epic above
      if (epicsToClose.has(trade.epic)) continue;
      
      try {
        const positionByEpic = positions?.find(p => p.epic === trade.epic);
        
        if (!positionByEpic) {
          await db
            .update(actualTrades)
            .set({ status: 'closed', closedAt: new Date(), closeReason: 'manual' })
            .where(eq(actualTrades.id, trade.id));
          continue;
        }
        
        const dealIdToClose = positionByEpic.dealId;
        const dealIdMismatch = dealIdToClose !== trade.dealId;
        
        if (dealIdMismatch) {
          await orchestrationLogger.warn('POSITION_CLOSE_REQUESTED', 
            `Trade ${trade.id} dealId mismatch - stored: ${trade.dealId}, actual position: ${dealIdToClose}`,
            { accountId, epic: trade.epic, data: { storedDealId: trade.dealId, actualDealId: dealIdToClose } }
          );
        }
        
        await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
          `Closing position ${dealIdToClose} for ${trade.epic} (size: ${positionByEpic.size})`,
          { accountId, epic: trade.epic, data: { positionSize: positionByEpic.size, positionLevel: positionByEpic.level } }
        );
        
        // Use apiQueue for rate limiting with critical priority
        const closeSuccess = await new Promise<boolean>((resolve) => {
          apiQueue.enqueue({
            priority: 'critical',
            isCritical: true, // Bypass quiet period
            environment: account.accountType as 'demo' | 'live',
            operation: 'close',
            description: `CLOSE_LEGACY ${trade.epic} dealId=${dealIdToClose} accountId=${accountId} env=${account.accountType}`,
            fn: async () => {
              const success = await client.closePosition(dealIdToClose);
              return success;
            },
            maxRetries: 3,
            onSuccess: (success) => resolve(success as boolean),
            onError: () => resolve(false),
          });
        });
        
        if (closeSuccess) {
          // Update actual_trades with close details and correct dealId
          const updateData: any = {
            closedAt: new Date(),
            status: 'closed',
            dealId: dealIdToClose,  // Always update to the correct position dealId
            // exitPrice and pnl will be updated by reconciliation
          };
          
          await db
            .update(actualTrades)
            .set(updateData)
            .where(eq(actualTrades.id, trade.id));
          
          result.tradesClosedCount++;
          
          await orchestrationLogger.info('POSITION_CLOSE_SUCCESS', 
            `Closed position ${dealIdToClose} for ${trade.epic}${dealIdMismatch ? ' (fixed dealId in DB)' : ''}`,
            { accountId, epic: trade.epic }
          );
        } else {
          result.errors.push(`Failed to close position ${dealIdToClose} for ${trade.epic}`);
          await orchestrationLogger.warn('POSITION_CLOSE_FAILED', 
            `Failed to close position ${dealIdToClose} for ${trade.epic}`,
            { accountId, epic: trade.epic }
          );
        }
        
      } catch (error: any) {
        result.errors.push(`Error closing ${trade.epic}: ${error.message}`);
        await orchestrationLogger.logError('POSITION_CLOSE_FAILED', 
          `Failed to close position for ${trade.epic}`,
          error,
          { accountId, epic: trade.epic }
        );
      }
    }
    
    // 6. Execute rebalancing if needed
    if (brainDecision?.rebalanceNeeded && brainDecision.decision === 'buy') {
      const rebalanceResult = fundTracker.calculateRebalanceNeeded(
        accountId,
        windowCloseTime
      );
      
      if (rebalanceResult.needed && rebalanceResult.fromDealId) {
        await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
          `Executing rebalance: closing ${rebalanceResult.excessPct.toFixed(1)}% from ${rebalanceResult.fromWindow}`,
          { accountId }
        );
        
        // Find the position to partially close
        const posToRebalance = positions?.find(p => p.dealId === rebalanceResult.fromDealId);
        
        if (posToRebalance) {
          const partialCloseResult = await client.partialClosePosition(
            rebalanceResult.fromDealId,
            rebalanceResult.contractsToClose,
            {
              epic: posToRebalance.epic,
              direction: posToRebalance.direction as 'BUY' | 'SELL',
              size: posToRebalance.size,
            }
          );
          
          if (partialCloseResult.success) {
            fundTracker.markRebalanceComplete(
              accountId,
              rebalanceResult.fromWindow!,
              rebalanceResult.excessValue
            );
            result.rebalanceExecuted = true;
            
            await orchestrationLogger.info('POSITION_CLOSE_SUCCESS', 
              `Rebalance complete: closed ${partialCloseResult.closedSize} contracts`,
              { accountId }
            );
          } else {
            result.errors.push(`Rebalance failed for ${rebalanceResult.fromDealId}`);
          }
        }
      }
    }
    
    // OPTIMIZATION: Removed per-account getAccounts call here
    // Fresh balances are fetched ONCE for all accounts at T-15s in open_orchestrator
    // This saves 5 API calls (one per account) = ~1.1 seconds
    
    // OPTIMIZATION: Removed per-account getMarketInfo call here
    // Bid/ask is fetched ONCE per epic at T-15s in open_orchestrator
    // This saves 3-5 API calls = ~0.7-1.1 seconds
    
    await orchestrationLogger.info('POSITION_CLOSE_SUCCESS', 
      `Close sequence complete: ${result.tradesClosedCount} trades closed, ${result.rebalanceExecuted ? 'rebalance executed' : '0 rebalances executed'}`,
      { accountId }
    );
    
    return result;
    
  } catch (error: any) {
    result.errors.push(`Account processing failed: ${error.message}`);
    await orchestrationLogger.logError('POSITION_CLOSE_FAILED', 
      `Failed to close trades for account ${accountId}`,
      error,
      { accountId }
    );
    return result;
  }
}

/**
 * Get unique epics from close results that need trades
 */
export function getEpicsWithBidAsk(results: CloseResult[]): Map<string, { bid: number; ask: number; minSize: number; minSizeIncrement: number }> {
  const combined = new Map<string, { bid: number; ask: number; minSize: number; minSizeIncrement: number }>();
  
  for (const result of results) {
    for (const [epic, data] of result.bidAskFetched) {
      combined.set(epic, data);
    }
  }
  
  return combined;
}
