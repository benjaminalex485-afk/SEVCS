import { appState, setLatestSnapshot, registerAction, resolveAction } from './state.js';
import { events } from './events.js';

const BASE_URL = 'http://localhost:5001'; 
const POLL_INTERVAL = 300;
const MAX_BACKOFF = 3000;
const MIN_TIMEOUT = 1500;
const MAX_TIMEOUT = 5000;
let avgLatency = 300; // Moving average for adaptive timeout

// Global Listeners (Register Once)
events.on('EMA_RESET_REQUESTED', () => {
    avgLatency = 300;
});
const EMA_ALPHA = 0.2;

let currentRetryDelay = POLL_INTERVAL;
let consecutiveFailures = 0;
const MAX_PENDING = 5;
const FAIL_THRESHOLD = 3;

/**
 * Polling loop with exponential backoff and latency tracking.
 */
export async function startPolling() {
    const poll = async () => {
        if (appState.isSimulating) {
            setTimeout(poll, POLL_INTERVAL);
            return;
        }

        // Adaptive Timeout: clamp(avgLatency * 3, 1500, 5000)
        const currentTimeout = Math.min(MAX_TIMEOUT, Math.max(MIN_TIMEOUT, avgLatency * 3));
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), currentTimeout);
        const start = Date.now();

        try {
            const response = await fetch(`${BASE_URL}/api/status`, { signal: controller.signal });
            clearTimeout(timeoutId);

            if (!response.ok) throw new Error(`HTTP Error: ${response.status}`);
            
            const data = await response.json();
            let latency = Date.now() - start;
            
            // Filter Latency: Ignore < 10ms (unrealistic/local), cap at 5000ms
            if (latency >= 10) {
                latency = Math.min(5000, latency);
                // Update EMA Latency
                if (avgLatency === 0) avgLatency = latency;
                else avgLatency = (EMA_ALPHA * latency) + ((1 - EMA_ALPHA) * avgLatency);
            }

            setLatestSnapshot(data, latency);
            
            // Success: Reset metrics
            currentRetryDelay = POLL_INTERVAL;
            consecutiveFailures = 0;
            
            if (appState.uiState === 'DISCONNECTED' || appState.uiState === 'DEGRADED') {
                appState.uiState = 'SYNCHRONIZED';
                appState.stagnantCounter = 0;
            }
        } catch (error) {
            clearTimeout(timeoutId);
            const isTimeout = error.name === 'AbortError';
            consecutiveFailures++;
            
            console.error('[SEVCS API] Polling Failed:', isTimeout ? 'Timeout' : error.message);
            
            const errorObj = {
                code: isTimeout ? 'TIMEOUT' : 'NETWORK_ERROR',
                retryable: true,
                message: isTimeout ? 'Polling timed out' : error.message
            };

            // State Split: Timeout but responses exist -> DEGRADED, No response at all -> DISCONNECTED
            if (isTimeout && avgLatency > 0) {
                appState.uiState = 'DEGRADED';
            } else {
                appState.uiState = 'DISCONNECTED';
            }
            
            // Deterministic Jittered Backoff
            const jitter = (appState.lastSequence % 5) * 40; 
            currentRetryDelay = Math.min(currentRetryDelay * 2, MAX_BACKOFF) + jitter;

            if (consecutiveFailures >= FAIL_THRESHOLD && !appState.isSimulating) {
                events.emit('SIMULATION_TRIGGERED');
            }

            events.emit('API_ERROR', errorObj);
        }
        
        setTimeout(poll, currentRetryDelay);
    };

    poll();
}

/**
 * Execute a mutative action with deterministic bindings.
 */
const ACTION_TIMEOUT_MS = 10000;

export async function executeAction(endpoint, payload) {
    if (!appState.snapshot) {
        events.emit('API_ERROR', { code: 'NO_SNAPSHOT', retryable: false, message: 'No active snapshot to bind action' });
        return;
    }

    if (appState.pendingActions.size >= MAX_PENDING) {
        events.emit('API_ERROR', { code: 'THROTTLE', retryable: true, message: 'Too many pending requests' });
        return;
    }

    const requestId = crypto.randomUUID();
    const currentVersion = appState.snapshotVersion;
    const currentSequence = appState.lastSequence;

    registerAction(requestId, currentVersion, endpoint);

    let responseData = null;

    // Add timeout guard
    const timeout = setTimeout(() => {
        if (appState.pendingActions.has(requestId)) {
            resolveAction(requestId, { status: 'TIMEOUT', snapshot_version: currentVersion });
            events.emit('API_ERROR', { 
                code: 'TIMEOUT', 
                retryable: true,
                message: `Action ${endpoint} timed out` 
            });
        }
    }, ACTION_TIMEOUT_MS);

    try {
        const response = await fetch(`${BASE_URL}/api/${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                request_id: requestId,
                snapshot_version: currentVersion,
                snapshot_sequence: currentSequence,
                payload
            })
        });

        responseData = await response.json();

        // Hard Binding: Ignore response if it corresponds to an older version than current UI
        if (responseData.snapshot_version < appState.snapshotVersion) {
            console.warn(`[SEVCS] IGNORED STALE RESPONSE: ${responseData.snapshot_version} < ${appState.snapshotVersion}`);
            events.emit('API_ERROR', { code: 'STALE_RESPONSE', retryable: true, message: 'Response ignored (Snapshot out of date)' });
            return;
        }

        events.emit('ACTION_RESPONSE', { 
            requestId, 
            endpoint,
            status: responseData.status === 'OK' ? (responseData.replayed ? 'REPLAYED' : 'NEW') : 'REJECTED',
            snapshot_version: responseData.snapshot_version,
            snapshot_sequence: responseData.snapshot_sequence, // Bind to actual backend sequence
            decision_id: responseData.decision_id,
            error: responseData.error,
            payload: responseData 
        });

    } catch (error) {
        console.error(`[SEVCS API] Action ${endpoint} Failed:`, error);
        events.emit('API_ERROR', { code: 'ACTION_FAILED', retryable: false, message: error.message });
    } finally {
        clearTimeout(timeout);
        resolveAction(requestId, responseData);
    }
}

/**
 * Flush pending actions with optional scope filtering
 */
export function resetPendingActions(filter = {}) {
    console.warn('[SEVCS API] Scoped Action Reset:', filter);
    
    if (!filter.source) {
        appState.pendingActions.clear();
        return;
    }

    // Example scoping logic (requires request metadata in pendingActions)
    for (const [id, action] of appState.pendingActions.entries()) {
        if (action.source === filter.source) {
            appState.pendingActions.delete(id);
        }
    }
}
