import { appState, resync } from '../app/state_v3.js';
import { login, signup, startPolling } from '../app/api_v3.js';
import { events } from '../app/events.js';

let lastStatus = null;
let lastSyncState = null;
let lastMode = null;

export function renderAuthUI() {
    const container = document.getElementById('auth-container');
    if (!container) return;

    const currentMode = container.dataset.mode || 'login';

    const isStatusSame = appState.authStatus === lastStatus;
    const isModeSame = currentMode === lastMode;
    const isSyncStateSame = appState.isDesync === lastSyncState;
    const isContainerEmpty = container.innerHTML === '';

    if (isStatusSame && isModeSame && !isContainerEmpty) {
        if (appState.authStatus !== 'AUTHENTICATED_PENDING' || isSyncStateSame) {
            return;
        }
    }

    lastStatus = appState.authStatus;
    lastSyncState = appState.isDesync;
    lastMode = currentMode;

    if (appState.authStatus === 'AUTHENTICATED') {
        container.innerHTML = '';
        container.style.display = 'none';
        return;
    }

    container.style.display = 'flex';

    if (appState.authStatus === 'AUTHENTICATED_PENDING') {
        if (appState.isResyncing) {
            container.innerHTML = `
                <div class="auth-card glass">
                    <div class="error-icon" style="font-size: 3rem; margin-bottom: 1rem;">⚠️</div>
                    <h2>Sync Interrupted</h2>
                    <p>A massive sequence gap was detected during initial synchronization. Manual resync required.</p>
                    <button class="primary-btn" id="resync-btn" style="margin-top: 1.5rem">Initialize Resync Pipeline</button>
                </div>
            `;
            document.getElementById('resync-btn').onclick = () => {
                resync();
                startPolling();
            };
            return;
        }

        container.innerHTML = `
            <div class="auth-card glass">
                <div class="spinner"></div>
                <h2>Syncing System State...</h2>
                <p>Establishing deterministic pipeline connection.</p>
                <div class="status-bar">
                    <div class="status-progress" style="width: 60%"></div>
                </div>
            </div>
        `;
        return;
    }

    if (appState.authStatus === 'AUTHENTICATING') {
        container.innerHTML = `
            <div class="auth-card glass">
                <div class="spinner"></div>
                <h2>Authenticating...</h2>
                <p>Verifying credentials with secure backend.</p>
            </div>
        `;
        return;
    }

    // GUEST state: Show Login/Signup Toggle
    const isSignup = container.dataset.mode === 'signup';
    
    container.innerHTML = `
        <div class="auth-card glass">
            <h1>SEVCS Smart Charging</h1>
            <p class="subtitle">Secure Deterministic Control Layer</p>
            
            <div class="auth-tabs">
                <button class="tab-btn ${!isSignup ? 'active' : ''}" id="login-tab">Login</button>
                <button class="tab-btn ${isSignup ? 'active' : ''}" id="signup-tab">Sign Up</button>
            </div>

            <form id="auth-form" class="auth-form">
                ${isSignup ? `
                    <div class="input-group">
                        <label>Full Name</label>
                        <input type="text" id="name" placeholder="John Doe" required>
                    </div>
                ` : ''}
                <div class="input-group">
                    <label>Email Address</label>
                    <input type="text" id="email" placeholder="user@example.com" required>
                </div>
                <div class="input-group">
                    <label>Password</label>
                    <input type="password" id="password" placeholder="••••••••" required>
                </div>
                ${isSignup ? `
                    <div class="input-group">
                        <label>Vehicle Type</label>
                        <select id="vehicleType">
                            <option value="FAST">Fast (DC)</option>
                            <option value="STANDARD">Standard (AC)</option>
                        </select>
                    </div>
                ` : ''}
                
                <button type="submit" class="primary-btn" id="submit-btn">
                    ${isSignup ? 'Create Account' : 'Sign In'}
                </button>
                
                <div id="auth-error" class="error-msg" style="display: none;"></div>
            </form>
        </div>
    `;

    // Event Listeners
    document.getElementById('login-tab').onclick = () => {
        container.dataset.mode = 'login';
        renderAuthUI();
    };
    document.getElementById('signup-tab').onclick = () => {
        container.dataset.mode = 'signup';
        renderAuthUI();
    };

    const form = document.getElementById('auth-form');
    if (!form) return;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        console.log('[SEVCS AUTH] Submit triggered. Current Status:', appState.authStatus);
        
        const emailEl = document.getElementById('email');
        const passwordEl = document.getElementById('password');
        
        if (!emailEl || !passwordEl) return;

        const email = emailEl.value;
        const password = passwordEl.value;
        
        let name = null;
        let vehicleType = null;
        if (isSignup) {
            const nameEl = document.getElementById('name');
            const vehicleEl = document.getElementById('vehicleType');
            if (nameEl) name = nameEl.value;
            if (vehicleEl) vehicleType = vehicleEl.value;
        }

        if (appState.authStatus === 'AUTHENTICATING') {
            console.warn('[SEVCS AUTH] Already authenticating. Ignoring.');
            return;
        }
        
        appState.authStatus = 'AUTHENTICATING';
        renderAuthUI();

        try {
            let result;
            if (isSignup) {
                console.log('[SEVCS AUTH] Attempting Signup:', email);
                result = await signup({ name, email, password, vehicleType });
                
                if (result.success) {
                    container.dataset.mode = 'login';
                    appState.authStatus = 'GUEST';
                    renderAuthUI();
                    const errorEl = document.getElementById('auth-error');
                    if (errorEl) {
                        errorEl.textContent = 'Account created. Please login.';
                        errorEl.style.display = 'block';
                        errorEl.style.color = '#4CAF50';
                    }
                    return;
                }
            } else {
                console.log('[SEVCS AUTH] Attempting Login:', email);
                result = await login({ email, password });
            }

            if (result && !result.success) {
                console.error('[SEVCS AUTH] Auth Failed:', result.message);
                appState.authStatus = 'GUEST';
                renderAuthUI();
                const errorEl = document.getElementById('auth-error');
                if (errorEl) {
                    errorEl.textContent = result.message;
                    errorEl.style.display = 'block';
                }
            }
        } catch (error) {
            console.error('[SEVCS AUTH] Critical Auth Error:', error);
            appState.authStatus = 'GUEST';
            renderAuthUI();
            const errorEl = document.getElementById('auth-error');
            if (errorEl) {
                errorEl.textContent = error.message || 'Connection failed. Ensure backend is running.';
                errorEl.style.display = 'block';
            }
        }
    });
}

// Global Event Listeners
events.on('HARD_RESET_COMPLETE', renderAuthUI);
events.on('FORCE_LOGOUT', renderAuthUI);
events.on('STATE_UPDATED', renderAuthUI);
