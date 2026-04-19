/**
 * loader.js — Startup Animation Handler.
 * Manages the initialization sequence and transitions to the main dashboard.
 */

(function () {
    // --- Configuration & Constants ---
    const CONFIG = {
        POLL_INTERVAL: 1000,
        ERROR_RETRY_DELAY: 2000,
        MESSAGE_STAGGER: 100,
        MINER_STAGGER: 100,
        MAX_MINERS_IN_TRAY: 50,
        TRANSITION_WELCOME_DELAY: 2000,
        TRANSITION_FADEOUT_DELAY: 500
    };

    // --- DOM Elements ---
    const elements = {
        overlay: document.getElementById('loader-overlay'),
        progressBar: document.querySelector('.loader-progress-bar'),
        statusText: document.querySelector('.loader-status-text'),
        detailsContainer: document.querySelector('.loader-details'),
        welcomeScreen: document.querySelector('.loader-welcome'),
        startBtnWrapper: document.getElementById('loader-start-button-wrapper'),
        startBtn: document.getElementById('loader-start-btn'),
        minersTray: document.getElementById('loader-miners-tray'),
        minersTrack: document.querySelector('#loader-miners-tray .miners-track')
    };

    // --- State Management ---
    const state = {
        lastDetailsCount: 0,
        detailsQueue: [],
        isProcessingDetailsQueue: false,

        renderedMinersCount: 0,
        minerQueue: [],
        isProcessingMinerQueue: false,

        isTransitioning: false,
        systemReady: false,
        hasFailed: false,

        // Tracks the two-phase button lifecycle
        phase: 'connecting', // 'connecting' -> 'waiting' -> 'initializing' -> 'ready'

        // Logical interlock for final readiness
        backendReady: false,
        initResults: null,
        
        // Timing trackers
        lastMinerAddedTime: 0
    };
    // --- Initialization ---
    function init() {
        setupEventListeners();
        startPolling();
    }

    // Allow scroll through the miners tray with the mouse wheel
    function setupEventListeners() {
        if (elements.minersTray) {
            elements.minersTray.addEventListener('wheel', (e) => {
                if (e.deltaY !== 0) {
                    e.preventDefault();
                    elements.minersTray.scrollLeft += e.deltaY;
                }
            });
        }
        elements.startBtn.addEventListener('click', handleButtonClick);
    }

    // Checks if we can finally show the "ENTER DASHBOARD" button
    function tryShowReadyButton() {
        if (!state.backendReady || state.systemReady) return;

        // Only show if all visual logs and miner cards are finished rendering
        const queuesEmpty = state.detailsQueue.length === 0 && state.minerQueue.length === 0;
        const processingFinished = !state.isProcessingDetailsQueue && !state.isProcessingMinerQueue;

        if (queuesEmpty && processingFinished) {
            state.systemReady = true;
            state.phase = 'ready';
            elements.startBtn.textContent = 'Enter Dashboard';
            elements.startBtnWrapper.classList.remove('closing');
            elements.startBtnWrapper.classList.remove('invisible');

            // Final status polish
            elements.statusText.textContent = "System Ready - Access Granted";
        }
    }

    // --- Button Logic: Two-phase handler ---
    async function handleButtonClick() {
        if (state.isTransitioning) return;

        if (state.phase === 'waiting') {
            // Phase 1: Operator confirms → trigger backend initialization
            elements.startBtn.disabled = true;

            try {
                const res = await fetch('/api/init-status', { method: 'POST' });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);

                // Hide button, switch to initializing phase
                state.phase = 'initializing';
                elements.startBtnWrapper.classList.add('closing');
                setTimeout(() => {
                    elements.startBtnWrapper.classList.add('invisible');
                    elements.startBtn.disabled = false;
                }, 500);

                // Resume polling for progress
                startPolling();
            } catch (err) {
                appendLog('', `\u001b[31m[Loader] Failed to trigger init: ${err.message || err}\u001b[0m`, 'ERROR');
                elements.startBtn.textContent = 'Retry Start';
                elements.startBtn.disabled = false;
            }

        } else if (state.phase === 'ready') {
            // Phase 2: System ready → enter dashboard
            elements.startBtnWrapper.classList.add('closing');
            setTimeout(() => {
                elements.startBtnWrapper.classList.add('invisible');
                finishLoading();
            }, 500);
        }
    }

    // --- Core Logic: Polling ---
    async function startPolling() {
        if (state.isTransitioning || state.hasFailed) return;

        try {
            const response = await fetch('/api/init-status');
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

            const data = await response.json();

            // 1. Check for Failure
            if (data.failed) {
                handleFailure(data.error_message || data.step || "Startup failed");
                return;
            }

            // 3. Update Progress Bar
            if (data.percentage !== undefined) {
                elements.progressBar.style.width = `${data.percentage}%`;
            }

            // 4. Update Status Step
            if (data.step) {
                elements.statusText.textContent = data.step.toUpperCase();
            }

            // 5. Update Detailed Messages
            if (data.details && data.details.length > state.lastDetailsCount) {
                for (let i = state.lastDetailsCount; i < data.details.length; i++) {
                    state.detailsQueue.push(data.details[i]);
                }
                state.lastDetailsCount = data.details.length;
                processDetailsQueue();
            }

            // 6. Update Discovered Miners Tray
            if (data.miners && data.miners.length > state.renderedMinersCount) {
                for (let i = state.renderedMinersCount; i < data.miners.length; i++) {
                    state.minerQueue.push(data.miners[i]);
                }
                state.renderedMinersCount = data.miners.length;
                processMinerQueue();
            }

            // 7. Handle "waiting for operator" state
            // This is checked AFTER processing details to ensure initial logs show up
            if (data.waiting && state.phase !== 'waiting') {
                state.phase = 'waiting';
                elements.statusText.textContent = 'Waiting the Operator...';
                elements.startBtn.textContent = 'Start System Engine';
                elements.startBtnWrapper.classList.remove('invisible');
                // Don't poll while waiting — the button click will resume
                return;
            }

            // If still in waiting phase, don't trigger next poll
            if (data.waiting) return;

            // 8. Handle Completion (Deferred)
            if (data.ready && !state.backendReady) {
                state.backendReady = true;
                state.initResults = data.results; // Bundle captured here
                tryShowReadyButton();
            } else if (!state.backendReady) {
                setTimeout(startPolling, CONFIG.POLL_INTERVAL);
            }

        } catch (error) {
            appendLog('', `\u001b[31m[Loader] Init poll error: ${error.message || error}\u001b[0m`, 'ERROR');
            elements.statusText.textContent = 'Connecting to Core...';
            setTimeout(startPolling, CONFIG.ERROR_RETRY_DELAY);
        }
    }

    // --- UI Update Helpers ---

    function handleFailure(message) {
        state.hasFailed = true;
        elements.statusText.textContent = 'Error: Initialization Failed';
        elements.statusText.style.color = '#ff4444';
        elements.progressBar.style.backgroundColor = '#ff4444';
        elements.progressBar.style.boxShadow = '0 0 15px rgba(255, 68, 68, 0.5)';

        // Guarantee that the start button is hidden if there is a failure
        if (elements.startBtnWrapper) {
            elements.startBtnWrapper.classList.add('invisible');
        }

        addDetailItem(`CRITICAL: ${message}`);
        addDetailItem("Please check your configuration or logs, then refresh the page.");
    }

    function addDetailItem(text) {
        const item = document.createElement('div');
        item.className = 'loader-detail-item';
        item.textContent = text;
        elements.detailsContainer.prepend(item);
    }

    function processDetailsQueue() {
        if (state.isProcessingDetailsQueue || state.detailsQueue.length === 0) return;
        state.isProcessingDetailsQueue = true;

        const next = () => {
            if (state.detailsQueue.length > 0) {
                addDetailItem(state.detailsQueue.shift());
                setTimeout(next, CONFIG.MESSAGE_STAGGER);
            } else {
                state.isProcessingDetailsQueue = false;
                tryShowReadyButton(); // Check stability
            }
        };
        next();
    }

    function renderMinerDiscovery(miner) {
        if (!elements.minersTrack) return;

        const card = document.createElement('div');
        card.className = 'discovered-miner-card';
        card.title = miner.name;

        if (miner.image) {
            const img = document.createElement('img');
            img.src = miner.image;
            img.alt = miner.name;
            card.appendChild(img);
        }

        elements.minersTrack.appendChild(card);

        // Limit track size to avoid memory bloat
        if (elements.minersTrack.children.length > CONFIG.MAX_MINERS_IN_TRAY) {
            elements.minersTrack.removeChild(elements.minersTrack.firstChild);
        }
        
        // Update the end-time for the scroll sync loop (animation duration is ~0.8s)
        state.lastMinerAddedTime = performance.now();
    }

    // Unified Scroll Sync Loop (Frame-perfect)
    function syncTrayScroll(timestamp) {
        if (!elements.minersTray) return;

        // Keep syncing as long as we are processing the queue OR an animation is still finishing (1.5s buffer)
        const isStillAnimating = timestamp < (state.lastMinerAddedTime + 1500);
        
        if (state.isProcessingMinerQueue || isStillAnimating) {
            // Fix: If overflowing, switch from 'center' to 'flex-start' to avoid left-side clipping
            if (elements.minersTray.scrollWidth > elements.minersTray.clientWidth) {
                elements.minersTray.style.justifyContent = 'flex-start';
            }

            elements.minersTray.scrollLeft = elements.minersTray.scrollWidth;
            requestAnimationFrame(syncTrayScroll);
        }
    }

    function processMinerQueue() {
        if (state.isProcessingMinerQueue || state.minerQueue.length === 0) return;
        state.isProcessingMinerQueue = true;
        
        // Start the intelligent scroll sync loop
        requestAnimationFrame(syncTrayScroll);

        const next = () => {
            if (state.minerQueue.length > 0) {
                renderMinerDiscovery(state.minerQueue.shift());
                setTimeout(next, CONFIG.MINER_STAGGER);
            } else {
                state.isProcessingMinerQueue = false;
                tryShowReadyButton(); // Check stability
            }
        };
        next();
    }

    function finishLoading() {
        state.isTransitioning = true;

        // Final UI Polish
        elements.progressBar.style.width = '100%';
        elements.statusText.textContent = 'Opening the Operator interface...';

        // Trigger Dashboard Data Load with preloaded results
        if (typeof window.initDashboard === 'function') {
            window.initDashboard(state.initResults);
        }

        setTimeout(() => {
            elements.welcomeScreen.classList.add('active');

            setTimeout(() => {
                // Phase 2: Start the fade-out animation
                elements.overlay.classList.add('closing');

                // Phase 3: Total cleanup after the animation is done
                setTimeout(() => {
                    elements.overlay.classList.add('hidden');
                    elements.overlay.remove();
                }, CONFIG.TRANSITION_FADEOUT_DELAY);
            }, CONFIG.TRANSITION_WELCOME_DELAY);
        }, 800);
    }

    // Launch
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
