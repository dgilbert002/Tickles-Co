/**
 * Brain Preview Service
 * 
 * Allows testing what would happen at the next window close without actually executing trades.
 * Shows which accounts will buy vs hold, winning indicators, leverage, and all decision details.
 */

import { brainPreview } from '../live_trading/brain';
import type { BrainResult } from '../live_trading/brain';
import { getDb } from '../db';
import { accounts } from '../../drizzle/schema';
import { eq, and, isNotNull } from 'drizzle-orm';
import { getAccountsForCloseTime } from '../orchestration/epic_filter';

export interface BrainPreviewResult {
  accountId: number;
  accountName: string;
  strategyId: number;
  strategyName: string;
  decision: 'buy' | 'hold';
  epic?: string;
  timeframe?: string;
  leverage?: number;
  stopLoss?: number;
  indicatorName?: string;
  winningTestId?: number;
  conflictMode?: string;
  marketData?: {
    mid: number;
    bid: number;
    ask: number;
  };
  calculationTimeMs: number;
  error?: string;
}

export interface WindowPreviewResult {
  windowCloseTime: string;
  windowName: string;
  totalAccounts: number;
  buyAccounts: number;
  holdAccounts: number;
  accountResults: BrainPreviewResult[];
  calculatedAt: Date;
}

/**
 * Preview brain calculations for a specific window close time
 * Shows what would happen at the next window close
 * 
 * @param windowCloseTime - Window close time in HH:MM:SS format (e.g., "16:00:00")
 * @returns Preview results showing which accounts will buy/hold
 */
export async function previewWindowBrain(windowCloseTime: string): Promise<WindowPreviewResult> {
  const startTime = Date.now();
  
  console.log(`[BrainPreview] Starting preview for window ${windowCloseTime}`);
  
  const db = await getDb();
  if (!db) {
    throw new Error('Database not available');
  }
  
  // Convert window close time to Date object for today
  const [hours, minutes, seconds] = windowCloseTime.split(':').map(Number);
  const closeDate = new Date();
  closeDate.setHours(hours, minutes, seconds, 0);
  
  // Get accounts that have epics closing at this time
  const accountsForWindow = await getAccountsForCloseTime(closeDate);
  
  console.log(`[BrainPreview] Found ${accountsForWindow.length} accounts for window ${windowCloseTime}`);
  
  const accountResults: BrainPreviewResult[] = [];
  let buyCount = 0;
  let holdCount = 0;
  
  // Run brain preview for each account
  for (const account of accountsForWindow) {
    const calcStart = Date.now();
    
    try {
      // Get account details to determine environment
      const db = await getDb();
      if (!db) throw new Error('Database not available');
      
      const accountDetails = await db
        .select()
        .from(accounts)
        .where(eq(accounts.id, account.accountId))
        .limit(1);
      
      if (!accountDetails || accountDetails.length === 0) {
        throw new Error(`Account ${account.accountId} not found`);
      }
      
      const environment = accountDetails[0].accountType as 'demo' | 'live';
      
      // Call existing brain preview function
      const brainResult: BrainResult = await brainPreview(account.strategyId, account.accountId, environment);
      
      const calcTime = Date.now() - calcStart;
      
      const result: BrainPreviewResult = {
        accountId: account.accountId,
        accountName: `Account ${account.accountId}`,
        strategyId: account.strategyId,
        strategyName: 'Strategy',
        decision: brainResult.decision,
        calculationTimeMs: calcTime,
      };
      
      if (brainResult.decision === 'buy') {
        buyCount++;
        result.epic = brainResult.winningEpic;
        result.timeframe = brainResult.winningTimeframe;
        result.leverage = brainResult.leverage ?? undefined;
        result.stopLoss = brainResult.stopLoss ?? undefined;
        result.indicatorName = brainResult.winningIndicator;
        result.winningTestId = brainResult.winningTestId;
        result.conflictMode = brainResult.conflictMode;
        result.marketData = brainResult.marketData;
      } else {
        holdCount++;
      }
      
      accountResults.push(result);
      
      console.log(`[BrainPreview] Account ${account.accountId}: ${brainResult.decision.toUpperCase()} (${calcTime}ms)`);
      
    } catch (error: any) {
      const calcTime = Date.now() - calcStart;
      
      accountResults.push({
        accountId: account.accountId,
        accountName: `Account ${account.accountId}`,
        strategyId: account.strategyId,
        strategyName: 'Strategy',
        decision: 'hold',
        calculationTimeMs: calcTime,
        error: error.message,
      });
      
      holdCount++;
      
      console.error(`[BrainPreview] Error for account ${account.accountId}:`, error.message);
    }
  }
  
  const totalTime = Date.now() - startTime;
  
  console.log(`[BrainPreview] Completed in ${totalTime}ms: ${buyCount} buy, ${holdCount} hold`);
  
  return {
    windowCloseTime,
    windowName: determineWindowName(windowCloseTime),
    totalAccounts: accountsForWindow.length,
    buyAccounts: buyCount,
    holdAccounts: holdCount,
    accountResults,
    calculatedAt: new Date(),
  };
}

/**
 * Preview brain calculations for all active windows
 * Returns preview for each window that has active accounts
 */
export async function previewAllWindows(): Promise<WindowPreviewResult[]> {
  console.log('[BrainPreview] Starting preview for all windows');
  
  const db = await getDb();
  if (!db) {
    throw new Error('Database not available');
  }
  
  // Get all active accounts with strategies
  const activeAccounts = await db
    .select({
      id: accounts.id,
      assignedStrategyId: accounts.assignedStrategyId,
      windowConfig: accounts.windowConfig,
    })
    .from(accounts)
    .where(
      and(
        eq(accounts.botStatus, 'running'),
        isNotNull(accounts.assignedStrategyId),
        isNotNull(accounts.windowConfig)
      )
    );
  
  // Collect all unique window close times
  const windowCloseTimes = new Set<string>();
  
  for (const account of activeAccounts) {
    if (account.windowConfig && account.windowConfig.windows) {
      for (const window of account.windowConfig.windows) {
        windowCloseTimes.add(window.closeTime);
      }
    }
  }
  
  console.log(`[BrainPreview] Found ${windowCloseTimes.size} unique windows`);
  
  // Preview each window
  const results: WindowPreviewResult[] = [];
  
  for (const closeTime of Array.from(windowCloseTimes).sort()) {
    try {
      const windowResult = await previewWindowBrain(closeTime);
      results.push(windowResult);
    } catch (error: any) {
      console.error(`[BrainPreview] Failed to preview window ${closeTime}:`, error.message);
    }
  }
  
  return results;
}

/**
 * Determine window name from close time
 */
function determineWindowName(closeTime: string): string {
  const hour = parseInt(closeTime.split(':')[0]);
  
  if (hour === 16 || hour === 15) {
    return 'Regular Market Close (4pm ET)';
  } else if (hour === 20 || hour === 19) {
    return 'Extended Hours Close (8pm ET)';
  } else if (hour === 9 || hour === 8) {
    return 'Pre-Market (9:30am ET)';
  } else {
    return `Window ${closeTime}`;
  }
}
