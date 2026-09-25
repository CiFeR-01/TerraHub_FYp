"""Reusable field validators. Each raises django ValidationError, so they can be
attached to a model field (validators=[...]) or called from a view.

static/js/contact-validation.js applies the same rules in the browser for live
feedback (via libphonenumber-js, the JS port of the same Google rules). The
server check here is the one that counts.
"""
import phonenumbers
from phonenumbers import geocoder
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

DEFAULT_PHONE_REGION = 'MY'


def _example(region):
    example = phonenumbers.example_number(region)
    if not example:
        return ''
    fmt = phonenumbers.PhoneNumberFormat.NATIONAL if region == DEFAULT_PHONE_REGION else phonenumbers.PhoneNumberFormat.INTERNATIONAL
    return f" (e.g. {phonenumbers.format_number(example, fmt)})"


def normalise_phone(raw, region=DEFAULT_PHONE_REGION):
    """Check a phone number against its country's rules and return it tidied up.

    `region` is the country picked on the form (ISO code, e.g. 'SG'); a number
    typed with a leading + uses its own country code instead. Malaysian numbers
    come back in local form (012-345 6789), others in international form
    (+65 9123 4567). Blank in, blank out.
    """
    raw = (raw or '').strip()
    if not raw:
        return ''
    region = (region or DEFAULT_PHONE_REGION).upper()
    if region not in phonenumbers.SUPPORTED_REGIONS:
        region = DEFAULT_PHONE_REGION

    try:
        number = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        raise ValidationError(f"'{raw}' is not a phone number{_example(region)}.")

    if not phonenumbers.is_valid_number(number):
        region = phonenumbers.region_code_for_number(number) or region
        country = geocoder.country_name_for_number(number, 'en') or region
        raise ValidationError(f"'{raw}' is not a valid {country} phone number{_example(region)}.")

    fmt = (phonenumbers.PhoneNumberFormat.NATIONAL
           if phonenumbers.region_code_for_number(number) == DEFAULT_PHONE_REGION
           else phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    return phonenumbers.format_number(number, fmt)


def validate_phone(value):
    """Model-field validator. Stored numbers are Malaysian-local or +international,
    so the default region is right for anything already saved."""
    normalise_phone(value)


def validate_email_address(value):
    value = (value or '').strip()
    if not value:
        return
    try:
        validate_email(value)
    except ValidationError:
        raise ValidationError(f"'{value}' is not a valid email address (e.g. name@company.com).")


def validate_phone_or_email(value):
    """For contact fields that take either, e.g. a shipment's delivery contact."""
    value = (value or '').strip()
    if '@' in value:
        validate_email_address(value)
    else:
        validate_phone(value)


def normalise_phone_or_email(value):
    value = (value or '').strip()
    if '@' in value:
        validate_email_address(value)
        return value
    return normalise_phone(value)


def validation_messages(exc):
    """Flatten a ValidationError into one line for messages.error()."""
    if hasattr(exc, 'message_dict'):
        return ' '.join(
            msg if field == '__all__' else f"{field.replace('_', ' ').capitalize()}: {msg}"
            for field, msgs in exc.message_dict.items() for msg in msgs
        )
    return ' '.join(exc.messages)
