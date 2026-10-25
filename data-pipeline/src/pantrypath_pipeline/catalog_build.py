"""Assemble the dish catalog from the refined dishes: apply merges, enforce the cuisine
quotas, and write dish_profiles.json plus reports/catalog_summary.json.

The quotas (catalog.quotas) fix the cuisine mix, because a plain top 100 by reviews is
~89% American. When a cuisine has fewer usable dishes than its quota, the gap is NOT
filled from other cuisines (that would undo the quota): the shortfall is reported and
alerted. Below catalog.min_dishes the step fails and the previous catalog stays in place.

Pool dishes marked always_include (the golden-set dishes, too niche on Food.com to make
the popularity cut) are ranked first within their cuisine, so they always land in the
catalog and take quota slots. The LLM still decides: if it drops or merges one, we follow
it, log a warning and list the case in the summary. A merged always_include dish passes
the flag on to the dish it was merged into.
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from pantrypath_core.catalog import Catalog, DishProfile
from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import ValidationFailedError
from pantrypath_pipeline.llm import write_json_atomic, write_usage_report
from pantrypath_pipeline.refine import load_pool, refined_path, replies_from_records

log = logging.getLogger(__name__)


@dataclass
class Assembly:
    selected: list[DishProfile]
    per_cuisine: dict[str, dict[str, int]]
    shortfalls: dict[str, int]
    dropped: list[str] = field(default_factory=list)
    merged: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    # always_include dishes that did not make it as their own entry, and why
    always_include_changed: list[dict[str, str]] = field(default_factory=list)
    always_include_selected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _popularity(profile: DishProfile) -> tuple[int, int, str]:
    return (profile.review_count, profile.recipe_count, profile.dish_id)


def _absorb(target: DishProfile, name: str, aliases: Iterable[str], recipe_count: int,
            review_count: int) -> DishProfile:
    """Fold a duplicate dish into target: its names become aliases, its counts add up."""
    data = target.model_dump()
    data.update(
        aliases=[*target.aliases, name, *aliases],
        recipe_count=target.recipe_count + recipe_count,
        review_count=target.review_count + review_count,
    )
    return DishProfile.model_validate(data)


def load_refined(cfg: Config, pool_dishes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Refined records of the current pool's dishes (stale files of other dishes are ignored)."""
    records: dict[str, dict[str, Any]] = {}
    for dish in pool_dishes:
        path = refined_path(cfg, dish["dish_id"])
        if not path.is_file():
            continue
        try:
            records[dish["dish_id"]] = json.loads(path.read_text())
        except (OSError, ValueError):
            records[dish["dish_id"]] = {"error": f"unreadable refined record {path}"}
    return records


def assemble(pool_dishes: list[dict[str, Any]], records: dict[str, dict[str, Any]],
             quotas: dict[str, int]) -> Assembly:
    """Pure assembly logic: classify, merge, de-duplicate, then apply quotas."""
    asm = Assembly(selected=[], per_cuisine={}, shortfalls={})
    must = {d["dish_id"] for d in pool_dishes if d.get("always_include")}
    kept: dict[str, DishProfile] = {}
    sources: dict[str, dict[str, Any]] = {}  # dishes the LLM merged into another one

    def must_changed(dish_id: str, outcome: str) -> None:
        if dish_id in must:
            asm.always_include_changed.append({"dish_id": dish_id, "outcome": outcome})
            log.warning("always_include dish %s: %s", dish_id, outcome)

    for dish in pool_dishes:
        dish_id = dish["dish_id"]
        rec = records.get(dish_id)
        if rec is None or rec.get("error"):
            error = str(rec["error"]) if rec else "not refined (run refine-dish)"
            asm.failed.append({"dish_id": dish_id, "error": error})
            must_changed(dish_id, f"failed: {error}")
            continue
        if not rec.get("keep"):
            asm.dropped.append(dish_id)
            must_changed(dish_id, "dropped by the LLM (keep=false)")
            continue
        try:
            profile = DishProfile.model_validate(rec["profile"]) if rec.get("profile") else None
        except ValidationError as exc:
            asm.failed.append({"dish_id": dish_id, "error": f"invalid profile: {exc}"})
            must_changed(dish_id, "failed: invalid profile")
            continue
        if rec.get("merge_into"):
            sources[dish_id] = {"into": rec["merge_into"], "name": rec.get("name") or dish["name"],
                                "aliases": rec.get("aliases") or [], "profile": profile,
                                "recipes": int(dish.get("recipe_count") or 0),
                                "reviews": int(dish.get("review_count") or 0)}
        elif profile is None:
            asm.failed.append({"dish_id": dish_id, "error": "kept but has no profile"})
            must_changed(dish_id, "failed: kept but has no profile")
        else:
            kept[dish_id] = profile

    # Merge targets are always more popular, so handling the most popular sources first
    # means a target that was itself merged is already resolved.
    resolved: dict[str, str | None] = {}
    by_popularity = sorted(sources, reverse=True,
                           key=lambda i: (sources[i]["reviews"], sources[i]["recipes"], i))
    for source_id in by_popularity:
        src = sources[source_id]
        final = src["into"] if src["into"] in kept else resolved.get(src["into"])
        if final in kept:
            kept[final] = _absorb(kept[final], src["name"], src["aliases"], src["recipes"],
                                  src["reviews"])
            asm.merged.append({"dish_id": source_id, "into": final, "reason": "llm"})
            resolved[source_id] = final
            if source_id in must:
                must_changed(source_id, f"merged by the LLM into {final}")
                must.add(final)
        elif src["profile"] is not None:
            kept[source_id] = src["profile"]
            resolved[source_id] = source_id
            asm.notes.append(f"{source_id}: merge target {src['into']} unavailable; kept alone")
        else:
            asm.dropped.append(source_id)
            resolved[source_id] = None
            asm.notes.append(f"{source_id}: merge target {src['into']} unavailable; dropped")
            must_changed(source_id, f"dropped: merge target {src['into']} unavailable")

    # Two dishes the LLM gave the same name are one dish: keep the more popular one
    by_name: dict[str, list[str]] = {}
    for dish_id, profile in kept.items():
        by_name.setdefault(profile.name, []).append(dish_id)
    for ids in by_name.values():
        ids.sort(key=lambda i: _popularity(kept[i]), reverse=True)
        for loser in ids[1:]:
            p = kept.pop(loser)
            kept[ids[0]] = _absorb(kept[ids[0]], p.name, p.aliases, p.recipe_count,
                                   p.review_count)
            asm.merged.append({"dish_id": loser, "into": ids[0], "reason": "same name"})
            if loser in must:
                must_changed(loser, f"same name as {ids[0]}; merged into it")
                must.add(ids[0])

    # An alias may not be another dish's name, and belongs to one dish (the most popular)
    dish_names = {p.name for p in kept.values()}
    owner: dict[str, str] = {}
    for dish_id in sorted(kept, key=lambda i: _popularity(kept[i]), reverse=True):
        profile = kept[dish_id]
        aliases = []
        for alias in profile.aliases:
            if alias in dish_names or alias in owner:
                continue
            owner[alias] = dish_id
            aliases.append(alias)
        if aliases != profile.aliases:
            kept[dish_id] = profile.model_copy(update={"aliases": aliases})

    by_cuisine: dict[str, list[DishProfile]] = {}
    for profile in kept.values():
        by_cuisine.setdefault(profile.cuisine.value, []).append(profile)
    for cuisine in sorted(set(by_cuisine) - set(quotas)):
        asm.notes.append(f"cuisine {cuisine} has no quota; its dishes are left out")
    for cuisine, quota in quotas.items():
        group = sorted(by_cuisine.get(cuisine, []), reverse=True,
                       key=lambda p: (p.dish_id in must, _popularity(p)))
        n_must = sum(p.dish_id in must for p in group)
        if n_must > quota:  # never cut a golden dish; say so instead
            asm.notes.append(f"{cuisine}: {n_must} always_include dishes exceed the quota "
                             f"of {quota}; all kept")
            log.warning("%s: %d always_include dishes exceed the quota of %d; all kept",
                        cuisine, n_must, quota)
        chosen = group[:max(quota, n_must)]
        asm.per_cuisine[cuisine] = {"quota": quota, "available": len(group),
                                    "selected": len(chosen), "always_include": n_must}
        if len(chosen) < quota:
            asm.shortfalls[cuisine] = quota - len(chosen)
        asm.selected += chosen
    asm.always_include_selected = [p.dish_id for p in asm.selected if p.dish_id in must]
    return asm


def _llm_added(profile: DishProfile) -> float:
    return sum(i.added_by_llm for i in profile.ingredients) / len(profile.ingredients)


def run(cfg: Config) -> dict[str, Any]:
    settings = cfg["catalog"]
    pool = load_pool(cfg)
    records = load_refined(cfg, pool["dishes"])
    asm = assemble(pool["dishes"], records, settings["quotas"])
    version = datetime.now(UTC).date().isoformat()
    dishes = [p.model_copy(update={"catalog_version": version}) for p in asm.selected]

    limit = float(settings.get("max_llm_added_share", 0.3))
    heavy = [{"dish_id": p.dish_id, "llm_added_share": round(_llm_added(p), 3)}
             for p in dishes if _llm_added(p) > limit]
    n_ingredients = sum(len(p.ingredients) for p in dishes)
    summary = {
        "catalog_version": version,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dishes": len(dishes),
        "min_dishes": settings["min_dishes"],
        "pool_dishes": len(pool["dishes"]),
        "per_cuisine": asm.per_cuisine,
        "shortfalls": asm.shortfalls,
        "always_include": {"selected": asm.always_include_selected,
                           "changed_by_llm": asm.always_include_changed},
        "dropped": asm.dropped,
        "merged": asm.merged,
        "failed": asm.failed,
        "llm_added_share": round(
            sum(i.added_by_llm for p in dishes for i in p.ingredients) / n_ingredients, 3
        ) if n_ingredients else None,
        "dishes_over_llm_added_limit": heavy,
        "plain_top100_cuisine_hints": pool.get("plain_top100_cuisine_hints"),
        "notes": asm.notes,
    }
    write_json_atomic(cfg.path(settings.get("summary_report", "reports/catalog_summary.json")),
                      summary)
    usage = write_usage_report(cfg, replies_from_records(records.values()))
    for item in heavy:
        log.warning("Dish %s: %.0f%% of its ingredients came from the LLM (limit %.0f%%)",
                    item["dish_id"], 100 * item["llm_added_share"], 100 * limit)

    if asm.shortfalls:
        lines = [f"{c}: {d['selected']} of {d['quota']} (only {d['available']} usable)"
                 for c, d in asm.per_cuisine.items() if c in asm.shortfalls]
        send_alert("catalog cuisine quota shortfall",
                   "Not enough usable dishes for these cuisines; gaps are not back-filled:\n"
                   + "\n".join(lines), level="warning", cfg=cfg)

    if len(dishes) < int(settings["min_dishes"]):
        first_error = asm.failed[0]["error"] if asm.failed else "none"
        raise ValidationFailedError(
            f"Catalog has {len(dishes)} dishes, below catalog.min_dishes={settings['min_dishes']}"
            f" ({len(asm.failed)} failed to refine, first error: {first_error}; "
            f"{len(asm.dropped)} dropped; shortfalls {asm.shortfalls}). "
            "The previous dish_profiles.json was left in place."
        )

    catalog = Catalog(catalog_version=version, generated_at=datetime.now(UTC), dishes=dishes)
    write_json_atomic(cfg.path(settings["profiles_output"]), catalog.model_dump(mode="json"))
    log.info("Catalog %s: %d dishes %s", version, len(dishes),
             {c: d["selected"] for c, d in asm.per_cuisine.items()})
    return {
        "catalog_version": version,
        "dishes": len(dishes),
        "shortfalls": asm.shortfalls,
        "dropped": len(asm.dropped),
        "merged": len(asm.merged),
        "failed": len(asm.failed),
        "always_include_changed": len(asm.always_include_changed),
        "llm_calls_real": usage["totals"]["calls_real"],
        "llm_est_cost_usd": usage["totals"]["est_cost_usd"],
    }
