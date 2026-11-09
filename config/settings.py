from pathlib import Path
import os
from datetime import timedelta
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
try:
    import dj_database_url
except ImportError:
    dj_database_url = None

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / '.env', override=True)

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'change-this-development-key')
ENVIRONMENT = os.getenv('DJANGO_ENV', 'development').strip().lower()
if ENVIRONMENT not in {'development', 'test', 'production'}:
    raise ImproperlyConfigured(
        'DJANGO_ENV must be development, test, or production.'
    )
IS_PRODUCTION = ENVIRONMENT == 'production'
DEBUG = os.getenv('DJANGO_DEBUG', 'True').lower() == 'true'
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv('DJANGO_ALLOWED_HOSTS', '').split(',')
    if host.strip()
]
for default_host in ['127.0.0.1', 'localhost', 'getnailed.vercel.app']:
    if default_host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(default_host)

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]
for default_origin in ['https://getnailed.vercel.app', 'https://*.vercel.app']:
    if default_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(default_origin)

vercel_url = os.getenv('VERCEL_URL')
if vercel_url:
    if vercel_url not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(vercel_url)
    origin = f'https://{vercel_url}'
    if origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(origin)

if IS_PRODUCTION and DEBUG:
    raise ImproperlyConfigured('DJANGO_DEBUG must be False in production.')
if not DEBUG and SECRET_KEY == 'change-this-development-key':
    raise ImproperlyConfigured('DJANGO_SECRET_KEY must be configured outside development.')
if IS_PRODUCTION and (
    len(SECRET_KEY) < 50
    or SECRET_KEY in {'change-this-development-key', 'replace-this-value'}
):
    raise ImproperlyConfigured('Production requires a strong DJANGO_SECRET_KEY.')
if IS_PRODUCTION and any(host == '*' or host.startswith('.') for host in ALLOWED_HOSTS):
    raise ImproperlyConfigured('Production requires explicit DJANGO_ALLOWED_HOSTS without wildcards.')
if IS_PRODUCTION and (
    not CSRF_TRUSTED_ORIGINS
    or any(
        urlparse(origin).scheme != 'https' or not urlparse(origin).netloc
        for origin in CSRF_TRUSTED_ORIGINS
    )
):
    raise ImproperlyConfigured(
        'Production requires HTTPS DJANGO_CSRF_TRUSTED_ORIGINS.'
    )

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'allauth',
    'allauth.account',
    'allauth.mfa',
    'axes',
    'captcha',
    'apps.core',
    'apps.accounts',
    'apps.services',
    'apps.customers',
    'apps.bookings',
    'apps.pos',
    'apps.monitoring',
    'apps.reports',
    'apps.forecasting',
    'apps.notifications',
    'apps.audittrail',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'apps.audittrail.request_middleware.RequestIDMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'apps.accounts.middleware.SessionSecurityMiddleware',
    'apps.accounts.middleware.RequiredMFAMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.audittrail.middleware.AuditTrailMiddleware',
    'axes.middleware.AxesMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.core.context_processors.branding',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

IS_VERCEL = bool(os.getenv('VERCEL') or os.getenv('VERCEL_ENV') or os.getenv('AWS_LAMBDA_FUNCTION_NAME') or '/var/task' in str(BASE_DIR))

if os.getenv("DATABASE_URL") and dj_database_url:
    DATABASES = {"default": dj_database_url.parse(os.getenv("DATABASE_URL"))}
else:
    if IS_VERCEL or IS_PRODUCTION:
        tmp_db = Path('/tmp/db.sqlite3')
        source_db = BASE_DIR / 'db.sqlite3'
        if source_db.exists() and not tmp_db.exists():
            import shutil
            try:
                shutil.copyfile(source_db, tmp_db)
                os.chmod(tmp_db, 0o666)
            except Exception:
                pass
        db_path = str(tmp_db)
    else:
        db_path = os.getenv('DB_NAME', str(BASE_DIR / 'db.sqlite3'))

    DATABASES = {
        'default': {
            'ENGINE': os.getenv('DB_ENGINE', 'django.db.backends.sqlite3'),
            'NAME': db_path,
            'USER': os.getenv('DB_USER', ''),
            'PASSWORD': os.getenv('DB_PASSWORD', ''),
            'HOST': os.getenv('DB_HOST', ''),
            'PORT': os.getenv('DB_PORT', ''),
        }
    }

CACHE_BACKEND = os.getenv(
    'DJANGO_CACHE_BACKEND',
    'django.core.cache.backends.locmem.LocMemCache',
)
CACHE_LOCATION = os.getenv('DJANGO_CACHE_LOCATION', 'get-nailed-development')
if IS_PRODUCTION and not IS_VERCEL and CACHE_BACKEND != 'django.core.cache.backends.redis.RedisCache':
    raise ImproperlyConfigured('Production requires the shared Redis cache backend.')
if IS_PRODUCTION and (not IS_VERCEL or CACHE_BACKEND == 'django.core.cache.backends.redis.RedisCache'):
    cache_url = urlparse(CACHE_LOCATION)
    if cache_url.scheme not in {'redis', 'rediss'} or not cache_url.hostname:
        raise ImproperlyConfigured(
            'Production DJANGO_CACHE_LOCATION must be a Redis URL.'
        )
CACHES = {
    'default': {
        'BACKEND': CACHE_BACKEND,
        'LOCATION': CACHE_LOCATION,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 12},
    },
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.Argon2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher',
    'django.contrib.auth.hashers.ScryptPasswordHasher',
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = Path(os.getenv('DJANGO_MEDIA_ROOT', BASE_DIR / 'media')).resolve()

if IS_PRODUCTION and not os.getenv('DJANGO_MEDIA_ROOT'):
    if IS_VERCEL:
        MEDIA_ROOT = Path('/tmp/media').resolve()
    else:
        raise ImproperlyConfigured('Production requires an explicit DJANGO_MEDIA_ROOT.')
for static_location in [STATIC_ROOT, *STATICFILES_DIRS]:
    static_location = Path(static_location).resolve()
    if (
        MEDIA_ROOT == static_location
        or MEDIA_ROOT.is_relative_to(static_location)
        or static_location.is_relative_to(MEDIA_ROOT)
    ):
        raise ImproperlyConfigured('DJANGO_MEDIA_ROOT must be outside static-file locations.')

MAX_IMAGE_UPLOAD_SIZE = int(os.getenv('MAX_IMAGE_UPLOAD_SIZE_MB', '5')) * 1024 * 1024
MAX_IMAGE_UPLOAD_PIXELS = int(os.getenv('MAX_IMAGE_UPLOAD_PIXELS', '20000000'))
DATA_UPLOAD_MAX_NUMBER_FILES = int(os.getenv('DATA_UPLOAD_MAX_NUMBER_FILES', '5'))
FILE_UPLOAD_PERMISSIONS = 0o640
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o750

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'accounts.User'
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'apps.accounts.backends.EligibleUserBackend',
]
LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'core:dashboard_router'
LOGOUT_REDIRECT_URL = 'core:home'

SESSION_ENGINE = os.getenv(
    'DJANGO_SESSION_ENGINE',
    'django.contrib.sessions.backends.db',
)
if SESSION_ENGINE not in {
    'django.contrib.sessions.backends.db',
    'django.contrib.sessions.backends.cached_db',
}:
    raise ImproperlyConfigured('DJANGO_SESSION_ENGINE must use db or cached_db sessions.')
OWNER_INACTIVITY_TIMEOUT = int(os.getenv('OWNER_INACTIVITY_TIMEOUT_MINUTES', '30')) * 60
STAFF_INACTIVITY_TIMEOUT = int(os.getenv('STAFF_INACTIVITY_TIMEOUT_MINUTES', '60')) * 60

BRAND_NAME = 'Get Nailed Nail Bar and Spa'
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '')

if not DEBUG and not PUBLIC_BASE_URL:
    raise ImproperlyConfigured('PUBLIC_BASE_URL must be configured outside development.')
if IS_PRODUCTION:
    public_url = urlparse(PUBLIC_BASE_URL)
    if public_url.scheme != 'https' or not public_url.netloc:
        raise ImproperlyConfigured('Production PUBLIC_BASE_URL must be an HTTPS URL.')

ACCOUNT_ACTIVATION_TIMEOUT = int(os.getenv('ACCOUNT_ACTIVATION_TIMEOUT', '86400'))
ACCOUNT_ACTIVATION_MAX_ATTEMPTS = int(os.getenv('ACCOUNT_ACTIVATION_MAX_ATTEMPTS', '5'))
PASSWORD_RESET_TIMEOUT = int(os.getenv('PASSWORD_RESET_TIMEOUT', '3600'))

ACCOUNT_ADAPTER = 'apps.accounts.adapters.AccountAdapter'
ACCOUNT_FORMS = {'login': 'apps.accounts.forms.BrandedAllauthLoginForm'}
ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_REAUTHENTICATION_TIMEOUT = int(
    os.getenv('SENSITIVE_ACTION_REAUTHENTICATION_MINUTES', '5')
) * 60
ACCOUNT_LOGOUT_ON_GET = False
ACCOUNT_RATE_LIMITS = {'login_failed': None}

MFA_ADAPTER = 'apps.accounts.adapters.EncryptedMFAAdapter'
MFA_FORMS = {
    'authenticate': 'apps.accounts.forms.MFAAuthenticateForm',
    'reauthenticate': 'apps.accounts.forms.MFAReauthenticateForm',
}
MFA_SUPPORTED_TYPES = ['totp']
MFA_ALLOW_UNVERIFIED_EMAIL = False
MFA_TOTP_ISSUER = BRAND_NAME
# 10 steps x 30 seconds = 300 seconds (5 minutes validity)
MFA_TOTP_TOLERANCE = int(os.getenv('MFA_TOTP_TOLERANCE', '10'))
MFA_TRUST_ENABLED = False
MFA_RECOVERY_CODES_SHOW_ONCE = True
MFA_RECOVERY_CODE_COUNT = int(os.getenv('MFA_RECOVERY_CODE_COUNT', '10'))
MFA_ENCRYPTION_KEY = os.getenv('MFA_ENCRYPTION_KEY', 'kTBqXCXXq2hbIcGaTBaetjnbmtdGR4qWlENY9Kpgjbw')
MFA_ENFORCE_OWNER = os.getenv('MFA_ENFORCE_OWNER', 'True').lower() == 'true'
MFA_REQUIRE_INTERNAL_USERS = os.getenv('MFA_REQUIRE_INTERNAL_USERS', 'False').lower() == 'true'
MFA_FAILURE_LIMIT = int(os.getenv('MFA_FAILURE_LIMIT', '5'))
MFA_FAILURE_TIMEOUT = int(os.getenv('MFA_FAILURE_TIMEOUT_MINUTES', '15')) * 60
if MFA_FAILURE_LIMIT < 1 or MFA_FAILURE_TIMEOUT < 1:
    raise ImproperlyConfigured('MFA failure limit and timeout must be positive.')
if IS_PRODUCTION:
    mfa_keys = [key.strip() for key in MFA_ENCRYPTION_KEY.split(',') if key.strip()]
    if (
        not mfa_keys
        or any(len(key) < 32 for key in mfa_keys)
        or MFA_ENCRYPTION_KEY == 'replace-with-a-long-random-secret'
    ):
        raise ImproperlyConfigured('Production requires strong MFA encryption keys.')

AXES_FAILURE_LIMIT = int(os.getenv('LOGIN_FAILURE_LIMIT', '5'))
AXES_COOLOFF_TIME = timedelta(minutes=int(os.getenv('LOGIN_LOCKOUT_MINUTES', '15')))
AXES_LOCKOUT_PARAMETERS = [['username', 'ip_address']]
AXES_USERNAME_FORM_FIELD = 'login'
AXES_USERNAME_CALLABLE = 'apps.accounts.login_security.normalize_axes_username'
AXES_RESET_ON_SUCCESS = True
AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT = False
AXES_USE_ATTEMPT_EXPIRATION = True
AXES_NEVER_LOCKOUT_GET = True
AXES_ENABLE_ACCESS_FAILURE_LOG = True
AXES_ACCESS_FAILURE_LOG_PER_USER_LIMIT = 100
AXES_DISABLE_ACCESS_LOG = False
AXES_ENABLE_ADMIN = False
AXES_HTTP_RESPONSE_CODE = 429
AXES_LOCKOUT_TEMPLATE = 'accounts/login_locked.html'
AXES_COOLOFF_MESSAGE = 'Too many sign-in attempts. Try again later.'
AXES_SENSITIVE_PARAMETERS = [
    'login',
    'password',
    'old_password',
    'new_password1',
    'new_password2',
    'captcha_0',
    'captcha_1',
    'code',
    'token',
    'secret',
    'key',
]
ENABLE_LOGIN_CAPTCHA = os.getenv('ENABLE_LOGIN_CAPTCHA', 'False').lower() in ('true', '1')
LOGIN_CAPTCHA_THRESHOLD = int(os.getenv('LOGIN_CAPTCHA_THRESHOLD', '3'))
CAPTCHA_TIMEOUT = 5
CAPTCHA_LENGTH = 5

BOOKING_UNVERIFIED_TIMEOUT = timedelta(
    minutes=int(os.getenv('BOOKING_UNVERIFIED_TIMEOUT_MINUTES', '30'))
)
BOOKING_VERIFICATION_CODE_TIMEOUT = timedelta(
    minutes=int(os.getenv('BOOKING_VERIFICATION_CODE_TIMEOUT_MINUTES', '15'))
)
BOOKING_VERIFICATION_MAX_ATTEMPTS = int(
    os.getenv('BOOKING_VERIFICATION_MAX_ATTEMPTS', '5')
)
BOOKING_RESEND_COOLDOWN = timedelta(
    minutes=int(os.getenv('BOOKING_RESEND_COOLDOWN_MINUTES', '5'))
)
BOOKING_MAX_RESENDS = int(os.getenv('BOOKING_MAX_RESENDS', '3'))
BOOKING_ACCESS_TOKEN_TIMEOUT = timedelta(
    hours=int(os.getenv('BOOKING_ACCESS_TOKEN_TIMEOUT_HOURS', '72'))
)

EMAIL_BACKEND = os.getenv(
    'DJANGO_EMAIL_BACKEND',
    'django.core.mail.backends.smtp.EmailBackend'
    if IS_PRODUCTION
    else 'django.core.mail.backends.console.EmailBackend',
)
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', 'godzu1890@gmail.com')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True').lower() == 'true'
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', 'False').lower() == 'true'
EMAIL_TIMEOUT = int(os.getenv('EMAIL_TIMEOUT', '10'))
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'godzu1890@gmail.com')
SERVER_EMAIL = DEFAULT_FROM_EMAIL

if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured('EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled.')
if IS_PRODUCTION:
    if EMAIL_BACKEND != 'django.core.mail.backends.smtp.EmailBackend':
        raise ImproperlyConfigured('Production requires the SMTP email backend.')
    required_email_settings = {
        'EMAIL_HOST': os.getenv('EMAIL_HOST'),
        'EMAIL_HOST_USER': EMAIL_HOST_USER,
        'EMAIL_HOST_PASSWORD': EMAIL_HOST_PASSWORD,
        'DEFAULT_FROM_EMAIL': os.getenv('DEFAULT_FROM_EMAIL'),
    }
    missing_email_settings = [
        name for name, value in required_email_settings.items() if not value
    ]
    if missing_email_settings:
        raise ImproperlyConfigured(
            'Production email settings are missing: ' + ', '.join(missing_email_settings)
        )

PASSWORD_RESET_EMAIL_LIMIT = int(os.getenv('PASSWORD_RESET_EMAIL_LIMIT', '3'))
PASSWORD_RESET_EMAIL_WINDOW = int(os.getenv('PASSWORD_RESET_EMAIL_WINDOW_MINUTES', '15')) * 60
SECURITY_EMAIL_LIMIT = int(os.getenv('SECURITY_EMAIL_LIMIT', '5'))
SECURITY_EMAIL_WINDOW = int(os.getenv('SECURITY_EMAIL_WINDOW_MINUTES', '60')) * 60

SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', str(IS_PRODUCTION)).lower() == 'true'
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', str(IS_PRODUCTION)).lower() == 'true'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax')
CSRF_COOKIE_SECURE = os.getenv('CSRF_COOKIE_SECURE', str(IS_PRODUCTION)).lower() == 'true'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
X_FRAME_OPTIONS = 'DENY'
ENABLE_HSTS = os.getenv('ENABLE_HSTS', 'False').lower() == 'true'
SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000')) if ENABLE_HSTS else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = ENABLE_HSTS
SECURE_HSTS_PRELOAD = ENABLE_HSTS

# Additional recommended security headers
SECURE_BROWSER_XSS_FILTER = True
SECURE_PROXY_SSL_HEADER = (
    ('HTTP_X_FORWARDED_PROTO', 'https')
    if (IS_VERCEL or os.getenv('USE_PROXY', 'False').lower() == 'true')
    else None
)
