"""
Configuration et règles pour l'outil IaC Guardrails Scan.

Ce fichier contient toutes les constantes, mappings et configurations
qui étaient précédemment inline dans le fichier tool principal.
"""

# Templates de prompts pour l'analyse LLM
DEEP_ANALYSIS_PROMPT_TEMPLATE = """Analyse les configurations IaC et les problèmes de sécurité détectés.

## Fichiers IaC analysés
{files_summary}

## Findings ({findings_count} total)
{findings_summary}

## Contexte
- Cloud provider: {cloud}
- Profil: {policy_profile}

---

Fournis une analyse de sécurité enrichie au format JSON strict:
{{
  "security_score": 0.0-1.0,
  "compliance_score": 0.0-1.0,
  "risk_level": "low|medium|high|critical",
  "insights": [
    {{
      "category": "vulnerability|misconfiguration|compliance|best_practice",
      "insight": "Description détaillée du problème ou de la recommandation",
      "risk_level": "low|medium|high|critical",
      "affected_resources": ["resource1", "resource2"],
      "compliance_frameworks": ["CIS", "SOC2", "HIPAA"]
    }}
  ]
}}

Catégories d'insights:
- **vulnerability**: Failles de sécurité exploitables
- **misconfiguration**: Configurations incorrectes ou dangereuses
- **compliance**: Non-conformité aux standards (CIS, SOC2, HIPAA, PCI-DSS)
- **best_practice**: Recommandations d'amélioration

Scores:
- `security_score`: 1.0 = sécurisé, 0.0 = critique
- `compliance_score`: 1.0 = conforme, 0.0 = non conforme

Réponds UNIQUEMENT avec le JSON, sans markdown ni explication."""


FALLBACK_PROMPT_TEMPLATE = """Tu es un expert en sécurité Infrastructure as Code (DevSecOps).
Je vais te fournir les résultats bruts d'un scan de sécurité IaC (Terraform, Kubernetes, Dockerfile).

### PROBLÈMES DÉTECTÉS
{findings_json}

### TÂCHE
1. Analyse ces problèmes de sécurité.
2. Identifie les risques métier, les impacts sur la conformité (CIS, SOC2, etc.).
3. Calcule un score de sécurité (0.0 à 1.0) basé sur la sévérité et la densité des problèmes.

### FORMAT DE RÉPONSE (JSON STRICT)
{{
    "insights": [
        {{
            "category": "vulnerability|misconfiguration|compliance|best_practice",
            "insight": "Explication claire du risque et de son impact réel",
            "risk_level": "low|medium|high|critical",
            "affected_resources": ["res1", "res2"],
            "compliance_frameworks": ["CIS AWS 1.2", "SOC2 CC6.1"]
        }}
    ],
    "security_score": 0.8,
    "compliance_score": 0.9,
    "risk_level": "low|medium|high|critical"
}}

RÈGLES IMPORTANTES:
- Sois précis et synthétique dans tes insights.
- Ne répète pas simplement les findings, analyse leur impact combiné.
- `security_score`: 1.0 = sécurisé, 0.0 = critique
- `compliance_score`: 1.0 = conforme, 0.0 = non conforme

Réponds UNIQUEMENT avec le JSON, sans markdown ni explication."""


# Poids des sévérités pour le calcul des scores
SEVERITY_WEIGHTS = {
    'critical': 0.4,
    'high': 0.25,
    'medium': 0.1,
    'low': 0.05
}

# Seuils de risque basés sur le score de sécurité
RISK_THRESHOLDS = {
    'critical': 0.3,
    'high': 0.5,
    'medium': 0.7
}
