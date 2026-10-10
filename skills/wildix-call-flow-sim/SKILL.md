---
name: wildix-call-flow-sim
description: Build an interactive Wildix call-flow simulator page (one self-contained HTML file) from a WMS system backup (*_wms_b_*.tar.gz), a build plan (ticket replies, client answers, notes, the conversation), or both. Controls for day/time, switches (office mode, meeting mode), who is free or busy, queue length and what the caller presses drive a highlighted path through the call-flow diagram, an outcome banner and a step-by-step trace. From a plan it adds Build plan, info, UAT checklist and Open items tabs; with a backup it uses the PBX's real users, call groups, timetables and switches and flags plan objects that still need creating. Use this whenever the user wants to visualize, simulate, demo or sanity-check a Wildix call flow or PBX deployment plan, "show the client how calls will be handled", build a call-flow page or visualizer, turn a build plan into something interactive, or see what the current PBX does with a call at a given time, even if they only say "make one of those call simulator pages for Acme".
---

# Wildix Call Flow Simulator

Produces one **self-contained HTML page** that simulates how a Wildix PBX handles a call: pick a scenario or set the controls, and the diagram highlights the path, a banner gives the outcome, and a numbered trace explains each step. Claude never connects to or changes the PBX.

`scripts/wildix_flow_sim.py` (stdlib only, Python 3.9+) does the work. You don't write the page's HTML or JavaScript. You write a **plan JSON** (for a proposed design) and the script turns it, and/or a backup, into the page:
- **Backup only**: one flow per inbound number, built from the real dialplans (same walk as the wildix-config-review Mermaid charts). This is the current configuration.
- **Plan only**: the proposed design with build tabs, before the PBX exists.
- **Plan + backup**: the proposed design, checked against the PBX. Real extensions and names are used, existing call groups and timetables fill in their real settings, and anything not in the PBX is flagged *To create* or *Not found*. `--with-current` also adds the backup's current flows to the number picker, for before/after comparison.

`references/flow-spec.md` is the plan format (node types, conditions, presets, tabs, writing rules). `examples/example-dental.plan.json` is a complete worked example. Read both before writing a plan.

**Credentials:** a WMS backup contains plaintext secrets. The parser redacts them, and the page never includes trunk passwords, SIP auth users or PINs. Never read or grep the raw backup yourself. External numbers in backup flows show their last 4 digits only (`--full-numbers` to override); in plans, describe private numbers ("the owner's cell"), never write them.

## Step 1: Inputs

Work out what you have:
1. **A backup?** Look in the working folder or a path the user gives for `*_wms_b_*.tar.gz`. If there are several, list them (hostname, serial, date from the file name) and ask.
2. **A build plan?** It can be the client's answers in a ticket (read it with the ticketing connector if the user names one), notes, a document, a previous plan JSON, or this conversation. If the user wants a plan visualized but hasn't given the details, ask for them. Don't design the call flow from guesses.
3. **The client and site name** for the title. Use the user's wording, not the PBX hostname.

Neither a backup nor a plan: ask which one they have.

## Step 2: Backup only (current configuration)

```bash
python "<skill-dir>/scripts/wildix_flow_sim.py" --backup "<backup>.tar.gz" [--title "Acme Dental: current call flows"]
```

The output goes to `<backup-name>-callflow/` next to the backup (git-ignored like other backup output). Read the report it prints:
- **flows**: one per inbound number or pattern, with node counts. A flow that is a single `hangup` ("Number not found") is a DID with no route. Mention it.
- **NOTE**: truncated diagrams, dynamic gotos (`${...}` targets the simulator can't follow)
- **Parked call returns** is an extra flow when the PBX has park orbits: pick-up from the park key, ring-back to the parker, then the parker's forward (probably first, unconfirmed) or the reserved `[receptionist]` dialplan's `default` number. `receptionist` is entered by the PBX itself; it is never "unused".
- **Switches** are logic gates in the dialplan, not tied to a timetable. Each "Jump to if switch is on" or "Jump to if 3 state switch is on" rule becomes a switch node, with Off/On (and Blinking for 3-state) radio buttons that start in the position the switch had when the backup was taken. Say which position that was. It may have changed since. "Jump to if call type is" and "Jump to if congested / busy" become yes/no checkboxes.

Then go to Step 5.

## Step 3: Write the plan JSON

Write `<client>-callflow/plan.json` (next to the backup if there is one, otherwise in the working folder) following `references/flow-spec.md`. The rules that matter most:
- **Facts come from the client or the backup.** Unconfirmed items go in `open` as `["o", ...]` with an `[o: ...]` chip where they appear in the Build plan. The flow uses a neutral placeholder ("Admin A") until confirmed.
- **Wildix behaviour you haven't verified** is a `["v", ...]` item, a `[v]` chip on the build step, and a `note` on the node. Examples: a 3-state switch tested in a dialplan, a free-member count jump, an IVR key during the greeting, dial-out permissions for an urgent line.
- **Number the dialplan steps** in `tabs.build` (`steps` block) and use the same numbers in each node's `step`, so the trace and the build steps line up.
- **Presets:** one per scenario the client asked about, plus the UAT edge cases (queue full, nobody free, outage, each switch). Give them human names.
- With a backup, use the PBX's real names for existing objects (call group, timetable, switch and user names) so they resolve. Give `people` an `ext` when you know it.

## Step 4: Render

```bash
python "<skill-dir>/scripts/wildix_flow_sim.py" --plan "<folder>/plan.json" [--backup "<backup>.tar.gz"] [--with-current] --output "<folder>"
```

- **Exit code 2** lists structural errors (missing nodes, undefined switches or people). Fix the plan and re-run.
- **Objects** (with a backup): check every *Not found* or *Ambiguous* person and every *To create* object. A misspelt name looks like a missing user, so fix the spelling or add `ext`. Real gaps stay: the page adds them to Open items automatically.
- **WARNING: timetable … plan hours differ from the PBX**: tell the user. One of them is out of date.
- **WARNING: overlapping nodes / edge crosses node**: fix the layout with `primary` or `layout` pins (see the reference), then re-run.

`flow_spec.json` in the output folder is the resolved spec. Look there to check what the page will contain.

## Step 5: Check the page

Open the HTML file in the browser pane. If the pane only shows a static snapshot of local files (scripts don't run), serve the folder instead: `python -m http.server 8765 --bind 127.0.0.1 --directory "<folder>"`, started as a preview server, and open `http://localhost:8765/<file>.html`.

Then run every preset and check its outcome against what the plan intends:

```js
window.flowSim.presets().map((l, i) => l + ' => ' + window.flowSim.run(i).banner).join('\n')
```

`window.flowSim.run(i)` also returns `path` (the node ids lit up). Any preset whose outcome doesn't match the plan is a bug in the plan JSON. Fix it and re-render. If screenshots work, also look at the diagram for awkward edges.

## Step 6: Report

Tell the user:
- the file path (one HTML file: send it, attach it to the ticket, or open it on a call with the client)
- the flows and presets, and what each preset shows (one line each)
- with a backup: objects to create, and people or trunks not found
- the counts of Open and Verify items, and the most important ones
- for backup flows: anything odd the simulation exposes (unrouted DIDs, queues with no max wait, forwards that skip later rules)

When the plan changes (client answers an open item, design change), edit `plan.json`, bump `version`, and re-render. The new version starts with fresh checklist ticks.

## Follow-ups

Offer to:
- re-render after the client answers open items, or from a new backup once it's built (`--with-current` to compare)
- trace a specific number or explain a rule in depth (wildix-config-review)
- document the finished PBX in Hudu (wildix-config-docs)
