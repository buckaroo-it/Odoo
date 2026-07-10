# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Unit tests for shared BNPL helpers (module-level functions in helpers/).

These tests use only mocks and do not require a database connection.
"""

from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests import BaseCase

from odoo.addons.payment_buckaroo_official.helpers.articles import get_order_articles
from odoo.addons.payment_buckaroo_official.helpers.customer import (
    get_customer_data,
    get_shipping_partner,
    is_b2b,
    parse_street,
    resolve_b2b_registry,
    sanitize_phone,
    validate_bnpl_registry,
)


def make_tax(amount_type="percent", amount=21.0):
    tax = MagicMock()
    tax.amount_type = amount_type
    tax.amount = amount
    return tax


def make_order_line(
    product_default_code="PROD-1",
    product_id_val=42,
    name="Product One",
    product_uom_qty=2.0,
    price_total=48.40,
    price_subtotal=40.0,
    taxes=None,
    display_type=False,
    is_delivery=False,
):
    line = MagicMock()
    line.display_type = display_type
    line.is_delivery = is_delivery

    product = MagicMock()
    product.default_code = product_default_code
    product.id = product_id_val
    product.display_name = name
    line.product_id = product
    line.name = name
    line.product_uom_qty = product_uom_qty
    line.price_total = price_total
    line.price_subtotal = price_subtotal
    line.price_tax = round(price_total - price_subtotal, 2)
    line.tax_ids = taxes if taxes is not None else [make_tax(amount=21.0)]
    return line


def make_order(lines=None, partner_shipping_id=None):
    order = MagicMock()
    order.order_line = lines or []
    order.partner_shipping_id = partner_shipping_id
    return order


def make_transaction(amount=96.80, sale_orders=None, partner=None):
    tx = MagicMock()
    tx.amount = amount
    tx.sale_order_ids = sale_orders or []
    tx.partner_id = partner or make_partner()
    return tx


def make_partner(
    name="Jan de Vries",
    street="Keizersgracht 424",
    street2=None,
    zip_code="1016 GC",
    city="Amsterdam",
    country_code="NL",
    email="jan@example.com",
    phone="+31612345678",
    mobile=None,
    is_company=False,
    company_name=None,
    commercial_company_name=None,
    company_registry=None,
    vat=None,
):
    partner = MagicMock()
    partner.name = name
    partner.street = street
    partner.street2 = street2
    partner.zip = zip_code
    partner.city = city
    country = MagicMock()
    country.code = country_code
    partner.country_id = country
    partner.email = email
    partner.phone = phone
    partner.mobile = mobile
    partner.is_company = is_company
    partner.company_name = company_name
    partner.commercial_company_name = commercial_company_name
    partner.company_registry = company_registry
    partner.vat = vat
    return partner


class TestParseStreet(BaseCase):
    def test_simple_street_with_number(self):
        self.assertEqual(parse_street("Keizersgracht 424"), ("Keizersgracht", "424"))

    def test_street_with_number_and_suffix(self):
        self.assertEqual(parse_street("Keizersgracht 424 A"), ("Keizersgracht", "424 A"))

    def test_multi_word_street(self):
        self.assertEqual(
            parse_street("Lange Leidse Dwarsstraat 2"), ("Lange Leidse Dwarsstraat", "2")
        )

    def test_street_without_number(self):
        self.assertEqual(parse_street("Herengracht"), ("Herengracht", ""))

    def test_empty_string(self):
        self.assertEqual(parse_street(""), ("", ""))

    def test_none(self):
        self.assertEqual(parse_street(None), ("", ""))

    def test_number_at_start(self):
        self.assertEqual(parse_street("424 Keizersgracht"), ("Keizersgracht", "424"))

    def test_number_with_suffix_at_start(self):
        self.assertEqual(parse_street("10B Downing Street"), ("Downing Street", "10B"))


class TestGetOrderArticles(BaseCase):
    def test_single_product(self):
        line = make_order_line(
            product_default_code="SKU-001",
            name="Widget",
            product_uom_qty=1.0,
            price_total=12.10,
            price_subtotal=10.0,
            taxes=[make_tax(amount=21.0)],
        )
        order = make_order(lines=[line])
        tx = make_transaction(amount=12.10, sale_orders=[order])
        articles = get_order_articles(tx)
        self.assertEqual(len(articles), 1)
        a = articles[0]
        self.assertEqual(a["identifier"], "SKU-001")
        self.assertEqual(a["description"], "Widget")
        self.assertEqual(a["quantity"], 1.0)
        self.assertAlmostEqual(a["unit_price_incl"], 12.10, places=2)
        self.assertAlmostEqual(a["unit_price_excl"], 10.0, places=2)
        self.assertAlmostEqual(a["vat_percentage"], 21.0, places=2)
        self.assertAlmostEqual(a["vat_amount"], 2.10, places=2)
        self.assertEqual(a["type"], "product")

    def test_identifier_falls_back_to_product_id(self):
        line = make_order_line(product_default_code=None, product_id_val=99)
        order = make_order(lines=[line])
        tx = make_transaction(amount=48.40, sale_orders=[order])
        self.assertEqual(get_order_articles(tx)[0]["identifier"], "99")

    def test_multi_product(self):
        line1 = make_order_line(
            product_default_code="A",
            product_uom_qty=1.0,
            price_total=10.0,
            price_subtotal=10.0,
            taxes=[],
        )
        line2 = make_order_line(
            product_default_code="B",
            product_uom_qty=2.0,
            price_total=20.0,
            price_subtotal=20.0,
            taxes=[],
        )
        order = make_order(lines=[line1, line2])
        tx = make_transaction(amount=30.0, sale_orders=[order])
        self.assertEqual(len(get_order_articles(tx)), 2)

    def test_vat_from_price_tax(self):
        line = make_order_line(
            product_uom_qty=1.0,
            price_total=12.10,
            price_subtotal=10.0,
            taxes=[make_tax(amount=21.0)],
        )
        order = make_order(lines=[line])
        tx = make_transaction(amount=12.10, sale_orders=[order])
        self.assertAlmostEqual(get_order_articles(tx)[0]["vat_percentage"], 21.0, places=2)

    def test_vat_from_price_diff_fallback(self):
        line = make_order_line(
            product_uom_qty=1.0, price_total=12.10, price_subtotal=10.0, taxes=[]
        )
        line.price_tax = None
        order = make_order(lines=[line])
        tx = make_transaction(amount=12.10, sale_orders=[order])
        self.assertAlmostEqual(get_order_articles(tx)[0]["vat_percentage"], 21.0, places=1)

    def test_section_lines_skipped(self):
        normal = make_order_line(
            product_default_code="X",
            product_uom_qty=1.0,
            price_total=10.0,
            price_subtotal=10.0,
            taxes=[],
        )
        section = make_order_line(display_type="line_section")
        order = make_order(lines=[normal, section])
        tx = make_transaction(amount=10.0, sale_orders=[order])
        self.assertEqual(len(get_order_articles(tx)), 1)

    def test_rounding_correction_when_mismatch(self):
        line1 = make_order_line(
            product_default_code="R1",
            product_uom_qty=1.0,
            price_total=4.99,
            price_subtotal=4.99,
            taxes=[],
        )
        line2 = make_order_line(
            product_default_code="R2",
            product_uom_qty=1.0,
            price_total=5.00,
            price_subtotal=5.00,
            taxes=[],
        )
        order = make_order(lines=[line1, line2])
        tx = make_transaction(amount=10.00, sale_orders=[order])
        articles = get_order_articles(tx)
        self.assertEqual(len(articles), 3)
        self.assertEqual(articles[-1]["type"], "rounding")
        self.assertAlmostEqual(articles[-1]["unit_price_incl"], 0.01, places=2)

    def test_no_rounding_when_exact(self):
        line = make_order_line(
            product_default_code="EXACT",
            product_uom_qty=2.0,
            price_total=20.00,
            price_subtotal=20.00,
            taxes=[],
        )
        order = make_order(lines=[line])
        tx = make_transaction(amount=20.00, sale_orders=[order])
        self.assertEqual(len(get_order_articles(tx)), 1)

    def test_empty_when_no_orders(self):
        tx = make_transaction(amount=0.0, sale_orders=[])
        self.assertEqual(get_order_articles(tx), [])

    def test_shipping_line_type(self):
        delivery = make_order_line(
            product_default_code=None,
            product_id_val=500,
            name="Shipping",
            product_uom_qty=1.0,
            price_total=5.00,
            price_subtotal=5.00,
            taxes=[],
            is_delivery=True,
        )
        order = make_order(lines=[delivery])
        tx = make_transaction(amount=5.00, sale_orders=[order])
        self.assertEqual(get_order_articles(tx)[0]["type"], "shipping")

    def test_multiple_orders_combined(self):
        line1 = make_order_line(
            product_default_code="O1",
            product_uom_qty=1.0,
            price_total=5.0,
            price_subtotal=5.0,
            taxes=[],
        )
        line2 = make_order_line(
            product_default_code="O2",
            product_uom_qty=1.0,
            price_total=5.0,
            price_subtotal=5.0,
            taxes=[],
        )
        tx = make_transaction(amount=10.0, sale_orders=[make_order([line1]), make_order([line2])])
        self.assertEqual(len(get_order_articles(tx)), 2)


class TestIsB2B(BaseCase):
    def test_b2c(self):
        self.assertFalse(is_b2b(make_partner(is_company=False, commercial_company_name=None)))

    def test_b2b_is_company(self):
        self.assertTrue(is_b2b(make_partner(is_company=True)))

    def test_b2b_commercial_name(self):
        self.assertTrue(is_b2b(make_partner(is_company=False, commercial_company_name="Acme BV")))

    def test_b2b_both(self):
        self.assertTrue(is_b2b(make_partner(is_company=True, commercial_company_name="Acme")))


class TestGetCustomerData(BaseCase):
    def test_name_split(self):
        data = get_customer_data(make_partner(name="Jan de Vries"))
        self.assertEqual(data["first_name"], "Jan")
        self.assertEqual(data["last_name"], "de Vries")

    def test_single_word_name(self):
        data = get_customer_data(make_partner(name="Jan"))
        self.assertEqual(data["first_name"], "Jan")
        self.assertEqual(data["last_name"], "")

    def test_initials(self):
        data = get_customer_data(make_partner(name="Jan de Vries"))
        self.assertEqual(data["initials"], "J.D.V.")

    def test_street_parsed(self):
        data = get_customer_data(make_partner(street="Keizersgracht 424"))
        self.assertEqual(data["street_name"], "Keizersgracht")
        self.assertEqual(data["house_number"], "424")

    def test_address_fields(self):
        data = get_customer_data(
            make_partner(
                zip_code="1016 GC",
                city="Amsterdam",
                country_code="NL",
                email="jan@example.com",
            )
        )
        self.assertEqual(data["postal_code"], "1016 GC")
        self.assertEqual(data["city"], "Amsterdam")
        self.assertEqual(data["country_code"], "NL")
        self.assertEqual(data["email"], "jan@example.com")

    def test_phone(self):
        self.assertEqual(
            get_customer_data(make_partner(phone="+31612345678"))["phone"], "+31612345678"
        )

    def test_phone_empty(self):
        self.assertEqual(get_customer_data(make_partner(phone=None))["phone"], "")

    def test_phone_prefers_mobile_when_present(self):
        # Riverty NL/BE accept either MobilePhone or Phone; prefer the
        # dedicated mobile field so the customer's actual mobile flows
        # through and is reachable for SMS-based fraud verification.
        data = get_customer_data(
            make_partner(
                phone="+31201234567",
                mobile="+31612345678",
            )
        )
        self.assertEqual(data["phone"], "+31612345678")

    def test_phone_falls_back_to_landline_when_mobile_empty(self):
        data = get_customer_data(make_partner(phone="+31201234567", mobile=None))
        self.assertEqual(data["phone"], "+31201234567")

    def test_missing_country(self):
        partner = make_partner()
        partner.country_id = None
        self.assertEqual(get_customer_data(partner)["country_code"], "")

    def test_b2b_company_fields(self):
        data = get_customer_data(
            make_partner(
                is_company=True,
                commercial_company_name="Acme BV",
                company_registry="12345678",
                vat="NL123456789B01",
            )
        )
        self.assertTrue(data["is_b2b"])
        self.assertEqual(data["company_name"], "Acme BV")
        self.assertEqual(data["chamber_of_commerce"], "12345678")
        self.assertEqual(data["vat_number"], "NL123456789B01")

    def test_b2c_empty_company_fields(self):
        data = get_customer_data(make_partner(is_company=False, commercial_company_name=None))
        self.assertFalse(data["is_b2b"])
        self.assertEqual(data["company_name"], "")

    def test_street2_fallback(self):
        data = get_customer_data(make_partner(street="Herengracht", street2="42"))
        self.assertEqual(data["house_number"], "42")


class TestSanitizePhone(BaseCase):
    def test_strips_plus_and_whitespace(self):
        self.assertEqual(sanitize_phone("+31 20 123 4567"), "31201234567")

    def test_strips_plus_only(self):
        self.assertEqual(sanitize_phone("+31612345678"), "31612345678")

    def test_empty_string(self):
        self.assertEqual(sanitize_phone(""), "")

    def test_none(self):
        self.assertEqual(sanitize_phone(None), "")

    def test_strips_dashes_and_parens(self):
        self.assertEqual(sanitize_phone("(020) 123-4567"), "0201234567")


class TestGetShippingPartner(BaseCase):
    def test_falls_back_to_billing(self):
        billing = make_partner(city="Utrecht")
        tx = make_transaction(partner=billing, sale_orders=[])
        self.assertEqual(get_shipping_partner(tx), billing)

    def test_falls_back_when_same(self):
        billing = make_partner(city="Leiden")
        tx = make_transaction(
            partner=billing, sale_orders=[make_order(partner_shipping_id=billing)]
        )
        self.assertEqual(get_shipping_partner(tx), billing)

    def test_uses_different_shipping(self):
        billing = make_partner(city="Amsterdam")
        shipping = make_partner(city="Rotterdam")
        tx = make_transaction(
            partner=billing, sale_orders=[make_order(partner_shipping_id=shipping)]
        )
        self.assertEqual(get_shipping_partner(tx), shipping)

    def test_uses_first_different(self):
        billing = make_partner(city="Amsterdam")
        ship1 = make_partner(city="Rotterdam")
        tx = make_transaction(
            partner=billing,
            sale_orders=[
                make_order(partner_shipping_id=ship1),
                make_order(partner_shipping_id=make_partner(city="Eindhoven")),
            ],
        )
        self.assertEqual(get_shipping_partner(tx), ship1)


class TestResolveB2BRegistry(BaseCase):
    def test_session_value_wins(self):
        mock_req = MagicMock()
        mock_req.session = {"buckaroo_b2b_registry": "12345678"}
        partner = make_partner(company_registry="87654321")
        tx = make_transaction(partner=partner)
        with patch("odoo.http.request", mock_req):
            self.assertEqual(resolve_b2b_registry(tx, "buckaroo_b2b_registry"), "12345678")

    def test_falls_back_to_partner_company_registry(self):
        mock_req = MagicMock()
        mock_req.session = {}
        partner = make_partner(company_registry="87654321")
        tx = make_transaction(partner=partner)
        with patch("odoo.http.request", mock_req):
            self.assertEqual(resolve_b2b_registry(tx, "buckaroo_b2b_registry"), "87654321")

    def test_returns_empty_when_neither_present(self):
        mock_req = MagicMock()
        mock_req.session = {}
        partner = make_partner(company_registry=None)
        tx = make_transaction(partner=partner)
        with patch("odoo.http.request", mock_req):
            self.assertEqual(resolve_b2b_registry(tx, "buckaroo_b2b_registry"), "")

    def test_raises_when_missing_error_supplied_and_nothing_found(self):
        mock_req = MagicMock()
        mock_req.session = {}
        partner = make_partner(company_registry=None)
        tx = make_transaction(partner=partner)
        with patch("odoo.http.request", mock_req):
            with self.assertRaises(ValidationError):
                resolve_b2b_registry(
                    tx,
                    "buckaroo_b2b_registry",
                    missing_error="Please enter your registry number.",
                )


class TestValidateBnplRegistry(BaseCase):
    def _make_request(self, session=None, is_public=False):
        mock_req = MagicMock()
        mock_req.session = session if session is not None else {}
        user = MagicMock()
        user._is_public.return_value = is_public
        mock_req.env.user = user
        return mock_req, user

    def test_raises_when_empty(self):
        mock_req, _user = self._make_request()
        with patch("odoo.http.request", mock_req):
            with self.assertRaises(ValidationError):
                validate_bnpl_registry(
                    {},
                    registry_kwarg="registry",
                    session_key="buckaroo_b2b_registry",
                    missing_msg="Please enter your registry number.",
                )

    def test_raises_when_whitespace_only(self):
        mock_req, _user = self._make_request()
        with patch("odoo.http.request", mock_req):
            with self.assertRaises(ValidationError):
                validate_bnpl_registry(
                    {"registry": "   "},
                    registry_kwarg="registry",
                    session_key="buckaroo_b2b_registry",
                    missing_msg="Please enter your registry number.",
                )

    def test_persists_session_and_logged_in_partner(self):
        mock_req, user = self._make_request(is_public=False)
        with patch("odoo.http.request", mock_req):
            result = validate_bnpl_registry(
                {"registry": "12345678"},
                registry_kwarg="registry",
                session_key="buckaroo_b2b_registry",
                missing_msg="Please enter your registry number.",
            )
        self.assertEqual(result, "12345678")
        self.assertEqual(mock_req.session["buckaroo_b2b_registry"], "12345678")
        self.assertEqual(user.partner_id.sudo().company_registry, "12345678")

    def test_skips_partner_write_for_public_user(self):
        mock_req, user = self._make_request(is_public=True)
        with patch("odoo.http.request", mock_req):
            validate_bnpl_registry(
                {"registry": "12345678"},
                registry_kwarg="registry",
                session_key="buckaroo_b2b_registry",
                missing_msg="Please enter your registry number.",
            )
        self.assertEqual(mock_req.session["buckaroo_b2b_registry"], "12345678")
        user.partner_id.sudo.assert_not_called()

    def test_persists_trimmed_value_not_raw_input(self):
        # A value with stray leading/trailing whitespace must be stored
        # trimmed everywhere (session, partner, return value) — Buckaroo
        # can reject a registry number with stray whitespace even though
        # it passes the "is it non-empty" validation.
        mock_req, user = self._make_request(is_public=False)
        with patch("odoo.http.request", mock_req):
            result = validate_bnpl_registry(
                {"registry": "  12345678  "},
                registry_kwarg="registry",
                session_key="buckaroo_b2b_registry",
                missing_msg="Please enter your registry number.",
            )
        self.assertEqual(result, "12345678")
        self.assertEqual(mock_req.session["buckaroo_b2b_registry"], "12345678")
        self.assertEqual(user.partner_id.sudo().company_registry, "12345678")
