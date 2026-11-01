/** A question about an issue the graph has never seen, with the canned result of asking it and the
 * pre-filled report a technician would send. Replay mode only: nothing here calls the API. */
export interface NovelRecord {
  machine: string; // asset id, or "" when the question names no known machine
  machineLabel: string; // e.g. "Leak tester" or "Transfer conveyor (not in the graph)"
  symptom: string; // what the technician saw
  cause: string; // what they found
  fixSteps: string[]; // what they did, in order
  parts: { id: string; name: string; qty: number }[];
  downtimeMin: number;
  technician: string;
}

export interface NovelCase {
  question: string; // exact text shown as a suggestion
  confidence: number; // 0.1 to 0.3
  nearest: { label: string; why: string }[]; // up to 2 nearest known things and why they do not fit
  record: NovelRecord; // the pre-filled capture form
  adds: { type: string; id: string; label: string; link?: string }[]; // what the merge would add
}

export const NOVEL: NovelCase[] = [
  {
    question: "LT-01 touchscreen freezes after a few test cycles",
    confidence: 0.18,
    nearest: [
      {
        label: "LT-01 pressure sensor drift",
        why: "Drift changes the readings. It does not freeze the screen.",
      },
    ],
    record: {
      machine: "LT-01",
      machineLabel: "Leak tester",
      symptom: "The touchscreen stops responding after 3 to 5 test cycles. A power cycle clears it.",
      cause: "HMI cooling fan seized, so the panel overheats",
      fixSteps: [
        "Lock out the tester.",
        "Replace the HMI cooling fan.",
        "Clean the panel vents and run 10 test cycles.",
      ],
      parts: [{ id: "FAN-HMI-60", name: "HMI cooling fan, 60 mm", qty: 1 }],
      downtimeMin: 95,
      technician: "Night shift, B. Tan",
    },
    adds: [
      {
        type: "FailureMode",
        id: "FM-HMI-FAN-SEIZED",
        label: "HMI cooling fan seized",
        link: "LT-01 has this cause",
      },
      { type: "Procedure", id: "PROC-LT-NEW-1", label: "Replace HMI cooling fan" },
      { type: "Part", id: "FAN-HMI-60", label: "HMI cooling fan, 60 mm" },
      { type: "Incident", id: "INC-NEW-001", label: "LT-01 touchscreen freeze" },
    ],
  },
  {
    question: "CNC-01 chips welding to the tool, rough surface finish",
    confidence: 0.22,
    nearest: [
      {
        label: "CNC-01 spindle bearing wear",
        why: "Worn bearings cause vibration marks. Welded chips point to the cutting conditions.",
      },
    ],
    record: {
      machine: "CNC-01",
      machineLabel: "Machining centre 1",
      symptom: "Aluminium chips weld to the insert. The finish on the bore is rough.",
      cause: "Coolant concentration too low, below 4 percent",
      fixSteps: [
        "Measure coolant concentration with the refractometer.",
        "Top up concentrate to 7 percent.",
        "Replace the insert and recut a test part.",
      ],
      parts: [{ id: "INS-AL-12", name: "Aluminium cutting insert", qty: 2 }],
      downtimeMin: 60,
      technician: "Day shift, R. Kumar",
    },
    adds: [
      {
        type: "FailureMode",
        id: "FM-COOLANT-CONCENTRATION-LOW",
        label: "coolant concentration low",
        link: "CNC-01 has this cause",
      },
      { type: "Procedure", id: "PROC-CNC-NEW-1", label: "Restore coolant concentration" },
      { type: "Part", id: "INS-AL-12", label: "Aluminium cutting insert" },
      { type: "Incident", id: "INC-NEW-002", label: "CNC-01 welded chips" },
    ],
  },
  {
    question: "RW-01 vacuum cup leaves marks on the parts",
    confidence: 0.27,
    nearest: [
      {
        label: "RW-01 gripper finger wear",
        why: "The gripper fingers are a different end effector from the vacuum cup.",
      },
    ],
    record: {
      machine: "RW-01",
      machineLabel: "Robot workcell",
      symptom: "Round marks on finished parts where the robot picks them.",
      cause: "Vacuum cup lip hardened and cracked",
      fixSteps: [
        "Stop the cell and lock out.",
        "Replace both vacuum cups.",
        "Check the vacuum level reads at least 0.6 bar.",
      ],
      parts: [{ id: "CUP-VAC-30", name: "Vacuum cup, 30 mm", qty: 2 }],
      downtimeMin: 40,
      technician: "Day shift, S. Lim",
    },
    adds: [
      {
        type: "FailureMode",
        id: "FM-VACUUM-CUP-HARDENED",
        label: "vacuum cup hardened",
        link: "RW-01 has this cause",
      },
      { type: "Component", id: "RW-01-VACUUM-CUP", label: "Vacuum cup" },
      { type: "Part", id: "CUP-VAC-30", label: "Vacuum cup, 30 mm" },
      { type: "Incident", id: "INC-NEW-003", label: "RW-01 vacuum cup marks" },
    ],
  },
  {
    question: "Transfer conveyor between CNC-02 and LT-01 jams",
    confidence: 0.12,
    nearest: [],
    record: {
      machine: "",
      machineLabel: "Transfer conveyor (not in the graph)",
      symptom: "Parts stack up at the transfer point and the conveyor stops.",
      cause: "Drive belt stretched and slipping",
      fixSteps: [
        "Lock out the conveyor.",
        "Tension the drive belt to spec.",
        "Run 20 parts through and watch the transfer.",
      ],
      parts: [{ id: "BELT-CV-1200", name: "Conveyor drive belt", qty: 1 }],
      downtimeMin: 75,
      technician: "Night shift, B. Tan",
    },
    adds: [
      { type: "Machine", id: "CV-01", label: "Transfer conveyor", link: "on Line 3" },
      { type: "FailureMode", id: "FM-BELT-STRETCHED", label: "drive belt stretched" },
      { type: "Part", id: "BELT-CV-1200", label: "Conveyor drive belt" },
      { type: "Incident", id: "INC-NEW-004", label: "Transfer conveyor jam" },
    ],
  },
];

const fold = (s: string) => s.trim().toLowerCase();

/** The novel case a question asks, matched on the exact text ignoring case and outer spaces. */
export function novelCase(question: string): NovelCase | null {
  const q = fold(question);
  return NOVEL.find((c) => fold(c.question) === q) ?? null;
}
