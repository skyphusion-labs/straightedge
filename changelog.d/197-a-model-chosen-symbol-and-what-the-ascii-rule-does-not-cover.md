### A model-chosen symbol, and what the ASCII rule does NOT cover (#197 residue)

One sentence, where a reader would look for it. The ASCII-before-transform rule
covers a MODEL-chosen name and deliberately is not a global invariant:
`desk.parse_kv` and `engine.market_signal` still case-fold what an operator
typed, by the same reasoning that exempts a human `/buy` from the advice
whitelist. An operator pasting `EURUſD` out of a model's prose receives an order
on the instrument the string LOOKS like, which is the one they chose. Recorded
so nobody reads the rule as wider than it is and nobody files it as a defect.
