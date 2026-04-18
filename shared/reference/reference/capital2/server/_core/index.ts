import "dotenv/config";
import express from "express";
import { createServer } from "http";
import net from "net";
import { createExpressMiddleware } from "@trpc/server/adapters/express";
import { registerOAuthRoutes } from "./oauth";
import { appRouter } from "../routers";
import { createContext } from "./context";
import { serveStatic, setupVite } from "./vite";
import { recoverStuckBacktests } from "../db";
import { runStartupTasks } from "../orchestration/startup_service";
import liveTradingRoutes from "../routes/live_trading";
import logsRoutes from "../routes/logs";
import { orchestrationLogger } from "../orchestration/logger";
import { killPorts } from "./port_manager";
import { ensureSingleInstance } from "./single_instance";
import { 
  handleUncaughtException, 
  handleUnhandledRejection, 
  startErrorCountCleanup,
  registerService,
  reportServiceHealthy,
  exitWithRestart,
} from "./resilience";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { dirname } from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const ERROR_LOG_PATH = path.join(__dirname, '../../logs/server_errors.log');

// Ensure logs directory exists
if (!fs.existsSync(path.dirname(ERROR_LOG_PATH))) {
  fs.mkdirSync(path.dirname(ERROR_LOG_PATH), { recursive: true });
}

// Log error to file
function logError(error: any, context?: any) {
  const timestamp = new Date().toISOString();
  const logEntry = {
    timestamp,
    error: {
      message: error.message,
      stack: error.stack,
      name: error.name,
    },
    context,
  };
  
  const logLine = JSON.stringify(logEntry) + '\n';
  fs.appendFileSync(ERROR_LOG_PATH, logLine);
  console.error(`[ERROR] ${timestamp}`, error);
  if (context) {
    console.error('[ERROR Context]', context);
  }
}

function isPortAvailable(port: number): Promise<boolean> {
  return new Promise(resolve => {
    const server = net.createServer();
    server.listen(port, () => {
      server.close(() => resolve(true));
    });
    server.on("error", () => resolve(false));
  });
}

async function findAvailablePort(startPort: number = 3000): Promise<number> {
  for (let port = startPort; port < startPort + 20; port++) {
    if (await isPortAvailable(port)) {
      return port;
    }
  }
  throw new Error(`No available port found starting from ${startPort}`);
}

async function startServer() {
  // Ensure only one server instance runs at a time
  // This will kill any existing instance or fail if FORCE_SINGLE_INSTANCE is not set
  const forceKill = process.env.FORCE_SINGLE_INSTANCE !== "false";
  const preferredPort = parseInt(process.env.PORT || "3001");
  
  console.log("[Server] Starting single-instance check...");
  await ensureSingleInstance(preferredPort, forceKill);
  // Note: ensureSingleInstance already handles killing processes on the port

  const app = express();
  const server = createServer(app);
  // Configure body parser with larger size limit for file uploads
  app.use(express.json({ limit: "50mb" }));
  app.use(express.urlencoded({ limit: "50mb", extended: true }));
  // OAuth callback under /api/oauth/callback
  registerOAuthRoutes(app);
  // Live Trading REST API
  app.use("/api/live-trading", liveTradingRoutes);
  // Logs API
  app.use("/api/logs", logsRoutes);
  // tRPC API
  app.use(
    "/api/trpc",
    createExpressMiddleware({
      router: appRouter,
      createContext,
      onError({ error, type, path, input, ctx }) {
        logError(error, {
          type,
          path,
          input,
          user: ctx?.user ? { id: ctx.user.id, email: ctx.user.email } : null,
        });
      },
    })
  );
  // development mode uses Vite, production mode uses static files
  if (process.env.NODE_ENV === "development") {
    await setupVite(app, server);
  } else {
    serveStatic(app);
  }

  // Use the preferred port (already ensured available by single instance guard)
  const port = preferredPort;

  server.listen(port, async () => {
    console.log(`Server running on http://localhost:${port}/`);
    
    // Recover stuck backtests from previous server crashes
    try {
      const recovered = await recoverStuckBacktests();
      if (recovered > 0) {
        console.log(`[Recovery] Recovered ${recovered} stuck backtest(s)`);
      } else {
        console.log('[Recovery] No stuck backtests found');
      }
    } catch (error) {
      console.error('[Recovery] Failed to recover stuck backtests:', error);
    }

    // Run startup tasks (market hours initialization, etc.)
    try {
      await runStartupTasks();
    } catch (error) {
      console.error('[Startup] Failed to run startup tasks:', error);
    }
  });
}

// Global uncaught exception handler with resilience
process.on('uncaughtException', async (error) => {
  logError(error, { type: 'uncaughtException' });
  
  // Use resilience service to decide if we should exit
  // If fatal, resilience service will spawn restart script and exit after delay
  const shouldContinue = await handleUncaughtException(error);
  
  if (shouldContinue) {
    console.warn('[RECOVERED] Uncaught Exception (recoverable) - Server continuing:', error.message);
  }
  // Note: If !shouldContinue, resilience service already called exitWithRestart()
  // which spawns the restart script and exits after 2 seconds
});

// Global unhandled rejection handler with resilience  
process.on('unhandledRejection', async (reason, promise) => {
  logError(reason, { type: 'unhandledRejection', promise });
  
  // Use resilience service to decide if we should exit
  // If fatal, resilience service will spawn restart script and exit after delay
  const shouldContinue = await handleUnhandledRejection(reason);
  
  if (shouldContinue) {
    console.warn('[RECOVERED] Unhandled Rejection (recoverable) - Server continuing:', 
      reason instanceof Error ? reason.message : String(reason));
  }
  // Note: If !shouldContinue, resilience service already called exitWithRestart()
  // which spawns the restart script and exits after 2 seconds
});

// Start error count cleanup (clears old error tracking data)
startErrorCountCleanup();

// Graceful shutdown handlers
process.on('SIGTERM', () => {
  console.log('[Server] Received SIGTERM, shutting down gracefully...');
  process.exit(0);
});

process.on('SIGINT', () => {
  console.log('[Server] Received SIGINT, shutting down gracefully...');
  process.exit(0);
});

startServer().catch((error) => {
  logError(error, { type: 'serverStartup' });
  console.error('[FATAL] Server startup failed:', error);
  exitWithRestart(`Server startup failed: ${error.message || error}`);
});
