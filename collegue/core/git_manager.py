import git
import os
import logging
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

class GitManager:
    """
    Gestionnaire pour les interactions avec le dépôt Git local.
    """
    def __init__(self, repo_path: str = "."):
        self.repo_path = os.path.abspath(repo_path)
        try:
            self.repo = git.Repo(self.repo_path, search_parent_directories=True)
            self.working_dir = self.repo.working_dir
            logger.info(f"GitManager initialisé sur {self.working_dir}")
        except git.InvalidGitRepositoryError:
            logger.warning(f"Aucun dépôt Git trouvé dans {self.repo_path}")
            self.repo = None
            self.working_dir = None

    def is_available(self) -> bool:
        """Vérifie si un dépôt Git est actif."""
        return self.repo is not None

    def get_status(self) -> Dict[str, Any]:
        """Récupère l'état courant du dépôt (statut court)."""
        if not self.repo:
            return {"error": "Not a git repository", "available": False}
        
        try:
            active_branch = self.repo.active_branch.name
        except TypeError:
            active_branch = "DETACHED_HEAD"

        return {
            "available": True,
            "path": self.working_dir,
            "branch": active_branch,
            "is_dirty": self.repo.is_dirty(),
            "untracked_files": self.repo.untracked_files,
            "changed_files": [item.a_path for item in self.repo.index.diff(None)],
            "staged_files": [item.a_path for item in self.repo.index.diff("HEAD")],
            "last_commit": self._get_last_commit_info()
        }

    def _get_last_commit_info(self) -> Optional[Dict[str, Any]]:
        """Récupère les infos du dernier commit."""
        try:
            head_commit = self.repo.head.commit
            return {
                "hexsha": head_commit.hexsha,
                "message": head_commit.message.strip(),
                "author": head_commit.author.name,
                "date": datetime.fromtimestamp(head_commit.committed_date).isoformat()
            }
        except ValueError:
            return None

    def get_diff(self, file_path: Optional[str] = None) -> str:
        """Récupère le diff des modifications non stagées."""
        if not self.repo:
            return ""
        
        if file_path:
            return self.repo.git.diff(file_path)
        return self.repo.git.diff()

    def get_log(self, max_count: int = 10) -> List[Dict[str, Any]]:
        """Récupère l'historique des commits."""
        if not self.repo:
            return []
        
        commits = []
        for commit in self.repo.iter_commits(max_count=max_count):
            commits.append({
                "hexsha": commit.hexsha,
                "message": commit.message.strip(),
                "author": commit.author.name,
                "date": datetime.fromtimestamp(commit.committed_date).isoformat()
            })
        return commits

    def checkout_branch(self, branch_name: str, create: bool = False):
        """Change de branche ou en crée une nouvelle."""
        if not self.repo:
            raise RuntimeError("Not a git repository")
        
        if create:
            current = self.repo.create_head(branch_name)
            current.checkout()
        else:
            self.repo.git.checkout(branch_name)

    def commit(self, message: str, files: List[str] = None):
        """Crée un commit avec les fichiers spécifiés (ou tous si None)."""
        if not self.repo:
            raise RuntimeError("Not a git repository")
        
        if files:
            self.repo.index.add(files)
        else:
            # Stage all changes (tracked and untracked)
            self.repo.git.add(A=True)
            
        self.repo.index.commit(message)
        logger.info(f"Commit créé: {message}")

    def create_pr_description(self, base_branch: str = "main") -> str:
        """Génère une description de PR basée sur les commits depuis base_branch."""
        if not self.repo:
            return ""
            
        # Logique simplifiée pour récupérer les commits distincts
        try:
            commits = list(self.repo.iter_commits(f"{base_branch}..HEAD"))
            description = "# PR Description\n\n## Changes\n"
            for c in commits:
                description += f"- {c.message.strip()}\n"
            return description
        except git.Exc:
            return "Unable to generate PR description (base branch not found?)"
