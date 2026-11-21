from urllib.parse import urljoin

from allauth.account.decorators import reauthentication_required
from allauth.account.views import LoginView as AllauthLoginView, SignupView
from allauth.mfa import signals as mfa_signals
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal import flows as totp_flows
from allauth.mfa.totp.views import ActivateTOTPView, DeactivateTOTPView
from axes.utils import reset as reset_axes_attempts
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import (
    LogoutView,
    PasswordChangeView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.core import signing
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .decorators import owner_required
from .emails import consume_email_rate_limit, send_branded_email
from .forms import (
    ActivationSetPasswordForm,
    EligiblePasswordResetForm,
    InternalAccountInvitationForm,
    LockoutClearForm,
    MFAConfigurationForm,
    RecoveryCodeRegenerationForm,
    SecurePasswordChangeForm,
)
from .mfa import has_totp
from .login_security import get_valid_account_lockouts
from .models import AccountActivation, MFAConfiguration, MFARecoveryCode, User
from .session_security import invalidate_user_sessions
from apps.audittrail.events import record_security_event
from apps.audittrail.models import SecurityEvent


ACTIVATION_SESSION_KEY = "pending_account_activation"


class BrandedLoginView(AllauthLoginView):
    template_name = "accounts/login.html"

    def form_valid(self, form):
        user = getattr(form, "user", None)
        if user and getattr(user, "role", None) == User.Role.CUSTOMER:
            from apps.customers.otp import initiate_customer_otp
            remember = form.cleaned_data.get("remember", False)
            next_url = self.request.POST.get("next") or self.request.GET.get("next") or ""
            sent = initiate_customer_otp(self.request, user, next_url=next_url, remember=remember)
            if not sent:
                form.add_error(
                    None,
                    "Unable to send verification code to your Gmail. Please try again in a moment.",
                )
                return self.form_invalid(form)
            return redirect("customers:customer_login_verify_otp")
        response = super().form_valid(form)
        if self.request.user.is_authenticated:
            try:
                from allauth.account.internal.flows.reauthentication import record_authentication
                record_authentication(self.request, self.request.user, method="password", reauthenticated=True)
            except Exception:
                pass
        return response


class BrandedSignupView(SignupView):
    template_name = "accounts/signup.html"
    # Inherit from Allauth SignupView to handle account creation.


    def get_success_url(self):
        """Redirect users based on their role after login.

        Customers are sent to the booking index page, while staff and owners
        continue to the core dashboard.
        """
        user = self.request.user
        if user.is_authenticated:
            # Assuming the User model defines ``is_customer`` property.
            if getattr(user, "is_customer", False):
                from django.urls import reverse
                return reverse("bookings:index")
            # For staff members (including owners) keep the existing behavior.
            return reverse("core:dashboard_router")
        # Fallback to the default success URL.
        return super().get_success_url()


class BrandedLogoutView(LogoutView):
    next_page = reverse_lazy("core:home")



class SecurePasswordChangeView(PasswordChangeView):
    template_name = "accounts/password_change.html"
    form_class = SecurePasswordChangeForm

    def form_valid(self, form):
        user = form.save()
        is_customer = getattr(user, "is_customer", False)
        record_security_event(
            SecurityEvent.Action.PASSWORD_CHANGED,
            request=self.request,
            user=user,
            target=user,
        )
        send_branded_email(
            user.email,
            f"{settings.BRAND_NAME} password changed",
            "accounts/emails/password_changed",
            rate_action="password-changed",
            rate_identifier=user.pk,
            fail_silently=True,
        )
        invalidate_user_sessions(user)
        logout(self.request)
        messages.success(self.request, "Your password was changed. Sign in again on each device.")
        if is_customer:
            return redirect("customers:customer_login")
        return redirect("accounts:login")


class SecureActivateTOTPView(ActivateTOTPView):
    def dispatch(self, request, *args, **kwargs):
        from django.core.exceptions import PermissionDenied

        if request.user.is_authenticated and getattr(request.user, "role", None) == User.Role.CUSTOMER:
            messages.info(
                request,
                "Customer accounts use Gmail verification codes (2FA) and do not need an authenticator app.",
            )
            return redirect("mfa_index")
        if request.user.is_authenticated and request.user.email_verified_at is None:
            messages.error(request, "Verify your email address before enabling MFA.")
            return redirect("mfa_index")
        auth_methods = (
            request.session.get("account_authentication_methods", [])
            if hasattr(request, "session")
            else []
        )
        is_social_session = any(
            isinstance(m, dict) and m.get("method") == "socialaccount"
            for m in auth_methods
        )
        if request.user.is_authenticated and (
            is_social_session or not request.user.has_usable_password()
        ):
            return redirect("core:dashboard_router")
        try:
            return super().dispatch(request, *args, **kwargs)
        except PermissionDenied:
            return redirect("core:dashboard_router")

    def form_valid(self, form):
        totp_flows.activate_totp(self.request, form)
        codes = MFARecoveryCode.regenerate_for_user(self.request.user)
        return render(
            self.request,
            "accounts/mfa_recovery_codes_created.html",
            {"recovery_codes": codes},
        )


class SecureDeactivateTOTPView(DeactivateTOTPView):
    def form_valid(self, form):
        response = super().form_valid(form)
        MFARecoveryCode.objects.filter(user=self.request.user).delete()
        return response


@login_required
@require_http_methods(["GET"])
def recovery_codes(request):
    if not has_totp(request.user):
        return redirect("mfa_activate_totp")
    unused_count = MFARecoveryCode.objects.filter(user=request.user, used_at__isnull=True).count()
    return render(
        request,
        "accounts/mfa_recovery_codes.html",
        {"unused_count": unused_count},
    )


@login_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def regenerate_recovery_codes(request):
    if not has_totp(request.user):
        return redirect("mfa_activate_totp")
    form = RecoveryCodeRegenerationForm(
        request.POST if request.method == "POST" else None
    )
    if request.method == "POST" and form.is_valid():
        codes = MFARecoveryCode.regenerate_for_user(request.user)
        authenticator = Authenticator.objects.get(
            user=request.user,
            type=Authenticator.Type.TOTP,
        )
        mfa_signals.authenticator_reset.send(
            sender=MFARecoveryCode,
            request=request,
            user=request.user,
            authenticator=authenticator,
        )
        send_branded_email(
            request.user.email,
            f"{settings.BRAND_NAME} MFA security changed",
            "accounts/emails/mfa_changed",
            {"action": "New MFA recovery codes were generated."},
            rate_action="mfa-changed",
            rate_identifier=request.user.pk,
            fail_silently=True,
        )
        return render(
            request,
            "accounts/mfa_recovery_codes_created.html",
            {"recovery_codes": codes},
        )
    return render(request, "accounts/mfa_recovery_regenerate.html", {"form": form})


@owner_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def mfa_configuration(request):
    configuration = MFAConfiguration.get_solo()
    form = MFAConfigurationForm(
        request.POST if request.method == "POST" else None,
        instance=configuration,
        actor=request.user,
    )
    if request.method == "POST" and form.is_valid():
        configuration = form.save(commit=False)
        configuration.updated_by = request.user
        configuration.save()
        messages.success(request, "MFA requirements were updated.")
        return redirect("accounts:mfa_configuration")
    return render(request, "accounts/mfa_configuration.html", {"form": form})


@owner_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def login_lockouts(request):
    lockouts = get_valid_account_lockouts()
    for lockout in lockouts:
        lockout["token"] = signing.dumps(
            {"username": lockout["username"], "ip_address": lockout["ip_address"]},
            salt="accounts.clear-login-lockout",
            compress=True,
        )

    if request.method == "POST":
        form = LockoutClearForm(request.POST)
        if form.is_valid():
            try:
                selected = signing.loads(
                    form.cleaned_data["lockout"],
                    salt="accounts.clear-login-lockout",
                    max_age=600,
                )
            except signing.BadSignature:
                selected = None
            matching_lockout = next(
                (
                    lockout
                    for lockout in lockouts
                    if selected
                    and lockout["username"] == selected.get("username")
                    and lockout["ip_address"] == selected.get("ip_address")
                ),
                None,
            )
            if matching_lockout:
                reset_axes_attempts(
                    username=matching_lockout["username"],
                    ip=matching_lockout["ip_address"] or None,
                )
                messages.success(request, "The temporary login lockout was cleared.")
                return redirect("accounts:login_lockouts")
        messages.error(request, "That lockout is no longer valid. Refresh and try again.")

    return render(request, "accounts/login_lockouts.html", {"lockouts": lockouts})


@owner_required
@require_http_methods(["GET", "POST"])
def invite_internal_account(request):
    form = InternalAccountInvitationForm(
        request.POST if request.method == "POST" else None,
        actor=request.user,
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save()
            activation, token = AccountActivation.issue(user)
            activation_path = reverse(
                "accounts:activate",
                kwargs={"public_id": activation.public_id, "token": token},
            )
            activation_url = (
                urljoin(f"{settings.PUBLIC_BASE_URL.rstrip('/')}/", activation_path.lstrip("/"))
                if settings.PUBLIC_BASE_URL
                else request.build_absolute_uri(activation_path)
            )
            send_branded_email(
                user.email,
                f"Activate your {settings.BRAND_NAME} account",
                "accounts/emails/activation",
                {
                    "user": user,
                    "activation_url": activation_url,
                    "expires_at": activation.expires_at,
                },
            )
        messages.success(request, "The account was created and an activation email was sent.")
        return redirect("accounts:invite_internal")
    return render(request, "accounts/invite_internal.html", {"form": form})


def _activation_unavailable(request, activation=None):
    request.session.pop(ACTIVATION_SESSION_KEY, None)
    record_security_event(
        SecurityEvent.Action.ACCOUNT_ACTIVATED,
        request=request,
        target=activation,
        target_type="accounts.AccountActivation",
        result=SecurityEvent.Result.FAILURE,
    )
    return render(request, "accounts/activation_invalid.html", status=400)


@require_http_methods(["GET", "POST"])
def activate_account(request, public_id, token):
    with transaction.atomic():
        activation = (
            AccountActivation.objects.select_for_update()
            .select_related("user")
            .filter(public_id=public_id)
            .first()
        )
        if activation is None or not activation.is_available():
            return _activation_unavailable(request, activation)
        if not activation.matches(token):
            if request.method == "POST":
                activation.failed_attempts += 1
                activation.save(update_fields=("failed_attempts",))
            return _activation_unavailable(request, activation)

    if request.method == "GET":
        return render(request, "accounts/activation_confirm.html")

    request.session.cycle_key()
    request.session[ACTIVATION_SESSION_KEY] = str(activation.public_id)
    return redirect("accounts:activation_password", public_id=activation.public_id)


@require_http_methods(["GET", "POST"])
def activation_password(request, public_id):
    if request.session.get(ACTIVATION_SESSION_KEY) != str(public_id):
        return _activation_unavailable(request)

    if request.method == "POST":
        with transaction.atomic():
            activation = (
                AccountActivation.objects.select_for_update()
                .select_related("user")
                .filter(public_id=public_id)
                .first()
            )
            if activation is None or not activation.is_available():
                return _activation_unavailable(request, activation)
            form = ActivationSetPasswordForm(activation.user, request.POST)
            if form.is_valid():
                user = form.save(commit=False)
                user.email_verified_at = timezone.now()
                user.is_active = True
                user.is_active_staff_member = True
                user.save()
                record_security_event(
                    SecurityEvent.Action.EMAIL_VERIFIED,
                    request=request,
                    user=user,
                    target=user,
                )
                activation.used_at = timezone.now()
                activation.save(update_fields=("used_at",))
                request.session.pop(ACTIVATION_SESSION_KEY, None)
                messages.success(request, "Your account is active. You can now sign in.")
                return redirect("accounts:login")
    else:
        activation = AccountActivation.objects.select_related("user").filter(public_id=public_id).first()
        if activation is None or not activation.is_available():
            return _activation_unavailable(request, activation)
        form = ActivationSetPasswordForm(activation.user)

    return render(request, "accounts/activation_password.html", {"form": form})


class GenericPasswordResetView(PasswordResetView):
    template_name = "accounts/password_reset_form.html"
    form_class = EligiblePasswordResetForm
    email_template_name = "accounts/emails/password_reset.txt"
    html_email_template_name = "accounts/emails/password_reset.html"
    subject_template_name = "accounts/emails/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")
    extra_email_context = {"brand_name": settings.BRAND_NAME}

    def post(self, request, *args, **kwargs):
        candidate_form = self.get_form()
        email = (
            candidate_form.cleaned_data["email"]
            if candidate_form.is_valid()
            else ""
        )
        target_user = User.objects.filter(email=email).first()
        record_security_event(
            SecurityEvent.Action.PASSWORD_RESET_REQUESTED,
            request=request,
            user=target_user,
            target=target_user,
        )
        ip_address = request.META.get("REMOTE_ADDR", "unknown")
        email_allowed = consume_email_rate_limit(
            "password-reset-email",
            email or "blank",
            settings.PASSWORD_RESET_EMAIL_LIMIT,
            settings.PASSWORD_RESET_EMAIL_WINDOW,
        )
        ip_allowed = consume_email_rate_limit(
            "password-reset-ip",
            ip_address,
            settings.PASSWORD_RESET_EMAIL_LIMIT * 3,
            settings.PASSWORD_RESET_EMAIL_WINDOW,
        )
        if not email_allowed or not ip_allowed:
            return redirect("accounts:password_reset_done")
        return super().post(request, *args, **kwargs)


class GenericPasswordResetDoneView(PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class GenericPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    form_class = ActivationSetPasswordForm
    success_url = reverse_lazy("accounts:password_reset_complete")

    def form_valid(self, form):
        user = form.user
        response = super().form_valid(form)
        record_security_event(
            SecurityEvent.Action.PASSWORD_CHANGED,
            request=self.request,
            user=user,
            target=user,
        )
        send_branded_email(
            user.email,
            f"{settings.BRAND_NAME} password changed",
            "accounts/emails/password_changed",
            rate_action="password-changed",
            rate_identifier=user.pk,
            fail_silently=True,
        )
        invalidate_user_sessions(user)
        return response


class GenericPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"


@login_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def logout_all_devices(request):
    if request.method == "POST":
        user = request.user
        is_customer = getattr(user, "is_customer", False)
        invalidate_user_sessions(user)
        logout(request)
        messages.success(request, "You have been signed out on every device.")
        if is_customer:
            return redirect("core:home")
        return redirect("accounts:login")
    return render(request, "accounts/logout_all_devices.html")
