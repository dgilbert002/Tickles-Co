import { COOKIE_NAME } from "@shared/const";
import { getSessionCookieOptions } from "./_core/cookies";
import { systemRouter } from "./_core/systemRouter";
import { resourceRouter } from "./resource_router";
import { publicProcedure, router } from "./_core/trpc";
import { z } from "zod";
import * as db from "./db";
import { runBacktest, stopBacktest } from './backtest_bridge_v2';
import { resourceManager } from './resource_manager';
import { orchestrationTimer } from './orchestration/timer';
import { spawnPython } from './python_spawn';
import { logsRouter } from './routes/logs';
import { botsRouter } from './routes/bots';
import { strategiesRouter } from './routes/strategies';
import { phasesRouter } from './routes/phases';
import { previewWindowBrain, previewAllWindows } from './services/brain_preview_service';
import { epics, accounts } from '../drizzle/schema';
import { getDb } from './db';
import { eq, sql, ne, isNotNull } from 'drizzle-orm';
import { refreshWebSocketSubscriptions } from './services/candle_data_service';
// DB_PATH no longer needed - using MySQL

// Simple accounts router for Strategy Library
const accountsRouter = router({
  getAll: publicProcedure.query(async () => {
    const dbInstance = await getDb();
    if (!dbInstance) return [];
    
    const allAccounts = await dbInstance
      .select({
        id: accounts.id,
        accountName: accounts.accountName,
        accountType: accounts.accountType,
        assignedStrategyId: accounts.assignedStrategyId,
        isActive: accounts.isActive,
        isArchived: accounts.isArchived,
      })
      .from(accounts);
    
    return allAccounts;
  }),
});

export const appRouter = router({
  system: systemRouter,
  resource: resourceRouter,
  logs: logsRouter,
  botManagement: botsRouter,
  strategies: strategiesRouter,
  phases: phasesRouter,
  accounts: accountsRouter,

  // Indicator Parameters
  indicatorParams: router({
    // Get all parameters grouped by indicator
    getAll: publicProcedure.query(async () => {
      const params = await db.getAllIndicatorParams();
      
      // Group by indicator name
      const grouped: Record<string, typeof params> = {};
      for (const param of params) {
        if (!grouped[param.indicatorName]) {
          grouped[param.indicatorName] = [];
        }
        grouped[param.indicatorName].push(param);
      }
      
      return { params, grouped };
    }),
    
    // Get all indicator names
    getIndicatorNames: publicProcedure.query(async () => {
      return await db.getIndicatorNames();
    }),
    
    // Get parameters for a specific indicator
    getByIndicator: publicProcedure
      .input(z.object({ indicatorName: z.string() }))
      .query(async ({ input }) => {
        return await db.getIndicatorParams(input.indicatorName);
      }),
    
    // Get universal parameters (leverage, stop_loss)
    getUniversal: publicProcedure.query(async () => {
      return await db.getUniversalParams();
    }),
    
    // Update a parameter
    update: publicProcedure
      .input(z.object({
        id: z.number(),
        minValue: z.number().optional(),
        maxValue: z.number().optional(),
        stepValue: z.number().optional(),
        defaultValue: z.number().optional(),
        discreteValues: z.string().nullable().optional(), // JSON array for list type e.g., "[1, 2, 3, 4]"
        isEnabled: z.boolean().optional(),
        description: z.string().optional(),
        displayName: z.string().optional(),
        paramType: z.enum(['int', 'float', 'list']).optional(),
      }))
      .mutation(async ({ input }) => {
        const { id, ...updates } = input;
        await db.updateIndicatorParam(id, updates);
        return { success: true };
      }),
    
    // Create a new parameter
    create: publicProcedure
      .input(z.object({
        indicatorName: z.string(),
        paramName: z.string(),
        displayName: z.string().optional(),
        minValue: z.number(),
        maxValue: z.number(),
        stepValue: z.number(),
        defaultValue: z.number(),
        discreteValues: z.string().nullable().optional(), // JSON array for list type
        description: z.string().optional(),
        paramType: z.enum(['int', 'float', 'list']).default('float'),
        sortOrder: z.number().optional(),
      }))
      .mutation(async ({ input }) => {
        const id = await db.createIndicatorParam(input);
        return { success: true, id };
      }),
    
    // Delete a parameter
    delete: publicProcedure
      .input(z.object({ id: z.number() }))
      .mutation(async ({ input }) => {
        await db.deleteIndicatorParam(input.id);
        return { success: true };
      }),
    
    // Reset an indicator's params (delete all, will need to re-seed)
    resetIndicator: publicProcedure
      .input(z.object({ indicatorName: z.string() }))
      .mutation(async ({ input }) => {
        await db.deleteIndicatorParamsByIndicator(input.indicatorName);
        return { success: true };
      }),
    
    // Calculate total combinations for an indicator (including universal params)
    calculateCombinations: publicProcedure
      .input(z.object({ indicatorName: z.string() }))
      .query(async ({ input }) => {
        const indicatorParamsData = await db.getIndicatorParams(input.indicatorName);
        const universalParams = await db.getUniversalParams();
        
        const allParams = [...universalParams, ...indicatorParamsData].filter(p => p.isEnabled);
        
        let totalCombinations = 1;
        const paramDetails: { name: string; values: number }[] = [];
        
        for (const param of allParams) {
          let numValues: number;
          
          if (param.paramType === 'list' && param.discreteValues) {
            // Parse the JSON array to count discrete values
            try {
              const values = JSON.parse(param.discreteValues);
              numValues = Array.isArray(values) ? values.length : 1;
            } catch {
              numValues = 1;
            }
          } else {
            // Calculate number of values for stepped range
            const range = param.maxValue - param.minValue;
            numValues = param.stepValue > 0 ? Math.floor(range / param.stepValue) + 1 : 1;
          }
          
          totalCombinations *= numValues;
          paramDetails.push({ name: param.paramName, values: numValues });
        }
        
        return { 
          totalCombinations, 
          paramDetails,
          enabledParams: allParams.length 
        };
      }),
  }),

  // Brain Preview
  brainPreview: router({
    previewWindow: publicProcedure
      .input(z.object({ windowCloseTime: z.string() }))
      .query(async ({ input }) => {
        return await previewWindowBrain(input.windowCloseTime);
      }),
    
    previewAllWindows: publicProcedure
      .query(async () => {
        return await previewAllWindows();
      }),

    // Account-specific window brain preview
    previewAccountWindow: publicProcedure
      .input(z.object({ 
        accountId: z.number(),
        windowId: z.string(),
      }))
      .query(async ({ input }) => {
        const { generateAccountWindowBrainPreview } = await import('./services/account_brain_preview_service');
        return await generateAccountWindowBrainPreview(input.accountId, input.windowId);
      }),

    // System-wide brain preview
    previewSystem: publicProcedure
      .query(async () => {
        const { generateSystemBrainPreview } = await import('./services/account_brain_preview_service');
        return await generateSystemBrainPreview();
      }),

    // DNA strand-based brain preview (new architecture)
    previewWithDNA: publicProcedure
      .input(z.object({
        strategyId: z.number(),
        accountId: z.number(),
        environment: z.enum(['demo', 'live']).default('demo'),
      }))
      .query(async ({ input }) => {
        const { brainPreviewWithDNA } = await import('./services/brain_preview_dna');
        return await brainPreviewWithDNA(input.strategyId, input.accountId, input.environment);
      }),

    // Global brain preview - comprehensive view of all accounts and windows
    globalPreview: publicProcedure
      .query(async () => {
        const { runGlobalBrainPreview } = await import('./services/global_brain_preview_service');
        return await runGlobalBrainPreview();
      }),
  }),

  // Market Hours
  marketHours: router({
    getAll: publicProcedure.query(async () => {
      const database = await getDb();
      if (!database) return [];
      const allEpics = await database.select().from(epics);
      return allEpics.map(epic => ({
        symbol: epic.symbol,
        marketStatus: epic.marketStatus,
        openingHours: epic.openingHours,
        nextOpen: epic.nextOpen,
        nextClose: epic.nextClose,
        lastUpdated: epic.marketHoursLastFetched,
      }));
    }),

    getBySymbol: publicProcedure
      .input(z.object({ symbol: z.string() }))
      .query(async ({ input }) => {
        const database = await getDb();
        if (!database) return null;
        const result = await database.select().from(epics).where(eq(epics.symbol, input.symbol)).limit(1);
        if (result.length === 0) return null;
        const epic = result[0];
        return {
          symbol: epic.symbol,
          marketStatus: epic.marketStatus,
          openingHours: epic.openingHours,
          nextOpen: epic.nextOpen,
          nextClose: epic.nextClose,
          lastUpdated: epic.marketHoursLastFetched,
        };
      }),
  }),

  // Orchestrator (Global Timer)
  orchestrator: router({
    getStatus: publicProcedure.query(async () => {
      const status = orchestrationTimer.getStatus();
      return {
        isRunning: status.isRunning,
        settings: status.settings,
      };
    }),

    startAllBots: publicProcedure.mutation(async () => {
      try {
        // 1. Start the global timer
        await orchestrationTimer.start();
        
        // 2. Activate all accounts that have assigned strategies
        const dbInstance = await db.getDb();
        if (dbInstance) {
          const { accounts } = await import('../drizzle/schema');
          const { isNotNull, eq } = await import('drizzle-orm');
          
          // Get all accounts with assigned strategies
          const accountsWithStrategies = await dbInstance
            .select({ id: accounts.id, accountName: accounts.accountName, botStatus: accounts.botStatus })
            .from(accounts)
            .where(isNotNull(accounts.assignedStrategyId));
          
          // Activate each account (set to 'running' regardless of current state)
          let activated = 0;
          const errors: string[] = [];
          
          for (const account of accountsWithStrategies) {
            try {
              await dbInstance.update(accounts)
                .set({
                  botStatus: 'running',
                  isActive: true,
                  errorMessage: null,
                  startedAt: new Date(),
                  pausedAt: null,
                  lastHeartbeat: new Date(),
                })
                .where(eq(accounts.id, account.id));
              activated++;
            } catch (err: any) {
              errors.push(`${account.accountName}: ${err.message}`);
            }
          }
          
          if (errors.length > 0) {
            // Refresh WebSocket subscriptions after activating bots
            await refreshWebSocketSubscriptions();
            return { 
              success: true, 
              message: `Timer started. Activated ${activated}/${accountsWithStrategies.length} bots.`,
              errors 
            };
          }
          
          // Refresh WebSocket subscriptions after activating all bots
          await refreshWebSocketSubscriptions();
          return { 
            success: true, 
            message: `All bots started (${activated} accounts activated)` 
          };
        }
        
        return { success: true, message: 'Timer started (no accounts to activate)' };
      } catch (error: any) {
        return { success: false, message: error.message };
      }
    }),

    stopAllBots: publicProcedure.mutation(async () => {
      try {
        // 1. Stop the global timer
        await orchestrationTimer.stop();
        
        // 2. Stop all running/paused accounts
        const dbInstance = await db.getDb();
        if (dbInstance) {
          const { accounts } = await import('../drizzle/schema');
          const { ne, eq } = await import('drizzle-orm');
          
          // Get all non-stopped accounts
          const activeAccounts = await dbInstance
            .select({ id: accounts.id })
            .from(accounts)
            .where(ne(accounts.botStatus, 'stopped'));
          
          // Stop each account
          for (const account of activeAccounts) {
            await dbInstance.update(accounts)
              .set({
                botStatus: 'stopped',
                isActive: false,
                stoppedAt: new Date(),
                pausedAt: null,
              })
              .where(eq(accounts.id, account.id));
          }
          
          // Refresh WebSocket subscriptions after stopping all bots
          await refreshWebSocketSubscriptions();
          return { 
            success: true, 
            message: `All bots stopped (${activeAccounts.length} accounts deactivated)` 
          };
        }
        
        return { success: true, message: 'Timer stopped' };
      } catch (error: any) {
        return { success: false, message: error.message };
      }
    }),
  }),

  auth: router({
    me: publicProcedure.query(opts => opts.ctx.user),
    testError: publicProcedure.query(() => {
      throw new Error('Test error for logging');
    }),
    logout: publicProcedure.mutation(({ ctx }) => {
      const cookieOptions = getSessionCookieOptions(ctx.req);
      ctx.res.clearCookie(COOKIE_NAME, { ...cookieOptions, maxAge: -1 });
      return {
        success: true,
      } as const;
    }),
  }),

  // Bot Management
  bots: router({
    list: publicProcedure.query(async ({ ctx }) => {
      // Public access - return all bots
      return await db.getUserBots(ctx.user?.id || 1);
    }),

    get: publicProcedure
      .input(z.object({ id: z.number() }))
      .query(async ({ input }) => {
        return await db.getBot(input.id);
      }),

    create: publicProcedure
      .input(z.object({
        name: z.string(),
        description: z.string().optional(),
        indicators: z.array(z.string()),
        direction: z.enum(['long', 'short', 'both']),
        defaultLeverage: z.number(),
        defaultStopLoss: z.number(),
        indicatorParams: z.record(z.string(), z.any()),
        entrySeconds: z.number().default(15),
        exitSeconds: z.number().default(30),
        crashProtectionEnabled: z.boolean().default(false),
        crashProtectionParams: z.object({
          timeframeDays: z.number(),
          ddThreshold: z.number(),
          rsiBottom: z.number(),
          volSpike: z.number(),
          recoveryThreshold: z.number(),
        }).optional(),
      }))
      .mutation(async ({ ctx, input }) => {
        const botId = await db.createBot({
          userId: ctx.user?.id || 1,
          name: input.name,
          description: input.description || null,
          indicators: input.indicators,
          direction: input.direction,
          defaultLeverage: input.defaultLeverage,
          defaultStopLoss: input.defaultStopLoss,
          indicatorParams: input.indicatorParams,
          entrySeconds: input.entrySeconds,
          exitSeconds: input.exitSeconds,
          crashProtectionEnabled: input.crashProtectionEnabled,
          crashProtectionParams: input.crashProtectionParams || null,
        });
        return { id: botId };
      }),

    update: publicProcedure
      .input(z.object({
        id: z.number(),
        updates: z.object({
          name: z.string().optional(),
          description: z.string().optional(),
          indicators: z.array(z.string()).optional(),
          direction: z.enum(['long', 'short', 'both']).optional(),
          defaultLeverage: z.number().optional(),
          defaultStopLoss: z.number().optional(),
          indicatorParams: z.record(z.string(), z.any()).optional(),
          entrySeconds: z.number().optional(),
          exitSeconds: z.number().optional(),
          crashProtectionEnabled: z.boolean().optional(),
          crashProtectionParams: z.object({
            timeframeDays: z.number(),
            ddThreshold: z.number(),
            rsiBottom: z.number(),
            volSpike: z.number(),
            recoveryThreshold: z.number(),
          }).optional(),
        }),
      }))
      .mutation(async ({ input }) => {
        await db.updateBot(input.id, input.updates);
        return { success: true };
      }),

    delete: publicProcedure
      .input(z.object({ id: z.number() }))
      .mutation(async ({ input }) => {
        await db.deleteBot(input.id);
        return { success: true };
      }),
  }),

  // Account Management
  accounts: router({
    list: publicProcedure.query(async ({ ctx }) => {
      // Return accounts with their assigned strategy details
      // This is used by GlobalBrainPreview and other pages that need strategy info
      const { getAccountsWithStrategies } = await import('./live_trading/db_functions');
      return await getAccountsWithStrategies(ctx.user?.id || 1);
    }),

    get: publicProcedure
      .input(z.object({ id: z.number() }))
      .query(async ({ input }) => {
        return await db.getAccount(input.id);
      }),

    create: publicProcedure
      .input(z.object({
        accountId: z.string(),
        accountType: z.enum(['demo', 'live']),
        accountName: z.string().optional(),
        apiKey: z.string(),
        balance: z.number().default(0),
        currency: z.string().default('USD'),
      }))
      .mutation(async ({ ctx, input }) => {
        const accountId = await db.createAccount({
          userId: ctx.user?.id || 1,
          accountId: input.accountId,
          accountType: input.accountType,
          accountName: input.accountName || '',
          balance: input.balance,
          currency: input.currency,
        });
        return { id: accountId };
      }),

    assignBot: publicProcedure
      .input(z.object({
        accountId: z.number(),
        botId: z.number().nullable(),
      }))
      .mutation(async ({ input }) => {
        await db.assignBotToAccount(input.accountId, input.botId);
        // Refresh WebSocket subscriptions when strategy is assigned/removed
        await refreshWebSocketSubscriptions();
        return { success: true };
      }),

    updateConfig: publicProcedure
      .input(z.object({
        accountId: z.number(),
        epic: z.string().optional(),
        leverage: z.number().optional(),
        stopLoss: z.number().optional(),
        investmentPct: z.number().optional(),
        monthlyTopup: z.number().optional(),
      }))
      .mutation(async ({ input }) => {
        const { accountId, ...updates } = input;
        await db.updateAccount(accountId, updates);
        return { success: true };
      }),

    setActive: publicProcedure
      .input(z.object({
        accountId: z.number(),
        isActive: z.boolean(),
      }))
      .mutation(async ({ input }) => {
        await db.updateAccount(input.accountId, { isActive: input.isActive });
        // Refresh WebSocket subscriptions when account active state changes
        await refreshWebSocketSubscriptions();
        return { success: true };
      }),

    // Account control mutations
    activate: publicProcedure
      .input(z.object({ accountId: z.number() }))
      .mutation(async ({ input }) => {
        // Check if account has a strategy assigned - can't activate without one
        const account = await db.getAccount(input.accountId);
        if (!account) {
          throw new Error('Account not found');
        }
        if (!account.assignedStrategyId) {
          throw new Error('Cannot activate account without a strategy assigned. Please assign a strategy first.');
        }
        
        await db.updateAccount(input.accountId, { 
          botStatus: 'running',
          isActive: true, // Also set isActive flag
          errorMessage: null, // Clear error message when activating
          startedAt: new Date(),
          pausedAt: null,
          lastHeartbeat: new Date(),
        });
        // Refresh WebSocket subscriptions when account is activated
        await refreshWebSocketSubscriptions();
        return { success: true };
      }),

    pause: publicProcedure
      .input(z.object({ accountId: z.number() }))
      .mutation(async ({ input }) => {
        await db.updateAccount(input.accountId, { 
          botStatus: 'paused',
          pausedAt: new Date(),
        });
        return { success: true };
      }),

    stop: publicProcedure
      .input(z.object({ accountId: z.number() }))
      .mutation(async ({ input }) => {
        await db.updateAccount(input.accountId, { 
          botStatus: 'stopped',
          isActive: false, // Also set isActive flag
          stoppedAt: new Date(),
          pausedAt: null,
        });
        // Refresh WebSocket subscriptions when account is stopped
        await refreshWebSocketSubscriptions();
        return { success: true };
      }),

    // Get window countdown information for an account
    getWindowCountdown: publicProcedure
      .input(z.object({ accountId: z.number() }))
      .query(async ({ input }) => {
        const account = await db.getAccount(input.accountId);
        if (!account || !account.windowConfig) {
          return null;
        }

        const { calculateAccountWindowCountdowns } = await import('./services/window_countdown_service');
        
        // Get epics from account (need to fetch from strategy)
        const epics: string[] = [];
        if (account.assignedStrategyId) {
          const database = await getDb();
          if (database) {
            const { savedStrategies } = await import('../drizzle/schema');
            const { eq } = await import('drizzle-orm');
            const strategy = await database.select()
              .from(savedStrategies)
              .where(eq(savedStrategies.id, account.assignedStrategyId!))
              .limit(1);
            
            if (strategy[0] && strategy[0].dnaStrands) {
              const dnaStrands = strategy[0].dnaStrands as any[];
              const epicSet = new Set<string>();
              for (const strand of dnaStrands) {
                if (strand.epic) {
                  epicSet.add(strand.epic);
                }
              }
              epics.push(...Array.from(epicSet));
            }
          }
        }

        return await calculateAccountWindowCountdowns(
          input.accountId,
          account.windowConfig,
          epics
        );
      }),
  }),

  // Backtesting
  backtest: router({
    getIndicators: publicProcedure.query(async () => {
      // Cache indicator data to avoid repeated Python calls
      const cacheKey = 'indicator_registry_cache';
      const cacheTimeout = 5 * 60 * 1000; // 5 minutes
      
      if ((global as any)[cacheKey] && (global as any)[cacheKey].timestamp > Date.now() - cacheTimeout) {
        return (global as any)[cacheKey].data;
      }
      
      // Call Python to get organized indicator list
      return new Promise((resolve, reject) => {
        const python = spawnPython('python_engine/get_indicators.py', {});
        
        let output = '';
        python.stdout?.on('data', (data) => {
          output += data.toString();
        });
        
        let errorOutput = '';
        python.stderr?.on('data', (data) => {
          errorOutput += data.toString();
          console.error('[Indicators] Error:', data.toString());
        });
        
        python.on('close', (code) => {
          if (code === 0) {
            try {
              const result = JSON.parse(output);
              // Cache the result
              (global as any)[cacheKey] = {
                data: result,
                timestamp: Date.now()
              };
              resolve(result);
            } catch (e) {
              reject(new Error('Failed to parse indicator data'));
            }
          } else {
            reject(new Error(`Python exited with code ${code}`));
          }
        });
      });
    }),

    run: publicProcedure
      .input(z.object({
        epic: z.string(),
        epics: z.array(z.string()).optional(), // Multi-epic support
        startDate: z.string(),
        endDate: z.string(),
        initialBalance: z.number(),
        monthlyTopup: z.number(),
        investmentPct: z.number(),
        indicators: z.array(z.string()),
        numSamples: z.number(),
        direction: z.enum(['long', 'short', 'both']),
        batchMode: z.boolean().optional(),
        optimizationStrategy: z.string().optional(),
        calculationMode: z.enum(['standard', 'numba']).optional().default('standard'),
        stopConditions: z.object({
          maxDrawdown: z.number().optional(),
          minBalanceThreshold: z.number().optional(), // Stop if balance falls below this
          // Legacy fields (optional for backwards compatibility)
          minWinRate: z.number().optional(),
          minSharpe: z.number().optional(),
          minProfitability: z.number().optional(),
        }).optional(),
        timingConfig: z.object({
          mode: z.enum([
            // Signal-based trading (ignores market close)
            'SignalBased',          // Trade on indicator signals
            
            // NEW: Fake 5-min candle modes (recommended for realistic backtesting)
            'Fake5min_3rdCandle_API', // 3x 1-min + API call at T-120s (most realistic for live)
            'Fake5min_4thCandle',     // 4x 1-min candles at T-60s (best for backtesting)
            'SecondLastCandle',       // Second-to-last 5-min candle of day
            
            // Auto-detect market close from data
            'MarketClose',          // Last candle of day (auto-detect close time)
            'T5BeforeClose',        // T-5min before detected close
            'T60FakeCandle',        // T-60s with fake 5-min from 1-min data (legacy)
            
            // Fixed time modes
            'USMarketClose',        // Fixed 16:00 ET
            'ExtendedHoursClose',   // Fixed 20:00 ET
            
            // Random modes
            'random_morning', 'random_afternoon',
            
            // Custom
            'Custom',
            
            // Legacy modes (for backwards compatibility)
            'EpicClosingTimeBrainCalc', 'EpicClosingTime', 
            'USMarketClosingTime', 'ManusTime', 'OriginalBotTime', 
            'Random', 'SpecificHour'
          ]),
          market_close: z.string().optional(),
          calc_offset_seconds: z.number().optional(),
          close_offset_seconds: z.number().optional(),
          open_offset_seconds: z.number().optional(),
          entry_range_start: z.string().optional(),
          entry_range_end: z.string().optional(),
          exit_range_start: z.string().optional(),
          exit_range_end: z.string().optional(),
          entry_time_specific: z.string().optional(),
          exit_time_specific: z.string().optional(),
        }).optional(),
        timeframeConfig: z.object({
          mode: z.enum(['default', 'random', 'multiple']),
          selectedTimeframes: z.array(z.string()),
        }).optional(),
        signalBasedConfig: z.object({
          enabled: z.boolean(),
          closeOnNextSignal: z.boolean(),
          maxHoldPeriod: z.enum(['same_day', 'next_day', 'custom']),
          customHoldDays: z.number().optional(),
        }).optional(),
        // VERSION 3: Enhanced signal-based config (multi-indicator, trust matrix)
        enhancedSignalConfig: z.object({
          enabled: z.boolean(),
          entryIndicators: z.array(z.string()),
          exitIndicators: z.array(z.string()),
          entryParams: z.record(z.string(), z.record(z.string(), z.any())),
          exitParams: z.record(z.string(), z.record(z.string(), z.any())),
          allowShort: z.boolean(),
          reverseOnSignal: z.boolean(),
          stopLossMode: z.enum(['none', 'fixed', 'auto', 'random']),
          stopLossPct: z.number(),
          marginCloseoutLevel: z.number().min(30).max(80).optional().default(55),
          positionSizePct: z.number().min(1).max(100),
          minBalanceThreshold: z.number().min(0),
          useTrustMatrix: z.boolean(),
          defaultLeverage: z.number().min(1).max(20),
          // Parameter variation controls
          entrySamplesPerIndicator: z.number().min(1).max(100).optional().default(5),
          exitSamplesPerIndicator: z.number().min(1).max(100).optional().default(5),
        }).optional(),
        crashProtectionMode: z.enum(['without', 'with', 'both']).optional().default('without'),
        // HMH (Hold Means Hold) - trail stop loss on HOLD instead of closing
        hmhConfig: z.object({
          enabled: z.boolean(),
          offsets: z.array(z.number()),  // e.g., [0, -1, -2] for original, -1%, -2%
        }).optional(),
        freeMemory: z.boolean().optional().default(false),
        testOrder: z.enum(['sequential', 'random']).optional().default('sequential'),
        parallelCores: z.number().min(1).optional().default(1),
        rerunDuplicates: z.boolean().optional().default(false),
        dataSource: z.literal('capital').optional().default('capital'),  // Only Capital.com data supported
      }))
      .mutation(async ({ ctx, input }) => {
        // Check concurrent backtest limit (max 5)
        const userId = ctx.user?.id || 1;
        const activeRuns = await db.getActiveBacktestRuns(userId);
        if (activeRuns.length >= 5) {
          throw new Error('Maximum of 5 concurrent backtests allowed. Please wait for a backtest to complete or pause/stop one before starting a new one.');
        }

        // Multi-epic support: if epics array provided, use it; otherwise use single epic
        console.log(`[Backtest] === EPIC DEBUG ===`);
        console.log(`[Backtest] input.epic (single): "${input.epic}"`);
        console.log(`[Backtest] input.epics (array): ${JSON.stringify(input.epics)}`);
        console.log(`[Backtest] input.epics?.length: ${input.epics?.length}`);
        
        const epicsToRun = input.epics && input.epics.length > 0 ? input.epics : [input.epic];
        const runIds: number[] = [];
        
        console.log(`[Backtest] epicsToRun (resolved): ${JSON.stringify(epicsToRun)}`);
        console.log(`[Backtest] Starting backtest for ${epicsToRun.length} epic(s): ${epicsToRun.join(', ')}`);
        
        // Create a backtest run for each epic
        for (const epicToRun of epicsToRun) {
          // Create backtest run record
          const runId = await db.createBacktestRun({
            userId,
            epic: epicToRun,
            startDate: input.startDate,
            endDate: input.endDate,
            initialBalance: input.initialBalance,
            monthlyTopup: input.monthlyTopup,
            investmentPct: input.investmentPct,
            indicators: input.indicators,
            numSamples: input.numSamples,
            direction: input.direction,
            timingConfig: input.timingConfig || null,
            timeframeConfig: input.timeframeConfig || null,
            stopConditions: input.stopConditions || null,
            crashProtectionMode: input.crashProtectionMode || 'without',
            // HMH (Hold Means Hold) config - stored for resume capability
            hmhConfig: input.hmhConfig || null,
            optimizationStrategy: input.optimizationStrategy || 'random',
            freeMemory: input.freeMemory || false,
            testOrder: input.testOrder || 'sequential',
            parallelCores: input.parallelCores || 1,
            status: 'pending',
            totalTests: (() => {
              const timeframeMultiplier = input.timeframeConfig?.mode === 'multiple' && input.timeframeConfig.selectedTimeframes.length > 1 
                ? input.timeframeConfig.selectedTimeframes.length 
                : 1;
              const crashMultiplier = input.crashProtectionMode === 'both' ? 2 : 1;
              // HMH multiplier: if enabled, multiply by number of offsets to test
              const hmhMultiplier = input.hmhConfig?.enabled && input.hmhConfig.offsets?.length > 0 
                ? input.hmhConfig.offsets.length 
                : 1;
              
              // Check if this is enhanced signal-based mode (indicator pairs)
              if (input.enhancedSignalConfig?.enabled) {
                const entryCount = input.enhancedSignalConfig.entryIndicators?.length || 0;
                const exitCount = input.enhancedSignalConfig.exitIndicators?.length || 0;
                const entrySamples = input.enhancedSignalConfig.entrySamplesPerIndicator || 5;
                const exitSamples = input.enhancedSignalConfig.exitSamplesPerIndicator || 5;
                // Total = entry_indicators × entry_param_samples × exit_indicators × exit_param_samples × hmhMultiplier
                const total = entryCount * entrySamples * exitCount * exitSamples * hmhMultiplier;
                console.log(`[totalTests calc - SIGNAL MODE] epic=${epicToRun}, entries=${entryCount}×${entrySamples}, exits=${exitCount}×${exitSamples}, hmh=${hmhMultiplier}, total=${total}`);
                return total;
              }
              
              // Standard timing-based mode
              const total = input.indicators.length * input.numSamples * timeframeMultiplier * crashMultiplier * hmhMultiplier;
              console.log(`[totalTests calc] epic=${epicToRun}, indicators=${input.indicators.length}, numSamples=${input.numSamples}, timeframe=${timeframeMultiplier}, crash=${crashMultiplier}, hmh=${hmhMultiplier}, crashMode=${input.crashProtectionMode}, total=${total}`);
              return total;
            })(),
          });
          
          runIds.push(runId);
          
          // Trigger Python backtest in background
          runBacktest({
            runId,
            epic: epicToRun,
            startDate: input.startDate,
            endDate: input.endDate,
            initialBalance: input.initialBalance,
            monthlyTopup: input.monthlyTopup,
            investmentPct: input.investmentPct,
            indicators: input.indicators,
            numSamples: input.numSamples,
            direction: input.direction,
            batchMode: input.batchMode,
            optimizationStrategy: input.optimizationStrategy,
            calculationMode: input.calculationMode || 'standard',
            stopConditions: input.stopConditions,
            timingConfig: input.timingConfig,
            timeframeConfig: input.timeframeConfig,
            signalBasedConfig: input.signalBasedConfig,
            // VERSION 3: Enhanced signal-based config
            enhancedSignalConfig: input.enhancedSignalConfig,
            crashProtectionMode: input.crashProtectionMode || 'without',
            // HMH (Hold Means Hold) config
            hmhConfig: input.hmhConfig || { enabled: false, offsets: [] },
            freeMemory: input.freeMemory || false,
            testOrder: input.testOrder || 'sequential',
            parallelCores: input.parallelCores || 1,
            rerunDuplicates: input.rerunDuplicates || false,
            dataSource: input.dataSource || 'capital',  // Always use Capital.com data (UTC)
          }).catch(console.error);
        }
        
        // Return primary runId (first one) for backward compatibility
        // Also return all runIds for multi-epic tracking
        return { runId: runIds[0], runIds, epicsCount: epicsToRun.length };
      }),

    getStatus: publicProcedure
      .input(z.object({ runId: z.number() }))
      .query(async ({ input }) => {
        const run = await db.getBacktestRun(input.runId);
        
        // Auto-sync status: if marked as "paused" but cores are active, update to "running"
        if (run && run.status === 'paused') {
          const coreStatus = resourceManager.getRunCoreStatus(input.runId);
          const hasActiveCores = coreStatus.some(c => c.allocation?.status === 'running');
          
          if (hasActiveCores) {
            // Cores are active, update status to running
            await db.updateBacktestRun(input.runId, { status: 'running', pausedAt: null });
            run.status = 'running';
            run.pausedAt = null;
            console.log(`[Status Sync] Auto-updated run #${input.runId} from paused to running (active cores detected)`);
          }
        }
        
        // Add actualResultsCount and recalculate progress based on completedTests
        // completedTests = ALL processed (ran + skipped), actualResultsCount = saved results only
        if (run) {
          const results = await db.getBacktestResults(input.runId);
          const actualResultsCount = results.length;
          const completedTests = run.completedTests || 0;
          
          // Recalculate progress based on ALL processed tests (completedTests)
          if (run.totalTests && run.totalTests > 0) {
            run.progress = Math.floor((completedTests / run.totalTests) * 100);
          }
          
          // Add actualResultsCount for UI to show "X ran, Y skipped"
          (run as any).actualResultsCount = actualResultsCount;
          (run as any).duplicateCount = run.duplicateCount || Math.max(0, completedTests - actualResultsCount);
        }
        
        return run;
      }),

    getResults: publicProcedure
      .input(z.object({ 
        runId: z.number(),
        // VERSION 3: Optional filter by backtest type
        backtestType: z.enum(['all', 'time_based', 'signal_based']).optional().default('all'),
      }))
      .query(async ({ input }) => {
        const results = await db.getBacktestResults(input.runId);
        
        // Filter by backtest type if specified
        if (input.backtestType && input.backtestType !== 'all') {
          return results.filter(r => (r.backtestType || 'time_based') === input.backtestType);
        }
        
        return results;
      }),

    stop: publicProcedure
      .input(z.object({ runId: z.number() }))
      .mutation(async ({ input }) => {
        // Stop both backtest AND discovery processes (run could be either type)
        // This ensures the frontend's single "Stop" button works for all run types
        stopBacktest(input.runId);
        
        // Also try to stop as discovery run (discovery runs use runningDiscoveries map)
        try {
          const { stopDiscovery } = await import('./discovery_bridge');
          stopDiscovery(input.runId);
        } catch (e) {
          // Discovery bridge might not have this run - that's fine
        }
        
        // Roll up best result from existing results before marking as stopped
        const bestResult = await db.getBestResultForRun(input.runId);
        
        await db.updateBacktestRun(input.runId, {
          status: 'stopped',
          completedAt: new Date(),
          ...(bestResult && {
            bestResult: {
              indicatorName: bestResult.indicatorName,
              finalBalance: bestResult.finalBalance,
              totalReturn: bestResult.totalReturn,
              winRate: bestResult.winRate,
              sharpeRatio: bestResult.sharpeRatio,
            },
          }),
        });
        
        // Clean up queue entries for stopped runs (user pressed stop, won't resume)
        await db.deleteQueueItems(input.runId);
        console.log(`[Backtest] Stopped run ${input.runId}, best result: ${bestResult?.indicatorName || 'none'} ($${bestResult?.finalBalance?.toFixed(2) || 0})`);
        return { success: true };
      }),

    // Check what would be affected by deleting a run (for modal preview)
    checkDeleteRun: publicProcedure
      .input(z.object({ runId: z.number() }))
      .query(async ({ input }) => {
        const result = await db.deleteBacktestRun(input.runId, 'check');
        return result;
      }),

    // Delete a run with mode: 'except_protected' keeps strategy-linked results, 'force' deletes all
    deleteRun: publicProcedure
      .input(z.object({ 
        runId: z.number(),
        mode: z.enum(['except_protected', 'force']).default('except_protected'),
      }))
      .mutation(async ({ input }) => {
        const result = await db.deleteBacktestRun(input.runId, input.mode);
        console.log(`[Backtest] Delete run ${input.runId} (mode: ${input.mode}): ${result.deletedResults}/${result.totalResults} results deleted, run ${result.runDeleted ? 'deleted' : 'kept'}`);
        return result;
      }),

    getLogs: publicProcedure
      .input(z.object({ runId: z.number() }))
      .query(async ({ input }) => {
        const BacktestLogger = (await import('./logger')).default;
        const logs = BacktestLogger.getRunLogs(input.runId);
        return { logs };
      }),

    getRecentLogs: publicProcedure
      .input(z.object({ lines: z.number().optional().default(100) }))
      .query(async ({ input }) => {
        const BacktestLogger = (await import('./logger')).default;
        const logs = BacktestLogger.getRecentLogs(input.lines);
        return { logs };
      }),

    listRuns: publicProcedure.query(async ({ ctx }) => {
      return await db.getUserBacktestRuns(ctx.user?.id || 1);
    }),

    getAllRuns: publicProcedure.query(async ({ ctx }) => {
      return await db.getUserBacktestRuns(ctx.user?.id || 1);
    }),

    // Get map of backtest IDs to strategy names (for showing which results are saved)
    getSavedToStrategiesMap: publicProcedure.query(async ({ ctx }) => {
      const dbInstance = await db.getDb();
      if (!dbInstance) return {};
      
      const { savedStrategies } = await import('../drizzle/schema');
      const strategies = await dbInstance.select({
        id: savedStrategies.id,
        name: savedStrategies.name,
        dnaStrands: savedStrategies.dnaStrands,
      }).from(savedStrategies).where(eq(savedStrategies.userId, ctx.user?.id || 1));
      
      // Build map: backtestId → { strategyId, strategyName }
      const map: Record<number, { strategyId: number; strategyName: string }[]> = {};
      
      for (const strategy of strategies) {
        if (strategy.dnaStrands && Array.isArray(strategy.dnaStrands)) {
          for (const dna of strategy.dnaStrands) {
            const sourceTestId = (dna as any).sourceTestId;
            if (sourceTestId) {
              if (!map[sourceTestId]) {
                map[sourceTestId] = [];
              }
              map[sourceTestId].push({
                strategyId: strategy.id,
                strategyName: strategy.name,
              });
            }
          }
        }
      }
      
      return map;
    }),

    // VERSION 3: Attach signal-based backtest to strategy with trust matrix
    // Copies the trust matrix scores for the epic/timeframe to the strategy for live trading
    attachSignalBacktestToStrategy: publicProcedure
      .input(z.object({
        strategyId: z.number(),
        backtestResultId: z.number(),
        epic: z.string(),
        timeframe: z.string(),
      }))
      .mutation(async ({ input }) => {
        // Get the backtest result to extract the enhanced signal config
        const result = await db.getBacktestResultWithEnhancedConfig(input.backtestResultId);
        if (!result) {
          throw new Error(`Backtest result ${input.backtestResultId} not found`);
        }

        // Verify it's a signal-based backtest
        if (result.backtestType !== 'signal_based' || !result.enhancedSignalConfig) {
          throw new Error('This backtest is not a signal-based backtest');
        }

        // Load the trust matrix snapshot from the database
        const trustMatrix = await db.getTrustMatrixSnapshot(input.epic, input.timeframe);
        
        // Update the strategy with the config and trust matrix
        await db.updateStrategyWithTrustMatrix(
          input.strategyId,
          result.enhancedSignalConfig,
          trustMatrix
        );

        return {
          success: true,
          message: `Attached signal-based backtest to strategy with ${trustMatrix.length} trust matrix pairs`,
          trustMatrixCount: trustMatrix.length,
        };
      }),

    // Paginated results for Results History (lightweight, no trades/dailyBalances)
    // Supports server-side sorting and filtering for lightning-fast loading
    getPaginatedResults: publicProcedure
      .input(z.object({
        runId: z.number().optional(),  // Optional: filter to specific run (Results page)
        limit: z.number().min(1).max(1000).default(100),  // Increased max to 1000
        offset: z.number().min(0).default(0),
        sortBy: z.enum([
          'totalReturn', 'winRate', 'sharpeRatio', 'maxDrawdown', 'totalTrades', 'finalBalance', 'createdAt', 'leverage', 'indicatorName',
          // Additional columns for VirtualizedHistoryTable sorting
          'epic', 'liquidationCount', 'minMarginLevel', 'crashProtectionEnabled', 'initialBalance', 'monthlyTopup',
          // Date and stop loss columns
          'startDate', 'endDate', 'stopLoss'
        ]).optional(),
        sortDir: z.enum(['asc', 'desc']).optional(),
        // Server-side filters
        epic: z.string().optional(),
        indicatorName: z.string().optional(),
        minReturn: z.number().optional(),
        minWinRate: z.number().optional(),
        minSharpe: z.number().optional(),
        maxDrawdown: z.number().optional(),
        minTrades: z.number().optional(),
        maxLeverage: z.number().optional(),
        // Liquidation and Margin Level filters
        maxLiquidations: z.number().optional(),  // Show results with Liqs ≤ this
        minMarginLevel: z.number().optional(),   // Show results with MinML ≥ this
      }))
      .query(async ({ input, ctx }) => {
        return await db.getPaginatedBacktestResults(ctx.user?.id || 1, input);
      }),

    // Get filter metadata (unique epics, indicators, leverage values) for Results History
    // Lightweight query - no full result data, just distinct values and aggregates
    getFilterMetadata: publicProcedure
      .input(z.object({
        runId: z.number().optional(),  // Optional: filter to specific run
      }))
      .query(async ({ input, ctx }) => {
        return await db.getBacktestFilterMetadata(ctx.user?.id || 1, input.runId);
      }),

    // Get test IDs that are used in strategies (as DNA strands)
    // Used to show a star next to results that are in strategies
    getProtectedTestIds: publicProcedure
      .query(async () => {
        const protectedIds = await db.getProtectedTestIds();
        return Array.from(protectedIds);
      }),

    getRun: publicProcedure
      .input(z.object({ runId: z.number() }))
      .query(async ({ input }) => {
        return await db.getBacktestRun(input.runId);
      }),

    // Get a single backtest result by ID (full data including trades/dailyBalances)
    getResult: publicProcedure
      .input(z.object({ resultId: z.number() }))
      .query(async ({ input }) => {
        // Use getBacktestResultFull for detail view - includes trades/dailyBalances
        return await db.getBacktestResultFull(input.resultId);
      }),

    // Re-run a specific backtest test with new parameters (dates, amounts)
    // Keeps the same indicator, leverage, stop loss, and other core settings
    rerunTest: publicProcedure
      .input(z.object({
        testId: z.number(),
        startDate: z.string(),  // YYYY-MM-DD
        endDate: z.string(),    // YYYY-MM-DD
        initialBalance: z.number().min(1),
        monthlyTopup: z.number().min(0),
      }))
      .mutation(async ({ input }) => {
        const startTime = Date.now();
        
        console.log('\n' + '='.repeat(80));
        console.log('[RerunTest] ========== RE-RUN TEST STARTED ==========');
        console.log('[RerunTest] Timestamp:', new Date().toISOString());
        console.log('[RerunTest] Test ID:', input.testId);
        console.log('='.repeat(80));
        
        // 1. Fetch the original test
        console.log('\n[RerunTest] Step 1: Fetching original test from database...');
        const originalTest = await db.getBacktestResultFull(input.testId);
        if (!originalTest) {
          console.error('[RerunTest] ❌ ERROR: Test not found in database');
          throw new Error('Test not found');
        }
        
        // Log original test configuration
        console.log('\n[RerunTest] ===== ORIGINAL TEST CONFIGURATION =====');
        console.log('[RerunTest] Epic:', originalTest.epic);
        console.log('[RerunTest] Indicator:', originalTest.indicatorName);
        console.log('[RerunTest] Indicator Params:', JSON.stringify(originalTest.indicatorParams, null, 2));
        console.log('[RerunTest] Leverage:', originalTest.leverage);
        console.log('[RerunTest] Stop Loss:', originalTest.stopLoss, '%');
        console.log('[RerunTest] Timeframe:', originalTest.timeframe || '5m');
        console.log('[RerunTest] Crash Protection:', originalTest.crashProtectionEnabled);
        console.log('[RerunTest] Timing Config:', JSON.stringify(originalTest.timingConfig, null, 2));
        console.log('[RerunTest] Data Source:', originalTest.dataSource);
        
        // Log original test results (BEFORE re-run)
        console.log('\n[RerunTest] ===== ORIGINAL TEST RESULTS (BEFORE) =====');
        console.log('[RerunTest] Date Range:', originalTest.startDate, 'to', originalTest.endDate);
        console.log('[RerunTest] Initial Balance: $' + originalTest.initialBalance);
        console.log('[RerunTest] Total Contributions: $' + originalTest.totalContributions);
        console.log('[RerunTest] Final Balance: $' + originalTest.finalBalance?.toFixed(2));
        console.log('[RerunTest] Total Return:', originalTest.totalReturn?.toFixed(2), '%');
        console.log('[RerunTest] Total Trades:', originalTest.totalTrades);
        console.log('[RerunTest] Win Rate:', originalTest.winRate?.toFixed(1), '%');
        console.log('[RerunTest] Max Drawdown:', originalTest.maxDrawdown?.toFixed(2), '%');
        console.log('[RerunTest] Sharpe Ratio:', originalTest.sharpeRatio?.toFixed(2));
        console.log('[RerunTest] Liquidation Count:', originalTest.liquidationCount || 0);
        console.log('[RerunTest] Min Margin Level:', originalTest.minMarginLevel?.toFixed(1) || 'N/A', '%');
        
        // Log new parameters
        console.log('\n[RerunTest] ===== NEW PARAMETERS FOR RE-RUN =====');
        console.log('[RerunTest] New Date Range:', input.startDate, 'to', input.endDate);
        console.log('[RerunTest] New Initial Balance: $' + input.initialBalance);
        console.log('[RerunTest] New Monthly Top-up: $' + input.monthlyTopup);
        
        // Log what's changing vs staying the same
        console.log('\n[RerunTest] ===== PARAMETER COMPARISON =====');
        console.log('[RerunTest] Date Range:', originalTest.startDate, '→', input.startDate, '|', originalTest.endDate, '→', input.endDate);
        console.log('[RerunTest] Initial Balance: $' + originalTest.initialBalance, '→ $' + input.initialBalance);
        console.log('[RerunTest] (Unchanged) Epic:', originalTest.epic);
        console.log('[RerunTest] (Unchanged) Indicator:', originalTest.indicatorName);
        console.log('[RerunTest] (Unchanged) Leverage:', originalTest.leverage);
        console.log('[RerunTest] (Unchanged) Stop Loss:', originalTest.stopLoss, '%');
        console.log('[RerunTest] (Unchanged) Crash Protection:', originalTest.crashProtectionEnabled);

        // 2. Import the single test runner
        console.log('\n[RerunTest] Step 2: Loading backtest runner...');
        const { runSingleBacktest } = await import('./backtest_single_runner');

        // 3. Run the backtest with original params but new dates/amounts
        console.log('\n[RerunTest] Step 3: Starting backtest execution...');
        console.log('[RerunTest] ===== BACKTEST CONFIG =====');
        const backtestConfig = {
          testId: input.testId,
          epic: originalTest.epic,
          startDate: input.startDate,
          endDate: input.endDate,
          indicatorName: originalTest.indicatorName,
          indicatorParams: originalTest.indicatorParams as Record<string, any>,
          leverage: originalTest.leverage,
          stopLoss: originalTest.stopLoss,
          initialBalance: input.initialBalance,
          monthlyTopup: input.monthlyTopup,
          investmentPct: 99,
          direction: 'long',
          timeframe: originalTest.timeframe || '5m',
          timingConfig: originalTest.timingConfig,
          crashProtectionEnabled: originalTest.crashProtectionEnabled || false,
          dataSource: (originalTest.dataSource as 'capital') || 'capital',
        };
        console.log(JSON.stringify(backtestConfig, null, 2));
        
        let result;
        try {
          result = await runSingleBacktest(backtestConfig);
        } catch (error: any) {
          console.error('\n[RerunTest] ❌ BACKTEST EXECUTION FAILED');
          console.error('[RerunTest] Error:', error.message);
          console.error('[RerunTest] Stack:', error.stack);
          throw error;
        }
        
        const duration = Date.now() - startTime;
        
        // Log new results
        console.log('\n[RerunTest] ===== NEW TEST RESULTS (AFTER) =====');
        console.log('[RerunTest] Final Balance: $' + result.finalBalance?.toFixed(2));
        console.log('[RerunTest] Total Return:', result.totalReturn?.toFixed(2), '%');
        console.log('[RerunTest] Total Trades:', result.totalTrades);
        console.log('[RerunTest] Win Rate:', result.winRate?.toFixed(1), '%');
        console.log('[RerunTest] Max Drawdown:', result.maxDrawdown?.toFixed(2), '%');
        console.log('[RerunTest] Sharpe Ratio:', result.sharpeRatio?.toFixed(2));
        console.log('[RerunTest] Liquidation Count:', result.liquidationCount || 0);
        console.log('[RerunTest] Min Margin Level:', result.minMarginLevel?.toFixed(1) || 'N/A', '%');
        
        // Log comparison if dates/amounts are the same (for debugging)
        const sameConfig = 
          input.startDate === originalTest.startDate && 
          input.endDate === originalTest.endDate &&
          input.initialBalance === originalTest.initialBalance;
          
        if (sameConfig) {
          console.log('\n[RerunTest] ⚠️ SAME CONFIG COMPARISON (should be identical results):');
          console.log('[RerunTest] Return Difference:', ((result.totalReturn || 0) - (originalTest.totalReturn || 0)).toFixed(2), '%');
          console.log('[RerunTest] Trades Difference:', (result.totalTrades || 0) - (originalTest.totalTrades || 0));
          console.log('[RerunTest] Win Rate Difference:', ((result.winRate || 0) - (originalTest.winRate || 0)).toFixed(1), '%');
          
          if (Math.abs((result.totalReturn || 0) - (originalTest.totalReturn || 0)) > 0.01) {
            console.log('[RerunTest] ❌ RESULTS DIFFER - This may indicate a bug!');
          } else {
            console.log('[RerunTest] ✓ Results match within tolerance');
          }
        }
        
        console.log('\n[RerunTest] ========== RE-RUN TEST COMPLETE ==========');
        console.log('[RerunTest] Duration:', duration, 'ms');
        console.log('[RerunTest] Success: ✓');
        console.log('='.repeat(80) + '\n');
        
        return result;
      }),

    getActiveRuns: publicProcedure.query(async ({ ctx }) => {
      const runs = await db.getActiveBacktestRuns(ctx.user?.id || 1);
      
      // Enrich each run with actual results count
      const enrichedRuns = await Promise.all(runs.map(async (run) => {
        const results = await db.getBacktestResults(run.id);
        const actualResultsCount = results.length;  // Tests that saved to DB
        const completedTests = run.completedTests || 0;  // ALL processed tests (ran + skipped)
        const calculatedSkipped = Math.max(0, completedTests - actualResultsCount);
        
        // Progress based on ALL processed tests (completedTests), not just results
        // This ensures progress bar matches "X of Y tests processed"
        const recalculatedProgress = run.totalTests && run.totalTests > 0 
          ? Math.floor((completedTests / run.totalTests) * 100)
          : run.progress || 0;
        
        return {
          ...run,
          actualResultsCount,  // Tests that ran and saved results
          completedTests,      // ALL processed tests (keep original, don't override)
          progress: recalculatedProgress,
          // Use duplicateCount if available, otherwise calculate from difference
          duplicateCount: (run.duplicateCount ?? 0) || calculatedSkipped,
        };
      }));
      
      return enrichedRuns;
    }),

    pause: publicProcedure
      .input(z.object({ runId: z.number() }))
      .mutation(async ({ input }) => {
        // =====================================================================
        // CRITICAL: Update database status to 'paused' FIRST, BEFORE killing processes!
        // The close handler checks DB status to distinguish pause from failure.
        // If we kill first, status is still 'running' when close handler runs.
        // =====================================================================
        console.log(`[Backtest] Pausing run ${input.runId} - updating DB status first`);
        await db.updateBacktestRun(input.runId, {
          status: 'paused',
          pausedAt: new Date(),
        });
        
        // Now kill any running Python processes for this run
        const { runningBacktests } = await import('./backtest_bridge_v2');
        const running = runningBacktests.get(input.runId);
        if (running) {
          console.log(`[Backtest] Killing Python processes for paused run ${input.runId}`);
          for (const process of running.processes) {
            try {
              process.kill();
            } catch (error) {
              console.error(`[Backtest] Error killing process on pause:`, error);
            }
          }
          running.processes = []; // Clear the processes array
        }
        
        // Also try to pause as discovery run
        try {
          const { pauseDiscovery } = await import('./discovery_bridge');
          await pauseDiscovery(input.runId);
        } catch (e) {
          // Discovery bridge might not have this run - that's fine
        }
        
        // Release cores back to the pool
        resourceManager.releaseRunCores(input.runId);
        
        return { success: true };
      }),

    resume: publicProcedure
      .input(z.object({ runId: z.number() }))
      .mutation(async ({ input }) => {
        // Get the original run configuration
        const run = await db.getBacktestRun(input.runId);
        if (!run) {
          throw new Error('Backtest run not found');
        }
        
        // Ensure any stale core allocations are released before resuming
        // This handles edge cases where cores weren't properly released on pause
        const staleReleased = resourceManager.releaseRunCores(input.runId);
        if (staleReleased > 0) {
          console.log(`[Backtest] Resume: Released ${staleReleased} stale cores before restart`);
        }
        
        // Check available cores vs requested
        const requestedCores = run.parallelCores || 1;
        const availableCores = resourceManager.getAvailableCores();
        
        if (availableCores < requestedCores) {
          console.warn(`[Backtest] Resume warning: Requested ${requestedCores} cores but only ${availableCores} available. Will allocate partial.`);
        }
        
        console.log(`[Backtest] Resuming run #${input.runId} with ${requestedCores} cores (${availableCores} available)`);
        
        // Update status to running
        await db.updateBacktestRun(input.runId, {
          status: 'running',
          resumedAt: new Date(),
        });
        
        // Restart Python process with original configuration (will load checkpoint)
        runBacktest({
          runId: input.runId,
          epic: run.epic,
          startDate: run.startDate,
          endDate: run.endDate,
          initialBalance: run.initialBalance,
          monthlyTopup: run.monthlyTopup,
          investmentPct: run.investmentPct,
          indicators: run.indicators as any,
          numSamples: run.numSamples,
          direction: run.direction as any,
          batchMode: true,
          timingConfig: run.timingConfig as any,
          timeframeConfig: run.timeframeConfig as any,
          stopConditions: run.stopConditions as any,
          crashProtectionMode: run.crashProtectionMode as any,
          optimizationStrategy: run.optimizationStrategy || 'random',
          freeMemory: run.freeMemory || false,
          testOrder: run.testOrder as any,
          parallelCores: requestedCores,
        }).catch((err) => {
          console.error(`[Backtest] Resume failed for run #${input.runId}:`, err);
          // Mark as failed if Python fails to start
          db.updateBacktestRun(input.runId, {
            status: 'failed',
            errorMessage: `Resume failed: ${err.message}`,
          }).catch(console.error);
        });
        
        return { success: true };
      }),

    // Update core allocation for a running/paused backtest
    // Changes take effect at next process recycle (running) or on resume (paused)
    updateCores: publicProcedure
      .input(z.object({ 
        runId: z.number(),
        parallelCores: z.number().min(1).max(64), // Support up to 64 cores (backend validates against actual available)
      }))
      .mutation(async ({ input }) => {
        const run = await db.getBacktestRun(input.runId);
        if (!run) {
          throw new Error('Backtest run not found');
        }
        
        // Only allow updating running or paused runs
        if (run.status !== 'running' && run.status !== 'paused') {
          throw new Error(`Cannot update cores for run with status: ${run.status}`);
        }
        
        const currentDbCores = run.parallelCores || 1;
        const totalCores = resourceManager.getTotalCores();
        
        // For PAUSED runs: cores should be released, so just check total system cores
        // For RUNNING runs: check if additional cores needed are available
        if (run.status === 'paused') {
          // Cores should be free (released on pause)
          const available = resourceManager.getAvailableCores();
          if (input.parallelCores > available) {
            console.warn(`[Backtest] Warning: Requested ${input.parallelCores} cores for paused run but only ${available} currently available`);
            // Don't error - cores might be freed by other runs by resume time
          }
        } else {
          // Running - check if we can get more cores
          const currentlyAllocated = resourceManager.getRunCoreStatus(input.runId).length;
          const available = resourceManager.getAvailableCores();
          const coresNeeded = input.parallelCores - currentlyAllocated;
          
          if (coresNeeded > 0 && coresNeeded > available) {
            throw new Error(`Not enough cores available. Need ${coresNeeded} more, but only ${available} available (${currentlyAllocated} currently allocated to this run).`);
          }
        }
        
        // Update the database
        await db.updateBacktestRun(input.runId, {
          parallelCores: input.parallelCores,
        });
        
        const effectMessage = run.status === 'paused' 
          ? 'Change takes effect when you resume the run.'
          : 'Change takes effect at next process recycle (~500 tests).';
        
        console.log(`[Backtest] Run #${input.runId} cores updated: ${currentDbCores} → ${input.parallelCores} (${effectMessage})`);
        
        return { 
          success: true, 
          previousCores: currentDbCores,
          newCores: input.parallelCores,
          message: `Core allocation updated. ${effectMessage}`,
        };
      }),

  }),

  // Chain Backtest Discovery
  // USES SAME PATTERN AS backtest.run - creates record in backtestRuns table
  // so it integrates with existing Results page, progress tracking, resource management
  discovery: router({
    // Start a new discovery run
    start: publicProcedure
      .input(z.object({
        epic: z.string(),
        timeframe: z.string().default('5m'),
        timeframes: z.array(z.string()).optional(),  // Support multiple timeframes
        startDate: z.string(),        // From GUI date picker
        endDate: z.string(),          // From GUI date picker
        crashProtection: z.boolean().default(false),  // From GUI checkbox
        directionMode: z.enum(['long_only', 'short_only', 'reverse']),
        entryIndicators: z.array(z.string()),
        exitIndicators: z.array(z.string()),
        initialBalance: z.number().default(500),
        monthlyTopup: z.number().default(100),
        positionSizePct: z.number().default(99),  // Default 99% like other backtests
        stage1Samples: z.number().default(100),
        stage2Samples: z.number().default(500),
        stage3GridSize: z.number().default(20),
        topNPairs: z.number().default(10),
        topNFinal: z.number().default(3),
        leverageMode: z.enum(['auto', 'fixed', 'search']).default('auto'),
        fixedLeverage: z.number().optional(),
        stopLossMode: z.enum(['auto', 'fixed', 'search']).default('auto'),
        fixedStopLoss: z.number().optional(),
        parallelCores: z.number().default(1),
        // Allocation strategy - phased approach for finding optimal position size
        allocationStrategy: z.object({
          stage1Values: z.array(z.number()).default([1, 2, 3, 4, 5, 10]),
          stage2: z.object({
            from: z.number().default(20),
            to: z.number().default(90),
            step: z.number().default(10),
          }).default({ from: 20, to: 90, step: 10 }),
          stage3: z.object({
            range: z.number().default(10),
            step: z.number().default(2),
          }).default({ range: 10, step: 2 }),
          earlyExitOnLiquidation: z.boolean().default(true),
        }).optional(),
      }))
      .mutation(async ({ ctx, input }) => {
        // Import discovery bridge
        const { runDiscovery } = await import('./discovery_bridge');
        
        const userId = ctx.user?.id || 1;
        
        // Calculate estimated total tests for progress tracking
        const timeframeCount = input.timeframes?.length || 1;
        const stage1Tests = input.entryIndicators.length * input.exitIndicators.length * input.stage1Samples;
        const stage2Tests = input.topNPairs * input.stage2Samples;
        const stage3Tests = input.topNFinal * input.stage3GridSize * input.stage3GridSize;
        const testsPerTimeframe = stage1Tests + stage2Tests + stage3Tests;
        const totalTests = testsPerTimeframe * timeframeCount;
        
        console.log(`[Discovery] Starting discovery for ${input.epic}`);
        console.log(`[Discovery] Date range: ${input.startDate} to ${input.endDate}`);
        console.log(`[Discovery] Timeframes: ${input.timeframes?.join(', ') || input.timeframe}`);
        console.log(`[Discovery] Crash protection: ${input.crashProtection}`);
        console.log(`[Discovery] Position size: ${input.positionSizePct}%`);
        console.log(`[Discovery] Entry indicators: ${input.entryIndicators.join(', ')}`);
        console.log(`[Discovery] Exit indicators: ${input.exitIndicators.join(', ')}`);
        console.log(`[Discovery] Tests per timeframe: ${testsPerTimeframe}, Total: ${totalTests}`);
        
        // Determine timeframes to use (array or single)
        const timeframes = input.timeframes?.length ? input.timeframes : [input.timeframe];
        
        // Create a backtest run record in SAME table as timing-based backtests
        // This lets us use existing Results page, progress polling, etc.
        const runId = await db.createBacktestRun({
          userId,
          epic: input.epic,
          startDate: input.startDate,   // From GUI
          endDate: input.endDate,       // From GUI
          initialBalance: input.initialBalance,
          monthlyTopup: input.monthlyTopup,
          investmentPct: input.positionSizePct,
          indicators: input.entryIndicators,  // Store entry indicators
          numSamples: input.stage1Samples,
          direction: input.directionMode === 'short_only' ? 'short' : 'long',
          timingConfig: { 
            mode: 'Discovery',
            directionMode: input.directionMode,
            exitIndicators: input.exitIndicators,
            stage1Samples: input.stage1Samples,
            stage2Samples: input.stage2Samples,
            stage3GridSize: input.stage3GridSize,
            topNPairs: input.topNPairs,
            topNFinal: input.topNFinal,
            leverageMode: input.leverageMode,
            fixedLeverage: input.fixedLeverage,
            stopLossMode: input.stopLossMode,
            fixedStopLoss: input.fixedStopLoss,
          },
          timeframeConfig: { mode: 'default', selectedTimeframes: timeframes },
          stopConditions: null,
          crashProtectionMode: input.crashProtection ? 'with' : 'without',  // From GUI
          optimizationStrategy: 'discovery',  // Mark as discovery type
          freeMemory: false,
          testOrder: 'sequential',
          parallelCores: input.parallelCores,
          status: 'pending',
          totalTests,
        });
        
        console.log(`[Discovery] Created run #${runId} in backtestRuns table`);
        
        // Start discovery process (same pattern as runBacktest)
        runDiscovery({
          runId,
          epic: input.epic,
          timeframes,                  // Multiple timeframes support
          startDate: input.startDate,  // From GUI
          endDate: input.endDate,      // From GUI
          crashProtection: input.crashProtection,  // From GUI
          directionMode: input.directionMode,
          entryIndicators: input.entryIndicators,
          exitIndicators: input.exitIndicators,
          initialBalance: input.initialBalance,
          monthlyTopup: input.monthlyTopup,
          positionSizePct: input.positionSizePct,
          stage1Samples: input.stage1Samples,
          stage2Samples: input.stage2Samples,
          stage3GridSize: input.stage3GridSize,
          topNPairs: input.topNPairs,
          topNFinal: input.topNFinal,
          leverageMode: input.leverageMode,
          fixedLeverage: input.fixedLeverage,
          stopLossMode: input.stopLossMode,
          fixedStopLoss: input.fixedStopLoss,
          parallelCores: input.parallelCores,
          // Allocation strategy - phased approach
          allocationStrategy: input.allocationStrategy || {
            stage1Values: [1, 2, 3, 4, 5, 10],
            stage2: { from: 20, to: 90, step: 10 },
            stage3: { range: 10, step: 2 },
            earlyExitOnLiquidation: true,
          },
        }).catch(console.error);
        
        // Return same format as backtest.run so frontend redirects to /results/runId
        return { 
          runId,
          runIds: [runId],
        };
      }),
    
    // Get discovery run status
    getStatus: publicProcedure
      .input(z.object({ runId: z.string() }))
      .query(async ({ input }) => {
        const dbInstance = await getDb();
        if (!dbInstance) {
          throw new Error('Database not available');
        }
        
        const result: any = await dbInstance.execute(
          sql`SELECT * FROM discovery_runs WHERE run_id = ${input.runId}`
        );
        
        if (!result[0] || result[0].length === 0) {
          return null;
        }
        
        return result[0][0];
      }),
    
    // List all discovery runs
    list: publicProcedure
      .input(z.object({ 
        epic: z.string().optional(),
        status: z.string().optional(),
        limit: z.number().default(20),
      }))
      .query(async ({ input }) => {
        const dbInstance = await getDb();
        if (!dbInstance) {
          return [];
        }
        
        let query = 'SELECT * FROM discovery_runs WHERE 1=1';
        if (input.epic) {
          query += ` AND epic = '${input.epic}'`;
        }
        if (input.status) {
          query += ` AND status = '${input.status}'`;
        }
        query += ` ORDER BY created_at DESC LIMIT ${input.limit}`;
        
        const result: any = await dbInstance.execute(sql.raw(query));
        return result[0] || [];
      }),
    
    // Get discovered relationships for an epic
    getRelationships: publicProcedure
      .input(z.object({
        epic: z.string(),
        timeframe: z.string().optional(),
        directionMode: z.string().optional(),
        limit: z.number().default(20),
      }))
      .query(async ({ input }) => {
        const dbInstance = await getDb();
        if (!dbInstance) {
          return [];
        }
        
        let query = `SELECT * FROM indicator_relationships WHERE epic = '${input.epic}'`;
        if (input.timeframe) {
          query += ` AND timeframe = '${input.timeframe}'`;
        }
        if (input.directionMode) {
          query += ` AND direction_mode = '${input.directionMode}'`;
        }
        query += ` ORDER BY rank_score DESC LIMIT ${input.limit}`;
        
        const result: any = await dbInstance.execute(sql.raw(query));
        return result[0] || [];
      }),
    
    // Cancel/Stop a running discovery - KILLS PYTHON PROCESSES
    cancel: publicProcedure
      .input(z.object({ runId: z.string() }))
      .mutation(async ({ input }) => {
        const runIdNum = parseInt(input.runId, 10);
        
        // Import and call stopDiscovery to kill Python processes
        const { stopDiscovery } = await import('./discovery_bridge');
        stopDiscovery(runIdNum);
        console.log(`[Discovery] Stopped run ${runIdNum} via cancel endpoint`);
        
        // Also update discovery_runs table if it exists
        const dbInstance = await getDb();
        if (dbInstance) {
          await dbInstance.execute(sql`
            UPDATE discovery_runs 
            SET status = 'cancelled', completed_at = NOW()
            WHERE run_id = ${input.runId} AND status NOT IN ('completed', 'failed', 'cancelled')
          `);
        }
        
        // Update backtestRuns table (discovery uses this table)
        await db.updateBacktestRun(runIdNum, {
          status: 'stopped',
          completedAt: new Date(),
        });
        
        // Clean up queue items
        await db.deleteQueueItems(runIdNum);
        
        return { success: true };
      }),
    
    // Pause a running discovery
    pause: publicProcedure
      .input(z.object({ runId: z.string() }))
      .mutation(async ({ input }) => {
        const runIdNum = parseInt(input.runId, 10);
        
        // Import and call pauseDiscovery
        const { pauseDiscovery } = await import('./discovery_bridge');
        await pauseDiscovery(runIdNum);
        console.log(`[Discovery] Paused run ${runIdNum}`);
        
        return { success: true };
      }),
    
    // Resume a paused discovery (NOTE: Full resume requires re-triggering from UI)
    resume: publicProcedure
      .input(z.object({ runId: z.string() }))
      .mutation(async ({ input }) => {
        const runIdNum = parseInt(input.runId, 10);
        
        // Import and call resumeDiscovery
        const { resumeDiscovery } = await import('./discovery_bridge');
        await resumeDiscovery(runIdNum);
        console.log(`[Discovery] Resume requested for run ${runIdNum}`);
        
        return { success: true, message: 'Run marked as resumable - re-trigger from UI to continue' };
      }),
  }),

  // Data Management
  data: router({
    listEpics: publicProcedure.query(async () => {
      return await db.listEpics();
    }),
    
    // Quick stats audit - updates cached data ranges without downloading
    quickStatsAudit: publicProcedure.mutation(async () => {
      const { candleDataService } = await import('./services/candle_data_service');
      await candleDataService.quickStatsAudit();
      return { success: true };
    }),
    
    // Get refresh progress for real-time updates in the modal
    getRefreshProgress: publicProcedure
      .input(z.object({ epic: z.string() }))
      .query(async ({ input }) => {
        const { candleDataService } = await import('./services/candle_data_service');
        return candleDataService.getRefreshProgress(input.epic) || null;
      }),
    
    // Get daily candles for price overlay on backtest results chart
    // Aggregates from 5m candles if 1d candles not available
    getDailyCandles: publicProcedure
      .input(z.object({ 
        epic: z.string(),
        startDate: z.string(),  // YYYY-MM-DD
        endDate: z.string(),    // YYYY-MM-DD
      }))
      .query(async ({ input }) => {
        const dbInstance = await db.getDb();
        if (!dbInstance) {
          return { candles: [], source: 'none' as const };
        }

        try {
          // First try to get 1d candles directly
          // Use LEFT(timestamp, 10) to extract UTC date directly
          const dailyQuery = `
            SELECT 
              LEFT(timestamp, 10) as date,
              open_bid as open,
              high_bid as high,
              low_bid as low,
              close_bid as close
            FROM candles
            WHERE epic = ? 
              AND source = 'capital' 
              AND timeframe = '1d'
              AND LEFT(timestamp, 10) >= ?
              AND LEFT(timestamp, 10) <= ?
            ORDER BY timestamp ASC
          `;
          
          const dailyResult: any = await dbInstance.execute(
            sql.raw(dailyQuery
              .replace('?', `'${input.epic}'`)
              .replace('?', `'${input.startDate}'`)
              .replace('?', `'${input.endDate}'`)
            )
          );
          
          if (dailyResult[0] && dailyResult[0].length > 0) {
            return {
              candles: dailyResult[0].map((row: any) => ({
                date: row.date,
                open: Number(row.open),
                high: Number(row.high),
                low: Number(row.low),
                close: Number(row.close),
              })),
              source: '1d' as const,
            };
          }
          
          // Fallback: aggregate from 5m candles
          // Use LEFT(timestamp, 10) to extract UTC date directly from ISO string
          // This prevents timezone offset issues (e.g., 2024-11-21T06:31:00Z → 2024-11-21)
          const aggregateQuery = `
            SELECT 
              LEFT(timestamp, 10) as date,
              SUBSTRING_INDEX(GROUP_CONCAT(open_bid ORDER BY timestamp ASC), ',', 1) as open,
              MAX(high_bid) as high,
              MIN(low_bid) as low,
              SUBSTRING_INDEX(GROUP_CONCAT(close_bid ORDER BY timestamp DESC), ',', 1) as close
            FROM candles
            WHERE epic = ? 
              AND source = 'capital' 
              AND timeframe = '5m'
              AND LEFT(timestamp, 10) >= ?
              AND LEFT(timestamp, 10) <= ?
            GROUP BY LEFT(timestamp, 10)
            ORDER BY date ASC
          `;
          
          const aggregateResult: any = await dbInstance.execute(
            sql.raw(aggregateQuery
              .replace('?', `'${input.epic}'`)
              .replace('?', `'${input.startDate}'`)
              .replace('?', `'${input.endDate}'`)
            )
          );
          
          if (aggregateResult[0] && aggregateResult[0].length > 0) {
            return {
              candles: aggregateResult[0].map((row: any) => ({
                date: row.date,
                open: Number(row.open),
                high: Number(row.high),
                low: Number(row.low),
                close: Number(row.close),
              })),
              source: '5m_aggregated' as const,
            };
          }
          
          return { candles: [], source: 'none' as const };
        } catch (error: any) {
          console.error(`[getDailyCandles] Error for ${input.epic}:`, error.message);
          return { candles: [], source: 'error' as const };
        }
      }),

    getCapitalDataRanges: publicProcedure
      .input(z.object({ symbol: z.string() }))
      .query(async ({ input }) => {
        const dbInstance = await db.getDb();
        if (!dbInstance) {
          return { 
            fiveMin: { earliest: null, latest: null, count: 0 },
            oneMin: { earliest: null, latest: null, count: 0 }
          };
        }

        try {
          // Get 5-minute Capital.com data range
          const fiveMinQuery = `
            SELECT 
              MIN(timestamp) as earliest,
              MAX(timestamp) as latest,
              COUNT(*) as count
            FROM candles
            WHERE epic = ? AND source = 'capital' AND timeframe = '5m'
          `;
          const fiveMinResult: any = await dbInstance.execute(sql.raw(fiveMinQuery.replace('?', `'${input.symbol}'`)));
          
          // Get 1-minute Capital.com data range
          const oneMinQuery = `
            SELECT 
              MIN(timestamp) as earliest,
              MAX(timestamp) as latest,
              COUNT(*) as count
            FROM candles
            WHERE epic = ? AND source = 'capital' AND timeframe = '1m'
          `;
          const oneMinResult: any = await dbInstance.execute(sql.raw(oneMinQuery.replace('?', `'${input.symbol}'`)));

          return {
            fiveMin: {
              earliest: fiveMinResult[0]?.[0]?.earliest || null,
              latest: fiveMinResult[0]?.[0]?.latest || null,
              count: Number(fiveMinResult[0]?.[0]?.count) || 0,
            },
            oneMin: {
              earliest: oneMinResult[0]?.[0]?.earliest || null,
              latest: oneMinResult[0]?.[0]?.latest || null,
              count: Number(oneMinResult[0]?.[0]?.count) || 0,
            },
          };
        } catch (error: any) {
          console.error(`[getCapitalDataRanges] Error for ${input.symbol}:`, error.message);
          return { 
            fiveMin: { earliest: null, latest: null, count: 0 },
            oneMin: { earliest: null, latest: null, count: 0 }
          };
        }
      }),

    optimizeDatabase: publicProcedure.mutation(async () => {
      const dbInstance = await db.getDb();
      if (!dbInstance) {
        throw new Error('Database not available');
      }

      // Tables to optimize - ordered by likelihood of having deletions
      const tablesToOptimize = [
        'backtestResults',  // Most deletions happen here
        'backtestRuns',     // Run deletions
        'backtestQueue',    // Queue cleanup
        'candles',          // Candle data (rarely deleted)
      ];

      const results: { table: string; success: boolean; error?: string }[] = [];
      let optimized = 0;

      for (const table of tablesToOptimize) {
        try {
          console.log(`[optimizeDatabase] Optimizing ${table}...`);
          await dbInstance.execute(sql.raw(`OPTIMIZE TABLE ${table}`));
          results.push({ table, success: true });
          optimized++;
          console.log(`[optimizeDatabase] ✅ ${table} optimized`);
        } catch (error: any) {
          console.error(`[optimizeDatabase] ❌ Failed to optimize ${table}:`, error.message);
          results.push({ table, success: false, error: error.message });
        }
      }

      return {
        success: optimized > 0,
        message: `Optimized ${optimized}/${tablesToOptimize.length} tables (backtestResults, backtestRuns, backtestQueue, candles)`,
        tablesOptimized: optimized,
        totalTables: tablesToOptimize.length,
        details: results,
      };
    }),

    getDatabaseSize: publicProcedure.query(async () => {
      const dbInstance = await db.getDb();
      if (!dbInstance) {
        return { totalSizeMB: 0, candleDataSizeMB: 0, otherDataSizeMB: 0 };
      }

      try {
        // Get total database size
        const sizeQuery = `
          SELECT 
            SUM(data_length + index_length) / 1024 / 1024 AS total_size_mb
          FROM information_schema.TABLES
          WHERE table_schema = DATABASE()
        `;
        const totalResult: any = await dbInstance.execute(sql.raw(sizeQuery));
        const totalSizeMB = totalResult[0]?.[0]?.total_size_mb || 0;

        // Get candle data size (unified candles table)
        const candleQuery = `
          SELECT 
            SUM(data_length + index_length) / 1024 / 1024 AS candle_size_mb
          FROM information_schema.TABLES
          WHERE table_schema = DATABASE()
            AND table_name = 'candles'
        `;
        const candleResult: any = await dbInstance.execute(sql.raw(candleQuery));
        const candleDataSizeMB = candleResult[0]?.[0]?.candle_size_mb || 0;

        return {
          totalSizeMB: Number(totalSizeMB),
          candleDataSizeMB: Number(candleDataSizeMB),
          otherDataSizeMB: Number(totalSizeMB) - Number(candleDataSizeMB),
        };
      } catch (error: any) {
        console.error('[getDatabaseSize] Error:', error.message);
        return { totalSizeMB: 0, candleDataSizeMB: 0, otherDataSizeMB: 0 };
      }
    }),

    addEpic: publicProcedure
      .input(z.object({
        symbol: z.string(),
        name: z.string().optional(),
      }))
      .mutation(async ({ input }) => {
        // Check if epic already exists
        const existing = await db.getEpic(input.symbol);
        if (existing) {
          throw new Error(`Epic ${input.symbol} already exists`);
        }

        // Create epic record
        const id = await db.createEpic({
          symbol: input.symbol,
          name: input.name,
          candleCount: 0,
          gapCount: 0,
          coverage: 0,
        });

        // Fetch market hours and market info from Capital.com immediately
        try {
          const { createCapitalAPIClient } = await import('./live_trading/credentials');
          const { fetchAndUpdateMarketHours } = await import('./orchestration/market_hours_service');
          const { fetchAndStoreMarketInfo } = await import('./services/market_info_service');
          
          const client = await createCapitalAPIClient();
          if (client) {
            // Fetch market hours (open/close times)
            await fetchAndUpdateMarketHours(client, input.symbol);
            
            // Fetch full market info (leverage options, contract sizes, funding rates, etc.)
            await fetchAndStoreMarketInfo(client, input.symbol);
          }
        } catch (error: any) {
          console.error(`Failed to fetch market info for ${input.symbol}:`, error.message);
          // Don't fail the entire operation if market info fetch fails
        }

        // REMOVED: Auto-download historical data on epic add
        // User will manually click "Refresh" button when they want to download candle data
        // This makes epic addition instant and prevents unnecessary API usage
        //
        // Old behavior (commented out):
        // try {
        //   const { candleDataService } = await import('./services/candle_data_service');
        //   console.log(`[addEpic] Fetching Capital.com candle data for ${input.symbol}...`);
        //   await candleDataService.fetchHistoricalDataForEpic(input.symbol);
        //   console.log(`[addEpic] Capital.com data fetch complete for ${input.symbol}`);
        // } catch (error: any) {
        //   console.error(`Failed to fetch Capital.com data for ${input.symbol}:`, error.message);
        // }
        
        console.log(`[addEpic] Epic ${input.symbol} added successfully (market info fetched, historical data NOT downloaded - use Refresh button to download)`);


        return { id, symbol: input.symbol };
      }),

    updateEpic: publicProcedure
      .input(z.object({
        symbol: z.string(),
        dataSource: z.literal('capital').optional().default('capital'),  // Only Capital.com data supported
      }))
      .mutation(async ({ input }) => {
        const epic = await db.getEpic(input.symbol);
        if (!epic) {
          throw new Error(`Epic ${input.symbol} not found`);
        }

        // Refresh market info from Capital.com (leverage options, funding rates, etc.)
        try {
          const { createCapitalAPIClient } = await import('./live_trading/credentials');
          const { fetchAndStoreMarketInfo } = await import('./services/market_info_service');
          
          const client = await createCapitalAPIClient();
          if (client) {
            await fetchAndStoreMarketInfo(client, input.symbol);
          }
        } catch (error: any) {
          console.error(`Failed to refresh market info for ${input.symbol}:`, error.message);
          // Continue with candle data refresh even if market info fails
        }

        // Fetch Capital.com data (ALL DATA IS NOW FROM CAPITAL.COM IN UTC)
        try {
          const { candleDataService } = await import('./services/candle_data_service');
          console.log(`[updateEpic] Fetching Capital.com data for ${input.symbol}...`);
          await candleDataService.fetchHistoricalDataForEpic(input.symbol);
          console.log(`[updateEpic] Capital.com data refresh complete for ${input.symbol}`);
          
          return {
            success: true,
            message: `Capital.com data refreshed for ${input.symbol}`,
            dataSource: 'capital',
          };
        } catch (error: any) {
          console.error(`[updateEpic] Failed to fetch Capital.com data for ${input.symbol}:`, error.message);
          throw new Error(`Data refresh failed: ${error.message}`);
        }
        
        // NOTE: Alpha Vantage data refresh has been removed
        // All data now comes from Capital.com in UTC
      }),

    deleteEpic: publicProcedure
      .input(z.object({
        id: z.number(),
      }))
      .mutation(async ({ input }) => {
        // TODO: Also delete candle data from SQLite database
        await db.deleteEpic(input.id);
        return { success: true };
      }),

    updateDesiredStartDate: publicProcedure
      .input(z.object({
        symbol: z.string(),
        desiredStartDate: z.string(),
      }))
      .mutation(async ({ input }) => {
        const epic = await db.getEpic(input.symbol);
        if (!epic) {
          throw new Error(`Epic ${input.symbol} not found`);
        }
        
        await db.updateEpic(epic.id, {
          desiredStartDate: input.desiredStartDate,
        });
        
        return { success: true };
      }),

    forwardFillGaps: publicProcedure
      .input(z.object({
        epic: z.string(),
      }))
      .mutation(async ({ input }) => {
        // Call Python forward-fill script
        const pythonProcess = spawnPython('python_engine/forward_fill.py', {
          args: [input.epic]
        });

        let output = '';
        let errorOutput = '';

        pythonProcess.stdout?.on('data', (data: Buffer) => {
          output += data.toString();
        });

        pythonProcess.stderr?.on('data', (data: Buffer) => {
          errorOutput += data.toString();
        });

        return new Promise((resolve, reject) => {
          pythonProcess.on('close', async (code: number) => {
            if (code !== 0) {
              reject(new Error(`Forward-fill failed: ${errorOutput}`));
              return;
            }

            try {
              const result = JSON.parse(output);
              
              if (result.success) {
                // Re-run data refresh to get updated stats
                const epic = await db.getEpic(input.epic);
                if (epic) {
                  // Update epic metadata
                  await db.updateEpic(epic.id, {
                    gapCount: 0,  // Gaps filled
                  });
                }
              }
              
              resolve(result);
            } catch (e) {
              reject(new Error('Failed to parse forward-fill result'));
            }
          });
        });
      }),
  }),

  // Market Info Management
  marketInfo: router({
    getAll: publicProcedure.query(async () => {
      return await db.getAllMarketInfo();
    }),

    get: publicProcedure
      .input(z.object({ epic: z.string() }))
      .query(async ({ input }) => {
        return await db.getMarketInfo(input.epic);
      }),

    create: publicProcedure
      .input(z.object({
        epic: z.string(),
        spreadPercent: z.string(),
        minContractSize: z.string(),
        maxContractSize: z.string(),
        overnightFundingLongPercent: z.string(),
        overnightFundingShortPercent: z.string(),
        marketOpenTime: z.string(),
        marketCloseTime: z.string(),
        currency: z.string(),
        isActive: z.boolean(),
      }))
      .mutation(async ({ input }) => {
        await db.createMarketInfo(input);
        return { success: true };
      }),

    update: publicProcedure
      .input(z.object({
        epic: z.string(),
        spreadPercent: z.string().optional(),
        minContractSize: z.string().optional(),
        maxContractSize: z.string().optional(),
        overnightFundingLongPercent: z.string().optional(),
        overnightFundingShortPercent: z.string().optional(),
        marketOpenTime: z.string().optional(),
        marketCloseTime: z.string().optional(),
        currency: z.string().optional(),
        isActive: z.boolean().optional(),
      }))
      .mutation(async ({ input }) => {
        const { epic, ...updates } = input;
        await db.updateMarketInfo(epic, updates);
        return { success: true };
      }),

    delete: publicProcedure
      .input(z.object({ epic: z.string() }))
      .mutation(async ({ input }) => {
        await db.deleteMarketInfo(input.epic);
        return { success: true };
      }),
  }),

// OLD_ROUTER:   // ============================================================================
// OLD_ROUTER:   // Strategy Management
// OLD_ROUTER:   // ============================================================================
// OLD_ROUTER:   // DELETE_PROTECTION: Strategy-Test Relationship
// OLD_ROUTER:   // ============================================================================
// OLD_ROUTER:   // IMPORTANT: Before implementing delete functionality for backtestResults:
// OLD_ROUTER:   // 1. Check if testId exists in any strategyCombos.description.tests[]
// OLD_ROUTER:   // 2. Block deletion if test is linked to a strategy
// OLD_ROUTER:   // 3. Require user to unlink test from strategy first
// OLD_ROUTER:   // 4. Keywords for search: PURGE, TRUNCATE, DELETE, CASCADE, ORPHAN
// OLD_ROUTER:   // 
// OLD_ROUTER:   // Implementation guidance:
// OLD_ROUTER:   // - Add db.getStrategiesUsingTest(testId) helper function
// OLD_ROUTER:   // - In delete endpoint, check if result.length > 0
// OLD_ROUTER:   // - Return error: "Cannot delete test {testId}, used by {count} strategies"
// OLD_ROUTER:   // - Provide list of strategy names using this test
// OLD_ROUTER:   // 
// OLD_ROUTER:   // Related functions:
// OLD_ROUTER:   // - db.getBacktestResult(id) - fetches test by ID
// OLD_ROUTER:   // - db.getStrategy(id) - fetches strategy (tests in description JSON)
// OLD_ROUTER:   // - strategyCombos.description = JSON { tests: [{ testId, ... }] }
// OLD_ROUTER:   // ============================================================================
// OLD_ROUTER:   strategies: router({
// OLD_ROUTER:     list: publicProcedure.query(async ({ ctx }) => {
// OLD_ROUTER:       return await db.getUserStrategies(ctx.user?.id || 1);
// OLD_ROUTER:     }),
// OLD_ROUTER: 
// OLD_ROUTER:     get: publicProcedure
// OLD_ROUTER:       .input(z.object({ id: z.number() }))
// OLD_ROUTER:       .query(async ({ input }) => {
// OLD_ROUTER:         return await db.getStrategy(input.id);
// OLD_ROUTER:       }),
// OLD_ROUTER: 
// OLD_ROUTER:     create: publicProcedure
// OLD_ROUTER:       .input(z.object({
// OLD_ROUTER:         name: z.string(),
// OLD_ROUTER:         description: z.string().optional(),
// OLD_ROUTER:         resultIds: z.array(z.number()).optional(),
// OLD_ROUTER:         tests: z.array(z.object({
// OLD_ROUTER:           epic: z.string(),
// OLD_ROUTER:           indicatorName: z.string(),
// OLD_ROUTER:           indicatorParams: z.record(z.string(), z.any()),
// OLD_ROUTER:           leverage: z.number(),
// OLD_ROUTER:           stopLoss: z.number(),
// OLD_ROUTER:           allocationPercent: z.number(),
// OLD_ROUTER:           timingConfig: z.object({
// OLD_ROUTER:             mode: z.enum(['ManusTime', 'OriginalBotTime', 'Custom', 'Random', 'SpecificHour']),
// OLD_ROUTER:             closeTradesOffset: z.number().optional(),
// OLD_ROUTER:             calculateOffset: z.number().optional(),
// OLD_ROUTER:             openTradesOffset: z.number().optional(),
// OLD_ROUTER:             customTime: z.string().optional(),
// OLD_ROUTER:           }),
// OLD_ROUTER:         })).optional(),
// OLD_ROUTER:         conflictResolution: z.object({
// OLD_ROUTER:           mode: z.string(),
// OLD_ROUTER:           weights: z.record(z.string(), z.number()).optional(),
// OLD_ROUTER:         }).optional(),
// OLD_ROUTER:       }))
// OLD_ROUTER:       .mutation(async ({ ctx, input }) => {
// OLD_ROUTER:         // If resultIds provided, fetch test data from backtestResults
// OLD_ROUTER:         let tests = input.tests || [];
// OLD_ROUTER:         if (input.resultIds && input.resultIds.length > 0) {
// OLD_ROUTER:           const results = await db.getBacktestResultsByIds(input.resultIds);
// OLD_ROUTER:           
// OLD_ROUTER:           // Fetch runs for each result to get startDate, endDate, monthlyTopup
// OLD_ROUTER:           const runIds = Array.from(new Set(results.map(r => r.runId)));
// OLD_ROUTER:           const runs = await Promise.all(runIds.map(id => db.getBacktestRun(id)));
// OLD_ROUTER:           const runMap = new Map(runs.filter(r => r !== null).map(r => [r!.id, r!]));
// OLD_ROUTER:           
// OLD_ROUTER:           tests = results.map(r => {
// OLD_ROUTER:             const run = runMap.get(r.runId);
// OLD_ROUTER:             return {
// OLD_ROUTER:               // Fields brain uses (UNCHANGED)
// OLD_ROUTER:               epic: r.epic || 'SOXL',
// OLD_ROUTER:               indicatorName: r.indicatorName,
// OLD_ROUTER:               indicatorParams: r.indicatorParams,
// OLD_ROUTER:               leverage: r.leverage,
// OLD_ROUTER:               stopLoss: r.stopLoss,
// OLD_ROUTER:               allocationPercent: 100 / results.length, // Equal allocation by default
// OLD_ROUTER:               timingConfig: r.timingConfig || { mode: 'ManusTime' as const },
// OLD_ROUTER:               timeframe: r.timeframe || '5m',
// OLD_ROUTER:               
// OLD_ROUTER:               // NEW fields for traceability and display (brain ignores)
// OLD_ROUTER:               testId: r.id,
// OLD_ROUTER:               startDate: run?.startDate || '2024-01-01',
// OLD_ROUTER:               endDate: run?.endDate || '2024-12-31',
// OLD_ROUTER:               initialBalance: r.initialBalance,
// OLD_ROUTER:               monthlyTopup: run?.monthlyTopup || 0,
// OLD_ROUTER:               crashProtectionEnabled: r.crashProtectionEnabled,
// OLD_ROUTER:               
// OLD_ROUTER:               // Performance snapshot (for display, brain queries backtestResults table)
// OLD_ROUTER:               performance: {
// OLD_ROUTER:                 totalReturn: r.totalReturn,
// OLD_ROUTER:                 sharpeRatio: r.sharpeRatio,
// OLD_ROUTER:                 winRate: r.winRate,
// OLD_ROUTER:                 maxDrawdown: r.maxDrawdown,
// OLD_ROUTER:                 totalTrades: r.totalTrades,
// OLD_ROUTER:               },
// OLD_ROUTER:             };
// OLD_ROUTER:           });
// OLD_ROUTER:         }
// OLD_ROUTER:         
// OLD_ROUTER:         const strategyId = await db.createStrategy({
// OLD_ROUTER:           userId: ctx.user?.id || 1,
// OLD_ROUTER:           name: input.name,
// OLD_ROUTER:           description: input.description || null,
// OLD_ROUTER:           tests,
// OLD_ROUTER:           conflictResolution: input.conflictResolution || { mode: 'single_best_sharpe' },
// OLD_ROUTER:         });
// OLD_ROUTER:         return { id: strategyId };
// OLD_ROUTER:       }),
// OLD_ROUTER: 
// OLD_ROUTER:     addTest: publicProcedure
// OLD_ROUTER:       .input(z.object({
// OLD_ROUTER:         strategyId: z.number(),
// OLD_ROUTER:         resultId: z.number(),
// OLD_ROUTER:       }))
// OLD_ROUTER:       .mutation(async ({ input }) => {
// OLD_ROUTER:         // Fetch the result
// OLD_ROUTER:         const result = await db.getBacktestResult(input.resultId);
// OLD_ROUTER:         if (!result) {
// OLD_ROUTER:           throw new Error('Backtest result not found');
// OLD_ROUTER:         }
// OLD_ROUTER:         
// OLD_ROUTER:         // Fetch the run to get startDate, endDate, monthlyTopup
// OLD_ROUTER:         const run = await db.getBacktestRun(result.runId);
// OLD_ROUTER:         if (!run) {
// OLD_ROUTER:           throw new Error('Backtest run not found');
// OLD_ROUTER:         }
// OLD_ROUTER:         
// OLD_ROUTER:         // Get current strategy
// OLD_ROUTER:         const strategy = await db.getStrategy(input.strategyId);
// OLD_ROUTER:         if (!strategy) {
// OLD_ROUTER:           throw new Error('Strategy not found');
// OLD_ROUTER:         }
// OLD_ROUTER:         
// OLD_ROUTER:         // Add test to strategy with COMPLETE DNA
// OLD_ROUTER:         // Brain reads: epic, indicatorName, indicatorParams, leverage, stopLoss, timingConfig, timeframe
// OLD_ROUTER:         // New fields are for display/reference only (brain ignores them)
// OLD_ROUTER:         const newTest = {
// OLD_ROUTER:           // Fields brain uses (UNCHANGED)
// OLD_ROUTER:           epic: result.epic || 'SOXL',
// OLD_ROUTER:           indicatorName: result.indicatorName,
// OLD_ROUTER:           indicatorParams: result.indicatorParams,
// OLD_ROUTER:           leverage: result.leverage,
// OLD_ROUTER:           stopLoss: result.stopLoss,
// OLD_ROUTER:           allocationPercent: 0, // User will need to adjust allocations
// OLD_ROUTER:           timingConfig: result.timingConfig || { mode: 'ManusTime' as const },
// OLD_ROUTER:           timeframe: result.timeframe || '5m',
// OLD_ROUTER:           
// OLD_ROUTER:           // NEW fields for traceability and display (brain ignores)
// OLD_ROUTER:           testId: result.id, // Reference to backtestResults.id
// OLD_ROUTER:           startDate: run.startDate,
// OLD_ROUTER:           endDate: run.endDate,
// OLD_ROUTER:           initialBalance: result.initialBalance,
// OLD_ROUTER:           monthlyTopup: run.monthlyTopup,
// OLD_ROUTER:           crashProtectionEnabled: result.crashProtectionEnabled,
// OLD_ROUTER:           
// OLD_ROUTER:           // Performance snapshot (for display, brain queries backtestResults table)
// OLD_ROUTER:           performance: {
// OLD_ROUTER:             totalReturn: result.totalReturn,
// OLD_ROUTER:             sharpeRatio: result.sharpeRatio,
// OLD_ROUTER:             winRate: result.winRate,
// OLD_ROUTER:             maxDrawdown: result.maxDrawdown,
// OLD_ROUTER:             totalTrades: result.totalTrades,
// OLD_ROUTER:           },
// OLD_ROUTER:         };
// OLD_ROUTER:         
// OLD_ROUTER:         await db.addTestToStrategy(input.strategyId, newTest);
// OLD_ROUTER:         return { success: true };
// OLD_ROUTER:       }),
// OLD_ROUTER: 
// OLD_ROUTER:     update: publicProcedure
// OLD_ROUTER:       .input(z.object({
// OLD_ROUTER:         id: z.number(),
// OLD_ROUTER:         description: z.string().optional(),
// OLD_ROUTER:         conflictResolution: z.object({
// OLD_ROUTER:           mode: z.string(),
// OLD_ROUTER:         }).optional(),
// OLD_ROUTER:         tests: z.array(z.object({
// OLD_ROUTER:           epic: z.string(),
// OLD_ROUTER:           indicatorName: z.string(),
// OLD_ROUTER:           indicatorParams: z.record(z.string(), z.any()).optional(),
// OLD_ROUTER:           leverage: z.number(),
// OLD_ROUTER:           stopLoss: z.number().optional(),
// OLD_ROUTER:           allocationPercent: z.number(),
// OLD_ROUTER:           timingConfig: z.any().optional(),
// OLD_ROUTER:         })).optional(),
// OLD_ROUTER:       }))
// OLD_ROUTER:       .mutation(async ({ input }) => {
// OLD_ROUTER:         await db.updateStrategy(input.id, {
// OLD_ROUTER:           description: input.description,
// OLD_ROUTER:           conflictResolution: input.conflictResolution,
// OLD_ROUTER:           tests: input.tests,
// OLD_ROUTER:         });
// OLD_ROUTER:         return { success: true };
// OLD_ROUTER:       }),
// OLD_ROUTER: 
// OLD_ROUTER:     testStrategy: publicProcedure
// OLD_ROUTER:       .input(z.object({
// OLD_ROUTER:         strategyId: z.number(),
// OLD_ROUTER:         startDate: z.string(),
// OLD_ROUTER:         endDate: z.string(),
// OLD_ROUTER:         initialBalance: z.number().optional(),
// OLD_ROUTER:         monthlyTopup: z.number().optional(),
// OLD_ROUTER:         calculationMode: z.enum(['standard', 'numba']).optional(),
// OLD_ROUTER:         crashProtection: z.boolean().optional(),
// OLD_ROUTER:         rerunDuplicates: z.boolean().optional(),
// OLD_ROUTER:       }))
// OLD_ROUTER:       .mutation(async ({ input, ctx }) => {
// OLD_ROUTER:         try {
// OLD_ROUTER:           console.log('[Strategy Test] Starting test for strategy:', input.strategyId);
// OLD_ROUTER:           const { executeStrategy } = await import('./strategy_bridge');
// OLD_ROUTER:           const strategy = await db.getStrategy(input.strategyId);
// OLD_ROUTER:           if (!strategy) {
// OLD_ROUTER:             throw new Error('Strategy not found');
// OLD_ROUTER:           }
// OLD_ROUTER: 
// OLD_ROUTER:           // Extract epic from strategy tests - all tests should have the same epic
// OLD_ROUTER:           if (!strategy.tests || strategy.tests.length === 0) {
// OLD_ROUTER:             throw new Error('Strategy has no tests configured');
// OLD_ROUTER:           }
// OLD_ROUTER:           const epic = strategy.tests[0].epic;
// OLD_ROUTER:           if (!epic) {
// OLD_ROUTER:             throw new Error('Strategy tests do not have an epic configured');
// OLD_ROUTER:           }
// OLD_ROUTER: 
// OLD_ROUTER:           const config = {
// OLD_ROUTER:             epic,
// OLD_ROUTER:             start_date: input.startDate,
// OLD_ROUTER:             end_date: input.endDate,
// OLD_ROUTER:             initial_balance: input.initialBalance || 500,
// OLD_ROUTER:             monthly_topup: input.monthlyTopup || 100,
// OLD_ROUTER:             tests: strategy.tests,
// OLD_ROUTER:             conflict_resolution: strategy.conflictResolution,
// OLD_ROUTER:             calculation_mode: input.calculationMode || 'standard',
// OLD_ROUTER:             duplicate_tests: input.crashProtection ? 1 : 0,
// OLD_ROUTER:             rerun_duplicates: input.rerunDuplicates || false,
// OLD_ROUTER:           };
// OLD_ROUTER: 
// OLD_ROUTER:           console.log('[Strategy Test] Executing strategy with config:', JSON.stringify(config, null, 2));
// OLD_ROUTER:           const startTime = Date.now();
// OLD_ROUTER:           const result = await executeStrategy(config);
// OLD_ROUTER:           const executionTime = Date.now() - startTime;
// OLD_ROUTER:           console.log('[Strategy Test] Execution completed in', executionTime, 'ms');
// OLD_ROUTER: 
// OLD_ROUTER:           // Save strategy test result to database
// OLD_ROUTER:           const savedResult = await db.createStrategyTestResult({
// OLD_ROUTER:             userId: ctx.user?.id || 1,
// OLD_ROUTER:             strategyId: input.strategyId,
// OLD_ROUTER:             epic,
// OLD_ROUTER:             startDate: input.startDate,
// OLD_ROUTER:             endDate: input.endDate,
// OLD_ROUTER:             initialBalance: input.initialBalance || 500,
// OLD_ROUTER:             monthlyTopup: input.monthlyTopup || 100,
// OLD_ROUTER:             conflictResolution: strategy.conflictResolution.mode,
// OLD_ROUTER:             testCount: strategy.tests.length,
// OLD_ROUTER:             finalBalance: result.final_balance,
// OLD_ROUTER:             totalContributions: result.total_contributions,
// OLD_ROUTER:             totalReturn: result.total_return,
// OLD_ROUTER:             totalTrades: result.total_trades,
// OLD_ROUTER:             winningTrades: result.winning_trades,
// OLD_ROUTER:             losingTrades: result.losing_trades,
// OLD_ROUTER:             winRate: result.win_rate,
// OLD_ROUTER:             maxDrawdown: result.max_drawdown,
// OLD_ROUTER:             sharpeRatio: result.sharpe_ratio,
// OLD_ROUTER:             trades: result.trades || [],
// OLD_ROUTER:             dailyBalances: result.daily_balances || [],
// OLD_ROUTER:             calculationMode: input.calculationMode || 'standard',
// OLD_ROUTER:             executionTimeMs: executionTime,
// OLD_ROUTER:           });
// OLD_ROUTER: 
// OLD_ROUTER:           console.log('[Strategy Test] Result saved with ID:', savedResult);
// OLD_ROUTER:           return {
// OLD_ROUTER:             ...result,
// OLD_ROUTER:             resultId: savedResult,
// OLD_ROUTER:           };
// OLD_ROUTER:         } catch (error) {
// OLD_ROUTER:           console.error('[Strategy Test] Error:', error);
// OLD_ROUTER:           console.error('[Strategy Test] Stack:', error instanceof Error ? error.stack : 'No stack trace');
// OLD_ROUTER:           throw error;
// OLD_ROUTER:         }
// OLD_ROUTER:       }),
// OLD_ROUTER: 
// OLD_ROUTER:     delete: publicProcedure
// OLD_ROUTER:       .input(z.object({ id: z.number() }))
// OLD_ROUTER:       .mutation(async ({ input }) => {
// OLD_ROUTER:         await db.deleteStrategy(input.id);
// OLD_ROUTER:         return { success: true };
// OLD_ROUTER:       }),
// OLD_ROUTER: 
// OLD_ROUTER:     getRecentResults: publicProcedure
// OLD_ROUTER:       .input(z.object({ 
// OLD_ROUTER:         strategyId: z.number(),
// OLD_ROUTER:         limit: z.number().optional().default(10),
// OLD_ROUTER:       }))
// OLD_ROUTER:       .query(async ({ input }) => {
// OLD_ROUTER:         return await db.getStrategyTestResults(input.strategyId, input.limit);
// OLD_ROUTER:       }),
// OLD_ROUTER: 
// OLD_ROUTER:     getAllTestResults: publicProcedure
// OLD_ROUTER:       .query(async ({ ctx }) => {
// OLD_ROUTER:         return await db.getAllStrategyTestResults(ctx.user?.id || 1);
// OLD_ROUTER:       }),
// OLD_ROUTER: 
// OLD_ROUTER:     getTestResult: publicProcedure
// OLD_ROUTER:       .input(z.object({ id: z.number() }))
// OLD_ROUTER:       .query(async ({ input }) => {
// OLD_ROUTER:         return await db.getStrategyTestResult(input.id);
// OLD_ROUTER:       }),
// OLD_ROUTER: 
// OLD_ROUTER:     deleteTestResult: publicProcedure
// OLD_ROUTER:       .input(z.object({ id: z.number() }))
// OLD_ROUTER:       .mutation(async ({ input }) => {
// OLD_ROUTER:         await db.deleteStrategyTestResult(input.id);
// OLD_ROUTER:         return { success: true };
// OLD_ROUTER:       }),
// OLD_ROUTER:   }),

  // Bot Control
  botControl: router({
    start: publicProcedure
      .input(z.object({
        accountId: z.number(),
      }))
      .mutation(async ({ input }) => {
        await db.updateAccount(input.accountId, { isActive: true });
        // Refresh WebSocket subscriptions when bot is started
        await refreshWebSocketSubscriptions();
        return { success: true, message: 'Bot started' };
      }),

    stop: publicProcedure
      .input(z.object({
        accountId: z.number(),
      }))
      .mutation(async ({ input }) => {
        await db.updateAccount(input.accountId, { isActive: false });
        // Refresh WebSocket subscriptions when bot is stopped
        await refreshWebSocketSubscriptions();
        return { success: true, message: 'Bot stopped' };
      }),

    pause: publicProcedure
      .input(z.object({
        accountId: z.number(),
      }))
      .mutation(async ({ input }) => {
        await db.updateAccount(input.accountId, { isActive: false });
        // Refresh WebSocket subscriptions when bot is paused
        await refreshWebSocketSubscriptions();
        return { success: true, message: 'Bot paused' };
      }),

    resume: publicProcedure
      .input(z.object({
        accountId: z.number(),
      }))
      .mutation(async ({ input }) => {
        await db.updateAccount(input.accountId, { isActive: true });
        // Refresh WebSocket subscriptions when bot is resumed
        await refreshWebSocketSubscriptions();
        return { success: true, message: 'Bot resumed' };
      }),

    status: publicProcedure
      .input(z.object({
        accountId: z.number(),
      }))
      .query(async ({ input }) => {
        const account = await db.getAccount(input.accountId);
        if (!account) {
          throw new Error('Account not found');
        }
        return {
          isActive: account.isActive,
          balance: account.balance,
          lastSync: account.lastSync,
        };
      }),
  }),

  // Settings Management
  settings: router({
    list: publicProcedure.query(async () => {
      return await db.getAllSettings();
    }),

    getByCategory: publicProcedure
      .input(z.object({ category: z.string() }))
      .query(async ({ input }) => {
        return await db.getSettingsByCategory(input.category);
      }),

    upsert: publicProcedure
      .input(z.object({
        category: z.string(),
        key: z.string(),
        value: z.string(),
        valueType: z.enum(['string', 'number', 'boolean', 'json']).optional(),
        description: z.string().optional(),
        isSecret: z.boolean().optional(),
      }))
      .mutation(async ({ input }) => {
        await db.upsertSetting({
          category: input.category,
          key: input.key,
          value: input.value,
          valueType: input.valueType || 'string',
          description: input.description,
          isSecret: input.isSecret || false,
        });
        return { success: true };
      }),

    delete: publicProcedure
      .input(z.object({
        category: z.string(),
        key: z.string(),
      }))
      .mutation(async ({ input }) => {
        await db.deleteSetting(input.category, input.key);
        return { success: true };
      }),
    
    // API Statistics
    getApiStats: publicProcedure.query(async () => {
      const { apiStatsCollector } = await import('./orchestration/logger');
      return apiStatsCollector.getStats();
    }),
    
    resetApiStats: publicProcedure.mutation(async () => {
      const { apiStatsCollector } = await import('./orchestration/logger');
      apiStatsCollector.reset();
      return { success: true };
    }),
  }),
});

export type AppRouter = typeof appRouter;

