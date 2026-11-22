"""Read-only business views with ownership and replication freshness boundaries.

IT supplies the documented views and a SELECT-only principal. No legacy snapshot
or numeric-ID fallback is permitted. The model never supplies SQL or owner IDs.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

import aiomysql
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.customer_ai_identity import AIIdentity
from app.customer_users import customer_users
from app.shipment_client import ShipmentClient
from app.shipping_quote_service import execute_catalog_tool


class BusinessDataError(Exception):
    def __init__(self, reason: str = "BUSINESS_DATA_UNAVAILABLE"):
        super().__init__(reason)
        self.reason = reason


class DataRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Shipment(DataRecord):
    order_id: str = Field(min_length=1, max_length=191)
    tracking_number: str | None = Field(default=None, max_length=191)
    shipment_id: str | None = Field(default=None, max_length=191)
    status: str = Field(min_length=1, max_length=100)


class Wallet(DataRecord):
    balance: Decimal = Field(allow_inf_nan=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class Transaction(DataRecord):
    reference: str = Field(min_length=1, max_length=191)
    amount: Decimal = Field(allow_inf_nan=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    occurred_at: datetime


@dataclass(frozen=True)
class BusinessResult:
    tool: str
    as_of: datetime
    source: str
    payload: dict[str, Any]


class BusinessDataSource(Protocol):
    async def read(self, tool: str, arguments: dict, identity: AIIdentity) -> BusinessResult: ...


def fresh_timestamp(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise BusinessDataError("BUSINESS_DATA_STALE")
    stamp = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if age < -5 or age > settings.ai_business_max_age_seconds:
        raise BusinessDataError("BUSINESS_DATA_STALE")
    return stamp


class SnapshotCatalog(ShipmentClient):
    """Reuse catalogue matching and Decimal calculations without legacy DB access."""
    def __init__(self, rates: list[dict], destinations: list[dict]):
        super().__init__()
        self.rates, self.destinations = rates, destinations

    async def _load_shipping_rates(self):
        return self.rates

    async def _load_shipping_destinations(self):
        return self.destinations

    async def connect(self):
        raise BusinessDataError()


class ReplicaBusinessDataSource:
    def __init__(self):
        self._pool = None
        self._lock = None

    async def _ensure_pool(self):
        if self._pool is None:
            if self._lock is None:
                self._lock = asyncio.Lock()
            async with self._lock:
                if self._pool is None:
                    if not all((settings.ai_business_db_host, settings.ai_business_db_name,
                                settings.ai_business_db_user, settings.ai_business_db_password)):
                        raise BusinessDataError()
                    self._pool = await aiomysql.create_pool(
                        host=settings.ai_business_db_host, port=settings.ai_business_db_port,
                        db=settings.ai_business_db_name, user=settings.ai_business_db_user,
                        password=settings.ai_business_db_password, minsize=1, maxsize=4,
                        connect_timeout=5, autocommit=False, charset="utf8mb4",
                        init_command="SET SESSION TRANSACTION READ ONLY",
                    )
        return self._pool

    async def close(self):
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
        self._lock = None

    async def _snapshot(self, dataset: str, queries: list[tuple[str, tuple]]) -> tuple[datetime, list[list[dict]]]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            try:
                await conn.begin()
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT as_of FROM ai_data_freshness WHERE dataset = %s LIMIT 2", (dataset,))
                    marks = await cur.fetchall()
                    if len(marks) != 1:
                        raise BusinessDataError("BUSINESS_DATA_STALE")
                    as_of = fresh_timestamp(marks[0]["as_of"])
                    results = []
                    for sql, params in queries:
                        await cur.execute(sql, params)
                        results.append(list(await cur.fetchall()))
                    return as_of, results
            finally:
                await conn.rollback()

    async def read(self, tool: str, arguments: dict, identity: AIIdentity) -> BusinessResult:
        try:
            return await self._read(tool, arguments, identity)
        except BusinessDataError:
            raise
        except Exception:
            # Driver errors may embed credentials, SQL parameters or business data.
            raise BusinessDataError() from None

    async def _read(self, tool: str, arguments: dict, identity: AIIdentity) -> BusinessResult:
        data_class = {
            "get_customer_shipments": "shipments", "track_shipment": "tracking",
            "get_order_status": "orders", "get_customer_balance": "wallet",
            "get_customer_transactions": "payments", "get_shipping_price": "rates",
            "get_route_shipping_options": "rates", "get_delivery_duration": "rates",
        }.get(tool)
        if data_class is None:
            raise BusinessDataError("AI_TOOL_UNAVAILABLE")
        if not identity.allows(data_class):
            raise BusinessDataError("IDENTITY_VERIFICATION_REQUIRED")
        if tool in {"get_shipping_price", "get_route_shipping_options", "get_delivery_duration"}:
            as_of, rows = await self._snapshot("catalog", [
                ("SELECT id, destination_id, origin, destination, shipping_method, goods_type, price, transit_time "
                 "FROM ai_shipping_rates ORDER BY id LIMIT 10001", ()),
                ("SELECT id, slug, label FROM ai_destinations ORDER BY id LIMIT 10001", ()),
            ])
            if not rows[0] or any(len(items) > 10000 for items in rows):
                raise BusinessDataError()
            payload = await execute_catalog_tool(SnapshotCatalog(*rows), tool, arguments)
            if not payload.get("found") or payload.get("matched_requested_filters") is False:
                raise BusinessDataError()
        else:
            # UUIDs, never coincidentally equal numeric IDs, link independent sources.
            account = await customer_users.get_by_identity(identity.customer_id or "")
            owner = str((account or {}).get("uuid") or "").strip()
            if not owner:
                raise BusinessDataError()
            if tool in {"get_customer_shipments", "track_shipment", "get_order_status"}:
                query = ("SELECT order_id, tracking_number, shipment_id, status FROM ai_orders "
                         "WHERE customer_uuid = %s")
                params: tuple = (owner,)
                if tool != "get_customer_shipments":
                    query += " AND (order_id = %s OR tracking_number = %s OR shipment_id = %s)"
                    params += (arguments["reference"],) * 3
                query += " ORDER BY created_at DESC, order_id DESC LIMIT " + ("10" if tool == "get_customer_shipments" else "2")
                as_of, rows = await self._snapshot("orders", [(query, params)])
                if not rows[0] or (tool != "get_customer_shipments" and len(rows[0]) != 1):
                    raise BusinessDataError()
                shipments = [Shipment.model_validate(row).model_dump(mode="json") for row in rows[0]]
                payload = {"found": True, "shipments": shipments, "count": len(shipments)}
            elif tool == "get_customer_balance":
                as_of, rows = await self._snapshot("wallet", [
                    ("SELECT balance, currency FROM ai_wallets WHERE customer_uuid = %s LIMIT 2", (owner,)),
                ])
                if len(rows[0]) != 1:
                    raise BusinessDataError()
                payload = Wallet.model_validate(rows[0][0]).model_dump(mode="json")
            elif tool == "get_customer_transactions":
                as_of, rows = await self._snapshot("payments", [
                    ("SELECT reference, amount, currency, occurred_at FROM ai_transactions "
                     "WHERE customer_uuid = %s ORDER BY occurred_at DESC, reference DESC LIMIT 10", (owner,)),
                ])
                if not rows[0]:
                    raise BusinessDataError()
                payload = {"transactions": [Transaction.model_validate(row).model_dump(mode="json") for row in rows[0]]}
            else:
                raise BusinessDataError()
        # Recheck after the lookup/calculation, not only at its start.
        return BusinessResult(tool, fresh_timestamp(as_of), "business_replica", payload)


business_data_source = ReplicaBusinessDataSource()
