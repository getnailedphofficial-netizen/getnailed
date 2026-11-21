import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import User
from apps.customers.models import Customer
from apps.services.models import Service

from .models import Notification, SalonMessage


@login_required
def index(request):
    notifications = Notification.objects.filter(recipient=request.user).select_related(
        "appointment"
    )
    return render(request, "notifications/index.html", {"notifications": notifications})


@login_required
@require_POST
def mark_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=("read_at",))
    if notification.appointment_id:
        if request.user.role == User.Role.CUSTOMER:
            return redirect("core:customer_appointments")
        return redirect("bookings:detail", reference=notification.appointment.reference)
    if notification.event_type == Notification.EventType.MESSAGE:
        return redirect("notifications:messages")
    return redirect("notifications:index")


def _get_or_create_customer_for_user(user):
    customer = getattr(user, "customer_profile", None)
    if customer is None:
        customer = Customer.objects.filter(email__iexact=user.email).first()
        if customer and customer.user_id is None:
            customer.user = user
            customer.save(update_fields=("user", "updated_at"))
    if customer is None:
        customer = Customer.objects.create(
            user=user,
            first_name=user.first_name or "Valued",
            last_name=user.last_name or "Client",
            email=user.email,
            phone=getattr(user, "phone_number", "") or "",
        )
    return customer


@login_required
@require_http_methods(["GET", "POST"])
def messages_inbox(request):
    if request.user.role == User.Role.CUSTOMER:
        customer = _get_or_create_customer_for_user(request.user)
        SalonMessage.objects.filter(
            customer=customer, is_from_customer=False, is_read=False
        ).update(is_read=True)

        if request.method == "POST":
            body = (request.POST.get("body") or "").strip()
            if body:
                SalonMessage.objects.create(
                    customer=customer,
                    sender=request.user,
                    sender_name=customer.full_name,
                    body=body,
                    is_from_customer=True,
                )
                staff_recipients = User.objects.filter(
                    role__in=(User.Role.OWNER, User.Role.CASHIER, User.Role.STAFF),
                    is_active=True,
                )
                Notification.objects.bulk_create(
                    [
                        Notification(
                            recipient=recipient,
                            event_type=Notification.EventType.MESSAGE,
                            title=f"New Message from {customer.full_name}",
                            message=body[:480],
                        )
                        for recipient in staff_recipients
                    ]
                )
                messages.success(request, "Your message has been sent to the salon team.")
                return redirect("notifications:messages")

        thread = SalonMessage.objects.filter(customer=customer).select_related("sender")
        return render(
            request,
            "notifications/messages.html",
            {
                "is_customer_view": True,
                "customer": customer,
                "thread": thread,
            },
        )

    # Owner / Cashier / Staff view
    # Store complete chat history visible to owner & authorized staff like Messenger
    all_customers = Customer.objects.annotate(
        last_msg_at=Max("salon_messages__created_at")
    ).order_by("-last_msg_at", "first_name", "last_name")

    # Search filter across customer conversations
    q = (request.GET.get("q") or "").strip()
    if q:
        all_customers = all_customers.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(phone__icontains=q)
            | Q(email__icontains=q)
        )

    selected_id = request.GET.get("customer") or request.POST.get("customer_id")
    selected_customer = None
    if selected_id and str(selected_id).isdigit():
        selected_customer = Customer.objects.filter(pk=int(selected_id)).first()
    if selected_customer is None:
        selected_customer = (
            all_customers.filter(last_msg_at__isnull=False).first()
            or all_customers.first()
        )

    if selected_customer:
        SalonMessage.objects.filter(
            customer=selected_customer, is_from_customer=True, is_read=False
        ).update(is_read=True)

    if request.method == "POST" and selected_customer:
        body = (request.POST.get("body") or "").strip()
        if body:
            sender_label = (
                f"{request.user.get_full_name() or request.user.email} "
                f"({request.user.get_role_display()})"
            )
            SalonMessage.objects.create(
                customer=selected_customer,
                sender=request.user,
                sender_name=sender_label,
                body=body,
                is_from_customer=False,
            )
            if selected_customer.user_id and selected_customer.user.is_active:
                Notification.objects.create(
                    recipient=selected_customer.user,
                    event_type=Notification.EventType.MESSAGE,
                    title="New Reply from Get Nailed Salon",
                    message=f"{sender_label}: {body}"[:480],
                )
            messages.success(
                request, f"Reply sent to {selected_customer.full_name}."
            )
            return redirect(f"{request.path}?customer={selected_customer.pk}")

    thread = (
        SalonMessage.objects.filter(customer=selected_customer).select_related("sender")
        if selected_customer
        else SalonMessage.objects.none()
    )
    return render(
        request,
        "notifications/messages.html",
        {
            "is_customer_view": False,
            "customers": all_customers,
            "selected_customer": selected_customer,
            "thread": thread,
            "search_query": q,
        },
    )


@require_POST
def chatbot_api(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        payload = {}
    prompt = (payload.get("message") or request.POST.get("message") or "").strip()
    if not prompt:
        return JsonResponse(
            {"reply": "Hi! How can I help you with your nail appointment today?"}
        )

    lower = prompt.lower()
    reply_parts = []

    if any(k in lower for k in ("branch", "location", "where", "address", "plaridel", "bulacan")):
        reply_parts.append(
            "📍 **Our Location:**\n"
            "**Plaridel Branch:** Unit 14 G/F Rocka Commercial Complex, Tabang, Plaridel, Bulacan.\n"
            "Open Daily: 9:00 AM – 8:00 PM • Contact: 0905 084 3892 / (044) 794-7692."
        )

    if any(k in lower for k in ("friday", "saturday", "sunday", "weekend", "300", "package", "walk-in", "walk in")):
        reply_parts.append(
            "✨ **Friday–Sunday Peak Booking Policy:**\n"
            "On Fridays, Saturdays, and Sundays, online bookings prioritize package and major services above ₱300. "
            "Services ₱300 and below are accepted online on Fri–Sun only if there are no other bookings at that time slot, "
            "or you may visit us as a walk-in when technicians are free!"
        )

    if any(k in lower for k in ("late", "grace", "10", "15", "advance", "payment", "deposit", "reminder", "cancel")):
        reply_parts.append(
            "⏰ **No Advance Payment & 10–15 Min Grace Period:**\n"
            "We do not require advance payment! We send email & SMS reminders on the day of your booking and 4–5 hours before. "
            "Our owner/staff will also call or text to confirm. If a client does not arrive or respond within **10–15 minutes** of their start time, "
            "the slot and technician are automatically released for waiting clients."
        )

    if any(k in lower for k in ("promo", "voucher", "discount", "code", "approved")):
        reply_parts.append(
            "🎁 **Promo Code on Approval:**\n"
            "Once your appointment is approved by our salon team, you will automatically receive a notification and email with an exclusive promo code (such as `GETNAILED10`) that you can use at checkout!"
        )

    if any(k in lower for k in ("price", "service", "menu", "how much", "gel", "manicure", "pedicure", "extension", "spabooth")):
        top_services = list(
            Service.objects.filter(is_active=True)
            .select_related("category")
            .order_by("category__name", "price")[:8]
        )
        if top_services:
            svc_lines = [
                f"• {s.name} ({s.category.name}) — ₱{s.price} ({s.duration_minutes} mins)"
                for s in top_services
            ]
            reply_parts.append(
                "💅 **Popular Salon Services:**\n"
                + "\n".join(svc_lines)
                + "\nBrowse all categorized services on our Services page!"
            )

    if any(k in lower for k in ("staff", "owner", "human", "agent", "talk", "message", "chat")) and request.user.is_authenticated and request.user.role == User.Role.CUSTOMER:
        customer = _get_or_create_customer_for_user(request.user)
        SalonMessage.objects.create(
            customer=customer,
            sender=request.user,
            sender_name=customer.full_name,
            body=f"[Via Concierge Chatbot]: {prompt}",
            is_from_customer=True,
        )
        reply_parts.append(
            "💬 I've also forwarded your message directly to our Owner & Staff inbox! You can view and continue the conversation in **Messages**."
        )

    if not reply_parts:
        reply_parts.append(
            "✨ **Welcome to Get Nailed Nail Bar & Spa!**\n"
            "I can help you with:\n"
            "• **Location:** Unit 14 G/F Rocka Commercial Complex, Tabang, Plaridel, Bulacan\n"
            "• **Services:** Categorized nail care, gel extensions & spa packages\n"
            "• **Weekend Policy:** Fri–Sun priority for packages > ₱300 (walk-ins welcome for basic services)\n"
            "• **Reminders & Grace Period:** No advance payment, 4–5hr & same-day reminders, 10–15 min grace period\n"
            "• **Direct Staff Chat:** Click **Messages** in the navigation bar to chat directly with our Owner or Staff!"
        )

    return JsonResponse({"reply": "\n\n".join(reply_parts)})
