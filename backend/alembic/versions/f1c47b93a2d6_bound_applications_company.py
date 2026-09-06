"""a CHECK constraint on applications.company, so the bound is not application-only

Revision ID: f1c47b93a2d6
Revises: a3f7d21c60be
Create Date: 2026-09-06 20:50:00.000000

Issue #738. ``applications.company`` accepted anything: ``character varying``
with ``character_maximum_length = NULL`` and, read against production on
2026-09-06, **zero** CHECK constraints on the table.

WHAT THE BOUND PREVENTS. ``ix_applications_company`` is a btree, and Postgres
refuses an index entry over 2704 bytes — ``ProgramLimitExceededError: index row
size 2720 exceeds btree version 4 maximum 2704``. Inside the sync's single
transaction that takes the WHOLE batch with it: measured, ``AAA + POISON +
ZZZ`` left zero rows, including the innocent message that had already flushed,
and nothing commits, so every later sync re-reads the same mail and re-poisons.

WHY THE APPLICATION-LEVEL BOUND WAS NOT THE CLOSURE. #581 placed bounds at the
producers known then, all of them Python. The defect is a FLOW property — "no
unbounded value reaches this column" — so a future display producer reaching an
existing write site bypasses every one of them. The tree already holds the
template: ``employer_named_in_body`` is a display producer whose confinement to
display grade is enforced by docstring only.

The decisive property is not Postgres. **SQLite enforces CHECK constraints
too**, so the whole "invisible on a laptop" class that produced #406 and #581
becomes loud in the first local test run, for every writer and every syntax.

SAFE TO APPLY, MEASURED FIRST RATHER THAN AFTER. Postgres re-validates a CHECK
on any UPDATE of a row, so ``NOT VALID`` would not make this free — a legacy row
over 300 characters would start failing on its next status change, which is the
sync advancing a card, turning a latent value into a live breakage. Read
read-only against production with the service role (RLS makes the app's own
count a lie) on 2026-09-06:

    SELECT count(*), count(*) FILTER (WHERE length(company) > 300),
           max(length(company)) FROM applications;
    ->  76 rows, 0 over the bound, longest 21 characters

So no data pass is owed, and the constraint is created validating.

EITHER DEPLOY ORDER IS SAFE, which is the question ``check_expand_only.py``
exists to ask. A push starts the migrate workflow and the Vercel deploy at the
same moment and nothing orders them. If this revision lands FIRST, the code
already serving cannot write a value it would reject — #581's four Python bounds
are what has been holding the line, and they are the same 300. If the code
lands first, the column is simply unbounded for a few seconds longer, exactly
as it has been. No declaration is added: adding a CHECK is not one of the five
facts that gate calls destructive, and it rejects a ``CONTRACT_STEP`` on a
revision that removes nothing.

THE 300 IS RETYPED HERE ON PURPOSE, and that is the opposite of the rule the
model follows. ``database/models.py`` imports ``MAX_COMPANY_LEN`` because two
live ceilings on one column is the drift #581 exists to prevent. A MIGRATION is
a historical record of what was done to a database on a date: importing a live
constant would make this file silently describe a different constraint the day
that constant changed, and the database would still hold this one.
``tests/test_company_is_bounded_in_the_column.py`` pins the two together, so a
change to the constant without a new revision is loud rather than quiet.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f1c47b93a2d6"
down_revision: Union[str, Sequence[str], None] = "a3f7d21c60be"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The bound as it stood when this revision was written. See the module
#: docstring for why this is a literal and not an import.
COMPANY_LEN_AT_THIS_REVISION = 300


# BATCH MODE, and it is not optional. SQLite has no `ALTER TABLE ... ADD
# CONSTRAINT`, and alembic raises `NotImplementedError: No support for ALTER of
# constraints in SQLite dialect` — which `tests/test_alembic.py` catches,
# because it walks this whole chain against SQLite. `batch_alter_table`
# recreates the table there (copy-and-move) and issues a plain `ALTER` on
# Postgres, so one revision serves both engines. That matters beyond the test:
# the desktop and test databases are SQLite, and a revision that silently
# skipped them would leave the "loud on a laptop" property — the entire reason
# for choosing a CHECK — true only where nobody develops.
def upgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        batch.create_check_constraint(
            "ck_applications_company_len",
            f"length(company) <= {COMPANY_LEN_AT_THIS_REVISION}",
        )


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("ck_applications_company_len", type_="check")
