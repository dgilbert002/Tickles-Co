/**
 * ============================================================================
 * PHASE 2.1: Window Resolver Service
 * ============================================================================
 * 
 * PURPOSE: Dynamically resolve window close times from current market data
 *          instead of using stale stored values.
 * 
 * PROBLEM SOLVED: Strategies and accounts stored closeTime statically at creation.
 *                 When market hours change (extended hours, holidays, DST), the
 *                 stored times become stale and accounts don't trade.
 * 
 * SOLUTION: Always resolve close times from:
 *           1. epics.nextClose (most accurate for today's specific schedule)
 *           2. marketInfo.marketCloseTime (fallback, general schedule)
 * 
 * ROLLBACK: If this causes issues, callers can fall back to using stored
 *           windowConfig.closeTime directly. Search for "PHASE 2" comments.
 * ============================================================================
 */

import { getDb } from '../db';
import { epics, marketInfo, savedStrategies } from '../../drizzle/schema';
import { eq, inArray } from 'drizzle-orm';

// Type alias for the epics table to avoid name collision
const epicsTable = epics;

/**
 * Resolve the current close time for a single epic
 * 
 * Priority:
 * 1. epics.nextClose - Most accurate, reflects today's actual schedule
 * 2. marketInfo.marketCloseTime - Fallback, general market hours
 * 3. Default '21:00:00' - Failsafe for US regular hours
 * 
 * @param epic - The epic symbol (e.g., 'SOXL', 'MSTR')
 * @returns Close time in HH:MM:SS format (UTC)
 */
export async function resolveCurrentCloseTime(epic: string): Promise<string> {
  const db = await getDb();
  if (!db) {
    console.warn(`[WindowResolver] Database not available, using default close time for ${epic}`);
    return '21:00:00';
  }

  try {
    // 1. Try epics.nextClose first (most accurate)
    const epicRecord = await db
      .select({ 
        nextClose: epicsTable.nextClose,
        marketStatus: epicsTable.marketStatus 
      })
      .from(epicsTable)
      .where(eq(epicsTable.symbol, epic))
      .limit(1);

    if (epicRecord[0]?.nextClose) {
      // Extract time portion (HH:MM:SS) from datetime
      const nextClose = new Date(epicRecord[0].nextClose);
      const hours = nextClose.getUTCHours().toString().padStart(2, '0');
      const minutes = nextClose.getUTCMinutes().toString().padStart(2, '0');
      const seconds = nextClose.getUTCSeconds().toString().padStart(2, '0');
      const closeTime = `${hours}:${minutes}:${seconds}`;
      
      console.debug(`[WindowResolver] Resolved ${epic} close time from epics.nextClose: ${closeTime}`);
      return closeTime;
    }

    // 2. Fallback to marketInfo.marketCloseTime
    const marketRecord = await db
      .select({ marketCloseTime: marketInfo.marketCloseTime })
      .from(marketInfo)
      .where(eq(marketInfo.epic, epic))
      .limit(1);

    if (marketRecord[0]?.marketCloseTime) {
      // Normalize to HH:MM:SS format
      const closeTime = normalizeTimeFormat(marketRecord[0].marketCloseTime);
      console.debug(`[WindowResolver] Resolved ${epic} close time from marketInfo: ${closeTime}`);
      return closeTime;
    }

    // 3. Default fallback
    console.warn(`[WindowResolver] No close time found for ${epic}, using default 21:00:00`);
    return '21:00:00';
  } catch (error) {
    console.error(`[WindowResolver] Error resolving close time for ${epic}:`, error);
    return '21:00:00';
  }
}

/**
 * Resolve close times for multiple epics
 * Returns a map of epic → closeTime
 */
export async function resolveCloseTimesForEpics(epicList: string[]): Promise<Map<string, string>> {
  const result = new Map<string, string>();
  
  if (epicList.length === 0) {
    return result;
  }

  const db = await getDb();
  if (!db) {
    // Return defaults for all
    epicList.forEach(epic => result.set(epic, '21:00:00'));
    return result;
  }

  try {
    // Batch query epics table
    const epicRecords = await db
      .select({ 
        symbol: epicsTable.symbol,
        nextClose: epicsTable.nextClose 
      })
      .from(epicsTable)
      .where(inArray(epicsTable.symbol, epicList));

    // Batch query marketInfo for fallback
    const marketRecords = await db
      .select({ 
        epic: marketInfo.epic,
        marketCloseTime: marketInfo.marketCloseTime 
      })
      .from(marketInfo)
      .where(inArray(marketInfo.epic, epicList));

    // Build fallback map
    const marketInfoMap = new Map<string, string>();
    marketRecords.forEach(m => {
      if (m.marketCloseTime) {
        marketInfoMap.set(m.epic, normalizeTimeFormat(m.marketCloseTime));
      }
    });

    // Resolve each epic
    for (const epic of epicList) {
      // Try epics.nextClose first
      const epicRecord = epicRecords.find(e => e.symbol === epic);
      if (epicRecord?.nextClose) {
        const nextClose = new Date(epicRecord.nextClose);
        const hours = nextClose.getUTCHours().toString().padStart(2, '0');
        const minutes = nextClose.getUTCMinutes().toString().padStart(2, '0');
        const seconds = nextClose.getUTCSeconds().toString().padStart(2, '0');
        result.set(epic, `${hours}:${minutes}:${seconds}`);
        continue;
      }

      // Fallback to marketInfo
      if (marketInfoMap.has(epic)) {
        result.set(epic, marketInfoMap.get(epic)!);
        continue;
      }

      // Default
      result.set(epic, '21:00:00');
    }

    return result;
  } catch (error) {
    console.error('[WindowResolver] Error resolving close times:', error);
    epicList.forEach(epic => result.set(epic, '21:00:00'));
    return result;
  }
}

/**
 * Resolve the primary close time for a window containing multiple epics
 * Uses the MOST COMMON close time among the epics
 * 
 * @param epicList - Array of epics in the window
 * @returns Primary close time in HH:MM:SS format
 */
export async function resolveWindowCloseTime(epicList: string[]): Promise<string> {
  if (epicList.length === 0) {
    return '21:00:00';
  }

  // Get unique epics to reduce DB queries
  const uniqueEpics = [...new Set(epicList)];
  const closeTimeMap = await resolveCloseTimesForEpics(uniqueEpics);
  
  // ========================================================================
  // BUG FIX: Count based on ORIGINAL epicList (with duplicates), not unique map!
  // Before: closeTimeMap.forEach() only counted unique epics = 1 each
  // After: Loop through epicList to properly count duplicates
  // Example: ['SOXL', 'SOXL', 'TECL', 'TECL'] should give 01:00→2, 21:00→2
  // ========================================================================
  const counts = new Map<string, number>();
  for (const epic of epicList) {
    const closeTime = closeTimeMap.get(epic) || '21:00:00';
    counts.set(closeTime, (counts.get(closeTime) || 0) + 1);
  }
  
  console.log(`[WindowResolver] resolveWindowCloseTime: epicList=${epicList.length} epics, counts=${JSON.stringify(Object.fromEntries(counts))}`);

  // Find most common - in case of tie, first one wins (insertion order)
  let maxCount = 0;
  let primaryCloseTime = '21:00:00';
  counts.forEach((count, closeTime) => {
    if (count > maxCount) {
      maxCount = count;
      primaryCloseTime = closeTime;
    }
  });

  return primaryCloseTime;
}

/**
 * Get all epics that close at a specific time TODAY
 * Used by timer to determine which accounts should trade at a given close time
 * 
 * @param closeTime - Close time to match (HH:MM:SS format)
 * @param toleranceMinutes - How many minutes of tolerance for matching (default 1)
 * @returns Array of epic symbols
 */
export async function getEpicsForCloseTime(
  closeTime: string, 
  toleranceMinutes: number = 1
): Promise<string[]> {
  const db = await getDb();
  if (!db) return [];

  try {
    // Parse target time
    const [targetHours, targetMinutes] = closeTime.split(':').map(Number);
    const targetTotalMinutes = targetHours * 60 + targetMinutes;

    // Get all epics with their close times
    const epicRecords = await db
      .select({ 
        symbol: epicsTable.symbol,
        nextClose: epicsTable.nextClose,
        marketStatus: epicsTable.marketStatus
      })
      .from(epicsTable);

    const matchingEpics: string[] = [];

    for (const epic of epicRecords) {
      if (!epic.nextClose) continue;

      const nextClose = new Date(epic.nextClose);
      const epicHours = nextClose.getUTCHours();
      const epicMinutes = nextClose.getUTCMinutes();
      const epicTotalMinutes = epicHours * 60 + epicMinutes;

      // Check if within tolerance
      const diff = Math.abs(epicTotalMinutes - targetTotalMinutes);
      if (diff <= toleranceMinutes) {
        matchingEpics.push(epic.symbol);
      }
    }

    return matchingEpics;
  } catch (error) {
    console.error('[WindowResolver] Error getting epics for close time:', error);
    return [];
  }
}

/**
 * Check if a strategy has DNA strands for epics closing at a specific time
 * This is the KEY function for matching accounts to windows dynamically
 * 
 * @param strategyId - The strategy to check
 * @param closeTime - The close time to match against
 * @returns true if strategy has DNA strands for epics closing at this time
 */
export async function strategyMatchesCloseTime(
  strategyId: number,
  closeTime: string
): Promise<{ matches: boolean; matchingEpics: string[]; allEpics: string[] }> {
  const db = await getDb();
  if (!db) {
    return { matches: false, matchingEpics: [], allEpics: [] };
  }

  try {
    // Get strategy's DNA strands
    const [strategy] = await db
      .select({ dnaStrands: savedStrategies.dnaStrands })
      .from(savedStrategies)
      .where(eq(savedStrategies.id, strategyId))
      .limit(1);

    if (!strategy?.dnaStrands) {
      return { matches: false, matchingEpics: [], allEpics: [] };
    }

    // Extract unique epics from DNA strands
    const dnaStrands = strategy.dnaStrands as Array<{ epic?: string }>;
    const allEpics = [...new Set(dnaStrands.map(d => d.epic).filter(Boolean))] as string[];

    if (allEpics.length === 0) {
      return { matches: false, matchingEpics: [], allEpics: [] };
    }

    // Get epics closing at the target time
    const epicsClosingNow = await getEpicsForCloseTime(closeTime);
    
    // Find intersection
    const matchingEpics = allEpics.filter(epic => epicsClosingNow.includes(epic));

    return {
      matches: matchingEpics.length > 0,
      matchingEpics,
      allEpics
    };
  } catch (error) {
    console.error('[WindowResolver] Error checking strategy match:', error);
    return { matches: false, matchingEpics: [], allEpics: [] };
  }
}

/**
 * Check if today is an unusual market day (non-standard close times)
 * Used to warn users when editing strategies on holidays/early close days
 * 
 * @returns Object with unusual flag and reason
 */
export async function isUnusualMarketDay(): Promise<{ 
  unusual: boolean; 
  reason?: string;
  closeTimes: string[];
}> {
  const db = await getDb();
  if (!db) {
    return { unusual: false, closeTimes: [] };
  }

  try {
    // Normal US market close times in UTC
    const normalCloseTimes = ['21:00', '01:00']; // Regular (4pm ET) and Extended (8pm ET)
    
    // Get current close times from epics
    const epicRecords = await db
      .select({ 
        symbol: epicsTable.symbol,
        nextClose: epicsTable.nextClose 
      })
      .from(epicsTable)
      .where(eq(epicsTable.marketStatus, 'TRADEABLE'));

    const currentCloseTimes = new Set<string>();
    
    for (const epic of epicRecords) {
      if (epic.nextClose) {
        const nextClose = new Date(epic.nextClose);
        const hours = nextClose.getUTCHours().toString().padStart(2, '0');
        const minutes = nextClose.getUTCMinutes().toString().padStart(2, '0');
        currentCloseTimes.add(`${hours}:${minutes}`);
      }
    }

    const closeTimesArray = Array.from(currentCloseTimes);
    
    // Check for unusual times
    const unusualTimes = closeTimesArray.filter(
      time => !normalCloseTimes.includes(time)
    );

    if (unusualTimes.length > 0) {
      return {
        unusual: true,
        reason: `Early market close detected (${unusualTimes.join(', ')} UTC) - possibly a holiday`,
        closeTimes: closeTimesArray
      };
    }

    return { unusual: false, closeTimes: closeTimesArray };
  } catch (error) {
    console.error('[WindowResolver] Error checking for unusual day:', error);
    return { unusual: false, closeTimes: [] };
  }
}

/**
 * Normalize time format to HH:MM:SS
 */
function normalizeTimeFormat(time: string): string {
  if (!time) return '21:00:00';
  
  // Already has seconds
  if (time.match(/^\d{2}:\d{2}:\d{2}$/)) {
    return time;
  }
  
  // Just HH:MM
  if (time.match(/^\d{2}:\d{2}$/)) {
    return `${time}:00`;
  }
  
  // Single digit hours
  if (time.match(/^\d:\d{2}$/)) {
    return `0${time}:00`;
  }
  
  return '21:00:00';
}

/**
 * Compare two close times with tolerance
 */
export function closeTimesMatch(
  time1: string, 
  time2: string, 
  toleranceMinutes: number = 1
): boolean {
  const [h1, m1] = time1.split(':').map(Number);
  const [h2, m2] = time2.split(':').map(Number);
  
  const total1 = h1 * 60 + m1;
  const total2 = h2 * 60 + m2;
  
  return Math.abs(total1 - total2) <= toleranceMinutes;
}
