"""The company lookup's index, verified where the planner actually lives.

``_company_rows`` filters on ``lower(company)``, twice. Production's only
company index was ``ix_applications_company`` on the RAW column, which cannot
serve either predicate — the index stores the original bytes and ``lower()`` is
not order-preserving over them. Migration ``e7a1c4d92b30`` adds the functional
index that can.

WHY THIS MODULE HAS TO BE A POSTGRES MODULE
-------------------------------------------
Everything about this fix is a *planner* fact. SQLite has no
``text_pattern_ops``, no ``EXPLAIN`` output of this shape, and its query planner
makes different choices — a SQLite assertion here would be green regardless of
whether the production index works, which is the "check that cannot fail" shape.

WHAT IS ASSERTED, AND WHAT IS NOT
---------------------------------
NOT a speed-up. ``applications`` holds 65 live rows in production, three orders
of magnitude below where any planner prefers an index to a scan, so no honest
before/after timing exists to measure and none is claimed. What is asserted is
the property that matters before real users arrive: given a table large enough
to *want* an index, the planner **can** use this one — for BOTH predicates.

The second half is the one that would have shipped wrong. Under a non-C
collation (Supabase is ``en_US.utf8``) a DEFAULT btree cannot push
``LIKE 'prefix%'`` into an index condition. ``test_the_default_opclass_would_not_have_worked``
builds that index and proves it: same table, same query, prefix left in
``Filter``. The green assertion above it is only meaningful because that red one
holds.
"""

from __future__ import annotations

import os
import random
import string
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.pg_support import reset_public_schema, resolve_admin_url, sync_url

BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

INDEX_NAME = "ix_applications_user_id_lower_company"

# Big enough that a sequential scan is genuinely the more expensive plan. At the
# production row count (65) Postgres would correctly ignore every index here,
# and a test seeded that way would assert nothing.
SEED_ROWS = 200_000

USER = uuid.UUID("3c9f1b52-7d0a-4e63-9f18-5b2c84a7e011")


ADMIN_URL, _OWNED_CONTAINER = resolve_admin_url()

# NO ``teardown_module`` stopping the container: it is SHARED with
# ``test_cascade_delete_postgres`` (see tests/pg_support.py), so whichever
# module finished first would pull the server out from under the other.
# A throwaway container dies with the pytest process.

pytestmark = pytest.mark.skipif(
    not ADMIN_URL,
    reason=(
        "No Postgres available: set JOBTRACKER_TEST_PG_ADMIN_URL, or run Docker "
        "so a throwaway postgres:16 can be started. Skipping leaves the "
        "lower(company) index UNVERIFIED — the SQLite suites have no "
        "text_pattern_ops and no comparable planner, so nothing else in this "
        "repo can see whether it works."
    ),
)


@pytest.fixture(scope="module")
def seeded_engine():
    """A migrated database with a table large enough to prefer an index.

    The schema comes from ``alembic upgrade head`` — the real chain, in a
    subprocess, exactly as ``test_migrations_postgres.py`` runs it — so this
    tests the index the migration actually creates rather than one the test
    typed out itself. That distinction is the whole point: a hand-written
    ``CREATE INDEX`` here would stay green if the migration were deleted.
    """

    url = sync_url(ADMIN_URL)
    engine = create_engine(url, future=True)

    # Take the schema for this module. Under CI every Postgres suite shares one
    # database, so a module that skipped this would inherit the previous one's
    # tables and its `upgrade head` would fail with "relation already exists" —
    # a test-ordering problem wearing the costume of a broken migration.
    reset_public_schema(engine, owner_ids=(USER,))

    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=dict(os.environ, DIRECT_URL=url),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"alembic upgrade head failed ({proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )

    with engine.begin() as conn:
        # RLS is FORCEd on applications by the chain; the seeding role must not
        # be blocked by a policy that has no JWT to evaluate.
        conn.execute(text("ALTER TABLE applications DISABLE ROW LEVEL SECURITY"))
        conn.execute(
            text(
                # Company names spread over 9,000 distinct leading words, so a
                # prefix is SELECTIVE. This matters: seeded with one repeated
                # name, `LIKE 'company%'` matches every row and a sequential
                # scan is genuinely the cheaper plan — the test would then be
                # measuring the planner being right, not the index being
                # unusable.
                "INSERT INTO applications "
                "(user_id, company, position, status, source, created_at, updated_at) "
                "SELECT :uid, 'Corp' || (i % 9000) || ' Holdings', 'Engineer', "
                "'APPLIED', 'gmail', now(), now() "
                "FROM generate_series(1, :n) AS i"
            ),
            {"uid": USER, "n": SEED_ROWS},
        )
        conn.execute(text("ANALYZE applications"))

    yield engine
    engine.dispose()


def _plan(engine, sql: str) -> str:
    with engine.connect() as conn:
        return "\n".join(
            r[0] for r in conn.execute(text(f"EXPLAIN (COSTS OFF) {sql}"), {"uid": USER})
        )


# The two predicates _company_rows issues. Kept as literals rather than built
# from the ORM so a refactor of the helper cannot quietly change what is being
# measured.
#
# ``company`` is VARCHAR, so Postgres renders the expression as
# ``lower((company)::text)`` in both the index definition and every plan —
# hence EXPR below rather than a bare "lower(company)" in the assertions.
EXPR = "lower((company)::text)"

EQUALITY = (
    "SELECT * FROM applications "
    "WHERE user_id = :uid AND lower(company) = 'corp42 holdings'"
)
PREFIX = (
    "SELECT * FROM applications "
    "WHERE user_id = :uid AND lower(company) LIKE 'corp8123' || '%'"
)


def test_the_migration_creates_the_functional_index(seeded_engine):
    """It exists, on the expression and opclass the fix depends on."""

    with seeded_engine.connect() as conn:
        ddl = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
            {"n": INDEX_NAME},
        ).scalar()

    assert ddl is not None, f"{INDEX_NAME} was not created by the chain"
    assert EXPR in ddl.lower()
    assert "text_pattern_ops" in ddl.lower(), (
        "the index exists but in the DEFAULT operator class, which cannot serve "
        f"a prefix LIKE under a non-C collation: {ddl}"
    )


def test_the_planner_uses_it_for_the_equality_predicate(seeded_engine):
    plan = _plan(seeded_engine, EQUALITY)

    assert INDEX_NAME in plan, f"equality fell back to a scan:\n{plan}"
    assert "Seq Scan" not in plan, plan
    assert f"{EXPR} = " in plan.lower(), (
        f"the index was touched but lower(company) is not an Index Cond:\n{plan}"
    )


def test_the_planner_uses_it_for_the_prefix_predicate(seeded_engine):
    """The half that needs ``text_pattern_ops``.

    Asserting only "the index appears in the plan" would pass for the broken
    default-opclass index too — it gets used for the ``user_id`` column alone
    while the prefix stays a Filter. So the assertion is on the RANGE operators
    a pattern-opclass index scan emits (``~>=~`` / ``~<~``), which is what
    "the prefix is an index condition" actually looks like.
    """

    plan = _plan(seeded_engine, PREFIX)

    assert INDEX_NAME in plan, f"prefix fell back to a scan:\n{plan}"
    assert "~>=~" in plan and "~<~" in plan, (
        "the prefix is not an index condition — it is being re-checked as a "
        f"Filter over every row of the user:\n{plan}"
    )


def test_the_default_opclass_would_not_have_worked(seeded_engine):
    """PROVE THE INSTRUMENT — and the design decision behind the migration.

    Builds the index the obvious way (default operator class) and shows the
    prefix predicate is NOT pushed into it. Without this, the green test above
    is just an assertion that some index exists, and the ``text_pattern_ops``
    in the migration reads as superstition.
    """

    naive = "ix_applications_default_opclass_probe"
    with seeded_engine.begin() as conn:
        # Drop the real index for the duration so the planner has only the
        # naive one to choose from — otherwise it simply picks the good one and
        # the test proves nothing.
        conn.execute(text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
        conn.execute(
            text(f"CREATE INDEX {naive} ON applications (user_id, lower(company))")
        )
        conn.execute(text("ANALYZE applications"))
    try:
        plan = _plan(seeded_engine, PREFIX)
        assert "~>=~" not in plan, (
            "the default opclass DID serve the prefix — this database's "
            f"collation is not the production one, so the gate is void:\n{plan}"
        )
        # However the planner copes without a usable prefix range — a seq scan
        # here, a user_id-only bitmap scan on a board with several users — the
        # predicate lands in a re-check Filter rather than an index condition.
        assert "Filter:" in plan and f"{EXPR} ~~" in plan, (
            f"expected the prefix to be left as a re-check Filter:\n{plan}"
        )
    finally:
        with seeded_engine.begin() as conn:
            conn.execute(text(f"DROP INDEX IF EXISTS {naive}"))
            conn.execute(
                text(
                    f"CREATE INDEX {INDEX_NAME} ON applications "
                    "(user_id, lower(company) text_pattern_ops)"
                )
            )
            conn.execute(text("ANALYZE applications"))


def test_the_collation_is_the_production_one(seeded_engine):
    """The whole opclass argument is collation-dependent; pin the assumption.

    If this database were ``C``, a default btree WOULD serve the prefix and
    every conclusion above would be an artefact of the fixture rather than a
    fact about production.
    """

    with seeded_engine.connect() as conn:
        collation = conn.execute(
            text(
                "SELECT datcollate FROM pg_database WHERE datname = current_database()"
            )
        ).scalar()

    assert collation and not collation.startswith("C"), (
        f"test database collation is {collation!r}; production (Supabase) is "
        "en_US.utf8 and this suite's conclusions do not transfer"
    )




# =============================================================================
# The ceiling those indexes impose on the column (issue #406)
# =============================================================================
#
# A btree version 4 index row may not exceed 2704 bytes, and ``company`` is in
# two of them. Nothing bounded the field, so ``POST /applications`` answered
# 201 to a 5,000,000-character company and the INSERT was the thing that broke:
#
#     company len=2000 -> INSERT OK
#     company len=2700 -> ProgramLimitExceeded: index row size 2712 exceeds
#                         btree version 4 maximum 2704
#     smallest rejected INCOMPRESSIBLE company: 2677 characters
#
# ``CloudApplicationCreate.company`` carries ``max_length=_MAX_COMPANY_LEN``
# now, so the API refuses with 422 before the database is asked. That refusal is
# tested on SQLite in ``test_application_create_is_bounded.py``, which cannot
# see this ceiling at all — SQLite has no index-row limit. These two tests are
# the half that can: the bound is inserted for real, and the length it protects
# against is inserted for real too.
#
# THE FIXTURES ARE RANDOM BECAUSE THE CEILING IS ABOUT STORED BYTES. Postgres
# compresses a varlena index datum before measuring it, so ``"C" * 2700``
# INSERTS FINE — measured here, and the reason the issue records the smallest
# rejected length as an *incompressible* one. A repeated character would have
# made the first test below silently vacuous, which is the failure shape this
# module's docstring is already about.

_COMPANY_PROBE_USER = uuid.UUID("5f2b7c19-3a84-4d61-8e07-6c9a1b3d4f22")


def _incompressible(n: int, *, four_byte: bool = False) -> str:
    """``n`` characters that pglz cannot shrink, seeded so a red is reproducible.

    ``four_byte=True`` draws from CJK Extension B, every code point of which is
    four bytes in UTF-8 — the worst exchange rate between the CHARACTER count
    ``max_length`` enforces and the BYTE count the index measures.
    """

    rng = random.Random(406)
    if four_byte:
        return "".join(chr(rng.randrange(0x20000, 0x2A6DF)) for _ in range(n))
    alphabet = string.ascii_letters + string.digits
    return "".join(rng.choice(alphabet) for _ in range(n))


def _insert_company(engine, company: str) -> None:
    """One INSERT in its own transaction, so a failure cannot poison the next."""

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO applications "
                "(user_id, company, position, status, source, created_at, updated_at) "
                "VALUES (:uid, :company, 'Engineer', 'APPLIED', 'manual', now(), now())"
            ),
            {"uid": _COMPANY_PROBE_USER, "company": company},
        )


@pytest.fixture
def probe_rows(seeded_engine):
    """Clear this user's rows either side of a probe.

    Either side, not just after: the first draft left a row behind when its
    INSERT unexpectedly SUCCEEDED, and the next test read that row instead of
    its own and failed with a confusing diff rather than an honest one.
    """

    def clear():
        with seeded_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM applications WHERE user_id = :uid"),
                {"uid": _COMPANY_PROBE_USER},
            )

    clear()
    yield seeded_engine
    clear()


def test_the_length_the_api_now_refuses_really_does_break_the_insert(probe_rows):
    """PROVE THE INSTRUMENT. Without this, the bound is superstition.

    If this database's indexes did not have the ceiling, the test below would
    assert only that a short string fits in a large index — true of any schema,
    and no evidence at all that ``_MAX_COMPANY_LEN`` was needed.

    THE CHECK CONSTRAINT NOW STANDS IN FRONT OF THE CEILING (#738), and this
    test had to be rewritten rather than re-baselined. Once
    ``ck_applications_company_len`` existed, the 2,700-character INSERT was
    refused by the CHECK and the assertion below started reading
    ``CheckViolation`` instead of ``index row size``. Changing the expected
    string to match would have kept the test green while deleting the only
    evidence in this repository that the btree ceiling is real — and the
    ceiling is the whole reason the bound exists.

    So the constraint is dropped INSIDE this transaction, the probe runs
    against the raw column, and the rollback puts it back. Postgres DDL is
    transactional, and the failing INSERT aborts the transaction anyway, so
    the drop cannot escape this test even if the assertion does.
    """

    from sqlalchemy.exc import DatabaseError

    # The constraint has to BE there for dropping it to mean anything. Without
    # this the DROP would raise "constraint does not exist", `pytest.raises`
    # would swallow it as a DatabaseError, and the failure would read as "the
    # btree did not fire" — the wrong diagnosis for a missing migration.
    with probe_rows.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'ck_applications_company_len'"
                )
            ).scalar()
            == 1
        ), "the CHECK is absent from this schema: `alembic upgrade head` did not create it"

    with pytest.raises(DatabaseError) as excinfo, probe_rows.begin() as conn:
        conn.execute(
            text("ALTER TABLE applications DROP CONSTRAINT ck_applications_company_len")
        )
        conn.execute(
            text(
                "INSERT INTO applications "
                "(user_id, company, position, status, source, created_at, updated_at) "
                "VALUES (:uid, :company, 'Engineer', 'APPLIED', 'manual', now(), now())"
            ),
            {"uid": _COMPANY_PROBE_USER, "company": _incompressible(2700)},
        )

    message = str(excinfo.value)
    assert "index row size" in message and "btree" in message, (
        "the INSERT failed for some reason other than the btree index-row limit, "
        f"so this module is not measuring what it claims to:\n{message}"
    )

    # AND THE CONSTRAINT REALLY DID COME BACK. Without this the test above
    # could leave the column unbounded for the rest of the session and the
    # sibling below would then be measuring a schema this suite had quietly
    # dismantled.
    with probe_rows.connect() as conn:
        present = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname = 'ck_applications_company_len'"
            )
        ).scalar()
    assert present == 1, "the rollback did not restore the CHECK constraint"


def test_the_check_constraint_answers_before_the_btree_does(probe_rows):
    """Defence in depth, and the order matters (#738).

    The btree ceiling is a *crash*: inside the sync's single transaction it
    takes the whole batch, so every message in that page is lost and the next
    sync re-reads and re-poisons. The CHECK is a *refusal* of one row.

    Same 2,700-character value as the test above, with the constraint left
    where it belongs: the column says no first, by name, and the btree is never
    reached.

    MUST RED ON: the revision `f1c47b93a2d6` no longer creating the constraint
    — the error string reverts to `index row size`, the pre-#738 behaviour.

    THE MODEL IS NOT WHAT THIS GRADES, and that was measured rather than
    assumed: removing the `CheckConstraint` from `Application.__table_args__`
    leaves all eight tests in this module green. `seeded_engine` builds its
    schema with `alembic upgrade head`, so this module grades the DEPLOYED
    shape and `tests/test_company_is_bounded_in_the_column.py` grades the
    model's. Both are needed — the model is what `create_all` gives every
    SQLite database, and the migration is what production gets.
    """

    from sqlalchemy.exc import DatabaseError

    with pytest.raises(DatabaseError) as excinfo:
        _insert_company(probe_rows, _incompressible(2700))

    message = str(excinfo.value)
    assert "ck_applications_company_len" in message, message
    assert "index row size" not in message, (
        "the btree limit was reached, so the CHECK did not answer first:\n" + message
    )


def test_a_company_at_the_api_bound_inserts_even_at_four_bytes_a_character(probe_rows):
    """The bound cannot reach the ceiling, at the worst byte cost UTF-8 allows.

    ``_MAX_COMPANY_LEN`` incompressible four-byte code points is the largest
    ``company`` the API will now let through. It goes into the real schema,
    through both real indexes, and comes back unchanged.
    """

    from jobtracker.cloud.applications import _MAX_COMPANY_LEN

    company = _incompressible(_MAX_COMPANY_LEN, four_byte=True)
    assert len(company.encode("utf-8")) == _MAX_COMPANY_LEN * 4

    _insert_company(probe_rows, company)

    with probe_rows.connect() as conn:
        stored = conn.execute(
            text("SELECT company FROM applications WHERE user_id = :uid"),
            {"uid": _COMPANY_PROBE_USER},
        ).scalar()

    assert stored == company
