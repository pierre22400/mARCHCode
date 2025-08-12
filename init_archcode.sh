#!/bin/bash
set -e

echo "🔧 Initialisation de .archcode/"

ARCHCODE_DIR=".archcode"
ARCHIVE_DIR="$ARCHCODE_DIR/archive"
MEMORY_DIR="$ARCHCODE_DIR/memory"

mkdir -p "$ARCHIVE_DIR"
mkdir -p "$MEMORY_DIR"

# Fichiers YAML/JSON initiaux (créés s'ils n'existent pas déjà)
touch_if_absent() {
    local path="$1"
    local default="$2"
    if [ ! -f "$path" ]; then
        echo -e "$default" > "$path"
        echo "✅ $path créé"
    else
        echo "ℹ️  $path déjà présent, ignoré"
    fi
}

# YAMLs de base
touch_if_absent "$ARCHCODE_DIR/rollback_bundle.yaml" "# rollback_bundle — liste des retours arrière\nrollbacks: []"
touch_if_absent "$ARCHCODE_DIR/plan_validated.yaml" "# plan_validated — dernier plan prêt à exécution\nmodules: []"
touch_if_absent "$ARCHCODE_DIR/current_status.yaml" "# current_status — état du dernier run\nstatus: idle"

# JSON vide (dernière trace utile)
touch_if_absent "$ARCHCODE_DIR/last_pb.json" "{}"

# Mémoire (préremplissage possible plus tard)
touch_if_absent "$MEMORY_DIR/memory_index.yaml" "# mémoire\nindex: []"
touch_if_absent "$MEMORY_DIR/memory_cache.json" "{}"

echo "✅ .archcode/ initialisé proprement"

