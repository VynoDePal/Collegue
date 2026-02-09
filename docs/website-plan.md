# Plan — Site Web Vitrine pour Collègue MCP

Plan complet pour la réalisation du site web de présentation du serveur MCP "Collègue", inspiré de gemini-design-mcp.com, avec charte blanc/or et bilingue EN/FR.

---

## 1. Synthèse du Projet Collègue MCP

**Collègue** est un serveur MCP (Model Context Protocol) open-source (MIT, par VynoDePal) fournissant **15 outils** répartis en 2 catégories :

### Outils d'Analyse & Qualité de Code (9)

| Outil | Description courte |
|---|---|
| `code_documentation` | Génération auto de doc (Markdown, RST, HTML, docstring) |
| `code_refactoring` | Refactoring intelligent (rename, extract, simplify, optimize, clean, modernize) |
| `test_generation` | Génération de tests unitaires avec validation auto |
| `secret_scan` | Détection de secrets exposés (30+ patterns : AWS, GCP, OpenAI, GitHub…) |
| `dependency_guard` | Audit supply chain via API OSV Google (vulnérabilités, typosquatting) |
| `impact_analysis` | Analyse prédictive d'impact avant modification |
| `repo_consistency_check` | Détection d'hallucinations IA (code mort, imports inutilisés) |
| `iac_guardrails_scan` | Sécurisation IaC (Terraform, K8s, Dockerfile) |
| `run_tests` | Exécution de tests avec rapports structurés |

### Outils d'Intégration (4)

| Outil | Description courte |
|---|---|
| `postgres_db` | Inspection PostgreSQL (schéma, requêtes lecture seule, stats) |
| `github_ops` | Gestion repos, PRs, issues, branches, CI/CD |
| `sentry_monitor` | Erreurs, stacktraces, releases Sentry |
| `kubernetes_ops` | Pods, logs, déploiements, services K8s |

### Agent Autonome

| Feature | Description |
|---|---|
| **Watchdog (Self-Healing)** | Surveille Sentry → analyse LLM → crée PR GitHub automatiquement |

---

## 2. Analyse du Site de Référence (gemini-design-mcp.com)

### Structure des sections

1. **Navbar** — Logo, liens (Features, Integration, Pricing), CTA "Get Started"
2. **Hero** — Titre accrocheur + sous-titre explicatif + 2 CTAs + 4 stats en badges
3. **Synergy Section** — 3 cartes expliquant la proposition de valeur
4. **MCP Capabilities** — 6 cartes détaillant chaque outil/fonctionnalité
5. **Integration** — Blocs de code avec tabs par IDE + instructions d'installation
6. **CLAUDE.md / AGENTS.md** — Section documentation intégrée avec code
7. **Pricing** — 3 tiers (Starter/Pro/Enterprise) + option BYOK
8. **Testimonial / Créateur** — Section personnelle du créateur
9. **CTA Final** — Appel à l'action final
10. **Footer** — Liens Product/Resources/Legal + réseaux sociaux

### Éléments de design notables

- Design sombre (dark mode par défaut)
- Animations subtiles, gradients
- Blocs de code avec syntax highlighting
- Cartes avec bordures lumineuses
- Badges/stats avec chiffres impactants

---

## 3. Spécifications du Site Collègue MCP

### 3.1 Stack Technique

- **Framework** : Next.js 14 (App Router)
- **Langage** : TypeScript
- **Styling** : Tailwind CSS
- **Composants** : shadcn/ui + Radix UI
- **Icônes** : Lucide React
- **i18n** : next-intl (anglais par défaut, français)
- **Animations** : Framer Motion
- **Syntax Highlighting** : Shiki ou Prism
- **Déploiement** : Netlify

### 3.2 Charte Graphique

#### Palette de couleurs

| Token | Light Mode | Dark Mode |
|---|---|---|
| `--background` | `#FFFFFF` (blanc pur) | `#0A0A0F` (noir profond) |
| `--foreground` | `#1A1A2E` (noir doux) | `#F5F5F5` (blanc cassé) |
| `--primary` | `#D4A017` (or royal) | `#F5C518` (or lumineux) |
| `--primary-hover` | `#B8860B` (or foncé) | `#FFD700` (gold vif) |
| `--accent` | `#FFF8E1` (crème doré) | `#1A1508` (noir doré) |
| `--muted` | `#F8F6F0` (beige clair) | `#18181B` (zinc-900) |
| `--border` | `#E8E0D0` (doré subtil) | `#2A2520` (brun sombre) |
| `--card` | `#FFFDF7` (blanc chaud) | `#121215` (noir carte) |

#### Typographie

- **Titres** : Inter (Bold/Semibold)
- **Corps** : Inter (Regular)
- **Code** : JetBrains Mono / Fira Code

#### Style général

- Élégant, premium, professionnel
- Gradients dorés subtils sur les éléments interactifs
- Bordures dorées lumineuses (glow) sur les cartes en hover
- Mode sombre avec accents or lumineux
- Mode clair avec blanc dominant et touches dorées raffinées

### 3.3 Internationalisation (i18n)

- **Langue par défaut** : Anglais (EN)
- **Langue secondaire** : Français (FR)
- **Routing** : `/en/...` et `/fr/...` via next-intl middleware
- **Sélecteur de langue** : dans la navbar (dropdown discret)
- Tous les textes dans des fichiers JSON de traduction (`messages/en.json`, `messages/fr.json`)

---

## 4. Architecture des Pages & Sections

### Page Unique (Landing Page / One-Page)

#### S1 — Navbar

- Logo Collègue (image `Logo-mcp-collegue.png`)
- Liens : Features, Outils, Intégration, Pricing (ancres)
- Sélecteur de langue (EN/FR)
- CTA : "Get Started" / "Commencer"
- Toggle dark/light mode

#### S2 — Hero

- **Titre** : "Your AI Development Colleague." / "Votre Collègue de Développement IA."
- **Sous-titre** : Explication concise (1-2 phrases) — un assistant MCP complet pour analyse, sécurité, refactoring, tests, intégrations
- **2 CTAs** : "Get Started" (lien GitHub/NPM) + "See Documentation"
- **4 Badges stats** :
  - `12` Outils MCP
  - `3+` Langages supportés
  - `30+` Patterns de secrets détectés
  - `30s` Setup time

#### S3 — Proposition de Valeur (The Power of Collègue)

3 cartes en grille :

1. **Analyse & Sécurité** — Scannez votre code pour secrets, vulnérabilités, incohérences et configs IaC dangereuses
2. **Productivité & Qualité** — Refactoring, documentation et tests unitaires générés automatiquement avec validation
3. **Intégrations DevOps** — PostgreSQL, GitHub, Sentry, Kubernetes intégrés nativement dans votre IDE

#### S4 — Outils MCP (MCP Tools) — Section principale

Affichage en grille de cartes (2-3 colonnes) pour les 12 outils, chacun avec :

- Icône/Emoji
- Nom de l'outil
- Description courte
- Tags (ex: "Security", "NEW")
- Hover avec description étendue

Regroupés en 3 sous-sections :

1. **Code Quality** (documentation, refactoring, test_generation, run_tests)
2. **Security & Analysis** (secret_scan, dependency_guard, impact_analysis, repo_consistency_check, iac_guardrails_scan)
3. **Integrations** (postgres_db, github_ops, sentry_monitor, kubernetes_ops)

#### S5 — Watchdog (Self-Healing Agent)

- Section dédiée avec schéma visuel du flux : Sentry → Watchdog → LLM → GitHub PR
- Liste des sécurités intégrées (validation AST, protection anti-destruction, fuzzy matching)
- Badge "Autonomous" / "Multi-Users"

#### S6 — Intégration (Installation)

- Tabs par méthode : **NPX**
- Blocs de code avec syntax highlighting et bouton "Copy"
- Sous-section "Configuration des intégrations" (tableau des variables env)
- Sous-section "IDEs compatibles" avec logos (Windsurf, Cursor, Claude Desktop)

**Contenu détaillé de la section S6 :**

```json
// Configuration NPX de base
{
  "mcpServers": {
    "collegue": {
      "command": "npx",
      "args": ["-y", "@collegue/mcp@latest"]
    }
  }
}
```

```json
// Configuration avec intégrations (PostgreSQL, GitHub, Sentry, Kubernetes)
{
  "mcpServers": {
    "collegue": {
      "command": "npx",
      "args": ["-y", "@collegue/mcp@latest"],
      "env": {
        "POSTGRES_URL": "postgresql://user:password@host:5432/database",
        "GITHUB_TOKEN": "ghp_xxxxxxxxxxxx",
        "SENTRY_AUTH_TOKEN": "sntrys_xxxxxxxxxxxx",
        "SENTRY_ORG": "my-organization",
        "SENTRY_URL": "https://sentry.io",
        "KUBECONFIG": "~/.kube/config"
      }
    }
  }
}
```

**Tableau des variables d'environnement :**

| Variable | Description | Outil |
|----------|-------------|-------|
| `POSTGRES_URL` | URI PostgreSQL (ou `DATABASE_URL`) | postgres_db |
| `GITHUB_TOKEN` | Token GitHub (permissions: repo, read:org) | github_ops |
| `SENTRY_AUTH_TOKEN` | Token d'authentification Sentry | sentry_monitor |
| `SENTRY_ORG` | Slug de l'organisation Sentry | sentry_monitor |
| `SENTRY_URL` | URL Sentry self-hosted (optionnel) | sentry_monitor |
| `KUBECONFIG` | Chemin vers kubeconfig (optionnel) | kubernetes_ops |

**Configuration Multi-Utilisateurs (Config Registry) :**

Le système supporte automatiquement plusieurs utilisateurs via le Config Registry :

1. **Enregistrement automatique** : Les configurations sont capturées lors de chaque requête MCP

2. **Surveillance multi-utilisateurs** : Le watchdog itère sur toutes les configurations actives

3. **Nettoyage automatique** : Les configurations inactives (> 48h) sont supprimées automatiquement

#### S7 — Pricing

**Modèle : Open-Core Freemium (3 tiers + option BYOK)**

Le code est MIT (open-source). On monétise le **service hébergé** (infra, LLM, Watchdog managé, SSO), pas le code.

| | **Community** | **Pro** | **Team** |
|---|---|---|---|
| **Prix** | **Gratuit** | **$19/mois** | **$49/mois/user** (min 3) |
| **Prix BYOK** | — | **$9/mois** | **$29/mois/user** |
| Outils analyse (9) | ✅ | ✅ | ✅ |
| Intégrations (4) | ❌ Self-host only | ✅ Hébergé | ✅ Hébergé |
| Watchdog | ❌ | ✅ 1 org Sentry | ✅ Orgs illimitées |
| Requêtes LLM | ❌ | 500/mois | Illimité |
| Multi-utilisateurs | ❌ | ❌ | ✅ Config Registry |
| SSO / OAuth | ❌ | ❌ | ✅ Keycloak managé |
| Support | Community (GitHub) | Email prioritaire | Prioritaire + SLA 99.9% |

**BYOK (Bring Your Own Key)** : L'utilisateur fournit sa propre clé API LLM (Google Gemini, etc.)

**Justification des montants :**
- $19/mois Pro : aligné avec GitHub Copilot ($19), Cursor Pro ($20), Snyk (~$25)
- $49/mois Team : aligné avec GitLab Premium, Sentry Team — SSO + multi-users justifient le premium
- BYOK : couvre les coûts d'infra uniquement (~$2/user/mois), marge ~78%

**Funnel de conversion :**
1. Community (gratuit) → adoption massive, GitHub stars, crédibilité
2. Pro ($19) → conversion quand le dev veut Watchdog ou intégrations sans gérer Docker
3. Team ($49) → upgrade quand l'équipe grandit (SSO, multi-users)

**Design de la section :**
- 3 cartes en grille avec le tier Pro mis en avant (badge "Popular")
- Toggle "Monthly / Annual" (réduction -20% annuel)
- Toggle "Managed LLM / BYOK" pour afficher les prix alternatifs
- Checkmarks dorés pour les features incluses
- CTA par tier : "Get Started" / "Start Free Trial" / "Contact Sales"

#### S8 — FAQ

Questions issues du `content.md` :

- Support OAuth/Keycloak ?
- Comment configurer le LLM ?
- Quels endpoints HTTP utiles ?
- Quels clients MCP compatibles ?
- Langages supportés ?

#### S9 — CTA Final

- Titre accrocheur : "Ready to upgrade your development workflow?"
- Sous-titre + bouton "Get Started for Free"

#### S10 — Footer

- Logo + tagline
- Colonnes : Product (Features, Outils, Pricing) | Resources (Docs, GitHub, NPM) | Legal (Privacy, Terms)
- Réseaux sociaux (GitHub, Discord, Twitter/X)
- Copyright © 2026 VynoDePal

---

## 5. Structure des Fichiers (Arborescence)

```
collegue-website/
├── public/
│   ├── logo.png
│   └── og-image.png
├── messages/
│   ├── en.json
│   └── fr.json
├── src/
│   ├── app/
│   │   ├── [locale]/
│   │   │   ├── layout.tsx
│   │   │   └── page.tsx
│   │   ├── layout.tsx
│   │   └── globals.css
│   ├── components/
│   │   ├── navbar.tsx
│   │   ├── hero.tsx
│   │   ├── value-proposition.tsx
│   │   ├── tools-section.tsx
│   │   ├── watchdog-section.tsx
│   │   ├── integration-section.tsx
│   │   ├── pricing-section.tsx
│   │   ├── faq-section.tsx
│   │   ├── cta-section.tsx
│   │   ├── footer.tsx
│   │   ├── language-switcher.tsx
│   │   ├── theme-toggle.tsx
│   │   └── ui/ (shadcn components)
│   ├── i18n/
│   │   ├── routing.ts
│   │   └── request.ts
│   └── lib/
│       └── utils.ts
├── next.config.ts
├── tailwind.config.ts
├── package.json
└── README.md
```

---

## 6. Étapes de Réalisation

1. **Initialiser** le projet Next.js 14 + TypeScript + Tailwind + shadcn/ui
2. **Configurer** next-intl (i18n EN/FR) + dark mode (CSS variables)
3. **Créer** les fichiers de traduction (`messages/en.json`, `messages/fr.json`)
4. **Développer** chaque section dans l'ordre : Navbar → Hero → Value Prop → Tools → Watchdog → Integration → FAQ → CTA → Footer
5. **Ajouter** les animations Framer Motion
6. **Intégrer** le syntax highlighting pour les blocs de code
7. **Tester** responsive (mobile-first) + accessibilité
8. **Déployer** sur Netlify

---

## 7. Questions Ouvertes

- **Pricing** : Modèle Open-Core Freemium retenu (3 tiers : Community gratuit, Pro $19/mois, Team $49/mois/user + option BYOK). À valider les limites exactes de requêtes LLM par tier.
- **Domaine** : As-tu un nom de domaine prévu (ex: collegue-mcp.com) ?
- **Logo** : On utilise le `Logo-mcp-collegue.png` existant tel quel ?
- **Contenu additionnel** : Souhaites-tu une page `/docs` séparée ou tout sur une seule landing page ?
