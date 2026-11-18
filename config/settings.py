import os
import sys
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(nome, padrao=False):
    v = os.environ.get(nome)
    if v is None:
        return padrao
    return v.strip().lower() in ("1", "true", "sim", "yes", "on")


DEBUG = env_bool("DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-inseguro-apenas-local"
    else:
        # Sem chave definida: gera uma efemera (sessoes invalidam ao reiniciar).
        # Em producao defina DJANGO_SECRET_KEY.
        from django.core.management.utils import get_random_secret_key
        SECRET_KEY = get_random_secret_key()

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", ".satela.com.br,localhost,127.0.0.1").split(",")
    if h.strip()
]

_csrf = os.environ.get("CSRF_TRUSTED_ORIGINS")
if _csrf:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf.split(",") if o.strip()]
else:
    CSRF_TRUSTED_ORIGINS = []
    for h in ALLOWED_HOSTS:
        if h in ("*", "localhost", "127.0.0.1"):
            continue
        CSRF_TRUSTED_ORIGINS.append("https://" + (("*" + h) if h.startswith(".") else h))

DEMO_MODE = env_bool("DEMO_MODE", True)
DEMO_ADMIN_PASSWORD = os.environ.get("DEMO_ADMIN_PASSWORD", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "fin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "fin.middleware.UsuarioAtualMiddleware",
    "fin.middleware.LoginObrigatorioMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "fin.context_processors.global_ctx",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=60,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = False

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

if DEBUG or "test" in sys.argv:
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

MEDIA_URL = "media/"
MEDIA_ROOT = Path("/data/media") if Path("/data").exists() else BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/entrar/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/entrar/"

# Limites de upload
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000
LIMITE_ANEXO_BYTES = 2 * 1024 * 1024
LIMITE_EXTRATO_BYTES = 1 * 1024 * 1024

# Nenhum e-mail real e enviado.
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"

# Rate limit simples de login
LOGIN_MAX_TENTATIVAS = 5
LOGIN_JANELA_SEGUNDOS = 300

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Seguranca
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)  # o proxy da plataforma ja termina TLS
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_REDIRECT_EXEMPT = [r"^saude/$"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
