from datetime import datetime, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import AccountActivation, User


TEST_PASSWORD = "Correct-Horse-Battery-47!"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    MFA_ENCRYPTION_KEY="test-only-mfa-encryption-key",
    MFA_ENFORCE_OWNER=False,
    LOGIN_CAPTCHA_THRESHOLD=100,
)
class AuthenticationSecurityTests(TestCase):
    def create_eligible_user(self, email="staff@example.com", role=User.Role.STAFF, **extra):
        values = {
            "role": role,
            "email_verified_at": timezone.now(),
            "is_active": True,
            "is_active_staff_member": True,
        }
        values.update(extra)
        return User.objects.create_user(email=email, password=TEST_PASSWORD, **values)

    def password_reset_url(self, user):
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        return reverse(
            "accounts:password_reset_confirm",
            kwargs={"uidb64": uid, "token": token},
        )

    def login_client(self, client, user, password=TEST_PASSWORD):
        response = client.post(
            reverse("accounts:login"),
            {"login": user.email, "password": password},
        )
        return response.status_code == 302

    def test_email_is_normalized_and_unique_case_insensitively(self):
        user = self.create_eligible_user("  Person@Example.COM ")
        self.assertEqual(user.email, "person@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_eligible_user("PERSON@example.com")

    def test_login_uses_email_and_django_password_hashing(self):
        user = self.create_eligible_user()
        self.assertNotEqual(user.password, TEST_PASSWORD)
        self.assertTrue(user.password.startswith("argon2$"))
        response = self.client.post(
            reverse("accounts:login"),
            {"login": "STAFF@EXAMPLE.COM", "password": TEST_PASSWORD},
        )
        self.assertRedirects(
            response,
            reverse("core:dashboard_router"),
            fetch_redirect_response=False,
        )

    def test_existing_pbkdf2_password_is_supported_and_upgraded(self):
        user = self.create_eligible_user()
        hasher = PBKDF2PasswordHasher()
        user.password = hasher.encode(TEST_PASSWORD, hasher.salt())
        user.save(update_fields=("password",))

        self.assertTrue(self.login_client(self.client, user))
        user.refresh_from_db()
        self.assertTrue(user.password.startswith("argon2$"))

    def test_superuser_is_bootstrapped_as_a_verified_owner(self):
        owner = User.objects.create_superuser("root@example.com", TEST_PASSWORD)
        self.assertEqual(owner.role, User.Role.OWNER)
        self.assertIsNotNone(owner.email_verified_at)
        self.assertTrue(owner.is_owner)

    def test_ineligible_accounts_receive_the_same_login_error(self):
        states = (
            {"email_verified_at": None},
            {"is_active": False},
            {"is_active_staff_member": False},
            {"is_locked": True},
        )
        for index, state in enumerate(states):
            user = self.create_eligible_user(f"blocked{index}@example.com", **state)
            response = self.client.post(
                reverse("accounts:login"),
                {"login": user.email, "password": TEST_PASSWORD},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Unable to sign in with the provided credentials.")

    def test_internal_login_requires_a_verified_email(self):
        user = self.create_eligible_user("unverified@example.com", email_verified_at=None)
        response = self.client.post(
            reverse("accounts:login"),
            {"login": user.email, "password": TEST_PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unable to sign in with the provided credentials.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_no_public_registration_route_exists(self):
        self.assertEqual(self.client.get("/accounts/register/").status_code, 404)

    def test_only_owner_can_open_internal_invitation(self):
        staff = self.create_eligible_user()
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("accounts:invite_internal")).status_code, 403)

        owner = self.create_eligible_user("owner@example.com", User.Role.OWNER)
        self.client.force_login(owner)
        self.assertEqual(self.client.get(reverse("accounts:invite_internal")).status_code, 200)

    def test_invitation_only_creates_cashier_or_staff_and_sends_activation(self):
        owner = self.create_eligible_user("owner@example.com", User.Role.OWNER)
        self.client.force_login(owner)
        response = self.client.post(
            reverse("accounts:invite_internal"),
            {
                "first_name": "Casey",
                "last_name": "Cashier",
                "email": "NEW.CASHIER@Example.com",
                "phone_number": "09171234567",
                "role": User.Role.CASHIER,
            },
        )
        self.assertRedirects(response, reverse("accounts:invite_internal"))
        invited = User.objects.get(email="new.cashier@example.com")
        self.assertFalse(invited.is_active)
        self.assertFalse(invited.is_active_staff_member)
        self.assertIsNone(invited.email_verified_at)
        self.assertFalse(invited.has_usable_password())
        self.assertTrue(invited.can_use_pos)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(str(invited.activations.get().public_id), mail.outbox[0].body)
        self.assertNotIn(invited.activations.get().token_digest, mail.outbox[0].body)
        self.assertNotIn(TEST_PASSWORD, mail.outbox[0].body)

        tampered = self.client.post(
            reverse("accounts:invite_internal"),
            {"email": "another@example.com", "role": User.Role.OWNER},
        )
        self.assertEqual(tampered.status_code, 200)
        self.assertFalse(User.objects.filter(email="another@example.com").exists())

    def test_activation_sets_password_verifies_email_and_is_single_use(self):
        user = User.objects.create_user(
            "invitee@example.com",
            password=None,
            role=User.Role.STAFF,
            is_active=False,
            is_active_staff_member=False,
        )
        activation, token = AccountActivation.issue(user)
        url = reverse("accounts:activate", kwargs={"public_id": activation.public_id, "token": token})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertRedirects(
            self.client.post(url),
            reverse("accounts:activation_password", kwargs={"public_id": activation.public_id}),
        )
        response = self.client.post(
            reverse("accounts:activation_password", kwargs={"public_id": activation.public_id}),
            {"new_password1": TEST_PASSWORD, "new_password2": TEST_PASSWORD},
        )
        self.assertRedirects(response, reverse("accounts:login"))
        user.refresh_from_db()
        activation.refresh_from_db()
        self.assertTrue(user.check_password(TEST_PASSWORD))
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_active_staff_member)
        self.assertIsNotNone(user.email_verified_at)
        self.assertIsNotNone(activation.used_at)
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_activation_token_is_not_stored_and_failed_attempts_are_capped(self):
        user = User.objects.create_user("invitee@example.com", password=None)
        activation, token = AccountActivation.issue(user)
        self.assertNotEqual(activation.token_digest, token)
        self.assertNotIn(token, activation.token_digest)
        for _ in range(settings.ACCOUNT_ACTIVATION_MAX_ATTEMPTS):
            response = self.client.post(
                reverse(
                    "accounts:activate",
                    kwargs={"public_id": activation.public_id, "token": "wrong-token"},
                )
            )
            self.assertEqual(response.status_code, 400)
        activation.refresh_from_db()
        self.assertEqual(activation.failed_attempts, settings.ACCOUNT_ACTIVATION_MAX_ATTEMPTS)
        valid_url = reverse(
            "accounts:activate", kwargs={"public_id": activation.public_id, "token": token}
        )
        self.assertEqual(self.client.get(valid_url).status_code, 400)

    def test_expired_activation_is_rejected(self):
        user = User.objects.create_user("invitee@example.com", password=None)
        activation, token = AccountActivation.issue(user)
        activation.expires_at = timezone.now() - timedelta(seconds=1)
        activation.save(update_fields=("expires_at",))
        url = reverse("accounts:activate", kwargs={"public_id": activation.public_id, "token": token})
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_password_reset_does_not_enumerate_accounts(self):
        self.create_eligible_user()
        existing = self.client.post(reverse("accounts:password_reset"), {"email": "staff@example.com"})
        existing_location = existing["Location"]
        self.assertEqual(len(mail.outbox), 1)
        mail.outbox.clear()
        missing = self.client.post(reverse("accounts:password_reset"), {"email": "missing@example.com"})
        self.assertEqual(existing.status_code, missing.status_code)
        self.assertEqual(existing_location, missing["Location"])
        self.assertEqual(len(mail.outbox), 0)

    def test_password_reset_is_not_sent_to_ineligible_account(self):
        self.create_eligible_user(is_locked=True)
        response = self.client.post(reverse("accounts:password_reset"), {"email": "staff@example.com"})
        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_password_change_rejects_current_and_short_passwords(self):
        user = self.create_eligible_user()
        self.client.force_login(user)
        same_password = self.client.post(
            reverse("accounts:password_change"),
            {
                "old_password": TEST_PASSWORD,
                "new_password1": TEST_PASSWORD,
                "new_password2": TEST_PASSWORD,
            },
        )
        self.assertEqual(same_password.status_code, 200)
        self.assertContains(same_password, "must be different from your current password")

        short_password = self.client.post(
            reverse("accounts:password_change"),
            {
                "old_password": TEST_PASSWORD,
                "new_password1": "Short-7!",
                "new_password2": "Short-7!",
            },
        )
        self.assertEqual(short_password.status_code, 200)
        self.assertContains(short_password, "at least 12 characters")

    def test_password_change_invalidates_all_sessions(self):
        user = self.create_eligible_user()
        other_client = Client()
        self.assertTrue(self.login_client(self.client, user))
        self.assertTrue(self.login_client(other_client, user))
        new_password = "A-New-Secure-Password-58!"

        response = self.client.post(
            reverse("accounts:password_change"),
            {
                "old_password": TEST_PASSWORD,
                "new_password1": new_password,
                "new_password2": new_password,
            },
        )
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(other_client.get(reverse("core:dashboard_router")).status_code, 302)
        self.assertTrue(self.login_client(self.client, user, new_password))

    def test_password_reset_rejects_current_password(self):
        user = self.create_eligible_user()
        response = self.client.get(self.password_reset_url(user))
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            response["Location"],
            {"new_password1": TEST_PASSWORD, "new_password2": TEST_PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be different from your current password")

    def test_password_reset_is_single_use_and_invalidates_sessions(self):
        user = self.create_eligible_user()
        first_session = Client()
        second_session = Client()
        self.assertTrue(self.login_client(first_session, user))
        self.assertTrue(self.login_client(second_session, user))
        user.refresh_from_db()
        original_reset_url = self.password_reset_url(user)
        reset_client = Client()
        response = reset_client.get(original_reset_url)
        self.assertEqual(response.status_code, 302)
        new_password = "Reset-To-A-New-Password-93!"
        response = reset_client.post(
            response["Location"],
            {"new_password1": new_password, "new_password2": new_password},
        )
        self.assertRedirects(response, reverse("accounts:password_reset_complete"))

        self.assertEqual(first_session.get(reverse("core:dashboard_router")).status_code, 302)
        self.assertEqual(second_session.get(reverse("core:dashboard_router")).status_code, 302)
        reuse = Client().get(original_reset_url)
        self.assertEqual(reuse.status_code, 200)
        self.assertContains(reuse, "Reset unavailable")

    def test_password_reset_token_expires(self):
        user = self.create_eligible_user()
        issued_at = datetime(2026, 1, 1, 12, 0, 0)
        with patch.object(default_token_generator, "_now", return_value=issued_at):
            token = default_token_generator.make_token(user)
        with override_settings(PASSWORD_RESET_TIMEOUT=1):
            with patch.object(
                default_token_generator,
                "_now",
                return_value=issued_at + timedelta(seconds=2),
            ):
                self.assertFalse(default_token_generator.check_token(user, token))

    def test_logout_requires_post(self):
        user = self.create_eligible_user()
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.assertRedirects(
            self.client.post(reverse("accounts:logout")),
            reverse("core:home"),
        )

    def test_owner_can_update_account_access_and_new_social_users_default_to_customer(self):
        from apps.accounts.adapters import CustomSocialAccountAdapter
        from unittest.mock import MagicMock

        # 1. Verify CustomSocialAccountAdapter.populate_user forces brand-new user to CUSTOMER
        adapter = CustomSocialAccountAdapter()
        dummy_sociallogin = MagicMock()
        dummy_sociallogin.user = User(email="newgoogle@example.com")
        populated = adapter.populate_user(
            request=None,
            sociallogin=dummy_sociallogin,
            data={"email": "newgoogle@example.com", "first_name": "New", "last_name": "Client"},
        )
        self.assertEqual(populated.role, User.Role.CUSTOMER)

        # 2. Verify Owner can change an account's role between CUSTOMER, STAFF, and CASHIER
        owner = self.create_eligible_user("owner@example.com", User.Role.OWNER)
        target = self.create_eligible_user("guest@example.com", User.Role.CUSTOMER)
        self.client.force_login(owner)

        # Promote CUSTOMER -> STAFF with specialty & permissions
        resp = self.client.post(
            reverse("accounts:update_account_access", kwargs={"user_id": target.pk}),
            {
                "role": User.Role.STAFF,
                "is_active": "on",
                "can_manage_bookings": "on",
                "specialty": "Softgel & Nail Art Specialist",
            },
        )
        self.assertRedirects(resp, reverse("accounts:invite_internal"))
        target.refresh_from_db()
        self.assertEqual(target.role, User.Role.STAFF)
        self.assertTrue(target.can_manage_bookings)
        self.assertTrue(hasattr(target, "staff_profile"))
        self.assertEqual(target.staff_profile.specialty, "Softgel & Nail Art Specialist")

        # Demote STAFF -> CUSTOMER removes StaffProfile and ensures Customer profile exists
        resp2 = self.client.post(
            reverse("accounts:update_account_access", kwargs={"user_id": target.pk}),
            {
                "role": User.Role.CUSTOMER,
                "is_active": "on",
            },
        )
        self.assertRedirects(resp2, reverse("accounts:invite_internal"))
        target.refresh_from_db()
        self.assertEqual(target.role, User.Role.CUSTOMER)
        self.assertTrue(hasattr(target, "customer_profile"))

        # 3. Verify passwordless Google user can set a backup password via password_change view
        google_user = User.objects.create_user(
            "googleclient@example.com",
            password=None,
            role=User.Role.CUSTOMER,
            email_verified_at=timezone.now(),
            is_active=True,
        )
        self.assertFalse(google_user.has_usable_password())
        self.client.force_login(google_user)
        get_pw = self.client.get(reverse("accounts:password_change"))
        self.assertEqual(get_pw.status_code, 200)
        self.assertContains(get_pw, "Set backup salon password")
        post_pw = self.client.post(
            reverse("accounts:password_change"),
            {"new_password1": TEST_PASSWORD, "new_password2": TEST_PASSWORD},
        )
        self.assertEqual(post_pw.status_code, 302)
        google_user.refresh_from_db()
        self.assertTrue(google_user.has_usable_password())


