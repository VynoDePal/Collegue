---
name: cicd-pipeline
description: Conventions et bonnes pratiques CI/CD pour GitHub Actions, GitLab CI et autres plateformes. Utilise cette skill quand l'utilisateur crée, modifie ou debug un pipeline CI/CD, ou demande des conseils sur l'intégration continue.
---

# CI/CD Pipeline — Conventions et workflows

Ce guide définit les conventions pour créer des pipelines CI/CD robustes et sécurisés. Utilise `iac_guardrails_scan` pour valider les fichiers de configuration.

## Principes fondamentaux

### 1. Sécurité d'abord
- **Jamais** de secrets hardcodés dans les fichiers de pipeline
- Utiliser les secrets natifs de la plateforme (GitHub Secrets, GitLab CI/CD Variables)
- Limiter les permissions au minimum nécessaire
- Scanner les dépendances et le code dans le pipeline (`dependency_guard`, `secret_scan`)

### 2. Reproductibilité
- **Pinner** toutes les versions (actions, images Docker, dépendances)
- Utiliser des lockfiles (package-lock.json, poetry.lock)
- Préférer les tags semver aux branches (`actions/checkout@v4`, pas `@main`)
- Utiliser des digests SHA pour les images Docker critiques

### 3. Performance
- Activer le **cache** pour les dépendances (node_modules, pip cache, .m2)
- Utiliser le **parallélisme** (matrix builds, jobs concurrents)
- Séparer les pipelines (lint rapide vs tests complets)
- Utiliser des images Docker slim/alpine

### 4. Fiabilité
- Définir des **timeouts** sur chaque job
- Configurer les **retries** pour les étapes réseau (install, deploy)
- Utiliser des **health checks** post-déploiement
- Implémenter le **rollback** automatique

## Structure recommandée d'un pipeline

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  Lint &  │──▶│  Tests   │──▶│ Security │──▶│  Build   │
│  Format  │   │  Unit    │   │  Scan    │   │          │
└──────────┘   └──────────┘   └──────────┘   └──────────┘
                                                   │
                                                   ▼
                              ┌──────────┐   ┌──────────┐
                              │  Deploy  │◀──│  Tests   │
                              │  Prod    │   │  E2E     │
                              └──────────┘   └──────────┘
```

### Étapes standard
1. **Lint & Format** — ESLint, Prettier, Black, Ruff (rapide, fail fast)
2. **Tests unitaires** — Jest, Pytest, avec couverture minimum
3. **Security scan** — `secret_scan`, `dependency_guard`, SAST
4. **Build** — Compilation, bundling, Docker build
5. **Tests E2E** — Playwright, Cypress (sur l'artefact buildé)
6. **Deploy staging** — Déploiement automatique sur staging
7. **Deploy production** — Manuel ou auto avec approval

## Bonnes pratiques par plateforme

Pour les détails par plateforme :
- [GitHub Actions](platforms/github-actions.md)
- [GitLab CI](platforms/gitlab-ci.md)

## Validation avec Collègue

Avant de merger un pipeline, valide-le :

```
# Appeler iac_guardrails_scan avec le fichier de pipeline
iac_guardrails_scan({
  files: [{path: ".github/workflows/ci.yml", content: "..."}],
  policy_profile: "strict"
})
```

## Patterns CI/CD courants

### Matrix build (multi-version)
Tester sur plusieurs versions de runtime (Node 18/20/22, Python 3.10/3.11/3.12).
Utilise la stratégie matrix de la plateforme.

### Cache efficace
- Clé de cache = hash du lockfile (`hashFiles('**/package-lock.json')`)
- Restore keys par ordre de spécificité
- Ne pas cacher les artefacts de build (seulement les dépendances)

### Deploy progressif
- **Canary** : déployer sur 5% du trafic, monitorer, puis 100%
- **Blue/Green** : basculer le load balancer entre 2 environnements
- **Rolling** : remplacer les instances une par une

### Monorepo
- Détecter les changements par dossier (`paths:` dans GitHub Actions)
- Ne builder/tester que les packages modifiés
- Utiliser des outils comme Turborepo, Nx, ou Lerna

## Anti-patterns CI/CD

| Anti-pattern | Problème | Solution |
|-------------|----------|----------|
| `@latest` ou `@main` sur les actions | Non reproductible | Pinner avec `@v4` ou SHA |
| Secrets en `echo` dans les logs | Fuite de secrets | Utiliser les secrets natifs, masquer les outputs |
| Job unique géant | Lent, pas de parallélisme | Séparer en jobs indépendants |
| Pas de timeout | Jobs bloqués indéfiniment | Ajouter `timeout-minutes` |
| Skip tests sur `main` | Régressions en prod | Toujours tester avant deploy |
| `allow_failure: true` partout | Problèmes ignorés | Limiter aux tests flaky avec suivi |
| Docker build sans cache | Builds lents | Multi-stage + cache layers |
