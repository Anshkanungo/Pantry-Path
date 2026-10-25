"""Group cleaned recipes into dishes, then pick the candidate pool for the LLM.

run_group: recipe titles -> dish keys ("Mom's Easy Chicken Pot Pie (Copycat)" ->
"chicken pot pie"); one row per dish with its popularity, a cuisine hint from Food.com
keywords, and the share of its recipes that list each ingredient.

run_select: the most popular dishes per cuisine hint. The sizes (config) are larger than
the catalog quotas because the LLM drops non-dishes, merges duplicates and assigns the
final cuisine. It also records the cuisine mix of a plain top 100, the "before" picture
for the bias report (that mix is mostly American or untagged).
"""

import json
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from pantrypath_core.catalog import Cuisine, slugify
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError
from pantrypath_pipeline.preprocess.recipes import singularize_last

log = logging.getLogger(__name__)

UNKNOWN = "unknown"
HINTS = [c.value for c in Cuisine] + [UNKNOWN]

# Food.com keywords and categories that name a cuisine (lowercase). There is no Italian or
# French tag: those recipes carry the generic "European" at best.
CUISINE_KEYWORDS = {
    **dict.fromkeys(
        ["southwestern u.s.", "cajun", "creole", "hawaiian", "native american",
         "pennsylvania dutch", "canadian", "american", "southern"], "american"),
    **dict.fromkeys(
        ["greek", "german", "spanish", "scandinavian", "scottish", "russian", "polish",
         "swedish", "swiss", "portuguese", "hungarian", "danish", "dutch", "welsh", "norwegian",
         "austrian", "belgian", "czech", "finnish", "icelandic", "georgian", "italian",
         "french", "english", "british", "irish", "ukrainian"], "european"),
    **dict.fromkeys(
        ["mexican", "tex mex", "south american", "brazilian", "caribbean", "cuban",
         "puerto rican", "peruvian", "colombian", "chilean", "venezuelan", "ecuadorean",
         "costa rican", "guatemalan", "honduran", "argentine", "argentinian",
         "central american"], "latin_american"),
    **dict.fromkeys(
        ["chinese", "thai", "japanese", "korean", "vietnamese", "filipino", "indonesian",
         "malaysian", "cambodian", "cantonese", "szechuan", "hunan", "mongolian", "laotian",
         "burmese", "singaporean"], "east_se_asian"),
    **dict.fromkeys(
        ["indian", "pakistani", "nepalese", "sri lankan", "bangladeshi"], "south_asian"),
    **dict.fromkeys(
        ["southwest asia (middle east)", "moroccan", "lebanese", "turkish", "egyptian",
         "south african", "iraqi", "palestinian", "ethiopian", "nigerian", "sudanese",
         "somalian", "iranian", "persian", "israeli", "syrian", "middle eastern"],
        "middle_eastern_african"),
}
# Broad tags count only when no specific one is present ("Asian" + "Indian" -> south_asian)
GENERIC_KEYWORDS = {
    "asian": "east_se_asian", "european": "european", "african": "middle_eastern_african",
}

# Recipes that aren't dishes to shop for: drinks, sauces, dressings and non-food
NON_DISH_CATEGORIES = frozenset({
    "beverages", "smoothies", "shakes", "punch beverage", "sauces", "salad dressings",
    "chutneys", "jellies", "stocks", "bath/beauty", "homeopathy/remedies",
    "household cleaner",
})
NON_DISH_KEYWORDS = frozenset({
    "beverages", "smoothies", "shakes", "punch beverage", "household cleaner", "bath/beauty",
    "homeopathy/remedies", "halloween cocktail",
})
# Dish keys that are components or basics, not dishes (matched at the end of the key)
NON_DISH_ENDINGS = re.compile(
    r"\b(?:substitute|seasoning|seasoning mix|spice mix|spice blend|spice rub|dry rub|rub|"
    r"marinade|frosting|icing|glaze|syrup|vinaigrette|brine|stock|ice cube|ice|dough|crust)$"
)
NON_DISH_PHRASES = re.compile(
    r"\b(?:dog treat|dog biscuit|dog food|cat treat|play dough|playdough|play doh|bird seed|"
    r"bubble solution|potpourri|cleaner|detergent)s?\b"
)

# Title clean-up: rewrites first, then fillers that say nothing about the dish
REWRITES = [
    (re.compile(r"\bcrock ?pot (?=roast\b)"), "pot "),  # "crock pot roast" is pot roast
    (re.compile(r"\bslow cooker (?=pot roast\b)"), ""),
    (re.compile(r"\bmac(?:aroni)? (?:and|n|'n'|'n|n') cheese\b"), "macaroni and cheese"),
    (re.compile(r"\bbarbe?que\b|\bbbq\b"), "barbecue"),
    (re.compile(r"\bw/\s*"), "with "),
    (re.compile(r"&"), " and "),
]
FILLERS = sorted([
    "world's best", "worlds best", "world famous", "best ever", "the best", "very best",
    "best", "easiest", "super easy", "quick and easy", "easy", "quick", "simple", "simply",
    "homemade", "home made", "copycat", "copy cat", "knock off", "knockoff", "the", "my",
    "our", "your", "ultimate", "delicious", "deliciously", "yummy", "yum", "perfect",
    "famous", "favorite", "favourite", "awesome", "amazing", "fabulous", "terrific",
    "wonderful", "incredible", "fantastic", "excellent", "tasty", "absolutely", "really",
    "super", "very", "melt in your mouth", "to die for", "kid friendly", "family favorite",
    "restaurant style", "classic", "traditional", "authentic", "old fashioned", "basic",
    "original", "crock pot", "crockpot", "slow cooker", "instant pot", "pressure cooker",
    "microwave", "air fryer", "bread machine", "one pot",
    # restaurant names in copycat titles ("olive garden zuppa toscana")
    "olive garden", "red lobster", "cracker barrel", "cheesecake factory", "outback steakhouse",
    "outback", "taco bell", "kfc", "starbucks", "panera bread", "panera", "chick fil a",
    "boston market", "texas roadhouse", "dairy queen", "mcdonalds", "mcdonald's",
    "white castle", "bob evans", "pf changs", "pf chang's", "p f chang's", "applebees",
    "applebee's", "chilis", "chili's", "tgi fridays", "tgi friday's",
], key=len, reverse=True)  # longest first, so "the best" goes before "the"
FILLER_RE = re.compile(r"\b(?:" + "|".join(re.escape(f) for f in FILLERS) + r")\b")
NUMBERS = re.compile(r"\b\d+(?:[./]\d+)?(?:\s*(?:minutes?|mins?|hours?|ingredients?|step))?\b")
TRAILING_NOISE = re.compile(r"(?:\s+(?:recipe|version|ii|iii|iv|vi|vii|viii|ix|x|#))+$")
# Possessives that are part of a dish's name, not an author's ("shepherd's pie")
KEEP_POSSESSIVES = frozenset({
    "shepherd's", "shepherds'", "chef's", "devil's", "baker's", "farmer's", "farmers'",
    "hunter's", "fisherman's", "ploughman's", "plowman's", "gardener's", "angel's",
    "valentine's", "patrick's", "year's", "mother's", "father's", "president's",
})


CUISINE_WORDS = frozenset(w for w in [*CUISINE_KEYWORDS, *GENERIC_KEYWORDS] if " " not in w)

# One row per candidate dish (dish_candidates.parquet)
CANDIDATE_SCHEMA = pa.schema([
    ("dish_id", pa.string()),
    ("name", pa.string()),
    ("recipe_count", pa.int64()),
    ("review_count", pa.int64()),
    ("cuisine_hint", pa.string()),
    ("hint_share", pa.float64()),
    ("sample_titles", pa.list_(pa.string())),
    ("ingredients", pa.list_(pa.struct([("name", pa.string()), ("share", pa.float64())]))),
])


def _fold_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def _strip_author(tokens: list[str]) -> list[str]:
    """Drop a leading "Mom's" / "Aunt Bee's" / "Paula Deen's" (first 3 words) if more follows.
    Cuisine words before it stay: "japanese mum's chicken" -> "japanese chicken"."""
    for i, token in enumerate(tokens[:3]):
        if token.endswith(("'s", "s'")) and token not in KEEP_POSSESSIVES:
            if len(tokens) == i + 1:
                return tokens
            return [t for t in tokens[:i] if t in CUISINE_WORDS] + tokens[i + 1:]
    return tokens


def display_name(title: str | None) -> str | None:
    """A clean title without filler: "kittencal's famous chicken pot pie (copycat)" ->
    "chicken pot pie". Returns None when nothing is left."""
    if not isinstance(title, str) or not title:  # pandas turns None into NaN
        return None
    text = re.sub(r"\([^)]*\)?|\[[^\]]*\]?", " ", title.lower())
    text = text.replace('"', " ").replace("-", " ")
    for pattern, replacement in REWRITES:
        text = pattern.sub(replacement, text)
    text = re.sub(r"[^\w' ]+", " ", text)
    text = " ".join(_strip_author(text.split()))
    text = FILLER_RE.sub(" ", text)
    text = NUMBERS.sub(" ", text)
    text = " ".join(text.split()).strip(" '")
    text = TRAILING_NOISE.sub("", text).strip(" '")
    return text or None


def dish_key(display: str | None) -> str | None:
    """Grouping key: ASCII letters and digits only, head noun singular
    ("shepherd's pies" and "shepherds pie" -> "shepherds pie")."""
    if not isinstance(display, str) or not display:
        return None
    text = _fold_accents(display).replace("'", "")
    text = " ".join(re.sub(r"[^a-z0-9 ]+", " ", text).split())
    return singularize_last(text) or None


def recipe_cuisine(keywords: Iterable[str] | None, category: str | None) -> str | None:
    """Cuisine bucket named by a recipe's keywords or category, or None."""
    words = [k.lower() for k in (keywords if keywords is not None else [])]
    if isinstance(category, str) and category:
        words.append(category.lower())
    specific = [CUISINE_KEYWORDS[w] for w in words if w in CUISINE_KEYWORDS]
    if specific:
        return Counter(specific).most_common(1)[0][0]
    generic = [GENERIC_KEYWORDS[w] for w in words if w in GENERIC_KEYWORDS]
    return Counter(generic).most_common(1)[0][0] if generic else None


def cuisine_hint(recipe_hints: Sequence[str | None], min_share: float) -> tuple[str, float]:
    """Majority cuisine across a dish's recipes; 'unknown' unless it covers min_share of them."""
    if not recipe_hints:
        return UNKNOWN, 0.0
    counts = Counter(h for h in recipe_hints if isinstance(h, str) and h)
    if not counts:
        return UNKNOWN, 0.0
    best, n = min(counts.items(), key=lambda kv: (-kv[1], HINTS.index(kv[0])))
    share = n / len(recipe_hints)
    return (best if share >= min_share else UNKNOWN), round(share, 3)


def is_non_dish(category: str | None, keywords: Iterable[str] | None) -> bool:
    """Drinks, sauces, dressings, stocks and non-food recipes, judged by Food.com tags."""
    if isinstance(category, str) and category.lower() in NON_DISH_CATEGORIES:
        return True
    return any(k.lower() in NON_DISH_KEYWORDS for k in (keywords if keywords is not None else []))


def is_non_dish_key(key: str) -> bool:
    """Basics and components by name: 'taco seasoning', 'pie crust', 'dog treats'.
    Only the head counts: 'carrot cake with cream cheese frosting' is a dish."""
    head = key.split(" with ")[0]
    return bool(NON_DISH_ENDINGS.search(head) or NON_DISH_PHRASES.search(key))


def ingredient_shares(ingredient_lists: Sequence[Iterable[str]], top_n: int) -> list[dict]:
    """Share of recipes that list each ingredient, most common first (ties by name)."""
    if not ingredient_lists:
        return []
    counts = Counter(name for names in ingredient_lists for name in set(names))
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return [{"name": name, "share": round(c / len(ingredient_lists), 3)} for name, c in ranked]


def _most_common_in_order(values: Sequence[str]) -> str:
    """Most frequent value; ties go to the one seen first (input is sorted by reviews)."""
    counts = Counter(values)
    best = max(counts.values())
    return next(v for v in values if counts[v] == best)


def group_dishes(
    recipes: pd.DataFrame, min_recipes: int, top_n: int, hint_min_share: float
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Clean recipes -> one row per dish with >= min_recipes recipes, most popular first."""
    stats: dict[str, int] = {"recipes_in": len(recipes)}
    non_dish = [is_non_dish(c, k) for c, k in zip(recipes["category"], recipes["keywords"],
                                                    strict=True)]
    df = recipes.loc[[not x for x in non_dish]].copy()
    stats["non_dish_recipes"] = len(recipes) - len(df)

    df["display"] = [display_name(t) for t in df["title"]]
    df["key"] = [dish_key(d) for d in df["display"]]
    no_key = df["key"].isna()
    stats["no_dish_name"] = int(no_key.sum())
    df = df[~no_key]
    basics = df["key"].map(is_non_dish_key)
    stats["basic_or_component_recipes"] = int(basics.sum())
    df = df[~basics]

    sizes = df.groupby("key").size()
    stats["dish_groups"] = len(sizes)
    keep = sizes[sizes >= min_recipes].index
    df = df[df["key"].isin(keep)]
    df["hint"] = [recipe_cuisine(k, c) for k, c in zip(df["keywords"], df["category"],
                                                        strict=True)]
    df = df.sort_values(["review_count", "recipe_id"], ascending=[False, True])

    rows = []
    for key, g in df.groupby("key", sort=False):
        hint, hint_share = cuisine_hint(list(g["hint"]), hint_min_share)
        titles = list(dict.fromkeys(g["title"]))[:5]
        rows.append({
            "dish_id": slugify(key),
            "name": _most_common_in_order(list(g["display"])),
            "recipe_count": len(g),
            "review_count": int(g["review_count"].sum()),
            "cuisine_hint": hint,
            "hint_share": hint_share,
            "sample_titles": titles,
            "ingredients": ingredient_shares(list(g["ingredients"]), top_n),
        })
    stats["dishes"] = len(rows)
    out = pd.DataFrame(rows, columns=list(CANDIDATE_SCHEMA.names))
    return popularity_order(out), stats


def popularity_order(candidates: pd.DataFrame) -> pd.DataFrame:
    """Most reviews first; ties by recipe count, then dish_id (stable, deterministic)."""
    return candidates.sort_values(
        ["review_count", "recipe_count", "dish_id"], ascending=[False, False, True]
    ).reset_index(drop=True)


def hint_mix(hints: Iterable[str]) -> dict[str, int]:
    counts = Counter(hints)
    return {h: counts.get(h, 0) for h in HINTS}


def select_pool(
    candidates: pd.DataFrame, pool_size: dict[str, int], always_include: Iterable[str] = ()
) -> pd.DataFrame:
    """Top pool_size[hint] dishes per cuisine hint, by popularity, plus the always_include ids.

    always_include holds dishes the team relies on (the golden set: quiche lorraine, pad thai,
    ...) that are too niche on Food.com to make the popularity cut. They are flagged so the
    catalog can keep them within their cuisine's quota.
    """
    ordered = popularity_order(candidates)
    parts = []
    for hint in HINTS:
        n = int(pool_size.get(hint, 0))
        part = ordered[ordered["cuisine_hint"] == hint].head(n)
        if len(part) < n:
            log.warning("only %d candidate dishes with cuisine hint %s (wanted %d)",
                        len(part), hint, n)
        parts.append(part)
    pool = pd.concat(parts, ignore_index=True)

    wanted = list(dict.fromkeys(always_include))
    missing = [d for d in wanted if d not in set(ordered["dish_id"])]
    if missing:
        log.warning("always_include dishes not among the candidates: %s", missing)
    extra = ordered[ordered["dish_id"].isin(wanted) & ~ordered["dish_id"].isin(pool["dish_id"])]
    pool = pd.concat([pool, extra], ignore_index=True)
    pool["always_include"] = pool["dish_id"].isin(wanted)
    return pool


def _pool_entry(row: Any) -> dict:
    return {
        "dish_id": row.dish_id,
        "name": row.name,
        "cuisine_hint": row.cuisine_hint,
        "recipe_count": int(row.recipe_count),
        "review_count": int(row.review_count),
        "always_include": bool(getattr(row, "always_include", False)),
        "sample_titles": [str(t) for t in row.sample_titles],
        "ingredients": [
            {"name": str(i["name"]), "share": float(i["share"])} for i in row.ingredients
        ],
    }


def run_group(cfg: Config, **_: Any) -> dict:
    fc = cfg["foodcom"]
    source = cfg.path(fc["clean_output"])
    if not source.exists():
        raise DataUnavailableError(f"{source} not found; run the clean-recipes step first")
    try:
        recipes = pd.read_parquet(source)
    except Exception as exc:
        raise DataCorruptError(f"Could not read {source} ({exc}); rerun clean-recipes") from exc

    candidates, stats = group_dishes(
        recipes,
        min_recipes=int(fc["min_recipes_per_dish"]),
        top_n=int(fc["top_ingredients_per_dish"]),
        hint_min_share=float(fc.get("hint_min_share", 0.2)),
    )
    if candidates.empty:
        raise DataCorruptError(
            f"No dish has {fc['min_recipes_per_dish']}+ recipes; check {source} and the config"
        )
    target = cfg.path(fc["candidates_output"])
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(candidates, schema=CANDIDATE_SCHEMA,
                                        preserve_index=False), target)
    for name, value in stats.items():
        log.info("group-dishes %s: %d", name, value)
    return {**stats, "hint_mix": hint_mix(candidates["cuisine_hint"])}


def run_select(cfg: Config, **_: Any) -> dict:
    fc = cfg["foodcom"]
    source = cfg.path(fc["candidates_output"])
    if not source.exists():
        raise DataUnavailableError(f"{source} not found; run the group-dishes step first")
    try:
        candidates = pd.read_parquet(source)
    except Exception as exc:
        raise DataCorruptError(f"Could not read {source} ({exc}); rerun group-dishes") from exc

    pool = select_pool(candidates, fc["pool_size"], fc.get("always_include") or [])
    plain_top = hint_mix(popularity_order(candidates)["cuisine_hint"].head(100))
    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "foodcom",
        "plain_top100_cuisine_hints": plain_top,
        "dishes": [_pool_entry(row) for row in pool.itertuples(index=False)],
    }
    target = cfg.path(fc["pool_output"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    pool_mix = hint_mix(pool["cuisine_hint"])
    log.info("dish pool: %d dishes %s; plain top 100: %s", len(pool), pool_mix, plain_top)
    return {"pool": len(pool), "pool_hints": pool_mix, "plain_top100": plain_top}
