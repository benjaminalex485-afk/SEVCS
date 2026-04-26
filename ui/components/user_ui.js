import { executeAction, getAvailability, getPricingQuote, processMockPayment } from '../app/api_v3.js';
import { appState } from '../app/state_v3.js';
import { events } from '../app/events.js';

export function initUserUI() {
    const container = document.getElementById('user-ui-container');
    let isLoading = false;
    let lastSlotHash = "";
    const rechargeFlowState = {
        open: false,
        loading: false,
        error: null,
        success: null,
        form: {
            amount: '50',
            cardNumber: '',
            cardHolder: '',
            cardExpiry: '',
            cardCvv: ''
        }
    };

    const initialFlowData = () => ({
        slot_id: null,
        charger_type: null,
        date: null,
        time_window: null,
        requested_kwh: 20,
        charge_rate_kw: 7,
        allow_waitlist: false,
        quote: null,
        payment: null,
        auth_code: null,
        available_slots: [],
        wait_eta_minutes: null,
        wait_message: null
    });

    const chargeFlowState = {
        step: "IDLE",
        previousStep: "IDLE",
        data: initialFlowData(),
        error: null
    };

    function displaySlot(slotId) {
        const n = Number(slotId);
        if (Number.isNaN(n)) return String(slotId ?? '-');
        return String(n + 1);
    }

    function resetFlow() {
        chargeFlowState.step = "IDLE";
        chargeFlowState.previousStep = "IDLE";
        chargeFlowState.data = initialFlowData();
        chargeFlowState.error = null;
    }

    function setFlowError(message) {
        const sourceStep = chargeFlowState.step;
        chargeFlowState.error = message;
        chargeFlowState.step = "ERROR";
        console.error(`[ChargeFlow] ERROR: ${message}`);
        console.log(`[ChargeFlow] STEP_CHANGE: ${sourceStep} -> ERROR`);
        renderChargeFlow();
    }

    function validateCardInputs(cardNumber, cardHolder, cardExpiry, cardCvv) {
        const digitsOnlyCard = (cardNumber || '').replace(/\D/g, '');
        const expiryOk = /^(0[1-9]|1[0-2])\/\d{2}$/.test(cardExpiry || '');
        const cvvOk = /^\d{3,4}$/.test(cardCvv || '');
        if (!cardHolder || digitsOnlyCard.length < 12 || digitsOnlyCard.length > 19 || !expiryOk || !cvvOk) {
            return 'Please fill valid card details';
        }
        return null;
    }

    function renderWalletRechargePanel() {
        const panel = document.getElementById('wallet-recharge-area');
        if (!panel) return;
        if (!rechargeFlowState.open) {
            panel.classList.add('hidden');
            panel.innerHTML = '';
            return;
        }
        panel.classList.remove('hidden');
        panel.innerHTML = `
            <div class="wallet-recharge-panel">
                <div class="mono" style="margin-bottom:8px;">Add funds with card</div>
                <div class="form-group">
                    <label>Amount (USD)</label>
                    <input type="number" id="recharge-amount" min="1" max="5000" step="1" value="${rechargeFlowState.form.amount}" ${rechargeFlowState.loading ? 'disabled' : ''} />
                </div>
                <div class="form-group">
                    <label>Card Number</label>
                    <input type="text" id="recharge-card-number" placeholder="4111 1111 1111 1111" value="${rechargeFlowState.form.cardNumber}" ${rechargeFlowState.loading ? 'disabled' : ''} />
                </div>
                <div class="form-group">
                    <label>Name on Card</label>
                    <input type="text" id="recharge-card-holder" placeholder="Your full name" value="${rechargeFlowState.form.cardHolder}" ${rechargeFlowState.loading ? 'disabled' : ''} />
                </div>
                <div class="wallet-recharge-inline-row">
                    <div class="form-group" style="margin:0;">
                        <label>Expiry (MM/YY)</label>
                        <input type="text" id="recharge-card-expiry" placeholder="12/30" value="${rechargeFlowState.form.cardExpiry}" ${rechargeFlowState.loading ? 'disabled' : ''} />
                    </div>
                    <div class="form-group" style="margin:0;">
                        <label>CVV</label>
                        <input type="text" id="recharge-card-cvv" placeholder="123" value="${rechargeFlowState.form.cardCvv}" ${rechargeFlowState.loading ? 'disabled' : ''} />
                    </div>
                </div>
                ${rechargeFlowState.error ? `<p class="status-msg warning" style="margin-top:8px;">${rechargeFlowState.error}</p>` : ''}
                ${rechargeFlowState.success ? `<p class="mono" style="margin-top:8px; color:#22c55e;">${rechargeFlowState.success}</p>` : ''}
                <div class="wallet-recharge-actions">
                    <button class="primary-btn btn-small" data-wallet-action="confirm-recharge" ${rechargeFlowState.loading ? 'disabled' : ''}>${rechargeFlowState.loading ? 'Processing...' : 'Confirm Payment'}</button>
                    <button class="primary-btn btn-small wallet-cancel-btn" data-wallet-action="cancel-recharge" ${rechargeFlowState.loading ? 'disabled' : ''}>Cancel</button>
                </div>
            </div>
        `;
    }

    function renderChargeFlow() {
        const resultArea = document.getElementById('user-result-area');
        if (!resultArea) return;
        switch (chargeFlowState.step) {
            case "IDLE":
                resultArea.innerHTML = "";
                break;
            case "INIT":
                resultArea.innerHTML = `<p class="mono">Checking availability...</p>`;
                break;
            case "SELECT_SLOT":
                resultArea.innerHTML = `
                    <div class="mono">Select Slot</div>
                    ${chargeFlowState.data.available_slots.map(s => `
                        <button class="primary-btn btn-small" data-flow-action="slot" data-slot-id="${s.slot_id}" data-charger-type="${s.charger_type}">
                            Slot ${displaySlot(s.slot_id)} (${s.charger_type})
                        </button>
                    `).join('')}
                `;
                break;
            case "SELECT_TIME": {
                const savedDate = chargeFlowState.data.date || "";
                const savedWindow = chargeFlowState.data.time_window || "00:00-06:00";
                const savedKwh = Number(chargeFlowState.data.requested_kwh || 20);
                const savedRate = Number(chargeFlowState.data.charge_rate_kw || 7);
                resultArea.innerHTML = `
                    <div class="mono">Selected Slot ${displaySlot(chargeFlowState.data.slot_id)} (${chargeFlowState.data.charger_type})</div>
                    <div class="form-group">
                        <label>Date</label>
                        <input type="date" id="charge-date" />
                    </div>
                    <div class="form-group">
                        <label>Time Window</label>
                        <select id="charge-time-window">
                            <option value="00:00-06:00">00:00-06:00</option>
                            <option value="06:00-12:00">06:00-12:00</option>
                            <option value="12:00-18:00">12:00-18:00</option>
                            <option value="18:00-24:00">18:00-24:00</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Required Charge (kWh)</label>
                        <input type="number" id="charge-kwh" min="5" max="120" step="1" value="${savedKwh}" />
                    </div>
                    <div class="form-group">
                        <label>Charge Rate (kW)</label>
                        <input type="number" id="charge-rate-kw" min="3" max="150" step="1" value="${savedRate}" />
                    </div>
                    <button class="primary-btn btn-small" data-flow-action="quote">Get Quote</button>
                `;
                const dateEl = document.getElementById("charge-date");
                if (dateEl && savedDate) dateEl.value = savedDate;
                const tw = document.getElementById("charge-time-window");
                if (tw) tw.value = savedWindow;
                break;
            }
            case "WAIT_DECISION":
                resultArea.innerHTML = `
                    <p class="mono">${chargeFlowState.data.wait_message || 'No slot is currently free.'}</p>
                    <p class="mono">Earliest availability: ${chargeFlowState.data.wait_eta_minutes ?? '-'} min</p>
                    <button class="primary-btn btn-small" data-flow-action="find-reserve">Reserve & Pay Now</button>
                    <button class="primary-btn btn-small" data-flow-action="find-wait" style="margin-top:8px;">Wait For Availability</button>
                `;
                break;
            case "QUOTE":
                resultArea.innerHTML = `
                    <p class="mono">Quote generated</p>
                    <p class="mono">Price: $${chargeFlowState.data.quote.total_price.toFixed(2)}</p>
                    <p class="mono">Multiplier: ${chargeFlowState.data.quote.multiplier}</p>
                    <p class="mono">Requested: ${chargeFlowState.data.quote.requested_kwh} kWh @ ${chargeFlowState.data.quote.charge_rate_kw} kW</p>
                    <p class="mono">Expiry: ${new Date(chargeFlowState.data.quote.expires_at * 1000).toLocaleTimeString()}</p>
                    <button class="primary-btn btn-small" data-flow-action="pay">Proceed to Payment</button>
                `;
                break;
            case "PAYMENT":
                const walletBalance = Number(appState.snapshot?.user_wallet?.balance ?? 0);
                const payable = Number(chargeFlowState.data.quote?.total_price ?? 0);
                const hasFunds = walletBalance >= payable;
                resultArea.innerHTML = `
                    <div class="mono">Pay with Wallet</div>
                    <p class="mono">Amount: $${payable.toFixed(2)}</p>
                    <p class="mono">Wallet Balance: $${walletBalance.toFixed(2)}</p>
                    ${hasFunds ? '' : `<p class="mono" style="color: var(--accent-red)">Insufficient balance. Please recharge your wallet first.</p>`}
                    <button class="primary-btn btn-small" data-flow-action="pay-confirm" ${hasFunds ? '' : 'disabled'}>Pay from Wallet</button>
                    <button class="primary-btn btn-small" data-flow-action="pay-cancel" style="margin-top:8px;">Back to Quote</button>
                `;
                break;
            case "COMPLETE":
                resultArea.innerHTML = `
                    <p class="mono">Booking complete</p>
                    <p class="mono">Auth Code: ${chargeFlowState.data.auth_code}</p>
                    <button class="primary-btn btn-small" data-flow-action="close">Done</button>
                `;
                break;
            case "ERROR":
                resultArea.innerHTML = `
                    <p class="mono" style="color: var(--accent-red)">Error: ${chargeFlowState.error}</p>
                    <button class="primary-btn btn-small" data-flow-action="retry">Retry</button>
                `;
                break;
            case "START_CHARGING_CONFIRM":
                resultArea.innerHTML = `
                    <p class="mono">Authorization verified for Slot ${displaySlot(chargeFlowState.data.slot_id)}.</p>
                    <p class="mono">Press confirm once vehicle is parked correctly.</p>
                    <button class="primary-btn btn-small" data-flow-action="start-charge-confirm">Start Charging</button>
                `;
                break;
            default:
                resultArea.innerHTML = `<p class="mono">Unknown flow state</p>`;
        }
    }

    async function startChargeFlow(slotId = null, chargerType = null) {
        resetFlow();
        if (slotId != null && !Number.isNaN(Number(slotId))) {
            chargeFlowState.data.slot_id = Number(slotId);
            chargeFlowState.data.charger_type = chargerType || null;
        }
        console.log('[ChargeFlow] STEP_CHANGE: IDLE -> SELECT_SLOT');
        await transitionTo('SELECT_SLOT');
    }

    async function handleAvailability() {
        const payload = await getAvailability(appState.session.userId);
        chargeFlowState.data.available_slots = payload.slots || [];
        console.log('[ChargeFlow] API_SUCCESS: availability');
        if (chargeFlowState.data.available_slots.length === 0) {
            setFlowError('No slots available right now');
            return;
        }
        if (chargeFlowState.data.slot_id !== null && !Number.isNaN(Number(chargeFlowState.data.slot_id))) {
            const selected = chargeFlowState.data.available_slots.find(s => Number(s.slot_id) === Number(chargeFlowState.data.slot_id));
            if (selected) {
                chargeFlowState.data.charger_type = selected.charger_type;
                // Must be SELECT_SLOT before SELECT_TIME — transition guard only allows SELECT_SLOT -> SELECT_TIME.
                chargeFlowState.step = 'SELECT_SLOT';
                await transitionTo('SELECT_TIME');
                return;
            }
        }
        chargeFlowState.step = 'SELECT_SLOT';
        renderChargeFlow();
    }

    async function handleQuote() {
        const quote = await getPricingQuote({
            slot_id: chargeFlowState.data.slot_id,
            date: chargeFlowState.data.date,
            time_window: chargeFlowState.data.time_window,
            requested_kwh: chargeFlowState.data.requested_kwh,
            charge_rate_kw: chargeFlowState.data.charge_rate_kw,
            allow_waitlist: chargeFlowState.data.allow_waitlist,
            username: appState.session.userId
        });
        chargeFlowState.data.quote = quote;
        console.log('[ChargeFlow] API_SUCCESS: pricing_quote');
        chargeFlowState.step = 'QUOTE';
        renderChargeFlow();
    }

    async function handlePayment() {
        chargeFlowState.step = 'PAYMENT';
        renderChargeFlow();
        const payment = await processMockPayment({
            quote_id: chargeFlowState.data.quote.quote_id,
            username: appState.session.userId,
            method: 'WALLET'
        });
        chargeFlowState.data.payment = payment;
        console.log('[ChargeFlow] API_SUCCESS: payment_wallet');
    }

    async function finalizeBooking() {
        const res = await executeAction('book', {
            slot_id: chargeFlowState.data.slot_id,
            username: appState.session.userId,
            quote_id: chargeFlowState.data.quote.quote_id,
            date: chargeFlowState.data.date,
            time_window: chargeFlowState.data.time_window
        }, 'charge_flow_book');
        if (res?.status !== 'success') {
            throw new Error(res?.message || res?.error || 'Booking failed');
        }
        chargeFlowState.data.auth_code = res.auth_code || 'N/A';
        console.log('[ChargeFlow] API_SUCCESS: book');
    }

    async function transitionTo(step) {
        const fromStep = chargeFlowState.step;
        const validTransitions = {
            IDLE: ['SELECT_SLOT'],
            INIT: ['SELECT_SLOT'],
            SELECT_SLOT: ['SELECT_TIME', 'ERROR'],
            WAIT_DECISION: ['SELECT_TIME', 'ERROR'],
            SELECT_TIME: ['QUOTE', 'ERROR'],
            QUOTE: ['PAYMENT', 'ERROR'],
            PAYMENT: ['COMPLETE', 'ERROR'],
            COMPLETE: [],
            ERROR: ['SELECT_SLOT', 'SELECT_TIME', 'QUOTE', 'PAYMENT']
        };
        const allowed = validTransitions[fromStep] || [];
        if (!allowed.includes(step)) {
            console.warn(`[ChargeFlow] invalid transition blocked: ${fromStep} -> ${step}`);
            return;
        }
        if (fromStep !== 'ERROR') {
            // Starting from IDLE (dashboard) is not a meaningful retry target; treat as slot step.
            chargeFlowState.previousStep = fromStep === 'IDLE' ? 'SELECT_SLOT' : fromStep;
        }
        console.log(`[ChargeFlow] STEP_CHANGE: ${fromStep} -> ${step}`);

        try {
            if (step === 'SELECT_SLOT') {
                chargeFlowState.step = 'INIT';
                renderChargeFlow();
                await handleAvailability();
                return;
            }
            if (step === 'SELECT_TIME') {
                const sid = chargeFlowState.data.slot_id;
                if (sid == null || Number.isNaN(Number(sid))) throw new Error('Please select a slot first');
                chargeFlowState.step = 'SELECT_TIME';
                renderChargeFlow();
                return;
            }
            if (step === 'QUOTE') {
                const sid = chargeFlowState.data.slot_id;
                if (sid == null || Number.isNaN(Number(sid)) || !chargeFlowState.data.date || !chargeFlowState.data.time_window) {
                    throw new Error('Please select slot, date and time');
                }
                await handleQuote();
                return;
            }
            if (step === 'PAYMENT') {
                if (!chargeFlowState.data.quote) throw new Error('Quote missing');
                chargeFlowState.step = 'PAYMENT';
                renderChargeFlow();
                return;
            }
            chargeFlowState.step = step;
            renderChargeFlow();
        } catch (err) {
            setFlowError(err.message || 'Flow failed');
        }
    }

    function initialRender() {
        container.innerHTML = `
            <div class="user-dashboard-grid">
                <div class="card wallet-card glass" id="wallet-area">
                    <div class="wallet-header">
                        <h3>Your Wallet</h3>
                        <span class="wallet-id" id="wallet-user-id">ID: ---</span>
                    </div>
                    <div class="balance-area">
                        <span class="currency">$</span>
                        <span class="balance" id="user-balance">0.00</span>
                    </div>
                    <button class="primary-btn btn-small" id="btn-recharge">Quick Recharge $50</button>
                    <div id="wallet-recharge-area" class="hidden"></div>
                </div>
                <div class="card camera-card glass" id="camera-status-card">
                    <div class="wallet-header">
                        <h3>Camera Status</h3>
                        <span class="wallet-id" id="camera-status-mode">--</span>
                    </div>
                    <div class="camera-status-line">
                        <span class="status-dot status-dot-amber" id="camera-status-dot" aria-hidden="true"></span>
                        <div class="mono camera-status-text-amber" id="camera-status-text">Waiting for status...</div>
                    </div>
                </div>
                <div class="card slots-card glass">
                    <div class="card-header">
                        <h3>Available Slots</h3>
                        <span class="count-badge" id="free-slots-count">0 Free</span>
                    </div>
                    <div class="slot-grid" id="slot-grid-area"></div>
                </div>
                <div class="card actions-card glass">
                    <h3>Smart Allocation</h3>
                    <div class="form-group">
                        <label>Vehicle Type</label>
                        <select id="user-vehicle">
                            <option value="SUV">SUV</option>
                            <option value="Sedan">Sedan</option>
                            <option value="Truck">Truck</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Urgency</label>
                        <select id="user-urgency">
                            <option value="LOW">Low</option>
                            <option value="HIGH">High</option>
                        </select>
                    </div>
                    <button class="primary-btn" id="btn-find-slot">Find Best Slot</button>
                    <div id="user-result-area"></div>
                    <div class="booking-table-wrap">
                        <h4 class="mono" style="margin: 10px 0 6px;">Booked Sessions</h4>
                        <div id="user-bookings-table"></div>
                    </div>
                </div>
            </div>
        `;

        container.addEventListener('click', async (e) => {
            const btn = e.target.closest('.btn-charge, #btn-recharge, #btn-find-slot, [data-flow-action], [data-wallet-action]');
            if (!btn) return;
            const rawSlotId = btn.dataset.slotId;
            const slotId = rawSlotId === undefined || rawSlotId === '' ? NaN : Number(rawSlotId);
            const action = btn.dataset.action;
            const flowAction = btn.dataset.flowAction;

            if (btn.id === 'btn-recharge') {
                rechargeFlowState.open = true;
                rechargeFlowState.error = null;
                rechargeFlowState.success = null;
                renderWalletRechargePanel();
                return;
            }

            const walletAction = btn.dataset.walletAction;
            if (walletAction === 'cancel-recharge') {
                rechargeFlowState.open = false;
                rechargeFlowState.loading = false;
                rechargeFlowState.error = null;
                rechargeFlowState.success = null;
                renderWalletRechargePanel();
                return;
            }
            if (walletAction === 'confirm-recharge') {
                const amount = Number(rechargeFlowState.form.amount || 0);
                const cardNumber = rechargeFlowState.form.cardNumber.trim();
                const cardHolder = rechargeFlowState.form.cardHolder.trim();
                const cardExpiry = rechargeFlowState.form.cardExpiry.trim();
                const cardCvv = rechargeFlowState.form.cardCvv.trim();

                if (!Number.isFinite(amount) || amount <= 0) {
                    rechargeFlowState.error = 'Please enter a valid recharge amount';
                    rechargeFlowState.success = null;
                    renderWalletRechargePanel();
                    return;
                }
                const cardError = validateCardInputs(cardNumber, cardHolder, cardExpiry, cardCvv);
                if (cardError) {
                    rechargeFlowState.error = cardError;
                    rechargeFlowState.success = null;
                    renderWalletRechargePanel();
                    return;
                }

                rechargeFlowState.loading = true;
                rechargeFlowState.error = null;
                rechargeFlowState.success = null;
                renderWalletRechargePanel();
                const res = await executeAction(
                    'recharge',
                    { amount, username: appState.session.userId },
                    `recharge_${appState.session.userId}`
                );
                rechargeFlowState.loading = false;
                if (res?.status === 'success') {
                    rechargeFlowState.success = `Payment successful. Wallet recharged by $${amount.toFixed(2)}.`;
                    rechargeFlowState.error = null;
                    rechargeFlowState.form = { amount: '50', cardNumber: '', cardHolder: '', cardExpiry: '', cardCvv: '' };
                } else {
                    rechargeFlowState.error = res?.message || res?.error || 'Recharge failed';
                    rechargeFlowState.success = null;
                }
                renderWalletRechargePanel();
                return;
            }

            if (flowAction === 'slot') {
                chargeFlowState.data.slot_id = Number(btn.dataset.slotId);
                chargeFlowState.data.charger_type = btn.dataset.chargerType || 'STANDARD';
                await transitionTo('SELECT_TIME');
                return;
            }
            if (flowAction === 'quote') {
                chargeFlowState.data.date = document.getElementById('charge-date')?.value || null;
                chargeFlowState.data.time_window = document.getElementById('charge-time-window')?.value || null;
                chargeFlowState.data.requested_kwh = Number(document.getElementById('charge-kwh')?.value || 20);
                chargeFlowState.data.charge_rate_kw = Number(document.getElementById('charge-rate-kw')?.value || 7);
                await transitionTo('QUOTE');
                return;
            }
            if (flowAction === 'find-reserve') {
                chargeFlowState.data.allow_waitlist = true;
                await transitionTo('SELECT_TIME');
                return;
            }
            if (flowAction === 'find-wait') {
                chargeFlowState.step = 'IDLE';
                renderChargeFlow();
                const resultArea = document.getElementById('user-result-area');
                if (resultArea) {
                    resultArea.innerHTML = `<p class="mono">You can wait ~${chargeFlowState.data.wait_eta_minutes ?? '-'} min for earliest slot.</p>`;
                }
                return;
            }
            if (flowAction === 'pay') {
                await transitionTo('PAYMENT');
                return;
            }
            if (flowAction === 'pay-cancel') {
                chargeFlowState.step = 'QUOTE';
                renderChargeFlow();
                return;
            }
            if (flowAction === 'pay-confirm') {
                const resultArea = document.getElementById('user-result-area');
                if (resultArea) resultArea.innerHTML = `<p class="mono">Processing wallet payment...</p>`;
                try {
                    await handlePayment();
                    await finalizeBooking();
                    chargeFlowState.step = 'COMPLETE';
                    renderChargeFlow();
                } catch (err) {
                    setFlowError(err.message || 'Payment or booking failed');
                }
                return;
            }
            if (flowAction === 'retry') {
                await transitionTo(chargeFlowState.previousStep || 'SELECT_SLOT');
                return;
            }
            if (flowAction === 'close') {
                resetFlow();
                renderChargeFlow();
                return;
            }

            if (action === 'book') {
                if (Number.isNaN(slotId)) {
                    console.warn('[ChargeFlow] Charge clicked without valid slot id');
                    return;
                }
                await startChargeFlow(slotId);
                return;
            }
            if (action === 'authorize') {
                const code = prompt(`Enter Authorization Code for Slot ${slotId}:`);
                if (code) {
                    const authRes = await executeAction('authorize', { slot_id: slotId, code: code, username: appState.session.userId });
                    if (authRes?.status === 'success') {
                        chargeFlowState.data.slot_id = Number(slotId);
                        chargeFlowState.data.auth_code = code;
                        chargeFlowState.step = 'START_CHARGING_CONFIRM';
                        renderChargeFlow();
                    } else {
                        setFlowError(authRes?.message || authRes?.error || authRes?.code || 'Authorization failed');
                    }
                }
                return;
            }
            if (flowAction === 'start-charge-confirm') {
                const startRes = await executeAction('start_charging', {
                    slot_id: chargeFlowState.data.slot_id,
                    code: chargeFlowState.data.auth_code,
                    username: appState.session.userId
                }, `start_charging_${chargeFlowState.data.slot_id}`);
                if (startRes?.status === 'success') {
                    chargeFlowState.step = 'IDLE';
                    renderChargeFlow();
                    const resultArea = document.getElementById('user-result-area');
                    // start_charging API returns slot_id as 1-based for user readability.
                    if (resultArea) resultArea.innerHTML = `<p class="mono">Charging started at Slot ${startRes.slot_id}.</p>`;
                } else {
                    setFlowError(startRes?.message || startRes?.error || 'Unable to start charging');
                }
                return;
            }
            if (btn.id === 'btn-find-slot') {
                isLoading = true;
                update();
                try {
                    const lookupDate = document.getElementById('charge-date')?.value || new Date().toISOString().slice(0, 10);
                    const lookupWindow = document.getElementById('charge-time-window')?.value || '06:00-12:00';
                    const result = await executeAction('find_slot', {
                        type: document.getElementById('user-vehicle').value,
                        urgency: document.getElementById('user-urgency').value,
                        date: lookupDate,
                        time_window: lookupWindow,
                        username: appState.session.userId
                    }, 'find_slot');
                    if (result?.status !== 'success') {
                        setFlowError(result?.message || result?.error || 'Unable to find slot');
                        return;
                    }
                    const rec = result.recommended_slot;
                    if (!rec) {
                        setFlowError('No recommended slot found');
                        return;
                    }
                    chargeFlowState.data.slot_id = Number(rec.slot_id);
                    chargeFlowState.data.charger_type = rec.charger_type || 'STANDARD';
                    chargeFlowState.data.date = result.date || lookupDate;
                    chargeFlowState.data.time_window = result.time_window || lookupWindow;
                    chargeFlowState.data.allow_waitlist = false;
                    if (result.mode === 'WAIT') {
                        chargeFlowState.data.wait_eta_minutes = Number(result.eta_minutes || 0);
                        chargeFlowState.data.wait_message = result.message || 'No slot is currently free.';
                        chargeFlowState.step = 'WAIT_DECISION';
                        renderChargeFlow();
                    } else {
                        chargeFlowState.step = 'SELECT_SLOT';
                        await transitionTo('SELECT_TIME');
                    }
                } finally {
                    isLoading = false;
                    update();
                }
                return;
            }
        });

        container.addEventListener('input', (e) => {
            const target = e.target;
            if (!(target instanceof HTMLInputElement)) return;
            if (target.id === 'recharge-amount') rechargeFlowState.form.amount = target.value;
            if (target.id === 'recharge-card-number') rechargeFlowState.form.cardNumber = target.value;
            if (target.id === 'recharge-card-holder') rechargeFlowState.form.cardHolder = target.value;
            if (target.id === 'recharge-card-expiry') rechargeFlowState.form.cardExpiry = target.value;
            if (target.id === 'recharge-card-cvv') rechargeFlowState.form.cardCvv = target.value;
        });
    }

    function update() {
        const isUser = appState.uiMode === 'USER' && appState.authStatus !== 'GUEST';
        if (!isUser) {
            container.classList.add('hidden');
            return;
        }
        container.classList.remove('hidden');
        if (!appState.snapshot) return;
        const snapshot = appState.snapshot;
        const wallet = snapshot.user_wallet || { balance: 0, currency: 'USD' };

        const balanceEl = document.getElementById('user-balance');
        if (balanceEl) balanceEl.innerText = wallet.balance.toFixed(2);
        const userIdEl = document.getElementById('wallet-user-id');
        if (userIdEl) userIdEl.innerText = `ID: ${appState.session.userId}`;

        const slotGridArea = document.getElementById('slot-grid-area');
        if (slotGridArea) {
            const sortedSlots = [...snapshot.slots].sort((a, b) => a.slot_id - b.slot_id);
            const currentHash = JSON.stringify(sortedSlots.map(s => ({ id: s.slot_id, state: s.state })));
            if (currentHash !== lastSlotHash) {
                slotGridArea.innerHTML = sortedSlots.map(slot => `
                    <div class="slot-item ${slot.state.toLowerCase()}">
                        <div class="slot-info">
                            <span class="slot-label">Slot ${displaySlot(slot.slot_id)}</span>
                            <span class="slot-status ${slot.state === 'AUTH_PENDING' ? 'status-pulse' : ''}">${slot.state}</span>
                        </div>
                        ${slot.state === 'FREE' ? `
                            <button class="btn-charge" data-action="book" data-slot-id="${slot.slot_id}">Charge</button>
                        ` : slot.state === 'AUTH_PENDING' ? `
                            <button class="btn-charge btn-auth" data-action="authorize" data-slot-id="${slot.slot_id}">Authorize</button>
                        ` : `
                            <div class="assigned-user">ID: ${slot.assigned_global_id || '---'}</div>
                        `}
                    </div>
                `).join('');
                lastSlotHash = currentHash;
            }
        }

        const countBadge = document.getElementById('free-slots-count');
        const freeCount = snapshot.slots.filter(s => s.state === 'FREE').length;
        if (countBadge) countBadge.innerText = `${freeCount} Free`;

        const findBtn = document.getElementById('btn-find-slot');
        // Do not tie to allowActions: it flips with camera/sync (~100ms) and makes the button unusable.
        // find_slot is allowed while waiting for vision (see api_v3 executeAction allowlist).
        if (findBtn) findBtn.disabled = isLoading;

        const camMode = snapshot.system_mode || snapshot.mode || 'UNKNOWN';
        const displayState = appState.displayState || '';
        const camModeEl = document.getElementById('camera-status-mode');
        const camDotEl = document.getElementById('camera-status-dot');
        const camTextEl = document.getElementById('camera-status-text');
        if (camModeEl) camModeEl.textContent = camMode;
        if (camTextEl) {
            let statusColor = 'green';
            let statusText = 'Camera active and streaming.';

            if (displayState === 'WAITING_FOR_CAMERA' || displayState === 'INITIALIZING' || camMode === 'WAITING_FOR_CAMERA') {
                statusColor = 'amber';
                statusText = 'Camera is waiting to initialize.';
            } else if (
                displayState === 'DISCONNECTED' ||
                displayState === 'FROZEN' ||
                displayState === 'FROZEN_UNKNOWN' ||
                displayState === 'DESYNCHRONIZED' ||
                displayState === 'DEGRADED' ||
                displayState === 'DEGRADED_MODE' ||
                camMode === 'DEGRADED'
            ) {
                statusColor = 'red';
                statusText = 'Camera signal is unstable. Some actions may be limited.';
            }

            camTextEl.textContent = statusText;
            camTextEl.classList.remove('camera-status-text-green', 'camera-status-text-amber', 'camera-status-text-red');
            camTextEl.classList.add(`camera-status-text-${statusColor}`);

            if (camDotEl) {
                camDotEl.classList.remove('status-dot-green', 'status-dot-amber', 'status-dot-red');
                camDotEl.classList.add(`status-dot-${statusColor}`);
            }
        }

        const bookingsTable = document.getElementById('user-bookings-table');
        if (bookingsTable) {
            const rows = Array.isArray(snapshot.user_bookings) ? snapshot.user_bookings : [];
            if (rows.length === 0) {
                bookingsTable.innerHTML = `<p class="mono" style="opacity:.8">No bookings yet</p>`;
            } else {
                bookingsTable.innerHTML = `
                    <table style="width:100%; border-collapse:collapse; font-size:12px;">
                        <thead>
                            <tr>
                                <th style="text-align:left; padding:4px;">Slot</th>
                                <th style="text-align:left; padding:4px;">Date</th>
                                <th style="text-align:left; padding:4px;">Time</th>
                                <th style="text-align:left; padding:4px;">Auth</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${rows.map((b) => `
                                <tr>
                                    <td style="padding:4px;">${displaySlot(b.slot_id)}</td>
                                    <td style="padding:4px;">${b.date || '-'}</td>
                                    <td style="padding:4px;">${b.time_window || '-'}</td>
                                    <td style="padding:4px;" class="mono">${b.auth_code || 'N/A'}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
            }
        }

        const activeSessionsWrapId = 'user-active-sessions-table';
        let activeWrap = document.getElementById(activeSessionsWrapId);
        if (!activeWrap) {
            const bookingsWrap = document.querySelector('.booking-table-wrap');
            if (bookingsWrap) {
                const host = document.createElement('div');
                host.innerHTML = `<h4 class="mono" style="margin: 10px 0 6px;">Active Charging</h4><div id="${activeSessionsWrapId}"></div>`;
                bookingsWrap.parentNode.insertBefore(host, bookingsWrap);
                activeWrap = document.getElementById(activeSessionsWrapId);
            }
        }
        if (activeWrap) {
            const sessions = Array.isArray(snapshot.user_active_sessions) ? snapshot.user_active_sessions : [];
            if (sessions.length === 0) {
                activeWrap.innerHTML = `<p class="mono" style="opacity:.8">No active charging session</p>`;
            } else {
                activeWrap.innerHTML = `
                    <table style="width:100%; border-collapse:collapse; font-size:12px;">
                        <thead>
                            <tr>
                                <th style="text-align:left; padding:4px;">Slot</th>
                                <th style="text-align:left; padding:4px;">Battery</th>
                                <th style="text-align:left; padding:4px;">Power</th>
                                <th style="text-align:left; padding:4px;">Energy</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${sessions.map((s) => `
                                <tr>
                                    <td style="padding:4px;">${displaySlot(s.slot_id)}</td>
                                    <td style="padding:4px;">${Number(s.battery_pct).toFixed(1)}%</td>
                                    <td style="padding:4px;">${Number(s.power_kw).toFixed(1)} kW</td>
                                    <td style="padding:4px;">${Number(s.energy_kwh).toFixed(2)} kWh</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
            }
        }
    }

    initialRender();
    events.on('STATE_UPDATED', () => update());
    events.on('ACTIONS_CHANGED', () => update());
    events.on('API_ERROR', () => { isLoading = false; update(); });
    events.on('CHARGE_FLOW_START', (payload) => {
        void startChargeFlow(payload?.slot_id ?? null, payload?.charger_type ?? null).catch((e) => console.error('[ChargeFlow] CHARGE_FLOW_START failed', e));
    });
    update();
}
