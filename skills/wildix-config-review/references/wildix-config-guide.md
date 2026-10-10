# Wildix Config Guide: WMS backup, dialplans and call-flow playbooks

## Contents
1. What's in a WMS backup
2. Dialplan tables
3. Dialplan applications and their parameters
4. Number patterns and matching order
5. Users, call classes and call features
6. Call park
7. Call groups / queues
8. Trunks
9. Credentials in the backup
10. Playbooks: parking, queue no-answer, after-hours, inbound DIDs, forwarding surprises

Items marked **(unconfirmed)** come from reading the config, not from a confirmed Wildix test. Phrase them as "probably" and suggest a test call with an autotrace.

## 1. What's in a WMS backup

`<hostname>_<YYYY.MM.DD>_<HH.MM.SS>_<version>_<serial>_wms_b_<id>.tar.gz`, taken at PBX local time.

| Member | Contents | Used by parser |
|---|---|---|
| `wmsdb.sql` | SQLite text dump: dialplans, call groups, trunks, timetables, switches, IVR, paging, `system` | yes |
| `ldapcat.ldif` | Users and groups (LDAP): extension, name, `userKind`, `userDialplan`, group | yes (whitelisted attributes) |
| `configs.tar` | `/etc` snapshot: `callweaver/*.conf`, `timezone`, `pbx/company.conf`, kamailio, nginx… | selected files only |
| `users.tar` | `<uid>.lua`: per-user call features and function keys, **compiled Lua 5.1** | yes (`lua51.py`) |
| `astdb.sqlite3`, `devdb`, `htables.db`, `presentitydump.db` | runtime DB, provisioning, peers, presence | no |
| `cust_sounds.tar`, `voicemail_metadata.tar`, `calendardb`, `cticonnectdb` | prompts, VM metadata, calendars | no |

Useful `configs.tar` files:
- `callweaver/extensions-variables.conf`: globals. **`GPARK_TIMEOUT`** (ms), `GINTDIAL_TIMEOUT` (s), `GCOUNTRY_CODE`, `GNATIONAL_PREFIX`, `GINTERNATIONAL_PREFIX`
- `callweaver/queues-entities.conf`: queues as rendered for callweaver (`[<call group id>]`, `member => Local/<ext>@internalcalls,<penalty>`)
- `callweaver/sip-extra.conf`: user-added SIP trunks. `callweaver/sip-<name>.conf` (e.g. `sip-classound.conf`) holds Wildix-managed trunks
- `callweaver/res_features.lua`: feature-code map (`*61`… see §5)
- `timezone`, `pbx/company.conf` (`pbx_domain`)

## 2. Dialplan tables

- `dialplan_rulenames(id, rulename, ruledescription)`: one row per dialplan ("procedure")
- `dialplan_rules(id, rule_id → dialplan id, number, priority, application, parameters, dialplanappname, comment)`: one row per action. Rows with the same `number` form one rule, run in `priority` order (0, 1, 2…). The parser prints the row `id` as `[rule N]`, which identifies a row precisely
- `dialplan_rules_includes(rule_id, priority, include_rule_id)`: dialplan includes. Negative ids are built-ins (e.g. `-4` on `users`)
- `dialplan_links(object_from_id, object_to_type, object_to_id)`: WMS's reference index. `to_type` 1 = dialplan, 2 = trunk, 3 = call group

Which dialplan handles a call:
- **Inbound on a trunk**: the trunk's `context` (shown as "inbound dialplan"). The number matched is the DID as the carrier sends it, usually E.164 `+1…`
- **Dialled by a user**: the user's `userDialplan` (LDAP), typically `users`
- **Goto/GotoIf**: the named dialplan. An empty dialplan field means the current one

If no rule matches an internal extension, built-in routing rings that user. That's why most dialplans have no rules for plain extensions.

## 3. Dialplan applications and their parameters

`parameters` is a quoted CSV (`"a","b\,c"`). `CustomApp` holds raw callweaver syntax instead. Position names marked `?` aren't confirmed.

| application (dialplanappname) | parameters | Behaviour |
|---|---|---|
| Set (app-set) | variable, value | Sets a channel variable. `__CALLTYPE` tags the call; `CALLERID(name)` changes the displayed name (e.g. `PARKED ${name}`); `SIP_CODECS` limits codecs |
| Goto (app-goto-general) | number, dialplan, priority, ? | Jumps. Following rules in the current number never run |
| GotoIf (app-goto-if) | condition, number, dialplan, priority, ? | Jumps if the condition is true, otherwise continues |
| GotoIfSwitchIsOn (app-goto-if-switch-is-on, "Jump to if switch is on") | switch id, number, dialplan, priority, ? | Jumps if the 2-state switch is on, otherwise continues |
| GotoIf3SwitchIsOnOrBlinking (app-goto-if-three-switch-is-on-or-blinking, "Jump to if 3 state switch is on") | switch id, number_on, dialplan_on, priority_on, ?, number_blinking, dialplan_blinking, priority_blinking, ? | Jumps to one target when the 3-state switch is on and another when it's blinking. Off continues |
| GotoIfCallType (app-goto-if-call-type, "Jump to if call type is") | call type (e.g. `blacklist`), number, dialplan, priority, ? | Jumps if the call has that type, otherwise continues |
| GotoIfBusyCongested (app-goto-if-dialstatus, "Jump to if congested / busy") | dial status (`BUSY`), number, dialplan, priority, ? | Jumps if the last Dial ended with that status, otherwise continues |
| Answer | (none) | Answers the call |
| DialPhone (app-dial-internalcall) | number, timeout_s, options, ? | Rings a user/extension. An empty timeout uses the user's own no-answer timeout. When unanswered, the next rule runs, **unless the user's call features forward the call first** (§5, **unconfirmed** in edge cases) |
| DialTrunk (app-dial-trunk) | number, trunk (URL-encoded name), ?, ?, ?, call class | Outbound call. `call_class` 3 national, 5 emergency, `auto` |
| Queue (app-queue) | call group id, options, ?, ?, **max wait s**, music class | Puts the caller in a call group. When max wait expires (or immediately if `joinempty` rejects), the next rule runs. `t` = callee may transfer |
| Playback (app-playback) | sound, no_answer | Plays a prompt, then continues. `no_answer` is the editor's "Do Not Answer The Call" |
| Timetable ("Verify timetable" in the editor) | timetable id, number, dialplan, priority? | If now is inside the timetable, jumps to number@dialplan; otherwise the next rule runs. The editor shows only the first three (`timetableId`, `number`, `dialplan`); the 4th raw value is probably the jump priority |
| Background (app-playback-waitDigits) + WaitExten | sound / timeout s | IVR. Digits dialled are looked up as numbers **in the same dialplan**. On timeout, the next rule runs |
| VoiceMail (app-voicemail) | mailbox, options | Ends in voicemail |
| FaxToEmail | email, format, station id, options | Receives a fax and emails it (`destination`, `format` e.g. pdf, `localstationId`). Ends the call |
| MuxMon (app-mux-mon) | …, options | Call recording |
| CustomApp | raw `App(args)` | Anything (`NoOp`, `Set(DB(...))`, `System`, `Record`). Read it literally |

Switches (2-state and 3-state, `wmsswitches`) are **not** tied to a timetable: they are logic gates that dialplan rules test. Their state is kept in the AstDB as `TIMETABLE/<switch id>` (a feature code can set it with `Set(DB(TIMETABLE/<id>)=…)`). Don't confuse a switch id with a timetable id: both tables number from 1. Rules test them with GotoIfSwitchIsOn (2-state) and GotoIf3SwitchIsOnOrBlinking (3-state). `wmsswitches.swtype` 3 = 3-state, anything else 2-state. `state` 0 = off, 1 = on (WMS shows "enabled"), 2 = blinking (WMS shows "extra"). For timetables, `timetable_main.state` 1 = "check time" (follow the schedule); other timetable states aren't confirmed yet. `pbxinternal` in a jump target is WMS's built-in "Users and call groups" dialplan (not in the backup).

A rule that ends with `Goto` back to its own number creates an endless loop. Callers cycle (queue → prompt → queue…) until answered or they hang up. Sometimes that's intended ("keep trying"), but it means no voicemail fallback.

## 4. Number patterns and matching order

Wildix numbers are Asterisk patterns, usually written without the leading `_`:
`X` 0-9, `Z` 1-9, `N` 2-9, `[1-5]` set or range, `.` one or more of anything, `!` zero or more, and anything else is literal (`+`, digits, words like `ivr`, `s`).

Order: an exact number beats any pattern. Between patterns, the one more specific at the first position where they differ wins (`1XXXXXXXXXX` beats `X.`). The parser's `specificity()` approximates Asterisk's ordering, so check ties by hand.

## 5. Users, call classes and call features

`userKind`: `user`, `admin`, `park_orbit`, and others (fax, service…). Group (department/ACL) comes from the LDAP `owner` gid.

Per-user features (`users.tar/<uid>.lua`, table `features`):
- `_classes[<class>][<status>][<CODE>] = {status = "0|1", value = …}`
- class: `external`, `internal`, `whitelist`, `blacklist`, i.e. who is calling
- status: `default` (available), `away`, `dnd`: the user's presence. The parser shows the profile for the current `STATUS.value`, falling back to `default`
- `_ACTIVATED` is kept raw in `config.json`. Its exact meaning is **unconfirmed**

| Code | Feature code | Meaning | Parser label |
|---|---|---|---|
| FCF | *63 | forward all calls → value | fwd-all |
| BCF | *61 | forward if busy → value | fwd-busy |
| UCF | *62 | forward if no answer / unavailable → value | fwd-noanswer |
| TOUT | *65 | no-answer timeout, s (empty = system default) | noanswer-timeout |
| MEXT | *75 | mobility extension (value = delay) | mobility-ext |
| CW | *64 | call waiting | (hidden) |
| REJECT, MOBCONFIRMATION, RING, EMAILMCN, SMSMCN | | reject, mobility confirm, ring style, missed-call notifications | |

`VOICEMAIL` as a forward value means the user's own voicemail.

Function keys: `FKEYS` is a comma-separated list of `TYPE number label …` (empty fields kept, `_` in labels = space). `PORBIT <orbit>` is a park-orbit key ("SP1/SP2"), `EXT <ext>` a BLF, `LINE` a line key.

## 6. Call park

Confirmed from cw.log on a production PBX (WMS 7.08) and a test on a lab PBX (WMS 7.09) unless marked otherwise. Wildix docs: "How to transfer external forwarded and parked calls back to a receptionist" and its child page "How to set up Site Specific Return from Park" (wildix.atlassian.net, DOC space, pages 30285187 and 30277734).

- **Park orbits are users** with `userKind: park_orbit` (e.g. 901 "Park 1", 902 "Park 2").
- **Parking is a transfer to the orbit number, run through the *parker's* dialplan.** The SP1/SP2 key sends a REFER to `901`. The call then runs the rule for `901` in the parker's `userDialplan` (e.g. `[901@users]`). WMS often comments these rules "Call back from Park", but they run **when the call is parked**, not only when it comes back. A typical rule: `Set __CALLTYPE=parked` → `Set CALLERID(name)=PARKED ${name}` → `DialPhone(901)`. If there's no rule, built-in routing does the same DialPhone.
- **Hold:** `DialPhone(<orbit>)` to a park-orbit user becomes `Dial(SIP/901,<GPARK_TIMEOUT s>,m…)`: music on hold, no ringing (cw.log: `Kind of the destination user is 'park orbit'`, `options - music instead ring`).
- **Park timeout** = `GPARK_TIMEOUT` (ms) in `extensions-variables.conf`, the WMS "Park timeout" setting (menu location varies by WMS version). cw.log: `Nobody picked up in <ms> ms`. `features.conf` `parkingtime`/`parkext 700` is the unused Asterisk parking lot. Ignore it.
- **Return:** still inside the same DialPhone, the call rings back **the user who parked it**: `Return from 'transfer' to <ext> (parkOrbit)` → `Calltype 'parked'` → `Dial(SIP/<ext>,<GINTDIAL_TIMEOUT s>,…)`. The phone shows the caller as `R901-PARKED …`. On that production PBX, GINTDIAL_TIMEOUT=65, so the ring-back lasts up to 65 s.
- **The parker's INTERNAL call features apply to the return** (cw.log: `Dst features for active calltype/status - internal/default: … BCF:1(VOICEMAIL) UCF:1(VOICEMAIL) …`):
  - fwd-noanswer → the return probably goes there after GINTDIAL_TIMEOUT
  - fwd-busy → if the parker is on another call when the park times out, the return probably goes there **immediately**
  - **(unconfirmed)** Neither path has been seen in a log yet. Both callers in the case hung up before 65 s.
- Worst case with defaults: park timeout + GINTDIAL_TIMEOUT (45 + 65 = 110 s on that production PBX) of hold music plus ringing before the caller reaches a person or voicemail. Most callers hang up well before that.
- **After the ring-back: the reserved `receptionist` dialplan.** If the parker is busy or doesn't answer the ring-back, the PBX sends the call to the dialplan named exactly `receptionist`, number **`default`** (stored as **`s`** in the backup; the WMS editor shows it as `default`). Nothing points at this dialplan: the PBX enters it itself, so it is never "unused". Confirmed on the test PBX with a parker that had **no** forwards: park timeout (22 s) → ring parker → busy/no answer → `[receptionist] default` → `Goto 8006@queues testing`. The site-specific page uses `Verify caller` rules in `[receptionist]` to route by a caller-name tag set at park time.
  - **(unconfirmed)** Whether a parker's internal fwd-busy / fwd-noanswer takes the call before `[receptionist]`. Probably yes (the ring-back runs with the parker's internal features). Test with a parker that has forwards.
  - No `receptionist` dialplan (or no `default`/`s` number in it): nothing is tried after the ring-back. What the caller then gets isn't confirmed.
- **Return from transfer** uses the same `[receptionist]` dialplan: an unanswered blind or semi-attended transfer returns to the person who transferred, then `[receptionist]`. Wildix disables it by default on PBXs with the USA country code unless the custom global variable `RETURNFROMTRANSFER=yes` is set; park returns aren't affected. `GCOUNTRY_CODE=1` covers all of North America, so with it and no `RETURNFROMTRANSFER` the parser reports transfer returns as probably off.
- **Rules after `DialPhone(<orbit>)`** (e.g. a Goto to a "call back from park" queue) probably run only if the ring-back ends without a forward or `[receptionist]` taking the call. **(unconfirmed)**: prefer `[receptionist] default` for where returns go.
- Check what `[receptionist] default` leads to: a queue that ends with a Goto back to itself loops forever with no voicemail exit.
- The parser's CALL PARK section lists the timeout, orbits, rules on the orbit numbers, and every `PORBIT` key per user. It warns about keys pointing at numbers that aren't orbits. `--trace 901 --from user:<parker>` shows the whole park path, including the parker's forwards.
- Park keys can also come from the **device/template provisioning** rather than the user's FKEYS. If the parser shows no `PORBIT` key for a user who clearly parks with SP1/SP2, check the phone's template in WMS.

## 7. Call groups / queues

`call_groups.queue_parameters` (JSON) and `queues-entities.conf`:
- `strategy` (ringall, linear, leastrecent, fewestcalls, random, rrmemory)
- `timeout` = how long members ring per attempt (s), `retry` = pause between attempts (s), `wrapuptime`, `maxlen` (0 = unlimited)
- `joinempty`/`leavewhenempty` (`invalid` = don't join or leave when all members are invalid/unregistered)
- `ringinuse` (ring members already on a call), `autopause*`, `announce-*`, `queue-cid`

How long the caller waits is the **Queue rule's max wait** (5th parameter), not `timeout`. With max wait empty, the caller waits indefinitely.

Members are `Local/<ext>@internalcalls`, so a member's own call features (DND, forwards) still apply.

## 8. Trunks

`trunks` table: SIP trunks added in WMS. Key settings: `context` (inbound dialplan), `host`, `allow` (codecs), `dtmfmode`, `session_timer`, `session-expires`, `transport_wildix`, `trustrpid`, `preserve_orig_cid`. Registration is in `trunks.register` (credentials redacted). `pstn_trunks` holds FXO/BRI/PRI. Wildix-managed trunks (e.g. ClasSound, `host=operator-pbx.wildix.com`) live in `callweaver/sip-<name>.conf`. A DialTrunk to a name not in either is probably a trunk group (`outgoing_groups_*`).

## 9. Credentials in the backup

The backup contains: `system.SMTP.password`, trunk `secret`/`username` and the register string, LDAP `userPassword`, `wuserPasswordSip`, `userPIN`, TOTP/2FA secrets in user Lua files, TLS/API keys in `configs.tar` (`pbx/wildix/ssl/*`, `api_keys_pair/`, `rsyslog.d/certs/`), `msmtprc`, `sip-mashup.conf`, `cdr_*.conf`, `sip-registration.conf`.

The parser reads none of the key/cert files, keeps only whitelisted LDAP attributes, and replaces values under secret-looking keys with `[REDACTED]`. If you extend the parser, add new fields to the whitelist and check the output with a grep for known secret values before trusting it. Treat the backup itself like a password vault: keep it out of git (`*.tar.gz` is ignored) and delete it when done.

## 10. Playbooks

### Parking: parked calls time out, ring back, callers hang up

Typical evidence: `Nobody picked up in N ms` → `Return from 'transfer' to <parker>` → several returns stacked on one phone → trunk-side BYE (the caller hung up) 60–110 s after parking.

Work out the caller's timeline first: park timeout (`GPARK_TIMEOUT`) + ring-back (`GINTDIAL_TIMEOUT`) → the parker's internal fwd-noanswer / fwd-busy. Then pick from these options, roughly in order of impact:
1. **Make parked calls visible to more phones.** Give the same `PORBIT` keys (SP1/SP2) to every phone that should help: reception, backup, manager. Anyone can then pick up a blinking slot before the timeout. This is configuration only, with no dialplan risk. Check the park-key list and device templates.
2. **Shorten the silent part.** Lower `GINTDIAL_TIMEOUT`, or give the parker a no-answer timeout (TOUT, internal class), so an unanswered ring-back reaches its destination in ~20–30 s instead of 65. Check what else this affects: GINTDIAL_TIMEOUT is the default ring time for **all** internal dialling, and TOUT applies to all internal calls to that user.
3. **Choose where unanswered returns end up.** With a parker forward, probably that forward (often their personal voicemail, which a receptionist may not check). Without one, the reserved `[receptionist] default` rule. Options:
   - build `[receptionist] default` → a "parked call" queue (members = reception/backup, short max wait) → shared voicemail, **not** a Goto loop. This is the Wildix-documented way and also catches unanswered transfers when Return from transfer is on
   - point the parker's internal forwards at a shared voicemail or a reception group instead of their own voicemail, or remove them so `[receptionist]` takes over (§6 unconfirmed)
   - test with a park that times out on a phone with forwards before relying on it
4. **Mind fwd-busy.** If the parker is on another call when the park times out, the return probably goes straight to their fwd-busy destination. With fwd-busy=VOICEMAIL, a busy receptionist's parked callers go to voicemail without ringing anyone.
5. **Tune `GPARK_TIMEOUT` last.** Longer gives the parker more time but means more silent hold. Shorter causes more ring-backs. 45–60 s is common. Raise it only together with options 1–3.
6. **People and process.** Pick up parked calls from the slot key. Answer the ring-back (it shows `R<orbit>-PARKED …`). With several calls parked, the oldest comes back first. Don't park when a transfer to a person or group fits better.

Trade-offs to state: group fallbacks ring other people; loops never reach voicemail; a lower GINTDIAL_TIMEOUT shortens every internal ring; personal voicemail for parked callers is easy to miss.

### Queue: callers wait forever or go nowhere

Check the Queue rule's max wait (empty = forever) and what follows it (a Goto back to itself = a loop with no exit). Check `joinempty` with no logged-in members, members with DND/forwards, `ringinuse=no` with busy members, and `timeout`/`retry` too short for desk phones. Recommend: a max wait, then overflow (second group, voicemail or callback IVR), and announce position/hold time if waits are long.

### After-hours / holidays

Look for timetables and switches (TIMETABLES / SWITCHES section) and the rules using them: Timetable ("Verify timetable"), GotoIfSwitchIsOn / GotoIf3SwitchIsOnOrBlinking, and GotoIf/CustomApp rules on `DB(TIMETABLE/<id>)` or `${TIMETABLESTATE}`. Confirm the timetable covers the hours stated and check each switch's position in the backup. A switch left on (a manual override, e.g. "office closed") is a common cause of "closed message during business hours". Rules run in order, so a switch checked after a timetable only matters when the timetable didn't jump.

### Inbound DID not reaching the right place

Trace from the trunk with the DID exactly as the carrier sends it (check a SIP INVITE in the autotrace: `+1…` vs. 10 digits). No rule match falls through to built-in routing or "number not found". Look for pattern rules catching it first, and for rules that DialPhone a user whose forwards divert the call before later rules run.

### Forwarding surprises

`fwd-all` on a user diverts every call before DialPhone's timeout matters. `fwd-noanswer` to VOICEMAIL means a DialPhone with a following Queue/Goto probably never reaches the next rule. Settings differ per class (external vs. internal) and per presence status (DND/away profiles), so check the profile that applied at the time of the call.
