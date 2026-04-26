import { addSlot, removeSlot, updateSlotType } from '../app/api_v3.js';
import { events } from '../app/events.js';

export function initGrids() {
    const slotContainer = document.getElementById('slot-grid-container');
    const queueContainer = document.getElementById('queue-table-container');
    const kpiContainer = document.getElementById('admin-kpi-container');

    function formatCurrency(value) {
        return `$${Number(value || 0).toFixed(2)}`;
    }

    function formatKwh(value) {
        return `${Number(value || 0).toFixed(2)} kWh`;
    }

    function renderKpiCards(snapshot) {
        if (!kpiContainer) return;
        if (!snapshot) {
            kpiContainer.innerHTML = `
                <div class="card">
                    <h2>Station KPI (Last 24h)</h2>
                    <p class="mono" style="color: var(--text-secondary)">Waiting for metrics...</p>
                </div>
                <div class="card">
                    <h2>Station KPI (Lifetime)</h2>
                    <p class="mono" style="color: var(--text-secondary)">Waiting for metrics...</p>
                </div>
            `;
            return;
        }

        const slots = Array.isArray(snapshot.slots) ? snapshot.slots : [];
        const queueLen = Array.isArray(snapshot.queue) ? snapshot.queue.length : 0;
        const occupied = slots.filter((s) => String(s.state || '').toUpperCase() !== 'FREE').length;
        const occupancyPct = slots.length > 0 ? (occupied / slots.length) * 100 : 0;
        const activeSessionsNow = slots.filter((s) => String(s.state || '').toUpperCase() === 'CHARGING').length;

        const zeroMetric = {
            total_revenue: 0,
            total_energy_kwh: 0,
            session_count: 0,
            avg_session_value: 0,
            avg_kwh_per_session: 0
        };
        const adminKpis = snapshot.admin_kpis || {};
        const last24h = { ...zeroMetric, ...(adminKpis.last24h || {}) };
        const lifetime = { ...zeroMetric, ...(adminKpis.lifetime || {}) };

        const cardHtml = (title, metric) => `
            <div class="card">
                <h2>${title}</h2>
                <div class="admin-kpi-grid">
                    <div class="admin-kpi-item">
                        <div class="admin-kpi-label">Total Revenue</div>
                        <div class="admin-kpi-value">${formatCurrency(metric.total_revenue)}</div>
                    </div>
                    <div class="admin-kpi-item">
                        <div class="admin-kpi-label">Total Energy</div>
                        <div class="admin-kpi-value">${formatKwh(metric.total_energy_kwh)}</div>
                    </div>
                    <div class="admin-kpi-item">
                        <div class="admin-kpi-label">Active Sessions</div>
                        <div class="admin-kpi-value">${activeSessionsNow}</div>
                    </div>
                    <div class="admin-kpi-item">
                        <div class="admin-kpi-label">Avg Session Value</div>
                        <div class="admin-kpi-value">${formatCurrency(metric.avg_session_value)}</div>
                    </div>
                    <div class="admin-kpi-item">
                        <div class="admin-kpi-label">Avg kWh / Session</div>
                        <div class="admin-kpi-value">${Number(metric.avg_kwh_per_session || 0).toFixed(2)}</div>
                    </div>
                    <div class="admin-kpi-item">
                        <div class="admin-kpi-label">Occupancy / Queue</div>
                        <div class="admin-kpi-value">${occupancyPct.toFixed(1)}% / ${queueLen}</div>
                    </div>
                </div>
            </div>
        `;

        kpiContainer.innerHTML = `
            ${cardHtml('Station KPI (Last 24h)', last24h)}
            ${cardHtml('Station KPI (Lifetime)', lifetime)}
        `;
    }

    // Delegated click handler for slots
    slotContainer.addEventListener('click', async (e) => {
        // 1. Booking Action
        const slotCard = e.target.closest('.slot-card');
        if (slotCard && slotCard.classList.contains('interactive') && !e.target.closest('button')) {
            const slotId = slotCard.dataset.id;
            const chargerType = slotCard.dataset.chargerType || 'STANDARD';
            console.log(`[ChargeFlow] grid entry for slot ${slotId}`);
            events.emit('CHARGE_FLOW_START', { slot_id: Number(slotId), charger_type: chargerType });
            return;
        }

        // 2. Remove Slot (Admin)
        const btnRemove = e.target.closest('.btn-remove-slot');
        if (btnRemove) {
            const slotId = btnRemove.dataset.id;
            if (confirm(`Remove Slot ${slotId}?`)) {
                await removeSlot(slotId);
            }
            return;
        }

        // 3. Update Type (Admin)
        const btnType = e.target.closest('.btn-toggle-type');
        if (btnType) {
            const slotId = btnType.dataset.id;
            const currentType = btnType.dataset.type;
            const nextType = currentType === 'FAST' ? 'STANDARD' : 'FAST';
            await updateSlotType(slotId, nextType);
            return;
        }

        // 4. Global Admin Actions
        if (e.target.id === 'btn-add-slot') {
            await addSlot('STANDARD');
        }
    });

    let lastRenderedHash = null;

    events.on('STATE_UPDATED', (state) => {
        const snapshot = state.snapshot;
        const isAdmin = state.uiMode === 'ADMIN';
        const mainDashboard = document.getElementById('main-dashboard');
        const dataPanel = document.querySelector('.data-panel');
        if (mainDashboard) mainDashboard.classList.toggle('admin-layout', isAdmin);
        if (dataPanel) dataPanel.classList.toggle('admin-layout', isAdmin);

        // Performance Guard: Only re-render if data has actually changed
        const currentHash = snapshot ? `${snapshot.state_hash}_${snapshot.snapshot_sequence}_${isAdmin}` : 'empty';
        if (currentHash === lastRenderedHash) return;
        lastRenderedHash = currentHash;

        // 1. Render Slot Grid
        slotContainer.innerHTML = `
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem">
                    <h2 style="margin: 0">Charging Slots</h2>
                    ${isAdmin ? `
                        <button class="btn btn-outline btn-small" id="btn-add-slot">+ Add Slot</button>
                    ` : ''}
                </div>
                ${snapshot ? `
                    ${snapshot.slots.length > 0 ? `
                        <div class="grid-container">
                            ${snapshot.slots.map(slot => `
                                <div class="slot-card ${slot.state.toLowerCase()} ${state.allowActions && slot.state === 'FREE' ? 'interactive' : ''}" 
                                     data-id="${slot.slot_id}"
                                     data-charger-type="${slot.charger_type || 'STANDARD'}"
                                     title="${slot.state === 'FREE' ? 'Click to book' : ''}">
                                    
                                    <div style="display: flex; justify-content: space-between; align-items: flex-start">
                                        <div style="font-size: 0.7rem; color: var(--text-secondary)">Slot ${Number(slot.slot_id) + 1}</div>
                                        ${isAdmin ? `
                                            <button class="btn-remove-slot" data-id="${slot.slot_id}" title="Remove Slot">×</button>
                                        ` : ''}
                                    </div>

                                    <div class="slot-state-label">${slot.state}</div>
                                    <div class="mono" style="font-size: 0.75rem">${slot.assigned_global_id ? 'V-' + slot.assigned_global_id : '---'}</div>
                                    
                                    <div class="slot-type-badge ${slot.charger_type?.toLowerCase() || 'standard'}">
                                        ${slot.charger_type || 'STANDARD'}
                                    </div>

                                    ${isAdmin ? `
                                        <button class="btn-toggle-type" data-id="${slot.slot_id}" data-type="${slot.charger_type || 'STANDARD'}">
                                            ⚙️ Change Type
                                        </button>
                                    ` : ''}
                                </div>
                            `).join('')}
                        </div>
                    ` : '<div class="mono" style="color: var(--accent-red); padding: 1rem; border: 1px dashed; text-align: center;">EMPTY SYSTEM STATE – NO ACTIVE SLOTS</div>'}
                ` : '<p class="mono" style="color: var(--text-secondary)">Scanning for available slots...</p>'}
            </div>
        `;

        // 2. Render Queue Table
        queueContainer.innerHTML = `
            <div class="card">
                <h2>Vehicle Queue</h2>
                ${snapshot && snapshot.queue.length === 0 ? `
                    <div class="mono" style="color: var(--accent-orange); padding: 1rem; border: 1px dashed; text-align: center;">No objects detected</div>
                ` : `
                <table class="status-table">
                    <thead>
                        <tr>
                            <th>Global ID</th>
                            <th>Track ID</th>
                            <th>State</th>
                            <th>Confidence</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${snapshot ? snapshot.queue.map(v => `
                            <tr>
                                <td class="mono">V-${v.global_id}</td>
                                <td class="mono">T-${v.track_id}</td>
                                <td>${v.state}</td>
                                <td>${(((v.signal_confidence ?? v.confidence ?? 0) * 100)).toFixed(1)}%</td>
                            </tr>
                        `).join('') : '<tr><td colspan="4" style="text-align: center; color: var(--text-secondary)">Waiting for queue synchronization...</td></tr>'}
                    </tbody>
                </table>
                `}
            </div>
        `;

        // 3. Render KPI Cards (ADMIN ONLY)
        if (isAdmin) {
            renderKpiCards(snapshot);
        } else if (kpiContainer) {
            kpiContainer.innerHTML = '';
        }
    });
}
