# WhatsApp Chatbot Fixes and Deployment Guide

This README consolidates the complete fix documentation into one file. The sections are ordered from the foundational fixes to the latest cumulative shipping-catalog pipeline.

> **Deployment note:** Later sections are cumulative and may supersede earlier replacement or patch instructions. Use the deployment procedure that matches the build you are installing.

## Contents

1. [WhatsApp delayed-reply and shipping-rate fixes](#delayed-reply-shipping-rate)
2. [Public route-category lookup fix](#public-route-category-lookup)
3. [Pending-request resume and language-style fix](#authentication-resume-language-style)
4. [Customer Reply Guard Fix](#customer-reply-guard)
5. [Supervisor staff management + multilingual shipping quote fix](#staff-management-ksa-quote)
6. [Root multilingual shipping quotes and staff lifecycle management](#root-shipping-staff-lifecycle)
7. [Canonical multilingual shipping-catalog pipeline](#canonical-shipping-catalog-pipeline)

---

<a id="delayed-reply-shipping-rate"></a>
## 1. WhatsApp delayed-reply and shipping-rate fixes

_Merged from `README_FIXES.md`._

### What changed

#### 1. Delayed or apparently random bot messages

The patched webhook now applies four protections:

1. **One sender at a time.** Different WhatsApp message IDs from the same customer are serialized with a Redis processing lock, so an older slow AI request cannot finish after a newer request and send out of order.
2. **Bounded AI work.** An OpenAI request has a provider timeout and the entire AI turn has a hard deadline. A canceled old request cannot continue and send much later.
3. **At-most-once outbound reply guard.** The primary bot response is reserved by inbound WhatsApp message ID before calling Meta. Webhook retries cannot send a second reply after a timeout or uncertain delivery result.
4. **Stale/out-of-order rejection.** Messages older than the configured age window, or older than the last completed customer timestamp, are acknowledged but are not sent to AI.

The patch also fixes an important retry bug: some handoff-failure paths sent a WhatsApp fallback and then returned HTTP 500. That told Meta the webhook failed and invited a later redelivery even though a customer-facing message had already been sent. Those paths now return success after notifying the customer.

#### 2. Guest shipping-rate inquiries

Shipping rates remain public; authentication is not required. The active tool dispatcher already excludes `get_shipping_price` from the protected-tool set.

The new rate lookup:

- matches route, goods type, and broad method using multilingual aliases;
- handles `Air` / `par avion` / `bl jaw` / `شحن جوي` against a database label such as `Air (Daily)`;
- handles `Lebanon` / `Liban` / `Lebnen` / `لبنان` and UAE/Dubai variants;
- parses explicit per-kilogram rate text;
- multiplies each explicit rate by the supplied weight using `Decimal` arithmetic;
- returns calculated route options even when one requested filter does not exactly match, instead of incorrectly reporting that no route rate exists.

For this row:

```text
Pickup: $12.30/kg, Delivery: $12.80/kg
```

and `20 kg`, the authoritative calculated values are:

```text
Pickup:  $246.00
Delivery: $256.00
Transit:  2-3 business days
```

### Files to replace

Copy the patched `app/` files over the existing application package. The changed files are:

```text
app/ai_config.py
app/ai_tools.py
app/config.py
app/conversation_store.py
app/main.py
app/openai_service.py
app/shipment_client.py
app/whatsapp_client.py
```

No new Python dependency was introduced.

### Optional `.env` values

The code includes these defaults, but adding them explicitly makes production behavior clear:

```dotenv
SHIPPING_DB_SCHEMA=shipping_db
OPENAI_REQUEST_TIMEOUT_SECONDS=45
OPENAI_MAX_RETRIES=1
OPENAI_TURN_TIMEOUT_SECONDS=90
WHATSAPP_MAX_INBOUND_AGE_SECONDS=900
CONVERSATION_PROCESSING_LOCK_TTL_SECONDS=180
OUTBOUND_REPLY_DEDUPE_TTL_SECONDS=604800
```

`WHATSAPP_MAX_INBOUND_AGE_SECONDS=900` ignores uniquely delivered customer messages that arrive more than 15 minutes late. Set it to `0` only when deliberately accepting arbitrarily old inbound messages.

### Verify the database permission used by the bot

phpMyAdmin access does not prove that the application account `whatsapp_bot` can read the second schema. Test with the same host, port, username, and password used by the service:

```bash
mysql -h 127.0.0.1 -P 3307 -u whatsapp_bot -p \
  -e "SELECT id, origin, destination, shipping_method, goods_type, price, transit_time FROM shipping_db.shipping_rates WHERE origin='UAE' AND destination='Lebanon' AND goods_type='Cosmetics';"
```

When that returns an access-denied error, inspect the actual MariaDB account host as an administrator:

```sql
SELECT User, Host FROM mysql.user WHERE User = 'whatsapp_bot';
```

Then grant the matching account read access, replacing `<Host>` with the returned host value:

```sql
GRANT SELECT ON shipping_db.shipping_rates TO 'whatsapp_bot'@'<Host>';
FLUSH PRIVILEGES;
```

The patched lookup reads only `shipping_db.shipping_rates`; it no longer requires a join to `shipping_db.destinations` for the shown UAE-to-Lebanon row.

### Deploy on the VPS

From the project directory used by systemd:

```bash
cd /root/whatsapp_chatbot_latest
cp -a app "app.backup.$(date +%Y%m%d-%H%M%S)"
# Copy the patched app/*.py files into /root/whatsapp_chatbot_latest/app/

source venv/bin/activate
python -m compileall -q app
python verify_shipping_rate.py
REDIS_URL='' PYTHONPATH=. pytest -q tests
sudo systemctl restart shipment-bot
sudo journalctl -u shipment-bot -n 150 --no-pager
```

Do not set `REDIS_URL=''` in production. It is used only in the included isolated guard tests. Production should continue using Redis so locks and idempotency work across Uvicorn workers and process restarts.

Useful production log phrases after this patch include:

```text
Shipping-rate tool result: found=True matched=True
Outbound reply suppressed by idempotency guard
Stale/out-of-order inbound WhatsApp message ignored
Conversation is already processing another message
AI turn timed out
```

### Acceptance checks

Send this as a logged-out/guest user:

```text
shu se3er she7nit cosmetics mn uae aa lebnen bl jaw? 20kg
```

A correct Arabizi response should preserve the customer's style and include both exact totals when pickup/delivery was not specified, for example:

```text
La 20 kg cosmetics men UAE 3a Lebnen bel jaw (Air Daily):
Pickup $246.00, Delivery $256.00. L transit time 2-3 business days.
```

Then test rapid consecutive messages from the same number. The second webhook may briefly receive `conversation_busy` and be retried by Meta, but replies must remain ordered and only one primary reply may be sent per inbound WhatsApp message ID.

---

<a id="public-route-category-lookup"></a>
## 2. Public route-category lookup fix

_Merged from `README_ROUTE_OPTIONS_FIX.md`._

### Problem fixed

The customer question:

```text
طيب شو فيني اشحن من اميركا على لبنان
```

asks for the shipping categories available on a route. The existing public tools were
focused on price calculations and required a weight, so the model had no authoritative
tool for this question and could incorrectly say that the information was unavailable
or offer a human handoff.

The previous country alias map also covered UAE and Lebanon but not USA variants such
as `اميركا`, `أمريكا`, `America`, `Amérique`, `États-Unis`, or `amerka`.

### What changed

#### New public tool

`get_route_shipping_options` now reads all matching rows from
`shipping_db.shipping_rates` and returns:

- rate-catalog goods categories;
- shipping methods and schedules;
- the exact listed price text;
- the exact transit time;
- a scope note explaining that the rows are service/rate categories, not a complete
  customs or dangerous-goods policy.

The tool does **not** require customer authentication or a package weight.

#### Expanded multilingual route matching

The backend now normalizes common English, French, Arabic, and Arabizi names for:

- USA;
- UAE;
- Saudi Arabia / KSA;
- China;
- Turkey;
- Syria;
- Lebanon.

#### Handoff behavior

The AI instructions now require the route-options tool for questions such as:

```text
what can I ship from the USA to Lebanon?
شو فيني اشحن من اميركا على لبنان؟
shu fine esh7an men amerka 3a lebnen?
qu'est-ce que je peux expédier des États-Unis au Liban ?
```

When matching rows exist, the assistant must answer from them and must not claim that
the information is unavailable or offer a human solely because the customer used the
words “allowed” or “what can I ship.”

### Expected answer for the current USA → Lebanon rows

A natural Lebanese-Arabic answer should be similar to:

```text
حسب جدول الشحن المتوفّر من أميركا للبنان، في شحن جوي كل ثلاثاء وجمعة لهالفئات:

- أغراض عامة: Pickup $23.00/kg أو Delivery $23.50/kg — 12-15 يوم عمل.
- مستحضرات تجميل: Pickup $25.00/kg أو Delivery $25.50/kg — 10-15 يوم عمل.
- إلكترونيات: Pickup $25.00/kg أو Delivery $25.50/kg — 12-15 يوم عمل.

هيدي فئات الشحن الموجودة بجدول الأسعار. إذا الغرض حساس أو فيه بطارية/سوائل، لازم نعرف شو هو بالتحديد لنتأكد من شروطه.
```

The exact wording may vary, but the categories, prices, schedule, and transit times must
come from the tool output.

### Files changed

```text
app/ai_config.py
app/ai_tools.py
app/shipment_client.py
```

Additional validation files:

```text
tests/test_route_options_tool.py
verify_route_options.py
```

### Deploy

From the project directory used by `shipment-bot`:

```bash
cd /root/whatsapp_chatbot_latest
cp -a app "app.backup.$(date +%Y%m%d-%H%M%S)"

# Copy these three patched files into app/:
#   ai_config.py
#   ai_tools.py
#   shipment_client.py

source venv/bin/activate
python -m compileall -q app
python verify_route_options.py
sudo systemctl restart shipment-bot
sudo journalctl -u shipment-bot -n 150 --no-pager
```

A successful smoke test ends with:

```text
OK: public USA -> Lebanon route options are available.
```

Useful runtime log line:

```text
Shipping-options tool result: found=True matched=True category_count=3
```

### Database permission

The application account must be able to read the table:

```bash
mysql -h 127.0.0.1 -P 3307 -u whatsapp_bot -p \
  -e "SELECT origin, destination, shipping_method, goods_type, price, transit_time
      FROM shipping_db.shipping_rates
      WHERE origin='USA' AND destination='Lebanon';"
```

If access is denied, grant `SELECT` to the exact MariaDB account/host used by the bot.

---

<a id="authentication-resume-language-style"></a>
## 3. Pending-request resume and language-style fix

_Merged from `README_AUTH_RESUME_STYLE_FIX.md`._

This cumulative build includes the earlier delayed-message, shipping-rate, and route-option fixes, plus the authentication-resume and communication-style corrections described here.

### Symptoms fixed

1. A guest asks to see/check orders.
2. The chatbot asks for User ID, then password.
3. Login succeeds, but the chatbot only confirms login and does not execute the original order request.
4. Arabizi input receives Arabic-script, Arabizi, and English mixed together.

Example affected request:

```text
bade tshefle order
```

### Root causes

- `pending_request` was saved only when the model returned the explicit `auth_required` flag. If the model independently asked for a User ID, the application activated authentication state but did not retain the interrupted request.
- The post-login model turn had the redacted password as the most recent user turn. The pending request was only described in system context, allowing a login-only acknowledgement instead of a protected tool call.
- The language detector required multiple known Arabizi tokens. Short phrases such as `bade tshefle order` and `shefle el order` were classified as `mixed`.
- The first authentication prompt was model-generated, so even a correctly detected style could be ignored.

### Behavior after the fix

For an unauthenticated customer writing Arabizi:

```text
Customer: bade tshefle order
Bot: B3atle l user ID taba3ak.
Customer: 10002
Bot: Tamem, b3atle l password la nkammel.
Customer: ********
Bot: [immediately returns the authenticated customer's order/shipment result in Arabizi]
```

There is no separate login-only stopping point when an interrupted request exists.

### Implementation

- Any credential prompt produced by the model is intercepted by the backend.
- The original non-secret request is always stored before requesting credentials.
- If the original AI turn reached a protected tool, its exact safe tool name and arguments are stored in Redis conversation state.
- After backend credential verification, that exact protected action is replayed once with the verified `users.id` injected by backend code.
- The authoritative result is passed ephemerally to the model only for customer-friendly wording; tools are disabled during that formatting step.
- If no blocked tool was captured, the pending request is re-presented as the active user turn. A login-only response is rejected and retried unless a protected customer tool actually executes.
- User ID and password prompts are deterministic language templates instead of model-generated text.
- Passwords remain outside conversation JSON and are never sent to OpenAI.
- Arabizi detection now recognizes short Lebanese phrases including `bade tshefle order` and `shefle el order` while keeping ordinary English such as `check my order` classified as English.
- For `communication_style=leb_arabizi`, the model receives an explicit Latin-script-only output rule.

### Files changed

Deploy these files together:

```text
app/ai_config.py
app/conversation_store.py
app/language.py
app/main.py
app/openai_service.py
app/support_models.py
```

No database migration is required.

### Deployment: replacement bundle

From the project directory on the VPS:

```bash
cd /root/whatsapp_chatbot_latest
cp -a app "app.backup.$(date +%Y%m%d-%H%M%S)"

rm -rf /tmp/auth-resume-style-fix
mkdir -p /tmp/auth-resume-style-fix
unzip /path/to/auth_resume_style_replacement_files.zip \
  -d /tmp/auth-resume-style-fix

cp /tmp/auth-resume-style-fix/auth_resume_style_replacement_files/app/ai_config.py app/
cp /tmp/auth-resume-style-fix/auth_resume_style_replacement_files/app/conversation_store.py app/
cp /tmp/auth-resume-style-fix/auth_resume_style_replacement_files/app/language.py app/
cp /tmp/auth-resume-style-fix/auth_resume_style_replacement_files/app/main.py app/
cp /tmp/auth-resume-style-fix/auth_resume_style_replacement_files/app/openai_service.py app/
cp /tmp/auth-resume-style-fix/auth_resume_style_replacement_files/app/support_models.py app/
cp /tmp/auth-resume-style-fix/auth_resume_style_replacement_files/verify_auth_resume_style.py .
```

Then validate and restart:

```bash
source venv/bin/activate
python -m compileall -q app
python verify_auth_resume_style.py
sudo systemctl restart shipment-bot
sudo journalctl -u shipment-bot -n 150 --no-pager
```

### Deployment: incremental patch

The incremental patch is based on the previous cumulative `chatbot_route_options_fix.zip` build:

```bash
cd /root/whatsapp_chatbot_latest
cp -a app "app.backup.$(date +%Y%m%d-%H%M%S)"
git apply --check /path/to/auth_resume_style_incremental.patch
git apply /path/to/auth_resume_style_incremental.patch
source venv/bin/activate
python -m compileall -q app
python verify_auth_resume_style.py
sudo systemctl restart shipment-bot
```

If `git apply --check` fails because the deployed files are older or locally modified, use the replacement bundle or the full cumulative ZIP instead.

### Clean test session

The conversation may still be authenticated from an earlier test. Before reproducing the two-step login flow, either send a logout message to the bot or delete only that test conversation's Redis key:

```bash
redis-cli DEL "conversation:<CUSTOMER_WHATSAPP_NUMBER>"
```

Use the number in the same format Meta supplies to the webhook. Do not flush the entire Redis database on a production system.

### WhatsApp acceptance test

Send exactly:

```text
bade tshefle order
```

Expected first response:

```text
B3atle l user ID taba3ak.
```

Send the User ID. Expected second response:

```text
Tamem, b3atle l password la nkammel.
```

After sending the password, the next response must contain the order/shipment result. It must not stop at only:

```text
Tamem, sar l login.
```

Also test:

```text
shefle el order
```

Both requests should remain Latin-script Lebanese Arabizi throughout the authentication flow and the order response.

### Useful logs

A successful initial gate includes:

```text
Authentication gate activated: ... pending_request=True style=leb_arabizi
```

After login, the protected request should produce a tool dispatch such as:

```text
AI tool dispatch: tool=get_customer_shipments authenticated=True
```

If the model tries to send only a login acknowledgement instead of using a protected tool, the application logs:

```text
Pending request resume produced no protected tool call; retrying
```

### Validation performed

```bash
python -m compileall -q app tests
REDIS_URL='' python -m pytest -q
```

Result: 17 tests passed. Tests cover the existing delayed-message/rate/route protections plus:

- `bade tshefle order` -> `leb_arabizi`
- `shefle el order` -> `leb_arabizi`
- ordinary `check my order` -> English
- pending request retention even when the model omits `auth_required`
- deterministic Arabizi User ID prompt with no Arabic-script characters
- exact backend replay of `get_customer_shipments` after login
- the pending request being the last ephemeral user turn after password redaction

---

<a id="customer-reply-guard"></a>
## 4. Customer Reply Guard Fix

_Merged from `README_CUSTOMER_REPLY_GUARD_FIX.md`._

This incremental fix is based on the previous `chatbot_auth_resume_style_fix` build.
It prevents model drafting/self-correction text from being persisted or sent to a
WhatsApp customer.

### Incident addressed

The customer received text similar to:

```text
L2et 10 orders ... Wait Arabic forbidden! Need all Latin ...
```

That text was not a database value. It was model-generated visible commentary that the
application treated as a completed response.

### Root cause

The application used the Responses API convenience `output_text` value as the final
reply, appended it to Redis conversation history, and sent it to WhatsApp without a
customer-output validation step. In a reasoning/tool flow, output messages can carry a
`phase`; intermediate commentary and completed answers must not be treated as the same
customer-visible artifact.

The language prompt also said Arabizi should use Latin characters, but prompt
instructions alone are not an enforcement boundary.

### Changes

#### 1. Phase-aware response extraction

`app/openai_service.py` now inspects output message items directly:

- `phase="final_answer"` is preferred.
- `phase="commentary"` is discarded.
- unphased messages remain supported for older SDK/model responses.
- a commentary-only response is treated as empty, never as a final answer.

Persisted assistant messages are replayed to the Responses API with
`phase="final_answer"`.

#### 2. Deterministic output firewall

New file: `app/customer_reply_guard.py`.

Before an AI reply is persisted or sent, it rejects:

- known drafting/self-correction phrases;
- prompt, tool, and internal-state leakage markers;
- Arabic-script letters when the current style is `leb_arabizi`;
- empty, oversized, or control-character-bearing messages.

The exact reported incident is rejected for both `internal_note_leak` and
`arabic_script_in_arabizi`.

#### 3. One isolated repair attempt

When a reply fails validation, the application makes one no-tools formatter call with:

- reasoning effort `none`;
- strict Structured Output containing only `reply_text`;
- the current communication style;
- authoritative tool results, when available.

The repaired reply is validated again. If it still fails, the original text is never
stored or sent; a deterministic style-safe fallback is used instead.

#### 4. Final WhatsApp-boundary check

`app/main.py` validates the reply a second time immediately before the send path. This
protects against future callers, tests, or refactors that bypass the normal OpenAI
service guard.

#### 5. Post-login order lists no longer require a model renderer

When the pending protected operation is `get_customer_shipments`, the backend renders a
compact list directly from authoritative tool output. It includes order IDs, tracking
numbers, shipment IDs, and status where available. For Arabizi, the prose is Latin-only.

Example:

```text
L2et 2 men a7das l orders:
1) Order ID: ORD-77 | Tracking: TRK-77 | Shipment ID: SHP-77 | Status: IN_TRANSIT
2) Order ID: ORD-88 | Tracking: TRK-88 | Shipment ID: SHP-88 | Status: RECEIVED
B3atle l order ID aw l tracking taba3 l order li baddak shouf details taba3a.
```

### Files changed

```text
app/ai_config.py
app/customer_reply_guard.py        # new
app/main.py
app/openai_service.py
tests/test_auth_resume_style.py
tests/test_customer_reply_guard.py # new
verify_customer_reply_guard.py     # new
```

### Deploy replacement files

From the VPS project directory:

```bash
cd /root/whatsapp_chatbot_latest
cp -a app "app.backup.$(date +%Y%m%d-%H%M%S)"

rm -rf /tmp/customer-reply-guard
mkdir -p /tmp/customer-reply-guard
unzip /path/to/customer_reply_guard_replacement_files.zip \
  -d /tmp/customer-reply-guard

cp /tmp/customer-reply-guard/customer_reply_guard_replacement_files/app/ai_config.py app/
cp /tmp/customer-reply-guard/customer_reply_guard_replacement_files/app/customer_reply_guard.py app/
cp /tmp/customer-reply-guard/customer_reply_guard_replacement_files/app/main.py app/
cp /tmp/customer-reply-guard/customer_reply_guard_replacement_files/app/openai_service.py app/
cp /tmp/customer-reply-guard/customer_reply_guard_replacement_files/verify_customer_reply_guard.py .
```

Validate:

```bash
source venv/bin/activate
python -m compileall -q app
python verify_customer_reply_guard.py
pytest -q
```

Expected verifier output:

```text
OK: customer reply phase filtering, internal-note blocking, and Arabizi order rendering are configured.
```

Restart:

```bash
sudo systemctl restart shipment-bot
sudo journalctl -u shipment-bot -n 150 --no-pager
```

### Acceptance test

Use a clean test conversation, log out first if needed, then send:

```text
bade tshefle order
```

Complete the two-step login. After successful authentication, the bot should immediately
send the recent order list with identifiers. The reply must contain no Arabic-script
letters and none of these phrases:

```text
Wait Arabic forbidden
Need all Latin
Need no Arabic
Include identifiers
Maybe order IDs
```

Useful logs:

```text
AI customer reply rejected by output guard
AI customer reply repaired by output guard
Unsafe AI reply blocked at WhatsApp boundary
```

The logs contain only reason labels and hashed customer identifiers; they do not log the
rejected reply body.

---

<a id="staff-management-ksa-quote"></a>
## 5. Supervisor staff management + multilingual shipping quote fix

_Merged from `README_STAFF_SHIPPING_FIX.md`._

This build is cumulative on top of `chatbot_customer_reply_guard_fix` and keeps all
previous delayed-message, shipping-catalog, login-resume, style, and customer-output
protections.

### 1. Staff-account management in `/support`

Authenticated `supervisor` and `admin` staff now see a **Staff accounts** button in the
support dashboard. The modal can:

- list existing staff accounts using a public projection;
- create an `agent` account;
- create an `admin` account.

An `agent` cannot access either endpoint even if they call it directly.

#### API

- `GET /api/support/staff`
- `POST /api/support/staff`

POST body:

```json
{
  "name": "Agent Name",
  "email": "agent@example.com",
  "password": "minimum-12-characters",
  "role": "agent"
}
```

`role` is restricted to `agent` or `admin`. Creating another `supervisor` through the
web interface is intentionally not supported.

#### Security controls

- The browser button is hidden for agents, but authorization is enforced again on the
  server.
- Creation requires the existing authenticated session and CSRF token.
- Initial passwords must be at least 12 characters and at most 72 UTF-8 bytes.
- Passwords are bcrypt-hashed with cost 12 in a worker thread.
- Passwords and hashes are never returned by the API or written to application logs.
- Email addresses are normalized case-insensitively and duplicate accounts return HTTP
  409.
- `staff.email_normalized` should have a unique index to make duplicate prevention safe
  under concurrent requests.

Check the index:

```sql
SHOW INDEX FROM staff;
```

If no unique index exists, first verify there are no duplicates:

```sql
SELECT email_normalized, COUNT(*) AS account_count
FROM staff
GROUP BY email_normalized
HAVING COUNT(*) > 1;
```

After resolving any duplicates, add the index:

```sql
ALTER TABLE staff
ADD UNIQUE KEY uq_staff_email_normalized (email_normalized);
```

### 2. KSA shipping-rate follow-up

The reported conversation is now handled as a continuation of the existing quote:

```text
bade esh7an 20 kg mawed tejmil mn emarat 3a lebnen adesh betkallef
...
tyb w eza mn el su3udiye
```

The backend now recognizes Lebanese spellings including:

- `su3udiye`, `so3oudiye`, `sou3oudiye`, `sa3oudiye`, `saudiye` -> `KSA`
- `mawed tejmil`, `mawad tejmil`, `tejmil` -> `Cosmetics`
- `emarat` -> `UAE`
- `lebnen` -> `Lebanon`
- `bl jaw`, `bl barr`, `bl ba7er` -> air, land, sea when explicitly stated

A shipping method is accepted only when the **active customer message explicitly says
it**. A method such as `Air (Daily)` returned by the UAE database row is no longer
silently reused as a filter for the later KSA quote.

For the supplied KSA cosmetics row and 20 kg:

```text
Pickup:  20 × $5.25/kg = $105.00
Delivery: 20 × $5.75/kg = $115.00
Method: Land (Every Thursday)
Transit: 20-25 days from departure
```

The customer-facing quote is rendered directly from the authoritative tool result. The
model still interprets intent and selects the lookup, but it cannot alter the matched
row, arithmetic, or reply script during paraphrasing.

Expected Lebanese Arabizi answer:

```text
La 20 kg mawed tejmil mn l Su3oudiye 3a Lebnen, l service l available howwe bel barr (kel khamis):
- Pickup: $105.00 ($5.25/kg)
- Delivery: $115.00 ($5.75/kg)
L wa2et: 20-25 yom mn wa2et l departure.
```

The deterministic renderer supports English, French, formal Arabic, Lebanese Arabic,
Lebanese Arabizi, and the existing mixed style. Unknown goods categories are not
silently priced as general goods; the reply lists the available rate categories instead.

### Files changed

```text
app/ai_config.py
app/ai_tools.py
app/customer_reply_guard.py
app/language.py
app/openai_service.py
app/shipment_client.py
app/staff_management.py       # new
app/staff_repository.py
app/support_api.py
app/support_ui.py
```

### Deployment

Back up the current application:

```bash
cd /root/whatsapp_chatbot_latest
cp -a app "app.backup.$(date +%Y%m%d-%H%M%S)"
```

Extract the replacement bundle and copy all files together:

```bash
rm -rf /tmp/staff-shipping-fix
mkdir -p /tmp/staff-shipping-fix
unzip /path/to/staff_shipping_replacement_files.zip -d /tmp/staff-shipping-fix

cp /tmp/staff-shipping-fix/staff_shipping_replacement_files/app/*.py app/
cp /tmp/staff-shipping-fix/staff_shipping_replacement_files/verify_staff_shipping_fix.py .
```

Validate and restart:

```bash
source venv/bin/activate
python -m compileall -q app
python verify_staff_shipping_fix.py
sudo systemctl restart shipment-bot
sudo journalctl -u shipment-bot -n 150 --no-pager
```

Expected verifier output:

```text
OK: supervisor staff management schema and KSA Arabizi quote are ready.
```

No new environment variable is required. The application DB account still needs:

- normal access to the existing `staff` and `staff_sessions` tables in the application
  database;
- `SELECT` access to `shipping_db.shipping_rates`.

### Acceptance tests

#### Supervisor UI

1. Sign in to `/support` as a `supervisor`.
2. Confirm **Staff accounts** is visible.
3. Create one test `agent` and one test `admin` using unique email addresses.
4. Sign out and verify each account can sign in.
5. Sign in as an `agent` and confirm the button is hidden.
6. As that agent, calling `GET /api/support/staff` directly must return HTTP 403.

#### WhatsApp quote

Send:

```text
bade esh7an 20 kg mawed tejmil mn emarat 3a lebnen adesh betkallef
```

Then:

```text
tyb w eza mn el su3udiye
```

The second answer must immediately show the KSA land rate with `$105.00` pickup and
`$115.00` delivery, remain fully in Latin-script Lebanese Arabizi, and must not say that
there is no rate for air unless the customer explicitly asked for air in that active
message.

---

<a id="root-shipping-staff-lifecycle"></a>
## 6. Root multilingual shipping quotes and staff lifecycle management

_Merged from `README_ROOT_SHIPPING_STAFF_FIX.md`._

This cumulative build fixes the two-turn Turkey quote failure and expands the support
staff screen from create-only account provisioning into full account lifecycle
management.

### What caused the Turkey failure

The English rate row itself was valid:

- Origin: `Turkey`
- Destination: `Lebanon`
- Goods: `Normal (Clothes & Accessories)`
- Price: `$7.00/kg`

The failure happened before/around the lookup:

1. `terkiya` was not guaranteed to normalize to the English `Turkey` value.
2. The first turn had the route and goods category but no weight. The model asked for
   weight, while the backend did not own the incomplete quote slots.
3. On the next message (`50`), the route/category could be reconstructed incorrectly or
   lost completely.
4. A transport method mentioned in a prior database answer could also be copied into a
   later follow-up even when the customer had not requested that method.

The root fix is therefore not another prompt synonym. It moves quote state, matching,
and arithmetic into deterministic application code.

### Shipping quote architecture

`app/shipping_quote_service.py` runs before the generic AI path for recognized rate
requests.

It now:

- Resolves origin, destination, goods category, and explicit transport method against
  the live `shipping_db.shipping_rates` catalogue.
- Separates the customer language from database labels. The database can remain in
  English.
- Handles the supported English, French, Arabic, Lebanese Arabic, Lebanese Arabizi,
  and mixed styles.
- Includes exact aliases, token matching, conservative typo/transliteration matching,
  and live catalogue labels. For example, `terkiya`, `turkiya`, `Turkey`, `Turquie`,
  and `تركيا` resolve to the same route lookup.
- Recognizes `tyeb`, clothes, vêtements, ملابس, and تياب as the clothes category.
- Parses English and Arabic digits and weights expressed in kg, grams, pounds, or tons.
- Accepts a bare number only while the backend is explicitly waiting for quote weight.
- Persists incomplete and last-completed quote slots in Redis conversation state.
- Expires quote context after 30 minutes by default so an old quote cannot capture an
  unrelated numeric message later.
- Reuses prior weight/category for natural follow-ups such as `tyb w eza mn el
  su3udiye`.
- Never inherits air/sea/land merely because the previous database result displayed it;
  a method becomes a filter only when the customer explicitly requested it.
- Multiplies only parsed `/kg` rates using decimal arithmetic and deterministic currency
  rounding.
- Renders the authoritative result directly in the current customer style instead of
  asking the model to recalculate or paraphrase it.
- Falls through to the normal AI/tool path for unrelated or genuinely unrecognized
  conversation.

#### Reported Turkey conversation after the fix

Customer:

```text
bade esh7an tyeb mn terkiya 3a lebnen adesh betkallef
```

Bot:

```text
2adde wazna bel kg?
```

Customer:

```text
50
```

Bot:

```text
La 50 kg tyeb w accessories mn Turkey 3a Lebnen, l service l available howwe bel jaw (kel jem3a):
- Rate: $350.00 ($7.00/kg)
L wa2et: 2-3 iyem 3amal.
```

The total is calculated as `50 × $7.00 = $350.00` from the database row.

### Staff account management

Supervisors and administrators can now open **Staff accounts** in `/support` and:

- List active and inactive accounts.
- Create agent or admin accounts.
- Change an account name.
- Change its email/login credential.
- Reset its password.
- Change `agent` ↔ `admin` role.
- Deactivate or reactivate it.
- Delete it safely. Delete is implemented as deactivation plus session revocation so
  historical ticket/audit references remain valid.

New API operations:

```text
GET    /api/support/staff
POST   /api/support/staff
PATCH  /api/support/staff/{staff_id}
DELETE /api/support/staff/{staff_id}
```

All write operations continue to require the existing authenticated support session and
CSRF token.

#### Session and account safety

Changing email, password, role, or activation state revokes all active sessions for the
target account. A demoted or disabled user therefore cannot continue using an old
session.

The dashboard also prevents:

- Deleting your own active account.
- Deactivating your own active account.
- Changing your own role from the session being used.
- Structural changes to bootstrap `supervisor` accounts through the web screen.

Supervisor bootstrap maintenance can still be performed through the existing CLI.

### Duplicate email protection

Email identity is now case-insensitive and trimmed. These are the same identity:

```text
test@gmail.com
Test@Gmail.com
" test@gmail.com "
```

Protection exists at three levels:

1. The service checks for an existing canonical email before hashing a password.
2. The repository repeats the check under a MariaDB advisory lock, including legacy
   rows whose `email_normalized` is null or stale.
3. An exact single-column unique index on `staff.email_normalized` is the permanent
   database invariant.

A duplicate create attempt returns HTTP 409 with structured detail similar to:

```json
{
  "code": "email_conflict",
  "message": "A staff account with that email already exists. It currently has role agent; edit that account to change its role to admin instead of creating a duplicate.",
  "existing_staff": {
    "id": "9",
    "email": "test@gmail.com",
    "role": "agent"
  },
  "requested_role": "admin",
  "suggested_action": "change_role"
}
```

The web application shows **Edit the existing account**, taking the supervisor directly
to that account instead of silently creating a second row.

### Required database cleanup and invariant

Deploy the application files first. The application-level check immediately blocks new
duplicates, even before the index is installed.

Then run from the project virtual environment:

```bash
cd /root/whatsapp_chatbot_latest
source venv/bin/activate
python ensure_staff_email_uniqueness.py
```

When no conflicts exist, it backfills `email_normalized` and creates:

```sql
UNIQUE KEY uq_staff_email_normalized (email_normalized)
```

When duplicate rows already exist, the script exits without changing the table and
prints their IDs. In `/support`:

1. Decide which row represents the real account for that email.
2. Change that retained row to the desired role.
3. Change the duplicate row to a unique archival email, then delete/deactivate it.
4. Run `python ensure_staff_email_uniqueness.py` again.

The script intentionally does not hard-delete database rows because staff IDs may be
referenced by support history.

A reference SQL version is included at:

```text
migrations/20260826_staff_email_uniqueness.sql
```

The Python script is preferred because it uses the same email normalization as the
application and checks for an exact single-column unique index rather than accepting a
weaker composite index.

### Deployment

#### Replacement bundle

From the VPS project directory:

```bash
cd /root/whatsapp_chatbot_latest
cp -a app "app.backup.$(date +%Y%m%d-%H%M%S)"

rm -rf /tmp/root-shipping-staff-fix
mkdir -p /tmp/root-shipping-staff-fix
unzip /path/to/root_shipping_staff_replacement_files.zip \
  -d /tmp/root-shipping-staff-fix

cp /tmp/root-shipping-staff-fix/root_shipping_staff_replacement_files/app/*.py app/
cp /tmp/root-shipping-staff-fix/root_shipping_staff_replacement_files/ensure_staff_email_uniqueness.py .
cp /tmp/root-shipping-staff-fix/root_shipping_staff_replacement_files/verify_root_shipping_staff_fix.py .
mkdir -p migrations
cp /tmp/root-shipping-staff-fix/root_shipping_staff_replacement_files/migrations/20260826_staff_email_uniqueness.sql migrations/
```

#### Environment setting

The setting has a default, so this is optional:

```dotenv
SHIPPING_QUOTE_CONTEXT_TTL_SECONDS=1800
```

#### Compile, migrate, verify, restart

```bash
source venv/bin/activate
python -m compileall -q app
python ensure_staff_email_uniqueness.py
python verify_root_shipping_staff_fix.py

sudo systemctl restart shipment-bot
sudo journalctl -u shipment-bot -n 200 --no-pager
```

Expected verifier output:

```text
OK: backend-owned multilingual quote context and full staff-account lifecycle are ready.
```

### Acceptance tests

#### Turkey two-turn quote

Send:

```text
bade esh7an tyeb mn terkiya 3a lebnen adesh betkallef
```

The bot must ask only for weight. Send:

```text
50
```

The response must contain:

```text
$350.00
$7.00/kg
```

and must stay entirely in Latin-script Lebanese Arabizi.

Repeat equivalent requests in English, French, Arabic script, and mixed wording. They
must all select the same English database row and preserve the current turn's style.

#### KSA contextual follow-up

After a 20 kg UAE cosmetics quote, send:

```text
tyb w eza mn el su3udiye
```

The reply must contain:

```text
Pickup: $105.00
Delivery: $115.00
```

and identify the available land service rather than carrying over UAE air service.

#### Staff lifecycle

1. Log in as a supervisor.
2. Create a test agent.
3. Edit its name and email.
4. Reset its password.
5. Change its role to admin and verify the old session is revoked.
6. Deactivate it, verify login fails, then reactivate it.
7. Delete it and verify it is inactive and all sessions are revoked.
8. Attempt to create an admin using an existing agent email with different casing.
   The operation must return a conflict and offer to edit the existing account.
9. Log in as a normal agent and verify staff management is unavailable and direct API
   calls return HTTP 403.

### Local validation performed

The cumulative source tree was validated with:

```text
48 automated tests passed
full Python compilation passed
support dashboard JavaScript syntax check passed
```

The tests include the exact reported Turkey conversation, English/French/Arabic/
Arabizi equivalents, Arabic digits, stale quote-context expiry, KSA method isolation,
duplicate-email conflict details, account updates, role/activation changes, session
revocation flags, protected-account rules, and dashboard/API lifecycle operations.

The final live MariaDB and WhatsApp acceptance test must still be run on the VPS.

---

<a id="canonical-shipping-catalog-pipeline"></a>
## 7. Canonical multilingual shipping-catalog pipeline

_Merged from `README_CANONICAL_SHIPPING_PIPELINE_FIX.md`._

This cumulative update replaces conversational phrase-by-phrase rate lookup with one
backend-owned pipeline for every request related to:

```text
shipping_db.destinations
shipping_db.shipping_rates
```

The database can remain in English. Customer wording may be English, French, Arabic,
Lebanese Arabic, Lebanese Arabizi, Arabizi, or a natural mixture.

### Why the reported sequence failed

The previous implementation had improved aliases and quote state, but two responsibilities
were still mixed together:

1. A conversational message was interpreted as both customer prose and database lookup
   input.
2. The last quote was reused to fill omitted fields without first establishing which
   values the current turn explicitly changed.

That caused three visible problems:

- `electronics men amerka` was interpreted correctly for pricing but the language detector
  treated the English product noun as a full switch to English.
- `ekseswar 3a souriya` did not reliably map `ekseswar` to the composite English row
  `Clothes & Accessories`.
- `tyb 3al iraq fine esh7an?` could miss the new destination and replay an older
  USA-to-Lebanon quote.

The last case is the most important correctness issue: an explicit current-turn value must
replace prior context even when the newly requested destination is unsupported.

### New architecture

```text
Customer message in any supported style
        |
        v
Fast deterministic extraction
        |
        +---- recognized aliases / digits / units / route markers
        |
        v
Narrow Structured Output normalizer
        |
        +---- explicit English fields only
        +---- no prices and no arithmetic
        +---- current turn only; previous context cannot be copied as explicit data
        |
        v
Canonical request object
        |
        +---- request_kind: price_quote | route_options | transit_time
        +---- origin
        +---- destination
        +---- goods_type
        +---- shipping_method, only when explicitly requested
        +---- weight_kg
        |
        v
Safe context merge
        |
        +---- explicit current-turn fields always win
        +---- deterministic recognition outranks model normalization
        +---- only omitted price-quote fields may come from fresh quote context
        +---- unsupported explicit destinations replace old routes
        |
        v
Live MariaDB catalogue validation
        |
        +---- shipping_db.destinations
        +---- shipping_db.shipping_rates
        |
        v
Decimal rate calculation
        |
        +---- /kg rates are multiplied in backend code
        +---- non-/kg or special-price rows are not invented or multiplied
        |
        v
Deterministic response renderer
        |
        +---- exact database facts and totals
        +---- same script/style as the current customer conversation
```

The model never receives database credentials, never writes SQL, never chooses a price,
and never performs the multiplication.

### Canonical normalization

`app/shipping_intent_normalizer.py` performs a narrow Structured Output request. It
returns only explicit fields stated in the active customer message.

Examples:

```text
electronics men amerka
```

becomes conceptually:

```json
{
  "request_kind": "price_quote",
  "origin": "USA",
  "goods_type": "Electronics",
  "explicit_origin": true,
  "explicit_goods_type": true
}
```

The missing destination and weight may then be filled only from fresh backend quote
context.

```text
tyb 3al iraq fine esh7an?
```

becomes conceptually:

```json
{
  "request_kind": "route_options",
  "destination": "Iraq",
  "explicit_destination": true
}
```

`Iraq` is intentionally preserved even though it is not in the live catalogue. This lets
the database layer return an authoritative no-match instead of silently retaining the old
`Lebanon` destination.

#### Trust ordering

The pipeline uses this precedence:

```text
1. Exact deterministic current-turn extraction
2. Structured semantic normalization for unresolved current-turn fields
3. Fresh backend quote context for omitted fields only
4. Live database rows
```

A model normalization can fill an unknown transliteration such as an unsupported country,
but it cannot overwrite a locally recognized `UAE`, `KSA`, `USA`, `Lebanon`, category,
method, or numeric weight.

When the semantic request fails or times out, deterministic matching remains available.
No old quote is used as a substitute for a newly named route.

### Composite goods categories

The matching layer now understands that one English database row may represent several
valid customer categories.

For example:

```text
Makeup & Electronics
```

matches either:

```text
makeup / cosmetics / مواد تجميل / mawed tejmil
electronics / إلكترونيات
```

Likewise, `ekseswar`, `accessories`, `accessoires`, and `إكسسوارات` can match:

```text
Clothes & Accessories
```

The customer-facing renderer also preserves composite meaning. It no longer collapses
`Makeup & Electronics` into cosmetics only.

### Context behavior for the reported conversation

The state transitions are now:

```text
25 kg cosmetics, UAE -> Lebanon
        |
        v
25 kg cosmetics, KSA -> Lebanon
        |
        v
electronics, KSA -> Lebanon
        |
        v
25 kg electronics, USA -> Lebanon
        |
        v
25 kg accessories, USA -> Syria
        |
        v
50 kg accessories, USA -> Syria
        |
        v
route availability to Iraq (new explicit destination; old quote discarded)
```

The Iraq response is based on the live destination/rate catalogue and must not contain the
prior `$1,750.00` quote.

With the currently supplied tables, the expected Arabizi response is similar to:

```text
Ma l2et destination aw rate msajjal 7aliyan 3a Iraq. L destinations l msajjalin halla2: Lebnen, Souria.
```

### Style handling

The language detector now uses the current turn plus the established conversation style.
Short product/route fragments do not automatically switch the entire reply to English.

For an established Lebanese Arabizi conversation, these remain Arabizi:

```text
electronics adesh?
electronics men amerka
ekseswar 3a souriya
50kg ekseswar 3a souriya
tyb 3al iraq fine esh7an?
```

A clear language switch such as:

```text
What is the price from America to Lebanon?
```

still switches the reply to English.

Exact amounts, units, database route labels, and transit facts remain independent of
presentation style.

### Files changed

```text
app/ai_config.py
app/config.py
app/customer_reply_guard.py
app/language.py
app/main.py
app/shipment_client.py
app/shipping_intent_normalizer.py       (new)
app/shipping_quote_service.py
tests/test_canonical_shipping_pipeline.py (new)
```

The cumulative build retains the previous complete staff-management implementation,
including create, edit credentials, change role, deactivate/reactivate, safe delete,
session revocation, and normalized unique-email protection.

### Environment settings

Defaults are already present. These may be set explicitly in `.env`:

```dotenv
SHIPPING_SEMANTIC_NORMALIZER_ENABLED=true

# Leave blank to use OPENAI_MODEL.
SHIPPING_SEMANTIC_NORMALIZER_MODEL=

# A failed normalizer falls back to deterministic parsing; keep this bounded.
SHIPPING_SEMANTIC_NORMALIZER_TIMEOUT_SECONDS=10

# Cache the small English rate/destination catalogue briefly.
SHIPPING_CATALOG_CACHE_SECONDS=60

# Existing settings.
SHIPPING_QUOTE_CONTEXT_TTL_SECONDS=1800
SHIPPING_DB_SCHEMA=shipping_db
```

Setting `SHIPPING_SEMANTIC_NORMALIZER_ENABLED=false` disables the model normalization
layer while preserving deterministic multilingual aliases, context safety, database
matching, arithmetic, and rendering.

### Database permissions

The application database account needs read access to both catalogue tables:

```sql
GRANT SELECT ON shipping_db.destinations
TO 'whatsapp_bot'@'<application-host>';

GRANT SELECT ON shipping_db.shipping_rates
TO 'whatsapp_bot'@'<application-host>';

FLUSH PRIVILEGES;
```

Use the actual MariaDB account host shown by:

```sql
SELECT User, Host
FROM mysql.user
WHERE User = 'whatsapp_bot';
```

### Deployment from the replacement bundle

```bash
cd /root/whatsapp_chatbot_latest

cp -a app "app.backup.$(date +%Y%m%d-%H%M%S)"

rm -rf /tmp/canonical-shipping-fix
mkdir -p /tmp/canonical-shipping-fix

unzip /path/to/canonical_shipping_pipeline_replacement_files.zip \
  -d /tmp/canonical-shipping-fix

cp \
  /tmp/canonical-shipping-fix/canonical_shipping_pipeline_replacement_files/app/*.py \
  app/

cp \
  /tmp/canonical-shipping-fix/canonical_shipping_pipeline_replacement_files/verify_canonical_shipping_pipeline.py \
  .

cp \
  /tmp/canonical-shipping-fix/canonical_shipping_pipeline_replacement_files/canonical_shipping.env.example \
  .
```

Compile and run the complete local test suite when the cumulative build/tests are present:

```bash
source venv/bin/activate
python -m compileall -q app
pytest -q
```

Run the live MariaDB verifier without making any OpenAI request:

```bash
python verify_canonical_shipping_pipeline.py
```

Expected output:

```text
OK: canonical English shipping fields, live catalogue lookup, Decimal pricing, context replacement, and style-safe rendering are ready.
```

An optional live Structured Output smoke test is available:

```bash
python verify_canonical_shipping_pipeline.py --semantic-smoke
```

That option uses `OPENAI_API_KEY` once and verifies that an unsupported French destination
is normalized to an explicit English `Qatar` field without copying the old route.

Restart:

```bash
sudo systemctl restart shipment-bot
sudo journalctl -u shipment-bot -n 200 --no-pager
```

### Clean acceptance test

Existing Redis state from the reported conversation can contain an older quote context.
Delete only the test conversation before retesting:

```bash
redis-cli DEL "conversation:<CUSTOMER_WHATSAPP_NUMBER>"
```

Do not flush the whole Redis database.

Then repeat the sequence:

```text
eza bade esh7an mawed tejmil mn el emarat 3a lebnen adesh betkallef
25
tyb eza bade esh7an mn el su3udiye 3a lebnen
tamem ysallemon
electronics adesh?
electronics men amerka
ekseswar 3a souriya
50kg ekseswar 3a souriya
tyb 3al iraq fine esh7an?
```

Acceptance criteria:

- The UAE and KSA totals come from their matching `/kg` rows.
- `tamem ysallemon` is treated as conversation, not as a quote continuation.
- Missing KSA electronics returns available KSA categories rather than inventing a rate.
- `electronics men amerka` returns USA electronics pricing and stays in Lebanese
  Arabizi.
- `ekseswar 3a souriya` matches `Clothes & Accessories`.
- `50kg` recalculates from the same authoritative row.
- Iraq returns an authoritative unsupported-destination response.
- The Iraq response contains no amount or route from the previous quote.
- Arabizi replies contain no Arabic-script prose.

Useful logs:

```text
Shipping catalogue request normalized: kind=price_quote ...
Shipping catalogue request normalized: kind=route_options ...
Shipping semantic normalizer unavailable: error_type=...
```

The logs record only field-presence booleans and operation type; they do not log the raw
customer message or model/provider response body.
