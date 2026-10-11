"""Pay an issued purchase order by card into its escrow; the cards and payout accounts it uses.

  GET    /v1/payments/config                                  what the browser needs (public)
  GET    /v1/payment-methods                                  the company's saved cards
  POST   /v1/payment-methods                                  save a card tokenized in the browser
  DELETE /v1/payment-methods/{id}                             remove a card
  GET    /v1/suppliers/{supplier_id}/payout-accounts          where a supplier is paid (masked)
  POST   /v1/suppliers/{supplier_id}/payout-accounts          add one (approver or owner)
  POST   /v1/suppliers/{supplier_id}/payout-accounts/{id}/disable
  POST   /v1/purchase-orders/{po_id}/payment                  propose paying a sent order (202)
  GET    /v1/purchase-orders/{po_id}/payment                  where the payment stands

Nothing here charges a card: paying is a `pay_purchase_order` approval a person
decides in `/v1/approvals`, and the worker charges, mints and funds the escrow.
Card numbers never reach Bido (Basis Theory Elements tokenize them in the
browser), and a supplier's bank account is stored sealed for the payout provider.
Routes that call a provider answer 503 while payments are not set up on this server.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_job_queue,
    get_payment_providers,
    get_principal,
)
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.payments import service
from bido_back.payments.config import PaymentProviders
from bido_back.payments.errors import (
    PaymentError,
    PaymentForbidden,
    PaymentInvalidState,
    PaymentNotFound,
    PaymentRefused,
    PaymentsNotConfigured,
    ProviderError,
)
from bido_back.payments.schemas import (
    CreatePayoutAccountRequest,
    EnrollPaymentMethodRequest,
    PaymentRequested,
    PayPurchaseOrderRequest,
)
from bido_back.payments.views import (
    PaymentCardVerificationView,
    PaymentMethodView,
    PaymentsConfigView,
    PaymentView,
    PayoutAccountView,
)
from bido_back.rfqs.errors import Forbidden, InvalidState, NotFound, RfqError
from bido_back.settings import Settings
from bido_back.worker.jobs import JobQueue

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Providers = Annotated[PaymentProviders, Depends(get_payment_providers)]
Jobs = Annotated[JobQueue, Depends(get_job_queue)]


def get_payment_settings(request: Request) -> Settings:
    resources: object = request.app.state.resources
    if not isinstance(resources, ApiResources):
        raise RuntimeError("app.state.resources is not configured")
    return resources.settings


PaymentSettings = Annotated[Settings, Depends(get_payment_settings)]


def payments_http_error(exc: PaymentError | RfqError) -> HTTPException:
    """Map a refusal to an HTTP error. Messages carry ids and reasons only."""
    if isinstance(exc, PaymentNotFound | NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, PaymentForbidden | Forbidden):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, PaymentInvalidState | InvalidState):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, PaymentRefused):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if isinstance(exc, PaymentsNotConfigured):
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "payments are not set up on this server"
        )
    if isinstance(exc, ProviderError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "the payment provider failed; try again")
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "payment operation failed")


def payments_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["payments"])

    @router.get("/payments/config", responses=error_responses())
    async def payments_config(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, settings: PaymentSettings
    ) -> PaymentsConfigView:
        """Whether payments are on, and the public values the browser needs (the Basis
        Theory public key for Elements, the Solana cluster and mint)."""
        return service.payments_config_view(settings)

    # --- Cards ---------------------------------------------------------------------------

    @router.get("/payment-methods", responses=error_responses())
    async def list_payment_methods(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> list[PaymentMethodView]:
        """The company's saved cards, newest first. A buyer pays only with a card they saved."""
        return await service.list_payment_methods(db, principal)

    @router.post(
        "/payment-methods",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            status.HTTP_502_BAD_GATEWAY,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def enroll_payment_method(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        body: EnrollPaymentMethodRequest,
        db: Db,
        providers: Providers,
        settings: PaymentSettings,
    ) -> PaymentMethodView:
        """Save a card from its Basis Theory token id (never card data). 409: the company
        already saved this card; 422: it cannot be charged by Bido."""
        try:
            return await service.enroll_payment_method(
                db,
                principal,
                body,
                providers.card,
                consumer_country=settings.payments_consumer_country,
            )
        except (PaymentError, RfqError) as exc:
            raise payments_http_error(exc) from None

    @router.delete(
        "/payment-methods/{payment_method_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT, status.HTTP_503_SERVICE_UNAVAILABLE
        ),
    )
    async def remove_payment_method(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, payment_method_id: UUID, db: Db, providers: Providers
    ) -> None:
        """Remove a card (whoever saved it, an approver or the owner). 409 while a payment
        waiting for approval or being charged uses it."""
        try:
            await service.remove_payment_method(db, principal, payment_method_id, providers.card)
        except (PaymentError, RfqError) as exc:
            raise payments_http_error(exc) from None

    # --- Supplier payout accounts ----------------------------------------------------------

    @router.get(
        "/suppliers/{supplier_id}/payout-accounts",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def list_payout_accounts(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, supplier_id: UUID, db: Db
    ) -> list[PayoutAccountView]:
        """Where the supplier is paid, newest first, disabled accounts included (masked)."""
        try:
            return await service.list_payout_accounts(db, principal, supplier_id)
        except (PaymentError, RfqError) as exc:
            raise payments_http_error(exc) from None

    @router.post(
        "/suppliers/{supplier_id}/payout-accounts",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            status.HTTP_502_BAD_GATEWAY,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def create_payout_account(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        supplier_id: UUID,
        body: CreatePayoutAccountRequest,
        db: Db,
        providers: Providers,
    ) -> PayoutAccountView:
        """Add a bank account the supplier is paid to (approver or owner). The details are
        stored sealed for the payout provider; the response is masked. 409: the supplier
        already has it."""
        try:
            return await service.create_payout_account(
                db, principal, supplier_id, body, providers.bank_details
            )
        except (PaymentError, RfqError) as exc:
            raise payments_http_error(exc) from None

    @router.post(
        "/suppliers/{supplier_id}/payout-accounts/{account_id}/disable",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def disable_payout_account(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, supplier_id: UUID, account_id: UUID, db: Db
    ) -> PayoutAccountView:
        """Stop paying to this account (approver or owner). Corrected details are a new
        account; a payment proposed to this one fails when it runs."""
        try:
            return await service.disable_payout_account(db, principal, supplier_id, account_id)
        except (PaymentError, RfqError) as exc:
            raise payments_http_error(exc) from None

    # --- Paying a purchase order ---------------------------------------------------------

    @router.post(
        "/purchase-orders/{po_id}/payment",
        status_code=status.HTTP_202_ACCEPTED,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def request_po_payment(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        po_id: UUID,
        body: PayPurchaseOrderRequest,
        db: Db,
        providers: Providers,
        settings: PaymentSettings,
    ) -> PaymentRequested:
        """Propose paying a sent order with one of your cards into its escrow, at today's
        USD quote; a person approves it at POST /v1/approvals/{approval_id}/decision."""
        try:
            return await service.request_po_payment(
                db,
                principal,
                po_id,
                body,
                providers,
                settings=settings,
                today=datetime.now(UTC).date(),
            )
        except (PaymentError, RfqError) as exc:
            raise payments_http_error(exc) from None

    @router.post(
        "/purchase-orders/{po_id}/payment/card-verification",
        responses=error_responses(
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def confirm_card_verification(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, po_id: UUID, db: Db, providers: Providers, jobs: Jobs
    ) -> PaymentCardVerificationView:
        """The Visa passkey approval of the order's payment (`card_rail` `vic`), read from
        Basis Theory. Call it for the allowance to verify, run `verifyAllowance` in the
        browser, then call it again: `verified` lets the payment be approved and executed.
        Only the person paying (whose card it is) may call it."""
        try:
            return await service.confirm_card_verification(
                db, principal, po_id, providers.card, jobs
            )
        except (PaymentError, RfqError) as exc:
            raise payments_http_error(exc) from None

    @router.get(
        "/purchase-orders/{po_id}/payment",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def get_po_payment(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, po_id: UUID, db: Db, settings: PaymentSettings
    ) -> PaymentView:
        """The order's live payment, else its latest: amounts, card, payout account, the
        escrow and every Solana transaction (with explorer links)."""
        try:
            return await service.get_po_payment(db, principal, po_id, settings)
        except (PaymentError, RfqError) as exc:
            raise payments_http_error(exc) from None

    return router
