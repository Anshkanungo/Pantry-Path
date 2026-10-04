Project Scoping Submission
Pantry Path: a conversational food finder for special diets
Project Team 11
Team Members:
•     Ansh Manoj Kanungo
•     Kenil Prafulbhai Devani
•     Khushal Bharatkumar Trivedi
•     Shivu Donmardi Gorva
•     Tirth Ghanshyambhai Borasaniya
 
1. Introduction
People with special dietary needs (food allergies, celiac disease, pregnancy, medical diets such as low-sodium or diabetic, religious diets such as halal or kosher, and lifestyle diets such as vegan) cannot simply buy the usual groceries. Turning "I feel like quiche tonight" into a shopping trip means adapting a recipe, reading labels product by product, comparing stores and working out a route. Today that work is spread across recipe sites, store apps, label scanners and guesswork.
Pantry Path is a conversational assistant that does this in one chat. The user says what they want to eat. The assistant adapts the recipe to the user's diet profile and asks only the questions that matter, such as crust or crustless and how many servings. While the conversation continues, it searches nearby stores in the background. When the user confirms, it returns within seconds:
1.   A shopping list with each ingredient, the product and package to buy, and how many.
2.   A store list showing where each item is and how strong the evidence is: in stock at a store, a catalog match only, or just a nearby store.
3.   The fastest multi-stop route through those stores, with a map link.
A language model interprets the conversation and explains the results, but deterministic rule checks decide whether a product fits a diet, and unknown evidence is never presented as safe. The project builds on a working proof of concept (Streamlit, Gemini, Kroger, Open Food Facts). We will deliver it as a working demo for the final presentation on December 8: one preloaded demo profile, no login or payments, and live data only. Production features such as accounts, checkout handoff and large-scale data pipelines are presented as future work.
2. Dataset Information
2.1 Dataset Introduction
Pantry Path does not rely on a single static dataset. It combines open product and nutrition data, live retailer and map APIs, and small curated datasets that we create for the rules and for evaluation:
•     Open Food Facts (OFF) is an open, crowd-sourced product database with ingredients, allergen and trace tags, labels (for example vegan, halal or gluten-free) and nutrition, indexed by barcode. It is the main evidence source for the diet checks.
•     USDA FoodData Central (FDC) Branded Foods lists US branded products with UPC codes, ingredient lists and nutrient values. We use it for the medical diet rules (sodium, sugar and carbohydrates per serving).
•     The Kroger Products and Locations APIs give live, branch-level product search with price and stock level for Kroger-family stores. They are the only free source of branch-level availability we have found.
•     Map data from OpenStreetMap or TomTom (still to be decided) provides grocery store locations, geocoding and drive-time routing, including multi-stop route optimization.
•     Our own curated datasets cover the versioned diet rules, golden evaluation sets, demo personas and product-match corrections. They are described in the data card below.
The product and nutrition data decide whether an item can be recommended to a person with a given diet. The retailer and map data decide where it can be bought and how to get there. The curated sets let us check that the rules and the language model behave correctly.
2.2 Data Card
Dataset
Size and format
Key fields
Refresh
Open Food Facts
Millions of products worldwide; JSON API by barcode, bulk JSONL/CSV/Parquet exports
barcode, ingredients_text, allergens_tags, traces_tags, labels_tags, nutriments, states_tags
Continuous (community edits); bulk export updated regularly
USDA FoodData Central, Branded Foods
Hundreds of thousands of US branded items; CSV/JSON download and REST API
gtinUpc, ingredients, brandOwner, servingSize, foodNutrients
Periodic releases
Kroger Products and Locations API
Live JSON responses per query and store
productId, upc, description, size, price (regular/promo), inventory.stockLevel, locationId, address
Live (we cache briefly)
Store locations and routing
Live JSON (Overpass/Nominatim/OSRM or TomTom)
name, address, coordinates, shop/diet tags, drive time and distance
Live (we cache briefly)
Diet rules registry (ours)
One versioned YAML file
category, rule type, threshold or excluded tags, evidence required, strictness, source citation
Versioned with the code
Golden diet set (ours)
20+ labeled cases per diet category (6 categories)
profile, product evidence, expected decision (include / exclude / withhold / not verified)
Grows with each bug or review
Demo personas (ours)
2–3 JSON profiles
name, ZIP, allergies, diet flags
Fixed per demo script
Product-match corrections (ours)
Small labeled set collected during testing
ingredient, quantity, product, correct/incorrect, reason
Collected until the presentation

 
The data is mostly semi-structured JSON with free text (ingredient lists, product names), categorical tags (allergens, labels, stock level), numeric values (price, nutrients, drive time) and geographic coordinates.
2.3 Data Sources
•     Open Food Facts data and API: https://world.openfoodfacts.org/data
•     USDA FoodData Central: https://fdc.nal.usda.gov/
•     Kroger Developer Portal: https://developer.kroger.com/
•     OpenStreetMap Overpass API: https://wiki.openstreetmap.org/wiki/Overpass_API
•     OSRM routing engine: https://project-osrm.org/
•     TomTom Search and Routing APIs (current POC): https://developer.tomtom.com/
•     Google Gemini API (language model): https://ai.google.dev/
2.4 Data Rights and Privacy
•     The Open Food Facts database is under the Open Database License (ODbL), its contents under the Database Contents License and its images under CC BY-SA. We will credit OFF and keep any derived database under the same terms.
•     USDA FoodData Central is in the public domain (CC0). We still credit it.
•     OpenStreetMap data is under the ODbL and is credited in the app.
•     We use the Kroger and TomTom APIs under their developer terms. We do not keep a permanent copy of retailer catalogs: results are cached for the session, and only high-availability results are kept briefly across sessions.
•     The demo has no real users. It uses fictional personas, and no names, addresses, health conditions or payment details of real people are collected or stored. Pregnancy and medical diet flags would be treated as sensitive health data in a production version (consent, encryption, deletion), even though the demo values are fictional.
•     The conversation and filtered product data are sent to the Gemini API. On unpaid tiers Google may use inputs to improve its products, so we never send real personal data. API keys are kept in a secret manager and never appear in logs or error messages.
•     The assistant gives information and shows its evidence. It does not certify that a product is safe and is not medical advice.
3. Data Planning and Splits
3.1 Data processing
1.   The language model turns the conversation into a structured recipe (ingredients, quantities, alternatives, servings), which is validated against a strict schema.
2.   Each ingredient and its alternatives become search terms, and the providers are queried in the background across nearby stores.
3.   Products are joined by barcodes (UPC/GTIN) to Open Food Facts and USDA records. Product names alone are never used as evidence.
4.   Recipe quantities and package sizes are converted to common units to work out package counts and leftovers.
5.   Each product gets an evidence grade (certified label, structured ingredient data, nutrition panel, name only, unknown), and the diet rules run every time results are read.
6.   API responses go through schema and range checks. Malformed or explicitly out-of-stock items are dropped, and missing values stay missing.
3.2 Caching
Everything found in a session is kept in a session cache, stored before diet filtering. Users can change their mind (mushrooms, then spinach, then mushrooms again) without repeating any search, and a diet change only re-filters what is already cached. Only high-availability results and stable facts such as store locations and barcode evidence are kept across sessions, with a short expiry.
3.3 Splits
The project does not train a model, so there is no training split. The splits apply to the evaluation data:
•     The golden diet set is split per diet category into a development set (about 70%), used while writing rules and prompts, and a held-out test set (about 30%), used only for the CI gate and milestone checks. New failure cases go into the development set first.
•     The golden conversations and demo scripts are a fixed set per persona, used as regression tests for recipe coherence and preference retention.
•     Product-match corrections are saved as a versioned dataset for a future product-matching model, which is outside this project's scope.
4. GitHub Repository
Planned structure:
•     backend/ holds the FastAPI service: chat orchestrator, provider adapters, diet rules engine, cache, package matcher and route optimizer.
•     frontend/ holds the React chat app: chat window, profile sidebar, confirmation card, results and route views.
•     data/ holds diet_rules.yaml, the demo personas and the golden evaluation sets.
•     pipelines/ holds the scripts that import Open Food Facts and USDA bulk data.
•     evals/ and tests/ hold the rule, contract and conversation tests (the proof of concept already has 30 automated tests).
•     infra/ holds the GCP deployment configuration, and docs/ holds the project plan and architecture diagrams.
The README will cover setup, the required API keys, how to run the demo locally and on GCP, and how to run the tests.
5. Project Scope
5.1 Problems
•     Finding food that fits a special diet takes many disconnected steps: recipe, substitutions, label checks, store search and trip planning.
•     Product data is incomplete and inconsistent. A product that sounds vegan or gluten-free may not be, and missing allergen data is often treated as safe.
•     Availability is uncertain. A nearby store, a catalog listing and a branch-level stock report are very different kinds of evidence, but apps rarely say which one they show.
•     Changing one choice, such as a filling, the number of servings or the budget, usually means starting the search over.
5.2 Current Solutions
•     Recipe apps suggest dishes and filter by diet, but do not connect to local products or stock.
•     Grocery and delivery apps (store apps, Instacart) show catalogs and prices for one retailer or service, with limited diet checking.
•     Label scanners, such as apps built on Open Food Facts, check one product at a time while in the store.
•     Dietitians and manual research are accurate but slow and not available on demand.
5.3 Proposed Solutions
•     One conversation that adapts the recipe to the full diet profile and asks before making a change that conflicts with it.
•     A background search that starts with the first message, so the plan is ready seconds after the user confirms.
•     Rule-based diet checks with graded evidence. The language model explains but never decides that a product is safe.
•     A shopping list with package counts and leftovers, a store list labeled by evidence strength, and an optimized route with one-stop, fastest and cheapest options.
•     A session cache so changes of mind cost nothing, and free or open data sources wherever possible.





6. Current Approach Flow Chart and Bottleneck Detection
The flow below shows how the conversation and the background search overlap, and how a change of mind is answered from the cache.

Figure 1: Search while you talk: the conversation and the background search run in parallel
Every product then passes the diet check before it can appear in the plan:

Figure 2: Diet check: evidence grades, the decision flow, and what happens when evidence is missing
Bottlenecks and mitigations
Bottleneck
Why it matters
Mitigation
Language model latency per turn
Each reply waits on the model
Stream replies; keep searches independent of the chat; small structured outputs
Search fan-out (ingredients × alternatives × stores)
Many API calls, quotas and cost
Per-session call budget, prioritize selected ingredients, session cache, cancel stale work
Barcode evidence lookups
Public OFF API is rate limited (the POC paces it to 1 request/second)
Local lookups from the OFF and USDA bulk data; cache barcode evidence
Sparse evidence (certifications, pasteurization, nutrition)
Many items are withheld for strict diets
Show the gap honestly; add sources; suggest alternatives
Multi-stop route optimization
Store combinations grow quickly
Limit candidate stores; use an optimizer (OSRM trip / VROOM or TomTom)
Live provider failures during the presentation
Demo reliability
Independent provider failure handling, fallbacks, pre-loaded cache

 
7. Metrics, Objectives, and Business Goals
7.1 Objectives
•     Deliver a reliable, end-to-end demo of the conversation, the background search and the three outputs for every diet category by December 8.
•     Never show a product that breaks a recorded diet rule, and never present unknown evidence as safe.
•     Make changes of mind instant and the final plan fast.
7.2 Metrics
Metric
Target
Diet-rule violations in shown products
0
Golden diet set pass rate (held-out test set)
100% (CI gate)
Time from confirmation to plan
About 5 seconds
Repeated provider calls after a change of mind
0 (answered from the session cache)
Ingredients with a store-level product that fits the diet
Tracked per diet category and reported honestly
Provider calls per session
Within the per-session call budget
Demo script reliability
Each script runs cleanly 5 times in a row

 
7.3 Business Goals
•     Show a working product for an underserved audience: people with special diets, for whom ordinary grocery tools fall short.
•     Demonstrate what sets it apart: diet-first checks, results in seconds, and honesty about evidence.
•     Present a credible path from the demo to a product: broader retailer coverage, accounts, checkout handoff, multi-meal planning and a product-matching model.
8. Failure Analysis
Failure
Impact
Detection
Mitigation
The model misreads or drops an allergy or diet need
Unsafe suggestion
Golden conversations; profile shown in the UI
Profile is structured and confirmed once; rules engine enforces it on every read
Wrong product match (wrong size or variety)
Wrong shopping list
Match score; user corrections
Low scores ask the user; corrections are logged
Missing or stale evidence
Items withheld or uncertain
Evidence grade per product
Withhold for strict diets; label "not verified" for lifestyle diets
Stock or price changed since the lookup
User finds the item unavailable
Data freshness on results
Short cache expiry; label availability as reported, not guaranteed
Provider outage, quota or rate limit
Missing results
Error and quota monitoring
Independent failures, fallback order, cooldowns, clear warnings
Explanation claims more than the evidence
Misleading answer
Answer checks against the plan data
Block unsupported claims; deterministic table remains available
Live failure during the presentation
Failed demo
Rehearsals and pre-demo checks
Pre-loaded cache, rehearsed scripts, graceful degradation
Leaked API keys
Security and cost
Secret scanning; sanitized errors
Secret manager; errors never include raw requests

 


9. Deployment Infrastructure
•     The system runs on Google Cloud Platform.
•     The FastAPI backend runs on Cloud Run as a single instance for the demo, so the in-memory session cache stays consistent. Replies stream to the browser over server-sent events.
•     The React web app is served from Cloud Run or static hosting.
•     The session cache lives in memory. The small cross-session cache uses Valkey (open-source Redis) or SQLite; we will decide which.
•     API keys are stored in Google Secret Manager.
•     Automated tests and the golden diet set run on every change, and the app deploys only when they pass.
•     External services are Gemini, Kroger, Open Food Facts, USDA FoodData Central and the chosen map and routing provider.
10. Monitoring Plan
•     Structured logs record every chat turn, provider call, cache hit or miss, and diet decision, along with the rule and evidence behind it.
•     Cloud Monitoring dashboards show latency per step (model reply, searches, plan), provider error and quota rates, cache hit rate, and language model token usage and cost.
•     Alerts fire on provider failures or exhausted quotas, latency above target, and any diet-rule violation found in audits.
•     The golden diet set and golden conversations run in CI on every change, and new failures become new test cases.
•     A small stats panel in the app shows stores searched, calls made, cache hits and time per step, for debugging and during the demo.
•     Before every presentation we check provider health, pre-load the cache and do one full script run.
11. Success and Acceptance Criteria
Milestone
Acceptance criteria
Day 30 (Nov 1): core demo engine
End-to-end quiche flow on GCP with the demo profile; a change of mind triggers no repeated provider calls; the POC tests ported, plus cache-reuse tests
Day 60 (Dec 1): every diet, plus list, stores and route
Plan within about 5 seconds of confirmation; every diet category has a passing demo case; golden diet set passes 100% in CI
Final presentation (Dec 8)
2–3 rehearsed demo scripts each run cleanly 5 times in a row; the presentation links each demo feature to future work

 
Across all milestones: zero products shown that break a recorded diet rule, and no runtime data that is made up or mocked.
12. Timeline Planning
We have about nine weeks, from October 3 to the final presentation on December 8. The plan has two 30-day phases and a short final week reserved for rehearsal, with no new features.
Phase
Dates
Focus
Key deliverables
Phase 1 (days 1–30)
Oct 3 – Nov 1
Core demo engine
FastAPI backend with streaming chat on GCP; React chat with profile sidebar and confirmation card; POC engine and provider adapters ported; session cache with the diet filter applied on read; diet rules v1 (allergens, celiac, vegan and vegetarian); POC tests ported plus cache-reuse tests
Phase 2 (days 31–60)
Nov 2 – Dec 1
Every diet, plus list, stores and route
Diet rules v2 (pregnancy, medical, religious) with evidence grades; nutrition source; package matcher; route optimizer with one-stop, fastest and cheapest options; list, store and route views with export; answer checks; cross-session cache; stats panel; golden diet set in CI
Final week
Dec 2 – Dec 8
Rehearsal and presentation
Feature freeze; 2-3 demo personas and scripts; pre-loaded cache and graceful handling of provider failures; rehearsals; final presentation on December 8

 
Workstream owners will be assigned within the team: platform, demo app, conversation, diet rules, search and cache, matching and route, quality, and demo and presentation.
13. Additional Information
Out of scope for this project
•     Login and accounts, payments and checkout, real customers, native mobile apps, multi-meal planning, and training a product-matching model.
•     Follow-up edits after the plan, such as "keep it under $30" or "only one store", are planned as future work.
Open decisions
•     Whether to replace TomTom with OpenStreetMap services (Overpass, Nominatim, OSRM/VROOM) to stay fully free and open.
•     Whether to keep or drop the optional paid fallbacks (RedCircle for Target, Spoonacular).
•     Whether to allow a recorded backup of API responses for the live presentation, given the live-data-only principle.
•     Which personas go into the main demo.

Ethical considerations
•     The assistant gives information, not medical or religious rulings. Every diet rule cites its source (FDA, CDC, USDA, labeling standards), and expert review is recommended before any real-world use.
•     Uncertainty is shown, not hidden. Many items will be withheld for strict diets, and the product says so.
Supporting material
•     The detailed project plan and architecture diagrams are in the project repository.

