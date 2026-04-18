/**
 * Trading Session State Service
 * 
 * Manages the tradingSessionState JSON field in the accounts table.
 * This replaces the complex in-memory fund_tracker reconstruction with a persistent,
 * easily-queryable state that survives server restarts.
 * 
 * DESIGN PRINCIPLES:
 * 1. Strategy Type Abstraction - Supports time_based (current), indicator_group (future), etc.
 * 2. Session-based - State is per trading session (date), automatically clears for new sessions
 * 3. Persistent - Stored in DB, survives restarts
 * 4. Atomic Updates - Each update writes complete state to avoid partial updates
 * 
 * USAGE:
 * - At T-5m: initializeSession() creates fresh state from strategy config
 * - After brain: updateWindowDecision() records BUY/HOLD
 * - At T-15s: getAvailableAllocation() returns funds for position sizing
 * - After trade: markWindowTraded() updates with trade details
 */

import { getDb } from '../db';
import { accounts } from '../../drizzle/schema';
import { eq } from 'drizzle-orm';

// ═══════════════════════════════════════════════════════════════════════════════
// TIME NORMALIZATION UTILITY
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Normalize time string to HH:MM:SS format for consistent key lookups
 * Handles various input formats:
 * - "21:00" -> "21:00:00"
 * - "1:00" -> "01:00:00"
 * - "01:00" -> "01:00:00"
 * - "21:00:00" -> "21:00:00"
 */
function normalizeTimeFormat(time: string | null | undefined): string {
  if (!time) return '21:00:00';
  
  const parts = time.split(':');
  const hours = parts[0].padStart(2, '0');
  const minutes = (parts[1] || '00').padStart(2, '0');
  const seconds = (parts[2] || '00').padStart(2, '0');
  
  return `${hours}:${minutes}:${seconds}`;
}

// ═══════════════════════════════════════════════════════════════════════════════
// TYPE DEFINITIONS
// ═══════════════════════════════════════════════════════════════════════════════

export type StrategyType = 'time_based' | 'indicator_group' | 'discovery' | 'custom';

export type WindowStatus = 'pending' | 'hold' | 'traded' | 'skipped' | 'failed';

export interface WindowState {
  // Configuration (snapshot from strategy at session start)
  allocationPct: number;
  carryOverEnabled: boolean;
  tradingHoursType: 'regular' | 'extended' | 'crypto' | 'custom';
  epics: string[];
  
  // Runtime state
  status: WindowStatus;
  decision: 'BUY' | 'HOLD' | null;
  winningEpic: string | null;
  winningIndicator: string | null;
  indicatorValue: number | null;
  
  // Trade execution details
  tradeId: number | null;
  dealReference: string | null;
  dealId: string | null;
  marginUsed: number;
  contracts: number;
  entryPrice: number | null;
  
  // Timestamps
  decisionTime: string | null;
  executionTime: string | null;
}

export interface IndicatorGroupState {
  indicators: string[];
  allocationPct: number;
  status: WindowStatus;
  winnerIndicator: string | null;
  winningEpic: string | null;
  marginUsed: number;
  tradeId: number | null;
}

export interface TradingSessionState {
  strategyType: StrategyType;
  sessionId: string;
  lastUpdated: string;
  totalAccountBalancePct: number;
  originalDailyBalance: number;
  
  // Time-based windows
  windows?: { [windowCloseTime: string]: WindowState };
  
  // Future: Indicator groups
  indicatorGroups?: { [groupId: string]: IndicatorGroupState };
  
  // Aggregated metrics
  usedPct: number;
  carriedOverPct: number;
  windowsTraded: string[];
  windowsHold: string[];
}

// ═══════════════════════════════════════════════════════════════════════════════
// HELPER: Get current trading session ID (YYYY-MM-DD)
// Session starts at 02:00 UTC - so trades at 01:00 UTC belong to previous day's session
// ═══════════════════════════════════════════════════════════════════════════════

export function getTradingSessionId(date?: Date): string {
  const now = date || new Date();
  const utcHour = now.getUTCHours();
  
  // If before 02:00 UTC, session belongs to previous calendar day
  if (utcHour < 2) {
    const yesterday = new Date(now);
    yesterday.setUTCDate(yesterday.getUTCDate() - 1);
    return yesterday.toISOString().split('T')[0];
  }
  
  return now.toISOString().split('T')[0];
}

// ═══════════════════════════════════════════════════════════════════════════════
// HELPER: Infer trading hours type from window close time
// ═══════════════════════════════════════════════════════════════════════════════

function inferTradingHoursType(windowCloseTime: string): 'regular' | 'extended' | 'crypto' | 'custom' {
  const [hours] = windowCloseTime.split(':').map(Number);
  
  // Regular hours: 21:00-22:00 UTC (4-5pm ET)
  if (hours === 21 || hours === 22 || hours === 16 || hours === 17 || hours === 18) {
    return 'regular';
  }
  
  // Extended hours: 00:00-02:00 UTC (8-9pm ET previous day)
  if (hours >= 0 && hours <= 2 || hours === 19 || hours === 20) {
    return 'extended';
  }
  
  // Crypto: 22:00 UTC (if crypto market)
  // Default to custom for unknown times
  return 'custom';
}

// ═══════════════════════════════════════════════════════════════════════════════
// CORE SERVICE CLASS
// ═══════════════════════════════════════════════════════════════════════════════

class TradingSessionStateService {
  
  // In-memory cache for fast access during trading windows
  private cache: Map<number, TradingSessionState> = new Map();
  
  /**
   * Get session state for an account (from cache or DB)
   */
  async getSessionState(accountId: number): Promise<TradingSessionState | null> {
    // Check cache first
    const cached = this.cache.get(accountId);
    const currentSessionId = getTradingSessionId();
    
    if (cached && cached.sessionId === currentSessionId) {
      return cached;
    }
    
    // Load from database
    const db = await getDb();
    if (!db) return null;
    
    const [account] = await db
      .select({ tradingSessionState: accounts.tradingSessionState })
      .from(accounts)
      .where(eq(accounts.id, accountId))
      .limit(1);
    
    if (!account?.tradingSessionState) {
      return null;
    }
    
    const state = account.tradingSessionState as TradingSessionState;
    
    // Check if state is for current session
    if (state.sessionId !== currentSessionId) {
      console.log(`[TradingSessionState] Account ${accountId}: Session ${state.sessionId} is stale (current: ${currentSessionId}) - returning null`);
      return null;
    }
    
    // Update cache
    this.cache.set(accountId, state);
    
    return state;
  }
  
  /**
   * Initialize session state for a new trading session
   * Called at T-5m (quiet period start) - MAY BE CALLED MULTIPLE TIMES PER SESSION
   * 
   * CRITICAL: T-5m triggers for EACH window (21:00 and 01:00), so we must:
   * 1. If same session exists: MERGE new window config, don't overwrite
   * 2. If new session: Create fresh state
   * 3. Preserve Window 1's state when Window 2's T-5m triggers
   * 
   * @param accountId - Account ID
   * @param balance - Current account balance
   * @param windowConfig - Strategy's window configuration
   * @param strategyType - Type of strategy (default: time_based)
   */
  async initializeSession(
    accountId: number,
    balance: number,
    windowConfig: {
      windows?: Array<{
        closeTime: string;
        allocationPct: number;
        carryOver?: boolean;
        tradingHoursType?: string;
        epics?: string[];
      }>;
      totalAccountBalancePct?: number;
    },
    strategyType: StrategyType = 'time_based'
  ): Promise<TradingSessionState> {
    const sessionId = getTradingSessionId();
    const now = new Date().toISOString();
    
    // Check if we already have state for this session
    const existingState = await this.getSessionState(accountId);
    
    // ═══════════════════════════════════════════════════════════════════════════
    // CASE 1: Same session exists - MERGE windows, don't overwrite
    // This handles Window 2's T-5m not overwriting Window 1's state
    // ═══════════════════════════════════════════════════════════════════════════
    if (existingState && existingState.sessionId === sessionId) {
      console.log(`[TradingSessionState] Account ${accountId}: Session ${sessionId} exists - MERGING (not overwriting)`);
      
      // ═══════════════════════════════════════════════════════════════════════════
      // FIX: Update originalDailyBalance if stale OR if this is first window of session
      // ═══════════════════════════════════════════════════════════════════════════
      const totalAccountBalancePct = windowConfig.totalAccountBalancePct || 99;
      const newPool = balance * (totalAccountBalancePct / 100);
      const poolDifference = Math.abs(newPool - existingState.originalDailyBalance);
      const poolDifferencePct = existingState.originalDailyBalance > 0 
        ? (poolDifference / existingState.originalDailyBalance) * 100
        : 100;
      
      // Check if this is the FIRST window of the session (no trades/holds yet)
      const isFirstWindow = existingState.usedPct === 0 
                         && existingState.windowsTraded.length === 0
                         && existingState.windowsHold.length === 0;
      
      // Update pool if:
      // A) First window of session (always use fresh balance for first window)
      // B) Pool changed significantly (>20%) and no trades yet
      if (isFirstWindow && poolDifferencePct > 5) {
        console.log(`[TradingSessionState] Account ${accountId}: FIRST WINDOW - updating pool from ${existingState.originalDailyBalance.toFixed(2)} → ${newPool.toFixed(2)} (${poolDifferencePct.toFixed(1)}% change)`);
        existingState.originalDailyBalance = newPool;
        existingState.totalAccountBalancePct = totalAccountBalancePct;
        existingState.lastUpdated = now;
        await this.saveSessionState(accountId, existingState);
      } else if (!isFirstWindow && poolDifferencePct > 20) {
        console.warn(`[TradingSessionState] Account ${accountId}: ⚠️ Pool changed ${poolDifferencePct.toFixed(1)}% mid-session but trades exist - keeping original pool ${existingState.originalDailyBalance.toFixed(2)}`);
      }
      
      // Check if any NEW windows need to be added (shouldn't normally happen, but handle it)
      let windowsAdded = 0;
      if (strategyType === 'time_based' && windowConfig.windows) {
        for (const win of windowConfig.windows) {
          const closeTime = normalizeTimeFormat(win.closeTime);
          
          // Only add if window doesn't exist yet
          if (!existingState.windows?.[closeTime]) {
            if (!existingState.windows) existingState.windows = {};
            existingState.windows[closeTime] = {
              allocationPct: win.allocationPct || 50,
              carryOverEnabled: win.carryOver !== false,
              tradingHoursType: (win.tradingHoursType as any) || inferTradingHoursType(closeTime),
              epics: win.epics || [],
              status: 'pending',
              decision: null,
              winningEpic: null,
              winningIndicator: null,
              indicatorValue: null,
              tradeId: null,
              dealReference: null,
              dealId: null,
              marginUsed: 0,
              contracts: 0,
              entryPrice: null,
              decisionTime: null,
              executionTime: null,
            };
            windowsAdded++;
          }
        }
      }
      
      if (windowsAdded > 0) {
        existingState.lastUpdated = now;
        await this.saveSessionState(accountId, existingState);
        console.log(`[TradingSessionState] Account ${accountId}: Added ${windowsAdded} new windows to existing session`);
      } else {
        console.log(`[TradingSessionState] Account ${accountId}: All windows already exist - no changes needed`);
      }
      
      return existingState;
    }
    
    // ═══════════════════════════════════════════════════════════════════════════
    // CASE 2: New session - Create fresh state
    // ═══════════════════════════════════════════════════════════════════════════
    console.log(`[TradingSessionState] Account ${accountId}: Creating NEW session ${sessionId}`);
    
    const totalAccountBalancePct = windowConfig.totalAccountBalancePct || 99;
    const originalDailyBalance = balance * (totalAccountBalancePct / 100);
    
    // Build windows object from config
    const windows: { [key: string]: WindowState } = {};
    
    if (strategyType === 'time_based' && windowConfig.windows) {
      for (const win of windowConfig.windows) {
        // Normalize close time format
        const closeTime = normalizeTimeFormat(win.closeTime);
        
        windows[closeTime] = {
          // Configuration snapshot
          allocationPct: win.allocationPct || 50,
          carryOverEnabled: win.carryOver !== false, // Default true
          tradingHoursType: (win.tradingHoursType as any) || inferTradingHoursType(closeTime),
          epics: win.epics || [],
          
          // Runtime state (pending)
          status: 'pending',
          decision: null,
          winningEpic: null,
          winningIndicator: null,
          indicatorValue: null,
          
          // Trade execution (empty)
          tradeId: null,
          dealReference: null,
          dealId: null,
          marginUsed: 0,
          contracts: 0,
          entryPrice: null,
          
          // Timestamps
          decisionTime: null,
          executionTime: null,
        };
      }
    }
    
    const state: TradingSessionState = {
      strategyType,
      sessionId,
      lastUpdated: now,
      totalAccountBalancePct,
      originalDailyBalance,
      windows,
      usedPct: 0,
      carriedOverPct: 0,
      windowsTraded: [],
      windowsHold: [],
    };
    
    // Save to database
    await this.saveSessionState(accountId, state);
    
    console.log(`[TradingSessionState] Account ${accountId}: Initialized session ${sessionId} with ${Object.keys(windows).length} windows, original balance $${originalDailyBalance.toFixed(2)}`);
    
    return state;
  }
  
  /**
   * Update window decision after brain calculation
   * Called at T-60s after brain returns BUY or HOLD
   */
  async updateWindowDecision(
    accountId: number,
    windowCloseTime: string,
    decision: 'BUY' | 'HOLD',
    details?: {
      epic?: string;
      indicator?: string;
      indicatorValue?: number;
      tradeId?: number;
    }
  ): Promise<void> {
    const state = await this.getSessionState(accountId);
    if (!state) {
      console.error(`[TradingSessionState] Account ${accountId}: No session state found for updateWindowDecision`);
      return;
    }
    
    // Normalize close time for consistent key lookups
    const closeTime = normalizeTimeFormat(windowCloseTime);
    
    if (!state.windows?.[closeTime]) {
      console.error(`[TradingSessionState] Account ${accountId}: Window ${closeTime} not found in session state`);
      return;
    }
    
    const now = new Date().toISOString();
    const window = state.windows[closeTime];
    
    window.decision = decision;
    window.decisionTime = now;
    
    if (decision === 'BUY') {
      window.status = 'pending'; // Will become 'traded' after execution
      window.winningEpic = details?.epic || null;
      window.winningIndicator = details?.indicator || null;
      window.indicatorValue = details?.indicatorValue || null;
      window.tradeId = details?.tradeId || null;
    } else {
      // HOLD decision
      window.status = 'hold';
      
      // If carryOver enabled, add allocation to carriedOverPct
      if (window.carryOverEnabled) {
        state.carriedOverPct += window.allocationPct;
        console.log(`[TradingSessionState] Account ${accountId}: Window ${closeTime} HOLD with carryOver → carriedOverPct now ${state.carriedOverPct}%`);
      }
      
      state.windowsHold.push(closeTime);
    }
    
    state.lastUpdated = now;
    
    await this.saveSessionState(accountId, state);
    
    console.log(`[TradingSessionState] Account ${accountId}: Window ${closeTime} decision=${decision}${details?.epic ? ` epic=${details.epic}` : ''}`);
  }
  
  /**
   * Mark window as traded after successful execution
   * Called after trade is fired at T-15s
   */
  async markWindowTraded(
    accountId: number,
    windowCloseTime: string,
    tradeDetails: {
      tradeId: number;
      dealReference?: string;
      dealId?: string;
      marginUsed: number;
      contracts: number;
      entryPrice: number;
    }
  ): Promise<void> {
    const state = await this.getSessionState(accountId);
    if (!state) return;
    
    const closeTime = normalizeTimeFormat(windowCloseTime);
    
    if (!state.windows?.[closeTime]) return;
    
    const now = new Date().toISOString();
    const window = state.windows[closeTime];
    
    window.status = 'traded';
    window.executionTime = now;
    window.tradeId = tradeDetails.tradeId;
    window.dealReference = tradeDetails.dealReference || null;
    window.dealId = tradeDetails.dealId || null;
    window.marginUsed = tradeDetails.marginUsed;
    window.contracts = tradeDetails.contracts;
    window.entryPrice = tradeDetails.entryPrice;
    
    // Update aggregated metrics
    const usedPct = (tradeDetails.marginUsed / state.originalDailyBalance) * 100;
    state.usedPct += usedPct;
    state.carriedOverPct = 0; // Reset carryOver after trade
    state.windowsTraded.push(closeTime);
    state.lastUpdated = now;
    
    await this.saveSessionState(accountId, state);
    
    console.log(`[TradingSessionState] Account ${accountId}: Window ${closeTime} TRADED - margin $${tradeDetails.marginUsed.toFixed(2)} (${usedPct.toFixed(1)}% of original)`);
  }
  
  /**
   * Mark window as failed
   */
  async markWindowFailed(accountId: number, windowCloseTime: string, reason?: string): Promise<void> {
    const state = await this.getSessionState(accountId);
    if (!state) return;
    
    const closeTime = normalizeTimeFormat(windowCloseTime);
    
    if (!state.windows?.[closeTime]) return;
    
    state.windows[closeTime].status = 'failed';
    state.lastUpdated = new Date().toISOString();
    
    await this.saveSessionState(accountId, state);
    
    console.log(`[TradingSessionState] Account ${accountId}: Window ${closeTime} FAILED${reason ? `: ${reason}` : ''}`);
  }
  
  /**
   * Update dealId after reconciliation
   */
  async updateDealId(accountId: number, windowCloseTime: string, dealId: string): Promise<void> {
    const state = await this.getSessionState(accountId);
    if (!state) return;
    
    const closeTime = normalizeTimeFormat(windowCloseTime);
    
    if (!state.windows?.[closeTime]) return;
    
    state.windows[closeTime].dealId = dealId;
    state.lastUpdated = new Date().toISOString();
    
    await this.saveSessionState(accountId, state);
  }
  
  /**
   * Get available allocation for a window
   * Returns { pct, funds } for position sizing
   * 
   * Logic:
   * 1. If window already traded → 0%
   * 2. If prior windows had HOLD with carryOver → base% + carriedOver%
   * 3. Otherwise → base%
   */
  async getAvailableAllocation(
    accountId: number,
    windowCloseTime: string,
    currentBalance?: number
  ): Promise<{ pct: number; funds: number }> {
    const state = await this.getSessionState(accountId);
    if (!state) {
      console.warn(`[TradingSessionState] Account ${accountId}: No session state - returning 0`);
      return { pct: 0, funds: 0 };
    }
    
    const closeTime = normalizeTimeFormat(windowCloseTime);
    
    const window = state.windows?.[closeTime];
    if (!window) {
      console.warn(`[TradingSessionState] Account ${accountId}: Window ${closeTime} not found - returning 0`);
      return { pct: 0, funds: 0 };
    }
    
    // Already traded?
    if (window.status === 'traded') {
      console.log(`[TradingSessionState] Account ${accountId}: Window ${closeTime} already traded → 0%`);
      return { pct: 0, funds: 0 };
    }
    
    // Calculate available %
    const basePct = window.allocationPct;
    const carriedPct = state.carriedOverPct;
    const maxAllowed = 100 - state.usedPct;
    
    // If other windows traded, this window gets remaining
    const availablePct = Math.min(basePct + carriedPct, maxAllowed);
    
    // Calculate funds using ORIGINAL daily balance (not current)
    const funds = (state.originalDailyBalance * availablePct) / 100;
    
    console.log(`[TradingSessionState] Account ${accountId}: Window ${closeTime} available ${availablePct.toFixed(1)}% = $${funds.toFixed(2)} (base=${basePct}%, carried=${carriedPct.toFixed(1)}%, used=${state.usedPct.toFixed(1)}%)`);
    
    return { pct: availablePct, funds };
  }
  
  /**
   * Check if a window has already been processed (not pending)
   */
  async isWindowProcessed(accountId: number, windowCloseTime: string): Promise<boolean> {
    const state = await this.getSessionState(accountId);
    if (!state) return false;
    
    const closeTime = normalizeTimeFormat(windowCloseTime);
    
    const window = state.windows?.[closeTime];
    return window ? window.status !== 'pending' : false;
  }
  
  // ═══════════════════════════════════════════════════════════════════════════════
  // SIMPLE ALLOCATION CALCULATION FOR T-15
  // ═══════════════════════════════════════════════════════════════════════════════
  // This is the simple version - just looks at prior windows to calculate %
  // Called at T-15 with fresh balance from Capital.com
  // ═══════════════════════════════════════════════════════════════════════════════
  
  /**
   * Calculate allocation % for a window at T-15
   * 
   * SIMPLE LOGIC:
   * 1. Look at all PRIOR windows in this session
   * 2. If prior window HOLD + carryOver=true → add its % to this window
   * 3. If prior window TRADED → don't add (it used its allocation)
   * 4. Return: this window's base% + any carried%
   * 
   * Example (2 windows, 50% each):
   * - Window 1 HOLD + carryOver → Window 2 gets 50% + 50% = 100%
   * - Window 1 TRADED → Window 2 gets only its 50%
   * 
   * @param accountId - Account ID
   * @param windowCloseTime - This window's close time (e.g., "01:00:00")
   * @param freshBalance - Fresh balance from Capital.com GET /accounts
   * @param totalAccountBalancePct - Strategy's total % to use (e.g., 99)
   */
  async calculateAllocationAtT15(
    accountId: number,
    windowCloseTime: string,
    freshBalance: number,
    totalAccountBalancePct: number = 99
  ): Promise<{ allocationPct: number; availableFunds: number; breakdown: string }> {
    const state = await this.getSessionState(accountId);
    
    const closeTime = normalizeTimeFormat(windowCloseTime);
    
    // Default: use full balance if no state
    if (!state || !state.windows) {
      const funds = freshBalance * (totalAccountBalancePct / 100);
      return { 
        allocationPct: 100, 
        availableFunds: funds,
        breakdown: 'No session state - using 100%'
      };
    }
    
    const thisWindow = state.windows[closeTime];
    if (!thisWindow) {
      const funds = freshBalance * (totalAccountBalancePct / 100);
      return { 
        allocationPct: 100, 
        availableFunds: funds,
        breakdown: `Window ${closeTime} not found - using 100%`
      };
    }
    
    // Start with this window's base allocation
    let allocationPct = thisWindow.allocationPct;
    const breakdownParts: string[] = [`base=${thisWindow.allocationPct}%`];
    
    // Check PRIOR windows (windows that come before this one chronologically)
    // Sort windows CHRONOLOGICALLY (not alphabetically!)
    // Trading session: 02:00 UTC Day N → 01:59 UTC Day N+1
    // Times 00:00-01:59 come AFTER times 02:00-23:59
    const windowTimes = Object.keys(state.windows).sort((a, b) => {
      const [hoursA] = a.split(':').map(Number);
      const [hoursB] = b.split(':').map(Number);
      
      // Windows after midnight (00:00-01:59) are LATER in session
      // Add 24 hours to sort them after 23:59
      const adjustedA = hoursA < 2 ? hoursA + 24 : hoursA;
      const adjustedB = hoursB < 2 ? hoursB + 24 : hoursB;
      
      return adjustedA - adjustedB;
    });
    
    const thisWindowIndex = windowTimes.indexOf(closeTime);
    const isLastWindow = thisWindowIndex === windowTimes.length - 1;
    
    for (let i = 0; i < thisWindowIndex; i++) {
      const priorWindowTime = windowTimes[i];
      const priorWindow = state.windows[priorWindowTime];
      
      if (!priorWindow) continue;
      
      // Check if prior window had HOLD with carryOver
      if (priorWindow.status === 'hold' && priorWindow.carryOverEnabled) {
        allocationPct += priorWindow.allocationPct;
        breakdownParts.push(`+${priorWindow.allocationPct}% from ${priorWindowTime} (HOLD+carryOver)`);
      } else if (priorWindow.status === 'traded') {
        breakdownParts.push(`${priorWindowTime} traded (no carryOver)`);
      } else if (priorWindow.status === 'hold' && !priorWindow.carryOverEnabled) {
        breakdownParts.push(`${priorWindowTime} HOLD but carryOver=false`);
      }
    }
    
    // Cap at 100%
    if (allocationPct > 100) {
      allocationPct = 100;
      breakdownParts.push('(capped at 100%)');
    }
    
    // Calculate actual funds
    let availableFunds: number;
    
    if (isLastWindow) {
      // Last window: use all remaining (preserve reserve)
      const reservePct = 100 - totalAccountBalancePct;
      const reserveAmount = freshBalance * (reservePct / 100);
      availableFunds = freshBalance - reserveAmount;
      breakdownParts.push(`LAST WINDOW: all remaining (preserve ${reservePct}% reserve)`);
    } else {
      // Non-last window: use % of original pool
      availableFunds = state.originalDailyBalance * (allocationPct / 100);
      breakdownParts.push(`${allocationPct}% of pool ($${state.originalDailyBalance.toFixed(2)})`);
    }
    
    console.log(`[TradingSessionState] Account ${accountId} T-15 allocation:`);
    console.log(`  Fresh balance: $${freshBalance.toFixed(2)}`);
    console.log(`  Total account %: ${totalAccountBalancePct}%`);
    console.log(`  Window allocation: ${allocationPct}%`);
    console.log(`  Available funds: $${availableFunds.toFixed(2)}`);
    console.log(`  Breakdown: ${breakdownParts.join(', ')}`);
    
    return {
      allocationPct,
      availableFunds,
      breakdown: breakdownParts.join(', ')
    };
  }
  
  /**
   * Get debug state for logging
   */
  async getDebugState(accountId: number): Promise<any> {
    const state = await this.getSessionState(accountId);
    if (!state) return { exists: false };
    
    return {
      exists: true,
      sessionId: state.sessionId,
      strategyType: state.strategyType,
      originalDailyBalance: state.originalDailyBalance,
      usedPct: state.usedPct,
      carriedOverPct: state.carriedOverPct,
      windowCount: state.windows ? Object.keys(state.windows).length : 0,
      windows: state.windows ? Object.fromEntries(
        Object.entries(state.windows).map(([k, v]) => [k, {
          status: v.status,
          decision: v.decision,
          allocationPct: v.allocationPct,
          carryOverEnabled: v.carryOverEnabled,
          marginUsed: v.marginUsed,
        }])
      ) : {},
      windowsTraded: state.windowsTraded,
      windowsHold: state.windowsHold,
    };
  }
  
  /**
   * Clear session state (for testing or manual reset)
   */
  async clearSessionState(accountId: number): Promise<void> {
    this.cache.delete(accountId);
    
    const db = await getDb();
    if (!db) return;
    
    await db
      .update(accounts)
      .set({ tradingSessionState: null })
      .where(eq(accounts.id, accountId));
    
    console.log(`[TradingSessionState] Account ${accountId}: Session state cleared`);
  }
  
  /**
   * Save session state to database
   */
  private async saveSessionState(accountId: number, state: TradingSessionState): Promise<void> {
    // Update cache
    this.cache.set(accountId, state);
    
    // Save to database
    const db = await getDb();
    if (!db) {
      console.error(`[TradingSessionState] Account ${accountId}: No DB connection - state only in cache!`);
      return;
    }
    
    await db
      .update(accounts)
      .set({ 
        tradingSessionState: state as any,
        updatedAt: new Date(),
      })
      .where(eq(accounts.id, accountId));
  }
  
  /**
   * Clear cache (useful for testing)
   */
  clearCache(): void {
    this.cache.clear();
  }
  
  // ═══════════════════════════════════════════════════════════════════════════════
  // CRASH RECOVERY (NOT USED - kept for potential future use)
  // ═══════════════════════════════════════════════════════════════════════════════
  // Decision: Position closing happens at T-60 for THAT window's epic(s) only.
  // Crash recovery is NOT needed at T-5m because:
  // - Each window closes its own epic's positions at T-60
  // - Window 1 (21:00) closes TECL, Window 2 (01:00) closes SOXL
  // - Other epics are left alone (they belong to other windows)
  // ═══════════════════════════════════════════════════════════════════════════════
  
  /**
   * @deprecated NOT USED - Position closing happens at T-60 per window/epic
   * Check for and close orphaned positions from previous sessions
   * Called at T-5m or server startup
   * 
   * CRITICAL: This queries Capital.com GET /positions to find ANY open positions
   * regardless of how old they are or what session they're from.
   * 
   * @param account - Account object with accountType, capitalAccountId, etc.
   * @param strategyEpics - List of epics this account's strategy trades
   * @param client - Capital.com API client (already connected)
   * @returns Array of positions that were closed
   */
  async closeOrphanedPositions(
    account: { 
      id: number; 
      accountType: 'demo' | 'live'; 
      capitalAccountId: string;
      accountName?: string;
    },
    strategyEpics: string[],
    client: any
  ): Promise<{ dealId: string; epic: string; closed: boolean }[]> {
    const results: { dealId: string; epic: string; closed: boolean }[] = [];
    
    try {
      console.log(`[TradingSessionState] 🔍 CRASH RECOVERY: Checking for orphaned positions on account ${account.accountName || account.id}`);
      console.log(`[TradingSessionState]   Strategy epics: ${strategyEpics.join(', ')}`);
      
      // Switch to this account
      await client.switchAccount(account.capitalAccountId);
      
      // Query Capital.com for ALL open positions
      const positions = await client.getPositions();
      
      if (!positions || positions.length === 0) {
        console.log(`[TradingSessionState]   ✅ No open positions found - nothing to recover`);
        return results;
      }
      
      console.log(`[TradingSessionState]   Found ${positions.length} open positions on Capital.com`);
      
      // Check each position against strategy epics
      for (const position of positions) {
        const dealId = position.position?.dealId || position.dealId;
        const epic = position.market?.epic || position.epic;
        const size = position.position?.size || position.size;
        const direction = position.position?.direction || position.direction;
        const openDate = position.position?.createdDate || position.createdDate;
        
        if (!dealId || !epic) {
          console.log(`[TradingSessionState]   ⚠️ Position missing dealId/epic, skipping`);
          continue;
        }
        
        // Check if this epic is in the strategy
        const isStrategyEpic = strategyEpics.includes(epic);
        
        if (!isStrategyEpic) {
          console.log(`[TradingSessionState]   ℹ️ Position ${dealId} (${epic}) is NOT a strategy epic - leaving open`);
          continue;
        }
        
        // This is a strategy epic - it should be closed before new session trades
        console.log(`[TradingSessionState]   🎯 ORPHANED POSITION: ${dealId} ${epic} ${direction} size=${size} opened=${openDate}`);
        
        try {
          // Close the position
          const closeSuccess = await client.closePosition(dealId);
          
          if (closeSuccess) {
            console.log(`[TradingSessionState]   ✅ Closed orphaned position ${dealId} (${epic})`);
            results.push({ dealId, epic, closed: true });
            
            // Try to update database if we have a matching record
            try {
              const db = await getDb();
              if (db) {
                const { actualTrades } = await import('../../drizzle/schema');
                const { eq, and } = await import('drizzle-orm');
                
                await db
                  .update(actualTrades)
                  .set({
                    closedAt: new Date(),
                    status: 'closed',
                    closeReason: 'crash_recovery',
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
              console.warn(`[TradingSessionState]   ⚠️ DB update warning: ${dbErr.message}`);
            }
          } else {
            console.log(`[TradingSessionState]   ❌ Failed to close position ${dealId}`);
            results.push({ dealId, epic, closed: false });
          }
        } catch (closeErr: any) {
          console.error(`[TradingSessionState]   ❌ Error closing position ${dealId}: ${closeErr.message}`);
          results.push({ dealId, epic, closed: false });
        }
      }
      
      const closedCount = results.filter(r => r.closed).length;
      console.log(`[TradingSessionState] 📊 CRASH RECOVERY COMPLETE: ${closedCount}/${results.length} positions closed`);
      
      return results;
      
    } catch (error: any) {
      console.error(`[TradingSessionState] ❌ CRASH RECOVERY FAILED for account ${account.id}: ${error.message}`);
      return results;
    }
  }
  
  /**
   * Get epics from a strategy's DNA strands
   * Helper for crash recovery
   */
  getStrategyEpics(strategy: { dnaStrands?: any[]; windowConfig?: any }): string[] {
    const epics = new Set<string>();
    
    // From DNA strands
    if (strategy.dnaStrands && Array.isArray(strategy.dnaStrands)) {
      for (const strand of strategy.dnaStrands) {
        if (strand.epic) epics.add(strand.epic);
      }
    }
    
    // From window config
    if (strategy.windowConfig?.windows && Array.isArray(strategy.windowConfig.windows)) {
      for (const win of strategy.windowConfig.windows) {
        if (win.epics && Array.isArray(win.epics)) {
          win.epics.forEach((e: string) => epics.add(e));
        }
      }
    }
    
    return Array.from(epics);
  }
}

// Export singleton instance
export const tradingSessionState = new TradingSessionStateService();

// Export class for testing
export { TradingSessionStateService };
