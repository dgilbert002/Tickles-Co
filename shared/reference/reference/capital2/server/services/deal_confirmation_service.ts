/**
 * Deal Confirmation Service (CONSOLIDATED)
 * 
 * SINGLE SOURCE OF TRUTH for confirming Capital.com trades.
 * 
 * Capital.com's 2-step workflow:
 * 1. POST /positions returns dealReference with "o_" prefix (Order)
 * 2. GET /confirms/{dealReference} returns actual dealId (Position)
 * 
 * The dealId from confirmation is what appears in positions and should be
 * used for closing trades. The affectedDeals[0].dealId is the actual position
 * that can be closed via DELETE /positions/{dealId}.
 * 
 * Usage:
 * - import { confirmDeal, confirmMultipleDeals } from './services/deal_confirmation_service';
 * - const result = await confirmDeal(client, dealReference, accountId);
 */

import { orchestrationLogger } from '../orchestration/logger';
import { apiQueue } from '../orchestration/api_queue';

export interface DealConfirmationResult {
  dealReference: string;
  status: 'confirmed' | 'rejected' | 'pending' | 'timeout' | 'error';
  
  // Confirmed deal info
  dealId?: string;           // Position ID for closing (from affectedDeals[0].dealId)
  orderDealId?: string;      // Order ID from confirmation.dealId
  dealReferenceConfirmed?: string;  // dealReference with p_ prefix
  
  // Trade details
  epic?: string;
  direction?: 'BUY' | 'SELL';
  size?: number;
  entryPrice?: number;
  stopLevel?: number;
  
  // Execution time
  capitalExecutedAt?: Date;
  
  // Error info
  error?: string;
  rejectReason?: string;
  
  // Metadata
  attempts?: number;
  verifiedInPositions?: boolean;
}

export interface DealConfirmationOptions {
  maxRetries?: number;        // default: 5
  initialDelayMs?: number;    // default: 1000
  maxDelayMs?: number;        // default: 10000
  useExponentialBackoff?: boolean;  // default: true
  verifyInPositions?: boolean;      // default: true
  useCriticalPriority?: boolean;    // default: true (bypasses quiet period)
}

const DEFAULT_OPTIONS: Required<DealConfirmationOptions> = {
  maxRetries: 5,
  initialDelayMs: 1000,
  maxDelayMs: 10000,
  useExponentialBackoff: true,
  verifyInPositions: true,
  useCriticalPriority: true,
};

/**
 * Sleep helper
 */
function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Confirm a single deal reference and get the actual deal ID
 * 
 * @param client - Capital.com API client (CapitalComAPI instance)
 * @param dealReference - The reference returned from createPosition (o_xxx)
 * @param accountId - Database account ID (for logging)
 * @param options - Configuration options
 * @returns Confirmation result with dealId and trade details
 */
export async function confirmDeal(
  client: any,
  dealReference: string,
  accountId: number,
  options: DealConfirmationOptions = {}
): Promise<DealConfirmationResult> {
  const opts = { ...DEFAULT_OPTIONS, ...options };
  
  const result: DealConfirmationResult = {
    dealReference,
    status: 'pending',
    attempts: 0,
  };

  // Skip if already has p_ prefix (already confirmed)
  if (dealReference.startsWith('p_')) {
    result.status = 'confirmed';
    result.dealId = dealReference.substring(2);
    result.dealReferenceConfirmed = dealReference;
    result.verifiedInPositions = false;
    
    await orchestrationLogger.info('DEAL_CONFIRMATION_SKIPPED',
      `DealReference ${dealReference} already has p_ prefix`,
      { accountId, data: { dealReference } }
    );
    
    return result;
  }

  let delay = opts.initialDelayMs;
  
  for (let attempt = 1; attempt <= opts.maxRetries; attempt++) {
    result.attempts = attempt;
    
    try {
      // Wait before attempt (including first)
      await sleep(attempt === 1 ? opts.initialDelayMs : delay);
      
      await orchestrationLogger.info('DEAL_CONFIRMATION_ATTEMPT',
        `Confirming ${dealReference} (attempt ${attempt}/${opts.maxRetries})`,
        { accountId, data: { dealReference, attempt, delayMs: delay } }
      );

      // Get confirmation from Capital.com
      let confirmation: any;
      
      if (opts.useCriticalPriority) {
        // Use API queue with critical priority (bypasses quiet period)
        confirmation = await new Promise<any>((resolve, reject) => {
          apiQueue.enqueue({
            priority: 'critical',
            isCritical: true,
            operation: 'confirm_deal',
            description: `GET /confirms/${dealReference}`,
            fn: async () => {
              return await client.getConfirmation(dealReference);
            },
            maxRetries: 1,
            onSuccess: resolve,
            onError: reject,
          });
        });
      } else {
        confirmation = await client.getConfirmation(dealReference);
      }

      if (!confirmation) {
        await orchestrationLogger.warn('DEAL_CONFIRMATION_PENDING',
          `No confirmation yet for ${dealReference} (attempt ${attempt})`,
          { accountId, data: { dealReference, attempt } }
        );
        
        if (opts.useExponentialBackoff) {
          delay = Math.min(delay * 2, opts.maxDelayMs);
        }
        continue;
      }

      // Check deal status
      if (confirmation.dealStatus === 'REJECTED') {
        result.status = 'rejected';
        result.rejectReason = confirmation.rejectReason || confirmation.reason || 'Unknown';
        result.error = `REJECTED: ${result.rejectReason}`;
        result.epic = confirmation.epic;
        result.direction = confirmation.direction;
        result.size = confirmation.size;
        
        await orchestrationLogger.warn('DEAL_CONFIRMATION_REJECTED',
          `Deal ${dealReference} REJECTED: ${result.rejectReason}`,
          { accountId, data: { dealReference, rejectReason: result.rejectReason, epic: result.epic } }
        );
        
        return result;
      }

      if (confirmation.dealStatus === 'PENDING') {
        await orchestrationLogger.info('DEAL_CONFIRMATION_PENDING',
          `Deal ${dealReference} still PENDING (attempt ${attempt})`,
          { accountId, data: { dealReference, attempt } }
        );
        
        if (opts.useExponentialBackoff) {
          delay = Math.min(delay * 2, opts.maxDelayMs);
        }
        continue;
      }

      // ACCEPTED - Extract deal info
      // IMPORTANT: Use affectedDeals[0].dealId for the position ID
      const positionDealId = confirmation.affectedDeals?.[0]?.dealId || confirmation.dealId;
      const orderDealId = confirmation.dealId;
      
      result.status = 'confirmed';
      result.dealId = positionDealId;
      result.orderDealId = orderDealId;
      result.dealReferenceConfirmed = confirmation.dealReference;
      result.epic = confirmation.epic;
      result.direction = confirmation.direction;
      result.size = confirmation.size;
      result.entryPrice = confirmation.level;
      result.stopLevel = confirmation.stopLevel;
      
      // Parse Capital.com's execution timestamp
      if (confirmation.dateUTC || confirmation.date) {
        const dateStr = confirmation.dateUTC || confirmation.date;
        const utcDateStr = dateStr.endsWith('Z') ? dateStr : dateStr + 'Z';
        result.capitalExecutedAt = new Date(utcDateStr);
      }

      // Optionally verify dealId exists in positions
      if (opts.verifyInPositions) {
        try {
          let positions: any[];
          
          if (opts.useCriticalPriority) {
            positions = await new Promise<any[]>((resolve, reject) => {
              apiQueue.enqueue({
                priority: 'critical',
                isCritical: true,
                operation: 'verify_position',
                description: 'GET /positions (verify)',
                fn: async () => client.getPositions(),
                maxRetries: 1,
                onSuccess: (pos) => resolve(pos || []),
                onError: reject,
              });
            });
          } else {
            positions = await client.getPositions() || [];
          }

          const matchingPosition = positions.find((p: any) => p.dealId === positionDealId);
          result.verifiedInPositions = !!matchingPosition;
          
          if (!matchingPosition && !confirmation.affectedDeals?.[0]?.dealId) {
            // Try to find by epic if affectedDeals was empty
            const epicMatch = positions.find((p: any) => p.market?.epic === confirmation.epic);
            if (epicMatch) {
              result.dealId = epicMatch.dealId;
              result.verifiedInPositions = true;
              
              await orchestrationLogger.info('DEAL_CONFIRMATION_SUCCESS',
                `Found position by epic match: ${epicMatch.dealId}`,
                { accountId, data: { dealReference, epic: confirmation.epic, dealId: epicMatch.dealId } }
              );
            }
          }
          
        } catch (error: any) {
          await orchestrationLogger.warn('DEAL_CONFIRMATION_SUCCESS',
            `Confirmed but couldn't verify in positions: ${error.message}`,
            { accountId, data: { dealReference, dealId: positionDealId } }
          );
          result.verifiedInPositions = false;
        }
      }

      await orchestrationLogger.info('DEAL_CONFIRMATION_SUCCESS',
        `Deal ${dealReference} CONFIRMED: dealId=${positionDealId}, price=${result.entryPrice}`,
        { accountId, data: { 
          dealReference, 
          dealId: positionDealId, 
          orderDealId,
          entryPrice: result.entryPrice,
          size: result.size,
          attempts: attempt,
          verified: result.verifiedInPositions,
        }}
      );

      return result;

    } catch (error: any) {
      await orchestrationLogger.warn('DEAL_CONFIRMATION_ERROR',
        `Error confirming ${dealReference} (attempt ${attempt}): ${error.message}`,
        { accountId, data: { dealReference, attempt, error: error.message } }
      );
      
      if (opts.useExponentialBackoff) {
        delay = Math.min(delay * 2, opts.maxDelayMs);
      }
    }
  }

  // All retries exhausted
  result.status = 'timeout';
  result.error = `Failed to confirm after ${opts.maxRetries} attempts`;
  
  await orchestrationLogger.warn('DEAL_CONFIRMATION_TIMEOUT',
    `Deal ${dealReference} confirmation timed out after ${opts.maxRetries} attempts`,
    { accountId, data: { dealReference, maxRetries: opts.maxRetries } }
  );

  return result;
}

/**
 * Confirm multiple deals with rate limiting
 * 
 * @param client - Capital.com API client
 * @param deals - Array of {dealReference, accountId} pairs
 * @param options - Configuration options
 * @param concurrency - Max concurrent confirmations (default: 2)
 * @returns Array of confirmation results
 */
export async function confirmMultipleDeals(
  client: any,
  deals: Array<{ dealReference: string; accountId: number }>,
  options: DealConfirmationOptions = {},
  concurrency: number = 2
): Promise<DealConfirmationResult[]> {
  
  await orchestrationLogger.info('DEAL_CONFIRMATION_BATCH_STARTED',
    `Confirming ${deals.length} deals with concurrency ${concurrency}`
  );

  const results: DealConfirmationResult[] = [];
  
  // Process in batches
  for (let i = 0; i < deals.length; i += concurrency) {
    const batch = deals.slice(i, i + concurrency);
    
    const batchResults = await Promise.all(
      batch.map(({ dealReference, accountId }) => 
        confirmDeal(client, dealReference, accountId, options)
      )
    );
    
    results.push(...batchResults);
  }

  const confirmed = results.filter(r => r.status === 'confirmed').length;
  const rejected = results.filter(r => r.status === 'rejected').length;
  const timeout = results.filter(r => r.status === 'timeout').length;

  await orchestrationLogger.info('DEAL_CONFIRMATION_BATCH_COMPLETE',
    `Confirmed ${confirmed}/${deals.length} deals (${rejected} rejected, ${timeout} timeout)`,
    { data: { total: deals.length, confirmed, rejected, timeout } }
  );

  return results;
}

