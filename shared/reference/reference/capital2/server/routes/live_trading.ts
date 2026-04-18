/**
 * Live Trading API Routes
 */

import { Router } from 'express';
import { createCapitalAPIClient, getKnownAccountIds } from '../live_trading/credentials';
import {
  getAccountsWithStrategies,
  createOrUpdateAccount,
  assignBotToAccount,
  removeBotFromAccount,
  updateBotStatus,
  syncAccountState,
  getAccountById,
} from '../live_trading/db_functions';
import { sdk } from '../_core/sdk';
import { getDb } from '../db';
import { accounts, savedStrategies } from '../../drizzle/schema';
import { eq, and, or, asc, desc, isNull, isNotNull } from 'drizzle-orm';
import { refreshWebSocketSubscriptions } from '../services/candle_data_service';

const router = Router();

/**
 * GET /api/live-trading/accounts?environment=demo|live
 * Fetch and sync accounts from Capital.com
 */
router.get('/accounts', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    // Get environment from query parameter
    const environment = (req.query.environment as 'demo' | 'live') || 'demo';

    // Create API client for specified environment
    const api = await createCapitalAPIClient(environment);
    if (!api) {
      return res.status(500).json({ error: 'Failed to authenticate with Capital.com' });
    }

    // Fetch accounts from Capital.com
    const capitalAccounts = await api.getAccounts();

    // Sync each account to database
    // All accounts from this API call are of the same type (environment)
    // NOTE: We do NOT sync isActive from Capital.com - that's our internal trading flag
    // Capital.com's account.status is about account enablement, not our trading status
    const syncedAccounts = [];
    for (const account of capitalAccounts) {
      const accountType = environment; // Use the environment parameter directly

      const dbAccount = await createOrUpdateAccount({
        userId,
        accountId: account.accountId,
        accountType,
        accountName: account.accountName,
        balance: account.balance.balance,
        currency: account.currency,
        // Equity ≈ Balance (Capital.com doesn't return equity directly)
        // Real equity = Balance + Unrealized P&L from positions
        equity: account.balance.balance,
        // Margin Used = Balance - Available (funds tied up in positions)
        margin: account.balance.balance - account.balance.available,
        available: account.balance.available,
        profitLoss: account.balance.profitLoss,
        // DO NOT set isActive here - it's our internal flag for whether the account should trade
        // isActive should only be changed via activate/stop mutations
        lastSync: new Date(),
      });

      if (dbAccount) {
        syncedAccounts.push(dbAccount);
      }
    }

    // Get accounts with assigned strategies
    const accountsWithStrategies = await getAccountsWithStrategies(userId);

    res.json({
      success: true,
      accounts: accountsWithStrategies,
      syncedCount: syncedAccounts.length,
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error fetching accounts:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * GET /api/live-trading/accounts/:id
 * Get single account details
 */
router.get('/accounts/:id', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    const accounts = await getAccountsWithStrategies(userId);
    const account = accounts.find(a => a.id === accountId);

    if (!account) {
      return res.status(404).json({ error: 'Account not found' });
    }

    res.json({ success: true, account });
  } catch (error: any) {
    console.error('[LiveTrading] Error fetching account:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/assign-bot
 * Assign a strategy to an account
 */
router.post('/accounts/:id/assign-bot', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    const { strategyId, epic, leverage, stopLoss, investmentPct, windowConfig } = req.body;

    console.log(`[AssignBot] === ASSIGN BOT REQUEST ===`);
    console.log(`[AssignBot] accountId: ${accountId}, strategyId: ${strategyId}`);
    console.log(`[AssignBot] epic: ${epic}, leverage: ${leverage}, stopLoss: ${stopLoss}, investmentPct: ${investmentPct}`);
    console.log(`[AssignBot] windowConfig provided: ${!!windowConfig}`);

    if (!strategyId) {
      console.log(`[AssignBot] ERROR: No strategyId provided`);
      return res.status(400).json({ error: 'Strategy ID is required' });
    }

    // If window config is provided, validate it
    if (windowConfig) {
      console.log(`[AssignBot] Validating windowConfig...`);
      try {
        const { validateWindowConfig } = await import('../orchestration/window_detection');
        validateWindowConfig(windowConfig);
        console.log(`[AssignBot] windowConfig validation passed`);
      } catch (error: any) {
        console.log(`[AssignBot] windowConfig validation FAILED:`, error.message);
        console.log(`[AssignBot] Full error:`, error);
        return res.status(400).json({ error: `Invalid window configuration: ${error.message}` });
      }
    }

    console.log(`[AssignBot] Calling assignBotToAccount...`);
    const success = await assignBotToAccount(accountId, strategyId, {
      epic,
      leverage,
      stopLoss,
      investmentPct,
      windowConfig, // Pass window config to be stored
    });

    console.log(`[AssignBot] assignBotToAccount returned: ${success}`);
    if (success) {
      console.log(`[AssignBot] SUCCESS - Bot assigned to account ${accountId}`);
      // Refresh WebSocket subscriptions when strategy is assigned
      await refreshWebSocketSubscriptions();
      res.json({ success: true, message: 'Bot assigned successfully' });
    } else {
      console.log(`[AssignBot] FAILED - assignBotToAccount returned false for account ${accountId}`);
      res.status(500).json({ error: 'Failed to assign bot' });
    }
  } catch (error: any) {
    console.error('[AssignBot] EXCEPTION:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/remove-bot
 * Remove bot from account
 */
router.post('/accounts/:id/remove-bot', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    const success = await removeBotFromAccount(accountId);

    if (success) {
      // Refresh WebSocket subscriptions when strategy is removed
      await refreshWebSocketSubscriptions();
      res.json({ success: true, message: 'Bot removed successfully' });
    } else {
      res.status(500).json({ error: 'Failed to remove bot' });
    }
  } catch (error: any) {
    console.error('[LiveTrading] Error removing bot:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/start
 * Start bot on account
 */
router.post('/accounts/:id/start', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    
    // Check if account has a strategy assigned - can't start without one
    const account = await getAccountById(accountId);
    if (!account) {
      return res.status(404).json({ error: 'Account not found' });
    }
    if (!account.assignedStrategyId) {
      return res.status(400).json({ 
        error: 'Cannot start account without a strategy assigned. Please assign a strategy first.' 
      });
    }
    
    const success = await updateBotStatus(accountId, 'running');

    if (success) {
      // Also set isActive flag
      const db = await getDb();
      if (db) {
        await db.update(accounts)
          .set({ isActive: true })
          .where(eq(accounts.id, accountId));
      }
      // Refresh WebSocket subscriptions when bot is started
      await refreshWebSocketSubscriptions();
      res.json({ success: true, message: 'Bot started' });
    } else {
      res.status(500).json({ error: 'Failed to start bot' });
    }
  } catch (error: any) {
    console.error('[LiveTrading] Error starting bot:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/pause
 * Pause bot on account
 */
router.post('/accounts/:id/pause', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    const success = await updateBotStatus(accountId, 'paused');

    if (success) {
      res.json({ success: true, message: 'Bot paused' });
    } else {
      res.status(500).json({ error: 'Failed to pause bot' });
    }
  } catch (error: any) {
    console.error('[LiveTrading] Error pausing bot:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/stop
 * Stop bot on account
 */
router.post('/accounts/:id/stop', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    const success = await updateBotStatus(accountId, 'stopped');

    if (success) {
      // Also set isActive flag to false
      const db = await getDb();
      if (db) {
        await db.update(accounts)
          .set({ isActive: false })
          .where(eq(accounts.id, accountId));
      }
      // Refresh WebSocket subscriptions when bot is stopped
      await refreshWebSocketSubscriptions();
      res.json({ success: true, message: 'Bot stopped' });
    } else {
      res.status(500).json({ error: 'Failed to stop bot' });
    }
  } catch (error: any) {
    console.error('[LiveTrading] Error stopping bot:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/sync
 * Manually sync account state from Capital.com
 */
router.post('/accounts/:id/sync', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    const { environment } = req.body;

    // Create API client for specified environment
    const api = await createCapitalAPIClient(environment || 'demo');
    if (!api) {
      return res.status(500).json({ error: 'Failed to authenticate with Capital.com' });
    }

    // Fetch accounts from Capital.com
    const capitalAccounts = await api.getAccounts();
    
    // Find the account we want to sync
    const accounts = await getAccountsWithStrategies(userId);
    const dbAccount = accounts.find(a => a.id === accountId);
    
    if (!dbAccount) {
      return res.status(404).json({ error: 'Account not found' });
    }

    const capitalAccount = capitalAccounts.find(a => a.accountId === dbAccount.accountId);
    
    if (!capitalAccount) {
      return res.status(404).json({ error: 'Account not found on Capital.com' });
    }

    // Sync state
    await syncAccountState(accountId, {
      balance: capitalAccount.balance.balance,
      // Equity ≈ Balance (Capital.com doesn't return equity directly)
      equity: capitalAccount.balance.balance,
      // Margin Used = Balance - Available
      margin: capitalAccount.balance.balance - capitalAccount.balance.available,
      available: capitalAccount.balance.available,
      profitLoss: capitalAccount.balance.profitLoss,
    });

    res.json({ success: true, message: 'Account synced successfully' });
  } catch (error: any) {
    console.error('[LiveTrading] Error syncing account:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * GET /api/live-trading/accounts/:id/preferences
 * Fetch account preferences from Capital.com (including hedging mode)
 * This is fetched on-demand when the Strategy Configuration page loads
 */
router.get('/accounts/:id/preferences', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    
    // Get the account from database to get the Capital.com account ID
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    const [dbAccount] = await db
      .select()
      .from(accounts)
      .where(and(eq(accounts.id, accountId), eq(accounts.userId, user.id)))
      .limit(1);

    if (!dbAccount) {
      return res.status(404).json({ error: 'Account not found' });
    }

    // Create API client for the account's environment
    const api = await createCapitalAPIClient(dbAccount.accountType as 'demo' | 'live');
    if (!api) {
      return res.status(500).json({ error: 'Failed to authenticate with Capital.com' });
    }

    // Switch to the correct Capital.com sub-account
    const switchSuccess = await api.switchAccount(dbAccount.accountId);
    if (!switchSuccess) {
      return res.status(500).json({ error: 'Failed to switch to account' });
    }

    // Fetch account preferences from Capital.com
    const preferences = await api.getAccountPreferences();
    
    if (!preferences) {
      return res.status(500).json({ error: 'Failed to fetch account preferences' });
    }

    console.log(`[LiveTrading] Fetched preferences for account ${accountId}:`, {
      hedgingMode: preferences.hedgingMode,
      sharesLeverage: preferences.leverages?.SHARES?.current,
    });

    res.json({
      accountId,
      capitalAccountId: dbAccount.accountId,
      hedgingMode: preferences.hedgingMode ?? false,
      leverages: preferences.leverages || {},
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error fetching account preferences:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/archive
 * Archive account (hide from main view)
 */
router.post('/accounts/:id/archive', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    // When archiving, also deactivate the account to prevent it from trading
    // An archived account should never be included in brain calculations or closing sequences
    await db.update(accounts)
      .set({ 
        isArchived: true,
        isActive: false,        // Stop including in brain/trading sequences
        botStatus: 'stopped',   // Mark as stopped
        stoppedAt: new Date(),
      })
      .where(and(eq(accounts.id, accountId), eq(accounts.userId, userId)));

    // Refresh WebSocket subscriptions when account is archived
    await refreshWebSocketSubscriptions();
    console.log(`[LiveTrading] Archived account ${accountId} - also set isActive=false, botStatus=stopped`);
    res.json({ success: true });
  } catch (error: any) {
    console.error('[LiveTrading] Error archiving account:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/unarchive
 * Unarchive account (show in main view)
 */
router.post('/accounts/:id/unarchive', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    // When unarchiving, just remove the archived flag
    // User must manually activate the account if they want it to trade
    // This prevents accidentally including old accounts in trading
    await db.update(accounts)
      .set({ isArchived: false })
      .where(and(eq(accounts.id, accountId), eq(accounts.userId, userId)));

    console.log(`[LiveTrading] Unarchived account ${accountId} - NOTE: isActive is still false, user must manually activate to trade`);
    res.json({ success: true });
  } catch (error: any) {
    console.error('[LiveTrading] Error unarchiving account:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/brain-preview
 * Preview what strategy would do RIGHT NOW (calculation only)
 * Optional query param: windowCloseTime (HH:MM:SS format) to preview specific window
 * 
 * IMPORTANT: This fetches fresh T-60 API prices to simulate what the brain
 * would see at T-60s before market close. This matches live trading behavior.
 */
router.post('/accounts/:id/brain-preview', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {      return res.status(401).json({ error: 'Unauthorized' });
    }
    const userId = user.id;

    const accountId = parseInt(req.params.id);
    const windowCloseTime = req.query.windowCloseTime as string | undefined;

    // Get account to find assigned strategy
    const account = await getAccountById(accountId);
    if (!account || !account.assignedStrategyId) {
      return res.status(400).json({ error: 'No strategy assigned to this account' });
    }

    const { brainPreview } = await import('../live_trading/brain');
    const { getStrategy } = await import('../db');
    const { candleDataService } = await import('../services/candle_data_service');
    
    // Load strategy to get all epics
    const strategy = await getStrategy(account.assignedStrategyId);
    if (!strategy || !strategy.tests) {
      return res.status(400).json({ error: 'Strategy not found or has no tests' });
    }
    
    // Collect all unique epics from strategy tests
    const uniqueEpics = [...new Set(strategy.tests.map((t: any) => t.epic || 'SOXL'))];
    console.log(`[Brain Preview] Fetching fresh data for epics: ${uniqueEpics.join(', ')}`);
    
    // STEP 1: Ensure fresh candles are available (fetch from API if stale)
    // Note: WebSocket may have already pushed newer data than API returns
    try {
      const freshnessResults = await candleDataService.ensureFreshCandles(uniqueEpics, 5); // Max 5 min old
      for (const [epic, result] of Object.entries(freshnessResults)) {
        if (result.fetched > 0) {
          console.log(`[Brain Preview] ✓ ${epic}: Fetched ${result.fetched} candles from API`);
        } else if (result.wasFresh) {
          console.log(`[Brain Preview] ✓ ${epic}: Data already fresh (${result.ageMinutes.toFixed(1)} min old)`);
        } else {
          console.log(`[Brain Preview] ⚠ ${epic}: API had no newer candles (using WebSocket/cached data)`);
        }
      }
    } catch (error: any) {
      console.warn(`[Brain Preview] Warning: Failed to refresh candles: ${error.message}`);
    }
    
    // STEP 2: Fetch T-60 API prices for all epics (current bid/ask for fake 5min candle)
    const epicPrices: Record<string, number> = {};
    try {
      console.log(`[Brain Preview] Fetching T-60 API prices...`);
      const apiPrices = await candleDataService.getInstantPrices(uniqueEpics);
      for (const [epic, priceData] of apiPrices) {
        if (priceData.bid && priceData.ask) {
          const mid = (priceData.bid + priceData.ask) / 2;
          epicPrices[epic] = mid;
          console.log(`[Brain Preview] ✓ ${epic}: API price ${mid.toFixed(4)}`);
        }
      }
    } catch (error: any) {
      console.warn(`[Brain Preview] Warning: Failed to fetch API prices: ${error.message}`);
      // Continue without API prices - brain will use DB candles as fallback
    }
    
    // If windowCloseTime is provided, filter strategy tests for that window
    let result;
    if (windowCloseTime) {
      console.log(`[Brain Preview] Running for specific window: ${windowCloseTime}`);
      
      // Get window configuration to find which epics are in this window
      const windowConfig = account.windowConfig as any;
      const window = windowConfig?.windows?.find((w: any) => w.closeTime === windowCloseTime);
      
      if (!window || !window.epics || window.epics.length === 0) {
        return res.status(400).json({ error: `Window ${windowCloseTime} not found or has no epics configured` });
      }
      
      const windowEpics = window.epics;
      console.log(`[Brain Preview] Window ${windowCloseTime} epics:`, windowEpics);
      
      // Run brain preview with window epic filter AND T-60 API prices
      result = await brainPreview(
        account.assignedStrategyId, 
        accountId, 
        account.accountType || 'demo',
        windowEpics, // Pass window epics to filter tests
        undefined, // epicDataCache
        epicPrices // Pass T-60 API prices
      );
      // Add window context to result
      result = { ...result, windowCloseTime, windowEpics };
    } else {
      // Run brain preview for all windows (no epic filter) with T-60 API prices
      result = await brainPreview(
        account.assignedStrategyId, 
        accountId, 
        account.accountType || 'demo',
        undefined, // filterEpics
        undefined, // epicDataCache
        epicPrices // Pass T-60 API prices
      );
    }

    if (result) {
      res.json({ success: true, result });
    } else {
      res.status(500).json({ error: 'Failed to run brain preview' });
    }
  } catch (error: any) {
    console.error('[LiveTrading] Error in brain preview:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/simulate
 * Run full simulation for a single account (all windows)
 * 
 * Body params:
 * - countdownSeconds: number (default 10) - seconds between windows
 * - confirmedLive: boolean - required for live accounts
 * 
 * This runs the complete trading cycle:
 * - Brain calculations (with HOLD→BUY override in simulation)
 * - Close existing trades
 * - Open new trades
 * - Reconciliation
 * 
 * Windows are processed sequentially with 10-second countdown between each.
 */
router.post('/accounts/:id/simulate', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const { countdownSeconds = 10, confirmedLive = false } = req.body;

    // Fetch account to check type
    const { getDb } = await import('../db');
    const { accounts } = await import('../../drizzle/schema');
    const { eq } = await import('drizzle-orm');
    
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }
    
    const [account] = await db.select().from(accounts).where(eq(accounts.id, accountId)).limit(1);
    
    if (!account) {
      return res.status(404).json({ error: 'Account not found' });
    }
    
    // Safety check: live simulation requires explicit confirmation
    if (account.accountType === 'live' && !confirmedLive) {
      return res.status(400).json({ 
        error: 'Live account simulation requires explicit confirmation. Set confirmedLive: true in request body.',
        code: 'LIVE_NOT_CONFIRMED',
        accountType: account.accountType,
        accountName: account.accountName
      });
    }

    const { runAccountSimulation, getSimulationState } = await import('../orchestration/simulation_controller');
    
    // Check if simulation is already running
    const currentState = getSimulationState();
    if (currentState.isRunning) {
      return res.status(409).json({ 
        error: 'A simulation is already running',
        state: currentState 
      });
    }

    const typeLabel = account.accountType === 'live' ? 'LIVE' : 'DEMO';
    console.log(`[Simulation] Starting ${typeLabel} single-account simulation for account ${accountId} (${account.accountName})`);
    
    // Run simulation (this is async and may take a while)
    const result = await runAccountSimulation(accountId, countdownSeconds, confirmedLive);

    res.json({ success: true, result });
  } catch (error: any) {
    console.error('[LiveTrading] Error in simulate:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/simulate-global
 * Run full simulation for ALL active accounts across all windows
 * 
 * Body params:
 * - countdownSeconds: number (default 10) - seconds between windows
 * - accountType: 'demo' | 'live' | undefined - filter by account type
 * - confirmedLive: boolean - required when accountType is 'live'
 * 
 * WARNING: This overrides HOLD decisions with random BUY in simulation mode.
 */
router.post('/simulate-global', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const { countdownSeconds = 10, accountType, confirmedLive = false } = req.body;

    // Safety check: live simulation requires explicit confirmation
    if (accountType === 'live' && !confirmedLive) {
      return res.status(400).json({ 
        error: 'Live simulation requires explicit confirmation. Set confirmedLive: true in request body.',
        code: 'LIVE_NOT_CONFIRMED'
      });
    }

    const { runGlobalSimulation, getSimulationState } = await import('../orchestration/simulation_controller');
    
    // Check if simulation is already running
    const currentState = getSimulationState();
    if (currentState.isRunning) {
      return res.status(409).json({ 
        error: 'A simulation is already running',
        state: currentState 
      });
    }

    const accountTypeLabel = accountType ? accountType.toUpperCase() : 'ALL';
    console.log(`[Simulation] Starting ${accountTypeLabel} simulation (confirmedLive: ${confirmedLive})`);
    
    // Run simulation with account type filter
    const result = await runGlobalSimulation(countdownSeconds, accountType, confirmedLive);

    res.json({ success: true, result });
  } catch (error: any) {
    console.error('[LiveTrading] Error in global simulate:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * GET /api/live-trading/simulation-state
 * Get current simulation state (for UI progress display)
 */
router.get('/simulation-state', async (req, res) => {
  try {
    const { getSimulationState } = await import('../orchestration/simulation_controller');
    const state = getSimulationState();
    res.json({ success: true, state });
  } catch (error: any) {
    console.error('[LiveTrading] Error getting simulation state:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/simulation-stop
 * Stop a running simulation (if possible)
 */
router.post('/simulation-stop', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const { stopSimulation } = await import('../orchestration/simulation_controller');
    const stopped = stopSimulation();
    
    if (stopped) {
      res.json({ success: true, message: 'Simulation stop requested' });
    } else {
      res.json({ success: false, message: 'No simulation running' });
    }
  } catch (error: any) {
    console.error('[LiveTrading] Error stopping simulation:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/test-live-sequence
 * Test the live trading sequence without waiting for actual market close
 * 
 * This runs through the ACTUAL live code paths:
 * 1. Quiet period start (initializes loggers, fund tracker, accumulators)
 * 2. T-60s brain calculations (get API prices, run DNA calculations)
 * 3. T-15s open trades (fetch fresh balances, fetch bid/ask, fire trades)
 * 4. Quiet period end
 * 
 * Uses compressed timings (3s between steps) for quick testing.
 * Creates a log in logs/closing for review.
 * 
 * Body params:
 * - accountType: 'demo' | 'live' (default: 'demo') - which accounts to test
 */
router.post('/test-live-sequence', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const { accountType = 'demo' } = req.body;
    
    // Safety check for live accounts
    if (accountType === 'live') {
      return res.status(400).json({ 
        error: 'Live account testing not allowed via this endpoint. Use simulation instead.' 
      });
    }

    console.log('[LiveTrading] Starting test live sequence for', accountType, 'accounts');

    // Import the timer and run test sequence
    const { orchestrationTimer } = await import('../orchestration/timer');
    
    // Track progress for response
    const progressLog: { step: string; status: string; timestamp: string; details?: any }[] = [];
    
    const result = await orchestrationTimer.testLiveSequence((step, status, details) => {
      progressLog.push({
        step,
        status,
        timestamp: new Date().toISOString(),
        details,
      });
      console.log(`[TestLive] ${step}: ${status}`, details || '');
    });

    res.json({
      success: result.success,
      result,
      progressLog,
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error in test live sequence:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * GET /api/live-trading/test-live-state
 * Get current test live sequence state (for UI progress display)
 */
router.get('/test-live-state', async (req, res) => {
  try {
    // For now, just return that no test is running
    // In the future, we could track ongoing tests
    res.json({ 
      success: true, 
      isRunning: false,
      message: 'Test live sequence status endpoint ready'
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error getting test live state:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/trades/:id/validate
 * Validate a single trade by re-running backtest with AV-only data
 */
router.post('/trades/:id/validate', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const tradeId = parseInt(req.params.id);
    
    const { validateTrade } = await import('../services/validation_service');
    const result = await validateTrade(tradeId);

    res.json({ success: true, result });
  } catch (error: any) {
    console.error('[LiveTrading] Error validating trade:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/validate-all
 * Validate all pending trades for an account
 */
router.post('/accounts/:id/validate-all', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    
    const { validatePendingTrades } = await import('../services/validation_service');
    const results = await validatePendingTrades(accountId);

    res.json({ 
      success: true, 
      count: results.length,
      validated: results.filter(r => r.status === 'validated').length,
      mismatches: results.filter(r => r.status === 'mismatch').length,
      dataNotReady: results.filter(r => r.status === 'data_not_ready').length,
      results 
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error validating trades:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/validate-all
 * Validate ALL trades across ALL accounts (faster bulk validation)
 * Uses optimized batch DNA evaluation + parallel trade processing
 */
router.post('/validate-all', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    console.log('[LiveTrading] Starting bulk validation of all trades...');
    
    const { validateAllTrades } = await import('../services/validation_service');
    const results = await validateAllTrades();

    res.json({ 
      success: true, 
      count: results.length,
      validated: results.filter(r => r.status === 'validated').length,
      signalMismatches: results.filter(r => r.status === 'signal_mismatch').length,
      winnerMismatches: results.filter(r => r.status === 'winner_mismatch').length,
      dataNotReady: results.filter(r => r.status === 'data_not_ready').length,
      errors: results.filter(r => r.status === 'error').length,
      results 
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error in bulk validation:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * GET /api/live-trading/accounts/:id/validation-summary
 * Get validation summary for an account
 */
router.get('/accounts/:id/validation-summary', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    
    const { getValidationSummary } = await import('../services/validation_service');
    const summary = await getValidationSummary(accountId);

    res.json({ success: true, summary });
  } catch (error: any) {
    console.error('[LiveTrading] Error getting validation summary:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * GET /api/live-trading/accounts/:id/trades
 * Get all actual trades for an account with validation status
 */
router.get('/accounts/:id/trades', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    const { actualTrades, validatedTrades, tradeComparisons } = await import('../../drizzle/schema');
    
    // Get all actual trades for this account
    const trades = await db
      .select()
      .from(actualTrades)
      .where(eq(actualTrades.accountId, accountId))
      .orderBy(actualTrades.createdAt);
    
    // Get validation status for each trade
    const tradesWithValidation = await Promise.all(
      trades.map(async (trade) => {
        const validation = await db
          .select()
          .from(validatedTrades)
          .where(eq(validatedTrades.actualTradeId, trade.id))
          .limit(1);
        
        const comparison = await db
          .select()
          .from(tradeComparisons)
          .where(eq(tradeComparisons.actualTradeId, trade.id))
          .limit(1);
        
        return {
          ...trade,
          validation: validation[0] || null,
          comparison: comparison[0] || null,
        };
      })
    );

    res.json({ success: true, trades: tradesWithValidation });
  } catch (error: any) {
    console.error('[LiveTrading] Error getting trades:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/sync-trades
 * Sync all trades from Capital.com activity history
 * This mirrors all trades (app, manual, stoploss, system) to our database
 * 
 * Body params:
 * - days: Number of days to sync (default: 30, max: 365)
 * - fullSync: If true, fetches all history regardless of existing data
 * - incrementalSync: If true (default), starts from last synced date with 1-day overlap
 */
router.post('/accounts/:id/sync-trades', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const { 
      days = 30,           // Default 30 days
      fullSync = false,    // Full history fetch
      incrementalSync = true // Start from latest trade date
    } = req.body;
    
    // Validate days parameter (max 365)
    const syncDays = Math.min(Math.max(1, days), 365);
    
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    // Get account details
    const { accounts, actualTrades } = await import('../../drizzle/schema');
    const account = await db
      .select()
      .from(accounts)
      .where(eq(accounts.id, accountId))
      .limit(1);

    if (account.length === 0) {
      return res.status(404).json({ error: 'Account not found' });
    }

    const acc = account[0];
    const environment = acc.accountType === 'demo' ? 'demo' : 'live';
    
    // Create API client
    const { createCapitalAPIClient } = await import('../live_trading/credentials');
    const client = await createCapitalAPIClient(environment);
    
    if (!client) {
      return res.status(500).json({ error: 'Failed to create API client' });
    }

    // Switch to the correct account using Capital.com's account ID
    // First fetch accounts to find the correct Capital ID if not stored
    let capitalAccountId = acc.capitalAccountId;
    if (!capitalAccountId) {
      const capitalAccounts = await client.getAccounts();
      const matchingAccount = capitalAccounts.find(a => a.accountName === acc.accountName);
      if (matchingAccount) {
        capitalAccountId = matchingAccount.accountId;
        console.log(`[SyncTrades] Found Capital.com account ID: ${capitalAccountId} for ${acc.accountName}`);
      } else {
        console.error(`[SyncTrades] Could not find Capital.com account for ${acc.accountName}`);
        return res.status(400).json({ error: `Could not find Capital.com account for ${acc.accountName}` });
      }
    }
    await client.switchAccount(capitalAccountId);

    // Determine sync start date
    let startDate: Date;
    
    if (fullSync) {
      // Full sync: go back requested number of days
      startDate = new Date();
      startDate.setDate(startDate.getDate() - syncDays);
      console.log(`[SyncTrades] Full sync requested: fetching ${syncDays} days of history`);
    } else if (incrementalSync) {
      // Incremental sync: start from latest trade date with 2-day overlap for safety
      const latestTrade = await db
        .select()
        .from(actualTrades)
        .where(eq(actualTrades.accountId, accountId))
        .orderBy(desc(actualTrades.capitalExecutedAt))
        .limit(1);
      
      if (latestTrade.length > 0 && latestTrade[0].capitalExecutedAt) {
        startDate = new Date(latestTrade[0].capitalExecutedAt);
        startDate.setDate(startDate.getDate() - 2); // 2-day overlap for safety
        console.log(`[SyncTrades] Incremental sync: starting from ${startDate.toISOString()} (2 days before latest trade)`);
      } else {
        // No existing trades, fetch requested days
        startDate = new Date();
        startDate.setDate(startDate.getDate() - syncDays);
        console.log(`[SyncTrades] No existing trades, fetching ${syncDays} days of history`);
      }
    } else {
      // Default: fetch requested days
      startDate = new Date();
      startDate.setDate(startDate.getDate() - syncDays);
    }
    
    const endDate = new Date();
    
    // Calculate actual days to fetch
    const actualDaysToFetch = Math.ceil((endDate.getTime() - startDate.getTime()) / (24 * 60 * 60 * 1000));
    console.log(`[SyncTrades] Fetching ${actualDaysToFetch} days of activity from ${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}`);

    // Get activity history (Capital.com only allows 1 day per query, so we loop)
    // Note: Capital.com expects date format YYYY-MM-DDTHH:MM:SS (no milliseconds or timezone)
    const formatDate = (d: Date) => d.toISOString().split('.')[0];
    
    let allActivities: any[] = [];
    let daysFetched = 0;
    
    // Loop through each day from endDate back to startDate
    for (let daysBack = 0; daysBack < actualDaysToFetch; daysBack++) {
      const dayEnd = new Date();
      dayEnd.setDate(dayEnd.getDate() - daysBack);
      dayEnd.setHours(23, 59, 59, 0);
      
      const dayStart = new Date(dayEnd);
      dayStart.setHours(0, 0, 0, 0);
      
      // Stop if we've gone past the start date
      if (dayStart < startDate) break;
      
      const from = formatDate(dayStart);
      const to = formatDate(dayEnd);
      
      try {
        const dayActivities = await client.getActivityHistory(from, to);
        if (dayActivities?.length > 0) {
          console.log(`[SyncTrades] Day ${daysBack + 1} (${dayStart.toISOString().split('T')[0]}): ${dayActivities.length} activities`);
          allActivities.push(...dayActivities);
        }
        daysFetched++;
      } catch (error: any) {
        console.error(`[SyncTrades] Error fetching day ${dayStart.toISOString().split('T')[0]}:`, error.message);
      }
      
      // Rate limit protection - 200ms between calls
      await new Promise(r => setTimeout(r, 200));
      
      // Progress logging every 10 days
      if (daysBack > 0 && daysBack % 10 === 0) {
        console.log(`[SyncTrades] Progress: ${daysBack}/${actualDaysToFetch} days fetched, ${allActivities.length} activities so far`);
      }
    }
    
    console.log(`[SyncTrades] Fetched ${allActivities.length} total activities over ${daysFetched} days for account ${accountId}`);
    
    const activities = allActivities;
    
    if (!activities || activities.length === 0) {
      return res.json({ 
        success: true, 
        newTrades: 0, 
        updatedTrades: 0,
        daysFetched,
        message: `No activities found in the past ${daysFetched} days`
      });
    }
    
    /**
     * Helper: Infer window close time from trade execution time
     * This helps assign manual/external trades to the correct window
     */
    const inferWindowCloseTime = (executionTimeUTC: string): string => {
      try {
        const tradeTime = new Date(executionTimeUTC);
        const hour = tradeTime.getUTCHours();
        const minute = tradeTime.getUTCMinutes();
        
        // Window detection based on UTC time
        // Regular hours window: 21:00 UTC (trades typically 20:45-21:15)
        if ((hour === 20 && minute >= 45) || (hour === 21 && minute <= 15)) {
          return '21:00:00';
        }
        // Extended hours window: 01:00 UTC (trades typically 00:45-01:15)
        if ((hour === 0 && minute >= 45) || (hour === 1 && minute <= 15)) {
          return '01:00:00';
        }
        // Pre-market window: 14:30 UTC (if applicable)
        if ((hour === 14 && minute >= 15) || (hour === 14 && minute <= 45)) {
          return '14:30:00';
        }
        
        // If we can't infer, return the closest standard window
        // Round to nearest hour and return that as window time
        const roundedHour = minute >= 30 ? (hour + 1) % 24 : hour;
        return `${roundedHour.toString().padStart(2, '0')}:00:00`;
      } catch {
        return '00:00:00'; // Fallback if parsing fails
      }
    };

    let newTrades = 0;
    let updatedTrades = 0;

    // Process each activity
    for (const activity of activities) {
      // Only process position open/close activities
      if (activity.type !== 'POSITION' && activity.type !== 'ORDER') continue;
      
      const dealId = activity.dealId;
      if (!dealId) continue;

      // Determine trade source from Capital.com source field
      let tradeSource: 'app' | 'manual' | 'stoploss' | 'system' | 'unknown' = 'unknown';
      switch (activity.source?.toUpperCase()) {
        case 'USER':
          // Check if it matches any of our app trades first
          const existingAppTrade = await db
            .select()
            .from(actualTrades)
            .where(and(
              eq(actualTrades.accountId, accountId),
              eq(actualTrades.dealId, dealId),
              eq(actualTrades.tradeSource, 'app')
            ))
            .limit(1);
          
          tradeSource = existingAppTrade.length > 0 ? 'app' : 'manual';
          break;
        case 'SYSTEM':
          tradeSource = activity.details?.stopLoss ? 'stoploss' : 'system';
          break;
        default:
          tradeSource = 'unknown';
      }

      // Check if trade already exists in our DB by dealId
      const existingTrade = await db
        .select()
        .from(actualTrades)
        .where(and(
          eq(actualTrades.accountId, accountId),
          eq(actualTrades.dealId, dealId)
        ))
        .limit(1);

      if (existingTrade.length > 0) {
        // Update existing trade with latest info from Capital.com
        const updateData: any = {
          capitalExecutedAt: activity.dateUTC ? new Date(activity.dateUTC) : null,
          entryPrice: activity.details?.level || existingTrade[0].entryPrice,
          contracts: activity.details?.size || existingTrade[0].contracts,
          // Don't override tradeSource if it's already 'app' (preserve our app trades)
          tradeSource: existingTrade[0].tradeSource === 'app' ? 'app' : tradeSource,
        };
        
        // If window time is still the placeholder '00:00:00', try to infer it
        if (existingTrade[0].windowCloseTime === '00:00:00' && activity.dateUTC) {
          const inferredWindow = inferWindowCloseTime(activity.dateUTC);
          if (inferredWindow !== '00:00:00') {
            updateData.windowCloseTime = inferredWindow;
            console.log(`[SyncTrades] Inferred window time ${inferredWindow} for existing trade #${existingTrade[0].id}`);
          }
        }
        
        await db
          .update(actualTrades)
          .set(updateData)
          .where(eq(actualTrades.id, existingTrade[0].id));
        updatedTrades++;
      } else {
        // Insert new trade from Capital.com
        // Infer window time from execution timestamp
        const inferredWindow = activity.dateUTC 
          ? inferWindowCloseTime(activity.dateUTC)
          : '00:00:00';
        
        // NOTE: We CANNOT attribute to a specific DNA strand here because:
        // 1. A strategy can have MULTIPLE DNA strands for the same epic
        // 2. Conflict resolution determines the winner at runtime
        // 3. We don't know which DNA won when importing historical trades
        //
        // If this trade was made by the app but brain failed to store it:
        // - strategyId will be 0 (unknown)
        // - winningIndicatorName will be NULL
        // - Validation will need to RE-RUN the brain for that window to determine winner
        //
        // The PROPER flow is:
        // 1. storeBrainResult creates trade with winning DNA info
        // 2. Open orchestrator updates with dealReference
        // 3. Reconciliation confirms with dealId
        // This route only handles imports that weren't properly recorded.
        
        // Store with strategyId from account (for reference) but mark as 'manual' source
        // since we can't determine the winning DNA
        const strategyIdForReference = acc.assignedStrategyId || 0;
        
        await db.insert(actualTrades).values({
          accountId: accountId,
          strategyId: strategyIdForReference, // Strategy reference (but winning DNA unknown)
          windowCloseTime: inferredWindow, // Inferred from execution time
          epic: activity.epic || 'UNKNOWN',
          direction: activity.details?.direction === 'BUY' ? 'BUY' : 'SELL',
          dealId: dealId,
          dealReference: activity.details?.dealReference || null,
          entryPrice: activity.details?.level || null,
          contracts: activity.details?.size || null,
          leverage: 1, // Unknown - would need brain re-run to determine
          brainDecision: activity.details?.direction === 'BUY' ? 'BUY' : 'HOLD',
          // NOTE: winningIndicatorName is NULL - we don't know which DNA won
          // Validation should re-run brain calculation to determine this
          winningIndicatorName: null,
          indicatorParams: null,
          status: activity.status === 'CLOSED' ? 'closed' : 'open',
          capitalExecutedAt: activity.dateUTC ? new Date(activity.dateUTC) : null,
          tradeSource: tradeSource,
          isSimulation: false,
        });
        
        console.log(`[SyncTrades] New trade from Capital.com: ${activity.epic} at ${activity.dateUTC}, window: ${inferredWindow}, strategyRef: ${strategyIdForReference} (winning DNA unknown - needs brain re-run)`);
        newTrades++;
      }
    }

    // Phase 2: Sync closed trade data (exit prices, P&L) from activity/transaction history
    console.log(`[SyncTrades] Phase 2: Syncing closed trade data...`);
    
    let closedTradesUpdated = 0;
    
    // Get transactions for P&L data (loop through days - same range as activities)
    let allTransactions: any[] = [];
    for (let daysBack = 0; daysBack < actualDaysToFetch; daysBack++) {
      const dayEnd = new Date();
      dayEnd.setDate(dayEnd.getDate() - daysBack);
      dayEnd.setHours(23, 59, 59, 0);
      
      const dayStart = new Date(dayEnd);
      dayStart.setHours(0, 0, 0, 0);
      
      // Stop if we've gone past the start date
      if (dayStart < startDate) break;
      
      const from = formatDate(dayStart);
      const to = formatDate(dayEnd);
      
      try {
        const dayTransactions = await client.getTransactionHistory(from, to);
        if (dayTransactions?.length > 0) {
          allTransactions.push(...dayTransactions);
        }
      } catch (error: any) {
        console.error(`[SyncTrades] Error fetching transactions for ${dayStart.toISOString().split('T')[0]}:`, error.message);
      }
      
      await new Promise(r => setTimeout(r, 200));
    }
    const transactions = allTransactions;
    console.log(`[SyncTrades] Fetched ${transactions.length} transactions`);
    
    // Index transactions by dealId
    const transactionsByDealId = new Map<string, any>();
    for (const tx of transactions || []) {
      if (tx.dealId && tx.transactionType === 'TRADE' && tx.note?.includes('closed')) {
        transactionsByDealId.set(tx.dealId, tx);
      }
    }
    
    // Process close activities to update exit prices
    for (const activity of activities) {
      if (activity.type !== 'POSITION') continue;
      if (activity.status !== 'ACCEPTED') continue;
      
      const dealId = activity.dealId;
      if (!dealId) continue;
      
      // Check if this is a CLOSE activity (has openPrice in details)
      const isCloseActivity = activity.details?.openPrice !== undefined;
      if (!isCloseActivity) continue;
      
      // Find the trade in our DB
      const existingTrade = await db
        .select()
        .from(actualTrades)
        .where(and(
          eq(actualTrades.accountId, accountId),
          eq(actualTrades.dealId, dealId)
        ))
        .limit(1);
      
      if (existingTrade.length === 0) continue;
      
      const trade = existingTrade[0];
      
      // Get exit price from close activity
      const exitPrice = activity.details?.level || null;
      
      // Get P&L from transaction history first
      const closeTx = transactionsByDealId.get(dealId);
      let netPnl = closeTx ? parseFloat(closeTx.size) : null;
      
      // FALLBACK: Calculate P&L from trade data if transaction not found
      // This handles cases where transaction sync fails or doesn't match
      if (netPnl === null && exitPrice !== null && trade.entryPrice && trade.contracts) {
        const entryPrice = trade.entryPrice;
        const contracts = trade.contracts;
        const direction = trade.direction === 'BUY' ? 1 : -1;
        
        // P&L = (exit - entry) * contracts * direction
        // Note: This is a simplified calculation without spread/overnight costs
        netPnl = (exitPrice - entryPrice) * contracts * direction;
        console.log(`[SyncTrades] Calculated P&L for trade #${trade.id}: (${exitPrice} - ${entryPrice}) * ${contracts} * ${direction} = ${netPnl.toFixed(2)}`);
      }
      
      // Determine close source - must match enum: window_close, stop_loss, manual, rebalance, rejected
      let closeReason: "window_close" | "stop_loss" | "manual" | "rebalance" | "rejected" | null = null;
      switch (activity.source) {
        case 'SL': closeReason = 'stop_loss'; break;
        case 'TP': closeReason = 'manual'; break; // No take_profit enum, use manual
        case 'USER': closeReason = 'manual'; break;
        case 'SYSTEM': closeReason = 'window_close'; break;
        case 'CLOSE_OUT': closeReason = 'rejected'; break; // Margin closeout maps to rejected
        default: closeReason = null; // Don't set if unknown
      }
      
      // Build update object
      const updateData: any = {};
      if (exitPrice !== null && !trade.exitPrice) updateData.exitPrice = exitPrice;
      if (netPnl !== null && !trade.netPnl) updateData.netPnl = netPnl;
      if (closeReason && !trade.closeReason) updateData.closeReason = closeReason;
      if (activity.dateUTC && !trade.closedAt) {
        // Capital.com dateUTC format: "2025-12-16T15:25:22.805" or "2025-12-16 15:25:22.805"
        // Normalize to ISO format and create Date
        const normalizedDate = activity.dateUTC.replace(' ', 'T');
        updateData.closedAt = new Date(normalizedDate.endsWith('Z') ? normalizedDate : normalizedDate + 'Z');
      }
      // Set status based on close source
      if (activity.source === 'SL') {
        updateData.status = 'stopped_out';
      } else if (trade.status !== 'closed' && trade.status !== 'stopped_out') {
        updateData.status = 'closed';
      }
      
      if (Object.keys(updateData).length > 0) {
        await db
          .update(actualTrades)
          .set(updateData)
          .where(eq(actualTrades.id, trade.id));
        closedTradesUpdated++;
        console.log(`[SyncTrades] Updated trade #${trade.id} with close data: exit=${exitPrice}, pnl=${netPnl}, source=${closeReason}`);
      }
    }
    
    console.log(`[SyncTrades] Sync complete: ${newTrades} new, ${updatedTrades} updated, ${closedTradesUpdated} closed trades updated`);
    
    res.json({ 
      success: true, 
      newTrades, 
      updatedTrades,
      closedTradesUpdated,
      totalActivities: activities.length,
      totalTransactions: transactions?.length || 0,
      daysFetched,
      dateRange: {
        from: startDate.toISOString().split('T')[0],
        to: endDate.toISOString().split('T')[0]
      }
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error syncing trades:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * GET /api/live-trading/accounts/:id/comparisons
 * Get trade comparisons for an account (for chart overlay)
 * Generates comparison data from actual_trades + validated_trades
 */
router.get('/accounts/:id/comparisons', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    const { actualTrades, validatedTrades } = await import('../../drizzle/schema');
    
    // Get all actual trades with their validation records
    const tradesWithValidation = await db
      .select({
        trade: actualTrades,
        validation: validatedTrades,
      })
      .from(actualTrades)
      .leftJoin(validatedTrades, eq(actualTrades.id, validatedTrades.actualTradeId))
      .where(eq(actualTrades.accountId, accountId))
      .orderBy(actualTrades.createdAt);
    
    // Generate comparison records from trades
    const comparisons = tradesWithValidation.map(({ trade, validation }) => {
      // Extract date from trade
      const tradeDate = trade.createdAt 
        ? new Date(trade.createdAt).toISOString().split('T')[0]
        : trade.windowCloseTime?.split(' ')[0] || '';
      
      // Determine match status
      let matchStatus: 'match' | 'mismatch' | 'winner_mismatch' | 'actual_only' | 'validated_only' | 'pending' = 'pending';
      
      if (validation) {
        if (validation.validationStatus === 'validated' && validation.signalMatch) {
          matchStatus = 'match';
        } else if (validation.validationStatus === 'signal_mismatch') {
          matchStatus = 'mismatch';
        } else if (validation.validationStatus === 'winner_mismatch') {
          matchStatus = 'winner_mismatch';
        } else if (validation.validationStatus === 'validated') {
          matchStatus = validation.signalMatch ? 'match' : 'mismatch';
        }
      }
      
      // Calculate P&L values
      const actualPnl = trade.netPnl || 0;
      // For validated P&L, we could estimate based on whether it would have traded
      // For now, use actual P&L if signal matched, else 0
      const validatedPnl = matchStatus === 'match' ? actualPnl : 
                          matchStatus === 'mismatch' ? 0 : actualPnl;
      
      return {
        id: trade.id,
        accountId: trade.accountId,
        tradeDate,
        windowCloseTime: trade.windowCloseTime || '',
        
        // Actual trade summary
        actualTraded: true,
        actualIndicator: trade.winningIndicatorName || trade.epic,
        actualPnl,
        actualTradeId: trade.id,
        
        // Validated trade summary  
        validatedWouldTrade: validation ? (validation.rerunSignal === 'BUY') : null,
        validatedIndicator: validation?.originalIndicatorName || null,
        validatedPnl,
        validatedTradeId: validation?.id || null,
        
        // Comparison result
        matchStatus,
        pnlDifference: actualPnl - validatedPnl,
      };
    });

    res.json({ success: true, comparisons });
  } catch (error: any) {
    console.error('[LiveTrading] Error getting comparisons:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * GET /api/live-trading/accounts/:id/comparison-defaults
 * Get default values for strategy comparison (dates from history, balance from strategy)
 */
router.get('/accounts/:id/comparison-defaults', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    const { actualTrades, accounts: accountsTable, accountTransactions, strategyRuns } = await import('../../drizzle/schema');
    
    // 1. Get account and its assigned strategy
    const account = await db
      .select()
      .from(accountsTable)
      .where(eq(accountsTable.id, accountId))
      .limit(1);
    
    if (account.length === 0) {
      return res.status(404).json({ error: 'Account not found' });
    }
    
    const assignedStrategyId = account[0].assignedStrategyId;
    
    // 2. Get date range from BOTH transactions and trades
    const allDates: string[] = [];
    
    // Get trade dates
    const trades = await db
      .select({ createdAt: actualTrades.createdAt })
      .from(actualTrades)
      .where(eq(actualTrades.accountId, accountId));
    
    trades.forEach(t => {
      if (t.createdAt) {
        allDates.push(new Date(t.createdAt).toISOString().split('T')[0]);
      }
    });
    
    // Get transaction dates
    const transactions = await db
      .select({ transactionDate: accountTransactions.transactionDate })
      .from(accountTransactions)
      .where(eq(accountTransactions.accountId, accountId));
    
    transactions.forEach(tx => {
      if (tx.transactionDate) {
        allDates.push(new Date(tx.transactionDate).toISOString().split('T')[0]);
      }
    });
    
    // Sort and get range
    allDates.sort();
    const startDate = allDates.length > 0 ? allDates[0] : null;
    const endDate = allDates.length > 0 ? allDates[allDates.length - 1] : null;
    
    // 3. Get strategy configuration (initial balance and monthly topup from last run)
    let initialBalance: number | null = null;
    let monthlyTopup: number = 0;
    
    if (assignedStrategyId) {
      // Look for the most recent strategy run for this strategy
      const runs = await db
        .select({
          initialBalance: strategyRuns.initialBalance,
          monthlyTopup: strategyRuns.monthlyTopup,
        })
        .from(strategyRuns)
        .where(eq(strategyRuns.strategyId, assignedStrategyId))
        .orderBy(desc(strategyRuns.createdAt))
        .limit(1);
      
      if (runs.length > 0) {
        initialBalance = runs[0].initialBalance;
        monthlyTopup = runs[0].monthlyTopup || 0;
      }
    }
    
    // If no strategy run found, try to use account balance
    if (!initialBalance) {
      initialBalance = account[0].balance || 1000;
    }
    
    res.json({
      success: true,
      startDate,
      endDate,
      initialBalance,
      monthlyTopup,
      hasStrategyRun: !!initialBalance,
    });
    
  } catch (error: any) {
    console.error('[LiveTrading] Error getting comparison defaults:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/run-strategy-comparison
 * Run full strategy test and compare against actual trades
 * This shows missed trades, unexpected trades, and matches
 */
router.post('/accounts/:id/run-strategy-comparison', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const { 
      startDate: inputStartDate, 
      endDate: inputEndDate,
      initialBalance: inputInitialBalance,
      monthlyTopup: inputMonthlyTopup,
      useRealCapitalEvents = false, // NEW: Use actual deposits/withdrawals from Capital.com
    } = req.body;
    
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    // Helper function to calculate correct window date from execution time
    // Handles timezone issues and overnight windows correctly
    const calculateWindowDateForTrade = (trade: any): string => {
      const windowTime = trade.windowCloseTime?.split(' ')[1] || trade.windowCloseTime || '';
      const windowHour = parseInt(windowTime.split(':')[0] || '21', 10);
      const executionTime = trade.capitalExecutedAt || trade.createdAt;
      if (!executionTime) return '';
      
      let execDate: Date;
      if (executionTime instanceof Date) {
        execDate = executionTime;
      } else {
        const timeStr = String(executionTime);
        execDate = timeStr.includes('Z') || timeStr.includes('+') ? new Date(timeStr) : new Date(timeStr + 'Z');
      }
      
      const execUtcDate = execDate.getUTCDate();
      const execUtcMonth = execDate.getUTCMonth();
      const execUtcYear = execDate.getUTCFullYear();
      const execUtcHour = execDate.getUTCHours();
      
      let windowDate = new Date(Date.UTC(execUtcYear, execUtcMonth, execUtcDate));
      // For early morning windows (01:00-05:00 UTC) with late evening execution (20:00-23:59 UTC), 
      // the window date is the NEXT day
      if (windowHour <= 5 && execUtcHour >= 20) {
        windowDate.setUTCDate(windowDate.getUTCDate() + 1);
      }
      // For late evening windows (21:00+) with early morning execution (00:00-05:00 UTC),
      // the window date is the PREVIOUS day
      else if (windowHour >= 21 && execUtcHour <= 5) {
        windowDate.setUTCDate(windowDate.getUTCDate() - 1);
      }
      return windowDate.toISOString().split('T')[0];
    };

    const { actualTrades, accounts: accountsTable } = await import('../../drizzle/schema');
    
    // 1. Get account and its assigned strategy
    const account = await db
      .select()
      .from(accountsTable)
      .where(eq(accountsTable.id, accountId))
      .limit(1);
    
    if (account.length === 0) {
      return res.status(404).json({ error: 'Account not found' });
    }
    
    const assignedStrategyId = account[0].assignedStrategyId;
    if (!assignedStrategyId) {
      return res.status(400).json({ error: 'Account has no assigned strategy' });
    }
    
    // 2. Get the strategy with DNA strands
    const strategy = await db
      .select()
      .from(savedStrategies)
      .where(eq(savedStrategies.id, assignedStrategyId))
      .limit(1);
    
    if (strategy.length === 0) {
      return res.status(404).json({ error: 'Assigned strategy not found' });
    }
    
    const strategyData = strategy[0];
    const dnaStrands = strategyData.dnaStrands || [];
    let windowConfig = strategyData.windowConfig;
    
    if (!dnaStrands.length) {
      return res.status(400).json({ error: 'Strategy has no DNA strands configured' });
    }
    
    // === WINDOW CONFIGURATION PREPARATION ===
    // Use STORED window times for strategy test (not dynamic resolution)
    // Dynamic resolution can change windows (e.g., 21:00 → 01:00 due to epic voting)
    // which breaks comparison matching. Strategy test should use original config.
    
    // Ensure each window has proper close time fields
    if (windowConfig?.windows) {
      windowConfig.windows = windowConfig.windows.map((w: any) => ({
        ...w,
        closeTime: w.closeTime || w.windowCloseTime || '21:00:00',
        windowCloseTime: w.closeTime || w.windowCloseTime || '21:00:00',
      }));
      
      console.log(`[StrategyComparison] Using STORED window times for strategy test:`);
      windowConfig.windows.forEach((w: any, i: number) => {
        console.log(`  Window ${i + 1}: ${w.closeTime} (${w.windowName || 'unnamed'})`);
      });
    }
    
    // 3. Get all actual trades for this account (exclude manual/synced trades)
    // Only compare app-generated trades, not manual trades synced from Capital.com
    const trades = await db
      .select()
      .from(actualTrades)
      .where(and(
        eq(actualTrades.accountId, accountId),
        // Exclude manual and unknown trades - only compare app trades
        // 'app' = bot-generated, null = legacy trades before tradeSource was added
        or(
          eq(actualTrades.tradeSource, 'app'),
          isNull(actualTrades.tradeSource)
        )
      ))
      .orderBy(actualTrades.createdAt);
    
    // 4. Get account transactions for date range
    const { accountTransactions } = await import('../../drizzle/schema');
    
    const transactions = await db
      .select()
      .from(accountTransactions)
      .where(eq(accountTransactions.accountId, accountId))
      .orderBy(asc(accountTransactions.transactionDate));
    
    // Determine date range from BOTH transactions and trades
    const allDates: string[] = [];
    
    // Add trade dates
    trades.forEach(t => {
      if (t.createdAt) {
        allDates.push(new Date(t.createdAt).toISOString().split('T')[0]);
      }
    });
    
    // Add transaction dates
    transactions.forEach(tx => {
      if (tx.transactionDate) {
        allDates.push(new Date(tx.transactionDate).toISOString().split('T')[0]);
      }
    });
    
    if (allDates.length === 0) {
      return res.json({ 
        success: true, 
        comparisons: [],
        summary: { totalWindows: 0, matches: 0, missed: 0, unexpected: 0, failed: 0, correctHolds: 0 },
        message: 'No trades or transactions found for this account'
      });
    }
    
    allDates.sort();
    
    // Calculate warmup days - EXACTLY matching brain.ts calculateRequiredDays()
    const BASE_CANDLES = 300; // Minimum safe buffer
    const SAFETY_MARGIN = 2.0; // 100% extra for safety
    const MIN_DAYS = 5; // Absolute minimum days to load
    
    // Find the longest timeframe in the strategy
    let maxMinutes = 5; // Default to 5m
    
    // Find the largest period parameter across all indicators
    let maxPeriod = BASE_CANDLES;
    
    for (const dna of dnaStrands) {
      // Check timeframe
      const timeframe = dna.timeframe || '5m';
      const tfMatch = timeframe.match(/(\d+)(m|h|d)/);
      if (tfMatch) {
        const value = parseInt(tfMatch[1]);
        const unit = tfMatch[2];
        const minutes = unit === 'm' ? value : unit === 'h' ? value * 60 : value * 1440;
        if (minutes > maxMinutes) maxMinutes = minutes;
      }
      
      // Check indicator parameters for period-related values
      const params = dna.indicatorParams || dna.params || {};
      const periodParams = [
        'period', 'slow_period', 'long_period', 'lookback', 'lookback_period',
        'rank_period', 'atr_period', 'bb_period', 'kc_period', 'dc_period', 'window'
      ];
      
      for (const paramName of periodParams) {
        const value = params[paramName];
        if (typeof value === 'number' && value > maxPeriod) {
          maxPeriod = value;
          console.log(`[StrategyComparison] Found large period param: ${paramName}=${value} in ${dna.indicatorName}`);
        }
      }
    }
    
    // Use 3x the period to ensure indicator has fully stabilized
    const candlesNeeded = Math.max(maxPeriod * 3, BASE_CANDLES);
    
    // Calculate days needed: (candles × minutes per candle) / (minutes per day)
    const minutesNeeded = candlesNeeded * maxMinutes;
    const daysNeeded = minutesNeeded / (60 * 24);
    const warmupDays = Math.max(Math.ceil(daysNeeded * SAFETY_MARGIN), MIN_DAYS);
    
    console.log(`[StrategyComparison] Calculated data window (EXACT brain.ts logic):`);
    console.log(`  - Max timeframe: ${maxMinutes}m`);
    console.log(`  - Max period param: ${maxPeriod}`);
    console.log(`  - Candles needed: ${candlesNeeded} (${maxPeriod} × 3 or ${BASE_CANDLES} minimum)`);
    console.log(`  - Days to load: ${warmupDays} (with ${SAFETY_MARGIN}x safety margin, min ${MIN_DAYS} days)`);
    
    // Start warmupDays before first activity (for indicator warmup), end at last activity
    const firstActivityDate = new Date(allDates[0]);
    firstActivityDate.setDate(firstActivityDate.getDate() - warmupDays);
    const startDate = inputStartDate || firstActivityDate.toISOString().split('T')[0];
    const endDate = inputEndDate || allDates[allDates.length - 1];
    
    // First actual trade date (for filtering comparison results)
    const tradeDates = trades
      .map(t => calculateWindowDateForTrade(t))
      .filter(Boolean) as string[];
    tradeDates.sort();
    
    // Get unique epics from DNA strands for logging
    const strategyEpics = [...new Set(dnaStrands.map((d: any) => d.epic))];
    const windowCloseTimes = windowConfig?.windows?.map((w: any) => w.closeTime) || [];
    
    console.log(`[StrategyComparison] Running for account ${accountId}, strategy ${assignedStrategyId}`);
    console.log(`[StrategyComparison] Date range: ${startDate} to ${endDate}`);
    console.log(`[StrategyComparison] DNA strands: ${dnaStrands.length}, Epics: ${strategyEpics.join(', ')}`);
    console.log(`[StrategyComparison] Windows: ${windowCloseTimes.join(', ')}`);
    console.log(`[StrategyComparison] Actual trades: ${trades.length}`);
    
    // SANITY CHECK: Detect trades made with different strategies
    const tradesWithDifferentStrategy = trades.filter((t: any) => 
      t.strategyId && t.strategyId !== assignedStrategyId
    );
    const uniqueStrategiesUsed = [...new Set(tradesWithDifferentStrategy.map((t: any) => t.strategyId))];
    
    // Build strategy mismatch warnings
    let strategyMismatchWarning: string | null = null;
    let strategyMismatchDetails: Array<{
      strategyId: number;
      tradeCount: number;
      tradeIds: number[];
    }> = [];
    
    if (tradesWithDifferentStrategy.length > 0) {
      console.warn(`[StrategyComparison] ⚠️ WARNING: ${tradesWithDifferentStrategy.length} trades were made with different strategies!`);
      
      for (const stratId of uniqueStrategiesUsed) {
        const tradesForStrat = tradesWithDifferentStrategy.filter((t: any) => t.strategyId === stratId);
        strategyMismatchDetails.push({
          strategyId: stratId as number,
          tradeCount: tradesForStrat.length,
          tradeIds: tradesForStrat.map((t: any) => t.id),
        });
        console.warn(`[StrategyComparison]   - Strategy ${stratId}: ${tradesForStrat.length} trades`);
      }
      
      strategyMismatchWarning = `${tradesWithDifferentStrategy.length} trades were made with different strategies (${uniqueStrategiesUsed.join(', ')}). These will show as 'Different Strategy' and cannot be accurately compared.`;
    }
    
    // 5. Run the strategy executor
    const { spawnPython } = await import('../python_spawn');
    const path = await import('path');
    const PYTHON_ENGINE_DIR = path.join(process.cwd(), 'python_engine');
    const STRATEGY_EXECUTOR = path.join(PYTHON_ENGINE_DIR, 'strategy_executor.py');
    
    // Use provided values or defaults
    const initialBalance = inputInitialBalance || account[0].balance || 1000;
    const monthlyTopup = inputMonthlyTopup || 0;
    
    // NEW: Fetch capital events (deposits/withdrawals) if requested
    let capitalEvents: Array<{ date: string; type: 'deposit' | 'withdrawal'; amount: number }> = [];
    
    if (useRealCapitalEvents) {
      console.log(`[StrategyComparison] Fetching real capital events for apples-to-apples comparison`);
      
      // Filter transactions for DEPOSIT and WITHDRAWAL only
      const capitalMovements = transactions.filter(tx => 
        tx.transactionType === 'DEPOSIT' || tx.transactionType === 'WITHDRAWAL'
      );
      
      // Format for Python
      capitalEvents = capitalMovements.map(tx => ({
        date: tx.transactionDate 
          ? new Date(tx.transactionDate).toISOString().split('T')[0]
          : '',
        type: tx.transactionType === 'DEPOSIT' ? 'deposit' as const : 'withdrawal' as const,
        amount: Math.abs(tx.amount || 0),
      })).filter(e => e.date && e.amount > 0);
      
      // Sort by date
      capitalEvents.sort((a, b) => a.date.localeCompare(b.date));
      
      console.log(`[StrategyComparison] Found ${capitalEvents.length} capital events:`, 
        capitalEvents.map(e => `${e.date}: ${e.type} $${e.amount}`).join(', ')
      );
    }
    
    // Always extend end date to include ALL actual trades + today
    const today = new Date().toISOString().split('T')[0];
    const latestActualTrade = allDates.length > 0 ? allDates[allDates.length - 1] : today;
    const effectiveEndDate = endDate 
      ? (endDate > latestActualTrade ? endDate : latestActualTrade)
      : latestActualTrade;
    // Ensure we include today (for same-day comparisons)
    const finalEndDate = effectiveEndDate > today ? effectiveEndDate : today;
    
    console.log(`[StrategyComparison] Date range: ${startDate} → ${finalEndDate} (auto-extended to include all trades)`);
    
    const pythonConfig = {
      start_date: startDate,
      end_date: finalEndDate,
      initial_balance: initialBalance,
      monthly_topup: monthlyTopup,
      dna_strands: dnaStrands,
      window_config: windowConfig || { windows: [], totalAccountBalancePct: 99 },
      calculation_mode: 'standard',
      // NEW: Pass capital events for apples-to-apples comparison
      capital_events: capitalEvents,
    };
    
    const configJson = JSON.stringify(pythonConfig);
    
    let strategyResult: any;
    try {
      strategyResult = await new Promise((resolve, reject) => {
        const pythonProcess = spawnPython(STRATEGY_EXECUTOR, {
          args: [configJson],
          cwd: PYTHON_ENGINE_DIR,
          env: { ...process.env, PYTHONUNBUFFERED: '1' },
        });

        let stdout = '';
        let stderr = '';

        pythonProcess.stdout?.on('data', (data: Buffer) => {
          stdout += data.toString();
        });

        pythonProcess.stderr?.on('data', (data: Buffer) => {
          stderr += data.toString();
          // Log progress
          const lines = data.toString().split('\n');
          lines.forEach((line: string) => {
            if (line.trim()) {
              console.log(`[StrategyComparison] ${line}`);
            }
          });
        });

        pythonProcess.on('close', (code: number) => {
          if (code !== 0) {
            reject(new Error(`Strategy executor failed: ${stderr}`));
            return;
          }

          const resultMatch = stdout.match(/RESULT:(.+)/);
          if (!resultMatch) {
            reject(new Error('No result found in strategy output'));
            return;
          }

          try {
            resolve(JSON.parse(resultMatch[1]));
          } catch (e) {
            reject(new Error(`Failed to parse strategy result: ${e}`));
          }
        });

        pythonProcess.on('error', (err: Error) => reject(err));
      });
    } catch (error: any) {
      console.error('[StrategyComparison] Strategy execution failed:', error);
      return res.status(500).json({ error: `Strategy execution failed: ${error.message}` });
    }
    
    // 6. Build window-by-window comparison
    // Strategy trades are in strategyResult.trades
    const strategyTrades = strategyResult.trades || [];
    
    console.log(`[StrategyComparison] Strategy would have made ${strategyTrades.length} trades`);
    
    // DEBUG: Log strategy trades to understand date/window matching
    if (strategyTrades.length > 0) {
      console.log(`[StrategyComparison] Strategy trades:`);
      strategyTrades.slice(0, 10).forEach((t: any) => {
        console.log(`  - ${t.epic} on ${t.date || t.entry_date} @ ${t.window_close_time || t.windowCloseTime} (entry: ${t.entry_price})`);
      });
    }
    
    // 6a. Load candle data for variance calculation
    // This compares T-60 price vs 4th 1m candle (what backtest uses)
    // Use candle_variance_service to get the SAME candle the brain uses
    const candleMap = new Map<string, any>();
    
    // For each trade, get the 4th 1m candle using the SAME logic as brain
    const { get4th1mCandleClose } = await import('../services/candle_variance_service');
    
    console.log(`[StrategyComparison] Getting 4th 1m candles for ${trades.length} trades...`);
    
    for (const trade of trades) {
      if (!trade.epic || !trade.windowCloseTime) continue;
      
      const tradeDate = calculateWindowDateForTrade(trade);
      if (!tradeDate) continue;
      
      const windowTime = trade.windowCloseTime.split(' ')[1] || trade.windowCloseTime || '';
      
      try {
        // Use the EXACT same function the brain uses to get 4th 1m candle
        const windowDate = new Date(tradeDate + 'T00:00:00Z');
        const candleResult = await get4th1mCandleClose(
          trade.epic,
          windowDate,
          windowTime,
          'Fake5min_4thCandle'
        );
        
        if (candleResult.fake5minClose) {
          // Store with simple key (epic_date) - matches how display code looks it up
          const candleKey = `${trade.epic}_${tradeDate}`;
          candleMap.set(candleKey, {
            timestamp: candleResult.last1mCandleTime,
            closeBid: candleResult.fake5minClose,  // Using mid as both bid/ask
            closeAsk: candleResult.fake5minClose,
            fake5minClose: candleResult.fake5minClose,
          });
          
          console.log(`[StrategyComparison] ✅ ${candleKey}: 4th 1m = $${candleResult.fake5minClose.toFixed(4)}`);
        } else {
          console.log(`[StrategyComparison] ❌ ${trade.epic} ${tradeDate} ${windowTime}: 4th 1m candle not found`);
        }
      } catch (err: any) {
        console.warn(`[StrategyComparison] Failed to get 4th 1m candle for ${trade.epic} ${tradeDate}: ${err.message}`);
      }
    }
    
    console.log(`[StrategyComparison] Candle map has ${candleMap.size} entries (using 4th 1m candles)`);
    
    // Create a map of actual trades by date + window
    // Prefer trades with entryPrice and status='open' over duplicates (pending without price)
    const actualTradesMap = new Map<string, any>();
    trades.forEach(trade => {
      const tradeDate = calculateWindowDateForTrade(trade);
      const windowTime = trade.windowCloseTime?.split(' ')[1] || trade.windowCloseTime || '';
      const key = `${tradeDate}_${windowTime}`;
      
      const existingTrade = actualTradesMap.get(key);
      if (!existingTrade) {
        // No existing trade for this key, add it
        actualTradesMap.set(key, trade);
      } else {
        // Prefer trade with entryPrice over one without
        // Prefer status='open' over 'pending'
        const existingHasPrice = existingTrade.entryPrice != null;
        const newHasPrice = trade.entryPrice != null;
        const existingIsOpen = existingTrade.status === 'open' || existingTrade.status === 'closed';
        const newIsOpen = trade.status === 'open' || trade.status === 'closed';
        
        if ((!existingHasPrice && newHasPrice) || (!existingIsOpen && newIsOpen && newHasPrice)) {
          console.log(`[StrategyComparison] Preferring trade ${trade.id} (price=${trade.entryPrice}, status=${trade.status}) over ${existingTrade.id} (price=${existingTrade.entryPrice}, status=${existingTrade.status})`);
          actualTradesMap.set(key, trade);
        }
      }
    });
    
    // Create a map of strategy trades by date + window
    const strategyTradesMap = new Map<string, any>();
    strategyTrades.forEach((trade: any) => {
      const tradeDate = trade.date || trade.entry_date || '';
      const windowTime = trade.window_close_time || trade.windowCloseTime || '';
      const key = `${tradeDate}_${windowTime}`;
      strategyTradesMap.set(key, trade);
    });
    
    // Add alternate window matching for dynamic resolution (Friday early close, etc.)
    // This allows strategy trades at stored time (01:00) to match actual trades at resolved time (22:00)
    actualTradesMap.forEach((actualTrade, actualKey) => {
      if (!strategyTradesMap.has(actualKey)) {
        const [tradeDate, actualWindow] = actualKey.split('_');
        
        // Try common alternate windows
        const alternates: Record<string, string> = {
          '22:00:00': '01:00:00',  // Friday early close → normal extended hours
          '01:00:00': '22:00:00',  // Reverse
          '21:00:00': '20:00:00',  // Possible early regular close
        };
        
        const altWindow = alternates[actualWindow];
        if (altWindow) {
          const altKey = `${tradeDate}_${altWindow}`;
          if (strategyTradesMap.has(altKey)) {
            // Found a match at alternate window - use it
            strategyTradesMap.set(actualKey, strategyTradesMap.get(altKey));
            console.log(`[StrategyComparison] Matched ${actualKey} to alternate ${altKey} (dynamic window resolution)`);
          }
        }
      }
    });
    
    // DEBUG: Log the actual trade keys vs strategy trade keys
    console.log(`[StrategyComparison] Actual trade keys: ${[...actualTradesMap.keys()].join(', ')}`);
    console.log(`[StrategyComparison] Strategy trade keys: ${[...strategyTradesMap.keys()].join(', ')}`);
    
    // DEBUG: Log details for TECL to trace the date issue
    actualTradesMap.forEach((trade, key) => {
      if (trade.epic === 'TECL') {
        const execTime = trade.capitalExecutedAt || trade.createdAt;
        const execUtc = execTime ? new Date(String(execTime).includes('Z') ? execTime : execTime + 'Z').toISOString() : 'N/A';
        console.log(`[StrategyComparison] TECL actual trade: key=${key}, capitalExecutedAt=${trade.capitalExecutedAt}, createdAt=${trade.createdAt}, execUTC=${execUtc}, windowCloseTime=${trade.windowCloseTime}`);
      }
    });
    strategyTradesMap.forEach((trade, key) => {
      if (trade.epic === 'TECL') {
        console.log(`[StrategyComparison] TECL strategy trade: key=${key}, date=${trade.date}, window_close_time=${trade.window_close_time}`);
      }
    });
    
    // Combine all unique keys
    const allKeys = new Set([...actualTradesMap.keys(), ...strategyTradesMap.keys()]);
    
    const comparisons: any[] = [];
    let matches = 0;
    let missed = 0;
    let unexpected = 0;
    let correctHolds = 0;
    
    // Count failed trades separately
    let failed = 0;
    
    allKeys.forEach(key => {
      const [tradeDate, windowTime] = key.split('_');
      const actualTrade = actualTradesMap.get(key);
      const strategyTrade = strategyTradesMap.get(key);
      
      const strategySignal = strategyTrade ? 'BUY' : 'HOLD';
      
      // Determine actual signal - treat failed trades differently
      // If trade exists but status is 'failed' or 'error', the trade was attempted but rejected
      const tradeWasFailed = actualTrade && (actualTrade.status === 'failed' || actualTrade.status === 'error');
      const actualSignal = actualTrade && !tradeWasFailed ? 'BUY' : 'HOLD';
      
      // Check if the trade was made with a DIFFERENT strategy than currently assigned
      // This can happen if the user changed strategies after trades were made
      const tradeUsedDifferentStrategy = actualTrade && 
        actualTrade.strategyId && 
        actualTrade.strategyId !== assignedStrategyId;
      
      let status: 'match' | 'missed' | 'unexpected' | 'correct_hold' | 'failed' | 'strategy_mismatch';
      
      if (tradeWasFailed) {
        // Trade was attempted but rejected by Capital.com
        status = 'failed';
        failed++;
      } else if (tradeUsedDifferentStrategy && actualSignal === 'BUY') {
        // Trade was made with a different strategy - can't compare accurately
        status = 'strategy_mismatch';
        // Don't count as unexpected - it's just a different strategy
      } else if (strategySignal === 'BUY' && actualSignal === 'BUY') {
        status = 'match';
        matches++;
      } else if (strategySignal === 'BUY' && actualSignal === 'HOLD') {
        status = 'missed';
        missed++;
      } else if (strategySignal === 'HOLD' && actualSignal === 'BUY') {
        status = 'unexpected';
        unexpected++;
      } else {
        status = 'correct_hold';
        correctHolds++;
      }
      
      // Calculate T-60 vs actual 5m close variance for "unexpected" trades
      // This shows why the live trade happened (T-60 price) vs backtest (actual close)
      let t60Price: number | null = null;
      let actual5mClose: number | null = null;
      let priceVariance: number | null = null;
      let priceVariancePct: number | null = null;
      
      if (actualTrade) {
        console.log(`[StrategyComparison] Trade ${actualTrade.id}: entryPrice=${actualTrade.entryPrice}, epic=${actualTrade.epic}`);
        
        // PRIORITY 1: Use stored brain data (what brain actually used at T-60)
        if (actualTrade.fake5minClose) {
          actual5mClose = parseFloat(actualTrade.fake5minClose);
          t60Price = actualTrade.brainCalcPrice 
            ? parseFloat(actualTrade.brainCalcPrice) 
            : (actualTrade.entryPrice ? parseFloat(actualTrade.entryPrice) : null);
          priceVariancePct = actualTrade.priceVariancePct 
            ? parseFloat(actualTrade.priceVariancePct) 
            : null;
          
          // Recalculate variance if stored value is null but we have both prices
          if (t60Price !== null && actual5mClose !== null && priceVariancePct === null) {
            priceVariance = t60Price - actual5mClose;
            priceVariancePct = (priceVariance / actual5mClose) * 100;
          }
          
          console.log(`[StrategyComparison] Using stored brain data: t60=$${t60Price?.toFixed(4) || 'N/A'}, fake5min=$${actual5mClose.toFixed(4)}, variance=${priceVariancePct?.toFixed(4) || 'N/A'}%`);
        }
        // PRIORITY 2: Fallback to candle lookup (for old trades without stored data)
        else {
          if (actualTrade.entryPrice) {
            t60Price = parseFloat(actualTrade.entryPrice);
          }
          
          // Look up the actual 5m candle close
          const candleKey = `${actualTrade.epic}_${tradeDate}`;
          const candle = candleMap.get(candleKey);
          console.log(`[StrategyComparison] Looking up candle: ${candleKey}, found=${!!candle}`);
          
          if (candle) {
            // Calculate mid price from bid/ask (closeBid + closeAsk) / 2
            const closeBid = parseFloat(candle.closeBid || '0');
            const closeAsk = parseFloat(candle.closeAsk || '0');
            console.log(`[StrategyComparison] Candle ${candleKey}: closeBid=${closeBid}, closeAsk=${closeAsk}`);
            
            if (closeBid > 0 && closeAsk > 0) {
              actual5mClose = (closeBid + closeAsk) / 2;
              if (t60Price !== null) {
                priceVariance = t60Price - actual5mClose;
                priceVariancePct = (priceVariance / actual5mClose) * 100;
                console.log(`[StrategyComparison] Variance: t60=${t60Price}, 5mClose=${actual5mClose}, variance=${priceVariancePct.toFixed(3)}%`);
              }
            }
          }
          
          console.log(`[StrategyComparison] No stored brain data, using candle lookup fallback`);
        }
      }
      
      // Build error details for failed/error trades
      let errorDetails: any = null;
      if (actualTrade && (actualTrade.status === 'failed' || actualTrade.status === 'error')) {
        errorDetails = {
          status: actualTrade.status,
          closeReason: actualTrade.closeReason || null,
          dealReference: actualTrade.dealReference || null,
          dealId: actualTrade.dealId || null,
          // Include allDnaResults if available for debugging
          brainDecision: actualTrade.brainDecision,
          indicatorAttempted: actualTrade.winningIndicatorName,
          epicAttempted: actualTrade.epic,
          leverageAttempted: actualTrade.leverage,
          createdAt: actualTrade.createdAt?.toISOString() || null,
        };
      }
      
      comparisons.push({
        tradeDate,
        windowCloseTime: windowTime,
        
        // Strategy info
        strategySignal,
        strategyEpic: strategyTrade?.epic || strategyTrade?.dna_name?.split(' ')[0] || null,
        strategyIndicator: strategyTrade?.dna_name || strategyTrade?.indicatorName || null,
        strategyPnl: strategyTrade?.net_pnl || strategyTrade?.gross_pnl || 0,
        
        // Actual trade info
        actualSignal,
        actualEpic: actualTrade?.epic || null,
        actualIndicator: actualTrade?.winningIndicatorName || null,
        actualPnl: actualTrade?.netPnl || 0,
        actualTradeId: actualTrade?.id || null,
        
        // NEW: Extended actual trade details
        actualEntryPrice: actualTrade?.entryPrice || null,
        actualExitPrice: actualTrade?.exitPrice || null,
        actualContracts: actualTrade?.contracts || null,
        actualLeverage: actualTrade?.leverage || null,
        actualStopLossPercent: actualTrade?.stopLossPercent || null,
        actualStopLossPrice: actualTrade?.stopLossPrice || null,
        actualGrossPnl: actualTrade?.grossPnl || null,
        actualSpreadCost: actualTrade?.spreadCost || null,
        actualOvernightCost: actualTrade?.overnightCost || null,
        actualStatus: actualTrade?.status || null,
        actualCloseReason: actualTrade?.closeReason || null,
        actualTradeSource: actualTrade?.tradeSource || null,
        
        // NEW: Capital.com references
        dealId: actualTrade?.dealId || null,
        dealReference: actualTrade?.dealReference || null,
        capitalExecutedAt: actualTrade?.capitalExecutedAt?.toISOString() || null,
        openedAt: actualTrade?.openedAt?.toISOString() || null,
        closedAt: actualTrade?.closedAt?.toISOString() || null,
        
        // NEW: Brain decision details (for validation/debugging)
        brainDecision: actualTrade?.brainDecision || null,
        indicatorParams: actualTrade?.indicatorParams || null,
        conflictResolutionMetric: actualTrade?.conflictResolutionMetric || null,
        
        // NEW: Strategy mismatch detection
        // Shows when trade was made with a different strategy than currently assigned
        tradeUsedDifferentStrategy,
        tradeUsedStrategyId: actualTrade?.strategyId || null,
        currentAssignedStrategyId: assignedStrategyId,
        
        // NEW: Error details for copy/paste debugging
        errorDetails,
        
        // T-60 vs 5m close variance (for understanding "unexpected" trades)
        // Shows difference between price used for brain calculation vs actual candle close
        t60Price,           // Price used at T-60 for brain calculation
        actual5mClose,      // Actual 5m candle close price
        priceVariance,      // Absolute difference (t60Price - actual5mClose)
        priceVariancePct,   // Percentage difference
        
        // Comparison
        status,
        pnlDifference: (actualTrade?.netPnl || 0) - (strategyTrade?.net_pnl || strategyTrade?.gross_pnl || 0),
      });
    });
    
    // Sort by date
    comparisons.sort((a, b) => {
      const dateCompare = a.tradeDate.localeCompare(b.tradeDate);
      if (dateCompare !== 0) return dateCompare;
      return a.windowCloseTime.localeCompare(b.windowCloseTime);
    });
    
    // Filter to only dates from first actual trade onwards (not warmup period)
    const firstActualTradeDate = tradeDates[0];
    const lastActualTradeDate = tradeDates[tradeDates.length - 1];
    const filteredComparisons = comparisons.filter(c => 
      c.tradeDate >= firstActualTradeDate && c.tradeDate <= lastActualTradeDate
    );
    
    // IMPORTANT: Recalculate stats based on FILTERED comparisons only
    // The initial counts include warmup period which shouldn't count
    matches = filteredComparisons.filter(c => c.status === 'match').length;
    missed = filteredComparisons.filter(c => c.status === 'missed').length;
    unexpected = filteredComparisons.filter(c => c.status === 'unexpected').length;
    correctHolds = filteredComparisons.filter(c => c.status === 'correct_hold').length;
    failed = filteredComparisons.filter(c => c.status === 'failed').length;
    const totalWindows = filteredComparisons.filter(c => c.status !== 'start').length;
    const matchRate = (totalWindows > 0 ? ((matches / (matches + missed + unexpected)) * 100).toFixed(1) : '0.0');
    
    console.log(`[StrategyComparison] After filtering to ${firstActualTradeDate} - ${lastActualTradeDate}:`);
    console.log(`[StrategyComparison]   Total windows: ${totalWindows}, Matches: ${matches}, Missed: ${missed}, Unexpected: ${unexpected}`);
    
    // 7a. Fetch strategy assignment history for this account
    const { strategyAssignmentHistory } = await import('../../drizzle/schema');
    let strategyChanges: Array<{
      date: string;
      action: 'assigned' | 'removed' | 'edited';
      strategyId: number;
      strategyName?: string;
      previousStrategyId?: number;
    }> = [];
    
    try {
      const assignmentHistory = await db
        .select()
        .from(strategyAssignmentHistory)
        .where(eq(strategyAssignmentHistory.accountId, accountId))
        .orderBy(asc(strategyAssignmentHistory.assignedAt));
      
      strategyChanges = assignmentHistory.map(h => ({
        date: h.assignedAt ? new Date(h.assignedAt).toISOString().split('T')[0] : '',
        action: h.action as 'assigned' | 'removed' | 'edited',
        strategyId: h.strategyId,
        previousStrategyId: h.previousStrategyId || undefined,
      }));
      console.log(`[StrategyComparison] Found ${strategyChanges.length} strategy changes`);
    } catch (e) {
      console.log(`[StrategyComparison] No strategy assignment history found`);
    }
    
    // 7b. Group capital events by date for easy lookup
    const capitalEventsByDate = new Map<string, Array<{type: 'deposit' | 'withdrawal', amount: number}>>();
    capitalEvents.forEach(e => {
      if (!capitalEventsByDate.has(e.date)) {
        capitalEventsByDate.set(e.date, []);
      }
      capitalEventsByDate.get(e.date)!.push({ type: e.type, amount: e.amount });
    });
    
    // 7c. Get swap fees (overnight costs) by date from transactions
    const swapFeesByDate = new Map<string, number>();
    transactions
      .filter(tx => tx.transactionType === 'SWAP')
      .forEach(tx => {
        const date = tx.transactionDate.toISOString().split('T')[0];
        swapFeesByDate.set(date, (swapFeesByDate.get(date) || 0) + (tx.amount || 0));
      });
    
    // 7. Calculate cumulative BALANCE for chart (starting balance + P&L + capital events)
    let strategyBalance = initialBalance;
    let actualBalance = initialBalance;
    let strategyPnlTotal = 0;
    let actualPnlTotal = 0;
    let totalSwapFees = 0;
    
    // Add starting point before first trade
    const chartData: any[] = [{
      tradeDate: firstActualTradeDate,
      windowCloseTime: 'START',
      strategySignal: 'START',
      actualSignal: 'START',
      strategyPnl: 0,
      actualPnl: 0,
      status: 'start',
      strategyBalance: initialBalance,
      actualBalance: initialBalance,
      strategyCumulative: 0, // P&L only (for secondary view)
      actualCumulative: 0,
      capitalEvents: null,
      swapFees: 0,
      strategyChange: null,
    }];
    
    // Track processed dates for capital events (to avoid double-counting)
    const processedDates = new Set<string>();
    
    // Build a map of all dates from first to last trade (for interpolation)
    const allDatesInRange: string[] = [];
    if (firstActualTradeDate && lastActualTradeDate) {
      const start = new Date(firstActualTradeDate);
      const end = new Date(lastActualTradeDate);
      const current = new Date(start);
      while (current <= end) {
        allDatesInRange.push(current.toISOString().split('T')[0]);
        current.setDate(current.getDate() + 1);
      }
    }
    
    // Create a map of comparisons by date for quick lookup
    const comparisonsByDate = new Map<string, any[]>();
    filteredComparisons.forEach(c => {
      if (!comparisonsByDate.has(c.tradeDate)) {
        comparisonsByDate.set(c.tradeDate, []);
      }
      comparisonsByDate.get(c.tradeDate)!.push(c);
    });
    
    // Process each date in the range, filling in missing days
    allDatesInRange.forEach(date => {
      const comparisonsForDate = comparisonsByDate.get(date) || [];
      
      // Check for capital events on this date (only apply once per date)
      let dayDeposits = 0;
      let dayWithdrawals = 0;
      let daySwapFees = 0;
      const eventsForDay = capitalEventsByDate.get(date);
      const strategyChangeForDay = strategyChanges.find(s => s.date === date);
      
      if (!processedDates.has(date)) {
        processedDates.add(date);
        
        // Apply capital events
        if (eventsForDay && useRealCapitalEvents) {
          eventsForDay.forEach(e => {
            if (e.type === 'deposit') {
              dayDeposits += e.amount;
              actualBalance += e.amount;
              strategyBalance += e.amount; // Strategy also gets the deposit
            } else {
              dayWithdrawals += e.amount;
              actualBalance -= e.amount;
              strategyBalance -= e.amount; // Strategy also loses to withdrawal
            }
          });
        }
        
        // Apply swap fees
        daySwapFees = swapFeesByDate.get(date) || 0;
        if (daySwapFees !== 0) {
          actualBalance += daySwapFees; // Already negative
          totalSwapFees += daySwapFees;
        }
      }
      
      // If there are trades on this date, add them
      if (comparisonsForDate.length > 0) {
        comparisonsForDate.forEach(c => {
          // Add P&L
          strategyBalance += c.strategyPnl || 0;
          actualBalance += c.actualPnl || 0;
          strategyPnlTotal += c.strategyPnl || 0;
          actualPnlTotal += c.actualPnl || 0;
          
          chartData.push({
            ...c,
            strategyBalance: strategyBalance || 0, // Ensure always a number
            actualBalance: actualBalance || 0, // Ensure always a number
            strategyCumulative: strategyPnlTotal || 0,
            actualCumulative: actualPnlTotal || 0,
            // Include capital events for this day (for chart markers)
            capitalEvents: eventsForDay && eventsForDay.length > 0 ? {
              deposits: dayDeposits,
              withdrawals: dayWithdrawals,
            } : null,
            swapFees: daySwapFees,
            // Include strategy changes (for yellow dot markers)
            strategyChange: strategyChangeForDay || null,
          });
        });
      } else {
        // No trades on this date - create an interpolated entry with last known balance
        // This ensures the chart shows continuous progression even on non-trading days
        chartData.push({
          tradeDate: date,
          windowCloseTime: '00:00:00', // Placeholder for non-trading days
          strategySignal: 'HOLD',
          actualSignal: 'HOLD',
          strategyPnl: 0,
          actualPnl: 0,
          status: 'correct_hold',
          strategyBalance: strategyBalance || initialBalance, // Ensure always a number, fallback to initial
          actualBalance: actualBalance || initialBalance, // Ensure always a number, fallback to initial
          strategyCumulative: strategyPnlTotal || 0,
          actualCumulative: actualPnlTotal || 0,
          // Include capital events for this day (for chart markers)
          capitalEvents: eventsForDay && eventsForDay.length > 0 ? {
            deposits: dayDeposits,
            withdrawals: dayWithdrawals,
          } : null,
          swapFees: daySwapFees,
          // Include strategy changes (for yellow dot markers)
          strategyChange: strategyChangeForDay || null,
        });
      }
    });
    
    // Sort chart data by date to ensure chronological order for Recharts
    chartData.sort((a, b) => {
      const dateCompare = a.tradeDate.localeCompare(b.tradeDate);
      if (dateCompare !== 0) return dateCompare;
      // If same date, sort by window time (START first, then by time)
      if (a.windowCloseTime === 'START') return -1;
      if (b.windowCloseTime === 'START') return 1;
      return a.windowCloseTime.localeCompare(b.windowCloseTime);
    });
    
    // Ensure all balance values are numbers (not null/undefined)
    chartData.forEach(entry => {
      if (entry.strategyBalance == null) entry.strategyBalance = initialBalance;
      if (entry.actualBalance == null) entry.actualBalance = initialBalance;
      if (typeof entry.strategyBalance !== 'number') entry.strategyBalance = Number(entry.strategyBalance) || initialBalance;
      if (typeof entry.actualBalance !== 'number') entry.actualBalance = Number(entry.actualBalance) || initialBalance;
    });
    
    console.log(`[StrategyComparison] Chart data: ${chartData.length} entries from ${chartData[0]?.tradeDate} to ${chartData[chartData.length - 1]?.tradeDate}`);
    console.log(`[StrategyComparison] Sample entries:`, chartData.slice(0, 3).map(e => ({
      date: e.tradeDate,
      window: e.windowCloseTime,
      strategyBalance: e.strategyBalance,
      actualBalance: e.actualBalance,
      status: e.status
    })));
    console.log(`[StrategyComparison] Results: ${matches} matches, ${missed} missed, ${unexpected} unexpected, ${failed} failed, ${correctHolds} correct holds`);
    console.log(`[StrategyComparison] Initial balance: $${initialBalance}, Strategy final: $${strategyBalance.toFixed(2)}, Actual final: $${actualBalance.toFixed(2)}`);
    
    // Calculate total capital events
    const totalDeposits = capitalEvents
      .filter(e => e.type === 'deposit')
      .reduce((sum, e) => sum + e.amount, 0);
    const totalWithdrawals = capitalEvents
      .filter(e => e.type === 'withdrawal')
      .reduce((sum, e) => sum + e.amount, 0);
    
    res.json({
      success: true,
      comparisons: chartData,
      // Include capital events array for chart markers
      capitalEvents: capitalEvents.map(e => ({
        date: e.date,
        type: e.type,
        amount: e.amount,
      })),
      // Include strategy changes for chart markers
      strategyChanges,
      summary: {
        strategyName: strategyData.name,
        dateRange: { startDate: firstActualTradeDate, endDate: lastActualTradeDate },
        initialBalance,
        monthlyTopup,
        totalWindows,
        matches,
        missed,
        unexpected,
        failed,       // Trades that were attempted but rejected by Capital.com
        correctHolds,
        matchRate: parseFloat(matchRate),
        strategyPnl: strategyPnlTotal,
        actualPnl: actualPnlTotal,
        pnlDifference: actualPnlTotal - strategyPnlTotal,
        strategyFinalBalance: strategyBalance,
        actualFinalBalance: actualBalance,
        strategyEpics,
        windowCloseTimes,
        
        // Capital events summary
        capitalEventsUsed: useRealCapitalEvents,
        capitalEventsCount: capitalEvents.length,
        totalDeposits,
        totalWithdrawals,
        netCapitalMovement: totalDeposits - totalWithdrawals,
        
        // Fee summary
        totalSwapFees,
        
        // Strategy mismatch warning
        // Shows when trades were made with different strategies than currently assigned
        strategyMismatchWarning,
        strategyMismatchDetails,
        strategyMismatchCount: tradesWithDifferentStrategy.length,
        strategyChangesCount: strategyChanges.length,
      },
    });
    
  } catch (error: any) {
    console.error('[StrategyComparison] Error:', error);
    res.status(500).json({ error: error.message });
  }
});

/**
 * ============================================================================
 * PHASE 3: GET /api/live-trading/strategies/:id/windows
 * ============================================================================
 * Detect execution windows for a strategy with DYNAMIC close time resolution
 * 
 * CHANGE: Now resolves close times dynamically from current market data
 *         instead of returning stale stored values.
 * 
 * ROLLBACK: Replace `currentCloseTime` with `storedCloseTime` in the return
 * ============================================================================
 */
router.get('/strategies/:id/windows', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const strategyId = parseInt(req.params.id);
    if (isNaN(strategyId)) {
      return res.status(400).json({ error: 'Invalid strategy ID' });
    }

    // PHASE 3: Import window resolver for dynamic close time resolution
    const { resolveWindowCloseTime, isUnusualMarketDay } = await import('../services/window_resolver');

    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    const strategy = await db
      .select()
      .from(savedStrategies)
      .where(eq(savedStrategies.id, strategyId))
      .limit(1);

    if (strategy.length === 0) {
      return res.status(404).json({ error: `Strategy ${strategyId} not found` });
    }

    const windowConfig = strategy[0].windowConfig;
    if (!windowConfig || !windowConfig.windows || windowConfig.windows.length === 0) {
      return res.json({ 
        success: true, 
        config: { windows: [], mode: 'carry_over' },
        message: 'No window configuration available'
      });
    }

    // Check if today is an unusual market day
    const unusualDayInfo = await isUnusualMarketDay();

    // Transform windows with DYNAMIC close time resolution
    const dnaStrands = strategy[0].dnaStrands || [];
    const transformedWindows = await Promise.all(
      windowConfig.windows.map(async (window: any) => {
        // Get epics from DNA strands in this window
        const windowEpics = dnaStrands
          .filter((strand: any) => window.dnaStrandIds?.includes(strand.id))
          .map((strand: any) => strand.epic);
        
        const uniqueEpics = Array.from(new Set(windowEpics)) as string[];
        
        // PHASE 3: Resolve close time DYNAMICALLY from current market data
        const currentCloseTime = uniqueEpics.length > 0 
          ? await resolveWindowCloseTime(uniqueEpics)
          : window.closeTime || '21:00:00';
        
        const storedCloseTime = window.closeTime || '21:00:00';
        const isStale = currentCloseTime !== storedCloseTime;
        
        // Log if stale for debugging
        if (isStale) {
          console.log(`[WindowsEndpoint] Strategy ${strategyId} window has stale time: stored=${storedCloseTime}, current=${currentCloseTime}`);
        }
        
        return {
          closeTime: currentCloseTime,       // DYNAMIC - use this!
          storedCloseTime: storedCloseTime,  // Keep for reference/rollback
          isStale: isStale,                  // Flag for UI warning
          marketName: window.windowName || `Window (${currentCloseTime})`,
          epics: uniqueEpics,
          allocationPct: window.allocationPct || 0,
        };
      })
    );

    res.json({ 
      success: true, 
      config: {
        windows: transformedWindows,
        mode: 'carry_over',
      },
      // PHASE 3: Include unusual day warning for UI
      unusualDay: unusualDayInfo.unusual,
      unusualDayReason: unusualDayInfo.reason,
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error detecting windows:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

// =============================================================================
// ACCOUNT TRANSACTIONS - Balance History & P&L Tracking
// =============================================================================

import {
  getAccountTransactions,
  getAccountTransactionsInRange,
  getLatestTransactionDate,
  insertAccountTransactionsBatch,
  getTransactionSummaryByType,
  deleteAccountTransactions,
} from '../db';

/**
 * GET /api/live-trading/accounts/:id/transactions
 * Get all stored transactions for balance history chart
 */
router.get('/accounts/:id/transactions', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    // Get the account
    const [acc] = await db.select().from(accounts).where(eq(accounts.id, accountId)).limit(1);
    if (!acc) {
      return res.status(404).json({ error: 'Account not found' });
    }

    // Get transactions
    const transactions = await getAccountTransactions(accountId);
    
    // Get summary by type
    const summary = await getTransactionSummaryByType(accountId);

    res.json({
      success: true,
      accountId,
      accountName: acc.accountName,
      transactions,
      summary,
      totalTransactions: transactions.length,
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error fetching transactions:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/sync-transactions
 * Sync ALL transaction types from Capital.com (TRADE, SWAP, DEPOSIT, WITHDRAWAL, etc.)
 * This enables balance history reconstruction
 */
router.post('/accounts/:id/sync-transactions', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const { days = 30, fullResync = false, startingBalance } = req.body;
    
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    // Get the account with Capital.com ID
    const [acc] = await db.select().from(accounts).where(eq(accounts.id, accountId)).limit(1);
    if (!acc) {
      return res.status(404).json({ error: 'Account not found' });
    }

    // Create API client
    const environment = acc.accountType as 'demo' | 'live';
    const client = await createCapitalAPIClient(environment);
    if (!client) {
      return res.status(500).json({ error: 'Failed to create Capital.com client' });
    }

    // Get Capital.com account ID - stored in 'accountId' column
    // If not available, fetch from Capital.com API by matching account name
    let capitalAccountId = acc.accountId;
    if (!capitalAccountId) {
      console.log(`[SyncTransactions] No accountId stored for ${acc.accountName}, fetching from Capital.com...`);
      const capitalAccounts = await client.getAccounts();
      const matchingAccount = capitalAccounts.find(a => a.accountName === acc.accountName);
      if (matchingAccount) {
        capitalAccountId = matchingAccount.accountId;
        console.log(`[SyncTransactions] Found Capital.com account ID: ${capitalAccountId}`);
      } else {
        return res.status(400).json({ error: `Could not find Capital.com account for ${acc.accountName}` });
      }
    }

    // Switch to the correct account
    await client.switchAccount(capitalAccountId);

    // If fullResync, delete existing transactions first
    if (fullResync) {
      const deleted = await deleteAccountTransactions(accountId);
      console.log(`[SyncTransactions] Deleted ${deleted} existing transactions for full resync`);
    }

    // Get latest transaction date to resume from (if not full resync)
    let fromDate: Date;
    if (!fullResync) {
      const latestDate = await getLatestTransactionDate(accountId);
      if (latestDate) {
        fromDate = new Date(latestDate.getTime() - 24 * 60 * 60 * 1000); // Overlap by 1 day for safety
      } else {
        fromDate = new Date();
        fromDate.setDate(fromDate.getDate() - days);
      }
    } else {
      fromDate = new Date();
      fromDate.setDate(fromDate.getDate() - days);
    }

    const toDate = new Date();
    
    console.log(`[SyncTransactions] Fetching transactions from ${fromDate.toISOString()} to ${toDate.toISOString()}`);

    // Fetch transactions day by day (Capital.com limits results)
    const allTransactions: any[] = [];
    const dayMs = 24 * 60 * 60 * 1000;
    let currentFrom = new Date(fromDate);

    while (currentFrom < toDate) {
      const currentTo = new Date(Math.min(currentFrom.getTime() + dayMs, toDate.getTime()));
      
      const fromStr = currentFrom.toISOString().split('.')[0]; // YYYY-MM-DDTHH:MM:SS
      const toStr = currentTo.toISOString().split('.')[0];
      
      try {
        const dayTransactions = await client.getTransactionHistory(fromStr, toStr);
        if (dayTransactions?.length > 0) {
          allTransactions.push(...dayTransactions);
          console.log(`[SyncTransactions] Day ${currentFrom.toISOString().split('T')[0]}: ${dayTransactions.length} transactions`);
        }
      } catch (error: any) {
        console.error(`[SyncTransactions] Error fetching day ${currentFrom.toISOString().split('T')[0]}:`, error.message);
      }
      
      currentFrom = currentTo;
      await new Promise(r => setTimeout(r, 200)); // Rate limit protection
    }

    console.log(`[SyncTransactions] Total fetched: ${allTransactions.length} transactions`);

    // Map Capital.com transaction types to our enum
    // Note: Capital.com uses TRANSFER for both deposits and withdrawals
    // We determine DEPOSIT vs WITHDRAWAL based on positive/negative size
    const mapTransactionType = (
      type: string, 
      size: number
    ): "TRADE" | "SWAP" | "DEPOSIT" | "WITHDRAWAL" | "TRANSFER" | "DIVIDEND" | "CORRECTION" | "OTHER" => {
      const upperType = type?.toUpperCase();
      
      // Handle TRANSFER - positive = deposit into account, negative = withdrawal from account
      if (upperType === 'TRANSFER') {
        return size >= 0 ? 'DEPOSIT' : 'WITHDRAWAL';
      }
      
      const typeMap: Record<string, "TRADE" | "SWAP" | "DEPOSIT" | "WITHDRAWAL" | "TRANSFER" | "DIVIDEND" | "CORRECTION" | "OTHER"> = {
        'TRADE': 'TRADE',
        'DEAL': 'TRADE',
        'SWAP': 'SWAP',
        'OVERNIGHT_FUNDING': 'SWAP',
        'DEPOSIT': 'DEPOSIT',
        'WITHDRAWAL': 'WITHDRAWAL',
        'DIVIDEND': 'DIVIDEND',
        'REBATE': 'CORRECTION', // Spread rebates
        'CORRECTION': 'CORRECTION',
        'ADJUSTMENT': 'CORRECTION',
      };
      return typeMap[upperType] || 'OTHER';
    };

    // Transform and insert transactions
    const transactionsToInsert = allTransactions.map(tx => {
      const amount = parseFloat(tx.size) || 0;
      return {
        accountId,
        transactionType: mapTransactionType(tx.transactionType, amount),
        amount, // Capital.com uses 'size' for the amount
        runningBalance: tx.balance ? parseFloat(tx.balance) : null,
        currency: acc.currency || 'USD',
        capitalReference: tx.reference || tx.transactionId || `${tx.date}_${tx.transactionType}_${tx.size}`,
        dealId: tx.dealId || null,
        epic: tx.instrumentName || tx.epic || null,
        note: tx.note || null,
        transactionDate: new Date(tx.dateUtc || tx.date),
        rawData: tx,
      };
    });

    // For demo accounts: Add synthetic "Starting Balance" deposit if provided
    // This allows demo accounts to have a proper starting point for balance tracking
    if (startingBalance && startingBalance > 0 && acc.accountType === 'demo') {
      // Find the earliest transaction date, or use fromDate
      const earliestDate = transactionsToInsert.length > 0
        ? new Date(Math.min(...transactionsToInsert.map(t => t.transactionDate.getTime())))
        : fromDate;
      
      // Create synthetic deposit 1 day before earliest transaction
      const syntheticDepositDate = new Date(earliestDate);
      syntheticDepositDate.setDate(syntheticDepositDate.getDate() - 1);
      
      const syntheticDeposit = {
        accountId,
        transactionType: 'DEPOSIT' as const,
        amount: startingBalance,
        runningBalance: startingBalance,
        currency: acc.currency || 'USD',
        capitalReference: `DEMO_STARTING_BALANCE_${accountId}`,
        dealId: null,
        epic: null,
        note: 'Demo account starting balance (manual entry)',
        transactionDate: syntheticDepositDate,
        rawData: { synthetic: true, startingBalance, accountType: 'demo' },
      };
      
      // Add to beginning of array
      transactionsToInsert.unshift(syntheticDeposit);
      console.log(`[SyncTransactions] Added synthetic starting balance of $${startingBalance} for demo account`);
    }

    const { inserted, skipped } = await insertAccountTransactionsBatch(transactionsToInsert);
    
    console.log(`[SyncTransactions] Inserted: ${inserted}, Skipped duplicates: ${skipped}`);

    // Get updated summary
    const summary = await getTransactionSummaryByType(accountId);

    res.json({
      success: true,
      accountId,
      fetched: allTransactions.length,
      inserted,
      skipped,
      summary,
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error syncing transactions:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/manual-transaction
 * Add a manual deposit/withdrawal for demo accounts
 * This allows simulating fund additions/removals in demo accounts
 */
router.post('/accounts/:id/manual-transaction', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const { type, amount, date, note } = req.body;
    
    if (!type || !amount) {
      return res.status(400).json({ error: 'Type and amount are required' });
    }
    
    if (!['DEPOSIT', 'WITHDRAWAL'].includes(type)) {
      return res.status(400).json({ error: 'Type must be DEPOSIT or WITHDRAWAL' });
    }
    
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    // Get the account
    const [acc] = await db.select().from(accounts).where(eq(accounts.id, accountId)).limit(1);
    if (!acc) {
      return res.status(404).json({ error: 'Account not found' });
    }

    // Only allow manual transactions for demo accounts
    if (acc.accountType !== 'demo') {
      return res.status(400).json({ error: 'Manual transactions are only allowed for demo accounts' });
    }

    // Parse the date or use current time
    const transactionDate = date ? new Date(date + 'T12:00:00Z') : new Date();
    
    // Create unique reference for this manual transaction
    const reference = `MANUAL_${type}_${accountId}_${Date.now()}`;
    
    // Insert the transaction
    const transaction = {
      accountId,
      transactionType: type as 'DEPOSIT' | 'WITHDRAWAL',
      amount: type === 'DEPOSIT' ? Math.abs(amount) : -Math.abs(amount),
      runningBalance: null,
      currency: acc.currency || 'USD',
      capitalReference: reference,
      dealId: null,
      epic: null,
      note: note || `Manual ${type.toLowerCase()} (demo account)`,
      transactionDate,
      rawData: { manual: true, type, originalAmount: amount },
    };

    const { inserted } = await insertAccountTransactionsBatch([transaction]);
    
    if (inserted > 0) {
      console.log(`[ManualTransaction] Added ${type} of $${amount} for demo account ${acc.accountName}`);
      res.json({ success: true, inserted: 1 });
    } else {
      res.json({ success: false, error: 'Transaction already exists or failed to insert' });
    }
  } catch (error: any) {
    console.error('[LiveTrading] Error adding manual transaction:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

/**
 * GET /api/live-trading/accounts/:id/balance-history
 * Get computed balance history from transactions
 * Returns daily balances and cumulative P&L for charting
 */
router.get('/accounts/:id/balance-history', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    // Get the account
    const [acc] = await db.select().from(accounts).where(eq(accounts.id, accountId)).limit(1);
    if (!acc) {
      return res.status(404).json({ error: 'Account not found' });
    }

    // Get all transactions sorted by date
    const transactions = await getAccountTransactions(accountId);

    if (transactions.length === 0) {
      return res.json({
        success: true,
        accountId,
        history: [],
        summary: {
          totalDeposits: 0,
          totalWithdrawals: 0,
          totalTradePnl: 0,
          totalSwapFees: 0,
          currentBalance: acc.balance,
        },
      });
    }

    // Compute running balances and categorize
    let runningBalance = 0;
    let cumulativeTradePnl = 0;
    let cumulativeSwapFees = 0;
    let totalDeposits = 0;
    let totalWithdrawals = 0;

    // Group by day for chart data
    const dailyData = new Map<string, {
      date: string;
      tradePnl: number;
      swapFees: number;
      deposits: number;
      withdrawals: number;
      transactions: any[];
    }>();

    const historyPoints: any[] = [];

    for (const tx of transactions) {
      const dateKey = tx.transactionDate.toISOString().split('T')[0];
      
      if (!dailyData.has(dateKey)) {
        dailyData.set(dateKey, {
          date: dateKey,
          tradePnl: 0,
          swapFees: 0,
          deposits: 0,
          withdrawals: 0,
          transactions: [],
        });
      }
      
      const day = dailyData.get(dateKey)!;
      day.transactions.push(tx);

      // Categorize by type
      switch (tx.transactionType) {
        case 'TRADE':
          runningBalance += tx.amount;
          cumulativeTradePnl += tx.amount;
          day.tradePnl += tx.amount;
          break;
        case 'SWAP':
          runningBalance += tx.amount;
          cumulativeSwapFees += tx.amount; // Usually negative
          day.swapFees += tx.amount;
          break;
        case 'DEPOSIT':
          // Deposits are positive amounts (transfers IN)
          runningBalance += tx.amount;
          totalDeposits += tx.amount;
          day.deposits += tx.amount;
          break;
        case 'WITHDRAWAL':
          // Withdrawals come from negative TRANSFER amounts, stored as negative
          // So tx.amount is already negative, we just add it
          runningBalance += tx.amount;
          totalWithdrawals += Math.abs(tx.amount);
          day.withdrawals += Math.abs(tx.amount);
          break;
        case 'CORRECTION':
          // Rebates and corrections - treat as P&L adjustment
          runningBalance += tx.amount;
          cumulativeTradePnl += tx.amount;
          day.tradePnl += tx.amount;
          break;
        default:
          runningBalance += tx.amount;
      }

      // Use Capital.com's running balance if available
      if (tx.runningBalance !== null) {
        runningBalance = tx.runningBalance;
      }

      historyPoints.push({
        date: tx.transactionDate.toISOString(),
        type: tx.transactionType,
        amount: tx.amount,
        epic: tx.epic,
        note: tx.note,
        runningBalance,
        cumulativeTradePnl,
        cumulativeSwapFees,
      });
    }

    // Convert daily data to array for charting
    const dailyHistory = Array.from(dailyData.values()).map(day => {
      // Find the last transaction's running balance for the day
      const lastTx = day.transactions[day.transactions.length - 1];
      return {
        date: day.date,
        tradePnl: day.tradePnl,
        swapFees: day.swapFees,
        deposits: day.deposits,
        withdrawals: day.withdrawals,
        // Get running balance from last transaction of the day
        balance: lastTx?.runningBalance || 0,
      };
    });

    res.json({
      success: true,
      accountId,
      accountName: acc.accountName,
      // Daily aggregated data for chart
      dailyHistory,
      // Full transaction-level history
      transactionHistory: historyPoints,
      // Summary stats
      summary: {
        totalDeposits,
        totalWithdrawals,
        totalTradePnl: cumulativeTradePnl,
        totalSwapFees: cumulativeSwapFees,
        currentBalance: runningBalance,
        actualBalance: acc.balance, // From Capital.com
        transactionCount: transactions.length,
      },
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error computing balance history:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

// ============================================================================
// API RATE LIMIT MONITORING
// ============================================================================

import { rateLimitManager } from '../api/rate_limit_manager';

/**
 * GET /api/live-trading/rate-limit-stats
 * Get comprehensive API rate limit statistics
 * 
 * Use this to:
 * 1. Monitor current rate limit status
 * 2. View per-endpoint statistics  
 * 3. Check if we're in backoff mode
 * 4. Compare our usage to Capital.com's limits
 */
router.get('/rate-limit-stats', async (_req, res) => {
  try {
    const stats = rateLimitManager.getStats();
    
    res.json({
      success: true,
      timestamp: new Date().toISOString(),
      stats,
      report: rateLimitManager.getReport(),
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error getting rate limit stats:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

/**
 * POST /api/live-trading/rate-limit-settings
 * Update rate limit settings dynamically
 * 
 * Body: Partial<RateLimitConfig>
 * e.g. { generalRequestsPerSecond: 8, safetyBuffer: 0.7 }
 */
router.post('/rate-limit-settings', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const updates = req.body;
    
    if (!updates || Object.keys(updates).length === 0) {
      return res.status(400).json({ error: 'No settings provided' });
    }

    // Update config in memory
    rateLimitManager.updateConfig(updates);
    
    // Persist to database
    await rateLimitManager.saveSettingsToDb();
    
    res.json({
      success: true,
      message: 'Rate limit settings updated and saved',
      newConfig: rateLimitManager.getStats().config,
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error updating rate limit settings:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

/**
 * POST /api/live-trading/rate-limit-reset
 * Reset rate limit statistics (use with caution)
 */
router.post('/rate-limit-reset', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    rateLimitManager.reset();
    
    res.json({
      success: true,
      message: 'Rate limit statistics reset',
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error resetting rate limit stats:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

/**
 * POST /api/live-trading/rate-limit-init
 * Initialize default rate limit settings in database
 * (Call once during setup or to reset to defaults)
 */
router.post('/rate-limit-init', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    await rateLimitManager.initializeDefaultSettings();
    
    res.json({
      success: true,
      message: 'Default rate limit settings initialized in database',
      config: rateLimitManager.getStats().config,
    });
  } catch (error: any) {
    console.error('[LiveTrading] Error initializing rate limit settings:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

/**
 * POST /api/live-trading/accounts/:id/validate-backtest-accuracy
 * 
 * Validates how accurate the T-60 API price approximation was compared to
 * the actual 4th 1-minute candle close (what backtest uses).
 * 
 * For each actual trade:
 * 1. Gets stored allDnaResults (what brain decided at T-60)
 * 2. Gets actual 4th 1-minute candle close from DB
 * 3. Re-runs ALL DNA strands with 4th candle price
 * 4. Compares: Did each DNA give same signal? Did same DNA win conflict resolution?
 * 
 * This tells you: "Would the backtest have made the same trade?"
 */
router.post('/accounts/:id/validate-backtest-accuracy', async (req, res) => {
  try {
    const user = await sdk.authenticateRequest(req);
    if (!user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const accountId = parseInt(req.params.id);
    const { startDate, endDate } = req.body;
    
    const db = await getDb();
    if (!db) {
      return res.status(500).json({ error: 'Database not available' });
    }

    const { actualTrades, accounts: accountsTable, candles: candlesTable, savedStrategies } = await import('../../drizzle/schema');
    
    // 1. Get account and strategy
    const account = await db
      .select()
      .from(accountsTable)
      .where(eq(accountsTable.id, accountId))
      .limit(1);
    
    if (account.length === 0) {
      return res.status(404).json({ error: 'Account not found' });
    }
    
    // 2. Get trades with allDnaResults for this account
    // Only include app-generated trades with stored DNA data
    const tradesQuery = db
      .select()
      .from(actualTrades)
      .where(and(
        eq(actualTrades.accountId, accountId),
        or(
          eq(actualTrades.tradeSource, 'app'),
          isNull(actualTrades.tradeSource)
        ),
        isNotNull(actualTrades.allDnaResults),
        isNotNull(actualTrades.winningIndicatorName)
      ))
      .orderBy(desc(actualTrades.createdAt));
    
    const trades = await tradesQuery;
    
    if (trades.length === 0) {
      return res.json({
        success: true,
        message: 'No trades with DNA data found. Trades need allDnaResults stored for validation.',
        totalTrades: 0,
        validations: [],
        summary: {
          totalTrades: 0,
          signalMatches: 0,
          winnerMatches: 0,
          fullMatches: 0,
          mismatches: 0,
          avgPriceVariance: 0,
          maxPriceVariance: 0,
        }
      });
    }
    
    console.log(`[BacktestAccuracy] Validating ${trades.length} trades for account ${accountId}`);
    
    // 3. For each trade, get 4th candle and re-run DNA strands
    const { spawnPython } = await import('../python_spawn');
    const path = await import('path');
    const PYTHON_ENGINE_DIR = path.join(process.cwd(), 'python_engine');
    
    const validations: any[] = [];
    let signalMatches = 0;
    let winnerMatches = 0;
    let fullMatches = 0;
    let totalPriceVariance = 0;
    let maxPriceVariance = 0;
    
    for (const trade of trades) {
      const validation: any = {
        tradeId: trade.id,
        tradeDate: trade.createdAt?.toISOString().split('T')[0] || '',
        windowCloseTime: trade.windowCloseTime,
        epic: trade.epic,
        
        // Brain decision at T-60
        brainWinnerIndicator: trade.winningIndicatorName,
        brainWinnerEpic: trade.epic,
        brainSignal: trade.brainDecision,
        
        // T-60 API price (stored as entryPrice is execution price, not brain calc price)
        // We'll compare against 4th candle close
        t60Price: trade.entryPrice,
        
        // Will be populated below
        backtestCandleClose: null,    // Price from candle backtest would use
        backtestCandleTimestamp: null,
        backtestCandleTimeframe: null, // '1m' or '5m'
        timingMode: null,             // DNA timing mode
        priceVariancePct: null,
        
        dnaSignalMatches: [] as any[],
        rerunWinnerIndicator: null,
        rerunWinnerEpic: null,
        
        allSignalsMatch: false,
        winnerMatch: false,
        fullMatch: false,
      };
      
      try {
        // Parse stored DNA results
        const storedDnaResults = trade.allDnaResults as any;
        if (!storedDnaResults?.dnaResults || storedDnaResults.dnaResults.length === 0) {
          validation.error = 'No DNA results stored';
          validations.push(validation);
          continue;
        }
        
        const dnaResults = storedDnaResults.dnaResults;
        const conflictResolution = storedDnaResults.conflictResolution;
        
        // Get the trade date and window time
        const tradeDate = trade.createdAt ? new Date(trade.createdAt) : null;
        if (!tradeDate) {
          validation.error = 'No trade date';
          validations.push(validation);
          continue;
        }
        
        const tradeDateStr = tradeDate.toISOString().split('T')[0];
        const windowTime = trade.windowCloseTime || '21:00:00';
        const [hours, minutes] = windowTime.split(':').map(Number);
        
        // Get timing mode from trade's timingConfig
        const timingConfig = trade.timingConfig as { mode?: string } | null;
        const timingMode = timingConfig?.mode || 'Fake5min_4thCandle'; // Default
        validation.timingMode = timingMode;
        
        // Determine candle offset based on timing mode:
        // - Fake5min_4thCandle / T60FakeCandle: T-60s (4th 1-min candle)
        // - Fake5min_3rdCandle_API: T-120s (3rd 1-min candle)
        // - SecondLastCandle / T5BeforeClose / EpicClosingTimeBrainCalc: T-5min (second-to-last 5-min)
        // - MarketClose / EpicClosingTime: T-0 (actual market close 5-min candle)
        let candleOffsetMinutes: number;
        let timeframeToUse: '1m' | '5m';
        
        if (['Fake5min_4thCandle', 'T60FakeCandle'].includes(timingMode)) {
          // 4th candle = T-60s = candle that closes at T-1min
          candleOffsetMinutes = 2; // Start of 4th 1-min candle (e.g., 20:58 for 21:00 close)
          timeframeToUse = '1m';
        } else if (timingMode === 'Fake5min_3rdCandle_API') {
          // 3rd candle = T-120s = candle that closes at T-2min
          candleOffsetMinutes = 3; // Start of 3rd 1-min candle (e.g., 20:57 for 21:00 close)
          timeframeToUse = '1m';
        } else if (['SecondLastCandle', 'T5BeforeClose', 'EpicClosingTimeBrainCalc'].includes(timingMode)) {
          // Second-to-last 5-min = T-5min (e.g., 20:55 for 21:00 close)
          candleOffsetMinutes = 5;
          timeframeToUse = '5m';
        } else if (['MarketClose', 'EpicClosingTime'].includes(timingMode)) {
          // Actual market close 5-min candle
          candleOffsetMinutes = 0;
          timeframeToUse = '5m';
        } else {
          // Default to 4th candle behavior
          candleOffsetMinutes = 2;
          timeframeToUse = '1m';
        }
        
        // Calculate target candle timestamp
        const candleStartTime = new Date(tradeDateStr + 'T00:00:00Z');
        candleStartTime.setUTCHours(hours, minutes - candleOffsetMinutes, 0, 0);
        
        const candleEndTime = new Date(candleStartTime);
        if (timeframeToUse === '1m') {
          candleEndTime.setUTCMinutes(candleEndTime.getUTCMinutes() + 1);
        } else {
          candleEndTime.setUTCMinutes(candleEndTime.getUTCMinutes() + 5);
        }
        
        // Look up the candle from DB
        const { gte: drizzleGte, lte: drizzleLte } = await import('drizzle-orm');
        const targetCandles = await db
          .select()
          .from(candlesTable)
          .where(and(
            eq(candlesTable.epic, trade.epic),
            eq(candlesTable.timeframe, timeframeToUse),
            drizzleGte(candlesTable.timestamp, candleStartTime),
            drizzleLte(candlesTable.timestamp, candleEndTime)
          ))
          .orderBy(desc(candlesTable.timestamp))
          .limit(1);
        
        if (targetCandles.length === 0) {
          // Try a wider search
          const widerStart = new Date(candleStartTime);
          widerStart.setUTCMinutes(widerStart.getUTCMinutes() - 5);
          const widerEnd = new Date(candleEndTime);
          widerEnd.setUTCMinutes(widerEnd.getUTCMinutes() + 5);
          
          const widerCandles = await db
            .select()
            .from(candlesTable)
            .where(and(
              eq(candlesTable.epic, trade.epic),
              eq(candlesTable.timeframe, timeframeToUse),
              drizzleGte(candlesTable.timestamp, widerStart),
              drizzleLte(candlesTable.timestamp, widerEnd)
            ))
            .orderBy(desc(candlesTable.timestamp))
            .limit(5);
          
          if (widerCandles.length === 0) {
            validation.error = `No ${timeframeToUse} candles found for ${trade.epic} around ${candleStartTime.toISOString()} (mode: ${timingMode})`;
            validations.push(validation);
            continue;
          }
          
          // Use the closest candle to expected time
          const targetCandle = widerCandles[0];
          const closeBid = parseFloat(targetCandle.closeBid?.toString() || '0');
          const closeAsk = parseFloat(targetCandle.closeAsk?.toString() || '0');
          validation.backtestCandleClose = (closeBid + closeAsk) / 2;
          validation.backtestCandleTimestamp = targetCandle.timestamp?.toISOString();
          validation.backtestCandleTimeframe = timeframeToUse;
          validation.note = `Used nearest ${timeframeToUse} candle (exact not found)`;
        } else {
          const targetCandle = targetCandles[0];
          const closeBid = parseFloat(targetCandle.closeBid?.toString() || '0');
          const closeAsk = parseFloat(targetCandle.closeAsk?.toString() || '0');
          validation.backtestCandleClose = (closeBid + closeAsk) / 2;
          validation.backtestCandleTimestamp = targetCandle.timestamp?.toISOString();
          validation.backtestCandleTimeframe = timeframeToUse;
        }
        
        // Calculate price variance
        if (validation.t60Price && validation.backtestCandleClose) {
          const variance = ((validation.t60Price - validation.backtestCandleClose) / validation.backtestCandleClose) * 100;
          validation.priceVariancePct = parseFloat(variance.toFixed(4));
          totalPriceVariance += Math.abs(variance);
          if (Math.abs(variance) > maxPriceVariance) {
            maxPriceVariance = Math.abs(variance);
          }
        }
        
        // Re-run each DNA strand with 4th candle close
        // We need to call Python to evaluate each indicator
        const rerunResults: any[] = [];
        
        for (let i = 0; i < dnaResults.length; i++) {
          const dna = dnaResults[i];
          
          // Call Python to re-evaluate this indicator with 4th candle close as fake_5min_close
          const signalConfig = {
            epic: dna.epic,
            start_date: tradeDateStr, // Just need recent candles
            end_date: tradeDateStr,
            indicator_name: dna.indicatorName,
            indicator_params: dna.indicatorParams || {},
            fake_5min_close: validation.backtestCandleClose, // Use backtest candle as the fake close
            fake_5min_timestamp: validation.backtestCandleTimestamp,
            crash_protection_enabled: false,
          };
          
          try {
            const GET_SIGNAL_SCRIPT = path.join(PYTHON_ENGINE_DIR, 'get_current_signal.py');
            const configJson = JSON.stringify(signalConfig);
            
            const signalResult: any = await new Promise((resolve, reject) => {
              const pythonProcess = spawnPython(GET_SIGNAL_SCRIPT, {
                args: [configJson],
                cwd: PYTHON_ENGINE_DIR,
                env: { ...process.env, PYTHONUNBUFFERED: '1' },
              });

              let stdout = '';
              let stderr = '';

              pythonProcess.stdout?.on('data', (data: Buffer) => {
                stdout += data.toString();
              });

              pythonProcess.stderr?.on('data', (data: Buffer) => {
                stderr += data.toString();
              });

              pythonProcess.on('close', (code: number) => {
                if (code !== 0) {
                  reject(new Error(`Signal calculation failed: ${stderr}`));
                  return;
                }

                const resultMatch = stdout.match(/RESULT:(.+)/);
                if (!resultMatch) {
                  reject(new Error('No result found in signal output'));
                  return;
                }

                try {
                  resolve(JSON.parse(resultMatch[1]));
                } catch (e) {
                  reject(new Error(`Failed to parse signal result: ${e}`));
                }
              });

              pythonProcess.on('error', (err: Error) => reject(err));
            });
            
            const rerunSignal = signalResult.signal === 1 ? 'BUY' : 'HOLD';
            const originalSignal = dna.signal;
            const signalMatches = rerunSignal === originalSignal;
            
            rerunResults.push({
              indicatorName: dna.indicatorName,
              epic: dna.epic,
              originalSignal,
              rerunSignal,
              signalMatch: signalMatches,
              originalIndicatorValue: dna.indicatorValue,
              rerunIndicatorValue: signalResult.indicator_value,
              sharpeRatio: dna.sharpeRatio,
            });
            
            validation.dnaSignalMatches.push({
              indicatorName: dna.indicatorName,
              match: signalMatches,
              original: originalSignal,
              rerun: rerunSignal,
            });
            
          } catch (signalError: any) {
            rerunResults.push({
              indicatorName: dna.indicatorName,
              epic: dna.epic,
              error: signalError.message,
              signalMatch: false,
            });
            
            validation.dnaSignalMatches.push({
              indicatorName: dna.indicatorName,
              match: false,
              error: signalError.message,
            });
          }
        }
        
        // Check if all signals match
        validation.allSignalsMatch = validation.dnaSignalMatches.every((d: any) => d.match);
        if (validation.allSignalsMatch) {
          signalMatches++;
        }
        
        // Run conflict resolution on rerun results to see who wins
        const rerunBuys = rerunResults.filter(r => r.rerunSignal === 'BUY' && !r.error);
        
        if (rerunBuys.length === 0) {
          // Rerun says HOLD (no BUY signals)
          validation.rerunWinnerIndicator = null;
          validation.rerunWinnerEpic = null;
          validation.winnerMatch = (trade.brainDecision === 'HOLD' || !trade.winningIndicatorName);
        } else if (rerunBuys.length === 1) {
          // Only one BUY, it wins
          validation.rerunWinnerIndicator = rerunBuys[0].indicatorName;
          validation.rerunWinnerEpic = rerunBuys[0].epic;
          validation.winnerMatch = validation.rerunWinnerIndicator === trade.winningIndicatorName;
        } else {
          // Multiple BUYs - run conflict resolution
          const metric = conflictResolution?.metric || 'sharpeRatio';
          let winner = rerunBuys[0];
          
          for (const buy of rerunBuys) {
            if (metric === 'sharpeRatio' || metric === 'sharpe') {
              if ((buy.sharpeRatio || 0) > (winner.sharpeRatio || 0)) {
                winner = buy;
              }
            } else if (metric === 'totalReturn' || metric === 'return') {
              const buyReturn = dnaResults.find((d: any) => d.indicatorName === buy.indicatorName)?.totalReturn || 0;
              const winnerReturn = dnaResults.find((d: any) => d.indicatorName === winner.indicatorName)?.totalReturn || 0;
              if (buyReturn > winnerReturn) {
                winner = buy;
              }
            } else if (metric === 'winRate') {
              const buyWinRate = dnaResults.find((d: any) => d.indicatorName === buy.indicatorName)?.winRate || 0;
              const winnerWinRate = dnaResults.find((d: any) => d.indicatorName === winner.indicatorName)?.winRate || 0;
              if (buyWinRate > winnerWinRate) {
                winner = buy;
              }
            }
          }
          
          validation.rerunWinnerIndicator = winner.indicatorName;
          validation.rerunWinnerEpic = winner.epic;
          validation.winnerMatch = validation.rerunWinnerIndicator === trade.winningIndicatorName;
        }
        
        if (validation.winnerMatch) {
          winnerMatches++;
        }
        
        // Full match = all signals match AND same winner
        validation.fullMatch = validation.allSignalsMatch && validation.winnerMatch;
        if (validation.fullMatch) {
          fullMatches++;
        }
        
      } catch (tradeError: any) {
        validation.error = tradeError.message;
      }
      
      validations.push(validation);
    }
    
    // Calculate summary
    const totalTrades = trades.length;
    const avgPriceVariance = totalTrades > 0 ? totalPriceVariance / totalTrades : 0;
    
    // Find mismatches for detailed review
    const mismatches = validations.filter(v => !v.fullMatch && !v.error);
    
    console.log(`[BacktestAccuracy] Results: ${fullMatches}/${totalTrades} full matches (${(fullMatches/totalTrades*100).toFixed(1)}%)`);
    console.log(`[BacktestAccuracy] Signal matches: ${signalMatches}, Winner matches: ${winnerMatches}`);
    console.log(`[BacktestAccuracy] Avg price variance: ${avgPriceVariance.toFixed(4)}%, Max: ${maxPriceVariance.toFixed(4)}%`);
    
    res.json({
      success: true,
      totalTrades,
      validations,
      summary: {
        totalTrades,
        signalMatches,
        winnerMatches,
        fullMatches,
        mismatches: mismatches.length,
        signalMatchRate: totalTrades > 0 ? parseFloat((signalMatches / totalTrades * 100).toFixed(1)) : 0,
        winnerMatchRate: totalTrades > 0 ? parseFloat((winnerMatches / totalTrades * 100).toFixed(1)) : 0,
        fullMatchRate: totalTrades > 0 ? parseFloat((fullMatches / totalTrades * 100).toFixed(1)) : 0,
        avgPriceVariance: parseFloat(avgPriceVariance.toFixed(4)),
        maxPriceVariance: parseFloat(maxPriceVariance.toFixed(4)),
      },
      mismatchDetails: mismatches.map(m => ({
        tradeId: m.tradeId,
        tradeDate: m.tradeDate,
        windowCloseTime: m.windowCloseTime,
        epic: m.epic,
        priceVariancePct: m.priceVariancePct,
        brainWinner: m.brainWinnerIndicator,
        rerunWinner: m.rerunWinnerIndicator,
        allSignalsMatch: m.allSignalsMatch,
        winnerMatch: m.winnerMatch,
        dnaSignalMatches: m.dnaSignalMatches,
      })),
    });
    
  } catch (error: any) {
    console.error('[BacktestAccuracy] Error:', error);
    res.status(500).json({ error: error.message });
  }
});

export default router;

