from django import template

from core.utils import format_mt, format_stock_display

register = template.Library()


@register.filter
def weight_display(qty, product):
    """Human-friendly weight for a Product-linked unit-count quantity.
    Delegates to core.utils.format_stock_display (also used on product_detail.html)
    so the conversion logic - including the 'pcs' special case - stays in one place.
    """
    if product is None or qty is None:
        return ""
    try:
        return format_stock_display(qty, product)
    except (TypeError, ValueError):
        return ""


@register.filter
def mt_display(total_mt):
    """Human-friendly display for an MT amount that's already computed
    (no product/material to look weight_mt_per_unit up from) - e.g. the
    rent-reduction DSS, which works in MT throughout. Auto-switches to kg
    for small amounts via core.utils.format_mt, same threshold as weight_display.
    """
    if total_mt is None:
        return ""
    try:
        return format_mt(total_mt)
    except (TypeError, ValueError):
        return ""
