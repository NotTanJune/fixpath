import { useState, type FormEvent } from "react";

type Step = "ask" | "fixed" | "form" | "added";

/** The mock of a technician's report under a finished answer. Nothing is sent or saved: a report
 * is added to the graph panel of this answer only. */
export function WriteBack({
  onSubmit,
}: {
  onSubmit: (input: { cause: string; part: string; note: string }) => void;
}) {
  const [step, setStep] = useState<Step>("ask");
  const [cause, setCause] = useState("");
  const [part, setPart] = useState("");
  const [note, setNote] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!cause.trim()) return;
    onSubmit({ cause: cause.trim(), part: part.trim(), note: note.trim() });
    setStep("added");
  };

  if (step === "fixed") {
    return <p className="wb-done">Thanks. In the live system this confirms the fix on the graph.</p>;
  }
  if (step === "added") {
    return <p className="wb-done">Added to the graph for this session. Demo only, not saved.</p>;
  }
  if (step === "form") {
    return (
      <form className="wb-form" onSubmit={submit} aria-label="Report what was wrong">
        <label>
          Cause
          <input value={cause} onChange={(e) => setCause(e.target.value)} autoComplete="off" required />
        </label>
        <label>
          Part used
          <input value={part} onChange={(e) => setPart(e.target.value)} autoComplete="off" />
        </label>
        <label>
          Note
          <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} />
        </label>
        <div className="wb-actions">
          <button type="submit" className="wb-submit">
            Submit
          </button>
          <button type="button" className="wb-btn" onClick={() => setStep("ask")}>
            Cancel
          </button>
        </div>
      </form>
    );
  }
  return (
    <div className="wb-actions" role="group" aria-label="Was the answer right?">
      <button type="button" className="wb-btn" onClick={() => setStep("fixed")}>
        Fixed it
      </button>
      <button type="button" className="wb-btn" onClick={() => setStep("form")}>
        Something else was wrong
      </button>
    </div>
  );
}
