import { executeAction, startPolling } from './api_v3.js';
import { startRenderer } from './renderer.js';
import { initSystemUI } from '../components/system_ui.js';
import { initGrids } from '../components/grids.js';
import { initSimulationUI } from '../components/simulation.js';
import { initUserUI } from '../components/user_ui.js';
import { renderAuthUI } from '../components/auth_ui.js';
import { events } from './events.js';
import { appState, performHardReset } from './state_v3.js';

/**
 * SEVCS UI Bootstrap
 */
function bootstrap() {
    console.log('[SEVCS] Initializing Deterministic UI...');

    // 1. Initialize Components
    try {
        initSystemUI();
        initGrids();
        initSimulationUI();
        initUserUI();
        renderAuthUI();
    } catch (e) {
        console.error('[SEVCS] Component Initialization Failed:', e);
    }

    // 2. Mode Toggling
    document.getElementById('btn-mode-admin').onclick = () => {
        if (appState.uiState !== 'SYNCHRONIZED') return;
        appState.uiMode = 'ADMIN';
        events.emit('STATE_UPDATED', appState);
    };

    document.getElementById('btn-mode-user').onclick = () => {
        if (appState.uiState !== 'SYNCHRONIZED') return;
        appState.uiMode = 'USER';
        events.emit('STATE_UPDATED', appState);
    };

    document.getElementById('btn-logout').onclick = () => {
        performHardReset();
    };

    // 3. Auth Persistence Check
    const token = localStorage.getItem('sevcs_token');
    if (token) {
        // Simple recovery - in production this would verify the token
        appState.session.token = token;
        appState.authStatus = 'AUTHENTICATED_PENDING';
        startPolling();
    }

    // 4. Start Render Tick
    startRenderer();

    // UI Reactivity
    events.on('STATE_UPDATED', (state) => {
        // Toggle logout button
        document.getElementById('btn-logout').style.display = 
            state.authStatus !== 'GUEST' ? 'block' : 'none';
        
        // Hide/Show main dashboard based on auth
        document.getElementById('main-dashboard').style.visibility = 
            state.authStatus === 'AUTHENTICATED' ? 'visible' : 'hidden';

        // Toggle Admin/User sections
        const isAdmin = state.uiMode === 'ADMIN';
        document.getElementById('admin-controls').classList.toggle('hidden', !isAdmin);
        document.getElementById('user-ui-container').classList.toggle('hidden', isAdmin);
    });

    events.on('API_ERROR', (error) => {
        console.error(`[SEVCS UI ERROR] ${error.code}: ${error.message}`);
    });
}

// Ensure DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
} else {
    bootstrap();
}
