/**
 * Notification Service - Alerts and notifications for trading events
 * 
 * This module provides:
 * 1. Console logging with severity levels
 * 2. Database logging for audit trail
 * 3. Placeholder for future integrations (email, Telegram, Discord, etc.)
 * 
 * Event types:
 * - TRADE_EXECUTED: Trade successfully opened
 * - TRADE_CLOSED: Trade successfully closed
 * - TRADE_FAILED: Trade execution failed
 * - LEVERAGE_CHANGED: Leverage modified
 * - SIMULATION_STARTED/COMPLETED: Simulation lifecycle
 * - ERROR: Any critical error
 * - WARNING: Non-critical issues
 * - RATE_LIMIT: API rate limit warnings
 */

import { orchestrationLogger } from './logger';

/**
 * Notification severity levels
 */
export type NotificationSeverity = 'info' | 'warning' | 'error' | 'critical';

/**
 * Notification event types
 */
export type NotificationEventType = 
  | 'TRADE_EXECUTED'
  | 'TRADE_CLOSED'
  | 'TRADE_FAILED'
  | 'TRADE_STOPPED_OUT'
  | 'LEVERAGE_CHANGED'
  | 'SIMULATION_STARTED'
  | 'SIMULATION_COMPLETED'
  | 'SIMULATION_FAILED'
  | 'BRAIN_DECISION'
  | 'REBALANCE_EXECUTED'
  | 'RATE_LIMIT_WARNING'
  | 'API_ERROR'
  | 'DATABASE_ERROR'
  | 'SYSTEM_ERROR'
  | 'QUIET_PERIOD_STARTED'
  | 'QUIET_PERIOD_ENDED';

/**
 * Notification payload
 */
interface NotificationPayload {
  eventType: NotificationEventType;
  severity: NotificationSeverity;
  title: string;
  message: string;
  accountId?: number;
  accountType?: 'demo' | 'live';
  tradeId?: number;
  epic?: string;
  pnl?: number;
  details?: Record<string, any>;
  timestamp: Date;
}

/**
 * Notification handler type
 */
type NotificationHandler = (payload: NotificationPayload) => Promise<void>;

/**
 * Notification Service class
 */
class NotificationService {
  private handlers: NotificationHandler[] = [];
  private recentNotifications: NotificationPayload[] = [];
  private maxRecentNotifications = 100;
  
  constructor() {
    // Register default console handler
    this.registerHandler(this.consoleHandler.bind(this));
    
    // Register database logger handler
    this.registerHandler(this.databaseHandler.bind(this));
  }
  
  /**
   * Register a notification handler
   */
  registerHandler(handler: NotificationHandler): void {
    this.handlers.push(handler);
  }
  
  /**
   * Send a notification
   */
  async notify(
    eventType: NotificationEventType,
    severity: NotificationSeverity,
    title: string,
    message: string,
    options: {
      accountId?: number;
      accountType?: 'demo' | 'live';
      tradeId?: number;
      epic?: string;
      pnl?: number;
      details?: Record<string, any>;
    } = {}
  ): Promise<void> {
    const payload: NotificationPayload = {
      eventType,
      severity,
      title,
      message,
      ...options,
      timestamp: new Date(),
    };
    
    // Store in recent notifications
    this.recentNotifications.unshift(payload);
    if (this.recentNotifications.length > this.maxRecentNotifications) {
      this.recentNotifications.pop();
    }
    
    // Send to all handlers
    for (const handler of this.handlers) {
      try {
        await handler(payload);
      } catch (error) {
        console.error('[NotificationService] Handler error:', error);
      }
    }
  }
  
  /**
   * Console handler - logs to console with formatting
   */
  private async consoleHandler(payload: NotificationPayload): Promise<void> {
    const icon = this.getSeverityIcon(payload.severity);
    const accountLabel = payload.accountType === 'live' ? '🔴 LIVE' : '🟢 DEMO';
    const accountInfo = payload.accountId ? ` [${accountLabel} Account ${payload.accountId}]` : '';
    
    const logLine = `${icon} ${payload.title}${accountInfo}: ${payload.message}`;
    
    switch (payload.severity) {
      case 'critical':
        console.error(`\n${'!'.repeat(60)}`);
        console.error(logLine);
        console.error(`${'!'.repeat(60)}\n`);
        break;
      case 'error':
        console.error(logLine);
        break;
      case 'warning':
        console.warn(logLine);
        break;
      default:
        console.log(logLine);
    }
    
    if (payload.details && Object.keys(payload.details).length > 0) {
      console.log('  Details:', JSON.stringify(payload.details, null, 2));
    }
  }
  
  /**
   * Database handler - logs to orchestration logger
   */
  private async databaseHandler(payload: NotificationPayload): Promise<void> {
    const logMethod = payload.severity === 'error' || payload.severity === 'critical'
      ? orchestrationLogger.error.bind(orchestrationLogger)
      : payload.severity === 'warning'
        ? orchestrationLogger.warn.bind(orchestrationLogger)
        : orchestrationLogger.info.bind(orchestrationLogger);
    
    await logMethod(
      payload.eventType,
      `${payload.title}: ${payload.message}`,
      {
        accountId: payload.accountId,
        epic: payload.epic,
        data: {
          severity: payload.severity,
          accountType: payload.accountType,
          tradeId: payload.tradeId,
          pnl: payload.pnl,
          ...payload.details,
        },
      }
    );
  }
  
  /**
   * Get severity icon
   */
  private getSeverityIcon(severity: NotificationSeverity): string {
    switch (severity) {
      case 'critical': return '🚨';
      case 'error': return '❌';
      case 'warning': return '⚠️';
      default: return 'ℹ️';
    }
  }
  
  /**
   * Get recent notifications
   */
  getRecentNotifications(limit: number = 20): NotificationPayload[] {
    return this.recentNotifications.slice(0, limit);
  }
  
  /**
   * Get notifications by severity
   */
  getNotificationsBySeverity(severity: NotificationSeverity): NotificationPayload[] {
    return this.recentNotifications.filter(n => n.severity === severity);
  }
  
  /**
   * Get notifications by event type
   */
  getNotificationsByType(eventType: NotificationEventType): NotificationPayload[] {
    return this.recentNotifications.filter(n => n.eventType === eventType);
  }
  
  /**
   * Clear recent notifications
   */
  clearNotifications(): void {
    this.recentNotifications = [];
  }
  
  // ============================================
  // Convenience methods for common notifications
  // ============================================
  
  async tradeExecuted(
    accountId: number,
    accountType: 'demo' | 'live',
    epic: string,
    direction: 'BUY' | 'SELL',
    contracts: number,
    entryPrice: number,
    tradeId?: number
  ): Promise<void> {
    await this.notify(
      'TRADE_EXECUTED',
      'info',
      'Trade Executed',
      `${direction} ${contracts} ${epic} @ ${entryPrice}`,
      {
        accountId,
        accountType,
        tradeId,
        epic,
        details: { direction, contracts, entryPrice },
      }
    );
  }
  
  async tradeClosed(
    accountId: number,
    accountType: 'demo' | 'live',
    epic: string,
    pnl: number,
    tradeId?: number
  ): Promise<void> {
    const pnlStr = pnl >= 0 ? `+$${pnl.toFixed(2)}` : `-$${Math.abs(pnl).toFixed(2)}`;
    await this.notify(
      'TRADE_CLOSED',
      pnl >= 0 ? 'info' : 'warning',
      'Trade Closed',
      `${epic} closed with ${pnlStr}`,
      {
        accountId,
        accountType,
        tradeId,
        epic,
        pnl,
      }
    );
  }
  
  async tradeFailed(
    accountId: number,
    accountType: 'demo' | 'live',
    epic: string,
    reason: string,
    tradeId?: number
  ): Promise<void> {
    await this.notify(
      'TRADE_FAILED',
      'error',
      'Trade Failed',
      `Failed to execute trade on ${epic}: ${reason}`,
      {
        accountId,
        accountType,
        tradeId,
        epic,
        details: { reason },
      }
    );
  }
  
  async tradeStoppedOut(
    accountId: number,
    accountType: 'demo' | 'live',
    epic: string,
    pnl: number,
    tradeId?: number
  ): Promise<void> {
    await this.notify(
      'TRADE_STOPPED_OUT',
      'warning',
      'Trade Stopped Out',
      `${epic} hit stop loss: -$${Math.abs(pnl).toFixed(2)}`,
      {
        accountId,
        accountType,
        tradeId,
        epic,
        pnl,
      }
    );
  }
  
  async simulationStarted(accountCount: number, windowCount: number): Promise<void> {
    await this.notify(
      'SIMULATION_STARTED',
      'info',
      'Simulation Started',
      `Running simulation on ${accountCount} accounts across ${windowCount} windows`,
      {
        details: { accountCount, windowCount },
      }
    );
  }
  
  async simulationCompleted(
    totalTrades: number,
    successfulTrades: number,
    failedTrades: number,
    durationMs: number
  ): Promise<void> {
    await this.notify(
      'SIMULATION_COMPLETED',
      'info',
      'Simulation Completed',
      `${successfulTrades}/${totalTrades} trades successful in ${(durationMs / 1000).toFixed(1)}s`,
      {
        details: { totalTrades, successfulTrades, failedTrades, durationMs },
      }
    );
  }
  
  async rateLimitWarning(accountId: number, callsPerMinute: number): Promise<void> {
    await this.notify(
      'RATE_LIMIT_WARNING',
      'warning',
      'Rate Limit Warning',
      `Account ${accountId} approaching rate limit: ${callsPerMinute} calls/min`,
      {
        accountId,
        details: { callsPerMinute },
      }
    );
  }
  
  async apiError(endpoint: string, errorMessage: string, accountId?: number): Promise<void> {
    await this.notify(
      'API_ERROR',
      'error',
      'API Error',
      `${endpoint}: ${errorMessage}`,
      {
        accountId,
        details: { endpoint, errorMessage },
      }
    );
  }
  
  async systemError(component: string, errorMessage: string): Promise<void> {
    await this.notify(
      'SYSTEM_ERROR',
      'critical',
      'System Error',
      `${component}: ${errorMessage}`,
      {
        details: { component, errorMessage },
      }
    );
  }
}

// Singleton instance
export const notificationService = new NotificationService();

