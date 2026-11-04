import calendar
from datetime import datetime, time, timedelta
from decimal import Decimal

from allauth.account.decorators import reauthentication_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import models, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.authorization import CAPABILITY_USE_POS, is_owner
from apps.accounts.decorators import capability_required, owner_required
from apps.accounts.models import User
from apps.bookings.models import (
    Appointment,
    AppointmentService,
    AppointmentStatusHistory,
)
from apps.bookings.services import BLOCKING_STATUSES
from apps.customers.models import Customer
from apps.services.models import Service, StaffProfile, StaffSchedule, StaffTimeBlock

from .forms import (
    CheckoutForm,
    CheckoutItemFormSet,
    CloseShiftForm,
    OpenShiftForm,
    PromoVoucherForm,
    SaleHistoryFilterForm,
    VoidSaleForm,
)
from .models import CashierShift, Payment, PromoVoucher, Sale
from .services import create_sale, void_sale


def _appointment_initial(form, appointment_id):
    if not appointment_id:
        return {}, []
    appointment = form.fields["appointment"].queryset.filter(pk=appointment_id).first()
    if appointment is None:
        return {}, []
    items = []
    elapsed_minutes = 0
    for row in appointment.appointment_services.select_related("service").order_by("position", "pk"):
        if row.service_id and row.service and row.service.is_active:
            row_start = (appointment.scheduled_for + timedelta(minutes=elapsed_minutes)).time()
            items.append(
                {
                    "service": row.service_id,
                    "assigned_staff": row.assigned_staff_id or appointment.assigned_staff_id,
                    "service_time": row_start.strftime("%H:%M"),
                    "quantity": 1,
                }
            )
            elapsed_minutes += row.duration_minutes
    return {"appointment": appointment, "customer": appointment.customer}, items


@capability_required(CAPABILITY_USE_POS)
@require_http_methods(["POST"])
def api_quick_create_customer(request):
    import json
    data = request.POST
    if not data:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            data = {}

    full_name = data.get("name", "").strip()
    first_name = data.get("first_name", "").strip()
    last_name = data.get("last_name", "").strip()
    phone = data.get("phone", "").strip()
    email = data.get("email", "").strip().lower()

    if full_name and not first_name:
        parts = full_name.split()
        first_name = parts[0]
        last_name = " ".join(parts[1:]) if len(parts) > 1 else "(Walk-in)"

    if not first_name:
        return JsonResponse({"status": "error", "message": "Paki-type ang pangalan ng customer."}, status=400)
    if not last_name:
        last_name = "(Walk-in)"

    existing = None
    if phone:
        existing = Customer.objects.filter(phone=phone).first()
    if not existing and email:
        existing = Customer.objects.filter(email=email).first()

    if existing:
        customer = existing
        created = False
    else:
        try:
            customer = Customer.objects.create(
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                email=email,
            )
            created = True
        except ValidationError as exc:
            return JsonResponse({"status": "error", "message": "; ".join(exc.messages)}, status=400)

    return JsonResponse({
        "status": "ok",
        "created": created,
        "customer": {
            "id": customer.pk,
            "name": customer.full_name,
            "first_name": customer.first_name,
            "last_name": customer.last_name,
            "phone": customer.phone or "",
            "email": customer.email or "",
        },
    })


@require_http_methods(["GET"])
def api_appointment_details(request, pk):
    appointment = (
        Appointment.objects.exclude(
            status__in=(
                Appointment.Status.UNVERIFIED,
                Appointment.Status.REJECTED,
                Appointment.Status.CANCELLED,
                Appointment.Status.EXPIRED,
            )
        )
        .filter(Q(sale__isnull=True) | Q(status=Appointment.Status.ONGOING), pk=pk)
        .select_related("customer", "assigned_staff")
        .first()
    )
    if not appointment:
        return JsonResponse(
            {"error": "Appointment not found or not eligible for checkout."},
            status=404,
        )
    items = []
    elapsed_minutes = 0
    for row in appointment.appointment_services.select_related("service").order_by("position", "pk"):
        if row.service_id and row.service and row.service.is_active:
            row_start = (appointment.scheduled_for + timedelta(minutes=elapsed_minutes)).time()
            items.append(
                {
                    "service_id": str(row.service_id),
                    "assigned_staff_id": str(row.assigned_staff_id or appointment.assigned_staff_id or ""),
                    "service_time": row_start.strftime("%H:%M"),
                }
            )
            elapsed_minutes += row.duration_minutes
    return JsonResponse(
        {
            "appointment_id": appointment.pk,
            "ref": appointment.booking_reference,
            "customer_id": str(appointment.customer_id) if appointment.customer_id else "",
            "customer_name": appointment.customer_name_snapshot,
            "appointment_date": appointment.appointment_date.isoformat(),
            "start_time": appointment.start_time.strftime("%H:%M"),
            "status": appointment.status,
            "is_ongoing": appointment.status == Appointment.Status.ONGOING,
            "items": items,
        }
    )


@capability_required(CAPABILITY_USE_POS)
@require_http_methods(["POST"])
def api_add_service_to_appointment(request, pk):
    import json
    data = request.POST
    if not data:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            data = {}

    appointment = get_object_or_404(
        Appointment.objects.select_related("customer", "assigned_staff"),
        pk=pk,
    )
    service_id = data.get("service_id")
    staff_id = data.get("staff_id")
    service_time_str = data.get("service_time", "").strip()

    if not service_id or not str(service_id).isdigit():
        return JsonResponse({"status": "error", "message": "Pumili ng serbisyo na idadagdag."}, status=400)

    service = Service.objects.filter(pk=int(service_id), is_active=True).first()
    if not service:
        return JsonResponse({"status": "error", "message": "Hindi mahanap ang napiling serbisyo."}, status=404)

    staff = None
    if staff_id and str(staff_id).isdigit():
        staff = User.objects.filter(pk=int(staff_id), role=User.Role.STAFF, is_active=True).first()

    with transaction.atomic():
        existing_positions = [s.position for s in appointment.appointment_services.all()]
        max_pos = max(existing_positions) if existing_positions else 0
        next_pos = max_pos + 1

        if service_time_str:
            try:
                s_time = datetime.strptime(service_time_str[:5], "%H:%M").time()
            except ValueError:
                s_time = timezone.localtime().time().replace(second=0, microsecond=0)
        else:
            s_time = appointment.end_time or appointment.start_time

        now_ts = timezone.now()
        svc_target_status = AppointmentService.Status.ONGOING if appointment.status == Appointment.Status.ONGOING else AppointmentService.Status.APPROVED

        assigned_tech = staff or appointment.assigned_staff
        new_appt_svc = AppointmentService.objects.create(
            appointment=appointment,
            service=service,
            service_name=service.name,
            duration_minutes=service.duration_minutes,
            price=service.price,
            assigned_staff=assigned_tech,
            status=svc_target_status,
            position=next_pos,
            started_at=now_ts if svc_target_status == AppointmentService.Status.ONGOING else None,
            approved_at=now_ts,
        )

        total_duration = sum(s.duration_minutes for s in appointment.appointment_services.all())
        new_end_dt = datetime.combine(appointment.appointment_date, appointment.start_time) + timedelta(minutes=total_duration)
        appointment.end_time = new_end_dt.time() if new_end_dt.date() == appointment.appointment_date else time(23, 59)
        appointment.save(update_fields=["end_time", "updated_at"])

        if assigned_tech:
            dt_start = datetime.combine(appointment.appointment_date, s_time)
            dt_end = dt_start + timedelta(minutes=service.duration_minutes)
            end_time = dt_end.time() if dt_end.date() == appointment.appointment_date else time(23, 59)
            if end_time > s_time:
                StaffTimeBlock.objects.create(
                    staff=assigned_tech,
                    date=appointment.appointment_date,
                    start_time=s_time,
                    end_time=end_time,
                    reason=f"Added to Session #{appointment.booking_reference}: {service.name}"[:200],
                )

        AppointmentStatusHistory.objects.create(
            appointment=appointment,
            from_status=appointment.status,
            to_status=appointment.status,
            actor=request.user,
            note=f"Added service {service.name} (₱{service.price:0.2f}) to session.",
        )

    return JsonResponse({
        "status": "ok",
        "message": f"Matagumpay na naidagdag ang '{service.name}' sa active session ni {appointment.customer_name_snapshot}!",
        "service": {
            "id": new_appt_svc.pk,
            "service_id": service.pk,
            "name": service.name,
            "price": str(service.price),
            "duration": service.duration_minutes,
            "staff_name": assigned_tech.get_full_name() if assigned_tech else "Unassigned",
        },
        "appointment": {
            "id": appointment.pk,
            "ref": appointment.booking_reference,
            "end_time": appointment.end_time.strftime("%I:%M %p"),
        }
    })


@capability_required(CAPABILITY_USE_POS)
@require_http_methods(["GET"])
def api_technician_slots(request):
    """
    Returns available time slots for a specific technician on a given date (defaults to today).
    Takes into account working shifts, lunch breaks, calendar time blocks, and appointments.
    """
    staff_id = request.GET.get("staff_id")
    if not staff_id or not staff_id.isdigit():
        return JsonResponse({"status": "error", "message": "Valid staff_id is required."}, status=400)

    staff = User.objects.filter(pk=int(staff_id), role=User.Role.STAFF, is_active=True).first()
    if not staff:
        return JsonResponse({"status": "error", "message": "Technician not found."}, status=404)

    profile = getattr(staff, "staff_profile", None)
    if profile and profile.availability_status == StaffProfile.Availability.ON_LEAVE:
        return JsonResponse({
            "status": "off_duty",
            "staff_id": staff.pk,
            "staff_name": staff.get_full_name(),
            "message": f"{staff.get_full_name()} is currently on leave.",
            "slots": [],
        })

    date_str = request.GET.get("date")
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    service_id = request.GET.get("service_id")
    duration = 30
    if service_id and service_id.isdigit():
        service = Service.objects.filter(pk=int(service_id), is_active=True).first()
        if service:
            duration = service.duration_minutes

    weekday = target_date.weekday()
    schedules = list(staff.schedules.filter(day_of_week=weekday))
    if schedules:
        sched = schedules[0]
        if not sched.is_working:
            return JsonResponse({
                "status": "off_duty",
                "staff_id": staff.pk,
                "staff_name": staff.get_full_name(),
                "message": f"{staff.get_full_name()} is off duty on {target_date.strftime('%A')}s.",
                "slots": [],
            })
        sched_start = sched.start_time
        sched_end = sched.end_time
        lunch_start = sched.lunch_start
        lunch_end = sched.lunch_end
    else:
        sched_start = time(9, 0)
        sched_end = time(21, 0)
        lunch_start = None
        lunch_end = None

    time_blocks = list(staff.time_blocks.filter(date=target_date))
    appts = list(
        Appointment.objects.filter(
            assigned_staff=staff,
            appointment_date=target_date,
            status__in=BLOCKING_STATUSES,
        )
    )
    exclude_appt_id = request.GET.get("exclude_appointment_id")
    if exclude_appt_id and exclude_appt_id.isdigit():
        appts = [a for a in appts if a.pk != int(exclude_appt_id)]

    today = timezone.localdate()
    now_time = timezone.localtime().time()

    slots = []
    cur_dt = datetime.combine(target_date, sched_start)
    end_limit_dt = datetime.combine(target_date, sched_end)

    while cur_dt + timedelta(minutes=duration) <= end_limit_dt:
        slot_start = cur_dt.time()
        slot_end = (cur_dt + timedelta(minutes=duration)).time()

        # Check lunch
        is_lunch = False
        if lunch_start and lunch_end:
            if slot_start < lunch_end and slot_end > lunch_start:
                is_lunch = True

        # Check time blocks
        is_blocked = False
        if not is_lunch:
            for tb in time_blocks:
                if tb.start_time < slot_end and tb.end_time > slot_start:
                    is_blocked = True
                    break

        # Check appointments
        is_appt = False
        if not is_lunch and not is_blocked:
            for apt in appts:
                apt_end = apt.end_time or slot_end
                if apt.start_time < slot_end and apt_end > slot_start:
                    is_appt = True
                    break

        if not is_lunch and not is_blocked and not is_appt:
            is_now = False
            if target_date == today:
                slot_plus_30 = (cur_dt + timedelta(minutes=30)).time()
                if slot_start <= now_time < slot_plus_30:
                    is_now = True

            label = slot_start.strftime("%I:%M %p")
            if is_now:
                label += " (Now • Current Time)"

            slots.append({
                "time": slot_start.strftime("%H:%M"),
                "label": label,
                "is_now": is_now,
            })

        cur_dt += timedelta(minutes=30)

    shift_display = f"{sched_start.strftime('%I:%M %p')} - {sched_end.strftime('%I:%M %p')}"
    if lunch_start and lunch_end:
        shift_display += f" (Lunch: {lunch_start.strftime('%I:%M %p')} - {lunch_end.strftime('%I:%M %p')})"

    return JsonResponse({
        "status": "ok",
        "staff_id": staff.pk,
        "staff_name": staff.get_full_name(),
        "shift_display": shift_display,
        "slots_count": len(slots),
        "slots": slots,
    })


@capability_required(CAPABILITY_USE_POS)
@require_http_methods(["GET", "POST"])
def index(request):
    form = CheckoutForm(request.POST if request.method == "POST" else None)
    initial_items = []
    if request.method == "GET":
        initial, initial_items = _appointment_initial(form, request.GET.get("appointment"))
        if initial:
            form = CheckoutForm(initial=initial)
        elif request.GET.get("assigned_staff"):
            initial_items = [{"assigned_staff": request.GET.get("assigned_staff")}]
    item_formset = CheckoutItemFormSet(
        request.POST if request.method == "POST" else None,
        prefix="items",
        initial=initial_items,
    )
    active_shift = CashierShift.objects.filter(
        cashier=request.user, status=CashierShift.Status.OPEN
    ).first()

    if request.method == "POST" and form.is_valid() and item_formset.is_valid():
        raw_items = [
            item_form.cleaned_data
            for item_form in item_formset.forms
            if item_form.cleaned_data
            and item_form.cleaned_data.get("service")
        ]
        chosen_customer = form.cleaned_data["customer"]
        walk_in_name = request.POST.get("walk_in_customer_name", "").strip()
        walk_in_phone = request.POST.get("walk_in_customer_phone", "").strip()
        save_to_system = request.POST.get("save_walk_in_as_customer") in ("1", "true", "on")

        if not chosen_customer and walk_in_name and save_to_system:
            parts = walk_in_name.split()
            fn = parts[0]
            ln = " ".join(parts[1:]) if len(parts) > 1 else "(Walk-in)"
            cust_kwargs = {"first_name": fn, "last_name": ln}
            if walk_in_phone:
                cust_kwargs["phone"] = walk_in_phone
            try:
                chosen_customer, _ = Customer.objects.get_or_create(**cust_kwargs)
            except Exception:
                chosen_customer = None

        try:
            sale = create_sale(
                cashier=request.user,
                raw_items=raw_items,
                customer=chosen_customer,
                appointment=form.cleaned_data["appointment"],
                walk_in_referral_number=form.cleaned_data.get("walk_in_referral_number", ""),
                walk_in_customer_name=walk_in_name,
                discount_type=form.cleaned_data["discount_type"],
                discount_value=form.cleaned_data["discount_value"],
                discount_id_number=form.cleaned_data.get("discount_id_number", ""),
                discount_id_name=form.cleaned_data.get("discount_id_name", ""),
                voucher=form.cleaned_data.get("voucher_obj"),
                max_cap=form.cleaned_data.get("max_discount_cap"),
                payment_method=form.cleaned_data["payment_method"],
                amount_tendered=(
                    None
                    if form.cleaned_data["payment_method"] in (Payment.Method.GCASH, Payment.Method.MAYA)
                    else form.cleaned_data["amount_tendered"]
                ),
                payment_reference=form.cleaned_data["payment_reference"],
                shift=active_shift,
                mark_appointment_completed=form.cleaned_data[
                    "mark_appointment_completed"
                ],
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            confirmation = f"Sale {sale.receipt_number} completed."
            if sale.payment_method == Payment.Method.CASH:
                confirmation = (
                    f"✅ Cash Tendered: ₱{sale.amount_tendered:.2f} received. "
                    f"Total: ₱{sale.total:.2f}. Change due: ₱{sale.change:.2f}."
                )
                if sale.appointment:
                    try:
                        from apps.notifications.models import Notification
                        for staff_user in User.objects.filter(
                            role__in=(User.Role.OWNER, User.Role.CASHIER), is_active=True
                        ):
                            Notification.objects.create(
                                recipient=staff_user,
                                appointment=sale.appointment,
                                event_type=Notification.EventType.APPROVED,
                                title=f"Payment Received: {sale.receipt_number}",
                                message=(
                                    f"Cash Tendered: ₱{sale.amount_tendered:.2f} | "
                                    f"Change Due: ₱{sale.change:.2f} for Appointment #{sale.appointment.booking_reference}."
                                ),
                            )
                    except Exception:
                        pass
            messages.success(request, confirmation)
            return redirect("pos:receipt", receipt_number=sale.receipt_number)
    active_staff_qs = list(
        User.objects.filter(
            role=User.Role.STAFF,
            is_active=True,
            is_active_staff_member=True,
            staff_profile__is_active=True,
        ).select_related("staff_profile").prefetch_related("staff_profile__skills")
    )
    all_services = list(
        Service.objects.filter(is_active=True).select_related("category").order_by("category__name", "name")
    )

    service_qualified_staff = {}
    for svc in all_services:
        qualified = []
        for u in active_staff_qs:
            profile = getattr(u, "staff_profile", None)
            if profile and profile.can_perform(svc):
                spec_label = f" — {profile.specialty}" if profile.specialty else ""
                qualified.append({
                    "id": str(u.id),
                    "name": f"{u.get_full_name() or u.email}{spec_label}",
                })
        service_qualified_staff[str(svc.id)] = qualified

    staff_qualified_services = {}
    for u in active_staff_qs:
        profile = getattr(u, "staff_profile", None)
        if profile:
            if not profile.skills.exists():
                svc_ids = [str(s.id) for s in all_services]
            else:
                svc_ids = [str(s.id) for s in profile.skills.filter(is_active=True)]
        else:
            svc_ids = [str(s.id) for s in all_services]
        staff_qualified_services[str(u.id)] = svc_ids

    all_staff_options = [
        {
            "id": str(u.id),
            "name": f"{u.get_full_name() or u.email}{' — ' + u.staff_profile.specialty if getattr(u, 'staff_profile', None) and u.staff_profile.specialty else ''}",
        }
        for u in active_staff_qs
    ]

    all_services_catalog = []
    from itertools import groupby
    for cat_name, services_iter in groupby(all_services, key=lambda s: s.category.name if s.category else "Other Treatments"):
        services_in_cat = list(services_iter)
        all_services_catalog.append({
            "category": cat_name,
            "services": [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "category": cat_name,
                    "price": str(s.price),
                    "duration": s.duration_minutes,
                    "label": f"{s.name} — ₱{s.price:0.2f} ({s.duration_minutes}m)",
                }
                for s in services_in_cat
            ],
        })

    today = timezone.localdate()
    today_active_appts = {
        appt.customer_id: {
            "id": appt.pk,
            "ref": appt.booking_reference,
            "status": appt.status,
            "status_display": appt.get_status_display(),
            "time_display": appt.start_time.strftime("%I:%M %p"),
            "staff_name": appt.assigned_staff.get_full_name() if appt.assigned_staff else "",
            "services_summary": ", ".join(row.service.name for row in appt.appointment_services.all() if row.service),
        }
        for appt in Appointment.objects.filter(
            appointment_date=today,
            status__in=(Appointment.Status.APPROVED, Appointment.Status.ONGOING),
        ).select_related("assigned_staff").prefetch_related("appointment_services__service")
        if appt.customer_id
    }

    customers_catalog = [
        {
            "id": str(c.id),
            "name": c.full_name or c.email,
            "phone": c.phone or "",
            "email": c.email or "",
            "active_session": today_active_appts.get(c.id),
        }
        for c in form.fields["customer"].queryset
    ]

    ongoing_sessions_count = 0
    eligible_appointments = []
    for appt in form.fields["appointment"].queryset.select_related("customer", "assigned_staff").prefetch_related("appointment_services__service"):
        is_ongoing = appt.status == Appointment.Status.ONGOING and appt.appointment_date == today
        if is_ongoing:
            ongoing_sessions_count += 1
            slot = "ongoing"
        else:
            hour = appt.start_time.hour
            if 9 <= hour < 13:
                slot = "morning"
            elif 13 <= hour < 17:
                slot = "afternoon"
            elif 17 <= hour < 22:
                slot = "evening"
            else:
                slot = "other"

        svcs = [
            row.service.name
            for row in appt.appointment_services.all()
            if row.service
        ]
        services_summary = ", ".join(svcs) if svcs else "Custom Appointment"

        eligible_appointments.append({
            "id": str(appt.id),
            "ref": appt.booking_reference,
            "customer_id": str(appt.customer_id) if appt.customer_id else "",
            "customer_name": appt.customer_name_snapshot,
            "customer_phone": appt.customer_phone_snapshot or (appt.customer.phone if appt.customer else ""),
            "date": appt.appointment_date.isoformat(),
            "is_today": appt.appointment_date == today,
            "is_ongoing": is_ongoing,
            "date_display": appt.appointment_date.strftime("%b %d, %Y"),
            "time_display": appt.start_time.strftime("%I:%M %p"),
            "time_raw": appt.start_time.strftime("%H:%M"),
            "slot": slot,
            "services_summary": services_summary,
            "staff_name": (appt.assigned_staff.get_full_name() or appt.assigned_staff.email) if appt.assigned_staff else "Any Staff",
            "status": appt.status,
            "status_display": appt.get_status_display(),
        })

    initial_mode = request.GET.get("mode", "")
    if not initial_mode:
        if request.GET.get("appointment") or form.initial.get("appointment") or (request.method == "POST" and request.POST.get("appointment")):
            initial_mode = "booked"
        else:
            initial_mode = "walkin"

    return render(
        request,
        "pos/index.html",
        {
            "form": form,
            "item_formset": item_formset,
            "active_shift": active_shift,
            "service_prices": {
                str(pk): str(price)
                for pk, price in Service.objects.filter(is_active=True).values_list(
                    "pk", "price"
                )
            },
            "service_qualified_staff": service_qualified_staff,
            "staff_qualified_services": staff_qualified_services,
            "all_staff_options": all_staff_options,
            "all_services_catalog": all_services_catalog,
            "customers_catalog": customers_catalog,
            "eligible_appointments": eligible_appointments,
            "ongoing_sessions_count": ongoing_sessions_count,
            "initial_mode": initial_mode,
        },
    )



@capability_required(CAPABILITY_USE_POS)
def history(request):
    sales = Sale.objects.visible_to(request.user).select_related(
        "customer", "cashier", "payment"
    )
    filter_form = SaleHistoryFilterForm(request.GET or None)
    if filter_form.is_valid():
        query = filter_form.cleaned_data.get("q")
        if query:
            sales = sales.filter(
                Q(receipt_number__icontains=query)
                | Q(customer_name_snapshot__icontains=query)
            )

        status = filter_form.cleaned_data.get("status")
        if status:
            sales = sales.filter(status=status)

        period = filter_form.cleaned_data.get("period")
        start_date = filter_form.cleaned_data.get("start_date")
        end_date = filter_form.cleaned_data.get("end_date")
        single_date = filter_form.cleaned_data.get("date")
        start_time = filter_form.cleaned_data.get("start_time")
        end_time = filter_form.cleaned_data.get("end_time")

        today = timezone.localdate()
        if period == "today":
            start_date = today
            end_date = today
        elif period == "yesterday":
            start_date = today - timedelta(days=1)
            end_date = start_date
        elif period == "this_week":
            start_date = today - timedelta(days=today.weekday())
            end_date = start_date + timedelta(days=6)
        elif period == "this_month":
            start_date = today.replace(day=1)
            _, last_day = calendar.monthrange(today.year, today.month)
            end_date = today.replace(day=last_day)
        elif period == "last_month":
            first_of_this_month = today.replace(day=1)
            last_of_prev_month = first_of_this_month - timedelta(days=1)
            start_date = last_of_prev_month.replace(day=1)
            end_date = last_of_prev_month
        elif single_date and not start_date and not end_date:
            start_date = single_date
            end_date = single_date

        tz = timezone.get_current_timezone()
        from datetime import time as dt_time

        if (start_time or end_time) and not start_date and not end_date:
            start_date = today
            end_date = today

        if start_date and end_date:
            s_time = start_time or dt_time(0, 0, 0)
            e_time = end_time or dt_time(23, 59, 59, 999999)
            start_dt = timezone.make_aware(datetime.combine(start_date, s_time), tz)
            end_dt = timezone.make_aware(datetime.combine(end_date, e_time), tz)
            sales = sales.filter(created_at__gte=start_dt, created_at__lte=end_dt)
        elif start_date:
            s_time = start_time or dt_time(0, 0, 0)
            start_dt = timezone.make_aware(datetime.combine(start_date, s_time), tz)
            sales = sales.filter(created_at__gte=start_dt)
        elif end_date:
            e_time = end_time or dt_time(23, 59, 59, 999999)
            end_dt = timezone.make_aware(datetime.combine(end_date, e_time), tz)
            sales = sales.filter(created_at__lte=end_dt)

    totals = sales.aggregate(
        total_revenue=models.Sum("total", filter=Q(status=Sale.Status.COMPLETED)),
        completed_count=models.Count("pk", filter=Q(status=Sale.Status.COMPLETED)),
        voided_count=models.Count("pk", filter=Q(status=Sale.Status.VOIDED)),
        all_count=models.Count("pk"),
    )
    period_summary = {
        "total_revenue": totals["total_revenue"] or Decimal("0.00"),
        "completed_count": totals["completed_count"] or 0,
        "voided_count": totals["voided_count"] or 0,
        "total_count": totals["all_count"] or 0,
    }

    params = request.GET.copy()
    params.pop("page", None)
    query_string = params.urlencode()

    page = Paginator(sales, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "pos/history.html",
        {
            "page": page,
            "filter_form": filter_form,
            "period_summary": period_summary,
            "query_string": query_string,
        },
    )


@capability_required(CAPABILITY_USE_POS)
def receipt(request, receipt_number):
    sale = get_object_or_404(
        Sale.objects.visible_to(request.user)
        .select_related("customer", "appointment", "cashier", "payment")
        .prefetch_related("items"),
        receipt_number=receipt_number,
    )
    return render(request, "pos/receipt.html", {"sale": sale})


@owner_required
def void_approval(request):
    sales = Sale.objects.filter(status=Sale.Status.COMPLETED).select_related(
        "cashier", "payment"
    )[:100]
    return render(request, "pos/void_list.html", {"sales": sales})


@owner_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def void_detail(request, receipt_number):
    sale = get_object_or_404(
        Sale.objects.select_related("cashier", "payment").prefetch_related("items"),
        receipt_number=receipt_number,
    )
    form = VoidSaleForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            void_sale(sale, actor=request.user, reason=form.cleaned_data["reason"])
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, f"Sale {sale.receipt_number} was voided.")
            return redirect("pos:void_approval")
    return render(request, "pos/void_detail.html", {"sale": sale, "form": form})


@capability_required(CAPABILITY_USE_POS)
def shift_dashboard(request):
    active_shift = CashierShift.objects.filter(
        cashier=request.user, status=CashierShift.Status.OPEN
    ).first()

    current_stats = None
    if active_shift:
        sales = active_shift.sales.filter(status=Sale.Status.COMPLETED)
        from apps.pos.models import Payment
        payments = Payment.objects.filter(sale__in=sales)
        cash_sales = (
            payments.filter(payment_method=Payment.Method.CASH).aggregate(
                s=models.Sum("sale__total")
            )["s"]
            or Decimal("0.00")
        )
        ewallet_sales = (
            payments.filter(
                payment_method__in=[Payment.Method.GCASH, Payment.Method.MAYA]
            ).aggregate(s=models.Sum("sale__total"))["s"]
            or Decimal("0.00")
        )
        card_sales = (
            payments.filter(payment_method=Payment.Method.CARD).aggregate(
                s=models.Sum("sale__total")
            )["s"]
            or Decimal("0.00")
        )
        bank_sales = (
            payments.filter(payment_method=Payment.Method.BANK).aggregate(
                s=models.Sum("sale__total")
            )["s"]
            or Decimal("0.00")
        )
        total_sales = sales.aggregate(s=models.Sum("total"))["s"] or Decimal("0.00")
        expected_cash = active_shift.opening_cash + cash_sales
        current_stats = {
            "sales_count": sales.count(),
            "cash_sales": cash_sales,
            "ewallet_sales": ewallet_sales,
            "card_sales": card_sales,
            "bank_sales": bank_sales,
            "total_sales": total_sales,
            "expected_cash": expected_cash,
        }

    recent_shifts = (
        CashierShift.objects.all()
        if is_owner(request.user)
        else CashierShift.objects.filter(cashier=request.user)
    )[:10]

    return render(
        request,
        "pos/shift_dashboard.html",
        {
            "active_shift": active_shift,
            "current_stats": current_stats,
            "recent_shifts": recent_shifts,
        },
    )


@capability_required(CAPABILITY_USE_POS)
@require_http_methods(["GET", "POST"])
def shift_open(request):
    active = CashierShift.objects.filter(
        cashier=request.user, status=CashierShift.Status.OPEN
    ).first()
    if active:
        messages.info(request, f"You already have an active shift #{active.pk} open.")
        return redirect("pos:shift_dashboard")

    form = OpenShiftForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        shift = CashierShift.objects.create(
            cashier=request.user,
            opening_cash=form.cleaned_data["opening_cash"],
            notes=form.cleaned_data["notes"],
            status=CashierShift.Status.OPEN,
        )
        messages.success(
            request,
            f"Shift #{shift.pk} opened with starting drawer float of ₱{shift.opening_cash:,.2f}.",
        )
        return redirect("pos:shift_dashboard")
    return render(request, "pos/shift_open.html", {"form": form})


@capability_required(CAPABILITY_USE_POS)
@require_http_methods(["GET", "POST"])
def shift_close(request):
    active = CashierShift.objects.filter(
        cashier=request.user, status=CashierShift.Status.OPEN
    ).first()
    if not active:
        messages.warning(request, "You do not have an open shift to reconcile.")
        return redirect("pos:shift_dashboard")

    sales = active.sales.filter(status=Sale.Status.COMPLETED)
    from apps.pos.models import Payment
    payments = Payment.objects.filter(sale__in=sales)
    cash_sales = (
        payments.filter(payment_method=Payment.Method.CASH).aggregate(
            s=models.Sum("sale__total")
        )["s"]
        or Decimal("0.00")
    )
    ewallet_sales = (
        payments.filter(
            payment_method__in=[Payment.Method.GCASH, Payment.Method.MAYA]
        ).aggregate(s=models.Sum("sale__total"))["s"]
        or Decimal("0.00")
    )
    expected_cash = active.opening_cash + cash_sales

    form = CloseShiftForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        closing_cash = form.cleaned_data["closing_cash"]
        notes = form.cleaned_data["notes"]
        active.reconcile(closing_cash, request.user, notes=notes)
        messages.success(
            request,
            f"Shift #{active.pk} successfully closed. Variance: ₱{active.cash_variance:+,.2f}.",
        )
        return redirect("pos:shift_reconciliation", pk=active.pk)

    return render(
        request,
        "pos/shift_close.html",
        {
            "form": form,
            "shift": active,
            "cash_sales": cash_sales,
            "ewallet_sales": ewallet_sales,
            "expected_cash": expected_cash,
        },
    )


@capability_required(CAPABILITY_USE_POS)
def shift_reconciliation(request, pk):
    shift = get_object_or_404(CashierShift, pk=pk)
    if not is_owner(request.user) and shift.cashier != request.user:
        raise PermissionDenied("You do not have permission to view this till reconciliation.")
    return render(request, "pos/shift_reconciliation.html", {"shift": shift})


@capability_required(CAPABILITY_USE_POS)
def shift_history(request):
    shifts = (
        CashierShift.objects.all()
        if is_owner(request.user)
        else CashierShift.objects.filter(cashier=request.user)
    )
    date_filter = request.GET.get("date")
    if date_filter:
        shifts = shifts.filter(opened_at__date=date_filter)
    cashier_id = request.GET.get("cashier")
    if is_owner(request.user) and cashier_id and cashier_id.isdigit():
        shifts = shifts.filter(cashier_id=cashier_id)

    page = Paginator(shifts, 20).get_page(request.GET.get("page"))
    cashiers = User.objects.filter(role__in=[User.Role.CASHIER, User.Role.OWNER, User.Role.STAFF])
    return render(
        request,
        "pos/shift_history.html",
        {"page": page, "cashiers": cashiers, "date_filter": date_filter},
    )


@owner_required
def voucher_list(request):
    vouchers = PromoVoucher.objects.all()
    return render(request, "pos/voucher_list.html", {"vouchers": vouchers})


@owner_required
@require_http_methods(["GET", "POST"])
def voucher_create(request):
    form = PromoVoucherForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        voucher = form.save()
        messages.success(request, f"Promo voucher {voucher.code} created.")
        return redirect("pos:voucher_list")
    return render(request, "pos/voucher_form.html", {"form": form})
