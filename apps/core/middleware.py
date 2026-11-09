from django.conf import settings
from django.http import HttpResponseRedirect


class CanonicalDomainMiddleware:
    """
    Ensures users are always on the canonical production domain (getnailed-woad.vercel.app).
    Redirects temporary Vercel preview deployment URLs so that OAuth, cookies, and CSRF
    match the registered Google OAuth credentials.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().split(":")[0].lower()
        # If running on Vercel or in production, ensure traffic stays on getnailed-woad.vercel.app
        if getattr(settings, "IS_VERCEL", False) or getattr(settings, "IS_PRODUCTION", False):
            if host.endswith(".vercel.app") and host != "getnailed-woad.vercel.app":
                canonical_url = f"https://getnailed-woad.vercel.app{request.get_full_path()}"
                return HttpResponseRedirect(canonical_url)

        return self.get_response(request)
