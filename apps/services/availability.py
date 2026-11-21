from datetime import datetime, time
from django.contrib.auth import get_user_model
from django.utils import timezone
from apps.services.models import StaffSchedule, StaffTimeBlock
from apps.bookings.models import Appointment

User = get_user_model()


def get_walk_in_technicians_status(target_datetime=None):
    """
    Calculates real-time availability for walk-in customers across all technicians.
    Returns:
        dict: {
            'technicians': [...],
            'available_count': int,
            'busy_count': int,
            'break_count': int,
            'off_duty_count': int,
            'current_time': datetime,
        }
    """
    if target_datetime is None:
        target_datetime = timezone.localtime()

    target_date = target_datetime.date()
    now_time = target_datetime.time()
    day_of_week = target_datetime.weekday()  # 0=Monday, 6=Sunday

    staff_qs = (
        User.objects.filter(role=User.Role.STAFF, is_active=True)
        .select_related("staff_profile")
        .prefetch_related("staff_profile__skills")
        .order_by("first_name", "last_name")
    )

    # Prefetch today's schedules, blocks, and appointments for efficiency
    schedules = {
        s.staff_id: s
        for s in StaffSchedule.objects.filter(day_of_week=day_of_week)
    }

    blocks_today = StaffTimeBlock.objects.filter(date=target_date)
    appts_today = Appointment.objects.filter(
        appointment_date=target_date,
        status__in=[
            Appointment.Status.APPROVED,
            Appointment.Status.ONGOING,
        ],
    ).select_related("customer")

    results = []
    available_count = 0
    busy_count = 0
    break_count = 0
    off_duty_count = 0

    for staff in staff_qs:
        specialty = (
            staff.staff_profile.specialty
            if hasattr(staff, "staff_profile") and staff.staff_profile.specialty
            else "Nail Specialist"
        )
        schedule = schedules.get(staff.pk)

        profile = getattr(staff, "staff_profile", None)
        skills = [s for s in profile.skills.all() if s.is_active] if profile else []
        item = {
            "staff_id": staff.pk,
            "name": staff.get_full_name() or staff.email,
            "specialty": specialty,
            "skill_ids": [str(s.pk) for s in skills],
            "skill_ids_csv": ",".join(str(s.pk) for s in skills),
            "skill_names": [s.name for s in skills],
            "status": "OFF_DUTY",
            "status_label": "Off Duty Today",
            "badge_class": "bg-secondary-subtle text-secondary border",
            "is_available": False,
            "free_until": "",
            "details": "",
            "active_client": "",
            "shift_hours": "",
        }

        if not schedule:
            item["status"] = "OFF_DUTY"
            item["status_label"] = "No Shift Scheduled"
            off_duty_count += 1
            results.append(item)
            continue

        shift_start = schedule.start_time
        shift_end = schedule.end_time
        item["shift_hours"] = f"{shift_start.strftime('%I:%M %p')} - {shift_end.strftime('%I:%M %p')}"

        if now_time < shift_start:
            item["status"] = "UPCOMING"
            item["status_label"] = f"Shift starts at {shift_start.strftime('%I:%M %p')}"
            item["badge_class"] = "bg-info-subtle text-info border border-info-subtle"
            off_duty_count += 1
            results.append(item)
            continue

        if now_time > shift_end:
            item["status"] = "SHIFT_ENDED"
            item["status_label"] = f"Shift ended at {shift_end.strftime('%I:%M %p')}"
            item["badge_class"] = "bg-secondary-subtle text-secondary border"
            off_duty_count += 1
            results.append(item)
            continue

        # Check if staff is on break or blocked right now
        active_block = next(
            (b for b in blocks_today if b.staff_id == staff.pk and b.start_time <= now_time < b.end_time),
            None,
        )
        if active_block:
            item["status"] = "ON_BREAK"
            reason_text = active_block.reason or "Break"
            item["status_label"] = f"On Break ({reason_text})"
            item["badge_class"] = "bg-warning-subtle text-warning border border-warning-subtle"
            item["details"] = f"Until {active_block.end_time.strftime('%I:%M %p')}"
            break_count += 1
            results.append(item)
            continue

        # Check if currently servicing an appointment
        active_appt = next(
            (a for a in appts_today if a.assigned_staff_id == staff.pk and a.start_time <= now_time < (a.end_time or time(23, 59))),
            None,
        )
        if active_appt:
            item["status"] = "IN_SERVICE"
            item["status_label"] = "In Service (Busy)"
            item["badge_class"] = "bg-primary-subtle text-primary border border-primary-subtle"
            item["active_client"] = active_appt.customer_name
            end_display = active_appt.end_time.strftime('%I:%M %p') if active_appt.end_time else "TBD"
            item["details"] = f"Client: {active_appt.customer_name} • Ends {end_display}"
            busy_count += 1
            results.append(item)
            continue

        # Staff is free right now! Calculate free until time
        next_appt = (
            appts_today.filter(assigned_staff_id=staff.pk, start_time__gte=now_time)
            .order_by("start_time")
            .first()
        )
        next_block = (
            blocks_today.filter(staff_id=staff.pk, start_time__gte=now_time)
            .order_by("start_time")
            .first()
        )

        next_busy_time = shift_end
        next_busy_label = f"Shift End ({shift_end.strftime('%I:%M %p')})"

        if next_appt and next_block:
            if next_appt.start_time <= next_block.start_time:
                next_busy_time = next_appt.start_time
                next_busy_label = f"Appt at {next_appt.start_time.strftime('%I:%M %p')}"
            else:
                next_busy_time = next_block.start_time
                next_busy_label = f"Break at {next_block.start_time.strftime('%I:%M %p')}"
        elif next_appt:
            next_busy_time = next_appt.start_time
            next_busy_label = f"Appt at {next_appt.start_time.strftime('%I:%M %p')}"
        elif next_block:
            next_busy_time = next_block.start_time
            next_busy_label = f"Break at {next_block.start_time.strftime('%I:%M %p')}"

        item["status"] = "AVAILABLE"
        item["status_label"] = "Available Now (Walk-In Ready)"
        item["badge_class"] = "bg-success text-white"
        item["is_available"] = True
        item["free_until"] = next_busy_label
        item["details"] = f"Ready for walk-in client • Free until {next_busy_label}"
        available_count += 1
        results.append(item)

    # Sort available technicians first
    results.sort(key=lambda x: (not x["is_available"], x["status"], x["name"]))

    return {
        "technicians": results,
        "available_count": available_count,
        "busy_count": busy_count,
        "break_count": break_count,
        "off_duty_count": off_duty_count,
        "current_time": target_datetime,
    }
