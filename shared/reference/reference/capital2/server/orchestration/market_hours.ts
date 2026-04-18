/**
 * Market Hours - Fetch and calculate market open/close times for epics
 * 
 * Uses Capital.com API to get market navigation data
 * Calculates next market open/close based on current time
 */

import type { CapitalComAPI } from '../live_trading/capital_api';
import { orchestrationLogger } from './logger';

export interface MarketHours {
  epic: string;
  isOpen: boolean;
  nextOpen: Date | null;
  nextClose: Date | null;
  timezone: string;
}

export interface MarketTiming {
  nextActionTime: Date;
  nextActionType: 'open' | 'close';
  timeUntilAction: number; // milliseconds
}

/**
 * Fetch market hours for an epic from Capital.com
 * 
 * Note: This is a simplified version that uses market status.
 * For production, we'll need to parse actual market hours from the API.
 */
export async function fetchMarketHours(
  client: CapitalComAPI,
  epic: string
): Promise<MarketHours | null> {
  try {
    // Fetch market info
    const marketInfo = await client.getMarketInfo(epic);
    
    if (!marketInfo || !marketInfo.snapshot) {
      await orchestrationLogger.warn('MARKET_HOURS_FETCH_FAILED', `No market data for ${epic}`);
      return null;
    }

    const snapshot = marketInfo.snapshot;
    const marketStatus = snapshot.marketStatus; // 'TRADEABLE', 'CLOSED', etc.
    const isOpen = marketStatus === 'TRADEABLE';

    // For now, use simplified logic:
    // - If market is open, assume it closes at 4pm ET (9pm UTC)
    // - If market is closed, assume it opens at 9:30am ET (2:30pm UTC)
    // TODO: Parse actual market hours from API response
    
    const now = new Date();
    let nextOpen: Date | null = null;
    let nextClose: Date | null = null;

    if (isOpen) {
      // Market is open, calculate next close (4pm ET = 21:00 UTC)
      nextClose = new Date(now);
      nextClose.setUTCHours(21, 0, 0, 0);
      
      // If close time has passed today, it's tomorrow
      if (nextClose <= now) {
        nextClose.setUTCDate(nextClose.getUTCDate() + 1);
      }
    } else {
      // Market is closed, calculate next open (9:30am ET = 14:30 UTC)
      nextOpen = new Date(now);
      nextOpen.setUTCHours(14, 30, 0, 0);
      
      // If open time has passed today, it's tomorrow
      if (nextOpen <= now) {
        nextOpen.setUTCDate(nextOpen.getUTCDate() + 1);
      }
    }

    const marketHours: MarketHours = {
      epic,
      isOpen,
      nextOpen,
      nextClose,
      timezone: 'America/New_York', // ET timezone
    };

    await orchestrationLogger.debug('MARKET_HOURS_FETCHED', `Market hours for ${epic}`, {
      epic,
      data: {
        isOpen,
        nextOpen: nextOpen?.toISOString(),
        nextClose: nextClose?.toISOString(),
      },
    });

    return marketHours;
  } catch (error: any) {
    await orchestrationLogger.logError(
      'MARKET_HOURS_FETCH_FAILED',
      `Failed to fetch market hours for ${epic}`,
      error,
      { epic }
    );
    return null;
  }
}

/**
 * Calculate next market timing (open or close) for an epic
 */
export function calculateNextTiming(marketHours: MarketHours): MarketTiming | null {
  const now = new Date();

  if (marketHours.isOpen && marketHours.nextClose) {
    return {
      nextActionTime: marketHours.nextClose,
      nextActionType: 'close',
      timeUntilAction: marketHours.nextClose.getTime() - now.getTime(),
    };
  }

  if (!marketHours.isOpen && marketHours.nextOpen) {
    return {
      nextActionTime: marketHours.nextOpen,
      nextActionType: 'open',
      timeUntilAction: marketHours.nextOpen.getTime() - now.getTime(),
    };
  }

  return null;
}

/**
 * Get earliest market timing from multiple epics
 */
export function getEarliestTiming(timings: MarketTiming[]): MarketTiming | null {
  if (timings.length === 0) return null;

  return timings.reduce((earliest, current) => {
    return current.timeUntilAction < earliest.timeUntilAction ? current : earliest;
  });
}

/**
 * Format countdown time (e.g., "2h 15m 30s")
 */
export function formatCountdown(milliseconds: number): string {
  if (milliseconds < 0) return 'Overdue';

  const seconds = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (days > 0) {
    return `${days}d ${hours % 24}h ${minutes % 60}m`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
}
