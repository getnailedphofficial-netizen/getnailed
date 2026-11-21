from django import forms
from django.db.models import Q

from apps.accounts.authorization import is_owner
from apps.accounts.models import User

from .models import (
    Service,
    ServiceCategory,
    StaffProfile,
    StaffSchedule,
    StaffTimeBlock,
)


FORM_CONTROL = {"class": "form-control"}


class OwnerManagedFormMixin:
    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        if not self.actor or not is_owner(self.actor):
            raise forms.ValidationError("Only an owner can manage service records.")
        return cleaned_data


class ServiceCategoryForm(OwnerManagedFormMixin, forms.ModelForm):
    class Meta:
        model = ServiceCategory
        fields = ("name", "description")
        widgets = {
            "name": forms.TextInput(attrs=FORM_CONTROL),
            "description": forms.Textarea(attrs={**FORM_CONTROL, "rows": 3}),
        }

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())


class ServiceForm(OwnerManagedFormMixin, forms.ModelForm):
    class Meta:
        model = Service
        fields = (
            "name",
            "category",
            "description",
            "duration_minutes",
            "price",
            "is_active",
            "image",
        )
        widgets = {
            "name": forms.TextInput(attrs=FORM_CONTROL),
            "category": forms.Select(attrs={"class": "form-select"}),
            "description": forms.Textarea(attrs={**FORM_CONTROL, "rows": 5}),
            "duration_minutes": forms.NumberInput(
                attrs={**FORM_CONTROL, "min": "5", "max": "480", "step": "5"}
            ),
            "price": forms.NumberInput(
                attrs={**FORM_CONTROL, "min": "0.01", "step": "0.01"}
            ),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "image": forms.ClearableFileInput(
                attrs={"class": "form-control", "accept": "image/jpeg,image/png,image/webp"}
            ),
        }

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())


class StaffProfileForm(OwnerManagedFormMixin, forms.ModelForm):
    can_use_pos = forms.BooleanField(
        required=False,
        label="Point of Sale / Cashier Access",
        help_text="Allow staff to open cashier shifts and process sales in POS.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = StaffProfile
        fields = ("user", "specialty", "skills", "availability_status", "is_active")
        widgets = {
            "user": forms.Select(attrs={"class": "form-select"}),
            "specialty": forms.TextInput(attrs=FORM_CONTROL),
            "skills": forms.CheckboxSelectMultiple(),
            "availability_status": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        profile_user_id = self.instance.user_id if self.instance and self.instance.pk else None
        self.fields["user"].queryset = User.objects.filter(role=User.Role.STAFF).filter(
            Q(staff_profile__isnull=True) | Q(pk=profile_user_id)
        ).order_by("first_name", "last_name", "email")
        self.fields["skills"].queryset = Service.objects.filter(is_active=True).order_by("category__name", "name")
        self.fields["skills"].required = False
        if self.instance and self.instance.pk and self.instance.user_id:
            self.fields["can_use_pos"].initial = self.instance.user.can_use_pos

    def clean_specialty(self):
        return " ".join(self.cleaned_data["specialty"].split())

    def save(self, commit=True):
        profile = super().save(commit=commit)
        if "can_use_pos" in self.cleaned_data and profile.user_id:
            user = profile.user
            val = self.cleaned_data["can_use_pos"]
            if user.can_use_pos != val:
                user.can_use_pos = val
                if val:
                    user.can_manage_bookings = True
                    user.can_manage_customers = True
                    user.can_assign_services = True
                user.save(update_fields=("can_use_pos", "can_manage_bookings", "can_manage_customers", "can_assign_services"))
        return profile


class StaffScheduleForm(OwnerManagedFormMixin, forms.ModelForm):
    class Meta:
        model = StaffSchedule
        fields = ("staff", "day_of_week", "start_time", "end_time", "lunch_start", "lunch_end", "is_working")
        widgets = {
            "staff": forms.Select(attrs={"class": "form-select"}),
            "day_of_week": forms.Select(attrs={"class": "form-select"}),
            "start_time": forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
            "end_time": forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
            "lunch_start": forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
            "lunch_end": forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
            "is_working": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = User.objects.filter(role=User.Role.STAFF, is_active=True).select_related("staff_profile").order_by("first_name", "last_name")
        self.fields["staff"].label_from_instance = (
            lambda u: f"{u.get_full_name()} — {u.staff_profile.specialty}"
            if getattr(u, "staff_profile", None) and u.staff_profile.specialty
            else u.get_full_name() or u.email
        )


class StaffTimeBlockForm(OwnerManagedFormMixin, forms.ModelForm):
    class Meta:
        model = StaffTimeBlock
        fields = ("staff", "date", "start_time", "end_time", "reason")
        widgets = {
            "staff": forms.Select(attrs={"class": "form-select"}),
            "date": forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
            "start_time": forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
            "end_time": forms.TimeInput(attrs={**FORM_CONTROL, "type": "time"}),
            "reason": forms.TextInput(
                attrs={
                    **FORM_CONTROL,
                    "placeholder": "e.g., Lunch, Work Time, Leave",
                }
            ),
        }
        help_texts = {
            "reason": "Standard categories: Lunch, Work Time, Leave",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = User.objects.filter(role=User.Role.STAFF, is_active=True).select_related("staff_profile").order_by("first_name", "last_name")
        self.fields["staff"].label_from_instance = (
            lambda u: f"{u.get_full_name()} — {u.staff_profile.specialty}"
            if getattr(u, "staff_profile", None) and u.staff_profile.specialty
            else u.get_full_name() or u.email
        )


class ServiceFilterForm(forms.Form):
    q = forms.CharField(max_length=100, required=False)
    category = forms.ModelChoiceField(
        queryset=ServiceCategory.objects.none(),
        required=False,
        empty_label="All categories",
    )
    status = forms.ChoiceField(
        required=False,
        choices=(("", "All statuses"), ("active", "Active"), ("inactive", "Inactive")),
    )

    def __init__(self, *args, include_inactive=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ServiceCategory.objects.order_by("name")
        if not include_inactive:
            self.fields["status"].choices = (("active", "Active services"),)
            self.fields["status"].initial = "active"
        for field in self.fields.values():
            field.widget.attrs.update(
                {"class": "form-select" if isinstance(field.widget, forms.Select) else "form-control"}
            )


class StaffProfileFilterForm(forms.Form):
    SPECIALTY_CHOICES = (
        ("", "All specialties"),
        ("NAIL TECH", "Nail Tech"),
        ("NAIL ART", "Nail Art"),
        ("LASH", "Lash Tech / Lash Lift"),
        ("WAX", "Wax / Threading"),
        ("THERAPIST", "Therapist"),
        ("FOOTSPA", "Foot Spa"),
        ("CASHIER", "Cashier"),
    )
    q = forms.CharField(max_length=100, required=False)
    specialty = forms.ChoiceField(
        required=False,
        choices=SPECIALTY_CHOICES,
    )
    availability = forms.ChoiceField(
        required=False,
        choices=(("", "All availability"), *StaffProfile.Availability.choices),
    )
    status = forms.ChoiceField(
        required=False,
        choices=(("", "All statuses"), ("active", "Active"), ("inactive", "Inactive")),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update(
                {"class": "form-select" if isinstance(field.widget, forms.Select) else "form-control"}
            )
