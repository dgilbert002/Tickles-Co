import { ChildProcess } from 'child_process';
import { spawnPython } from './python_spawn';
import * as path from 'path';
import crypto from 'crypto';
import { fileURLToPath } from 'url';
import { dirname } from 'path';
import * as db from './db';
import BacktestLogger from './logger';
import { resourceManager } from './resource_manager';
import { DB_PATH } from './config';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Track running backtests for stop functionality
export const runningBacktests = new Map<number, {
  processes: ChildProcess[];
  shouldStop: boolean;
}>();

interface BacktestConfig {
  runId: number;
  epic: string;
  startDate: string;
  endDate: string;
  initialBalance: number;
  monthlyTopup: number;
  investmentPct: number;
  indicators: string[];
  numSamples: number;
  direction: 'long' | 'short' | 'both';
  batchMode?: boolean;
  optimizationStrategy?: string;
  calculationMode?: 'standard' | 'numba';
  stopConditions?: {
    maxDrawdown: number;
    minWinRate: number;
    minSharpe: number;
    minProfitability?: number;
  };
  timingConfig?: {
    mode: 
      // Signal-based trading
      | 'SignalBased'
      // Auto-detect market close from data (recommended)
      | 'MarketClose' | 'T5BeforeClose' | 'T60FakeCandle'
      // Fixed time modes
      | 'USMarketClose' | 'ExtendedHoursClose'
      // Fake candle modes (T-60 brain calc)
      | 'Fake5min_3rdCandle_API' | 'Fake5min_4thCandle'
      // Random modes
      | 'random_morning' | 'random_afternoon'
      // Custom
      | 'Custom'
      // Legacy modes
      | 'EpicClosingTimeBrainCalc' | 'EpicClosingTime' | 'USMarketClosingTime' | 'ManusTime' | 'OriginalBotTime' | 'Random' | 'SpecificHour'
      // Second last candle
      | 'SecondLastCandle';
    market_close?: string;
    calc_offset_seconds?: number;
    close_offset_seconds?: number;
    open_offset_seconds?: number;
    entry_range_start?: string;
    entry_range_end?: string;
    exit_range_start?: string;
    exit_range_end?: string;
    entry_time_specific?: string;
    exit_time_specific?: string;
  };
  timeframeConfig?: {
    mode: 'default' | 'random' | 'multiple';
    selectedTimeframes: string[];
  };
  signalBasedConfig?: {
    enabled: boolean;
    closeOnNextSignal: boolean;
    maxHoldPeriod: 'same_day' | 'next_day' | 'custom';
    customHoldDays?: number;
  };
  // VERSION 3: Enhanced signal-based config (multi-indicator, trust matrix)
  enhancedSignalConfig?: {
    enabled: boolean;
    entryIndicators: string[];      // List of bullish indicators for entry
    exitIndicators: string[];       // List of bearish indicators for exit
    entryParams: Record<string, Record<string, any>>;  // Params per entry indicator
    exitParams: Record<string, Record<string, any>>;   // Params per exit indicator
    allowShort: boolean;            // Can short if epic supports it
    reverseOnSignal: boolean;       // true = reverse direction, false = close and wait
    stopLossMode: 'none' | 'fixed' | 'auto';  // auto = leverage-safe calculation
    stopLossPct: number;            // Fixed stop loss % (if mode='fixed')
    positionSizePct: number;        // % of available balance per trade
    minBalanceThreshold: number;    // Early termination if balance falls below
    useTrustMatrix: boolean;        // Use learned indicator pair relationships
    defaultLeverage: number;        // Default leverage if no trust data
  };
  crashProtectionMode?: 'without' | 'with' | 'both';
  // HMH (Hold Means Hold) - trail stop loss on HOLD instead of closing
  hmhConfig?: {
    enabled: boolean;
    offsets: number[];  // e.g., [0, -1, -2] for original, -1%, -2%
  };
  freeMemory?: boolean;
  testOrder?: 'sequential' | 'random';
  parallelCores?: number;
  rerunDuplicates?: boolean;
  dataSource?: 'capital';  // All data is from Capital.com (UTC)
}

interface BacktestResult {
  indicatorName: string;
  indicatorParams: Record<string, any>;
  leverage: number;
  stopLoss: number;
  timeframe?: string;
  initialBalance: number;
  finalBalance: number;
  totalContributions: number;
  totalReturn: number;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  maxDrawdown: number;
  sharpeRatio: number;
  trades?: any[];
  dailyBalances?: any[];
  // VERSION 3: Additional fields for signal-based backtests
  backtestType?: 'time_based' | 'signal_based';
  crashProtectionEnabled?: boolean;
  totalFees?: number;
  totalSpreadCosts?: number;
  totalOvernightCosts?: number;
  minMarginLevel?: number | null;
  liquidationCount?: number;
  marginLiquidatedTrades?: number;
  totalLiquidationLoss?: number;
  marginCloseoutLevel?: number | null;
  trustMatrix?: Record<string, any>;  // Trust matrix data for signal-based
  status?: 'success' | 'failed';      // Early termination status
  failReason?: string;                // Reason for early termination
}

export async function runBacktest(config: BacktestConfig): Promise<void> {
  const logger = new BacktestLogger(config.runId);
  logger.info(`Starting backtest run`, {
    mode: config.batchMode ? 'Batch' : 'Single',
    epic: config.epic,
    indicators: config.indicators,
    numSamples: config.numSamples,
    timeframeMode: config.timeframeConfig?.mode,
    selectedTimeframes: config.timeframeConfig?.selectedTimeframes,
  });
  
  // Fetch market close time based on timing mode
  let marketCloseTime = "auto"; // Default to auto-detect from data
  const mode = config.timingConfig?.mode;
  
  // Signal-based trading mode
  if (mode === 'SignalBased') {
    marketCloseTime = 'auto';  // Still need to detect close for max hold period
    logger.info(`Using Signal-Based trading mode with config:`, config.signalBasedConfig);
  }
  // Auto-detect modes - Python will detect close time from the candle data per day
  else if (mode === 'MarketClose' || mode === 'T5BeforeClose' || mode === 'T60FakeCandle') {
    marketCloseTime = 'auto';  // Signal Python to detect from data
    logger.info(`Using auto-detect market close from data with mode: ${mode}`);
  }
  // Fixed time modes
  else if (mode === 'USMarketClose' || mode === 'USMarketClosingTime') {
    marketCloseTime = '16:00:00';
    logger.info(`Using fixed US Market Close: ${marketCloseTime}`);
  } else if (mode === 'ExtendedHoursClose') {
    marketCloseTime = '20:00:00';
    logger.info(`Using fixed Extended Hours Close: ${marketCloseTime}`);
  }
  // Legacy modes - also use auto-detect
  else if (mode === 'EpicClosingTime' || mode === 'EpicClosingTimeBrainCalc') {
    marketCloseTime = 'auto';
    logger.info(`Using auto-detect market close for legacy mode: ${mode}`);
  }
  // Random modes - use auto-detect for exit time
  else if (mode === 'random_morning' || mode === 'random_afternoon' || mode === 'Random') {
    marketCloseTime = 'auto';
    logger.info(`Using auto-detect market close for random mode: ${mode}`);
  }
  // ManusTime, OriginalBotTime - use auto-detect
  else if (mode === 'ManusTime' || mode === 'OriginalBotTime') {
    marketCloseTime = 'auto';
    logger.info(`Using auto-detect market close for mode: ${mode}`);
  }
  // Custom mode - use auto-detect unless specific time provided
  else if (mode === 'Custom') {
    marketCloseTime = 'auto';
    logger.info(`Using auto-detect market close for Custom mode`);
  }
  // Default to auto-detect
  else {
    logger.info(`Unknown mode ${mode}, using auto-detect market close`);
  }
  
  // Add market_close to timing config for Python
  if (config.timingConfig) {
    (config.timingConfig as any).market_close = marketCloseTime;
  }
  
  // Allocate CPU cores for this backtest (with queuing support)
  const requestedCores = config.parallelCores || 1;
  logger.info(`Requesting ${requestedCores} cores...`);
  
  const allocatedCores = await resourceManager.allocateCores(config.runId, requestedCores);
  
  if (allocatedCores.length === 0) {
    logger.error(`Failed to allocate any cores after waiting (requested ${requestedCores})`);
    throw new Error(`No cores available: requested ${requestedCores}, got 0`);
  }
  
  if (allocatedCores.length < requestedCores) {
    logger.warn(`Partial allocation: requested ${requestedCores} cores, got ${allocatedCores.length}`);
  } else {
    logger.info(`Allocated ${allocatedCores.length} cores: ${allocatedCores.join(', ')}`);
  }
  
  // Initialize tracking
  runningBacktests.set(config.runId, {
    processes: [],
    shouldStop: false,
  });

  // Update status to running
  await db.updateBacktestRun(config.runId, {
    status: 'running',
    startedAt: new Date(),
    progress: 0,
  });

  try {
    if (config.batchMode && config.optimizationStrategy) {
      logger.info('Running batch optimization mode');
      await runBatchOptimization(config, logger, allocatedCores);
    } else {
      logger.info('Running standard backtest mode');
      await runStandardBacktest(config, logger);
    }
    logger.info('Backtest completed successfully');
  } catch (error) {
    logger.error('Backtest failed with error', error instanceof Error ? error : new Error(String(error)), {
      errorType: error instanceof Error ? error.constructor.name : typeof error,
      config: {
        epic: config.epic,
        indicators: config.indicators,
        numSamples: config.numSamples,
      },
    });
    
    await db.updateBacktestRun(config.runId, {
      status: 'failed',
      completedAt: new Date(),
    });
  } finally {
    // Release allocated cores
    const releasedCount = resourceManager.releaseRunCores(config.runId);
    logger.info(`Released ${releasedCount} cores`);
    
    // Cleanup
    runningBacktests.delete(config.runId);
    logger.info('Backtest cleanup completed');
  }
}

export function stopBacktest(runId: number): void {
  const running = runningBacktests.get(runId);
  if (running) {
    console.log(`[Backtest] Stopping run ${runId}`);
    running.shouldStop = true;

    // Kill all running processes - use taskkill on Windows to kill process trees
    for (const proc of running.processes) {
      if (proc && proc.pid) {
        const pid = proc.pid;
        console.log(`[Backtest] Killing process tree for PID ${pid}`);
        
        try {
          if (process.platform === 'win32') {
            // Windows: Use taskkill to kill entire process tree (including child workers)
            const { execSync } = require('child_process');
            try {
              execSync(`taskkill /T /F /PID ${pid}`, { stdio: 'ignore' });
              console.log(`[Backtest] Killed process tree for PID ${pid}`);
            } catch (e) {
              // Process may have already exited
              console.log(`[Backtest] Process ${pid} may have already exited`);
            }
          } else {
            // Unix: Kill process group
            try {
              process.kill(-pid, 'SIGTERM');
            } catch (e) {
              proc.kill('SIGTERM');
            }
          }
        } catch (error) {
          console.error(`[Backtest] Error killing process ${pid}:`, error);
          // Try regular kill as fallback
          try {
            proc.kill();
          } catch (e) {
            console.error(`[Backtest] Fallback kill also failed:`, e);
          }
        }
      }
    }

    // Release all cores allocated to this run
    const releasedCount = resourceManager.releaseRunCores(runId);
    console.log(`[Backtest] Released ${releasedCount} cores from run ${runId}`);

    // Update status
    db.updateBacktestRun(runId, {
      status: 'stopped',
      completedAt: new Date(),
    }).catch(console.error);
    
    // Remove from tracking map
    runningBacktests.delete(runId);
    console.log(`[Backtest] Run ${runId} fully stopped`);
  }
}

async function runBatchOptimization(config: BacktestConfig, logger: BacktestLogger, allocatedCores: number[]): Promise<void> {
  const pythonScript = path.join(__dirname, '../python_engine/batch_runner.py');
  const dbPath = DB_PATH;
  
  logger.info(`Running batch optimization`, { strategy: config.optimizationStrategy });
  
  // Send full timeframe config to Python for proper handling
  const timeframes = config.timeframeConfig?.selectedTimeframes || ['5m'];
  const timeframeMode = config.timeframeConfig?.mode || 'default';
  
  logger.info('Timeframe configuration', { mode: timeframeMode, timeframes });
  logger.info('Timing configuration (for fake candles)', { timingConfig: config.timingConfig });

  // Determine backtest type for appropriate timeout default
  const isIndicatorBased = config.enhancedSignalConfig?.enabled === true;
  const backtestTypeForTimeout = isIndicatorBased ? 'indicator' : 'timing';
  
  // Get configurable worker timeout (timing: 60s, indicator: 90s)
  const workerTimeoutSeconds = await db.getWorkerTimeoutSeconds(backtestTypeForTimeout);
  
  const batchConfig = JSON.stringify({
    db_path: dbPath,
    epic: config.epic,
    start_date: config.startDate,
    end_date: config.endDate,
    indicators: config.indicators,
    num_tests: config.numSamples,
    initial_balance: config.initialBalance,
    monthly_topup: config.monthlyTopup,
    investment_pct: config.investmentPct,
    direction: config.direction,
    optimization_strategy: config.optimizationStrategy || 'random',
    run_id: config.runId,  // For pause/resume
    timeframe_config: {
      mode: timeframeMode,
      timeframes: timeframes,
    },
    // Timing config for fake candle modes (Fake5min_4thCandle, Fake5min_3rdCandle_API, etc.)
    timing_config: config.timingConfig || { mode: 'MarketClose' },
    stop_conditions: config.stopConditions || {
      maxDrawdown: 100,
      minWinRate: 0,
      minSharpe: -999,
    },
    crash_protection_mode: config.crashProtectionMode || 'without',
    // HMH (Hold Means Hold) - trail stop loss on HOLD instead of closing
    hmh_config: config.hmhConfig || { enabled: false, offsets: [] },
    free_memory: config.freeMemory || false,
    test_order: config.testOrder || 'sequential',
    parallel_cores: allocatedCores.length,  // Use actual allocated cores, not requested
    rerun_duplicates: config.rerunDuplicates || false,
    data_source: config.dataSource || 'capital',  // All data is from Capital.com (UTC)
    // VERSION 3: Enhanced signal-based config (multi-indicator, trust matrix)
    enhanced_signal_config: config.enhancedSignalConfig || null,
    // Worker timeout in seconds (configurable via Settings)
    worker_timeout_seconds: workerTimeoutSeconds,
  });

  const python = spawnPython(pythonScript, { args: [batchConfig] });
  
  const running = runningBacktests.get(config.runId);
  if (running) {
    running.processes.push(python);
  }

  // NOTE: We don't accumulate stdout anymore - it caused "Invalid string length" crash
  // after ~6700 tests (string grew too large). All processing uses lineBuffer instead.
  let stderr = '';
  let resultsCount = 0;
  let duplicateCount = 0;
  let bestResult: BacktestResult | null = null;
  
  // Track base completed count for resume scenarios
  // When Python starts, it counts from 0. For resumed runs, we need to ADD Python's count
  // to the base (tests already completed before this Python process started)
  const initialRun = await db.getBacktestRun(config.runId);
  const resumeBaseCompleted = initialRun?.completedTests || 0;
  logger.info(`[Run ${config.runId}] Resume base: ${resumeBaseCompleted} tests already completed`);
  
  // Track PID to core ID mapping for worker monitoring
  // Use actual allocated core IDs instead of sequential counter
  const pidToCoreId = new Map<number, number>();
  let nextCoreIndex = 0;  // Index into allocatedCores array

  // Line buffer to handle multi-chunk stdout data
  let lineBuffer = '';
  
  // ============================================================================
  // BATCH RESULT BUFFER (Phase 1 optimization)
  // Buffer results and flush in batches for 10-50x faster DB writes
  // ============================================================================
  const RESULT_BATCH_SIZE = 10;
  const resultBuffer: db.InsertBacktestResult[] = [];
  
  // Flush buffered results to database
  async function flushResultBuffer(): Promise<void> {
    if (resultBuffer.length === 0) return;
    
    const toFlush = [...resultBuffer];
    resultBuffer.length = 0; // Clear buffer
    
    try {
      await db.createBacktestResultsBatch(toFlush);
      logger.info(`[Run ${config.runId}] Batch flushed ${toFlush.length} results to database`);
    } catch (error: any) {
      // On batch failure, fall back to individual inserts
      // This often happens due to duplicate hashes in the batch
      const isDuplicateError = error?.code === 'ER_DUP_ENTRY';
      if (!isDuplicateError) {
        // Log minimal error info to avoid dumping huge trade arrays
        logger.error(`[Run ${config.runId}] Batch insert failed (code: ${error?.code}): ${error?.message?.slice(0, 200) || 'Unknown error'}`);
      }
      // Silently fall back to individual inserts
      
      let inserted = 0;
      let skipped = 0;
      for (const result of toFlush) {
        try {
          await db.createBacktestResult(result);
          inserted++;
        } catch (e: any) {
          // Silently skip duplicates (already exists with same hash)
          if (e?.code === 'ER_DUP_ENTRY') {
            skipped++;
          } else {
            // Log minimal error info
            logger.error(`[Run ${config.runId}] Insert failed (code: ${e?.code}): ${e?.message?.slice(0, 200) || 'Unknown error'}`);
          }
        }
      }
      if (inserted > 0 || skipped > 0) {
        logger.info(`[Run ${config.runId}] Batch fallback: ${inserted} inserted, ${skipped} duplicates skipped`);
      }
    }
  }

  python.stdout.on('data', async (data) => {
    const output = data.toString();
    // NOTE: Removed "stdout += output" which caused memory crash after ~6700 tests
    
    // Add to line buffer and process complete lines
    lineBuffer += output;
    const lines = lineBuffer.split('\n');
    // Keep the last incomplete line in the buffer
    lineBuffer = lines.pop() || '';
    
    // Process each complete line
    for (const line of lines) {
      await processOutputLine(line);
    }
  });
  
  async function processOutputLine(output: string) {
    // Check for worker status updates
    const workerStartMatch = output.match(/WORKER_START:(\d+):(\d+):(.+)/);
    if (workerStartMatch) {
      const pid = parseInt(workerStartMatch[1]);
      const testNum = parseInt(workerStartMatch[2]);
      const indicator = workerStartMatch[3];
      
      // Assign core ID if this is a new PID
      // Cycle through allocated cores (round-robin)
      if (!pidToCoreId.has(pid)) {
        const coreId = allocatedCores[nextCoreIndex % allocatedCores.length];
        pidToCoreId.set(pid, coreId);
        nextCoreIndex++;
      }
      const coreId = pidToCoreId.get(pid)!;
      
      // Update Resource Manager
      resourceManager.updateCoreTest(coreId, testNum, indicator, pid);
      logger.info(`Worker ${pid} (core ${coreId}) started test #${testNum}: ${indicator}`);
    }
    
    const workerCompleteMatch = output.match(/WORKER_COMPLETE:(\d+):(\d+):(.+)/);
    if (workerCompleteMatch) {
      const pid = parseInt(workerCompleteMatch[1]);
      const coreId = pidToCoreId.get(pid);
      if (coreId !== undefined) {
        resourceManager.completeCore(coreId);
        logger.info(`Worker ${pid} (core ${coreId}) completed test`);
      }
    }
    
    const workerFailedMatch = output.match(/WORKER_FAILED:(\d+):(\d+):(.+?):(.+)/);
    if (workerFailedMatch) {
      const pid = parseInt(workerFailedMatch[1]);
      const errorMsg = workerFailedMatch[4];
      const coreId = pidToCoreId.get(pid);
      if (coreId !== undefined) {
        resourceManager.failCore(coreId, errorMsg);
        logger.error(`Worker ${pid} (core ${coreId}) failed: ${errorMsg}`);
      }
    }
    
    // Check for TOTAL_TESTS message (sent once at start with accurate count)
    const totalTestsMatch = output.match(/TOTAL_TESTS:(\d+)/);
    if (totalTestsMatch) {
      const accurateTotal = parseInt(totalTestsMatch[1]);
      
      await db.updateBacktestRun(config.runId, {
        totalTests: accurateTotal,
      });
      
      console.log(`[Backtest] 📊 Accurate total tests: ${accurateTotal}`);
    }
    
    // Check for progress updates
    const progressMatch = output.match(/PROGRESS:(\d+)\/(\d+)/);
    if (progressMatch) {
      const pythonCompleted = parseInt(progressMatch[1]);  // Python's count (starts from 0 each run)
      const pythonTotal = parseInt(progressMatch[2]);
      
      // For resumed runs: Python counts from 0, so we ADD to the base count
      // For fresh runs: base is 0, so this just equals Python's count
      const actualCompleted = resumeBaseCompleted + pythonCompleted;
      
      // Use the accurate total from TOTAL_TESTS if available, otherwise use Python's total
      const currentRun = await db.getBacktestRun(config.runId);
      const accurateTotal = currentRun?.totalTests || pythonTotal;
      const accurateProgress = Math.floor((actualCompleted / accurateTotal) * 100);
      
      await db.updateBacktestRun(config.runId, {
        progress: accurateProgress,
        completedTests: actualCompleted,
        // Don't overwrite totalTests here - it was set accurately by TOTAL_TESTS
      });
      
      console.log(`[Backtest] Progress: ${actualCompleted}/${accurateTotal} (${accurateProgress}%)`);
    }
    
    // Check for batch optimization complete message
    // This fires when Python finishes all available tests (some may have been skipped)
    const batchCompleteMatch = output.match(/Batch optimization complete! Total tests: (\d+)\/(\d+)/);
    if (batchCompleteMatch) {
      const pythonCompleted = parseInt(batchCompleteMatch[1]);
      const pythonTotal = parseInt(batchCompleteMatch[2]);
      
      // Python may have completed fewer tests than requested due to:
      // 1. Queue blocking (tests already claimed by previous runs)
      // 2. Hash deduplication (results already in DB)
      // Update the run to reflect actual completion
      const skipped = pythonTotal - pythonCompleted;
      
      // Calculate actual total completed (base from before resume + what Python completed this run)
      const actualCompleted = resumeBaseCompleted + pythonCompleted;
      
      // Mark as completing - the close handler will finalize
      logger.info(`[Run ${config.runId}] Batch optimization complete: ${pythonCompleted} ran this session (${resumeBaseCompleted} before), ${skipped} skipped of ${pythonTotal} total`);
      
      // Update progress to show we're done with available tests
      await db.updateBacktestRun(config.runId, {
        progress: 100,  // Mark as 100% since all available tests are done
        completedTests: actualCompleted,
      });
      
      console.log(`[Backtest] ✅ Batch complete: ${actualCompleted} total (${pythonCompleted} this session, ${resumeBaseCompleted} before) - ${skipped} skipped`);
    }
    
    // Check for process status updates (temporarily disabled)
    // const processStatusMatch = output.match(/PROCESS_STATUS:({.*})/);
    // if (processStatusMatch) {
    //   try {
    //     const processStatus = JSON.parse(processStatusMatch[1]);
    //     // Store process status in database
    //     await db.updateBacktestRun(config.runId, {
    //       processStatus: JSON.stringify(processStatus),
    //     });
    //   } catch (e) {
    //     console.error('[Backtest] Failed to parse process status:', e);
    //   }
    // }
    
    // Check for result lines
    const resultMatch = output.match(/RESULT:({.*})/);
    if (resultMatch) {
      logger.info(`[Run ${config.runId}] Received RESULT message`);
      try {
        const result = JSON.parse(resultMatch[1]);
        const testNum = result.testNum || '?';
        
        logger.info(`[Run ${config.runId}] Parsed result: Test #${testNum} - ${result.indicatorName} - Final: $${result.finalBalance}`);
        
        // Build hash input object for deduplication (includes ALL parameters)
        // Keys MUST be in alphabetical order for consistent hashing across Python/TypeScript
        const hashInputObj = {
          crashProtectionEnabled: result.crashProtectionEnabled || false,  // Include crash protection flag
          dataSource: config.dataSource || 'capital',  // All data from Capital.com (UTC)
          direction: config.direction,
          endDate: config.endDate,
          epic: config.epic,
          hmhEnabled: result.hmhEnabled || false,  // HMH: Hold Means Hold
          hmhStopLossOffset: result.hmhStopLossOffset ?? null,  // HMH SL offset (0, -1, -2)
          indicatorName: result.indicatorName,
          indicatorParams: result.indicatorParams,
          initialBalance: config.initialBalance,
          investmentPct: config.investmentPct,
          leverage: result.leverage,
          monthlyTopup: config.monthlyTopup,
          startDate: config.startDate,
          stopLoss: result.stopLoss,
          timeframe: result.timeframe,  // Include timeframe
          timingConfig: config.timingConfig,  // Include timing config
        };
        
        // Log ALL hash input values for debugging
        logger.info(`[Run ${config.runId}] === HASH INPUT DEBUG ===`);
        logger.info(`[Run ${config.runId}]   crashProtectionEnabled: ${hashInputObj.crashProtectionEnabled}`);
        logger.info(`[Run ${config.runId}]   dataSource: ${hashInputObj.dataSource}`);
        logger.info(`[Run ${config.runId}]   direction: ${hashInputObj.direction}`);
        logger.info(`[Run ${config.runId}]   endDate: ${hashInputObj.endDate}`);
        logger.info(`[Run ${config.runId}]   epic: ${hashInputObj.epic}`);
        logger.info(`[Run ${config.runId}]   hmhEnabled: ${hashInputObj.hmhEnabled}`);
        logger.info(`[Run ${config.runId}]   hmhStopLossOffset: ${hashInputObj.hmhStopLossOffset}`);
        logger.info(`[Run ${config.runId}]   indicatorName: ${hashInputObj.indicatorName}`);
        logger.info(`[Run ${config.runId}]   indicatorParams: ${JSON.stringify(hashInputObj.indicatorParams)}`);
        logger.info(`[Run ${config.runId}]   initialBalance: ${hashInputObj.initialBalance}`);
        logger.info(`[Run ${config.runId}]   investmentPct: ${hashInputObj.investmentPct}`);
        logger.info(`[Run ${config.runId}]   leverage: ${hashInputObj.leverage}`);
        logger.info(`[Run ${config.runId}]   monthlyTopup: ${hashInputObj.monthlyTopup}`);
        logger.info(`[Run ${config.runId}]   startDate: ${hashInputObj.startDate}`);
        logger.info(`[Run ${config.runId}]   stopLoss: ${hashInputObj.stopLoss}`);
        logger.info(`[Run ${config.runId}]   timeframe: ${hashInputObj.timeframe}`);
        logger.info(`[Run ${config.runId}]   timingConfig: ${JSON.stringify(hashInputObj.timingConfig)}`);
        logger.info(`[Run ${config.runId}]   finalBalance (NOT in hash): $${result.finalBalance}`);
        
        // Create a deterministic string by sorting keys at all levels
        // Using a custom replacer that handles nested objects
        const sortObjectKeys = (obj: any): any => {
          if (obj === null || typeof obj !== 'object') return obj;
          if (Array.isArray(obj)) return obj.map(sortObjectKeys);
          return Object.keys(obj).sort().reduce((sorted: any, key) => {
            sorted[key] = sortObjectKeys(obj[key]);
            return sorted;
          }, {});
        };
        
        const sortedObj = sortObjectKeys(hashInputObj);
        const hashInput = JSON.stringify(sortedObj);
        const paramHash = crypto.createHash('sha256').update(hashInput).digest('hex');
        
        // Debug: log the actual string being hashed
        logger.info(`[Run ${config.runId}]   hashInput string: ${hashInput}`);
        
        logger.info(`[Run ${config.runId}]   HASH: ${paramHash}`);
        logger.info(`[Run ${config.runId}] === END HASH INPUT ===`);
        
        // =====================================================================
        // GLOBAL DEDUPLICATION: Check testedHashes table (not backtestResults)
        // This prevents re-running ANY test that has EVER been run before
        // =====================================================================
        const existingHash = config.rerunDuplicates ? null : await db.getTestedHash(paramHash);
        if (existingHash) {
          const existingReturn = typeof existingHash.totalReturn === 'number' ? existingHash.totalReturn : parseFloat(existingHash.totalReturn) || 0;
          logger.info(`[Run ${config.runId}] 🔄 Already tested (hash: ${paramHash.substring(0, 8)}...) - P&L: ${existingReturn.toFixed(1)}%`);
          duplicateCount++;
          
          await db.updateBacktestRun(config.runId, {
            duplicateCount: duplicateCount,
          });
          logger.info(`[Run ${config.runId}] Duplicate skipped, duplicateCount: ${duplicateCount}`);
        } else {
          // =====================================================================
          // NEW TEST: Save hash first, then decide if full result is worth saving
          // =====================================================================
          const totalReturn = result.totalReturn || 0;
          
          // Get the minimum profit threshold from settings
          const minProfitThreshold = await db.getMinProfitThreshold();
          
          // Always save to testedHashes (for global deduplication)
          await db.saveTestedHash({
            paramHash,
            totalReturn,
            epic: config.epic,
            indicatorName: result.indicatorName,
            leverage: result.leverage,
            stopLoss: result.stopLoss,
            resultId: null,  // Will be updated if we save full result
            firstRunId: config.runId,
          });
          
          // Only save full result if above threshold
          if (totalReturn >= minProfitThreshold) {
            logger.info(`[Run ${config.runId}] 💰 Profitable (${totalReturn.toFixed(1)}% >= ${minProfitThreshold}%) - buffering full result`);
            resultBuffer.push({
              runId: config.runId,
              epic: config.epic,
              timeframe: result.timeframe || '5m',
              crashProtectionEnabled: result.crashProtectionEnabled || false,
              // HMH (Hold Means Hold) parameters
              hmhEnabled: result.hmhEnabled || false,
              hmhStopLossOffset: result.hmhStopLossOffset ?? null,
              indicatorName: result.indicatorName,
              indicatorParams: result.indicatorParams,
              leverage: result.leverage,
              stopLoss: result.stopLoss,
              // Timing configuration (full DNA)
              timingConfig: config.timingConfig || { mode: 'MarketClose' as const },
              // Signal-based configuration (if enabled)
              signalBasedConfig: config.signalBasedConfig || null,
              // VERSION 3: Enhanced signal-based config and backtest type
              enhancedSignalConfig: config.enhancedSignalConfig || null,
              backtestType: result.backtestType || (config.enhancedSignalConfig?.enabled ? 'signal_based' : 'time_based'),
              // Data source (Capital.com only)
              dataSource: config.dataSource || 'capital',
              paramHash: paramHash,
              initialBalance: result.initialBalance,
              finalBalance: result.finalBalance,
              totalContributions: result.totalContributions,
              totalReturn: result.totalReturn,
              totalTrades: result.totalTrades,
              winningTrades: result.winningTrades,
              losingTrades: result.losingTrades,
              winRate: result.winRate,
              maxDrawdown: result.maxDrawdown,
              sharpeRatio: result.sharpeRatio,
              totalFees: result.totalFees || 0,
              totalSpreadCosts: result.totalSpreadCosts || 0,
              totalOvernightCosts: result.totalOvernightCosts || 0,
              // Margin Level (ML) tracking metrics
              minMarginLevel: result.minMarginLevel || null,
              liquidationCount: result.liquidationCount || 0,
              marginLiquidatedTrades: result.marginLiquidatedTrades || 0,
              totalLiquidationLoss: result.totalLiquidationLoss || 0,
              marginCloseoutLevel: result.marginCloseoutLevel || null,
              trades: result.trades || [],
              dailyBalances: result.dailyBalances || [],
            });
            resultsCount++;
            
            // Flush buffer when it reaches batch size
            if (resultBuffer.length >= RESULT_BATCH_SIZE) {
              await flushResultBuffer();
            }
            
            logger.info(`[Run ${config.runId}] ✅ Result buffered! Buffer: ${resultBuffer.length}/${RESULT_BATCH_SIZE}, Total saved: ${resultsCount}`);
          } else {
            logger.info(`[Run ${config.runId}] ⏭️ Below threshold (${totalReturn.toFixed(1)}% < ${minProfitThreshold}%) - hash saved, result skipped`);
          }
        }
        
        // Track best result
        if (!bestResult || result.finalBalance > bestResult.finalBalance) {
          bestResult = result;
          
          if (bestResult) {
            await db.updateBacktestRun(config.runId, {
              bestResult: {
                indicatorName: bestResult.indicatorName,
                finalBalance: bestResult.finalBalance,
                totalReturn: bestResult.totalReturn,
                winRate: bestResult.winRate,
                sharpeRatio: bestResult.sharpeRatio,
              },
            });
          }
        }
      } catch (error: any) {
        // Handle race condition duplicate key errors gracefully
        // This happens when parallel workers generate the same parameter combination
        if (error?.cause?.code === 'ER_DUP_ENTRY' || error?.message?.includes('Duplicate entry')) {
          logger.info(`[Run ${config.runId}] Race condition duplicate (hash collision) - already saved by another worker`);
          duplicateCount++;
          // Still track as potential best result even if we didn't save it
        } else {
          logger.error(`[Run ${config.runId}] ❌ Failed to parse/save result:`, error);
          console.error(`[Backtest] Failed to parse result:`, error);
        }
      }
    }
  }

  python.stderr.on('data', (data) => {
    stderr += data.toString();
    const stderrLine = data.toString().trim();
    console.log(`[Backtest] ${stderrLine}`);
    // Also log stderr to run log file for debugging
    logger.info(`[STDERR] ${stderrLine}`);
  });

  return new Promise((resolve, reject) => {
    python.on('close', async (code) => {
      const running = runningBacktests.get(config.runId);
      
      // Flush any buffered results before handling exit
      if (resultBuffer.length > 0) {
        logger.info(`[Run ${config.runId}] Flushing ${resultBuffer.length} buffered results before close handling`);
        await flushResultBuffer();
      }
      
      // Check current DB status FIRST - Python may have set it to 'paused' before exiting
      const run = await db.getBacktestRun(config.runId);
      // Consider tests complete if:
      // 1. completedTests === totalTests (all requested tests ran)
      // 2. OR progress === 100 (batch complete handler fired - all AVAILABLE tests ran)
      const allTestsCompleted = run && (
        (run.completedTests === run.totalTests && (run.totalTests ?? 0) > 0) ||
        run.progress === 100
      );
      const isPaused = run?.status === 'paused';
      
      // === PAUSED HANDLING ===
      // Exit code 101 means Python paused (user requested pause via DB status)
      // Also check if DB status is 'paused' (for backwards compatibility with code 0 exits)
      if (code === 101 || isPaused) {
        console.log(`[Backtest] ⏸️ Run ${config.runId} paused at ${run?.completedTests}/${run?.totalTests} tests`);
        logger.info(`[Run ${config.runId}] Paused - Python exited with code ${code}, DB status: ${run?.status}`);
        
        // Release cores on pause to avoid stale allocations
        // The pause mutation also releases cores, but Python can exit with 101 naturally
        // (when it detects paused status from DB), so we release here too (safe to call twice)
        const releasedOnPause = resourceManager.releaseRunCores(config.runId);
        if (releasedOnPause > 0) {
          console.log(`[Backtest] Released ${releasedOnPause} cores on pause (close handler)`);
          logger.info(`[Run ${config.runId}] Released ${releasedOnPause} cores in close handler`);
        }
        
        // Don't change status - Python already set it to 'paused' before exiting
        resolve();
        return;
      }
      
      if (running?.shouldStop) {
        console.log(`[Backtest] Run ${config.runId} was stopped by user`);
        // If all tests completed, mark as completed instead of stopped
        if (allTestsCompleted) {
          await db.updateBacktestRun(config.runId, {
            status: 'completed',
            completedAt: new Date(),
            progress: 100,
            duplicateCount: duplicateCount,
          });
          console.log(`[Backtest] All tests completed, marking as completed despite stop signal`);
        }
        resolve();
        return;
      }
      
      // === PROCESS RECYCLING HANDLING ===
      // Exit code 100 means Python hit the recycle limit and needs to restart fresh
      if (code === 100) {
        console.log(`[Backtest] 🔄 Process recycle triggered - respawning Python to continue`);
        logger.info(`[Run ${config.runId}] Process recycled after ${resultsCount} results, respawning...`);
        
        // Check if parallelCores was updated in DB (dynamic core allocation)
        const freshRun = await db.getBacktestRun(config.runId);
        const freshCores = freshRun?.parallelCores || config.parallelCores || 1;
        let newAllocatedCores = allocatedCores;
        
        if (freshCores !== allocatedCores.length) {
          console.log(`[Backtest] 🔄 Core allocation changed: ${allocatedCores.length} → ${freshCores}`);
          logger.info(`[Run ${config.runId}] Dynamic core reallocation: ${allocatedCores.length} → ${freshCores}`);
          
          // Release old cores and allocate new amount
          resourceManager.releaseRunCores(config.runId);
          const newCores = await resourceManager.allocateCores(config.runId, freshCores);
          newAllocatedCores = newCores;
          
          // Update config with new core count
          config.parallelCores = newCores.length;
        }
        
        // Respawn Python to continue processing
        // Python will query the DB for existing results and skip them automatically
        try {
          await runBatchOptimization(config, logger, newAllocatedCores);
          resolve();
        } catch (err) {
          reject(err);
        }
        return;
      }
      
      if (code !== 0) {
        console.error(`[Backtest] Python error:`, stderr);
        reject(new Error(`Python process exited with code ${code}`));
        return;
      }

      // Only mark as completed if actually completed (all tests done)
      if (allTestsCompleted) {
        await db.updateBacktestRun(config.runId, {
          status: 'completed',
          completedAt: new Date(),
          progress: 100,
          duplicateCount: duplicateCount,
        });

        console.log(`[Backtest] Completed run ${config.runId} with ${resultsCount} results (${duplicateCount} duplicates skipped)`);
        
        // Store duplicate count in run metadata (optional)
        if (duplicateCount > 0) {
          console.log(`[Backtest] ${duplicateCount} tests were skipped because they had already been run before`);
        }
      } else {
        // Python exited with code 0 but tests not complete - likely paused or error
        console.log(`[Backtest] ⚠️ Run ${config.runId} exited early: ${run?.completedTests}/${run?.totalTests} tests completed`);
        logger.warn(`[Run ${config.runId}] Exited with code 0 but only ${run?.completedTests}/${run?.totalTests} tests done - may have been paused`);
      }
      resolve();
    });
  });
}

async function runStandardBacktest(config: BacktestConfig, logger: BacktestLogger): Promise<void> {
  const pythonScript = path.join(__dirname, '../python_engine/backtest_runner.py');
  const dbPath = DB_PATH;
  
  logger.info('Initializing standard backtest');
  
  // Generate all test configurations
  const testConfigs: Array<{
    indicator: string;
    params: Record<string, any>;
    leverage: number;
    stopLoss: number;
    timeframe?: string;
    crashProtection?: boolean;
  }> = [];

  // Leverage and stop loss ranges
  const leverages = [2, 3, 4, 5, 10];
  const stopLosses = [2, 3, 3.5, 4, 5, 7.5, 8, 10];

  for (const indicator of config.indicators) {
    for (let i = 0; i < config.numSamples; i++) {
      const params = generateRandomParams(indicator);
      const leverage = leverages[Math.floor(Math.random() * leverages.length)];
      const stopLoss = stopLosses[Math.floor(Math.random() * stopLosses.length)];
      
      testConfigs.push({
        indicator,
        params,
        leverage,
        stopLoss,
      });
    }
  }

  console.log(`[Backtest] Generated ${testConfigs.length} test configurations`);

  // Determine timeframe(s) to use
  const timeframes = config.timeframeConfig?.selectedTimeframes || ['5m'];
  const timeframeMode = config.timeframeConfig?.mode || 'default';
  
  // For 'multiple' mode, multiply tests by number of timeframes
  let expandedTestConfigs = testConfigs;
  if (timeframeMode === 'multiple' && timeframes.length > 1) {
    expandedTestConfigs = testConfigs.flatMap(testConfig => 
      timeframes.map(timeframe => ({ ...testConfig, timeframe }))
    );
    console.log(`[Backtest] Expanded to ${expandedTestConfigs.length} tests (${testConfigs.length} × ${timeframes.length} timeframes)`);
  } else {
    // For default/random mode, assign timeframe to each test
    const selectedTimeframe = timeframeMode === 'random' && timeframes.length > 0
      ? timeframes[Math.floor(Math.random() * timeframes.length)]
      : timeframes[0];
    expandedTestConfigs = testConfigs.map(testConfig => ({ ...testConfig, timeframe: selectedTimeframe }));
  }

  // Check if this is a resume - count existing results
  const existingResults = await db.getBacktestResults(config.runId);
  const alreadyCompleted = existingResults.length;
  
  await db.updateBacktestRun(config.runId, {
    totalTests: expandedTestConfigs.length,
    completedTests: alreadyCompleted, // Don't reset if resuming
  });
  
  if (alreadyCompleted > 0) {
    console.log(`[Backtest] Resuming run ${config.runId} with ${alreadyCompleted} tests already completed`);
  }

  // Run tests in batches
  const batchSize = 10;
  let completed = alreadyCompleted; // Start from existing count
  let bestResult: BacktestResult | null = null;
  
  for (let i = 0; i < expandedTestConfigs.length; i += batchSize) {
    // Check if should stop
    const running = runningBacktests.get(config.runId);
    if (running?.shouldStop) {
      console.log(`[Backtest] Run ${config.runId} stopped by user`);
      break;
    }

    const batch = expandedTestConfigs.slice(i, i + batchSize);
    
    // Run batch in parallel - each test runs TWICE (with and without crash protection)
    const testsToRun = batch.flatMap(testConfig => [
      { ...testConfig, crashProtection: false },
      { ...testConfig, crashProtection: true }
    ]);
    
    const batchResults = await Promise.all(
      testsToRun.map(testConfig => runSingleTest(
        config.runId,
        pythonScript,
        dbPath,
        config.epic,
        config.startDate,
        config.endDate,
        testConfig.indicator,
        testConfig.params,
        testConfig.leverage,
        testConfig.stopLoss,
        config.initialBalance,
        config.monthlyTopup,
        config.investmentPct,
        config.direction,
        config.timingConfig,
        config.calculationMode,
        testConfig.timeframe || '5m',
        config.stopConditions,
        testConfig.crashProtection
      ))
    );

    // Save results to database (including failed tests) with ALL configuration
    for (const result of batchResults) {
      // Save all results, even failed ones with default values
      await db.createBacktestResult({
          runId: config.runId,
          epic: config.epic,
          indicatorName: result?.indicatorName || 'unknown',
          indicatorParams: result?.indicatorParams || {},
          leverage: result?.leverage || 1,
          stopLoss: result?.stopLoss || 0,
          // Timing configuration (full DNA)
          timingConfig: config.timingConfig || { mode: 'MarketClose' as const },
          // Signal-based configuration (if enabled)
          signalBasedConfig: config.signalBasedConfig || null,
          // Data source (Capital.com only)
          dataSource: config.dataSource || 'capital',
          timeframe: result?.timeframe || '5m',
          initialBalance: result?.initialBalance || config.initialBalance,
          finalBalance: result?.finalBalance || 0,
          totalContributions: result?.totalContributions || 0,
          totalReturn: result?.totalReturn || -100,
          totalTrades: result?.totalTrades || 0,
          winningTrades: result?.winningTrades || 0,
          losingTrades: result?.losingTrades || 0,
          winRate: result?.winRate || 0,
          maxDrawdown: result?.maxDrawdown || -100,
          sharpeRatio: result?.sharpeRatio || -999,
          totalFees: result?.totalFees || 0,
          totalSpreadCosts: result?.totalSpreadCosts || 0,
          totalOvernightCosts: result?.totalOvernightCosts || 0,
          // Margin Level (ML) tracking metrics
          minMarginLevel: result?.minMarginLevel || null,
          liquidationCount: result?.liquidationCount || 0,
          marginLiquidatedTrades: result?.marginLiquidatedTrades || 0,
          totalLiquidationLoss: result?.totalLiquidationLoss || 0,
          marginCloseoutLevel: result?.marginCloseoutLevel || null,
          trades: result?.trades || [],
          dailyBalances: result?.dailyBalances || [],
        });
        
        if (result && (!bestResult || result.finalBalance > bestResult.finalBalance)) {
          bestResult = result;
        }
    }

    // Update progress
    completed += batch.length;
    const progress = Math.floor((completed / expandedTestConfigs.length) * 100);
    
    await db.updateBacktestRun(config.runId, {
      progress,
      completedTests: completed,
      bestResult: bestResult ? {
        indicatorName: bestResult.indicatorName,
        finalBalance: bestResult.finalBalance,
        totalReturn: bestResult.totalReturn,
        winRate: bestResult.winRate,
        sharpeRatio: bestResult.sharpeRatio,
      } : null,
    });

    console.log(`[Backtest] Progress: ${completed}/${testConfigs.length} (${progress}%)`);
  }

  // Mark as completed (check if stopped early or completed all tests)
  const run = await db.getBacktestRun(config.runId);
  const allTestsCompleted = completed === testConfigs.length;
  
  if (allTestsCompleted || run?.status !== 'stopped') {
    await db.updateBacktestRun(config.runId, {
      status: 'completed',
      completedAt: new Date(),
      progress: 100,
    });
    console.log(`[Backtest] Completed run ${config.runId}`);
  } else {
    console.log(`[Backtest] Run ${config.runId} stopped at ${completed}/${testConfigs.length} tests`);
  }
}

function generateRandomParams(indicator: string): Record<string, any> {
  const paramRanges: Record<string, Record<string, any[]>> = {
    'rsi_oversold': {
      'period': [7, 10, 14, 20],
      'threshold': Array.from({ length: 16 }, (_, i) => 20 + i),
    },
    'rsi_overbought': {
      'period': [7, 10, 14, 20],
      'threshold': Array.from({ length: 16 }, (_, i) => 65 + i),
    },
    'rsi_bullish_cross_50': {
      'period': [7, 10, 14, 20],
    },
    'rsi_bearish_cross_50': {
      'period': [7, 10, 14, 20],
    },
    'bb_lower_break': {
      'period': [10, 15, 20, 25],
      'std_dev': [1.5, 2.0, 2.5, 3.0],
    },
    'bb_upper_break': {
      'period': [10, 15, 20, 25],
      'std_dev': [1.5, 2.0, 2.5, 3.0],
    },
    'macd_bullish_cross': {
      'fast_period': [8, 12, 13],
      'slow_period': [21, 26, 30],
      'signal_period': [7, 9, 11],
    },
    'macd_positive': {
      'fast_period': [8, 12, 13],
      'slow_period': [21, 26, 30],
      'signal_period': [7, 9, 11],
      'threshold': [0.01, 0.05, 0.1],
    },
    'price_above_vwap': {
      'threshold': [-0.02, -0.01, 0.0, 0.01],
    },
    'roc_below_threshold': {
      'period': [5, 10, 15, 20],
      'threshold': [-3.0, -2.0, -1.0, -0.5],
    },
  };

  const ranges = paramRanges[indicator] || {};
  const params: Record<string, any> = {};

  for (const [paramName, values] of Object.entries(ranges)) {
    params[paramName] = values[Math.floor(Math.random() * values.length)];
  }

  return params;
}

async function runSingleTest(
  runId: number,
  scriptPath: string,
  dbPath: string,
  epic: string,
  startDate: string,
  endDate: string,
  indicator: string,
  params: Record<string, any>,
  leverage: number,
  stopLoss: number,
  initialBalance: number,
  monthlyTopup: number,
  investmentPct: number,
  direction: string,
  timingConfig?: any,
  calculationMode?: string,
  timeframe?: string,
  stopConditions?: any,
  crashProtectionEnabled?: boolean,
  dataSource?: 'capital'
): Promise<BacktestResult | null> {
  return new Promise((resolve) => {
    const config = JSON.stringify({
      db_path: dbPath,
      epic,
      start_date: startDate,
      end_date: endDate,
      indicator,
      params,
      leverage,
      stop_loss: stopLoss,
      initial_balance: initialBalance,
      monthly_topup: monthlyTopup,
      investment_pct: investmentPct,
      direction,
      timing_config: timingConfig,
      calculation_mode: calculationMode || 'standard',
      timeframe: timeframe || '5m',
      stop_conditions: stopConditions,
      crash_protection_enabled: crashProtectionEnabled || false,
      data_source: dataSource || 'capital',  // All data from Capital.com (UTC)
    });

    const python = spawnPython(scriptPath, { args: [config] });
    
    // Track process
    const running = runningBacktests.get(runId);
    if (running) {
      running.processes.push(python);
    }
    
    let stdout = '';
    let stderr = '';

    python.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    python.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    python.on('close', (code) => {
      // Remove from tracking
      if (running) {
        const index = running.processes.indexOf(python);
        if (index > -1) {
          running.processes.splice(index, 1);
        }
      }

      if (code !== 0) {
        console.error(`[Backtest] Python error:`, stderr);
        resolve(null);
        return;
      }

      try {
        const lines = stdout.split('\n');
        
        // Try to find RESULT: prefixed line first
        let jsonLine = lines.find(line => line.trim().startsWith('RESULT:'));
        if (jsonLine) {
          jsonLine = jsonLine.replace(/^RESULT:/, '').trim();
        } else {
          // Fall back to plain JSON line
          jsonLine = lines.find(line => line.trim().startsWith('{'));
        }
        
        if (jsonLine) {
          const result = JSON.parse(jsonLine);
          resolve(result);
        } else {
          console.error(`[Backtest] No JSON output found in stdout:`, stdout.substring(0, 200));
          resolve(null);
        }
      } catch (error) {
        console.error(`[Backtest] Failed to parse result:`, error);
        resolve(null);
      }
    });
  });
}

