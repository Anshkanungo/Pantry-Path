Pantry Path: a conversational food finder for special diets
Project Team 11
Team Members:
•     Ansh Manoj Kanungo
•     Kenil Prafulbhai Devani
•     Khushal Bharatkumar Trivedi
•     Shivu Donmardi Gorva
•     Tirth Ghanshyambhai Borasaniya
 

# Pantry Path - Project Summary

Conversational assistant that turns "I feel like quiche tonight" into a diet-safe
shopping list, store availability, and an optimized multi-stop route - in one chat.

## Problem
People with special diets (allergies, celiac, pregnancy, medical, religious, lifestyle)
can't just buy groceries normally. Recipe → substitutions → label checks → store search →
route planning today means juggling 4-5 disconnected tools.

## Approach
- LLM (Gemini) interprets the conversation and adapts the recipe; deterministic rules
  decide diet safety - the model never decides what's safe to eat.
- Background search starts as soon as the user starts chatting, so results are ready
  within ~5 seconds of confirmation.
- Every product gets an evidence grade (certified label → structured data → nutrition
  panel → name only → unknown). Unknown is never shown as safe.

## Data sources
Open Food Facts (ingredients/allergens), USDA FoodData Central (nutrition), Kroger API
(live stock/price), OSM/TomTom (routing).

## Scope
**In:** chat-driven recipe adaptation, diet rule engine, shopping list, store list with
evidence grading, multi-stop route, session cache.
**Out (for this phase):** accounts, payments, native apps, multi-meal planning.

## Timeline
- Day 30 (Nov 1): core demo engine, one diet category working end-to-end
- Day 60 (Dec 1): all diet categories, full list/store/route output
- Dec 8: final presentation, rehearsed demo scripts

## Success criteria
Zero diet-rule violations shown. Golden eval set passes 100% in CI. Plan ready in ~5s.
