"""From a built email to what is proposed: the gate first, then the model's optional note.

1. The email without a note is rendered and checked (`checks.email_problems`). If that
   fails, nothing is proposed and the model is not called (a code-level problem would
   only recur).
2. The note source (the writer agent) is asked for its note. A failed run, an empty note
   or a note that breaks a rule leaves the note out; the email is still proposed. A writer
   problem never again means "no email".
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from bido_back.supplier_email.checks import GateContext, email_problems, names_in, note_problems
from bido_back.supplier_email.model import SupplierEmail
from bido_back.supplier_email.render import RenderedEmail, render

# Returns the model's note ("" when it has none or the run failed). Never raises.
NoteSource = Callable[[], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Composed:
    email: SupplierEmail
    rendered: RenderedEmail
    # Why the model's note was left out (codes), when it was.
    note_dropped: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Refused:
    problems: tuple[str, ...]


async def compose(
    email: SupplierEmail, context: GateContext, note: NoteSource | None = None
) -> Composed | Refused:
    bare = replace(email, note="")
    rendered = render(bare)
    problems = email_problems(bare, rendered, context)
    if problems:
        return Refused(tuple(problems))
    if note is None:
        return Composed(bare, rendered)
    text = " ".join((await note()).split())
    if not text:
        return Composed(bare, rendered)
    dropped = note_problems(text, language=email.language, names=names_in(email, context))
    if not dropped:
        noted = replace(bare, note=text)
        noted_rendered = render(noted)
        dropped = email_problems(noted, noted_rendered, context)
        if not dropped:
            return Composed(noted, noted_rendered)
    return Composed(bare, rendered, tuple(dropped))
