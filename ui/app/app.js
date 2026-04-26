console.log('[VoltPark] APP.JS LOADED');
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
 * VoltPark UI Bootstrap
 */
function bootstrap() {
    console.log('[VoltPark] Initializing Deterministic UI...');

    // 0. Emergency Catch-All (dev-only)
    const params = new URLSearchParams(window.location.search);
    const isDevResetEnabled = params.has('devreset') || localStorage.getItem('voltpark_dev_reset') === '1';
    const resetBtn = document.getElementById('btn-hard-reset');
    if (resetBtn) {
        if (isDevResetEnabled) {
            console.log('[VoltPark] Emergency Reset Button Enabled (dev mode)');
            resetBtn.style.display = 'block';
            resetBtn.style.border = '2px solid white';
            resetBtn.addEventListener('click', () => {
                console.warn('[VoltPark] Emergency Reset Triggered');
                if (confirm('Clear all local session data and reload?')) {
                    localStorage.clear();
                    location.reload();
                }
            });
        } else {
            resetBtn.style.display = 'none';
        }
    }

    // 1. Initialize Components
    try {
        console.log('[VoltPark] Initializing System UI...');
        initSystemUI();
        console.log('[VoltPark] Initializing Grids...');
        initGrids();
        console.log('[VoltPark] Initializing Simulation UI...');
        initSimulationUI();
        console.log('[VoltPark] Initializing User UI...');
        initUserUI();
        console.log('[VoltPark] Initializing Auth UI...');
        renderAuthUI();
        console.log('[VoltPark] Component Initialization Complete.');
    } catch (e) {
        console.error('[VoltPark] Component Initialization Failed:', e);
    }

    // 2. Mode Toggling
    let lastDisplayState = 'INITIALIZING';
    events.on('STATE_UPDATED', (state) => {
        lastDisplayState = state.displayState;
        
        // Toggle logout button
        document.getElementById('btn-logout').style.display = 
            state.authStatus !== 'GUEST' ? 'block' : 'none';
        
        // Hide/Show main dashboard based on auth
        const isAuth = state.authStatus === 'AUTHENTICATED' || (state.authStatus === 'AUTHENTICATED_PENDING' && state.lastSequence > -1);
        // Keep dashboard visible for observability even before auth.
        document.getElementById('main-dashboard').style.visibility = 'visible';
        
        if (state.authStatus === 'GUEST') {
            document.getElementById('auth-container').style.display = 'flex';
        } else {
            document.getElementById('auth-container').style.display = 'none';
        }

        // Toggle Admin/User sections
        const isAdmin = state.uiMode === 'ADMIN';
        document.getElementById('admin-controls').classList.toggle('hidden', !isAdmin);
        document.getElementById('user-ui-container').classList.toggle('hidden', isAdmin);
    });

    document.getElementById('btn-mode-admin').onclick = () => {
        if (lastDisplayState !== 'SYNCHRONIZED') return;
        appState.uiMode = 'ADMIN';
        events.emit('STATE_UPDATED', appState);
    };

    document.getElementById('btn-mode-user').onclick = () => {
        if (lastDisplayState !== 'SYNCHRONIZED') return;
        appState.uiMode = 'USER';
        events.emit('STATE_UPDATED', appState);
    };
    
    document.getElementById('btn-logout').onclick = () => {
        performHardReset();
    };

    // 3. Auth Persistence Check
    const token = localStorage.getItem('sevcs_token');
    // Always poll status so startup state is visible to guests.
    startPolling();
    if (token) {
        appState.session.token = token;
        appState.authStatus = 'AUTHENTICATED_PENDING';
    }

    // 4. Start Render Tick
    startRenderer();

    events.on('API_ERROR', (error) => {
        console.error(`[VoltPark UI ERROR] ${error.code}: ${error.message}`);
    });
}

// Ensure DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
} else {
    bootstrap();
}
