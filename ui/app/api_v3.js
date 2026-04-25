import { appState, setLatestSnapshot, registerAction, resolveAction } from './state_v3.js';
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

let pollingInterval = null;

/**
 * Centralized Fetch Wrapper with Auth & 401 Handling
 */
async function safeFetch(url, options = {}) {
    const headers = options.headers || {};
    if (appState.session.token) {
        headers['Authorization'] = `Bearer ${appState.session.token}`;
    }

    const response = await fetch(url, { ...options, headers });

    if (response.status === 401) {
        console.error('[SEVCS API] Unauthorized (401). Triggering Logout.');
        events.emit('FORCE_LOGOUT');
        return null;
    }

    if (!response.ok) {
        const error = await response.json().catch(() => ({ message: response.statusText }));
        throw new Error(error.message || `HTTP Error: ${response.status}`);
    }

    return response.json();
}

/**
 * Authentication Layer (Independent of Snapshot Pipeline)
 */
export async function login(credentials) {
    try {
        const data = await safeFetch(`${BASE_URL}/api/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(credentials)
        });

        if (data && data.token) {
            appState.session = { token: data.token, userId: data.user_id, role: data.role || 'USER' };
            appState.uiMode = data.role || 'USER'; 
            appState.authStatus = 'AUTHENTICATED_PENDING';
            localStorage.setItem('sevcs_token', data.token);
            startPolling();
            return { success: true };
        }
        return { success: false, message: data ? data.message : 'Login failed' };
    } catch (error) {
        return { success: false, message: error.message };
    }
}

export async function signup(profile) {
    try {
        const data = await safeFetch(`${BASE_URL}/api/signup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(profile)
        });

        if (data && data.success) {
            return { success: true };
        }
        return { success: false, message: data ? data.message : 'Signup failed' };
    } catch (error) {
        return { success: false, message: error.message };
    }
}

/**
 * Polling loop with exponential backoff and latency tracking.
 */
/**
 * Polling loop with single-instance guard and auth gate.
 */
export async function startPolling() {
    if (pollingInterval) return;

    const fetchStatus = async () => {
        if (appState.authStatus === 'GUEST' || appState.isSimulating) return;

        const currentTimeout = Math.min(MAX_TIMEOUT, Math.max(MIN_TIMEOUT, avgLatency * 3));
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), currentTimeout);
        const start = Date.now();

        try {
            const data = await safeFetch(`${BASE_URL}/api/status`, { signal: controller.signal });
            clearTimeout(timeoutId);
            if (!data) return;

            // Overlap Guard: Reject if sequence is old
            if (appState.lastSequence !== -1 && data.snapshot_sequence <= appState.lastSequence) {
                return;
            }

            let latency = Date.now() - start;
            if (latency >= 10) {
                latency = Math.min(5000, latency);
                avgLatency = (EMA_ALPHA * latency) + ((1 - EMA_ALPHA) * avgLatency);
            }

            setLatestSnapshot(data, latency);
            
            currentRetryDelay = POLL_INTERVAL;
            consecutiveFailures = 0;
        } catch (error) {
            clearTimeout(timeoutId);
            const isTimeout = error.name === 'AbortError';
            consecutiveFailures++;
            
            const errorObj = {
                code: isTimeout ? 'TIMEOUT' : 'NETWORK_ERROR',
                retryable: true,
                message: isTimeout ? 'Polling timed out' : error.message
            };

            if (isTimeout && avgLatency > 0) appState.uiState = 'DEGRADED';
            else appState.uiState = 'DISCONNECTED';
            
            const jitter = (appState.lastSequence % 5) * 40; 
            currentRetryDelay = Math.min(currentRetryDelay * 2, MAX_BACKOFF) + jitter;

            events.emit('API_ERROR', errorObj);
        }
    };

    pollingInterval = setInterval(fetchStatus, POLL_INTERVAL);
}

export function stopPolling() {
    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }
}

events.on('STOP_POLLING', stopPolling);
events.on('RESYNC_STARTED', startPolling);

/**
 * Execute a mutative action with deterministic bindings.
 */
const ACTION_TIMEOUT_MS = appState.INTENT_TIMEOUT_MS;

export async function executeAction(endpoint, payload, intentKey = null) {
    if (!appState.allowActions) {
        events.emit('API_ERROR', { code: 'NOT_SYNCHRONIZED', retryable: false, message: 'System not synchronized or action disallowed' });
        return;
    }

    // Intent Lock Guard
    if (intentKey && appState.pendingIntents.has(intentKey)) {
        const intent = appState.pendingIntents.get(intentKey);
        if (intent.status === 'PENDING') return; 
    }

    if (appState.pendingActions.size >= MAX_PENDING) {
        events.emit('API_ERROR', { code: 'THROTTLE', retryable: true, message: 'Too many pending requests' });
        return;
    }

    // Click-Time Version Binding
    const versionAtClick = appState.snapshotVersion;
    const requestId = crypto.randomUUID();

    registerAction(requestId, versionAtClick, endpoint, intentKey);

    try {
        const data = await safeFetch(`${BASE_URL}/api/${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                request_id: requestId,
                snapshot_version: versionAtClick,
                snapshot_sequence: appState.lastSequence,
                payload
            })
        });

        if (!data) return;

        // Freeze Race Guard: Check before resolving
        if (appState.snapshot.freeze_state) {
            console.warn('[SEVCS API] Response received during FREEZE. Marking as UNKNOWN.');
            resolveAction(requestId, { ...data, status: 'UNKNOWN', error: 'System frozen during completion' });
            return;
        }

        events.emit('ACTION_RESPONSE', { 
            requestId, 
            endpoint,
            status: data.status === 'OK' ? (data.replayed ? 'REPLAYED' : 'NEW') : 'REJECTED',
            snapshot_version: data.snapshot_version,
            snapshot_sequence: data.snapshot_sequence,
            payload: data 
        });

        resolveAction(requestId, data);

    } catch (error) {
        console.error(`[SEVCS API] Action ${endpoint} Failed:`, error);
        events.emit('API_ERROR', { code: 'ACTION_FAILED', retryable: false, message: error.message });
        resolveAction(requestId, { status: 'ERROR', error: error.message });
    }
}

export async function bookSlot(slotId) {
    return executeAction('book_slot', { slot_id: slotId }, `book_slot_${slotId}`);
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
