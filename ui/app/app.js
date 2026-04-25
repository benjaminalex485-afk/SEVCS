import { startPolling } from './api.js';
import { startRenderer } from './renderer.js';
import { initSystemUI } from '../components/system_ui.js';
import { initGrids } from '../components/grids.js';
import { initSimulationUI } from '../components/simulation.js';
import { initUserUI } from '../components/user_ui.js';
import { events } from './events.js';
import { appState } from './state.js';

/**
 * SEVCS UI Bootstrap
 */
function bootstrap() {
    console.log('[SEVCS] Initializing Deterministic UI...');

    // 1. Initialize Components (Bind listeners)
    initSystemUI();
    initGrids();
    initSimulationUI();
    initUserUI();

    // 2. Emit initial state to populate placeholders
    events.emit('STATE_UPDATED', appState);

    // 3. Start Polling Loop
    startPolling();

    // 4. Start Render Tick
    startRenderer();

    // Global Error Handling for Debugging
    events.on('API_ERROR', (error) => {
        console.error(`[SEVCS UI ERROR] ${error.type}: ${error.message}`);
    });

    events.on('SNAPSHOT_GAP', (gap) => {
        console.warn(`[SEVCS UI WARNING] Sequence Gap detected: ${gap.from} -> ${gap.to}`);
    });
}

// Ensure DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
} else {
    bootstrap();
}
