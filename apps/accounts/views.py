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

    def get_form_class(self):
        if self.request.user.is_authenticated and not self.request.user.has_usable_password():
            return ActivationSetPasswordForm
        return SecurePasswordChangeForm

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["is_setting_initial_password"] = (
            self.request.user.is_authenticated and not self.request.user.has_usable_password()
        )
        return ctx

    def form_valid(self, form):
        was_initial = not self.request.user.has_usable_password()
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
        if was_initial:
            messages.success(
                self.request,
                "Your backup salon password was set. You can now sign in with either Google or your email and password.",
            )
        else:
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
    from django.db.models import Q
    from apps.customers.models import Customer
    from apps.services.models import StaffProfile

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

    search_q = (request.GET.get("q") or "").strip()
    role_filter = (request.GET.get("role") or "").strip().upper()
    accounts_qs = (
        User.objects.select_related("staff_profile")
        .prefetch_related("socialaccount_set")
        .order_by("role", "first_name", "last_name", "email")
    )
    if search_q:
        accounts_qs = accounts_qs.filter(
            Q(email__icontains=search_q)
            | Q(first_name__icontains=search_q)
            | Q(last_name__icontains=search_q)
            | Q(phone_number__icontains=search_q)
        )
    if role_filter in dict(User.Role.choices):
        accounts_qs = accounts_qs.filter(role=role_filter)

    return render(
        request,
        "accounts/invite_internal.html",
        {
            "form": form,
            "accounts": accounts_qs,
            "role_choices": User.Role.choices,
            "search_q": search_q,
            "role_filter": role_filter,
        },
    )


@owner_required
@require_http_methods(["POST"])
def update_account_access(request, user_id):
    from django.shortcuts import get_object_or_404
    from apps.customers.models import Customer
    from apps.services.models import StaffProfile

    target_user = get_object_or_404(User, pk=user_id)
    new_role = (request.POST.get("role") or "").strip().upper()
    valid_roles = dict(User.Role.choices)
    if new_role not in valid_roles:
        messages.error(request, "Invalid role selected.")
        return redirect("accounts:invite_internal")

    if target_user.pk == request.user.pk and new_role != User.Role.OWNER:
        messages.error(request, "You cannot remove Owner access from your own active account.")
        return redirect("accounts:invite_internal")

    is_active = request.POST.get("is_active") == "on"
    if target_user.pk == request.user.pk and not is_active:
        messages.error(request, "You cannot deactivate your own Owner account.")
        return redirect("accounts:invite_internal")

    can_use_pos = request.POST.get("can_use_pos") == "on" or new_role in (User.Role.OWNER, User.Role.CASHIER)
    can_manage_bookings = request.POST.get("can_manage_bookings") == "on" or new_role in (User.Role.OWNER, User.Role.CASHIER)
    can_manage_customers = request.POST.get("can_manage_customers") == "on" or new_role in (User.Role.OWNER, User.Role.CASHIER)
    can_assign_services = request.POST.get("can_assign_services") == "on" or new_role in (User.Role.OWNER, User.Role.CASHIER)
    specialty = (request.POST.get("specialty") or "").strip()

    old_role = target_user.role
    with transaction.atomic():
        if new_role != User.Role.STAFF:
            # Remove StaffProfile first before saving target_user so pre_save signal allows the role change
            StaffProfile.objects.filter(user=target_user).delete()

        target_user.role = new_role
        target_user.is_active = is_active
        target_user.is_active_staff_member = is_active and new_role in (
            User.Role.OWNER,
            User.Role.CASHIER,
            User.Role.STAFF,
        )
        target_user.can_use_pos = can_use_pos
        target_user.can_manage_bookings = can_manage_bookings
        target_user.can_manage_customers = can_manage_customers
        target_user.can_assign_services = can_assign_services
        if is_active and not target_user.email_verified_at:
            target_user.email_verified_at = timezone.now()
        target_user.save()

        if new_role == User.Role.STAFF:
            sp, _ = StaffProfile.objects.get_or_create(
                user=target_user,
                defaults={
                    "specialty": specialty or "Nail & Spa Specialist",
                    "is_active": True,
                },
            )
            if specialty and sp.specialty != specialty:
                sp.specialty = specialty
                sp.save(update_fields=["specialty"])
        elif new_role == User.Role.CUSTOMER:
            cust = Customer.objects.filter(user=target_user).first()
            if cust is None and target_user.email:
                cust = Customer.objects.filter(email__iexact=target_user.email).first()
                if cust and cust.user_id is None:
                    cust.user = target_user
                    cust.save(update_fields=["user", "updated_at"])
            if cust is None:
                Customer.objects.create(
                    user=target_user,
                    first_name=target_user.first_name or "Valued",
                    last_name=target_user.last_name or "Client",
                    email=target_user.email,
                    phone=target_user.phone_number or "",
                )

        if old_role != new_role:
            record_security_event(
                SecurityEvent.Action.ROLE_CHANGED,
                request=request,
                user=request.user,
                target=target_user,
            )

    messages.success(
        request,
        f"Updated access for {target_user.get_full_name() or target_user.email} → {target_user.get_role_display()}.",
    )
    return redirect("accounts:invite_internal")


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
