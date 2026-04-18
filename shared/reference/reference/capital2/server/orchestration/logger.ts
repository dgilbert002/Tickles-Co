// Database logging disabled - using file logs instead
// import { getDb } from '../db';
// import { executionLogs } from '../../drizzle/schema';

/**
 * Orchestration Logger - Comprehensive logging for bot orchestration
 * Logs EVERYTHING: API calls, errors, decisions, database operations, etc.
 */

import { ClosingLogger } from '../logging/file_logger';

export type LogLevel = 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';

export type LogEvent = 
  // Timer events
  | 'TIMER_STARTED'
  | 'TIMER_STOPPED'
  | 'WINDOW_TRIGGERED'
  | 'MARKET_CLOSE_DETECTED'
  
  // API Queue events
  | 'API_CALL_QUEUED'
  | 'API_CALL_FIRED'
  | 'API_CALL_SUCCESS'
  | 'API_CALL_FAILED'
  | 'API_CALL_EXPIRED'
  | 'API_CALL_RETRY'
  
  // Brain calculation events
  | 'BRAIN_STARTED'
  | 'BRAIN_DATA_FETCHED'
  | 'BRAIN_TEST_RESULT'
  | 'BRAIN_DECISION'
  | 'BRAIN_ERROR'
  
  // Trade execution events
  | 'LEVERAGE_ADJUSTED'
  | 'POSITION_CLOSE_REQUESTED'
  | 'POSITION_CLOSE_SUCCESS'
  | 'POSITION_CLOSE_FAILED'
  | 'BALANCE_FETCHED'
  | 'BIDASK_FETCHED'
  | 'POSITION_SIZE_CALCULATED'
  | 'POSITION_OPEN_REQUESTED'
  | 'POSITION_OPEN_SUCCESS'
  | 'POSITION_OPEN_FAILED'
  
  // Database events
  | 'DB_INSERT'
  | 'DB_UPDATE'
  | 'DB_DELETE'
  | 'DB_QUERY_FAILED'
  
  // Data sync events
  | 'DATA_SYNC_STARTED'
  | 'DATA_SYNC_COMPLETED'
  | 'DATA_SYNC_FAILED'
  
  // Bot lifecycle events
  | 'BOT_STARTED'
  | 'BOT_STOPPED'
  | 'BOT_PAUSED'
  | 'BOT_RESUMED'
  | 'BOT_ERROR'
  
  // Trade validation events
  | 'TRADE_VALIDATION_STARTED'
  | 'TRADE_VALIDATION_RETRY'
  | 'TRADE_VALIDATION_SUCCESS'
  | 'TRADE_VALIDATION_FAILED'
  | 'TRADE_VALIDATION_ERROR'
  | 'LEVERAGE_ADJUSTED'
  
  // Deal validation events
  | 'DEAL_VALIDATION_REQUESTED'
  | 'DEAL_VALIDATION_SUCCESS'
  | 'DEAL_VALIDATION_FAILED'
  
  // Market hours events
  | 'MARKET_HOURS_FETCH_STARTED'
  | 'MARKET_HOURS_FETCHED'
  | 'MARKET_HOURS_FETCH_FAILED'
  | 'MARKET_HOURS_UPDATE_FAILED'
  | 'MARKET_STATE_CHANGED'
  
  // T-60 trigger events (Fake5min_4thCandle mode)
  | 'T60_CAPTURE_STARTED'
  | 'T60_CAPTURE_COMPLETE'
  | 'T60_CAPTURE_FAILED'
  | 'T60_BRAIN'
  | 'T60_QUEUE'
  | 'T60_CLOSE_SUCCESS'
  
  // Candle trigger events
  | 'CANDLE_TRIGGER'
  
  // Session events
  | 'SESSION_KEEPALIVE'
  | 'SESSION_ERROR'
  
  // Trade polling events
  | 'TRADE_POLL'
  
  // General events
  | 'SYSTEM_ERROR'
  | 'VALIDATION_ERROR';

interface LogEntry {
  level: LogLevel;
  event: LogEvent;
  message: string;
  accountId?: number;
  strategyId?: number;
  epic?: string;
  data?: any;
  error?: {
    message: string;
    stack?: string;
    code?: string;
  };
  timestamp: Date;
}

class OrchestrationLogger {
  private static instance: OrchestrationLogger;

  // Optional TRACE sink: when enabled, mirror orchestration logs into the active per-window closing log file.
  // This is turned on/off by the timer when a quiet period starts/ends.
  private closingTraceLogger: ClosingLogger | null = null;
  private closingTraceEnabled: boolean = false;
  private closingTraceWindowCloseTime: string | null = null;

  private constructor() {}

  static getInstance(): OrchestrationLogger {
    if (!OrchestrationLogger.instance) {
      OrchestrationLogger.instance = new OrchestrationLogger();
    }
    return OrchestrationLogger.instance;
  }

  /**
   * Log a message with full context
   */
  async log(entry: Omit<LogEntry, 'timestamp'>): Promise<void> {
    const fullEntry: LogEntry = {
      ...entry,
      timestamp: new Date(),
    };

    // Console logging with color coding
    this.logToConsole(fullEntry);

    // Optional file trace (per-window closing log)
    this.logToClosingTrace(fullEntry);

    // Database logging (async, non-blocking)
    this.logToDatabase(fullEntry).catch(err => {
      console.error('[OrchestrationLogger] Failed to write to database:', err);
    });
  }

  /**
   * Enable/disable per-window TRACE logging into the active closing log file.
   *
   * IMPORTANT:
   * - This is intentionally time-bounded: the timer sets it during quiet period only.
   * - We sanitize API payloads to avoid logging secrets and to keep logs readable.
   */
  setClosingTraceLogger(logger: ClosingLogger | null, enabled: boolean, windowCloseTime?: string): void {
    this.closingTraceLogger = logger;
    this.closingTraceEnabled = enabled;
    this.closingTraceWindowCloseTime = windowCloseTime || null;
    if (enabled && logger) {
      logger.log(`🧾 TRACE MODE ENABLED for window ${windowCloseTime || 'unknown'}`);
    }
  }

  private sanitizeApiParams(endpoint: string, method: string, params: any): any {
    if (!params) return params;
    // Shallow clone is enough for our current usage
    const safe = { ...params };
    // Never log passwords
    if (endpoint === '/session' && method === 'POST') {
      if (typeof safe.password === 'string') safe.password = '***REDACTED***';
      if (typeof safe.identifier === 'string') safe.identifier = '***REDACTED***';
    }
    return safe;
  }

  private sanitizeApiResponse(endpoint: string, response: any): any {
    if (!response) return response;
    // Prices endpoints can be huge; summarize to keep logs practical.
    if (typeof endpoint === 'string' && endpoint.startsWith('/prices/')) {
      const prices = (response as any)?.prices;
      if (Array.isArray(prices)) {
        const first = prices[0];
        const last = prices[prices.length - 1];
        return {
          ...response,
          prices: {
            count: prices.length,
            first,
            last,
          },
        };
      }
    }
    return response;
  }

  private logToClosingTrace(entry: LogEntry): void {
    if (!this.closingTraceEnabled || !this.closingTraceLogger) return;

    try {
      const tracePrefix = `[TRACE][${entry.level}][${entry.event}]`;
      const ctx = {
        accountId: entry.accountId,
        strategyId: entry.strategyId,
        epic: entry.epic,
        data: entry.data,
        error: entry.error,
      };

      // Special formatting for API calls (request/response)
      if (
        entry.event === 'API_CALL_FIRED' ||
        entry.event === 'API_CALL_SUCCESS' ||
        entry.event === 'API_CALL_FAILED' ||
        entry.event === 'API_CALL_QUEUED'
      ) {
        const endpoint = entry.data?.endpoint || 'unknown';
        const method = entry.data?.method || 'unknown';
        const durationMs = entry.data?.durationMs;
        const params = this.sanitizeApiParams(endpoint, method, entry.data?.params);
        const response = this.sanitizeApiResponse(endpoint, entry.data?.response);

        if (entry.event === 'API_CALL_FIRED') {
          this.closingTraceLogger.log(`${tracePrefix} ▶️ ${method} ${endpoint}`, { ...ctx, data: { ...entry.data, params } });
          return;
        }

        if (entry.event === 'API_CALL_SUCCESS') {
          // Use the file logger's structured API section
          (this.closingTraceLogger as any).api(method, endpoint, params, response, undefined, durationMs);
          return;
        }

        if (entry.event === 'API_CALL_FAILED') {
          (this.closingTraceLogger as any).api(method, endpoint, params, response, entry.error, durationMs);
          return;
        }

        // queued/other
        this.closingTraceLogger.log(`${tracePrefix} ${method} ${endpoint}`, { ...ctx, data: { ...entry.data, params, response } });
        return;
      }

      // Default: mirror event + data
      this.closingTraceLogger.log(`${tracePrefix} ${entry.message}`, ctx);
    } catch (err: any) {
      // Never let tracing crash the system
      console.error('[OrchestrationLogger] Failed to write to closing trace log:', err?.message || err);
    }
  }

  /**
   * Log to console with color coding
   */
  private logToConsole(entry: LogEntry): void {
    const colors = {
      DEBUG: '\x1b[36m', // Cyan
      INFO: '\x1b[32m',  // Green
      WARN: '\x1b[33m',  // Yellow
      ERROR: '\x1b[31m', // Red
    };
    const reset = '\x1b[0m';

    const color = colors[entry.level];
    const timestamp = entry.timestamp.toISOString();
    const prefix = `${color}[${entry.level}]${reset} [${entry.event}]`;
    
    let logMessage = `${prefix} ${entry.message}`;
    
    // Add context
    if (entry.accountId) logMessage += ` | Account: ${entry.accountId}`;
    if (entry.strategyId) logMessage += ` | Strategy: ${entry.strategyId}`;
    if (entry.epic) logMessage += ` | Epic: ${entry.epic}`;
    
    console.log(`[${timestamp}] ${logMessage}`);
    
    // Log data if present
    if (entry.data) {
      console.log(`  Data:`, JSON.stringify(entry.data, null, 2));
    }
    
    // Log error if present
    if (entry.error) {
      console.error(`  Error: ${entry.error.message}`);
      if (entry.error.stack) {
        console.error(`  Stack: ${entry.error.stack}`);
      }
    }
  }

  /**
   * Map log event to execution phase
   */
  private mapEventToPhase(event: LogEvent): 'brain' | 'close' | 'open' {
    if (event.includes('BRAIN')) return 'brain';
    if (event.includes('CLOSE') || event === 'LEVERAGE_ADJUSTED') return 'close';
    if (event.includes('OPEN') || event.includes('BALANCE') || event.includes('BIDASK')) return 'open';
    return 'brain'; // default
  }

  /**
   * Log to database - DISABLED
   * Database execution_logs table is no longer used. 
   * All logging now goes to file logs in logs/closing/, logs/brain/, logs/strategy/
   */
  private async logToDatabase(entry: LogEntry): Promise<void> {
    // NO-OP: Database logging disabled - using file logs instead
    // The execution_logs table can be dropped from the database
  }

  /**
   * Generate human-readable message from log entry
   */
  private generateHumanMessage(entry: LogEntry): string {
    const { event, message, data, epic, accountId } = entry;

    // API calls - Success
    if (event === 'API_CALL_SUCCESS' && data?.endpoint) {
      const duration = data.durationMs ? ` (${data.durationMs}ms)` : '';
      
      // GET /accounts
      if (data.endpoint === '/accounts' && data.method === 'GET') {
        return `Fetched accounts from Capital.com${duration}`;
      }
      
      // GET /positions
      if (data.endpoint === '/positions' && data.method === 'GET') {
        return `Fetched open positions from Capital.com${duration}`;
      }
      
      // POST /positions (open trade)
      if (data.endpoint === '/positions' && data.method === 'POST') {
        const dealId = data.dealId || 'unknown';
        return `Opened position for ${epic || 'unknown'} (Deal: ${dealId})${duration}`;
      }
      
      // DELETE /positions (close trade)
      if (data.endpoint && data.endpoint.includes('/positions') && data.method === 'DELETE') {
        const dealId = data.dealId || 'unknown';
        return `Closed position (Deal ID: ${dealId})${duration}`;
      }
      
      // Leverage adjustment
      if (data.endpoint && data.endpoint.includes('/accounts/preferences')) {
        return `Adjusted leverage to ${data.params?.leverage || 'N/A'}x for account ${accountId}${duration}`;
      }
      
      // Generic success
      return `${data.method} ${data.endpoint} succeeded${duration}`;
    }

    // API calls - Fired
    if (event === 'API_CALL_FIRED' && data?.endpoint) {
      return `Calling ${data.method} ${data.endpoint}...`;
    }

    // API calls - Failed
    if (event === 'API_CALL_FAILED' && data?.endpoint) {
      const duration = data.durationMs ? ` (${data.durationMs}ms)` : '';
      return `${data.method} ${data.endpoint} failed${duration}`;
    }

    // Brain calculations
    if (event === 'BRAIN_DECISION') {
      const decision = data?.decision || 'unknown';
      return `Brain decision for ${epic || 'unknown'}: ${decision.toUpperCase()}`;
    }

    // Trade execution
    if (event === 'POSITION_OPEN_SUCCESS') {
      return `Successfully opened ${data?.direction || 'long'} position for ${epic} with ${data?.leverage || 1}x leverage`;
    }
    if (event === 'POSITION_CLOSE_SUCCESS') {
      return `Successfully closed position for ${epic} (P&L: ${data?.pnl || 'N/A'})`;
    }

    // Default: use original message
    return message;
  }

  /**
   * Convenience methods for different log levels
   */
  async debug(event: LogEvent, message: string, context?: Partial<LogEntry>): Promise<void> {
    await this.log({ level: 'DEBUG', event, message, ...context });
  }

  async info(event: LogEvent, message: string, context?: Partial<LogEntry>): Promise<void> {
    await this.log({ level: 'INFO', event, message, ...context });
  }

  async warn(event: LogEvent, message: string, context?: Partial<LogEntry>): Promise<void> {
    await this.log({ level: 'WARN', event, message, ...context });
  }

  async error(event: LogEvent, message: string, context?: Partial<LogEntry>): Promise<void> {
    await this.log({ level: 'ERROR', event, message, ...context });
  }

  /**
   * Log an error with full stack trace
   */
  async logError(event: LogEvent, message: string, error: Error, context?: Partial<LogEntry>): Promise<void> {
    await this.log({
      level: 'ERROR',
      event,
      message,
      error: {
        message: error.message,
        stack: error.stack,
        code: (error as any).code,
      },
      ...context,
    });
  }

  /**
   * Log an API call with full details
   */
  async logApiCall(
    event: 'API_CALL_QUEUED' | 'API_CALL_FIRED' | 'API_CALL_SUCCESS' | 'API_CALL_FAILED',
    endpoint: string,
    method: string,
    params: any,
    response?: any,
    error?: Error,
    context?: Partial<LogEntry>
  ): Promise<void> {
    const level: LogLevel = event === 'API_CALL_FAILED' ? 'ERROR' : 'INFO';
    
    // Merge context data with API call data
    const mergedData = {
      endpoint,
      method,
      params,
      response,
      ...context?.data,  // Merge additional context data (like durationMs)
    };

    await this.log({
      level,
      event,
      message: `${method} ${endpoint}`,
      ...context,  // Spread context first
      data: mergedData,  // Then override data with merged version
      error: error ? {
        message: error.message,
        stack: error.stack,
      } : undefined,
    });
  }

  /**
   * Log a database operation
   */
  async logDbOperation(
    operation: 'INSERT' | 'UPDATE' | 'DELETE',
    table: string,
    recordId?: number,
    data?: any,
    error?: Error
  ): Promise<void> {
    const event: LogEvent = error ? 'DB_QUERY_FAILED' : 
      operation === 'INSERT' ? 'DB_INSERT' :
      operation === 'UPDATE' ? 'DB_UPDATE' : 'DB_DELETE';
    
    const level: LogLevel = error ? 'ERROR' : 'DEBUG';
    
    await this.log({
      level,
      event,
      message: `${operation} ${table}${recordId ? ` (ID: ${recordId})` : ''}`,
      data: { table, recordId, data },
      error: error ? {
        message: error.message,
        stack: error.stack,
      } : undefined,
    });
  }

  /**
   * Log a trade execution with full context
   */
  async logTradeExecution(
    action: 'open' | 'close',
    epic: string,
    accountId: number,
    strategyId: number,
    dealId?: string,
    tradeData?: {
      direction?: 'long' | 'short';
      price?: number;
      size?: number;
      leverage?: number;
      stopLoss?: number;
      pnl?: number;
    },
    error?: Error
  ): Promise<void> {
    const event: LogEvent = error ? 
      (action === 'open' ? 'POSITION_OPEN_FAILED' : 'POSITION_CLOSE_FAILED') :
      (action === 'open' ? 'POSITION_OPEN_SUCCESS' : 'POSITION_CLOSE_SUCCESS');

    const message = action === 'open' ?
      `Opening ${tradeData?.direction || 'long'} position for ${epic} at ${tradeData?.price || 'N/A'}` :
      `Closing position for ${epic} (Deal ID: ${dealId || 'N/A'})`;

    await this.log({
      level: error ? 'ERROR' : 'INFO',
      event,
      message,
      accountId,
      epic,
      data: {
        strategyId,
        dealId,
        ...tradeData,
      },
      error: error ? {
        message: error.message,
        stack: error.stack,
      } : undefined,
    });
  }

  /**
   * Log a brain calculation with decision
   */
  async logBrainCalculation(
    epic: string,
    accountId: number,
    strategyId: number,
    decision: 'buy' | 'hold' | 'sell',
    brainResult: any,
    error?: Error
  ): Promise<void> {
    const event: LogEvent = error ? 'BRAIN_ERROR' : 'BRAIN_DECISION';

    await this.log({
      level: error ? 'ERROR' : 'INFO',
      event,
      message: `Brain calculation for ${epic}: ${decision.toUpperCase()}`,
      accountId,
      epic,
      data: {
        strategyId,
        decision,
        brainResult,
      },
      error: error ? {
        message: error.message,
        stack: error.stack,
      } : undefined,
    });
  }

  /**
   * Log a leverage adjustment
   */
  async logLeverageAdjustment(
    accountId: number,
    fromLeverage: number,
    toLeverage: number,
    success: boolean,
    error?: Error
  ): Promise<void> {
    const event: LogEvent = success ? 'LEVERAGE_ADJUSTED' : 'SYSTEM_ERROR';

    await this.log({
      level: success ? 'INFO' : 'ERROR',
      event,
      message: `Leverage adjusted from ${fromLeverage}x to ${toLeverage}x for account ${accountId}`,
      accountId,
      data: {
        fromLeverage,
        toLeverage,
      },
      error: error ? {
        message: error.message,
        stack: error.stack,
      } : undefined,
    });
  }
}

// Export singleton instance
export const orchestrationLogger = OrchestrationLogger.getInstance();

/**
 * API Call Timing Tracker
 * Tracks timing between API calls to identify rate limiting issues
 */
interface ApiCallRecord {
  id: number;
  timestamp: number;  // When call started
  endTimestamp?: number;  // When response received
  method: string;
  endpoint: string;
  duration?: number;  // How long the call took
  gapFromPrevious?: number;  // Time since previous call started
  status: 'started' | 'success' | 'failed' | 'rate_limited';
  error?: string;
}

class ApiTimingTracker {
  private static instance: ApiTimingTracker;
  private calls: ApiCallRecord[] = [];
  private callCounter = 0;
  private sessionStartTime: number = Date.now();
  private enabled = true;
  
  static getInstance(): ApiTimingTracker {
    if (!ApiTimingTracker.instance) {
      ApiTimingTracker.instance = new ApiTimingTracker();
    }
    return ApiTimingTracker.instance;
  }
  
  /**
   * Reset the tracker (call at start of test/simulation)
   */
  reset(): void {
    this.calls = [];
    this.callCounter = 0;
    this.sessionStartTime = Date.now();
    console.log(`[API-Timing] 🔄 Tracker reset at ${new Date().toISOString()}`);
  }
  
  /**
   * Track an API call start
   */
  startCall(method: string, endpoint: string): number {
    const now = Date.now();
    const id = ++this.callCounter;
    
    // Calculate gap from previous call
    let gapFromPrevious: number | undefined;
    if (this.calls.length > 0) {
      const lastCall = this.calls[this.calls.length - 1];
      gapFromPrevious = now - lastCall.timestamp;
    }
    
    const record: ApiCallRecord = {
      id,
      timestamp: now,
      method,
      endpoint,
      gapFromPrevious,
      status: 'started',
    };
    
    this.calls.push(record);
    
    // Log with warning if calls are too close
    const relativeTime = now - this.sessionStartTime;
    const gapStr = gapFromPrevious !== undefined ? `gap=${gapFromPrevious}ms` : 'first call';
    
    if (gapFromPrevious !== undefined && gapFromPrevious < 200) {
      console.log(`[API-Timing] ⚠️ #${id} START ${method} ${endpoint} @ +${relativeTime}ms (${gapStr}) ** TOO FAST **`);
    } else if (this.enabled) {
      console.log(`[API-Timing] ▶️ #${id} START ${method} ${endpoint} @ +${relativeTime}ms (${gapStr})`);
    }
    
    return id;
  }
  
  /**
   * Track an API call completion
   */
  endCall(id: number, status: 'success' | 'failed' | 'rate_limited', error?: string): void {
    const now = Date.now();
    const record = this.calls.find(c => c.id === id);
    
    if (!record) {
      console.warn(`[API-Timing] Call #${id} not found`);
      return;
    }
    
    record.endTimestamp = now;
    record.duration = now - record.timestamp;
    record.status = status;
    record.error = error;
    
    const relativeTime = now - this.sessionStartTime;
    const statusIcon = status === 'success' ? '✅' : status === 'rate_limited' ? '🚫' : '❌';
    
    if (status === 'rate_limited') {
      console.log(`[API-Timing] ${statusIcon} #${id} RATE LIMITED ${record.method} ${record.endpoint} @ +${relativeTime}ms (took ${record.duration}ms)`);
    } else if (this.enabled || status === 'failed') {
      console.log(`[API-Timing] ${statusIcon} #${id} ${status.toUpperCase()} ${record.method} ${record.endpoint} @ +${relativeTime}ms (took ${record.duration}ms)`);
    }
    
    // Record to persistent stats collector (deferred import to avoid circular deps)
    try {
      apiStatsCollector.recordCall(
        record.method,
        record.endpoint,
        record.duration,
        record.gapFromPrevious,
        status
      );
    } catch (e) {
      // Stats collection is optional, don't break main flow
    }
  }
  
  /**
   * Get summary of API calls
   */
  getSummary(): {
    totalCalls: number;
    successCalls: number;
    failedCalls: number;
    rateLimitedCalls: number;
    avgDuration: number;
    minGap: number;
    callsUnder200ms: number;
    callsUnder100ms: number;
  } {
    const completed = this.calls.filter(c => c.status !== 'started');
    const successCalls = completed.filter(c => c.status === 'success').length;
    const failedCalls = completed.filter(c => c.status === 'failed').length;
    const rateLimitedCalls = completed.filter(c => c.status === 'rate_limited').length;
    
    const durations = completed.filter(c => c.duration).map(c => c.duration!);
    const avgDuration = durations.length > 0 
      ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length)
      : 0;
    
    const gaps = this.calls.filter(c => c.gapFromPrevious !== undefined).map(c => c.gapFromPrevious!);
    const minGap = gaps.length > 0 ? Math.min(...gaps) : 0;
    const callsUnder200ms = gaps.filter(g => g < 200).length;
    const callsUnder100ms = gaps.filter(g => g < 100).length;
    
    return {
      totalCalls: this.calls.length,
      successCalls,
      failedCalls,
      rateLimitedCalls,
      avgDuration,
      minGap,
      callsUnder200ms,
      callsUnder100ms,
    };
  }
  
  /**
   * Print summary to console
   */
  printSummary(): void {
    const summary = this.getSummary();
    const duration = Date.now() - this.sessionStartTime;
    
    console.log('\n' + '═'.repeat(80));
    console.log('  API TIMING SUMMARY');
    console.log('═'.repeat(80));
    console.log(`  Session duration: ${(duration / 1000).toFixed(1)}s`);
    console.log(`  Total API calls: ${summary.totalCalls}`);
    console.log(`  ├─ Success: ${summary.successCalls}`);
    console.log(`  ├─ Failed: ${summary.failedCalls}`);
    console.log(`  └─ Rate Limited: ${summary.rateLimitedCalls}`);
    console.log(`  Average duration: ${summary.avgDuration}ms`);
    console.log(`  Minimum gap between calls: ${summary.minGap}ms`);
    if (summary.callsUnder200ms > 0) {
      console.log(`  ⚠️ Calls under 200ms gap: ${summary.callsUnder200ms}`);
    }
    if (summary.callsUnder100ms > 0) {
      console.log(`  🚨 Calls under 100ms gap: ${summary.callsUnder100ms}`);
    }
    console.log('═'.repeat(80) + '\n');
  }
  
  /**
   * Print detailed timeline of all API calls
   */
  printTimeline(): void {
    console.log('\n' + '═'.repeat(100));
    console.log('  API CALL TIMELINE - DETAILED VIEW');
    console.log('═'.repeat(100));
    console.log('');
    console.log('  #   | Start     | End       | Duration | Gap      | Status | Method | Endpoint');
    console.log('  ' + '─'.repeat(96));
    
    // Track concurrent calls
    const concurrentWarnings: string[] = [];
    
    for (const call of this.calls) {
      const startRel = call.timestamp - this.sessionStartTime;
      const endRel = call.endTimestamp ? call.endTimestamp - this.sessionStartTime : 0;
      const duration = call.duration || 0;
      const gap = call.gapFromPrevious;
      
      // Check for concurrent calls (call started before previous finished)
      if (call.id > 1) {
        const prevCall = this.calls.find(c => c.id === call.id - 1);
        if (prevCall && prevCall.endTimestamp && call.timestamp < prevCall.endTimestamp) {
          concurrentWarnings.push(`#${call.id} started while #${call.id - 1} still running (overlap: ${prevCall.endTimestamp - call.timestamp}ms)`);
        }
      }
      
      // Format each field
      const idStr = `#${call.id}`.padStart(4);
      const startStr = `+${startRel}ms`.padStart(9);
      const endStr = call.endTimestamp ? `+${endRel}ms`.padStart(9) : '...'.padStart(9);
      const durStr = `${duration}ms`.padStart(8);
      const gapStr = gap !== undefined ? (gap < 200 ? `${gap}ms ⚠️` : `${gap}ms`).padStart(8) : '-'.padStart(8);
      
      let statusIcon = '⏳';
      if (call.status === 'success') statusIcon = '✅';
      else if (call.status === 'failed') statusIcon = '❌';
      else if (call.status === 'rate_limited') statusIcon = '🚫';
      
      const methodStr = call.method.padEnd(6);
      const endpointStr = call.endpoint.substring(0, 45);
      
      console.log(`  ${idStr} | ${startStr} | ${endStr} | ${durStr} | ${gapStr} | ${statusIcon}     | ${methodStr} | ${endpointStr}`);
    }
    
    console.log('  ' + '─'.repeat(96));
    
    // Print concurrent warnings
    if (concurrentWarnings.length > 0) {
      console.log('');
      console.log('  🚨 CONCURRENT/OVERLAPPING CALLS DETECTED:');
      for (const warning of concurrentWarnings) {
        console.log(`     ${warning}`);
      }
    }
    
    // Print gap analysis
    const gaps = this.calls.filter(c => c.gapFromPrevious !== undefined).map(c => ({ id: c.id, gap: c.gapFromPrevious!, endpoint: c.endpoint }));
    const fastGaps = gaps.filter(g => g.gap < 200).sort((a, b) => a.gap - b.gap);
    
    if (fastGaps.length > 0) {
      console.log('');
      console.log('  ⚠️ FASTEST GAPS (under 200ms):');
      for (const g of fastGaps.slice(0, 10)) {
        console.log(`     #${g.id}: ${g.gap}ms before ${g.endpoint}`);
      }
    }
    
    console.log('');
    console.log('═'.repeat(100) + '\n');
  }
  
  /**
   * Get all calls for detailed analysis
   */
  getAllCalls(): ApiCallRecord[] {
    return [...this.calls];
  }
  
  /**
   * Enable/disable verbose logging
   */
  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
  }
}

export const apiTimingTracker = ApiTimingTracker.getInstance();

/**
 * Persistent API Statistics Collector
 * Aggregates API call timing data across multiple sessions for analysis
 */
interface ApiEndpointStats {
  endpoint: string;
  method: string;
  totalCalls: number;
  successCalls: number;
  failedCalls: number;
  rateLimitedCalls: number;
  totalDurationMs: number;
  minDurationMs: number;
  maxDurationMs: number;
  avgDurationMs: number;
  // Gap analysis
  totalGapMs: number;
  minGapMs: number;
  maxGapMs: number;
  avgGapMs: number;
  gapsUnder200ms: number;
  gapsUnder100ms: number;
}

interface ApiStatsSession {
  sessionId: string;
  startTime: Date;
  endTime?: Date;
  totalCalls: number;
  successCalls: number;
  failedCalls: number;
  rateLimitedCalls: number;
  avgDurationMs: number;
  minGapMs: number;
  overlappingCalls: number;
  configuredIntervalMs: number;
}

class ApiStatisticsCollector {
  private static instance: ApiStatisticsCollector;
  private endpointStats: Map<string, ApiEndpointStats> = new Map();
  private sessions: ApiStatsSession[] = [];
  private currentSessionId: string | null = null;
  private maxSessions = 100; // Keep last 100 sessions
  
  static getInstance(): ApiStatisticsCollector {
    if (!ApiStatisticsCollector.instance) {
      ApiStatisticsCollector.instance = new ApiStatisticsCollector();
    }
    return ApiStatisticsCollector.instance;
  }
  
  /**
   * Start a new statistics session
   */
  startSession(configuredIntervalMs: number): string {
    const sessionId = `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    this.currentSessionId = sessionId;
    
    this.sessions.push({
      sessionId,
      startTime: new Date(),
      totalCalls: 0,
      successCalls: 0,
      failedCalls: 0,
      rateLimitedCalls: 0,
      avgDurationMs: 0,
      minGapMs: Infinity,
      overlappingCalls: 0,
      configuredIntervalMs,
    });
    
    // Trim old sessions
    if (this.sessions.length > this.maxSessions) {
      this.sessions = this.sessions.slice(-this.maxSessions);
    }
    
    return sessionId;
  }
  
  /**
   * End current session with final stats
   */
  endSession(summary: ReturnType<typeof apiTimingTracker.getSummary>, overlappingCalls: number): void {
    if (!this.currentSessionId) return;
    
    const session = this.sessions.find(s => s.sessionId === this.currentSessionId);
    if (session) {
      session.endTime = new Date();
      session.totalCalls = summary.totalCalls;
      session.successCalls = summary.successCalls;
      session.failedCalls = summary.failedCalls;
      session.rateLimitedCalls = summary.rateLimitedCalls;
      session.avgDurationMs = summary.avgDuration;
      session.minGapMs = summary.minGap;
      session.overlappingCalls = overlappingCalls;
    }
    
    this.currentSessionId = null;
  }
  
  /**
   * Record a completed API call
   */
  recordCall(
    method: string,
    endpoint: string,
    durationMs: number,
    gapMs: number | undefined,
    status: 'success' | 'failed' | 'rate_limited'
  ): void {
    // Normalize endpoint (remove IDs and hashes)
    const normalizedEndpoint = this.normalizeEndpoint(endpoint);
    const key = `${method}:${normalizedEndpoint}`;
    
    let stats = this.endpointStats.get(key);
    if (!stats) {
      stats = {
        endpoint: normalizedEndpoint,
        method,
        totalCalls: 0,
        successCalls: 0,
        failedCalls: 0,
        rateLimitedCalls: 0,
        totalDurationMs: 0,
        minDurationMs: Infinity,
        maxDurationMs: 0,
        avgDurationMs: 0,
        totalGapMs: 0,
        minGapMs: Infinity,
        maxGapMs: 0,
        avgGapMs: 0,
        gapsUnder200ms: 0,
        gapsUnder100ms: 0,
      };
      this.endpointStats.set(key, stats);
    }
    
    // Update counts
    stats.totalCalls++;
    if (status === 'success') stats.successCalls++;
    else if (status === 'failed') stats.failedCalls++;
    else if (status === 'rate_limited') stats.rateLimitedCalls++;
    
    // Update duration stats
    stats.totalDurationMs += durationMs;
    stats.minDurationMs = Math.min(stats.minDurationMs, durationMs);
    stats.maxDurationMs = Math.max(stats.maxDurationMs, durationMs);
    stats.avgDurationMs = Math.round(stats.totalDurationMs / stats.totalCalls);
    
    // Update gap stats
    if (gapMs !== undefined) {
      stats.totalGapMs += gapMs;
      stats.minGapMs = Math.min(stats.minGapMs, gapMs);
      stats.maxGapMs = Math.max(stats.maxGapMs, gapMs);
      stats.avgGapMs = Math.round(stats.totalGapMs / stats.totalCalls);
      if (gapMs < 200) stats.gapsUnder200ms++;
      if (gapMs < 100) stats.gapsUnder100ms++;
    }
  }
  
  /**
   * Normalize endpoint to group similar calls
   */
  private normalizeEndpoint(endpoint: string): string {
    return endpoint
      .replace(/\/[a-f0-9-]{8,}/gi, '/{id}')  // UUIDs
      .replace(/\/\d{10,}/g, '/{id}')          // Long numeric IDs
      .replace(/\([^)]+\)/g, '')               // Remove parenthetical info like (switch to xxx)
      .trim();
  }
  
  /**
   * Get aggregated statistics
   */
  getStats(): {
    endpoints: ApiEndpointStats[];
    sessions: ApiStatsSession[];
    overall: {
      totalCalls: number;
      totalSessions: number;
      avgDurationMs: number;
      avgMinGapMs: number;
      rateLimitedTotal: number;
      successRate: number;
      recommendedIntervalMs: number;
    };
  } {
    const endpoints = Array.from(this.endpointStats.values())
      .sort((a, b) => b.totalCalls - a.totalCalls);
    
    // Calculate overall stats
    const totalCalls = endpoints.reduce((sum, e) => sum + e.totalCalls, 0);
    const totalDuration = endpoints.reduce((sum, e) => sum + e.totalDurationMs, 0);
    const avgDurationMs = totalCalls > 0 ? Math.round(totalDuration / totalCalls) : 0;
    
    const successTotal = endpoints.reduce((sum, e) => sum + e.successCalls, 0);
    const rateLimitedTotal = endpoints.reduce((sum, e) => sum + e.rateLimitedCalls, 0);
    const successRate = totalCalls > 0 ? Math.round((successTotal / totalCalls) * 100) : 100;
    
    // Calculate average min gap from sessions
    const sessionsWithGaps = this.sessions.filter(s => s.minGapMs !== Infinity && s.minGapMs > 0);
    const avgMinGapMs = sessionsWithGaps.length > 0 
      ? Math.round(sessionsWithGaps.reduce((sum, s) => sum + s.minGapMs, 0) / sessionsWithGaps.length)
      : 0;
    
    // Recommend interval based on data
    // If we had rate limits, increase. If all success with good gaps, can try lower.
    let recommendedIntervalMs = 200;
    if (rateLimitedTotal > 0) {
      recommendedIntervalMs = 250; // Got rate limited, increase
    } else if (totalCalls > 50 && avgMinGapMs > 150) {
      recommendedIntervalMs = 150; // Good success rate, can try lower
    } else if (totalCalls > 100 && avgMinGapMs > 120 && successRate === 100) {
      recommendedIntervalMs = 120; // Excellent success, try even lower
    }
    
    return {
      endpoints,
      sessions: this.sessions.slice(-20), // Last 20 sessions
      overall: {
        totalCalls,
        totalSessions: this.sessions.length,
        avgDurationMs,
        avgMinGapMs,
        rateLimitedTotal,
        successRate,
        recommendedIntervalMs,
      },
    };
  }
  
  /**
   * Reset all statistics
   */
  reset(): void {
    this.endpointStats.clear();
    this.sessions = [];
    this.currentSessionId = null;
    console.log('[API-Stats] Statistics reset');
  }
}

export const apiStatsCollector = ApiStatisticsCollector.getInstance();
