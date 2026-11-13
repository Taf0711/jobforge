# Email-driven application tracker

JobForge can read a normal IMAP inbox and keep the application stage current
from employer mail. It is deterministic: no LLM decides that an email is an
interview, and no email can submit an application.

## What changes

```text
IMAP (read-only, BODY.PEEK[])
  → message dedupe by Message-ID
  → sender / company / role / ATS / job-URL match
  → conservative category
  → forward-only stage transition + event
  → tracker timeline and inbox review queue
```

The tracker stores the message, its match score and reason, and the before/after
stage. The application row gets `status_source='email'` when mail moved it.
Manual status changes set `status_source='manual'`.

## Matching

A message is linked only when the evidence agrees:

- company name in the sender or subject;
- company name in the message body;
- company domain in the sender;
- role title or role-token overlap in the subject/body;
- ATS sender domain (Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
  Workday, and common enterprise ATS domains);
- the stored job URL or apply-path in the body.

The threshold is 45/100. Two close candidates are left ambiguous rather than
guessed. A same-company email must name the role when the profile has multiple
applications at that company. Ambiguous and unknown mail waits in
**Inbox → Needs review**, where a human can link it to one application.

A link is not a status change. Automatic status changes require a match score
of at least 60/100 or a manual link.

## Categories and transitions

| Email signal | Category | Stage |
|---|---|---|
| application received / thank you for applying | `confirmation` | `submitted` |
| coding challenge / online assessment | `assessment` | `screening` |
| interview invitation / schedule a call | `interview` | `interview` |
| offer letter / pleased to offer | `offer` | `offer` |
| not moving forward / other candidates / unable to offer | `rejection` | `rejected` |
| verification code | `verification` | no stage change |

Transitions are forward-only. `offer`, `rejected`, `failed`, and `withdrawn` are
terminal. A late rejection cannot downgrade an offer, and a quoted rejection in
a reply does not reclassify the fresh message. A confirmation on a `draft` or
`prepared` row proves an application exists, so it sets `submitted_at` to the
message timestamp.

The first email that proves submission also sets `submitted_at` if it was
missing. That makes the quality gate refuse a duplicate application.

## Inbox setup

1. Open **Settings → Email sync**.
2. Use `imap.gmail.com:993` for Gmail.
3. For Gmail, enable 2-Step Verification and create a **Mail App Password**.
   Use the app password, not the normal account password.
4. Save. The password goes to the OS keychain, or an atomic `0600` file under
   `data/connections/email/` when no keychain backend is available. It is never
   written to `config.yml`, SQLite, exports, templates, logs, or tool output.
5. Run **Sync inbox** once and use **Test connection** if needed.

The dashboard starts a background sync when `enabled` and `auto_poll` are on and
the dashboard process is running. `jobforge email watch` provides the same loop
without the dashboard. The default interval is 15 minutes.

IMAP opens the folder read-only and fetches with `BODY.PEEK[]`, so polling does
not mark messages as read.

## Host-agent ingestion (no Gmail OAuth in JobForge)

A host agent that already has a Gmail, Outlook/Microsoft 365, or other mail
connector does not need JobForge to own a Google OAuth client. The host reads
the messages with its own connector and passes normalized fields to the
`jobforge_ingest_emails` MCP tool. JobForge never fetches the mailbox.

```json
{
  "provider": "gmail",
  "apply_status": true,
  "messages": [
    {
      "message_id": "18f...",
      "sender": "Recruiting <jobs@company.com>",
      "subject": "Interview invitation",
      "received_at": "2026-11-12T14:03:00Z",
      "body": "We would like to schedule a call for the Backend Engineer role..."
    }
  ]
}
```

- The host needs `email:write` on its JobForge connection:
  `jobforge integrations grant --connection conn_... --scopes email:write`.
- JobForge still classifies and matches the message. The host cannot assert a
  category, application id, or status.
- `message_id` is required and must be stable across syncs (the provider's
  message id or RFC Message-ID). JobForge prefixes it with `host:<provider>:`
  for dedupe.
- `apply_status: false` links and classifies without changing a stage.
- One batch accepts at most 50 messages and 1,000,000 input characters.
- The tool does not send, delete, mark read, or submit anything.

This is the seamless path for products whose host already owns the provider
OAuth connection. It keeps tokens with the host provider and keeps JobForge
local. The IMAP/app-password path remains available for hosts without a mail
connector.

## Commands

```bash
jobforge email status
jobforge email poll
jobforge email watch --interval 15
jobforge email watch --limit 50
```

`poll` returns `fetched`, `saved`, `matched`, `unmatched`, `ambiguous`,
`status_changes`, `categories`, `changes`, and `errors`.

## Safety boundaries

- No outbound email, no mailbox mutation, no message deletion.
- No arbitrary email provider API; IMAP over TLS only.
- JobForge does not send email bodies to a model. Classification is regex/rule based. A host connector may expose messages to the host agent under that provider's terms.
- No captcha, account creation, or submission is involved.
- No status change from an ambiguous match.
- The quality gate and human submission rules are unchanged.

## Schema

Schema version 8 adds:

- `applications.status_source`
- `emails.match_score`, `match_reason`, `match_source`
- `emails.status_before`, `status_after`, `processed_at`

Older databases migrate in place on `init_db`; no data rewrite is required.
