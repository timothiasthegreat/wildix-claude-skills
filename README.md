# Wildix Claude Skills

Four [Claude skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) for supporting Wildix PBX systems. They help an MSP or Wildix partner technician:
- find out why a call failed
- see how a PBX is set up to route calls
- document a client's phone system in Hudu
- show a client how their calls will be handled

All of the work happens offline, on files you download from the PBX. The skills never connect to a PBX or change one. Every recommendation is a WMS change for a technician to make by hand.

> Community project. Not affiliated with or endorsed by Wildix. Check findings against the PBX before acting on them.

## The skills

| Skill | Answers | You provide | You get |
|---|---|---|---|
| [wildix-diagnostics](#wildix-diagnostics) | *What happened on this call?* | A Diagnostics Hub / autotrace archive plus a description of the problem | Findings with log and SIP/RTP evidence, likely causes, next steps |
| [wildix-config-review](#wildix-config-review) | *Why is the PBX set up to do that, and what should change?* | A recent WMS system backup | Call-flow traces, Mermaid diagrams, ranked WMS change recommendations |
| [wildix-config-docs](#wildix-config-docs) | *Where's the documentation for this PBX?* | A recent WMS system backup, the Hudu company name and a **Hudu MCP connection** | A standardized Hudu KB article per PBX, saved as a draft |
| [wildix-call-flow-sim](#wildix-call-flow-sim) | *What will happen to a call at 6 pm on a Saturday?* | A WMS system backup, a build plan, or both | One self-contained interactive HTML page |

They chain together. Diagnostics explains a failed call. Config review shows which settings caused it and what to change. The simulator lets the client see the fix before it's built. Config docs keeps Hudu up to date afterwards.

### wildix-diagnostics

Analyzes a Wildix autotrace archive to find the cause of dropped or failed calls, one-way or no audio, registration failures, calls that don't ring, routing errors and trunk problems.

**Required**
- **A Diagnostics Hub / autotrace archive** (`autotrace_logs-*.tar.gz`) covering the time of the incident.
- **A description of the problem**: when it happened (date, approximate time, and the timezone you're quoting), plus the extension, caller ID or user involved when you have them.
  - The easiest way to get both is for the affected user to dial **77777** ("report a problem") right after the issue. The archive then contains `reports/report_<ext>_<date>_<time>.json` with their extension, the PBX-local time and their own description. The skill reads it before asking you anything.

**What it does.** `wildix_analyzer.py` detects the PBX timezone and filters `wms.log`, `cw.log`, `authorization.log` and the SIP/RTP pcaps to the incident window and the call identifiers. It decodes the pcaps itself, without Wireshark or tshark, into:
- a SIP ladder
- an RTP stream summary (direction, packet count, estimated loss, codec)
- the full SIP messages

Claude reads the filtered report and builds the timeline: who sent the BYE, whether media flowed, which errors line up. It then reports findings with the log lines as evidence. Filtered `.pcap` files are kept so you can open them in Wireshark.

### wildix-config-review

Reviews a PBX's configuration from a WMS backup: dialplans, call groups and queues, park orbits and park timeout, user forwards and no-answer settings, function keys and trunks.

**Required**
- **A recent WMS system backup** (`<hostname>_<date>_<time>_<version>_<serial>_wms_b_<id>.tar.gz`). In WMS, go to **Settings → Backup** and create or download a system backup. It must come from the PBX you're asking about, and it should be recent: changes made after the backup don't show up.
- *Optional:* the autotrace archive or diagnostics output for an incident. The skill then checks that both files come from the same PBX (see [Matching a backup to a diagnostic report](#matching-a-backup-to-a-diagnostic-report)).

**What it does.** `wildix_config_parser.py` produces a readable summary of the configuration and a WARNINGS list: loops, dangling Gotos, empty queues, park keys pointing at missing orbits, unused dialplans. It can also:
- trace a number step by step from a trunk, user or dialplan (`--trace`)
- focus on one area, such as park, a queue or a user (`--focus`)
- draw Mermaid flowcharts of inbound routing, park and outbound dialling (`--mermaid`)

Claude ties that to the reported behaviour and recommends changes. Each recommendation gives a WMS click-path, the reason, side effects and how to verify it.

### wildix-config-docs

Writes or updates the client's Wildix configuration article in Hudu: one standardized article per PBX, filed under the client's company.

**Required**
- **A recent WMS system backup** (same as config review).
- **The Hudu company name.** The skill doesn't guess it from the PBX hostname.
- **A Hudu MCP connection.** This is the only skill that needs one. The connection must provide these Hudu tools:
  - `company_index_tool`
  - `article_index_tool`
  - `article_show_tool`
  - `article_create_tool`
  - `article_edit_tool`
  - `article_folder_index_tool` (only used when you name a folder)

**What it does.** The article has:
- an **Important notes** block at the top
- accordion sections: overview, inbound call flow, trunks and numbers, dialplans, call groups, users, call park, outbound dialling, timetables and warnings
- Mermaid call-flow diagrams and a text layout of every dialplan
- **Technician notes** and a **change log**

When the article already exists, it's updated in place. Important notes, technician notes and the change log are carried over word for word. Claude writes only the plain-language summary and the change-log line. Everything else is generated, so the same backup always produces the same article.

Changes are saved as a **draft**. The skill asks before replacing an existing article and publishes only when you say so. It warns before overwriting documentation built from a newer backup.

### wildix-call-flow-sim

Builds a single self-contained HTML page that simulates call handling. Set the day and time, switch positions (office mode, meeting mode), who is free or busy, queue length and what the caller presses. The page then highlights the path through the call-flow diagram, shows the outcome and lists each step.

**Required:** at least one of:
- **A WMS system backup.** Gives the *current* call flows, one per inbound number, using the PBX's real users, call groups, timetables and switches.
- **A build plan** for a *proposed* design: the client's answers in a ticket, notes, a document or the conversation. Claude turns it into a plan JSON (see `references/flow-spec.md`). The page then gains tabs for the build plan, a UAT checklist and open items.

With both, the plan is checked against the PBX. Anything missing is flagged *To create* or *Not found*, and `--with-current` adds the current flows for a before/after comparison.

**Optional:** a ticketing connector (HaloPSA or similar), if you want Claude to read the build plan straight from a ticket.

## Privacy and secrets: what the model sees

A WMS backup contains **plaintext credentials**: SIP trunk passwords, SMTP and LDAP passwords, user PINs, TLS keys. An autotrace contains phone numbers, IP addresses and call records. The skills are built so that **Claude reads the output of local Python scripts, never the raw archives**. The redaction is done in code before anything reaches the model's context, not left to the model's judgement.

### Backup-based skills (config review, config docs, call-flow sim)

All three read the backup through the same parser (`wildix_config_parser.py`). The other two skills get a copy at build time.

1. **The backup is read in memory.** It is never extracted to disk, so there are no loose files for Claude to stumble onto later.
2. **Only the files the parser needs are opened**:
   - the WMS database (`wmsdb.sql`)
   - the LDAP directory (`ldapcat.ldif`)
   - the per-user feature files (`users.tar`)
   - a short list of callweaver/PBX config files (timezone, company, globals, features, queues, SIP trunk peers)

   Nothing else in the archive is read.
3. **The LDAP directory is filtered with an allow-list.** Only the attributes needed to describe users are kept: uid, name, extension, user type, dialplan, group, email, office phone, licence and language. Password hashes and every other attribute are dropped.
4. **Every value whose key looks like a secret is replaced with `[REDACTED]`.** That covers `password`, `passwd`, `secret`, `token`, `pin`, `userpin`, `totp`, `twofactor`, `private`, `api_key`, `auth_user`, `authkey` and `cred*`. The check runs over the whole configuration model before anything is written, so it applies to `config_summary.txt`, `config.json`, traces, diagrams, the Hudu article and the simulator page alike.
5. **Credentials embedded in SIP register strings** (`sip:user:password@host`) are masked to `[REDACTED]:[REDACTED]`.
6. **Secret-looking strings in the compiled per-user Lua files** are dropped as well.
7. **The skill instructions forbid reading the backup directly.** Claude is told never to read, extract or grep the raw backup. If something it needs is missing from the parser output, the fix is to extend the parser and its redaction, not to open the archive.
8. **Outputs never include credentials.**
   - The Hudu article leaves out trunk passwords, SIP auth users, PINs and SMTP/LDAP credentials, and points readers to Hudu Passwords instead.
   - The simulator page leaves them out too. It also shows **only the last 4 digits** of external numbers, such as forwards to someone's mobile (`--full-numbers` overrides this).
   - When writing a plan, Claude describes private numbers ("the owner's cell") instead of writing them.

**What still reaches the model:** configuration and directory data, meaning user names, extensions, email addresses, DIDs, external forward numbers (full in config review and the Hudu article), trunk hostnames and dialplan rules. That's the information you need to reason about a call flow. Treat the parser output as client data.

### wildix-diagnostics

Autotrace logs are evidence, so the analyzer **minimizes them rather than redacting them**:
- The report includes only entries from the incident time window and, when you give them, the extensions, caller IDs or users involved.
- Claude is told to read the filtered report, or grep it when it's large, rather than the full log set.
- The pcaps are decoded locally, and only the SIP signalling and RTP statistics are written. Audio payloads are never decoded or written to the report.
- SIP over TLS (port 5061) stays encrypted and isn't decoded.

The filtered report **does** contain phone numbers, IP addresses, user agents and full SIP messages. Those include digest `Authorization` headers, which carry a hash, not the password. If your policy rules that out, review the report before sharing it, or narrow the window and filters.

### In every case

- Redaction keeps secrets out of the **model's context**. The archives themselves still sit on your machine, or in the sandbox if you upload them to claude.ai, until you delete them.
- `.gitignore` keeps archives (`*.tar.gz`), extracted folders, pcaps, analyzer and parser output and `cases/` out of git. **Never commit a backup or autotrace**, including in issues or pull requests.

## Matching a backup to a diagnostic report

Config advice only makes sense for the PBX where the incident happened. Every PBX has a **serial** (12 hex characters, e.g. `2211000012ab`) and a **domain** (`<name>.wildixin.com`):

| Source | Where the identity is |
|---|---|
| WMS backup | File name: `<hostname>_<date>_<time>_<version>_<serial>_wms_b_<id>.tar.gz`; domain in `config_summary.txt` |
| Diagnostics output | The `PBX:` line in `filtered_report.txt`, and `pbx_identity.json` in the output folder |

Pass the autotrace or diagnostics output to the parser with `--autotrace`. It prints MATCH, or stops with exit code 3 when the files come from different PBXes. Also compare the backup date with the incident date: changes made in between won't show up.

## Requirements

- **Claude with code execution.** Claude Code, or claude.ai / Claude Desktop with code execution enabled. The skills run Python scripts.
- **Python 3.9+, standard library only.** No pip packages, no Wireshark, no tshark.
  - On Windows, IANA timezone names such as `America/Toronto` need the `tzdata` package. Use a UTC offset such as `-0400` instead, or run `python -m pip install tzdata`.
- **Hudu MCP connection:** only for wildix-config-docs (see above).
- **Ticketing connector:** optional, for wildix-call-flow-sim to read a build plan from a ticket.

## Installing

The packaged skills are in `dist/`:

- **claude.ai / Claude Desktop:** upload the `.skill` files under **Settings → Capabilities → Skills**.
- **Claude Code:** unzip each `.skill` into `~/.claude/skills/` (personal) or `<project>/.claude/skills/` (per project). Each package contains a top-level `<skill-name>/` folder.

Install all four for the full workflow. Each skill works on its own; the shared parser is bundled into every package that needs it.

## Repository layout

| Path | What |
|---|---|
| `skills/<name>/SKILL.md` | The workflow Claude follows |
| `skills/<name>/scripts/` | The Python that does the parsing, filtering and redaction |
| `skills/<name>/references/` | Background Claude reads when needed: log guide, config guide, Hudu format, plan spec |
| `skills/<name>/README.md`, `tests/` | Developer notes and unit tests (not packaged) |
| `dist/<name>.skill` | Packaged skills, ready to install |
| `build_skills.py` | Rebuilds the packages |

The tests use a synthetic backup (`skills/wildix-config-review/tests/make_fixture.py`) and an anonymised example plan, so no client data is needed or included.

## Building and testing

```bash
python build_skills.py
```

Pass skill names to build only those (`python build_skills.py wildix-diagnostics`). `tests/`, `__pycache__/` and each skill's `README.md` are left out of the packages. The parser lives in `wildix-config-review` and is copied into the config-docs and call-flow-sim packages, so rebuild all three after changing it.

```bash
python -m unittest discover -s skills/wildix-config-review/tests
```

Run the same command with `wildix-config-docs` or `wildix-call-flow-sim` for their tests.

## Contributing

Issues and pull requests are welcome, especially:
- Wildix behaviour the skills flag as unconfirmed
- dialplan applications the parser doesn't decode yet
- log patterns worth adding to the guides

If a change makes the parser read something new from the backup, extend the redaction to match, and add a test with a fake secret proving it doesn't reach the output. **Never attach real backups, autotraces or unredacted output.** Reproduce problems with the synthetic fixture.
