/**
 * Leverage Checker
 * 
 * Capital.com API supports leverage management via:
 * - GET /accounts/preferences - Get current leverage per asset class
 * - PUT /accounts/preferences - Set leverage per asset class
 * 
 * Leverage is set at the ACCOUNT level per ASSET CLASS (e.g., SHARES, CURRENCIES).
 * All trades for SHARES (like SOXL, TECL) use the same leverage setting.
 * 
 * Available SHARES leverage options: 1, 2, 3, 4, 5, 10, 20
 * 
 * This module:
 * 1. Checks the current SHARES leverage for an account
 * 2. Updates leverage if it doesn't match the strategy's required leverage
 * 3. Verifies the change was successful
 * 
 * OPTIMIZATION (Dec 2025):
 * - Before making API call, check database for last trade's leverage
 * - If database has matching leverage → skip API call
 * - This reduces unnecessary API calls when leverage hasn't changed
 * 
 * Timing: At T-60s, after brain decision is known (if 'buy'), before trade preparation
 */

import { orchestrationLogger } from './logger';
import { CapitalComAPI } from '../live_trading/capital_api';
import { getDb } from '../db';
import { actualTrades } from '../../drizzle/schema';
import { eq, desc, isNotNull } from 'drizzle-orm';
import { orchestrationTimer } from './timer';

/**
 * In-memory cache of SHARES leverage per Capital.com account
 * Key: Capital.com account ID (string)
 * Value: Current SHARES leverage (number)
 * 
 * This cache prevents unnecessary API calls when leverage hasn't changed.
 * Even if a different DNA wins, it might require the same leverage.
 */
const leverageCache = new Map<string, number>();

/**
 * Get cached leverage for an account
 */
export function getCachedLeverage(capitalAccountId: string): number | undefined {
  return leverageCache.get(capitalAccountId);
}

/**
 * Update cached leverage for an account
 */
export function setCachedLeverage(capitalAccountId: string, leverage: number): void {
  leverageCache.set(capitalAccountId, leverage);
  orchestrationLogger.debug('LEVERAGE_CACHE', 
    `Cached leverage for ${capitalAccountId}: ${leverage}x`
  );
}

/**
 * Clear leverage cache (e.g., on session start or for testing)
 */
export function clearLeverageCache(): void {
  leverageCache.clear();
  orchestrationLogger.debug('LEVERAGE_CACHE', 'Leverage cache cleared');
}

/**
 * Get cache stats for debugging
 */
export function getLeverageCacheStats(): { size: number; entries: Record<string, number> } {
  const entries: Record<string, number> = {};
  leverageCache.forEach((v, k) => { entries[k] = v; });
  return { size: leverageCache.size, entries };
}

export interface LeverageCheckResult {
  accountId: number;
  epic: string;
  requiredLeverage: number;
  currentLeverage: number | null;
  adjusted: boolean;
  success: boolean;
  error?: string;
  availableLeverages?: number[];
  source?: 'cache' | 'database' | 'api';  // Where leverage was confirmed from
}

/**
 * Get the leverage from the most recent trade for an account
 * This is used as a fallback before making API calls
 * 
 * @param accountId - Internal account ID
 * @returns Last trade leverage or null if not found
 */
async function getLastTradeLeverage(accountId: number): Promise<number | null> {
  try {
    const db = await getDb();
    if (!db) {
      return null;
    }
    
    // Query the most recent trade with leverage for this account
    const lastTrade = await db
      .select({ leverage: actualTrades.leverage })
      .from(actualTrades)
      .where(eq(actualTrades.accountId, accountId))
      .orderBy(desc(actualTrades.id))
      .limit(1);
    
    if (lastTrade.length > 0 && lastTrade[0].leverage) {
      return lastTrade[0].leverage;
    }
    
    return null;
  } catch (error: any) {
    orchestrationLogger.warn('LEVERAGE_CHECK', 
      `Failed to get last trade leverage from DB for account ${accountId}: ${error.message}`,
      { accountId }
    );
    return null;
  }
}

/**
 * Check and adjust leverage for a single account/epic
 * 
 * OPTIMIZATION: Uses in-memory cache to skip API calls when leverage is unchanged.
 * - On server restart, cache is empty → first trade fetches from API
 * - Subsequent trades check cache → skip API if leverage matches
 * - Different DNA winners might have same leverage → still a cache hit
 * 
 * @param client - Authenticated Capital.com API client
 * @param accountId - Internal account ID (for logging)
 * @param epic - The epic being traded (for logging)
 * @param requiredLeverage - The leverage the strategy requires
 * @param capitalAccountId - Capital.com account ID (for caching)
 */
export async function checkAndAdjustLeverage(
  client: CapitalComAPI,
  accountId: number,
  epic: string,
  requiredLeverage: number,
  capitalAccountId?: string,
  actualLeverageState?: Map<string, number>  // NEW: Actual state from T-5m fetch (GROUND TRUTH)
): Promise<LeverageCheckResult> {
  const result: LeverageCheckResult = {
    accountId,
    epic,
    requiredLeverage,
    currentLeverage: null,
    adjusted: false,
    success: false,
  };
  
  // Get closing logger for leverage change logging
  const closingLogger = orchestrationTimer.getClosingLogger();

  try {
    // =========================================================================
    // STEP 1: Use T-5m actual leverage state (GROUND TRUTH)
    // =========================================================================
    // This prevents circular database logic where pending trade's DESIRED leverage
    // (just inserted into actual_trades) is mistaken for ACTUAL leverage on Capital.com
    if (actualLeverageState && capitalAccountId) {
      const actualLeverage = actualLeverageState.get(capitalAccountId);
      
      if (actualLeverage !== undefined) {
        // We know the ACTUAL leverage from T-5m fetch
        console.log(`[LeverageCheck] Account ${accountId}: ACTUAL leverage from T-5m = ${actualLeverage}x, Required = ${requiredLeverage}x`);
        
        if (actualLeverage === requiredLeverage) {
          // Already correct - skip API call
          await orchestrationLogger.info('LEVERAGE_CHECK',
            `✓ SHARES leverage ${requiredLeverage}x (T-5m verified - no change needed)`,
            { accountId, epic, data: { actualLeverage, requiredLeverage, source: 't5m_verified' } }
          );
          closingLogger?.log(`   🔧 Leverage: ${requiredLeverage}x (T-5m verified - no change)`);
          
          result.currentLeverage = actualLeverage;
          result.success = true;
          result.adjusted = false;
          result.source = 't5m_verified';
          return result;
        }
        
        // Mismatch detected - ACTUALLY change it via API
        console.log(`[LeverageCheck] Account ${accountId}: Mismatch detected! ${actualLeverage}x → ${requiredLeverage}x - calling API`);
        
        await orchestrationLogger.info('LEVERAGE_CHECK',
          `Leverage mismatch: ${actualLeverage}x → ${requiredLeverage}x (calling API to change)`,
          { accountId, epic, data: { actualLeverage, requiredLeverage } }
        );
        closingLogger?.log(`   🔧 Leverage: ${actualLeverage}x → ${requiredLeverage}x (CHANGING via API)`);
        
        // ACTUALLY call API to change leverage
        const changeSuccess = await client.setAccountLeverage({ SHARES: requiredLeverage });
        
        if (changeSuccess) {
          // Update the actual state map for next window in same session
          actualLeverageState.set(capitalAccountId, requiredLeverage);
          
          console.log(`[LeverageCheck] Account ${accountId}: ✅ Leverage changed to ${requiredLeverage}x`);
          
          await orchestrationLogger.info('LEVERAGE_ADJUSTED',
            `✅ SHARES leverage changed ${actualLeverage}x → ${requiredLeverage}x`,
            { accountId, epic, data: { before: actualLeverage, after: requiredLeverage } }
          );
          closingLogger?.log(`   ✅ Leverage changed to ${requiredLeverage}x (confirmed via API)`);
          
          result.currentLeverage = requiredLeverage;
          result.adjusted = true;
          result.success = true;
          result.source = 'api';
          return result;
        } else {
          console.error(`[LeverageCheck] Account ${accountId}: ❌ Failed to change leverage`);
          
          await orchestrationLogger.error('LEVERAGE_ERROR',
            `❌ Failed to change leverage ${actualLeverage}x → ${requiredLeverage}x`,
            new Error('setAccountLeverage returned false'),
            { accountId, epic }
          );
          closingLogger?.log(`   ❌ Leverage change FAILED!`);
          
          result.currentLeverage = actualLeverage;
          result.adjusted = false;
          result.success = false;
          result.error = 'API call failed';
          result.source = 'api';
          return result;
        }
      }
      
      // If actualLeverage is undefined, fall through to legacy logic below
      console.warn(`[LeverageCheck] Account ${accountId}: No T-5m leverage data found - falling back to API`);
    }

    // =========================================================================
    // FALLBACK: If no actualLeverageState (shouldn't happen), call API directly
    // =========================================================================
    // This is for safety in case T-5m fetch failed or was skipped
    console.warn(`[LeverageCheck] Account ${accountId}: Using fallback API call (T-5m data not available)`);
    
    await orchestrationLogger.warn('LEVERAGE_CHECK',
      `No T-5m leverage data for account ${accountId} - calling API directly`,
      { accountId, epic, data: { requiredLeverage } }
    );
    closingLogger?.log(`   ⚠️ Leverage: T-5m data missing - calling API directly`);
    
    const leverageResult = await client.ensureSharesLeverage(requiredLeverage);
    
    result.currentLeverage = leverageResult.currentLeverage;
    result.adjusted = leverageResult.wasUpdated;
    result.success = leverageResult.success;
    result.error = leverageResult.error;
    result.source = 'api_fallback';

    // Update actualLeverageState if successful (for next check in same window)
    if (capitalAccountId && leverageResult.success && actualLeverageState) {
      actualLeverageState.set(capitalAccountId, requiredLeverage);
    }

    if (leverageResult.success) {
      if (leverageResult.wasUpdated) {
        await orchestrationLogger.info('LEVERAGE_ADJUSTED',
          `✓ SHARES leverage updated to ${requiredLeverage}x (fallback API)`,
          { accountId, epic, data: { 
            requiredLeverage, 
            previousLeverage: leverageResult.currentLeverage,
            wasUpdated: true,
            source: 'api_fallback'
          }}
        );
        closingLogger?.log(`   ✅ Leverage set to ${requiredLeverage}x via API (fallback - was ${leverageResult.currentLeverage || 'unknown'}x)`);
      } else {
        await orchestrationLogger.info('LEVERAGE_CHECK',
          `✓ SHARES leverage already at ${requiredLeverage}x (fallback API confirmed)`,
          { accountId, epic, data: { requiredLeverage, wasUpdated: false, source: 'api_fallback' } }
        );
        closingLogger?.log(`   ✅ Leverage: ${requiredLeverage}x (fallback API confirmed - no change needed)`);
      }
    } else {
      await orchestrationLogger.warn('LEVERAGE_ERROR',
        `✗ Failed to set leverage for account ${accountId}: ${leverageResult.error}`,
        { accountId, epic, data: { 
          requiredLeverage, 
          currentLeverage: leverageResult.currentLeverage,
          error: leverageResult.error 
        }}
      );
      closingLogger?.log(`   ❌ Leverage FAILED: ${leverageResult.error || 'unknown error'}`);
    }

    return result;
    
  } catch (error: any) {
    result.error = error.message;
    await orchestrationLogger.error('LEVERAGE_ERROR',
      `Error checking leverage for account ${accountId}: ${error.message}`,
      { accountId, epic, data: { error: error.message } }
    );
    closingLogger?.log(`   ❌ Leverage ERROR: ${error.message}`);
    return result;
  }
}

/**
 * Check leverage for multiple brain results
 * 
 * This should be called after brain decisions are known, for all accounts
 * that have a 'buy' decision. It ensures each account's leverage is set
 * correctly before trades are placed.
 * 
 * @param client - Authenticated Capital.com API client
 * @param accountId - Internal account ID
 * @param brainResults - Array of brain results with epic, leverage, and decision
 */
export async function checkLeverageForBrainResults(
  client: CapitalComAPI,
  accountId: number,
  brainResults: Array<{ epic: string; leverage: number | null; decision: string }>
): Promise<LeverageCheckResult[]> {
  const results: LeverageCheckResult[] = [];

  // Filter to only 'buy' decisions with leverage specified
  const buyDecisions = brainResults.filter(
    r => r.decision === 'buy' && r.leverage !== null
  );

  if (buyDecisions.length === 0) {
    await orchestrationLogger.info('LEVERAGE_CHECK',
      `No leverage adjustment needed (no buy decisions)`,
      { accountId }
    );
    return results;
  }

  await orchestrationLogger.info('LEVERAGE_CHECK',
    `Checking leverage for ${buyDecisions.length} buy decisions`,
    { accountId, data: { count: buyDecisions.length } }
  );

  // For SHARES, all epics use the same leverage setting
  // So we only need to set it once based on the first buy decision
  // (In theory, all buy decisions for SHARES should have the same leverage)
  const firstBuy = buyDecisions[0];
  
  if (firstBuy.leverage !== null) {
    const result = await checkAndAdjustLeverage(
      client,
      accountId,
      firstBuy.epic,
      firstBuy.leverage
    );
    results.push(result);
    
    // If we have multiple buy decisions with different leverages, warn
    const differentLeverages = buyDecisions.filter(b => b.leverage !== firstBuy.leverage);
    if (differentLeverages.length > 0) {
      await orchestrationLogger.warn('LEVERAGE_CONFLICT',
        `Multiple buy decisions with different leverage requirements! Using ${firstBuy.leverage}x`,
        { accountId, data: { 
          usedLeverage: firstBuy.leverage,
          conflicts: differentLeverages.map(d => ({ epic: d.epic, leverage: d.leverage }))
        }}
      );
    }
  }

  await orchestrationLogger.info('LEVERAGE_CHECK',
    `Leverage check complete for account ${accountId}`,
    { accountId, data: { results: results.length, success: results.every(r => r.success) } }
  );

  return results;
}

/**
 * Get current SHARES leverage for an account
 * 
 * Utility function to just check the current leverage without changing it.
 */
export async function getCurrentSharesLeverage(
  client: CapitalComAPI
): Promise<{ current: number | null; available: number[] }> {
  const prefs = await client.getAccountPreferences();
  
  if (!prefs?.leverages?.SHARES) {
    return { current: null, available: [] };
  }
  
  return {
    current: prefs.leverages.SHARES.current,
    available: prefs.leverages.SHARES.available,
  };
}
