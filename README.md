<p align="center">
  <img src="docs/branding/readme-light.svg#gh-light-mode-only" width="800" alt="Applied — your job search, tracked automatically">
  <img src="docs/branding/readme-dark.svg#gh-dark-mode-only" width="800" alt="Applied — your job search, tracked automatically">
</p>

<h1 align="center">Applied</h1>

<p align="center">
  <strong>Applied turns a job-search inbox into a pipeline. Connect your mail and every
  confirmation, rejection, interview invite and take-home lands on the application it belongs to —
  instead of in a spreadsheet that is wrong within a week.</strong>
</p>

<p align="center">
  <a href="https://getapplied.vercel.app"><strong>Live app</strong></a> ·
  <a href="#status-and-access"><strong>Status &amp; access</strong></a> ·
  <a href="https://getapplied.vercel.app/demo">Try it, no account</a> ·
  <a href="https://getapplied.vercel.app/privacy">Privacy</a> ·
  <a href="https://getapplied.vercel.app/system-card">System Card</a> ·
  <a href="#what-you-can-check">What you can check</a> ·
  <a href="#licensing-and-partnership">Licensing</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/tests-3163%20collected%20%C2%B7%200%20skipped-brightgreen" alt="Tests">
  <img src="https://img.shields.io/badge/rules%20macro--F1-0.9791%20(CI%20floor%200.95)-2b9348" alt="Rules macro-F1">
  <img src="https://img.shields.io/badge/Next.js-16.3-000000?logo=nextdotjs&logoColor=white" alt="Next.js">
  <img src="https://img.shields.io/badge/React-19.2-61dafb?logo=react&logoColor=black" alt="React">
  <img src="https://img.shields.io/badge/Python-3.11-3776ab?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Postgres-RLS%20forced-336791?logo=postgresql&logoColor=white" alt="Postgres RLS">
  <img src="https://img.shields.io/badge/license-proprietary%20%C2%B7%20all%20rights%20reserved-8b1a1a" alt="License: proprietary, all rights reserved">
</p>

---

## What it does

Job hunting generates a flood of email — confirmations, rejections, interview invites, take-home assessments, recruiter follow-ups — and keeping a spreadsheet in sync with it by hand is tedious and wrong within a week. Applied connects to Gmail or iCloud, classifies each message into a job-search category, links related messages into a single tracked application, and shows where every opportunity actually stands. Predictions below a 0.85 confidence gate go to a human review queue instead of being silently accepted, and each correction is recorded against your own account. No model trains on it.

It is single-user by construction. Every table is scoped to one account and Postgres enforces
that scoping itself, so there is no shared workspace, no recruiter view and no way for one
account's mail to reach another's. It is for a person running their own search — someone sending
dozens of applications a term, someone changing industries, anyone whose inbox is the only
honest record of where they applied.

### In the product today

- **Inbox sync** — Gmail only (OAuth, `gmail.readonly` scope). Incremental or full sync, with polled status. **iCloud IMAP is not in the product today**: that client lives at `jobtracker/email_clients/icloud.py`, it belonged to the desktop app, and `test_cloud_app_does_not_import_the_desktop_email_clients` actively forbids the deployed app from importing it — so there is no path in the web UI that could reach it. The live WebSocket stream belonged to the same deleted client; Vercel's Python runtime does not support WebSockets
- **Automatic classification** into the nine `EmailCategory` enum values — `applied`, `pending_application`, `interview`, `rejection`, `offer`, `assessment`, `follow_up`, `other`, plus `needs_review` for anything under the gate. Eight of the nine are predicted labels; `needs_review` is the routing outcome.
- **Application linking** — related messages are grouped into one tracked application and relinked when new signals arrive
- **Human-in-the-loop review** — anything below `CONFIDENCE_AUTO = 0.85` (`classifier/hybrid.py`) lands in a review queue; corrections persist to `training_data` and flag the email `user_corrected`, so a later sync leaves your answer alone. Nothing retrains on them — the deployed classifier is rules-only. The training machinery ships in the repository, but no hosted path reaches it
- **Pipeline views** — Feature Cards, Compact Rows, or a Status Board, filterable by unreviewed and unlinked
- **Fixture demo** — the full UI on synthetic data at [`/demo`](https://getapplied.vercel.app/demo), no login. Layer 1 recomputes **live in the browser** there via `apps/web/lib/demo/rulesLayer.ts`, a port of the same 219 patterns; layers 2 and 3 are precomputed, because the app's CSP forbids the WASM eval and CDN fetch Transformers.js needs.
- **Weekly ML operations** — candidate mining for sparse labels, drift and confidence monitoring, and an alert-issue path, all scripted (`scripts/weekly_labeling_cycle.sh`, `scripts/monitoring_cycle.sh`). These are operator commands run by hand against a local backend; neither runs in CI, and the hosted app does not run them

### The three-layer cascade

Each layer is cheaper and more explainable than the next, so the expensive one only runs on what the cheap ones could not settle.

```mermaid
flowchart TB
    In["Email — subject · body · sender"] --> CF

    CF{{"0 · content filter<br/>newsletter / promo / security guards"}}
    CF -->|"matched"| Out1["category · method=content_filter"]
    CF -->|"pass"| R

    R{{"1 · rules<br/>219 regex patterns over 7 categories<br/>129 strong · 31 weak · 59 negative<br/>hand-written · classifier/rules.py"}}
    R -->|"scores a category"| Out2["category · method=rules"]
    R -->|"nothing scored"| CLOUD

    CLOUD{"deployment == cloud ?"}
    CLOUD -->|"yes"| Out3["other · confidence 0.0 · method=rules<br/>cloud never escalates"]
    CLOUD -->|"no"| E

    E{{"2 · embeddings<br/>intfloat/e5-small-v2 · PRETRAINED, not trained here<br/>cosine similarity vs stored examples<br/>accepts at ≥ 0.85"}}
    E -->|"≥ 0.85"| Out4["category · method=embeddings"]
    E -->|"below"| S

    S{{"3 · SetFit<br/>the ONE model fine-tuned in this repo<br/>body: sentence-transformers/paraphrase-MiniLM-L6-v2<br/>8 labels · accepts at ≥ 0.70"}}
    S -->|"≥ 0.70"| Out5["category · method=setfit"]
    S -->|"below"| F["4 · fallback<br/>best available signal · method=fallback"]

    Out2 --> G
    Out4 --> G
    Out5 --> G
    F --> G
    G{"confidence ≥ 0.85 ?"}
    G -->|"yes"| Auto["auto-classified"]
    G -->|"no"| Review["needs_review queue<br/>correction → training_data"]
```

Thresholds are `CONFIDENCE_AUTO = 0.85` and `CONFIDENCE_MIN_CLASSIFICATION = 0.70`, both defined in `backend/jobtracker/classifier/hybrid.py`. The 219 patterns are counted at their definition site — the `PATTERNS` dict in `classifier/rules.py` — not at any call site.

---

## Status and access

Applied is **in beta and under active development**, built and run by one person. There is no
company behind it and no staff.

**Connecting your own Gmail is invite-only.** `gmail.readonly` is a Google *restricted* scope:
until the app completes Google's OAuth verification and an independent CASA security assessment,
it may authorize at most **100 test users**, each added by address on the OAuth consent screen.
That cap is Google's, not a positioning choice, and it is why access is granted by hand.

**To ask for a seat**, email **aesh.03.23@gmail.com** with the Google account you would connect
read-only. Say roughly how you would use it; that is the feedback the beta exists for.

**What works with no invite and no account:**

- [`/demo`](https://getapplied.vercel.app/demo) — the whole interface over synthetic mail. Layer 1
  recomputes live in your browser.
- [`/import`](https://getapplied.vercel.app/import) — drop a Google Takeout export and have your
  own mail classified on-device.

What Applied reads from a connected mailbox, what it keeps, and how to delete all of it is written
out claim-by-claim at [`/privacy`](https://getapplied.vercel.app/privacy), with each claim cited to
the file that implements it.

---

## What you can check

The reason to trust a product that reads your mail is not a promise; it is something you can open.
Applied publishes specific numbers about itself and `scripts/readme_facts.py` recomputes every
**registered** one of them from the code that defines it — an unregistered number, or a
registered one whose site nobody pointed at the file holding it, is unchecked — [`readme-facts.yml`](.github/workflows/readme-facts.yml)
fails the build when a claim on this page stops matching the source, and a claim reworded so the
checker can no longer find it fails too. Where each number terminates is in [Verify it](#verify-it).

- **The classifier is deterministic, and it is not an LLM.** Classification is regexes, an
  embedding comparison and a small fine-tuned head, all of it code in this repository rather than a
  prompt — and on the deployed path it is the rules layer alone, which is a limitation and is
  stated as one below. No hosted model provider is in the path either way, so your mail is never
  sent to one.
- **The body is read to classify, then discarded.** Applied fetches the full message because
  Gmail's snippet is too short to recognise a rejection, and stores only that short snippet.
  `backend/tests/test_body_is_never_persisted.py` proves it rather than asserting it: a sentinel
  string planted in every fake body is searched for in every column of every stored row and in
  every API response, with a positive control that fails if the body was never fetched — because
  an absence test passes trivially when nothing was there to find.
- **Nothing is filed for you below the confidence gate.** A prediction under the gate goes to a
  human review queue instead of being written as a decision, and your correction is recorded as
  yours — stored in `training_data`, flagged `user_corrected`, and never overwritten by a later
  sync. No model trains on it. Applied reads mail under Gmail's restricted `gmail.readonly`
  scope, and Google's Workspace API user-data policy permits training only a model personalized
  to a single end user, with no co-mingling; the deployed classifier is rules-only, and
  `backend/tests/test_training_is_single_user.py` pins the corpus read to one `user_id` and
  raises on a corpus that spans two.
- **A metric that names its stage.** The **rules layer** — 219 regex patterns, no model — scores **0.9791 macro-F1** on the 96-example v3 evaluation set, committed at `backend/data/evaluation/baseline_rules_v3.json`. `backend-ci.yml` fails any merge that drops below a **0.95** floor. That number belongs to the rules layer and not to the full cascade; the difference, and why the filenames mislead, is spelled out in [Classifier evaluation](#classifier-evaluation).
- **Cost measured per layer, not averaged.** SetFit costs roughly **100×** the rules layer at p50 — 17.649 ms against 0.176 ms — and the rules layer answers 174 of the 288 classifications in the benchmark run. That is the cascade justifying itself as a measurement rather than an assertion. See [Performance](#performance).
- **Tenant isolation enforced by Postgres, live in production.** Eight tenant tables carry `ENABLE` + `FORCE ROW LEVEL SECURITY` with four policies each, and a ninth (`gmail_sync_enrollment`) carries three — **35 policies** — and production connects as `jobtracker_app`, a `NOSUPERUSER NOBYPASSRLS` role. 24 tests drive the real connection machinery against a real Postgres, and CI fails the build if they *skip*.
- **The trained model exports to something a browser can run.** The SetFit head quantized from 90,362,391 bytes of float32 to a **22,843,695-byte int8 ONNX** file, measured on the export produced by `ml/browser/export_onnx.py` on 2026-08-03. **Those weights are no longer published, and this repository no longer carries them** — see [The published checkpoint was withdrawn](#the-published-checkpoint-was-withdrawn). The export pipeline still ships; the artifact it produces stays local.

---

## Classifier evaluation

**The 0.9791 belongs to the rules layer. It is not a whole-system accuracy figure, and the filenames actively mislead on this point.**

The **rules layer** — 219 regex patterns and no model — scores **0.9791 macro-F1** (accuracy 0.9792, 2 of 96 misclassified) on the v3 evaluation set, committed at `backend/data/evaluation/baseline_rules_v3.json` over `classifier_eval_v3.jsonl`, and `backend-ci.yml` fails any merge below a **0.95** floor. The **full three-layer cascade** scores **0.9583** on that same set (accuracy 0.9583, 4 misclassified), recorded in `docs/ML_EXECUTION_TRACKER.md` Cycle H.

The trap is that `baseline_hybrid_v3.json` reports 0.9791 too. It does so because it was regenerated under the evaluator's `deterministic` hybrid profile, which calls `set_lite_mode(True)` and blanks `_known_embeddings` — so it measures the deterministic path, which is the regexes. Every metric block in the two files is identical, including both mismatch records; only the `meta` block differs, by `mode`, `hybrid_profile` and timestamp. `benchmark_history.md` says this in its own header. CI runs that profile on purpose, because a gate that consults a stochastic model is a gate that goes red for reasons unrelated to the change under test.

Being fair to the model: on the **v2** set the cascade beat the rules — 0.9843 against 0.9686 macro-F1 (`docs/ML_EXECUTION_TRACKER.md`, Cycle B5). The learned layers are not decoration; they lost on v3.

That comparison is now a measurement rather than a citation. `scripts/cascade_gate.sh` scores the full cascade and the rules layer over the same set in one run, and commits the delta, the per-example exchange and the checkpoint that produced it to `backend/data/evaluation/baseline_cascade_v3.json`. It does **not** run in CI, and the reason is not an omission: no SetFit checkpoint ships in this repository, so a GitHub-hosted runner has nothing to load. `learning-gate.yml` is therefore `workflow_dispatch`, and on a hosted runner it fails naming the directory it searched rather than degrading to the rules layer and reporting that as the cascade. What the number gates — the margin a learned layer has to clear before it may touch real mail, and what puts it back — is [`docs/ML_PROMOTION_POLICY.md`](docs/ML_PROMOTION_POLICY.md).

What the v3 set is, exactly, from `classifier_eval_v3_spec.json` and the dataset itself: **96 examples, 12 per label across 8 labels**, grouped as 65 core-positive, 17 edge-noise, 8 historical-miss and 6 core-negative, with confusion-pair tagging. The rows carry `subject`, `body_text`, `label`, `sender_email`, `scenario_group` and `confusion_pair` — and **no provenance field**, so the dataset does not record how many examples came from a real inbox versus a generator. That is a real limit on how far 0.9791 generalizes, and 96 examples is a small sample under any reading.

### The 18,200-message adversarial corpus

The answer to the paragraph above. `backend/tests/corpus_independent/` invents **18,200 messages
across 39 families over 8,500 companies**, every employer invented — six of those
families phrased in wordings transcribed from mail that actually arrived, and one carrying job
titles and locations copied byte-for-byte from public Greenhouse job boards, so the corpus is not
graded only on the vocabulary the author of `rules.py` wrote. It drives them through the whole
sync end to end: classify, roll up, upsert, persist the review queue, then read the board back out
of the tables. It is replayed
in day-sized batches because that is what a real sync is — a delta, usually of one message — and a
rebuild that only works when it can see the whole mailbox at once is not the thing that runs in
production. `scripts/run_independent_corpus.py` is the instrument;
`backend/tests/test_independent_corpus.py` is the ratchet, and pins the corpus by digest so a
number here describes the same mail it was measured on.

**Read the headline with its corpus. 18% of it is adversarial by construction** — mail written to
defeat the classifier, not mail that happens to be hard — so this is a stress figure and not the
accuracy a user would see on their own inbox.

**A rejection is no longer filed as an active application because of a word in the job title.** The
same body, differing only in the role: `Software Engineer, Early Career` scored `applied` at exactly
the 0.85 auto-file gate while `Embedded Software Engineer, Access Control` scored `rejection` and was
queued. `thank you for your interest.{0,40}(position|role|career)` was strong evidence for `applied`
and its window reached inside the job title, so the text naming WHICH application decided WHAT
HAPPENED to it. Removed, on evidence that it points the wrong way: across the transcribed wordings
that greeting opens **67% of rejections and 22% of confirmations**. Wrong verdicts stated as fact
went 139 to 119 with no confirmation family losing a single message
([#455](https://github.com/yadava5/applied/issues/455)).

**And read it knowing it is a property of the mix, not of the engine.** It was 93.05% on
2026-08-22 and is 92.36%
on the same engine, and it has moved three times since without a rule changing. It went DOWN first,
to 92.87%, because the corpus stopped being written entirely by the author of the classifier. It went
back up to 93.24% because [#626](https://github.com/yadava5/applied/issues/626) added 760 messages
whose difficulty is entirely in the IDENTITY layer — a job title readable only from the last segment
of an ATS subject — and the classifier reads every one of them correctly. Neither move is an accuracy
change.

**The third move is a different kind, and it is why this figure is lower than every earlier one
published here.** 93.24% was measured by an instrument that was wrong. The harness handed the
classifier bodies with no length cap, so the 320-message family built to test the product's own
4,000-character cap was graded on text production cannot deliver, and 160 verdicts counted correct
were the harness's rather than the product's
([#767](https://github.com/yadava5/applied/issues/767)). Corrected, they abstain — `wrong` did not
move and `Wrong AND stated to the user as fact` stayed 0, so nothing was ever told to a reader as
fact; 160 things stopped being counted as read. **Every earlier accuracy figure in this series
overstates by roughly the same 0.88 points.** Measured before the transcribed families landed: **100.0% of the 13,730 lifecycle messages contained an
engine pattern verbatim**, and **123 of 160 engine patterns were never exercised at all**. A corpus
in that state cannot find a gap — it can only confirm the pattern list against itself, and its
headline describes the author's vocabulary rather than the product's reach. `observed.py` holds 36
wordings transcribed from mail that actually arrived, from ten applicant tracking platforms, written
by recruiting teams with no knowledge of this repository. 92.36% is the first number here that was
not partly graded by the person who set the exam — a fact about who wrote the mail, and not the
reason for the 0.88 points above, which was the instrument.

| | measured 2026-09-06 |
| --- | --- |
| Correct | **16,810 of 18,200 — 92.36%** |
| Wrong | **304** |
| **Wrong AND stated to the user as fact** | **0** |
| Abstained (below the 0.70 review floor, the product says nothing) | **1086** |
| Board: cards / splits / merges / noise / misrouted review | **9,908 / 0 / 0 / 0 / 0** |
| Updates that reached the wrong card | **0** |
| Updates held for a person because the classifier was unsure | 360 |
| Mail about a real application that reached nothing | **0 lost**, 0 dropped |

**No message has ever landed on the wrong card.** Zero merges, zero misrouted updates over 18,200
messages and 9,908 cards — the half that could destroy a record, because a rejection filed onto a
sibling application settles it terminally and `advance_application_status` will never let it leave.
That claim survived the corpus growing to include applications that share one Gmail thread: real
applicant tracking systems send every acknowledgement for an employer under one subject from one
address, so Gmail files four different roles as one conversation, and the board still gives each its
own card. Thread is a delivery grouping here, never an identity.

**0 applications did end up on two cards each**, and it was 2 for one commit — long enough to name what
caused it. The role extractor disagreed with itself between a confirmation and its own rejection: one
said "applying to Northwind's Frontend Engineer position" and took the employer into the title, while
the other said "apply for the Frontend Engineer opening at Northwind" and did not. Two identities, two
cards, one application — and that title is seventeen characters, so it was never a length problem. A
second wording lost the role entirely, because the pattern that reads a requisition-numbered title
excluded the "(" that a real title like "Software Engineer I, Entry-Level (Graduation Date: Fall
2025-Summer 2026)" contains. Both are fixed. Split is kept as its own number because it is the milder
failure — the user sees one application twice and can act on it, where a merge loses a record
silently. [#466](https://github.com/yadava5/applied/issues/466).

**0 messages did MINT a card that should not exist**, and that number is not a fix — read it with the
caveat rather than without. It was 2, both a profile-completion nudge relayed by an ATS and scored
`assessment` at 0.90, and it reached 0 when the corpus began drawing realistic job titles and every
case re-drew its wording. The nudge no longer draws what got it onto a card; nothing about the
product changed. They come from `ats-relay-noise`, a family added on 2026-08-22 as the control on the
review floor — the product's behaviour on ATS mail that is not about you was not good before and
simply was not measured. It stays pinned at 0, so anything minting a card here is loud.

**Wrong verdicts stated as fact went 464 to 0.** There was no such thing as a wrong-but-hedged
verdict on the morning of 2026-08-22: the review queue caught the classifier being *unsure* and had
never once caught it being *wrong*, so every mistake it made it made confidently, and a user read it
as a fact about their own job search. Two thirds of them now land in the queue instead, because the
classifier stopped scoring quoted history and a reply's copied subject as the message's own words.

**That fix shipped, as [#451](https://github.com/yadava5/applied/issues/451)** — this paragraph used
to describe it as filed-not-shipped, three lines under a table already reporting the result. A
*reference* to an application tied with a *report* about one, and the tie broke on enum declaration
order. Two changes in one commit: the reference pattern demoted from `strong` to `weak` for
`applied`, and ties broken by what a category **claims**, a report of a later stage outranking an
assertion that an application merely exists.

**Zero is not "eliminated", and the difference is measurable.** At the two other seeds the corpus
re-samples, the same count reads 0 and 1. The pinned value is this seed's, not a proof that no
wording can still reach the auto-file gate wrongly.

**It was not free, and the price is the part worth reading.** 107 messages left the auto-filed
bucket: the wrong ones, and 35 *correct* ones that now wait in the review queue instead of arriving
on the board by themselves. 104 applications moved from a card the product guessed at to a question
it asks. Nothing became unreachable — messages lost did not move, and held updates rose from 631 to
685. A product whose pitch is that it can be trusted with a job search should prefer asking to
guessing, so that trade was taken deliberately.

The last row is the one that stayed bad longest. Until 2026-08-22 the replay ran only the rollup and never
the review path, so "held for a person to settle" and "vanished entirely" produced identical scores —
precisely the blind spot that let four Microsoft applications disappear on 2026-08-21 with every gate
green. It went from 610 to 0 on that day, and **0 messages about real applications
now reach no card, no queue and no counter**
([#447](https://github.com/yadava5/applied/issues/447), then
[#458](https://github.com/yadava5/applied/issues/458)). The 610 scored
`other` at 0.50 — not a lifecycle category, so neither the ATS floor nor the drop counter could see
them. `pipeline.references_an_application` floors a message an ATS relayed into the review queue when
its own text speaks about an application the reader made, and only then: the corpus carries 400
`ats-relay-noise` messages from the same relay domains scoring the same `other`, and none of them is
queued. The two assertions are each other's control, because reaching zero lost by queueing
everything an ATS sends would pass the first one alone.

**The 0 became 66 when the transcribed wordings arrived, and that is the honest reading rather than a
regression.** 0 was measured on a corpus written by the author of the classifier; against real mail
the same guarantee leaked 66. Completing the reference category with `your assessment` and `your
interview` closed most of them, and #466 — a character class that could not span a real job title —
closed five more.

The last 11 were closed by #458, and not by the wording that issue expected. Read rather than
executed they looked like one sender's rejection whose snippet stops one character before "with your
application"; measured, all 11 scored `follow_up` at 0.70, because the sender's own subject line says
"Follow-Up" and the decision sentence sits just past Gmail's snippet cut, so the veto that would
outrank it never fires. `follow_up` is dropped by the filing path, by the review queue and by the
drop counter alike, all three on one premise — that it is the reader's own chasing mail — which is
false of a message an applicant tracking system relayed. A relayed `follow_up` now reaches the queue;
one the reader sent still drops. No wording was copied into the product, which is why the earlier fix
was declined.

Every defect below is pinned at its measured size rather than excluded, because a corpus that
asserts only what already passes is a check that cannot fail — and this repository has a ledger of
those. When one is fixed its number moves and the gate has to say so:

- **`quoted-history`, 200 of 400.** Half of the follow-ups that quote their own confirmation read as
  `applied`, so an interview invite never advances the card it belongs to. The widest of the three
  and the mechanism behind the next one.
- **`rescinded-offer`, 0 wrong and 260 held** ([#417](https://github.com/yadava5/applied/issues/417)).
  It measured 164 of 260 confidently wrong until the classifier stopped scoring quoted history. It is
  not yet right, and this bullet used to describe the wrong failure: the withdrawals do not vanish,
  they **land in the review queue**. The offer files a card at `offered`, the withdrawal that revokes
  it scores `other` at 0.50 — nothing in the rejection patterns matches "we must rescind the offer" —
  and until the user answers, the board shows an offer they no longer have. The card is ahead of
  their life. A known, open defect, pinned so a fix moves it, and half of #447.
- **`hostile-zero-width`, 70 to 83 of 100** ([#424](https://github.com/yadava5/applied/issues/424) is
  the sender-name half). A zero-width space inside "moving" defeats the rejection pattern while
  rendering identically to the eye.

**Re-seeded, three times.** The seed varies which company, role, sender and wording every case
draws; it never varies the family sizes or the ground truth, so a re-seed is a different sample of
the same population rather than a different question. `quoted-history` is 200 of 400 at all three
seeds and truncation is wrong 0 times at all three, so those errors are structural.

**Two of the figures this paragraph used to give were a generation out of date, and the corrections
run in opposite directions.** `hostile-zero-width` was an exact 100 of 100 while every job title in
the corpus was short; with realistic titles drawn it measures 72, 72 and 81, asserted as a band of
70 to 83 — the attack still lands, but how many messages it lands on now depends on the title each
case drew. `rescinded-offer` moved the other way: it was a band of 164, 171 and 170, and is now
exactly **0 wrong** at every seed, with all 260 held for review instead. `correct` is no longer
pinned to a point at all; the re-seed test asserts it stays within a tolerance of the recorded
figure, because a long title can push a verdict past a bounded window at one seed and not another.

The seed did nothing at all until 2026-08-22: `_Builder` constructed a `random.Random` and never
drew from it, so three seeds produced one byte-identical corpus and "we ran it three times" would
have meant one run reported three times. `test_the_seeds_are_actually_different` is the control
that keeps the re-seeding honest.

The one family that fails safely is the control on those three: a rejection whose verdict sits past
Gmail's ~186-character snippet abstains 350 times and is wrong zero times. Truncation makes the
board silent, not wrong, and that distinction is asserted rather than assumed.

Its first run also found a production defect nothing else had: `employers_with_several_applications`
was quadratic in board size and ran on every sync. 2,000 messages did not finish in eight minutes;
after the histogram fix, 4.8 seconds.

```bash
cd backend && PYTHONPATH=. python ../scripts/run_independent_corpus.py
```

```bash
cd backend
# the exact rules gate CI runs
python -m jobtracker.scripts.evaluate_classifier \
  --mode rules \
  --dataset data/evaluation/classifier_eval_v3.jsonl \
  --baseline data/evaluation/baseline_rules_v3.json \
  --tolerance 0.001 --min-macro-f1 0.95

# the deterministic hybrid gate — same numbers, and that is the point
python -m jobtracker.scripts.evaluate_classifier \
  --mode hybrid --hybrid-profile deterministic \
  --dataset data/evaluation/classifier_eval_v3.jsonl \
  --baseline data/evaluation/baseline_hybrid_v3.json \
  --tolerance 0.001 --min-macro-f1 0.95

# the full cascade — this is the one that reads 0.9583
python -m jobtracker.scripts.evaluate_classifier \
  --mode hybrid --hybrid-profile full \
  --dataset data/evaluation/classifier_eval_v3.jsonl
```

The last command prints an `answered by:` line next to the score, and it refuses to report a verdict at all when no semantic layer answered: `_assert_layers_exercised` fails a `full`-profile run whose ML layers never loaded, unless you override it with `--allow-degraded-layers`. That guard exists because a cascade with no SetFit model on disk degrades to rules and reports 0.9791 again — flatteringly, for a classifier that is not running. It is the same failure `--require-semantic` guards in the latency benchmark.

---

## Performance

**Per-layer classifier latency.** 96-example v3 set, warm (a warmup pass excludes model load and lazy imports), 3 repetitions — 288 classifications.

| Layer | p50 | p95 | answered |
| --- | ---: | ---: | ---: |
| `content_filter` | 0.036 ms | 0.043 ms | 15 |
| `rules` | **0.176 ms** | 0.241 ms | 174 |
| `fallback` | 0.262 ms | 21.176 ms | 39 |
| `setfit` | **17.649 ms** | 37.702 ms | 60 |

The ratio is the result: SetFit costs roughly **100×** the rules layer at p50, and the rules layer alone answers 174 of the 288. That is the cascade doing its job as a measurement rather than an assertion. It is reported per layer because a single mean is a statement about the corpus mix as much as about the code — change the proportion of inputs the regexes catch and the mean moves with no code change. The script uses nearest-rank percentiles deliberately, because numpy's interpolated default disagrees on a sample this small.

**Provenance, stated plainly:** these figures come from the run recorded in commit `2c17470` (2026-08-03). **No results artifact is committed**, and the commit does not record the machine, so treat the absolute milliseconds as machine-dependent and the ratio as the durable claim. Regenerate with:

```bash
cd backend
python -m jobtracker.scripts.benchmark_classifier_latency --require-semantic --output latency.json
```

`--require-semantic` fails a run in which no model answered. The cascade degrades to rules when SetFit will not import, and a degraded run reports flatteringly low latency for a classifier that is not actually running.

**Model size.** The SetFit head exported to ONNX at **90,362,391 bytes** float32 and quantized to **22,843,695 bytes** int8 — measured on 2026-08-03 on the `ml/browser/artifacts/` export. Neither file is committed any more, so these two numbers are pinned in `scripts/readme_facts.py` rather than read off disk: they record an artifact that was withdrawn, not one you can `stat`.

---

## The published checkpoint was withdrawn

On **2026-08-15** the trained SetFit checkpoint and everything derived from it were pulled from
every public surface. What was published, and is no longer:

| Surface | Held | Now |
| --- | --- | --- |
| This repository | `model.onnx` (90.4 MB fp32), `model_quantized.onnx` and the `ml/browser/site/` copy (22.8 MB int8 each), `head.json` (the fitted head), `examples.json` (vectors from the fine-tuned body) | Deleted at `HEAD` and `.gitignore`d. **Still reachable in git history** — see below |
| `huggingface.co/yadava5/jobtracker-setfit-classifier` | `model.safetensors`, `model_head.pkl`, `training_metadata.json`, and a 723-byte hand-written model card | Private |
| `huggingface.co/spaces/yadava5/jobtracker-classifier` | its own int8 copy, `head.json`, `examples.json` | Private |

**Why — and precisely what was wrong.** The checkpoint (`setfit_model_20260306_175404`) recorded
`user_correction: 39` of `total_examples: 192` in its own `training_metadata.json`, so a published
artifact stated in machine-readable form that it had been fitted partly on a real mailbox.

That mailbox was **iCloud IMAP, not Gmail** — measured against the desktop-era store, which still
exists: every one of its 856 messages is `source_account = ICLOUD`, the Gmail-only `thread_id`
column is populated on none of them, and `sync_state` holds a single `icloud` row. A Gmail client
shipped in that build but was never authenticated on that machine. **No Google user data has ever
entered a training corpus here**, so Google's restricted-scope policy did not govern this
checkpoint, and any argument starting from "Applied reads Gmail" is about today's architecture
rather than the March tree that produced these weights.

What was wrong is simpler and survives the correction: **publishing weights trained on someone's
real mailbox**, which is poor practice whatever the provider. The fitted head and the embedding
bank went with the weights because both derive from the same fine-tuned encoder. Full provenance —
which of two same-day checkpoints, on what evidence, and what was *not* published — is in
[`docs/ML_PROMOTION_POLICY.md`](docs/ML_PROMOTION_POLICY.md).

**What this does not claim.** The blobs remain retrievable from this repository's git history and
from any existing clone or fork. Removing them at `HEAD` stops redistribution going forward; it is
not erasure, and this README does not pretend otherwise.

**One thing that was never published, stated because it is the obvious worry.** The checkpoint
directory carries an auto-generated model card of 166,204 bytes that is 94.6% verbatim
training-example text — real mailbox content. It never left the maintainer's machine: the Hugging
Face repository has two commits, both 2026-07-17, and its card is 723 bytes throughout. The
downloads obtained weights and metadata, not message text. The local card is not to be uploaded.

**The 90.4 MB → 22.8 MB claim loses its public receipt.** That compression figure is cited on the
résumé and portfolio, and until now anyone could check it by running `stat` on two committed files.
They can no longer. The measurement stands — it was taken on 2026-08-03 and is pinned in
`scripts/readme_facts.py`, which still fails the build if any of the four places quoting it drifts
— but it is now an attested number rather than a reproducible one, and it should be described that
way wherever it is cited.

**Getting the demo back.** `ml/browser/export_onnx.py` is unchanged and still produces the artifact
from a local checkpoint — it writes to `ml/browser/artifacts/`, so running `ml/browser/site/` again
also needs `head.json` and `examples.json` copied into that directory and `model_quantized.onnx`
copied to `ml/browser/site/model/onnx/model.onnx`. The route back to a *publishable* demo is a
checkpoint trained on synthetic data only — the 400-case corpus at `backend/tests/corpus/` is the
intended source. No such retrain has been run, and no number in this README has been re-recorded
against one.

---

## Implemented vs delegated vs planned

Being precise about this is the point.

### Implemented — hand-written in this repo

- **The rules engine.** 219 regex patterns across 7 categories (129 strong, 31 weak, 59 negative), the scoring weights (strong +3, +6 in subject; weak +1, +2; negative −5), the margin-to-confidence tiers, and the ATS-domain boost. Ported byte-for-byte to JavaScript in `apps/web/lib/demo/rulesLayer.ts` and to `ml/browser/site/app.js`. A further 40 **veto** patterns sit outside that count, because they score nothing: a veto caps its category at zero, which is the only way to overrule a strong subject match — +6 survives a negative's −5, so "Complete your self-assessment" read as an `assessment` invitation for as long as the negative was the strongest tool available. Two categories declare vetoes. `assessment` has 10, for the senses of the noun that are not a candidate test (risk, self, needs, impact, performance, damages). `follow_up` has 30 — the decision sentences, repeated verbatim from `rejection`'s strong list, because a message that states the hiring decision is not a nudge whatever its subject line reads. They name no marketing vocabulary on purpose: that belongs to the content guard which runs *ahead* of the rules layer, and a veto would apply it to message bodies at a threshold of one, suppressing every real invitation with an unsubscribe footer.
- **The cascade and its gate** — layer ordering, escalation conditions, the 0.85 auto-classify threshold and the 0.70 minimum for trusting a semantic layer, the `needs_review` routing, and the path that writes a correction into `training_data`. That path stops at the write: the row is recorded against its own user's account, and **nothing in the hosted app reads it back to train**. The retrain code exists in the repository and is reachable only as an operator command against a local backend — never on a request path, and default-deny since #357 (refused unless the corpus is entirely synthetic or its single owner is explicitly allowlisted).
- **The SetFit head is the one model trained here.** Fine-tuned on `sentence-transformers/paraphrase-MiniLM-L6-v2` over 8 labels, with a provenance contract (`training_metadata.json`) that is schema-versioned and validated *before* it is written, covering label counts, source counts, split sizes and exact `label_to_id` / `id_to_label` inversion.
- **The evaluation harness** — `evaluate_classifier.py` with its `deterministic` and `full` hybrid profiles, baseline comparison with tolerance, the macro-F1 floor, and `benchmark_classifier_latency.py`.
- **Multi-tenant isolation** — the `user_id` column and composite indexes, the 35 RLS policies, the per-transaction `request.jwt.claims` GUC with `search_path` pinning, and the Fernet credential envelope with a `key_id` column for rotation.
- **The cloud/desktop split** — lazy imports, PEP 562 module `__getattr__`, and the subprocess guard test that proves the heavy stack never enters `sys.modules`.
- **ML operations** — weekly sparse-label candidate mining with gap-based quotas, drift and confidence monitoring with thresholded alerts, and the alert-issue automation.

### Delegated — on purpose

- **The embedding model.** `intfloat/e5-small-v2` is **pretrained and used as shipped**. It is downloaded, not trained here; only the stored example set it compares against is this project's.
- **The SetFit body and training loop.** The `setfit` library does contrastive fine-tuning over a sentence-transformers backbone. This project supplies the data, the sampling policy and the provenance contract.
- **ONNX quantization.** The int8 export is produced by the standard toolchain (`ml/browser/export_onnx.py`) and executed by Transformers.js. No custom kernel, no custom quantizer.
- **Identity.** Supabase Auth issues and signs the JWT. This repo verifies it against a two-algorithm whitelist — ES256 against the project's published JWKS, HS256 against the shared secret, everything else rejected including `alg: none` and `alg: RS256` — and never mints one.
- **Mail access.** `google-api-python-client` for Gmail and `aioimaplib` for iCloud. No hand-rolled IMAP or OAuth transport.

### Planned — not in this build

- **Semantic layers in the cloud.** The deployed Vercel product runs the **rules layer only**, and there is no embedding or SetFit inference on that path. Moving them behind an external inference service is a documented follow-up in `requirements.txt` and `docs/WEB_ARCHITECTURE.md`; nothing is wired.
- **In-browser inference anywhere public.** The 22.8 MB int8 ONNX build was real and ran in the Hugging Face Space and under `ml/browser/site/`. Both were withdrawn on 2026-08-15: the Space is private and the weights are out of this repository. `ml/browser/site/` still holds the loader, the rules and the tokenizer, and will run again against a locally-exported checkpoint — it ships no weights. It never ran on `getapplied.vercel.app` in any case: the app's strict CSP forbids the WASM eval and CDN fetch Transformers.js needs, so `/demo` runs layer 1 live in the browser and serves precomputed layer 2 and 3 verdicts.
- **WebSocket sync.** Vercel's Python runtime does not support it, so sync status is polled. The desktop path had a live `/ws/sync-status` stream; that router was deleted with the rest of the desktop surface, so there is no WebSocket anywhere in the tree now.
- **Credential rotation.** `user_credentials.key_id` and a multi-key decrypt path are scaffolded. Only key `v1` is active and rotation is not wired.
- **A mobile client.** `apps/mobile/` is a reserved directory. There is no app in it.
- **Green ruff and mypy gates.** Both currently report and do not block. See [Testing](#testing).

---

## Licensing and partnership

Applied is **proprietary software, all rights reserved** — see [LICENSE](LICENSE). The source is
published so that the claims on this page can be checked, not so the software can be taken: no
licence to use, copy, modify, host, distribute or build on it is granted by its availability here.
Copies distributed under the repository's earlier licence keep the grant they were given — a
licence, once given, runs with the copy — and the PRIOR VERSIONS section of the licence file says
so in terms. Third-party dependencies keep their own licences, which nothing here alters.

**To license Applied, host it, evaluate it for an institution, or talk about a partnership,
sponsorship or funding**, contact Ayush Yadav at **aesh.03.23@gmail.com**. Permission is granted
only in writing.

**Contributions.** This is not an open-source project and pull requests are not accepted — the
licence grants no rights, so there is nothing to contribute under. Bug reports, a claim on this
page you can show is wrong, and security findings are genuinely wanted: email the address above.

---

## Who makes it

**Ayush Yadav** — sole author and maintainer. Design, full-stack engineering, and ML.
[github.com/yadava5](https://github.com/yadava5) · aesh.03.23@gmail.com

---

## Documentation

| Doc | What's inside |
| --- | --- |
| [System Card](https://getapplied.vercel.app/system-card) | Classifier design, evaluation, limitations, safety notes |
| [Privacy](https://getapplied.vercel.app/privacy) | What Applied reads, what it stores, where it runs, how to delete it — each claim cited to the file that implements it |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Choices whose rejected alternative is attractive and whose reason is invisible from the code — what was chosen against, and whether anything enforces it |
| [`docs/CLASSIFIER_RULES_GOVERNANCE.md`](docs/CLASSIFIER_RULES_GOVERNANCE.md) | What a change to the classifier rules has to prove before it lands, and what actually enforces it |
| [`docs/TEST_DATA_POLICY.md`](docs/TEST_DATA_POLICY.md) | What a fixture may contain, why nothing already published is deleted, and what the gate does not cover |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System architecture and component boundaries |
| [`docs/WEB_ARCHITECTURE.md`](docs/WEB_ARCHITECTURE.md) | Deployment modes, cloud auth flow, credential storage |
| [`docs/API_SPEC.md`](docs/API_SPEC.md) | Backend REST contract — auth, the 29-route table, and the shapes worth stating in prose. The machine-checked authority is `apps/web/lib/api/schema.d.ts`, generated from the app and gated by `e2e-ci.yml` |
| [`docs/ML_STRATEGY.md`](docs/ML_STRATEGY.md) | Classifier behaviour, training lifecycle, metadata contract |
| [`docs/ML_EXECUTION_TRACKER.md`](docs/ML_EXECUTION_TRACKER.md) | Every ML cycle with its measured results — the source for the cascade's 0.9583 |
| [`docs/ML_PROMOTION_POLICY.md`](docs/ML_PROMOTION_POLICY.md) | What a learned layer must beat before it serves real mail, and what puts it back |
| [`docs/ML_WEEKLY_OPERATIONS.md`](docs/ML_WEEKLY_OPERATIONS.md) · [`docs/ML_MONITORING_RUNBOOK.md`](docs/ML_MONITORING_RUNBOOK.md) | Weekly SOP and monitoring triage |
| [`docs/RLS-AUDIT-2026-08-03.md`](docs/RLS-AUDIT-2026-08-03.md) | Live row-level-security audit |
| [`docs/SETUP.md`](docs/SETUP.md) | Local setup and day-to-day development |
| [`DEPLOY.md`](DEPLOY.md) | Cloud deployment paths (auth, applications API, Gmail OAuth) |

---

## Under the hood

Everything below is for someone reading the code: how Applied is built, how to run it, and where
each number above comes from. The design write-ups it summarises live in the
[System Card](https://getapplied.vercel.app/system-card) and in [`docs/`](docs/), which are the
places to go for depth — this section is a map, not the territory.

The setup and deployment instructions here are the maintainer's, and are published so the claims
above can be traced to something runnable. They are not an offer of a licence: running your own
copy of Applied needs written permission, per [Licensing and partnership](#licensing-and-partnership).

It is a monorepo: a Next.js 16 app on Vercel over Supabase Postgres, talking to one FastAPI serverless function that imports `backend/jobtracker/`. The classifier runs its rules layer only in that function, for a budget reason spelled out under [Architecture](#architecture).

> **It used to be two.** Applied began as a SwiftUI macOS app driving a local FastAPI process with the full three-layer classifier, and the repository carried both modes over one Python package. The desktop client was de-scoped on 2026-08-12 and deleted, along with a second, unmounted set of FastAPI routers that had no user scoping at all (issue #73). `JOBTRACKER_DEPLOYMENT` still exists and `api/index.py` still forces it to `cloud` — that setting selects Postgres over SQLite and the encrypted-row credential store over the macOS Keychain, so it is load-bearing whether or not a desktop app exists.

> **A note on names.** The product was renamed from JobTracker to Applied. The internal identifiers were not renamed with it, and this README prints them verbatim wherever it gives a path or a command: the Python package is `backend/jobtracker/`, and every environment variable is prefixed `JOBTRACKER_` (`config.py`, `env_prefix="JOBTRACKER_"`). Renaming them would be a migration with no user-visible benefit, so they stayed.

### Architecture

#### One deployment, one package

`JOBTRACKER_DEPLOYMENT` still selects a mode, and `api/index.py` forces it to `cloud` before the app is imported. The table in `docs/WEB_ARCHITECTURE.md` is the source for this diagram.

```mermaid
flowchart TB
    subgraph Cloud["cloud mode — JOBTRACKER_DEPLOYMENT=cloud, forced by api/index.py"]
        direction TB
        Web["Next.js 16 · React 19<br/>apps/web/ on Vercel"]
        Fn["FastAPI on Vercel Python<br/>api/index.py → jobtracker.main_cloud"]
        PG[("Supabase Postgres<br/>asyncpg · transaction pooler")]
        Fernet["user_credentials<br/>Fernet-encrypted rows"]
        Rules["classifier: RULES ONLY"]
        Web -->|"Authorization: Bearer supabase JWT"| Fn
        Fn -->|"per-transaction request.jwt.claims<br/>RLS: 35 policies, FORCE"| PG
        Fn --> Fernet
        Fn --> Rules
    end

    Pkg["backend/jobtracker/ — the one package"]
    Pkg -.-> Fn

    Mail["Gmail API · iCloud IMAP"] --> Mailfetch["cloud/gmail_client.py"]
    Mailfetch --> Fn
```

The desktop half of this diagram — a SwiftUI app over a local FastAPI process over SQLite and the macOS Keychain, with the full three-layer cascade — was deleted on de-scoping. What remains of it in the package is deliberate: the SQLite paths in `database/connection.py` and the `keyring` import in `credentials/desktop.py` are still reachable under `JOBTRACKER_DEPLOYMENT=desktop`, which is why forcing `cloud` is a gate and not a formality.

#### The design decision that shaped the repo

**One classifier package, two import graphs.** The desktop app ran all three layers; it was deleted on 2026-08-12 and nothing runs them on a request path now. The cloud deployment runs the rules layer alone, and that is not a simplification for the README — it is enforced in code.

The reason is a hard budget. Root `requirements.txt` states it: torch (~800 MB) plus sentence-transformers plus SetFit exceeds Vercel's Python function budget of 50 MB zipped on Hobby and 250 MB zipped on Pro, and `docs/WEB_ARCHITECTURE.md` adds that even on Pro the cold-start cost blows the 60-second wall clock. So the deployed function must never *import* the heavy stack, not merely never call it.

Three mechanisms hold that line:

- `HybridClassifier.__init__` sets `_cloud_rules_only` when `settings.deployment == "cloud"` and lazy-imports `embeddings` / `setfit_model` inside method bodies (`classifier/hybrid.py`). The `jobtracker.classifier` package uses PEP 562 `__getattr__` so heavy re-exports resolve only on demand.
- Root `requirements.txt` is deliberately different from `backend/requirements.txt` and carries a DO-NOT-ADD list.
- `tests/test_main_cloud.py::test_cloud_classifier_is_rules_only_and_skips_heavy_ml_imports` subprocess-invokes `get_classifier()` under `JOBTRACKER_DEPLOYMENT=cloud` and asserts that neither `torch`, `sentence_transformers`, `setfit` nor `transformers` entered `sys.modules`. The `cloud-smoke` CI job runs it on every push.

The honest consequence: a cloud rules miss collapses to `{category: "other", confidence: 0.0, method: "rules"}`. It does not escalate, and since the desktop client was deleted there is no longer a second surface where the full cascade runs — corrections persist to Postgres and are reviewed in the web app. Layers 2 and 3 remain in the tree, are still exercised by `backend-ci.yml`, and are still what `ml/` trains and evaluates; they are simply not on the request path.

#### Data model

Every tenant table carries `user_id UUID NOT NULL` (Alembic rev `6e64c46d32fd`), keyed to Supabase `auth.users.id`. The column's *default* is a sentinel UUID, `LOCAL_USER_ID` (`database/models.py`), left over from the single-user desktop build — which is why RLS, not application code, is what actually enforces isolation.

```mermaid
erDiagram
    auth_users ||--o{ applications : owns
    auth_users ||--o{ emails : owns
    auth_users ||--o{ sync_state : owns
    auth_users ||--o{ user_credentials : owns
    applications ||--o{ emails : "links (nullable FK)"
    applications ||--o{ contacts : has
    applications ||--o{ interviews : has
    emails ||--o| email_embeddings : "has vector"
    emails ||--o{ training_data : "yields correction"

    applications {
        int id PK
        uuid user_id FK
        string company
        string position
        enum status
        date applied_date
        string source
        string url
    }
    emails {
        int id PK
        uuid user_id FK
        int application_id FK "nullable"
        enum source_account "gmail | icloud"
        string message_id UK
        datetime received_at
        string subject
        string sender_email
        enum classified_as
        float classification_confidence
        string classification_method
        bool user_corrected
        bool is_reviewed
    }
    contacts {
        int id PK
        uuid user_id FK
        int application_id FK
    }
    interviews {
        int id PK
        uuid user_id FK
        int application_id FK
        enum type
        datetime scheduled_at
        enum status
    }
    training_data {
        int id PK
        uuid user_id FK
        int email_id FK "nullable"
        string label
        string source "user_correction | mock_seed_v3 | external_dataset"
    }
    email_embeddings {
        int id PK
        uuid user_id FK
        int email_id FK
        string label
        bytes embedding "384 floats"
        string model_version
    }
    sync_state {
        int id PK
        uuid user_id FK
        string account_type
        string account_email
        string gmail_history_id
        int imap_last_uid
    }
    user_credentials {
        uuid user_id PK
        string kind PK "gmail_oauth | icloud_mail"
        bytes ciphertext "Fernet"
        string key_id
    }
```

`auth_users` is Supabase's `auth.users`; it is not defined by this repo's migrations, and it does not exist under SQLite, where the same columns are plain UUIDs.

#### Row-level security, as deployed

RLS here is live, not staged. Verified against the production database on 2026-08-03 and re-read for this README against the migrations and `docs/RLS-AUDIT-2026-08-03.md`:

- **Eight tenant tables** — `applications`, `emails`, `contacts`, `interviews`, `training_data`, `email_embeddings`, `sync_state` (rev `a8d4ec5fba26`) and `user_credentials` (revs `c4_user_credentials_rls`, `c5_force_user_credentials_rls`) — each with `ENABLE` **and** `FORCE ROW LEVEL SECURITY` and four policies (`SELECT` / `INSERT` / `UPDATE` / `DELETE`). That is **32** owner policies. `FORCE` is the part that matters: without it the table owner is exempt, and the owner is what an application usually connects as.
- **A ninth table is deliberately different, and says so.** `gmail_sync_enrollment` (rev `e2b6f0a4d517`) is `ENABLE` + `FORCE` with **3** policies: owner-scoped `INSERT` and `DELETE`, and a `SELECT` policy scoped `TO jobtracker_app` with a permissive predicate. That is the one place a predicate is not `user_id = auth.uid()`, because the scheduled sync carries no JWT and must still enumerate who has Gmail linked. What it exposes to a `jobtracker_app` connection is exactly which user ids have linked Gmail and when — a membership fact. The table holds no ciphertext and no email address, so there is no secret in it to leak. That is **35 policies** across the nine.
- **The application role cannot bypass any of it.** Production connects as `jobtracker_app`: `rolsuper=false`, `rolbypassrls=false`, `rolcanlogin=true`.
- **Identity is bound per transaction.** `_install_rls_guc_listener` in `database/connection.py` sets `request.jwt.claims` transaction-locally on every `begin`, so nothing leaks across the PgBouncer transaction pool, and `search_path` is pinned to `public` so a policy cannot be fooled by a shadowed relation.
- **It fails closed.** With no user bound, `auth.uid()` is NULL, `user_id = NULL` matches nothing, and an unauthenticated path sees zero rows rather than everything.
- **All 32 owner predicates are `user_id = (SELECT auth.uid())`** after rev `c6_rls_initplan_hoist` — the enumeration policy on `gmail_sync_enrollment` above is the deliberate exception, and it guards a table with no secret in it. This is a planning-time change: bare `auth.uid()` is `STABLE` and re-evaluated once per *row*; the sub-select is hoisted into an `InitPlan` evaluated once per *query*. Measured on a **synthetic** 200,000-row sequential scan in a throwaway `postgres:16` — **not** a production measurement — invocations went 200,001 → 1 and the query 126 ms → 10 ms, with an identical row set. The invocation ratio is the part that holds at any table size; Applied's real tables are far smaller.
- The migration is a **no-op on SQLite**, so `alembic upgrade head` stays green for CI. It was written while the SQLite-backed desktop build still existed and the no-op is what kept that build green too.

### Tech Stack

Versions are pinned from `apps/web/package.json`, `requirements.txt`, and the CI workflows.

**These are gated now, and were not.** `scripts/readme_facts.py` checks the numbers it has been given a rule for, so for as long as no dependency version was registered as a fact this table could drift freely under a clean `--check` — and it did: it read `zod 3.25`, `Next.js 16.2.11`, `React 19.2.4` and `@supabase/ssr 0.5` against a manifest pinning entirely different versions, hand-corrected 2026-08-21, and drifted again by three of the four within two weeks. Registered against `apps/web/package.json` in #401: Next.js, React, zod (major and specifier), `@supabase/ssr` and `@playwright/test`. TypeScript and Tailwind are quoted as bare majors here and the manifest pins them as bare majors too, so there is nothing to drift; `openapi-fetch` and `openapi-typescript` are not registered and remain hand-transcribed.

#### Web

| Category | Technologies |
| --- | --- |
| **Framework** | Next.js 16.3.3 (App Router, Turbopack), React 19.2.8 |
| **Language** | TypeScript 5 (strict), zod 4 (`^4.5.2`) for runtime env validation |
| **Styling** | Tailwind CSS 4, shadcn/ui-compatible scaffold, Radix Slot |
| **Auth** | Supabase Auth via `@supabase/ssr` `^0.12.5` (SSR cookie `getAll`/`setAll`) |
| **API client** | `openapi-fetch` 0.17 over types generated by `openapi-typescript` 7 |
| **Testing** | Playwright `^1.62.1` (21 spec files under `apps/web/tests/e2e/`) |

#### Backend

| Category | Technologies |
| --- | --- |
| **Runtime** | Python 3.11, FastAPI, Uvicorn |
| **Data** | SQLModel / SQLAlchemy 2 (async), Alembic, Supabase Postgres via asyncpg (SQLite remains the test fixture and the deleted desktop build's store) |
| **Auth** | PyJWT `[crypto]`, ES256 via the project JWKS or HS256 via the shared secret (one algorithm per branch, chosen before verification), `audience="authenticated"`, `require=["exp","sub","aud"]` |
| **Secrets** | `cryptography.fernet` rows in `user_credentials` |
| **Email** | `google-api-python-client` (Gmail, `gmail.readonly`), `aioimaplib` (iCloud), BeautifulSoup + lxml for parsing |

#### ML

| Category | Technologies |
| --- | --- |
| **Layer 1** | Hand-written regex engine, 219 patterns across 7 categories |
| **Layer 2** | `intfloat/e5-small-v2` (pretrained, downloaded, not trained here) |
| **Layer 3** | SetFit fine-tuned in this repo on `sentence-transformers/paraphrase-MiniLM-L6-v2`, 8 labels |
| **Export** | int8 ONNX + Transformers.js (`ml/browser/`), Gradio Space (`ml/demo/`), BentoML service (`ml/bento_service.py`) |
| **Tracking** | MLflow — `ml/track_run.py:72` points the tracking store at `sqlite:///ml/mlflow.db`, which is git-ignored (`*.db`) and created by the first local run rather than committed; the plain-filesystem store is in maintenance mode upstream, which is why it is SQLite. The committed run artifacts live under `mlruns/` at the **repo root**, not `ml/mlruns` — that path does not exist. Registry alias `production` gated at the 0.95 floor, plus a W&B mirror (offline) |

#### Infrastructure

| Category | Technologies |
| --- | --- |
| **Hosting** | Vercel (Next.js + one Python function, `maxDuration` 60), Supabase Postgres, Hugging Face Spaces |
| **CI** | GitHub Actions — 15 workflows (see [Verify it](#verify-it)) |

### Testing

**3163 tests collected, 0 skipped.** These figures were recorded on 2026-09-05 by `python3 scripts/readme_facts.py --record`, which runs `pytest tests -q --cov=jobtracker` in the project's Python 3.11.14 venv and writes `docs/readme-facts.json`; `--check` fails the build when this page and that artifact disagree. `--record` refuses to write at all unless that run was whole — Docker reachable, nothing skipped, suite green — because skipped tests are still *collected*, so a recording taken without the Postgres extras used to publish "0 skipped" while five modules sat out (#351). The artifact names the interpreter that ran the suite rather than the one that ran the script; those differ here, and a Python 3.14 run is exactly what produced the wrong coverage figures corrected below. The count was first published from commit `37dd805` and corrected in `5b895d8`. It has grown since: a static parse counts 1843 `test_*` functions across 159 modules at HEAD, against 300 across 25 modules at `37dd805` — the tests added with the sync-cursor, recoverable-removal, company-matching, stage-vocabulary, application-identity, RLS, migration-chain and expand-only-gate work, five of which brought their own module (`test_status_vocabulary.py`, `test_application_identity.py`, `test_rls_postgres.py`, `test_migrations_postgres.py`, `test_expand_only_gate.py`). The bold 3163 is the artifact's and moves only on `--record`, while the static parse is recomputed on every `--check`, so between recordings the two drift apart — and parametrization lifts collected above the parse besides. CI reruns the suite with `--cov` on every push, so the current number lands in a public run log rather than resting on this sentence.

The Postgres row-level-security module is the only thing in the repo that can demonstrate the isolation the product claims, and **24 tests** now exercise it. It has not always run: its tests waited on a database URL no workflow set, and a skip is green, so the 10 it held on 2026-08-02 had **never executed anywhere**. Two fixes: `test_rls_postgres.py` now starts its own `postgres:16` via testcontainers when `JOBTRACKER_TEST_PG_ADMIN_URL` is absent and Docker is available, and the `rls-postgres` CI job supplies its own service container. That job then parses the JUnit XML and **fails the build if the suite reports zero tests or any skip**, because a skipped security test and a passing one produce the same green tick.

Those tests drive the production machinery, not a fixture: `jobtracker.database.connection.get_session` with its real GUC and `search_path` handling, against a non-`BYPASSRLS` role, asserting that a raw query with the application-level `WHERE user_id = ...` filter *removed* still returns nothing for another tenant.

`test_migrations_postgres.py` rides in the same job, for a defect the rest of the suite is structurally blind to: on SQLite, `sa.Enum` renders as `VARCHAR`, so a migration can add an enum label in the wrong case and every other test stays green while production 500s on the first write. It applies the whole Alembic chain to a bare database through the real CLI, then asserts the `applicationstatus` labels are the Python enum's member names in declaration order, that a row round-trips, and that the lowercase spelling is genuinely rejected. It is guarded by the same "did it actually run" JUnit check as the RLS suite.

**Coverage**, from the same run: **66.31%** overall — 9,543 statements, 3,215 missed. The distribution matters more than the total. `jobtracker/cloud`, the code that actually deploys, is at **92.5%**; `auth` 89.6%; `database` 81.1%. What pulls the average down is `jobtracker/scripts` at 2,363 statements and 33.7%, of which eight modules no test imports account for 1,010 statements at 0%.

This paragraph read "54% overall, 61% excluding one-off scripts … 2,163 statements of dataset importers" until 2026-08-03, and three of those four numbers were wrong. The corrections, in full, are in commit `5b895d8`: 54% came from a Python 3.14 run, where PEP 649 stops emitting line events for annotation-only class attributes, so the same tree measures 8,018 statements instead of 8,210 — a 192-statement gap across 13 Pydantic models. 61% is dropped rather than restated, because it reaches 60.5% only by excluding 1,234 statements that CI invokes directly as gates. And 2,163 never described dataset importers; those are 662 statements at 0%. The portfolio was citing this README instead of a run, which is how they persisted.

| Layer | Tooling | Scope |
| --- | --- | --- |
| **Backend unit + integration** | pytest | classifier, API, sync, auth, cloud entrypoint, evaluator, ML-ops scripts |
| **Database isolation** | pytest + testcontainers / CI service container | 24 RLS enforcement tests against real Postgres |
| **Web e2e** | Playwright | 21 spec files — auth, beta, boot, connect, dashboard, demo, file-application, import, inbox-geometry, landing, navigation, production, review-picker, sample-inbox, scan-correct, security-headers, session-edge, settings, shell, smoke, stage-focus |
| **Web e2e, production build** | Playwright vs `next build` + `next start` | the `production` spec: every route driven against a real production build, failing on React hydration errors, uncaught exceptions and 5xx |
| **Web static** | `tsc --noEmit`, ESLint, `next build` | every push touching `apps/web/**` |
| **README claims** | `scripts/readme_facts.py --check` | every **registered** fact, at every **registered** claim site; no path filter. Both words are load-bearing and #401 is why: a site that names no file defaults to `README.md`, so for six months this row read "across every file that holds one" while four claims in three other files said 18 against a tree of 20. Coverage here is set membership in two dimensions — which facts are registered, and which files their sites point at — and the second one is the quiet one. Most are recomputed from source on each run; the ones needing a pytest + coverage run are replayed from `docs/readme-facts.json`. The totals used to be written out here and in the workflow table below, in two different and both-wrong versions — a hand-maintained count of a checker is the one number the checker cannot check. A number that is not registered is not checked — see the note under Tech Stack |

Two lint gates run **advisory**, on purpose. `ruff check .` reported 379 findings on its first CI run (2026-08-07) and `mypy .` under `strict = true` reported 879 across 65 of 92 files. Both were configured in `pyproject.toml` from the start and had never actually run. They print their count on every build and flip to blocking when they reach zero; a gate that is red from birth gets ignored or deleted, and neither should be silenced with `--fix` or blanket `# type: ignore`.

**Dependencies.** A local `pip-audit -r requirements.txt` against the root file — the set that ships to Vercel — reported **0 known vulnerabilities** when the audit ran on 2026-08-03. The CI step is advisory and, because the job's working directory is `backend/`, it resolves `backend/requirements.txt` instead — the full desktop set. It reported **8 known vulnerabilities in 2 packages** (cryptography, transformers) on 2026-08-07, and `transformers` is the tell: it is pinned in `backend/requirements.txt` and appears nowhere in the root file, so the findings cannot be describing the Vercel surface the step's own comment names. Neither figure covers torch, which CI installs out-of-band from the PyTorch CPU index. Use `pip-audit` and not `osv-scanner` against these files: osv-scanner resolves each `>=` floor to its *minimum* and reports the worst case the constraints permit, which overstated this repository by two orders of magnitude.

### Getting Started

#### Prerequisites

- Python 3.11+ (CI pins 3.11; `backend/.venv311` is the venv `scripts/generate_api_schema.sh` prefers, not `backend/.venv`)
- Node.js 22 and pnpm 10, for the web app — the major is load-bearing, not incidental. `pnpm test:unit` imports `.ts` modules straight from `.mjs` test files and runs them on the runtime's built-in type stripping, which needs **22.6 or newer** (and the built-in glob, 21 or newer). On Node 20 those tests do not fail, they refuse to load with `ERR_UNKNOWN_FILE_EXTENSION` — which is how they once existed while no job ran them
- A Supabase project (Postgres + Auth) for anything past the landing page and `/demo`
- Internet on first run of the **ML** tooling, to download `intfloat/e5-small-v2`. The web app and the deployed backend never download a model: the cloud path is rules-only

#### Web app

```bash
git clone https://github.com/yadava5/applied.git
cd applied/apps/web

cp .env.example .env.local     # Supabase URL + anon key, BACKEND_API_URL
pnpm install --frozen-lockfile
pnpm dev                       # http://localhost:3000
```

The landing page (`/`) and the fixture demo (`/demo`) run with **no backend and no Supabase**, so a review deploy needs only placeholder values — which is exactly what `frontend-ci.yml` supplies. See [`apps/web/README.md`](apps/web/README.md) for the full web setup.

#### Backend

The backend is served by Vercel as a Python function from `api/index.py`; there is no local process to start for normal web work — point `BACKEND_API_URL` at a deployment. To run its suite, or to serve it locally:

```bash
cd backend
python3.11 -m venv .venv311
.venv311/bin/pip install -r ../requirements.txt \
    pytest pytest-asyncio pytest-cov httpx aiosqlite alembic keyring numpy
.venv311/bin/python -m pytest tests -q          # see docs/SETUP.md for the two heavy-ML exclusions

JOBTRACKER_DEPLOYMENT=cloud .venv311/bin/python -m uvicorn jobtracker.main_cloud:app --port 8000
```

The full walkthrough — including why `alembic`, `keyring` and `numpy` must be installed by hand — is in [`docs/SETUP.md`](docs/SETUP.md).

#### Environment variables

Backend settings use the `JOBTRACKER_` prefix (`backend/jobtracker/config.py`).

```env
# Mode
JOBTRACKER_DEPLOYMENT=cloud             # or `desktop` (default)
JOBTRACKER_ENVIRONMENT=test             # used by CI and local test runs

# Cloud only
JOBTRACKER_SUPABASE_JWT_SECRET=...      # Supabase shared secret, for the HS256 branch
JOBTRACKER_SUPABASE_JWKS_URL=...        # required when the project signs ES256, which is the default since 2025
JOBTRACKER_SECRET_ENCRYPTION_KEY=...    # urlsafe base64, 32 bytes, for Fernet
JOBTRACKER_CORS_ALLOWED_HOSTS=...

# Tests
JOBTRACKER_TEST_PG_ADMIN_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/test_db
```

```env
# apps/web/.env.local
NEXT_PUBLIC_SUPABASE_URL=https://<project>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
BACKEND_API_URL=http://localhost:8000
```

Generate a Fernet key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

#### Commands

| Command | What it does |
| --- | --- |
| `./scripts/generate_api_schema.sh` | Regenerate `apps/web/lib/api/schema.d.ts` from the cloud app; `e2e-ci.yml` fails on any diff |
| `pnpm dev` / `pnpm build` | Web app, from `apps/web/` |
| `pnpm typecheck` / `pnpm lint` / `pnpm e2e` | The three checks `frontend-ci.yml` and `e2e-ci.yml` run |
| `pnpm e2e:prod <files>` | **The landing, boot and production specs only run this way.** They are skipped against `next dev`, which gets this page's geometry wrong, so `pnpm e2e tests/e2e/landing.spec.ts` reports `24 skipped` and exits 0. Needs `pnpm build && pnpm start` first |
| `pnpm e2e:check <files>` | Same as `pnpm e2e`, but fails instead of exiting 0 when every selected test skipped |
| `pytest tests -q --cov=jobtracker` | Backend suite with coverage, from `backend/` |
| `./scripts/generate_eval_baselines.sh --version 3` | Regenerate the committed rules and hybrid baselines |
| `./scripts/train_pipeline.sh` | Retrain the SetFit head and write the provenance artifact |
| `./scripts/weekly_labeling_cycle.sh --append-tracker` | Weekly sparse-label candidate mining |
| `./scripts/monitoring_cycle.sh` | Drift and confidence monitoring report |
| `python3 scripts/readme_facts.py` | Verify every number in this README against the code (`--write` to repair, `--record` to re-measure the suite) |

<details>
<summary><strong>Project structure</strong> — where each of the above lives</summary>


```
applied/
├── apps/
│   ├── web/                 # Next.js 16 App Router product (the cloud UI)
│   │   ├── app/             # (auth) · (app) · demo · import · api routes
│   │   ├── lib/demo/        # rulesLayer.ts — layer 1 ported to run live in the tab
│   │   └── tests/e2e/       # 21 Playwright specs
│   └── mobile/              # reserved; empty
│
├── backend/
│   ├── jobtracker/          # the one package
│   │   ├── classifier/      # rules.py (219 patterns) · embeddings.py · setfit_model.py · hybrid.py
│   │   ├── cloud/           # every router the app mounts, require_user() at the router level
│   │   ├── main_cloud.py    # the only app builder
│   │   ├── auth/            # supabase_jwt.py — ES256/HS256 whitelist, one per branch
│   │   ├── credentials/     # types · desktop (Keychain, unused) · cloud (Fernet)
│   │   ├── database/        # models, connection (the RLS GUC listener lives here)
│   │   └── scripts/         # evaluator, latency benchmark, ML-ops tooling
│   ├── alembic/versions/    # 24 revisions incl. the RLS + InitPlan-hoist migrations
│   ├── data/evaluation/     # eval sets, committed baselines, benchmark + monitoring history
│   └── tests/               # 159 modules
│
├── ml/                      # the classifier as a deployable service
│   ├── browser/             # ONNX export + the in-browser site (Transformers.js)
│   ├── demo/                # Gradio Space
│   ├── service.py           # standalone sync facade
│   └── track_run.py         # MLflow run + registry promotion past the 0.95 floor
│
├── api/index.py             # Vercel Python entry → jobtracker.main_cloud
├── requirements.txt         # the CLOUD dependency set; deliberately not backend/requirements.txt
├── docs/                    # architecture, API spec, ML strategy + runbooks, RLS audit
└── .github/workflows/       # 15 workflows
```

</details>

### Technical Decisions

**Rules first, model last.** Ordering the cascade cheapest-first is not only a latency decision; it is an explainability one. A regex hit can be shown to a user as the phrase that matched. The measured cost — 0.176 ms against 17.649 ms at p50 — is what makes the ordering worth the extra code path, and the review queue is what catches the cases where the cheap layer was confidently wrong. The tradeoff is real: the rules are hand-maintained and every new ATS phrasing is a code change.

**A gate below the measured value, and a gate on the gate.** The macro-F1 floor is 0.95 against a measured 0.9791, deliberately loose, because a gate pinned at the current number turns every honest refactor red; what it guards against is a collapse, not a two-point drift. Separately, the RLS job asserts that its own suite *ran*, because the failure mode that actually occurred here was not a failing test but ten silently skipped ones.

**Deployment mode as an import-graph decision.** The alternative to splitting the classifier by deployment was a single build that carries torch everywhere — which does not fit in a Vercel function — or two divergent packages, which drift. Applied keeps one package and makes the divergence a property of the import graph, then tests that property in a subprocess. The cost is honesty overhead: the cloud runs a weaker classifier than the full cascade, and every surface that talks about accuracy has to say which one it means. That was true of the desktop comparison when the desktop existed, and it is true today of the cascade the repository still trains and evaluates but does not deploy.

### Verify it

Every number above terminates in something you can open.

**Continuous integration** — `.github/workflows/`:

| Workflow | What it proves |
| --- | --- |
| `backend-ci.yml` | `pytest tests -q --cov=jobtracker` (the coverage number lands in the public run log); the rules gate at `--min-macro-f1 0.95`; the deterministic hybrid gate; the `rls-postgres` job with its assert-it-ran step; the `expand-only` job, which walks the Alembic chain one revision at a time against a `postgres:16` service and fails a revision that drops or narrows anything without a module-level `CONTRACT_STEP` saying why; the `cloud-smoke` job that imports the cloud app under `JOBTRACKER_DEPLOYMENT=cloud` and probes `/health` |
| `frontend-ci.yml` | `pnpm typecheck`, `pnpm lint` (`--max-warnings 0`, so every warn-level rule next ships — the six `jsx-a11y/*` among them — is a red build rather than a printed suggestion), `pnpm test:unit`, `pnpm build` on Node 22 / pnpm 10. The Node major is a constraint, not a default: `test:unit` needs the runtime's type stripping (22.6+), so pinning back to 20 does not fail the job — it stops running the unit suite, which is exactly what happened before |
| `e2e-ci.yml` | The API schema drift gate — `apps/web/lib/api/schema.d.ts` regenerated from `jobtracker.main_cloud`, red on any diff — then Playwright twice: against `pnpm dev` and against a real `next build` + `next start`. **No backend server is booted.** That step used to run `uvicorn jobtracker.main:app` on `127.0.0.1:8000`; that was the desktop app, deleted in #73, and the workflow now points at nothing on that port. Every route the specs visit is public or redirects at the protected layout before any API call, which the sibling `playwright-production` job — same suite, no backend, green — demonstrates. Uploads the HTML report and per-test traces; there is no `backend.log` any more |
| `codeql.yml`, `gitleaks.yml` | SAST and full-history secret scanning |
| `.githooks/pre-commit` (local, opt-in) | The same scan over the *staged* diff, before the commit exists. Not a workflow — git does not enable a hooks path for you, so each clone runs `git config core.hooksPath .githooks` once. CI is the net that always runs; this one exists because a credential that reaches GitHub is published even if the next commit deletes it |
| `ml-monitoring-weekly.yml` | Scheduled drift/confidence report, artifacts uploaded, alert issue opened on threshold breach |
| `scorecard.yml`, `booklet.yml` | Supply-chain grading; the system-card booklet build |
| `readme-facts.yml` | `python3 scripts/readme_facts.py --check` — every registered fact at every claim site — recomputed from the source that defines it, except those replayed from `docs/readme-facts.json` because they need a full pytest + coverage run. `--check` prints the current totals; they are not restated here, because a hardcoded count of the checker is exactly the kind of number it cannot police. Unfiltered by path, because a claim here can be invalidated from anywhere; and a claim site whose sentence was reworded so the checker can no longer find it fails the build rather than passing quietly. **What it does not do is find numbers nobody registered** — the four wrong dependency versions under Tech Stack sat on this page through a green `--check` |

**Committed evaluation artifacts** — `backend/data/evaluation/`:

- `classifier_eval_v3.jsonl` and `classifier_eval_v3_spec.json` — the 96 examples and the coverage contract they must satisfy
- `baseline_rules_v3.json` — where 0.9791 lives, with the confusion matrix and both mismatches
- `baseline_hybrid_v3.json` — the deterministic-profile file that reads the same, which is the trap this README exists to defuse
- `baseline_cascade_v3.json` — the cascade with its models actually answering, with the checkpoint that produced it, the layer that answered each mismatch, and the delta to rules
- `benchmark_history.{md,jsonl}` — every baseline, v1 through v3, with its profile
- `ml_monitoring_report.{md,json}`, `ml_monitoring_history.jsonl`, `label_balance_report.md`
- `ml/browser/artifacts/model_quantized.onnx` — was 22,843,695 bytes. **Withdrawn 2026-08-15 and no longer committed**, so this one cannot be checked with `stat`; the number is pinned in `scripts/readme_facts.py`. Re-export it locally with `ml/browser/export_onnx.py` if you need the artifact.

**Third-party score.** [OpenSSF Scorecard](https://scorecard.dev/viewer/?uri=github.com/yadava5/applied) grades this repository against 18 supply-chain checks and publishes the result. It is computed by someone else, which is the entire value: a number this project calculates about itself is a claim. Several of the 18 grade repository *settings* that no file in the repo can turn on, so the score moving up over time is a better signal than wherever it starts.

**Security posture.** `docs/RLS-AUDIT-2026-08-03.md` is the read of the live database, including a retracted finding it kept rather than deleted, and `docs/harden-2026-08-03.sql` is the applied fix with its verification query.

---

<p align="center">
  <sub>Built by Ayush Yadav · <a href="https://getapplied.vercel.app">getapplied.vercel.app</a></sub>
</p>
