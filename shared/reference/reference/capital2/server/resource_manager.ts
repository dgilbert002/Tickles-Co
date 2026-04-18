import os from 'os';
// Using console for logging

/**
 * Core allocation information
 */
export interface CoreAllocation {
  coreId: number;
  runId: number;
  testNum: number;
  indicator: string;
  pid: number | null;
  status: 'allocated' | 'running' | 'completed' | 'failed';
  startTime: Date;
  endTime?: Date;
  errorMessage?: string;
  completedTests: number;  // Track how many tests this core has completed
  lastActivityTime: Date;  // Track last time this core did something
}

/**
 * Core status for monitoring
 */
export interface CoreStatus {
  coreId: number;
  available: boolean;
  allocation: CoreAllocation | null;
}

/**
 * Pending allocation request in the queue
 */
interface AllocationRequest {
  runId: number;
  requestedCores: number;
  timestamp: Date;
  resolve: (cores: number[]) => void;
  reject: (error: Error) => void;
  timeoutId?: NodeJS.Timeout;
}

/**
 * Queue status for monitoring
 */
export interface QueueStatus {
  position: number;
  runId: number;
  requestedCores: number;
  waitTime: number;  // seconds
}

/**
 * Centralized Resource Manager for CPU core allocation
 * Tracks and manages core assignments across all backtests
 */
class ResourceManager {
  private totalCores: number;
  private coreAllocations: Map<number, CoreAllocation>;
  private requestQueue: AllocationRequest[];
  private readonly DEFAULT_TIMEOUT_MS = 300000;  // 5 minutes
  
  constructor() {
    this.totalCores = os.cpus().length;
    this.coreAllocations = new Map();
    this.requestQueue = [];
    
    console.log(`[ResourceManager] Initialized with ${this.totalCores} CPU cores`);
  }
  
  /**
   * Get total number of CPU cores in the system
   */
  getTotalCores(): number {
    return this.totalCores;
  }
  
  /**
   * Get number of available (free) cores
   */
  getAvailableCores(): number {
    let available = 0;
    for (let i = 0; i < this.totalCores; i++) {
      if (!this.coreAllocations.has(i)) {
        available++;
      }
    }
    return available;
  }
  
  /**
   * Get list of available core IDs
   */
  getAvailableCoreIds(): number[] {
    const available: number[] = [];
    for (let i = 0; i < this.totalCores; i++) {
      if (!this.coreAllocations.has(i)) {
        available.push(i);
      }
    }
    return available;
  }
  
  /**
   * Get status of all cores
   */
  getAllCoreStatus(): CoreStatus[] {
    const status: CoreStatus[] = [];
    for (let i = 0; i < this.totalCores; i++) {
      const allocation = this.coreAllocations.get(i) || null;
      status.push({
        coreId: i,
        available: !this.coreAllocations.has(i),
        allocation,
      });
    }
    return status;
  }
  
  /**
   * Get status of cores allocated to a specific backtest run
   */
  getRunCoreStatus(runId: number): CoreStatus[] {
    const status: CoreStatus[] = [];
    for (let i = 0; i < this.totalCores; i++) {
      const allocation = this.coreAllocations.get(i);
      if (allocation && allocation.runId === runId) {
        status.push({
          coreId: i,
          available: false,
          allocation,
        });
      }
    }
    return status;
  }
  
  /**
   * Get queue status for monitoring
   */
  getQueueStatus(): QueueStatus[] {
    return this.requestQueue.map((req, index) => ({
      position: index + 1,
      runId: req.runId,
      requestedCores: req.requestedCores,
      waitTime: (Date.now() - req.timestamp.getTime()) / 1000,
    }));
  }
  
  /**
   * Allocate cores immediately (synchronous, for backward compatibility)
   * Returns array of allocated core IDs, or empty array if not enough cores available
   */
  allocateCoresSync(runId: number, requestedCores: number): number[] {
    const availableCoreIds = this.getAvailableCoreIds();
    
    // Return empty array only if NO cores available
    if (availableCoreIds.length === 0) {
      console.warn(`[ResourceManager] Cannot allocate any cores for run ${runId}, all ${this.totalCores} cores are busy`);
      return [];
    }
    
    // Allocate as many cores as possible (partial allocation)
    const coresToAllocate = Math.min(requestedCores, availableCoreIds.length);
    const allocatedCores = availableCoreIds.slice(0, coresToAllocate);
    
    if (coresToAllocate < requestedCores) {
      console.warn(`[ResourceManager] Partial allocation for run ${runId}: requested ${requestedCores}, allocated ${coresToAllocate}`);
    }
    
    // Reserve the cores (mark as allocated but not yet running)
    for (const coreId of allocatedCores) {
      this.coreAllocations.set(coreId, {
        coreId,
        runId,
        testNum: -1, // Will be updated when test actually starts
        indicator: 'pending',
        pid: null,
        status: 'allocated',
        startTime: new Date(),
        completedTests: 0,
        lastActivityTime: new Date(),
      });
    }
    
    console.log(`[ResourceManager] Allocated cores ${allocatedCores.join(', ')} to run ${runId}`);
    
    return allocatedCores;
  }
  
  /**
   * Allocate cores with queuing support (async)
   * Waits for cores to become available if none are free
   * @param runId - Backtest run ID
   * @param requestedCores - Number of cores requested
   * @param timeoutMs - Maximum wait time in milliseconds (default: 5 minutes)
   * @returns Promise that resolves with allocated core IDs
   */
  async allocateCores(runId: number, requestedCores: number, timeoutMs?: number): Promise<number[]> {
    const timeout = timeoutMs || this.DEFAULT_TIMEOUT_MS;
    
    // Try immediate allocation first
    const immediate = this.allocateCoresSync(runId, requestedCores);
    if (immediate.length > 0) {
      return immediate;
    }
    
    // No cores available, queue the request
    console.log(`[ResourceManager] Run ${runId} queued (requested ${requestedCores} cores, queue position ${this.requestQueue.length + 1})`);
    
    return new Promise<number[]>((resolve, reject) => {
      const request: AllocationRequest = {
        runId,
        requestedCores,
        timestamp: new Date(),
        resolve,
        reject,
      };
      
      // Set timeout
      request.timeoutId = setTimeout(() => {
        // Remove from queue
        const index = this.requestQueue.indexOf(request);
        if (index !== -1) {
          this.requestQueue.splice(index, 1);
        }
        reject(new Error(`Timeout waiting for cores (waited ${timeout / 1000}s)`));
      }, timeout);
      
      this.requestQueue.push(request);
    });
  }
  
  /**
   * Process the queue and allocate cores to waiting requests
   */
  private processQueue(): void {
    if (this.requestQueue.length === 0) {
      return;
    }
    
    const availableCoreIds = this.getAvailableCoreIds();
    if (availableCoreIds.length === 0) {
      return;  // No cores to allocate
    }
    
    console.log(`[ResourceManager] Processing queue (${this.requestQueue.length} waiting, ${availableCoreIds.length} cores available)`);
    
    // Process requests in FIFO order
    let coresRemaining = availableCoreIds.length;
    const toRemove: AllocationRequest[] = [];
    
    for (const request of this.requestQueue) {
      if (coresRemaining === 0) {
        break;  // No more cores to allocate
      }
      
      // Try to allocate cores for this request
      const allocated = this.allocateCoresSync(request.runId, request.requestedCores);
      
      if (allocated.length > 0) {
        // Clear timeout
        if (request.timeoutId) {
          clearTimeout(request.timeoutId);
        }
        
        const waitTime = (Date.now() - request.timestamp.getTime()) / 1000;
        console.log(`[ResourceManager] Dequeued run ${request.runId} after ${waitTime.toFixed(1)}s wait (allocated ${allocated.length} cores)`);
        
        // Resolve the promise
        request.resolve(allocated);
        toRemove.push(request);
        
        coresRemaining -= allocated.length;
      }
    }
    
    // Remove processed requests from queue
    for (const request of toRemove) {
      const index = this.requestQueue.indexOf(request);
      if (index !== -1) {
        this.requestQueue.splice(index, 1);
      }
    }
  }
  
  /**
   * Update core allocation when a test starts running
   */
  updateCoreTest(coreId: number, testNum: number, indicator: string, pid: number): void {
    const allocation = this.coreAllocations.get(coreId);
    if (!allocation) {
      console.error(`[ResourceManager] Cannot update core ${coreId}, not allocated`);
      return;
    }
    
    allocation.testNum = testNum;
    allocation.indicator = indicator;
    allocation.pid = pid;
    allocation.status = 'running';
    allocation.lastActivityTime = new Date();
    
    // Reset startTime when starting a new test (for accurate duration tracking)
    allocation.startTime = new Date();
    
    console.log(`[ResourceManager] Core ${coreId} running test #${testNum} (${indicator}) PID ${pid} (completed: ${allocation.completedTests})`);
  }
  
  /**
   * Mark a core's test as completed
   */
  completeCore(coreId: number): void {
    const allocation = this.coreAllocations.get(coreId);
    if (!allocation) {
      console.error(`[ResourceManager] Cannot complete core ${coreId}, not allocated`);
      return;
    }
    
    allocation.status = 'completed';
    allocation.endTime = new Date();
    allocation.completedTests++;
    allocation.lastActivityTime = new Date();
    
    const duration = allocation.endTime.getTime() - allocation.startTime.getTime();
    console.log(`[ResourceManager] Core ${coreId} completed test #${allocation.testNum} in ${(duration / 1000).toFixed(2)}s (total: ${allocation.completedTests})`);
  }
  
  /**
   * Mark a core's test as failed
   */
  failCore(coreId: number, errorMessage: string): void {
    const allocation = this.coreAllocations.get(coreId);
    if (!allocation) {
      console.error(`[ResourceManager] Cannot fail core ${coreId}, not allocated`);
      return;
    }
    
    allocation.status = 'failed';
    allocation.endTime = new Date();
    allocation.errorMessage = errorMessage;
    
    console.error(`[ResourceManager] Core ${coreId} failed test #${allocation.testNum}: ${errorMessage}`);
  }
  
  /**
   * Release a specific core (make it available again)
   */
  releaseCore(coreId: number): void {
    const allocation = this.coreAllocations.get(coreId);
    if (!allocation) {
      console.warn(`[ResourceManager] Core ${coreId} already released`);
      return;
    }
    
    this.coreAllocations.delete(coreId);
    console.log(`[ResourceManager] Released core ${coreId} (was running test #${allocation.testNum})`);
  }
  
  /**
   * Release all cores allocated to a specific backtest run
   */
  releaseRunCores(runId: number): number {
    let releasedCount = 0;
    
    const entries = Array.from(this.coreAllocations.entries());
    for (const [coreId, allocation] of entries) {
      if (allocation.runId === runId) {
        this.coreAllocations.delete(coreId);
        releasedCount++;
      }
    }
    
    if (releasedCount > 0) {
      console.log(`[ResourceManager] Released ${releasedCount} cores from run ${runId}`);
      
      // Process queue to allocate freed cores to waiting requests
      this.processQueue();
    }
    
    return releasedCount;
  }
  
  /**
   * Get allocation info for a specific core
   */
  getCoreAllocation(coreId: number): CoreAllocation | null {
    return this.coreAllocations.get(coreId) || null;
  }
}

// Singleton instance
export const resourceManager = new ResourceManager();

