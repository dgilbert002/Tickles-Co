/**
 * Market Hours Service
 * 
 * Fetches, parses, and manages market hours from Capital.com API
 * Handles multiple trading sessions, weekends, and holidays
 * Calculates next open/close times in UTC
 */

import { CapitalComAPI } from '../live_trading/capital_api';
import { getDb } from '../db';
import { epics, marketInfo } from '../../drizzle/schema';
import { eq } from 'drizzle-orm';
import { orchestrationLogger } from './logger';

interface OpeningHours {
  mon: string[];
  tue: string[];
  wed: string[];
  thu: string[];
  fri: string[];
  sat: string[];
  sun: string[];
  zone: string;
}

interface MarketTiming {
  isOpen: boolean;
  nextOpen: Date | null;
  nextClose: Date | null;
  currentSession: { start: string; end: string } | null;
}

/**
 * Parse time string "HH:MM" to minutes since midnight
 */
function parseTimeToMinutes(time: string): number {
  const [hours, minutes] = time.split(':').map(Number);
  return hours * 60 + minutes;
}

/**
 * Create Date object from day of week and time string
 * @param dayOfWeek 0=Sunday, 1=Monday, ..., 6=Saturday
 * @param time "HH:MM" in UTC
 * @param referenceDate Starting point for calculation
 */
function createDateFromDayAndTime(dayOfWeek: number, time: string, referenceDate: Date): Date {
  const [hours, minutes] = time.split(':').map(Number);
  const date = new Date(referenceDate);
  
  // Calculate days to add to get to target day of week
  const currentDay = date.getUTCDay();
  let daysToAdd = dayOfWeek - currentDay;
  if (daysToAdd < 0) {
    daysToAdd += 7; // Next week
  }
  
  date.setUTCDate(date.getUTCDate() + daysToAdd);
  date.setUTCHours(hours, minutes, 0, 0);
  
  return date;
}

/**
 * Parse opening hours string like "14:30 - 21:00" to {start, end}
 */
function parseSession(session: string): { start: string; end: string } {
  const [start, end] = session.split(' - ').map(s => s.trim());
  return { start, end };
}

/**
 * Get all trading sessions for a specific day
 */
function getSessionsForDay(openingHours: OpeningHours, dayName: string): Array<{ start: string; end: string }> {
  const sessions = openingHours[dayName as keyof Omit<OpeningHours, 'zone'>] || [];
  return sessions.map(parseSession);
}

/**
 * Check if current time is within a trading session
 */
function isWithinSession(now: Date, session: { start: string; end: string }, dayOfWeek: number): boolean {
  const nowMinutes = now.getUTCHours() * 60 + now.getUTCMinutes();
  const startMinutes = parseTimeToMinutes(session.start);
  const endMinutes = parseTimeToMinutes(session.end);
  
  // Handle sessions that cross midnight (e.g., "09:00 - 00:00")
  if (endMinutes < startMinutes || endMinutes === 0) {
    // Session crosses midnight
    return nowMinutes >= startMinutes || nowMinutes < endMinutes;
  }
  
  return nowMinutes >= startMinutes && nowMinutes < endMinutes;
}

/**
 * Calculate next market timing (open/close) based on opening hours
 */
export function calculateMarketTiming(openingHours: OpeningHours, now: Date = new Date()): MarketTiming {
  const dayNames = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
  const currentDayIndex = now.getUTCDay();
  const currentDayName = dayNames[currentDayIndex];
  
  // Get today's sessions
  const todaySessions = getSessionsForDay(openingHours, currentDayName);
  
  // Check if market is currently open
  let isOpen = false;
  let currentSession: { start: string; end: string } | null = null;
  
  for (const session of todaySessions) {
    if (isWithinSession(now, session, currentDayIndex)) {
      isOpen = true;
      currentSession = session;
      break;
    }
  }
  
  let nextOpen: Date | null = null;
  let nextClose: Date | null = null;
  
  if (isOpen && currentSession) {
    // Market is open, find next close time
    const endMinutes = parseTimeToMinutes(currentSession.end);
    
    if (endMinutes === 0 || endMinutes < parseTimeToMinutes(currentSession.start)) {
      // Session ends at midnight or crosses midnight
      // Check if there's a CONTINUATION session tomorrow starting at 00:00
      // (Extended hours: Monday 09:00-00:00 continues into Tuesday 00:00-01:00)
      const tomorrowIndex = (currentDayIndex + 1) % 7;
      const tomorrowName = dayNames[tomorrowIndex];
      const tomorrowSessions = getSessionsForDay(openingHours, tomorrowName);
      
      // Find continuation session starting at 00:00
      const continuationSession = tomorrowSessions.find(s => parseTimeToMinutes(s.start) === 0);
      
      if (continuationSession) {
        // Use the continuation session's end time (e.g., 01:00 for extended hours)
        const contEndMinutes = parseTimeToMinutes(continuationSession.end);
        nextClose = new Date(now);
        nextClose.setUTCDate(nextClose.getUTCDate() + 1);
        nextClose.setUTCHours(Math.floor(contEndMinutes / 60), contEndMinutes % 60, 0, 0);
      } else {
        // No continuation, close is at midnight
        nextClose = new Date(now);
        nextClose.setUTCDate(nextClose.getUTCDate() + 1);
        nextClose.setUTCHours(0, 0, 0, 0);
      }
    } else {
      nextClose = new Date(now);
      nextClose.setUTCHours(Math.floor(endMinutes / 60), endMinutes % 60, 0, 0);
    }
  } else {
    // Market is closed, find next open time AND next close time
    // First, check remaining sessions today
    const nowMinutes = now.getUTCHours() * 60 + now.getUTCMinutes();
    
    let foundSession: { start: string; end: string } | null = null;
    let daysToAdd = 0;
    
    for (const session of todaySessions) {
      const startMinutes = parseTimeToMinutes(session.start);
      if (startMinutes > nowMinutes) {
        nextOpen = new Date(now);
        nextOpen.setUTCHours(Math.floor(startMinutes / 60), startMinutes % 60, 0, 0);
        foundSession = session;
        break;
      }
    }
    
    // If no sessions left today, find next day with sessions
    if (!nextOpen) {
      for (let i = 1; i <= 7; i++) {
        const checkDayIndex = (currentDayIndex + i) % 7;
        const checkDayName = dayNames[checkDayIndex];
        const sessions = getSessionsForDay(openingHours, checkDayName);
        
        if (sessions.length > 0) {
          const firstSession = sessions[0];
          const startMinutes = parseTimeToMinutes(firstSession.start);
          nextOpen = new Date(now);
          nextOpen.setUTCDate(nextOpen.getUTCDate() + i);
          nextOpen.setUTCHours(Math.floor(startMinutes / 60), startMinutes % 60, 0, 0);
          foundSession = firstSession;
          daysToAdd = i;
          break;
        }
      }
    }
    
    // FIX: Also calculate nextClose based on the found session's end time
    // This is critical for scheduling trading windows for markets that open later today
    if (foundSession) {
      const endMinutes = parseTimeToMinutes(foundSession.end);
      
      if (endMinutes === 0 || endMinutes < parseTimeToMinutes(foundSession.start)) {
        // Session ends at midnight or crosses midnight (e.g., 09:00-00:00)
        // Check for continuation session tomorrow starting at 00:00
        const tomorrowIndex = (currentDayIndex + daysToAdd + 1) % 7;
        const tomorrowName = dayNames[tomorrowIndex];
        const tomorrowSessions = getSessionsForDay(openingHours, tomorrowName);
        
        const continuationSession = tomorrowSessions.find(s => parseTimeToMinutes(s.start) === 0);
        
        if (continuationSession) {
          // Extended hours: use continuation session's end (e.g., 01:00)
          const contEndMinutes = parseTimeToMinutes(continuationSession.end);
          nextClose = new Date(now);
          nextClose.setUTCDate(nextClose.getUTCDate() + daysToAdd + 1);
          nextClose.setUTCHours(Math.floor(contEndMinutes / 60), contEndMinutes % 60, 0, 0);
        } else {
          // No continuation, close is at midnight
          nextClose = new Date(now);
          nextClose.setUTCDate(nextClose.getUTCDate() + daysToAdd + 1);
          nextClose.setUTCHours(0, 0, 0, 0);
        }
      } else {
        // Session ends same day
        nextClose = new Date(now);
        nextClose.setUTCDate(nextClose.getUTCDate() + daysToAdd);
        nextClose.setUTCHours(Math.floor(endMinutes / 60), endMinutes % 60, 0, 0);
      }
    }
  }
  
  return {
    isOpen,
    nextOpen,
    nextClose,
    currentSession,
  };
}

/**
 * Fetch and update market hours for an epic
 */
export async function fetchAndUpdateMarketHours(
  client: CapitalComAPI,
  epic: string
): Promise<boolean> {
  try {
    orchestrationLogger.log({
      event: 'MARKET_HOURS_FETCH_STARTED',
      level: 'INFO',
      message: `Fetching market hours for ${epic}`,
      data: { epic },
    });

    const apiMarketInfo = await client.getMarketInfo(epic);
    
    if (!apiMarketInfo) {
      orchestrationLogger.log({
        event: 'MARKET_HOURS_FETCH_FAILED',
        level: 'ERROR',
        message: `Failed to fetch market info for ${epic}`,
        data: { epic },
      });
      return false;
    }

    // DEBUG: Log full API response to find session fields
    // TODO: Remove this after identifying the correct fields
    console.log(`[MarketHours-DEBUG] ${epic} FULL API RESPONSE:`, JSON.stringify(apiMarketInfo, null, 2));
    
    // Look for session-related fields that Capital.com might provide
    const instrument = apiMarketInfo.instrument as any;
    const snapshot = apiMarketInfo.snapshot as any;
    
    // Check for various possible field names for next session times
    const possibleSessionFields = [
      'nextSessionStart', 'nextSessionEnd', 
      'sessionStart', 'sessionEnd',
      'tradingSessionStart', 'tradingSessionEnd',
      'marketOpenTime', 'marketCloseTime',
      'nextOpen', 'nextClose'
    ];
    
    for (const field of possibleSessionFields) {
      if (instrument?.[field]) {
        console.log(`[MarketHours-DEBUG] ${epic} instrument.${field}:`, instrument[field]);
      }
      if (snapshot?.[field]) {
        console.log(`[MarketHours-DEBUG] ${epic} snapshot.${field}:`, snapshot[field]);
      }
      if ((apiMarketInfo as any)?.[field]) {
        console.log(`[MarketHours-DEBUG] ${epic} root.${field}:`, (apiMarketInfo as any)[field]);
      }
    }

    const openingHours = apiMarketInfo.instrument?.openingHours as OpeningHours | undefined;
    const marketStatus = apiMarketInfo.snapshot?.marketStatus as 'TRADEABLE' | 'CLOSED' | undefined;

    if (!openingHours) {
      orchestrationLogger.log({
        event: 'MARKET_HOURS_FETCH_FAILED',
        level: 'WARN',
        message: `No opening hours data for ${epic}`,
        data: { epic, marketStatus },
      });
      return false;
    }

    // Calculate next open/close times
    const timing = calculateMarketTiming(openingHours);

    // Extract market parameters
    const bid = apiMarketInfo.snapshot?.bid;
    const offer = apiMarketInfo.snapshot?.offer;
    const spreadPercent = (bid && offer) ? ((offer - bid) / bid) * 100 : 0;
    
    const minContractSize = apiMarketInfo.dealingRules?.minDealSize?.value || 0;
    const maxContractSize = apiMarketInfo.dealingRules?.maxDealSize?.value || 0;
    
    const overnightFundingLong = apiMarketInfo.instrument?.overnightFee?.longRate || 0;
    const overnightFundingShort = apiMarketInfo.instrument?.overnightFee?.shortRate || 0;
    
    // Extract GSL (Guaranteed Stop Loss) settings
    const dealingRules = apiMarketInfo.dealingRules || {};
    const minGuaranteedStopDistancePct = dealingRules.minGuaranteedStopDistance?.value || null;
    const guaranteedStopAllowed = (apiMarketInfo.instrument as any)?.guaranteedStopAllowed ?? false;
    
    // Calculate scaling factor (decimal places) from bid price
    let scalingFactor = 2; // Default for USD equities
    if (bid && bid > 0) {
      const bidStr = bid.toString();
      const decimalIndex = bidStr.indexOf('.');
      if (decimalIndex !== -1) {
        const decimals = bidStr.length - decimalIndex - 1;
        scalingFactor = Math.min(decimals, 5);
      } else {
        scalingFactor = 0;
      }
    }
    
    // Extract instrument name
    const instrumentName = apiMarketInfo.instrument?.name || null;

    // Calculate market open/close times
    // For extended hours: Monday ends at 00:00, Tuesday continues 00:00-01:00
    // So the REAL close time is 01:00 (from Tuesday's first session end)
    const mondaySessions = openingHours.mon || [];
    const tuesdaySessions = openingHours.tue || [];
    
    const firstSession = mondaySessions[0] || '09:00 - 22:00';
    const [openTime, mondayCloseTime] = firstSession.split(' - ').map((t: string) => t.trim());
    
    // Check if this is extended hours (Monday ends at 00:00, continues Tuesday)
    let closeTime = mondayCloseTime;
    
    if (mondayCloseTime === '00:00' && tuesdaySessions.length > 0) {
      // Extended hours: Tuesday's first session is the continuation (00:00 - 01:00)
      const tuesdayFirst = tuesdaySessions[0];
      const [tueStart, tueEnd] = tuesdayFirst.split(' - ').map((t: string) => t.trim());
      
      // If Tuesday starts at 00:00, the market continues overnight
      // The real close time is when Tuesday's first session ends (01:00)
      if (tueStart === '00:00') {
        closeTime = tueEnd; // This is 01:00 for extended hours
        console.log(`[MarketHours] ${epic}: Extended hours detected - real close is ${closeTime} UTC`);
      }
    }
    
    // Determine trading hours type based on close time
    // Regular hours: close at 16:00 ET (21:00 UTC)
    // Extended hours: close at 20:00 ET (01:00 UTC next day)
    const closeHour = parseInt(closeTime.split(':')[0]);
    const tradingHoursType = (closeHour >= 0 && closeHour <= 2) ? 'extended' : 'regular';

    // Update database
    const db = await getDb();
    if (!db) {
      orchestrationLogger.log({
        event: 'MARKET_HOURS_UPDATE_FAILED',
        level: 'ERROR',
        message: 'Database not available',
        data: { epic },
      });
      return false;
    }

    // Update epics table with market hours and name
    await db
      .update(epics)
      .set({
        name: instrumentName,
        marketStatus: marketStatus || 'CLOSED',
        tradingHoursType: tradingHoursType as 'regular' | 'extended',
        openingHours: openingHours as any,
        nextOpen: timing.nextOpen,
        nextClose: timing.nextClose,
        marketHoursLastFetched: new Date(),
      })
      .where(eq(epics.symbol, epic));

    // Upsert marketInfo table with market parameters + GSL fields
    try {
      await db
        .insert(marketInfo)
        .values({
          epic,
          spreadPercent: spreadPercent.toFixed(6),
          minContractSize: minContractSize.toFixed(2),
          maxContractSize: maxContractSize.toFixed(2),
          overnightFundingLongPercent: overnightFundingLong.toFixed(10),
          overnightFundingShortPercent: overnightFundingShort.toFixed(10),
          marketOpenTime: openTime,
          marketCloseTime: closeTime,
          minGuaranteedStopDistancePct: minGuaranteedStopDistancePct?.toFixed(4) || null,
          guaranteedStopAllowed: guaranteedStopAllowed,
          scalingFactor: scalingFactor,
          currency: 'USD',
          isActive: true,
        })
        .onDuplicateKeyUpdate({
          set: {
            spreadPercent: spreadPercent.toFixed(6),
            minContractSize: minContractSize.toFixed(2),
            maxContractSize: maxContractSize.toFixed(2),
            overnightFundingLongPercent: overnightFundingLong.toFixed(10),
            overnightFundingShortPercent: overnightFundingShort.toFixed(10),
            marketOpenTime: openTime,
            marketCloseTime: closeTime,
            minGuaranteedStopDistancePct: minGuaranteedStopDistancePct?.toFixed(4) || null,
            guaranteedStopAllowed: guaranteedStopAllowed,
            scalingFactor: scalingFactor,
            updatedAt: new Date(),
          },
        });
      
      console.log(`[MarketHours] ${epic}: GSL allowed=${guaranteedStopAllowed}, min distance=${minGuaranteedStopDistancePct}%, scaling=${scalingFactor}`);
    } catch (error: any) {
      orchestrationLogger.log({
        event: 'MARKET_HOURS_UPDATE_FAILED',
        level: 'WARN',
        message: `Failed to update market parameters for ${epic}: ${error.message}`,
        data: { epic },
        error: error.message,
      });
      // Don't fail the entire operation if market params update fails
    }

    orchestrationLogger.log({
      event: 'MARKET_HOURS_FETCHED',
      level: 'INFO',
      message: `Market hours updated for ${epic}`,
      data: {
        epic,
        marketStatus,
        isOpen: timing.isOpen,
        nextOpen: timing.nextOpen?.toISOString(),
        nextClose: timing.nextClose?.toISOString(),
      },
    });

    return true;
  } catch (error: any) {
    orchestrationLogger.log({
      event: 'MARKET_HOURS_FETCH_FAILED',
      level: 'ERROR',
      message: `Error fetching market hours for ${epic}: ${error.message}`,
      data: { epic },
      error: error.message,
    });
    return false;
  }
}

/**
 * Fetch and update market hours for multiple epics
 */
export async function fetchAndUpdateMultipleMarketHours(
  client: CapitalComAPI,
  epicSymbols: string[]
): Promise<{ success: number; failed: number }> {
  let success = 0;
  let failed = 0;

  for (const epic of epicSymbols) {
    const result = await fetchAndUpdateMarketHours(client, epic);
    if (result) {
      success++;
    } else {
      failed++;
    }
    
    // Rate limiting: wait 200ms between requests
    await new Promise(resolve => setTimeout(resolve, 200));
  }

  return { success, failed };
}

/**
 * Format countdown for display
 * @param milliseconds Time until event in milliseconds
 * @returns Formatted string like "2h 15m 30s"
 */
export function formatCountdown(milliseconds: number): string {
  if (milliseconds < 0) return 'Overdue';
  
  const seconds = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (days > 0) {
    return `${days}d ${hours % 24}h ${minutes % 60}m`;
  } else if (hours > 0) {
    return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
  } else if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  } else {
    return `${seconds}s`;
  }
}

/**
 * Format time in dual timezone (ET + GST)
 * @param date Date to format
 * @returns Formatted string like "9:30 AM ET (5:30 PM GST)"
 */
export function formatDualTimezone(date: Date): string {
  // ET (Eastern Time) - UTC-5 (EST) or UTC-4 (EDT)
  const etFormatter = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
  const etTime = etFormatter.format(date);

  // GST (Gulf Standard Time) - UTC+4
  const gstFormatter = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Dubai',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
  const gstTime = gstFormatter.format(date);

  return `${etTime} ET (${gstTime} GST)`;
}
