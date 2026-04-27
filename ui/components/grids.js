import { addSlot, removeSlot, updateSlotType } from '../app/api_v3.js';
import { events } from '../app/events.js';

const CHARGER_TYPE_OPTIONS = [
    { value: 'AC_WIRED', label: 'AC (Wired)' },
    { value: 'DC_WIRED', label: 'DC (Wired)' },
    { value: 'WIRELESS', label: 'Wireless' }
];
const CHARGING_LEVEL_OPTIONS = [
    { value: 'LEVEL_1', label: 'Level 1 (120V)' },
    { value: 'LEVEL_2', label: 'Level 2 (240V)' },
    { value: 'LEVEL_3', label: 'Level 3 (DC Fast)' }
];

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

    function normalizeSlotCapabilities(slot) {
        const chargerTypes = Array.isArray(slot?.charger_types) && slot.charger_types.length > 0
            ? slot.charger_types
            : [String(slot?.charger_type || '').toUpperCase() === 'FAST' ? 'DC_WIRED' : 'AC_WIRED'];
        const chargingLevels = Array.isArray(slot?.charging_levels) && slot.charging_levels.length > 0
            ? slot.charging_levels
            : [String(slot?.charger_type || '').toUpperCase() === 'FAST' ? 'LEVEL_3' : 'LEVEL_2'];
        return { chargerTypes, chargingLevels };
    }

    function badgeList(values, options) {
        const labels = values.map((value) => options.find((o) => o.value === value)?.label || value);
        return labels.join(', ');
    }

    const slotCapFeedbackTimers = new Map();

    function showSlotCapabilityFeedback(slotId, ok, message) {
        const el = document.getElementById(`slot-cap-feedback-${slotId}`);
        if (!el) return;
        const key = String(slotId);
        const prev = slotCapFeedbackTimers.get(key);
        if (prev) clearTimeout(prev);
        el.textContent = message;
        el.hidden = false;
        el.classList.remove('slot-cap-feedback--ok', 'slot-cap-feedback--err');
        el.classList.add(ok ? 'slot-cap-feedback--ok' : 'slot-cap-feedback--err');
        const t = setTimeout(() => {
            el.hidden = true;
            el.textContent = '';
            el.classList.remove('slot-cap-feedback--ok', 'slot-cap-feedback--err');
            slotCapFeedbackTimers.delete(key);
        }, 2800);
        slotCapFeedbackTimers.set(key, t);
    }

    function renderSlotCard(slot, isAdmin, allowActions) {
        const normalized = normalizeSlotCapabilities(slot);
        const chargerSummary = badgeList(normalized.chargerTypes, CHARGER_TYPE_OPTIONS);
        const levelSummary = badgeList(normalized.chargingLevels, CHARGING_LEVEL_OPTIONS);
        const userBookable = !isAdmin && allowActions && slot.state === 'FREE';
        return `
            <div class="slot-card ${slot.state.toLowerCase()} ${userBookable ? 'interactive' : ''}" 
                 data-id="${slot.slot_id}"
                 data-charger-type="${slot.charger_type || 'STANDARD'}"
                 title="${userBookable ? 'Click to book' : ''}">
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
                <div class="slot-capabilities">
                    <div class="slot-capability-item"><span class="slot-cap-label">Type:</span> ${chargerSummary}</div>
                    <div class="slot-capability-item"><span class="slot-cap-label">Level:</span> ${levelSummary}</div>
                </div>

                ${isAdmin ? `
                    <div class="slot-cap-editor">
                        <div class="slot-cap-editor-group">
                            <div class="slot-cap-title">Charger Type</div>
                            ${CHARGER_TYPE_OPTIONS.map((opt) => `
                                <label class="slot-cap-option">
                                    <input type="checkbox" id="slot-${slot.slot_id}-ct-${opt.value}" ${normalized.chargerTypes.includes(opt.value) ? 'checked' : ''} />
                                    <span>${opt.label}</span>
                                </label>
                            `).join('')}
                        </div>
                        <div class="slot-cap-editor-group">
                            <div class="slot-cap-title">Charging Level</div>
                            ${CHARGING_LEVEL_OPTIONS.map((opt) => `
                                <label class="slot-cap-option">
                                    <input type="checkbox" id="slot-${slot.slot_id}-cl-${opt.value}" ${normalized.chargingLevels.includes(opt.value) ? 'checked' : ''} />
                                    <span>${opt.label}</span>
                                </label>
                            `).join('')}
                        </div>
                    </div>
                    <button class="btn-edit-capabilities" data-id="${slot.slot_id}">
                        Save Capabilities
                    </button>
                    <div id="slot-cap-feedback-${slot.slot_id}" class="slot-cap-feedback mono" hidden aria-live="polite"></div>
                ` : ''}
            </div>
        `;
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
        // 1. Booking Action (user mode only: admin cards are never .interactive; never treat form controls as booking)
        const slotCard = e.target.closest('.slot-card');
        const bookingChrome = e.target.closest('button, input, label, select, textarea, .slot-cap-editor, .btn-edit-capabilities');
        if (slotCard && slotCard.classList.contains('interactive') && !bookingChrome) {
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
        const btnType = e.target.closest('.btn-edit-capabilities');
        if (btnType) {
            const slotId = btnType.dataset.id;
            const slot = (stateSnapshotForActions?.slots || []).find((s) => Number(s.slot_id) === Number(slotId));
            if (!slot) return;
            const { chargerTypes, chargingLevels } = normalizeSlotCapabilities(slot);
            const nextTypes = CHARGER_TYPE_OPTIONS
                .filter((o) => {
                    const el = document.getElementById(`slot-${slotId}-ct-${o.value}`);
                    return !!el?.checked;
                })
                .map((o) => o.value);
            const nextLevels = CHARGING_LEVEL_OPTIONS
                .filter((o) => {
                    const el = document.getElementById(`slot-${slotId}-cl-${o.value}`);
                    return !!el?.checked;
                })
                .map((o) => o.value);
            const finalTypes = nextTypes.length > 0 ? nextTypes : chargerTypes;
            const finalLevels = nextLevels.length > 0 ? nextLevels : chargingLevels;
            btnType.disabled = true;
            try {
                const res = await updateSlotType(slotId, finalTypes, finalLevels);
                const ok = res && (res.status === 'success' || res.status === 'OK');
                if (ok) {
                    showSlotCapabilityFeedback(slotId, true, 'Capabilities saved');
                } else {
                    showSlotCapabilityFeedback(slotId, false, res?.message || res?.error || 'Save failed');
                }
            } catch (err) {
                showSlotCapabilityFeedback(slotId, false, err?.message || 'Save failed');
            } finally {
                btnType.disabled = false;
            }
            return;
        }

        // 4. Global Admin Actions
        if (e.target.id === 'btn-add-slot') {
            await addSlot(['AC_WIRED'], ['LEVEL_2']);
        }
    });

    let lastSlotGridKey = null;
    let lastQueueKey = null;
    let lastKpiKey = null;
    let stateSnapshotForActions = null;

    /** Admin slot grid: ignore snapshot state_hash churn so checkbox edits are not wiped every frame. */
    function slotGridRenderKey(snapshot, isAdmin, allowActions) {
        if (!snapshot) return 'empty';
        if (isAdmin) {
            const body = (snapshot.slots || []).map((s) => {
                const { chargerTypes, chargingLevels } = normalizeSlotCapabilities(s);
                return {
                    id: s.slot_id,
                    state: String(s.state || ''),
                    ct: [...chargerTypes].sort().join(','),
                    cl: [...chargingLevels].sort().join(',')
                };
            });
            return `admin_slots:${JSON.stringify(body)}`;
        }
        return `user_slots:${allowActions ? 1 : 0}|${snapshot.state_hash}|${snapshot.snapshot_sequence}|${(snapshot.slots || []).length}`;
    }

    function queueRenderKey(snapshot) {
        if (!snapshot) return 'empty';
        const q = snapshot.queue || [];
        return JSON.stringify(q.map((e) => ({
            gid: e.global_id,
            tid: e.track_id,
            st: e.state,
            c: Math.round(((e.signal_confidence ?? e.confidence ?? 0) * 1000)) / 1000
        })));
    }

    function kpiRenderKey(snapshot) {
        if (!snapshot) return 'empty';
        const slots = snapshot.slots || [];
        const occupied = slots.filter((s) => String(s.state || '').toUpperCase() !== 'FREE').length;
        return JSON.stringify({
            kpis: snapshot.admin_kpis,
            n: slots.length,
            occ: occupied,
            charging: slots.filter((s) => String(s.state || '').toUpperCase() === 'CHARGING').length
        });
    }

    events.on('STATE_UPDATED', (state) => {
        const snapshot = state.snapshot;
        stateSnapshotForActions = snapshot;
        const isAdmin = state.uiMode === 'ADMIN';
        const mainDashboard = document.getElementById('main-dashboard');
        const dataPanel = document.querySelector('.data-panel');
        const col2Stack = document.querySelector('.user-col2-stack');
        const adminCol2Stack = document.getElementById('admin-col2-stack');
        if (mainDashboard) mainDashboard.classList.toggle('admin-layout', isAdmin);
        if (dataPanel) dataPanel.classList.toggle('admin-layout', isAdmin);

        // Keep Vehicle Queue in admin layout root for admin mode,
        // but attach it under user column-2 stack in user mode
        // so wallet expansion in column-1 only pushes camera card.
        if (adminCol2Stack && dataPanel && isAdmin && adminCol2Stack.parentElement !== dataPanel) {
            dataPanel.appendChild(adminCol2Stack);
        }
        if (adminCol2Stack && col2Stack && !isAdmin && adminCol2Stack.parentElement !== col2Stack) {
            col2Stack.appendChild(adminCol2Stack);
        }

        const slotKey = slotGridRenderKey(snapshot, isAdmin, state.allowActions);
        if (slotKey !== lastSlotGridKey) {
            lastSlotGridKey = slotKey;
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
                            ${snapshot.slots.map((slot) => renderSlotCard(slot, isAdmin, state.allowActions)).join('')}
                        </div>
                    ` : '<div class="mono" style="color: var(--accent-red); padding: 1rem; border: 1px dashed; text-align: center;">EMPTY SYSTEM STATE – NO ACTIVE SLOTS</div>'}
                ` : '<p class="mono" style="color: var(--text-secondary)">Scanning for available slots...</p>'}
            </div>
        `;
        }

        const qKey = queueRenderKey(snapshot);
        if (qKey !== lastQueueKey) {
            lastQueueKey = qKey;
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
        }

        if (isAdmin) {
            const kKey = kpiRenderKey(snapshot);
            if (kKey !== lastKpiKey) {
                lastKpiKey = kKey;
                renderKpiCards(snapshot);
            }
        } else if (kpiContainer) {
            lastKpiKey = null;
            kpiContainer.innerHTML = '';
        }
    });
}
