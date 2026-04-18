/**
 * Trade Queue Manager
 * 
 * Manages the flow of trades from brain calculation to execution.
 * 
 * Queue Flow per Account:
 * 1. BRAIN_CALCULATING - Brain calculation in progress
 * 2. CLOSING_POSITION - Closing existing position (if any)
 * 3. CHANGING_LEVERAGE - Adjusting leverage if needed
 * 4. READY_TO_BUY - Ready to fire buy order at T-15s
 * 5. TRADE_FIRED - Buy order sent
 * 6. COMPLETED - Trade confirmed
 * 
 * Priority Ordering Options (user selects in settings):
 * - highest_balance: Accounts with highest available balance first
 * - highest_pnl: Accounts with highest P&L first
 * - highest_sharpe: Accounts with highest Sharpe ratio first
 * - highest_winrate: Accounts with highest win rate first
 * - first_come_first_serve: Order by brain completion time
 */

import { orchestrationLogger } from './logger';
import type { BrainDecision } from './brain_orchestrator';
import { executeCloseTrades as closeOrchestratorClose, type CloseResult } from './close_orchestrator';
import { checkAndAdjustLeverage } from './leverage_checker';
import { connectionManager } from './connection_manager';
import { apiQueue } from './api_queue';
import { fundTracker } from './fund_tracker';
import { orchestrationTimer } from './timer';

export type TradeQueueState = 
  | 'BRAIN_CALCULATING'
  | 'CLOSING_POSITION'
  | 'CHANGING_LEVERAGE'
  | 'READY_TO_BUY'
  | 'TRADE_FIRED'
  | 'COMPLETED'
  | 'FAILED'
  | 'HOLD';  // No trade needed

export type PriorityOrderType = 
  | 'first_come_first_serve'
  | 'highest_balance'
  | 'highest_pnl'
  | 'highest_sharpe'
  | 'highest_winrate';

export interface QueuedTrade {
  accountId: number;
  accountName: string;
  capitalAccountId: string;
  accountType: 'demo' | 'live';
  windowCloseTime: string;
  marketCloseTime: Date;
  state: TradeQueueState;
  brainDecision: BrainDecision | null;
  queuedAt: Date;
  stateChangedAt: Date;
  closedPositionDealId: string | null;
  leverageAdjusted: boolean;
  error: string | null;
  // For priority ordering
  balance: number;
  pnl: number;
  sharpe: number;
  winRate: number;
}

class TradeQueueManager {
  private queues: Map<number, QueuedTrade[]> = new Map(); // Key: marketCloseTime.getTime()
  private priorityOrder: PriorityOrderType = 'first_come_first_serve';
  
  /**
   * Compare two queued trades using:
   * 1) LIVE before DEMO (always)
   * 2) User-selected priority order (Settings -> orchestration.trade_priority_order)
   * 3) Stable tie-breakers (queuedAt, accountId) for deterministic ordering
   */
  private compareTrades(a: QueuedTrade, b: QueuedTrade): number {
    // LIVE always first
    if (a.accountType !== b.accountType) {
      return a.accountType === 'live' ? -1 : 1;
    }

    // Apply selected priority ordering within the same environment
    let cmp = 0;
    switch (this.priorityOrder) {
      case 'highest_balance':
        cmp = b.balance - a.balance;
        break;
      case 'highest_pnl':
        cmp = b.pnl - a.pnl;
        break;
      case 'highest_sharpe':
        cmp = b.sharpe - a.sharpe;
        break;
      case 'highest_winrate':
        cmp = b.winRate - a.winRate;
        break;
      case 'first_come_first_serve':
      default:
        cmp = a.queuedAt.getTime() - b.queuedAt.getTime();
        break;
    }

    // Stable tie-breakers (avoid flapping when metrics are equal/0)
    if (cmp !== 0) return cmp;
    const t = a.queuedAt.getTime() - b.queuedAt.getTime();
    if (t !== 0) return t;
    return a.accountId - b.accountId;
  }
  
  /**
   * Initialize queue for a window
   */
  initWindow(marketCloseTime: Date): void {
    const windowKey = marketCloseTime.getTime();
    if (!this.queues.has(windowKey)) {
      this.queues.set(windowKey, []);
      orchestrationLogger.info('TRADE_QUEUE', `Initialized trade queue for window ${marketCloseTime.toISOString()}`);
    }
  }
  
  /**
   * Add account to queue (starts in BRAIN_CALCULATING state)
   */
  addToQueue(
    marketCloseTime: Date,
    accountId: number,
    accountName: string,
    capitalAccountId: string,
    accountType: 'demo' | 'live',
    windowCloseTime: string,
    balance: number = 0,
    pnl: number = 0,
    sharpe: number = 0,
    winRate: number = 0
  ): void {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey) || [];
    
    // Check if already in queue
    const existing = queue.find(t => t.accountId === accountId);
    if (existing) {
      orchestrationLogger.warn('TRADE_QUEUE', `Account ${accountId} already in queue for this window`);
      return;
    }
    
    const now = new Date();
    const queuedTrade: QueuedTrade = {
      accountId,
      accountName,
      capitalAccountId,
      accountType,
      windowCloseTime,
      marketCloseTime,
      state: 'BRAIN_CALCULATING',
      brainDecision: null,
      queuedAt: now,
      stateChangedAt: now,
      closedPositionDealId: null,
      leverageAdjusted: false,
      error: null,
      balance,
      pnl,
      sharpe,
      winRate,
    };
    
    queue.push(queuedTrade);
    this.queues.set(windowKey, queue);
    
    // Log with millisecond precision
    const timestamp = now.toISOString();
    const typeLabel = accountType.toUpperCase();
    console.log(`[TradeQueue] ${timestamp} [${typeLabel}] Account ${accountId} (${accountName}): QUEUED → BRAIN_CALCULATING [balance=$${balance.toFixed(2)}]`);
    
    orchestrationLogger.debug('TRADE_QUEUE', 
      `Added account ${accountId} (${accountName}) to queue`, 
      { accountId, data: { windowCloseTime, state: 'BRAIN_CALCULATING', accountType, balance, pnl, sharpe, winRate, timestamp } }
    );
  }

  /**
   * Update priority metrics for an account in the queue.
   * These fields back the existing Settings dropdown ordering at T-15s:
   * - highest_balance -> balance
   * - highest_pnl -> pnl
   * - highest_sharpe -> sharpe
   * - highest_winrate -> winRate
   */
  updateMetrics(
    marketCloseTime: Date,
    accountId: number,
    metrics: Partial<Pick<QueuedTrade, 'balance' | 'pnl' | 'sharpe' | 'winRate'>>
  ): void {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey);
    if (!queue) return;

    const trade = queue.find(t => t.accountId === accountId);
    if (!trade) return;

    if (typeof metrics.balance === 'number') trade.balance = metrics.balance;
    if (typeof metrics.pnl === 'number') trade.pnl = metrics.pnl;
    if (typeof metrics.sharpe === 'number') trade.sharpe = metrics.sharpe;
    if (typeof metrics.winRate === 'number') trade.winRate = metrics.winRate;

    orchestrationLogger.debug('TRADE_QUEUE',
      `[updateMetrics] accountId=${accountId}`,
      { accountId, data: { ...metrics } }
    );
  }
  
  /**
   * Update state for an account in the queue
   * 
   * ENHANCED LOGGING: Includes millisecond timestamps for tracking queue flow
   */
  updateState(
    marketCloseTime: Date,
    accountId: number,
    newState: TradeQueueState,
    brainDecision?: BrainDecision,
    error?: string
  ): void {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey);
    if (!queue) return;
    
    const trade = queue.find(t => t.accountId === accountId);
    if (!trade) return;
    
    const oldState = trade.state;
    const now = new Date();
    const prevStateTime = trade.stateChangedAt;
    const durationMs = prevStateTime ? now.getTime() - prevStateTime.getTime() : 0;
    
    trade.state = newState;
    trade.stateChangedAt = now;
    
    if (brainDecision) {
      trade.brainDecision = brainDecision;
    }
    
    if (error) {
      trade.error = error;
    }
    
    // Log with millisecond precision for queue flow analysis
    const timestamp = now.toISOString();
    const accountType = trade.accountType.toUpperCase();
    console.log(`[TradeQueue] ${timestamp} [${accountType}] Account ${accountId} (${trade.accountName}): ${oldState} → ${newState} [${durationMs}ms in prev state]`);
    
    orchestrationLogger.info('TRADE_QUEUE', 
      `Account ${accountId}: ${oldState} → ${newState} (${durationMs}ms)`,
      { accountId, data: { oldState, newState, decision: brainDecision?.decision, durationMs, timestamp } }
    );
  }
  
  /**
   * Mark position as closed
   */
  markPositionClosed(marketCloseTime: Date, accountId: number, dealId: string | null): void {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey);
    if (!queue) return;
    
    const trade = queue.find(t => t.accountId === accountId);
    if (!trade) return;
    
    trade.closedPositionDealId = dealId;
    trade.stateChangedAt = new Date();
  }
  
  /**
   * Mark leverage as adjusted
   */
  markLeverageAdjusted(marketCloseTime: Date, accountId: number): void {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey);
    if (!queue) return;
    
    const trade = queue.find(t => t.accountId === accountId);
    if (!trade) return;
    
    trade.leverageAdjusted = true;
    trade.stateChangedAt = new Date();
  }
  
  /**
   * Mark trade as fired (after API call sent)
   */
  markTradeFired(marketCloseTime: Date, accountId: number, dealReference: string): void {
    this.updateState(marketCloseTime, accountId, 'TRADE_FIRED');
    
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey);
    if (!queue) return;
    
    const trade = queue.find(t => t.accountId === accountId);
    if (trade) {
      const timestamp = new Date().toISOString();
      const typeLabel = trade.accountType.toUpperCase();
      console.log(`[TradeQueue] ${timestamp} [${typeLabel}] Account ${accountId} (${trade.accountName}): TRADE_FIRED → dealRef=${dealReference}`);
      
      orchestrationLogger.info('TRADE_QUEUE', 
        `Account ${accountId}: Trade fired with reference ${dealReference}`,
        { accountId, data: { dealReference, timestamp } }
      );
    }
  }
  
  /**
   * Mark trade as completed (after reconciliation confirms)
   */
  markTradeCompleted(marketCloseTime: Date, accountId: number, dealId: string): void {
    this.updateState(marketCloseTime, accountId, 'COMPLETED');
    
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey);
    const trade = queue?.find(t => t.accountId === accountId);
    
    const timestamp = new Date().toISOString();
    const typeLabel = trade?.accountType?.toUpperCase() || 'UNKNOWN';
    const accountName = trade?.accountName || `Account_${accountId}`;
    console.log(`[TradeQueue] ${timestamp} [${typeLabel}] Account ${accountId} (${accountName}): COMPLETED → dealId=${dealId}`);
    
    orchestrationLogger.info('TRADE_QUEUE', 
      `Account ${accountId}: Trade completed with dealId ${dealId}`,
      { accountId, data: { dealId, timestamp } }
    );
  }
  
  /**
   * Update balance for priority ordering
   */
  updateBalance(marketCloseTime: Date, accountId: number, balance: number): void {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey);
    if (!queue) return;
    
    const trade = queue.find(t => t.accountId === accountId);
    if (trade) {
      trade.balance = balance;
    }
  }
  
  /**
   * Get all trades ready to buy, sorted by priority
   */
  getReadyToBuy(marketCloseTime: Date): QueuedTrade[] {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey) || [];
    
    const readyTrades = queue.filter(t => t.state === 'READY_TO_BUY');
    
    // Sort by priority order
    return this.sortByPriority(readyTrades);
  }
  
  /**
   * Get next trade ready to buy (respects priority)
   */
  getNextReadyTrade(marketCloseTime: Date): QueuedTrade | null {
    const ready = this.getReadyToBuy(marketCloseTime);
    return ready.length > 0 ? ready[0] : null;
  }
  
  /**
   * Check if any trades are still processing (not yet READY_TO_BUY or terminal state)
   */
  hasProcessingTrades(marketCloseTime: Date): boolean {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey) || [];
    
    return queue.some(t => 
      t.state === 'BRAIN_CALCULATING' || 
      t.state === 'CLOSING_POSITION' || 
      t.state === 'CHANGING_LEVERAGE'
    );
  }
  
  /**
   * Get queue status summary
   */
  getQueueStatus(marketCloseTime: Date): {
    total: number;
    brainCalculating: number;
    closingPosition: number;
    changingLeverage: number;
    readyToBuy: number;
    tradeFired: number;
    completed: number;
    failed: number;
    hold: number;
  } {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey) || [];
    
    return {
      total: queue.length,
      brainCalculating: queue.filter(t => t.state === 'BRAIN_CALCULATING').length,
      closingPosition: queue.filter(t => t.state === 'CLOSING_POSITION').length,
      changingLeverage: queue.filter(t => t.state === 'CHANGING_LEVERAGE').length,
      readyToBuy: queue.filter(t => t.state === 'READY_TO_BUY').length,
      tradeFired: queue.filter(t => t.state === 'TRADE_FIRED').length,
      completed: queue.filter(t => t.state === 'COMPLETED').length,
      failed: queue.filter(t => t.state === 'FAILED').length,
      hold: queue.filter(t => t.state === 'HOLD').length,
    };
  }
  
  /**
   * Set priority ordering (from settings)
   */
  setPriorityOrder(order: PriorityOrderType): void {
    this.priorityOrder = order;
    orchestrationLogger.info('TRADE_QUEUE', `Priority order set to: ${order}`);
  }
  
  /**
   * Get current priority order
   */
  getPriorityOrder(): PriorityOrderType {
    return this.priorityOrder;
  }
  
  /**
   * Sort trades by priority
   */
  private sortByPriority(trades: QueuedTrade[]): QueuedTrade[] {
    // LIVE-first, then apply the existing dropdown order (stable tie-breakers)
    return [...trades].sort((a, b) => this.compareTrades(a, b));

    // --- ROLLBACK REFERENCE (old behavior; kept for easy revert) ---
    // switch (this.priorityOrder) {
    //   case 'highest_balance':
    //     return [...trades].sort((a, b) => b.balance - a.balance);
    //   case 'highest_pnl':
    //     return [...trades].sort((a, b) => b.pnl - a.pnl);
    //   case 'highest_sharpe':
    //     return [...trades].sort((a, b) => b.sharpe - a.sharpe);
    //   case 'highest_winrate':
    //     return [...trades].sort((a, b) => b.winRate - a.winRate);
    //   case 'first_come_first_serve':
    //   default:
    //     return [...trades].sort((a, b) => a.queuedAt.getTime() - b.queuedAt.getTime());
    // }
  }
  
  /**
   * Get trade by account ID
   */
  getTrade(marketCloseTime: Date, accountId: number): QueuedTrade | null {
    const windowKey = marketCloseTime.getTime();
    const queue = this.queues.get(windowKey);
    if (!queue) return null;
    
    return queue.find(t => t.accountId === accountId) || null;
  }
  
  /**
   * Get all trades for a window
   */
  getAllTrades(marketCloseTime: Date): QueuedTrade[] {
    const windowKey = marketCloseTime.getTime();
    return this.queues.get(windowKey) || [];
  }
  
  /**
   * Clear queue for a window
   */
  clearWindow(marketCloseTime: Date): void {
    const windowKey = marketCloseTime.getTime();
    this.queues.delete(windowKey);
    orchestrationLogger.debug('TRADE_QUEUE', `Cleared queue for window ${marketCloseTime.toISOString()}`);
  }
  
  /**
   * Clear all queues
   */
  clearAll(): void {
    this.queues.clear();
    orchestrationLogger.debug('TRADE_QUEUE', 'Cleared all trade queues');
  }
  
  /**
   * Process account after brain calculation completes
   * This kicks off the close → leverage → ready flow
   * 
   * Returns: The updated queued trade, or null if not found/error
   */
  async processAccountBrainComplete(
    marketCloseTime: Date,
    accountId: number,
    brainDecision: BrainDecision,
    account: { 
      capitalAccountId: string; 
      accountType: 'demo' | 'live';
      accountName?: string;
    }
  ): Promise<QueuedTrade | null> {
    const trade = this.getTrade(marketCloseTime, accountId);
    if (!trade) {
      orchestrationLogger.warn('TRADE_QUEUE', `Account ${accountId} not found in queue`);
      return null;
    }
    
    // Update with brain decision
    trade.brainDecision = brainDecision;
    trade.stateChangedAt = new Date();
    
    // ═══════════════════════════════════════════════════════════════════════════
    // CRITICAL FIX: For time-based strategies, ALWAYS close positions at T-60
    // REGARDLESS of BUY or HOLD decision!
    // 
    // The trade cycle is:
    // - Yesterday: Position opened
    // - Today T-60: Close yesterday's position (ALWAYS)
    // - Today T-15: If BUY → open new position
    // ═══════════════════════════════════════════════════════════════════════════
    
    // Move to CLOSING_POSITION state (for both BUY and HOLD)
    this.updateState(marketCloseTime, accountId, 'CLOSING_POSITION', brainDecision);
    
    orchestrationLogger.info('TRADE_QUEUE', 
      `Account ${accountId} ${brainDecision.decision.toUpperCase()} decision - closing existing positions first`,
      { accountId, data: { decision: brainDecision.decision, epic: brainDecision.epic } }
    );
    
    try {
      // Close existing positions for this account (ALWAYS, regardless of BUY/HOLD)
      const windowCloseTime = brainDecision.windowCloseTime;
      const closeResults = await closeOrchestratorClose(windowCloseTime, [brainDecision], false);
      
      const accountCloseResult = closeResults.find(r => r.accountId === accountId);
      if (accountCloseResult) {
        orchestrationLogger.info('TRADE_QUEUE', 
          `Account ${accountId}: Closed ${accountCloseResult.tradesClosedCount} positions, cancelled ${accountCloseResult.pendingOrdersCancelled} orders`,
          { accountId, data: { closedCount: accountCloseResult.tradesClosedCount, cancelledOrders: accountCloseResult.pendingOrdersCancelled } }
        );
      }
    } catch (closeError: any) {
      orchestrationLogger.warn('TRADE_QUEUE', 
        `Account ${accountId}: Close phase error: ${closeError.message}`,
        { accountId }
      );
      // Continue anyway - we still want to handle the decision
    }
    
    // If HOLD, mark as HOLD and handle carryover (AFTER closing positions)
    if (brainDecision.decision === 'hold') {
      this.updateState(marketCloseTime, accountId, 'HOLD', brainDecision);
      
      // CRITICAL: Notify fund tracker of HOLD decision for carryover logic
      // This allows the next window to inherit this window's allocation (if carryOver enabled)
      fundTracker.markHold(accountId, brainDecision.windowCloseTime);
      
      orchestrationLogger.info('TRADE_QUEUE', 
        `Account ${accountId}: HOLD - positions closed, no new trade`,
        { accountId }
      );
      
      return trade;
    }
    
    // BUY decision - continue with leverage → ready flow
    orchestrationLogger.info('TRADE_QUEUE', 
      `Account ${accountId} BUY - proceeding to leverage check`,
      { accountId, data: { epic: brainDecision.epic, leverage: brainDecision.leverage } }
    );
    
    try {
      // Leverage phase (only for BUY)
      // Move to CHANGING_LEVERAGE state
      this.updateState(marketCloseTime, accountId, 'CHANGING_LEVERAGE');
      
      // Get the API client for this account type
      const client = await connectionManager.getClient(account.accountType as 'demo' | 'live');
      if (!client) {
        throw new Error(`No API client available for ${account.accountType} account`);
      }

      // Queue leverage check as a CRITICAL call so it can sit behind OPEN(LIVE) at T-15,
      // and so account switching cannot interleave with other operations.
      const leverageResult = await new Promise<any>((resolve, reject) => {
        // Get actual leverage state from timer (T-5m fetch)
        const actualLeverageState = orchestrationTimer.getActualLeverageState();
        
        apiQueue.enqueue({
          priority: 'critical',
          isCritical: true,
          environment: account.accountType,
          operation: 'leverage',
          description: `LEVERAGE ${brainDecision.leverage || 1}x epic=${brainDecision.epic} accountId=${accountId} env=${account.accountType}`,
          fn: async () => {
            const switched = await client.switchAccount(account.capitalAccountId);
            if (!switched) {
              throw new Error(`Failed to switch to Capital.com account ${account.capitalAccountId}`);
            }

            return await checkAndAdjustLeverage(
              client,
              accountId,
              brainDecision.epic,
              brainDecision.leverage || 1,
              account.capitalAccountId,
              actualLeverageState  // Pass T-5m actual state (GROUND TRUTH)
            );
          },
          maxRetries: 3,
          onSuccess: (res) => resolve(res),
          onError: (err) => reject(err),
        });
      });
      
      trade.leverageAdjusted = leverageResult.adjusted || false;
      orchestrationLogger.info('TRADE_QUEUE', 
        `Account ${accountId}: Leverage ${leverageResult.adjusted ? 'adjusted to ' + brainDecision.leverage : 'OK'}`,
        { accountId, data: { adjusted: leverageResult.adjusted, targetLeverage: brainDecision.leverage } }
      );
      
      // Move to READY_TO_BUY state
      this.updateState(marketCloseTime, accountId, 'READY_TO_BUY');
      
      return trade;
      
    } catch (error: any) {
      orchestrationLogger.logError('TRADE_QUEUE', 
        `Account ${accountId} failed during close/leverage: ${error.message}`,
        error
      );
      this.updateState(marketCloseTime, accountId, 'FAILED', undefined, error.message);
      trade.error = error.message;
      return trade;
    }
  }
  
  /**
   * Check if market is still open for trading
   * Returns true if current time is before market close
   */
  isMarketOpen(marketCloseTime: Date): boolean {
    return new Date() < marketCloseTime;
  }
}

// Singleton instance
export const tradeQueue = new TradeQueueManager();

