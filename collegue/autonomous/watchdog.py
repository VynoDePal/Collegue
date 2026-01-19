"""
Watchdog Autonome - Self-Healing Sentry -> GitHub
Ce script surveille Sentry et tente de corriger automatiquement les erreurs simples.

Peut être exécuté:
1. En standalone: python -m collegue.autonomous.watchdog
2. Intégré dans l'app principale via start_background_watchdog()
"""
import asyncio
import difflib
import logging
import os
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from collegue.config import settings
from collegue.core.tool_llm_manager import ToolLLMManager
from collegue.tools.sentry_monitor import SentryMonitorTool, SentryRequest
from collegue.tools.github_ops import GitHubOpsTool, GitHubRequest
from collegue.autonomous.config_registry import get_config_registry, UserConfig
from collegue.autonomous.context_pack import ContextPackBuilder, ContextPack

try:
    from fastmcp.server.dependencies import get_http_headers
except Exception:
    get_http_headers = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("watchdog")

# Variable globale pour stocker la tâche de fond
_watchdog_task: Optional[asyncio.Task] = None

# Set pour tracker les issues déjà traitées (évite les doublons)
_processed_issues: set = set()


def _fuzzy_find_match(search: str, content: str, threshold: float = 0.6) -> Tuple[Optional[str], float]:
    """
    Trouve le meilleur match fuzzy pour 'search' dans 'content'.
    Utilise difflib.SequenceMatcher (stratégie Aider/RooCode).
    
    Returns:
        (best_match, score) où best_match est le texte exact trouvé dans content,
        ou (None, 0) si aucun match au-dessus du threshold.
    """
    search_lines = search.strip().split('\n')
    content_lines = content.split('\n')
    search_len = len(search_lines)
    
    if search_len == 0 or len(content_lines) == 0:
        return None, 0.0
    
    best_match = None
    best_score = 0.0
    best_start = -1
    
    # Sliding window sur le contenu
    for i in range(len(content_lines) - search_len + 1):
        window = content_lines[i:i + search_len]
        window_text = '\n'.join(window)
        
        # Score avec SequenceMatcher
        ratio = difflib.SequenceMatcher(None, search.strip(), window_text.strip()).ratio()
        
        if ratio > best_score:
            best_score = ratio
            best_start = i
            best_match = window_text
    
    # Essayer aussi avec whitespace normalisé si le score est faible
    if best_score < threshold:
        normalized_search = ' '.join(search.split())
        for i in range(len(content_lines) - search_len + 1):
            window = content_lines[i:i + search_len]
            window_text = '\n'.join(window)
            normalized_window = ' '.join(window_text.split())
            
            ratio = difflib.SequenceMatcher(None, normalized_search, normalized_window).ratio()
            if ratio > best_score:
                best_score = ratio
                best_start = i
                best_match = window_text
    
    if best_score >= threshold:
        return best_match, best_score
    
    return None, best_score


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
    def __init__(self, user_config: Optional[UserConfig] = None):
        self.sentry = SentryMonitorTool()
        self.github = GitHubOpsTool()
        self.llm = ToolLLMManager()
        self.user_config = user_config
        
    def _get_sentry_org(self) -> Optional[str]:
        """Récupère l'organisation Sentry depuis config, env ou headers."""
        if self.user_config:
            return self.user_config.sentry_org
        return _get_config_value(
            "SENTRY_ORG", 
            ["x-sentry-org", "x-collegue-sentry-org"]
        )
    
    def _get_sentry_token(self) -> Optional[str]:
        """Récupère le token Sentry depuis config ou env."""
        if self.user_config and self.user_config.sentry_token:
            return self.user_config.sentry_token
        return os.environ.get("SENTRY_AUTH_TOKEN")
    
    def _get_github_token(self) -> Optional[str]:
        """Récupère le token GitHub depuis config ou env."""
        if self.user_config and self.user_config.github_token:
            return self.user_config.github_token
        return os.environ.get("GITHUB_TOKEN")
    
    def _get_github_owner(self) -> Optional[str]:
        """Récupère le propriétaire GitHub depuis config, env ou headers."""
        if self.user_config and self.user_config.github_owner:
            return self.user_config.github_owner
        return _get_config_value(
            "GITHUB_OWNER",
            ["x-github-owner", "x-collegue-github-owner"]
        )
    
    def _get_github_repo(self) -> Optional[str]:
        """Récupère le nom du repo GitHub depuis config ou env."""
        if self.user_config and self.user_config.github_repo:
            return self.user_config.github_repo
        return os.environ.get("GITHUB_REPO")
        
    async def run_once(self):
        """Exécute une passe de vérification et correction sur TOUS les projets."""
        org = self._get_sentry_org()
        token = self._get_sentry_token()
        
        if not org:
            logger.warning("Configuration sans SENTRY_ORG, ignorée.")
            return
            
        logger.info(f"🔍 Scan de l'organisation: {org}")

        try:
            projects_resp = self.sentry._execute_core_logic(SentryRequest(
                command="list_projects",
                organization=org,
                token=token
            ))
            projects = projects_resp.projects or []
            
            repos_resp = self.sentry._execute_core_logic(SentryRequest(
                command="list_repos",
                organization=org,
                token=token
            ))
            repos = repos_resp.repos or []
            
            self.repo_map = {}
            for r in repos:
                self.repo_map[r.name] = r
                if "/" in r.name:
                    short_name = r.name.split("/")[-1]
                    self.repo_map[short_name] = r

            logger.info(f"✅ {len(projects)} projets et {len(repos)} dépôts liés trouvés.")
            
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des données Sentry: {e}")
            return

        for project in projects:
            await self.scan_project(org, project, token)

    async def scan_project(self, org, project, token: Optional[str] = None):
        """Scanne un projet spécifique."""
        logger.info(f"📂 Scan du projet: {project.slug} (id: {project.id})")
        
        try:
            # Utiliser project.id car l'API Sentry attend un ID numérique pour list_issues
            sentry_response = self.sentry._execute_core_logic(SentryRequest(
                command="list_issues",
                organization=org,
                project=project.id,
                query="is:unresolved level:error",
                limit=3,
                token=token
            ))
        except Exception as e:
            logger.error(f"Erreur lecture issues projet {project.slug}: {e}")
            return

        if not sentry_response.issues:
            return

        for issue in sentry_response.issues:
            logger.info(f"🚨 [Projet: {project.slug}] Analyse issue: {issue.title} ({issue.short_id})")
            
            # Priorité: 1) Config GITHUB_REPO, 2) Mapping Sentry, 3) Slug du projet
            repo_owner = self._get_github_owner()
            repo_name = self._get_github_repo()
            
            # Si pas de repo configuré, essayer le mapping Sentry
            if not repo_name:
                mapped_repo = self.repo_map.get(project.slug)
                if mapped_repo and "/" in mapped_repo.name:
                    repo_owner, repo_name = mapped_repo.name.split("/", 1)
                    logger.info(f"🔗 Lien détecté via Sentry: Projet {project.slug} -> GitHub {repo_owner}/{repo_name}")
                else:
                    repo_name = project.slug
            
            if not repo_owner:
                repo_owner = org
            
            logger.info(f"📍 Repo cible: {repo_owner}/{repo_name}")
            await self.attempt_fix(issue, repo_owner, repo_name, org, token)

    async def attempt_fix(self, issue, repo_owner, repo_name, org: str, sentry_token: Optional[str] = None):
        """Tente de corriger une issue spécifique avec Context Pack et patchs minimaux."""
        global _processed_issues
        import ast
        import json
        import re
        
        issue_id = issue.id
        
        # Éviter de traiter la même issue plusieurs fois
        if issue_id in _processed_issues:
            logger.info(f"Issue {issue_id} déjà traitée, skip")
            return
        
        github_token = self._get_github_token()
        
        override_owner = self._get_github_owner()
        if override_owner:
            repo_owner = override_owner
            
        if not repo_owner:
             logger.warning("Impossible de déterminer le GitHub Owner (ni env, ni headers MCP).")
             return
        
        if not github_token:
            logger.warning("Aucun token GitHub configuré - opérations GitHub impossibles.")
            return

        # 1. Récupérer l'événement Sentry
        try:
            events_resp = self.sentry._execute_core_logic(SentryRequest(
                command="issue_events",
                issue_id=issue_id,
                organization=org,
                token=sentry_token,
                limit=1
            ))
            if not events_resp.events:
                logger.warning(f"Pas d'événements pour l'issue {issue_id}")
                return
                
            event = events_resp.events[0]
            
        except Exception as e:
            logger.error(f"Impossible de lire les détails de l'issue {issue_id}: {e}")
            return

        # 2. Construire le Context Pack
        logger.info("📦 Construction du Context Pack...")
        
        builder = ContextPackBuilder(
            github_tool=self.github,
            repo_owner=repo_owner,
            repo_name=repo_name,
            github_token=github_token,
            project_prefixes=["collegue/", "src/", "app/", "lib/"]
        )
        
        context_pack = await builder.build(
            sentry_event=event,
            issue_title=issue.title,
            error_message=getattr(issue, 'metadata', {}).get('value', '') if hasattr(issue, 'metadata') else '',
            error_type=getattr(issue, 'metadata', {}).get('type', '') if hasattr(issue, 'metadata') else ''
        )
        
        if not context_pack.primary_file:
            logger.warning("Impossible de construire le Context Pack - pas de fichier source")
            # Fallback: utiliser la stacktrace brute
            stacktrace = event.stacktrace or "No stacktrace available"
            context_prompt = f"STACKTRACE:\n{stacktrace}"
            filepath = None
            original_content = None
        else:
            context_prompt = context_pack.to_prompt_context()
            filepath = context_pack.primary_file.filepath
            original_content = context_pack.primary_file.full_content
            logger.info(f"✅ Context Pack prêt: {filepath}")

        # 3. Construire le prompt avec format SEARCH/REPLACE
        logger.info("🧠 Analyse de la cause racine avec le LLM...")
        
        prompt = f"""Tu es un expert Python/Backend autonome spécialisé dans la correction de bugs.

{context_prompt}

## TÂCHE
Analyse cette erreur et génère un correctif MINIMAL. 

## FORMAT DE RÉPONSE (JSON strict)
{{
    "filepath": "{filepath or 'chemin/vers/fichier.py'}",
    "explanation": "Explication courte de la cause et du fix",
    "patches": [
        {{
            "search": "le code EXACT à remplacer (copié depuis le fichier ci-dessus)",
            "replace": "le nouveau code qui corrige le bug"
        }}
    ]
}}

## RÈGLES CRITIQUES
1. Le champ "search" doit contenir du code EXACTEMENT tel qu'il apparaît dans le fichier
2. Génère des patchs MINIMAUX - ne modifie que les lignes nécessaires
3. NE JAMAIS remplacer tout le fichier - seulement les parties qui causent l'erreur
4. Inclus assez de contexte dans "search" pour que le remplacement soit unique
5. Si plusieurs modifications sont nécessaires, utilise plusieurs patchs

Réponds UNIQUEMENT avec le JSON, sans markdown ni explication."""
        
        try:
            analysis_json = await self.llm.async_generate(prompt)
            
            # Parser la réponse JSON
            match = re.search(r'```json\s*(.*?)\s*```', analysis_json, re.DOTALL)
            if match:
                json_str = match.group(1)
            else:
                json_str = analysis_json.strip()
                
            fix_data = json.loads(json_str)
            
        except Exception as e:
            logger.error(f"Echec de l'analyse LLM: {e}")
            return

        # 4. Valider et appliquer les patchs
        patches = fix_data.get("patches", [])
        target_filepath = fix_data.get("filepath", filepath)
        
        if not target_filepath:
            logger.error("Pas de filepath dans la réponse LLM")
            return
            
        if ".." in target_filepath or target_filepath.startswith("/"):
            logger.error(f"Chemin de fichier suspect: {target_filepath}")
            return
        
        if not patches:
            logger.error("Pas de patchs dans la réponse LLM")
            return
        
        # Récupérer le contenu actuel si pas déjà fait
        if original_content is None or target_filepath != filepath:
            try:
                file_resp = self.github._execute_core_logic(GitHubRequest(
                    command="get_file",
                    owner=repo_owner,
                    repo=repo_name,
                    path=target_filepath,
                    token=github_token
                ))
                import base64
                original_content = base64.b64decode(file_resp.content).decode('utf-8')
            except Exception as e:
                logger.error(f"Impossible de récupérer {target_filepath}: {e}")
                return
        
        # Appliquer les patchs avec fuzzy matching (stratégie Aider/RooCode)
        patched_content = original_content
        patches_applied = 0
        
        for i, patch in enumerate(patches):
            search = patch.get("search", "")
            replace = patch.get("replace", "")
            
            if not search:
                logger.warning(f"Patch {i+1}: 'search' vide, ignoré")
                continue
            
            # 1. Essayer match exact d'abord
            if search in patched_content:
                patched_content = patched_content.replace(search, replace, 1)
                patches_applied += 1
                logger.info(f"✅ Patch {i+1}/{len(patches)} appliqué (exact match)")
                continue
            
            # 2. Essayer fuzzy matching avec difflib
            logger.info(f"Patch {i+1}: tentative fuzzy matching...")
            fuzzy_match, score = _fuzzy_find_match(search, patched_content, threshold=0.7)
            
            if fuzzy_match:
                logger.info(f"Patch {i+1}: fuzzy match trouvé (score: {score:.2f})")
                patched_content = patched_content.replace(fuzzy_match, replace, 1)
                patches_applied += 1
                logger.info(f"✅ Patch {i+1}/{len(patches)} appliqué (fuzzy match)")
                continue
            
            # 3. Log détaillé pour debug
            logger.warning(f"Patch {i+1}: 'search' non trouvé (meilleur score: {score:.2f})")
            logger.debug(f"Search attendu (premières 100 chars): {search[:100]}...")
            logger.error(f"Patch {i+1}: impossible d'appliquer le patch")
        
        if patches_applied == 0:
            logger.error("Aucun patch n'a pu être appliqué")
            return
        
        # 5. Valider le code patché (syntaxe Python)
        if target_filepath.endswith('.py'):
            try:
                ast.parse(patched_content)
                logger.info("✅ Validation syntaxique OK")
            except SyntaxError as e:
                logger.error(f"❌ Code généré invalide: {e}")
                return
        
        # 6. Vérifier que le fichier n'a pas été "vidé"
        if len(patched_content) < len(original_content) * 0.5:
            logger.error(f"❌ Le patch réduit le fichier de plus de 50% ({len(original_content)} -> {len(patched_content)})")
            return

        # 7. Créer la branche et la PR
        branch_name = f"fix/sentry-{issue.short_id}"
        pr_title = f"Fix: {issue.title} (Sentry-{issue.short_id})"
        
        logger.info(f"🛠️ Application du correctif sur {target_filepath} (Branche: {branch_name})")
        
        try:
            # Créer la branche (gérer le cas où elle existe déjà)
            try:
                self.github._execute_core_logic(GitHubRequest(
                    command="create_branch",
                    owner=repo_owner,
                    repo=repo_name,
                    branch=branch_name,
                    token=github_token
                ))
            except Exception as e:
                if "already exists" in str(e).lower() or "422" in str(e):
                    logger.info(f"Branche {branch_name} existe déjà, réutilisation")
                else:
                    raise
            
            # Mettre à jour le fichier
            self.github._execute_core_logic(GitHubRequest(
                command="update_file",
                owner=repo_owner,
                repo=repo_name,
                path=target_filepath,
                message=f"Fix {issue.title}\n\nAppliqué {patches_applied} patch(s) minimal(aux)",
                content=patched_content,
                branch=branch_name,
                token=github_token
            ))
            
            # Créer la PR
            pr_body = f"""## Fix automatique généré par Collegue Watchdog

**Issue Sentry:** {issue.permalink}

### Explication
{fix_data.get('explanation', 'N/A')}

### Patchs appliqués
{patches_applied} modification(s) minimale(s) sur `{target_filepath}`

### Validation
- ✅ Syntaxe Python vérifiée
- ✅ Taille du fichier préservée

---
*Ce fix a été généré automatiquement. Veuillez le revoir avant de merger.*
"""
            
            pr_resp = self.github._execute_core_logic(GitHubRequest(
                command="create_pr",
                owner=repo_owner,
                repo=repo_name,
                title=pr_title,
                body=pr_body,
                head=branch_name,
                base="main",
                token=github_token
            ))
            
            logger.info(f"🚀 PR Créée avec succès: {pr_resp.pr.html_url}")
            
            # Marquer l'issue comme traitée
            _processed_issues.add(issue_id)
            
        except Exception as e:
            logger.error(f"Echec de l'opération GitHub: {e}")

async def _watchdog_loop(interval_seconds: int = 300):
    """Boucle principale du watchdog - multi-utilisateur."""
    registry = get_config_registry()
    
    while True:
        logger.info("🔍 Démarrage du cycle de Self-Healing Multi-Utilisateurs...")
        
        # Récupère toutes les configurations actives (dernières 24h)
        configs = registry.get_all_active(max_age_hours=24.0)
        
        if not configs:
            # Fallback: essayer avec les variables d'environnement
            env_org = os.environ.get("SENTRY_ORG")
            if env_org:
                logger.info(f"Mode mono-utilisateur (env): {env_org}")
                fixer = AutoFixer()
                try:
                    await fixer.run_once()
                except Exception as e:
                    logger.error(f"Erreur dans le cycle watchdog: {e}")
            else:
                logger.warning("Aucune configuration utilisateur enregistrée. "
                             "Effectuez une requête Sentry pour enregistrer vos credentials.")
        else:
            logger.info(f"👥 {len(configs)} configuration(s) utilisateur active(s)")
            for config in configs:
                try:
                    fixer = AutoFixer(user_config=config)
                    await fixer.run_once()
                except Exception as e:
                    logger.error(f"Erreur pour org {config.sentry_org}: {e}")
        
        # Nettoyage des configs inactives
        removed = registry.cleanup_stale(max_age_hours=48.0)
        if removed > 0:
            logger.info(f"🧹 {removed} configuration(s) inactive(s) supprimée(s)")
        
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
