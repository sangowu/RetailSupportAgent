"""M11: the summary a confirmation form shows above the argument list.

Built on the pinned τ²-bench records, read through the real domain, so the products,
options and prices are the ones a user of the demo would see.
"""

from __future__ import annotations

import pytest

from retail_support_agent.domain import RetailDomain
from retail_support_agent.form_summary import Reader, reader, summarize
from retail_support_agent.mcp_server import (
    cancel_call,
    exchange_call,
    modify_address_call,
    modify_items_call,
    modify_payment_call,
    return_call,
)

RAJ_CARD = "credit_card_3362387"


@pytest.fixture(scope="module")
def read() -> Reader:
    return reader(RetailDomain().execute)


class TestItemChanges:
    def test_an_exchange_for_a_cheaper_item_is_a_refund(self, read: Reader) -> None:
        call = exchange_call("#W7736708", ["3104857380"], ["7493556126"], RAJ_CARD)
        assert summarize(call, read) == "\n".join(
            [
                "Exchange items in order #W7736708:",
                "- Headphones: type on-ear → over-ear, color red → black; $377.97 → $346.97.",
                f"Price difference: $31.00 refunded to {RAJ_CARD}.",
                "Prices are as of now; the amount is worked out again when it runs.",
            ]
        )

    def test_a_change_to_a_dearer_item_is_a_charge(self, read: Reader) -> None:
        call = modify_items_call("#W4566809", ["5826601160"], ["8302289002"], RAJ_CARD)
        summary = summarize(call, read)
        assert summary is not None
        assert summary.startswith("Change items in order #W4566809:")
        assert "- Air Purifier: room size medium → large, filter type carbon → HEPA;" in summary
        assert f"Price difference: $41.40 charged to {RAJ_CARD}." in summary

    def test_an_item_not_in_the_order_is_said_and_no_total_is_given(self, read: Reader) -> None:
        call = exchange_call("#W7736708", ["1569829406"], ["7493556126"], RAJ_CARD)
        summary = summarize(call, read)
        assert summary is not None
        assert "- item 1569829406: not found in this order." in summary
        assert "Price difference: cannot be worked out from these items." in summary

    def test_an_unknown_new_item_is_said(self, read: Reader) -> None:
        call = exchange_call("#W7736708", ["3104857380"], ["0000000000"], RAJ_CARD)
        summary = summarize(call, read)
        assert summary is not None
        assert "- Headphones (item 3104857380) → item 0000000000: not found." in summary
        assert "Price difference: cannot be worked out from these items." in summary

    def test_lists_of_different_lengths_are_said(self, read: Reader) -> None:
        call = exchange_call("#W7736708", ["3104857380", "9692325258"], ["7493556126"], RAJ_CARD)
        summary = summarize(call, read)
        assert summary is not None
        assert "2 item(s) to replace but 1 new item(s)" in summary
        assert "Price difference: cannot be worked out from these items." in summary


class TestOtherChanges:
    def test_an_address_change_shows_both_addresses(self, read: Reader) -> None:
        call = modify_address_call("#W4135875", "1 Test Street", "", "Austin", "TX", "USA", "73301")
        assert summarize(call, read) == "\n".join(
            [
                "Ship order #W4135875 to: 1 Test Street, Austin, TX 73301, USA.",
                "Currently: 996 Cedar Street, Suite 656, San Antonio, TX 78234, USA.",
            ]
        )

    def test_a_payment_change_shows_both_methods_and_the_total(self, read: Reader) -> None:
        call = modify_payment_call("#W8855135", "credit_card_8105988")
        assert summarize(call, read) == "\n".join(
            [
                "Pay for order #W8855135 ($1,058.79) with credit_card_8105988.",
                "Currently paid with: credit_card_3951670.",
            ]
        )

    def test_an_unreadable_order_is_said(self, read: Reader) -> None:
        summary = summarize(modify_payment_call("#W0000000", "credit_card_8105988"), read)
        assert summary == (
            "Pay for order #W0000000 with credit_card_8105988. The order could not be read."
        )


class TestTheM9WritesKeepTheirForm:
    def test_no_summary_for_a_cancel_or_a_return(self, read: Reader) -> None:
        """Their arguments are the order and a reason or the returned items, which the
        user named; their form stays exactly M9's."""
        cancel = cancel_call("#W4135875", "no longer needed")
        returned = return_call("#W4817420", ["6777246137"], "gift_card_8168843")
        assert summarize(cancel, read) is None
        assert summarize(returned, read) is None
