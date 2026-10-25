"""Diet rules: is a grocery product safe for a user's allergies and diet?

One module, used by the data pipeline and the backend, so both give the same answer.

Evidence comes from one source at a time (the store's own data, else the Open Food
Facts snapshot). The rules read declared allergens ("contains"), traces ("may
contain"), the ingredient statement, and the product name and categories.

Golden rule: no evidence means "unknown", never "safe". Declared allergens alone
can prove a product unsafe, but calling it safe needs an ingredient statement.

Words are matched as whole words, singular or plural ("egg" also matches "Eggs"),
after removing false friends for that allergen ("peanut butter" is not milk) and
skipping negated mentions ("dairy-free", "no milk", "does not contain peanuts").
The word lists were checked against Open Food Facts' vegan, vegetarian and
gluten-free labels on the US snapshot (see the bias report for current accuracy).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field


class Allergen(str, Enum):
    """The 9 major US allergens (FALCPA + FASTER Act) plus gluten."""

    milk = "milk"
    egg = "egg"
    fish = "fish"
    shellfish = "shellfish"
    tree_nuts = "tree_nuts"
    peanuts = "peanuts"
    wheat = "wheat"
    soy = "soy"
    sesame = "sesame"
    gluten = "gluten"


class Diet(str, Enum):
    vegan = "vegan"
    vegetarian = "vegetarian"
    none = "none"


class DietClass(str, Enum):
    """What a product is (as opposed to Diet, what a user eats)."""

    vegan = "vegan"
    vegetarian = "vegetarian"
    non_vegetarian = "non_vegetarian"
    unknown = "unknown"


class VerdictKind(str, Enum):
    safe = "safe"
    unsafe = "unsafe"
    unknown = "unknown"


EvidenceSource = Literal["kroger", "target", "off", "none"]


class ProductEvidence(BaseModel):
    """What one source says about a product's contents."""

    source: Literal["kroger", "target", "off"]
    name: str = ""
    brand: str = ""
    categories: list[str] = Field(default_factory=list)
    ingredients_text: str = ""
    allergens: list[str] = Field(
        default_factory=list,
        description="Declared 'contains' allergens: tags ('en:milk') or words ('Tree Nuts')",
    )
    traces: list[str] = Field(
        default_factory=list, description="Declared 'may contain' (cross-contact) allergens"
    )


class Verdict(BaseModel):
    verdict: VerdictKind
    reason: str
    evidence_source: EvidenceSource


# How allergens read in reasons ("Contains tree nuts")
LABELS = {
    Allergen.milk: "milk",
    Allergen.egg: "eggs",
    Allergen.fish: "fish",
    Allergen.shellfish: "shellfish",
    Allergen.tree_nuts: "tree nuts",
    Allergen.peanuts: "peanuts",
    Allergen.wheat: "wheat",
    Allergen.soy: "soy",
    Allergen.sesame: "sesame",
    Allergen.gluten: "gluten",
}

# --- Vocabulary -------------------------------------------------------------------------
# Each list covers ingredient-statement words, OFF tags ("en:sesame-seeds" -> "sesame seeds")
# and store words ("Crustacean Shellfish"). Plurals are matched automatically.

ALLERGEN_WORDS: dict[Allergen, tuple[str, ...]] = {
    Allergen.milk: (
        "milk", "milkfat", "milk fat", "buttermilk", "butter", "butterfat", "butteroil", "cream",
        "cheese", "whey", "casein", "caseinate", "lactose", "lactalbumin", "lactoglobulin",
        "yogurt", "yoghurt", "ghee", "kefir", "curd", "paneer", "ricotta", "mozzarella",
        "parmesan", "cheddar", "mascarpone", "dairy",
    ),
    Allergen.egg: (
        "egg", "egg white", "egg yolk", "yolk", "albumen", "albumin", "ovalbumin", "ovomucoid",
        "lysozyme", "meringue", "eggnog",
    ),
    Allergen.fish: (
        "fish", "anchovy", "anchovies", "tuna", "salmon", "cod", "pollock", "pollack", "haddock",
        "halibut", "tilapia", "swordfish", "sardine", "mackerel", "trout", "herring", "catfish",
        "snapper", "flounder", "mahi mahi", "sea bass", "whitefish", "surimi", "bonito", "caviar",
        "roe", "eel", "grouper", "hake", "branzino", "barramundi",
    ),
    Allergen.shellfish: (
        "shellfish", "crustacean", "mollusc", "mollusk", "shrimp", "prawn", "crab", "crabmeat",
        "lobster", "crayfish", "crawfish", "langoustine", "krill", "clam", "mussel", "oyster",
        "scallop", "squid", "calamari", "octopus", "cuttlefish", "snail", "escargot", "abalone",
        "conch", "whelk",
    ),
    Allergen.tree_nuts: (
        "nut", "tree nut", "almond", "walnut", "pecan", "cashew", "pistachio", "hazelnut",
        "filbert", "macadamia", "brazil nut", "pine nut", "pignoli", "chestnut", "hickory nut",
        "pili nut", "praline", "marzipan", "gianduja", "nutella",
    ),
    Allergen.peanuts: ("peanut", "groundnut", "arachis"),
    Allergen.wheat: (
        "wheat", "semolina", "durum", "spelt", "farro", "kamut", "khorasan", "einkorn", "emmer",
        "triticale", "bulgur", "bulghur", "couscous", "seitan", "farina", "freekeh",
        "graham flour",
    ),
    Allergen.soy: (
        "soy", "soya", "soybean", "edamame", "tofu", "tempeh", "miso", "natto", "tamari", "shoyu",
        "textured vegetable protein", "yuba", "okara",
    ),
    Allergen.sesame: (
        "sesame", "tahini", "tahina", "benne", "gingelly", "gomasio", "halva", "halvah",
    ),
    # Wheat implies gluten too (added in code); these are the other gluten grains
    Allergen.gluten: ("gluten", "barley", "rye", "malt", "malted"),
}

# Meat and animal-derived words for the diet check (fish and shellfish come from above)
MEAT_WORDS = (
    "meat", "beef", "veal", "pork", "chicken", "turkey", "duck", "goose", "lamb", "mutton",
    "venison", "bison", "rabbit", "bacon", "ham", "prosciutto", "pancetta", "salami",
    "pepperoni", "chorizo", "sausage", "jerky", "frankfurter", "poultry", "poultries", "seafood",
    "lard", "tallow", "suet", "gelatin", "gelatine", "gelatina", "collagen", "rennet",
    "animal rennet", "carmine", "cochineal", "isinglass", "bone broth",
)
OTHER_ANIMAL_WORDS = ("honey", "beeswax", "royal jelly", "propolis", "shellac")

_DAIRY_NOUNS = (
    r"(?:milk|cream cheese|sour cream|cream|cheese|cheddar|mozzarella|parmesan|ricotta|feta"
    r"|gouda|provolone|brie|yogh?urt|butter|creamer|spread|ghee|kefir)"
)
# Plant words that turn a dairy word into something else ("oat milk", "cocoa butter")
_PLANT_DAIRY = (
    r"(?:oat|soy|soya|almond|rice|coconut|cashew|hemp|pea|chickpea|flax|macadamia|hazelnut"
    r"|pistachio|walnut|pecan|peanut|nut|seed|sunflower(?: seed)?|pumpkin(?: seed)?|sesame"
    r"|apple|cocoa|cacao|shea|shea ?nut|sheanut|mango|cookie|plant|grain|quinoa|banana|vegan"
    r"|non[\s-]?dairy|dairy[\s-]?free|plant[\s-]?based)"
)
# Strong qualifiers may sit a couple of words away ("vegan cheddar flavor", "vegan natural
# bacon flavor"), but never across "and" / "or" / "with"
_STRONG_DAIRY_QUALIFIER = r"(?:vegan|non[\s-]?dairy|dairy[\s-]?free|plant[\s-]?based)"
_GAP = r"(?:\s+(?!(?:and|or|with)\b)[\w'-]+){0,2}?\s+"

# Highly refined wheat sugars are still wheat, but not gluten ("glucose syrup (from wheat)")
_REFINED_WHEAT = (
    r"(?:glucose|dextrose|maltodextrin|caramel)(?:[\s-]+(?:fructose|syrup|solids|colou?r))*"
    r"\s*[(\[]?\s*(?:from|derived from|made from)\s+wheat(?:\s+(?:and/or|or|and)\s+[a-z]+)?"
)

# Removed before looking for that allergen (other allergens still see the full text)
FALSE_FRIENDS: dict[Allergen, tuple[str, ...]] = {
    Allergen.milk: (
        _PLANT_DAIRY + r"(?:[\s-]+(?:style|type))?\s+" + _DAIRY_NOUNS + r"(?:e?s)?",
        _STRONG_DAIRY_QUALIFIER + _GAP + _DAIRY_NOUNS + r"(?:e?s)?",
        _DAIRY_NOUNS + r"(?:[\s-]+(?:style|type))?(?:\s+chee[sz]e)?\s+"
        r"(?:alternative|substitute|analogue|analog)s?",
        r"cream of (?:tartar|coconut|rice|wheat)",
        r"cream soda",
        r"butter ?beans?",
        r"milk ?thistle",
        r"bean ?curds?",
    ),
    Allergen.egg: (r"egg ?plants?", r"(?:vegan|plant[\s-]?based) eggs?"),
    Allergen.fish: (),
    Allergen.shellfish: (r"oyster (?:mushroom|cracker)s?", r"crab ?apples?"),
    # Coconut and shea are no longer tree nuts in FDA's 2025 guidance
    Allergen.tree_nuts: (r"water ?chestnuts?", r"shea ?nuts?", r"tiger ?nuts?"),
    Allergen.peanuts: (),
    Allergen.wheat: (r"buck ?wheat",),
    Allergen.soy: (),
    Allergen.sesame: (),
    Allergen.gluten: (),
}

# Animal nouns for the meat false friends below, built from the word lists
_ANIMAL_NOUNS = "(?:" + "|".join(
    re.escape(w).replace(r"\ ", r"[\s-]+")
    for w in sorted(
        {*MEAT_WORDS, *ALLERGEN_WORDS[Allergen.fish], *ALLERGEN_WORDS[Allergen.shellfish],
         "burger", "patty", "patties", "crumble", "nugget", "strip", "hot dog", "wiener",
         "bratwurst", "meatball", "deli slice"},
        key=len, reverse=True,
    )
) + r")(?:e?s)?"
_STRONG_PLANT = (
    r"(?:vegan|vegetarian|vegeterian|plant[\s-]?based|meatless|meat[\s-]?free|tofurky|harmless)"
)
_WEAK_PLANT = r"(?:imitation|faux|mock|soy|tofu|seitan|tempeh|jackfruit|no)"
# Flavorings named after meat ("beef flavor", "prawn cocktail flavour", "pork type flavor"):
# real meat shows up in their parentheses anyway
_MEAT_FLAVOR = (
    "(?P<animal>" + _ANIMAL_NOUNS + r")(?:[\s-]+[\w'-]+)?(?:[\s-]+(?:type|style))?[\s-]+"
    r"(?:flavou?r(?:s|ed|ing|ings)?|seasonings?)"
)
MEAT_FALSE_FRIENDS = (
    _STRONG_PLANT + _GAP + _ANIMAL_NOUNS,
    _WEAK_PLANT + r"\s+" + _ANIMAL_NOUNS,
    _MEAT_FLAVOR.replace("?P<animal>", "?:"),
    _ANIMAL_NOUNS + r"[\s-]+(?:type|style)",
    r"(?:mushroom|portabella|portobello|shiitake|coconut|eggplant|jackfruit|carrot|beet|banana"
    r"|mango|fruit)\s+jerky",
    r"(?:coconut|nut|jackfruit|walnut) meat",
    r"meat (?:substitute|analogue|analog|alternative)s?",
    r"duck sauce",
    r"lamb'?s ?lettuce",
    r"(?:chicken|hen) of the woods",
    r"turkey tail(?: mushrooms?)?",
    r"(?:product|produce) of turkey",
    r"(?:from|origin:?|grown in|made in|imported from|packed in) turkey",
    r"(?:chicken|duck|quail|goose|hen|turkey)'?s?\s+eggs?",
    r"vegetable suet",
    # Plain or calf rennet is animal; these kinds are not
    r"(?:microbial|vegetable|vegetarian|non[\s-]?animal|plant|fungal)(?:[\s-]+based)?\s+rennet",
    r"rennet\s*\(\s*(?:non[\s-]?animal|microbial|vegetable|vegetarian)",
    r"swedish fish",
    r"fish[\s-]shaped",
)

# Plant-based meat brands: their "ham" and "sausage" names are not meat
PLANT_MEAT_BRANDS = (
    "tofurky", "lightlife", "gardein", "morningstar", "field roast", "quorn", "boca", "yves",
    "sweet earth", "beyond meat", "impossible", "upton's", "no evil", "hooray", "abbot's",
    "daring", "simulate", "nuggs", "alpha foods", "tindle", "worthington", "loma linda",
    "gimme lean", "smart deli", "plant pioneers", "meatless farm", "vegetarian butcher",
    "unreal deli", "louisville vegan jerky", "primal spirit", "moving mountains",
)
# A product name (or brand) with one of these is a meat substitute: don't read meat words in it
_NAME_PLANT_MARKERS = re.compile(
    r"\b(?:vegan|vegetarian|veggie|plant[\s-]?based|meatless|meat[\s-]?free|imitation|beyond"
    r"|impossible|tofu|tempeh|seitan|faux|mock|jackfruit|chick'?n|chik'?n"
    r"|(?:chicken|meat|beef|pork|fish)[\s-]?less"
    r"|no[\s-](?:chicken|pork|beef|meat|fish|tuna|bacon|ham|turkey)"
    r"|meat (?:substitute|alternative)s?|"
    + "|".join(re.escape(b) for b in PLANT_MEAT_BRANDS) + r")\b"
)
# Same for categories ("en:meat-analogues"); broad ones like "plant-based foods" don't count
_CATEGORY_PLANT_MARKERS = re.compile(
    r"\b(?:vegan|vegetarian|meatless|meat free|imitation|analogues?|analogs?|alternatives?"
    r"|substitutes?)\b"
)
# Products named after the meat they go with ("chicken seasoning", "bacon flavored bits")
_FLAVOR_MARKERS = re.compile(
    r"\b(?:flavou?r(?:s|ed)?|seasonings?|rubs?|marinades?|spices?|sauces?|gravy|bouillon"
    r"|dressings?)\b"
)
# OFF files seaweed snacks under "seafood"
_SEAWEED = re.compile(r"\b(?:seaweeds?|algae|nori|kelp|spirulina|sea vegetables?)\b")

_NEGATED_BEFORE = re.compile(
    r"(?:\b(?:no|non|not|without|zero)|\bfree (?:of|from)|\b(?:does not|doesn't|do not) contain)"
    r"[\s-]+(?:added[\s-]+|any[\s-]+)?$"
)
_NEGATED_AFTER = re.compile(r"^[\s-]*free\b(?![\s-]*range)")

# Clauses cut out of the ingredient statement (each runs to the next . ; or ")"):
# "does not contain peanuts, fish or eggs", "free from milk, eggs", "No: milk, egg" (dropped)
_NEGATION_CLAUSE = re.compile(
    r"(?:\b(?:does not|doesn't|do not|did not|never) (?:contain|use|include|process|handle)"
    r"(?:s|es)?(?: any)?\b|\bfree (?:of|from)\b|\bmade without\b|\bno:)"
    r"(?:(?!\bcontains?\b)[^.;)])*"
)
# Serving suggestions: "delicious with seafood" (dropped)
_SERVING_CLAUSE = re.compile(
    r"\b(?:serve[ds]?|serving suggestion|delicious|pairs?|paired|great|perfect|enjoy(?:ed)?"
    r"|goes) (?:well |nicely |best )?with\b[^.;)]*"
)
# "May contain ...", "made in a facility that also processes ..." (kept as traces)
_TRACE_CLAUSE = re.compile(
    r"(?:\bmay (?:also )?contain|\bmay be present|\bcan contain|\bmight contain|\btraces? of"
    r"|\b(?:made|manufactured|produced|processed|packed|packaged|prepared) (?:in|on|with) "
    r"(?:a |an |the )?(?:same |shared |common )?(?:facility|facilities|plant|equipment|line"
    r"|premises)"
    r"|\b(?:in|on) (?:a |an |the )?(?:same |shared |common )?(?:facility|plant|equipment|line"
    r"|premises) (?:that|which|where|with)\b"
    r"|\balso (?:processes|handles|uses|manufactures|packages|packs|produces)\b"
    r"|\bshared (?:facility|equipment|line)|\bcross[\s-]contact"
    r"|\bcross[\s-]contamination)[^.;)]*"
)
_OATS = re.compile(r"\boat(?:s|meal)?\b")
_GLUTEN_FREE_CLAIM = re.compile(r"\bgluten[\s-]?free\b|\b(?:no|without|free (?:of|from)) gluten\b")
_LETTERS = re.compile(r"[a-z]")


def _words_pattern(words: Iterable[str]) -> re.Pattern[str]:
    """Whole-word regex for words or phrases, plural allowed, '-' or space between words."""
    alternatives = sorted(words, key=len, reverse=True)
    body = "|".join(re.escape(w).replace(r"\ ", r"[\s-]+") for w in alternatives)
    return re.compile(r"\b(?:" + body + r")(?:e?s)?\b")


def _phrases_pattern(phrases: Iterable[str]) -> re.Pattern[str] | None:
    phrases = list(phrases)
    return re.compile(r"\b(?:" + "|".join(phrases) + r")\b") if phrases else None


_ALLERGEN_RES = {a: _words_pattern(words) for a, words in ALLERGEN_WORDS.items()}
_FALSE_FRIEND_RES = {a: _phrases_pattern(p) for a, p in FALSE_FRIENDS.items()}
_REFINED_WHEAT_RE = _phrases_pattern([_REFINED_WHEAT])
_MEAT_FLAVOR_RE = re.compile(r"\b" + _MEAT_FLAVOR + r"\b")
_MEAT_RE = _words_pattern(MEAT_WORDS)
_OTHER_ANIMAL_RE = _words_pattern(OTHER_ANIMAL_WORDS)
_MEAT_FALSE_FRIEND_RE = _phrases_pattern(MEAT_FALSE_FRIENDS)


# --- Text helpers -----------------------------------------------------------------------

def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'")
    return " ".join(text.split())


def _normalize_tag(tag: str) -> str:
    """'en:sesame-seeds' -> 'sesame seeds'; 'Tree Nuts' -> 'tree nuts'."""
    tag = re.sub(r"^[a-z]{2}:", "", _normalize(tag))
    return tag.replace("-", " ").replace("_", " ")


def _declared(values: Iterable[str]) -> str:
    return " ; ".join(_normalize_tag(v) for v in values if v and v.strip())


@lru_cache(maxsize=4096)
def _split_text(text: str) -> tuple[str, str]:
    """Split a normalized ingredient statement into (contents, may-contain clauses).

    Serving suggestions and "does not contain ..." clauses are dropped from both.
    """
    text = _SERVING_CLAUSE.sub(" ; ", text)
    text = _NEGATION_CLAUSE.sub(" ; ", text)
    traces = " ; ".join(m.group(0) for m in _TRACE_CLAUSE.finditer(text))
    return _TRACE_CLAUSE.sub(" ; ", text), traces


def _has_words(text: str) -> bool:
    return bool(_LETTERS.search(text.lower()))


def _matches(
    text: str, pattern: re.Pattern[str], false_friends: Iterable[re.Pattern[str] | None] = ()
) -> list[str]:
    """Words of `pattern` found in `text`, minus false friends and negated mentions."""
    for friend in false_friends:
        if friend is not None:
            text = friend.sub(" ", text)
    found = []
    for m in pattern.finditer(text):
        before = text[max(0, m.start() - 30):m.start()]
        if _NEGATED_BEFORE.search(before) or _NEGATED_AFTER.match(text[m.end():]):
            continue
        found.append(" ".join(m.group(0).split()))
    return found


def _allergens_in(text: str) -> set[Allergen]:
    found = {
        a for a in Allergen
        if _matches(text, _ALLERGEN_RES[a], [_FALSE_FRIEND_RES[a]])
    }
    # Wheat implies gluten, unless the only wheat is a refined sugar made from it
    if Allergen.wheat in found and _matches(
        text, _ALLERGEN_RES[Allergen.wheat], [_FALSE_FRIEND_RES[Allergen.wheat], _REFINED_WHEAT_RE]
    ):
        found.add(Allergen.gluten)
    return found


def _gluten_free_claim(evidence: ProductEvidence) -> bool:
    """Does the name, ingredient statement or a category claim gluten-free?"""
    claim_text = " ".join(
        [_normalize(evidence.name), _normalize(evidence.ingredients_text)]
        + [_normalize_tag(c) for c in evidence.categories]
    )
    return bool(_GLUTEN_FREE_CLAIM.search(claim_text))


def _unique(words: Iterable[str]) -> list[str]:
    """Drop repeats, treating singular and plural as one ('eggs' tag + 'egg' word -> 'eggs')."""
    seen: set[str] = set()
    out = []
    for word in words:
        key = word.rstrip("s")
        if key not in seen:
            seen.add(key)
            out.append(word)
    return out


def _join_or(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


# --- Diet facts -------------------------------------------------------------------------

@dataclass
class _DietFacts:
    meat: list[str] = field(default_factory=list)          # proves non-vegetarian
    animal: list[str] = field(default_factory=list)        # proves non-vegan (milk, eggs, honey)
    trace_animal: list[str] = field(default_factory=list)  # "may contain", doesn't change the class
    has_ingredients: bool = False


def _meat_words(text: str) -> list[str]:
    text = _MEAT_FALSE_FRIEND_RE.sub(" ", text)
    return (
        _matches(text, _ALLERGEN_RES[Allergen.fish], [_FALSE_FRIEND_RES[Allergen.fish]])
        + _matches(text, _ALLERGEN_RES[Allergen.shellfish], [_FALSE_FRIEND_RES[Allergen.shellfish]])
        + _matches(text, _MEAT_RE)
    )


def _animal_words(text: str) -> list[str]:
    return (
        _matches(text, _ALLERGEN_RES[Allergen.milk], [_FALSE_FRIEND_RES[Allergen.milk]])
        + _matches(text, _ALLERGEN_RES[Allergen.egg], [_FALSE_FRIEND_RES[Allergen.egg]])
        + _matches(text, _OTHER_ANIMAL_RE)
    )


def _name_meat_words(evidence: ProductEvidence, content: str) -> list[str]:
    """Meat named by the product name or categories: the cure-only bacon fix.

    'Bacon' cured with 'water, salt, sugar, sodium nitrite' names no animal in its
    ingredients, but it is still pork. Skipped for meat substitutes (by name, brand or
    category), for products named after a flavor ('chicken seasoning'), and for meat the
    ingredient list only mentions as a flavoring ('BBQ Beef' crisps with 'beef flavour').
    """
    flavors = {" ".join(m.group("animal").split()).rstrip("s")
               for m in _MEAT_FLAVOR_RE.finditer(content)}
    name = _normalize(evidence.name)
    brands = [b.strip() for b in _normalize(evidence.brand).split(",") if b.strip()]
    categories = [_normalize_tag(c) for c in evidence.categories]
    if _NAME_PLANT_MARKERS.search(" | ".join([name, *brands])) or any(
        _CATEGORY_PLANT_MARKERS.search(c) for c in categories
    ):
        return []
    if _SEAWEED.search(name) or any(_SEAWEED.search(c) for c in categories):
        categories = [c for c in categories if "seafood" not in c]
    for brand in brands:
        name = name.replace(brand, " ")  # 'Turkey Hill ice cream' is not turkey

    found = []
    if not _FLAVOR_MARKERS.search(name):
        found += [f"{w} (product name)" for w in _meat_words(name) if w.rstrip("s") not in flavors]
    for category in categories:
        if not _FLAVOR_MARKERS.search(category):
            found += [f"{w} (category)" for w in _meat_words(category)
                      if w.rstrip("s") not in flavors]
    return found


def _diet_facts(evidence: ProductEvidence) -> _DietFacts:
    content, trace_text = _split_text(_normalize(evidence.ingredients_text))
    declared = _declared(evidence.allergens)
    declared_traces = _declared(evidence.traces)

    meat = _meat_words(declared) + _meat_words(content)
    if not meat:
        meat = _name_meat_words(evidence, content)
    animal = _animal_words(declared) + _animal_words(content)
    trace_animal = [
        w for text in (declared_traces, trace_text)
        for w in _meat_words(text) + _animal_words(text)
    ]
    return _DietFacts(
        meat=_unique(meat), animal=_unique(animal), trace_animal=_unique(trace_animal),
        has_ingredients=_has_words(content),
    )


def _uncertain_allergens(evidence: ProductEvidence, content: str) -> dict[Allergen, str]:
    """Allergens we can't rule in or out from the text: oats without a gluten-free claim."""
    if _OATS.search(content) and not _gluten_free_claim(evidence):
        return {
            Allergen.gluten: "Oats without a gluten-free claim may carry gluten (cross-contact)"
        }
    return {}


# --- Public API -------------------------------------------------------------------------

def has_evidence(evidence: ProductEvidence | None) -> bool:
    """True when the evidence has an ingredient statement or declared allergens.

    Declared allergens include "may contain" declarations. Name and categories alone
    are not evidence.
    """
    if evidence is None:
        return False
    return bool(
        evidence.ingredients_text.strip()
        or any(a.strip() for a in evidence.allergens)
        or any(t.strip() for t in evidence.traces)
    )


def pick_evidence(*candidates: ProductEvidence | None) -> ProductEvidence | None:
    """Return the first candidate with usable evidence (store data first, then OFF).

    A candidate with an ingredient statement wins over an earlier one with only declared
    allergens, because only an ingredient statement can show a product is safe.
    """
    usable = [c for c in candidates if has_evidence(c)]
    for candidate in usable:
        if _has_words(candidate.ingredients_text):
            return candidate
    return usable[0] if usable else None


def detect_allergens(evidence: ProductEvidence) -> tuple[set[Allergen], set[Allergen]]:
    """Return (contains, may_contain) allergens found in the declarations and ingredient text.

    "May contain" sentences inside the ingredient statement count as may_contain. An
    allergen in both sets is only reported as contained. Oats without a gluten-free claim
    are in neither set: assess() reports gluten as unknown for them.

    A declared gluten tag (OFF has no wheat tag, only en:gluten) is set aside when the
    ingredient list names no gluten grain but has oats or a gluten-free claim: OFF tags
    oats as gluten (EU rule) and sometimes reads "gluten-free" as gluten.
    """
    content, trace_text = _split_text(_normalize(evidence.ingredients_text))
    declared = _allergens_in(_declared(evidence.allergens))
    in_text = _allergens_in(content)
    if (
        Allergen.gluten in declared
        and Allergen.wheat not in declared
        and Allergen.gluten not in in_text
        and _has_words(content)
        and (_OATS.search(content) or _gluten_free_claim(evidence))
    ):
        declared.discard(Allergen.gluten)
    contains = declared | in_text
    may_contain = _allergens_in(_declared(evidence.traces)) | _allergens_in(trace_text)
    return contains, may_contain - contains


def classify_diet(evidence: ProductEvidence) -> tuple[DietClass, str]:
    """Return what the product is (vegan / vegetarian / non_vegetarian / unknown) and why."""
    if not has_evidence(evidence):
        return DietClass.unknown, "No ingredient or allergen information"
    facts = _diet_facts(evidence)
    if facts.meat:
        return DietClass.non_vegetarian, "Contains " + ", ".join(facts.meat)
    if not facts.has_ingredients:
        return DietClass.unknown, "No ingredient list"
    if facts.animal:
        return DietClass.vegetarian, "Contains " + ", ".join(facts.animal)
    if facts.trace_animal:
        return DietClass.vegan, (
            "No animal ingredients; may contain traces of " + ", ".join(facts.trace_animal)
        )
    return DietClass.vegan, "No animal ingredients found"


def assess(evidence: ProductEvidence | None, avoid: Iterable[Allergen], diet: Diet) -> Verdict:
    """Verdict for one product, one user: unsafe beats unknown beats safe."""
    avoid_set = {Allergen(a) for a in avoid}
    diet = Diet(diet)
    source = evidence.source if evidence is not None and has_evidence(evidence) else "none"

    if not avoid_set and diet == Diet.none:
        return Verdict(verdict=VerdictKind.safe, reason="No restrictions to check",
                       evidence_source=source)
    if evidence is None or not has_evidence(evidence):
        return Verdict(verdict=VerdictKind.unknown,
                       reason="No ingredient or allergen information", evidence_source="none")

    contains, may_contain = detect_allergens(evidence)
    content, _ = _split_text(_normalize(evidence.ingredients_text))
    has_ingredients = _has_words(content)
    uncertain = _uncertain_allergens(evidence, content)

    avoided = [a for a in Allergen if a in avoid_set]
    unsafe = [f"Contains {LABELS[a]}" for a in avoided if a in contains]
    unsafe += [f"May contain {LABELS[a]} (cross-contact)" for a in avoided if a in may_contain]
    unknown: list[str] = []
    unresolved = [a for a in avoided if a not in contains | may_contain]
    if unresolved and not has_ingredients:
        labels = [LABELS[a] for a in unresolved]
        unknown.append("No ingredient list to check for " + _join_or(labels))
    elif unresolved:
        unknown += [uncertain[a] for a in unresolved if a in uncertain]

    trace_note = ""
    if diet != Diet.none:
        facts = _diet_facts(evidence)
        if facts.meat:
            unsafe.append(f"Not {diet.value}: contains " + ", ".join(facts.meat))
        elif diet == Diet.vegan and facts.animal:
            unsafe.append("Not vegan: contains " + ", ".join(facts.animal))
        elif not facts.has_ingredients:
            unknown.append(f"No ingredient list to check if {diet.value}")
        elif facts.trace_animal:
            trace_note = " (may contain traces of " + ", ".join(facts.trace_animal) + ")"

    if unsafe:
        return Verdict(verdict=VerdictKind.unsafe, reason="; ".join(unsafe),
                       evidence_source=source)
    if unknown:
        return Verdict(verdict=VerdictKind.unknown, reason="; ".join(unknown),
                       evidence_source=source)
    parts = []
    if avoid_set:
        labels = [LABELS[a] for a in Allergen if a in avoid_set]
        parts.append(f"No {_join_or(labels)} found in ingredients")
    if diet != Diet.none:
        parts.append(f"Fits a {diet.value} diet{trace_note}")
    return Verdict(verdict=VerdictKind.safe, reason="; ".join(parts), evidence_source=source)
