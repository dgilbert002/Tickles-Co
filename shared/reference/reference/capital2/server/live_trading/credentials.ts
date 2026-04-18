/**
 * Capital.com Credentials Management
 * 
 * Handles loading and managing Capital.com API credentials from settings.
 * 
 * IMPORTANT: Uses client caching to avoid re-authenticating on every API call.
 * Capital.com rate-limits POST /session to 1 request/second.
 * By caching clients, we authenticate once and reuse for subsequent calls.
 */

import { getSetting } from '../db';
import { CapitalComAPI } from './capital_api';

export interface CapitalCredentials {
  email: string;
  password: string;
  apiKey: string;
  environment: 'demo' | 'live';
}

/**
 * Client cache to avoid re-authenticating on every operation
 * Key: 'demo' | 'live'
 * Value: { client, createdAt, lastUsed }
 */
interface CachedClient {
  client: CapitalComAPI;
  createdAt: Date;
  lastUsed: Date;
}

const clientCache: Map<string, CachedClient> = new Map();

// Cache expiry: 5 minutes (Capital.com sessions last ~10 minutes)
const CACHE_EXPIRY_MS = 5 * 60 * 1000;

/**
 * Load Capital.com credentials from database settings
 */
export async function loadCapitalCredentials(): Promise<CapitalCredentials | null> {
  try {
    const email = await getSetting('credentials', 'email');
    const password = await getSetting('credentials', 'password');
    const apiKey = await getSetting('credentials', 'api_key');
    const environment = await getSetting('api_config', 'environment');

    if (!email || !password || !apiKey) {
      console.error('[Credentials] Missing required Capital.com credentials');
      return null;
    }

    return {
      email: email.value,
      password: password.value,
      apiKey: apiKey.value,
      environment: (environment?.value === 'live' ? 'live' : 'demo') as 'demo' | 'live',
    };
  } catch (error) {
    console.error('[Credentials] Error loading credentials:', error);
    return null;
  }
}

/**
 * Get a cached client if available and not expired
 */
function getCachedClient(environment: 'demo' | 'live'): CapitalComAPI | null {
  const cached = clientCache.get(environment);
  
  if (!cached) {
    return null;
  }
  
  const age = Date.now() - cached.createdAt.getTime();
  
  if (age > CACHE_EXPIRY_MS) {
    console.log(`[Credentials] Cached ${environment} client expired (${(age / 1000).toFixed(0)}s old), will re-authenticate`);
    clientCache.delete(environment);
    return null;
  }
  
  // Update last used time
  cached.lastUsed = new Date();
  
  console.log(`[Credentials] Reusing cached ${environment} client (${(age / 1000).toFixed(0)}s old)`);
  return cached.client;
}

/**
 * Cache a client for reuse
 */
function cacheClient(environment: 'demo' | 'live', client: CapitalComAPI): void {
  clientCache.set(environment, {
    client,
    createdAt: new Date(),
    lastUsed: new Date(),
  });
  console.log(`[Credentials] Cached ${environment} client for reuse`);
}

/**
 * Clear cached client (call when auth fails or on logout)
 */
export function clearCachedClient(environment?: 'demo' | 'live'): void {
  if (environment) {
    clientCache.delete(environment);
    console.log(`[Credentials] Cleared cached ${environment} client`);
  } else {
    clientCache.clear();
    console.log('[Credentials] Cleared all cached clients');
  }
}

/**
 * Get cache status (for debugging)
 */
export function getCacheStatus(): { demo: boolean; live: boolean; demoAge?: number; liveAge?: number } {
  const demoCache = clientCache.get('demo');
  const liveCache = clientCache.get('live');
  
  return {
    demo: !!demoCache,
    live: !!liveCache,
    demoAge: demoCache ? Date.now() - demoCache.createdAt.getTime() : undefined,
    liveAge: liveCache ? Date.now() - liveCache.createdAt.getTime() : undefined,
  };
}

/**
 * Create or get cached Capital.com API client with credentials from settings
 * 
 * This function caches authenticated clients to avoid hitting the 1/second
 * rate limit on POST /session. Clients are cached for 5 minutes.
 * 
 * @param environment Override environment (demo/live), defaults to settings value
 * @param forceNew Force creating a new client (bypasses cache)
 */
export async function createCapitalAPIClient(
  environment?: 'demo' | 'live',
  forceNew: boolean = false
): Promise<CapitalComAPI | null> {
  const credentials = await loadCapitalCredentials();
  
  if (!credentials) {
    console.error('[Credentials] Cannot create API client without credentials');
    return null;
  }

  // Use provided environment or fall back to credentials environment
  const targetEnvironment = environment || credentials.environment;

  // Check cache first (unless forceNew is true)
  if (!forceNew) {
    const cached = getCachedClient(targetEnvironment);
    if (cached) {
      return cached;
    }
  }

  // Create new client
  console.log(`[Credentials] Creating new ${targetEnvironment} API client...`);
  
  const api = new CapitalComAPI(
    {
      email: credentials.email,
      password: credentials.password,
      apiKey: credentials.apiKey,
    },
    targetEnvironment
  );

  // Authenticate immediately
  const authenticated = await api.authenticate();
  
  if (!authenticated) {
    console.error('[Credentials] Failed to authenticate with Capital.com');
    return null;
  }

  // Cache the authenticated client
  cacheClient(targetEnvironment, api);

  console.log(`[Credentials] Successfully created and authenticated API client (${targetEnvironment})`);
  return api;
}

/**
 * Get demo and live account IDs from settings
 */
export async function getKnownAccountIds(): Promise<{ demo: string | null; live: string | null }> {
  try {
    const demoAccount = await getSetting('env_accounts', 'demo');
    const liveAccount = await getSetting('env_accounts', 'live');

    return {
      demo: demoAccount?.value || null,
      live: liveAccount?.value || null,
    };
  } catch (error) {
    console.error('[Credentials] Error loading account IDs:', error);
    return { demo: null, live: null };
  }
}

