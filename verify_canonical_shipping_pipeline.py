from __future__ import annotations

import argparse
import asyncio
import re
from typing import Any

from app import shipping_intent_normalizer
from app.config import settings
from app.conversation_store import Conversation
from app.language import detect_communication_style
from app.shipment_client import ShipmentDBError, shipment_client
from app.shipping_quote_service import maybe_handle_shipping_quote


_ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06FF]")


def _refresh_style(conversation: Conversation, text: str) -> None:
    if not any(ch.isalpha() for ch in text):
        return
    profile = detect_communication_style(
        text,
        previous_style=conversation.communication_style,
    )
    conversation.current_language = profile.language
    conversation.communication_style = profile.style


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _totals(result: dict[str, Any]) -> list[str]:
    values: list[str] = []
    calculated = result.get("calculated_totals")
    if isinstance(calculated, dict):
        values.extend(str(value) for value in calculated.values() if value)
    total = result.get("calculated_total")
    if total:
        values.append(str(total))
    return values


async def _verify_catalog_access() -> None:
    rates = await shipment_client._load_shipping_rates()
    destinations = await shipment_client._load_shipping_destinations()
    _require(bool(rates), "shipping_db.shipping_rates returned no rows.")
    _require(bool(destinations), "shipping_db.destinations returned no rows.")

    destination_labels = {
        str(row.get("label") or row.get("slug") or "").strip().casefold()
        for row in destinations
    }
    _require("lebanon" in destination_labels, "Lebanon is missing from destinations.")
    _require("syria" in destination_labels, "Syria is missing from destinations.")


async def _authoritative_expected_quotes() -> dict[str, dict[str, Any]]:
    queries = {
        "uae_cosmetics_25": dict(
            origin="UAE",
            destination="Lebanon",
            weight_kg=25,
            goods_type="Cosmetics",
        ),
        "ksa_cosmetics_25": dict(
            origin="KSA",
            destination="Lebanon",
            weight_kg=25,
            goods_type="Cosmetics",
        ),
        "usa_electronics_25": dict(
            origin="USA",
            destination="Lebanon",
            weight_kg=25,
            goods_type="Electronics",
        ),
        "usa_syria_accessories_25": dict(
            origin="USA",
            destination="Syria",
            weight_kg=25,
            goods_type="Accessories",
        ),
        "usa_syria_accessories_50": dict(
            origin="USA",
            destination="Syria",
            weight_kg=50,
            goods_type="Accessories",
        ),
    }
    output: dict[str, dict[str, Any]] = {}
    for name, query in queries.items():
        result = await shipment_client.get_shipping_price(**query)
        _require(
            result.get("found") is True
            and result.get("matched_requested_filters") is True,
            f"Live catalogue query {name} did not match exactly: {result}",
        )
        _require(
            bool(_totals(result)),
            f"Live catalogue query {name} has no calculable /kg total: {result}",
        )
        output[name] = result
    return output


async def _verify_reported_sequence(expected: dict[str, dict[str, Any]]) -> None:
    # Keep this verifier deterministic and free of provider/network dependencies. The
    # production semantic normalizer remains enabled by normal settings; this sequence
    # validates the DB-owned fallback, context merge, arithmetic, and renderer.
    original_semantic_enabled = settings.shipping_semantic_normalizer_enabled
    settings.shipping_semantic_normalizer_enabled = False
    try:
        conversation = Conversation(
            phone_number="canonical-pipeline-verification",
            current_language="ar-LB",
            communication_style="leb_arabizi",
        )

        async def turn(text: str):
            _refresh_style(conversation, text)
            outcome = await maybe_handle_shipping_quote(conversation, text)
            if outcome is not None:
                _require(
                    not _ARABIC_SCRIPT_RE.search(outcome.reply_text),
                    f"Arabizi reply contains Arabic-script letters: {outcome.reply_text!r}",
                )
            return outcome

        first = await turn(
            "eza bade esh7an mawed tejmil mn el emarat 3a lebnen adesh betkallef"
        )
        _require(first is not None, "Initial UAE quote was not recognized.")
        _require(
            first.status == "shipping_quote_waiting_weight_kg",
            f"Initial UAE quote did not wait for weight: {first}",
        )

        uae = await turn("25")
        _require(uae is not None, "25 kg UAE quote did not complete.")
        for value in _totals(expected["uae_cosmetics_25"]):
            _require(value in uae.reply_text, f"UAE reply is missing {value}: {uae.reply_text}")

        ksa = await turn("tyb eza bade esh7an mn el su3udiye 3a lebnen")
        _require(ksa is not None, "KSA follow-up was not recognized.")
        for value in _totals(expected["ksa_cosmetics_25"]):
            _require(value in ksa.reply_text, f"KSA reply is missing {value}: {ksa.reply_text}")
        _require("bel barr" in ksa.reply_text, f"KSA land method is missing: {ksa.reply_text}")

        thanks = await turn("tamem ysallemon")
        _require(thanks is None, "A thank-you message was incorrectly treated as a quote.")

        missing_ksa_category = await turn("electronics adesh?")
        _require(
            missing_ksa_category is not None
            and missing_ksa_category.status == "shipping_quote_options",
            "Missing KSA electronics category did not return route options.",
        )

        usa = await turn("electronics men amerka")
        _require(usa is not None, "USA electronics follow-up was not recognized.")
        for value in _totals(expected["usa_electronics_25"]):
            _require(value in usa.reply_text, f"USA reply is missing {value}: {usa.reply_text}")
        _require(
            conversation.communication_style == "leb_arabizi",
            "English goods nouns incorrectly switched the established Arabizi style.",
        )

        accessories = await turn("ekseswar 3a souriya")
        _require(accessories is not None, "Accessories-to-Syria follow-up was not recognized.")
        for value in _totals(expected["usa_syria_accessories_25"]):
            _require(
                value in accessories.reply_text,
                f"25 kg accessories reply is missing {value}: {accessories.reply_text}",
            )

        accessories_50 = await turn("50kg ekseswar 3a souriya")
        _require(accessories_50 is not None, "50 kg accessories quote was not recognized.")
        for value in _totals(expected["usa_syria_accessories_50"]):
            _require(
                value in accessories_50.reply_text,
                f"50 kg accessories reply is missing {value}: {accessories_50.reply_text}",
            )

        iraq = await turn("tyb 3al iraq fine esh7an?")
        _require(
            iraq is not None and iraq.status == "shipping_catalog_options",
            "Iraq route-availability request was not handled by the catalogue pipeline.",
        )
        _require("Iraq" in iraq.reply_text, f"Iraq is missing from no-route reply: {iraq.reply_text}")
        for stale_value in _totals(expected["usa_syria_accessories_50"]):
            _require(
                stale_value not in iraq.reply_text,
                f"A stale prior quote leaked into the Iraq reply: {iraq.reply_text}",
            )
        _require(
            conversation.last_shipping_quote is not None
            and str(conversation.last_shipping_quote.get("destination")) == "iraq",
            "Explicit unsupported destination did not replace stale route context.",
        )
    finally:
        settings.shipping_semantic_normalizer_enabled = original_semantic_enabled


async def _optional_semantic_smoke() -> None:
    _require(bool(settings.openai_api_key), "OPENAI_API_KEY is required for --semantic-smoke.")
    catalog = await shipment_client.get_shipping_catalog_summary()
    normalized = await shipping_intent_normalizer.normalize_shipping_intent(
        user_text="Et vers le Qatar, je peux expédier ?",
        catalog=catalog,
        previous_context={"origin": "USA", "destination": "Lebanon"},
    )
    _require(normalized is not None, "Structured semantic normalizer returned no result.")
    _require(
        normalized.is_shipping_catalog_request
        and normalized.request_kind == "route_options"
        and normalized.explicit_destination
        and str(normalized.destination).casefold() == "qatar",
        f"Unexpected semantic normalization: {normalized}",
    )
    _require(
        normalized.origin is None,
        "Semantic normalizer copied the old origin into a non-explicit current-turn field.",
    )


async def main(*, semantic_smoke: bool) -> None:
    try:
        await _verify_catalog_access()
        expected = await _authoritative_expected_quotes()
        await _verify_reported_sequence(expected)
        if semantic_smoke:
            await _optional_semantic_smoke()
    except ShipmentDBError as exc:
        raise RuntimeError(
            "Could not read shipping_db.destinations/shipping_rates with the application "
            "database account. Verify SELECT privileges and SHIPPING_DB_SCHEMA."
        ) from exc
    finally:
        await shipment_client.close()

    suffix = " Semantic normalization also passed." if semantic_smoke else ""
    print(
        "OK: canonical English shipping fields, live catalogue lookup, Decimal pricing, "
        "context replacement, and style-safe rendering are ready." + suffix
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify the canonical multilingual shipping-catalog pipeline."
    )
    parser.add_argument(
        "--semantic-smoke",
        action="store_true",
        help="Also make one live OpenAI structured-normalization request.",
    )
    args = parser.parse_args()
    asyncio.run(main(semantic_smoke=args.semantic_smoke))
