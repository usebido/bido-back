"""HTTP clients of the payment providers: Basis Theory, Stripe and Privy.

Their request and response shapes stay in this package; `payments.card` and
`payments.wallets` turn them into the neutral types of `payments.ports`.
Every client takes an optional injected `httpx.AsyncClient` (tests use
`httpx.MockTransport`), passes an explicit timeout on every call, never
retries by itself, parses each response into frozen pydantic models, and
fails with `ProviderError(provider, operation, code)`: a code it wrote or the
provider's machine code, never a body, a header or a URL.
"""
