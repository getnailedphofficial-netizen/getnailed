from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import socket

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connections, models, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.accounts.decorators import (
    authenticated_internal_user_required,
    customer_required,
    owner_required,
)
from apps.accounts.models import User
from apps.bookings.models import Appointment, AppointmentStatusHistory
from apps.customers.models import Customer
from apps.pos.models import Sale
from apps.reports.services import (
    cashier_dashboard_data,
    owner_dashboard_data,
    staff_dashboard_data,
)
from apps.services.models import Service, ServiceCategory


def home(request):
    return render(request, "core/home.html")


@login_required
def dashboard_router(request):
    if request.user.is_owner:
        return redirect("core:owner_dashboard")
    if request.user.is_service_staff or request.user.is_cashier:
        return redirect("core:staff_dashboard")
    return redirect("core:customer_dashboard")


@owner_required
def owner_dashboard(request):
    period = request.GET.get("period", "today")
    return render(request, "dashboards/owner.html", owner_dashboard_data(period=period))


@owner_required
def system_health(request):
    """Safe system health overview for OWNER. Does not expose secrets."""
    checks = {}
    # Application / settings
    checks['environment'] = {
        'debug': settings.DEBUG,
        'installed_apps_count': len(settings.INSTALLED_APPS),
    }

    # Database check (basic)
    db_status = {'ok': False, 'message': ''}
    try:
        conn = connections['default']
        with conn.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        db_status['ok'] = True
    except Exception as exc:  # pragma: no cover - environment dependent
        db_status['message'] = str(exc)
    checks['database'] = db_status

    # Email backend check (no credentials)
    email_backend = getattr(settings, 'EMAIL_BACKEND', '')
    email_info = {'backend': email_backend}
    if 'smtp' in email_backend.lower():
        host = getattr(settings, 'EMAIL_HOST', '')
        port = getattr(settings, 'EMAIL_PORT', '')
        email_info['host'] = host
        email_info['port'] = port
        try:
            # only resolve host, do not attempt auth
            socket.getaddrinfo(host, port or 25)
            email_info['resolves'] = True
        except Exception:
            email_info['resolves'] = False
    checks['email'] = email_info

    # Last backup metadata (if present)
    last_backup = None
    backups_dir = Path(settings.BASE_DIR) / 'backups'
    metadata_file = backups_dir / 'last_backup.json'
    if metadata_file.exists():
        try:
            with metadata_file.open() as fh:
                last_backup = json.load(fh)
        except Exception:
            last_backup = {'error': 'corrupt metadata'}
    checks['last_backup'] = last_backup

    # Forecast model availability (trained runs with model file)
    model_available = False
    try:
        from apps.forecasting.models import ForecastRun

        trained = ForecastRun.objects.filter(status=ForecastRun.Status.TRAINED).exclude(model_file_path='').order_by('-completed_at').first()
        if trained and Path(trained.model_file_path).exists():
            model_available = True
    except Exception:
        model_available = False
    checks['forecast_model_available'] = model_available

    # Failed login alert count (recent failures)
    failed_logins = 0
    try:
        from apps.audittrail.models import SecurityEvent

        failed_logins = SecurityEvent.objects.filter(action=SecurityEvent.Action.LOGIN_FAILED).count()
    except Exception:
        failed_logins = 0
    checks['failed_login_count'] = failed_logins

    return render(request, 'core/system_health.html', {'checks': checks})


@authenticated_internal_user_required
def staff_dashboard(request):
    if request.user.is_cashier:
        return render(
            request,
            "dashboards/cashier.html",
            cashier_dashboard_data(request.user),
        )
    return render(
        request,
        "dashboards/staff.html",
        staff_dashboard_data(request.user),
    )


def _get_or_create_customer(user):
    customer = getattr(user, "customer_profile", None)
    if customer is None and user.role == User.Role.CUSTOMER:
        customer, _ = Customer.objects.get_or_create(
            user=user,
            defaults={
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email,
                "phone": getattr(user, "phone_number", ""),
            },
        )
    return customer


@customer_required
def customer_dashboard(request):
    customer = _get_or_create_customer(request.user)

    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        if action == "cancel":
            reference = request.POST.get("reference", "").strip()
            appointment = get_object_or_404(
                Appointment.objects.filter(customer=customer),
                booking_reference=reference,
            )
            if appointment.status in (
                Appointment.Status.PENDING,
                Appointment.Status.APPROVED,
                Appointment.Status.RESCHEDULED,
            ):
                with transaction.atomic():
                    previous = appointment.status
                    reason = request.POST.get("reason", "Cancelled by customer").strip() or "Cancelled by customer"
                    appointment.status = Appointment.Status.CANCELLED
                    appointment.cancelled_at = timezone.now()
                    appointment.cancellation_reason = reason
                    appointment.save(update_fields=("status", "cancelled_at", "cancellation_reason", "updated_at"))
                    AppointmentStatusHistory.objects.create(
                        appointment=appointment,
                        from_status=previous,
                        to_status=appointment.status,
                        actor=request.user,
                        note=reason,
                    )
                    try:
                        from apps.bookings.services import _create_notifications
                        from apps.notifications.models import Notification

                        _create_notifications(
                            appointment,
                            Notification.EventType.CANCELLED,
                            "Appointment cancelled",
                            f"{appointment.customer_name} cancelled {appointment.reference}.",
                            include_managers=True,
                        )
                    except Exception:
                        pass
                messages.success(request, f"Appointment {appointment.booking_reference} was cancelled successfully.")
            else:
                messages.error(request, f"Appointment {appointment.booking_reference} cannot be cancelled at this stage.")
            return redirect("core:customer_dashboard")

        elif action == "reschedule":
            reference = request.POST.get("reference", "").strip()
            appointment = get_object_or_404(
                Appointment.objects.filter(customer=customer),
                booking_reference=reference,
            )
            if appointment.status in (
                Appointment.Status.PENDING,
                Appointment.Status.APPROVED,
                Appointment.Status.RESCHEDULED,
            ):
                token = appointment.issue_public_access_token(timezone.now() + timedelta(hours=24))
                appointment.save(update_fields=("public_access_token_digest", "public_access_expires_at"))
                return redirect("bookings:reschedule", reference=appointment.booking_reference, token=token)
            else:
                messages.error(request, "This appointment cannot be rescheduled.")
            return redirect("core:customer_dashboard")

        elif action == "view_status":
            reference = request.POST.get("reference", "").strip()
            appointment = get_object_or_404(
                Appointment.objects.filter(customer=customer),
                booking_reference=reference,
            )
            token = appointment.issue_public_access_token(timezone.now() + timedelta(hours=24))
            appointment.save(update_fields=("public_access_token_digest", "public_access_expires_at"))
            return redirect("bookings:public_status", reference=appointment.booking_reference, token=token)

        elif action == "update_profile":
            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()
            phone = request.POST.get("phone", "").strip()
            if first_name and last_name:
                if customer:
                    customer.first_name = first_name
                    customer.last_name = last_name
                    customer.phone = phone
                    customer.save(update_fields=("first_name", "last_name", "phone", "updated_at"))
                request.user.first_name = first_name
                request.user.last_name = last_name
                request.user.phone_number = phone
                request.user.save(update_fields=("first_name", "last_name", "phone_number"))
                messages.success(request, "Your contact details have been updated successfully.")
            else:
                messages.error(request, "First and last name are required.")
            return redirect("core:customer_dashboard")

    today_date = timezone.localdate()
    today_str = today_date.strftime("%b. %d, %Y")

    appointments = Appointment.objects.none()
    upcoming_appointments = Appointment.objects.none()
    completed_appointments = Appointment.objects.none()
    ongoing_appointments = Appointment.objects.none()
    total_spent = Decimal("0.00")

    if customer is not None:
        appointments = (
            Appointment.objects.filter(customer=customer)
            .exclude(status__in=(Appointment.Status.UNVERIFIED, Appointment.Status.EXPIRED))
            .select_related("assigned_staff")
            .prefetch_related("appointment_services__service")
            .order_by("-appointment_date", "-start_time")
        )
        upcoming_appointments = appointments.filter(
            status__in=(
                Appointment.Status.PENDING,
                Appointment.Status.APPROVED,
                Appointment.Status.RESCHEDULED,
            ),
            appointment_date__gte=today_date,
        ).order_by("appointment_date", "start_time")

        completed_appointments = appointments.filter(
            status=Appointment.Status.COMPLETED
        ).order_by("-appointment_date", "-start_time")

        ongoing_appointments = appointments.filter(
            Q(status=Appointment.Status.ONGOING)
            | Q(status=Appointment.Status.APPROVED, appointment_date=today_date)
        )

        completed_sales = Sale.objects.filter(customer=customer, status=Sale.Status.COMPLETED)
        total_spent = completed_sales.aggregate(models.Sum("total"))["total__sum"] or Decimal("0.00")

    services = (
        Service.objects.filter(is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )
    categories = ServiceCategory.objects.all().order_by("name")

    context = {
        "customer": customer,
        "today": today_str,
        "appointments": appointments,
        "upcoming_appointments": upcoming_appointments,
        "completed_appointments": completed_appointments,
        "ongoing_appointments": ongoing_appointments,
        "appointment_count": appointments.count(),
        "upcoming_count": upcoming_appointments.count(),
        "completed_count": completed_appointments.count(),
        "ongoing_count": ongoing_appointments.count(),
        "total_spent": total_spent,
        "services": services,
        "featured_services": services,
        "categories": categories,
    }
    return render(request, "dashboards/customer.html", context)


@customer_required
def customer_appointments(request):
    customer = _get_or_create_customer(request.user)

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if action == "cancel":
            reference = request.POST.get("reference", "").strip()
            appointment = get_object_or_404(
                Appointment.objects.filter(customer=customer),
                booking_reference=reference,
            )
            if appointment.status in (
                Appointment.Status.PENDING,
                Appointment.Status.APPROVED,
                Appointment.Status.RESCHEDULED,
            ):
                with transaction.atomic():
                    previous = appointment.status
                    reason = request.POST.get("reason", "Cancelled by customer").strip() or "Cancelled by customer"
                    appointment.status = Appointment.Status.CANCELLED
                    appointment.cancelled_at = timezone.now()
                    appointment.cancellation_reason = reason
                    appointment.save(update_fields=("status", "cancelled_at", "cancellation_reason", "updated_at"))
                    AppointmentStatusHistory.objects.create(
                        appointment=appointment,
                        from_status=previous,
                        to_status=appointment.status,
                        actor=request.user,
                        note=reason,
                    )
                    try:
                        from apps.bookings.services import _create_notifications
                        from apps.notifications.models import Notification

                        _create_notifications(
                            appointment,
                            Notification.EventType.CANCELLED,
                            "Appointment cancelled",
                            f"{appointment.customer_name} cancelled {appointment.reference}.",
                            include_managers=True,
                        )
                    except Exception:
                        pass
                messages.success(request, f"Appointment {appointment.booking_reference} was cancelled successfully.")
            else:
                messages.error(request, f"Appointment {appointment.booking_reference} cannot be cancelled at this stage.")
            return redirect("core:customer_appointments")

        elif action == "reschedule":
            reference = request.POST.get("reference", "").strip()
            appointment = get_object_or_404(
                Appointment.objects.filter(customer=customer),
                booking_reference=reference,
            )
            if appointment.status in (
                Appointment.Status.PENDING,
                Appointment.Status.APPROVED,
                Appointment.Status.RESCHEDULED,
            ):
                token = appointment.issue_public_access_token(timezone.now() + timedelta(hours=24))
                appointment.save(update_fields=("public_access_token_digest", "public_access_expires_at"))
                return redirect("bookings:reschedule", reference=appointment.booking_reference, token=token)
            else:
                messages.error(request, "This appointment cannot be rescheduled.")
            return redirect("core:customer_appointments")

        elif action == "view_status":
            reference = request.POST.get("reference", "").strip()
            appointment = get_object_or_404(
                Appointment.objects.filter(customer=customer),
                booking_reference=reference,
            )
            token = appointment.issue_public_access_token(timezone.now() + timedelta(hours=24))
            appointment.save(update_fields=("public_access_token_digest", "public_access_expires_at"))
            return redirect("bookings:public_status", reference=appointment.booking_reference, token=token)

    today_date = timezone.localdate()
    appointments = Appointment.objects.none()
    upcoming_appointments = Appointment.objects.none()
    past_appointments = Appointment.objects.none()

    if customer is not None:
        appointments = (
            Appointment.objects.filter(customer=customer)
            .exclude(status__in=(Appointment.Status.UNVERIFIED, Appointment.Status.EXPIRED))
            .select_related("assigned_staff", "sale")
            .prefetch_related("appointment_services__service")
            .order_by("-appointment_date", "-start_time")
        )
        upcoming_appointments = appointments.filter(
            status__in=(
                Appointment.Status.PENDING,
                Appointment.Status.APPROVED,
                Appointment.Status.RESCHEDULED,
            ),
            appointment_date__gte=today_date,
        ).order_by("appointment_date", "start_time")

        past_appointments = appointments.exclude(
            pk__in=upcoming_appointments.values_list("pk", flat=True)
        ).order_by("-appointment_date", "-start_time")

    customer_sales = (
        Sale.objects.filter(customer=customer, status=Sale.Status.COMPLETED)
        .select_related("payment")
        .prefetch_related("items")
        .order_by("-created_at")
        if customer is not None
        else Sale.objects.none()
    )

    return render(
        request,
        "customers/appointments.html",
        {
            "customer": customer,
            "upcoming_appointments": upcoming_appointments,
            "past_appointments": past_appointments,
            "upcoming_count": upcoming_appointments.count(),
            "past_count": past_appointments.count(),
            "customer_sales": customer_sales,
        },
    )


@customer_required
def customer_receipt(request, receipt_number):
    customer = _get_or_create_customer(request.user)
    sale = get_object_or_404(
        Sale.objects.filter(customer=customer)
        .select_related("customer", "appointment", "cashier", "payment")
        .prefetch_related("items"),
        receipt_number=receipt_number,
    )
    return render(request, "pos/receipt.html", {"sale": sale, "is_customer_receipt": True})


@customer_required
def customer_services(request):
    query = request.GET.get("q", "").strip()
    category_id = request.GET.get("category", "").strip()

    services = (
        Service.objects.filter(is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )
    if query:
        services = services.filter(Q(name__icontains=query) | Q(description__icontains=query))
    if category_id:
        services = services.filter(category_id=category_id)

    all_categories = ServiceCategory.objects.all()

    return render(
        request,
        "customers/services.html",
        {
            "services": services,
            "all_categories": all_categories,
            "query": query,
            "selected_category": category_id,
            "services_count": services.count(),
        },
    )


@customer_required
def customer_profile(request):
    customer = _get_or_create_customer(request.user)

    if request.method == "POST":
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        phone = request.POST.get("phone", "").strip()
        if first_name and last_name:
            if customer:
                customer.first_name = first_name
                customer.last_name = last_name
                customer.phone = phone
                customer.save(update_fields=("first_name", "last_name", "phone", "updated_at"))
            request.user.first_name = first_name
            request.user.last_name = last_name
            request.user.phone_number = phone
            request.user.save(update_fields=("first_name", "last_name", "phone_number"))
            messages.success(request, "Your contact details have been updated successfully.")
        else:
            messages.error(request, "First and last name are required.")
        return redirect("core:customer_profile")

    completed_sales = (
        Sale.objects.filter(customer=customer, status=Sale.Status.COMPLETED)
        if customer
        else Sale.objects.none()
    )
    total_spent = (
        completed_sales.aggregate(models.Sum("total"))["total__sum"]
        or Decimal("0.00")
    )
    appointment_count = (
        Appointment.objects.filter(customer=customer)
        .exclude(status__in=(Appointment.Status.UNVERIFIED, Appointment.Status.EXPIRED))
        .count()
        if customer
        else 0
    )

    return render(
        request,
        "customers/profile.html",
        {
            "customer": customer,
            "total_spent": total_spent,
            "appointment_count": appointment_count,
        },
    )
