/**
 * Trade Validation Service
 * 
 * COMPREHENSIVE VALIDATION: Re-runs the ENTIRE brain calculation (all DNA strands)
 * using historical candle data to validate that the same winner would be chosen.
 * 
 * What we validate:
 * 1. Run ALL DNA strands on the stored candle range (using real candles, not fake T-60)
 * 2. Apply the SAME conflict resolution metric
 * 3. Check if the SAME DNA wins
 * 4. Compare: winning indicator, params, signals
 * 
 * ALL DATA IS FROM CAPITAL.COM IN UTC.
 */

import { getDb } from '../db';
import { actualTrades, validatedTrades, tradeComparisons, backtestResults, candles, savedStrategies } from '../../drizzle/schema';
import { eq, and, desc, between } from 'drizzle-orm';
import { getCurrentSignal, SignalConfig, SignalResult, batchValidateSignals, BatchDnaConfig, BatchValidateResult } from '../signal_bridge';

/**
 * DNA result from re-running the signal calculation
 */
export interface RerunDnaResult {
  testIndex: number;
  indicatorName: string;
  indicatorParams: Record<string, any>;
  epic: string;
  timeframe: string;
  signal: 'BUY' | 'HOLD';
  indicatorValue: number | null;
  sharpeRatio: number | null;
  totalReturn: number | null;
  winRate: number | null;
  maxDrawdown: number | null;
}

/**
 * Result of validating a single trade
 */
export interface ValidationResult {
  actualTradeId: number;
  status: 'validated' | 'signal_mismatch' | 'winner_mismatch' | 'data_not_ready' | 'error';
  
  // Original trade details
  originalEpic: string;
  originalIndicatorName: string;
  originalIndicatorParams: Record<string, any>;
  originalLeverage: number;
  originalStopLoss: number | null;
  originalEntryPrice: number | null;
  originalTradeDate: string;
  originalCandleCount: number;
  
  // Original brain calculation (from stored allDnaResults)
  originalAllDnaResults: {
    dnaResults: RerunDnaResult[];
    conflictResolution: {
      metric: string;
      winnerIndex: number;
      reason: string;
      hadConflict: boolean;
    };
  } | null;
  
  // Re-run results (using real historical candles)
  rerunAllDnaResults: {
    dnaResults: RerunDnaResult[];
    conflictResolution: {
      metric: string;
      winnerIndex: number;
      reason: string;
      hadConflict: boolean;
    };
  } | null;
  
  // Winning DNA comparison
  rerunWinningIndicator: string | null;
  rerunWinningSignal: 'BUY' | 'HOLD' | null;
  rerunWinningIndicatorValue: number | null;
  rerunCandleCount: number;
  
  // Match analysis
  winnerMatch: boolean;       // Did the SAME DNA win?
  signalMatch: boolean;       // Did the winning DNA produce the same signal?
  allSignalsMatch: boolean;   // Did ALL DNA strands produce the same signals?
  
  // Notes/errors
  notes: string[];
  error?: string;
}

/**
 * Validate a single actual trade by re-running the FULL brain calculation
 * 
 * @param actualTradeId - ID of the actual_trades record to validate
 * @returns ValidationResult with comprehensive comparison details
 */
export async function validateTrade(actualTradeId: number): Promise<ValidationResult> {
  const result: ValidationResult = {
    actualTradeId,
    status: 'error',
    originalEpic: '',
    originalIndicatorName: '',
    originalIndicatorParams: {},
    originalLeverage: 1,
    originalStopLoss: null,
    originalEntryPrice: null,
    originalTradeDate: '',
    originalCandleCount: 0,
    originalAllDnaResults: null,
    rerunAllDnaResults: null,
    rerunWinningIndicator: null,
    rerunWinningSignal: null,
    rerunWinningIndicatorValue: null,
    rerunCandleCount: 0,
    winnerMatch: false,
    signalMatch: false,
    allSignalsMatch: false,
    notes: [],
  };
  
  try {
    const db = await getDb();
    if (!db) {
      result.error = 'Database not available';
      return result;
    }
    
    // 1. Get the actual trade with all stored brain data
    const actualTradeRecords = await db
      .select()
      .from(actualTrades)
      .where(eq(actualTrades.id, actualTradeId))
      .limit(1);
    
    if (actualTradeRecords.length === 0) {
      result.error = `Actual trade ${actualTradeId} not found`;
      return result;
    }
    
    const actualTrade = actualTradeRecords[0];
    
    // Store original trade details
    result.originalEpic = actualTrade.epic;
    result.originalIndicatorName = actualTrade.winningIndicatorName || 'unknown';
    result.originalIndicatorParams = (actualTrade.indicatorParams as Record<string, any>) || {};
    result.originalLeverage = actualTrade.leverage;
    result.originalStopLoss = actualTrade.stopLossPrice;
    result.originalEntryPrice = actualTrade.entryPrice;
    result.originalTradeDate = actualTrade.createdAt?.toISOString().split('T')[0] || '';
    result.originalCandleCount = actualTrade.candleCount || 0;
    result.originalAllDnaResults = actualTrade.allDnaResults as any || null;
    
    console.log(`[Validation] Validating trade ${actualTradeId}:`);
    console.log(`  Epic: ${actualTrade.epic}`);
    console.log(`  Winning Indicator: ${actualTrade.winningIndicatorName}`);
    console.log(`  Conflict Resolution: ${actualTrade.conflictResolutionMetric}`);
    console.log(`  Candle Range: ${actualTrade.candleStartDate} to ${actualTrade.candleEndDate}`);
    console.log(`  Candle Count: ${actualTrade.candleCount}`);
    console.log(`  Has allDnaResults: ${!!actualTrade.allDnaResults}`);
    console.log(`  Strategy ID: ${actualTrade.strategyId}`);
    
    // FIX: Check if trade has minimum required data for validation
    if (!actualTrade.allDnaResults) {
      // If trade has a valid strategyId but no allDnaResults, we need to RE-RUN brain
      // This happens when storeBrainResult failed but the trade was later synced from Capital.com
      if (actualTrade.strategyId && actualTrade.strategyId > 0) {
        result.notes.push('⚠️ Trade missing allDnaResults but has strategyId - will attempt brain re-run validation');
        console.log(`[Validation] Trade ${actualTradeId} has strategyId ${actualTrade.strategyId} but no allDnaResults - need brain re-run`);
        
        // For now, mark as needs_brain_rerun - we can implement actual re-run later
        // The proper fix is to ensure storeBrainResult works so this case doesn't happen
        result.status = 'data_not_ready';
        result.error = `Trade synced from Capital.com without brain data. Need to re-run brain for window ${actualTrade.windowCloseTime} on ${actualTrade.createdAt?.toISOString().split('T')[0]} to determine winning DNA.`;
        result.notes.push(`💡 To fix: Re-run brain calculation for account ${actualTrade.accountId}, window ${actualTrade.windowCloseTime}`);
        console.log(`[Validation] ERROR: ${result.error}`);
        return result;
      }
      
      // Check if we have at least a winning indicator name for legacy validation
      if (!actualTrade.winningIndicatorName || actualTrade.winningIndicatorName === 'unknown') {
        result.status = 'data_not_ready';
        result.error = 'Trade missing required data: no allDnaResults, no winningIndicatorName, and strategyId=0';
        result.notes.push('⚠️ Cannot validate: Trade is missing all DNA data (likely a purely manual trade)');
        console.log(`[Validation] ERROR: ${result.error}`);
        return result;
      }
      
      result.notes.push('⚠️ Trade does not have allDnaResults stored - using limited validation');
      // Fall back to single-indicator validation
      return await validateTradeLegacy(actualTradeId, actualTrade, result);
    }
    
    const storedDnaResults = actualTrade.allDnaResults as any;
    
    // 3. Get DNA strand configurations - prefer stored snapshot, fallback to strategy
    // This allows validation to work even if the strategy has been deleted
    let dnaStrands: any[] = [];
    let usingStoredSnapshot = false;
    
    // First, try to use the stored DNA results snapshot (always available if allDnaResults exists)
    if (storedDnaResults.dnaResults && storedDnaResults.dnaResults.length > 0) {
      // Use the stored snapshot - works even if strategy deleted
      dnaStrands = storedDnaResults.dnaResults.map((dna: any) => ({
        indicatorName: dna.indicatorName,
        indicatorParams: dna.indicatorParams,
        epic: dna.epic,
        timeframe: dna.timeframe,
        // Include stored performance metrics for conflict resolution
        sharpeRatio: dna.sharpeRatio,
        totalReturn: dna.totalReturn,
        winRate: dna.winRate,
        maxDrawdown: dna.maxDrawdown,
      }));
      usingStoredSnapshot = true;
      console.log(`[Validation] Using stored DNA snapshot (${dnaStrands.length} strands)`);
    }
    
    // If no stored snapshot, try loading from strategy (for backwards compatibility)
    if (dnaStrands.length === 0) {
      // FIX: Check if strategyId is valid (not 0)
      if (!actualTrade.strategyId || actualTrade.strategyId === 0) {
        result.status = 'data_not_ready';
        result.error = 'Trade missing strategyId (is 0) and no DNA snapshot stored';
        result.notes.push('⚠️ Cannot validate: strategyId is 0 and no allDnaResults stored');
        console.log(`[Validation] ERROR: ${result.error}`);
        return result;
      }
      
      console.log(`[Validation] Looking up strategy ${actualTrade.strategyId}...`);
      const strategyRecords = await db
        .select()
        .from(savedStrategies)
        .where(eq(savedStrategies.id, actualTrade.strategyId))
        .limit(1);
      
      if (strategyRecords.length === 0) {
        result.status = 'data_not_ready';
        result.error = `Strategy ${actualTrade.strategyId} not found and no DNA snapshot stored`;
        result.notes.push(`⚠️ Cannot validate: Strategy ${actualTrade.strategyId} not found`);
        console.log(`[Validation] ERROR: ${result.error}`);
        return result;
      }
      
      const strategy = strategyRecords[0];
      dnaStrands = (strategy.dnaStrands as any[]) || [];
      
      if (dnaStrands.length === 0) {
        result.status = 'data_not_ready';
        result.error = 'Strategy has no DNA strands configured';
        result.notes.push('⚠️ Cannot validate: Strategy has no DNA strands');
        console.log(`[Validation] ERROR: ${result.error}`);
        return result;
      }
      
      console.log(`[Validation] Found ${dnaStrands.length} DNA strands from strategy`);
      console.log(`[Validation] DNA Strands Details:`);
      dnaStrands.forEach((dna, idx) => {
        console.log(`  ${idx + 1}. ${dna.indicatorName || 'N/A'} (${dna.epic || 'N/A'}, ${dna.timeframe || 'N/A'})`);
        console.log(`     Params: ${JSON.stringify(dna.indicatorParams || {})}`);
        console.log(`     Leverage: ${dna.leverage || 'N/A'}, Stop Loss: ${dna.stopLoss || 'N/A'}`);
      });
    }
    
    if (usingStoredSnapshot) {
      result.notes.push('✓ Using stored DNA snapshot (strategy-independent validation)');
    }
    
    // 4. Determine candle date range for re-run
    // Use stored range, or default to 60 days before trade date
    let startDate: string;
    let endDate: string;
    
    if (actualTrade.candleStartDate && actualTrade.candleEndDate) {
      startDate = new Date(actualTrade.candleStartDate).toISOString().split('T')[0];
      endDate = new Date(actualTrade.candleEndDate).toISOString().split('T')[0];
    } else {
      // Fallback: use 60 days before trade date
      const tradeDate = new Date(actualTrade.createdAt);
      endDate = tradeDate.toISOString().split('T')[0];
      const startDateObj = new Date(tradeDate);
      startDateObj.setDate(startDateObj.getDate() - 60);
      startDate = startDateObj.toISOString().split('T')[0];
    }
    
    console.log(`[Validation] Re-running with candle range: ${startDate} to ${endDate}`);
    
    // 5. Re-run ALL DNA strands in a SINGLE batch call (OPTIMIZED!)
    // This is 3-10x faster than calling getCurrentSignal() N times
    const batchDnaConfigs: BatchDnaConfig[] = dnaStrands.map((dna: any) => ({
      indicatorName: dna.indicatorName,
      indicatorParams: dna.indicatorParams || dna.params || {},
      epic: dna.epic || actualTrade.epic,
      timeframe: dna.timeframe || '5m',
      dataSource: 'capital',
      crashProtectionEnabled: false,
      // Include stored performance metrics for conflict resolution
      sharpeRatio: dna.sharpeRatio,
      totalReturn: dna.totalReturn,
      winRate: dna.winRate,
      maxDrawdown: dna.maxDrawdown,
    }));
    
    console.log(`[Validation] Batch evaluating ${batchDnaConfigs.length} DNA strands...`);
    
    let batchResult: BatchValidateResult;
    try {
      batchResult = await batchValidateSignals({
        dna_configs: batchDnaConfigs,
        start_date: startDate,
        end_date: endDate,
      });
    } catch (error: any) {
      console.error(`[Validation] Batch validation failed: ${error.message}`);
      result.error = `Batch validation failed: ${error.message}`;
      return result;
    }
    
    // Convert batch results to RerunDnaResult format
    const rerunResults: RerunDnaResult[] = batchResult.results.map((br, i) => {
      // Get performance metrics from original dna config (stored snapshot)
      const dna = dnaStrands[i];
      
      return {
        testIndex: br.index ?? i,
        indicatorName: br.indicatorName,
        indicatorParams: batchDnaConfigs[i]?.indicatorParams || {},
        epic: br.epic,
        timeframe: br.timeframe || '5m',
        signal: br.signal as 'BUY' | 'HOLD',
        indicatorValue: br.indicatorValue,
        // Use stored performance metrics
        sharpeRatio: dna?.sharpeRatio ?? null,
        totalReturn: dna?.totalReturn ?? null,
        winRate: dna?.winRate ?? null,
        maxDrawdown: dna?.maxDrawdown ?? null,
      };
    });
    
    console.log(`[Validation] Batch complete: ${batchResult.dnaWithBuy}/${batchResult.totalDna} signaled BUY`);
    
    result.rerunCandleCount = batchResult.results[0]?.candlesLoaded || 0;
    
    // 6. Apply conflict resolution to find the winner
    const conflictMode = actualTrade.conflictResolutionMetric || 'sharpeRatio';
    const buySignals = rerunResults.filter(r => r.signal === 'BUY');
    
    let rerunWinnerIndex = -1;
    let rerunWinnerReason = 'No BUY signals';
    
    if (buySignals.length === 0) {
      rerunWinnerReason = 'No DNA produced BUY signal';
    } else if (buySignals.length === 1) {
      rerunWinnerIndex = buySignals[0].testIndex;
      rerunWinnerReason = 'Only one DNA signaled BUY';
    } else {
      // Multiple BUY signals - apply conflict resolution
      rerunWinnerIndex = resolveConflict(buySignals, conflictMode);
      const winner = rerunResults[rerunWinnerIndex];
      
      if (conflictMode.includes('sharpe')) {
        rerunWinnerReason = `Best Sharpe Ratio (${winner.sharpeRatio?.toFixed(2) || 'N/A'})`;
      } else if (conflictMode.includes('return')) {
        rerunWinnerReason = `Best Return (${winner.totalReturn?.toFixed(2) || 'N/A'}%)`;
      } else if (conflictMode.includes('win')) {
        rerunWinnerReason = `Best Win Rate (${winner.winRate?.toFixed(2) || 'N/A'}%)`;
      } else {
        rerunWinnerReason = `${conflictMode} selection`;
      }
    }
    
    // Build rerun conflict resolution result
    result.rerunAllDnaResults = {
      dnaResults: rerunResults,
      conflictResolution: {
        metric: conflictMode,
        winnerIndex: rerunWinnerIndex,
        reason: rerunWinnerReason,
        hadConflict: buySignals.length > 1,
      },
    };
    
    // Set winner details
    if (rerunWinnerIndex >= 0) {
      const winner = rerunResults[rerunWinnerIndex];
      result.rerunWinningIndicator = winner.indicatorName;
      result.rerunWinningSignal = winner.signal;
      result.rerunWinningIndicatorValue = winner.indicatorValue;
    }
    
    // 7. Compare results
    const originalWinnerIndex = storedDnaResults.conflictResolution?.winnerIndex ?? -1;
    const originalWinner = storedDnaResults.dnaResults?.[originalWinnerIndex];
    
    // Winner match: same DNA won
    result.winnerMatch = rerunWinnerIndex === originalWinnerIndex;
    
    // Signal match: winning DNA produced same signal
    if (originalWinner && rerunWinnerIndex >= 0) {
      const rerunWinner = rerunResults[rerunWinnerIndex];
      result.signalMatch = rerunWinner.signal === originalWinner.signal;
    }
    
    // All signals match: every DNA produced the same signal
    if (storedDnaResults.dnaResults && storedDnaResults.dnaResults.length === rerunResults.length) {
      result.allSignalsMatch = storedDnaResults.dnaResults.every((orig: any, i: number) => 
        orig.signal === rerunResults[i].signal
      );
    }
    
    // 8. Determine validation status
    if (result.winnerMatch && result.signalMatch) {
      result.status = 'validated';
      result.notes.push('✓ Same DNA won with same signal');
      if (result.allSignalsMatch) {
        result.notes.push('✓ All DNA signals match exactly');
      } else {
        result.notes.push('⚠️ Some DNA signals differ (but winner is correct)');
      }
    } else if (!result.winnerMatch) {
      result.status = 'winner_mismatch';
      result.notes.push(`✗ Different DNA won: Original=${originalWinner?.indicatorName || 'N/A'} vs Rerun=${result.rerunWinningIndicator || 'N/A'}`);
    } else {
      result.status = 'signal_mismatch';
      result.notes.push(`✗ Signal mismatch: Original=${originalWinner?.signal || 'N/A'} vs Rerun=${result.rerunWinningSignal || 'N/A'}`);
    }
    
    console.log(`[Validation] Result: ${result.status}`);
    console.log(`[Validation]   Winner Match: ${result.winnerMatch}`);
    console.log(`[Validation]   Signal Match: ${result.signalMatch}`);
    console.log(`[Validation]   All Signals Match: ${result.allSignalsMatch}`);
    
    // 9. Store validation result
    await storeValidationResult(actualTradeId, actualTrade, result);
    
    return result;
    
  } catch (error: any) {
    result.error = error.message;
    console.error(`[Validation] Error validating trade ${actualTradeId}:`, error);
    return result;
  }
}

/**
 * Legacy validation for trades without allDnaResults
 * Only validates the winning indicator
 */
async function validateTradeLegacy(
  actualTradeId: number,
  actualTrade: any,
  result: ValidationResult
): Promise<ValidationResult> {
  // Calculate start_date as 60 days before the trade date
  const tradeDate = new Date(actualTrade.createdAt);
  const tradeStartDate = new Date(tradeDate);
  tradeStartDate.setDate(tradeStartDate.getDate() - 60);
  const startDateStr = tradeStartDate.toISOString().split('T')[0];
  const endDateStr = tradeDate.toISOString().split('T')[0];
  
  console.log(`[Validation] Legacy validation with dates: ${startDateStr} to ${endDateStr}`);
  
  // FIX: Validate that we have a valid indicator name
  if (!actualTrade.winningIndicatorName || actualTrade.winningIndicatorName === 'unknown') {
    result.status = 'data_not_ready';
    result.error = 'Trade missing winningIndicatorName - cannot perform legacy validation';
    result.notes.push('⚠️ Cannot validate: winningIndicatorName is missing or "unknown"');
    console.log(`[Validation] ERROR: ${result.error}`);
    return result;
  }
  
  const signalConfig: SignalConfig = {
    epic: actualTrade.epic,
    indicator_name: actualTrade.winningIndicatorName,
    indicator_params: (actualTrade.indicatorParams as Record<string, any>) || {},
    timeframe: '5m',
    leverage: actualTrade.leverage,
    stop_loss: actualTrade.stopLossPrice || 2,
    data_source: 'capital',
    start_date: startDateStr,
    end_date: endDateStr,
  };
  
  try {
    const signalResult = await getCurrentSignal(signalConfig);
    
    result.rerunWinningIndicator = actualTrade.winningIndicatorName;
    result.rerunWinningSignal = signalResult.signal === 1 ? 'BUY' : 'HOLD';
    result.rerunWinningIndicatorValue = signalResult.indicator_value;
    result.rerunCandleCount = signalResult.candle_count || signalResult.candles_loaded || 0;
    
    // The actual trade was a BUY
    result.signalMatch = result.rerunWinningSignal === 'BUY';
    result.winnerMatch = true; // Can't validate winner without all DNA results
    
    if (result.signalMatch) {
      result.status = 'validated';
      result.notes.push('✓ Signal match (legacy validation - winner not verified)');
    } else {
      result.status = 'signal_mismatch';
      result.notes.push(`✗ Signal mismatch: Actual=BUY, Rerun=${result.rerunWinningSignal}`);
    }
    
    await storeValidationResult(actualTradeId, actualTrade, result);
    return result;
    
  } catch (error: any) {
    result.error = error.message;
    result.notes.push(`Signal calculation error: ${error.message}`);
    return result;
  }
}

/**
 * Resolve conflict between multiple BUY signals
 */
function resolveConflict(buySignals: RerunDnaResult[], conflictMode: string): number {
  if (buySignals.length === 0) return -1;
  if (buySignals.length === 1) return buySignals[0].testIndex;
  
  let winner = buySignals[0];
  
  for (const signal of buySignals) {
    if (conflictMode.toLowerCase().includes('sharpe')) {
      if ((signal.sharpeRatio || 0) > (winner.sharpeRatio || 0)) {
        winner = signal;
      }
    } else if (conflictMode.toLowerCase().includes('return')) {
      if ((signal.totalReturn || 0) > (winner.totalReturn || 0)) {
        winner = signal;
      }
    } else if (conflictMode.toLowerCase().includes('win')) {
      if ((signal.winRate || 0) > (winner.winRate || 0)) {
        winner = signal;
      }
    } else if (conflictMode.toLowerCase().includes('drawdown')) {
      // Lower drawdown is better
      if ((signal.maxDrawdown || 100) < (winner.maxDrawdown || 100)) {
        winner = signal;
      }
    }
  }
  
  return winner.testIndex;
}

/**
 * Get historical performance for a DNA from backtest results
 */
async function getHistoricalPerformance(
  epic: string,
  indicatorName: string,
  indicatorParams: Record<string, any>
): Promise<{ sharpeRatio: number; totalReturn: number; winRate: number; maxDrawdown: number } | null> {
  try {
    const db = await getDb();
    if (!db) return null;
    
    const results = await db
      .select()
      .from(backtestResults)
      .where(and(
        eq(backtestResults.epic, epic),
        eq(backtestResults.indicatorName, indicatorName)
      ))
      .orderBy(desc(backtestResults.sharpeRatio))
      .limit(10);
    
    // Find best match by params
    const paramStr = JSON.stringify(indicatorParams);
    const match = results.find(r => JSON.stringify(r.indicatorParams) === paramStr);
    
    if (match) {
      return {
        sharpeRatio: match.sharpeRatio || 0,
        totalReturn: match.totalReturn || 0,
        winRate: match.winRate || 0,
        maxDrawdown: match.maxDrawdown || 0,
      };
    }
    
    // Return first result if no exact match
    if (results.length > 0) {
      return {
        sharpeRatio: results[0].sharpeRatio || 0,
        totalReturn: results[0].totalReturn || 0,
        winRate: results[0].winRate || 0,
        maxDrawdown: results[0].maxDrawdown || 0,
      };
    }
    
    return null;
  } catch (error) {
    return null;
  }
}

/**
 * Get candle count for a date range
 */
async function getCandleCount(epic: string, startDate: string, endDate: string): Promise<number> {
  try {
    const db = await getDb();
    if (!db) return 0;
    
    const result = await db
      .select()
      .from(candles)
      .where(and(
        eq(candles.epic, epic),
        eq(candles.source, 'capital')
      ));
    
    // Filter by date range
    const start = new Date(startDate);
    const end = new Date(endDate);
    const filtered = result.filter(c => {
      const ts = new Date(c.timestamp);
      return ts >= start && ts <= end;
    });
    
    return filtered.length;
  } catch (error) {
    return 0;
  }
}

/**
 * Store validation result in validated_trades table
 */
async function storeValidationResult(
  actualTradeId: number,
  actualTrade: any,
  result: ValidationResult
): Promise<number | null> {
  try {
    const db = await getDb();
    if (!db) return null;
    
    // Check if validation already exists
    const existing = await db
      .select()
      .from(validatedTrades)
      .where(eq(validatedTrades.actualTradeId, actualTradeId))
      .limit(1);
    
    const validationData = {
      actualTradeId,
      // Original trade details
      originalEpic: result.originalEpic,
      originalIndicatorName: result.originalIndicatorName,
      originalIndicatorParams: result.originalIndicatorParams,
      originalLeverage: result.originalLeverage,
      originalStopLoss: result.originalStopLoss,
      originalEntryPrice: result.originalEntryPrice,
      originalTradeDate: result.originalTradeDate,
      // Re-run results
      rerunSignal: result.rerunWinningSignal,
      rerunEntryPrice: null, // Not applicable for comprehensive validation
      rerunIndicatorValue: result.rerunWinningIndicatorValue,
      rerunWouldHaveTraded: result.rerunWinningSignal === 'BUY',
      // Comparison
      signalMatch: result.signalMatch,
      priceMatch: false, // Not applicable
      priceDifferencePercent: null,
      // Data info
      dataSource: 'capital',
      candleCountUsed: result.rerunCandleCount,
      lastCandleTimestamp: null,
      // Status
      validationStatus: result.status as any,
      validationNotes: result.notes.join('\n'),
      validatedAt: ['validated', 'signal_mismatch', 'winner_mismatch'].includes(result.status) ? new Date() : null,
    };
    
    if (existing.length > 0) {
      await db
        .update(validatedTrades)
        .set(validationData)
        .where(eq(validatedTrades.id, existing[0].id));
      
      console.log(`[Validation] Updated validation record ${existing[0].id}`);
      return existing[0].id;
    } else {
      const insertResult = await db.insert(validatedTrades).values(validationData);
      console.log(`[Validation] Created validation record ${insertResult[0].insertId}`);
      return Number(insertResult[0].insertId);
    }
    
  } catch (error: any) {
    console.error(`[Validation] Error storing validation result:`, error);
    return null;
  }
}

/**
 * Get validation summary for an account
 */
export async function getValidationSummary(accountId: number): Promise<{
  total: number;
  validated: number;
  signalMismatches: number;
  winnerMismatches: number;
  pending: number;
  dataNotReady: number;
  errors: number;
}> {
  try {
    const db = await getDb();
    if (!db) {
      return { total: 0, validated: 0, signalMismatches: 0, winnerMismatches: 0, pending: 0, dataNotReady: 0, errors: 0 };
    }
    
    // Get all actual trades for this account
    const tradesForAccount = await db
      .select({ id: actualTrades.id })
      .from(actualTrades)
      .where(eq(actualTrades.accountId, accountId));
    
    if (tradesForAccount.length === 0) {
      return { total: 0, validated: 0, signalMismatches: 0, winnerMismatches: 0, pending: 0, dataNotReady: 0, errors: 0 };
    }
    
    // Get all validations
    const validations = await db.select().from(validatedTrades);
    
    const tradeIds = new Set(tradesForAccount.map(t => t.id));
    const accountValidations = validations.filter(v => tradeIds.has(v.actualTradeId));
    
    return {
      total: tradesForAccount.length,
      validated: accountValidations.filter(v => v.validationStatus === 'validated').length,
      signalMismatches: accountValidations.filter(v => v.validationStatus === 'signal_mismatch').length,
      winnerMismatches: accountValidations.filter(v => v.validationStatus === 'winner_mismatch').length,
      pending: tradesForAccount.length - accountValidations.length,
      dataNotReady: accountValidations.filter(v => v.validationStatus === 'data_not_ready').length,
      errors: accountValidations.filter(v => v.validationStatus === 'error').length,
    };
    
  } catch (error: any) {
    console.error(`[Validation] Error getting validation summary:`, error);
    return { total: 0, validated: 0, signalMismatches: 0, winnerMismatches: 0, pending: 0, dataNotReady: 0, errors: 0 };
  }
}

/**
 * Validate all pending trades for an account
 * OPTIMIZED: Validates trades in parallel batches for faster processing
 */
export async function validatePendingTrades(accountId: number): Promise<ValidationResult[]> {
  const results: ValidationResult[] = [];
  
  try {
    const db = await getDb();
    if (!db) return results;
    
    // Get all actual trades that need validation
    const pendingTrades = await db
      .select()
      .from(actualTrades)
      .where(eq(actualTrades.accountId, accountId));
    
    console.log(`[Validation] Found ${pendingTrades.length} trades to validate for account ${accountId}`);
    
    // Filter out already validated trades
    const tradesToValidate: typeof pendingTrades = [];
    
    for (const trade of pendingTrades) {
      const existingValidation = await db
        .select()
        .from(validatedTrades)
        .where(eq(validatedTrades.actualTradeId, trade.id))
        .limit(1);
      
      if (existingValidation.length > 0 && 
          !['pending', 'data_not_ready', 'error'].includes(existingValidation[0].validationStatus)) {
        console.log(`[Validation] Skipping trade ${trade.id} - already validated: ${existingValidation[0].validationStatus}`);
        continue;
      }
      
      tradesToValidate.push(trade);
    }
    
    console.log(`[Validation] ${tradesToValidate.length} trades need validation`);
    
    // Validate in parallel batches (4 at a time to avoid overwhelming the system)
    const PARALLEL_BATCH_SIZE = 4;
    
    for (let i = 0; i < tradesToValidate.length; i += PARALLEL_BATCH_SIZE) {
      const batch = tradesToValidate.slice(i, i + PARALLEL_BATCH_SIZE);
      
      console.log(`[Validation] Processing batch ${Math.floor(i / PARALLEL_BATCH_SIZE) + 1}/${Math.ceil(tradesToValidate.length / PARALLEL_BATCH_SIZE)} (${batch.length} trades)`);
      
      // Run batch in parallel
      const batchResults = await Promise.all(
        batch.map(trade => validateTrade(trade.id))
      );
      
      results.push(...batchResults);
      
      // Log progress
      const validated = results.filter(r => r.status === 'validated').length;
      const failed = results.filter(r => r.status !== 'validated').length;
      console.log(`[Validation] Progress: ${results.length}/${tradesToValidate.length} (${validated} validated, ${failed} issues)`);
    }
    
    return results;
    
  } catch (error: any) {
    console.error(`[Validation] Error validating pending trades:`, error);
    return results;
  }
}

/**
 * Validate ALL trades across all accounts
 * OPTIMIZED: Uses batch DNA evaluation + parallel trade validation
 */
export async function validateAllTrades(): Promise<ValidationResult[]> {
  const results: ValidationResult[] = [];
  
  try {
    const db = await getDb();
    if (!db) return results;
    
    // Get all actual trades that need validation
    const allTrades = await db
      .select()
      .from(actualTrades)
      .orderBy(desc(actualTrades.id)); // Newest first
    
    console.log(`[Validation] Found ${allTrades.length} total trades to validate`);
    
    // Filter out already validated trades
    const tradesToValidate: typeof allTrades = [];
    
    for (const trade of allTrades) {
      const existingValidation = await db
        .select()
        .from(validatedTrades)
        .where(eq(validatedTrades.actualTradeId, trade.id))
        .limit(1);
      
      if (existingValidation.length > 0 && 
          !['pending', 'data_not_ready', 'error'].includes(existingValidation[0].validationStatus)) {
        // Already validated successfully
        continue;
      }
      
      tradesToValidate.push(trade);
    }
    
    console.log(`[Validation] ${tradesToValidate.length} trades need validation`);
    
    if (tradesToValidate.length === 0) {
      console.log(`[Validation] All trades already validated!`);
      return results;
    }
    
    // Validate in parallel batches
    const PARALLEL_BATCH_SIZE = 4;
    const startTime = Date.now();
    
    for (let i = 0; i < tradesToValidate.length; i += PARALLEL_BATCH_SIZE) {
      const batch = tradesToValidate.slice(i, i + PARALLEL_BATCH_SIZE);
      
      console.log(`[Validation] Batch ${Math.floor(i / PARALLEL_BATCH_SIZE) + 1}/${Math.ceil(tradesToValidate.length / PARALLEL_BATCH_SIZE)} (trades: ${batch.map(t => t.id).join(', ')})`);
      
      // Run batch in parallel
      const batchResults = await Promise.all(
        batch.map(trade => validateTrade(trade.id))
      );
      
      results.push(...batchResults);
      
      // Log progress with timing estimate
      const elapsed = (Date.now() - startTime) / 1000;
      const avgPerTrade = elapsed / results.length;
      const remaining = (tradesToValidate.length - results.length) * avgPerTrade;
      
      const validated = results.filter(r => r.status === 'validated').length;
      console.log(`[Validation] Progress: ${results.length}/${tradesToValidate.length} | ${validated} validated | ETA: ${Math.round(remaining)}s`);
    }
    
    // Final summary
    const totalTime = (Date.now() - startTime) / 1000;
    const validated = results.filter(r => r.status === 'validated').length;
    const signalMismatch = results.filter(r => r.status === 'signal_mismatch').length;
    const winnerMismatch = results.filter(r => r.status === 'winner_mismatch').length;
    const errors = results.filter(r => r.status === 'error').length;
    
    console.log(`[Validation] ========== COMPLETE ==========`);
    console.log(`[Validation] Total: ${results.length} trades in ${totalTime.toFixed(1)}s`);
    console.log(`[Validation] ✓ Validated: ${validated}`);
    console.log(`[Validation] ✗ Signal mismatch: ${signalMismatch}`);
    console.log(`[Validation] ✗ Winner mismatch: ${winnerMismatch}`);
    console.log(`[Validation] ✗ Errors: ${errors}`);
    console.log(`[Validation] Avg time per trade: ${(totalTime / results.length).toFixed(2)}s`);
    
    return results;
    
  } catch (error: any) {
    console.error(`[Validation] Error validating all trades:`, error);
    return results;
  }
}
