from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.conf import settings
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.adapters import EncryptedMFAAdapter
from apps.accounts.models import MFAConfiguration, MFARecoveryCode, User
from apps.audittrail.models import SecurityEvent


PASSWORD = "Correct-Horse-Battery-47!"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    MFA_ENCRYPTION_KEY="test-only-mfa-encryption-key",
    MFA_ENFORCE_OWNER=True,
    MFA_REQUIRE_INTERNAL_USERS=False,
)
class MFASecurityTests(TestCase):
    def setUp(self):
        cache.clear()

    def create_user(self, email="staff@example.com", role=User.Role.STAFF, **extra):
        values = {
            "role": role,
            "email_verified_at": timezone.now(),
            "is_active": True,
            "is_active_staff_member": True,
        }
        values.update(extra)
        return User.objects.create_user(email, PASSWORD, **values)

    def enable_totp(self, user, secret=TOTP_SECRET):
        return TOTP.activate(user, secret).instance

    def current_totp_code(self, secret=TOTP_SECRET):
        counter = next(yield_hotp_counters_from_time())
        return format_hotp_value(hotp_value(secret, counter))

    def begin_login(self, client, user):
        return client.post(
            reverse("accounts:login"),
            {"login": user.email, "password": PASSWORD},
        )

    def test_totp_secret_is_encrypted_at_rest(self):
        user = self.create_user()
        authenticator = self.enable_totp(user)
        stored_secret = authenticator.data["secret"]
        self.assertNotEqual(stored_secret, TOTP_SECRET)
        self.assertNotIn(TOTP_SECRET, stored_secret)
        self.assertEqual(EncryptedMFAAdapter().decrypt(stored_secret), TOTP_SECRET)

    def test_totp_is_required_during_login_when_enabled(self):
        user = self.create_user()
        self.enable_totp(user)
        response = self.begin_login(self.client, user)
        self.assertRedirects(response, reverse("mfa_authenticate"), fetch_redirect_response=False)

        response = self.client.post(
            reverse("mfa_authenticate"),
            {"code": self.current_totp_code()},
        )
        self.assertRedirects(
            response,
            reverse("core:dashboard_router"),
            fetch_redirect_response=False,
        )

    def test_owner_login_requires_password_and_valid_mfa(self):
        owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.enable_totp(owner)

        response = self.begin_login(self.client, owner)
        self.assertRedirects(response, reverse("mfa_authenticate"), fetch_redirect_response=False)
        response = self.client.post(
            reverse("mfa_authenticate"),
            {"code": self.current_totp_code()},
        )
        self.assertRedirects(
            response,
            reverse("core:dashboard_router"),
            fetch_redirect_response=False,
        )
        self.assertEqual(self.client.get(reverse("core:owner_dashboard")).status_code, 200)

    def test_totp_code_from_5_minutes_ago_is_accepted_due_to_tolerance(self):
        import time
        from allauth.mfa import app_settings
        owner = self.create_user("owner5min@example.com", User.Role.OWNER)
        self.enable_totp(owner)

        current_counter = int(time.time()) // app_settings.TOTP_PERIOD
        # Code from 10 steps (5 minutes) ago is accepted
        code_5_min_old = format_hotp_value(hotp_value(TOTP_SECRET, current_counter - 10))

        response = self.begin_login(self.client, owner)
        self.assertRedirects(response, reverse("mfa_authenticate"), fetch_redirect_response=False)
        response = self.client.post(
            reverse("mfa_authenticate"),
            {"code": code_5_min_old},
        )
        self.assertRedirects(
            response,
            reverse("core:dashboard_router"),
            fetch_redirect_response=False,
        )

        # Code from 11 steps (5.5 minutes) ago is beyond tolerance and rejected
        code_too_old = format_hotp_value(hotp_value(TOTP_SECRET, current_counter - 11))
        second_client = Client()
        self.begin_login(second_client, owner)
        response = second_client.post(
            reverse("mfa_authenticate"),
            {"code": code_too_old},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incorrect code")

    def test_cashier_login_requires_password_and_valid_mfa_when_policy_enabled(self):
        MFAConfiguration.objects.create(pk=1, require_internal_user_mfa=True)
        cashier = self.create_user("cashier@example.com", User.Role.CASHIER)
        self.enable_totp(cashier)

        response = self.begin_login(self.client, cashier)
        self.assertRedirects(response, reverse("mfa_authenticate"), fetch_redirect_response=False)
        response = self.client.post(
            reverse("mfa_authenticate"),
            {"code": self.current_totp_code()},
        )
        self.assertRedirects(
            response,
            reverse("core:dashboard_router"),
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            self.client.get(reverse("core:dashboard_router")),
            reverse("core:staff_dashboard"),
            fetch_redirect_response=False,
        )

    def test_invalid_totp_is_rejected(self):
        user = self.create_user()
        self.enable_totp(user)
        self.begin_login(self.client, user)

        response = self.client.post(reverse("mfa_authenticate"), {"code": "123"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incorrect code")
        self.assertNotIn("_auth_user_id", self.client.session)
        event = SecurityEvent.objects.get(action=SecurityEvent.Action.MFA_FAILED)
        self.assertEqual(event.user, user)
        self.assertEqual(event.result, SecurityEvent.Result.FAILURE)

    @override_settings(MFA_FAILURE_LIMIT=3, MFA_FAILURE_TIMEOUT=900)
    def test_mfa_attempt_limit_temporarily_rejects_further_codes(self):
        user = self.create_user()
        self.enable_totp(user)
        self.begin_login(self.client, user)

        for attempt in range(settings.MFA_FAILURE_LIMIT):
            response = self.client.post(
                reverse("mfa_authenticate"),
                {"code": f"invalid-{attempt}"},
            )
        self.assertContains(response, "Too many incorrect codes")

        blocked = self.client.post(
            reverse("mfa_authenticate"),
            {"code": self.current_totp_code()},
        )
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, "Too many incorrect codes")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(
            SecurityEvent.objects.filter(
                action=SecurityEvent.Action.MFA_LOCKED,
                user=user,
                result=SecurityEvent.Result.FAILURE,
            ).exists()
        )

    def test_owner_without_totp_is_forced_to_enroll(self):
        owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.client.force_login(owner)
        response = self.client.get(reverse("core:dashboard_router"))
        self.assertRedirects(response, reverse("mfa_activate_totp"), fetch_redirect_response=False)

    def test_owner_can_reauthenticate_during_mandatory_enrollment(self):
        owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.client.force_login(owner)
        response = self.client.get(reverse("mfa_activate_totp"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_reauthenticate"), response["Location"])
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_staff_is_optional_until_owner_enables_policy(self):
        staff = self.create_user()
        self.client.force_login(staff)
        response = self.client.get(reverse("core:dashboard_router"))
        self.assertRedirects(
            response,
            reverse("core:staff_dashboard"),
            fetch_redirect_response=False,
        )

        MFAConfiguration.objects.create(pk=1, require_internal_user_mfa=True)
        response = self.client.get(reverse("core:dashboard_router"))
        self.assertRedirects(response, reverse("mfa_activate_totp"), fetch_redirect_response=False)

    def test_owner_can_require_mfa_for_all_internal_users(self):
        owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.enable_totp(owner)
        response = self.begin_login(self.client, owner)
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            reverse("mfa_authenticate"),
            {"code": self.current_totp_code()},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            reverse("accounts:mfa_configuration"),
            {"require_internal_user_mfa": "on"},
        )
        self.assertRedirects(response, reverse("accounts:mfa_configuration"))
        self.assertTrue(MFAConfiguration.get_solo().require_internal_user_mfa)

    def test_unverified_email_cannot_activate_mfa(self):
        user = self.create_user(email_verified_at=None)
        self.client.force_login(user)
        response = self.client.get(reverse("mfa_activate_totp"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response["Location"])
        self.assertFalse(Authenticator.objects.filter(user=user).exists())

    def test_qr_enrollment_creates_hashed_recovery_codes_shown_once(self):
        user = self.create_user()
        response = self.begin_login(self.client, user)
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("mfa_activate_totp"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Authenticator enrollment QR code")
        secret = self.client.session["mfa.totp.secret"]

        response = self.client.post(
            reverse("mfa_activate_totp"),
            {"code": self.current_totp_code(secret)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Save your recovery codes now")
        self.assertEqual(MFARecoveryCode.objects.filter(user=user).count(), 10)
        for recovery_code in MFARecoveryCode.objects.filter(user=user):
            self.assertEqual(len(recovery_code.code_digest), 64)

        status_page = self.client.get(reverse("mfa_recovery_codes"))
        self.assertNotContains(status_page, "SHOWN ONCE")

    def test_recovery_codes_are_hashed_single_use_and_regeneration_invalidates_old_codes(self):
        user = self.create_user()
        old_codes = MFARecoveryCode.regenerate_for_user(user)
        stored_values = list(
            MFARecoveryCode.objects.filter(user=user).values_list("code_digest", flat=True)
        )
        for code in old_codes:
            self.assertNotIn(code, stored_values)
        self.assertTrue(MFARecoveryCode.consume(user, old_codes[0]))
        self.assertFalse(MFARecoveryCode.consume(user, old_codes[0]))

        MFARecoveryCode.regenerate_for_user(user)
        self.assertFalse(MFARecoveryCode.consume(user, old_codes[1]))

    def test_recovery_code_can_complete_mfa_login_only_once(self):
        user = self.create_user()
        self.enable_totp(user)
        recovery_code = MFARecoveryCode.regenerate_for_user(user)[0]
        response = self.begin_login(self.client, user)
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("mfa_authenticate"), {"code": recovery_code})
        self.assertEqual(response.status_code, 302)

        second_client = Client()
        response = self.begin_login(second_client, user)
        self.assertEqual(response.status_code, 302)
        response = second_client.post(reverse("mfa_authenticate"), {"code": recovery_code})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incorrect code")

    def test_recovery_regeneration_requires_reauthentication(self):
        user = self.create_user()
        self.enable_totp(user)
        self.client.force_login(user)
        response = self.client.get(reverse("mfa_regenerate_recovery_codes"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("reauthenticate", response["Location"])

    def test_owner_cannot_disable_mfa_without_reauthentication(self):
        owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.enable_totp(owner)
        self.client.force_login(owner)
        response = self.client.get(reverse("mfa_deactivate_totp"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("reauthenticate", response["Location"])
        self.assertTrue(
            Authenticator.objects.filter(user=owner, type=Authenticator.Type.TOTP).exists()
        )

    def test_required_users_cannot_delete_their_authenticator(self):
        owner = self.create_user("owner@example.com", User.Role.OWNER)
        owner_authenticator = self.enable_totp(owner)
        staff = self.create_user()
        staff_authenticator = self.enable_totp(staff, secret=TOTP_SECRET[::-1])

        adapter = EncryptedMFAAdapter()
        self.assertFalse(adapter.can_delete_authenticator(owner_authenticator))
        self.assertTrue(adapter.can_delete_authenticator(staff_authenticator))

    def test_owner_can_complete_reauthentication_then_change_sensitive_policy(self):
        owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.enable_totp(owner)
        self.client.force_login(owner)

        protected = self.client.get(reverse("accounts:mfa_configuration"))
        self.assertEqual(protected.status_code, 302)
        self.assertIn("reauthenticate", protected["Location"])
        reauthenticated = self.client.post(
            protected["Location"],
            {"password": PASSWORD},
        )
        self.assertEqual(reauthenticated.status_code, 302)
        self.assertIn(reverse("accounts:mfa_configuration"), reauthenticated["Location"])

        changed = self.client.post(
            reverse("accounts:mfa_configuration"),
            {"require_internal_user_mfa": "on"},
        )
        self.assertRedirects(changed, reverse("accounts:mfa_configuration"))
        self.assertTrue(MFAConfiguration.get_solo().require_internal_user_mfa)

    def test_only_totp_is_configured_without_sms_or_trusted_browser_bypass(self):
        self.assertEqual(settings.MFA_SUPPORTED_TYPES, ["totp"])
        self.assertFalse(settings.MFA_TRUST_ENABLED)
