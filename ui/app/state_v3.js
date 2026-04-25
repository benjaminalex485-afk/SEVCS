import { events } from './events.js';
import { resetPendingActions } from './api_v3.js';

console.log('[SEVCS] STATE_V3 LOADED - VERIFYING LATENCY FIX AT L402');
export const appState = {
    // System Status
    snapshot: null,
    lastSequence: -1,
    snapshotVersion: 0,
    systemState: 'VALID', // VALID | INVALID (empty queue AND slots)
    
    // Health & Determinism
    uiState: 'DISCONNECTED', // SYNCHRONIZED | DEGRADED | DESYNCHRONIZED | DISCONNECTED | RESYNC_REQUIRED
    uiHealth: 'CRITICAL', // GOOD | DEGRADED | CRITICAL
    stagnantCounter: 0,
    hasGap: false,
    isSynchronized: false,
    stabilityCounter: 0,
    recoveryCounter: 0, // 3-frame stability for desync recovery
    stableFrames: 0, // Sequence stability counter
    lastSnapshotTime: 0, // Wall clock for freshness
    lastDesyncFrameTime: 0,
    lastUpdateTimestamp: 0,
    latency: 0,
    timeOffset: 0, // EMA for long-term clock drift
    lastClockSyncTime: 0,
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
    
    // Constants
    MAX_STAGNANT_FRAMES: 5,
    MAX_LATENCY_THRESHOLD: 1000, 
    BACKEND_STABILITY_FRAMES: 2,
    MAX_SNAPSHOT_AGE: 5000, 
    SNAPSHOT_FRESHNESS_MS: 1000,
    INTENT_TIMEOUT_MS: 5000,
    MAX_ALLOWED_GAP: 5,
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

    // 🔥 HARD RESYNC (Periodic Anchor)
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
    if (appState.uiState === 'RESYNC_REQUIRED') {
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
    if (timestamp < 1e12) timestamp *= 1000; 

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
        console.warn('[SEVCS] SNAPSHOT REJECTED: Stale Data', { age, offset: appState.timeOffset });
        appState.stableBackendFrames = 0; 
        return;
    }

    // Future: Reject drift
    if (timestamp - adjustedNow > 3000) {
        console.error('[SEVCS] SNAPSHOT REJECTED: Future Timestamp');
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
        appState.uiState = 'DESYNCHRONIZED';
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
            appState.uiState = 'DESYNCHRONIZED';
        } else if (simTime > appState.SIM_WARNING_THRESHOLD) {
            appState.uiState = 'SIMULATION_EXPIRING';
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
    if (appState.uiState === 'DESYNCHRONIZED' && latestIncomingSnapshot) {
        if (!appState.lastDesyncFrameTime) appState.lastDesyncFrameTime = latestIncomingSnapshot.timestamp;
        
        // Hysteresis: Only hard sync if no recovery progress is being made
        const desyncDuration = latestIncomingSnapshot.timestamp - appState.lastDesyncFrameTime;
        if (desyncDuration > 2000 && appState.recoveryCounter === 0) {
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
    const next = {
        uiState: appState.uiState,
        uiHealth: appState.uiHealth,
        systemState: 'VALID',
        stagnantCounter: appState.stagnantCounter,
        stabilityCounter: appState.stabilityCounter,
        recoveryCounter: appState.recoveryCounter,
        hasGap: appState.hasGap,
        criticalLatencyCycles: appState.criticalLatencyCycles
    };

    const newSequence = data.snapshot_sequence;
    const oldSequence = appState.lastSequence;

    // 1. Snapshot Integrity (Monotonicity & User Scoping)
    if (data.user_id && appState.session.userId && data.user_id !== appState.session.userId) {
        console.error('[SEVCS] USER SCOPE VIOLATION: Triggering logout');
        events.emit('FORCE_LOGOUT');
        return;
    }

    if (newSequence <= oldSequence && oldSequence !== -1) {
        next.stagnantCounter++;
        return; 
    }

    // 2. Dual-Mode Sequence & Gap Threshold Awareness
    const gap = newSequence - oldSequence;
    const isNext = gap === 1 || oldSequence === -1;

    if (isNext) {
        next.stableFrames++;
        if (next.uiState === 'DESYNCHRONIZED' && next.stableFrames >= appState.RECOVERY_THRESHOLD) {
            next.uiState = 'SYNCHRONIZED';
            next.hasGap = false;
        }
        next.isSynchronized = true;
        next.stagnantCounter = 0;
    } else if (gap > 1) {
        next.stableFrames = 0;
        next.isSynchronized = false;
        
        if (gap > appState.MAX_ALLOWED_GAP) {
            console.warn(`[SEVCS] MASSIVE GAP DETECTED (${gap}): RESYNC REQUIRED`);
            performHardSync(null);
            return;
        } else {
            next.uiState = 'DESYNCHRONIZED';
            next.hasGap = true;
        }
    }

    // 3. Stability Tracking
    const stateChanged = (
        !appState.snapshot ||
        data.snapshot_sequence !== appState.lastSequence ||
        data.snapshot_version !== appState.snapshotVersion ||
        data.state_hash !== appState.snapshot.state_hash
    );
    if (!stateChanged) {
        next.stabilityCounter++;
    } else {
        next.stabilityCounter = 0;
    }

    // 4. Latency Classification (Smoothed)
    appState.latencyBuffer.push(data.latency);
    if (appState.latencyBuffer.length > 10) appState.latencyBuffer.shift();
    
    const avgLatency = appState.latencyBuffer.reduce((a, b) => a + b, 0) / appState.latencyBuffer.length;
    
    if (avgLatency < 150) next.uiHealth = 'GOOD';
    else if (avgLatency < 400) next.uiHealth = 'DEGRADED';
    else next.uiHealth = 'CRITICAL';

    if (next.uiHealth === 'CRITICAL') {
        next.criticalLatencyCycles++;
    } else {
        next.criticalLatencyCycles = 0;
    }

    // 5. Empty State Handling
    const queueEmpty = data.queue.length === 0;
    const slotsEmpty = data.slots.length === 0;
    if (queueEmpty && slotsEmpty) {
        next.systemState = 'INVALID';
    }

    // 6. Transition Logging
    if (oldSequence !== -1) {
        appState.transitionLog.push({
            prev_sequence: oldSequence,
            new_sequence: newSequence,
            delta: newSequence - oldSequence,
            transition: `${appState.uiState} -> ${next.uiState}`,
            timestamp: Date.now()
        });
        if (appState.transitionLog.length > 50) appState.transitionLog.shift();
    }

    // 7. Atomic Commit
    appState.snapshot = data;
    appState.lastSequence = newSequence;
    appState.snapshotVersion = data.snapshot_version;
    appState.lastProcessedSource = data.source;
    appState.lastUpdateTimestamp = Date.now();
    appState.lastSnapshotTime = Date.now(); // Wall clock freshness
    appState.latency = data.latency; // Deterministic latency mapping
    
    appState.uiState = next.uiState;
    appState.uiHealth = next.uiHealth;
    appState.systemState = next.systemState;
    appState.stagnantCounter = next.stagnantCounter;
    appState.stabilityCounter = next.stabilityCounter;
    appState.recoveryCounter = next.recoveryCounter;
    appState.stableFrames = next.stableFrames;
    appState.isSynchronized = next.isSynchronized;
    appState.hasGap = next.hasGap;
    appState.criticalLatencyCycles = next.criticalLatencyCycles;

    // First Valid Snapshot Transition
    if (appState.authStatus === 'AUTHENTICATED_PENDING' && appState.isSynchronized) {
        appState.authStatus = 'AUTHENTICATED';
    }

    // 8. Interlocks (Strict Deterministic Gate)
    appState.allowActions = (
        appState.authStatus === 'AUTHENTICATED' &&
        appState.isSynchronized &&
        !data.freeze_state &&
        (Date.now() - appState.lastSnapshotTime < appState.SNAPSHOT_FRESHNESS_MS)
    );
}

/**
 * Hard Sync: System-triggered lock on major inconsistency.
 */
function performHardSync(authoritativeData) {
    if (authoritativeData && appState.lastSyncVersion === authoritativeData.snapshot_version) return;

    console.warn('[SEVCS] HARD SYNC: Transitioning to RESYNC_REQUIRED');
    
    latestIncomingSnapshot = null; 

    // Lock UI
    appState.isSynchronized = false;
    appState.uiState = 'RESYNC_REQUIRED';
    appState.snapshot = null;
    appState.lastSequence = -1;
    appState.stableFrames = 0;
    
    // Reset Derived State
    appState.isSimulating = false;
    appState.stableBackendFrames = 0;
    appState.stabilityCounter = 0;
    appState.stagnantCounter = 0;
    appState.recoveryCounter = 0;
    appState.hasGap = false;
    appState.lastSyncVersion = authoritativeData ? authoritativeData.snapshot_version : -1;
    
    events.emit('RESYNC_REQUIRED');
}

/**
 * Resync: User-triggered recovery without killing session.
 */
export function resync() {
    console.warn('[SEVCS] USER RESYNC: Re-initializing pipeline');
    appState.snapshot = null;
    appState.lastSequence = -1;
    appState.stableFrames = 0;
    appState.isSynchronized = false;
    appState.uiState = 'DISCONNECTED';
    
    // Explicitly preserve session and restart polling
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
    appState.uiState = 'DISCONNECTED';
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
    appState.uiState = 'DEGRADED';
    
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
            slots: [
                {slot_id: 1, state: "CHARGING", assigned_global_id: 999},
                {slot_id: 2, state: "FREE", assigned_global_id: null}
            ],
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
