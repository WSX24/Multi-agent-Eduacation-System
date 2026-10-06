# Backend

Django + Django REST Framework API for the teaching platform.

## Local setup (Windows PowerShell)

From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

The health endpoint is available at `http://127.0.0.1:8000/api/health/`. After starting Django, open `http://127.0.0.1:8000/api/docs/` for the interactive Swagger UI, `http://127.0.0.1:8000/api/schema/` for the OpenAPI schema, or `http://127.0.0.1:8000/api/redoc/` for the ReDoc view. The account API contract is also documented in `../docs/02-api-backend.md`.

## Account API

The six account routes under `/api/accounts/` support registration codes, registration, login, access-token refresh, the current user, and logout. Registration requires a valid email code; successful registration and login return JWT access and refresh tokens. The access token lasts 15 minutes and the refresh token lasts 7 days. Logout blacklists the refresh token.

Run `python manage.py migrate` after installing dependencies so the account and token-blacklist tables exist. For local development Django creates an ignored `.dev-secret-key` file to keep JWTs valid across server restarts. Set `DJANGO_SECRET_KEY` to a private, stable value for any deployment; never commit it.

## Registration email codes

`POST /api/accounts/email-codes/` generates a six-digit registration code for
the address in the request and prints an email-formatted message in the Django
terminal running `manage.py runserver`. It is not delivered to the inbox, and
the API response never includes the code. Keep the backend terminal open while
requesting a code, then copy it from the printed message into the registration
form.

If ports 8000 and 5173 are already in use, start Django on another free port,
then set the Vite proxy target to the same backend port. For example, start Vue
in a second PowerShell terminal from `frontend/` with:

```powershell
$env:VITE_API_TARGET = "http://127.0.0.1:8001"
npm run dev -- --host 127.0.0.1 --port 5174
```

Open `http://127.0.0.1:5174/auth`. The Vue proxy will send `/api` requests to
the Django server on port 8001.

Start the Vue dev server separately. Vite proxies `/api` requests to
`http://127.0.0.1:8000`, so the registration page can call this endpoint without
a separate browser CORS configuration.

## Current structure

- `config/`: project settings and root URL routing.
- `apps/accounts/`: email-based user model, registration codes, and JWT account endpoints.
- `apps/courses/`: course domain app and the initial health-check API; course models and endpoints will be added here.

The initial user model is configured before the first migration. Keep the local `.venv/` and `db.sqlite3` out of Git; both are ignored by this directory's `.gitignore`.
