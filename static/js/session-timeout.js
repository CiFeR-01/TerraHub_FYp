// Idle sign-out with a warning popup (markup in base.html, settings in TerraHub/settings.py).
// Mirrors the server's SESSION_IDLE_TIMEOUT deadline, extends it on activity, warns before it expires,
// and shares the last-refresh time across tabs through localStorage.
(function () {
    var modal = document.getElementById('session-timeout-modal');
    if (!modal) return;

    var TIMEOUT_MS = parseInt(modal.dataset.timeout, 10) * 1000;
    var WARNING_MS = parseInt(modal.dataset.warning, 10) * 1000;
    var KEEPALIVE_URL = modal.dataset.keepaliveUrl;
    var LOGOUT_URL = modal.dataset.logoutUrl;
    var LOGIN_URL = modal.dataset.loginUrl;
    var PING_EVERY_MS = 60 * 1000;   // at most one background ping a minute
    var STORAGE_KEY = 'th_session_touched_at';

    var countdownEl = document.getElementById('session-timeout-countdown');
    var stayBtn = document.getElementById('session-timeout-stay');
    var logoutBtn = document.getElementById('session-timeout-logout');
    var csrfInput = document.querySelector('input[name="csrfmiddlewaretoken"]');

    var touchedAt = Date.now();      // this page load counts as a request
    var lastActivity = 0;
    var pinging = false;
    var signingOut = false;

    function sharedTouchedAt() {
        try {
            var stored = parseInt(localStorage.getItem(STORAGE_KEY), 10);
            if (stored > touchedAt) touchedAt = stored;
        } catch (e) {}
        return touchedAt;
    }

    function markTouched() {
        touchedAt = Date.now();
        try { localStorage.setItem(STORAGE_KEY, String(touchedAt)); } catch (e) {}
    }
    markTouched();

    function goToLogin() {
        var next = window.location.pathname + window.location.search;
        window.location.href = LOGIN_URL + '?reason=idle&next=' + encodeURIComponent(next);
    }

    function ping() {
        if (pinging || signingOut) return;
        pinging = true;
        fetch(KEEPALIVE_URL, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'X-CSRFToken': csrfInput ? csrfInput.value : '',
                'X-Requested-With': 'XMLHttpRequest'
            }
        }).then(function (resp) {
            if (resp.status === 401) { signingOut = true; goToLogin(); return; }
            if (resp.ok) { markTouched(); hideWarning(); }
        }).catch(function () {
            // Network blip: leave the deadline alone and try again on the next tick.
        }).then(function () { pinging = false; });
    }

    function signOut() {
        if (signingOut) return;
        signingOut = true;
        var body = new FormData();
        if (csrfInput) body.append('csrfmiddlewaretoken', csrfInput.value);
        fetch(LOGOUT_URL, { method: 'POST', credentials: 'same-origin', body: body })
            .catch(function () {})
            .then(goToLogin);
    }

    function showWarning() {
        if (!modal.hidden) return;
        modal.hidden = false;
        stayBtn.focus();
    }

    function hideWarning() {
        if (modal.hidden) return;
        modal.hidden = true;
    }

    function formatRemaining(ms) {
        var secs = Math.max(0, Math.ceil(ms / 1000));
        var m = Math.floor(secs / 60);
        var s = secs % 60;
        return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function tick() {
        if (signingOut) return;
        var now = Date.now();
        var remaining = sharedTouchedAt() + TIMEOUT_MS - now;

        if (remaining <= 0) { signOut(); return; }

        if (remaining <= WARNING_MS) {
            // Once the popup is up, only the button keeps the session.
            showWarning();
            countdownEl.textContent = formatRemaining(remaining);
            return;
        }

        hideWarning();
        if (lastActivity > touchedAt && now - touchedAt >= PING_EVERY_MS) ping();
    }

    ['mousedown', 'keydown', 'scroll', 'touchstart', 'mousemove'].forEach(function (evt) {
        document.addEventListener(evt, function () {
            if (modal.hidden) lastActivity = Date.now();
        }, { passive: true, capture: true });
    });

    stayBtn.addEventListener('click', ping);
    logoutBtn.addEventListener('click', signOut);

    // Another tab refreshed the session: pick it up straight away.
    window.addEventListener('storage', function (e) {
        if (e.key === STORAGE_KEY) tick();
    });
    // Timers are throttled in background tabs, so re-check when the tab is visible again.
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) tick();
    });

    setInterval(tick, 1000);
})();
