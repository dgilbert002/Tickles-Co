/**
 * Candle Data Service
 * 
 * Manages continuous collection of candle data from Capital.com via WebSocket.
 * Uses a unified 'candles' table for all epics, sources, and timeframes.
 * 
 * Features:
 * 1. On startup: Audit for gaps, fill from API, start WebSocket streams
 * 2. Continuous: WebSocket streams write to database (except quiet period)
 * 3. Pre-trade: Stop DB writes 10 min before window, keep data in memory
 * 4. Brain calc: Store fake_5min_close with the actual value used
 * 5. Logging: Comprehensive logging for troubleshooting
 */

import { getDb } from '../db';
import { candles, epics, marketInfo, accounts, savedStrategies } from '../../drizzle/schema';
import { sql, eq, and, desc, gte, lte, inArray, isNotNull } from 'drizzle-orm';
import { CapitalComAPI } from '../live_trading/capital_api';
import { loadCapitalCredentials } from '../live_trading/credentials';
import { connectionManager } from '../orchestration/connection_manager';
import WebSocket from 'ws';
import { EventEmitter } from 'events';

// ============================================================================
// LOGGING
// ============================================================================

type LogLevel = 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';

interface LogEntry {
  timestamp: Date;
  level: LogLevel;
  component: string;
  message: string;
  data?: any;
}

class CandleLogger {
  private logs: LogEntry[] = [];
  private maxLogs: number = 10000;
  
  log(level: LogLevel, component: string, message: string, data?: any): void {
    const entry: LogEntry = {
      timestamp: new Date(),
      level,
      component,
      message,
      data
    };
    
    this.logs.push(entry);
    if (this.logs.length > this.maxLogs) {
      this.logs.shift();
    }
    
    // Console output with color coding
    const prefix = `[${entry.timestamp.toISOString()}] [${level}] [${component}]`;
    const fullMessage = data ? `${message} ${JSON.stringify(data)}` : message;
    
    switch (level) {
      case 'ERROR':
        console.error(`\x1b[31m${prefix}\x1b[0m ${fullMessage}`);
        break;
      case 'WARN':
        console.warn(`\x1b[33m${prefix}\x1b[0m ${fullMessage}`);
        break;
      case 'INFO':
        console.log(`\x1b[36m${prefix}\x1b[0m ${fullMessage}`);
        break;
      case 'DEBUG':
        console.log(`\x1b[90m${prefix}\x1b[0m ${fullMessage}`);
        break;
    }
  }
  
  debug(component: string, message: string, data?: any): void {
    this.log('DEBUG', component, message, data);
  }
  
  info(component: string, message: string, data?: any): void {
    this.log('INFO', component, message, data);
  }
  
  warn(component: string, message: string, data?: any): void {
    this.log('WARN', component, message, data);
  }
  
  error(component: string, message: string, data?: any): void {
    this.log('ERROR', component, message, data);
  }
  
  getRecentLogs(count: number = 100): LogEntry[] {
    return this.logs.slice(-count);
  }
}

export const candleLogger = new CandleLogger();

// ============================================================================
// WEBSOCKET CLIENT
// ============================================================================

interface WebSocketCandle {
  epic: string;
  resolution: 'MINUTE' | 'MINUTE_5';
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  priceType: 'bid' | 'ask';
}

interface CandlePair {
  bid: WebSocketCandle | null;
  ask: WebSocketCandle | null;
  receivedAt: Date;
}

class CandleWebSocket extends EventEmitter {
  private ws: WebSocket | null = null;
  private cst: string = '';
  private securityToken: string = '';
  private isConnected: boolean = false;
  private reconnectAttempts: number = 0;
  private pingInterval: NodeJS.Timeout | null = null;
  private subscribedEpics: Set<string> = new Set();
  
  // Rapid disconnect detection & exponential backoff
  private lastDisconnectTime: number = 0;
  private rapidDisconnectCount: number = 0;  // Count of disconnects within cooldown window
  private reconnectCooldownMs: number = 5000;  // Base cooldown (5s)
  private maxCooldownMs: number = 300000;  // Max cooldown (5 minutes)
  private inCooldown: boolean = false;
  
  // Store latest candles: key = "EPIC:RESOLUTION"
  private latestCandles: Map<string, CandlePair> = new Map();
  
  // Store 1-min history for fake 5-min building: key = "EPIC"
  private minuteHistory: Map<string, WebSocketCandle[]> = new Map();
  
  // Pending candle waiters: key = "EPIC:RESOLUTION:TIMESTAMP"
  private pendingWaiters: Map<string, { resolve: (candle: CandlePair | null) => void; timeout: NodeJS.Timeout }> = new Map();
  
  private wsUrl: string = 'wss://api-streaming-capital.backend-capital.com/connect';
  
  async connect(cst: string, securityToken: string): Promise<boolean> {
    this.cst = cst;
    this.securityToken = securityToken;
    
    return new Promise((resolve) => {
      try {
        candleLogger.info('WebSocket', 'Connecting to Capital.com WebSocket...', { url: this.wsUrl });
        
        this.ws = new WebSocket(this.wsUrl);
        
        this.ws.on('open', () => {
          candleLogger.info('WebSocket', 'Connected successfully');
          this.isConnected = true;
          this.reconnectAttempts = 0;
          this.inCooldown = false;
          // Don't reset rapidDisconnectCount on connect - only reset after stable connection (see below)
          this.startPingInterval();
          this.emit('connected');
          resolve(true);
          
          // Reset rapid disconnect count after 60 seconds of stable connection
          setTimeout(() => {
            if (this.isConnected) {
              this.rapidDisconnectCount = 0;
              this.reconnectCooldownMs = 5000; // Reset to base cooldown
              candleLogger.debug('WebSocket', 'Connection stable - reset cooldown counters');
            }
          }, 60000);
        });
        
        this.ws.on('message', (data: Buffer) => {
          this.handleMessage(data.toString());
        });
        
        this.ws.on('close', (code, reason) => {
          const now = Date.now();
          const timeSinceLastDisconnect = now - this.lastDisconnectTime;
          this.lastDisconnectTime = now;
          
          // Detect rapid disconnections (within 60 seconds of last disconnect)
          if (timeSinceLastDisconnect < 60000 && timeSinceLastDisconnect > 0) {
            this.rapidDisconnectCount++;
            // Increase cooldown exponentially (5s, 10s, 20s, 40s, 80s, up to 5min)
            this.reconnectCooldownMs = Math.min(
              this.reconnectCooldownMs * 2,
              this.maxCooldownMs
            );
            candleLogger.warn('WebSocket', 'Rapid disconnect detected', { 
              code, 
              reason: reason.toString(),
              rapidDisconnectCount: this.rapidDisconnectCount,
              nextCooldownMs: this.reconnectCooldownMs
            });
          } else {
            candleLogger.warn('WebSocket', 'Connection closed', { code, reason: reason.toString() });
          }
          
          this.isConnected = false;
          this.stopPingInterval();
          this.emit('disconnected');
          this.attemptReconnect();
        });
        
        this.ws.on('error', (error) => {
          candleLogger.error('WebSocket', 'Connection error', { error: error.message });
          this.emit('error', error);
        });
        
        // Timeout for initial connection
        setTimeout(() => {
          if (!this.isConnected) {
            candleLogger.error('WebSocket', 'Connection timeout');
            resolve(false);
          }
        }, 10000);
        
      } catch (error: any) {
        candleLogger.error('WebSocket', 'Failed to connect', { error: error.message });
        resolve(false);
      }
    });
  }
  
  subscribeToEpics(epicList: string[]): void {
    // Check WebSocket is fully connected (readyState === OPEN)
    if (!this.isConnected || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
      candleLogger.warn('WebSocket', 'Cannot subscribe - not connected or not ready', { 
        isConnected: this.isConnected, 
        hasWs: !!this.ws, 
        readyState: this.ws?.readyState 
      });
      return;
    }
    
    const newEpics = epicList.filter(e => !this.subscribedEpics.has(e));
    if (newEpics.length === 0) {
      candleLogger.debug('WebSocket', 'All epics already subscribed');
      return;
    }
    
    const message = {
      destination: 'OHLCMarketData.subscribe',
      correlationId: `sub_${Date.now()}`,
      cst: this.cst,
      securityToken: this.securityToken,
      payload: {
        epics: newEpics,
        resolutions: ['MINUTE', 'MINUTE_5'],
        type: 'classic'
      }
    };
    
    candleLogger.info('WebSocket', `Subscribing to ${newEpics.length} epics`, { epics: newEpics });
    candleLogger.debug('WebSocket', 'Subscription request', { message });
    
    try {
      this.ws.send(JSON.stringify(message));
      newEpics.forEach(epic => this.subscribedEpics.add(epic));
    } catch (error: any) {
      candleLogger.error('WebSocket', 'Failed to send subscription', { error: error.message });
    }
  }
  
  /**
   * Unsubscribe from specific epics (e.g., when market closes)
   */
  unsubscribeFromEpics(epicList: string[]): void {
    if (!this.isConnected || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
      candleLogger.warn('WebSocket', 'Cannot unsubscribe - not connected or not ready');
      return;
    }
    
    const epicsToRemove = epicList.filter(e => this.subscribedEpics.has(e));
    if (epicsToRemove.length === 0) {
      candleLogger.debug('WebSocket', 'No epics to unsubscribe');
      return;
    }
    
    const message = {
      destination: 'OHLCMarketData.unsubscribe',
      correlationId: `unsub_${Date.now()}`,
      cst: this.cst,
      securityToken: this.securityToken,
      payload: {
        epics: epicsToRemove,
        resolutions: ['MINUTE', 'MINUTE_5'],
      }
    };
    
    candleLogger.info('WebSocket', `📴 Unsubscribing from ${epicsToRemove.length} epics (market closed)`, { epics: epicsToRemove });
    
    try {
      this.ws.send(JSON.stringify(message));
      epicsToRemove.forEach(epic => this.subscribedEpics.delete(epic));
    } catch (error: any) {
      candleLogger.error('WebSocket', 'Failed to send unsubscription', { error: error.message });
    }
  }
  
  private handleMessage(data: string): void {
    try {
      const message = JSON.parse(data);
      
      candleLogger.debug('WebSocket', 'Message received', { 
        destination: message.destination,
        status: message.status 
      });
      
      if (message.destination === 'ohlc.event') {
        const payload = message.payload;
        const candle: WebSocketCandle = {
          epic: payload.epic,
          resolution: payload.resolution,
          timestamp: payload.t,
          open: payload.o,
          high: payload.h,
          low: payload.l,
          close: payload.c,
          priceType: payload.priceType
        };
        
        candleLogger.debug('WebSocket', 'Candle received', {
          epic: candle.epic,
          resolution: candle.resolution,
          timestamp: new Date(candle.timestamp).toISOString(),
          close: candle.close,
          priceType: candle.priceType
        });
        
        this.storeCandle(candle);
        this.checkPendingWaiters(candle);
        this.emit('candle', candle);
        
      } else if (message.destination === 'OHLCMarketData.subscribe') {
        if (message.status === 'ERROR') {
          candleLogger.error('WebSocket', '❌ Subscription FAILED', { 
            errorCode: message.errorCode,
            error: message.payload?.error || message.payload,
          });
        } else {
          candleLogger.info('WebSocket', '✅ Subscription confirmed', { subscriptions: message.payload?.subscriptions });
        }
      } else if (message.destination === 'OHLCMarketData.unsubscribe') {
        if (message.status === 'ERROR') {
          // Downgrade "invalid session token" to warn - harmless during market close cleanup
          const errorCode = message.payload?.error?.errorCode || message.errorCode;
          if (errorCode === 'error.invalid.session.token') {
            candleLogger.warn('WebSocket', '⚠️ Unsubscription skipped (session expired - harmless cleanup)', { 
              errorCode,
            });
          } else {
            candleLogger.error('WebSocket', '❌ Unsubscription FAILED', { 
              errorCode: message.errorCode,
              error: message.payload?.error || message.payload,
            });
          }
        } else {
          candleLogger.info('WebSocket', '📴 Unsubscription confirmed', { unsubscribed: message.payload?.epics });
        }
      }
      
    } catch (error: any) {
      candleLogger.error('WebSocket', 'Failed to parse message', { error: error.message, data: data.substring(0, 200) });
    }
  }
  
  private storeCandle(candle: WebSocketCandle): void {
    const key = `${candle.epic}:${candle.resolution}`;
    
    let pair = this.latestCandles.get(key);
    if (!pair) {
      pair = { bid: null, ask: null, receivedAt: new Date() };
      this.latestCandles.set(key, pair);
    }
    
    if (candle.priceType === 'bid') {
      pair.bid = candle;
    } else {
      pair.ask = candle;
    }
    pair.receivedAt = new Date();
    
    // CRITICAL: Persist to database when we have both bid AND ask
    // This ensures the database stays up-to-date with WebSocket data
    if (pair.bid && pair.ask && pair.bid.timestamp === pair.ask.timestamp) {
      this.persistCandleToDatabase(pair.bid, pair.ask).catch(err => {
        candleLogger.error('WebSocket', `Failed to persist candle to DB: ${err.message}`);
      });
    }
    
    // Store 1-min history
    if (candle.resolution === 'MINUTE' && candle.priceType === 'bid') {
      let history = this.minuteHistory.get(candle.epic);
      if (!history) {
        history = [];
        this.minuteHistory.set(candle.epic, history);
      }
      
      const lastCandle = history[history.length - 1];
      if (!lastCandle || lastCandle.timestamp !== candle.timestamp) {
        history.push(candle);
        if (history.length > 10) {
          history.shift();
        }
      } else {
        history[history.length - 1] = candle;
      }
    }
  }
  
  /**
   * Persist a WebSocket candle pair (bid + ask) to the database
   * Uses ON DUPLICATE KEY UPDATE to handle existing candles (updates close prices)
   */
  private async persistCandleToDatabase(bidCandle: WebSocketCandle, askCandle: WebSocketCandle): Promise<void> {
    const db = await getDb();
    if (!db) return;
    
    const timeframe = bidCandle.resolution === 'MINUTE' ? '1m' : '5m';
    const timestamp = new Date(bidCandle.timestamp);
    
    // Note: The connection pool now uses timezone: '+00:00' which ensures
    // all timestamps are interpreted as UTC
    const utcTimestamp = timestamp.toISOString().slice(0, 19).replace('T', ' ');
    
    try {
      await db.execute(
        sql.raw(`
          INSERT INTO candles (epic, source, timeframe, timestamp, open_bid, high_bid, low_bid, close_bid, open_ask, high_ask, low_ask, close_ask, volume, data_source_type)
          VALUES (
            '${bidCandle.epic}',
            'capital',
            '${timeframe}',
            '${utcTimestamp}',
            ${bidCandle.close},
            ${bidCandle.close},
            ${bidCandle.close},
            ${bidCandle.close},
            ${askCandle.close},
            ${askCandle.close},
            ${askCandle.close},
            ${askCandle.close},
            0,
            'websocket'
          )
          ON DUPLICATE KEY UPDATE
            close_bid = ${bidCandle.close},
            close_ask = ${askCandle.close},
            high_bid = GREATEST(high_bid, ${bidCandle.close}),
            high_ask = GREATEST(high_ask, ${askCandle.close}),
            low_bid = LEAST(low_bid, ${bidCandle.close}),
            low_ask = LEAST(low_ask, ${askCandle.close}),
            data_source_type = 'websocket'
        `)
      );
      
      // Track which epics have been persisted since last log
      this.epicsPersistSinceLastLog.add(bidCandle.epic);
      
      // Log occasionally (not every candle to reduce noise) - show ALL epics persisted
      const now = Date.now();
      if (!this.lastDbPersistLog || now - this.lastDbPersistLog > 60000) {
        const epicsList = Array.from(this.epicsPersistSinceLastLog).sort();
        candleLogger.info('WebSocket', `Persisted ${epicsList.length} ${timeframe} candles to DB: ${epicsList.join(', ')}`);
        this.lastDbPersistLog = now;
        this.epicsPersistSinceLastLog.clear();
      }
    } catch (error: any) {
      // Don't log every duplicate key error, just unexpected ones
      if (!error.message?.includes('Duplicate entry')) {
        candleLogger.error('WebSocket', `DB persist error for ${bidCandle.epic}: ${error.message}`);
      }
    }
  }
  
  private lastDbPersistLog: number = 0;
  private epicsPersistSinceLastLog: Set<string> = new Set();
  
  private checkPendingWaiters(candle: WebSocketCandle): void {
    // Check if anyone is waiting for this candle
    const key = `${candle.epic}:${candle.resolution}:${candle.timestamp}`;
    const waiter = this.pendingWaiters.get(key);
    
    if (waiter) {
      const pair = this.latestCandles.get(`${candle.epic}:${candle.resolution}`);
      if (pair && pair.bid && pair.ask) {
        clearTimeout(waiter.timeout);
        waiter.resolve(pair);
        this.pendingWaiters.delete(key);
        candleLogger.debug('WebSocket', 'Waiter resolved', { key });
      }
    }
  }
  
  /**
   * Wait for a specific candle to arrive (with timeout)
   */
  async waitForCandle(epic: string, resolution: 'MINUTE' | 'MINUTE_5', expectedTimestamp: number, timeoutMs: number = 3000): Promise<CandlePair | null> {
    const key = `${epic}:${resolution}:${expectedTimestamp}`;
    
    // Check if we already have it
    const existing = this.latestCandles.get(`${epic}:${resolution}`);
    if (existing && existing.bid && existing.bid.timestamp === expectedTimestamp) {
      candleLogger.debug('WebSocket', 'Candle already available', { key });
      return existing;
    }
    
    // Wait for it
    return new Promise((resolve) => {
      const timeout = setTimeout(() => {
        this.pendingWaiters.delete(key);
        candleLogger.warn('WebSocket', 'Candle wait timeout', { key, timeoutMs });
        resolve(null);
      }, timeoutMs);
      
      this.pendingWaiters.set(key, { resolve, timeout });
      candleLogger.debug('WebSocket', 'Waiting for candle', { key, timeoutMs });
    });
  }
  
  /**
   * Get latest candle pair for an epic/resolution
   */
  getLatestCandle(epic: string, resolution: 'MINUTE' | 'MINUTE_5'): CandlePair | null {
    return this.latestCandles.get(`${epic}:${resolution}`) || null;
  }
  
  /**
   * Get 1-min history for building fake 5-min candle
   */
  get1MinHistory(epic: string, count: number = 5): WebSocketCandle[] {
    const history = this.minuteHistory.get(epic) || [];
    return history.slice(-count);
  }
  
  private startPingInterval(): void {
    this.pingInterval = setInterval(() => {
      // Only ping if WebSocket is fully connected (readyState === OPEN)
      if (this.ws && this.isConnected && this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.ping();
        } catch (error) {
          candleLogger.warn('WebSocket', 'Ping failed - socket not ready');
        }
      }
    }, 30000);
  }
  
  private stopPingInterval(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }
  
  private attemptReconnect(): void {
    // If already in cooldown, skip this reconnect attempt
    if (this.inCooldown) {
      candleLogger.debug('WebSocket', 'Skipping reconnect - already in cooldown');
      return;
    }
    
    // After too many rapid disconnects, enter extended cooldown
    if (this.rapidDisconnectCount >= 10) {
      candleLogger.error('WebSocket', 'Too many rapid disconnects - entering 5 minute extended cooldown', {
        rapidDisconnectCount: this.rapidDisconnectCount
      });
      this.inCooldown = true;
      setTimeout(() => {
        this.inCooldown = false;
        this.rapidDisconnectCount = 0;
        this.reconnectCooldownMs = 5000;
        this.reconnectAttempts = 0;
        candleLogger.info('WebSocket', 'Extended cooldown complete - ready to reconnect');
        this.attemptReconnect();
      }, this.maxCooldownMs);
      return;
    }
    
    if (this.reconnectAttempts >= 5) {
      candleLogger.error('WebSocket', 'Max reconnection attempts reached - waiting before retry', {
        cooldownMs: this.reconnectCooldownMs
      });
      this.inCooldown = true;
      setTimeout(() => {
        this.inCooldown = false;
        this.reconnectAttempts = 0;
        candleLogger.info('WebSocket', 'Cooldown complete - retrying connection');
        this.attemptReconnect();
      }, this.reconnectCooldownMs);
      return;
    }
    
    this.reconnectAttempts++;
    this.inCooldown = true;
    candleLogger.info('WebSocket', `Reconnecting (attempt ${this.reconnectAttempts}/5) after ${this.reconnectCooldownMs}ms cooldown...`);
    
    setTimeout(async () => {
      this.inCooldown = false;
      
      // Get FRESH tokens before reconnecting (old tokens may have expired)
      try {
        const credentials = await loadCapitalCredentials();
        if (credentials) {
          const client = new CapitalComAPI(credentials);
          await client.authenticate();
          const tokens = client.getAuthTokens();
          
          if (tokens) {
            // Update stored tokens with fresh ones
            this.cst = tokens.cst;
            this.securityToken = tokens.securityToken;
            candleLogger.info('WebSocket', '🔑 Refreshed auth tokens for reconnect');
          } else {
            candleLogger.warn('WebSocket', 'Failed to get fresh tokens, using existing');
          }
        }
      } catch (tokenError: any) {
        candleLogger.warn('WebSocket', 'Token refresh failed, using existing tokens', { 
          error: tokenError.message 
        });
      }
      
      const success = await this.connect(this.cst, this.securityToken);
      if (success && this.subscribedEpics.size > 0) {
        const epicList = Array.from(this.subscribedEpics);
        this.subscribedEpics.clear();
        // Small delay after connection to ensure socket is fully ready
        await new Promise(resolve => setTimeout(resolve, 200));
        this.subscribeToEpics(epicList);
      }
    }, this.reconnectCooldownMs);
  }
  
  disconnect(): void {
    this.stopPingInterval();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.isConnected = false;
    this.subscribedEpics.clear();
    this.latestCandles.clear();
    this.minuteHistory.clear();
    candleLogger.info('WebSocket', 'Disconnected');
  }
  
  /**
   * Update stored auth tokens (call this when REST API refreshes tokens via keep-alive)
   * Prevents "invalid session token" errors during subscribe/unsubscribe
   */
  updateAuthTokens(cst: string, securityToken: string): void {
    if (cst && securityToken) {
      this.cst = cst;
      this.securityToken = securityToken;
      candleLogger.debug('WebSocket', '🔑 Auth tokens updated from REST API refresh');
    }
  }
  
  get connected(): boolean {
    return this.isConnected;
  }
  
  getSubscribedEpics(): string[] {
    return Array.from(this.subscribedEpics);
  }
}

export const candleWebSocket = new CandleWebSocket();

// ============================================================================
// PROGRESS TRACKING
// ============================================================================

export interface RefreshProgress {
  epic: string;
  phase: 'starting' | 'fetching_5m' | 'fetching_1m' | 'complete' | 'error';
  fiveMin: {
    progress: number;        // 0-100
    currentChunk: number;
    totalChunks: number;
    candlesFetched: number;
    dateRange?: { start: string; end: string };
  };
  oneMin: {
    progress: number;        // 0-100
    currentChunk: number;
    totalChunks: number;
    candlesFetched: number;
    dateRange?: { start: string; end: string };
  };
  startTime: number;
  elapsedMs: number;
  message: string;
  error?: string;
}

// Progress listeners (for real-time updates)
type ProgressListener = (progress: RefreshProgress) => void;
const progressListeners: Map<string, Set<ProgressListener>> = new Map();

export function subscribeToRefreshProgress(epic: string, listener: ProgressListener): () => void {
  let listeners = progressListeners.get(epic);
  if (!listeners) {
    listeners = new Set();
    progressListeners.set(epic, listeners);
  }
  listeners.add(listener);
  
  // Return unsubscribe function
  return () => {
    listeners?.delete(listener);
    if (listeners?.size === 0) {
      progressListeners.delete(epic);
    }
  };
}

function emitProgress(progress: RefreshProgress): void {
  const listeners = progressListeners.get(progress.epic);
  if (listeners) {
    listeners.forEach(listener => listener(progress));
  }
  // Also emit to a global listener for "all" epics
  const globalListeners = progressListeners.get('*');
  if (globalListeners) {
    globalListeners.forEach(listener => listener(progress));
  }
}

// ============================================================================
// DATA SERVICE
// ============================================================================

class CandleDataService {
  private isRunning: boolean = false;
  private inQuietPeriod: boolean = false;
  private auditInterval: NodeJS.Timeout | null = null;
  private dbWriteInterval: NodeJS.Timeout | null = null;
  
  // Buffer for candles during quiet period
  private candleBuffer: Map<string, { candle: any; receivedAt: Date }[]> = new Map();
  
  // Current refresh progress per epic
  private refreshProgress: Map<string, RefreshProgress> = new Map();
  
  // Lock to prevent duplicate refresh operations
  private refreshInProgress: Set<string> = new Set();
  
  // Track last candle received time for health monitoring
  private lastCandleReceivedAt: Date | null = null;
  
  /**
   * Initialize on server startup
   */
  async initialize(): Promise<void> {
    candleLogger.info('DataService', '='.repeat(60));
    candleLogger.info('DataService', 'Initializing Candle Data Service...');
    
    try {
      // 1. Ensure candles table exists
      await this.ensureTableExists();
      
      // 2. Get all epics
      // Get epics ONLY from strategies assigned to active accounts (more efficient)
      const epicList = await this.getActiveStrategyEpics();
      candleLogger.info('DataService', `Found ${epicList.length} epics from active strategies`, { epics: epicList });
      
      if (epicList.length === 0) {
        candleLogger.warn('DataService', 'No epics found, skipping initialization');
        return;
      }
      
      // 3. Audit and fill gaps
      await this.auditAndFillGaps(epicList);
      
      // 4. Start WebSocket
      await this.startWebSocket(epicList);
      
      // 5. Start periodic audit (hourly)
      this.startPeriodicAudit();
      
      // 6. Start DB write interval (every 30s)
      this.startDbWriteInterval();
      
      this.isRunning = true;
      candleLogger.info('DataService', 'Initialization complete');
      candleLogger.info('DataService', '='.repeat(60));
      
    } catch (error: any) {
      candleLogger.error('DataService', 'Initialization failed', { error: error.message });
    }
  }
  
  /**
   * Public method to fetch historical data for a specific epic
   * Called by the Data Manager when refreshing epic data
   */
  async fetchHistoricalDataForEpic(epic: string): Promise<{ success: boolean; message: string }> {
    // Check if refresh is already in progress for this epic
    if (this.refreshInProgress.has(epic)) {
      candleLogger.warn('DataService', `Refresh already in progress for ${epic}, skipping duplicate request`);
      return { success: true, message: `Refresh already in progress for ${epic}` };
    }
    
    // Acquire lock
    this.refreshInProgress.add(epic);
    candleLogger.info('DataService', `Fetching historical data for ${epic}...`);
    
    const startTime = Date.now();
    
    // Initialize progress tracking
    const progress: RefreshProgress = {
      epic,
      phase: 'starting',
      fiveMin: { progress: 0, currentChunk: 0, totalChunks: 0, candlesFetched: 0 },
      oneMin: { progress: 0, currentChunk: 0, totalChunks: 0, candlesFetched: 0 },
      startTime,
      elapsedMs: 0,
      message: 'Initializing...',
    };
    this.refreshProgress.set(epic, progress);
    emitProgress(progress);
    
    try {
      // Ensure table exists
      await this.ensureTableExists();
      
      // Fetch 5-minute data
      progress.phase = 'fetching_5m';
      progress.message = 'Fetching 5-minute candles...';
      progress.elapsedMs = Date.now() - startTime;
      emitProgress(progress);
      
      await this.fillGapWithProgress(epic, '5m', progress);
      
      // Fetch 1-minute data (for fake candle support)
      progress.phase = 'fetching_1m';
      progress.message = 'Fetching 1-minute candles...';
      progress.elapsedMs = Date.now() - startTime;
      emitProgress(progress);
      
      await this.fillGapWithProgress(epic, '1m', progress);
      
      // Complete
      progress.phase = 'complete';
      progress.message = 'Data refresh complete!';
      progress.elapsedMs = Date.now() - startTime;
      emitProgress(progress);
      
      candleLogger.info('DataService', `Historical data fetch complete for ${epic}`);
      
      // Update epic stats in database after refresh
      try {
        const stats = await this.getDataRange(epic, '5m');
        const statsDb = await getDb();
        if (stats && statsDb) {
          const startDateStr = stats.earliest ? stats.earliest.toISOString().slice(0, 10) : null;
          const endDateStr = stats.latest ? stats.latest.toISOString().slice(0, 10) : null;
          await statsDb.update(epics)
            .set({
              startDate: startDateStr,
              endDate: endDateStr,
              candleCount: stats.count || 0,
            })
            .where(eq(epics.symbol, epic));
          candleLogger.info('DataService', `Updated ${epic} stats: ${startDateStr} → ${endDateStr}, ${stats.count} candles`);
        }
      } catch (statsError: any) {
        candleLogger.warn('DataService', `Failed to update stats for ${epic}`, { error: statsError.message });
      }
      
      return { 
        success: true, 
        message: `Fetched ${progress.fiveMin.candlesFetched} 5m and ${progress.oneMin.candlesFetched} 1m candles for ${epic}` 
      };
      
    } catch (error: any) {
      progress.phase = 'error';
      progress.error = error.message;
      progress.message = `Error: ${error.message}`;
      progress.elapsedMs = Date.now() - startTime;
      emitProgress(progress);
      
      candleLogger.error('DataService', `Failed to fetch historical data for ${epic}`, { error: error.message });
      return { success: false, message: error.message };
    } finally {
      // Release lock
      this.refreshInProgress.delete(epic);
      this.refreshProgress.delete(epic);
    }
  }
  
  /**
   * Check if refresh is in progress for an epic
   */
  isRefreshInProgress(epic: string): boolean {
    return this.refreshInProgress.has(epic);
  }
  
  /**
   * Get current refresh progress for an epic
   */
  getRefreshProgress(epic: string): RefreshProgress | undefined {
    return this.refreshProgress.get(epic);
  }
  
  private async ensureTableExists(): Promise<void> {
    const db = await getDb();
    if (!db) return;
    
    try {
      // Check if table exists by querying it
      await db.execute(sql`SELECT 1 FROM candles LIMIT 1`);
      candleLogger.info('DataService', 'Candles table exists');
    } catch (e: any) {
      if (e.message.includes("doesn't exist")) {
        candleLogger.info('DataService', 'Creating candles table...');
        // Table will be created by Drizzle migration
        // For now, create it manually
        await db.execute(sql`
          CREATE TABLE IF NOT EXISTS candles (
            id INT AUTO_INCREMENT PRIMARY KEY,
            epic VARCHAR(20) NOT NULL,
            source ENUM('av', 'capital') NOT NULL DEFAULT 'capital',
            timeframe ENUM('1m', '5m', '15m', '1h', '4h', '1d') NOT NULL DEFAULT '5m',
            timestamp DATETIME NOT NULL,
            open_bid DECIMAL(20,8) NOT NULL,
            high_bid DECIMAL(20,8) NOT NULL,
            low_bid DECIMAL(20,8) NOT NULL,
            close_bid DECIMAL(20,8) NOT NULL,
            open_ask DECIMAL(20,8) NOT NULL,
            high_ask DECIMAL(20,8) NOT NULL,
            low_ask DECIMAL(20,8) NOT NULL,
            close_ask DECIMAL(20,8) NOT NULL,
            volume INT DEFAULT 0,
            fake_5min_close DECIMAL(20,8) NULL,
            fake_5min_source_timestamp DATETIME NULL,
            fake_5min_comment VARCHAR(500) NULL,
            fake_5min_calculated_at DATETIME NULL,
            data_source_type ENUM('api', 'websocket', 'backfill') DEFAULT 'api',
            received_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_candle (epic, source, timeframe, timestamp),
            INDEX idx_epic_timeframe (epic, timeframe),
            INDEX idx_timestamp (timestamp)
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        `);
        candleLogger.info('DataService', 'Candles table created');
      }
    }
  }
  
  /**
   * Get epics ONLY from strategies assigned to ACTIVE accounts
   * This is more efficient than subscribing to ALL epics
   */
  private async getActiveStrategyEpics(): Promise<string[]> {
    const db = await getDb();
    if (!db) return [];
    
    try {
      // Get all active accounts with strategies
      const activeAccounts = await db
        .select({
          assignedStrategyId: accounts.assignedStrategyId,
        })
        .from(accounts)
        .where(and(
          eq(accounts.isActive, true),
          isNotNull(accounts.assignedStrategyId)
        ));
      
      if (activeAccounts.length === 0) {
        candleLogger.info('DataService', 'No active accounts with strategies - falling back to all epics');
        return this.getAllEpics();
      }
      
      // Get unique strategy IDs
      const strategyIds = [...new Set(activeAccounts.map(a => a.assignedStrategyId).filter(Boolean))] as number[];
      
      // Get strategies with their DNA strands
      const strategies = await db
        .select({
          id: savedStrategies.id,
          dnaStrands: savedStrategies.dnaStrands,
        })
        .from(savedStrategies)
        .where(inArray(savedStrategies.id, strategyIds));
      
      // Extract unique epics from all DNA strands
      const epicsSet = new Set<string>();
      
      for (const strategy of strategies) {
        const dnaStrands = strategy.dnaStrands as any[];
        if (Array.isArray(dnaStrands)) {
          for (const dna of dnaStrands) {
            if (dna.epic) {
              epicsSet.add(dna.epic);
            }
          }
        }
      }
      
      const activeEpics = Array.from(epicsSet);
      
      candleLogger.info('DataService', `Found ${activeEpics.length} epics from ${activeAccounts.length} active accounts`, {
        epics: activeEpics,
        accountCount: activeAccounts.length,
        strategyCount: strategies.length,
      });
      
      return activeEpics;
      
    } catch (error: any) {
      candleLogger.error('DataService', 'Failed to get active strategy epics, falling back to all', { error: error.message });
      return this.getAllEpics();
    }
  }
  
  /**
   * FALLBACK: Get ALL epics from database (used when no active accounts)
   */
  private async getAllEpics(): Promise<string[]> {
    const db = await getDb();
    if (!db) return [];
    
    try {
      const epicRows = await db.select({ epic: epics.symbol }).from(epics);
      if (epicRows.length > 0) {
        return epicRows.map(r => r.epic).filter(Boolean);
      }
    } catch (e) {}
    
    try {
      const marketRows = await db.select({ epic: marketInfo.epic }).from(marketInfo);
      return marketRows.map(r => r.epic).filter(Boolean);
    } catch (e) {
      return [];
    }
  }
  
  /**
   * Get the date range of existing data for an epic/timeframe
   */
  private async getDataRange(epic: string, timeframe: '1m' | '5m'): Promise<{ earliest: Date | null; latest: Date | null; count: number } | null> {
    const db = await getDb();
    if (!db) return null;
    
    try {
      const result = await db.execute(sql`
        SELECT MIN(timestamp) as earliest, MAX(timestamp) as latest, COUNT(*) as count
        FROM candles
        WHERE epic = ${epic} AND source = 'capital' AND timeframe = ${timeframe}
      `);
      const rows = (result as any)?.[0];
      if (rows?.[0] && rows[0].count > 0) {
        // CRITICAL: MySQL timestamps are stored in UTC but Date() interprets them as local time
        // We must force UTC interpretation by treating the timestamp components as UTC
        const parseAsUTC = (ts: any): Date | null => {
          if (!ts) return null;
          if (ts instanceof Date) {
            // MySQL returns Date objects interpreted as local time - convert to UTC
            return new Date(Date.UTC(
              ts.getFullYear(), ts.getMonth(), ts.getDate(),
              ts.getHours(), ts.getMinutes(), ts.getSeconds()
            ));
          }
          // String timestamp - append Z to force UTC
          const str = String(ts).replace(' ', 'T');
          return new Date(str.endsWith('Z') ? str : `${str}Z`);
        };
        
        return {
          earliest: parseAsUTC(rows[0].earliest),
          latest: parseAsUTC(rows[0].latest),
          count: Number(rows[0].count) || 0,
        };
      }
    } catch (e: any) {
      candleLogger.error('DataService', `Failed to get data range for ${epic} ${timeframe}`, { error: e.message });
    }
    return null;
  }
  
  /**
   * Migrate existing data from legacy tables to unified candles table
   * NOTE: Legacy tables have been deleted. This is now a no-op but kept for safety.
   */
  
  private async auditAndFillGaps(epicList: string[]): Promise<void> {
    candleLogger.info('DataService', 'Starting gap audit...');
    
    // Check if candles table has data
    const db = await getDb();
    if (db) {
      const countResult = await db.execute(sql`SELECT COUNT(*) as count FROM candles`);
      const existingCount = Number((countResult as any)?.[0]?.[0]?.count) || 0;
      if (existingCount === 0) {
        candleLogger.info('DataService', 'Candles table is empty, will fetch fresh data from API');
      } else {
        candleLogger.info('DataService', `Candles table has ${existingCount.toLocaleString()} rows`);
      }
    }
    
    // Get market status for all epics (skip CLOSED markets to save API calls)
    const epicStatuses = new Map<string, string>();
    if (db) {
      try {
        const statuses = await db
          .select({
            symbol: epics.symbol,
            marketStatus: epics.marketStatus,
            instrumentType: marketInfo.instrumentType,
          })
          .from(epics)
          .leftJoin(marketInfo, eq(epics.symbol, marketInfo.epic))
          .where(inArray(epics.symbol, epicList));
        
        statuses.forEach(s => {
          epicStatuses.set(s.symbol, s.marketStatus || 'UNKNOWN');
        });
      } catch (error: any) {
        candleLogger.warn('DataService', 'Failed to load market statuses, will process all epics', { error: error.message });
      }
    }
    
    let processedCount = 0;
    let skippedCount = 0;
    
    for (let i = 0; i < epicList.length; i++) {
      const epic = epicList[i];
      
      // Check market status - skip CLOSED markets (saves API calls on weekends/off-hours)
      const marketStatus = epicStatuses.get(epic);
      if (marketStatus === 'CLOSED') {
        candleLogger.debug('DataService', `⏭️ Skipping ${epic} - market CLOSED (no data available)`);
        skippedCount++;
        continue;
      }
      
      // Add delay between epics to avoid rate limiting (except for first)
      if (processedCount > 0) {
        candleLogger.info('DataService', `⏳ Waiting 3s before auditing ${epic} (rate limit protection)...`);
        await new Promise(resolve => setTimeout(resolve, 3000));
      }
      
      await this.auditEpic(epic, '5m');
      // Small delay between 5m and 1m for same epic
      await new Promise(resolve => setTimeout(resolve, 1000));
      await this.auditEpic(epic, '1m');
      
      processedCount++;
    }
    
    if (skippedCount > 0) {
      candleLogger.info('DataService', `Gap audit complete: ${processedCount} epics processed, ${skippedCount} skipped (markets closed)`);
    }
  }
  
  private async auditEpic(epic: string, timeframe: '1m' | '5m'): Promise<void> {
    const db = await getDb();
    if (!db) return;
    
    try {
      // Get date range
      const [result] = await db
        .select({
          earliest: sql<Date>`MIN(timestamp)`,
          latest: sql<Date>`MAX(timestamp)`,
          total: sql<number>`COUNT(*)`
        })
        .from(candles)
        .where(
          and(
            eq(candles.epic, epic),
            eq(candles.source, 'capital'),
            eq(candles.timeframe, timeframe)
          )
        );
      
      if (!result?.earliest || !result?.latest) {
        candleLogger.info('DataService', `No ${timeframe} data for ${epic}, fetching historical...`);
        await this.fetchHistoricalData(epic, timeframe);
        return;
      }
      
      // CRITICAL: MySQL timestamps are stored in UTC but Drizzle returns Date objects interpreted as local time
      // We must force UTC interpretation
      const parseAsUTC = (ts: any): Date | null => {
        if (!ts) return null;
        if (ts instanceof Date) {
          // Drizzle returns Date objects interpreted as local time - convert to UTC
          return new Date(Date.UTC(
            ts.getFullYear(), ts.getMonth(), ts.getDate(),
            ts.getHours(), ts.getMinutes(), ts.getSeconds()
          ));
        }
        // String timestamp - append Z to force UTC
        const str = String(ts).replace(' ', 'T');
        return new Date(str.endsWith('Z') ? str : `${str}Z`);
      };
      
      const earliestUTC = parseAsUTC(result.earliest);
      const latestUTC = parseAsUTC(result.latest);
      
      candleLogger.info('DataService', `${epic} ${timeframe}: ${result.total} candles`, {
        earliest: earliestUTC?.toISOString(),
        latest: latestUTC?.toISOString()
      });
      
      // Check if we need to fetch recent data
      const now = new Date();
      const latestDate = latestUTC || new Date(0);
      const hoursBehind = (now.getTime() - latestDate.getTime()) / (1000 * 60 * 60);
      
      if (hoursBehind > 1) {
        candleLogger.info('DataService', `${epic} ${timeframe} is ${hoursBehind.toFixed(1)}h behind, fetching new data...`);
        await this.fillGap(epic, timeframe, latestDate, now);
      }
      
    } catch (error: any) {
      candleLogger.error('DataService', `Audit failed for ${epic} ${timeframe}`, { error: error.message });
    }
  }
  
  private async fetchHistoricalData(epic: string, timeframe: '1m' | '5m'): Promise<void> {
    const db = await getDb();
    const end = new Date();
    let start: Date;
    
    // Try to fetch back to epic's desiredStartDate (user-specified for backtest history)
    // This applies to BOTH 5m and 1m data
    if (db) {
      try {
        const epicRow = await db.select({ 
          desiredStartDate: epics.desiredStartDate,
          startDate: epics.startDate 
        }).from(epics).where(eq(epics.symbol, epic));
        
        if (epicRow.length > 0 && epicRow[0].desiredStartDate) {
          // Use desiredStartDate (user-specified) - this is what they want for backtesting
          start = new Date(epicRow[0].desiredStartDate);
          candleLogger.info('DataService', `Fetching ${epic} ${timeframe} data back to desiredStartDate: ${start.toISOString().slice(0, 10)}`);
        } else if (epicRow.length > 0 && epicRow[0].startDate && timeframe === '5m') {
          // Fallback to startDate (first available candle) if no desired date set - only for 5m
          start = new Date(epicRow[0].startDate);
          candleLogger.info('DataService', `Fetching ${epic} ${timeframe} data back to startDate: ${start.toISOString().slice(0, 10)}`);
        } else if (timeframe === '5m') {
          // Fallback for 5m: 30 days
          start = new Date();
          start.setDate(start.getDate() - 30);
          candleLogger.info('DataService', `No desiredStartDate for ${epic}, fetching 30 days of ${timeframe} data`);
        } else {
          // Fallback for 1m: 7 days
          start = new Date();
          start.setDate(start.getDate() - 7);
          candleLogger.info('DataService', `No desiredStartDate for ${epic}, fetching 7 days of ${timeframe} data`);
        }
      } catch (e) {
        start = new Date();
        start.setDate(start.getDate() - (timeframe === '5m' ? 30 : 7));
      }
    } else {
      start = new Date();
      start.setDate(start.getDate() - (timeframe === '5m' ? 30 : 7));
    }
    
    await this.fillGap(epic, timeframe, start, end);
  }
  
  private async fillGap(epic: string, timeframe: '1m' | '5m', start: Date, end: Date): Promise<void> {
    if (this.inQuietPeriod) {
      candleLogger.debug('DataService', 'Skipping gap fill during quiet period');
      return;
    }
    
    try {
      const credentials = await loadCapitalCredentials();
      if (!credentials) {
        candleLogger.error('DataService', 'No credentials for gap fill');
        return;
      }
      
      const client = new CapitalComAPI(credentials);
      await client.authenticate();
      
      const resolution = timeframe === '5m' ? 'MINUTE_5' : 'MINUTE';
      // 1m data: max 1000 candles = ~16 hours max, use 12 hours for safety
      const chunkHours = timeframe === '5m' ? 72 : 12;  // 3 days vs 12 hours
      
      // Fetch from NEWEST to OLDEST (so we get recent data first)
      let currentEnd = new Date(end);
      let totalFetched = 0;
      let consecutiveErrors = 0;
      // For 1m data: 14 errors = 7 days (12-hour chunks). For 5m: 5 errors = 15 days (3-day chunks)
      const maxConsecutiveErrors = timeframe === '1m' ? 14 : 5;
      
      candleLogger.info('DataService', `Filling gap for ${epic} ${timeframe}: ${start.toISOString()} → ${end.toISOString()} (newest first)`);
      
      while (currentEnd > start) {
        const chunkStart = new Date(currentEnd);
        chunkStart.setHours(chunkStart.getHours() - chunkHours);
        if (chunkStart < start) chunkStart.setTime(start.getTime());
        
        const apiCandles = await client.getHistoricalPrices(
          epic,
          resolution,
          1000,
          chunkStart,
          currentEnd
        );
        
        if (apiCandles && apiCandles.length > 0) {
          await this.writeCandles(epic, timeframe, 'capital', apiCandles, 'api');
          totalFetched += apiCandles.length;
          consecutiveErrors = 0;  // Reset error counter on success
          candleLogger.debug('DataService', `Fetched ${apiCandles.length} candles for ${epic} ${timeframe}`);
        } else {
          consecutiveErrors++;
          candleLogger.warn('DataService', `No data for ${epic} ${timeframe}: ${chunkStart.toISOString()} → ${currentEnd.toISOString()} (${consecutiveErrors}/${maxConsecutiveErrors} consecutive)`);
          
          // For 1m data, hitting max consecutive errors likely means we've reached Capital.com's data limit
          if (timeframe === '1m' && consecutiveErrors >= maxConsecutiveErrors) {
            candleLogger.info('DataService', `Stopping 1m fetch for ${epic} - likely reached Capital.com historical data limit (~2 months for stocks)`);
            break;
          }
        }
        
        currentEnd = chunkStart;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      
      candleLogger.info('DataService', `Gap fill complete: ${totalFetched} candles for ${epic} ${timeframe}`);
      
    } catch (error: any) {
      candleLogger.error('DataService', `Gap fill failed for ${epic} ${timeframe}`, { error: error.message });
    }
  }
  
  /**
   * Fill gap with real-time progress tracking
   */
  private async fillGapWithProgress(epic: string, timeframe: '1m' | '5m', progress: RefreshProgress): Promise<void> {
    if (this.inQuietPeriod) {
      candleLogger.debug('DataService', 'Skipping gap fill during quiet period');
      return;
    }
    
    const startTime = progress.startTime;
    const timeframeProgress = timeframe === '5m' ? progress.fiveMin : progress.oneMin;
    
    try {
      const credentials = await loadCapitalCredentials();
      if (!credentials) {
        throw new Error('No Capital.com credentials');
      }
      
      // Always use LIVE environment for historical data - more reliable than demo
      const liveCredentials = { ...credentials, environment: 'live' as const };
      const client = new CapitalComAPI(liveCredentials);
      await client.authenticate();
      
      // Get date range
      const end = new Date();
      let start: Date;
      
      // Check what data we already have
      const existingRange = await this.getDataRange(epic, timeframe);
      const db = await getDb();
      
      // Get desiredStartDate - applies to BOTH 5m and 1m data
      let desiredStart: Date | null = null;
      if (db) {
        try {
          const epicRow = await db.select({ 
            desiredStartDate: epics.desiredStartDate 
          }).from(epics).where(eq(epics.symbol, epic));
          
          if (epicRow.length > 0 && epicRow[0].desiredStartDate) {
            desiredStart = new Date(epicRow[0].desiredStartDate);
          }
        } catch (e) {
          // Ignore error
        }
      }
      
      // Declare resolution early so it can be used in fetchCandleRange
      const resolution = timeframe === '5m' ? 'MINUTE_5' : 'MINUTE';
      // 1m data: max 1000 candles = ~16 hours max, use 12 hours for safety
      const chunkHours = timeframe === '5m' ? 72 : 12;  // 3 days vs 12 hours
      
      // Determine the full date range we need to fetch
      let historicalStart: Date | null = null;
      
      if (existingRange && existingRange.latest && existingRange.earliest) {
        // We have existing data
        const earliest = new Date(existingRange.earliest);
        
        // Check if we need to fetch older historical data (back to desiredStartDate)
        if (desiredStart && earliest > desiredStart) {
          // Go back to desiredStartDate for BOTH 5m and 1m
          historicalStart = desiredStart;
          
          // Set date range to show the FULL range we're fetching (desiredStart → now)
          timeframeProgress.dateRange = {
            start: desiredStart.toISOString().slice(0, 10),
            end: end.toISOString().slice(0, 10),
          };
          
          // Calculate total chunks for historical + gap fill
          const totalHistoricalMs = earliest.getTime() - desiredStart.getTime();
          const totalGapMs = end.getTime() - new Date(existingRange.latest).getTime();
          const chunkMs = chunkHours * 60 * 60 * 1000;
          timeframeProgress.totalChunks = Math.ceil(totalHistoricalMs / chunkMs) + Math.ceil(totalGapMs / chunkMs);
          
          candleLogger.info('DataService', `${epic} ${timeframe}: Existing data starts at ${earliest.toISOString().slice(0, 10)}, but desiredStartDate is ${desiredStart.toISOString().slice(0, 10)} - fetching historical data first`);
          
          // Fetch historical data backwards from desiredStartDate to earliest
          // Will stop after 14 consecutive errors for 1m (7 days), 5 for 5m (15 days)
          await this.fetchCandleRange(epic, client, resolution as 'MINUTE' | 'MINUTE_5', desiredStart, earliest, timeframeProgress, startTime, progress, chunkHours);
        }
        
        // Now fill from latest to now
        start = new Date(existingRange.latest);
        candleLogger.info('DataService', `${epic} ${timeframe}: Filling gap from ${start.toISOString()} to now`);
      } else {
        // No existing data - fetch full history back to desiredStartDate (for BOTH 5m and 1m)
        if (desiredStart) {
          start = desiredStart;
          candleLogger.info('DataService', `${epic} ${timeframe}: No existing data, fetching back to desiredStartDate: ${start.toISOString().slice(0, 10)}`);
        } else if (timeframe === '5m') {
          // Fallback for 5m: 30 days if no desiredStartDate
          start = new Date();
          start.setDate(start.getDate() - 30);
          candleLogger.info('DataService', `${epic} ${timeframe}: No desiredStartDate set, fetching 30 days`);
        } else {
          // Fallback for 1m: 7 days if no desiredStartDate
          start = new Date();
          start.setDate(start.getDate() - 7);
          candleLogger.info('DataService', `${epic} ${timeframe}: No desiredStartDate set, fetching 7 days`);
        }
      }
      
      // Only set dateRange if not already set by historical backfill
      if (!timeframeProgress.dateRange) {
        timeframeProgress.dateRange = {
          start: start.toISOString().slice(0, 10),
          end: end.toISOString().slice(0, 10),
        };
      }
      
      // Calculate total chunks for the gap fill (if not already set by historical backfill)
      const totalMs = end.getTime() - start.getTime();
      const chunkMs = chunkHours * 60 * 60 * 1000;
      timeframeProgress.totalChunks = Math.ceil(totalMs / chunkMs);
      
      let currentEnd = new Date(end);
      let consecutiveErrors = 0;
      // For 1m data: 14 errors = 7 days (12-hour chunks). For 5m: 5 errors = 15 days (3-day chunks)
      const maxConsecutiveErrors = timeframe === '1m' ? 14 : 5;
      
      candleLogger.info('DataService', `Filling gap for ${epic} ${timeframe}: ${start.toISOString()} → ${end.toISOString()} (${timeframeProgress.totalChunks} chunks)`);
      
      while (currentEnd > start) {
        const chunkStart = new Date(currentEnd);
        chunkStart.setHours(chunkStart.getHours() - chunkHours);
        if (chunkStart < start) chunkStart.setTime(start.getTime());
        
        // Update progress
        timeframeProgress.currentChunk++;
        timeframeProgress.progress = Math.min(100, Math.round((timeframeProgress.currentChunk / timeframeProgress.totalChunks) * 100));
        progress.message = `Fetching ${timeframe} candles... (chunk ${timeframeProgress.currentChunk}/${timeframeProgress.totalChunks})`;
        progress.elapsedMs = Date.now() - startTime;
        emitProgress(progress);
        
        const apiCandles = await client.getHistoricalPrices(
          epic,
          resolution,
          1000,
          chunkStart,
          currentEnd
        );
        
        if (apiCandles && apiCandles.length > 0) {
          await this.writeCandles(epic, timeframe, 'capital', apiCandles, 'api');
          timeframeProgress.candlesFetched += apiCandles.length;
          consecutiveErrors = 0;
          
          // Emit progress with updated candle count
          progress.elapsedMs = Date.now() - startTime;
          emitProgress(progress);
          
          candleLogger.debug('DataService', `Fetched ${apiCandles.length} candles for ${epic} ${timeframe} (total: ${timeframeProgress.candlesFetched})`);
        } else {
          consecutiveErrors++;
          
          if (timeframe === '1m' && consecutiveErrors >= maxConsecutiveErrors) {
            candleLogger.info('DataService', `Stopping 1m fetch for ${epic} - reached Capital.com limit`);
            timeframeProgress.progress = 100;
            break;
          }
        }
        
        currentEnd = chunkStart;
        await new Promise(resolve => setTimeout(resolve, 300));  // Slightly faster for better UX
      }
      
      timeframeProgress.progress = 100;
      progress.elapsedMs = Date.now() - startTime;
      emitProgress(progress);
      
      candleLogger.info('DataService', `Gap fill complete: ${timeframeProgress.candlesFetched} candles for ${epic} ${timeframe}`);
      
    } catch (error: any) {
      candleLogger.error('DataService', `Gap fill failed for ${epic} ${timeframe}`, { error: error.message });
      throw error;
    }
  }
  
  /**
   * Fetch candles for a specific date range (used for historical backfill)
   */
  private async fetchCandleRange(
    epic: string,
    client: CapitalComAPI,
    resolution: 'MINUTE' | 'MINUTE_5',
    start: Date,
    end: Date,
    timeframeProgress: { progress: number; currentChunk: number; totalChunks: number; candlesFetched: number; dateRange?: { start: string; end: string } },
    startTime: number,
    progress: RefreshProgress,
    chunkHours: number
  ): Promise<void> {
    const timeframe = resolution === 'MINUTE_5' ? '5m' : '1m';
    
    // Calculate total chunks for this range
    const totalMs = end.getTime() - start.getTime();
    const chunkMs = chunkHours * 60 * 60 * 1000;
    const rangeChunks = Math.ceil(totalMs / chunkMs);
    
    candleLogger.info('DataService', `Fetching historical ${timeframe} data: ${start.toISOString().slice(0, 10)} → ${end.toISOString().slice(0, 10)} (${rangeChunks} chunks)`);
    
    let currentEnd = new Date(end);
    let consecutiveErrors = 0;
    // For 1m data: 14 errors = 7 days (12-hour chunks). For 5m: 5 errors = 15 days (3-day chunks)
    const maxConsecutiveErrors = timeframe === '1m' ? 14 : 5;
    
    while (currentEnd > start && consecutiveErrors < maxConsecutiveErrors) {
      const chunkStart = new Date(currentEnd);
      chunkStart.setHours(chunkStart.getHours() - chunkHours);
      if (chunkStart < start) chunkStart.setTime(start.getTime());
      
      // Update progress - increment currentChunk and calculate percentage
      timeframeProgress.currentChunk++;
      timeframeProgress.progress = Math.min(100, Math.round((timeframeProgress.currentChunk / timeframeProgress.totalChunks) * 100));
      progress.message = `Fetching historical ${timeframe}... (chunk ${timeframeProgress.currentChunk}/${timeframeProgress.totalChunks})`;
      progress.elapsedMs = Date.now() - startTime;
      emitProgress(progress);
      
      try {
        const apiCandles = await client.getHistoricalPrices(
          epic,
          resolution,
          1000,
          chunkStart,
          currentEnd
        );
        
        if (apiCandles && apiCandles.length > 0) {
          await this.writeCandles(epic, timeframe, 'capital', apiCandles, 'api');
          timeframeProgress.candlesFetched += apiCandles.length;
          candleLogger.debug('DataService', `Fetched ${apiCandles.length} historical candles for ${epic} ${timeframe} (total: ${timeframeProgress.candlesFetched})`);
          consecutiveErrors = 0;
        } else {
          consecutiveErrors++;
          candleLogger.warn('DataService', `No data for ${epic} ${timeframe}: ${chunkStart.toISOString()} → ${currentEnd.toISOString()} (${consecutiveErrors}/${maxConsecutiveErrors})`);
        }
        
        // Move window back
        currentEnd = new Date(chunkStart);
        
        // Rate limiting
        await new Promise(resolve => setTimeout(resolve, 300));
        
      } catch (error: any) {
        consecutiveErrors++;
        candleLogger.warn('DataService', `Error fetching ${epic} ${timeframe}: ${error.message} (${consecutiveErrors}/${maxConsecutiveErrors})`);
        
        if (consecutiveErrors >= maxConsecutiveErrors) {
          candleLogger.info('DataService', `Stopping historical fetch for ${epic} ${timeframe} after ${maxConsecutiveErrors} consecutive errors`);
          break;
        }
        
        // Still move window back to try next chunk
        currentEnd = new Date(chunkStart);
        await new Promise(resolve => setTimeout(resolve, 500));
      }
    }
    
    candleLogger.info('DataService', `Historical ${timeframe} fetch complete for ${epic}: ${timeframeProgress.candlesFetched} candles`);
  }
  
  private async writeCandles(
    epic: string,
    timeframe: '1m' | '5m',
    source: 'av' | 'capital',
    apiCandles: any[],
    sourceType: 'api' | 'websocket' | 'backfill'
  ): Promise<void> {
    if (this.inQuietPeriod) {
      // Buffer instead
      const key = `${epic}:${source}:${timeframe}`;
      let buffer = this.candleBuffer.get(key);
      if (!buffer) {
        buffer = [];
        this.candleBuffer.set(key, buffer);
      }
      apiCandles.forEach(c => buffer!.push({ candle: c, receivedAt: new Date() }));
      return;
    }
    
    const db = await getDb();
    if (!db) return;
    
    for (const c of apiCandles) {
      try {
        const ts = c.snapshotTimeUTC || c.snapshotTime || c.timestamp;
        // CRITICAL: Capital.com timestamps may not have 'Z' suffix but ARE in UTC
        // Force UTC interpretation by appending 'Z' if not present
        let timestamp: Date;
        if (ts instanceof Date) {
          timestamp = ts;
        } else {
          const tsStr = String(ts).replace(' ', 'T');
          // If no timezone indicator, treat as UTC
          const tsUTC = tsStr.endsWith('Z') || tsStr.includes('+') || tsStr.includes('-') ? tsStr : `${tsStr}Z`;
          timestamp = new Date(tsUTC);
        }
        
        await db.insert(candles).values({
          epic,
          source,
          timeframe,
          timestamp,
          openBid: String(c.openPrice?.bid || c.open_bid || c.openPrice || 0),
          highBid: String(c.highPrice?.bid || c.high_bid || c.highPrice || 0),
          lowBid: String(c.lowPrice?.bid || c.low_bid || c.lowPrice || 0),
          closeBid: String(c.closePrice?.bid || c.close_bid || c.closePrice || 0),
          openAsk: String(c.openPrice?.ask || c.open_ask || c.openPrice || 0),
          highAsk: String(c.highPrice?.ask || c.high_ask || c.highPrice || 0),
          lowAsk: String(c.lowPrice?.ask || c.low_ask || c.lowPrice || 0),
          closeAsk: String(c.closePrice?.ask || c.close_ask || c.closePrice || 0),
          volume: c.lastTradedVolume || c.volume || 0,
          dataSourceType: sourceType,
        }).onDuplicateKeyUpdate({
          set: {
            closeBid: String(c.closePrice?.bid || c.close_bid || c.closePrice || 0),
            closeAsk: String(c.closePrice?.ask || c.close_ask || c.closePrice || 0),
          }
        });
      } catch (e: any) {
        if (!e.message.includes('Duplicate')) {
          candleLogger.error('DataService', 'Write failed', { error: e.message });
        }
      }
    }
  }
  
  private webSocketStarting: boolean = false;
  
  private async startWebSocket(epicList: string[]): Promise<void> {
    // Prevent concurrent WebSocket initialization
    if (this.webSocketStarting) {
      candleLogger.warn('DataService', 'WebSocket initialization already in progress, skipping');
      return;
    }
    
    // Filter to only OPEN markets (no point subscribing to closed markets)
    const openEpics = await this.filterOpenMarkets(epicList);
    
    if (openEpics.length === 0) {
      candleLogger.info('DataService', 'No open markets to subscribe to WebSocket');
      return;
    }
    
    if (openEpics.length < epicList.length) {
      const closedCount = epicList.length - openEpics.length;
      candleLogger.info('DataService', `📴 Skipping ${closedCount} closed markets for WebSocket`, {
        open: openEpics,
        closed: epicList.filter(e => !openEpics.includes(e)),
      });
    }
    
    // Skip if already connected
    if (candleWebSocket.connected) {
      candleLogger.info('DataService', 'WebSocket already connected, subscribing to open epics');
      candleWebSocket.subscribeToEpics(openEpics);
      return;
    }
    
    this.webSocketStarting = true;
    
    try {
      const credentials = await loadCapitalCredentials();
      if (!credentials) {
        candleLogger.error('DataService', 'No credentials for WebSocket');
        this.webSocketStarting = false;
        return;
      }
      
      const client = new CapitalComAPI(credentials);
      await client.authenticate();
      
      const tokens = client.getAuthTokens();
      if (!tokens) {
        candleLogger.error('DataService', 'No auth tokens for WebSocket');
        this.webSocketStarting = false;
        return;
      }
      
      const connected = await candleWebSocket.connect(tokens.cst, tokens.securityToken);
      
      if (connected) {
        // Small delay to ensure WebSocket is fully ready
        await new Promise(resolve => setTimeout(resolve, 100));
        candleWebSocket.subscribeToEpics(openEpics);
        
        // Listen for candles and write to DB
        candleWebSocket.on('candle', async (candle: WebSocketCandle) => {
          // Track last candle received for health monitoring
          this.lastCandleReceivedAt = new Date();
          
          const timeframe = candle.resolution === 'MINUTE_5' ? '5m' : '1m';
          
          // Convert to API format and write
          await this.writeCandles(candle.epic, timeframe, 'capital', [{
            snapshotTimeUTC: new Date(candle.timestamp),
            openPrice: { bid: candle.open, ask: candle.open },
            highPrice: { bid: candle.high, ask: candle.high },
            lowPrice: { bid: candle.low, ask: candle.low },
            closePrice: { bid: candle.close, ask: candle.close },
            volume: 0
          }], 'websocket');
        });
        
        candleLogger.info('DataService', 'WebSocket streaming started', { epics: openEpics });
      }
      
    } catch (error: any) {
      candleLogger.error('DataService', 'WebSocket setup failed', { error: error.message });
    } finally {
      this.webSocketStarting = false;
    }
  }
  
  /**
   * Filter epic list to only include markets that are currently OPEN (TRADEABLE)
   */
  private async filterOpenMarkets(epicList: string[]): Promise<string[]> {
    try {
      const db = await getDb();
      if (!db) return epicList; // If no DB, subscribe to all
      
      const epicRecords = await db
        .select({
          symbol: epics.symbol,
          marketStatus: epics.marketStatus,
        })
        .from(epics)
        .where(inArray(epics.symbol, epicList));
      
      const openEpics = epicRecords
        .filter(e => e.marketStatus === 'TRADEABLE')
        .map(e => e.symbol);
      
      return openEpics;
    } catch (error: any) {
      candleLogger.error('DataService', 'Failed to filter open markets', { error: error.message });
      return epicList; // On error, subscribe to all
    }
  }
  
  private startPeriodicAudit(): void {
    // Run gap audit every hour
    this.auditInterval = setInterval(async () => {
      if (!this.inQuietPeriod) {
        candleLogger.info('DataService', 'Running periodic audit...');
        // Use active strategy epics (more efficient than all epics)
        const epicList = await this.getActiveStrategyEpics();
        
        // Check WebSocket health first
        await this.checkWebSocketHealth(epicList);
        
        await this.auditAndFillGaps(epicList);
        
        // Also check for and retry any recent gaps
        await this.retryRecentGaps(epicList);
      }
    }, 60 * 60 * 1000);
    
    candleLogger.info('DataService', 'Periodic audit scheduled (hourly)');
  }
  
  /**
   * Check WebSocket health and reconnect if needed
   */
  private async checkWebSocketHealth(epicList: string[]): Promise<void> {
    const isConnected = candleWebSocket.connected;
    const subscribedEpics = candleWebSocket.getSubscribedEpics();
    
    // Check if WebSocket is connected
    if (!isConnected) {
      candleLogger.warn('DataService', 'WebSocket disconnected, attempting reconnect...');
      await this.startWebSocket(epicList);
      return;
    }
    
    // Check if all epics are subscribed
    const missingEpics = epicList.filter(e => !subscribedEpics.includes(e));
    if (missingEpics.length > 0) {
      candleLogger.warn('DataService', 'Missing epic subscriptions, resubscribing...', { missing: missingEpics });
      candleWebSocket.subscribeToEpics(epicList);
    }
    
    // Check if we've received candles recently (within 10 minutes for 24/7 markets like BTC)
    if (this.lastCandleReceivedAt) {
      const minutesSinceLastCandle = (Date.now() - this.lastCandleReceivedAt.getTime()) / (1000 * 60);
      
      if (minutesSinceLastCandle > 10) {
        candleLogger.warn('DataService', `No candles received for ${minutesSinceLastCandle.toFixed(0)} minutes, reconnecting WebSocket...`);
        candleWebSocket.disconnect();
        await this.startWebSocket(epicList);
      } else {
        candleLogger.info('DataService', 'WebSocket health OK', { 
          connected: true, 
          subscribedEpics: subscribedEpics.length,
          lastCandleMinutesAgo: minutesSinceLastCandle.toFixed(1)
        });
      }
    }
  }
  
  /**
   * Find and retry filling gaps from the last 24 hours
   * Sometimes Capital.com data becomes available later
   */
  private async retryRecentGaps(epicList: string[]): Promise<void> {
    const db = await getDb();
    if (!db) return;
    
    const oneDayAgo = new Date();
    oneDayAgo.setHours(oneDayAgo.getHours() - 24);
    
    for (const epic of epicList) {
      try {
        // Find gaps > 5 minutes in the last 24 hours
        const result = await db.execute(sql`
          SELECT timestamp FROM candles 
          WHERE epic = ${epic} AND source = 'capital' AND timeframe = '1m'
            AND timestamp > ${oneDayAgo}
          ORDER BY timestamp
        `);
        
        const rows = (result as any)?.[0] || [];
        if (rows.length < 2) continue;
        
        // Helper to parse MySQL timestamps as UTC
        const parseAsUTC = (ts: any): Date => {
          if (ts instanceof Date) {
            return new Date(Date.UTC(
              ts.getFullYear(), ts.getMonth(), ts.getDate(),
              ts.getHours(), ts.getMinutes(), ts.getSeconds()
            ));
          }
          const str = String(ts).replace(' ', 'T');
          return new Date(str.endsWith('Z') ? str : `${str}Z`);
        };
        
        const gaps: { from: Date; to: Date }[] = [];
        for (let i = 1; i < rows.length; i++) {
          const prev = parseAsUTC(rows[i-1].timestamp);
          const curr = parseAsUTC(rows[i].timestamp);
          const gapMinutes = (curr.getTime() - prev.getTime()) / (1000 * 60);
          
          // Only consider gaps 5-60 minutes (not overnight gaps)
          if (gapMinutes > 5 && gapMinutes <= 60) {
            gaps.push({ from: prev, to: curr });
          }
        }
        
        if (gaps.length > 0) {
          candleLogger.info('DataService', `Found ${gaps.length} recent gaps for ${epic} 1m, attempting to fill...`);
          
          const credentials = await loadCapitalCredentials();
          if (!credentials) continue;
          
          const client = new CapitalComAPI(credentials);
          await client.authenticate();
          
          for (const gap of gaps) {
            const apiCandles = await client.getHistoricalPrices(
              epic,
              'MINUTE',
              1000,
              new Date(gap.from.getTime() - 60000), // 1 min before
              new Date(gap.to.getTime() + 60000)    // 1 min after
            );
            
            if (apiCandles && apiCandles.length > 0) {
              await this.writeCandles(epic, '1m', 'capital', apiCandles, 'api');
              candleLogger.info('DataService', `Filled gap ${gap.from.toISOString()} with ${apiCandles.length} candles`);
            }
            
            await new Promise(resolve => setTimeout(resolve, 500));
          }
        }
      } catch (e: any) {
        candleLogger.error('DataService', `Gap retry failed for ${epic}`, { error: e.message });
      }
    }
  }
  
  private startDbWriteInterval(): void {
    this.dbWriteInterval = setInterval(async () => {
      if (!this.inQuietPeriod) {
        await this.flushBuffer();
      }
    }, 30000);
  }
  
  private async flushBuffer(): Promise<void> {
    for (const [key, items] of this.candleBuffer) {
      if (items.length > 0) {
        const [epic, source, timeframe] = key.split(':');
        await this.writeCandles(
          epic,
          timeframe as '1m' | '5m',
          source as 'av' | 'capital',
          items.map(i => i.candle),
          'websocket'
        );
        items.length = 0;
      }
    }
  }
  
  /**
   * Enter quiet period (10 min before window close)
   */
  enterQuietPeriod(): void {
    candleLogger.info('DataService', 'Entering quiet period - DB writes paused');
    this.inQuietPeriod = true;
  }
  
  /**
   * Exit quiet period (after trading window)
   */
  async exitQuietPeriod(): Promise<void> {
    candleLogger.info('DataService', 'Exiting quiet period - flushing buffer');
    this.inQuietPeriod = false;
    await this.flushBuffer();
  }
  
  /**
   * Get candle for brain calculation
   * Waits up to 3 seconds for the expected candle, falls back to previous if not available
   */
  async getCandleForBrain(
    epic: string,
    expectedTimestamp: number
  ): Promise<{ candle: CandlePair | null; usedFallback: boolean; comment: string }> {
    
    // Wait up to 3 seconds for the expected candle
    const candle = await candleWebSocket.waitForCandle(epic, 'MINUTE', expectedTimestamp, 3000);
    
    if (candle && candle.bid) {
      return {
        candle,
        usedFallback: false,
        comment: `Used expected candle at ${new Date(expectedTimestamp).toISOString()}`
      };
    }
    
    // Fallback to latest available
    const fallback = candleWebSocket.getLatestCandle(epic, 'MINUTE');
    
    if (fallback && fallback.bid) {
      const fallbackTs = new Date(fallback.bid.timestamp).toISOString();
      candleLogger.warn('DataService', `Using fallback candle for ${epic}`, {
        expected: new Date(expectedTimestamp).toISOString(),
        actual: fallbackTs
      });
      
      return {
        candle: fallback,
        usedFallback: true,
        comment: `Candle at ${new Date(expectedTimestamp).toISOString()} not available, used ${fallbackTs} instead`
      };
    }
    
    candleLogger.error('DataService', `No candle available for ${epic}`);
    return {
      candle: null,
      usedFallback: true,
      comment: 'No candle data available from WebSocket'
    };
  }
  
  /**
   * Build fake 5-min candle from 1-min history
   */
  buildFake5MinCandle(epic: string): { close: number; sourceTimestamp: Date; comment: string } | null {
    const history = candleWebSocket.get1MinHistory(epic, 4);
    
    if (history.length < 3) {
      candleLogger.warn('DataService', `Not enough 1-min candles for fake 5-min: ${epic}`, { count: history.length });
      return null;
    }
    
    const lastCandle = history[history.length - 1];
    
    return {
      close: lastCandle.close,
      sourceTimestamp: new Date(lastCandle.timestamp),
      comment: `Fake 5-min close from ${history.length} 1-min candles, last at ${new Date(lastCandle.timestamp).toISOString()}`
    };
  }
  
  /**
   * Store fake 5-min close in database
   */
  async storeFake5MinClose(
    epic: string,
    timestamp: Date,
    fake5minClose: number,
    sourceTimestamp: Date,
    comment: string
  ): Promise<void> {
    const db = await getDb();
    if (!db) return;
    
    try {
      await db.update(candles)
        .set({
          fake5minClose: String(fake5minClose),
          fake5minSourceTimestamp: sourceTimestamp,
          fake5minComment: comment,
          fake5minCalculatedAt: new Date()
        })
        .where(
          and(
            eq(candles.epic, epic),
            eq(candles.source, 'capital'),
            eq(candles.timeframe, '5m'),
            eq(candles.timestamp, timestamp)
          )
        );
      
      candleLogger.info('DataService', 'Stored fake 5-min close', {
        epic,
        timestamp: timestamp.toISOString(),
        fake5minClose,
        comment
      });
      
    } catch (error: any) {
      candleLogger.error('DataService', 'Failed to store fake 5-min close', { error: error.message });
    }
  }
  
  /**
   * Calculate and store fake 5-min candle for an epic at T-60 before market close
   * This should be called 60 seconds before each epic's market close
   * IMPORTANT: This runs for ALL epics, regardless of whether there's a trade
   */
  async captureT60FakeCandle(epic: string): Promise<{ success: boolean; fakeClose?: number; comment?: string }> {
    candleLogger.info('DataService', `Capturing T-60 fake candle for ${epic}`);
    
    try {
      // Build fake 5-min candle from last 4 minutes of 1-min data
      const fakeCandle = this.buildFake5MinCandle(epic);
      
      if (!fakeCandle) {
        candleLogger.warn('DataService', `Could not build fake candle for ${epic} - not enough 1-min data`);
        return { success: false, comment: 'Insufficient 1-min data for fake candle' };
      }
      
      // Determine the 5-min candle timestamp this belongs to (round down to nearest 5-min)
      const now = new Date();
      const minutes = now.getMinutes();
      const roundedMinutes = Math.floor(minutes / 5) * 5;
      const candleTimestamp = new Date(now);
      candleTimestamp.setMinutes(roundedMinutes, 0, 0);
      
      // Store the fake candle
      await this.storeFake5MinClose(
        epic,
        candleTimestamp,
        fakeCandle.close,
        fakeCandle.sourceTimestamp,
        `T-60 capture: ${fakeCandle.comment}`
      );
      
      candleLogger.info('DataService', `✅ T-60 fake candle captured for ${epic}`, {
        fakeClose: fakeCandle.close,
        candleTimestamp: candleTimestamp.toISOString()
      });
      
      return { 
        success: true, 
        fakeClose: fakeCandle.close,
        comment: fakeCandle.comment 
      };
      
    } catch (error: any) {
      candleLogger.error('DataService', `Failed to capture T-60 fake candle for ${epic}`, { error: error.message });
      return { success: false, comment: error.message };
    }
  }
  
  /**
   * Capture T-60 fake candles for ALL active epics using REST API
   * Should be called 60 seconds before each market close window
   * 
   * CRITICAL: Uses REST API to get INSTANT bid/ask prices, not delayed WebSocket data!
   * This is essential because WebSocket candles arrive ~23 seconds late.
   */
  async captureAllT60FakeCandles(): Promise<Map<string, { success: boolean; close?: number; error?: string }>> {
    const results = new Map<string, { success: boolean; close?: number; error?: string }>();
    // Only capture T-60 for epics in active strategies
    const epicList = await this.getActiveStrategyEpics();
    
    candleLogger.info('DataService', `🔥 T-60 CAPTURE: Getting instant prices via REST API for ${epicList.length} active epics`);
    
    // First, try to get instant prices via REST API (primary method)
    const apiPrices = await this.getInstantPrices(epicList);
    
    for (const epic of epicList) {
      const apiPrice = apiPrices.get(epic);
      
      if (apiPrice && apiPrice.success && apiPrice.mid) {
        // Success: Got instant price from API
        candleLogger.info('DataService', `✅ ${epic}: API price ${apiPrice.mid.toFixed(4)} (bid: ${apiPrice.bid?.toFixed(4)}, ask: ${apiPrice.ask?.toFixed(4)})`);
        
        // Store the fake candle close in database
        const now = new Date();
        const minutes = now.getMinutes();
        const roundedMinutes = Math.floor(minutes / 5) * 5;
        const candleTimestamp = new Date(now);
        candleTimestamp.setMinutes(roundedMinutes, 0, 0);
        
        await this.storeFake5MinClose(
          epic,
          candleTimestamp,
          apiPrice.mid,
          now,
          `T-60 API capture: bid=${apiPrice.bid}, ask=${apiPrice.ask}`
        );
        
        results.set(epic, { 
          success: true, 
          close: apiPrice.mid 
        });
      } else {
        // NO FALLBACK: If API fails, we don't proceed with stale WebSocket data
        // This is intentional - we need accurate T-60s price, not delayed WebSocket data
        candleLogger.error('DataService', `❌ ${epic}: API failed - NO FALLBACK (${apiPrice?.error})`);
        results.set(epic, { 
          success: false, 
          error: apiPrice?.error || 'API price fetch failed - no fallback'
        });
      }
    }
    
    const successCount = Array.from(results.values()).filter(r => r.success).length;
    candleLogger.info('DataService', `T-60 capture complete: ${successCount}/${epicList.length} epics`, {
      successful: [...results.entries()].filter(([_, r]) => r.success).map(([e, r]) => `${e}:${r.close}`),
      failed: [...results.entries()].filter(([_, r]) => !r.success).map(([e, r]) => `${e}:${r.error}`)
    });
    
    return results;
  }
  
  /**
   * Get instant bid/ask prices for multiple epics via REST API
   * This is critical for T-60s timing because WebSocket candles are delayed
   * 
   * CRITICAL FIX: Uses cached client from connectionManager to AVOID new auth call!
   * The auth endpoint has a 1 req/s limit - creating new clients at T-60 was causing
   * rate limit issues right when we need API access the most.
   * 
   * Returns mid-price (average of bid and ask) for each epic
   */
  async getInstantPrices(epicList: string[]): Promise<Map<string, { 
    success: boolean; 
    bid?: number; 
    ask?: number; 
    mid?: number;
    error?: string;
  }>> {
    const results = new Map<string, { success: boolean; bid?: number; ask?: number; mid?: number; error?: string }>();
    
    candleLogger.info('DataService', `Getting instant prices for ${epicList.length} epics via REST API`);
    
    try {
      // ========================================================================
      // CRITICAL: Use cached client to AVOID new auth call at T-60!
      // Auth endpoint has 1 req/s limit - new auth here was causing rate limits
      // ========================================================================
      let client = connectionManager.getClient('live') || connectionManager.getClient('demo');
      let usedCachedClient = !!client;
      
      if (!client) {
        // Fallback: No cached client available - must create new one (rare case)
        candleLogger.warn('DataService', '⚠️ No cached client available - creating new one (will authenticate)');
        
        const credentials = await loadCapitalCredentials();
        if (!credentials) {
          candleLogger.error('DataService', 'No Capital.com credentials available');
          epicList.forEach(epic => results.set(epic, { success: false, error: 'No credentials' }));
          return results;
        }
        
        client = new CapitalComAPI(credentials);
        await client.authenticate();
        usedCachedClient = false;
      } else {
        candleLogger.info('DataService', `✅ Using cached ${connectionManager.getClient('live') ? 'LIVE' : 'DEMO'} client (no auth needed)`);
      }
      
      // Fetch prices in parallel (but with some concurrency limit)
      const batchSize = 5;
      for (let i = 0; i < epicList.length; i += batchSize) {
        const batch = epicList.slice(i, i + batchSize);
        
        const batchPromises = batch.map(async (epic) => {
          try {
            const marketInfo = await client!.getMarketInfo(epic);
            
            if (marketInfo?.snapshot) {
              const bid = marketInfo.snapshot.bid;
              const ask = marketInfo.snapshot.offer;
              
              if (bid && ask) {
                const mid = (bid + ask) / 2;
                return { epic, success: true, bid, ask, mid };
              } else {
                return { epic, success: false, error: 'No bid/ask in snapshot' };
              }
            } else {
              return { epic, success: false, error: 'No market snapshot available' };
            }
          } catch (error: any) {
            return { epic, success: false, error: error.message };
          }
        });
        
        const batchResults = await Promise.all(batchPromises);
        
        for (const result of batchResults) {
          results.set(result.epic, {
            success: result.success,
            bid: result.bid,
            ask: result.ask,
            mid: result.mid,
            error: result.error
          });
        }
        
        // Small delay between batches to respect rate limits
        if (i + batchSize < epicList.length) {
          await new Promise(resolve => setTimeout(resolve, 100));
        }
      }
      
      candleLogger.info('DataService', `Price fetch complete: ${results.size} epics, cached client: ${usedCachedClient}`);
      
    } catch (error: any) {
      candleLogger.error('DataService', 'Failed to get instant prices', { error: error.message });
      epicList.forEach(epic => {
        if (!results.has(epic)) {
          results.set(epic, { success: false, error: error.message });
        }
      });
    }
    
    return results;
  }
  
  /**
   * Get instant price for a single epic via REST API
   * Convenience wrapper for getInstantPrices()
   */
  async getInstantPrice(epic: string): Promise<{ bid: number; ask: number; mid: number } | null> {
    const results = await this.getInstantPrices([epic]);
    const result = results.get(epic);
    
    if (result?.success && result.bid && result.ask && result.mid) {
      return { bid: result.bid, ask: result.ask, mid: result.mid };
    }
    
    return null;
  }
  
  /**
   * Ensure fresh candles are available for brain preview/simulate
   * If data is stale (>maxAgeMinutes), fetches latest candles from API
   * 
   * @param epicList - List of epics to check/refresh
   * @param maxAgeMinutes - Maximum acceptable data age (default 5 minutes)
   * @returns Object with refresh status per epic
   */
  async ensureFreshCandles(epicList: string[], maxAgeMinutes: number = 5): Promise<{
    [epic: string]: {
      wasFresh: boolean;
      fetched: number;
      latestTimestamp: string | null;
      ageMinutes: number;
    }
  }> {
    const results: { [epic: string]: { wasFresh: boolean; fetched: number; latestTimestamp: string | null; ageMinutes: number } } = {};
    const db = await getDb();
    if (!db) return results;
    
    candleLogger.info('DataService', `Ensuring fresh candles for ${epicList.length} epics (max age: ${maxAgeMinutes} min)`);
    
    for (const epic of epicList) {
      try {
        // Check current data age
        const dataRange = await this.getDataRange(epic, '5m');
        const now = new Date();
        
        if (!dataRange || !dataRange.latest) {
          candleLogger.warn('DataService', `No 5m data found for ${epic}`);
          results[epic] = { wasFresh: false, fetched: 0, latestTimestamp: null, ageMinutes: Infinity };
          continue;
        }
        
        const ageMinutes = (now.getTime() - dataRange.latest.getTime()) / (1000 * 60);
        
        if (ageMinutes <= maxAgeMinutes) {
          // Data is fresh enough
          candleLogger.info('DataService', `✓ ${epic} data is fresh (${ageMinutes.toFixed(1)} min old)`);
          results[epic] = { 
            wasFresh: true, 
            fetched: 0, 
            latestTimestamp: dataRange.latest.toISOString(), 
            ageMinutes 
          };
          continue;
        }
        
        // Data is stale - fetch fresh candles from API
        candleLogger.info('DataService', `⚠ ${epic} data is ${ageMinutes.toFixed(1)} min old, fetching fresh...`);
        
        // Use cached client to avoid unnecessary authentication (prevents rate limiting)
        const { createCapitalAPIClient } = await import('../live_trading/credentials');
        const client = await createCapitalAPIClient('live'); // Always use live for candle data
        if (!client) {
          candleLogger.error('DataService', 'No API client available for fresh candle fetch');
          results[epic] = { wasFresh: false, fetched: 0, latestTimestamp: dataRange.latest.toISOString(), ageMinutes };
          continue;
        }
        
        // Fetch from the last known candle timestamp to now (fill the gap)
        // Use a minimum of 4 hours to ensure we catch any extended hours trading
        const gapStart = dataRange.latest;
        const fourHoursAgo = new Date(now.getTime() - 4 * 60 * 60 * 1000);
        const fetchFrom = gapStart < fourHoursAgo ? fourHoursAgo : gapStart;
        
        candleLogger.info('DataService', `Fetching ${epic} 5m candles from ${fetchFrom.toISOString()} to ${now.toISOString()}`);
        const apiCandles = await client.getHistoricalPrices(epic, 'MINUTE_5', 500, fetchFrom, now);
        
        if (apiCandles && apiCandles.length > 0) {
          await this.writeCandles(epic, '5m', 'capital', apiCandles, 'api');
          
          // Get the latest timestamp from fetched candles
          const latestFetched = apiCandles[apiCandles.length - 1];
          const latestTs = latestFetched.snapshotTimeUTC || latestFetched.snapshotTime || latestFetched.timestamp;
          
          // Parse timestamp correctly (Capital.com returns UTC but may not have 'Z' suffix)
          let latestDate: Date;
          if (latestTs instanceof Date) {
            latestDate = latestTs;
          } else {
            const tsStr = String(latestTs).replace(' ', 'T');
            const tsUTC = tsStr.endsWith('Z') || tsStr.includes('+') || tsStr.includes('-') ? tsStr : `${tsStr}Z`;
            latestDate = new Date(tsUTC);
          }
          
          candleLogger.info('DataService', `✓ ${epic}: Fetched ${apiCandles.length} fresh candles (latest: ${latestDate.toISOString()})`);
          results[epic] = { 
            wasFresh: false, 
            fetched: apiCandles.length, 
            latestTimestamp: latestDate.toISOString(), 
            ageMinutes: (now.getTime() - latestDate.getTime()) / (1000 * 60)
          };
        } else {
          candleLogger.warn('DataService', `${epic}: No fresh candles available from API`);
          results[epic] = { wasFresh: false, fetched: 0, latestTimestamp: dataRange.latest.toISOString(), ageMinutes };
        }
        
        // Small delay to respect rate limits
        await new Promise(resolve => setTimeout(resolve, 200));
        
      } catch (error: any) {
        candleLogger.error('DataService', `Error ensuring fresh candles for ${epic}`, { error: error.message });
        results[epic] = { wasFresh: false, fetched: 0, latestTimestamp: null, ageMinutes: Infinity };
      }
    }
    
    return results;
  }
  
  /**
   * Quick stats audit - updates epics table with current data ranges without downloading
   * This is a lightweight operation suitable for page load
   */
  async quickStatsAudit(): Promise<void> {
    const db = await getDb();
    if (!db) return;
    
    candleLogger.info('DataService', 'Running quick stats audit...');
    
    try {
      // Only audit epics from active strategies
      const epicList = await this.getActiveStrategyEpics();
      
      for (const epic of epicList) {
        // Get 5m data stats
        const [stats] = await db
          .select({
            earliest: sql<string>`DATE(MIN(timestamp))`,
            latest: sql<string>`DATE(MAX(timestamp))`,
            total: sql<number>`COUNT(*)`
          })
          .from(candles)
          .where(
            and(
              eq(candles.epic, epic),
              eq(candles.source, 'capital'),
              eq(candles.timeframe, '5m')
            )
          );
        
        if (stats?.earliest && stats?.latest) {
          // Update epics table with current stats
          await db.update(epics)
            .set({
              startDate: stats.earliest,
              endDate: stats.latest,
              candleCount: stats.total || 0,
            })
            .where(eq(epics.symbol, epic));
        }
      }
      
      candleLogger.info('DataService', `Quick stats audit complete for ${epicList.length} epics`);
    } catch (error: any) {
      candleLogger.error('DataService', 'Quick stats audit failed', { error: error.message });
    }
  }
  
  /**
   * Refresh WebSocket subscriptions based on current active accounts/strategies
   */
  async refreshActiveSubscriptions(): Promise<void> {
    candleLogger.info('DataService', '🔄 Refreshing WebSocket subscriptions...');
    
    try {
      const activeEpics = await this.getActiveStrategyEpics();
      const currentSubscribed = candleWebSocket.getSubscribedEpics();
      
      const toSubscribe = activeEpics.filter(e => !currentSubscribed.includes(e));
      const toUnsubscribe = currentSubscribed.filter(e => !activeEpics.includes(e));
      
      if (toSubscribe.length === 0 && toUnsubscribe.length === 0) {
        candleLogger.info('DataService', '✅ Subscriptions already up to date');
        return;
      }
      
      if (toUnsubscribe.length > 0) {
        candleLogger.info('DataService', `📴 Unsubscribing from ${toUnsubscribe.length} epics`);
        candleWebSocket.unsubscribeFromEpics(toUnsubscribe);
      }
      
      if (toSubscribe.length > 0) {
        const openEpics = await this.filterOpenMarkets(toSubscribe);
        if (openEpics.length > 0) {
          candleLogger.info('DataService', `📡 Subscribing to ${openEpics.length} new epics`);
          candleWebSocket.subscribeToEpics(openEpics);
        }
      }
      
      candleLogger.info('DataService', '✅ Subscription refresh complete');
    } catch (error: any) {
      candleLogger.error('DataService', 'Failed to refresh subscriptions', { error: error.message });
    }
  }
  
  /**
   * Shutdown
   */
  async shutdown(): Promise<void> {
    candleLogger.info('DataService', 'Shutting down...');
    
    if (this.auditInterval) clearInterval(this.auditInterval);
    if (this.dbWriteInterval) clearInterval(this.dbWriteInterval);
    
    await this.flushBuffer();
    candleWebSocket.disconnect();
    
    this.isRunning = false;
    candleLogger.info('DataService', 'Shutdown complete');
  }
  
  get running(): boolean {
    return this.isRunning;
  }
}

export const candleDataService = new CandleDataService();

/**
 * Convenience function to refresh WebSocket subscriptions
 */
export async function refreshWebSocketSubscriptions(): Promise<void> {
  console.log('[WebSocket] refreshWebSocketSubscriptions called');
  await candleDataService.refreshActiveSubscriptions();
}
