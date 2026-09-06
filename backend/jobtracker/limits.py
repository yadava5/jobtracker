"""Bounds that more than one layer has to agree about.

WHY THIS MODULE EXISTS. ``_MAX_COMPANY_LEN`` was declared in
``cloud/pipeline.py`` and imported from there by ``cloud/applications.py``'s
request models. That was fine while every enforcer was application code. It
stops being fine the moment the DATABASE has to know the same number: making
``database/models.py`` import the classifier pipeline to learn a length would
drag five thousand lines of regex into the ORM's import graph to fetch an
integer.

So the number moves to a leaf that both can import, and neither owns. The
alternative — retyping ``300`` in the CHECK constraint — is the drift #581
exists to prevent, and a CHECK that disagrees with the request models is worse
than neither: one of them would reject a value the other accepted, and which
one you hit would depend on the write path.
"""

#: The longest employer name any writer may put on ``applications.company``.
#:
#: NOT A STYLE PREFERENCE. ``ix_applications_company`` is a btree, and Postgres
#: refuses an index entry over 2704 bytes — ``ProgramLimitExceededError: index
#: row size 2720 exceeds btree version 4 maximum 2704``. Inside the sync's
#: single transaction that takes the WHOLE batch with it: measured, ``AAA +
#: POISON + ZZZ`` left zero rows, including the innocent message that had
#: already flushed, and nothing commits — so every later sync re-reads the same
#: mail and re-poisons.
#:
#: 300 CHARACTERS, AND THE UNIT MATTERS. ``length()`` is character semantics on
#: both engines, and 300 characters is at most 1,200 bytes of UTF-8 — comfortably
#: inside the btree limit even for four-byte code points, which is why the bound
#: is expressed in characters rather than bytes.
#:
#: The value is deliberately far above anything real: the longest employer name
#: in production is 21 characters (76 rows, read 2026-09-06), and the longest
#: sender name in the independent corpus is 42. This is a rail against a hostile
#: or malformed value, not an editorial judgement about names.
MAX_COMPANY_LEN = 300
