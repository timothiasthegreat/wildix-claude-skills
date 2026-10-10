# Wildix Log & SIP Interpretation Guide

## Contents
1. What's in the archive
2. Report sections
3. SIP response codes that matter
4. Call-flow patterns and what they mean
5. Audio (RTP/SDP) diagnosis
6. NAT / SIP ALG signs
7. Log line patterns
8. Wildix SIP topology: reading the ladder
9. Call park (SP1/SP2 keys)
10. Known noise: safe to ignore

---

## 1. What's in the archive

| File | What it holds | Timestamp format |
|---|---|---|
| `wms.log`, `wms.log.N.gz` | System syslog: WMS web/API (nginx), kernel IPTABLES drops, service events | `Jun  8 08:28:22` (PBX local, no zone) |
| `cw.log`, `cw.log.N.gz`, `cw_log_archive_YYYYMMDD.tar.gz` | Callweaver (call engine): dialplan execution, channel events, warnings | `[May 19 09:36:16]` (PBX local) |
| `authorization.log` | Login/registration authentication results | syslog style |
| `subscriptions*.log` | BLF/presence subscriptions | (included whole) |
| `siponly.pcapN` | SIP signalling only | UTC epoch |
| `siprtp.pcapN` | SIP + RTP media | UTC epoch |
| `mtr.log`, `ping_result.txt` | Connectivity tests from the PBX (ping to 8.8.8.8) | (included whole) |
| `dialplan.txt` | DID / inbound routing rules | — |
| `public_ip` | PBX's public IP as seen externally | — |
| `reports/report_*.json` | User-triggered reports (dialling 77777) | — |

Pcaps rotate (`.pcap0`, `.pcap1`…). If the capture mode was switched between siponly and siprtp, old files from the other mode can linger for weeks. The report flags these as anomalous.

## 2. Report sections

- **Header**: detected PBX timezone, window in user TZ and UTC, active call filter
- **Static context**: public IP, ping, MTR, user reports, dialplan, subscriptions
- **WMS / AUTHORIZATION / CALLWEAVER LOG**: entries in the window, each prefixed with its time converted to the user's timezone (the original PBX-local timestamp follows)
- **SIP CAPTURE** per pcap:
  - *SIP LADDER*: `time [Cn] src:port -> dst:port PROTO first-line (CSeq ...)`, where `Cn` is a short alias for a Call-ID (legend below the ladder)
  - *RTP STREAMS*: one line per direction: duration, packets, `est_lost` (from sequence-number gaps), codec, SSRC changes
  - *FULL SIP MESSAGES*: complete headers + SDP

## 3. SIP response codes that matter

| Code | Meaning | Typical cause on Wildix |
|---|---|---|
| 100 Trying / 180 Ringing / 183 Session Progress | Normal progress | 183 with SDP = early media (ringback/announcements) |
| 401 / 407 | Auth challenge | **Normal once** per REGISTER/INVITE. A problem only if the retry with credentials gets 401/403 again |
| 403 Forbidden | Rejected | Wrong password, blocked IP, trunk not allowing that caller ID, user lacks permission for that route |
| 404 Not Found | No such number/user | Dialplan has no match, typo in number, unregistered destination |
| 408 Request Timeout | No response | Device offline, network loss, firewall dropping |
| 480 Temporarily Unavailable | Destination not reachable | Phone unregistered, DND |
| 481 Call/Transaction Does Not Exist | Dialog lost | Often follows a NAT rebinding or a PBX restart mid-call |
| 486 Busy Here / 600 Busy Everywhere | Busy | Expected if the callee was on a call |
| 487 Request Terminated | Caller hung up / CANCEL | Normal after CANCEL; look at *why* the caller cancelled |
| 488 Not Acceptable Here | SDP rejected | Codec mismatch, SRTP vs RTP mismatch |
| 500 / 503 | Server error / unavailable | PBX or carrier trouble. 503 from a trunk often means carrier outage or channel limit |
| 603 Decline | Rejected by user/carrier | Call screening, carrier blocking |

## 4. Call-flow patterns

- **INVITE → 200 OK → no ACK, then BYE ~32 s later**: the ACK never arrived. Classic NAT/firewall/SIP ALG problem. Call connects then drops at ~30 seconds.
- **INVITE retransmitted several times, no response**: packets not reaching the other side (firewall, wrong IP/port, ISP blocking 5060).
- **REGISTER every few seconds from the same device**: expiry set too low, NAT keepalive problems, or a device fighting with another one using the same credentials.
- **BYE from the network side mid-call with no user action**: carrier timer, session-timer (`Session-Expires`) refresh failure (look for a re-INVITE/UPDATE that got no answer), or RTP timeout on the PBX.
- **CANCEL shortly after INVITE**: caller gave up. Check whether 180 Ringing was ever sent (caller heard silence?).
- **Re-INVITE with `a=sendonly`/`a=inactive`**: hold. Problems resuming often show as a missing re-INVITE with `sendrecv`.

## 5. Audio diagnosis

1. Find the SDP in the INVITE (offer) and 200 OK (answer). Note each side's `c=IN IP4 <ip>` and `m=audio <port> RTP/AVP <payload types>`.
2. In RTP STREAMS, look for one stream **to** each side's SDP IP:port.
   - Both directions present, low loss → media OK, look elsewhere (handset, echo, codec quality).
   - Only one direction → **one-way audio**. The missing direction tells you which side can't receive. Usually NAT (media sent to a private IP), a firewall, or SIP ALG rewriting.
   - No RTP at all after 200 OK → no audio both ways, or the capture is siponly (check the pcap name).
   - RTP going to an RFC1918 address (10.x, 172.16–31.x, 192.168.x) that isn't on the PBX's LAN → NAT problem.
3. Codec: answer's payload type must be one offered. PT 0 = G.711u, 8 = G.711a, 9 = G.722, 18 = G.729, 101 = DTMF events.
4. Loss: `est_lost` above ~1–2% is audible. Above 5% is choppy/robotic. Bursty gaps point to the network, Wi-Fi, or congestion.
5. `ssrc_changes` > 0 mid-stream: the media source switched (transfer, re-INVITE, or a media relay change). Fine if it lines up with a SIP event, suspicious otherwise.

## 6. NAT / SIP ALG signs

- Via/Contact headers containing a private IP while the packet's source IP is public
- SDP `c=` private IP from a remote phone
- `received=`/`rport=` values on Via that differ from the Via host
- Headers rewritten oddly, or SIP over UDP 5060 working intermittently while 5061/TLS works: router SIP ALG. The standard fix is to disable SIP ALG on the site router.
- `public_ip` in the archive doesn't match the IP the carrier/trunk sees

## 7. Log line patterns

**wms.log**
- `IPTABLES DROP ... SRC=<ip> ... DPT=<port>`: PBX firewall dropped traffic. Repeated drops from a phone's or carrier's IP = blocked legitimate traffic. Scattered drops from random foreign IPs on 5060 = normal internet scanning noise.
- nginx lines `[08/Jun/2026:08:28:35 -0500]`: web/API access. The offset is how the PBX timezone gets detected.

**cw.log (callweaver)**
- `Executing Dial(...)` / `Executing Goto(...)`: dialplan steps, which show how the call was routed
- `Unknown context`, `No such extension`, `Jump to ... failed`: dialplan misconfiguration
- `Unable to create channel`, `Congestion`, `CHANUNAVAIL`: destination/trunk unavailable
- `Hangup cause` values: 16 = normal clearing, 17 = busy, 19 = no answer, 21 = rejected, 34 = no circuit available, 38 = network out of order, 127 = interworking/unspecified
- Calls to **77777**: the user dialled the "report a problem" code, which marks when they noticed the issue

**authorization.log**
- Repeated failures for one user = wrong saved password on a device. Failures for many usernames from one IP = brute-force attempt (cross-check IPTABLES).

## 8. Wildix SIP topology: reading the ladder

Each call shows up in the capture several times, because a Wildix PBX has two SIP layers:

- **kamailio** (SIP proxy) on `127.0.0.1:5060` faces the outside world: phones on the PBX's LAN/public IP (e.g. `10.0.0.10:5060 <-> <phone public IP>:<port>`), and trunks.
- **callweaver** (call engine) on `127.0.0.1:6050` talks only to kamailio over loopback.

So a phone call appears twice: `phone <-> 172.x:5060`, then mirrored as `127.0.0.1:5060 <-> 127.0.0.1:6050`.

**Cloud/SIP trunks connect over TLS** through the Wildix SBC (e.g. `18.x.x.x:5061`). That outside leg is encrypted and not in the ladder. You only see its loopback mirror (`127.0.0.1:5060 <-> 6050`, with `transport=tls` in the URI). The carrier is usually visible in the trunk Call-ID domain (e.g. `@IRISMSC10.iristel.net` = Iristel).

**Who hung up?** Find the BYE for that Call-ID:
- `127.0.0.1:5060 -> 127.0.0.1:6050` BYE on a trunk call: the hangup came **from outside** (caller or carrier). Confirm via the `From` tag (the caller's tag) and the `Via` chain through the SBC. A `Reason:` header, if present, gives the carrier's cause. None means a normal clearing.
- `127.0.0.1:6050 -> 127.0.0.1:5060` BYE: **callweaver** (the PBX) ended it. Look in cw.log at that second for why.
- `<phone> -> 172.x:5060` BYE: the phone/user hung up.
- In cw.log, `Send request CANCEL to peer <ext>` with `Reason cause: 16 (Normal Clearing)` while that extension is ringing usually just means the other party went away first. Check for a BYE on the trunk leg in the same second.

**Quiet ladder.** Busy PBXes are dominated by keepalives. Filter them out when reading:
```bash
grep -E "^  20" filtered_report.txt | grep -vE "OPTIONS|NOTIFY|PUBLISH|SUBSCRIBE|REGISTER"
```
Then follow one call via its `[Cn]` tags and the Call-ID legend.

**Timing heuristics:** BYE ~32 s after 200 OK = ACK never arrived (timer H/B). If the ACK *is* in the ladder, look elsewhere (caller hung up, carrier session/media timer). Hangups on multiple trunk calls within a second or two of each other suggest a shared upstream event (carrier/SBC), but with only two calls it can be coincidence. Say so rather than concluding.

## 9. Call park (SP1/SP2 keys)

"SP1/SP2" on a Wildix phone are typically **park slot keys**, backed by park-orbit users (e.g. 901, 902). In the logs:
- Parking: the phone sends `REFER` on the call, callweaver dials the orbit (`INVITE sip:901@127.0.0.1` answered by `100 Fake Trying` / `180 Fake Ringing`) while the caller hears music on hold.
- Park timeout: cw.log `Nobody picked up in 45000 ms` → `Stopped music on hold` → `CANCEL to peer 901` → `Return from 'transfer' to <ext> (parkOrbit)` → `Dial(SIP/<ext>...)` with caller ID prefixed `R901-PARKED ...`. The parker's phone rings again ("rang for a cycle").
- If nobody answers the return ring, the caller keeps waiting. Callers who hang up then show as a trunk-side BYE (section 8). An immediate callback from the same number seconds later is strong evidence the caller hung up rather than being cut off by a fault.
- Timeline arithmetic (time parked → timeout → time ringing back → hangup) usually explains "both calls dropped" reports.

## 10. Known noise: safe to ignore

These appear constantly on healthy systems. Don't report them as findings unless they line up exactly with the failure and something else points at them:
- cw.log `Abnormal 'Gosub(...)' exit. Popping routine return locations`: normal Wildix dialplan behaviour
- cw.log `Spawn extension (...) exited non-zero`: normal at call end
- `Got response from peer <ext> to incoming request PUBLISH: '412 Conditional request failed'`: BLF/presence refresh
- `Received AVP profile in audio offer but AVPF is enabled`: harmless profile mismatch, call proceeds
- `Hard hangup called by thread ... Expect a failure`: callweaver internal race at hangup time. It's a consequence of the hangup, not the cause.
- wms.log `userFeatures ... Could not load user (nil) features: Invalid user ident`: fires on every queue/call-group dial
- wms.log `mariadbd ... Aborted connection ... 'cdr'`, `kamailio ... dns_hash_put(): unlinked item`, `sudo: pam_unix` session lines
- subscriptions logs consisting only of `00 subscriptions for user N` lines
