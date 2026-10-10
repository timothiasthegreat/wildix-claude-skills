# wildix-config-docs

Writes a client's Wildix PBX configuration documentation into Hudu from a standard WMS system backup (`*_wms_b_*.tar.gz`). The result is one standardized KB article per PBX, under the client's company: accordion sections, Mermaid call-flow diagrams and text layouts of every dialplan. Claude creates the article, or updates it in place keeping technician notes and the change log, as a draft through the Hudu connector, and publishes only when the user says so. Never connects to the PBX.

| Path | What |
|---|---|
| `SKILL.md` | The workflow Claude follows (company lookup, find/update article, draft, publish) |
| `scripts/wildix_hudu_doc.py` | Builds `hudu_article.html` + `hudu_meta.json` from the backup |
| `references/hudu-article-format.md` | Section ids, markup and what is generated vs preserved |
| `tests/` | Unit tests against wildix-config-review's synthetic fixture (not packaged) |

The script uses `wildix-config-review/scripts/wildix_config_parser.py` and `lua51.py`. In the repo it imports them from there. `build_skills.py` copies them into this skill's package (`SHARED`), so the parser has only one source. A parser change that affects this skill needs both skills rebuilt.

## Running the script directly

```bash
python skills/wildix-config-docs/scripts/wildix_hudu_doc.py <backup>_wms_b_x.tar.gz --company "Acme Ltd" --output <backup>_wms_b_x-hudu
python skills/wildix-config-docs/scripts/wildix_hudu_doc.py <backup>.tar.gz --company "Acme Ltd" --output out \
  --previous out/previous_article.html --summary-file out/summary.txt --changelog "Added user 214"
```

## Tests

```bash
python -m unittest discover -s skills/wildix-config-docs/tests
```
