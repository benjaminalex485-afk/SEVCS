import { appState, setLatestSnapshot, registerAction, resolveAction } from './state_v3.js';
import { events } from './events.js';

const BASE_URL = window.location.origin; // Use the ESP32 as a proxy gateway
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
const fetchStatus = async () => {
    if (appState.authStatus === 'GUEST' || appState.isSimulating) return;

    const currentTimeout = Math.min(MAX_TIMEOUT, Math.max(MIN_TIMEOUT, avgLatency * 3));
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), currentTimeout);
    const start = Date.now();

    try {
        const url = new URL(`${BASE_URL}/api/status`);
        if (appState.session.userId) {
            url.searchParams.append('username', appState.session.userId);
        }
        const data = await safeFetch(url.toString(), { signal: controller.signal });
        clearTimeout(timeoutId);
        if (!data) return;

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

        if (isTimeout && avgLatency > 0) {
            // Derived health will handle this
        } else {
            // Derived health will handle this
        }
        
        const jitter = (appState.lastSequence % 5) * 40; 
        currentRetryDelay = Math.min(currentRetryDelay * 2, MAX_BACKOFF) + jitter;

        events.emit('API_ERROR', errorObj);
    }
};

let isPolling = false;

export async function startPolling() {
    if (isPolling) return;
    isPolling = true;

    async function pollLoop() {
        if (!isPolling) return;
        await fetchStatus();
        pollingInterval = setTimeout(pollLoop, currentRetryDelay);
    }
    pollLoop();
}

export function stopPolling() {
    isPolling = false;
    if (pollingInterval) {
        clearTimeout(pollingInterval);
        pollingInterval = null;
    }
    console.log('[SEVCS API] Polling STOPPED');
}

export async function resync() {
    console.log('[SEVCS API] Initiating Resync...');
    try {
        await fetchStatus();
        console.log('[SEVCS API] Resync SUCCESS');
    } catch (e) {
        console.error('[SEVCS API] Resync FAILED:', e);
        throw e;
    }
}

events.on('STOP_POLLING', stopPolling);
events.on('RESYNC_STARTED', () => {
    stopPolling();
    resync().then(() => startPolling());
});

/**
 * Execute a mutative action with deterministic bindings.
 */
const ACTION_TIMEOUT_MS = appState.INTENT_TIMEOUT_MS;

export async function executeAction(endpoint, payload, intentKey = null) {
    // 0. Policy Guard: Allow financial actions even during desync
    const isCriticalVisionAction = !['recharge', 'login', 'signup'].includes(endpoint);
    
    if (isCriticalVisionAction && !appState.allowActions) {
        console.warn(`[SEVCS API] Action ${endpoint} blocked: System not synchronized.`);
        events.emit('API_ERROR', { 
            code: 'NOT_SYNCHRONIZED', 
            retryable: false, 
            message: 'System not synchronized. Please wait for health indicator to turn Green.' 
        });
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
    const requestId = `req_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

    console.log(`[SEVCS API] Executing ${endpoint} (ID: ${requestId})`);
    registerAction(requestId, versionAtClick, endpoint, intentKey);

    // Simulation Intercept (Admin Infrastructure)
    if (appState.isSimulating) {
        let success = false;
        if (endpoint === 'admin_add_slot') {
            const newId = appState.simSlots.length > 0 ? Math.max(...appState.simSlots.map(s => s.slot_id)) + 1 : 1;
            appState.simSlots.push({ slot_id: newId, state: 'FREE', charger_type: payload.charger_type || 'STANDARD', assigned_global_id: null });
            success = true;
        } else if (endpoint === 'admin_remove_slot') {
            appState.simSlots = appState.simSlots.filter(s => s.slot_id != payload.slot_id);
            success = true;
        } else if (endpoint === 'admin_update_slot_type') {
            const slot = appState.simSlots.find(s => s.slot_id == payload.slot_id);
            if (slot) {
                slot.charger_type = payload.charger_type;
                success = true;
            }
        }

        if (success) {
            console.log(`[SEVCS ADMIN] ${endpoint} SUCCESS - Local State Updated`);
            // Instant local feedback
            events.emit('STATE_UPDATED', appState);
            
            setTimeout(() => {
                const response = { status: 'OK', snapshot_version: appState.snapshotVersion + 1, snapshot_sequence: appState.lastSequence + 1 };
                events.emit('ACTION_RESPONSE', { requestId, endpoint, status: 'NEW', ...response, payload: response });
                resolveAction(requestId, response);
            }, 500);
            return;
        }
    }

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
 * Administrative Infrastructure Management
 */
export async function addSlot(chargerType = 'STANDARD') {
    return executeAction('admin_add_slot', { charger_type: chargerType });
}

export async function removeSlot(slotId) {
    return executeAction('admin_remove_slot', { slot_id: slotId });
}

export async function updateSlotType(slotId, chargerType) {
    return executeAction('admin_update_slot_type', { slot_id: slotId, charger_type: chargerType });
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
