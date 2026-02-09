import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_skills_dir() -> Path:
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


SKILLS_DIR = _get_skills_dir()
def _discover_skills() -> Dict[str, Dict[str, Any]]:
	skills: Dict[str, Dict[str, Any]] = {}
	if not SKILLS_DIR.is_dir():
		logger.warning("Dossier skills/ introuvable: %s", SKILLS_DIR)
		return skills
	for entry in sorted(SKILLS_DIR.iterdir()):
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
		description = _extract_description(skill_md)
		skills[skill_name] = {
			"name": skill_name,
			"description": description,
			"files": ["SKILL.md"] + annexes,
		}
	return skills
def _extract_description(skill_md: Path) -> str:
	try:
		text = skill_md.read_text(encoding="utf-8")
		if text.startswith("---"):
			parts = text.split("---", 2)
			if len(parts) >= 3:
				for line in parts[1].splitlines():
					line = line.strip()
					if line.startswith("description:"):
						desc = line[len("description:"):].strip()
						return desc[:200]
		for line in text.splitlines():
			stripped = line.strip()
			if stripped.startswith("# "):
				return stripped[2:].strip()[:200]
	except Exception:
		pass
	return "Skill Collègue"
def _read_skill_file(skill_name: str, file_path: str) -> Optional[str]:
	target = (SKILLS_DIR / skill_name / file_path).resolve()
	if not str(target).startswith(str(SKILLS_DIR)):
		logger.warning("Path traversal détecté: %s", file_path)
		return None
	if not target.is_file():
		return None
	try:
		return target.read_text(encoding="utf-8")
	except Exception as e:
		logger.error("Erreur lecture %s: %s", target, e)
		return None
def _register_annex(app: Any, skill_name: str, file_name: str) -> None:
	uri = f"collegue://skills/{skill_name}/{file_name}"
	@app.resource(uri)
	def _read_annex() -> str:
		content = _read_skill_file(skill_name, file_name)
		if content is None:
			return json.dumps(
				{"error": f"Fichier '{file_name}' introuvable"},
				ensure_ascii=False,
			)
		return content
	_read_annex.__name__ = f"skill_{skill_name}_{file_name.replace('/', '_').replace('.', '_')}"
	_read_annex.__doc__ = (
		f"Fichier annexe '{file_name}' du skill '{skill_name}'."
	)
def register_skills(app: Any, app_state: Any) -> None:
	skills_index = _discover_skills()
	logger.info(
		"Skills découverts: %s",
		list(skills_index.keys()),
	)
	@app.resource("collegue://skills")
	def get_skills_index() -> str:
		refreshed = _discover_skills()
		return json.dumps(
			{
				"skills": list(refreshed.values()),
				"total": len(refreshed),
				"usage": (
					"Lisez un skill via collegue://skills/{name} "
					"ou un fichier annexe via "
					"collegue://skills/{name}/{file}"
				),
			},
			ensure_ascii=False,
			indent=2,
		)
	@app.resource("collegue://skills/{skill_name}")
	def get_skill_main(skill_name: str) -> str:
		content = _read_skill_file(skill_name, "SKILL.md")
		if content is None:
			return json.dumps(
				{
					"error": f"Skill '{skill_name}' non trouvé",
					"available": list(_discover_skills().keys()),
				},
				ensure_ascii=False,
			)
		return content
	for skill_name, skill_info in skills_index.items():
		for file_name in skill_info["files"]:
			if file_name == "SKILL.md":
				continue
			_register_annex(app, skill_name, file_name)
	count = len(skills_index)
	logger.info(
		"%d skill(s) enregistré(s) comme MCP Resources", count,
	)
	print(f"  📚 {count} skill(s) exposé(s) comme MCP Resources")