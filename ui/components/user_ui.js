import { executeAction, startPolling } from '../app/api_v3.js';
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
                    <div class="error-icon" style="font-size: 3rem; margin-bottom: 1rem;">⚠️</div>
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
                        <p class="status-msg warning">⚠️ Last recharge status unknown. Please verify state.</p>
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
