# Appointment scheduling assistant

A lightweight browser chat for finding providers, looking up existing appointments, and booking a visit against the supplied synthetic scheduling API. A terminal interface runs the same workflow. The language model interprets the request. One Python `Workflow` object owns the patient verification, the options shown, and the pending selection. The scheduling API supplies the booking and handoff receipts.

## Run

Use Python 3.8 or newer. The project uses the standard library, so there are no packages to install. From the project root, start the supplied mock in one terminal.

```bash
python3 mock-api/server.py
```

In a second terminal, set the temporary assignment key in your shell and start the assistant.

```bash
export OPENAI_API_KEY="your-temporary-key"
python3 web.py
```

Open [the appointment desk](http://127.0.0.1:8000) in your browser. Type a request or use a shortcut. Choosing an option displays the selected visit. **Confirm booking** sends an explicit yes for that visit. **Choose another time** clears the selection. **Start over** clears the conversation and patient identity.

For the terminal interface, run `python3 assistant.py` in place of `python3 web.py`.

The default model is `gpt-4o-mini`. Override it with `OPENAI_MODEL` if needed. The default scheduling URL is `http://127.0.0.1:4010`. For another local port, start the mock with `--port` and set `SCHEDULING_API_BASE` to the same loopback URL. Use Ctrl+C to stop either server or type `quit` in the CLI. Restart the mock to reset its in-memory bookings and handoffs. Start a new conversation after the reset to clear any previously offered slots.

## Try it

In one conversation, enter these lines in order.

```text
Which primary care providers are downtown?
I'd like to book a primary care visit downtown.
555-0101 1985-04-12
1
yes?
2
yes
Look up my booked appointment.
I have chest pain. Should I wait for an appointment?
```

On fresh fixtures, the first search returns Dr. Elena Brooks and Dr. Marcus King. Choosing `1` displays a confirmation prompt. `yes?` asks again. Choosing `2` replaces the pending selection. The standalone `yes` books the second visit. The lookup then includes the booking reference the API returned. The final message queues a medical handoff.

Use **Start over** or a new CLI session for each short demo.

| Scenario | Inputs | Expected result |
|---|---|---|
| No matching patient | Ask to look up existing appointments. Enter `555-9999 1990-01-01`, then `hand it off`. | The assistant offers a retry or a handoff. Accepting the offer queues one `identity_unclear` handoff without retrieving appointments. |
| Duplicate identity | Ask to look up an appointment. Enter `555-0130 1978-09-22`, then `70115`. | The assistant asks for ZIP before retrieving one patient's appointments. An unmatched ZIP queues a handoff. |
| Slot taken | On fresh fixtures, ask to book downtown primary care, enter the first demo's identity, choose `3` (8:30 a.m.), then `yes`. | The API returns 409. Choose `2` (11:00 a.m.) from the refreshed options and confirm again to book the replacement. |
| Empty search | Ask to book dermatology at Lakeside. Enter `555-0102 1992-06-03`, then `yes` when offered a handoff. | The assistant queues one `no_availability` handoff. |

## Decisions and failure behavior

| Owner | Responsibility |
|---|---|
| Language model | Interprets free-form requests, corrections, requests for a person, and slot descriptions. Its context includes the current step, search criteria, and numbered public options. It can propose a choice. |
| Python state object | Parses identifiers locally, verifies one record, retains only offered slots, and checks a standalone affirmative against the current pending selection. Changing the patient or search clears that selection. Bare identity, slot numbers, and consent take no model call. |
| Scheduling API | Supplies providers, patient matches, availability, and write receipts. Python checks the booking receipt against the verified patient and selected visit before announcing success. |

If a patient corrects their identifying details, Python discards the prior match, duplicate candidates, and pending slot. It requests any missing identifier and searches again before showing appointments or offering times. A standalone `yes` accepts the action currently offered. The booking confirmation and human handoff use distinct workflow states.

A 409 excludes that slot for the conversation, even if the next availability read still lists it. A lost or inconsistent booking receipt leaves the outcome unknown. The assistant sends one handoff asking the office to verify the booking before a retry. A handoff is reported as queued only after the API supplies its reference and queued status. If that receipt is unavailable, the patient is directed to contact the office. Subsequent turns preserve that outcome.

The API client uses loopback HTTP and sends no model credentials to the mock. Common numeric phone, date-of-birth and ZIP forms are masked in mixed requests. Identity-only replies stay local. The mock fixture format is a seven-digit phone and an ISO birth date. Both interfaces also accept the same seven digits without a hyphen and U.S. numeric dates such as `04/12/1985`. Use synthetic data throughout. Free text can contain names and other identifying details beyond the supported masking rules.

## Tests and traces

```bash
python3 -m unittest
python3 -m unittest -v test_scenarios test_web
```

Neither command needs an OpenAI key or a separately running mock. Scenario tests send HTTP requests through the supplied handler and fixtures. They check the stored appointments and handoffs as well as the conversation. The tests use an ephemeral loopback listener where available and a connected socket pair on restricted hosts. Model outputs are controlled in these tests. A live-key run exercises the actual classifier.

Both interfaces write JSON lines to owner-only `assistant.log` in the project directory when launched as shown above. Set `ASSISTANT_LOG` to choose a different path. To watch the trace while running the browser chat, open a separate terminal and run `tail -f assistant.log` after starting the assistant. Inspect `MODEL`, `GET`, `POST`, and `TURN` events for latency, workflow state, API status, error code, or handoff reason. Patient-specific routes are logged as templates. The mock server also masks the patient ID in its access log.

## Limits and next steps

The browser server keeps a workflow and the last 40 exchanges per browser session in memory. Refreshing restores the conversation. Tabs in the same browser session share it. Forms are bound to the turn that displayed them, so a stale confirmation cannot authorize a newer selection. Sessions expire after 30 minutes of inactivity. The CLI keeps one conversation per process. Verification relies on the assignment's phone, birth date, and optional ZIP. The mock has no idempotency keys. A production version would use a stable booking request ID and a reconciliation endpoint to resolve lost receipts.

To add reschedule or cancel, verify the patient, list their appointments, bind the chosen appointment and proposed change to state, then request fresh confirmation before calling a dedicated endpoint. For rescheduling, the scheduling service should reserve the replacement and release the original as one operation.

For rollout, start with provider lookup, then enable booking for a small cohort behind a separate flag. Track booking success, unresolved writes, handoff acceptance, model calls per completed booking, and model/API p95 latency. Local handling of structured replies and a 120-token output budget limit model cost. Disable booking independently if unresolved writes rise, and reconcile outstanding requests before re-enabling it.
