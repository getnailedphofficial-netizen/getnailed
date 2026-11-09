from datetime import time, timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Appointment, AppointmentService
from apps.customers.models import Customer
from apps.notifications.models import Notification
from apps.pos.models import Payment, Sale
from apps.pos.services import create_sale, void_sale
from apps.services.models import Service, ServiceCategory, StaffProfile

from .services import (
    build_report,
    cashier_dashboard_data,
    owner_dashboard_data,
    staff_dashboard_data,
)


PASSWORD = "Correct-Horse-Battery-47!"


@override_settings(MFA_ENFORCE_OWNER=False, MFA_REQUIRE_INTERNAL_USERS=False)
class ReportsAndDashboardTests(TestCase):
    def setUp(self):
        self.owner = self.create_user("owner@example.com", User.Role.OWNER)
        self.cashier = self.create_user("cashier@example.com", User.Role.CASHIER)
        self.other_cashier = self.create_user("other-cashier@example.com", User.Role.CASHIER)
        self.staff = self.create_user("staff@example.com", User.Role.STAFF)
        self.customer_user = self.create_user("login-customer@example.com", User.Role.CUSTOMER)
        StaffProfile.objects.create(user=self.staff)
        category = ServiceCategory.objects.create(name="Reports")
        self.service = Service.objects.create(
            category=category,
            name="Report manicure",
            duration_minutes=45,
            price=Decimal("100.00"),
        )
        self.customer = Customer.objects.create(
            first_name="Report",
            last_name="Customer",
            email="report-customer@example.com",
        )
        self.appointment = Appointment.objects.create(
            customer=self.customer,
            customer_name_snapshot=self.customer.full_name,
            customer_email_snapshot=self.customer.email,
            appointment_date=timezone.localdate(),
            start_time=time(10, 0),
            end_time=time(10, 45),
            assigned_staff=self.staff,
            status=Appointment.Status.ONGOING,
            expires_at=timezone.now() + timedelta(hours=1),
            verified_at=timezone.now(),
        )
        self.work = AppointmentService.objects.create(
            appointment=self.appointment,
            service=self.service,
            service_name=self.service.name,
            duration_minutes=self.service.duration_minutes,
            price=self.service.price,
            assigned_staff=self.staff,
            status=AppointmentService.Status.COMPLETED,
            completed_at=timezone.now(),
        )
        self.completed_sale = self.make_sale(self.cashier, Decimal("100.00"))
        self.voided_sale = self.make_sale(self.cashier, Decimal("100.00"))
        void_sale(self.voided_sale, actor=self.owner, reason="Test void")
        self.other_sale = self.make_sale(self.other_cashier, Decimal("100.00"))

    def create_user(self, email, role):
        return User.objects.create_user(
            email,
            PASSWORD,
            role=role,
            email_verified_at=timezone.now(),
            is_active=True,
            is_active_staff_member=True,
        )

    def make_sale(self, cashier, tendered):
        return create_sale(
            cashier=cashier,
            raw_items=[
                {"service": self.service, "assigned_staff": self.staff, "quantity": 1}
            ],
            customer=self.customer,
            payment_method=Payment.Method.CASH,
            amount_tendered=tendered,
        )

    def test_all_sales_period_reports_exclude_voids(self):
        today = timezone.localdate()
        for report_type in ("daily", "weekly", "monthly", "annual"):
            with self.subTest(report_type=report_type):
                report = build_report(report_type, today, today)
                self.assertEqual(report.totals["transactions"], 2)
                self.assertEqual(report.totals["gross"], Decimal("200.00"))
                self.assertEqual(report.totals["net"], Decimal("200.00"))
                self.assertEqual(sum(row["Transactions"] for row in report.rows), 2)

    def test_service_sales_uses_only_completed_nonvoided_sale_items(self):
        today = timezone.localdate()
        report = build_report("service-sales", today, today)
        self.assertEqual(len(report.rows), 1)
        self.assertEqual(report.rows[0]["Quantity"], 2)
        self.assertEqual(report.rows[0]["Gross Service Sales"], Decimal("200.00"))

    def test_operational_reports_count_appointment_status_and_staff_work(self):
        today = timezone.localdate()
        status_report = build_report("appointment-status", today, today)
        self.assertEqual(status_report.totals["appointments"], 4)
        status_map = {row["Status"]: row["Appointments"] for row in status_report.rows}
        self.assertEqual(status_map.get("Ongoing"), 1)
        self.assertEqual(status_map.get("Completed"), 3)
        workload = build_report("staff-workload", today, today)
        self.assertEqual(workload.rows[0]["Staff"], str(self.staff))
        self.assertEqual(workload.rows[0]["Completed Services"], 4)

    def test_owner_dashboard_contains_live_kpis_rankings_and_trend(self):
        data = owner_dashboard_data()
        self.assertEqual(data["today_sales"]["transactions"], 2)
        self.assertEqual(data["today_sales"]["net"], Decimal("200.00"))
        self.assertEqual(data["customer_count"], 1)
        self.assertEqual(data["appointment_count"], 4)
        self.assertEqual(data["top_services"][0]["quantity"], 2)
        self.assertEqual(data["top_staff"][0].completed_services, 4)
        self.assertEqual(data["today_avg_ticket"], Decimal("100.00"))
        self.assertEqual(data["month_sales"]["net"], Decimal("200.00"))
        self.assertEqual(data["month_avg_ticket"], Decimal("100.00"))
        self.assertEqual(data["today_appointments_count"], 4)
        self.assertEqual(data["pending_appointments_count"], 0)
        self.assertEqual(data["active_staff_count"], 1)
        self.assertEqual(data["staff_on_duty_today"], 1)
        self.assertEqual(data["new_customers_this_month"], 1)

        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:owner_dashboard"))
        self.assertContains(response, "₱200.00")
        self.assertContains(response, "This month's sales")
        self.assertContains(response, "Staff on duty")
        self.assertNotContains(response, "Connect this card")
        self.assertNotContains(response, "Sales trend")
        self.assertContains(response, "Sales by Category")
        self.assertContains(response, "Payment Method Breakdown")
        self.assertContains(response, "Peak Rush Hours")
        self.assertContains(response, "kpi-charts-payload")

    def test_owner_dashboard_kpi_period_filter_and_pending_alert(self):
        Appointment.objects.create(
            customer=self.customer,
            customer_name_snapshot=self.customer.full_name,
            customer_email_snapshot=self.customer.email,
            appointment_date=timezone.localdate(),
            start_time=time(14, 0),
            end_time=time(14, 45),
            status=Appointment.Status.PENDING,
            expires_at=timezone.now() + timedelta(hours=2),
            verified_at=timezone.now(),
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:owner_dashboard"), {"period": "month"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Action Required")
        self.assertContains(response, "pending appointment")
        self.assertContains(response, "Filtered View")

    def test_owner_dashboard_kpi_charts_breakdown_and_peak_hours(self):
        cat2 = ServiceCategory.objects.create(name="Nails & Spa")
        service2 = Service.objects.create(
            category=cat2,
            name="Spa Deluxe",
            duration_minutes=60,
            price=Decimal("350.00"),
        )
        create_sale(
            cashier=self.cashier,
            raw_items=[{"service": service2, "assigned_staff": self.staff, "quantity": 1}],
            customer=self.customer,
            payment_method=Payment.Method.GCASH,
            amount_tendered=Decimal("350.00"),
        )
        create_sale(
            cashier=self.cashier,
            raw_items=[{"service": self.service, "assigned_staff": self.staff, "quantity": 2}],
            customer=self.customer,
            payment_method=Payment.Method.MAYA,
            amount_tendered=Decimal("200.00"),
        )

        data = owner_dashboard_data("today")
        self.assertEqual(data["payment_totals"]["cash"], Decimal("200.00"))
        self.assertEqual(data["payment_totals"]["gcash"], Decimal("350.00"))
        self.assertEqual(data["payment_totals"]["maya"], Decimal("200.00"))

        payload = data["kpi_charts_payload"]
        self.assertTrue(payload["category"]["has_data"])
        self.assertTrue(payload["payment"]["has_data"])
        self.assertTrue(payload["peak_hours"]["has_data"])

        cat_names = [c["name"] for c in data["category_sales_list"]]
        self.assertIn("Reports", cat_names)
        self.assertIn("Nails & Spa", cat_names)

        self.assertIn("Cash", payload["payment"]["labels"])
        self.assertIn("GCash", payload["payment"]["labels"])
        self.assertIn("Maya", payload["payment"]["labels"])

        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:owner_dashboard"))
        self.assertContains(response, "₱350.00")
        self.assertContains(response, "GCash")
        self.assertContains(response, "Maya")
        self.assertContains(response, "Sales by Category")
        self.assertContains(response, "Payment Method Breakdown")
        self.assertContains(response, "Peak Rush Hours")

    def test_cashier_dashboard_is_scoped_to_that_cashier(self):
        data = cashier_dashboard_data(self.cashier)
        self.assertEqual(data["pos_totals"]["transactions"], 1)
        self.assertEqual(data["pos_totals"]["net"], Decimal("100.00"))
        self.assertEqual(data["bookings_count"], 4)
        self.client.force_login(self.cashier)
        response = self.client.get(reverse("core:staff_dashboard"))
        self.assertTemplateUsed(response, "dashboards/cashier.html")
        self.assertContains(response, self.completed_sale.receipt_number)
        self.assertNotContains(response, self.other_sale.receipt_number)

    def test_staff_dashboard_has_assigned_work_and_notifications(self):
        Notification.objects.create(
            recipient=self.staff,
            appointment=self.appointment,
            event_type=Notification.EventType.NEW,
            title="Assigned work",
            message="A service is ready.",
        )
        data = staff_dashboard_data(self.staff)
        self.assertEqual(data["assigned_count"], 4)
        self.assertEqual(data["completed_count"], 4)
        self.assertEqual(data["unread_notifications"], 1)
        self.client.force_login(self.staff)
        response = self.client.get(reverse("core:staff_dashboard"))
        self.assertContains(response, self.service.name)
        self.assertContains(response, "Assigned work")

    def test_owner_report_pages_accept_date_filters(self):
        self.client.force_login(self.owner)
        params = {
            "start_date": timezone.localdate().isoformat(),
            "end_date": timezone.localdate().isoformat(),
        }
        for url_name in (
            "reports:daily",
            "reports:weekly",
            "reports:monthly",
            "reports:annual",
            "reports:service_sales",
            "reports:appointment_status",
            "reports:staff_workload",
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name), params)
                self.assertEqual(response.status_code, 200)
                self.assertIsNotNone(response.context["report"])

    def test_csv_xlsx_and_branded_pdf_exports(self):
        self.client.force_login(self.owner)
        params = {
            "start_date": timezone.localdate().isoformat(),
            "end_date": timezone.localdate().isoformat(),
        }
        expected = {
            "csv": ("text/csv", b"Period"),
            "xlsx": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"PK",
            ),
            "pdf": ("application/pdf", b"%PDF"),
        }
        for export_format, (content_type, signature) in expected.items():
            with self.subTest(export_format=export_format):
                response = self.client.get(
                    reverse(
                        "reports:export",
                        kwargs={"report_type": "daily", "export_format": export_format},
                    ),
                    params,
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response["Content-Type"].startswith(content_type))
                self.assertIn(signature, response.content)
                self.assertIn("attachment", response["Content-Disposition"])

    def test_report_and_export_routes_are_owner_only(self):
        protected = (
            "reports:index",
            "reports:daily",
            "reports:weekly",
            "reports:monthly",
            "reports:annual",
            "reports:service_sales",
            "reports:appointment_status",
            "reports:staff_workload",
            "reports:top_services",
            "reports:top_staff",
        )
        for user in (self.cashier, self.staff, self.customer_user):
            self.client.force_login(user)
            for url_name in protected:
                with self.subTest(user=user.role, url_name=url_name):
                    self.assertEqual(self.client.get(reverse(url_name)).status_code, 403)
            self.assertEqual(
                self.client.get(
                    reverse(
                        "reports:export",
                        kwargs={"report_type": "daily", "export_format": "csv"},
                    )
                ).status_code,
                403,
            )

    def test_top_services_and_top_staff_reports(self):
        today = timezone.localdate()
        services_report = build_report("top-services", today, today)
        self.assertEqual(services_report.key, "top-services")
        self.assertIn("Total Bookings", services_report.columns)
        self.assertGreaterEqual(len(services_report.rows), 1)
        self.assertEqual(services_report.rows[0]["Service"], self.service.name)

        staff_report = build_report("top-staff", today, today)
        self.assertEqual(staff_report.key, "top-staff")
        self.assertIn("Completion Rate", staff_report.columns)
        self.assertGreaterEqual(len(staff_report.rows), 1)

    def test_sort_report_rows_and_presets(self):
        from .services import sort_report_rows
        from .forms import ReportDateRangeForm
        today = timezone.localdate()

        rows = [
            {"Name": "A", "Count": 10},
            {"Name": "B", "Count": 25},
            {"Name": "C", "Count": 5},
        ]
        sorted_desc = sort_report_rows(rows, "Count", "desc")
        self.assertEqual(sorted_desc[0]["Name"], "B")
        self.assertEqual(sorted_desc[2]["Name"], "C")

        form = ReportDateRangeForm({"preset": "week", "start_date": today, "end_date": today}, report_type="daily")
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["start_date"], today - timedelta(days=6))
        self.assertEqual(form.cleaned_data["end_date"], today)

    def test_owner_can_view_top_reports_and_export(self):
        self.client.force_login(self.owner)
        for url_name in ("reports:top_services", "reports:top_staff"):
            resp = self.client.get(reverse(url_name))
            self.assertEqual(resp.status_code, 200)

        export_resp = self.client.get(
            reverse("reports:export", kwargs={"report_type": "top-services", "export_format": "csv"})
        )
        self.assertEqual(export_resp.status_code, 200)
        self.assertTrue(export_resp["Content-Type"].startswith("text/csv"))

