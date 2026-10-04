# Pantry Path

Conversational food finder for special diets. Built for Team 11.

## Setup
1. Clone the repo
2. Copy `.env.example` to `.env` and fill in API keys
3. Backend: `cd backend && pip install -r requirements.txt`
4. Frontend: `cd frontend && npm install`

## Structure
- `backend/` - FastAPI service
- `frontend/` - React chat app
- `data/` - diet rules, personas, golden eval sets
- `pipelines/` - OFF/USDA bulk data import scripts
- `evals/`, `tests/` - rule, contract, conversation tests
- `infra/` - GCP deployment config
- `docs/` - project plan, architecture diagrams
