from django import forms
from django.utils import timezone

from apps.accounts.models import User
from apps.services.models import StaffProfile


FORM_CONTROL = {"class": "form-control"}


class MonitoringFilterForm(forms.Form):
    date = forms.DateField(
        required=False,
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={**FORM_CONTROL, "type": "date"}),
    )
    staff = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="All staff",
        widget=forms.Select(attrs=FORM_CONTROL),
    )
    q = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={**FORM_CONTROL, "placeholder": "Service, customer, or reference"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = (
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
        self.fields["staff"].label_from_instance = (
            lambda u: f"{u.get_full_name()} — {u.staff_profile.specialty}"
            if getattr(u, "staff_profile", None) and u.staff_profile.specialty
            else (u.get_full_name() or u.email)
        )


class ServiceStatusForm(forms.Form):
    note = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={**FORM_CONTROL, "placeholder": "Optional note"}),
    )


class ServiceAssignmentForm(forms.Form):
    assigned_staff = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="Unassigned",
        widget=forms.Select(attrs=FORM_CONTROL),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_staff"].queryset = User.objects.filter(
            role=User.Role.STAFF,
            is_active=True,
            is_active_staff_member=True,
            staff_profile__is_active=True,
        ).order_by("first_name", "last_name", "email")
