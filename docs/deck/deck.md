<!-- Generated from docs/deck/deck.template.md by scripts/brand.py. Edit the template, not this file. -->

<!-- theme: shiftboard -->
<!-- paginate: true -->
<!-- footer: "FixPath" -->
<!-- _class: title -->
<!-- _paginate: false -->

<div class="bar"></div>

<p class="kicker">Troubleshooting assistant, proof of concept</p>

# FixPath

<p class="tag">Every answer shows its source and the graph path behind it.</p>

<p class="meta">Customer: a fictional Tier-1 automotive supplier. Line 3, Singapore. Five machines, night shift.</p>
<p class="meta">A discovery plan, an MVP, and a working proof of concept.</p>
<p class="audience"></p>

<!--
Talk track: This is a 20 minute talk and a 10 minute demo. The plant is fictional: a Tier-1 automotive supplier, Line 3 in Singapore, with five machines. I built a working proof of concept so that we can talk about a real system and not a slide of boxes. The talk answers six questions: how to guide the customer to an MVP, which questions narrow the requirements, which data to use, how messy data becomes a clean graph, how the agents answer a technician, and how we know it works.
-->

---

<p class="kicker">Define the problem</p>

# Five places to look at a stopped machine

<div class="cols five">

<div class="card machine">

### Machine logs

<span class="big">5</span><span class="what">files, four date formats</span>

</div>

<div class="card past">

### Handover notes

<span class="big">90</span><span class="what">notes, shorthand and typos</span>

</div>

<div class="card fix">

### Spares sheets

<span class="big">2</span><span class="what">lists that disagree</span>

</div>

<div class="card cause">

### Maintenance notes

<span class="big">6</span><span class="what">files, free text</span>

</div>

<div class="card alarm">

### PDF manuals

<span class="big">4</span><span class="what">vendor manuals</span>

</div>

</div>

<p style="margin-top:44px;font-size:30px"><span class="chip">Assumption</span> Nobody has measured the time from "machine down" to the first correct action. The workshop sets that baseline: a stopwatch on ten real breakdowns.</p>

<!--
Talk track: At night a technician stands at a stopped machine. The cause, the fix steps, the part and its bin are spread across five kinds of source. In the plant folder I built there are 107 files: 5 machine logs, 90 shift handover notes, 2 spares lists, 6 maintenance files and 4 vendor manuals. The success measure I would propose is time to the first correct action. I do not have a baseline, and I will not make one up: that is the first thing the workshop measures. Every number on later slides is either from this simulated plant or from the eval results, and I say which.
-->

---
<!-- _class: roomy -->

<p class="kicker">Guide the customer: the workshop</p>

# A half-day workshop on site

<div class="cols wide-right">

<div>

### Who attends

Maintenance lead. Two or three technicians, one from nights. Production supervisor. IT and OT engineer. Quality engineer. EHS representative.

</div>

<div>

| Block | Output |
|---|---|
| Last five breakdowns | Map of where the time went |
| Questions and sources | Top 20 questions, source map |
| Open the folder together | Data inventory |
| Slice the MVP | One line, one persona, top failure modes |
| Success and limits | Success metrics, constraint list |

</div>

</div>

<!--
Talk track: To guide the customer to an MVP I would not start with a demo. I would start with a half-day workshop. The maintenance lead comes with two or three technicians, at least one from nights, plus a production supervisor, an IT and OT engineer, a quality engineer and the EHS representative. Five blocks. First we walk through the last five breakdowns: what they needed to know, where they looked, how long it took. Then sticky notes: the questions technicians ask and where each answer lives today. Then we open the shared folder together and rate each source for availability, quality and value. Then an impact and effort matrix plus a downtime Pareto chart slices the MVP. Last, we agree the success metric, the baseline method, the safety rules and the IT policy. Each block ends with one written output.
-->

---

<p class="kicker">Narrow the requirements</p>

# Questions that narrow the scope

<div class="cols three">

<div class="card cause">

### Problem

Which three machines cause the most downtime?

Where does breakdown time go: diagnosis, finding information, parts or approvals?

</div>

<div class="card machine">

### Users

Who troubleshoots at night, at what skill level?

What device do they carry? Do gloves, noise or language get in the way?

</div>

<div class="card fix">

### Data

Which source do technicians trust?

How current is the spares sheet? Who owns each source?

</div>

</div>

<div class="cols two" style="margin-top:18px">

<div class="card alarm">

### Constraints

Are cloud models allowed, or only on-prem? What must the assistant never say about safety? Are IT and OT networks separated?

</div>

<div class="card past">

### Success

Does "fixed faster" mean MTTR, time to first correct action or first-time fix rate? Is there a baseline, and who signs off the pilot?

</div>

</div>

<!--
Talk track: These are the questions I would bring, grouped in five. Problem: which three machines cause the most downtime, and where the time goes. Users: who works nights, at what skill, on what device, and whether gloves or noise matter. Data: which source technicians trust, how current the spares sheet is, who owns each source. Constraints: whether cloud models are allowed, what the assistant must never say about safety, whether IT and OT networks are separate. Success: whether fixed faster means MTTR, time to first correct action or first-time fix rate, whether a baseline exists, and who signs off the pilot. The answers set the MVP line on the next slides.
-->

---
<!-- _class: dense -->

<p class="kicker">Make assumptions visible</p>

# Assumptions, and how the workshop checks each

| Assumption | How the workshop validates it |
|---|---|
| The shop floor works in English, in Singapore | Ask directly, check the language of existing notes |
| The MVP covers one line with five machines | Pareto chart of downtime by machine |
| Technicians share a tablet on the floor | Observe one shift |
| Data arrives as a shared-folder export, with no MES, CMMS or historian access | IT and OT inventory session |
| Cloud models are allowed for the pilot | IT security sign-off |
| The assistant is read-only and writes no work orders | Agree it in the MVP scope |
| Lockout and tagout are mandatory for hydraulic and electrical work | EHS representative confirms the procedure list |
| The success metric is time to the first correct action | Stopwatch baseline on ten real breakdowns |

<!--
Talk track: I list eight assumptions and put the validation next to each, so a wrong assumption costs a conversation and not a rebuild. The ones I would worry about first: data arrives as a folder export with no MES or CMMS access, cloud models are allowed, and lockout is mandatory for hydraulic and electrical work. The last one is the metric: time to the first correct action, measured with a stopwatch on ten real breakdowns.
-->

---

<p class="kicker">Define the MVP</p>

# MVP: one line, one persona, read-only

<div class="cols three">

<div class="card fix">

### In

One line, five machines

One persona: the night-shift technician

Likely cause, fix steps, parts with stock and bin, similar past breakdowns, who fixed it last

Every answer cites its source

Lockout block, always shown when a fix needs it

</div>

<div class="card alarm">

### Out

Writing work orders

MES and CMMS integration

Photo input from HMI screens

On-prem models

Other lines

</div>

<div class="card machine">

### Success

Time to the first correct action, against the stopwatch baseline

Lockout recall of 100%

Thumbs up or down on each answer, stored as a score

</div>

</div>

<!--
Talk track: The MVP is deliberately small. One line, five machines, one persona: the night-shift technician at a stopped machine. The assistant is read-only. It gives the likely cause, the fix steps, the parts with stock and bin, similar past breakdowns and who fixed it last, and every answer cites its source. The lockout block is always shown when a fix needs it. Out of scope: writing work orders, MES and CMMS integration, photo input, on-prem models and other lines. They are on the roadmap, not in the MVP. Success: time to the first correct action against the baseline, lockout recall at 100 percent, and a thumbs up or down on every answer.
-->

---
<!-- _class: dense -->

<p class="kicker">Data inputs</p>

# Five sources, and what is wrong with each

| Source | Files | A real line from the folder |
|---|---|---|
| Machine logs | 5 | <code>2026-04-01T04:00:00Z HB ok</code><br><code>01/04/2026 06:45:00 \| ERR 200 \| PUMP START</code><br><code>1774994594;RB-551;GRIPPER PART-PRESENT SIGNAL LOST</code> |
| Shift handovers | 90 | <code>leak test stopped 0631 w/ VT-012, good parts failing leak test,</code><br><code>reference off. Recalibrate pressure senosr, tested ok.</code> |
| Spares sheets | 2 | <code>BRG7014ACD;Spindle angular contact;4;A04</code><br><code>SEAL-KIT-50 \| Main cylinder seal kit, 50 mm rod \| 3 \| pc \| B12</code> |
| Maintenance notes | 6 | <code>2026-04-14,PR-2,"press losing pressure mid-stroke, oil on the floor"</code><br><code>17/04/2026,Kessler 1,"ALM CL-210: coolant pressure dropping"</code> |
| PDF manuals | 4 | <code>Plant ref: PROC-RW-01 \| Lockout required: yes \| Time: 40 min</code> |

<!--
Talk track: These are the suitable data inputs, and each has a real flaw. The logs use four date formats and the same alarm written three ways. The handover notes are shorthand with typos: "senosr", "w/", "tmr". The spares data is two lists with part numbers written differently, such as BRG7014ACD with no dashes, and they disagree: the old list shows one seal kit, the October workbook three. The maintenance export calls the same press "PR-2" and "HP-01", and calls CNC-01 "Kessler 1". The manuals are clean PDFs, and they carry the lockout warnings and the alarm tables. I chose these because a shared-folder export is what most plants can hand over in week one, without touching MES or CMMS.
-->

---

<p class="kicker">Ontology</p>

# Nine node types, twelve links, five colour families

<div class="diagram">

![w:1130](diagrams/ontology.svg)

</div>

<p class="small" style="margin-top:6px">Every link reads as a sentence. A line has machines. A machine raised an alarm. An alarm usually means a root cause. A root cause is fixed by a fix. A fix needs a spare part. A past breakdown happened on a machine, started with an alarm, was caused by a root cause and used a part. Source backs each one.</p>

<!--
Talk track: This is the ontology: nine node types and twelve relations, defined once in code and exported to the web app. The colour families are Where, Problem, Fix, History and Source, and each type also has a glyph and a label, so colour is never the only signal. The idea is that every link reads as a sentence, so one path through the graph is the answer. Example: HP-01 raised E-217. E-217 usually means main cylinder seal wear. That is fixed by PROC-HP-04. PROC-HP-04 needs a SEAL-KIT-50, and bin B-12 holds three. Every node and every link carries evidence: the source file, the quote, the page and a confidence. A new node type is one edit to one file.
-->

---

<p class="kicker">From messy to structured</p>

# One handover line becomes graph facts

<div class="cols wide-right">

<div class="shot">

![h:470](images/pipeline-handover-crop.png)

</div>

<div>

**INC-2026-016** is a past breakdown.

It **happened on** LT-01.

It **started with** VT-012.

It **was caused by** pressure sensor drift. The linker (JEV) picked that cause.

<p class="small muted">Evidence: the highlighted line, read by the text reader at 0.80. The maintenance export row for the same breakdown backs it at 0.90, so two sources merged into one node.</p>

</div>

</div>

<!--
Talk track: This is the Pipeline view of the app on a real note from the folder. The highlighted line is the only part that matters: "leak test stopped 0631 w/ VT-012, good parts failing leak test, reference off. Recalibrate pressure senosr, tested ok". The text reader, Sol, turned it into one past breakdown with two links: it happened on LT-01 and started with VT-012, each at 0.80 confidence with the quote attached. The maintenance export has a row for the same breakdown, at 0.90. The resolver treated them as one incident and merged them. The linker then asked JEV which root cause this breakdown had, and linked it to pressure sensor drift. Nothing here is a model free-writing a graph: every fact has a quote you can click.
-->

---

<p class="kicker">Agents: ingestion</p>

# The ingestion crew

<div class="diagram">

![w:1136](diagrams/ingestion-crew.svg)

</div>

<p class="legend"><span class="key code"></span>code <span class="key jev"></span>JEV, fast typed decisions <span class="key llm"></span>Sol reads and writes</p>

<p class="small" style="text-align:center;margin-top:4px">107 files, 164 nodes, 442 links, 906 evidence quotes, 37 items in the review queue.</p>

<!--
Talk track: One LangGraph graph. A router sends each file to an extractor by file type: code parsers for logs, spares sheets and the maintenance export; Sol for handovers and loose notes; Sol for the PDF manuals, which also produce text chunks for search. The files fan out and run in parallel. The resolver merges mentions: rules first, fuzzy candidates next, then JEV answers one yes or no question per pair. Parts and machines merge from 0.70, and from 0.50 they go to review. Breakdowns merge from 0.90, with a veto when a direct answer contradicts. The linker asks JEV for each breakdown's root cause and links it from 0.75. The validator checks the ontology rules in code. The loader writes nodes, links, evidence quotes and the text chunks with local embeddings. The last full run loaded 107 documents, 164 nodes, 442 links and 906 evidence quotes, and left 37 items for a person to look at: 12 merges, 4 conflicts, 1 unresolved and 20 validator flags.
-->

---

<p class="kicker">Agents: troubleshooting</p>

# The troubleshooting crew

<div class="diagram">

![w:1136](diagrams/troubleshooting-crew.svg)

</div>

<p class="legend"><span class="key code"></span>code <span class="key jev"></span>System 1: JEV decides <span class="key llm"></span>System 2: Sol and Terra read and write</p>

<!--
Talk track: This is the second LangGraph graph, and it is where the two systems split the work. System 1 is JEV: fast typed decisions with a calibrated confidence. Triage asks five questions in one request: which machine, which alarm, which root cause, what intent, and whether it is safety critical. If it is confident it acts. If it is in the middle, it confirms with chips. If it is unsure, it asks back, for example when someone says Line 4 and the plant has no Line 4. Two retrieval agents then run in parallel. The graph agent, Terra, has six read-only graph tools. The manual agent, Terra, searches the manuals by keyword and by vector and fuses the ranks. System 2 writes: the synthesizer, Sol, produces a short summary and cited claims, and nothing else. The verifier, JEV, asks one question per claim: does the quote support it? Unsupported claims move to unverified and never reach the card. Then code assembles the card: every fact on it is copied from the graph, and the lockout block comes from the machine's own manual paragraph and the procedure warnings. No model can drop it.
-->

---

<p class="kicker">Agents: one question</p>

# One question, end to end

<div class="diagram">

![h:480](diagrams/question-sequence.svg)

</div>

<!--
Talk track: One question from the golden set: HP-01 stopped with E-217, oil under the press. Triage reads machine HP-01 at 1.00 and alarm E-217 at 1.00, but root cause only at 0.51 for main cylinder seal wear, so it does not guess: it shows the card and offers chips for the other two causes. In live mode a tap on a chip re-runs the question. The graph agent and the manual agent run in parallel. The synthesizer writes the summary and the claims. The verifier checks each claim against its quote. Assemble builds the card: cause with likelihood, the fix PROC-HP-04, SEAL-KIT-50 with three on hand in bin B-12, three similar past breakdowns, and the lockout block. In the demo the six suggested questions answer in under a second from the saved results, with the path lighting up on the graph, and a question you type streams live for about 14 seconds.
-->

---

<p class="kicker">Validation</p>

# What holds, and what it rests on

<div class="tiles">

<div class="tile"><span class="label">Lockout recall</span><span class="value">19 <small>of 19</small></span><span class="sub">fixes that need lockout, all warned</span></div>

<div class="tile"><span class="label">Root cause, top 1</span><span class="value">20 <small>of 20</small></span><span class="sub">questions with a known cause</span></div>

<div class="tile"><span class="label">Traps passed</span><span class="value">5 <small>of 5</small></span><span class="sub">no Line 4, vague, off topic, conflicting notes, zero stock</span></div>

<div class="tile"><span class="label">Triage, acted on</span><span class="value">22 <small>of 22</small></span><span class="sub">all five fields right; 29 of 32 on all 32 questions</span></div>

<div class="tile"><span class="label">Citation precision</span><span class="value">96 <small>of 102</small></span><span class="sub">kept claims judged supported (0.94)</span></div>

<div class="tile"><span class="label">One live answer</span><span class="value">$0.01</span><span class="sub">median cost, 13.6 s median, n = 25</span></div>

</div>

<div class="limits">
<div class="head">Limits</div>

- The 25 golden questions re-ask recorded incidents, so these are not hold-out results.
- Over-warning (1 of 1) and the safety false-positive rate (0 of 1) each rest on one negative case.
- Live latency and cost were measured before the last code-only fixes.

</div>

<!--
Talk track: Five validation layers, run against the hidden ground truth that the pipeline never reads. What holds: lockout recall is 19 of 19. Root cause top 1 is 20 of 20 and top 3 is 20 of 20. All five traps pass: the Line 4 question, a vague symptom, an off-topic request, conflicting notes and a zero-stock part. Triage got all five fields right on the 22 questions it acted on, and 29 of 32 across all 32. Citation precision is 96 of 102 kept claims. A live answer costs about a cent at the median, 13.6 seconds. Now the limits, and they matter. The golden questions re-ask incidents whose text is in the ingested data, so this is not hold-out: it shows the system answers questions whose records it has seen, not how it generalises. The over-warning rate and the safety false-positive rate each rest on one negative case, so I do not claim them. And the latency and cost were measured before the last code-only fixes. Extraction also has soft spots I would not hide: the shift is missing on 18 of 60 breakdowns, and the alarm counts match exactly on 13 of 26 log roll-ups, although 98 percent of the raised events are captured.
-->

---
<!-- _class: dense -->

<p class="kicker">Validation: the decider bake-off</p>

# JEV and Decisions: comparable accuracy

<div class="cols wide-left">

<div>

| Task | n | JEV | Decisions |
|---|---|---|---|
| Pick the root cause | 60 | 100% | 100% |
| Same part? | 41 | 100% | 100% |
| Same incident? | 111 | 98% | 96% |
| Triage fields | 32 | 98% | 98% |
| Verify a claim | 102 | 95% | 93% |

<p class="small muted">Each backend at its own best gate. No task is led by either backend (sign test). 0 led for JEV, 0 for Decisions, 5 ties.</p>

</div>

<div>

<div class="tile"><span class="label">Cost per 1,000 asks</span><span class="value">$0.04 <small>vs $0.07</small></span><span class="sub">JEV vs Decisions, 266 asks each</span></div>

<div class="tile"><span class="label">Triage latency, p50</span><span class="value">0.27 <small>s vs 0.41 s</small></span><span class="sub">p95: 0.39 s vs 0.61 s, n = 30 asks</span></div>

</div>

</div>

<div class="limits">
<div class="head">Limits</div>

- The gates and prompts were tuned on JEV. The best gates are picked on the same questions they are scored on, so they are in sample.
- One LLM judge labels the verify claims. Latency is one run of 30 triage asks per backend, not interleaved.

</div>

<!--
Talk track: JEV is a small vendor's model, so I put a second backend behind the same interface: the OpenAI Decisions API. Both answer the same questions with the same typed answers and a confidence. I ran the same five tasks through both: root cause linking, same part, same incident, triage and claim verification. The honest reading is comparable accuracy. Once each backend gets its own best gate and a sign test is applied, neither leads any task: 0 for JEV, 0 for Decisions, 5 ties. At the gate that was tuned for JEV, same-incident looks better for JEV, but that gate favours it. What differs is cost and speed: about 4 cents per 1,000 asks for JEV against 7 cents for Decisions, and a median of 0.27 seconds against 0.41 for triage. The caveats are on the slide: the gates and prompts were tuned on JEV, so JEV is read with that bias in its favour; the best gates are in sample; one LLM judge labels the verify claims; and latency is one non-interleaved run of 30 asks. The fallback exists so that an outage or a vendor risk does not stop the plant.
-->

---
<!-- _class: dense -->

<p class="kicker">Next</p>

# Roadmap, risks and how we keep validating

<div class="cols two">

<div>

### Risks and mitigations

| Risk | Mitigation |
|---|---|
| A model invents a fix step | Cited claims, JEV verifier, citation precision measured |
| A wrong merge joins two machines | Confidence gates, review queue |
| A safety warning goes missing | Code builds the block, recall must stay 100% |
| Stock data is stale | Shows the stock date, flags sources that disagree |
| A decider vendor fails | Second backend, replay mode |

</div>

<div>

### After the proof of concept

<div class="small">

Shadow-mode pilot on real breakdowns

Time to first correct action against the stopwatch baseline

Thumbs up or down on every answer, stored as a score

Expert review of the merge queue

</div>

### Roadmap

<div class="small">

Graph database at scale. Asset Administration Shell mapping. CMMS and MES integration. Work orders written back. Photos from HMI screens. On-prem models.

</div>

</div>

</div>

<!--
Talk track: Risks first, with the measure that watches each. A model could invent a fix step: every claim cites a quote, the verifier drops unsupported ones, and layer 4 measures citation precision. A wrong merge could join two machines: gates, the review queue, and the calibration chart. A safety warning could go missing: code builds the block from the machine's own manual, and recall must stay at 100 percent. Stock data goes stale: the card shows the stock date and flags sources that disagree. A decider vendor could fail: the second backend and the replay mode. After the proof of concept, validation moves to real use: a shadow-mode pilot where the assistant answers beside the technician and nobody acts on it, time to first correct action against the stopwatch baseline, a thumbs up or down on every answer, and expert review of the merge queue. The roadmap is a graph database at scale, mapping to the Asset Administration Shell, CMMS and MES integration, writing work orders back, photo input and on-prem models.
-->

---
<!-- _class: dense -->

<p class="kicker">Next</p>

# Maintenance contracts and SLAs

Link each machine to its contract terms: response time, uptime target and the parts covered.

Measure uptime against the target from the graph, which already records each breakdown's date and downtime.

Measure response time from when the alarm was raised to when the technician arrived.

Send the customer a monthly service report where every number links back to a recorded breakdown.

Warn before a breach: use the upkeep predictions to flag a part that is due to fail on a machine whose uptime is near its target.

<!--
Talk track: The graph already holds dates and downtime, so contract terms are a small step. The demo shows example terms next to the uptime the graph measured. HP-01 is at 97.9 percent against a 98 percent target.
-->

---

<p class="kicker">Demo</p>

# Demo: ten minutes, live

<div class="cols wide-left">

<div class="shot">

![w:700](images/ask-hp01.png)

</div>

<div>

1. Ask: HP-01 stopped with E-217
2. The path lights up, and a magnet opens its source
3. The lockout block
4. Line 4: the clarify
5. The zero-stock gripper
6. Pipeline: drop a new handover
7. Evals: scores and limits

<p class="small muted">Runs with no keys too: <code>make api-replay</code> and <code>make web</code>. The repository README has setup, the data and the eval summary.</p>

</div>

</div>

<!--
Talk track: Now the demo. I start with the HP-01 question and show the path from alarm to bin, then open a source. Then the lockout block, the Line 4 question that asks back, and the gripper question where the part is out of stock. Then the Pipeline view, where I drop a new handover note and watch it join the graph. Last, the Evals view with its limits. If an API is down, replay mode serves the saved answers with no keys. Setup and the data are in the README.
-->

---
<!-- _class: dense -->

<p class="kicker">Backup</p>

# Schema detail: what is in the graph

| Type | Label | Glyph | Family | In the graph |
|---|---|---|---|---|
| Line | Production line | factory | Where | 1 |
| Asset | Machine | cog | Where | 5 |
| Component | Component | bolt | Where | 15 |
| AlarmCode | Alarm | siren | Problem | 20 |
| FailureMode | Root cause | unlink | Problem | 11 |
| Procedure | Fix | wrench | Fix | 12 |
| Part | Spare part | package | Fix | 39 |
| Incident | Past breakdown | history | History | 61 |
| Document | Source | file-text | Source | 107 |

<p class="small muted">Links in the graph: 442. Largest groups: used 105, caused by 61, happened on 61, started with 54, fits 51.</p>

<!--
Talk track: Backup. The schema as built: nine types with the label, glyph and family the web app shows. Counts are from the graph file, and documents are the 107 raw files. The incident count is 61, one more than the 60 in the hidden truth: the eval counts that as one false positive. Link counts come from the same file. Properties on each type, such as stock date and bin on a part, or likelihood and support on the alarm-to-cause link, are in the ontology file.
-->

---
<!-- _class: dense -->

<p class="kicker">Backup</p>

# Decider questions and gates

| Role | Question to JEV | Gate |
|---|---|---|
| Triage | Five in one ask: machine, alarm, root cause, intent, safety | Act from 0.75, confirm chips from 0.40 |
| Merge a part or machine | Same entity? | Merge from 0.70, review from 0.50 |
| Merge a breakdown | Same incident? | Merge from 0.90, review from 0.50, veto on a contradicting answer |
| Link | Which root cause for this breakdown? | Link from 0.75, review from 0.40 |
| Verify | Does the quote support this claim? | Keep when supported plus partly reaches 0.50 |

<p class="small muted">The gates are tuned on eval layers 2 and 3. For same entity, every true pair scored 0.72 or more and the highest wrong pair 0.44.</p>

<!--
Talk track: Backup. Each fast decision is a typed question with a probability. Triage asks five questions at once. The resolver asks same entity for parts and machines, and same incident for breakdowns. The linker asks for the root cause. The verifier asks whether a quote supports a claim, and keeps it when the probability of supported plus partly reaches 0.50. Gates started at 0.9 and 0.5 and were tuned on the eval layers: for same entity, every true pair scored 0.72 or more and the highest wrong pair 0.44, so I merge from 0.70. Same incident stays at 0.90 with a veto, because a direct contradicting answer outranks a similar-looking record.
-->

---
<!-- _class: dense -->

<p class="kicker">Backup</p>

# Cost model

| Item | Measured |
|---|---|
| Whole project, every call booked | $0.98 in the spend ledger |
| Last full ingest of 107 files | $0.0026, 83 paid calls of 302 |
| One live answer | $0.0098 median, $0.0114 p95 (n = 25) |
| Decider, per 1,000 asks | JEV $0.040, Decisions $0.070 |
| Hard stop | $40, set in the spend ledger |

<p class="small muted">Everything replays from saved answers, so a rerun costs nothing. Live cost was measured before the last code-only fixes.</p>

<!--
Talk track: Backup. Every model and decider call is booked in a ledger with a hard stop at 40 dollars. The whole build and all the evals cost 98 cents, because every answer is cached and a rerun replays from the cache. The last full ingest of 107 files cost a quarter of a cent in paid calls. One live answer costs about a cent at the median, and the median is the right number: the mean is lower because the trap questions stop at triage. The decider costs per 1,000 asks come from the bake-off. At a plant scale these numbers would be re-measured on the customer's own volume, and the ledger and the hard stop go with the system.
-->

---
<!-- _class: dense -->

<p class="kicker">Backup</p>

# Failure handling

| What goes wrong | What the system does |
|---|---|
| Not sure which machine, alarm or cause | Confirm with chips, or ask back. Never guess |
| A place the plant does not have (Line 4) | Says so and offers the closest machine |
| Not a maintenance question | Says so and names who to ask |
| A claim the quote does not support | Moves to unverified, never shown on the card |
| A part with zero stock | Shows "0 on hand" with the lead time |
| Two notes disagree | Goes to the review queue as a conflict |
| A model step fails | The card still shows the graph facts and a note |
| Spend reaches the budget | Hard stop, no further call starts |
| A decider API is down | Second backend, or replay mode for the demo |

<!--
Talk track: Backup. Uncertainty is a designed outcome, not an error. If triage is unsure it confirms or asks back. A question about Line 4 gets a clarify that offers RW-01 on Line 3. A claim the quote does not support moves to unverified and is never shown. Zero stock is shown plainly with the lead time. Conflicting notes go to the review queue: 4 conflicts are waiting there now. If a model step fails, the card is still built in code from the graph, with a note saying what is missing, and the lockout block is still there. If the budget is reached, no further call starts. For the demo, replay mode serves saved answers with no keys.
-->
