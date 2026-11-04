from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.authorization import CAPABILITY_USE_POS, has_capability
from apps.accounts.models import User
from apps.bookings.models import Appointment, AppointmentService, AppointmentStatusHistory
from apps.customers.models import Customer
from apps.monitoring.models import ServiceStatusHistory
from apps.services.models import Service, StaffTimeBlock

from .models import CashierShift, Payment, PromoVoucher, ReceiptSequence, Sale, SaleItem


CENT = Decimal("0.01")
ZERO = Decimal("0.00")
SALEABLE_APPOINTMENT_STATUSES = (
    Appointment.Status.PENDING,
    Appointment.Status.APPROVED,
    Appointment.Status.RESCHEDULED,
    Appointment.Status.ONGOING,
    Appointment.Status.COMPLETED,
)


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_totals(
    items,
    discount_type,
    discount_value,
    voucher=None,
    discount_id_number="",
    max_cap=None,
):
    subtotal = money(sum((item["line_total"] for item in items), ZERO))
    discount_value = money(discount_value or ZERO)
    if discount_value < ZERO:
        raise ValidationError("Discounts cannot be negative.")

    if discount_type == Sale.DiscountType.NONE:
        if discount_value != ZERO:
            raise ValidationError("Choose a discount type before entering a discount.")
        discount_amount = ZERO
    elif discount_type == Sale.DiscountType.FIXED:
        discount_amount = discount_value
    elif discount_type == Sale.DiscountType.PERCENT:
        if discount_value > Decimal("100.00"):
            raise ValidationError("Percentage discounts cannot exceed 100%.")
        discount_amount = money(subtotal * discount_value / Decimal("100.00"))
    elif discount_type == Sale.DiscountType.SENIOR_CITIZEN:
        if not str(discount_id_number).strip():
            raise ValidationError("Senior Citizen ID number is required.")
        # RA 9994 20% discount on services
        discount_amount = money(subtotal * Decimal("20.00") / Decimal("100.00"))
        if max_cap is not None and max_cap > ZERO:
            discount_amount = min(discount_amount, max_cap)
    elif discount_type == Sale.DiscountType.PWD:
        if not str(discount_id_number).strip():
            raise ValidationError("PWD ID number is required.")
        # RA 10754 20% discount on services
        discount_amount = money(subtotal * Decimal("20.00") / Decimal("100.00"))
        if max_cap is not None and max_cap > ZERO:
            discount_amount = min(discount_amount, max_cap)
    elif discount_type == Sale.DiscountType.PROMO_VOUCHER:
        if voucher is None:
            raise ValidationError("A valid promo voucher is required.")
        is_valid, reason = voucher.is_valid_for(subtotal)
        if not is_valid:
            raise ValidationError(reason)
        discount_amount = money(voucher.calculate_discount(subtotal))
    else:
        raise ValidationError("Choose a valid discount type.")

    if discount_amount > subtotal:
        raise ValidationError("The discount cannot exceed the subtotal.")
    total = money(subtotal - discount_amount)
    if total < ZERO:
        raise ValidationError("Sale totals cannot be negative.")
    return subtotal, discount_amount, total


def _next_receipt_number():
    sequence = ReceiptSequence.objects.select_for_update().get(pk=1)
    while True:
        sequence.last_value += 1
        receipt = f"GN-{timezone.localdate():%Y%m%d}-{sequence.last_value:08d}"
        if not Sale.objects.filter(receipt_number=receipt).exists():
            sequence.save(update_fields=("last_value", "updated_at"))
            return receipt


def _locked_items(raw_items):
    if not raw_items:
        raise ValidationError("Add at least one service to the sale.")

    service_items = [item for item in raw_items if item.get("service")]
    if not service_items:
        raise ValidationError("Add at least one service to the sale.")

    service_ids = [item["service"].pk for item in service_items]
    services = {
        service.pk: service
        for service in Service.objects.select_for_update().filter(
            pk__in=service_ids, is_active=True
        )
    }
    if len(services) != len(set(service_ids)):
        raise ValidationError("One or more selected services are unavailable.")

    staff_ids = {
        item["assigned_staff"].pk
        for item in service_items
        if item.get("assigned_staff") is not None
    }
    staff_members = {
        staff.pk: staff
        for staff in User.objects.select_for_update().filter(
            pk__in=staff_ids,
            role=User.Role.STAFF,
            is_active=True,
            is_active_staff_member=True,
        )
    }
    if len(staff_members) != len(staff_ids):
        raise ValidationError("One or more assigned staff members are unavailable.")

    items = []
    for position, raw_item in enumerate(raw_items):
        if not raw_item.get("service"):
            continue
        quantity = int(raw_item.get("quantity") or 1)
        if quantity < 1 or quantity > 100:
            raise ValidationError("Quantities must be between 1 and 100.")
        service = services[raw_item["service"].pk]
        staff = raw_item.get("assigned_staff")
        staff = staff_members[staff.pk] if staff else None
        unit_price = money(service.price)
        items.append(
            {
                "service": service,
                "staff": staff,
                "name": service.name,
                "category": service.category.name,
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": money(unit_price * quantity),
                "position": position,
                "service_time": raw_item.get("service_time"),
            }
        )
    if not items:
        raise ValidationError("Add at least one service to the sale.")
    return items


def _validate_payment(payment_method, amount_tendered, total):
    amount_tendered = money(amount_tendered if amount_tendered is not None else total)
    if payment_method == Payment.Method.CASH:
        if amount_tendered < total:
            raise ValidationError("Cash tendered is insufficient for this sale.")
        return amount_tendered, money(amount_tendered - total)
    if payment_method not in Payment.Method.values:
        raise ValidationError("Choose a valid payment method.")
    if amount_tendered != total:
        raise ValidationError("Non-cash payments must match the sale total exactly.")
    return amount_tendered, ZERO


def create_sale(
    *,
    cashier,
    raw_items,
    payment_method,
    amount_tendered=None,
    payment_reference="",
    customer=None,
    appointment=None,
    walk_in_referral_number="",
    walk_in_customer_name="",
    discount_type=Sale.DiscountType.NONE,
    discount_value=ZERO,
    discount_id_number="",
    discount_id_name="",
    voucher=None,
    voucher_code=None,
    max_cap=None,
    shift=None,
    mark_appointment_completed=True,
):
    if not has_capability(cashier, CAPABILITY_USE_POS):
        raise PermissionDenied("You cannot process sales.")
    with transaction.atomic():
        if voucher_code and not voucher:
            from apps.pos.models import PromoVoucher
            voucher = PromoVoucher.objects.filter(code__iexact=voucher_code.strip()).first()
            if not voucher:
                raise ValidationError("Invalid promo voucher code.")
        items = _locked_items(raw_items)
        subtotal, discount_amount, total = calculate_totals(
            items,
            discount_type,
            discount_value,
            voucher=voucher,
            discount_id_number=discount_id_number,
            max_cap=max_cap,
        )
        amount_tendered, change = _validate_payment(
            payment_method, amount_tendered, total
        )

        locked_appointment = None
        is_addon_sale = False
        if appointment is not None:
            locked_appointment = (
                Appointment.objects.select_for_update()
                .select_related("customer")
                .filter(pk=appointment.pk)
                .first()
            )
            if locked_appointment is None:
                raise ValidationError("The selected appointment is unavailable.")
            if locked_appointment.status not in SALEABLE_APPOINTMENT_STATUSES:
                raise ValidationError("The selected appointment cannot be checked out.")
            if Sale.objects.filter(appointment=locked_appointment).exists():
                is_addon_sale = True
            customer = customer or locked_appointment.customer

        if shift is None:
            shift = CashierShift.objects.filter(
                cashier=cashier, status=CashierShift.Status.OPEN
            ).first()

        receipt_number = _next_receipt_number()
        walk_in_ref = (walk_in_referral_number or "").strip()
        if not customer and not locked_appointment and not walk_in_ref:
            import secrets
            walk_in_ref = f"WLK-{timezone.localdate():%Y%m%d}-{secrets.randbelow(9000) + 1000}"

        sale = Sale.objects.create(
            receipt_number=receipt_number,
            shift=shift,
            customer=customer,
            customer_name_snapshot=(
                customer.full_name
                if customer
                else (
                    f"{walk_in_customer_name.strip()} ({walk_in_ref})"
                    if walk_in_customer_name.strip() and walk_in_ref
                    else (
                        walk_in_customer_name.strip()
                        if walk_in_customer_name.strip()
                        else (f"Walk-in customer ({walk_in_ref})" if walk_in_ref else "Walk-in customer")
                    )
                )
            ),
            walk_in_referral_number=walk_in_ref,
            appointment=None if is_addon_sale else locked_appointment,
            subtotal=subtotal,
            discount_type=discount_type,
            discount_value=money(discount_value or ZERO),
            discount_amount=discount_amount,
            discount_id_number=discount_id_number.strip(),
            discount_id_name=discount_id_name.strip(),
            voucher=voucher,
            total=total,
            cashier=cashier,
            cashier_name_snapshot=str(cashier),
        )
        if voucher is not None:
            voucher.times_used += 1
            voucher.save(update_fields=("times_used", "updated_at"))

        SaleItem.objects.bulk_create(
            [
                SaleItem(
                    sale=sale,
                    service=item["service"],
                    assigned_staff=item["staff"],
                    service_name=item["name"],
                    service_category=item["category"],
                    staff_name=str(item["staff"]) if item["staff"] else "",
                    unit_price=item["unit_price"],
                    quantity=item["quantity"],
                    line_total=item["line_total"],
                    position=item["position"],
                )
                for item in items
            ]
        )
        Payment.objects.create(
            sale=sale,
            payment_method=payment_method,
            amount_tendered=amount_tendered,
            change=change,
            reference=payment_reference.strip(),
            recorded_by=cashier,
        )

        if locked_appointment is not None:
            if is_addon_sale:
                # Append newly purchased services to the active session
                existing_positions = [s.position for s in locked_appointment.appointment_services.all()]
                max_pos = max(existing_positions) if existing_positions else 0
                now_ts = timezone.now()
                target_svc_status = (
                    AppointmentService.Status.COMPLETED
                    if mark_appointment_completed
                    else AppointmentService.Status.ONGOING
                )
                for idx, itm in enumerate(items, start=1):
                    svc = itm["service"]
                    assigned = itm.get("staff") or locked_appointment.assigned_staff
                    new_svc = AppointmentService.objects.create(
                        appointment=locked_appointment,
                        service=svc,
                        service_name=itm["name"],
                        duration_minutes=svc.duration_minutes,
                        price=itm["unit_price"],
                        assigned_staff=assigned,
                        status=target_svc_status,
                        position=max_pos + idx,
                        started_at=now_ts,
                        completed_at=now_ts if mark_appointment_completed else None,
                        approved_at=now_ts,
                    )
                    ServiceStatusHistory.objects.create(
                        appointment_service=new_svc,
                        user=cashier,
                        old_status=AppointmentService.Status.PENDING,
                        new_status=target_svc_status,
                        note=f"Added to ongoing session via Sale #{receipt_number}",
                    )
                locked_appointment.notes = (
                    f"{locked_appointment.notes}\nAdd-on POS Sale #{receipt_number} ({payment_method})"
                ).strip()
                if mark_appointment_completed and locked_appointment.status != Appointment.Status.COMPLETED:
                    locked_appointment.status = Appointment.Status.COMPLETED
                locked_appointment.save(update_fields=("status", "notes", "updated_at"))
            elif (
                mark_appointment_completed
                and locked_appointment.status != Appointment.Status.COMPLETED
            ):
                previous = locked_appointment.status
                locked_appointment.status = Appointment.Status.COMPLETED
                locked_appointment.save(update_fields=("status", "updated_at"))
                AppointmentStatusHistory.objects.create(
                    appointment=locked_appointment,
                    from_status=previous,
                    to_status=Appointment.Status.COMPLETED,
                    actor=cashier,
                    note=f"Completed through sale {receipt_number}",
                )
                from apps.monitoring.services import sync_services_for_appointment

                sync_services_for_appointment(
                    locked_appointment,
                    Appointment.Status.COMPLETED,
                    actor=cashier,
                    note=f"Completed through sale {receipt_number}",
                )
            elif not mark_appointment_completed and locked_appointment.status in (
                Appointment.Status.PENDING,
                Appointment.Status.APPROVED,
            ):
                previous = locked_appointment.status
                locked_appointment.status = Appointment.Status.ONGOING
                locked_appointment.save(update_fields=("status", "updated_at"))
                AppointmentStatusHistory.objects.create(
                    appointment=locked_appointment,
                    from_status=previous,
                    to_status=Appointment.Status.ONGOING,
                    actor=cashier,
                    note=f"Started through sale {receipt_number}",
                )
                from apps.monitoring.services import sync_services_for_appointment

                sync_services_for_appointment(
                    locked_appointment,
                    Appointment.Status.ONGOING,
                    actor=cashier,
                    note=f"Started through sale {receipt_number}",
                )
        else:
            # Walk-in sale: automatically create Appointment and services for Service Monitoring
            appt_customer = customer
            if not appt_customer:
                ref_suffix = walk_in_ref or f"Client-{receipt_number}"
                appt_customer, _ = Customer.objects.get_or_create(
                    first_name="Walk-in",
                    last_name=ref_suffix,
                    defaults={"phone": ""},
                )

            appt_date = timezone.localdate()
            first_service_time = None
            for itm in items:
                if itm.get("service_time"):
                    first_service_time = itm["service_time"]
                    break
            if not first_service_time:
                first_service_time = timezone.localtime().time().replace(second=0, microsecond=0)

            total_duration = sum(itm["service"].duration_minutes for itm in items)
            appt_end_dt = datetime.combine(appt_date, first_service_time) + timedelta(minutes=total_duration)
            appt_end_time = appt_end_dt.time() if appt_end_dt.date() == appt_date else time(23, 59)

            primary_staff = None
            for itm in items:
                if itm.get("staff"):
                    primary_staff = itm["staff"]
                    break

            initial_status = (
                Appointment.Status.COMPLETED
                if mark_appointment_completed
                else Appointment.Status.ONGOING
            )

            now_ts = timezone.now()
            created_appt = Appointment.objects.create(
                branch=Appointment.Branch.PLARIDEL,
                customer=appt_customer,
                customer_name_snapshot=appt_customer.full_name,
                customer_email_snapshot=appt_customer.email or "",
                customer_phone_snapshot=appt_customer.phone or "",
                appointment_date=appt_date,
                start_time=first_service_time,
                end_time=appt_end_time,
                assigned_staff=primary_staff,
                booking_source=Appointment.BookingSource.WALK_IN,
                status=initial_status,
                notes=f"Walk-in POS Sale #{receipt_number}",
                created_by=cashier,
                verified_at=now_ts,
                expires_at=now_ts + timedelta(days=365),
            )

            service_target_status = (
                AppointmentService.Status.COMPLETED
                if mark_appointment_completed
                else AppointmentService.Status.ONGOING
            )
            for itm in items:
                svc = itm["service"]
                assigned = itm.get("staff") or primary_staff
                appt_svc = AppointmentService.objects.create(
                    appointment=created_appt,
                    service=svc,
                    service_name=itm["name"],
                    duration_minutes=svc.duration_minutes,
                    price=itm["unit_price"],
                    assigned_staff=assigned,
                    status=service_target_status,
                    position=itm["position"],
                    started_at=now_ts if not mark_appointment_completed else None,
                    completed_at=now_ts if mark_appointment_completed else None,
                    approved_at=now_ts,
                )

                ServiceStatusHistory.objects.create(
                    appointment_service=appt_svc,
                    user=cashier,
                    old_status=AppointmentService.Status.PENDING,
                    new_status=service_target_status,
                    note=f"POS Sale #{receipt_number} ({payment_method})",
                )

            AppointmentStatusHistory.objects.create(
                appointment=created_appt,
                from_status="",
                to_status=initial_status,
                actor=cashier,
                note=f"POS Sale #{receipt_number}",
            )

            sale.appointment = created_appt
            sale.save(update_fields=("appointment",))
            locked_appointment = created_appt

        # Automatically block technician time on their calendar planner
        sale_date = locked_appointment.appointment_date if locked_appointment else timezone.localdate()
        tech_last_end = {}
        for itm in items:
            assigned = itm.get("staff")
            if not assigned:
                continue
            svc = itm.get("service")
            duration = svc.duration_minutes if svc else 30
            item_time = itm.get("service_time")
            if not item_time:
                if assigned.pk in tech_last_end:
                    item_time = tech_last_end[assigned.pk]
                elif locked_appointment and locked_appointment.start_time:
                    item_time = locked_appointment.start_time
                else:
                    item_time = timezone.localtime().time().replace(second=0, microsecond=0)

            dt_start = datetime.combine(sale_date, item_time)
            dt_end = dt_start + timedelta(minutes=duration)
            end_time = dt_end.time() if dt_end.date() == sale_date else time(23, 59)

            if end_time > item_time:
                tech_last_end[assigned.pk] = end_time
                block_reason = f"POS #{receipt_number}: {itm['name']}"[:200]
                StaffTimeBlock.objects.create(
                    staff=assigned,
                    date=sale_date,
                    start_time=item_time,
                    end_time=end_time,
                    reason=block_reason,
                )
    return sale


def void_sale(sale, *, actor, reason):
    if not actor.is_owner:
        raise PermissionDenied("Only an owner can void sales.")
    reason = " ".join(reason.split())
    if not reason:
        raise ValidationError("A void reason is required.")
    with transaction.atomic():
        sale = Sale.objects.select_for_update().get(pk=sale.pk)
        if sale.status == Sale.Status.VOIDED:
            raise ValidationError("This sale is already voided.")
        sale.status = Sale.Status.VOIDED
        sale.void_reason = reason
        sale.voided_by = actor
        sale.voided_at = timezone.now()
        sale.save(
            update_fields=("status", "void_reason", "voided_by", "voided_at", "updated_at")
        )
        # Release calendar time blocks created by this sale
        StaffTimeBlock.objects.filter(reason__startswith=f"POS #{sale.receipt_number}").delete()
    return sale
