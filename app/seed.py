"""Sample catalog. Seeded on first start; `uv run python -m app.seed --reset` starts over."""

import sys
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import create_tables, make_engine
from .models import Base, Product

# (title, price, stock, category, description)
PRODUCTS = [
    (
        "Wireless Earbuds", "49.99", 25, "Electronics",
        "Bluetooth in-ear headphones with a charging case and 24-hour battery life.",
    ),
    (
        "Coffee Beans", "18.00", 40, "Grocery",
        "One kilogram of medium-roast whole-bean Arabica.",
    ),
    (
        "Leather Backpack", "85.00", 12, "Bags",
        "Full-grain leather bag that fits a 15-inch laptop. Brown or black.",
    ),
    (
        "Smart Watch", "129.00", 8, "Electronics",
        "Fitness watch with heart-rate tracking, GPS and a week of battery.",
    ),
    (
        "Ceramic Mug Set", "24.50", 30, "Home",
        "Set of four dishwasher-safe stoneware cups.",
    ),
    (
        "Cotton T-Shirt", "15.99", 60, "Clothing",
        "Organic cotton tee in black, white or navy. Sizes S to XL.",
    ),
    # Out of stock and low stock on purpose, so both paths can be demoed.
    (
        "Desk Lamp", "34.00", 0, "Home",
        "Dimmable LED light with a USB charging port.",
    ),
    (
        "Yoga Mat", "29.95", 3, "Sports",
        "Non-slip six-millimetre exercise mat with a carry strap.",
    ),
]


def seed_if_empty(db: Session) -> int:
    if db.scalar(select(func.count()).select_from(Product)):
        return 0
    db.add_all(
        Product(title=t, price=Decimal(p), stock_quantity=s, category=c, description=d)
        for t, p, s, c, d in PRODUCTS
    )
    db.commit()
    return len(PRODUCTS)


def main() -> None:
    engine = make_engine(get_settings().database_url)
    if "--reset" in sys.argv[1:]:
        Base.metadata.drop_all(engine)
    create_tables(engine)
    with Session(engine) as db:
        added = seed_if_empty(db)
    print(f"Added {added} products." if added else "Catalog already has products; nothing added.")


if __name__ == "__main__":
    main()
