import { getSetting } from '../db';
import { orchestrationLogger, apiTimingTracker, apiStatsCollector } from './logger';
import { executeBrainCalculations, BrainDecision } from './brain_orchestrator';
import { executeCloseTrades } from './close_orchestrator';
import { executeOpenTrades } from './open_orchestrator';
import { sessionManager } from './session_manager';
import { apiQueue } from './api_queue';
import { tradePoller } from './trade_poller';
import { candleDataService, candleWebSocket } from '../services/candle_data_service';
import { candleTriggerSystem } from './candle_trigger';
import { 
  initializeWindowAccumulators, 
  runBrainForTimingMode, 
  runFinalConflictResolution,
  runBrainWithImmediateProcessing 
} from './dna_brain_calculator';
import { dnaBrainAccumulator } from './dna_brain_accumulator';
import { createClosingLog, ClosingLogger } from '../logging/file_logger';
import { fundTracker } from './fund_tracker';
import { connectionManager } from './connection_manager';
import { tradeQueue, type PriorityOrderType } from './trade_queue';

/**
 * Convert a UTC Date to a UTC time string (HH:MM:SS format)
 * IMPORTANT: Use this instead of toTimeString() which returns local time
 * 
 * @param date - Date object (should be in UTC)
 * @returns Time string in HH:MM:SS format (UTC)
 */
function getUTCTimeString(date: Date): string {
  const hours = date.getUTCHours().toString().padStart(2, '0');
  const minutes = date.getUTCMinutes().toString().padStart(2, '0');
  const seconds = date.getUTCSeconds().toString().padStart(2, '0');
  return `${hours}:${minutes}:${seconds}`;
}

/**
 * Global Orchestration Timer
 * 
 * Single master clock that orchestrates ALL bots across ALL accounts.
 * Detects execution windows and triggers appropriate actions.
 */

interface TimerSettings {
  brainWindowSeconds: number;
  closeWindowSeconds: number;
  openWindowSeconds: number;
  dailyRefreshTime: string; // HH:MM:SS format
  quietPeriodStartSeconds: number; // Seconds before close to start quiet period (default: 300 = 5 min)
  quietPeriodEndSeconds: number; // Seconds after close to end quiet period (default: -60 = 1 min after)
  // When enabled, mirror orchestration logs (API calls, decisions, DB ops) into the per-window closing log file.
  traceModeEnabled: boolean;
}

interface ExecutionWindow {
  type: 'brain' | 'close' | 'open' | 'quiet_start' | 'quiet_end' | 't60_capture';
  triggerTime: Date;
  marketCloseTime: Date;
}

class OrchestrationTimer {
  private static instance: OrchestrationTimer;
  private intervalId: NodeJS.Timeout | null = null;
  private isRunning: boolean = false;
  private settings: TimerSettings | null = null;
  
  // Track which windows have been triggered today
  private triggeredWindows: Set<string> = new Set();
  
  // Epic-specific close times loaded from marketInfo
  private epicCloseTimes: Map<string, Date> = new Map();
  
  // Brain decisions stored between windows
  private brainDecisions: Map<number, any[]> = new Map();
  
  // Close results stored between T-30s and T-15s windows (for passing fresh balance to open)
  private closeResults: Map<number, any[]> = new Map();
  
  // Track last hourly poll time
  private lastHourlyPoll: number = -1;
  
  // Track last nightly sync (date string like "2026-01-06")
  private lastNightlySync: string = '';
  
  // Current closing sequence logger (active during T-5m to T+10m)
  private closingLogger: ClosingLogger | null = null;
  
  // Pre-fetched candle data (loaded during quiet period, shared across T-60 calculations)
  // This ensures all accounts use the EXACT same data snapshot
  private epicDataCache: Record<string, any[]> = {};
  
  // Track if candle data is ready for each window (keyed by marketCloseTime.getTime())
  // True = :55 candle received and data preloaded, False = waiting
  private dataReadyForWindow: Map<number, boolean> = new Map();
  
  // Store active epics per window for gap-fill
  private windowActiveEpics: Map<number, string[]> = new Map();
  
  // Backup prices captured at T-5m (in case WebSocket doesn't deliver :55 candle)
  private backupPricesAt55: Record<string, { bid?: number; ask?: number }> = {};
  
  // Time offset between our server and Capital.com (milliseconds)
  // Positive = our server is AHEAD, Negative = our server is BEHIND
  // Used to adjust window timing calculations
  private capitalTimeOffset: number = 0;
  
  // Actual leverage state fetched at T-5m from Capital.com (GROUND TRUTH)
  // Key: capitalAccountId (string), Value: current SHARES leverage (number)
  // This prevents circular database logic where pending trade's DESIRED leverage
  // is mistaken for ACTUAL leverage on Capital.com
  private actualLeverageState: Map<string, number> = new Map();
  
  private constructor() {}
  
  /**
   * Get the current closing sequence logger (if active)
   * Used by other orchestrators to log to the closing sequence file
   */
  getClosingLogger(): ClosingLogger | null {
    return this.closingLogger;
  }
  
  /**
   * Get the close time for a specific epic
   * Used for window-epic validation to prevent wrong-epic trades
   */
  getEpicCloseTime(epic: string): Date | null {
    return this.epicCloseTimes.get(epic) || null;
  }
  
  /**
   * Get actual leverage state fetched at T-5m from Capital.com
   * Used by leverage_checker to avoid circular database cache bug
   */
  getActualLeverageState(): Map<string, number> {
    return this.actualLeverageState;
  }
  
  /**
   * Sync time with Capital.com's server
   * Called at the start of each closing sequence (T-300s)
   * 
   * This ensures our window timing aligns with Capital.com's clock,
   * not our potentially drifted local server clock.
   */
  private async syncWithCapitalTime(): Promise<void> {
    try {
      const { CapitalComAPI } = await import('../live_trading/capital_api');
      
      const timeSync = await CapitalComAPI.getServerTime('live');
      
      this.capitalTimeOffset = timeSync.offsetMs;
      
      const offsetDirection = this.capitalTimeOffset > 0 ? 'AHEAD of' : 'BEHIND';
      const absOffset = Math.abs(this.capitalTimeOffset);
      
      console.log(`[TimeSync] Capital.com server time: ${timeSync.serverTimeISO}`);
      console.log(`[TimeSync] Our server time:         ${new Date(timeSync.localTimeMs).toISOString()}`);
      console.log(`[TimeSync] Offset: ${this.capitalTimeOffset}ms (we are ${absOffset}ms ${offsetDirection} Capital.com)`);
      console.log(`[TimeSync] Network latency: ${timeSync.latencyMs}ms`);
      
      // Log to closing file if available
      if (this.closingLogger) {
        this.closingLogger.log('=== TIME SYNC WITH CAPITAL.COM ===');
        this.closingLogger.log(`Capital.com: ${timeSync.serverTimeISO}`);
        this.closingLogger.log(`Our server:  ${new Date(timeSync.localTimeMs).toISOString()}`);
        this.closingLogger.log(`Offset:      ${this.capitalTimeOffset}ms (${absOffset}ms ${offsetDirection} Capital.com)`);
        this.closingLogger.log(`Latency:     ${timeSync.latencyMs}ms`);
        
        if (absOffset > 5000) {
          this.closingLogger.log(`⚠️ WARNING: Clock drift > 5 seconds! Window timing will be adjusted.`);
        }
        this.closingLogger.log('==================================');
      }
      
      // Log warning if offset is significant
      if (absOffset > 5000) {
        await orchestrationLogger.warn('TIME_SYNC', 
          `⚠️ Significant time drift detected: ${absOffset}ms ${offsetDirection} Capital.com`,
          { data: { offsetMs: this.capitalTimeOffset, latencyMs: timeSync.latencyMs } }
        );
      }
      
    } catch (error: any) {
      console.error(`[TimeSync] Failed to sync with Capital.com: ${error.message}`);
      this.capitalTimeOffset = 0; // Fail-safe: use local time
    }
  }
  
  /**
   * Get the current time adjusted for Capital.com's clock
   * Use this instead of Date.now() for window timing calculations
   */
  private getAdjustedTime(): number {
    return Date.now() - this.capitalTimeOffset;
  }

  static getInstance(): OrchestrationTimer {
    if (!OrchestrationTimer.instance) {
      OrchestrationTimer.instance = new OrchestrationTimer();
    }
    return OrchestrationTimer.instance;
  }

  /**
   * Start the global timer
   * 
   * IMPORTANT: This now always does a FRESH start to prevent stale state issues.
   * If the timer thinks it's already running, it will stop first and restart cleanly.
   * This ensures epic close times and settings are always freshly loaded on app startup.
   */
  async start(): Promise<void> {
    // ALWAYS do a fresh start - stop first if we think we're running
    // This prevents the bug where timer reports isRunning=true but hasn't loaded epicCloseTimes
    if (this.isRunning) {
      await orchestrationLogger.info('TIMER_STARTED', 'Timer already running - stopping for fresh restart');
      await this.stop();
    }

    try {
      // Clear any stale state
      this.triggeredWindows.clear();
      this.epicCloseTimes.clear();
      this.dataReadyForWindow.clear();
      this.windowActiveEpics.clear();
      this.brainDecisions.clear();
      this.epicDataCache = {};
      this.actualLeverageState.clear();
      
      // Load settings from database
      await this.loadSettings();
      
      // Load epic close times from marketInfo
      await this.loadEpicCloseTimes();
      
      // Detect single-window day (e.g., day after Christmas with early close)
      // If all epics close at the same time, it's a single-window day
      const isSingleWindowDay = this.detectSingleWindowDay();
      fundTracker.setSingleWindowDay(isSingleWindowDay);
      
      this.isRunning = true;
      
      // Build epic close times summary for logging
      const epicCloseTimeSummary: Record<string, string> = {};
      for (const [epic, closeTime] of this.epicCloseTimes.entries()) {
        epicCloseTimeSummary[epic] = closeTime.toISOString();
      }
      
      await orchestrationLogger.info('TIMER_STARTED', 'Global orchestration timer started (FRESH)', {
        data: {
          brainWindow: `${this.settings!.brainWindowSeconds}s before close`,
          closeWindow: `${this.settings!.closeWindowSeconds}s before close`,
          openWindow: `${this.settings!.openWindowSeconds}s before close`,
          checkInterval: '1 second',
          epicCloseTimesLoaded: this.epicCloseTimes.size,
          epicCloseTimes: epicCloseTimeSummary,
          isSingleWindowDay,
        },
      });
      
      console.log(`[OrchestrationTimer] ✅ Timer FRESH START - loaded ${this.epicCloseTimes.size} epic close times:`, epicCloseTimeSummary);
      if (isSingleWindowDay) {
        console.log(`[OrchestrationTimer] ⚡ SINGLE-WINDOW DAY - All DNA strands will compete for 100% allocation`);
      }

      // Start session keep-alive
      sessionManager.start();
      
      // Start candle trigger system (event-driven execution)
      candleTriggerSystem.start();

      // Check every second
      this.intervalId = setInterval(() => {
        this.tick().catch(error => {
          orchestrationLogger.logError('SYSTEM_ERROR', 'Timer tick failed', error);
        });
      }, 1000);

    } catch (error: any) {
      await orchestrationLogger.logError('SYSTEM_ERROR', 'Failed to start timer', error);
      throw error;
    }
  }

  /**
   * Get timer status
   */
  getStatus(): { isRunning: boolean; settings: TimerSettings | null } {
    return {
      isRunning: this.isRunning,
      settings: this.settings,
    };
  }

  /**
   * Get the active closing logger (for writing brain/trade summaries)
   * Returns null if no window is currently active
   */
  getClosingLogger(): ClosingLogger | null {
    return this.closingLogger;
  }

  /**
   * Stop the global timer
   */
  async stop(): Promise<void> {
    if (!this.isRunning) {
      return;
    }

    if (this.intervalId) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }

    this.isRunning = false;
    
    // Clear all state to prevent stale data on next start
    this.triggeredWindows.clear();
    this.epicCloseTimes.clear();
    this.dataReadyForWindow.clear();
    this.windowActiveEpics.clear();
    this.brainDecisions.clear();
    this.epicDataCache = {};
    this.actualLeverageState.clear();
    
    // Stop session keep-alive
    sessionManager.stop();
    
    // Stop candle trigger system
    candleTriggerSystem.stop();
    
    await orchestrationLogger.info('TIMER_STOPPED', 'Global orchestration timer stopped - all state cleared');
  }

  /**
   * Test Live Sequence - Triggers the full closing sequence for testing
   * 
   * This runs through the actual live code paths with FORCE mode:
   * - Bypasses window time matching (runs brain for ALL active accounts)
   * - Forces fund tracker initialization for all accounts
   * 
   * Steps:
   * 1. Initialize logging and fund tracker for ALL accounts
   * 2. T-60s brain calculations (get API prices, run DNA calculations)
   * 3. T-15s open trades (fetch fresh balances, fetch bid/ask, fire trades)
   * 4. Cleanup
   * 
   * Uses compressed timings (3s between steps) for quick testing.
   * Creates a log in logs/closing for review.
   * 
   * @param progressCallback - Optional callback to report progress to UI
   * @returns Test results including log file path
   */
  async testLiveSequence(
    progressCallback?: (step: string, status: 'started' | 'completed' | 'error', details?: any) => void
  ): Promise<{
    success: boolean;
    logFile: string | null;
    steps: { step: string; status: string; duration: number; error?: string }[];
    totalDuration: number;
  }> {
    const results = {
      success: true,
      logFile: null as string | null,
      steps: [] as { step: string; status: string; duration: number; error?: string }[],
      totalDuration: 0,
    };
    
    const startTime = Date.now();
    
    // Reset API timing tracker at start of test
    apiTimingTracker.reset();
    
    // Get configured interval and start stats session
    const intervalSetting = await getSetting('system', 'api_call_interval_ms');
    const configuredIntervalMs = intervalSetting?.value ? parseInt(intervalSetting.value, 10) : 200;
    apiStatsCollector.startSession(configuredIntervalMs);
    
    await orchestrationLogger.info('TEST_LIVE', '🧪 Starting TEST LIVE SEQUENCE', {});
    
    progressCallback?.('initializing', 'started', { forceMode: true });
    
    try {
      // Ensure settings are loaded
      if (!this.settings) {
        await this.loadSettings();
      }
      
      // Import required modules
      const { getDb } = await import('../db');
      const { accounts: accountsTable, savedStrategies } = await import('../../drizzle/schema');
      const { eq, and, isNotNull } = await import('drizzle-orm');
      const db = await getDb();
      
      // Get ALL active demo accounts with strategies
      const activeAccounts = await db!
        .select()
        .from(accountsTable)
        .where(and(
          eq(accountsTable.isActive, true), 
          isNotNull(accountsTable.assignedStrategyId),
          eq(accountsTable.accountType, 'demo')  // Only demo for safety
        ));
      
      if (activeAccounts.length === 0) {
        results.success = false;
        results.steps.push({ step: 'initialization', status: 'error', duration: 0, error: 'No active demo accounts with strategies' });
        return results;
      }
      
      // Get the FIRST window time from the first account's strategy (use REAL window time like simulation)
      const [firstStrategy] = await db!
        .select()
        .from(savedStrategies)
        .where(eq(savedStrategies.id, activeAccounts[0].assignedStrategyId!))
        .limit(1);
      
      const windowConfig = firstStrategy?.windowConfig as any;
      // Default to 21:00 UTC (4pm ET) if no window config - must be UTC!
      const windowCloseTime = windowConfig?.windows?.[0]?.closeTime || '21:00:00';
      
      // Create marketCloseDate from windowCloseTime (needed for trade queue)
      const [hours, minutes, seconds] = windowCloseTime.split(':').map(Number);
      const marketCloseDate = new Date();
      marketCloseDate.setUTCHours(hours, minutes, seconds, 0);
      
      await orchestrationLogger.info('TEST_LIVE', `Using window close time: ${windowCloseTime} (from strategy)`, {
        data: { windowCloseTime, marketCloseDate: marketCloseDate.toISOString(), accountCount: activeAccounts.length }
      });
      
      // === Initialize Trade Queue for this window ===
      tradeQueue.initWindow(marketCloseDate);
      
      // === STEP 1: Initialize Fund Tracker (like simulation) ===
      const step1Start = Date.now();
      progressCallback?.('quiet_period_start', 'started');
      
      try {
        // Create closing logger
        const accountsForLog = activeAccounts.map((acc: any) => ({
          id: acc.id,
          name: acc.accountName || `Account_${acc.id}`,
          type: acc.accountType || 'demo',
        }));
        
        this.closingLogger = createClosingLog(windowCloseTime, accountsForLog);
        this.closingLogger.quietPeriodStart();
        this.closingLogger.log(`🧪 TEST MODE: Processing ${activeAccounts.length} demo accounts`);
        this.closingLogger.log(`📅 Using window close time: ${windowCloseTime}`);
        
        // Start quiet period in API queue
        apiQueue.startQuietPeriod(windowCloseTime, 5);
        
        // Initialize fund tracker for ALL active accounts (EXACTLY like simulation)
        // First, fetch all account balances in one API call (more efficient)
        const allBalances = new Map<string, number>();
        const demoClient = connectionManager.getClient('demo');
        if (demoClient) {
          const allAccounts = await demoClient.getAccounts();
          for (const acc of allAccounts) {
            allBalances.set(acc.accountId, acc.balance?.available || acc.balance?.balance || 0);
          }
        }
        
        let fundTrackerCount = 0;
        for (const account of activeAccounts) {
          try {
            // Get balance from pre-fetched data
            const capitalAccountId = account.capitalAccountId || account.accountId;
            const balance = allBalances.get(capitalAccountId) || account.balance || 0;
            
            // Get strategy's window config (use ORIGINAL - not forced!)
            const [strategy] = await db!
              .select()
              .from(savedStrategies)
              .where(eq(savedStrategies.id, account.assignedStrategyId!))
              .limit(1);
            
            if (strategy?.windowConfig) {
              // CRITICAL: First reconstruct state from DB to handle server restarts
              // This restores windowsTradedToday, usedPct, carriedOverPct from actual_trades
              const fullBalance = account.balance > 0 ? account.balance : balance;
              await fundTracker.reconstructFromDatabase(account.id, strategy.windowConfig, fullBalance, this.closingLogger);
              
              // Use ORIGINAL windowConfig (like simulation does)
              fundTracker.initialize(account.id, balance, strategy.windowConfig);
              fundTrackerCount++;
              
              this.closingLogger?.log(`   💰 Account ${account.id} (${account.accountName}): $${balance.toFixed(2)}`);
            }
          } catch (accountError: any) {
            this.closingLogger?.log(`   ⚠️ Account ${account.id}: ${accountError.message}`);
          }
        }
        
        this.closingLogger?.log(`💰 Fund tracker: ${fundTrackerCount} accounts initialized`);
        
        results.steps.push({ step: 'quiet_period_start', status: 'completed', duration: Date.now() - step1Start });
        progressCallback?.('quiet_period_start', 'completed', { 
          duration: Date.now() - step1Start,
          accountsFound: activeAccounts.length,
          fundTrackerInitialized: fundTrackerCount,
          windowCloseTime
        });
      } catch (error: any) {
        results.steps.push({ step: 'quiet_period_start', status: 'error', duration: Date.now() - step1Start, error: error.message });
        progressCallback?.('quiet_period_start', 'error', { error: error.message });
        results.success = false;
      }
      
      // Wait 2 seconds
      await new Promise(resolve => setTimeout(resolve, 2000));
      
      // === STEP 2: T-60s Brain Calculations ===
      const step2Start = Date.now();
      progressCallback?.('brain_calculations', 'started');
      
      try {
        // Get API prices for all active epics
        const epicsList = await this.getActiveEpics();
        const epicPrices: Record<string, number> = {};
        
        this.closingLogger?.log(`🧠 T-60s: Running brain calculations for ${epicsList.length} epics`);
        
        // Fetch current prices via API
        const client = connectionManager.getClient('demo') || connectionManager.getClient('live');
        if (client) {
          for (const epic of epicsList) {
            try {
              const marketInfo = await client.getMarketInfo(epic);
              const midPrice = marketInfo?.snapshot?.bid 
                ? (marketInfo.snapshot.bid + (marketInfo.snapshot.offer || marketInfo.snapshot.bid)) / 2
                : undefined;
              
              if (midPrice) {
                epicPrices[epic] = midPrice;
                this.closingLogger?.log(`   ${epic}: $${midPrice.toFixed(2)}`);
              }
            } catch (err: any) {
              this.closingLogger?.log(`   ${epic}: Error - ${err.message}`);
            }
          }
        }
        
        // FORCE: Run brain calculations for ALL demo accounts
        // Pass undefined for marketCloseTime so ALL accounts are processed (no filtering)
        // But pass marketCloseDate in options so trade queue can use it
        const decisions = await executeBrainCalculations(
          undefined,  // No market close filter - process ALL accounts
          windowCloseTime,   // Window close time string
          {
            isSimulation: false,  // Not simulation - real live code path
            triggerMode: 'timer',
            isT60Trigger: true,  // This enables trade queue integration
            epicPrices,
            epicDataCache: this.epicDataCache,
            windowCloseTime,
            marketCloseDate,  // Pass for trade queue tracking
          }
        );
        
        // Store decisions for T-15s
        this.brainDecisions.set(marketCloseDate.getTime(), decisions);
        
        const buyCount = decisions.filter(d => d.decision === 'buy').length;
        const holdCount = decisions.filter(d => d.decision === 'hold').length;
        
        this.closingLogger?.log(`🧠 T-60s Brain complete: ${buyCount} BUY, ${holdCount} HOLD`);
        for (const d of decisions) {
          this.closingLogger?.log(`   Account ${d.accountId}: ${d.decision.toUpperCase()} ${d.epic || 'N/A'} (${d.indicatorName || 'unknown'})`);
        }
        
        results.steps.push({ 
          step: 'brain_calculations', 
          status: 'completed', 
          duration: Date.now() - step2Start 
        });
        progressCallback?.('brain_calculations', 'completed', { 
          duration: Date.now() - step2Start,
          decisions: decisions.length,
          buySignals: buyCount,
          holdSignals: holdCount
        });
      } catch (error: any) {
        results.steps.push({ step: 'brain_calculations', status: 'error', duration: Date.now() - step2Start, error: error.message });
        progressCallback?.('brain_calculations', 'error', { error: error.message });
        this.closingLogger?.log(`✗ T-60s brain failed: ${error.message}`);
        results.success = false;
      }
      
      // Wait 2 seconds - check queue status (closing happened during brain calculations)
      await new Promise(resolve => setTimeout(resolve, 2000));
      
      // === STEP 3: Check Queue Status ===
      const step3Start = Date.now();
      progressCallback?.('queue_status', 'started');
      
      try {
        const queueStatus = tradeQueue.getQueueStatus(marketCloseDate);
        
        this.closingLogger?.log(`📋 Queue Status:`);
        this.closingLogger?.log(`   Total: ${queueStatus.total} accounts`);
        this.closingLogger?.log(`   Ready to Buy: ${queueStatus.readyToBuy}`);
        this.closingLogger?.log(`   Hold: ${queueStatus.hold}`);
        this.closingLogger?.log(`   Still Processing: ${queueStatus.brainCalculating + queueStatus.closingPosition + queueStatus.changingLeverage}`);
        this.closingLogger?.log(`   Failed: ${queueStatus.failed}`);
        
        results.steps.push({ 
          step: 'queue_status', 
          status: 'completed', 
          duration: Date.now() - step3Start 
        });
        progressCallback?.('queue_status', 'completed', { 
          duration: Date.now() - step3Start,
          ...queueStatus
        });
      } catch (error: any) {
        results.steps.push({ step: 'queue_status', status: 'error', duration: Date.now() - step3Start, error: error.message });
        progressCallback?.('queue_status', 'error', { error: error.message });
        this.closingLogger?.log(`✗ Queue status check failed: ${error.message}`);
      }
      
      // Wait 2 seconds before T-15s open
      await new Promise(resolve => setTimeout(resolve, 2000));
      
      // === STEP 4: T-15s Open New Trades (via trade queue) ===
      const step4Start = Date.now();
      progressCallback?.('open_trades', 'started');
      
      try {
        this.closingLogger?.log(`📈 T-15s: Opening new trades...`);
        
        // Use the proper executeOpenTrades which uses trade queue
        await this.executeOpenTrades(marketCloseDate);
        
        // Get final queue status
        const finalStatus = tradeQueue.getQueueStatus(marketCloseDate);
        
        results.steps.push({ 
          step: 'open_trades', 
          status: 'completed', 
          duration: Date.now() - step4Start 
        });
        progressCallback?.('open_trades', 'completed', { 
          duration: Date.now() - step4Start,
          tradeFired: finalStatus.tradeFired,
          completed: finalStatus.completed,
          failed: finalStatus.failed
        });
      } catch (error: any) {
        results.steps.push({ step: 'open_trades', status: 'error', duration: Date.now() - step4Start, error: error.message });
        progressCallback?.('open_trades', 'error', { error: error.message });
        this.closingLogger?.log(`✗ T-15s open failed: ${error.message}`);
        results.success = false;
      }
      
      // Wait 1 second for trades to settle
      await new Promise(resolve => setTimeout(resolve, 1000));
      
      // === STEP 5: Reconciliation (like simulation Phase 4) ===
      const step5Start = Date.now();
      progressCallback?.('reconciliation', 'started');
      
      try {
        // Get the open result from the last executeOpenTrades call
        // We need to get the fired trades to reconcile
        const decisions = this.brainDecisions.get(marketCloseDate.getTime()) || [];
        const readyTrades = tradeQueue.getReadyToBuy(marketCloseDate);
        
        // Build a minimal open result for reconciliation
        const firedTrades = readyTrades
          .filter(qt => qt.state === 'TRADE_FIRED' || qt.brainDecision?.tradeId)
          .map(qt => ({
            accountId: qt.accountId,
            trade: qt.brainDecision!,
            dealReference: null as string | null, // Will be fetched from DB
            dealId: null as string | null,
            entryPrice: null as number | null,
            error: null as string | null,
          }));
        
        this.closingLogger?.log(`🔍 T+1s: Reconciling ${firedTrades.length} trades...`);
        
        // Import and call executeReconciliation (like simulation does)
        const { executeReconciliation } = await import('./reconciliation_orchestrator');
        const reconciliationResult = await executeReconciliation(
          windowCloseTime,
          {
            windowCloseTime,
            tradesAttempted: firedTrades.length,
            tradesSucceeded: firedTrades.length,
            tradesFailed: 0,
            firedTrades,
            errors: [],
          },
          false  // Not simulation mode
        );
        
        this.closingLogger?.log(`✅ Reconciliation: ${reconciliationResult.confirmed} confirmed, ${reconciliationResult.rejected} rejected, ${reconciliationResult.pending} pending`);
        
        // === COMPREHENSIVE RECONCILIATION: Check ALL unconfirmed trades across ALL windows ===
        const { reconcileAllUnconfirmedTrades, syncPositionsFromCapital } = await import('./reconciliation_orchestrator');
        
        this.closingLogger?.log('🔍 Checking ALL unconfirmed trades across all windows...');
        const allUnconfirmedResult = await reconcileAllUnconfirmedTrades(false, 3, 500);
        
        if (allUnconfirmedResult.found > 0) {
          this.closingLogger?.log(`   Found ${allUnconfirmedResult.found} unconfirmed trades from other windows`);
          this.closingLogger?.log(`   Confirmed: ${allUnconfirmedResult.confirmed}, Rejected: ${allUnconfirmedResult.rejected}`);
        } else {
          this.closingLogger?.log('   No unconfirmed trades from other windows');
        }
        
        // === POSITION SYNC: Verify database matches Capital.com positions ===
        this.closingLogger?.log('🔄 Syncing positions from Capital.com...');
        const syncResult = await syncPositionsFromCapital(false);
        
        if (syncResult.tradesUpdated > 0 || syncResult.orphansDetected > 0) {
          this.closingLogger?.log(`   Updated: ${syncResult.tradesUpdated} trades, Orphans: ${syncResult.orphansDetected}`);
        } else {
          this.closingLogger?.log(`   All ${syncResult.positionsFound} positions in sync`);
        }
        
        results.steps.push({ 
          step: 'reconciliation', 
          status: 'completed', 
          duration: Date.now() - step5Start 
        });
        progressCallback?.('reconciliation', 'completed', { 
          duration: Date.now() - step5Start,
          confirmed: reconciliationResult.confirmed,
          rejected: reconciliationResult.rejected,
          pending: reconciliationResult.pending,
          allWindowsChecked: allUnconfirmedResult.found,
          positionsInSync: syncResult.positionsFound
        });
      } catch (error: any) {
        results.steps.push({ step: 'reconciliation', status: 'error', duration: Date.now() - step5Start, error: error.message });
        progressCallback?.('reconciliation', 'error', { error: error.message });
        this.closingLogger?.log(`✗ Reconciliation failed: ${error.message}`);
      }
      
      // Wait 2 seconds
      await new Promise(resolve => setTimeout(resolve, 2000));
      
      // === STEP 6: Cleanup ===
      const step6Start = Date.now();
      progressCallback?.('quiet_period_end', 'started');
      
      try {
        // End quiet period
        apiQueue.endQuietPeriod();
        
        // Close the logger
        if (this.closingLogger) {
          this.closingLogger.quietPeriodEnd();
          results.logFile = (this.closingLogger as any).filePath || null;
          this.closingLogger.close();
          this.closingLogger = null;
        }
        
        results.steps.push({ step: 'quiet_period_end', status: 'completed', duration: Date.now() - step6Start });
        progressCallback?.('quiet_period_end', 'completed', { duration: Date.now() - step6Start });
      } catch (error: any) {
        results.steps.push({ step: 'quiet_period_end', status: 'error', duration: Date.now() - step6Start, error: error.message });
        progressCallback?.('quiet_period_end', 'error', { error: error.message });
      }
      
    } catch (error: any) {
      await orchestrationLogger.logError('TEST_LIVE', 'Test live sequence failed', error);
      results.success = false;
      progressCallback?.('error', 'error', { error: error.message });
    }
    
    results.totalDuration = Date.now() - startTime;
    
    // Print API timing timeline and summary
    apiTimingTracker.printTimeline();
    apiTimingTracker.printSummary();
    
    // Log timing summary to closing log
    const timingSummary = apiTimingTracker.getSummary();
    
    // Count overlapping calls for stats
    const allCalls = apiTimingTracker.getAllCalls();
    let overlappingCalls = 0;
    for (let i = 1; i < allCalls.length; i++) {
      const prevCall = allCalls[i - 1];
      const currCall = allCalls[i];
      if (prevCall.endTimestamp && currCall.timestamp < prevCall.endTimestamp) {
        overlappingCalls++;
      }
    }
    
    // End stats session
    apiStatsCollector.endSession(timingSummary, overlappingCalls);
    if (this.closingLogger) {
      this.closingLogger.log(`\n📊 API TIMING SUMMARY:`);
      this.closingLogger.log(`   Total calls: ${timingSummary.totalCalls}`);
      this.closingLogger.log(`   Success: ${timingSummary.successCalls}, Failed: ${timingSummary.failedCalls}, Rate Limited: ${timingSummary.rateLimitedCalls}`);
      this.closingLogger.log(`   Avg duration: ${timingSummary.avgDuration}ms`);
      this.closingLogger.log(`   Min gap: ${timingSummary.minGap}ms`);
      if (timingSummary.callsUnder200ms > 0) {
        this.closingLogger.log(`   ⚠️ Calls under 200ms gap: ${timingSummary.callsUnder200ms}`);
      }
    }
    
    await orchestrationLogger.info('TEST_LIVE', `🧪 TEST LIVE SEQUENCE ${results.success ? 'COMPLETED' : 'FAILED'}`, {
      data: { 
        totalDuration: results.totalDuration, 
        steps: results.steps.length, 
        success: results.success,
        apiTimingSummary: timingSummary
      }
    });
    
    progressCallback?.('complete', 'completed', { 
      totalDuration: results.totalDuration,
      success: results.success,
      logFile: results.logFile,
      apiTimingSummary: timingSummary
    });
    
    return results;
  }

  /**
   * Load timer settings from database
   */
  private async loadSettings(): Promise<void> {
    try {
      const brainWindow = await getSetting('orchestration', 'brain_window_seconds');
      const closeWindow = await getSetting('orchestration', 'close_window_seconds');
      const openWindow = await getSetting('orchestration', 'open_window_seconds');
      const refreshTime = await getSetting('orchestration', 'daily_refresh_time');

      // Load quiet period settings
      const quietStart = await getSetting('orchestration', 'quiet_period_start_seconds');
      const quietEnd = await getSetting('orchestration', 'quiet_period_end_seconds');
      
      // Load trade priority order setting
      const priorityOrder = await getSetting('orchestration', 'trade_priority_order');
      if (priorityOrder) {
        tradeQueue.setPriorityOrder(priorityOrder.value as PriorityOrderType);
      }

      // Load trace mode setting (optional)
      const traceModeSetting = await getSetting('orchestration', 'trace_mode_enabled');
      const envTrace = process.env.ORCHESTRATION_TRACE_MODE_ENABLED;
      const traceModeEnabled = (envTrace !== undefined && envTrace !== null && envTrace !== '')
        ? ['true', '1', 'yes', 'on'].includes(envTrace.toLowerCase())
        : (traceModeSetting
          ? ['true', '1', 'yes', 'on'].includes((traceModeSetting.value || '').toLowerCase())
          : false);
      
      this.settings = {
        brainWindowSeconds: brainWindow ? parseInt(brainWindow.value) : 240,
        closeWindowSeconds: closeWindow ? parseInt(closeWindow.value) : 30,
        openWindowSeconds: openWindow ? parseInt(openWindow.value) : 15,
        dailyRefreshTime: refreshTime ? refreshTime.value : '21:00:00',
        quietPeriodStartSeconds: quietStart ? parseInt(quietStart.value) : 300, // 5 min before close
        quietPeriodEndSeconds: quietEnd ? parseInt(quietEnd.value) : -60, // 1 min after close (negative = after)
        traceModeEnabled,
      };

      await orchestrationLogger.debug('TIMER_STARTED', 'Timer settings loaded', {
        data: { ...this.settings, tradePriorityOrder: tradeQueue.getPriorityOrder() },
      });
    } catch (error: any) {
      await orchestrationLogger.logError('SYSTEM_ERROR', 'Failed to load timer settings, using defaults', error);
      
      // Use defaults if database fails
      this.settings = {
        brainWindowSeconds: 240,
        closeWindowSeconds: 30,
        openWindowSeconds: 15,
        dailyRefreshTime: '21:00:00',
        quietPeriodStartSeconds: 300, // 5 min before close
        quietPeriodEndSeconds: -60, // 1 min after close
        traceModeEnabled: false,
      };
    }
  }

  /**
   * Timer tick - called every second
   */
  private async tick(): Promise<void> {
    try {
      const now = new Date();
      
      // Get all market close times for today
      const closeTimes = await this.getMarketCloseTimes(now);
      
      // DEBUG: Log tick status around market close time (only every 30 seconds)
      const seconds = now.getSeconds();
      if (seconds === 0 || seconds === 30) {
        // Check if any close time is within 2 minutes of now
        const nearbyCloseTimes = closeTimes.filter(ct => {
          const diff = Math.abs(ct.getTime() - now.getTime()) / 1000;
          return diff < 120;
        });
        if (nearbyCloseTimes.length > 0) {
          console.log(`[Timer-Debug] tick at ${now.toISOString()}: ${closeTimes.length} close times, ${nearbyCloseTimes.length} nearby`);
          nearbyCloseTimes.forEach(ct => {
            const secondsUntil = (ct.getTime() - now.getTime()) / 1000;
            console.log(`[Timer-Debug]   closeTime=${ct.toISOString()}, secondsUntil=${secondsUntil.toFixed(1)}`);
          });
        }
      }
      
      // Check each close time for execution windows
      for (const closeTime of closeTimes) {
        await this.checkExecutionWindows(now, closeTime);
      }
      
      // Check hourly trade polling (on the hour, minute 0, second 0-1)
      await this.checkHourlyPoll(now);
      
      // Check daily candle refresh window
      await this.checkDailyRefreshWindow(now);
      
      // Check nightly trade sync (01:30 UTC - after extended hours close)
      await this.checkNightlyTradeSync(now);
      
      // Reset triggered windows at midnight
      if (now.getHours() === 0 && now.getMinutes() === 0 && now.getSeconds() === 0) {
        this.triggeredWindows.clear();
        this.lastHourlyPoll = -1; // Reset hourly poll tracker
        // Note: lastNightlySync is NOT reset at midnight (uses date string to prevent duplicate runs)
        await orchestrationLogger.debug('TIMER_STARTED', 'Daily reset: cleared triggered windows');
      }
    } catch (error: any) {
      await orchestrationLogger.logError('SYSTEM_ERROR', 'Timer tick error', error);
    }
  }

  /**
   * Load epic close times from epics table (DYNAMIC nextClose)
   * 
   * IMPORTANT: Uses epics.nextClose which is DYNAMICALLY calculated by market_hours_service.ts
   * This accounts for:
   * - Early Friday closes
   * - Holidays
   * - DST changes
   * - Any irregular market schedules from Capital.com
   * 
   * Only loads close times for epics where market is currently OPEN (TRADEABLE)
   * This prevents windows from firing on weekends/holidays when markets are closed
   */
  private async loadEpicCloseTimes(): Promise<void> {
    try {
      const { getDb } = await import('../db');
      const { marketInfo, epics: epicsTable } = await import('../../drizzle/schema');
      const { sql } = await import('drizzle-orm');
      
      const db = await getDb();
      if (!db) {
        await orchestrationLogger.warn('TIMER_STARTED', 'Database not available, cannot load epic close times');
        return;
      }

      // Clear existing close times (important for hourly refresh)
      this.epicCloseTimes.clear();

      const now = new Date();

      // Load epics with DYNAMIC nextClose times (calculated from Capital.com API)
      // This is the key change - using nextClose instead of static marketCloseTime
      const epicRecords = await db.select({
        symbol: epicsTable.symbol,
        marketStatus: epicsTable.marketStatus,
        nextOpen: epicsTable.nextOpen,
        nextClose: epicsTable.nextClose,
      }).from(epicsTable);

      // Also load marketInfo to know which epics are "active" (have trading config)
      const activeEpics = new Set<string>();
      const marketInfoRecords = await db.select({ epic: marketInfo.epic }).from(marketInfo).where(sql`isActive = true`);
      marketInfoRecords.forEach(m => activeEpics.add(m.epic));

      let loadedCount = 0;
      let skippedClosedCount = 0;
      let skippedNoCloseTime = 0;

      for (const epic of epicRecords) {
        // Skip epics not in marketInfo (not configured for trading)
        if (!activeEpics.has(epic.symbol)) {
          continue;
        }

        // Use DYNAMIC nextClose from epics table
        // This is calculated by market_hours_service.ts from Capital.com's actual schedule
        if (!epic.nextClose) {
          // Check if market is CLOSED with no upcoming close (weekend/holiday)
          if (epic.marketStatus === 'CLOSED') {
            const nextOpenStr = epic.nextOpen 
              ? epic.nextOpen.toISOString() 
              : 'unknown';
            await orchestrationLogger.debug('TIMER_STARTED', 
              `⏸️ Skipping ${epic.symbol} - market CLOSED with no nextClose (opens ${nextOpenStr})`);
            skippedClosedCount++;
          } else {
            await orchestrationLogger.debug('TIMER_STARTED', 
              `⚠️ Skipping ${epic.symbol} - no nextClose time available`);
            skippedNoCloseTime++;
          }
          continue;
        }

        // Use the actual nextClose time (already accounts for early Friday, holidays, etc.)
        const closeTimeUTC = new Date(epic.nextClose);
        
        // Grace period: Keep close time even if it just passed (for quiet_end window)
        const afterCloseGraceMs = 2 * 60 * 1000; // 2 minutes
        if (closeTimeUTC.getTime() < now.getTime() - afterCloseGraceMs) {
          // Close time is too far in the past - skip (will be refreshed on next hourly poll)
          await orchestrationLogger.debug('TIMER_STARTED', 
            `⏭️ Skipping ${epic.symbol} - nextClose ${closeTimeUTC.toISOString()} already passed`);
          continue;
        }
        
        // Log if market is currently CLOSED but has a valid close time later today
        if (epic.marketStatus === 'CLOSED') {
          const nextOpenStr = epic.nextOpen ? epic.nextOpen.toISOString() : 'later';
          await orchestrationLogger.debug('TIMER_STARTED', 
            `⏰ ${epic.symbol} - market CLOSED now, opens ${nextOpenStr}, close window at ${closeTimeUTC.toISOString()}`);
        }

        this.epicCloseTimes.set(epic.symbol, closeTimeUTC);
        loadedCount++;
        
        // Log in both UTC and ET for clarity
        const etTime = closeTimeUTC.toLocaleString('en-US', { 
          timeZone: 'America/New_York', 
          hour: '2-digit', 
          minute: '2-digit',
          hour12: false 
        });
        await orchestrationLogger.debug('TIMER_STARTED', 
          `✅ Loaded DYNAMIC close time for ${epic.symbol}: ${closeTimeUTC.toISOString()} UTC (${etTime} ET)`);
      }

      let summaryMsg = `Loaded ${loadedCount} epic close times (DYNAMIC from nextClose)`;
      if (skippedClosedCount > 0) {
        summaryMsg += `, skipped ${skippedClosedCount} (markets closed)`;
      }
      if (skippedNoCloseTime > 0) {
        summaryMsg += `, skipped ${skippedNoCloseTime} (no nextClose)`;
      }
      await orchestrationLogger.info('TIMER_STARTED', summaryMsg);
    } catch (error: any) {
      await orchestrationLogger.logError('TIMER_STARTED', 'Failed to load epic close times', error);
    }
  }

  /**
   * Detect if today is a single-window day
   * 
   * A single-window day occurs when:
   * 1. All tradeable epics close at the same time (within 5-minute tolerance)
   * 2. The close time is NOT a normal schedule (i.e., not 21:00 AND 01:00)
   * 
   * Examples of single-window days:
   * - Day after Christmas: All markets close at 18:00 UTC
   * - Holiday half-days: Early close for all markets
   * 
   * On single-window days:
   * - All DNA strands from ALL windows compete in conflict resolution
   * - The winning trade gets 100% allocation instead of window %
   */
  private detectSingleWindowDay(): boolean {
    if (this.epicCloseTimes.size === 0) {
      console.log(`[OrchestrationTimer] detectSingleWindowDay: No epic close times loaded, returning false`);
      return false;
    }
    
    // Get unique close times (within 5-minute tolerance)
    const uniqueTimes = new Map<number, string[]>(); // timestamp → epics
    
    for (const [epic, closeTime] of this.epicCloseTimes.entries()) {
      const timeKey = Math.round(closeTime.getTime() / (5 * 60 * 1000)); // 5-min buckets
      if (!uniqueTimes.has(timeKey)) {
        uniqueTimes.set(timeKey, []);
      }
      uniqueTimes.get(timeKey)!.push(epic);
    }
    
    // Log unique time buckets for debugging
    console.log(`[OrchestrationTimer] detectSingleWindowDay: ${this.epicCloseTimes.size} epics, ${uniqueTimes.size} unique close time buckets`);
    for (const [timeKey, epics] of uniqueTimes.entries()) {
      const time = new Date(timeKey * 5 * 60 * 1000);
      console.log(`   Bucket ${time.toISOString()}: ${epics.join(', ')}`);
    }
    
    // If MORE than one unique close time bucket, it's NOT a single-window day
    if (uniqueTimes.size !== 1) {
      console.log(`[OrchestrationTimer] detectSingleWindowDay: ${uniqueTimes.size} different close times → NORMAL day`);
      return false;
    }
    
    // Get the single close time
    const closeTime = Array.from(this.epicCloseTimes.values())[0];
    const closeHour = closeTime.getUTCHours();
    
    // Check if this is a NORMAL schedule
    // Normal has TWO windows: 21:00 UTC (regular) and 01:00 UTC (extended)
    // If the close time is 21:00 or 01:00, it's probably normal (unless market was closed earlier)
    const isNormalRegularClose = closeHour === 21;
    const isNormalExtendedClose = closeHour === 0 || closeHour === 1;
    
    // Single-window day = all epics close at same time AND it's NOT a normal close time
    const isSingleWindow = !isNormalRegularClose && !isNormalExtendedClose;
    
    if (isSingleWindow) {
      console.log(`[OrchestrationTimer] ⚡ SINGLE-WINDOW DAY detected: All ${this.epicCloseTimes.size} epics close at ${closeTime.toISOString()} (hour ${closeHour})`);
    } else {
      console.log(`[OrchestrationTimer] detectSingleWindowDay: Close hour ${closeHour} is normal schedule → NORMAL day`);
    }
    
    return isSingleWindow;
  }

  /**
   * Get all unique market close times for today
   * Returns array of close times from all epics
   */
  private async getMarketCloseTimes(now: Date): Promise<Date[]> {
    // Get unique close times from epicCloseTimes map
    const uniqueTimes = new Set<number>();
    
    this.epicCloseTimes.forEach((closeTime) => {
      uniqueTimes.add(closeTime.getTime());
    });
    
    return Array.from(uniqueTimes).map(time => new Date(time));
  }

  /**
   * Check if any execution windows should be triggered
   */
  private async checkExecutionWindows(now: Date, marketCloseTime: Date): Promise<void> {
    if (!this.settings) return;
    
    // Use Capital.com-adjusted time for accurate window calculations
    // This compensates for any drift between our server clock and Capital.com's clock
    const adjustedNowMs = this.getAdjustedTime();
    const secondsUntilClose = (marketCloseTime.getTime() - adjustedNowMs) / 1000;
    
    // Skip if market closed more than 2 minutes ago (allow quiet_end to trigger)
    if (secondsUntilClose < -120) return;
    
    // Check quiet period START (e.g., 300 seconds before close)
    // DYNAMIC CHECK: Verify active epics exist before starting quiet period
    // NO CACHING - accounts/strategies may change at any time
    if (this.shouldTriggerWindow('quiet_start', marketCloseTime, secondsUntilClose, this.settings.quietPeriodStartSeconds)) {
      const activeEpics = await this.getActiveEpicsForMarketCloseTime(marketCloseTime);
      
      if (activeEpics.length === 0) {
        const closeTimeStr = getUTCTimeString(marketCloseTime);
        await orchestrationLogger.debug('TIMER_STARTED', 
          `Skipping quiet_start for ${closeTimeStr} - no active strategy epics`);
        // Mark quiet_start as triggered so we don't recheck, but DON'T skip other windows
        // Brain/Open will fire independently and handle no-accounts gracefully
        this.triggeredWindows.add(`quiet_start_${marketCloseTime.getTime()}`);
      } else {
        await this.triggerQuietPeriodStart(marketCloseTime);
      }
    }
    
    // Gap-fill failsafe windows (T-4m, T-3m, T-2m)
    // These trigger if the WebSocket :55 candle hasn't arrived yet
    if (this.shouldTriggerWindow('gapfill_4m', marketCloseTime, secondsUntilClose, 240)) {
      await this.triggerGapFillFailsafe(marketCloseTime, 4);
    }
    if (this.shouldTriggerWindow('gapfill_3m', marketCloseTime, secondsUntilClose, 180)) {
      await this.triggerGapFillFailsafe(marketCloseTime, 3);
    }
    if (this.shouldTriggerWindow('gapfill_2m', marketCloseTime, secondsUntilClose, 120)) {
      await this.triggerGapFillFailsafe(marketCloseTime, 2);
    }
    
    // Check brain window (e.g., 240 seconds before close)
    if (this.shouldTriggerWindow('brain', marketCloseTime, secondsUntilClose, this.settings.brainWindowSeconds)) {
      await this.triggerBrainWindow(marketCloseTime);
    }
    
    // Check T-60 fake candle capture (60 seconds before close)
    // This runs for ALL epics regardless of trading activity
    if (this.shouldTriggerWindow('t60_capture', marketCloseTime, secondsUntilClose, 60)) {
      await this.triggerT60FakeCandleCapture(marketCloseTime);
    }
    
    // T-30s SAFETY NET: Close any positions that weren't closed by brain calculations
    // This catches:
    // - Accounts with mismatched window configs (e.g., Christmas early close at 18:00 but strategies use 21:00)
    // - Stale positions from previous windows
    // - Any positions that brain calculation missed
    // Brain-closed positions are tracked and skipped (no duplicate closes)
    if (this.shouldTriggerWindow('close', marketCloseTime, secondsUntilClose, this.settings.closeWindowSeconds)) {
      await this.triggerCloseWindow(marketCloseTime);
    }
    
    // Check open window (e.g., 15 seconds before close)
    if (this.shouldTriggerWindow('open', marketCloseTime, secondsUntilClose, this.settings.openWindowSeconds)) {
      await this.triggerOpenWindow(marketCloseTime);
    }
    
    // Check quiet period END (e.g., -60 seconds = 1 minute AFTER close)
    // Note: quietPeriodEndSeconds is negative for "after close"
    
    // DEBUG: Log quiet_end check for troubleshooting (only when close to window time)
    if (secondsUntilClose > -120 && secondsUntilClose < 0) {
      const windowKey = `quiet_end_${marketCloseTime.getTime()}`;
      const alreadyTriggered = this.triggeredWindows.has(windowKey);
      const diff = Math.abs(secondsUntilClose - this.settings.quietPeriodEndSeconds);
      console.log(`[Timer-Debug] quiet_end check: marketClose=${marketCloseTime.toISOString()}, secondsUntilClose=${secondsUntilClose.toFixed(1)}, diff=${diff.toFixed(1)}, alreadyTriggered=${alreadyTriggered}, willTrigger=${diff <= 1 && !alreadyTriggered}`);
    }
    
    if (this.shouldTriggerWindow('quiet_end', marketCloseTime, secondsUntilClose, this.settings.quietPeriodEndSeconds)) {
      await this.triggerQuietPeriodEnd(marketCloseTime);
    }
  }

  /**
   * Check if a window should be triggered
   */
  private shouldTriggerWindow(
    windowType: 'brain' | 'close' | 'open' | 'quiet_start' | 'quiet_end' | 't60_capture' | 'gapfill_4m' | 'gapfill_3m' | 'gapfill_2m',
    marketCloseTime: Date,
    secondsUntilClose: number,
    windowSeconds: number
  ): boolean {
    const windowKey = `${windowType}_${marketCloseTime.getTime()}`;
    
    // Already triggered?
    if (this.triggeredWindows.has(windowKey)) {
      return false;
    }
    
    // Within 1 second of trigger time? (allows for timing jitter)
    const isWithinWindow = Math.abs(secondsUntilClose - windowSeconds) <= 1;
    
    return isWithinWindow;
  }
  
  /**
   * Trigger T-60 fake candle capture for ALL epics + brain calculations for Fake5min_4thCandle accounts
   * This runs 60 seconds before market close to:
   * 1. Capture fake 5-min candles via REST API (instant price)
   * 2. Trigger brain calculations for Fake5min_4thCandle mode accounts
   * 
   * CRITICAL: We use REST API here instead of waiting for WebSocket because:
   * - WebSocket candles arrive ~23 seconds late
   * - At T-60s, we need the price NOW to have time for brain + close + leverage operations
   * - REST API gives us instant bid/ask prices
   */
  private async triggerT60FakeCandleCapture(marketCloseTime: Date): Promise<void> {
    const windowKey = `t60_capture_${marketCloseTime.getTime()}`;
    this.triggeredWindows.add(windowKey);
    
    await orchestrationLogger.info('T60_CAPTURE_STARTED', '🔥 T-60s TRIGGER: Capturing prices + starting brain calculations', {
      data: { marketCloseTime: marketCloseTime.toISOString() }
    });
    
    // === T-60s SESSION REFRESH ===
    // CRITICAL: Refresh session BEFORE making API calls
    // The quiet period started at T-5m, session could be 4+ minutes old
    // Capital.com sessions expire after ~10 minutes of inactivity
    try {
      await sessionManager.executeKeepAliveNow();
      await orchestrationLogger.info('SESSION_KEEPALIVE', '🔐 T-60s keep-alive executed (pre-API refresh)');
      if (this.closingLogger) {
        this.closingLogger.log('🔐 T-60s session refresh OK');
      }
    } catch (keepaliveError: any) {
      await orchestrationLogger.logError('SESSION_ERROR', 'T-60s keep-alive failed - API calls may fail', keepaliveError);
      if (this.closingLogger) {
        this.closingLogger.log(`⚠️ T-60s session refresh FAILED: ${keepaliveError.message}`);
      }
      // Continue anyway - the API calls might still work if session is valid
    }
    
    try {
      // Step 1: Capture fake candles via REST API for ALL epics
      const results = await candleDataService.captureAllT60FakeCandles();
      
      const successCount = Array.from(results.values()).filter(r => r.success).length;
      const totalCount = results.size;
      
      await orchestrationLogger.info('T60_CAPTURE_COMPLETE', `T-60 capture: ${successCount}/${totalCount} epics`, {
        data: {
          marketCloseTime: marketCloseTime.toISOString(),
          successCount,
          totalCount,
        }
      });
      
      // Step 2: Trigger brain calculations for Fake5min_4thCandle accounts
      // These accounts use the API price we just fetched (not waiting for WebSocket)
      await this.triggerT60BrainCalculations(marketCloseTime, results);
      
    } catch (error: any) {
      await orchestrationLogger.logError('T60_CAPTURE_FAILED', 'Failed to capture T-60 fake candles', error);
    }
  }
  
  /**
   * Trigger brain calculations for Fake5min_4thCandle accounts at T-60s
   * Uses the instant API prices captured in triggerT60FakeCandleCapture
   * 
   * DNA-DRIVEN: After T-60 DNA calculations complete:
   * 1. Check if all DNA strands are complete for each account
   * 2. Run FINAL conflict resolution across ALL DNA results
   * 3. Queue close/leverage operations based on final winners
   */
  private async triggerT60BrainCalculations(
    marketCloseTime: Date, 
    captureResults: Map<string, { success: boolean; close?: number; error?: string }>
  ): Promise<void> {
    const windowCloseTime = getUTCTimeString(marketCloseTime);
    
    try {
      // Get API prices for each epic from capture results
      const epicPrices: Record<string, number> = {};
      Array.from(captureResults.entries()).forEach(([epic, result]) => {
        if (result.success && result.close) {
          epicPrices[epic] = result.close;
        }
      });
      
      // === OPTIMIZED: Run brain with IMMEDIATE processing ===
      // - All DNA calculations run in PARALLEL
      // - LIVE accounts are processed IMMEDIATELY as each completes (close/leverage)
      // - DEMO accounts are processed AFTER all LIVE are done
      // - No waiting for slow calculations to block fast ones!
      
      await orchestrationLogger.info('T60_BRAIN', 
        `🔥 T-60s IMMEDIATE PROCESSING: Starting brain calculations`, {
          data: { windowCloseTime, epicCount: Object.keys(epicPrices).length }
        }
      );
      
      if (this.closingLogger) {
        this.closingLogger.log(`🔥 T-60s IMMEDIATE PROCESSING mode active`);
        this.closingLogger.log(`   LIVE accounts processed AS THEY COMPLETE`);
        this.closingLogger.log(`   DEMO accounts processed AFTER all LIVE`);
      }
      
      const result = await runBrainWithImmediateProcessing(
        'Fake5min_4thCandle',
        windowCloseTime,
        marketCloseTime,
        epicPrices
      );
      
      // Store decisions for T-15s open window
      this.brainDecisions.set(marketCloseTime.getTime(), result.decisions);
      
      const buyCount = result.decisions.filter(d => d.decision === 'buy').length;
      const holdCount = result.decisions.filter(d => d.decision === 'hold').length;
      
      await orchestrationLogger.info('T60_BRAIN', 
        `✅ T-60s complete: ${buyCount} BUY, ${holdCount} HOLD (${result.totalCalcTimeMs}ms)`, {
          data: {
            marketCloseTime: marketCloseTime.toISOString(),
            totalDecisions: result.decisions.length,
            buyCount,
            holdCount,
            liveProcessed: result.liveProcessed,
            demoProcessed: result.demoProcessed,
            totalCalcTimeMs: result.totalCalcTimeMs,
          }
        }
      );
      
      // Log to closing log file
      if (this.closingLogger) {
        this.closingLogger.log(`🧠 T-60s Brain complete: ${buyCount} BUY, ${holdCount} HOLD`);
        this.closingLogger.log(`   ⚡ LIVE: ${result.liveProcessed} accounts processed immediately`);
        this.closingLogger.log(`   📊 DEMO: ${result.demoProcessed} accounts processed after LIVE`);
        this.closingLogger.log(`   ⏱️ Total time: ${result.totalCalcTimeMs}ms`);
        for (const d of result.decisions) {
          this.closingLogger.log(`   Account ${d.accountId}: ${d.decision.toUpperCase()} ${d.epic || 'N/A'} (${d.indicatorName || 'unknown'})`);
        }
      }
      
    } catch (error: any) {
      await orchestrationLogger.logError('T60_BRAIN', 'T-60s brain calculations failed', error);
      if (this.closingLogger) {
        this.closingLogger.log(`✗ T-60s brain failed: ${error.message}`);
      }
    }
  }

  /**
   * Trigger quiet period START (e.g., 5 minutes before close)
   * Blocks non-critical API calls to reserve bandwidth for trading
   * 
   * DNA-DRIVEN: Also initializes accumulators for all active accounts
   */
  private async triggerQuietPeriodStart(marketCloseTime: Date): Promise<void> {
    const windowKey = `quiet_start_${marketCloseTime.getTime()}`;
    this.triggeredWindows.add(windowKey);
    
    // Calculate window close time string (HH:MM:SS)
    const windowCloseTime = getUTCTimeString(marketCloseTime);
    
    // Calculate duration: from now until quietPeriodEndSeconds after close
    const durationSeconds = this.settings!.quietPeriodStartSeconds + Math.abs(this.settings!.quietPeriodEndSeconds);
    const durationMinutes = Math.ceil(durationSeconds / 60);
    
    // === Initialize closing sequence file logger ===
    try {
      // Get all active accounts for logging
      const { getDb } = await import('../db');
      const { accounts } = await import('../../drizzle/schema');
      const { eq } = await import('drizzle-orm');
      const db = await getDb();
      const activeAccounts = await db!.select().from(accounts).where(eq(accounts.isActive, true));
      
      const accountsForLog = activeAccounts.map((acc: any) => ({
        id: acc.id,
        name: acc.accountName || `Account_${acc.id}`,
        type: acc.accountType || 'unknown',
      }));
      
      // Create the closing sequence logger
      this.closingLogger = createClosingLog(windowCloseTime, accountsForLog);
      this.closingLogger.quietPeriodStart();
      
      // === SYNC TIME WITH CAPITAL.COM ===
      // This ensures our window timing aligns with Capital.com's clock
      await this.syncWithCapitalTime();
      
      // === LOG TIMER STATUS FOR DEBUGGING ===
      this.closingLogger.log('=== TIMER STATUS AT WINDOW START ===');
      this.closingLogger.log(`Timer running: ${this.isRunning}`);
      this.closingLogger.log(`Epic close times loaded: ${this.epicCloseTimes.size}`);
      if (this.epicCloseTimes.size > 0) {
        const epicTimes: Record<string, string> = {};
        for (const [epic, closeTime] of this.epicCloseTimes.entries()) {
          epicTimes[epic] = closeTime.toISOString();
        }
        this.closingLogger.log(`Epic close times: ${JSON.stringify(epicTimes, null, 2)}`);
      }
      this.closingLogger.log(`Settings: brainWindow=${this.settings?.brainWindowSeconds}s, openWindow=${this.settings?.openWindowSeconds}s`);
      this.closingLogger.log(`Triggered windows count: ${this.triggeredWindows.size}`);
      this.closingLogger.log('=====================================');
      this.closingLogger.log('');
      this.closingLogger.log('Active accounts:', accountsForLog);

      // TRACE MODE: Mirror orchestration logs (API calls, decisions, DB ops) into this closing log file
      // for the duration of the quiet period.
      if (this.settings?.traceModeEnabled) {
        orchestrationLogger.setClosingTraceLogger(this.closingLogger, true, windowCloseTime);
      } else {
        orchestrationLogger.setClosingTraceLogger(null, false);
      }
    } catch (error: any) {
      await orchestrationLogger.logError('CLOSING_LOG', 'Failed to create closing logger', error);
    }
    
    await orchestrationLogger.info('WINDOW_TRIGGERED', '🔇 QUIET PERIOD STARTED', {
      data: {
        marketCloseTime: marketCloseTime.toISOString(),
        windowCloseTime,
        windowType: 'quiet_start',
        durationMinutes,
        message: 'Non-critical API calls will be deferred',
      },
    });
    
    // Start quiet period in API queue
    apiQueue.startQuietPeriod(windowCloseTime, durationMinutes);
    
    // === TRADE QUEUE: Initialize queue for this window ===
    tradeQueue.initWindow(marketCloseTime);
    await orchestrationLogger.info('TRADE_QUEUE', `Initialized trade queue for window ${windowCloseTime}`);
    
    // === DNA-DRIVEN: Initialize accumulators for all active accounts ===
    try {
      const accumulatorCount = await initializeWindowAccumulators(marketCloseTime, windowCloseTime);
      await orchestrationLogger.info('DNA_ACCUMULATOR', 
        `Initialized ${accumulatorCount} DNA accumulators for window ${windowCloseTime}`
      );
    } catch (error: any) {
      await orchestrationLogger.logError('DNA_ACCUMULATOR', 'Failed to initialize accumulators', error);
    }
    
    // === FUND TRACKER: Initialize with fresh balances from Capital.com ===
    // This matches simulation's Phase 0 - fund tracker must be initialized before T-15s open phase
    try {
      const { getDb } = await import('../db');
      const { accounts, savedStrategies } = await import('../../drizzle/schema');
      const { eq, and, isNotNull } = await import('drizzle-orm');
      const dbForFunds = await getDb();
      
      if (dbForFunds) {
        // Get all active accounts with assigned strategies
        const activeAccounts = await dbForFunds
          .select()
          .from(accounts)
          .where(and(eq(accounts.isActive, true), isNotNull(accounts.assignedStrategyId)));
        
        await orchestrationLogger.info('FUND_TRACKER', 
          `Initializing fund tracker for ${activeAccounts.length} active accounts`
        );

        // IMPORTANT:
        // We intentionally avoid per-account `switchAccount()` + `getAccountBalance()` here.
        // Reason: it is slow, increases rate-limit risk, and historically caused account-switch conflicts.
        // Instead we bulk-fetch all accounts (one call per environment) and map balances by Capital accountId.
        //
        // NOTE: Old code is intentionally left commented out for rollback/debugging.
        //
        // OLD (DO NOT USE - kept for rollback):
        // for (const account of activeAccounts) {
        //   const client = connectionManager.getClient(account.accountType as 'demo' | 'live');
        //   if (client) {
        //     await client.switchAccount(account.accountId);
        //     const accountBalance = await client.getAccountBalance(); // ⚠️ Not implemented on CapitalComAPI
        //   }
        // }

        const freshBalances = new Map<string, number>(); // Key: accountId string from Capital.com

        try {
          const demoClient = connectionManager.getClient('demo');
          if (demoClient) {
            const demoAccounts = await demoClient.getAccounts();
            for (const acc of demoAccounts) {
              // Use balance.balance (total equity) NOT available (free margin)
              // This ensures pool calculation isn't affected by locked positions
              freshBalances.set(acc.accountId, acc.balance?.balance || acc.balance?.available || 0);
            }
          }

          const liveClient = connectionManager.getClient('live');
          if (liveClient) {
            const liveAccounts = await liveClient.getAccounts();
            for (const acc of liveAccounts) {
              // Use balance.balance (total equity) NOT available (free margin)
              freshBalances.set(acc.accountId, acc.balance?.balance || acc.balance?.available || 0);
            }
          }
        } catch (balanceError: any) {
          await orchestrationLogger.warn(
            'FUND_TRACKER',
            `[triggerQuietPeriodStart] Bulk balance fetch failed - falling back to DB balances: ${balanceError.message}`
          );
        }

        let initializedCount = 0;
        for (const account of activeAccounts) {
          try {
            // Get strategy's window config
            const strategy = await dbForFunds
              .select()
              .from(savedStrategies)
              .where(eq(savedStrategies.id, account.assignedStrategyId!))
              .limit(1);

            if (!(strategy.length > 0 && strategy[0].windowConfig)) {
              continue;
            }

            // T-5m: Only session state + fund tracker initialization
            // Position closing happens at T-60 (for THAT account, THAT window, THAT epic only)
            
            // First reconstruct state from database to handle server restarts
            // This restores windowsTradedToday, usedPct, carriedOverPct, and originalDailyBalance from actual_trades
            // Use FULL balance (account.balance) not "available" for reconstruction
            const fullBalance = account.balance > 0 ? account.balance : account.available;
            await fundTracker.reconstructFromDatabase(account.id, strategy[0].windowConfig, fullBalance, this.closingLogger);

            // Prefer fresh balance from Capital.com bulk call, else fall back to DB snapshot
            const freshBalance = freshBalances.get(account.accountId);
            const balance =
              (freshBalance !== undefined && freshBalance > 0)
                ? freshBalance
                : (account.available > 0 ? account.available : account.balance);

            fundTracker.initialize(account.id, balance, strategy[0].windowConfig);
            initializedCount++;

            await orchestrationLogger.info(
              'FUND_TRACKER',
              `[triggerQuietPeriodStart] Account ${account.id} (${account.accountName}): $${balance.toFixed(2)} available`,
              { accountId: account.id, data: { balance, accountName: account.accountName, usedFreshBalance: freshBalance !== undefined } }
            );
            
            // Also initialize tradingSessionState for persistence
            // This is done inside fundTracker.initialize() via persistToTradingSessionState()
            // but we log it here for visibility
          } catch (accountError: any) {
            await orchestrationLogger.warn(
              'FUND_TRACKER',
              `[triggerQuietPeriodStart] Failed to initialize fund tracker for account ${account.id}: ${accountError.message}`,
              { accountId: account.id }
            );
          }
        }
        
        await orchestrationLogger.info('FUND_TRACKER', 
          `Fund tracker initialized: ${initializedCount}/${activeAccounts.length} accounts`
        );
        if (this.closingLogger) {
          this.closingLogger.log(`💰 Fund tracker: ${initializedCount} accounts initialized with fresh balances`);
        }
        
        // === FETCH ACTUAL LEVERAGE STATE: T-5m ground truth from Capital.com ===
        // This prevents circular database logic where pending trade's DESIRED leverage
        // is mistaken for ACTUAL leverage. We fetch ONCE at T-5m, then use this state
        // at T-60s to detect mismatches and actually change leverage if needed.
        // 
        // IMPORTANT: This is READ-ONLY at T-5m. Leverage changes happen at T-60s
        // after brain calcs complete and we know what leverage each account needs.
        // 
        // NOTE: Must be inside this block where activeAccounts is in scope!
        try {
          await this.fetchActualLeverageState(activeAccounts);
        } catch (error: any) {
          await orchestrationLogger.logError('LEVERAGE_FETCH', 'Failed to fetch actual leverage state at T-5m', error);
          if (this.closingLogger) {
            this.closingLogger.log(`✗ Leverage fetch failed: ${error.message}`);
          }
        }
      }
    } catch (error: any) {
      await orchestrationLogger.logError('FUND_TRACKER', 'Failed to initialize fund tracker', error);
      if (this.closingLogger) {
        this.closingLogger.log(`✗ Fund tracker failed: ${error.message}`);
      }
    }
    
    // === SMART GAP-FILL: Wait for :55 candle via WebSocket before gap-filling ===
    // The :55 candle (5 min before close) isn't published until ~:55:30 to :56:00
    // We register to wait for it via WebSocket, with failsafe triggers at T-4m, T-3m, T-2m
    try {
      // Only prep candles for epics that are relevant to THIS close time.
      // This prevents e.g. the BTCUSD 22:00 window from waiting on TECL/SOXL candles.
      const epicsList = await this.getActiveEpicsForMarketCloseTime(marketCloseTime);
      if (epicsList.length > 0) {
        // Store epics for this window so failsafe triggers know what to gap-fill
        this.windowActiveEpics.set(marketCloseTime.getTime(), epicsList);
        this.dataReadyForWindow.set(marketCloseTime.getTime(), false);
        
        // Calculate the expected :55 CLOSING candle timestamp
        // For a 21:00 close, the :55 closing candle is the period 20:50-20:55
        // Capital.com names 5-min candles by their START time, so this is the "20:50" candle
        // It closes at 20:55 and should be published shortly after
        const target55ClosingCandle = new Date(marketCloseTime);
        target55ClosingCandle.setUTCMinutes(target55ClosingCandle.getUTCMinutes() - 10); // 21:00 - 10 = 20:50
        target55ClosingCandle.setUTCSeconds(0);
        target55ClosingCandle.setUTCMilliseconds(0);
        
        await orchestrationLogger.info('DATA_GAPFILL', 
          `Waiting for :55 closing candle (starts at ${target55ClosingCandle.toISOString()}) for ${epicsList.length} epics: ${epicsList.join(', ')}`
        );
        if (this.closingLogger) {
          this.closingLogger.log(`⏳ Waiting for :55 closing candle (${target55ClosingCandle.toISOString()}) for ${epicsList.length} epics`);
        }
        
        // === BACKUP API CALL at T-5m ===
        // Capture current bid/ask for all epics as a backup
        // In case WebSocket never delivers the :55 closing candle, we can use this
        try {
          await orchestrationLogger.info('DATA_GAPFILL', 
            `📡 Making backup API call for ${epicsList.length} epics at T-5m`
          );
          if (this.closingLogger) {
            this.closingLogger.log(`📡 Backup API call at T-5m for ${epicsList.length} epics`);
          }
          
          const backupPricesMap = await candleDataService.getInstantPrices(epicsList);
          
          // Store backup prices for potential use in gap-fill (convert Map to object)
          this.backupPricesAt55 = {};
          let capturedCount = 0;
          for (const [epic, priceResult] of backupPricesMap) {
            if (priceResult.success && priceResult.bid && priceResult.ask) {
              this.backupPricesAt55[epic] = { bid: priceResult.bid, ask: priceResult.ask };
              capturedCount++;
            }
          }
          
          await orchestrationLogger.info('DATA_GAPFILL', 
            `✓ Backup prices captured for ${capturedCount}/${epicsList.length} epics`
          );
          if (this.closingLogger) {
            for (const [epic, price] of Object.entries(this.backupPricesAt55)) {
              if (price.bid && price.ask) {
                this.closingLogger.log(`  ${epic}: Bid=${price.bid.toFixed(4)}, Ask=${price.ask.toFixed(4)}`);
              }
            }
          }
        } catch (backupError: any) {
          await orchestrationLogger.warn('DATA_GAPFILL', 
            `Backup API call failed: ${backupError.message}`
          );
        }
        
        // Start async WebSocket wait for each epic (don't block)
        this.waitFor55CandleAndGapFill(marketCloseTime, epicsList, target55ClosingCandle);
      } else {
        if (this.closingLogger) {
          this.closingLogger.log(`ℹ️ No active strategy epics match this close time (${windowCloseTime}) - skipping :55 candle wait/gap-fill`);
        }
      }
    } catch (error: any) {
      await orchestrationLogger.logError('DATA_GAPFILL', 'Failed to setup :55 candle wait', error);
      if (this.closingLogger) {
        this.closingLogger.log(`✗ Setup :55 candle wait failed: ${error.message}`);
      }
    }
    
    // NOTE: Leverage fetch moved into the fund tracker block above where activeAccounts is in scope
    
    // Execute keep-alive now (before quiet period blocks it)
    try {
      await sessionManager.executeKeepAliveNow();
      await orchestrationLogger.info('SESSION_KEEPALIVE', 'Pre-quiet period keep-alive executed');
    } catch (error: any) {
      await orchestrationLogger.logError('SESSION_ERROR', 'Pre-quiet period keep-alive failed', error);
    }
  }
  
  /**
   * Fetch actual leverage state from Capital.com at T-5m
   * 
   * This provides GROUND TRUTH for what leverage Capital.com ACTUALLY has set,
   * preventing circular database logic where pending trade's DESIRED leverage
   * is mistaken for ACTUAL leverage.
   * 
   * IMPORTANT:
   * - This is READ-ONLY (no changes made here)
   * - Leverage changes happen at T-60s after brain calcs
   * - LIVE accounts processed first, then DEMO
   * - Only active accounts (never archived)
   * 
   * @param activeAccounts - All active accounts from database
   */
  private async fetchActualLeverageState(activeAccounts: any[]): Promise<void> {
    console.log(`[Timer] T-5m: Fetching actual leverage state for ${activeAccounts.length} accounts...`);
    
    if (this.closingLogger) {
      this.closingLogger.log('');
      this.closingLogger.section('T-5m: Fetching Actual Leverage State from Capital.com');
      this.closingLogger.log('(Ground truth - prevents circular database cache bug)');
    }
    
    // Clear previous state
    this.actualLeverageState.clear();
    
    // Separate LIVE and DEMO accounts
    const liveAccounts = activeAccounts.filter(acc => acc.accountType === 'live');
    const demoAccounts = activeAccounts.filter(acc => acc.accountType === 'demo');
    
    let fetchedCount = 0;
    
    // === LIVE ACCOUNTS FIRST ===
    if (liveAccounts.length > 0) {
      const liveClient = connectionManager.getClient('live');
      if (liveClient) {
        console.log(`[Timer] T-5m: Fetching leverage for ${liveAccounts.length} LIVE accounts...`);
        
        for (const acc of liveAccounts) {
          try {
            // Switch to this account
            await liveClient.switchAccount(acc.accountId);
            
            // Small delay to ensure switch completes
            await new Promise(r => setTimeout(r, 100));
            
            // Get account preferences (includes leverage per asset class)
            const prefs = await liveClient.getAccountPreferences();
            
            if (prefs?.leverages?.SHARES?.current) {
              const sharesLeverage = prefs.leverages.SHARES.current;
              this.actualLeverageState.set(acc.accountId, sharesLeverage);
              fetchedCount++;
              
              console.log(`[Timer] T-5m: Account ${acc.id} (${acc.accountName}) SHARES leverage = ${sharesLeverage}x (ACTUAL from Capital.com)`);
              
              if (this.closingLogger) {
                this.closingLogger.log(`   📊 ${acc.accountName} (LIVE): SHARES = ${sharesLeverage}x`);
              }
              
              await orchestrationLogger.info('LEVERAGE_FETCH',
                `Account ${acc.id}: Actual SHARES leverage = ${sharesLeverage}x (from Capital.com)`,
                { accountId: acc.id, data: { leverage: sharesLeverage, source: 't5m_api', accountType: 'live' } }
              );
            } else {
              console.warn(`[Timer] T-5m: Account ${acc.id} (${acc.accountName}) - no SHARES leverage in preferences`);
            }
            
            // Rate limit: 200ms between accounts
            await new Promise(r => setTimeout(r, 200));
            
          } catch (error: any) {
            console.error(`[Timer] T-5m: Failed to fetch leverage for account ${acc.id}: ${error.message}`);
            await orchestrationLogger.warn('LEVERAGE_FETCH',
              `Account ${acc.id}: Failed to fetch leverage: ${error.message}`,
              { accountId: acc.id }
            );
          }
        }
      } else {
        console.warn('[Timer] T-5m: No LIVE client available for leverage fetch');
      }
    }
    
    // === DEMO ACCOUNTS SECOND ===
    if (demoAccounts.length > 0) {
      const demoClient = connectionManager.getClient('demo');
      if (demoClient) {
        console.log(`[Timer] T-5m: Fetching leverage for ${demoAccounts.length} DEMO accounts...`);
        
        for (const acc of demoAccounts) {
          try {
            await demoClient.switchAccount(acc.accountId);
            await new Promise(r => setTimeout(r, 100));
            
            const prefs = await demoClient.getAccountPreferences();
            
            if (prefs?.leverages?.SHARES?.current) {
              const sharesLeverage = prefs.leverages.SHARES.current;
              this.actualLeverageState.set(acc.accountId, sharesLeverage);
              fetchedCount++;
              
              console.log(`[Timer] T-5m: Account ${acc.id} (${acc.accountName}) SHARES leverage = ${sharesLeverage}x`);
              
              if (this.closingLogger) {
                this.closingLogger.log(`   📊 ${acc.accountName} (DEMO): SHARES = ${sharesLeverage}x`);
              }
              
              await orchestrationLogger.info('LEVERAGE_FETCH',
                `Account ${acc.id}: Actual SHARES leverage = ${sharesLeverage}x`,
                { accountId: acc.id, data: { leverage: sharesLeverage, source: 't5m_api', accountType: 'demo' } }
              );
            }
            
            await new Promise(r => setTimeout(r, 200));
            
          } catch (error: any) {
            console.error(`[Timer] T-5m: Failed to fetch leverage for account ${acc.id}: ${error.message}`);
            await orchestrationLogger.warn('LEVERAGE_FETCH',
              `Account ${acc.id}: Failed to fetch leverage: ${error.message}`,
              { accountId: acc.id }
            );
          }
        }
      }
    }
    
    console.log(`[Timer] T-5m: Leverage fetch complete - ${fetchedCount}/${activeAccounts.length} accounts`);
    
    if (this.closingLogger) {
      this.closingLogger.log(`✅ Fetched actual leverage for ${fetchedCount}/${activeAccounts.length} accounts`);
      this.closingLogger.log('(These are stored and will be used at T-60s for leverage change detection)');
    }
    
    await orchestrationLogger.info('LEVERAGE_FETCH',
      `T-5m leverage fetch complete: ${fetchedCount}/${activeAccounts.length} accounts`,
      { data: { total: activeAccounts.length, fetched: fetchedCount, live: liveAccounts.length, demo: demoAccounts.length } }
    );
  }
  
  /**
   * Wait for the :55 CLOSING candle via WebSocket, then trigger gap-fill
   * This runs asynchronously and doesn't block quiet period start
   * 
   * For a 21:00 close, the :55 closing candle covers period 20:50-20:55
   * Capital.com names it by START time: "20:50" candle
   * It should be published ~30s to 1min after 20:55:00
   */
  private async waitFor55CandleAndGapFill(
    marketCloseTime: Date, 
    epicsList: string[], 
    target55ClosingCandle: Date
  ): Promise<void> {
    const windowKey = marketCloseTime.getTime();
    
    try {
      // Wait for the :55 closing candle from any epic (they should all arrive around the same time)
      // Timeout after 90 seconds (should arrive by :56:30 at the latest)
      const targetTimestamp = target55ClosingCandle.getTime();
      
      await orchestrationLogger.info('DATA_GAPFILL', 
        `Waiting for :55 closing candle via WebSocket (timeout: 90s)...`
      );
      
      // Check each epic for the :55 closing candle
      let candle55Received = false;
      for (const epic of epicsList) {
        const candlePair = await candleWebSocket.waitForCandle(epic, 'MINUTE_5', targetTimestamp, 90000);
        if (candlePair && candlePair.bid) {
          candle55Received = true;
          await orchestrationLogger.info('DATA_GAPFILL', 
            `📡 Received :55 closing candle for ${epic} via WebSocket (timestamp: ${new Date(candlePair.bid.timestamp).toISOString()})`
          );
          if (this.closingLogger) {
            this.closingLogger.log(`📡 Received :55 closing candle for ${epic} via WebSocket`);
          }
          break; // One is enough - they all publish at similar times
        }
      }
      
      if (candle55Received && !this.dataReadyForWindow.get(windowKey)) {
        // :55 closing candle received - trigger gap-fill immediately
        await this.executeGapFillAndPreload(marketCloseTime, epicsList, 'WebSocket :55 closing candle');
      } else if (!candle55Received) {
        await orchestrationLogger.warn('DATA_GAPFILL', 
          'WebSocket :55 closing candle timeout - will use failsafe triggers'
        );
        if (this.closingLogger) {
          this.closingLogger.log('⚠️ WebSocket :55 closing candle timeout - waiting for failsafe triggers');
        }
      }
    } catch (error: any) {
      await orchestrationLogger.logError('DATA_GAPFILL', 
        `WebSocket :55 closing candle wait failed: ${error.message}`,
        error
      );
    }
  }
  
  /**
   * Failsafe gap-fill trigger at T-4m, T-3m, T-2m
   * Only runs if data isn't already ready
   */
  private async triggerGapFillFailsafe(marketCloseTime: Date, minutesBefore: number): Promise<void> {
    const windowKey = marketCloseTime.getTime();
    const windowType = `gapfill_${minutesBefore}m` as const;
    const triggerKey = `${windowType}_${windowKey}`;
    
    this.triggeredWindows.add(triggerKey);
    
    // Skip if data is already ready
    if (this.dataReadyForWindow.get(windowKey)) {
      await orchestrationLogger.debug('DATA_GAPFILL', 
        `T-${minutesBefore}m failsafe skipped - data already ready`
      );
      return;
    }
    
    const epicsList = this.windowActiveEpics.get(windowKey) || [];
    if (epicsList.length === 0) {
      await orchestrationLogger.warn('DATA_GAPFILL', 
        `T-${minutesBefore}m failsafe: No epics found for this window`
      );
      return;
    }
    
    await orchestrationLogger.info('DATA_GAPFILL', 
      `⏰ T-${minutesBefore}m FAILSAFE: Attempting gap-fill for ${epicsList.length} epics`
    );
    if (this.closingLogger) {
      this.closingLogger.log(`⏰ T-${minutesBefore}m FAILSAFE: Attempting gap-fill...`);
    }
    
    // Check if :55 closing candle is now available in the database
    // For a 21:00 close, this is the candle starting at 20:50 (covers 20:50-20:55)
    const target55ClosingCandle = new Date(marketCloseTime);
    target55ClosingCandle.setUTCMinutes(target55ClosingCandle.getUTCMinutes() - 10); // 21:00 - 10 = 20:50
    target55ClosingCandle.setUTCSeconds(0);
    target55ClosingCandle.setUTCMilliseconds(0);
    
    await this.executeGapFillAndPreload(
      marketCloseTime, 
      epicsList, 
      `T-${minutesBefore}m failsafe`,
      target55ClosingCandle
    );
  }
  
  /**
   * Execute gap-fill and preload candle data
   * Called by WebSocket trigger or failsafe triggers
   */
  private async executeGapFillAndPreload(
    marketCloseTime: Date, 
    epicsList: string[], 
    trigger: string,
    expectedCandleTime?: Date
  ): Promise<void> {
    const windowKey = marketCloseTime.getTime();
    
    // Check if already done
    if (this.dataReadyForWindow.get(windowKey)) {
      await orchestrationLogger.debug('DATA_GAPFILL', 
        `Gap-fill skipped (${trigger}) - already complete`
      );
      return;
    }
    
    try {
      // Gap-fill the candle data
      await orchestrationLogger.info('DATA_GAPFILL', 
        `📊 Gap-filling ${epicsList.length} epics (trigger: ${trigger})`
      );
      if (this.closingLogger) {
        this.closingLogger.log(`📊 Gap-filling ${epicsList.length} epics (trigger: ${trigger})`);
      }
      
      const freshnessResults = await candleDataService.ensureFreshCandles(epicsList, 5); // Max 5 min old
      
      // Check if we got the expected :55 closing candle
      // For a 21:00 close, this is the candle starting at 20:50 (covers 20:50-20:55)
      let got55ClosingCandle = true;
      if (expectedCandleTime) {
        for (const [epic, result] of Object.entries(freshnessResults)) {
          if (result.latestTimestamp) {
            const latestTime = new Date(result.latestTimestamp);
            if (latestTime.getTime() < expectedCandleTime.getTime()) {
              got55ClosingCandle = false;
              await orchestrationLogger.warn('DATA_GAPFILL', 
                `${epic}: Latest candle ${result.latestTimestamp} is BEFORE expected :55 closing candle ${expectedCandleTime.toISOString()}`
              );
            }
          }
        }
      }
      
      for (const [epic, result] of Object.entries(freshnessResults)) {
        if (result.fetched > 0) {
          await orchestrationLogger.debug('DATA_GAPFILL', 
            `✓ ${epic}: Fetched ${result.fetched} fresh candles (latest: ${result.latestTimestamp || 'unknown'})`
          );
        }
      }
      
      if (got55ClosingCandle) {
        await orchestrationLogger.info('DATA_GAPFILL', '✓ Gap-fill complete - :55 closing candle confirmed');
        if (this.closingLogger) {
          this.closingLogger.log('✓ Gap-fill complete - :55 closing candle confirmed');
        }
      } else {
        await orchestrationLogger.warn('DATA_GAPFILL', 
          '⚠️ Gap-fill complete but :55 closing candle may be missing - will retry at next failsafe'
        );
        if (this.closingLogger) {
          this.closingLogger.log('⚠️ Gap-fill done but :55 closing candle may be missing');
        }
        return; // Don't mark as ready - try again at next failsafe
      }
      
      // Pre-load candle data cache
      await this.preloadCandleData();
      if (this.closingLogger) {
        this.closingLogger.log(`📂 Pre-loaded candle data for ${Object.keys(this.epicDataCache).length} epics`);
      }
      
      // Mark data as ready
      this.dataReadyForWindow.set(windowKey, true);
      
      await orchestrationLogger.info('DATA_GAPFILL', 
        `✅ Data ready for window ${getUTCTimeString(marketCloseTime)} (trigger: ${trigger})`
      );
      
    } catch (error: any) {
      await orchestrationLogger.logError('DATA_GAPFILL', 
        `Gap-fill failed (${trigger}): ${error.message}`,
        error
      );
      if (this.closingLogger) {
        this.closingLogger.log(`✗ Gap-fill failed (${trigger}): ${error.message}`);
      }
    }
  }
  
  /**
   * Trigger quiet period END (e.g., 1 minute after close)
   * Runs reconciliation and resumes normal API call processing
   */
  private async triggerQuietPeriodEnd(marketCloseTime: Date): Promise<void> {
    const windowKey = `quiet_end_${marketCloseTime.getTime()}`;
    this.triggeredWindows.add(windowKey);
    const windowCloseTime = getUTCTimeString(marketCloseTime);
    
    // IMPORTANT: Logger stays open through ALL reconciliation phases, even on errors
    // Use try/finally to ensure logger is always closed at the end
    try {
      // === PHASE 1: Standard Reconciliation ===
      // Match trades by dealReference using GET /confirms
      try {
        await orchestrationLogger.info('RECONCILIATION', '🔍 Phase 1: Standard reconciliation (GET /confirms)...');
        this.closingLogger?.log('═'.repeat(60));
        this.closingLogger?.log('🔍 PHASE 1: Standard Reconciliation (GET /confirms)');
        this.closingLogger?.log('═'.repeat(60));
        
        const { executeReconciliation } = await import('./reconciliation_orchestrator');
        const reconciliationResult = await executeReconciliation(windowCloseTime);
        
        await orchestrationLogger.info('RECONCILIATION', 
          `✅ Phase 1 complete: ${reconciliationResult.confirmed} confirmed, ${reconciliationResult.rejected} rejected`,
          { data: { 
            windowCloseTime, 
            confirmed: reconciliationResult.confirmed, 
            rejected: reconciliationResult.rejected,
            totalTrades: reconciliationResult.totalTrades
          }}
        );
        
        this.closingLogger?.log(`✅ Phase 1 Result: ${reconciliationResult.confirmed} confirmed, ${reconciliationResult.rejected} rejected, ${reconciliationResult.pending} pending`);
        for (const tradeResult of reconciliationResult.results) {
          this.closingLogger?.log(`   Trade ${tradeResult.tradeId}: ${tradeResult.status} ${tradeResult.confirmedDealId ? `(${tradeResult.confirmedDealId})` : ''}`);
        }
      } catch (error: any) {
        await orchestrationLogger.logError('RECONCILIATION', 'Phase 1 (standard) failed', error);
        this.closingLogger?.log(`✗ Phase 1 failed: ${error.message}`);
        this.closingLogger?.log(`   Stack: ${error.stack?.split('\n').slice(0,3).join('\n   ')}`);
        // Continue to next phase even on error
      }

      // Small delay to avoid rate limiting
      await new Promise(r => setTimeout(r, 500));
      
      // === PHASE 2: Bulk Position Matching ===
      // For trades that Phase 1 missed, try matching by position data
      try {
        this.closingLogger?.log('');
        this.closingLogger?.log('═'.repeat(60));
        this.closingLogger?.log('🔍 PHASE 2: Bulk Position Matching (GET /positions + /history/activity)');
        this.closingLogger?.log('═'.repeat(60));
        
        const { bulkReconcileAccount, cancelUnfilledOrders } = await import('./reconciliation_orchestrator');
        
        // Get all active accounts for this window
        const { getDb } = await import('../db');
        const { accounts, savedStrategies } = await import('../../drizzle/schema');
        const { eq, and, isNotNull } = await import('drizzle-orm');
        
        const db = await getDb();
        if (db) {
          const activeAccounts = await db
            .select()
            .from(accounts)
            .where(and(
              eq(accounts.isActive, true),
              eq(accounts.isArchived, false),
              isNotNull(accounts.assignedStrategyId)
            ));
          
          let totalBulkMatched = 0;
          let totalBulkRejected = 0;
          
          for (const account of activeAccounts) {
            this.closingLogger?.log(`   Checking ${account.accountName}...`);
            const bulkResult = await bulkReconcileAccount(account.id, windowCloseTime, false);
            
            if (bulkResult.found > 0) {
              this.closingLogger?.log(`   ✓ ${account.accountName}: ${bulkResult.matchedByPosition} by position, ${bulkResult.matchedByActivity} by activity, ${bulkResult.rejected} rejected`);
              totalBulkMatched += bulkResult.matchedByPosition + bulkResult.matchedByActivity;
              totalBulkRejected += bulkResult.rejected;
            }
            
            // Small delay between accounts
            await new Promise(r => setTimeout(r, 200));
          }
          
          this.closingLogger?.log(`✅ Phase 2 Result: ${totalBulkMatched} matched, ${totalBulkRejected} rejected`);
        }
      } catch (error: any) {
        await orchestrationLogger.logError('RECONCILIATION', 'Phase 2 (bulk) failed', error);
        this.closingLogger?.log(`✗ Phase 2 failed: ${error.message}`);
        // Continue to next phase even on error
      }
      
      // Small delay
      await new Promise(r => setTimeout(r, 500));
      
      // === PHASE 3: Comprehensive Reconciliation ===
      // Check ALL unconfirmed trades across ALL windows (catch-all)
      try {
        this.closingLogger?.log('');
        this.closingLogger?.log('═'.repeat(60));
        this.closingLogger?.log('🔍 PHASE 3: Comprehensive Reconciliation (all windows)');
        this.closingLogger?.log('═'.repeat(60));
        
        const { reconcileAllUnconfirmedTrades, syncPositionsFromCapital } = await import('./reconciliation_orchestrator');
        
        const allUnconfirmedResult = await reconcileAllUnconfirmedTrades(false, 3, 500);
        
        this.closingLogger?.allWindowsReconciliation(
          allUnconfirmedResult.found,
          allUnconfirmedResult.confirmed,
          allUnconfirmedResult.rejected,
          allUnconfirmedResult.pending
        );
        
        if (allUnconfirmedResult.found > 0) {
          await orchestrationLogger.info('RECONCILIATION', 
            `Comprehensive reconciliation: ${allUnconfirmedResult.confirmed}/${allUnconfirmedResult.found} confirmed`,
            { data: allUnconfirmedResult }
          );
        }
      } catch (error: any) {
        await orchestrationLogger.logError('RECONCILIATION', 'Phase 3 (comprehensive) failed', error);
        this.closingLogger?.log(`✗ Phase 3 failed: ${error.message}`);
      }
      
      // Small delay
      await new Promise(r => setTimeout(r, 500));
      
      // === PHASE 4: Position Sync ===
      // Verify database matches Capital.com positions
      try {
        this.closingLogger?.log('');
        this.closingLogger?.log('═'.repeat(60));
        this.closingLogger?.log('🔍 PHASE 4: Position Sync (verify DB matches Capital.com)');
        this.closingLogger?.log('═'.repeat(60));
        
        const { syncPositionsFromCapital } = await import('./reconciliation_orchestrator');
        
        this.closingLogger?.positionSyncStart();
        const syncResult = await syncPositionsFromCapital(false);
        
        this.closingLogger?.positionSyncResult(syncResult);
        
        if (syncResult.tradesUpdated > 0 || syncResult.orphansDetected > 0) {
          await orchestrationLogger.info('RECONCILIATION', 
            `Position sync: ${syncResult.tradesUpdated} updated, ${syncResult.orphansDetected} orphans`,
            { data: syncResult }
          );
        }
      } catch (error: any) {
        await orchestrationLogger.logError('RECONCILIATION', 'Phase 4 (sync) failed', error);
        this.closingLogger?.log(`✗ Phase 4 failed: ${error.message}`);
      }
      
      // Small delay
      await new Promise(r => setTimeout(r, 500));
      
      // === PHASE 5: Cancel Unfilled Orders ===
      // Cancel any working orders that didn't become positions
      try {
        this.closingLogger?.log('');
        this.closingLogger?.log('═'.repeat(60));
        this.closingLogger?.log('🔍 PHASE 5: Cancel Unfilled Orders');
        this.closingLogger?.log('═'.repeat(60));
        
        const { cancelUnfilledOrders } = await import('./reconciliation_orchestrator');
        
        const { getDb } = await import('../db');
        const { accounts } = await import('../../drizzle/schema');
        const { eq, and, isNotNull } = await import('drizzle-orm');
        
        const db = await getDb();
        if (db) {
          const activeAccounts = await db
            .select()
            .from(accounts)
            .where(and(
              eq(accounts.isActive, true),
              eq(accounts.isArchived, false),
              isNotNull(accounts.assignedStrategyId)
            ));
          
          let totalCancelled = 0;
          let totalAlreadyGone = 0;
          
          for (const account of activeAccounts) {
            const cancelResult = await cancelUnfilledOrders(account.id, windowCloseTime, false);
            
            if (cancelResult.ordersFound > 0) {
              this.closingLogger?.log(`   ${account.accountName}: ${cancelResult.ordersCancelled} cancelled, ${cancelResult.ordersAlreadyGone} already gone`);
              totalCancelled += cancelResult.ordersCancelled;
              totalAlreadyGone += cancelResult.ordersAlreadyGone;
            }
            
            // Small delay between accounts
            await new Promise(r => setTimeout(r, 200));
          }
          
          this.closingLogger?.log(`✅ Phase 5 Result: ${totalCancelled} orders cancelled, ${totalAlreadyGone} already processed`);
        }
      } catch (error: any) {
        await orchestrationLogger.logError('RECONCILIATION', 'Phase 5 (cancel orders) failed', error);
        this.closingLogger?.log(`✗ Phase 5 failed: ${error.message}`);
      }
      
      this.closingLogger?.log('');
      this.closingLogger?.log('═'.repeat(60));
      this.closingLogger?.log('✅ ALL RECONCILIATION PHASES COMPLETE');
      this.closingLogger?.log('═'.repeat(60));
      
    } finally {
      // ALWAYS close the logger, even if there were errors
      if (this.closingLogger) {
        this.closingLogger.quietPeriodEnd();
        this.closingLogger.close();
        // Disable TRACE mode before clearing the logger reference
        orchestrationLogger.setClosingTraceLogger(null, false);
        this.closingLogger = null;
      }
    }
    
    await orchestrationLogger.info('WINDOW_TRIGGERED', '🔊 QUIET PERIOD ENDED', {
      data: {
        marketCloseTime: marketCloseTime.toISOString(),
        windowType: 'quiet_end',
        message: 'Normal API call processing resumed',
      },
    });
    
    // End quiet period in API queue
    apiQueue.endQuietPeriod();
    
    // Clear caches (no longer needed after quiet period)
    this.epicDataCache = {};
    this.brainDecisions.delete(marketCloseTime.getTime());
    this.closeResults.delete(marketCloseTime.getTime());
    this.dataReadyForWindow.delete(marketCloseTime.getTime());
    this.windowActiveEpics.delete(marketCloseTime.getTime());
    await orchestrationLogger.debug('DATA_CACHE', 'Cleared epic data cache and decisions after quiet period');
  }
  
  /**
   * Get all active epics from active accounts' strategies
   * IMPORTANT: Excludes inactive and archived accounts
   */
  private async getActiveEpics(): Promise<string[]> {
    const { getDb } = await import('../db');
    const { accounts, savedStrategies } = await import('../../drizzle/schema');
    const { eq, and, isNotNull } = await import('drizzle-orm');
    
    const db = await getDb();
    if (!db) return [];
    
    const allEpics = new Set<string>();
    const activeAccounts = await db
      .select()
      .from(accounts)
      .where(and(
        isNotNull(accounts.assignedStrategyId), 
        eq(accounts.isActive, true),
        eq(accounts.isArchived, false)
      ));
    
    for (const account of activeAccounts) {
      if (!account.assignedStrategyId) continue;
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(eq(savedStrategies.id, account.assignedStrategyId))
        .limit(1);
      
      if (strategy.length > 0 && strategy[0].dnaStrands) {
        const dnaStrands = strategy[0].dnaStrands as any[];
        for (const dna of dnaStrands) {
          if (dna.epic) allEpics.add(dna.epic);
        }
      }
    }
    
    return Array.from(allEpics);
  }

  /**
   * Get active strategy epics that match a specific market close time (UTC).
   *
   * Why:
   * - The timer runs windows for ALL close times found in `marketInfo` (e.g., BTCUSD @ 22:00).
   * - Our bots only trade a subset of epics (those present in active strategies).
   * - If we don't filter, the 22:00 window can incorrectly wait for TECL/SOXL candles that
   *   will never exist at that time (because those markets are already closed).
   */
  private async getActiveEpicsForMarketCloseTime(marketCloseTime: Date): Promise<string[]> {
    const activeEpics = await this.getActiveEpics();
    if (activeEpics.length === 0) return [];

    const targetCloseTime = getUTCTimeString(marketCloseTime);

    // Filter to epics whose configured close time matches this window close time.
    // NOTE: We compare by HH:MM:SS (UTC) to avoid date-rollover issues.
    const filtered = activeEpics.filter((epic) => {
      const close = this.epicCloseTimes.get(epic);
      if (!close) return true; // Unknown epic timing - keep (safe fallback)
      return getUTCTimeString(close) === targetCloseTime;
    });

    return filtered;
  }
  
  /**
   * Pre-load candle data for all active epics during quiet period
   * This ensures all T-60 brain calculations use the EXACT same data snapshot
   * Matches the pattern used in global brain preview and simulation
   */
  private async preloadCandleData(): Promise<void> {
    const { getDb } = await import('../db');
    const { accounts, savedStrategies } = await import('../../drizzle/schema');
    const { eq, and, isNotNull } = await import('drizzle-orm');
    const { loadCandleData } = await import('../live_trading/brain');
    
    const db = await getDb();
    if (!db) return;
    
    // Collect all unique epics from active accounts' strategies
    // IMPORTANT: Excludes inactive and archived accounts
    const allEpics = new Set<string>();
    const activeAccounts = await db
      .select()
      .from(accounts)
      .where(and(
        isNotNull(accounts.assignedStrategyId), 
        eq(accounts.isActive, true),
        eq(accounts.isArchived, false)
      ));
    
    for (const account of activeAccounts) {
      if (!account.assignedStrategyId) continue;
      const strategy = await db
        .select()
        .from(savedStrategies)
        .where(eq(savedStrategies.id, account.assignedStrategyId))
        .limit(1);
      
      if (strategy.length > 0 && strategy[0].dnaStrands) {
        const dnaStrands = strategy[0].dnaStrands as any[];
        for (const dna of dnaStrands) {
          if (dna.epic) allEpics.add(dna.epic);
        }
      }
    }
    
    const epicsList = Array.from(allEpics);
    if (epicsList.length === 0) {
      await orchestrationLogger.debug('DATA_CACHE', 'No epics to pre-load (no active, non-archived accounts with strategies)');
      return;
    }
    
    await orchestrationLogger.info('DATA_CACHE', `Pre-loading candle data for ${epicsList.length} epics...`);
    
    // Clear previous cache
    this.epicDataCache = {};
    const daysToLoad = 30;
    
    for (const epic of epicsList) {
      try {
        const candleData = await loadCandleData(epic, daysToLoad);
        if (candleData.length > 0) {
          this.epicDataCache[epic] = candleData;
          const lastTimestamp = candleData[candleData.length - 1].timestamp;
          const ageMinutes = (Date.now() - new Date(lastTimestamp).getTime()) / (1000 * 60);
          await orchestrationLogger.debug('DATA_CACHE', `✓ ${epic}: ${candleData.length} candles (${ageMinutes.toFixed(1)} min old)`);
        }
      } catch (error: any) {
        await orchestrationLogger.logError('DATA_CACHE', `Failed to load ${epic}`, error);
      }
    }
    
    await orchestrationLogger.info('DATA_CACHE', 
      `Pre-loaded data for ${Object.keys(this.epicDataCache).length}/${epicsList.length} epics`
    );
  }
  
  /**
   * Trigger brain calculation window (e.g., 4 minutes before close)
   * 
   * DNA-DRIVEN: Runs brain for DNA strands with early timing modes:
   * - T5BeforeClose (if brainWindowSeconds >= 300)
   * - T4BeforeClose (default at 240s)
   * - SecondLastCandle, MarketClose
   * 
   * Fake5min_3rdCandle_API → WebSocket triggered
   * Fake5min_4thCandle → T-60s timer triggered
   */
  private async triggerBrainWindow(marketCloseTime: Date): Promise<void> {
    const windowKey = `brain_${marketCloseTime.getTime()}`;
    this.triggeredWindows.add(windowKey);
    
    const windowCloseTime = getUTCTimeString(marketCloseTime);
    
    await orchestrationLogger.info('WINDOW_TRIGGERED', 'Brain calculation window triggered (T-240s)', {
      data: {
        marketCloseTime: marketCloseTime.toISOString(),
        windowCloseTime,
        windowType: 'brain',
        secondsUntilClose: this.settings!.brainWindowSeconds,
      },
    });
    
    try {
      // === DNA-DRIVEN: Run brain for early timing modes ===
      // T5BeforeClose would have triggered at T-300s if enabled
      // T4BeforeClose triggers now at T-240s
      const earlyTimingModes = ['T5BeforeClose', 'T4BeforeClose', 'SecondLastCandle'];
      
      for (const timingMode of earlyTimingModes) {
        const result = await runBrainForTimingMode(timingMode, windowCloseTime);
        
        if (result.calculated > 0) {
          await orchestrationLogger.info('DNA_TRIGGER', 
            `${timingMode}: ${result.calculated} DNA strands calculated (${result.buySignals} BUY)`
          );
        }
      }
      
      // Register WebSocket-triggered DNA strands (Fake5min_3rdCandle_API)
      // These will be triggered when the 3rd candle arrives via WebSocket
      await this.registerWebSocketTriggers(marketCloseTime, windowCloseTime);
      
      // Log T-60 DNA strands (will trigger at T-60s)
      const pending = dnaBrainAccumulator.getPendingForTimingMode('Fake5min_4thCandle');
      if (pending.length > 0) {
        await orchestrationLogger.info('DNA_TRIGGER', 
          `${pending.length} Fake5min_4thCandle DNA strands will trigger at T-60s`
        );
      }
      
      // LEGACY CODE REMOVED - DNA-only path handles all accounts
      // executeBrainCalculations() was running for backwards compatibility
      // but caused conflicts with DNA accumulator
      
    } catch (error: any) {
      await orchestrationLogger.logError('BRAIN_ERROR', 'Brain window execution failed', error, {
        data: { marketCloseTime: marketCloseTime.toISOString() },
      });
    }
  }
  
  /**
   * Register WebSocket-triggered DNA strands with candle trigger system
   */
  private async registerWebSocketTriggers(marketCloseTime: Date, windowCloseTime: string): Promise<void> {
    const pending = dnaBrainAccumulator.getPendingForTimingMode('Fake5min_3rdCandle_API');
    
    if (pending.length === 0) return;
    
    // Group by epic for efficient WebSocket registration
    const byEpic = new Map<string, typeof pending>();
    for (const item of pending) {
      if (!byEpic.has(item.dnaStrand.epic)) {
        byEpic.set(item.dnaStrand.epic, []);
      }
      byEpic.get(item.dnaStrand.epic)!.push(item);
    }
    
    for (const [epic, items] of byEpic) {
      // Register with candle trigger system
      // The first item's account ID is used; trigger will handle all accounts for this epic
      candleTriggerSystem.registerTrigger(
        items[0].accountId,
        epic,
        'Fake5min_3rdCandle_API',
        marketCloseTime
      );
    }
    
    await orchestrationLogger.info('CANDLE_TRIGGER', 
      `Registered ${pending.length} Fake5min_3rdCandle_API DNA strands for WebSocket trigger`, {
        data: {
          epics: Array.from(byEpic.keys()),
          windowCloseTime,
        }
      }
    );
  }

  /**
   * T-30s SAFETY NET: Close any remaining positions for epics closing NOW
   * 
   * This is a SAFETY NET that runs AFTER brain calculations at T-60s.
   * It catches:
   * - Accounts with mismatched window configs (e.g., Christmas early close at 18:00)
   * - Stale positions from previous windows
   * - Any positions the brain missed
   * 
   * KEY: Positions already closed by brain are skipped (status='closed' in DB)
   * PRIORITY: Uses 'normal' priority so T-15s buy trades take precedence
   */
  private async triggerCloseWindow(marketCloseTime: Date): Promise<void> {
    const windowKey = `close_${marketCloseTime.getTime()}`;
    this.triggeredWindows.add(windowKey);
    
    // Get epics closing at this time for logging
    const closingEpics: string[] = [];
    for (const [epic, closeTime] of this.epicCloseTimes.entries()) {
      const diff = Math.abs(closeTime.getTime() - marketCloseTime.getTime());
      if (diff < 60000) {
        closingEpics.push(epic);
      }
    }
    
    await orchestrationLogger.info('WINDOW_TRIGGERED', '🛡️ T-30s SAFETY NET triggered', {
      data: {
        marketCloseTime: marketCloseTime.toISOString(),
        windowType: 'close_safety_net',
        secondsUntilClose: this.settings!.closeWindowSeconds,
        closingEpics,
        note: 'Checking for positions not closed by brain calculations',
      },
    });
    
    if (this.closingLogger) {
      this.closingLogger.log('');
      this.closingLogger.log('═══════════════════════════════════════════════════════════');
      this.closingLogger.log(`🛡️ T-30s SAFETY NET - Epics closing: ${closingEpics.join(', ')}`);
      this.closingLogger.log('═══════════════════════════════════════════════════════════');
    }
    
    try {
      // Execute safety net close for remaining open positions
      await this.executeCloseTrades(marketCloseTime);
    } catch (error: any) {
      await orchestrationLogger.logError('POSITION_CLOSE_FAILED', 'T-30s SAFETY NET failed', error, {
        data: { marketCloseTime: marketCloseTime.toISOString(), closingEpics },
      });
    }
  }

  /**
   * Trigger open trades window (e.g., 15 seconds before close)
   */
  private async triggerOpenWindow(marketCloseTime: Date): Promise<void> {
    const windowKey = `open_${marketCloseTime.getTime()}`;
    this.triggeredWindows.add(windowKey);
    
    await orchestrationLogger.info('WINDOW_TRIGGERED', 'Open trades window triggered', {
      data: {
        marketCloseTime: marketCloseTime.toISOString(),
        windowType: 'open',
        secondsUntilClose: this.settings!.openWindowSeconds,
      },
    });
    
    try {
      // TODO: Execute open trades for all winning signals
      await this.executeOpenTrades(marketCloseTime);
    } catch (error: any) {
      await orchestrationLogger.logError('POSITION_OPEN_FAILED', 'Open window execution failed', error, {
        data: { marketCloseTime: marketCloseTime.toISOString() },
      });
    }
  }

  /**
   * Check if hourly trade polling should run
   * Runs on the hour (minute 0, second 0-1)
   * Skips during quiet periods
   */
  private async checkHourlyPoll(now: Date): Promise<void> {
    const currentHour = now.getHours();
    
    // Only run at minute 0, second 0-1
    if (now.getMinutes() !== 0 || now.getSeconds() > 1) {
      return;
    }
    
    // Already polled this hour?
    if (this.lastHourlyPoll === currentHour) {
      return;
    }
    
    // Skip during quiet period
    if (apiQueue.isQuietPeriod) {
      await orchestrationLogger.debug('TRADE_POLL', 'Skipping hourly poll - quiet period active');
      return;
    }
    
    // Mark this hour as polled
    this.lastHourlyPoll = currentHour;
    
    await orchestrationLogger.info('TRADE_POLL', `Starting hourly refresh at ${now.toLocaleTimeString()}`);
    
    try {
      // IMPORTANT: Refresh epic close times hourly to pick up DST changes, holidays, etc.
      // MarketStateMonitor updates marketInfo table, we need to reload it into memory
      await this.loadEpicCloseTimes();
      await orchestrationLogger.info('TIMER_STARTED', 'Hourly refresh: Reloaded epic close times from marketInfo');
      
      // Run trade polling in background (don't block timer tick)
      tradePoller.pollAllAccounts().catch(error => {
        orchestrationLogger.logError('TRADE_POLL', 'Hourly poll failed', error);
      });
    } catch (error: any) {
      await orchestrationLogger.logError('TRADE_POLL', 'Failed to start hourly poll', error);
    }
  }

  /**
   * Nightly trade sync - runs at 01:30 UTC (after extended hours close)
   * 
   * This performs:
   * 1. Sync trades for all active accounts (incremental)
   * 2. Log summary of the day's trading
   * 
   * Runs Monday-Friday only (skips weekends)
   */
  private async checkNightlyTradeSync(now: Date): Promise<void> {
    // Only run at 01:30:00 UTC (after extended hours close at 01:00 UTC)
    if (now.getUTCHours() !== 1 || now.getUTCMinutes() !== 30 || now.getUTCSeconds() > 1) {
      return;
    }
    
    // Skip weekends (0 = Sunday, 6 = Saturday)
    const dayOfWeek = now.getUTCDay();
    if (dayOfWeek === 0 || dayOfWeek === 6) {
      return;
    }
    
    // Already synced today?
    const todayStr = now.toISOString().split('T')[0];
    if (this.lastNightlySync === todayStr) {
      return;
    }
    
    // Mark as synced
    this.lastNightlySync = todayStr;
    
    await orchestrationLogger.info('NIGHTLY_SYNC', `Starting nightly trade sync at ${now.toISOString()}`);
    console.log(`[Timer] 🌙 Starting nightly trade sync for ${todayStr}`);
    
    try {
      // Get all active accounts
      const db = await getDb();
      if (!db) {
        console.error('[Timer] Nightly sync: Database not available');
        return;
      }
      
      const { accounts } = await import('../../drizzle/schema');
      const { eq } = await import('drizzle-orm');
      
      const activeAccounts = await db
        .select()
        .from(accounts)
        .where(eq(accounts.isActive, true));
      
      console.log(`[Timer] Nightly sync: ${activeAccounts.length} active accounts`);
      
      // Sync trades for each account (incremental)
      for (const acc of activeAccounts) {
        try {
          console.log(`[Timer] Syncing trades for ${acc.accountName} (${acc.accountType})...`);
          
          // Use the trade sync service directly
          const { syncTradesForAccount } = await import('../services/trade_sync_service');
          
          const result = await syncTradesForAccount(acc.id, {
            days: 7,
            incrementalSync: true,
            fullSync: false
          });
          
          if (result.success) {
            console.log(`[Timer] ✅ ${acc.accountName}: ${result.newTrades || 0} new, ${result.updatedTrades || 0} updated`);
          } else {
            console.warn(`[Timer] ⚠️ ${acc.accountName}: sync warning - ${result.error || 'unknown'}`);
          }
          
          // Rate limit - 500ms between accounts
          await new Promise(r => setTimeout(r, 500));
          
          // Note: Strategy comparison is NOT run automatically at night
          // It requires initialBalance which is account-specific
          // Users run it manually via the Trade History page
        } catch (error: any) {
          console.error(`[Timer] ❌ ${acc.accountName}: sync failed - ${error.message}`);
        }
      }
      
      await orchestrationLogger.info('NIGHTLY_SYNC', `Nightly sync complete for ${activeAccounts.length} accounts`);
      console.log(`[Timer] 🌙 Nightly trade sync complete`);
      
    } catch (error: any) {
      await orchestrationLogger.logError('NIGHTLY_SYNC', 'Nightly sync failed', error);
      console.error('[Timer] Nightly sync failed:', error.message);
    }
  }

  // =========================================================================
  // LEGACY METHODS REMOVED (Dec 2025):
  // - executeBrainCalculations (private): Was running legacy brain calculations at T-240s
  // - categorizeAccountsByTimingMode: Was categorizing accounts by timing mode for legacy path
  // 
  // The DNA accumulator now handles ALL brain calculations:
  // 1. initializeWindowAccumulators() at T-300s sets up expected DNA strands per account/window
  // 2. runBrainForTimingMode('Fake5min_4thCandle') at T-60s calculates signals
  // 3. runFinalConflictResolution() resolves conflicts and picks winners
  // 
  // This ensures:
  // - Consistent window filtering (accounts only trade in their assigned windows)
  // - Proper conflict resolution (best DNA strand wins per account)
  // - No duplicate/conflicting brain calculations
  // =========================================================================

  /**
   * Execute close trades - SAFETY NET
   * 
   * This T-30s window catches positions that weren't closed by brain calculations:
   * - Accounts with mismatched window configs (e.g., Christmas early close)
   * - Stale positions from previous windows
   * - Any positions the brain missed
   * 
   * KEY DIFFERENCE from brain close:
   * - Brain closes filter by strategy window (only closes if account matches window)
   * - Safety net closes by EPIC (closes ANY open position for epics closing NOW)
   */
  private async executeCloseTrades(marketCloseTime: Date): Promise<void> {
    const windowCloseTime = getUTCTimeString(marketCloseTime);
    
    await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 'T-30s SAFETY NET: Checking for remaining open positions', {
      data: { marketCloseTime: marketCloseTime.toISOString(), windowCloseTime },
    });
    
    if (this.closingLogger) {
      this.closingLogger.log('📉 T-30s SAFETY NET: Checking for positions not closed by brain...');
    }
    
    // 1. Get epics that are closing at this time
    const closingEpics: string[] = [];
    for (const [epic, closeTime] of this.epicCloseTimes.entries()) {
      const diff = Math.abs(closeTime.getTime() - marketCloseTime.getTime());
      if (diff < 60000) { // Within 1 minute = same close time
        closingEpics.push(epic);
      }
    }
    
    if (closingEpics.length === 0) {
      await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
        'T-30s SAFETY NET: No epics closing at this time',
        { data: { marketCloseTime: marketCloseTime.toISOString() } }
      );
      if (this.closingLogger) {
        this.closingLogger.log('   No epics closing at this time - nothing to do');
      }
      return;
    }
    
    await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
      `T-30s SAFETY NET: Epics closing now: ${closingEpics.join(', ')}`,
      { data: { closingEpics, marketCloseTime: marketCloseTime.toISOString() } }
    );
    
    // 2. Find ALL open positions for these epics (regardless of windowCloseTime)
    // BUG FIX: Import getDb before using it
    const { getDb } = await import('../db');
    const db = await getDb();
    if (!db) {
      await orchestrationLogger.warn('POSITION_CLOSE_FAILED', 'Database not available');
      return;
    }
    
    const { actualTrades, accounts: accountsTable } = await import('../../drizzle/schema');
    const { eq, and, isNull, isNotNull, inArray } = await import('drizzle-orm');
    
    const openPositions = await db
      .select({
        trade: actualTrades,
        account: accountsTable,
      })
      .from(actualTrades)
      .innerJoin(accountsTable, eq(actualTrades.accountId, accountsTable.id))
      .where(
        and(
          inArray(actualTrades.epic, closingEpics),     // Epic is closing NOW
          isNotNull(actualTrades.dealId),               // Has been executed
          isNull(actualTrades.closedAt),                // Not already closed
          eq(actualTrades.status, 'open'),              // Is open
          eq(accountsTable.isActive, true)              // Account is active
        )
      );
    
    if (openPositions.length === 0) {
      await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
        'T-30s SAFETY NET: No remaining open positions for closing epics ✅',
        { data: { closingEpics, marketCloseTime: marketCloseTime.toISOString() } }
      );
      if (this.closingLogger) {
        this.closingLogger.log(`   ✅ No remaining open positions for ${closingEpics.join(', ')} - brain closed everything`);
      }
      return;
    }
    
    // 3. Log what we found
    await orchestrationLogger.warn('POSITION_CLOSE_REQUESTED', 
      `T-30s SAFETY NET: Found ${openPositions.length} positions NOT closed by brain!`,
      { data: { 
        count: openPositions.length, 
        closingEpics,
        positions: openPositions.map(p => ({
          accountId: p.trade.accountId,
          accountName: p.account.accountName,
          epic: p.trade.epic,
          dealId: p.trade.dealId,
          storedWindowCloseTime: p.trade.windowCloseTime,
          direction: p.trade.direction,
        }))
      } }
    );
    
    if (this.closingLogger) {
      this.closingLogger.log(`   ⚠️ Found ${openPositions.length} positions NOT closed by brain:`);
      for (const p of openPositions) {
        this.closingLogger.log(`      - Account ${p.account.accountName} (${p.trade.accountId}): ${p.trade.epic} ${p.trade.direction} dealId=${p.trade.dealId} (stored window: ${p.trade.windowCloseTime})`);
      }
    }
    
    // 4. Close each position with appropriate priority (below T-15s trades)
    // Group by account to minimize account switches
    const positionsByAccount = new Map<number, typeof openPositions>();
    for (const pos of openPositions) {
      if (!positionsByAccount.has(pos.trade.accountId)) {
        positionsByAccount.set(pos.trade.accountId, []);
      }
      positionsByAccount.get(pos.trade.accountId)!.push(pos);
    }
    
    let totalClosed = 0;
    let totalErrors = 0;
    
    // Process LIVE accounts first, then DEMO
    const accountEntries = Array.from(positionsByAccount.entries());
    accountEntries.sort((a, b) => {
      const aIsLive = openPositions.find(p => p.trade.accountId === a[0])?.account.accountType === 'live';
      const bIsLive = openPositions.find(p => p.trade.accountId === b[0])?.account.accountType === 'live';
      if (aIsLive && !bIsLive) return -1;
      if (!aIsLive && bIsLive) return 1;
      return 0;
    });
    
    for (const [accountId, positions] of accountEntries) {
      const account = positions[0].account;
      const accountType = account.accountType || 'demo';
      
      try {
        // Get API client
        const client = connectionManager.getClient(accountType);
        if (!client) {
          await orchestrationLogger.logError('POSITION_CLOSE_FAILED', 
            `T-30s SAFETY NET: No ${accountType} client available for account ${accountId}`, 
            new Error('No client')
          );
          totalErrors++;
          continue;
        }
        
        // Switch to account (use normal priority - below open trades)
        await apiQueue.enqueue({
          fn: () => client.switchAccount(account.capitalAccountId || ''),
          priority: 'normal', // Lower than 'critical' used for open trades
          description: `T-30s safety net: Switch to account ${account.accountName}`,
        });
        
        // Close each position
        for (const pos of positions) {
          const { trade } = pos;
          
          try {
            await orchestrationLogger.info('POSITION_CLOSE_REQUESTED', 
              `T-30s SAFETY NET: Closing position ${trade.dealId} for ${trade.epic}`,
              { data: { accountId, accountName: account.accountName, epic: trade.epic, dealId: trade.dealId } }
            );
            
            // Close position (use normal priority - below open trades)
            const closeResult = await apiQueue.enqueue({
              fn: () => client.closePosition(trade.dealId!),
              priority: 'normal',
              description: `T-30s safety net: Close ${trade.epic} position ${trade.dealId}`,
            });
            
            // Update database
            const now = new Date();
            await db.update(actualTrades)
              .set({
                status: 'closed',
                closedAt: now,
                closeReason: 'safety_net_t30',
                updatedAt: now,
              })
              .where(eq(actualTrades.id, trade.id));
            
            totalClosed++;
            
            if (this.closingLogger) {
              this.closingLogger.log(`      ✅ Closed ${trade.epic} position for ${account.accountName}`);
            }
            
          } catch (error: any) {
            await orchestrationLogger.logError('POSITION_CLOSE_FAILED', 
              `T-30s SAFETY NET: Failed to close position ${trade.dealId}`, 
              error,
              { data: { accountId, epic: trade.epic, dealId: trade.dealId } }
            );
            totalErrors++;
            
            if (this.closingLogger) {
              this.closingLogger.log(`      ❌ FAILED to close ${trade.epic}: ${error.message}`);
            }
          }
        }
        
      } catch (error: any) {
        await orchestrationLogger.logError('POSITION_CLOSE_FAILED', 
          `T-30s SAFETY NET: Failed to process account ${accountId}`, 
          error
        );
        totalErrors++;
      }
    }
    
    // 5. Summary
    await orchestrationLogger.info('POSITION_CLOSE_COMPLETED', 
      `T-30s SAFETY NET: Completed - ${totalClosed} closed, ${totalErrors} errors`,
      { data: { totalClosed, totalErrors, closingEpics } }
    );
    
    if (this.closingLogger) {
      this.closingLogger.log(`   📊 Safety net summary: ${totalClosed} positions closed, ${totalErrors} errors`);
    }
    
    // Store results for compatibility with existing code
    this.closeResults.set(marketCloseTime.getTime(), []);
  }

  /**
   * Execute open trades for all winning signals
   * 
   * QUEUE-BASED FLOW:
   * 1. Fetch fresh balances from Capital.com (one API call per environment)
   * 2. Re-initialize fundTracker with fresh balances
   * 3. Get READY_TO_BUY trades from queue in priority order
   * 4. Fire trades sequentially (respecting rate limiting)
   * 5. Continue processing until market close for late completions
   */
  private async executeOpenTrades(marketCloseTime: Date): Promise<void> {
    await orchestrationLogger.debug('POSITION_OPEN_REQUESTED', 'Executing open trades', {
      data: { marketCloseTime: marketCloseTime.toISOString() },
    });
    
    if (this.closingLogger) {
      this.closingLogger.log('📈 T-15s: Opening new trades...');
    }
    
    const windowCloseTime = getUTCTimeString(marketCloseTime);
    
    // === STEP 1: Fetch FRESH balances from Capital.com ===
    // This is a single API call that returns all accounts with their current balances
    await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
      'Fetching fresh account balances from Capital.com...'
    );
    
    const freshBalances = new Map<string, number>(); // Key: capitalAccountId, Value: balance
    
    try {
      // Fetch from demo environment
      const demoClient = connectionManager.getClient('demo');
      if (demoClient) {
        const demoAccounts = await demoClient.getAccounts();
        for (const acc of demoAccounts) {
          // Use balance.balance (total equity) NOT available (free margin)
          // Available is reduced by locked positions, but we want total value for pool calculation
          freshBalances.set(acc.accountId, acc.balance?.balance || acc.balance?.available || 0);
        }
        await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
          `Fetched ${demoAccounts.length} demo account balances`
        );
        if (this.closingLogger) {
          this.closingLogger.log(`   💰 Demo: ${demoAccounts.length} accounts`);
          for (const acc of demoAccounts) {
            const bal = acc.balance?.balance || acc.balance?.available || 0;
            this.closingLogger.log(`      ${acc.accountName || acc.accountId}: $${bal.toFixed(2)}`);
          }
        }
      }
      
      // Fetch from live environment (if any active)
      const liveClient = connectionManager.getClient('live');
      if (liveClient) {
        const liveAccounts = await liveClient.getAccounts();
        for (const acc of liveAccounts) {
          // Use balance.balance (total equity) NOT available (free margin)
          freshBalances.set(acc.accountId, acc.balance?.balance || acc.balance?.available || 0);
        }
        await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
          `Fetched ${liveAccounts.length} live account balances`
        );
        if (this.closingLogger) {
          this.closingLogger.log(`   💰 Live: ${liveAccounts.length} accounts`);
          for (const acc of liveAccounts) {
            const bal = acc.balance?.available || acc.balance?.balance || 0;
            this.closingLogger.log(`      ${acc.accountName || acc.accountId}: $${bal.toFixed(2)}`);
          }
        }
      }
      
      if (freshBalances.size === 0) {
        throw new Error('No account balances fetched - cannot proceed with trades');
      }
      
    } catch (error: any) {
      await orchestrationLogger.logError('POSITION_OPEN_FAILED', 
        'CRITICAL: Failed to fetch fresh balances - aborting open trades', error);
      if (this.closingLogger) {
        this.closingLogger.log(`   ❌ CRITICAL: Failed to fetch balances - ${error.message}`);
      }
      return; // Do not proceed without fresh balances
    }
    
    // === STEP 2: Re-initialize fundTracker with fresh balances ===
    const db = await import('../db').then(m => m.getDb());
    const { accounts: accountsTable, savedStrategies } = await import('../../drizzle/schema');
    const { eq, and, isNotNull } = await import('drizzle-orm');
    
    // BUG FIX: Declare activeAccounts outside the if block so it's accessible later
    let activeAccounts: any[] = [];
    
    if (db) {
      activeAccounts = await db
        .select()
        .from(accountsTable)
        .where(and(eq(accountsTable.isActive, true), isNotNull(accountsTable.assignedStrategyId)));
      
      for (const account of activeAccounts) {
        const capitalAccountId = account.capitalAccountId || account.accountId;
        const freshBalance = freshBalances.get(capitalAccountId);
        
        if (freshBalance !== undefined && freshBalance > 0) {
          // Get strategy's window config
          const [strategy] = await db
            .select()
            .from(savedStrategies)
            .where(eq(savedStrategies.id, account.assignedStrategyId!))
            .limit(1);
          
          if (strategy?.windowConfig) {
            // CRITICAL: First reconstruct state from DB to handle server restarts
            // This restores windowsTradedToday, usedPct, carriedOverPct from actual_trades
            const fullBalance = account.balance > 0 ? account.balance : freshBalance;
            await fundTracker.reconstructFromDatabase(account.id, strategy.windowConfig, fullBalance, this.closingLogger);
            
            fundTracker.initialize(account.id, freshBalance, strategy.windowConfig);
            
            // Also update the trade queue balance for priority ordering
            // NOTE: We keep updateBalance for rollback reference; the new preferred path
            // updates ALL metrics used by the Settings dropdown ordering (balance + pnl).
            // tradeQueue.updateBalance(marketCloseTime, account.id, freshBalance);
            tradeQueue.updateMetrics(marketCloseTime, account.id, {
              balance: freshBalance,
              pnl: account.profitLoss || 0,
            });
            
            await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
              `Account ${account.id}: Fresh balance $${freshBalance.toFixed(2)}`,
              { accountId: account.id, data: { freshBalance } }
            );
          }
        } else {
          await orchestrationLogger.warn('POSITION_OPEN_REQUESTED', 
            `Account ${account.id}: No fresh balance available`,
            { accountId: account.id }
          );
        }
      }
    }
    
    // === STEP 3: Get READY_TO_BUY trades from queue (sorted by priority) ===
    const readyTrades = tradeQueue.getReadyToBuy(marketCloseTime);
    
    await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
      `Found ${readyTrades.length} trades in READY_TO_BUY state (priority: ${tradeQueue.getPriorityOrder()})`,
      { data: { windowCloseTime, readyCount: readyTrades.length, priorityOrder: tradeQueue.getPriorityOrder() } }
    );
    
    if (this.closingLogger) {
      this.closingLogger.log(`   📋 Queue: ${readyTrades.length} trades ready (priority: ${tradeQueue.getPriorityOrder()})`);
      for (const qt of readyTrades) {
        this.closingLogger.log(`      - ${qt.accountName}: ${qt.brainDecision?.epic || 'N/A'} ($${qt.balance.toFixed(2)})`);
      }
    }
    
    // Build account type map for sorting
    const accountTypeMap = new Map<number, string>();
    for (const account of activeAccounts) {
      accountTypeMap.set(account.id, account.accountType || 'demo');
    }
    
    // Build decisions from ready trades (queue-based)
    let decisions = readyTrades
      .filter(qt => qt.brainDecision)
      .map(qt => qt.brainDecision!);
    
    // If queue is empty, use brainDecisions map (DNA accumulator populates this)
    // This is the primary source since legacy code was removed
    if (decisions.length === 0) {
      const dnaDecisions = this.brainDecisions.get(marketCloseTime.getTime()) || [];
      decisions = dnaDecisions.filter(d => d.decision === 'buy');
      
      await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
        `Using brainDecisions map: ${decisions.length} BUY decisions (queue was empty)`,
        { data: { windowCloseTime, decisionCount: decisions.length } }
      );
    }
    
    // === CRITICAL: Sort decisions to prioritize LIVE accounts ===
    // This ensures live trades fire first, before demo accounts
    const sortedDecisions = [...decisions].sort((a, b) => {
      const aType = accountTypeMap.get(a.accountId) || 'demo';
      const bType = accountTypeMap.get(b.accountId) || 'demo';
      
      // LIVE accounts first
      if (aType !== bType) {
        return aType === 'live' ? -1 : 1;
      }
      
      // Then by accountId for consistency
      return a.accountId - b.accountId;
    });
    
    decisions = sortedDecisions;
    
    // Log the sorted order
    const liveCount = decisions.filter(d => accountTypeMap.get(d.accountId) === 'live').length;
    const demoCount = decisions.length - liveCount;
    
    const buyDecisions = decisions.filter(d => d.decision === 'buy');
    await orchestrationLogger.info('POSITION_OPEN_REQUESTED', 
      `Opening trades for ${buyDecisions.length} BUY decisions (${liveCount} LIVE first, ${demoCount} DEMO)`,
      { data: { windowCloseTime, buyCount: buyDecisions.length, liveCount, demoCount, fromQueue: readyTrades.length } }
    );
    
    // T-30s close window removed - pass empty array for closeResults
    // Open orchestrator will fetch bid/ask directly (fresher prices!)
    const closeResults: any[] = [];
    
    // === STEP 4: Fire trades in priority order ===
    // The open orchestrator will:
    // 1. Fetch fresh bid/ask for each epic (since T-30s is removed)
    // 2. Use freshly initialized fundTracker for position sizing
    const openResult = await executeOpenTrades(windowCloseTime, decisions, closeResults, false);
    
    // Update trade queue states
    for (const fired of openResult.firedTrades) {
      if (fired.dealReference) {
        tradeQueue.markTradeFired(marketCloseTime, fired.accountId, fired.dealReference);
      }
    }
    
    await orchestrationLogger.info('POSITION_OPEN_COMPLETED', 
      `Open phase complete: ${openResult.tradesSucceeded} trades opened, ${openResult.tradesFailed} failed`,
      { data: { windowCloseTime, opened: openResult.tradesSucceeded, failed: openResult.tradesFailed } }
    );
    
    if (this.closingLogger) {
      this.closingLogger.log(`✅ Opened ${openResult.tradesSucceeded} trades (${openResult.tradesFailed} failed)`);
      for (const fired of openResult.firedTrades) {
        const status = fired.dealId ? 'confirmed' : (fired.error ? 'failed' : 'pending');
        this.closingLogger.log(`   ${fired.trade.epic}: ${fired.trade.direction} ${fired.trade.positionSize} @ ${fired.entryPrice || 'N/A'} (${status})`);
      }
    }
  }

  /**
   * Check if daily candle refresh should be triggered
   */
  private async checkDailyRefreshWindow(now: Date): Promise<void> {
    if (!this.settings) return;
    
    const windowKey = `refresh_${now.toDateString()}`;
    
    // Already triggered today?
    if (this.triggeredWindows.has(windowKey)) {
      return;
    }
    
    // Parse refresh time (e.g., "21:00:00")
    const [hours, minutes, seconds] = this.settings.dailyRefreshTime.split(':').map(Number);
    const refreshTime = new Date(now);
    refreshTime.setHours(hours, minutes, seconds || 0, 0);
    
    // Within 1 second of refresh time?
    const timeDiff = Math.abs(now.getTime() - refreshTime.getTime());
    if (timeDiff <= 1000) {
      this.triggeredWindows.add(windowKey);
      await this.triggerDailyRefresh(now);
    }
  }

  /**
   * Trigger daily candle data refresh for all epics
   */
  private async triggerDailyRefresh(now: Date): Promise<void> {
    await orchestrationLogger.info('WINDOW_TRIGGERED', 'Daily candle refresh window triggered', {
      data: {
        refreshTime: this.settings!.dailyRefreshTime,
        timestamp: now.toISOString(),
      },
    });
    
    try {
      // Get all active epics
      const { getDb } = await import('../db');
      const { epics } = await import('../../drizzle/schema');
      const { sql } = await import('drizzle-orm');
      
      const db = await getDb();
      if (!db) {
        await orchestrationLogger.warn('DATA_SYNC_STARTED', 'Database not available for refresh');
        return;
      }
      
      const activeEpics = await db.select().from(epics);
      
      await orchestrationLogger.info('DATA_SYNC_STARTED', `Refreshing candle data for ${activeEpics.length} epics`);
      
      // Refresh each epic sequentially (to avoid overwhelming AV API)
      for (const epic of activeEpics) {
        try {
          await orchestrationLogger.info('DATA_SYNC_STARTED', `Refreshing ${epic.symbol}...`);
          
          // Call existing sync_epic_data.py
          const { spawnPython } = await import('../python_spawn');
          
          const result = await new Promise<{stdout: string, stderr: string}>((resolve, reject) => {
            const process = spawnPython('python_engine/sync_epic_data.py', {
              args: [epic.symbol]
            });
            
            let stdout = '';
            let stderr = '';
            
            process.stdout?.on('data', (data) => {
              stdout += data.toString();
            });
            
            process.stderr?.on('data', (data) => {
              stderr += data.toString();
            });
            
            process.on('close', (code) => {
              if (code === 0) {
                resolve({ stdout, stderr });
              } else {
                reject(new Error(`Process exited with code ${code}: ${stderr}`));
              }
            });
          });
          
          await orchestrationLogger.info('DATA_SYNC_COMPLETED', `✅ ${epic.symbol} refreshed`, {
            data: { output: result.stdout },
          });
          
        } catch (error: any) {
          await orchestrationLogger.logError('DATA_SYNC_FAILED', `Failed to refresh ${epic.symbol}`, error, {
            data: { epic: epic.symbol },
          });
        }
      }
      
      await orchestrationLogger.info('DATA_SYNC_COMPLETED', 'Daily candle refresh completed');
      
    } catch (error: any) {
      await orchestrationLogger.logError('DATA_SYNC_FAILED', 'Daily refresh failed', error);
    }
  }
}

// Export singleton instance
export const orchestrationTimer = OrchestrationTimer.getInstance();
