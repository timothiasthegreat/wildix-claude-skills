# Hudu article format

One KB article per PBX, under the client's company in Hudu. `wildix_hudu_doc.py` generates all of it except the summary text, the change-log line (both supplied by Claude through `--summary-file` / `--changelog`) and the Technician notes (written by people in Hudu).

## Title

`Configuration - Wildix - PBX Configuration (<hostname>)`, following the instance's `Type - Vendor - Topic` naming. The hostname comes from the backup file name.

## Important notes (top of the page)

Above everything else, **not** an accordion, so it can't be missed:

```html
<div id="wx-important"><h3>Important notes</h3>
<p>...written by people in Hudu...</p>
</div>
<hr>
```

The generator writes a placeholder only on a new article. On every update the whole `<div>` (heading included) is copied verbatim from the previous article: it's found by id, or by its `Important notes` heading if an editor dropped the id (which is then restored). Nested divs are balanced. Claude never writes or edits it.

## Sections

Every section is a TinyMCE accordion, which Hudu stores as-is:

```html
<details class="mce-accordion" id="wx-overview" open="open"><summary>Overview</summary>
...
</details>
```

Only Overview is open by default. Order and ids are fixed. `--previous` relies on the ids, so don't rename them:

| id | Summary text | Content | On regenerate |
|---|---|---|---|
| `wx-overview` | Overview | `<div id="wx-summary">` (Claude's summary), PBX facts table, incoming numbers → plain-language path | regenerated |
| `wx-inbound` | Inbound call flow | legend + Mermaid inbound diagram | regenerated |
| `wx-trunks` | Trunks and numbers | trunks table (no credentials), trunk groups | regenerated |
| `wx-dialplans` | Dialplans | one `<h4>` + `<pre>` text layout per dialplan; Mermaid diagram of unreached dialplans | regenerated |
| `wx-queues` | Call groups | members, strategy, ring/retry/wrap-up, which rules use it and their max wait | regenerated |
| `wx-users` | Users and extensions | users with forwards per call class; other extensions (park orbits etc.) | regenerated |
| `wx-park` | Call park | timeout, ring-back, orbits, park keys, Mermaid park diagram | regenerated |
| `wx-outbound` | Outbound dialling | legend + Mermaid outbound diagram | regenerated |
| `wx-timetables` | Timetables and switches | timetables with periods; switches | regenerated |
| `wx-warnings` | Configuration warnings | parser warnings | regenerated |
| `wx-notes` | Technician notes | free text written in Hudu | **kept verbatim** |
| `wx-changelog` | Change log | table: documented date, backup taken, changes (newest first) | **rows kept**, new row added |

Sections with nothing to show say "None configured." rather than disappearing, so every article has the same shape.

`--previous` finds a preserved section by `id`, or by its summary text if an editor dropped the id, and balances nested `<details>`, so technicians can use accordions inside their notes. It also reads **Serial**, **PBX hostname** and **Backup taken** from the previous Overview table to detect a different PBX or an older backup.

## What Hudu's editor does on save

An article saved through the API keeps this markup exactly. Once someone edits and saves it in Hudu's editor (seen 2026-10-02), the editor rewrites it:
- adds `data-id="<uuid>"` to most elements and `id`/`data-toc-id` to headings
- **drops the `id`s on the accordions**. `div#wx-important` and `div#wx-summary` keep theirs
- wraps each accordion body in `<div class="mce-accordion-body" data-mce-bogus="1">`
- moves table header rows into `<tbody>` as `<th>` cells, wraps cell text in `<p>`, and adds `<colgroup>`
- turns `<hr>` into `<div data-type="horizontalRule"><hr></div>`
- turns every `<pre>` into `<pre data-id><code>`. A plain `<pre class="mermaid">` loses its class and stops rendering, which is why the generator uses the editor's own Mermaid block (below)

The script copes with all of this when reading `--previous`: sections are matched by summary text, the label/value cells by loose patterns, and `<th>` rows are skipped in the change log. Each regenerate writes clean markup again, so diagrams render after an update even if an edit broke them.

## Mermaid

Diagrams use the markup of Hudu's editor's own Mermaid block (`/code`, language Mermaid). Tested 2026-10-02: it survives an editor save unchanged, apart from a `data-id`:

```html
<pre class="language-mermaid"><code class="language-mermaid">flowchart TD
    ...
</code></pre>
```

`<pre class="mermaid">` (Hudu's documented form) renders when saved through the API, but the editor turns it into a plain code block on the next save. `<pre><code class="language-mermaid">` is normalised to the form above. `<div class="mermaid">` gets flattened into a paragraph. The source is HTML-escaped text (`&lt;br/&gt;`, `--&gt;`), so `<br/>` in labels stays text for Mermaid instead of becoming a real line break. `classDef` colours come from the parser's `MERMAID_CLASSES`: blue = PBX step, green = answered, grey = voicemail/forward, red = ends without a person, dashed = unreached. Diagrams are capped at about 220 nodes; a truncated diagram ends with a `%% truncated` comment.

## Dialplan text layout

```
[inbound]  From carrier
  used by trunks: Example Trunk

  +15550100
     1. DialPhone(201, timeout_s=20)  -> 201 (Front Desk)   [rule 21]
     2. Queue(1, options=t, max_wait_s=30)  -> call group 'Reception'   [rule 22]
```

Numbers are sorted most-specific first (the order the PBX matches them). Rule ids match `dialplan_rules.id` in WMS, so a technician can find the rule.
