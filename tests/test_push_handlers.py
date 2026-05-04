# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
from unittest.mock import MagicMock

from buckaroo.config.buckaroo_config import BuckarooConfig
from buckaroo.http.client import BuckarooHttpClient
from buckaroo.services.reply import HttpPost
from werkzeug.exceptions import Forbidden

from odoo.tests import BaseCase

from odoo.addons.payment_buckaroo_official.utils.push_handlers import (
    ParsedPush,
    parse_push,
    verify_signature,
)

from .common import make_mock_request, parsed_from_form, parsed_from_json


def _make_request(content_type='', form=None, json_body=None):
    """Build a minimal mock ``odoo.http.request`` for testing push parsing."""
    mock_request = MagicMock()
    mock_request.httprequest.content_type = content_type
    mock_request.httprequest.headers = {}
    mock_request.httprequest.url = None
    mock_request.httprequest.method = None

    form_data = form or {}
    mock_request.httprequest.values = form_data
    mock_request.httprequest.form = form_data

    raw_json = json.dumps(json_body) if json_body is not None else ''
    mock_request.httprequest.get_data.return_value = raw_json
    return mock_request



class TestParsePush(BaseCase):

    def test_json_content_type_returns_parsed_push(self):
        req = _make_request(
            content_type='application/json',
            json_body={'Transaction': {'Invoice': 'INV-1'}},
        )
        parsed = parse_push(req)
        self.assertIsInstance(parsed, ParsedPush)
        self.assertEqual(parsed.reference, 'INV-1')

    def test_form_content_type_returns_parsed_push(self):
        req = _make_request(
            content_type='application/x-www-form-urlencoded',
            form={'brq_invoicenumber': 'TX-001'},
        )
        parsed = parse_push(req)
        self.assertIsInstance(parsed, ParsedPush)
        self.assertEqual(parsed.reference, 'TX-001')

    def test_empty_content_type_falls_back_to_form(self):
        parsed = parse_push(_make_request(
            content_type='',
            form={'brq_invoicenumber': 'TX-001'},
        ))
        self.assertEqual(parsed.reference, 'TX-001')

    def test_none_content_type_falls_back_to_form(self):
        mock_req = MagicMock()
        mock_req.httprequest.content_type = None
        mock_req.httprequest.values = {'brq_invoicenumber': 'TX-001'}
        parsed = parse_push(mock_req)
        self.assertIsInstance(parsed, ParsedPush)
        self.assertEqual(parsed.reference, 'TX-001')



class TestParsedPushForm(BaseCase):

    def _parsed(self, **overrides):
        raw = {
            'brq_invoicenumber': 'TX-001',
            'brq_amount': '50.00',
            'brq_currency': 'EUR',
            'brq_statuscode': '190',
            'brq_transactions': 'KEY_123',
            'brq_signature': 'abc',
        }
        raw.update(overrides)
        return parsed_from_form(raw)

    def test_reference(self):
        self.assertEqual(self._parsed().reference, 'TX-001')

    def test_reference_fallback_to_description(self):
        self.assertEqual(parsed_from_form({'brq_description': 'DESC-001'}).reference, 'DESC-001')

    def test_amount(self):
        self.assertEqual(self._parsed().amount, 50.0)

    def test_amount_missing(self):
        self.assertIsNone(parsed_from_form({}).amount)

    def test_currency(self):
        self.assertEqual(self._parsed().currency, 'EUR')

    def test_status_code(self):
        self.assertEqual(self._parsed().status_code, 190)

    def test_status_code_invalid(self):
        self.assertIsNone(parsed_from_form({'brq_statuscode': 'bad'}).status_code)

    def test_transaction_key(self):
        self.assertEqual(self._parsed().transaction_key, 'KEY_123')

    def test_signature(self):
        self.assertEqual(self._parsed().signature, 'abc')

    def test_raw_preserves_case(self):
        raw = {'BRQ_AMOUNT': '10.00', 'brq_currency': 'EUR'}
        self.assertEqual(parsed_from_form(raw).raw, raw)

    def test_is_success(self):
        self.assertTrue(self._parsed(brq_statuscode='190').is_success())
        self.assertFalse(self._parsed(brq_statuscode='890').is_success())

    def test_is_pending(self):
        self.assertTrue(self._parsed(brq_statuscode='790').is_pending())

    def test_is_cancelled(self):
        self.assertTrue(self._parsed(brq_statuscode='890').is_cancelled())

    def test_is_failed(self):
        self.assertTrue(self._parsed(brq_statuscode='490').is_failed())

    def test_parse_push_preserves_form_case_in_raw(self):
        form_data = {
            'BRQ_INVOICENUMBER': 'TX-001',
            'BRQ_AMOUNT': '50.00',
            'brq_signature': 'sig',
        }
        req = _make_request(
            content_type='application/x-www-form-urlencoded',
            form=form_data,
        )
        parsed = parse_push(req)
        self.assertEqual(parsed.reference, 'TX-001')
        self.assertEqual(parsed.amount, 50.0)
        self.assertEqual(parsed.signature, 'sig')
        self.assertEqual(parsed.raw['BRQ_INVOICENUMBER'], 'TX-001')



class TestParsedPushJson(BaseCase):

    def _payload(self, status_code=190, key='TXN_KEY', invoice='TX-001',
                 amount=50.0, currency='EUR'):
        return {
            'Transaction': {
                'Key': key,
                'Invoice': invoice,
                'Currency': currency,
                'AmountDebit': amount,
                'Status': {
                    'Code': {'Code': status_code, 'Description': 'Success'},
                    'SubCode': {'Code': 'S001', 'Description': 'Approved'},
                    'DateTime': '2025-01-01T12:00:00',
                },
                'Signature': 'json_sig',
            },
        }

    def _parsed(self, **kwargs):
        return parsed_from_json(self._payload(**kwargs))

    def test_reference(self):
        self.assertEqual(self._parsed().reference, 'TX-001')

    def test_amount(self):
        self.assertEqual(self._parsed().amount, 50.0)

    def test_amount_missing(self):
        self.assertIsNone(parsed_from_json({}).amount)

    def test_currency(self):
        self.assertEqual(self._parsed().currency, 'EUR')

    def test_status_code(self):
        self.assertEqual(self._parsed().status_code, 190)

    def test_transaction_key(self):
        self.assertEqual(self._parsed().transaction_key, 'TXN_KEY')

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
            'Transaction': {
                'Key': 'K1',
                'Invoice': 'INV-1',
                'AmountDebit': 25.0,
                'Currency': 'EUR',
                'Status': {'Code': {'Code': 190, 'Description': 'OK'}},
                'Signature': 'sig',
            },
        }
        req = _make_request(content_type='application/json', json_body=payload)
        parsed = parse_push(req)
        self.assertEqual(parsed.reference, 'INV-1')
        self.assertEqual(parsed.amount, 25.0)
        self.assertTrue(parsed.is_success())

    def test_parse_push_empty_json_body(self):
        req = _make_request(content_type='application/json')
        req.httprequest.get_data.return_value = ''
        self.assertIsNone(parse_push(req).reference)

    def test_service_code_from_services_list(self):
        payload = self._payload()
        payload['Transaction']['Services'] = [{'Name': 'ideal'}]
        self.assertEqual(parsed_from_json(payload).service_code, 'ideal')

    def test_service_code_fallback_to_service_code(self):
        payload = self._payload()
        payload['Transaction']['Services'] = []
        payload['Transaction']['ServiceCode'] = 'ideal'
        self.assertEqual(parsed_from_json(payload).service_code, 'ideal')

    def test_service_code_missing(self):
        self.assertIsNone(parsed_from_json(self._payload()).service_code)

    def test_credit_amount(self):
        payload = self._payload()
        payload['Transaction']['AmountCredit'] = 25.50
        self.assertEqual(parsed_from_json(payload).credit_amount, 25.5)

    def test_parse_push_invalid_json(self):
        req = MagicMock()
        req.httprequest.content_type = 'application/json'
        req.httprequest.get_data.return_value = 'not-json{'
        self.assertIsNone(parse_push(req).reference)

    def test_json_raw_is_top_level_payload(self):
        """Signature is computed over the top-level payload, so raw preserves it."""
        payload = self._payload()
        parsed = parsed_from_json(payload)
        self.assertEqual(parsed.raw, payload)



class TestVerifySignature(BaseCase):

    SECRET = 'test_secret'
    STORE = 'test_store'

    def _provider(self):
        provider = MagicMock()
        provider.buckaroo_official_secret_key = self.SECRET
        provider.buckaroo_official_website_key = self.STORE
        return provider

    def test_form_missing_signature_raises_forbidden(self):
        parsed = parsed_from_form({'brq_invoicenumber': 'TX-001'})
        with self.assertRaises(Forbidden):
            verify_signature(parsed, self._provider())

    def test_form_invalid_signature_raises_forbidden(self):
        parsed = parsed_from_form({
            'brq_invoicenumber': 'TX-001',
            'brq_signature': 'bad_sig',
        })
        with self.assertRaises(Forbidden):
            verify_signature(parsed, self._provider())

    def _signed_json_request(self, body):
        url = 'https://shop.example.com/payment/buckaroo_official/webhook'
        header = BuckarooHttpClient(self.STORE, self.SECRET, BuckarooConfig())\
            ._generate_hmac_signature('POST', url, body)['Authorization']
        req = make_mock_request(content_type='application/json', body=body)
        req.httprequest.headers = {'Authorization': header}
        req.httprequest.url = url
        req.httprequest.method = 'POST'
        return req

    def test_form_matching_signature_does_not_raise(self):
        params = {'brq_invoicenumber': 'TX-001'}
        sig = HttpPost(self.SECRET).compute_signature(params)
        parsed = parsed_from_form({**params, 'brq_signature': sig})
        self.assertIsNone(verify_signature(parsed, self._provider()))

    def test_json_missing_authorization_raises_forbidden(self):
        parsed = parsed_from_json({'Transaction': {'Invoice': 'TX-1'}})
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
