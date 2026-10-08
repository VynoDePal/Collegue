# Référence sécurité — Checklist et patterns

## OWASP Top 10 (2021) — Checklist

### A01:2021 — Broken Access Control
- [ ] Vérifier que les endpoints API exigent une authentification
- [ ] Vérifier le contrôle d'accès basé sur les rôles (RBAC)
- [ ] Vérifier qu'on ne peut pas accéder aux ressources d'un autre utilisateur (IDOR)
- [ ] Vérifier que les tokens JWT sont validés côté serveur
- [ ] Vérifier que CORS est configuré strictement

### A02:2021 — Cryptographic Failures
- [ ] Vérifier le chiffrement au repos (base de données, fichiers)
- [ ] Vérifier le chiffrement en transit (TLS/HTTPS)
- [ ] Vérifier qu'aucun secret n'est hardcodé (`secret_scan`)
- [ ] Vérifier les algorithmes de hashing (bcrypt/argon2, pas MD5/SHA1)
- [ ] Vérifier la rotation régulière des clés

### A03:2021 — Injection
- [ ] Vérifier les requêtes SQL paramétrées (pas de concaténation)
- [ ] Vérifier la sanitization des inputs utilisateur
- [ ] Vérifier l'échappement des outputs (XSS)
- [ ] Vérifier l'absence de `eval()`, `exec()`, `Function()` sur des inputs
- [ ] Vérifier les commandes OS (pas de `os.system()` avec input utilisateur)

### A04:2021 — Insecure Design
- [ ] Vérifier la validation des inputs côté serveur (pas seulement client)
- [ ] Vérifier les rate limits sur les endpoints sensibles
- [ ] Vérifier la séparation des environnements (dev/staging/prod)
- [ ] Vérifier que les erreurs ne leakent pas d'informations sensibles

### A05:2021 — Security Misconfiguration
- [ ] Vérifier les headers de sécurité HTTP (CSP, HSTS, X-Frame-Options)
- [ ] Vérifier que le mode debug est désactivé en production
- [ ] Vérifier les permissions des fichiers et répertoires
- [ ] Vérifier la configuration IaC (`iac_guardrails_scan`)
- [ ] Vérifier que les ports inutiles sont fermés

### A06:2021 — Vulnerable and Outdated Components
- [ ] Scanner les dépendances pour CVE (`dependency_guard`)
- [ ] Vérifier les versions des frameworks et bibliothèques
- [ ] Vérifier l'absence de packages dépréciés
- [ ] Vérifier les images Docker de base

### A07:2021 — Identification and Authentication Failures
- [ ] Vérifier la politique de mots de passe (longueur, complexité)
- [ ] Vérifier la protection contre le brute-force (rate limiting, lockout)
- [ ] Vérifier l'implémentation du MFA
- [ ] Vérifier la gestion sécurisée des sessions (expiration, invalidation)

### A08:2021 — Software and Data Integrity Failures
- [ ] Vérifier l'intégrité des dépendances (lockfiles, checksums)
- [ ] Vérifier les pipelines CI/CD (permissions, secrets)
- [ ] Vérifier les mises à jour automatiques (Dependabot, Renovate)

### A09:2021 — Security Logging and Monitoring Failures
- [ ] Vérifier que les événements de sécurité sont loggés
- [ ] Vérifier que les logs ne contiennent pas de données sensibles
- [ ] Vérifier l'alerting sur les événements critiques
- [ ] Vérifier la rétention des logs

### A10:2021 — Server-Side Request Forgery (SSRF)
- [ ] Vérifier que les URLs utilisateur sont validées (whitelist)
- [ ] Vérifier que les requêtes internes ne sont pas accessibles
- [ ] Vérifier les redirections ouvertes

## Patterns de rotation de secrets

### Rotation immédiate (secret exposé dans Git)
```
1. Révoquer le secret immédiatement (console du provider)
2. Générer un nouveau secret
3. Mettre à jour dans le vault/secret manager
4. Supprimer de l'historique Git :
   git filter-branch --force --index-filter \
     'git rm --cached --ignore-unmatch PATH_TO_FILE' \
     --prune-empty --tag-name-filter cat -- --all
   OU utiliser BFG Repo Cleaner
5. Force push + invalider les caches
6. Vérifier les logs d'accès du secret compromis
```

### Stockage recommandé par environnement
| Environnement | Solution recommandée |
|---------------|---------------------|
| **Local** | `.env` (non commité, dans .gitignore) |
| **CI/CD** | Secrets GitHub/GitLab (encrypted, masqués dans les logs) |
| **Staging** | AWS Secrets Manager / HashiCorp Vault |
| **Production** | AWS Secrets Manager / HashiCorp Vault + rotation automatique |
| **Kubernetes** | K8s Secrets (encrypted at rest) + External Secrets Operator |

### Fréquence de rotation recommandée
| Type de secret | Fréquence |
|---------------|-----------|
| Clés API cloud (AWS, GCP, Azure) | 90 jours |
| Tokens d'accès (GitHub, GitLab) | 30-90 jours |
| Mots de passe base de données | 90 jours |
| Clés de chiffrement | 365 jours |
| Certificats TLS | Avant expiration (Let's Encrypt = 90j) |

## Patterns de sécurité des dépendances

### Protection contre le typosquatting
- Toujours vérifier le nom exact du package sur le registre officiel
- Utiliser un lockfile (`package-lock.json`, `poetry.lock`)
- Activer `dependency_guard` avec `check_existence: true`
- Configurer une allowlist pour les projets critiques

### Supply chain security
- Vérifier les checksums des packages (lockfile)
- Utiliser des registres privés pour les packages internes
- Activer Dependabot / Renovate pour les mises à jour automatiques
- Scanner régulièrement avec `dependency_guard` en CI/CD
