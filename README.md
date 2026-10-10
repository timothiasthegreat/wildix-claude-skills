# Wildix Claude Skills

Claude skills for supporting Wildix PBX systems. Each skill lives in `skills/<name>/` and is packaged to `dist/<name>.skill` for installation.

| Skill | Use it for | Input |
|---|---|---|
| [wildix-diagnostics](skills/wildix-diagnostics/README.md) | Why did a call drop / fail / have no audio? Evidence from logs and SIP/RTP captures. | Autotrace / Diagnostics Hub archive (`autotrace_logs-*.tar.gz`) |
| [wildix-config-review](skills/wildix-config-review/README.md) | How is this PBX set up to handle the call? Dialplans, queues, park, forwards, trunks; call-flow tracing, Mermaid call-flow diagrams and WMS change recommendations. | WMS system backup (`*_wms_b_*.tar.gz`) |
| [wildix-config-docs](skills/wildix-config-docs/README.md) | Document the client's PBX in Hudu: one standardized KB article per PBX (accordions, Mermaid call flows, dialplan layouts), created or updated as a draft. | WMS system backup + the Hudu company name |
| [wildix-call-flow-sim](skills/wildix-call-flow-sim/README.md) | Interactive call-flow simulator page: set day/time, switches and who's free, and see the path a call takes. Proposed designs get build-plan, UAT and open-item tabs. | WMS system backup, a build plan, or both |

The skills work well together:
- diagnostics explains *what happened* on a call
- config review explains *why the PBX is set up to do that* and what to change
- the call-flow simulator shows a client or technician *what will happen* under each scenario, before or after the build
- config docs keeps the client's Hudu documentation current

## Matching a backup to a diagnostic report

Config advice only makes sense against the PBX the incident happened on. Every PBX has a **serial** (12 hex characters, e.g. `2211000012ab`) and a **domain** (`<name>.wildixin.com`):

| Source | Where the identity is |
|---|---|
| WMS backup | File name: `<hostname>_<date>_<time>_<version>_<serial>_wms_b_<id>.tar.gz`; domain in `config_summary.txt` |
| Autotrace analysis | `PBX:` line in `filtered_report.txt`, and `pbx_identity.json` in the output folder |
| Case write-up | `**PBX:**` line |

Run the config parser with `--autotrace <archive or diagnostics output folder>`. It prints MATCH, or stops with exit code 3 if the two come from different PBXes. Also compare the backup date with the incident date: changes made between them won't show up.

## Layout

| Path | What |
|---|---|
| `skills/<name>/` | Skill source: `SKILL.md`, `scripts/`, `references/`, plus a dev-only `README.md` and `tests/` |
| `dist/<name>.skill` | Packaged skills, ready to install |
| `build_skills.py` | Rebuilds the packages |
| `cases/` | Write-ups of real analyses. Used to improve the skills and as test cases. |

## Customer data

Autotrace archives, WMS backups (`*.tar.gz`), analyzer/parser output folders and `cases/` are **git-ignored**. They contain phone numbers, IPs, call records and, in WMS backups, plaintext credentials.

## Conventions

- Scripts are Python 3.9+, standard library only (`python` on Windows, `python3` elsewhere).
- Scripts are read-only with respect to the PBX: they analyze files you download. Recommendations are applied by a technician in WMS.

## Building packages

```bash
python build_skills.py
```

Run tests with `python -m unittest discover -s skills/<name>/tests` where a skill has them. Pass skill names to build only those (`python build_skills.py wildix-diagnostics`). `tests/`, `__pycache__/` and the skill's `README.md` are left out of the package.
