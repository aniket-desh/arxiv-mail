# arxivmail

`arxivmail` builds a short weekday reading queue from the arXiv announcement feed. It screens broadly, uses embeddings and a read-only Zotero profile for recall, asks `gpt-5.6-luna` to judge the actual abstracts, then includes only strong papers plus at most three explicitly labeled “Less urgent” papers. It never pads to ten.

The current profile centers statistical physics of learning, disorder, theoretical neuroscience, mechanistic interpretability, tensor networks, and many-body/electronic-structure work. The September 2026 overlay gives extra weight to replica, cavity, and perturbative methods; associative-memory variants; and higher-order or many-body learning models. Both layers live in [`config/interests.toml`](config/interests.toml), including the eight-paper `MMMPMMPP` calibration set.

## Local use

The application has no runtime package dependencies beyond Python 3.11. Local credentials can come from environment variables, or from the first, second, and third non-empty lines of `~/.arxiv`:

1. OpenAI API key
2. Zotero Web API key
3. Gmail App Password

All three entries are required when using that positional format, so an omitted middle value can never shift a Gmail credential into the Zotero header. A partial file must instead use named entries such as `OPENAI_API_KEY=...` and `GMAIL_APP_PASSWORD=...`. The file must be private (`chmod 600 ~/.arxiv`). Credential values are never logged, written to the preview, or committed.

```bash
python3 -m unittest discover -s tests -v
python3 -m arxivmail doctor --online
python3 -m arxivmail calibrate
python3 -m arxivmail generate
```

`generate` never sends mail. It reads one rate-limited arXiv RSS announcement feed per category, writes `output/digest-preview.html`, `output/digest-preview.txt`, and `output/digest-data.json`, and skips mail on an empty day. To exercise missed-run recovery without sending, use:

```bash
python3 -m arxivmail generate --source api --lookback-hours 120
```

The production source uses one RSS feed per category because RSS identifies new, cross-listed, and replaced papers while multi-category feeds have a result cap. A paginated search-API query recovers new submissions after a missed run, but arXiv exposes no exact historical RSS-announcement API, so missed replacement and cross-list-only announcements are explicitly reported as a recovery gap. Delivery history is keyed by base arXiv ID, and a later version returns only when its metadata changed materially.

## Ranking and privacy

The taxonomy is a retrieval vocabulary, not a flat score. Abstract embeddings establish semantic fit, recent Zotero items supply a small taste signal, and Luna makes the final M/P/N decision from the abstract with the user-labeled examples in its prompt. Numerical-linear-algebra items in Zotero are deliberately weak evidence unless they connect to tensor contraction, eigensolvers, simulation, or electronic structure.

The Zotero sync requests bibliographic metadata only: titles, abstracts, creators, tags, collection names, and dates. It never requests notes, attachment files, or indexed PDF text. Candidate abstracts and up to 160 recent limited-metadata Zotero items are sent to OpenAI for ranking; at most 500 recent Zotero records are cached. Local previews and state are mode `0600` and ignored by Git. The hosted workflow keeps a historyless state snapshot on a separate branch in the private repository, so deleted taste metadata is not retained in ordinary branch history.

## Hosted weekday delivery

The prepared workflow runs at 7:07 a.m. `America/Chicago`, Monday through Friday, and can also be started manually. Before enabling it, create a private GitHub repository and add these Actions secrets:

- `OPENAI_API_KEY`
- `ZOTERO_API_KEY`
- `GMAIL_APP_PASSWORD`

Scheduled runs remain disabled until the repository variable `ARXIVMAIL_ENABLED` is set to `true`; leave it unset while reviewing this local preview. A manual run likewise sends only when its required `deliver` checkbox is enabled.

The workflow tests the code, reserves selected arXiv IDs in the private `state` branch, and only then hands the rendered message to Gmail. After SMTP acceptance it updates the state snapshot with a delivery receipt. This is deliberately at-most-once: if a runner dies during an uncertain SMTP result, the durable reservation suppresses a duplicate and that digest may require manual recovery. Checkout credentials are removed before account secrets enter the process, and the repository token is exposed only to the state restore/publish steps. The workflow never writes generated metadata into the source branch.

During the first week of a new month, replace or add a `[[monthly_focus]]` entry in `config/interests.toml`, give it explicit start and expiry dates, and run `calibrate` before merging. The permanent profile remains unchanged when the temporary overlay expires.

## Feedback boundary

Zotero saves already feed the next ranking cycle. The code has no mailbox-reading path and automatic parsing of Gmail replies is intentionally off. The configured App Password is still an account credential rather than a technically send-scoped OAuth token; replace SMTP with Gmail API OAuth and the `gmail.send` scope if credential-enforced send-only access becomes a requirement.
