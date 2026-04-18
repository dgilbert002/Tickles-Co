/**
 * Capital.com WebSocket Client for Real-Time OHLC Data
 * 
 * Subscribes to MINUTE and MINUTE_5 candle data for all epics in active strategies.
 * This provides real-time candle updates without API rate limits.
 * 
 * Usage:
 * - Call connect() on server startup
 * - Call subscribeToEpics() when strategies are activated
 * - Listen for 'candle' events to get real-time updates
 * - At T-60s, use getLatest1MinCandle() to build fake 5-min candle for RSI
 */

import WebSocket from 'ws';
import { EventEmitter } from 'events';

interface OHLCCandle {
  epic: string;
  resolution: 'MINUTE' | 'MINUTE_5';
  timestamp: number;  // Unix timestamp in ms
  open: number;
  high: number;
  low: number;
  close: number;
  priceType: 'bid' | 'ask';
}

interface CandleStore {
  bid: OHLCCandle | null;
  ask: OHLCCandle | null;
}

class CapitalWebSocket extends EventEmitter {
  private ws: WebSocket | null = null;
  private cst: string = '';
  private securityToken: string = '';
  private isConnected: boolean = false;
  private reconnectAttempts: number = 0;
  private maxReconnectAttempts: number = 5;
  private reconnectDelay: number = 5000;
  private pingInterval: NodeJS.Timeout | null = null;
  private subscribedEpics: Set<string> = new Set();
  
  // Store latest candles for each epic
  // Key: "EPIC:RESOLUTION" (e.g., "SOXL:MINUTE")
  private candleStore: Map<string, CandleStore> = new Map();
  
  // Store last N 1-minute candles for building fake 5-min candles
  // Key: "EPIC", Value: array of last 10 1-min candles
  private minuteHistory: Map<string, OHLCCandle[]> = new Map();
  
  private wsUrl: string = 'wss://api-streaming-capital.backend-capital.com/connect';
  
  /**
   * Connect to Capital.com WebSocket
   */
  async connect(cst: string, securityToken: string): Promise<boolean> {
    this.cst = cst;
    this.securityToken = securityToken;
    
    return new Promise((resolve) => {
      try {
        console.log('[CapitalWS] Connecting to WebSocket...');
        
        this.ws = new WebSocket(this.wsUrl);
        
        this.ws.on('open', () => {
          console.log('[CapitalWS] WebSocket connected');
          this.isConnected = true;
          this.reconnectAttempts = 0;
          this.startPingInterval();
          this.emit('connected');
          resolve(true);
        });
        
        this.ws.on('message', (data: Buffer) => {
          this.handleMessage(data.toString());
        });
        
        this.ws.on('close', (code, reason) => {
          console.log(`[CapitalWS] WebSocket closed: ${code} - ${reason}`);
          this.isConnected = false;
          this.stopPingInterval();
          this.emit('disconnected');
          this.attemptReconnect();
        });
        
        this.ws.on('error', (error) => {
          console.error('[CapitalWS] WebSocket error:', error.message);
          this.emit('error', error);
        });
        
      } catch (error: any) {
        console.error('[CapitalWS] Connection failed:', error.message);
        resolve(false);
      }
    });
  }
  
  /**
   * Subscribe to OHLC data for multiple epics
   */
  subscribeToEpics(epics: string[], resolutions: ('MINUTE' | 'MINUTE_5')[] = ['MINUTE', 'MINUTE_5']): void {
    if (!this.isConnected || !this.ws) {
      console.warn('[CapitalWS] Cannot subscribe - not connected');
      return;
    }
    
    // Filter out already subscribed epics
    const newEpics = epics.filter(e => !this.subscribedEpics.has(e));
    if (newEpics.length === 0) {
      console.log('[CapitalWS] All epics already subscribed');
      return;
    }
    
    const message = {
      destination: 'OHLCMarketData.subscribe',
      correlationId: `sub_${Date.now()}`,
      cst: this.cst,
      securityToken: this.securityToken,
      payload: {
        epics: newEpics,
        resolutions: resolutions,
        type: 'classic'
      }
    };
    
    console.log(`[CapitalWS] Subscribing to ${newEpics.length} epics: ${newEpics.join(', ')}`);
    this.ws.send(JSON.stringify(message));
    
    newEpics.forEach(epic => this.subscribedEpics.add(epic));
  }
  
  /**
   * Unsubscribe from specific epics
   */
  unsubscribeFromEpics(epics: string[]): void {
    if (!this.isConnected || !this.ws) return;
    
    const message = {
      destination: 'OHLCMarketData.unsubscribe',
      correlationId: `unsub_${Date.now()}`,
      cst: this.cst,
      securityToken: this.securityToken,
      payload: {
        epics: epics
      }
    };
    
    this.ws.send(JSON.stringify(message));
    epics.forEach(epic => this.subscribedEpics.delete(epic));
  }
  
  /**
   * Handle incoming WebSocket messages
   */
  private handleMessage(data: string): void {
    try {
      const message = JSON.parse(data);
      
      if (message.destination === 'ohlc.event') {
        // OHLC candle update
        const payload = message.payload;
        const candle: OHLCCandle = {
          epic: payload.epic,
          resolution: payload.resolution,
          timestamp: payload.t,
          open: payload.o,
          high: payload.h,
          low: payload.l,
          close: payload.c,
          priceType: payload.priceType
        };
        
        this.storeCandle(candle);
        this.emit('candle', candle);
        
      } else if (message.destination === 'OHLCMarketData.subscribe') {
        // Subscription confirmation
        console.log('[CapitalWS] Subscription confirmed:', message.payload?.subscriptions);
        
      } else if (message.status === 'OK') {
        // Generic OK response
        console.log(`[CapitalWS] ${message.destination}: OK`);
      }
      
    } catch (error: any) {
      console.error('[CapitalWS] Failed to parse message:', error.message);
    }
  }
  
  /**
   * Store candle in memory
   */
  private storeCandle(candle: OHLCCandle): void {
    const key = `${candle.epic}:${candle.resolution}`;
    
    // Get or create store for this epic:resolution
    let store = this.candleStore.get(key);
    if (!store) {
      store = { bid: null, ask: null };
      this.candleStore.set(key, store);
    }
    
    // Update bid or ask
    if (candle.priceType === 'bid') {
      store.bid = candle;
    } else {
      store.ask = candle;
    }
    
    // For MINUTE resolution, also store in history for fake 5-min candle building
    if (candle.resolution === 'MINUTE' && candle.priceType === 'bid') {
      let history = this.minuteHistory.get(candle.epic);
      if (!history) {
        history = [];
        this.minuteHistory.set(candle.epic, history);
      }
      
      // Add candle if it's a new timestamp
      const lastCandle = history[history.length - 1];
      if (!lastCandle || lastCandle.timestamp !== candle.timestamp) {
        history.push(candle);
        // Keep only last 10 candles
        if (history.length > 10) {
          history.shift();
        }
      } else {
        // Update existing candle (price updates within same minute)
        history[history.length - 1] = candle;
      }
    }
  }
  
  /**
   * Get the latest 1-minute candle for an epic
   */
  getLatest1MinCandle(epic: string): { bid: OHLCCandle | null; ask: OHLCCandle | null } {
    const bidStore = this.candleStore.get(`${epic}:MINUTE`);
    const askStore = this.candleStore.get(`${epic}:MINUTE`);
    return {
      bid: bidStore?.bid || null,
      ask: askStore?.ask || null
    };
  }
  
  /**
   * Get the latest 5-minute candle for an epic
   */
  getLatest5MinCandle(epic: string): { bid: OHLCCandle | null; ask: OHLCCandle | null } {
    const store = this.candleStore.get(`${epic}:MINUTE_5`);
    return {
      bid: store?.bid || null,
      ask: store?.ask || null
    };
  }
  
  /**
   * Get recent 1-minute candle history for building fake 5-min candle
   * Returns last N candles (default 5)
   */
  get1MinHistory(epic: string, count: number = 5): OHLCCandle[] {
    const history = this.minuteHistory.get(epic) || [];
    return history.slice(-count);
  }
  
  /**
   * Build a fake 5-minute candle from the last 4 1-minute candles
   * (T-4 to T-1 minutes before the 5-min candle closes)
   */
  buildFake5MinCandle(epic: string): OHLCCandle | null {
    const history = this.get1MinHistory(epic, 4);
    
    if (history.length < 3) {
      console.warn(`[CapitalWS] Not enough 1-min candles for ${epic}: ${history.length}`);
      return null;
    }
    
    // Build fake candle from available 1-min candles
    const fakeCandle: OHLCCandle = {
      epic,
      resolution: 'MINUTE_5',
      timestamp: history[0].timestamp,  // Start of the 5-min period
      open: history[0].open,
      high: Math.max(...history.map(c => c.high)),
      low: Math.min(...history.map(c => c.low)),
      close: history[history.length - 1].close,  // Latest 1-min close
      priceType: 'bid'
    };
    
    return fakeCandle;
  }
  
  /**
   * Get mid-price (average of bid and ask close)
   */
  getMidPrice(epic: string, resolution: 'MINUTE' | 'MINUTE_5' = 'MINUTE'): number | null {
    const store = this.candleStore.get(`${epic}:${resolution}`);
    if (!store?.bid || !store?.ask) return null;
    return (store.bid.close + store.ask.close) / 2;
  }
  
  /**
   * Start ping interval to keep connection alive
   */
  private startPingInterval(): void {
    this.pingInterval = setInterval(() => {
      if (this.ws && this.isConnected) {
        this.ws.ping();
      }
    }, 30000);  // Ping every 30 seconds
  }
  
  private stopPingInterval(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }
  
  /**
   * Attempt to reconnect after disconnect
   */
  private attemptReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error('[CapitalWS] Max reconnection attempts reached');
      this.emit('reconnect_failed');
      return;
    }
    
    this.reconnectAttempts++;
    console.log(`[CapitalWS] Attempting reconnect ${this.reconnectAttempts}/${this.maxReconnectAttempts}...`);
    
    setTimeout(() => {
      this.connect(this.cst, this.securityToken).then(success => {
        if (success && this.subscribedEpics.size > 0) {
          // Re-subscribe to all epics
          const epics = Array.from(this.subscribedEpics);
          this.subscribedEpics.clear();  // Clear so subscribeToEpics will re-add
          this.subscribeToEpics(epics);
        }
      });
    }, this.reconnectDelay);
  }
  
  /**
   * Disconnect from WebSocket
   */
  disconnect(): void {
    this.stopPingInterval();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.isConnected = false;
    this.subscribedEpics.clear();
    this.candleStore.clear();
    this.minuteHistory.clear();
  }
  
  /**
   * Check if connected
   */
  get connected(): boolean {
    return this.isConnected;
  }
  
  /**
   * Get list of subscribed epics
   */
  getSubscribedEpics(): string[] {
    return Array.from(this.subscribedEpics);
  }
}

// Singleton instance
export const capitalWebSocket = new CapitalWebSocket();

// Export types
export type { OHLCCandle };

