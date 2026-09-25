/* Live phone/email checks for contact forms.
 *
 * Mirrors core/validators.py, which is the check that actually counts - this is
 * only feedback while typing. Load vendor/libphonenumber-max.js and
 * vendor/phone-examples.js first.
 *
 * Markup:
 *   <div data-phone-field>                     country dropdown + number box
 *     <select name="phone_country"></select>   (options are filled in here)
 *     <input type="tel" name="phone">
 *   </div>
 *   <input data-email-field>                   email only
 *   <input data-phone-or-email>                either one (shipment contact)
 *
 * A form's submit buttons are disabled while any checked field in it is invalid.
 */
(function () {
    'use strict';

    var lib = window.libphonenumber;
    var DEFAULT_COUNTRY = 'MY';
    // Same shape Django's validate_email requires: something@domain.tld
    var EMAIL_RE = /^[^\s@]+@[^\s@.]+(\.[^\s@.]+)*\.[A-Za-z]{2,}$/;
    var regionNames = null;
    try { regionNames = new Intl.DisplayNames(['en'], { type: 'region' }); } catch (e) { /* old browser: show codes */ }

    function countryName(code) {
        try { return (regionNames && regionNames.of(code)) || code; } catch (e) { return code; }
    }

    function exampleNumber(country) {
        var raw = window.PHONE_EXAMPLES && window.PHONE_EXAMPLES[country];
        if (!raw || !lib) return '';
        try {
            var p = lib.parsePhoneNumber(raw, country);
            return country === DEFAULT_COUNTRY ? p.formatNational() : p.formatInternational();
        } catch (e) { return ''; }
    }

    function withExample(msg, country) {
        var ex = exampleNumber(country);
        return ex ? msg + ' (e.g. ' + ex + ')' : msg;
    }

    function checkPhone(value, country) {
        value = (value || '').trim();
        if (!value || !lib) return '';
        var p;
        try {
            p = lib.parsePhoneNumberWithError(value, country);
        } catch (e) {
            return withExample('Not a phone number', country);
        }
        if (!p.isValid()) {
            var c = p.country || country;
            return withExample('Not a valid ' + countryName(c) + ' phone number', c);
        }
        return '';
    }

    function checkEmail(value) {
        value = (value || '').trim();
        if (!value) return '';
        return EMAIL_RE.test(value) ? '' : 'Not a valid email address (e.g. name@company.com)';
    }

    // --- display state --------------------------------------------------------

    function hintFor(anchor) {
        var next = anchor.nextElementSibling;
        if (next && next.classList.contains('field-hint')) return next;
        var hint = document.createElement('div');
        hint.className = 'field-hint';
        hint.setAttribute('role', 'alert');
        anchor.insertAdjacentElement('afterend', hint);
        return hint;
    }

    // `field` is the registered record: {input, anchor, check, touched}
    function render(field) {
        var value = field.input.value.trim();
        var error = field.check();
        field.error = error;
        var input = field.input;
        input.classList.toggle('is-valid', !!value && !error);
        // Don't nag mid-typing: only show red once the user has left the field.
        var showError = !!error && field.touched;
        input.classList.toggle('is-invalid', showError);
        input.setAttribute('aria-invalid', showError ? 'true' : 'false');
        var hint = hintFor(field.anchor);
        hint.textContent = showError ? error + '.' : '';
        updateSubmit(input.form);
    }

    var fieldsByForm = new WeakMap();

    function register(form, field) {
        if (!form) return;
        if (!fieldsByForm.has(form)) {
            fieldsByForm.set(form, []);
            form.addEventListener('submit', function (ev) {
                var bad = fieldsByForm.get(form).filter(function (f) { f.touched = true; render(f); return !!f.error; });
                if (bad.length) { ev.preventDefault(); bad[0].input.focus(); }
            });
        }
        fieldsByForm.get(form).push(field);
    }

    function updateSubmit(form) {
        if (!form || !fieldsByForm.has(form)) return;
        var invalid = fieldsByForm.get(form).some(function (f) { return !!f.error; });
        form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach(function (btn) {
            btn.disabled = invalid;
            btn.title = invalid ? 'Fix the highlighted contact details first' : '';
        });
    }

    function wire(field) {
        field.touched = false;
        field.input.addEventListener('input', function () { render(field); });
        field.input.addEventListener('blur', function () {
            if (field.input.value.trim()) field.touched = true;
            render(field);
        });
        field.input._contactField = field;
        register(field.input.form, field);
        render(field);
    }

    // --- phone with country dropdown -----------------------------------------

    function fillCountries(select) {
        if (select.options.length || !lib) return;
        var codes = lib.getCountries().slice().sort(function (a, b) {
            return countryName(a).localeCompare(countryName(b));
        });
        var add = function (code) {
            var opt = document.createElement('option');
            opt.value = code;
            opt.textContent = countryName(code) + ' (+' + lib.getCountryCallingCode(code) + ')';
            select.appendChild(opt);
        };
        add(DEFAULT_COUNTRY);
        var sep = document.createElement('option');
        sep.disabled = true;
        sep.textContent = '──────────';
        select.appendChild(sep);
        codes.forEach(function (code) { if (code !== DEFAULT_COUNTRY) add(code); });
        select.value = select.getAttribute('data-default') || DEFAULT_COUNTRY;
    }

    function setPlaceholder(input, country) {
        var ex = exampleNumber(country);
        input.placeholder = ex ? 'e.g. ' + ex : '';
    }

    function initPhoneField(wrapper) {
        var select = wrapper.querySelector('select');
        var input = wrapper.querySelector('input');
        if (!select || !input) return;
        fillCountries(select);
        setPlaceholder(input, select.value);
        input.maxLength = 50;

        var field = {
            input: input,
            anchor: wrapper,
            check: function () { return checkPhone(input.value, select.value); }
        };

        // A number pasted or typed with its own +code picks its country.
        input.addEventListener('input', function () {
            var v = input.value.trim();
            if (v.charAt(0) !== '+' || !lib) return;
            try {
                var p = lib.parsePhoneNumber(v);
                if (p && p.country && p.country !== select.value) {
                    select.value = p.country;
                    setPlaceholder(input, p.country);
                    render(field);
                }
            } catch (e) { /* not complete yet */ }
        });
        select.addEventListener('change', function () {
            setPlaceholder(input, select.value);
            render(field);
        });
        wrapper._contactField = field;
        wire(field);
    }

    // --- init -------------------------------------------------------------------

    function init(root) {
        root = root || document;
        root.querySelectorAll('[data-phone-field]').forEach(initPhoneField);
        root.querySelectorAll('input[data-email-field]').forEach(function (input) {
            wire({ input: input, anchor: input, check: function () { return checkEmail(input.value); } });
        });
        root.querySelectorAll('input[data-phone-or-email]').forEach(function (input) {
            wire({
                input: input,
                anchor: input,
                check: function () {
                    return input.value.indexOf('@') >= 0 ? checkEmail(input.value) : checkPhone(input.value, DEFAULT_COUNTRY);
                }
            });
        });
    }

    // For edit modals: load a saved number and point the dropdown at its country.
    // Saved numbers are Malaysian-local or +international (see normalise_phone).
    function setPhone(wrapper, value) {
        var select = wrapper.querySelector('select');
        var input = wrapper.querySelector('input');
        value = value || '';
        input.value = value;
        var country = DEFAULT_COUNTRY;
        if (value && lib) {
            try {
                var p = lib.parsePhoneNumber(value, DEFAULT_COUNTRY);
                if (p && p.country) country = p.country;
            } catch (e) { /* leave on the default */ }
        }
        select.value = country;
        setPlaceholder(input, country);
        refresh(wrapper);
    }

    // Re-check a field (or every field in a form) after its value was set from code,
    // clearing any "touched" state so a freshly opened modal starts clean.
    function refresh(el) {
        var fields = el.tagName === 'FORM' ? (fieldsByForm.get(el) || []) : [el._contactField];
        fields.forEach(function (f) { if (f) { f.touched = false; render(f); } });
    }

    window.ContactValidation = {
        init: init,
        setPhone: setPhone,
        refresh: refresh,
        checkPhone: checkPhone,
        checkEmail: checkEmail
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { init(); });
    } else {
        init();
    }
})();
