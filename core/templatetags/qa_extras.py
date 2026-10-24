from django import template

register = template.Library()


@register.filter
def who(user):
    """A user's full name, else username, else "System" (for steps the daily QA sweep took on its own)."""
    if user is None:
        return "System"
    return user.get_full_name() or user.username
