import { bookSlot, addSlot, removeSlot, updateSlotType } from '../app/api_v3.js';
import { events } from '../app/events.js';

export function initGrids() {
    const slotContainer = document.getElementById('slot-grid-container');
    const queueContainer = document.getElementById('queue-table-container');

    // Delegated click handler for slots
    slotContainer.addEventListener('click', async (e) => {
        // 1. Booking Action
        const slotCard = e.target.closest('.slot-card');
        if (slotCard && slotCard.classList.contains('interactive') && !e.target.closest('button')) {
            const slotId = slotCard.dataset.id;
            console.log(`[SEVCS] Requesting booking for slot ${slotId}`);
            await bookSlot(slotId);
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
                                     title="${slot.state === 'FREE' ? 'Click to book' : ''}">
                                    
                                    <div style="display: flex; justify-content: space-between; align-items: flex-start">
                                        <div style="font-size: 0.7rem; color: var(--text-secondary)">ID: ${slot.slot_id}</div>
                                        ${isAdmin ? `
                                            <button class="btn-remove-slot" data-id="${slot.slot_id}" title="Remove Slot">×</button>
                                        ` : ''}
                                    </div>

                                    <div style="font-weight: 700; margin: 4px 0">${slot.state}</div>
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
                    <div class="mono" style="color: var(--accent-orange); padding: 1rem; border: 1px dashed; text-align: center;">EMPTY SYSTEM STATE – NO ACTIVE TRACKS</div>
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
                                <td>${(v.confidence * 100).toFixed(1)}%</td>
                            </tr>
                        `).join('') : '<tr><td colspan="4" style="text-align: center; color: var(--text-secondary)">Waiting for queue synchronization...</td></tr>'}
                    </tbody>
                </table>
                `}
            </div>
        `;
    });
}
