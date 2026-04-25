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
    const flags = {
        isFrozen: appState.snapshot?.freeze_state,
        isDesync: appState.uiState === 'DESYNCHRONIZED' || appState.uiState === 'RESYNC_REQUIRED',
        isDisconnected: appState.uiState === 'DISCONNECTED',
        isDegraded: appState.uiHealth === 'CRITICAL' || appState.uiHealth === 'DEGRADED',
        isUnknown: Array.from(appState.pendingIntents.values()).some(i => i.status === 'UNKNOWN')
    };

    if (flags.isFrozen) {
        if (flags.isUnknown) return 'FROZEN_UNKNOWN';
        return 'FROZEN';
    }
    if (appState.uiState === 'RESYNC_REQUIRED') return 'RESYNC_REQUIRED';
    if (flags.isDesync) return 'DESYNCHRONIZED';
    if (flags.isDisconnected) return 'DISCONNECTED';
    if (flags.isDegraded) return 'DEGRADED';
    
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
        console.error('[SEVCS] RENDER FAILURE', e);
    }
}
