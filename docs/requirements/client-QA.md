# Client Q&A (OpsFleet)

Thread between Mariya Vlahova and Harry (OpsFleet, AI Assistant). Copied verbatim. Recorded on 2026-10-22; the message dates were not given.

## Mariya Vlahova

Questions:

1. Does the Golden Bucket exist at all or i need to use syntetic generated? The requirements calls it "Theoretical", and no Trio schema is defined. Could you send me the schema ?
2. I guess data governance( ingestion pipeline and RAG building and evaluation) is out of the scope ? Do we have some RAG to connect to for golden retrieval or to work with mock data?
3. Building memory agent is out of the scope too right - to extract glossary, prefernce?

## Harry - AI Assistant [9:44 AM]

@Mariya Vlahova happy to answer these:

1. **Golden Bucket** — it's entirely theoretical; there's no real dataset or predefined schema. Define the Trio format yourself (Question → SQL Query → Analyst Report) and seed a small sample set as you see fit. The design requirement is to explain how you'd update the bucket over time and retrieve relevant Trios at query time — a small seeded set with retrieval in the prototype is a nice touch but not required.

2. **Data governance / ingestion pipeline / RAG** — out of scope. You can assume another team handles ETL and data modeling; focus on the agent itself. There's no external RAG to connect to — work with mock data you create yourself (tied to the Golden Bucket above).

3. **Memory / preference extraction** — design only. Continuous Improvement and Persona Management are not prototype requirements; cover them in your HLD and technical explanation. The four requirements that do need to be prototyped are: Safety & PII Masking, High-Stakes Oversight, Resilience & Graceful Error Handling, and Observability.

Hope that helps — let us know if anything else comes up. Thanks, and good luck!

## Mariya Vlahova [9:56 AM]

ok please explain what do you mean exactly with "Each user should only be able to analyze data on products related to him." from Safety & PII Masking: The agent is only allowed to answer analysis questions and needs to be safeguarded against malicious users. The raw transaction logs contain sensitive data. The agent is strictly forbidden from displaying these PIIs in the final output. Each user should only be able to analyze data on products related to him.

## Harry - AI Assistant [9:57 AM]

@Mariya Vlahova sure — here's how to think about it:

Each user in the system is a brand manager responsible for one or more brands. They should only be able to analyze data tied to those brands — meaning customer behavior, orders, and product performance for their brands only. The CEO is the exception and sees everything.

The mapping between managers and brands is not in the thelook_ecommerce dataset — you'll need to define your own structure for that (a config file, an access table, or similar). In production, the frontend would send a JWT containing the user's scopes (e.g., which brands they can access), and the agent enforces access based on those scopes.

So for the prototype, a small hardcoded or config-driven mapping is perfectly fine — just make sure the agent filters data accordingly. Thanks, and good luck!
