from django import forms
from django.forms import BaseFormSet, formset_factory
from django.db.models import Q
from django.utils import timezone
from itertools import groupby

from apps.accounts.models import User
from apps.bookings.models import Appointment
from apps.customers.models import Customer
from apps.services.models import Service

from .models import CashierShift, Payment, PromoVoucher, Sale


FORM_CONTROL = {"class": "form-control"}


class GroupedModelChoiceIterator(forms.models.ModelChoiceIterator):
    def __iter__(self):
        if self.field.empty_label is not None:
            yield ("", self.field.empty_label)
        queryset = self.queryset.select_related("category").order_by("category__name", "name")
        for category, services in groupby(queryset, key=lambda x: x.category.name if x.category else "Other Treatments"):
            yield (
                category,
                [self.choice(service) for service in services]
            )


class GroupedModelChoiceField(forms.ModelChoiceField):
    iterator = GroupedModelChoiceIterator


class CheckoutForm(forms.Form):
    customer = forms.ModelChoiceField(
        queryset=Customer.objects.none(),
        required=False,
        empty_label="Walk-in customer",
        widget=forms.Select(attrs={**FORM_CONTROL, "id": "id_customer"}),
    )
    walk_in_referral_number = forms.CharField(
        required=False,
        max_length=40,
        label="Walk-in Referral Number",
        widget=forms.TextInput(
            attrs={
                **FORM_CONTROL,
                "placeholder": "e.g., WLK-20261028-1042 (Required for Walk-in)",
                "id": "id_walk_in_referral_number",
            }
        ),
        help_text="Required referral / tracking code for walk-in customers.",
    )
    appointment = forms.ModelChoiceField(
        queryset=Appointment.objects.none(),
        required=False,
        empty_label="No linked appointment",
        widget=forms.Select(attrs=FORM_CONTROL),
    )
    discount_type = forms.ChoiceField(
        choices=Sale.DiscountType.choices,
        initial=Sale.DiscountType.NONE,
        widget=forms.Select(attrs={**FORM_CONTROL, "id": "id_discount_type"}),
    )
    discount_value = forms.DecimalField(
        min_value=0,
        max_digits=12,
        decimal_places=2,
        initial=0,
        required=False,
        widget=forms.NumberInput(attrs={**FORM_CONTROL, "step": "0.01", "id": "id_discount_value"}),
    )
    discount_id_number = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={**FORM_CONTROL, "placeholder": "Senior / PWD ID Number", "id": "id_discount_id_number"}),
    )
    discount_id_name = forms.CharField(
        required=False,
        max_length=160,
        widget=forms.TextInput(attrs={**FORM_CONTROL, "placeholder": "Cardholder full name", "id": "id_discount_id_name"}),
    )
    voucher_code = forms.CharField(
        required=False,
        max_length=50,
        widget=forms.TextInput(attrs={**FORM_CONTROL, "placeholder": "Voucher Code", "id": "id_voucher_code"}),
    )
    max_discount_cap = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={**FORM_CONTROL, "step": "0.01", "placeholder": "Discount cap in ₱ (optional)", "id": "id_max_discount_cap"}),
    )
    payment_method = forms.ChoiceField(
        choices=(
            (Payment.Method.CASH, "Cash"),
            (Payment.Method.GCASH, "GCash"),
            (Payment.Method.MAYA, "Maya"),
        ),
        initial=Payment.Method.CASH,
        widget=forms.Select(attrs={**FORM_CONTROL, "id": "id_payment_method"}),
    )
    amount_tendered = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={**FORM_CONTROL, "step": "0.01", "inputmode": "decimal", "id": "id_amount_tendered"}
        ),
    )
    payment_reference = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={**FORM_CONTROL, "placeholder": "Reference number (for GCash / Maya)", "id": "id_payment_reference"}),
    )
    mark_appointment_completed = forms.BooleanField(
        required=False,
        initial=True,
        label="Mark linked appointment completed",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        visible_statuses = tuple(
            value
            for value, _ in Appointment.Status.choices
            if value not in (Appointment.Status.UNVERIFIED, Appointment.Status.EXPIRED)
        )
        self.fields["customer"].queryset = (
            Customer.objects.filter(
                Q(appointments__isnull=True)
                | Q(appointments__status__in=visible_statuses)
            )
            .distinct()
            .order_by("last_name", "first_name")
        )
        today_date = timezone.localdate()
        self.fields["appointment"].queryset = (
            Appointment.objects.exclude(
                status__in=(
                    Appointment.Status.UNVERIFIED,
                    Appointment.Status.REJECTED,
                    Appointment.Status.CANCELLED,
                    Appointment.Status.EXPIRED,
                )
            )
            .filter(
                Q(sale__isnull=True)
                | Q(status=Appointment.Status.ONGOING, appointment_date=today_date)
            )
            .select_related("customer")
            .order_by("appointment_date", "start_time")
        )
        self.fields["appointment"].label_from_instance = lambda obj: (
            f"[{obj.start_time.strftime('%I:%M %p')}] Ref #{obj.booking_reference} - {obj.customer_name_snapshot} ({obj.appointment_date})"
        )
        if not self.is_bound and not self.initial.get("walk_in_referral_number"):
            import secrets
            self.fields["walk_in_referral_number"].initial = (
                f"WLK-{timezone.localdate():%Y%m%d}-{secrets.randbelow(9000) + 1000}"
            )

    def clean(self):
        cleaned_data = super().clean()
        mode = self.data.get("pos_customer_mode", "")
        if mode == "walkin" and not self.data.get("appointment"):
            cleaned_data["appointment"] = None
        elif (mode == "booked" or self.data.get("appointment")) and cleaned_data.get("appointment"):
            cleaned_data["walk_in_referral_number"] = ""

        dtype = cleaned_data.get("discount_type")
        id_num = cleaned_data.get("discount_id_number", "").strip()
        vcode = cleaned_data.get("voucher_code", "").strip()
        customer = cleaned_data.get("customer")
        appointment = cleaned_data.get("appointment")
        walk_in_ref = (cleaned_data.get("walk_in_referral_number") or "").strip()

        if not customer and not appointment and not walk_in_ref:
            import secrets
            from django.utils import timezone
            walk_in_ref = f"WLK-{timezone.localdate():%Y%m%d}-{secrets.randbelow(9000) + 1000}"
        cleaned_data["walk_in_referral_number"] = walk_in_ref

        if dtype in (Sale.DiscountType.SENIOR_CITIZEN, Sale.DiscountType.PWD):
            if not id_num:
                self.add_error("discount_id_number", "ID number is mandatory for Senior Citizen and PWD discounts.")
        elif dtype == Sale.DiscountType.PROMO_VOUCHER:
            if not vcode:
                self.add_error("voucher_code", "Please enter a promo voucher code.")
            else:
                voucher = PromoVoucher.objects.filter(code=vcode.upper(), is_active=True).first()
                if not voucher:
                    self.add_error("voucher_code", "Voucher code not found or inactive.")
                else:
                    cleaned_data["voucher_obj"] = voucher
        return cleaned_data


class CheckoutItemForm(forms.Form):
    service = GroupedModelChoiceField(
        queryset=Service.objects.none(),
        required=False,
        empty_label="-- Select Service --",
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select pos-service"}),
    )
    assigned_staff = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="Unassigned",
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select pos-staff"}),
    )
    service_time = forms.TimeField(
        required=False,
        input_formats=["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"],
        widget=forms.TimeInput(
            attrs={**FORM_CONTROL, "type": "time", "class": "form-control pos-time"}
        ),
    )
    quantity = forms.IntegerField(
        required=False,
        initial=1,
        widget=forms.HiddenInput(attrs={"value": "1"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = Service.objects.filter(
            is_active=True
        ).select_related("category").order_by("category__name", "name")
        self.fields["service"].label_from_instance = (
            lambda s: f"{s.name} — ₱{s.price:0.2f} ({s.duration_minutes}m)"
        )
        self.fields["assigned_staff"].queryset = User.objects.filter(
            role=User.Role.STAFF,
            is_active=True,
            is_active_staff_member=True,
            staff_profile__is_active=True,
        ).select_related("staff_profile").order_by("first_name", "last_name", "email")
        self.fields["assigned_staff"].label_from_instance = (
            lambda u: f"{u.get_full_name()} — {u.staff_profile.specialty}"
            if getattr(u, "staff_profile", None) and u.staff_profile.specialty
            else u.get_full_name() or u.email
        )

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("quantity"):
            cleaned_data["quantity"] = 1
        return cleaned_data


class BaseCheckoutItemFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not any(
            form.cleaned_data and form.cleaned_data.get("service")
            for form in self.forms
        ):
            raise forms.ValidationError("Add at least one service to the sale.")


CheckoutItemFormSet = formset_factory(
    CheckoutItemForm,
    formset=BaseCheckoutItemFormSet,
    extra=6,
    max_num=12,
    validate_max=True,
)


class OpenShiftForm(forms.Form):
    opening_cash = forms.DecimalField(
        label="Opening Cash Float (₱)",
        min_value=0,
        max_digits=12,
        decimal_places=2,
        initial=0,
        widget=forms.NumberInput(attrs={**FORM_CONTROL, "step": "0.01", "placeholder": "e.g., 2000.00"}),
        help_text="Starting cash amount in the cash drawer.",
    )
    notes = forms.CharField(
        label="Shift Notes",
        required=False,
        widget=forms.Textarea(attrs={**FORM_CONTROL, "rows": 2, "placeholder": "Optional opening notes"}),
    )


class CloseShiftForm(forms.Form):
    closing_cash = forms.DecimalField(
        label="Actual Cash in Drawer (₱)",
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={**FORM_CONTROL, "step": "0.01", "placeholder": "Count physical cash and enter total"}),
        help_text="Physically counted cash in the till drawer at shift close.",
    )
    notes = forms.CharField(
        label="Closing Notes / Reconciliation Remarks",
        required=False,
        widget=forms.Textarea(attrs={**FORM_CONTROL, "rows": 3, "placeholder": "Explain any cash overage, shortage, or till notes"}),
    )


class PromoVoucherForm(forms.ModelForm):
    class Meta:
        model = PromoVoucher
        fields = (
            "code",
            "description",
            "discount_type",
            "discount_value",
            "max_discount_cap",
            "min_spend",
            "valid_from",
            "valid_until",
            "usage_limit",
            "is_active",
        )
        widgets = {
            "code": forms.TextInput(attrs=FORM_CONTROL),
            "description": forms.TextInput(attrs=FORM_CONTROL),
            "discount_type": forms.Select(attrs={"class": "form-select"}),
            "discount_value": forms.NumberInput(attrs={**FORM_CONTROL, "step": "0.01"}),
            "max_discount_cap": forms.NumberInput(attrs={**FORM_CONTROL, "step": "0.01"}),
            "min_spend": forms.NumberInput(attrs={**FORM_CONTROL, "step": "0.01"}),
            "valid_from": forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
            "valid_until": forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
            "usage_limit": forms.NumberInput(attrs=FORM_CONTROL),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class SaleHistoryFilterForm(forms.Form):
    PERIOD_CHOICES = (
        ("", "All / Custom"),
        ("today", "Today"),
        ("yesterday", "Yesterday"),
        ("this_week", "This Week"),
        ("this_month", "This Month"),
        ("last_month", "Last Month"),
    )

    q = forms.CharField(
        label="Search",
        required=False,
        widget=forms.TextInput(attrs={**FORM_CONTROL, "placeholder": "Receipt or customer"}),
    )
    period = forms.ChoiceField(
        label="Time Period",
        required=False,
        choices=PERIOD_CHOICES,
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )
    start_date = forms.DateField(
        label="From Date",
        required=False,
        widget=forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
    )
    end_date = forms.DateField(
        label="To Date",
        required=False,
        widget=forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
    )
    date = forms.DateField(
        label="Specific Date",
        required=False,
        widget=forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
    )
    start_time = forms.TimeField(
        label="From Time",
        required=False,
        widget=forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
    )
    end_time = forms.TimeField(
        label="To Time",
        required=False,
        widget=forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
    )
    status = forms.ChoiceField(
        label="Status",
        required=False,
        choices=(("", "All statuses"), *Sale.Status.choices),
        widget=forms.Select(attrs={**FORM_CONTROL, "class": "form-select"}),
    )


class VoidSaleForm(forms.Form):
    reason = forms.CharField(
        min_length=3,
        max_length=500,
        widget=forms.Textarea(attrs={**FORM_CONTROL, "rows": 4}),
    )

