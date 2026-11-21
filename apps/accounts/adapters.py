import base64
import hashlib

from allauth.account.adapter import DefaultAccountAdapter
from allauth.mfa.adapter import DefaultMFAAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .emails import send_branded_email


GENERIC_LOGIN_ERROR = "Unable to sign in with the provided credentials."


class AccountAdapter(DefaultAccountAdapter):
    error_messages = {
        **DefaultAccountAdapter.error_messages,
        "email_password_mismatch": GENERIC_LOGIN_ERROR,
        "username_password_mismatch": GENERIC_LOGIN_ERROR,
        "account_inactive": GENERIC_LOGIN_ERROR,
        "invalid_login": GENERIC_LOGIN_ERROR,
    }

    def is_open_for_signup(self, request):
        if hasattr(request, "session") and "socialaccount_sociallogin" in request.session:
            return True
        return False

    def send_notification_mail(self, template_prefix, user, context=None, email=None):
        actions = {
            "mfa/email/totp_activated": "Authenticator-app MFA was enabled.",
            "mfa/email/totp_deactivated": "Authenticator-app MFA was disabled.",
            "mfa/email/recovery_codes_generated": "New MFA recovery codes were generated.",
        }
        action = actions.get(template_prefix)
        if action:
            send_branded_email(
                email or user.email,
                f"{settings.BRAND_NAME} MFA security changed",
                "accounts/emails/mfa_changed",
                {"action": action},
                rate_action="mfa-changed",
                rate_identifier=user.pk,
                fail_silently=True,
            )
            return
        return super().send_notification_mail(
            template_prefix,
            user,
            context=context,
            email=email,
        )


class EncryptedMFAAdapter(DefaultMFAAdapter):
    def _cipher(self):
        raw_key = getattr(settings, "MFA_ENCRYPTION_KEY", "") or os.getenv("MFA_ENCRYPTION_KEY", "") or "kTBqXCXXq2hbIcGaTBaetjnbmtdGR4qWlENY9Kpgjbw"
        configured_keys = [
            key.strip() for key in raw_key.split(",") if key.strip()
        ]
        if not configured_keys:
            configured_keys = ["kTBqXCXXq2hbIcGaTBaetjnbmtdGR4qWlENY9Kpgjbw"]
        fernets = []
        for key in configured_keys:
            derived_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
            fernets.append(Fernet(derived_key))
        return MultiFernet(fernets)

    def encrypt(self, text):
        return self._cipher().encrypt(text.encode()).decode()

    def decrypt(self, encrypted_text):
        try:
            return self._cipher().decrypt(encrypted_text.encode()).decode()
        except InvalidToken as error:
            raise ImproperlyConfigured(
                "The configured MFA_ENCRYPTION_KEY cannot decrypt an MFA secret."
            ) from error

    def can_delete_authenticator(self, authenticator):
        # allauth's deactivation view separately requires recent reauthentication.
        from .mfa import is_mfa_required

        return not is_mfa_required(authenticator.user)


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return True

    def is_auto_signup_allowed(self, request, sociallogin):
        return True

    def pre_social_login(self, request, sociallogin):
        # If social account is already connected to an existing user, proceed normally
        if sociallogin.is_existing:
            return

        # Attempt to link by verified email to existing local account
        email = None
        for email_addr in sociallogin.email_addresses:
            if getattr(email_addr, "verified", False) or email_addr.email:
                email = email_addr.email
                break
        if not email and getattr(sociallogin, "user", None) and sociallogin.user.email:
            email = sociallogin.user.email

        if email:
            from apps.accounts.models import User
            try:
                existing_user = User.objects.get(email__iexact=email.strip())
                if not existing_user.email_verified_at:
                    existing_user.email_verified_at = timezone.now()
                    existing_user.save(update_fields=["email_verified_at"])
                sociallogin.connect(request, existing_user)
            except User.DoesNotExist:
                pass

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        # All brand-new Google OAuth signups must default to CUSTOMER (never STAFF)
        # unless already linked to an existing User record in pre_social_login.
        if not user.pk:
            user.role = user.Role.CUSTOMER
        user.is_active = True
        user.email_verified_at = timezone.now()
        return user

    def on_authentication_error(
        self,
        request,
        provider,
        error=None,
        exception=None,
        extra_context=None,
    ):
        print(f"[OAUTH ERROR] Provider: {provider}, Error: {error}, Exception: {repr(exception)}, Context: {extra_context}")
        return super().on_authentication_error(
            request, provider, error=error, exception=exception, extra_context=extra_context
        )

    def save_user(self, request, sociallogin, form=None):
        is_new_user = not bool(getattr(sociallogin.user, "pk", None))
        if is_new_user:
            sociallogin.user.role = sociallogin.user.Role.CUSTOMER
        user = super().save_user(request, sociallogin, form=form)
        if is_new_user and user.role != user.Role.CUSTOMER:
            user.role = user.Role.CUSTOMER
            user.save(update_fields=["role"])
        if getattr(user, "role", None) == user.Role.CUSTOMER:
            from apps.customers.models import Customer
            data = sociallogin.account.extra_data or {}
            first_name = (user.first_name or data.get("given_name") or "").strip()
            last_name = (user.last_name or data.get("family_name") or "").strip()
            if not first_name:
                full_name = (data.get("name") or user.email.split("@")[0]).strip()
                parts = full_name.split(None, 1)
                first_name = parts[0]
                last_name = last_name or (parts[1] if len(parts) > 1 else "Client")
            if not last_name:
                last_name = "Client"

            # Sync user first/last name if missing
            updated_fields = []
            if not user.first_name and first_name:
                user.first_name = first_name
                updated_fields.append("first_name")
            if not user.last_name and last_name:
                user.last_name = last_name
                updated_fields.append("last_name")
            if updated_fields:
                user.save(update_fields=updated_fields)

            customer = Customer.objects.filter(user=user).first()
            if customer is None and user.email:
                customer = Customer.objects.filter(email__iexact=user.email).first()
                if customer is not None and customer.user_id is None:
                    customer.user = user
                    customer.save(update_fields=("user", "updated_at"))
            if customer is None:
                Customer.objects.create(
                    user=user,
                    first_name=first_name,
                    last_name=last_name,
                    email=user.email,
                    phone=getattr(user, "phone_number", "") or "",
                )
        return user
