# 📁 Dossier `.archcode/` — Conventions internes ARCHCode

Ce dossier est réservé à l’écosystème **ARCHCode** pour stocker les artefacts internes liés à l’exécution, la validation, l’archivage et le rollback du code généré.

---

## 📂 Arborescence

.archcode/
├── archive/ → Archives reproductibles post-commit (rollback)
├── checks/ → Rapports de validation des checkers (file/module)
├── logs/ → Journaux bruts des runs/dry-run/CI
├── runs/ → Snapshots complets d’exécutions (dry-run ou run)


---

## 📌 Description des sous-dossiers

### `archive/`
Contient les **archives `.tar.gz`** des commits validés (`green`). Elles sont utilisées pour restaurer un état stable lors d’un rollback.

- Format : `patch_post_commit_<sha>.tar.gz`
- Associé à un tag `green-<date>-<shortsha>`
- Contient un `metadata.yaml` ou `metadata.json` embarqué

### `checks/`
Contient les rapports des **checkers** (file checker, module checker, etc.).  
Peut inclure les résumés, erreurs ou logs d’analyse.

- Format possible : `check_<plan_line_id>.json` ou `.txt`

### `logs/`
Contient tous les **journaux bruts** liés aux exécutions du pipeline (run, dry-run, erreurs, CI…).

- Format recommandé : `run_<timestamp>.log` ou `dryrun_<timestamp>.log`

### `runs/`
Contient les **snapshots complets** des exécutions mARCHCode (fichiers générés, PatchBlocks, décisions, etc.).

- Format : `.arch_runs/<timestamp>/...`
- Structure miroir du pipeline

---

## 🔒 Ignoré dans Git ?
Non : ce dossier est **inclus** dans le suivi Git (grâce à `.gitkeep`)  
Cependant, certains fichiers peuvent être ignorés dans `.gitignore` (ex. `*.log`, `*.tmp`).

---

## 📜 Liens de référence

- [docs/BRANCHING.md](../docs/BRANCHING.md)
- [docs/ROLLBACK.md](../docs/ROLLBACK.md)
- [docs/COMMITS.md](../docs/COMMITS.md)

---

## 🧠 Bonnes pratiques

- Ne jamais modifier ce dossier manuellement (sauf pour consulter les artefacts)
- Ne jamais supprimer une archive ou un log sans en référer au processus ARCHCode
- Toujours passer par `scripts/green_tag.py` ou les commandes CLI pour interagir avec `.archcode/`

---

ARCHCode est un système traçable. Ce dossier est son **registre local de confiance**.

