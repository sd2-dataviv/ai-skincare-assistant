"""Seed the catalogue from clinic knowledge and create demo staff accounts (idempotent).

Usage: python -m app.seed [--demo-users]
"""
import asyncio
import os
import sys

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db import dispose_engine, sessionmaker
from app.knowledge import catalogue_seed
from app.models import CatalogueItem, User
from app.security import hash_password
from app.services.ot import seed_doctor_hours, seed_equipment, seed_rooms

DEMO_USERS = [
    ("admin@clinic.local", "Clinic Admin", "admin", None),
    ("doctor@clinic.local", "Dr. Kavya Rao", "doctor", "DMC/R/12345"),
    ("operator@clinic.local", "Front Desk Kiosk", "operator", None),
]


async def seed_catalogue(db) -> int:
    n = 0
    for item in catalogue_seed()["items"]:
        values = {
            "id": item["id"], "kind": item["kind"], "name": item["name"], "description": item.get("description", ""),
            "concerns": item.get("concerns", []), "skin_types": item.get("skin_types", []),
            "contraindications": item.get("contraindications", []), "ingredients": item.get("ingredients", []),
            "default_sessions": item.get("default_sessions", 1), "interval_days": item.get("interval_days", 0),
            "usage": item.get("usage", ""), "price_inr": item.get("price_inr", 0), "active": True,
            "room_type": item.get("room_type"), "duration_min": item.get("duration_min", 0),
            "equipment_type": item.get("equipment_type"), "needs_doctor": bool(item.get("needs_doctor", False)),
        }
        stmt = insert(CatalogueItem).values(**values).on_conflict_do_update(
            index_elements=["id"], set_={k: v for k, v in values.items() if k != "id"})
        await db.execute(stmt)
        n += 1
    return n


async def seed_users(db, password: str) -> list[str]:
    created = []
    for email, name, role, reg in DEMO_USERS:
        exists = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if exists:
            continue
        db.add(User(email=email, full_name=name, role=role, registration_no=reg, password_hash=hash_password(password)))
        created.append(email)
    return created


async def main(demo_users: bool) -> None:
    async with sessionmaker()() as db:
        n = await seed_catalogue(db)
        print(f"[seed] catalogue: {n} items upserted")
        rooms = await seed_rooms(db)
        print(f"[seed] treatment rooms: {rooms} added")
        eq = await seed_equipment(db)
        print(f"[seed] treatment equipment: {eq} added")
        admin_email, admin_pw = os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD")
        if admin_email and admin_pw:
            exists = (await db.execute(select(User).where(User.email == admin_email.lower()))).scalar_one_or_none()
            if not exists:
                db.add(User(email=admin_email.lower(), full_name="Administrator", role="admin",
                            password_hash=hash_password(admin_pw)))
                print(f"[seed] admin created: {admin_email}")
        if demo_users:
            pw = os.environ.get("DEMO_PASSWORD", "Demo@12345")
            created = await seed_users(db, pw)
            print(f"[seed] demo users created: {', '.join(created) or 'none (already exist)'}")
        await db.flush()
        hours = await seed_doctor_hours(db)
        print(f"[seed] working hours added for {hours} doctor(s) without any (Mon–Sat 10:00–19:00)")
        await db.commit()
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main("--demo-users" in sys.argv))
