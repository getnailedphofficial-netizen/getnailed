from django.urls import path
from . import views

app_name = "core"
urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard_router, name="dashboard_router"),
    path("dashboard/owner/", views.owner_dashboard, name="owner_dashboard"),
    path("system-health/", views.system_health, name="system_health"),
    path("dashboard/staff/", views.staff_dashboard, name="staff_dashboard"),
    path("dashboard/customer/", views.customer_dashboard, name="customer_dashboard"),
    path("dashboard/customer/appointments/", views.customer_appointments, name="customer_appointments"),
    path("dashboard/customer/receipt/<str:receipt_number>/", views.customer_receipt, name="customer_receipt"),
    path("dashboard/customer/services/", views.customer_services, name="customer_services"),
    path("dashboard/customer/profile/", views.customer_profile, name="customer_profile"),
]
