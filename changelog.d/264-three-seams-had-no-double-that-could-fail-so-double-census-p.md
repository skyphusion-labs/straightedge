### Three seams had no double that could fail, so `double_census.py` can now be gated (issue #264)

`cancel`, `check_working` and `working` each had doubles and no double that
could raise, so the suite covered no failure path through any of them. The list
had been stable at three across #244, #248, #257 and #262: not drift, three
paths that were never covered. `double_census.py` exited 1 on `main` because of
it, which is what blocked gating it, and the only alternative was a baseline or
an allowlist, which is the #61 defect of a scanner that quietly shrinks its own
denominator.

**All three took answer 1 from `docs/TESTING.md`, make the double enter the
state, and they share that answer because they share one MECHANISM.** All three
are `self._result(self._call(op, ...))` in `Mt4Broker`, and `_call` is the
injected transport whose mailbox bridge raises `BridgeTimeout`. There was no
case for disclaiming a path that one real exception reaches through all three,
and no need to cite a live run for a failure reproducible offline. Each double
raises the REAL `BridgeTimeout` rather than a stand-in `RuntimeError`, because
the sweep's arm is typed (`except (RuntimeError, OSError, ValueError)`) and a
stand-in proves the handler journals without proving the real exception reaches
it.

**What differs is the CONSEQUENCE, which is why it is three tests and not one
parametrised over three method names.** Each was measured before the answer was
chosen, with a healthy-broker control in every case:

- **`working` is a SEND, and `inflight.begin()` has already run.** The ledger
  entry must stay OPEN and the next `/confirm` of the same staged order must be
  refused as COULD NOT MEASURE rather than filed as a rejection. The market path
  had this guarantee pinned since the send-ledger work; the pending path did
  not, although a working order is committed exposure the moment it rests
  (#107). `InFlightBroker`'s docstring already claimed to cover the raised
  shape, and that was true of `market` only.
- **`check_working` is a READ, one call EARLIER.** `begin()` has not run, so the
  ledger must stay EMPTY and nothing may be sent. That is the OPPOSITE correct
  answer to `working`, and the pair is what pins that where the exception comes
  from decides which answer is right. Leaving an entry open here would refuse
  the operator's next good attempt for a send that never happened.
- **`cancel` is a sweep step on the SAFETY path.** The order is counted a
  survivor, the report is incomplete, and the record says COULD NOT MEASURE
  instead of carrying a venue retcode it never received. `flatten` is reached by
  daily-loss and drawdown and `flatten_incomplete` is the only member of
  `telegram.ALWAYS_NOTIFY_EVENTS`, so a sweep reporting clean here was the worst
  available failure. Distinct from the existing `cancel_reject` case: a reject
  is a VERDICT, a raise is an absence of one.

`KNOWN_BLIND_SEAMS` is now empty, and the pin redded naming all three before it
was updated, which is what makes the doubles evidence rather than a claim.
**`double_census.py` exits 0 and is gateable**; it is already a gate in practice
because `tests/test_the_censuses_have_an_invoker.py` asserts on it.

**And the census's own claim was too strong, narrowed here and separately from
the finding.** It said a blind seam meant "the suite covers no failure path
through them". It measures test-file DOUBLES, by looking for a `raise` in a
method whose name matches a seam, so a path covered by making the REAL
implementation fail is invisible to it and reads as blind. `docs/TESTING.md`
prefers exactly that ("when the real thing can be made to fail cheaply, that
beats any double"), and #232's own repair pointed a real transport at a closed
loopback port and used no double at all. The wording now says "no DOUBLE here
covers" and tells the reader to check for a real-implementation test first. No
measurement, blind set or exit code changed, and that separation is deliberate:
a census edited to clear its own finding would be the control validating itself.
