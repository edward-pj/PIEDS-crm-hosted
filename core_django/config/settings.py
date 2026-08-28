import sys
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent      # core_django/
REPO_ROOT = BASE_DIR.parent                            # ignite_crm/

# `shared` lives at the repo root and is imported by both apps.
sys.path.insert(0, str(REPO_ROOT))

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)
environ.Env.read_env(REPO_ROOT / ".env")

#: Django's test runner CREATEs and DROPs its database. Once DATABASE_URL points
#: at Supabase -- as it will in everyone's .env -- an ordinary `pytest` would aim
#: that at the hosted instance. Tests therefore always run against local Docker
#: Postgres, regardless of the environment. Override with IGNITE_TEST_DATABASE_URL.
#:
#: Read here rather than beside the database block because the production guards
#: below must not fire under pytest, which runs with DJANGO_DEBUG unset and so
#: sees DEBUG=False.
RUNNING_TESTS = "pytest" in sys.modules or "test" in sys.argv

#: Named so the guard below can recognise it. Never use this value anywhere.
INSECURE_DEV_SECRET_KEY = "dev-insecure-key-do-not-use-in-prod"

SECRET_KEY = env("DJANGO_SECRET_KEY", default=INSECURE_DEV_SECRET_KEY)
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

# Hosting turns each of the convenient dev fallbacks into a live vulnerability,
# so in production they fail the boot instead of applying quietly. A container
# running on the shipped key signs session cookies that anyone holding a copy of
# this repository can forge -- and a crash at startup is a deploy that visibly
# failed, where the fallback is a deploy that looks fine and is not.
if not DEBUG and not RUNNING_TESTS and SECRET_KEY == INSECURE_DEV_SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is still the shipped development key, and DEBUG is "
        "off. Set a real one:  python -c 'import secrets; "
        "print(secrets.token_urlsafe(64))'"
    )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "crm",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves the vendored Basecoat files in production without nginx.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "crm" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- database -------------------------------------------------------------
# In production this points at Supabase. Two things about that connection are
# load-bearing and easy to get wrong; see docs in README "Supabase".
#
#   1. Use the SESSION pooler (port 5432 on the pooler host). services/mailing.py
#      and services/assignment.py rely on SELECT ... FOR UPDATE, and the
#      transaction-mode pooler on 6543 breaks psycopg3's prepared statements.
#   2. sslmode=require belongs in the URL.

LOCAL_DATABASE_URL = "postgres://ignite:ignite@localhost:5432/ignite_crm"

if RUNNING_TESTS:
    DATABASE_URL = env("IGNITE_TEST_DATABASE_URL", default=LOCAL_DATABASE_URL)
else:
    DATABASE_URL = env("DATABASE_URL", default=None)
    if DATABASE_URL is None:
        # Falling back to localhost in a hosted container is the worst kind of
        # failure: the process boots, serves, and quietly talks to a database
        # that does not exist -- or, on a shared host, to the wrong one.
        if not DEBUG:
            raise ImproperlyConfigured(
                "DATABASE_URL is not set and DEBUG is off. Point it at the "
                "Supabase SESSION pooler (port 5432, not 6543) with "
                "?sslmode=require."
            )
        DATABASE_URL = LOCAL_DATABASE_URL

DATABASES = {"default": env.db_url_config(DATABASE_URL)}

# Persistent connections are off by default: a pooler has a finite slot count,
# and holding one open per gunicorn worker per request cycle exhausts the free
# tier long before traffic does.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=0)

# Needed only if someone deliberately switches to the transaction-mode pooler
# (:6543), where server-side prepared statements and cursors are not safe.
if env.bool("DB_TRANSACTION_POOLER", default=False):
    DATABASES["default"].setdefault("OPTIONS", {})["prepare_threshold"] = None
    DISABLE_SERVER_SIDE_CURSORS = True

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Basecoat CSS/JS is vendored once at the repo root and served by BOTH apps, so
# the Django CRM and the local FastAPI agent stay visually identical.
STATICFILES_DIRS = [REPO_ROOT / "shared" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

#: CSV import parses the whole upload and stashes every importable row in the
#: SESSION (crm/views.py::contact_import), and sessions are database-backed --
#: so an oversized import is written through the Supabase pooler row by row
#: before anyone has confirmed it. Django's own default is 2.5 MB; it is set
#: explicitly here because on a public host this is a deliberate ceiling rather
#: than an incidental one. A 2.5 MB CSV is roughly 20,000 contacts.
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("DATA_UPLOAD_MAX_MEMORY_SIZE", default=2621440)

#: Render captures stdout and nothing else; without this there is no application
#: logging at all in production, and a swallowed exception leaves no trace.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
            "formatter": "simple",
        },
    },
    "root": {"handlers": ["stdout"], "level": env("DJANGO_LOG_LEVEL", default="INFO")},
    "loggers": {
        # Unhandled exceptions in a view. Django logs these at ERROR and would
        # otherwise only mail them to ADMINS, which is configured nowhere.
        "django.request": {"handlers": ["stdout"], "level": "ERROR", "propagate": False},
    },
}

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# --- scheduled sending ----------------------------------------------------
# Gmail has no sendAt, so a scheduled mail goes out only when a tick runs to
# send it. These settings decide what "on time" means. See
# docs/MAIL_SCHEDULING.md.

#: Delivery window, in TIME_ZONE. A job falling due outside it waits for the
#: next open slot rather than mailing a prospect at three in the morning.
#:
#: **OFF by default** (start == end disables it), so mail sends whenever it is
#: queued. It defaulted to 09:00-19:00, which surprised people: a send pressed
#: at 20:00 went HELD rather than out, and "the button did nothing" is a worse
#: first impression than a mail arriving at an odd hour to a list you control.
#:
#: The machinery is intact, not deleted -- turn it back on by setting both, e.g.
#: SCHEDULE_WINDOW_START=9 and SCHEDULE_WINDOW_END=19. Worth doing before the
#: first large campaign to real prospects, when 3am delivery starts to cost
#: something. Restraint on cold outreach is a real concern; it just should not
#: be the thing standing between you and a test send.
SCHEDULE_WINDOW_START = env.int("SCHEDULE_WINDOW_START", default=0)
SCHEDULE_WINDOW_END = env.int("SCHEDULE_WINDOW_END", default=0)

#: Weekdays mail may go out. Monday is 0, matching datetime.weekday().
#: Default is all seven; set e.g. 0,1,2,3,4 for weekdays only.
SCHEDULE_WINDOW_DAYS = env.list(
    "SCHEDULE_WINDOW_DAYS", cast=int, default=[0, 1, 2, 3, 4, 5, 6]
)

#: How late a job may still go out. Measured from the moment it was first
#: ALLOWED to run, not from scheduled_at -- a job deferred overnight by the
#: window must not be declared missed for a lateness it could not avoid.
#:
#: 20 hours, not 6, because the queue is now drained by an external pinger on a
#: fixed daily window rather than by whoever happens to be looking at the CRM.
#: With the pinger running 10:00-17:00 IST, a send queued at 18:00 has nothing
#: to execute it until 10:00 the next morning -- a 16-hour wait that is correct
#: behaviour. At 6 hours the sweep marked every one of those MISSED around
#: midnight, and a member arrived to a queue of failures nobody had failed.
SCHEDULE_GRACE_HOURS = env.int("SCHEDULE_GRACE_HOURS", default=20)

#: Mails one member may send per rolling 24 hours. Enforced server-side by
#: mailing.claim_batch, so it holds across every device and every campaign.
#:
#: An operational number, not a code constant: it is the thing most likely to
#: need changing at short notice -- ramping a new team up, or backing off when
#: Gmail starts throttling -- and a redeploy is the wrong unit of work for that.
#: A cap-blocked contact keeps its place in the queue and goes out when the
#: window frees; it is a rate limit, never a refusal.
DAILY_SEND_CAP = env.int("DAILY_SEND_CAP", default=800)

#: Shared secret for GET /internal/tick, the route an external scheduler uses to
#: drain the send queue. Blank disables the route entirely (503) rather than
#: leaving it open -- see crm/tick_views.py::tick_enabled.
#:
#: Not an ApiToken and not a session: the pinger is not a team member. It can
#: only start work members already queued, so the blast radius of a leaked
#: secret is send TIMING, not content. Generate one with:
#:   python -c 'import secrets; print(secrets.token_urlsafe(32))'
TICK_SECRET = env("TICK_SECRET", default="")


# --- who may sign in ------------------------------------------------------
# Batch 2024 picks a name; batch 2025 signs in with Google. The reasoning is in
# crm/services/auth.py. This is a WEB OAuth client -- distinct from the DESKTOP
# client the local agent uses for Gmail. Leaving these blank disables the Google
# door and leaves a clear message on the login page rather than a stack trace.
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", default="")

# Rejects personal Gmail even if it is somehow in the pool. Verified against the
# signed `hd` claim, not the address string.
GOOGLE_OAUTH_HOSTED_DOMAIN = env(
    "GOOGLE_OAUTH_HOSTED_DOMAIN", default="pilani.bits-pilani.ac.in"
)

# --- sending mail --------------------------------------------------------
# The server sends on each member's behalf using a Gmail refresh token they
# granted in the browser. That token is encrypted at rest with this key; see
# crm/services/secrets.py for exactly what that does and does not protect.
#
# Generate one:
#   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
#
# LOSING THIS KEY means every stored token becomes unreadable and every member
# has to reconnect Gmail. It belongs in the hosting platform's environment and
# nowhere near the database, which is the whole point of encrypting with it.
GMAIL_TOKEN_KEY = env("GMAIL_TOKEN_KEY", default="")

#: Seconds of slack when deciding whether a cached access token is still usable.
#: A token that expires mid-flight fails the send it was fetched for.
GMAIL_TOKEN_EXPIRY_SKEW_SECONDS = env.int("GMAIL_TOKEN_EXPIRY_SKEW_SECONDS", default=120)

#: Pause between messages within one send run. Gmail's per-account quota is
#: real and tripping it throttles the mailbox for hours.
GMAIL_SEND_DELAY_SECONDS = env.float("GMAIL_SEND_DELAY_SECONDS", default=0.0)

# --- production hardening -------------------------------------------------
# The CRM is reachable from the public internet, so TLS is not optional once
# DEBUG is off.

# Set unconditionally: a cookie that a cross-site request can carry is a CSRF
# hole in development too, and "Lax" still allows the top-level GET redirect
# that Google's OAuth callback arrives as.
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

# `not RUNNING_TESTS` is load-bearing, not defensive. Without it the whole block
# applies under pytest -- there is no .env in a fresh checkout, so DEBUG is
# False -- and SECURE_SSL_REDIRECT turns every self.client.get() into a 301
# before it reaches a view. The suite then silently tests TLS redirects instead
# of the CRM, and its result depends on whether an untracked file exists.
# The production block is exercised by setting its settings explicitly in the
# tests that care (see tests/test_login.py::TestHealthz).
if not DEBUG and not RUNNING_TESTS:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }
    SECURE_SSL_REDIRECT = True

    # The platform health check reaches the container on its own port over plain
    # HTTP, with no X-Forwarded-Proto header. Without this exemption /healthz
    # answers 301 rather than 200 and every deploy is marked unhealthy -- which
    # presents as a broken application rather than as a misconfigured redirect.
    # Matched against request.path with the leading slash stripped.
    SECURE_REDIRECT_EXEMPT = [r"^healthz$"]

    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    X_FRAME_OPTIONS = "DENY"

    # Explicit first, derived second.
    #
    # The derivation has to translate, not concatenate: the two settings spell a
    # subdomain wildcard differently. ALLOWED_HOSTS uses a leading dot
    # (".onrender.com"); CSRF_TRUSTED_ORIGINS wants an explicit star
    # ("https://*.onrender.com"). Pasting a scheme onto the former yields
    # "https://.onrender.com", which matches no host at all -- and that failure
    # surfaces as a 403 on form submission from the very subdomain you deployed
    # to, long after the deploy looked successful.
    #
    # ALLOWED_HOSTS=["*"] derives to an empty list, which is why the explicit
    # environment variable exists and why it is consulted first.
    def _as_csrf_origin(host):
        return f"https://*{host}" if host.startswith(".") else f"https://{host}"

    CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[]) or [
        _as_csrf_origin(host) for host in ALLOWED_HOSTS if host != "*"
    ]
