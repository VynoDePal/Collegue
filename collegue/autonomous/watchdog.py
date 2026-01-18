"""
Watchdog Autonome - Self-Healing Sentry -> GitHub
Ce script surveille Sentry et tente de corriger automatiquement les erreurs simples.

Peut être exécuté:
1. En standalone: python -m collegue.autonomous.watchdog
2. Intégré dans l'app principale via start_background_watchdog()
"""
import asyncio
import logging
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from collegue.config import settings
from collegue.core.tool_llm_manager import ToolLLMManager
from collegue.tools.sentry_monitor import SentryMonitorTool, SentryRequest
from collegue.tools.github_ops import GitHubOpsTool, GitHubRequest

try:
    from fastmcp.server.dependencies import get_http_headers
except Exception:
    get_http_headers = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("watchdog")

# Variable globale pour stocker la tâche de fond
_watchdog_task: Optional[asyncio.Task] = None


def _get_config_value(key: str, header_names: List[str] = None) -> Optional[str]:
    """
    Récupère une valeur de configuration avec fallback:
    1. Variables d'environnement (passées par l'IDE via mcp.json)
    2. Headers HTTP MCP (si disponibles)
    
    Args:
        key: Nom de la variable d'environnement (ex: SENTRY_ORG)
        header_names: Noms des headers HTTP à vérifier (ex: ['x-sentry-org'])
    """
    value = os.environ.get(key)
    if value:
        return value
    
    if get_http_headers is not None and header_names:
        try:
            headers = get_http_headers() or {}
            for header in header_names:
                if headers.get(header):
                    return headers.get(header)
        except Exception:
            pass
    
    return None


class AutoFixer:
    def __init__(self):
        self.sentry = SentryMonitorTool()
        self.github = GitHubOpsTool()
        self.llm = ToolLLMManager()
        
    def _get_sentry_org(self) -> Optional[str]:
        """Récupère l'organisation Sentry depuis env ou headers."""
        return _get_config_value(
            "SENTRY_ORG", 
            ["x-sentry-org", "x-collegue-sentry-org"]
        )
    
    def _get_github_owner(self) -> Optional[str]:
        """Récupère le propriétaire GitHub depuis env ou headers."""
        return _get_config_value(
            "GITHUB_OWNER",
            ["x-github-owner", "x-collegue-github-owner"]
        )
        
    async def run_once(self):
        """Exécute une passe de vérification et correction sur TOUS les projets."""
        logger.info("🔍 Démarrage du cycle de Self-Healing Multi-Projets...")
        
        org = self._get_sentry_org()
        if not org:
            logger.error("SENTRY_ORG non défini (ni env, ni headers MCP). Impossible de scanner.")
            return

        try:
            logger.info(f"📡 Récupération des données pour l'org: {org}")
            
            projects_resp = self.sentry._execute_core_logic(SentryRequest(
                command="list_projects",
                organization=org
            ))
            projects = projects_resp.projects or []
            
            repos_resp = self.sentry._execute_core_logic(SentryRequest(
                command="list_repos",
                organization=org
            ))
            repos = repos_resp.repos or []
            
            self.repo_map = {}
            for r in repos:
                self.repo_map[r.name] = r # ex: owner/repo
                if "/" in r.name:
                    short_name = r.name.split("/")[-1]
                    self.repo_map[short_name] = r

            logger.info(f"✅ {len(projects)} projets et {len(repos)} dépôts liés trouvés.")
            
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des données Sentry: {e}")
            return

        for project in projects:
            await self.scan_project(org, project)

    async def scan_project(self, org, project):
        """Scanne un projet spécifique."""
        logger.info(f"📂 Scan du projet: {project.slug}")
        
        try:
            sentry_response = self.sentry._execute_core_logic(SentryRequest(
                command="list_issues",
                organization=org,
                project=project.slug,
                query="is:unresolved level:error",
                limit=3  # On limite à 3 par projet pour ne pas spammer
            ))
        except Exception as e:
            logger.error(f"Erreur lecture issues projet {project.slug}: {e}")
            return

        if not sentry_response.issues:
            return

        for issue in sentry_response.issues:
            logger.info(f"🚨 [Projet: {project.slug}] Analyse issue: {issue.title} ({issue.short_id})")
            
            mapped_repo = self.repo_map.get(project.slug)
            
            repo_owner = None
            repo_name = project.slug
            
            if mapped_repo:
                if "/" in mapped_repo.name:
                    repo_owner, repo_name = mapped_repo.name.split("/", 1)
                    logger.info(f"🔗 Lien détecté via Sentry: Projet {project.slug} -> GitHub {repo_owner}/{repo_name}")
            
            if not repo_owner:
                repo_owner = org
                
            await self.attempt_fix(issue, repo_owner, repo_name)

    async def attempt_fix(self, issue, repo_owner, repo_name):
        """Tente de corriger une issue spécifique."""
        issue_id = issue.id
        
        override_owner = self._get_github_owner()
        if override_owner:
            repo_owner = override_owner
            
        if not repo_owner:
             logger.warning("Impossible de déterminer le GitHub Owner (ni env, ni headers MCP).")
             return

        try:
            events_resp = self.sentry._execute_core_logic(SentryRequest(
                command="issue_events",
                issue_id=issue_id,
                limit=1
            ))
            if not events_resp.events:
                logger.warning(f"Pas d'événements pour l'issue {issue_id}")
                return
                
            event = events_resp.events[0]
            stacktrace = event.stacktrace or "No stacktrace available"
            
        except Exception as e:
            logger.error(f"Impossible de lire les détails de l'issue {issue_id}: {e}")
            return

        logger.info("🧠 Analyse de la cause racine avec le LLM...")
        
        prompt = f"""
        Tu es un expert Python/Backend autonome.
        Analyse cette erreur Sentry et propose un correctif.
        
        ERREUR: {issue.title}
        STACKTRACE:
        {stacktrace}
        
        CONTEXTE:
        Le projet est un serveur MCP Python.
        
        TACHE:
        1. Identifie le fichier coupable (ex: collegue/app.py).
        2. Propose le code corrigé.
        3. Donne une explication courte.
        
        Réponds UNIQUEMENT au format JSON strict:
        {{
            "filepath": "chemin/vers/fichier.py",
            "explanation": "explication courte",
            "new_code": "contenu complet du fichier corrigé"
        }}
        """
        
        try:
            analysis_json = await self.llm.async_generate(prompt)
            import json
            import re
            
            match = re.search(r'```json\s*(.*?)\s*```', analysis_json, re.DOTALL)
            if match:
                json_str = match.group(1)
            else:
                json_str = analysis_json
                
            fix_data = json.loads(json_str)
            
        except Exception as e:
            logger.error(f"Echec de l'analyse LLM: {e}")
            return

        filepath = fix_data.get("filepath")
        if ".." in filepath or filepath.startswith("/"):
            logger.error(f"Chemin de fichier suspect: {filepath}")
            return

        branch_name = f"fix/sentry-{issue.short_id}"
        pr_title = f"Fix: {issue.title} (Sentry-{issue.short_id})"
        
        logger.info(f"🛠️ Application du correctif sur {filepath} (Branche: {branch_name})")
        
        try:
            self.github._execute_core_logic(GitHubRequest(
                command="create_branch",
                owner=repo_owner,
                repo=repo_name,
                branch=branch_name
            ))
            
            self.github._execute_core_logic(GitHubRequest(
                command="update_file",
                owner=repo_owner,
                repo=repo_name,
                path=filepath,
                message=f"Fix {issue.title}",
                content=fix_data["new_code"],
                branch=branch_name
            ))
            
            pr_resp = self.github._execute_core_logic(GitHubRequest(
                command="create_pr",
                owner=repo_owner,
                repo=repo_name,
                title=pr_title,
                body=f"Fix automatique généré par Collegue Watchdog.\n\nIssue: {issue.permalink}\n\nExplication:\n{fix_data['explanation']}",
                head=branch_name,
                base="main"
            ))
            
            logger.info(f"🚀 PR Créée avec succès: {pr_resp.pr.html_url}")
            
        except Exception as e:
            logger.error(f"Echec de l'opération GitHub: {e}")

async def _watchdog_loop(interval_seconds: int = 300):
    """Boucle principale du watchdog."""
    fixer = AutoFixer()
    while True:
        try:
            await fixer.run_once()
        except Exception as e:
            logger.error(f"Erreur dans le cycle watchdog: {e}")
        logger.info(f"💤 Pause de {interval_seconds // 60} minutes...")
        await asyncio.sleep(interval_seconds)


def start_background_watchdog(interval_seconds: int = 300) -> Optional[asyncio.Task]:
    """
    Démarre le watchdog en tâche de fond.
    
    Cette fonction permet d'intégrer le watchdog dans l'app principale
    pour qu'il hérite des variables d'environnement passées par l'IDE via mcp.json.
    
    Args:
        interval_seconds: Intervalle entre les cycles (défaut: 5 minutes)
        
    Returns:
        La tâche asyncio créée, ou None si déjà en cours
        
    Usage dans app.py:
        from collegue.autonomous.watchdog import start_background_watchdog
        
        @app.on_event("startup")
        async def startup():
            start_background_watchdog()
    """
    global _watchdog_task
    
    if _watchdog_task is not None and not _watchdog_task.done():
        logger.warning("Watchdog déjà en cours d'exécution")
        return None
    
    try:
        loop = asyncio.get_running_loop()
        _watchdog_task = loop.create_task(_watchdog_loop(interval_seconds))
        logger.info(f"🚀 Watchdog démarré en tâche de fond (intervalle: {interval_seconds}s)")
        return _watchdog_task
    except RuntimeError:
        logger.error("Pas de boucle asyncio active. Utilisez asyncio.run(main()) pour le mode standalone.")
        return None


def stop_background_watchdog():
    """Arrête le watchdog en cours d'exécution."""
    global _watchdog_task
    
    if _watchdog_task is not None and not _watchdog_task.done():
        _watchdog_task.cancel()
        logger.info("🛑 Watchdog arrêté")
        _watchdog_task = None


async def main():
    """Point d'entrée pour le mode standalone."""
    await _watchdog_loop(interval_seconds=300)


if __name__ == "__main__":
    asyncio.run(main())
