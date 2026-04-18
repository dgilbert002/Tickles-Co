/**
 * Session Keep-Alive Manager
 * 
 * Maintains active sessions with Capital.com API to prevent timeouts.
 * Monitors bot heartbeats and detects stale/dead bots.
 */

import { getDb } from '../db';
import { accounts } from '../../drizzle/schema';
import { eq } from 'drizzle-orm';
import { connectionManager } from './connection_manager';
import { orchestrationLogger } from './logger';

interface SessionStatus {
  accountId: string;
  lastKeepalive: Date;
  isAlive: boolean;
  consecutiveFailures: number;
}

class SessionManager {
  private sessions: Map<string, SessionStatus> = new Map();
  private keepaliveInterval: NodeJS.Timeout | null = null;
  
  // Configuration
  private readonly KEEPALIVE_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes
  private readonly MAX_CONSECUTIVE_FAILURES = 3;

  /**
   * Start session keep-alive
   * 
   * NOTE: Heartbeat/stale detection has been REMOVED.
   * Bots are either running or stopped - simple as that.
   * The orchestration timer handles brain calculations for active accounts.
   */
  start(): void {
    if (this.keepaliveInterval) {
      orchestrationLogger.warn('SYSTEM_ERROR', '[SessionManager] Already running');
      return;
    }

    orchestrationLogger.info('TIMER_STARTED', '[SessionManager] Starting session keep-alive (API connection maintenance)');

    // Keep-alive: Ping Capital.com API periodically to maintain connections
    this.keepaliveInterval = setInterval(() => {
      this.runKeepalive();
    }, this.KEEPALIVE_INTERVAL_MS);

    // Run immediately
    this.runKeepalive();
  }

  /**
   * Stop session keep-alive and heartbeat monitoring
   */
  stop(): void {
    if (this.keepaliveInterval) {
      clearInterval(this.keepaliveInterval);
      this.keepaliveInterval = null;
    }

    if (this.heartbeatCheckInterval) {
      clearInterval(this.heartbeatCheckInterval);
      this.heartbeatCheckInterval = null;
    }

    this.sessions.clear();
    orchestrationLogger.info('TIMER_STOPPED', '[SessionManager] Stopped');
  }

  /**
   * Run keep-alive for all active sessions
   */
  private async runKeepalive(): Promise<void> {
    try {
      const db = await getDb();
      if (!db) return;

      // Get all running bots
      const runningAccounts = await db
        .select()
        .from(accounts)
        .where(eq(accounts.botStatus, 'running'));

      if (runningAccounts.length === 0) {
        orchestrationLogger.debug('SYSTEM_ERROR', '[SessionManager] No running bots to keep alive');
        return;
      }

      orchestrationLogger.info('SYSTEM_ERROR', `[SessionManager] Running keep-alive for ${runningAccounts.length} active sessions`);

      // Group by account type (demo/live)
      const demoAccounts = runningAccounts.filter(a => a.accountType === 'demo');
      const liveAccounts = runningAccounts.filter(a => a.accountType === 'live');

      // Keep-alive for demo accounts
      if (demoAccounts.length > 0) {
        await this.keepaliveForEnvironment('demo', demoAccounts.map(a => a.accountId));
      }

      // Keep-alive for live accounts
      if (liveAccounts.length > 0) {
        await this.keepaliveForEnvironment('live', liveAccounts.map(a => a.accountId));
      }
    } catch (error: any) {
      orchestrationLogger.error('SYSTEM_ERROR', '[SessionManager] Error in keep-alive', { error: { message: error.message } });
    }
  }

  /**
   * Keep-alive for a specific environment
   */
  private async keepaliveForEnvironment(
    environment: 'demo' | 'live',
    accountIds: string[]
  ): Promise<void> {
    try {
      const api = connectionManager.getClient(environment);
      if (!api) {
        orchestrationLogger.error('SYSTEM_ERROR', `[SessionManager] No ${environment} API client available`);
        return;
      }

      const success = await api.keepalive();
      const now = new Date();

      for (const accountId of accountIds) {
        const status = this.sessions.get(accountId) || {
          accountId,
          lastKeepalive: now,
          isAlive: true,
          consecutiveFailures: 0,
        };

        if (success) {
          status.lastKeepalive = now;
          status.isAlive = true;
          status.consecutiveFailures = 0;
          orchestrationLogger.debug('SYSTEM_ERROR', `[SessionManager] Keep-alive OK for ${accountId} (${environment})`);
        } else {
          status.consecutiveFailures++;
          orchestrationLogger.warn(
            'SYSTEM_ERROR',
            `[SessionManager] Keep-alive failed for ${accountId} (${environment}), ` +
            `failures: ${status.consecutiveFailures}/${this.MAX_CONSECUTIVE_FAILURES}`
          );

          // Mark session as dead after max failures
          if (status.consecutiveFailures >= this.MAX_CONSECUTIVE_FAILURES) {
            status.isAlive = false;
            await this.handleDeadSession(accountId, environment);
          }
        }

        this.sessions.set(accountId, status);
      }
    } catch (error: any) {
      orchestrationLogger.error('SYSTEM_ERROR', `[SessionManager] Error in keep-alive for ${environment}`, { error: { message: error.message } });
    }
  }


  /**
   * Handle dead session (keep-alive failures)
   */
  private async handleDeadSession(accountId: string, environment: 'demo' | 'live'): Promise<void> {
    try {
      const db = await getDb();
      if (!db) return;

      orchestrationLogger.error('SYSTEM_ERROR', `[SessionManager] Session dead for ${accountId} (${environment}), marking bot as error`);

      // Find bot in database
      const bot = await db
        .select()
        .from(accounts)
        .where(eq(accounts.accountId, accountId))
        .limit(1);

      if (bot.length > 0) {
        await updateBotStatus(bot[0].id, 'error');
        orchestrationLogger.info('SYSTEM_ERROR', `[SessionManager] Bot ${bot[0].id} marked as error due to dead session`);
      }
    } catch (error: any) {
      orchestrationLogger.error('SYSTEM_ERROR', '[SessionManager] Error handling dead session', { error: { message: error.message } });
    }
  }

  
  /**
   * Execute keep-alive immediately (called before quiet period starts)
   * This ensures sessions stay alive during the trading window when
   * regular keep-alive calls are blocked.
   */
  async executeKeepAliveNow(): Promise<void> {
    orchestrationLogger.info('SESSION_KEEPALIVE', '[SessionManager] Executing immediate keep-alive before quiet period');
    await this.runKeepalive();
  }

  /**
   * Get session status
   */
  getSessionStatus(accountId: string): SessionStatus | null {
    return this.sessions.get(accountId) || null;
  }

  /**
   * Get all session statuses
   */
  getAllSessionStatuses(): SessionStatus[] {
    return Array.from(this.sessions.values());
  }

  /**
   * Get status summary
   */
  getStatus(): {
    isRunning: boolean;
    activeSessions: number;
    aliveSessions: number;
    deadSessions: number;
  } {
    const sessions = Array.from(this.sessions.values());
    return {
      isRunning: this.keepaliveInterval !== null,
      activeSessions: sessions.length,
      aliveSessions: sessions.filter(s => s.isAlive).length,
      deadSessions: sessions.filter(s => !s.isAlive).length,
    };
  }
}

// Singleton instance
export const sessionManager = new SessionManager();
