/**
 * Conflict Resolution Bridge
 * 
 * Calls Python conflict_resolver.py to resolve conflicts between BUY signals.
 * 
 * This is the SINGLE implementation of conflict resolution, used by:
 * - brain.ts (for brain preview and live brain calculations)
 * - dna_brain_accumulator.ts (for cross-timing DNA conflicts)
 * 
 * By centralizing conflict resolution in Python:
 * 1. Identical logic everywhere (no TS/Python drift)
 * 2. Single place to update resolution algorithms
 * 3. Consistent behavior across backtest and live trading
 */

import { spawnPython } from './python_spawn';
import path from 'path';

const PYTHON_ENGINE_DIR = path.join(process.cwd(), 'python_engine');
const CONFLICT_RESOLVER = path.join(PYTHON_ENGINE_DIR, 'conflict_resolver.py');

/**
 * Signal to resolve - represents a BUY signal from an indicator/DNA strand
 */
export interface ConflictSignal {
  index: number;           // Index in original test/DNA list
  indicatorName: string;   // Name of indicator (e.g., 'rsi_oversold')
  sharpe?: number;         // Sharpe ratio from backtest
  totalReturn?: number;    // Total return % from backtest
  winRate?: number;        // Win rate % from backtest
  maxDrawdown?: number;    // Max drawdown % from backtest
  leverage?: number;       // Leverage setting
  stopLoss?: number;       // Stop loss setting
  epic?: string;           // Epic symbol
  timeframe?: string;      // Timeframe
  
  // Additional fields for DNA strands
  dnaStrandId?: string;
  timingMode?: string;
  dataSource?: string;
}

/**
 * Conflict resolution mode
 * 
 * Must match values in Python conflict_resolver.py
 */
export type ConflictMode = 
  | 'sharpeRatio' | 'sharpe' | 'highest_sharpe'
  | 'profitability' | 'return' | 'totalReturn'
  | 'winRate' | 'win_rate'
  | 'maxDrawdown' | 'drawdown' | 'min_drawdown'
  | 'first_signal';

/**
 * Result of conflict resolution
 */
export interface ConflictResolutionResult {
  winnerIndex: number;       // Index in input signals array
  originalIndex: number;     // Original index in test/DNA list
  winner: ConflictSignal | null;
  mode: string;
  hadConflict: boolean;
  reason: string;
  competingSignals?: Array<{
    index: number;
    indicator: string;
    sharpe?: number;
    totalReturn?: number;
    winRate?: number;
    maxDrawdown?: number;
  }>;
}

/**
 * Resolve conflicts between BUY signals
 * 
 * Calls Python conflict_resolver.py to pick the winner.
 * 
 * @param signals - Array of BUY signals to resolve
 * @param mode - Resolution mode (default: 'sharpeRatio')
 * @returns Resolution result with winner
 * 
 * @example
 * ```typescript
 * const buySignals = [
 *   { index: 0, indicatorName: 'rsi_oversold', sharpe: 1.5, totalReturn: 150 },
 *   { index: 2, indicatorName: 'macd_bullish', sharpe: 2.0, totalReturn: 200 },
 * ];
 * 
 * const result = await resolveConflict(buySignals, 'sharpeRatio');
 * console.log(`Winner: ${result.winner.indicatorName} (${result.reason})`);
 * ```
 */
export async function resolveConflict(
  signals: ConflictSignal[],
  mode: ConflictMode = 'sharpeRatio'
): Promise<ConflictResolutionResult> {
  // Handle empty or single signal cases locally (no need to call Python)
  if (signals.length === 0) {
    return {
      winnerIndex: -1,
      originalIndex: -1,
      winner: null,
      mode,
      hadConflict: false,
      reason: 'No BUY signals to resolve',
    };
  }
  
  if (signals.length === 1) {
    return {
      winnerIndex: 0,
      originalIndex: signals[0].index,
      winner: signals[0],
      mode,
      hadConflict: false,
      reason: `Single BUY signal: ${signals[0].indicatorName}`,
    };
  }
  
  // Multiple signals - call Python for conflict resolution
  return new Promise((resolve, reject) => {
    const config = {
      signals: signals.map(s => ({
        index: s.index,
        indicatorName: s.indicatorName,
        sharpe: s.sharpe,
        totalReturn: s.totalReturn,
        winRate: s.winRate,
        maxDrawdown: s.maxDrawdown,
        leverage: s.leverage,
        stopLoss: s.stopLoss,
        epic: s.epic,
        timeframe: s.timeframe,
        dnaStrandId: s.dnaStrandId,
        timingMode: s.timingMode,
        dataSource: s.dataSource,
      })),
      mode,
    };
    
    const configJson = JSON.stringify(config);
    
    const pythonProcess = spawnPython(CONFLICT_RESOLVER, {
      args: [configJson],
      cwd: PYTHON_ENGINE_DIR,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });

    let stdout = '';
    let stderr = '';

    pythonProcess.stdout?.on('data', (data) => {
      stdout += data.toString();
    });

    pythonProcess.stderr?.on('data', (data) => {
      stderr += data.toString();
      console.log('[ConflictResolver]', data.toString().trim());
    });

    pythonProcess.on('close', (code) => {
      if (code !== 0) {
        reject(new Error(`Conflict resolver failed: ${stderr}`));
        return;
      }

      try {
        // Parse RESULT:{json} format
        const resultMatch = stdout.match(/RESULT:({.*})/);
        if (!resultMatch) {
          reject(new Error(`No result found in output: ${stdout}`));
          return;
        }

        const result = JSON.parse(resultMatch[1]) as ConflictResolutionResult;
        resolve(result);
      } catch (error: any) {
        reject(new Error(`Failed to parse conflict result: ${error.message}`));
      }
    });
  });
}

/**
 * Synchronous conflict resolution (for simple cases)
 * 
 * Uses TypeScript-only logic for performance when Python call is unnecessary.
 * Falls back to Python for complex modes.
 * 
 * @param signals - Array of BUY signals
 * @param mode - Resolution mode
 * @returns Winner index (or -1 if no signals)
 */
export function resolveConflictSync(
  signals: ConflictSignal[],
  mode: ConflictMode = 'sharpeRatio'
): number {
  if (signals.length === 0) return -1;
  if (signals.length === 1) return 0;
  
  const modeLower = mode.toLowerCase().replace(/_/g, '');
  
  switch (modeLower) {
    case 'sharperatio':
    case 'sharpe':
    case 'highestsharpe':
      return signals.reduce((maxIdx, s, i) => 
        (s.sharpe || 0) > (signals[maxIdx].sharpe || 0) ? i : maxIdx, 0);
      
    case 'profitability':
    case 'return':
    case 'totalreturn':
      return signals.reduce((maxIdx, s, i) => 
        (s.totalReturn || 0) > (signals[maxIdx].totalReturn || 0) ? i : maxIdx, 0);
      
    case 'winrate':
      return signals.reduce((maxIdx, s, i) => 
        (s.winRate || 0) > (signals[maxIdx].winRate || 0) ? i : maxIdx, 0);
      
    case 'maxdrawdown':
    case 'drawdown':
    case 'mindrawdown':
      return signals.reduce((minIdx, s, i) => 
        Math.abs(s.maxDrawdown || 100) < Math.abs(signals[minIdx].maxDrawdown || 100) ? i : minIdx, 0);
      
    case 'firstsignal':
    case 'first':
      return signals.reduce((minIdx, s, i) => 
        s.index < signals[minIdx].index ? i : minIdx, 0);
      
    default:
      // Default to sharpe
      return signals.reduce((maxIdx, s, i) => 
        (s.sharpe || 0) > (signals[maxIdx].sharpe || 0) ? i : maxIdx, 0);
  }
}

