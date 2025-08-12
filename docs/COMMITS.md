# Commit message standard — ARCHCode

## 🎯 Objectif
Assurer une traçabilité parfaite entre :
- **PlanLine** (ce qui était prévu),
- **PatchBlock / patch_id** (ce qui a été tenté),
- **Module/agent** (qui a agi),
- **Statut** (résultat),
- et **source** (manuel, agent, rollback, brownfield).

---

## 1) Ligne de sujet (obligatoire)

Format exact :
feat(mARCH): <plan_line_id> <role> <module>

- `feat` peut être remplacé par : `fix`, `chore`, `refactor`, `docs`, `test`, `perf`, `build`, `ci`.
- `mARCH` = surface concernée (ici le MVP mARCHCode).
- `<plan_line_id>` = identifiant **exact** de la PlanLine (ex. `PL-013`).
- `<role>` = rôle/agent/acteur principal (ex. `agent_module_checker`).
- `<module>` = module ou répertoire logique (ex. `core/orchestrator`).

**Règles de forme**
- ≤ 100 caractères si possible.
- **Une ligne vide** après le sujet avant le corps.

---

## 2) Corps structuré (obligatoire)

Le corps est une **liste de paires clé: valeur** (une par ligne), dans cet ordre :

patch_id: <PB-YYYYMMDD-NNNN> # identifiant unique de PatchBlock ou rollback (RB-...)
status: <accepted|rejected|partial>
contraintes: <NF/tech majeures impactant la décision>
notes: <contexte court, références, TODO ciblés>
commit_source: <manual|agent|brownfield-migration|rollback-fix>


**Contraintes**
- UTF-8, pas d’espaces de fin de ligne.
- 1 info par ligne, sans indentation exotique.
- Pas de multi-lignes : si nécessaire, scinder en phrases courtes.

---

## 3) Exemples canoniques

**Ajout de fonctionnalité**
feat(mARCH): PL-013 agent_module_checker core/orchestrator

patch_id: PB-20250812-1234
status: accepted
contraintes: keep idempotent on PatchBlock; no change to global_status/next_action
notes: align KV with ModuleChecker: STATUS|REASONS|STRATEGY|REMEDIATION|COMMENT
commit_source: agent

**Correction de bug**
fix(mARCH): PL-021 agent_file_checker core/checkers

patch_id: PB-20250812-1259
status: partial
contraintes: must pass on Windows 3.11.6; no external io
notes: guard on empty REASONS; unit added
commit_source: manual

**Commit de gouvernance / release “green”**
chore(mARCH): PL-000 ops release

patch_id: RB-20250812-00A1
status: accepted
contraintes: rollback to last green tag
notes: see docs/ROLLBACK.md; target=green-20250812-a1b2c3d
commit_source: rollback-fix


**Migration brownfield (réécriture docstreams)**

refactor(mARCH): PL-104 agent_spec_rewriter core/spec

patch_id: PB-20250812-2077
status: accepted
contraintes: preserve file-level semantics; docstreams added; metrics blocks stable
notes: brownfield: Program B file pass 1; tests unchanged
commit_source: brownfield-migration



---

## 4) Bonnes pratiques

- **Un commit = une intention** (atomique, diff lisible).
- Toujours référencer **`<plan_line_id>`** et **`patch_id`**.
- Les changements mécaniques massifs (renames/formatage) → commit dédié `chore(mARCH)`.
- Ne pas réécrire l’historique d’une branche partagée.
- Conserver la cohérence avec les checkers (ex. normalisation KV déjà définie côté agents).

---

## 5) Validation locale minimale

Avant push :
- Lancer les tests locaux liés au module touché.
- Si ajout de logique, ajouter **au moins** un test unitaire rapide.
- Vérifier le format du message via votre template d’éditeur (si disponible).

---

## 6) Rappels d’intégration

- Les tags **green** sont créés **seulement** après CI verte sur `main` (voir `docs/BRANCHING.md`).
- Tout rollback doit produire un commit avec `commit_source: rollback-fix` et mention explicite du tag visé.
- La présence de l’archive `.archcode/archive/patch_post_commit_<sha>.tar.gz` est un prérequis aux opérations de rollback (voir `docs/ROLLBACK.md`).

---

