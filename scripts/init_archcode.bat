@echo off
REM ============================================================================
REM Initialisation du dossier .archcode/ pour mARCHCode
REM Crée les sous-dossiers nécessaires pour archivage et rollback
REM A placer dans : scripts/init_archcode.bat
REM ============================================================================

echo [INFO] Initialisation de .archcode...

REM Crée les sous-dossiers
mkdir .archcode\archive 2>nul
mkdir .archcode\checks 2>nul
mkdir .archcode\logs 2>nul
mkdir .archcode\runs 2>nul

REM Placeholders
echo # Archives post-commit seront placées ici. > .archcode\archive\.gitkeep
echo # Rapports de validation checker. > .archcode\checks\.gitkeep
echo # Journaux d’exécution mARCHCode (dry-run, run, etc.) > .archcode\logs\.gitkeep
echo # Snapshots complets de run > .archcode\runs\.gitkeep

REM Confirme
echo [OK] Dossier .archcode initialisé proprement.
exit /b 0

