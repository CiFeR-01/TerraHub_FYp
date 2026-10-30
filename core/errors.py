"""Error pages: one standalone template (no base.html, no context processors) so a 500 caused by a DB outage still renders."""
from django.http import HttpResponse
from django.template.loader import render_to_string

PAGES = {
    400: ("Bad request", "The request couldn't be understood. Go back and try again."),
    403: ("Access denied", "You don't have permission to view this page."),
    404: ("Page not found", "That page or record doesn't exist, or it was deleted."),
    500: ("Something went wrong", "An unexpected error occurred. Please try again in a moment."),
}


def _page(status, title=None, message=None):
    default_title, default_message = PAGES[status]
    html = render_to_string('error.html', {'status': status, 'title': title or default_title,
                                           'message': message or default_message})
    return HttpResponse(html, status=status)


def bad_request(request, exception=None):
    return _page(400)


def permission_denied(request, exception=None):
    return _page(403)


def page_not_found(request, exception=None):
    return _page(404)


def server_error(request):
    return _page(500)


def csrf_failure(request, reason=""):
    return _page(403, "Session expired", "This form was out of date, usually because you were signed out. "
                                         "Reload the page or sign in again, then retry.")
