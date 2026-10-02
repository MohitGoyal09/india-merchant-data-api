# API reference (short)

The full machine-readable spec is [openapi.json](openapi.json). It is generated from the code with
`create_app().openapi()`. A running server also serves it at `/openapi.json` and an interactive
page at `/docs`.

This file covers the parts that are the same for every endpoint. The endpoint list is in the
[README](../README.md#endpoints).

## Success envelope

Every JSON success response has three keys.

```json
{
  "data": {},
  "meta": {"count": 1, "next_cursor": null, "degraded": false, "warnings": []},
  "provenance": [
    {
      "source": "rbi",
      "dataset": "holidays",
      "source_url": "https://www.rbi.org.in/Scripts/HolidayMatrixDisplay.aspx",
      "fetched_at": "2026-10-02T05:57:00.867501+00:00",
      "stale": false
    }
  ]
}
```

| Key | Meaning |
|---|---|
| `data` | The result. A list or an object, depending on the endpoint. |
| `meta.count` | Number of items in `data` (1 for an object). |
| `meta.next_cursor` | Opaque cursor for the next page, or `null` on the last page. |
| `meta.degraded` | `true` when a source used by this response has status `degraded` or `broken`. The API then serves the last good data. |
| `meta.warnings` | Plain-text notes, for example "fbil/fx_reference_rates is stale: latest data is 2026-09-24, expected 2026-10-01". |
| `provenance` | One entry for each source and dataset behind the answer: where it came from, when it was fetched, and whether it is `stale`. |

Money values are strings (`"95.9832"`), never floats.

## Error format

Every error has the same shape. No stack trace is ever returned.

```json
{
  "error": {
    "code": "CALENDAR_DATA_MISSING",
    "message": "No holiday data loaded for office 'mumbai', year 2027",
    "details": {
      "office": "mumbai",
      "year": 2027,
      "hint": "imda backfill --datasets holidays --from 2027-01-01"
    }
  },
  "request_id": "491ec3bc24a34f05ae273cc838c28cc5"
}
```

The `request_id` is also sent in the `X-Request-ID` response header. A client may send its own
`X-Request-ID` (letters, digits and `-`, up to 64 characters). The server then echoes it.

## Error codes

| HTTP | Code | When |
|---|---|---|
| 401 | `UNAUTHORIZED` | Admin endpoint called without a valid `Authorization: Bearer <token>`. |
| 404 | `OFFICE_NOT_FOUND` | The office slug is not one of the 34 RBI offices. |
| 404 | `RATE_NOT_FOUND` | No rate for that currency in the as-of look-back window (10 days by default). |
| 404 | `WEBHOOK_NOT_FOUND` | Unknown webhook subscription id. |
| 404 | `NOT_FOUND` | Unknown route. |
| 405 | `METHOD_NOT_ALLOWED` | Wrong HTTP method for the route. |
| 409 | `CALENDAR_DATA_MISSING` | Holiday data for that office and year is not loaded. `details.hint` has the command to load it. |
| 409 | `REFRESH_IN_PROGRESS` | A refresh is already running (`POST /v1/admin/refresh`). |
| 422 | `INVALID_REQUEST` | A parameter failed type checks: bad date format, unknown currency, number out of range. `details.errors` lists each field. |
| 422 | `VALIDATION_ERROR` | A rule failed after parsing: `from` after `to`, an amount with more than 2 decimal places, an invalid cursor, a page `limit` above the maximum. |
| 422 | `RANGE_TOO_LARGE` | The date range is longer than 3660 days. |
| 422 | `UNSAFE_WEBHOOK_URL` | The webhook URL is not `https`, or it points to a private, loopback or link-local address. |
| 500 | `INTERNAL_ERROR` | Unexpected server error. The details are in the server log under the same request id. |
| 503 | `ADMIN_DISABLED` | `IMDA_ADMIN_TOKEN` is not set, so admin endpoints are off. |

An upstream failure (RBI or FBIL down, page changed) never causes a 500. The API serves the last
good data and sets `meta.degraded` to `true`.

## Input rules

- Dates are `YYYY-MM-DD` only.
- `captured_at` is ISO 8601 with a UTC offset, for example `2026-03-27T11:00:00+05:30`. In a URL,
  write `+` as `%2B`.
- Currency is one of `USD`, `GBP`, `EUR`, `JPY`, `AED`, `IDR`. `INR` is allowed as the other side
  of `/v1/fx/convert`.
- `amount` is a positive decimal string with at most 2 decimal places.
- Office is a slug such as `mumbai`. `GET /v1/offices` lists all 34.

## Pagination

Only `GET /v1/fx/rates` is paged in JSON mode.

- `limit` sets the page size. The default and the maximum are 1000 (`IMDA_MAX_PAGE_SIZE`).
- Rows are ordered oldest first, one row per date.
- When more rows exist, `meta.next_cursor` has a value. Send it back as `cursor` with the same
  other parameters. When it is `null`, you have read the last page.

```bash
curl -s "http://127.0.0.1:8000/v1/fx/rates?currency=USD&from=2026-09-28&to=2026-09-30&limit=2"
# meta.next_cursor = "MjAyNi0wOS0yOQ"
curl -s "http://127.0.0.1:8000/v1/fx/rates?currency=USD&from=2026-09-28&to=2026-09-30&limit=2&cursor=MjAyNi0wOS0yOQ"
```

## CSV

`GET /v1/fx/rates` returns CSV when you send `format=csv` or `Accept: text/csv`. CSV returns the
whole range in one response (no cursor), up to 3660 days.

```text
date,currency,rate,unit,rate_per_unit,source,published_at
2026-09-28,USD,95.9681,1,95.9681,rbi,
2026-09-29,USD,96.0321,1,96.0321,rbi,
```

The response headers carry the provenance summary: `X-IMDA-Sources`, `X-IMDA-Degraded` and
`X-IMDA-Stale`.

`rate` is INR for `unit` units of the currency (for example 100 JPY). `rate_per_unit` is INR for
one unit.

## ICS calendar feed

`GET /v1/calendar/{office}.ics?year=2026` returns an RFC 5545 calendar (`text/calendar`) with one
all-day event for each RBI holiday of that office. Subscribe to it from any calendar app.

- `include_weekly_off=true` also adds the 2nd and 4th Saturdays.
- The response has `Cache-Control: public, max-age=3600`.
- A year that is not loaded returns `409 CALENDAR_DATA_MISSING`.

## Admin and webhooks

Admin endpoints (`/v1/webhooks*`, `/v1/admin/*`) need the header
`Authorization: Bearer <IMDA_ADMIN_TOKEN>`. If the token is not set on the server, they return
`503 ADMIN_DISABLED`.

Events:

| Event | Sent when |
|---|---|
| `fx.rates.published` | An ingest run stored new FX rates. |
| `holidays.updated` | An ingest run stored holiday rows. |
| `source.degraded` | A source changes from healthy to `degraded` or `broken`. |
| `source.recovered` | A source returns to `ok`. |

Create a subscription (the `secret` is shown once):

```bash
curl -s -X POST http://127.0.0.1:8000/v1/webhooks \
  -H "Authorization: Bearer $IMDA_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/hooks/imda", "events": ["source.degraded", "source.recovered"]}'
```

Deliveries are made by the worker (`imda worker`) or by `imda webhooks dispatch`. A failed
delivery is retried with waits of 30 s, 2 min and 10 min, up to 3 attempts. The delivery log is at
`GET /v1/webhooks/{id}/deliveries`.

### Verify a signature

Each delivery is a `POST` of a JSON body. The body has `id`, `event`, `created_at` and `payload`.
These headers come with it.

| Header | Value |
|---|---|
| `X-IMDA-Signature` | Hex HMAC-SHA256 of `"<timestamp>." + raw body`, keyed with the subscription secret |
| `X-IMDA-Timestamp` | Unix seconds when the delivery was signed |
| `X-IMDA-Event-Id` | Unique event id. Use it to drop duplicates. |
| `X-IMDA-Event` | Event name |

Check the RAW bytes before you parse the JSON. Reject a timestamp more than 300 seconds from your
clock.

```python
import hashlib, hmac, time

def verify(secret: str, raw_body: bytes, signature: str, timestamp: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

This is the same family as Razorpay's `X-Razorpay-Signature`, with a timestamp added to stop
replays. The code is in `src/imda/events/signing.py`.
