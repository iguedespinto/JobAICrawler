## Job AI Crawler

Personal Flask app for collecting and reviewing job opportunities, stored in
MongoDB Atlas. Opportunities are prepared externally and brought in through a
JSON import flow.

### Run locally

1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Export required env vars (see below).
4. Run the app:
   - `flask --app wsgi run`

### Importing opportunities

1. Open `/import`.
2. Upload a JSON file. The file is an array of objects (a single object is also
   accepted) with these fields:
   - `name` (job title), `company`, `url`, `salary`, `description`, `keywords`.
3. Review the preview. Each opportunity is matched against what is already in the
   database: first by URL, then by title + company. New opportunities are
   pre-selected; matches are not.
4. Adjust the selection and click **Import selected** to create them.

### Environment variables

Required:
- `MONGODB_URI` - MongoDB Atlas connection string.

Optional:
- `MONGO_DB_NAME` - Database name (default: `jobs_db`).
- `SECRET_KEY` - Flask session signing key for flashed messages
  (default: a development value).

### Deploy to Heroku

1. Create a Heroku app:
   - `heroku create <app-name>`
2. Set config vars:
   - `heroku config:set MONGODB_URI="..."`
3. Deploy:
   - `git push heroku main`
