from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django.views.static import serve
from apps.customers.views import CustomerLoginView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('security/', include('apps.accounts.allauth_urls')),
    path('security/2fa/', include('apps.accounts.mfa_urls')),
    path('captcha/', include('captcha.urls')),
    path('', include('apps.core.urls')),
    path('login/customer/', CustomerLoginView.as_view(), name='customer_login_alt'),
    path('accounts/', include('allauth.socialaccount.providers.google.urls')),
    path('accounts/', include('allauth.socialaccount.urls')),
    path('accounts/', include('apps.accounts.urls')),
    path('services/', include('apps.services.urls')),
    path('customers/', include('apps.customers.urls')),
    path('bookings/', include('apps.bookings.urls')),
    path('book/', RedirectView.as_view(url='/bookings/', permanent=False)),
    path('pos/', include('apps.pos.urls')),
    path('monitoring/', include('apps.monitoring.urls')),
    path('reports/', include('apps.reports.urls')),
    path('forecasting/', include('apps.forecasting.urls')),
    path('notifications/', include('apps.notifications.urls')),
    path('audit-trail/', include('apps.audittrail.urls')),
    re_path(r'^static/(?P<path>.*)$', serve, {'document_root': settings.BASE_DIR / 'static'}),
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]
