# Holo Growth Engine

The go to market system for <a href="https://github.com/Lihini0202/holoapp"><b>Holo</b></a>,
a dating safety application that analyses a conversation and reports whether the other
person is losing interest.

Holo is the product. This repository covers everything that turns events and sign ups into
active users: an event lead pipeline into HubSpot or Salesforce, lifecycle stages from product usage,
campaign emails checked before anyone sends them, website analytics from GA4, and tracking
of how Holo appears in AI assistant answers. Every part runs from the command line, a
dashboard, or Claude through an MCP server.

```
$ holo-growth leads process saturday_signups.csv --event "Online Dating Safety Workshop - Colombo"

Online Dating Safety Workshop - Colombo: 123 rows read, 3 rejected, 9 duplicates merged,
33 without consent (not uploaded). CRM: 78 created, 0 updated, 0 failed. 47 sent to review,
20 hot leads. Subscriptions: 78 subscribed, 0 unsubscribed.
```

## Quick start

Everything runs offline without API keys: rule based enrichment, a local SQLite CRM, and
synthetic Holo users and website traffic.

```bash
pip install -e ".[dev,dashboard]"
holo-growth demo
HOLO_GROWTH_DB=demo.db streamlit run dashboard/app.py
```

## Architecture

```
Event sign up sheet ──> Lead pipeline ─────┬──> HubSpot or Salesforce
Holo Supabase ────────> Lifecycle sync ────┤    contacts, lifecycle stages,
  (usage metadata only)                    │    email subscription status
                        Consent ledger ────┘
GA4 ──────────────────> Website sign ups ──┐
                        Campaign checks ───┼──> SQLite store ──> Dashboard, weekly brief, MCP server
                        GEO tracker ───────┘

Claude API (optional): enrichment, drafting, tone review and GEO checks
```

<table>
  <tr><th align="left">Component</th><th align="left">Responsibility</th><th align="left">Outcome</th></tr>
  <tr><td><b>Lead pipeline</b></td><td>Maps messy columns, corrects email domain typos such as <code>gmial.com</code>, normalises Sri Lankan phone numbers, merges duplicates, removes anyone who did not opt in, enriches with Claude (persona, intent, language), scores, and upserts to the CRM</td><td>Event lists cleaned, enriched and uploaded the same day</td></tr>
  <tr><td><b>Lifecycle sync</b></td><td>Assigns each Holo user a stage (new, activated, engaged, at risk, dormant) from product usage, counts predictions awaiting the user's confirmation, and syncs consented users to the CRM</td><td>CRM data entered once, from the source</td></tr>
  <tr><td><b>Campaign checks</b></td><td>Drafts lifecycle emails (Claude or templates) and blocks false urgency, guarantees, accuracy claims, off brand tone, unknown links and any reference to a user's private analyses</td><td>Every email checked before it is sent</td></tr>
  <tr><td><b>GEO tracker</b></td><td>Asks Claude and any OpenRouter model the questions Holo users ask, and records whether Holo is named and which competitors are</td><td>Visibility in AI search, tracked over time</td></tr>
  <tr><td><b>Website sign ups</b></td><td>Pulls daily sessions and sign ups by source from GA4 (Instagram, event QR codes, ChatGPT referrals and more)</td><td>The channels that bring sign ups</td></tr>
  <tr><td><b>Reports</b></td><td>Event funnel (lead to user to active), lifecycle stages, website sign ups by source, AI visibility and the review queue, as a dashboard and a Markdown weekly brief</td><td>Pipeline reporting</td></tr>
  <tr><td><b>MCP server</b></td><td>Eleven tools that run all of the above from Claude</td><td>Reusable tools for the whole team</td></tr>
</table>

## Design decisions

**The CRM never receives conversation data.** Holo's model predicts whether a user's
*chat partner* is about to stop replying. That is sensitive data about a third person who
never signed up. The growth engine therefore reads usage metadata only (who ran an
analysis, when, and whether an outcome was recorded), and the Supabase query selects
exactly those columns. A test fails if a risk field ever reaches the CRM, and a content
rule fails any email that mentions risk bands or "your conversation with".

**Lifecycle comes from usage, not from the ghosting model.** The model predicts what
happens in someone else's conversation, not whether the user will churn. Stages are
simple, explainable rules on activity instead.

**Consent is a ledger, not a checkbox.** Every grant and withdrawal is stored with its
source and time, and no record means no marketing. An unsubscribe always wins: neither a
reimported sign up sheet nor a stale answer from the app can resubscribe someone; only a
newer explicit choice can. People who said no are counted and never stored. In HubSpot
the ledger drives each contact's own email subscription status through an outbox that
sends every change exactly once, with the source and time of the choice as the legal basis
note, and retries anything HubSpot has not yet accepted. The design draws on six months of
building consent delegation for WSO2's OpenFGC.

**Model output is validated, not trusted.** Enrichment uses forced tool use with the
Pydantic schema, validates the result, retries once with the validation error, and falls
back to rules if validation still fails. Results below 0.7 confidence go to a human review
queue and are uploaded *without* the enrichment fields until approved. Sign up free text is
treated as data: it is wrapped in delimiters the user cannot close, and the sample data
includes a prompt injection attempt covered by a test.

**A failed batch never loses a run.** HubSpot calls retry on 429 and 5xx responses
(honouring <code>Retry-After</code>), fail fast on other client errors, and record per contact
errors from 207 responses. The run is marked <code>partial</code>, failed rows are listed, and
rerunning is safe because upserts are keyed on email. A file that was already processed is
skipped unless forced.

**Salesforce is a drop in alternative to HubSpot.** Contacts become Leads keyed on a
unique external ID, <code>Holo_Email__c</code>, so reruns update a lead instead of creating a
second one. The first run creates the nine <code>Holo_*</code> custom fields and a
<code>Holo_Growth_Engine</code> permission set that grants access to them and to the standard
<code>HasOptedOutOfEmail</code> field, which carries the consent ledger. Placeholder values for
the required <code>LastName</code> and <code>Company</code> fields are set only when a lead is
created, so an update never replaces real data. Leads are sent in batches of 200 through the
composite upsert API, with the same retries and per record error reporting as HubSpot.

**Drafts are never sent automatically.** A draft that passes every check is ready for a
person to load into HubSpot. Sinhala and Tamil drafts always go to a native speaker for
review.

## Configuration

Copy <code>.env.example</code> to <code>.env</code>. Each integration switches on when its key is
present and runs offline otherwise; <code>holo-growth status</code> shows the active mode.

<table>
  <tr><th align="left">Service</th><th align="left">Variables</th><th align="left">Notes</th></tr>
  <tr><td>Claude</td><td><code>ANTHROPIC_API_KEY</code></td><td>Enrichment, drafting, tone review and GEO</td></tr>
  <tr><td>HubSpot</td><td><code>HUBSPOT_PRIVATE_APP_TOKEN</code></td><td>A Service Key from any HubSpot account, including free accounts (Development, Keys, Service keys); legacy private app tokens also work. Custom <code>holo_*</code> properties are created on the first run</td></tr>
  <tr><td>HubSpot email subscriptions</td><td><code>HUBSPOT_SUBSCRIPTION_ID</code></td><td>Requires the <code>communication_preferences.read_write</code> scope; <code>holo-growth consent types</code> lists the IDs. When unset, consent is written to the <code>holo_marketing_consent</code> property</td></tr>
  <tr><td>Salesforce</td><td><code>SALESFORCE_DOMAIN</code>, <code>SALESFORCE_CLIENT_ID</code>, <code>SALESFORCE_CLIENT_SECRET</code>, <code>CRM_PROVIDER</code></td><td>Free with a Developer Edition org. Create an External Client App with the client credentials flow and a Run As user, and use the org's My Domain URL. Custom <code>Holo_*</code> Lead fields and their permission set are created on the first run. Set <code>CRM_PROVIDER=salesforce</code> when HubSpot credentials are also present</td></tr>
  <tr><td>GA4</td><td><code>GA4_PROPERTY_ID</code>, <code>GA4_CREDENTIALS_FILE</code></td><td>Free. Install the <code>ga4</code> extra and use a Google Cloud service account with Viewer access to the property. <code>GA4_SIGNUP_EVENT</code> defaults to <code>sign_up</code></td></tr>
  <tr><td>Holo</td><td><code>HOLO_SUPABASE_URL</code>, <code>HOLO_SUPABASE_SERVICE_KEY</code></td><td>Read only. Marketing consent requires the table in <code>integrations/holo/marketing_consent.sql</code>. "Not sure" answers are read from <code>outcome_skipped</code></td></tr>
  <tr><td>Additional GEO providers</td><td><code>OPENROUTER_API_KEY</code>, <code>HOLO_GEO_OPENROUTER_MODELS</code></td><td>The same provider Holo uses for its reply coach</td></tr>
</table>

```bash
holo-growth leads process signups.xlsx --event "Colombo Tech Meetup" --dry-run
holo-growth leads process signups.xlsx --event "Colombo Tech Meetup"
holo-growth review list
holo-growth review approve 12 --persona young_adult --intent high
holo-growth retention sync
holo-growth content draft validation_pending
holo-growth geo run
holo-growth consent withdraw someone@gmail.com
holo-growth consent sync                 # push consent changes to the CRM
holo-growth web sync                     # GA4 sessions and sign ups by source, last 28 days
holo-growth report brief --out weekly_brief.md
```

The weekly workflow (<code>.github/workflows/weekly.yml</code>) runs the lifecycle and
subscription sync, the GA4 sync, the GEO check and the weekly brief every Monday at 08:00
Sri Lanka time once the repository secrets are configured.

### From Claude

```bash
holo-growth-mcp        # stdio MCP server
```

Add <code>integrations/claude/mcp.json</code> to Claude Desktop or Claude Code, place a sign up
sheet in the inbox folder, and ask *"process the workshop sheet"*. Tools that write default
to <code>dry_run=true</code>, so Claude previews the result and a person confirms. Lead emails
are masked unless requested, and the server reads files only from the inbox folder.

## Dashboard

```bash
streamlit run dashboard/app.py       # from the repository root so .streamlit/config.toml applies
```

Six pages (Overview, Leads, Holo users, Website, AI visibility and Operations) built on the
same report queries as the CLI and the MCP server, so every surface shows the same numbers.
The event filter applies to lead figures, page and filter state persist in the URL, and a
sidebar switch toggles the light and dark themes. Enrichment awaiting review is shown as
"in review".

## Measuring enrichment

<code>holo-growth eval enrichment</code> scores the rules baseline, and Claude when a key is set,
on a labelled set. It reports accuracy per field, the share routed to review, and the
accuracy of auto accepted results, which is the error rate of data reaching the CRM without
review.

Rules baseline on the 60 lead synthetic set (<code>evals/results_rules_baseline.json</code>):

<table>
  <tr><th>Persona</th><th>Intent</th><th>Language</th><th>All three</th><th>Sent to review</th><th>Auto accepted, all correct</th></tr>
  <tr><td align="center">0.88</td><td align="center">0.85</td><td align="center">1.00</td><td align="center">0.75</td><td align="center">62%</td><td align="center">100%</td></tr>
</table>

The synthetic sign ups are generated from the same phrase templates the rules were written
for, so this baseline is optimistic. Hand labelled sign ups from the first real event will
provide the reference comparison between the rules and Claude. The review rate is the key
figure: at 62%, most leads still need a person, which is where the LLM adds value.

## Development

```bash
pip install -e ".[dev,dashboard,ga4]"
pytest                 # no network access or keys required
ruff check . && ruff format --check . && mypy holo_growth
```

CI runs linting, type checks, the test suite with an 80% coverage floor on Python 3.11 to
3.13, the offline demo end to end, and a full history secret scan. External APIs are
mocked with <code>respx</code>, so CI holds no secrets.

```
holo_growth/
├── leads/          ingest, clean, dedupe, enrich, scoring, pipeline
├── retention/      usage sources (Supabase, synthetic), lifecycle rules, CRM sync
├── content/        campaign briefs and templates, checks before sending
├── geo/            providers, mention detection
├── crm/            HubSpot and Salesforce clients, local SQLite CRM, consent outbox
├── web/            GA4 traffic by source, plus a synthetic source for the demo
├── consent.py      append only consent ledger
├── llm.py          Claude client with validated structured output
├── reports.py      SQL shared by the CLI, dashboard and MCP server
├── mcp_server.py   MCP tools
└── cli.py
dashboard/app.py    Streamlit dashboard
.streamlit/         dashboard themes (light and dark)
integrations/       Holo migration, Claude MCP configuration
```

## Operational notes

* All figures in this repository come from synthetic data and will be replaced by results
  from the first real event.
* Email subscriptions use HubSpot's single contact v4 endpoint, one request per contact,
  because the batch endpoints require Marketing Hub Enterprise. Verified on a free HubSpot
  account in October 2026: subscribe, unsubscribe with the legal basis note, and a repeat
  sync that sends nothing new. A change HubSpot has not accepted stays in the outbox and is
  retried on the next sync.
* Salesforce is verified on a Developer Edition org in October 2026: custom fields and the
  permission set created on the first run, leads created from a sign up sheet, subscribe and
  unsubscribe through <code>HasOptedOutOfEmail</code>, and a repeat sync that sends nothing new.
  Salesforce uses a single email opt out, so HubSpot subscription types do not apply.
* GA4 provides totals by traffic source. Attributing an individual lead to a source uses UTM
  parameters captured at sign up in the Holo app.
* GA4 is connected to a live property, with service account authentication, Data API access
  and sync verified in October 2026. Figures by source appear once GA4 has processed a day,
  usually within 24 to 48 hours.

## License

Released under the MIT License. See <a href="LICENSE">LICENSE</a>.
