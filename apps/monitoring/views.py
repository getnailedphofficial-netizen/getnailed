from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.authorization import (
    CAPABILITY_ASSIGN_SERVICES,
    CAPABILITY_VIEW_MONITORING,
    has_capability,
)
from apps.accounts.decorators import capability_required
from apps.accounts.models import User
from apps.audittrail.events import record_security_event
from apps.audittrail.models import SecurityEvent
from apps.bookings.models import Appointment, AppointmentService

from .forms import MonitoringFilterForm, ServiceAssignmentForm, ServiceStatusForm
from .services import (
    allowed_transitions,
    assign_service,
    transition_service,
    visible_work_items,
)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _board_context(request):
    filter_form = MonitoringFilterForm(request.GET or None)
    selected_date = timezone.localdate()
    selected_staff = None
    query = ""
    if filter_form.is_valid():
        selected_date = filter_form.cleaned_data["date"] or selected_date
        selected_staff = filter_form.cleaned_data["staff"]
        query = filter_form.cleaned_data["q"]
    items = (
        visible_work_items(request.user)
        .filter(appointment__appointment_date=selected_date)
        .select_related(
            "appointment",
            "appointment__customer",
            "service",
            "assigned_staff",
        )
        .prefetch_related("appointment__appointment_services")
    )
    if selected_staff:
        items = items.filter(assigned_staff=selected_staff)
    if query:
        items = items.filter(
            Q(service_name__icontains=query)
            | Q(appointment__customer_name_snapshot__icontains=query)
            | Q(appointment__booking_reference__icontains=query)
        )
    items = list(items.order_by("appointment__start_time", "position", "pk"))
    now = timezone.now()
    labels = dict(AppointmentService.Status.choices)
    action_labels = {
        AppointmentService.Status.APPROVED: "Confirm / Approve",
        AppointmentService.Status.ONGOING: "Start Service (Ongoing)",
        AppointmentService.Status.COMPLETED: "Confirm Completed",
    }
    column_headers = {
        AppointmentService.Status.PENDING: "Pending Approval",
        AppointmentService.Status.APPROVED: "Confirmed / Approved",
        AppointmentService.Status.ONGOING: "Current Service (Ongoing)",
        AppointmentService.Status.COMPLETED: "Completed / Confirmed Done",
    }

    all_staff_list = list(filter_form.fields["staff"].queryset)
    for u in all_staff_list:
        u.display_label = (
            f"{u.get_full_name()} — {u.staff_profile.specialty}"
            if getattr(u, "staff_profile", None) and u.staff_profile.specialty
            else (u.get_full_name() or u.email)
        )

    current_ongoing_services = []
    for item in items:
        if item.service:
            item.qualified_staff = [
                u for u in all_staff_list
                if hasattr(u, "staff_profile") and u.staff_profile.can_perform(item.service)
            ]
        else:
            item.qualified_staff = all_staff_list
        item.next_statuses = [
            (status, action_labels.get(status, labels[status]))
            for status in allowed_transitions(item, request.user)
        ]
        if item.status == AppointmentService.Status.ONGOING:
            if item.started_at:
                elapsed = max(0, int((now - item.started_at).total_seconds() / 60))
            else:
                elapsed = 0
            dur = item.duration_minutes or 30
            pct = min(100, max(5, int((elapsed / dur) * 100)))
            rem = max(0, dur - elapsed)
            item.elapsed_minutes = elapsed
            item.progress_percent = pct
            item.remaining_minutes = rem
            current_ongoing_services.append(item)

    statuses = AppointmentService.Status
    columns = [
        {
            "status": status,
            "label": column_headers.get(status, label),
            "items": [item for item in items if item.status == status],
        }
        for status, label in statuses.choices
    ]
    active_filter = Q(
        assigned_service_work__appointment__appointment_date=selected_date,
        assigned_service_work__appointment__status__in=(
            Appointment.Status.PENDING,
            Appointment.Status.APPROVED,
            Appointment.Status.RESCHEDULED,
            Appointment.Status.ONGOING,
            Appointment.Status.COMPLETED,
        ),
    )
    workloads = User.objects.filter(
        role=User.Role.STAFF, is_active=True, staff_profile__is_active=True
    ).annotate(
        pending_count=Count(
            "assigned_service_work",
            filter=active_filter & Q(assigned_service_work__status=statuses.PENDING),
        ),
        approved_count=Count(
            "assigned_service_work",
            filter=active_filter & Q(assigned_service_work__status=statuses.APPROVED),
        ),
        ongoing_count=Count(
            "assigned_service_work",
            filter=active_filter & Q(assigned_service_work__status=statuses.ONGOING),
        ),
    ).order_by("first_name", "last_name", "email")
    return {
        "filter_form": filter_form,
        "selected_date": selected_date,
        "columns": columns,
        "workloads": workloads,
        "queue_count": len(items),
        "ongoing_count": sum(
            item.status == AppointmentService.Status.ONGOING for item in items
        ),
        "current_ongoing_services": current_ongoing_services,
        "can_assign": has_capability(request.user, CAPABILITY_ASSIGN_SERVICES),
        "staff_choices": filter_form.fields["staff"].queryset,
        "poll_query": request.GET.urlencode(),
    }


@capability_required(CAPABILITY_VIEW_MONITORING)
@require_GET
def index(request):
    return render(request, "monitoring/index.html", _board_context(request))


@capability_required(CAPABILITY_VIEW_MONITORING)
@require_GET
def board(request):
    return render(request, "monitoring/_board.html", _board_context(request))


@capability_required(CAPABILITY_VIEW_MONITORING)
@require_POST
def update_status(request, pk, status):
    item = get_object_or_404(visible_work_items(request.user), pk=pk)
    form = ServiceStatusForm(request.POST)
    if form.is_valid():
        item = transition_service(
            item,
            status,
            actor=request.user,
            note=form.cleaned_data["note"],
        )
        record_security_event(
            SecurityEvent.Action.SERVICE_STATUS_CHANGED,
            request=request,
            target=item,
        )
        messages.success(request, f"{item.service_name} is now {item.get_status_display()}.")
    if _is_htmx(request):
        return render(request, "monitoring/_board.html", _board_context(request))
    return redirect("monitoring:index")


@capability_required(CAPABILITY_ASSIGN_SERVICES)
@require_http_methods(["GET"])
def assignments(request):
    items = list(
        visible_work_items(request.user)
        .select_related("appointment", "assigned_staff", "service")
        .order_by("appointment__appointment_date", "appointment__start_time", "position")
    )
    all_staff = list(
        User.objects.filter(
            role=User.Role.STAFF,
            is_active=True,
            is_active_staff_member=True,
            staff_profile__is_active=True,
        )
        .select_related("staff_profile")
        .prefetch_related("staff_profile__skills")
        .order_by("first_name", "last_name", "email")
    )
    for u in all_staff:
        u.display_label = (
            f"{u.get_full_name()} — {u.staff_profile.specialty}"
            if getattr(u, "staff_profile", None) and u.staff_profile.specialty
            else (u.get_full_name() or u.email)
        )
    for item in items:
        if item.service:
            item.qualified_staff = [
                u for u in all_staff
                if hasattr(u, "staff_profile") and u.staff_profile.can_perform(item.service)
            ]
        else:
            item.qualified_staff = all_staff
    return render(
        request,
        "monitoring/assignments.html",
        {"items": items, "staff_choices": all_staff},
    )


@capability_required(CAPABILITY_ASSIGN_SERVICES)
@require_POST
def assign(request, pk):
    item = get_object_or_404(visible_work_items(request.user), pk=pk)
    form = ServiceAssignmentForm(request.POST)
    if form.is_valid():
        try:
            item = assign_service(
                item, form.cleaned_data["assigned_staff"], actor=request.user
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            record_security_event(
                SecurityEvent.Action.SERVICE_ASSIGNED,
                request=request,
                target=item,
            )
            messages.success(request, f"Assignment updated for {item.service_name}.")
    if _is_htmx(request):
        return render(request, "monitoring/_board.html", _board_context(request))
    return redirect("monitoring:assignments")
