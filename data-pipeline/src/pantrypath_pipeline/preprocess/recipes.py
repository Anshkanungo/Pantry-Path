"""Clean Food.com recipes: tidy titles and ingredient names, drop unusable rows.

Food.com's ingredient "parts" are already short names ("garlic cloves", "lemon, zest of"),
but they carry HTML entities, brand symbols, sizes ("8-inch flour tortillas"), prep words
("chopped tomatoes") and singular/plural twins ("egg" / "eggs"). Cleaning them makes the
per-dish ingredient shares meaningful. Quantities are dropped on purpose: they have no units.

Output, one row per recipe: recipe_id, title, ingredients (cleaned names), keywords,
category, review_count, rating.
"""

import html
import logging
import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from pantrypath_pipeline.acquire.foodcom import raw_path
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError

log = logging.getLogger(__name__)

SOURCE_COLUMNS = [
    "RecipeId", "Name", "RecipeIngredientParts", "Keywords", "RecipeCategory",
    "ReviewCount", "AggregatedRating",
]

OUTPUT_SCHEMA = pa.schema([
    ("recipe_id", pa.int64()),
    ("title", pa.string()),
    ("ingredients", pa.list_(pa.string())),
    ("keywords", pa.list_(pa.string())),
    ("category", pa.string()),
    ("review_count", pa.int64()),
    ("rating", pa.float64()),
])

# Unicode vulgar fractions -> ASCII, so "½ cup" is handled like "1/2 cup"
FRACTIONS = {
    "¼": "1/4", "½": "1/2", "¾": "3/4", "⅐": "1/7", "⅑": "1/9", "⅒": "1/10", "⅓": "1/3",
    "⅔": "2/3", "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5", "⅙": "1/6", "⅚": "5/6",
    "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8", "⁄": "/",
}
QUOTES = {"’": "'", "‘": "'", "`": "'", "“": '"', "”": '"'}
SYMBOLS = re.compile(r"[®™©]")

UNITS = (
    r"cups?|c\.|tablespoons?|tbsps?|tbs|teaspoons?|tsps?|ounces?|oz|pounds?|lbs?|grams?|g|"
    r"kilograms?|kg|milliliters?|ml|liters?|litres?|l|pints?|quarts?|qts?|gallons?|pinch(?:es)?|"
    r"dash(?:es)?|cans?|packages?|pkgs?|jars?|envelopes?|packets?|containers?|cartons?|bottles?"
)
QUANTITY = r"\d+(?:[.,/]\d+)?(?:\s*-\s*\d+(?:[./]\d+)?)?(?:\s+\d+/\d+)?"
# A number must stand alone ("7-up" is a product, not 7 of "up")
LEADING_AMOUNT = re.compile(rf"^\s*{QUANTITY}(?=\s|$)\s*(?:(?:{UNITS})\b\.?\s*(?:of\s+)?)?")
SIZE = re.compile(
    r"\b\d+(?:\.\d+)?\s*-?\s*(?:inch(?:es)?|in\.|oz|ounces?|lbs?|pounds?|qt|quarts?)\b"
)
PERCENT = re.compile(r"\b\d+(?:\.\d+)?\s*%\s*(?:fat\b)?")
PARENS = re.compile(r"\([^)]*\)?|\[[^\]]*\]?")
# "lemon, juice of" -> "lemon juice"; "limes, juice and zest of" -> "lime" (buy the fruit)
FRUIT_PART = re.compile(r"^(?P<fruit>[a-z ]+?), (?P<part>juice|zest|rind|seeds|pulp) of$")
FRUIT_WHOLE = re.compile(r"^(?P<fruit>[a-z ]+?), \w+ and \w+ of$")

# Home preparation, not something you buy differently. Kept on purpose: diced, crushed,
# shredded, grated, ground, frozen, dried, smoked (they name real products).
PREP_WORDS = re.compile(
    r"\b(?:chopped|minced|sliced|finely|coarsely|roughly|thinly|freshly|fresh|melted|softened|"
    r"beaten|lightly|cubed|peeled|halved|quartered|divided|optional|julienned|trimmed|rinsed|"
    r"drained|seeded|deveined|pitted|cored|cooked|heaping|level|packed|large|small|medium|"
    r"extra[- ]large|jumbo|at room temperature|room temperature|to taste|for garnish|"
    r"for serving|plus more|or more|as needed|if desired)\b"
)
EDGE_PUNCTUATION = " \t\n-–—_:;,.!?*#~\"'/&+"

# Not grocery items: nobody buys them, and they would swamp the ingredient shares
NOT_GROCERIES = frozenset({
    "water", "cold water", "warm water", "hot water", "boiling water", "ice water",
    "tap water", "lukewarm water", "very hot water", "ice", "ice cube", "crushed ice",
})

# Words left alone by singularize(): already singular, or plural in product names
UNCOUNTABLE = frozenset({
    "molasses", "hummus", "couscous", "asparagus", "swiss", "grits", "oats", "greens",
    "flakes", "crumbs", "chips", "sprinkles", "bitters", "schnapps", "brussels", "lees",
    "species", "series", "news", "chives", "hops", "herbes", "fries", "nachos", "s'mores",
})
IRREGULAR = {
    "leaves": "leaf", "halves": "half", "loaves": "loaf", "cookies": "cookie",
    "brownies": "brownie", "smoothies": "smoothie", "pies": "pie", "veggies": "veggie",
    "pierogies": "pierogi", "quiches": "quiche", "brioches": "brioche", "ganaches": "ganache",
    "calories": "calorie", "goodies": "goodie", "knives": "knife", "wolves": "wolf",
}


def is_missing(value: Any) -> bool:
    """None, NaN or pd.NA (scalars only)."""
    return value is None or (not isinstance(value, str) and bool(pd.isna(value)))


def fix_text(value: Any) -> str:
    """Unescape HTML (also double-escaped '&amp;quot;'), drop brand symbols, unify quotes."""
    if is_missing(value):
        return ""
    text = str(value)
    for _ in range(3):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = unicodedata.normalize("NFC", text)
    for old, new in {**FRACTIONS, **QUOTES}.items():
        text = text.replace(old, new)
    text = SYMBOLS.sub("", text)
    return " ".join(text.split())


def singularize(word: str) -> str:
    """Rough English singular for the head noun of a name ('tomatoes' -> 'tomato')."""
    if word in IRREGULAR:
        return IRREGULAR[word]
    if len(word) <= 3 or word in UNCOUNTABLE or word.endswith(("ss", "us", "is", "'s")):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("oes"):  # tomatoes -> tomato, but joes -> joe, shoes -> shoe
        return word[:-2] if len(word) > 5 else word[:-1]
    if word.endswith(("ches", "shes", "xes", "zes", "sses")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def singularize_last(name: str) -> str:
    words = name.split()
    if not words:
        return name
    words[-1] = singularize(words[-1])
    return " ".join(words)


def clean_title(value: Any) -> str | None:
    """Readable lowercase title: '&quot;Best&quot; Brownies!' -> 'best" brownies'."""
    text = fix_text(value).lower().strip(EDGE_PUNCTUATION)
    return text or None


@lru_cache(maxsize=50_000)
def clean_ingredient(value: str | None) -> str | None:
    """One Food.com ingredient part -> a clean, singular name; None if nothing buyable is left.

    '8-inch flour tortillas' -> 'flour tortilla', 'Lea &amp; Perrins Worcestershire Sauce'
    -> 'lea & perrins worcestershire sauce', 'lemons, zest of' -> 'lemon zest'.
    """
    text = fix_text(value).lower()
    if not text:
        return None
    text = PARENS.sub(" ", text)
    text = " ".join(text.split())

    whole = FRUIT_WHOLE.match(text)
    part = FRUIT_PART.match(text) if not whole else None
    if whole:
        text = singularize_last(whole["fruit"])
    elif part:
        piece = "zest" if part["part"] == "rind" else part["part"]
        text = f"{singularize_last(part['fruit'])} {piece}"

    # Sizes and percentages first, so "8-inch" isn't half-eaten as a leading quantity
    text = SIZE.sub(" ", text)
    text = PERCENT.sub(" ", text)
    text = LEADING_AMOUNT.sub("", text.strip())
    text = PREP_WORDS.sub(" ", text)
    text = text.replace(",", " ")
    text = " ".join(text.split()).strip(EDGE_PUNCTUATION)
    if len(text) < 2 or not re.search(r"[a-z]", text):
        return None
    text = singularize_last(text)
    return None if text in NOT_GROCERIES else text


def clean_ingredients(values: Iterable[Any] | None) -> list[str]:
    """Clean a recipe's ingredient list: drop empties and non-groceries, keep order, dedupe."""
    if values is None or isinstance(values, float):
        return []
    out: list[str] = []
    for value in values:
        name = clean_ingredient(value if isinstance(value, str) else None)
        if name and name not in out:
            out.append(name)
    return out


def clean_keywords(values: Iterable[Any] | None) -> list[str]:
    """Lowercased, unescaped, deduped Food.com keywords."""
    if values is None or isinstance(values, float):
        return []
    out: list[str] = []
    for value in values:
        word = fix_text(value).lower() if isinstance(value, str) else ""
        if word and word not in out:
            out.append(word)
    return out


def clean_frame(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Clean raw Food.com rows; return (clean rows, dropped count per reason)."""
    dropped: dict[str, int] = {}
    df = raw.drop_duplicates(subset="RecipeId", keep="first")
    dropped["duplicate_id"] = len(raw) - len(df)
    df = df[df["RecipeId"].notna()]

    out = pd.DataFrame({
        "recipe_id": df["RecipeId"].astype("int64").to_numpy(),
        "title": [clean_title(v) for v in df["Name"]],
        "ingredients": [clean_ingredients(v) for v in df["RecipeIngredientParts"]],
        "keywords": [clean_keywords(v) for v in df["Keywords"]],
        "category": [fix_text(v) or None for v in df["RecipeCategory"]],
        "review_count": pd.to_numeric(df["ReviewCount"], errors="coerce")
        .fillna(0).astype("int64").to_numpy(),
        "rating": pd.to_numeric(df["AggregatedRating"], errors="coerce").to_numpy(),
    })
    no_title = out["title"].isna()
    no_ingredients = ~no_title & (out["ingredients"].map(len) == 0)
    dropped["no_title"] = int(no_title.sum())
    dropped["no_ingredients"] = int(no_ingredients.sum())
    return out[~(no_title | no_ingredients)].reset_index(drop=True), dropped


def write_parquet(df: pd.DataFrame, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, schema=OUTPUT_SCHEMA, preserve_index=False)
    pq.write_table(table, path)


def run(cfg: Config, **_: Any) -> dict:
    fc = cfg["foodcom"]
    source = raw_path(cfg)
    if not source.exists():
        raise DataUnavailableError(f"{source} not found; run the download-foodcom step first")
    try:
        raw = pd.read_parquet(source, columns=SOURCE_COLUMNS)
    except Exception as exc:
        raise DataCorruptError(f"Could not read {source} ({exc}); rerun download-foodcom") from exc

    clean, dropped = clean_frame(raw)
    for reason, count in dropped.items():
        log.info("dropped %d recipes: %s", count, reason)
    if clean.empty:
        raise DataCorruptError(f"No usable recipes left after cleaning {source}")

    target = cfg.path(fc["clean_output"])
    write_parquet(clean, target)
    distinct = clean["ingredients"].explode().nunique()
    log.info("wrote %d clean recipes (%d distinct ingredient names) to %s",
             len(clean), distinct, target)
    return {"recipes_in": len(raw), "recipes_out": len(clean), "dropped": dropped,
            "distinct_ingredients": int(distinct)}
