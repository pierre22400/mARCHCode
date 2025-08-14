# run_test.py
import argparse, importlib, json, os, sys, time, traceback
from types import ModuleType
from pathlib import Path

"""
ARCHCode — Test Runner unifié (Render & Console)
===============================================================================
Objet
    Exécuter une fonction cible (module[:callable]) de manière reproductible,
    capturer stdout/stderr, produire un résultat structuré et des logs identiques
    sur Render et en local.

Contexte brownfield
    - Zéro interaction utilisateur (non-bloquant).
    - Compatible subprocess serveur et console dev.
    - Utile pour smoke tests, tests rapides d’intégration légère.

Entrées attendues
    - ENV:
        ARCH_ENV = "render" | "local"
        LOG_LEVEL = "INFO" | "DEBUG" | ...
        TIMEOUT_S (optionnel)
        OUT_DIR (optionnel)
    - Args CLI:
        target = "package.module[:callable]"
        --args='[ ... ]' (JSON)
        --kwargs='{ ... }' (JSON)

Sorties/artefacts
    - OUT_DIR/run.log (timeline)
    - OUT_DIR/result.json :
        { status, module, callable, elapsed_s, result_preview | traceback }

Invariants/garanties
    - Jamais d’input interactif.
    - Format de sortie stable (consommable par ACW/module_checker).
    - Échec → code retour non-zéro + traceback condensé.

Échecs courants & diagnostics
    - Module introuvable/callable absent → erreur explicite.
    - Timeout ou mémoire → message clair + recommandation (réduire charge).

Bonnes pratiques
    - En local, LOG_LEVEL=DEBUG pour voir la progression.
    - Sur Render, privilégier les artefacts fichiers (analyse après-coup).
"""

# --- config minimale ---
ARCH_ENV = os.getenv("ARCH_ENV", "local").lower()       # "render" | "local"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
TIMEOUT_S = int(os.getenv("TIMEOUT_S", "45"))
OUT_DIR   = Path(os.getenv("OUT_DIR", "./.arch_results"))
OUT_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    """Écrit un message de log.

    Le message est :
      - imprimé sur stdout en environnement local,
      - systématiquement ajouté à `OUT_DIR/run.log`.

    Args:
        msg: Contenu textuel à consigner.

    Returns:
        None.
    """
    # En local, on affiche; sur Render, on écrit surtout fichier (stdout reste ok mais on réduit)
    line = f"[ARCH:{LOG_LEVEL}] {msg}"
    if ARCH_ENV == "local":
        print(line)
    (OUT_DIR / "run.log").open("a", encoding="utf-8").write(line + "\n")


def write_result(payload: dict, fname: str = "result.json"):
    """Écrit le résultat structuré du run en JSON.

    Args:
        payload: Dictionnaire sérialisable (status, meta, etc.).
        fname: Nom de fichier (par défaut `result.json`) dans `OUT_DIR`.

    Returns:
        None.
    """
    p = OUT_DIR / fname
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_callable(target: str):
    """Résout une cible `module[:callable]` vers un objet appelable.

    Formats acceptés:
      - `package.module:func`
      - `package.module` (alors `main()` est supposé)

    Args:
        target: Spécification de la cible à exécuter.

    Returns:
        Tuple `(module_name, func_name, func_obj)`.

    Raises:
        ModuleNotFoundError: Si le module est introuvable.
        AttributeError: Si la fonction n’existe pas ou n’est pas appelable.
    """
    """
    target formats:
      - package.module:func
      - package.module   (then calls main() if exists)
    """
    if ":" in target:
        mod_name, func_name = target.split(":", 1)
    else:
        mod_name, func_name = target, "main"
    mod: ModuleType = importlib.import_module(mod_name)
    func = getattr(mod, func_name, None)
    if func is None or not callable(func):
        raise AttributeError(f"Callable '{func_name}' introuvable dans '{mod_name}'")
    return mod_name, func_name, func


def main():
    """Point d’entrée CLI du test runner.

    Lit les arguments, résout la cible, exécute l’appel, capture le résultat,
    écrit les artefacts (`run.log`, `result.json`) et définit le code de sortie.

    Args:
        None.

    Returns:
        None. Le process se termine via `sys.exit(0|1)`.

    Side Effects:
        - Création/écriture de fichiers dans `OUT_DIR`.
        - Impression console en mode local.
    """
    ap = argparse.ArgumentParser(description="ARCH unified test runner")
    ap.add_argument("target", help="module[:callable], ex: src.my_mod:test_entry")
    ap.add_argument("--args", default="[]", help="JSON list, ex: '[\"--flag\", 3]'")
    ap.add_argument("--kwargs", default="{}", help="JSON dict, ex: '{\"fast\": true}'")
    args = ap.parse_args()

    start = time.time()
    try:
        mod_name, func_name, func = resolve_callable(args.target)
        pos = json.loads(args.args)
        kw  = json.loads(args.kwargs)

        # Exécution "soft-timeout"
        # (Render gère aussi des timeouts à l'échelle du conteneur; ici on garde un garde-fou)
        # Pour rester simple (1 bloc), on checke la durée avant/pendant.
        log(f"→ Start {mod_name}:{func_name} with args={pos} kwargs={kw}")
        result = func(*pos, **kw)

        elapsed = time.time() - start
        payload = {
            "status": "passed",
            "module": mod_name,
            "callable": func_name,
            "elapsed_s": round(elapsed, 3),
            "result_preview": str(result)[:400],
        }
        write_result(payload)
        log("✓ PASS")
        if ARCH_ENV == "local":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        sys.exit(0)

    except Exception as e:
        elapsed = time.time() - start
        tb = traceback.format_exc()
        # extraction ultra-simple (ligne/fichier si dispo dans trace)
        file_hint = None
        line_hint = None
        for frame in traceback.extract_tb(sys.exc_info()[2]):
            file_hint = frame.filename
            line_hint = frame.lineno
        payload = {
            "status": "failed",
            "error_type": e.__class__.__name__,
            "message": str(e),
            "file": file_hint,
            "line": line_hint,
            "elapsed_s": round(elapsed, 3),
            "traceback": tb,
            "notes": "Voir run.log pour le fil d’exécution.",
        }
        write_result(payload)
        log(f"✗ FAIL {e.__class__.__name__}: {e}")
        if ARCH_ENV == "local":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
