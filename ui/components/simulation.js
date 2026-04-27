import { events } from '../app/events.js';
import { executeAction, getAdminPricingSettings, updateAdminPricingSettings } from '../app/api_v3.js';
import { appState } from '../app/state_v3.js';

export function initSimulationUI() {
    const container = document.getElementById('simulation-panel-container');
    const actionContainer = document.getElementById('action-panel-container');

    events.on('STATE_UPDATED', (state) => render(state));

    let lastRenderedHash = null;
    let pricingUi = { value: '1.25', loading: false, message: '', tone: 'ok' };

    function render(state = {}) {
        const isAdmin = appState.uiMode === 'ADMIN';

        const serverMultiplier = Number(state?.snapshot?.pricing_settings?.high_urgency_multiplier || 1.25);
        if (!pricingUi.loading && Number.isFinite(serverMultiplier)) {
            pricingUi.value = String(serverMultiplier);
        }
        const currentHash = `${isAdmin}_${appState.isSimulating}_${appState.requestHistory.length}_${serverMultiplier}_${pricingUi.loading}_${pricingUi.message}`;
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
                        ${state.displayState === 'FROZEN' ? '⚠️ SYSTEM HALTED' : 'EMERGENCY STOP'}
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
                                <td><span class="status-tag ${(entry.status || 'UNKNOWN').toLowerCase()}">${entry.status || 'UNKNOWN'}</span></td>
                                <td class="mono">v${entry.snapshot_version}</td>
                            </tr>
                        `).join('') : '<tr><td colspan="4" style="text-align: center; color: var(--text-secondary)">No requests logged.</td></tr>'}
                    </tbody>
                </table>
                <div class="card" style="margin-top: 1rem; background: rgba(255,255,255,0.02)">
                    <h3 style="margin-bottom: 0.5rem;">Premium Payout</h3>
                    <div class="mono" style="font-size: 0.72rem; color: var(--text-secondary); margin-bottom: 0.4rem;">
                        High urgency quote multiplier
                    </div>
                    <div style="display:flex; gap:0.5rem; align-items:center;">
                        <input type="number" id="high-urgency-multiplier" min="1" max="5" step="0.05"
                            value="${Number.isFinite(Number(pricingUi.value)) ? Number(pricingUi.value).toFixed(2) : (Number.isFinite(serverMultiplier) ? serverMultiplier.toFixed(2) : '1.25')}"
                            ${pricingUi.loading ? 'disabled' : ''} style="max-width:120px;" />
                        <button class="btn btn-outline" id="btn-save-urgency-multiplier" ${pricingUi.loading ? 'disabled' : ''}>
                            ${pricingUi.loading ? 'Saving...' : 'Save'}
                        </button>
                    </div>
                    <div class="mono ${pricingUi.tone === 'error' ? 'slot-cap-feedback--err' : 'slot-cap-feedback--ok'}" style="margin-top:0.4rem; min-height:1rem;">
                        ${pricingUi.message || ''}
                    </div>
                </div>
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
        document.getElementById('btn-save-urgency-multiplier')?.addEventListener('click', async () => {
            const input = document.getElementById('high-urgency-multiplier');
            const nextValue = Number(input?.value || 1.25);
            pricingUi.loading = true;
            pricingUi.message = '';
            pricingUi.tone = 'ok';
            lastRenderedHash = null;
            render(state);
            try {
                const res = await updateAdminPricingSettings(nextValue);
                pricingUi.value = String(res?.high_urgency_multiplier ?? nextValue);
                pricingUi.message = `Saved (${Number(pricingUi.value).toFixed(2)}x)`;
                pricingUi.tone = 'ok';
            } catch (err) {
                pricingUi.message = err?.message || 'Save failed';
                pricingUi.tone = 'error';
            } finally {
                pricingUi.loading = false;
                lastRenderedHash = null;
                render(appState);
            }
        });

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

    (async () => {
        try {
            const settings = await getAdminPricingSettings();
            const v = Number(settings?.high_urgency_multiplier);
            if (Number.isFinite(v)) pricingUi.value = String(v);
        } catch (_) {
            // Non-fatal: use server snapshot/default value.
        } finally {
            lastRenderedHash = null;
            render(appState);
        }
    })();

    render();
}
