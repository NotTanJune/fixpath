# Demo script: ten minutes

The demo follows the deck's last slide. It runs the web app in a browser at 1440 by 900. It works in two modes:

- Live: keys in `.env`, started with `make api` and `make web`. Any question gets a live answer, and a new handover note can be dropped in.
- Replay: no keys, started with `make api-replay` and `make web`. The top bar says "Replay". The six suggested questions answer from saved results. A typed question that is not saved gets a plain error. The drop zone is switched off.

Everything below works in both modes except where it says "live only".

## Before you start

Rehearsal checklist. Do it 30 minutes before, and again 5 minutes before.

- [ ] Keys present (live only): `.env` has the model key and the decider key. Do not open the file on screen. `curl -s localhost:8000/api/health` shows `"openai": true` and `"typesafe": true`, and `"replay_mode": false`.
- [ ] Budget left: the top bar shows the spend, for example "$0.98 of $40". The hard stop is $40. A live answer costs about one cent.
- [ ] Servers: the API on port 8000 and the web app on port 5173, both running. Nothing else on those ports. Open `http://localhost:5173/#/ask`.
- [ ] Mode: the top bar says "Live" for the live demo, "Replay" for the fallback. Check it before you speak.
- [ ] Browser zoom at 100 percent, window at 1440 by 900 or a full screen on the projector. Close other tabs and turn off notifications.
- [ ] Network: reachable from the venue, if you run live. If the venue network blocks the model APIs, use replay mode and do not apologise for it.
- [ ] Embedding model: the first run on a machine downloads a 64 MB embedding model into `.cache/fastembed`, so it needs the network once. Do this at the desk, not at the venue: start the API and ask one suggested question.
- [ ] Suggested questions: the six replay from the committed caches in under a second, even in live mode, so there is nothing to warm. Ask each once and check that it ends with the path drawn.
- [ ] Typed question (live only): rehearse one that is not among the six, for example a suggested question with one word changed. It streams for about 14 seconds and costs about $0.01. Do this once before the talk, so the audience never waits on a first run you have not seen.
- [ ] The new handover note (live only) saved as a file with a name that has not been used yet, text below.
- [ ] Data reset (live only): the live drop changes `data/kg.sqlite`, `data/cache/llm_cache.sqlite` and `data/cache/decisions.sqlite`. It adds the dropped file under `data/raw/handovers/` and writes `data/pipeline/last_file.json`. It leaves `data/pipeline/latest.json` and `data/evals/results/latest.json` alone. A dropped note takes about 5 seconds and costs about half a cent. Restore before every rehearsal, because a file name that already exists is refused with a 409:

      git checkout -- data/kg.sqlite data/cache/llm_cache.sqlite data/cache/decisions.sqlite && rm -f data/pipeline/last_file.json data/raw/handovers/<demo file>

  Replace `<demo file>` with the name you saved, for example `handover_demo_01.txt`. Run `git status` afterwards: it should show nothing under `data/`.

The note to drop in (live only). Save it as `handover_demo_01.txt`. Restore first (see above), or use a new name, because an existing name is refused:

    Shift handover 2026-10-29 C
    From: Arun   To: Jia Hui

    - CNC 2 SP-104 again at 0215, grinding noise from the spindle. Swapped the front bearing set, warm up ok. Down approx 3h.

    Signed Arun

## Minute by minute

### 0:00 to 1:00. Frame it

Show the Ask view. Point at the top bar: the mode badge and the spend.

Say: "A night-shift technician is at a stopped press. Everything you will see answers from a graph built out of 107 messy files, and every fact on screen links back to its source."

### 1:00 to 2:30. The HP-01 question

Click the first suggested question: **HP-01 stopped with E-217, oil under the press**. It arrives in under a second, in live mode too, because the six suggested questions replay from the committed caches. A typed or changed question streams for about 14 seconds.

Say: "Five steps run: triage, a graph agent and a manual agent in parallel, a synthesizer, a verifier. The agent trace at the bottom right shows each one with its model."

When the answer lands, point at the line under the question: Machine HP-01 1.00, Alarm E-217 1.00, Root cause main cylinder seal wear 0.51.

Say: "It is sure about the machine and the alarm. It is only half sure about the cause, so it does not guess. It shows the answer and offers the other two causes as chips." Live only: tap a chip to re-run the question. In replay the chips are greyed out and say "Needs live mode".

### 2:30 to 3:30. The path, and one source

Point at the row of five tiles: Alarm, Root cause, Fix, Spare part, Bin. On the right the graph draws the same path, with a doubled blue line.

Say: "This is the path to the fix: E-217 usually means main cylinder seal wear, fixed by PROC-HP-04, which needs SEAL-KIT-50, and bin B-12 holds three."

Click the **SEAL-KIT-50** tile. A source sheet slides in from the right. It shows the manual page (BH-250 service manual, page 5) with the quote highlighted, and a second tab for the spares workbook row. Close it with the X.

Say: "A model did not write that. Code copied it from the graph, and this is the file it came from."

### 3:30 to 4:30. The lockout band

Scroll to the red **Lockout required (PROC-HP-04, PROC-HP-06)** band. Point at the manual links under it.

Say: "This block is built from the machine's own manual paragraph and the procedures' warnings. No model writes it, so no model can drop it. Across 19 fixes that need lockout, it was shown 19 times."

### 4:30 to 5:30. Line 4: the clarify

Click **Line 4 robot is down**.

Say: "There is no Line 4 in this plant. It does not invent one. It asks: did you mean RW-01 on Line 3? The chips carry its confidence. Asking back is a normal outcome, not an error." Point at the red lockout band that still appears: "It also kept the safety block, because it could not rule out a hazard."

### 5:30 to 6:30. The zero-stock gripper

Click **RW-01 gripper keeps dropping parts, RB-551**. Point at the Bin tile: "short: 0 on hand".

Say: "The fix needs two GRP-FNG-12. The bin is D-08 and there are none, with a 14 day lead time. The technician finds that out before walking to the store."

### 6:30 to 7:30. Pipeline: from file to facts

Click **Pipeline**. In the source list, open **HO 05-05 night shift.txt**. The highlighted line in the note is the part that was read.

Say: "This is how the graph was built. The note is typos and shorthand. One line became one past breakdown that happened on LT-01 and started with VT-012, each fact with its quote and a confidence." Click the **Merges** tab and the **Review** tab: "The medium-confidence calls wait here for a person."

### 7:30 to 9:00. Drop a new handover note (live only)

Drag `handover_demo_01.txt` onto the drop zone at the top left. The steps stream in. When the run ends, say what changed: a new past breakdown on CNC-02 with SP-104.

Say: "Same pipeline, one file. It costs a fraction of a cent." Then go to Ask and type: `CNC-02 spindle alarm SP-104, grinding noise`. The new breakdown shows among the similar past breakdowns. Its row shows no technician. Say why if asked: the From line of a handover names the shift lead, not the person who did the fix, so the system leaves the technician empty instead of guessing.

If the demo is in replay mode: the drop zone says "Ingest needs keys". Say so, and open another note instead, for example **HO 06-12 night shift.txt**, to show the extraction. Do not try to work around it.

### 9:00 to 10:00. Evals, and the limits

Click **Evals**. Point at the first tiles: lockout recall 19 of 19, root cause top 1 20 of 20, traps 5 of 5, citation precision 96 of 102.

Say: "These are measured against a hidden ground truth the pipeline never reads. Now the limits, and they are printed at the top of the page: the golden questions re-ask incidents whose text is in the data, so these are not hold-out results. Some rates rest on one negative case and the page says 'too few to judge'." Scroll down past "Does it know when it is unsure" to the **JEV against Decisions** table: "Comparable accuracy. JEV is cheaper per thousand asks and faster at the median. The gates were tuned on JEV, so read it with that in mind."

Close: go back to the last slide and take questions.

## If something fails

| What fails | What to do |
|---|---|
| The model API is down or slow | Stop the live API, run `make api-replay`, reload the page. The badge changes to "Replay". Use the six suggested questions only, and say you are replaying saved answers. |
| The decider (JEV) is down | Set `DECIDER=decisions` in `.env`, restart `make api`, and run the demo as above. The question and answer types are the same on both backends. If that fails too, use replay mode. |
| The live ingest fails | Say what the message says. Move on to the Pipeline view with a note that is already in the graph. |
| A typed question gets an error in replay mode | That is expected. Use a suggested question. |
| The budget is reached | The API refuses further calls and the top bar shows the spend. Switch to replay mode. |
| A port is already in use | Stop what holds it (`lsof -i :8000`, `lsof -i :5173`), then start again. Never start the API on another port in the middle of the talk. |
| The browser shows a blank page | Reload. If the API is down, the top bar shows no mode. Start `make api-replay` as the quickest recovery. |

## Questions you may get

- "Is this tested on data it has not seen?" No. Say it plainly, as the Evals page does, and say the shadow-mode pilot is the answer.
- "Why two decision backends?" A vendor risk. The bake-off shows comparable accuracy, so the second backend is a real fallback.
- "What stops it giving a wrong fix step?" Every claim cites a quote, a verifier drops unsupported claims, and the card's facts are copied from the graph by code.
