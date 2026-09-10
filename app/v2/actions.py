"""Transactional sandbox actions for the isolated v2 runtime.

The service is intentionally synchronous and owns its SQLAlchemy unit of work.
Callers must finish model and network work before entering this boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeAlias, cast

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts import AuthorizationContext
from app.models.product import Product
from app.models.v2 import (
    V2ActionAudit,
    V2ActionIdempotency,
    V2Cart,
    V2CartLine,
    V2Conversation,
    V2Offer,
    V2Order,
    V2OrderItem,
    V2Proposal,
    V2Turn,
)
from app.v2.authorization import (
    ResourceBinding,
    ResourceNotFoundError,
    bind_request_authorization,
)
from app.v2.contracts import (
    ActionCard,
    ActionChange,
    ActionConfirmRequest,
    ActionDecisionResponse,
    ActionKind,
    ActionRejectRequest,
    ActionStatus,
    ActionTarget,
    ConversationMode,
    DialogueOutcome,
    TurnStatus,
)
from app.v2.registry import (
    ActionExecutionResult,
    CartChangeInput,
    CartItem,
    CartReadInput,
    CartResult,
    CheckoutInput,
    CheckoutPreviewResult,
    MerchantOffer,
    MerchantOfferProposalInput,
    MerchantReadInput,
    MerchantReadResult,
    ProposalResult,
)

SessionFactory: TypeAlias = Callable[[], Session]
JsonObject: TypeAlias = dict[str, JsonValue]

_MAX_PRICE_VND = 10_000_000_000
_MAX_STOCK = 1_000_000
_PROPOSAL_LIFETIME = timedelta(minutes=10)


class ActionServiceError(RuntimeError):
    """Stable action-layer failure suitable for API error mapping."""

    code = "action_failed"


class ActionConflictError(ActionServiceError):
    code = "action_conflict"


class ActionIdempotencyConflictError(ActionConflictError):
    code = "idempotency_conflict"


class ActionStateConflictError(ActionConflictError):
    code = "proposal_state_conflict"


class ActionInProgressError(ActionConflictError):
    code = "action_in_progress"


@dataclass(frozen=True, slots=True)
class StoredAction:
    """Owner-authorized durable action state and its exact stored result."""

    card: ActionCard
    result: JsonObject | None


class V2ActionService:
    """Owner-scoped sandbox reads, proposals, and atomic action execution."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        catalog_version_id: str,
    ) -> None:
        ActionTarget(
            resource_type="cart",
            resource_id="cart_validation",
            expected_resource_version=1,
            data_version_ids=(catalog_version_id, "sandbox_validation"),
        )
        self._session_factory = session_factory
        self._catalog_version_id = catalog_version_id

    def read_inventory(
        self,
        authorization: AuthorizationContext,
        request: MerchantReadInput,
    ) -> MerchantReadResult:
        request = MerchantReadInput.model_validate(request.model_dump(mode="python"))
        access = bind_request_authorization(
            authorization, ConversationMode.MERCHANT, write=False
        )
        binding = access.binding
        with self._session_factory() as session:
            statement = select(V2Offer).where(
                V2Offer.tenant_id == binding.tenant_id,
                V2Offer.store_id == binding.store_id,
            )
            if request.product_ids:
                statement = statement.where(V2Offer.product_id.in_(request.product_ids))
            offers = tuple(
                session.scalars(statement.order_by(V2Offer.product_id).limit(5))
            )
            return MerchantReadResult(
                offers=tuple(_merchant_offer(offer) for offer in offers),
                snapshot_version_id=_sandbox_snapshot_id(offers),
            )

    def read_cart(
        self,
        authorization: AuthorizationContext,
        request: CartReadInput,
        *,
        cart_id: str | None = None,
    ) -> CartResult:
        CartReadInput.model_validate(request.model_dump(mode="python"))
        access = bind_request_authorization(
            authorization, ConversationMode.SHOPPER, write=False
        )
        binding = access.binding
        with self._session_factory() as session:
            statement = select(V2Cart).where(*_cart_owner_predicates(binding))
            if cart_id is None:
                statement = statement.where(V2Cart.status == "active").order_by(
                    V2Cart.created_at.desc(), V2Cart.id.desc()
                )
            else:
                statement = statement.where(V2Cart.id == cart_id)
            cart = session.scalar(statement.limit(1))
            if cart is None:
                raise ResourceNotFoundError
            basis = self._checkout_basis(session, cart, lock=False)
            return _cart_result(basis)

    def set_cart_item(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        turn_id: str,
        request: CartChangeInput,
    ) -> ActionExecutionResult:
        request = CartChangeInput.model_validate(request.model_dump(mode="python"))
        access = bind_request_authorization(
            authorization, ConversationMode.SHOPPER, write=True
        )
        binding = access.binding
        action_id = _stable_id(
            "proposal",
            {
                "owner": _binding_json(binding),
                "turn_id": turn_id,
                "kind": ActionKind.CART_CHANGE.value,
                "request": request.model_dump(mode="json"),
            },
        )
        result: ActionExecutionResult
        with self._session_factory() as session, session.begin():
            conversation, turn = self._lock_live_context(
                session, binding, conversation_id, turn_id
            )
            cart = self._lock_cart(session, binding, request.cart_id)
            lines = self._lock_cart_lines(session, cart)
            existing = session.get(V2Proposal, action_id)
            if existing is not None:
                if existing.result is None:
                    raise ActionStateConflictError("cart action has no stored result")
                result = ActionExecutionResult.model_validate(
                    existing.result["response"]
                )
                return result
            if cart.status != "active":
                raise ActionConflictError("cart_not_active")
            if cart.version != request.expected_version:
                raise ActionConflictError("cart_version_conflict")
            offer = session.scalar(
                select(V2Offer)
                .where(
                    V2Offer.tenant_id == binding.tenant_id,
                    V2Offer.store_id == binding.store_id,
                    V2Offer.product_id == request.product_id,
                )
                .with_for_update()
            )
            if offer is None:
                raise ResourceNotFoundError
            if not offer.is_active:
                raise ActionConflictError("offer_inactive")
            if request.quantity > offer.stock:
                raise ActionConflictError("insufficient_stock")
            line = next((item for item in lines if item.offer_id == offer.id), None)
            before_quantity = line.quantity if line is not None else 0
            db_now = self._database_now(session)
            if request.quantity == 0:
                if line is not None:
                    session.delete(line)
            elif line is None:
                session.add(
                    V2CartLine(
                        id=_stable_id("cartline", {"cart": cart.id, "offer": offer.id}),
                        cart_id=cart.id,
                        tenant_id=binding.tenant_id,
                        principal_id=binding.principal_id,
                        store_id=binding.store_id,
                        offer_id=offer.id,
                        quantity=request.quantity,
                        offer_version=offer.version,
                    )
                )
            else:
                line.quantity = request.quantity
                line.offer_version = offer.version
                line.updated_at = db_now
            if before_quantity != request.quantity:
                cart.version += 1
                cart.updated_at = db_now
            result = ActionExecutionResult(
                action_id=action_id,
                status=ActionStatus.EXECUTED,
                resource_id=cart.id,
                resource_version=cart.version,
            )
            before_payload: JsonObject = {
                "schema_version": 1,
                "quantity": before_quantity,
                "offer_id": offer.id,
                "offer_version": offer.version,
                "data_version_ids": [
                    self._catalog_version_id,
                    _sandbox_snapshot_id((offer,)),
                ],
            }
            after_payload: JsonObject = {
                "schema_version": 1,
                "quantity": request.quantity,
                "cart_version": cart.version,
            }
            proposal = V2Proposal(
                id=action_id,
                tenant_id=binding.tenant_id,
                principal_id=binding.principal_id,
                mode=binding.mode.value,
                store_id=binding.store_id,
                conversation_id=conversation.id,
                turn_id=turn.id,
                action_type=ActionKind.CART_CHANGE.value,
                target_type="cart",
                target_id=cart.id,
                target_version=request.expected_version,
                before_payload=before_payload,
                after_payload=after_payload,
                proposal_version=1,
                status=ActionStatus.EXECUTED.value,
                expires_at=db_now + _PROPOSAL_LIFETIME,
                result={
                    "schema_version": 1,
                    "response": result.model_dump(mode="json"),
                },
                created_at=db_now,
                resolved_at=db_now,
            )
            session.add(proposal)
            session.flush()
            self._append_audit(
                session,
                proposal,
                event_type="cart.executed",
                payload={"response": result.model_dump(mode="json")},
            )
            self._before_commit()
        return result

    def preview_checkout(
        self,
        authorization: AuthorizationContext,
        request: CheckoutInput,
    ) -> CheckoutPreviewResult:
        request = CheckoutInput.model_validate(request.model_dump(mode="python"))
        access = bind_request_authorization(
            authorization, ConversationMode.SHOPPER, write=False
        )
        with self._session_factory() as session:
            cart = self._find_cart(session, access.binding, request.cart_id)
            basis = self._checkout_basis(session, cart, lock=False)
            issues = _checkout_issues(basis, request.expected_version)
            return CheckoutPreviewResult(
                cart=_cart_result(basis), can_checkout=not issues, issues=issues
            )

    def propose_checkout(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        turn_id: str,
        request: CheckoutInput,
    ) -> ProposalResult:
        request = CheckoutInput.model_validate(request.model_dump(mode="python"))
        access = bind_request_authorization(
            authorization, ConversationMode.SHOPPER, write=True
        )
        binding = access.binding
        with self._session_factory() as session, session.begin():
            conversation, turn = self._lock_live_context(
                session, binding, conversation_id, turn_id
            )
            cart = self._lock_cart(session, binding, request.cart_id)
            basis = self._checkout_basis(session, cart, lock=True)
            issues = _checkout_issues(basis, request.expected_version)
            if issues:
                raise ActionConflictError(issues[0])
            before_payload = basis
            after_payload: JsonObject = {
                "schema_version": 1,
                "cart_status": "checked_out",
                "total_vnd": cast(int, basis["total_vnd"]),
            }
            proposal = self._create_or_replay_proposal(
                session,
                binding,
                conversation,
                turn,
                action_type=ActionKind.CHECKOUT,
                target_type="cart",
                target_id=cart.id,
                target_version=cart.version,
                before_payload=before_payload,
                after_payload=after_payload,
            )
            card = self._proposal_card(proposal)
            self._before_commit()
        return ProposalResult(action=card)

    def propose_offer_change(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        turn_id: str,
        request: MerchantOfferProposalInput,
    ) -> ProposalResult:
        request = MerchantOfferProposalInput.model_validate(
            request.model_dump(mode="python")
        )
        access = bind_request_authorization(
            authorization, ConversationMode.MERCHANT, write=True
        )
        binding = access.binding
        with self._session_factory() as session, session.begin():
            conversation, turn = self._lock_live_context(
                session, binding, conversation_id, turn_id
            )
            offer = self._lock_offer(session, binding, request.offer_id)
            if offer.version != request.expected_version:
                raise ActionConflictError("offer_version_conflict")
            if not offer.is_active:
                raise ActionConflictError("offer_inactive")
            before_payload = self._offer_basis(offer)
            after_payload = dict(before_payload)
            if request.new_price_vnd is not None:
                after_payload["price_vnd"] = request.new_price_vnd
                kind = ActionKind.MERCHANT_PRICE_CHANGE
            else:
                assert request.quantity_delta is not None
                new_stock = offer.stock + request.quantity_delta
                if not 0 <= new_stock <= _MAX_STOCK:
                    raise ActionConflictError("stock_out_of_range")
                after_payload["stock"] = new_stock
                kind = ActionKind.MERCHANT_INVENTORY_CHANGE
            proposal = self._create_or_replay_proposal(
                session,
                binding,
                conversation,
                turn,
                action_type=kind,
                target_type="offer",
                target_id=offer.id,
                target_version=offer.version,
                before_payload=before_payload,
                after_payload=after_payload,
            )
            card = self._proposal_card(proposal)
            self._before_commit()
        return ProposalResult(action=card)

    def confirm_action(
        self,
        authorization: AuthorizationContext,
        *,
        action_id: str,
        request: ActionConfirmRequest,
        idempotency_key: str,
    ) -> ActionExecutionResult:
        request = ActionConfirmRequest.model_validate(request.model_dump(mode="python"))
        key = _validated_idempotency_key(idempotency_key)
        mode = self._proposal_mode(authorization, action_id)
        access = bind_request_authorization(authorization, mode, write=True)
        binding = access.binding
        payload_hash = _canonical_hash(
            {
                "decision": "confirm",
                "action_id": action_id,
                "proposal_version": request.proposal_version,
            }
        )
        result: ActionExecutionResult
        with self._session_factory() as session, session.begin():
            proposal = self._lock_action_context(session, binding, action_id)
            idempotency = self._lock_idempotency(
                session, binding, proposal, key, payload_hash
            )
            if idempotency.status != "started":
                if idempotency.result is None:
                    raise ActionStateConflictError("idempotency result is missing")
                return ActionExecutionResult.model_validate(idempotency.result)
            if proposal.status != ActionStatus.PROPOSED.value:
                if proposal.result is None:
                    raise ActionStateConflictError("action is already terminal")
                if proposal.status == ActionStatus.REJECTED.value:
                    raise ActionStateConflictError("action was already rejected")
                stored = ActionExecutionResult.model_validate(
                    proposal.result["response"]
                )
                idempotency.status = (
                    "committed" if stored.status == ActionStatus.EXECUTED else "failed"
                )
                idempotency.result = stored.model_dump(mode="json")
                idempotency.completed_at = self._database_now(session)
                self._before_commit()
                return stored
            db_now = self._database_now(session)
            if proposal.proposal_version != request.proposal_version:
                result = self._terminal_confirmation_result(
                    session,
                    proposal,
                    idempotency,
                    status=ActionStatus.CONFLICTED,
                    db_now=db_now,
                    event_type="action.version_conflicted",
                    terminalize_proposal=False,
                )
            elif _aware(proposal.expires_at) <= db_now:
                result = self._terminal_confirmation_result(
                    session,
                    proposal,
                    idempotency,
                    status=ActionStatus.EXPIRED,
                    db_now=db_now,
                    event_type="action.expired",
                )
            elif proposal.action_type == ActionKind.CHECKOUT.value:
                result = self._confirm_checkout(
                    session, binding, proposal, idempotency, db_now
                )
            elif proposal.action_type in {
                ActionKind.MERCHANT_PRICE_CHANGE.value,
                ActionKind.MERCHANT_INVENTORY_CHANGE.value,
            }:
                result = self._confirm_offer_change(
                    session, binding, proposal, idempotency, db_now
                )
            else:
                result = self._terminal_confirmation_result(
                    session,
                    proposal,
                    idempotency,
                    status=ActionStatus.FAILED,
                    db_now=db_now,
                    event_type="action.failed",
                )
            self._before_commit()
        return result

    def reject_action(
        self,
        authorization: AuthorizationContext,
        *,
        action_id: str,
        request: ActionRejectRequest,
    ) -> ActionDecisionResponse:
        request = ActionRejectRequest.model_validate(request.model_dump(mode="python"))
        mode = self._proposal_mode(authorization, action_id)
        access = bind_request_authorization(authorization, mode, write=True)
        binding = access.binding
        result: ActionDecisionResponse
        with self._session_factory() as session, session.begin():
            proposal = self._lock_action_context(session, binding, action_id)
            if proposal.status == ActionStatus.REJECTED.value and proposal.result:
                if proposal.result.get("reason") != request.reason:
                    raise ActionStateConflictError("action was rejected differently")
                return ActionDecisionResponse.model_validate(
                    proposal.result["response"]
                )
            if proposal.status != ActionStatus.PROPOSED.value:
                raise ActionStateConflictError("action is already terminal")
            if proposal.proposal_version != request.proposal_version:
                raise ActionConflictError("proposal_version_conflict")
            db_now = self._database_now(session)
            status = (
                ActionStatus.EXPIRED
                if _aware(proposal.expires_at) <= db_now
                else ActionStatus.REJECTED
            )
            result = ActionDecisionResponse(
                action_id=proposal.id,
                proposal_version=proposal.proposal_version,
                status=status,
                decided_at=db_now,
            )
            proposal.status = status.value
            proposal.resolved_at = db_now
            proposal.result = {
                "schema_version": 1,
                "response": result.model_dump(mode="json"),
                "reason": request.reason,
            }
            self._append_audit(
                session,
                proposal,
                event_type=(
                    "action.expired"
                    if status == ActionStatus.EXPIRED
                    else "action.rejected"
                ),
                payload={"response": result.model_dump(mode="json")},
            )
            self._before_commit()
        return result

    def read_action_result(
        self,
        authorization: AuthorizationContext,
        *,
        action_id: str,
    ) -> StoredAction:
        mode = self._proposal_mode(authorization, action_id)
        access = bind_request_authorization(authorization, mode, write=False)
        binding = access.binding
        with self._session_factory() as session:
            proposal = session.scalar(
                select(V2Proposal).where(
                    V2Proposal.id == action_id,
                    *_proposal_owner_predicates(binding),
                )
            )
            if proposal is None:
                raise ResourceNotFoundError
            return StoredAction(
                card=self._proposal_card(proposal),
                result=cast(JsonObject | None, proposal.result),
            )

    def _proposal_mode(
        self, authorization: AuthorizationContext, action_id: str
    ) -> ConversationMode:
        with self._session_factory() as session:
            mode = session.scalar(
                select(V2Proposal.mode).where(
                    V2Proposal.id == action_id,
                    V2Proposal.tenant_id == authorization.tenant_id,
                    V2Proposal.principal_id == authorization.principal_id,
                    V2Proposal.store_id == "demo",
                )
            )
        if mode is None:
            raise ResourceNotFoundError
        return ConversationMode(mode)

    def _lock_live_context(
        self,
        session: Session,
        binding: ResourceBinding,
        conversation_id: str,
        turn_id: str,
    ) -> tuple[V2Conversation, V2Turn]:
        conversation = session.scalar(
            select(V2Conversation)
            .where(
                V2Conversation.id == conversation_id,
                V2Conversation.deleted_at.is_(None),
                *_conversation_owner_predicates(binding),
            )
            .with_for_update()
        )
        if conversation is None:
            raise ResourceNotFoundError
        turn = session.scalar(
            select(V2Turn)
            .where(
                V2Turn.id == turn_id,
                V2Turn.conversation_id == conversation.id,
                *_turn_owner_predicates(binding),
            )
            .with_for_update()
        )
        if turn is None:
            raise ResourceNotFoundError
        if turn.execution_state in {
            TurnStatus.CANCELLED.value,
            TurnStatus.INTERRUPTED.value,
            TurnStatus.FAILED.value,
        }:
            raise ActionStateConflictError("turn is not live")
        return conversation, turn

    def _lock_action_context(
        self, session: Session, binding: ResourceBinding, action_id: str
    ) -> V2Proposal:
        locator = session.scalar(
            select(V2Proposal).where(
                V2Proposal.id == action_id, *_proposal_owner_predicates(binding)
            )
        )
        if locator is None:
            raise ResourceNotFoundError
        _, turn = self._lock_live_context(
            session, binding, locator.conversation_id, locator.turn_id
        )
        proposal = session.scalar(
            select(V2Proposal)
            .where(
                V2Proposal.id == action_id,
                V2Proposal.turn_id == turn.id,
                *_proposal_owner_predicates(binding),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if proposal is None:
            raise ResourceNotFoundError
        if (
            turn.execution_state != TurnStatus.COMPLETED.value
            or turn.dialogue_outcome != DialogueOutcome.AWAITING_CONFIRMATION.value
        ):
            raise ActionStateConflictError("turn is not awaiting confirmation")
        return proposal

    def _find_cart(
        self, session: Session, binding: ResourceBinding, cart_id: str
    ) -> V2Cart:
        cart = session.scalar(
            select(V2Cart).where(V2Cart.id == cart_id, *_cart_owner_predicates(binding))
        )
        if cart is None:
            raise ResourceNotFoundError
        return cart

    def _lock_cart(
        self, session: Session, binding: ResourceBinding, cart_id: str
    ) -> V2Cart:
        cart = session.scalar(
            select(V2Cart)
            .where(V2Cart.id == cart_id, *_cart_owner_predicates(binding))
            .with_for_update()
        )
        if cart is None:
            raise ResourceNotFoundError
        return cart

    @staticmethod
    def _lock_cart_lines(session: Session, cart: V2Cart) -> tuple[V2CartLine, ...]:
        return tuple(
            session.scalars(
                select(V2CartLine)
                .where(V2CartLine.cart_id == cart.id)
                .order_by(V2CartLine.id)
                .with_for_update()
            )
        )

    @staticmethod
    def _lock_offer(
        session: Session, binding: ResourceBinding, offer_id: str
    ) -> V2Offer:
        offer = session.scalar(
            select(V2Offer)
            .where(
                V2Offer.id == offer_id,
                V2Offer.tenant_id == binding.tenant_id,
                V2Offer.store_id == binding.store_id,
            )
            .with_for_update()
        )
        if offer is None:
            raise ResourceNotFoundError
        return offer

    def _checkout_basis(
        self, session: Session, cart: V2Cart, *, lock: bool
    ) -> JsonObject:
        line_statement = (
            select(V2CartLine)
            .where(V2CartLine.cart_id == cart.id)
            .order_by(V2CartLine.id)
        )
        if lock:
            line_statement = line_statement.with_for_update()
        lines = tuple(session.scalars(line_statement))
        offer_ids = sorted({line.offer_id for line in lines})
        offer_statement = (
            select(V2Offer).where(V2Offer.id.in_(offer_ids)).order_by(V2Offer.id)
        )
        if lock:
            offer_statement = offer_statement.with_for_update()
        offers = tuple(session.scalars(offer_statement)) if offer_ids else ()
        offers_by_id = {offer.id: offer for offer in offers}
        product_ids = sorted({offer.product_id for offer in offers})
        products = (
            tuple(
                session.scalars(
                    select(Product)
                    .where(Product.id.in_(product_ids))
                    .order_by(Product.id)
                )
            )
            if product_ids
            else ()
        )
        products_by_id = {product.id: product for product in products}
        entries: list[JsonValue] = []
        total = 0
        for line in lines:
            offer = offers_by_id.get(line.offer_id)
            if offer is None:
                raise ActionConflictError("offer_missing")
            product = products_by_id.get(offer.product_id)
            if product is None:
                raise ActionConflictError("product_missing")
            line_total = line.quantity * offer.demo_price_vnd
            total += line_total
            entries.append(
                {
                    "line_id": line.id,
                    "offer_id": offer.id,
                    "product_id": offer.product_id,
                    "quantity": line.quantity,
                    "line_offer_version": line.offer_version,
                    "offer_version": offer.version,
                    "price_vnd": offer.demo_price_vnd,
                    "stock": offer.stock,
                    "is_active": offer.is_active,
                    "title": product.name,
                    "product_snapshot": _product_snapshot(product),
                }
            )
        return {
            "schema_version": 1,
            "cart_id": cart.id,
            "cart_version": cart.version,
            "cart_status": cart.status,
            "lines": entries,
            "total_vnd": total,
            "data_version_ids": [
                self._catalog_version_id,
                _sandbox_snapshot_id(offers),
            ],
        }

    def _offer_basis(self, offer: V2Offer) -> JsonObject:
        return {
            "schema_version": 1,
            "offer_id": offer.id,
            "product_id": offer.product_id,
            "price_vnd": offer.demo_price_vnd,
            "stock": offer.stock,
            "version": offer.version,
            "is_active": offer.is_active,
            "data_version_ids": [
                self._catalog_version_id,
                _sandbox_snapshot_id((offer,)),
            ],
        }

    def _create_or_replay_proposal(
        self,
        session: Session,
        binding: ResourceBinding,
        conversation: V2Conversation,
        turn: V2Turn,
        *,
        action_type: ActionKind,
        target_type: str,
        target_id: str,
        target_version: int,
        before_payload: JsonObject,
        after_payload: JsonObject,
    ) -> V2Proposal:
        identity = {
            "owner": _binding_json(binding),
            "turn_id": turn.id,
            "action_type": action_type.value,
            "target_type": target_type,
            "target_id": target_id,
            "target_version": target_version,
            "before": before_payload,
            "after": after_payload,
        }
        proposal_id = _stable_id("proposal", identity)
        existing = session.get(V2Proposal, proposal_id)
        if existing is not None:
            return existing
        db_now = self._database_now(session)
        proposal = V2Proposal(
            id=proposal_id,
            tenant_id=binding.tenant_id,
            principal_id=binding.principal_id,
            mode=binding.mode.value,
            store_id=binding.store_id,
            conversation_id=conversation.id,
            turn_id=turn.id,
            action_type=action_type.value,
            target_type=target_type,
            target_id=target_id,
            target_version=target_version,
            before_payload=before_payload,
            after_payload=after_payload,
            proposal_version=1,
            status=ActionStatus.PROPOSED.value,
            expires_at=db_now + _PROPOSAL_LIFETIME,
            created_at=db_now,
        )
        session.add(proposal)
        session.flush()
        return proposal

    def _lock_idempotency(
        self,
        session: Session,
        binding: ResourceBinding,
        proposal: V2Proposal,
        key: str,
        payload_hash: str,
    ) -> V2ActionIdempotency:
        row_id = _stable_id(
            "idempotency", {"owner": _binding_json(binding), "key": key}
        )
        statement = (
            pg_insert(V2ActionIdempotency)
            .values(
                id=row_id,
                tenant_id=binding.tenant_id,
                principal_id=binding.principal_id,
                mode=binding.mode.value,
                store_id=binding.store_id,
                proposal_id=proposal.id,
                idempotency_key=key,
                payload_hash=payload_hash,
                status="started",
            )
            .on_conflict_do_nothing(constraint="uq_v2_action_idempotency_scope")
        )
        inserted_id = session.scalar(statement.returning(V2ActionIdempotency.id))
        row = session.scalar(
            select(V2ActionIdempotency)
            .where(
                V2ActionIdempotency.tenant_id == binding.tenant_id,
                V2ActionIdempotency.principal_id == binding.principal_id,
                V2ActionIdempotency.mode == binding.mode.value,
                V2ActionIdempotency.store_id == binding.store_id,
                V2ActionIdempotency.idempotency_key == key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise ActionStateConflictError("idempotency row was not created")
        if row.payload_hash != payload_hash or row.proposal_id != proposal.id:
            raise ActionIdempotencyConflictError(
                "idempotency key was already used with a different payload"
            )
        if row.status == "started" and inserted_id is None:
            raise ActionInProgressError("action is still in progress")
        return row

    def _confirm_checkout(
        self,
        session: Session,
        binding: ResourceBinding,
        proposal: V2Proposal,
        idempotency: V2ActionIdempotency,
        db_now: datetime,
    ) -> ActionExecutionResult:
        if proposal.target_type != "cart":
            return self._terminal_confirmation_result(
                session,
                proposal,
                idempotency,
                status=ActionStatus.FAILED,
                db_now=db_now,
                event_type="action.failed",
            )
        cart = self._lock_cart(session, binding, proposal.target_id)
        basis = self._checkout_basis(session, cart, lock=True)
        issues = _checkout_issues(basis, proposal.target_version)
        if issues or basis != proposal.before_payload:
            return self._terminal_confirmation_result(
                session,
                proposal,
                idempotency,
                status=ActionStatus.CONFLICTED,
                db_now=db_now,
                event_type="action.stale",
            )
        lines = cast(list[dict[str, JsonValue]], basis["lines"])
        order_id = _stable_id("order", {"proposal": proposal.id})
        order = V2Order(
            id=order_id,
            tenant_id=binding.tenant_id,
            principal_id=binding.principal_id,
            store_id=binding.store_id,
            cart_id=cart.id,
            cart_version=cart.version,
            status="confirmed",
            total_vnd=cast(int, basis["total_vnd"]),
        )
        session.add(order)
        offers = {
            offer.id: offer
            for offer in session.scalars(
                select(V2Offer)
                .where(
                    V2Offer.id.in_(
                        sorted(cast(str, line["offer_id"]) for line in lines)
                    )
                )
                .order_by(V2Offer.id)
                .with_for_update()
            )
        }
        for line in lines:
            offer_id = cast(str, line["offer_id"])
            offer = offers[offer_id]
            quantity = cast(int, line["quantity"])
            offer.stock -= quantity
            offer.version += 1
            offer.updated_at = db_now
            session.add(
                V2OrderItem(
                    id=_stable_id("orderitem", {"order": order_id, "offer": offer_id}),
                    order_id=order_id,
                    tenant_id=binding.tenant_id,
                    principal_id=binding.principal_id,
                    store_id=binding.store_id,
                    offer_id=offer_id,
                    product_id=cast(int, line["product_id"]),
                    quantity=quantity,
                    unit_price_vnd=cast(int, line["price_vnd"]),
                    product_snapshot=cast(dict[str, Any], line["product_snapshot"]),
                )
            )
        cart.status = "checked_out"
        cart.version += 1
        cart.updated_at = db_now
        return self._terminal_confirmation_result(
            session,
            proposal,
            idempotency,
            status=ActionStatus.EXECUTED,
            db_now=db_now,
            event_type="checkout.executed",
            resource_id=order_id,
            resource_version=1,
        )

    def _confirm_offer_change(
        self,
        session: Session,
        binding: ResourceBinding,
        proposal: V2Proposal,
        idempotency: V2ActionIdempotency,
        db_now: datetime,
    ) -> ActionExecutionResult:
        if proposal.target_type != "offer":
            return self._terminal_confirmation_result(
                session,
                proposal,
                idempotency,
                status=ActionStatus.FAILED,
                db_now=db_now,
                event_type="action.failed",
            )
        offer = self._lock_offer(session, binding, proposal.target_id)
        before = self._offer_basis(offer)
        if (
            offer.version != proposal.target_version
            or before != proposal.before_payload
        ):
            return self._terminal_confirmation_result(
                session,
                proposal,
                idempotency,
                status=ActionStatus.CONFLICTED,
                db_now=db_now,
                event_type="action.stale",
            )
        after = proposal.after_payload
        if proposal.action_type == ActionKind.MERCHANT_PRICE_CHANGE.value:
            new_price = after.get("price_vnd")
            if type(new_price) is not int or not 0 < new_price <= _MAX_PRICE_VND:
                status = ActionStatus.FAILED
            else:
                offer.demo_price_vnd = new_price
                status = ActionStatus.EXECUTED
        else:
            new_stock = after.get("stock")
            if type(new_stock) is not int or not 0 <= new_stock <= _MAX_STOCK:
                status = ActionStatus.FAILED
            else:
                offer.stock = new_stock
                status = ActionStatus.EXECUTED
        if status == ActionStatus.EXECUTED:
            offer.version += 1
            offer.updated_at = db_now
        return self._terminal_confirmation_result(
            session,
            proposal,
            idempotency,
            status=status,
            db_now=db_now,
            event_type=(
                "merchant.offer.executed"
                if status == ActionStatus.EXECUTED
                else "action.failed"
            ),
            resource_id=offer.id,
            resource_version=offer.version,
        )

    def _terminal_confirmation_result(
        self,
        session: Session,
        proposal: V2Proposal,
        idempotency: V2ActionIdempotency,
        *,
        status: ActionStatus,
        db_now: datetime,
        event_type: str,
        resource_id: str | None = None,
        resource_version: int | None = None,
        terminalize_proposal: bool = True,
    ) -> ActionExecutionResult:
        result = ActionExecutionResult(
            action_id=proposal.id,
            status=status,
            resource_id=resource_id or proposal.target_id,
            resource_version=resource_version or proposal.target_version,
        )
        if terminalize_proposal:
            proposal.status = status.value
            proposal.result = {
                "schema_version": 1,
                "response": result.model_dump(mode="json"),
            }
            proposal.resolved_at = db_now
        idempotency.status = (
            "committed" if status == ActionStatus.EXECUTED else "failed"
        )
        idempotency.result = result.model_dump(mode="json")
        idempotency.completed_at = db_now
        self._append_audit(
            session,
            proposal,
            event_type=event_type,
            payload={"response": result.model_dump(mode="json")},
            idempotency=idempotency,
        )
        return result

    def _proposal_card(self, proposal: V2Proposal) -> ActionCard:
        kind = ActionKind(proposal.action_type)
        before = proposal.before_payload
        after = proposal.after_payload
        data_versions = tuple(cast(Sequence[str], before["data_version_ids"]))
        if kind == ActionKind.CHECKOUT:
            changes = (
                ActionChange(
                    resource_type="order",
                    resource_id=_stable_id("order", {"proposal": proposal.id}),
                    field="status",
                    before_text="cart_active",
                    after_text="confirmed",
                ),
            )
            title = "Xác nhận đơn hàng sandbox"
            permission = "ecommerce.write"
        elif kind == ActionKind.MERCHANT_PRICE_CHANGE:
            changes = (
                ActionChange(
                    resource_type="offer",
                    resource_id=proposal.target_id,
                    field="price_vnd",
                    before_integer=cast(int, before["price_vnd"]),
                    after_integer=cast(int, after["price_vnd"]),
                ),
            )
            title = "Đổi giá offer sandbox"
            permission = "merchant.write"
        elif kind == ActionKind.MERCHANT_INVENTORY_CHANGE:
            changes = (
                ActionChange(
                    resource_type="offer",
                    resource_id=proposal.target_id,
                    field="quantity",
                    before_integer=cast(int, before["stock"]),
                    after_integer=cast(int, after["stock"]),
                ),
            )
            title = "Điều chỉnh tồn kho sandbox"
            permission = "merchant.write"
        else:
            changes = (
                ActionChange(
                    resource_type="cart_item",
                    resource_id=cast(str, before["offer_id"]),
                    field="quantity",
                    before_integer=cast(int, before["quantity"]),
                    after_integer=cast(int, after["quantity"]),
                ),
            )
            title = "Cập nhật giỏ hàng sandbox"
            permission = "ecommerce.write"
        return ActionCard(
            action_id=proposal.id,
            proposal_id=proposal.id,
            proposal_version=proposal.proposal_version,
            kind=kind,
            status=ActionStatus(proposal.status),
            title=title,
            required_permission=permission,
            confirmation_required=kind != ActionKind.CART_CHANGE,
            target=ActionTarget(
                resource_type=cast(Any, proposal.target_type),
                resource_id=proposal.target_id,
                expected_resource_version=proposal.target_version,
                data_version_ids=data_versions,
            ),
            changes=changes,
            expires_at=_aware(proposal.expires_at),
        )

    @staticmethod
    def _append_audit(
        session: Session,
        proposal: V2Proposal,
        *,
        event_type: str,
        payload: JsonObject,
        idempotency: V2ActionIdempotency | None = None,
    ) -> None:
        session.add(
            V2ActionAudit(
                id=_stable_id(
                    "audit",
                    {
                        "proposal": proposal.id,
                        "event": event_type,
                        "idempotency": idempotency.id if idempotency else None,
                    },
                ),
                tenant_id=proposal.tenant_id,
                principal_id=proposal.principal_id,
                mode=proposal.mode,
                store_id=proposal.store_id,
                proposal_id=proposal.id,
                action_idempotency_id=idempotency.id if idempotency else None,
                event_type=event_type,
                payload=payload,
            )
        )

    @staticmethod
    def _database_now(session: Session) -> datetime:
        value = session.scalar(select(func.now()))
        if not isinstance(value, datetime):
            raise ActionServiceError("database clock is unavailable")
        return _aware(value)

    def _before_commit(self) -> None:
        """Fault-injection seam used to prove rollback-before-commit semantics."""


def _checkout_issues(
    basis: Mapping[str, JsonValue], expected_version: int
) -> tuple[str, ...]:
    issues: list[str] = []
    if basis["cart_status"] != "active":
        issues.append("cart_not_active")
    if basis["cart_version"] != expected_version:
        issues.append("cart_version_conflict")
    lines = cast(list[dict[str, JsonValue]], basis["lines"])
    if not lines:
        issues.append("cart_empty")
    for line in lines:
        if not line["is_active"]:
            issues.append("offer_inactive")
        if cast(int, line["quantity"]) > cast(int, line["stock"]):
            issues.append("insufficient_stock")
    return tuple(dict.fromkeys(issues))


def _cart_result(basis: Mapping[str, JsonValue]) -> CartResult:
    lines = cast(list[dict[str, JsonValue]], basis["lines"])
    return CartResult(
        cart_id=cast(str, basis["cart_id"]),
        version=cast(int, basis["cart_version"]),
        items=tuple(
            CartItem(
                product_id=cast(int, line["product_id"]),
                title=cast(str, line["title"]),
                quantity=cast(int, line["quantity"]),
                unit_price_vnd=cast(int, line["price_vnd"]),
                line_total_vnd=cast(int, line["quantity"])
                * cast(int, line["price_vnd"]),
            )
            for line in lines
        ),
        total_price_vnd=cast(int, basis["total_vnd"]),
    )


def _merchant_offer(offer: V2Offer) -> MerchantOffer:
    return MerchantOffer(
        offer_id=offer.id,
        product_id=offer.product_id,
        price_vnd=offer.demo_price_vnd,
        available_quantity=offer.stock,
        version=offer.version,
    )


def _product_snapshot(product: Product) -> JsonObject:
    return {
        "product_id": product.id,
        "title": product.name,
        "authors": list(product.authors),
        "publisher": product.publisher,
        "category": product.category,
    }


def _sandbox_snapshot_id(offers: Sequence[V2Offer]) -> str:
    return _stable_id(
        "sandbox",
        [
            {
                "id": offer.id,
                "product_id": offer.product_id,
                "price_vnd": offer.demo_price_vnd,
                "stock": offer.stock,
                "version": offer.version,
                "active": offer.is_active,
            }
            for offer in offers
        ],
    )


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}_{_canonical_hash(payload)[:32]}"


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_idempotency_key(value: str) -> str:
    if (
        type(value) is not str
        or not 8 <= len(value) <= 128
        or value != value.strip()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("invalid Idempotency-Key")
    return value


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _binding_json(binding: ResourceBinding) -> JsonObject:
    return {
        "tenant_id": binding.tenant_id,
        "principal_id": binding.principal_id,
        "mode": binding.mode.value,
        "store_id": binding.store_id,
    }


def _conversation_owner_predicates(binding: ResourceBinding) -> tuple[Any, ...]:
    return (
        V2Conversation.tenant_id == binding.tenant_id,
        V2Conversation.principal_id == binding.principal_id,
        V2Conversation.mode == binding.mode.value,
        V2Conversation.store_id == binding.store_id,
    )


def _turn_owner_predicates(binding: ResourceBinding) -> tuple[Any, ...]:
    return (
        V2Turn.tenant_id == binding.tenant_id,
        V2Turn.principal_id == binding.principal_id,
        V2Turn.mode == binding.mode.value,
        V2Turn.store_id == binding.store_id,
    )


def _proposal_owner_predicates(binding: ResourceBinding) -> tuple[Any, ...]:
    return (
        V2Proposal.tenant_id == binding.tenant_id,
        V2Proposal.principal_id == binding.principal_id,
        V2Proposal.mode == binding.mode.value,
        V2Proposal.store_id == binding.store_id,
    )


def _cart_owner_predicates(binding: ResourceBinding) -> tuple[Any, ...]:
    return (
        V2Cart.tenant_id == binding.tenant_id,
        V2Cart.principal_id == binding.principal_id,
        V2Cart.store_id == binding.store_id,
    )


__all__ = [
    "ActionConflictError",
    "ActionIdempotencyConflictError",
    "ActionInProgressError",
    "ActionServiceError",
    "ActionStateConflictError",
    "StoredAction",
    "V2ActionService",
]
