/**
 * Fund Allocation Tracker with Dynamic Rebalancing
 * 
 * IMPORTANT: ALL window times must be in UTC format!
 * Capital.com returns market hours in UTC, so strategy windows must match.
 * Example: US market close 4pm ET = 21:00:00 UTC (NOT 16:00:00!)
 * 
 * Tracks fund usage across windows for each bot within a trading day.
 * Implements carry over logic: if a window has HOLD decision, its allocation
 * is carried over to the next window.
 * 
 * Uses strategy's configured percentages - NO HARDCODED VALUES:
 * - totalAccountBalancePct: Max % of account balance to use (from strategy config)
 * - allocationPct: % allocated per window (from strategy config)
 * 
 * Dynamic Rebalancing:
 * When a window needs funds but another window has excess allocation,
 * we can rebalance by partially closing the over-allocated position.
 * 
 * PARTIAL CLOSE STRATEGY:
 * We use opposite-direction trades to partially close positions.
 * This avoids closing and reopening, which would incur extra fees.
 * Example: 100 contracts LONG, close 49 → place SELL 49 → net 51 LONG
 * The original dealId remains valid for the remaining position.
 * 
 * Example:
 * - Window 1 (21:00 UTC): 50% allocation → HOLD → carry over 50%
 * - Window 2 (01:00 UTC): 50% allocation + 50% carried = 100% available
 * 
 * Resets at the start of each trading day.
 */

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

interface BotFundState {
  accountId: number;
  totalBalance: number;        // Account balance * totalAccountBalancePct from strategy (no hardcoded values)
  originalDailyBalance: number; // ORIGINAL balance at start of day (before any trades used margin)
  windowAllocations: Map<string, number>; // windowCloseTime → base allocation %
  windowCarryOver: Map<string, boolean>;  // windowCloseTime → carryOver setting (from strategy config)
  // NEW: Also store by tradingHoursType for dynamic matching
  // This handles DST, short days, etc. when actual close time differs from stored time
  hoursTypeAllocations: Map<string, number>; // tradingHoursType ('regular'|'extended') → base allocation %
  hoursTypeCarryOver: Map<string, boolean>;  // tradingHoursType → carryOver setting
  hoursTypeToCloseTime: Map<string, string>; // tradingHoursType → stored closeTime (for backwards compat)
  usedPct: number;              // Total % used so far today
  carriedOverPct: number;       // % carried over from HOLD windows
  windowsTradedToday: Set<string>; // Track which windows have already traded today
  windowsHoldToday: Set<string>;   // Track which windows had HOLD decisions today
  // Track actual position values per window (updated from trade poller)
  windowPositionValues: Map<string, number>; // windowCloseTime → current position value
  windowDealIds: Map<string, string>; // windowCloseTime → dealId of open position
}

/**
 * Rebalance calculation result
 */
export interface RebalanceResult {
  needed: boolean;
  fromWindow: string | null;      // Window to take funds from
  fromDealId: string | null;      // Deal ID of position to partially close
  currentPct: number;             // Current allocation % of fromWindow
  targetPct: number;              // Target allocation % of fromWindow
  excessPct: number;              // % to close (currentPct - targetPct)
  excessValue: number;            // $ value to close
  contractsToClose: number;       // Number of contracts to close
}

class FundTracker {
  private botStates: Map<number, BotFundState>;
  private lastClearSession: string;  // Session ID, not calendar date
  
  // Single-window day detection
  // When true, ALL accounts get 100% allocation (conflict resolution picks the best DNA across ALL strands)
  private isSingleWindowDayFlag: boolean = false;
  
  constructor() {
    this.botStates = new Map();
    this.lastClearSession = this.getTradingSessionId();
  }
  
  /**
   * Mark today as a single-window day
   * Called by timer when it detects only one unique market close time for all epics
   * 
   * On single-window days (e.g., day after Christmas with 18:00 close):
   * - ALL DNA strands from ALL windows compete in conflict resolution
   * - The winning DNA gets 100% of available funds (not just its window's allocation)
   * - This ensures we don't waste capital on partial allocation days
   */
  setSingleWindowDay(isSingle: boolean): void {
    this.isSingleWindowDayFlag = isSingle;
    if (isSingle) {
      console.log('[FundTracker] ⚡ SINGLE-WINDOW DAY DETECTED - All accounts will get 100% allocation');
    }
  }
  
  /**
   * Check if today is a single-window day
   */
  isSingleWindowDay(): boolean {
    return this.isSingleWindowDayFlag;
  }
  
  /**
   * Initialize fund tracking for a bot
   * 
   * Uses the strategy's totalAccountBalancePct to determine max allocation.
   * This prevents RISK_CHECK failures from Capital.com when using full balance with leverage.
   * 
   * DYNAMIC WINDOW MATCHING:
   * Stores allocations by BOTH closeTime AND tradingHoursType.
   * When looking up allocation, tries closeTime first (exact match),
   * then falls back to tradingHoursType (handles DST, short days, etc.)
   * 
   * CROSS-WINDOW TRACKING:
   * - Preserves windowsTradedToday and usedPct when re-initialized within same day
   * - Uses originalDailyBalance (first init of day) for allocation calculations
   * - This ensures Window 2 gets its full allocation even if Window 1 used margin
   * 
   * Example: 
   * - Account balance: $1000
   * - totalAccountBalancePct: 98 (from strategy)
   * - Max tradeable: $980 (leaves $20 buffer for spreads/fees)
   */
  initialize(accountId: number, balance: number, windowConfig: any): void {
    this.clearIfNewDay();
    
    // Check if we already have state for this account today
    const existingState = this.botStates.get(accountId);
    
    const windowAllocations = new Map<string, number>();
    const windowCarryOver = new Map<string, boolean>();
    const hoursTypeAllocations = new Map<string, number>();
    const hoursTypeCarryOver = new Map<string, boolean>();
    const hoursTypeToCloseTime = new Map<string, string>();
    
    // Use strategy's totalAccountBalancePct (default 99% if not specified)
    // IMPORTANT: This prevents RISK_CHECK failures when trading with leverage
    const totalAccountBalancePct = windowConfig?.totalAccountBalancePct ?? 99;
    const effectiveBalance = balance * (totalAccountBalancePct / 100);
    
    if (windowConfig && windowConfig.windows) {
      for (const window of windowConfig.windows) {
        // NORMALIZE time to HH:MM:SS format for consistent lookups
        const normalizedCloseTime = normalizeTimeFormat(window.closeTime);
        
        // Store by closeTime (exact match, always normalized)
        windowAllocations.set(normalizedCloseTime, window.allocationPct);
        // Store carryOver setting (default TRUE for backwards compatibility)
        windowCarryOver.set(normalizedCloseTime, window.carryOver !== undefined ? window.carryOver : true);
        
        // Also store by tradingHoursType (dynamic match for DST/short days)
        // If tradingHoursType not stored (old strategies), infer from closeTime
        let hoursType = window.tradingHoursType;
        if (!hoursType) {
          hoursType = this.inferTradingHoursType(normalizedCloseTime);
        }
        
        if (hoursType) {
          hoursTypeAllocations.set(hoursType, window.allocationPct);
          hoursTypeCarryOver.set(hoursType, window.carryOver !== undefined ? window.carryOver : true);
          hoursTypeToCloseTime.set(hoursType, normalizedCloseTime);
        }
      }
    }
    
    // CRITICAL: If account already initialized today, preserve cross-window tracking
    // This prevents Window 2 from losing track of Window 1's usage
    const windowsTradedToday = existingState?.windowsTradedToday || new Set<string>();
    const windowsHoldToday = existingState?.windowsHoldToday || new Set<string>();
    const usedPct = existingState?.usedPct || 0;
    const carriedOverPct = existingState?.carriedOverPct || 0;
    
    // Use original daily balance if available (prevents margin-reduced balance from affecting W2)
    // Only set originalDailyBalance on FIRST init of the day
    const originalDailyBalance = existingState?.originalDailyBalance || effectiveBalance;
    
    const hoursTypes = Array.from(hoursTypeAllocations.keys());
    const isReinit = !!existingState;
    const carryOverSettings = Array.from(windowCarryOver.entries()).map(([k, v]) => `${k}:${v}`).join(', ');
    console.log(`[FundTracker] ${isReinit ? 'Re-initialize' : 'Initialize'} account ${accountId}: balance=$${balance.toFixed(2)}, totalPct=${totalAccountBalancePct}%, effective=$${effectiveBalance.toFixed(2)}, originalDaily=$${originalDailyBalance.toFixed(2)}, usedPct=${usedPct.toFixed(1)}%, carriedOver=${carriedOverPct.toFixed(1)}%, windowsTraded=${Array.from(windowsTradedToday).join(',') || 'none'}, windowsHold=${Array.from(windowsHoldToday).join(',') || 'none'}, carryOver=[${carryOverSettings}]`);
    
    this.botStates.set(accountId, {
      accountId,
      totalBalance: effectiveBalance, // Current effective balance (for position sizing)
      originalDailyBalance,           // ORIGINAL balance (for allocation % calculations)
      windowAllocations,
      windowCarryOver,                 // carryOver setting per window (from strategy config)
      hoursTypeAllocations,
      hoursTypeCarryOver,              // carryOver setting per hours type
      hoursTypeToCloseTime,
      usedPct,                         // Preserved from previous window
      carriedOverPct,                  // Preserved from previous window
      windowsTradedToday,              // Preserved - tracks which windows traded
      windowsHoldToday,                // Preserved - tracks which windows had HOLD
      windowPositionValues: existingState?.windowPositionValues || new Map(),
      windowDealIds: existingState?.windowDealIds || new Map(),
    });
    
    // ═══════════════════════════════════════════════════════════════════════════
    // PERSIST TO DATABASE via tradingSessionState
    // This ensures state survives server restarts
    // ═══════════════════════════════════════════════════════════════════════════
    this.persistToTradingSessionState(accountId, balance, windowConfig).catch(err => {
      console.error(`[FundTracker] Failed to persist session state for account ${accountId}:`, err.message);
    });
  }
  
  /**
   * Persist fund tracker state to database via tradingSessionState service
   * This is called after initialize() to ensure DB state is in sync
   */
  private async persistToTradingSessionState(accountId: number, balance: number, windowConfig: any): Promise<void> {
    try {
      const { tradingSessionState } = await import('../services/trading_session_state');
      await tradingSessionState.initializeSession(accountId, balance, windowConfig, 'time_based');
    } catch (err: any) {
      // Non-fatal - we still have in-memory state
      console.warn(`[FundTracker] persistToTradingSessionState warning: ${err.message}`);
    }
  }
  
  /**
   * ═══════════════════════════════════════════════════════════════════════════════
   * RECONSTRUCT DAILY STATE FROM DATABASE AFTER SERVER RESTART
   * ═══════════════════════════════════════════════════════════════════════════════
   * 
   * PURPOSE:
   * When the server restarts between trading windows (e.g., between TECL at 21:00 UTC
   * and SOXL at 01:00 UTC), the in-memory fund tracker state is lost. This method
   * queries the actual_trades table to reconstruct that state.
   * 
   * WHAT IT RECONSTRUCTS:
   * 1. windowsTradedToday - Set of windows that already executed trades
   * 2. usedPct - Percentage of originalDailyBalance already used by trades
   * 3. carriedOverPct - Percentage carried over from HOLD windows (if carryOver=true)
   * 4. windowsHoldToday - Set of windows that had HOLD decisions
   * 
   * EXAMPLE SCENARIO:
   * - Session: 2026-01-05
   * - Window 1 (TECL 21:00 UTC): Traded, used 50% ($215 margin)
   * - Server restarts at 23:00 UTC
   * - Window 2 (SOXL 01:00 UTC): This method runs BEFORE initialize()
   *   - Finds TECL trade in database
   *   - Sets usedPct = 50%
   *   - Window 2 correctly sees only 50% remaining
   * 
   * HOLD WINDOW DETECTION:
   * If a window's close time has PASSED but no trade exists for it, we treat it as
   * a HOLD decision. If carryOver=true for that window, its allocation carries forward.
   * 
   * CRITICAL: Call this BEFORE initialize() when server may have restarted between windows.
   * 
   * @param accountId - The account to reconstruct state for
   * @param windowConfig - Strategy's windowConfig containing totalAccountBalancePct and windows array
   * @param fullAccountBalance - Full account balance (NOT "available" which is reduced by margin)
   * @param closingLogger - Optional ClosingLogger instance for detailed logging to closing file
   */
  async reconstructFromDatabase(
    accountId: number, 
    windowConfig: any,
    fullAccountBalance: number,
    closingLogger?: any
  ): Promise<void> {
    // Helper function to log both to console and closing file
    const log = (message: string) => {
      console.log(message);
      closingLogger?.log?.(message);
    };
    
    try {
      // ═══════════════════════════════════════════════════════════════════════════
      // FIRST: Try to load from tradingSessionState (DB-persisted state)
      // This is the preferred method as it survives restarts
      // ═══════════════════════════════════════════════════════════════════════════
      try {
        const { tradingSessionState } = await import('../services/trading_session_state');
        const sessionState = await tradingSessionState.getSessionState(accountId);
        
        if (sessionState) {
          log(`[FundTracker] ═══════════════════════════════════════════════════════════════`);
          log(`[FundTracker] ✅ LOADED STATE FROM tradingSessionState`);
          log(`[FundTracker] ═══════════════════════════════════════════════════════════════`);
          log(`[FundTracker]   Session: ${sessionState.sessionId}`);
          log(`[FundTracker]   Original Balance: $${sessionState.originalDailyBalance.toFixed(2)}`);
          log(`[FundTracker]   Used %: ${sessionState.usedPct.toFixed(1)}%`);
          log(`[FundTracker]   Carried Over %: ${sessionState.carriedOverPct.toFixed(1)}%`);
          log(`[FundTracker]   Windows Traded: ${sessionState.windowsTraded.join(', ') || 'none'}`);
          log(`[FundTracker]   Windows HOLD: ${sessionState.windowsHold.join(', ') || 'none'}`);
          
          // Apply state to in-memory fund tracker
          const existingState = this.botStates.get(accountId);
          if (existingState) {
            existingState.usedPct = sessionState.usedPct;
            existingState.carriedOverPct = sessionState.carriedOverPct;
            existingState.originalDailyBalance = sessionState.originalDailyBalance;
            sessionState.windowsTraded.forEach(w => existingState.windowsTradedToday.add(w));
            sessionState.windowsHold.forEach(w => existingState.windowsHoldToday.add(w));
            this.botStates.set(accountId, existingState);
            log(`[FundTracker] ✅ Applied tradingSessionState to in-memory fund tracker`);
          }
          
          return; // Success! No need to query actual_trades
        }
      } catch (tssErr: any) {
        log(`[FundTracker] ⚠️ tradingSessionState load failed: ${tssErr.message} - falling back to actual_trades query`);
      }
      
      // ─────────────────────────────────────────────────────────────────────────
      // FALLBACK: Query actual_trades table directly (legacy method)
      // ─────────────────────────────────────────────────────────────────────────
      
      // ─────────────────────────────────────────────────────────────────────────
      // STEP 1: Determine trading session
      // Trading sessions run from 02:00 UTC Day N to 01:59 UTC Day N+1
      // This keeps overnight windows (21:00 → 01:00) in the same session
      // ─────────────────────────────────────────────────────────────────────────
      const sessionId = this.getTradingSessionId();
      const now = new Date();
      
      log(`[FundTracker] ═══════════════════════════════════════════════════════════════`);
      log(`[FundTracker] 🔄 RECONSTRUCTING STATE FROM DATABASE`);
      log(`[FundTracker] ═══════════════════════════════════════════════════════════════`);
      log(`[FundTracker]   Account ID: ${accountId}`);
      log(`[FundTracker]   Current Time: ${now.toISOString()}`);
      log(`[FundTracker]   Trading Session: ${sessionId}`);
      log(`[FundTracker]   Full Account Balance: $${fullAccountBalance.toFixed(2)}`);
      
      // ─────────────────────────────────────────────────────────────────────────
      // STEP 2: Connect to database
      // ─────────────────────────────────────────────────────────────────────────
      const { getDb } = await import('../db');
      const { actualTrades } = await import('../../drizzle/schema');
      const { eq, and, gte, inArray } = await import('drizzle-orm');
      
      const db = await getDb();  // FIXED: was missing await - caused "db.select is not a function"
      if (!db) {
        log(`[FundTracker] ⚠️ ERROR: No database connection - cannot reconstruct state`);
        log(`[FundTracker]   → Fund tracker will use fresh balance (may cause incorrect allocation)`);
        return;
      }
      
      // ─────────────────────────────────────────────────────────────────────────
      // STEP 3: Query actual_trades for this session
      // Session starts at 02:00 UTC of the session date
      // Only include 'open' or 'closed' trades (not 'pending' or 'failed')
      // ─────────────────────────────────────────────────────────────────────────
      const sessionDate = new Date(sessionId + 'T02:00:00.000Z');
      log(`[FundTracker]   Session Start: ${sessionDate.toISOString()}`);
      
      const todaysTrades = await db
        .select({
          id: actualTrades.id,
          windowCloseTime: actualTrades.windowCloseTime,
          epic: actualTrades.epic,
          contracts: actualTrades.contracts,
          entryPrice: actualTrades.entryPrice,
          leverage: actualTrades.leverage,
          status: actualTrades.status,
          createdAt: actualTrades.createdAt,
        })
        .from(actualTrades)
        .where(
          and(
            eq(actualTrades.accountId, accountId),
            gte(actualTrades.createdAt, sessionDate),
            inArray(actualTrades.status, ['open', 'closed'])
          )
        );
      
      // Build set of windows that traded
      const tradedWindowTimes = new Set(todaysTrades.map(t => t.windowCloseTime || '21:00:00'));
      
      log(`[FundTracker] ───────────────────────────────────────────────────────────────`);
      log(`[FundTracker]   QUERY RESULTS: Found ${todaysTrades.length} trade(s) in session`);
      log(`[FundTracker]   Traded Windows: [${Array.from(tradedWindowTimes).join(', ') || 'none'}]`);
      
      // ─────────────────────────────────────────────────────────────────────────
      // STEP 4: Calculate totalAccountBalancePct and originalDailyBalance
      // originalDailyBalance = fullAccountBalance × (totalAccountBalancePct / 100)
      // This is the BASE for all percentage calculations
      // ─────────────────────────────────────────────────────────────────────────
      const totalAccountBalancePct = windowConfig?.totalAccountBalancePct ?? 99;
      const effectiveBalance = fullAccountBalance * (totalAccountBalancePct / 100);
      
      log(`[FundTracker]   Total Account Balance %: ${totalAccountBalancePct}%`);
      log(`[FundTracker]   Original Daily Balance: $${effectiveBalance.toFixed(2)} (basis for allocation %)`);
      
      // ─────────────────────────────────────────────────────────────────────────
      // STEP 5: Get or create state structure
      // If no existing state (server restart), create fresh structure
      // ─────────────────────────────────────────────────────────────────────────
      let existingState = this.botStates.get(accountId);
      const hadExistingState = !!existingState;
      
      if (!existingState) {
        log(`[FundTracker]   State: Creating NEW (no existing state - likely server restart)`);
        
        // Parse window allocations from strategy config
        const windowAllocations = new Map<string, number>();
        const windowCarryOver = new Map<string, boolean>();
        const hoursTypeAllocations = new Map<string, number>();
        const hoursTypeCarryOver = new Map<string, boolean>();
        const hoursTypeToCloseTime = new Map<string, string>();
        
        if (windowConfig && windowConfig.windows) {
          for (const window of windowConfig.windows) {
            windowAllocations.set(window.closeTime, window.allocationPct);
            windowCarryOver.set(window.closeTime, window.carryOver !== undefined ? window.carryOver : true);
            
            let hoursType = window.tradingHoursType;
            if (!hoursType) {
              hoursType = this.inferTradingHoursType(window.closeTime);
            }
            if (hoursType) {
              hoursTypeAllocations.set(hoursType, window.allocationPct);
              hoursTypeCarryOver.set(hoursType, window.carryOver !== undefined ? window.carryOver : true);
              hoursTypeToCloseTime.set(hoursType, window.closeTime);
            }
          }
        }
        
        existingState = {
          accountId,
          totalBalance: effectiveBalance,
          originalDailyBalance: effectiveBalance,
          windowAllocations,
          windowCarryOver,
          hoursTypeAllocations,
          hoursTypeCarryOver,
          hoursTypeToCloseTime,
          usedPct: 0,
          carriedOverPct: 0,
          windowsTradedToday: new Set<string>(),
          windowsHoldToday: new Set<string>(),
          windowPositionValues: new Map(),
          windowDealIds: new Map(),
        };
      } else {
        log(`[FundTracker]   State: Using EXISTING (state was preserved in memory)`);
      }
      
      // ─────────────────────────────────────────────────────────────────────────
      // STEP 6: Process each trade found in database
      // Calculate margin used and percentage of originalDailyBalance
      // Mark windows as traded
      // ─────────────────────────────────────────────────────────────────────────
      log(`[FundTracker] ───────────────────────────────────────────────────────────────`);
      log(`[FundTracker]   PROCESSING TRADES:`);
      
      let totalUsedPct = 0;
      
      if (todaysTrades.length === 0) {
        log(`[FundTracker]     (no trades found)`);
      }
      
      for (const trade of todaysTrades) {
        const windowTime = trade.windowCloseTime || '21:00:00';
        
        // Mark this window as traded
        existingState.windowsTradedToday.add(windowTime);
        
        // Also mark by hours type for flexible matching
        const hoursType = this.inferTradingHoursType(windowTime);
        if (hoursType) {
          existingState.windowsTradedToday.add(hoursType);
        }
        
        // Calculate margin used: notional / leverage
        const contracts = Number(trade.contracts) || 0;
        const entryPrice = Number(trade.entryPrice) || 0;
        const leverage = Number(trade.leverage) || 1;
        const notional = contracts * entryPrice;
        const marginUsed = notional / leverage;
        
        // Calculate what % of originalDailyBalance this trade used
        const pctUsed = (marginUsed / existingState.originalDailyBalance) * 100;
        totalUsedPct += pctUsed;
        
        log(`[FundTracker]     Trade #${trade.id}: ${trade.epic} @ Window ${windowTime}`);
        log(`[FundTracker]       Contracts: ${contracts} × $${entryPrice.toFixed(2)} = $${notional.toFixed(2)} notional`);
        log(`[FundTracker]       Leverage: ${leverage}x → Margin: $${marginUsed.toFixed(2)}`);
        log(`[FundTracker]       % of Daily Balance: ${pctUsed.toFixed(1)}%`);
      }
      
      existingState.usedPct = totalUsedPct;
      
      // ─────────────────────────────────────────────────────────────────────────
      // STEP 7: Check for HOLD windows (passed but no trade)
      // If a window's time has passed and no trade exists, it was a HOLD
      // If carryOver=true, add its allocation to carriedOverPct
      // If carryOver=false, the allocation is lost
      // ─────────────────────────────────────────────────────────────────────────
      log(`[FundTracker] ───────────────────────────────────────────────────────────────`);
      log(`[FundTracker]   CHECKING FOR HOLD WINDOWS:`);
      
      let totalCarriedOverPct = 0;
      let holdWindowsChecked = 0;
      
      if (windowConfig && windowConfig.windows) {
        for (const windowDef of windowConfig.windows) {
          holdWindowsChecked++;
          const windowCloseTime = windowDef.closeTime;
          // Normalize time format (handle "21:00" vs "21:00:00")
          const normalizedTime = windowCloseTime.includes(':') && windowCloseTime.split(':').length === 2 
            ? windowCloseTime + ':00' 
            : windowCloseTime;
          
          // Calculate when this window closes for current session
          const windowDateTime = this.getWindowDateTimeForSession(sessionId, normalizedTime);
          const hasPassed = now > windowDateTime;
          
          // Check if this window traded (check multiple formats)
          const didTrade = tradedWindowTimes.has(windowCloseTime) || 
                           tradedWindowTimes.has(normalizedTime) ||
                           existingState.windowsTradedToday.has(windowCloseTime) ||
                           existingState.windowsTradedToday.has(normalizedTime);
          
          const carryOver = windowDef.carryOver !== undefined ? windowDef.carryOver : true;
          const allocationPct = windowDef.allocationPct || 50;
          
          log(`[FundTracker]     Window ${normalizedTime}:`);
          log(`[FundTracker]       Closes: ${windowDateTime.toISOString()}`);
          log(`[FundTracker]       Has Passed: ${hasPassed ? 'YES' : 'NO'}`);
          log(`[FundTracker]       Did Trade: ${didTrade ? 'YES' : 'NO'}`);
          log(`[FundTracker]       Allocation: ${allocationPct}%, CarryOver: ${carryOver}`);
          
          if (hasPassed && !didTrade) {
            // This window ran but didn't trade = HOLD decision
            existingState.windowsHoldToday.add(normalizedTime);
            
            if (carryOver) {
              totalCarriedOverPct += allocationPct;
              log(`[FundTracker]       → HOLD with carryOver=true: +${allocationPct}% carried forward`);
            } else {
              log(`[FundTracker]       → HOLD with carryOver=false: ${allocationPct}% LOST`);
            }
          } else if (!hasPassed) {
            log(`[FundTracker]       → Window not yet passed (will trade later)`);
          } else {
            log(`[FundTracker]       → Already traded`);
          }
        }
      } else {
        log(`[FundTracker]     (no windows defined in strategy config)`);
      }
      
      existingState.carriedOverPct = totalCarriedOverPct;
      
      // ─────────────────────────────────────────────────────────────────────────
      // STEP 8: Save reconstructed state and log summary
      // ─────────────────────────────────────────────────────────────────────────
      this.botStates.set(accountId, existingState);
      
      log(`[FundTracker] ═══════════════════════════════════════════════════════════════`);
      log(`[FundTracker] ✅ RECONSTRUCTION COMPLETE`);
      log(`[FundTracker] ═══════════════════════════════════════════════════════════════`);
      log(`[FundTracker]   Original Daily Balance: $${existingState.originalDailyBalance.toFixed(2)}`);
      log(`[FundTracker]   Used %: ${totalUsedPct.toFixed(1)}% ($${(existingState.originalDailyBalance * totalUsedPct / 100).toFixed(2)})`);
      log(`[FundTracker]   Carried Over %: ${totalCarriedOverPct.toFixed(1)}%`);
      log(`[FundTracker]   Remaining %: ${(100 - totalUsedPct).toFixed(1)}%`);
      log(`[FundTracker]   Windows Traded: [${Array.from(existingState.windowsTradedToday).join(', ') || 'none'}]`);
      log(`[FundTracker]   Windows HOLD: [${Array.from(existingState.windowsHoldToday).join(', ') || 'none'}]`);
      log(`[FundTracker] ═══════════════════════════════════════════════════════════════`);
      
    } catch (error: any) {
      const errorMsg = `[FundTracker] ❌ reconstructFromDatabase FAILED for account ${accountId}: ${error.message}`;
      console.error(errorMsg);
      closingLogger?.log?.(errorMsg);
      console.error(error.stack);
    }
  }
  
  /**
   * Update balance (called when fresh balance is fetched at T-30 or T-15)
   * Preserves the original totalAccountBalancePct ratio
   */
  updateBalance(accountId: number, balance: number, totalAccountBalancePct?: number): void {
    const state = this.botStates.get(accountId);
    if (!state) return;
    
    // Calculate current ratio to preserve (or use provided pct)
    // Default to 99% if we can't determine the original ratio
    const pct = totalAccountBalancePct ?? 99;
    state.totalBalance = balance * (pct / 100);
    this.botStates.set(accountId, state);
  }
  
  /**
   * Update position value for a specific window
   * Called by trade poller or at T-30 when fresh positions are fetched
   */
  updateWindowPosition(
    accountId: number, 
    windowCloseTime: string, 
    positionValue: number,
    dealId: string | null
  ): void {
    const state = this.botStates.get(accountId);
    if (!state) return;
    
    if (positionValue > 0 && dealId) {
      state.windowPositionValues.set(windowCloseTime, positionValue);
      state.windowDealIds.set(windowCloseTime, dealId);
    } else {
      state.windowPositionValues.delete(windowCloseTime);
      state.windowDealIds.delete(windowCloseTime);
    }
    
    this.botStates.set(accountId, state);
  }
  
  /**
   * Get current position value for a window
   */
  getWindowPositionValue(accountId: number, windowCloseTime: string): number {
    const state = this.botStates.get(accountId);
    if (!state) return 0;
    return state.windowPositionValues.get(windowCloseTime) || 0;
  }
  
  /**
   * Get current allocation % for a window based on actual position value
   */
  getCurrentAllocationPct(accountId: number, windowCloseTime: string): number {
    const state = this.botStates.get(accountId);
    if (!state || state.totalBalance === 0) return 0;
    
    const positionValue = state.windowPositionValues.get(windowCloseTime) || 0;
    return (positionValue / state.totalBalance) * 100;
  }
  
  /**
   * Get available allocation % for a bot in a specific window
   * 
   * CROSS-WINDOW ALLOCATION LOGIC:
   * 1. If THIS window already traded today → return 0 (don't trade twice)
   * 2. If OTHER windows traded today → give this window 100% of remaining (not just base %)
   * 3. Otherwise: Returns base% + carriedOver%
   * 
   * Example with 2 windows @ 50% each:
   * - Day start: Window 1 has 50%, Window 2 has 50%
   * - After Window 1 trades: usedPct=50%
   * - Window 2's allocation: 100% - 50% = 50% (its full remaining share)
   * 
   * DYNAMIC WINDOW MATCHING:
   * 1. First tries exact closeTime match
   * 2. If no match, determines tradingHoursType from closeTime and matches by type
   * 
   * This handles DST changes, short trading days, etc. where the actual close time
   * differs from what was stored in the strategy config.
   */
  getAvailablePct(accountId: number, windowCloseTime: string, tradingHoursType?: string): number {
    this.clearIfNewDay();
    
    const state = this.botStates.get(accountId);
    if (!state) return 0;
    
    // NORMALIZE time format for consistent lookups
    const normalizedTime = normalizeTimeFormat(windowCloseTime);
    
    // Determine the hoursType for this window
    const hoursType = tradingHoursType || this.inferTradingHoursType(normalizedTime);
    
    // CHECK 1: If this window (or its hoursType) already traded today, return 0
    if (state.windowsTradedToday.has(normalizedTime)) {
      console.log(`[FundTracker] Account ${accountId}: Window ${normalizedTime} already traded today → 0%`);
      return 0;
    }
    if (hoursType && state.windowsTradedToday.has(hoursType)) {
      console.log(`[FundTracker] Account ${accountId}: Window type '${hoursType}' already traded today → 0%`);
      return 0;
    }
    
    // SINGLE-WINDOW DAY: Return 100% allocation
    // On special days (e.g., day after Christmas), only one window fires
    // All DNA strands compete, winner gets full allocation
    if (this.isSingleWindowDayFlag) {
      const maxAllowed = 100 - state.usedPct;
      console.log(`[FundTracker] ⚡ Account ${accountId}: SINGLE-WINDOW DAY → 100% allocation (maxAllowed=${maxAllowed.toFixed(1)}%)`);
      return maxAllowed;
    }
    
    // 1. Try exact closeTime match first (using normalized time)
    let basePct = state.windowAllocations.get(normalizedTime) || 0;
    
    // 2. If no exact match, try matching by tradingHoursType
    if (basePct === 0 && state.hoursTypeAllocations.size > 0 && hoursType) {
      basePct = state.hoursTypeAllocations.get(hoursType) || 0;
      
      if (basePct > 0) {
        const storedCloseTime = state.hoursTypeToCloseTime.get(hoursType);
        console.log(`[FundTracker] ✓ Account ${accountId}: Matched window ${windowCloseTime} to '${hoursType}' type (stored as ${storedCloseTime}) → ${basePct}%`);
      }
    }
    
    // If still no match, log for debugging
    if (basePct === 0 && (state.windowAllocations.size > 0 || state.hoursTypeAllocations.size > 0)) {
      const availableWindows = Array.from(state.windowAllocations.keys());
      const availableTypes = Array.from(state.hoursTypeAllocations.keys());
      console.log(`[FundTracker] ⚠️ Account ${accountId}: No allocation for window ${windowCloseTime}. Available windows: ${availableWindows.join(', ') || 'none'}. Available types: ${availableTypes.join(', ') || 'none'}`);
    }
    
    // CHECK 2: If other windows have already traded, this window gets ALL remaining
    // This ensures Window 2 gets 100% of remaining after Window 1 used its share
    const maxAllowed = 100 - state.usedPct;
    
    if (state.windowsTradedToday.size > 0 && maxAllowed > 0) {
      // Other windows traded - give this window everything remaining
      const availableWithCarryover = basePct + state.carriedOverPct;
      // Take the HIGHER of: (base + carryover) OR (remaining)
      // This handles both HOLD carryover and cross-window allocation
      const finalPct = Math.max(availableWithCarryover, maxAllowed);
      console.log(`[FundTracker] Account ${accountId}: Prior windows traded (used ${state.usedPct.toFixed(1)}%), window ${windowCloseTime} gets ${finalPct.toFixed(1)}% (base=${basePct}%, carryover=${state.carriedOverPct.toFixed(1)}%, maxAllowed=${maxAllowed.toFixed(1)}%)`);
      return Math.min(finalPct, maxAllowed);
    }
    
    // Normal case: no prior trades yet
    const availablePct = basePct + state.carriedOverPct;
    console.log(`[FundTracker] Account ${accountId}: Window ${windowCloseTime} → ${Math.min(availablePct, maxAllowed).toFixed(1)}% (base=${basePct}%, carryover=${state.carriedOverPct.toFixed(1)}%, maxAllowed=${maxAllowed.toFixed(1)}%)`);
    return Math.min(availablePct, maxAllowed);
  }
  
  /**
   * Infer tradingHoursType from a close time string
   * 
   * Handles BOTH old ET-based times and new UTC-based times:
   * 
   * OLD (ET-based, stored before fix):
   * - 16:00 = regular (4pm ET)
   * - 20:00 = extended (8pm ET)
   * 
   * NEW (UTC-based, correct):
   * - 21:00 UTC = regular (4pm ET)
   * - 01:00 UTC = extended (8pm ET)
   * 
   * Key insight: Certain times can only be one format:
   * - 16:00 can only be OLD regular (new regular is 21:00)
   * - 20:00 can only be OLD extended (new extended is 01:00)
   * - 21:00 can only be NEW regular
   * - 01:00 can only be NEW extended
   */
  private inferTradingHoursType(windowCloseTime: string): string | null {
    const [hours] = windowCloseTime.split(':').map(Number);
    
    // OLD format: 16:00 = regular (4pm ET)
    if (hours === 16 || hours === 15 || hours === 17) {
      return 'regular';
    }
    
    // OLD format: 20:00 = extended (8pm ET) - NOTE: NOT 21:00!
    // 19:00-20:00 in old format = extended
    if (hours === 19 || hours === 20) {
      return 'extended';
    }
    
    // NEW format: 21:00-22:00 UTC = regular (4pm ET with DST variance)
    if (hours === 21 || hours === 22) {
      return 'regular';
    }
    
    // NEW format: 00:00-02:00 UTC = extended (8pm ET with DST variance)
    if (hours >= 0 && hours <= 2) {
      return 'extended';
    }
    
    // Early close days (e.g., 18:00) - treat as regular
    if (hours === 18) {
      return 'regular';
    }
    
    return null;
  }
  
  /**
   * Get available funds (in currency) for a bot in a specific window
   * 
   * CRITICAL FIX: Uses originalDailyBalance (from session start) NOT totalBalance (current).
   * This ensures Window 2 gets its full 50% of original balance even if Window 1's
   * position has reduced the "available" balance shown by the API.
   * 
   * Example:
   * - Session starts with $460 balance
   * - Window 1 uses 50% ($230) for TECL → API now shows $230 available
   * - Window 2 should STILL get 50% of $460 = $230 for SOXL
   * - NOT 50% of $230 = $115
   * 
   * @param tradingHoursType - Optional hint for matching by type instead of exact time
   */
  getAvailableFunds(accountId: number, windowCloseTime: string, tradingHoursType?: string): number {
    const availablePct = this.getAvailablePct(accountId, windowCloseTime, tradingHoursType);
    const state = this.botStates.get(accountId);
    if (!state) return 0;
    
    // CRITICAL: Use originalDailyBalance, NOT totalBalance
    // This ensures each window gets its allocation of the ORIGINAL balance
    // even if margin is already tied up in positions from other windows
    const funds = (state.originalDailyBalance * availablePct) / 100;
    
    console.log(`[FundTracker] Account ${accountId}: getAvailableFunds for ${windowCloseTime} = $${funds.toFixed(2)} (${availablePct.toFixed(1)}% of original $${state.originalDailyBalance.toFixed(2)}, current balance $${state.totalBalance.toFixed(2)})`);
    
    return funds;
  }
  
  /**
   * Mark funds as used when a position is opened
   * 
   * CROSS-WINDOW TRACKING:
   * - Records the window as "traded today" 
   * - Also tracks by tradingHoursType for flexible matching
   * - This prevents double-trading and enables correct allocation for subsequent windows
   * 
   * CRITICAL: Uses originalDailyBalance for % calculation to maintain consistent tracking
   * across windows even as current balance changes due to margin usage.
   */
  markUsed(accountId: number, windowCloseTime: string, amountUsed: number): void {
    this.clearIfNewDay();
    
    const state = this.botStates.get(accountId);
    if (!state) return;
    
    // NORMALIZE time format for consistent tracking
    const normalizedTime = normalizeTimeFormat(windowCloseTime);
    
    // CRITICAL: Use originalDailyBalance for percentage, not current totalBalance
    // This ensures usedPct reflects % of ORIGINAL, not % of whatever's left
    const pctUsed = (amountUsed / state.originalDailyBalance) * 100;
    state.usedPct += pctUsed;
    state.carriedOverPct = 0; // Reset carried over after use
    
    // Track this window as having traded today (always use normalized time)
    state.windowsTradedToday.add(normalizedTime);
    
    // Also track by hoursType for flexible matching
    const hoursType = this.inferTradingHoursType(normalizedTime);
    if (hoursType) {
      state.windowsTradedToday.add(hoursType);
    }
    
    console.log(`[FundTracker] Account ${accountId}: Marked window ${normalizedTime} (${hoursType || 'unknown'}) as traded. amountUsed=$${amountUsed.toFixed(2)}, usedPct=${state.usedPct.toFixed(1)}% of original $${state.originalDailyBalance.toFixed(2)}, windowsTraded=${Array.from(state.windowsTradedToday).join(',')}`);
    
    this.botStates.set(accountId, state);
    
    // Also update tradingSessionState (async, non-blocking)
    // NOTE: markWindowTraded needs tradeDetails which we don't have here
    // The actual trade details are updated separately via updateTradeDetails()
  }
  
  /**
   * Update trade details in tradingSessionState after trade execution
   * Called after trade is fired and we have dealReference, etc.
   */
  async updateTradeDetails(
    accountId: number, 
    windowCloseTime: string, 
    details: {
      tradeId: number;
      dealReference?: string;
      dealId?: string;
      marginUsed: number;
      contracts: number;
      entryPrice: number;
    }
  ): Promise<void> {
    try {
      const { tradingSessionState } = await import('../services/trading_session_state');
      await tradingSessionState.markWindowTraded(accountId, windowCloseTime, details);
    } catch (err: any) {
      console.warn(`[FundTracker] updateTradeDetails warning: ${err.message}`);
    }
  }
  
  /**
   * Mark window as HOLD - carry over allocation to next window (if carryOver is enabled)
   * 
   * CARRYOVER LOGIC:
   * - If carryOver=TRUE for this window: Add this window's allocation % to carriedOverPct
   * - If carryOver=FALSE for this window: Do NOT carry over (this window's allocation is lost)
   * 
   * Example with carryOver=TRUE:
   * - Window 1 (21:00): 50% allocation, HOLD → carriedOverPct += 50%
   * - Window 2 (01:00): 50% allocation + 50% carried = 100% available
   * 
   * Example with carryOver=FALSE:
   * - Window 1 (21:00): 50% allocation, HOLD → NO carryover
   * - Window 2 (01:00): Only gets its own 50%
   */
  markHold(accountId: number, windowCloseTime: string): void {
    this.clearIfNewDay();
    
    const state = this.botStates.get(accountId);
    if (!state) return;
    
    // NORMALIZE time format for consistent tracking
    const normalizedTime = normalizeTimeFormat(windowCloseTime);
    
    // Track that this window had a HOLD decision
    state.windowsHoldToday.add(normalizedTime);
    
    // Also track by hoursType for flexible matching
    const hoursType = this.inferTradingHoursType(normalizedTime);
    if (hoursType) {
      state.windowsHoldToday.add(hoursType);
    }
    
    // Check if carryOver is enabled for this window (using normalized time)
    let carryOverEnabled = state.windowCarryOver.get(normalizedTime);
    
    // Fallback to hoursType if no exact match
    if (carryOverEnabled === undefined && hoursType) {
      carryOverEnabled = state.hoursTypeCarryOver.get(hoursType);
    }
    
    // Default to TRUE for backwards compatibility
    if (carryOverEnabled === undefined) {
      carryOverEnabled = true;
    }
    
    const basePct = state.windowAllocations.get(normalizedTime) || 
                    (hoursType ? state.hoursTypeAllocations.get(hoursType) : 0) || 0;
    
    if (carryOverEnabled) {
      state.carriedOverPct += basePct;
      console.log(`[FundTracker] Account ${accountId}: Window ${normalizedTime} HOLD with carryOver=true → carrying ${basePct}% to next window (total carried: ${state.carriedOverPct.toFixed(1)}%)`);
    } else {
      console.log(`[FundTracker] Account ${accountId}: Window ${normalizedTime} HOLD with carryOver=false → ${basePct}% NOT carried (allocation lost)`);
    }
    
    this.botStates.set(accountId, state);
    
    // Also update tradingSessionState (async, non-blocking)
    this.updateWindowDecisionInDb(accountId, windowCloseTime, 'HOLD').catch(err => {
      console.warn(`[FundTracker] updateWindowDecisionInDb warning: ${err.message}`);
    });
  }
  
  /**
   * Update window decision in tradingSessionState database
   * Called after brain decision for HOLD windows
   */
  private async updateWindowDecisionInDb(
    accountId: number, 
    windowCloseTime: string, 
    decision: 'BUY' | 'HOLD',
    details?: { epic?: string; indicator?: string; tradeId?: number }
  ): Promise<void> {
    try {
      const { tradingSessionState } = await import('../services/trading_session_state');
      await tradingSessionState.updateWindowDecision(accountId, windowCloseTime, decision, details);
    } catch (err: any) {
      console.warn(`[FundTracker] updateWindowDecisionInDb error: ${err.message}`);
    }
  }
  
  /**
   * Update BUY decision in tradingSessionState (called externally from brain orchestrator)
   */
  async markBuyDecision(
    accountId: number, 
    windowCloseTime: string, 
    details: { epic: string; indicator: string; indicatorValue?: number; tradeId: number }
  ): Promise<void> {
    await this.updateWindowDecisionInDb(accountId, windowCloseTime, 'BUY', details);
  }
  
  /**
   * Get current state for a bot (for debugging/logging)
   */
  getState(accountId: number): BotFundState | undefined {
    this.clearIfNewDay();
    return this.botStates.get(accountId);
  }
  
  /**
   * Get debug state for logging (simplified view)
   */
  getDebugState(accountId: number): { 
    exists: boolean; 
    effectiveBalance?: number;
    originalDailyBalance?: number;  // NEW: Show original balance for debugging
    windows?: string[]; 
    allocations?: Record<string, number>;
    hoursTypes?: string[];
    hoursTypeAllocations?: Record<string, number>;
    usedPct?: number;
    carriedOverPct?: number;        // NEW: Show carry-over for debugging
    windowsTradedToday?: string[];  // NEW: Show which windows traded
    windowsHoldToday?: string[];    // NEW: Show which windows had HOLD
    sessionId?: string;             // NEW: Show current session ID
  } | null {
    const state = this.botStates.get(accountId);
    if (!state) {
      return { exists: false };
    }
    
    const allocations: Record<string, number> = {};
    state.windowAllocations.forEach((pct, window) => {
      allocations[window] = pct;
    });
    
    const hoursTypeAllocations: Record<string, number> = {};
    state.hoursTypeAllocations.forEach((pct, type) => {
      hoursTypeAllocations[type] = pct;
    });
    
    return {
      exists: true,
      effectiveBalance: state.totalBalance, // Current balance (may be reduced by margin)
      originalDailyBalance: state.originalDailyBalance, // Original balance at session start
      windows: Array.from(state.windowAllocations.keys()),
      allocations,
      hoursTypes: Array.from(state.hoursTypeAllocations.keys()),
      hoursTypeAllocations,
      usedPct: state.usedPct,
      carriedOverPct: state.carriedOverPct,
      windowsTradedToday: Array.from(state.windowsTradedToday),
      windowsHoldToday: Array.from(state.windowsHoldToday),
      sessionId: this.lastClearSession,
    };
  }
  
  /**
   * Calculate if rebalancing is needed for a window with BUY signal
   * 
   * Scenario: Window X has BUY signal but another window Y has excess allocation
   * Solution: Partially close Window Y's position to free funds for Window X
   * 
   * @param accountId - The account ID
   * @param buyWindow - The window that has a BUY signal and needs funds
   * @param currentPrice - Current price of the epic (for contract calculation)
   * @returns RebalanceResult with details on what to rebalance
   */
  calculateRebalanceNeeded(
    accountId: number,
    buyWindow: string,
    currentPrice: number = 1
  ): RebalanceResult {
    const noRebalance: RebalanceResult = {
      needed: false,
      fromWindow: null,
      fromDealId: null,
      currentPct: 0,
      targetPct: 0,
      excessPct: 0,
      excessValue: 0,
      contractsToClose: 0,
    };
    
    const state = this.botStates.get(accountId);
    if (!state) return noRebalance;
    
    // Get target allocation for buyWindow
    const buyWindowTargetPct = state.windowAllocations.get(buyWindow) || 0;
    if (buyWindowTargetPct === 0) return noRebalance;
    
    // Calculate total current allocation across all windows
    let totalCurrentPct = 0;
    state.windowPositionValues.forEach((value) => {
      totalCurrentPct += (value / state.totalBalance) * 100;
    });
    
    // Calculate available % for buyWindow
    const availablePct = this.getAvailablePct(accountId, buyWindow);
    
    // If we have enough available, no rebalancing needed
    if (availablePct >= buyWindowTargetPct) {
      console.log(`[FundTracker] No rebalance needed: available ${availablePct.toFixed(1)}% >= target ${buyWindowTargetPct}%`);
      return noRebalance;
    }
    
    // Find a window with excess allocation
    let bestFromWindow: string | null = null;
    let bestExcessPct = 0;
    let bestFromDealId: string | null = null;
    
    state.windowPositionValues.forEach((value, windowCloseTime) => {
      if (windowCloseTime === buyWindow) return; // Skip the window that needs funds
      
      const currentPctForWindow = (value / state.totalBalance) * 100;
      const targetPctForWindow = state.windowAllocations.get(windowCloseTime) || 0;
      const excessPct = currentPctForWindow - targetPctForWindow;
      
      if (excessPct > bestExcessPct) {
        bestExcessPct = excessPct;
        bestFromWindow = windowCloseTime;
        bestFromDealId = state.windowDealIds.get(windowCloseTime) || null;
      }
    });
    
    if (!bestFromWindow || bestExcessPct <= 0) {
      console.log(`[FundTracker] No window with excess allocation found`);
      return noRebalance;
    }
    
    // Calculate how much to close
    const neededPct = buyWindowTargetPct - availablePct;
    const closeExcessPct = Math.min(bestExcessPct, neededPct);
    const closeExcessValue = (closeExcessPct / 100) * state.totalBalance;
    const contractsToClose = closeExcessValue / currentPrice;
    
    const currentPctForFromWindow = this.getCurrentAllocationPct(accountId, bestFromWindow);
    const targetPctForFromWindow = state.windowAllocations.get(bestFromWindow) || 0;
    
    console.log(`[FundTracker] Rebalance needed:`);
    console.log(`  - buyWindow ${buyWindow} needs ${buyWindowTargetPct}%, has ${availablePct.toFixed(1)}% available`);
    console.log(`  - fromWindow ${bestFromWindow} has ${currentPctForFromWindow.toFixed(1)}%, target ${targetPctForFromWindow}%`);
    console.log(`  - Will close ${closeExcessPct.toFixed(1)}% ($${closeExcessValue.toFixed(2)}, ~${contractsToClose.toFixed(2)} contracts)`);
    
    return {
      needed: true,
      fromWindow: bestFromWindow,
      fromDealId: bestFromDealId,
      currentPct: currentPctForFromWindow,
      targetPct: targetPctForFromWindow,
      excessPct: closeExcessPct,
      excessValue: closeExcessValue,
      contractsToClose,
    };
  }
  
  /**
   * Mark rebalance as complete - update position values after partial close
   * 
   * Note: With partial close via opposite trade, the original dealId remains valid.
   * We only update the position value, not the dealId.
   */
  markRebalanceComplete(
    accountId: number,
    fromWindow: string,
    closedValue: number
  ): void {
    const state = this.botStates.get(accountId);
    if (!state) return;
    
    const currentValue = state.windowPositionValues.get(fromWindow) || 0;
    const newValue = currentValue - closedValue;
    
    if (newValue > 0) {
      // Position still exists, just reduced - dealId stays the same
      state.windowPositionValues.set(fromWindow, newValue);
    } else {
      // Position fully closed
      state.windowPositionValues.delete(fromWindow);
      state.windowDealIds.delete(fromWindow);
    }
    
    this.botStates.set(accountId, state);
    
    console.log(`[FundTracker] Rebalance complete: ${fromWindow} now has $${newValue.toFixed(2)}`);
  }
  
  /**
   * Get all windows with their current allocation status
   */
  getAllWindowAllocations(accountId: number): Array<{
    windowCloseTime: string;
    targetPct: number;
    currentPct: number;
    positionValue: number;
    dealId: string | null;
  }> {
    const state = this.botStates.get(accountId);
    if (!state) return [];
    
    const allocations: Array<{
      windowCloseTime: string;
      targetPct: number;
      currentPct: number;
      positionValue: number;
      dealId: string | null;
    }> = [];
    
    state.windowAllocations.forEach((targetPct, windowCloseTime) => {
      const positionValue = state.windowPositionValues.get(windowCloseTime) || 0;
      const currentPct = state.totalBalance > 0 
        ? (positionValue / state.totalBalance) * 100 
        : 0;
      
      allocations.push({
        windowCloseTime,
        targetPct,
        currentPct,
        positionValue,
        dealId: state.windowDealIds.get(windowCloseTime) || null,
      });
    });
    
    return allocations;
  }
  
  /**
   * Clear all tracking data
   */
  clear(): void {
    this.botStates.clear();
    this.lastClearSession = this.getTradingSessionId();
  }
  
  /**
   * Clear tracking if it's a new trading SESSION (not calendar day!)
   * 
   * CRITICAL FIX: US stock trading sessions span midnight UTC:
   * - Regular hours close: ~21:00 UTC (4pm ET)
   * - Extended hours close: ~01:00 UTC (8pm ET) - this is NEXT calendar day in UTC!
   * 
   * A trading "session" runs from 02:00 UTC Day N to 01:59 UTC Day N+1
   * This keeps the 21:00→01:00 window sequence in the SAME session.
   * 
   * Example:
   * - Dec 30 21:00 UTC (Window 1: TECL) → Session "2025-12-30"
   * - Dec 31 01:00 UTC (Window 2: SOXL) → Session "2025-12-30" (SAME!)
   * - Dec 31 02:00 UTC → New session "2025-12-31" starts
   */
  private clearIfNewDay(): void {
    const currentSession = this.getTradingSessionId();
    if (currentSession !== this.lastClearSession) {
      console.log(`[FundTracker] 🔄 NEW TRADING SESSION: ${this.lastClearSession} → ${currentSession}. Clearing all state.`);
      this.botStates.clear();
      this.lastClearSession = currentSession;
    }
  }
  
  /**
   * Get trading session ID based on session boundaries, NOT calendar date.
   * 
   * Session boundary is 02:00 UTC (after extended hours close + buffer).
   * Everything from 02:00 UTC Day N to 01:59 UTC Day N+1 is session "Day N".
   * 
   * This ensures:
   * - 21:00 UTC Dec 30 and 01:00 UTC Dec 31 are in the SAME session
   * - Session clears at 02:00 UTC Dec 31, ready for Dec 31's trading
   */
  private getTradingSessionId(): string {
    const now = new Date();
    const utcHour = now.getUTCHours();
    
    // If we're before 02:00 UTC, we're still in YESTERDAY's trading session
    // (because extended hours close at 01:00 UTC belongs to previous session)
    if (utcHour < 2) {
      // Subtract one day to get the session date
      const yesterday = new Date(now);
      yesterday.setUTCDate(yesterday.getUTCDate() - 1);
      return yesterday.toISOString().split('T')[0];
    }
    
    // After 02:00 UTC, we're in TODAY's session
    return now.toISOString().split('T')[0];
  }
  
  /**
   * Get the actual Date object for when a window closes in a given session.
   * 
   * Handles overnight windows (e.g., 01:00 UTC belongs to previous day's session
   * but actually occurs on the next calendar day).
   * 
   * @param sessionId - Session date in YYYY-MM-DD format (e.g., "2026-01-05")
   * @param windowCloseTime - Time in HH:MM:SS format (e.g., "21:00:00" or "01:00:00")
   * @returns Date object representing when this window closes
   */
  private getWindowDateTimeForSession(sessionId: string, windowCloseTime: string): Date {
    const [hours, minutes, seconds] = windowCloseTime.split(':').map(Number);
    
    // Parse session date
    const sessionDate = new Date(sessionId + 'T00:00:00.000Z');
    
    // If window is before 02:00 UTC (overnight window like 01:00), 
    // it actually occurs on the NEXT calendar day
    if (hours < 2) {
      // Window is on the next day (e.g., 01:00 UTC Jan 6 for session "2026-01-05")
      sessionDate.setUTCDate(sessionDate.getUTCDate() + 1);
    }
    
    sessionDate.setUTCHours(hours, minutes || 0, seconds || 0, 0);
    return sessionDate;
  }
  
  /**
   * Force start a new session (for testing or manual reset)
   */
  forceNewSession(): void {
    console.log(`[FundTracker] ⚡ Forcing new session start`);
    this.botStates.clear();
    this.lastClearSession = this.getTradingSessionId();
  }
}

// Global singleton instance
export const fundTracker = new FundTracker();
