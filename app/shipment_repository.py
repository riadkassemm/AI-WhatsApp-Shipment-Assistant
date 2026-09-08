from __future__ import annotations

from typing import Any

from app.database import db


class ShipmentDatabaseError(Exception):
    pass


class ShipmentRepository:

    # ---------------------------------------------------------
    # CUSTOMER
    # ---------------------------------------------------------

    async def get_customer(self, customer_id: str) -> dict | None:
        return await db.fetch_one(
            """
            SELECT
                id,
                userid,
                name,
                email,
                mobile,
                country,
                city,
                address,
                receiving_location,
                status
            FROM users
            WHERE id = %s
            LIMIT 1
            """,
            (customer_id,),
        )

    # ---------------------------------------------------------
    # BALANCE
    # ---------------------------------------------------------

    async def get_customer_balance(
        self,
        customer_id: str,
    ) -> dict:
        wallet = await db.fetch_one(
            """
            SELECT
                id,
                user_id,
                ballance,
                ballancet
            FROM user_wallet
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (customer_id,),
        )

        if not wallet:
            return {
                "customer_id": customer_id,
                "balance": 0,
                "currency": None,
            }

        return {
            "customer_id": customer_id,
            "balance": wallet["ballance"],
            "balance_type": wallet["ballancet"],
        }

    # ---------------------------------------------------------
    # TRACKING
    # ---------------------------------------------------------

    async def track_shipment(
        self,
        customer_id: str,
        tracking_number: str,
    ) -> dict | None:
        """
        Retrieve shipment/order information only if the shipment belongs
        to the authenticated customer.
        """

        row = await db.fetch_one(
            """
            SELECT
                o.id,
                o.uid,
                o.shipmentid,
                o.orderid,
                o.number,
                o.trackid,
                o.description,
                o.weight,
                o.qty,
                o.pod,
                o.branch,
                o.receiving_location,
                o.status,
                o.received,
                o.warehousereceive,
                o.express_assigned,
                o.deliveryid,
                o.assigneddate,
                o.completedate,
                o.date,

                s.sid,
                s.scode,
                s.sway,
                s.complete,
                s.shipped,
                s.pickprice,
                s.deliveryprice,
                s.date AS shipment_date

            FROM orders_new o

            INNER JOIN users u
                ON o.uid = u.userid

            LEFT JOIN shipments s
                ON s.sid = o.shipmentid

            WHERE
                u.id = %s
                AND (
                    o.trackid = %s
                    OR o.shipmentid = %s
                    OR o.orderid = %s
                )

            ORDER BY o.id DESC
            LIMIT 1
            """,
            (
                customer_id,
                tracking_number,
                tracking_number,
                tracking_number,
            ),
        )

        if not row:
            return None

        return self._format_shipment(row)

    # ---------------------------------------------------------
    # CUSTOMER SHIPMENTS
    # ---------------------------------------------------------

    async def get_customer_shipments(
        self,
        customer_id: str,
        limit: int = 10,
    ) -> list[dict]:

        # limit is controlled by our application, not user SQL.
        limit = max(1, min(limit, 50))

        rows = await db.fetch_all(
            f"""
            SELECT
                o.id,
                o.shipmentid,
                o.orderid,
                o.number,
                o.trackid,
                o.description,
                o.weight,
                o.qty,
                o.pod,
                o.branch,
                o.receiving_location,
                o.status,
                o.received,
                o.warehousereceive,
                o.assigneddate,
                o.completedate,
                o.date,

                s.sid,
                s.scode,
                s.sway,
                s.complete,
                s.shipped,
                s.pickprice,
                s.deliveryprice

            FROM orders_new o

            INNER JOIN users u
                ON o.uid = u.userid

            LEFT JOIN shipments s
                ON s.sid = o.shipmentid

            WHERE u.id = %s

            ORDER BY o.id DESC
            LIMIT {limit}
            """,
            (customer_id,),
        )

        return [self._format_shipment(row) for row in rows]

    # ---------------------------------------------------------
    # UPDATE SHIPMENT MODE
    # ---------------------------------------------------------

    async def update_shipment_mode(
        self,
        customer_id: str,
        tracking_number: str,
        mode: str,
    ) -> dict:

        if mode not in {"pickup", "delivery"}:
            raise ShipmentDatabaseError(
                "mode must be either pickup or delivery"
            )

        pod = "1" if mode == "pickup" else "2"

        # IMPORTANT:
        # Ownership is enforced here.
        #
        # We only update the order if:
        #   o.uid = authenticated customer
        #   AND tracking number matches.
        #
        result = await db.execute(
            """
            UPDATE orders_new o
            INNER JOIN users u ON o.uid = u.userid
            SET o.pod = %s
            WHERE
                u.id = %s
                AND (
                    o.trackid = %s
                    OR o.shipmentid = %s
                    OR o.orderid = %s
                )
            """,
            (
                pod,
                customer_id,
                tracking_number,
                tracking_number,
                tracking_number,
            ),
        )

        if result == 0:
            return {
                "success": False,
                "error": "Shipment not found or does not belong to the authenticated customer.",
            }

        updated = await self.track_shipment(
            customer_id,
            tracking_number,
        )

        return {
            "success": True,
            "mode": mode,
            "shipment": updated,
        }

    # ---------------------------------------------------------
    # SHIPPING PRICE
    # ---------------------------------------------------------

    async def get_shipping_price(
        self,
        origin: str,
        destination: str,
        weight_kg: float,
    ) -> dict:

        """
        TODO:

        The old API performed the actual pricing calculation.

        We do NOT yet know from the provided DB schema how the company's
        pricing engine calculates this.

        Do not invent pricing rules here.

        Once you show the old pricing/API implementation or the relevant
        database tables/business rules, replace this method.
        """

        raise ShipmentDatabaseError(
            "Shipping price calculation has not yet been mapped from the "
            "existing shipment system."
        )

    # ---------------------------------------------------------
    # DELIVERY DURATION
    # ---------------------------------------------------------

    async def get_delivery_duration(
        self,
        origin: str,
        destination: str,
    ) -> dict:

        """
        TODO:

        The old API provided delivery duration. The supplied four tables
        do not expose enough information to reproduce that business rule.
        """

        raise ShipmentDatabaseError(
            "Delivery duration calculation has not yet been mapped from "
            "the existing shipment system."
        )

    # ---------------------------------------------------------
    # BRANCHES
    # ---------------------------------------------------------

    async def get_branch_locations(
        self,
        city: str | None = None,
    ) -> list[dict]:

        """
        The branches table exists in mikexport.

        We haven't inspected its schema yet, so this method is intentionally
        left unmapped until we DESCRIBE branches.
        """

        raise ShipmentDatabaseError(
            "Branch schema has not yet been inspected."
        )

    # ---------------------------------------------------------
    # FORMATTER
    # ---------------------------------------------------------

    @staticmethod
    def _format_shipment(row: dict) -> dict:

        pod = str(row.get("pod", ""))

        if pod == "1":
            mode = "pickup"
        elif pod == "2":
            mode = "delivery"
        else:
            mode = pod

        return {
            "id": row.get("id"),
            "tracking_number": row.get("trackid"),
            "shipment_id": row.get("shipmentid"),
            "order_id": row.get("orderid"),
            "number": row.get("number"),
            "description": row.get("description"),
            "weight": row.get("weight"),
            "quantity": row.get("qty"),
            "mode": mode,
            "branch": row.get("branch"),
            "receiving_location": row.get("receiving_location"),
            "status": row.get("status"),
            "received": row.get("received"),
            "warehouse_received": row.get("warehousereceive"),
            "express_assigned": row.get("express_assigned"),
            "delivery_id": row.get("deliveryid"),
            "assigned_date": row.get("assigneddate"),
            "completed_date": row.get("completedate"),
            "date": row.get("date"),
            "shipment": {
                "sid": row.get("sid"),
                "scode": row.get("scode"),
                "sway": row.get("sway"),
                "complete": row.get("complete"),
                "shipped": row.get("shipped"),
                "pickup_price": row.get("pickprice"),
                "delivery_price": row.get("deliveryprice"),
                "date": row.get("shipment_date"),
            },
        }


shipment_repository = ShipmentRepository()