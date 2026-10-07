"""The supplier simulator: simulated suppliers answer a sandbox tenant's RFQ emails.

A sandbox tenant (`tenants.sandbox`) is a test or demo company. Its outbound
email goes to `SandboxEmailSender` instead of a mailbox (`sender`), and the
`simulator.reply` job (`jobs`) answers each email as the supplier's persona
(`personas`): code decides the offer (`offers`), the `supplier_persona` agent
writes the prose (`agent`), code renders the email and its attachment
(`render`), and the reply goes through the normal intake path, so extraction,
comparison, follow-ups and negotiation run exactly as with real email.
"""
