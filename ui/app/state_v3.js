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

    // ✅ BOOTSTRAP (Clean Boundary)
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

        // ✅ AUTO-PROMOTE AUTH (Hide Splash)
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

    // ❗ ATOMIC HARD DESYNC
    if (gap > (appState.MAX_SEQ_JUMP ?? 20)) {
        performHardSync({ reason: 'SEQUENCE_JUMP', gap });
        return;
    }

    // ✅ ATOMIC COMMIT
    const nowMono = performance.now();
    let delta = nowMono - appState.lastSnapshotMono;
    
    // ❗ Defensive Delta Clamping (Tab suspend guard)
    if (!Number.isFinite(delta) || delta < 0 || delta > 60000) delta = 0;

    appState.snapshot = deepFreeze(data); // Immutable commit
    appState.lastSequence = newSequence;
    appState.lastSnapshotTime = Date.now();
    appState.lastSnapshotMono = nowMono;
    
    console.log("[PIPELINE] ACCEPTING SNAPSHOT:", newSequence);
    
    // 🩺 Asymmetric Health Metric (Fast recovery, smoothed decay)
    const instantHealth = Math.max(0, 100 - Math.floor(delta / 50));
    if (instantHealth > (appState.healthScore || 0)) {
        appState.healthScore = Math.min(100, (appState.healthScore || 0) + 2);
    } else {
        appState.healthScore = Math.round(0.8 * (appState.healthScore || 100) + 0.2 * instantHealth);
    }

    // ✅ HYSTERESIS
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

    // 🔥 SET FLAG FIRST (Atomic Boundary)
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
