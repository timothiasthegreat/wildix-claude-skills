---
name: wildix-diagnostics
description: Analyze Wildix PBX autotrace / Diagnostics Hub archives (autotrace_logs-*.tar.gz) to find the cause of a phone-system problem — dropped or failed calls, one-way or no audio, registration failures, calls not ringing, routing/dialplan errors, trunk issues. Filters wms.log, cw.log (callweaver), authorization.log and SIP/RTP pcaps to the time of the incident and the extension or caller involved, then reports findings with log evidence. Use this whenever the user mentions Wildix, an autotrace or diagnostics archive, a PBX trace, a siprtp/siponly pcap, or asks why a call failed or audio dropped on a Wildix system — even if they only say "can you look at this trace" or "customer says calls dropped around 9:30".
---

# Wildix Diagnostics

Turn a Wildix autotrace archive plus a user's vague incident description ("calls dropped around 9:30 for the front desk") into evidence-backed findings.

The heavy lifting is done by `scripts/wildix_analyzer.py` (in this skill's directory). It needs only Python 3.9+, no tcpdump/tshark/Wireshark. It:
- auto-detects the PBX timezone from nginx entries in `wms.log` (syslog and callweaver timestamps carry no zone, so this matters)
- filters every log and pcap to the requested window and optional call identifiers
- decodes the pcaps itself into a **SIP ladder** (one line per message, grouped by Call-ID), an **RTP stream summary** (direction, packet count, estimated loss, codec), and full SIP messages with SDP
- writes `filtered_report.txt` and `filtered_*.pcap` files (open those in Wireshark for deeper work)

## Step 1: Locate the archive

The archive is usually in the folder the user is working in, or a path they give (often `~/Downloads`). Look for `autotrace_logs-*.tar.gz` or an already-extracted `autotrace_logs-*` directory. If there are several, list them with their modified dates and ask which one. The script accepts either form.

## Step 2: Gather only what's missing

Read the user's message first, since they often give everything up front. Also check the archive for **user reports** before asking anything. When a user dials the PBX's report code (the pilot code that jumps to the `Autotrace_WEB` dialplan; often `77777`, but each PBX picks its own), the archive includes `reports/report_<ext>_<date>_<time>.json` with the extension, the PBX-local time, and the user's own description of the problem. A quick look works without extracting:

```bash
python -c "import tarfile,sys; t=tarfile.open(sys.argv[1]); [print(m.name, t.extractfile(m).read().decode()) for m in t if '/reports/' in m.name and m.isfile()]" "<archive>"
```

Times quoted in those reports are PBX-local, so omit `--user-tz` (it defaults to the PBX timezone) unless the user wants another zone.

Ask (AskUserQuestion works well) only for gaps:

**Needed**
- **When it happened**: date plus approximate time.
- **The user's timezone**: the one they're quoting the time in. Don't assume one. Ask if it isn't clear from context. You do *not* need the PBX's timezone, which is auto-detected.

**Helpful when the issue involves specific calls or people**
- Extension number(s), caller ID name or number, PBX username

Window sizing: for "around 9:30", use ±15 min. For a vaguer time ("this morning", "around lunch"), pad ±30 min or more. Wider is cheaper than re-running, but very wide windows on busy PBXes produce huge reports.

## Step 3: Run the filter

Put output in a folder named after the archive, next to the archive, so the filtered pcaps and report persist and are easy to find later:

```bash
python "<skill-dir>/scripts/wildix_analyzer.py" "<archive>" \
  --start "YYYY-MM-DD HH:MM:SS" --end "YYYY-MM-DD HH:MM:SS" \
  --user-tz "America/Toronto" \
  [--extension 117] [--callerid-name "Front Desk"] [--callerid-num "+15551234567"] [--username jsmith] \
  --output "<archive-folder>/<archive-name-without-.tar.gz>"
```

Notes:
- Use `python` on Windows, `python3` on macOS/Linux.
- Filters are repeatable (`--extension 117 --extension 118`) and are OR'd together.
- Leave `--system-tz` off (it defaults to `auto`). Override it only if the report header says auto-detection failed or the times are clearly shifted.
- **Windows timezone gotcha:** IANA names like `America/Toronto` need the `tzdata` package on Windows. If the script says the timezone isn't found, either use a UTC offset matching the date (e.g. `-0400` for Eastern daylight time, `-0500` for standard time) or ask the user before running `python -m pip install tzdata`.
- **Call filters hide system-level evidence.** An IPTABLES drop, trunk error, or service restart usually doesn't contain the extension number, so a filtered run won't show it. If the filtered report doesn't explain the problem, or the issue sounds network-wide ("all phones", "nobody could call out"), run again **without** call filters, into a sibling output folder like `<name>-unfiltered`.
- In the report's full SIP messages, digest `response`, `nonce`, `cnonce` and `opaque` values in `Authorization` / `Proxy-Authorization` / `WWW-Authenticate` / `Proxy-Authenticate` headers are replaced with `[REDACTED]`. `username`, `realm`, `uri`, `algorithm` and `qop` stay visible, which is enough to diagnose 401/403/407 loops. The filtered `.pcap` files are not altered. Don't add `--no-redact` unless the user explicitly asks for the raw values.
- siprtp pcaps are filtered by time only (RTP packets carry no extension), so the RTP summary covers every stream in the window. Tie streams to the call using the IP and port in each side's SDP `c=` and `m=audio` lines.
- If the script reports 0 entries everywhere, the window is probably off. Check the "Time range (UTC)" line in the header against the pcap time ranges printed in the console output, then widen the window or fix the timezone.

## Step 4: Read and analyze the report

Read `<output>/filtered_report.txt`. If it's large (thousands of lines), don't read it all at once: start with the header, the SIP LADDER and RTP STREAMS sections, then grep for specifics (Call-IDs, `WARNING`, `ERROR`, `SIP/2.0 4`, `SIP/2.0 5`, `IPTABLES`, the extension).

Work out the call story: who called whom, when, what the PBX did (cw.log), what went over the wire (SIP ladder), and whether media flowed (RTP). For any dropped call, establish **who sent the BYE**. That single fact splits "the caller/carrier hung up" from "the PBX ended it" from "the phone ended it."

`references/wildix-log-guide.md` has the detailed patterns, SIP response codes, and what each log file contains. Read it when interpreting something unfamiliar. In particular:
- **Section 8** explains why each call appears twice (kamailio 5060 ↔ callweaver 6050 over loopback), why TLS trunk legs are only visible as their loopback mirror, how to tell who hung up, and a grep that strips keepalive noise from the ladder
- **Section 9** covers call park ("SP1/SP2" keys, park timeouts, return-ringing)
- **Section 10** lists warnings that appear on every healthy Wildix system. Leave them out of findings so the real evidence isn't buried.

Then check the problem categories below.

- **Registration/auth**: 401/403/407 on REGISTER, repeated re-registrations, failures in authorization.log, user-agent changes
- **Call setup**: 4xx/5xx/6xx responses, CANCEL before answer, missing ACK after 200 OK (call drops at ~32 s), no 180/183 ringing
- **Audio/RTP**: 200 OK with no RTP afterwards, a stream in only one direction (one-way audio), RTP sent to a private IP from the SDP (NAT), codec mismatch between offer and answer, high estimated loss
- **Network**: IPTABLES DROP lines in wms.log (source IPs, frequency), ping/MTR loss or latency. A mid-path MTR hop showing 100% loss while later hops respond is usually just ICMP rate-limiting, not a fault.
- **Dialplan/routing**: callweaver "unknown context", "no such extension", jump failures. A call that enters the `Autotrace_WEB` dialplan (dialled as the report code, often `77777`; ask the user if you can't tell which code the PBX uses) means the user triggered a report at that moment, so its timestamp marks when they noticed the problem.
- **System health**: one error repeating at high frequency, anomalously old pcap files (flagged in the report header), public IP in the archive vs. IPs seen in SIP Via/Contact (NAT/SIP ALG problems)

## Step 5: Present findings

Use this structure:

**PBX**: the domain and serial from the report header (`PBX: acme.wildixin.com (serial 2211000012ab)`). Always include it, including in case write-ups, so later config reviews can be matched to the right PBX.

**Summary**: 2–3 sentences covering what happened, when (in the user's timezone), and which users/devices/trunks were involved.

**Findings**: a numbered list. Each item states the issue and quotes the specific log line or SIP message as evidence, with its timestamp.

**Likely cause(s)**: ranked by probability, each tied to the findings that support it.

**Recommended next steps**: concrete actions (check SIP ALG on the site router, fix the trunk's NAT setting, correct the dialplan context, reprovision the phone, etc.).

Stick to what the logs show. If the evidence doesn't settle the cause, say so and say what extra data would (a wider window, an unfiltered run, a siprtp capture instead of siponly, a capture from the phone side). A confident wrong answer sends the technician down the wrong path, while "inconclusive, here's what to capture next" still moves the ticket forward.

Mention where the output folder is so they can open the filtered pcap in Wireshark.

When the findings come down to configuration (park timeout and ring-back, queue/call-group timeouts, a dialplan routing the call somewhere unexpected, a user's forward), say so and suggest the **wildix-config-review** skill: with a WMS system backup it can show the exact settings and dialplan rules involved and recommend changes. The backup must be from the same PBX (same serial/domain as the `PBX:` line). Pass this output folder to the config parser with `--autotrace` and it checks this automatically.

## Step 6: Follow-ups

End by offering follow-ups: questions about any finding, or searching the logs for something specific. Be ready to:
- re-run with a different window or filters (or none)
- grep the filtered or raw logs for a pattern
- trace one Call-ID end to end
- explain a SIP response code or log entry
