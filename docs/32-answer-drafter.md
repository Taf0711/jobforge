# AI answer drafts

Free-text application questions such as “Why N1?” are not guessed by the
autopilot. `jobforge/automation/drafter.py` proposes a short answer from the
job description and the verified candidate facts. It does **not** fill or submit
anything. A human must approve the draft before it enters the answer bank.

## Boundary

| Stage | What happens |
|---|---|
| Request | The user chooses a stored job and a free-text motivation question |
| Generation | The configured model receives the job description and verified fact pack |
| Checks | Sensitive questions, structure, citation IDs, numbers, and skills are checked deterministically |
| Draft | Saved to `data/answer_drafts.json` (`0600`) with status `draft` |
| Approval | The human edits or approves the text; the answer is written to `data/answers.json` |
| Fill | The ordinary `AnswerBank` path may now fill that question |
| Rejection | The draft stays out of the answer bank |

The drafter never writes the answer bank without an explicit approval request,
and the autopilot never calls the drafter on its own.

## Deterministic checks

- Only `motivation` free-text questions are eligible.
- Sensitive, legal-attestation, clearance, work-authorization, sponsorship, and
  salary questions are refused.
- The model must return `answer` and at least one `used_fact_ids` entry.
- Every cited ID must exist in the entry-scoped source pack.
- The answer must be non-empty, at most 500 characters, single-paragraph, and
  free of `<`, `>`, or control characters.
- Every number in the answer must appear in the cited fact text. Numbers that
  appear only in the employer JD are not treated as candidate evidence.
- Every detected skill must be present in the profile or the cited facts.
- A provider failure or malformed JSON fails closed; no deterministic answer is
  invented.

These are automated grounding checks, not a semantic proof. The human approval
step is required.

## UI and API

`/answers` now includes **AI answer drafts**:

- choose a job ID and question;
- generate a draft;
- edit the proposed text;
- approve it into the answer bank or reject it.

Read-only API: `GET /api/answers/drafts`.

Routes:

```text
POST /answers/draft
POST /answers/drafts/{id}/approve
POST /answers/drafts/{id}/reject
```

## Storage

`data/answer_drafts.json`:

```json
{
  "version": 1,
  "drafts": [
    {
      "id": "uuid",
      "question": "Why N1?",
      "category": "motivation",
      "job_id": 4136,
      "company": "N1",
      "title": "New Grad Software Engineer (Backend Rust)",
      "text": "50-90 word draft",
      "used_fact_ids": ["exp:0:b:0"],
      "basis": [{"id": "exp:0:b:0", "text": "verified fact"}],
      "model": "provider/model",
      "status": "draft",
      "created_at": "...",
      "approved_at": "",
      "approved_key": ""
    }
  ]
}
```

The file is private; only the newest 100 drafts are retained. Approved text is
also in `data/answers.json` under a canonical question key such as `why n1`.

## Privacy

Drafting sends the selected job description and verified profile facts to the
configured model provider. Do not use a hosted provider for facts you do not
want to leave the machine; use Ollama or LM Studio. Provider keys are never
written to the draft or answer files.

## Tests

- `tests/test_answer_drafter.py`: draft persistence, no answer-bank write before
  approval, approval path, rejection, sensitive refusal, uncited draft,
  unsupported number, unsupported skill, and non-mutating `read_profile`.
- `tests/test_answers_ui.py`: draft form rendering and redirects.
