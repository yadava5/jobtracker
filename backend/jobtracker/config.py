"""
Configuration Module
====================

Application settings and configuration management using pydantic-settings.

All settings can be overridden via environment variables with the
JOBTRACKER_ prefix. For example:
    JOBTRACKER_SYNC_BATCH_SIZE=250
    JOBTRACKER_LOG_LEVEL=DEBUG

Settings are loaded from:
1. Default values defined in this file
2. .env file in the backend directory (if exists)
3. Environment variables (highest priority)

Usage:
------
    from jobtracker.config import settings

    print(settings.sync_batch_size)  # 100
    print(settings.database_path)  # ~/Library/Application Support/JobTracker/jobtracker.db

Both examples name fields that something READS. That is not incidental: the
previous pair demonstrated ``api_port`` and ``api_host``, and both were deleted
in #645 as fields nothing consumed — a module docstring teaching an example
that the module no longer contains. ``tests/test_no_dead_settings_fields.py``
is what stops a field outliving its last reader; nothing stops a docstring
outliving its subject except reading it.
"""

import os
import urllib.parse
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class CronSyncUserIdsError(RuntimeError):
    """``JOBTRACKER_CRON_SYNC_USER_IDS`` holds something that is not a UUID.

    DELIBERATELY NOT A ``ValueError``, AND THAT IS THE WHOLE POINT.

    Pydantic v2 catches ``ValueError``/``AssertionError`` out of a validator
    and re-raises them as a ``ValidationError`` whose rendering appends
    ``input_value=<the raw value>`` — for a ``NoDecode`` field that is the
    **entire env var string**, verbatim. So a validator that says "the value
    is not echoed" while raising ``ValueError`` is simply wrong: pydantic
    echoes it a line later. That is not hypothetical; it was measured on
    pydantic 2.12, and the test that was supposed to catch it passed only
    because the string happened to be long enough for pydantic to truncate.

    Any other exception type propagates out of the validator untouched, so the
    message below is all the operator (and all the log) ever sees. The
    realistic mishap this protects against is not an attacker — it is pasting
    the wrong variable's contents into this box, e.g. the cron secret, and
    then finding it verbatim in a build log.

    It still fails at config load, which is the required behaviour: a non-UUID
    entry would reach the RLS GUC listener as a ``str``, bind no identity at
    all, and read zero rows without raising.
    """


class TrainingAllowedUserIdsError(RuntimeError):
    """``JOBTRACKER_TRAINING_ALLOWED_USER_IDS`` holds something that is not a UUID.

    Not a ``ValueError``, for the reason spelled out on
    :class:`CronSyncUserIdsError` — pydantic re-renders a ``ValueError`` out of
    a validator with ``input_value=<the entire env var string>`` appended, so
    a validator that withholds the value while raising ``ValueError`` does not
    actually withhold it.

    Failing at config load is the required behaviour and not merely tidy. A
    malformed entry that survived parsing would be a ``str`` in a list of
    ``uuid.UUID``, would compare equal to no user id, and the training gate
    would then refuse *everyone* — silently, and identically to the "you
    forgot to set it" case. Default-deny is supposed to be a decision, not an
    accident nobody can tell apart from a typo.
    """


class Settings(BaseSettings):
    """
    Application settings with environment variable support.

    All settings can be overridden via environment variables
    prefixed with JOBTRACKER_.
    """

    model_config = SettingsConfigDict(
        env_prefix="JOBTRACKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Application
    # -------------------------------------------------------------------------
    app_name: str = "Applied"
    app_version: str = "0.1.0"
    # Environment:
    # - development: normal local runs (uses on-disk SQLite DB)
    # - production: same as development for now, but reserved for future tuning
    # - test: in-memory SQLite DB for pytest (never touches real data)
    #
    # THIS VALUE IS A DEFAULT, NOT A MEASUREMENT. Nothing sets
    # JOBTRACKER_ENVIRONMENT on Vercel, so the deployed production API has
    # reported "development" since the day it shipped -- not because anyone
    # decided that, but because nobody decided anything. Use
    # ``environment_is_configured`` to tell the two apart, and never build a
    # security decision on this field alone: a condition of the form
    # ``if settings.environment == "production"`` is false in production and
    # is therefore a check that cannot fail.
    environment: Literal["development", "production", "test"] = "development"

    # Interactive API documentation for the CLOUD app: /docs (Swagger UI),
    # /redoc, and the /openapi.json document that feeds them. The desktop app
    # in jobtracker/main.py does not consult this setting -- it binds
    # 127.0.0.1 and is not a published surface.
    #
    # OFF BY DEFAULT, AND DELIBERATELY NOT DERIVED FROM ``environment``.
    # The obvious gate -- "serve docs unless environment == production" --
    # cannot work here for the reason spelled out above: the deployed API
    # *is* "development" as far as this config can tell, so that gate would
    # keep publishing the full interactive API surface to anyone who asked.
    # An independent switch whose default is the safe value inverts the
    # failure mode: a deployment configured with nothing at all serves no
    # docs, and a docs endpoint can only exist because somebody explicitly
    # asked for one.
    enable_docs: bool = Field(
        default=False,
        description=(
            "Serve the cloud app's interactive API docs (/docs, /redoc, "
            "/openapi.json). Off unless explicitly enabled -- set "
            "JOBTRACKER_ENABLE_DOCS=true for local development. A cloud "
            "deployment that configures nothing serves no docs. The desktop "
            "app does not read this setting."
        ),
    )

    # Deployment target. "desktop" keeps every existing assumption (SQLite,
    # Keychain, WebSocket router, localhost CORS). "cloud" selects the
    # Vercel-safe code paths (Postgres via DATABASE_URL, encrypted-column
    # credentials, polling, env-driven CORS). Downstream issues wire the
    # cloud paths in one at a time; this flag only gates which app builder
    # is imported.
    deployment: Literal["desktop", "cloud"] = "desktop"

    @property
    def environment_is_configured(self) -> bool:
        """True only when ``environment`` was actually supplied by the operator.

        Pydantic records the field names a model was *constructed* with in
        ``model_fields_set``, and pydantic-settings feeds env vars and ``.env``
        entries in as constructor values. A field left at its default is
        therefore absent from that set. This is the only way to distinguish a
        deployment that said ``JOBTRACKER_ENVIRONMENT=development`` from one
        that said nothing and inherited the identical string -- which is
        exactly the situation the deployed cloud API is in.

        Not a ``computed_field``: this is a fact about how the settings were
        loaded, not a setting, and it has no business in a serialised dump.
        """

        return "environment" in self.model_fields_set

    # -------------------------------------------------------------------------
    # Database
    # -------------------------------------------------------------------------
    database_dir: str = Field(
        default="~/Library/Application Support/JobTracker",
        description="Directory for SQLite database and related files",
    )
    database_name: str = "jobtracker.db"
    database_echo: bool = Field(
        default=False,
        description="Enable verbose SQL statement logging.",
    )
    database_url_override: str | None = Field(
        default=None,
        description=(
            "Explicit async DB URL (e.g. postgresql+asyncpg://... for Supabase "
            "or sqlite+aiosqlite:///path.db). When set, this overrides the "
            "computed SQLite URL used by the application engine. Leave unset "
            "on desktop builds to keep the local SQLite database."
        ),
    )
    database_pool_size: int = Field(
        default=0,
        ge=0,
        description=(
            "Postgres only: connections each process keeps open for reuse. "
            "0 (the default) preserves NullPool — a fresh connection per "
            "request, the pooler owning all lifecycle — which costs ~216 ms "
            "of TCP+TLS+auth on every DB-touching request (issue #203). "
            "Setting this >0 is a deliberate opt-in to client-side reuse "
            "through the transaction-mode pooler: identity GUCs are already "
            "transaction-local so no user's claims can outlive their "
            "transaction on a reused connection, and pre-ping/recycle guard "
            "against pooler-killed idle connections. Keep it small (1-2): "
            "every warm serverless instance multiplies it against the shared "
            "free-tier pooler's client limit."
        ),
    )

    @computed_field  # type: ignore[misc]
    @property
    def database_path(self) -> Path:
        """Full path to the SQLite database file."""
        return Path(self.database_dir).expanduser() / self.database_name

    @computed_field  # type: ignore[misc]
    @property
    def database_url(self) -> str:
        """SQLAlchemy async database URL.

        Resolution order:
        1. ``database_url_override`` - explicit opt-in for Postgres/Supabase.
        2. Test environment -> isolated in-memory SQLite.
        3. Desktop default -> on-disk SQLite at ``database_path``.
        """

        if self.database_url_override:
            return self.database_url_override

        # During tests we want a completely isolated, in-memory database that
        # does not touch the real on-disk JobTracker DB.
        if self.environment == "test":
            return "sqlite+aiosqlite:///:memory:"

        return f"sqlite+aiosqlite:///{self.database_path}"

    # -------------------------------------------------------------------------
    # Logging
    # -------------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_dir: str = Field(
        default="~/Library/Logs/JobTracker",
        description="Directory for log files",
    )
    uvicorn_access_log: bool = Field(
        default=False,
        description="Enable Uvicorn per-request access logging.",
    )
    log_format: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    log_date_format: str = "%Y-%m-%d %H:%M:%S"

    @computed_field  # type: ignore[misc]
    @property
    def log_path(self) -> Path:
        """Full path to the log directory."""
        return Path(self.log_dir).expanduser()

    # -------------------------------------------------------------------------
    # Email Sync
    # -------------------------------------------------------------------------
    sync_batch_size: int = Field(
        default=100,
        description="Number of emails to fetch per batch",
    )

    # -------------------------------------------------------------------------
    # Gmail API
    # -------------------------------------------------------------------------
    gmail_scopes: list[str] = Field(
        default=["https://www.googleapis.com/auth/gmail.readonly"],
        description="OAuth2 scopes for Gmail API access",
    )

    # -------------------------------------------------------------------------
    # ML Classifier
    # -------------------------------------------------------------------------
    ml_model_delivery_strategy: Literal[
        "download_on_first_launch", "bundle_in_app"
    ] = Field(
        default="download_on_first_launch",
        description=(
            "How ML models are delivered for desktop builds. "
            "'download_on_first_launch' keeps app size smaller and downloads models "
            "the first time classification is used."
        ),
    )
    training_allowed_user_ids: Annotated[list[uuid.UUID], NoDecode] = Field(
        default_factory=list,
        description=(
            "The ONLY user ids whose `training_data` rows SetFit may train on "
            "(JOBTRACKER_TRAINING_ALLOWED_USER_IDS='<uuid>,<uuid>'). Empty — "
            "the default, and what the hosted app sets — means training is "
            "refused for every user, which is the intended production state: "
            "no deployed path retrains, and a misconfiguration must fail "
            "closed. Single-user is not owner-only; the corpus read is already "
            "pinned to one user_id, but without this list any user_id could be "
            "the one. A corpus every row of which is synthetic (see "
            "`classifier.setfit_model.SYNTHETIC_TRAINING_SOURCES`) trains "
            "without being listed, so fixtures and the local dev loop still "
            "work. Enforced in `classifier.setfit_model`, not here."
        ),
    )
    lite_mode: bool = Field(
        default=False,
        description="Disable SetFit for 8GB RAM machines (rules + embeddings only)",
    )

    # -------------------------------------------------------------------------
    # Keychain
    # -------------------------------------------------------------------------
    keychain_service: str = "jobtracker"

    # -------------------------------------------------------------------------
    # Cloud (Vercel + Supabase). Only consumed when deployment == "cloud".
    # -------------------------------------------------------------------------
    cors_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Extra hostnames permitted by CORS in cloud mode. Comma-separated "
            "in the env var, for example "
            "JOBTRACKER_CORS_ALLOWED_HOSTS='jobtracker.app,app.jobtracker.dev'. "
            "Vercel preview URLs (*.vercel.app) are always allowed."
        ),
    )
    supabase_jwt_secret: str | None = Field(
        default=None,
        description="Supabase JWT signing secret; required for cloud auth middleware (C3).",
    )
    supabase_jwks_url: str | None = Field(
        default=None,
        description=(
            "JWKS endpoint for Supabase's asymmetric JWT signing keys "
            "(new projects sign user tokens with ES256), e.g. "
            "https://<ref>.supabase.co/auth/v1/.well-known/jwks.json. "
            "When set, ES256 tokens verify against these keys; HS256 "
            "tokens still verify against supabase_jwt_secret."
        ),
    )
    secret_encryption_key: str | None = Field(
        default=None,
        description=(
            "Fernet key (urlsafe base64, 32 bytes) used to encrypt user credentials "
            "stored in the cloud `user_credentials` table (C4). Generate with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
        ),
    )
    vercel_cron_secret: str | None = Field(
        default=None,
        description=(
            "Shared secret Vercel Cron attaches via `x-vercel-cron-secret` header; "
            "used by `POST /cron/sync` (C7) to reject unauthenticated cron calls."
        ),
    )
    cron_sync_user_ids: Annotated[list[uuid.UUID], NoDecode] = Field(
        default_factory=list,
        description=(
            "NO LONGER CONSULTED. This was the scheduled sync's allowlist "
            "(JOBTRACKER_CRON_SYNC_USER_IDS='<uuid>,<uuid>') back when the cron "
            "could not read who had Gmail linked — `user_credentials` is "
            "FORCE-RLS on auth.uid() and a cron carries no JWT. Since #291 the "
            "membership fact lives in `gmail_sync_enrollment`, which an "
            "identity-less connection CAN read, and `jobtracker.cloud.cron` "
            "enumerates that instead. Nothing reads this field; a deployment "
            "that still sets the env var is simply ignored. Kept so an existing "
            "environment cannot fail validation on a variable it was told to "
            "set, and so this note has somewhere to live."
        ),
    )

    # -------------------------------------------------------------------------
    # Gmail Web OAuth (cloud, C5). Only consumed when deployment == "cloud".
    #
    # These configure the *web* authorization-code flow used by the deployed
    # app — distinct from the desktop `run_local_server` flow. The client
    # secret is a Google-issued credential: it is provided by the operator
    # via the Vercel env, is NEVER committed to the repo, NEVER returned to
    # the browser, and NEVER logged.
    # -------------------------------------------------------------------------
    google_oauth_client_id: str | None = Field(
        default=None,
        description=(
            "OAuth 2.0 client_id for the JobTracker *Web application* client "
            "(Google Cloud Console → Credentials). Public by design; pairs with "
            "google_oauth_client_secret. Env: JOBTRACKER_GOOGLE_OAUTH_CLIENT_ID."
        ),
    )
    google_oauth_client_secret: str | None = Field(
        default=None,
        description=(
            "OAuth 2.0 client_secret for the Web application client. Operator-"
            "supplied secret — set ONLY in the backend env (Vercel), never in the "
            "repo, chat, or the browser. Env: JOBTRACKER_GOOGLE_OAUTH_CLIENT_SECRET."
        ),
    )
    gmail_oauth_redirect_uri: str | None = Field(
        default=None,
        description=(
            "Absolute HTTPS callback URL registered as an Authorized redirect URI "
            "on the Web OAuth client, e.g. "
            "https://<api-host>/auth/gmail/callback. Google redirects the browser "
            "here after consent; must match the console entry byte-for-byte. "
            "Env: JOBTRACKER_GMAIL_OAUTH_REDIRECT_URI."
        ),
    )
    web_app_url: str | None = Field(
        default=None,
        description=(
            "FALLBACK base URL of the web app the callback bounces the user "
            "back to after a connect/disconnect. No longer the primary answer: "
            "`/auth/gmail/authorize` now takes the caller's own origin, "
            "validates it against `trusted_web_hosts` AT MINT TIME, and carries "
            "it across the round trip inside the signed `state` — so the "
            "browser returns to the origin the user actually started from, by "
            "construction. This value is only consulted when a request arrives "
            "WITHOUT that origin: a state minted by an older deploy, or a "
            "callback whose state was forged/expired and therefore carries no "
            "trustworthy destination at all. When it is used it is still held "
            "to the same rule — it must be a host this deployment serves the "
            "app on (see `trusted_web_hosts`), because a merely-reachable alias "
            "of the same project carries none of the user's cookies and "
            "returning the browser there lands it signed out. This description "
            "used to carry a concrete example, and the example was the "
            "pre-rename alias that caused exactly that — so it names no host "
            "now, deliberately. Env: JOBTRACKER_WEB_APP_URL."
        ),
    )
    gmail_oauth_state_ttl_seconds: int = Field(
        default=600,
        description=(
            "Lifetime of the signed OAuth `state` token that binds the Google "
            "callback to the authenticated user. Short by design (10 min) to "
            "limit the CSRF/replay window."
        ),
    )
    gmail_inbox_cache_ttl_seconds: int = Field(
        default=60,
        description=(
            "Short TTL (seconds) for the per-user in-process cache of the "
            "`GET /gmail/inbox` classify result. A repeat load within the "
            "window is served from memory instead of re-hitting Gmail and "
            "re-running the classifier, which is the expensive part of that "
            "endpoint. Scoped strictly per user_id — never shared across "
            "users — so caching never weakens auth or leaks mail. Set to 0 to "
            "disable (always fetch + classify fresh)."
        ),
    )
    gmail_fetch_hard_cap: int = Field(
        default=2000,
        description=(
            "Absolute upper bound on the TOTAL number of messages a single "
            "high-volume inbox mine may request across all pages. The web UI "
            "offers 100/200/500/1000/2000; this caps whatever a caller asks "
            "for so a hand-crafted request cannot ask Gmail for an unbounded "
            "scan. Env: JOBTRACKER_GMAIL_FETCH_HARD_CAP."
        ),
    )
    gmail_fetch_page_size: int = Field(
        default=99,
        description=(
            "Messages fetched + classified in ONE serverless invocation of "
            "`GET /gmail/inbox`. The endpoint is server-paginated: it returns "
            "at most this many verdicts plus a `next_page_token`, and the web "
            "client loops until it reaches the user's chosen count. "
            "WAS 500 — the Gmail `messages.list` page ceiling — until "
            "2026-09-04, when that stopped being achievable. The binding "
            "constraint is quota, not the list API: a page costs "
            "`20 * messages + 5` units against 6,000 per minute per user, so "
            "300 messages is an entire minute's budget and a 500-message page "
            "(10,005 units) cannot complete against a full bucket no matter "
            "how often it is retried. "
            "99 IS NOT A ROUND NUMBER AND THAT IS THE POINT. Throughput is "
            "`N * floor(6000 / (20N + 5))`, and the `+5` for the page's one "
            "`messages.list` call puts the round numbers on the wrong side of "
            "a boundary: 100 costs 2,005 so only TWO pages fit a minute "
            "(200 msg/min), while 99 costs 1,985 so THREE fit (297 msg/min) — "
            "a 48% difference from changing the number by one. 150 is worse "
            "still at 150 msg/min. 99 is within 0.4% of the best value the "
            "handler's clamp allows (149, at 298), and is preferred over it "
            "for finer progress and because one page then costs under a third "
            "of a minute rather than half. Pinned by "
            "tests/test_the_page_size_fits_gmails_minute.py. "
            "Clamped to [1, 250] in the handler so this env var cannot re-arm "
            "an impossible page. Env: JOBTRACKER_GMAIL_FETCH_PAGE_SIZE."
        ),
    )
    gmail_batch_size: int = Field(
        default=100,
        description=(
            "Sub-requests per Gmail batch HTTP request when fetching message "
            "bodies (Subject/From/Date + snippet + body text). Gmail caps a "
            "batch at 100 and recommends no more than 50. Further clamped by "
            "`_FULL_BATCH_SIZE` (25) in `cloud/gmail_client.py`, which is the "
            "value that actually applies. "
            "NOTE, corrected 2026-09-04: this text used to say a 100-message "
            "batch costs ~500 units and that the pace below respects a "
            "per-user ~250 units/sec quota. BOTH numbers were stale. "
            "`messages.get` costs 20 units (changed 2026-05-01), so 100 of "
            "them cost 2,000; and the limit is 6,000 units per MINUTE per "
            "user, with no per-second limit published at all. Batching does "
            "not help: Gmail counts n batched sub-requests as n requests. "
            "Env: JOBTRACKER_GMAIL_BATCH_SIZE."
        ),
    )
    gmail_batch_pause_seconds: float = Field(
        default=0.4,
        description=(
            "Seconds to sleep between successive Gmail batches. "
            "ITS ORIGINAL JUSTIFICATION WAS WRONG and is recorded here rather "
            "than quietly replaced: it said this paced against a per-user "
            "~250 units/sec limit with a 100-message batch costing ~500 units. "
            "Gmail publishes no per-second limit; the limit is 6,000 units per "
            "minute per user, and a 25-message batch of `messages.get` costs "
            "500 units on its own. Pacing against a per-second figure cannot "
            "bound a per-minute bucket, which is how a live scan came to spend "
            "two thirds of a minute's quota in one invocation and take a 403. "
            "This pause still earns its keep — it spreads a burst rather than "
            "delivering it instantaneously — but the real bound is the page "
            "size and the caller's retry, not this number. Set to 0 to "
            "disable. Env: JOBTRACKER_GMAIL_BATCH_PAUSE_SECONDS."
        ),
    )
    gmail_followup_stale_days: int = Field(
        default=21,
        description=(
            "Age (days) after which an `applied` email with no later "
            "interview/assessment/offer/rejection from the same company is "
            "flagged 'No response — consider following up'. The ghosting "
            "differentiator. Env: JOBTRACKER_GMAIL_FOLLOWUP_STALE_DAYS."
        ),
    )
    gmail_connection_cap: int = Field(
        default=25,
        description=(
            "How many DISTINCT users may hold a connected Gmail mailbox on this "
            "deployment. Not a rate limit and not a quota that refills — it "
            "rations a resource that cannot be bought back. The Google Cloud "
            "project this app publishes under has a lifetime ceiling of 100 "
            "users for its restricted `gmail.readonly` scope, and Google's own "
            "wording is that the number 'cannot be reset or changed': a slot is "
            "spent the moment a person REACHES the consent screen, and no "
            "disconnect, deletion or refund gives it back. So the enforcement "
            "point is `/auth/gmail/authorize` (see "
            "`cloud.gmail_oauth._enforce_connection_cap`), which runs before a "
            "consent URL exists; a check at the callback would run after the "
            "slot was already gone. "
            "The default is 25, deliberately far below 100, because this "
            "deployment can only count what it RECORDS — a user who opens the "
            "consent screen and walks away spends a Google slot and leaves no "
            "row anywhere, and a user who disconnects frees a row here that "
            "Google does not give back. The headroom absorbs both. "
            "There is no 'off' value and no unlimited sentinel: raise it to the "
            "number you actually mean, on purpose, one edit at a time. Zero or "
            "negative means no NEW mailbox may connect (already-connected users "
            "are still let through — reconnecting spends no Google slot). "
            "Env: JOBTRACKER_GMAIL_CONNECTION_CAP."
        ),
    )

    # Every setting the Gmail web OAuth flow needs before it can offer a
    # connect button. Each maps to an env var ``JOBTRACKER_<UPPER>``.
    # ``secret_encryption_key`` is required twice over: to encrypt the stored
    # refresh token (C4) and to sign the OAuth ``state`` token — so it belongs
    # here even though it is not a "Google" value.
    #
    # ``web_app_url`` USED TO BE IN THIS TUPLE and deliberately is not any more
    # (#333). The flow's return destination is now the caller's own origin,
    # validated against ``trusted_web_hosts`` when ``/auth/gmail/authorize``
    # mints the state and carried across the round trip inside it, so a
    # deployment with the variable unset is fully able to offer a connect
    # button. Leaving it here would have made this list state a requirement the
    # code no longer has — a 503 saying "set JOBTRACKER_WEB_APP_URL" on a
    # deployment that does not need it is exactly the kind of untrue check this
    # change exists to remove. It remains a *fallback* (see the field), and the
    # one place that reads it still refuses an untrusted value.
    _GMAIL_OAUTH_REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "google_oauth_client_id",
        "google_oauth_client_secret",
        "gmail_oauth_redirect_uri",
        "secret_encryption_key",
    )

    @property
    def gmail_oauth_missing_fields(self) -> list[str]:
        """Names (never values) of the required Gmail-OAuth settings still unset.

        Turns the opaque ``gmail_oauth_configured is False`` into an actionable
        list: an operator can map each name to its ``JOBTRACKER_<UPPER>`` env
        var and see exactly what to set. Only field *names* are exposed here —
        secret values are never read out — so this is safe to log or surface in
        a 503 detail. An empty list means the flow is fully configured.
        """

        return [
            name
            for name in self._GMAIL_OAUTH_REQUIRED_FIELDS
            if not getattr(self, name)
        ]

    @computed_field  # type: ignore[misc]
    @property
    def gmail_oauth_configured(self) -> bool:
        """True when every value the Gmail web OAuth flow needs is present.

        Routers use this to return an honest ``503 not configured`` (instead
        of a 500) while the operator has not yet set the Google client
        credentials + redirect URIs + encryption key in the backend env.
        Backed by :attr:`gmail_oauth_missing_fields` so the "is it configured?"
        boolean and the "what's missing?" diagnostic can never drift apart.
        """

        return not self.gmail_oauth_missing_fields

    @field_validator("cors_allowed_hosts", mode="before")
    @classmethod
    def _split_cors_hosts(cls, value: Any) -> Any:
        """Accept a comma-separated env var string for cors_allowed_hosts.

        Vercel and most shells can only pass strings, so
        `JOBTRACKER_CORS_ALLOWED_HOSTS='jobtracker.app,app.jobtracker.dev'`
        should Just Work. A list/tuple is still accepted for programmatic use.
        """

        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cron_sync_user_ids", mode="before")
    @classmethod
    def _parse_cron_sync_user_ids(cls, value: Any) -> Any:
        """Split the comma-separated env var into real ``uuid.UUID`` objects.

        THE PARSE IS THE POINT, AND IT MUST BE LOUD.
        ``database.connection._apply_transaction_gucs`` binds the RLS identity
        only when the ContextVar holds a ``uuid.UUID``; for a ``str`` it takes
        the ``isinstance`` early return and sets **no** ``request.jwt.claims``
        at all. A string that slipped through here would therefore not raise —
        it would make every query in that user's sync run with ``auth.uid()``
        NULL, which RLS answers with zero rows and no error. "Syncs nobody,
        silently" is precisely the failure this setting exists to end, so a
        malformed entry has to stop the process rather than degrade into it.
        (That ``isinstance`` guard is also what makes the listener's f-string
        interpolation of the claims JSON safe: a UUID's string form is
        strictly ``[0-9a-fA-F-]``. Feeding it raw strings would remove both
        properties at once.)

        The failing **index** is named, never the offending value: an operator
        can paste anything into a Vercel env box and this message reaches logs.
        That property is why the error is a :class:`CronSyncUserIdsError` and
        not a ``ValueError`` — see that class for the measurement. Do not
        "simplify" it back to ``ValueError``; doing so silently reintroduces
        pydantic's ``input_value=<whole env string>`` echo.
        """

        if value is None or value == "":
            return []
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
        else:
            items = list(value)

        parsed: list[uuid.UUID] = []
        for index, item in enumerate(items):
            if isinstance(item, uuid.UUID):
                parsed.append(item)
                continue
            try:
                parsed.append(uuid.UUID(str(item)))
            except (ValueError, AttributeError, TypeError) as exc:
                raise CronSyncUserIdsError(
                    f"JOBTRACKER_CRON_SYNC_USER_IDS entry #{index + 1} of "
                    f"{len(items)} is not a valid UUID ({type(exc).__name__}). "
                    "The value is withheld from this message because it "
                    "reaches the logs. Expected a comma-separated list of "
                    "user UUIDs."
                # ``from None``, not ``from exc``: ``uuid.UUID`` does not
                # always keep quiet about its input. A near-miss UUID raises
                # ``invalid literal for int() with base 16: '<the entry, minus
                # its dashes>'``, so the chained traceback would quote most of
                # the value straight back out. Measured, not assumed.
                ) from None
        return parsed

    @field_validator("training_allowed_user_ids", mode="before")
    @classmethod
    def _parse_training_allowed_user_ids(cls, value: Any) -> Any:
        """Split the comma-separated env var into real ``uuid.UUID`` objects.

        The parse must be loud for the same reason the cron one is, with the
        polarity that matters here spelled out: the gate this feeds compares
        ``user_id in settings.training_allowed_user_ids``, and a ``str`` that
        slipped through would equal no ``uuid.UUID``. The list would silently
        become empty-in-effect and every training run would be refused — the
        safe direction, but indistinguishable from an unset variable, so an
        operator would be told "not allowlisted" for a user they had in fact
        listed. Stop at load instead.

        The failing **index** is named, never the offending value: an operator
        can paste anything into a Vercel env box and this message reaches
        logs. That is why this raises :class:`TrainingAllowedUserIdsError` and
        not ``ValueError`` — see that class, and ``CronSyncUserIdsError`` for
        the measurement behind it.
        """

        if value is None or value == "":
            return []
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
        else:
            items = list(value)

        parsed: list[uuid.UUID] = []
        for index, item in enumerate(items):
            if isinstance(item, uuid.UUID):
                parsed.append(item)
                continue
            try:
                parsed.append(uuid.UUID(str(item)))
            except (ValueError, AttributeError, TypeError) as exc:
                raise TrainingAllowedUserIdsError(
                    f"JOBTRACKER_TRAINING_ALLOWED_USER_IDS entry #{index + 1} "
                    f"of {len(items)} is not a valid UUID "
                    f"({type(exc).__name__}). The value is withheld from this "
                    "message because it reaches the logs. Expected a "
                    "comma-separated list of user UUIDs."
                # ``from None`` for the reason given on the cron validator:
                # a near-miss UUID's own message quotes most of the value.
                ) from None
        return parsed

    def ensure_directories(self) -> None:
        """Create required directories if they don't exist."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """
    Get cached application settings.

    Settings are loaded once and cached for performance.
    Use this function to access settings throughout the app.

    Returns:
        Settings: Application settings instance.
    """
    return Settings()


# Convenience alias for importing
settings = get_settings()


def trusted_web_hosts() -> list[str]:
    """Every hostname this deployment considers to be "the web app".

    ONE LIST, TWO READERS, AND WHY THAT MATTERS
    -------------------------------------------
    This deployment already had an answer to "which host is the front end?" —
    the CORS allowlist in ``main_cloud._build_cors_origin_regex``, derived from
    the hostnames Vercel injects. It also had a SECOND, unrelated answer: the
    hand-set ``JOBTRACKER_WEB_APP_URL``, which the Gmail OAuth callback bounces
    the browser to. Nothing compared them, so they were free to disagree — and
    on 2026-08-14 they did, measured against production with no credentials:

        $ curl -sD - "https://jobtracker-api-seven.vercel.app\
/auth/gmail/callback?state=bogus&code=bogus"
        HTTP/2 302
        location: https://jobtracker-web-five.vercel.app/settings?gmail=error

    ``jobtracker-web-five.vercel.app`` is a pre-rename alias of the web
    project. It still serves the app, so nothing looked broken — but **cookies
    are scoped to a host**, and the user's Supabase session lives on
    ``getapplied.vercel.app``. Landing on the other name is landing signed out:

        $ curl -sD - "https://jobtracker-web-five.vercel.app\
/settings?gmail=connected"
        HTTP/2 307
        location: /login?gmail=connected&redirect=%2Fsettings

    So finishing a Gmail reconnect dumped the owner on ``/login`` with his
    session perfectly intact on the host he had come from. Nothing was wrong
    with sign-in; he was simply on the wrong hostname.

    Returning the hosts from ONE function is the fix for the class rather than
    the instance. ``main_cloud`` builds its regex from this list, and
    ``cloud.gmail_oauth._web_redirect`` refuses to bounce anywhere outside it,
    so the two answers can no longer drift apart in silence.

    THE SPLIT DEPLOYMENT, AND WHY THE WEB HOST MUST BE DECLARED
    -----------------------------------------------------------
    This is the part that is easy to get wrong, and the first draft of this
    change did. ``VERCEL_URL`` and ``VERCEL_PROJECT_PRODUCTION_URL`` are
    injected PER PROJECT, and this code runs on the **API** project — so what
    they name is ``jobtracker-api-…``, never the web app. The web host reaches
    this list through ``JOBTRACKER_CORS_ALLOWED_HOSTS`` or not at all.

    And on 2026-08-14 it did not. Probed live, with ``localhost`` as the
    control that proves the probe discriminates rather than just being silent:

        $ curl -sD - -H "Origin: http://localhost:3000" \\
              https://jobtracker-api-seven.vercel.app/health
        access-control-allow-origin: http://localhost:3000     <- echoed

        $ curl -sD - -H "Origin: https://getapplied.vercel.app" \\
              https://jobtracker-api-seven.vercel.app/health
        (no access-control-allow-origin at all)                <- NOT in the list

    Nothing had noticed, because the browser never calls this API: every
    backend call is made server-side from the web app's route handlers with
    the user's JWT attached, so CORS has never had to admit the web origin.
    See the note in ``apps/web/next.config.ts`` for the same finding from the
    other side.

    So the operator MUST declare the web host in ``JOBTRACKER_CORS_ALLOWED_HOSTS``
    for the Gmail return-host guard to pass. That is a real requirement, not an
    incidental one, and the refusal messages name it.

    THE SECOND VARIABLE IS GONE (#333). What stood here said the durable fix
    was for ``/auth/gmail/authorize`` to carry the CALLER'S OWN origin across
    the round trip inside the signed ``state`` this flow already has, and that
    it was deliberately not in that commit. It is in the tree now:
    ``cloud.gmail_oauth._validated_return_origin`` checks the caller's origin
    against this list **when the state is minted** — before Google is ever
    reached — and ``_verify_state`` hands the callback the origin the user
    actually started from. ``JOBTRACKER_WEB_APP_URL`` is a fallback for states
    minted before that shipped, not the answer.

    Validating at MINT and not at CONSUME is the whole design. An origin that
    round-tripped through ``state`` without being checked first would be an
    open redirect signed by us, which is strictly worse than the
    two-variables-must-agree bug it replaces. This list is therefore still
    load-bearing, and declaring the web host here is still required — what
    changed is that it is now the ONLY thing that has to be right.

    WHAT IS AND IS NOT IN HERE. ``VERCEL_URL`` is this deployment's own host
    (unique per preview); ``VERCEL_PROJECT_PRODUCTION_URL`` is the stable
    production one; both arrive WITHOUT a scheme. ``cors_allowed_hosts`` is the
    operator declaration described above. ``localhost`` and ``127.0.0.1`` are
    here for ``vercel dev`` and are matched with an optional port by both
    readers. Deliberately NO ``*.vercel.app`` wildcard — anyone can deploy one,
    and re-adding it re-opens SECURITY_AUDIT.md finding 2. See
    ``backend/tests/test_cors_origin_regex.py``.

    THIS LIST CONTAINS THE API'S OWN HOSTNAMES, which is correct for CORS and
    wrong for a return destination: pointed at the API, a return host would
    pass "is it ours?" and strand the browser on a backend that serves no
    ``/settings`` — the same broken outcome as the unset case in a different
    costume. "Who may call this API" and "where does the browser go
    afterwards" are genuinely different questions on a split deployment, and
    this function answers only the first. :func:`return_origin_is_this_api`
    answers the second by subtracting this deployment's own origins from it;
    ``/auth/gmail/authorize`` applies both.

    Read from ``os.environ`` at CALL time, not import time, so a test that
    monkeypatches the environment sees the change without reloading modules.
    """

    hosts = ["localhost", "127.0.0.1"]
    for var_name in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
        own_host = os.environ.get(var_name, "").strip()
        if own_host:
            hosts.append(own_host)
    hosts.extend(host for host in settings.cors_allowed_hosts if host)
    return hosts


def configured_web_app_host() -> str | None:
    """The hostname of ``JOBTRACKER_WEB_APP_URL``, or ``None`` when unset.

    Parsed with ``urlsplit().hostname`` rather than a substring test: that is
    what makes ``https://getapplied.vercel.app.evil.com`` a different host from
    ``getapplied.vercel.app`` instead of a match, and it is the difference
    between a check and the appearance of one.
    """

    configured = (settings.web_app_url or "").strip()
    if not configured:
        return None
    return urllib.parse.urlsplit(configured).hostname or None


def web_app_host_is_trusted() -> bool:
    """Is the configured return host one this deployment serves the app on?

    The single predicate behind both readers — ``/health`` reports it, and
    ``cloud.gmail_oauth._web_app_base`` refuses on it. Two call sites, one
    answer, which is the whole point: this bug existed because the same
    question had two implementations that were free to disagree.
    """

    host = configured_web_app_host()
    if host is None:
        return False
    return host.lower() in {trusted.lower() for trusted in trusted_web_hosts()}


# =============================================================================
# The caller's own origin as a return destination (#333)
# =============================================================================
#
# ``/auth/gmail/authorize`` is called SERVER-SIDE by the web app's route
# handler with the user's JWT attached, so there is no browser ``Origin``
# header to read — the web app states its own origin explicitly and this
# module decides whether to believe it. The decision happens once, when the
# state is minted; the callback consumes an origin this code already approved.

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_of(url: str) -> str | None:
    """Reduce a URL to ``scheme://host[:port]``, or ``None`` if it is not one.

    Lowercased, with a redundant default port dropped so ``https://x`` and
    ``https://x:443`` are the same string rather than two. IPv6 literals are
    refused rather than reconstructed: ``urlsplit().hostname`` strips the
    brackets, so rebuilding one would emit a URL that no longer parses, and
    nothing this deployment serves is reached by address anyway.
    """

    try:
        parts = urllib.parse.urlsplit((url or "").strip())
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        # Malformed port, malformed IPv6 literal — ``urlsplit`` defers both to
        # attribute access, so they land here rather than at the split.
        return None
    if not scheme or not host or ":" in host:
        return None
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


def canonical_return_origin(raw: str) -> str | None:
    """Parse a caller-supplied return origin, or ``None`` if it is not usable.

    WHAT COMES BACK IS REBUILT, NEVER THE CALLER'S BYTES. This is the property
    that matters, and it is the one a "does it look right?" check would miss.
    Validating the *submitted string* and then storing that same string leaves
    a parser differential: Python's ``urlsplit`` and a browser's URL parser do
    not agree about backslashes, userinfo or stray whitespace, so
    ``https://getapplied.vercel.app@evil.com`` can be approved by one reading
    and followed by the other. Everything returned from here is assembled from
    parsed components, so the string that reaches the signed state is one this
    code constructed — the whole smuggling class disappears rather than being
    enumerated.

    Refused outright, before any allowlist is consulted:

    - anything that is not ``http``/``https`` (no ``javascript:``, no ``data:``)
    - ``http`` for a remote host — a return that downgrades the scheme sends
      the browser somewhere its Secure session cookie will not follow. Local
      development is the exception, and is spelled out as one.
    - credentials in the authority (``user:pass@host``), the classic way a
      trusted hostname is made to *appear* first in a URL
    - any path, query or fragment beyond a bare ``/`` — an origin is a scheme,
      a host and a port; a caller sending more is not sending an origin, and
      the redirect this feeds appends its own path.
    """

    value = (raw or "").strip()
    if not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        has_credentials = parts.username is not None or parts.password is not None
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if has_credentials:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if scheme not in ("http", "https"):
        return None
    if scheme == "http" and host not in ("localhost", "127.0.0.1"):
        return None
    return _origin_of(value)


def return_origin_is_trusted(origin: str) -> bool:
    """Is ``origin``'s hostname one this deployment serves the web app on?

    The trust decision, and the only one — read :func:`trusted_web_hosts` for
    where that list comes from and why it is the same list CORS is built from.
    Compared on the parsed hostname, port-insensitively, because
    ``trusted_web_hosts`` holds hostnames and ``vercel dev`` picks its own port.
    """

    host = (urllib.parse.urlsplit(origin).hostname or "").lower()
    if not host:
        return False
    return host in {trusted.lower() for trusted in trusted_web_hosts()}


def api_own_hosts() -> set[str]:
    """Hostnames that are THIS API, at any port.

    ``VERCEL_URL`` (this deployment, unique per preview) and
    ``VERCEL_PROJECT_PRODUCTION_URL`` (the stable production one) are injected
    per PROJECT, and this code runs on the API project — so what they name is
    never the web app. They are in ``trusted_web_hosts`` because CORS wants
    them there; they must not be a place the browser is sent.
    """

    hosts: set[str] = set()
    for var_name in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
        host = os.environ.get(var_name, "").strip().lower()
        if host:
            hosts.add(host)
    return hosts


def api_own_origins() -> set[str]:
    """Origins that are THIS API, port included.

    Derived from ``gmail_oauth_redirect_uri``, which names the API's own
    callback URL by definition and so is the one self-identifying fact that
    survives off Vercel. Matched at ORIGIN granularity rather than hostname
    granularity on purpose: a local split runs the web app on
    ``http://localhost:3000`` and this API on ``http://localhost:8000``, which
    differ only by port. Subtracting the bare hostname would make local
    development — one of the outcomes #333 exists to deliver — impossible.
    """

    origin = _origin_of(settings.gmail_oauth_redirect_uri or "")
    return {origin} if origin else set()


def return_origin_is_this_api(origin: str) -> bool:
    """Would returning the browser to ``origin`` strand it on the backend?

    The case ``trusted_web_hosts`` cannot catch and says so: its list contains
    this API's own hostnames, so "is it ours?" answers yes for a destination
    that serves no ``/settings``. Same broken outcome as an unset return host,
    in a different costume — which is why it is checked separately rather than
    trusted to fall out of the allowlist.
    """

    host = (urllib.parse.urlsplit(origin).hostname or "").lower()
    return bool(host) and (host in api_own_hosts() or origin in api_own_origins())
