import secrets
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.core.validators import validate_phone_number
from apps.customers.models import Customer
from apps.services.models import Service


def generate_booking_reference():
    """Generate a randomized, unique number-only booking reference."""
    from django.apps import apps

    AppointmentModel = apps.get_model("bookings", "Appointment", require_ready=False)
    BookingModel = apps.get_model("bookings", "Booking", require_ready=False)

    while True:
        candidate = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
        try:
            if (
                AppointmentModel
                and AppointmentModel.objects.filter(booking_reference=candidate).exists()
            ):
                continue
            if (
                BookingModel
                and BookingModel.objects.filter(reference=candidate).exists()
            ):
                continue
        except Exception:
            pass
        return candidate


class BookingQuerySet(models.QuerySet):
    def visible_to(self, user):
        from apps.accounts.authorization import CAPABILITY_MANAGE_BOOKINGS, has_capability, is_owner

        if is_owner(user) or has_capability(user, CAPABILITY_MANAGE_BOOKINGS):
            return self
        if user.is_authenticated and user.is_service_staff:
            return self.filter(assigned_staff=user)
        return self.none()


class Booking(models.Model):
    class Status(models.TextChoices):
        UNVERIFIED = "UNVERIFIED", "Unverified"
        CONFIRMED = "CONFIRMED", "Confirmed"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"

    reference = models.CharField(
        max_length=32,
        unique=True,
        default=generate_booking_reference,
        editable=False,
    )
    customer_name = models.CharField(max_length=160)
    email = models.EmailField()
    phone_number = models.CharField(
        max_length=30,
        blank=True,
        validators=(validate_phone_number,),
    )
    service_name = models.CharField(max_length=160)
    scheduled_for = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNVERIFIED,
    )
    assigned_staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="assigned_bookings",
    )
    expires_at = models.DateTimeField()
    verification_code_digest = models.CharField(max_length=64, blank=True)
    verification_code_expires_at = models.DateTimeField(blank=True, null=True)
    verification_failed_attempts = models.PositiveSmallIntegerField(default=0)
    verification_sent_at = models.DateTimeField(blank=True, null=True)
    verification_resend_count = models.PositiveSmallIntegerField(default=0)
    public_access_token_digest = models.CharField(max_length=64, blank=True)
    public_access_expires_at = models.DateTimeField(blank=True, null=True)
    verified_at = models.DateTimeField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BookingQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def _digest(self, purpose, value):
        return salted_hmac(
            f"bookings.{purpose}.{self.reference}",
            value,
            algorithm="sha256",
        ).hexdigest()

    def set_verification_code(self, code, expires_at):
        self.verification_code_digest = self._digest("verification", code)
        self.verification_code_expires_at = expires_at
        self.verification_failed_attempts = 0
        self.verification_sent_at = timezone.now()

    def matches_verification_code(self, code):
        return constant_time_compare(
            self.verification_code_digest,
            self._digest("verification", code),
        )

    def issue_public_access_token(self, expires_at):
        token = secrets.token_urlsafe(32)
        self.public_access_token_digest = self._digest("access", token)
        self.public_access_expires_at = expires_at
        return token

    def accepts_public_access_token(self, token):
        return bool(
            token
            and self.public_access_token_digest
            and self.public_access_expires_at
            and self.public_access_expires_at > timezone.now()
            and constant_time_compare(
                self.public_access_token_digest,
                self._digest("access", token),
            )
        )

    def expire_if_needed(self):
        if self.status == self.Status.UNVERIFIED and self.expires_at <= timezone.now():
            self.status = self.Status.EXPIRED
            self.verification_code_digest = ""
            self.save(update_fields=("status", "verification_code_digest", "updated_at"))
            return True
        return False

    def __str__(self):
        return self.reference

    @property
    def is_legacy_booking(self):
        return True


class AppointmentQuerySet(models.QuerySet):
    def visible_to(self, user):
        from apps.accounts.authorization import is_cashier_or_owner

        visible = self.exclude(status__in=("UNVERIFIED", "EXPIRED"))
        if is_cashier_or_owner(user):
            return visible
        if user.is_authenticated and user.is_service_staff:
            return visible.filter(assigned_staff=user)
        return self.none()

    def assigned_to(self, user):
        visible = self.exclude(status__in=("UNVERIFIED", "EXPIRED"))
        if user.is_authenticated and user.is_owner:
            return visible
        if user.is_authenticated and user.is_service_staff:
            return visible.filter(assigned_staff=user)
        return self.none()


class Appointment(models.Model):
    class Branch(models.TextChoices):
        PLARIDEL = "PLARIDEL", "Plaridel Branch (Unit 14 G/F Rocka Commercial Complex, Tabang, Plaridel, Bulacan)"
        RETIRO_QC = "RETIRO_QC", "Retiro QC Branch (Amoranto, Quezon City)"

    class BookingSource(models.TextChoices):
        PUBLIC = "PUBLIC", "Public booking"
        WALK_IN = "WALK_IN", "Walk-in"
        STAFF = "STAFF", "Staff-created"

    class Status(models.TextChoices):
        UNVERIFIED = "UNVERIFIED", "Unverified"
        PENDING = "PENDING", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        RESCHEDULED = "RESCHEDULED", "Rescheduled"
        ONGOING = "ONGOING", "Ongoing"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        REJECTED = "REJECTED", "Rejected"
        NO_SHOW = "NO_SHOW", "No-show"
        EXPIRED = "EXPIRED", "Verification expired"

    booking_reference = models.CharField(
        max_length=32,
        unique=True,
        default=generate_booking_reference,
        editable=False,
    )
    branch = models.CharField(
        max_length=20,
        choices=Branch.choices,
        default=Branch.PLARIDEL,
    )
    promo_code_issued = models.CharField(max_length=50, blank=True)
    day_of_reminder_sent_at = models.DateTimeField(blank=True, null=True)
    four_hour_reminder_sent_at = models.DateTimeField(blank=True, null=True)
    late_alert_sent_at = models.DateTimeField(blank=True, null=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    customer_name_snapshot = models.CharField(max_length=201)
    customer_email_snapshot = models.EmailField(blank=True)
    customer_phone_snapshot = models.CharField(max_length=30, blank=True)
    selected_services = models.ManyToManyField(
        Service,
        through="AppointmentService",
        related_name="appointments",
    )
    appointment_date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField(blank=True, null=True)
    assigned_staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="assigned_appointments",
        limit_choices_to={"role": "STAFF"},
    )
    booking_source = models.CharField(
        max_length=20,
        choices=BookingSource.choices,
        default=BookingSource.PUBLIC,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNVERIFIED,
    )
    notes = models.TextField(blank=True)
    cancellation_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_appointments",
    )
    expires_at = models.DateTimeField()
    verification_code_digest = models.CharField(max_length=64, blank=True)
    verification_code_expires_at = models.DateTimeField(blank=True, null=True)
    verification_failed_attempts = models.PositiveSmallIntegerField(default=0)
    verification_sent_at = models.DateTimeField(blank=True, null=True)
    verification_resend_count = models.PositiveSmallIntegerField(default=0)
    public_access_token_digest = models.CharField(max_length=64, blank=True)
    public_access_expires_at = models.DateTimeField(blank=True, null=True)
    verified_at = models.DateTimeField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)
    requires_schedule_review = models.BooleanField(default=False)
    legacy_booking_id = models.PositiveBigIntegerField(blank=True, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AppointmentQuerySet.as_manager()

    class Meta:
        ordering = ("appointment_date", "start_time", "booking_reference")
        indexes = [
            models.Index(fields=("appointment_date", "status"), name="appointment_date_status_idx"),
            models.Index(
                fields=("assigned_staff", "appointment_date", "status"),
                name="appointment_staff_date_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__isnull=True) | models.Q(end_time__gt=models.F("start_time")),
                name="appointment_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=(
                        "UNVERIFIED",
                        "PENDING",
                        "APPROVED",
                        "RESCHEDULED",
                        "ONGOING",
                        "COMPLETED",
                        "CANCELLED",
                        "REJECTED",
                        "NO_SHOW",
                        "EXPIRED",
                    )
                ),
                name="appointment_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(booking_source__in=("PUBLIC", "WALK_IN", "STAFF")),
                name="appointment_source_valid",
            ),
        ]

    def clean(self):
        super().clean()
        if self.customer_id and not self.customer_name_snapshot:
            self.customer_name_snapshot = self.customer.full_name
        if self.customer_id and not self.customer_email_snapshot and getattr(self.customer, "email", ""):
            self.customer_email_snapshot = self.customer.email
        if self.customer_id and not self.customer_phone_snapshot and getattr(self.customer, "phone", ""):
            self.customer_phone_snapshot = self.customer.phone
        if self.end_time and self.end_time <= self.start_time:
            raise ValidationError({"end_time": "End time must be later than start time."})
        if self.assigned_staff_id and not self.assigned_staff.is_service_staff:
            raise ValidationError({"assigned_staff": "Appointments can only be assigned to STAFF."})

    def save(self, *args, **kwargs):
        if self.customer_id and not self.customer_name_snapshot:
            self.customer_name_snapshot = self.customer.full_name
        if self.customer_id and not self.customer_email_snapshot and getattr(self.customer, "email", ""):
            self.customer_email_snapshot = self.customer.email
        if self.customer_id and not self.customer_phone_snapshot and getattr(self.customer, "phone", ""):
            self.customer_phone_snapshot = self.customer.phone
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=365)
        super().save(*args, **kwargs)

    @property
    def reference(self):
        return self.booking_reference

    @property
    def customer_name(self):
        return self.customer_name_snapshot

    @property
    def email(self):
        return self.customer_email_snapshot

    @property
    def phone_number(self):
        return self.customer_phone_snapshot

    @property
    def service_name(self):
        return ", ".join(self.appointment_services.values_list("service_name", flat=True))

    @property
    def scheduled_for(self):
        combined = datetime.combine(self.appointment_date, self.start_time)
        return timezone.make_aware(combined, timezone.get_current_timezone())

    @property
    def total_duration_minutes(self):
        return sum(self.appointment_services.values_list("duration_minutes", flat=True))

    @property
    def total_price(self):
        return sum(
            self.appointment_services.values_list("price", flat=True),
            Decimal("0.00"),
        )

    def _digest(self, purpose, value):
        return salted_hmac(
            f"bookings.{purpose}.{self.booking_reference}",
            value,
            algorithm="sha256",
        ).hexdigest()

    def set_verification_code(self, code, expires_at):
        self.verification_code_digest = self._digest("verification", code)
        self.verification_code_expires_at = expires_at
        self.verification_failed_attempts = 0
        self.verification_sent_at = timezone.now()

    def matches_verification_code(self, code):
        return constant_time_compare(
            self.verification_code_digest,
            self._digest("verification", code),
        )

    def issue_public_access_token(self, expires_at):
        token = secrets.token_urlsafe(32)
        self.public_access_token_digest = self._digest("access", token)
        self.public_access_expires_at = expires_at
        return token

    def accepts_public_access_token(self, token):
        return bool(
            token
            and self.public_access_token_digest
            and self.public_access_expires_at
            and self.public_access_expires_at > timezone.now()
            and constant_time_compare(
                self.public_access_token_digest,
                self._digest("access", token),
            )
        )

    def expire_if_needed(self):
        if self.status == self.Status.UNVERIFIED and self.expires_at <= timezone.now():
            self.status = self.Status.EXPIRED
            self.verification_code_digest = ""
            self.save(update_fields=("status", "verification_code_digest", "updated_at"))
            return True
        return False

    def __str__(self):
        return self.booking_reference

    @property
    def is_legacy_booking(self):
        return False


class AppointmentService(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        ONGOING = "ONGOING", "Ongoing"
        COMPLETED = "COMPLETED", "Completed"

    appointment = models.ForeignKey(
        Appointment,
        on_delete=models.CASCADE,
        related_name="appointment_services",
    )
    service = models.ForeignKey(
        Service,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="appointment_service_rows",
    )
    service_name = models.CharField(max_length=160)
    duration_minutes = models.PositiveSmallIntegerField(
        validators=(MinValueValidator(5), MaxValueValidator(480)),
    )
    price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=(MinValueValidator(Decimal("0.00")),),
    )
    assigned_staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="assigned_service_work",
        limit_choices_to={"role": "STAFF"},
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.PENDING,
    )
    approved_at = models.DateTimeField(blank=True, null=True)
    started_at = models.DateTimeField(blank=True, null=True)
    expected_finish_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("position", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("appointment", "position"),
                name="appointment_service_position_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(duration_minutes__gte=5, duration_minutes__lte=480),
                name="appointment_service_duration_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(price__gte=Decimal("0.00")),
                name="appointment_service_price_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=("PENDING", "APPROVED", "ONGOING", "COMPLETED")
                ),
                name="appointment_service_status_valid",
            ),
        ]

    def clean(self):
        super().clean()
        if self.service:
            if not self.service_name:
                self.service_name = self.service.name
            if self.duration_minutes is None:
                self.duration_minutes = self.service.duration_minutes
            if self.price is None:
                self.price = self.service.price
        if not self.assigned_staff_id and self.appointment_id and getattr(self.appointment, "assigned_staff_id", None):
            self.assigned_staff = self.appointment.assigned_staff

    def save(self, *args, **kwargs):
        if self.service:
            if not self.service_name:
                self.service_name = self.service.name
            if self.duration_minutes is None:
                self.duration_minutes = self.service.duration_minutes
            if self.price is None:
                self.price = self.service.price
        if not self.assigned_staff_id and self.appointment_id and getattr(self.appointment, "assigned_staff_id", None):
            self.assigned_staff = self.appointment.assigned_staff
        if self.position == 0 and self.appointment_id:
            max_pos = AppointmentService.objects.filter(appointment_id=self.appointment_id).exclude(pk=self.pk).aggregate(models.Max("position"))["position__max"]
            if max_pos is not None:
                self.position = max_pos + 1
        super().save(*args, **kwargs)

    @property
    def expected_finish(self):
        if self.expected_finish_at:
            return self.expected_finish_at
        rows = list(self.appointment.appointment_services.all())
        minutes = sum(row.duration_minutes for row in rows if row.position <= self.position)
        return self.appointment.scheduled_for + timedelta(minutes=minutes)

    @property
    def is_late(self):
        if self.status == self.Status.COMPLETED:
            return bool(self.completed_at and self.completed_at > self.expected_finish)
        return self.expected_finish < timezone.now()

    def __str__(self):
        return self.service_name


class AppointmentStatusHistory(models.Model):
    appointment = models.ForeignKey(
        Appointment,
        on_delete=models.CASCADE,
        related_name="status_history",
    )
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, choices=Appointment.Status.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="appointment_status_changes",
    )
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "pk")

    def __str__(self):
        return f"{self.appointment} -> {self.to_status}"
