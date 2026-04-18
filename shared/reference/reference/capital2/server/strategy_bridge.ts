import { spawnPython } from './python_spawn';
import path from 'path';

const PYTHON_ENGINE_DIR = path.join(process.cwd(), 'python_engine');
const STRATEGY_EXECUTOR = path.join(PYTHON_ENGINE_DIR, 'strategy_executor.py');

export interface StrategyConfig {
  epic: string;
  start_date: string;
  end_date: string;
  initial_balance: number;
  monthly_topup: number;
  tests: Array<{
    indicatorName: string;
    indicatorParams?: Record<string, any>;
    leverage: number;
    stopLoss?: number;
    allocationPercent: number;
    timingConfig?: any;
    dataSource?: 'av' | 'capital';
  }>;
  conflict_resolution: {
    mode: string;
  };
  calculation_mode?: string;
  dataSource?: 'av' | 'capital';  // Data source for all tests (default: capital)
}

export interface StrategyResult {
  initial_balance: number;
  final_balance: number;
  total_contributions: number;
  total_return: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  max_drawdown: number;
  sharpe_ratio: number;
  trades: any[];
  daily_balances?: any[];
  conflict_mode: string;
  test_count: number;
}

export async function executeStrategy(config: StrategyConfig): Promise<StrategyResult> {
  return new Promise((resolve, reject) => {
    const configJson = JSON.stringify(config);
    
    const pythonProcess = spawnPython(STRATEGY_EXECUTOR, {
      args: [configJson],
      cwd: PYTHON_ENGINE_DIR,
      env: {
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
      console.error('[Strategy Executor Error]', data.toString());
    });

    pythonProcess.on('close', (code) => {
      console.log('[Strategy Executor] Process closed with code:', code);
      console.log('[Strategy Executor] stdout length:', stdout.length);
      console.log('[Strategy Executor] stderr length:', stderr.length);
      
      if (code !== 0) {
        console.error('[Strategy Executor] Non-zero exit code');
        console.error('[Strategy Executor] stderr:', stderr);
        reject(new Error(`Strategy execution failed with code ${code}: ${stderr || 'No error message'}`));
        return;
      }

      try {
        // Parse RESULT:{json} format
        const resultMatch = stdout.match(/RESULT:({[\s\S]*})/); // Match multiline JSON
        if (!resultMatch) {
          console.error('[Strategy Executor] No RESULT: found in stdout');
          console.error('[Strategy Executor] Full stdout:', stdout);
          reject(new Error(`No result found in output. stdout length: ${stdout.length}, stderr: ${stderr}`));
          return;
        }

        console.log('[Strategy Executor] Parsing result JSON, length:', resultMatch[1].length);
        const result = JSON.parse(resultMatch[1]);
        console.log('[Strategy Executor] Successfully parsed result');
        resolve(result);
      } catch (error) {
        console.error('[Strategy Executor] JSON parse error:', error);
        console.error('[Strategy Executor] Attempted to parse:', stdout.substring(0, 500));
        reject(new Error(`Failed to parse strategy result: ${error instanceof Error ? error.message : String(error)}`));
      }
    });;

    pythonProcess.on('error', (error) => {
      reject(new Error(`Failed to start strategy executor: ${error.message}`));
    });
  });
}

