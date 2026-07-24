# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A [pretix](https://github.com/pretix/pretix) payment plugin that accepts payments through Przelewy24, a Polish payment facilitator. It is a Django app installed into an existing pretix instance — it has no standalone runtime and depends on pretix being installed in the same environment.

## Commands

Development requires a working pretix development setup with its virtualenv activated.

```bash
python setup.py develop        # register the plugin with pretix's plugin registry
make                           # compile translations (django-admin compilemessages)
make localegen                 # regenerate .po files

# Tests (requires: pip install pytest pytest-django; pretix must be installed)
py.test tests                  # DJANGO_SETTINGS_MODULE=pretix.testutils.settings is set in setup.cfg
py.test tests/test_main.py::test_empty   # run a single test

# Code style (CI enforces all three)
black --check .
isort -c .
flake8 .                       # max-line-length=160, see setup.cfg

# Auto-fix
isort .
black .
```

`.install-hooks.sh` installs a pre-commit hook that runs the style checks.

## Architecture

Standard pretix payment-plugin layout — all logic lives in `pretix_przelewy24/`:

- `payment.py` — `Przelewy24(BasePaymentProvider)`, the core of the plugin. Handles settings form, payment registration (`execute_payment` posts to `transaction/register` and redirects the customer to Przelewy24), verification (`_verify_transaction` PUTs to `transaction/verify` and confirms/fails the payment), status checks (`_check_transaction`), refunds (`execute_refund`), and GDPR data shredding (`shred_payment_info`). API requests are signed with SHA-384 over a compact JSON payload including the CRC key (`_sign_payment` / `_sign_verification`). Sandbox vs. production endpoint is a plugin setting.
- `views.py` — three webhook/return endpoints, all subclassing `Przelewy24OrderView` which authenticates requests via pretix tagged order secrets in the URL (`order.tagged_secret(tag)`). `ReturnView` (customer browser returns from Przelewy24), `CallbackView` (server-to-server status notification), `RefundCallbackView`. Callbacks deliberately ignore the payload contents and re-query/verify the transaction with the API instead of trusting the CRC signature; the tagged-secret URL is what authenticates the caller. Payment/refund rows are locked with `select_for_update` to handle the return view and callback racing each other.
- `urls.py` — `event_patterns` under `_przelewy24/`; callback routes use `require_live=False` so notifications work for non-live events.
- `signals.py` — registers the provider via pretix's `register_payment_providers` signal.
- `apps.py` — `PretixPluginMeta` (plugin metadata, `compatibility = "pretix>=2024.7.0"`).

Payment state is stored in `OrderPayment.info_data` (JSON): `token` after registration, then `orderId` and status fields merged in from the callback. `orderId` presence is the precondition for verification and refunds.

Note the amount conversion helpers (`_decimal_to_int` / `_int_to_decimal`): the Przelewy24 API works in minor units (grosze/cents) based on `settings.CURRENCY_PLACES`.

## Conventions

- isort is configured with the `black` profile in `setup.cfg`; `pretix` is a known third-party, `pretix_przelewy24` first-party.
- Translations live in `pretix_przelewy24/locale/`; CI ignores locale changes. User-facing strings use `gettext_lazy`.
- Packaging is via `pretix-plugin-build` (custom build command in `pyproject.toml`); version comes from `pretix_przelewy24/__init__.py`. CI runs `check-manifest`, so new non-Python files may need `MANIFEST.in` entries.
