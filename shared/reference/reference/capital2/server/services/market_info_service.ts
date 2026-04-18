/**
 * Market Info Service
 * 
 * Fetches and stores market information from Capital.com API:
 * - Spread, contract sizes, lot size
 * - Overnight funding rates
 * - Leverage options for the instrument type
 * - Stop loss distance rules
 * 
 * This data is used by:
 * - Backtest runner (leverage options)
 * - Position sizing (contract sizes, lot size)
 * - Trade execution (spread, stop rules)
 */

import { getDb } from '../db';
import { marketInfo } from '../../drizzle/schema';
import { eq } from 'drizzle-orm';
import { CapitalComAPI } from '../live_trading/capital_api';

export interface MarketInfoUpdate {
  epic: string;
  name?: string;
  instrumentType?: string;
  spreadPercent: number;
  minContractSize: number;
  maxContractSize: number;
  minSizeIncrement: number;
  lotSize: number;
  leverageOptions: number[];
  marginFactor?: number;
  overnightFundingLongPercent: number;
  overnightFundingShortPercent: number;
  minStopDistancePct?: number;
  maxStopDistancePct?: number;
  minGuaranteedStopDistancePct?: number;
  // GSL (Guaranteed Stop Loss) fields
  guaranteedStopAllowed?: boolean;  // Whether instrument supports GSL
  scalingFactor?: number;            // Number of decimal places for price rounding
  currency: string;
}

/**
 * Fetch market info from Capital.com and store in database
 * 
 * @param client - Authenticated Capital.com API client
 * @param epic - The epic/symbol to fetch info for (e.g., "SOXL")
 * @returns The fetched market info, or null if failed
 */
export async function fetchAndStoreMarketInfo(
  client: CapitalComAPI,
  epic: string
): Promise<MarketInfoUpdate | null> {
  console.log(`[MarketInfoService] Fetching market info for ${epic}...`);
  
  try {
    // 1. Get market info from Capital.com
    const marketInfoResponse = await client.getMarketInfo(epic);
    
    if (!marketInfoResponse) {
      console.error(`[MarketInfoService] Failed to get market info for ${epic}`);
      return null;
    }
    
    // 2. Extract instrument type to get leverage options
    const instrumentType = marketInfoResponse.instrument?.type || 'SHARES';
    
    // 3. Get leverage options for this instrument type
    const leverageOptions = await client.getLeverageOptionsForType(instrumentType);
    
    console.log(`[MarketInfoService] ${epic} is type ${instrumentType}, leverage options: ${leverageOptions.join(', ')}`);
    
    // 4. Calculate spread percentage from bid/offer
    const bid = marketInfoResponse.snapshot?.bid || 0;
    const offer = marketInfoResponse.snapshot?.offer || 0;
    const spreadPercent = bid > 0 ? ((offer - bid) / bid) * 100 : 0;
    
    // 5. Extract dealing rules
    const dealingRules = marketInfoResponse.dealingRules || {};
    const minDealSize = dealingRules.minDealSize?.value || 1;
    const maxDealSize = dealingRules.maxDealSize?.value || 10000;
    const minSizeIncrement = dealingRules.minSizeIncrement?.value || 1;
    const minStopDistancePct = dealingRules.minStopOrProfitDistance?.value;
    const maxStopDistancePct = dealingRules.maxStopOrProfitDistance?.value;
    const minGuaranteedStopDistancePct = dealingRules.minGuaranteedStopDistance?.value;
    
    // 5b. Extract GSL (Guaranteed Stop Loss) settings
    // Capital.com provides guaranteedStopAllowed on the instrument object
    const guaranteedStopAllowed = (marketInfoResponse.instrument as any)?.guaranteedStopAllowed ?? false;
    
    // Calculate scaling factor (decimal places) from the bid price
    // This determines how many decimal places to use when rounding stop levels
    // e.g., 40.25 = 2 decimals, 1.12345 = 5 decimals, 39500 = 0 decimals
    let scalingFactor = 2; // Default for USD equities
    if (bid > 0) {
      const bidStr = bid.toString();
      const decimalIndex = bidStr.indexOf('.');
      if (decimalIndex !== -1) {
        // Count digits after decimal point (excluding trailing zeros in some cases)
        const decimals = bidStr.length - decimalIndex - 1;
        scalingFactor = Math.min(decimals, 5); // Cap at 5 decimals
      } else {
        // No decimal point - round to whole numbers
        scalingFactor = 0;
      }
    }
    
    // 6. Extract instrument details
    const instrument = marketInfoResponse.instrument || {};
    const lotSize = instrument.lotSize || 1;
    const currency = instrument.currency || 'USD';
    const overnightFee = instrument.overnightFee || {};
    
    // DEBUG: Log raw API values
    console.log(`[MarketInfoService] RAW API VALUES for ${epic}:`);
    console.log(`[MarketInfoService]   - instrument.marginFactor: ${instrument.marginFactor}`);
    console.log(`[MarketInfoService]   - overnightFee.longRate: ${overnightFee.longRate}`);
    console.log(`[MarketInfoService]   - overnightFee.shortRate: ${overnightFee.shortRate}`);
    
    // Convert marginFactor from Capital.com format to decimal
    // Capital.com returns margin as percentage (e.g., 20 = 20%, 5 = 5%)
    // But our backtest code expects decimal (e.g., 0.2 = 20%, 0.05 = 5%)
    // Safeguard: if value is > 1, it's a percentage and needs conversion
    // If value is <= 1, it's already a decimal (e.g., 0.2)
    let marginFactor = instrument.marginFactor;
    if (marginFactor !== undefined && marginFactor !== null) {
      if (marginFactor > 1) {
        // Value is a percentage (e.g., 20), convert to decimal (e.g., 0.2)
        marginFactor = marginFactor / 100;
        console.log(`[MarketInfoService]   - marginFactor converted: ${instrument.marginFactor}% -> ${marginFactor}`);
      }
      // else: already a decimal, use as-is
    }
    
    // 7. Build the update object
    const update: MarketInfoUpdate = {
      epic,
      name: instrument.name,
      instrumentType,
      spreadPercent: parseFloat(spreadPercent.toFixed(6)),
      minContractSize: minDealSize,
      maxContractSize: maxDealSize,
      minSizeIncrement,
      lotSize,
      leverageOptions,
      marginFactor,
      overnightFundingLongPercent: overnightFee.longRate || 0,
      overnightFundingShortPercent: overnightFee.shortRate || 0,
      minStopDistancePct,
      maxStopDistancePct,
      minGuaranteedStopDistancePct,
      guaranteedStopAllowed,
      scalingFactor,
      currency,
    };
    
    // 8. Store in database
    await upsertMarketInfo(update);
    
    console.log(`[MarketInfoService] ✓ Stored market info for ${epic}`);
    console.log(`[MarketInfoService]   - Spread: ${update.spreadPercent.toFixed(4)}%`);
    console.log(`[MarketInfoService]   - Min Size: ${update.minContractSize}, Max Size: ${update.maxContractSize}`);
    console.log(`[MarketInfoService]   - Increment: ${update.minSizeIncrement}, Lot Size: ${update.lotSize}`);
    console.log(`[MarketInfoService]   - Leverage Options: [${update.leverageOptions.join(', ')}]`);
    console.log(`[MarketInfoService]   - Overnight Long: ${(update.overnightFundingLongPercent * 100).toFixed(4)}%`);
    console.log(`[MarketInfoService]   - Overnight Short: ${(update.overnightFundingShortPercent * 100).toFixed(4)}%`);
    console.log(`[MarketInfoService]   - GSL Allowed: ${update.guaranteedStopAllowed ? 'YES' : 'NO'}`);
    console.log(`[MarketInfoService]   - Min GSL Distance: ${update.minGuaranteedStopDistancePct ?? 'N/A'}%`);
    console.log(`[MarketInfoService]   - Scaling Factor (decimals): ${update.scalingFactor}`);
    
    return update;
    
  } catch (error: any) {
    console.error(`[MarketInfoService] Error fetching market info for ${epic}:`, error.message);
    return null;
  }
}

/**
 * Upsert market info into database
 */
async function upsertMarketInfo(update: MarketInfoUpdate): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error('Database not available');
  
  // Check if record exists
  const existing = await db
    .select()
    .from(marketInfo)
    .where(eq(marketInfo.epic, update.epic))
    .limit(1);
  
  const dbValues = {
    epic: update.epic,
    name: update.name,
    instrumentType: update.instrumentType,
    spreadPercent: update.spreadPercent.toString(),
    minContractSize: update.minContractSize.toString(),
    maxContractSize: update.maxContractSize.toString(),
    minSizeIncrement: update.minSizeIncrement.toString(),
    lotSize: update.lotSize.toString(),
    leverageOptions: update.leverageOptions,
    marginFactor: update.marginFactor?.toString(),
    overnightFundingLongPercent: update.overnightFundingLongPercent.toString(),
    overnightFundingShortPercent: update.overnightFundingShortPercent.toString(),
    minStopDistancePct: update.minStopDistancePct?.toString(),
    maxStopDistancePct: update.maxStopDistancePct?.toString(),
    minGuaranteedStopDistancePct: update.minGuaranteedStopDistancePct?.toString(),
    guaranteedStopAllowed: update.guaranteedStopAllowed ?? false,
    scalingFactor: update.scalingFactor ?? 2,
    currency: update.currency,
    marketOpenTime: '09:30:00', // Default US market hours
    marketCloseTime: '16:00:00',
    lastFetchedFromCapital: new Date(),
    isActive: true,
  };
  
  if (existing.length > 0) {
    // Update existing
    await db
      .update(marketInfo)
      .set(dbValues)
      .where(eq(marketInfo.epic, update.epic));
  } else {
    // Insert new
    await db.insert(marketInfo).values(dbValues);
  }
}

/**
 * Get leverage options for an epic from the database
 * 
 * @param epic - The epic/symbol to get leverage options for
 * @returns Array of valid leverage values, or default [1,2,3,4,5,10,20] if not found
 */
export async function getLeverageOptionsForEpic(epic: string): Promise<number[]> {
  const db = await getDb();
  if (!db) {
    console.warn('[MarketInfoService] Database not available, returning default leverage options');
    return [1, 2, 3, 4, 5, 10, 20];
  }
  
  const result = await db
    .select({ leverageOptions: marketInfo.leverageOptions })
    .from(marketInfo)
    .where(eq(marketInfo.epic, epic))
    .limit(1);
  
  if (result.length > 0 && result[0].leverageOptions) {
    return result[0].leverageOptions;
  }
  
  // Default leverage options for SHARES if not found
  console.warn(`[MarketInfoService] No leverage options found for ${epic}, returning default`);
  return [1, 2, 3, 4, 5, 10, 20];
}

/**
 * Get full market info for an epic from the database
 */
export async function getMarketInfoForEpic(epic: string): Promise<any | null> {
  const db = await getDb();
  if (!db) return null;
  
  const result = await db
    .select()
    .from(marketInfo)
    .where(eq(marketInfo.epic, epic))
    .limit(1);
  
  return result.length > 0 ? result[0] : null;
}

/**
 * Refresh market info for all active epics
 */
export async function refreshAllMarketInfo(client: CapitalComAPI): Promise<{ success: number; failed: number }> {
  const db = await getDb();
  if (!db) throw new Error('Database not available');
  
  // Get all active market info records
  const allEpics = await db
    .select({ epic: marketInfo.epic })
    .from(marketInfo)
    .where(eq(marketInfo.isActive, true));
  
  let success = 0;
  let failed = 0;
  
  for (const { epic } of allEpics) {
    const result = await fetchAndStoreMarketInfo(client, epic);
    if (result) {
      success++;
    } else {
      failed++;
    }
    
    // Small delay to avoid rate limiting
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  
  console.log(`[MarketInfoService] Refreshed ${success} epics, ${failed} failed`);
  return { success, failed };
}

