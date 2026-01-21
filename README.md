## Job AI Crawler

Personal Flask app that scrapes job postings, enriches them with an LLM, and stores results in MongoDB Atlas.

### Run locally

1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Export required env vars (see below).
4. Run the app:
   - `flask --app wsgi run`

### Environment variables

Required:
- `MONGODB_URI` - MongoDB Atlas connection string.
- `LLM_API_KEY` - API key for the LLM provider.
- `LLM_BASE_URL` - Base URL for the LLM chat completion endpoint.
- `LLM_MODEL` - Model identifier for the LLM provider.

Optional:
- `MONGO_DB_NAME` - Database name (default: `jobs_db`).

### Deploy to Heroku

1. Create a Heroku app:
   - `heroku create <app-name>`
2. Set config vars:
   - `heroku config:set MONGODB_URI="..." LLM_API_KEY="..." LLM_BASE_URL="..." LLM_MODEL="..."`
3. Deploy:
   - `git push heroku main`
4. Run the pipeline manually (optional):
   - `heroku run python -m app.pipeline all`
