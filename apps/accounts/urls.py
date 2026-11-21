from django.urls import path

from .views import (
    BrandedLoginView,
    BrandedLogoutView,
    GenericPasswordResetCompleteView,
    GenericPasswordResetConfirmView,
    GenericPasswordResetDoneView,
    GenericPasswordResetView,
    SecurePasswordChangeView,
    activate_account,
    activation_password,
    invite_internal_account,
    login_lockouts,
    logout_all_devices,
    mfa_configuration,
    update_account_access,
)

app_name = "accounts"
urlpatterns = [
    path("login/", BrandedLoginView.as_view(), name="login"),
    path("logout/", BrandedLogoutView.as_view(), name="logout"),
    path("logout-all/", logout_all_devices, name="logout_all_devices"),
    path("password-change/", SecurePasswordChangeView.as_view(), name="password_change"),
    path("internal/invite/", invite_internal_account, name="invite_internal"),
    path("internal/accounts/<int:user_id>/access/", update_account_access, name="update_account_access"),
    path("internal/mfa-configuration/", mfa_configuration, name="mfa_configuration"),
    path("internal/login-lockouts/", login_lockouts, name="login_lockouts"),
    path("activate/<uuid:public_id>/set-password/", activation_password, name="activation_password"),
    path("activate/<uuid:public_id>/<str:token>/", activate_account, name="activate"),
    path("password-reset/", GenericPasswordResetView.as_view(), name="password_reset"),
    path("password-reset/sent/", GenericPasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "password-reset/<uidb64>/<token>/",
        GenericPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        GenericPasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
]
