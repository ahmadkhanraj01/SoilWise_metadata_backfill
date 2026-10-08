### 4. Messy / Malformed Text Data

I scanned all free text fields (`title, abstract, persons, organizations, contact_persons, contact_organizations, subjects, matched_subjects, license, view_authors, view_contacts, soilmission, project_acronyms, projects, sources`) in **Sep25_solrData.json** for:

*   **Emoji**
*   **Pipe** `|`
*   **Backslash** `\`
*   **Control characters** (non-printable bytes)
*   **Mojibake** (encoding artifacts like `Ã©, Ã£` — text that was decoded with the wrong character set)
*   **Unescaped HTML/XML tags** (`<i>`, `<sub>`, `<jats:p>`, etc.) leaking into plain text
*   **Literal URL-encoding** (`%3A`, `%2F`) sitting inside non-link text
*   **Double/irregular spacing**

*(I checked forward slash `/` too, but it's almost entirely legitimate — URLs in `license`, and `subjects` values like "raw data / geochemical mapping" that genuinely use `/` as a separator — so it's not included as a "messy" flag below, though it's in the raw scan if you want it.)*

#### Current state (Sep25), records affected per field/issue

| Field | Issue | Records |
| :--- | :--- | :--- |
| abstract | html_tag (JATS/XML markup leaking in, e.g. `<jats:title>`, `<jats:p>`) | 6,311 |
| abstract | multi_space | 5,402 |
| title | html_tag (`<sub>`, `<i>` tags) | 443 |
| title | multi_space | 580 |
| abstract | pipe `|` | 159 |
| abstract | backslash `\` (mostly LaTeX math: `\Delta, \pm, \text{}`) | 76 |
| abstract | emoji | 70 |
| title | pipe `|` | 17 |
| title | emoji | 5 |
| persons / contact_persons / view_authors / view_contacts | pipe `|` (e.g. `\|Name\|\|`, or a lone `\|` as the entire value) | 40-42 each |
| persons / contact_persons / view_authors / view_contacts | mojibake (Angelo, SÃ¸rmo, NÃ¦) | 7 each |
| abstract | url_encoded (%xx) inside prose | 21 |
| abstract | mojibake | 6 |

**Concrete examples:** 
- Title with emoji + stray symbols: `Zoran{butterfly emoji} GMUC B" — White Paper de Consolidation Scientifique` (10.5281/zenodo.12559448) 
- Title with raw HTML tag: `Long-Term Elevated CO <sub>2</sub> Improves Soil Health…` (10.1002/advs.202503190) 
- Persons field with only a pipe, no name: `|` (85abfa4d-110f-45cc-aabc-b1074a9c4809) 
- Persons with trailing double pipe: `Bartrons Vilanala, Mireia||` (10.1371/journal.pone.0268163) 
- Abstract with raw LaTeX leaking through: `...supported by the BES grant agreement number LRB17\1019 (MUSGONET)...` (10.1007/s11104-013-1868-9) 
- Mojibake name (UTF-8 decoded as Latin-1 or similar): `SÃ¸rmo, Erlend` — should read `Sørmo, Erlend`

**Did it get better or worse since July? (matched records only)**