---
name: wildix-config-docs
description: Write or update a client's Wildix PBX configuration documentation in Hudu from a standard WMS system backup (*_wms_b_*.tar.gz). Produces one standardized Hudu KB article per PBX under the client's company, with a preserved Important notes block at the top and accordion sections (overview, inbound call flow, trunks and numbers, dialplans, call groups, users, call park, outbound dialling, timetables, warnings, technician notes, change log), Mermaid call-flow diagrams and text layouts of every dialplan. Creates the article if there isn't one and updates it in place (keeping technician notes and the change log) if there is. Use this whenever the user wants to document a Wildix PBX, write up or refresh a client's phone-system setup in Hudu, "put the PBX config in Hudu", or update Hudu after changing a Wildix system, even if they only say "document Acme's phones from this backup".
---

# Wildix Config Docs (Hudu)

Turn a WMS backup into the client's **Wildix PBX configuration article in Hudu**: one article per PBX, in a fixed format, created as a draft and published only when the user says so. Claude never connects to or changes the PBX.

`scripts/wildix_hudu_doc.py` (stdlib only, Python 3.9+) reads the backup through the wildix-config-review parser (credentials redacted) and writes:
- `hudu_article.html`: the full article body, ready for Hudu. It's generated: the same backup always gives the same HTML
- `hudu_meta.json`: title, PBX identity (hostname, serial, domain), backup date, counts, and what was carried over from the previous article

`references/hudu-article-format.md` describes the format: section ids, accordion and Mermaid markup, title convention, and which parts are generated and which are preserved.

**Credentials:** a WMS backup contains plaintext secrets. The parser redacts them, and the article never includes trunk passwords, SIP auth users, PINs or SMTP/LDAP credentials. **Never read, extract or grep the raw backup yourself**, and never add credentials to the article by hand. They belong in Hudu passwords.

## Step 1: Inputs

You need two things:
1. **The backup.** Look in the working folder or a path the user gives for `*_wms_b_*.tar.gz`. The name encodes `<hostname>_<YYYY.MM.DD>_<HH.MM.SS>_<version>_<serial>_wms_b_<id>`. If there are several, list them with hostname, serial and date, and ask which one. If there's none, tell the user how to get one (WMS → **Settings → Backup** → create/download a system backup) and stop.
2. **The Hudu company name, from the user.** The user names the company the documentation belongs to. If they haven't, ask. Don't guess it from the PBX hostname or domain: hostnames are often abbreviations or old names, and documenting under the wrong client is worse than asking.

## Step 2: Resolve the company in Hudu

`company_index_tool(q="<name>")`:
- exactly one match: use its `id`
- several matches: list them (name + id) and ask which one
- no match: try a shorter or partial name once, then show what's close and ask. Don't create companies.

Sanity check: if the company name and the PBX hostname/domain obviously belong to different organisations (e.g. company "Acme Dental", domain `smithlaw.wildixin.com`), say so and confirm before continuing.

## Step 3: Look for an existing article

`article_index_tool(company_id=<id>, q="Wildix - PBX Configuration")`, then `article_show_tool` on each candidate. The Overview table holds the **PBX hostname**, **Serial** and **Backup taken**.

- **Same serial** (or same hostname with no serial on the page): this is the article to update. Note its `id`, then write `<output>/previous_article.html` (create the output folder first) for `--previous` in Step 4.
  - A full article is usually too big for the tool result, so the result is saved to a file. Write the whole `content` from that file with Python (`json.load(...)["content"]`, or slice from the first `{` if there's a preamble). Never re-type it.
  - If the content came back inline, write only the parts the script carries over or checks, copied **character for character** (don't tidy, re-indent or "fix" them). Leave the generated sections out, since they're rebuilt anyway:
  1. the whole `<div id="wx-important">…</div>` block at the top
  2. the Overview facts table (the `<table>` with the **PBX hostname**, **Serial** and **Backup taken** rows)
  3. the whole `<details … id="wx-notes">…</details>` section
  4. the whole `<details … id="wx-changelog">…</details>` section

  Hudu's editor drops the accordion ids when someone saves the article (see the reference doc). In that case copy the block that starts with the `Important notes` heading, or the `<details>` whose summary is `Technician notes` / `Change log`. If the script says it **couldn't read the serial** from the previous article, confirm with the user that it's the same PBX before replacing it.
- **The company has a Wildix article for a different serial:** that's another PBX (multi-site client) or a replaced PBX. Ask: update that one (replacement) or create a new article (second PBX)?
- **None:** you'll create a new article.

If the existing article was built from a **newer** backup than this one, the script warns. Stop and ask before replacing newer documentation with older data.

## Step 4: Generate the article

```bash
python "<skill-dir>/scripts/wildix_hudu_doc.py" "<backup>.tar.gz" --company "<Hudu company name>" \
  --output "<folder-next-to-backup>/<backup-name-without-.tar.gz>-hudu" \
  [--previous "<output>/previous_article.html"]
```

Use `python` on Windows and `python3` on macOS/Linux. It prints the title and, with `--previous`, how many notes/change-log rows were kept, plus warnings for a serial mismatch or an older backup. Take those warnings seriously (Step 3).

## Step 5: Write the summary and change-log entry

Read `hudu_article.html` (and `hudu_meta.json`). The **Important notes** block at the top and the **Technician notes** section belong to people: never write into them, even to add a warning you think matters (put that in the summary instead). On an update they are carried over verbatim. Check that `hudu_meta.json` → `previous.important_kept` / `notes_kept` are true when the old article had them, and if not, stop and tell the user before saving anything. Two parts are written by you, everything else is generated:

1. **Summary** (always): 2–4 sentences of plain language for whoever opens the article next, technician or client. Cover how inbound calls are answered (who rings first, call groups, after-hours/timetables, where unanswered calls end up), anything unusual, and the most important configuration warnings. Base every statement on the generated content. Mark anything the backup can't confirm ("per config, probably…"). Write it to `<output>/summary.txt` (blank line between paragraphs).
2. **Change log** (updates only): compare the previous article with the new one and describe what changed in one line: users added/removed, DID routing, call-group members or timeouts, forwards, park settings, trunks. If nothing material changed, say "No configuration changes; refreshed from a newer backup."

Then re-run the Step 4 command with `--summary-file "<output>/summary.txt"` and, for updates, `--changelog "<one line>"`. Don't hand-edit `hudu_article.html`: regenerating keeps the format standard.

## Step 6: Save to Hudu as a draft

Pass the **file content exactly as written** as `content` (it is already TinyMCE-clean HTML: don't convert, wrap or tidy it).

- **New article:** `article_create_tool(company_id=<id>, name=<meta title>, content=<html>, draft=true)`. No `folder_id`: client articles live under the company. Use a folder only if the user names one (find its id with `article_folder_index_tool(company_id=<id>)`).
- **Update:** first tell the user what will be replaced (the article name and link, that generated sections are rewritten, and that the Important notes, technician notes and N change-log rows are kept) and wait for a yes. Then `article_edit_tool(id=<id>, name=<meta title>, content=<html>, draft=true)`.

## Step 7: Report and publish

Give the user:
- the Hudu link (`hudu_url`) and whether it was created or updated
- the PBX: hostname, serial, backup date
- the summary, and for an update the change-log line
- configuration warnings worth acting on (from the warnings section). For recommendations on fixing them, offer the wildix-config-review skill

Ask whether to publish. On a yes: `article_edit_tool(id=<id>, draft=false)`. If the user said in the request that it should be published, still create the draft first, check the result, then publish and say so.

## Follow-ups

Offer to:
- document another PBX for the same client (a separate article)
- trace a specific number or explain a call flow in more detail (wildix-config-review)
- re-run after changes are made in WMS, from a new backup
