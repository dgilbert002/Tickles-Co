/**
 * Signal Orchestrator - VERSION 3
 * 
 * Handles live trading based on indicator signals rather than fixed time windows.
 * 
 * This module:
 * 1. Monitors 5-minute candle closes (fires at XX:00:02, XX:05:02, etc.)
 * 2. Evaluates entry/exit indicators using confirmed candle data
 * 3. Uses trust matrix from attached strategy for optimal entry selection
 * 4. Executes trades through existing trade queue infrastructure
 * 5. Logs all decisions for audit trail and replay validation
 * 
 * KEY DESIGN DECISIONS:
 * - Uses API polling (not WebSocket) for confirmed candles to ensure determinism
 * - Fires 2 seconds after minute mark to allow candle confirmation
 * - Stores decision context for post-trade validation
 * - Supports both long-only and long/short modes
 */

import { orchestrationLogger } from './logger';
import { getDb, getSavedStrategyWithTrust, getSetting } from '../db';
import { accounts, actualTrades, savedStrategies } from '../../drizzle/schema';
import { eq, and, isNotNull, desc } from 'drizzle-orm';
import { connectionManager } from './connection_manager';
import { tradeQueue, type PriorityOrderType } from './trade_queue';
import { apiQueue } from './api_queue';
import { spawn } from 'child_process';
import path from 'path';

// ============================================================================
// Types
// ============================================================================

interface TrustMatrixEntry {
  entryIndicator: string;
  exitIndicator: string;
  trades: number;
  wins: number;
  totalPnl: number;
  avgPnl: number;
  sharpe: number;
  avgHoldBars: number;
  winRate: number;
  probability: number;
  optimalLeverage: number;
  optimalStopLoss: number;
}

interface EnhancedSignalConfig {
  enabled: boolean;
  entryIndicators: string[];
  exitIndicators: string[];
  entryParams: Record<string, Record<string, any>>;
  exitParams: Record<string, Record<string, any>>;
  allowShort: boolean;
  reverseOnSignal: boolean;
  stopLossMode: 'none' | 'fixed' | 'auto';
  stopLossPct?: number;
  positionSizePct: number;
  defaultLeverage: number;
  minBalanceThreshold: number;
  useTrustMatrix: boolean;
}

interface PositionState {
  direction: 'FLAT' | 'LONG' | 'SHORT';
  entryIndicator: string | null;
  entryPrice: number | null;
  entryTime: Date | null;
  contracts: number;
  leverage: number;
  dealId: string | null;
}

interface IndicatorSignal {
  indicator: string;
  signal: 'BUY' | 'SELL' | 'HOLD';
  value: number;
  params: Record<string, any>;
}

interface TradeDecision {
  action: 'ENTER_LONG' | 'ENTER_SHORT' | 'EXIT_LONG' | 'EXIT_SHORT' | 'REVERSE' | 'HOLD';
  entryIndicator?: string;
  exitIndicator?: string;
  leverage: number;
  stopLoss: number;
  contracts: number;
  reason: string;
  trustScore?: number;
  candleData: {
    open: number;
    high: number;
    low: number;
    close: number;
    timestamp: string;
  };
  firingIndicators: IndicatorSignal[];
}

// ============================================================================
// Signal Orchestrator Class
// ============================================================================

class SignalOrchestrator {
  private static instance: SignalOrchestrator;
  private isRunning: boolean = false;
  private intervalId: NodeJS.Timeout | null = null;
  
  // Track position state per account
  private positionStates: Map<number, PositionState> = new Map();
  
  // Cache strategies with trust matrix
  private strategyCache: Map<number, {
    config: EnhancedSignalConfig;
    trustMatrix: TrustMatrixEntry[];
    epic: string;
    timeframe: string;
  }> = new Map();
  
  // Track last processed candle per epic to avoid duplicate processing
  private lastProcessedCandle: Map<string, string> = new Map();
  
  private constructor() {}
  
  public static getInstance(): SignalOrchestrator {
    if (!SignalOrchestrator.instance) {
      SignalOrchestrator.instance = new SignalOrchestrator();
    }
    return SignalOrchestrator.instance;
  }
  
  // ============================================================================
  // Lifecycle Methods
  // ============================================================================
  
  /**
   * Start the signal orchestrator
   * Runs a timer that fires 2 seconds after each 5-minute mark
   */
  public async start(): Promise<void> {
    if (this.isRunning) {
      console.log('[SignalOrchestrator] Already running');
      return;
    }
    
    console.log('[SignalOrchestrator] Starting signal-based trading orchestrator');
    this.isRunning = true;
    
    // Load active signal-based strategies
    await this.loadActiveStrategies();
    
    // Initialize position states from database
    await this.initializePositionStates();
    
    // Start the 1-second tick timer
    this.intervalId = setInterval(() => this.tick(), 1000);
    
    console.log('[SignalOrchestrator] Started - monitoring for 5-minute candle closes');
  }
  
  /**
   * Stop the signal orchestrator
   */
  public stop(): void {
    if (this.intervalId) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
    this.isRunning = false;
    console.log('[SignalOrchestrator] Stopped');
  }
  
  /**
   * Check if currently running
   */
  public getStatus(): { isRunning: boolean; activeStrategies: number; trackedPositions: number } {
    return {
      isRunning: this.isRunning,
      activeStrategies: this.strategyCache.size,
      trackedPositions: this.positionStates.size,
    };
  }
  
  // ============================================================================
  // Timer Tick
  // ============================================================================
  
  /**
   * Main tick function - runs every second
   * Checks if we're at XX:00:02, XX:05:02, etc. to process candle closes
   */
  private async tick(): Promise<void> {
    const now = new Date();
    const seconds = now.getUTCSeconds();
    const minutes = now.getUTCMinutes();
    
    // Fire at :02 seconds after each 5-minute mark
    // This gives Capital.com 2 seconds to confirm the candle
    if (seconds === 2 && minutes % 5 === 0) {
      await this.processCandleClose(now);
    }
  }
  
  // ============================================================================
  // Candle Processing
  // ============================================================================
  
  /**
   * Process a 5-minute candle close
   * Called at XX:00:02, XX:05:02, XX:10:02, etc.
   */
  private async processCandleClose(timestamp: Date): Promise<void> {
    // Calculate the candle close time (2 seconds ago)
    const candleCloseTime = new Date(timestamp);
    candleCloseTime.setUTCSeconds(0);
    candleCloseTime.setUTCMilliseconds(0);
    
    const candleKey = candleCloseTime.toISOString();
    
    console.log(`[SignalOrchestrator] Processing candle close: ${candleKey}`);
    
    // Get all accounts with signal-based strategies
    const db = await getDb();
    if (!db) return;
    
    // Get active accounts with assigned strategies
    const activeAccounts = await db
      .select()
      .from(accounts)
      .where(
        and(
          eq(accounts.isActive, true),
          isNotNull(accounts.assignedStrategyId)
        )
      );
    
    // Process each account
    for (const account of activeAccounts) {
      if (!account.assignedStrategyId) continue;
      
      // Check if strategy is signal-based
      const strategy = this.strategyCache.get(account.assignedStrategyId);
      if (!strategy || !strategy.config.enabled) continue;
      
      // Skip if we already processed this candle for this epic
      const accountEpicKey = `${account.id}-${strategy.epic}-${candleKey}`;
      if (this.lastProcessedCandle.get(accountEpicKey) === candleKey) {
        continue;
      }
      this.lastProcessedCandle.set(accountEpicKey, candleKey);
      
      try {
        await this.processAccountSignals(account, strategy, candleCloseTime);
      } catch (error) {
        console.error(`[SignalOrchestrator] Error processing account ${account.id}:`, error);
      }
    }
  }
  
  /**
   * Process signals for a specific account
   */
  private async processAccountSignals(
    account: any,
    strategy: {
      config: EnhancedSignalConfig;
      trustMatrix: TrustMatrixEntry[];
      epic: string;
      timeframe: string;
    },
    candleCloseTime: Date
  ): Promise<void> {
    const { config, trustMatrix, epic, timeframe } = strategy;
    
    console.log(`[SignalOrchestrator] Account ${account.id} (${account.accountName}): evaluating ${epic} ${timeframe}`);
    
    // Get current position state
    const position = this.positionStates.get(account.id) || {
      direction: 'FLAT',
      entryIndicator: null,
      entryPrice: null,
      entryTime: null,
      contracts: 0,
      leverage: config.defaultLeverage,
      dealId: null,
    };
    
    // Fetch confirmed candle data via API
    const candle = await this.fetchConfirmedCandle(epic, timeframe, candleCloseTime, account.isDemo);
    if (!candle) {
      console.log(`[SignalOrchestrator] Account ${account.id}: No candle data available`);
      return;
    }
    
    // Evaluate all indicators
    const entrySignals = await this.evaluateIndicators(
      candle,
      config.entryIndicators,
      config.entryParams,
      epic,
      timeframe,
      'entry'
    );
    
    const exitSignals = await this.evaluateIndicators(
      candle,
      config.exitIndicators,
      config.exitParams,
      epic,
      timeframe,
      'exit'
    );
    
    // Determine action based on current position and signals
    const decision = this.determineAction(
      position,
      entrySignals,
      exitSignals,
      config,
      trustMatrix,
      candle
    );
    
    // Log the decision
    await this.logDecision(account.id, decision, candleCloseTime);
    
    // Execute the decision if not HOLD
    if (decision.action !== 'HOLD') {
      await this.executeDecision(account, decision, position, epic, candle.close);
    }
  }
  
  // ============================================================================
  // Indicator Evaluation
  // ============================================================================
  
  /**
   * Evaluate indicators using Python engine
   * Returns array of signals for each indicator
   */
  private async evaluateIndicators(
    candle: { open: number; high: number; low: number; close: number; timestamp: string },
    indicators: string[],
    params: Record<string, Record<string, any>>,
    epic: string,
    timeframe: string,
    type: 'entry' | 'exit'
  ): Promise<IndicatorSignal[]> {
    const signals: IndicatorSignal[] = [];
    
    // For now, we'll use a simplified approach
    // In production, this should call the Python indicator engine
    // TODO: Implement proper indicator evaluation via Python bridge
    
    for (const indicator of indicators) {
      const indicatorParams = params[indicator] || {};
      
      // Placeholder: actual implementation would call Python
      // The Python engine would load historical candles and compute the indicator
      const signal: IndicatorSignal = {
        indicator,
        signal: 'HOLD', // Default to HOLD
        value: 0,
        params: indicatorParams,
      };
      
      signals.push(signal);
    }
    
    return signals;
  }
  
  /**
   * Determine action based on position and signals
   */
  private determineAction(
    position: PositionState,
    entrySignals: IndicatorSignal[],
    exitSignals: IndicatorSignal[],
    config: EnhancedSignalConfig,
    trustMatrix: TrustMatrixEntry[],
    candle: { open: number; high: number; low: number; close: number; timestamp: string }
  ): TradeDecision {
    // Find firing entry indicators (BUY signals)
    const firingEntries = entrySignals.filter(s => s.signal === 'BUY');
    
    // Find firing exit indicators (SELL signals)
    const firingExits = exitSignals.filter(s => s.signal === 'SELL');
    
    // Default decision: HOLD
    const decision: TradeDecision = {
      action: 'HOLD',
      leverage: config.defaultLeverage,
      stopLoss: config.stopLossPct || 0,
      contracts: 0,
      reason: 'No signals',
      candleData: candle,
      firingIndicators: [...firingEntries, ...firingExits],
    };
    
    // Case 1: Currently FLAT
    if (position.direction === 'FLAT') {
      if (firingEntries.length > 0) {
        // Select best entry using trust matrix
        const bestEntry = config.useTrustMatrix
          ? this.selectBestEntry(firingEntries, config.exitIndicators, trustMatrix, config.defaultLeverage)
          : { indicator: firingEntries[0].indicator, leverage: config.defaultLeverage, stopLoss: config.stopLossPct || 0, score: 0 };
        
        decision.action = 'ENTER_LONG';
        decision.entryIndicator = bestEntry.indicator;
        decision.leverage = bestEntry.leverage;
        decision.stopLoss = bestEntry.stopLoss;
        decision.trustScore = bestEntry.score;
        decision.reason = `Entry signal from ${bestEntry.indicator} (trust: ${bestEntry.score.toFixed(2)})`;
      }
    }
    
    // Case 2: Currently LONG
    else if (position.direction === 'LONG') {
      if (firingExits.length > 0) {
        // Find which exit indicator fired
        const exitIndicator = firingExits[0].indicator;
        
        if (config.reverseOnSignal && config.allowShort) {
          // Reverse to SHORT
          const bestEntry = config.useTrustMatrix
            ? this.selectBestEntry(firingExits.map(e => ({ ...e, signal: 'BUY' as const })), config.entryIndicators, trustMatrix, config.defaultLeverage)
            : { indicator: exitIndicator, leverage: config.defaultLeverage, stopLoss: config.stopLossPct || 0, score: 0 };
          
          decision.action = 'REVERSE';
          decision.exitIndicator = exitIndicator;
          decision.entryIndicator = bestEntry.indicator;
          decision.leverage = bestEntry.leverage;
          decision.stopLoss = bestEntry.stopLoss;
          decision.trustScore = bestEntry.score;
          decision.reason = `Reverse from LONG to SHORT on ${exitIndicator}`;
        } else {
          // Just exit
          decision.action = 'EXIT_LONG';
          decision.exitIndicator = exitIndicator;
          decision.reason = `Exit LONG on ${exitIndicator}`;
        }
      }
    }
    
    // Case 3: Currently SHORT
    else if (position.direction === 'SHORT') {
      if (firingEntries.length > 0) {
        // Entry signal while short = exit short
        const entryIndicator = firingEntries[0].indicator;
        
        if (config.reverseOnSignal) {
          // Reverse to LONG
          const bestEntry = config.useTrustMatrix
            ? this.selectBestEntry(firingEntries, config.exitIndicators, trustMatrix, config.defaultLeverage)
            : { indicator: entryIndicator, leverage: config.defaultLeverage, stopLoss: config.stopLossPct || 0, score: 0 };
          
          decision.action = 'REVERSE';
          decision.exitIndicator = position.entryIndicator || undefined;
          decision.entryIndicator = bestEntry.indicator;
          decision.leverage = bestEntry.leverage;
          decision.stopLoss = bestEntry.stopLoss;
          decision.trustScore = bestEntry.score;
          decision.reason = `Reverse from SHORT to LONG on ${entryIndicator}`;
        } else {
          // Just exit
          decision.action = 'EXIT_SHORT';
          decision.entryIndicator = entryIndicator;
          decision.reason = `Exit SHORT on ${entryIndicator}`;
        }
      }
    }
    
    return decision;
  }
  
  /**
   * Select best entry indicator using trust matrix Expected Value
   */
  private selectBestEntry(
    firingEntries: IndicatorSignal[],
    exitIndicators: string[],
    trustMatrix: TrustMatrixEntry[],
    defaultLeverage: number
  ): { indicator: string; leverage: number; stopLoss: number; score: number } {
    let bestEntry = firingEntries[0].indicator;
    let bestScore = -Infinity;
    let bestLeverage = defaultLeverage;
    let bestStopLoss = 0;
    
    for (const entry of firingEntries) {
      // Calculate Expected Value across all exit indicators
      let totalEV = 0;
      let totalProb = 0;
      let pairLeverage = defaultLeverage;
      let pairStopLoss = 0;
      let pairCount = 0;
      
      for (const exitInd of exitIndicators) {
        const pair = trustMatrix.find(
          t => t.entryIndicator === entry.indicator && t.exitIndicator === exitInd
        );
        
        if (pair && pair.trades > 0) {
          // EV = probability * avgPnl
          const ev = pair.probability * pair.avgPnl;
          totalEV += ev;
          totalProb += pair.probability;
          pairLeverage = pair.optimalLeverage || defaultLeverage;
          pairStopLoss = pair.optimalStopLoss || 0;
          pairCount++;
        }
      }
      
      // Average score
      const score = pairCount > 0 ? totalEV / pairCount : 0;
      
      if (score > bestScore) {
        bestScore = score;
        bestEntry = entry.indicator;
        bestLeverage = pairLeverage;
        bestStopLoss = pairStopLoss;
      }
    }
    
    return {
      indicator: bestEntry,
      leverage: bestLeverage,
      stopLoss: bestStopLoss,
      score: bestScore,
    };
  }
  
  // ============================================================================
  // Trade Execution
  // ============================================================================
  
  /**
   * Execute a trade decision
   */
  private async executeDecision(
    account: any,
    decision: TradeDecision,
    position: PositionState,
    epic: string,
    currentPrice: number
  ): Promise<void> {
    console.log(`[SignalOrchestrator] Account ${account.id}: Executing ${decision.action}`);
    
    // Get API client
    const apiClient = await connectionManager.getClient(account.isDemo ? 'demo' : 'live');
    if (!apiClient) {
      console.error(`[SignalOrchestrator] No API client available for account ${account.id}`);
      return;
    }
    
    try {
      switch (decision.action) {
        case 'ENTER_LONG':
          await this.enterPosition(account, epic, 'BUY', decision, currentPrice);
          break;
          
        case 'ENTER_SHORT':
          await this.enterPosition(account, epic, 'SELL', decision, currentPrice);
          break;
          
        case 'EXIT_LONG':
        case 'EXIT_SHORT':
          await this.exitPosition(account, position);
          break;
          
        case 'REVERSE':
          await this.exitPosition(account, position);
          const newDirection = position.direction === 'LONG' ? 'SELL' : 'BUY';
          await this.enterPosition(account, epic, newDirection, decision, currentPrice);
          break;
      }
    } catch (error) {
      console.error(`[SignalOrchestrator] Trade execution failed for account ${account.id}:`, error);
    }
  }
  
  /**
   * Enter a new position
   */
  private async enterPosition(
    account: any,
    epic: string,
    direction: 'BUY' | 'SELL',
    decision: TradeDecision,
    currentPrice: number
  ): Promise<void> {
    // Calculate position size based on available balance
    const availableBalance = account.balance?.available || account.balance || 0;
    const allocationAmount = availableBalance * (decision.contracts > 0 ? 1 : 0.5); // Use positionSizePct from decision
    
    // Queue the trade for execution
    const orderPayload = {
      epic,
      direction,
      size: decision.contracts || this.calculateContracts(allocationAmount, currentPrice, decision.leverage),
      stopLoss: decision.stopLoss > 0 ? {
        distance: decision.stopLoss * currentPrice / 100,
        trailingStop: false,
      } : undefined,
      guaranteedStop: false,
    };
    
    console.log(`[SignalOrchestrator] Account ${account.id}: Queuing ${direction} order`, orderPayload);
    
    // Update position state
    this.positionStates.set(account.id, {
      direction: direction === 'BUY' ? 'LONG' : 'SHORT',
      entryIndicator: decision.entryIndicator || null,
      entryPrice: currentPrice,
      entryTime: new Date(),
      contracts: orderPayload.size,
      leverage: decision.leverage,
      dealId: null, // Will be updated when trade executes
    });
    
    // TODO: Queue trade via tradeQueue
    // For now, log the intent
    console.log(`[SignalOrchestrator] TRADE INTENT: ${account.accountName} ${direction} ${epic} @ ${currentPrice}`);
  }
  
  /**
   * Exit current position
   */
  private async exitPosition(account: any, position: PositionState): Promise<void> {
    if (!position.dealId) {
      console.warn(`[SignalOrchestrator] Account ${account.id}: No deal ID to close`);
      return;
    }
    
    console.log(`[SignalOrchestrator] Account ${account.id}: Closing position ${position.dealId}`);
    
    // Reset position state
    this.positionStates.set(account.id, {
      direction: 'FLAT',
      entryIndicator: null,
      entryPrice: null,
      entryTime: null,
      contracts: 0,
      leverage: 1,
      dealId: null,
    });
    
    // TODO: Close via API
    console.log(`[SignalOrchestrator] CLOSE INTENT: ${account.accountName} dealId=${position.dealId}`);
  }
  
  /**
   * Calculate number of contracts based on allocation and leverage
   */
  private calculateContracts(allocationAmount: number, price: number, leverage: number): number {
    // Notional value = allocation * leverage
    const notionalValue = allocationAmount * leverage;
    // Contracts = notional / price
    return Math.floor((notionalValue / price) * 100) / 100; // Round to 2 decimal places
  }
  
  // ============================================================================
  // Data Fetching
  // ============================================================================
  
  /**
   * Fetch confirmed candle data from API
   */
  private async fetchConfirmedCandle(
    epic: string,
    timeframe: string,
    candleCloseTime: Date,
    isDemo: boolean
  ): Promise<{ open: number; high: number; low: number; close: number; timestamp: string } | null> {
    try {
      // Get API client
      const mode = isDemo ? 'demo' : 'live';
      const apiClient = await connectionManager.getClient(mode);
      if (!apiClient) return null;
      
      // Fetch recent candles
      const resolution = timeframe === '5m' ? 'MINUTE_5' : timeframe === '1m' ? 'MINUTE' : 'MINUTE_5';
      
      // Use apiQueue to rate limit
      const candles = await apiQueue.add(async () => {
        const { CapitalComAPI } = await import('../live_trading/capital_api');
        return await CapitalComAPI.getHistoricalPrices(mode, epic, resolution, 5);
      });
      
      if (!candles || candles.length === 0) return null;
      
      // Find the candle matching our close time
      const targetTime = candleCloseTime.toISOString();
      const candle = candles.find(c => {
        const candleTime = new Date(c.snapshotTimeUTC || c.snapshotTime);
        return Math.abs(candleTime.getTime() - candleCloseTime.getTime()) < 60000; // Within 1 minute
      });
      
      if (!candle) {
        console.log(`[SignalOrchestrator] No matching candle for ${epic} at ${targetTime}`);
        return null;
      }
      
      return {
        open: candle.openPrice?.bid || candle.open || 0,
        high: candle.highPrice?.bid || candle.high || 0,
        low: candle.lowPrice?.bid || candle.low || 0,
        close: candle.closePrice?.bid || candle.close || 0,
        timestamp: candle.snapshotTimeUTC || candle.snapshotTime || targetTime,
      };
    } catch (error) {
      console.error(`[SignalOrchestrator] Error fetching candle for ${epic}:`, error);
      return null;
    }
  }
  
  // ============================================================================
  // Logging & Audit
  // ============================================================================
  
  /**
   * Log trade decision to database for audit trail
   */
  private async logDecision(
    accountId: number,
    decision: TradeDecision,
    candleCloseTime: Date
  ): Promise<void> {
    const db = await getDb();
    if (!db) return;
    
    try {
      // Store in execution_logs or a new signal_decisions table
      // For now, log to console with full context
      console.log(`[SignalOrchestrator] Decision Log:`, {
        accountId,
        timestamp: candleCloseTime.toISOString(),
        action: decision.action,
        entryIndicator: decision.entryIndicator,
        exitIndicator: decision.exitIndicator,
        leverage: decision.leverage,
        stopLoss: decision.stopLoss,
        trustScore: decision.trustScore,
        reason: decision.reason,
        candleClose: decision.candleData.close,
        firingIndicators: decision.firingIndicators.length,
      });
      
      // TODO: Insert into signal_decisions table for persistence
    } catch (error) {
      console.error(`[SignalOrchestrator] Failed to log decision:`, error);
    }
  }
  
  // ============================================================================
  // Initialization
  // ============================================================================
  
  /**
   * Load active signal-based strategies into cache
   */
  private async loadActiveStrategies(): Promise<void> {
    const db = await getDb();
    if (!db) return;
    
    try {
      // Get all accounts with signal-based strategies
      const activeAccounts = await db
        .select()
        .from(accounts)
        .where(
          and(
            eq(accounts.isActive, true),
            isNotNull(accounts.assignedStrategyId)
          )
        );
      
      for (const account of activeAccounts) {
        if (!account.assignedStrategyId) continue;
        
        // Load strategy with trust matrix
        const strategy = await getSavedStrategyWithTrust(account.assignedStrategyId);
        if (!strategy || !strategy.enhancedSignalConfig) continue;
        
        const config = strategy.enhancedSignalConfig as EnhancedSignalConfig;
        if (!config.enabled) continue;
        
        // Get epic from DNA strands
        const dnaStrands = strategy.dnaStrands as any[];
        const epic = dnaStrands?.[0]?.epic || 'SOXL';
        const timeframe = dnaStrands?.[0]?.timeframe || '5m';
        
        this.strategyCache.set(account.assignedStrategyId, {
          config,
          trustMatrix: (strategy.trustMatrixSnapshot || []) as TrustMatrixEntry[],
          epic,
          timeframe,
        });
        
        console.log(`[SignalOrchestrator] Loaded strategy ${strategy.name} for account ${account.accountName}`);
      }
      
      console.log(`[SignalOrchestrator] Loaded ${this.strategyCache.size} signal-based strategies`);
    } catch (error) {
      console.error('[SignalOrchestrator] Error loading strategies:', error);
    }
  }
  
  /**
   * Initialize position states from existing open positions
   */
  private async initializePositionStates(): Promise<void> {
    const db = await getDb();
    if (!db) return;
    
    try {
      // Get open positions from actual_trades
      const openPositions = await db
        .select()
        .from(actualTrades)
        .where(eq(actualTrades.status, 'OPEN'));
      
      for (const trade of openPositions) {
        this.positionStates.set(trade.accountId, {
          direction: trade.direction === 'BUY' ? 'LONG' : 'SHORT',
          entryIndicator: trade.indicatorName || null,
          entryPrice: trade.entryPrice || null,
          entryTime: trade.entryTime ? new Date(trade.entryTime) : null,
          contracts: trade.contracts || 0,
          leverage: trade.leverage || 1,
          dealId: trade.dealId || null,
        });
      }
      
      console.log(`[SignalOrchestrator] Initialized ${this.positionStates.size} position states`);
    } catch (error) {
      console.error('[SignalOrchestrator] Error initializing positions:', error);
    }
  }
  
  /**
   * Refresh strategies from database
   * Call this when strategies are updated
   */
  public async refreshStrategies(): Promise<void> {
    this.strategyCache.clear();
    await this.loadActiveStrategies();
  }
  
  // ============================================================================
  // Trade Validation (Replay/Audit)
  // ============================================================================
  
  /**
   * Validate a recent trade by replaying the indicator evaluation
   * 
   * This function:
   * 1. Takes a trade ID and its recorded decision
   * 2. Re-fetches historical candle data (now confirmed)
   * 3. Re-runs indicator evaluation with the same parameters
   * 4. Compares results and returns validation status
   * 
   * Purpose: Ensure live decisions match what historical replay would show
   * This catches issues like:
   * - WebSocket data delays/misses
   * - Indicator calculation bugs
   * - Clock sync issues
   * 
   * @param tradeId - ID of the actual_trade to validate
   * @param delayMinutes - How long after the trade to wait before validating (default: 15)
   * @returns Validation result with match status and details
   */
  public async validateRecentTrade(
    tradeId: number,
    delayMinutes: number = 15
  ): Promise<{
    isValid: boolean;
    originalDecision: string;
    replayDecision: string;
    match: boolean;
    discrepancies: string[];
    validatedAt: Date;
    candleData: {
      original: { close: number; timestamp: string } | null;
      replay: { close: number; timestamp: string } | null;
    };
    indicatorComparison: Array<{
      indicator: string;
      originalSignal: string;
      replaySignal: string;
      match: boolean;
    }>;
  }> {
    const db = await getDb();
    if (!db) {
      throw new Error('Database not available');
    }
    
    const discrepancies: string[] = [];
    
    try {
      // Get the trade record
      const trades = await db
        .select()
        .from(actualTrades)
        .where(eq(actualTrades.id, tradeId))
        .limit(1);
      
      if (trades.length === 0) {
        throw new Error(`Trade ${tradeId} not found`);
      }
      
      const trade = trades[0];
      
      // Get account and strategy
      const accountResults = await db
        .select()
        .from(accounts)
        .where(eq(accounts.id, trade.accountId))
        .limit(1);
      
      if (accountResults.length === 0) {
        throw new Error(`Account ${trade.accountId} not found`);
      }
      
      const account = accountResults[0];
      
      if (!account.assignedStrategyId) {
        throw new Error(`Account ${trade.accountId} has no strategy`);
      }
      
      // Get cached strategy
      const strategy = this.strategyCache.get(account.assignedStrategyId);
      if (!strategy || !strategy.config.enabled) {
        throw new Error(`Strategy ${account.assignedStrategyId} not found or not signal-based`);
      }
      
      const { config, trustMatrix, epic, timeframe } = strategy;
      
      // Determine the candle close time from the trade
      // The trade's createdAt should be shortly after the candle close that triggered it
      const tradeTime = new Date(trade.createdAt);
      const candleCloseTime = new Date(tradeTime);
      // Round down to the nearest 5-minute mark
      candleCloseTime.setUTCMinutes(Math.floor(candleCloseTime.getUTCMinutes() / 5) * 5);
      candleCloseTime.setUTCSeconds(0);
      candleCloseTime.setUTCMilliseconds(0);
      
      console.log(`[Validation] Trade ${tradeId}: Validating decision at candle ${candleCloseTime.toISOString()}`);
      
      // Fetch the historical candle (should now be fully confirmed)
      const replayCandle = await this.fetchConfirmedCandle(
        epic,
        timeframe,
        candleCloseTime,
        account.isDemo
      );
      
      // Get original candle data from trade metadata (if stored)
      const originalCandle = trade.indicatorParams as any; // May contain candleData
      
      // Re-evaluate indicators
      const replayEntrySignals = await this.evaluateIndicators(
        replayCandle || { open: 0, high: 0, low: 0, close: 0, timestamp: candleCloseTime.toISOString() },
        config.entryIndicators,
        config.entryParams,
        epic,
        timeframe,
        'entry'
      );
      
      const replayExitSignals = await this.evaluateIndicators(
        replayCandle || { open: 0, high: 0, low: 0, close: 0, timestamp: candleCloseTime.toISOString() },
        config.exitIndicators,
        config.exitParams,
        epic,
        timeframe,
        'exit'
      );
      
      // Get position state at the time (simplified - assume based on trade direction)
      const positionAtTime: PositionState = {
        direction: 'FLAT', // Assume flat before entry
        entryIndicator: null,
        entryPrice: null,
        entryTime: null,
        contracts: 0,
        leverage: config.defaultLeverage,
        dealId: null,
      };
      
      // Re-determine what the decision should be
      const replayDecision = this.determineAction(
        positionAtTime,
        replayEntrySignals,
        replayExitSignals,
        config,
        trustMatrix,
        replayCandle || { open: 0, high: 0, low: 0, close: 0, timestamp: candleCloseTime.toISOString() }
      );
      
      // Compare original trade action with replay
      const originalAction = trade.direction === 'BUY' ? 'ENTER_LONG' : 
                            trade.direction === 'SELL' ? 'ENTER_SHORT' : 'HOLD';
      
      const actionMatch = originalAction === replayDecision.action || 
                         (originalAction === 'ENTER_LONG' && replayDecision.action === 'ENTER_LONG') ||
                         (originalAction === 'ENTER_SHORT' && replayDecision.action === 'ENTER_SHORT');
      
      if (!actionMatch) {
        discrepancies.push(`Action mismatch: original=${originalAction}, replay=${replayDecision.action}`);
      }
      
      // Compare indicator signals
      const indicatorComparison: Array<{
        indicator: string;
        originalSignal: string;
        replaySignal: string;
        match: boolean;
      }> = [];
      
      // Check if the indicator that triggered the trade matches
      if (trade.indicatorName) {
        const replaySignal = replayEntrySignals.find(s => s.indicator === trade.indicatorName);
        if (replaySignal) {
          const match = replaySignal.signal === 'BUY';
          indicatorComparison.push({
            indicator: trade.indicatorName,
            originalSignal: 'BUY',
            replaySignal: replaySignal.signal,
            match,
          });
          if (!match) {
            discrepancies.push(`Indicator ${trade.indicatorName} signal mismatch: original=BUY, replay=${replaySignal.signal}`);
          }
        }
      }
      
      // Compare candle close prices
      if (replayCandle && originalCandle?.close) {
        const priceDiff = Math.abs(replayCandle.close - originalCandle.close);
        const priceDiffPct = (priceDiff / originalCandle.close) * 100;
        if (priceDiffPct > 0.1) { // More than 0.1% difference
          discrepancies.push(`Candle close price mismatch: original=${originalCandle.close}, replay=${replayCandle.close} (${priceDiffPct.toFixed(3)}%)`);
        }
      }
      
      const isValid = discrepancies.length === 0;
      
      // Log validation result
      console.log(`[Validation] Trade ${tradeId}: ${isValid ? '✅ VALID' : '❌ INVALID'}`);
      if (!isValid) {
        console.log(`[Validation] Discrepancies:`, discrepancies);
      }
      
      return {
        isValid,
        originalDecision: originalAction,
        replayDecision: replayDecision.action,
        match: actionMatch,
        discrepancies,
        validatedAt: new Date(),
        candleData: {
          original: originalCandle ? { close: originalCandle.close, timestamp: originalCandle.timestamp } : null,
          replay: replayCandle ? { close: replayCandle.close, timestamp: replayCandle.timestamp } : null,
        },
        indicatorComparison,
      };
    } catch (error) {
      console.error(`[Validation] Error validating trade ${tradeId}:`, error);
      throw error;
    }
  }
  
  /**
   * Schedule validation for a trade (runs after delay)
   * @param tradeId - Trade ID to validate
   * @param delayMs - Milliseconds to wait before validating (default: 15 minutes)
   */
  public scheduleValidation(tradeId: number, delayMs: number = 15 * 60 * 1000): void {
    console.log(`[SignalOrchestrator] Scheduling validation for trade ${tradeId} in ${delayMs / 1000 / 60} minutes`);
    
    setTimeout(async () => {
      try {
        const result = await this.validateRecentTrade(tradeId);
        
        // Store validation result in database
        const db = await getDb();
        if (db) {
          await db.update(actualTrades)
            .set({
              // Store validation result in a JSON field or separate column
              // For now, log it
            })
            .where(eq(actualTrades.id, tradeId));
        }
        
        // Log results
        if (!result.isValid) {
          console.warn(`[Validation] ⚠️ Trade ${tradeId} FAILED validation:`, result.discrepancies);
        } else {
          console.log(`[Validation] ✅ Trade ${tradeId} passed validation`);
        }
      } catch (error) {
        console.error(`[Validation] Failed to validate trade ${tradeId}:`, error);
      }
    }, delayMs);
  }
  
  /**
   * Batch validate all recent trades (useful for auditing)
   * @param hoursBack - How many hours back to check
   * @returns Array of validation results
   */
  public async validateRecentTrades(hoursBack: number = 24): Promise<{
    total: number;
    valid: number;
    invalid: number;
    results: Array<{ tradeId: number; isValid: boolean; discrepancies: string[] }>;
  }> {
    const db = await getDb();
    if (!db) {
      throw new Error('Database not available');
    }
    
    const cutoffTime = new Date();
    cutoffTime.setHours(cutoffTime.getHours() - hoursBack);
    
    // Get recent signal-based trades
    const { gte } = await import('drizzle-orm');
    const recentTrades = await db
      .select({ id: actualTrades.id })
      .from(actualTrades)
      .where(
        and(
          gte(actualTrades.createdAt, cutoffTime),
          // Filter for signal-based trades only (check indicatorParams for signal config)
          isNotNull(actualTrades.indicatorName)
        )
      );
    
    const results: Array<{ tradeId: number; isValid: boolean; discrepancies: string[] }> = [];
    let valid = 0;
    let invalid = 0;
    
    for (const trade of recentTrades) {
      try {
        const result = await this.validateRecentTrade(trade.id);
        results.push({
          tradeId: trade.id,
          isValid: result.isValid,
          discrepancies: result.discrepancies,
        });
        if (result.isValid) valid++;
        else invalid++;
      } catch (error) {
        console.error(`[Validation] Error validating trade ${trade.id}:`, error);
        results.push({
          tradeId: trade.id,
          isValid: false,
          discrepancies: [`Validation error: ${error}`],
        });
        invalid++;
      }
    }
    
    console.log(`[Validation] Batch complete: ${valid}/${recentTrades.length} valid, ${invalid} invalid`);
    
    return {
      total: recentTrades.length,
      valid,
      invalid,
      results,
    };
  }
}

// Export singleton instance
export const signalOrchestrator = SignalOrchestrator.getInstance();
