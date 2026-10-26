"""Catalogue-only recommendations and safety checks (contraindications, allergies, interactions)."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import rules
from app.models import Assessment, CatalogueItem, Client, Visit
from app.services.analysis import CONCERN_LABELS
from app.services.clients import age_of

STRONG_ACTIVES = {"retinol", "glycolic", "salicylic acid", "ascorbic acid"}


def client_conditions(client: Client, assessment: Assessment | None) -> set[str]:
    tags: set[str] = set()
    age = age_of(client)
    if age is not None and age < 18:
        tags.add("under_18")
    if assessment:
        f = assessment.flags or {}
        if f.get("pregnancy"):
            tags.add("pregnancy")
        if f.get("breastfeeding"):
            tags.add("breastfeeding")
        tags.update(f.get("medication_tags", []))
        tags.update(f.get("allergy_tags", []))
        if assessment.sensitive:
            tags.add("sensitive_skin")
    return tags


def safety_check(items: list[dict], catalogue: dict[str, CatalogueItem], conditions: set[str],
                 allergy_text: str | None) -> tuple[list[dict], list[dict], list[str]]:
    """Return (kept, excluded, warnings). Each excluded entry carries the reason."""
    kept, excluded, warnings = [], [], []
    allergy_text = (allergy_text or "").lower()
    for it in items:
        cat = catalogue[it["catalogue_id"]]
        hits = sorted(set(cat.contraindications) & conditions)
        ingredient_hits = [ing for ing in (cat.ingredients or []) if allergy_text and allergy_text != "none" and ing.lower() in allergy_text]
        if "fragrance_allergy" in conditions and "fragrance" in (cat.ingredients or []):
            ingredient_hits.append("fragrance")
        if hits or ingredient_hits:
            reason = []
            if hits:
                reason.append("contraindicated: " + ", ".join(h.replace("_", " ") for h in hits))
            if ingredient_hits:
                reason.append("allergy to " + ", ".join(sorted(set(ingredient_hits))))
            excluded.append({"catalogue_id": cat.id, "name": cat.name, "reason": "; ".join(reason)})
        else:
            kept.append(it)
    actives = [catalogue[i["catalogue_id"]] for i in kept
               if STRONG_ACTIVES & {x.lower() for x in (catalogue[i["catalogue_id"]].ingredients or [])}]
    if len(actives) >= 2:
        warnings.append("Several strong actives (" + ", ".join(a.name for a in actives)
                        + "): introduce one at a time and avoid using them on the same night.")
    if any(catalogue[i["catalogue_id"]].kind == "service" and "photosensitising" in catalogue[i["catalogue_id"]].contraindications for i in kept):
        warnings.append("Strict daily sunscreen is required alongside peels or laser treatments.")
    return kept, excluded, warnings


async def load_catalogue(db: AsyncSession) -> dict[str, CatalogueItem]:
    rows = (await db.execute(select(CatalogueItem).where(CatalogueItem.active.is_(True)))).scalars().all()
    return {r.id: r for r in rows}


async def recommend(db: AsyncSession, visit: Visit, client: Client, assessment: Assessment | None,
                    diagnosis: dict) -> dict:
    cfg = rules()["recommendation"]
    catalogue = await load_catalogue(db)
    skin_type = assessment.skin_type if assessment else None
    raised = list(visit.concerns or [])
    if assessment:
        raised += [c for c in (assessment.flags or {}).get("questionnaire_concerns", []) if c not in raised]

    # Rank concerns: those the user raised first, then by severity.
    ranked = sorted(
        {c for c in diagnosis if diagnosis[c]["score"] >= cfg["finding_threshold"]} | set(raised),
        key=lambda c: (c not in raised, -diagnosis.get(c, {"score": 0})["score"]),
    )
    age = age_of(client)
    candidates: list[dict] = []
    chosen: set[str] = set()

    def add(item: CatalogueItem, concern: str | None, why: str) -> None:
        if item.id in chosen:
            return
        chosen.add(item.id)
        candidates.append({
            "catalogue_id": item.id, "kind": item.kind, "name": item.name,
            "sessions": item.default_sessions if item.kind != "product" else None,
            "instructions": item.usage or None, "concern": concern, "reason": why,
        })

    def suits(item: CatalogueItem) -> bool:
        return not item.skin_types or skin_type is None or skin_type in item.skin_types

    services = [i for i in catalogue.values() if i.kind == "service" and suits(i)]
    products = [i for i in catalogue.values() if i.kind == "product" and suits(i)]
    n_services = n_products = 0
    for concern in ranked:
        score = diagnosis.get(concern, {}).get("score")
        label = CONCERN_LABELS.get(concern, concern.replace("_", " "))
        sev = f" (score {score:.0f}/100)" if score is not None else ""
        if n_services < cfg["max_services"]:
            svc = next((s for s in sorted(services, key=lambda s: len(s.concerns)) if concern in s.concerns and s.id not in chosen), None)
            if svc:
                add(svc, concern, f"Targets {label}{sev}.")
                n_services += 1
        for prd in sorted(products, key=lambda p: (len(p.concerns), p.price_inr)):
            if n_products >= cfg["max_products"] - len(cfg["always_include"]):
                break
            if concern in prd.concerns and prd.id not in chosen and prd.id not in cfg["always_include"]:
                add(prd, concern, f"Daily care for {label}.")
                n_products += 1
                break
    for trig in cfg["doctor_service_triggers"]:
        sc = diagnosis.get(trig["concern"], {}).get("score", 0)
        if sc >= trig["min_severity"] and (age or 0) >= trig.get("min_age", 0) and trig["item"] in catalogue:
            add(catalogue[trig["item"]], trig["concern"], f"Score {sc:.0f}/100 is high enough to need a doctor's assessment.")
    for pid in cfg["always_include"]:
        if pid in catalogue:
            add(catalogue[pid], None, "Daily sun protection supports every treatment and prevents new damage.")

    conditions = client_conditions(client, assessment)
    allergy_text = (assessment.flags or {}).get("allergy_text") if assessment else None
    kept, excluded, warnings = safety_check(candidates, catalogue, conditions, allergy_text)
    return {"items": kept, "excluded": excluded, "warnings": warnings, "skin_type": skin_type,
            "conditions": sorted(conditions), "ranked_concerns": ranked}
