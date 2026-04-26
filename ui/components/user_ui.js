import { executeAction, startPolling } from '../app/api_v3.js';
import { appState, resync } from '../app/state_v3.js';
import { events } from '../app/events.js';

export function initUserUI() {
    const container = document.getElementById('user-ui-container');
    let isLoading = false;

    // 1. Initial Static Render (Called once)
    function initialRender() {
        container.innerHTML = `
            <div class="user-dashboard-grid">
                <!-- Wallet Section -->
                <div class="card wallet-card glass" id="wallet-area">
                    <div class="spinner"></div>
                </div>

                <!-- Slot Overview -->
                <div class="card slots-card glass">
                    <div class="card-header">
                        <h3>Available Slots</h3>
                        <span class="count-badge" id="free-slots-count">0 Free</span>
                    </div>
                    <div class="slot-grid" id="slot-grid-area">
                        <!-- Dynamic Slots -->
                    </div>
                </div>

                <!-- Allocation Controls -->
                <div class="card actions-card glass">
                    <h3>Smart Allocation</h3>
                    <div class="form-group">
                        <label>Vehicle Type</label>
                        <select id="user-vehicle">
                            <option value="SUV">SUV</option>
                            <option value="Sedan">Sedan</option>
                            <option value="Truck">Truck</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Urgency</label>
                        <select id="user-urgency">
                            <option value="LOW">Low</option>
                            <option value="HIGH">High</option>
                        </select>
                    </div>
                    <button class="primary-btn" id="btn-find-slot">
                        Find Best Slot
                    </button>
                    
                    <div id="user-result-area"></div>
                </div>
            </div>
        `;

        // Event Delegation (Attach ONCE to the container)
        container.onclick = async (e) => {
            const btn = e.target.closest('.btn-charge, #btn-recharge, #btn-find-slot');
            if (!btn) return;
            
            // Allow actions if synchronized
            if (!appState.allowActions && !btn.id?.includes('recharge')) {
                console.warn('[USER UI] Action blocked: System not ready.');
                return;
            }

            const slotId = parseInt(btn.dataset.slotId);
            const action = btn.dataset.action;
            
            if (action === 'book') {
                isLoading = true;
                update(appState);
                try {
                    const res = await executeAction('book', { slot_id: slotId, username: appState.session.userId });
                    if (res.status === 'success') {
                        alert(`Slot ${slotId} Booked! Auth Code: ${res.auth_code}\nKeep this code to authorize your session.`);
                    } else {
                        alert(`Booking Failed: ${res.message}`);
                    }
                } catch (err) {
                    alert(`Network Error: ${err.message}`);
                }
                isLoading = false;
                update(appState);
            } else if (action === 'authorize') {
                const code = prompt(`Enter Authorization Code for Slot ${slotId}:`);
                if (code) {
                    isLoading = true;
                    update(appState);
                    try {
                        const res = await executeAction('authorize', { 
                            slot_id: slotId, 
                            code: code,
                            username: appState.session.userId 
                        });
                        if (res.status === 'success') {
                            alert(`Authorization Successful! Charging will begin.`);
                        } else {
                            alert(`Auth Failed: ${res.message}`);
                        }
                    } catch (err) {
                        alert(`Error: ${err.message}`);
                    }
                    isLoading = false;
                    update(appState);
                }
            } else if (btn.id === 'btn-find-slot') {
                isLoading = true;
                update(appState);
                try {
                    const payload = {
                        type: document.getElementById('user-vehicle').value,
                        urgency: document.getElementById('user-urgency').value
                    };
                    const res = await executeAction('find_slot', payload, 'find_slot');
                    // Find Slot result handling is via events
                } catch (err) {
                    alert(`Error: ${err.message}`);
                }
                isLoading = false;
                update(appState);
            }
        };
    }

    // 2. Dynamic Update (Called every poll)
    function update(state = {}) {
        const isUser = appState.uiMode === 'USER' && appState.authStatus !== 'GUEST';
        if (!isUser) {
            container.classList.add('hidden');
            return;
        }
        container.classList.remove('hidden');

        if (!appState.snapshot) return;
        const snapshot = appState.snapshot;
        const wallet = snapshot.user_wallet || { balance: 0, currency: 'USD' };

        // Update Wallet Area
        const walletArea = document.getElementById('wallet-area');
        if (walletArea) {
            walletArea.innerHTML = `
                <div class="wallet-header">
                    <h3>Your Wallet</h3>
                    <span class="wallet-id">ID: ${appState.session.userId}</span>
                </div>
                <div class="balance-area">
                    <span class="currency">$</span>
                    <span class="balance">${wallet.balance.toFixed(2)}</span>
                </div>
                <button class="primary-btn btn-small" id="btn-recharge">Quick Recharge $50</button>
            `;
        }

        // Update Slot Grid
        const slotGridArea = document.getElementById('slot-grid-area');
        if (slotGridArea) {
            const sortedSlots = [...snapshot.slots].sort((a, b) => a.slot_id - b.slot_id);
            slotGridArea.innerHTML = sortedSlots.map(slot => `
                <div class="slot-item ${slot.state.toLowerCase()}">
                    <div class="slot-info">
                        <span class="slot-label">Slot ${slot.slot_id}</span>
                        <span class="slot-status ${slot.state === 'AUTH_PENDING' ? 'status-pulse' : ''}">${slot.state}</span>
                    </div>
                    ${slot.state === 'FREE' ? `
                        <button class="btn-charge" data-action="book" data-slot-id="${slot.slot_id}"
                            ${!appState.allowActions ? 'disabled' : ''}>
                            Charge
                        </button>
                    ` : slot.state === 'AUTH_PENDING' ? `
                        <button class="btn-charge btn-auth" data-action="authorize" data-slot-id="${slot.slot_id}"
                            ${!appState.allowActions ? 'disabled' : ''}>
                            Authorize
                        </button>
                    ` : `
                        <div class="assigned-user">ID: ${slot.assigned_global_id || '---'}</div>
                    `}
                </div>
            `).join('');
        }

        const countBadge = document.getElementById('free-slots-count');
        if (countBadge) {
            countBadge.innerText = `${snapshot.slots.filter(s => s.state === 'FREE').length} Free`;
        }
        
        // Update Allocation Button State
        const findBtn = document.getElementById('btn-find-slot');
        if (findBtn) {
            findBtn.disabled = !appState.allowActions || isLoading;
            findBtn.innerText = isLoading ? 'PROCESSING...' : 'Find Best Slot';
        }
    }

    // Initialize
    initialRender();
    
    events.on('STATE_UPDATED', (state) => update(state));
    events.on('ACTIONS_CHANGED', () => update());
    events.on('API_ERROR', () => {
        isLoading = false;
        update();
    });
    
    // Initial call
    update();
}
