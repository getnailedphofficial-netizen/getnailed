import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect

from .mfa import has_totp, is_mfa_required
from .models import User
from .session_security import invalidate_user_sessions


class SessionSecurityMiddleware:
    LAST_ACTIVITY_KEY = "accounts.last_activity"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Prevent Back-button GET requests to /accounts/google/login/ from auto-logging in or showing raw allauth screen
        if request.method == "GET" and request.path.rstrip("/") == "/accounts/google/login":
            if request.user.is_authenticated:
                return redirect("core:dashboard_router")
            return redirect("accounts:login")

        if request.user.is_authenticated:
            timeout = self._timeout_for(request.user)
            if timeout:
                now = int(time.time())
                last_activity = request.session.get(self.LAST_ACTIVITY_KEY)
                if last_activity and now - last_activity >= timeout:
                    logout(request)
                    messages.warning(request, "Your session expired due to inactivity.")
                    return redirect(settings.LOGIN_URL)
                request.session[self.LAST_ACTIVITY_KEY] = now
                request.session.set_expiry(timeout)
            # Keep socialaccount OAuth authentication timestamp fresh while session is active
            auth_methods = request.session.get("account_authentication_methods", [])
            if auth_methods and not request.user.has_usable_password():
                now_ts = time.time()
                for m in auth_methods:
                    if isinstance(m, dict) and m.get("method") == "socialaccount":
                        m["at"] = now_ts
                request.session["account_authentication_methods"] = auth_methods

        response = self.get_response(request)
        if getattr(request, "_flush_sessions_after_security_change", False):
            user = request.user
            if user.is_authenticated:
                invalidate_user_sessions(user)
                logout(request)

        # Prevent browser back/forward cache (bfcache) from retaining any dynamic or protected pages
        if not request.path.startswith(("/static/", "/media/")):
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"

        return response

    @staticmethod
    def _timeout_for(user):
        if user.role == User.Role.OWNER or user.is_superuser:
            return settings.OWNER_INACTIVITY_TIMEOUT
        if user.role in {User.Role.CASHIER, User.Role.STAFF}:
            return settings.STAFF_INACTIVITY_TIMEOUT
        return None


class RequiredMFAMiddleware:
    EXEMPT_PREFIXES = (
        "/security/2fa/",
        "/security/login/",
        "/security/logout/",
        "/security/reauthenticate/",
        "/accounts/logout/",
        "/static/",
        "/media/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        auth_methods = (
            request.session.get("account_authentication_methods", [])
            if hasattr(request, "session")
            else []
        )
        is_social_session = any(
            isinstance(m, dict) and m.get("method") == "socialaccount"
            for m in auth_methods
        )
        if (
            request.user.is_authenticated
            and not is_social_session
            and is_mfa_required(request.user)
            and not has_totp(request.user)
            and not request.path.startswith(self.EXEMPT_PREFIXES)
        ):
            return redirect("mfa_activate_totp")
        return self.get_response(request)
