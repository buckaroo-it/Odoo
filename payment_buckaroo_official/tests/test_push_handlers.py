# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
from unittest.mock import MagicMock, patch

from buckaroo.config.buckaroo_config import BuckarooConfig
from buckaroo.http.client import BuckarooHttpClient
from buckaroo.services.reply import HttpPost
from werkzeug.exceptions import Forbidden

from odoo.addons.account_payment.models.payment_transaction import PaymentTransaction
from odoo.tests import BaseCase, tagged

from odoo.addons.payment_buckaroo_official.utils.push_handlers import (
    ParsedPush,
    parse_push,
    verify_signature,
)

from .common import BuckarooOfficialCommon, make_mock_request, parsed_from_form, parsed_from_json


def _route_push(env, parsed):
    """Drive the same split + process path the webhook controller runs."""
    tx_sudo = env["payment.transaction"].sudo()._search_by_reference("buckaroo_official", parsed)
    if not tx_sudo:
        return None
    remainder_tx = tx_sudo._buckaroo_split_remainder_push(parsed)
    target = remainder_tx or tx_sudo
    target._process("buckaroo_official", parsed)
    if remainder_tx and remainder_tx.state == "done":
        remainder_tx._post_process()
    return remainder_tx or tx_sudo


def _refund_push(invoicenumber, refund_relation, txn_key, credit="50.00", service="ideal"):
    return parsed_from_form(
        {
            "brq_invoicenumber": invoicenumber,
            "brq_amount_credit": credit,
            "brq_currency": "EUR",
            "brq_statuscode": "190",
            "brq_transactions": txn_key,
            "brq_relatedtransaction_refund": refund_relation,
            "brq_transaction_method": service,
        }
    )


def _make_request(content_type="", form=None, json_body=None):
    """Build a minimal mock ``odoo.http.request`` for testing push parsing."""
    mock_request = MagicMock()
    mock_request.httprequest.content_type = content_type
    mock_request.httprequest.headers = {}
    mock_request.httprequest.url = None
    mock_request.httprequest.method = None

    form_data = form or {}
    mock_request.httprequest.values = form_data
    mock_request.httprequest.form = form_data

    raw_json = json.dumps(json_body) if json_body is not None else ""
    mock_request.httprequest.get_data.return_value = raw_json
    return mock_request


class TestParsePush(BaseCase):
    def test_json_content_type_returns_parsed_push(self):
        req = _make_request(
            content_type="application/json",
            json_body={"Transaction": {"Invoice": "INV-1"}},
        )
        parsed = parse_push(req)
        self.assertIsInstance(parsed, ParsedPush)
        self.assertEqual(parsed.reference, "INV-1")

    def test_form_content_type_returns_parsed_push(self):
        req = _make_request(
            content_type="application/x-www-form-urlencoded",
            form={"brq_invoicenumber": "TX-001"},
        )
        parsed = parse_push(req)
        self.assertIsInstance(parsed, ParsedPush)
        self.assertEqual(parsed.reference, "TX-001")

    def test_empty_content_type_falls_back_to_form(self):
        parsed = parse_push(
            _make_request(
                content_type="",
                form={"brq_invoicenumber": "TX-001"},
            )
        )
        self.assertEqual(parsed.reference, "TX-001")

    def test_none_content_type_falls_back_to_form(self):
        mock_req = MagicMock()
        mock_req.httprequest.content_type = None
        mock_req.httprequest.values = {"brq_invoicenumber": "TX-001"}
        parsed = parse_push(mock_req)
        self.assertIsInstance(parsed, ParsedPush)
        self.assertEqual(parsed.reference, "TX-001")


class TestParsedPushForm(BaseCase):
    def _parsed(self, **overrides):
        raw = {
            "brq_invoicenumber": "TX-001",
            "brq_amount": "50.00",
            "brq_currency": "EUR",
            "brq_statuscode": "190",
            "brq_transactions": "KEY_123",
            "brq_signature": "abc",
        }
        raw.update(overrides)
        return parsed_from_form(raw)

    def test_reference(self):
        self.assertEqual(self._parsed().reference, "TX-001")

    def test_reference_fallback_to_description(self):
        self.assertEqual(parsed_from_form({"brq_description": "DESC-001"}).reference, "DESC-001")

    def test_amount(self):
        self.assertEqual(self._parsed().amount, 50.0)

    def test_amount_missing(self):
        self.assertIsNone(parsed_from_form({}).amount)

    def test_currency(self):
        self.assertEqual(self._parsed().currency, "EUR")

    def test_status_code(self):
        self.assertEqual(self._parsed().status_code, 190)

    def test_status_code_invalid(self):
        self.assertIsNone(parsed_from_form({"brq_statuscode": "bad"}).status_code)

    def test_transaction_key(self):
        self.assertEqual(self._parsed().transaction_key, "KEY_123")

    def test_partial_payment_relation(self):
        parsed = self._parsed(brq_relatedtransaction_partialpayment="PKEY")
        self.assertEqual(parsed.get_partial_payment_relation(), "PKEY")
        self.assertIsNone(self._parsed().get_partial_payment_relation())

    def test_refund_relation(self):
        parsed = self._parsed(brq_relatedtransaction_refund="RKEY")
        self.assertEqual(parsed.get_refund_relation(), "RKEY")
        self.assertIsNone(self._parsed().get_refund_relation())

    def test_signature(self):
        self.assertEqual(self._parsed().signature, "abc")

    def test_raw_preserves_case(self):
        raw = {"BRQ_AMOUNT": "10.00", "brq_currency": "EUR"}
        self.assertEqual(parsed_from_form(raw).raw, raw)

    def test_is_success(self):
        self.assertTrue(self._parsed(brq_statuscode="190").is_success())
        self.assertFalse(self._parsed(brq_statuscode="890").is_success())

    def test_is_pending(self):
        self.assertTrue(self._parsed(brq_statuscode="790").is_pending())

    def test_is_cancelled(self):
        self.assertTrue(self._parsed(brq_statuscode="890").is_cancelled())

    def test_is_failed(self):
        self.assertTrue(self._parsed(brq_statuscode="490").is_failed())

    def test_parse_push_preserves_form_case_in_raw(self):
        form_data = {
            "BRQ_INVOICENUMBER": "TX-001",
            "BRQ_AMOUNT": "50.00",
            "brq_signature": "sig",
        }
        req = _make_request(
            content_type="application/x-www-form-urlencoded",
            form=form_data,
        )
        parsed = parse_push(req)
        self.assertEqual(parsed.reference, "TX-001")
        self.assertEqual(parsed.amount, 50.0)
        self.assertEqual(parsed.signature, "sig")
        self.assertEqual(parsed.raw["BRQ_INVOICENUMBER"], "TX-001")


class TestParsedPushJson(BaseCase):
    def _payload(
        self, status_code=190, key="TXN_KEY", invoice="TX-001", amount=50.0, currency="EUR"
    ):
        return {
            "Transaction": {
                "Key": key,
                "Invoice": invoice,
                "Currency": currency,
                "AmountDebit": amount,
                "Status": {
                    "Code": {"Code": status_code, "Description": "Success"},
                    "SubCode": {"Code": "S001", "Description": "Approved"},
                    "DateTime": "2025-01-01T12:00:00",
                },
                "Signature": "json_sig",
            },
        }

    def _parsed(self, **kwargs):
        return parsed_from_json(self._payload(**kwargs))

    def test_reference(self):
        self.assertEqual(self._parsed().reference, "TX-001")

    def test_amount(self):
        self.assertEqual(self._parsed().amount, 50.0)

    def test_amount_missing(self):
        self.assertIsNone(parsed_from_json({}).amount)

    def test_currency(self):
        self.assertEqual(self._parsed().currency, "EUR")

    def test_status_code(self):
        self.assertEqual(self._parsed().status_code, 190)

    def test_transaction_key(self):
        self.assertEqual(self._parsed().transaction_key, "TXN_KEY")

    def test_relations_from_related_transactions(self):
        payload = self._payload()
        payload["Transaction"]["RelatedTransactions"] = [
            {"RelationType": "Refund", "RelatedTransactionKey": "RKEY"},
            {"RelationType": "PartialPayment", "RelatedTransactionKey": "PKEY"},
        ]
        parsed = parsed_from_json(payload)
        self.assertEqual(parsed.get_refund_relation(), "RKEY")
        self.assertEqual(parsed.get_partial_payment_relation(), "PKEY")
        self.assertIsNone(self._parsed().get_refund_relation())

    def test_signature_field_is_unused_for_json(self):
        self.assertIsNone(self._parsed().signature)

    def test_is_success(self):
        self.assertTrue(self._parsed(status_code=190).is_success())
        self.assertFalse(self._parsed(status_code=890).is_success())

    def test_is_pending(self):
        self.assertTrue(self._parsed(status_code=791).is_pending())

    def test_is_cancelled(self):
        self.assertTrue(self._parsed(status_code=890).is_cancelled())

    def test_is_failed(self):
        self.assertTrue(self._parsed(status_code=490).is_failed())

    def test_empty_payload(self):
        p = parsed_from_json({})
        self.assertIsNone(p.reference)
        self.assertIsNone(p.amount)
        self.assertIsNone(p.status_code)

    def test_parse_push_json(self):
        payload = {
            "Transaction": {
                "Key": "K1",
                "Invoice": "INV-1",
                "AmountDebit": 25.0,
                "Currency": "EUR",
                "Status": {"Code": {"Code": 190, "Description": "OK"}},
                "Signature": "sig",
            },
        }
        req = _make_request(content_type="application/json", json_body=payload)
        parsed = parse_push(req)
        self.assertEqual(parsed.reference, "INV-1")
        self.assertEqual(parsed.amount, 25.0)
        self.assertTrue(parsed.is_success())

    def test_parse_push_empty_json_body(self):
        req = _make_request(content_type="application/json")
        req.httprequest.get_data.return_value = ""
        self.assertIsNone(parse_push(req).reference)

    def test_service_code_from_services_list(self):
        payload = self._payload()
        payload["Transaction"]["Services"] = [{"Name": "ideal"}]
        self.assertEqual(parsed_from_json(payload).service_code, "ideal")

    def test_service_code_fallback_to_service_code(self):
        payload = self._payload()
        payload["Transaction"]["Services"] = []
        payload["Transaction"]["ServiceCode"] = "ideal"
        self.assertEqual(parsed_from_json(payload).service_code, "ideal")

    def test_service_code_missing(self):
        self.assertIsNone(parsed_from_json(self._payload()).service_code)

    def test_credit_amount(self):
        payload = self._payload()
        payload["Transaction"]["AmountCredit"] = 25.50
        self.assertEqual(parsed_from_json(payload).credit_amount, 25.5)

    def test_parse_push_invalid_json(self):
        req = MagicMock()
        req.httprequest.content_type = "application/json"
        req.httprequest.get_data.return_value = "not-json{"
        self.assertIsNone(parse_push(req).reference)

    def test_json_raw_is_top_level_payload(self):
        """Signature is computed over the top-level payload, so raw preserves it."""
        payload = self._payload()
        parsed = parsed_from_json(payload)
        self.assertEqual(parsed.raw, payload)


class TestServiceParameters(BaseCase):
    def test_form_extracts_service_params(self):
        parsed = parsed_from_form(
            {
                "brq_amount": "50.00",
                "brq_payment_method": "transfer",
                "brq_SERVICE_transfer_IBAN": "NL05RABO0121503038",
                "brq_SERVICE_transfer_BIC": "RABONL2U",
            }
        )
        self.assertEqual(parsed.service_parameters["IBAN"], "NL05RABO0121503038")
        self.assertEqual(parsed.service_parameters["BIC"], "RABONL2U")

    def test_form_ignores_other_services(self):
        """Secondary services on the same push must not bleed into the
        primary service's parameter dict."""
        parsed = parsed_from_form(
            {
                "brq_amount": "50.00",
                "brq_payment_method": "transfer",
                "brq_SERVICE_transfer_IBAN": "NL05RABO0121503038",
                "brq_SERVICE_surcharge_PaymentReference": "BAD_CLOBBER",
            }
        )
        self.assertEqual(parsed.service_parameters, {"IBAN": "NL05RABO0121503038"})

    def test_form_ignores_non_service_keys(self):
        parsed = parsed_from_form(
            {
                "brq_amount": "50.00",
                "brq_payment_method": "transfer",
            }
        )
        self.assertEqual(parsed.service_parameters, {})

    def test_form_no_service_code_yields_empty(self):
        """Without a primary service identified, extraction is skipped."""
        parsed = parsed_from_form({"brq_SERVICE_transfer_IBAN": "X"})
        self.assertEqual(parsed.service_parameters, {})

    def test_form_get_service_parameter_case_insensitive(self):
        parsed = parsed_from_form(
            {
                "brq_payment_method": "transfer",
                "brq_SERVICE_transfer_IBAN": "NL05RABO0121503038",
            }
        )
        self.assertEqual(parsed.get_service_parameter("iban"), "NL05RABO0121503038")
        self.assertEqual(parsed.get_service_parameter("IBAN"), "NL05RABO0121503038")
        self.assertIsNone(parsed.get_service_parameter("Missing"))

    def test_json_extracts_service_params(self):
        parsed = parsed_from_json(
            {
                "Transaction": {
                    "Status": {"Code": {"Code": 792}},
                    "Services": [
                        {
                            "Name": "transfer",
                            "Parameters": [
                                {"Name": "IBAN", "Value": "NL05RABO0121503038"},
                                {"Name": "BIC", "Value": "RABONL2U"},
                                {"Name": "AccountHolderName", "Value": "Buckaroo"},
                                {"Name": "PaymentReference", "Value": "14256252"},
                            ],
                        }
                    ],
                },
            }
        )
        self.assertEqual(parsed.service_parameters["IBAN"], "NL05RABO0121503038")
        self.assertEqual(parsed.service_parameters["BIC"], "RABONL2U")
        self.assertEqual(parsed.service_parameters["AccountHolderName"], "Buckaroo")
        self.assertEqual(parsed.service_parameters["PaymentReference"], "14256252")

    def test_json_empty_services_yields_empty_params(self):
        parsed = parsed_from_json({"Transaction": {"Status": {"Code": {"Code": 190}}}})
        self.assertEqual(parsed.service_parameters, {})


class TestVerifySignature(BaseCase):
    SECRET = "test_secret"
    STORE = "test_store"

    def _provider(self):
        provider = MagicMock()
        provider.buckaroo_official_secret_key = self.SECRET
        provider.buckaroo_official_website_key = self.STORE
        return provider

    def test_form_missing_signature_raises_forbidden(self):
        parsed = parsed_from_form({"brq_invoicenumber": "TX-001"})
        with self.assertRaises(Forbidden):
            verify_signature(parsed, self._provider())

    def test_form_invalid_signature_raises_forbidden(self):
        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-001",
                "brq_signature": "bad_sig",
            }
        )
        with self.assertRaises(Forbidden):
            verify_signature(parsed, self._provider())

    def _signed_json_request(self, body):
        url = "https://shop.example.com/payment/buckaroo_official/webhook"
        header = BuckarooHttpClient(
            self.STORE, self.SECRET, BuckarooConfig()
        )._generate_hmac_signature("POST", url, body)["Authorization"]
        req = make_mock_request(content_type="application/json", body=body)
        req.httprequest.headers = {"Authorization": header}
        req.httprequest.url = url
        req.httprequest.method = "POST"
        return req

    def test_form_matching_signature_does_not_raise(self):
        params = {"brq_invoicenumber": "TX-001"}
        sig = HttpPost(self.SECRET).compute_signature(params)
        parsed = parsed_from_form({**params, "brq_signature": sig})
        self.assertIsNone(verify_signature(parsed, self._provider()))

    def test_json_missing_authorization_raises_forbidden(self):
        parsed = parsed_from_json({"Transaction": {"Invoice": "TX-1"}})
        with self.assertRaises(Forbidden):
            verify_signature(parsed, self._provider())

    def test_json_valid_hmac_does_not_raise(self):
        body = '{"Transaction":{"Invoice":"TX-1","Status":{"Code":{"Code":190}}}}'
        parsed = parse_push(self._signed_json_request(body))
        self.assertIsNone(verify_signature(parsed, self._provider()))

    def test_json_tampered_body_raises_forbidden(self):
        signed_body = '{"Transaction":{"Status":{"Code":{"Code":190}}}}'
        req = self._signed_json_request(signed_body)
        # Replace the body after signing so the HMAC no longer matches.
        tampered = b'{"Transaction":{"Status":{"Code":{"Code":690}}}}'
        req.httprequest.get_data.return_value = tampered
        parsed = parse_push(req)
        with self.assertRaises(Forbidden):
            verify_signature(parsed, self._provider())


@tagged("post_install", "-at_install")
class TestPlazaRefundPush(BuckarooOfficialCommon):
    """A Plaza refund push (190 + ``RelatedTransactions.Refund``) on an
    already-done tx spawns the ``R-`` child generically — the spawn lives on
    the base payment method, so non-giftcard methods are covered too."""

    def setUp(self):
        super().setUp()
        # Isolate the spawn from accounting (the journal-less test provider
        # would otherwise blow up inside _create_payment).
        patcher = patch.object(PaymentTransaction, "_post_process", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _done_ideal_tx(self, reference, key, amount=50.0):
        tx = self._create_buckaroo_tx(reference=reference, amount=amount, payment_method=self.ideal)
        tx.provider_reference = key
        tx._set_done()
        return tx

    def test_refund_push_spawns_refund_child_for_ideal(self):
        tx = self._done_ideal_tx("IDEAL-RF-001", "IDEAL_KEY")

        _route_push(self.env, _refund_push("IDEAL-RF-001", "IDEAL_KEY", "IDEAL_REFUND_KEY"))

        refund = tx.child_transaction_ids.filtered(lambda t: t.operation == "refund")
        self.assertEqual(len(refund), 1)
        self.assertEqual(refund.reference, "R-IDEAL-RF-001")
        self.assertEqual(refund.amount, -50.0)
        self.assertEqual(refund.state, "done")
        self.assertEqual(refund.source_transaction_id, tx)
        self.assertEqual(refund.provider_reference, "IDEAL_REFUND_KEY")

    def test_refund_push_is_idempotent_on_replay(self):
        tx = self._done_ideal_tx("IDEAL-RF-002", "IDEAL_KEY_2")
        push = _refund_push("IDEAL-RF-002", "IDEAL_KEY_2", "IDEAL_REFUND_KEY_2")

        _route_push(self.env, push)
        _route_push(self.env, push)  # replay

        refunds = tx.child_transaction_ids.filtered(lambda t: t.operation == "refund")
        self.assertEqual(len(refunds), 1)

    def test_two_distinct_partial_refunds_each_registered(self):
        tx = self._done_ideal_tx("IDEAL-RF-004", "IDEAL_KEY_4")

        _route_push(
            self.env,
            _refund_push("IDEAL-RF-004", "IDEAL_KEY_4", "IDEAL_REFUND_KEY_4A", credit="20.00"),
        )
        _route_push(
            self.env,
            _refund_push("IDEAL-RF-004", "IDEAL_KEY_4", "IDEAL_REFUND_KEY_4B", credit="15.00"),
        )

        refunds = tx.child_transaction_ids.filtered(lambda t: t.operation == "refund")
        self.assertEqual(len(refunds), 2)

        refund_a = refunds.filtered(lambda t: t.provider_reference == "IDEAL_REFUND_KEY_4A")
        refund_b = refunds.filtered(lambda t: t.provider_reference == "IDEAL_REFUND_KEY_4B")
        self.assertEqual(len(refund_a), 1)
        self.assertEqual(len(refund_b), 1)

        self.assertEqual(refund_a.reference, "R-IDEAL-RF-004")
        self.assertEqual(refund_b.reference, "R-IDEAL-RF-004-1")

        self.assertEqual(refund_a.amount, -20.0)
        self.assertEqual(refund_b.amount, -15.0)

        self.assertEqual(refund_a.state, "done")
        self.assertEqual(refund_b.state, "done")

    def test_duplicate_190_without_refund_relation_does_not_spawn(self):
        tx = self._done_ideal_tx("IDEAL-RF-003", "IDEAL_KEY_3")

        _route_push(
            self.env,
            parsed_from_form(
                {
                    "brq_invoicenumber": "IDEAL-RF-003",
                    "brq_amount": "50.00",
                    "brq_currency": "EUR",
                    "brq_statuscode": "190",
                    "brq_transactions": "SOME_OTHER_KEY",
                    "brq_transaction_method": "ideal",
                }
            ),
        )

        self.assertFalse(tx.child_transaction_ids)
