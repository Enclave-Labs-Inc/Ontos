# Enclave Labs: Readiness and Singapore/APAC Market Fit

Scope note: Part A is based on the local repo `/home/user/Ontos` (read-only; README.md, CLAUDE.md, CHANGELOG.md, docs/architecture.md, docs/compliance/eu-ai-act-article-12.md, docs/design/m1–m7.md, git log). getenclave.ai could not be fetched: the sandbox's egress proxy blocked it (EGRESS_BLOCKED), so nothing below comes from the marketing site. Part B uses web sources current to Oct 2026. Repo citations use local file paths.

## How mature is Enclave and what gaps would residency/accelerator reviewers flag?

### Takeaway
Ontos, Enclave's knowledge-graph layer, is a well-engineered open-source (Apache-2.0) runtime. It has shipped quickly: v0.0.1 to v0.5.0 in about 2 weeks, published on PyPI, GHCR and Helm, with real compliance engineering (a hash-chained Article-12 audit log, ACL pruning during traversal, bitemporal facts). But it is very early. Its git history starts on 2026-09-24, one founder wrote almost all of it, and there is no evidence of customers, pilots, revenue or team in the repo. The sibling products (enclave-runtime, enclave-scribe) are not in this repo, and Scribe is explicitly "in development". Reviewers will flag traction, team depth, and the gap between the "sovereign" claim and the frontier-LLM "bridge" backends the product still depends on.

### Cited Findings
**What it is**
- Ontos is "the knowledge-graph layer of Enclave's sovereign AI company brain". It extracts typed entities and relationships, persists them as "immutable, bitemporally-valid, provenance-tagged facts", serves them to LLM agents over MCP with permission-aware traversal, and "emits a compliance-grade audit record for every query". — [README.md](/home/user/Ontos/README.md)
- The Enclave product family has these parts. enclave-runtime is the retrieval layer: Rust shard workers, HNSW-over-S3 (vector search indexes stored on S3), and a layer-0 cache. enclave-scribe is a sovereign extraction model, "in development". enclave-ocr handles OCR. Product surfaces (enclave-gtm, enclave-home, enclave-business) consume Ontos via MCP. — [docs/architecture.md](/home/user/Ontos/docs/architecture.md), [README.md](/home/user/Ontos/README.md)
- Non-negotiables:
  - provenance on every fact;
  - an audit event on every query path;
  - permission pre-filtering that never leaks the existence of a node;
  - bitemporal writes `(t_valid, t_invalid, ingested_at, superseded_by)`;
  - no outbound calls or telemetry from the runtime.
  — [CLAUDE.md](/home/user/Ontos/CLAUDE.md)

**What has shipped (per CHANGELOG and design docs)**
- Release cadence:
  - 0.0.1 pre-alpha scaffold: 2026-09-25
  - 0.1.0 first public release: 2026-09-26
  - 0.2.0: 2026-10-01
  - 0.3.0 and 0.4.0: 2026-10-06
  - 0.5.0: 2026-10-07

  Each release ships a PyPI wheel (`enclave-ontos`), a GHCR image and a Helm chart. — [CHANGELOG.md](/home/user/Ontos/CHANGELOG.md)
- Milestones and their status:

  | Milestone | Scope | Status in its design doc |
  |---|---|---|
  | M2 | Authz backends (OpenFGA, SpiceDB, in-memory) and persistent Article-12 audit | "implemented" |
  | M3 | Planner/executor split with Personalized PageRank + PathRAG pruning; `ask` MCP tool | "implemented" |
  | M4 | Entity-resolution cascade (only the rules tier ships), connector interface, Helm chart, benchmark harness | "implemented" |
  | M5 | Ingest pipeline, Ollama (sovereign) and OpenAI (bridge) adapters, CLI | "implemented" |
  | M6 | Release plumbing for 0.1.0 | "implemented" |
  | M7 | Fintech and pharma ontologies | Shipped. Sample corpora, graded questions and the `ontos vertical` CLI are "follow-up PRs" |

  — [docs/design/m2.md–m7.md](/home/user/Ontos/docs/design/)
- `ontos/storage/neo4j_store.py` exists alongside the NetworkX store. Yet docs/design/m1.md still says "Status: design, pre-implementation" and architecture.md still calls M0 NetworkX "current". The docs are out of date relative to the code. — repo inspection
- Article-12 mapping:
  - captures 12 fields per tool call;
  - records are hash-chained and HMAC-signed;
  - fields include `acting_on_behalf_of` (per-user attribution) and `policy_decisions`.

  The compliance doc lists the Postgres-persistent log, 6-month retention and export to CloudTrail/S3 as roadmap (M2/M3+). However, CHANGELOG 0.4.0/0.5.0 shows a `PostgresAuditEmitter` and `ontos audit verify` already shipped. — [docs/compliance/eu-ai-act-article-12.md](/home/user/Ontos/docs/compliance/eu-ai-act-article-12.md), [CHANGELOG.md](/home/user/Ontos/CHANGELOG.md)
- Sovereignty caveats:
  - PDF parsing has a sovereign `pypdf` path. Scanned PDFs use LlamaParse, a "BRIDGE" backend that sends documents to `api.cloud.llamaindex.ai`.
  - Ollama is the default in-VPC LLM. The OpenAI and Anthropic backends are bridge backends.
  - v0.5.0 added guardrails: a loud warning whenever a bridge backend is instantiated, and refusal in `ONTOS_ENV=prod` unless explicitly opted in.

  — [README.md](/home/user/Ontos/README.md), [CHANGELOG.md](/home/user/Ontos/CHANGELOG.md)
- Extraction today wraps LlamaIndex/LangChain. Enclave says this is "a bridge, not a long-term choice" until Scribe passes benchmarks against frontier models. — [CLAUDE.md](/home/user/Ontos/CLAUDE.md)

**Activity and team signals (git log)**
- 120 commits, first commit dated 2026-09-24, latest 2026-10-07. Development runs through PRs (#35–#55 merged, with review-fix commits). — `git log`
- Authors:

  | Author | Commits |
  |---|---|
  | shashank1623 / Shashank Bhardwaj | 112 |
  | Aleksandra Lazic / ALEKS0805 | 4 |
  | "Claude" | 3 |
  | enclavedotai | 1 |

  The GitHub org is `Enclave-Labs-Inc`. — `git shortlog`
- About 7.7k lines of Python under `ontos/` and 57 test files. A gate-blocking `tests/compliance/` suite (authz-leak and Article-12 field completeness) is mandated. — repo inspection, [CLAUDE.md](/home/user/Ontos/CLAUDE.md)
- The only vertical content is synthetic fintech 10-K example files (`docs/ontology/examples/fintech-corpus/`). The repo mentions no customers, design partners or pilots. — repo grep

### Inferences
- The engineering signal is strong for the stage. In two weeks the repo shows release discipline, compliance tests, Helm/GHCR packaging and two vertical ontologies (financial services and pharma). This suggests a technically credible, AI-assisted solo founder with high velocity.
- Gaps reviewers will flag:
  1. No traction (no logos, LOIs, pilots or revenue) and no usage metrics, beyond a PyPI-downloads badge whose value is unknown.
  2. Team. It looks like one primary builder. A GTM or enterprise-sales co-founder, and domain or regulatory expertise in Singapore financial services, are not evident.
  3. The company is very young (weeks of history).
  4. The full sovereignty story depends on enclave-scribe, which is unshipped. Today's best extraction quality uses bridge backends to third-party clouds.
  5. Docs are stale (README "Status: 0.1.0", m1 "pre-implementation"). That reads as sloppy to technical reviewers.
  6. The compliance positioning is EU-centric (Article 12, August 2026 enforcement), not anchored in Singapore regulation.
  7. Of the "company brain" stack, only the knowledge-graph layer is visible. Reviewers may ask whether Enclave is a platform or a component (a GraphRAG/MCP memory layer). That puts it against well-funded graph-RAG and memory players.
- Strongest assets for an application:
  - per-user attribution (`acting_on_behalf_of`) on every agent query;
  - ACL pruning during traversal with no leakage of node existence;
  - bitemporal "what did we know on date X" queries;
  - deployability inside the customer's VPC (Helm, Ollama default).

  These map closely to regulator concerns about agentic AI (see below).

### Gaps
- getenclave.ai content (positioning, team bios, pricing, customers) was not retrievable: the sandbox's egress proxy blocked it.
- The founding date, incorporation location (the name "Inc" suggests a US entity), funding to date and team size could not be verified from the repo.
- PyPI download counts and GitHub stars were not checked.
- The state of enclave-runtime and enclave-scribe is unknown; they are separate repos and were not inspected.

## Is there real Singapore demand for sovereign in-VPC enterprise AI?

### Takeaway
Yes, but the demand is stronger in stated priority and regulation than in budget. Regulators are building a dense, governance-heavy framework that emphasises traceability, inventories and lifecycle controls: the MAS AI Risk Management Guidelines (proposed November 2025), IMDA's Agentic AI governance framework (January 2026, updated May 2026), and PDPC's GenAI advisory guidelines (July 2026). Government and large vendors are investing heavily in sovereign and air-gapped AI infrastructure: Singtel RE:AI + Mistral, Google Distributed Cloud air-gapped for HTX/GovTech/CSIT, H2O.ai, and Equinix/Cisco. But an IDC survey shows government buyers are mostly still evaluating or running POCs without spending plans. Big banks already buy cloud or private-cloud vendors, such as DBS with Glean and OCBC with Azure OpenAI.

### Cited Findings
**Regulation and governance**
- MAS AI Risk Management Guidelines:
  - The consultation was issued 13 November 2025 and closed 31 January 2026.
  - They would apply to all financial institutions, and cover generative AI and AI agents.
  - They require AI inventories, risk materiality assessments (impact, complexity, reliance), lifecycle controls and board oversight.
  - MAS proposed a 12-month transition after issuance.

  — [MAS consultation page](https://www.mas.gov.sg/publications/consultations/2025/consultation-paper-on-guidelines-on-artificial-intelligence-risk-management); [Linklaters](https://www.linklaters.com/knowledge/publications/alerts-newsletters-and-guides/2025/november/27/mas-consults-on-new-ai-risk-management-guidelines-for-financial-institutions); [MAS media release](https://www.mas.gov.sg/news/media-releases/2025/mas-guidelines-for-artificial-intelligence-risk-management)
- Linklaters: the MAS guidelines are "less prescriptive than the EU's AI Act" but "likely to require major compliance uplift exercises for FIs in Singapore". They build on FEAT (MAS's Fairness, Ethics, Accountability and Transparency principles), IMDA's Model AI Governance Framework and AI Verify. — [Linklaters](https://financialregulation.linklaters.com/post/102lw8a/singapore-mas-proposes-comprehensive-ai-risk-management-guidelines-for-financial)
- No evidence was found that MAS has issued final guidelines. A Simmons & Simmons article dated 27 February 2026 still refers to them as proposed. — [Simmons & Simmons](https://www.simmons-simmons.com/en/publications/cmm4lh7wv004utmikclcdujg7/mas-guidelines-on-artificial-intelligence-risk-management)
- MAS Technology Risk Management Guidelines were revised in January 2021 to address cloud, APIs and rapid software development. — [WongPartnership](https://wongpartnership.com/insights/detail/mas-releases-revised-technology-risk-management-guidelines)
- MAS published an information paper on AI model risk management in December 2024. This is described in a secondary source only. — [Tenable webinar page](https://www.tenable.com/webinars/ai-driven-risk-your-guide-to-cloud-ai-security-for-monetary-authority-of-singapore-mas)
- IMDA Model AI Governance Framework for Agentic AI:
  - Released January 2026; non-binding.
  - Built on five dimensions: oversight, **traceability**, reliability, interaction and ecosystem.
  - Flags "unauthorised actions, misuse of sensitive data and increased automation bias".
  - Updated 20 May 2026 with case studies and guidance on multi-agent and third-party agent risks.

  — [Rajah & Tann / Allen & Gledhill summary](https://www.rahmatlim.com/sg/publication/articles/33285/imda-updates-model-ai-governance-framework-for-agentic-ai); [Marketing-Interactive](https://www.marketing-interactive.com/imda-sets-guardrails-for-agentic-ai-with-new-framework); [Digital Policy Alert](https://digitalpolicyalert.org/event/40016-infocomm-media-development-authority-issued-updated-model-ai-governance-framework-for-agentic-ai)
- PDPC Advisory Guidelines on Use of Personal Data in Generative AI:
  - Consultation ran 2 June – 1 July 2026; final version issued 20 July 2026.
  - Covers personal data used for model development, allocation of responsibilities across the GenAI lifecycle, and handling of individuals' requests.

  — [PDPC](https://www.pdpc.gov.sg/media-events/pdpc-issues-guidance-for-organisations-on-responsible-use-of-personal-data-in-generative-ai); [Rajah & Tann](https://www.rahmatlim.com/sg/publication/articles/33126/pdpc-consults-on-proposed-advisory-guidelines-on-use-of-personal-data-in-generative-ai); [Digital Policy Alert](https://digitalpolicyalert.org/event/41880-personal-data-protection-commission-issued-advisory-guidelines-on-use-of-personal-data-in-generative-ai)

**Government and infrastructure demand**
- Singtel's RE:AI and Mistral AI:
  - April 2026 partnership for a sovereign multi-tenant GPU-as-a-Service and AI-as-a-Service offering across Singapore, Johor and Batam.
  - Targets financial services, defence, government and healthcare, "where data sovereignty and on-premises deployment requirements have slowed AI adoption".
  - Includes an Applied AI Centre of Excellence.

  — [DatacenterDynamics](https://www.datacenterdynamics.com/en/news/singtels-reai-teams-up-with-mistral-ai-on-sovereign-offering-in-singapore/); [Compare the Cloud](https://www.comparethecloud.net/news/singtels-reai-and-mistral-ai-agree-sovereign-ai-cloud-deal-for-singapore-and-wider-southeast-asia)
- In May 2026 Mistral added a strategic partnership with HTX and MOUs with Singtel, NCS and ST Engineering. — [Intelligent CIO](https://www.intelligentcio.com/apac/2026/05/12/mistral-ai-accelerates-singapore-expansion-with-strategic-partnership-and-industry-collaborations/)
- CSIT, GovTech and HTX were reported as the first in Asia to access Gemini on Google Distributed Cloud air-gapped, which keeps highly sensitive data in on-premises data centres disconnected from the internet. Sources differ on timing. — [iTnews Asia](https://www.itnews.asia/news/singapores-htx-deploys-air-gapped-cloud-to-enhance-ai-safety-for-public-617481); [DatacenterDynamics](https://www.datacenterdynamics.com/en/news/singaporean-science-and-tech-agency-signs-mou-with-google-cloud/)
- In April 2026, Senior Minister K Shanmugam framed a government "sovereign AI" strategy around keeping control over critical systems. This comes from secondary coverage. — [Intelligent CIO](https://www.intelligentcio.com/apac/wp-json/wp/v2/posts/49660)
- IDC InfoBrief, sponsored by Dell and NVIDIA (survey of 30 Singapore government IT leaders, December 2025):
  - 93.3% say data sovereignty affects their AI investment decisions.
  - Only 3.3% report significant investment in it.
  - 40% are evaluating sovereign AI technologies.
  - 43.3% are running proofs of concept with no spending plan.
  - Across eight APAC markets, sovereign AI rose from 7th to 2nd government investment priority in a year.

  — [CDOTrends](https://www.cdotrends.com/story/5120/singapore-wrote-sovereign-ai-playbook-almost-nobody-funded-it); [Complete AI Training](https://completeaitraining.com/news/singapore-writes-sovereign-ai-playbook-but-few-fund-it/)
- In July 2026 H2O.ai expanded its Forward Deployed AI Lab in Singapore for organisations with strict data-residency, regulatory and sovereignty requirements. H2O.ai holds IMDA accreditation. — [TechIntelPro](https://techintelpro.com/news/ai/enterprise-ai/h2oai-expands-forward-deployed-ai-lab-investment-in-singapore); [Maxthon blog (promotional)](https://blog.maxthon.com/2026/01/16/h2o-ais-imda-accreditation-catalyzing-singapores-enterprise-ai-revolution/)
- Broadcom's Private Cloud Outlook 2026 reports that APJ enterprises are moving workloads from public to private cloud faster than the rest of the world. This comes via news-aggregator coverage. — [Data Center News Asia](https://datacenternews.asia/tag/datasovereignty?page=6+9)
- A Zendesk report says only 35% of APAC organisations can provide a fully auditable record of AI decisions. This comes from a search snippet of aggregator coverage. — [techcoffeehouse](https://techcoffeehouse.com/tag/sovereign-ai/)

**Bank buyer behaviour**
- DBS uses Glean for enterprise search and AI, with 40,000+ users per Glean's case study. The deployment model is not confirmed. The page could not be fetched; this is from the search snippet. — [Glean customer story](https://www.glean.com/resources/customer-stories/dbs)
- OCBC GPT reached about 30,000 staff in 2023. It runs on Azure OpenAI with data "retained within the confines of the bank's private cloud". — [The Edge](https://ceomorningbrief.theedgemalaysia.com/article/2023/0658/World/25/687465); [HRD Asia](https://hcamag.com/asia/specialisation/hr-technology/bank-to-roll-out-generative-ai-chatbot-to-30000-global-staff/464416)
- UOB trialled Microsoft 365 Copilot with 300 employees. Its job postings show in-house LLM/RAG engineering. — [UOB PDF](https://www.uobgroup.com/web-resources/uobgroup/pdf/newsroom/2023/uob-microsoft-copilot-genai-tool.pdf); [NodeFlair job](https://nodeflair.com/jobs/uob-first-vp-genai-engineer-scientist-lead-innovation-group-537046)

### Inferences
- Demand is real but takes a specific shape. Singapore's large banks already run cloud or private-cloud GenAI from incumbents, so "in-VPC" alone is not the wedge. The pain that is still unmet is evidential. MAS and IMDA ask for AI inventories, traceability of agent actions, per-user accountability and lifecycle controls. Off-the-shelf copilots provide little of that at the level of individual facts and queries.
- Government sovereign-AI money currently flows to infrastructure: GPUs, air-gapped clouds, and model partnerships (Mistral, Google). Enclave's opening is to be the governed knowledge/context layer that runs on those sovereign stacks (Singtel RE:AI, GDC air-gapped, NCS), rather than competing with them.
- A plausible timing argument: the MAS guidelines, once finalised, carry a 12-month transition. That would put financial-institution compliance build-outs in 2026–2027. This is an inference; finalisation is not confirmed.

### Gaps
- No primary source confirms that the MAS AI Risk Management Guidelines were finalised as of October 2026.
- No market-size figures were found for Singapore sovereign or on-prem enterprise AI.
- No named Singapore bank was found running on-prem or in-VPC GenAI knowledge systems.
- "GovTech Pair" and the status of AI Verify's agentic testing were not confirmed.
- Healthcare buyers (Synapxe, the cluster hospitals) were not researched.

## What positioning should the application use?

### Takeaway
Reframe the pitch from "EU AI Act Article-12 audit" to "an evidence-grade context layer for agentic AI under MAS and IMDA governance". Ontos provides per-user attributable, permission-pruned, provenance-cited and time-travelable answers that run inside a bank's or agency's own VPC or a Singapore sovereign cloud. Lead with financial services: the MAS AI Risk Management Guidelines and FEAT, with the fintech ontology already shipped. Present government, GLCs and pharma/healthcare as expansion segments. Position Enclave as complementary to sovereign infrastructure (Singtel RE:AI/Mistral, GDC air-gapped, H2O.ai), not as a rival to Glean or Cohere.

### Cited Findings
- Each MAS/IMDA requirement maps to an existing Ontos feature:

  | Regulatory ask | Ontos feature |
  |---|---|
  | MAS: AI inventories, lifecycle controls, oversight of GenAI and agents ([MAS](https://www.mas.gov.sg/publications/consultations/2025/consultation-paper-on-guidelines-on-artificial-intelligence-risk-management)) | `model_versions`, `tool_invoked`, `policy_decisions` fields recorded on every query ([compliance doc](/home/user/Ontos/docs/compliance/eu-ai-act-article-12.md)) |
  | IMDA agentic framework: "traceability"; risks of "unauthorised actions" and "misuse of sensitive data" ([Rajah & Tann](https://www.rahmatlim.com/sg/publication/articles/33285/imda-updates-model-ai-governance-framework-for-agentic-ai)) | `agent_identity` + `acting_on_behalf_of` attribution; ACL pruning during traversal with no leakage of node existence ([architecture.md](/home/user/Ontos/docs/architecture.md)) |
  | PDPC: responsibilities across the GenAI lifecycle ([PDPC](https://www.pdpc.gov.sg/media-events/pdpc-issues-guidance-for-organisations-on-responsible-use-of-personal-data-in-generative-ai)) | Provenance (source id, extractor id/version, confidence) on every fact; bitemporal supersession instead of overwrites ([CLAUDE.md](/home/user/Ontos/CLAUDE.md)) |

- Existing vertical ontologies: fintech and pharma/life sciences ("the two verticals Enclave sells into first"). — [docs/design/m7.md](/home/user/Ontos/docs/design/m7.md)
- Competitor and peer presence in Singapore:

  | Company | Singapore activity |
  |---|---|
  | Glean | Incumbent at DBS ([Glean](https://www.glean.com/resources/customer-stories/dbs)) |
  | Cohere | Expanding Singapore public-sector partnerships; plans to double APAC headcount ([Digital Today](https://www.digitaltoday.co.kr/en/view/89642/cohere-pushes-to-set-up-south-korea-unit-plans-large-hiring-of-field-development-engineers); [The Logic](https://thelogic.co/news/cohere-south-korea-office/)) |
  | Mistral | Sovereign partnerships with Singtel and HTX ([Intelligent CIO](https://www.intelligentcio.com/apac/2026/05/12/mistral-ai-accelerates-singapore-expansion-with-strategic-partnership-and-industry-collaborations/)) |
  | H2O.ai | Singapore Forward Deployed AI Lab ([TechIntelPro](https://techintelpro.com/news/ai/enterprise-ai/h2oai-expands-forward-deployed-ai-lab-investment-in-singapore)) |
  | Writer | No Singapore-specific presence found in this research |

- Comparable Singapore funding: Level3AI (enterprise AI agents for APAC customer engagement) raised a US$13M seed led by Lightspeed in January 2026. It claims profitability and clients such as Carousell and Carsome. — [AI Market Watch](https://www.ai-market-watch.com/news/covenant-advises-on-us13-million-seed-round-for-en); [NeuronFeed tracker](https://neuronfeed.com/country/sg)
- Other Singapore AI rounds in 2026, from an aggregator and not enterprise-infrastructure plays: Agnes AI Series A of $10M; OnSite seed of $1.3M. — [NeuronFeed](https://neuronfeed.com/country/sg)

### Inferences
- **Recommended headline:** "Ontos/Enclave is the governed memory for enterprise AI agents. It is a sovereign, in-VPC knowledge graph that cites every fact and enforces each user's permissions inside the graph traversal itself. It also produces a tamper-evident audit trail mapped to MAS AI Risk Management, the IMDA Agentic AI Framework and PDPA, and to the EU AI Act for multinational banks."
- **Keep the EU AI Act as a secondary proof point.** It shows rigour and matters for EU-exposed Singapore banks and insurers. Lead with MAS/IMDA vocabulary: traceability, accountability, inventory, materiality, human oversight.
- **Differentiate against Glean and Cohere North** on three things:
  1. Structured relationships and "as-of" history versus document search.
  2. Permission enforcement during traversal versus post-filtering.
  3. Open-source, self-hostable Apache-2.0 code that buyers can audit.

  Do not claim to replace the enterprise-search incumbent.
- **Partner-led go-to-market** (an inference, since none of these partnerships exist yet): offer Ontos as the context/memory layer on Singtel RE:AI + Mistral, NCS or GDC air-gapped stacks, all of which target regulated sectors.
- **Fix before applying:**
  - update the stale README and design docs;
  - publish a Singapore-reg mapping doc (MAS AIRM / IMDA agentic MGF / PDPC) alongside the Article-12 doc;
  - ship the fintech sample corpus and graded questions (M7.c/d) so the demo is credible;
  - state the Scribe timeline and the sovereign-only path (Ollama + pypdf) plainly;
  - secure at least 1–3 Singapore design partners or LOIs (financial institution, GLC, or HTX/GovTech-adjacent).
- **Expect reviewers to benchmark Enclave against seed rounds like Level3AI's.** That company has profitability and named logos at seed. A pre-traction solo-founder company will need a sharp regulatory wedge plus design-partner evidence to compete.

### Gaps
- No Singapore-program-specific funding data was found for comparable sovereign or knowledge-graph startups (for example, SGInnovate, Antler SG, EF SG, Startup SG Tech portfolio companies).
- No local sovereign enterprise-AI startup at seed or Series A was identified, beyond infrastructure players.
- Writer's and Glean's current Singapore sales presence was not verified.
- AI Verify toolkit coverage of RAG, graph or agentic systems was not confirmed. Plugging into the AI Verify Foundation could be a further positioning angle, but this is unverified.
