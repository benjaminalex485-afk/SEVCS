import { appState, takeLatestSnapshot, commitSnapshot, processSnapshot, checkPendingHardSync } from './state_v3.js';
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
                    console.error(`[VoltPark] SNAPSHOT PROCESS FAILED: Attempt ${retryCount}`, e);
                    
                    if (retryCount >= MAX_RETRY) {
                        console.error('[VoltPark] TOXIC SNAPSHOT IDENTIFIED: Blacklisting for 30s', identityKey);
                        toxicFrames.set(identityKey, Date.now());
                        if (toxicFrames.size > 100) toxicFrames.clear();
                        commitSnapshot();
                        retryCount = 0;
                    }
                }
            }
            
            // 3. RENDER
            safeDraw();
        } catch (e) {
            console.error('[VoltPark] RENDER TICK CRASH:', e);
        } finally {
            isRendering = false;
        }
    };

    // Main Loop
    setInterval(renderTick, 100);

    // Watchdog
    setInterval(() => {
        if (Date.now() - lastRenderTime > MAX_RENDER_DELAY) {
            console.warn('[VoltPark] RENDER STARVATION DETECTED: Forcing recovery frame');
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
    const mode = appState.snapshot?.system_mode || appState.snapshot?.mode || '';
    const displayState = (() => {
        if (appState.isResyncing) return 'RESYNC_REQUIRED';
        if (!appState.snapshot) {
            const sinceStart = performance.now() - appState.appStartMono;
            if (sinceStart > 5000) return 'DISCONNECTED';
            return 'INITIALIZING';
        }
        if (mode === 'WAITING_FOR_CAMERA') return 'WAITING_FOR_CAMERA';
        if (mode === 'DEGRADED') return 'DEGRADED_MODE';
        const delta = performance.now() - appState.lastSnapshotMono;
        if (appState.snapshot?.freeze_state) return 'FROZEN';
        if (delta > appState.SNAPSHOT_FRESHNESS_MS + 1000) return 'DISCONNECTED';
        if (appState.isDesync) return 'DESYNCHRONIZED';
        if (delta > appState.SNAPSHOT_FRESHNESS_MS) return 'DEGRADED';
        return 'SYNCHRONIZED';
    })();

    const isDevMode = appState.snapshot?.dev_mode === true;
    const allowActions = (
        displayState === 'SYNCHRONIZED' ||
        displayState === 'DEGRADED' ||
        displayState === 'DEGRADED_MODE' ||
        isDevMode
    );
    
    if (appState.allowActions !== allowActions) {
        console.log(`[RENDER] allowActions: ${appState.allowActions} -> ${allowActions} (State: ${displayState}, DevMode: ${isDevMode})`);
        appState.allowActions = allowActions;
    }
    return displayState;
}

/**
 * Fail-Safe Drawing
 */
function safeDraw() {
    try {
        const displayState = computeUIState();
        const slots = Array.isArray(appState.snapshot?.slots) ? appState.snapshot.slots : [];
        const queue = Array.isArray(appState.snapshot?.queue) ? appState.snapshot.queue : [];
        
        // Final Display Synthesis
        const stateToEmit = {
            ...appState,
            displayState,
            snapshot: appState.snapshot ? {
                ...appState.snapshot,
                slots: [...slots].sort((a, b) => (a.slot_id || 0) - (b.slot_id || 0)),
                queue: [...queue].sort((a, b) => (a.global_id || 0) - (b.global_id || 0))
            } : null
        };
        if (appState.lastRenderState !== displayState) {
            appState.lastRenderState = displayState;
            console.log('[VoltPark RENDER] state_source', {
                displayState,
                snapshotMode: stateToEmit.snapshot?.system_mode || 'NONE',
                seq: stateToEmit.snapshot?.snapshot_sequence ?? -1
            });
        }
        
        events.emit('STATE_UPDATED', stateToEmit);
    } catch (e) {
        console.error('[VoltPark] KERNEL CRASH', e);
        events.emit('RENDER_FALLBACK', e);
    }
}
