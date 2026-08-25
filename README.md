# Dispatch

You call a number or type a task. A real Chrome window does the clicking, and you watch it happen.

If you want listings, it uses Facebook Marketplace. Not Craigslist. If you just say hi, it waits.

## What's running it

- Python / FastAPI
- [browser-use](https://github.com/browser-use/browser-use) + Claude
- Real Chrome when it's installed on the machine
- Next.js page that streams screenshots of that Chrome window
- Twilio for the phone part

## Setup

You need Python 3.11+, Node 20+, an [Anthropic key](https://console.anthropic.com/), and Chrome (or `uvx browser-use install`).

Linux with no screen: install `Xvfb`.

### Backend

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvx browser-use install     # once per machine
cp .env.example .env        # then set ANTHROPIC_API_KEY
uvicorn main:app --reload --port 8000
```

Facebook email and password go in `LOGIN_USERNAME` / `LOGIN_PASSWORD` in `backend/.env`. Don't commit that file. Login cookies live in `backend/.browser-profile`.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

Try:

```text
Search Facebook Marketplace for a used couch near me and tell me the top listings with prices.
```

Hit **Run**. Chrome should show up in the live view.

## Phone

The page checks for new tasks every couple seconds, so a call shows up without you copying a task id.

1. Get a Twilio number with Voice.
2. Expose the backend (Cloudflare Tunnel or ngrok).
3. Put that HTTPS URL in `PUBLIC_BASE_URL` in `backend/.env`.
4. In Twilio, set **A call comes in** to `POST` `https://<your-tunnel>/voice`.
5. Restart the backend, call the number, say what you want.

One thing at a time. A second run or call while something is going gets a 409.

## API

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/tasks` | `{ "instruction": string }` → `{ "task_id": "..." }` |
| `POST` | `/tasks/stop` | Stop the current run |
| `POST` | `/tasks/reset` | Stop and clear the demo |
| `GET` | `/tasks` | Latest task (so the page can pick up a call) |
| `WS` | `/ws/{task_id}` | Screenshots, log lines, done |
| `POST` | `/voice` | Inbound call |
| `POST` | `/voice/collect` | Speech result starts the agent |
