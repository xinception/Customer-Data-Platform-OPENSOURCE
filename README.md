# Customer Data Platform (CDP) - Open Source

A full-featured, open-source Customer Data Platform built with FastAPI, SQLAlchemy, Redis, and scikit-learn. Unify customer data, track behaviour in real time, leverage AI-driven segmentation, and orchestrate marketing automation -- all from a single platform.

## Architecture

```
                          +---------------------+
                          |   Browser / Mobile   |
                          +----------+----------+
                                     |
                           JS Tracker / SDK
                                     |
                          +----------v----------+
                          |     FastAPI App      |
                          |     (cdp-api)        |
                          |   :8000              |
                          +--+-------+-------+--+
                             |       |       |
              +--------------+   +---+---+   +--------------+
              |                  |       |                   |
     +--------v--------+ +------v-----+ +--------v--------+ |
     |   PostgreSQL 15  | | Redis 7    | | Celery Workers  | |
     |   :5432          | | :6379      | | (async tasks)   | |
     |                  | |            | +---------+-------+ |
     |  - Customers     | | - Cache    |           |         |
     |  - Events        | | - Sessions |  +--------v-------+ |
     |  - Campaigns     | | - Queues   |  | Celery Beat    | |
     |  - AI Segments   | |            |  | (scheduled)    | |
     +------------------+ +------------+  +----------------+ |
                                                             |
                          +----------------------------------+
                          |       AI / ML Engine
                          |  - RFM Segmentation
                          |  - Churn Prediction
                          |  - LTV Forecasting
                          |  - Recommendations
                          +----------------------------------+
```

## Features

### AI & Machine Learning
- RFM-based customer segmentation (Champions, Loyal, At Risk, etc.)
- Churn probability prediction
- Customer lifetime value (LTV) forecasting
- Personalised product recommendations
- Automatic segment recomputation

### Real-Time Tracking
- Lightweight JavaScript tracker for page views and custom events
- Server-side event ingestion API
- Session stitching and identity resolution
- Cookie consent management (GDPR-friendly)
- UTM parameter capture

### Marketing Automation
- Multi-channel campaigns (email, SMS, push, in-app, webhook)
- Automation workflows with event/time/segment triggers
- Campaign performance analytics (open, click, conversion rates)
- Per-channel marketing consent management
- A/B testing support

### Customer Profiles
- Unified 360-degree customer view
- Identity resolution across email, phone, cookie, device, and social
- Custom attributes with confidence scoring
- Engagement and risk scoring
- Tagging and flexible metadata

## Quick Start (Docker Compose)

The fastest way to get the full stack running locally:

```bash
# Clone the repository
git clone https://github.com/your-org/Customer-Data-Platform-OPENSOURCE.git
cd Customer-Data-Platform-OPENSOURCE

# Copy environment config
cp .env.example .env

# Start all services
docker compose up -d

# Verify everything is running
curl http://localhost:8000/health
```

This starts the API server, PostgreSQL, Redis, Celery worker, and Celery beat.

### Seed Sample Data

```bash
docker compose exec cdp-api python -m scripts.seed_data
```

This populates the database with 100 fake customers, events, sessions, campaigns, and AI segments.

## Manual Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 15+
- Redis 7+

### Installation

```bash
# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and edit environment config
cp .env.example .env
# Edit .env with your database and Redis connection strings

# Run database migrations
alembic upgrade head

# Start the development server
uvicorn cdp.main:app --reload --host 0.0.0.0 --port 8000
```

### Start Celery Workers

```bash
# In a separate terminal - task worker
celery -A cdp.services.celery_app worker --loglevel=info

# In another terminal - scheduled tasks
celery -A cdp.services.celery_app beat --loglevel=info
```

## API Documentation

Once the server is running, interactive API documentation is available at:

| Format  | URL                              |
|---------|----------------------------------|
| Swagger | http://localhost:8000/docs        |
| ReDoc   | http://localhost:8000/redoc       |
| OpenAPI | http://localhost:8000/openapi.json |

### Key Endpoints

| Method | Path                        | Description                    |
|--------|-----------------------------|--------------------------------|
| GET    | `/health`                   | Health check                   |
| POST   | `/api/v1/auth/login`        | Obtain JWT token               |
| GET    | `/api/v1/customers`         | List customers (paginated)     |
| POST   | `/api/v1/customers`         | Create customer profile        |
| GET    | `/api/v1/customers/{id}`    | Get customer 360 view          |
| POST   | `/api/v1/tracking/events`   | Ingest tracking event          |
| POST   | `/api/v1/tracking/consent`  | Record cookie consent          |
| GET    | `/api/v1/campaigns`         | List campaigns                 |
| POST   | `/api/v1/campaigns`         | Create campaign                |
| GET    | `/api/v1/analytics/segments`| List AI segments               |
| POST   | `/api/v1/workflows`         | Create automation workflow      |

## Configuration Reference

All settings are managed via environment variables (or a `.env` file).

| Variable                     | Default                                          | Description                              |
|------------------------------|--------------------------------------------------|------------------------------------------|
| `APP_NAME`                   | `Customer Data Platform`                         | Application display name                 |
| `DEBUG`                      | `false`                                          | Enable debug mode and SQL echo           |
| `DATABASE_URL`               | `postgresql+asyncpg://cdp:cdp@localhost:5432/cdp`| Async PostgreSQL connection string       |
| `REDIS_URL`                  | `redis://localhost:6379/0`                       | Redis connection string                  |
| `SECRET_KEY`                 | `change-me-in-production`                        | Secret for JWT signing                   |
| `JWT_ALGORITHM`              | `HS256`                                          | JWT algorithm                            |
| `ACCESS_TOKEN_EXPIRE_MINUTES`| `30`                                             | JWT token expiry in minutes              |
| `AI_MODEL_PATH`              | `./models`                                       | Path to stored ML model files            |
| `COOKIE_DOMAIN`              | `localhost`                                      | Domain for tracking cookies              |
| `COOKIE_MAX_AGE`             | `31536000`                                       | Cookie max age in seconds (1 year)       |
| `CONSENT_REQUIRED`           | `true`                                           | Require consent before tracking          |
| `SMTP_HOST`                  | `localhost`                                      | SMTP server hostname                     |
| `SMTP_PORT`                  | `587`                                            | SMTP server port                         |
| `SMTP_USER`                  | (empty)                                          | SMTP username                            |
| `SMTP_PASSWORD`              | (empty)                                          | SMTP password                            |
| `AWS_ACCESS_KEY`             | (empty)                                          | AWS access key for S3                    |
| `AWS_SECRET_KEY`             | (empty)                                          | AWS secret key for S3                    |
| `AWS_REGION`                 | `us-east-1`                                      | AWS region                               |
| `S3_BUCKET`                  | (empty)                                          | S3 bucket name                           |
| `CELERY_BROKER_URL`          | `redis://localhost:6379/1`                       | Celery broker (Redis DB 1)               |
| `CELERY_RESULT_BACKEND`      | `redis://localhost:6379/2`                       | Celery result backend (Redis DB 2)       |

## Database Migrations

This project uses [Alembic](https://alembic.sqlalchemy.org/) for database schema migrations.

```bash
# Create a new migration after model changes
alembic revision --autogenerate -m "description of changes"

# Apply all pending migrations
alembic upgrade head

# Rollback one migration
alembic downgrade -1

# View migration history
alembic history
```

## Project Structure

```
Customer-Data-Platform-OPENSOURCE/
├── cdp/
│   ├── api/routes/          # FastAPI route handlers
│   ├── models/              # SQLAlchemy ORM models
│   ├── services/            # Business logic & AI engine
│   ├── static/              # JS tracker & assets
│   ├── templates/           # Email & campaign templates
│   ├── utils/               # Shared utilities
│   ├── config.py            # Pydantic settings
│   ├── database.py          # Async SQLAlchemy setup
│   └── main.py              # FastAPI app entry point
├── migrations/              # Alembic migration scripts
├── scripts/                 # Utility scripts (seed data, etc.)
├── tests/                   # Test suite
├── docker-compose.yml       # Full-stack Docker setup
├── Dockerfile               # Container image definition
├── alembic.ini              # Alembic configuration
├── requirements.txt         # Python dependencies
└── .env.example             # Environment variable template
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
