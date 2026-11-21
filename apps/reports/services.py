from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDay, TruncMonth, TruncWeek, TruncYear
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Appointment, AppointmentService
from apps.customers.models import Customer
from apps.notifications.models import Notification
from apps.pos.models import CashierShift, Payment, Sale, SaleItem


ZERO = Decimal("0.00")
VISIBLE_APPOINTMENT_STATUSES = tuple(
    value
    for value, _ in Appointment.Status.choices
    if value not in (Appointment.Status.UNVERIFIED, Appointment.Status.EXPIRED)
)
WORKLOAD_APPOINTMENT_STATUSES = (
    Appointment.Status.PENDING,
    Appointment.Status.APPROVED,
    Appointment.Status.RESCHEDULED,
    Appointment.Status.ONGOING,
    Appointment.Status.COMPLETED,
)


@dataclass
class ReportResult:
    key: str
    title: str
    description: str
    columns: tuple
    rows: list
    totals: dict
    start_date: object
    end_date: object


def completed_sales(start_date=None, end_date=None):
    sales = Sale.objects.filter(status=Sale.Status.COMPLETED)
    if start_date is not None:
        sales = sales.filter(created_at__date__gte=start_date)
    if end_date is not None:
        sales = sales.filter(created_at__date__lte=end_date)
    return sales


def _financial_totals(sales):
    values = sales.aggregate(
        transactions=Count("pk"),
        gross=Sum("subtotal"),
        discounts=Sum("discount_amount"),
        net=Sum("total"),
    )
    for key in ("gross", "discounts", "net"):
        values[key] = values[key] or ZERO
    return values


def owner_dashboard_data(period="today"):
    today = timezone.localdate()
    today_sales_qs = completed_sales(today, today)
    today_sales = _financial_totals(today_sales_qs)
    today_avg_ticket = (
        (today_sales["net"] / Decimal(today_sales["transactions"]))
        if today_sales["transactions"] > 0
        else ZERO
    )

    # Month-to-date performance
    first_day_of_month = today.replace(day=1)
    month_sales_qs = completed_sales(first_day_of_month, today)
    month_sales = _financial_totals(month_sales_qs)
    month_avg_ticket = (
        (month_sales["net"] / Decimal(month_sales["transactions"]))
        if month_sales["transactions"] > 0
        else ZERO
    )

    # Selected period KPI calculations
    period = (period or "today").lower().strip()
    if period == "week":
        p_start = today - timedelta(days=6)
        p_end = today
        period_label = "Last 7 Days"
    elif period == "month":
        p_start = first_day_of_month
        p_end = today
        period_label = "This Month"
    elif period == "year":
        p_start = today.replace(month=1, day=1)
        p_end = today
        period_label = "This Year"
    elif period == "all":
        p_start = None
        p_end = None
        period_label = "All Time"
    else:
        period = "today"
        p_start = today
        p_end = today
        period_label = "Today"

    period_sales_qs = completed_sales(p_start, p_end)
    period_sales = _financial_totals(period_sales_qs)
    period_avg_ticket = (
        (period_sales["net"] / Decimal(period_sales["transactions"]))
        if period_sales["transactions"] > 0
        else ZERO
    )

    # Appointment operational KPIs
    today_appointments = Appointment.objects.filter(
        appointment_date=today, status__in=VISIBLE_APPOINTMENT_STATUSES
    )
    today_appointments_count = today_appointments.count()
    today_completed_appointments = today_appointments.filter(
        status=Appointment.Status.COMPLETED
    ).count()
    pending_appointments_count = Appointment.objects.filter(
        status=Appointment.Status.PENDING
    ).count()

    if p_start is not None and p_end is not None:
        period_appointments_count = Appointment.objects.filter(
            appointment_date__range=(p_start, p_end),
            status__in=VISIBLE_APPOINTMENT_STATUSES,
        ).count()
    elif p_start is not None:
        period_appointments_count = Appointment.objects.filter(
            appointment_date__gte=p_start,
            status__in=VISIBLE_APPOINTMENT_STATUSES,
        ).count()
    else:
        period_appointments_count = Appointment.objects.filter(
            status__in=VISIBLE_APPOINTMENT_STATUSES
        ).count()

    # Staff & Floor KPIs
    active_staff_count = User.objects.filter(
        role=User.Role.STAFF, is_active=True, is_active_staff_member=True
    ).count()
    staff_on_duty_today = (
        User.objects.filter(role=User.Role.STAFF)
        .filter(
            Q(assigned_service_work__appointment__appointment_date=today)
            | Q(shifts__opened_at__date=today, shifts__status=CashierShift.Status.OPEN)
        )
        .distinct()
        .count()
    )
    active_shifts_count = CashierShift.objects.filter(
        status=CashierShift.Status.OPEN
    ).count()

    # Sales trend (last 7 days)
    trend_start = today - timedelta(days=6)
    trend_values = {
        row["day"]: row["total"]
        for row in completed_sales(trend_start, today)
        .annotate(day=TruncDay("created_at"))
        .values("day")
        .annotate(total=Sum("total"))
    }
    sales_trend = []
    for offset in range(7):
        day = trend_start + timedelta(days=offset)
        aware_key = next(
            (key for key in trend_values if timezone.localtime(key).date() == day), None
        )
        sales_trend.append(
            {"date": day, "total": trend_values.get(aware_key, ZERO) or ZERO}
        )
    sales_trend_max = max((item["total"] for item in sales_trend), default=ZERO)
    for item in sales_trend:
        item["percent"] = (
            int(item["total"] / sales_trend_max * 100) if sales_trend_max else 0
        )

    # Top services by quantity & revenue
    top_services = list(
        SaleItem.objects.filter(sale__in=completed_sales())
        .values("service_name")
        .annotate(quantity=Sum("quantity"), revenue=Sum("line_total"))
        .order_by("-quantity", "-revenue", "service_name")[:5]
    )

    # Top staff by completed service count
    top_staff = list(
        User.objects.filter(role=User.Role.STAFF)
        .annotate(
            completed_services=Count(
                "assigned_service_work",
                filter=Q(
                    assigned_service_work__status=AppointmentService.Status.COMPLETED
                ),
            )
        )
        .filter(completed_services__gt=0)
        .order_by("-completed_services", "first_name", "last_name", "email")[:5]
    )

    visible_customers = Customer.objects.filter(
        Q(appointments__isnull=True)
        | Q(appointments__status__in=VISIBLE_APPOINTMENT_STATUSES)
    ).distinct()
    customer_count = visible_customers.count()
    new_customers_this_month = Customer.objects.filter(
        created_at__date__gte=first_day_of_month
    ).count()

    # 1. Sales by Category KPI (Revenue share per service category)
    category_palette = [
        "#00875a",  # Deep emerald
        "#0d9488",  # Teal
        "#0284c7",  # Blue
        "#d97706",  # Amber/Orange
        "#7c3aed",  # Purple
        "#db2777",  # Pink
        "#059669",  # Green
        "#4b5563",  # Slate
    ]

    raw_category_sales = list(
        SaleItem.objects.filter(sale__in=period_sales_qs)
        .values("service_category")
        .annotate(revenue=Sum("line_total"), quantity=Sum("quantity"))
        .order_by("-revenue", "service_category")
    )
    category_total_revenue = sum((row["revenue"] or ZERO for row in raw_category_sales), ZERO)

    category_sales_list = []
    for idx, row in enumerate(raw_category_sales):
        cat_name = row["service_category"] or "General"
        rev = row["revenue"] or ZERO
        pct = (
            round(float((rev / category_total_revenue) * 100), 1)
            if category_total_revenue > 0
            else 0.0
        )
        color = category_palette[idx % len(category_palette)]
        category_sales_list.append({
            "name": cat_name,
            "revenue": rev,
            "revenue_float": float(rev),
            "percentage": pct,
            "quantity": row["quantity"] or 0,
            "color": color,
        })

    category_chart_labels = [item["name"] for item in category_sales_list]
    category_chart_data = [item["revenue_float"] for item in category_sales_list]
    category_chart_colors = [item["color"] for item in category_sales_list]

    # 2. Payment Method Breakdown KPI (Cash vs GCash vs Maya collections)
    raw_payment_sales = list(
        Payment.objects.filter(sale__in=period_sales_qs)
        .values("payment_method")
        .annotate(revenue=Sum("sale__total"), transactions=Count("id"))
        .order_by("-revenue")
    )
    pm_revenue_map = {row["payment_method"]: (row["revenue"] or ZERO) for row in raw_payment_sales}

    payment_totals = {
        "cash": pm_revenue_map.get(Payment.Method.CASH, ZERO),
        "gcash": pm_revenue_map.get(Payment.Method.GCASH, ZERO),
        "maya": pm_revenue_map.get(Payment.Method.MAYA, ZERO),
        "card": pm_revenue_map.get(Payment.Method.CARD, ZERO),
        "bank": pm_revenue_map.get(Payment.Method.BANK, ZERO),
    }
    payment_total_revenue = sum(payment_totals.values(), ZERO)

    # Standard colors: Cash (#00875a), GCash (#0284c7), Maya (#0d9488), Card (#d97706), Bank (#7c3aed)
    payment_method_configs = [
        {"method": Payment.Method.CASH, "name": "Cash", "color": "#00875a", "key": "cash"},
        {"method": Payment.Method.GCASH, "name": "GCash", "color": "#0284c7", "key": "gcash"},
        {"method": Payment.Method.MAYA, "name": "Maya", "color": "#0d9488", "key": "maya"},
    ]
    if payment_totals["card"] > ZERO:
        payment_method_configs.append({"method": Payment.Method.CARD, "name": "Card", "color": "#d97706", "key": "card"})
    if payment_totals["bank"] > ZERO:
        payment_method_configs.append({"method": Payment.Method.BANK, "name": "Bank", "color": "#7c3aed", "key": "bank"})

    payment_method_list = []
    payment_chart_labels = []
    payment_chart_data = []
    payment_chart_colors = []

    for cfg in payment_method_configs:
        rev = payment_totals.get(cfg["key"], ZERO)
        pct = (
            round(float((rev / payment_total_revenue) * 100), 1)
            if payment_total_revenue > 0
            else 0.0
        )
        payment_method_list.append({
            "method": cfg["method"],
            "name": cfg["name"],
            "revenue": rev,
            "revenue_float": float(rev),
            "percentage": pct,
            "color": cfg["color"],
        })
        if rev > 0:
            payment_chart_labels.append(cfg["name"])
            payment_chart_data.append(float(rev))
            payment_chart_colors.append(cfg["color"])

    # 3. Peak Rush Hours KPI (Store traffic & order volume by hour)
    hourly_slots = [
        (7, "07 AM"),
        (8, "08 AM"),
        (9, "09 AM"),
        (10, "10 AM"),
        (11, "11 AM"),
        (12, "12 PM"),
        (13, "01 PM"),
        (14, "02 PM"),
        (15, "03 PM"),
        (16, "04 PM"),
        (17, "05 PM"),
        (18, "06 PM"),
        (19, "07 PM"),
        (20, "08 PM"),
        (21, "09 PM"),
        (22, "10 PM"),
    ]
    hourly_counts = {hour: 0 for hour, _ in hourly_slots}

    for sale in period_sales_qs:
        h = timezone.localtime(sale.created_at).hour
        if h in hourly_counts:
            hourly_counts[h] += 1

    appt_period_filter = Q(status__in=(Appointment.Status.COMPLETED, Appointment.Status.ONGOING)) & Q(sale__isnull=True)
    if p_start is not None and p_end is not None:
        period_traffic_appts = Appointment.objects.filter(appt_period_filter, appointment_date__range=(p_start, p_end))
    elif p_start is not None:
        period_traffic_appts = Appointment.objects.filter(appt_period_filter, appointment_date__gte=p_start)
    else:
        period_traffic_appts = Appointment.objects.filter(appt_period_filter)

    for appt in period_traffic_appts:
        if appt.start_time:
            h = appt.start_time.hour
            if h in hourly_counts:
                hourly_counts[h] += 1

    peak_hours_labels = [label for _, label in hourly_slots]
    peak_hours_data = [hourly_counts[hour] for hour, _ in hourly_slots]
    peak_hours_max = max(peak_hours_data) if peak_hours_data else 0

    kpi_charts_payload = {
        "category": {
            "labels": category_chart_labels,
            "data": category_chart_data,
            "colors": category_chart_colors,
            "total": float(category_total_revenue),
            "has_data": bool(category_chart_data and category_total_revenue > 0),
        },
        "payment": {
            "labels": payment_chart_labels,
            "data": payment_chart_data,
            "colors": payment_chart_colors,
            "total": float(payment_total_revenue),
            "has_data": bool(payment_chart_data and payment_total_revenue > 0),
        },
        "peak_hours": {
            "labels": peak_hours_labels,
            "data": peak_hours_data,
            "has_data": any(val > 0 for val in peak_hours_data),
        },
    }

    return {
        "today": today,
        "today_sales": today_sales,
        "today_avg_ticket": today_avg_ticket,
        "month_sales": month_sales,
        "month_avg_ticket": month_avg_ticket,
        "customer_count": customer_count,
        "new_customers_this_month": new_customers_this_month,
        "appointment_count": Appointment.objects.filter(
            status__in=VISIBLE_APPOINTMENT_STATUSES
        ).count(),
        "today_appointments_count": today_appointments_count,
        "today_completed_appointments": today_completed_appointments,
        "pending_appointments_count": pending_appointments_count,
        "pending_appointments": list(
            Appointment.objects.filter(status=Appointment.Status.PENDING)
            .select_related("customer", "assigned_staff")
            .prefetch_related("appointment_services")
            .order_by("appointment_date", "start_time")[:6]
        ),
        "today_queue": list(
            Appointment.objects.filter(
                appointment_date=today,
                status__in=(
                    Appointment.Status.PENDING,
                    Appointment.Status.APPROVED,
                    Appointment.Status.RESCHEDULED,
                    Appointment.Status.ONGOING,
                    Appointment.Status.COMPLETED,
                ),
            )
            .select_related("customer", "assigned_staff")
            .prefetch_related("appointment_services", "sale")
            .order_by("start_time")[:10]
        ),
        "ongoing_services": AppointmentService.objects.filter(
            status=AppointmentService.Status.ONGOING
        ).count(),
        "active_staff_count": active_staff_count,
        "staff_on_duty_today": staff_on_duty_today,
        "active_shifts_count": active_shifts_count,
        "period": period,
        "period_label": period_label,
        "period_sales": period_sales,
        "period_avg_ticket": period_avg_ticket,
        "period_appointments_count": period_appointments_count,
        "top_services": top_services,
        "top_staff": top_staff,
        "recent_transactions": completed_sales().select_related("cashier", "payment")[:8],
        "sales_trend": sales_trend,
        "sales_trend_max": sales_trend_max,
        "category_sales_list": category_sales_list,
        "category_chart_labels": category_chart_labels,
        "category_chart_data": category_chart_data,
        "category_chart_colors": category_chart_colors,
        "category_total_revenue": category_total_revenue,
        "payment_totals": payment_totals,
        "payment_method_list": payment_method_list,
        "payment_chart_labels": payment_chart_labels,
        "payment_chart_data": payment_chart_data,
        "payment_chart_colors": payment_chart_colors,
        "payment_total_revenue": payment_total_revenue,
        "peak_hours_labels": peak_hours_labels,
        "peak_hours_data": peak_hours_data,
        "peak_hours_max": peak_hours_max,
        "kpi_charts_payload": kpi_charts_payload,
    }


def cashier_dashboard_data(user):
    today = timezone.localdate()
    sales = completed_sales(today, today).filter(cashier=user)
    appointments = Appointment.objects.filter(
        appointment_date=today, status__in=VISIBLE_APPOINTMENT_STATUSES
    ).select_related("customer", "assigned_staff")
    return {
        "today": today,
        "pos_totals": _financial_totals(sales),
        "bookings_count": appointments.count(),
        "pending_bookings": appointments.filter(status=Appointment.Status.PENDING).count(),
        "appointments": appointments[:8],
        "recent_transactions": sales.select_related("payment")[:8],
    }


def staff_dashboard_data(user):
    today = timezone.localdate()
    work = (
        AppointmentService.objects.filter(
            assigned_staff=user,
            appointment__appointment_date=today,
        )
        .exclude(
            appointment__status__in=(
                Appointment.Status.UNVERIFIED,
                Appointment.Status.CANCELLED,
                Appointment.Status.REJECTED,
                Appointment.Status.NO_SHOW,
                Appointment.Status.EXPIRED,
            )
        )
        .select_related("appointment", "appointment__customer")
        .order_by("appointment__start_time", "position")
    )
    notifications = Notification.objects.filter(recipient=user).select_related(
        "appointment"
    )[:8]
    return {
        "today": today,
        "assigned_work": work,
        "assigned_count": work.count(),
        "ongoing_count": work.filter(status=AppointmentService.Status.ONGOING).count(),
        "completed_count": work.filter(status=AppointmentService.Status.COMPLETED).count(),
        "unread_notifications": Notification.objects.filter(
            recipient=user, read_at__isnull=True
        ).count(),
        "notifications": notifications,
    }


def _period_sales_report(report_type, start_date, end_date):
    truncation = {
        "daily": TruncDay,
        "weekly": TruncWeek,
        "monthly": TruncMonth,
        "annual": TruncYear,
    }[report_type]
    sales = completed_sales(start_date, end_date)
    values = (
        sales.annotate(period=truncation("created_at"))
        .values("period")
        .annotate(
            transactions=Count("pk"),
            gross=Sum("subtotal"),
            discounts=Sum("discount_amount"),
            net=Sum("total"),
        )
        .order_by("period")
    )
    rows = [
        {
            "Period": timezone.localtime(row["period"]).date(),
            "Transactions": row["transactions"],
            "Gross Sales": row["gross"] or ZERO,
            "Discounts": row["discounts"] or ZERO,
            "Net Sales": row["net"] or ZERO,
        }
        for row in values
    ]
    labels = {
        "daily": "Daily Sales",
        "weekly": "Weekly Sales",
        "monthly": "Monthly Sales",
        "annual": "Annual Sales",
    }
    return ReportResult(
        key=report_type,
        title=labels[report_type],
        description="Completed, non-voided sales grouped by period.",
        columns=("Period", "Transactions", "Gross Sales", "Discounts", "Net Sales"),
        rows=rows,
        totals=_financial_totals(sales),
        start_date=start_date,
        end_date=end_date,
    )


def _service_sales_report(start_date, end_date):
    items = SaleItem.objects.filter(sale__in=completed_sales(start_date, end_date))
    values = (
        items.values("service_name")
        .annotate(
            quantity=Sum("quantity"),
            transactions=Count("sale", distinct=True),
            gross=Sum("line_total"),
        )
        .order_by("-gross", "service_name")
    )
    rows = [
        {
            "Service": row["service_name"],
            "Quantity": row["quantity"],
            "Transactions": row["transactions"],
            "Gross Service Sales": row["gross"] or ZERO,
        }
        for row in values
    ]
    totals = {
        "transactions": completed_sales(start_date, end_date).count(),
        "gross": sum((row["Gross Service Sales"] for row in rows), ZERO),
        "discounts": ZERO,
        "net": sum((row["Gross Service Sales"] for row in rows), ZERO),
    }
    return ReportResult(
        key="service-sales",
        title="Service Sales",
        description="Gross service-item sales from completed, non-voided transactions.",
        columns=("Service", "Quantity", "Transactions", "Gross Service Sales"),
        rows=rows,
        totals=totals,
        start_date=start_date,
        end_date=end_date,
    )


def _appointment_status_report(start_date, end_date):
    values = (
        Appointment.objects.filter(
            appointment_date__range=(start_date, end_date),
            status__in=VISIBLE_APPOINTMENT_STATUSES,
        )
        .values("status")
        .annotate(count=Count("pk"))
        .order_by("status")
    )
    labels = dict(Appointment.Status.choices)
    rows = [
        {"Status": labels[row["status"]], "Appointments": row["count"]}
        for row in values
    ]
    return ReportResult(
        key="appointment-status",
        title="Appointment Status",
        description="Verified appointments grouped by current status.",
        columns=("Status", "Appointments"),
        rows=rows,
        totals={"appointments": sum(row["Appointments"] for row in rows)},
        start_date=start_date,
        end_date=end_date,
    )


def _staff_workload_report(start_date, end_date):
    staff = (
        User.objects.filter(role=User.Role.STAFF)
        .annotate(
            assigned=Count(
                "assigned_service_work",
                filter=Q(
                    assigned_service_work__appointment__appointment_date__range=(
                        start_date,
                        end_date,
                    ),
                    assigned_service_work__appointment__status__in=WORKLOAD_APPOINTMENT_STATUSES,
                ),
            ),
            completed=Count(
                "assigned_service_work",
                filter=Q(
                    assigned_service_work__appointment__appointment_date__range=(
                        start_date,
                        end_date,
                    ),
                    assigned_service_work__appointment__status__in=WORKLOAD_APPOINTMENT_STATUSES,
                    assigned_service_work__status=AppointmentService.Status.COMPLETED,
                ),
            ),
            ongoing=Count(
                "assigned_service_work",
                filter=Q(
                    assigned_service_work__appointment__appointment_date__range=(
                        start_date,
                        end_date,
                    ),
                    assigned_service_work__appointment__status__in=WORKLOAD_APPOINTMENT_STATUSES,
                    assigned_service_work__status=AppointmentService.Status.ONGOING,
                ),
            ),
            minutes=Sum(
                "assigned_service_work__duration_minutes",
                filter=Q(
                    assigned_service_work__appointment__appointment_date__range=(
                        start_date,
                        end_date,
                    ),
                    assigned_service_work__appointment__status__in=WORKLOAD_APPOINTMENT_STATUSES,
                ),
            ),
        )
        .filter(assigned__gt=0)
        .order_by("-completed", "first_name", "last_name", "email")
    )
    rows = [
        {
            "Staff": str(user),
            "Assigned Services": user.assigned,
            "Completed Services": user.completed,
            "Ongoing Services": user.ongoing,
            "Scheduled Minutes": user.minutes or 0,
        }
        for user in staff
    ]
    return ReportResult(
        key="staff-workload",
        title="Staff Workload",
        description="Assigned operational service workload by staff member.",
        columns=(
            "Staff",
            "Assigned Services",
            "Completed Services",
            "Ongoing Services",
            "Scheduled Minutes",
        ),
        rows=rows,
        totals={"assigned": sum(row["Assigned Services"] for row in rows)},
        start_date=start_date,
        end_date=end_date,
    )


def _top_services_report(start_date, end_date):
    appt_services = (
        AppointmentService.objects.filter(
            appointment__appointment_date__range=(start_date, end_date),
            appointment__status__in=VISIBLE_APPOINTMENT_STATUSES,
        )
        .values("service_name")
        .annotate(
            total_bookings=Count("id"),
            completed_bookings=Count(
                "id",
                filter=Q(status=AppointmentService.Status.COMPLETED)
                | Q(appointment__status=Appointment.Status.COMPLETED),
            ),
            revenue=Sum(
                "price",
                filter=Q(status=AppointmentService.Status.COMPLETED)
                | Q(appointment__status=Appointment.Status.COMPLETED),
            ),
        )
        .order_by("-total_bookings", "-revenue")
    )
    rows = []
    for rank, item in enumerate(appt_services, 1):
        rows.append(
            {
                "Rank": rank,
                "Service": item["service_name"],
                "Total Bookings": item["total_bookings"],
                "Completed": item["completed_bookings"],
                "Revenue": item["revenue"] or ZERO,
            }
        )
    totals = {
        "total_bookings": sum((r["Total Bookings"] for r in rows), 0),
        "completed": sum((r["Completed"] for r in rows), 0),
        "revenue": sum((r["Revenue"] for r in rows), ZERO),
        "net": sum((r["Revenue"] for r in rows), ZERO),
        "gross": sum((r["Revenue"] for r in rows), ZERO),
        "transactions": sum((r["Total Bookings"] for r in rows), 0),
        "discounts": ZERO,
    }
    return ReportResult(
        key="top-services",
        title="Top Services (Most Booked)",
        description="Most booked services ranked by appointment volume and revenue.",
        columns=("Rank", "Service", "Total Bookings", "Completed", "Revenue"),
        rows=rows,
        totals=totals,
        start_date=start_date,
        end_date=end_date,
    )


def _top_staff_report(start_date, end_date):
    staff_qs = (
        User.objects.filter(role=User.Role.STAFF)
        .annotate(
            assigned_count=Count(
                "assigned_service_work",
                filter=Q(
                    assigned_service_work__appointment__appointment_date__range=(
                        start_date,
                        end_date,
                    ),
                    assigned_service_work__appointment__status__in=WORKLOAD_APPOINTMENT_STATUSES,
                ),
            ),
            completed_count=Count(
                "assigned_service_work",
                filter=Q(
                    assigned_service_work__appointment__appointment_date__range=(
                        start_date,
                        end_date,
                    ),
                    assigned_service_work__appointment__status__in=WORKLOAD_APPOINTMENT_STATUSES,
                    assigned_service_work__status=AppointmentService.Status.COMPLETED,
                ),
            ),
            service_revenue=Sum(
                "assigned_service_work__price",
                filter=Q(
                    assigned_service_work__appointment__appointment_date__range=(
                        start_date,
                        end_date,
                    ),
                    assigned_service_work__appointment__status__in=WORKLOAD_APPOINTMENT_STATUSES,
                    assigned_service_work__status=AppointmentService.Status.COMPLETED,
                ),
            ),
        )
        .filter(assigned_count__gt=0)
        .order_by("-completed_count", "-service_revenue")
    )
    rows = []
    for rank, u in enumerate(staff_qs, 1):
        rate = (
            f"{(u.completed_count / u.assigned_count * 100):.1f}%"
            if u.assigned_count
            else "0.0%"
        )
        rows.append(
            {
                "Rank": rank,
                "Staff": str(u),
                "Assigned": u.assigned_count,
                "Completed": u.completed_count,
                "Completion Rate": rate,
                "Service Revenue": u.service_revenue or ZERO,
            }
        )
    totals = {
        "assigned": sum((r["Assigned"] for r in rows), 0),
        "completed": sum((r["Completed"] for r in rows), 0),
        "revenue": sum((r["Service Revenue"] for r in rows), ZERO),
        "net": sum((r["Service Revenue"] for r in rows), ZERO),
        "gross": sum((r["Service Revenue"] for r in rows), ZERO),
        "transactions": sum((r["Completed"] for r in rows), 0),
        "discounts": ZERO,
    }
    return ReportResult(
        key="top-staff",
        title="Top Performing Staff",
        description="Technicians ranked by completed services and service performance.",
        columns=(
            "Rank",
            "Staff",
            "Assigned",
            "Completed",
            "Completion Rate",
            "Service Revenue",
        ),
        rows=rows,
        totals=totals,
        start_date=start_date,
        end_date=end_date,
    )


def sort_report_rows(rows, sort_column, direction="asc"):
    if not sort_column or not rows:
        return rows
    reverse = direction.lower() == "desc"

    def sort_key(row):
        val = row.get(sort_column)
        if val is None:
            return ""
        if isinstance(val, str) and val.endswith("%"):
            try:
                return float(val.rstrip("%"))
            except ValueError:
                pass
        return val

    try:
        return sorted(rows, key=sort_key, reverse=reverse)
    except TypeError:
        return sorted(rows, key=lambda r: str(r.get(sort_column, "")), reverse=reverse)


def build_report(report_type, start_date, end_date):
    if report_type in ("daily", "weekly", "monthly", "annual"):
        return _period_sales_report(report_type, start_date, end_date)
    if report_type == "service-sales":
        return _service_sales_report(start_date, end_date)
    if report_type == "appointment-status":
        return _appointment_status_report(start_date, end_date)
    if report_type == "staff-workload":
        return _staff_workload_report(start_date, end_date)
    if report_type == "top-services":
        return _top_services_report(start_date, end_date)
    if report_type == "top-staff":
        return _top_staff_report(start_date, end_date)
    raise ValueError("Unknown report type.")
