import { spawnPython } from './python_spawn';
import path from 'path';

const PYTHON_ENGINE_DIR = path.join(process.cwd(), 'python_engine');
const SIGNAL_SCRIPT = path.join(PYTHON_ENGINE_DIR, 'get_current_signal.py');
const BATCH_VALIDATE_SCRIPT = path.join(PYTHON_ENGINE_DIR, 'batch_validate_signals.py');

export interface SignalConfig {
  db_path?: string;  // Deprecated, kept for compatibility
  epic: string;
  start_date?: string;  // Optional: defaults to 60 days before end_date
  end_date?: string;    // Optional: defaults to today
  indicator_name: string;
  indicator_params: Record<string, any>;
  
  // Optional trade parameters (for validation)
  timeframe?: string;   // e.g., '5m' (default)
  leverage?: number;    // Trade leverage
  stop_loss?: number;   // Stop loss percentage
  
  // Optional: Real-time mode parameters
  fake_5min_close?: number;      // If provided, append fake candle with this close
  fake_5min_timestamp?: string;  // Timestamp for fake candle (ISO format)
  data_source?: 'capital'; // All data is from Capital.com (UTC)
  
  // Timing configuration (per-DNA, for consistency with backtest runner)
  timing_config?: {
    mode: string;  // e.g., 'Fake5min_3rdCandle_API', 'Fake5min_4thCandle', 'MarketClose'
    calc_offset_seconds?: number;
  };
  
  // Crash protection (MUST match original backtest for consistent signals)
  crash_protection_enabled?: boolean;
}

export interface SignalResult {
  signal: number; // 1 = BUY, 0 = HOLD
  indicator_value: number | null;
  timestamp: string;
  close_price: number;
  entry_price?: number;  // Entry price for validation (same as close_price)
  candles_loaded?: number;
  candle_count?: number;  // Alias for candles_loaded
  candles_needed?: number;
  used_fake_candle?: boolean;
  data_warning?: string;
  crash_blocked?: boolean;  // True if signal was blocked by crash protection
  crash_reason?: string;    // Reason for blocking
  error?: string;  // Error message if signal calculation failed
}

export async function getCurrentSignal(config: SignalConfig): Promise<SignalResult> {
  return new Promise((resolve, reject) => {
    const configJson = JSON.stringify(config);
    
    const pythonProcess = spawnPython(SIGNAL_SCRIPT, {
      args: [configJson],
      cwd: PYTHON_ENGINE_DIR,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
      },
    });

    let stdout = '';
    let stderr = '';

    pythonProcess.stdout?.on('data', (data) => {
      stdout += data.toString();
    });

    pythonProcess.stderr?.on('data', (data) => {
      stderr += data.toString();
      // Python uses stderr for logging - not actually errors
      console.log('[Signal Script]', data.toString().trim());
    });

    pythonProcess.on('close', (code) => {
      if (code !== 0) {
        reject(new Error(`Signal script failed: ${stderr}`));
        return;
      }

      try {
        // Parse RESULT:{json} format
        const resultMatch = stdout.match(/RESULT:({.*})/);
        if (!resultMatch) {
          reject(new Error(`No result found in output: ${stdout}`));
          return;
        }

        const result = JSON.parse(resultMatch[1]);
        resolve(result);
      } catch (error: any) {
        reject(new Error(`Failed to parse result: ${error.message}`));
      }
    });
  });
}

// ============================================================================
// BATCH VALIDATION - Evaluate ALL DNA strands in a single Python call
// ============================================================================

export interface BatchDnaConfig {
  indicatorName: string;
  indicatorParams: Record<string, any>;
  epic: string;
  timeframe?: string;
  dataSource?: string;
  crashProtectionEnabled?: boolean;
  // Performance metrics for conflict resolution
  sharpeRatio?: number;
  totalReturn?: number;
  winRate?: number;
  maxDrawdown?: number;
}

export interface BatchDnaResult {
  index: number;
  success: boolean;
  indicatorName: string;
  epic: string;
  timeframe?: string;
  signal: 'BUY' | 'HOLD';
  indicatorValue: number | null;
  timestamp?: string;
  closePrice?: number;
  crashBlocked?: boolean;
  crashReason?: string;
  candlesLoaded?: number;
  error?: string;
}

export interface BatchValidateConfig {
  dna_configs: BatchDnaConfig[];
  start_date: string;
  end_date: string;
}

export interface BatchValidateResult {
  success: boolean;
  results: BatchDnaResult[];
  candlesCached?: string[];
  totalDna: number;
  dnaWithBuy: number;
  error?: string;
}

/**
 * Evaluate ALL DNA strands in a SINGLE Python call.
 * This is 3-10x faster than calling getCurrentSignal() N times because:
 * 1. Only one Python process spawn
 * 2. Candles loaded once per epic (cached)
 * 
 * Use this for trade validation where you need to re-run all DNA.
 */
export async function batchValidateSignals(config: BatchValidateConfig): Promise<BatchValidateResult> {
  return new Promise((resolve, reject) => {
    const configJson = JSON.stringify(config);
    
    const pythonProcess = spawnPython(BATCH_VALIDATE_SCRIPT, {
      args: [configJson],
      cwd: PYTHON_ENGINE_DIR,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
      },
    });

    let stdout = '';
    let stderr = '';

    pythonProcess.stdout?.on('data', (data) => {
      stdout += data.toString();
    });

    pythonProcess.stderr?.on('data', (data) => {
      stderr += data.toString();
      // Python uses stderr for logging
      console.log('[BatchValidate]', data.toString().trim());
    });

    pythonProcess.on('close', (code) => {
      try {
        // Parse RESULT:{json} format
        const resultMatch = stdout.match(/RESULT:({.*})/);
        if (!resultMatch) {
          reject(new Error(`No result found in batch validate output: ${stdout.slice(0, 500)}`));
          return;
        }

        const result = JSON.parse(resultMatch[1]);
        
        // Even if Python exited non-zero, we might have a valid error result
        if (!result.success && result.error) {
          console.error(`[BatchValidate] Error: ${result.error}`);
        }
        
        resolve(result);
      } catch (error: any) {
        reject(new Error(`Failed to parse batch result: ${error.message}`));
      }
    });

    pythonProcess.on('error', (error) => {
      reject(new Error(`Failed to spawn batch validate: ${error.message}`));
    });
  });
}

