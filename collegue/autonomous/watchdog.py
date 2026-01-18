"""
Watchdog Autonome - Self-Healing Sentry -> GitHub
Ce script surveille Sentry et tente de corriger automatiquement les erreurs simples.
"""
import asyncio
import logging
import os
import sys
from typing import List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from collegue.config import settings
from collegue.core.tool_llm_manager import ToolLLMManager
from collegue.tools.sentry_monitor import SentryMonitorTool, SentryRequest
from collegue.tools.github_ops import GitHubOpsTool, GitHubRequest

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("watchdog")

class AutoFixer:
    def __init__(self):
        self.sentry = SentryMonitorTool()
        self.github = GitHubOpsTool()
        self.llm = ToolLLMManager()
        
    async def run_once(self):
        """Exécute une passe de vérification et correction sur TOUS les projets."""
        logger.info("🔍 Démarrage du cycle de Self-Healing Multi-Projets...")
        
        org = os.environ.get("SENTRY_ORG")
        if not org:
            logger.error("SENTRY_ORG non défini. Impossible de scanner l'organisation.")
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
        
        if os.environ.get("GITHUB_OWNER"):
            repo_owner = os.environ.get("GITHUB_OWNER")
            
        if not repo_owner:
             logger.warning("Impossible de déterminer le GitHub Owner.")
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

async def main():
    fixer = AutoFixer()
    while True:
        await fixer.run_once()
        logger.info("💤 Pause de 5 minutes...")
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
