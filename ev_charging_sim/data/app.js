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
    try {
        await fetch(`${BASE_URL}/api/logout`, { method: 'POST' });
    } catch(e) {}
    stopPolling();
    document.getElementById('navbar').classList.add('hidden');
    showPage('login');
}

function startPolling() {
    if(!updateInterval) {
        pollStatus(); // immediate
        updateInterval = setInterval(pollStatus, 2000); // polling interval per specs
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
        
        // Poll camera separately
        const camRes = await fetch(`${BASE_URL}/api/camera/status`);
        if(camRes.ok) {
            const camData = await camRes.json();
            const cr = document.getElementById('cam-online');
            cr.textContent = camData.online ? "ONLINE" : "OFFLINE";
            cr.style.color = camData.online ? "var(--success-color)" : "var(--error-color)";
            document.getElementById('cam-state').textContent = camData.state;
        }
    } catch(e) {
        console.error("Poll error", e);
    }
}

function updateDashboard(data) {
    const stEl = document.getElementById('sys-state');
    stEl.textContent = data.state;
    stEl.className = 'value state-' + data.state.toLowerCase();

    document.getElementById('batt-val').textContent = data.battery_pct;
    document.getElementById('batt-bar').style.width = data.battery_pct + '%';
    
    document.getElementById('pwr-val').textContent = (data.power / 1000).toFixed(2);
    document.getElementById('nrg-val').textContent = data.energy.toFixed(3);
    
    document.getElementById('fault-display').textContent = data.fault_type;
    
    // Fetch Station Summary for Admin
    if(userRole === 'admin') updateAdminSummary();
    
    updateChart(data);
}

async function updateAdminSummary() {
    try {
        const res = await fetch(`${BASE_URL}/api/station/summary`);
        if(res.ok) {
            const sum = await res.json();
            document.getElementById('sum-charging').textContent = sum.charging || 0;
            document.getElementById('sum-reserved').textContent = sum.reserved || 0;
            document.getElementById('sum-queue').textContent = sum.queue || 0;
        }
    } catch(e) {}
}

function goToStep(step) {
    document.querySelectorAll('.booking-step').forEach(el => el.classList.add('hidden'));
    document.getElementById(`booking-step-${step}`).classList.remove('hidden');
}

async function processPayment() {
    const btn = document.getElementById('pay-btn');
    btn.textContent = "Processing...";
    btn.disabled = true;

    // Simulate network delay for payment
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
                const estDate = new Date(data.estimate * 1000);
                document.getElementById('assigned-slot-time').textContent = estDate.toLocaleTimeString();
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
    }, 1500);
}

function finalizeBooking() {
    showPage('dashboard');
}

async function clearLogs() {
    if(!confirm("Are you sure you want to clear all charging logs?")) return;
    try {
        const res = await fetch(`${BASE_URL}/api/logs/clear`, { method: 'POST' });
        if(res.ok) {
            alert("Logs cleared successfully.");
            fetchLogs();
        }
    } catch(e) {
        alert("Error clearing logs.");
    }
}

function updateChart(data) {
    if (!chartInstance) return;
    const now = new Date();
    const timeStr = `${now.getHours()}:${now.getMinutes().toString().padStart(2,'0')}:${now.getSeconds().toString().padStart(2,'0')}`;
    
    chartData.labels.push(timeStr);
    chartData.datasets[0].data.push(data.power / 1000); // kW
    
    // Window of 30 points
    if(chartData.labels.length > 30) {
        chartData.labels.shift();
        chartData.datasets[0].data.shift();
    }
    chartInstance.update();
}

async function sendAction(endpoint) {
    try {
        const res = await fetch(`${BASE_URL}${endpoint}`, { method: 'POST' });
        const data = await res.json();
        if(data.status !== 'success') alert(data.message || 'Action failed');
    } catch (e) {
        alert("Action failed to send.");
    }
}

async function setCurrentLimit() {
    const lim = document.getElementById('current-limit').value;
    try {
        const res = await fetch(`${BASE_URL}/api/set_current`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ limit: lim })
        });
        const data = await res.json();
        if(data.status === 'success') alert("Limit set to " + data.limit + "A");
    } catch(e) {
        alert("Failed to set limit.");
    }
}

async function fetchLogs() {
    try {
        const res = await fetch(`${BASE_URL}/api/logs`);
        const data = await res.json();
        const tbody = document.getElementById('logs-body');
        tbody.innerHTML = '';
        data.forEach(log => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${log.user || '-'}</td>
                <td>${log.start_time || log.timestamp || '-'}</td>
                <td>${log.end_time || '-'}</td>
                <td>${(log.energy_kwh !== undefined) ? log.energy_kwh.toFixed(3) : '-'}</td>
                <td>${log.fault || log.event || '-'}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch(e) {
        console.error("Failed to load logs");
    }
}

async function fetchUsers() {
    try {
        const res = await fetch(`${BASE_URL}/api/users`);
        const data = await res.json();
        const tbody = document.getElementById('users-body');
        tbody.innerHTML = '';
        data.users.forEach(u => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${u.username}</td>
                <td>${u.role}</td>
                <td>
                    ${u.username !== 'admin' ? `<button class="btn-danger" style="padding: 5px; width:auto;" onclick="deleteUser('${u.username}')">Del</button>` : '-'}
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch(e) { console.error("Err fetching users"); }
}

async function addUser() {
    const u = document.getElementById('new-user').value;
    const p = document.getElementById('new-pass').value;
    const r = document.getElementById('new-role').value;
    if(!u || !p) return alert("Missing fields");
    
    try {
        const res = await fetch(`${BASE_URL}/api/users`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username: u, password: p, role: r})
        });
        const data = await res.json();
        if(data.status === 'success') {
            document.getElementById('new-user').value = '';
            document.getElementById('new-pass').value = '';
            fetchUsers();
        } else {
            alert(data.message || "Failed to add user");
        }
    } catch(e) {}
}

async function deleteUser(username) {
    if(!confirm(`Delete ${username}?`)) return;
    try {
        const res = await fetch(`${BASE_URL}/api/users`, {
            method: 'DELETE',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username})
        });
        if(res.ok) fetchUsers();
    } catch(e) {}
}
