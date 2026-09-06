"""Pure pipeline analytics over classified cloud mail (issue C7).

The high-volume inbox mine (``GET /gmail/inbox``) is server-paginated: the
web client loops pages and accumulates one verdict per message. This module
holds the *pure, Gmail-free* analytics that run over that accumulated set:

- :func:`company_key` — collapse a sender + subject to a stable company token
  so mail from one employer groups together even when it is relayed through a
  shared ATS domain (Lever, Greenhouse, Workday, …) that fronts many
  companies.
- :func:`summarize` — counts per category for a fetched set.
- :func:`flag_follow_ups` — the ghosting differentiator: ``applied`` mail with
  no later interview/assessment/offer/rejection from the same company within
  ``stale_days`` is surfaced as "No response — consider following up."

Everything here is a pure function over plain data (:class:`PipelineItem`),
which is what lets it be unit-tested without a Gmail token and re-used by the
Phase 2 dashboard-persistence path. No network, no I/O, no side effects — with
ONE deliberate exception: :func:`collect_review_items` emits a log line for a
confident verdict it drops. That drop is the module's only outcome that leaves
no trace anywhere else (no application row, no queue row, no counter), and its
invisibility is what let a whole class of persistence bug ship unnoticed. A log
record is not a side effect the callers can observe, so purity as the tests use
it — same input, same return value — still holds.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.parse
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

# The bound itself lives in a leaf module, because `database/models.py`
# now needs the same number for a CHECK constraint and must not import
# this file to learn it (#738). Aliased rather than renamed: twenty call
# sites below spell it `_MAX_COMPANY_LEN` and the churn would bury the
# change that matters.
from jobtracker.limits import MAX_COMPANY_LEN

logger = logging.getLogger(__name__)

# The full category vocabulary the cloud rules classifier can emit. Kept in one
# place so a summary always reports every bucket (a category with zero hits is
# an explicit 0, never a missing key the UI has to guard).
CANONICAL_CATEGORIES: tuple[str, ...] = (
    "applied",
    "pending_application",
    "interview",
    "assessment",
    "offer",
    "rejection",
    "follow_up",
    "needs_review",
    "other",
)

# Categories that are part of a real job-search lifecycle (everything the
# tracker cares about) — i.e. NOT the "other" noise nor the "needs_review"
# holding pen. Phase 2 persists exactly these to the applications table.
JOB_LIFECYCLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "applied",
        "pending_application",
        "interview",
        "assessment",
        "offer",
        "rejection",
        "follow_up",
    }
)

# A later message in one of these categories counts as the company having
# "responded" to an application, so the application is NOT ghosted.
RESPONSE_CATEGORIES: frozenset[str] = frozenset(
    {"interview", "assessment", "offer", "rejection"}
)

# Domains that relay mail on behalf of MANY EMPLOYERS: applicant tracking
# systems, job boards and the generic ESPs that front them. The domain does not
# identify the employer, but the *message* still comes from one — so the sender
# display-name and the subject are legitimate places to look for the company.
ATS_RELAY_DOMAINS: frozenset[str] = frozenset(
    {
        # Applicant tracking systems / recruiting relays (front many employers).
        "lever",
        "greenhouse",
        "greenhouse-mail",
        "greenhousemail",
        "hire",
        "myworkday",
        "myworkdayjobs",
        "workday",
        "icims",
        "ashbyhq",
        "smartrecruiters",
        "jobvite",
        "workable",
        "recruitee",
        "bamboohr",
        "breezy",
        "teamtailor",
        "gem",
        "goodtime",
        "modernloop",
        "taleo",
        "successfactors",
        "brassring",
        "oraclecloud",
        "eightfold",
        "avature",
        "phenom",
        "paradox",
        # Rippling fronts other employers' careers mail from ats.rippling.com.
        # Observed live: "Thank You for Applying to Supernova Technology" sent by
        # no-reply@ats.rippling.com filed an application at *Rippling*, which is
        # not a company the owner applied to. The sender display name already
        # said "Supernova Technology"; the domain overrode it.
        "rippling",
        "pageuppeople",
        "pageup",
        "jobs",
        "jobapp",
        "myjobs",
        "onboarding",
        "online-onboarding",
        # Job boards / aggregators / campus recruiting.
        "linkedin",
        "indeed",
        "ziprecruiter",
        "glassdoor",
        "wellfound",
        "angel",
        "monster",
        "dice",
        "handshake",
        "joinhandshake",
        "hire-education",
        "builtin",
        "lensa",
        "simplyhired",
        # Generic mail-relay / ESP brands that front many senders.
        "sendgrid",
        "mailgun",
        "amazonses",
        "mailchimp",
        "mandrillapp",
        "sparkpostmail",
        "notifications",
        "email",
        "mail",
        "notification",
        "message",
        "messaging",
    }
)

# Domains that relay an EMPLOYER'S ASSESSMENT on the employer's behalf: coding
# challenge, skills-test and recorded-interview vendors. A different kind of
# relay from an ATS — nobody applies *through* Coderbyte the way they apply
# through Greenhouse — but the same thing is true of the domain, which is all
# this list is for: the brand identifies the COURIER, not the employer.
#
# Kept as its own named set rather than buried in ``ATS_RELAY_DOMAINS`` because
# the two answer different questions and only one of them earns the display-name
# and subject-lead fallbacks: see :func:`resolve_employer`, whose steps 3 and 4
# stay scoped to ATS relays.
#
# #687 IS WHAT THIS IS FOR. Coderbyte's own no-reply address sent "Netic AI
# invites you to take an assessment" and the board grew a card at **Coderbyte** — a
# company the owner never applied to — while the real Netic AI application sat on
# a separate card that never advanced past APPLIED. ``ATS_RELAY_DOMAINS`` had 62
# members and not one assessment vendor, even though ``classifier/rules.py``
# already knows ``hackerrank``/``codility``/``codesignal``/``hirevue`` well
# enough to CATEGORISE their mail. The system recognised the platform and did not
# know it was a courier.
#
# LEETCODE IS DELIBERATELY ABSENT, and it is on the classifier's list. The two
# lists answer different questions and this is not a contradiction: "does this
# text talk about an assessment" is true of LeetCode's own product mail, while
# "does this domain front an employer" is not. LeetCode is overwhelmingly a
# consumer practice site mailing its own users about itself, and membership here
# is not free — it would take the one population where the domain IS the right
# answer and push it onto weaker signals. Its assessments product does exist; if
# a real employer-fronted LeetCode invite ever lands, add it then, with the
# message that justifies it.
ASSESSMENT_RELAY_DOMAINS: frozenset[str] = frozenset(
    {
        # Coding-challenge / skills-assessment vendors.
        "coderbyte",
        "hackerrank",
        "hackerearth",
        "codility",
        "codesignal",
        "codesubmit",
        "testgorilla",
        "devskiller",
        # DevSkiller renamed to SkillPanel in September 2025 and
        # `devskiller.com` 301s to `skillpanel.com`. BOTH stay, because both
        # are live senders on different stacks: `devskiller.com` publishes an
        # SPF including Postmark with `p=reject`, `skillpanel.com` publishes
        # its own including Zoho. A rename is not a migration.
        "skillpanel",
        "imocha",
        "mettl",
        # Mettl is Mercer's, and its regional senders are the reason this entry
        # is three lines of prose rather than one string.
        #
        # `mercermettl` is safe: nobody applies to a company by that name. The
        # `.eu` half is the evidenced one (SPF `include:amazonses.com`, an
        # outbound sender); the `.com` half rests on the vendor's KB text alone
        # and has no SPF, so it is here on weaker ground — recorded so the next
        # reader knows which half to doubt.
        #
        # `mercer` IS DELIBERATELY ABSENT AND MUST STAY ABSENT. Mettl's Indian
        # region sends from an `admin.mettl` mailbox on Mercer's own domain, so the brand that would
        # match is `mercer` — and Mercer is a large real employer that sends its
        # own recruiting mail. Adding it would push every genuine Mercer
        # application onto the display-name and subject fallbacks, which is the
        # person-as-employer class this module already fights. That address is
        # therefore UNREACHABLE by a brand-keyed set, and catching it needs a
        # full-address exception, which is a product decision and not a set edit.
        "mercermettl",
        # Interview-as-a-service and recorded/one-way interview vendors. The
        # mail they send is an invitation to an employer's assessment round, so
        # the employer is named in the message and never in the domain.
        "hirevue",
        "karat",
        "woven",
        # THE ONE THIS AUDIT WAS RUN TO FIND. Woven's candidate mail comes from
        # `woventeams.com`, which `_domain_brand` renders `woventeams` — so the
        # plain `woven` entry above never matched a real Woven message and an
        # invite would have filed a card at the vendor, which is #687's defect
        # still live for one vendor. Evidenced by Greenhouse's own Woven
        # integration doc naming a `candidates+greenhouse_errors` mailbox at that domain,
        # and by DNS: live Google MX, SPF, and its own DMARC record.
        #
        # `woven` stays. `woven.com` is a different entity and `woven.io` is
        # parked, which is exactly why the wrong entry looked correct on
        # inspection — there was no alternate sender to notice.
        #
        # Watch item, not a change: Andela acquired Woven in January 2026.
        # `woventeams.com` is live today and there is no evidence of a mail
        # migration, so `andela` is NOT added on speculation.
        "woventeams",
        "sparkhire",
    }
)

# Consumer webmail. Also a relay in the sense that the domain never identifies
# an employer — but unlike an ATS relay there is no employer behind it at all:
# a display-name here is a PERSON ("Julee Johnson"), which is exactly how the
# board once grew a "Julee Johnson → OFFERED" row. Kept as its own set so the
# display-name/subject fallbacks in :func:`resolve_employer` can be applied to
# ATS mail WITHOUT ever being applied to a human's personal mail.
CONSUMER_WEBMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail",
        "googlemail",
        "outlook",
        "hotmail",
        "live",
        "yahoo",
        "ymail",
        "aol",
        "icloud",
        "me",
        "proton",
        "protonmail",
        "zoho",
    }
)

# Every domain whose brand must NOT be used as the employer. Composed from the
# three sets above so membership can never drift between them.
RELAY_DOMAINS: frozenset[str] = (
    ATS_RELAY_DOMAINS | ASSESSMENT_RELAY_DOMAINS | CONSUMER_WEBMAIL_DOMAINS
)

# Corporate/recruiting noise words stripped from a sender display-name before
# it is used as a company token ("Acme Recruiting" / "Acme via Lever" → "acme").
_NAME_NOISE = re.compile(
    r"\b(?:recruit(?:ing|er|ment)?|talent|careers?|jobs?|hiring|hr|"
    r"people|team|no[-\s]?reply|noreply|notifications?|via|the)\b",
    re.IGNORECASE,
)

# Subject patterns that name the company directly. First match wins.
_SUBJECT_COMPANY = re.compile(
    r"(?:application|interview|role|position|opportunity|offer)\s+"
    r"(?:to|at|for|with|from)\s+([A-Z][\w&.\- ]{1,40}?)"
    r"(?=[\s,.!?:;)]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PipelineItem:
    """One classified message reduced to what the analytics need.

    ``confidence`` is the classifier's confidence for ``category`` (0.0-1.0).
    It is what the Phase-2 rollup gates on: only a *high-confidence* lifecycle
    verdict may assert a hard application status, so a low-confidence guess can
    no longer manufacture a fake ``interviewing``/``offered`` row. Absent (the
    default 0.0) it is treated as "no confidence" — the safe end of the gate.
    ``thread_id`` lets a persisted row deep-link back to the Gmail conversation.
    """

    message_id: str
    category: str
    sender_email: str
    subject: str
    sender_name: str | None = None
    received_at: datetime | None = None
    confidence: float = 0.0
    thread_id: str | None = None
    snippet: str = ""
    # WHICH APPLICATION THIS MESSAGE NAMES, derived by the reader from the
    # message BODY rather than re-derived here from ``snippet``.
    #
    # ``snippet`` is Gmail's own ~200 characters and is all this dataclass used
    # to carry, so a title printed past character 200 was invisible to every
    # identity decision while the classifier — which IS handed the body — read
    # it correctly. Torc's card carried no position for that reason alone.
    #
    # NULL/None means "not derived", not "names nothing": the client relay path
    # carries a snippet and no body, so its items leave these unset and
    # :func:`item_identity` falls back to reading ``snippet``, which is exactly
    # what it did before. An empty string means "derived, and it names nothing".
    identity_role: str | None = None
    identity_req_id: str | None = None
    # WHICH LAYER PRODUCED ``category``, straight off the classifier that ran.
    #
    # This used to be thrown away, and the persist layer wrote the literal
    # ``"rules"`` for every row it stored (#496). ``get_classifier`` is
    # ``get_hybrid_classifier``, so the layer that actually answered may equally
    # have been embeddings, setfit, the content filter or the fallback — the
    # column read like provenance and was a constant, which is no evidence at
    # all. It already cost one wrong diagnosis: while tracing #493 the stored
    # rows claimed ``rules`` while the rules layer disagreed with them, and the
    # contradiction was read as "some other layer labelled this". It had not.
    #
    # ``None`` means NOT DERIVED HERE and is the honest answer for the two
    # client-relay paths, where the caller classified the mail and the server
    # never saw a classifier run. It is written through as NULL rather than
    # backfilled with a guess: the column is ``Optional[str]`` and nullable rows
    # already exist, and "we do not know" is a different fact from "rules".
    method: str | None = None


@dataclass(frozen=True)
class FollowUp:
    """An `applied` message flagged as ghosted (no later response)."""

    message_id: str
    company: str
    subject: str
    days_since: int
    applied_at: datetime | None = None


def _normalize_token(value: str) -> str:
    """Lowercase, collapse to ``[a-z0-9]`` words, join with single spaces."""

    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _domain_brand(domain: str) -> str:
    """Return the registrable brand label of a host (``jobs.acme.co.uk`` → acme).

    A tiny public-suffix heuristic: if the last two labels look like a country
    second-level domain (``co.uk``, ``com.au``, …) the brand is the third-from-
    last label; otherwise it is the second-from-last. Good enough to group a
    company's own mail without shipping a full PSL.
    """

    labels = [p for p in domain.lower().split(".") if p]
    if len(labels) < 2:
        return labels[0] if labels else ""
    cc_slds = {"co", "com", "org", "net", "ac", "gov", "edu"}
    if len(labels) >= 3 and labels[-2] in cc_slds and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2]


def _company_from_subject(subject: str, assessment_relay: bool = False) -> str:
    """The company a subject names, as a grouping token, or ``""``.

    ``assessment_relay`` carries the SAME fence :func:`_employer_from_subject`
    applies, and it is passed rather than re-derived so the two entry points
    cannot answer the question differently. See :data:`_EMPLOYER_INVITES` for
    why the fence is that narrow: off an assessment vendor, "<Name> invites you
    to a technical screening" is a RECRUITER, and reading it here is the
    grouping equivalent of the "Julee Johnson → OFFERED" row.
    """

    match = _SUBJECT_COMPANY.search(subject or "")
    if match:
        return _normalize_token(match.group(1))
    if assessment_relay:
        invited = _EMPLOYER_INVITES.search(subject or "")
        if invited:
            return _normalize_token(invited.group(1))
    return ""


def _company_from_name(sender_name: str | None) -> str:
    if not sender_name:
        return ""
    stripped = _NAME_NOISE.sub(" ", sender_name)
    return _normalize_token(stripped)


def company_key(
    sender_email: str,
    subject: str = "",
    sender_name: str | None = None,
) -> str:
    """Collapse a message to a stable company token used for grouping.

    Strategy, in order:

    1. Take the sender-domain brand (``jobs.acme.com`` → ``acme``).
    2. If that brand is a shared relay (ATS / job board / assessment vendor /
       consumer webmail) it does NOT identify the employer, so derive the
       company from the subject ("application to <Company>", and — for an
       ASSESSMENT vendor only — "<Company> invites you to take an assessment")
       and then from the cleaned sender display-name, falling back to the relay
       brand only if neither yields anything.

    Always returns a non-empty token (``"unknown"`` as the last resort) so
    callers can group without None-guards.
    """

    domain = ""
    if "@" in sender_email:
        domain = sender_email.rsplit("@", 1)[1].strip().lower()
    brand = _domain_brand(domain)

    if brand and brand not in RELAY_DOMAINS:
        return brand

    from_subject = _company_from_subject(
        subject, assessment_relay=brand in ASSESSMENT_RELAY_DOMAINS
    )
    if from_subject:
        return from_subject

    from_name = _company_from_name(sender_name)
    if from_name:
        return from_name

    return brand or "unknown"


def normalize_company_name(value: str) -> str:
    """Public form of the internal token normalizer.

    Lowercase, collapse to ``[a-z0-9]`` words, single-space joined. Exposed so
    the persistence layer can normalize a STORED company name with exactly the
    same rules the tokens were minted under, instead of inventing a fifth
    spelling of "the same company" (``lower(company)``, which is what filed a
    second "Together AI" row on every sync).
    """

    return _normalize_token(value or "")


def matches_company_token(company_name: str, token: str) -> bool:
    """Does a stored row's company NAME identify the employer ``token`` names?

    The two sides are minted differently and cannot simply be compared:

    - a row stores the human DISPLAY name (``"Together AI"``, ``"Y Combinator"``);
    - a rollup carries the match TOKEN, which is either the sender's domain
      brand (``"tcs"``, ``"y-combinator"``) or the normalized FIRST WORD of a
      display name (``"together"``).

    So ``lower("Together AI") == "together"`` is false and the upsert filed a
    duplicate — twice on the owner's board (applications 64 and 65), with the
    only linked email re-pointed to the newer row and the older one stranded.

    Matching normalizes both sides and accepts either a full match or a match on
    the leading word, which is the same grouping :func:`roll_up_applications`
    already applies when it collapses a company's mail under one token. Two
    employers sharing a first word therefore merge — but they would have shared
    a rolled row anyway, whereas the alternative is the duplicate above.

    THIS RULE HAS A WEB MIRROR: ``matchesEmployerToken`` in
    ``apps/web/lib/dashboard/review.ts``. The filed ledger asks the user which
    application a correction is about, and it may only offer rows this function
    would accept as an answer — offering one it would reject files the mail
    somewhere else, and offering none asks nothing and lets the backend's
    tie-break move a live application unasked (#560). The two are held together
    by ``apps/web/tests/fixtures/employer-token-match.json``, a table both
    sides execute; add a case there, not to one side.
    """

    left = _normalize_token(company_name or "")
    right = _normalize_token(token or "")
    if not left or not right:
        return False
    if left == right:
        return True
    return left.split(" ")[0] == right.split(" ")[0]


def summarize(items: Iterable[PipelineItem]) -> dict[str, int]:
    """Count messages per canonical category (every bucket present, 0-filled)."""

    counts = dict.fromkeys(CANONICAL_CATEGORIES, 0)
    for item in items:
        if item.category in counts:
            counts[item.category] += 1
        else:  # a category outside the known set still gets tallied honestly
            counts[item.category] = counts.get(item.category, 0) + 1
    return counts


def _as_utc(value: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC (naive → assumed UTC)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to NAIVE UTC for persistence, or pass through None.

    The DB columns are ``TIMESTAMP WITHOUT TIME ZONE`` and the codebase writes
    naive ``datetime.utcnow()``-style values. But ``received_at`` comes from
    ``email.utils.parsedate_to_datetime``, which returns a timezone-AWARE
    datetime — and asyncpg refuses to encode an aware datetime into a naive
    column (``DataError``), which 500'd the whole sync in production. Every
    datetime that flows into a persisted column MUST pass through here so the DB
    never sees a mix of naive and aware values. (SQLite silently tolerates the
    mismatch, which is why the unit suite missed it.)
    """

    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def flag_follow_ups(
    items: Iterable[PipelineItem],
    *,
    now: datetime | None = None,
    stale_days: int = 21,
) -> list[FollowUp]:
    """Flag `applied` mail that a company never responded to.

    An application is "ghosted" when there is no later message answering it in
    :data:`RESPONSE_CATEGORIES` (interview / assessment / offer / rejection) and
    it is at least ``stale_days`` old.

    "Answering it" is judged per APPLICATION, not per company: since one employer
    can hold several applications, a rejection for one role must not silence the
    nudge for a different role that really has gone quiet. A response that names
    a role answers only that role; a response that names none — "Update on your
    application" is the common shape — cannot be attributed, so it counts as
    contact for the whole company. That direction is deliberate: suppressing a
    nudge is a small annoyance, while asserting a company has ignored you when it
    has already written back is the kind of wrong that makes a user distrust the
    product.

    De-duplicated to at most one flag per application — the oldest un-answered
    one — so re-sending the same application does not produce two identical
    "follow up" cards.

    Returns the flags sorted by ``days_since`` descending (most overdue first).
    """

    reference = _as_utc(now) if now is not None else datetime.now(UTC)
    materialized = list(items)

    def sub_key(item: PipelineItem) -> str | None:
        return item_identity(item)

    # Group every message by company so we can ask "did THIS company respond?",
    # then narrow to the specific application inside the loop.
    by_company: dict[str, list[PipelineItem]] = defaultdict(list)
    for item in materialized:
        key = company_key(item.sender_email, item.subject, item.sender_name)
        by_company[key].append(item)

    best_per_company: dict[str, FollowUp] = {}
    for item in materialized:
        if item.category != "applied" or item.received_at is None:
            continue
        applied_at = _as_utc(item.received_at)
        key = company_key(item.sender_email, item.subject, item.sender_name)
        mine = sub_key(item)

        responded = any(
            other.category in RESPONSE_CATEGORIES
            and other.received_at is not None
            and _as_utc(other.received_at) >= applied_at
            # None on either side means "not attributable to one role" and so
            # counts company-wide; two named roles must match to answer.
            and (mine is None or (theirs := sub_key(other)) is None or theirs == mine)
            for other in by_company[key]
        )
        if responded:
            continue

        days_since = (reference - applied_at).days
        if days_since < stale_days:
            continue

        candidate = FollowUp(
            message_id=item.message_id,
            company=key,
            subject=item.subject,
            days_since=days_since,
            applied_at=applied_at,
        )
        current = best_per_company.get(key)
        if current is None or candidate.days_since > current.days_since:
            best_per_company[key] = candidate

    return sorted(
        best_per_company.values(), key=lambda f: f.days_since, reverse=True
    )


# =============================================================================
# Rollup → Application rows (Phase 2: dashboard persistence)
# =============================================================================
#
# The classified pipeline is grouped into ONE application per company (role
# where detectable), with the status set to the furthest lifecycle stage that
# company's mail reached. These plain-string statuses match ApplicationStatus
# values; sync.py maps them to the enum + upserts. pipeline.py stays DB-free.
#
# PRECISION GATE (issue: "far too eager, low precision")
# ------------------------------------------------------
# A message may only assert a *hard* application status when BOTH:
#   1. its classifier confidence is at or above the auto-file gate (0.85), and
#   2. a real employer can be identified from the mail (never the sender domain
#      of a shared ATS relay, never a bare subject fragment, never a person).
# A lifecycle verdict in the 0.70-0.85 band, or one that clears the gate but
# whose employer cannot be named, is routed to a *review* bucket rather than
# fabricating an ``interviewing``/``offered``/``rejected`` row. Anything below
# the review floor is dropped. Net effect: a handful of real rows, not 21 fake
# ones parsed out of job-alert/newsletter/onboarding noise.

# Confidence gates — lock-stepped with classifier/hybrid.py (CONFIDENCE_AUTO /
# CONFIDENCE_MIN_CLASSIFICATION) and with every copy on the web side. Both
# halves of that are now held by something that can fail, which is why this
# comment names the checks rather than asking you to remember:
#
#   backend  tests/test_confidence_gate_lockstep.py — the four Python copies
#            (here, hybrid.py, and classification.py's constant AND its
#            seed_training_data default).
#   web      scripts/readme_facts.py — an invariant that reads CONFIDENCE_AUTO
#            out of hybrid.py and each TypeScript gate constant out of apps/web
#            and fails when they disagree, plus a census so a new hand-written
#            copy has to be registered. It runs in readme-facts.yml, the one
#            workflow with no path filter, because backend-ci and frontend-ci
#            are each filtered to a single side and neither can see this drift.
#
# This comment used to name only `lib/dashboard/model.ts`, which the dashboard
# largely did not read — ReviewQueue and ApplicationDetail imported a second
# copy from components/viz/GateMeter.tsx, so the invariant claimed here could
# hold while the number a user actually sees drifted (#229). The second copy is
# gone; the pointer is a check now, not a promise.
AUTO_FILE_GATE = 0.85  # >= → may assert a hard status
REVIEW_FLOOR = 0.70  # [floor, gate) → needs human review; below → dropped

# Sort sentinel for undated mail (kept aware so mixed aware/naive never raises).
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# The same instant without a zone. ``to_naive_utc`` returns naive datetimes, and
# comparing one against ``_EPOCH`` raises, so a sort that falls back for undated
# mail needs this one rather than the aware constant above.
_NAIVE_EPOCH = datetime(1970, 1, 1)

# EmailCategory → lifecycle stage rank. Higher = further along.
#
# ``follow_up`` is deliberately absent. :func:`_qualifies_for_hard_row` drops
# follow-ups before any rank is consulted, so the entry that used to sit here
# was unreachable — and it read as if a nudge asserted ``applied``, which is a
# claim the pipeline does not make. (Behaviourally inert either way: the lookup
# defaults to 0 and :func:`_rank_to_status` bottoms out at ``applied``.)
_STAGE_RANK: dict[str, int] = {
    "applied": 1,
    "pending_application": 1,
    "assessment": 2,
    "interview": 3,
    "offer": 4,
}

# The categories that ASSERT a new application rather than report on an existing
# one. A confirmation is the only mail that says "I applied"; everything else
# (assessment, interview, offer, rejection) is a later message ABOUT an
# application that already exists. :func:`partition_applications` leans on that
# asymmetry to tell a second application from a second message.
#
# ``pending_application`` USED TO BE IN HERE, and the sentence above is why it
# no longer is — it enumerated one member while the set held two, and the code
# followed the set. A "please verify your email before we can review your
# application" is an outstanding STEP in an application that already exists; it
# reports, it does not assert. Leaving it in meant an employer's confirmation
# and its own verification mail read as two anonymous confirmations and minted
# two cards. Issue #459.
#
# It does NOT stop such mail getting a card. An employer whose only message is a
# pending_application still mints one through the "no other cluster" branch of
# :func:`partition_applications`, and ``EmailCategory.PENDING_APPLICATION`` still
# maps to ``ApplicationStatus.APPLIED``. What changes is narrower and is the
# whole point: it is no longer EVIDENCE OF A SECOND application.
APPLIED_SIGNAL_CATEGORIES: frozenset[str] = frozenset({"applied"})

# Application lifecycle status (ApplicationStatus values) by ascending progress.
# Used to advance monotonically (:func:`advance_application_status`).
#
# NOT the same scale as ``_STAGE_RANK`` above, and since ``assessment`` became a
# status (2026-08-12) the two no longer even share a maximum: stage ranks top
# out at 4 (``offer``), status ranks at 5 (``accepted``). Only ``_STAGE_RANK``
# values may be passed to :func:`_rank_to_status`; a status rank fed to it would
# read one stage too high.
_STATUS_RANK: dict[str, int] = {
    "applied": 1,
    "assessment": 2,
    "interviewing": 3,
    "offered": 4,
    "accepted": 5,
}

# A stored status the mail signal must never silently override (a manual/terminal
# decision the user or a prior signal already settled).
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"rejected", "accepted", "withdrawn", "ghosted"}
)

# Words that are never, on their own, a company or a role — so an extraction
# that yields only these is rejected (this is what stops rows like "The",
# "Software", "Team", "Careers" from ever being created).
_COMPANY_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "your", "our", "my", "this", "that", "these", "those",
        "new", "re", "fw", "fwd", "hi", "hello", "hey", "thanks", "thank",
        "software", "engineer", "developer", "engineering", "intern",
        "internship", "role", "roles", "position", "positions", "opening",
        "openings", "application", "applications", "interview", "interviews",
        "offer", "offers", "update", "updates", "team", "teams", "careers",
        "career", "job", "jobs", "hiring", "talent", "recruiting", "recruiter",
        "recruitment", "hr", "people", "services", "service", "mail", "email",
        "notification", "notifications", "message", "opportunity",
        "opportunities", "candidate", "candidacy", "status", "confirmation",
        "onboarding", "welcome", "us", "you", "we", "here", "now", "today",
    }
)

# Corporate suffixes / recruiting tails, matched ANYWHERE in a string. Used to
# ask "is what remains nothing but corporate noise?" — see the remainder test in
# :func:`_employer_lead_segment_candidates`, which is the only caller left.
#
# IT IS NO LONGER WHAT CLEANS A DISPLAY NAME, and #532 is why. As an unanchored
# substitution it ate these words out of the MIDDLE and the FRONT of a name, not
# only off the tail its own name claims:
#
#     "People Data Labs"      -> "Data"      leading word destroyed
#     "Team Liquid"           -> "Liquid"
#     "Systems Research"      -> "Research"
#     "Health Solutions Group"-> "Health"
#
# The first is not cosmetic. A row displayed "Data" no longer shares a leading
# word with the token "people", so :func:`matches_company_token` stops matching
# it and the next sync files a SECOND card for the same employer — the exact
# duplicate that function exists to prevent. ``_NAME_ROLE_TAIL``'s comment
# already claims "People Data Labs" is safe from being shredded from the middle
# out; that was true of ``_NAME_ROLE_TAIL`` and false of the pipeline, because
# this ran unanchored immediately after it.
_CORP_TAIL = re.compile(
    r"\b(?:inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|corp|corp\.|corporation|co|co\.|"
    r"gmbh|plc|group|holdings|technologies|technology|labs|systems|solutions|"
    r"careers?|recruiting|recruitment|talent|hiring|team|hr|people)\b\.?",
    re.IGNORECASE,
)

# What a DISPLAY name may end with and still not be part of the name: legal
# entity forms, and the recruiting-desk tail an ATS appends to an employer.
#
# Two differences from ``_CORP_TAIL`` above, and both are the fix for #532.
#
# ANCHORED TO THE END, applied repeatedly — the same shape as
# ``_NAME_ROLE_TAIL`` and for the same reason. "Crusoe Hiring Team" needs two
# passes; "Team Liquid" and "People Data Labs" must survive all of them.
#
# DESCRIPTIVE WORDS ARE GONE FROM THE SET. "Labs", "Systems", "Solutions",
# "Technologies", "Group" and "Holdings" are parts of company names, not
# suffixes to be trimmed off them, and stripping them is what put a shorter
# employer name on 1,420 of 9,252 graded cards — "Arcgrove" for a person who
# applied to "Arcgrove Systems". Grouping does not need them stripped:
# :func:`matches_company_token` collapses on the LEADING word, so "Arcgrove
# Systems" and the token "arcgrove" were always the same employer to the
# product. The trimming bought nothing and cost the second half of the name.
_DISPLAY_TAIL = re.compile(
    r"(?:\s|^)(?:inc|llc|l\.l\.c|ltd|corp|corporation|co|gmbh|plc|"
    r"careers?|recruiting|recruitment|talent|hiring|team|hr|people)\.?\s*$",
    re.IGNORECASE,
)

# "Acme via Lever" / "Acme (Greenhouse)" tails that name the relay, not the co.
#
# Applied ONLY to whitespace-canonicalised text — both callers collapse runs to a
# single space first, and that is a precondition, not a nicety. The pattern used
# to open with ``\s*`` and close with ``.*$``, and both were quadratic under
# ``re.sub``, which retries at every start position (CodeQL py/polynomial-redos,
# alert 80). ``\s*`` re-scanned a whitespace run from every offset inside it, and
# ``.*$`` re-scanned a line from every offset whenever ``$`` was out of reach —
# 2.1 s and 0.33 s respectively on an 8,000-character name, both growing as n².
# A caller-supplied company string is unbounded (``ReviewClassifyRequest.company``),
# so that is reachable, not theoretical.
#
# The leading ``\s*`` is gone because both callers ``.strip()`` the result, which
# removes exactly the whitespace it used to eat. ``.*$`` is ``.*\Z`` under DOTALL
# because on single-line input the two are the same match and the second cannot
# fail, so it never backtracks. Both rewrites are equivalence-checked against the
# old pattern over the test corpus in ``test_company_name_regexes_are_linear.py``.
_VIA_TAIL = re.compile(
    r"(?:\bvia\b|\bthrough\b|\bon\b|[(\[]).*\Z", re.IGNORECASE | re.DOTALL
)

# A capitalized proper-noun-ish company token (leading capital, up to 3 words).
_COMPANY_CAPTURE = r"[A-Z][A-Za-z0-9&.\-']*(?:\s+[A-Z0-9][A-Za-z0-9&.\-']*){0,3}"

# The same capture, but a FULL STOP ENDS IT. A period is kept only inside a
# token, where more word characters follow it ("Amazon.jobs", "Inc." losing a
# trailing dot `_CORP_TAIL` would strip anyway); a period followed by space is
# a sentence boundary and terminates the company.
#
# This exists because the body is a different medium from the subject. Subjects
# rarely carry a full stop, so `_COMPANY_CAPTURE` running past one costs
# nothing there; a body is prose and always does. Measured on the owner's own
# mailbox, the naive capture read "Thank you for your interest in Palantir.
# After careful consideration…" as the employer ``Palantir. After``. The token
# (first word) would still have been right, which is exactly why this would
# have survived every token-level assertion and shipped a wrong CARD TITLE.
_COMPANY_CAPTURE_SENTENCE = (
    r"[A-Z][A-Za-z0-9&\-']*(?:\.[A-Za-z0-9&\-']+)*"
    r"(?:\s+[A-Z0-9][A-Za-z0-9&\-']*(?:\.[A-Za-z0-9&\-']+)*){0,3}"
)

# The employer named explicitly in a subject, anchored to lifecycle language so
# a random "to Monday" is not mistaken for a company. The anchor/connective is
# case-insensitive (subjects are Capitalized) but the company capture stays
# case-sensitive so it only ever grabs a Capitalized proper noun. First match
# wins.
_EMPLOYER_ANCHORED = re.compile(
    r"(?i:(?:application|applying|apply|interview(?:ing)?|role|position|"
    r"opportunity|opening|offer|assessment|candidacy|"
    r"thank you for your interest in)\b[^\n]{0,40}?\b"
    r"(?:at|with|to|for|from|join)\s+)"
    r"(" + _COMPANY_CAPTURE + r")"
)
# "Thank you for your interest in <Company>" — the standard opening of an ATS
# REJECTION, and the reason a real one sat in the review queue labelled "we
# couldn't name the employer" while naming it (#512).
#
# It needs its own pattern because :data:`_EMPLOYER_ANCHORED` already lists this
# very phrase as an anchor and then requires a SECOND preposition after it
# (``at|with|to|for|from|join``). This phrase ends in its own preposition, so
# there is never another one to find, and the anchor could not fire on the
# shape it was added for. Measured, same sender, same employer:
#
#     "Thank you for applying to Verkada"            -> ('verkada', 'Verkada')
#     "Thank you for your interest in Verkada, Ayush" -> None
#
# The capture stays case-sensitive, which is what keeps the far more common
# "... interest in the <Role> position at <Company>" out: ``the`` is lowercase,
# this pattern does not match at all, and the subject falls through to
# _EMPLOYER_ANCHORED, which reads it correctly via "position at".
#
# IT DOES NOT ONLY ADD RESOLUTIONS, and an earlier version of this comment
# claimed it did. Measured over 17,462 corpus cases: 48 change, all of them
# DISPLAY-ONLY, none newly resolved and none lost, every token identical. Those
# 48 already resolved through the SENDER DISPLAY NAME — step 3 of
# `resolve_employer`, which does not run `_clean_company_display` — and the
# subject path outranks it, so they now take the subject's cleaned form:
#
#     "Thank you for your interest in Granitethwaitevale Labs, Ayush"
#         before ('granitethwaitevale', 'Granitethwaitevale Labs')
#         after  ('granitethwaitevale', 'Granitethwaitevale')
#
# ``_CORP_TAIL`` stripped "Labs"/"Systems"/"Group". That was pre-existing
# subject path behaviour, not something this pattern introduced — "Thank you for
# applying to Acme Labs" displayed "Acme" — so these 48 became CONSISTENT with
# every other subject-resolved employer rather than newly wrong. The token was
# unchanged, so nothing split, no card was re-keyed and no assertion in the
# suite moved: which is exactly why the false claim above could sit here
# unchallenged, and why it is corrected rather than deleted.
#
# #532 THEN CHANGED WHICH ANSWER THE TWO PATHS AGREE ON. They still agree; the
# display path stopped being the one that truncates, so "Granitethwaitevale
# Labs" now survives BOTH paths and the example above reads
# ('granitethwaitevale', 'Granitethwaitevale Labs') either way. Measured over
# the 17,260-message independent corpus: company drift 1420 -> 0, with cards,
# splits, merges and wrong-company all unmoved.
#
# "Thanks for your interest", "Thank you so much for your interest" and a bare
# "for your interest in" are all the same sentence with the same meaning; a
# pattern that only knew the longest one would leave the same bug for the other
# three. ``our``/``the``/``your`` are all company stopwords, so the possessive
# variants ("interest in our team") cannot mint a company.
#
# THE THANK-YOU IS MANDATORY, and an earlier draft of this pattern made every
# prefix group optional — which collapsed it to a bare "interest in <Capital>"
# and matched the wrong population entirely:
#
#     "Jobs matching your interest in Machine Learning" -> ('machine', 'Machine Learning')
#     "We noticed your interest in Data Science"        -> ('data', 'Data Science')
#
# `resolve_employer` gates `_qualifies_for_hard_row`, so those are not merely
# wrong labels — they are CARDS ON THE BOARD for employers that do not exist,
# minted out of job-alert mail, which is exactly the garbage the precision gate
# is for. That is a worse defect than the one this pattern fixes.
#
# The two negative controls this shipped with ("interest in our team", "interest
# in the position") did not catch it, and it is worth saying why: they passed on
# `_COMPANY_STOPWORDS`, not on the pattern. A Title-Case noun phrase has no
# stopword to catch it, so the controls were testing a different guard than the
# one under test.
_EMPLOYER_INTEREST_IN = re.compile(
    r"(?i:\b(?:thanks|thank\s+you)(?:\s+so\s+much)?\s+for\s+"
    r"(?:your\s+|the\s+)?interest\s+in\s+)"
    r"(" + _COMPANY_CAPTURE + r")"
)
# The same sentence, read out of the BODY rather than the subject. One of the
# two patterns in this module that are ever pointed at body prose — the other is
# :data:`_EMPLOYER_INVITED_BY_BODY`, added by #687 and fenced the same way.
#
# It exists for one measured population: an ATS rejection whose subject names
# the role and the candidate but not the employer — "<Employer> Follow-Up for
# <ROLE> | <Name>" reaches `_employer_from_subject` and matches nothing — while
# the first line of the body says "Thank you so much for your interest in
# <Employer>". The employer is right there, plainly readable by the human
# staring at the row, and the queue was telling him we could not name it.
#
# WHY THIS ONE PATTERN AND NOTHING ELSE. Body prose is a far weaker signal than
# a subject line, so the question is not "does this pattern work" but "does its
# population change when the medium does". Measured against 40 real messages
# spanning every ATS in the mailbox, it fires on 6, agrees with the existing
# resolution on 5, supplies the missing employer on 1, and disagrees on none.
# The bodies that would have been dangerous do not match, because the capture
# is case-sensitive and `_COMPANY_STOPWORDS` takes the rest:
#
#     "interest in the Software Engineer, C# position at Path Robotics"  no match
#     "interest in our Associate Software Engineer ... role"             no match
#     "interest in potential opportunities with Jump Trading"            no match
#     "interest in joining the flock here at MotherDuck"                 no match
#
# `_EMPLOYER_BARE_AT` and friends must NEVER be pointed at a body: "at Home",
# "at Noon", "at Miami University" are all ordinary body prose, and the comment
# on `_EMPLOYER_AT_END` already explains why that shape needs a relay fence a
# body cannot provide.
_EMPLOYER_INTEREST_IN_BODY = re.compile(
    r"(?i:\b(?:thanks|thank\s+you)(?:\s+so\s+much)?\s+for\s+"
    r"(?:your\s+|the\s+)?interest\s+in\s+)"
    r"(" + _COMPANY_CAPTURE_SENTENCE + r")"
)
_EMPLOYER_ON_BEHALF = re.compile(
    r"(?i:on behalf of\s+)(" + _COMPANY_CAPTURE + r")"
)
_EMPLOYER_BARE_AT = re.compile(r"(?i:\bat\s+)(" + _COMPANY_CAPTURE + r")")

# WHAT AN INVITATION IS AN INVITATION TO. Shared by the two patterns below —
# deliberately, because they ask the identical question of two media ("is this
# sentence an invitation to an employer's assessment round?") and a vocabulary
# that drifts between them would make the same message readable in the subject
# and unreadable in the body. Only the NOUN is shared; the two shapes put the
# employer on opposite sides of the verb and so cannot share a pattern.
#
# "INTERVIEW" IS NOT ON THIS LIST, and it was, until it was measured. An
# interview is the one thing on this list a PERSON invites you to in their own
# name, and two of the vendors this reading is fenced to — Karat and HireVue —
# sell exactly that:
#
#     "Sarah Chen invites you to interview"             -> ('sarah', 'Sarah Chen')
#     "Sarah Chen invites you to schedule an interview" -> ('sarah', 'Sarah Chen')
#
# which is #535's exact tuple, re-minted through a new door. Removing the noun
# refuses all four measured shapes of it. What it costs is a genuine "<Employer>
# invites you to complete an on-demand interview", which now reads nothing and
# goes to the review queue, where a person decides. That is the direction this
# module takes everywhere else, and a wrong employer on the board is strictly
# worse than a queued one.
#
# THE NOUNS THAT REMAIN CARRY THE SAME RISK IN A SMALLER POPULATION, and that is
# the honest statement of it rather than a claim of safety. "<Person> invites you
# to a technical screening" resolves the person, and so do the take-home
# exercise, test and challenge wordings. What contains it is the CALLER'S fence,
# not this list: those subjects reach this pattern only from one of the fourteen
# assessment vendors, never from the ATS and scheduling relays (`goodtime`,
# `modernloop`, Greenhouse) whose mail routinely carries a recruiter's name. An
# earlier draft fenced this to ATS relays as well and filed "Sarah Chen" as a
# company from a Greenhouse subject, above `AUTO_FILE_GATE`, with no display name
# involved at all.
#
# THE WORD BOUNDARIES AROUND THIS ARE LOAD-BEARING and live at the two use sites.
# Without the leading one "assessment" matches inside "reassessment" and "test"
# inside "pretest" and "contest"; without the trailing one "assessmentathon"
# reads. All four are gated.
#
# ONE DISAGREEMENT WITH THE CLASSIFIER, STATED RATHER THAN CLOSED: "a
# self-assessment" satisfies this list, because "self-assessment" carries a word
# boundary in front of "assessment" — while `classifier/rules.py` explicitly
# VETOES ``\bself[- ]assessments?\b`` for ASSESSMENT. So the classifier says
# that phrase is not an assessment and this reader says the sentence names an
# employer. Closing it needs a lookbehind and a decision about what a
# self-assessment invitation is, which is not #687's to make; it is recorded here
# so the next reader finds it rather than rediscovering it.
_INVITATION_OBJECT = r"(?:assessments?|tests?|challenges?|screenings?|exercises?)"

# "<Employer> invites you to take an assessment" — THE EMPLOYER AS THE SENTENCE
# SUBJECT, which is the shape #687 filed under the wrong company.
#
# Every other pattern in this module reads the employer as the OBJECT of a
# preposition ("application to <X>", "on behalf of <X>", "@ <X>"), so an
# assessment vendor's standard invitation subject matched nothing at all and the
# resolver fell through to the sender display name — which is the vendor's. The
# live row: Coderbyte's own no-reply address, "Netic AI invites you to take an
# assessment", filed at *Coderbyte*.
#
# THREE FENCES, and #535 is why each one is here rather than "a leading
# Title-Case run". That issue is the record of what a loose leading-capitals rule
# mints on this exact mail — companies named "Invitation", "Decision", "Sorry"
# and "Sarah Chen", auto-filed above the gate because `resolve_employer` gates
# `_qualifies_for_hard_row`.
#
# 1. THE VERB IS THE ANCHOR. The capture is not "the subject starts with a
#    capital"; it is the grammatical subject of "invites/invited you", which is
#    an agent, and in an assessment vendor's mail the agent is the employer.
# 2. THE OBJECT MUST BE AN ASSESSMENT. Without it "Sarah Chen invites you to our
#    webinar" reads as a company. The bounded ``[^\n]{0,40}?`` is the same idiom
#    `_EMPLOYER_ANCHORED` uses and is bounded for the same reason: an unbounded
#    gap between two quantified runs is what the ReDoS work in
#    ``test_company_name_regexes_are_linear.py`` was cleaning up.
# 3. THE CAPTURE STARTS AT A BOUNDARY — the start of the subject or immediately
#    after punctuation. That is what handles the prefixes real mail uses:
#    "Reminder: <Employer> invites you…", "[Action Required] <Employer> invites
#    you…" and "Hi Ayush, <Employer> invites you…" all read the employer,
#    measured. A Title-Case phrase glued to the front with NO punctuation at all
#    ("Action Required Netic AI invites you…") is still captured whole, and this
#    residual is stated rather than hidden — it costs the TOKEN too, not only the
#    card title: `_normalize_token(...).split(" ")[0]` of that capture is
#    "action", which groups with nothing. No observed subject has that shape;
#    every prefix in the mailbox carries a bracket, a colon or a comma.
#
# The caller adds a FOURTH fence it cannot express here, and it is the one that
# does the most work: the message must have come from an ASSESSMENT VENDOR —
# `ASSESSMENT_RELAY_DOMAINS`, not the wider relay vocabulary — and the capture
# must not name that vendor.
#
# THE NARROWNESS IS THE POINT. Read off an ATS or a scheduling relay as well,
# this pattern mints a RECRUITER as a company: measured on Greenhouse with no
# display name at all, "Sarah Chen invites you to a technical screening" and four
# more wordings resolved ('sarah', 'Sarah Chen'), classify at 0.90-0.95, and so
# clear `AUTO_FILE_GATE` and file a card nobody chose — #535 all over again,
# through a door #535 never had. Scoped to the vendors, the same subjects resolve
# to nothing and go to the review queue. What it costs is a genuine "<Employer>
# invites you to take an assessment" relayed by an ATS rather than by the vendor:
# that queues now too, and queuing is the direction this module takes everywhere.
#
# An assessment vendor writes "Coderbyte invites you to take an assessment" about
# its own product mail, and that names the courier — hence the second half.
_EMPLOYER_INVITES = re.compile(
    r"(?:^|[|:;,.!?\]\)>–—-])\s*"
    r"(" + _COMPANY_CAPTURE + r")"
    r"(?i:\s+(?:has\s+|have\s+)?invit(?:es|ed)\s+you\b"
    r"[^\n]{0,40}?\b" + _INVITATION_OBJECT + r"\b)"
)

# The same invitation, read out of the BODY, where the employer moves to the
# other side of the verb: "you have been invited by <Employer> to complete an
# assessment". DISPLAY GRADE ONLY — it is reached from
# :func:`employer_named_in_body` and nothing else, so it cannot file a card.
# See that function's docstring for why body prose gets the strictest fence and
# never the filing path; this pattern carries the assessment-object requirement
# for the same reason.
_EMPLOYER_INVITED_BY_BODY = re.compile(
    r"(?i:\byou\s+(?:have\s+been|has\s+been|were|are)\s+invited\s+by\s+)"
    r"(" + _COMPANY_CAPTURE_SENTENCE + r")"
    r"(?i:[^\n]{0,40}?\b" + _INVITATION_OBJECT + r"\b)"
)

# "<Role> @ <Company>" — the at-sign an ATS puts between the job title and the
# employer, at the very END of the subject, which is where the employer sits in
# that grammar. Issue #325 is entirely about this pattern being tried FIRST,
# ahead of :data:`_EMPLOYER_ANCHORED`. Both fire on the real subject
#
#     "Important information about your application to
#      Systems Research Engineer, GPU Programming @ Together AI"
#
# and they cannot both be right: here "to" introduces the ROLE and the at-sign
# introduces the employer. One has to outrank the other, and the at-sign is the
# better claim — "<title> @ <company>" is a fixed convention with one meaning,
# while "application to X" is a preposition whose object is a company only when
# the subject happens not to name a role first. Anchored to the end so an
# at-sign anywhere else in a line cannot invent a company, and a trailing "!"
# or "." is tolerated because subjects carry them.
#
# Read only for mail from an ATS relay, and that restriction is load-bearing
# rather than cautious: "<title> @ <company>" is a convention of ATS subject
# lines, and off a relay the same shape is a time or a place. "Interview @ Noon"
# and "Coffee @ Home" both satisfy this pattern and neither names an employer —
# a person's mail is where they occur, and a person's mail is exactly what the
# relay test excludes. Steps 3 and 4 of :func:`resolve_employer` are fenced off
# the same way, for the same reason.
# The trailing run is POSSESSIVE. ``\s*[!?.]*\s*$`` holds two whitespace
# quantifiers either side of a punctuation one, so a subject ending in a long
# run of spaces made the engine try every way of splitting that run between them
# — 0.45 s on an 8,000-character subject, quadratic. No successful match ever
# needed those retries (giving a space back leaves ``[!?.]*`` facing whitespace,
# which it cannot match), so committing is equivalence-preserving; it is proven
# exhaustively over short strings rather than argued.
_EMPLOYER_AT_SIGN = re.compile(r"@\s*(" + _COMPANY_CAPTURE + r")\s*+[!?.]*+\s*+$")

# ...but an at-sign is also what an EMAIL ADDRESS is made of, so a capture whose
# dot is followed by more letters is a hostname ("… @ Careers.Acme.com") and is
# refused. The "@" branch of :func:`_employer_from_sender_name` refuses a
# hostname for the same reason, though with the blunter "any dot at all" — this
# one leaves a TRAILING dot alone, because "Acme Inc." is a company, not a host.
_CAPTURE_IS_HOSTNAME = re.compile(r"\.[A-Za-z]")

# Lifecycle words an ATS puts between the employer and the delimiter. These are
# what the message is ABOUT, never who sent it, so the capture stops at them.
_SUBJECT_LIFECYCLE_TAIL = (
    r"(?:Follow[-\s]?Ups?|Applications?|Interviews?|Offers?|Assessments?|"
    r"Updates?|Opportunit(?:y|ies)|Careers?|Recruit(?:ing|ment))"
)

# The employer named by the LEADING segment of an ATS subject, before a "|" or a
# spaced dash: "Crusoe | Application Received", "Acme — Interview scheduled".
# Anchored to the start so a separator later in the line cannot invent a company,
# and the capture stays case-sensitive so only a Capitalized proper noun is taken.
#
# THE SEGMENT MAY CARRY A LIFECYCLE TAIL (#512, gap 2). It used to require the
# company to run UNBROKEN to the delimiter, so Greenhouse's standard rejection
# subject — "<Employer> Follow-Up for <Role> | <Candidate>" — matched nothing at
# all: the lowercase "for" breaks the run, and the match failed rather than
# falling back to the Title-Case prefix. A rejection scored at 0.95 therefore
# produced no card, which is the row the owner reported three times.
#
# THE DELIMITER STAYS REQUIRED, and that restriction is the whole safety of
# this. Dropping it — "a leading company-shaped run terminated by a lifecycle
# noun" — reads just as well and mints JOB TITLES as employers: measured over a
# 28-subject trial it produced "Senior Software Engineer", "Machine Learning
# Engineer" and "Product Designer" as companies. This is the filing path, so a
# rule that invents three employers to rescue one is worse than the bug. With
# the delimiter kept the same trial had no false positive at all; what it costs
# is delimiter-less subjects like "Stripe Application Received", which resolve
# to nothing and go to the review queue, where a person decides.
#
_EMPLOYER_LEAD_SEGMENT = re.compile(
    r"^\s*(" + _COMPANY_CAPTURE + r")\s*(?:\||\s[-–—]\s)"
)

# THE CUT IS DONE IN CODE, NOT IN THE PATTERN, and that is a correctness fix
# rather than a style choice. Every attempt to express "company, then a lifecycle
# word, then anything, then the delimiter" as one regex loses to
# `_COMPANY_CAPTURE`'s own greed: it reaches the delimiter by itself wherever it
# can, so the lifecycle branch never gets the split it exists to produce.
# "Northwind Labs Application Update - <Role>" captured all four words twice
# over — once greedily to the dash, once greedily past "Application" — and
# yielded nothing either way.
#
# So the segment is taken first, then its leading Title-Case run, then that run
# is cut at its FIRST lifecycle word. Three small steps that each say what they
# do, instead of one pattern that has to be traced to be believed.
_SEGMENT_DELIMITER = re.compile(r"\||\s[-–—]\s")
_LEADING_RUN = re.compile(r"^\s*(" + _COMPANY_CAPTURE + r")")
#: What introduces a lifecycle word's object. "<Employer> Follow-Up FOR
#: <Role>" is the reported shape; "from", "with", "by" and the rest introduce a
#: person or a source, not the job the mail is about.
_LIFECYCLE_OBJECT = re.compile(r"^(?:for|regarding|re)\b", re.IGNORECASE)
_LIFECYCLE_WORD = re.compile(r"^" + _SUBJECT_LIFECYCLE_TAIL + r"$", re.IGNORECASE)

# Head nouns of a JOB TITLE. A leading segment that ENDS in one of these is
# describing the ROLE the message concerns, not naming the employer who sent it:
# in "Senior Software Engineer Interview" the noun is the head and everything
# before it modifies it.
#
# Kept SEPARATE from `_COMPANY_STOPWORDS`, and tested on the LAST word only,
# because real companies are full of these words anywhere else — "Team Liquid",
# "People Data Labs", "Cedar Labs" all survive, and all three would be destroyed
# by testing every word. The lifecycle nouns ("interview", "offer", "update")
# are already in `_COMPANY_STOPWORDS`; this adds the title heads that are not,
# which is what "Staff Data Scientist" and "Product Designer" turn on.
_ROLE_HEAD_NOUNS: frozenset[str] = frozenset(
    {
        "engineer", "developer", "designer", "scientist", "analyst",
        "architect", "manager", "director", "lead", "specialist",
        "consultant", "administrator", "technician", "researcher",
        "associate", "intern", "internship",
    }
)

# Role-ish tails an ATS sender's display name carries AFTER the company name:
# "Crusoe Hiring Team", "Supabase Recruiting", "Acme Talent Acquisition".
# Anchored to the END (and applied repeatedly) so a company whose own name
# contains one of these words — "People Data Labs", "Team Liquid" — is not
# shredded from the middle out the way a global substitution would do it.
_NAME_ROLE_TAIL = re.compile(
    r"(?:\s|^)(?:hiring|recruit(?:ing|ment|er|ers)?|talent|careers?|jobs?|hr|"
    r"people|team|notifications?|no[-\s]?reply|noreply|support|"
    r"acquisition|ops|operations)"
    r"\s*$",
    re.IGNORECASE,
)

# A display name that is really an email address ("no-reply@ashbyhq.com"), which
# names the relay, never the employer.
# Written as an unrolled loop rather than the obvious ``^\S+@\S+\.\S+$``: three
# greedy ``\S+`` runs separated by the very characters they can also match is
# quadratic (0.23 s on 8,000 at-signs). This form pins each run to the FIRST
# delimiter after it, which is the same language — the earliest ``@`` past index 0
# leaves the longest tail, so if any split satisfies the old pattern that one does
# — with no ambiguity left to backtrack through.
_NAME_IS_ADDRESS = re.compile(r"^\S[^\s@]*@[^\s][^\s.]*\.[^\s]+$")

# Pure filler that is never itself a role title (kept SEPARATE from the company
# stopwords, which reject legitimate title words like "Software"/"Engineer").
# Words that can never BE a job title on their own. Two groups: articles and
# mail-thread noise, and — added after the adversarial corpus caught it — the
# LIFECYCLE NOUNS that name what a message is about rather than what the job is.
#
# "Interview at <Employer>" is one of the commonest subjects an ATS sends, and
# ``_ROLE_PATTERNS``' "<TITLE> at <Company>" rule captured "Interview" from it.
# That is not a cosmetic wrong title: the role token IS the application's
# identity, so the interview mail keyed on "interview" while the confirmation
# keyed on the real title, and the board grew a second card. Same for "Offer
# from <Employer>" and "Assessment at <Employer>".
#
# Safe for real titles because ``_clean_role`` only rejects a capture when
# EVERY word is filler — "Application Engineer" and "Offer Management Lead"
# survive; the bare noun does not.
_ROLE_FILLER: frozenset[str] = frozenset(
    {"the", "a", "an", "your", "our", "my", "this", "that", "new", "re",
     "fw", "fwd", "update", "status", "position", "role", "opening",
     "interview", "interviewing", "application", "assessment", "offer",
     "invitation", "opportunity", "rejection", "confirmation"}
)

# Legal-notice phrases whose OWN next word is one of the role keywords, so the
# body patterns terminate on it and hand back the notice as a job title.
#
# Google's acknowledgement is the case that shipped: it closes with an
# equal-opportunity notice, "opportunity" is the weakest keyword in
# ``_ROLE_BODY_PATTERNS``, and the real board filed the Google card's position
# as `"Equal Employment`.
#
# MATCHED WHOLE, never as a prefix, and that is the whole care in this
# constant. "Equal Employment Opportunity Specialist" is a real job title and a
# prefix test would refuse it; the phrase is only a notice when the keyword that
# ENDED the capture was the phrase's own next word, which is exactly the case
# where the capture equals the stem and nothing more. Normalized through
# ``_normalize_token`` so quoting and punctuation cannot dodge it.
_LEGAL_NOTICE_STEMS: frozenset[str] = frozenset(
    {
        "equal employment",
        "equal opportunity",
        "equal employment opportunity",
        "affirmative action",
        "reasonable accommodation",
        "equal access",
    }
)

# Role named in the subject. Tried in order; the capture is validated against
# ``_ROLE_FILLER`` so "the role" alone never survives. Best-effort — a missing
# role renders as nothing, never the literal "Unknown role".
_ROLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:for the|for a|for an|as a|as an|regarding the|to the|to a|to an)\s+"
        r"([A-Za-z][\w/&.\-]*(?:\s+[\w/&.\-]+){0,4}?)\s+"
        r"(?:role|position|opening|opportunity|internship|intern)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?i:\bapplication for)(?i:\s+the)?\s+"
        r"([A-Z][\w/&.\-]*(?:\s+[A-Z0-9][\w/&.\-]*){0,4})",
    ),
    re.compile(
        r"^\s*(?i:new\s+)?"
        r"([A-Z][\w/&.\-]*(?:\s+[A-Z0-9][\w/&.\-]*){0,4}?)\s+"
        r"(?i:role|position|opening|internship)\b",
    ),
    re.compile(
        r"^\s*(?i:new\s+)?"
        r"([A-Z][\w/&.\-]*(?:\s+[A-Z0-9][\w/&.\-]*){0,4}?)\s+(?:at|@|[-–—])\s+[A-Z]",
    ),
)

# Role named in the BODY. Real ATS confirmations put the company in the subject
# and the role in the first sentence, which is why every one of the owner's four
# Amazon confirmations shares the subject "Thank you for Applying to Amazon!" and
# differs only here. Measured against the live corpus: these three patterns name
# the role for Amazon, Roblox, DoorDash, SimpliSafe, Crusoe, Baseten, Cursor,
# MotherDuck and Anthropic; Supabase, Twitch, Together AI and IXL genuinely name
# no role anywhere in the mail, and must degrade to None rather than to a guess.
#
# Ordered most-specific first. Each capture is bounded to one clause (no ``.``,
# ``!``, ``?`` or newline) so a runaway match cannot swallow the next sentence.
#: A parenthesised requisition id sitting between the title and the keyword that
#: terminates it — "…Annapurna Labs (ID: 10475660) position."
#:
#: IT MUST NOT COUNT AGAINST THE TITLE'S WIDTH. The role captures below are
#: bounded (``{3,90}``), and that bound is spent while the id is still inside the
#: span, even though :func:`_clean_role` deletes the id one line later. So the
#: bound is measured against text that is by construction not part of the answer.
#:
#: Measured, in the owner's mailbox on 2026-08-23. Amazon writes:
#:
#:     "...your application for the Software Development Engineer I – AI/ML
#:      Network Infrastructure, Annapurna Labs (ID: 10475660) position."
#:
#: "Software" to " position" is 92 characters WITH the id and 77 WITHOUT it. At
#: 92 the bound cannot be met, so the engine backtracks the preceding gap and
#: restarts the capture one word later — and applications 112 and 126 went to the
#: live board titled "Development Engineer I – AI/ML Network Infrastructure,
#: Annapurna Labs", each missing the first word of its own job title.
#:
#: Not a length problem to be solved by a bigger number: widening 90 to 120 was
#: measured on 2026-08-22 and made the corpus WORSE (splits 2 -> 3), because a
#: wider bound also lets prose through. The id is simply not part of what is
#: being bounded, so it is matched outside the bound instead.
#:
#: Optional, and the same label set :func:`_clean_role` strips, so the pattern
#: and the cleaner cannot disagree about what an id looks like. Case-insensitive
#: inline because one of the two patterns using it is not.
_ROLE_TRAILING_REQ = r"(?:\s*\(\s*(?i:(?:job\s*|requisition\s*|req\s*)?id[:\s#])[^)]{0,80}\))?"

#: A JOB TITLE IS A NOUN PHRASE, NOT A RUN OF CHARACTERS.
#:
#: The two patterns at the end of the tuple below have no trailing keyword to
#: stop on — the sentence simply continues past the title — so the tempered dot
#: every other pattern uses lets the capture run to whatever terminator turns up
#: next, however far downstream that is. Measured against the wordings real
#: recruiting mail uses, that is not a corner case: it produced
#:
#:     "Your application for Data Scientist is under review at Northwind."
#:         -> "Data Scientist is under review"
#:     "...an offer to join us as a Staff Engineer at Northwind."
#:         -> "Staff Engineer at Northwind"
#:     "Thanks for applying to Northwind at GHC last week!"
#:         -> "Northwind"
#:
#: A wrong title is strictly worse than the blank one these rules exist to fill:
#: the role token is half of an application's identity, so a title that reads
#: one way in the offer and another way in the confirmation MINTS A SECOND CARD
#: for a job the board already tracks. That is the failure this whole change set
#: was opened to fix, so producing it here would be self-defeating.
#:
#: So the span is SHAPED, not merely bounded. A title word is Capitalised (or an
#: acronym, a level, a roman numeral); words join on a space, a comma, a slash,
#: an ampersand or a hyphen; and only the few lowercase function words that
#: really do occur inside titles may sit BETWEEN two title words — "Head of
#: Design", "Engineer in Test". The span therefore begins and ends on a title
#: word, which is what refuses "Data Scientist, and" and stops "Machine Learning
#: Engineer at this time" at "Engineer".
#: A full stop is NOT part of a title word. Allowing it so that "Sr." could be
#: one word let "Operator Experience. We" be two, which is the sentence boundary
#: this whole fragment exists to respect. An abbreviation loses its stop and
#: keeps its meaning; a title that swallows the next sentence does not.
_ROLE_WORD = r"[A-Z][A-Za-z0-9&/+-]*"
_ROLE_INNER = r"(?:of|and|in|for|the)"
#: En and em dashes join title segments as often as the hyphen does — the
#: module's own worked example ("Software Development Engineer I – AI/ML
#: Network Infrastructure") uses one, and an ASCII-only joiner refused it.
_ROLE_JOIN = r"(?:[ ]+|[ ]*[,/&\u2010-\u2015-][ ]*)"
#: A trailing parenthetical is part of the posted title and routinely carries the
#: cohort — "Software Engineer I, Entry-Level (Graduation Date: Fall 2026)".
#: Bounded, and it may not contain a sentence-ender or a nested paren, so it can
#: only ever extend the span across material that was already inside the clause.
_ROLE_PAREN = r"(?:[ ]*\([^()\n.!?]{0,60}\))?"
_ROLE_SPAN = (
    _ROLE_WORD
    + r"(?:" + _ROLE_JOIN + r"(?:" + _ROLE_INNER + _ROLE_JOIN + r")?" + _ROLE_WORD + r"){0,9}"
    + _ROLE_PAREN
)

#: WHERE A TITLE ENDS: the clause ends, or lowercase prose resumes.
#:
#: Stated positively rather than as a list of stop-words, because a list is only
#: ever as complete as the sentences whoever wrote it had seen. "as a Software
#: Engineer starting on 5 January", "as a Staff Engineer at Northwind", "as a
#: Software Engineer on Northwind's Platform team" all end the title at the
#: first lowercase word, and so does every wording nobody has thought of yet.
#:
#: A newline is deliberately NOT a terminator. Plain-text bodies hard-wrap at
#: ~72 columns, and treating the wrap as the end of the clause turned
#: "as a Machine Learning\nEngineer." into the title "Machine Learning" — clean
#: enough to look right on a card and wrong enough to split the identity. The
#: span cannot cross the wrap either, so this shape now yields nothing and the
#: message goes to the review queue, which is the safe direction.
#: END OF STRING IS NOT A CLAUSE END, and this is the one pattern where the
#: difference is load-bearing. Every other body pattern needs a trailing keyword
#: and so fails CLOSED when the text runs out; this one has no keyword, so
#: accepting `$` made it fail OPEN on truncation. The extractor is fed
#: `bodies.get(id) or msg.snippet`, and Gmail's snippet is cut at an arbitrary
#: character — so "…an offer to join Northwind as a Software Eng" yielded the
#: title "Software Eng", and "…as a Software" yielded "Software".
#:
#: That is worse than a cosmetic wrong title. The role token is half the
#: identity, so a truncated capture mints a card that no later, fuller-text
#: message about the same job can ever join. Requiring real punctuation or
#: resumed prose means a cut-off body yields nothing and goes to the queue.
_ROLE_ENDS = r"(?=[.!?,;:]|\s+[a-z])"

_ROLE_BODY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Ashby: "Thank you for applying to our role: Software Engineer I, Storage."
    re.compile(r"\brole:\s*(?P<role>[^.!?\n]{3,90}?)\s*(?=[.!?\n]|$)", re.IGNORECASE),
    # "...application for the <ROLE> position", "...interest in the <ROLE> position",
    # "...applying to our <ROLE> role", "...application for the <ROLE> role"
    #
    # The anchor must be the article NEAREST the trailing keyword, not the
    # leftmost one. ``re.search`` returns the leftmost match, so the plain form
    # of this pattern anchored on the first preposition+article in the sentence
    # and let the lazy capture stretch across everything up to the sentence's
    # single "position" — which is how SimpliSafe's rejection ("Thank you for
    # your interest in SimpliSafe and our Software Engineer I- User Systems
    # position.") yielded the role "interest in SimpliSafe and our Software
    # Engineer I- User Systems" and minted a SECOND card for a job already on
    # the board.
    #
    # The capture is therefore TEMPERED: it may not run across another
    # anchor+article sequence. The leftmost start ("for your ") can then no
    # longer match at all, so the engine advances to the innermost one
    # ("and our ") on its own. Re-anchoring beats post-cutting because it
    # happens before the length bound is spent, and it needs no second list of
    # prepositions to be kept in sync.
    #
    # ``and`` joins the anchor alternation for the same message: an employer
    # that names itself and then its role ("interest in <Employer> and our
    # <ROLE> position") offers no preposition at the inner anchor.
    re.compile(
        r"\b(?:for|in|to|and)\s+(?:the|our|your|a|an)\s+"
        r"(?P<role>(?:(?!\b(?:for|in|to|at|with|and)\s+(?:the|our|your|a|an)\s)"
        r"[^.!?\n]){3,90}?)" + _ROLE_TRAILING_REQ + r"\s+"
        r"(?:position|role|opening|opportunity|req)\b",
        re.IGNORECASE,
    ),
    # DoorDash-shaped: "...applying to DoorDash's <ROLE> position!" — the employer
    # sits between the verb and the title, so no article anchors the capture.
    #
    # WHICH IS WHY IT USED TO TAKE THE EMPLOYER WITH IT. With nothing to anchor
    # on, the capture began at the first capitalised word after the verb, and
    # for a possessive employer that is the employer:
    #
    #   "applying to <Employer>'s Frontend Engineer position!"
    #      -> "<Employer>'s Frontend Engineer"
    #
    # while the same application's rejection said "apply for the Frontend
    # Engineer opening at <Employer>" and yielded "Frontend Engineer". Two
    # tokens, two cards, one application — and the title there is SEVENTEEN
    # characters, so this was never about length. See #466.
    #
    # A job title never contains "<Word>'s "; an employer's possessive does.
    # Forbidding it INSIDE the capture is what makes the capture start after it,
    # and it leaves every non-possessive wording untouched.
    re.compile(
        r"\b(?:applying|applied|application)\b[^.!?\n]{0,40}?"
        r"(?P<role>[A-Z](?:(?!'s\s)[^.!?\n]){3,90}?)" + _ROLE_TRAILING_REQ
        + r"\s+(?:position|role)\b",
    ),
    # Microsoft-shaped: "submit your application for Software Engineer II
    # (Job number: 200045485)." No article before the title and no trailing
    # "position"/"role" keyword after it, so none of the patterns above can
    # see it, and every Microsoft card on the board has a blank position.
    #
    # THE PARENTHESISED REQUISITION IS THE TERMINATOR, and that is what makes
    # this safe to add. The other patterns end on a common noun that can also
    # appear mid-sentence; this one ends on an employer explicitly labelling a
    # requisition, immediately after the title. The label alternation is the
    # same set `_REQ_ID_PATTERNS` accepts, so a wording either yields both a
    # role and an id or neither, rather than one of the two.
    #
    # THE CAPTURE USED TO EXCLUDE "(" TOO, on the stated reasoning that this is
    # what stops it running past the label. That reasoning was wrong — the
    # LABEL is what stops it, and it still does. Excluding the character only
    # meant a title containing an ordinary parenthesis produced NO ROLE AT ALL:
    #
    #   "...application for Software Engineer I, Entry-Level
    #    (Graduation Date: Fall 2025-Summer 2026) (Job number: 200045485)."
    #      -> role = None
    #
    # which is a real DoorDash title. That confirmation carried a requisition id
    # and no role while its own rejection carried a role and no id, so nothing
    # joined them and the application opened a second card. `Software Engineer
    # II (Job number: 200045485)` is the control and is unchanged. See #466.
    re.compile(
        r"\b(?:application|applying|applied)\s+(?:for|to)\s+"
        r"(?P<role>[^.!?\n]{3,120}?)\s*"
        r"\(\s*(?:job|requisition|req|posting|position|vacancy)\s*"
        r"(?:number|no\.?|id|code|ref(?:erence)?)\b",
        re.IGNORECASE,
    ),
    # Lever-shaped: "Thank you for submitting your application to be a Software
    # Engineer, New Grad at Palantir." The title is named plainly, with no
    # article anchor before it and no "position"/"role" noun after it, so every
    # pattern above walks past it — measured against the owner's real mail on
    # 2026-08-23, where Palantir's card sat with a blank position while both of
    # its messages spelled the title out.
    #
    # THE EMPLOYER IS THE TERMINATOR. "at <Capitalised>" is what ends the
    # capture, the same way the parenthesised requisition ends the pattern
    # above: a lowercase "at a company like ..." is not an employer and does not
    # terminate, so the capture simply fails rather than running to the end of
    # the sentence.
    #
    # ANCHORED ON THE APPLICATION WORD, and that is the whole safety of it.
    # "to be a <Title> at <Employer>" is an extremely common English shape that
    # has nothing to do with applying — "we have invited you to be a Mentor at
    # Palantir University" is the control, and it is refused here and nowhere
    # else: it is Title-Case and possessive-free, so neither the article
    # tempering nor the possessive guard above can see anything wrong with it.
    # Only the missing verb refuses it.
    #
    # LAST IN THE TUPLE, deliberately. :func:`role_from_message` returns the
    # first pattern that yields a clean role, so a rule appended here can only
    # fire where every other rule already found nothing. It cannot change a
    # single capture the board already has.
    re.compile(
        r"\b(?:application|applying|applied)\b[^.!?\n]{0,40}?"
        r"\bto\s+be\s+(?:an?|the)\s+"
        r"(?P<role>[A-Z](?:(?!'s\s)[^.!?\n]){3,90}?)" + _ROLE_TRAILING_REQ
        + r"\s+at\s+[A-Z]",
    ),
    # "…submit your application for <ROLE> at <Employer>." The sibling of the
    # Lever rule above with the words "to be a" absent, which is the commonest
    # confirmation wording there is — and every pattern before this one walks
    # past it. The subject rule for "application for" exists but requires an
    # unbroken Title-Case run, so a real title dies on its own punctuation:
    #
    #   "…application for Software Engineer I, Entry-Level (Graduation Date:
    #    Fall 2025-Summer 2026) at <Employer>."   ->  role = None
    #
    # 127 blank-titled cards in the corpus, all of this one shape.
    #
    # THE EMPLOYER IS THE TERMINATOR, exactly as above, and it is what makes
    # the loose capture safe: "at" followed by a capital ends it, a lowercase
    # "at a company like…" does not terminate and the capture simply fails
    # rather than running to the end of the sentence. The capture must also
    # START with a capital, which is what refuses "application to work at
    # <Employer>" — "work" is a lowercase verb, not a job title.
    #
    # "for" ONLY, never "to". "application to <X>" names the EMPLOYER, not the
    # job — "Thanks for applying to Northwind at GHC last week!" filed the
    # company itself as the position, and the company name is exactly the token
    # most likely to collide with a real card's identity. No corpus wording and
    # no observed template loses anything by the restriction: every "applying
    # to" shape in the corpus carries "position" or "role" and is answered by a
    # pattern above.
    re.compile(
        r"\b(?:application|applying|applied)\s+for\s+(?:the\s+)?"
        r"(?P<role>" + _ROLE_SPAN + r")" + _ROLE_TRAILING_REQ
        + r"\s+at\s+[A-Z]",
    ),
    # "…an offer to join <Employer> as a <ROLE>." The offer names the job and
    # nothing reads it, so all 260 cards the corpus opens from an offer carry a
    # blank title — and those are the cards a rescission later has to find.
    #
    # NO TRAILING KEYWORD EXISTS HERE. The sentence ends on the title, so the
    # patterns above — every one of which terminates on "position"/"role" or on
    # a requisition label — have nothing to stop on. The terminator is the end
    # of the clause.
    #
    # ANCHORED ON "offer … to join", not on the bare "as a" that carries the
    # title. "as a" is ordinary English ("we will be in touch as a team", "this
    # is sent as a courtesy") and a rule keyed on it alone would take a noun out
    # of any sentence in the corpus. Requiring the offer verb and the join verb
    # ahead of it is what makes the shape an assertion about a job.
    re.compile(
        r"\boffer\b[^.!?\n]{0,40}?\bto\s+join\b[^.!?\n]{0,60}?"
        r"\bas\s+an?\s+"
        r"(?P<role>" + _ROLE_SPAN + r")" + _ROLE_TRAILING_REQ + _ROLE_ENDS,
    ),
)

# A requisition id, when the employer prints one. DELIBERATELY conservative: a
# false shared id merges two genuinely different applications, which is strictly
# worse than having no id at all and falling back to the role token. So every
# pattern requires an explicit label or a recognised ATS shape, and a bare number
# (a year, a salary, "2026") never qualifies.
_REQ_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Amazon: "(ID: 3177934)". Also "Job ID 12345", "Requisition ID: R-4821".
    re.compile(
        r"\b(?:job\s*|requisition\s*|req\s*|posting\s*)?id[:\s#]+(?P<id>[A-Z]{0,3}-?\d{4,12})\b",
        re.IGNORECASE,
    ),
    # Workday/Greenhouse style standalone requisition codes: "R-4821", "JR0093214".
    re.compile(r"\b(?P<id>(?:R|JR|REQ)-?\d{4,10})\b"),
    # Microsoft: "(Job number: 200045485)". The pattern above requires the
    # literal word "id" and Microsoft does not use it, so every Microsoft
    # confirmation returned no requisition id at all.
    #
    # THIS COST FOUR REAL APPLICATIONS. On 2026-08-21 four Microsoft
    # applications were submitted within five minutes of each other, for
    # Software Engineer II (200045485), Customer Experience Engineer
    # (200049333), Software Engineer (200043070) and Pre-Training (200007619).
    # Every confirmation carries its own number, in the Gmail snippet, well
    # inside the 200 characters the snippet gives us. None was read, so all
    # four had null identity at an employer that already had a row.
    #
    # THE PREFIX IS MANDATORY HERE, unlike the `id` pattern above where it is
    # optional. "id" is already a strong enough token to stand alone; "number"
    # is not, and a bare `number[:\s#]+\d{4,12}` would happily match an order
    # number, a case number, a phone number or a tracking number in an
    # employer's boilerplate footer. A wrong requisition id is worse than none:
    # `_pick_application` files a message with no identity onto the employer's
    # existing row, while a wrong one mints a duplicate card.
    re.compile(
        r"\b(?:job|requisition|req|posting|position|vacancy)\s*"
        r"(?:number|no\.?|code|ref(?:erence)?)[:\s#]+(?P<id>[A-Z]{0,3}-?\d{4,12})\b",
        re.IGNORECASE,
    ),
)

# Words a role token drops before comparison, so "Software Engineer I, Storage"
# and "Software Engineer I - Storage" are the same application and not two.
_ROLE_TOKEN_STRIP = re.compile(r"[^a-z0-9]+")


def unescape_entities(text: str) -> str:
    """Undo the HTML entities Gmail snippets arrive carrying.

    Snippets come back pre-escaped (``We&#39;ve received your application``).
    That matters twice over: an escaped apostrophe inside a captured role makes
    two spellings of one title compare unequal, and the raw entity is also what
    the user READS — the detail sheet rendered "Please don&#39;t be" verbatim on
    the live board, because the snippet is stored exactly as fetched.

    Uses the stdlib table rather than a hand-written handful, so every entity
    Gmail can emit is covered rather than the six that happened to show up.
    ``&`` is a cheap guard for the common case of no entities at all.
    """

    if not text or "&" not in text:
        return text
    return html.unescape(text)


# ── deadlines ────────────────────────────────────────────────────────────────
#
# The product's landing page opens by promising that an assessment's 48-hour
# deadline will not pass unseen. Everything below is what makes that true, and
# the governing rule is that a deadline is REPORTED, never inferred: if the mail
# does not state one, the application does not have one. A fabricated deadline
# is worse than none — it would have someone drop what they are doing for a date
# nobody set.
#
# So every pattern requires an explicit deadline cue ("complete by", "expires",
# "within 48 hours"). A date merely mentioned in passing — an interview slot, a
# start date, a copyright year — never qualifies.

_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "january february march april may june july august september "
            "october november december"
        ).split(),
        start=1,
    )
}
_MONTHS.update(
    {m[:3]: i for m, i in list(_MONTHS.items())}
)

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

# The cue that a date is a DEADLINE and not just a date.
_DEADLINE_CUE = (
    r"(?:complete|submit|finish|respond|reply|return|accept|schedule)?[^.!?\n]{0,30}?"
    r"\b(?:due|deadline|expires?|expiring|by|before|no later than|within)\b"
)

# "within 48 hours", "you have 5 days", "48-hour window".
_RELATIVE_DEADLINE = re.compile(
    r"\b(?:within|in|you\s+have|have)\s+(?P<n>\d{1,3})\s*(?:-|\s)?\s*"
    r"(?P<unit>hours?|hrs?|days?|business\s+days?)\b",
    re.IGNORECASE,
)
_HYPHEN_WINDOW = re.compile(
    r"\b(?P<n>\d{1,3})\s*-\s*(?P<unit>hour|day)\s+(?:window|deadline|limit|period)\b",
    re.IGNORECASE,
)

# "by August 15, 2026", "before Aug 15", "expires on 08/15/2026".
_ABSOLUTE_WORD_DATE = re.compile(
    rf"\b(?P<month>{_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s*(?P<year>20\d{{2}}))?",
    re.IGNORECASE,
)
_ABSOLUTE_NUMERIC_DATE = re.compile(
    r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>20\d{2}|\d{2}))?\b"
)

# How far out a stated deadline may plausibly sit. Beyond this the parse is far
# likelier to be wrong than the employer is to mean it.
_MAX_DEADLINE_DAYS = 180


# A window belongs to the COMPANY, not the candidate. "We will get back to you
# within 5 business days" is the single most common sentence in application mail
# and it is not a deadline — it is a promise about them. Reading it as one would
# have put a fabricated due date on very nearly every card on the board.
_COMPANY_PROMISE = re.compile(
    r"\b(?:we(?:'|’)?ll|we\s+will|we\s+aim|our\s+team\s+will|the\s+team\s+will|"
    r"you(?:'|’)?ll\s+hear|you\s+will\s+hear|hear\s+(?:back\s+)?from\s+us|"
    r"get\s+back\s+to\s+you|be\s+in\s+touch|respond\s+to\s+(?:you|all)|"
    r"review\s+your\s+application)\b",
    re.IGNORECASE,
)

# The window belongs to the CANDIDATE: an instruction addressed to the reader.
_RECIPIENT_TASK = re.compile(
    r"\b(?:please|kindly|complete|submit|finish|return|accept|schedule|confirm|"
    r"you\s+(?:have|must|need|should)|your\s+(?:assessment|challenge|exercise|"
    r"take[-\s]?home|invitation|link))\b",
    re.IGNORECASE,
)


def _is_recipient_task(context: str) -> bool:
    """Is this clause telling the READER to do something by a time?

    Both halves are required. The promise test alone lets through a bare date
    with no owner; the task test alone lets through "we will review your
    application within 5 days", which contains "your application".
    """

    if _COMPANY_PROMISE.search(context):
        return False
    return bool(_RECIPIENT_TASK.search(context))


def _cue_window(text: str) -> list[str]:
    """The clauses that carry a deadline cue, so a stray date can't be read."""

    out: list[str] = []
    for match in re.finditer(_DEADLINE_CUE, text, re.IGNORECASE):
        # From the cue to the end of its clause — a deadline is stated forward
        # ("by August 15"), never backward — plus the run-up, which is where the
        # sentence says whose deadline it is.
        clause = text[match.start() : match.start() + 90]
        if _is_recipient_task(text[max(0, match.start() - 80) : match.start() + 90]):
            out.append(clause)
    return out


def extract_deadline(
    subject: str, snippet: str, received_at: datetime | None
) -> datetime | None:
    """The deadline a message STATES, in naive UTC — or None.

    Anchored to ``received_at`` because "within 48 hours" is meaningless without
    it, and because a parsed calendar date is only believable if it lands after
    the mail that announced it. Returns None for anything ambiguous: no cue, no
    anchor, a date that resolves into the past, or one absurdly far out.
    """

    if received_at is None:
        return None
    anchor = to_naive_utc(received_at)
    if anchor is None:
        return None

    text = unescape_entities(f"{subject or ''}. {snippet or ''}")

    # Relative windows first — they are unambiguous and the common case for
    # assessments ("complete within 48 hours").
    for pattern in (_RELATIVE_DEADLINE, _HYPHEN_WINDOW):
        for match in pattern.finditer(text):
            if not _is_recipient_task(
                text[max(0, match.start() - 80) : match.end() + 40]
            ):
                continue  # the company's promise about itself, not your deadline
            break
        else:
            continue
        n = int(match.group("n"))
        unit = match.group("unit").lower()
        if n == 0:
            continue
        if unit.startswith(("hour", "hr")):
            due = anchor + timedelta(hours=n)
        elif "business" in unit:
            # Weekends are not working days; step over them rather than
            # pretending a 5-business-day window is 5 calendar days.
            due = anchor
            remaining = n
            while remaining > 0:
                due += timedelta(days=1)
                if due.weekday() < 5:
                    remaining -= 1
        else:
            due = anchor + timedelta(days=n)
        if 0 < (due - anchor).total_seconds() <= _MAX_DEADLINE_DAYS * 86400:
            return due

    # Absolute dates, but only inside a clause that carries a deadline cue.
    for clause in _cue_window(text):
        for match in _ABSOLUTE_WORD_DATE.finditer(clause):
            month = _MONTHS.get(match.group("month").lower())
            if month is None:
                continue
            due = _resolve_calendar_date(
                anchor, month, int(match.group("day")), match.group("year")
            )
            if due is not None:
                return due
        for match in _ABSOLUTE_NUMERIC_DATE.finditer(clause):
            due = _resolve_calendar_date(
                anchor,
                int(match.group("month")),
                int(match.group("day")),
                match.group("year"),
            )
            if due is not None:
                return due
    return None


def _resolve_calendar_date(
    anchor: datetime, month: int, day: int, year: str | None
) -> datetime | None:
    """A stated calendar date as an end-of-day UTC deadline, or None.

    A year-less date takes the next occurrence at or after the mail — "complete
    by August 15" in a December message means the following August, not eight
    months ago. End of day because a date without a time is a whole day, and
    treating it as midnight would mark it overdue a day early.
    """

    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    years = (
        [int(year) if len(year) == 4 else 2000 + int(year)]
        if year
        else [anchor.year, anchor.year + 1]
    )
    for candidate_year in years:
        try:
            due = datetime(candidate_year, month, day, 23, 59, 59)
        except ValueError:
            continue  # e.g. Feb 30
        delta = (due - anchor).total_seconds()
        if 0 < delta <= _MAX_DEADLINE_DAYS * 86400:
            return due
    return None


def extract_req_id(subject: str, snippet: str = "") -> str | None:
    """Return the employer's own requisition id for this application, or None.

    This is the strongest identity signal available: two Amazon confirmations
    with different ids are two applications no matter how similar their titles
    read, and two messages carrying the same id are one application no matter
    how differently they word it.
    """

    for text in (subject or "", unescape_entities(snippet or "")):
        for pattern in _REQ_ID_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group("id").upper()
    return None


def _clean_role(raw: str) -> str | None:
    """Normalize a captured role, or None if the capture is not a real title."""

    role = re.sub(r"\s+", " ", raw).strip(" .,;:-–—")
    # The requisition id is identity, not part of the human-readable title.
    role = re.sub(r"\s*\(\s*(?:job\s*|requisition\s*|req\s*)?id[:\s#][^)]*\)", "", role, flags=re.IGNORECASE)
    # "applying to DoorDash's Software Engineer I" — the employer's possessive
    # rides in ahead of the title because no article separates them.
    role = re.sub(r"^\w+['’]s\s+", "", role)
    # A regex matches leftmost-first, so a sentence with two preposition+article
    # pairs hands back the prose between them: "Thank you for your interest in
    # the Software Engineer, C# position" captured on "for your …" and yielded
    # "interest in the Software Engineer, C#", which shipped to the live board.
    # Cutting at the LAST such pair keeps the innermost, which is the title.
    # Deliberately narrow — it only fires when the capture itself still contains
    # a preposition + article, which a real job title does not.
    role = re.sub(r"^.*\b(?:in|for|to|at|with)\s+(?:the|our|your|a|an)\s+", "", role, flags=re.IGNORECASE)
    role = role.strip(" .,;:-–—")
    # Last line of defence: a capture that STILL spans a clause boundary is a
    # sentence fragment, not a title, and a wrong role is strictly worse than no
    # role — `_pick_application`'s rule 4 files a role-less message onto the
    # employer's existing row, while a wrong role mints a duplicate card.
    #
    # Reachable independently of the body pattern above: the Ashby ``role:``
    # pattern is deliberately untempered (Ashby prints the title verbatim after
    # the colon), so "applying to our role: Software Engineer and our Storage
    # team" arrives here intact. Refusing is the correct outcome.
    #
    # Scoped to a conjunction/preposition + article sequence, plus the bare
    # possessives "our"/"your" which never appear inside a real job title. A
    # bare "the" is NOT refused: "Head of the Americas" is a legitimate title,
    # and the cut above has already removed every "the" that follows a
    # preposition.
    if re.search(r"\b(?:and|for|in|to|at|with)\s+(?:the|our|your|a|an)\b", role, re.IGNORECASE):
        return None
    if re.search(r"\b(?:our|your)\b", role, re.IGNORECASE):
        return None
    # A capture that OPENS a quotation and never closes it was cut out of a
    # quoted phrase rather than parsed as a title. Google's acknowledgement ends
    # with an equal-opportunity notice citing a poster by name, and the weakest
    # of the trailing keywords — "opportunity" — sits inside that title:
    #
    #   ... please refer to the "Equal Employment Opportunity is the Law" poster
    #                           ^^^^^^^^^^^^^^^^^^ capture   ^^^^^^^^^^^ keyword
    #
    # which filed the real board's Google card as the position `"Equal
    # Employment`, stray quote and all. The unbalanced quote is the structural
    # tell and it is not specific to this sentence: any title lifted out of a
    # quoted span carries one. A BALANCED pair is left alone — `Engineer II
    # ("Platform")` is a real, if ugly, title.
    # Double quotes only. An apostrophe is ordinary inside a title ("Women's
    # Health", "Engineers' Lead") and counting it here would refuse real roles
    # to catch a case the guard below already catches on its own terms.
    if role.count('"') % 2:
        return None
    # Legal boilerplate that shares its next word with a role keyword. Matched
    # WHOLE and only whole: the phrase is refused when the capture is exactly it
    # — i.e. the keyword that terminated the capture was the boilerplate's own
    # next word — and kept when the title continues past it. "Equal Employment
    # Opportunity Specialist" is a real job title and must survive; "Equal
    # Employment" immediately before "Opportunity" is a legal notice.
    if _normalize_token(role) in _LEGAL_NOTICE_STEMS:
        return None
    words = role.split()
    if not words or len(role) < 3:
        return None
    if all(_normalize_token(w) in _ROLE_FILLER for w in words):
        return None
    # A real job title in an ATS template is Title Case — "Software Engineer I,
    # Storage", "TPU Kernel Engineer". An all-lowercase capture is prose that
    # happened to sit between the anchors, and prose must never become an
    # identity: Supabase's "Thanks for your interest in a role with Supabase"
    # yielded the role "interest in a", which would have keyed an application.
    if not any(w[:1].isupper() for w in words):
        return None
    return role


def role_from_message(subject: str, snippet: str = "") -> str | None:
    """Extract the job title this message is about, or None. Never a guess.

    Subject first (it is the cleaner signal when present), then the body. The
    body half is what makes per-application tracking possible at all: ATS
    templates repeat one subject across every role a candidate applies to.
    """

    from_subject = _role_from_subject(subject)
    if from_subject is not None:
        return from_subject

    body = unescape_entities(snippet or "")
    for pattern in _ROLE_BODY_PATTERNS:
        match = pattern.search(body)
        if not match:
            continue
        role = _clean_role(match.group("role"))
        if role is not None:
            return role
    return None


def normalize_role_token(role: str | None) -> str | None:
    """Collapse a role title to a comparison key, or None.

    Punctuation and spacing vary between an employer's confirmation and its own
    later interview mail ("Software Engineer I, Storage" vs "Software Engineer I
    - Storage"); the token has to survive that or one application becomes two.
    """

    if not role:
        return None
    token = _ROLE_TOKEN_STRIP.sub(" ", role.lower()).strip()
    return token or None


def sub_key_from_parts(req_id: str | None, role: str | None) -> str | None:
    """The identity cascade, applied to values somebody ALREADY derived.

    The same order :func:`application_sub_key` uses — requisition id first, then
    the normalized role token — but reading parts that were extracted once, from
    the body, and then stored. Empty string is a derived "names nothing" and
    normalizes to ``None`` exactly like a missing one.

    Exists so that a caller holding derived parts and a caller holding raw text
    cannot disagree about the order of the cascade. They used to be able to.
    """

    return (req_id or None) or normalize_role_token(role)


def identity_or_derive(
    *,
    req_id: str | None,
    role: str | None,
    subject: str,
    snippet: str,
) -> str | None:
    """Which application this message names — from a derivation if there is one.

    THE ONE PLACE THAT DECIDES WHETHER TO TRUST A DERIVATION, for the same
    reason :func:`review_dedup_key` is the one place that decides what a
    decision is: the callers must not be able to disagree. There are three of
    them — the queue key, the card builder, and the ghosting sweep — and a rule
    written out three times is a rule with three answers.

    ``role``/``req_id`` are what the READER extracted, from the message body,
    and stored. Both ``None`` means no derivation exists for this message, not
    that it names nothing: a relay item from the client carries a snippet and
    never had a body, and every row written before the columns existed is in the
    same position. Those fall back to re-deriving from ``snippet``, which is
    exactly the old behaviour rather than a second competing answer.

    An empty string is a derived "names nothing" and resolves to ``None``, which
    stays a VALUE meaning "the same unknown" and not a failure — it is what
    keeps one employer's two identical acknowledgements a single decision.
    """

    role, req_id = identity_parts(
        req_id=req_id, role=role, subject=subject, snippet=snippet
    )
    return sub_key_from_parts(req_id, role)


def identity_parts(
    *,
    req_id: str | None,
    role: str | None,
    subject: str,
    snippet: str,
) -> tuple[str | None, str | None]:
    """``(role, req_id)`` — the derivation if there is one, else read the text.

    The half of :func:`identity_or_derive` that a caller minting a CARD needs,
    because a card shows the title itself and not the key that distinguishes it.
    Both go through here so the board and the queue cannot end up disagreeing
    about which application a message names — the failure #454 describes, where
    four of five sites computed a key one way and the fifth another.

    THE TRUST RULE LIVES HERE AND ONLY HERE. This function existed for one
    revision with the card builder carrying its own copy of the branch, and a
    mutation that removed the derivation left the card builder's tests green:
    the duplicate was doing the work, so nothing measured the rule. Both parts
    ``None`` means no derivation exists and the text is read instead; anything
    else is used as given, with ``""`` meaning "derived, names nothing".
    """

    if role is None and req_id is None:
        return (
            role_from_message(subject, snippet),
            extract_req_id(subject, snippet),
        )
    return (role or None, req_id or None)


def identity_never_derived(
    *,
    req_id: str | None,
    role: str | None,
    snippet: str,
) -> bool:
    """True when a stored row's silence about its application is IGNORANCE.

    Both parts ``None`` means no derivation exists for this row — the same
    reading :func:`identity_parts` makes — and an empty ``snippet`` means there
    is no text for a reader to derive one from now either. Together they say
    the row names no application because nothing about it is known, which is a
    different fact from a reader having looked and found none.

    ``is None``, never ``not role``: ``""`` is a DERIVED "names nothing", the
    honest value that keeps one employer's two identical acknowledgements a
    single decision, and a truthiness spelling here would collapse the two
    classes this predicate exists to tell apart.

    Lives beside :func:`identity_parts` because it reads the same trust rule
    and that rule is written once. Its caller is
    ``applications._settle_thread_siblings`` (#462), which settles a row of the
    first class — and only that class — when its thread names exactly one
    application. Both sources of the class are permanent: rows written before
    the identity columns existed (migration ``d5e91c4a7f28`` backfilled
    nothing, deliberately) and every row ``_record_scanned_email`` stores for a
    client-relayed scan, because :class:`PipelineItemIn` refuses to accept an
    identity from a client and must not learn to. A backfill cannot reach the
    second one.
    """

    return req_id is None and role is None and not snippet.strip()


def item_identity(item: PipelineItem) -> str | None:
    """:func:`identity_or_derive` for a message in flight."""

    return identity_or_derive(
        req_id=item.identity_req_id,
        role=item.identity_role,
        subject=item.subject,
        snippet=item.snippet,
    )


def item_identity_parts(item: PipelineItem) -> tuple[str | None, str | None]:
    """:func:`identity_parts` for a message in flight."""

    return identity_parts(
        req_id=item.identity_req_id,
        role=item.identity_role,
        subject=item.subject,
        snippet=item.snippet,
    )


def application_sub_key(subject: str, snippet: str = "") -> str | None:
    """WHICH application, within one employer, this message is about — or None.

    The identity cascade the whole module already uses, in one place:
    requisition id first (the employer's own key, and the only thing that tells
    two same-titled openings apart), then the normalized role token, then
    nothing — which is honest rather than empty. Plenty of real mail names no
    application at all: "Crusoe | Application Received" carries no role in its
    subject and no body to extract one from.

    None is a VALUE here, not a failure. Two messages that both name nothing are
    the same unknown, and callers that key on this rely on that: it is what
    keeps one employer's two identical acknowledgements a single decision.
    """

    return extract_req_id(subject, snippet) or normalize_role_token(
        role_from_message(subject, snippet)
    )


#: The width a snippet is STORED at — ``Email.body_snippet`` is
#: ``max_length=500`` and every writer truncates to it. The review key has to be
#: computed from the same text on both sides of a decision or the decision
#: cannot settle the row it was made about, so :func:`review_dedup_key`
#: truncates here too rather than trusting its caller to have done it.
#:
#: Not hypothetical. The pipeline keys on ``PipelineItem.snippet``, which the
#: sync endpoint accepts up to 2000 characters, while the persisted row holds
#: the first 500. A message whose role sits past character 500 was queued under
#: ``(thread, "backend engineer alarms")`` and settled against
#: ``(thread, None)`` — measured 2026-08-22, before this line existed. It would
#: have left the row unlinked and un-reviewed, re-queued on every sync forever.
STORED_SNIPPET_CHARS = 500


def review_dedup_key(
    *,
    message_id: str,
    thread_id: str | None,
    subject: str,
    snippet: str,
    identity_role: str | None = None,
    identity_req_id: str | None = None,
) -> tuple[str, str | None] | str:
    """The unit of ONE DECISION in the review queue — issue #454.

    A Gmail conversation is one decision only when it is about one application,
    and an ATS thread routinely is not: every acknowledgement an employer sends
    goes out under one subject from one no-reply address, and Gmail threads on
    subject plus sender. Measured in the owner's mailbox on 2026-08-22, thread
    ``19ff36237eef1ef3`` holds five Verkada messages naming FOUR different
    roles, and ``19fed820cd93d18e`` holds two Anthropic applications. Keyed on
    the thread alone the queue asked about one of the four and the other three
    reached no card, no entry and no counter.

    So the key is the thread PLUS which application the message names, using the
    same :func:`application_sub_key` the filing path
    (:func:`partition_applications`) has used for months. On the real Verkada
    thread that is four distinct tokens, with the duplicate acknowledgement of
    "Backend Engineer, Alarms" folding back into one: five messages, four
    decisions.

    THE CRUSOE CASE IS THE CONTROL AND IS UNCHANGED. Its two messages ("Crusoe |
    Application Received", emails 58 and 73 of thread ``19fed7e0706ee704``)
    carry no body, so both sub-keys are ``None`` — equal, one entry, one
    decision, exactly as before. Widening the key by identity can never narrow
    this: mail that names no application still collides with every other
    nameless message of its thread.

    NO THREAD, NO WIDENING. Unthreaded mail returns the bare ``message_id``
    rather than a ``(None, sub_key)`` pair, which would collide two different
    employers' "software engineer" mail into one entry. A message id is unique
    and cannot.

    Lives here, and is called from every place that decides how many decisions a
    set of messages is, because those places must not be able to disagree: the
    pipeline that builds the queue, the additive persist that keeps a settled
    conversation out of it, the endpoint that renders it, the classify that
    settles its siblings, and the summary tile that counts it. Four of those
    five said "thread" and the fifth had to as well; a fix at fewer than all
    five is invisible, because the rows exist and the screen still shows one.
    """

    if not thread_id:
        return message_id
    return (
        thread_id,
        identity_or_derive(
            req_id=identity_req_id,
            role=identity_role,
            subject=subject,
            snippet=snippet[:STORED_SNIPPET_CHARS],
        ),
    )


@dataclass(frozen=True)
class MessageRef:
    """A metadata-only reference to one underlying email (for click-through)."""

    message_id: str
    thread_id: str | None
    subject: str
    sender_email: str
    sender_name: str | None
    received_at: datetime | None
    category: str
    confidence: float
    snippet: str = ""
    # Carried so the persisted row keeps the identity the reader derived from
    # the body instead of the persist layer re-deriving a weaker one from the
    # stored snippet. None means "this pass derived nothing" and the writer
    # leaves the stored column alone — the same ratchet ``snippet`` itself uses
    # two fields up.
    identity_role: str | None = None
    identity_req_id: str | None = None
    # The classifier's PROPOSAL for a ref that carries no committed category —
    # i.e. a review item, whose ``category`` is the literal ``"needs_review"``.
    # None on the rolled-application path, where ``category`` already IS the
    # commitment and there is nothing outstanding to propose.
    suggested_category: str | None = None
    # Carried from ``PipelineItem.method`` so the persisted row records which
    # classifier layer actually answered instead of asserting one (#496).
    # ``None`` means the server never saw a classifier run for this message.
    method: str | None = None


@dataclass(frozen=True)
class RolledApplication:
    """ONE application — an employer plus the specific role applied for.

    Not "one company's applications rolled into a single row", which is what
    this used to be and what made four different Amazon requisitions render as
    one card. ``company_token`` alone is no longer an identity; the identity is
    ``(company_token, req_id or role_token)``.
    """

    company_token: str  # normalized match key (e.g. "acme")
    company_display: str  # human display (e.g. "Acme")
    role: str | None  # detected role, or None
    status: str  # ApplicationStatus value
    applied_at: datetime | None  # earliest application date
    last_activity: datetime | None  # most recent relevant date
    messages: tuple[MessageRef, ...] = ()  # contributing mail, newest-first
    # Identity within the employer. ``req_id`` is the employer's own requisition
    # number when it prints one; ``role_token`` is the normalized title. Both are
    # None for an employer that names no role anywhere in its mail (Supabase,
    # Twitch, Together AI in the live corpus) — that degrades to one row, which
    # is the honest floor when the mail genuinely does not distinguish.
    req_id: str | None = None
    role_token: str | None = None
    # The deadline this application's mail STATES, if any. None is the common
    # and correct case — most mail states nothing, and nothing is what it gets.
    due_at: datetime | None = None
    # The evidence behind ``status`` when a rejection is involved, carried so the
    # persistent half can tell a genuine re-application from a rejection the
    # scan's window simply did not reach. ``latest_rejection_at`` is the newest
    # DATED rejection in the cluster (None when there is none, or when the only
    # rejection carries no date); ``latest_applied_signal_at`` is the newest
    # dated applied/pending_application signal. Both are cluster-wide maxima
    # rather than segment-scoped, because ``upsert_applications_for_user`` needs
    # the applied signal even on a cluster whose rejection it never saw.
    latest_rejection_at: datetime | None = None
    latest_applied_signal_at: datetime | None = None


@dataclass(frozen=True)
class ReviewItem:
    """A lifecycle-ish verdict that is too uncertain to auto-file.

    These populate the dashboard's "needs classification" queue: the user can
    confirm a category (which then persists as an application *and* trains the
    model) or dismiss it. Never rendered on the pipeline board as a hard row.
    """

    message_id: str
    thread_id: str | None
    subject: str
    sender_email: str
    sender_name: str | None
    received_at: datetime | None
    category: str  # the tentative lifecycle category
    confidence: float
    company_display: str | None  # best-effort; may be None (unknown employer)
    # Carried so the persisted Email keeps its snippet. It used to be dropped
    # here and hard-coded to "" at the two persist sites, and because the persist
    # path assigns `body_snippet` unconditionally, a message that came back
    # through review had its stored snippet ERASED — taking the role with it,
    # which is the one field per-application identity depends on.
    snippet: str = ""


@dataclass(frozen=True)
class DroppedVerdict:
    """A lifecycle verdict this pipeline threw away, named so it can be counted.

    THE DROP THAT COST A USER FOUR APPLICATIONS. On 2026-08-21 four Microsoft
    confirmations arrived within five minutes of each other. Each scored
    ``rejection`` at 0.60 — below :data:`REVIEW_FLOOR` — because the body
    carries a CONDITIONAL explainer ("if you see the job moved to an inactive
    state, that means ... you were not selected for the role"), and the sender's
    domain is not on ``rules.ATS_DOMAINS`` so the ATS floor did not catch them
    either. All four left by the terminal drop below. They produced no
    application row, no queue entry, no counter and — because the log was gated
    at ``AUTO_FILE_GATE`` and 0.60 is nowhere near it — no log line.

    The user's report was "I applied to 4 new Microsoft and a Google
    application, but when I sync it in the app, I'm not getting anything", and
    from the product's side that is indistinguishable from a quiet mailbox.
    Finding out which it was took a session, a mailbox read and a local
    reproduction, because nothing the running system emitted said "four
    messages that looked like application mail were discarded".

    So the drop is now COUNTED and NAMED. This carries no verdict and changes
    no routing; it exists so ``GET/POST /gmail/sync`` can answer the one
    question the product could not: did we see nothing, or did we throw
    something away?

    The sender's address deliberately does not ride along — it is the user's
    correspondent, and ``message_id`` already names the message. Same reasoning
    as ``_warn_if_capped`` in ``cloud/applications.py``.
    """

    message_id: str
    category: str
    confidence: float


@dataclass(frozen=True)
class ScanLedger:
    """Where every message a scan looked at ENDED UP. One partition, it closes.

    THE QUESTION THIS ANSWERS is the most common one a user asks about this
    product — "did you see my mail?" — and until now neither the response nor
    the database could answer it. On 2026-08-21 four Microsoft confirmations
    were read by a sync and produced no application, no queue entry and no
    ``emails`` row (:class:`DroppedVerdict` is that half, and it is counted
    here as ``dropped``). What was still missing afterwards is the OTHER
    terminal exit: a message the classifier scored ``other``, which leaves
    through the same door and is not counted by anything. From the database
    it is indistinguishable from mail that never arrived.

    THE PARTITION, over the messages that entered the pipeline::

        classified == filed + queued + dropped + reached_nothing

    · ``classified`` — messages that reached :func:`roll_up_applications` and
      :func:`collect_review_items`. NOT the same as the scan's ``scanned``:
      ``scanned - classified`` is everything the run dropped BEFORE an item
      existed. Today that is the user's own sent mail, which
      ``_classify_messages`` skips structurally, plus — since this counts
      DISTINCT ids — any message id a caller relayed twice (``gmail_sync``
      keeps one item per id, first occurrence, so a repeat widens this gap
      instead of landing the same message in two buckets). The partition
      closes over this number and not over ``scanned``, because a message that
      never became an item was never routed anywhere.
    · ``filed`` — landed in a rolled-up application, so it becomes an
      ``emails`` row attached to a card.
    · ``queued`` — routed to the needs-classification queue. This is what the
      SCAN produced, not what the queue then held: the additive merge drops
      refs whose thread is already settled, so ``SyncResponse.needs_review``
      is legitimately smaller. Two numbers, two questions, named apart.
    · ``dropped`` — a lifecycle verdict under :data:`REVIEW_FLOOR`. Counted
      and NAMED per message; see :class:`DroppedVerdict`.
    · ``reached_nothing`` — everything else. It left no row, no queue entry
      and, until this class, no number.

    ``reached_nothing`` IS THE HARNESS'S ``LOST``, WIDENED TO WHAT THE PRODUCT
    CAN ACTUALLY COMPUTE. ``tests/corpus_independent/harness.py`` scores a
    message LOST when it is about a real application and reached nothing, and
    DROPPED when it went under the review floor — "one of these is invisible
    and the other is merely bad". That distinction is reused here verbatim:
    ``dropped`` is the harness's DROPPED. But LOST needs ground truth, and at
    runtime there is none — a newsletter and a missed confirmation both score
    ``other`` and both reach nothing. So this bucket is the SUPERSET the
    product can honestly compute: LOST plus the noise that was correctly
    ignored. It is a haystack with a needle in it sometimes, and the point is
    that until now there was not even a haystack.

    Three shapes land in it, and only the first is a defect:

      · ``other`` — a classifier miss, or ordinary inbox noise. The bulk of it.
      · ``follow_up`` — the user's own chasing mail, excluded by design.
      · a message whose thread was already represented in the queue.
        :func:`collect_review_items` keeps one entry per thread-and-application,
        and the siblings it deduplicates are persisted nowhere, so they really
        did reach nothing. Counting them anywhere else would be a lie.

    COUNTS, NOTHING ELSE. This is a privacy-sensitive product: the message ids
    behind these numbers are used to compute them and are thrown away with the
    sets. A ledger that listed what was ignored would store subjects and
    senders for mail the product decided not to file, which is the one thing
    ``apps/web/app/(app)/privacy/page.tsx`` promises it does not do.
    """

    classified: int
    filed: int
    queued: int
    dropped: int
    reached_nothing: int

    @property
    def closes(self) -> bool:
        """Whether the four buckets account for every classified message."""

        return (
            self.filed + self.queued + self.dropped + self.reached_nothing
            == self.classified
        )


def ledger_for_scan(
    items: Iterable[PipelineItem],
    rolled: Iterable[RolledApplication],
    review: Iterable[ReviewItem],
    dropped: Iterable[DroppedVerdict],
) -> ScanLedger:
    """Partition one scan's messages by where they ended up.

    Derived from the OUTPUTS of the three functions that route a scan, not from
    a re-implementation of their branch conditions. A counter that re-derives a
    routing decision is a second reader of the same shape and drifts from the
    first — the corpus's incremental layer was exactly that mirror and was
    reporting merges the product no longer had.

    ``reached_nothing`` is a SET DIFFERENCE, not a subtraction. Computed as
    ``classified - filed - queued - dropped`` on the ids, it cannot go negative
    and the partition closes by construction rather than by luck.

    WHEN IT CANNOT CLOSE it logs and reports anyway. The buckets are disjoint
    for every shape measured — including the 17,260-message adversarial corpus,
    where they partition it exactly — but a future routing change could make
    one message both filed and queued, and a 500 on the user's sync to defend a
    counter would be a worse bug than the silence this replaces.

    ONE SHAPE ALREADY REACHED IT, and it was fixed at the caller rather than
    here: two relayed items sharing a ``message_id`` under two categories were
    routed twice and landed in two buckets at once. That is a duplicate INPUT,
    not overlapping routing, so ``gmail_sync`` now keeps one item per id and
    this function is left deriving counts from the routing outputs rather than
    correcting them. The hard
    assertion lives in the tests, where a violation is a red build rather than
    a failed sync.
    """

    scanned_ids = {item.message_id for item in items}
    filed_ids = {m.message_id for r in rolled for m in r.messages} & scanned_ids
    queued_ids = {r.message_id for r in review} & scanned_ids
    dropped_ids = {d.message_id for d in dropped} & scanned_ids

    overlap = (
        (filed_ids & queued_ids) | (filed_ids & dropped_ids) | (queued_ids & dropped_ids)
    )
    if overlap:
        logger.warning(
            "Scan ledger buckets overlap on %s message(s); the partition does "
            "not close and the counts below under-report. This is a routing "
            "change, not a counting one: a message reached two of filed / "
            "queued / dropped.",
            len(overlap),
        )

    return ScanLedger(
        classified=len(scanned_ids),
        filed=len(filed_ids),
        queued=len(queued_ids),
        dropped=len(dropped_ids),
        reached_nothing=len(scanned_ids - filed_ids - queued_ids - dropped_ids),
    )


def _rank_to_status(rank: int) -> str:
    """Roll a ``_STAGE_RANK`` value (a mail CATEGORY's rank) up to a status.

    Takes a stage rank, never a status rank — see the note on ``_STATUS_RANK``.
    Since ``assessment`` became a status the mapping is 1:1 with the stage
    ranks (1 applied, 2 assessment, 3 interview→interviewing, 4 offer→offered);
    rank 2 used to fold up into ``interviewing``, which is the fold this change
    removes.
    """

    if rank >= 4:
        return "offered"
    if rank >= 3:
        return "interviewing"
    if rank >= 2:
        return "assessment"
    return "applied"


# A standalone requisition code, in the NORMALIZED token form — lowercased,
# with punctuation already collapsed to spaces, so "JR0093214" arrives as
# "jr0093214" and "R-77120" as "r 77120". Only the unambiguous ATS shapes; the
# labelled patterns in ``_REQ_ID_PATTERNS`` need an explicit "id:" and so
# cannot be confused with a company name in the first place.
_REQ_CODE_TOKEN = re.compile(r"(?:r|jr|req)\s?\d{4,10}")


def _valid_company_token(token: str) -> bool:
    """A token is a usable company only if it is not a stopword, number or req id."""

    if not token or len(token) < 2:
        return False
    words = token.split()
    if all(w in _COMPANY_STOPWORDS for w in words):
        return False
    if words[0] in _COMPANY_STOPWORDS:
        return False
    # A requisition code identifies an APPLICATION, never an employer. Workday
    # and Greenhouse write subjects like "Interview for JR0093214 at <Employer>",
    # which puts the code exactly where ``_SUBJECT_COMPANY`` looks for a company
    # — so the code became the employer, minting a card titled "JR0093214" for a
    # company that does not exist AND splitting it off the real application whose
    # id it was. Rejecting it here lets the resolver fall through to the sender
    # name and the rest of the subject, which do name the employer.
    if _REQ_CODE_TOKEN.fullmatch(token):
        return False
    return re.fullmatch(r"[0-9]+", token) is None


# THE ONE BOUND POSTGRES ENFORCES FOR US, AND ONLY POSTGRES. ``company`` is
# indexed — ``ix_applications_company`` on the raw column, and
# ``ix_applications_user_id_lower_company`` on ``lower(company)`` — and a btree
# version 4 index row may not exceed 2704 bytes. Measured against the schema the
# real migrations build (issue #406):
#
#     company len=2000  -> INSERT OK
#     company len=2700  -> ProgramLimitExceeded: index row size 2712 exceeds
#                          btree version 4 maximum 2704
#     smallest rejected incompressible company: 2677 characters
#     position len=5,000,000 -> INSERT OK      # unindexed, so this is `company`
#
# SQLite has no such limit, which is why the whole backend suite passes with the
# field unbounded and this is invisible on a laptop. The API accepted a
# 5,000,000-character company and answered 201, so the failure landed on the
# INSERT rather than at the door.
#
# WHERE 300 COMES FROM. It is a character count and the ceiling is a byte count,
# so the conversion has to assume the worst: a UTF-8 code point is up to 4
# bytes, making 300 characters at most 1,200 bytes — well under half the 2,704
# available, with the remainder covering the index tuple's own overhead, the
# ``user_id`` in the composite index, and the rare code point whose ``lower()``
# is longer than itself. A registered company name does not approach it; the
# longest in the owner's own board is 34 characters.
_MAX_COMPANY_LEN = MAX_COMPANY_LEN

# Applied as a REFUSAL, never a truncation. A 2,700-character sender display
# name is not a company name that needs shortening; it is a string that does not
# name an employer. Refusing sends the message to the review queue and asks,
# which is what this module already does with every name it cannot resolve.
# Truncating would mint a card titled with 300 characters of somebody's garbage.
#
# It lives HERE rather than in ``cloud.applications`` because that module
# imports this one, so the reverse would cycle — and a second literal ``300`` is
# exactly the drift #581 exists to prevent. ``applications`` re-exports it.


def _clean_company_display(raw: str) -> str:
    r"""Trim a captured company string to a clean human display name.

    Whitespace is canonicalised FIRST rather than last. Every regex below is
    written for a one-line name, and until now nothing enforced that a name is
    one line. Two ways in: the USER-typed path (:func:`employer_from_text`, fed
    by the review-classify body, an unbounded JSON string), and — checked, not
    assumed — the extraction patterns themselves, because
    :data:`_COMPANY_CAPTURE`'s inter-word ``\s+`` matches a newline as happily
    as a space. Only :data:`_SUBJECT_COMPANY`, whose class is ``[\w&.\- ]``,
    cannot produce one.

    Collapsing at the door makes every caller obey the assumption the rest of
    the module already makes, and it is what keeps :data:`_VIA_TAIL` linear.
    It is also a behaviour change on exactly those inputs, deliberately: see
    ``test_company_name_regexes_are_linear.py``, which pins both the old and the
    new answer for a newline-bearing name.
    """

    text = re.sub(r"\s+", " ", raw or "")
    text = _VIA_TAIL.sub("", text).strip()
    # Bounded, like `_clean_sender_display_name`'s loop: "Acme Talent Team"
    # needs two passes and nothing needs four. See :data:`_DISPLAY_TAIL` for
    # why this is anchored and why "Labs" and "Systems" are no longer in it.
    for _ in range(4):
        stripped = _DISPLAY_TAIL.sub("", text).strip(" ,.-&")
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"\s+", " ", text)
    # See :data:`_MAX_COMPANY_LEN`. Every caller already treats "" as "this does
    # not name an employer" and falls through to the next resolution step.
    return "" if len(text) > _MAX_COMPANY_LEN else text


def _employer_from_subject(
    subject: str, ats_relay: bool = False, relay_brand: str = ""
) -> str | None:
    """Return the employer explicitly named in a subject, or None.

    Only trusts language that unambiguously names an employer: application/
    interview/offer "... at/with/to <Company>", "on behalf of <Company>", or a
    bare "at <Company>" — plus, for ATS mail only, a trailing "@ <Company>".
    The capture is cleaned and validated so a fragment like "The" or "Software"
    can never survive.

    ORDER IS THE MEANING, not a performance detail — see :data:`_EMPLOYER_AT_SIGN`.
    A subject that names both a role and a company ("... application **to**
    <Role> @ <Company>") satisfies two patterns at once, and whichever runs
    first decides whether the row files under the employer or under a job title.
    Until #325 the anchored pattern ran first and "your application to Systems
    Research Engineer, GPU Programming @ Together AI" filed as "Research
    Engineer" — "Systems" being eaten by :data:`_CORP_TAIL` on the way out.
    Since #532 the display path no longer strips that word, so the same subject
    would file as "Systems Research Engineer": still a job title, and now at
    least a whole one. The ordering fix is what makes it read the employer.

    Two rules this leaves wrong, stated rather than papered over:

    - A subject naming a role with NO at-sign still yields the role. "Your
      application to Systems Research Engineer" alone returns "Research
      Engineer", because nothing in that line distinguishes it from "Your
      application to Stripe". Deciding it would need a role-vocabulary test, and
      the only place to put one is :func:`_valid_company_token` — which is also
      what the USER-typed company path goes through, so a company whose name
      reads like a title would stop being enterable by hand.
    - The at-sign path does not check whether it just named the RELAY. "Your
      application to Acme @ Greenhouse" resolves to Greenhouse, not Acme.
      Adding :func:`_names_the_relay` here would cost more than it saves: the
      live corpus holds a real Handshake application, and Handshake is in
      ``RELAY_DOMAINS``, so the check would refuse a genuine employer to guard
      against a subject shape nothing has yet sent.
    """

    patterns = (
        _EMPLOYER_INTEREST_IN,
        _EMPLOYER_ANCHORED,
        _EMPLOYER_ON_BEHALF,
        _EMPLOYER_BARE_AT,
    )
    if ats_relay:
        patterns = (_EMPLOYER_AT_SIGN, *patterns)
    # LAST, so this is PURELY ADDITIVE: every subject that resolves today
    # resolves to the same employer, and only a subject that matched nothing at
    # all can reach the new reading. It is also fenced to ASSESSMENT VENDORS and
    # nothing wider — not consumer webmail, where a name is a person, and not the
    # ATS and scheduling relays either, whose mail routinely puts a RECRUITER'S
    # name in exactly this position. See :data:`_EMPLOYER_INVITES`.
    if relay_brand in ASSESSMENT_RELAY_DOMAINS:
        patterns = (*patterns, _EMPLOYER_INVITES)

    for pattern in patterns:
        match = pattern.search(subject or "")
        if not match:
            continue
        raw = match.group(1)
        if pattern is _EMPLOYER_AT_SIGN and _CAPTURE_IS_HOSTNAME.search(raw):
            # An address, not a company. Fall through to the other patterns
            # rather than returning None, so this branch can only ever ADD a
            # resolution — never take one away from the subjects that already
            # resolve without it.
            continue
        display = _clean_company_display(raw)
        token = _normalize_token(display.split(" ")[0]) if display else ""
        if pattern is _EMPLOYER_INVITES and _names_the_relay(token, relay_brand):
            # An assessment vendor writes its own name into this position all
            # the time — "Coderbyte invites you to take an assessment" is the
            # vendor talking about its own product. Refused HERE and not for the
            # other patterns, whose shapes ("application to <X>") name the
            # employer even in relayed mail; #508 is the record of what a blanket
            # relay-vocabulary refusal costs a company that is also a platform.
            continue
        if _valid_company_token(token):
            return display
    return None


def _clean_sender_display_name(raw: str) -> str:
    """Trim an ATS sender display-name down to the employer it fronts.

    Drops a "via Lever" / "(Greenhouse)" relay tail, then strips trailing
    role-ish words repeatedly ("Crusoe Hiring Team" → "Crusoe Hiring" →
    "Crusoe"). Only the TAIL is touched, so a company whose name legitimately
    contains one of those words keeps it.
    """

    text = re.sub(r"\s+", " ", raw or "")  # see _clean_company_display
    text = _VIA_TAIL.sub("", text).strip()
    for _ in range(4):  # bounded: "Acme Talent Acquisition" needs two passes
        stripped = _NAME_ROLE_TAIL.sub("", text).strip(" ,.-&|")
        if stripped == text:
            break
        text = stripped
    cleaned = re.sub(r"\s+", " ", text).strip(" ,.-&|")
    # SEPARATELY BOUNDED, and this is the whole point of #581. Step 3 of
    # `resolve_employer` does not run `_clean_company_display` — the comment at
    # the top of this module says so — so bounding that function alone leaves
    # the sender-display-name door open. Proved by stubbing the other cleaner to
    # refuse everything: a 1,907-character name still resolved, untouched.
    return "" if len(cleaned) > _MAX_COMPANY_LEN else cleaned


def _names_the_relay(token: str, relay_brand: str) -> bool:
    """True when a candidate names the RELAY itself, not the employer behind it.

    "Handshake", "Greenhouse", "Ashby" are the courier, not the company — a row
    built from one of those is exactly the garbage the precision gate exists to
    prevent. Matched both against the known relay vocabulary and against the
    actual sending brand (so "Ashby" is rejected for ``ashbyhq.com``).
    """

    first = token.split(" ")[0] if token else ""
    if not first:
        return True

    # NO RELAY IDENTIFIED — the vocabulary is the only signal left, so use it.
    if not relay_brand:
        return first in RELAY_DOMAINS

    # A RELAY IS KNOWN, so ask the precise question: is this token the name of
    # THAT relay? The vocabulary test this replaced asked whether the token was
    # the name of *any* relay, which is a different question and refuses a real
    # employer whose name happens to be a platform's (#508).
    #
    # Handshake is both. It relays mail for other employers, and it hires. A
    # rejection it sent through ASHBY named it in the sender display name,
    # `_clean_sender_display_name` read "Handshake" correctly, and this function
    # threw it away because "handshake" is in ``RELAY_DOMAINS`` — so a real
    # rejection from a real company sat in the review queue as unattributable.
    # `_employer_from_subject`'s docstring predicted exactly this and applied
    # the reasoning to the wrong function.
    # A SHORT relay name is refused whatever carried the message. The guard
    # below makes containment safe for long tokens, but it leaves short
    # vocabulary entries ("gem", "aol", "dice") reachable when a relay IS known
    # and the prefix tests miss — so "Gem", a real recruiting CRM, resolved as
    # an EMPLOYER through Ashby. Three letters is not enough signal to tell a
    # company from a courier, and the population of employers whose whole name
    # is a three-letter relay brand is smaller than the population of relay mail
    # that names one.
    if len(first) < 4 and first in RELAY_DOMAINS:
        return True

    if relay_brand.startswith(first) or first.startswith(relay_brand):
        return True

    # ...and the containment arm, which is not redundant with the two prefix
    # tests above. A relay's domain brand is not always its brand name:
    # ``joinhandshake.com`` yields ``joinhandshake``, and neither prefix test
    # relates that to ``handshake``, so without this a Handshake-relayed
    # message naming Handshake would now resolve — the exact garbage the
    # precision gate exists to refuse, re-introduced by the fix for its
    # opposite. That case is the control for this change.
    #
    # Length-guarded because containment is a weak test: a two- or three-letter
    # employer token is a substring of far too many words to mean anything, and
    # ``RELAY_DOMAINS`` holds short entries ("gem", "aol", "dice") that the
    # prefix tests above already catch exactly.
    return len(first) >= 4 and first in relay_brand


#: Words that are corporate BY CONSTRUCTION and are effectively never surnames.
#:
#: Deliberately short and deliberately boring. This is positive evidence, so the
#: two failure directions are not symmetric: a word MISSING from here costs one
#: queue row that a person confirms, while a word wrongly IN here mints a card
#: named after a human being. Surname-shaped words are therefore excluded even
#: when they are common in company names — Banks, Rivers, Woods, Fields, Stone,
#: Marsh, Wells — because each is somebody's last name.
_CORPORATE_WORDS = frozenset(
    """
    inc llc ltd limited corp corporation gmbh plc pte pty srl spa ag nv bv sarl
    labs laboratories technologies technology systems solutions software
    analytics robotics networks semiconductors instruments dynamics sciences
    therapeutics biosciences pharmaceuticals diagnostics
    ventures holdings industries enterprises logistics consulting advisory
    associates group partners collective foundation institute university college
    studios interactive digital cloud data platform platforms
    """.split()
)

#: An acronym-ish token: "AI", "IBM", "NVIDIA", "3M". Two or more characters and
#: no lowercase, which a name in ordinary Title Case never satisfies.
_ACRONYM_TOKEN = re.compile(r"^[A-Z0-9][A-Z0-9&.\-]+$")


def _carries_corporate_evidence(
    display: str, raw: str, *, one_word_is_evidence: bool = True
) -> bool:
    """Does this display name give a POSITIVE reason to read it as a company?

    #733. `_employer_from_sender_name` and `_employer_from_subject_segment`
    both mint a filing-grade employer out of whatever Title-Case run they find,
    and neither had any notion that a run of Title-Case words might be a person.
    Measured on the shipped code, an interview invite through Greenhouse whose
    display name was a recruiter filed a board card named after that recruiter,
    at 0.95 — above the auto-file gate — and a second message keyed to the same
    person GROUPED onto it.

    The fix is not a lexicon of first names. It cannot separate "Path Robotics"
    from "Sarah Chen", and every miss would be open-ended. It is the estate's
    display-grade / filing-grade split: a bare run of Title-Case words with
    nothing corporate about it is not evidence strong enough to name a card, so
    it goes to the queue with the name pre-filled instead.

    Evidence, any one of which is enough:

    - a role or relay tail was stripped on the way here ("Crusoe Hiring Team"),
      which only ATS-shaped company mail carries;
    - an ``@`` — but ONLY at the subject site. At the sender-name site this
      branch cannot fire: ``_employer_from_sender_name`` has already split on
      the ``@`` and passes the bare tail, so by the time the guard sees it
      there is no ``@`` left. ``Team Talent @ MotherDuck`` survives because
      ``MotherDuck`` is ONE WORD, not because of the ``@``, and
      ``Team Talent @ Basalt Row`` is refused. Kept because the subject site
      can still carry one, and said out loud because the previous version of
      this docstring credited the wrong rule for the case it named;
    - an acronym or numeric token ("Netic AI", "IBM", "3M", "H&R");
    - a legal or corporate-suffix word (:data:`_CORPORATE_WORDS`);
    - a single word — "Stripe", "Northwind" — but ONLY when the message itself
      offered that word, which is what ``one_word_is_evidence`` carries. A
      person's full name is not one word, and a one-word display is the
      overwhelmingly common company shape.

    The cost, stated rather than hidden: a two-word plain-name employer whose
    mail also carries an employer-less subject ("Hugging Face", "Jane Street")
    now costs one confirmation click. That is the price of never publishing a
    person's name as a company, and it is the right way round.

    ``one_word_is_evidence`` IS #539, AND IT IS ABOUT PROVENANCE, NOT LENGTH.
    The one-word rule reads a shape somebody else chose; it says nothing about
    a fragment this module cut for itself. :func:`_lead_segment_candidates`
    offers a second reading of a leading segment — the run CUT at its first
    lifecycle word — and "Phone Interview" was correctly refused here for want
    of evidence while the cut it left behind, "Phone", walked through the
    one-word door and filed a company called Phone. Three ordinary ATS
    subjects did it at HEAD: "Phone Interview - <Employer>", "Final Interview
    Details | <Employer>" and "Important Update | <Employer>".

    So a caller looking at a reading it has ALREADY turned down passes False,
    and the word must earn evidence some other way. "IBM Interview" still
    does, through :data:`_ACRONYM_TOKEN` — which is why the test below falls
    THROUGH to the word loop instead of returning False, and why a one-word
    acronym or a name with a digit in it costs nothing here.

    This is not the word-count cap the comment further down records as
    deliberately removed. That cap was an UPPER bound on the person-shape
    test — an escape hatch at the shape being tested. This conditions the
    LOWER-bound exemption on where the string came from, and it can only
    refuse.

    Its cost, stated rather than hidden, in the same voice as the one above:
    "<Employer> <Lifecycle> | <Candidate>" with a ONE-WORD employer and
    nothing after the lifecycle word — "Larkspur Interview | <Candidate>" —
    resolved to Larkspur and now resolves to nothing, because that word is
    the second reading of its segment and the first was refused. The row goes
    to the review queue instead of onto the board under a name that may be an
    adjective.
    """

    if raw and _clean_sender_display_name(raw) != raw.strip():
        return True                       # a tail was stripped: ATS company mail
    if "@" in display or "&" in display:
        return True
    words = display.split()
    if len(words) < 2 and one_word_is_evidence:
        return True                       # one word is a company, not a full name
    # NO UPPER BOUND ON WORD COUNT, AND THERE USED TO BE ONE. `len(words) > 4`
    # was here as "too long to be a name", which is false in exactly the
    # direction that matters: `Mary Anne Van Der Berg` is five words and filed
    # as a company through the hole. A cap on a person-shape test is an escape
    # hatch AT the shape being tested. A long company name with no corporate
    # word in it now costs a confirmation click, which is the same price every
    # other bare name pays.
    for word in words:
        if _ACRONYM_TOKEN.match(word):
            return True
        if _normalize_token(word) in _CORPORATE_WORDS:
            return True
        if any(ch.isdigit() for ch in word):
            return True
    return False


def _employer_from_sender_name(
    sender_name: str | None, relay_brand: str
) -> tuple[str, str] | None:
    """Employer named by an ATS sender's DISPLAY NAME, or None.

    Handles the two shapes ATS mail actually uses:
      - ``"Crusoe Hiring Team"`` → ``Crusoe`` (role-ish tail stripped)
      - ``"Team Talent @ MotherDuck"`` → ``MotherDuck`` (company after the ``@``)
    """

    raw = (sender_name or "").strip().strip('"')
    if not raw or _NAME_IS_ADDRESS.match(raw):
        return None

    candidates: list[str] = []
    if "@" in raw:
        head, tail = raw.rsplit("@", 1)
        head, tail = head.strip(), tail.strip()
        # A dot in the tail means it is a hostname ("…@ashbyhq.com"), not a name.
        if tail and "." not in tail:
            # ATS display names use the ``@`` in BOTH directions, and which
            # side holds the employer depends on which side names the relay:
            #
            #   "Team Talent @ MotherDuck"   -> employer is the TAIL
            #   "Medpace, Inc. @ icims"      -> employer is the HEAD
            #
            # The tail was already being rejected in the second shape — it
            # names the relay, so ``_names_the_relay`` refuses it — but the
            # fallback then took the WHOLE raw string, and the employer kept a
            # courier's name glued to it. That is the real board's
            # ``Medpace, Inc. @ icims`` card, which groups and sorts as a
            # different employer from the same company reached through any
            # other ATS.
            #
            # Reading the head in that case is not a special case for icims: it
            # is the same question asked of the other side. Whichever side names
            # the relay is the one carrying no employer information.
            if _names_the_relay(_normalize_token(tail.split(" ")[0]), relay_brand):
                if head:
                    candidates.append(head)
            else:
                candidates.append(tail)
    candidates.append(raw)

    for candidate in candidates:
        display = _clean_sender_display_name(candidate)
        if not display:
            continue
        token = _normalize_token(display.split(" ")[0])
        if not _valid_company_token(token) or _names_the_relay(token, relay_brand):
            continue
        # #733: a bare personal name is not filing-grade evidence of an employer.
        if not _carries_corporate_evidence(display, candidate):
            continue
        return token, display
    return None


def _lead_segment_candidates(subject: str) -> list[str]:
    """Company-shaped readings of an ATS subject's LEADING SEGMENT, best first.

    The segment is everything before the first ``|`` or spaced dash. A subject
    with no such delimiter yields nothing at all, deliberately — see
    :data:`_EMPLOYER_LEAD_SEGMENT` for why that restriction is the whole safety
    of this branch.

    Two readings, in order:

    1. the leading Title-Case run of the segment, which is the shape this rule
       has always read ("Crusoe | Application Received");
    2. that run CUT AT ITS FIRST LIFECYCLE WORD, which is #512's gap 2 —
       Greenhouse's "<Employer> Follow-Up for <Role> | <Candidate>", where the
       run reaches "Anthropic Follow-Up" and the employer is the part in front
       of the lifecycle word.

    Both are offered because the cut is not always right: "Northwind Labs" is a
    company and "Northwind Labs Application" is that company plus a lifecycle
    word, while "Crusoe" needs no cut at all. The caller validates each in turn
    and takes the first that survives.
    """

    parts = _SEGMENT_DELIMITER.split(subject, 1)
    if len(parts) < 2:
        return []
    segment = parts[0].strip()
    run_match = _LEADING_RUN.match(segment)
    if not run_match:
        return []
    run = run_match.group(1)
    remainder = segment[run_match.end() :].strip()

    words = run.split()
    cut = None
    for index, word in enumerate(words):
        if index and _LIFECYCLE_WORD.match(word):
            cut = " ".join(words[:index])
            break

    # THE RUN MUST ACCOUNT FOR THE WHOLE SEGMENT, or the part of the segment it
    # does not account for must be introduced by a lifecycle word it DOES.
    #
    # This is the requirement the rule has always carried and the one that was
    # lost when the candidate list was extracted out of `_EMPLOYER_LEAD_SEGMENT`:
    # matching a leading run and discarding the rest of the segment turns the
    # test into "the segment begins with a capital letter", which ordinary ATS
    # subjects satisfy constantly. Measured against the mail this actually reads,
    # dropping the requirement minted
    #
    #     "Invitation to interview | Acme"            -> Invitation
    #     "Decision on your application | Acme"       -> Decision
    #     "Sorry for the delay in getting back to you | Acme" -> Sorry
    #     "Sarah Chen from Acme - quick chat?"        -> Sarah Chen
    #     "Congratulations Ayush on your application | Acme" -> Congratulations Ayush
    #
    # every one of them at 0.95 on the AUTO-FILE path, i.e. a card on the board
    # under a name nobody chose. All five resolved to nothing before the rule was
    # widened, and resolve to nothing again with the requirement restored.
    #
    # A trailing remainder is therefore only forgiven when the run itself ends in
    # a lifecycle word, which is what makes the remainder that word's object
    # rather than unrelated prose: "Anthropic Follow-Up | for <Role>" is the
    # reported shape and keeps working, while "Invitation | to interview" has no
    # lifecycle word AFTER a company-shaped prefix and so offers nothing.
    if remainder:
        # A LEGAL SUFFIX IS PART OF THE COMPANY, not a remainder. "Salesforce,
        # Inc. | Application Received" splits its run at the comma, and refusing
        # it would drop an employer the rule reads correctly today.
        if not _CORP_TAIL.sub("", remainder).strip(" ,.&-"):
            return [run] if cut is None else [run, cut]
        # Otherwise the remainder is only forgiven when it is the LIFECYCLE
        # WORD'S OBJECT, and "for" is what marks it as one. Without that test
        # any preposition will do, and "Quick Update | from Sarah" resolves to
        # the company "Quick" — the run ends in a lifecycle word, so the cut is
        # offered, and "Quick" passes every downstream guard.
        #
        # The distinction is the whole point of the rule: "Anthropic Follow-Up
        # FOR <Role>" is a follow-up ABOUT a job at a named employer, while
        # "Quick Update FROM Sarah" is prose that happens to open with a capital.
        if cut and _LIFECYCLE_OBJECT.match(remainder):
            return [cut]
        return []
    return [run] if cut is None else [run, cut]


#: THE ROLE LIVES IN THE SAME SEGMENT THE EMPLOYER WAS READ OUT OF (#553).
#:
#: A shape must be recognised once, not twice. :func:`_lead_segment_candidates`
#: already establishes that "<Employer> Follow-Up for <Role> | <Candidate>" is
#: this subject's shape and where its boundaries are, and then reads only the
#: half in front of the lifecycle word. The half behind it — the job the message
#: is about — was thrown away, on the same message, in the same request.
#:
#: That cost more than a missing title. ``identity_parts`` returning
#: ``(None, None)`` is what sends the resolver into ``_pick_application``'s rule
#: 4, the tie-break that files onto the employer's OLDEST row; so an unread role
#: here downgraded the filing decision as well as blanking the card.
#:
#: THREE THINGS ARE REQUIRED, and every one of them is a refusal that the
#: reported shape happens to satisfy rather than a rule written to fit it:
#:
#: 1. the leading run must END in a lifecycle word, with something in front of
#:    it. That is what makes the remainder that word's object rather than
#:    unrelated prose — the same test #537 had to restore for the employer half
#:    after #525 minted companies named "Invitation", "Decision" and "Sorry";
#: 2. the remainder must be INTRODUCED by "for"/"regarding"/"re"
#:    (:data:`_LIFECYCLE_OBJECT`). "Quick Update from Sarah | …" offers nothing;
#: 3. what is left must be TITLE-SHAPED and must contain a title's head noun.
#:
#: AND THE SEGMENT IS BOUNDED BY A PIPE, NEVER BY A DASH. This reads the role,
#: not the employer, and the two need different boundaries even though they are
#: read out of the same subject. ``_SEGMENT_DELIMITER`` accepts a spaced dash,
#: which is correct for the employer — it sits in front of the delimiter, and no
#: company name in this mail carries one — but a JOB TITLE routinely does:
#:
#:     "…Follow-Up for Software Engineer, Agentic AI Harness & Quality - Talonflow | <name>"
#:     "…Follow-Up for Software Development Engineer I - AI/ML Network Infrastructure, … | <name>"
#:
#: Splitting those at the dash yields "Software Engineer, Agentic AI Harness &
#: Quality" and "Software Development Engineer I" — clean enough to look right on
#: a card and wrong enough to split the identity, which is the exact failure this
#: module's role captures are built to avoid. Both shipped in a first draft and
#: the independent corpus caught them: 38 of 40 right is a REGRESSION here, not a
#: partial win, because the two wrong ones minted a rival card for an application
#: the board already tracked.
#:
#: Measured over the 17,260-case corpus, against ground truth:
#:
#:     any delimiter   40 fire, 38 exact, 2 WRONG
#:     pipe only       40 fire, 40 exact, 0 wrong
#:
#: The pipe costs nothing on this corpus — every subject of this shape has one —
#: and a dash-delimited subject now reads no role and goes to the review queue,
#: where a person decides. That is the direction this file takes everywhere else.
#:
#: The head noun is the load-bearing one. Shape alone accepts "Acme Interview
#: for Tomorrow | <Candidate>" and "Acme Follow-Up for Tuesday | <Candidate>",
#: both of which are Title-Case single words in exactly the reported position,
#: and a wrong role is strictly worse than a blank one: the token is half an
#: application's identity, so it becomes the card's title AND captures that
#: application's future mail. :data:`_ROLE_HEAD_NOUNS` is the set the employer
#: half already uses to tell a role from a company, so the two halves of this
#: subject cannot end up disagreeing about what a job title looks like.
#:
#: What it costs is a real title whose head noun is not in that set — "Chief of
#: Staff", "Product Owner", "Scrum Master" read as nothing here. That is the
#: safe direction and the one this module takes everywhere else: the row goes to
#: the review queue, where a person decides, instead of onto the board under a
#: title nobody chose.
_TITLE_SHAPED = re.compile(r"^" + _ROLE_SPAN + r"$")


def _role_from_lead_segment(subject: str) -> str | None:
    """The job title named INSIDE an ATS subject's leading segment, or None.

    ``"Northwind Follow-Up for Backend Engineer | <Candidate>"`` -> ``Backend
    Engineer``. Reads the same segment :func:`_lead_segment_candidates` reads the
    employer from, but bounded only by a ``|`` — see the note above for the two
    real titles a spaced dash truncated.
    """

    parts = (subject or "").split("|", 1)
    if len(parts) < 2:
        return None
    segment = parts[0].strip()
    run_match = _LEADING_RUN.match(segment)
    if not run_match:
        return None

    # (1) An employer-shaped prefix, then the lifecycle word. ``[:-1]`` is not
    # enough on its own — a run that IS a lifecycle word ("Interview | for
    # Backend Engineer") has no employer in front of it and names no company,
    # so there is nothing here to attach a role to.
    words = run_match.group(1).split()
    if len(words) < 2 or not _LIFECYCLE_WORD.match(words[-1]):
        return None

    # (2) …and the remainder is that word's object, not prose that follows it.
    remainder = segment[run_match.end() :].strip()
    intro = _LIFECYCLE_OBJECT.match(remainder)
    if not intro:
        return None

    role = _clean_role(remainder[intro.end() :])
    if role is None:
        return None

    # (3) Title-shaped, and it names a job rather than a day of the week.
    if not _TITLE_SHAPED.match(role):
        return None
    if not any(_normalize_token(w) in _ROLE_HEAD_NOUNS for w in role.split()):
        return None
    return role


#: A TRAILING PARENTHETICAL ON THE LAST SEGMENT IS THE POSTING'S LOCATION.
#:
#: "<Role> - <Employer> (Remote)" — the parenthetical belongs to the employer
#: half of the segment, not to the title, so it is removed BEFORE the segment is
#: cut. Only one, only at the very end, and it may not nest, so a title that
#: carries its own parenthetical cohort ("Software Engineer I (New Grad) -
#: <Employer>") keeps it: that paren is not at the end of the segment.
_TRAILING_SEGMENT_PAREN = re.compile(r"\s*\([^()]{0,80}\)\s*$")

#: The spaced dash that separates the title from the employer echo. Every
#: occurrence is found and the LAST one is used — see
#: :func:`_role_from_trailing_segment` for why the last, and #553 for the two
#: real titles the first one truncated.
_SPACED_DASH = re.compile(r"\s[-–—]\s")

#: How a posting says WHERE the job is worked, not WHAT the job is.
#:
#: Scanned over the POST-HEAD REGION (see :func:`_post_head_region`), not over
#: the last word, and that placement is the whole of its precision. A work
#: arrangement standing to the LEFT of the title's head noun is a modifier of
#: the job — "Remote Infrastructure Engineer", "Hybrid Cloud Architect" are real
#: titles and both survive, because the region this set is scanned in is empty
#: for them. Standing to the RIGHT of the head noun it is the ATS's own
#: annotation of the posting.
#:
#: IT IS NOT SUBSUMED BY THE STRUCTURAL RULE, which is the only reason it still
#: exists. :func:`_post_head_is_introduced` licenses a comma- or dash-introduced
#: continuation, because that is what the reported title is made of — and an
#: arrangement hides there perfectly: "Software Engineer, Distributed Systems
#: Platform, New Grad Remote" is comma-introduced, structurally indistinguishable
#: from the real title, and mints a second ``role_token`` for the job the
#: bracketed placement already filed. So structure closes the space-joined
#: position and this set closes the introduced one.
#:
#: WHAT IT DOES NOT CLOSE, stated rather than implied: place names are an open
#: set, so "Software Engineer, San Francisco" still resolves where "Software
#: Engineer - <Employer> (San Francisco)" resolves to the shorter title. Nothing
#: this module would accept tells ", San Francisco" from ", Distributed Systems
#: Platform" without world knowledge; the residual is pinned as a strict xfail
#: in the acceptance suite rather than hidden.
#:
#: The cost is recall, in the direction this module always fails: "Software
#: Engineer, Remote Sensing Systems" and "Analyst, Virtual Reality" go to the
#: review queue. A person types the title once; nothing is minted wrongly.
#:
#: Normalised through :func:`_normalize_token` over the whole region, so every
#: member is reachable in both of its spellings — "On-Site" (the hyphen folds to
#: a space) and "On Site" (two words) are one entry, and so are "In-Office" and
#: "In Office". Under the last-word probe this replaces, the two-word members
#: could only ever be reached through the hyphen fold.
_WORK_ARRANGEMENT_WORDS: frozenset[str] = frozenset(
    {"remote", "hybrid", "onsite", "on site", "in office", "virtual", "telecommute"}
)

#: The nine lifecycle stems, matched ANYWHERE in a region rather than as a whole
#: word on its own. :data:`_LIFECYCLE_WORD` anchors the same stems with ``^…$``
#: and is what the three older callers want; this one is what the post-head scan
#: wants, because a lifecycle phrase is not one word — "Interview Invitation",
#: "Offer Letter" and "Final Interview" all carry their stem in a position an
#: anchored test cannot see. Appending a noun to a lifecycle word is what
#: revived the refusal this replaces.
#:
#: The set is NOT widened to reach them. "Invitation", "Letter", "Reminder",
#: "Alert", "Event" and "Newsletter" are refused STRUCTURALLY by
#: :func:`_post_head_is_introduced` — they are bare space-joined words standing
#: right of the head noun. These nine earn their keep only on the INTRODUCED
#: shapes structure licenses: "Software Engineer, Final Interview".
_LIFECYCLE_IN_REGION = re.compile(
    r"\b" + _SUBJECT_LIFECYCLE_TAIL + r"\b", re.IGNORECASE
)

#: A whitespace-delimited token, kept with its offsets. The head-noun membership
#: test is written on ``role.split()`` in :func:`_role_from_lead_segment`, and
#: :func:`_last_head_noun_end` has to give the same answers as that line while
#: also saying WHERE the noun ended — so it walks the same tokens rather than a
#: different tokenisation that would drift from it.
_WHITESPACE_TOKEN = re.compile(r"\S+")

#: Trailing punctuation on such a token. "Engineer," is the head noun plus the
#: comma that introduces the title's next segment, and the region begins at the
#: comma, not after it: losing it would turn an introduced continuation into a
#: bare one and refuse the reported title itself.
_TOKEN_TAIL_PUNCT = re.compile(r"[^A-Za-z0-9]+$")

#: What may join a title's own continuation to it — the same characters
#: :data:`_ROLE_JOIN` accepts, which is the point: a continuation this module
#: already agreed was part of the title is INTRODUCED, and a space is not an
#: introduction.
_ROLE_CONTINUATION_MARKS = ",/&\u2010\u2011\u2012\u2013\u2014\u2015-"

#: A seniority level, which continues a title without being introduced:
#: "Software Engineer II", "Analyst 3", "Engineer L4". Roman numerals are
#: enumerated rather than written as a character class, because ``[IVXLC]+``
#: also matches "CIVIL".
_ROLE_LEVEL = re.compile(r"^(?:I{1,3}|IV|VI{0,3}|IX|X|[0-9]{1,2}|[A-Z][0-9]{1,2})$")

#: The lowercase connectives :data:`_ROLE_SPAN` already allows INSIDE a title.
#: Case-sensitive, exactly as ``_TITLE_SHAPED`` uses them: "Engineer in Test",
#: "Director of Engineering". They introduce the word that follows them, so that
#: word is title material and not an ATS annotation.
_ROLE_INNER_ONLY = re.compile(r"^" + _ROLE_INNER + r"$")


def _last_head_noun_end(role: str) -> int | None:
    """Where the LAST :data:`_ROLE_HEAD_NOUNS` word ends, or None if there is none.

    ``None`` is exactly the condition
    ``not any(_normalize_token(w) in _ROLE_HEAD_NOUNS for w in role.split())``
    tests — the same tokens, the same membership, so this function REPLACES that
    line rather than sitting behind it. Two tests of one thing is two answers
    waiting to disagree, and :func:`_post_head_region` needs the offset anyway.

    The offset stops at the token's last alphanumeric character, so the comma in
    "Software Engineer, Distributed Systems Platform" belongs to the region that
    FOLLOWS the noun. That comma is what tells the reported title from
    "Engineering Manager Interview", so dropping it would refuse the bug this
    reader was written for.
    """

    end: int | None = None
    for match in _WHITESPACE_TOKEN.finditer(role):
        token = match.group(0)
        if _normalize_token(token) in _ROLE_HEAD_NOUNS:
            end = match.start() + len(_TOKEN_TAIL_PUNCT.sub("", token))
    return end


def _post_head_region(role: str, head_end: int) -> str:
    """Everything a candidate says AFTER its last title head noun.

    English compounds are right-headed, so this region is where a phrase stops
    being a job title and starts being something else: what the mail is about
    ("Engineering Manager INTERVIEW"), where the job is worked ("Software
    Engineer REMOTE"), or what the ATS is sending ("Engineer NEWSLETTER"). To
    the LEFT of the head noun the same words are ordinary modifiers —
    "Applications Engineer" and "Remote Infrastructure Engineer" are real titles
    — which is why nothing in this reader scans the whole string.
    """

    return role[head_end:]


def _word_end(region: str, start: int) -> int:
    """Where the word beginning at ``start`` ends.

    A word ends at whitespace OR at a continuation mark, and the second half is
    load-bearing: "Software Engineer I, Entry-Level" is a real posted title, and
    a run that stopped only at whitespace read its level token as ``"I,"`` —
    which is not a level, so the title was refused for having a comma in it.
    Caught by the test written for the guard above it, which is the only reason
    it is not still here.
    """

    pos = start
    while (
        pos < len(region)
        and not region[pos].isspace()
        and region[pos] not in _ROLE_CONTINUATION_MARKS
    ):
        pos += 1
    return pos


def _post_head_is_introduced(region: str) -> bool:
    """Is everything in the post-head region INTRODUCED, rather than space-joined?

    THE POSITIVE RULE THAT REPLACES A STOP-WORD LIST. A title's own continuation
    announces itself — by a comma, a slash, an ampersand, a dash, a level token
    or one of :data:`_ROLE_INNER`'s connectives:

    * ``Software Engineer, Distributed Systems Platform, New Grad`` — comma;
    * ``Software Engineer - Storage`` — dash;
    * ``Software Engineer II`` — a level;
    * ``Engineer in Test``, ``Director of Engineering`` — a connective.

    A lifecycle tail, a location or a mailing type does not: it is simply
    space-joined onto the right of the head noun, because it is a new word about
    a different subject. ``Engineering Manager Interview``,
    ``Senior Engineer Hiring Event``, ``Engineer Newsletter``,
    ``Software Engineer Job Alert``, ``Software Engineer New York`` and
    ``Software Engineer WFH`` are all refused by this one rule, with no
    vocabulary of any kind — which is what makes appending a word powerless
    against it. The last-word probe this replaces was revived by exactly that:
    "Engineering Manager Interview" refused and "Engineering Manager Interview
    Invitation" did not.

    ITS GAPS FAIL CLOSED, and that is why a rule of this shape is defensible
    here where a stop-word list is not. A suffix nobody has thought of yet is
    space-joined, so it refuses; a stop-word list's gaps ship a wrong title.

    A PARENTHETICAL IS NOT LISTED as an introduction, deliberately.
    :data:`_ROLE_SPAN` only ever admits one at the very END of the span, and
    :func:`_role_from_trailing_segment` refuses any candidate carrying one
    before it reaches here — so a paren branch in this function could never
    execute, and a branch that cannot fire is indistinguishable from one that
    does not exist.
    """

    pos = 0
    size = len(region)
    while True:
        separator_start = pos
        while pos < size and (
            region[pos].isspace() or region[pos] in _ROLE_CONTINUATION_MARKS
        ):
            pos += 1
        separator = region[separator_start:pos]
        if pos >= size:
            # Nothing (more) follows the head noun: the title ended on its head,
            # which is what "Applications Engineer" and "Platform Engineer" do.
            return True
        if any(mark in separator for mark in _ROLE_CONTINUATION_MARKS):
            # Introduced. Everything from here is the title's own continuation —
            # "Distributed Systems Platform, New Grad" is space-joined INSIDE a
            # comma-introduced segment and must stay legal, which is why this
            # accepts the remainder rather than continuing the walk.
            return True
        word_start = pos
        pos = _word_end(region, pos)
        word = region[word_start:pos]
        if _ROLE_LEVEL.match(word):
            continue
        if _ROLE_INNER_ONLY.match(word):
            # A connective introduces exactly the word after it. "Engineer in
            # Test Remote" therefore still refuses on "Remote".
            while pos < size and region[pos].isspace():
                pos += 1
            object_start = pos
            pos = _word_end(region, pos)
            if pos == object_start:
                # A dangling connective is prose, not a title.
                return False
            continue
        # A bare space-joined word standing right of the head noun.
        return False


def _role_from_trailing_segment(subject: str) -> str | None:
    """The job title named in an ATS subject's TRAILING segment, or None.

    ``"<Employer> | <Boilerplate> | <Role> - <Employer> (<Location>)"`` — the
    shape where the employer BRACKETS the subject, opening the first segment and
    closing the last, and the title sits between the two. Reported as #626, where
    a seven-word two-comma title filed as a blank role because every other reader
    in this module declines it:

    * ``_ROLE_PATTERNS[2]`` and ``[3]`` are ``^``-anchored and two segments sit
      in front of the title;
    * their capture class excludes the comma this title has two of, and their
      ``{0,4}`` caps a title at five words;
    * the body of this ATS template says "this role" throughout and never names
      the title, so :data:`_ROLE_BODY_PATTERNS` cannot rescue it either.

    The subject is the only place the title exists, and it went to the review
    queue with ``identity_role = ''``.

    THE EMPLOYER ECHO IS WHAT LICENSES THE DASH, and that is the whole safety of
    this reader. #553 measured what happens when a spaced dash is assumed to
    separate a role from an employer: it truncated "Software Engineer, Agentic AI
    Harness & Quality" and "Software Development Engineer I - AI/ML Network
    Infrastructure" — clean enough to look right on a card and wrong enough to
    split the identity, which mints a rival card for a job the board already
    tracks. So the dash terminates the title here ONLY when what follows it is
    the company the subject's LEADING segment already named. The segment is cut
    at the LAST spaced dash for the same reason: an interior dash stays inside
    the title, so "<Role> - <Subteam> - <Employer>" yields "<Role> - <Subteam>".

    The lead employer is re-derived through :func:`_lead_segment_candidates`,
    which is the reading the employer half of this subject is filed under, so
    the two halves cannot disagree about who sent the mail. An echo that names a
    DIFFERENT company, or no echo at all, refuses and this reader returns None.

    IT DOES NOT "FAIL CLOSED", AND THE SENTENCE THAT SAID SO WAS FALSE (#657).
    It read "the message goes to the review queue, where a person decides.
    Fails closed, the direction this module takes everywhere." Nothing routes a
    message to the queue for naming no role. :func:`collect_review_items` skips
    anything :func:`_qualifies_for_hard_row` accepts, and that function asks
    only about confidence and an employer — never whether a title was read. So
    a refusal here at or above :data:`AUTO_FILE_GATE` files a card with a BLANK
    role and asks nobody. Measured on the shape #657 reports: confidence 0.95,
    ``_qualifies_for_hard_row`` true, ``collect_review_items`` returns zero.

    THE ONE CARVE-OUT DOES NOT CARRY THIS CASE, which is what makes the false
    sentence wider than it looks. ``unplaceable_message_ids`` re-admits
    role-less mail at an employer already holding SEVERAL applications, because
    there is no single row to pick — but it promotes what cannot be PLACED, and
    since #641 an identity-less CONFIRMATION at such an employer is placeable:
    it mints its own card. So a confirmation whose role this reader refused is
    silent at a one-application employer AND at a multi-card one.

    What DOES reach a person that way is an update, and the set is all five
    non-``applied`` lifecycle categories: ``rejection``, ``interview``,
    ``assessment``, ``offer`` and ``pending_application``. This sentence said
    "a rejection, interview or assessment" and the word before it was "only" —
    an enumeration that named three of five while claiming to be exhaustive,
    which is the same defect as the one this paragraph exists to correct.
    ``follow_up`` and ``other`` are a third state again: outside
    ``JOB_LIFECYCLE_CATEGORIES``, so they neither file nor queue, and the
    pipeline logs them as dropped. The mail this reader exists for is a
    confirmation, so none of that reaches it.

    This reader is a title reader that declines rather than guesses, which is
    right — #553 measured what a guess costs. Calling that "fails closed"
    described a safety net that does not exist, and a future reader would have
    relied on it. Whether a blank role SHOULD gate a review is #657's open
    half and a product decision; it is pinned as it stands in
    ``test_the_trailing_segment_names_the_role.py`` so the answer cannot drift
    without a test going red.

    The candidate then passes the three guards :func:`_role_from_lead_segment`
    uses (:func:`_clean_role`, :data:`_TITLE_SHAPED`, a title head noun) plus
    the ones the trailing position needs and the others do not.

    RIGHT-EDGE HYGIENE, in three rules, none of them a probe of the last word.
    The first version of this reader tested ``role.split()[-1]`` twice, and an
    independent cross-check measured what that costs: 26 of 48 adversarial
    subjects in this shape came back with a title nobody would want on a card,
    every one of them by putting a second word on the right of the one being
    probed.

    1. ANY PARENTHETICAL ON THE ROLE SIDE REFUSES, through the same
       :data:`_TRAILING_SEGMENT_PAREN` the tail side strips with, so the two
       sides cannot drift. Structural, not lexical, and that is the whole
       argument: a work-arrangement VOCABULARY on the role side would still fail
       open on every place name nobody listed, because the tail-side strip is
       unconditional — "<Role> (Bengaluru) - <Employer>" and "<Role> -
       <Employer> (Bengaluru)" would mint two tokens for one job. STRIPPING
       instead of refusing is worse than the split: "Software Engineer
       (Platform)" and "Software Engineer (Security)" at one employer would
       collapse onto one token and begin capturing each other's mail. Refusing
       can only ever cost recall, and the queue is where recall goes.

       It is tested AFTER :func:`_clean_role`, which is load-bearing: that
       function deletes a requisition-id parenthetical, and the tail-side strip
       deletes it too, so "Software Engineer II (Req ID: …)" converges on one
       token from both placements and keeps resolving.

    2. THE POST-HEAD REGION MUST BE INTRODUCED
       (:func:`_post_head_is_introduced`). A lifecycle tail, a location or a
       mailing type stands space-joined to the right of the title's head noun;
       a title's own continuation is introduced by a comma, a dash, a level or a
       connective. This is a positive rule about shape and no list can be
       appended past it.

    3. THE WHOLE POST-HEAD REGION IS SCANNED for the nine
       :data:`_SUBJECT_LIFECYCLE_TAIL` stems and for
       :data:`_WORK_ARRANGEMENT_WORDS` — not the last word, and not the whole
       candidate. Rule 2 licenses "Software Engineer, <anything>", so these two
       scans are what refuse "Software Engineer, Final Interview" and "Software
       Engineer, New Grad Remote", which structure alone cannot see.

    ...and an explicit refusal when the candidate normalises to the employer
    itself. The head-noun test catches most of those by accident; it does not
    catch a company whose own name contains a title head noun, and relying on an
    accident is how a rule stops refusing when an unrelated set is widened.

    RUNS LAST. It recognises one narrow shape, so it must not pre-empt the
    general patterns or the leading-segment reader — this is purely additive,
    and nothing that resolved before resolves differently now.
    """

    text = subject or ""
    # (1) The shape is pipe-segmented. Without this the "last segment" is the
    # whole subject, and "<Employer> - <Role> - <Employer>" reads the employer
    # into its own title.
    if "|" not in text:
        return None

    # (2) and (3) The last segment, less the location parenthetical.
    segment = _TRAILING_SEGMENT_PAREN.sub("", text.rsplit("|", 1)[-1].strip()).strip()

    # (4) and (5) Cut at the LAST spaced dash: title on the left, echo right.
    dashes = list(_SPACED_DASH.finditer(segment))
    if not dashes:
        return None
    cut = dashes[-1]
    candidate = segment[: cut.start()].strip()
    echo = segment[cut.end() :].strip()

    # (6) The licence.
    lead_tokens = {
        token
        for token in (_normalize_token(c) for c in _lead_segment_candidates(text))
        if token
    }
    echo_token = _normalize_token(echo)
    if not echo_token or echo_token not in lead_tokens:
        return None

    # (7) The guards the leading-segment reader uses. The head-noun test is the
    # same membership over the same tokens, asked for the noun's OFFSET as well
    # as its existence — one computation, so the guard and the region below it
    # cannot disagree about where the title's head is.
    role = _clean_role(candidate)
    if role is None:
        return None
    if not _TITLE_SHAPED.match(role):
        return None
    head_end = _last_head_noun_end(role)
    if head_end is None:
        return None

    # (8) ...and the refusals this position needs on its own account.
    if _normalize_token(role) in lead_tokens:
        return None
    # BOTH PLACEMENTS OR NEITHER, made structural. "<Role> (Remote) - <Employer>"
    # and "<Role> - <Employer> (Remote)" are one posting written two ways and the
    # strip above only reaches the second, so keeping the first hands back
    # "Software Engineer (Remote)" where the second gives "Software Engineer" —
    # and :func:`normalize_role_token` deletes the brackets but KEEPS THE WORD,
    # so those are two role_tokens for one job.
    #
    # ANY parenthetical, not a listed one. A vocabulary here cannot close the
    # split, because the tail-side strip is unconditional and place names are an
    # open set: "(Bengaluru)" would sail through a work-arrangement list and mint
    # the second token anyway. The regex is literally the one the tail side uses,
    # so the two edges cannot drift apart in a later edit.
    #
    # Its cost is the cohort parenthetical — "Software Engineer I (Graduation
    # Date: Fall 2026)" now queues in this shape. Since every subject of this
    # shape resolved to nothing at all before this reader existed, that is recall
    # not gained rather than recall lost, and a person types the title once.
    if _TRAILING_SEGMENT_PAREN.search(role):
        return None
    region = _post_head_region(role, head_end)
    # The nine stems and the arrangement words, over the WHOLE region. Rule 2
    # below licenses any comma-introduced continuation — which is what the
    # reported title is made of — so "Software Engineer, Final Interview" and
    # "Software Engineer, Distributed Systems Platform, New Grad Remote" are
    # invisible to structure and visible only here.
    if _LIFECYCLE_IN_REGION.search(region):
        return None
    normalized_region = _normalize_token(region)
    if normalized_region and any(
        f" {word} " in f" {normalized_region} " for word in _WORK_ARRANGEMENT_WORDS
    ):
        return None
    if not _post_head_is_introduced(region):
        return None
    return role


def _employer_from_subject_segment(
    subject: str, relay_brand: str
) -> tuple[str, str] | None:
    """Employer named by the leading segment of an ATS subject, or None.

    ``"Crusoe | Application Received"`` → ``Crusoe``. This is the shape that has
    no ``at``/``with``/``to`` connective for :data:`_EMPLOYER_ANCHORED` to hang
    off, which is why a real production classification silently created nothing.

    Since #512 it also reads ``"<Employer> Follow-Up for <Role> | <Candidate>"``,
    Greenhouse's standard rejection subject, whose employer sat unreadable in
    the first word for as long as this function has existed.
    """

    for index, candidate in enumerate(_lead_segment_candidates(subject or "")):
        display = _clean_company_display(candidate)
        if not display:
            continue
        token = _normalize_token(display.split(" ")[0])
        if not _valid_company_token(token) or _names_the_relay(token, relay_brand):
            continue
        # A SEGMENT ENDING IN A TITLE'S HEAD NOUN IS THE ROLE, NOT THE EMPLOYER.
        #
        # `_valid_company_token` reads the FIRST word, which is why this was
        # needed and why it went unnoticed: "Senior Software Engineer Interview
        # | <name>" is an unbroken Title-Case run to the delimiter and "senior"
        # is not a stopword, so the shipped code resolved it to
        # ('senior', 'Senior Software Engineer Interview') and would have put
        # that on the board as a company. Verified against `main` before this
        # line existed, along with "Machine Learning Engineer Offer" and
        # "Product Designer Recruiting Update" — three invented employers.
        #
        # The LAST word, because it is the segment's head; everything before it
        # modifies it. Testing every word would refuse "Team Liquid" and
        # "People Data Labs", which `_NAME_ROLE_TAIL` above already records as
        # names that must not be shredded from the middle out.
        #
        # `continue`, not `return None`: a refusal here is a refusal of THIS
        # reading of the subject, and the tailed pattern may still find a
        # shorter company in front of the same words.
        #
        # The lifecycle words are tested through `_LIFECYCLE_WORD` rather than
        # through `_COMPANY_STOPWORDS` alone, because that set spells them as
        # single words and the subjects do not: "Follow-Up" normalises to
        # neither "follow" nor "up", so "Staff Data Scientist Follow-Up"
        # survived every other guard here.
        last = display.split(" ")[-1]
        tail = _normalize_token(last)
        if (
            tail in _ROLE_HEAD_NOUNS
            or tail in _COMPANY_STOPWORDS
            or _LIFECYCLE_WORD.match(last)
        ):
            continue
        # #733 again, and this door is why guarding the display name alone is
        # not a fix: "Sarah Chen - quick chat?" reaches here with NO display
        # name at all and resolved ('sarah', 'Sarah Chen') on the shipped code.
        #
        # ...and #539: THE ONE-WORD EXEMPTION BELONGS TO THE FIRST READING.
        # `_lead_segment_candidates` offers at most two readings of a segment
        # and the second is always the first one CUT at a lifecycle word. When
        # the uncut reading has just been refused above — "Phone Interview" has
        # no corporate evidence — the cut left behind is one word, cleared the
        # guard unconditionally, and filed a company called Phone.
        #
        # INDEX, NOT 'IS IT A CUT', and the difference is #512's whole shape.
        # "<Employer> Follow-Up for <Role> | <Candidate>" has a remainder, so
        # the uncut run is never offered and the list is the CUT ALONE, at
        # index 0. Refusing every cut would take that case — the one this
        # branch was widened for — back to None. What separates the two is
        # whether this loop already read a longer version of the same segment
        # and turned it down.
        if not _carries_corporate_evidence(display, "", one_word_is_evidence=index == 0):
            continue
        return token, display
    return None


def employer_from_text(raw: str | None) -> tuple[str, str] | None:
    """Resolve a caller-supplied company string to ``(token, display)`` or None.

    Used when the pipeline cannot name the employer itself and the USER supplies
    it (``POST /applications/review/{id}/classify`` with a ``company``). Cleaned
    and validated with exactly the same rules as an extracted name, so a blank
    or stopword-only string still cannot manufacture a row.
    """

    display = _clean_company_display(raw or "")
    if not display:
        return None
    token = _normalize_token(display.split(" ")[0])
    if not _valid_company_token(token):
        return None
    return token, display


# How far apart two employer names may be and still be one employer typed twice:
# ONE edit — a substitution, an insertion, a deletion, or a swap of two adjacent
# letters, which is every way a hand slips on a keyboard.
_NEAR_MISS_MAX_EDITS = 1

# ...and the shortest name that edit may be applied to. Below five characters a
# single edit is most of the word, and the pairs it produces are real, DIFFERENT
# employers: Zoom/Loom, Bolt/Volt, Ramp/Rump.
_NEAR_MISS_MIN_LENGTH = 5

# The opening letters a reader recognises a brand by. Requiring them to agree is
# what separates "Verkada"/"Verkeda" — a slipped key in the middle of a name —
# from "Figma"/"Sigma" and "Notion"/"Motion", which are one edit apart and not
# the same company by any reading.
_NEAR_MISS_PREFIX = 2


def _within_one_edit(left: str, right: str) -> bool:
    """Is ``right`` reachable from ``left`` in at most one keyboard slip?

    Substitution, insertion, deletion, or a transposition of adjacent letters —
    the Damerau-Levenshtein neighbourhood at distance 1 — decided by walking the
    two strings once instead of filling a matrix. A bounded question does not
    need the general algorithm, and answering it this way keeps the serverless
    bundle free of a fuzzy-matching dependency it would otherwise carry for
    fifteen lines of work.
    """

    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    if len(long) - len(short) > _NEAR_MISS_MAX_EDITS:
        return False

    i = j = 0
    edits = 0
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > _NEAR_MISS_MAX_EDITS:
            return False
        if len(short) == len(long):
            # Same length, so it is a substitution — unless the very next letter
            # on each side is the other's, which makes it one transposition
            # rather than the two substitutions it would otherwise count as.
            if short[i + 1 : i + 2] == long[j : j + 1] and short[i : i + 1] == long[j + 1 : j + 2]:
                i += 2
                j += 2
                continue
            i += 1
            j += 1
        else:
            j += 1  # the longer side carries the extra letter
    # Whatever is left unconsumed on either side is the edit that ends it.
    return edits + (len(long) - j) + (len(short) - i) <= _NEAR_MISS_MAX_EDITS


def near_miss_employer(token: str, existing: Iterable[str]) -> str | None:
    """The stored employer name ``token`` is probably a MISSPELLING of, or None.

    ``token`` is a match key that named no stored row; ``existing`` is the set of
    company names already on the board. Both sides are reduced to their leading
    normalized word before comparison, the same way :func:`matches_company_token`
    compares them, because a stored display name and a match key are minted
    differently — otherwise every multi-word employer on the board is invisible
    to this check.

    A candidate qualifies only when all four hold: at least
    :data:`_NEAR_MISS_MIN_LENGTH` characters on both sides, the same first
    :data:`_NEAR_MISS_PREFIX` characters, at most :data:`_NEAR_MISS_MAX_EDITS`
    edits apart, and not already equal (an equal token is not a near miss — the
    caller would have found the row).

    THIS RESULT IS A QUESTION, NEVER AN ACTION. It exists so the one caller —
    the review queue's naming path — can offer the stored spelling back to the
    human who just typed a new one. Nothing merges on it. That is the whole
    safety argument: the rule above is deliberately loose enough that two real
    and distinct employers can trip it (Stripe/Strive are one edit apart and
    share their first two letters), and loose is affordable precisely because
    the answer is shown to a person rather than acted on.

    Several candidates return the alphabetically first rather than None. Under
    confirm-semantics, ambiguity is a reason to ASK, not a reason to fall silent
    and mint the row that ambiguity was about — a board already holding both
    "Verkada" and "Verkata" is where a typo is most likely, not least.
    """

    typed = _normalize_token(token or "").split(" ")[0]
    if len(typed) < _NEAR_MISS_MIN_LENGTH:
        return None

    matches: list[tuple[str, str]] = []
    for display in existing:
        stored = _normalize_token(display or "").split(" ")[0]
        if len(stored) < _NEAR_MISS_MIN_LENGTH or stored == typed:
            continue
        if stored[:_NEAR_MISS_PREFIX] != typed[:_NEAR_MISS_PREFIX]:
            continue
        if _within_one_edit(typed, stored):
            matches.append((stored, display))

    return min(matches)[1] if matches else None


def _brand_display(brand: str, sender_name: str | None) -> str:
    """Human display for an employer identified by its own mail domain."""

    if sender_name:
        cleaned = _clean_company_display(sender_name)
        if cleaned and _normalize_token(cleaned).startswith(brand):
            return cleaned
    return brand.replace("-", " ").title()


# "Thanks for applying to Twitch", "Thank you for applying to DoorDash" — the
# employer, spelled by the employer, in its own subject line.
_SUBJECT_NAMES_EMPLOYER = re.compile(
    r"\bapply(?:ing)?\s+(?:to|with|for)\s+(?:the\s+)?(?P<name>[A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,2})",
)


def _corporate_identity(
    brand: str, subject: str, sender_name: str | None
) -> tuple[str, str]:
    """``(token, display)`` for an employer that mailed from its own domain.

    The domain is the right thing to TRUST but the wrong thing to PRINT. Two
    live rows show why: ``no-reply@twitchjobs.tv`` rendered the company as
    "Twitchjobs" while its own subject said "Thank you for applying to Twitch",
    and ``no-reply@doordash.com`` rendered "Doordash" because title-casing a
    lowercase domain label cannot know where the intercap goes.

    So when the subject names a company and the domain agrees with it — the
    domain brand starts with, or is started by, the normalized subject name —
    the subject's spelling wins. The agreement test is what keeps this from
    picking up a company merely *mentioned* in a subject: "Your application to
    Acme via Workday" mailed from workday.com resolves nothing here, and falls
    through to the relay branches as before.

    The TOKEN moves with the display, deliberately. Returning "Twitch" for
    display while keeping "twitchjobs" as the match key would make the row
    unfindable by its own token on the next sync — `matches_company_token`
    compares leading words, and "twitch" is not "twitchjobs" — so the sync would
    file a duplicate. Keeping them consistent means the old mis-named row is
    simply left without mail and dismissed as an auto row, which is recoverable
    and self-healing.
    """

    match = _SUBJECT_NAMES_EMPLOYER.search(subject or "")
    if match:
        named = _clean_company_display(match.group("name"))
        # The FIRST normalized word, not the whole name space-stripped. Every
        # other token in this module is a single word, and
        # `matches_company_token` compares normalized names word-wise — so a
        # concatenated "ixllearning" matches the stored "IXL Learning" under no
        # rule at all. It cost the owner's board a fresh row per rebuild: the
        # lookup never found the existing one, the upsert minted another, and the
        # emptied predecessor was dismissed, forever.
        token = _normalize_token(named).split(" ")[0]
        if token and (brand.startswith(token) or token.startswith(brand)):
            return token, named
    return brand, _brand_display(brand, sender_name)


def resolve_employer(
    sender_email: str,
    subject: str = "",
    sender_name: str | None = None,
) -> tuple[str, str] | None:
    """Identify the real EMPLOYER for a message, or None when unsure.

    Returns ``(token, display)`` where ``token`` is the stable lowercase match
    key and ``display`` the human name. Unlike :func:`company_key` (which always
    returns *something* so follow-up grouping never None-guards), this refuses
    to guess: if the employer cannot be named with confidence it returns None,
    and the caller must NOT create an application row from that message.

    Order:
      1. The sender's own corporate domain (``careers@stripe.com`` → Stripe) —
         but NOT a shared ATS/job-board relay, an assessment vendor, consumer
         webmail, a generic ESP, or a ``.edu`` host (a student's university is
         not an employer here).
      2. An employer named explicitly in the subject ("... at <Company>",
         "on behalf of <Company>", and — for ATS relays only — a trailing
         "@ <Company>"). This is the relay case (Lever/Greenhouse). For an
         ASSESSMENT VENDOR only it also reads the employer as the sentence
         subject of an invitation, "<Company> invites you to take an
         assessment" (#687) — see :data:`_EMPLOYER_INVITES` for why that fence
         is narrower than the one the rest of this list uses.
      3. (ATS relays only) the sender DISPLAY NAME — "Crusoe Hiring Team" →
         Crusoe, "Team Talent @ MotherDuck" → MotherDuck.
      4. (ATS relays only) the subject's leading segment before a ``|`` or a
         spaced dash — "Crusoe | Application Received" → Crusoe.

    Steps 3 and 4 are deliberately LAST and deliberately restricted to ATS /
    job-board / ESP relays: an ATS message really is sent on behalf of one
    employer, so its display name and subject lead are honest signals. Consumer
    webmail is excluded because a display name there is a person, and a ``.edu``
    (or any other host that already failed step 1) is excluded because it never
    reaches these branches at all. Without 3 and 4 a real production
    classification of "Crusoe | Application Received" resolved to None and the
    endpoint created nothing while reporting success.

    AN ASSESSMENT VENDOR GETS STEP 2 AND NOT STEPS 3-4, deliberately. Its brand
    stops being taken as the employer (``ASSESSMENT_RELAY_DOMAINS``), and its
    subject is read for the invitation shape — but a vendor's display name is
    the vendor ("Coderbyte"), and its subject lead is usually the test's name.
    So an invite that names nobody now resolves to None and goes to the review
    queue, where a person decides, instead of filing a card at the vendor. That
    is the direction this module takes everywhere else, and it is a change: the
    same message files a card today, at a company the reader never applied to.
    """

    domain = ""
    if "@" in sender_email:
        domain = sender_email.rsplit("@", 1)[1].strip().lower()
    labels = [p for p in domain.split(".") if p]
    tld = labels[-1] if labels else ""
    brand = _domain_brand(domain)

    corporate = (
        brand
        and brand not in RELAY_DOMAINS
        and len(brand) >= 2
        # The upper bound is not symmetry with the lower one. `_brand_display`
        # ends `brand.replace("-", " ").title()` on a RAW domain label, with no
        # cleaner anywhere on the path, so this conjunct is the only thing
        # standing between an oversized address and the indexed column. A DNS
        # label is at most 63 characters, so nothing real is refused here.
        and len(brand) <= _MAX_COMPANY_LEN
        and tld != "edu"
        and not brand.isdigit()
    )
    if corporate:
        return _corporate_identity(brand, subject, sender_name)

    from_subject = _employer_from_subject(
        subject, ats_relay=brand in ATS_RELAY_DOMAINS, relay_brand=brand
    )
    if from_subject:
        token = _normalize_token(from_subject.split(" ")[0])
        if _valid_company_token(token):
            return token, from_subject

    if brand in ATS_RELAY_DOMAINS:
        from_name = _employer_from_sender_name(sender_name, brand)
        if from_name is not None:
            return from_name
        from_segment = _employer_from_subject_segment(subject, brand)
        if from_segment is not None:
            return from_segment

    return None


def employer_named_in_body(snippet: str, sender_email: str = "") -> tuple[str, str] | None:
    """The employer this message's BODY names, or None. DISPLAY GRADE ONLY.

    Deliberately NOT part of :func:`resolve_employer`, and no caller may route
    it into :func:`_qualifies_for_hard_row`. The separation is the whole design:

    * :func:`resolve_employer` is FILING grade. Whatever it returns becomes a
      card on the board, so it reads only the sender's own domain, the subject,
      and (for relays) the display name — signals an employer controls.
    * this is DISPLAY grade. What it returns only ever reaches a sentence in
      the review queue and a prefilled name the user confirms, so a wrong
      answer costs a wrong suggestion the human is already looking at, not a
      wrong row on the board.

    That asymmetry is what makes reading body prose acceptable at all. The
    population this pattern would be most dangerous on is the ATS rejection
    preamble — "Thank you for your interest in <Employer>" is *documented above
    as the standard opening of a rejection*, and the reason those rows sit in
    the queue at high confidence is that a snippet-starved classifier can read
    the preamble as a confirmation. If body resolution fed the filing path, the
    exact population it was built for would start auto-filing REJECTIONS as
    APPLIED, which is the failure #166 refused. At display grade it cannot:
    nothing here can create, move or close an application.

    The relay check that :func:`_employer_from_subject` skips is applied here,
    and the inversion is deliberate: body prose is the weakest signal in the
    module, so it gets the strictest fence. A capture that names the SENDING
    RELAY is refused — "your interest in Ashby" off ``ashbyhq.com``, "in
    Greenhouse" off ``greenhouse-mail.io``, "in Lever" off ``hire.lever.co``.

    Note what this does NOT refuse, because the distinction is easy to get
    backwards: "your interest in Handshake" off ``ashbyhq.com`` resolves, and
    should. Handshake is the EMPLOYER there and Ashby is the relay carrying the
    mail. The check compares the capture against the sender's own brand, not
    against a list of companies that also happen to sell recruiting software
    (#508 is the scar from conflating those two).

    …EXCEPT ON THE ONE PATH WHERE THERE IS NO SENDING BRAND TO COMPARE AGAINST,
    and #687 widened that path by fourteen names, deliberately and with the cost
    written down here rather than discovered later. The call below passes
    ``brand if brand in RELAY_DOMAINS else ""``, so for a sender that is NOT a
    relay :func:`_names_the_relay` falls back to the vocabulary — and with the
    assessment vendors now in that vocabulary:

        "…your interest in Karat"  from a corporate domain
            before  ('karat', 'Karat')
            after    None

    …with HireVue, Woven, Coderbyte and Mettl the same. Through an ATS it is
    unchanged, because a brand IS known there and the precise question gets
    asked.

    KEPT, on a positive argument rather than a shrug: on that path the vocabulary
    is the only signal there is, and the population it now covers is real —
    "you have been invited by HackerRank to complete an assessment", sent from an
    employer's own domain, names the COURIER, and refusing it is right. What it
    costs is a genuine employer whose name is a vendor's, mentioned in body prose
    from a third party's domain: that row reaches the queue with no suggested
    name instead of a correct one. Display grade, so the cost is a queue row a
    human already has open, never a card. If it ever needs reversing, the change
    is here — narrow the fallback set — and not in
    :func:`_names_the_relay`, which four other callers depend on.
    """

    if not snippet:
        return None
    # Second, and only when the first declines, so this stays additive: the
    # rejection preamble this function was built for keeps the answer it gives
    # today, and an assessment invitation — which carries no "interest in"
    # sentence at all — now has a reading instead of none (#687).
    match: re.Match[str] | None = None
    for pattern in (_EMPLOYER_INTEREST_IN_BODY, _EMPLOYER_INVITED_BY_BODY):
        match = pattern.search(snippet)
        if match is not None:
            break
    if match is None:
        return None

    display = _clean_company_display(match.group(1))
    if not display:
        return None
    token = _normalize_token(display.split(" ")[0])
    if not _valid_company_token(token):
        return None

    brand = _domain_brand(sender_email.rsplit("@", 1)[1].lower()) if "@" in sender_email else ""
    if brand and _names_the_relay(token, brand if brand in RELAY_DOMAINS else ""):
        return None
    return token, display


def _role_from_subject(subject: str) -> str | None:
    """Extract a job role/title from a subject, or None. Never 'Unknown role'."""

    text = subject or ""
    for pattern in _ROLE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        role = re.sub(r"\s+", " ", match.group(1)).strip(" .-–—")
        words = role.split()
        if not words:
            continue
        # Reject a capture that is only filler (e.g. "the", "your update").
        if all(_normalize_token(w) in _ROLE_FILLER for w in words):
            continue
        if len(role) < 3:
            continue
        return role
    # Last, and only when every pattern above declined: the two segment readers
    # are narrower rules about known shapes, so they must not pre-empt the
    # general ones. Purely additive — nothing that resolved before resolves
    # differently now, and the leading-segment reader keeps its place ahead of
    # the trailing one so today's answers are reproduced exactly.
    from_lead = _role_from_lead_segment(text)
    if from_lead is not None:
        return from_lead
    return _role_from_trailing_segment(text)


def is_terminal_status(value: str) -> bool:
    """Is this a status a mail signal may never override on its own?

    Exported so ``jobtracker.cloud.applications`` can ask the question without
    keeping a second copy of the set. A second copy is how ``assessment`` once
    came to mean ``interviewing`` in one place and a settable stage in another.
    """

    return value in _TERMINAL_STATUSES


def advance_application_status(current: str, incoming: str) -> str:
    """Return the status a stored row should hold given an incoming signal.

    Decided from ``current`` and ``incoming`` alone, and it guarantees exactly
    two things:

    - a TERMINAL status (rejected/accepted/withdrawn/ghosted) is never left, and
    - an in-flight row only moves FORWARD (applied → assessment → interviewing
      → offered), or to ``rejected`` on a rejection. It never downgrades — so a
      re-test mailed to a row already at ``interviewing`` leaves it there, and
      that deadline still lands, because ``due_at`` is recomputed from the mail
      independently of the status.

    What it does NOT know is who owns the row. "Never overrides a status the
    USER settled" is a separate rule that lives entirely in the callers, all in
    ``jobtracker.cloud.applications``: ``upsert_applications_for_user``,
    ``split_application_cloud`` and ``reconcile_orphaned_classifications`` each
    check ``_is_auto_row(row.source)`` before writing anything, and
    ``classify_review_item`` — where the human IS the signal, so the check would
    be wrong — instead flips ``source`` to ``gmail_user`` on the way out, but
    only when the stage actually moved. Claiming that invariant here is how the
    orphan catch-up came to omit it: the docstring made it look already handled.
    """

    if current in _TERMINAL_STATUSES:
        return current
    if incoming == "rejected":
        return "rejected"
    if _STATUS_RANK.get(incoming, 0) > _STATUS_RANK.get(current, 0):
        return incoming
    return current


def _message_ref(item: PipelineItem) -> MessageRef:
    return MessageRef(
        message_id=item.message_id,
        thread_id=item.thread_id,
        subject=item.subject,
        sender_email=item.sender_email,
        sender_name=item.sender_name,
        # Naive-UTC so the ref persists straight into the naive Email.received_at
        # column without asyncpg rejecting an aware datetime.
        received_at=to_naive_utc(item.received_at),
        category=item.category,
        confidence=item.confidence,
        snippet=item.snippet,
        # Carried, not re-derived. The persist layer only ever sees the stored
        # ~200-character snippet, so re-deriving there would write a weaker
        # identity than the reader already computed from the body — and the two
        # would then disagree about the same message.
        identity_role=item.identity_role,
        identity_req_id=item.identity_req_id,
        # Carried for the same reason and with the same meaning for ``None``.
        method=item.method,
    )


def _qualifies_for_hard_row(item: PipelineItem) -> tuple[str, str] | None:
    """Return the (token, display) employer iff this item may assert a status.

    A hard-row contributor is a non-follow-up lifecycle verdict at/above the
    auto-file gate whose employer can be named. Everything else (low confidence,
    unknown employer, follow-up, other/needs_review) returns None.
    """

    if item.category not in JOB_LIFECYCLE_CATEGORIES or item.category == "follow_up":
        return None
    if item.confidence < AUTO_FILE_GATE:
        return None
    return resolve_employer(item.sender_email, item.subject, item.sender_name)


def _may_join(
    cluster_req_id: str | None,
    cluster_role_token: str | None,
    req_id: str | None,
    role_token: str | None,
) -> bool:
    """Does a message with this identity belong to a cluster with that one?

    The cascade this file documents is "requisition id first, then role token",
    and the docstrings on both :func:`partition_applications` and
    ``_pick_application`` state that nothing outranks the employer's own number.
    The code did not honour that: the two clauses were OR-ed, so a role-token
    match joined a cluster whose requisition id EXPLICITLY DISAGREED.

    Two openings at one employer routinely share a title — "Mechanical
    Engineer (R-40881)" and "Mechanical Engineer (R-40882)" — and the ids are
    the only thing that tells them apart. OR-ing collapsed them onto one card,
    which is the strictly worse direction of failure: a split leaves the user
    two cards to merge by hand, but a merge destroys a record silently and
    nothing on the board says a second application ever existed.

    The guard is narrow on purpose. It fires only when BOTH sides carry an id
    and the ids differ; when either is None the message may still join, which
    is what preserves "each message may carry the half of the identity the
    other lacked" — the confirmation brings the requisition id, the interview
    invite that follows brings only the title, and they are still one
    application.
    """

    if req_id is not None and cluster_req_id is not None and req_id != cluster_req_id:
        return False
    if req_id is not None and cluster_req_id == req_id:
        return True
    return role_token is not None and cluster_role_token == role_token


@dataclass(frozen=True)
class _Cluster:
    """One application's worth of gated mail, before it becomes a row."""

    company_token: str
    company_display: str
    req_id: str | None
    role_token: str | None
    role: str | None
    items: list[PipelineItem]


#: How far apart two acknowledgements may sit and still be about ONE submission.
#:
#: Not a tuning knob picked to make one mailbox come out right — the two shapes
#: it separates are an order of magnitude apart on either side, measured in the
#: owner's real mail on 2026-08-23:
#:
#:   ONE submission, two acknowledgements   Supabase, 2h01m apart (21:02 and
#:                                          23:03 on 10 August). Ashby's generic
#:                                          note and the Supabase talent team's,
#:                                          both reacting to the same submit.
#:   TWO submissions                        Google, 2 days and then 8 days apart
#:                                          (11, 13 and 21 August).
#:
#: Automation reacting to one event fires in minutes or hours. A person applying
#: again to the same employer, having named no role either time, took days. A day
#: sits between the two with a full order of magnitude of slack on both sides,
#: which is the same way DEPLOY_GRACE was chosen.
DOUBLE_ACK_WINDOW = timedelta(hours=24)


def acknowledgement_template(subject: str) -> str:
    """A subject reduced to the TEMPLATE it came from.

    Case, spacing, punctuation and emoji all vary between an employer's own
    acknowledgement and its ATS's, and none of them is part of which template
    fired. What matters is whether two subjects are the same generated string.
    """

    return " ".join(re.sub(r"[^0-9a-z]+", " ", subject.casefold()).split())


def group_double_acknowledgements(
    anchors: Sequence[PipelineItem],
) -> list[list[PipelineItem]]:
    """Group anonymous confirmations that acknowledge ONE submission — issue #480.

    An anonymous confirmation is mail that asserts an application and names
    nothing: no requisition id, no role, in the subject or the body. Two of them
    from one employer are genuinely ambiguous — the mail does not contain the
    answer — and until now every one of them minted its own card.

    THAT IS RIGHT FOR GOOGLE AND WRONG FOR SUPABASE, and the difference is not
    in what the messages say. Both of Supabase's were pulled in full on
    2026-08-23 and neither holds a role, a requisition, or a link:

        21:02  "Thanks for applying to Supabase 🚀"
               "Thanks for applying to Supabase. We're really glad you're
                interested in what we're building..."
        23:03  "Thank you for applying to Supabase!"
               "Thanks for your interest in a role with Supabase; we confirm
                your application has been received..."

    One submission. Two systems acknowledging it — Ashby's template and the
    talent team's — two hours apart. Google's three say the SAME sentence under
    the SAME subject on 11, 13 and 21 August, and are three real applications.

    So the signal is the acknowledgement's SHAPE, not its words. An employer's
    ATS emits one template per submission event: receiving the same template
    twice means the event happened twice, while two different templates in one
    window means two emitters reacted to one event. Both conditions are
    required, and each one alone would be wrong:

      * template alone — an employer that changes its wording between two
        applications weeks apart would silently lose one of them;
      * window alone — two genuine same-day applications, which
        ``repeat-anonymous`` in the corpus is built from, would collapse.

    WHY THE FAILURE DIRECTION MOVED. The rule this replaces argued that minting
    two cards was the safe error because "a user can merge them". There is no
    merge. `POST /applications/{id}/split` exists and has a UI prompt; no merge
    endpoint and no merge control exist anywhere in this repository. So the old
    failure was not the visible-and-remediable one it was documented as — it was
    unrecoverable, and it took the employer's future mail with it:
    ``known_multi`` makes every later role-less message from a two-card employer
    ``unplaced``, so it lands in the review queue asking which of two
    applications it belongs to when there is no right answer. Verified against
    the shipped code, 2026-08-23.

    THIS IS AN INFERENCE FROM DELIVERY SHAPE AND IT IS STATED AS ONE. Nothing in
    either Supabase message distinguishes it from the other. Where the mail is
    silent the product is guessing, and this changes which way it guesses.

    Anchors arrive oldest-first. Deterministic: no set or dict iteration.
    """

    groups: list[list[PipelineItem]] = []
    for item in anchors:
        template = acknowledgement_template(item.subject)
        joined = False
        for group in groups:
            # A group's clock runs from its OLDEST member, not its newest, so a
            # long chain of differently-worded acknowledgements cannot walk the
            # window forward indefinitely and swallow a later real application.
            earliest = to_naive_utc(group[0].received_at)
            current = to_naive_utc(item.received_at)
            if earliest is None or current is None:
                continue
            if current - earliest > DOUBLE_ACK_WINDOW:
                continue
            if any(acknowledgement_template(g.subject) == template for g in group):
                continue
            group.append(item)
            joined = True
            break
        if not joined:
            groups.append([item])
    return groups


def partition_applications(
    items: Iterable[PipelineItem],
    known_multi: frozenset[str] = frozenset(),
    known_threads: frozenset[str] = frozenset(),
) -> tuple[list[_Cluster], list[PipelineItem]]:
    """Split gated mail into per-application clusters, plus what it cannot place.

    This is the identity resolution the whole product rests on, and it is pure so
    it can be reasoned about and tested without a database.

    Within one employer, a message is placed by the first rule that fires:

    1. its requisition id matches a cluster (the strongest signal — two Amazon
       confirmations with different ids are two applications however similar
       their titles read);
    2. its normalized role token matches a cluster;
    3. it names no role at all, and the employer has exactly ONE cluster — so it
       joins that one. This is what keeps Roblox's separate email-verification
       message (different sender, no shared role text, no shared thread) on the
       same application as its confirmation, and it means behaviour changes only
       for employers with several applications.

    ONE narrow order-dependence survives the two passes, introduced by the
    requisition-id guard in :func:`_may_join`: a message carrying a role token
    but NO req id, at an employer holding two clusters that share that title and
    differ only by requisition id, joins whichever of them was minted first.
    Before the guard the question could not arise, because those two clusters
    were one. It is the right trade — the alternative is collapsing two
    applications — and the case needs an employer with two same-titled openings
    plus a message that names the title without the id, but it is stated here
    rather than left for someone to discover.

    A role-less message at an employer with SEVERAL applications is returned in
    the second element instead of being guessed at. Guessing here is not a cosmetic
    error: attributing a rejection to the wrong one of four Amazon rows settles a
    live application terminally and, because ``advance_application_status`` treats
    terminal states as final, freezes it against every later interview or offer.
    Those messages go to the review queue for the user to assign.

    A role-less message at an employer with NO other cluster mints its own
    ``(company, None)`` cluster — which is exactly the old behaviour, so an
    employer that genuinely never names a role (Supabase, Twitch, Together AI in
    the live corpus) still gets one honest row.

    ``known_multi`` — employer tokens the BOARD already holds several
    applications for. A real sync rolls up a delta, usually one message, so this
    function alone can only see what arrived in that batch: an employer with
    four cards and one role-less rejection in today's mail looks, from here,
    exactly like an employer with one application. It is not, and the difference
    is not cosmetic — ``advance_application_status`` treats a terminal status as
    final, so filing that rejection against whichever card sorted first freezes a
    live application against every later interview and offer. With the caller
    supplying what the board holds, the review-queue rule above applies to a
    delta exactly as it applies to a rebuild. Defaults to empty, which is the
    pure over-the-batch behaviour every existing caller had.
    """

    by_company: dict[str, list[tuple[PipelineItem, str, str | None, str | None, str | None]]] = {}
    for item in items:
        resolved = _qualifies_for_hard_row(item)
        if resolved is None:
            continue
        token, display = resolved
        # THE CARDS ARE BUILT HERE, so this is the site the user actually sees.
        # Fixing the queue keys and leaving this re-deriving from ``snippet``
        # would have fixed the plumbing and not the faucet: the board would go
        # on showing a blank position for every title printed past Gmail's ~200
        # characters, which is the whole of what was reported.
        #
        # Same fallback rule as everywhere else — a relay item carries no
        # derivation and is read from its snippet exactly as before.
        role, req = item_identity_parts(item)
        by_company.setdefault(token, []).append(
            (item, display, req, normalize_role_token(role), role)
        )

    clusters: list[_Cluster] = []
    unplaced: list[PipelineItem] = []

    for token, entries in by_company.items():
        display = entries[0][1]
        keyed: list[_Cluster] = []
        # Two passes, so placement never depends on arrival order: every message
        # that carries its own identity mints or joins first, and only then do the
        # anonymous ones look for a home.
        #
        # Scanning the clusters rather than keying a dict on ``req_id or
        # role_token`` is deliberate. Those are two namespaces that would
        # otherwise never meet: a confirmation carries the requisition id, the
        # interview invite that follows carries only the title, and a dict keyed
        # on "whichever we have" would file one application under two keys.
        for item, _display, req_id, role_token, role in entries:
            if req_id is None and role_token is None:
                continue
            match = next(
                (
                    c
                    for c in keyed
                    if _may_join(c.req_id, c.role_token, req_id, role_token)
                ),
                None,
            )
            if match is None:
                keyed.append(
                    _Cluster(
                        company_token=token,
                        company_display=_display,
                        req_id=req_id,
                        role_token=role_token,
                        role=role,
                        items=[item],
                    )
                )
                continue
            match.items.append(item)
            # Each message may carry the half of the identity the other lacked.
            keyed[keyed.index(match)] = replace(
                match,
                req_id=match.req_id or req_id,
                role_token=match.role_token or role_token,
                role=match.role or role,
                items=match.items,
            )

        anonymous = [e[0] for e in entries if e[2] is None and e[3] is None]
        if anonymous:
            # A NEW CONFIRMATION IS A NEW APPLICATION. AN UPDATE IS NOT.
            #
            # That is the whole rule, and ``APPLIED_SIGNAL_CATEGORIES`` is what
            # draws the line: a confirmation ASSERTS an application, while a
            # rejection, assessment, interview or offer REPORTS on one that
            # already exists. So a confirmation with no identity gets its own
            # card, and an update with no identity never mints one — it lands on
            # the application it is about.
            #
            # Google is why. Subject "Thanks for applying to Google", no role
            # anywhere in the body, no requisition number, no job link — three of
            # them arrived on 11, 13 and 21 August 2026 and all three folded onto
            # one card dated the 11th. A sync that classified every message
            # correctly showed the user a board that had not moved. Supabase is
            # the same shape at two.
            #
            # THREAD IS NOT AN IDENTITY, and is deliberately not used as one. The
            # four Microsoft confirmations of 21 August share a single Gmail
            # thread and are four separate applications; Gmail threaded them
            # because the sender and subject are byte-identical, which is a fact
            # about delivery and none about what the mail is. Thread is used
            # BELOW, and only below: to route an update to the right one of an
            # employer's applications, which is the case where a conversation
            # really does say "more about this one".
            #
            # Palantir is the control on the asymmetry — an anonymous
            # confirmation plus an anonymous rejection three days later stays one
            # application, and would have become two under a blanket split.
            #
            # The failure direction is deliberate and it is the one
            # :func:`_may_join` already argues for: an employer that sends two
            # confirmations for a SINGLE application, naming no role in either,
            # mints two cards — visible, and the spare one is a dismiss click.
            #
            # THERE IS NO MERGE, and this comment used to say a user could
            # perform one. `POST /applications/{id}/split` exists; nothing in
            # this repository pairs with it. The conclusion survives the
            # correction — a spare card can be taken off the board and a missing
            # application cannot be recovered — but the remedy is dismissal, and
            # naming a control that does not exist is how a failure direction
            # gets argued for on evidence nobody checked. The same correction is
            # already spelled out in :func:`group_double_acknowledgements`
            # above, which found it first.
            anchors = sorted(
                (i for i in anonymous if i.category in APPLIED_SIGNAL_CATEGORIES),
                # Oldest first, message id breaking a tie, so cluster order — and
                # therefore which of them adopts a pre-existing row — never
                # depends on the order Gmail happened to return the mail in.
                key=lambda i: (to_naive_utc(i.received_at) or _NAIVE_EPOCH, i.message_id),
            )

            # SEVERAL of them, or none of this applies. One anonymous
            # confirmation is not evidence of a second application — it is the
            # ordinary case of mail that names no role, and rule 3 below has
            # always been right about it. Roblox is why: its email-verification
            # message ("thank you for submitting your application for a position
            # at Roblox") reads as a confirmation, carries no role, and belongs
            # to the application whose real confirmation named one. Splitting on
            # a single anonymous confirmation would mint it a card of its own.
            if len(anchors) < 2:
                anchors = []

            anchored_ids = {i.message_id for i in anchors}
            first_anchor_index = len(keyed)
            for group in group_double_acknowledgements(anchors):
                keyed.append(
                    _Cluster(
                        company_token=token,
                        company_display=display,
                        req_id=None,
                        role_token=None,
                        role=None,
                        items=list(group),
                    )
                )

            # THE UPDATES. Everything left names no role and asserts no new
            # application, so none of it may mint. A conversation that names
            # exactly one of the applications above places it — that is the
            # "don't open a new card for an update" half, and the only thing
            # thread is trusted for. Ambiguous or unthreaded, it falls to rule 3
            # unchanged: ``keyed`` now counts the anchors, so a lone confirmation
            # still adopts its employer's follow-ups exactly as before, and an
            # employer with several applications still sends them to the review
            # queue for the user to assign rather than guessing which one.
            # The mapping is over the CLUSTERS the anchors became, not over the
            # anchors, because two acknowledgements of one submission arrive in
            # two threads and are now one cluster: keyed on the anchor's own
            # position this would have marked both threads ambiguous and sent
            # every later Supabase update to the review queue.
            by_conversation: dict[str, int | None] = {}
            for offset, item in (
                (o, i)
                for o, cluster in enumerate(keyed[first_anchor_index:])
                for i in cluster.items
            ):
                if item.thread_id:
                    # None marks a thread that holds MORE THAN ONE application —
                    # the Microsoft shape. It names no single row, so an update
                    # arriving in it is as ambiguous as an unthreaded one.
                    by_conversation[item.thread_id] = (
                        None
                        if item.thread_id in by_conversation
                        else first_anchor_index + offset
                    )

            unclaimed: list[PipelineItem] = []
            for item in (i for i in anonymous if i.message_id not in anchored_ids):
                index = by_conversation.get(item.thread_id) if item.thread_id else None
                if index is None:
                    unclaimed.append(item)
                else:
                    keyed[index].items.append(item)

            if unclaimed:
                if token in known_multi and len(keyed) != 1:
                    # The board already holds several applications here. There
                    # is no "the employer's only cluster" to join even when this
                    # batch contains one message, so asking is the only honest
                    # move — the same answer a rebuild gives for the same mail.
                    #
                    # TWO KINDS OF MAIL ARE NOT AMBIGUOUS AND MUST NOT BE ASKED
                    # ABOUT. A confirmation asserts an application, so "which of
                    # these is it about?" is the wrong question entirely — it is
                    # about a new one, and sending it to the queue is how the
                    # user's second application to an employer stops appearing
                    # at all. And an update whose Gmail conversation already
                    # names exactly one stored card belongs to that card;
                    # ``known_threads`` carries only the unambiguous ones, so a
                    # thread holding two applications still gets asked about.
                    # Both become their own clusters and the resolver places
                    # them — which is the same order a rebuild uses.
                    for item in unclaimed:
                        if (
                            item.category in APPLIED_SIGNAL_CATEGORIES
                            or (item.thread_id and item.thread_id in known_threads)
                        ):
                            keyed.append(
                                _Cluster(
                                    company_token=token,
                                    company_display=display,
                                    req_id=None,
                                    role_token=None,
                                    role=None,
                                    items=[item],
                                )
                            )
                        else:
                            unplaced.append(item)
                elif not keyed:
                    keyed.append(
                        _Cluster(
                            company_token=token,
                            company_display=display,
                            req_id=None,
                            role_token=None,
                            role=None,
                            items=list(unclaimed),
                        )
                    )
                elif len(keyed) == 1:
                    keyed[0].items.extend(unclaimed)
                else:
                    unplaced.extend(unclaimed)

        clusters.extend(keyed)

    return clusters, unplaced


def unplaceable_message_ids(
    items: Iterable[PipelineItem],
    known_multi: frozenset[str] = frozenset(),
    known_threads: frozenset[str] = frozenset(),
) -> set[str]:
    """Message ids that name no role at an employer holding several applications.

    :func:`collect_review_items` promotes these into the queue so the user can
    say which application they belong to, rather than the pipeline picking one.
    """

    _clusters, unplaced = partition_applications(items, known_multi, known_threads)
    return {item.message_id for item in unplaced}


def roll_up_applications(
    items: Iterable[PipelineItem],
    known_multi: frozenset[str] = frozenset(),
    known_threads: frozenset[str] = frozenset(),
) -> list[RolledApplication]:
    """Group high-confidence lifecycle mail into one row per real APPLICATION.

    Only messages that clear the precision gate (:func:`_qualifies_for_hard_row`)
    contribute: at/above the 0.85 auto-file confidence, a real lifecycle
    category, and a nameable employer. Identity within an employer comes from
    :func:`partition_applications`. An application's status is the furthest stage
    *its own* gated mail reached (applied < assessment < interview < offer), with
    a gated rejection as a terminal override — per application, so one
    requisition's rejection can no longer settle three live ones beside it.
    Uncertain mail never lands here — it goes to :func:`collect_review_items`
    instead — so the board shows real rows, not noise parsed out of job alerts.

    That override is scoped to the LATEST JOURNEY SEGMENT. Applying again to a
    role you were rejected for is a second application, and it does not get a
    second row: the resolver keys on ``(employer, req_id or role_token)`` and
    matches terminal rows too, so the new confirmation lands on the settled one.
    Reading the status from the mail strictly newer than the newest dated
    rejection is what makes that row show the application the user actually
    made. ``latest_rejection_at`` and ``latest_applied_signal_at`` carry the
    evidence to :func:`~jobtracker.cloud.applications.upsert_applications_for_user`,
    which is the only place a stored terminal status may be left.

    A cluster with no applied signal after its newest dated rejection rolls up
    EXACTLY as it did before segments existed, undated rejections included. That
    is the whole compatibility claim, and ``backend/tests/test_reopen_after_rejection.py``
    asserts it field-for-field against a verbatim copy of the old algorithm.

    Deterministic and DB-free — the same input always yields the same rows,
    which is what makes the downstream upsert idempotent.
    """

    clusters, _unplaced = partition_applications(items, known_multi, known_threads)

    rolled: list[RolledApplication] = []
    for cluster in clusters:
        token, display, msgs = cluster.company_token, cluster.company_display, cluster.items
        categories = {m.category for m in msgs}
        has_rejection = "rejection" in categories
        max_rank = max((_STAGE_RANK.get(c, 0) for c in categories), default=1)

        # Normalize to naive UTC FIRST, so min()/max() never compares a mix of
        # aware and naive datetimes (which raises), and the result persists into
        # the naive TIMESTAMP columns without asyncpg's aware→naive encoder error.
        dated = [to_naive_utc(m.received_at) for m in msgs if m.received_at is not None]
        applied_dates = [
            to_naive_utc(m.received_at)
            for m in msgs
            if m.category in ("applied", "pending_application") and m.received_at
        ]
        applied_at = (
            min(applied_dates) if applied_dates else (min(dated) if dated else None)
        )
        last_activity = max(dated) if dated else None

        # SEGMENTS. A rejection ends one; mail strictly newer than the newest
        # DATED rejection begins the next. Status is read from the latest
        # segment, so re-applying to a role you were turned down for shows the
        # application you actually made instead of the one that ended.
        #
        # Deliberately a set filter and not a chronological walk. A walk reads
        # as "the last message wins", which would downgrade an interviewing row
        # the moment a duplicate confirmation arrived after it; only a REJECTION
        # starts a segment, and within one the rollup is the same order-blind
        # maximum it has always been. Nothing here depends on the order mail
        # arrives in, so no tie-break is needed for a rebuild to be stable.
        #
        # Every ambiguity resolves toward STAY-REJECTED, because a false stay is
        # one visible bug a human can correct while a false reopen recurs on
        # every rebuild: an undated rejection cannot be ordered and so falls back
        # to the old rule wholesale; undated mail is never in a segment; and the
        # comparison is strict, so a confirmation at the rejection's own instant
        # does not reopen anything.
        latest_rejection_at = max(
            (
                to_naive_utc(m.received_at)
                for m in msgs
                if m.category == "rejection" and m.received_at is not None
            ),
            default=None,
        )
        latest_applied_signal_at = max(applied_dates, default=None)

        segment = (
            [
                m
                for m in msgs
                if m.received_at is not None
                and to_naive_utc(m.received_at) > latest_rejection_at
            ]
            if latest_rejection_at is not None
            else []
        )
        if any(m.category in ("applied", "pending_application") for m in segment):
            status = _rank_to_status(
                max((_STAGE_RANK.get(m.category, 0) for m in segment), default=1)
            )
        else:
            status = "rejected" if has_rejection else _rank_to_status(max_rank)

        role = cluster.role

        # The LATEST stated deadline wins: a rescheduled assessment supersedes
        # the original, and the newest message is the one that knows.
        stated = [
            (to_naive_utc(m.received_at), extract_deadline(m.subject, m.snippet, m.received_at))
            for m in msgs
        ]
        dated = [(seen, due) for seen, due in stated if due is not None and seen is not None]
        due_at = max(dated, key=lambda pair: pair[0])[1] if dated else None

        refs = sorted(
            (_message_ref(m) for m in msgs),
            key=lambda r: _as_utc(r.received_at) if r.received_at else _EPOCH,
            reverse=True,
        )

        rolled.append(
            RolledApplication(
                company_token=token,
                company_display=display,
                role=role,
                status=status,
                applied_at=applied_at,
                last_activity=last_activity,
                messages=tuple(refs),
                req_id=cluster.req_id,
                role_token=cluster.role_token,
                due_at=due_at,
                latest_rejection_at=latest_rejection_at,
                latest_applied_signal_at=latest_applied_signal_at,
            )
        )

    # Sorted by the full identity, not just the company: several applications at
    # one employer must come back in a stable order across syncs or the upsert
    # stops being idempotent.
    return sorted(rolled, key=lambda r: (r.company_token, r.req_id or "", r.role_token or ""))


def is_ats_sender(sender_email: str | None) -> bool:
    """Is this address a known Applicant Tracking System relay?

    Thin wrapper over ``classifier.rules.is_ats_sender``, imported inside the
    function on purpose. This module is otherwise free of ``jobtracker`` imports
    — that is what lets it be unit-tested without a Gmail token and what keeps
    ``sqlmodel`` and the classifier out of its import graph on a cold start. The
    list itself is NOT copied here: one definition, read late.
    """

    from jobtracker.classifier.rules import is_ats_sender as _rules_is_ats_sender

    return _rules_is_ats_sender(sender_email)


#: Text in which this message speaks about the READER'S OWN place in a hiring
#: process, rather than about jobs in general.
#:
#: ISSUE #447. This answers "is this mail about an application of yours?" and
#: deliberately never answers "what happened to it". Those are different
#: questions and conflating them is the defect #451 tracks: reference text
#: saying WHICH application a message concerns must never outrank report text
#: saying WHAT HAPPENED. Nothing here contributes to a category or a score. It
#: decides one thing — whether a human is asked — and the human supplies the
#: verdict.
#:
#: Why it is needed at all. An ATS rejection spends Gmail's whole ~186-character
#: snippet on a polite preamble, so when no body part can be extracted the
#: classifier sees only the thank-you and scores ``other`` at 0.50. ``other`` is
#: not a lifecycle category, so the ATS floor below did not reach it and the
#: message left through the terminal drop: no row, no queue entry, no counter.
#: 610 messages in the 15k corpus, and the residual ``pipeline`` already named
#: and declined to cover ("an ATS message that scores NOTHING in any category is
#: ``other`` and still drops").
#:
#: Why not simply queue everything an ATS relays. That is the widening declined
#: there, and ``tests/corpus_independent`` now has ``ats-relay-noise`` to make
#: the difference measurable: 400 job alerts, talent-community blasts, profile
#: nudges, surveys and referral asks, all from real relay domains, none about an
#: application the reader made. Sender alone queues all 400.
#:
#: EVERY ALTERNATIVE IS ONE OF THE FIVE PHRASES BELOW, and each was checked
#: against the four real rejections in the owner's mailbox (2026-08-22), which
#: between them use FOUR DIFFERENT ONES:
#:   · Anthropic  — "went into your application"
#:   · Palantir   — "proceeding with your candidacy"
#:   · Verkada    — "your interest in the Embedded Software Engineer ...
#:                  opportunity" — and it never uses the word "application"
#:   · TogetherAI — "taking the time to apply for the ... opening"
#: A signal measured against only the first would have looked perfect on a
#: corpus and missed a quarter of the real cases. The corpus family carries the
#: Verkada wording alongside the Together AI one for exactly this reason.
#:
#: The offer clause covers the rescinded-offer shape, where the sender's own
#: words are "we have had to withdraw the offer for this position" and the word
#: application never appears either.
#:
#: ``your assessment|interview`` COMPLETES THE CATEGORY rather than chasing a
#: wording. Every clause here has the same shape — a hiring-process artefact
#: that belongs to the READER — and application, candidacy and offer were three
#: of five. The two that were missing are the two the product has statuses for.
#: An assessment reminder ("our team noticed you haven't had a chance to
#: complete your assessments yet") names no application and is unmistakably
#: about one; 58 of them reached nothing in the 16.8k corpus.
#:
#: WHAT WAS DELIBERATELY NOT ADDED, because the line matters. Eight real
#: rejections still reach nothing: the snippet cuts at "thank you so much for
#: your interest in <Employer> and for the time and effort you have invested in
#: our process", one character before "with your application". Adding
#: ``invested in our process`` or ``in our hiring process`` takes the corpus to
#: 633/633 with zero noise — and it would be transcribing one sender's sentence
#: into the product, which is the closed loop ``observed.py`` exists to break.
#: The wording is not a category, it is a phrase. Left open and pinned instead.
# THE SPAN BETWEEN THE ANCHOR AND THE KEYWORD IS A JOB TITLE, so the only safe
# bound on it is a CLAUSE. It used to be `[\w,\ \-/]{0,60}`, a character class
# holding no `(`, `)`, `:` or `#`, and real titles carry all four:
#
#   Software Engineer I, Entry-Level (Graduation Date: Fall 2025-Summer 2026)
#   Software Engineer, Agentic AI Harness & Quality - <Product>
#   Software Engineer, C#
#
# So the #447 floor — which exists precisely to stop mail about a real
# application reaching nothing — could not see the mail whose title carried
# punctuation the class forgot. 5 messages per corpus run, on title shapes taken
# from the owner's mailbox. See #466.
#
# EXTENDING THE CHARACTER CLASS WAS THE OBVIOUS FIX AND IS THE WRONG ONE. `C#`
# already needed a character nobody anticipated; the next real title needs
# another. `[^.!?\n]` says what is actually true — a title sits inside one
# clause — which is the same assumption `_ROLE_PATTERNS` above already makes.
#
# The widening is bounded by the corpus's 400 `ats-relay-noise` messages, which
# are relayed by the same domains and reference no application of the reader's:
# zero of them enter the queue with this in place.
# THE EMPLOYER SITS INSIDE THE PHRASE, and a literal two-word anchor cannot see
# it. `your application` was spelled adjacently, so "Update on your <Employer>
# application" — the single most ordinary ATS subject there is — did not
# reference an application as far as this floor was concerned.
#
# NOT FITTED TO A FIXTURE, which is the bar this file sets for itself two
# paragraphs up. The shape is transcribed real mail: `observed.py`'s
# OBSERVED_PENDING carries "[Action Required] Your {display} Application"
# verbatim from the owner's inbox. The corpus case that exposed it is
# `verdict-past-the-body-cap`, and it only became visible when #767 stopped the
# harness feeding the classifier a body production cannot deliver — the 160
# rejections there were reaching NOTHING: no card, no queue row, no counter.
#
# MEASURED AT THREE WIDTHS over 18,200 cases before choosing one. `{0,40}?`
# floors 160 of 160; `{0,25}?` floors 143 and misses the longer employer names;
# adjacency floors 0. Newly matching across the whole corpus: 611 messages,
# every one of them `identity is not None` — real application mail — and the
# 1,690 that must never become an application match at exactly the same count
# as before, 339, so the widening admits no noise THIS CORPUS CAN SEE. That
# qualifier is load-bearing; see `_NOT_THE_READERS_APPLICATION` below.
#
# `{0,80}` AND NOT `{0,40}`, matching the two spans beneath it rather than
# sitting at half their width for no stated reason.
#
# THE CORPUS CANNOT CHOOSE THIS NUMBER AND IT IS WORTH SAYING WHY. Measured over
# all 18,200 cases: 4,164 subjects carry a `your ... application` frame and the
# LONGEST intervener among them is 29 characters. So every bound from 30 upward
# passes every case the corpus has, and 40 was fitted to nothing but the
# generator's own invented employer names — whose longest, at 42 characters,
# the frame never even reaches (#737). Real names run past it: "Update on your
# Pricewaterhouse Coopers International Limited application" is 44 characters of
# intervener and misses at 40. Confirmed the widening is inert here rather than
# assumed: over the 1,266 messages where the floor is actually consulted (an ATS
# sender with an `other` verdict), 786 floor at 40 and 786 at 80.
#
# Same clause bound as the `[^.!?\n]` spans below and for the same reason
# (#466): a title, or an employer name, sits inside one clause and carries
# punctuation no character class anticipates.
_APPLICATION_REFERENCE = re.compile(
    r"""(?xi)
      your\ [^.!?\n]{0,80}?application\b
    | your\ candidacy\b
    | \b(?:your|the)\ offer\b
    | your\ (?:assessment|interview)s?\b
    | your\ interest\ in\ (?:the\ |this\ |our\ )?
      [^.!?\n]{0,80}?(?:opportunity|position|role|opening)\b
    | \bappl(?:y|ied|ying)\ (?:for|to)\ (?:the\ |a\ |an\ )?
      [^.!?\n]{0,80}?(?:opportunity|position|role|opening)\b
    """
)


#: The two shapes the widened first alternative reaches that are NOT the
#: reader's own application. Held apart from the pattern rather than folded into
#: it, because each needs its own control and a negative lookahead inside an
#: alternation gets one shared one.
#:
#: THE CORPUS CANNOT SEE EITHER, and that is why they are here rather than
#: absent. Measured: adding these changes zero of the 1,266 floor decisions the
#: corpus makes. Its `ats-relay-noise` family carries referral *asks* — mail
#: inviting the reader to refer somebody — and no referral *status* mail, so the
#: zero-noise-delta that licensed the widening was structurally unable to
#: contain the first shape. A clean corpus delta is necessary here and never
#: sufficient; this file already declines a wording that measured 633/633 with
#: zero noise, twenty lines up.
#:
#: 1. A POSSESSIVE THAT IS NOT THE READER'S. Greenhouse and Lever mail the
#:    REFERRER when a referral moves: "Update on your referral's application".
#:    Adjacency could not match it and the widened span can, so this is a
#:    genuine new admission and not a pre-existing one.
#: 2. THE PROCESS, NOT AN INSTANCE. "your interest in our application process"
#:    is a talent-community blast. Sharper than it looks: the fourth alternative
#:    in the pattern fences `your interest in ...` with
#:    `opportunity|position|role|opening` precisely to exclude process-general
#:    text, and the widened first alternative reaches around that fence.
#:
#: Cost of being wrong in either direction is one review-queue row — the only
#: caller is `ats_floor`, gated on an ATS sender and an `other` verdict, and
#: `_qualifies_for_hard_row` is untouched — so a message this refuses is not
#: lost, it takes the route it took before the widening.
#:
#: REAL-WORLD FREQUENCY OF BOTH IS UNMEASURED. They are excluded on structure —
#: a possessive names its owner, and "application process" is a mass noun — not
#: on a count, and that is said plainly rather than implied.
_NOT_THE_READERS_APPLICATION = re.compile(
    r"""(?xi)
      your\ [^.!?\n]{0,80}?['’]s\ application\b
    | application\ process(?:es)?\b
    """
)


def references_an_application(subject: str, snippet: str) -> bool:
    """Does this message speak about an application the reader made?

    Subject and snippet only, because that is all a cloud scan is guaranteed to
    have: the body is read in flight when one can be extracted and is not
    retained, and the messages this exists for are precisely the ones where no
    body part could be extracted.

    Carries no verdict. See :data:`_APPLICATION_REFERENCE` and
    :data:`_NOT_THE_READERS_APPLICATION`.
    """

    text = f"{subject} {snippet}"
    if _NOT_THE_READERS_APPLICATION.search(text):
        return False
    return bool(_APPLICATION_REFERENCE.search(text))


# --- WHY a message is in the review queue --------------------------------
#
# The queue has always shown a sentence explaining the hold. Until #507 that
# sentence was not read from the decision — the web INFERRED it from the
# confidence score alone, so every held row at/above the gate claimed "held
# for a missing employer name". On the owner's own board that was wrong on
# all three rows it appeared on: two named their employer in the subject and
# were held because the employer had several applications and the mail named
# no role, and the third named its employer in the sender display name and
# was refused by the relay check (#508).
#
# These are the reasons the hold actually has. They are strings rather than an
# Enum to match ``category`` and everything else this module hands across the
# wire, and because a new member must be able to reach an old web build as a
# string it can pass through rather than an import it cannot resolve.

#: The classifier offered no category at all — genuinely "we cannot tell".
HOLD_NO_PROPOSAL = "no_proposal"
#: Under ``AUTO_FILE_GATE``: a real proposal, not strong enough to file alone.
HOLD_BELOW_GATE = "below_gate"
#: Under ``REVIEW_FLOOR`` and kept ONLY because a known ATS relayed it (#166).
HOLD_ATS_FLOOR = "ats_floor"
#: Clears the gate; no employer could be named ANYWHERE, body included. The
#: ONLY reason that may say "missing employer".
HOLD_NO_EMPLOYER = "no_employer"
#: Clears the gate; the filing path could not name the employer, but the body
#: does. The user can read it off the message, so "we couldn't name it" reads
#: as a lie (#512) — the honest ask is to confirm the name we found. The
#: proposal travels beside the reason as ``suggested_employer``.
HOLD_CONFIRM_EMPLOYER = "confirm_employer"
#: Clears the gate, but this category is never filed on its own — "other",
#: "needs_review", or a ``follow_up``, which :func:`_qualifies_for_hard_row`
#: excludes by name. Nothing about the employer or the score is the obstacle.
HOLD_NOT_FILEABLE = "not_fileable"
#: Clears the gate, employer known, role unknown, and that employer holds
#: several applications — so no single row can own it (#484).
HOLD_WHICH_APPLICATION = "which_application"
#: Clears the gate and none of the above fits. Deliberately not folded into a
#: neighbour: an unexplained hold is a bug, and naming it is how it surfaces.
HOLD_GATED_OTHER = "gated_other"

HOLD_REASONS: frozenset[str] = frozenset(
    {
        HOLD_NO_PROPOSAL,
        HOLD_BELOW_GATE,
        HOLD_ATS_FLOOR,
        HOLD_NO_EMPLOYER,
        HOLD_CONFIRM_EMPLOYER,
        HOLD_NOT_FILEABLE,
        HOLD_WHICH_APPLICATION,
        HOLD_GATED_OTHER,
    }
)


def hold_reason(
    *,
    confidence: float | None,
    subject: str,
    sender_email: str,
    sender_name: str | None = None,
    snippet: str = "",
    has_proposal: bool = True,
    sibling_applications: int = 0,
    category: str | None = None,
    stored_role: str | None = None,
) -> str:
    """Why this message is waiting for a human, as one of :data:`HOLD_REASONS`.

    Derived from the SAME functions the sync used to hold it —
    :func:`resolve_employer` and :func:`role_from_message` — rather than from a
    parallel reading of the row. That is the whole point: a reason computed by
    re-deriving the inputs can disagree with the decision it claims to explain,
    and this repo has the scar from a twin that re-created what it should have
    called.

    ``sibling_applications`` is how many live applications the caller found at
    this message's employer, and it is an ARGUMENT rather than a lookup because
    this module does no I/O. Zero is the safe answer: it can only ever move a
    row off :data:`HOLD_WHICH_APPLICATION`, never onto it, so a caller that
    cannot count them degrades to a vaguer reason instead of a wrong one.

    ``has_proposal`` is whether the classifier named a category at all. It is
    NOT derivable from ``classified_as``: everything in this queue is stored as
    ``needs_review``, which is the typed null, and the proposal lives in
    ``suggested_category``.

    PRECEDENCE IS THE MEANING. The gate splits the two questions the queue
    asks. At or above it the classifier was confident and something else
    stopped the filing, so the reason names that obstacle and the user's job is
    to remove it. Below it the classifier itself was unsure, so the user's job
    is to decide. Reading those in the wrong order tells a confident row that
    its problem is confidence.

    WHAT ``HOLD_GATED_OTHER`` MEANS ONCE THE BRANCH ABOVE IS COMPLETE. With all
    three of ``_qualifies_for_hard_row``'s refusals modelled, a row reaching the
    fallthrough is one the CURRENT code would file — the read path can find no
    obstacle at all. That is not a shrug about this message; it is the signature
    of a verdict written by an OLDER build and never revisited, because a
    routine sync resumes from a ``historyId`` cursor and re-reads nothing
    (#474). The reason is kept rather than folded into a neighbour precisely so
    that state stays visible: smoothing it away would rebuild #507's habit of
    printing a plausible sentence instead of a true one.
    """

    score = confidence if isinstance(confidence, (int, float)) else 0.0

    if score >= AUTO_FILE_GATE:
        # ABOVE THE GATE, THIS BRANCH MIRRORS ``_qualifies_for_hard_row`` — the
        # predicate that actually decides whether a message may file — IN ITS
        # OWN ORDER. That function refuses on three grounds and this used to
        # model only two of them, which is why it needed a fallthrough at all.
        #
        # A confident verdict with no proposal is "confident that it cannot
        # tell", and ``HOLD_NO_PROPOSAL`` is its exact meaning; reading the gate
        # first sent it to the fallthrough instead.
        if not has_proposal:
            return HOLD_NO_PROPOSAL
        # ``_qualifies_for_hard_row``'s FIRST test, which was missing here: a
        # category outside the lifecycle set, or a ``follow_up``, is never filed
        # on its own however confident it is. Nothing about the employer or the
        # score is the obstacle, and reporting one of those is a false lead.
        # ``None`` means the caller could not supply a category, and skipping
        # the test is the safe degradation — it can only widen the fallthrough,
        # never mislabel a row.
        if category is not None and (
            category not in JOB_LIFECYCLE_CATEGORIES or category == "follow_up"
        ):
            return HOLD_NOT_FILEABLE
        if resolve_employer(sender_email, subject, sender_name) is None:
            # The filing path cannot name it. Before saying so — the sentence
            # #512 is about — check whether the body names it anyway, because
            # the user is looking at that body and can read it.
            if employer_named_in_body(snippet, sender_email) is not None:
                return HOLD_CONFIRM_EMPLOYER
            return HOLD_NO_EMPLOYER
        # Role absent is only a REASON when it is also a problem. One
        # application at this employer and the mail lands on it regardless
        # (rule 3 of ``partition_applications``), so an unnameable role there
        # holds nothing up and must not be reported as though it did.
        #
        # ``stored_role`` outranks re-derivation: the sync wrote it from the
        # FULL body, while ``snippet`` here is the ~200 stored characters. A
        # role living past that boundary is present in the column and absent
        # from the snippet, and re-deriving would report "which application?"
        # about a row the sync itself placed without trouble.
        role = stored_role if stored_role else role_from_message(subject, snippet)
        if role is None and sibling_applications >= 2:
            return HOLD_WHICH_APPLICATION
        return HOLD_GATED_OTHER

    if not has_proposal:
        return HOLD_NO_PROPOSAL
    # Below the review floor at all, a lifecycle verdict is dropped outright;
    # reaching the queue from down here means the ATS floor caught it.
    if score < REVIEW_FLOOR and is_ats_sender(sender_email):
        return HOLD_ATS_FLOOR
    return HOLD_BELOW_GATE


def collect_review_items(
    items: Iterable[PipelineItem],
    dropped_out: list[DroppedVerdict] | None = None,
    known_multi: frozenset[str] = frozenset(),
    known_threads: frozenset[str] = frozenset(),
) -> list[ReviewItem]:
    """Return the uncertain lifecycle verdicts that need a human decision.

    An item is review-worthy when it is NOT a hard-row contributor and either:
      - the classifier explicitly emitted ``needs_review``, or
      - it is a lifecycle verdict (not follow-up) at/above the review floor
        (0.70) — including one that clears the gate but whose employer could not
        be named (skipping is better than inventing a company), or
      - it is a lifecycle verdict (not follow-up) relayed by a known ATS, at ANY
        confidence — the ATS floor, see below, or
      - a known ATS relayed it and the verdict is ``follow_up``, at ANY
        confidence. ``follow_up`` means the READER'S OWN chasing mail, which is
        why it is dropped everywhere else; a relay does not carry the reader's
        own mail, so on that sender the verdict is a category error and
        dropping it silently loses the sender's message (#458).

    Anything below the review floor, or plain ``other`` noise, is omitted. That
    drop is terminal — the one path through this module that leaves no row and
    no queue entry behind — so it reports itself two ways:

      - a LIFECYCLE verdict under the floor is always logged AND appended to
        ``dropped_out`` when the caller passes a list. This is mail the
        classifier believed was about a job application and the pipeline threw
        away; it is the failure case, not the designed one, and it is what lets
        a sync say "4 discarded" instead of nothing at all. See
        :class:`DroppedVerdict` for the four applications that were lost to its
        previous silence.
      - anything else is logged only when the classifier was CONFIDENT
        (at/above ``AUTO_FILE_GATE``) — a confident ``follow_up``, dropped by
        design, or a category outside the canonical vocabulary, which is a bug.

    ``dropped_out`` is an out-parameter rather than a second return value so
    every existing caller keeps unpacking a plain list.

    Deduplicated by THREAD AND WHICH APPLICATION the message names (newest
    wins), falling back to ``message_id`` for mail with no thread id. One Gmail
    conversation is one decision *per application*: the owner's queue asked them
    to classify "Crusoe | Application Received" twice (emails 58 and 73 — two
    messages, one thread ``19fed7e0706ee704``), and keying on the thread alone
    fixed that by losing three of the four applications in Verkada's thread
    ``19ff36237eef1ef3``. See #454 and the comment at the key itself. Newest-
    first overall.
    """

    items = list(items)
    # Gated mail that names no role at an employer holding several applications.
    # It clears the precision gate, so the loop below would skip it as "already a
    # real application row" — but there is no single row it belongs to, and
    # picking one would settle the wrong application (see
    # :func:`partition_applications`). Asking is the only honest move.
    unplaceable = unplaceable_message_ids(items, known_multi, known_threads)

    best: dict[tuple[str, str | None] | str, ReviewItem] = {}
    for item in items:
        if item.message_id not in unplaceable and _qualifies_for_hard_row(item) is not None:
            continue  # already a real application row

        is_needs_review = item.category == "needs_review"
        is_lifecycle = (
            item.category in JOB_LIFECYCLE_CATEGORIES and item.category != "follow_up"
        )
        # THE ATS FLOOR — issue #166.
        #
        # Mail relayed by a known ATS is never dropped silently. A cloud scan
        # classifies from Gmail's ~200-character ``snippet``, and an ATS
        # rejection spends that entire budget on a polite preamble — so the
        # classifier reads a CONFIRMATION and scores it as one. Whether the
        # message clears ``REVIEW_FLOOR`` at all then comes down to whether its
        # SUBJECT happens to contain a confirmation phrase: Verkada's did (+2,
        # 0.70, the queue), Together AI's did not (0.60, gone). #166 is that
        # knife-edge, and #238 proved it by execution.
        #
        # What this does and does not do. It routes to the HUMAN REVIEW QUEUE
        # and nothing else — it never files a row, never asserts a status and
        # never writes a verdict, because ``_qualifies_for_hard_row`` above still
        # requires ``AUTO_FILE_GATE`` and is untouched. So a floored message
        # cannot make the board confidently WRONG, which is the failure mode
        # that ruled out the obvious alternative fix (adding Greenhouse's
        # rejection subject template as a pattern scores the same message
        # ``applied`` at +6 and would auto-file a rejection as APPLIED).
        #
        # Bounded three ways, so "never dropped" cannot become "queue floods":
        #   - LIFECYCLE ONLY. ``other`` — which is what a classifier miss and
        #     ATS job-alert noise both produce — still drops, and so does a
        #     category outside the canonical vocabulary, which stays a logged
        #     bug rather than becoming a queue entry.
        #   - ``follow_up`` stays excluded, exactly as it is above the floor.
        #   - the sender must be on ``rules.ATS_DOMAINS``, a closed list of
        #     transactional relays. Ordinary company and personal mail below the
        #     floor is dropped exactly as before.
        #
        # Known residual as of #166, stated rather than hidden: an ATS message
        # that scores NOTHING in any category is ``other`` and still drops.
        # Covering that means queueing mail on the strength of its sender alone,
        # which is a wider decision than #166 needs.
        #
        # THAT RESIDUAL IS NOW COVERED — see the clause below and #447. It was
        # not theoretical: it was 610 messages in the 15k corpus, every one about
        # a real application, reaching no card, no queue and no counter. What
        # made covering it safe was finding a signal narrower than the sender,
        # which is ``references_an_application``.
        # #447 WIDENS THIS BY ONE CLAUSE, and only one. A message an ATS relayed
        # that scores no lifecycle category at all is queued when — and only
        # when — its own text refers to an application the reader made. That is
        # the residual named four paragraphs up, and the clause is what keeps
        # covering it from becoming "queue on the sender alone": the corpus's
        # 400 ``ats-relay-noise`` messages are relayed by the same domains,
        # score the same ``other`` 0.50, and reference nothing of the reader's,
        # so they still drop.
        #
        # Still routes to the HUMAN QUEUE and nothing else: everything the
        # paragraph above says about not filing a row, not asserting a status
        # and not writing a verdict holds unchanged, because
        # ``_qualifies_for_hard_row`` is untouched and still wants
        # ``AUTO_FILE_GATE``. A referenced message cannot make the board wrong;
        # it can only get a person asked.
        # SCOPED TO ``other``, and that scope is load-bearing rather than tidy.
        # The three shapes #166 deliberately drops each drop for their own
        # reason, and only ONE of them is what #447 is about:
        #
        #   · ``other`` — a classifier miss. THIS is the 610, and the reference
        #     clause is what separates them from the ATS noise that also lands
        #     here.
        #   · ``follow_up`` — excluded from filing AND from the queue by design,
        #     above the floor as well as below it. It is the user's own chasing
        #     mail; queueing it asks them to classify themselves. THAT REASON
        #     DOES NOT HOLD WHEN AN ATS RELAYED THE MESSAGE — see the clause
        #     below and #458.
        #   · a category outside the canonical vocabulary — a BUG, whose
        #     contract is that it is LOGGED rather than turned into a queue
        #     entry. Queueing it would hide the bug behind a plausible row.
        #
        # An earlier draft of this wrote ``is_lifecycle or references(...)``,
        # which reversed the second and third as a side effect and was caught by
        # `test_the_floor_does_not_swallow_the_shapes_that_must_stay_dropped`
        # — the test for #166 doing its job on #447's change.
        #
        # #458 ADDS ONE MORE CLAUSE, AND IT IS A DIRECTION ARGUMENT rather than
        # a wording one. ``follow_up`` is dropped everywhere on one premise,
        # stated in the bullet above and in that test: it is the READER'S OWN
        # chasing mail, so asking them to classify it asks them to classify
        # themselves. A message an applicant tracking system relayed is not
        # mail the reader sent. The premise is simply false for it, and what
        # the exclusion then destroys is the sender's message.
        #
        # WHAT THIS COSTS TODAY, measured rather than reasoned about
        # (17,260-message independent corpus, 2026-08-29):
        #
        #   ``follow_up`` verdicts in the whole corpus            11
        #   ...of those, relayed by a domain on ``ATS_DOMAINS``   11
        #   ...of those, whose ground truth is ``rejection``      11
        #
        # Every one is a real transcribed rejection whose subject carries the
        # sender's own word "Follow-Up" and whose verdict sentence sits past
        # Gmail's ~186-character snippet cut — one character past, so the
        # rejection veto that outranks the follow-up pattern never fires and
        # 0.70 ``follow_up`` is what the classifier returns. Delivered whole,
        # the same message is ``rejection`` at 0.95. These 11 are the residue
        # of #447 and they reached NOTHING: not a card, because
        # ``_qualifies_for_hard_row`` refuses ``follow_up`` at any confidence;
        # not the queue, because of the bullet above; and not even a
        # ``DroppedVerdict``, because that is scoped to lifecycle categories
        # and ``follow_up`` is excluded from those too. Three instruments, one
        # blind spot, and the message is indistinguishable from mail that never
        # arrived.
        #
        # NOT SCOPED BY CONFIDENCE, deliberately. The previous attempt on this
        # shape demoted ``follow-?up`` from a strong pattern to a weak one,
        # which moved the score from 0.90 to 0.70 and changed nothing at all:
        # the exclusion is CATEGORICAL, so no score can escape it. A clause
        # that reads the confidence would rebuild that.
        #
        # NOT IN ``classify`` EITHER, and that is the same boundary #447
        # respected. ``rules.py`` is ported byte-for-byte into
        # ``apps/web/lib/demo/rules.json`` and ``ml/browser/site/rules.json``,
        # so a verdict-changing rule there is two more artefacts and the demo
        # gate; and the corpus holds no CORRECT ``follow_up`` case anywhere, so
        # it cannot grade a change to how ``follow_up`` is detected. This
        # changes no verdict. It changes who gets asked, which is this
        # function's job.
        #
        # THE CONTROL IS THE ONE #447 ALREADY BUILT, and it is a measured zero
        # rather than a structural one: of the corpus's 400 ``ats-relay-noise``
        # messages — job alerts, talent-community blasts, profile nudges,
        # surveys, referral asks, all from these same relay domains — exactly
        # ZERO score ``follow_up``. So this clause cannot touch them. Nor can
        # it touch a legitimate ``follow_up``, which is mail the user sent: it
        # does not arrive from a relay.
        #
        # Still the queue and nothing else. ``_qualifies_for_hard_row`` is
        # untouched and still refuses ``follow_up`` outright, so a message
        # arriving here can only get a person asked — it can never file a
        # rejection as a follow-up, or as anything.
        ats_floor = is_ats_sender(item.sender_email) and (
            is_lifecycle
            or (
                item.category == "other"
                and references_an_application(item.subject, item.snippet)
            )
            or item.category == "follow_up"
        )
        if (
            not is_needs_review
            and not ats_floor
            and not (is_lifecycle and item.confidence >= REVIEW_FLOOR)
        ):
            # THE ONLY TERMINAL DROP IN THE PIPELINE, and until now a silent one.
            #
            # An item that gets here produces nothing at all: no application row
            # (:func:`partition_applications` skipped it), no queue row, no
            # counter, no log. A verdict the classifier was CONFIDENT about
            # leaving by that door is worth a line, because the absence of one is
            # how three separate persistence drops shipped without anyone
            # noticing the product had recorded zero non-applied statuses.
            #
            # Gated at the auto-file threshold rather than logged unconditionally,
            # and that gate does real work: the cloud rules classifier returns
            # ``other`` at confidence 0.0, so ordinary inbox noise — the bulk of
            # every scan — cannot reach this line. What does reach it is a
            # confident ``follow_up`` (0.90 on "Following up on my application"),
            # which is dropped BY DESIGN, and any category outside the canonical
            # vocabulary, which is a bug. Both are things you want to see.
            #
            # Volume, stated honestly: this is per SYNC, not per message. A
            # confident follow_up that stays inside the scan window is logged
            # again on every sync, indefinitely — messages x syncs, not messages.
            # Bounded and cheap, but do not read a repeated line as a new drop.
            #
            # Reporting only. Nothing below this line changes what is returned.
            #
            # A LIFECYCLE verdict leaving here is an ACCIDENT and is always
            # logged and always counted. It is mail the classifier itself
            # believed was about a job application, discarded for scoring below
            # ``REVIEW_FLOOR``. The old gate was ``>= AUTO_FILE_GATE``, which is
            # backwards for this purpose: the CONFIDENT drops are the designed
            # ones (``follow_up``), and the unconfident ones are the failures.
            # Four of them cost the owner four Microsoft applications in
            # silence; see :class:`DroppedVerdict`.
            #
            # Volume stays bounded because this is the lifecycle branch only.
            # ``other`` — inbox noise, and the bulk of every scan — takes the
            # ``elif`` and stays silent unless it was confident, exactly as
            # before. A lifecycle verdict under the floor is rare by
            # construction: it needs a real category AND a score too weak to
            # queue.
            if is_lifecycle:
                if dropped_out is not None:
                    dropped_out.append(
                        DroppedVerdict(
                            message_id=item.message_id,
                            category=item.category,
                            confidence=item.confidence,
                        )
                    )
                logger.warning(
                    "Pipeline dropped a lifecycle verdict BELOW the review "
                    "floor: category=%s confidence=%.2f message_id=%s. It "
                    "scored under %.2f and its sender is not a known ATS relay, "
                    "so it produced no application row and no review-queue "
                    "entry. This is mail the classifier thought was about a job "
                    "application.",
                    item.category,
                    item.confidence,
                    item.message_id,
                    REVIEW_FLOOR,
                )
            elif item.confidence >= AUTO_FILE_GATE:
                # Category, confidence and message id — the three facts the
                # brief above asks for. The sender's ADDRESS used to ride along
                # and no longer does: it is the user's correspondent, it is
                # mail-derived, and the message id already names the message it
                # came from (see ``_warn_if_capped`` in cloud/applications.py).
                logger.warning(
                    "Pipeline dropped a confident verdict: category=%s "
                    "confidence=%.2f message_id=%s. It is neither a "
                    "lifecycle category that can be filed nor needs_review, so "
                    "it produced no application row and no review-queue entry.",
                    item.category,
                    item.confidence,
                    item.message_id,
                )
            continue

        # Named for the queue whenever the message got here on a job-mail
        # signal — a lifecycle verdict, or the #447 reference clause. Gating this
        # on ``is_lifecycle`` alone would have put every referenced ``other``
        # into the queue with no company against it, which is a worse row to
        # hand a person than the one they get now: the whole point of queueing
        # these is that a human can act on them.
        employer = (
            resolve_employer(item.sender_email, item.subject, item.sender_name)
            if (is_lifecycle or ats_floor)
            else None
        )
        candidate = ReviewItem(
            message_id=item.message_id,
            thread_id=item.thread_id,
            subject=item.subject,
            sender_email=item.sender_email,
            sender_name=item.sender_name,
            # Naive-UTC — this persists straight into Email.received_at.
            received_at=to_naive_utc(item.received_at),
            category=item.category,
            confidence=item.confidence,
            company_display=employer[1] if employer else None,
            snippet=item.snippet,
        )
        key = review_dedup_key(
            message_id=item.message_id,
            thread_id=item.thread_id,
            subject=item.subject,
            snippet=item.snippet,
            identity_role=item.identity_role,
            identity_req_id=item.identity_req_id,
        )
        current = best.get(key)
        if current is None or _review_sort_key(candidate) >= _review_sort_key(current):
            best[key] = candidate

    return sorted(best.values(), key=_review_sort_key, reverse=True)


def _review_sort_key(item: ReviewItem) -> datetime:
    """Newest-first ordering key that never compares aware to naive."""

    return _as_utc(item.received_at) if item.received_at else _EPOCH


def gmail_deeplink(
    *,
    thread_id: str | None = None,
    message_id: str | None = None,
    account_email: str | None = None,
) -> str | None:
    """Build a stable Gmail web deep link for a thread/message, or None.

    Prefers the conversation (``#all/<threadId>``) so the whole thread opens;
    falls back to the message id. Uses the ``#all/`` anchor so archived mail is
    still reachable. We only have Gmail API ids (never the RFC822 header), which
    the ``#all/`` fragment resolves directly.

    ``account_email`` — the CONNECTED Gmail account. When known we select it with
    ``?authuser=<email>`` rather than the positional ``/u/0/`` slot. ``/u/0/`` is
    the FIRST account in the browser's session, which is almost never the linked
    mailbox for a user signed into several Google accounts — the reported bug
    where "Open in Gmail" dumped the user into the wrong inbox. ``authuser`` with
    the exact address is Google's robust multi-account selector; the ``/u/0/``
    form is kept only as the fallback when the account is unknown.
    """

    ref = (thread_id or "").strip() or (message_id or "").strip()
    if not ref:
        return None
    email = (account_email or "").strip()
    if email:
        return (
            f"https://mail.google.com/mail/?authuser={urllib.parse.quote(email)}"
            f"#all/{ref}"
        )
    return f"https://mail.google.com/mail/u/0/#all/{ref}"


def retarget_gmail_deeplink(url: str | None, account_email: str | None) -> str | None:
    """Point an existing stored Gmail deep link at the connected account.

    A persisted ``Application.url`` was minted with the positional ``/u/0/``
    account (or an older connection). Rewriting the account selector to
    ``?authuser=<connected-email>`` at READ time makes an "Open in Gmail" click
    always land in the mailbox the user has linked *now* — healing rows written
    before this fix without needing a re-sync, and following a reconnection to a
    different account. The message/thread fragment is preserved verbatim, so the
    same conversation still opens. Non-Gmail or fragment-less urls pass through
    unchanged; when no account is known the url is returned as-is.
    """

    if not url or not account_email or "mail.google.com" not in url:
        return url
    marker = url.find("#")
    if marker == -1:
        return url
    email = account_email.strip()
    if not email:
        return url
    fragment = url[marker:]  # keep '#all/<ref>' (or '#search/…') exactly
    return (
        f"https://mail.google.com/mail/?authuser={urllib.parse.quote(email)}{fragment}"
    )
