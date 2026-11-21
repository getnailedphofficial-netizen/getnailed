from datetime import time, timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audittrail.models import SecurityEvent
from apps.bookings.models import Appointment, AppointmentService
from apps.bookings.services import transition_appointment
from apps.customers.models import Customer
from apps.services.models import Service, ServiceCategory, StaffProfile

from .models import ServiceStatusHistory
from .services import assign_service, transition_service, visible_work_items


PASSWORD = "Correct-Horse-Battery-47!"


@override_settings(
    MFA_ENFORCE_OWNER=False,
    MFA_REQUIRE_INTERNAL_USERS=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class ServiceMonitoringTests(TestCase):
    def setUp(self):
        self.owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.cashier = self.create_user("cashier@example.com", User.Role.CASHIER)
        self.staff = self.create_user("staff@example.com", User.Role.STAFF)
        self.other_staff = self.create_user("other@example.com", User.Role.STAFF)
        StaffProfile.objects.create(user=self.staff)
        StaffProfile.objects.create(user=self.other_staff)
        category = ServiceCategory.objects.create(name="Monitoring")
        self.first_service = Service.objects.create(
            category=category,
            name="Gel manicure",
            duration_minutes=45,
            price=Decimal("850.00"),
        )
        self.second_service = Service.objects.create(
            category=category,
            name="Nail art",
            duration_minutes=30,
            price=Decimal("400.00"),
        )
        self.customer = Customer.objects.create(
            first_name="Board",
            last_name="Customer",
            email="board@example.com",
        )
        self.appointment = Appointment.objects.create(
            customer=self.customer,
            customer_name_snapshot=self.customer.full_name,
            customer_email_snapshot=self.customer.email,
            appointment_date=timezone.localdate(),
            start_time=time(23, 0),
            end_time=time(23, 59),
            assigned_staff=None,
            status=Appointment.Status.PENDING,
            expires_at=timezone.now() + timedelta(hours=1),
            verified_at=timezone.now(),
        )
        self.first = AppointmentService.objects.create(
            appointment=self.appointment,
            service=self.first_service,
            assigned_staff=self.staff,
            service_name=self.first_service.name,
            duration_minutes=self.first_service.duration_minutes,
            price=self.first_service.price,
            position=0,
        )
        self.second = AppointmentService.objects.create(
            appointment=self.appointment,
            service=self.second_service,
            assigned_staff=self.other_staff,
            service_name=self.second_service.name,
            duration_minutes=self.second_service.duration_minutes,
            price=self.second_service.price,
            position=1,
        )

    def create_user(self, email, role, **permissions):
        return User.objects.create_user(
            email,
            PASSWORD,
            role=role,
            email_verified_at=timezone.now(),
            is_active=True,
            is_active_staff_member=True,
            **permissions,
        )

    def test_staff_queryset_and_board_show_only_assigned_work(self):
        self.assertEqual(list(visible_work_items(self.staff)), [self.first])
        self.assertEqual(visible_work_items(self.owner).count(), 2)

        self.client.force_login(self.staff)
        response = self.client.get(reverse("monitoring:index"))
        self.assertContains(response, self.first.service_name)
        self.assertNotContains(response, self.second.service_name)
        self.assertContains(response, "every 10s")

    def test_valid_transitions_record_complete_history_and_sync_parent(self):
        transition_service(
            self.first,
            AppointmentService.Status.APPROVED,
            actor=self.owner,
            note="Ready",
        )
        transition_service(
            self.second,
            AppointmentService.Status.APPROVED,
            actor=self.cashier,
        )
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Appointment.Status.APPROVED)

        transition_service(
            self.first,
            AppointmentService.Status.ONGOING,
            actor=self.staff,
            note="Started at station 2",
        )
        self.first.refresh_from_db()
        self.appointment.refresh_from_db()
        self.assertIsNotNone(self.first.started_at)
        self.assertEqual(
            self.first.expected_finish_at,
            self.first.started_at + timedelta(minutes=45),
        )
        self.assertEqual(self.appointment.status, Appointment.Status.ONGOING)

        transition_service(
            self.first, AppointmentService.Status.COMPLETED, actor=self.staff
        )
        transition_service(
            self.second, AppointmentService.Status.ONGOING, actor=self.other_staff
        )
        transition_service(
            self.second, AppointmentService.Status.COMPLETED, actor=self.other_staff
        )
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Appointment.Status.COMPLETED)
        history = ServiceStatusHistory.objects.filter(appointment_service=self.first)
        self.assertEqual(history.count(), 3)
        started = history.get(new_status=AppointmentService.Status.ONGOING)
        self.assertEqual(started.user, self.staff)
        self.assertEqual(started.old_status, AppointmentService.Status.APPROVED)
        self.assertEqual(started.note, "Started at station 2")
        self.assertIsNotNone(started.timestamp)

    def test_staff_cannot_skip_or_update_another_staff_service(self):
        with self.assertRaises(PermissionDenied):
            transition_service(
                self.first,
                AppointmentService.Status.COMPLETED,
                actor=self.staff,
            )
        with self.assertRaises(PermissionDenied):
            transition_service(
                self.second,
                AppointmentService.Status.APPROVED,
                actor=self.staff,
            )
        self.assertFalse(ServiceStatusHistory.objects.exists())

    def test_assignment_requires_capability_and_updates_parent_summary(self):
        with self.assertRaises(PermissionDenied):
            assign_service(self.first, self.other_staff, actor=self.staff)
        assign_service(self.first, self.other_staff, actor=self.cashier)
        self.first.refresh_from_db()
        self.appointment.refresh_from_db()
        self.assertEqual(self.first.assigned_staff, self.other_staff)
        self.assertEqual(self.appointment.assigned_staff, self.other_staff)

    def test_status_and_assignment_views_record_audit_events(self):
        self.client.force_login(self.cashier)
        response = self.client.post(
            reverse(
                "monitoring:update_status",
                kwargs={"pk": self.first.pk, "status": AppointmentService.Status.APPROVED},
            ),
            {"note": "Checked"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<!doctype html>")
        self.assertTrue(
            SecurityEvent.objects.filter(
                action=SecurityEvent.Action.SERVICE_STATUS_CHANGED,
                target_id=str(self.first.pk),
                user=self.cashier,
            ).exists()
        )
        response = self.client.post(
            reverse("monitoring:assign", kwargs={"pk": self.first.pk}),
            {"assigned_staff": self.other_staff.pk},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            SecurityEvent.objects.filter(
                action=SecurityEvent.Action.SERVICE_ASSIGNED,
                target_id=str(self.first.pk),
                user=self.cashier,
            ).exists()
        )

    def test_workload_counters_filters_and_late_indicator(self):
        self.first.status = AppointmentService.Status.ONGOING
        self.first.started_at = timezone.now() - timedelta(hours=2)
        self.first.expected_finish_at = timezone.now() - timedelta(hours=1)
        self.first.save(
            update_fields=("status", "started_at", "expected_finish_at", "updated_at")
        )
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("monitoring:index"),
            {"date": timezone.localdate().isoformat(), "staff": self.staff.pk, "q": "Gel"},
        )
        self.assertContains(response, "Late")
        self.assertContains(response, self.first.service_name)
        self.assertNotContains(response, self.second.service_name)
        self.assertContains(response, "Currently ongoing")
        self.assertEqual(response.context["ongoing_count"], 1)
        workload = next(item for item in response.context["workloads"] if item.pk == self.staff.pk)
        self.assertEqual(workload.ongoing_count, 1)

    def test_appointment_status_updates_synchronize_each_service(self):
        transition_appointment(
            self.appointment,
            Appointment.Status.APPROVED,
            self.owner,
            "Approved together",
        )
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.first.status, AppointmentService.Status.APPROVED)
        self.assertEqual(self.second.status, AppointmentService.Status.APPROVED)
        self.assertEqual(ServiceStatusHistory.objects.count(), 2)

    def test_unverified_work_never_appears_on_board(self):
        self.appointment.status = Appointment.Status.UNVERIFIED
        self.appointment.save(update_fields=("status",))
        self.client.force_login(self.owner)
        response = self.client.get(reverse("monitoring:index"))
        self.assertNotContains(response, self.first.service_name)
        self.assertEqual(response.context["queue_count"], 0)

    def test_specialization_filtering_and_google_get_login_protection(self):
        from django.core.exceptions import ValidationError
        from apps.bookings.forms import AppointmentScheduleForm
        from apps.pos.forms import CheckoutItemForm

        # 1. GET /accounts/google/login/ redirects to login page (never auto-logs in via Back button)
        resp = self.client.get("/accounts/google/login/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("accounts:login"))

        # 2. Restrict self.staff to first_service only, and self.other_staff to second_service only
        self.staff.staff_profile.skills.set([self.first_service])
        self.other_staff.staff_profile.skills.set([self.second_service])

        # 3. assign_service rejects assigning self.other_staff to self.first (Gel manicure)
        with self.assertRaises(ValidationError):
            assign_service(self.first, self.other_staff, actor=self.owner)

        # 4. CheckoutItemForm rejects assigning self.other_staff to self.first_service
        pos_form = CheckoutItemForm(
            data={
                "service": self.first_service.pk,
                "assigned_staff": self.other_staff.pk,
                "quantity": 1,
            }
        )
        self.assertFalse(pos_form.is_valid())
        self.assertIn("assigned_staff", pos_form.errors)

        # 5. AppointmentScheduleForm filters assigned_staff queryset strictly to specialists
        single_svc_appt = Appointment.objects.create(
            customer=self.customer,
            customer_name_snapshot=self.customer.full_name,
            customer_email_snapshot=self.customer.email,
            appointment_date=timezone.localdate(),
            start_time=time(14, 0),
            end_time=time(14, 45),
            status=Appointment.Status.PENDING,
            expires_at=timezone.now() + timedelta(hours=1),
            verified_at=timezone.now(),
        )
        AppointmentService.objects.create(
            appointment=single_svc_appt,
            service=self.first_service,
            service_name=self.first_service.name,
            duration_minutes=self.first_service.duration_minutes,
            price=self.first_service.price,
            position=0,
        )
        sched_form = AppointmentScheduleForm(instance=single_svc_appt)
        allowed_staff_pks = list(
            sched_form.fields["assigned_staff"].queryset.values_list("pk", flat=True)
        )
        self.assertIn(self.staff.pk, allowed_staff_pks)
        self.assertNotIn(self.other_staff.pk, allowed_staff_pks)

