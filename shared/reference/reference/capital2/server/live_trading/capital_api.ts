/**
 * Capital.com API Client
 * 
 * Direct HTTP implementation of Capital.com REST API
 * Based on the legacy Python implementation using capitalcom-python library
 */

import axios, { AxiosInstance, AxiosError } from 'axios';
import { orchestrationLogger, apiTimingTracker } from '../orchestration/logger';
import { getSetting } from '../db';
import { rateLimitManager } from '../api/rate_limit_manager';

/**
 * Rate Limiter for API calls
 * Ensures minimum delay between calls to avoid rate limiting (429 errors)
 */
class ApiRateLimiter {
  private lastCallTime: number = 0;
  private minIntervalMs: number = 200; // Default 200ms (5 req/s)
  private enabled: boolean = true;
  
  /**
   * Load interval from settings
   */
  async loadInterval(): Promise<void> {
    try {
      const setting = await getSetting('system', 'api_call_interval_ms');
      if (setting?.value) {
        const interval = parseInt(setting.value, 10);
        if (interval >= 100 && interval <= 500) {
          this.minIntervalMs = interval;
          console.log(`[RateLimiter] Interval set to ${interval}ms from settings`);
        }
      }
    } catch (error) {
      console.warn('[RateLimiter] Failed to load interval from settings, using default 200ms');
    }
  }
  
  /**
   * Wait if needed to respect rate limit before making a call
   */
  async waitBeforeCall(): Promise<void> {
    if (!this.enabled) return;
    
    const now = Date.now();
    const timeSinceLastCall = now - this.lastCallTime;
    
    if (timeSinceLastCall < this.minIntervalMs) {
      const waitTime = this.minIntervalMs - timeSinceLastCall;
      await new Promise(resolve => setTimeout(resolve, waitTime));
    }
    
    this.lastCallTime = Date.now();
  }
  
  /**
   * Enable/disable rate limiting (e.g., disable for testing)
   */
  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
  }
  
  /**
   * Get current interval
   */
  getInterval(): number {
    return this.minIntervalMs;
  }
}

// Singleton rate limiter shared across all API clients
const apiRateLimiter = new ApiRateLimiter();
// Load interval on module load
apiRateLimiter.loadInterval();

interface CapitalComCredentials {
  email: string;
  password: string;
  apiKey: string;
}

interface AuthTokens {
  cst: string;
  xSecurityToken: string;
}

interface Account {
  accountId: string;
  accountName: string;
  accountType: 'SPREADBET' | 'CFD';
  preferred: boolean;
  balance: {
    balance: number;
    deposit: number;
    profitLoss: number;
    available: number;
  };
  currency: string;
  status: string;
}

interface Position {
  dealId: string;
  dealReference: string;
  epic: string;
  direction: 'BUY' | 'SELL';
  size: number;
  level: number;
  currency: string;
  createdDate: string;
  createdDateUTC: string;
  stopLevel?: number;
  limitLevel?: number;
}

interface MarketInfo {
  epic: string;
  instrument: {
    epic: string;
    name: string;
    type: string;
    marketId: string;
    lotSize: number;
    dealingRules: {
      minDealSize: {
        value: number;
        unit: string;
      };
      maxDealSize: {
        value: number;
        unit: string;
      };
      minSizeIncrement: {
        value: number;
      };
    };
    openingHours?: any;
    overnightFee?: {
      longRate: number;
      shortRate: number;
      swapChargeTimestamp: number;
      swapChargeInterval: number;
    };
  };
  snapshot: {
    bid: number;
    offer: number;
    high: number;
    low: number;
    percentageChange: number;
    updateTime: string;
    updateTimeUTC: string;
    delayTime: number;
    marketStatus: string;
  };
  dealingRules?: {
    minDealSize: {
      value: number;
      unit: string;
    };
    maxDealSize: {
      value: number;
      unit: string;
    };
  };
}

interface HistoricalPrice {
  snapshotTime: string;
  snapshotTimeUTC: string;
  openPrice: {
    bid: number;
    ask: number;
    lastTraded: number | null;
  };
  closePrice: {
    bid: number;
    ask: number;
    lastTraded: number | null;
  };
  highPrice: {
    bid: number;
    ask: number;
    lastTraded: number | null;
  };
  lowPrice: {
    bid: number;
    ask: number;
    lastTraded: number | null;
  };
  lastTradedVolume: number;
}

interface CreatePositionParams {
  epic: string;
  direction: 'BUY' | 'SELL';
  size: number;
  stopLevel?: number;
  limitLevel?: number;
  guaranteedStop?: boolean;
}

/**
 * Response from POST /positions
 * 
 * IMPORTANT: The dealReference returned here has an "o_" prefix (Order reference).
 * This is NOT the final trade ID. You must call GET /confirms/{dealReference}
 * to get the actual dealId with "p_" prefix (Position reference).
 * 
 * o_ prefix = Order (pending, not yet executed)
 * p_ prefix = Position (executed and confirmed)
 */
interface CreatePositionResponse {
  dealReference: string;  // Will have "o_" prefix (order reference)
  dealId?: string;        // May be present but unreliable until confirmed
  reason?: string;
  // Store full response for debugging
  _rawResponse?: any;
  _requestPayload?: any;
}

/**
 * Response from GET /confirms/{dealReference}
 * 
 * This is the CONFIRMED trade with actual execution details.
 * The dealReference will now have "p_" prefix (position reference).
 */
interface DealConfirmation {
  dealId: string;                              // The actual unique trade ID
  dealReference: string;                       // Will have "p_" prefix after confirmation
  dealStatus: 'ACCEPTED' | 'REJECTED' | 'PENDING';
  epic: string;
  direction: 'BUY' | 'SELL';
  size: number;
  level: number;                               // Entry price
  stopLevel?: number;
  limitLevel?: number;
  reason?: string;
  // Additional fields from Capital.com
  affectedDeals?: Array<{
    dealId: string;
    status: string;
  }>;
  // Position details (from GET /confirms response)
  position?: {
    contractSize: number;
    createdDate: string;
    createdDateUTC: string;
    dealId: string;
    dealReference: string;
    workingOrderId: string;
    size: number;
    leverage: number;
    upl: number;
    direction: string;
    level: number;
    currency: string;
    guaranteedStop: boolean;
  };
  // Store full response for debugging
  _rawResponse?: any;
}

export class CapitalComAPI {
  private baseUrl: string;
  private client: AxiosInstance;
  private tokens: AuthTokens | null = null;
  private credentials: CapitalComCredentials;
  private environment: 'demo' | 'live';
  private metrics: Map<string, number> = new Map();
  private lastError: string | null = null;
  private currentAccountId: string | null = null; // Track currently active account

  constructor(credentials: CapitalComCredentials, environment: 'demo' | 'live' = 'demo') {
    this.credentials = credentials;
    this.environment = environment;
    
    // Set base URL based on environment
    this.baseUrl = environment === 'demo'
      ? 'https://demo-api-capital.backend-capital.com/api/v1'
      : 'https://api-capital.backend-capital.com/api/v1';
    
    // Create axios instance
    this.client = axios.create({
      baseURL: this.baseUrl,
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
        'X-CAP-API-KEY': credentials.apiKey,
      },
    });

    // Add response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        if (error.response?.status === 429) {
          this.lastError = 'Rate limit exceeded (429)';
        } else {
          this.lastError = error.message;
        }
        throw error;
      }
    );
  }

  private bumpMetric(endpoint: string): void {
    const count = this.metrics.get(endpoint) || 0;
    this.metrics.set(endpoint, count + 1);
  }

  getMetrics(): Record<string, number> {
    return Object.fromEntries(this.metrics);
  }

  getLastError(): string | null {
    return this.lastError;
  }
  
  /**
   * Get Capital.com server time (NO AUTHENTICATION REQUIRED)
   * 
   * This is critical for time synchronization - our timer should align
   * with Capital.com's clock, not our local server clock.
   * 
   * @param environment - 'demo' or 'live' (both should return same time)
   * @returns Server time in milliseconds (epoch) and calculated offset
   */
  static async getServerTime(environment: 'demo' | 'live' = 'live'): Promise<{
    serverTimeMs: number;
    serverTimeISO: string;
    localTimeMs: number;
    offsetMs: number;  // Positive = local is ahead, Negative = local is behind
    latencyMs: number;
  }> {
    const baseUrl = environment === 'demo'
      ? 'https://demo-api-capital.backend-capital.com/api/v1'
      : 'https://api-capital.backend-capital.com/api/v1';
    
    const localTimeBefore = Date.now();
    
    try {
      const response = await axios.get(`${baseUrl}/time`, {
        timeout: 5000, // Short timeout for time sync
      });
      
      const localTimeAfter = Date.now();
      const latencyMs = localTimeAfter - localTimeBefore;
      
      // Capital.com returns { serverTime: 1649259764171 } (epoch milliseconds)
      const serverTimeMs = response.data.serverTime;
      
      // Use midpoint of request for more accurate comparison
      const localTimeMid = localTimeBefore + (latencyMs / 2);
      
      // Offset: how far our clock is ahead/behind Capital.com
      const offsetMs = localTimeMid - serverTimeMs;
      
      return {
        serverTimeMs,
        serverTimeISO: new Date(serverTimeMs).toISOString(),
        localTimeMs: localTimeMid,
        offsetMs: Math.round(offsetMs),
        latencyMs,
      };
    } catch (error: any) {
      console.error(`[CapitalAPI] Failed to get server time: ${error.message}`);
      // Return zero offset on error (fail-safe: use local time)
      return {
        serverTimeMs: Date.now(),
        serverTimeISO: new Date().toISOString(),
        localTimeMs: Date.now(),
        offsetMs: 0,
        latencyMs: 0,
      };
    }
  }

  /**
   * Authenticate with Capital.com API
   * Returns true if authentication successful
   */
  async authenticate(): Promise<boolean> {
    this.bumpMetric('authenticate');

    try {
      // Try authentication with rate limit handling
      for (let attempt = 0; attempt < 3; attempt++) {
        // Check rate limit BEFORE attempting auth
        const rateLimitStatus = rateLimitManager.canMakeCall('/session', 'POST', attempt === 2); // Critical on last attempt
        if (!rateLimitStatus.canMakeCall && attempt < 2) {
          console.warn(`[CapitalAPI] Auth rate limited - waiting ${rateLimitStatus.waitTimeMs}ms before attempt ${attempt + 1}`);
          await new Promise(resolve => setTimeout(resolve, rateLimitStatus.waitTimeMs));
        }
        
        // Track the call in rate limit manager
        const rateLimitCallId = rateLimitManager.startCall('/session', 'POST');
        
        try {
          const response = await this.client.post('/session', {
            identifier: this.credentials.email,
            password: this.credentials.password,
          });

          // Extract tokens from headers
          const cst = response.headers['cst'];
          const xSecurityToken = response.headers['x-security-token'];

          if (cst && xSecurityToken) {
            this.tokens = { cst, xSecurityToken };

            // Update axios instance with auth headers
            this.client.defaults.headers.common['CST'] = cst;
            this.client.defaults.headers.common['X-SECURITY-TOKEN'] = xSecurityToken;

            // Record success in rate limit manager
            rateLimitManager.endCall(rateLimitCallId, 'success', 200);
            
            console.log(`[CapitalAPI] Successfully authenticated (${this.environment})`);
            return true;
          } else {
            rateLimitManager.endCall(rateLimitCallId, 'failed', 200, 'No tokens in response');
            console.error('[CapitalAPI] Authentication failed: No tokens in response');
            return false;
          }
        } catch (error: any) {
          const httpStatus = error.response?.status;
          
          if (httpStatus === 429) {
            // Record rate limit in manager (it will handle backoff)
            rateLimitManager.endCall(rateLimitCallId, 'rate_limited', 429);
            
            // Get backoff time from settings-based rate limit manager
            const stats = rateLimitManager.getStats();
            const waitTime = stats.sessionStats.currentBackoffMs || 90000; // Fallback to 90s
            
            console.warn(`[CapitalAPI] Rate limited (429) on /session - waiting ${waitTime}ms before retry (attempt ${attempt + 1}/3)`);
            console.warn(`[CapitalAPI] NOTE: /session limit is 1 req/s per API key (from settings)`);
            
            await new Promise(resolve => setTimeout(resolve, waitTime));
            if (attempt === 2) throw error; // Last attempt
          } else {
            rateLimitManager.endCall(rateLimitCallId, 'failed', httpStatus, error.message);
            throw error;
          }
        }
      }

      return false;
    } catch (error: any) {
      this.lastError = `authenticate: ${error.message}`;
      console.error(`[CapitalAPI] Authentication error: ${error.message}`);
      if (error.response?.data) {
        console.error(`[CapitalAPI] Error details:`, JSON.stringify(error.response.data, null, 2));
      }
      return false;
    }
  }

  /**
   * Get all accounts
   */
  async getAccounts(): Promise<Account[]> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('get_accounts');
    const callId = apiTimingTracker.startCall('GET', '/accounts');
    const rateLimitCallId = rateLimitManager.startCall('/accounts', 'GET'); // Track in rate limit manager
    
    try {
      if (!this.tokens) {
        console.warn('[CapitalAPI] Not authenticated, attempting to authenticate');
        const authenticated = await this.authenticate();
        if (!authenticated) {
          apiTimingTracker.endCall(callId, 'failed', 'Not authenticated');
          rateLimitManager.endCall(rateLimitCallId, 'failed', undefined, 'Not authenticated');
          return [];
        }
      }

      // Log API call before making request
      await orchestrationLogger.logApiCall(
        'API_CALL_FIRED',
        '/accounts',
        'GET',
        {},  // No request params for GET
        undefined,
        undefined
      );

      const response = await this.client.get('/accounts');
      const accounts = response.data.accounts || [];

      // Log successful API call with complete raw response
      await orchestrationLogger.logApiCall(
        'API_CALL_SUCCESS',
        '/accounts',
        'GET',
        {},  // No request params for GET
        response.data,  // Complete raw response from Capital.com
        undefined,
        { data: { durationMs: Date.now() - (apiTimingTracker.getAllCalls().find(c => c.id === callId)?.timestamp || Date.now()) } }
      );

      apiTimingTracker.endCall(callId, 'success');
      rateLimitManager.endCall(rateLimitCallId, 'success', 200);
      return accounts;
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      const callInfo = apiTimingTracker.getAllCalls().find(c => c.id === callId);
      const duration = callInfo ? Date.now() - callInfo.timestamp : 0;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      this.lastError = `get_accounts: ${error.message}`;
      
      // Log failed API call
      await orchestrationLogger.logApiCall(
        'API_CALL_FAILED',
        '/accounts',
        'GET',
        {},
        undefined,
        error,
        { data: { durationMs: duration } }
      );

      console.error(`[CapitalAPI] Error getting accounts: ${error.message}`);
      return [];
    }
  }

  /**
   * Switch to a specific account
   * 
   * NOTE: Capital.com returns 400 if you try to switch to the already-active account.
   * We track the current account to avoid unnecessary API calls and errors.
   */
  /**
   * Get the currently tracked active account ID
   */
  getCurrentAccountId(): string | null {
    return this.currentAccountId;
  }
  
  /**
   * Switch to a different Capital.com sub-account
   * NOTE: Capital.com returns 400 if you try to switch to the already-active account.
   * We track the current account to avoid unnecessary API calls and errors.
   * 
   * IMPORTANT: The client is shared across all our accounts, so we MUST switch
   * before any operation that is account-specific (positions, orders, trades).
   */
  async switchAccount(accountId: string): Promise<boolean> {
    // Skip if already on this account (avoids 400 error from Capital.com)
    if (this.currentAccountId === accountId) {
      console.log(`[CapitalAPI] Already on account ${accountId}, skipping switch`);
      return true;
    }
    
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    console.log(`[CapitalAPI] Switching from ${this.currentAccountId || 'NONE'} to ${accountId}...`);
    this.bumpMetric('switch_account');
    const callId = apiTimingTracker.startCall('PUT', `/session (switch to ${accountId.slice(-6)})`);
    
    try {
      await this.client.put('/session', { accountId });
      const previousAccount = this.currentAccountId;
      this.currentAccountId = accountId; // Track the new active account
      apiTimingTracker.endCall(callId, 'success');
      console.log(`[CapitalAPI] ✓ Switched to account: ${accountId} (was: ${previousAccount || 'NONE'})`);
      return true;
    } catch (error: any) {
      // If we get 400, it might mean we're already on this account
      // Update our tracking and return success
      if (error.response?.status === 400) {
        console.log(`[CapitalAPI] Got 400 switching to ${accountId} - assuming already active`);
        this.currentAccountId = accountId;
        apiTimingTracker.endCall(callId, 'success'); // Still counts as success
        return true;
      }
      
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      
      // Extract Capital.com error details
      const capitalErrorCode = error.response?.data?.errorCode || 'unknown';
      const capitalErrorMessage = error.response?.data?.message || error.message;
      const httpStatus = error.response?.status || 'N/A';
      
      // Get human-readable error info
      const { getErrorInfo, getCategoryEmoji } = await import('./capital_error_codes');
      const errorInfo = getErrorInfo(capitalErrorCode);
      
      const detailedError = `[${capitalErrorCode}] ${capitalErrorMessage} (HTTP ${httpStatus})`;
      this.lastError = `switch_account: ${detailedError}`;
      
      console.error(`[CapitalAPI] === SWITCH ACCOUNT ERROR ===`);
      console.error(`[CapitalAPI] ${getCategoryEmoji(errorInfo.category)} Error Code: ${capitalErrorCode}`);
      console.error(`[CapitalAPI] Meaning: ${errorInfo.meaning}`);
      console.error(`[CapitalAPI] Target Account: ${accountId}`);
      console.error(`[CapitalAPI] HTTP Status: ${httpStatus}`);
      console.error(`[CapitalAPI] 💡 Fix: ${errorInfo.fix}`);
      
      return false;
    }
  }

  /**
   * Get all open positions
   * 
   * Capital.com returns nested structure: { position: {...}, market: {...} }
   * We flatten this to our Position interface for easier use
   */
  async getPositions(): Promise<Position[]> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('get_positions');
    const callId = apiTimingTracker.startCall('GET', '/positions');
    
    try {
      if (!this.tokens) {
        console.warn('[CapitalAPI] Not authenticated, attempting to authenticate');
        const authenticated = await this.authenticate();
        if (!authenticated) {
          apiTimingTracker.endCall(callId, 'failed', 'Not authenticated');
          return [];
        }
      }

      await orchestrationLogger.logApiCall('API_CALL_FIRED', '/positions', 'GET', {});

      const response = await this.client.get('/positions');
      const rawPositions = response.data.positions || [];

      await orchestrationLogger.logApiCall(
        'API_CALL_SUCCESS',
        '/positions',
        'GET',
        {},
        response.data,  // Complete raw response
        undefined,
        { data: { durationMs: Date.now() - (apiTimingTracker.getAllCalls().find(c => c.id === callId)?.timestamp || Date.now()) } }
      );

      apiTimingTracker.endCall(callId, 'success');

      // Capital.com returns nested: { position: {...}, market: {...} }
      // Flatten to our Position interface
      const positions: Position[] = rawPositions.map((raw: any) => ({
        dealId: raw.position?.dealId,
        dealReference: raw.position?.dealReference,
        epic: raw.market?.epic,
        direction: raw.position?.direction,
        size: raw.position?.size,
        level: raw.position?.level,
        currency: raw.position?.currency,
        createdDate: raw.position?.createdDate,
        createdDateUTC: raw.position?.createdDateUTC,
        stopLevel: raw.position?.stopLevel,
        limitLevel: raw.position?.limitLevel,
      }));

      return positions;
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      
      // Extract Capital.com error details
      const capitalErrorCode = error.response?.data?.errorCode || 'unknown';
      const capitalErrorMessage = error.response?.data?.message || error.message;
      const httpStatus = error.response?.status || 'N/A';
      
      // Get human-readable error info
      const { getErrorInfo, getCategoryEmoji } = await import('./capital_error_codes');
      const errorInfo = getErrorInfo(capitalErrorCode);
      
      const detailedError = `[${capitalErrorCode}] ${capitalErrorMessage} (HTTP ${httpStatus})`;
      this.lastError = `get_positions: ${detailedError}`;
      
      await orchestrationLogger.logApiCall(
        'API_CALL_FAILED',
        '/positions',
        'GET',
        {},
        undefined,
        error,
        { data: { 
          errorCode: capitalErrorCode,
          errorMeaning: errorInfo.meaning,
          errorFix: errorInfo.fix,
          httpStatus,
          durationMs: Date.now() - (apiTimingTracker.getAllCalls().find(c => c.id === callId)?.timestamp || Date.now()),
        } }
      );

      console.error(`[CapitalAPI] === GET POSITIONS ERROR ===`);
      console.error(`[CapitalAPI] ${getCategoryEmoji(errorInfo.category)} Error Code: ${capitalErrorCode}`);
      console.error(`[CapitalAPI] Meaning: ${errorInfo.meaning}`);
      console.error(`[CapitalAPI] 💡 Fix: ${errorInfo.fix}`);
      
      return [];
    }
  }

  /**
   * Get market information for an epic
   */
  async getMarketInfo(epic: string): Promise<MarketInfo | null> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('get_market_info');
    const callId = apiTimingTracker.startCall('GET', `/markets/${epic}`);
    
    try {
      if (!this.tokens) {
        console.warn('[CapitalAPI] Not authenticated, attempting to authenticate');
        const authenticated = await this.authenticate();
        if (!authenticated) {
          apiTimingTracker.endCall(callId, 'failed', 'Not authenticated');
          return null;
        }
      }

      const response = await this.client.get(`/markets/${epic}`);
      apiTimingTracker.endCall(callId, 'success');
      return response.data;
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      
      // Extract Capital.com error details
      const capitalErrorCode = error.response?.data?.errorCode || 'unknown';
      const capitalErrorMessage = error.response?.data?.message || error.message;
      const httpStatus = error.response?.status || 'N/A';
      
      // Get human-readable error info
      const { getErrorInfo, getCategoryEmoji } = await import('./capital_error_codes');
      const errorInfo = getErrorInfo(capitalErrorCode);
      
      const detailedError = `[${capitalErrorCode}] ${capitalErrorMessage} (HTTP ${httpStatus})`;
      this.lastError = `get_market_info: ${detailedError}`;
      
      console.error(`[CapitalAPI] === GET MARKET INFO ERROR ===`);
      console.error(`[CapitalAPI] ${getCategoryEmoji(errorInfo.category)} Error Code: ${capitalErrorCode}`);
      console.error(`[CapitalAPI] Epic: ${epic}`);
      console.error(`[CapitalAPI] Meaning: ${errorInfo.meaning}`);
      console.error(`[CapitalAPI] 💡 Fix: ${errorInfo.fix}`);
      return null;
    }
  }

  /**
   * Get historical prices
   * @param epic Market epic
   * @param resolution MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK
   * @param max Maximum number of data points (default 10, max 1000)
   */
  async getHistoricalPrices(
    epic: string,
    resolution: 'MINUTE' | 'MINUTE_5' | 'MINUTE_15' | 'MINUTE_30' | 'HOUR' | 'HOUR_4' | 'DAY' | 'WEEK',
    max: number = 200,
    from?: Date,
    to?: Date
  ): Promise<HistoricalPrice[]> {
    this.bumpMetric('get_historical_prices');
    
    try {
      if (!this.tokens) {
        console.error('[CapitalAPI] Not authenticated for historical prices');
        return [];
      }

      const cappedMax = Math.min(max, 1000);
      console.log(`[CapitalAPI] Fetching historical prices: epic=${epic}, resolution=${resolution}, max=${max} (capped=${cappedMax})`);
      console.log(`[CapitalAPI] Auth tokens present: CST=${!!this.tokens.cst}, X-SECURITY-TOKEN=${!!this.tokens.xSecurityToken}`);
      
      // Build params - from/to must be in format YYYY-MM-DDTHH:MM:SS (no Z, no milliseconds)
      const params: Record<string, any> = {
        resolution,
        max: cappedMax,
      };
      
      // Capital.com requires format: YYYY-MM-DDTHH:MM:SS (no Z, no milliseconds)
      const formatDate = (d: Date): string => {
        return d.toISOString().replace('Z', '').split('.')[0];
      };
      
      if (from) {
        params.from = formatDate(from);
        console.log(`[CapitalAPI] From: ${params.from}`);
      }
      if (to) {
        params.to = formatDate(to);
        console.log(`[CapitalAPI] To: ${params.to}`);
      }
      
      const response = await this.client.get(`/prices/${epic}`, { params });

      return response.data.prices || [];
    } catch (error: any) {
      this.lastError = `get_historical_prices: ${error.message}`;
      
      // Log detailed error information
      if (error.response) {
        console.error(`[CapitalAPI] Error getting historical prices for ${epic}:`);
        console.error(`  Status: ${error.response.status}`);
        console.error(`  Status Text: ${error.response.statusText}`);
        console.error(`  Data: ${JSON.stringify(error.response.data)}`);
        console.error(`  Headers: ${JSON.stringify(error.response.headers)}`);
      } else if (error.request) {
        console.error(`[CapitalAPI] No response received for ${epic}:`);
        console.error(`  Request: ${JSON.stringify(error.request)}`);
      } else {
        console.error(`[CapitalAPI] Error setting up request for ${epic}: ${error.message}`);
      }
      
      return [];
    }
  }

  /**
   * Create a new position (place a trade)
   * 
   * IMPORTANT: The response contains dealReference with "o_" prefix (Order reference).
   * This is NOT the confirmed trade. You MUST call getConfirmation() to get the
   * actual dealId with "p_" prefix (Position reference).
   * 
   * Workflow:
   * 1. POST /positions → Returns { dealReference: "o_xxx" } (Order created)
   * 2. GET /confirms/{dealReference} → Returns { dealId: "xxx", dealReference: "p_xxx" } (Position confirmed)
   */
  async createPosition(params: CreatePositionParams): Promise<CreatePositionResponse | null> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('create_position');
    const callId = apiTimingTracker.startCall('POST', `/positions (${params.epic})`);
    
    try {
      const payload: any = {
        epic: params.epic,
        direction: params.direction,
        size: params.size,
      };

      if (params.stopLevel !== undefined) {
        payload.stopLevel = params.stopLevel;
      }

      if (params.limitLevel !== undefined) {
        payload.limitLevel = params.limitLevel;
      }

      if (params.guaranteedStop !== undefined) {
        payload.guaranteedStop = params.guaranteedStop;
      }

      // Log full outgoing request
      console.log(`[CapitalAPI] === CREATE POSITION REQUEST ===`);
      console.log(`[CapitalAPI] Endpoint: POST /positions`);
      console.log(`[CapitalAPI] Payload: ${JSON.stringify(payload, null, 2)}`);

      await orchestrationLogger.logApiCall(
        'API_CALL_FIRED',
        '/positions',
        'POST',
        payload,
        undefined,
        undefined,
        { epic: params.epic, data: { fullPayload: payload } }
      );

      const response = await this.client.post('/positions', payload);
      const startTime = apiTimingTracker.getAllCalls().find(c => c.id === callId)?.timestamp || Date.now();
      const duration = Date.now() - startTime;

      // Log full incoming response
      console.log(`[CapitalAPI] === CREATE POSITION RESPONSE ===`);
      console.log(`[CapitalAPI] Status: ${response.status}`);
      console.log(`[CapitalAPI] Headers: ${JSON.stringify(response.headers)}`);
      console.log(`[CapitalAPI] Body: ${JSON.stringify(response.data, null, 2)}`);
      console.log(`[CapitalAPI] Duration: ${duration}ms`);
      
      // Check for o_ prefix (order reference)
      const dealRef = response.data.dealReference;
      if (dealRef && dealRef.startsWith('o_')) {
        console.log(`[CapitalAPI] ⚠️ Order reference received (o_ prefix): ${dealRef}`);
        console.log(`[CapitalAPI] ⚠️ This is NOT a confirmed trade. Call getConfirmation() to get actual dealId.`);
      } else if (dealRef && dealRef.startsWith('p_')) {
        console.log(`[CapitalAPI] ✓ Position reference received (p_ prefix): ${dealRef}`);
      }

      await orchestrationLogger.logApiCall(
        'API_CALL_SUCCESS',
        '/positions',
        'POST',
        payload,
        response.data,
        undefined,
        { 
          epic: params.epic,
          data: { 
            durationMs: duration,
            dealReference: response.data.dealReference,
            dealId: response.data.dealId,
            hasOrderPrefix: dealRef?.startsWith('o_'),
            fullResponse: response.data,
          } 
        }
      );

      apiTimingTracker.endCall(callId, 'success');

      // Return with full payloads attached for debugging
      return {
        ...response.data,
        _rawResponse: response.data,
        _requestPayload: payload,
      };
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      
      // Extract Capital.com error details
      const capitalErrorCode = error.response?.data?.errorCode || 'unknown';
      const capitalErrorMessage = error.response?.data?.message || error.message;
      const httpStatus = error.response?.status || 'N/A';
      
      // Get human-readable error info from our reference
      const { getErrorInfo, getCategoryEmoji } = await import('./capital_error_codes');
      const errorInfo = getErrorInfo(capitalErrorCode);
      
      // Create a detailed error message that includes all Capital.com info
      const detailedError = `[${capitalErrorCode}] ${capitalErrorMessage} (HTTP ${httpStatus})`;
      this.lastError = `create_position: ${detailedError}`;
      
      // Log full error details with human-readable explanation
      console.error(`[CapitalAPI] === CREATE POSITION ERROR ===`);
      console.error(`[CapitalAPI] ${getCategoryEmoji(errorInfo.category)} Error Code: ${capitalErrorCode}`);
      console.error(`[CapitalAPI] Meaning: ${errorInfo.meaning}`);
      console.error(`[CapitalAPI] Message: ${capitalErrorMessage}`);
      console.error(`[CapitalAPI] HTTP Status: ${httpStatus}`);
      console.error(`[CapitalAPI] 💡 Fix: ${errorInfo.fix}`);
      if (error.response) {
        console.error(`[CapitalAPI] Full Response: ${JSON.stringify(error.response.data, null, 2)}`);
      }
      
      await orchestrationLogger.logApiCall(
        'API_CALL_FAILED',
        '/positions',
        'POST',
        params,
        error.response?.data,
        error,
        { 
          epic: params.epic,
          data: { 
            errorStatus: httpStatus,
            errorCode: capitalErrorCode,
            errorMessage: capitalErrorMessage,
            errorMeaning: errorInfo.meaning,
            errorFix: errorInfo.fix,
            errorCategory: errorInfo.category,
            errorData: error.response?.data,
          } 
        }
      );

      // Throw a detailed error that includes the Capital.com error code and fix suggestion
      // This allows callers to extract and store the error details
      const enhancedError = new Error(detailedError);
      (enhancedError as any).capitalErrorCode = capitalErrorCode;
      (enhancedError as any).capitalErrorMessage = capitalErrorMessage;
      (enhancedError as any).httpStatus = httpStatus;
      (enhancedError as any).errorMeaning = errorInfo.meaning;
      (enhancedError as any).errorFix = errorInfo.fix;
      (enhancedError as any).errorCategory = errorInfo.category;
      throw enhancedError;
    }
  }

  /**
   * Get deal confirmation - validates that a dealReference resulted in an actual position
   * 
   * CRITICAL: This is the SECOND step in the 2-step workflow.
   * 
   * Workflow:
   * 1. POST /positions → Returns { dealReference: "o_xxx" } (Order created, pending)
   * 2. GET /confirms/{dealReference} → Returns confirmed position with actual dealId
   * 
   * The response will contain:
   * - dealId: The actual unique trade ID (use this for closing positions)
   * - dealReference: Now with "p_" prefix (Position confirmed)
   * - dealStatus: 'ACCEPTED', 'REJECTED', or 'PENDING'
   * - level: The actual entry price
   * - size: The actual position size
   * 
   * @param dealReference - The o_ reference returned from createPosition
   * @returns Deal confirmation with actual dealId, or null if not found/rejected
   */
  async getConfirmation(dealReference: string): Promise<DealConfirmation | null> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('get_confirmation');
    const callId = apiTimingTracker.startCall('GET', `/confirms/${dealReference.slice(-8)}`);
    
    try {
      console.log(`[CapitalAPI] === GET CONFIRMATION REQUEST ===`);
      console.log(`[CapitalAPI] Endpoint: GET /confirms/${dealReference}`);
      console.log(`[CapitalAPI] Input dealReference prefix: ${dealReference.substring(0, 2)}`);
      
      await orchestrationLogger.logApiCall(
        'API_CALL_FIRED',
        `/confirms/${dealReference}`,
        'GET',
        { dealReference },
        undefined,
        undefined,
        { data: { inputDealReference: dealReference } }
      );

      const response = await this.client.get(`/confirms/${dealReference}`);
      const startTime = apiTimingTracker.getAllCalls().find(c => c.id === callId)?.timestamp || Date.now();
      const duration = Date.now() - startTime;

      // Log full response
      console.log(`[CapitalAPI] === GET CONFIRMATION RESPONSE ===`);
      console.log(`[CapitalAPI] Status: ${response.status}`);
      console.log(`[CapitalAPI] Body: ${JSON.stringify(response.data, null, 2)}`);
      console.log(`[CapitalAPI] Duration: ${duration}ms`);
      
      // Check for p_ prefix (confirmed position)
      const confirmedRef = response.data.dealReference;
      const dealId = response.data.dealId;
      const dealStatus = response.data.dealStatus;
      
      if (dealStatus === 'ACCEPTED') {
        console.log(`[CapitalAPI] ✓ Trade ACCEPTED`);
        console.log(`[CapitalAPI] ✓ dealId: ${dealId}`);
        console.log(`[CapitalAPI] ✓ dealReference: ${confirmedRef} (${confirmedRef?.startsWith('p_') ? 'position' : 'unknown'})`);
        console.log(`[CapitalAPI] ✓ Entry price (level): ${response.data.level}`);
        console.log(`[CapitalAPI] ✓ Size: ${response.data.size}`);
      } else if (dealStatus === 'REJECTED') {
        const rejectReason = response.data.rejectReason || response.data.reason || 'Unknown';
        console.log(`[CapitalAPI] ✗ Trade REJECTED`);
        console.log(`[CapitalAPI] ✗ Reject Reason: ${rejectReason}`);
        console.log(`[CapitalAPI] ✗ Deal Reference: ${dealReference}`);
        console.log(`[CapitalAPI] ✗ Size attempted: ${response.data.size}`);
      } else if (dealStatus === 'PENDING') {
        console.log(`[CapitalAPI] ⏳ Trade PENDING - may need to poll again`);
      }

      await orchestrationLogger.logApiCall(
        'API_CALL_SUCCESS',
        `/confirms/${dealReference}`,
        'GET',
        { dealReference },
        response.data,
        undefined,
        { 
          data: { 
            durationMs: duration,
            dealId,
            confirmedDealReference: confirmedRef,
            dealStatus,
            level: response.data.level,
            size: response.data.size,
            hasPositionPrefix: confirmedRef?.startsWith('p_'),
            fullResponse: response.data,
          } 
        }
      );

      apiTimingTracker.endCall(callId, 'success');

      // Return with full response attached
      return {
        ...response.data,
        _rawResponse: response.data,
      };
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      this.lastError = `get_confirmation: ${error.message}`;
      
      console.error(`[CapitalAPI] === GET CONFIRMATION ERROR ===`);
      console.error(`[CapitalAPI] dealReference: ${dealReference}`);
      console.error(`[CapitalAPI] Error: ${error.message}`);
      if (error.response) {
        console.error(`[CapitalAPI] Status: ${error.response.status}`);
        console.error(`[CapitalAPI] Response: ${JSON.stringify(error.response.data, null, 2)}`);
      }
      
      await orchestrationLogger.logApiCall(
        'API_CALL_FAILED',
        `/confirms/${dealReference}`,
        'GET',
        { dealReference },
        error.response?.data,
        error,
        { 
          data: { 
            durationMs: duration,
            errorStatus: error.response?.status,
            errorData: error.response?.data,
          } 
        }
      );

      return null;
    }
  }

  /**
   * Close a position
   */
  async closePosition(dealId: string): Promise<boolean> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('close_position');
    const callId = apiTimingTracker.startCall('DELETE', `/positions/${dealId.slice(-8)}`);
    
    try {
      await orchestrationLogger.logApiCall(
        'API_CALL_FIRED',
        `/positions/${dealId}`,
        'DELETE',
        { dealId },
        undefined,
        undefined,
        { data: { dealId } }
      );

      await this.client.delete(`/positions/${dealId}`);
      const startTime = apiTimingTracker.getAllCalls().find(c => c.id === callId)?.timestamp || Date.now();
      const duration = Date.now() - startTime;

      await orchestrationLogger.logApiCall(
        'API_CALL_SUCCESS',
        `/positions/${dealId}`,
        'DELETE',
        { dealId },
        { success: true },
        undefined,
        { data: { durationMs: duration, dealId } }
      );

      apiTimingTracker.endCall(callId, 'success');
      console.log(`[CapitalAPI] Position closed: ${dealId}`);
      return true;
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      
      // Extract Capital.com error details
      const capitalErrorCode = error.response?.data?.errorCode || 'unknown';
      const capitalErrorMessage = error.response?.data?.message || error.message;
      const httpStatus = error.response?.status || 'N/A';
      
      // Get human-readable error info
      const { getErrorInfo, getCategoryEmoji } = await import('./capital_error_codes');
      const errorInfo = getErrorInfo(capitalErrorCode);
      
      const detailedError = `[${capitalErrorCode}] ${capitalErrorMessage} (HTTP ${httpStatus})`;
      this.lastError = `close_position: ${detailedError}`;
      
      await orchestrationLogger.logApiCall(
        'API_CALL_FAILED',
        `/positions/${dealId}`,
        'DELETE',
        { dealId },
        undefined,
        error,
        { data: { 
          dealId,
          errorCode: capitalErrorCode,
          errorMessage: capitalErrorMessage,
          errorMeaning: errorInfo.meaning,
          errorFix: errorInfo.fix,
          httpStatus,
        } }
      );

      console.error(`[CapitalAPI] === CLOSE POSITION ERROR ===`);
      console.error(`[CapitalAPI] ${getCategoryEmoji(errorInfo.category)} Error Code: ${capitalErrorCode}`);
      console.error(`[CapitalAPI] Meaning: ${errorInfo.meaning}`);
      console.error(`[CapitalAPI] Message: ${capitalErrorMessage}`);
      console.error(`[CapitalAPI] Deal ID: ${dealId}`);
      console.error(`[CapitalAPI] 💡 Fix: ${errorInfo.fix}`);
      
      return false;
    }
  }

  /**
   * Get working (pending) orders
   * Returns orders that have not been filled yet
   */
  async getWorkingOrders(): Promise<Array<{
    dealId: string;
    epic: string;
    direction: 'BUY' | 'SELL';
    size: number;
    level: number;
    status: string;
    createdDateUTC: string;
  }> | null> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('get_working_orders');
    const callId = apiTimingTracker.startCall('GET', '/workingorders');
    
    try {
      console.log('[CapitalAPI] === GET WORKING ORDERS ===');
      console.log('[CapitalAPI] Endpoint: GET /workingorders');
      
      const response = await this.client.get('/workingorders');
      const startTime = apiTimingTracker.getAllCalls().find(c => c.id === callId)?.timestamp || Date.now();
      const duration = Date.now() - startTime;
      
      console.log('[CapitalAPI] === GET WORKING ORDERS RESPONSE ===');
      console.log(`[CapitalAPI] Status: 200`);
      console.log(`[CapitalAPI] Body: ${JSON.stringify(response.data, null, 2)}`);
      console.log(`[CapitalAPI] Duration: ${duration}ms`);
      
      apiTimingTracker.endCall(callId, 'success');
      return response.data?.workingOrders || [];
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      this.lastError = `get_working_orders: ${error.message}`;
      console.error(`[CapitalAPI] Error getting working orders: ${error.message}`);
      return null;
    }
  }
  
  /**
   * Cancel a working (pending) order
   * Use this to clean up orders that didn't fill - we don't want pending orders!
   */
  async cancelOrder(dealId: string): Promise<boolean> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('cancel_order');
    const callId = apiTimingTracker.startCall('DELETE', `/workingorders/${dealId.slice(-8)}`);
    
    try {
      console.log('[CapitalAPI] === CANCEL ORDER ===');
      console.log(`[CapitalAPI] Endpoint: DELETE /workingorders/${dealId}`);
      
      await this.client.delete(`/workingorders/${dealId}`);
      
      apiTimingTracker.endCall(callId, 'success');
      console.log(`[CapitalAPI] ✓ Order cancelled: ${dealId}`);
      return true;
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      this.lastError = `cancel_order: ${error.message}`;
      console.error(`[CapitalAPI] ✗ Error cancelling order ${dealId}: ${error.message}`);
      return false;
    }
  }
  
  /**
   * Cancel all working orders for a specific epic
   * Used during close sequence to ensure no pending orders exist
   */
  async cancelAllOrdersForEpic(epic: string): Promise<{ cancelled: number; errors: string[] }> {
    const result = { cancelled: 0, errors: [] as string[] };
    
    try {
      const orders = await this.getWorkingOrders();
      if (!orders) {
        result.errors.push('Failed to fetch working orders');
        return result;
      }
      
      const epicOrders = orders.filter(o => o.epic === epic);
      console.log(`[CapitalAPI] Found ${epicOrders.length} pending orders for ${epic}`);
      
      for (const order of epicOrders) {
        const success = await this.cancelOrder(order.dealId);
        if (success) {
          result.cancelled++;
        } else {
          result.errors.push(`Failed to cancel order ${order.dealId}`);
        }
      }
      
      return result;
    } catch (error: any) {
      result.errors.push(`Error cancelling orders for ${epic}: ${error.message}`);
      return result;
    }
  }

  /**
   * Update position stop/limit levels
   */
  async updatePosition(dealId: string, stopLevel?: number, limitLevel?: number): Promise<boolean> {
    this.bumpMetric('update_position');
    
    try {
      const payload: any = {};
      if (stopLevel !== undefined) payload.stopLevel = stopLevel;
      if (limitLevel !== undefined) payload.limitLevel = limitLevel;

      await this.client.put(`/positions/${dealId}`, payload);
      console.log(`[CapitalAPI] Position updated: ${dealId}`);
      return true;
    } catch (error: any) {
      this.lastError = `update_position: ${error.message}`;
      console.error(`[CapitalAPI] Error updating position: ${error.message}`);
      return false;
    }
  }
  
  /**
   * Partial close a position
   * 
   * Capital.com API doesn't have a direct partial close endpoint.
   * Strategy: Place an opposite direction trade for the portion to close.
   * This reduces the net position without closing and reopening (saves fees).
   * 
   * Example: If you have 100 contracts LONG and want to close 49:
   *   - Place a SELL order for 49 contracts
   *   - Net position becomes 51 contracts LONG
   *   - Original dealId remains valid for the remaining position
   * 
   * @param dealId - The deal ID of the position (for reference/logging)
   * @param closeSize - The number of contracts to close
   * @param currentPosition - Current position details
   * @returns Object with success status (dealId unchanged for remaining position)
   */
  async partialClosePosition(
    dealId: string,
    closeSize: number,
    currentPosition: {
      epic: string;
      direction: 'BUY' | 'SELL';
      size: number;
      stopLevel?: number;
      limitLevel?: number;
    }
  ): Promise<{ success: boolean; closedSize: number; remainingSize: number }> {
    this.bumpMetric('partial_close_position');
    
    try {
      const remainingSize = currentPosition.size - closeSize;
      
      if (remainingSize <= 0) {
        // Close the full position using the standard close endpoint
        console.log(`[CapitalAPI] Partial close requested ${closeSize} >= position size ${currentPosition.size}, closing fully`);
        const closed = await this.closePosition(dealId);
        return { success: closed, closedSize: currentPosition.size, remainingSize: 0 };
      }
      
      console.log(`[CapitalAPI] Partial close: Closing ${closeSize} of ${currentPosition.size} contracts`);
      console.log(`[CapitalAPI] Position will remain at ${remainingSize} contracts (no reopen needed)`);
      
      // Place opposite direction trade to reduce position
      // If current is BUY (long), we SELL to reduce
      // If current is SELL (short), we BUY to reduce
      const oppositeDirection = currentPosition.direction === 'BUY' ? 'SELL' : 'BUY';
      
      const closeOrder = await this.createPosition({
        epic: currentPosition.epic,
        direction: oppositeDirection,
        size: closeSize,
        // No stop/limit on the closing trade
      });
      
      if (!closeOrder) {
        console.error(`[CapitalAPI] Partial close: Failed to place ${oppositeDirection} order for ${closeSize} contracts`);
        return { success: false, closedSize: 0, remainingSize: currentPosition.size };
      }
      
      console.log(`[CapitalAPI] Partial close complete: Closed ${closeSize} contracts, ${remainingSize} remaining`);
      console.log(`[CapitalAPI] Original dealId ${dealId} still valid for remaining position`);
      
      return { 
        success: true, 
        closedSize: closeSize,
        remainingSize: remainingSize,
      };
      
    } catch (error: any) {
      this.lastError = `partial_close_position: ${error.message}`;
      console.error(`[CapitalAPI] Error in partial close: ${error.message}`);
      return { success: false, closedSize: 0, remainingSize: currentPosition.size };
    }
  }

  /**
   * Leverage setting for an asset class
   */
  

  /**
   * Get account preferences including leverage settings
   * 
   * Returns the current leverage settings per asset class.
   * Each asset class has:
   * - current: The current leverage value
   * - available: Array of valid leverage values
   */
  async getAccountPreferences(): Promise<{
    leverages: {
      SHARES?: { current: number; available: number[] };
      CURRENCIES?: { current: number; available: number[] };
      INDICES?: { current: number; available: number[] };
      CRYPTOCURRENCIES?: { current: number; available: number[] };
      COMMODITIES?: { current: number; available: number[] };
      INTEREST_RATES?: { current: number; available: number[] };
      BONDS?: { current: number; available: number[] };
    };
    hedgingMode?: boolean;
  } | null> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('get_account_preferences');
    const callId = apiTimingTracker.startCall('GET', '/accounts/preferences');
    
    try {
      if (!this.tokens) {
        console.warn('[CapitalAPI] Not authenticated, attempting to authenticate');
        const authenticated = await this.authenticate();
        if (!authenticated) {
          apiTimingTracker.endCall(callId, 'failed', 'Not authenticated');
          return null;
        }
      }

      console.log('[CapitalAPI] === GET ACCOUNT PREFERENCES ===');
      console.log('[CapitalAPI] Endpoint: GET /accounts/preferences');

      const response = await this.client.get('/accounts/preferences');
      const startTime = apiTimingTracker.getAllCalls().find(c => c.id === callId)?.timestamp || Date.now();
      const duration = Date.now() - startTime;

      console.log('[CapitalAPI] === GET ACCOUNT PREFERENCES RESPONSE ===');
      console.log('[CapitalAPI] Status: 200');
      console.log(`[CapitalAPI] Body: ${JSON.stringify(response.data, null, 2)}`);
      console.log(`[CapitalAPI] Duration: ${duration}ms`);

      apiTimingTracker.endCall(callId, 'success');
      return response.data;
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      this.lastError = `get_account_preferences: ${error.message}`;
      
      console.error(`[CapitalAPI] Error getting account preferences: ${error.message}`);
      
      return null;
    }
  }

  /**
   * Set account leverage for specific asset classes
   * 
   * IMPORTANT: This sets leverage at the ACCOUNT level, not per-trade.
   * All trades for the specified asset class will use this leverage.
   * 
   * @param leverages - Object with asset class leverage values
   * @param hedgingMode - Optional hedging mode setting
   * @returns true if successful, false otherwise
   * 
   * Example:
   *   await setAccountLeverage({ SHARES: 5 }); // Set shares leverage to 5x
   */
  async setAccountLeverage(
    leverages: {
      SHARES?: number;
      CURRENCIES?: number;
      INDICES?: number;
      CRYPTOCURRENCIES?: number;
      COMMODITIES?: number;
    },
    hedgingMode?: boolean
  ): Promise<boolean> {
    await apiRateLimiter.waitBeforeCall(); // Enforce rate limit
    this.bumpMetric('set_account_leverage');
    const callId = apiTimingTracker.startCall('PUT', '/accounts/preferences (leverage)');
    
    try {
      if (!this.tokens) {
        console.warn('[CapitalAPI] Not authenticated, attempting to authenticate');
        const authenticated = await this.authenticate();
        if (!authenticated) {
          apiTimingTracker.endCall(callId, 'failed', 'Not authenticated');
          return false;
        }
      }

      const payload: any = { leverages };
      if (hedgingMode !== undefined) {
        payload.hedgingMode = hedgingMode;
      }

      console.log('[CapitalAPI] === SET ACCOUNT LEVERAGE ===');
      console.log('[CapitalAPI] Endpoint: PUT /accounts/preferences');
      console.log(`[CapitalAPI] Payload: ${JSON.stringify(payload, null, 2)}`);

      await orchestrationLogger.logApiCall(
        'API_CALL_FIRED',
        '/accounts/preferences',
        'PUT',
        payload,
        undefined,
        undefined,
        { data: { leverages } }
      );

      const response = await this.client.put('/accounts/preferences', payload);
      const startTime = apiTimingTracker.getAllCalls().find(c => c.id === callId)?.timestamp || Date.now();
      const duration = Date.now() - startTime;

      console.log('[CapitalAPI] === SET ACCOUNT LEVERAGE RESPONSE ===');
      console.log('[CapitalAPI] Status: 200');
      console.log(`[CapitalAPI] Body: ${JSON.stringify(response.data, null, 2)}`);
      console.log(`[CapitalAPI] Duration: ${duration}ms`);
      console.log(`[CapitalAPI] ✓ Leverage updated successfully`);

      await orchestrationLogger.logApiCall(
        'API_CALL_SUCCESS',
        '/accounts/preferences',
        'PUT',
        payload,
        response.data,
        undefined,
        { data: { durationMs: duration, leverages } }
      );

      apiTimingTracker.endCall(callId, 'success');
      return true;
    } catch (error: any) {
      const isRateLimited = error.response?.status === 429;
      apiTimingTracker.endCall(callId, isRateLimited ? 'rate_limited' : 'failed', error.message);
      
      // Extract Capital.com error details
      const capitalErrorCode = error.response?.data?.errorCode || 'unknown';
      const capitalErrorMessage = error.response?.data?.message || error.message;
      const httpStatus = error.response?.status || 'N/A';
      
      // Get human-readable error info
      const { getErrorInfo, getCategoryEmoji } = await import('./capital_error_codes');
      const errorInfo = getErrorInfo(capitalErrorCode);
      
      const detailedError = `[${capitalErrorCode}] ${capitalErrorMessage} (HTTP ${httpStatus})`;
      this.lastError = `set_account_leverage: ${detailedError}`;
      
      console.error('[CapitalAPI] === SET ACCOUNT LEVERAGE ERROR ===');
      console.error(`[CapitalAPI] ${getCategoryEmoji(errorInfo.category)} Error Code: ${capitalErrorCode}`);
      console.error(`[CapitalAPI] Meaning: ${errorInfo.meaning}`);
      console.error(`[CapitalAPI] Message: ${capitalErrorMessage}`);
      console.error(`[CapitalAPI] HTTP Status: ${httpStatus}`);
      console.error(`[CapitalAPI] Leverages requested: ${JSON.stringify(leverages)}`);
      console.error(`[CapitalAPI] 💡 Fix: ${errorInfo.fix}`);

      await orchestrationLogger.logApiCall(
        'API_CALL_FAILED',
        '/accounts/preferences',
        'PUT',
        { leverages },
        undefined,
        error,
        { data: { 
          errorCode: capitalErrorCode,
          errorMessage: capitalErrorMessage,
          errorMeaning: errorInfo.meaning,
          errorFix: errorInfo.fix,
          httpStatus,
          leveragesRequested: leverages,
          errorData: error.response?.data,
        } }
      );

      return false;
    }
  }

  /**
   * Convenience method to set leverage for SHARES (stocks like SOXL, TECL)
   * 
   * @param leverage - The leverage value (e.g., 2, 4, 5)
   * @returns true if successful
   */
  async setSharesLeverage(leverage: number): Promise<boolean> {
    console.log(`[CapitalAPI] Setting SHARES leverage to ${leverage}x`);
    return this.setAccountLeverage({ SHARES: leverage });
  }

  /**
   * Get available leverage options for a specific instrument type
   * 
   * @param instrumentType - The instrument type (SHARES, CURRENCIES, INDICES, etc.)
   * @returns Array of valid leverage values, or empty array if unable to fetch
   */
  async getLeverageOptionsForType(instrumentType: string): Promise<number[]> {
    const prefs = await this.getAccountPreferences();
    if (!prefs?.leverages) {
      return [];
    }
    
    const typeKey = instrumentType.toUpperCase() as keyof typeof prefs.leverages;
    const typePrefs = prefs.leverages[typeKey];
    
    if (!typePrefs) {
      console.warn(`[CapitalAPI] No leverage options found for type: ${instrumentType}`);
      return [];
    }
    
    return typePrefs.available || [];
  }

  /**
   * Get current SHARES leverage for the active account
   * 
   * @returns Current leverage value, or null if unable to fetch
   */
  async getSharesLeverage(): Promise<number | null> {
    const prefs = await this.getAccountPreferences();
    if (!prefs?.leverages?.SHARES) {
      return null;
    }
    return prefs.leverages.SHARES.current;
  }

  /**
   * Get available SHARES leverage options for the active account
   * 
   * @returns Array of valid leverage values, or empty array if unable to fetch
   */
  async getAvailableSharesLeverages(): Promise<number[]> {
    const prefs = await this.getAccountPreferences();
    if (!prefs?.leverages?.SHARES) {
      return [];
    }
    return prefs.leverages.SHARES.available;
  }

  /**
   * Ensure account leverage matches the required leverage
   * 
   * This should be called BEFORE placing trades to ensure the account's
   * leverage setting matches what the strategy expects.
   * 
   * @param requiredLeverage - The leverage the strategy needs
   * @returns Object with success status and actual leverage
   */
  async ensureSharesLeverage(requiredLeverage: number): Promise<{
    success: boolean;
    currentLeverage: number | null;
    wasUpdated: boolean;
    error?: string;
  }> {
    // Get current leverage
    const currentLeverage = await this.getSharesLeverage();
    
    if (currentLeverage === null) {
      return {
        success: false,
        currentLeverage: null,
        wasUpdated: false,
        error: 'Failed to get current leverage',
      };
    }
    
    // If already correct, no action needed
    if (currentLeverage === requiredLeverage) {
      console.log(`[CapitalAPI] SHARES leverage already at ${requiredLeverage}x`);
      return {
        success: true,
        currentLeverage,
        wasUpdated: false,
      };
    }
    
    // Check if required leverage is available
    const available = await this.getAvailableSharesLeverages();
    if (!available.includes(requiredLeverage)) {
      console.warn(`[CapitalAPI] Required leverage ${requiredLeverage}x not available. Options: ${available.join(', ')}`);
      return {
        success: false,
        currentLeverage,
        wasUpdated: false,
        error: `Leverage ${requiredLeverage}x not available. Options: ${available.join(', ')}`,
      };
    }
    
    // Update leverage
    console.log(`[CapitalAPI] Updating SHARES leverage from ${currentLeverage}x to ${requiredLeverage}x`);
    const updated = await this.setSharesLeverage(requiredLeverage);
    
    if (!updated) {
      return {
        success: false,
        currentLeverage,
        wasUpdated: false,
        error: 'Failed to set leverage',
      };
    }
    
    // Verify the change
    const newLeverage = await this.getSharesLeverage();
    
    return {
      success: newLeverage === requiredLeverage,
      currentLeverage: newLeverage,
      wasUpdated: true,
    };
  }

  /**
   * Keepalive - ping the API to keep session alive
   */
  async keepalive(): Promise<boolean> {
    this.bumpMetric('keepalive');
    
    try {
      await this.client.get('/ping');
      console.log('[CapitalAPI] Keepalive OK');
      return true;
    } catch (error: any) {
      this.lastError = `keepalive: ${error.message}`;
      console.warn(`[CapitalAPI] Keepalive failed: ${error.message}`);
      return false;
    }
  }

  /**
   * Get transaction history (financial transactions like deposits, PnL)
   */
  async getTransactionHistory(fromDate: string, toDate: string): Promise<any[]> {
    this.bumpMetric('get_transaction_history');
    
    try {
      if (!this.tokens) {
        console.warn('[CapitalAPI] Not authenticated for transaction history');
        return [];
      }

      const response = await this.client.get('/history/transactions', {
        params: {
          from: fromDate,
          to: toDate,
        },
      });

      return response.data.transactions || [];
    } catch (error: any) {
      this.lastError = `get_transaction_history: ${error.message}`;
      console.error(`[CapitalAPI] Error getting transaction history: ${error.message}`);
      return [];
    }
  }

  /**
   * Get activity history (trades opened/closed with source info)
   * This is the key endpoint for syncing ALL trades including manual ones
   * 
   * Response includes:
   * - date, dateUTC: timestamp
   * - epic: instrument traded
   * - dealId: position deal ID
   * - source: USER (manual) | SYSTEM (stoploss/auto)
   * - type: action type
   * - details: trade details including direction, size, level, etc.
   */
  async getActivityHistory(fromDate: string, toDate: string): Promise<any[]> {
    this.bumpMetric('get_activity_history');
    
    try {
      if (!this.tokens) {
        console.warn('[CapitalAPI] Not authenticated for activity history');
        return [];
      }

      console.log(`[CapitalAPI] Fetching activity history from ${fromDate} to ${toDate}`);
      
      const response = await this.client.get('/history/activity', {
        params: {
          from: fromDate,
          to: toDate,
          detailed: true, // Get full trade details
        },
      });

      console.log(`[CapitalAPI] Activity history: ${response.data.activities?.length || 0} activities found`);
      return response.data.activities || [];
    } catch (error: any) {
      this.lastError = `get_activity_history: ${error.message}`;
      console.error(`[CapitalAPI] Error getting activity history: ${error.message}`);
      return [];
    }
  }

  /**
   * Search for markets by keyword
   * @param searchTerm The search term to find markets
   * @returns Object with markets array
   */
  async searchMarkets(searchTerm: string): Promise<{ markets: any[] } | null> {
    this.bumpMetric('search_markets');
    
    try {
      if (!this.tokens) {
        console.warn('[CapitalAPI] Not authenticated, attempting to authenticate');
        await this.authenticate();
      }
      
      console.log(`[CapitalAPI] Searching markets for: ${searchTerm}`);
      const response = await this.client.get('/markets', {
        params: { searchTerm }
      });
      
      return response.data;
    } catch (error: any) {
      console.error(`[CapitalAPI] Error searching markets: ${error.message}`);
      return null;
    }
  }

  /**
   * Logout and clear session
   */
  async logout(): Promise<void> {
    try {
      if (this.tokens) {
        await this.client.delete('/session');
        this.tokens = null;
        delete this.client.defaults.headers.common['CST'];
        delete this.client.defaults.headers.common['X-SECURITY-TOKEN'];
        console.log('[CapitalAPI] Logged out successfully');
      }
    } catch (error: any) {
      console.error(`[CapitalAPI] Error during logout: ${error.message}`);
    }
  }

  /**
   * Get authentication tokens for WebSocket connection
   * Returns CST and X-SECURITY-TOKEN needed for WebSocket auth
   */
  getAuthTokens(): { cst: string; securityToken: string } | null {
    if (!this.tokens) return null;
    return {
      cst: this.tokens.cst,
      securityToken: this.tokens.xSecurityToken
    };
  }

  /**
   * Check if authenticated
   */
  isAuthenticated(): boolean {
    return !!this.tokens;
  }
}

