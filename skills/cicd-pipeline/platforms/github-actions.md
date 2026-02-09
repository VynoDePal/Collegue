# GitHub Actions — Bonnes pratiques

## Structure de fichier recommandée

```yaml
name: CI/CD Pipeline

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
      - run: npm ci
      - run: npm run lint

  test:
    needs: lint
    runs-on: ubuntu-latest
    timeout-minutes: 15
    strategy:
      matrix:
        node-version: [18, 20, 22]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node-version }}
          cache: 'npm'
      - run: npm ci
      - run: npm test -- --coverage
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: coverage-${{ matrix.node-version }}
          path: coverage/

  security:
    needs: lint
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - name: Audit dependencies
        run: npm audit --audit-level=high
      - name: Check for secrets
        uses: trufflesecurity/trufflehog@v3
        with:
          extra_args: --only-verified

  build:
    needs: [test, security]
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
      - run: npm ci
      - run: npm run build
      - uses: actions/upload-artifact@v4
        with:
          name: build
          path: dist/

  deploy-staging:
    needs: build
    if: github.ref == 'refs/heads/develop'
    runs-on: ubuntu-latest
    environment: staging
    timeout-minutes: 10
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: build
      - name: Deploy to staging
        env:
          DEPLOY_TOKEN: ${{ secrets.STAGING_DEPLOY_TOKEN }}
        run: echo "Deploy to staging"

  deploy-production:
    needs: build
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment:
      name: production
      url: https://example.com
    timeout-minutes: 10
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: build
      - name: Deploy to production
        env:
          DEPLOY_TOKEN: ${{ secrets.PROD_DEPLOY_TOKEN }}
        run: echo "Deploy to production"
```

## Règles de sécurité

### Permissions minimales
```yaml
# Au niveau du workflow (restrictif par défaut)
permissions:
  contents: read

# Au niveau du job (si besoin spécifique)
jobs:
  deploy:
    permissions:
      contents: read
      deployments: write
```

### Secrets
- Utiliser `${{ secrets.NAME }}` (jamais en clair)
- Les secrets sont masqués dans les logs automatiquement
- Ne jamais `echo` un secret, même pour debug
- Utiliser des environments pour séparer staging/prod
- Activer les "required reviewers" sur les environments de production

### Actions tierces
```yaml
# BON : version pinnée avec SHA
- uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11 # v4.1.1

# ACCEPTABLE : tag semver
- uses: actions/checkout@v4

# MAUVAIS : branche (non reproductible)
- uses: actions/checkout@main
```

## Optimisation de performance

### Cache NPM
```yaml
- uses: actions/setup-node@v4
  with:
    node-version: '20'
    cache: 'npm'  # Cache automatique basé sur package-lock.json
```

### Cache pip
```yaml
- uses: actions/setup-python@v5
  with:
    python-version: '3.12'
    cache: 'pip'
```

### Cache Docker layers
```yaml
- uses: docker/build-push-action@v5
  with:
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

### Concurrency (annuler les builds obsolètes)
```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

## Patterns avancés

### Monorepo — build conditionnel
```yaml
on:
  push:
    paths:
      - 'packages/frontend/**'
      - 'package.json'

# Ou utiliser dorny/paths-filter
- uses: dorny/paths-filter@v3
  id: changes
  with:
    filters: |
      frontend:
        - 'packages/frontend/**'
      backend:
        - 'packages/backend/**'
```

### Reusable workflows
```yaml
# .github/workflows/reusable-test.yml
on:
  workflow_call:
    inputs:
      node-version:
        type: string
        default: '20'

# Appelé depuis un autre workflow
jobs:
  test:
    uses: ./.github/workflows/reusable-test.yml
    with:
      node-version: '20'
```

### Dependabot auto-merge (patch seulement)
```yaml
# .github/workflows/dependabot-automerge.yml
name: Dependabot auto-merge
on: pull_request

permissions:
  contents: write
  pull-requests: write

jobs:
  automerge:
    if: github.actor == 'dependabot[bot]'
    runs-on: ubuntu-latest
    steps:
      - uses: dependabot/fetch-metadata@v2
        id: metadata
      - if: steps.metadata.outputs.update-type == 'version-update:semver-patch'
        run: gh pr merge --auto --squash "$PR_URL"
        env:
          PR_URL: ${{ github.event.pull_request.html_url }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

## Checklist avant merge

- [ ] `permissions` définies au niveau workflow (pas de `write-all`)
- [ ] Tous les jobs ont un `timeout-minutes`
- [ ] Les actions tierces sont pinnées (tag ou SHA)
- [ ] Les secrets utilisent `${{ secrets.X }}`, jamais en clair
- [ ] `concurrency` configuré pour éviter les builds parallèles inutiles
- [ ] Le cache est activé pour les dépendances
- [ ] Les environments de production ont des "required reviewers"
- [ ] Pas de `continue-on-error: true` sans justification
