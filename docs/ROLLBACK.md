# Rollback “green” — ARCHCode

## 🎯 Objectif
Revenir à un état **stable et validé** (`green`) en restaurant :
- Le code (commit SHA précis)
- Les artefacts d’archive associés

## 1) Définition d’un état “green”
Un commit est considéré **green** si :
1. Le build et **tous** les tests requis sont passés sur `main`.
2. Une archive post-commit existe :

```
.archcode/archive/patch_post_commit_<sha>.tar.gz
```
3. Un tag `green-<YYYYMMDD>-<shortsha>` est présent ou recréable.

---

## 2) Procédure standard — Retour au dernier green

### 🔎 Identifier le dernier commit green

```bash
git fetch --tags
git tag -l "green-*" --sort=-creatordate | head -n 1

TARGET_TAG=$(git tag -l "green-*" --sort=-creatordate | head -n 1)
TARGET_SHA=$(git rev-list -n 1 "$TARGET_TAG")
echo "$TARGET_TAG -> $TARGET_SHA"


ARCHIVE=".archcode/archive/patch_post_commit_${TARGET_SHA}.tar.gz"
test -f "$ARCHIVE" || { echo "Archive manquante: $ARCHIVE"; exit 2; }


git checkout "$TARGET_SHA"
tar -xzf "$ARCHIVE" -C .

Option A — Merge de rollback :
git checkout main
git merge --no-ff "$TARGET_SHA" -m "rollback: to ${TARGET_TAG}"
git push origin main

Option B — Reset forcé (exceptionnel) :
git checkout main
git reset --hard "$TARGET_SHA"
git push --force-with-lease origin main

SHORTSHA=$(git rev-parse --short HEAD)
DATE=$(date -u +%Y%m%d)
git tag -a "green-${DATE}-${SHORTSHA}" -m "green build ${DATE} (${SHORTSHA})"
git push origin "green-${DATE}-${SHORTSHA}"
