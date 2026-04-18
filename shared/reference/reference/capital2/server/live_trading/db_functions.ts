/**
 * Database functions for live trading bot management
 */

import { getDb } from '../db';
import { 
  accounts, 
  actualTrades, 
  validatedTrades, 
  tradeComparisons,
  botPerformance, 
  strategyCombos, 
  savedStrategies, 
  comboResults, 
  backtestResults,
  strategyAssignmentHistory 
} from '../../drizzle/schema';
import { eq, and, desc, sql, inArray } from 'drizzle-orm';
import type { 
  Account, InsertAccount, 
  ActualTrade, InsertActualTrade,
  ValidatedTrade, InsertValidatedTrade,
  TradeComparison, InsertTradeComparison,
  BotPerformance, InsertBotPerformance 
} from '../../drizzle/schema';
import { orchestrationLogger } from '../orchestration/logger';

/**
 * Account Management
 */

export async function getAccountsByUser(userId: number): Promise<Account[]> {
  const db = await getDb();
  if (!db) return [];

  try {
    return await db.select().from(accounts).where(eq(accounts.userId, userId));
  } catch (error) {
    console.error('[LiveTrading] Error fetching accounts:', error);
    return [];
  }
}

export async function getAccountById(accountId: number): Promise<Account | null> {
  const db = await getDb();
  if (!db) return null;

  try {
    const results = await db.select().from(accounts).where(eq(accounts.id, accountId)).limit(1);
    return results[0] || null;
  } catch (error) {
    console.error('[LiveTrading] Error fetching account:', error);
    return null;
  }
}

export async function getAccountByCapitalId(capitalAccountId: string): Promise<Account | null> {
  const db = await getDb();
  if (!db) return null;

  try {
    const results = await db.select().from(accounts).where(eq(accounts.accountId, capitalAccountId)).limit(1);
    return results[0] || null;
  } catch (error) {
    console.error('[LiveTrading] Error fetching account by Capital ID:', error);
    return null;
  }
}

export async function createOrUpdateAccount(data: InsertAccount): Promise<Account | null> {
  const db = await getDb();
  if (!db) return null;

  try {
    // Check if account exists
    const existing = await getAccountByCapitalId(data.accountId);

    if (existing) {
      // Update existing account
      await db.update(accounts)
        .set({
          ...data,
          updatedAt: new Date(),
        })
        .where(eq(accounts.id, existing.id));
      
      return await getAccountById(existing.id);
    } else {
      // Create new account
      const result = await db.insert(accounts).values(data);
      const insertId = Number(result[0].insertId);
      return await getAccountById(insertId);
    }
  } catch (error) {
    console.error('[LiveTrading] Error creating/updating account:', error);
    return null;
  }
}

export async function syncAccountState(
  accountId: number,
  state: {
    balance: number;
    equity: number;
    margin: number;
    available: number;
    profitLoss: number;
  }
): Promise<void> {
  const db = await getDb();
  if (!db) return;

  try {
    await db.update(accounts)
      .set({
        ...state,
        lastSync: new Date(),
        updatedAt: new Date(),
      })
      .where(eq(accounts.id, accountId));
  } catch (error) {
    console.error('[LiveTrading] Error syncing account state:', error);
  }
}

export async function assignBotToAccount(
  accountId: number,
  strategyId: number,
  config: {
    epic?: string;
    leverage?: number;
    stopLoss?: number;
    investmentPct?: number;
    windowConfig?: any; // User-provided window configuration
  }
): Promise<boolean> {
  console.log(`[AssignBotDB] === assignBotToAccount START ===`);
  console.log(`[AssignBotDB] accountId: ${accountId}, strategyId: ${strategyId}`);
  console.log(`[AssignBotDB] config:`, JSON.stringify(config, null, 2));
  
  const db = await getDb();
  if (!db) {
    console.log(`[AssignBotDB] ERROR: Database not available`);
    return false;
  }
  console.log(`[AssignBotDB] Database connection OK`);

  try {
    // First, check if the account exists
    const existingAccount = await db.select().from(accounts).where(eq(accounts.id, accountId)).limit(1);
    console.log(`[AssignBotDB] Account lookup result:`, existingAccount.length > 0 ? `Found account: ${existingAccount[0].accountName}` : 'NOT FOUND');
    
    if (existingAccount.length === 0) {
      console.log(`[AssignBotDB] ERROR: Account with id ${accountId} does not exist in database!`);
      return false;
    }

    // ========================================================================
    // PHASE 3: DYNAMIC WINDOW CONFIG - No longer store stale close times
    // ========================================================================
    // 
    // CHANGE: We now ALWAYS resolve window close times dynamically from the
    //         strategy at runtime (in loadStrategyDNAStrands). We no longer
    //         need to copy windowConfig to the account.
    // 
    // However, we still accept windowConfig for backwards compatibility
    // and for cases where the user wants to override the strategy windows.
    // The key change is that close times are now DYNAMICALLY resolved.
    //
    // ROLLBACK: To revert, remove the dynamic resolution and store windowConfig
    //           directly like before.
    // ========================================================================
    
    let windowConfig = config.windowConfig;
    
    if (!windowConfig) {
      // PHASE 3: Dynamically resolve windows from strategy with CURRENT close times
      console.log(`[AssignBotDB] No windowConfig provided, resolving dynamically from strategy...`);
      
      try {
        // Import the window resolver
        const { resolveWindowCloseTime } = await import('../services/window_resolver');
        
        // Get strategy to extract window structure
        const strategy = await db.select().from(savedStrategies).where(eq(savedStrategies.id, strategyId)).limit(1);
        
        if (strategy.length > 0 && strategy[0].windowConfig) {
          const strategyWindowConfig = strategy[0].windowConfig as any;
          const dnaStrands = strategy[0].dnaStrands as any[] || [];
          
          // Build windowConfig with DYNAMICALLY resolved close times
          const resolvedWindows = await Promise.all(
            (strategyWindowConfig.windows || []).map(async (window: any) => {
              // Get epics for this window
              const windowDnaIds = window.dnaStrandIds || [];
              const windowEpics = dnaStrands
                .filter(strand => windowDnaIds.includes(strand.id))
                .map(strand => strand.epic)
                .filter(Boolean);
              
              const uniqueEpics = [...new Set(windowEpics)] as string[];
              
              // DYNAMICALLY resolve close time
              const resolvedCloseTime = uniqueEpics.length > 0
                ? await resolveWindowCloseTime(uniqueEpics)
                : window.closeTime || '21:00:00';
              
              return {
                ...window,
                closeTime: resolvedCloseTime,  // DYNAMIC close time!
              };
            })
          );
          
          windowConfig = {
            ...strategyWindowConfig,
            windows: resolvedWindows,
          };
          
          console.log(`[AssignBotDB] Resolved ${resolvedWindows.length} windows with DYNAMIC close times for strategy ${strategyId}`);
        }
      } catch (error: any) {
        console.warn(`[AssignBotDB] Failed to resolve windows for strategy ${strategyId}:`, error.message);
        // Continue without window config - will be resolved at runtime
      }
    } else {
      // User provided windowConfig - resolve close times dynamically anyway
      console.log(`[AssignBotDB] User provided windowConfig, resolving close times dynamically...`);
      
      try {
        const { resolveWindowCloseTime } = await import('../services/window_resolver');
        
        // Get strategy for DNA strand info
        const strategy = await db.select().from(savedStrategies).where(eq(savedStrategies.id, strategyId)).limit(1);
        const dnaStrands = strategy[0]?.dnaStrands as any[] || [];
        
        const resolvedWindows = await Promise.all(
          (windowConfig.windows || []).map(async (window: any) => {
            const windowEpics = (window.epics || []) as string[];
            
            // If no epics in window directly, try to get from DNA strands
            let epicsToResolve = windowEpics;
            if (epicsToResolve.length === 0 && window.dnaStrandIds) {
              epicsToResolve = dnaStrands
                .filter(strand => window.dnaStrandIds.includes(strand.id))
                .map(strand => strand.epic)
                .filter(Boolean);
            }
            
            const uniqueEpics = [...new Set(epicsToResolve)] as string[];
            
            // DYNAMICALLY resolve close time
            const resolvedCloseTime = uniqueEpics.length > 0
              ? await resolveWindowCloseTime(uniqueEpics)
              : window.closeTime || '21:00:00';
            
            return {
              ...window,
              closeTime: resolvedCloseTime,
            };
          })
        );
        
        windowConfig = {
          ...windowConfig,
          windows: resolvedWindows,
        };
      } catch (error: any) {
        console.warn(`[AssignBotDB] Failed to resolve provided windowConfig:`, error.message);
        // Keep original windowConfig
      }
    }

    // Extract windowConfig from config before spreading
    const { windowConfig: _, ...otherConfig } = config;

    // Get previous strategy ID for history tracking
    const previousStrategyId = existingAccount[0].assignedStrategyId;
    
    console.log(`[AssignBotDB] Executing UPDATE query...`);
    await db.update(accounts)
      .set({
        assignedStrategyId: strategyId,
        botStatus: 'stopped',
        windowConfig: windowConfig as any, // Store window configuration with DYNAMIC times
        ...otherConfig,
        updatedAt: new Date(),
      })
      .where(eq(accounts.id, accountId));
    
    console.log(`[AssignBotDB] UPDATE successful`);
    
    // Record strategy assignment history
    try {
      const strategy = await db.select().from(savedStrategies).where(eq(savedStrategies.id, strategyId)).limit(1);
      if (strategy.length > 0) {
        const dnaSnapshot = strategy[0].dnaStrands || [];
        const windowConfigSnapshot = strategy[0].windowConfig || null;
        
        const action = previousStrategyId ? 'edited' : 'assigned';
        
        await db.insert(strategyAssignmentHistory).values({
          accountId,
          strategyId,
          action,
          previousStrategyId: previousStrategyId || null,
          dnaSnapshot: dnaSnapshot as any,
          windowConfigSnapshot: windowConfigSnapshot as any,
          notes: action === 'edited' 
            ? `Changed from strategy ID ${previousStrategyId} to ${strategyId}`
            : `Assigned strategy: ${strategy[0].name}`,
        });
        
        console.log(`[AssignBotDB] Recorded strategy assignment history: ${action} strategy ${strategyId} to account ${accountId}`);
      }
    } catch (historyError) {
      console.warn(`[AssignBotDB] Failed to record assignment history:`, historyError);
      // Don't fail the main operation if history fails
    }
    
    return true;
  } catch (error) {
    console.error('[AssignBotDB] EXCEPTION:', error);
    return false;
  }
}

export async function removeBotFromAccount(accountId: number): Promise<boolean> {
  const db = await getDb();
  if (!db) return false;

  try {
    // Get current strategy before removing for history tracking
    const currentAccount = await db.select({
      assignedStrategyId: accounts.assignedStrategyId,
    }).from(accounts).where(eq(accounts.id, accountId)).limit(1);
    
    const previousStrategyId = currentAccount[0]?.assignedStrategyId;
    
    // When removing a strategy, always deactivate the account
    // An account cannot be active without a strategy
    await db.update(accounts)
      .set({
        assignedStrategyId: null,
        botStatus: 'stopped',
        isActive: false, // Must deactivate when removing strategy
        startedAt: null,
        stoppedAt: new Date(),
        updatedAt: new Date(),
      })
      .where(eq(accounts.id, accountId));
    
    console.log(`[LiveTrading] Removed bot from account ${accountId} and deactivated`);
    
    // Record removal in strategy assignment history
    if (previousStrategyId) {
      try {
        const strategy = await db.select().from(savedStrategies).where(eq(savedStrategies.id, previousStrategyId)).limit(1);
        if (strategy.length > 0) {
          await db.insert(strategyAssignmentHistory).values({
            accountId,
            strategyId: previousStrategyId,
            action: 'removed',
            previousStrategyId: null,
            dnaSnapshot: strategy[0].dnaStrands as any || [],
            windowConfigSnapshot: strategy[0].windowConfig as any || null,
            notes: `Removed strategy: ${strategy[0].name}`,
          });
          
          console.log(`[LiveTrading] Recorded strategy removal history for account ${accountId}`);
        }
      } catch (historyError) {
        console.warn(`[LiveTrading] Failed to record removal history:`, historyError);
      }
    }
    
    return true;
  } catch (error) {
    console.error('[LiveTrading] Error removing bot from account:', error);
    return false;
  }
}

export async function updateBotStatus(
  accountId: number,
  status: 'stopped' | 'running' | 'paused' | 'error'
): Promise<boolean> {
  const db = await getDb();
  if (!db) return false;

  try {
    const updates: any = {
      botStatus: status,
      updatedAt: new Date(),
    };

    if (status === 'running') {
      updates.startedAt = new Date();
      updates.pausedAt = null;
      updates.lastHeartbeat = new Date();
    } else if (status === 'paused') {
      updates.pausedAt = new Date();
    } else if (status === 'stopped') {
      updates.stoppedAt = new Date();
      updates.pausedAt = null;
    }

    await db.update(accounts)
      .set(updates)
      .where(eq(accounts.id, accountId));
    
    // Log bot status change
    const statusMessages = {
      running: 'Bot started',
      stopped: 'Bot stopped',
      paused: 'Bot paused',
      error: 'Bot encountered error',
    };
    
    await orchestrationLogger.info(
      status === 'running' ? 'BOT_STARTED' : 
      status === 'stopped' ? 'BOT_STOPPED' :
      status === 'paused' ? 'BOT_PAUSED' : 'BOT_ERROR',
      statusMessages[status],
      {
        accountId,
        data: { status, accountId },
      }
    );
    
    return true;
  } catch (error) {
    console.error('[LiveTrading] Error updating bot status:', error);
    return false;
  }
}

export async function updateBotHeartbeat(accountId: number): Promise<void> {
  const db = await getDb();
  if (!db) return;

  try {
    await db.update(accounts)
      .set({
        lastHeartbeat: new Date(),
      })
      .where(eq(accounts.id, accountId));
  } catch (error) {
    console.error('[LiveTrading] Error updating bot heartbeat:', error);
  }
}

/**
 * Actual Trade Management (NEW - replaces liveTrades)
 * 
 * These functions manage the actual_trades table which stores all live/simulated
 * trades made by the system via Capital.com.
 */

/**
 * Create a new actual trade record
 * Called when brain decides to BUY - creates pending trade before execution
 */
export async function createActualTrade(trade: InsertActualTrade): Promise<ActualTrade | null> {
  const db = await getDb();
  if (!db) return null;

  try {
    const result = await db.insert(actualTrades).values(trade);
    const insertId = Number(result[0].insertId);
    
    const trades = await db.select().from(actualTrades).where(eq(actualTrades.id, insertId)).limit(1);
    return trades[0] || null;
  } catch (error) {
    console.error('[LiveTrading] Error creating actual trade:', error);
    return null;
  }
}

/**
 * Update an actual trade
 */
export async function updateActualTrade(tradeId: number, updates: Partial<ActualTrade>): Promise<boolean> {
  const db = await getDb();
  if (!db) return false;

  try {
    await db.update(actualTrades)
      .set({
        ...updates,
        updatedAt: new Date(),
      })
      .where(eq(actualTrades.id, tradeId));
    
    return true;
  } catch (error) {
    console.error('[LiveTrading] Error updating actual trade:', error);
    return false;
  }
}

/**
 * Close an actual trade
 */
export async function closeActualTrade(
  tradeId: number,
  exitPrice: number,
  closeReason: 'window_close' | 'stop_loss' | 'manual' | 'rebalance',
  pnlData: { grossPnl: number; spreadCost: number; overnightCost: number; netPnl: number }
): Promise<boolean> {
  const db = await getDb();
  if (!db) return false;

  try {
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
    
    return true;
  } catch (error) {
    console.error('[LiveTrading] Error closing actual trade:', error);
    return false;
  }
}

/**
 * Get open trades for an account
 */
export async function getOpenTradesByAccount(accountId: number): Promise<ActualTrade[]> {
  const db = await getDb();
  if (!db) return [];

  try {
    return await db.select()
      .from(actualTrades)
      .where(and(
        eq(actualTrades.accountId, accountId),
        eq(actualTrades.status, 'open')
      ));
  } catch (error) {
    console.error('[LiveTrading] Error fetching open trades:', error);
    return [];
  }
}

/**
 * Get open trade for a specific window
 */
export async function getOpenTradeForWindow(accountId: number, windowCloseTime: string): Promise<ActualTrade | null> {
  const db = await getDb();
  if (!db) return null;

  try {
    const results = await db.select()
      .from(actualTrades)
      .where(and(
        eq(actualTrades.accountId, accountId),
        eq(actualTrades.windowCloseTime, windowCloseTime),
        eq(actualTrades.status, 'open')
      ))
      .limit(1);
    
    return results[0] || null;
  } catch (error) {
    console.error('[LiveTrading] Error fetching open trade for window:', error);
    return null;
  }
}

/**
 * Get all trades for an account
 */
export async function getAllTradesByAccount(accountId: number, limit: number = 100): Promise<ActualTrade[]> {
  const db = await getDb();
  if (!db) return [];

  try {
    return await db.select()
      .from(actualTrades)
      .where(eq(actualTrades.accountId, accountId))
      .orderBy(desc(actualTrades.createdAt))
      .limit(limit);
  } catch (error) {
    console.error('[LiveTrading] Error fetching trades:', error);
    return [];
  }
}

/**
 * Get trade by Capital.com deal ID
 */
export async function getTradeByDealId(dealId: string): Promise<ActualTrade | null> {
  const db = await getDb();
  if (!db) return null;

  try {
    const results = await db.select().from(actualTrades).where(eq(actualTrades.dealId, dealId)).limit(1);
    return results[0] || null;
  } catch (error) {
    console.error('[LiveTrading] Error fetching trade by deal ID:', error);
    return null;
  }
}

/**
 * Mark trade as stopped out (detected via hourly polling)
 */
export async function markTradeStoppedOut(
  tradeId: number,
  exitPrice: number,
  pnlData: { grossPnl: number; netPnl: number }
): Promise<boolean> {
  const db = await getDb();
  if (!db) return false;

  try {
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
    
    return true;
  } catch (error) {
    console.error('[LiveTrading] Error marking trade as stopped out:', error);
    return false;
  }
}

/**
 * Update trade with deal ID after reconciliation (T+1 second)
 */
export async function reconcileActualTrade(
  tradeId: number,
  dealId: string,
  dealReference: string,
  actualEntryPrice: number,
  contracts: number,
  notionalValue: number,
  marginUsed: number
): Promise<boolean> {
  const db = await getDb();
  if (!db) return false;

  try {
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
    
    return true;
  } catch (error) {
    console.error('[LiveTrading] Error reconciling actual trade:', error);
    return false;
  }
}

/**
 * Get pending trades (created but not yet reconciled)
 */
export async function getPendingTrades(accountId?: number): Promise<ActualTrade[]> {
  const db = await getDb();
  if (!db) return [];

  try {
    const conditions = [eq(actualTrades.status, 'pending')];
    
    if (accountId) {
      conditions.push(eq(actualTrades.accountId, accountId));
    }

    return await db.select()
      .from(actualTrades)
      .where(and(...conditions))
      .orderBy(desc(actualTrades.createdAt));
  } catch (error) {
    console.error('[LiveTrading] Error fetching pending trades:', error);
    return [];
  }
}

// ============================================================================
// Validated Trade Management
// ============================================================================

/**
 * Create a validation record for an actual trade
 */
export async function createValidatedTrade(validation: InsertValidatedTrade): Promise<ValidatedTrade | null> {
  const db = await getDb();
  if (!db) return null;

  try {
    const result = await db.insert(validatedTrades).values(validation);
    const insertId = Number(result[0].insertId);
    
    const records = await db.select().from(validatedTrades).where(eq(validatedTrades.id, insertId)).limit(1);
    return records[0] || null;
  } catch (error) {
    console.error('[LiveTrading] Error creating validated trade:', error);
    return null;
  }
}

/**
 * Get validation for an actual trade
 */
export async function getValidationForTrade(actualTradeId: number): Promise<ValidatedTrade | null> {
  const db = await getDb();
  if (!db) return null;

  try {
    const results = await db.select()
      .from(validatedTrades)
      .where(eq(validatedTrades.actualTradeId, actualTradeId))
      .limit(1);
    
    return results[0] || null;
  } catch (error) {
    console.error('[LiveTrading] Error fetching validation for trade:', error);
    return null;
  }
}

/**
 * Update validation record
 */
export async function updateValidatedTrade(validationId: number, updates: Partial<ValidatedTrade>): Promise<boolean> {
  const db = await getDb();
  if (!db) return false;

  try {
    await db.update(validatedTrades)
      .set(updates)
      .where(eq(validatedTrades.id, validationId));
    
    return true;
  } catch (error) {
    console.error('[LiveTrading] Error updating validated trade:', error);
    return false;
  }
}

/**
 * Get trades pending validation (closed but not yet validated)
 */
export async function getPendingValidationTrades(accountId?: number): Promise<ActualTrade[]> {
  const db = await getDb();
  if (!db) return [];

  try {
    // Get closed trades
    const conditions = [eq(actualTrades.status, 'closed')];
    
    if (accountId) {
      conditions.push(eq(actualTrades.accountId, accountId));
    }

    const closedTrades = await db.select()
      .from(actualTrades)
      .where(and(...conditions))
      .orderBy(desc(actualTrades.closedAt));

    // Filter to only those without validation or with pending status
    const pendingTrades: ActualTrade[] = [];
    for (const trade of closedTrades) {
      const validation = await getValidationForTrade(trade.id);
      if (!validation || validation.validationStatus === 'pending') {
        pendingTrades.push(trade);
      }
    }

    return pendingTrades;
  } catch (error) {
    console.error('[LiveTrading] Error fetching pending validation trades:', error);
    return [];
  }
}

// ============================================================================
// Trade Comparison Management
// ============================================================================

/**
 * Create or update a trade comparison record
 */
export async function upsertTradeComparison(comparison: InsertTradeComparison): Promise<boolean> {
  const db = await getDb();
  if (!db) return false;

  try {
    // Check if comparison exists
    const existing = await db.select()
      .from(tradeComparisons)
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
    
    return true;
  } catch (error) {
    console.error('[LiveTrading] Error upserting trade comparison:', error);
    return false;
  }
}

/**
 * Get trade comparisons for an account
 */
export async function getTradeComparisons(accountId: number, limit: number = 100): Promise<TradeComparison[]> {
  const db = await getDb();
  if (!db) return [];

  try {
    return await db.select()
      .from(tradeComparisons)
      .where(eq(tradeComparisons.accountId, accountId))
      .orderBy(desc(tradeComparisons.tradeDate))
      .limit(limit);
  } catch (error) {
    console.error('[LiveTrading] Error fetching trade comparisons:', error);
    return [];
  }
}

/**
 * Bot Performance Tracking
 */

export async function recordBotPerformance(data: InsertBotPerformance): Promise<boolean> {
  const db = await getDb();
  if (!db) return false;

  try {
    await db.insert(botPerformance).values(data);
    return true;
  } catch (error) {
    console.error('[LiveTrading] Error recording bot performance:', error);
    return false;
  }
}

export async function getBotPerformanceHistory(
  accountId: number,
  limit: number = 100
): Promise<BotPerformance[]> {
  const db = await getDb();
  if (!db) return [];

  try {
    return await db.select()
      .from(botPerformance)
      .where(eq(botPerformance.accountId, accountId))
      .orderBy(desc(botPerformance.timestamp))
      .limit(limit);
  } catch (error) {
    console.error('[LiveTrading] Error fetching bot performance history:', error);
    return [];
  }
}

/**
 * Get accounts with their assigned strategies (for dashboard)
 */
export async function getAccountsWithStrategies(userId: number): Promise<any[]> {
  const db = await getDb();
  if (!db) return [];

  try {
    const userAccounts = await db.select()
      .from(accounts)
      .where(eq(accounts.userId, userId));

    // Fetch strategy details for accounts with assigned bots
    const accountsWithStrategies = await Promise.all(
      userAccounts.map(async (account) => {
        if (account.assignedStrategyId) {
          // Use savedStrategies table instead of strategyCombos
          const strategy = await db.select()
            .from(savedStrategies)
            .where(eq(savedStrategies.id, account.assignedStrategyId!))
            .limit(1);

          // Extract epics from DNA strands
          let epics: string[] = [];
          if (strategy[0] && strategy[0].dnaStrands) {
            const dnaStrands = strategy[0].dnaStrands as any[];
            const epicSet = new Set<string>();
            for (const strand of dnaStrands) {
              if (strand.epic) {
                epicSet.add(strand.epic);
              }
            }
            epics = Array.from(epicSet);
          }

          return {
            ...account,
            strategy: strategy[0] || null,
            epics, // Add epics array
          };
        }
        return {
          ...account,
          strategy: null,
          epics: [],
        };
      })
    );

    return accountsWithStrategies;
  } catch (error) {
    console.error('[LiveTrading] Error fetching accounts with strategies:', error);
    return [];
  }
}

