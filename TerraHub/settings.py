"""Django settings for TerraHub."""

from pathlib import Path
import os
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')


# Development settings (see the Django deployment checklist before production)

from django.core.exceptions import ImproperlyConfigured

# DEBUG defaults to True locally and to False on Railway (which sets RAILWAY_ENVIRONMENT_NAME); DEBUG=True/False overrides.
DEBUG = os.getenv('DEBUG', 'False' if (os.getenv('RAILWAY_ENVIRONMENT') or os.getenv('RAILWAY_ENVIRONMENT_NAME')) else 'True') == 'True'

# SECRET_KEY: the insecure dev key is only allowed while DEBUG is on; production refuses to start without SECRET_KEY.
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('SECRET_KEY must be set when DEBUG is False.')
    SECRET_KEY = 'django-insecure-q-%3+dj4%@gt&773333mbq41=6abjt9j%=3@n+spibj9t#3jv0'

# Locally: accepts all. On Railway: set ALLOWED_HOSTS=your-app.up.railway.app
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

# Required in production for CSRF to work — must include full https:// URL
CSRF_TRUSTED_ORIGINS = os.getenv('CSRF_TRUSTED_ORIGINS', 'http://localhost').split(',')


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'core.apps.CoreConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # Serves static files in production
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'core.account.IdleTimeoutMiddleware',
    'core.account.ForcePasswordChangeMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'TerraHub.urls'

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
                'core.context_processors.approvals_count',
                'core.context_processors.sidebar_nav',
                'core.context_processors.session_timeout',
                'core.context_processors.denied_actions',
            ],
        },
    },
]

WSGI_APPLICATION = 'TerraHub.wsgi.application'

# Cache used only by analytics (core/analytics_cache.py). Local-memory is per worker, so changes reach other workers after the short TTL. Tests use a dummy cache.
import sys
if 'test' in sys.argv:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}}
else:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


# Database

DB_ENGINE = os.getenv('DB_ENGINE', 'django.db.backends.postgresql')

if DB_ENGINE == 'django.db.backends.sqlite3':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('DB_NAME', 'TerraHub'),
            'USER': os.getenv('DB_USER', 'postgres'),
            'PASSWORD': os.getenv('DB_PASSWORD', 'postgres'),
            'HOST': os.getenv('DB_HOST', 'localhost'),
            'PORT': os.getenv('DB_PORT', '5050'),
        }
    }

# Railway PostgreSQL — auto-overrides DB config when DATABASE_URL env var is injected
import dj_database_url
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    DATABASES['default'] = dj_database_url.config(default=DATABASE_URL, conn_max_age=600)


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Tests only: a fast (insecure) hasher so creating test users doesn't dominate run time
if 'test' in sys.argv:
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']


# Internationalization

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Kuala_Lumpur'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, images)

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'  # Output folder for collectstatic
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Custom Authentication Configuration
AUTH_USER_MODEL = 'core.CustomUser'
LOGIN_REDIRECT_URL = 'dashboard'
LOGIN_URL = 'login'
LOGOUT_REDIRECT_URL = 'home'

# Idle timeout: sessions end after this long with no requests; static/js/session-timeout.js keeps active tabs alive via session_keepalive.
# This is the cap: the System Settings page can shorten it (core.account.IdleTimeoutMiddleware) but not raise it.
SESSION_IDLE_TIMEOUT = int(os.getenv('SESSION_IDLE_TIMEOUT_MINUTES', '30')) * 60
SESSION_IDLE_WARNING = 120  # seconds of warning popup before sign-out
SESSION_COOKIE_AGE = SESSION_IDLE_TIMEOUT
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True


# HTTPS hardening, only when DEBUG is off so localhost over plain http keeps working.
# Railway terminates TLS in front of the app and passes the original scheme in X-Forwarded-Proto.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True') == 'True'
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # One day to start with; raise once HTTPS is confirmed working (HSTS can't be undone for browsers that saw it).
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '86400'))

# Login throttling (core/login_throttle.py): after this many failed sign-ins for one username from one IP
# (or 4x that from any IPs), further attempts are refused until the window passes. Uses the default cache.
LOGIN_FAIL_LIMIT = int(os.getenv('LOGIN_FAIL_LIMIT', '5'))
LOGIN_LOCKOUT_SECONDS = int(os.getenv('LOGIN_LOCKOUT_MINUTES', '15')) * 60
AUTHENTICATION_BACKENDS = ['core.login_throttle.ThrottledModelBackend']
