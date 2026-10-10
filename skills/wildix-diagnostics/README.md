# wildix-diagnostics

Analyzes Wildix PBX autotrace / Diagnostics Hub archives (`autotrace_logs-*.tar.gz`) and explains dropped calls, one-way audio, registration failures, routing errors and similar problems, citing the logs as evidence.

| Path | What |
|---|---|
| `SKILL.md` | The workflow Claude follows |
| `scripts/wildix_analyzer.py` | Filters logs + pcaps by time window and extension, decodes SIP/RTP (pure Python, no tcpdump needed) |
| `references/wildix-log-guide.md` | SIP codes, Wildix topology, call park, known-noise list |

## Running the analyzer directly

```bash
python skills/wildix-diagnostics/scripts/wildix_analyzer.py autotrace_logs-XXXX.tar.gz \
  --start "2026-10-02 09:00" --end "2026-10-02 09:30" \
  [--extension 1001] --output autotrace_logs-XXXX
```

- Times default to the PBX's own timezone (auto-detected). Add `--user-tz -0400` (or `America/Toronto` after `python -m pip install tzdata`) to use another zone.
- The report header's `PBX:` line and `pbx_identity.json` identify the PBX (domain + serial), which is used to match a WMS backup for wildix-config-review.
- Re-run without `--extension` when the issue may be trunk- or system-wide. Filtering hides trunk-side and network events.
