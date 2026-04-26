import { events } from '../app/events.js';

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
        if (state.snapshot?.dev_mode) {
            healthIndicator.innerText = `[DEV] ${healthLabel}`;
            healthIndicator.style.border = '2px solid #ffaa00';
            healthIndicator.style.color = '#ffaa00';
        } else {
            healthIndicator.innerText = `${healthLabel} ${healthScore}%`;
            healthIndicator.style.border = '';
            healthIndicator.style.color = '';
        }

        // 3. Mode Toggle Protection & Styling
        const canSwitch = state.displayState === 'SYNCHRONIZED' || state.snapshot?.dev_mode === true;
        btnAdmin.disabled = !canSwitch;
        btnUser.disabled = !canSwitch;
        btnAdmin.className = `btn-toggle ${state.uiMode === 'ADMIN' ? 'active' : ''}`;
        btnUser.className = `btn-toggle ${state.uiMode === 'USER' ? 'active' : ''}`;

        // 3. Priority Banner System: FROZEN_UNKNOWN > FROZEN > DESYNC > DEGRADED > DISCONNECTED
        let bannerActive = true;
        const displayState = state.displayState;

        if (displayState === 'INITIALIZING') {
            banner.className = 'banner banner-desync';
            bannerMsg.innerText = 'Initializing...';
        } else if (displayState === 'WAITING_FOR_CAMERA') {
            banner.className = 'banner banner-frozen';
            bannerMsg.innerText = 'Waiting for camera...';
        } else if (displayState === 'DEGRADED_MODE') {
            banner.className = 'banner banner-frozen';
            bannerMsg.innerText = 'Degraded mode';
        } else if (displayState === 'SYNCHRONIZED') {
            bannerActive = false; // Camera status is shown inside the user dashboard card.
        } else if (displayState === 'FROZEN_UNKNOWN') {
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
            bannerMsg.innerText = '🛑 CRITICAL: NO SLOTS OR QUEUE - SYSTEM INVALID';
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
                            <p>Camera: ${snapshot.system_mode === 'WAITING_FOR_CAMERA' ? 'Waiting for camera...' : 'Camera Active'}</p>
                            ${snapshot.dev_mode ? '<p style="color: #ffaa00; font-weight: bold;">[DEV MODE ACTIVE]</p>' : ''}
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
