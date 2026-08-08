"""Idempotent side-effect receipts for patch and tool execution."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EffectStatus(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    UNCERTAIN = "uncertain"
    REVALIDATE = "revalidate"
    COMPENSATED = "compensated"


@dataclass
class EffectReceipt:
    effect_id: str
    action: str
    idempotency_key: str
    status: EffectStatus | str = EffectStatus.PREPARED
    task_id: str = ""
    actor: str = ""
    payload_hash: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.status = EffectStatus(str(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EffectReceipt:
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


class EffectLedger:
    def __init__(self, store=None):
        self.store = store
        self._receipts: dict[str, EffectReceipt] = {}

    def prepare(self, receipt: EffectReceipt) -> EffectReceipt:
        existing = self._receipts.get(receipt.idempotency_key)
        if existing is None and self.store is not None:
            existing = self.store.get_receipt(receipt.idempotency_key)
        if existing:
            if existing.action != receipt.action or (
                existing.payload_hash
                and receipt.payload_hash
                and existing.payload_hash != receipt.payload_hash
            ):
                raise ValueError("idempotency key is bound to a different effect")
            self._receipts[receipt.idempotency_key] = existing
            return existing
        receipt.status = EffectStatus.PREPARED
        self._receipts[receipt.idempotency_key] = receipt
        if self.store is not None:
            self.store.save_receipt(receipt)
        return receipt

    def transition(
        self, idempotency_key: str, status: EffectStatus | str, **updates: Any
    ) -> EffectReceipt:
        receipt = self.get(idempotency_key)
        if receipt is None:
            raise KeyError(idempotency_key)
        target = EffectStatus(str(status))
        if target == receipt.status:
            return receipt
        allowed = {
            EffectStatus.PREPARED: {
                EffectStatus.COMMITTED,
                EffectStatus.UNCERTAIN,
                EffectStatus.COMPENSATED,
            },
            EffectStatus.UNCERTAIN: {
                EffectStatus.REVALIDATE,
                EffectStatus.COMMITTED,
                EffectStatus.COMPENSATED,
            },
            EffectStatus.REVALIDATE: {
                EffectStatus.COMMITTED,
                EffectStatus.UNCERTAIN,
                EffectStatus.COMPENSATED,
            },
        }
        if target not in allowed.get(receipt.status, {receipt.status}):
            raise ValueError(f"invalid effect transition {receipt.status.value}->{target.value}")
        receipt.status = target
        for key, value in updates.items():
            if hasattr(receipt, key):
                setattr(receipt, key, value)
        receipt.updated_at = time.time()
        if self.store is not None:
            self.store.save_receipt(receipt)
        return receipt

    def get(self, idempotency_key: str) -> EffectReceipt | None:
        receipt = self._receipts.get(idempotency_key)
        if receipt is None and self.store is not None:
            receipt = self.store.get_receipt(idempotency_key)
            if receipt is not None:
                self._receipts[idempotency_key] = receipt
        return receipt

    def snapshot(self) -> dict[str, dict]:
        return {key: value.to_dict() for key, value in self._receipts.items()}
