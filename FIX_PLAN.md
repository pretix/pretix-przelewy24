# Fix plan: false "payment failed" message for successful Przelewy24 payments

## Symptom

Customers completing a payment are shown a "payment failed" error, but the order
turns paid a few seconds later. The pretix log sometimes shows:

```
pretix.base.models.orders orders Failed payment UKS7WV7C-P-1 but ignored due to likely race condition
```

## Root cause

After the customer pays, two requests race back to pretix:

| Endpoint | Trigger | What it does |
|---|---|---|
| `ReturnView` (`views.py:55`) | Customer's browser is redirected back from Przelewy24 | Calls `Przelewy24._check_transaction(payment)` |
| `CallbackView` (`views.py:83`) | Przelewy24 server-to-server status notification | Calls `Przelewy24._verify_transaction(payment)` → `payment.confirm()` |

The bug is in `_check_transaction` (`pretix_przelewy24/payment.py:355-382`):

```python
r.raise_for_status()
payment.fail(                                   # <-- called UNCONDITIONALLY
    info={
        **payment.info_data,
        **r.json()["data"],
    }
)

# Seems like we can't detect a pending payment properly :(
if r.json()["data"]["status"] != 1:
    raise PaymentException(_("No successful payment was detected."))
```

`payment.fail(info=...)` is misused here as a way to persist the fetched
transaction data into `payment.info_data`. But `OrderPayment.fail()` in pretix
also:

- sets the payment state to `PAYMENT_STATE_FAILED`,
- logs `pretix.event.order.payment.failed`,
- **sends the customer a "payment failed" email**.

It runs *before* the `status` check, so it fires even for successful payments.

### The two race orderings explain both symptoms

1. **Return view arrives first** (the browser redirect is usually faster than the
   notification): the successful payment is immediately marked FAILED and the
   failure email is sent — this is the "payment failed" message customers see.
   Seconds later `CallbackView` runs `_verify_transaction`, which succeeds and
   calls `payment.confirm()`. pretix's `confirm()` only refuses to act on
   already-CONFIRMED payments, so it happily flips the FAILED payment to
   CONFIRMED and the order becomes paid. → matches "message says failed, but paid
   a few seconds later".

2. **Callback arrives first**: the payment is already CONFIRMED when the return
   view calls `payment.fail()`. pretix's race-condition guard in `fail()`
   detects this, refuses, and logs
   `Failed payment ... but ignored due to likely race condition`. → matches the
   log line. (The subsequent `status != 1` check also wrongly raises "No
   successful payment was detected" because a verified transaction has status
   `2`, but `ReturnView` suppresses the message for confirmed payments, so this
   ordering is merely noisy.)

### Secondary flaw

Przelewy24's `GET /transaction/by/sessionId/{sessionId}` returns
`status`: `0` = no payment, `1` = pre-payment (paid, awaiting verification),
`2` = verified, `3` = refunded. Treating only `1` as success means a
transaction that has already been verified (status `2`) is also reported as
"no successful payment".

## Fix

All changes are in `pretix_przelewy24/payment.py`, method `_check_transaction`.
No changes to `views.py` — `ReturnView` and `CallbackView` already lock the
payment row with `select_for_update(of=OF_SELF)` inside `transaction.atomic()`,
so the two requests serialize correctly once `_check_transaction` stops failing
the payment.

### New `_check_transaction` behavior

1. **Persist the fetched data without failing the payment.** Replace the
   `payment.fail(info=...)` call with a plain merge-and-save:

   ```python
   data = r.json()["data"]
   payment.info_data = {
       **payment.info_data,
       **data,
   }
   payment.save(update_fields=["info"])
   ```

2. **Branch on status instead of failing unconditionally:**

   - `status in (1, 2)` (paid / verified):
     - If `payment.state` is already `PAYMENT_STATE_CONFIRMED`, do nothing —
       the callback won the race, the customer just gets redirected to the paid
       order page.
     - Otherwise call `self._verify_transaction(payment)`. The by-sessionId
       response includes `orderId`, which step 1 just merged into `info_data`,
       satisfying `_verify_transaction`'s precondition. This performs the
       `transaction/verify` PUT and `payment.confirm()` — so when the return
       view wins the race, the customer immediately sees the order as paid
       instead of waiting for the callback. (`_verify_transaction` itself calls
       `payment.fail()` only when the verify API explicitly answers
       non-success, which is a genuine failure.)
   - any other status (`0` / `3`): raise
     `PaymentException(_("No successful payment was detected."))` **without**
     calling `payment.fail()`. The payment stays in its pending/created state,
     so a late callback can still confirm it. `ReturnView` already turns the
     exception into a user-facing message and suppresses it for confirmed
     payments (`views.py:74-76`).

3. **Keep the existing network-error handling unchanged** — the
   `except (requests.RequestException, ValueError)` block that stores
   `check_error` / `check_response` in `info_data` and raises a
   PaymentException.

### Resulting method (sketch)

```python
def _check_transaction(self, payment: OrderPayment):
    r = None
    try:
        r = requests.get(
            urljoin(self.api_url, f"transaction/by/sessionId/{payment.full_id}"),
            auth=self._auth,
        )
        r.raise_for_status()
        data = r.json()["data"]

        payment.info_data = {
            **payment.info_data,
            **data,
        }
        payment.save(update_fields=["info"])

        # 1 = paid (awaiting verification), 2 = verified
        if data["status"] in (1, 2):
            if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
                self._verify_transaction(payment)
        else:
            # Do not fail the payment: the server-to-server callback may still
            # confirm it. Just inform the customer that no payment was found.
            raise PaymentException(_("No successful payment was detected."))
    except (requests.RequestException, ValueError) as e:
        payment.info_data = {
            **payment.info_data,
            "check_error": str(e),
            "check_response": r.text if r is not None else None,
        }
        payment.save(update_fields=["info"])
        raise PaymentException(
            _("We were unable to contact Przelewy24. Please try again later.")
        )
```

Note: `PaymentException` must not be swallowed by the generic `except` — it is
not a subclass of `RequestException`/`ValueError`, so the sketch above is safe
as written; keep it that way if the exception tuple is ever widened.

## What this fixes

- No more false "payment failed" messages or failure emails for successful
  payments (ordering 1).
- No more `Failed payment ... ignored due to likely race condition` log noise
  (ordering 2 — `fail()` is no longer called on confirmed payments).
- Customers whose return view wins the race see the order paid immediately,
  because the return view now completes verification itself.
- Genuinely abandoned payments are no longer prematurely moved to FAILED by a
  mere page visit; they expire naturally or fail via `_verify_transaction`.

## Verification

There is no runnable pretix instance here and `tests/` is a stub, so:

1. Style gates (CI enforces): `black --check .`, `isort -c .`, `flake8 .`
2. If a pretix dev environment is available, add tests mocking
   `requests.get` (by-sessionId) and `requests.put` (verify):
   - status `0` → payment remains pending, `PaymentException` raised, state not
     FAILED;
   - status `1` with verify answering `success` → payment CONFIRMED;
   - status `2` on an already-confirmed payment → no state change, no exception;
   - verify answering non-success → payment FAILED (via `_verify_transaction`).
3. Post-deploy sanity check in sandbox mode: complete a test payment; the
   browser return should land on the order page already marked paid, with no
   failure email and no race-condition log line.
