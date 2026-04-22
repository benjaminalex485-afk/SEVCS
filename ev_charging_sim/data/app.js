const BASE_URL = window.location.origin;
let updateInterval = null;
let chartInstance = null;
let userRole = 'user';
let currentUser = '';
const chartData = { labels: [], datasets: [] };

document.addEventListener('DOMContentLoaded', () => {
    initChart();
});

function initChart() {
    const ctx = document.getElementById('powerChart');
    if (!ctx) return;
    
    chartData.datasets = [
        {
            label: 'Power (kW)',
            borderColor: '#bb86fc',
            backgroundColor: 'rgba(187, 134, 252, 0.2)',
            borderWidth: 2,
            pointRadius: 0,
            fill: true,
            data: []
        }
    ];

    if (window.Chart) {
        chartInstance = new Chart(ctx, {
            type: 'line',
            data: chartData,
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                scales: {
                    x: { display: true, title: { display: true, text: 'Time', color: '#a0a0a0' }, ticks: { color: '#a0a0a0' } },
                    y: { display: true, title: { display: true, text: 'kW', color: '#a0a0a0' }, ticks: { color: '#a0a0a0' }, min: 0 }
                },
                plugins: {
                    legend: { labels: { color: '#e0e0e0' } }
                }
            }
        });
    } else {
        console.warn("Chart.js not loaded.");
    }
}

function showPage(pageId) {
    document.querySelectorAll('.page').forEach(el => el.classList.add('hidden'));
    const targetPage = document.getElementById(`${pageId}-page`);
    if (targetPage) targetPage.classList.remove('hidden');
    
    if(pageId === 'logs') fetchLogs();
    if(pageId === 'settings') fetchUsers();
    if(pageId === 'booking') goToStep(1);
}

async function login() {
    const u = document.getElementById('username').value;
    const p = document.getElementById('password').value;
    const err = document.getElementById('login-error');
    err.textContent = "";

    try {
        const res = await fetch(`${BASE_URL}/api/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: u, password: p })
        });
        const data = await res.json();
        if (res.ok && data.status === 'success') {
            userRole = data.role;
            currentUser = u;
            document.getElementById('navbar').classList.remove('hidden');
            
            // Role-based visibility
            if(userRole === 'admin') {
                document.getElementById('nav-settings').classList.remove('hidden');
                document.getElementById('nav-ctrls').classList.remove('hidden');
                document.getElementById('nav-book').classList.add('hidden');
                document.getElementById('admin-summary').classList.remove('hidden');
                document.getElementById('admin-clear-logs').classList.remove('hidden');
            } else {
                document.getElementById('nav-settings').classList.add('hidden');
                document.getElementById('nav-ctrls').classList.add('hidden');
                document.getElementById('nav-book').classList.remove('hidden');
                document.getElementById('admin-summary').classList.add('hidden');
                document.getElementById('admin-clear-logs').classList.add('hidden');
            }
            
            showPage(userRole === 'admin' ? 'dashboard' : 'booking');
            startPolling();
        } else {
            err.textContent = data.message || "Login failed";
        }
    } catch (e) {
        err.textContent = "Network error connecting to backend.";
    }
}

async function logout() {
    stopPolling();
    document.getElementById('navbar').classList.add('hidden');
    showPage('login');
}

function startPolling() {
    if(!updateInterval) {
        pollStatus(); // immediate
        updateInterval = setInterval(pollStatus, 1000); // 1Hz for simulator sync
    }
}

function stopPolling() {
    if(updateInterval) {
        clearInterval(updateInterval);
        updateInterval = null;
    }
}

async function pollStatus() {
    try {
        const res = await fetch(`${BASE_URL}/api/status`);
        if(res.ok) {
            const data = await res.json();
            updateDashboard(data);
        }
    } catch(e) {
        console.error("Poll error", e);
    }
}

function updateDashboard(data) {
    // 1. Update System Summary
    const sys = data.sys;
    const slots = data.slots;
    
    const stEl = document.getElementById('sys-state');
    const cameraText = sys.camera_online ? "ONLINE" : "OFFLINE";
    document.getElementById('cam-online').textContent = cameraText;
    document.getElementById('cam-online').style.color = sys.camera_online ? "var(--success-color)" : "var(--error-color)";
    
    // Heartbeat check
    const now = Date.now() / 1000;
    const heartbeatDiff = now - sys.vision_heartbeat;
    const isVisionStale = heartbeatDiff > 2.0;
    document.getElementById('cam-state').textContent = isVisionStale ? "STALE" : "SYNCED";

    // 2. Find "My" Slot or Slot 1 for Admin
    let activeSlot = slots.find(s => s.booking && s.booking.user === currentUser);
    if(!activeSlot && userRole === 'admin') activeSlot = slots[0];
    
    if(activeSlot) {
        stEl.textContent = activeSlot.state;
        stEl.className = 'value state-' + activeSlot.state.toLowerCase();
        
        if(activeSlot.session) {
            document.getElementById('batt-val').textContent = activeSlot.session.battery_pct;
            document.getElementById('batt-bar').style.width = activeSlot.session.battery_pct + '%';
            document.getElementById('pwr-val').textContent = activeSlot.session.power.toFixed(2);
            document.getElementById('nrg-val').textContent = activeSlot.session.energy.toFixed(3);
            updateChart(activeSlot.session);
        } else {
            resetMetrics();
        }
    } else {
        stEl.textContent = "IDLE";
        stEl.className = "value state-idle";
        resetMetrics();
    }

    // 3. Update Admin Counters
    if(userRole === 'admin') {
        document.getElementById('sum-charging').textContent = sys.charging_count;
        document.getElementById('sum-reserved').textContent = slots.filter(s => s.state === 'RESERVED').length;
        document.getElementById('sum-queue').textContent = sys.queue_count;
    }
}

function resetMetrics() {
    document.getElementById('batt-val').textContent = "0";
    document.getElementById('batt-bar').style.width = "0%";
    document.getElementById('pwr-val').textContent = "0.00";
    document.getElementById('nrg-val').textContent = "0.000";
}

function updateChart(session) {
    if (!chartInstance) return;
    const now = new Date();
    const timeStr = `${now.getHours()}:${now.getMinutes().toString().padStart(2,'0')}:${now.getSeconds().toString().padStart(2,'0')}`;
    
    chartData.labels.push(timeStr);
    chartData.datasets[0].data.push(session.power);
    
    if(chartData.labels.length > 30) {
        chartData.labels.shift();
        chartData.datasets[0].data.shift();
    }
    chartInstance.update();
}

async function processPayment() {
    const btn = document.getElementById('pay-btn');
    btn.textContent = "Processing...";
    btn.disabled = true;

    setTimeout(async () => {
        const units = document.getElementById('book-units').value;
        const type = document.getElementById('book-type').value;

        try {
            const res = await fetch(`${BASE_URL}/api/book`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    username: currentUser,
                    kwh: parseFloat(units),
                    type: type
                })
            });
            const data = await res.json();
            if(data.status === 'success') {
                document.getElementById('assigned-slot-id').textContent = '#' + data.slot_id;
                document.getElementById('assigned-slot-time').textContent = "Awaiting Arrival";
                goToStep(3);
            } else {
                alert("Booking failed: " + (data.message || "Unknown error"));
                goToStep(1);
            }
        } catch(e) {
            alert("Connection error during booking.");
            goToStep(1);
        } finally {
            btn.textContent = "Confirm & Pay";
            btn.disabled = false;
        }
    }, 1000);
}

function finalizeBooking() {
    showPage('dashboard');
}

async function fetchLogs() {
    // Stage 1: Simplified logs or static mock for logs page
    const tbody = document.getElementById('logs-body');
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center">Live logs migrating in Stage 2</td></tr>';
}

async function fetchUsers() {
    // For admin settings (Stage 1 uses default users_db in main.py)
    const tbody = document.getElementById('users-body');
    tbody.innerHTML = '<tr><td>admin</td><td>admin</td><td>-</td></tr><tr><td>user</td><td>user</td><td>-</td></tr>';
}

function goToStep(step) {
    document.querySelectorAll('.booking-step').forEach(el => el.classList.add('hidden'));
    document.getElementById(`booking-step-${step}`).classList.remove('hidden');
}

async function sendAction(endpoint) {
    console.log("Action sent to:", endpoint);
    // Stage 1: Actions like Start/Stop are automatic via Vision alignment.
    // Manual overrides can be implemented here in future stages.
}
