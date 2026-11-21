from django.conf import settings


def branding(request):
    ctx = {'BRAND_NAME': settings.BRAND_NAME}
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        try:
            from apps.notifications.models import Notification, SalonMessage
            ctx['unread_notifications_count'] = Notification.objects.filter(
                recipient=user, read_at__isnull=True
            ).count()
            if getattr(user, 'is_customer', False):
                customer = getattr(user, 'customer_profile', None)
                ctx['unread_messages_count'] = (
                    SalonMessage.objects.filter(
                        customer=customer, is_from_customer=False, is_read=False
                    ).count()
                    if customer
                    else 0
                )
            else:
                ctx['unread_messages_count'] = SalonMessage.objects.filter(
                    is_from_customer=True, is_read=False
                ).count()
        except Exception:
            ctx['unread_notifications_count'] = 0
            ctx['unread_messages_count'] = 0
    return ctx
