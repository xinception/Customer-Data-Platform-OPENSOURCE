"""Seed the database with realistic fake data for development and demos.

Usage:
    python -m scripts.seed_data

Generates:
    - 100 customers with names, emails, phones
    - Customer events, sessions, and page views
    - Sample marketing campaigns
    - AI segments (Champions, Loyal, At Risk, etc.)
"""

from __future__ import annotations

import asyncio
import random
import string
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = "postgresql+asyncpg://cdp:cdp_secret@localhost:5432/cdp"
NUM_CUSTOMERS = 100

# ---------------------------------------------------------------------------
# Realistic data pools
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Christopher", "Karen", "Charles",
    "Lisa", "Daniel", "Nancy", "Matthew", "Betty", "Anthony", "Margaret",
    "Mark", "Sandra", "Donald", "Ashley", "Steven", "Kimberly", "Paul",
    "Emily", "Andrew", "Donna", "Joshua", "Michelle", "Kenneth", "Carol",
    "Kevin", "Amanda", "Brian", "Dorothy", "George", "Melissa", "Timothy",
    "Deborah", "Ronald", "Stephanie", "Edward", "Rebecca", "Jason", "Sharon",
    "Jeffrey", "Laura", "Ryan", "Cynthia", "Jacob", "Kathleen", "Gary",
    "Amy", "Nicholas", "Angela", "Eric", "Shirley", "Jonathan", "Anna",
    "Stephen", "Brenda", "Larry", "Pamela", "Justin", "Emma", "Scott",
    "Nicole", "Brandon", "Helen", "Benjamin", "Samantha", "Samuel", "Katherine",
    "Raymond", "Christine", "Gregory", "Debra", "Frank", "Rachel", "Alexander",
    "Carolyn", "Patrick", "Janet", "Jack", "Catherine", "Dennis", "Maria",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
    "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts",
]

EMAIL_DOMAINS = [
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "protonmail.com", "fastmail.com", "zoho.com", "aol.com", "mail.com",
]

SOURCES = ["website", "mobile_app", "api", "import", "facebook", "google_ads"]

SEGMENTS = ["Champions", "Loyal", "Potential Loyalist", "At Risk",
            "Hibernating", "New Customers", "About to Sleep", "Lost"]

EVENT_TYPES = [
    ("page_view", "Viewed Page"),
    ("product_view", "Viewed Product"),
    ("add_to_cart", "Added to Cart"),
    ("remove_from_cart", "Removed from Cart"),
    ("purchase", "Completed Purchase"),
    ("signup", "Signed Up"),
    ("login", "Logged In"),
    ("search", "Searched"),
    ("wishlist_add", "Added to Wishlist"),
    ("review_submit", "Submitted Review"),
    ("email_open", "Opened Email"),
    ("email_click", "Clicked Email Link"),
    ("form_submit", "Submitted Form"),
    ("video_play", "Played Video"),
    ("share", "Shared Content"),
]

PAGE_URLS = [
    "/", "/products", "/products/electronics", "/products/clothing",
    "/products/home-garden", "/cart", "/checkout", "/account",
    "/account/orders", "/account/settings", "/blog", "/blog/tips",
    "/about", "/contact", "/pricing", "/faq", "/support",
]

PAGE_TITLES = [
    "Home", "Products", "Electronics", "Clothing", "Home & Garden",
    "Shopping Cart", "Checkout", "My Account", "Order History",
    "Account Settings", "Blog", "Tips & Tricks", "About Us",
    "Contact Us", "Pricing", "FAQ", "Support",
]

BROWSERS = ["Chrome", "Firefox", "Safari", "Edge", "Opera"]
DEVICES = ["desktop", "mobile", "tablet"]
OPERATING_SYSTEMS = ["Windows", "macOS", "Linux", "iOS", "Android"]
COUNTRIES = ["US", "GB", "CA", "AU", "DE", "FR", "JP", "BR", "IN", "MX"]
CITIES = [
    "New York", "London", "Toronto", "Sydney", "Berlin", "Paris",
    "Tokyo", "Sao Paulo", "Mumbai", "Mexico City", "Los Angeles",
    "Chicago", "Houston", "San Francisco", "Seattle",
]

UTM_SOURCES = ["google", "facebook", "twitter", "linkedin", "newsletter", None]
UTM_MEDIUMS = ["cpc", "social", "email", "organic", "referral", None]
UTM_CAMPAIGNS = ["spring_sale", "new_arrivals", "retargeting", "brand", None]

CAMPAIGN_TEMPLATES = [
    {
        "name": "Welcome Series",
        "description": "Automated welcome email series for new signups",
        "campaign_type": "email",
        "status": "active",
        "subject_line": "Welcome to our platform!",
    },
    {
        "name": "Re-engagement Campaign",
        "description": "Win back inactive customers with special offers",
        "campaign_type": "email",
        "status": "active",
        "subject_line": "We miss you! Here's 20% off",
    },
    {
        "name": "Cart Abandonment Reminder",
        "description": "Remind customers about items left in their cart",
        "campaign_type": "email",
        "status": "active",
        "subject_line": "You left something behind...",
    },
    {
        "name": "Flash Sale Push",
        "description": "Push notification for limited-time flash sales",
        "campaign_type": "push",
        "status": "scheduled",
        "subject_line": "Flash Sale - 24 hours only!",
    },
    {
        "name": "Product Launch SMS",
        "description": "SMS blast for new product launches",
        "campaign_type": "sms",
        "status": "draft",
        "subject_line": None,
    },
    {
        "name": "Loyalty Rewards Update",
        "description": "Monthly update on loyalty points and available rewards",
        "campaign_type": "email",
        "status": "completed",
        "subject_line": "Your loyalty rewards summary",
    },
    {
        "name": "Seasonal Promotion",
        "description": "Seasonal promotional campaign with personalized offers",
        "campaign_type": "email",
        "status": "paused",
        "subject_line": "Spring into savings!",
    },
    {
        "name": "In-App Onboarding",
        "description": "In-app messages guiding new users through key features",
        "campaign_type": "in_app",
        "status": "active",
        "subject_line": None,
    },
]

AI_SEGMENT_DEFINITIONS = [
    {
        "name": "Champions",
        "description": "High-value customers who buy often and recently. Top 10% by RFM score.",
        "model_type": "rfm_clustering",
        "criteria": {"recency_days_max": 14, "frequency_min": 10, "monetary_min": 500},
        "customer_count": 8,
    },
    {
        "name": "Loyal Customers",
        "description": "Frequent buyers with consistent engagement over time.",
        "model_type": "rfm_clustering",
        "criteria": {"recency_days_max": 30, "frequency_min": 6, "monetary_min": 200},
        "customer_count": 15,
    },
    {
        "name": "Potential Loyalists",
        "description": "Recent customers with moderate frequency showing loyalty signals.",
        "model_type": "rfm_clustering",
        "criteria": {"recency_days_max": 30, "frequency_min": 3, "monetary_min": 100},
        "customer_count": 18,
    },
    {
        "name": "At Risk",
        "description": "Previously active customers whose engagement is declining.",
        "model_type": "churn_prediction",
        "criteria": {"recency_days_min": 30, "recency_days_max": 90, "frequency_min": 3},
        "customer_count": 12,
    },
    {
        "name": "Hibernating",
        "description": "Customers who have not interacted in a long time.",
        "model_type": "churn_prediction",
        "criteria": {"recency_days_min": 90, "frequency_max": 2},
        "customer_count": 10,
    },
    {
        "name": "New Customers",
        "description": "Recently acquired customers with limited interaction history.",
        "model_type": "lifecycle_stage",
        "criteria": {"account_age_days_max": 30, "frequency_max": 2},
        "customer_count": 20,
    },
    {
        "name": "About to Sleep",
        "description": "Customers with decreasing activity who may become inactive soon.",
        "model_type": "churn_prediction",
        "criteria": {"recency_days_min": 45, "recency_days_max": 90, "frequency_max": 3},
        "customer_count": 9,
    },
    {
        "name": "High-Value Prospects",
        "description": "Predicted high lifetime value based on early behaviour patterns.",
        "model_type": "ltv_prediction",
        "criteria": {"predicted_ltv_min": 300, "account_age_days_max": 60},
        "customer_count": 8,
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

now = datetime.now(timezone.utc)


def random_phone() -> str:
    return f"+1{random.randint(200, 999)}{random.randint(100, 999)}{random.randint(1000, 9999)}"


def random_email(first: str, last: str) -> str:
    separators = [".", "_", ""]
    sep = random.choice(separators)
    suffix = random.randint(1, 999) if random.random() < 0.4 else ""
    domain = random.choice(EMAIL_DOMAINS)
    return f"{first.lower()}{sep}{last.lower()}{suffix}@{domain}"


def random_past(max_days: int = 365) -> datetime:
    return now - timedelta(
        days=random.randint(0, max_days),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
        seconds=random.randint(0, 59),
    )


def random_ip() -> str:
    return f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"


# ---------------------------------------------------------------------------
# Seeders
# ---------------------------------------------------------------------------

async def seed_customers(session: AsyncSession) -> list[dict]:
    """Insert 100 customers and return their data for later reference."""
    customers = []
    for _ in range(NUM_CUSTOMERS):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        cid = uuid.uuid4()
        created = random_past(365)
        segment = random.choice(SEGMENTS)
        ltv = round(random.uniform(0, 2000), 2)
        engagement = round(random.uniform(0, 100), 1)
        risk = round(random.uniform(0, 1), 3)

        customer = {
            "id": cid,
            "external_id": f"ext-{cid.hex[:12]}",
            "email": random_email(first, last),
            "phone": random_phone(),
            "first_name": first,
            "last_name": last,
            "source": random.choice(SOURCES),
            "is_active": random.random() > 0.1,
            "lifetime_value": ltv,
            "segment": segment,
            "risk_score": risk,
            "engagement_score": engagement,
            "tags": random.sample(
                ["vip", "newsletter", "beta", "early_adopter", "enterprise", "smb"],
                k=random.randint(0, 3),
            ),
            "metadata": {"signup_source": random.choice(SOURCES)},
            "created_at": created,
            "updated_at": created + timedelta(days=random.randint(0, 30)),
        }
        customers.append(customer)

        await session.execute(
            text("""
                INSERT INTO customers
                    (id, external_id, email, phone, first_name, last_name,
                     source, is_active, lifetime_value, segment, risk_score,
                     engagement_score, tags, metadata, created_at, updated_at)
                VALUES
                    (:id, :external_id, :email, :phone, :first_name, :last_name,
                     :source, :is_active, :lifetime_value, :segment, :risk_score,
                     :engagement_score, :tags::jsonb, :metadata::jsonb, :created_at, :updated_at)
            """),
            {
                **customer,
                "tags": str(customer["tags"]).replace("'", '"'),
                "metadata": str(customer["metadata"]).replace("'", '"'),
            },
        )

    await session.commit()
    print(f"  Seeded {len(customers)} customers")
    return customers


async def seed_events(session: AsyncSession, customers: list[dict]) -> None:
    """Generate random events for each customer."""
    count = 0
    for cust in customers:
        num_events = random.randint(3, 25)
        for _ in range(num_events):
            event_type, event_name = random.choice(EVENT_TYPES)
            ts = random_past(180)
            await session.execute(
                text("""
                    INSERT INTO customer_events
                        (id, customer_id, event_type, event_name, source,
                         session_id, page_url, ip_address, timestamp, processed)
                    VALUES
                        (:id, :customer_id, :event_type, :event_name, :source,
                         :session_id, :page_url, :ip_address, :timestamp, :processed)
                """),
                {
                    "id": uuid.uuid4(),
                    "customer_id": cust["id"],
                    "event_type": event_type,
                    "event_name": event_name,
                    "source": random.choice(SOURCES),
                    "session_id": str(uuid.uuid4()),
                    "page_url": random.choice(PAGE_URLS),
                    "ip_address": random_ip(),
                    "timestamp": ts,
                    "processed": random.random() > 0.2,
                },
            )
            count += 1

    await session.commit()
    print(f"  Seeded {count} customer events")


async def seed_sessions_and_pageviews(session: AsyncSession, customers: list[dict]) -> None:
    """Generate tracking sessions and page views."""
    session_count = 0
    pv_count = 0

    for cust in customers:
        num_sessions = random.randint(1, 8)
        visitor_id = uuid.uuid4()

        for _ in range(num_sessions):
            sess_id = uuid.uuid4()
            sess_uuid = uuid.uuid4()
            started = random_past(90)
            num_pages = random.randint(1, 10)

            await session.execute(
                text("""
                    INSERT INTO tracking_sessions
                        (id, visitor_id, customer_id, session_id, started_at,
                         ended_at, page_views, events_count, is_active,
                         device_type, browser, os, country, city,
                         utm_source, utm_medium, utm_campaign)
                    VALUES
                        (:id, :visitor_id, :customer_id, :session_id, :started_at,
                         :ended_at, :page_views, :events_count, :is_active,
                         :device_type, :browser, :os, :country, :city,
                         :utm_source, :utm_medium, :utm_campaign)
                """),
                {
                    "id": sess_id,
                    "visitor_id": visitor_id,
                    "customer_id": cust["id"],
                    "session_id": sess_uuid,
                    "started_at": started,
                    "ended_at": started + timedelta(minutes=random.randint(1, 120)),
                    "page_views": num_pages,
                    "events_count": random.randint(1, 20),
                    "is_active": False,
                    "device_type": random.choice(DEVICES),
                    "browser": random.choice(BROWSERS),
                    "os": random.choice(OPERATING_SYSTEMS),
                    "country": random.choice(COUNTRIES),
                    "city": random.choice(CITIES),
                    "utm_source": random.choice(UTM_SOURCES),
                    "utm_medium": random.choice(UTM_MEDIUMS),
                    "utm_campaign": random.choice(UTM_CAMPAIGNS),
                },
            )
            session_count += 1

            for j in range(num_pages):
                idx = random.randint(0, len(PAGE_URLS) - 1)
                await session.execute(
                    text("""
                        INSERT INTO page_views
                            (id, session_id, customer_id, page_url, page_title,
                             referrer, time_on_page, scroll_depth, timestamp)
                        VALUES
                            (:id, :session_id, :customer_id, :page_url, :page_title,
                             :referrer, :time_on_page, :scroll_depth, :timestamp)
                    """),
                    {
                        "id": uuid.uuid4(),
                        "session_id": sess_id,
                        "customer_id": cust["id"],
                        "page_url": PAGE_URLS[idx],
                        "page_title": PAGE_TITLES[idx],
                        "referrer": random.choice(["https://google.com", "https://facebook.com", None]),
                        "time_on_page": round(random.uniform(2.0, 300.0), 1),
                        "scroll_depth": round(random.uniform(0.0, 100.0), 1),
                        "timestamp": started + timedelta(seconds=j * random.randint(5, 60)),
                    },
                )
                pv_count += 1

    await session.commit()
    print(f"  Seeded {session_count} tracking sessions")
    print(f"  Seeded {pv_count} page views")


async def seed_campaigns(session: AsyncSession) -> None:
    """Create sample marketing campaigns."""
    for tmpl in CAMPAIGN_TEMPLATES:
        recipients = random.randint(500, 10000)
        sent = int(recipients * random.uniform(0.7, 1.0)) if tmpl["status"] != "draft" else 0
        opened = int(sent * random.uniform(0.15, 0.45))
        clicked = int(opened * random.uniform(0.1, 0.4))
        converted = int(clicked * random.uniform(0.05, 0.25))

        await session.execute(
            text("""
                INSERT INTO campaigns
                    (id, name, description, campaign_type, status,
                     subject_line, sender_name, sender_email,
                     total_recipients, sent_count, opened_count,
                     clicked_count, converted_count, revenue_generated)
                VALUES
                    (:id, :name, :description, :campaign_type, :status,
                     :subject_line, :sender_name, :sender_email,
                     :total_recipients, :sent_count, :opened_count,
                     :clicked_count, :converted_count, :revenue_generated)
            """),
            {
                "id": uuid.uuid4(),
                "name": tmpl["name"],
                "description": tmpl["description"],
                "campaign_type": tmpl["campaign_type"],
                "status": tmpl["status"],
                "subject_line": tmpl["subject_line"],
                "sender_name": "CDP Marketing",
                "sender_email": "marketing@cdp-platform.io",
                "total_recipients": recipients,
                "sent_count": sent,
                "opened_count": opened,
                "clicked_count": clicked,
                "converted_count": converted,
                "revenue_generated": round(converted * random.uniform(15.0, 120.0), 2),
            },
        )

    await session.commit()
    print(f"  Seeded {len(CAMPAIGN_TEMPLATES)} campaigns")


async def seed_ai_segments(session: AsyncSession) -> None:
    """Create AI-driven segment definitions."""
    for seg in AI_SEGMENT_DEFINITIONS:
        await session.execute(
            text("""
                INSERT INTO ai_segments
                    (id, name, description, criteria, model_type,
                     model_params, customer_count, last_computed)
                VALUES
                    (:id, :name, :description, :criteria::jsonb, :model_type,
                     :model_params::jsonb, :customer_count, :last_computed)
            """),
            {
                "id": uuid.uuid4(),
                "name": seg["name"],
                "description": seg["description"],
                "criteria": str(seg["criteria"]).replace("'", '"'),
                "model_type": seg["model_type"],
                "model_params": '{"n_clusters": 8, "random_state": 42}',
                "customer_count": seg["customer_count"],
                "last_computed": now - timedelta(hours=random.randint(1, 48)),
            },
        )

    await session.commit()
    print(f"  Seeded {len(AI_SEGMENT_DEFINITIONS)} AI segments")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("CDP Seed Data Script")
    print("=" * 60)
    print(f"Database: {DATABASE_URL}")
    print()

    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Import models to ensure metadata is populated
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from cdp.database import Base
    from cdp.models import (  # noqa: F401 - imported for side effects
        Customer, CustomerEvent, TrackingSession, PageView,
        Campaign, AISegment,
    )

    # Create tables if they don't exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables ensured.\n")

    async with session_factory() as session:
        print("[1/5] Seeding customers...")
        customers = await seed_customers(session)

        print("[2/5] Seeding customer events...")
        await seed_events(session, customers)

        print("[3/5] Seeding tracking sessions & page views...")
        await seed_sessions_and_pageviews(session, customers)

        print("[4/5] Seeding marketing campaigns...")
        await seed_campaigns(session)

        print("[5/5] Seeding AI segments...")
        await seed_ai_segments(session)

    await engine.dispose()

    print()
    print("=" * 60)
    print("Seed data complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
