from django.urls import path
from . import views

app_name = 'pos'
urlpatterns = [
    path('', views.index, name='index'),
    path('shifts/', views.shift_dashboard, name='shift_dashboard'),
    path('shifts/open/', views.shift_open, name='shift_open'),
    path('shifts/close/', views.shift_close, name='shift_close'),
    path('shifts/<int:pk>/reconciliation/', views.shift_reconciliation, name='shift_reconciliation'),
    path('shifts/history/', views.shift_history, name='shift_history'),
    path('vouchers/', views.voucher_list, name='voucher_list'),
    path('vouchers/create/', views.voucher_create, name='voucher_create'),
    path('history/', views.history, name='history'),
    path('receipt/<str:receipt_number>/', views.receipt, name='receipt'),
    path('api/customer/quick-create/', views.api_quick_create_customer, name='api_quick_create_customer'),
    path('api/appointment/<int:pk>/', views.api_appointment_details, name='api_appointment_details'),
    path('api/appointment/<int:pk>/add-service/', views.api_add_service_to_appointment, name='api_add_service_to_appointment'),
    path('api/technician-slots/', views.api_technician_slots, name='api_technician_slots'),
    path('void-approval/', views.void_approval, name='void_approval'),
    path('void-approval/<str:receipt_number>/', views.void_detail, name='void_detail'),
]
