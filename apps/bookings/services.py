import secrets
from datetime import datetime, timedelta
from urllib.parse import urljoin

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.emails import send_branded_email
from apps.accounts.models import User
from apps.customers.models import Customer
from apps.notifications.models import Notification
from apps.services.models import (
    Service,
    StaffProfile,
    StaffSchedule,
    StaffTimeBlock,
)

from .models import Appointment, AppointmentService, AppointmentStatusHistory, Booking


BLOCKING_STATUSES = (
    Appointment.Status.UNVERIFIED,
    Appointment.Status.PENDING,
    Appointment.Status.APPROVED,
    Appointment.Status.RESCHEDULED,
    Appointment.Status.ONGOING,
)
MANAGER_TRANSITIONS = {
    Appointment.Status.PENDING: {
        Appointment.Status.APPROVED,
        Appointment.Status.REJECTED,
        Appointment.Status.RESCHEDULED,
        Appointment.Status.CANCELLED,
    },
    Appointment.Status.APPROVED: {
        Appointment.Status.RESCHEDULED,
        Appointment.Status.ONGOING,
        Appointment.Status.COMPLETED,
        Appointment.Status.CANCELLED,
        Appointment.Status.NO_SHOW,
    },
    Appointment.Status.RESCHEDULED: {
        Appointment.Status.APPROVED,
        Appointment.Status.ONGOING,
        Appointment.Status.CANCELLED,
        Appointment.Status.NO_SHOW,
    },
    Appointment.Status.ONGOING: {
        Appointment.Status.COMPLETED,
        Appointment.Status.CANCELLED,
    },
}
STAFF_TRANSITIONS = {
    Appointment.Status.APPROVED: {
        Appointment.Status.ONGOING,
        Appointment.Status.NO_SHOW,
    },
    Appointment.Status.RESCHEDULED: {
        Appointment.Status.ONGOING,
        Appointment.Status.NO_SHOW,
    },
    Appointment.Status.ONGOING: {Appointment.Status.COMPLETED},
}


def _public_url(request, path):
    if settings.PUBLIC_BASE_URL:
        return urljoin(f"{settings.PUBLIC_BASE_URL.rstrip('/')}/", path.lstrip("/"))
    return request.build_absolute_uri(path)


def _new_verification_code():
    return f"{secrets.randbelow(100_000_000):08d}"


def _send_verification_email(booking, code, request):
    verify_url = _public_url(request, reverse("bookings:verify"))
    send_branded_email(
        booking.email,
        f"Verify booking {booking.reference}",
        "bookings/emails/verify",
        {"booking": booking, "code": code, "verify_url": verify_url},
    )


def _send_confirmed_email(booking, token, request):
    access_path = reverse(
        "bookings:public_status",
        kwargs={"reference": booking.reference, "token": token},
    )
    send_branded_email(
        booking.email,
        f"Booking received: {booking.reference}",
        "bookings/emails/confirmed",
        {"booking": booking, "access_url": _public_url(request, access_path)},
    )


def _send_status_email(appointment):
    if not appointment.email:
        return
    send_branded_email(
        appointment.email,
        f"Appointment update: {appointment.reference}",
        "bookings/emails/status_update",
        {"appointment": appointment},
    )


def _appointment_end(appointment_date, start_time, duration_minutes):
    start = datetime.combine(appointment_date, start_time)
    end = start + timedelta(minutes=duration_minutes)
    if end.date() != appointment_date:
        raise ValidationError("The selected services must finish on the appointment date.")
    return end.time()


def _lock_and_validate_staff(staff, lock=True):
    if staff is None:
        return None
    staff_qs = User.objects.filter(pk=staff.pk, role=User.Role.STAFF)
    if lock:
        staff_qs = staff_qs.select_for_update()
    staff = staff_qs.first()
    if staff is None or not staff.is_active or not staff.is_active_staff_member:
        raise ValidationError("The selected staff member is not available.")
    profile_qs = StaffProfile.objects.filter(user=staff)
    if lock:
        profile_qs = profile_qs.select_for_update()
    profile = profile_qs.first()
    if (
        profile is None
        or not profile.is_active
        or profile.availability_status != StaffProfile.Availability.AVAILABLE
    ):
        raise ValidationError("The selected staff member is not accepting appointments.")
    return staff


def _validate_staff_availability(
    staff,
    appointment_date,
    start_time,
    end_time,
    services=None,
    exclude_appointment_id=None,
    lock=True,
):
    from datetime import time
    staff = _lock_and_validate_staff(staff, lock=lock)
    if staff is None:
        return None

    if services and hasattr(staff, "staff_profile"):
        if not all(staff.staff_profile.can_perform(svc) for svc in services):
            raise ValidationError(f"{staff} is not qualified for all selected services.")

    weekday = appointment_date.weekday()
    schedules = list(staff.schedules.filter(day_of_week=weekday))
    if schedules:
        sched = schedules[0]
        if not sched.is_working:
            raise ValidationError(f"{staff} is not scheduled to work on {appointment_date.strftime('%A')}s.")
        if start_time < sched.start_time or end_time > sched.end_time:
            raise ValidationError(
                f"{staff}'s working hours on {appointment_date.strftime('%A')} are "
                f"{sched.start_time.strftime('%I:%M %p')} to {sched.end_time.strftime('%I:%M %p')}."
            )
        if sched.lunch_start and sched.lunch_end:
            if start_time < sched.lunch_end and end_time > sched.lunch_start:
                raise ValidationError(
                    f"{staff} is on lunch break from {sched.lunch_start.strftime('%I:%M %p')} to {sched.lunch_end.strftime('%I:%M %p')}."
                )
    else:
        if start_time < time(9, 0) or end_time > time(21, 0):
            raise ValidationError("Appointment must be within salon hours (09:00 AM - 09:00 PM).")

    blocked = staff.time_blocks.filter(
        date=appointment_date,
        start_time__lt=end_time,
        end_time__gt=start_time,
    ).exists()
    if blocked:
        raise ValidationError(f"{staff} has a scheduled leave or time block during this time.")

    _validate_no_overlap(staff, appointment_date, start_time, end_time, exclude=exclude_appointment_id)
    return staff


def _find_available_technician(
    appointment_date,
    start_time,
    end_time,
    services=None,
    exclude_appointment_id=None,
    lock=True,
):
    from datetime import time
    weekday = appointment_date.weekday()
    candidates_qs = (
        User.objects.filter(
            role=User.Role.STAFF,
            is_active=True,
            is_active_staff_member=True,
            staff_profile__is_active=True,
            staff_profile__availability_status=StaffProfile.Availability.AVAILABLE,
        )
        .select_related("staff_profile")
        .prefetch_related("staff_profile__skills", "schedules", "time_blocks")
    )
    if lock:
        candidates_qs = candidates_qs.select_for_update()
    candidates = list(candidates_qs)
    for staff in candidates:
        if services and hasattr(staff, "staff_profile"):
            if not all(staff.staff_profile.can_perform(svc) for svc in services):
                continue
        schedules = [s for s in staff.schedules.all() if s.day_of_week == weekday]
        if schedules:
            sched = schedules[0]
            if not sched.is_working or start_time < sched.start_time or end_time > sched.end_time:
                continue
            if sched.lunch_start and sched.lunch_end:
                if start_time < sched.lunch_end and end_time > sched.lunch_start:
                    continue
        else:
            if start_time < time(9, 0) or end_time > time(21, 0):
                continue
        blocked = any(
            b.date == appointment_date and b.start_time < end_time and b.end_time > start_time
            for b in staff.time_blocks.all()
        )
        if blocked:
            continue
        overlapping = Appointment.objects.filter(
            assigned_staff=staff,
            appointment_date=appointment_date,
            status__in=BLOCKING_STATUSES,
            start_time__lt=end_time,
            end_time__gt=start_time,
        )
        if exclude_appointment_id:
            overlapping = overlapping.exclude(pk=exclude_appointment_id)
        if overlapping.exists():
            continue
        return staff
    return None


def get_available_time_slots(appointment_date, service_ids, staff_id=None):
    from datetime import datetime, time, timedelta
    services = list(Service.objects.filter(pk__in=service_ids, is_active=True))
    if not services:
        return []
    duration = sum(s.duration_minutes for s in services)
    if duration <= 0:
        return []

    technician = None
    if staff_id:
        technician = User.objects.filter(pk=staff_id, role=User.Role.STAFF, is_active=True).first()

    available_slots = []
    cur_dt = datetime.combine(appointment_date, time(9, 0))
    closing_dt = datetime.combine(appointment_date, time(21, 0))
    now = timezone.localtime()

    while cur_dt + timedelta(minutes=duration) <= closing_dt:
        slot_start = cur_dt.time()
        slot_end = (cur_dt + timedelta(minutes=duration)).time()
        slot_aware = timezone.make_aware(cur_dt, timezone.get_current_timezone())

        if slot_aware > now:
            if technician:
                try:
                    _validate_staff_availability(
                        technician,
                        appointment_date,
                        slot_start,
                        slot_end,
                        services,
                        lock=False,
                    )
                    available_slots.append(slot_start.strftime("%H:%M"))
                except ValidationError:
                    pass
            else:
                found = _find_available_technician(
                    appointment_date,
                    slot_start,
                    slot_end,
                    services,
                    lock=False,
                )
                if found:
                    available_slots.append(slot_start.strftime("%H:%M"))

        cur_dt += timedelta(minutes=30)

    return available_slots


def _validate_no_overlap(staff, appointment_date, start_time, end_time, exclude=None):
    if staff is None:
        return
    overlapping = Appointment.objects.filter(
        assigned_staff=staff,
        appointment_date=appointment_date,
        status__in=BLOCKING_STATUSES,
        start_time__lt=end_time,
        end_time__gt=start_time,
    )
    if exclude:
        overlapping = overlapping.exclude(pk=exclude)
    if overlapping.exists():
        raise ValidationError("That staff member already has an appointment during this time.")


def _notification_recipients(appointment, include_managers=False):
    query = Q(pk=appointment.assigned_staff_id) if appointment.assigned_staff_id else Q(pk__in=[])
    if include_managers:
        query |= Q(role__in=(User.Role.OWNER, User.Role.CASHIER))
    return User.objects.filter(query, is_active=True).distinct()


def _create_notifications(appointment, event_type, title, message, include_managers=False):
    Notification.objects.bulk_create(
        [
            Notification(
                recipient=recipient,
                appointment=appointment,
                event_type=event_type,
                title=title,
                message=message,
            )
            for recipient in _notification_recipients(appointment, include_managers)
        ]
    )


def create_public_appointment(cleaned_data, services, request, auto_verify=False):
    now = timezone.now()
    service_ids = list(dict.fromkeys(item.pk for item in services))
    with transaction.atomic():
        services = list(
            Service.objects.select_for_update().filter(pk__in=service_ids, is_active=True)
        )
        if not services or len(services) != len(service_ids):
            raise ValidationError("Choose only currently available services.")
        duration = sum(item.duration_minutes for item in services)
        from decimal import Decimal
        total_price = sum((item.price for item in services), Decimal("0.00"))
        appointment_date = cleaned_data["appointment_date"]
        if appointment_date.weekday() in (4, 5, 6) and total_price <= Decimal("300.00"):
            has_existing = Appointment.objects.filter(
                appointment_date=appointment_date,
                status__in=BLOCKING_STATUSES,
            ).exists()
            if has_existing:
                raise ValidationError(
                    "Friday–Sunday Peak Policy: Online bookings on Friday, Saturday, and Sunday prioritize package and major services above ₱300 when other clients are booked. Services ₱300 and below are only accepted online if there are no existing bookings on that date/slot, or via walk-in when technicians are free."
                )
        end_time = _appointment_end(
            cleaned_data["appointment_date"], cleaned_data["start_time"], duration
        )

        requested_staff = cleaned_data.get("assigned_staff")
        if requested_staff is not None:
            staff = _validate_staff_availability(
                requested_staff,
                cleaned_data["appointment_date"],
                cleaned_data["start_time"],
                end_time,
                services,
            )
        else:
            staff = _find_available_technician(
                cleaned_data["appointment_date"],
                cleaned_data["start_time"],
                end_time,
                services,
            )
            if staff is None:
                raise ValidationError("No technician is available for this time slot. Please choose another time.")

        email = cleaned_data["email"].strip().lower()
        customer = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.is_customer:
            customer = Customer.objects.select_for_update().filter(user=request.user).first()
            if customer is None:
                customer = Customer.objects.select_for_update().filter(email__iexact=user.email).first()
                if customer is not None and customer.user_id is None:
                    customer.user = user
                    customer.save(update_fields=("user", "updated_at"))
                else:
                    customer = Customer.objects.create(
                        user=user,
                        first_name=cleaned_data.get("first_name") or user.first_name or user.email.split("@")[0],
                        last_name=cleaned_data.get("last_name") or user.last_name or "Client",
                        email=user.email,
                        phone=cleaned_data.get("phone", "") or getattr(user, "phone_number", ""),
                    )
            if customer.email.lower() != email:
                raise ValidationError("Use the email address connected to your customer account.")
        else:
            customer, _ = Customer.objects.get_or_create(
                email=email,
                defaults={
                    "first_name": cleaned_data["first_name"],
                    "last_name": cleaned_data["last_name"],
                    "phone": cleaned_data.get("phone", ""),
                },
            )
        changed = []
        for field in ("first_name", "last_name", "phone"):
            value = cleaned_data.get(field, "")
            if value and getattr(customer, field) != value:
                setattr(customer, field, value)
                changed.append(field)
        if changed:
            customer.save(update_fields=(*changed, "updated_at"))

        appointment = Appointment.objects.create(
            branch=cleaned_data.get("branch") or Appointment.Branch.PLARIDEL,
            customer=customer,
            customer_name_snapshot=customer.full_name,
            customer_email_snapshot=email,
            customer_phone_snapshot=cleaned_data.get("phone", ""),
            appointment_date=cleaned_data["appointment_date"],
            start_time=cleaned_data["start_time"],
            end_time=end_time,
            assigned_staff=staff,
            notes=cleaned_data.get("notes", ""),
            status=Appointment.Status.PENDING if auto_verify else Appointment.Status.UNVERIFIED,
            verified_at=now if auto_verify else None,
            expires_at=now + settings.BOOKING_UNVERIFIED_TIMEOUT,
        )
        AppointmentService.objects.bulk_create(
            [
                AppointmentService(
                    appointment=appointment,
                    service=service,
                    service_name=service.name,
                    duration_minutes=service.duration_minutes,
                    price=service.price,
                    assigned_staff=staff,
                    position=position,
                )
                for position, service in enumerate(services)
            ]
        )

        if auto_verify:
            token = appointment.issue_public_access_token(now + settings.BOOKING_ACCESS_TOKEN_TIMEOUT)
            appointment.save(
                update_fields=(
                    "public_access_token_digest",
                    "public_access_expires_at",
                    "updated_at",
                )
            )
            AppointmentStatusHistory.objects.create(
                appointment=appointment,
                from_status="",
                to_status=Appointment.Status.PENDING,
                actor=user if (user is not None and user.is_authenticated) else None,
                note="Submitted by verified customer account.",
            )
            _create_notifications(
                appointment,
                Notification.EventType.NEW,
                "New appointment request",
                f"{appointment.customer_name} requested {appointment.service_name}.",
                include_managers=True,
            )
            try:
                _send_confirmed_email(appointment, token, request)
            except Exception:
                pass
            appointment._issued_public_token = token
        else:
            code = _new_verification_code()
            appointment.set_verification_code(
                code,
                min(appointment.expires_at, now + settings.BOOKING_VERIFICATION_CODE_TIMEOUT),
            )
            appointment.save(
                update_fields=(
                    "verification_code_digest",
                    "verification_code_expires_at",
                    "verification_failed_attempts",
                    "verification_sent_at",
                    "updated_at",
                )
            )
            _send_verification_email(appointment, code, request)
    return appointment


def create_internal_appointment(
    cleaned_data,
    services,
    actor,
    request=None,
    booking_source=Appointment.BookingSource.WALK_IN,
    initial_status=Appointment.Status.APPROVED,
):
    now = timezone.now()
    service_ids = list(dict.fromkeys(item.pk for item in services))
    with transaction.atomic():
        services = list(
            Service.objects.select_for_update().filter(pk__in=service_ids, is_active=True)
        )
        if not services or len(services) != len(service_ids):
            raise ValidationError("Choose only currently available services.")
        duration = sum(item.duration_minutes for item in services)
        end_time = _appointment_end(
            cleaned_data["appointment_date"], cleaned_data["start_time"], duration
        )

        requested_staff = cleaned_data.get("assigned_staff")
        if requested_staff is not None:
            staff = _validate_staff_availability(
                requested_staff,
                cleaned_data["appointment_date"],
                cleaned_data["start_time"],
                end_time,
                services,
            )
        else:
            staff = _find_available_technician(
                cleaned_data["appointment_date"],
                cleaned_data["start_time"],
                end_time,
                services,
            )
            if staff is None:
                raise ValidationError("No technician is available for this time slot. Please choose another time.")

        email = cleaned_data.get("email", "").strip().lower()
        customer = cleaned_data.get("customer")
        if not customer:
            if email:
                customer, _ = Customer.objects.get_or_create(
                    email=email,
                    defaults={
                        "first_name": cleaned_data.get("first_name", "Walk-in"),
                        "last_name": cleaned_data.get("last_name", "Customer"),
                        "phone": cleaned_data.get("phone", ""),
                    },
                )
            else:
                customer = Customer.objects.create(
                    first_name=cleaned_data.get("first_name", "Walk-in"),
                    last_name=cleaned_data.get("last_name", "Customer"),
                    phone=cleaned_data.get("phone", ""),
                )

        appointment = Appointment.objects.create(
            branch=cleaned_data.get("branch") or Appointment.Branch.PLARIDEL,
            customer=customer,
            customer_name_snapshot=customer.full_name,
            customer_email_snapshot=customer.email or "",
            customer_phone_snapshot=customer.phone or cleaned_data.get("phone", ""),
            appointment_date=cleaned_data["appointment_date"],
            start_time=cleaned_data["start_time"],
            end_time=end_time,
            assigned_staff=staff,
            booking_source=booking_source,
            status=initial_status,
            notes=cleaned_data.get("notes", ""),
            created_by=actor,
            verified_at=now,
            expires_at=now + timedelta(days=365),
        )

        AppointmentService.objects.bulk_create(
            [
                AppointmentService(
                    appointment=appointment,
                    service=service,
                    service_name=service.name,
                    duration_minutes=service.duration_minutes,
                    price=service.price,
                    assigned_staff=staff,
                    status=(
                        AppointmentService.Status.APPROVED
                        if initial_status == Appointment.Status.APPROVED
                        else AppointmentService.Status.PENDING
                    ),
                    position=position,
                )
                for position, service in enumerate(services)
            ]
        )

        actor_name = actor.get_full_name() or getattr(actor, "email", "Admin")
        staff_name = staff.get_full_name() if staff else "Unassigned"
        AppointmentStatusHistory.objects.create(
            appointment=appointment,
            from_status="",
            to_status=initial_status,
            actor=actor,
            note=f"Appointment booked by {actor_name} with assigned technician {staff_name}",
        )

        if staff:
            _create_notifications(
                appointment,
                Notification.EventType.NEW,
                "New appointment assigned",
                f"You have been assigned to appointment #{appointment.booking_reference} on {appointment.appointment_date}.",
            )

    return appointment


def create_public_booking(cleaned_data, request):
    """Compatibility path for previously shipped free-text booking clients."""
    now = timezone.now()
    with transaction.atomic():
        booking = Booking(**cleaned_data, expires_at=now + settings.BOOKING_UNVERIFIED_TIMEOUT)
        code = _new_verification_code()
        booking.set_verification_code(
            code,
            min(booking.expires_at, now + settings.BOOKING_VERIFICATION_CODE_TIMEOUT),
        )
        booking.save()
        _send_verification_email(booking, code, request)
    return booking


def _find_unverified_for_update(reference, email=None):
    appointment_query = Appointment.objects.select_for_update().filter(
        booking_reference=reference.strip(), status=Appointment.Status.UNVERIFIED
    )
    if email is not None:
        appointment_query = appointment_query.filter(customer_email_snapshot=email)
    appointment = appointment_query.first()
    if appointment:
        return appointment
    booking_query = Booking.objects.select_for_update().filter(
        reference=reference.strip(), status=Booking.Status.UNVERIFIED
    )
    if email is not None:
        booking_query = booking_query.filter(email=email)
    return booking_query.first()


def resend_verification(reference, email, request):
    now = timezone.now()
    email = email.strip().lower()
    with transaction.atomic():
        booking = _find_unverified_for_update(reference, email)
        if booking is None:
            return False
        if booking.expires_at <= now:
            booking.expire_if_needed()
            return False
        if booking.verification_resend_count >= settings.BOOKING_MAX_RESENDS:
            return False
        if booking.verification_sent_at and booking.verification_sent_at + settings.BOOKING_RESEND_COOLDOWN > now:
            return False
        code = _new_verification_code()
        booking.set_verification_code(
            code,
            min(booking.expires_at, now + settings.BOOKING_VERIFICATION_CODE_TIMEOUT),
        )
        booking.verification_resend_count += 1
        booking.save(
            update_fields=(
                "verification_code_digest",
                "verification_code_expires_at",
                "verification_failed_attempts",
                "verification_sent_at",
                "verification_resend_count",
                "updated_at",
            )
        )
        _send_verification_email(booking, code, request)
    return True


def verify_public_booking(reference, code, request):
    now = timezone.now()
    with transaction.atomic():
        booking = _find_unverified_for_update(reference)
        if booking is None:
            return None, None
        if booking.expires_at <= now:
            booking.expire_if_needed()
            return None, None
        if (
            booking.verification_failed_attempts >= settings.BOOKING_VERIFICATION_MAX_ATTEMPTS
            or not booking.verification_code_expires_at
            or booking.verification_code_expires_at <= now
        ):
            return None, None
        if not booking.matches_verification_code(code.strip()):
            booking.verification_failed_attempts += 1
            booking.save(update_fields=("verification_failed_attempts", "updated_at"))
            return None, None

        token = booking.issue_public_access_token(now + settings.BOOKING_ACCESS_TOKEN_TIMEOUT)
        booking.status = (
            Appointment.Status.PENDING if isinstance(booking, Appointment) else Booking.Status.CONFIRMED
        )
        booking.verified_at = now
        booking.verification_code_digest = ""
        booking.save(
            update_fields=(
                "status",
                "verified_at",
                "verification_code_digest",
                "public_access_token_digest",
                "public_access_expires_at",
                "updated_at",
            )
        )
        if isinstance(booking, Appointment):
            AppointmentStatusHistory.objects.create(
                appointment=booking,
                from_status=Appointment.Status.UNVERIFIED,
                to_status=Appointment.Status.PENDING,
            )
            _create_notifications(
                booking,
                Notification.EventType.NEW,
                "New appointment request",
                f"{booking.customer_name} requested {booking.service_name}.",
                include_managers=True,
            )
        _send_confirmed_email(booking, token, request)
    return booking, token


def get_public_booking(reference, token):
    booking = Appointment.objects.filter(booking_reference=reference.strip()).first()
    if booking is None:
        booking = Booking.objects.filter(reference=reference.strip()).first()
    if booking is None or not booking.accepts_public_access_token(token):
        return None
    return booking


def cancel_public_booking(reference, token):
    with transaction.atomic():
        appointment = Appointment.objects.select_for_update().filter(booking_reference=reference).first()
        if appointment:
            if (
                not appointment.accepts_public_access_token(token)
                or appointment.status
                not in (
                    Appointment.Status.PENDING,
                    Appointment.Status.APPROVED,
                    Appointment.Status.RESCHEDULED,
                )
            ):
                return None
            previous = appointment.status
            appointment.status = Appointment.Status.CANCELLED
            appointment.cancelled_at = timezone.now()
            appointment.save(update_fields=("status", "cancelled_at", "updated_at"))
            AppointmentStatusHistory.objects.create(
                appointment=appointment,
                from_status=previous,
                to_status=appointment.status,
                note="Cancelled by customer",
            )
            _create_notifications(
                appointment,
                Notification.EventType.CANCELLED,
                "Appointment cancelled",
                f"{appointment.customer_name} cancelled {appointment.reference}.",
                include_managers=True,
            )
            return appointment

        booking = Booking.objects.select_for_update().filter(reference=reference).first()
        if (
            booking is None
            or not booking.accepts_public_access_token(token)
            or booking.status != Booking.Status.CONFIRMED
        ):
            return None
        booking.status = Booking.Status.CANCELLED
        booking.cancelled_at = timezone.now()
        booking.save(update_fields=("status", "cancelled_at", "updated_at"))
        return booking


def reschedule_public_booking(reference, token, scheduled_for):
    with transaction.atomic():
        appointment = Appointment.objects.select_for_update().filter(booking_reference=reference).first()
        if appointment:
            if (
                not appointment.accepts_public_access_token(token)
                or appointment.status
                not in (
                    Appointment.Status.PENDING,
                    Appointment.Status.APPROVED,
                    Appointment.Status.RESCHEDULED,
                )
            ):
                return None
            appointment_date = timezone.localtime(scheduled_for).date()
            start_time = timezone.localtime(scheduled_for).time().replace(tzinfo=None)
            end_time = _appointment_end(
                appointment_date, start_time, appointment.total_duration_minutes
            )
            staff = _validate_staff_availability(
                appointment.assigned_staff,
                appointment_date,
                start_time,
                end_time,
                services=list(appointment.selected_services.all()),
                exclude_appointment_id=appointment.pk,
            )
            previous = appointment.status
            appointment.appointment_date = appointment_date
            appointment.start_time = start_time
            appointment.end_time = end_time
            appointment.status = Appointment.Status.RESCHEDULED
            appointment.save(
                update_fields=(
                    "appointment_date",
                    "start_time",
                    "end_time",
                    "status",
                    "updated_at",
                )
            )
            AppointmentStatusHistory.objects.create(
                appointment=appointment,
                from_status=previous,
                to_status=appointment.status,
                note="Rescheduled by customer",
            )
            _create_notifications(
                appointment,
                Notification.EventType.RESCHEDULED,
                "Appointment rescheduled",
                f"{appointment.reference} requires approval for its new time.",
                include_managers=True,
            )
            return appointment

        booking = Booking.objects.select_for_update().filter(reference=reference).first()
        if (
            booking is None
            or not booking.accepts_public_access_token(token)
            or booking.status != Booking.Status.CONFIRMED
        ):
            return None
        booking.scheduled_for = scheduled_for
        booking.save(update_fields=("scheduled_for", "updated_at"))
        return booking


def _get_or_create_active_promo_code():
    from decimal import Decimal
    from apps.pos.models import PromoVoucher

    today = timezone.localdate()
    voucher = (
        PromoVoucher.objects.filter(
            is_active=True,
            valid_from__lte=today,
            valid_until__gte=today,
        )
        .order_by("-created_at")
        .first()
    )
    if voucher and (voucher.usage_limit == 0 or voucher.times_used < voucher.usage_limit):
        return voucher.code
    voucher, _ = PromoVoucher.objects.update_or_create(
        code="GETNAILED10",
        defaults={
            "description": "10% Approved Booking Exclusive Promo (Up to ₱200 off)",
            "discount_type": PromoVoucher.DiscountType.PERCENT,
            "discount_value": Decimal("10.00"),
            "min_spend": Decimal("0.00"),
            "max_discount_cap": Decimal("200.00"),
            "usage_limit": 0,
            "valid_from": today - timedelta(days=1),
            "valid_until": today + timedelta(days=365),
            "is_active": True,
        },
    )
    return voucher.code


def transition_appointment(appointment, new_status, actor, note=""):
    with transaction.atomic():
        appointment = (
            Appointment.objects.select_for_update()
            .select_related("customer__user")
            .get(pk=appointment.pk)
        )
        if actor.is_service_staff and not actor.is_owner:
            if appointment.assigned_staff_id != actor.pk:
                raise PermissionDenied("This appointment is not assigned to you.")
            transitions = STAFF_TRANSITIONS
        elif actor.is_owner or actor.is_cashier:
            transitions = MANAGER_TRANSITIONS
        else:
            raise PermissionDenied("You cannot update appointment statuses.")
        if new_status not in transitions.get(appointment.status, set()):
            raise ValidationError("That status transition is not allowed.")
        previous = appointment.status
        appointment.status = new_status
        if new_status == Appointment.Status.CANCELLED:
            appointment.cancelled_at = timezone.now()
            appointment.cancellation_reason = note
        elif new_status in (
            Appointment.Status.REJECTED,
            Appointment.Status.NO_SHOW,
            Appointment.Status.RESCHEDULED,
        ) and note:
            appointment.cancellation_reason = note

        if new_status == Appointment.Status.APPROVED and not appointment.promo_code_issued:
            appointment.promo_code_issued = _get_or_create_active_promo_code()

        appointment.save(
            update_fields=(
                "status",
                "cancelled_at",
                "cancellation_reason",
                "promo_code_issued",
                "updated_at",
            )
        )
        AppointmentStatusHistory.objects.create(
            appointment=appointment,
            from_status=previous,
            to_status=new_status,
            actor=actor,
            note=note,
        )
        from apps.monitoring.services import sync_services_for_appointment

        sync_services_for_appointment(appointment, new_status, actor=actor, note=note)
        event_type = {
            Appointment.Status.APPROVED: Notification.EventType.APPROVED,
            Appointment.Status.REJECTED: Notification.EventType.REJECTED,
            Appointment.Status.RESCHEDULED: Notification.EventType.RESCHEDULED,
            Appointment.Status.CANCELLED: Notification.EventType.CANCELLED,
            Appointment.Status.NO_SHOW: Notification.EventType.NO_SHOW_ALERT,
        }.get(new_status)
        if event_type:
            promo_suffix = (
                f" Promo Code: {appointment.promo_code_issued}"
                if new_status == Appointment.Status.APPROVED and appointment.promo_code_issued
                else ""
            )
            reason_suffix = f" Reason: {note}" if note else ""
            _create_notifications(
                appointment,
                event_type,
                f"Appointment {appointment.get_status_display().lower()}",
                f"{appointment.reference} is now {appointment.get_status_display().lower()}.{promo_suffix}{reason_suffix}",
            )
            customer_user = getattr(appointment.customer, "user", None)
            if customer_user and customer_user.is_active:
                Notification.objects.create(
                    recipient=customer_user,
                    appointment=appointment,
                    event_type=event_type,
                    title=(
                        f"Booking Approved! Promo Code: {appointment.promo_code_issued}"
                        if new_status == Appointment.Status.APPROVED and appointment.promo_code_issued
                        else f"Appointment {appointment.get_status_display()}"
                    ),
                    message=(
                        f"Your appointment {appointment.reference} at {appointment.get_branch_display()} on "
                        f"{appointment.appointment_date} at {appointment.start_time.strftime('%I:%M %p')} is now "
                        f"{appointment.get_status_display().lower()}.{promo_suffix}{reason_suffix}"
                    )[:500],
                )
            _send_status_email(appointment)
    return appointment


def update_appointment_schedule(
    appointment, *, appointment_date, start_time, assigned_staff, actor
):
    if not (actor.is_owner or actor.is_cashier):
        raise PermissionDenied("You cannot assign or reschedule appointments.")
    with transaction.atomic():
        appointment = Appointment.objects.select_for_update().get(pk=appointment.pk)
        end_time = _appointment_end(
            appointment_date, start_time, appointment.total_duration_minutes
        )
        staff = _validate_staff_availability(
            assigned_staff,
            appointment_date,
            start_time,
            end_time,
            services=list(appointment.selected_services.all()),
            exclude_appointment_id=appointment.pk,
        )
        schedule_changed = (
            appointment.appointment_date != appointment_date
            or appointment.start_time != start_time
        )
        previous = appointment.status
        appointment.appointment_date = appointment_date
        appointment.start_time = start_time
        appointment.end_time = end_time
        appointment.assigned_staff = staff
        appointment.requires_schedule_review = False
        if schedule_changed and appointment.status in (
            Appointment.Status.PENDING,
            Appointment.Status.APPROVED,
            Appointment.Status.RESCHEDULED,
        ):
            appointment.status = Appointment.Status.RESCHEDULED
        appointment.save(
            update_fields=(
                "appointment_date",
                "start_time",
                "end_time",
                "assigned_staff",
                "requires_schedule_review",
                "status",
                "updated_at",
            )
        )
        appointment.appointment_services.update(assigned_staff=staff)
        if appointment.status != previous:
            AppointmentStatusHistory.objects.create(
                appointment=appointment,
                from_status=previous,
                to_status=appointment.status,
                actor=actor,
                note="Schedule updated by management",
            )
        _create_notifications(
            appointment,
            Notification.EventType.RESCHEDULED if schedule_changed else Notification.EventType.NEW,
            "Appointment assignment updated",
            f"You are assigned to {appointment.reference} on {appointment.appointment_date}.",
        )
        if schedule_changed:
            _send_status_email(appointment)
    return appointment


def process_booking_reminders_and_grace_period(now=None):
    """
    1. Sends customer reminders on the day of the appointment and 4–5 hours before
       via email and SMS/phone notification log + in-app notification.
    2. Enforces the 10–15 minute grace period (no advance payment required; owner/staff
       remind via calls/text, and if 15 minutes have elapsed past start_time with the
       appointment still APPROVED/RESCHEDULED and not ONGOING/COMPLETED, alerts owner
       and staff that the client is a no-show and automatically marks NO_SHOW so the
       slot and assigned technician become immediately available).
    """
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    today = local_now.date()
    summary = {
        "day_of_reminders_sent": 0,
        "four_hour_reminders_sent": 0,
        "grace_period_released": 0,
    }

    active_today = list(
        Appointment.objects.filter(
            appointment_date=today,
            status__in=(Appointment.Status.APPROVED, Appointment.Status.RESCHEDULED),
        ).select_related("customer__user", "assigned_staff")
    )

    for appt in active_today:
        scheduled_dt = appt.scheduled_for
        minutes_until = (scheduled_dt - local_now).total_seconds() / 60.0

        # 1. Same-day morning / day-of reminder (email + phone/SMS notification)
        if appt.day_of_reminder_sent_at is None and minutes_until > 15:
            appt.day_of_reminder_sent_at = now
            appt.save(update_fields=("day_of_reminder_sent_at", "updated_at"))
            phone_display = appt.customer_phone_snapshot or (appt.customer.phone if appt.customer else "N/A")
            customer_user = getattr(appt.customer, "user", None)
            if customer_user and customer_user.is_active:
                Notification.objects.create(
                    recipient=customer_user,
                    appointment=appt,
                    event_type=Notification.EventType.REMINDER,
                    title=f"Same-Day Reminder: Appointment Today at {appt.start_time.strftime('%I:%M %p')}",
                    message=(
                        f"Reminder sent to {appt.email or 'email'} & SMS ({phone_display}): "
                        f"Your appointment {appt.reference} at {appt.get_branch_display()} is today at "
                        f"{appt.start_time.strftime('%I:%M %p')}. Please arrive on time (10–15 min grace period applies)."
                    )[:500],
                )
            _create_notifications(
                appt,
                Notification.EventType.REMINDER,
                f"Call/Text Reminder Due: {appt.customer_name} ({appt.start_time.strftime('%I:%M %p')})",
                (
                    f"Same-day reminder dispatched for {appt.reference} ({appt.get_branch_display()}). "
                    f"Client phone: {phone_display} | Email: {appt.email or 'N/A'}. No advance payment — please confirm via call/text."
                )[:500],
                include_managers=True,
            )
            _send_status_email(appt)
            summary["day_of_reminders_sent"] += 1

        # 2. 4–5 Hours Before Reminder (240 to 300 mins prior, or within 5 hours if not yet sent)
        if appt.four_hour_reminder_sent_at is None and 15 < minutes_until <= 300:
            appt.four_hour_reminder_sent_at = now
            appt.save(update_fields=("four_hour_reminder_sent_at", "updated_at"))
            phone_display = appt.customer_phone_snapshot or (appt.customer.phone if appt.customer else "N/A")
            customer_user = getattr(appt.customer, "user", None)
            if customer_user and customer_user.is_active:
                Notification.objects.create(
                    recipient=customer_user,
                    appointment=appt,
                    event_type=Notification.EventType.REMINDER,
                    title=f"4–5 Hours Reminder: {appt.reference} at {appt.start_time.strftime('%I:%M %p')}",
                    message=(
                        f"Email & SMS ({phone_display}) Reminder: Your appointment at {appt.get_branch_display()} "
                        f"starts at {appt.start_time.strftime('%I:%M %p')}. Please note our 10–15 minute grace period."
                    )[:500],
                )
            _send_status_email(appt)
            summary["four_hour_reminders_sent"] += 1

        # 3. 10–15 Minute Grace Period Auto-Release if client has not arrived/responded
        if minutes_until <= -15 and appt.late_alert_sent_at is None:
            with transaction.atomic():
                locked = (
                    Appointment.objects.select_for_update()
                    .select_related("customer__user", "assigned_staff")
                    .filter(
                        pk=appt.pk,
                        status__in=(Appointment.Status.APPROVED, Appointment.Status.RESCHEDULED),
                    )
                    .first()
                )
                if locked is None:
                    continue
                previous_status = locked.status
                staff_name = (
                    locked.assigned_staff.get_full_name()
                    if locked.assigned_staff
                    else "Assigned Technician"
                )
                reason_msg = (
                    "Auto-released after 15-minute grace period: Client did not arrive or respond to call/text. "
                    "Slot and technician are now automatically available."
                )
                locked.status = Appointment.Status.NO_SHOW
                locked.late_alert_sent_at = now
                locked.cancellation_reason = reason_msg
                locked.save(
                    update_fields=(
                        "status",
                        "late_alert_sent_at",
                        "cancellation_reason",
                        "updated_at",
                    )
                )
                AppointmentStatusHistory.objects.create(
                    appointment=locked,
                    from_status=previous_status,
                    to_status=Appointment.Status.NO_SHOW,
                    note=reason_msg,
                )
                from apps.monitoring.services import sync_services_for_appointment

                sync_services_for_appointment(
                    locked, Appointment.Status.NO_SHOW, actor=None, note=reason_msg
                )
                _create_notifications(
                    locked,
                    Notification.EventType.NO_SHOW_ALERT,
                    f"Client No-Show (15m Grace Elapsed): Slot & {staff_name} Now Available",
                    (
                        f"{locked.customer_name} ({locked.customer_phone_snapshot or 'no phone'}) did not arrive within "
                        f"10–15 mins of {locked.start_time.strftime('%I:%M %p')} ({locked.reference}). "
                        f"Time slot and technician {staff_name} are now automatically available for walk-ins/bookings."
                    )[:500],
                    include_managers=True,
                )
                customer_user = getattr(locked.customer, "user", None)
                if customer_user and customer_user.is_active:
                    Notification.objects.create(
                        recipient=customer_user,
                        appointment=locked,
                        event_type=Notification.EventType.NO_SHOW_ALERT,
                        title=f"Grace Period Expired: {locked.reference}",
                        message=(
                            f"We waited 15 minutes past your {locked.start_time.strftime('%I:%M %p')} slot at "
                            f"{locked.get_branch_display()}. Your slot has been released; please rebook or message us to reschedule."
                        )[:500],
                    )
                summary["grace_period_released"] += 1

    return summary
