from allauth.mfa.models import Authenticator
from django.conf import settings

from .models import MFAConfiguration, User


def has_totp(user):
    return Authenticator.objects.filter(
        user=user,
        type=Authenticator.Type.TOTP,
    ).exists()


def is_mfa_required(user):
    if not user.is_authenticated:
        return False
    if not user.has_usable_password():
        return False
    if (user.role == User.Role.OWNER or user.is_superuser) and settings.MFA_ENFORCE_OWNER:
        return True
    if user.role not in {User.Role.CASHIER, User.Role.STAFF}:
        return False
    if settings.MFA_REQUIRE_INTERNAL_USERS:
        return True
    return bool(
        MFAConfiguration.objects.filter(pk=1).values_list(
            "require_internal_user_mfa", flat=True
        ).first()
    )
