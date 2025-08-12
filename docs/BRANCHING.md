# Branching model — ARCHCode

- **Intégration stable** : `main`
  - Doit rester **green** (build + tests OK).

- **Branches de self-dev** : `archcode-self/<topic>`
  - `<topic>` court, en kebab-case (ex. `agent-module-checker-v2`).

- **Tags “green”** : `green-<YYYYMMDD>-<shortsha>`
  - Utilisés pour marquer un commit vert sur `main`.
---

## Créer une branche de self-dev

```bash
git checkout -b archcode-self/<topic>
Remplace <topic> par un nom court décrivant le chantier.
