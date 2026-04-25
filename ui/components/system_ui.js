import { events } from '../app/events.js';

export function initSystemUI() {
    const statusContainer = document.getElementById('live-status-container');
    const debugContainer = document.getElementById('debug-panel-container');
    const banner = document.getElementById('status-banner');
    const bannerMsg = document.getElementById('banner-message');
    const confidenceTag = document.getElementById('ui-confidence-tag');
    
    // Header elements
    const snapshotInfo = document.getElementById('snapshot-info');
    const healthIndicator = document.getElementById('ui-health-indicator');
    const btnAdmin = document.getElementById('btn-mode-admin');
    const btnUser = document.getElementById('btn-mode-user');

    // Mode Toggle Logic
    btnAdmin.addEventListener('click', () => {
        if (appState.uiState === 'SYNCHRONIZED') {
            appState.uiMode = 'ADMIN';
            updateModeUI();
        }
    });
    btnUser.addEventListener('click', () => {
        if (appState.uiState === 'SYNCHRONIZED') {
            appState.uiMode = 'USER';
            updateModeUI();
        }
    });

    function updateModeUI() {
        btnAdmin.className = `btn-toggle ${appState.uiMode === 'ADMIN' ? 'active' : ''}`;
        btnUser.className = `btn-toggle ${appState.uiMode === 'USER' ? 'active' : ''}`;
        // Force immediate render of other components
        events.emit('STATE_UPDATED', appState);
    }

    events.on('STATE_UPDATED', (state) => {
        const snapshot = state.snapshot;
        
        // 1. Header Global Info
        confidenceTag.className = `confidence-tag ${state.uiState.toLowerCase()}`;
        confidenceTag.innerText = state.uiState;

        if (snapshot) {
            snapshotInfo.innerText = `Snapshot: v${snapshot.snapshot_version} | Seq #${snapshot.snapshot_sequence}`;
        }

        healthIndicator.className = `health-indicator ${state.uiHealth.toLowerCase()}`;
        healthIndicator.innerText = `HEALTH: ${state.uiHealth}`;

        // 2. Mode Toggle Protection
        const canSwitch = state.uiState === 'SYNCHRONIZED';
        btnAdmin.disabled = !canSwitch;
        btnUser.disabled = !canSwitch;

        // 3. Priority Banner System: FROZEN > DESYNC > DEGRADED > DISCONNECTED
        let bannerActive = true;
        if (snapshot && snapshot.freeze_state) {
            banner.className = 'banner banner-frozen';
            bannerMsg.innerText = `SYSTEM FROZEN: ${snapshot.freeze_reason || 'EMERGENCY STOP'}`;
        } else if (state.uiState === 'DESYNCHRONIZED') {
            banner.className = 'banner banner-desync';
            bannerMsg.innerText = 'UI DESYNCHRONIZED - MONOTONICITY GAP DETECTED';
        } else if (state.systemState === 'INVALID') {
            banner.className = 'banner banner-invalid';
            bannerMsg.innerText = '🛑 CRITICAL: NO SLOTS OR QUEUE - SYSTEM INVALID';
        } else if (snapshot && snapshot.slots.length === 0) {
            banner.className = 'banner banner-frozen';
            bannerMsg.innerText = '🚨 NO AVAILABLE SLOTS - SYSTEM DEGRADED';
        } else if (snapshot && snapshot.queue.length === 0) {
            banner.className = 'banner banner-desync';
            bannerMsg.innerText = '⚠ EMPTY QUEUE - NO ACTIVE TRACKS';
        } else if (state.uiState === 'DEGRADED') {
            banner.className = 'banner banner-desync';
            bannerMsg.innerText = 'NETWORK DEGRADED - ADAPTIVE TIMEOUT ACTIVE';
        } else if (state.uiState === 'DISCONNECTED') {
            banner.className = 'banner banner-frozen';
            bannerMsg.innerText = 'BACKEND DISCONNECTED - SEARCHING FOR COORDINATOR';
        } else {
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
                            <p>Gap: ${state.hasGap ? 'YES' : 'NO'}</p>
                            <p>Recovery: ${state.recoveryCounter}/3</p>
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
                        <p>Latency: ${state.latency}ms</p>
                        <p>Stability: ${state.stabilityCounter} frames</p>
                        <p>Transition: ${state.transitionLog.length > 0 ? state.transitionLog[state.transitionLog.length-1].transition : '---'}</p>
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
                <h1 style="font-size: 3rem; margin-bottom: 1rem; color: #ff0000;">⚠️ SYSTEM DEGRADED</h1>
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
