from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Appointment, AppointmentService
from apps.customers.models import Customer
from apps.services.models import Service, ServiceCategory, StaffProfile

from .models import CashierShift, Payment, PromoVoucher, ReceiptSequence, Sale, SaleItem
from .services import create_sale, void_sale


PASSWORD = "Correct-Horse-Battery-47!"


@override_settings(MFA_ENFORCE_OWNER=False, MFA_REQUIRE_INTERNAL_USERS=False)
class PosTransactionTests(TestCase):
    def setUp(self):
        self.owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.cashier = self.create_user("cashier@example.com", User.Role.CASHIER)
        self.staff = self.create_user("staff@example.com", User.Role.STAFF)
        self.denied_staff = self.create_user("denied@example.com", User.Role.STAFF)
        StaffProfile.objects.create(user=self.staff)
        StaffProfile.objects.create(user=self.denied_staff)
        category = ServiceCategory.objects.create(name="POS Services")
        self.manicure = Service.objects.create(
            category=category,
            name="Manicure",
            duration_minutes=45,
            price=Decimal("1000.00"),
        )
        self.pedicure = Service.objects.create(
            category=category,
            name="Pedicure",
            duration_minutes=60,
            price=Decimal("500.00"),
        )
        self.customer = Customer.objects.create(
            first_name="Test",
            last_name="Customer",
            email="customer@example.com",
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

    def items(self):
        return [
            {"service": self.manicure, "assigned_staff": self.staff, "quantity": 2},
            {"service": self.pedicure, "assigned_staff": self.staff, "quantity": 1},
        ]

    def create_cash_sale(self, **overrides):
        data = {
            "cashier": self.cashier,
            "raw_items": self.items(),
            "customer": self.customer,
            "payment_method": Payment.Method.CASH,
            "amount_tendered": Decimal("3000.00"),
        }
        data.update(overrides)
        return create_sale(**data)

    def create_appointment(self):
        appointment = Appointment.objects.create(
            customer=self.customer,
            customer_name_snapshot=self.customer.full_name,
            customer_email_snapshot=self.customer.email,
            appointment_date=timezone.localdate() + timedelta(days=1),
            start_time=time(10, 0),
            end_time=time(10, 45),
            assigned_staff=self.staff,
            status=Appointment.Status.APPROVED,
            expires_at=timezone.now() + timedelta(days=1),
        )
        AppointmentService.objects.create(
            appointment=appointment,
            service=self.manicure,
            service_name=self.manicure.name,
            duration_minutes=self.manicure.duration_minutes,
            price=self.manicure.price,
        )
        return appointment

    def test_percentage_totals_change_and_historical_snapshots(self):
        sale = self.create_cash_sale(
            discount_type=Sale.DiscountType.PERCENT,
            discount_value=Decimal("10.00"),
        )

        self.assertEqual(sale.subtotal, Decimal("2500.00"))
        self.assertEqual(sale.discount_amount, Decimal("250.00"))
        self.assertEqual(sale.total, Decimal("2250.00"))
        self.assertEqual(sale.payment.amount_tendered, Decimal("3000.00"))
        self.assertEqual(sale.payment.change, Decimal("750.00"))

        self.manicure.name = "Renamed Manicure"
        self.manicure.price = Decimal("1200.00")
        self.manicure.save()
        item = sale.items.get(service=self.manicure)
        self.assertEqual(item.service_name, "Manicure")
        self.assertEqual(item.unit_price, Decimal("1000.00"))

    def test_fixed_discount_and_non_cash_exact_payment(self):
        sale = self.create_cash_sale(
            discount_type=Sale.DiscountType.FIXED,
            discount_value=Decimal("125.50"),
            payment_method=Payment.Method.GCASH,
            amount_tendered=Decimal("2374.50"),
            payment_reference="LOCAL-REF-1",
        )
        self.assertEqual(sale.total, Decimal("2374.50"))
        self.assertEqual(sale.payment.change, Decimal("0.00"))
        self.assertEqual(sale.payment.reference, "LOCAL-REF-1")

    def test_negative_or_excessive_discounts_and_insufficient_cash_are_rejected(self):
        invalid = (
            {
                "discount_type": Sale.DiscountType.FIXED,
                "discount_value": Decimal("3000.00"),
            },
            {
                "discount_type": Sale.DiscountType.PERCENT,
                "discount_value": Decimal("101.00"),
            },
            {"amount_tendered": Decimal("2499.99")},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.create_cash_sale(**values)
        self.assertFalse(Sale.objects.exists())
        self.assertEqual(ReceiptSequence.objects.get(pk=1).last_value, 0)

    def test_receipt_numbers_are_unique_and_immutable(self):
        first = self.create_cash_sale()
        second = self.create_cash_sale(customer=None)
        self.assertNotEqual(first.receipt_number, second.receipt_number)
        self.assertRegex(first.receipt_number, r"^GN-\d{8}-\d{8}$")

        first.receipt_number = "CHANGED"
        with self.assertRaisesMessage(ValidationError, "immutable"):
            first.save()

    def test_permission_is_rechecked_in_transaction_service(self):
        with self.assertRaises(PermissionDenied):
            self.create_cash_sale(cashier=self.denied_staff)
        granted = self.create_user(
            "granted@example.com", User.Role.STAFF, can_use_pos=True
        )
        sale = self.create_cash_sale(cashier=granted)
        self.assertEqual(sale.cashier, granted)

    def test_linked_appointment_is_completed_atomically(self):
        appointment = self.create_appointment()
        sale = self.create_cash_sale(appointment=appointment)
        appointment.refresh_from_db()
        self.assertEqual(sale.appointment, appointment)
        self.assertEqual(appointment.status, Appointment.Status.COMPLETED)
        self.assertTrue(
            appointment.status_history.filter(
                to_status=Appointment.Status.COMPLETED, actor=self.cashier
            ).exists()
        )

    def test_payment_failure_rolls_back_sale_sequence_items_and_appointment(self):
        appointment = self.create_appointment()
        with patch("apps.pos.services.Payment.objects.create", side_effect=RuntimeError("fail")):
            with self.assertRaises(RuntimeError):
                self.create_cash_sale(appointment=appointment)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.APPROVED)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(SaleItem.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(ReceiptSequence.objects.get(pk=1).last_value, 0)

    def test_database_rejects_negative_historical_totals(self):
        with self.assertRaises(IntegrityError):
            Sale.objects.create(
                receipt_number="GN-INVALID-00000001",
                subtotal=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                total=Decimal("-0.01"),
                cashier=self.cashier,
                cashier_name_snapshot=str(self.cashier),
            )

    def test_void_is_owner_only_and_preserves_financial_history(self):
        sale = self.create_cash_sale()
        with self.assertRaises(PermissionDenied):
            void_sale(sale, actor=self.cashier, reason="Mistake")
        void_sale(sale, actor=self.owner, reason="Duplicate transaction")
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.VOIDED)
        self.assertEqual(sale.items.count(), 2)
        self.assertTrue(hasattr(sale, "payment"))
        self.client.force_login(self.owner)
        summary = self.client.get(reverse("reports:daily_summary"))
        self.assertEqual(summary.context["totals"]["transactions"], 0)
        self.assertEqual(summary.context["totals"]["voided"], 1)

    def test_checkout_receipt_history_and_daily_summary_views(self):
        self.client.force_login(self.cashier)
        data = {
            "customer": "",
            "appointment": "",
            "discount_type": Sale.DiscountType.NONE,
            "discount_value": "0.00",
            "payment_method": Payment.Method.CASH,
            "amount_tendered": "1000.00",
            "payment_reference": "",
            "mark_appointment_completed": "on",
            "items-TOTAL_FORMS": "6",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "12",
            "items-0-service": str(self.manicure.pk),
            "items-0-assigned_staff": str(self.staff.pk),
            "items-0-quantity": "1",
        }
        for position in range(1, 6):
            data[f"items-{position}-service"] = ""
            data[f"items-{position}-assigned_staff"] = ""
            data[f"items-{position}-quantity"] = "1"
        response = self.client.post(reverse("pos:index"), data)
        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        self.assertRedirects(
            response,
            reverse("pos:receipt", kwargs={"receipt_number": sale.receipt_number}),
            fetch_redirect_response=False,
        )
        receipt = self.client.get(
            reverse("pos:receipt", kwargs={"receipt_number": sale.receipt_number})
        )
        self.assertContains(receipt, "Get Nailed")
        self.assertContains(receipt, f"Change due: ₱{sale.change:.2f}")
        self.assertContains(self.client.get(reverse("pos:history")), sale.receipt_number)
        summary = self.client.get(reverse("reports:daily_summary"))
        self.assertContains(summary, sale.receipt_number)
        self.assertContains(summary, "1000.00")

    def test_cashiers_cannot_read_each_others_receipts(self):
        sale = self.create_cash_sale()
        other = self.create_user("other-cashier@example.com", User.Role.CASHIER)
        self.client.force_login(other)
        response = self.client.get(
            reverse("pos:receipt", kwargs={"receipt_number": sale.receipt_number})
        )
        self.assertEqual(response.status_code, 404)
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(
                reverse("pos:receipt", kwargs={"receipt_number": sale.receipt_number})
            ).status_code,
            200,
        )

    def test_senior_citizen_and_pwd_discount_rules(self):
        with self.assertRaises(ValidationError):
            self.create_cash_sale(
                discount_type=Sale.DiscountType.SENIOR_CITIZEN,
                discount_value=Decimal("0.00"),
                amount_tendered=Decimal("3000.00"),
            )

        sale = self.create_cash_sale(
            discount_type=Sale.DiscountType.SENIOR_CITIZEN,
            discount_value=Decimal("0.00"),
            discount_id_number="OSCA-12345",
            discount_id_name="Juan Dela Cruz",
            amount_tendered=Decimal("2000.00"),
        )
        self.assertEqual(sale.discount_type, Sale.DiscountType.SENIOR_CITIZEN)
        self.assertEqual(sale.subtotal, Decimal("2500.00"))
        self.assertEqual(sale.discount_amount, Decimal("500.00"))
        self.assertEqual(sale.total, Decimal("2000.00"))
        self.assertEqual(sale.discount_id_number, "OSCA-12345")

        pwd_sale = self.create_cash_sale(
            discount_type=Sale.DiscountType.PWD,
            discount_value=Decimal("0.00"),
            discount_id_number="PWD-67890",
            discount_id_name="Maria Clara",
            amount_tendered=Decimal("2000.00"),
        )
        self.assertEqual(pwd_sale.discount_type, Sale.DiscountType.PWD)
        self.assertEqual(pwd_sale.discount_amount, Decimal("500.00"))
        self.assertEqual(pwd_sale.total, Decimal("2000.00"))

    def test_promo_voucher_with_cap_and_min_spend(self):
        voucher = PromoVoucher.objects.create(
            code="SAVEBIG",
            discount_type=PromoVoucher.DiscountType.PERCENT,
            discount_value=Decimal("20.00"),
            max_discount_cap=Decimal("200.00"),
            min_spend=Decimal("1000.00"),
            usage_limit=10,
        )

        sale = self.create_cash_sale(
            discount_type=Sale.DiscountType.PROMO_VOUCHER,
            voucher_code="SAVEBIG",
            amount_tendered=Decimal("2500.00"),
        )
        self.assertEqual(sale.discount_amount, Decimal("200.00"))
        self.assertEqual(sale.total, Decimal("2300.00"))
        voucher.refresh_from_db()
        self.assertEqual(voucher.times_used, 1)

    def test_cashier_shift_till_reconciliation(self):
        shift = CashierShift.objects.create(
            cashier=self.cashier,
            opening_cash=Decimal("1500.00"),
            status=CashierShift.Status.OPEN,
        )

        sale = self.create_cash_sale()
        self.assertEqual(sale.shift, shift)

        shift.reconcile(Decimal("4000.00"), closed_by=self.cashier)
        self.assertEqual(shift.status, CashierShift.Status.CLOSED)
        self.assertEqual(shift.expected_cash, Decimal("4000.00"))
        self.assertEqual(shift.closing_cash, Decimal("4000.00"))
        self.assertEqual(shift.cash_variance, Decimal("0.00"))

        short_shift = CashierShift.objects.create(
            cashier=self.cashier,
            opening_cash=Decimal("1000.00"),
            status=CashierShift.Status.OPEN,
        )
        self.create_cash_sale()
        short_shift.reconcile(Decimal("3450.00"), closed_by=self.cashier)
        self.assertEqual(short_shift.expected_cash, Decimal("3500.00"))
        self.assertEqual(short_shift.closing_cash, Decimal("3450.00"))
        self.assertEqual(short_shift.cash_variance, Decimal("-50.00"))

    def test_api_appointment_details_and_service_time(self):
        appointment = self.create_appointment()
        self.client.force_login(self.cashier)
        response = self.client.get(
            reverse("pos:api_appointment_details", kwargs={"pk": appointment.pk})
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["appointment_id"], appointment.pk)
        self.assertEqual(data["customer_id"], str(self.customer.pk))
        self.assertEqual(data["start_time"], "10:00")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["service_id"], str(self.manicure.pk))
        self.assertEqual(data["items"][0]["service_time"], "10:00")

    def test_checkout_with_service_time(self):
        appointment = self.create_appointment()
        self.client.force_login(self.cashier)
        data = {
            "customer": str(self.customer.pk),
            "appointment": str(appointment.pk),
            "discount_type": Sale.DiscountType.NONE,
            "discount_value": "0.00",
            "payment_method": Payment.Method.CASH,
            "amount_tendered": "1000.00",
            "payment_reference": "",
            "mark_appointment_completed": "on",
            "items-TOTAL_FORMS": "6",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "12",
            "items-0-service": str(self.manicure.pk),
            "items-0-assigned_staff": str(self.staff.pk),
            "items-0-service_time": "10:00",
            "items-0-quantity": "1",
        }
        for position in range(1, 6):
            data[f"items-{position}-service"] = ""
            data[f"items-{position}-assigned_staff"] = ""
            data[f"items-{position}-service_time"] = ""
            data[f"items-{position}-quantity"] = "1"
        response = self.client.post(reverse("pos:index"), data)
        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        self.assertEqual(sale.appointment, appointment)
        self.assertEqual(sale.items.count(), 1)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.COMPLETED)

    def test_checkout_with_gcash_payment(self):
        self.client.force_login(self.cashier)
        data = {
            "customer": str(self.customer.pk),
            "appointment": "",
            "discount_type": Sale.DiscountType.NONE,
            "discount_value": "0.00",
            "payment_method": Payment.Method.GCASH,
            "amount_tendered": "",
            "payment_reference": "GCASH-9876543210",
            "mark_appointment_completed": "on",
            "items-TOTAL_FORMS": "6",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "12",
            "items-0-service": str(self.manicure.pk),
            "items-0-assigned_staff": str(self.staff.pk),
            "items-0-service_time": "10:00",
            "items-0-quantity": "1",
        }
        for position in range(1, 6):
            data[f"items-{position}-service"] = ""
            data[f"items-{position}-assigned_staff"] = ""
            data[f"items-{position}-service_time"] = ""
            data[f"items-{position}-quantity"] = "1"
        response = self.client.post(reverse("pos:index"), data)
        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.latest("pk")
        self.assertEqual(sale.payment.payment_method, Payment.Method.GCASH)
        self.assertEqual(sale.payment.reference, "GCASH-9876543210")
        self.assertEqual(sale.payment.amount_tendered, sale.total)
        self.assertEqual(sale.payment.change, Decimal("0.00"))

    def test_checkout_with_maya_payment(self):
        self.client.force_login(self.cashier)
        data = {
            "customer": str(self.customer.pk),
            "appointment": "",
            "discount_type": Sale.DiscountType.NONE,
            "discount_value": "0.00",
            "payment_method": Payment.Method.MAYA,
            "amount_tendered": "",
            "payment_reference": "MAYA-1122334455",
            "mark_appointment_completed": "on",
            "items-TOTAL_FORMS": "6",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "12",
            "items-0-service": str(self.manicure.pk),
            "items-0-assigned_staff": str(self.staff.pk),
            "items-0-service_time": "10:00",
            "items-0-quantity": "1",
        }
        for position in range(1, 6):
            data[f"items-{position}-service"] = ""
            data[f"items-{position}-assigned_staff"] = ""
            data[f"items-{position}-service_time"] = ""
            data[f"items-{position}-quantity"] = "1"
        response = self.client.post(reverse("pos:index"), data)
        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.latest("pk")
        self.assertEqual(sale.payment.payment_method, Payment.Method.MAYA)
        self.assertEqual(sale.payment.reference, "MAYA-1122334455")
        self.assertEqual(sale.payment.amount_tendered, sale.total)
        self.assertEqual(sale.payment.change, Decimal("0.00"))

    def test_checkout_rejects_removed_card_and_bank_methods(self):
        self.client.force_login(self.cashier)
        for invalid_method in ("CARD", "BANK"):
            data = {
                "customer": str(self.customer.pk),
                "appointment": "",
                "discount_type": Sale.DiscountType.NONE,
                "discount_value": "0.00",
                "payment_method": invalid_method,
                "amount_tendered": "1000.00",
                "items-TOTAL_FORMS": "6",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "12",
                "items-0-service": str(self.manicure.pk),
                "items-0-assigned_staff": str(self.staff.pk),
                "items-0-quantity": "1",
            }
            for position in range(1, 6):
                data[f"items-{position}-service"] = ""
                data[f"items-{position}-assigned_staff"] = ""
                data[f"items-{position}-quantity"] = "1"
            response = self.client.post(reverse("pos:index"), data)
            self.assertEqual(response.status_code, 200)
            self.assertFormError(response.context["form"], "payment_method", f"Select a valid choice. {invalid_method} is not one of the available choices.")

    def test_pos_history_time_period_filter_and_admin(self):
        self.owner.is_staff = True
        self.owner.save()
        self.client.force_login(self.owner)
        now = timezone.now()
        yesterday_dt = now - timedelta(days=1)

        sale1 = Sale.objects.create(
            receipt_number="TEST-001",
            cashier=self.cashier,
            cashier_name_snapshot="Cashier One",
            customer=self.customer,
            customer_name_snapshot="Test Customer",
            subtotal=Decimal("1000.00"),
            total=Decimal("1000.00"),
            status=Sale.Status.COMPLETED,
        )
        Sale.objects.filter(pk=sale1.pk).update(created_at=now)

        sale2 = Sale.objects.create(
            receipt_number="TEST-002",
            cashier=self.cashier,
            cashier_name_snapshot="Cashier One",
            customer=self.customer,
            customer_name_snapshot="Test Customer",
            subtotal=Decimal("500.00"),
            total=Decimal("500.00"),
            status=Sale.Status.COMPLETED,
        )
        Sale.objects.filter(pk=sale2.pk).update(created_at=yesterday_dt)

        # Filter today
        resp = self.client.get(reverse("pos:history"), {"period": "today"})
        self.assertEqual(resp.status_code, 200)
        page_sales = list(resp.context["page"])
        self.assertIn(sale1, page_sales)
        self.assertNotIn(sale2, page_sales)

        # Filter by specific time range
        resp_time = self.client.get(
            reverse("pos:history"),
            {"period": "today", "start_time": "00:00", "end_time": "23:59"},
        )
        self.assertEqual(resp_time.status_code, 200)
        self.assertIn(sale1, list(resp_time.context["page"]))

        # Filter yesterday
        resp = self.client.get(reverse("pos:history"), {"period": "yesterday"})
        self.assertEqual(resp.status_code, 200)
        page_sales = list(resp.context["page"])
        self.assertIn(sale2, page_sales)
        self.assertNotIn(sale1, page_sales)

        # Test admin sale list and time period filter
        admin_resp = self.client.get(reverse("admin:pos_sale_changelist"), {"time_period": "today"})
        self.assertEqual(admin_resp.status_code, 200)
        self.assertContains(admin_resp, "TEST-001")

    def test_api_technician_slots_endpoint(self):
        from datetime import time
        from apps.services.models import StaffSchedule, StaffProfile
        self.client.force_login(self.cashier)

        # Missing staff_id
        res = self.client.get(reverse("pos:api_technician_slots"))
        self.assertEqual(res.status_code, 400)

        # Working staff slots
        today = timezone.localdate()
        StaffSchedule.objects.update_or_create(
            staff=self.staff,
            day_of_week=today.weekday(),
            defaults={
                "is_working": True,
                "start_time": time(9, 0),
                "end_time": time(18, 0),
                "lunch_start": time(12, 0),
                "lunch_end": time(13, 0),
            },
        )
        res = self.client.get(
            reverse("pos:api_technician_slots"),
            {"staff_id": self.staff.pk, "service_id": self.manicure.pk},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertGreater(data["slots_count"], 0)
        self.assertIn("09:00", [s["time"] for s in data["slots"]])
        # 12:00 lunch slot should NOT be in available slots
        self.assertNotIn("12:00", [s["time"] for s in data["slots"]])

        # Staff off duty
        StaffSchedule.objects.filter(staff=self.staff, day_of_week=today.weekday()).update(is_working=False)
        res_off = self.client.get(
            reverse("pos:api_technician_slots"),
            {"staff_id": self.staff.pk},
        )
        self.assertEqual(res_off.status_code, 200)
        data_off = res_off.json()
        self.assertEqual(data_off["status"], "off_duty")

    def test_walk_in_sale_creates_monitoring_and_time_blocks(self):
        from datetime import time
        from apps.monitoring.services import visible_work_items
        from apps.services.models import StaffTimeBlock

        # Process a walk-in sale with assigned staff and time
        sale = create_sale(
            cashier=self.cashier,
            raw_items=[
                {
                    "service": self.manicure,
                    "assigned_staff": self.staff,
                    "service_time": time(14, 0),
                    "quantity": 1,
                }
            ],
            payment_method=Payment.Method.CASH,
            amount_tendered=Decimal("1000.00"),
            mark_appointment_completed=True,
        )

        # 1. Appointment was created automatically for the walk-in
        self.assertIsNotNone(sale.appointment)
        appt = sale.appointment
        self.assertEqual(appt.booking_source, Appointment.BookingSource.WALK_IN)
        self.assertEqual(appt.status, Appointment.Status.COMPLETED)

        # 2. Service Monitoring entries created
        work_items = list(visible_work_items(self.cashier).filter(appointment=appt))
        self.assertEqual(len(work_items), 1)
        self.assertEqual(work_items[0].service, self.manicure)
        self.assertEqual(work_items[0].assigned_staff, self.staff)

        # 3. StaffTimeBlock created automatically for the chosen technician
        time_blocks = list(StaffTimeBlock.objects.filter(staff=self.staff, date=appt.appointment_date))
        self.assertTrue(any(tb.start_time == time(14, 0) for tb in time_blocks))

    def test_pos_context_includes_technician_skills_and_same_tab_walk_in(self):
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("pos:index"))
        self.assertEqual(res.status_code, 200)

        # 1. Available Technicians link should open in same tab (no target="_blank")
        self.assertContains(res, reverse("services:walk_in_technicians"))
        self.assertNotContains(res, f'href="{reverse("services:walk_in_technicians")}" target="_blank"')

        # 2. Context has staff_qualified_services and all_services_catalog
        self.assertIn("staff_qualified_services", res.context)
        self.assertIn("all_services_catalog", res.context)

        # 3. JSON scripts are rendered in HTML
        self.assertContains(res, 'id="staff-qualified-services-data"')
        self.assertContains(res, 'id="all-services-catalog-data"')

        # 4. Level-aligned badges and elements exist
        self.assertContains(res, 'pos-service-status-badge')
        self.assertContains(res, 'pos-staff-status-badge')
        self.assertContains(res, 'pos-time-hint')

        # 5. Technician skills filtering
        self.staff.staff_profile.skills.set([self.manicure])
        res_filtered = self.client.get(reverse("pos:index"))
        staff_services = res_filtered.context["staff_qualified_services"]
        self.assertIn(str(self.manicure.pk), staff_services[str(self.staff.pk)])
        self.assertNotIn(str(self.pedicure.pk), staff_services[str(self.staff.pk)])

    def test_pos_customer_modes_context_and_catalogs(self):
        self.client.force_login(self.cashier)

        # Default mode is walkin
        res = self.client.get(reverse("pos:index"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context["initial_mode"], "walkin")
        self.assertIn("customers_catalog", res.context)
        self.assertIn("eligible_appointments", res.context)
        self.assertContains(res, 'id="customers-catalog-data"')
        self.assertContains(res, 'id="eligible-appointments-data"')
        self.assertContains(res, 'id="panel-walkin"')
        self.assertContains(res, 'id="panel-booked"')
        self.assertContains(res, 'id="walkin-find-results-list"')
        self.assertContains(res, 'id="walkin-ongoing-list"')

        # Query param ?mode=booked
        res_booked = self.client.get(reverse("pos:index") + "?mode=booked")
        self.assertEqual(res_booked.status_code, 200)
        self.assertEqual(res_booked.context["initial_mode"], "booked")

    def test_walk_in_custom_customer_name_persisted(self):
        self.client.force_login(self.cashier)
        sale = create_sale(
            cashier=self.cashier,
            raw_items=[{"service": self.manicure, "quantity": 1, "assigned_staff": self.staff}],
            payment_method=Payment.Method.CASH,
            walk_in_customer_name="Maria Santos (Guest)",
            walk_in_referral_number="WLK-20261029-9999",
        )
        self.assertEqual(sale.customer_name_snapshot, "Maria Santos (Guest) (WLK-20261029-9999)")
        self.assertIsNone(sale.customer)
        self.assertEqual(sale.appointment.booking_source, Appointment.BookingSource.WALK_IN)

    def test_api_quick_create_customer(self):
        from apps.customers.models import Customer
        self.client.force_login(self.cashier)

        # 1. Successful customer creation
        res = self.client.post(
            reverse("pos:api_quick_create_customer"),
            {"name": "Ana Santos", "phone": "09181234567"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["created"])
        self.assertEqual(data["customer"]["name"], "Ana Santos")

        # Verify saved in database
        cust = Customer.objects.filter(phone="09181234567").first()
        self.assertIsNotNone(cust)
        self.assertEqual(cust.first_name, "Ana")
        self.assertEqual(cust.last_name, "Santos")

        # 2. Duplicate phone returns existing customer without crash
        res_dup = self.client.post(
            reverse("pos:api_quick_create_customer"),
            {"name": "Ana Santos", "phone": "09181234567"},
        )
        self.assertEqual(res_dup.status_code, 200)
        data_dup = res_dup.json()
        self.assertEqual(data_dup["status"], "ok")
        self.assertFalse(data_dup["created"])
        self.assertEqual(data_dup["customer"]["id"], cust.pk)

    def test_api_add_service_to_appointment(self):
        from datetime import date, time
        from apps.bookings.models import AppointmentService
        from apps.services.models import StaffTimeBlock

        self.client.force_login(self.cashier)
        today = timezone.localdate()

        appt = Appointment.objects.create(
            customer=self.customer,
            customer_name_snapshot=self.customer.full_name,
            customer_phone_snapshot=self.customer.phone,
            appointment_date=today,
            start_time=time(14, 0),
            end_time=time(14, 30),
            assigned_staff=self.staff,
            status=Appointment.Status.ONGOING,
            booking_source=Appointment.BookingSource.WALK_IN,
        )
        AppointmentService.objects.create(
            appointment=appt,
            service=self.manicure,
            service_name=self.manicure.name,
            duration_minutes=self.manicure.duration_minutes,
            price=self.manicure.price,
            assigned_staff=self.staff,
            status=AppointmentService.Status.ONGOING,
            position=1,
        )

        res = self.client.post(
            reverse("pos:api_add_service_to_appointment", kwargs={"pk": appt.pk}),
            {
                "service_id": str(self.pedicure.pk),
                "staff_id": str(self.staff.pk),
                "service_time": "15:00",
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("Matagumpay na naidagdag", data["message"])

        # Check AppointmentService was added
        services = list(appt.appointment_services.all().order_by("position"))
        self.assertEqual(len(services), 2)
        added_svc = services[1]
        self.assertEqual(added_svc.service, self.pedicure)
        self.assertEqual(added_svc.status, AppointmentService.Status.ONGOING)

        # Check StaffTimeBlock was created for the technician
        tbs = list(StaffTimeBlock.objects.filter(staff=self.staff, date=today, start_time=time(15, 0)))
        self.assertEqual(len(tbs), 1)

    def test_pos_add_on_sale_for_ongoing_appointment_with_prior_sale(self):
        from datetime import time
        from apps.bookings.models import AppointmentService
        from apps.services.models import StaffTimeBlock

        today = timezone.localdate()
        appt = Appointment.objects.create(
            customer=self.customer,
            customer_name_snapshot=self.customer.full_name,
            customer_phone_snapshot=self.customer.phone,
            appointment_date=today,
            start_time=time(10, 0),
            end_time=time(10, 45),
            assigned_staff=self.staff,
            status=Appointment.Status.ONGOING,
            booking_source=Appointment.BookingSource.WALK_IN,
        )
        AppointmentService.objects.create(
            appointment=appt,
            service=self.manicure,
            service_name=self.manicure.name,
            duration_minutes=self.manicure.duration_minutes,
            price=self.manicure.price,
            assigned_staff=self.staff,
            status=AppointmentService.Status.ONGOING,
            position=1,
        )

        # First initial sale
        sale1 = create_sale(
            cashier=self.cashier,
            appointment=appt,
            raw_items=[{"service": self.manicure, "assigned_staff": self.staff, "service_time": time(10, 0), "quantity": 1}],
            payment_method=Payment.Method.CASH,
            amount_tendered=Decimal("1000.00"),
        )
        self.assertEqual(sale1.appointment, appt)

        # Second add-on sale while in ongoing session (adds pedicure)
        sale2 = create_sale(
            cashier=self.cashier,
            appointment=appt,
            raw_items=[{"service": self.pedicure, "assigned_staff": self.staff, "service_time": time(11, 0), "quantity": 1}],
            payment_method=Payment.Method.CASH,
            amount_tendered=Decimal("500.00"),
        )
        self.assertEqual(sale2.status, Sale.Status.COMPLETED)
        # Avoids unique OneToOne constraint violation on appointment
        self.assertIsNone(sale2.appointment)
        self.assertEqual(sale2.customer, self.customer)

        # Pedicure was appended to the appointment's services
        services = list(appt.appointment_services.all().order_by("position"))
        self.assertEqual(len(services), 2)
        self.assertEqual(services[1].service, self.pedicure)

        # Time block was created for the technician
        tbs = list(StaffTimeBlock.objects.filter(staff=self.staff, date=today, start_time=time(11, 0)))
        self.assertTrue(len(tbs) >= 1)
