"""
Skills Provider - Migration vers FastMCP 3.0 SkillsDirectoryProvider
"""
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def get_skills_dir() -> Path:
    """
    Trouve le dossier skills/ en utilisant les mêmes priorités que l'ancien système
    """
    import os
    
    candidates = []
    
    env_path = os.environ.get('COLLEGUE_SKILLS_DIR')
    if env_path:
        candidates.append(("env", Path(env_path)))
    
    candidates.append(("__file__", Path(__file__).resolve().parent.parent.parent / "skills"))
    candidates.append(("cwd", Path.cwd() / "skills"))
    candidates.append(("workdir", Path("/app/skills")))
    
    for label, path in candidates:
        if path.is_dir():
            logger.info("SKILLS_DIR résolu via %s: %s", label, path)
            return path
    
    logger.error(
        "Dossier skills/ introuvable! Chemins testés: %s",
        ", ".join(f"{label}={path}" for label, path in candidates),
    )
    return candidates[1][1]


def register_skills(app: Any, app_state: dict):
    """
    Enregistre les skills en utilisant FastMCP SkillsDirectoryProvider (3.0+)
    """
    try:
        # Tenter d'importer SkillsDirectoryProvider (FastMCP 3.0+)
        from fastmcp.server.providers.skills import SkillsDirectoryProvider
        
        skills_dir = get_skills_dir()
        
        if skills_dir.is_dir():
            # Ajouter le provider FastMCP natif
            app.add_provider(SkillsDirectoryProvider(roots=skills_dir))
            logger.info(f"SkillsProvider FastMCP enregistré avec le dossier: {skills_dir}")
            
            # Compter les skills pour information
            skill_count = len([d for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()])
            print(f"✅ {skill_count} skills chargés via SkillsProvider FastMCP")
        else:
            logger.warning(f"Dossier skills introuvable: {skills_dir}")
            
    except ImportError:
        # Fallback: utiliser l'ancien système si FastMCP 3.0 n'est pas disponible
        logger.warning("SkillsProvider FastMCP 3.0 non disponible, utilisation du fallback")
        _register_skills_fallback(app, app_state)


def _register_skills_fallback(app: Any, app_state: dict):
    """
    Fallback utilisant l'ancien système de resources MCP
    """
    import json
    from typing import Dict, List
    
    skills_dir = get_skills_dir()
    
    def _discover_skills() -> Dict[str, Dict[str, Any]]:
        skills: Dict[str, Dict[str, Any]] = {}
        if not skills_dir.is_dir():
            return skills
            
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue
                
            skill_name = entry.name
            annexes: List[str] = []
            for f in sorted(entry.rglob("*")):
                if f.is_file() and f.name != "SKILL.md":
                    rel = f.relative_to(entry)
                    annexes.append(str(rel))
            
            skills[skill_name] = {
                "name": skill_name,
                "path": str(entry),
                "annexes": annexes
            }
        
        return skills
    
    def _extract_description(skill_md_path: Path) -> str:
        try:
            with open(skill_md_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Extraire la description du frontmatter YAML
            if content.startswith('---'):
                end_idx = content.find('---', 3)
                if end_idx != -1:
                    frontmatter = content[3:end_idx]
                    for line in frontmatter.split('\n'):
                        if line.startswith('description:'):
                            return line.split(':', 1)[1].strip().strip('"')
            
            # Fallback: première ligne du contenu
            lines = content[end_idx + 3:].strip().split('\n') if '---' in content else content.strip().split('\n')
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    return line[:100] + "..." if len(line) > 100 else line
            
            return "Skill sans description"
            
        except Exception:
            return "Skill sans description"
    
    def _register_annex(app, skill_name: str, skill_path: Path, annex_file: str):
        relative_path = f"{skill_name}/{annex_file}"
        full_path = skill_path / annex_file
        
        @app.resource(uri=f"collegue://skills/{relative_path}")
        def get_annex():
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                return f"Erreur lecture {annex_file}: {e}"
        
        # Renommer la fonction pour éviter les conflits
        get_annex.__name__ = f"get_{skill_name}_{annex_file.replace('/', '_')}"
    
    skills = _discover_skills()
    
    # Enregistrer l'index des skills
    @app.resource(uri="collegue://skills")
    def list_skills():
        return json.dumps({
            "skills": [
                {
                    "name": name,
                    "description": _extract_description(Path(info["path"]) / "SKILL.md"),
                    "annexes": info["annexes"]
                }
                for name, info in skills.items()
            ]
        }, indent=2)
    
    # Enregistrer chaque skill et ses annexes
    for skill_name, info in skills.items():
        skill_path = Path(info["path"])
        skill_md = skill_path / "SKILL.md"
        
        @app.resource(uri=f"collegue://skills/{skill_name}")
        def get_skill():
            try:
                with open(skill_md, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                return f"Erreur lecture SKILL.md: {e}"
        
        # Renommer la fonction pour éviter les conflits
        get_skill.__name__ = f"get_{skill_name}"
        
        # Enregistrer les fichiers annexes
        for annex_file in info["annexes"]:
            _register_annex(app, skill_name, skill_path, annex_file)
    
    if skills:
        print(f"✅ {len(skills)} skills chargés via fallback MCP resources")
    else:
        print("⚠️ Aucun skill trouvé")
