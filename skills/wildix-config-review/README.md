# wildix-config-review

Reviews a Wildix PBX's configuration offline from a standard WMS system backup (`*_wms_b_*.tar.gz`): dialplans, call groups, park orbits and timeout, users' forwards and function keys, trunks. Traces call flows and recommends WMS changes. Never connects to the PBX.

| Path | What |
|---|---|
| `SKILL.md` | The workflow Claude follows |
| `scripts/wildix_config_parser.py` | Parses the backup into `config_summary.txt` + `config.json`, with `--trace` and `--focus` |
| `scripts/lua51.py` | Reads the compiled Lua 5.1 per-user feature files in `users.tar` |
| `references/wildix-config-guide.md` | Backup layout, dialplan apps, patterns, call features, park, playbooks |
| `tests/` | Synthetic fixture backup + unit tests (not packaged) |

## Running the parser directly

```bash
python skills/wildix-config-review/scripts/wildix_config_parser.py <backup>_wms_b_x.tar.gz --output <backup>_wms_b_x \
  --autotrace autotrace_logs-XXXX      # incident trace or its analyzer output: must be the same PBX
python skills/wildix-config-review/scripts/wildix_config_parser.py <backup>.tar.gz --trace +17805551234 --from trunk:"Carrier Trunk"
python skills/wildix-config-review/scripts/wildix_config_parser.py <backup>.tar.gz --trace 901 --from user:1001
python skills/wildix-config-review/scripts/wildix_config_parser.py <backup>.tar.gz --focus park
python skills/wildix-config-review/scripts/wildix_config_parser.py <backup>.tar.gz --mermaid   # writes <output>/call_flow.md
```

`--autotrace` compares the PBX serial and domain of the backup with the trace and exits with code 3 on a mismatch (override with `--allow-mismatch`). The summary header shows the result as `PBX match`.

The backup holds plaintext credentials. The parser redacts them in its output, but keep the backup itself out of git (`*.tar.gz` is ignored) and delete it when you're done.

## Tests

```bash
python -m unittest discover -s skills/wildix-config-review/tests
```
