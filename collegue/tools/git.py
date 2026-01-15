"""
Git Tool - Outil pour interagir avec Git
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from .base import BaseTool, ToolError
from ..core.git_manager import GitManager

class GitRequest(BaseModel):
    """Modèle de requête pour les opérations Git."""
    command: str = Field(..., description="Commande à exécuter: status, diff, log, commit, checkout, pr_desc")
    args: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Arguments spécifiques à la commande")
    # Pour commit: message, files
    # Pour checkout: branch, create (bool)
    # Pour log: max_count
    # Pour pr_desc: base_branch

class GitResponse(BaseModel):
    """Modèle de réponse pour les opérations Git."""
    command: str
    output: Any
    success: bool
    error: Optional[str] = None

class GitTool(BaseTool):
    """
    Outil permettant d'effectuer des opérations Git sur le dépôt local.
    Permet de voir le statut, les diffs, l'historique, de commiter et de changer de branche.
    """

    def get_name(self) -> str:
        return "git_ops"

    def get_description(self) -> str:
        return "Effectue des opérations Git (status, diff, log, commit, checkout, PR description)"

    def get_request_model(self) -> type[BaseModel]:
        return GitRequest

    def get_response_model(self) -> type[BaseModel]:
        return GitResponse

    def is_long_running(self) -> bool:
        """L'outil Git peut nécessiter des appels LLM (commit auto) ou prendre du temps."""
        return True

    def _execute_core_logic(self, request: GitRequest, **kwargs) -> GitResponse:
        """Implémentation synchrone requise par BaseTool (non utilisée en mode async)."""
        # Pour les outils long-running, cette méthode ne devrait pas être appelée par le framework FastMCP
        # si task=True est configuré correctement.
        raise ToolError("GitTool doit être exécuté de manière asynchrone (appels LLM potentiels)")

    async def _execute_core_logic_async(self, request: GitRequest, **kwargs) -> GitResponse:
        git_manager: GitManager = self.app_state.get("git_manager")
        if not git_manager or not git_manager.is_available():
            raise ToolError("GitManager non disponible ou aucun dépôt Git actif")

        ctx = kwargs.get('ctx')
        # Récupération du LLM Manager injecté par FastMCP via kwargs
        llm_manager = kwargs.get('llm_manager') or self.llm_manager
        
        cmd = request.command.lower()
        args = request.args or {}

        try:
            result = None
            if cmd == "status":
                result = git_manager.get_status()
            
            elif cmd == "diff":
                file_path = args.get("file_path")
                result = git_manager.get_diff(file_path)
            
            elif cmd == "log":
                max_count = args.get("max_count", 10)
                result = git_manager.get_log(max_count)
            
            elif cmd == "commit":
                message = args.get("message")
                files = args.get("files")
                
                # Génération automatique du message si absent
                if not message:
                    if files:
                        diff_context = git_manager.get_diff() 
                    else:
                        diff_context = git_manager.get_diff()
                    
                    if not diff_context:
                        raise ToolError("Rien à committer (aucun changement détecté)")

                    prompt = f"""Génère un message de commit Git concis et conventionnel (Conventional Commits) pour les changements suivants.
Le message doit être en anglais, format: <type>(<scope>): <description>

Diff:
{diff_context[:3000]}  # Tronqué si trop long
"""
                    # Appel LLM via llm_manager (Server-Side)
                    if not llm_manager:
                         raise ToolError("LLM Manager non configuré, impossible de générer le message de commit")

                    message = await llm_manager.async_generate(
                        prompt=prompt,
                        system_prompt="Tu es un expert Git. Génère uniquement le message de commit, sans guillemets ni explications."
                    )
                    message = message.strip().strip('"').strip("'")
                    
                git_manager.commit(message, files)
                result = f"Commit effectué: {message}"
            
            elif cmd == "checkout":
                branch = args.get("branch")
                if not branch:
                    raise ToolError("Nom de branche requis")
                create = args.get("create", False)
                git_manager.checkout_branch(branch, create)
                result = f"Basculé sur la branche {branch}"
            
            elif cmd == "pr_desc":
                base_branch = args.get("base_branch", "main")
                result = git_manager.create_pr_description(base_branch)
            
            else:
                raise ToolError(f"Commande Git inconnue: {cmd}")

            return GitResponse(
                command=cmd,
                output=result,
                success=True
            )

        except Exception as e:
            return GitResponse(
                command=cmd,
                output=None,
                success=False,
                error=str(e)
            )
