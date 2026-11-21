from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.authorization import CAPABILITY_ASSIGN_SERVICES, has_capability
from apps.accounts.models import User
from apps.bookings.models import (
    Appointment,
    AppointmentService,
    AppointmentStatusHistory,
)
from apps.services.models import StaffProfile

from .models import ServiceStatusHistory


MANAGER_TRANSITIONS = {
    AppointmentService.Status.PENDING: {AppointmentService.Status.APPROVED},
    AppointmentService.Status.APPROVED: {AppointmentService.Status.ONGOING},
    AppointmentService.Status.ONGOING: {AppointmentService.Status.COMPLETED},
}
STAFF_TRANSITIONS = {
    AppointmentService.Status.APPROVED: {AppointmentService.Status.ONGOING},
    AppointmentService.Status.ONGOING: {AppointmentService.Status.COMPLETED},
}


def visible_work_items(user):
    items = AppointmentService.objects.exclude(
        appointment__status__in=(
            Appointment.Status.UNVERIFIED,
            Appointment.Status.CANCELLED,
            Appointment.Status.REJECTED,
            Appointment.Status.NO_SHOW,
            Appointment.Status.EXPIRED,
        )
    )
    if user.is_owner or user.is_cashier:
        return items
    if user.is_service_staff:
        return items.filter(assigned_staff=user)
    return items.none()


def allowed_transitions(item, actor):
    if actor.is_owner or actor.is_cashier:
        transitions = MANAGER_TRANSITIONS
    elif actor.is_service_staff and item.assigned_staff_id == actor.pk:
        transitions = STAFF_TRANSITIONS
    else:
        return set()
    return transitions.get(item.status, set())


def _update_parent_status(appointment, actor, note):
    statuses = list(
        appointment.appointment_services.values_list("status", flat=True)
    )
    if not statuses:
        return
    if all(status == AppointmentService.Status.COMPLETED for status in statuses):
        new_status = Appointment.Status.COMPLETED
    elif any(
        status in (AppointmentService.Status.ONGOING, AppointmentService.Status.COMPLETED)
        for status in statuses
    ):
        new_status = Appointment.Status.ONGOING
    elif all(status == AppointmentService.Status.APPROVED for status in statuses):
        new_status = Appointment.Status.APPROVED
    else:
        new_status = Appointment.Status.PENDING
    if appointment.status == new_status:
        return
    old_status = appointment.status
    appointment.status = new_status
    appointment.save(update_fields=("status", "updated_at"))
    AppointmentStatusHistory.objects.create(
        appointment=appointment,
        from_status=old_status,
        to_status=new_status,
        actor=actor,
        note=note or "Synchronized from service monitoring",
    )


def transition_service(item, new_status, *, actor, note=""):
    note = " ".join(note.split())
    with transaction.atomic():
        item = (
            AppointmentService.objects.select_for_update()
            .select_related("appointment", "assigned_staff")
            .get(pk=item.pk)
        )
        appointment = Appointment.objects.select_for_update().get(
            pk=item.appointment_id
        )
        if new_status not in allowed_transitions(item, actor):
            raise PermissionDenied("That service status transition is not allowed.")
        old_status = item.status
        now = timezone.now()
        item.status = new_status
        fields = ["status", "updated_at"]
        if new_status == AppointmentService.Status.APPROVED:
            item.approved_at = now
            fields.append("approved_at")
        elif new_status == AppointmentService.Status.ONGOING:
            item.started_at = now
            item.expected_finish_at = now + timedelta(minutes=item.duration_minutes)
            fields.extend(("started_at", "expected_finish_at"))
        elif new_status == AppointmentService.Status.COMPLETED:
            item.completed_at = now
            fields.append("completed_at")
        item.save(update_fields=fields)
        ServiceStatusHistory.objects.create(
            appointment_service=item,
            user=actor,
            old_status=old_status,
            new_status=new_status,
            note=note,
        )
        _update_parent_status(appointment, actor, note)
    return item


def sync_services_for_appointment(appointment, appointment_status, *, actor, note=""):
    target = {
        Appointment.Status.PENDING: AppointmentService.Status.PENDING,
        Appointment.Status.APPROVED: AppointmentService.Status.APPROVED,
        Appointment.Status.ONGOING: AppointmentService.Status.ONGOING,
        Appointment.Status.COMPLETED: AppointmentService.Status.COMPLETED,
    }.get(appointment_status)
    if target is None:
        return
    now = timezone.now()
    rows = list(
        AppointmentService.objects.select_for_update().filter(appointment=appointment)
    )
    for item in rows:
        if item.status == target:
            continue
        old_status = item.status
        item.status = target
        fields = ["status", "updated_at"]
        if target == AppointmentService.Status.APPROVED:
            item.approved_at = now
            fields.append("approved_at")
        elif target == AppointmentService.Status.ONGOING:
            item.started_at = item.started_at or now
            item.expected_finish_at = item.expected_finish_at or (
                item.started_at + timedelta(minutes=item.duration_minutes)
            )
            fields.extend(("started_at", "expected_finish_at"))
        elif target == AppointmentService.Status.COMPLETED:
            item.completed_at = item.completed_at or now
            fields.append("completed_at")
        item.save(update_fields=fields)
        ServiceStatusHistory.objects.create(
            appointment_service=item,
            user=actor,
            old_status=old_status,
            new_status=target,
            note=note or "Synchronized from appointment status",
        )


def assign_service(item, staff, *, actor):
    if not has_capability(actor, CAPABILITY_ASSIGN_SERVICES):
        raise PermissionDenied("You cannot assign services.")
    with transaction.atomic():
        item = AppointmentService.objects.select_for_update().select_related(
            "appointment", "service"
        ).get(pk=item.pk)
        if staff is not None:
            staff = User.objects.select_for_update().select_related("staff_profile").filter(
                pk=staff.pk,
                role=User.Role.STAFF,
                is_active=True,
                is_active_staff_member=True,
                staff_profile__is_active=True,
            ).first()
            if staff is None:
                raise ValidationError("The selected staff member is unavailable.")
            if item.service and hasattr(staff, "staff_profile") and not staff.staff_profile.can_perform(item.service):
                raise ValidationError(
                    f"{staff.get_full_name() or staff.email} does not specialize in {item.service_name}."
                )
        item.assigned_staff = staff
        item.save(update_fields=("assigned_staff", "updated_at"))

        staff_ids = set(
            item.appointment.appointment_services.values_list(
                "assigned_staff_id", flat=True
            )
        )
        appointment_staff_id = staff_ids.pop() if len(staff_ids) == 1 else None
        if item.appointment.assigned_staff_id != appointment_staff_id:
            item.appointment.assigned_staff_id = appointment_staff_id
            item.appointment.save(update_fields=("assigned_staff", "updated_at"))
    return item
