/**
 * Trade Sync Service
 * 
 * Provides centralized trade synchronization from Capital.com
 * Used by:
 * - API routes (sync-trades endpoint)
 * - Timer (nightly sync)
 * - Trade poller (hourly checks)
 */

import { getDb } from '../db';
import { eq, desc, and, isNull, gt, lt, or } from 'drizzle-orm';

export interface SyncOptions {
  days?: number;           // Days to fetch (default 30)
  fullSync?: boolean;      // Full sync (ignores incremental)
  incrementalSync?: boolean; // Start from last synced trade
}

export interface SyncResult {
  success: boolean;
  newTrades?: number;
  updatedTrades?: number;
  totalActivities?: number;
  error?: string;
}

/**
 * Sync trades for a single account
 */
export async function syncTradesForAccount(
  accountId: number,
  options: SyncOptions = {}
): Promise<SyncResult> {
  const {
    days = 30,
    fullSync = false,
    incrementalSync = true
  } = options;

  const db = await getDb();
  if (!db) {
    return { success: false, error: 'Database not available' };
  }

  try {
    const { accounts, actualTrades } = await import('../../drizzle/schema');
    
    // Get account details
    const account = await db
      .select()
      .from(accounts)
      .where(eq(accounts.id, accountId))
      .limit(1);

    if (account.length === 0) {
      return { success: false, error: 'Account not found' };
    }

    const acc = account[0];
    const environment = acc.accountType === 'demo' ? 'demo' : 'live';
    
    // Create API client
    const { createCapitalAPIClient } = await import('../live_trading/credentials');
    const client = await createCapitalAPIClient(environment);
    
    if (!client) {
      return { success: false, error: 'Failed to create API client' };
    }

    // Switch to account
    let capitalAccountId = acc.capitalAccountId;
    if (!capitalAccountId) {
      const capitalAccounts = await client.getAccounts();
      const matchingAccount = capitalAccounts.find(a => a.accountName === acc.accountName);
      if (matchingAccount) {
        capitalAccountId = matchingAccount.accountId;
      } else {
        return { success: false, error: `Could not find Capital.com account for ${acc.accountName}` };
      }
    }
    await client.switchAccount(capitalAccountId);

    // Determine sync start date
    let startDate: Date;
    const syncDays = Math.min(Math.max(1, days), 365);
    
    if (fullSync) {
      startDate = new Date();
      startDate.setDate(startDate.getDate() - syncDays);
    } else if (incrementalSync) {
      // Check last synced trade
      const latestTrade = await db
        .select()
        .from(actualTrades)
        .where(eq(actualTrades.accountId, accountId))
        .orderBy(desc(actualTrades.capitalExecutedAt))
        .limit(1);
      
      if (latestTrade.length > 0 && latestTrade[0].capitalExecutedAt) {
        startDate = new Date(latestTrade[0].capitalExecutedAt);
        startDate.setDate(startDate.getDate() - 2); // 2-day overlap
      } else {
        startDate = new Date();
        startDate.setDate(startDate.getDate() - syncDays);
      }
    } else {
      startDate = new Date();
      startDate.setDate(startDate.getDate() - syncDays);
    }
    
    const endDate = new Date();
    const actualDaysToFetch = Math.ceil((endDate.getTime() - startDate.getTime()) / (24 * 60 * 60 * 1000));
    
    // Format date for Capital.com API
    const formatDate = (d: Date) => d.toISOString().split('.')[0];
    
    let allActivities: any[] = [];
    let newTrades = 0;
    let updatedTrades = 0;
    
    // Loop through each day
    for (let daysBack = 0; daysBack < actualDaysToFetch; daysBack++) {
      const dayEnd = new Date();
      dayEnd.setDate(dayEnd.getDate() - daysBack);
      dayEnd.setHours(23, 59, 59, 0);
      
      const dayStart = new Date(dayEnd);
      dayStart.setHours(0, 0, 0, 0);
      
      if (dayStart < startDate) break;
      
      const from = formatDate(dayStart);
      const to = formatDate(dayEnd);
      
      try {
        const dayActivities = await client.getActivityHistory(from, to);
        if (dayActivities?.length > 0) {
          allActivities.push(...dayActivities);
        }
      } catch (error: any) {
        console.error(`[SyncService] Error fetching day ${dayStart.toISOString().split('T')[0]}:`, error.message);
      }
      
      // Rate limit
      await new Promise(r => setTimeout(r, 200));
    }
    
    // Process activities and create/update trades
    // Filter for position-related activities (POSITION, ORDER)
    const tradeActivities = allActivities.filter(a => 
      a.type === 'POSITION' || a.type === 'ORDER'
    );
    
    for (const activity of tradeActivities) {
      try {
        const dealId = activity.dealId;
        if (!dealId) continue;
        
        // Check if trade exists by dealId
        const existingTrade = await db
          .select()
          .from(actualTrades)
          .where(
            and(
              eq(actualTrades.accountId, accountId),
              eq(actualTrades.dealId, dealId)
            )
          )
          .limit(1);
        
        if (existingTrade.length === 0) {
          // New trade - insert
          // Note: This creates a "synced" trade, not a brain-generated one
          // The UI will show these but won't have brain data
          const windowTime = activity.dateUtc ? new Date(activity.dateUtc) : new Date();
          const windowCloseTime = `${String(windowTime.getUTCHours()).padStart(2, '0')}:00:00`;
          
          await db.insert(actualTrades).values({
            accountId,
            strategyId: 0, // Unknown - synced from Capital.com
            windowCloseTime,
            epic: activity.epic || 'UNKNOWN',
            direction: activity.direction || 'BUY',
            dealId,
            dealReference: activity.dealReference,
            entryPrice: activity.level ? parseFloat(activity.level) : null,
            contracts: activity.size ? parseFloat(activity.size) : null,
            tradeSource: 'manual', // Synced = manual (not app-generated)
            brainDecision: 'BUY',
            leverage: 1,
            status: activity.status === 'OPEN' ? 'open' : 
                   activity.status === 'CLOSED' ? 'closed' : 'pending',
            capitalExecutedAt: activity.dateUtc ? new Date(activity.dateUtc) : null,
          });
          newTrades++;
        } else {
          // Update existing trade with Capital.com data
          const updates: any = {};
          
          if (activity.status === 'CLOSED' && existingTrade[0].status !== 'closed') {
            updates.status = 'closed';
            updates.exitPrice = activity.level ? parseFloat(activity.level) : null;
          }
          
          if (Object.keys(updates).length > 0) {
            await db
              .update(actualTrades)
              .set(updates)
              .where(eq(actualTrades.id, existingTrade[0].id));
            updatedTrades++;
          }
        }
      } catch (error: any) {
        console.error(`[SyncService] Error processing activity:`, error.message);
      }
    }
    
    return {
      success: true,
      newTrades,
      updatedTrades,
      totalActivities: allActivities.length
    };
    
  } catch (error: any) {
    console.error(`[SyncService] Sync failed for account ${accountId}:`, error.message);
    return { success: false, error: error.message };
  }
}
