# Cache API initial

A FastAPI-based cache normalization service for sports betting data (teams, players, and markets).

## Features

- **RESTful API** for cache lookups
- **Flexible queries** supporting market, team, or player parameters
- **SQLite database** with comprehensive sports data (teams, players, markets)
- **Automated deployment** via GitHub Actions
- **Systemd service** management

## API Endpoints

### GET /cache

Retrieve normalized cache entries.

**Parameters:**

- `market` (optional): Market type (e.g., "moneyline", "spread", "total")
- `team` (optional): Team name to look up
- `player` (optional): Player name to look up
- `sport` (optional): Sport name - **required when searching by team**

**Examples:**

```bash
# Look up a team (sport is required)
curl "http://142.44.160.36:8001/cache?team=Lakers&sport=Basketball"

# Look up a player
curl "http://142.44.160.36:8001/cache?player=LeBron%20James"

# Look up a market
curl "http://142.44.160.36:8001/cache?market=moneyline"
```

**Response Format:**

```json
{
  "found": true,
  "data": {
    "type": "team",
    "query": "Lakers",
    "normalized_name": "Los Angeles Lakers",
    "abbreviation": "LAL",
    "league": "NBA",
    "aliases": ["LA Lakers", "Lakers", "L.A. Lakers"]
  },
  "query": {
    "market": null,
    "team": "Lakers",
    "player": null
  }
}
```

### GET /health

Health check endpoint for monitoring.

### GET /

Root endpoint showing service status.

## Local Development

### Prerequisites

- Python 3.8+
- pip

### Setup

1. Create a virtual environment:

```bash
python -m venv venv
```

2. Activate virtual environment:

```bash
# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Run the server:

```bash
python main.py
```

The API will be available at `http://localhost:8001`

## VPS Deployment

### Initial Setup (One-time)

1. **SSH into your VPS:**

```bash
ssh ubuntu@142.44.160.36
```

2. **Create service directory:**

```bash
mkdir -p /home/ubuntu/services/cache-api
cd /home/ubuntu/services/cache-api
```

3. **Initialize git repository:**

```bash
# Replace with your actual GitHub repo URL
git init
git remote add origin https://github.com/YOUR_USERNAME/cache-api.git
git fetch origin
git checkout main
```

4. **Create Python virtual environment:**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

5. **Set up systemd service:**

```bash
sudo cp cache-api.service /etc/systemd/system/cache-api.service
sudo systemctl daemon-reload
sudo systemctl enable cache-api
sudo systemctl start cache-api
```

6. **Verify service is running:**

```bash
sudo systemctl status cache-api
curl http://localhost:8000/health
```

### GitHub Secrets Configuration

Add these secrets to your GitHub repository (Settings → Secrets and variables → Actions):

- `VPS_HOST`: 142.44.160.36
- `VPS_USERNAME`: ubuntu
- `VPS_SSH_KEY`: Your private SSH key
- `VPS_PORT`: 22 (or your custom SSH port)

### Automated Deployment

Once configured, the service will automatically deploy when you:

1. Push to the `main` branch
2. Manually trigger the workflow from GitHub Actions

The GitHub Actions workflow will:

- Pull latest code
- Update dependencies
- Restart the service
- Verify deployment

## Project Structure

```
cache-api/
├── .github/
│   └── workflows/
│       └── deploy.yml          # GitHub Actions deployment workflow
├── main.py                      # FastAPI application
├── cache_db.py                  # Database access layer
├── sports_data.db              # SQLite database with sports data
├── requirements.txt             # Python dependencies
├── cache-api.service           # Systemd service configuration
├── .gitignore                  # Git ignore patterns
└── README.md                   # This file
```

## Database

The application uses a SQLite database (`sports_data.db`) containing:

- **Sports**: Various sports with leagues
- **Teams**: Team information including abbreviations, cities, mascots
- **Players**: Player details with team and league associations
- **Markets**: Betting market types and associations

The database is accessed through `cache_db.py` which provides query functions for:
- Team lookups (by name, abbreviation, or nickname)
- Player lookups (by full name)
- Market lookups (by market name)

## Monitoring & Logs

View service logs:

```bash
sudo journalctl -u cache-api -f
```

Check service status:

```bash
sudo systemctl status cache-api
```

Restart service manually:

```bash
sudo systemctl restart cache-api
```

## Port Configuration

The service runs on port **8001** by default (port 8000 is used by unified-odds service). To change:

1. Update `main.py` (line with `uvicorn.run`)
2. Update `deploy.yml` port check commands
3. Ensure firewall allows the new port

## License

Private - Internal Use Only
