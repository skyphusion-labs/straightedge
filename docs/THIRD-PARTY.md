# Third-party software and the terms it carries

This file records every third-party component straightedge uses or touches,
which licence governs it, what that licence lets this repo do, and what it does
not. straightedge itself is MIT (`LICENSE`). Nothing here is legal advice; it is
the text we relied on, quoted, with the date it was read, so the next person can
re-check it instead of re-deriving it. Questions that need a lawyer or MetaQuotes
are named as such.

The bot has no runtime Python dependencies (`pyproject.toml`: `dependencies = []`).
Everything below is either an optional extra, the agent's npm tree, or a
proprietary program the operator or CI installs and this repo never ships.

## 1. MetaTrader 4 and 5 terminals, MetaEditor, and the MQL4 compiler

**What they are.** Proprietary programs by MetaQuotes Ltd (Limassol, Cyprus).
The operator installs the terminal on the trading host. CI installs MetaTrader 4
on a GitHub-hosted `windows-latest` runner to compile `mt4/Experts/Mt4RiskBot.mq4`
(`.github/workflows/mt4-compile.yml`). **This repository does not contain, cache,
mirror, or publish any MetaQuotes binary**, and must not.

**Governing text.** The MetaQuotes End-User License Agreement for MetaTrader 4,
https://www.metaquotes.net/licenses/terminal/mt4, read 2026-09-26 and re-read
2026-10-08 (the four load-bearing clauses below were byte-identical on both
dates; the MT5 EULA at `/licenses/terminal/mt5` is the same instrument apart from
4/5 and EX4/EX5). Clause 2.13 lets MetaQuotes change it by posting, so the
posted version governs, not this copy. A broker-branded terminal adds the
broker's own EULA on top; it can narrow this grant and never widens it.

The grant:

> 2.1. License. Subject to the terms and conditions of this Agreement,
> MetaQuotes hereby grants You a limited, worldwide, individual, non-exclusive,
> simple, non-sublicensable, non-assignable, revocable, non-transferable free of
> charge license to download, install and use the Product solely with the use of
> MetaQuotes' Software on Your personal computer, mobile phone or mobile
> computer, for organizing a trader's workstation and trading in the financial
> markets via Financial Institutions with which You enter into separate
> contractual relations.

MetaEditor and the compiler are parts of "the Product" (clause 1.1 lists
"MetaEditor is an editor which is used to develop program code in MetaQuotes
Language 4 (MQL4) and to further compile it" and "MetaQuotes Language Compiler is
a MetaQuotes Language 4 (MQL4) source code compiler designed to create executable
files .EX4"), so every restriction on the Product reaches `metaeditor.exe` and
`metalang.exe` on their own.

The restrictions this repo is built around:

> 2.2. No Granting of Rights to Third Parties. You shall not sell, assign, rent,
> lease, distribute, export, import, or otherwise grant rights to use the
> Product or any part thereof to a third party.

> 2.16. In the event that You wish to use the Product in a manner other than as
> expressly set out in this Agreement, such use is expressly prohibited unless
> and until MetaQuotes grants You a specific license in writing.

The separate grant for the IDE, which is the one a CI compile most plausibly
sits under:

> 2.27.1. License. Subject to Your compliance with these MQL4 IDE terms,
> MetaQuotes Ltd. grants You a limited, non-exclusive, non-assignable, non-sub
> licensable, non-transferable license with the right to use the MQL4 IDE for a
> Permitted Purpose. 2.27.2. For the purposes of the MQL4 IDE terms, "Permitted
> Purpose" shall mean the purpose of developing an executable program code
> having the .EX4 file format (hereinafter referred to as "Software
> application") to be applied only with the Product and MetaQuotes' Software.

Acceptance is by use, and an unattended install binds the business:

> You can accept this Agreement by clicking on the "Next", "Continue", "Accept"
> or similar button or by using the relevant link and/or using the Product.
> (preamble)

> 2.17.2. If You are an employee or director of a Business (Legal entity) and You
> use the Product on behalf of that Business, You acknowledge and agree that:
> (i) by accepting this Agreement and using the Product, You do so on behalf of
> the Business and with the express authorization of the Business that You and
> the Business shall be bound by this Agreement

Governing law is Cyprus, exclusive jurisdiction the Cyprus courts (clause 5.4).

**What follows, and how the repo encodes it** (decided on straightedge#87,
2026-09-26; the workflow header restates it):

| Act | Reading | Where enforced |
| --- | --- | --- |
| Publish MetaEditor binaries as a release asset, image, or file in this repo | Prohibited by name: "distribute ... to a third party" (2.2). | Nothing in the tree; no `upload-artifact` glob reaches the install directory. |
| Cache them in GitHub Actions cache, a private repo, or GHCR | Not expressly granted, so prohibited under 2.16; the licence is "non-sublicensable, non-assignable" (2.1), so we cannot grant GitHub the storage and copy rights its Terms of Service section D.4 require for uploaded content. On a public repo a fork PR can restore the `main` cache, so a cache is third-party retrievable regardless. | No `actions/cache` step in `mt4-compile.yml`. |
| Download `mt4setup.exe` from the MetaQuotes CDN each run and `/auto` install | The grant's own verbs ("download, install and use"); no clause forbids unattended installation. The only option that avoids every clause naming a prohibited act. | `MT4_SETUP_URL` is the MetaQuotes CDN, sha256-pinned, no fallback source. |
| Compile our own `.mq4` with it | The IDE's Permitted Purpose (2.27.2). | The workflow's only output is `Mt4RiskBot.ex4`. |

**Accepted and recorded risk, not resolved.** Two questions the text does not
settle and only counsel or MetaQuotes can:

1. Whether a GitHub-hosted runner is "Your personal computer" under 2.1, or
   whether 2.27's IDE grant stands on its own for a compile job with no machine
   limitation. Download-each-run carries this question exactly as vendoring
   would; it only avoids adding storage and distribution on top.
2. Whether a scripted fetch from `download.mql5.com` is "access the Website
   through any automated means" under the metatrader.com and mql5.com site
   terms (clause 3.7 of each, read 2026-09-26). The clause reads as
   anti-scraping and a download link is meant to be fetched, but the words are
   broad.

The EULA is silent on automated, headless, and server use. Silent is a finding,
not a permission. If straightedge grows beyond a single operator's own trading,
or if anyone wants certainty rather than a documented posture, the move is the
written question below, which clause 2.16 invites.

**The question for MetaQuotes, drafted for Conrad to send.** No licensing or
legal email is published: `metaquotes.net/en/company/contacts` lists postal
addresses only (read 2026-10-08), and `mql5.com/en/contact` offers a support
request form for paid services plus the forum. Channels, in order: the MQL5
support request form; a letter to MetaQuotes Ltd, 35 Dodekanisou str,
Germasogeia, 4043, Limassol, Cyprus; and `plugins@metaquotes.net`, which is the
author contact in the `MetaTrader5` PyPI package metadata and is a packaging
address, not a legal one. Text:

> Subject: Written permission request under EULA clause 2.16: MetaEditor on a
> hosted CI runner
>
> We maintain an open-source (MIT) Expert Advisor for MetaTrader 4,
> https://github.com/skyphusion-labs/straightedge. To make sure every change to
> the Expert still compiles, our continuous-integration job downloads
> `mt4setup.exe` from `download.mql5.com`, installs MetaTrader 4 unattended on
> a short-lived GitHub-hosted Windows virtual machine, compiles our own `.mq4`
> source with MetaEditor, keeps only the resulting `.ex4`, and discards the
> machine. We never store, cache, mirror, or redistribute any MetaQuotes binary.
>
> Three questions. (1) Is that use of MetaEditor and the MQL4 compiler on a
> hosted CI runner within the licence granted by clause 2.1 and clause 2.27 of
> the MetaTrader 4 End-User License Agreement, given that the runner is not a
> personal computer? (2) Does a scripted download of the installer from your
> CDN for that purpose fall within your site terms? (3) If either answer is no,
> would MetaQuotes grant written permission for it under clause 2.16, and on
> what terms? We are not asking to cache or redistribute the binaries.
>
> We would be grateful for a written reply we can keep with the project's
> licence records.

**Re-check method.** `curl -sS https://www.metaquotes.net/licenses/terminal/mt4`,
strip tags, and compare clauses 2.1, 2.2, 2.16, 2.17.2, 2.27.1 and 2.27.2 against
the quotations above. A changed clause reopens the table.

## 2. The `.ex4` this repo builds

`Mt4RiskBot.ex4` is our Software application under clause 2.27; MIT covers the
source. Two things the EULA says about it that the MIT licence cannot override:

- Compilation "will automatically incorporate the executable code protection
  software" (2.27.3.8), MetaQuotes' layer, which we do not own and may not
  remove, and which is why the binary is not byte-reproducible across runners.
- The Software application must be used "only with the Product and MetaQuotes'
  Software" (2.27.2), may not be distributed through sites that imitate
  MetaQuotes or use its marks in the URL (2.27.3.4), and "the services and
  products offered through Your Software application, shall be provided in
  compliance with the laws and regulations applicable in Your country
  (including without limitation those relating to the protection of privacy and
  processing of personal data or traffic data)" (2.27.3.5).

The CI artifact (`actions/upload-artifact`, repo default retention) is that
binary and nothing else.

## 3. Python packages (optional extras only)

| Package | Extra | Licence | Source and date |
| --- | --- | --- | --- |
| `MetaTrader5` (MetaQuotes Ltd) | `mt5-win` | MIT. `LICENSE.txt` inside the wheel: "Copyright 2000-2025 MetaQuotes Ltd. Permission is hereby granted, free of charge, to any person obtaining a copy of this software ..." (standard MIT text). | `pip download MetaTrader5 --no-deps --platform win_amd64 --only-binary=:all: --python-version 3.12`, wheel 5.0.6231, `METADATA` says `License: MIT`, read 2026-10-08. |
| `mt5-mac` | `mt5-mac` | MIT per PyPI metadata (`License :: OSI Approved :: MIT License`). | PyPI JSON, version 0.3.0, read 2026-10-08. |

The `MetaTrader5` package is MIT; the terminal it connects to is not. Installing
the package does not change anything in section 1. `mt5-mac` is a third-party
package by an individual author, not a MetaQuotes product; README already says
the official package is Windows-only.

## 4. The agent's npm dependencies (`agent/package.json`)

`agent/package-lock.json` is authoritative for the exact tree; this is the
direct dependencies' declared licence at the npm registry, read 2026-10-08:

| Package | Licence |
| --- | --- |
| `ai` | Apache-2.0 |
| `@ai-sdk/openai` | Apache-2.0 |
| `@cloudflare/computer` | MIT |
| `zod` | MIT |

Apache-2.0 and MIT are both compatible with distributing the agent under MIT.
Apache-2.0 asks that its NOTICE content, if any, travel with redistributions of
the dependency; the agent is deployed, not redistributed as a package, so no
NOTICE obligation attaches to this repo today. Re-check with
`curl -sS https://registry.npmjs.org/<name>/latest | jq .license` when a direct
dependency is added.

`@cloudflare/computer` is a preview: its README reads "This package is provided
as a preview for feedback. APIs are unstable and the design is subject to
change" (npm registry readme, version 0.4.1, read 2026-10-08), and Cloudflare's
launch post calls it "an early preview of @cloudflare/computer"
(blog.cloudflare.com/cloudflare-computer/). That is a maturity fact about the
agent's dependency, recorded here because README and RUNBOOK cite it.

## 5. Services the running system talks to

Not software this repo ships, but terms the operator accepts by configuring
them: xAI (`AI_PROVIDER=grok`), Anthropic (`AI_PROVIDER=claude`), Cloudflare
(the agent, AI Gateway, Workers), Telegram (the desk), and the broker. Each
provider's own terms govern what it retains. What this repo's own code sends to
each of them and stores locally is documented separately (straightedge#91).
