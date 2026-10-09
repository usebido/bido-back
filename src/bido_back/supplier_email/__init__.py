"""One standardized, code-rendered layout for every negotiation-phase supplier email.

Reminders, clarifications, revised-offer and delivery-date requests, best-and-final
requests and the "not selected" notice share one layout (`render`), rendered by code
from a phrase catalog per language (`catalog`) and a typed input (`model`). Code decides
every number, date, ask and email type; the model may only add an optional note, empty
by default (`compose`, `checks.note_problems`). Every email is checked before it is
proposed (`checks.email_problems`).

- `model`: the typed input other workstreams feed (`SupplierEmail`, `Position`, facts, asks).
- `builders`: pure functions from the services' data to a `SupplierEmail`.
- `catalog`: fixed sentences per language (pt, en carefully; de, fr, it, es need review).
- `locale`: numbers, money, quantities, units and dates; never rounds, never E notation.
- `render`: the plain-text and HTML parts from the same strings.
- `subject`, `threading`: `Re: ` + the first subject; In-Reply-To the latest message.
- `deadlines`: the reply-by date of each type.
- `checks`, `compose`: the send gate, then the note.

Shared with the rest of the supplier emails: the language is the thread's, decided once
at the RFQ email's first send and recorded in its approved document
(`bido_back.rfqs.language.thread_language`), with the supplier's country as the region
of the `locale.Locale`; numbers print through `bido_back.decimals` (never in exponent
form); the signature is `bido_back.templates.signature.signature`, printed once.
"""
