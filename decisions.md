# Decision log — Buy or Wait?

## D001 — Deterministic solver core, LLM for extraction only
Architecture: LLM/VLM parse messages and images into structured facts; a
deterministic engine does all arithmetic, dates, plan enumeration and ranking;
explanations are string templates built from engine outputs.

> Why did you refuse to let a model compute money? What would have gone wrong?
> (Mention: output CSV scored against hidden ground truth on exact amounts and
> dates; spec hands you the ranking as a 6-key sort; re-runs cost nothing.)

## D002 — The `flexibility` vocabulary bug
I wrote a validator that rejected any spending change on an event whose
`flexibility` wasn't `"flexible"`. That value doesn't exist in the data. The
real vocabulary is `fixed` / `reducible` / `stoppable` /
`reducible_or_stoppable`, so my check would have rejected every legitimate
spending change silently — the field would just always have come back `none`.
I found it by profiling the column's actual values rather than trusting my
reading of the spec. I replaced the string comparison with `can_stop` /
`can_reduce` predicates and verified them against the four events the goldens
actually name: `event_476` is `stoppable`, `event_1816` is
`reducible_or_stoppable` with `minimum_allowed_amount` 23.5, and golden's
`reduce_to:event_1816:23.50` sits exactly on that floor. Lesson I kept for the
rest of the build: profile the column before writing the rule.

## D003 — Income streams split by description
user_11 has a constant monthly "Base salary" (23,256,000 on the 15th, CV = 0)
and variable "Performance commission" payouts on the 24th. Treated as one
blended stream the median gap is ~11 days, the monthly test fails, and I
projected zero income for a user with a perfectly regular salary.

> What does this tell you about how to detect recurrence in general? And what
> did you later reject about your own first fix (`dominant_stream`, which took
> only the stream with the most occurrences)?

## D004 — Income CV gate 5% → 25%, and income projects most-recent
Measured: CV at 25%, 50% and ungated score identically — no monthly-cadence
stream in this dataset exceeds CV 25%. Gig payouts were already excluded by the
cadence test (weekly / twice-monthly), not by CV. The gate's only live effect
was rejecting genuine salaries that stepped once, e.g. user_06's payroll going
1441 → 1037.52 (CV ≈ 15%). Median APE 13.24% → 6.43%.
Separately: recurring income projects the most recent occurrence's amount, not
the mean.

> Why is a salary a level rather than a distribution? Which spec rule backs
> this? (§6.3, newer records from the same source.) Why do expenses keep a
> tunable aggregation while income doesn't?

## D005 — Hybrid intra-day ordering
Excluding a day's credits everywhere improved amount_safe_to_pay (median APE
13.24% → 8.43%) but broke 7 earliest-date rows, each by exactly +1 day, each
onto a salary date (requests 03, 04, 07, 17, 18, 22, 23). Final rule:
`floor_for_payment_on(i) = min(closing[i], tail_min[i])` — the paying day may
spend its own credits; every later day is checked at its conservative intra-day
minimum. 18/25 earliest exact, median APE 3.91%. Strictly better than either
pure setting.

> State the day-ordering as a sequence of events within a single day, and say
> why that sequence is the one a person actually experiences. This is your
> strongest architecture answer — write it in your own words, not mine.

## D006 — Rejected a correlational rule for the not_affordable explanation variant
First candidate rule: variant B fires when partial payment is the user's only
accepted method and the request allows partial payment. It fit all 7 golden
not_affordable rows. I rejected it and instead had the candidate generator
record *why* partial payment was unavailable, keying the variant on
`earliest_missing` / `earliest_after_deadline`. The recorded reasons turned out
to be four distinct codes across those 7 rows, and the correlational rule would
have mislabelled request_10 (fails on safe = 0) and request_25 (fails on method
acceptance).

> Why is a rule that scores 7/7 on visible samples still the wrong rule? What
> does this say about the hidden 250?

## D007 — Directive 6 (spending changes) is untested, not wrong
Implemented to spec. None of the three golden change rows exercises it under
the current forecast: requests 06 and 21 never trigger (our forecast already
thinks full payment is safe today), and request_11 triggers against a gap
1.32× too large (ours 790,133 vs golden 599,355), so no single eligible stream
closes it and ascending accumulation correctly takes three changes where golden
needs one.

> You chose not to tune the iteration order to make request_11 match. Say why.
> (What would that have been fitting to, and what happens when the forecast
> later changes?)

## D008 — amount_safe_to_pay has a data floor
A 20-combination grid over expense aggregation, request-day inclusion and
minimum-interval-occurrences moved nothing: exact count pinned at 4/25 in every
combination, and the signed errors are bidirectional — 12 rows over, 9 under.
Golden drawdowns are exact round numbers (request_08 = 452.00,
request_10 = 512,055) while the emitted history rows are noised.

> What does that combination of facts imply about the generator, and therefore
> about what's recoverable? Where did you decide to spend the remaining time
> instead, and why?

## D009 — Facts layer: injection containment is structural
No free-text field exists on either fact model (`extra="forbid"`), so message
content can only resolve to one of ten enumerated kinds, a number, or a date.
`overrides_from_facts` discards any fact with an unusable payload rather than
guessing. Cache keyed by message_id / image_id; token accounting from call #1.

> Why is a schema boundary a better injection defence than a prompt
> instruction? What can an attacker still do, and what can't they?