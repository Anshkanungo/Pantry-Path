"""Diet rules: allergens, diets, false friends, missing evidence, and the lab's real products.

products_snapshot.json is the Open Food Facts snapshot from the lab (mlops-github-lab1),
ported here so the old cases keep passing under the new interface.
"""

import json
from pathlib import Path

import pytest

from pantrypath_core.diet_rules import (
    Allergen,
    Diet,
    DietClass,
    ProductEvidence,
    VerdictKind,
    assess,
    classify_diet,
    detect_allergens,
    has_evidence,
    pick_evidence,
)

SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "products_snapshot.json"
PRODUCTS = json.loads(SNAPSHOT.read_text())["products"]
LAB_DIETS = {"vegan": DietClass.vegan, "vegetarian": DietClass.vegetarian,
             "non-vegetarian": DietClass.non_vegetarian}


def ev(text="", allergens=(), traces=(), name="", brand="", categories=(), source="off"):
    return ProductEvidence(source=source, name=name, brand=brand, categories=list(categories),
                           ingredients_text=text, allergens=list(allergens), traces=list(traces))


# --- 1. Lab cases, ported -----------------------------------------------------------------

@pytest.mark.parametrize("product", PRODUCTS, ids=[p["name"] for p in PRODUCTS])
def test_lab_real_products_get_the_diet_on_their_label(product):
    e = product["evidence"]
    evidence = ev(e["ingredients_text"], e["allergens"], e["traces"],
                  name=product["name"], brand=product["brand"])
    diet_class, reason = classify_diet(evidence)
    assert diet_class == LAB_DIETS[product["expected_diet"]], reason


@pytest.mark.parametrize("diet, text, verdict", [
    (Diet.vegan, "water, organic soybeans", VerdictKind.safe),         # vegan eats tofu
    (Diet.vegan, "pasteurized cow's milk, salt", VerdictKind.unsafe),  # vegan skips brie
    (Diet.vegetarian, "eggs", VerdictKind.safe),                       # vegetarian eats eggs
    (Diet.vegetarian, "swordfish", VerdictKind.unsafe),                # vegetarian skips fish
    (Diet.none, "swordfish", VerdictKind.safe),                        # no restriction
])
def test_lab_who_can_eat_what(diet, text, verdict):
    assert assess(ev(text), [], diet).verdict == verdict


@pytest.mark.parametrize("text", [
    "peanut butter, salt", "oat milk, sugar", "cocoa butter, sugar", "eggplant, olive oil",
])
def test_lab_plant_phrases_are_vegan(text):
    assert classify_diet(ev(text))[0] == DietClass.vegan


def test_lab_text_catches_meat_when_tags_are_missing():
    assert classify_diet(ev("Albacore Tuna, Water, Sea Salt"))[0] == DietClass.non_vegetarian


def test_lab_trace_only_stays_vegan_with_note():
    diet_class, reason = classify_diet(ev("durum wheat semolina", traces=["eggs"]))
    assert diet_class == DietClass.vegan
    assert "may contain traces of eggs" in reason


def test_lab_reason_does_not_repeat_singular_and_plural():
    assert classify_diet(ev("Eggs", allergens=["eggs"]))[1] == "Contains eggs"


def test_lab_unknown_diet_is_rejected():
    with pytest.raises(ValueError):
        assess(ev("water"), [], "keto")


# --- 2. Allergens -------------------------------------------------------------------------

@pytest.mark.parametrize("allergen, text", [
    (Allergen.milk, "skim milk, vitamin a palmitate"),
    (Allergen.milk, "whey protein concentrate"),
    (Allergen.milk, "sodium caseinate (a milk derivative)"),
    (Allergen.milk, "cheddar cheese (pasteurized cultured milk, salt, enzymes)"),
    (Allergen.milk, "BUTTER, ghee, buttermilk"),
    (Allergen.milk, "milkfat, sugar"),
    (Allergen.egg, "dried egg whites"),
    (Allergen.egg, "Egg Yolks, water"),
    (Allergen.egg, "albumen"),
    (Allergen.fish, "anchovies, olive oil"),
    (Allergen.fish, "Atlantic salmon"),
    (Allergen.fish, "worcestershire sauce (vinegar, molasses, anchovy)"),
    (Allergen.shellfish, "shrimp, salt"),
    (Allergen.shellfish, "oyster extract, sugar"),
    (Allergen.shellfish, "lobster meat"),
    (Allergen.tree_nuts, "roasted almonds"),
    (Allergen.tree_nuts, "cashew pieces, sea salt"),
    (Allergen.tree_nuts, "hazelnut paste"),
    (Allergen.tree_nuts, "pine nuts"),
    (Allergen.tree_nuts, "mixed nuts"),
    (Allergen.peanuts, "roasted peanuts"),
    (Allergen.peanuts, "peanut oil"),
    (Allergen.peanuts, "peanut butter"),
    (Allergen.wheat, "enriched wheat flour"),
    (Allergen.wheat, "SEMOLINA"),
    (Allergen.wheat, "spelt flour"),
    (Allergen.wheat, "couscous"),
    (Allergen.soy, "soy lecithin (emulsifier)"),
    (Allergen.soy, "soybean oil"),
    (Allergen.soy, "tofu, miso"),
    (Allergen.sesame, "toasted sesame seeds"),
    (Allergen.sesame, "tahini"),
    (Allergen.gluten, "barley malt extract"),
    (Allergen.gluten, "rye flour"),
    (Allergen.gluten, "malt vinegar"),
])
def test_allergen_found_in_ingredient_text(allergen, text):
    contains, may_contain = detect_allergens(ev(text))
    assert allergen in contains
    assert allergen not in may_contain


@pytest.mark.parametrize("declared, allergen", [
    # Open Food Facts tags
    ("en:milk", Allergen.milk),
    ("en:eggs", Allergen.egg),
    ("en:fish", Allergen.fish),
    ("en:crustaceans", Allergen.shellfish),
    ("en:molluscs", Allergen.shellfish),
    ("en:nuts", Allergen.tree_nuts),
    ("en:peanuts", Allergen.peanuts),
    ("en:gluten", Allergen.gluten),
    ("en:soybeans", Allergen.soy),
    ("en:sesame-seeds", Allergen.sesame),
    # Store-style words
    ("Milk and its derivatives", Allergen.milk),
    ("Tree Nuts", Allergen.tree_nuts),
    ("Wheat", Allergen.wheat),
    ("Wheat", Allergen.gluten),
    ("Crustacean Shellfish", Allergen.shellfish),
    ("Soybean", Allergen.soy),
    ("Peanuts", Allergen.peanuts),
    ("Sesame Seeds", Allergen.sesame),
])
def test_declared_allergens_are_recognized(declared, allergen):
    assert allergen in detect_allergens(ev(allergens=[declared]))[0]


def test_declared_peanuts_are_not_tree_nuts():
    assert detect_allergens(ev(allergens=["Peanuts"]))[0] == {Allergen.peanuts}


def test_off_gluten_tag_proves_gluten_but_not_wheat():
    # OFF has no wheat tag: en:gluten covers wheat, barley and rye
    gluten_only = ev(allergens=["en:gluten"])
    assert detect_allergens(gluten_only)[0] == {Allergen.gluten}
    assert assess(gluten_only, [Allergen.gluten], Diet.none).verdict == VerdictKind.unsafe
    wheat = assess(gluten_only, [Allergen.wheat], Diet.none)
    assert wheat.verdict == VerdictKind.unknown
    assert wheat.reason == "No ingredient list to check for wheat"


def test_off_gluten_tag_with_text_reads_wheat_from_the_text():
    barley = ev("pearled barley", allergens=["en:gluten"])
    assert assess(barley, [Allergen.wheat], Diet.none).verdict == VerdictKind.safe
    bread = ev("wheat flour, water, yeast", allergens=["en:gluten"])
    assert assess(bread, [Allergen.wheat], Diet.none).verdict == VerdictKind.unsafe


@pytest.mark.parametrize("tag", ["en:Lactic", "en:", "fr:lait", "en:celery", "en:mustard", ""])
def test_unknown_or_stray_tags_are_ignored(tag):
    contains, may_contain = detect_allergens(ev("water, salt", allergens=[tag], traces=[tag]))
    assert contains == set() and may_contain == set()


@pytest.mark.parametrize("text, allergen", [
    ("peanut butter, salt", Allergen.milk),
    ("cocoa butter, sugar", Allergen.milk),
    ("shea butter", Allergen.milk),
    ("coconut milk, water", Allergen.milk),
    ("coconut cream", Allergen.milk),
    ("almond milk (filtered water, almonds)", Allergen.milk),
    ("cream of tartar", Allergen.milk),
    ("lactic acid", Allergen.milk),
    ("non-dairy creamer (corn syrup solids, coconut oil)", Allergen.milk),
    ("butternut squash", Allergen.milk),
    ("butter beans", Allergen.milk),
    ("eggplant, olive oil", Allergen.egg),
    ("oyster mushrooms", Allergen.shellfish),
    ("coconut, sugar", Allergen.tree_nuts),
    ("nutmeg, cinnamon", Allergen.tree_nuts),
    ("water chestnuts", Allergen.tree_nuts),
    ("butternut squash", Allergen.tree_nuts),
    ("peanut butter", Allergen.tree_nuts),
    ("shea nut butter", Allergen.tree_nuts),
    ("buckwheat flour", Allergen.wheat),
    ("buckwheat flour", Allergen.gluten),
    ("maltodextrin", Allergen.gluten),
    ("gluten-free oats", Allergen.gluten),
])
def test_false_friends_are_not_allergens(text, allergen):
    contains, may_contain = detect_allergens(ev(text))
    assert allergen not in contains | may_contain


@pytest.mark.parametrize("text, allergen", [
    ("dairy-free chocolate (sugar, cocoa)", Allergen.milk),
    ("contains no milk", Allergen.milk),
    ("Free from peanuts", Allergen.peanuts),
    ("egg free pasta: rice flour, water", Allergen.egg),
    ("does not contain soy", Allergen.soy),
])
def test_negated_mentions_are_ignored(text, allergen):
    assert allergen not in detect_allergens(ev(text))[0]


def test_lactose_free_milk_is_still_milk():
    assert Allergen.milk in detect_allergens(ev("lactose-free milk"))[0]


def test_plant_milk_still_has_its_own_allergen():
    contains, _ = detect_allergens(ev("almond milk (filtered water, almonds)"))
    assert contains == {Allergen.tree_nuts}


@pytest.mark.parametrize("text, expected", [
    ("whole wheat flour", {Allergen.wheat, Allergen.gluten}),
    ("spelt", {Allergen.wheat, Allergen.gluten}),
    ("pearled barley", {Allergen.gluten}),
])
def test_wheat_implies_gluten_but_not_the_reverse(text, expected):
    assert detect_allergens(ev(text))[0] == expected


# --- 3. Traces ("may contain") ------------------------------------------------------------

@pytest.mark.parametrize("text, allergens", [
    ("sugar, cocoa. May contain peanuts.", {Allergen.peanuts}),
    ("rice, salt. Manufactured in a facility that also processes milk and eggs.",
     {Allergen.milk, Allergen.egg}),
    ("oats, honey. Made on shared equipment with tree nuts", {Allergen.tree_nuts}),
])
def test_may_contain_sentences_are_traces(text, allergens):
    contains, may_contain = detect_allergens(ev(text))
    assert may_contain == allergens
    assert not contains & allergens


def test_declared_traces_are_may_contain():
    assert detect_allergens(ev("dark chocolate", traces=["en:nuts"]))[1] == {Allergen.tree_nuts}


def test_contained_allergen_is_not_also_may_contain():
    contains, may_contain = detect_allergens(ev("milk chocolate. May contain milk, peanuts."))
    assert Allergen.milk in contains
    assert may_contain == {Allergen.peanuts}


def test_trace_clause_stops_at_closing_parenthesis():
    text = "chips (sugar, cocoa; may contain milk), almonds, salt"
    contains, may_contain = detect_allergens(ev(text))
    assert Allergen.tree_nuts in contains
    assert Allergen.milk in may_contain


def test_may_contain_avoided_allergen_is_unsafe():
    v = assess(ev("dark chocolate, sugar", traces=["Tree Nuts"]), [Allergen.tree_nuts], Diet.none)
    assert v.verdict == VerdictKind.unsafe
    assert v.reason == "May contain tree nuts (cross-contact)"


# --- 4. Oats and gluten -------------------------------------------------------------------

def test_oats_without_gluten_free_claim_are_unknown_for_gluten():
    v = assess(ev("whole grain oats, sugar, salt"), [Allergen.gluten], Diet.none)
    assert v.verdict == VerdictKind.unknown
    assert "Oats" in v.reason


@pytest.mark.parametrize("text, name", [
    ("certified gluten-free whole grain oats", "Rolled Oats"),
    ("whole grain oats", "Gluten Free Old Fashioned Oats"),
])
def test_oats_with_gluten_free_claim_are_safe(text, name):
    assert assess(ev(text, name=name), [Allergen.gluten], Diet.none).verdict == VerdictKind.safe


def test_oats_with_wheat_are_unsafe_for_gluten():
    v = assess(ev("oats, wheat flour, sugar"), [Allergen.gluten], Diet.none)
    assert v.verdict == VerdictKind.unsafe
    assert v.reason == "Contains gluten"


# --- 5. Diet: meat by name or category (the cure-only bacon fix) --------------------------

CURE_ONLY = "Cured with water, salt, sugar, sodium phosphates, sodium ascorbate, sodium nitrite"


def test_cure_only_bacon_is_not_vegetarian():
    bacon = ev(CURE_ONLY, name="Original Bacon", brand="Oscar Mayer")
    diet_class, reason = classify_diet(bacon)
    assert diet_class == DietClass.non_vegetarian
    assert "bacon" in reason
    v = assess(bacon, [], Diet.vegetarian)
    assert v.verdict == VerdictKind.unsafe
    assert "bacon" in v.reason


def test_meat_category_counts_when_name_is_vague():
    product = ev(CURE_ONLY, name="Thick Cut Original", categories=["en:meats", "en:bacons"])
    assert classify_diet(product)[0] == DietClass.non_vegetarian


@pytest.mark.parametrize("name, categories", [
    ("Vegan Bacon Strips", []),
    ("Plant-Based Italian Sausage", []),
    ("Bacon Flavored Bits", []),
    ("Chicken Seasoning", []),
    ("Italian Sausage", ["en:plant-based-foods", "en:meat-analogues", "en:sausages"]),
])
def test_meat_words_in_substitute_or_flavor_names_do_not_count(name, categories):
    product = ev("water, soy protein, wheat gluten, salt, natural flavor, caramel color",
                 name=name, categories=categories)
    assert classify_diet(product)[0] == DietClass.vegan


def test_brand_in_name_is_not_read_as_meat():
    product = ev("milk, cream, sugar, vanilla", name="Turkey Hill Vanilla Ice Cream",
                 brand="Turkey Hill")
    assert classify_diet(product)[0] == DietClass.vegetarian


def test_chicken_eggs_are_vegetarian():
    assert classify_diet(ev("eggs", name="Large Chicken Eggs"))[0] == DietClass.vegetarian


@pytest.mark.parametrize("text", [
    "pork, water, salt", "beef stock, salt", "gelatin, sugar", "chicken fat, salt",
    "shrimp, garlic", "lard, flour",
])
def test_meat_in_text_is_non_vegetarian(text):
    assert classify_diet(ev(text))[0] == DietClass.non_vegetarian


@pytest.mark.parametrize("text", [
    "coconut meat, water", "plum sauce (duck sauce): plums, sugar", "jackfruit meat, salt",
])
def test_meat_false_friends_are_vegan(text):
    assert classify_diet(ev(text))[0] == DietClass.vegan


# --- 6. Diet: honey and other animal products ---------------------------------------------

def test_honey_is_vegetarian_but_not_vegan():
    honey = ev("organic raw honey", name="Raw Honey")
    assert classify_diet(honey)[0] == DietClass.vegetarian
    vegan = assess(honey, [], Diet.vegan)
    assert vegan.verdict == VerdictKind.unsafe
    assert vegan.reason == "Not vegan: contains honey"
    assert assess(honey, [], Diet.vegetarian).verdict == VerdictKind.safe


def test_vegan_product_with_trace_note_is_safe():
    v = assess(ev("durum wheat semolina", traces=["en:eggs"]), [], Diet.vegan)
    assert v.verdict == VerdictKind.safe
    assert "may contain traces of eggs" in v.reason


# --- 7. Missing evidence and restrictions -------------------------------------------------

@pytest.mark.parametrize("evidence", [None, ev(), ev("   ", allergens=[" "])])
def test_no_evidence_is_unknown_never_safe(evidence):
    v = assess(evidence, [Allergen.milk], Diet.vegan)
    assert v.verdict == VerdictKind.unknown
    assert v.evidence_source == "none"
    assert not has_evidence(evidence)


def test_no_restrictions_is_safe_even_without_evidence():
    v = assess(None, [], Diet.none)
    assert v.verdict == VerdictKind.safe
    assert v.reason == "No restrictions to check"
    assert v.evidence_source == "none"


def test_declared_allergens_alone_can_prove_unsafe():
    milk_only = ev(allergens=["en:milk"], source="kroger")
    assert assess(milk_only, [Allergen.milk], Diet.none).verdict == VerdictKind.unsafe
    assert assess(milk_only, [], Diet.vegan).verdict == VerdictKind.unsafe


def test_declared_allergens_alone_cannot_prove_safe():
    milk_only = ev(allergens=["en:milk"])
    v = assess(milk_only, [Allergen.egg, Allergen.gluten], Diet.none)
    assert v.verdict == VerdictKind.unknown
    assert v.reason == "No ingredient list to check for eggs or gluten"
    assert assess(milk_only, [], Diet.vegetarian).verdict == VerdictKind.unknown
    assert classify_diet(milk_only) == (DietClass.unknown, "No ingredient list")


def test_unsafe_beats_unknown():
    v = assess(ev(allergens=["en:milk"]), [Allergen.milk, Allergen.egg], Diet.none)
    assert v.verdict == VerdictKind.unsafe
    assert v.reason == "Contains milk"


def test_safe_needs_an_ingredient_list_and_says_why():
    v = assess(ev("rice, water, salt", source="kroger"), [Allergen.milk, Allergen.gluten],
               Diet.vegetarian)
    assert v.verdict == VerdictKind.safe
    assert v.reason == "No milk or gluten found in ingredients; Fits a vegetarian diet"
    assert v.evidence_source == "kroger"


def test_allergen_and_diet_reasons_are_combined():
    v = assess(ev("milk, bacon"), [Allergen.milk], Diet.vegetarian)
    assert v.verdict == VerdictKind.unsafe
    assert v.reason == "Contains milk; Not vegetarian: contains bacon"


def test_contains_reasons_come_before_may_contain():
    label = "Wheat flour, sugar, milk. Contains: wheat, milk. May contain: tree nuts."
    v = assess(ev(label, source="kroger"), [Allergen.tree_nuts, Allergen.gluten], Diet.vegan)
    assert v.reason == ("Contains gluten; May contain tree nuts (cross-contact); "
                        "Not vegan: contains milk")


def test_avoid_accepts_plain_strings():
    assert assess(ev("sesame seeds"), ["sesame"], "none").verdict == VerdictKind.unsafe


def test_classify_without_evidence_is_unknown():
    assert classify_diet(ev(name="Bacon"))[0] == DietClass.unknown


# --- 8. Cases found by checking the rules against OFF labels on the real snapshot ---------

@pytest.mark.parametrize("text", [
    "water, soy protein, natural pork type flavor (water, yeast extract, salt)",
    "semolina, vegan broth concentrate chicken type (maltodextrin, salt, yeast extract)",
    "rice, chicken flavor (salt, monosodium glutamate, onion powder)",
    "noodles, organic roast beef flavor (organic maltodextrin, salt)",
    "vegan oyster sauce (shiitake, sugar, salt)",
    "tofurky italian sausage (organic tofu, water, wheat gluten)",
    "pretzels, vegetarian frankfurter (filtered water, vital wheat gluten)",
    "portabella jerky (portabella mushrooms, coconut aminos, garlic)",
    "dried apricots (product of turkey), sulfur dioxide",
    "chaga mushroom, turkey tail mushroom, oyster mushroom",
    "grapes, sulfites. delicious with seafood or a goats' cheese salad.",
])
def test_vegan_texts_with_animal_words_are_vegan(text):
    assert classify_diet(ev(text))[0] == DietClass.vegan


@pytest.mark.parametrize("text, expected", [
    ("grana padano (raw cow milk, salt, calf rennet)", DietClass.non_vegetarian),
    ("pasteurized milk, salt, rennet", DietClass.non_vegetarian),
    ("pasteurized milk, cultures, salt, microbial rennet", DietClass.vegetarian),
    ("pasteurized milk, salt, vegetable rennet", DietClass.vegetarian),
    ("pasteurized milk, cultures, salt, enzymes. contains no animal rennet.", DietClass.vegetarian),
    ("leche, azucar, gelatina", DietClass.non_vegetarian),
    ("blend of cow's, goat's, sheep's milk, salt, rennet (non animal), mint", DietClass.vegetarian),
])
def test_rennet_and_gelatin(text, expected):
    assert classify_diet(ev(text))[0] == expected


@pytest.mark.parametrize("name, text", [
    ("Big Hoops BBQ Beef", "potato starch, barbecue beef flavour (rice flour, salt)"),
    ("Prawn Cocktail Crisps", "potatoes, sunflower oil, prawn cocktail flavour (sugar, salt)"),
])
def test_names_after_a_meat_flavoring_in_the_text_are_not_meat(name, text):
    assert classify_diet(ev(text, name=name))[0] == DietClass.vegan


def test_name_meat_counts_when_text_flavor_is_a_different_animal():
    product = ev("water, salt, sugar, beef flavor (yeast extract)", name="Smoked Ham")
    assert classify_diet(product)[0] == DietClass.non_vegetarian


def test_chicken_flavor_with_real_chicken_is_still_meat():
    text = "noodles, chicken flavor (salt, chicken fat, dehydrated chicken)"
    assert classify_diet(ev(text))[0] == DietClass.non_vegetarian


def test_soy_glazed_chicken_is_still_meat():
    assert classify_diet(ev("soy glazed chicken, rice"))[0] == DietClass.non_vegetarian


@pytest.mark.parametrize("name, brand, categories", [
    ("Chicken-Less Mandarin Orange Morsels", "", []),
    ("No Pork Sausage Rolls", "", []),
    ("Smoked Ham Deli Slices", "Tofurky", []),
    ("Portabella Jerky Sweet Balsamic", "", []),
    ("Organic Roasted Seaweed Snack", "", ["en:plant-based-foods", "en:seafood", "en:seaweeds"]),
])
def test_plant_products_named_after_meat_are_vegan(name, brand, categories):
    product = ev("water, wheat gluten, soy sauce, salt", name=name, brand=brand,
                 categories=categories)
    assert classify_diet(product)[0] == DietClass.vegan


@pytest.mark.parametrize("text", [
    "dairy free cream cheese alternative (water, coconut oil, potato starch)",
    "vegan mozzarella style cheeze (water, coconut oil, potato starch)",
    "pasta, sweet potato, parmesan style cheese alternative (filtered water, coconut oil)",
    "organic pasta, organic vegan cheddar flavor seasoning (organic rice flour, salt)",
    "palm oil, sheanut butter, water, salt",
])
def test_vegan_dairy_alternatives_are_not_milk(text):
    assert Allergen.milk not in detect_allergens(ev(text))[0]
    assert classify_diet(ev(text))[0] == DietClass.vegan


@pytest.mark.parametrize("text, allergens", [
    ("oats, sugar. Made with love in a facility that also uses milk, soy, wheat.",
     {Allergen.milk, Allergen.soy, Allergen.wheat, Allergen.gluten}),
    ("rice, salt, also processes peanuts, tree nuts", {Allergen.peanuts, Allergen.tree_nuts}),
])
def test_more_facility_statements_are_traces(text, allergens):
    contains, may_contain = detect_allergens(ev(text))
    assert may_contain == allergens
    assert not contains & allergens


@pytest.mark.parametrize("text", [
    "pasta, cheese sauce. Amy's kitchen does not use any peanuts, fish or eggs.",
    "rice, salt. Contains: wheat. No: milk, egg, fish, crustacean shellfish, peanuts.",
    "rice flour, salt. Free of milk, eggs and peanuts.",
    "rice. Manufactured in a facility that does not process peanuts or eggs.",
])
def test_negation_clauses_cover_whole_lists(text):
    contains, may_contain = detect_allergens(ev(text))
    assert not (contains | may_contain) & {Allergen.peanuts, Allergen.egg, Allergen.fish}


def test_negation_clause_stops_at_contains():
    contains, _ = detect_allergens(ev("Free from artificial colors, contains milk"))
    assert Allergen.milk in contains


def test_refined_wheat_sugar_is_wheat_but_not_gluten():
    contains, _ = detect_allergens(ev("almonds, sugar, glucose syrup (from wheat), water"))
    assert Allergen.wheat in contains
    assert Allergen.gluten not in contains


def test_graham_alone_is_not_wheat_but_graham_flour_is():
    gf_crackers = ev("graham crackers (pea starch, palm oil, cane sugar)")
    assert Allergen.wheat not in detect_allergens(gf_crackers)[0]
    assert Allergen.wheat in detect_allergens(ev("graham flour, sugar"))[0]


def test_off_gluten_tag_on_oats_is_unknown_not_unsafe():
    oat_bar = ev("rolled oats, oat flour, tapioca syrup, cane sugar", allergens=["en:gluten"])
    assert Allergen.gluten not in detect_allergens(oat_bar)[0]
    v = assess(oat_bar, [Allergen.gluten], Diet.none)
    assert v.verdict == VerdictKind.unknown
    assert "Oats" in v.reason


def test_off_gluten_tag_with_gluten_free_claim_and_no_gluten_grain_is_safe():
    wraps = ev("water, tapioca flour, teff flour, millet flour", allergens=["en:gluten"],
               name="Ivory Teff Wraps, Gluten Free")
    assert assess(wraps, [Allergen.gluten], Diet.none).verdict == VerdictKind.safe


def test_off_gluten_tag_without_explanation_still_counts():
    # No oats, no claim: the tag may come from the label's allergen statement
    assert Allergen.gluten in detect_allergens(ev("rice, salt", allergens=["en:gluten"]))[0]


# --- 9. Picking evidence: store first, then Open Food Facts --------------------------------

KROGER_FULL = ev("water, salt", allergens=["Milk"], source="kroger")
KROGER_ALLERGENS_ONLY = ev(allergens=["Milk"], source="kroger")
KROGER_EMPTY = ev(source="kroger")
OFF_FULL = ev("water, salt, milk", source="off")


@pytest.mark.parametrize("candidates, expected", [
    ((KROGER_FULL, OFF_FULL), KROGER_FULL),
    ((KROGER_EMPTY, OFF_FULL), OFF_FULL),
    ((None, OFF_FULL), OFF_FULL),
    ((KROGER_ALLERGENS_ONLY, OFF_FULL), OFF_FULL),  # only an ingredient list can show "safe"
    ((KROGER_ALLERGENS_ONLY, None), KROGER_ALLERGENS_ONLY),
    ((KROGER_EMPTY, None), None),
    ((), None),
])
def test_pick_evidence_order(candidates, expected):
    assert pick_evidence(*candidates) is expected
