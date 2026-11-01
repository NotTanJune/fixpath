/** A breakdown a technician reports from the answer screen. It is held in the page only. */
export interface TechBreakdown {
  id: string;
  label: string;
  machine: string;
  cause: string;
  part: string;
  note: string;
  source: "Technician report";
}

export interface TechInput {
  machine: string;
  cause: string;
  part: string;
  note: string;
}

/** The nth breakdown of this session, as the graph panel draws it. */
export function newBreakdown(input: TechInput, n: number): TechBreakdown {
  const id = `INC-TECH-${String(n).padStart(3, "0")}`;
  return {
    id,
    label: `${id} (technician, not reviewed)`,
    machine: input.machine,
    cause: input.cause,
    part: input.part,
    note: input.note,
    source: "Technician report",
  };
}
