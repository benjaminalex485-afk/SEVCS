import { executeAction } from '../app/api.js';
import { appState } from '../app/state.js';
import { events } from '../app/events.js';

export function initUserUI() {
    const container = document.getElementById('user-ui-container');
    let isLoading = false;
    let lastResult = null;

    function render() {
        const isUser = appState.uiMode === 'USER';
        if (!isUser) {
            container.classList.add('hidden');
            return;
        }

        container.classList.remove('hidden');
        container.innerHTML = `
            <div class="card">
                <h2>User Interface</h2>
                <div class="form-group">
                    <label>Vehicle Type</label>
                    <select id="user-vehicle" ${isLoading ? 'disabled' : ''}>
                        <option value="SUV">SUV</option>
                        <option value="Sedan">Sedan</option>
                        <option value="Truck">Truck</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Charging Type</label>
                    <select id="user-charge" ${isLoading ? 'disabled' : ''}>
                        <option value="FAST">Fast Charge (DC)</option>
                        <option value="SLOW">Standard (AC)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Urgency</label>
                    <select id="user-urgency" ${isLoading ? 'disabled' : ''}>
                        <option value="LOW">Low</option>
                        <option value="HIGH">High</option>
                    </select>
                </div>
                <button class="btn btn-outline" style="width: 100%" id="btn-find-slot" 
                    ${!appState.allowActions || isLoading ? 'disabled' : ''}>
                    ${isLoading ? 'PROCESING...' : 'Find Best Slot'}
                </button>
                
                <div id="user-result-area" style="margin-top: 1.5rem">
                    ${lastResult ? `
                        <div class="card" style="background: rgba(0,0,0,0.2); border-color: ${lastResult.success ? 'var(--accent-green)' : 'var(--accent-red)'}">
                            <h3 style="font-size: 0.75rem; margin-bottom: 0.5rem">${lastResult.success ? 'ALLOCATION SUCCESS' : 'ALLOCATION FAILED'}</h3>
                            <div class="mono" style="font-size: 0.8rem">
                                ${lastResult.success ? `
                                    <p>Assigned Slot: <span style="color: var(--accent-green)">${lastResult.assigned_slot}</span></p>
                                    <p>Status: ${lastResult.status}</p>
                                    <p>Snapshot: v${lastResult.version}</p>
                                ` : `
                                    <p style="color: var(--accent-red)">Error: ${lastResult.error}</p>
                                `}
                            </div>
                        </div>
                    ` : ''}
                </div>
            </div>
        `;

        document.getElementById('btn-find-slot')?.addEventListener('click', () => {
            isLoading = true;
            render();
            const payload = {
                type: document.getElementById('user-vehicle').value,
                charge: document.getElementById('user-charge').value,
                urgency: document.getElementById('user-urgency').value
            };
            executeAction('find_slot', payload);
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

    events.on('STATE_UPDATED', render);
    render();
}
