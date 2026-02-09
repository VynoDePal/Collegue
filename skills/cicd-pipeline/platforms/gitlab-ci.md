# GitLab CI — Bonnes pratiques

## Structure de fichier recommandée

```yaml
stages:
  - lint
  - test
  - security
  - build
  - deploy

variables:
  NODE_VERSION: "20"
  DOCKER_DRIVER: overlay2

default:
  image: node:${NODE_VERSION}-alpine
  cache:
    key:
      files:
        - package-lock.json
    paths:
      - node_modules/
    policy: pull
  retry:
    max: 2
    when:
      - runner_system_failure
      - stuck_or_timeout_failure

# ==================== LINT ====================

lint:
  stage: lint
  timeout: 10 minutes
  script:
    - npm ci --prefer-offline
    - npm run lint
  cache:
    key:
      files:
        - package-lock.json
    paths:
      - node_modules/
    policy: pull-push
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH

# ==================== TEST ====================

test:
  stage: test
  timeout: 15 minutes
  needs: [lint]
  parallel:
    matrix:
      - NODE_VERSION: ["18", "20", "22"]
  script:
    - npm ci --prefer-offline
    - npm test -- --coverage
  coverage: '/Statements\s*:\s*(\d+\.?\d*)%/'
  artifacts:
    reports:
      junit: junit.xml
      coverage_report:
        coverage_format: cobertura
        path: coverage/cobertura-coverage.xml
    expire_in: 7 days
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH

# ==================== SECURITY ====================

security:audit:
  stage: security
  timeout: 10 minutes
  needs: [lint]
  script:
    - npm audit --audit-level=high
  allow_failure: false
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH

# ==================== BUILD ====================

build:
  stage: build
  timeout: 15 minutes
  needs: [test, security:audit]
  script:
    - npm ci --prefer-offline
    - npm run build
  artifacts:
    paths:
      - dist/
    expire_in: 1 day
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
    - if: $CI_COMMIT_BRANCH == "develop"

# ==================== DEPLOY ====================

deploy:staging:
  stage: deploy
  timeout: 10 minutes
  needs: [build]
  environment:
    name: staging
    url: https://staging.example.com
  script:
    - echo "Deploy to staging"
  rules:
    - if: $CI_COMMIT_BRANCH == "develop"

deploy:production:
  stage: deploy
  timeout: 10 minutes
  needs: [build]
  environment:
    name: production
    url: https://example.com
  script:
    - echo "Deploy to production"
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  when: manual
```

## Règles de sécurité

### Variables protégées
```yaml
# Dans Settings > CI/CD > Variables
# Cocher "Protected" pour les secrets de production
# Cocher "Masked" pour masquer dans les logs
variables:
  DEPLOY_TOKEN:
    value: ""  # Défini dans les settings
    description: "Token de déploiement"
    # Protected: true (via l'UI)
    # Masked: true (via l'UI)
```

### Images Docker
```yaml
# BON : version spécifique
image: node:20.11.0-alpine

# ACCEPTABLE : tag mineur
image: node:20-alpine

# MAUVAIS : latest
image: node:latest
```

### Limiter l'exécution
```yaml
# N'exécuter que sur les branches protégées et les MR
rules:
  - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  - if: $CI_COMMIT_TAG  # Pour les releases
```

## Optimisation de performance

### Cache efficace
```yaml
# Cache global (lecture par défaut)
default:
  cache:
    key:
      files:
        - package-lock.json
    paths:
      - node_modules/
    policy: pull  # Lecture seule

# Le premier job écrit le cache
install:
  cache:
    policy: pull-push  # Lecture + écriture
```

### Parallélisme avec matrix
```yaml
test:
  parallel:
    matrix:
      - NODE_VERSION: ["18", "20", "22"]
        DB: ["postgres", "mysql"]
```

### DAG avec `needs` (au lieu de `stages` séquentiels)
```yaml
# Les jobs avec `needs` démarrent dès que leurs dépendances sont terminées
# sans attendre la fin du stage entier
lint:
  stage: lint

test:unit:
  stage: test
  needs: [lint]

test:e2e:
  stage: test
  needs: [lint]  # Parallèle avec test:unit

build:
  stage: build
  needs: [test:unit, test:e2e]  # Attend les deux
```

### Artefacts avec expiration
```yaml
artifacts:
  paths:
    - dist/
  expire_in: 1 day  # Ne pas garder éternellement
  when: on_success   # Seulement si le job réussit
```

## Patterns avancés

### Include de templates partagés
```yaml
include:
  - project: 'my-group/ci-templates'
    ref: v1.0.0
    file: '/templates/node-test.yml'
  - local: '/.gitlab/ci/deploy.yml'
```

### Environnements avec review apps
```yaml
deploy:review:
  environment:
    name: review/$CI_COMMIT_REF_SLUG
    url: https://$CI_COMMIT_REF_SLUG.review.example.com
    on_stop: stop:review
    auto_stop_in: 1 week
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"

stop:review:
  environment:
    name: review/$CI_COMMIT_REF_SLUG
    action: stop
  when: manual
```

### Monorepo avec `changes`
```yaml
test:frontend:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes:
        - packages/frontend/**/*
        - package.json

test:backend:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes:
        - packages/backend/**/*
        - requirements.txt
```

## Checklist avant merge

- [ ] Toutes les images Docker ont des versions spécifiques (pas `latest`)
- [ ] Les variables sensibles sont marquées "Protected" et "Masked"
- [ ] Chaque job a un `timeout` défini
- [ ] Le cache utilise `policy: pull` par défaut (un seul job en `pull-push`)
- [ ] `allow_failure` n'est PAS utilisé sur les jobs critiques (tests, security)
- [ ] Les artefacts ont `expire_in` pour ne pas remplir le stockage
- [ ] Les `rules` limitent l'exécution aux cas nécessaires
- [ ] `needs` est utilisé pour optimiser le DAG (pas de stages séquentiels inutiles)
- [ ] Le déploiement production est `when: manual` avec approval
- [ ] `retry` est configuré pour les erreurs d'infrastructure
