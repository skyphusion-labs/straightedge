### A regression guard that could not go red

- **`test_no_staleness_is_reported_as_a_measured_offset` passed six ways under
  a mutation making `implied()` return a measured clock.** It asserted
  `"measured" not in out`, and that mutation sends `doctor` down the
  `if clock.measured:` branch, which prints "declared by the venue": the word
  never appeared, so the guard named for the #182 blocking finding stayed green
  while doctor confidently asserted a wrong offset. **It pinned the WORD, not
  the CLAIM.** It now asserts the clock's state and the branch taken, so
  rephrasing either print line cannot make it decorative again.
