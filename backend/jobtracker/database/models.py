"""
Database Models
===============

SQLModel table definitions for the JobTracker database.

Tables:
-------
- Application: Job applications (company, position, status)
- Email: Synced emails with classification
- Contact: Recruiters and hiring managers
- Interview: Scheduled interviews
- TrainingData: User corrections for ML training
- EmailEmbedding: Stored embeddings for similarity matching
- SyncState: Email account sync status

All models use SQLModel which combines SQLAlchemy ORM with
Pydantic validation. This enables type-safe database operations
and automatic API serialization.

Usage:
------
    from jobtracker.database.models import Application, Email

    app = Application(company="Acme Corp", position="Software Engineer")
    email = Email(subject="Your application", classified_as="applied")
"""

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, Relationship, SQLModel

from jobtracker.limits import MAX_COMPANY_LEN

# =============================================================================
# Multi-tenancy sentinel
# =============================================================================
#
# Every entity table carries a ``user_id`` FK to ``auth.users(id)`` once the
# cloud deployment is live. Desktop (single-user SQLite) and pytest (in-memory
# SQLite) have no Supabase auth, so inserts would have no real UUID to put in
# the column. Rather than make the column nullable at the Python level (which
# would force every cloud query to handle None), we use a fixed sentinel UUID
# for local/test contexts. The cloud middleware (``auth.supabase_jwt``)
# overrides this with the JWT's ``sub`` claim per request.
#
# The sentinel is also what Alembic backfills existing rows with before the
# ``NOT NULL`` step, so local databases migrated from pre-C3 schemas keep
# working without user intervention.
LOCAL_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _user_id_field(*, index_name: str | None = None) -> "Field":
    """Factory for the ``user_id`` column shared by every entity table.

    Uses SQLAlchemy 2.0's ``sa.Uuid`` type which renders as native ``UUID``
    on Postgres and ``CHAR(32)`` on SQLite — the single declaration works
    across desktop (SQLite), tests (SQLite in-memory), and cloud (Postgres).
    """

    return Field(
        default=LOCAL_USER_ID,
        sa_column=Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            index=True,
        ),
        description="Supabase auth.users(id) owner of this row.",
    )


# =============================================================================
# Enums
# =============================================================================


class ApplicationStatus(str, Enum):
    """Possible statuses for a job application.

    THE canonical stage vocabulary. Everything that needs the list — the API
    body models, ``GET /applications/statuses``, the rollup's rank tables, the
    web's ``<select>`` — derives from here rather than restating it, because
    three hand-written copies is exactly how the board came to offer a stage
    (``assessment``) that the API answered with a 422. The word is settable now;
    the lesson is not about the word but about the copies.

    ``assessment`` IS a member, as of 2026-08-12: see :data:`CATEGORY_TO_STATUS`
    for the decision and why it changed.

    DECLARATION ORDER IS THE API'S ORDER. ``APPLICATION_STATUSES``, the
    endpoint's list and the web's mirror all take their order from here, so a
    member is inserted at its lifecycle position, never appended.

    The member NAMES are what Postgres stores — SQLModel/SQLAlchemy persist an
    enum's name, not its value — so the ``applicationstatus`` type holds
    ``'ASSESSMENT'`` while the API speaks ``'assessment'``. Adding a member
    therefore needs a migration that adds the UPPERCASE label
    (``b9e42f7c10ad``), and the SQLite suites cannot see that difference because
    ``sa.Enum`` renders as ``VARCHAR`` there.
    """

    APPLIED = "applied"
    ASSESSMENT = "assessment"
    INTERVIEWING = "interviewing"
    OFFERED = "offered"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    WITHDRAWN = "withdrawn"
    GHOSTED = "ghosted"


# The stage vocabulary as plain strings, in declaration order — DERIVED from the
# enum, so it cannot drift from it. This is what the API serves and what any
# other vocabulary (a UI select, a rank table) must be checked against.
APPLICATION_STATUSES: tuple[str, ...] = tuple(s.value for s in ApplicationStatus)

# The stage a brand-new row starts at (also ``Application.status``'s default).
DEFAULT_APPLICATION_STATUS: ApplicationStatus = ApplicationStatus.APPLIED


class EmailCategory(str, Enum):
    """Classification categories for job-related emails."""

    APPLIED = "applied"
    PENDING_APPLICATION = "pending_application"
    INTERVIEW = "interview"
    REJECTION = "rejection"
    OFFER = "offer"
    ASSESSMENT = "assessment"
    FOLLOW_UP = "follow_up"
    NEEDS_REVIEW = "needs_review"  # Uncertain - human should review
    OTHER = "other"


# What a classifier verdict means for the application it belongs to — the ONE
# statement of the category → stage mapping.
#
# ``assessment`` is the interesting one, and it is BOTH a category and a stage.
# It maps to itself.
#
# This reverses the decision recorded here until 2026-08-12, which folded it
# into ``interviewing`` on the grounds that "the product does not make that
# distinction". The product's owner does, on his own mail, and that is the
# authority that settles a vocabulary question: a real message from Roblox was
# five self-serve timed tasks with a seven-day expiry — no human, no scheduling,
# no call — and the board said "interviewing" about it. A tracker whose one
# screen names the wrong thing is wrong, however consistent it is internally.
#
# What actually changed, beyond the owner's verdict:
#
# - An EXPIRY is a different kind of time from a scheduled slot. Since
#   ``b7c31e0d94aa`` a row carries ``due_at``, so the difference between "you
#   must act before Friday" and "someone will meet you on Friday" is now a
#   fact the schema can hold; a stage that says which one this is makes the
#   deadline legible instead of decorative.
# - The rollup had ALREADY ranked it separately (``pipeline._STAGE_RANK`` has
#   ranked ``assessment`` between applied and interview since it was written).
#   The old decision cited that ranking as evidence the distinction was not
#   made, when it was in fact evidence that everything except the enum made it.
# - The cost cited against it — an ``ALTER TYPE applicationstatus ADD VALUE``
#   against live Postgres — is real but one-way and cheap; ``b9e42f7c10ad``
#   does it, forward-only, because Postgres has no ``DROP VALUE``.
#
# Deliberately UNCHANGED, so this stays a vocabulary change and not a redesign:
#
# - the classifier's category vocabulary (:class:`EmailCategory`, nine values)
#   and the corpus labelled with it — no retraining, no relabelling;
# - the terminal set (rejected/accepted/withdrawn/ghosted) — an assessment is
#   in-flight, so ``assessment`` is not terminal;
# - the monotonic rule in ``pipeline.advance_application_status`` — mail may
#   still only push a row FORWARD, so a re-test mailed to a row already at
#   ``interviewing`` leaves it at ``interviewing`` (its deadline still lands,
#   because ``due_at`` is recomputed independently of status);
# - application identity (employer + req_id-or-role) — a second requisition is
#   still a separate row that starts its own journey.
#
# Categories absent from this map (``follow_up``, ``needs_review``, ``other``)
# assert no stage at all: a follow-up is chasing an application, not a stage of
# one, and the other two are noise or a holding pen. That is what keeps the two
# vocabularies distinct now that they overlap by one member: a category is a
# claim about a MESSAGE, a status is a fact about an APPLICATION, and only the
# six categories below say anything about the second.
CATEGORY_TO_STATUS: dict[EmailCategory, ApplicationStatus] = {
    EmailCategory.APPLIED: ApplicationStatus.APPLIED,
    EmailCategory.PENDING_APPLICATION: ApplicationStatus.APPLIED,
    EmailCategory.ASSESSMENT: ApplicationStatus.ASSESSMENT,
    EmailCategory.INTERVIEW: ApplicationStatus.INTERVIEWING,
    EmailCategory.OFFER: ApplicationStatus.OFFERED,
    EmailCategory.REJECTION: ApplicationStatus.REJECTED,
}


class ClassificationMethod(str, Enum):
    """Method used to classify an email."""

    RULES = "rules"
    SIMILARITY = "similarity"
    SETFIT = "setfit"
    USER = "user"
    FALLBACK = "fallback"


class ReviewDisposition(str, Enum):
    """WHICH act a human performed on a verdict — agreement or override.

    ``user_corrected`` cannot express this and never could. It is written
    ``True`` by ``POST /applications/review/{message_id}/classify`` whatever the
    human chose, so a person who AGREES with the classifier and a person who
    OVERRULES it produce byte-identical rows. Every "the classifier was wrong N
    times" figure built on that flag is inflated by an unknown amount, and one
    audit read the flag on production, concluded the classifier had never once
    auto-detected a rejection, and reported it — while the message in question
    had been scored ``rejection`` at 0.75, correctly, and merely held under the
    0.85 gate for a human who then agreed with it.

    The two acts are different evidence and are now stored as different things.

    NULL is a state, and it is the normal one: no human decision is recorded for
    this row at all. It is NOT a synonym for any value below.

    ``UNKNOWN`` is the third state this enum exists to make honest. It means a
    human decision IS on record and which act it was is not recoverable — the
    row was written before this column existed. Revision ``b3e91c47da05``
    backfills every pre-existing ``user_corrected = true`` row to it. Nothing
    guesses: replaying today's classifier over those rows would reconstruct a
    verdict the correction had already overwritten in place, which is inventing
    the label in the other direction. A missing label is recoverable; a
    fabricated one looks like data.

    ``UNATTRIBUTED`` is a live case rather than a historical one, and is kept
    separate from ``UNKNOWN`` on purpose — folding them together would make the
    count of rows damaged by this defect unrecoverable the moment the backfill
    ran. It means the row carried no machine verdict for the human's choice to
    agree or disagree WITH: a live-scan message minted through
    ``ScannedMessageIn`` with ``category=None`` has no ``classified_as`` and no
    ``suggested_category``, so the human supplied the first verdict rather than
    ruling on one.

    This does NOT redefine ``user_corrected``, which keeps meaning "a human
    settled this row" — see the field's own comment for why narrowing it would
    silently move rows into the labeling queue and the needs-review count.
    """

    CONFIRMED = "confirmed"
    OVERRIDDEN = "overridden"
    UNATTRIBUTED = "unattributed"
    UNKNOWN = "unknown"


class EmailSource(str, Enum):
    """Source email account type."""

    GMAIL = "gmail"
    ICLOUD = "icloud"


class InterviewType(str, Enum):
    """Types of interviews."""

    PHONE = "phone"
    VIDEO = "video"
    ONSITE = "onsite"
    TECHNICAL = "technical"
    BEHAVIORAL = "behavioral"
    PANEL = "panel"


class InterviewStatus(str, Enum):
    """Interview scheduling status."""

    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"


class ContactRole(str, Enum):
    """Role of a contact person."""

    RECRUITER = "recruiter"
    HIRING_MANAGER = "hiring_manager"
    HR = "hr"
    OTHER = "other"


class SyncStatus(str, Enum):
    """Email sync status."""

    IDLE = "idle"
    SYNCING = "syncing"
    ERROR = "error"


# =============================================================================
# Base Models
# =============================================================================


class TimestampMixin(SQLModel):
    """Mixin for created_at and updated_at timestamps."""

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Record creation timestamp",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="Record last update timestamp",
        sa_column_kwargs={"onupdate": datetime.utcnow},
    )


# =============================================================================
# Application Model
# =============================================================================


class Application(TimestampMixin, table=True):
    """
    Job application record.

    Represents a single job application to a company/position.
    Links to related emails, contacts, and interviews.
    """

    __tablename__ = "applications"
    __table_args__ = (
        sa.Index("ix_applications_user_id_status", "user_id", "status"),
        # THE BOUND ON `company`, AT THE LAYER THAT OWNS IT (#738).
        #
        # #581 closed four application-code paths that could put an oversized
        # employer name here. Every one of those bounds is Python, placed at
        # the producers known then — and the defect is a FLOW property ("no
        # unbounded value reaches this column"), so a future display producer
        # reaching an existing write site bypasses all of them. The tree
        # already carries the template: `employer_named_in_body` is a display
        # producer whose confinement to display grade is enforced by docstring
        # only.
        #
        # WHAT IT PREVENTS. `ix_applications_company` is a btree; Postgres
        # refuses an entry over 2704 bytes, and inside the sync's single
        # transaction that takes the WHOLE batch — measured, `AAA + POISON +
        # ZZZ` left zero rows including the message that had already flushed,
        # and nothing commits, so every later sync re-reads the same mail and
        # re-poisons.
        #
        # THE DECISIVE PROPERTY IS NOT POSTGRES. SQLite enforces CHECK
        # constraints too, so the entire "invisible on a laptop" class that
        # produced #406 and #581 becomes loud in the first local test run —
        # for every writer past and future, whatever the syntax: `setattr`,
        # `**kwargs`, Core `update()`, raw SQL, and the producer nobody has
        # written yet. Declared on the model, so `create_all`-built test
        # schemas carry it without anyone remembering to.
        #
        # THE NUMBER IS IMPORTED, NEVER RETYPED. Two ceilings on one column is
        # the drift #581 exists to prevent, and a CHECK that disagrees with
        # `cloud/applications.py`'s request models is worse than neither: one
        # would reject what the other accepted, and which you hit would depend
        # on the write path. `length()` is character semantics on both engines,
        # which is the unit `MAX_COMPANY_LEN` is expressed in.
        sa.CheckConstraint(
            f"length(company) <= {MAX_COMPANY_LEN}",
            name="ck_applications_company_len",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Owner (Supabase auth.users.id). Defaults to the local-user sentinel on
    # desktop/tests; cloud writes set this from the validated JWT ``sub``.
    user_id: uuid.UUID = _user_id_field()

    # Core fields
    company: str = Field(index=True, description="Company name")
    position: str = Field(description="Job position/title")
    # Who put the position there — NULL for "the sync owns this field", 'user'
    # for a title a human typed.
    #
    # THE ORIGINAL JUSTIFICATION HERE WAS FALSE AND IS CORRECTED (#543). It read
    # that the Gmail path fetches ``format=metadata``, that ATS subjects never
    # name the role, and that ``position`` is therefore permanently "" on every
    # auto-filed row. All three are wrong today and the first was wrong when
    # written: the fetch is ``format="full"`` (``cloud/gmail_client.py:17``,
    # ``:521``, ``:907``), and the trailing- and lead-segment readers do resolve
    # roles from subjects (#553, #626), measured at 40 fire / 40 exact / 0 wrong
    # on the corpus. ``cloud/gmail_client.py:1121-1128`` already corrected the
    # same ``metadata`` claim in its own docstring; this site and its twin in
    # ``cloud/applications.py`` were missed.
    #
    # The column is still right, for the reason that survives the correction:
    # extraction succeeding sometimes is exactly why a user-typed title needs a
    # way to say the field is theirs now. Without it the next sync overwrites a
    # human's answer with a machine's — and it can now produce one.
    #
    # A separate column rather than the ``source`` flip
    # ``record_status_correction`` uses, because ``_is_auto_row(source)`` also
    # gates the status advance, the reopen-after-rejection evidence and the
    # employer-name restyle: moving the row off ``gmail`` to protect a job title
    # would stop a later rejection email from ever settling the card. This is
    # ``due_source``'s shape — per-field provenance — for the same reason
    # ``due_source`` has it.
    position_source: Optional[str] = Field(
        default=None,
        description="Origin of position: NULL (the sync's) or 'user' (typed)",
    )
    status: ApplicationStatus = Field(
        default=ApplicationStatus.APPLIED,
        index=True,
        description="Current application status",
    )

    # Application details
    applied_date: Optional[date] = Field(default=None, description="Date of application")
    source: Optional[str] = Field(default=None, description="Where you found the job")
    url: Optional[str] = Field(default=None, description="Job posting URL")
    notes: Optional[str] = Field(default=None, description="Personal notes")

    # Removal is RECOVERABLE, never a DELETE. Nothing automated may destroy an
    # application: the re-sync rebuild and the user's "not an application"
    # dismiss both set these instead, which hides the row from the board while
    # the row and its emails stay on disk for an undo. ``NULL`` = live.
    # ``dismissed_reason`` records WHO removed it (``user`` / ``resync``) —
    # fresh mail may resurrect an automated removal, but never a human's.
    dismissed_at: Optional[datetime] = Field(
        default=None,
        description="When the row was removed from the board (NULL = live)",
    )
    dismissed_reason: Optional[str] = Field(
        default=None,
        description="Who removed it: 'user' (explicit dismiss) or 'resync'",
    )

    # Identity WITHIN an employer. Until 2026-08-11 the identity of an
    # application was its company alone, so four different Amazon requisitions
    # applied for on one evening became one row and three real applications were
    # invisible. These two are what tell them apart on the next sync:
    # ``req_id`` is the employer's own requisition number when it prints one
    # ("(ID: 3177934)"), ``role_token`` the normalized job title. Both NULL is
    # legitimate and means the mail named no role anywhere — that employer keeps
    # exactly one row, which is the honest floor rather than a guess.
    #
    # Re-applying after a rejection does NOT produce a second row, and the
    # comment that used to sit here claiming it did was wrong in both halves.
    # ``_company_rows`` filters on owner and company token only — no status, and
    # "live" there means not-dismissed, which a rejected row still is — so a
    # fresh confirmation for the same role resolves straight onto the settled
    # row. What happens instead is REOPEN-IN-PLACE: ``roll_up_applications``
    # reads a cluster's status from the mail strictly newer than its newest
    # dated rejection, and ``upsert_applications_for_user`` lets a rejected AUTO
    # row leave the terminal state only on that evidence. One identity is one
    # row across any number of attempts — the board shows a single card whose
    # ``applied_date`` keeps the FIRST filing.
    #
    # Still deliberately NOT a unique constraint, for two reasons that survive
    # that correction. No column tuple expresses the identity the resolver
    # actually uses: it matches a normalized company TOKEN against the stored
    # display name, which the sync itself restyles ("Doordash" → "DoorDash").
    # And both columns are legitimately NULL for an employer that names no role,
    # where NULLs do not collide — so the constraint would police only the rows
    # that never needed policing.
    req_id: Optional[str] = Field(
        default=None,
        index=True,
        description="Employer's own requisition id for this application, if any",
    )
    role_token: Optional[str] = Field(
        default=None,
        description="Normalized job title, used to tell one employer's applications apart",
    )

    # When something is DUE — the assessment window, the take-home deadline, the
    # date an offer must be answered by. NULL means no deadline is known, which
    # is the honest default: a deadline is only ever recorded because a message
    # stated one or a human typed one. It is never inferred.
    due_at: Optional[datetime] = Field(
        default=None,
        index=True,
        description="When this application's next obligation is due (UTC)",
    )
    # Who put it there: 'mail' (extracted from an explicit statement) or 'user'.
    # The distinction is load-bearing — a sync may refresh a 'mail' deadline as
    # later mail supersedes it, and must never touch one a human set.
    due_source: Optional[str] = Field(
        default=None,
        description="Origin of due_at: 'mail' (extracted) or 'user' (typed)",
    )

    # Relationships
    emails: list["Email"] = Relationship(back_populates="application")
    contacts: list["Contact"] = Relationship(back_populates="application")
    interviews: list["Interview"] = Relationship(back_populates="application")


# =============================================================================
# Email Model
# =============================================================================


class Email(TimestampMixin, table=True):
    """
    Synced email record with classification.

    Stores email content and metadata, classification results,
    and linkage to applications.
    """

    __tablename__ = "emails"
    __table_args__ = (
        sa.Index("ix_emails_user_id_received_at", "user_id", "received_at"),
        # Uniqueness of a provider message id is PER OWNER, not global. Every
        # lookup in the cloud path is already scoped ``(user_id, message_id)``;
        # a global UNIQUE meant the second user to receive the same Gmail
        # message id would hit a unique violation and 500 their whole sync.
        # Same shape of de-globalization that revision ``6e64c46d32fd`` applied
        # to ``sync_state.account_email``.
        sa.Index("ix_emails_user_id_message_id", "user_id", "message_id", unique=True),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Owner (Supabase auth.users.id).
    user_id: uuid.UUID = _user_id_field()

    # Foreign key to application (optional - unlinked emails allowed)
    application_id: Optional[int] = Field(
        default=None,
        foreign_key="applications.id",
        index=True,
        description="Linked application ID",
    )

    # Email metadata
    source_account: EmailSource = Field(index=True, description="Gmail or iCloud")
    # Indexed but NOT globally unique — uniqueness is the composite
    # ``(user_id, message_id)`` index declared in ``__table_args__``.
    message_id: str = Field(index=True, description="Email Message-ID header")
    thread_id: Optional[str] = Field(default=None, description="Gmail thread ID")

    # Email content
    subject: Optional[str] = Field(default=None, description="Email subject")
    sender_name: Optional[str] = Field(default=None, description="Sender display name")
    sender_email: Optional[str] = Field(default=None, description="Sender email address")
    received_at: datetime = Field(index=True, description="Email receive timestamp")
    body_text: Optional[str] = Field(default=None, description="Plain text body")
    body_html: Optional[str] = Field(
        default=None,
        description="Raw HTML body when available for rich rendering",
    )
    body_snippet: Optional[str] = Field(
        default=None,
        max_length=500,
        description="First 500 chars for preview",
    )

    # WHICH APPLICATION THIS MESSAGE NAMES, derived once when the message was
    # read and stored so it is never derived twice from two different texts.
    #
    # The classifier is handed the message BODY; identity resolution used to be
    # handed ``body_snippet``, which is Gmail's own ~200 characters. A title
    # sitting past that was invisible to the board while the classifier read it
    # perfectly — Torc's card carried no position for that reason alone, and
    # against a production-shaped corpus the gap was 50 applications split over
    # two cards, 50 updates opening a rival card, and 81 further updates pushed
    # into the review queue on top of the 371 that belong there.
    #
    # Deriving from the body and NOT storing the result would have been worse
    # than the bug. ``STORED_SNIPPET_CHARS`` records the failure: a key computed
    # from one width of text on the queue side and another on the settle side
    # leaves a row unlinked and un-reviewed, re-queued on every sync forever.
    # Storing it is what makes both sides read the same value.
    #
    # THIS IS NOT THE BODY AND MUST NEVER BECOME IT. A job title and a
    # requisition number are bounded, and ``applications.position`` /
    # ``applications.role_token`` / ``applications.req_id`` have stored exactly
    # this class of value since the beginning. ``/privacy`` says the body is
    # read in flight and discarded; that stays true, and
    # ``tests/test_body_is_never_persisted.py`` places its sentinel immediately
    # after the capture boundary so a capture that ran long would drag it in
    # here and fail.
    #
    # NULL MEANS "NOT DERIVED ON THIS ROW YET" — a row written before this
    # column existed, or one written by the client relay, which carries a
    # snippet and no body and so has nothing better to offer than the reader can
    # compute itself. Empty string means "derived, and the message names
    # nothing", which is the common and correct case for mail like Google's
    # acknowledgement. The two are different questions and a single NULL could
    # not answer both: readers fall back to re-deriving only for NULL, and every
    # writer ratchets NULL upward and never blanks a value back down.
    identity_role: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Job title this message names, derived from the body at read time. "
            "NULL = not derived yet; '' = derived, names none."
        ),
    )
    identity_req_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description=(
            "Requisition id this message names, derived from the body at read "
            "time. NULL = not derived yet; '' = derived, names none."
        ),
    )

    # Classification
    classified_as: Optional[EmailCategory] = Field(
        default=None,
        index=True,
        description="ML classification result",
    )
    classification_confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Classification confidence (0.0-1.0)",
    )
    classification_method: Optional[str] = Field(
        default=None,
        description="Method used for classification (rules, embeddings, setfit, fallback, user_correction)",
    )
    # The classifier's PROPOSAL, which is not the same thing as ``classified_as``.
    #
    # ``classified_as`` is the COMMITTED category — the one the system may act
    # on — and ``NEEDS_REVIEW`` is its typed null: the linker refuses to act on
    # it, and ``reconcile_orphaned_classifications`` files from it as
    # human-owned. A parked queue item therefore had nowhere to record WHAT the
    # classifier thought it was, only how strongly it thought it ("needs_review
    # at 0.92"), and the verdict was destroyed at the two persist sites.
    #
    # Values here are always proposals, never assertions. NULL is the normal
    # state (nothing pending, or a row that already has a commitment) and
    # ``needs_review`` is deliberately NOT storable — this holds one of the
    # eight predicted labels or nothing. Unindexed on purpose: no reader filters
    # on it, and none should start.
    suggested_category: Optional[EmailCategory] = Field(
        default=None,
        description="Classifier's proposed category while the verdict is unconfirmed",
    )

    # User interaction
    #
    # ``user_corrected`` reads "a human SETTLED this row", not "a human changed
    # the verdict" — the name is older than the meaning and is deliberately not
    # being narrowed. Four queries filter ``user_corrected.is_(False)`` as their
    # definition of not-yet-human-settled
    # (``scripts/weekly_labeling_workflow.py`` ×2,
    # ``scripts/generate_ml_monitoring_report.py`` ×2), and none of them filters
    # ``is_reviewed``. Redefining this flag to mean overrides-only would push
    # every AGREEMENT back into the weekly labeling queue and into the
    # needs-review count — a message whose label a human already settled,
    # leading the queue. That is the same trap ``_settle_thread_siblings``
    # documents for siblings.
    #
    # WHICH act it was lives in ``review_disposition`` below.
    user_corrected: bool = Field(default=False, description="Was classification corrected?")
    # Agreement or override — the distinction ``user_corrected`` cannot express.
    #
    # NULL means no human decision is recorded for this row. Every non-null
    # value means one is; see :class:`ReviewDisposition` for what each says and
    # for why ``UNKNOWN`` exists rather than a backfilled guess.
    #
    # Unindexed on purpose, exactly like ``suggested_category``: it is read per
    # row for display and aggregated by the monitoring scripts, and no query
    # filters on it.
    review_disposition: Optional[ReviewDisposition] = Field(
        default=None,
        description="Whether the human confirmed or overrode the machine's verdict",
    )
    is_reviewed: bool = Field(default=False, description="Has user reviewed this email?")

    # Raw data
    raw_headers: Optional[str] = Field(
        default=None,
        description="JSON of email headers for debugging",
    )

    # Relationships
    application: Optional[Application] = Relationship(back_populates="emails")
    embedding: Optional["EmailEmbedding"] = Relationship(back_populates="email")


# =============================================================================
# Contact Model
# =============================================================================


class Contact(TimestampMixin, table=True):
    """
    Contact person associated with an application.

    Stores recruiters, hiring managers, and other contacts
    extracted from email signatures or manually added.
    """

    __tablename__ = "contacts"
    __table_args__ = (
        sa.Index("ix_contacts_user_id_application_id", "user_id", "application_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Owner (Supabase auth.users.id).
    user_id: uuid.UUID = _user_id_field()

    # Foreign key to application
    application_id: int = Field(
        foreign_key="applications.id",
        index=True,
        description="Associated application ID",
    )

    # Contact info
    name: Optional[str] = Field(default=None, description="Contact name")
    email: str = Field(description="Contact email address")
    role: Optional[ContactRole] = Field(default=None, description="Contact role")
    notes: Optional[str] = Field(default=None, description="Notes about contact")

    # Relationships
    application: Application = Relationship(back_populates="contacts")


# =============================================================================
# Interview Model
# =============================================================================


class Interview(TimestampMixin, table=True):
    """
    Interview record associated with an application.

    Tracks scheduled, completed, and cancelled interviews
    with type, time, and location details.
    """

    __tablename__ = "interviews"
    __table_args__ = (
        sa.Index("ix_interviews_user_id_application_id", "user_id", "application_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Owner (Supabase auth.users.id).
    user_id: uuid.UUID = _user_id_field()

    # Foreign key to application
    application_id: int = Field(
        foreign_key="applications.id",
        index=True,
        description="Associated application ID",
    )

    # Interview details
    type: Optional[InterviewType] = Field(default=None, description="Interview type")
    scheduled_at: Optional[datetime] = Field(default=None, description="Scheduled time")
    duration_minutes: Optional[int] = Field(default=None, description="Expected duration")
    location: Optional[str] = Field(default=None, description="Location or video link")
    notes: Optional[str] = Field(default=None, description="Interview notes")
    status: InterviewStatus = Field(
        default=InterviewStatus.SCHEDULED,
        description="Interview status",
    )

    # Relationships
    application: Application = Relationship(back_populates="interviews")


# =============================================================================
# Training Data Model
# =============================================================================


class TrainingData(SQLModel, table=True):
    """
    User corrections for ML model training.

    When a user corrects a misclassified email, the correction
    is stored here for SetFit retraining.
    """

    __tablename__ = "training_data"
    __table_args__ = (
        sa.Index("ix_training_data_user_id_label", "user_id", "label"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Owner (Supabase auth.users.id).
    user_id: uuid.UUID = _user_id_field()

    # Link to original email (optional)
    email_id: Optional[int] = Field(
        default=None,
        index=True,
        unique=True,
        description="Associated email ID (if from user correction)",
    )

    # Training example content
    subject: Optional[str] = Field(default=None, description="Email subject")
    body_text: Optional[str] = Field(default=None, description="Email body text")

    # Legacy field - combined text (for backwards compatibility)
    email_text: Optional[str] = Field(default=None, description="Combined email text (legacy)")

    # Label (stored as string for flexibility)
    label: str = Field(index=True, description="Correct classification label")
    source: str = Field(
        default="user_correction",
        description="Source of training data",
    )

    # Metadata
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When correction was made",
    )


# =============================================================================
# Email Embedding Model
# =============================================================================


class EmailEmbedding(SQLModel, table=True):
    """
    Stored embeddings for similarity-based classification.

    Embeddings are stored as BLOBs in SQLite for reliability
    (transactional, backed up automatically).
    """

    __tablename__ = "email_embeddings"
    __table_args__ = (
        sa.Index("ix_email_embeddings_user_id_label", "user_id", "label"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Owner (Supabase auth.users.id).
    user_id: uuid.UUID = _user_id_field()

    # Foreign key to email
    email_id: int = Field(
        foreign_key="emails.id",
        unique=True,
        index=True,
        description="Associated email ID",
    )

    # Embedding data (label stored as string for flexibility)
    label: str = Field(index=True, description="Classification label")
    embedding: Optional[bytes] = Field(default=None, description="Serialized numpy array (384 floats)")
    model_version: str = Field(
        default="e5-small-v2",
        description="Embedding model version",
    )

    # Metadata
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When embedding was created",
    )

    # Relationships
    email: Email = Relationship(back_populates="embedding")


# =============================================================================
# Sync State Model
# =============================================================================


class SyncState(SQLModel, table=True):
    """
    Email account sync state tracking.

    Stores the last sync position for incremental syncing
    of Gmail (historyId) and iCloud (IMAP UID).
    """

    __tablename__ = "sync_state"
    # ``account_email`` is unique *per user* (composite), not globally unique.
    # Two different Supabase users connecting the same iCloud account is a
    # legitimate cloud case that global-uniqueness would block. Desktop stays
    # single-user so the constraint is equivalent in practice.
    __table_args__ = (
        sa.UniqueConstraint(
            "user_id", "account_email", name="uq_sync_state_user_account"
        ),
        sa.Index(
            "ix_sync_state_user_id_account_email", "user_id", "account_email"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Owner (Supabase auth.users.id).
    user_id: uuid.UUID = _user_id_field()

    # Account info - use string column to store enum values (not names)
    account_type: str = Field(description="Gmail or iCloud")
    account_email: str = Field(description="Account email address")

    # Sync state
    last_sync_at: Optional[datetime] = Field(default=None, description="Last sync timestamp")
    gmail_history_id: Optional[str] = Field(
        default=None,
        description="Gmail historyId for incremental sync",
    )
    imap_last_uid: Optional[int] = Field(
        default=None,
        description="IMAP last UID for incremental sync",
    )

    # THE LEASE. When a sync for this (user, account) started, or NULL when
    # none is running.
    #
    # A timestamp rather than a boolean because a crashed sync must expire.
    # A serverless function killed on its 60 s ceiling leaves nobody behind to
    # clear a flag, so a boolean lease would lock the user out of their own
    # mailbox permanently; "held only if it started within the TTL" cannot.
    # Taken and tested in ONE conditional UPDATE — see
    # ``cloud/sync_state.acquire_gmail_sync_lease`` — because a read-then-write
    # in Python is exactly the race this exists to close.
    sync_started_at: Optional[datetime] = Field(
        default=None, description="When the in-flight sync started (lease); NULL if idle"
    )

    # Status - use string column to store enum values
    status: str = Field(default="idle", description="Current sync status")
    error_message: Optional[str] = Field(default=None, description="Last error message")

    # WHAT THE LAST SUCCESSFUL SYNC LOOKED AT — six counts, one partition.
    #
    # The most common question a user asks about this product is "did you see
    # my mail?", and until these columns existed the database could not answer
    # it. On 2026-08-21 four Microsoft confirmations were read by a sync and
    # produced no application row, no review-queue entry and no ``emails`` row
    # at all — ``_persist_message_refs`` writes one only for a message that
    # clustered into an application or was flagged for review. So "we never
    # fetched it", "it arrived after the cursor" and "we read it and threw it
    # away" were the same state on disk: nothing.
    #
    # WHY DURABLE AND NOT JUST ON THE RESPONSE. ``POST /gmail/sync`` already
    # returns these numbers, but a response is gone the moment the tab closes,
    # and the user reporting the bug is not the person reading the response.
    # Diagnosis happens against Postgres days later, which is exactly where the
    # numbers were missing. The response and these columns are assigned from
    # ONE ``pipeline.ScanLedger``, so they cannot disagree.
    #
    # LAST SYNC ONLY — overwritten on every success, never accumulated. A
    # history table would answer more questions and is a bigger thing than
    # #422 asks for; "what did the most recent run see" is the question that
    # was unanswerable.
    #
    # COUNTS ONLY. Nothing here names a message, a sender or a subject. What
    # was dropped is in the logs keyed by message id; storing it beside the
    # counts would mean keeping mail metadata for messages the product decided
    # NOT to file, which is the promise ``apps/web/app/(app)/privacy/page.tsx``
    # makes about the ``emails`` table.
    #
    # NULL means "no sync has recorded a ledger yet" — every row that predates
    # revision ``a3f7d21c60be``, and any account whose only syncs failed. It is
    # a different fact from 0 ("a sync ran and read nothing"), and the two must
    # not be collapsed: 0 is the answer that says the mailbox was quiet.
    #
    # ``last_scanned`` counts what the scan READ from Gmail; ``last_classified``
    # counts what reached the pipeline. They differ by whatever left before an
    # item existed — the user's own sent mail, which ``_classify_messages``
    # skips, and a repeated message id, since the ledger counts distinct ones.
    # The partition closes over the second, not the first — see
    # ``pipeline.ScanLedger``.
    last_scanned: Optional[int] = Field(
        default=None, description="Messages the last successful sync read from Gmail"
    )
    last_classified: Optional[int] = Field(
        default=None, description="Of those, how many entered the pipeline"
    )
    last_filed: Optional[int] = Field(
        default=None, description="Classified messages that landed on an application"
    )
    last_queued: Optional[int] = Field(
        default=None, description="Classified messages routed to the review queue"
    )
    last_dropped: Optional[int] = Field(
        default=None, description="Lifecycle verdicts discarded under the review floor"
    )
    last_reached_nothing: Optional[int] = Field(
        default=None, description="Classified messages that produced no row anywhere"
    )


class UserCredential(SQLModel, table=True):
    """
    Encrypted third-party credentials (cloud deployment only).

    Stores Gmail OAuth tokens and iCloud app-specific passwords as
    Fernet-encrypted blobs, scoped to the authenticated Supabase user.
    Desktop uses macOS Keychain via ``jobtracker.credentials.desktop``
    and never writes to this table.

    See ``jobtracker.credentials.cloud`` for the read/write API and
    ``jobtracker.config.secret_encryption_key`` for the encryption key.

    Composite PK (user_id, kind) means at most one row per user per
    credential type; re-issuing a Gmail OAuth token overwrites the
    existing row.
    """

    __tablename__ = "user_credentials"
    __table_args__ = (
        sa.PrimaryKeyConstraint("user_id", "kind", name="pk_user_credentials"),
        sa.CheckConstraint(
            "kind IN ('gmail_oauth', 'icloud_mail')",
            name="ck_user_credentials_kind",
        ),
        sa.Index("ix_user_credentials_kind", "kind"),
    )

    # Owner (Supabase auth.users.id). NOT using the shared
    # ``_user_id_field()`` factory here because this table's PK *is*
    # (user_id, kind) — we want the column declared with an explicit
    # SA column object that participates in the composite PK.
    user_id: uuid.UUID = Field(
        sa_column=Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
        ),
        description="Supabase auth.users(id) owner of this credential.",
    )

    # Credential type discriminator. Text, not Python enum — the
    # CHECK constraint (see __table_args__) enforces valid values and
    # keeps the column portable across SQLite/Postgres.
    kind: str = Field(
        sa_column=Column("kind", sa.Text, nullable=False),
        description="Credential kind: 'gmail_oauth' or 'icloud_mail'.",
    )

    # Fernet token: base64url(version || timestamp || iv || ciphertext || hmac).
    # Fernet embeds its own IV, so ``nonce`` is reserved for a future AEAD
    # upgrade and currently stored as an empty byte string.
    ciphertext: bytes = Field(
        sa_column=Column("ciphertext", sa.LargeBinary, nullable=False),
        description="Fernet-encrypted credential blob.",
    )
    nonce: bytes = Field(
        default=b"",
        sa_column=Column("nonce", sa.LargeBinary, nullable=False),
        description="Reserved for AEAD nonce (unused by Fernet).",
    )

    # Encryption key id — supports rotation. Active key is named 'v1'.
    key_id: str = Field(
        default="v1",
        sa_column=Column("key_id", sa.Text, nullable=False, server_default="v1"),
        description="Identifier of the encryption key used (rotation support).",
    )

    # When the third party told us this grant is gone — a user revoking Gmail
    # access at myaccount.google.com — or NULL while it is believed good.
    #
    # Written ONLY on a definitive, permanent refusal (``invalid_grant``); never
    # on a transport error, a timeout or an HTTP 5xx, which are transient and
    # would otherwise disconnect a working account the first time Google was
    # unreachable. See ``cloud/gmail_client.load_valid_credentials``.
    #
    # Marked rather than deleted so the row keeps the address the UI needs to
    # name which account to reconnect, and so reconnecting is reversible: the
    # OAuth callback's upsert clears this back to NULL.
    revoked_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        description="When the provider refused this grant permanently; NULL if live.",
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


# =============================================================================
# Gmail sync enrollment (cloud only)
# =============================================================================


class GmailSyncEnrollment(SQLModel, table=True):
    """Who has a Gmail credential — the *fact*, with none of the secret.

    WHY A SECOND TABLE INSTEAD OF READING ``user_credentials``
    ----------------------------------------------------------
    The scheduled sync (``jobtracker.cloud.cron``) carries no JWT, so
    ``auth.uid()`` is NULL for it and every policy on ``user_credentials``
    matches no row. That table is FORCE-RLS and holds Google refresh tokens, so
    the correct answer to "let the cron enumerate it" is not a wider policy, a
    ``SECURITY DEFINER`` wrapper or a ``BYPASSRLS`` role — every one of those
    puts a new path in front of the tokens, and the cron does not want the
    tokens. It wants the *set of user ids* that hold one.

    So the membership fact is published here, in a table that holds nothing
    else: a user id and when it was enrolled. No ciphertext, no email address,
    no ``kind``. The leak this whole design worries about is not guarded
    against, it is structurally impossible — there is no secret in this table
    to leak.

    THE DELIBERATE EXPOSURE, stated rather than buried. This table carries a
    ``SELECT`` policy for the runtime role with a permissive predicate (see the
    ``gmail_sync_enrollment`` revision), because a policy the cron's
    identity-less connection cannot satisfy would rebuild the original problem.
    Any reader connecting as ``jobtracker_app`` can therefore learn WHICH user
    ids have linked Gmail, and when. That is a membership fact. It is never a
    token and never an email address, and it is the trade this design makes on
    purpose.

    HOW IT STAYS TRUE. Written and deleted in the SAME transaction as the Gmail
    credential itself — ``jobtracker.credentials.cloud.save_gmail_credentials``
    and ``delete_gmail_credentials`` — so the two tables cannot drift. Nothing
    else may write it.
    """

    __tablename__ = "gmail_sync_enrollment"

    # Owner (Supabase auth.users.id) and the whole primary key: enrollment is
    # membership, so a user is in the set or is not. The Postgres-only foreign
    # key to ``auth.users(id) ON DELETE CASCADE`` is added by the migration,
    # mirroring ``user_credentials`` — SQLite has no ``auth`` schema.
    user_id: uuid.UUID = Field(
        sa_column=Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        description="Supabase auth.users(id) that has a Gmail credential stored.",
    )

    enrolled_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            "enrolled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        description="When this user first linked Gmail (not updated on reconnect).",
    )
