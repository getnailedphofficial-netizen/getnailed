import os
import tempfile
from datetime import datetime, time, timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User

from .forms import ServiceCategoryForm, ServiceForm, StaffProfileForm
from .models import (
    Service,
    ServiceCategory,
    StaffProfile,
    StaffSchedule,
    StaffTimeBlock,
)


PASSWORD = "Correct-Horse-Battery-47!"


def image_upload(name="service.png", image_format="PNG", content_type="image/png"):
    data = BytesIO()
    Image.new("RGB", (20, 20), color="#c9aeb1").save(data, format=image_format)
    return SimpleUploadedFile(name, data.getvalue(), content_type=content_type)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    MFA_ENCRYPTION_KEY="test-only-mfa-encryption-key",
    MFA_ENFORCE_OWNER=False,
    MFA_REQUIRE_INTERNAL_USERS=False,
    LOGIN_CAPTCHA_THRESHOLD=100,
)
class ServicesModuleTests(TestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media_directory.cleanup)

        self.owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.cashier = self.create_user("cashier@example.com", User.Role.CASHIER)
        self.staff = self.create_user("staff@example.com", User.Role.STAFF)
        self.customer = self.create_user("customer@example.com", User.Role.CUSTOMER)
        self.category = ServiceCategory.objects.create(
            name="Manicure",
            description="Nail grooming services",
        )

    def create_user(self, email, role):
        return User.objects.create_user(
            email,
            PASSWORD,
            role=role,
            email_verified_at=timezone.now(),
            is_active=True,
            is_active_staff_member=True,
        )

    def create_service(self, name="Classic Manicure", **extra):
        values = {
            "category": self.category,
            "description": "Shape, cuticle care, massage, and polish.",
            "duration_minutes": 45,
            "price": Decimal("350.00"),
            "is_active": True,
        }
        values.update(extra)
        return Service.objects.create(name=name, **values)

    def login(self, user):
        response = self.client.post(
            reverse("accounts:login"),
            {"login": user.email, "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 302)

    def test_category_and_service_validation_and_constraints(self):
        duplicate = ServiceCategory(name=" manicure ")
        duplicate.name = duplicate.name.strip()
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

        for duration, price in ((0, "350.00"), (481, "350.00"), (45, "0.00")):
            service = Service(
                name=f"Invalid {duration} {price}",
                category=self.category,
                duration_minutes=duration,
                price=price,
            )
            with self.subTest(duration=duration, price=price), self.assertRaises(ValidationError):
                service.full_clean()

        self.create_service()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_service(name="classic manicure")

    def test_staff_profile_accepts_only_staff_and_preserves_user_on_delete(self):
        profile = StaffProfile.objects.create(
            user=self.staff,
            specialty="Gel extensions",
        )
        self.assertEqual(profile.availability_status, StaffProfile.Availability.AVAILABLE)

        with self.assertRaises(ValidationError):
            StaffProfile.objects.create(user=self.cashier, specialty="Invalid")

        profile.delete()
        self.assertTrue(User.objects.filter(pk=self.staff.pk).exists())

    def test_staff_role_cannot_change_while_profile_exists(self):
        StaffProfile.objects.create(user=self.staff, specialty="Nail art")
        self.staff.role = User.Role.CASHIER
        with self.assertRaises(ValidationError):
            self.staff.full_clean()
        with self.assertRaises(ValidationError):
            self.staff.save(update_fields=("role",))

    def test_owner_managed_forms_recheck_actor_and_prevent_role_tampering(self):
        denied_category = ServiceCategoryForm({"name": "Nail Art"}, actor=self.cashier)
        self.assertFalse(denied_category.is_valid())
        self.assertIn("Only an owner", denied_category.non_field_errors()[0])

        denied_service = ServiceForm(
            {
                "name": "Gel Manicure",
                "category": self.category.pk,
                "duration_minutes": 60,
                "price": "650.00",
                "is_active": "on",
            },
            actor=self.staff,
        )
        self.assertFalse(denied_service.is_valid())

        profile_form = StaffProfileForm(
            {
                "user": self.cashier.pk,
                "specialty": "Tampered role",
                "availability_status": StaffProfile.Availability.AVAILABLE,
                "is_active": "on",
            },
            actor=self.owner,
        )
        self.assertFalse(profile_form.is_valid())
        self.assertIn("user", profile_form.errors)

    def test_catalog_read_permissions_and_inactive_service_scope(self):
        active = self.create_service()
        inactive = self.create_service("Retired Manicure", is_active=False)

        for user in (self.owner, self.cashier):
            client = Client()
            client.force_login(user)
            response = client.get(reverse("services:index"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, active.name)
            if user == self.owner:
                self.assertContains(response, inactive.name)
            else:
                self.assertNotContains(response, inactive.name)
                self.assertNotContains(response, "Add service")

        for user in (self.staff, self.customer):
            client = Client()
            client.force_login(user)
            self.assertEqual(client.get(reverse("services:index")).status_code, 403)
        self.assertEqual(Client().get(reverse("services:index")).status_code, 302)

    def test_management_routes_are_owner_only(self):
        routes = (
            reverse("services:service_create"),
            reverse("services:category_list"),
            reverse("services:category_create"),
            reverse("services:staff_list"),
            reverse("services:staff_create"),
        )
        for route in routes:
            owner_client = Client()
            owner_client.force_login(self.owner)
            self.assertEqual(owner_client.get(route).status_code, 200)
            for denied in (self.cashier, self.staff, self.customer):
                denied_client = Client()
                denied_client.force_login(denied)
                self.assertEqual(denied_client.get(route).status_code, 403)
            self.assertEqual(Client().get(route).status_code, 302)

    def test_update_delete_routes_enforce_owner_and_recent_reauthentication(self):
        service = self.create_service()
        profile = StaffProfile.objects.create(user=self.staff)
        protected_updates = (
            reverse("services:service_update", args=(service.pk,)),
            reverse("services:category_update", args=(self.category.pk,)),
            reverse("services:staff_update", args=(profile.pk,)),
        )
        protected_deletes = (
            reverse("services:service_delete", args=(service.pk,)),
            reverse("services:category_delete", args=(self.category.pk,)),
            reverse("services:staff_delete", args=(profile.pk,)),
        )
        for user in (self.cashier, self.staff, self.customer):
            client = Client()
            client.force_login(user)
            for route in (*protected_updates, *protected_deletes):
                self.assertEqual(client.get(route).status_code, 403)

        stale_owner = Client()
        stale_owner.force_login(self.owner)
        for route in protected_deletes:
            response = stale_owner.get(route)
            self.assertEqual(response.status_code, 302)
            self.assertIn("reauthenticate", response["Location"])

    def test_owner_can_create_update_and_delete_catalog_records(self):
        self.login(self.owner)
        category_response = self.client.post(
            reverse("services:category_create"),
            {"name": " Nail Art ", "description": "Creative details"},
        )
        self.assertEqual(category_response.status_code, 302)
        category = ServiceCategory.objects.get(name="Nail Art")

        create_response = self.client.post(
            reverse("services:service_create"),
            {
                "name": "French Tip Finish",
                "category": category.pk,
                "description": "Classic clean tips",
                "duration_minutes": 25,
                "price": "250.00",
                "is_active": "on",
            },
        )
        self.assertEqual(create_response.status_code, 302)
        service = Service.objects.get(name="French Tip Finish")

        update_response = self.client.post(
            reverse("services:service_update", args=(service.pk,)),
            {
                "name": "French Tip Design",
                "category": category.pk,
                "description": "Updated",
                "duration_minutes": 30,
                "price": "275.00",
                "is_active": "on",
            },
        )
        self.assertEqual(update_response.status_code, 302)
        service.refresh_from_db()
        self.assertEqual(service.name, "French Tip Design")
        self.assertEqual(service.price, Decimal("275.00"))

        delete_response = self.client.post(
            reverse("services:service_delete", args=(service.pk,))
        )
        self.assertEqual(delete_response.status_code, 302)
        self.assertFalse(Service.objects.filter(pk=service.pk).exists())

    def test_category_with_services_is_protected_from_deletion(self):
        self.create_service()
        self.login(self.owner)
        response = self.client.post(
            reverse("services:category_delete", args=(self.category.pk,)),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ServiceCategory.objects.filter(pk=self.category.pk).exists())
        self.assertContains(response, "Move or delete this category")

    def test_owner_can_manage_staff_profiles_without_changing_accounts(self):
        self.login(self.owner)
        create_response = self.client.post(
            reverse("services:staff_create"),
            {
                "user": self.staff.pk,
                "specialty": " Gel and acrylic enhancements ",
                "availability_status": StaffProfile.Availability.NOT_ACCEPTING,
                "is_active": "on",
            },
        )
        self.assertEqual(create_response.status_code, 302)
        profile = StaffProfile.objects.get(user=self.staff)
        self.assertEqual(profile.specialty, "Gel and acrylic enhancements")

        update_response = self.client.post(
            reverse("services:staff_update", args=(profile.pk,)),
            {
                "user": self.staff.pk,
                "specialty": "Nail art",
                "availability_status": StaffProfile.Availability.AVAILABLE,
                "is_active": "on",
            },
        )
        self.assertEqual(update_response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.specialty, "Nail art")

        delete_response = self.client.post(
            reverse("services:staff_delete", args=(profile.pk,))
        )
        self.assertEqual(delete_response.status_code, 302)
        self.assertFalse(StaffProfile.objects.filter(pk=profile.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.staff.pk).exists())

    def test_search_filters_and_pagination_compose(self):
        gel_category = ServiceCategory.objects.create(name="Gel")
        for index in range(14):
            self.create_service(f"Classic Service {index:02d}")
        Service.objects.create(
            name="Gel Signature",
            category=gel_category,
            duration_minutes=60,
            price="700.00",
            is_active=False,
        )
        self.client.force_login(self.owner)

        page = self.client.get(reverse("services:index"), {"q": "Classic", "page": 2})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["page_obj"].paginator.count, 14)
        self.assertEqual(page.context["page_obj"].number, 2)

        filtered = self.client.get(
            reverse("services:index"),
            {"category": gel_category.pk, "status": "inactive"},
        )
        self.assertEqual(list(filtered.context["page_obj"]), list(Service.objects.filter(name="Gel Signature")))

    def test_valid_image_uses_safe_name_and_invalid_image_is_rejected(self):
        self.client.force_login(self.owner)
        valid_response = self.client.post(
            reverse("services:service_create"),
            {
                "name": "Photo Service",
                "category": self.category.pk,
                "duration_minutes": 30,
                "price": "300.00",
                "is_active": "on",
                "image": image_upload("../../customer-name.png"),
            },
        )
        self.assertEqual(
            valid_response.status_code,
            302,
            valid_response.context["form"].errors if valid_response.context else "",
        )
        service = Service.objects.get(name="Photo Service")
        self.assertRegex(service.image.name, r"^service-images/[0-9a-f]{32}\.png$")
        self.assertTrue(os.path.exists(service.image.path))

        invalid_response = self.client.post(
            reverse("services:service_create"),
            {
                "name": "Unsafe Service",
                "category": self.category.pk,
                "duration_minutes": 30,
                "price": "300.00",
                "is_active": "on",
                "image": SimpleUploadedFile(
                    "attack.png",
                    b"<script>alert(1)</script>",
                    content_type="image/png",
                ),
            },
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(invalid_response, "Upload a valid image")
        self.assertFalse(Service.objects.filter(name="Unsafe Service").exists())

    def test_direct_model_image_validation_and_file_cleanup(self):
        service = Service(
            name="Direct Image",
            category=self.category,
            duration_minutes=30,
            price="300.00",
            image=image_upload(),
        )
        service.save()
        old_path = service.image.path
        self.assertTrue(os.path.exists(old_path))

        service.image = image_upload("replacement.jpg", "JPEG", "image/jpeg")
        with self.captureOnCommitCallbacks(execute=True):
            service.save()
        new_path = service.image.path
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(new_path))

        with self.captureOnCommitCallbacks(execute=True):
            service.delete()
        self.assertFalse(os.path.exists(new_path))

        invalid = Service(
            name="Invalid Direct Image",
            category=self.category,
            duration_minutes=30,
            price="300.00",
            image=SimpleUploadedFile("bad.png", b"bad", content_type="image/png"),
        )
        with self.assertRaises(ValidationError):
            invalid.save()

    def test_failed_database_save_removes_newly_stored_image(self):
        self.create_service("Unique Collision")
        collision = Service(
            name="unique collision",
            category=self.category,
            duration_minutes=30,
            price="300.00",
            image=image_upload(),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            collision.save()
        self.assertTrue(collision.image.name)
        self.assertFalse(collision.image.storage.exists(collision.image.name))

    def test_fixture_loads_realistic_catalog(self):
        Service.objects.all().delete()
        ServiceCategory.objects.all().delete()
        call_command("loaddata", "stage1_catalog", verbosity=0)
        self.assertEqual(ServiceCategory.objects.count(), 5)
        self.assertGreaterEqual(Service.objects.count(), 15)
        self.assertTrue(Service.objects.filter(name="Classic Manicure", price="350.00").exists())

    def test_browser_mutations_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(reverse("services:category_create"), {"name": "Blocked"}).status_code,
            403,
        )
        self.assertEqual(
            client.post(reverse("services:service_create"), {}).status_code,
            403,
        )

    def test_admin_is_registered_and_owner_restricted(self):
        for model in (ServiceCategory, Service, StaffProfile):
            model_admin = admin.site._registry[model]
            owner_request = RequestFactory().get("/admin/")
            owner_request.user = self.owner
            cashier_request = RequestFactory().get("/admin/")
            cashier_request.user = self.cashier
            self.assertTrue(model_admin.has_view_permission(owner_request))
            self.assertFalse(model_admin.has_view_permission(cashier_request))
            self.assertFalse(model_admin.has_delete_permission(owner_request))

    def test_service_has_no_physical_inventory(self):
        service = self.create_service("Gel Polish")
        self.assertFalse(service.has_physical_inventory)

    def test_staff_skills_schedules_and_blocks(self):
        from datetime import time
        service1 = self.create_service("Pedicure Deluxe")
        service2 = self.create_service("Acrylic Extensions")

        profile, _ = StaffProfile.objects.get_or_create(user=self.staff)
        profile.skills.add(service1)

        self.assertTrue(profile.can_perform(service1))
        self.assertFalse(profile.can_perform(service2))

        # Schedule creation for Monday (day 0)
        schedule = StaffSchedule.objects.create(
            staff=self.staff,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(18, 0),
            is_working=True,
        )
        self.assertEqual(schedule.day_of_week, 0)

        # Time block creation
        block = StaffTimeBlock.objects.create(
            staff=self.staff,
            date=timezone.localdate(),
            start_time=time(12, 0),
            end_time=time(13, 0),
            reason="Lunch break",
        )
        self.assertEqual(block.reason, "Lunch break")

    def test_schedule_crud_views(self):
        client = Client()
        client.force_login(self.owner)
        list_resp = client.get(reverse("services:schedule_list"))
        self.assertEqual(list_resp.status_code, 200)

        # Create schedule
        create_resp = client.post(
            reverse("services:schedule_create"),
            {
                "staff": self.staff.pk,
                "day_of_week": 1,
                "start_time": "09:00",
                "end_time": "18:00",
                "is_working": "on",
            },
        )
        self.assertRedirects(create_resp, reverse("services:schedule_list"))
        schedule = StaffSchedule.objects.get(staff=self.staff, day_of_week=1)
        self.assertTrue(schedule.is_working)

        # Update schedule
        update_resp = client.post(
            reverse("services:schedule_update", kwargs={"pk": schedule.pk}),
            {
                "staff": self.staff.pk,
                "day_of_week": 1,
                "start_time": "10:00",
                "end_time": "19:00",
                "is_working": "on",
            },
        )
        self.assertRedirects(update_resp, reverse("services:schedule_list"))
        schedule.refresh_from_db()
        self.assertEqual(schedule.start_time.strftime("%H:%M"), "10:00")

    def test_time_block_crud_views(self):
        client = Client()
        client.force_login(self.owner)
        list_resp = client.get(reverse("services:time_block_list"))
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(list_resp.context["hours_display"]), 13)
        self.assertEqual(list_resp.context["hours_display"][0]["label"], "9 am")
        self.assertEqual(list_resp.context["hours_display"][-1]["label"], "9 pm")

        today_str = timezone.localdate().isoformat()
        create_resp = client.post(
            reverse("services:time_block_create"),
            {
                "staff": self.staff.pk,
                "date": today_str,
                "start_time": "13:00",
                "end_time": "14:00",
                "reason": "Doctor appointment",
            },
        )
        self.assertRedirects(create_resp, reverse("services:time_block_list"))
        block = StaffTimeBlock.objects.get(staff=self.staff, reason="Doctor appointment")
        self.assertEqual(block.start_time.strftime("%H:%M"), "13:00")

    def test_visual_time_blocking_grid_and_multi_day_repeat(self):
        client = Client()
        client.force_login(self.owner)
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        day_after = today + timedelta(days=2)

        # Multi-day repeating time block creation
        resp = client.post(
            reverse("services:time_block_create"),
            {
                "staff": self.staff.pk,
                "date": today.isoformat(),
                "start_time": "12:00",
                "end_time": "13:00",
                "reason": "lunch",
                "repeat_dates": [tomorrow.isoformat(), day_after.isoformat()],
            },
        )
        self.assertRedirects(resp, reverse("services:time_block_list"))
        self.assertEqual(StaffTimeBlock.objects.filter(staff=self.staff, reason="lunch").count(), 3)

        # Visual grid renders the blocks and color classes
        list_resp = client.get(reverse("services:time_block_list"), {"date": today.isoformat()})
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, "Time Blocking Schedule")
        self.assertContains(list_resp, "block-color-lime")
        self.assertContains(list_resp, "lunch")
        self.assertContains(list_resp, "Lunch")
        self.assertContains(list_resp, "Work Time")
        self.assertContains(list_resp, "Leave")
        self.assertNotContains(list_resp, "Deep Work / Prep / Social")
        self.assertNotContains(list_resp, "Personal / Lunch / Admin")
        self.assertNotContains(list_resp, "Work Block / Meetings")
        self.assertNotContains(list_resp, "Leave / Doctor / Off")

    def test_category_color_mappings(self):
        from apps.services.views import _get_block_color_class
        self.assertEqual(_get_block_color_class("Lunch"), "block-color-lime")
        self.assertEqual(_get_block_color_class("lunch"), "block-color-lime")
        self.assertEqual(_get_block_color_class("Work Time"), "block-color-blue")
        self.assertEqual(_get_block_color_class("work time"), "block-color-blue")
        self.assertEqual(_get_block_color_class("Leave"), "block-color-rose")
        self.assertEqual(_get_block_color_class("leave"), "block-color-rose")
        self.assertEqual(_get_block_color_class("Doctor"), "block-color-rose")

    def test_staff_time_block_django_admin_registered(self):
        from django.contrib import admin
        from apps.services.models import StaffTimeBlock, StaffSchedule
        self.assertIn(StaffTimeBlock, admin.site._registry)
        self.assertIn(StaffSchedule, admin.site._registry)

    def test_staff_specialty_filter_and_setup_staff_command(self):
        from django.core.management import call_command
        from io import StringIO
        from unittest.mock import patch

        out = StringIO()
        with patch.dict("os.environ", {"GETNAILED_STAFF_PASSWORD": "Test-only-Staff-Password-123!"}):
            call_command("setup_staff", stdout=out)
        output = out.getvalue()
        self.assertIn("Successfully set up all 11 staff members!", output)
        self.assertNotIn("Test-only-Staff-Password-123!", output)

        # Check all 11 staff users exist with role STAFF
        self.assertEqual(User.objects.filter(role=User.Role.STAFF).count(), 11)

        # Check cashiers have can_use_pos=True
        cashier_emails = [
            "phen.abino@getnailed.com",
            "verna.agustin@getnailed.com",
            "aila.ramos@getnailed.com",
            "sheng.lucilla@getnailed.com",
        ]
        for email in cashier_emails:
            user = User.objects.get(email=email)
            self.assertTrue(user.can_use_pos)

        # Non-cashiers have can_use_pos=False
        nory = User.objects.get(email="nory.pecaso@getnailed.com")
        self.assertFalse(nory.can_use_pos)

        # Verify specialty filter on staff list view
        self.client.force_login(self.owner)
        resp_nail = self.client.get(reverse("services:staff_list"), {"specialty": "NAIL ART"})
        self.assertEqual(resp_nail.status_code, 200)
        self.assertContains(resp_nail, "Rita Inoferio")
        self.assertNotContains(resp_nail, "Josephine (Phen)")

        resp_cashier = self.client.get(reverse("services:staff_list"), {"specialty": "CASHIER"})
        self.assertEqual(resp_cashier.status_code, 200)
        self.assertContains(resp_cashier, "Vernalyn (Verna)")
        self.assertNotContains(resp_cashier, "Mary Ann (Ann)")

    def test_multiple_add_time_blocks_with_minute_precision(self):
        client = Client()
        client.force_login(self.owner)
        today = timezone.localdate()

        resp = client.post(
            reverse("services:time_block_create"),
            {
                "staff": self.staff.pk,
                "date": today.isoformat(),
                "start_time": ["09:15", "13:30", "15:45"],
                "end_time": ["12:00", "15:00", "16:30"],
                "reason": ["Work Time", "Work Time", "Break"],
            },
        )
        self.assertRedirects(resp, reverse("services:time_block_list"))

        blocks = StaffTimeBlock.objects.filter(staff=self.staff, date=today).order_by("start_time")
        self.assertEqual(blocks.count(), 3)
        self.assertEqual(blocks[0].start_time.strftime("%H:%M"), "09:15")
        self.assertEqual(blocks[0].end_time.strftime("%H:%M"), "12:00")
        self.assertEqual(blocks[1].start_time.strftime("%H:%M"), "13:30")
        self.assertEqual(blocks[2].start_time.strftime("%H:%M"), "15:45")
        self.assertEqual(blocks[2].reason, "Break")

    def test_multiple_staff_all_active_technicians_creation(self):
        client = Client()
        client.force_login(self.owner)
        today = timezone.localdate()
        active_staff_count = User.objects.filter(role=User.Role.STAFF, is_active=True).count()

        resp = client.post(
            reverse("services:time_block_create"),
            {
                "all_staff": "true",
                "date": today.isoformat(),
                "start_time": "12:15",
                "end_time": "13:15",
                "reason": "Team Lunch",
            },
        )
        self.assertRedirects(resp, reverse("services:time_block_list"))
        self.assertEqual(StaffTimeBlock.objects.filter(date=today, reason="Team Lunch").count(), active_staff_count)

    def test_overlapping_side_by_side_cluster_layout(self):
        from apps.services.views import _layout_day_blocks, START_HOUR, END_HOUR
        today = timezone.localdate()

        b1 = StaffTimeBlock.objects.create(
            staff=self.staff,
            date=today,
            start_time=datetime.strptime("09:00", "%H:%M").time(),
            end_time=datetime.strptime("10:00", "%H:%M").time(),
            reason="Work Time",
        )
        b2 = StaffTimeBlock.objects.create(
            staff=self.staff,
            date=today,
            start_time=datetime.strptime("09:30", "%H:%M").time(),
            end_time=datetime.strptime("10:30", "%H:%M").time(),
            reason="Lunch",
        )

        grid_start_min = START_HOUR * 60
        grid_end_min = END_HOUR * 60
        layout = _layout_day_blocks([b1, b2], grid_start_min, grid_end_min)

        self.assertEqual(len(layout), 2)
        # Both overlap from 9:30 to 10:00 -> num_cols should be 2
        self.assertEqual(layout[0]["num_cols"], 2)
        self.assertEqual(layout[1]["num_cols"], 2)
        self.assertEqual(layout[0]["width_pct"], 50.0)
        self.assertEqual(layout[1]["width_pct"], 50.0)
        self.assertNotEqual(layout[0]["left_pct"], layout[1]["left_pct"])

    def test_staff_schedule_leave_and_lunch_break_policy(self):
        from apps.services.schedules import (
            apply_staff_weekly_schedules,
            sync_staff_time_blocks,
            DEFAULT_STAFF_LEAVE_ASSIGNMENTS,
        )

        # Apply schedules to all active staff
        updated = apply_staff_weekly_schedules()
        self.assertGreater(updated, 0)

        # Verify each staff member has exactly 1 day off (leave) and 6 working days
        for staff in User.objects.filter(role=User.Role.STAFF, is_active=True):
            schedules = staff.schedules.all()
            self.assertEqual(schedules.count(), 7)
            working_count = schedules.filter(is_working=True).count()
            leave_count = schedules.filter(is_working=False).count()
            self.assertEqual(working_count, 6)
            self.assertEqual(leave_count, 1)

            # Check working shifts have 12:00 PM to 1:00 PM lunch break
            for ws in schedules.filter(is_working=True):
                self.assertEqual(ws.lunch_start.strftime("%H:%M"), "12:00")
                self.assertEqual(ws.lunch_end.strftime("%H:%M"), "13:00")
                self.assertEqual(ws.start_time.strftime("%H:%M"), "09:00")
                self.assertEqual(ws.end_time.strftime("%H:%M"), "21:00")

        # Test sync_staff_time_blocks populates Lunch and Leave blocks
        today = timezone.localdate()
        tb_count = sync_staff_time_blocks(ref_date=today, weeks=2)
        self.assertGreater(tb_count, 0)
        self.assertTrue(StaffTimeBlock.objects.filter(reason="Lunch").exists())
        self.assertTrue(StaffTimeBlock.objects.filter(reason="Leave").exists())

    def test_schedule_list_owner_view_filters_and_sync(self):
        from apps.services.schedules import apply_staff_weekly_schedules
        apply_staff_weekly_schedules()

        client = Client()
        client.force_login(self.owner)

        # 1. Main schedule list view renders summary cards and table
        resp = client.get(reverse("services:schedule_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Monday to Sunday")
        self.assertContains(resp, "12:00 PM – 01:00 PM")
        self.assertContains(resp, "1 Day Leave per Staff")
        self.assertContains(resp, "Universal Lunch Break")

        # 2. Filter by day of week (Monday = 0)
        resp_mon = client.get(reverse("services:schedule_list"), {"day": "0"})
        self.assertEqual(resp_mon.status_code, 200)
        self.assertContains(resp_mon, "Monday")

        # 3. Filter by status (leave / day off)
        resp_leave = client.get(reverse("services:schedule_list"), {"status": "leave"})
        self.assertEqual(resp_leave.status_code, 200)
        self.assertContains(resp_leave, "Day Off (Leave)")

        # 4. POST sync_time_blocks action works
        post_sync = client.post(
            reverse("services:schedule_list"),
            {"action": "sync_time_blocks"},
        )
        self.assertRedirects(post_sync, reverse("services:schedule_list"))

    def test_booking_availability_blocks_lunch_and_leave(self):
        from datetime import time
        from apps.bookings.services import (
            _validate_staff_availability,
            get_available_time_slots,
        )

        today = timezone.localdate()
        # Find next Monday and next Tuesday
        monday_offset = (0 - today.weekday()) % 7
        if monday_offset == 0:
            monday_offset = 7
        target_monday = today + timedelta(days=monday_offset)

        # Ensure self.staff has schedule with Monday as leave and other days working
        StaffSchedule.objects.update_or_create(
            staff=self.staff,
            day_of_week=0,  # Monday
            defaults={
                "start_time": "09:00:00",
                "end_time": "21:00:00",
                "lunch_start": "12:00:00",
                "lunch_end": "13:00:00",
                "is_working": False,  # On leave Monday
            },
        )
        StaffSchedule.objects.update_or_create(
            staff=self.staff,
            day_of_week=1,  # Tuesday
            defaults={
                "start_time": "09:00:00",
                "end_time": "21:00:00",
                "lunch_start": "12:00:00",
                "lunch_end": "13:00:00",
                "is_working": True,  # Working Tuesday
            },
        )

        profile, _ = StaffProfile.objects.get_or_create(
            user=self.staff,
            defaults={
                "availability_status": StaffProfile.Availability.AVAILABLE,
                "is_active": True,
            },
        )
        profile.availability_status = StaffProfile.Availability.AVAILABLE
        profile.is_active = True
        profile.save()

        svc = self.create_service("Basic Polish", duration_minutes=30)
        profile.skills.add(svc)

        # 1. Attempting appointment on Monday (Leave day) raises ValidationError
        with self.assertRaises(ValidationError) as ctx:
            _validate_staff_availability(
                self.staff,
                target_monday,
                time(10, 0),
                time(10, 30),
                services=[svc],
            )
        self.assertIn("not scheduled to work", str(ctx.exception))

        # 2. Attempting appointment on Tuesday during lunch break (12:00 - 12:30) raises ValidationError
        target_tuesday = target_monday + timedelta(days=1)
        with self.assertRaises(ValidationError) as ctx:
            _validate_staff_availability(
                self.staff,
                target_tuesday,
                time(12, 0),
                time(12, 30),
                services=[svc],
            )
        self.assertIn("lunch break", str(ctx.exception).lower())

        # 3. Attempting appointment on Tuesday outside lunch (10:00 - 10:30) succeeds
        result = _validate_staff_availability(
            self.staff,
            target_tuesday,
            time(10, 0),
            time(10, 30),
            services=[svc],
        )
        self.assertEqual(result, self.staff)

        # 4. get_available_time_slots excludes 12:00 PM and 12:30 PM for this staff
        slots = get_available_time_slots(target_tuesday, [svc.pk], staff_id=self.staff.pk)
        self.assertNotIn("12:00", slots)
        self.assertNotIn("12:30", slots)
        self.assertIn("10:00", slots)

    def test_schedule_list_contains_search_bar_and_zero_reload_views(self):
        """Staff schedules page renders search bar, view mode switcher, and all 3 view containers."""
        client = Client()
        client.force_login(self.owner)
        response = client.get(reverse("services:schedule_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="scheduleSearchInput"')
        self.assertContains(response, 'id="viewModeRoster"')
        self.assertContains(response, 'id="viewModeDaily"')
        self.assertContains(response, 'id="viewModeTable"')
        self.assertContains(response, 'id="quickEditShiftModal"')
        self.assertContains(response, 'id="syncShiftsForm"')

    def test_schedule_list_ajax_sync_time_blocks(self):
        """Sync shifts returns JSON with XMLHttpRequest header without reloading/redirecting."""
        client = Client()
        client.force_login(self.owner)
        response = client.post(
            reverse("services:schedule_list"),
            {"action": "sync_time_blocks"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("Successfully synchronized", data["message"])

    def test_schedule_update_ajax_get_and_post(self):
        """Schedule update endpoint supports GET and POST via AJAX for modal editing."""
        sched = StaffSchedule.objects.create(
            staff=self.staff,
            day_of_week=StaffSchedule.DayOfWeek.TUESDAY,
            start_time=time(9, 0),
            end_time=time(21, 0),
            lunch_start=time(12, 0),
            lunch_end=time(13, 0),
            is_working=True,
        )
        client = Client()
        client.force_login(self.owner)

        # 1. AJAX GET returns schedule details
        get_res = client.get(
            reverse("services:schedule_update", args=[sched.pk]),
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(get_res.status_code, 200)
        get_data = get_res.json()
        self.assertEqual(get_data["status"], "ok")
        self.assertEqual(get_data["schedule"]["id"], sched.pk)

        # 2. AJAX POST updates schedule and returns JSON
        post_res = client.post(
            reverse("services:schedule_update", args=[sched.pk]),
            {
                "staff": self.staff.pk,
                "day_of_week": StaffSchedule.DayOfWeek.TUESDAY,
                "start_time": "10:00",
                "end_time": "20:00",
                "lunch_start": "12:30",
                "lunch_end": "13:30",
                "is_working": "true",
            },
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(post_res.status_code, 200)
        post_data = post_res.json()
        self.assertEqual(post_data["status"], "ok")
        self.assertEqual(post_data["schedule"]["start_time"], "10:00")
        self.assertEqual(post_data["schedule"]["end_time"], "20:00")

        # Verify DB updated
        sched.refresh_from_db()
        self.assertEqual(sched.start_time, time(10, 0))
        self.assertEqual(sched.end_time, time(20, 0))







