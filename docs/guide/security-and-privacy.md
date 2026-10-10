# Security and privacy

A QA tool handles login credentials, drives real accounts, and records screens. This page
covers what it protects and how — and the known gaps.

## Typed secrets are redacted (`executors/core/redact.py`)

What the agent types can be a secret. One policy decides, applied at the executor's output
boundary:

- text typed into a **secure field** (a password box) is a secret;
- text the **test itself does not contain** is a secret (an invented email, a one-time code);
- a **credential written into the test** ("password hunter2", "PIN: 4821", "token=abc") is a
  secret from step 1 — the agent may repeat it in its notes before typing it;
- text the test spells out, typed into an ordinary field (a search term), is **not** a secret —
  hiding it would also mangle screen text that evidence relies on.

Secrets are replaced by `[typed text]` in everything the run stores: step traces, step events,
checklist evidence, the verdict, agent-view files and log lines. Step events are stored as they
stream, so at the end of the run they are rewritten with the executor's final, fully scrubbed
steps — a value recognised as secret only late in the run (mentioned at step 2, typed into a
password field at step 9) does not survive in the stored record.

A credential written into the test counts when it looks like one (a digit or a symbol in it) or
when the test marks it as the value: after `:` or `=`, or in quotes ("password: sunshine",
"PIN 'tiger'"). A bare word after "password" does not ("the password field").

**Known limits:**
- The model sees secrets (it must, to type them), so they reach the LLM provider. Laminar traces
  record prompt text unless `TAMAM_LMNR_TRACE_CONTENT=false` (then spans keep timing and tokens only).
- Viewers watching a run *live* may see an early step before a value is known to be secret.
- Screenshots and video show whatever was on screen, including text typed into ordinary fields.

The planned fix for the first — test secrets as parameters filled in at the device, never shown
to the model — is on the roadmap.

## Graduated scripts never store secrets

A compiled script stores a typed credential as a **reference** to the test text, not its value
(`graduation/compiler.py: credential_values`). A run that typed something the test doesn't
contain (an OTP, an invented value) is not graduated at all.

## Risky actions are never repeated silently

Replay repeats every recorded tap on every run. A test whose run *taps* something that
deletes, pays, sends, signs out, unsubscribes, transfers, etc. is **not graduated** unless you
allow it
(`allow_risky_actions` on the project or the test — e.g. for a test environment where
repeating it is harmless). Detection (`graduation/compiler.py: risky_step_reasons`) looks at:

- the tapped button's label ("Delete account", "Pay", "Place order");
- its accessibility identifier, split into words (`trashButton`, `cart.buy_now`);
- a generic "OK / Yes / Confirm" — judged by the dialog's text ("Delete account?");
- a tap by position — judged by the screen it was made on.

Harmless uses don't block ("Send code", "Resend", "Reset filters"). The rule is re-checked
before **every** replay, so turning the setting off stops replay immediately, including for
scripts compiled earlier. A keyboard "return" or "Submit" is judged by the screen it is
pressed on, like a generic "OK". Detection is English-only; that is tracked as an open issue.

## App dialogs are the agent's to answer

iOS **system** permission prompts are accepted automatically and reported in the run's log.
The app's **own** dialogs ("Remove all items?", "Couldn't save") are left to the agent:
blind acceptance once confirmed a destructive dialog for the agent and hid an error before
anyone saw it. One exception: when an action *times out* because a dialog is blocking the
session, the harness accepts that dialog to recover and records it in the run's log. Opt in
to always accepting with `TAMAM_IOS_AUTO_ACCEPT_ALERTS` (not recommended).

## Access control and tenancy (`tamam/auth.py`)

- Authentication is isolated in one file (Clerk JWTs today): RS256 signatures verified against
  the provider's public keys, plus issuer, expiry and not-before checks, and the authorized
  party when the token carries one.
- **Secure by default:** with no issuer configured the API denies every request. A dev
  principal is injected only when `TAMAM_AUTH_DEV_MODE` is explicitly on (local use only).
- Projects, tests, builds and runs belong to an organisation; every lookup is scoped to the
  caller's organisation, and a build may only reference the project's own uploads.
- Locally stored artifacts are served through HMAC-signed URLs; the live event stream
  authenticates with a token.

## Stored artifacts (`tamam/storage.py`)

Screenshots, videos, traces and uploaded builds go to local disk or an S3-compatible bucket
(`TAMAM_STORAGE_BACKEND`). Private buckets are read through short-lived presigned URLs
(`TAMAM_SIGNED_URL_TTL_S`). Build uploads are size-capped (`TAMAM_MAX_UPLOAD_MB`).

## Cloud credentials

Device-farm credentials are passed explicitly to their clients, never read from the ambient
environment (see [Devices](devices.md)). All secrets are configuration (`TAMAM_` environment
variables), never in the repository.
