<p align="center">
  <a href="https://www.buckaroo.nl">
    <img src="https://raw.githubusercontent.com/buckaroo-it/Media/main/Buckaroo/README.md%20Headers/buckaroo-odoo-header-rounded.png" alt="Buckaroo — Payments for Odoo" width="100%">
  </a>
</p>

<h1 align="center">Buckaroo for Odoo</h1>

<p align="center">
  <a href="https://github.com/buckaroo-it/Odoo/releases"><img src="https://img.shields.io/github/v/release/buckaroo-it/Odoo.svg?label=release" alt="Latest release"></a>
  <a href="https://github.com/buckaroo-it/Odoo/blob/19.0/LICENSE"><img src="https://img.shields.io/github/license/buckaroo-it/Odoo.svg?label=license" alt="License"></a>
  <a href="https://docs.buckaroo.io/docs/odoo"><img src="https://img.shields.io/badge/docs-docs.buckaroo.io-1a1a4b.svg" alt="Documentation"></a>
  <a href="https://apps.odoo.com/apps/modules/19.0/payment_buckaroo_official"><img src="https://img.shields.io/badge/Odoo%20Apps-download-714b67.svg" alt="Download from Odoo Apps"></a>
</p>

<p align="center">
  <a href="#about">About</a> &middot;
  <a href="#requirements">Requirements</a> &middot;
  <a href="#installation">Installation</a> &middot;
  <a href="#upgrade">Upgrade</a> &middot;
  <a href="#configuration">Configuration</a> &middot;
  <a href="#payment-methods">Payment methods</a> &middot;
  <a href="#support">Support</a> &middot;
  <a href="#contribute">Contribute</a>
</p>

---

## About

Odoo is an open source suite of business applications, including eCommerce, accounting, inventory and CRM.

The Buckaroo app for Odoo connects your webshop to the Buckaroo payment gateway, so you can start accepting payments within minutes. Buckaroo is a Dutch Payment Service Provider. The app plugs into Odoo's payment framework and runs on Odoo.sh and self-hosted installations.

> [!IMPORTANT]
> This is the official Buckaroo app, published as `payment_buckaroo_official`. Odoo also ships a built-in `payment_buckaroo` provider, which is maintained by Odoo and covers only a limited set of payment methods. Install this app to use the full range of methods listed below.

[Full plugin documentation on docs.buckaroo.io](https://docs.buckaroo.io/docs/odoo)

---

## Requirements

| Requirement | Supported versions |
|---|---|
| Odoo | 19.0 |
| Editions | Community and Enterprise |
| Hosting | Odoo.sh and self-hosted |

You also need a Buckaroo account. Don't have one yet? [Request an account](https://www.buckaroo.nl/start).

> [!NOTE]
> Odoo Online is not supported, because that platform does not allow custom modules to be installed. Use Odoo.sh or a self-hosted installation instead.

---

## Installation

Download the module from the [Odoo Apps store](https://apps.odoo.com/apps/modules/19.0/payment_buckaroo_official), or clone it from this repository:

```bash
git clone --branch 19.0 https://github.com/buckaroo-it/Odoo.git
```

Then add the module to your database:

**Self-hosted** — copy the module into your addons path and install the Python dependencies:

```bash
cp -r Odoo/payment_buckaroo_official /path/to/odoo/addons/
pip install -r Odoo/requirements.txt
```

Restart the Odoo service afterwards.

**Odoo.sh** — commit the `payment_buckaroo_official` folder into your project repository and add the contents of `requirements.txt` to your project's `requirements.txt`, then let the build complete.

Finally, in Odoo go to **Apps**, click **Update Apps List**, search for **Buckaroo** and click **Activate**.

---

## Upgrade

Replace the module folder with the latest version, then upgrade it. On Odoo.sh, commit the new version and let the build run. On a self-hosted database, run:

```bash
./odoo-bin -d YOUR_DATABASE -u payment_buckaroo_official --stop-after-init
```

> [!TIP]
> Always test an upgrade on a staging database first and check the [changelog](https://github.com/buckaroo-it/Odoo/blob/19.0/CHANGELOG.md) for breaking changes.

---

## Configuration

In Odoo, go to **Invoicing → Configuration → Payment Providers** (or **Website → Configuration → Payment Providers**), open **Buckaroo** and set its state to **Enabled** or **Test mode**.

You will need your **Store key** and **Secret key**, which you can find under [API credentials in Buckaroo Plaza](https://plaza.buckaroo.nl/Configuration/Merchant/ApiKeys). The Store key is unique per store, the Secret key applies to your whole account.

Push messages also need to be enabled in Buckaroo Plaza so Odoo is notified of the payment result.

Step-by-step instructions: [Configuring the Odoo app](https://docs.buckaroo.io/docs/odoo-configuration)

---

## Payment methods

The app supports the following payment methods. Each one can be enabled or disabled individually and switched between live and test mode.

| | | |
|---|---|---|
| [Alipay](https://docs.buckaroo.io/docs/alipay) | [Apple Pay](https://docs.buckaroo.io/docs/apple-pay) | [Bancontact](https://docs.buckaroo.io/docs/bancontact) |
| [Bank Transfer](https://docs.buckaroo.io/docs/transfer) | [Belfius](https://docs.buckaroo.io/docs/belfius) | [Billink](https://docs.buckaroo.io/docs/billink) |
| [Bizum](https://docs.buckaroo.io/docs/bizum) | [Blik](https://docs.buckaroo.io/docs/blik) | [Credit and debit cards](https://docs.buckaroo.io/docs/creditcards) |
| [EPS](https://docs.buckaroo.io/docs/eps) | [Giftcards](https://docs.buckaroo.io/docs/giftcards) | [Google Pay™](https://docs.buckaroo.io/docs/google-pay) |
| [iDEAL / Wero](https://docs.buckaroo.io/docs/ideal) | [In3](https://docs.buckaroo.io/docs/in3) | [KBC](https://docs.buckaroo.io/docs/kbc) |
| [Klarna](https://docs.buckaroo.io/docs/klarna-kp) | [MB Way](https://docs.buckaroo.io/docs/mb-way) | [Multibanco](https://docs.buckaroo.io/docs/multibanco) |
| [PayPal](https://docs.buckaroo.io/docs/paypal) | [Przelewy24](https://docs.buckaroo.io/docs/przelewy24) | [Riverty](https://docs.buckaroo.io/docs/riverty) |
| [Swish](https://docs.buckaroo.io/docs/swish) | [Trustly](https://docs.buckaroo.io/docs/trustly) | [Twint](https://docs.buckaroo.io/docs/twint) |
| [WeChatPay](https://docs.buckaroo.io/docs/wechatpay) | [Wero](https://docs.buckaroo.io/docs/wero) |  |

> [!IMPORTANT]
> All supported methods appear in Odoo, but you need an active Buckaroo subscription for a method before you can offer it in your checkout.

---

## Support

Having trouble? Work through this list before reaching out:

1. Check the [frequently asked questions](https://docs.buckaroo.io/docs/odoo-faq).
2. Confirm you are on the [latest release](https://github.com/buckaroo-it/Odoo/releases).
3. Reproduce the issue with the provider in test mode and check the Odoo server log.
4. Verify that your push URL is reachable from outside your network. Buckaroo sends push messages from fixed IP addresses and ports, so make sure these are on your allow list. See [push messages](https://docs.buckaroo.io/docs/integration-push-messages) for the current list.

Still stuck? Contact us and include your Odoo version, app version, hosting type, the relevant log lines and the transaction key.

- **Bug reports and feature requests:** [open an issue](https://github.com/buckaroo-it/Odoo/issues)
- **Technical support:** [support@buckaroo.nl](mailto:support@buckaroo.nl)
- **Phone:** +31 (0)30 711 50 50
- **Gateway status:** [status.buckaroo.io](https://status.buckaroo.io/)

---

## Contribute

We really appreciate it when developers help improve the Buckaroo plugins. Please read our [Contribution Guidelines](https://github.com/buckaroo-it/Odoo/blob/19.0/CONTRIBUTING.md) before opening a pull request, and target the branch matching your Odoo version.

Found a security issue? Please report it privately to [support@buckaroo.nl](mailto:support@buckaroo.nl) instead of opening a public issue.

---

## Versioning

We follow semantic versioning (`MAJOR.MINOR.PATCH`):

- **MAJOR** — breaking changes that require additional testing and caution.
- **MINOR** — new functionality with limited impact.
- **PATCH** — bug fixes and hotfixes only.

All changes are documented in the [changelog](https://github.com/buckaroo-it/Odoo/blob/19.0/CHANGELOG.md) and on the [releases page](https://github.com/buckaroo-it/Odoo/releases).

---

<p align="center">
  <sub>Made with care by <a href="https://www.buckaroo.nl">Buckaroo</a>.<br>
  This document is subject to change; typos and language errors are possible.</sub>
</p>
