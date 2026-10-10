# Plan JSON / flow spec reference

`wildix_flow_sim.py --plan plan.json` reads this format. `examples/example-dental.plan.json` is a complete worked example (a dental front desk: meeting mode, office-mode switch, closures, ring then queue, an after-hours urgent line). Copy its shape.

The script resolves the plan into `flow_spec.json` (the same structure plus layout and resolved objects) and injects it into the page. Unknown keys are ignored. Structural mistakes (a branch to a missing node, an undefined switch/timetable/person, an unknown node type) stop the script with exit code 2 and a list of errors.

## Top level

| Key | What |
|---|---|
| `title`, `subtitle` | Page heading. Put the client, the number and the draft version in them: "Acme Dental: main number build plan", "Draft v2 from the client's replies on ticket 12345". |
| `version` | Bump it when the plan changes. It is part of the browser key for UAT/Open-item ticks, so old ticks don't carry over to a changed plan. |
| `timetables` | `{name: {...}}`, see below |
| `switches` | `{name: {"states": [[value, label], ...], "default": value, "label": "...", "note": "..."}}`. 2-state: `[["off","Off"],["on","On"]]`. 3-state: e.g. `[["follow","Follow schedule"],["in","Force in hours"],["out","Force out of hours"]]`. |
| `people` | `{name: {"ext": "201", "label": "...", "match": "...", "external": true}}`. Everyone who rings in a group or call. `ext`/`match` help find them in the backup; `external: true` skips the backup check. |
| `people_legend` | Heading for the presence controls (default "Phones"), e.g. "Front desk phones" |
| `groups` | `{name: {"members": [...], "strategy": "..."}}`. A member is a person name, or `{"name": ..., "when": <condition>}` for conditional membership (a laptop that joins on schedule). With a backup, a group that exists in the PBX and has no `members` takes the PBX members. |
| `flags` | `{name: "checkbox label"}`. Free-form yes/no facts referenced from conditions (`{"flag": name}`) |
| `flows` | List of flows, one per called number or entry point. The page shows a "Called number" picker when there are several. |
| `presets` | Quick-scenario buttons, see below |
| `default_presets` | `false` to show only your presets. Default `true`: the generated ones (in hours, out of hours, nobody picks up, everyone busy, queue full, internet down) come first. |
| `tabs` | Build plan and info tabs, see below |
| `uat` | `[{"g": "Group", "i": ["test", ...]}]` |
| `open` | `[{"g": "Group", "i": [["o", "question for the client or us"], ["v", "check in WMS / with Wildix"]]}]` |

### Timetables

- Weekly: `{"hours": {"mon": "08:00-19:00", "tue-thu": "08:00-16:00", "fri": "07:00-12:00, 13:00-15:00"}}`. Days `mon`..`sun`, ranges wrap (`fri-mon`). Missing days are closed. Evaluated against the Day and Time controls.
- Date-based (holidays, closures, anything the simulator can't compute): `{"kind": "dates", "label": "Today is a stat holiday"}`. Shown as a checkbox with that label.
- `"dates": true` on a weekly timetable adds a "today matches a date entry" checkbox as well.

With a backup, a plan timetable with the same name as a PBX timetable is marked **In PBX**. If it has no `hours`, the PBX's hours are used. If the hours differ, the report warns.

## Nodes

Every node: `type`, `title` (short, it's the box heading), optional `sub` (1 to 3 short lines under it), `step` (shown in the trace, e.g. "Step 4": match the numbering of the Build plan dialplan steps), `note` (tooltip), `labels` (`{branch: edge label}`), `say` (`{branch: trace sentence}` to replace the generated one), `primary` (branch to draw straight down; normally automatic).

Branches go in `"go": {branch: node_id}`, except single-step nodes, which use `"next"`.

| type | Branches | Fields | Controls it adds |
|---|---|---|---|
| `start` | `next` | | |
| `route` | `next` | a dialplan entry point (used for backups) | |
| `play` | `next` | `text`: the wording, quoted in the trace | |
| `switch` | one per state value, `*` = any other | `switch`: name | radio buttons for the switch |
| `timetable` | `open`, `closed` | `timetable`: name | Day/Time, or a checkbox |
| `decide` | the keys of `go` | `cases`: `[{"when": cond, "go": key, "say": ..., "step": ...}` or `{"timetable": name, "match": false, "go": key}]`, `else`: key, `else_say`, `else_step`. Cases are checked in order; the first that holds wins. Use it for "Open right now?" style checks that combine switches and timetables in one box. | from the conditions |
| `condition` | `true`, `false` | `if`: cond (computed), or none: a checkbox labelled `label` | checkbox if no `if` |
| `ring` | `answered` (optional: no target ends the call as answered), `no_answer`, `skipped` (defaults to `no_answer`) | `group` or `members`, `timeout` (s), `strategy` (text), `skip_if_none_free` (jump past when nobody is free), `pick_label`, `answered_text` (`{who}` = who picked up) | "a free phone picks up" checkbox, presence per member |
| `queue` | `answered` (optional), `timeout`, `full` (defaults to `timeout`) | `group`/`members`, `max_len` (0 = unlimited), `max_wait` (s; none = caller waits forever), `pick` (`false` when the members were already rung in an earlier ring stage), `music`, `count_label`, `wait_label` | callers already waiting, "someone answers during the wait", "a free member picks up" (unless `pick: false`) |
| `ivr` | one per key (`"0"`, `"1"`, or a dialled extension), `timeout` | `text` (the greeting), `wait` (s), `key_label` | what the caller presses |
| `dial_external` | `answered` (optional), `no_answer` | `target` (describe it, e.g. "the urgent contact's cell"; **never** a full private number), `trunk`, `timeout`, `answer_label`, `answered_text`, `answered_small` | "they answer" checkbox |
| `park` | none | | |
| `voicemail` | none | `mailbox`, `email` | |
| `fax`, `forward`, `answered`, `hangup`, `end` | none | `banner`, `banner_small`, `text`, `outcome` (`ok`/`vm`/`closed`/`bad`) to override the outcome banner | |

### Conditions (`when`, `if`, member `when`)

```json
{"switch": "Office Mode", "is": "in"}            // is can be a list: {"switch": "X", "is": ["on", "follow"]}
{"timetable": "Laptop Days"}                     // true while the timetable matches
{"flag": "fax_shared"}                           // a checkbox from "flags"
{"any": [ ... ]}  {"all": [ ... ]}  {"not": { ... }}
```

Example from the worked plan: the laptop rings when its switch is Force On, or when it is Follow Schedule and today is a laptop day:

```json
{"name": "Laptop user", "when": {"any": [{"switch": "Laptop", "is": "on"},
  {"all": [{"switch": "Laptop", "is": "follow"}, {"timetable": "Laptop Days"}]}]}}
```

## Presets

```json
{"label": "Both on calls", "flow": "main", "state": {"dow": 3, "time": "11:30", "m:Admin A": "busy", "pick:ring": false, "qa:queue": true}}
```

`state` only lists what differs from the defaults (Tuesday 10:00, every switch at its `default`, everyone free, free phones pick up, nobody waiting, no key pressed, external numbers don't answer, date checkboxes off). Keys:

| Key | Value |
|---|---|
| `dow`, `time` | 0 = Sunday … 6 = Saturday; `"HH:MM"` |
| `tt:<timetable>` | `true` = today is in this date-based timetable |
| `sw:<switch>` | a state value |
| `f:<flag>` | `true`/`false` |
| `m:<person>` | `"free"`, `"busy"`, `"offline"` |
| `pick:<node id>` | ring/queue: does a free phone pick up |
| `q:<node id>`, `qa:<node id>` | queue: callers already waiting; someone answers during the wait |
| `ivr:<node id>` | key pressed (`""` = none) |
| `ext:<node id>` | external number answers |
| `c:<node id>` | a checkbox-condition is true |

Make one preset per scenario the client asked about, plus the edge cases from the UAT list (queue full, nobody free, outage, each mode switch). Name them as a person would ("Saturday evening", "Staff meeting"), not by mechanism.

## Tabs

```json
"tabs": {
  "build": [ blocks ],                          // "Build plan" tab
  "extra": [{"title": "Phones, fax, outage", "blocks": [ blocks ]}],
  "uat_intro": "Run on the temporary numbers.", "open_intro": "..."
}
```

Blocks: `{"h2": text}`, `{"h3": text}`, `{"p": text}`, `{"note": text}` (amber callout), `{"list": [text, ...]}`, `{"table": [[heading, value], ...]}` (two-column facts), `{"grid": [[col, ...], [cell, ...], ...]}` (first row is the header), `{"steps": [text or {"group": "OPEN"}, ...]}` (numbered steps; groups are unnumbered dividers), `{"cols": [[blocks], [blocks]]}` (two columns).

Text markup: `` `WMS object name` ``, `**bold**`, and chips: `[o]` / `[o: Open: confirm names]` (purple, open question), `[v]` / `[v: Verify: ...]` (amber, check in WMS or with Wildix), `[c: To create]`, `[d: Done]`, `[x: Problem]`. Everything else is escaped: no HTML.

With a backup, the Build plan tab also gets an **Objects in the plan** table showing each timetable, switch, person, call group and trunk as In PBX / To create / Not found / Ambiguous. Problems are added to Open items under "From the PBX backup check".

## Writing a plan from a build plan or ticket

- **Never invent a fact.** Hours, members, numbers and wording come from the client's replies or the backup. Anything unconfirmed goes in `open` as `o` (and a chip next to it in the Build plan), and the flow uses a clearly named placeholder ("Admin A").
- Anything that depends on Wildix behaviour you haven't confirmed (a 3-state switch tested in the dialplan, free-member counts, IVR accepting a key during the greeting, dial-out permissions) is a `v` item, a `[v]` chip in the step, and a `note` on the node.
- One node per decision or action a technician will build. A combined check such as "Open right now?" can be one `decide` node whose cases map 1:1 to the numbered dialplan steps.
- Number the dialplan steps in `tabs.build` (`steps` block) and put the same numbers in each node's `step`, so the trace reads "Step 4: …".
- Private numbers (an owner's cell for urgent calls) never appear: describe the target.
- Keep titles to about 26 characters and `sub` lines to about 30, or they wrap. The layout is automatic. If a diagram comes out awkward, set `"primary"` on a node, or pin nodes with `"layout": {"node_id": [column, row]}` on the flow.
