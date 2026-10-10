# wildix-call-flow-sim

Builds an interactive call-flow simulator page (one self-contained HTML file) from a WMS backup, a plan JSON written from a build plan, or both. The page has controls, presets, a highlighted SVG diagram, an outcome banner and a step-by-step trace. From a plan it also has Build plan, info, UAT and Open items tabs, with ticks saved in the browser.

| Path | What |
|---|---|
| `SKILL.md` | The workflow Claude follows |
| `scripts/wildix_flow_sim.py` | Backup → flows (`FlowBuilder`), plan loading and validation, resolution against the backup, grid layout, page rendering |
| `assets/template.html` | The page: CSS and a generic JS engine that reads the injected flow spec. No per-client code. |
| `references/flow-spec.md` | Plan JSON format and writing rules |
| `examples/example-dental.plan.json` | Worked example: the anonymised "Example Dental" demo page rebuilt as a plan. It is also a regression test (the 8 presets must give the demo's outcomes). |
| `tests/` | Unit tests (not packaged) |

The parser (`wildix_config_parser.py`, `lua51.py`) is shared from wildix-config-review. `build_skills.py` copies it into this package.

## Running it

```bash
python skills/wildix-call-flow-sim/scripts/wildix_flow_sim.py --backup <backup>_wms_b_x.tar.gz            # current flows
python skills/wildix-call-flow-sim/scripts/wildix_flow_sim.py --plan plan.json --output out                # proposed design
python skills/wildix-call-flow-sim/scripts/wildix_flow_sim.py --plan plan.json --backup <backup>.tar.gz --with-current --output out
```

Run the example in the browser: render it into the git-ignored `flowsim-workspace/` folder, then start the `flowsim-preview` server from `.claude/launch.json`. The pane doesn't run scripts in local files. Check outcomes with `window.flowSim.run(i)`.

```bash
python skills/wildix-call-flow-sim/scripts/wildix_flow_sim.py --plan skills/wildix-call-flow-sim/examples/example-dental.plan.json --output flowsim-workspace/demo
```

## How the engine works

`FlowBuilder` mirrors `MermaidBuilder.chain()` in the parser. It maps each WMS app to a typed node (Timetable → `timetable`, Queue → `queue`, DialPhone → `ring`/`forward`/`park`, WaitExten → `ivr`, …).

In the page, each node type has a step function that reads the control state and returns a branch. The controls themselves are derived from the node types each flow reaches.

The layout is a small grid algorithm:
- The main branch goes down.
- One side branch sits beside its node; several fan out on the row below.
- Shared nodes go below their callers.
- Loop-backs route through a gutter.

`check_overlaps` reports any node overlaps and any edges that run through a node.

## Tests

```bash
python -m unittest discover -s skills/wildix-call-flow-sim/tests
```
