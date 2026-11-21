import re
from datetime import time, timedelta
from decimal import Decimal

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.customers.models import Customer
from apps.notifications.models import Notification
from apps.services.models import (
    Service,
    ServiceCategory,
    StaffProfile,
    StaffSchedule,
    StaffTimeBlock,
)

from .models import Appointment, AppointmentService
from .services import (
    create_public_appointment,
    transition_appointment,
    update_appointment_schedule,
    verify_public_booking,
)


PASSWORD = "Correct-Horse-Battery-47!"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PUBLIC_BASE_URL="",
    MFA_ENFORCE_OWNER=False,
    MFA_REQUIRE_INTERNAL_USERS=False,
)
class AppointmentWorkflowTests(TestCase):
    def setUp(self):
        self.category = ServiceCategory.objects.create(name="Nails")
        self.service = Service.objects.create(
            category=self.category,
            name="Gel manicure",
            duration_minutes=45,
            price="850.00",
        )
        self.staff = self.create_user("staff@example.com", User.Role.STAFF)
        StaffProfile.objects.create(user=self.staff)
        self.owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.cashier = self.create_user("cashier@example.com", User.Role.CASHIER)
        self.request = RequestFactory().post("/bookings/new/review/")

    def create_user(self, email, role):
        return User.objects.create_user(
            email,
            PASSWORD,
            role=role,
            email_verified_at=timezone.now(),
            is_active=True,
            is_active_staff_member=True,
        )

    def appointment_data(self, email="guest@example.com", start_time=time(10, 0)):
        return {
            "first_name": "Guest",
            "last_name": "Customer",
            "email": email,
            "phone": "09171234567",
            "notes": "Quiet appointment",
            "appointment_date": timezone.localdate() + timedelta(days=2),
            "start_time": start_time,
            "assigned_staff": self.staff,
        }

    def create_appointment(self, **overrides):
        data = self.appointment_data()
        data.update(overrides)
        return create_public_appointment(data, [self.service], self.request)

    def test_creation_uses_canonical_service_snapshots_and_calculates_end_time(self):
        appointment = self.create_appointment()

        self.assertEqual(appointment.status, Appointment.Status.UNVERIFIED)
        self.assertEqual(appointment.end_time, time(10, 45))
        self.assertEqual(appointment.total_price, Decimal("850.00"))
        self.assertEqual(Customer.objects.count(), 1)
        row = AppointmentService.objects.get(appointment=appointment)
        self.assertEqual(row.service, self.service)
        self.assertEqual(row.service_name, "Gel manicure")

        self.service.name = "Renamed service"
        self.service.price = "900.00"
        self.service.save()
        row.refresh_from_db()
        self.assertEqual(row.service_name, "Gel manicure")
        self.assertEqual(str(row.price), "850.00")

    def test_overlapping_staff_appointment_is_rejected(self):
        first = self.create_appointment()
        first.status = Appointment.Status.APPROVED
        first.save(update_fields=("status",))

        with self.assertRaisesMessage(ValidationError, "already has an appointment"):
            self.create_appointment(email="second@example.com", start_time=time(10, 30))

        self.assertEqual(Appointment.objects.count(), 1)

    def test_verification_moves_request_to_pending_and_notifies_internal_users(self):
        appointment = self.create_appointment()
        code = re.search(r"\n(\d{8})\n", mail.outbox[-1].body).group(1)

        verified, token = verify_public_booking(appointment.reference, code, self.request)

        self.assertEqual(verified.status, Appointment.Status.PENDING)
        self.assertTrue(verified.accepts_public_access_token(token))
        self.assertEqual(
            set(Notification.objects.values_list("recipient__email", flat=True)),
            {self.owner.email, self.cashier.email, self.staff.email},
        )

    def test_customer_record_is_hidden_from_management_until_verification(self):
        appointment = self.create_appointment(email="hidden@example.com")
        self.client.force_login(self.cashier)
        self.assertNotContains(
            self.client.get(reverse("customers:index")), "hidden@example.com"
        )

        code = re.search(r"\n(\d{8})\n", mail.outbox[-1].body).group(1)
        verify_public_booking(appointment.reference, code, self.request)

        self.assertContains(
            self.client.get(reverse("customers:index")), "hidden@example.com"
        )

    def test_notifications_can_only_be_read_by_their_recipient(self):
        appointment = self.create_appointment()
        code = re.search(r"\n(\d{8})\n", mail.outbox[-1].body).group(1)
        verify_public_booking(appointment.reference, code, self.request)
        owner_notification = Notification.objects.get(
            appointment=appointment, recipient=self.owner
        )
        cashier_notification = Notification.objects.get(
            appointment=appointment, recipient=self.cashier
        )

        self.client.force_login(self.owner)
        denied = self.client.post(
            reverse("notifications:mark_read", kwargs={"pk": cashier_notification.pk})
        )
        self.assertEqual(denied.status_code, 404)
        response = self.client.post(
            reverse("notifications:mark_read", kwargs={"pk": owner_notification.pk})
        )
        self.assertRedirects(
            response,
            reverse("bookings:detail", kwargs={"reference": appointment.reference}),
            fetch_redirect_response=False,
        )
        owner_notification.refresh_from_db()
        self.assertIsNotNone(owner_notification.read_at)

    def test_staff_can_only_transition_their_assigned_appointment(self):
        appointment = self.create_appointment()
        appointment.status = Appointment.Status.APPROVED
        appointment.save(update_fields=("status",))

        transition_appointment(appointment, Appointment.Status.ONGOING, self.staff)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.ONGOING)

        other_staff = self.create_user("other@example.com", User.Role.STAFF)
        StaffProfile.objects.create(user=other_staff)
        with self.assertRaises(PermissionDenied):
            transition_appointment(appointment, Appointment.Status.COMPLETED, other_staff)

    def test_manager_assignment_uses_overlap_validation(self):
        first = self.create_appointment()
        first.status = Appointment.Status.APPROVED
        first.save(update_fields=("status",))
        second = self.create_appointment(
            email="unassigned@example.com", start_time=time(12, 0), assigned_staff=None
        )

        with self.assertRaisesMessage(ValidationError, "already has an appointment"):
            update_appointment_schedule(
                second,
                appointment_date=first.appointment_date,
                start_time=time(10, 15),
                assigned_staff=self.staff,
                actor=self.owner,
            )

    def test_public_wizard_creates_an_appointment(self):
        response = self.client.post(
            reverse("bookings:index"),
            {
                "first_name": "Web",
                "last_name": "Guest",
                "email": "web@example.com",
                "phone": "09171234567",
            },
        )
        self.assertRedirects(response, reverse("bookings:select_services"))
        response = self.client.post(
            reverse("bookings:select_services"), {"services": [self.service.pk]}
        )
        self.assertRedirects(response, reverse("bookings:select_schedule"))
        response = self.client.post(
            reverse("bookings:select_schedule"),
            {
                "appointment_date": (timezone.localdate() + timedelta(days=3)).isoformat(),
                "start_time": "14:00",
                "assigned_staff": self.staff.pk,
            },
        )
        self.assertRedirects(response, reverse("bookings:review"))
        response = self.client.post(reverse("bookings:review"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Appointment.objects.filter(customer_email_snapshot="web@example.com").exists())

    def test_management_and_detail_views_enforce_role_scope(self):
        appointment = self.create_appointment()
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("bookings:manage")).status_code, 200)
        self.assertNotContains(
            self.client.get(reverse("bookings:manage")), appointment.reference
        )

        appointment.status = Appointment.Status.PENDING
        appointment.save(update_fields=("status",))

        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("bookings:manage")).status_code, 403)
        self.assertEqual(self.client.get(reverse("bookings:assigned")).status_code, 200)
        self.assertEqual(
            self.client.get(
                reverse("bookings:detail", kwargs={"reference": appointment.reference})
            ).status_code,
            200,
        )

        other_staff = self.create_user("scoped@example.com", User.Role.STAFF)
        StaffProfile.objects.create(user=other_staff)
        self.client.force_login(other_staff)
        self.assertEqual(
            self.client.get(
                reverse("bookings:detail", kwargs={"reference": appointment.reference})
            ).status_code,
            404,
        )

    def test_shift_working_hours_constraint_blocks_appointment(self):
        target_date = timezone.localdate() + timedelta(days=2)
        weekday = target_date.weekday()
        StaffSchedule.objects.create(
            staff=self.staff,
            day_of_week=weekday,
            start_time=time(11, 0),
            end_time=time(18, 0),
            is_working=True,
        )

        with self.assertRaisesMessage(ValidationError, "working hours"):
            self.create_appointment(
                appointment_date=target_date,
                start_time=time(10, 0),
                assigned_staff=self.staff,
            )

        appt = self.create_appointment(
            appointment_date=target_date,
            start_time=time(11, 0),
            assigned_staff=self.staff,
        )
        self.assertEqual(appt.start_time, time(11, 0))

    def test_staff_time_block_blocks_appointment(self):
        target_date = timezone.localdate() + timedelta(days=2)
        StaffTimeBlock.objects.create(
            staff=self.staff,
            date=target_date,
            start_time=time(12, 0),
            end_time=time(13, 0),
            reason="Lunch break",
        )

        with self.assertRaisesMessage(ValidationError, "scheduled leave or time block"):
            self.create_appointment(
                appointment_date=target_date,
                start_time=time(12, 0),
                assigned_staff=self.staff,
            )

    def test_unqualified_staff_skill_constraint_blocks_appointment(self):
        target_date = timezone.localdate() + timedelta(days=2)
        other_service = Service.objects.create(
            category=self.category,
            name="Eyelash Extension",
            duration_minutes=60,
            price="1200.00",
        )
        profile = self.staff.staff_profile
        profile.skills.add(self.service)

        with self.assertRaisesMessage(ValidationError, "not qualified"):
            create_public_appointment(
                self.appointment_data(start_time=time(10, 0)),
                [other_service],
                self.request,
            )

    def test_multi_service_duration_block_and_api_slots(self):
        from .services import get_available_time_slots
        pedicure = Service.objects.create(
            category=self.category,
            name="Deluxe Pedicure",
            duration_minutes=60,
            price="500.00",
        )
        appt = create_public_appointment(
            self.appointment_data(start_time=time(10, 0)),
            [self.service, pedicure],
            self.request,
        )
        self.assertEqual(appt.total_duration_minutes, 105)
        self.assertEqual(appt.end_time, time(11, 45))

        target_date = timezone.localdate() + timedelta(days=2)
        slots = get_available_time_slots(
            target_date, [self.service.pk, pedicure.pk], staff_id=self.staff.pk
        )
        self.assertIsInstance(slots, list)
        self.assertTrue(len(slots) > 0)

    def test_technician_assignment_in_admin_and_auto_computation(self):
        from django.contrib.admin.sites import site
        self.owner.is_staff = True
        self.owner.save()
        customer = Customer.objects.create(
            first_name="Admin",
            last_name="Booking",
            email="adminbook@example.com",
            phone="09181234567",
        )
        admin_obj = site._registry[Appointment]
        rf = RequestFactory()
        post_data = {
            "customer": customer.pk,
            "customer_name_snapshot": "",
            "appointment_date": timezone.localdate().isoformat(),
            "start_time": "14:00:00",
            "status": Appointment.Status.APPROVED,
            "booking_source": Appointment.BookingSource.WALK_IN,
            "assigned_staff": str(self.staff.pk),
            "appointment_services-TOTAL_FORMS": "1",
            "appointment_services-INITIAL_FORMS": "0",
            "appointment_services-MIN_NUM_FORMS": "0",
            "appointment_services-MAX_NUM_FORMS": "1000",
            "appointment_services-0-service": str(self.service.pk),
            "appointment_services-0-status": "APPROVED",
            "appointment_services-0-position": "0",
        }
        req = rf.post("/admin/bookings/appointment/add/", post_data)
        req.user = self.owner

        FormClass = admin_obj.get_form(req)
        form = FormClass(req.POST)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        admin_obj.save_model(req, obj, form, change=False)

        inline_instances = admin_obj.get_inline_instances(req, obj)
        for inline in inline_instances:
            if isinstance(inline, admin_obj.inlines[0]):
                formset_cls = inline.get_formset(req, obj)
                formset = formset_cls(req.POST, prefix="appointment_services", instance=obj)
                self.assertTrue(formset.is_valid(), formset.errors)
                admin_obj.save_formset(req, form, formset, change=False)

        obj.refresh_from_db()
        self.assertEqual(obj.assigned_staff, self.staff)
        self.assertEqual(obj.customer_name_snapshot, customer.full_name)
        self.assertEqual(obj.total_duration_minutes, 45)
        self.assertEqual(obj.end_time, time(14, 45))
        self.assertEqual(obj.appointment_services.count(), 1)
        asvc = obj.appointment_services.first()
        self.assertEqual(asvc.assigned_staff, self.staff)
        self.assertEqual(asvc.service_name, self.service.name)
        self.assertEqual(asvc.duration_minutes, 45)

    def test_anti_spam_honeypot_rejection(self):
        from apps.bookings.forms import PublicContactForm
        # Normal human submission with empty honeypot
        valid_form = PublicContactForm({
            "first_name": "Maria",
            "last_name": "Clara",
            "email": "maria@example.com",
            "phone": "09171234567",
            "website_url_check": "",
        })
        self.assertTrue(valid_form.is_valid(), valid_form.errors)

        # Bot submission with honeypot filled
        bot_form = PublicContactForm({
            "first_name": "SpamBot",
            "last_name": "Trapped",
            "email": "spambot@spam.com",
            "phone": "09170000000",
            "website_url_check": "https://spam-link.ru",
        })
        self.assertFalse(bot_form.is_valid())
        self.assertIn("Spam submission detected.", str(bot_form.errors))

    def test_filter_sort_search_and_export_csv(self):
        from apps.customers.models import Customer
        customer = Customer.objects.create(
            first_name="UniqueSearchName",
            last_name="Customer",
            email="uniquesearch@example.com",
            phone="09998887777",
        )
        self.client.force_login(self.owner)
        target_date = timezone.localdate() + timedelta(days=3)
        appt = Appointment.objects.create(
            customer=customer,
            customer_name_snapshot="UniqueSearchName Customer",
            customer_email_snapshot="uniquesearch@example.com",
            customer_phone_snapshot="09998887777",
            appointment_date=target_date,
            start_time=time(10, 0),
            end_time=time(10, 45),
            assigned_staff=self.staff,
            status=Appointment.Status.APPROVED,
            booking_source=Appointment.BookingSource.PUBLIC,
            expires_at=timezone.now() + timedelta(hours=2),
        )

        # Search matching UniqueSearchName
        url = reverse("bookings:manage")
        res = self.client.get(url, {"search": "UniqueSearchName"})
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "UniqueSearchName")

        # Search non-matching term
        res_empty = self.client.get(url, {"search": "NonExistentXYZABC"})
        self.assertEqual(res_empty.status_code, 200)
        self.assertNotContains(res_empty, "UniqueSearchName")

        # Export to CSV
        csv_res = self.client.get(url, {"search": "UniqueSearchName", "export": "csv"})
        self.assertEqual(csv_res.status_code, 200)
        self.assertEqual(csv_res["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", csv_res["Content-Disposition"])
        content = csv_res.content.decode("utf-8")
        self.assertIn("Reference,Customer Name,Email", content)
        self.assertIn("UniqueSearchName", content)
        self.assertIn("uniquesearch@example.com", content)

    def test_walk_in_technician_availability_monitor(self):
        from apps.services.availability import get_walk_in_technicians_status
        status_data = get_walk_in_technicians_status()
        self.assertIn("technicians", status_data)
        self.assertIn("available_count", status_data)
        self.assertIsInstance(status_data["technicians"], list)

        # Access view as owner
        self.client.force_login(self.owner)
        res = self.client.get(reverse("services:walk_in_technicians"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Available Technicians for Walk-In")

    def test_consultation_assessment_recording(self):
        from apps.customers.models import Customer
        from apps.bookings.forms import PublicContactForm
        customer = Customer.objects.create(
            first_name="Ana",
            last_name="Santos",
            email="ana@example.com",
            phone="09171112222",
        )
        form = PublicContactForm({
            "first_name": "Ana",
            "last_name": "Santos",
            "email": "ana@example.com",
            "phone": "09171112222",
            "request_consultation": True,
            "nail_condition": "BRITTLE",
            "nail_allergies": "Sensitive to harsh acetone",
            "notes": "Prefers oval shape",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn("[NAIL CONSULTATION & HEALTH ASSESSMENT]", form.cleaned_data["notes"])
        self.assertIn("Brittle / Weak / Damaged", form.cleaned_data["notes"])
        self.assertIn("Sensitive to harsh acetone", form.cleaned_data["notes"])

        # Create appointment and record technician consultation advice
        appt = Appointment.objects.create(
            customer=customer,
            customer_name_snapshot="Ana Santos",
            customer_email_snapshot="ana@example.com",
            appointment_date=timezone.localdate() + timedelta(days=1),
            start_time=time(11, 0),
            end_time=time(11, 45),
            assigned_staff=self.staff,
            status=Appointment.Status.APPROVED,
            notes=form.cleaned_data["notes"],
            expires_at=timezone.now() + timedelta(hours=2),
        )
        self.client.force_login(self.staff)
        detail_url = reverse("bookings:detail", kwargs={"reference": appt.reference})
        post_res = self.client.post(detail_url, {
            "action": "consultation",
            "technician_notes": "Advised client on keratin treatment. Avoid buffing nails thin.",
        })
        self.assertEqual(post_res.status_code, 302)
        appt.refresh_from_db()
        self.assertIn("Technician Consultation Assessment", appt.notes)
        self.assertIn("Advised client on keratin treatment", appt.notes)

    def test_branch_selection_and_approval_promo_code_notification(self):
        appt = self.create_appointment(branch=Appointment.Branch.RETIRO_QC)
        self.assertEqual(appt.branch, Appointment.Branch.RETIRO_QC)
        appt.status = Appointment.Status.PENDING
        appt.save(update_fields=("status",))

        transition_appointment(appt, Appointment.Status.APPROVED, self.owner, note="Approved by owner")
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.APPROVED)
        self.assertTrue(bool(appt.promo_code_issued))
        self.assertIn(appt.promo_code_issued, mail.outbox[-1].body)

    def test_friday_to_sunday_peak_policy_for_services_300_and_below(self):
        low_service = Service.objects.create(
            category=self.category,
            name="Basic Nail Trim",
            duration_minutes=30,
            price="250.00",
        )
        # Find next Friday (weekday == 4)
        today = timezone.localdate()
        days_until_friday = (4 - today.weekday()) % 7 or 7
        friday = today + timedelta(days=days_until_friday)

        # Allowed when no bookings exist on that Friday yet
        first = create_public_appointment(
            self.appointment_data(email="first_fri@example.com", start_time=time(10, 0)) | {"appointment_date": friday},
            [low_service],
            self.request,
        )
        self.assertEqual(first.appointment_date, friday)

        # Rejected online when an active booking already exists on that Friday and service <= 300
        with self.assertRaisesMessage(ValidationError, "Friday–Sunday Peak Policy"):
            create_public_appointment(
                self.appointment_data(email="second_fri@example.com", start_time=time(14, 0)) | {"appointment_date": friday},
                [low_service],
                self.request,
            )

    def test_mandatory_reason_on_status_cancellation_or_no_show(self):
        from .forms import AppointmentStatusForm

        appt = self.create_appointment()
        appt.status = Appointment.Status.APPROVED
        appt.save(update_fields=("status",))

        invalid_form = AppointmentStatusForm(
            {"status": Appointment.Status.NO_SHOW, "note": ""},
            appointment=appt,
            actor=self.owner,
        )
        self.assertFalse(invalid_form.is_valid())
        self.assertIn("note", invalid_form.errors)

        valid_form = AppointmentStatusForm(
            {"status": Appointment.Status.NO_SHOW, "note": "Client did not arrive within 15 minutes."},
            appointment=appt,
            actor=self.owner,
        )
        self.assertTrue(valid_form.is_valid())

    def test_grace_period_auto_releases_slot_and_technician(self):
        from datetime import datetime
        from .services import process_booking_reminders_and_grace_period

        today = timezone.localdate()
        appt = self.create_appointment(
            appointment_date=today,
            start_time=time(10, 0),
        )
        appt.status = Appointment.Status.APPROVED
        appt.save(update_fields=("status",))

        simulated_now = timezone.make_aware(
            datetime.combine(today, time(10, 20)),
            timezone.get_current_timezone(),
        )
        summary = process_booking_reminders_and_grace_period(now=simulated_now)
        self.assertEqual(summary["grace_period_released"], 1)
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.NO_SHOW)

    def test_walk_in_referral_number_in_pos_and_messaging_chatbot(self):
        from apps.pos.services import create_sale
        from apps.pos.models import Payment

        sale = create_sale(
            cashier=self.owner,
            raw_items=[{"service": self.service, "assigned_staff": self.staff, "quantity": 1}],
            payment_method=Payment.Method.CASH,
            amount_tendered=Decimal("1000.00"),
        )
        self.assertTrue(sale.walk_in_referral_number.startswith("WLK-"))

        chat_res = self.client.post(
            reverse("notifications:chatbot_api"),
            data='{"message": "What are your branches and Friday policy?"}',
            content_type="application/json",
        )
        self.assertEqual(chat_res.status_code, 200)
        self.assertIn("Plaridel Branch", chat_res.json()["reply"])

    def test_guest_browser_can_browse_services_without_login_wall(self):
        response = self.client.get(
            reverse("bookings:index"),
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose Your Services")
        self.assertContains(response, self.service.name)

    def test_service_first_wizard_and_verified_customer_skips_email_otp(self):
        customer_user = self.create_user("client@example.com", User.Role.CUSTOMER)
        customer_user.first_name = "Maria"
        customer_user.last_name = "Santos"
        customer_user.save(update_fields=("first_name", "last_name"))
        Customer.objects.create(
            user=customer_user,
            first_name="Maria",
            last_name="Santos",
            email="client@example.com",
            phone="09171234567",
        )
        self.client.force_login(customer_user)

        # Step 1: Choose Services
        r1 = self.client.post(
            reverse("bookings:index"),
            {"wizard_step": "services", "services": [self.service.pk]},
        )
        self.assertRedirects(r1, reverse("bookings:select_schedule"))

        # Step 2: Choose Branch & Schedule
        booking_date = (timezone.localdate() + timedelta(days=3)).isoformat()
        r2 = self.client.post(
            reverse("bookings:select_schedule"),
            {
                "branch": Appointment.Branch.PLARIDEL,
                "appointment_date": booking_date,
                "start_time": "14:00",
                "assigned_staff": self.staff.pk,
            },
        )
        self.assertRedirects(r2, reverse("bookings:contact_details"))

        # Step 3: Contact Details (auto-filled)
        r3 = self.client.post(
            reverse("bookings:contact_details"),
            {
                "branch": Appointment.Branch.PLARIDEL,
                "first_name": "Maria",
                "last_name": "Santos",
                "email": "client@example.com",
                "phone": "09171234567",
            },
        )
        self.assertRedirects(r3, reverse("bookings:review"))

        # Step 4: Confirm — verified customer goes straight to PENDING (no OTP block)
        r4 = self.client.post(reverse("bookings:review"))
        self.assertRedirects(r4, reverse("core:customer_appointments"))
        appt = Appointment.objects.get(customer_email_snapshot="client@example.com")
        self.assertEqual(appt.status, Appointment.Status.PENDING)
        self.assertEqual(appt.branch, Appointment.Branch.PLARIDEL)
        self.assertIsNotNone(appt.verified_at)

    def test_owner_and_staff_quick_status_transitions(self):
        appt = self.create_appointment()
        appt.status = Appointment.Status.PENDING
        appt.verified_at = timezone.now()
        appt.save(update_fields=("status", "verified_at"))

        # Owner 1-click Approve from dashboard
        self.client.force_login(self.owner)
        res_approve = self.client.post(
            reverse("bookings:quick_status", kwargs={"reference": appt.reference}),
            {"status": Appointment.Status.APPROVED, "next": reverse("core:owner_dashboard")},
        )
        self.assertRedirects(res_approve, reverse("core:owner_dashboard"))
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.APPROVED)

        # Staff 1-click Start Service and Complete from Staff Dashboard
        self.client.force_login(self.staff)
        res_start = self.client.post(
            reverse("bookings:quick_status", kwargs={"reference": appt.reference}),
            {"status": Appointment.Status.ONGOING, "next": reverse("core:staff_dashboard")},
        )
        self.assertRedirects(res_start, reverse("core:staff_dashboard"))
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.ONGOING)

        res_done = self.client.post(
            reverse("bookings:quick_status", kwargs={"reference": appt.reference}),
            {"status": Appointment.Status.COMPLETED, "next": reverse("core:staff_dashboard")},
        )
        self.assertRedirects(res_done, reverse("core:staff_dashboard"))
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.COMPLETED)
