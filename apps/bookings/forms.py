import re
from datetime import datetime

from django import forms
from django.utils import timezone

from apps.accounts.models import User
from apps.customers.models import Customer
from apps.services.models import Service, StaffProfile

from .models import Appointment, Booking
from .services import MANAGER_TRANSITIONS, STAFF_TRANSITIONS


FORM_CONTROL = {"class": "form-control"}
BOOKING_REFERENCE_RE = re.compile(r"^(?:BK-[A-Za-z0-9_-]{1,29}|\d{6,32})$", re.IGNORECASE)
BOOKING_ACCESS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class PublicBookingForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = (
            "customer_name",
            "email",
            "phone_number",
            "service_name",
            "scheduled_for",
        )
        widgets = {
            "customer_name": forms.TextInput(attrs=FORM_CONTROL),
            "email": forms.EmailInput(attrs={**FORM_CONTROL, "autocomplete": "email"}),
            "phone_number": forms.TextInput(attrs=FORM_CONTROL),
            "service_name": forms.TextInput(attrs=FORM_CONTROL),
            "scheduled_for": forms.DateTimeInput(
                attrs={**FORM_CONTROL, "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
        }

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean_scheduled_for(self):
        scheduled_for = self.cleaned_data["scheduled_for"]
        if scheduled_for <= timezone.now():
            raise forms.ValidationError("Choose a future appointment time.")
        return scheduled_for


from apps.bookings.security import (
    generate_anti_spam_timestamp,
    validate_submission_speed,
    log_spam_blocked,
)


class PublicContactForm(forms.Form):
    branch = forms.ChoiceField(
        label="Salon Branch / Location",
        choices=((Appointment.Branch.PLARIDEL, "Plaridel Branch (Unit 14 G/F Rocka Commercial Complex, Tabang, Plaridel, Bulacan)"),),
        initial=Appointment.Branch.PLARIDEL,
        required=False,
        widget=forms.HiddenInput(),
    )
    customer = forms.ModelChoiceField(
        queryset=Customer.objects.none(),
        required=False,
        empty_label="-- New Customer / Walk-in --",
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    first_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs=FORM_CONTROL))
    last_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs=FORM_CONTROL))
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={**FORM_CONTROL, "autocomplete": "email"}),
    )
    phone = forms.CharField(
        max_length=30,
        required=False,
        widget=forms.TextInput(attrs={**FORM_CONTROL, "autocomplete": "tel"}),
    )
    # Anti-Spam Security Features
    website_url_check = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "tabindex": "-1",
                "autocomplete": "off",
                "style": "display:none!important;position:absolute;left:-9999px;opacity:0;",
                "aria-hidden": "true",
            }
        ),
    )
    form_load_token = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )
    # Consultation Feature
    request_consultation = forms.BooleanField(
        required=False,
        label="Request Nail & Health Consultation (Complimentary with service)",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    nail_condition = forms.ChoiceField(
        required=False,
        label="Nail Condition",
        choices=[
            ("", "Select nail condition (Optional)"),
            ("NATURAL", "Natural Nails (Healthy)"),
            ("BRITTLE", "Brittle / Weak / Damaged"),
            ("ACRYLIC_EXISTING", "Existing Acrylic / Gel Overlay"),
            ("INGROWN", "Ingrown Prone / Sensitive Cuticles"),
            ("OTHER", "Other Special Concern"),
        ],
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    nail_allergies = forms.CharField(
        required=False,
        max_length=200,
        label="Known Allergies / Skin Sensitivities",
        widget=forms.TextInput(
            attrs={**FORM_CONTROL, "placeholder": "e.g., Gel polish, latex, acetone, or none"}
        ),
    )
    notes = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={**FORM_CONTROL, "rows": 3}),
    )

    def __init__(self, *args, is_internal=False, **kwargs):
        super().__init__(*args, **kwargs)
        if is_internal:
            self.fields["customer"].queryset = Customer.objects.all().order_by("first_name", "last_name")
            self.fields["email"].required = False
            self.fields["email"].help_text = "Optional for in-salon walk-ins."
        else:
            del self.fields["customer"]

        # Stamp anti-spam token if not bound or empty
        if not self.is_bound and "form_load_token" in self.fields:
            self.fields["form_load_token"].initial = generate_anti_spam_timestamp()

    def clean_branch(self):
        return self.cleaned_data.get("branch") or Appointment.Branch.PLARIDEL

    def clean_email(self):
        email = self.cleaned_data.get("email", "")
        return email.strip().lower() if email else ""

    def clean(self):
        cleaned_data = super().clean()

        # 1. Anti-spam honeypot detection
        honeypot_val = cleaned_data.get("website_url_check")
        if honeypot_val:
            log_spam_blocked(None, "Honeypot field triggered")
            raise forms.ValidationError("Spam submission detected.")

        # 2. Anti-spam submission speed check
        token = cleaned_data.get("form_load_token")
        if token and not validate_submission_speed(token, min_seconds=1.5):
            log_spam_blocked(None, "Submission speed too rapid (< 1.5s)")
            raise forms.ValidationError(
                "Submission was processed too quickly. Please take a moment to review before submitting."
            )

        cust = cleaned_data.get("customer")
        if cust:
            if not cleaned_data.get("first_name"):
                cleaned_data["first_name"] = cust.first_name
            if not cleaned_data.get("last_name"):
                cleaned_data["last_name"] = cust.last_name
            if not cleaned_data.get("email"):
                cleaned_data["email"] = cust.email or ""
            if not cleaned_data.get("phone"):
                cleaned_data["phone"] = cust.phone or ""

        # 3. Format structured Consultation data into notes
        req_consult = cleaned_data.get("request_consultation")
        condition = cleaned_data.get("nail_condition")
        allergies = cleaned_data.get("nail_allergies")
        custom_notes = cleaned_data.get("notes", "").strip()

        if req_consult or condition or allergies:
            consult_lines = ["[NAIL CONSULTATION & HEALTH ASSESSMENT]"]
            if req_consult:
                consult_lines.append("• Client requested complimentary Nail Consultation.")
            if condition:
                dict_cond = dict(self.fields["nail_condition"].choices)
                consult_lines.append(f"• Nail Condition: {dict_cond.get(condition, condition)}")
            if allergies:
                consult_lines.append(f"• Sensitivities/Allergies: {allergies.strip()}")
            if custom_notes:
                consult_lines.append(f"• Client Remarks: {custom_notes}")
            cleaned_data["notes"] = "\n".join(consult_lines)

        return cleaned_data


class PublicServiceForm(forms.Form):
    services = forms.ModelMultipleChoiceField(
        queryset=Service.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Choose at least one service."},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["services"].queryset = Service.objects.filter(is_active=True).select_related(
            "category"
        )


class PublicScheduleForm(forms.Form):
    branch = forms.ChoiceField(
        label="Salon Branch",
        choices=((Appointment.Branch.PLARIDEL, "Plaridel Branch (Unit 14 G/F Rocka Commercial Complex, Tabang, Plaridel, Bulacan)"),),
        initial=Appointment.Branch.PLARIDEL,
        required=False,
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    appointment_date = forms.DateField(
        label="Appointment Date",
        widget=forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
    )
    assigned_staff = forms.ModelChoiceField(
        label="Preferred Technician / Staff",
        queryset=User.objects.none(),
        required=False,
        empty_label="No preference (Any available technician)",
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    start_time = forms.TimeField(
        label="Start Time",
        widget=forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
    )

    def clean_branch(self):
        return self.cleaned_data.get("branch") or Appointment.Branch.PLARIDEL

    def __init__(self, *args, services=None, is_internal=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.services = list(services or [])
        self.is_internal = is_internal
        qs = User.objects.filter(
            role=User.Role.STAFF,
            is_active=True,
            is_active_staff_member=True,
            staff_profile__is_active=True,
            staff_profile__availability_status=StaffProfile.Availability.AVAILABLE,
        ).select_related("staff_profile").order_by("first_name", "last_name", "email")

        if services:
            qualified_ids = [
                u.pk for u in qs
                if hasattr(u, "staff_profile") and all(u.staff_profile.can_perform(s) for s in services)
            ]
            qs = qs.filter(pk__in=qualified_ids)

        self.fields["assigned_staff"].queryset = qs
        self.fields["assigned_staff"].label_from_instance = (
            lambda u: f"{u.get_full_name()} — {u.staff_profile.specialty}"
            if getattr(u, "staff_profile", None) and u.staff_profile.specialty
            else u.get_full_name() or u.email
        )

    def clean(self):
        from decimal import Decimal
        from .services import BLOCKING_STATUSES

        cleaned_data = super().clean()
        appointment_date = cleaned_data.get("appointment_date")
        start_time = cleaned_data.get("start_time")
        if appointment_date and start_time:
            scheduled_for = timezone.make_aware(
                datetime.combine(appointment_date, start_time),
                timezone.get_current_timezone(),
            )
            if scheduled_for <= timezone.now():
                raise forms.ValidationError("Choose a future appointment time.")

            if not self.is_internal and self.services and appointment_date.weekday() in (4, 5, 6):
                total_price = sum((s.price for s in self.services), Decimal("0.00"))
                if total_price <= Decimal("300.00"):
                    has_bookings = Appointment.objects.filter(
                        appointment_date=appointment_date,
                        status__in=BLOCKING_STATUSES,
                    ).exists()
                    if has_bookings:
                        raise forms.ValidationError(
                            "Friday–Sunday Peak Policy: Online bookings on Friday, Saturday, and Sunday prioritize package and major services above ₱300 when other clients are booked. Services ₱300 and below are only accepted online if there are no existing bookings on that slot, or via walk-in when technicians are free."
                        )
        return cleaned_data


class AppointmentFilterForm(forms.Form):
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                **FORM_CONTROL,
                "placeholder": "Search reference, customer, email, phone...",
                "class": "form-control",
            }
        ),
    )
    date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
    )
    status = forms.ChoiceField(
        required=False,
        choices=(("", "All statuses"), *Appointment.Status.choices),
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    service = forms.ModelChoiceField(
        required=False,
        queryset=Service.objects.filter(is_active=True).select_related("category").order_by("category__name", "name"),
        empty_label="All services",
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    staff = forms.ModelChoiceField(
        required=False,
        queryset=User.objects.filter(role=User.Role.STAFF, is_active=True).select_related("staff_profile"),
        empty_label="All specialists",
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    source = forms.ChoiceField(
        required=False,
        choices=(("", "All sources"), *Appointment.BookingSource.choices),
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    branch = forms.ChoiceField(
        required=False,
        choices=(("", "All branches"), *Appointment.Branch.choices),
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    sort = forms.ChoiceField(
        required=False,
        choices=(
            ("-appointment_date", "Date: Newest first"),
            ("appointment_date", "Date: Oldest first"),
            ("start_time", "Time: Earliest first"),
            ("-start_time", "Time: Latest first"),
            ("customer_name_snapshot", "Customer Name: A - Z"),
            ("-customer_name_snapshot", "Customer Name: Z - A"),
            ("status", "Status: A - Z"),
        ),
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        staff_qs = (
            User.objects.filter(role=User.Role.STAFF, is_active=True)
            .select_related("staff_profile")
            .prefetch_related("staff_profile__skills")
            .order_by("first_name", "last_name", "email")
        )
        selected_service_id = self.data.get("service") if self.is_bound else self.initial.get("service")
        if selected_service_id and str(selected_service_id).isdigit():
            svc = Service.objects.filter(pk=int(selected_service_id)).first()
            if svc:
                qualified_ids = [
                    u.pk for u in staff_qs
                    if hasattr(u, "staff_profile") and u.staff_profile.can_perform(svc)
                ]
                staff_qs = staff_qs.filter(pk__in=qualified_ids)
        self.fields["staff"].queryset = staff_qs
        self.fields["staff"].label_from_instance = (
            lambda u: f"{u.get_full_name()} — {u.staff_profile.specialty}"
            if getattr(u, "staff_profile", None) and u.staff_profile.specialty
            else (u.get_full_name() or u.email)
        )


class AppointmentStatusForm(forms.Form):
    status = forms.ChoiceField(widget=forms.Select(attrs=FORM_CONTROL))
    note = forms.CharField(
        required=False,
        max_length=500,
        label="Reason / Status Note",
        widget=forms.Textarea(
            attrs={
                **FORM_CONTROL,
                "rows": 3,
                "placeholder": "Required when cancelling, rejecting, rescheduling, or marking no-show (e.g. Client did not arrive / sudden cancellation / requested reschedule)...",
            }
        ),
    )

    def __init__(self, *args, appointment, actor, **kwargs):
        super().__init__(*args, **kwargs)
        transitions = (
            STAFF_TRANSITIONS
            if actor.is_service_staff and not actor.is_owner
            else MANAGER_TRANSITIONS
        )
        allowed = transitions.get(appointment.status, set())
        self.fields["status"].choices = [
            choice for choice in Appointment.Status.choices if choice[0] in allowed
        ]

    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get("status")
        note = (cleaned_data.get("note") or "").strip()
        if status in (
            Appointment.Status.CANCELLED,
            Appointment.Status.REJECTED,
            Appointment.Status.NO_SHOW,
            Appointment.Status.RESCHEDULED,
        ) and not note:
            self.add_error(
                "note",
                "Please state the reason for removing, cancelling, rescheduling, or marking this booking as no-show.",
            )
        cleaned_data["note"] = note
        return cleaned_data


class AppointmentScheduleForm(forms.ModelForm):
    class Meta:
        model = Appointment
        fields = ("appointment_date", "start_time", "assigned_staff")
        widgets = {
            "appointment_date": forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
            "start_time": forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
            "assigned_staff": forms.Select(attrs=FORM_CONTROL),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = (
            User.objects.filter(
                role=User.Role.STAFF,
                is_active=True,
                is_active_staff_member=True,
                staff_profile__is_active=True,
                staff_profile__availability_status=StaffProfile.Availability.AVAILABLE,
            )
            .select_related("staff_profile")
            .prefetch_related("staff_profile__skills")
            .order_by("first_name", "last_name", "email")
        )
        services = []
        if self.instance and self.instance.pk:
            services = [
                row.service
                for row in self.instance.appointment_services.select_related("service").all()
                if row.service
            ]
        if services:
            qualified_ids = [
                u.pk
                for u in qs
                if hasattr(u, "staff_profile")
                and all(u.staff_profile.can_perform(s) for s in services)
            ]
            # Always keep current assigned staff selectable if already set and qualified
            qs = qs.filter(pk__in=qualified_ids)
        self.appointment_services_list = services
        self.fields["assigned_staff"].queryset = qs
        self.fields["assigned_staff"].label_from_instance = (
            lambda u: f"{u.get_full_name()} — {u.staff_profile.specialty}"
            if getattr(u, "staff_profile", None) and u.staff_profile.specialty
            else u.get_full_name() or u.email
        )

    def clean(self):
        cleaned_data = super().clean()
        appointment_date = cleaned_data.get("appointment_date")
        start_time = cleaned_data.get("start_time")
        assigned_staff = cleaned_data.get("assigned_staff")
        if assigned_staff and getattr(self, "appointment_services_list", None):
            profile = getattr(assigned_staff, "staff_profile", None)
            if profile and not all(profile.can_perform(s) for s in self.appointment_services_list):
                self.add_error(
                    "assigned_staff",
                    f"{assigned_staff.get_full_name()} does not specialize in the selected service(s).",
                )
        if appointment_date and start_time:
            value = timezone.make_aware(
                datetime.combine(appointment_date, start_time),
                timezone.get_current_timezone(),
            )
            if value <= timezone.now():
                raise forms.ValidationError("Choose a future appointment time.")
        return cleaned_data


class BookingVerificationForm(forms.Form):
    reference = forms.RegexField(
        BOOKING_REFERENCE_RE,
        max_length=32,
        error_messages={"invalid": "Enter a valid booking reference."},
        widget=forms.TextInput(attrs=FORM_CONTROL),
    )
    code = forms.RegexField(
        r"^\d{8}$",
        error_messages={"invalid": "Enter the eight-digit verification code."},
        widget=forms.TextInput(
            attrs={
                **FORM_CONTROL,
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "maxlength": "8",
            }
        ),
    )


class BookingResendForm(forms.Form):
    reference = forms.RegexField(
        BOOKING_REFERENCE_RE,
        max_length=32,
        error_messages={"invalid": "Enter a valid booking reference."},
        widget=forms.TextInput(attrs=FORM_CONTROL),
    )
    email = forms.EmailField(widget=forms.EmailInput(attrs=FORM_CONTROL))

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class BookingLookupForm(forms.Form):
    reference = forms.RegexField(
        BOOKING_REFERENCE_RE,
        max_length=32,
        error_messages={"invalid": "Enter a valid booking reference."},
        widget=forms.TextInput(attrs=FORM_CONTROL),
    )
    access_token = forms.RegexField(
        BOOKING_ACCESS_TOKEN_RE,
        max_length=64,
        error_messages={"invalid": "Enter a valid booking access token."},
        widget=forms.TextInput(attrs=FORM_CONTROL),
    )


class BookingRescheduleForm(forms.Form):
    scheduled_for = forms.DateTimeField(
        widget=forms.DateTimeInput(
            attrs={**FORM_CONTROL, "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=("%Y-%m-%dT%H:%M",),
    )

    def clean_scheduled_for(self):
        scheduled_for = self.cleaned_data["scheduled_for"]
        if scheduled_for <= timezone.now():
            raise forms.ValidationError("Choose a future appointment time.")
        return scheduled_for


class BookingCancellationForm(forms.Form):
    confirm = forms.BooleanField(label="Cancel this booking")
