from datetime import datetime, timedelta
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from allauth.account.decorators import reauthentication_required

from apps.accounts.authorization import is_owner
from apps.accounts.decorators import (
    authenticated_internal_user_required,
    cashier_or_owner_required,
    owner_required,
)
from apps.accounts.models import User

from .forms import (
    ServiceCategoryForm,
    ServiceFilterForm,
    ServiceForm,
    StaffProfileFilterForm,
    StaffProfileForm,
    StaffScheduleForm,
    StaffTimeBlockForm,
)
from .models import (
    Service,
    ServiceCategory,
    StaffProfile,
    StaffSchedule,
    StaffTimeBlock,
)


@cashier_or_owner_required
@require_http_methods(["GET"])
def index(request):
    owner = is_owner(request.user)
    filter_form = ServiceFilterForm(
        request.GET,
        include_inactive=owner,
    )
    all_categories = ServiceCategory.objects.annotate(
        active_count=Count("services", filter=Q(services__is_active=True)),
        total_count=Count("services"),
    ).order_by("name")

    services = Service.objects.select_related("category").order_by("category__name", "name")
    if not owner:
        services = services.filter(is_active=True)

    is_filtered = False
    if filter_form.is_valid():
        query = filter_form.cleaned_data.get("q")
        category = filter_form.cleaned_data.get("category")
        status = filter_form.cleaned_data.get("status")
        if query:
            is_filtered = True
            services = services.filter(
                Q(name__icontains=query) | Q(description__icontains=query)
            )
        if category:
            is_filtered = True
            services = services.filter(category=category)
        if owner and status == "active":
            is_filtered = True
            services = services.filter(is_active=True)
        elif owner and status == "inactive":
            is_filtered = True
            services = services.filter(is_active=False)

    page_obj = Paginator(services, 12).get_page(request.GET.get("page"))
    all_catalog_services = Service.objects.select_related("category").order_by("category__name", "name")
    if not owner:
        all_catalog_services = all_catalog_services.filter(is_active=True)

    total_services_count = all_catalog_services.count()
    raw_category = request.GET.get("category", "").strip()
    selected_category = ""
    if filter_form.is_valid() and filter_form.cleaned_data.get("category"):
        selected_category = str(filter_form.cleaned_data.get("category").pk)
    elif raw_category:
        selected_category = raw_category

    return render(
        request,
        "services/index.html",
        {
            "filter_form": filter_form,
            "page_obj": page_obj,
            "services": services,
            "all_catalog_services": all_catalog_services,
            "all_categories": all_categories,
            "owner_view": owner,
            "is_filtered": is_filtered,
            "services_count": services.count(),
            "total_services_count": total_services_count,
            "query": request.GET.get("q", "").strip(),
            "selected_category": selected_category,
            "selected_status": request.GET.get("status", "").strip(),
        },
    )


@owner_required
@require_http_methods(["GET"])
def category_list(request):
    query = request.GET.get("q", "")[:100].strip()
    categories = ServiceCategory.objects.annotate(service_count=Count("services")).order_by("name")
    if query:
        categories = categories.filter(name__icontains=query)
    page_obj = Paginator(categories, 15).get_page(request.GET.get("page"))
    return render(
        request,
        "services/category_list.html",
        {"page_obj": page_obj, "query": query},
    )


def _save_model_form(request, form, success_message, redirect_name):
    if form.is_valid():
        form.save()
        messages.success(request, success_message)
        return redirect(redirect_name)
    return None


@owner_required
@require_http_methods(["GET", "POST"])
def category_create(request):
    form = ServiceCategoryForm(
        request.POST if request.method == "POST" else None,
        actor=request.user,
    )
    response = _save_model_form(
        request, form, "Service category created.", "services:category_list"
    )
    if response:
        return response
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "New category", "cancel_url": "services:category_list"},
    )


@owner_required
@require_http_methods(["GET", "POST"])
def category_update(request, pk):
    category = get_object_or_404(ServiceCategory, pk=pk)
    form = ServiceCategoryForm(
        request.POST if request.method == "POST" else None,
        instance=category,
        actor=request.user,
    )
    response = _save_model_form(
        request, form, "Service category updated.", "services:category_list"
    )
    if response:
        return response
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "Edit category", "cancel_url": "services:category_list"},
    )


@owner_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def category_delete(request, pk):
    category = get_object_or_404(ServiceCategory, pk=pk)
    if request.method == "POST":
        try:
            category.delete()
        except ProtectedError:
            messages.error(request, "Move or delete this category's services first.")
        else:
            messages.success(request, "Service category deleted.")
        return redirect("services:category_list")
    return render(
        request,
        "services/confirm_delete.html",
        {
            "object": category,
            "title": "Delete category",
            "cancel_url": "services:category_list",
        },
    )


@owner_required
@require_http_methods(["GET", "POST"])
def service_create(request):
    form = ServiceForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        actor=request.user,
    )
    response = _save_model_form(request, form, "Service created.", "services:index")
    if response:
        return response
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "New service", "cancel_url": "services:index"},
    )


@owner_required
@require_http_methods(["GET", "POST"])
def service_update(request, pk):
    service = get_object_or_404(Service.objects.select_related("category"), pk=pk)
    form = ServiceForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        instance=service,
        actor=request.user,
    )
    response = _save_model_form(request, form, "Service updated.", "services:index")
    if response:
        return response
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "Edit service", "cancel_url": "services:index"},
    )


@owner_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def service_delete(request, pk):
    service = get_object_or_404(Service, pk=pk)
    if request.method == "POST":
        service.delete()
        messages.success(request, "Service deleted.")
        return redirect("services:index")
    return render(
        request,
        "services/confirm_delete.html",
        {"object": service, "title": "Delete service", "cancel_url": "services:index"},
    )


@owner_required
@require_http_methods(["GET"])
def staff_list(request):
    filter_form = StaffProfileFilterForm(request.GET)
    profiles = StaffProfile.objects.select_related("user")
    if filter_form.is_valid():
        query = filter_form.cleaned_data["q"]
        availability = filter_form.cleaned_data["availability"]
        status = filter_form.cleaned_data["status"]
        if query:
            profiles = profiles.filter(
                Q(user__first_name__icontains=query)
                | Q(user__last_name__icontains=query)
                | Q(user__email__icontains=query)
                | Q(specialty__icontains=query)
            )
        specialty = filter_form.cleaned_data.get("specialty")
        if specialty:
            if specialty == "CASHIER":
                profiles = profiles.filter(
                    Q(user__can_use_pos=True) | Q(specialty__icontains="Cashier")
                )
            elif specialty == "WAX":
                profiles = profiles.filter(
                    Q(specialty__icontains="Wax") | Q(specialty__icontains="Threading")
                )
            elif specialty == "LASH":
                profiles = profiles.filter(
                    Q(specialty__icontains="Lash")
                )
            elif specialty == "NAIL ART":
                profiles = profiles.filter(
                    Q(specialty__icontains="Nail Art")
                )
            elif specialty == "FOOTSPA":
                profiles = profiles.filter(
                    Q(specialty__icontains="Foot Spa") | Q(specialty__icontains="Footspa")
                )
            elif specialty == "THERAPIST":
                profiles = profiles.filter(
                    Q(specialty__icontains="Therapist")
                )
            elif specialty == "NAIL TECH":
                profiles = profiles.filter(
                    Q(specialty__icontains="Nail Tech") | Q(specialty__icontains="Nail Technician")
                )
            else:
                profiles = profiles.filter(specialty__icontains=specialty)
        if availability:
            profiles = profiles.filter(availability_status=availability)
        if status == "active":
            profiles = profiles.filter(is_active=True)
        elif status == "inactive":
            profiles = profiles.filter(is_active=False)
    page_obj = Paginator(profiles, 15).get_page(request.GET.get("page"))
    return render(
        request,
        "services/staff_list.html",
        {"filter_form": filter_form, "page_obj": page_obj},
    )


@owner_required
@require_http_methods(["GET", "POST"])
def staff_create(request):
    form = StaffProfileForm(
        request.POST if request.method == "POST" else None,
        actor=request.user,
    )
    response = _save_model_form(
        request, form, "Staff profile created.", "services:staff_list"
    )
    if response:
        return response
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "New staff profile", "cancel_url": "services:staff_list"},
    )


@owner_required
@require_http_methods(["GET", "POST"])
def staff_update(request, pk):
    profile = get_object_or_404(StaffProfile.objects.select_related("user"), pk=pk)
    form = StaffProfileForm(
        request.POST if request.method == "POST" else None,
        instance=profile,
        actor=request.user,
    )
    response = _save_model_form(
        request, form, "Staff profile updated.", "services:staff_list"
    )
    if response:
        return response
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "Edit staff profile", "cancel_url": "services:staff_list"},
    )


@owner_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def staff_delete(request, pk):
    profile = get_object_or_404(StaffProfile.objects.select_related("user"), pk=pk)
    if request.method == "POST":
        profile.delete()
        messages.success(request, "Staff profile deleted. The user account was not deleted.")
        return redirect("services:staff_list")
    return render(
        request,
        "services/confirm_delete.html",
        {
            "object": profile,
            "title": "Delete staff profile",
            "cancel_url": "services:staff_list",
        },
    )


# ---------------------------------------------------------------------------
# Staff Schedules (Working Shifts)
# ---------------------------------------------------------------------------

@authenticated_internal_user_required
@require_http_methods(["GET", "POST"])
def schedule_list(request):
    if request.method == "POST":
        is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("ajax") == "1"
        if not request.user.is_owner:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Only the owner/manager has permission to update staff rosters."}, status=403)
            messages.error(request, "Only the owner/manager has permission to update staff rosters.")
            return redirect("services:schedule_list")
        action = request.POST.get("action")
        if action == "sync_time_blocks":
            from .schedules import sync_staff_time_blocks
            count = sync_staff_time_blocks(weeks=4)
            msg = f"Successfully synchronized {count} calendar time blocks (Lunch & Leave) for the visual planner."
            if is_ajax:
                return JsonResponse({"status": "ok", "message": msg, "count": count})
            messages.success(request, msg)
            return redirect("services:schedule_list")
        elif action == "apply_default_roster":
            from .schedules import apply_staff_weekly_schedules, sync_staff_time_blocks
            apply_staff_weekly_schedules()
            count = sync_staff_time_blocks(weeks=4)
            msg = f"Re-applied standard roster (Mon-Sun, 1 day leave per staff, 12pm-1pm lunch) and synchronized {count} time blocks."
            if is_ajax:
                return JsonResponse({"status": "ok", "message": msg, "count": count})
            messages.success(request, msg)
            return redirect("services:schedule_list")

    all_schedules = list(
        StaffSchedule.objects.select_related("staff", "staff__staff_profile").order_by(
            "staff__first_name", "staff__last_name", "day_of_week"
        )
    )

    staff_list = User.objects.filter(role=User.Role.STAFF, is_active=True).order_by("first_name", "last_name")

    # Group schedules by staff
    from collections import defaultdict
    schedules_by_staff = defaultdict(list)
    for s in all_schedules:
        schedules_by_staff[s.staff_id].append(s)

    staff_roster = []
    specialties_set = set()
    for st in staff_list:
        st_scheds = schedules_by_staff.get(st.id, [])
        st_scheds.sort(key=lambda x: x.day_of_week)
        working_scheds = [s for s in st_scheds if s.is_working]
        off_scheds = [s for s in st_scheds if not s.is_working]

        profile = getattr(st, "staff_profile", None)
        specialty = profile.specialty if profile else ""
        if specialty:
            specialties_set.add(specialty)

        spec_lower = specialty.lower()
        spec_categories = []

        # 1. Classic Nail Care (All staff who know nail tech / manicure / pedicure / nail services)
        is_nail_tech = any(k in spec_lower for k in (
            "nail", "tech", "manicure", "pedicure", "mani", "pedi", "russian",
            "gel", "polish", "extension", "acrylic", "art", "chrome", "cat eye", "softgel"
        )) or (profile and profile.skills.filter(category__name__icontains="classic").exists())

        if is_nail_tech:
            spec_categories.append("classic_care")

        # 2. Nail Art & Extensions
        if any(k in spec_lower for k in ("art", "extension", "acrylic", "chrome", "cat eye", "softgel", "gel", "tech")):
            spec_categories.append("nail_art")

        # 3. Lashes, Brows & Spa
        if any(k in spec_lower for k in ("lash", "brow", "threading", "wax", "spa", "reflex", "massage", "candling", "paraffin")):
            spec_categories.append("spa_lashes")

        if not spec_categories:
            spec_categories.append("classic_care")

        # Primary category label for display
        if "nail_art" in spec_categories:
            spec_category = "nail_art"
            spec_category_label = "Nail Art & Extensions"
        elif "spa_lashes" in spec_categories and not is_nail_tech:
            spec_category = "spa_lashes"
            spec_category_label = "Lashes, Brows & Spa"
        else:
            spec_category = "classic_care"
            spec_category_label = "Classic Nail Care"

        first_w = working_scheds[0] if working_scheds else None
        staff_roster.append({
            "staff": st,
            "specialty": specialty,
            "spec_category": spec_category,
            "spec_categories": spec_categories,
            "spec_categories_str": " ".join(spec_categories),
            "spec_category_label": spec_category_label,
            "schedules": st_scheds,
            "working_count": len(working_scheds),
            "off_count": len(off_scheds),
            "off_days": [s.get_day_of_week_display() for s in off_scheds],
            "off_day_str": ", ".join(s.get_day_of_week_display() for s in off_scheds) if off_scheds else "None",
            "default_hours": f"{first_w.start_time.strftime('%I:%M %p')} - {first_w.end_time.strftime('%I:%M %p')}" if first_w else "Off Duty",
            "default_lunch": f"{first_w.lunch_start.strftime('%I:%M %p')} - {first_w.lunch_end.strftime('%I:%M %p')}" if (first_w and first_w.lunch_start and first_w.lunch_end) else "12:00 PM - 01:00 PM",
        })

    # Filter params
    view_mode = request.GET.get("view")
    if not view_mode:
        if request.GET.get("status"):
            view_mode = "table"
        else:
            view_mode = "roster"

    staff_id = request.GET.get("staff")
    if not staff_id and request.user.is_service_staff:
        staff_id = str(request.user.id)

    spec_filter = request.GET.get("spec", "")
    filtered_roster = staff_roster
    if staff_id and staff_id.isdigit():
        filtered_roster = [r for r in filtered_roster if r["staff"].id == int(staff_id)]
    if spec_filter:
        filtered_roster = [r for r in filtered_roster if spec_filter in r["spec_categories"]]

    category_counts = {
        "all": len(staff_roster),
        "nail_art": sum(1 for r in staff_roster if "nail_art" in r["spec_categories"]),
        "spa_lashes": sum(1 for r in staff_roster if "spa_lashes" in r["spec_categories"]),
        "classic_care": sum(1 for r in staff_roster if "classic_care" in r["spec_categories"]),
    }

    # Search parameter (for query matching & live search initialization)
    search_param = (request.GET.get("q") or request.GET.get("search") or "").strip()

    # Daily duty categorization
    today_code = timezone.localdate().weekday()
    day_param = request.GET.get("day")
    selected_day_code = int(day_param) if (day_param is not None and day_param.isdigit() and 0 <= int(day_param) <= 6) else today_code

    days_summary = []
    for code, label in StaffSchedule.DayOfWeek.choices:
        day_scheds = [s for s in all_schedules if s.day_of_week == code]
        day_working = [s for s in day_scheds if s.is_working]
        day_off = [s for s in day_scheds if not s.is_working]
        days_summary.append({
            "code": code,
            "label": label,
            "working_count": len(day_working),
            "off_count": len(day_off),
            "is_selected": (code == selected_day_code),
            "is_today": (code == today_code),
            "on_duty": day_working,
            "off_duty": day_off,
        })

    selected_day_scheds = [s for s in all_schedules if s.day_of_week == selected_day_code]
    daily_on_duty = [s for s in selected_day_scheds if s.is_working]
    daily_off_duty = [s for s in selected_day_scheds if not s.is_working]
    selected_day_label = dict(StaffSchedule.DayOfWeek.choices).get(selected_day_code, "Monday")

    # Table view query and pagination (kept 100% backward compatible)
    schedules = StaffSchedule.objects.select_related("staff", "staff__staff_profile").order_by(
        "staff__first_name", "staff__last_name", "day_of_week"
    )
    if staff_id and staff_id.isdigit():
        schedules = schedules.filter(staff_id=int(staff_id))
    if day_param is not None and day_param.isdigit() and 0 <= int(day_param) <= 6:
        schedules = schedules.filter(day_of_week=int(day_param))

    status_param = request.GET.get("status")
    if status_param == "working":
        schedules = schedules.filter(is_working=True)
    elif status_param in ("off", "leave"):
        schedules = schedules.filter(is_working=False)

    page_obj = Paginator(schedules, 25).get_page(request.GET.get("page"))

    return render(
        request,
        "services/schedule_list.html",
        {
            "view_mode": view_mode,
            "page_obj": page_obj,
            "all_table_schedules": all_schedules,
            "staff_id": staff_id,
            "staff_list": staff_list,
            "day_param": day_param,
            "status_param": status_param,
            "search_param": search_param,
            "day_choices": StaffSchedule.DayOfWeek.choices,
            "total_staff_count": staff_list.count(),
            "working_count": StaffSchedule.objects.filter(is_working=True).count(),
            "leave_count": StaffSchedule.objects.filter(is_working=False).count(),
            # Categorized view data
            "staff_roster": filtered_roster,
            "all_staff_roster": staff_roster,
            "spec_filter": spec_filter,
            "category_counts": category_counts,
            "days_summary": days_summary,
            "selected_day_code": selected_day_code,
            "selected_day_label": selected_day_label,
            "daily_on_duty": daily_on_duty,
            "daily_off_duty": daily_off_duty,
            "today_code": today_code,
        },
    )


@owner_required
@require_http_methods(["GET", "POST"])
def schedule_create(request):
    form = StaffScheduleForm(
        request.POST if request.method == "POST" else None,
        actor=request.user,
    )
    response = _save_model_form(
        request, form, "Staff shift schedule created.", "services:schedule_list"
    )
    if response:
        from .schedules import sync_staff_time_blocks
        sync_staff_time_blocks(weeks=4)
        return response
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "New staff schedule", "cancel_url": "services:schedule_list"},
    )


@owner_required
@require_http_methods(["GET", "POST"])
def schedule_update(request, pk):
    schedule = get_object_or_404(StaffSchedule.objects.select_related("staff"), pk=pk)
    form = StaffScheduleForm(
        request.POST if request.method == "POST" else None,
        instance=schedule,
        actor=request.user,
    )
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("ajax") == "1"
    if request.method == "POST" and is_ajax:
        if form.is_valid():
            saved_sched = form.save()
            from .schedules import sync_staff_time_blocks
            sync_staff_time_blocks(weeks=4)
            return JsonResponse({
                "status": "ok",
                "message": f"Updated shift schedule for {saved_sched.staff.get_full_name() or saved_sched.staff.email} ({saved_sched.get_day_of_week_display()}).",
                "schedule": {
                    "id": saved_sched.pk,
                    "staff_id": saved_sched.staff_id,
                    "staff_name": saved_sched.staff.get_full_name() or saved_sched.staff.email,
                    "day_of_week": saved_sched.day_of_week,
                    "day_name": saved_sched.get_day_of_week_display(),
                    "day_short": saved_sched.get_day_of_week_display()[:3],
                    "is_working": saved_sched.is_working,
                    "start_time": saved_sched.start_time.strftime("%H:%M") if saved_sched.start_time else "",
                    "end_time": saved_sched.end_time.strftime("%H:%M") if saved_sched.end_time else "",
                    "start_time_fmt": saved_sched.start_time.strftime("%I:%M %p").lstrip("0") if saved_sched.start_time else "",
                    "end_time_fmt": saved_sched.end_time.strftime("%I:%M %p").lstrip("0") if saved_sched.end_time else "",
                    "lunch_start": saved_sched.lunch_start.strftime("%H:%M") if saved_sched.lunch_start else "",
                    "lunch_end": saved_sched.lunch_end.strftime("%H:%M") if saved_sched.lunch_end else "",
                    "lunch_start_fmt": saved_sched.lunch_start.strftime("%I:%M %p").lstrip("0") if saved_sched.lunch_start else "",
                    "lunch_end_fmt": saved_sched.lunch_end.strftime("%I:%M %p").lstrip("0") if saved_sched.lunch_end else "",
                }
            })
        else:
            return JsonResponse({"status": "error", "errors": form.errors.get_json_data()}, status=400)

    if request.method == "GET" and is_ajax:
        return JsonResponse({
            "status": "ok",
            "schedule": {
                "id": schedule.pk,
                "staff_id": schedule.staff_id,
                "staff_name": schedule.staff.get_full_name() or schedule.staff.email,
                "day_of_week": schedule.day_of_week,
                "day_name": schedule.get_day_of_week_display(),
                "is_working": schedule.is_working,
                "start_time": schedule.start_time.strftime("%H:%M") if schedule.start_time else "09:00",
                "end_time": schedule.end_time.strftime("%H:%M") if schedule.end_time else "21:00",
                "lunch_start": schedule.lunch_start.strftime("%H:%M") if schedule.lunch_start else "12:00",
                "lunch_end": schedule.lunch_end.strftime("%H:%M") if schedule.lunch_end else "13:00",
            }
        })

    response = _save_model_form(
        request, form, "Staff shift schedule updated.", "services:schedule_list"
    )
    if response:
        from .schedules import sync_staff_time_blocks
        sync_staff_time_blocks(weeks=4)
        return response
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "Edit staff schedule", "cancel_url": "services:schedule_list"},
    )


@owner_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def schedule_delete(request, pk):
    schedule = get_object_or_404(StaffSchedule.objects.select_related("staff"), pk=pk)
    if request.method == "POST":
        schedule.delete()
        from .schedules import sync_staff_time_blocks
        sync_staff_time_blocks(weeks=4)
        messages.success(request, "Staff schedule deleted.")
        return redirect("services:schedule_list")
    return render(
        request,
        "services/confirm_delete.html",
        {
            "object": schedule,
            "title": "Delete staff schedule",
            "cancel_url": "services:schedule_list",
        },
    )


# ---------------------------------------------------------------------------
# Staff Time Blocks (Visual Weekly Grid & Management)
# ---------------------------------------------------------------------------

ROW_HEIGHT = 56
START_HOUR = 9
END_HOUR = 22


def _get_week_start(ref_date):
    # Sunday as first day of week
    return ref_date - timedelta(days=(ref_date.weekday() + 1) % 7)


def _get_block_color_class(reason):
    r = (reason or "").lower().strip()
    if any(k in r for k in ("lunch", "personal", "admin", "break", "finish", "rest", "errand", "meal", "snack")):
        return "block-color-lime"
    elif any(k in r for k in ("leave", "sick", "doctor", "medical", "off", "vacation", "emergency")):
        return "block-color-rose"
    else:
        return "block-color-blue"


def _layout_day_blocks(day_raw_blocks, grid_start_min, grid_end_min, day_raw_appts=None):
    from collections import defaultdict
    from django.urls import reverse

    prepared = []
    all_day_blocks = []

    # 1. Separate all-day leaves/blocks (e.g. 8+ hours) so they don't slice through the hourly grid
    hourly_raw_blocks = []
    for b in day_raw_blocks:
        s_min = b.start_time.hour * 60 + b.start_time.minute
        e_min = b.end_time.hour * 60 + b.end_time.minute
        duration = e_min - s_min
        reason_lower = (b.reason or "").lower()
        if duration >= 480 and ("leave" in reason_lower or "off" in reason_lower or "vacation" in reason_lower or "sick" in reason_lower):
            color_class = _get_block_color_class(b.reason)
            all_day_blocks.append({
                "is_appointment": False,
                "is_all_day": True,
                "obj": b,
                "id": b.pk,
                "staff_id": b.staff_id,
                "staff_name": b.staff.get_full_name() or b.staff.email,
                "date_iso": b.date.isoformat(),
                "start_time_iso": b.start_time.strftime("%H:%M"),
                "end_time_iso": b.end_time.strftime("%H:%M"),
                "start_time_display": b.start_time.strftime("%I:%M %p").lstrip("0"),
                "end_time_display": b.end_time.strftime("%I:%M %p").lstrip("0"),
                "reason": b.reason or "Leave",
                "color_class": color_class,
            })
        else:
            hourly_raw_blocks.append(b)

    # 2. Consolidate identical simultaneous team lunch breaks to avoid 10 duplicate slivers side-by-side
    lunch_groups = defaultdict(list)
    other_blocks = []
    for b in hourly_raw_blocks:
        if (b.reason or "").strip().lower() == "lunch":
            key = (b.start_time, b.end_time)
            lunch_groups[key].append(b)
        else:
            other_blocks.append(b)

    for (s_time, e_time), b_list in lunch_groups.items():
        if len(b_list) > 1:
            first_b = b_list[0]
            s_min = s_time.hour * 60 + s_time.minute
            e_min = e_time.hour * 60 + e_time.minute
            eff_s = max(grid_start_min, s_min)
            eff_e = min(grid_end_min, e_min)
            if eff_e <= eff_s:
                continue
            top_px = ((eff_s - grid_start_min) / 60) * ROW_HEIGHT
            height_px = max(28, ((eff_e - eff_s) / 60) * ROW_HEIGHT)
            staff_names = [b.staff.get_full_name() or b.staff.email for b in b_list]
            prepared.append({
                "is_appointment": False,
                "is_all_day": False,
                "is_consolidated": True,
                "obj": first_b,
                "id": first_b.pk,
                "block_ids": [b.pk for b in b_list],
                "staff_id": first_b.staff_id,
                "staff_count": len(b_list),
                "staff_name": f"{len(b_list)} Technicians",
                "staff_names_list": ", ".join(staff_names),
                "date_iso": first_b.date.isoformat(),
                "start_time_iso": s_time.strftime("%H:%M"),
                "end_time_iso": e_time.strftime("%H:%M"),
                "start_time_display": s_time.strftime("%I:%M %p").lstrip("0"),
                "end_time_display": e_time.strftime("%I:%M %p").lstrip("0"),
                "reason": f"Team Lunch ({len(b_list)} Techs)",
                "top_px": int(top_px),
                "height_px": int(height_px),
                "color_class": "block-color-lime",
                "eff_s": eff_s,
                "eff_e": eff_e,
            })
        else:
            other_blocks.extend(b_list)

    # 3. Process remaining hourly time blocks
    for b in other_blocks:
        # If this time block was generated by a POS sale that is already rendered as an appointment,
        # skip creating a duplicate side-by-side block.
        if (b.reason or "").startswith("POS #") and day_raw_appts:
            pos_ref = b.reason.split(":")[0].replace("POS #", "").strip()
            already_covered = any(
                pos_ref in (a.notes or "") or (getattr(a, "booking_reference", "") == pos_ref)
                for a in day_raw_appts
                if a.assigned_staff_id == b.staff_id
            )
            if already_covered:
                continue

        s_min = b.start_time.hour * 60 + b.start_time.minute
        e_min = b.end_time.hour * 60 + b.end_time.minute
        eff_s = max(grid_start_min, s_min)
        eff_e = min(grid_end_min, e_min)
        if eff_e <= eff_s:
            continue
        top_px = ((eff_s - grid_start_min) / 60) * ROW_HEIGHT
        height_px = max(24, ((eff_e - eff_s) / 60) * ROW_HEIGHT)
        color_class = _get_block_color_class(b.reason)
        prepared.append({
            "is_appointment": False,
            "is_all_day": False,
            "obj": b,
            "id": b.pk,
            "staff_id": b.staff_id,
            "staff_name": b.staff.get_full_name() or b.staff.email,
            "date_iso": b.date.isoformat(),
            "start_time_iso": b.start_time.strftime("%H:%M"),
            "end_time_iso": b.end_time.strftime("%H:%M"),
            "start_time_display": b.start_time.strftime("%I:%M %p").lstrip("0"),
            "end_time_display": b.end_time.strftime("%I:%M %p").lstrip("0"),
            "reason": b.reason or "Time block",
            "top_px": int(top_px),
            "height_px": int(height_px),
            "color_class": color_class,
            "eff_s": eff_s,
            "eff_e": eff_e,
        })

    # 4. Process customer appointments
    for appt in (day_raw_appts or []):
        s_min = appt.start_time.hour * 60 + appt.start_time.minute
        e_time = appt.end_time or appt.start_time
        e_min = e_time.hour * 60 + e_time.minute
        if e_min <= s_min:
            e_min = s_min + 60
        eff_s = max(grid_start_min, s_min)
        eff_e = min(grid_end_min, e_min)
        if eff_e <= eff_s:
            continue
        top_px = ((eff_s - grid_start_min) / 60) * ROW_HEIGHT
        height_px = max(28, ((eff_e - eff_s) / 60) * ROW_HEIGHT)
        staff_name = ""
        if appt.assigned_staff:
            staff_name = appt.assigned_staff.get_full_name() or appt.assigned_staff.email
        prepared.append({
            "is_appointment": True,
            "is_all_day": False,
            "obj": appt,
            "id": appt.pk,
            "reference": appt.booking_reference,
            "staff_id": appt.assigned_staff_id,
            "staff_name": staff_name,
            "customer_name": appt.customer_name_snapshot,
            "service_name": appt.service_name,
            "status": appt.status,
            "status_display": appt.get_status_display(),
            "url": reverse("bookings:detail", kwargs={"reference": appt.booking_reference}),
            "date_iso": appt.appointment_date.isoformat(),
            "start_time_iso": appt.start_time.strftime("%H:%M"),
            "end_time_iso": e_time.strftime("%H:%M"),
            "start_time_display": appt.start_time.strftime("%I:%M %p").lstrip("0"),
            "end_time_display": e_time.strftime("%I:%M %p").lstrip("0"),
            "reason": f"Appointment: {appt.customer_name_snapshot}",
            "top_px": int(top_px),
            "height_px": int(height_px),
            "color_class": "block-color-appointment",
            "eff_s": eff_s,
            "eff_e": eff_e,
        })

    # 5. Greedy interval column coloring and local concurrency calculation
    if prepared:
        # Sort blocks by start time, and longer duration first
        prepared.sort(key=lambda item: (item["eff_s"], -item["eff_e"]))

        # Group into connected overlapping clusters
        clusters = []
        current_cluster = []
        cluster_end = -1

        for item in prepared:
            if not current_cluster:
                current_cluster.append(item)
                cluster_end = item["eff_e"]
            else:
                if item["eff_s"] < cluster_end:
                    current_cluster.append(item)
                    cluster_end = max(cluster_end, item["eff_e"])
                else:
                    clusters.append(current_cluster)
                    current_cluster = [item]
                    cluster_end = item["eff_e"]
        if current_cluster:
            clusters.append(current_cluster)

        for cluster in clusters:
            columns = []
            for item in cluster:
                placed = False
                for col_idx, col_last_end in enumerate(columns):
                    if item["eff_s"] >= col_last_end:
                        columns[col_idx] = item["eff_e"]
                        item["col_idx"] = col_idx
                        placed = True
                        break
                if not placed:
                    item["col_idx"] = len(columns)
                    columns.append(item["eff_e"])

            # Local interval concurrency: each item scales to actual simultaneous blocks, not peak of whole day
            for item in cluster:
                overlapping = [
                    other for other in cluster
                    if other["eff_s"] < item["eff_e"] and other["eff_e"] > item["eff_s"]
                ]
                max_col = max(o["col_idx"] for o in overlapping) + 1 if overlapping else item["col_idx"] + 1
                num_cols = max(1, max_col)
                item["num_cols"] = num_cols
                item["left_pct"] = round((item["col_idx"] / num_cols) * 100, 2)
                item["width_pct"] = round((100 / num_cols), 2)

    # Append all-day blocks so callers have the complete item list
    prepared.extend(all_day_blocks)
    return prepared


@authenticated_internal_user_required
@require_http_methods(["GET"])
def time_block_list(request):
    today = timezone.localdate()
    date_param = request.GET.get("date") or request.GET.get("week")
    ref_date = today
    if date_param:
        try:
            ref_date = datetime.strptime(date_param.strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            ref_date = today

    week_start = _get_week_start(ref_date)
    week_end = week_start + timedelta(days=6)
    prev_week_date = week_start - timedelta(days=7)
    next_week_date = week_start + timedelta(days=7)
    prev_day_date = ref_date - timedelta(days=1)
    next_day_date = ref_date + timedelta(days=1)

    staff_id = request.GET.get("staff")
    selected_staff = None
    if staff_id and staff_id.isdigit():
        selected_staff = User.objects.filter(role=User.Role.STAFF, pk=int(staff_id)).first()
    elif request.user.is_service_staff:
        selected_staff = request.user
        staff_id = str(request.user.id)

    staff_list = User.objects.filter(role=User.Role.STAFF, is_active=True).order_by("first_name", "last_name")

    # Table view data (preserves pagination & existing table list)
    blocks_qs = StaffTimeBlock.objects.select_related("staff").order_by("-date", "start_time")
    if selected_staff:
        blocks_qs = blocks_qs.filter(staff=selected_staff)
    page_obj = Paginator(blocks_qs, 20).get_page(request.GET.get("page"))

    # Grid view data
    from apps.bookings.models import Appointment

    week_blocks_qs = StaffTimeBlock.objects.filter(
        date__range=(week_start, week_end)
    ).select_related("staff").order_by("date", "start_time")
    if selected_staff:
        week_blocks_qs = week_blocks_qs.filter(staff=selected_staff)

    week_appts_qs = (
        Appointment.objects.filter(
            appointment_date__range=(week_start, week_end)
        )
        .exclude(
            status__in=(
                Appointment.Status.UNVERIFIED,
                Appointment.Status.CANCELLED,
                Appointment.Status.REJECTED,
                Appointment.Status.EXPIRED,
            )
        )
        .select_related("assigned_staff", "customer")
        .prefetch_related("appointment_services")
        .order_by("appointment_date", "start_time")
    )
    if selected_staff:
        week_appts_qs = week_appts_qs.filter(assigned_staff=selected_staff)

    total_week_blocks = week_blocks_qs.count()
    total_week_appointments = week_appts_qs.count()

    grid_start_min = START_HOUR * 60
    grid_end_min = END_HOUR * 60
    total_grid_height = (END_HOUR - START_HOUR) * ROW_HEIGHT

    day_columns = []
    for i in range(7):
        current_date = week_start + timedelta(days=i)
        day_raw_blocks = [b for b in week_blocks_qs if b.date == current_date]
        day_raw_appts = [a for a in week_appts_qs if a.appointment_date == current_date]
        day_blocks = _layout_day_blocks(day_raw_blocks, grid_start_min, grid_end_min, day_raw_appts=day_raw_appts)
        day_columns.append({
            "date": current_date,
            "date_iso": current_date.isoformat(),
            "day_name": current_date.strftime("%A"),
            "day_short": current_date.strftime("%a"),
            "is_today": current_date == today,
            "blocks": [b for b in day_blocks if not b.get("is_all_day")],
            "all_day_blocks": [b for b in day_blocks if b.get("is_all_day")],
        })

    # 1. Day Picker tabs for switching days easily within the week
    day_picker_days = []
    for i in range(7):
        current_date = week_start + timedelta(days=i)
        day_picker_days.append({
            "date": current_date,
            "date_iso": current_date.isoformat(),
            "day_name": current_date.strftime("%A"),
            "day_short": current_date.strftime("%a"),
            "day_number": current_date.strftime("%d"),
            "is_today": current_date == today,
            "is_selected": current_date == ref_date,
            "appts_count": len([a for a in week_appts_qs if a.appointment_date == current_date]),
        })

    # 2. Technician columns for the focused Day View (zero horizontal squishing!)
    day_tech_columns = []
    active_staff_to_show = [selected_staff] if selected_staff else staff_list
    for tech in active_staff_to_show:
        tech_raw_blocks = [b for b in week_blocks_qs if b.date == ref_date and b.staff_id == tech.pk]
        tech_raw_appts = [a for a in week_appts_qs if a.appointment_date == ref_date and a.assigned_staff_id == tech.pk]
        tech_blocks = _layout_day_blocks(tech_raw_blocks, grid_start_min, grid_end_min, day_raw_appts=tech_raw_appts)
        day_tech_columns.append({
            "tech": tech,
            "tech_id": tech.pk,
            "tech_name": tech.get_full_name() or tech.email,
            "blocks": [b for b in tech_blocks if not b.get("is_all_day")],
            "all_day_blocks": [b for b in tech_blocks if b.get("is_all_day")],
        })

    hours_display = [
        {"hour": h, "label": f"{h % 12 or 12} {'am' if h < 12 else 'pm'}"}
        for h in range(START_HOUR, END_HOUR)
    ]

    add_form = StaffTimeBlockForm(
        initial={"staff": selected_staff, "date": today, "start_time": "09:00", "end_time": "10:00"},
        actor=request.user,
    )

    view_mode = request.GET.get("view", "day")

    return render(
        request,
        "services/time_block_list.html",
        {
            "page_obj": page_obj,
            "staff_id": str(selected_staff.pk) if selected_staff else "",
            "selected_staff": selected_staff,
            "staff_list": staff_list,
            "today": today,
            "ref_date": ref_date,
            "ref_date_iso": ref_date.isoformat(),
            "week_start": week_start,
            "week_end": week_end,
            "prev_week_date": prev_week_date.isoformat(),
            "next_week_date": next_week_date.isoformat(),
            "prev_day_date": prev_day_date.isoformat(),
            "next_day_date": next_day_date.isoformat(),
            "current_week_date": today.isoformat(),
            "day_picker_days": day_picker_days,
            "day_tech_columns": day_tech_columns,
            "day_columns": day_columns,
            "hours_display": hours_display,
            "row_height": ROW_HEIGHT,
            "total_grid_height": total_grid_height,
            "total_week_blocks": total_week_blocks,
            "total_week_appointments": total_week_appointments,
            "add_form": add_form,
            "view_mode": view_mode,
        },
    )


@owner_required
@require_http_methods(["GET", "POST"])
def time_block_create(request):
    if request.method == "POST":
        post_data = request.POST

        # 1. Staff selection (support single, multiple, or all active technicians)
        staff_input = post_data.getlist("staff")
        apply_all_staff = post_data.get("all_staff") in ("true", "1", "on") or "all" in staff_input

        active_staff_qs = User.objects.filter(role=User.Role.STAFF, is_active=True)
        if apply_all_staff:
            target_staff_list = list(active_staff_qs)
        else:
            staff_pks = [int(s) for s in staff_input if str(s).isdigit()]
            target_staff_list = list(active_staff_qs.filter(pk__in=staff_pks))

        if not target_staff_list:
            messages.error(request, "Please select at least one active technician.")
            return redirect("services:time_block_list")

        # 2. Date parsing (support primary date + multi-day repeat_dates)
        date_str = post_data.get("date", "").strip()
        try:
            primary_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            messages.error(request, "Invalid date provided.")
            return redirect("services:time_block_list")

        target_dates = [primary_date]
        for extra_date_str in post_data.getlist("repeat_dates"):
            try:
                extra_date = datetime.strptime(extra_date_str.strip(), "%Y-%m-%d").date()
                if extra_date not in target_dates:
                    target_dates.append(extra_date)
            except (ValueError, TypeError):
                pass

        # 3. Time slots (supports multiple rows with minute precision)
        start_times = post_data.getlist("start_time")
        end_times = post_data.getlist("end_time")
        reasons = post_data.getlist("reason")

        slots_to_create = []
        errors = []
        max_rows = max(len(start_times), len(end_times), len(reasons), 1)

        for idx in range(max_rows):
            s_str = start_times[idx].strip() if idx < len(start_times) else ""
            e_str = end_times[idx].strip() if idx < len(end_times) else ""
            r_str = reasons[idx].strip() if idx < len(reasons) else "Work Time"

            if not s_str or not e_str:
                continue

            try:
                s_time = datetime.strptime(s_str[:5], "%H:%M").time()
                e_time = datetime.strptime(e_str[:5], "%H:%M").time()
            except ValueError:
                errors.append(f"Invalid time in slot #{idx + 1}: {s_str} - {e_str}")
                continue

            if e_time <= s_time:
                errors.append(
                    f"Slot #{idx + 1}: End time ({e_time.strftime('%I:%M %p')}) must be after start time ({s_time.strftime('%I:%M %p')})."
                )
                continue

            slots_to_create.append({
                "start_time": s_time,
                "end_time": e_time,
                "reason": r_str[:200] if r_str else "Time block",
            })

        if errors:
            for err in errors:
                messages.error(request, err)
            return redirect("services:time_block_list")

        if not slots_to_create:
            messages.error(request, "Please enter at least one valid time block.")
            return redirect("services:time_block_list")

        # 4. Atomic batch creation
        created_blocks = []
        with transaction.atomic():
            for d in target_dates:
                for staff_obj in target_staff_list:
                    for slot in slots_to_create:
                        created_blocks.append(
                            StaffTimeBlock(
                                staff=staff_obj,
                                date=d,
                                start_time=slot["start_time"],
                                end_time=slot["end_time"],
                                reason=slot["reason"],
                            )
                        )
            StaffTimeBlock.objects.bulk_create(created_blocks)

        created_count = len(created_blocks)
        if created_count == 1:
            messages.success(request, "Staff time block created.")
        else:
            messages.success(
                request,
                f"Successfully created {created_count} time blocks ({len(slots_to_create)} slot(s) × {len(target_staff_list)} technician(s) × {len(target_dates)} day(s)).",
            )

        return redirect("services:time_block_list")

    form = StaffTimeBlockForm(actor=request.user)
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "New time block / break", "cancel_url": "services:time_block_list"},
    )


@owner_required
@require_http_methods(["GET", "POST"])
def time_block_update(request, pk):
    block = get_object_or_404(StaffTimeBlock.objects.select_related("staff"), pk=pk)
    form = StaffTimeBlockForm(
        request.POST if request.method == "POST" else None,
        instance=block,
        actor=request.user,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Staff time block updated.")
        return redirect("services:time_block_list")
    return render(
        request,
        "services/model_form.html",
        {"form": form, "title": "Edit time block / break", "cancel_url": "services:time_block_list"},
    )


@owner_required
@reauthentication_required
@require_http_methods(["GET", "POST"])
def time_block_delete(request, pk):
    block = get_object_or_404(StaffTimeBlock.objects.select_related("staff"), pk=pk)
    if request.method == "POST":
        block.delete()
        messages.success(request, "Staff time block deleted.")
        return redirect("services:time_block_list")
    return render(
        request,
        "services/confirm_delete.html",
        {
            "object": block,
            "title": "Delete staff time block",
            "cancel_url": "services:time_block_list",
        },
    )


@authenticated_internal_user_required
@require_http_methods(["GET"])
def walk_in_technicians(request):
    """
    Dedicated live monitor for reception/cashier to see available technicians
    for walk-in clients in real-time.
    """
    from django.http import JsonResponse
    from .availability import get_walk_in_technicians_status

    data = get_walk_in_technicians_status()
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        # Safe JSON serializable
        json_techs = []
        for t in data["technicians"]:
            json_techs.append({
                "staff_id": t["staff_id"],
                "name": t["name"],
                "specialty": t["specialty"],
                "status": t["status"],
                "status_label": t["status_label"],
                "is_available": t["is_available"],
                "free_until": t["free_until"],
                "details": t["details"],
                "shift_hours": t["shift_hours"],
            })
        return JsonResponse({
            "available_count": data["available_count"],
            "busy_count": data["busy_count"],
            "break_count": data["break_count"],
            "off_duty_count": data["off_duty_count"],
            "technicians": json_techs,
        })

    return render(
        request,
        "services/walk_in_technicians.html",
        data,
    )




