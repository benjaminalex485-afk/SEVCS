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

    try {
        const response = await fetch(url, { ...options, headers });
        
        if (response.status === 401) {
            console.error('[VoltPark API] Unauthorized (401).');
            if (appState.session.token) {
                console.warn('[VoltPark API] Active session invalidated. Triggering Logout.');
                events.emit('FORCE_LOGOUT');
            }
            return { ok: false, status: 401, error: 'Unauthorized' };
        }

        const data = await response.json().catch(() => ({}));
        
        return {
            ok: response.ok,
            status: response.status,
            data: data
        };
    } catch (error) {
        console.error('[API ERROR]', error);
        return {
            ok: false,
            status: 0,
            error: error.message
        };
    }
}

/**
 * Authentication Layer (Independent of Snapshot Pipeline)
 */
export async function login(credentials) {
    const result = await safeFetch(`${BASE_URL}/api/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(credentials)
    });

    if (result.ok && result.data?.token) {
        const data = result.data;
        appState.session = { token: data.token, userId: data.user_id, role: data.role || 'USER' };
        appState.uiMode = data.role || 'USER'; 
        appState.authStatus = 'AUTHENTICATED_PENDING';
        localStorage.setItem('sevcs_token', data.token);
        startPolling();
        return { success: true };
    }
    return { success: false, message: result.data?.message || result.error || 'Login failed' };
}

export async function signup(profile) {
    const result = await safeFetch(`${BASE_URL}/api/signup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(profile)
    });

    if (result.ok && result.data?.success) {
        return { success: true };
    }
    return { success: false, message: result.data?.message || result.error || 'Signup failed' };
}

export async function getAvailability(username = null, capabilityFilter = null) {
    const url = new URL(`${BASE_URL}/api/availability`);
    if (username) url.searchParams.append('username', username);
    if (capabilityFilter?.charger_types?.length) {
        for (const t of capabilityFilter.charger_types) {
            url.searchParams.append('charger_types', t);
        }
    }
    if (capabilityFilter?.charging_levels?.length) {
        for (const lv of capabilityFilter.charging_levels) {
            url.searchParams.append('charging_levels', lv);
        }
    }
    const result = await safeFetch(url.toString(), { method: 'GET' });
    if (!result.ok) {
        throw new Error(result.data?.message || result.error || `Availability failed (${result.status})`);
    }
    return result.data;
}

export async function getPricingQuote(payload) {
    const result = await safeFetch(`${BASE_URL}/api/pricing_quote`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload })
    });
    if (!result.ok) {
        throw new Error(result.data?.message || result.error || `Quote failed (${result.status})`);
    }
    return result.data;
}

export async function processMockPayment(payload) {
    const result = await safeFetch(`${BASE_URL}/api/payment/mock`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload })
    });
    if (!result.ok) {
        throw new Error(result.data?.message || result.error || `Payment failed (${result.status})`);
    }
    return result.data;
}

/**
 * Polling loop with exponential backoff and latency tracking.
 */
/**
 * Polling loop with single-instance guard and auth gate.
 */
const fetchStatus = async () => {
    if (appState.isSimulating) return;

    const currentTimeout = Math.min(MAX_TIMEOUT, Math.max(MIN_TIMEOUT, avgLatency * 3));
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), currentTimeout);
    const start = Date.now();

    try {
        const url = new URL(`${BASE_URL}/api/status`);
        if (appState.session.userId) {
            url.searchParams.append('username', appState.session.userId);
        }
        const result = await safeFetch(url.toString(), { signal: controller.signal });
        clearTimeout(timeoutId);
        console.log('[VoltPark POLL] status_response', { ok: result.ok, status: result.status, retryDelay: currentRetryDelay });
        
        if (!result.ok || !result.data) {
            if (result.status === 401) {
                events.emit('AUTH_EXPIRED');
            }
            console.warn('[VoltPark POLL] rejected_response', { status: result.status });
            return;
        }

        let latency = Date.now() - start;
        if (latency >= 10) {
            latency = Math.min(5000, latency);
            avgLatency = (EMA_ALPHA * latency) + ((1 - EMA_ALPHA) * avgLatency);
        }

        setLatestSnapshot(result.data, latency);
        console.log('[VoltPark POLL] snapshot_ingest', {
            seq: result.data?.snapshot_sequence,
            mode: result.data?.system_mode || result.data?.mode,
            latency
        });
        
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
        console.error('[VoltPark POLL] fetch_failed', { message: error.message, retryDelay: currentRetryDelay, consecutiveFailures });

        events.emit('API_ERROR', errorObj);
    }
};

let isPolling = false;

export async function startPolling() {
    if (isPolling) return;
    isPolling = true;

    async function pollLoop() {
        if (!isPolling) return;
        try {
            await fetchStatus();
        } finally {
            pollingInterval = setTimeout(pollLoop, currentRetryDelay);
        }
    }
    pollLoop();
}

export function stopPolling() {
    isPolling = false;
    if (pollingInterval) {
        clearTimeout(pollingInterval);
        pollingInterval = null;
    }
    console.log('[VoltPark API] Polling STOPPED');
}

export async function resync() {
    console.log('[VoltPark API] Initiating Resync...');
    try {
        await fetchStatus();
        console.log('[VoltPark API] Resync SUCCESS');
    } catch (e) {
        console.error('[VoltPark API] Resync FAILED:', e);
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

/** Align sim admin payloads with production: snapshot slot_id is 0-based; sim rows may use 1-based ids. */
function simAdminSlotFromPayload(payloadSlotId, simSlots) {
    const n = simSlots.length;
    const r = Number(payloadSlotId);
    if (!Number.isFinite(r) || n === 0) return null;
    if (r >= 0 && r < n) return simSlots[r];
    if (r >= 1 && r <= n) return simSlots[r - 1];
    return simSlots.find((s) => Number(s.slot_id) === r) || null;
}

function simAdminSlotIndexFromPayload(payloadSlotId, simSlots) {
    const n = simSlots.length;
    const r = Number(payloadSlotId);
    if (!Number.isFinite(r) || n === 0) return -1;
    if (r >= 0 && r < n) return r;
    if (r >= 1 && r <= n) return r - 1;
    return simSlots.findIndex((s) => Number(s.slot_id) === r);
}

export async function executeAction(endpoint, payload, intentKey = null) {
    // 0. Policy Guard: Allow financial actions even during desync
    console.log(`[API] executeAction called: ${endpoint}`, payload);
    const isQuoteBackedBooking = endpoint === 'book' && !!payload?.quote_id;
    // Admin slot CRUD is configuration, not vision-frame booking; allow while snapshots catch up.
    const nonVisionGatedEndpoints = new Set([
        'recharge', 'cancel_booking', 'login', 'signup', 'find_slot', 'authorize', 'start_charging',
        'admin_add_slot', 'admin_remove_slot', 'admin_update_slot_type'
    ]);
    const isCriticalVisionAction = !nonVisionGatedEndpoints.has(endpoint) && !isQuoteBackedBooking;
    
    if (isCriticalVisionAction && !appState.allowActions) {
        const blockMessage = 'System not synchronized. Please wait for health indicator to turn Green.';
        console.error(`[API] Action ${endpoint} BLOCKED: System not synchronized. Current State:`, appState.displayState);
        events.emit('API_ERROR', { 
            code: 'NOT_SYNCHRONIZED', 
            retryable: false, 
            message: blockMessage
        });
        return { status: 'error', message: blockMessage };
    }

    // Intent Lock Guard
    if (intentKey && appState.pendingIntents.has(intentKey)) {
        const intent = appState.pendingIntents.get(intentKey);
        if (intent.status === 'PENDING') return { status: 'pending', message: 'Action already in progress' };
    }

    if (appState.pendingActions.size >= MAX_PENDING) {
        const throttleMessage = 'Too many pending requests';
        events.emit('API_ERROR', { code: 'THROTTLE', retryable: true, message: throttleMessage });
        return { status: 'error', message: throttleMessage };
    }

    // Click-Time Version Binding
    const versionAtClick = appState.snapshotVersion;
    const requestId = `req_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

    console.log(`[VoltPark API] Executing ${endpoint} (ID: ${requestId})`);
    registerAction(requestId, versionAtClick, endpoint, intentKey);

    // Simulation Intercept (Admin Infrastructure)
    if (appState.isSimulating) {
        let success = false;
        if (endpoint === 'admin_add_slot') {
            const newId = appState.simSlots.length > 0 ? Math.max(...appState.simSlots.map(s => s.slot_id)) + 1 : 1;
            const chargerTypes = Array.isArray(payload.charger_types) && payload.charger_types.length > 0 ? payload.charger_types : ['AC_WIRED'];
            const chargingLevels = Array.isArray(payload.charging_levels) && payload.charging_levels.length > 0 ? payload.charging_levels : ['LEVEL_2'];
            appState.simSlots.push({
                slot_id: newId,
                state: 'FREE',
                charger_type: payload.charger_type || 'STANDARD',
                charger_types: chargerTypes,
                charging_levels: chargingLevels,
                assigned_global_id: null
            });
            success = true;
        } else if (endpoint === 'admin_remove_slot') {
            const idx = simAdminSlotIndexFromPayload(payload.slot_id, appState.simSlots);
            if (idx >= 0) {
                appState.simSlots.splice(idx, 1);
                appState.simSlots.forEach((s, i) => { s.slot_id = i; });
                success = true;
            }
        } else if (endpoint === 'admin_update_slot_type') {
            const slot = simAdminSlotFromPayload(payload.slot_id, appState.simSlots);
            if (slot) {
                slot.charger_types = Array.isArray(payload.charger_types) && payload.charger_types.length > 0 ? payload.charger_types : slot.charger_types || ['AC_WIRED'];
                slot.charging_levels = Array.isArray(payload.charging_levels) && payload.charging_levels.length > 0 ? payload.charging_levels : slot.charging_levels || ['LEVEL_2'];
                slot.charger_type = payload.charger_type || slot.charger_type;
                success = true;
            }
        }

        if (success) {
            console.log(`[VoltPark ADMIN] ${endpoint} SUCCESS - Local State Updated`);
            // Instant local feedback
            events.emit('STATE_UPDATED', appState);

            const simResponse = {
                status: 'OK',
                snapshot_version: appState.snapshotVersion + 1,
                snapshot_sequence: appState.lastSequence + 1
            };
            setTimeout(() => {
                events.emit('ACTION_RESPONSE', { requestId, endpoint, status: 'NEW', ...simResponse, payload: simResponse });
                resolveAction(requestId, simResponse);
            }, 500);
            return { ...simResponse };
        }
    }

    try {
        const result = await safeFetch(`${BASE_URL}/api/${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                request_id: requestId,
                snapshot_version: versionAtClick,
                snapshot_sequence: appState.lastSequence,
                payload
            })
        });

        if (!result.ok) {
            const errorMsg = result.data?.message || result.data?.code || result.error || `HTTP Error: ${result.status}`;
            throw new Error(errorMsg);
        }

        const data = result.data;

        // Freeze Race Guard: Check before resolving
        if (appState.snapshot?.freeze_state) {
            console.warn('[VoltPark API] Response received during FREEZE. Marking as UNKNOWN.');
            resolveAction(requestId, { ...data, status: 'UNKNOWN', error: 'System frozen during completion' });
            return { ...data, status: 'UNKNOWN' };
        }

        events.emit('ACTION_RESPONSE', { 
            requestId, 
            endpoint,
            status: data.status === 'OK' || data.status === 'success' ? (data.replayed ? 'REPLAYED' : 'NEW') : 'REJECTED',
            snapshot_version: data.snapshot_version,
            snapshot_sequence: data.snapshot_sequence,
            payload: data 
        });

        resolveAction(requestId, data);
        return data;

    } catch (error) {
        console.error(`[VoltPark API] Action ${endpoint} Failed:`, error);
        events.emit('API_ERROR', { code: 'ACTION_FAILED', retryable: false, message: error.message });
        const errorRes = { status: 'ERROR', error: error.message };
        resolveAction(requestId, errorRes);
        return errorRes;
    }
}

export async function bookSlot(slotId) {
    return executeAction('book', { slot_id: slotId }, `book_slot_${slotId}`);
}

/**
 * Administrative Infrastructure Management
 */
export async function addSlot(chargerTypes = ['AC_WIRED'], chargingLevels = ['LEVEL_2']) {
    return executeAction('admin_add_slot', { charger_types: chargerTypes, charging_levels: chargingLevels });
}

export async function removeSlot(slotId) {
    return executeAction('admin_remove_slot', { slot_id: slotId });
}

export async function updateSlotType(slotId, chargerTypes, chargingLevels) {
    return executeAction('admin_update_slot_type', {
        slot_id: slotId,
        charger_types: chargerTypes,
        charging_levels: chargingLevels
    });
}

export async function getAdminPricingSettings() {
    const result = await safeFetch(`${BASE_URL}/api/admin_pricing_settings`, { method: 'GET' });
    if (!result.ok) {
        throw new Error(result.data?.message || result.error || `Settings fetch failed (${result.status})`);
    }
    return result.data;
}

export async function updateAdminPricingSettings(highUrgencyMultiplier) {
    const result = await safeFetch(`${BASE_URL}/api/admin_pricing_settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload: { high_urgency_multiplier: highUrgencyMultiplier } })
    });
    if (!result.ok) {
        throw new Error(result.data?.message || result.error || `Settings update failed (${result.status})`);
    }
    return result.data;
}

/**
 * Flush pending actions with optional scope filtering
 */
export function resetPendingActions(filter = {}) {
    console.warn('[VoltPark API] Scoped Action Reset:', filter);
    
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
