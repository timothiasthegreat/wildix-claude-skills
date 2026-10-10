---
name: wildix-config-review
description: Review a Wildix PBX's configuration from a standard WMS system backup (*_wms_b_*.tar.gz). Covers dialplans, call groups/queues, park orbits and park timeout, users' forwarding/no-answer settings, function keys and trunks. Traces where a number goes through the dialplans, explains why the PBX handled a call the way it did, and recommends concrete WMS changes. Use this whenever the user mentions a Wildix backup or config, dialplan rules, call flow, call routing, "where does this call go", park/parking/SP1/SP2 settings, queue or call group timeouts, unanswered or looping calls, or wants configuration advice after a wildix-diagnostics analysis, even if they only say "check how their parking is set up".
---

# Wildix Config Review

Turn a WMS system backup into an evidence-backed explanation of how calls flow on that PBX, and into specific changes a technician can make in WMS. Everything is offline: Claude never connects to or changes the PBX.

`scripts/wildix_config_parser.py` (stdlib only, Python 3.9+) reads the backup in memory and writes:
- `config_summary.txt`: park settings, users with active forwards and function keys, call groups, trunks, every dialplan flattened rule by rule, and a WARNINGS section (loops, dangling Gotos, empty queues, bad park keys, unused dialplans)
- `config.json`: the same model as JSON, for detail the summary leaves out
- `--trace NUMBER --from trunk:NAME|user:EXT|dialplan:NAME`: the step-by-step path a call takes
- `--focus park | queue:ID|NAME | user:EXT`: one slice of the config
- `--mermaid [FILE]`: writes `call_flow.md` (or FILE) with Mermaid flowcharts of inbound routing from every trunk, call park, outbound dialling, and dialplans nothing reaches
- `--autotrace PATH`: checks that the backup and the incident's autotrace come from **the same PBX** (serial and domain) and stops with exit code 3 if they don't

**Credentials:** a WMS backup contains plaintext secrets (SMTP, SIP trunk, LDAP passwords, PINs, TLS keys). The parser redacts them. **Never read, extract or grep the raw backup yourself.** Work only from the parser output. If something you need is missing from the output, extend the parser (and its redaction) rather than opening the archive.

## Step 1: Locate the backup and make sure it's the right PBX

Look in the working folder or a path the user gives for `*_wms_b_*.tar.gz`. The name encodes `<hostname>_<YYYY.MM.DD>_<HH.MM.SS>_<version>_<serial>_wms_b_<id>`. If there are several, list them with hostname, serial and date, and ask which one.

**The backup must come from the PBX in the incident.** Technicians often have backups from several customers, or from a lab PBX set up to look like a customer's. Advice based on the wrong PBX is confidently wrong. Each PBX has a serial (12 hex characters, e.g. `2211000012ab`) and a domain (`<name>.wildixin.com`):
- backup: serial and hostname are in the file name; the domain is in the summary header
- autotrace: wildix-diagnostics prints `PBX: <domain> (serial <serial>)` in the `filtered_report.txt` header and writes `pbx_identity.json` to its output folder

When there's an autotrace or diagnostics output for the incident, always pass it with `--autotrace` (Step 2). The parser compares serial and domain:
- **MATCH**: go ahead
- **exit code 3 / "different PBX"**: stop. Tell the user which PBX each file is from and ask for a backup from the incident PBX. Use `--allow-mismatch` only if the user explicitly wants to review the other PBX anyway (e.g. a lab copy), and then label every finding as coming from that PBX, not the incident PBX
- **could not identify**: compare the hostname/serial by hand and ask the user if unsure

With no autotrace (a pure config review), confirm the PBX with the user from the hostname and serial in the backup name.

Also check the date. A backup taken **after** the incident may include changes made since; one taken long **before** may miss changes. Mention the gap whenever a finding depends on a setting that might have changed.

If there's no backup, tell the user how to get one: WMS → **Settings → Backup** → create/download a system backup (or download the latest from the backup list). Then stop and wait.

## Step 2: Run the parser

```bash
python "<skill-dir>/scripts/wildix_config_parser.py" "<backup>.tar.gz" --output "<folder-next-to-backup>/<backup-name-without-.tar.gz>" \
  --autotrace "<autotrace_logs-*.tar.gz, or the wildix-diagnostics output folder>"
```

Leave off `--autotrace` only when there's no incident trace.

Use `python` on Windows and `python3` on macOS/Linux. The console line gives counts (users, dialplans, call groups, trunks, warnings) and any parse notes.

## Step 3: Read the summary

Read `config_summary.txt`. It's usually a few hundred lines, so read it whole unless it's very large. In that case start with the header, CALL PARK and WARNINGS, then grep for the extensions, numbers or dialplans in question. `references/wildix-config-guide.md` explains the backup layout, what each dialplan application and parameter means, the feature codes (fwd-all / fwd-busy / fwd-noanswer / noanswer-timeout), and playbooks for common problems. Read the relevant playbook before recommending changes.

## Step 4: Connect it to the incident

If there's a wildix-diagnostics case write-up or report (often in `cases/`), pull out the identifiers: extensions, park orbits (SP1/SP2 → 901/902…), queue/call group numbers, the trunk, the DID called, and timeouts seen in cw.log. Reconcile each one with the config:
- `Nobody picked up in 45000 ms` ↔ `GPARK_TIMEOUT` in CALL PARK
- `Queue(...)` / call group in cw.log ↔ the call group's members, strategy, ring timeout and the rule's max wait
- the dialplan context in cw.log ↔ the dialplan named in the summary
- a forward to voicemail in cw.log ↔ the user's fwd-noanswer / fwd-busy settings

If the backup and the incident disagree (e.g. timeout 22 s in the backup, 45 s in the logs), say so: the backup may be from a different PBX or date, or the config changed since.

## Step 5: Trace the call flow

Run `--trace` for the call in question. Inbound calls start from the trunk (`--from trunk:"Trunk Name"`, number = the DID as the carrier sends it, usually `+1...`). Internal calls start from the user (`--from user:101`). To test a fragment, start from a dialplan. Use `--focus park` / `--focus user:EXT` / `--focus queue:ID` for detail.

The tracer covers Set, Goto, GotoIf, Verify timetable and the "Jump to if" rules (switch on, 3-state switch on/blinking, call type, busy/congested), each with every branch, Queue (members, timeouts), DialPhone (with the user's forwards), DialTrunk, VoiceMail, IVR digits and loops. Park traces continue past the ring-back into the reserved `[receptionist]` dialplan (its `default` number, stored as `s`), which the PBX enters itself after an unanswered or busy ring-back; never call it unused. Built-in routing (calls to plain extensions that no rule matches), CustomApp bodies, timetable/switch logic inside CustomApps, and Wildix behaviour the backup can't show (e.g. exactly when a park "Call back" rule fires) are flagged as such. Don't state those as fact. Say "per config, probably…" and suggest a test call with an autotrace to confirm.

## Step 6: Present findings

**PBX**: domain, serial, backup date, and the match result against the autotrace (or "not matched to an autotrace").

**Summary**: 2–3 sentences: how this PBX handles the call type in question, and the config reason behind the reported behaviour.

**Configuration as found**: the relevant settings, each with its evidence (`GPARK_TIMEOUT=45000`, `[queues testing] 8006 rule 3392: Queue(5, max_wait_s=10)`, `user 1001 external: fwd-noanswer=VOICEMAIL`).

**How it produced the behaviour**: walk the trace in plain language, with timings where you can add them up (e.g. 45 s parked + 35 s ring-back = 80 s before the caller gave up).

**Diagram** (when it helps, e.g. for the customer or a ticket): generate it with `--mermaid` and point to `call_flow.md`. If the user wants a simpler picture, hand-edit a copy (keep the generated file as the source of truth) rather than drawing from memory. Diagrams mark ⚠ for behaviour the backup can't confirm. Keep those marks.

**Recommendations**: ranked. Each one gives:
- what to change, as a WMS click-path (e.g. *WMS → Dialplan → Dialplan rules → `users` → add rule `901`*)
- why it helps
- trade-offs or side effects (which other callers or users it affects)
- how to verify it (test call, autotrace)

Claude does not make the change.

Also list any WARNINGS that matter for this call flow, plus other risky items worth fixing (loops with no exit, empty queues, park keys pointing at non-existent orbits). Skip cosmetic ones.

If the user is non-technical, add a short plain-language summary for the end user, like the diagnostics case write-ups.

## Step 7: Follow-ups

Offer to:
- trace other numbers or origins
- compare against a newer backup
- draft the exact rule list for a recommended dialplan change
- write the recommendations into the case file
