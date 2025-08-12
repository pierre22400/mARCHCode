"""
============================================================
Error Policy — mARCHCode (MVP-ready)
============================================================

But du module
-------------
Centraliser la logique de mapping "catégorie d'erreur" → "action suivante"
pour tout le pipeline (APPLY / RETRY / ROLLBACK).

Contrat
-------
- Input : ErrorCategory + policy_mode (ex. 'enforce' ou 'warn')
- Output : action suivante ('apply', 'retry', 'rollback')
"""

from enum import Enum


class ErrorCategory(str, Enum):
    """Typologie des erreurs détectables dans le pipeline."""
    SYNTAX = "syntax_error"
    MODULE_INCOHERENCE = "module_incoherence"
    POLICY_VIOLATION = "policy_violation"
    FATAL = "fatal"
    UNKNOWN = "unknown"


def map_error_to_next_action(category: ErrorCategory, policy_mode: str = "enforce") -> str:
    """
    Associe une catégorie d'erreur à l'action suivante recommandée.
    """
    if category == ErrorCategory.SYNTAX:
        return "retry"
    elif category == ErrorCategory.MODULE_INCOHERENCE:
        return "retry"
    elif category == ErrorCategory.POLICY_VIOLATION:
        return "rollback" if policy_mode == "enforce" else "retry"
    elif category == ErrorCategory.FATAL:
        return "rollback"
    else:
        return "retry"
