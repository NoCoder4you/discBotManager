from app.database import SessionLocal
from app.services.catalog import seed_catalog

if __name__ == "__main__":
    with SessionLocal() as db:
        seed_catalog(db)
    print("Canonical permissions and bot roles seeded.")
