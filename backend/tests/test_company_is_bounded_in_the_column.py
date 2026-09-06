"""The bound on ``applications.company`` is the COLUMN's, not only Python's.

THE DEFECT (#738). #581 closed four application-code paths that could put an
oversized employer name on this column. Every one of those bounds is Python,
placed at the producers known at the time — and the defect is a FLOW property,
"no unbounded value reaches this column", so a future display producer reaching
an existing write site bypasses all of them at once. The tree already carries
the template: ``employer_named_in_body`` is a display producer whose
confinement to display grade is enforced by docstring only.

WHAT IT PREVENTS. ``ix_applications_company`` is a btree; Postgres refuses an
index entry over 2704 bytes, and inside the sync's single transaction that
takes the WHOLE batch — nothing commits, so every later sync re-reads the same
mail and re-poisons.

WHY THESE TESTS RUN ON SQLITE AND THAT IS THE POINT. SQLite enforces CHECK
constraints too. The entire "invisible on a laptop, fatal in production" class
that produced #406 and #581 becomes loud in the first local run, for every
writer past and future and whatever the syntax — ``setattr``, ``**kwargs``,
Core ``update()``, raw SQL. Declared on the model, so ``create_all``-built test
schemas carry it without anyone remembering to.

Nothing here goes through ``cloud/applications.py``'s request models. That is
deliberate: those already have their own bound and their own tests, and a test
that reached the column through them would be re-measuring Pydantic.
"""

from __future__ import annotations

import ast
import pathlib
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from jobtracker.database.models import Application, ApplicationStatus
from jobtracker.limits import MAX_COMPANY_LEN

USER = uuid.UUID("00000000-0000-0000-0000-0000000000ab")

#: The revision that created the constraint. Named so the drift test below
#: fails loudly if it is ever renamed rather than silently finding nothing.
MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "f1c47b93a2d6_bound_applications_company.py"
)


def _row(company: str) -> Application:
    return Application(
        user_id=USER,
        company=company,
        position="Software Engineer",
        status=ApplicationStatus.APPLIED,
        source="gmail",
    )


async def test_a_name_at_the_bound_is_accepted(test_session: AsyncSession) -> None:
    """The control that sits ON the threshold.

    A refusal test alone cannot tell "the constraint works" from "the
    constraint is off by one and refuses everything near it", and an off-by-one
    here would reject legitimate employer names rather than only hostile ones.

    MUST RED ON: `length(company) < MAX_COMPANY_LEN`, or any bound below 300.
    """

    at_bound = "A" * MAX_COMPANY_LEN
    assert len(at_bound) == MAX_COMPANY_LEN

    test_session.add(_row(at_bound))
    await test_session.flush()  # no raise


async def test_a_name_over_the_bound_is_refused_by_the_DATABASE(
    test_session: AsyncSession,
) -> None:
    """One character past it, and the column says no.

    ``flush``, not ``commit``: the write is refused at the statement, which is
    what makes this the column's answer rather than a deferred check.

    MUST RED ON: removing the `CheckConstraint` from `Application.__table_args__`
    — which is exactly what "the bound is application-level only" was.
    """

    over = "A" * (MAX_COMPANY_LEN + 1)
    test_session.add(_row(over))

    with pytest.raises(IntegrityError) as caught:
        await test_session.flush()

    # NAMED, not merely "something raised". A required column, a bad enum or a
    # unique collision would all raise IntegrityError here, and a test that
    # accepted any of them would keep passing after the constraint was dropped
    # for whatever reason replaced it.
    assert "ck_applications_company_len" in str(caught.value), str(caught.value)


async def test_the_refusal_does_not_depend_on_how_the_value_was_set(
    test_session: AsyncSession,
) -> None:
    """`setattr` after construction — the shape a Python-side bound misses.

    #581's bounds live at named producers. This is the write that reaches the
    column without passing any of them, and it is the whole argument for
    putting the rule in the schema.
    """

    row = _row("Acme")
    row.company = "A" * (MAX_COMPANY_LEN + 1)
    test_session.add(row)

    with pytest.raises(IntegrityError) as caught:
        await test_session.flush()
    assert "ck_applications_company_len" in str(caught.value)


def test_the_migration_and_the_model_agree_on_the_bound() -> None:
    """Two independent sources of the same number, compared.

    The model IMPORTS `MAX_COMPANY_LEN`, because two live ceilings on one
    column is the drift #581 exists to prevent. The migration RETYPES it,
    because a migration is a historical record of what was done to a database
    on a date — importing a live constant would make that file silently
    describe a different constraint the day the constant changed, while the
    database still held this one.

    Those two rules are only safe together if something compares them, and this
    is that thing. Read by `ast.parse` rather than by a regex over the source:
    a comment or a docstring mentioning 300 must not be able to satisfy it.

    MUST RED ON: changing `MAX_COMPANY_LEN` without a new revision.
    """

    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    literals = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign | ast.Assign)
        for target in (
            [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        )
        if isinstance(target, ast.Name)
        and target.id == "COMPANY_LEN_AT_THIS_REVISION"
        and isinstance(node.value, ast.Constant)
    ]
    assert literals == [MAX_COMPANY_LEN], (
        f"{MIGRATION.name} was written against {literals}, but the model now "
        f"bounds company at {MAX_COMPANY_LEN}. A new revision is owed: the "
        f"deployed database still holds the old CHECK."
    )


def test_the_constraint_is_declared_on_the_model_not_only_in_a_migration() -> None:
    """`create_all` builds test and desktop schemas from the model, not Alembic.

    A constraint that existed only in a revision would be absent from every
    SQLite database this suite builds, and the "loud on a laptop" property —
    the reason for choosing a CHECK over another Python bound — would be gone
    while the two tests above still passed against Postgres alone.
    """

    import sqlalchemy as sa

    checks = {
        c.name: str(c.sqltext)
        for c in Application.__table__.constraints
        if isinstance(c, sa.CheckConstraint)
    }
    assert checks.get("ck_applications_company_len") == f"length(company) <= {MAX_COMPANY_LEN}"
