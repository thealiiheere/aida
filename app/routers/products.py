import re
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ..deps import Db, api_error
from ..models import Product
from ..schemas import ErrorOut, ProductOut, SearchOut, StockOut

router = APIRouter(prefix="/api/products", tags=["Catalog"])

# Callers say "do you have any earbuds?", and the agent may pass that straight through.
STOPWORDS = set(
    "a an and any are can do for got have i in is it me my of on or please show some "
    "the there to want what with you your".split()
)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold())


def _terms(query: str) -> set[str]:
    terms = set()
    for word in _words(query):
        if word not in STOPWORDS:
            plural = len(word) > 3 and word.endswith("s") and not word.endswith("ss")
            terms.add(word[:-1] if plural else word)
    return terms


def _matches(terms: set[str], text: str) -> set[str]:
    words = _words(text)
    return {term for term in terms if any(word.startswith(term) for word in words)}


def _rank(products: list[Product], terms: set[str]) -> list[Product]:
    """Products matching every term, best first; if none do, those matching some."""
    scored = []
    for product in products:
        title = _matches(terms, product.title)
        category = _matches(terms, product.category)
        description = _matches(terms, product.description)
        found = title | category | description
        if found:
            score = 3 * len(title) + 2 * len(category) + len(description)
            scored.append((len(found), score, product))
    if any(found == len(terms) for found, _, _ in scored):
        scored = [row for row in scored if row[0] == len(terms)]
    scored.sort(key=lambda row: (-row[0], -row[1], row[2].stock_quantity == 0, row[2].id))
    return [product for _, _, product in scored]


@router.get("/search")
def search_products(
    db: Db,
    q: Annotated[str, Query(max_length=200, description="Blank lists the catalog.")] = "",
    category: Annotated[str | None, Query(max_length=60)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> SearchOut:
    """Search titles, categories and descriptions. Tool: `search_catalog`."""
    # Ranking runs in Python: SQLite's LIKE only ignores case for ASCII, and a demo
    # catalog is tiny. Move to SQLite FTS5 past a few thousand products.
    products = list(db.scalars(select(Product).order_by(Product.id)))
    if category and category.strip():
        wanted = category.strip().casefold()
        products = [p for p in products if p.category.casefold() == wanted]
    terms = _terms(q)
    if terms:
        products = _rank(products, terms)
    else:
        products.sort(key=lambda p: p.stock_quantity == 0)
    products = products[:limit]
    return SearchOut(
        query=q, count=len(products), products=[ProductOut.model_validate(p) for p in products]
    )


@router.get("/{product_id}/stock", responses={404: {"model": ErrorOut}})
def check_stock(
    product_id: int, db: Db, variant: Annotated[str | None, Query(max_length=60)] = None
) -> StockOut:
    """Current stock for one product. Tool: `check_stock`."""
    product = db.get(Product, product_id)
    if product is None:
        raise api_error(404, "product_not_found", f"There is no product {product_id}.")
    variant = (variant or "").strip() or None
    return StockOut(
        product_id=product.id,
        title=product.title,
        price=product.price,
        stock_quantity=product.stock_quantity,
        in_stock=product.stock_quantity > 0,
        variant=variant,
        note="Stock is counted per product, not per size or color. Don't promise this variant."
        if variant
        else None,
    )
