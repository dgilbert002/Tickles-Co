/**
 * Window Detection Module
 * 
 * ============================================================================
 * ⚠️ LEGACY FILE - MOSTLY DEPRECATED
 * ============================================================================
 * 
 * This module was originally used for detecting execution windows from
 * strategyCombos (the old combo system). 
 * 
 * CURRENT STATUS:
 * - detectWindows() function: DELETED - was using wrong table (strategyCombos)
 * - validateWindowConfig(): STILL USED - for validation only
 * - getMarketName(): STILL USED - for generating human-readable window names
 * 
 * NEW SYSTEM:
 * - Window detection is now handled by:
 *   1. server/routes/strategies.ts::detectWindows() - at strategy creation
 *   2. server/services/window_resolver.ts - for DYNAMIC runtime resolution
 *   3. server/orchestration/dna_brain_calculator.ts::loadStrategyDNAStrands() - at execution
 * 
 * The new system uses epics.nextClose (dynamic) instead of static stored times.
 * ============================================================================
 */

// NOTE: getDb and strategyCombos imports removed - no longer needed
import { backtestResults, marketInfo } from '../../drizzle/schema';

export interface ExecutionWindow {
  closeTime: string;        // HH:MM:SS format (e.g., "16:00:00")
  marketName: string;       // e.g., "Regular Hours", "Extended Hours"
  epics: string[];          // Epics that close at this time
  allocationPct: number;    // Percentage of funds allocated to this window (0-100)
}

export interface WindowConfig {
  windows: ExecutionWindow[];
  mode: 'carry_over' | 'manual_split';
}

/**
 * ============================================================================
 * DELETED: detectWindows() function
 * ============================================================================
 * 
 * This function was removed because:
 * 1. It queried strategyCombos table which is the OLD combo system
 * 2. The accounts table now uses savedStrategies (via assignedStrategyId)
 * 3. Window detection is now handled by:
 *    - strategies.ts::detectWindows() for strategy creation
 *    - window_resolver.ts for DYNAMIC runtime resolution
 * 
 * ROLLBACK: If needed, check git history for the original function
 * Commit: a5281f6 (feat: Dynamic window resolution - fix stale close time bug)
 * ============================================================================
 */

/**
 * Determine market name based on close time
 * @param closeTime - Close time in HH:MM:SS format
 * @returns Human-readable market name
 */
function getMarketName(closeTime: string): string {
  // Parse hour from close time
  const hour = parseInt(closeTime.split(':')[0]);

  // US market hours (Eastern Time)
  if (hour === 16 || hour === 4) {
    return 'Regular Hours (4:00 PM ET)';
  } else if (hour === 20 || hour === 8) {
    return 'Extended Hours (8:00 PM ET)';
  } else if (hour === 13 || hour === 1) {
    return 'Early Close (1:00 PM ET)';
  } else {
    return `Custom (${closeTime})`;
  }
}

/**
 * Validate window configuration
 * @param windowConfig - Window configuration to validate
 * @returns true if valid, throws error otherwise
 */
export function validateWindowConfig(windowConfig: WindowConfig): boolean {
  if (!windowConfig.windows || windowConfig.windows.length === 0) {
    throw new Error('Window configuration must have at least one window');
  }

  // Check total allocation doesn't exceed 99% (for manual split mode)
  if (windowConfig.mode === 'manual_split') {
    const totalAllocation = windowConfig.windows.reduce((sum, w) => sum + w.allocationPct, 0);
    if (totalAllocation > 99) {
      throw new Error(`Total allocation (${totalAllocation}%) exceeds 99%`);
    }
  }

  // Check each window has valid data
  for (const window of windowConfig.windows) {
    // Accept both HH:MM and HH:MM:SS formats, normalize to HH:MM:SS
    if (!window.closeTime) {
      throw new Error(`Missing close time for window`);
    }
    
    // Check if it's HH:MM format and convert to HH:MM:SS
    if (window.closeTime.match(/^\d{2}:\d{2}$/)) {
      window.closeTime = window.closeTime + ':00';
      console.log(`[WindowDetection] Normalized close time to: ${window.closeTime}`);
    }
    
    // Now validate it's in HH:MM:SS format
    if (!window.closeTime.match(/^\d{2}:\d{2}:\d{2}$/)) {
      throw new Error(`Invalid close time format: ${window.closeTime} (expected HH:MM:SS)`);
    }
    
    if (!window.epics || window.epics.length === 0) {
      throw new Error(`Window ${window.closeTime} has no epics`);
    }
    if (window.allocationPct < 0 || window.allocationPct > 100) {
      throw new Error(`Invalid allocation percentage for window ${window.closeTime}: ${window.allocationPct}%`);
    }
  }

  return true;
}
