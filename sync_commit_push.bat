@echo off
REM ============================================================================
REM sync_to_github.bat — Push de la version locale actuelle vers GitHub
REM ============================================================================
REM Usage : À exécuter depuis le dossier racine du dépôt cloné (avec .git)
REM Objectif : Sauvegarder localement tous les fichiers puis les pousser sur GitHub
REM ============================================================================

setlocal

echo.
echo [INFO] Vérification de la présence de Git...
where git >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Git n'est pas installé ou pas accessible.
    pause
    exit /b 1
)

echo.
echo [INFO] Ajout de tous les changements...
git add .

echo.
echo [INFO] Création du commit...
git commit -m "sync: mise à jour locale envoyée vers GitHub"

echo.
echo [INFO] Envoi du commit sur la branche main de GitHub...
git push origin master:main

if errorlevel 1 (
    echo.
    echo [ERREUR] Le push a échoué. Vérifie que tu es bien connecté à GitHub.
    pause
    exit /b 1
)

echo.
echo [OK] Synchronisation terminée avec succès.
pause
endlocal



REM     pour commiter un fichier par exemple context-snapshot.yml vers github dans la console windows
REM git add .github/workflows/context-snapshot.yml
REM git commit -m "fix: déplacement du workflow context-snapshot vers .github/workflows"
REM git push origin master:main
