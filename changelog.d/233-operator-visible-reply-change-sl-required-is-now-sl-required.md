### OPERATOR-VISIBLE REPLY CHANGE: `sl required` is now `sl_required` (issue #233)

**A reply an operator reads has changed text.** The working-order modify path
answered `sl failed retcode=10016 sl required` and now answers
`sl failed retcode=10016 sl_required`. The paper adapter's send guard changes
the same way. This is not an internal tidy; if you grep your scrollback or
parse that reply, update it.

One condition, "no usable stop", had two renderings: the order and sizing path
reported the word `sl_required` and the modify path reported the prose
`sl required`. An operator who learned one could not find the other, and the
refusal table documented only the word.

- **The word now arrives on both channels and has ONE row**, in the
  `### Refusal reasons` table, naming both channels. Deliberately not
  duplicated into the stop-guard table: a second row is two places to drift.
- **The stop-guard gate gained a third bucket rather than an exclusion.** A
  comment it emits is now one of a guard word with a row here, prose in its
  pin, or a word whose row lives in the other table. The third case is paired
  with a test that the row really exists there, because an exclusion with no
  positive check is how a word stops being documented anywhere while two gates
  each believe the other covers it.
- **The new test asserts the PROPERTY, not the string.** It asks all three
  channels what they call the condition and requires the answers to be equal,
  derived from each rather than compared against a literal. Three hardcoded
  copies of the word would pass on three channels that had drifted apart
  again, which is how one condition acquired two spellings in the first place.
  A second test requires the shared value to be word-shaped, since converging
  all three on `sl required` would satisfy equality and defeat the point.
- The other two `invalid_stops` comments are untouched on purpose. They
  describe an ordering between three numbers, so there is no word to converge
  on and inventing one would be worse than the asymmetry.
