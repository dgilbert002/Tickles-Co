/**
 * DNA Brain Accumulator
 * 
 * Stores brain calculation results as they arrive from different timing modes.
 * Each DNA strand in a strategy can have its own timing mode:
 * - T5BeforeClose (T-300s)
 * - T4BeforeClose (T-240s) 
 * - Fake5min_3rdCandle_API (~T-120s, WebSocket triggered)
 * - Fake5min_4thCandle (T-60s, REST API triggered)
 * - MarketClose (T-0s)
 * 
 * Results accumulate as each DNA's timing triggers.
 * Final conflict resolution happens at T-60s when all DNA strands complete.
 */

import { orchestrationLogger } from './logger';

/**
 * Normalize time string to HH:MM:SS format for consistent key lookups
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

export interface DNABrainResult {
  dnaStrandId: string;
  indicatorName: string;
  epic: string;
  timeframe: string;
  timingMode: string;
  dataSource: string;
  
  // Result
  signal: 'BUY' | 'HOLD';
  triggeredAt: Date;
  
  // Performance metrics (for conflict resolution)
  sharpe?: number;
  totalReturn?: number;
  winRate?: number;
  maxDrawdown?: number;
  
  // Trade parameters (if BUY)
  leverage?: number;
  stopLoss?: number;
  crashProtectionEnabled?: boolean;
  
  // Raw indicator output
  indicatorValue?: number;
  indicatorParams?: Record<string, any>;
}

export interface WindowAccumulator {
  accountId: number;
  strategyId: number;
  windowCloseTime: string;      // "20:00:00" format
  marketCloseTime: Date;
  
  // Expected DNA strands for this window
  expectedDnaStrands: {
    dnaStrandId: string;
    indicatorName: string;
    epic: string;
    timingMode: string;
    triggeredAt?: Date;         // When it should trigger
    sourceTestId?: number;      // ID in backtestResults table (for validation)
  }[];
  
  // Accumulated results (keyed by dnaStrandId)
  dnaResults: Map<string, DNABrainResult>;
  
  // State
  createdAt: Date;
  allComplete: boolean;
  finalConflictResolutionAt?: Date;
  
  // Final winner after conflict resolution
  finalWinner?: DNABrainResult;
  conflictResolutionMetric?: string;  // sharpe, return, winRate, etc.
}

/**
 * DNA Brain Accumulator - Singleton
 * 
 * Manages brain result accumulation across timing modes.
 * Key: `${accountId}_${windowCloseTime}`
 */
class DNABrainAccumulator {
  private static instance: DNABrainAccumulator;
  
  // Active accumulators (one per account per window)
  private accumulators: Map<string, WindowAccumulator> = new Map();
  
  // Cleanup old accumulators after this many minutes
  private readonly CLEANUP_AFTER_MINUTES = 10;
  
  private constructor() {
    // Cleanup old accumulators every 5 minutes
    setInterval(() => this.cleanup(), 5 * 60 * 1000);
  }
  
  static getInstance(): DNABrainAccumulator {
    if (!DNABrainAccumulator.instance) {
      DNABrainAccumulator.instance = new DNABrainAccumulator();
    }
    return DNABrainAccumulator.instance;
  }
  
  /**
   * Create or get an accumulator for an account/window
   */
  getOrCreate(
    accountId: number,
    strategyId: number,
    windowCloseTime: string,
    marketCloseTime: Date,
    expectedDnaStrands: WindowAccumulator['expectedDnaStrands']
  ): WindowAccumulator {
    // CRITICAL: Normalize time format to prevent HH:MM vs HH:MM:SS mismatches
    const normalizedTime = normalizeTimeFormat(windowCloseTime);
    const key = `${accountId}_${normalizedTime}`;
    
    if (this.accumulators.has(key)) {
      return this.accumulators.get(key)!;
    }
    
    const accumulator: WindowAccumulator = {
      accountId,
      strategyId,
      windowCloseTime: normalizedTime,  // Store normalized
      marketCloseTime,
      expectedDnaStrands,
      dnaResults: new Map(),
      createdAt: new Date(),
      allComplete: false,
    };
    
    this.accumulators.set(key, accumulator);
    
    orchestrationLogger.debug('DNA_ACCUMULATOR', 
      `Created accumulator for account ${accountId}, window ${windowCloseTime}`, {
        data: {
          accountId,
          strategyId,
          windowCloseTime,
          expectedDnaCount: expectedDnaStrands.length,
          timingModes: expectedDnaStrands.map(d => d.timingMode),
        }
      }
    );
    
    return accumulator;
  }
  
  /**
   * Get accumulator for a specific account and window
   * Used by immediate processing to check completion status
   */
  getAccumulator(accountId: number, windowCloseTime: string): WindowAccumulator | undefined {
    // CRITICAL: Normalize time format to match stored key
    const normalizedTime = normalizeTimeFormat(windowCloseTime);
    const key = `${accountId}_${normalizedTime}`;
    return this.accumulators.get(key);
  }
  
  /**
   * Store a DNA brain result
   * Returns true if this was the last result needed (triggers final conflict resolution)
   */
  storeResult(
    accountId: number,
    windowCloseTime: string,
    result: DNABrainResult
  ): { stored: boolean; allComplete: boolean; accumulator?: WindowAccumulator } {
    // CRITICAL: Normalize time format to match stored key
    const normalizedTime = normalizeTimeFormat(windowCloseTime);
    const key = `${accountId}_${normalizedTime}`;
    const accumulator = this.accumulators.get(key);
    
    if (!accumulator) {
      orchestrationLogger.warn('DNA_ACCUMULATOR', 
        `No accumulator found for account ${accountId}, window ${windowCloseTime}`
      );
      return { stored: false, allComplete: false };
    }
    
    // Store the result
    accumulator.dnaResults.set(result.dnaStrandId, result);
    
    orchestrationLogger.info('DNA_ACCUMULATOR', 
      `Stored result for DNA ${result.indicatorName} (${result.signal})`, {
        data: {
          accountId,
          windowCloseTime,
          dnaStrandId: result.dnaStrandId,
          signal: result.signal,
          timingMode: result.timingMode,
          completedCount: accumulator.dnaResults.size,
          expectedCount: accumulator.expectedDnaStrands.length,
        }
      }
    );
    
    // Check if all DNA strands have completed
    const allComplete = accumulator.dnaResults.size >= accumulator.expectedDnaStrands.length;
    accumulator.allComplete = allComplete;
    
    return { stored: true, allComplete, accumulator };
  }
  
  /**
   * Run final conflict resolution for a window
   * Called when all DNA strands have completed (typically at T-60s)
   */
  runFinalConflictResolution(
    accountId: number,
    windowCloseTime: string,
    conflictMetric: 'sharpe' | 'return' | 'winRate' | 'first_signal' = 'sharpe'
  ): DNABrainResult | null {
    // CRITICAL: Normalize time format to match stored key
    const normalizedTime = normalizeTimeFormat(windowCloseTime);
    const key = `${accountId}_${normalizedTime}`;
    const accumulator = this.accumulators.get(key);
    
    if (!accumulator) {
      orchestrationLogger.warn('DNA_ACCUMULATOR', 
        `No accumulator found for conflict resolution: account ${accountId}, window ${windowCloseTime}`
      );
      return null;
    }
    
    // Get all BUY signals
    const buyResults = Array.from(accumulator.dnaResults.values())
      .filter(r => r.signal === 'BUY');
    
    orchestrationLogger.info('DNA_CONFLICT', 
      `Running conflict resolution for account ${accountId}: ${buyResults.length} BUY signals`, {
        data: {
          accountId,
          windowCloseTime,
          buyCount: buyResults.length,
          holdCount: accumulator.dnaResults.size - buyResults.length,
          conflictMetric,
        }
      }
    );
    
    if (buyResults.length === 0) {
      // No BUY signals = HOLD
      accumulator.finalConflictResolutionAt = new Date();
      accumulator.conflictResolutionMetric = conflictMetric;
      
      orchestrationLogger.info('DNA_CONFLICT', 
        `Account ${accountId}: No BUY signals → HOLD`
      );
      return null;
    }
    
    if (buyResults.length === 1) {
      // Single BUY = winner
      accumulator.finalWinner = buyResults[0];
      accumulator.finalConflictResolutionAt = new Date();
      accumulator.conflictResolutionMetric = conflictMetric;
      
      orchestrationLogger.info('DNA_CONFLICT', 
        `Account ${accountId}: Single BUY → ${buyResults[0].indicatorName}`
      );
      return buyResults[0];
    }
    
    // Multiple BUY signals = conflict resolution
    let winner: DNABrainResult;
    
    switch (conflictMetric) {
      case 'sharpe':
        winner = buyResults.reduce((best, current) => 
          (current.sharpe || 0) > (best.sharpe || 0) ? current : best
        );
        break;
        
      case 'return':
        winner = buyResults.reduce((best, current) => 
          (current.totalReturn || 0) > (best.totalReturn || 0) ? current : best
        );
        break;
        
      case 'winRate':
        winner = buyResults.reduce((best, current) => 
          (current.winRate || 0) > (best.winRate || 0) ? current : best
        );
        break;
        
      case 'first_signal':
        // First signal to arrive wins
        winner = buyResults.reduce((best, current) => 
          current.triggeredAt < best.triggeredAt ? current : best
        );
        break;
        
      default:
        winner = buyResults[0];
    }
    
    accumulator.finalWinner = winner;
    accumulator.finalConflictResolutionAt = new Date();
    accumulator.conflictResolutionMetric = conflictMetric;
    
    orchestrationLogger.info('DNA_CONFLICT', 
      `Account ${accountId}: Conflict resolved → ${winner.indicatorName} (${conflictMetric}: ${winner[conflictMetric as keyof DNABrainResult]})`, {
        data: {
          accountId,
          windowCloseTime,
          winner: winner.indicatorName,
          winnerEpic: winner.epic,
          metric: conflictMetric,
          metricValue: winner[conflictMetric as keyof DNABrainResult],
          candidates: buyResults.map(r => ({
            indicator: r.indicatorName,
            value: r[conflictMetric as keyof DNABrainResult],
          })),
        }
      }
    );
    
    return winner;
  }
  
  /**
   * Get accumulator for account/window
   */
  get(accountId: number, windowCloseTime: string): WindowAccumulator | undefined {
    // CRITICAL: Normalize time format to match stored key
    const normalizedTime = normalizeTimeFormat(windowCloseTime);
    const key = `${accountId}_${normalizedTime}`;
    return this.accumulators.get(key);
  }
  
  /**
   * Get all accumulators for a specific window time (all accounts)
   */
  getAllForWindow(windowCloseTime: string): WindowAccumulator[] {
    // CRITICAL: Normalize time format for comparison
    const normalizedTime = normalizeTimeFormat(windowCloseTime);
    return Array.from(this.accumulators.values())
      .filter(a => a.windowCloseTime === normalizedTime);
  }
  
  /**
   * Get DNA strands pending for a specific timing mode
   */
  getPendingForTimingMode(timingMode: string): {
    accountId: number;
    strategyId: number;
    windowCloseTime: string;
    marketCloseTime: Date;
    dnaStrand: WindowAccumulator['expectedDnaStrands'][0];
  }[] {
    const pending: ReturnType<typeof this.getPendingForTimingMode> = [];
    
    for (const accumulator of this.accumulators.values()) {
      // Skip if already complete
      if (accumulator.allComplete) continue;
      
      for (const dna of accumulator.expectedDnaStrands) {
        // Check if this DNA matches timing mode and hasn't completed yet
        if (dna.timingMode === timingMode && !accumulator.dnaResults.has(dna.dnaStrandId)) {
          pending.push({
            accountId: accumulator.accountId,
            strategyId: accumulator.strategyId,
            windowCloseTime: accumulator.windowCloseTime,
            marketCloseTime: accumulator.marketCloseTime,
            dnaStrand: dna,
          });
        }
      }
    }
    
    return pending;
  }
  
  /**
   * Clear accumulator after trade execution
   */
  clear(accountId: number, windowCloseTime: string): void {
    const key = `${accountId}_${windowCloseTime}`;
    this.accumulators.delete(key);
    
    orchestrationLogger.debug('DNA_ACCUMULATOR', 
      `Cleared accumulator for account ${accountId}, window ${windowCloseTime}`
    );
  }
  
  /**
   * Cleanup old accumulators
   */
  private cleanup(): void {
    const cutoff = new Date(Date.now() - this.CLEANUP_AFTER_MINUTES * 60 * 1000);
    let cleaned = 0;
    
    for (const [key, accumulator] of this.accumulators) {
      if (accumulator.createdAt < cutoff) {
        this.accumulators.delete(key);
        cleaned++;
      }
    }
    
    if (cleaned > 0) {
      orchestrationLogger.debug('DNA_ACCUMULATOR', 
        `Cleaned up ${cleaned} old accumulators`
      );
    }
  }
  
  /**
   * Get debug status
   */
  getStatus(): {
    activeAccumulators: number;
    byWindow: Record<string, number>;
  } {
    const byWindow: Record<string, number> = {};
    
    for (const accumulator of this.accumulators.values()) {
      byWindow[accumulator.windowCloseTime] = (byWindow[accumulator.windowCloseTime] || 0) + 1;
    }
    
    return {
      activeAccumulators: this.accumulators.size,
      byWindow,
    };
  }
}

// Export singleton
export const dnaBrainAccumulator = DNABrainAccumulator.getInstance();


