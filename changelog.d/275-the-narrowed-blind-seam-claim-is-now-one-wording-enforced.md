### The narrowed blind-seam claim is now one wording, enforced (issue #275 follow-up)

**#275 narrowed that claim in one of five places.** The four it missed were the
census module's docstring short form, this pin module's docstring, the CLAIM
line in `docs/TESTING.md`, and the assertion message in
`tests/test_the_censuses_have_an_invoker.py`. The last one is the one that
mattered: it is the text printed when the gate actually reds, so the retracted
overclaim was the only version a tripped reader was ever shown. A claim is not
retracted while the reader-facing copy still states it.

`double_census.BLIND_SEAM_CLAIM` is now the single runtime source of the
sentence and the message is built from it, so there is no second copy to drift.
No measurement, blind set or exit code changed.

**A new gate scans the repo for the two retracted wordings**, pinned as a STRING
TO BE ABSENT rather than as a list of known sites, because the defect was one
claim with five spellings and a site list would need keeping in step with
wherever the sixth lands. Two things it got wrong first, both worth recording
because both are shapes this repo has hit before:

- **The needles are assembled from fragments.** Spelled literally, the scan red
  on its own source: a scanner cannot hold the string it hunts in the text it
  reads. Same self-inclusion as a table that counted its own rows. Excluding the
  pin file by name was the alternative and it is worse, since that file is the
  likeliest place a sixth spelling gets pasted, so the control instead asserts
  the file IS scanned and that no needle is literal in it.
- **The reader-facing check was decorative and a mutation sweep caught it.** It
  asserted the source text contained the name of the shared constant, which can
  never fail, because that name is written in the file being read. A sweep leg
  that replaced the shared reference with a hand copy saying something else
  passed green. The check now CALLS the message builder and asserts on the
  returned text, which is the property actually worth holding.

The control's criterion is BY NAME, never a count. It first required at least
twenty scanned files, which is the same error as gating a branch on a check-run
ROW COUNT: the total drifts with how many docs the repo happens to carry and
says nothing about whether the scan reaches the files that carried the defect.
Naming those files answers that and proves the denominator is not empty; the
count stays as context in the failure text.

`CHANGELOG.md` is excluded by name, with that reason recorded at the exclusion:
it is append-only record that legitimately quotes the retracted wording while
describing the retraction, which the #264 entry above does. That exclusion is
load-bearing today, not a precaution, and a sweep leg that removes it reds.
