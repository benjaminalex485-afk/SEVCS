#ifndef UI_BUNDLE_H
#define UI_BUNDLE_H
#include <Arduino.h>
struct UIFile { const char* path; const char* content; const char* mimeType; };
const char UI_FILE_index_html[] PROGMEM = R"rawliteral(<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SEVCS | Deterministic Control Surface</title>
    <link rel="stylesheet" href="styles/style.css">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
</head>
<body class="dark-theme">
    <!-- Freeze/Desync Warning Banner -->
    <div id="status-banner" class="banner hidden">
        <span id="banner-message">SYSTEM FROZEN</span>
    </div>

    <div id="auth-container" class="auth-overlay" style="display: none;"></div>

    <header class="main-header">
        <div class="logo-area">
            <h1>SEVCS <span>Smart EV Charging System</span></h1>
            <div id="ui-confidence-tag" class="confidence-tag disconnected">DISCONNECTED</div>
        </div>
        
        <div class="header-center">
            <div id="snapshot-info" class="mono header-info">
                Snapshot: v0 | Seq #0
            </div>
            <div id="ui-health-indicator" class="health-indicator critical">
                HEALTH: CRITICAL
            </div>
        </div>

        <div class="header-actions">
            <div class="mode-toggle">
                <button id="btn-mode-admin" class="btn-toggle active">ADMIN</button>
                <button id="btn-mode-user" class="btn-toggle">USER</button>
            </div>
            <button id="btn-logout" class="btn-logout" style="display: none;">Logout</button>
            <div id="header-status" class="header-status">
                <!-- Polling Indicator & Latency -->
            </div>
        </div>
    </header>

    <main class="dashboard" id="main-dashboard">
        <!-- Left Column: Live Status & Actions -->
        <section class="control-panel" id="admin-controls">
            <div id="live-status-container"></div>
            <div id="action-panel-container"></div>
            <div id="simulation-panel-container"></div>
            <div id="debug-panel-container"></div>
        </section>

        <!-- Right Column: Grids & Tables -->
        <section class="data-panel">
            <div id="slot-grid-container"></div>
            <div id="queue-table-container"></div>
            <div id="user-ui-container" class="hidden"></div>
        </section>
    </main>

    <script type="module" src="app/app.js?v=3"></script>
</body>
</html>
)rawliteral";
const char UI_FILE_app_api_v3_js[] PROGMEM = R"rawliteral(import { appState, setLatestSnapshot, registerAction, resolveAction } from './state_v3.js';
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
        const data = await safeFetch(`${BASE_URL}/api/status`, { signal: controller.signal });
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
)rawliteral";
const char UI_FILE_app_app_js[] PROGMEM = R"rawliteral(import { executeAction, startPolling } from './api_v3.js';
import { startRenderer } from './renderer.js';
import { initSystemUI } from '../components/system_ui.js';
import { initGrids } from '../components/grids.js';
import { initSimulationUI } from '../components/simulation.js';
import { initUserUI } from '../components/user_ui.js';
import { renderAuthUI } from '../components/auth_ui.js';
import { events } from './events.js';
import { appState, performHardReset } from './state_v3.js';

/**
 * SEVCS UI Bootstrap
 */
function bootstrap() {
    console.log('[SEVCS] Initializing Deterministic UI...');

    // 1. Initialize Components
    try {
        initSystemUI();
        initGrids();
        initSimulationUI();
        initUserUI();
        renderAuthUI();
    } catch (e) {
        console.error('[SEVCS] Component Initialization Failed:', e);
    }

    // 2. Mode Toggling
    let lastDisplayState = 'INITIALIZING';
    events.on('STATE_UPDATED', (state) => {
        lastDisplayState = state.displayState;
        
        // Toggle logout button
        document.getElementById('btn-logout').style.display = 
            state.authStatus !== 'GUEST' ? 'block' : 'none';
        
        // Hide/Show main dashboard based on auth
        document.getElementById('main-dashboard').style.visibility = 
            state.authStatus === 'AUTHENTICATED' ? 'visible' : 'hidden';

        // Toggle Admin/User sections
        const isAdmin = state.uiMode === 'ADMIN';
        document.getElementById('admin-controls').classList.toggle('hidden', !isAdmin);
        document.getElementById('user-ui-container').classList.toggle('hidden', isAdmin);
    });

    document.getElementById('btn-mode-admin').onclick = () => {
        if (lastDisplayState !== 'SYNCHRONIZED') return;
        appState.uiMode = 'ADMIN';
        events.emit('STATE_UPDATED', appState);
    };

    document.getElementById('btn-mode-user').onclick = () => {
        if (lastDisplayState !== 'SYNCHRONIZED') return;
        appState.uiMode = 'USER';
        events.emit('STATE_UPDATED', appState);
    };

    document.getElementById('btn-logout').onclick = () => {
        performHardReset();
    };

    // 3. Auth Persistence Check
    const token = localStorage.getItem('sevcs_token');
    if (token) {
        appState.session.token = token;
        appState.authStatus = 'AUTHENTICATED_PENDING';
        startPolling();
    }

    // 4. Start Render Tick
    startRenderer();

    events.on('API_ERROR', (error) => {
        console.error(`[SEVCS UI ERROR] ${error.code}: ${error.message}`);
    });
}

// Ensure DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
} else {
    bootstrap();
}
)rawliteral";
const char UI_FILE_app_events_js[] PROGMEM = R"rawliteral(/**
 * SEVCS Lightweight Event Bus
 * Decouples state changes from rendering and API responses.
 */
class EventEmitter {
    constructor() {
        this.listeners = {};
    }

    on(event, callback) {
        if (!this.listeners[event]) {
            this.listeners[event] = [];
        }
        if (!this.listeners[event].includes(callback)) {
            this.listeners[event].push(callback);
        }
    }
    
    off(event, callback) {
        if (this.listeners[event]) {
            this.listeners[event] = this.listeners[event].filter(cb => cb !== callback);
        }
    }

    emit(event, data) {
        if (this.listeners[event]) {
            this.listeners[event].forEach(callback => {
                try {
                    callback(data);
                } catch (err) {
                    console.error(`[EVENT ERROR] ${event}`, err);
                }
            });
        }
    }
}

export const events = new EventEmitter();
)rawliteral";
const char UI_FILE_app_renderer_js[] PROGMEM = R"rawliteral(import { appState, takeLatestSnapshot, commitSnapshot, processSnapshot, checkPendingHardSync } from './state_v3.js';
import { events } from './events.js';

let lastRenderTime = Date.now();
const MAX_RENDER_DELAY = 500;
let isRendering = false;
let retryCount = 0;
const MAX_RETRY = 3;
let lastIdentity = null;

const toxicFrames = new Map(); // hash_seq -> timestamp
const TOXIC_TTL = 30000; // 30s

/**
 * Toxic Identity Guard
 */
function isToxic(identityKey) {
    const ts = toxicFrames.get(identityKey);
    if (!ts) return false;

    if (Date.now() - ts > TOXIC_TTL) {
        toxicFrames.delete(identityKey);
        return false;
    }

    return true;
}

/**
 * Decoupled Render Tick (100ms)
 * Enforces mandatory ordering: INPUT -> SYNC -> VALIDATE -> PROCESS -> RENDER
 */
export function startRenderer() {
    const renderTick = () => {
        if (isRendering) return; 
        
        isRendering = true;
        try {
            const now = Date.now();
            lastRenderTime = now;

            // 1. SYNC (Priority)
            if (checkPendingHardSync()) {
                safeDraw();
                return; 
            }

            // 2. INPUT (Commit-After-Use Buffer Pull)
            const snapshot = takeLatestSnapshot();
            
            if (snapshot) {
                console.log("[RENDER] Consuming snapshot:", snapshot.snapshot_sequence);
                const identityKey = `${snapshot.state_hash}_${snapshot.snapshot_sequence}`;
                
                if (identityKey === lastIdentity) {
                    commitSnapshot();
                    return;
                }
                
                if (isToxic(identityKey)) {
                    commitSnapshot();
                    return;
                }

                try {
                    processSnapshot(snapshot);
                    commitSnapshot();
                    retryCount = 0; 
                    lastIdentity = identityKey;
                } catch (e) {
                    retryCount++;
                    console.error(`[SEVCS] SNAPSHOT PROCESS FAILED: Attempt ${retryCount}`, e);
                    
                    if (retryCount >= MAX_RETRY) {
                        console.error('[SEVCS] TOXIC SNAPSHOT IDENTIFIED: Blacklisting for 30s', identityKey);
                        toxicFrames.set(identityKey, Date.now());
                        if (toxicFrames.size > 100) toxicFrames.clear();
                        commitSnapshot();
                        retryCount = 0;
                    }
                    throw e; 
                }
            }
            
            // 3. RENDER
            safeDraw();
        } catch (e) {
            // Kernel crash protection
        } finally {
            isRendering = false;
        }
    };

    // Main Loop
    setInterval(renderTick, 100);

    // Watchdog
    setInterval(() => {
        if (Date.now() - lastRenderTime > MAX_RENDER_DELAY) {
            console.warn('[SEVCS] RENDER STARVATION DETECTED: Forcing recovery frame');
            renderTick(); 
        }
    }, 200);
}

/**
 * Fail-Safe Drawing
 */
/**
 * Centralized UI State Priority Engine
 */
function computeUIState() {
    // 0. Hard Guards
    if (appState.isResyncing) return 'RESYNC_REQUIRED';

    if (!appState.snapshot) {
        const sinceStart = performance.now() - appState.appStartMono;
        if (sinceStart > 5000) return 'DISCONNECTED';
        return 'INITIALIZING';
    }

    const delta = performance.now() - appState.lastSnapshotMono;

    // 1. Critical: Freeze
    if (appState.snapshot?.freeze_state) return 'FROZEN';

    // 2. Link Failure (Hard Threshold)
    if (delta > appState.SNAPSHOT_FRESHNESS_MS + 1000) {
        return 'DISCONNECTED';
    }

    // 3. Warning: Sequence Integrity
    if (appState.isDesync) return 'DESYNCHRONIZED';

    // 4. Link Health (Soft Threshold)
    if (delta > appState.SNAPSHOT_FRESHNESS_MS) {
        return 'DEGRADED';
    }

    return 'SYNCHRONIZED';
}

/**
 * Fail-Safe Drawing
 */
function safeDraw() {
    try {
        const displayState = computeUIState();
        
        // Final Display Synthesis
        const stateToEmit = {
            ...appState,
            displayState,
            snapshot: appState.snapshot ? {
                ...appState.snapshot,
                slots: [...appState.snapshot.slots].sort((a, b) => a.slot_id - b.slot_id),
                queue: [...appState.snapshot.queue].sort((a, b) => (a.global_id || 0) - (b.global_id || 0))
            } : null
        };
        
        events.emit('STATE_UPDATED', stateToEmit);
    } catch (e) {
        console.error('[SEVCS] KERNEL CRASH', e);
        events.emit('RENDER_FALLBACK', e);
    }
}
)rawliteral";
const char UI_FILE_app_state_v3_js[] PROGMEM = R"rawliteral(import { events } from './events.js';
import { resetPendingActions } from './api_v3.js';

console.log('[SEVCS] STATE_V3 LOADED - VERIFYING LATENCY FIX AT L402');
export const appState = {
    // System Status
    snapshot: null,
    lastSequence: -1,
    snapshotVersion: 0,
    systemState: 'VALID', // VALID | INVALID (empty queue AND slots)
    
    // Health & Determinism
    uiHealth: 'CRITICAL', // GOOD | DEGRADED | CRITICAL
    stagnantCounter: 0,
    hasGap: false,
    isSynchronized: false,
    stabilityCounter: 0,
    recoveryCounter: 0, // 3-frame stability for desync recovery
    stableFrames: 0, // Sequence stability counter
    lastSnapshotTime: Date.now(), // Wall clock for freshness
    lastSnapshotMono: performance.now(),
    appStartMono: performance.now(),
    lastClockSyncTime: 0,
    lastHardSyncTime: 0,
    minAcceptedSequence: -1,
    isResyncing: false,
    isDesync: false,
    healthScore: 100,
    criticalLatencyCycles: 0,
    forceGapSimulation: false,
    
    // Interaction
    uiMode: 'ADMIN', // ADMIN | USER
    authStatus: 'GUEST', // GUEST | AUTHENTICATING | AUTHENTICATED_PENDING | AUTHENTICATED
    session: { token: null, userId: null, role: 'USER' },
    userProfile: null,
    latencyBuffer: [], // Smoothing for health flickering
    requestHistory: [], // Bounded log (max 20)
    transitionLog: [], // Snapshot jump/state audit
    pendingActions: new Map(),
    pendingIntents: new Map(), // intentKey -> { startedAt, endpoint, status }
    isSimulating: false,
    simEpoch: 0,
    simStartTime: 0,
    simExpired: false,
    blockSimRestart: false,
    simLockoutStartTime: 0,
    simAuditLog: [],
    stableBackendFrames: 0,
    pendingHardSync: null,
    lastSyncVersion: -1,
    lastProcessedSource: null,
    allowActions: false,
    simSlots: [
        {slot_id: 1, state: "FREE", charger_type: "FAST", assigned_global_id: null},
        {slot_id: 2, state: "FREE", charger_type: "STANDARD", assigned_global_id: null}
    ],
    
    // Constants
    MAX_STAGNANT_FRAMES: 5,
    MAX_LATENCY_THRESHOLD: 1000, 
    BACKEND_STABILITY_FRAMES: 2,
    MAX_SNAPSHOT_AGE: 5000, 
    SNAPSHOT_FRESHNESS_MS: 3000,
    INTENT_TIMEOUT_MS: 5000,
    MAX_ALLOWED_GAP: 5,
    MAX_SEQ_JUMP: 20,
    RECOVERY_THRESHOLD: 3,
    PRIORITY: { BACKEND: 2, SIMULATION: 1 }
};

let latestIncomingSnapshot = null;

/**
 * Universal Safe Clone for embedded environments
 */
function safeClone(obj) {
    try {
        if (typeof structuredClone === 'function') {
            return structuredClone(obj);
        }
    } catch (e) {}
    return JSON.parse(JSON.stringify(obj));
}

/**
 * Recursive Immutability Guard
 */
function deepFreeze(obj) {
    if (obj && typeof obj === 'object' && !Object.isFrozen(obj)) {
        Object.freeze(obj);
        for (const key of Object.keys(obj)) {
            deepFreeze(obj[key]);
        }
    }
    return obj;
}

/**
 * Contract-Enforced Normalization
 */
function normalizeSnapshot(s) {
    return {
        snapshot_sequence: Number(s.snapshot_sequence),
        snapshot_version: Number(s.snapshot_version),
        timestamp: Number(s.timestamp),
        system_mode: String(s.system_mode || s.mode || ""),
        system_health: Number(s.system_health || s.health || 0),
        freeze_state: Boolean(s.freeze_state),
        slots: Array.isArray(s.slots) ? s.slots : [],
        queue: Array.isArray(s.queue) ? s.queue : [],
        state_hash: String(s.state_hash || ""),
        source: String(s.source || ""),
        latency: Number(s.latency || 0)
    };
}

/**
 * EMA Clock Adjustment with Spike Rejection & 1min Anchor
 */
const MAX_DRIFT_STEP = 2000; 
const CLOCK_SYNC_INTERVAL = 60000; 
function adjustClock(serverTs) {
    const now = Date.now();
    let drift = serverTs - now;

    if (Math.abs(drift) > 10000) return;

    // ðŸ”¥ HARD RESYNC (Periodic Anchor)
    if (now - appState.lastClockSyncTime > CLOCK_SYNC_INTERVAL) {
        appState.timeOffset = drift;
        appState.lastClockSyncTime = now;
        return;
    }

    drift = Math.max(-MAX_DRIFT_STEP, Math.min(MAX_DRIFT_STEP, drift));
    appState.timeOffset = (0.9 * appState.timeOffset) + (0.1 * drift);
}

/**
 * Validation Pipeline & Input Stage
 */
export function setLatestSnapshot(data, latency) {
    // 0. Synchronization Barrier
    if (appState.isResyncing) {
        return; // Ignore all snapshots until manual resync
    }

    // 1. Source Guard
    if (data.source !== "BACKEND") {
        appState.stableBackendFrames = 0;
    } else {
        // 2. Adjust Clock Offset (Backend only)
        let ts = data.timestamp;
        if (ts < 1e12) ts *= 1000;
        adjustClock(ts);
    }

    // 3. Normalized Contract
    const normalized = normalizeSnapshot(data);
    normalized.latency = latency;

    // 4. Timestamp Normalization & Directional Guard
    let timestamp = normalized.timestamp;
    
    // Normalize ONCE and use everywhere
    if (timestamp < 1e12) {
        timestamp = timestamp * 1000;
    }
    normalized.timestamp = timestamp;

    // Simulation Injectors: Controlled gap injection
    if (appState.forceGapSimulation) {
        normalized.snapshot_sequence += 50;
        appState.forceGapSimulation = false;
        console.warn('[SEVCS SIM] Sequence GAP injected into pipeline');
    }

    // Use Adjusted Now for drift compensation
    const adjustedNow = Date.now() + appState.timeOffset;
    const age = adjustedNow - timestamp;

    // Stale: Reject hard
    if (age > appState.MAX_SNAPSHOT_AGE) {
        console.warn('[SEVCS] SNAPSHOT REJECTED: Stale Data', { age, timestamp, adjustedNow, offset: appState.timeOffset });
        appState.stableBackendFrames = 0; 
        return;
    }

    // Future: Reject drift
    if (timestamp - adjustedNow > 3000) {
        console.error('[SEVCS] SNAPSHOT REJECTED: Future Timestamp', { drift: timestamp - adjustedNow, timestamp, adjustedNow });
        appState.stableBackendFrames = 0; 
        return;
    }

    // 5. Strict Monotonicity Progression
    if (appState.snapshot && normalized.snapshot_sequence <= appState.lastSequence) {
        if (normalized.snapshot_sequence === appState.lastSequence && 
            normalized.state_hash === appState.snapshot.state_hash &&
            normalized.timestamp === appState.snapshot.timestamp) {
            return; // True network duplicate
        }
        
        if (normalized.snapshot_sequence < appState.lastSequence) {
            appState.stableBackendFrames = 0;
            return;
        }
    }

    // 6. Source-Aware Jump Protection (Only BACKEND -> BACKEND)
    if (appState.snapshot && appState.snapshot.source === 'BACKEND' && data.source === 'BACKEND') {
        const delta = normalized.snapshot_sequence - appState.lastSequence;
        if (Math.abs(delta) > appState.MAX_SEQ_JUMP) {
            appState.stableBackendFrames = 0;
            if (!appState.pendingHardSync || normalized.snapshot_sequence > appState.pendingHardSync.snapshot_sequence) {
                appState.pendingHardSync = safeClone(normalized);
            }
            return;
        }
    }

    // 7. Identity Check
    if (appState.snapshot && normalized.state_hash === appState.snapshot.state_hash && normalized.timestamp === appState.snapshot.timestamp) {
        return;
    }

    // 8. EMA Reset
    if (appState.lastProcessedSource !== 'BACKEND' && data.source === 'BACKEND') {
        events.emit('EMA_RESET_REQUESTED');
    }

    // 9. Stability Gate
    if (appState.isSimulating && data.source === 'BACKEND') {
        const isIncreasing = !appState.snapshot || normalized.snapshot_sequence > appState.lastSequence;
        if (isIncreasing) {
            appState.stableBackendFrames++;
            if (appState.stableBackendFrames >= appState.BACKEND_STABILITY_FRAMES) {
                if (!appState.pendingHardSync || normalized.snapshot_sequence > appState.pendingHardSync.snapshot_sequence) {
                    appState.pendingHardSync = safeClone(normalized);
                }
            }
        } else {
            appState.stableBackendFrames = 0; 
        }
        return;
    }

    // 10. Frame Isolation
    latestIncomingSnapshot = safeClone({ ...normalized, timestamp, arrivalTimestamp: Date.now() });

    // Desync Early Detection (Monotonicity Guard)
    if (appState.snapshot && normalized.snapshot_sequence > appState.lastSequence + 1) {
        appState.isDesync = true;
        appState.recoveryCounter = 0;
    }

    // Reset stability & lockout on backend success
    if (data.source === 'BACKEND') {
        appState.stableBackendFrames = 0;
        appState.blockSimRestart = false;
        appState.simExpired = false;
        appState.simLockoutStartTime = 0;
    }

    // 11. Simulation Expiry
    if (appState.isSimulating) {
        const simTime = Date.now() - appState.simStartTime;
        if (simTime > appState.MAX_SIM_DURATION) {
            appState.isSimulating = false;
            appState.simExpired = true;
            appState.blockSimRestart = true;
            appState.simLockoutStartTime = Date.now();
            appState.isDesync = true;
        } else if (simTime > appState.SIM_WARNING_THRESHOLD) {
            // Simulation expiring warning handled by derived health
        }
    }
}

/**
 * Sync Pipeline Stage: Consumes atomic transitions once per tick.
 */
export function checkPendingHardSync() {
    if (appState.pendingHardSync) {
        const syncTarget = appState.pendingHardSync;
        appState.pendingHardSync = null; 
        performHardSync(syncTarget);
        return true; 
    }
    return false;
}

export function takeLatestSnapshot() {
    // Audit for persistent desync using deterministic frame time
    if (appState.isDesync && latestIncomingSnapshot) {
        if (!appState.lastDesyncFrameTime) appState.lastDesyncFrameTime = latestIncomingSnapshot.timestamp;
        
        // Hysteresis: Only hard sync if no recovery progress is being made
        const desyncDuration = latestIncomingSnapshot.timestamp - appState.lastDesyncFrameTime;
        if (desyncDuration > 2000 && appState.recoveryCounter === 0) {
            // Cooldown check
            if (Date.now() - appState.lastHardSyncTime < 3000) return null;
            
            console.error('[SEVCS] PERSISTENT DESYNC (>2s): Triggering recovery sync');
            performHardSync(null); 
            appState.lastDesyncFrameTime = 0;
            return null;
        }
    } else {
        appState.lastDesyncFrameTime = 0;
    }
    return latestIncomingSnapshot;
}

export function commitSnapshot() {
    latestIncomingSnapshot = null;
}

/**
 * Processing Stage: Atomic state commitment.
 */
export function processSnapshot(data) {
    // 1. Type-Safe Sequence Guard
    if (!data || typeof data.snapshot_sequence !== 'number') return;

    // 2. Pipeline Guard: Reject during recovery
    if (appState.isResyncing) return;

    // 3. Late Packet Protection
    const newSequence = data.snapshot_sequence;
    if (appState.minAcceptedSequence !== -1 && newSequence < appState.minAcceptedSequence) {
        return;
    }

    // 4. User Scope Guard
    if (data.user_id && appState.session.userId && data.user_id !== appState.session.userId) {
        console.error('[SEVCS] USER SCOPE VIOLATION');
        events.emit('FORCE_LOGOUT');
        return;
    }

    const oldSequence = appState.lastSequence;

    // âœ… BOOTSTRAP (Clean Boundary)
    if (oldSequence === -1) {
        appState.snapshot = deepFreeze(data); // Immutable commit
        appState.lastSequence = newSequence;
        appState.lastSnapshotTime = Date.now();
        appState.lastSnapshotMono = performance.now();
        
        // Tighten bootstrap boundary
        if (appState.minAcceptedSequence === -1) {
            appState.minAcceptedSequence = newSequence;
        }
        
        appState.isDesync = false;
        appState.stableFrames = 0;

        // âœ… AUTO-PROMOTE AUTH (Hide Splash)
        if (appState.authStatus === 'AUTHENTICATED_PENDING') {
            console.log("[SYNC] AUTH PROMOTED -> AUTHENTICATED");
            appState.authStatus = 'AUTHENTICATED';
        }

        console.log("[SYNC] BOOTSTRAP:", newSequence);
        return;
    }

    // 5. Monotonicity Guard
    if (newSequence <= oldSequence) {
        if (newSequence === oldSequence && (Date.now() - appState.lastSnapshotTime) > 1000) {
            console.warn("[SYNC] SEQUENCE STALL DETECTED");
        }
        return;
    }

    const gap = newSequence - oldSequence;

    // â— ATOMIC HARD DESYNC
    if (gap > (appState.MAX_SEQ_JUMP ?? 20)) {
        performHardSync({ reason: 'SEQUENCE_JUMP', gap });
        return;
    }

    // âœ… ATOMIC COMMIT
    const nowMono = performance.now();
    let delta = nowMono - appState.lastSnapshotMono;
    
    // â— Defensive Delta Clamping (Tab suspend guard)
    if (!Number.isFinite(delta) || delta < 0 || delta > 60000) delta = 0;

    appState.snapshot = deepFreeze(data); // Immutable commit
    appState.lastSequence = newSequence;
    appState.lastSnapshotTime = Date.now();
    appState.lastSnapshotMono = nowMono;
    
    console.log("[PIPELINE] ACCEPTING SNAPSHOT:", newSequence);
    
    // ðŸ©º Asymmetric Health Metric (Fast recovery, smoothed decay)
    const instantHealth = Math.max(0, 100 - Math.floor(delta / 50));
    if (instantHealth > (appState.healthScore || 0)) {
        appState.healthScore = Math.min(100, (appState.healthScore || 0) + 2);
    } else {
        appState.healthScore = Math.round(0.8 * (appState.healthScore || 100) + 0.2 * instantHealth);
    }

    // âœ… HYSTERESIS
    if (gap === 1) {
        appState.stableFrames = Math.min((appState.stableFrames || 0) + 1, 10);
        if (appState.stableFrames >= 3) {
            appState.isDesync = false;
        }
    } else {
        appState.stableFrames = 0;
        if (!appState.isDesync) {
            appState.isDesync = true;
        }
    }

    // Rate-limited logging (1/10 frames)
    if (newSequence % 10 === 0) {
        console.log("[SYNC]", {
            type: "ACCEPTED",
            seq: newSequence,
            gap,
            delta,
            stable: appState.stableFrames,
            desync: appState.isDesync,
            user: data.user_id || 'NA'
        });
    }
}

/**
 * Hard Sync: System-triggered lock on major inconsistency.
 */
function performHardSync(meta) {
    if (appState.isResyncing) return;

    // ðŸ”¥ SET FLAG FIRST (Atomic Boundary)
    appState.isResyncing = true;
    appState.isDesync = true;
    appState.stableFrames = 0;

    console.warn('[SEVCS] HARD SYNC: Transitioning to RESYNC_REQUIRED', meta);
    
    // Stop Polling BEFORE establishing boundary
    import('./api_v3.js').then(m => m.stopPolling());

    appState.minAcceptedSequence = Number.MAX_SAFE_INTEGER; // Hard Barrier
    latestIncomingSnapshot = null; 

    // Lock UI state for computation
    appState.snapshot = null;
    appState.lastSequence = -1;
    
    events.emit('RESYNC_REQUIRED', meta);
}

/**
 * Resync: User-triggered recovery without killing session.
 */
export function resync() {
    if (!appState.isResyncing) return;

    console.warn('[SEVCS] USER RESYNC: Re-initializing pipeline');
    
    // Reset AUTHORITATIVE Boundary FIRST
    appState.snapshot = null;
    appState.lastSequence = -1;
    appState.stableFrames = 0;
    appState.minAcceptedSequence = -1; // Unlock Boundary
    
    // UNLOCK BEFORE POLLING
    appState.isResyncing = false;
    appState.isDesync = false;
    appState.stableFrames = 0;
    
    import('./api_v3.js').then(m => m.startPolling());
    
    events.emit('RESYNC_STARTED');
}

/**
 * Hard Reset: Full teardown (Logout).
 */
export function performHardReset() {
    console.warn('[SEVCS] HARD RESET: Tearing down all state');
    
    // 1. Clear Polling & Timers
    events.emit('STOP_POLLING');
    
    // 2. Wipe AppState
    appState.snapshot = null;
    appState.lastSequence = -1;
    appState.authStatus = 'GUEST';
    appState.session = { token: null, userId: null };
    appState.userProfile = null;
    appState.requestHistory = [];
    appState.transitionLog = [];
    appState.pendingActions.clear();
    appState.pendingIntents.clear();
    appState.isSynchronized = false;
    
    // 3. Clear Token
    localStorage.removeItem('sevcs_token');
    
    events.emit('HARD_RESET_COMPLETE');
}

export function registerAction(requestId, versionSent, endpoint, intentKey = null) {
    appState.pendingActions.set(requestId, { 
        versionSent, 
        endpoint, 
        intentKey, 
        snapshot_sequence: appState.lastSequence,
        timestamp: Date.now() 
    });
    if (intentKey) {
        appState.pendingIntents.set(intentKey, { startedAt: Date.now(), status: 'PENDING' });
    }
    events.emit('ACTIONS_CHANGED', appState.pendingActions);
}

export function resolveAction(requestId, responseData) {
    const action = appState.pendingActions.get(requestId);
    if (!action) return;

    // Intent Finalization Guard: If already finalized (timeout), ignore late response
    if (action.intentKey) {
        const intent = appState.pendingIntents.get(action.intentKey);
        // Intent Finalization Guard: If already finalized (timeout/replayed), ignore late response
        if (intent && intent.status !== 'PENDING') {
            console.warn('[SEVCS] IGNORED LATE RESPONSE: Intent already finalized as', intent.status);
            appState.pendingActions.delete(requestId);
            return;
        }
        
        // Finalize intent
        if (responseData) {
            appState.pendingIntents.set(action.intentKey, { 
                ...intent, 
                status: responseData.status === 'OK' ? 'SUCCESS' : 'FAILED' 
            });
        }
    }

    if (responseData) {
        const entry = {
            sequence: responseData.snapshot_sequence || appState.lastSequence,
            request_id: requestId,
            endpoint: action.endpoint,
            status: responseData.status === 'OK' ? (responseData.replayed ? 'REPLAYED' : 'NEW') : (responseData.status || 'REJECTED'),
            snapshot_version: responseData.snapshot_version || action.versionSent,
            snapshot_sequence: responseData.snapshot_sequence || action.snapshot_sequence,
            decision_id: responseData.decision_id || '---',
            frame_time: Date.now()
        };
        appState.requestHistory.unshift(entry);
        appState.requestHistory.sort((a, b) => {
            if (b.sequence !== a.sequence) return b.sequence - a.sequence;
            return b.frame_time - a.frame_time;
        });
        if (appState.requestHistory.length > 20) appState.requestHistory.pop();
    }
    appState.pendingActions.delete(requestId);
    events.emit('ACTIONS_CHANGED', appState.pendingActions);
}

/**
 * Intent Cleanup: Bounded loop (1s) to prevent permanent UI blocks.
 */
export function cleanupIntents() {
    const now = Date.now();
    for (const [key, intent] of appState.pendingIntents.entries()) {
        if (intent.status === 'PENDING' && now - intent.startedAt > appState.INTENT_TIMEOUT_MS) {
            console.error('[SEVCS] INTENT TIMEOUT:', key);
            appState.pendingIntents.set(key, { ...intent, status: 'UNKNOWN' });
            
            // Log as unknown in history if we can find the action
            const actionEntry = Array.from(appState.pendingActions.values()).find(a => a.intentKey === key);
            if (actionEntry) {
                events.emit('ACTION_TIMEOUT', { key, endpoint: actionEntry.endpoint });
            }
        }
    }
}
setInterval(cleanupIntents, 1000);

// Debug API
window.__SEVCS_DEBUG__ = {
    getState: () => appState,
    forceMockMode: () => { appState.isSimulating = true; startSimulation(); },
    simulateLatency: (ms) => { appState.debugLatency = ms; },
    simulateDisconnect: (active) => { appState.debugForceDisconnect = active; }
};

// Simulation Engine
let simInterval = null;
function startSimulation(forceOverride = false) {
    const now = Date.now();
    if (appState.blockSimRestart && !forceOverride) return;

    if (forceOverride) {
        const lockoutTime = now - appState.simLockoutStartTime;
        if (lockoutTime < appState.SIM_OVERRIDE_COOLDOWN) return;
        appState.simAuditLog.push({ type: "SIM_OVERRIDE", timestamp: now, reason: "manual" });
        appState.blockSimRestart = false;
    }

    if (simInterval) return;
    
    appState.isSimulating = true;
    appState.simStartTime = now;
    
    simInterval = setInterval(() => {
        if (!appState.isSimulating) {
            clearInterval(simInterval);
            simInterval = null;
            return;
        }

        const syntheticData = {
            source: "SIMULATION",
            simulation_epoch: ++appState.simEpoch,
            snapshot_sequence: appState.lastSequence + 1,
            snapshot_version: appState.snapshotVersion + 1,
            system_mode: "SIMULATION",
            system_health: 95,
            freeze_state: false,
            slots: appState.simSlots.map(s => ({...s})),
            queue: [],
            timestamp: Date.now() / 1000,
            state_hash: "SIM_HASH_" + Date.now()
        };
        
        setLatestSnapshot(syntheticData, 0);
    }, 1000);
}

events.on('SIMULATION_TRIGGERED', startSimulation);

/**
 * Controlled Pipeline Injections
 */
events.on('FORCE_GAP_SIMULATION', () => {
    appState.forceGapSimulation = true;
});

// Multi-Tab Sync
window.addEventListener('storage', (event) => {
    if (event.key === 'sevcs_token' && !event.newValue) {
        console.warn('[SEVCS] LOGOUT DETECTED IN OTHER TAB');
        performHardReset();
    }
});

events.on('FORCE_LOGOUT', performHardReset);
)rawliteral";
const char UI_FILE_components_auth_ui_js[] PROGMEM = R"rawliteral(import { appState, resync } from '../app/state_v3.js';
import { login, signup, startPolling } from '../app/api_v3.js';
import { events } from '../app/events.js';

let lastStatus = null;
let lastSyncState = null;
let lastMode = null;

export function renderAuthUI() {
    const container = document.getElementById('auth-container');
    if (!container) return;

    const currentMode = container.dataset.mode || 'login';

    const isStatusSame = appState.authStatus === lastStatus;
    const isModeSame = currentMode === lastMode;
    const isSyncStateSame = appState.isDesync === lastSyncState;
    const isContainerEmpty = container.innerHTML === '';

    if (isStatusSame && isModeSame && !isContainerEmpty) {
        if (appState.authStatus !== 'AUTHENTICATED_PENDING' || isSyncStateSame) {
            return;
        }
    }

    lastStatus = appState.authStatus;
    lastSyncState = appState.isDesync;
    lastMode = currentMode;

    if (appState.authStatus === 'AUTHENTICATED') {
        container.innerHTML = '';
        container.style.display = 'none';
        return;
    }

    container.style.display = 'flex';

    if (appState.authStatus === 'AUTHENTICATED_PENDING') {
        if (appState.isResyncing) {
            container.innerHTML = `
                <div class="auth-card glass">
                    <div class="error-icon" style="font-size: 3rem; margin-bottom: 1rem;">âš ï¸</div>
                    <h2>Sync Interrupted</h2>
                    <p>A massive sequence gap was detected during initial synchronization. Manual resync required.</p>
                    <button class="primary-btn" id="resync-btn" style="margin-top: 1.5rem">Initialize Resync Pipeline</button>
                </div>
            `;
            document.getElementById('resync-btn').onclick = () => {
                resync();
                startPolling();
            };
            return;
        }

        container.innerHTML = `
            <div class="auth-card glass">
                <div class="spinner"></div>
                <h2>Syncing System State...</h2>
                <p>Establishing deterministic pipeline connection.</p>
                <div class="status-bar">
                    <div class="status-progress" style="width: 60%"></div>
                </div>
            </div>
        `;
        return;
    }

    if (appState.authStatus === 'AUTHENTICATING') {
        container.innerHTML = `
            <div class="auth-card glass">
                <div class="spinner"></div>
                <h2>Authenticating...</h2>
                <p>Verifying credentials with secure backend.</p>
            </div>
        `;
        return;
    }

    // GUEST state: Show Login/Signup Toggle
    const isSignup = container.dataset.mode === 'signup';
    
    container.innerHTML = `
        <div class="auth-card glass">
            <h1>SEVCS Smart Charging</h1>
            <p class="subtitle">Secure Deterministic Control Layer</p>
            
            <div class="auth-tabs">
                <button class="tab-btn ${!isSignup ? 'active' : ''}" id="login-tab">Login</button>
                <button class="tab-btn ${isSignup ? 'active' : ''}" id="signup-tab">Sign Up</button>
            </div>

            <form id="auth-form" class="auth-form">
                ${isSignup ? `
                    <div class="input-group">
                        <label>Full Name</label>
                        <input type="text" id="name" placeholder="John Doe" required>
                    </div>
                ` : ''}
                <div class="input-group">
                    <label>Email Address</label>
                    <input type="email" id="email" placeholder="user@example.com" required>
                </div>
                <div class="input-group">
                    <label>Password</label>
                    <input type="password" id="password" placeholder="â€¢â€¢â€¢â€¢â€¢â€¢â€¢â€¢" required>
                </div>
                ${isSignup ? `
                    <div class="input-group">
                        <label>Vehicle Type</label>
                        <select id="vehicleType">
                            <option value="FAST">Fast (DC)</option>
                            <option value="STANDARD">Standard (AC)</option>
                        </select>
                    </div>
                ` : ''}
                
                <button type="submit" class="primary-btn" id="submit-btn">
                    ${isSignup ? 'Create Account' : 'Sign In'}
                </button>
                
                <div id="auth-error" class="error-msg" style="display: none;"></div>
            </form>
        </div>
    `;

    // Event Listeners
    document.getElementById('login-tab').onclick = () => {
        container.dataset.mode = 'login';
        renderAuthUI();
    };
    document.getElementById('signup-tab').onclick = () => {
        container.dataset.mode = 'signup';
        renderAuthUI();
    };

    const form = document.getElementById('auth-form');
    if (!form) return;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        console.log('[SEVCS AUTH] Submit triggered. Current Status:', appState.authStatus);
        
        const emailEl = document.getElementById('email');
        const passwordEl = document.getElementById('password');
        
        if (!emailEl || !passwordEl) return;

        const email = emailEl.value;
        const password = passwordEl.value;
        
        let name = null;
        let vehicleType = null;
        if (isSignup) {
            const nameEl = document.getElementById('name');
            const vehicleEl = document.getElementById('vehicleType');
            if (nameEl) name = nameEl.value;
            if (vehicleEl) vehicleType = vehicleEl.value;
        }

        if (appState.authStatus === 'AUTHENTICATING') {
            console.warn('[SEVCS AUTH] Already authenticating. Ignoring.');
            return;
        }
        
        appState.authStatus = 'AUTHENTICATING';
        renderAuthUI();

        try {
            let result;
            if (isSignup) {
                console.log('[SEVCS AUTH] Attempting Signup:', email);
                result = await signup({ name, email, password, vehicleType });
                
                if (result.success) {
                    container.dataset.mode = 'login';
                    appState.authStatus = 'GUEST';
                    renderAuthUI();
                    const errorEl = document.getElementById('auth-error');
                    if (errorEl) {
                        errorEl.textContent = 'Account created. Please login.';
                        errorEl.style.display = 'block';
                        errorEl.style.color = '#4CAF50';
                    }
                    return;
                }
            } else {
                console.log('[SEVCS AUTH] Attempting Login:', email);
                result = await login({ email, password });
            }

            if (result && !result.success) {
                console.error('[SEVCS AUTH] Auth Failed:', result.message);
                appState.authStatus = 'GUEST';
                renderAuthUI();
                const errorEl = document.getElementById('auth-error');
                if (errorEl) {
                    errorEl.textContent = result.message;
                    errorEl.style.display = 'block';
                }
            }
        } catch (error) {
            console.error('[SEVCS AUTH] Critical Auth Error:', error);
            appState.authStatus = 'GUEST';
            renderAuthUI();
            const errorEl = document.getElementById('auth-error');
            if (errorEl) {
                errorEl.textContent = error.message || 'Connection failed. Ensure backend is running.';
                errorEl.style.display = 'block';
            }
        }
    });
}

// Global Event Listeners
events.on('HARD_RESET_COMPLETE', renderAuthUI);
events.on('FORCE_LOGOUT', renderAuthUI);
events.on('STATE_UPDATED', renderAuthUI);
)rawliteral";
const char UI_FILE_components_grids_js[] PROGMEM = R"rawliteral(import { bookSlot, addSlot, removeSlot, updateSlotType } from '../app/api_v3.js';
import { events } from '../app/events.js';

export function initGrids() {
    const slotContainer = document.getElementById('slot-grid-container');
    const queueContainer = document.getElementById('queue-table-container');

    // Delegated click handler for slots
    slotContainer.addEventListener('click', async (e) => {
        // 1. Booking Action
        const slotCard = e.target.closest('.slot-card');
        if (slotCard && slotCard.classList.contains('interactive') && !e.target.closest('button')) {
            const slotId = slotCard.dataset.id;
            console.log(`[SEVCS] Requesting booking for slot ${slotId}`);
            await bookSlot(slotId);
            return;
        }

        // 2. Remove Slot (Admin)
        const btnRemove = e.target.closest('.btn-remove-slot');
        if (btnRemove) {
            const slotId = btnRemove.dataset.id;
            if (confirm(`Remove Slot ${slotId}?`)) {
                await removeSlot(slotId);
            }
            return;
        }

        // 3. Update Type (Admin)
        const btnType = e.target.closest('.btn-toggle-type');
        if (btnType) {
            const slotId = btnType.dataset.id;
            const currentType = btnType.dataset.type;
            const nextType = currentType === 'FAST' ? 'STANDARD' : 'FAST';
            await updateSlotType(slotId, nextType);
            return;
        }

        // 4. Global Admin Actions
        if (e.target.id === 'btn-add-slot') {
            await addSlot('STANDARD');
        }
    });

    let lastRenderedHash = null;

    events.on('STATE_UPDATED', (state) => {
        const snapshot = state.snapshot;
        const isAdmin = state.uiMode === 'ADMIN';

        // Performance Guard: Only re-render if data has actually changed
        const currentHash = snapshot ? `${snapshot.state_hash}_${snapshot.snapshot_sequence}_${isAdmin}` : 'empty';
        if (currentHash === lastRenderedHash) return;
        lastRenderedHash = currentHash;

        // 1. Render Slot Grid
        slotContainer.innerHTML = `
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem">
                    <h2 style="margin: 0">Charging Slots</h2>
                    ${isAdmin ? `
                        <button class="btn btn-outline btn-small" id="btn-add-slot">+ Add Slot</button>
                    ` : ''}
                </div>
                ${snapshot ? `
                    ${snapshot.slots.length > 0 ? `
                        <div class="grid-container">
                            ${snapshot.slots.map(slot => `
                                <div class="slot-card ${slot.state.toLowerCase()} ${state.allowActions && slot.state === 'FREE' ? 'interactive' : ''}" 
                                     data-id="${slot.slot_id}"
                                     title="${slot.state === 'FREE' ? 'Click to book' : ''}">
                                    
                                    <div style="display: flex; justify-content: space-between; align-items: flex-start">
                                        <div style="font-size: 0.7rem; color: var(--text-secondary)">ID: ${slot.slot_id}</div>
                                        ${isAdmin ? `
                                            <button class="btn-remove-slot" data-id="${slot.slot_id}" title="Remove Slot">Ã—</button>
                                        ` : ''}
                                    </div>

                                    <div style="font-weight: 700; margin: 4px 0">${slot.state}</div>
                                    <div class="mono" style="font-size: 0.75rem">${slot.assigned_global_id ? 'V-' + slot.assigned_global_id : '---'}</div>
                                    
                                    <div class="slot-type-badge ${slot.charger_type?.toLowerCase() || 'standard'}">
                                        ${slot.charger_type || 'STANDARD'}
                                    </div>

                                    ${isAdmin ? `
                                        <button class="btn-toggle-type" data-id="${slot.slot_id}" data-type="${slot.charger_type || 'STANDARD'}">
                                            âš™ï¸ Change Type
                                        </button>
                                    ` : ''}
                                </div>
                            `).join('')}
                        </div>
                    ` : '<div class="mono" style="color: var(--accent-red); padding: 1rem; border: 1px dashed; text-align: center;">EMPTY SYSTEM STATE â€“ NO ACTIVE SLOTS</div>'}
                ` : '<p class="mono" style="color: var(--text-secondary)">Scanning for available slots...</p>'}
            </div>
        `;

        // 2. Render Queue Table
        queueContainer.innerHTML = `
            <div class="card">
                <h2>Vehicle Queue</h2>
                ${snapshot && snapshot.queue.length === 0 ? `
                    <div class="mono" style="color: var(--accent-orange); padding: 1rem; border: 1px dashed; text-align: center;">EMPTY SYSTEM STATE â€“ NO ACTIVE TRACKS</div>
                ` : `
                <table class="status-table">
                    <thead>
                        <tr>
                            <th>Global ID</th>
                            <th>Track ID</th>
                            <th>State</th>
                            <th>Confidence</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${snapshot ? snapshot.queue.map(v => `
                            <tr>
                                <td class="mono">V-${v.global_id}</td>
                                <td class="mono">T-${v.track_id}</td>
                                <td>${v.state}</td>
                                <td>${(v.confidence * 100).toFixed(1)}%</td>
                            </tr>
                        `).join('') : '<tr><td colspan="4" style="text-align: center; color: var(--text-secondary)">Waiting for queue synchronization...</td></tr>'}
                    </tbody>
                </table>
                `}
            </div>
        `;
    });
}
)rawliteral";
const char UI_FILE_components_simulation_js[] PROGMEM = R"rawliteral(import { events } from '../app/events.js';
import { executeAction } from '../app/api_v3.js';
import { appState } from '../app/state_v3.js';

export function initSimulationUI() {
    const container = document.getElementById('simulation-panel-container');
    const actionContainer = document.getElementById('action-panel-container');

    events.on('STATE_UPDATED', (state) => render(state));

    let lastRenderedHash = null;

    function render(state = {}) {
        const isAdmin = appState.uiMode === 'ADMIN';

        const currentHash = `${isAdmin}_${appState.isSimulating}_${appState.requestHistory.length}`;
        if (currentHash === lastRenderedHash) return;
        lastRenderedHash = currentHash;

        if (!isAdmin) {
            container.classList.add('hidden');
            actionContainer.classList.add('hidden');
            return;
        }

        container.classList.remove('hidden');
        actionContainer.classList.remove('hidden');

        // Main Actions & Request History
        actionContainer.innerHTML = `
            <div class="card">
                <h2>Control Panel</h2>
                <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1.5rem">
                    <button class="btn" id="btn-auth" ${!appState.allowActions ? 'disabled' : ''}>Authorize</button>
                    <button class="btn" id="btn-book" ${!appState.allowActions ? 'disabled' : ''}>Book Slot</button>
                    <button class="btn" id="btn-release" ${!appState.allowActions ? 'disabled' : ''}>Release Slot</button>
                    <button class="btn btn-danger" id="btn-stop" 
                        ${state.displayState === 'DISCONNECTED' ? 'disabled' : ''}>
                        ${state.displayState === 'FROZEN' ? 'âš ï¸ SYSTEM HALTED' : 'EMERGENCY STOP'}
                    </button>
                </div>

                <h3>Request History</h3>
                <table class="history-table">
                    <thead>
                        <tr>
                            <th>Seq</th>
                            <th>Endpoint</th>
                            <th>Status</th>
                            <th>Ver</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${appState.requestHistory.length > 0 ? appState.requestHistory.map(entry => `
                            <tr>
                                <td class="mono">#${entry.sequence}</td>
                                <td>${entry.endpoint}</td>
                                <td><span class="status-tag ${entry.status.toLowerCase()}">${entry.status}</span></td>
                                <td class="mono">v${entry.snapshot_version}</td>
                            </tr>
                        `).join('') : '<tr><td colspan="4" style="text-align: center; color: var(--text-secondary)">No requests logged.</td></tr>'}
                    </tbody>
                </table>
            </div>
        `;

        // Simulation Actions
        container.innerHTML = `
            <div class="card">
                <h2>Failure Simulation</h2>
                <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem">
                    <button class="btn btn-outline" id="btn-sim-dup">Duplicate ID</button>
                    <button class="btn btn-outline" id="btn-sim-stale">Stale Version</button>
                    <button class="btn btn-outline" id="btn-sim-gap">Force Gap</button>
                </div>
                <div class="card" style="background: rgba(0,0,0,0.2); padding: 0.75rem; border-style: dashed">
                    <h3 style="font-size: 0.7rem; color: var(--text-secondary); margin-bottom: 0.5rem">IDEMPOTENCY TRACKER</h3>
                    <div class="mono" style="font-size: 0.8rem">
                        Last Decision: <span style="color: var(--accent-blue)">${appState.requestHistory[0]?.decision_id || '---'}</span><br>
                        Current Sync: <span style="color: var(--accent-green)">v${appState.snapshotVersion}</span>
                    </div>
                </div>
            </div>
        `;

        // Bind events
        document.getElementById('btn-auth')?.addEventListener('click', () => executeAction('authorize', {}));
        document.getElementById('btn-book')?.addEventListener('click', () => executeAction('book', {}));
        document.getElementById('btn-release')?.addEventListener('click', () => executeAction('release', {}));
        document.getElementById('btn-stop')?.addEventListener('click', () => executeAction('emergency_stop', {}));

        document.getElementById('btn-sim-dup')?.addEventListener('click', () => {
            const payload = { simulated_duplicate: true };
            executeAction('authorize', payload);
            executeAction('authorize', payload);
        });

        document.getElementById('btn-sim-stale')?.addEventListener('click', () => {
            const originalVersion = appState.snapshotVersion;
            appState.snapshotVersion -= 10;
            executeAction('authorize', { simulated_stale: true });
            appState.snapshotVersion = originalVersion;
        });

        document.getElementById('btn-sim-gap')?.addEventListener('click', () => {
            events.emit('FORCE_GAP_SIMULATION');
        });
    }

    render();
}
)rawliteral";
const char UI_FILE_components_system_ui_js[] PROGMEM = R"rawliteral(import { events } from '../app/events.js';

export function initSystemUI() {
    const statusContainer = document.getElementById('live-status-container');
    const debugContainer = document.getElementById('debug-panel-container');
    const banner = document.getElementById('status-banner');
    const bannerMsg = document.getElementById('banner-message');
    const confidenceTag = document.getElementById('ui-confidence-tag');
    
    const snapshotInfo = document.getElementById('snapshot-info');
    const healthIndicator = document.getElementById('ui-health-indicator');
    const modeToggle = document.querySelector('.mode-toggle');
    const btnAdmin = document.getElementById('btn-mode-admin');
    const btnUser = document.getElementById('btn-mode-user');
    const adminControls = document.getElementById('admin-controls');
    events.on('STATE_UPDATED', (state) => {
        const snapshot = state.snapshot;
        
        // 2. Role-Based Visibility Pruning
        const isAdmin = state.uiMode === 'ADMIN';
        
        if (isAdmin) {
            snapshotInfo?.classList.remove('hidden');
            healthIndicator?.classList.remove('hidden');
            confidenceTag?.classList.remove('hidden');
            modeToggle?.classList.add('hidden'); // Permanently hidden as requested
            adminControls?.classList.remove('hidden');
        } else {
            snapshotInfo?.classList.add('hidden');
            healthIndicator?.classList.add('hidden');
            confidenceTag?.classList.add('hidden');
            modeToggle?.classList.add('hidden');
            adminControls?.classList.add('hidden');
        }

        if (snapshot) {
            snapshotInfo.innerText = `Snapshot: v${snapshot.snapshot_version} | Seq #${snapshot.snapshot_sequence}`;
        }

        const healthScore = state.healthScore || 0;
        let healthLabel = 'CRITICAL';
        if (healthScore > 80) healthLabel = 'GOOD';
        else if (healthScore > 40) healthLabel = 'DEGRADED';
        
        healthIndicator.className = `health-indicator ${healthLabel.toLowerCase()}`;
        healthIndicator.innerText = `HEALTH: ${healthLabel} (${healthScore}%)`;

        // 3. Mode Toggle Protection & Styling
        const canSwitch = state.displayState === 'SYNCHRONIZED';
        btnAdmin.disabled = !canSwitch;
        btnUser.disabled = !canSwitch;
        btnAdmin.className = `btn-toggle ${state.uiMode === 'ADMIN' ? 'active' : ''}`;
        btnUser.className = `btn-toggle ${state.uiMode === 'USER' ? 'active' : ''}`;

        // 3. Priority Banner System: FROZEN_UNKNOWN > FROZEN > DESYNC > DEGRADED > DISCONNECTED
        let bannerActive = true;
        const displayState = state.displayState;

        if (displayState === 'FROZEN_UNKNOWN') {
            banner.className = 'banner banner-frozen';
            bannerMsg.innerText = 'SYSTEM FROZEN - Last action status unknown. Verify state after recovery.';
        } else if (displayState === 'FROZEN') {
            banner.className = 'banner banner-frozen';
            bannerMsg.innerText = `SYSTEM FROZEN: ${snapshot?.freeze_reason || 'EMERGENCY STOP'}`;
        } else if (displayState === 'RESYNC_REQUIRED') {
            banner.className = 'banner banner-desync';
            bannerMsg.innerText = 'CRITICAL DESYNC: MANUAL RESYNC REQUIRED';
        } else if (displayState === 'DESYNCHRONIZED') {
            banner.className = 'banner banner-desync';
            bannerMsg.innerText = 'UI DESYNCHRONIZED - MONOTONICITY GAP DETECTED';
        } else if (state.systemState === 'INVALID') {
            banner.className = 'banner banner-invalid';
            bannerMsg.innerText = 'ðŸ›‘ CRITICAL: NO SLOTS OR QUEUE - SYSTEM INVALID';
        } else if (displayState === 'DEGRADED') {
            banner.className = 'banner banner-desync';
            bannerMsg.innerText = 'NETWORK DEGRADED - ADAPTIVE TIMEOUT ACTIVE';
        } else if (displayState === 'DISCONNECTED' && state.authStatus !== 'GUEST') {
            banner.className = 'banner banner-frozen';
            bannerMsg.innerText = 'BACKEND DISCONNECTED - SEARCHING FOR COORDINATOR';
        } else {
            bannerActive = false;
        }

        // Final Banner Guard (Admin Only for desync info)
        if (!isAdmin && (displayState === 'DESYNCHRONIZED' || displayState === 'DEGRADED')) {
            bannerActive = false;
        }

        if (bannerActive) banner.classList.remove('hidden');
        else banner.classList.add('hidden');

        // 4. Render Status Panel (ADMIN ONLY)
        if (state.uiMode === 'ADMIN') {
            statusContainer.classList.remove('hidden');
            statusContainer.innerHTML = `
                <div class="card">
                    <h2>System Status</h2>
                    ${snapshot ? `
                        <div class="mono">
                            <p>Mode: <span style="color: var(--accent-blue)">${snapshot.system_mode}</span></p>
                            <p>Health: ${snapshot.system_health}%</p>
                            <p>Sync Score: ${state.healthScore}%</p>
                            <p>Stable: ${state.stableFrames}/10</p>
                        </div>
                    ` : '<p class="mono" style="color: var(--text-secondary)">Waiting for backend data...</p>'}
                </div>
            `;
        } else {
            statusContainer.classList.add('hidden');
        }

        // 5. Render Debug Panel (ADMIN ONLY)
        if (state.uiMode === 'ADMIN') {
            debugContainer.classList.remove('hidden');
            debugContainer.innerHTML = `
                <div class="card">
                    <h2>Telemetry</h2>
                    <div class="mono" style="font-size: 0.75rem">
                        <p>Latency: ${state.lastSequence !== -1 ? Math.round(performance.now() - state.lastSnapshotMono) : '---'}ms</p>
                        <p>Stable: ${state.stableFrames} frames</p>
                        <p>Resyncing: ${state.isResyncing ? 'YES' : 'NO'}</p>
                        <p>Simulation: ${state.isSimulating ? 'ACTIVE' : 'OFF'}</p>
                    </div>
                </div>
            `;
        } else {
            debugContainer.classList.add('hidden');
        }
    });

    events.on('RENDER_FALLBACK', () => {
        // ... (Keep existing fallback)
        const root = document.body;
        root.innerHTML = `
            <div style="background: #0a0a0a; color: #ff3333; height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; font-family: 'Inter', sans-serif; text-align: center; padding: 2rem;">
                <h1 style="font-size: 3rem; margin-bottom: 1rem; color: #ff0000;">âš ï¸ SYSTEM DEGRADED</h1>
                <p style="font-size: 1.5rem; color: #cccccc; max-width: 600px; line-height: 1.6;">
                    A terminal rendering failure has been detected. Dashboard safe-mode active.
                </p>
                <button onclick="location.reload()" style="margin-top: 2rem; background: #ff3333; color: white; border: none; padding: 1rem 2rem; border-radius: 4px; cursor: pointer; font-weight: bold;">
                    MANUAL SYSTEM RELOAD
                </button>
            </div>
        `;
    });
}
)rawliteral";
const char UI_FILE_components_user_ui_js[] PROGMEM = R"rawliteral(import { executeAction, startPolling } from '../app/api_v3.js';
import { appState, resync } from '../app/state_v3.js';
import { events } from '../app/events.js';

export function initUserUI() {
    const container = document.getElementById('user-ui-container');
    let isLoading = false;
    let lastResult = null;

    function render(state = {}) {
        const displayState = state.displayState || 'INITIALIZING';
        const isUser = appState.uiMode === 'USER' && appState.authStatus !== 'GUEST';
        if (!isUser) {
            container.classList.add('hidden');
            return;
        }

        container.classList.remove('hidden');

        // 0. Synchronization Check
        if (appState.isResyncing) {
            container.innerHTML = `
                <div class="auth-card glass" style="max-width: 600px; margin: 2rem auto; text-align: center;">
                    <div class="error-icon" style="font-size: 3rem; margin-bottom: 1rem;">âš ï¸</div>
                    <h2>Resync Required</h2>
                    <p>System state discontinuity detected. A manual re-initialization is required to ensure deterministic safety.</p>
                    <button class="primary-btn" id="resync-btn" style="margin-top: 1.5rem">Initialize Resync Pipeline</button>
                </div>
            `;
            document.getElementById('resync-btn').onclick = () => {
                resync();
                startPolling();
            };
            return;
        }

        if (appState.authStatus === 'AUTHENTICATED_PENDING' || !appState.snapshot) {
            container.innerHTML = `
                <div class="card glass" style="text-align: center; padding: 4rem;">
                    <div class="spinner"></div>
                    <h2 style="margin-top: 1rem">Syncing System Data...</h2>
                    <p>Waiting for first valid monotonic snapshot from backend.</p>
                </div>
            `;
            return;
        }

        const snapshot = appState.snapshot;
        const wallet = snapshot.user_wallet || { balance: 0, currency: 'USD' };
        
        // Sorting: Ensure stable DOM order
        const sortedSlots = [...snapshot.slots].sort((a, b) => a.slot_id - b.slot_id);
        const sortedQueue = [...snapshot.queue].sort((a, b) => (a.global_id || 0) - (b.global_id || 0));

        container.innerHTML = `
            <div class="user-dashboard-grid">
                <!-- Wallet Section -->
                <div class="card wallet-card glass">
                    <div class="wallet-header">
                        <h3>Your Wallet</h3>
                        <span class="wallet-id">ID: ${appState.session.userId}</span>
                    </div>
                    <div class="balance-area">
                        <span class="currency">$</span>
                        <span class="balance">${wallet.balance.toFixed(2)}</span>
                    </div>
                    <div class="wallet-actions">
                        <button class="primary-btn btn-small" id="btn-recharge" 
                            ${!appState.allowActions || appState.pendingIntents.has('recharge') ? 'disabled' : ''}>
                            ${appState.pendingIntents.get('recharge')?.status === 'PENDING' ? 'PROCESSING...' : 'Quick Recharge $50'}
                        </button>
                    </div>
                    ${appState.pendingIntents.get('recharge')?.status === 'UNKNOWN' ? `
                        <p class="status-msg warning">âš ï¸ Last recharge status unknown. Please verify state.</p>
                    ` : ''}
                </div>

                <!-- Slot Overview -->
                <div class="card slots-card glass">
                    <div class="card-header">
                        <h3>Available Slots</h3>
                        <span class="count-badge">${snapshot.slots.filter(s => s.state === 'FREE').length} Free</span>
                    </div>
                    <div class="slot-grid">
                        ${sortedSlots.map(slot => `
                            <div class="slot-item ${slot.state.toLowerCase()}">
                                <div class="slot-info">
                                    <span class="slot-label">Slot ${slot.slot_id}</span>
                                    <span class="slot-status">${slot.state}</span>
                                </div>
                                ${slot.state === 'FREE' ? `
                                    <button class="btn-charge" data-slot-id="${slot.slot_id}"
                                        ${!appState.allowActions || appState.pendingIntents.has(`book_slot_${slot.slot_id}`) ? 'disabled' : ''}>
                                        ${appState.pendingIntents.get(`book_slot_${slot.slot_id}`)?.status === 'PENDING' ? '...' : 'Charge'}
                                    </button>
                                ` : `
                                    <div class="assigned-user">ID: ${slot.assigned_global_id || '---'}</div>
                                `}
                            </div>
                        `).join('')}
                    </div>
                </div>

                <!-- Allocation Controls -->
                <div class="card actions-card glass">
                    <h3>Smart Allocation</h3>
                    <div class="form-group">
                        <label>Vehicle Type</label>
                        <select id="user-vehicle" ${isLoading ? 'disabled' : ''}>
                            <option value="SUV">SUV</option>
                            <option value="Sedan">Sedan</option>
                            <option value="Truck">Truck</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Urgency</label>
                        <select id="user-urgency" ${isLoading ? 'disabled' : ''}>
                            <option value="LOW">Low</option>
                            <option value="HIGH">High</option>
                        </select>
                    </div>
                    <button class="primary-btn" id="btn-find-slot" 
                        ${!appState.allowActions || isLoading ? 'disabled' : ''}>
                        ${isLoading ? 'PROCESSING...' : 'Find Best Slot'}
                    </button>
                    
                    <div id="user-result-area">
                        ${lastResult ? `
                            <div class="result-box ${lastResult.success ? 'success' : 'fail'}">
                                <h4>${lastResult.success ? 'ALLOCATION SUCCESS' : 'ALLOCATION FAILED'}</h4>
                                <p>${lastResult.success ? `Slot ${lastResult.assigned_slot} reserved` : lastResult.error}</p>
                            </div>
                        ` : ''}
                    </div>
                </div>
            </div>
        `;

        // Event Listeners
        document.getElementById('btn-recharge')?.addEventListener('click', () => {
            executeAction('recharge', { amount: 50 }, 'recharge');
        });

        document.querySelectorAll('.btn-charge').forEach(btn => {
            btn.onclick = () => {
                const slotId = btn.dataset.slotId;
                executeAction('book_slot', { slot_id: slotId }, `book_slot_${slotId}`);
            };
        });

        document.getElementById('btn-find-slot')?.addEventListener('click', () => {
            isLoading = true;
            render();
            const payload = {
                type: document.getElementById('user-vehicle').value,
                urgency: document.getElementById('user-urgency').value
            };
            executeAction('find_slot', payload, 'find_slot');
        });
    }

    events.on('ACTION_RESPONSE', (data) => {
        if (data.endpoint === 'find_slot') {
            isLoading = false;
            if (data.status !== 'REJECTED') {
                lastResult = {
                    success: true,
                    assigned_slot: data.payload?.assigned_slot || 'AUTO-01',
                    status: data.status,
                    version: data.snapshot_version
                };
            } else {
                lastResult = {
                    success: false,
                    error: data.error?.reason || 'Unknown decision rejection'
                };
            }
            render();
        }
    });

    events.on('STATE_UPDATED', (state) => render(state));
    events.on('ACTIONS_CHANGED', () => render());
    events.on('API_ERROR', () => {
        isLoading = false;
        render();
    });
    render();
}
)rawliteral";
const char UI_FILE_styles_style_css[] PROGMEM = R"rawliteral(:root {
    --bg-dark: #0f1115;
    --panel-bg: #1a1d23;
    --border-color: #2e323b;
    --text-primary: #e6e8eb;
    --text-secondary: #9ea4b0;
    --accent-blue: #3b82f6;
    --accent-green: #10b981;
    --accent-red: #ef4444;
    --accent-orange: #f59e0b;
    --accent-yellow: #fbbf24;
    --font-sans: 'Inter', system-ui, -apple-system, sans-serif;
    --font-mono: 'JetBrains Mono', monospace;
    --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
    --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
}

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    background-color: var(--bg-dark);
    color: var(--text-primary);
    font-family: var(--font-sans);
    line-height: 1.5;
    overflow-x: hidden;
}

/* Banner System */
.banner {
    width: 100%;
    padding: 10px;
    text-align: center;
    font-weight: 700;
    font-size: 0.9rem;
    z-index: 1000;
    position: sticky;
    top: 0;
}

.banner.hidden { display: none; }
.banner-frozen { background-color: var(--accent-red); color: white; }
.banner-desync { background-color: var(--accent-orange); color: white; }
.banner-invalid { background-color: #000; color: var(--accent-red); border: 2px solid var(--accent-red); }

/* Header */
.main-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem 2rem;
    border-bottom: 1px solid var(--border-color);
    background: rgba(15, 17, 21, 0.8);
    backdrop-filter: blur(8px);
    position: sticky;
    top: 0;
    z-index: 900;
}

.logo-area h1 {
    font-size: 1.25rem;
    font-weight: 800;
    letter-spacing: -0.025em;
}

.logo-area h1 span {
    display: block;
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

.confidence-tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 700;
    margin-top: 4px;
}

.confidence-tag.synchronized { background: rgba(16, 185, 129, 0.2); color: var(--accent-green); }
.confidence-tag.degraded { background: rgba(245, 158, 11, 0.2); color: var(--accent-orange); }
.confidence-tag.desynchronized { background: rgba(239, 68, 68, 0.2); color: var(--accent-red); }

/* Header Improvements */
.header-center {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
}

.header-info {
    font-size: 0.8rem;
    color: var(--text-secondary);
}

.health-indicator {
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 0.65rem;
    font-weight: 800;
    text-transform: uppercase;
}

.health-indicator.good { background: rgba(16, 185, 129, 0.1); color: var(--accent-green); border: 1px solid var(--accent-green); }
.health-indicator.degraded { background: rgba(245, 158, 11, 0.1); color: var(--accent-orange); border: 1px solid var(--accent-orange); }
.health-indicator.critical { background: rgba(239, 68, 68, 0.1); color: var(--accent-red); border: 1px solid var(--accent-red); }

.header-actions {
    display: flex;
    align-items: center;
    gap: 1.5rem;
}

.mode-toggle {
    display: flex;
    background: rgba(0,0,0,0.3);
    padding: 2px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
}

.btn-toggle {
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 0.75rem;
    font-weight: 700;
    border: none;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.2s;
}

.btn-toggle.active {
    background: var(--accent-blue);
    color: white;
}

.btn-toggle:disabled {
    opacity: 0.3;
    cursor: not-allowed;
}

/* Dashboard Layout */
.dashboard {
    display: grid;
    grid-template-columns: 350px 1fr;
    gap: 2rem;
    padding: 2rem;
    max-width: 1600px;
    margin: 0 auto;
}

.control-panel, .data-panel {
    display: flex;
    flex-direction: column;
    gap: 2rem;
}

/* Card Styling */
.card {
    background: var(--panel-bg);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 1.5rem;
    box-shadow: var(--shadow-md);
}

.card h2 {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 1rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

/* Grid & Table */
.grid-container {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
    gap: 1rem;
}

.slot-card {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
    text-align: center;
    transition: all 0.2s ease;
    position: relative;
}

.slot-card.interactive {
    cursor: pointer;
}

.slot-card.interactive:hover {
    background: rgba(255, 255, 255, 0.08);
    transform: translateY(-2px);
    border-color: var(--accent-blue);
}

.slot-card.charging { border-color: var(--accent-green); box-shadow: 0 0 10px rgba(16, 185, 129, 0.2); }
.slot-card.reserved { border-color: var(--accent-blue); }

.slot-type-badge {
    display: inline-block;
    font-size: 0.6rem;
    padding: 2px 6px;
    border-radius: 4px;
    font-weight: 800;
    margin-top: 8px;
    text-transform: uppercase;
}

.slot-type-badge.fast {
    background: rgba(245, 158, 11, 0.2);
    color: #f59e0b;
    border: 1px solid rgba(245, 158, 11, 0.3);
}

.slot-type-badge.standard {
    background: rgba(59, 130, 246, 0.2);
    color: #3b82f6;
    border: 1px solid rgba(59, 130, 246, 0.3);
}

.btn-remove-slot {
    background: none;
    border: none;
    color: var(--accent-red);
    cursor: pointer;
    font-size: 1.2rem;
    padding: 0 4px;
    line-height: 1;
    opacity: 0.5;
    transition: opacity 0.2s;
}

.btn-remove-slot:hover {
    opacity: 1;
}

.btn-toggle-type {
    display: block;
    width: 100%;
    margin-top: 10px;
    background: rgba(255,255,255,0.05);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    font-size: 0.65rem;
    padding: 4px;
    border-radius: 4px;
    cursor: pointer;
}

.btn-toggle-type:hover {
    background: rgba(255,255,255,0.1);
    color: white;
}

.btn-toggle-type:focus-visible {
    outline: 2px solid var(--accent-blue);
    outline-offset: 2px;
}

.btn-toggle-type:active {
    transform: scale(0.95);
}

.btn:focus-visible {
    outline: 2px solid var(--accent-blue);
    outline-offset: 2px;
}

.btn:active {
    transform: scale(0.98);
}

.status-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.875rem;
}

.status-table th {
    text-align: left;
    color: var(--text-secondary);
    padding: 0.75rem;
    border-bottom: 1px solid var(--border-color);
}

.status-table td {
    padding: 0.75rem;
    border-bottom: 1px solid var(--border-color);
}

/* Buttons */
.btn {
    padding: 0.625rem 1rem;
    border-radius: 6px;
    font-weight: 600;
    font-size: 0.875rem;
    cursor: pointer;
    transition: all 0.2s;
    border: none;
    background: var(--accent-blue);
    color: white;
}

.btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    filter: grayscale(1);
}

.btn-danger { background: var(--accent-red); }
.btn-outline { background: transparent; border: 1px solid var(--border-color); color: var(--text-primary); }

/* Forms */
.form-group {
    margin-bottom: 1rem;
}

.form-group label {
    display: block;
    font-size: 0.75rem;
    margin-bottom: 0.5rem;
    color: var(--text-secondary);
}

.form-group input, .form-group select {
    width: 100%;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 0.5rem;
    color: var(--text-primary);
}

/* Mono Text */
.mono {
    font-family: var(--font-mono);
    font-size: 0.85rem;
}

/* Request History Styles */
.history-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.7rem;
}

.history-table th {
    text-align: left;
    color: var(--text-secondary);
    padding: 0.5rem;
    border-bottom: 1px solid var(--border-color);
}

.history-table td {
    padding: 0.5rem;
    border-bottom: 1px solid var(--border-color);
}

.status-tag {
    padding: 1px 4px;
    border-radius: 3px;
    font-weight: 700;
}

.status-tag.new { background: rgba(16, 185, 129, 0.2); color: var(--accent-green); }
.status-tag.replayed { background: rgba(59, 130, 246, 0.2); color: var(--accent-blue); }
.status-tag.rejected { background: rgba(239, 68, 68, 0.2); color: var(--accent-red); }
.status-tag.unknown { background: rgba(245, 158, 11, 0.2); color: var(--accent-orange); }

/* Auth Overlay */
.auth-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(15, 17, 21, 0.95);
    z-index: 10000;
    display: flex;
    justify-content: center;
    align-items: center;
    backdrop-filter: blur(10px);
}

.glass {
    background: rgba(26, 29, 35, 0.7);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255, 255, 255, 0.1);
}

.auth-card {
    width: 100%;
    max-width: 400px;
    padding: 2.5rem;
    border-radius: 20px;
    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
    text-align: center;
}

.auth-card h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
.subtitle { color: var(--text-secondary); font-size: 0.85rem; margin-bottom: 2rem; }

.auth-tabs {
    display: flex;
    gap: 1rem;
    margin-bottom: 2rem;
    justify-content: center;
}

.tab-btn {
    background: transparent;
    border: none;
    color: var(--text-secondary);
    font-weight: 600;
    cursor: pointer;
    padding: 5px 10px;
    border-bottom: 2px solid transparent;
}

.tab-btn.active {
    color: var(--accent-blue);
    border-bottom-color: var(--accent-blue);
}

.auth-form { text-align: left; }
.input-group { margin-bottom: 1.25rem; }
.input-group label { display: block; font-size: 0.75rem; margin-bottom: 0.5rem; color: var(--text-secondary); }
.input-group input, .input-group select {
    width: 100%;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.75rem;
    color: var(--text-primary);
}

.primary-btn {
    width: 100%;
    padding: 0.75rem;
    background: var(--accent-blue);
    color: white;
    border: none;
    border-radius: 8px;
    font-weight: 700;
    cursor: pointer;
    transition: transform 0.2s;
}

.primary-btn:hover { transform: translateY(-2px); }
.primary-btn:disabled { opacity: 0.5; transform: none; }

.error-msg { color: var(--accent-red); font-size: 0.8rem; margin-top: 1rem; }

/* User Dashboard Grid */
.user-dashboard-grid {
    display: grid;
    grid-template-columns: 1fr 1.5fr;
    gap: 1.5rem;
}

.wallet-card { grid-column: span 1; }
.slots-card { grid-column: span 1; grid-row: span 2; }
.actions-card { grid-column: span 1; }

.wallet-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
.wallet-id { font-size: 0.7rem; color: var(--text-secondary); }
.balance-area { font-size: 2.5rem; font-weight: 800; margin: 1rem 0; }
.currency { color: var(--accent-green); font-size: 1.5rem; vertical-align: top; margin-right: 4px; }

.slot-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    gap: 1rem;
    max-height: 500px;
    overflow-y: auto;
    padding-right: 10px;
}

.slot-item {
    background: rgba(255, 255, 255, 0.03);
    border-radius: 10px;
    padding: 1rem;
    border: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
}

.slot-item.free { border-color: rgba(16, 185, 129, 0.3); }
.slot-item.charging { border-color: var(--accent-green); }
.slot-item.reserved { border-color: var(--accent-blue); }

.slot-info { display: flex; flex-direction: column; }
.slot-label { font-size: 0.7rem; color: var(--text-secondary); }
.slot-status { font-size: 0.8rem; font-weight: 700; }

.btn-charge {
    background: var(--accent-green);
    color: white;
    border: none;
    border-radius: 4px;
    padding: 4px;
    font-size: 0.7rem;
    font-weight: 700;
    cursor: pointer;
}

.btn-logout {
    background: rgba(239, 68, 68, 0.1);
    color: var(--accent-red);
    border: 1px solid var(--accent-red);
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 0.75rem;
    font-weight: 700;
    cursor: pointer;
}

.status-msg.warning { color: var(--accent-orange); font-size: 0.7rem; margin-top: 0.5rem; }

.spinner {
    width: 40px;
    height: 40px;
    border: 4px solid rgba(255, 255, 255, 0.1);
    border-left-color: var(--accent-blue);
    border-radius: 50%;
    animation: spin 1s linear infinite;
    margin: 0 auto 1.5rem;
}

@keyframes spin { to { transform: rotate(360deg); } }

.hidden { display: none !important; }
)rawliteral";
const UIFile ui_files[] = {
    {"/index.html", UI_FILE_index_html, "text/html"},
    {"/app/api_v3.js", UI_FILE_app_api_v3_js, "application/javascript"},
    {"/app/app.js", UI_FILE_app_app_js, "application/javascript"},
    {"/app/events.js", UI_FILE_app_events_js, "application/javascript"},
    {"/app/renderer.js", UI_FILE_app_renderer_js, "application/javascript"},
    {"/app/state_v3.js", UI_FILE_app_state_v3_js, "application/javascript"},
    {"/components/auth_ui.js", UI_FILE_components_auth_ui_js, "application/javascript"},
    {"/components/grids.js", UI_FILE_components_grids_js, "application/javascript"},
    {"/components/simulation.js", UI_FILE_components_simulation_js, "application/javascript"},
    {"/components/system_ui.js", UI_FILE_components_system_ui_js, "application/javascript"},
    {"/components/user_ui.js", UI_FILE_components_user_ui_js, "application/javascript"},
    {"/styles/style.css", UI_FILE_styles_style_css, "text/css"},
};
const int ui_file_count = 12;
#endif
