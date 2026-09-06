"""No field may outlive its last reader (#645, #631).

WHAT THIS EXISTS TO STOP. ``Settings`` accumulated fifteen fields nothing read.
Setting the corresponding ``JOBTRACKER_*`` variable parsed, validated and was
discarded — a knob wired to nothing, which is worse than an absent one because
it reads as configuration. Three shapes: shadowed by a hardcoded constant
(wiring them would be affirmatively WRONG — ``EmbeddingsClassifier.MODEL_NAME``,
Apple's fixed IMAP endpoint), a removed subsystem's leftovers (the desktop
entrypoint went with #298), and one never wired at all.

WHY THE CENSUS IS AST-BASED AND PROVENANCE-AWARE, and this is the whole design.
#645 records two earlier instruments and both were wrong, in opposite
directions:

  v1 counted a mention in a COMMENT as a reader, and cleared the very field
  #631 is about. A false ALIVE is loud — the field stays and someone re-checks.

  v2 scanned every file EXCEPT ``config.py`` itself, so it could not see a
  field read by a ``@computed_field`` property one screen below its own
  declaration. It produced three false DEADS, including
  ``database_url_override`` — the production Postgres switch. Deleting that
  drops the deployed API to SQLite with no error. A false dead is SILENT, and
  that is the direction this gate is written to avoid.

  A separate ``git grep`` cross-check disagreed on four fields and was wrong on
  all four: a same-named local (``log_dir = settings.log_path``), a comment
  saying "no longer consulted anywhere", and two module constants.

So "reader" is defined by RECEIVER, not by name:

1. ``self.<field>`` inside the ``Settings`` class body — the false-dead fix.
2. ``X.<field>`` only where ``X`` is bound to settings in that module: the
   imported ``settings``, the result of ``get_settings()``, or a constructed
   ``Settings()``. An unrelated object's same-named attribute is not a reader.
3. A string only as an argument to ``getattr``/``setattr``/``hasattr`` on such
   a receiver — ``gmail_oauth_missing_fields`` really does read four fields
   that way. A bare string anywhere else does not count, or any docstring
   naming a field would immortalise it.
4. Tests never count. A field read only by its own tests is dead.

KNOWN AND ACCEPTED BLIND SPOT: a computed attribute name
(``getattr(settings, f"setfit_{x}")``) or a ``model_dump()`` string-key
consumer defeats any static census. Neither pattern exists today; both would
produce a false DEAD, so if either is ever introduced this gate reds and the
next reader is looking at exactly the right line.

SCANNED FROM ``git ls-files``, NOT A FILESYSTEM WALK. This repository holds
dozens of agent worktrees under ``.claude/``; an ``rglob`` saw 10,348 files and
a reader living on a stale branch would clear a field that is dead on main.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "backend" / "jobtracker" / "config.py"

#: Bound to settings by construction. `settings` is the module-level singleton
#: `config.py` exports; the other two are how a caller obtains one.
_SETTINGS_FACTORIES = {"get_settings", "Settings"}


def declared_fields(source: str) -> set[str]:
    """Every field the ``Settings`` class declares.

    ``ClassVar`` and dunder/private names are excluded: the first is not a
    setting and the second is not addressable by an environment variable.
    """

    fields: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.ClassDef) and node.name == "Settings"):
            continue
        for stmt in node.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            name = stmt.target.id
            if name.startswith("_"):
                continue
            annotation = ast.unparse(stmt.annotation)
            if "ClassVar" in annotation:
                continue
            fields.add(name)
    return fields


class _Readers(ast.NodeVisitor):
    """Attribute reads whose RECEIVER is a settings object.

    Tracks which local names in this module hold settings, so
    ``other.log_dir`` and ``settings.log_dir`` are not the same fact.
    """

    def __init__(self, *, inside_settings_class: bool) -> None:
        self.found: set[str] = set()
        self._bound = {"settings"} if not inside_settings_class else {"settings"}
        self._self_is_settings: list[bool] = []
        self._inside_settings_class = inside_settings_class

    # --- provenance ------------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """`from jobtracker.config import settings as current_settings`.

        WRITTEN BECAUSE THIS GATE'S FIRST RUN PRODUCED A FALSE DEAD, which is
        the silent direction and the one #645 warns about twice. Without this,
        `training_allowed_user_ids` — read at `setfit_model.py:740` through an
        ALIASED import, precisely so a `importlib.reload` cannot leave the
        permission check consulting a stale singleton — was reported dead. The
        field that decides whose mail SetFit may train on.
        """

        if node.module and node.module.split(".")[-1] == "config":
            for alias in node.names:
                if alias.name in {"settings", "get_settings", "Settings"}:
                    self._bound.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _SETTINGS_FACTORIES:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._bound.add(target.id)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._self_is_settings.append(node.name == "Settings")
        self.generic_visit(node)
        self._self_is_settings.pop()

    # --- readers ---------------------------------------------------------
    def _is_settings_receiver(self, value: ast.expr) -> bool:
        if isinstance(value, ast.Name):
            if value.id in self._bound:
                return True
            if value.id == "self" and any(self._self_is_settings):
                return True
        if isinstance(value, ast.Call):
            func = value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            return name in _SETTINGS_FACTORIES
        # `config.settings.x`, `_config().settings.x` — the module-qualified
        # spellings. Widening here is the SAFE direction: its residual error is
        # a false ALIVE, which leaves a field declared and someone re-checking.
        return isinstance(value, ast.Attribute) and value.attr == "settings"

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._is_settings_receiver(node.value):
            self.found.add(node.attr)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        if (
            name in {"getattr", "setattr", "hasattr"}
            and len(node.args) >= 2
            and self._is_settings_receiver(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            self.found.add(node.args[1].value)
        self.generic_visit(node)


def readers_in(source: str) -> set[str]:
    """Field names this source READS off a settings object."""

    visitor = _Readers(inside_settings_class=False)
    visitor.visit(ast.parse(source))
    return visitor.found


def dead_fields(config_source: str, sources: list[str]) -> set[str]:
    """Declared minus read. ``config_source`` is scanned as a source too.

    Scanning `config.py` itself is the fix for the three false deads: a
    ``@computed_field`` property reads its inputs through ``self``, one screen
    below their declaration, and an instrument that skipped this file called
    the production Postgres switch dead.
    """

    read: set[str] = set()
    for src in [config_source, *sources]:
        read |= readers_in(src)
    return declared_fields(config_source) - read


def _tracked_python() -> list[Path]:
    """Tracked, non-test, non-mirror Python. See the header on why not rglob."""

    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    keep = []
    for rel in out:
        # Tests never count as readers (rule 4), and `ml/demo/space/` is a
        # generated copy of `backend/jobtracker/` — a reader there is the same
        # reader counted twice, never a unique one.
        if "/tests/" in rel or rel.startswith("ml/demo/space/"):
            continue
        keep.append(REPO / rel)
    return keep


#: Fields kept ON PURPOSE with no reader, each with the reason. Not a
#: convenience hatch: both directions are asserted below, so an entry that
#: stops being declared reds, and one that gains a reader reds too. A waiver
#: nobody re-reads is how a list like this becomes the thing it was meant to
#: prevent.
TOMBSTONES = {
    "cron_sync_user_ids": (
        "Retired deliberately, not overlooked. `cloud/cron.py:76` states "
        "'`settings.cron_sync_user_ids` is no longer consulted anywhere', and "
        "the field's own description records the decision to keep it so an "
        "existing environment does not fail validation. #645 names it as a "
        "case of its own and says not to sweep it up with the fifteen. Worth "
        "revisiting: under `extra=\"ignore\"` deletion cannot fail validation "
        "either, so the stated reason does not actually distinguish the two "
        "options — and keeping the field keeps its UUID validator, which is "
        "the one way this retired variable can still break a deploy."
    ),
}


# =============================================================================
# The gate
# =============================================================================


def test_every_settings_field_has_a_reader() -> None:
    """The census, against the real tree.

    MUST RED ON: re-declaring any field with no consumer — demonstrated, not
    asserted: adding `api_port: int = 8000` back to `Settings` with nothing
    reading it takes this test red. See
    `test_the_gate_reds_on_a_field_with_no_reader` for the sealed version of
    that demonstration.
    """

    config_source = CONFIG.read_text(encoding="utf-8")
    sources = []
    for path in _tracked_python():
        if path == CONFIG:
            continue
        try:
            sources.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue

    dead = dead_fields(config_source, sources) - set(TOMBSTONES)
    assert not dead, (
        "Settings declares fields nothing reads: "
        + ", ".join(sorted(dead))
        + ". A knob wired to nothing parses, validates and is discarded, which "
        "reads as configuration and is not. Delete it, or wire it and say where."
    )


def test_the_real_tree_still_clears_the_field_the_first_instrument_got_wrong() -> None:
    """`database_url_override` is the canary, and it is not a hypothetical one.

    An earlier census filed it as "read only by tests" because it scanned every
    file except `config.py`, where a `@computed_field` reads it through `self`.
    Deleting it drops the deployed API to SQLite with no error. If this gate
    ever calls it dead, the gate is wrong and not the field.
    """

    config_source = CONFIG.read_text(encoding="utf-8")
    assert "database_url_override" in declared_fields(config_source)
    assert "database_url_override" not in dead_fields(config_source, [])


# =============================================================================
# The controls — one per failure mode, not one seeded fake
# =============================================================================

_CONFIG_HEAD = "class Settings:\n"


def _config(*field_lines: str, extra: str = "") -> str:
    body = "".join(f"    {line}\n" for line in field_lines)
    return _CONFIG_HEAD + body + extra


def test_the_gate_reds_on_a_field_with_no_reader() -> None:
    """It can fire at all."""

    assert dead_fields(_config("orphan: int = 1"), []) == {"orphan"}


def test_a_settings_attribute_read_clears_a_field() -> None:
    """It can see the ordinary reader."""

    assert dead_fields(_config("wired: int = 1"), ["print(settings.wired)"]) == set()


def test_another_objects_same_named_attribute_does_not_clear_a_field() -> None:
    """Rule 2, and the reason the census keys on the RECEIVER.

    A local called `log_dir` is indistinguishable BY NAME from the setting; the
    `git grep` cross-check was wrong on four fields for exactly this.
    """

    assert dead_fields(_config("wired: int = 1"), ["print(other.wired)"]) == {"wired"}


def test_a_self_read_inside_the_class_clears_a_field() -> None:
    """Rule 1 — the shape that produced all three false deads.

    A `@computed_field` property reads its inputs through `self`, one screen
    below their declaration and inside the very file an earlier instrument
    excluded.
    """

    source = _config(
        "wired: int = 1",
        "",
        "def derived(self) -> int:",
        "    return self.wired + 1",
    )
    assert dead_fields(source, []) == set()


def test_a_fields_own_validator_does_not_clear_it() -> None:
    """A validator runs and discards its result — that is not a reader.

    Written because the obvious census counts any mention inside `config.py`,
    and a field whose only appearance is its own `field_validator` decorator
    would then immortalise itself.
    """

    source = _config(
        "wired: int = 1",
        "",
        '@field_validator("wired")',
        "def _check(cls, v):",
        "    return v",
    )
    assert dead_fields(source, []) == {"wired"}


def test_a_getattr_with_a_string_clears_a_field_but_only_on_a_settings_receiver() -> None:
    """Rule 3, both halves.

    `gmail_oauth_missing_fields` really does read four fields through
    `getattr(self, name)`-shaped calls; a bare string elsewhere must not count,
    or a docstring naming a field would keep it alive forever.
    """

    cleared = dead_fields(_config("wired: int = 1"), ['getattr(settings, "wired", None)'])
    assert cleared == set()

    not_cleared = dead_fields(_config("wired: int = 1"), ['getattr(other, "wired", None)'])
    assert not_cleared == {"wired"}

    bare_string = dead_fields(_config("wired: int = 1"), ['NAMES = ["wired"]'])
    assert bare_string == {"wired"}


@pytest.mark.parametrize("factory", ["get_settings()", "Settings()"])
def test_a_freshly_obtained_settings_object_is_a_settings_receiver(factory: str) -> None:
    """Both ways a caller obtains one, bound to a local or read inline."""

    assert dead_fields(_config("wired: int = 1"), [f"s = {factory}\nprint(s.wired)"]) == set()
    assert dead_fields(_config("wired: int = 1"), [f"print({factory}.wired)"]) == set()


def test_every_tombstone_is_still_a_field_with_no_reader() -> None:
    """The waiver list, checked in both directions.

    A stale entry is a false signal to the next reader — it says "we thought
    about this" about a field that no longer exists, or one that has since
    gained a reader and is no longer a tombstone at all. Neither can sit here
    quietly.
    """

    config_source = CONFIG.read_text(encoding="utf-8")
    declared = declared_fields(config_source)
    sources = [
        path.read_text(encoding="utf-8")
        for path in _tracked_python()
        if path != CONFIG
    ]
    dead = dead_fields(config_source, sources)

    for field, reason in TOMBSTONES.items():
        assert field in declared, (
            f"{field} is waived as a deliberate tombstone but is no longer "
            "declared. Delete the waiver."
        )
        assert field in dead, (
            f"{field} is waived as having no reader, but something reads it "
            "now. Delete the waiver — it is not a tombstone any more."
        )
        assert len(reason) > 80, (
            f"{field}'s waiver has no real reason attached. The reason IS the "
            "artifact; a bare entry is how this list stops being read."
        )
