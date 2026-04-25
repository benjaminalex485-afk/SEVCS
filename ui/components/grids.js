import { events } from '../app/events.js';

export function initGrids() {
    const slotContainer = document.getElementById('slot-grid-container');
    const queueContainer = document.getElementById('queue-table-container');

    events.on('STATE_UPDATED', (state) => {
        const snapshot = state.snapshot;
        const isAdmin = state.uiMode === 'ADMIN';

        // 1. Visibility Guard
        if (!isAdmin) {
            slotContainer.classList.add('hidden');
            queueContainer.classList.add('hidden');
            return;
        }

        slotContainer.classList.remove('hidden');
        queueContainer.classList.remove('hidden');

        // 2. Render Slot Grid
        slotContainer.innerHTML = `
            <div class="card">
                <h2>Charging Slots</h2>
                ${snapshot ? `
                    ${snapshot.slots.length > 0 ? `
                        <div class="grid-container">
                            ${snapshot.slots.map(slot => `
                                <div class="slot-card ${slot.state.toLowerCase()}">
                                    <div style="font-size: 0.7rem; color: var(--text-secondary)">ID: ${slot.slot_id}</div>
                                    <div style="font-weight: 700; margin: 4px 0">${slot.state}</div>
                                    <div class="mono" style="font-size: 0.75rem">${slot.assigned_global_id ? 'V-' + slot.assigned_global_id : '---'}</div>
                                </div>
                            `).join('')}
                        </div>
                    ` : '<div class="mono" style="color: var(--accent-red); padding: 1rem; border: 1px dashed; text-align: center;">EMPTY SYSTEM STATE – NO ACTIVE SLOTS</div>'}
                ` : '<p class="mono" style="color: var(--text-secondary)">Scanning for available slots...</p>'}
            </div>
        `;

        // 3. Render Queue Table
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
