"""Write the notes a plant floor keeps about its incidents.

The notes are shift handovers, a maintenance log export and a few loose notes. They
are short and untidy on purpose, with nicknames, shorthand and a few typos.

render_notes() only runs the steps in order. The other notes_ and render_ modules in
this folder do the work: which incidents go where, the wording, the handovers, and
the export with the loose notes.
"""

import random
from pathlib import Path

from app.generator.mentions import Mentions, merge
from app.generator.notes_incidents import incidents_of, place
from app.generator.render_handovers import write_handovers
from app.generator.render_maintenance import write_export, write_loose_notes
from app.generator.world import World


def render_notes(world: World, rng: random.Random, out: Path) -> Mentions:
    """Write the handovers, the export and the loose notes, and return what each file
    mentions."""
    incidents = incidents_of(world)
    exported, handed_over = place(rng, incidents)
    return merge(
        write_handovers(world, rng, out, incidents, handed_over),
        write_export(world, rng, out, incidents, exported),
        write_loose_notes(rng, out, incidents),
    )
