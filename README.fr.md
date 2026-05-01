# Google Merchant Center MCP

> 🇬🇧 An English version is available: **[README.md](./README.md)**

Un serveur [Model Context Protocol](https://modelcontextprotocol.io) qui expose
la [Google Merchant API](https://developers.google.com/merchant/api/reference/rest)
à Claude, Cursor et tout autre client compatible MCP.

Le serveur est **multi-utilisateur par défaut** : chaque utilisateur se
connecte avec son propre compte Google via OAuth 2.1, et les identifiants
sont stockés sur disque par utilisateur. Deux analystes qui interrogent le
même serveur ne voient strictement que leurs propres comptes Merchant Center.

Conçu et maintenu par **[Webloom](https://webloom.fr)** — agence de
search-marketing à Paris spécialisée en SEO, SEA et Google Shopping. Nous
l'utilisons quotidiennement pour brancher Merchant Center à nos workflows
LLM ; nous l'avons publié en open source pour que vous puissiez en faire
autant.

---

## Points clés

- **23 outils en lecture** couvrant comptes, sous-comptes, produits, flux
  de données, promotions, quotas, rendu localisé des problèmes, le DSL
  complet de Merchant Reports (l'équivalent du GAQL côté Ads) et plusieurs
  rapports prêts à l'emploi.
- **OAuth 2.1 avec Dynamic Client Registration** — fonctionne tel quel
  avec Claude Desktop / Claude.ai, Cursor et n'importe quel client MCP
  standard. Le serveur émet ses propres jetons MCP ; les identifiants
  Google ne quittent jamais le serveur.
- **Isolation par utilisateur** — chaque identité authentifiée a son propre
  fichier de credentials sous `GOOGLE_MCP_CREDENTIALS_DIR`. Caches et état
  OAuth sont indexés par adresse e-mail.
- **Lecture seule par défaut** — tout appel d'écriture qui n'est ni
  `:search` ni `:render*` est bloqué tant que vous ne mettez pas
  explicitement `GOOGLE_MERCHANT_READ_ONLY=0`.
- **Indépendant de l'hébergeur** — toute plateforme capable d'exécuter une
  application Python ASGI avec un répertoire persistant convient (Render,
  Fly.io, Railway, Heroku, AWS, GCP, bare metal, Docker, Kubernetes…).

---

## Ce que vous pouvez lui demander

| Outil | Rôle |
|---|---|
| `list_accounts` | Liste tous les comptes Merchant Center accessibles à l'utilisateur authentifié |
| `find_accounts(query)` | Recherche floue de comptes par **nom de marque, domaine ou ID partiel** — répondez à « quel est l'ID Merchant Center de `webloom.fr` ? » sans connaissance préalable |
| `list_subaccounts(account_id)` | Liste les sous-comptes d'un compte avancé (provider) |
| `get_account(account_id)` | Récupère un compte Merchant Center |
| `list_users(account_id)` | Liste les utilisateurs ayant accès à un compte |
| `list_programs(account_id)` | Liste les programmes (Free Listings, Shopping Ads…) actifs |
| `list_regions(account_id)` | Liste les régions de livraison configurées |
| `get_shipping_settings(account_id)` | Services de livraison + groupes de tarifs |
| `get_business_info(account_id)` | Adresse, téléphone, contacts service client |
| `list_products(account_id)` | Une page de produits du compte |
| `get_product(account_id, product_name)` | Récupère un produit par son resource name |
| `list_data_sources(account_id)` | Liste des flux / sources de données |
| `get_data_source(account_id, data_source_id)` | Récupère une source de données |
| `list_file_uploads(account_id, data_source_id)` | Statut du dernier upload pour une source |
| `aggregate_product_statuses(account_id)` | Synthèse des produits approuvés / en attente / refusés |
| `render_account_issues(account_id)` | Problèmes du compte (rendus localisés) |
| `render_product_issues(account_id, product_name)` | Problèmes d'un produit (rendus localisés) |
| `run_merchant_query(account_id, query)` | Lance une requête Merchant Reports arbitraire |
| `get_top_products(account_id, days)` | Rapport « top produits » prêt à l'emploi |
| `get_price_competitiveness(account_id)` | Rapport de benchmark prix vs. concurrents |
| `get_best_sellers(account_id, country)` | Rapport best-sellers par pays |
| `list_promotions(account_id)` | Liste des promotions configurées |
| `get_promotion(account_id, promotion_name)` | Récupère une promotion |
| `list_quotas(account_id)` | Quotas API utilisés / restants par groupe de méthodes |

S'y ajoutent une ressource MCP `merchant-reports://reference` et deux prompts
(`google_merchant_workflow`, `merchant_reports_help`).

---

## Architecture

```mermaid
flowchart TB
    User(Utilisateur) -->|Discute avec| Claude
    Claude(Claude / Cursor / etc.) -->|MCP via HTTP| MCP[Serveur MCP Merchant]

    subgraph "Serveur MCP"
      FastMCP[Runtime FastMCP]
      Tools[Outils Merchant API]
      Auth[Provider OAuth 2.1]
      State[État OAuth persistant /<br/>credentials par utilisateur]

      FastMCP -->|expose| Tools
      FastMCP -->|utilise| Auth
      Auth --> State
    end

    subgraph "Google"
      Consent[Écran de consentement Google]
      Merchant[Merchant API<br/>merchantapi.googleapis.com]
    end

    Auth -->|DCR + redirection| Consent
    Tools -->|Bearer par utilisateur| Merchant
    Merchant -->|JSON| Tools
```

Déroulé d'une première connexion :

1. Le client interroge `/.well-known/oauth-protected-resource` et découvre
   le serveur d'autorisation.
2. Le client effectue un **Dynamic Client Registration** sur `/register`.
3. L'utilisateur passe par l'écran de consentement Google pour le scope
   `https://www.googleapis.com/auth/content`.
4. Google redirige vers `/oauth2callback`, qui émet un couple
   access + refresh MCP et stocke les credentials Google de l'utilisateur.
5. Tous les appels `/mcp` suivants s'authentifient avec le jeton MCP ; le
   serveur retrouve les credentials Google en interne.

---

## 1. Configuration Google Cloud Console (une fois)

1. **Activez la Merchant API** : *APIs & Services → Library → "Merchant API"
   → Enable*.
2. **Écran de consentement OAuth** :
   - Type d'utilisateur : External (ou Internal pour Workspace seulement).
   - Ajoutez le scope `https://www.googleapis.com/auth/content`.
   - Ajoutez votre adresse comme test user.
   - Soumettez à la vérification avant d'aller en production (Google exige
     une vérification pour `auth/content` ; comptez 3-5 jours ouvrés).
3. **Créez un Client ID OAuth 2.0** (type : Web application) :
   - URIs de redirection autorisées :
     - `http://localhost:8000/oauth2callback` (dev local)
     - `https://<votre-host-public>/oauth2callback` (production)
   - Conservez le **Client ID** et le **Client Secret**.
4. **Accès Merchant Center** : le compte Google qui s'authentifie doit déjà
   avoir accès aux comptes Merchant Center cibles (compte avancé ou
   sous-comptes). L'accès tiers se gère sur <https://merchants.google.com/>.

> La Merchant API n'accepte que des **credentials utilisateur OAuth**. Pas
> de `developer-token`, et les comptes de service ne fonctionnent qu'en
> impersonation d'un utilisateur Merchant Center (rarement utile) — restez
> sur OAuth.

---

## 2. Développement local

```bash
git clone https://github.com/<vous>/merchant-center-mcp.git
cd merchant-center-mcp
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Renseignez GOOGLE_OAUTH_CLIENT_ID / SECRET et
#   GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/oauth2callback
python merchant_server.py
```

Vérifiez les métadonnées OAuth :

```bash
curl http://localhost:8000/.well-known/oauth-authorization-server | jq
curl http://localhost:8000/.well-known/oauth-protected-resource | jq
```

`scopes_supported` doit contenir `https://www.googleapis.com/auth/content`
et **pas** `https://www.googleapis.com/auth/adwords`.

Pour tester le flux complet, déclarez le serveur dans Cursor ou Claude
Desktop (cf. §4) et laissez le client gérer le DCR.

---

## 3. Déploiement sur n'importe quel hébergeur

Le serveur est une application Python ASGI standard :

- **Entrypoint :** `merchant_server:app`
- **Serveur ASGI :** `uvicorn` (déjà dans `requirements.txt`)
- **Runtime requis :** Python 3.11+
- **Variables d'environnement requises :** voir §6
- **Disque requis :** un répertoire inscriptible pour
  `GOOGLE_MCP_CREDENTIALS_DIR`, qui survit aux redéploiements (les jetons
  OAuth par utilisateur et le registre des clients DCR y vivent). Sans ça,
  chaque redéploiement déconnecte tous vos utilisateurs.

Commande de lancement générique :

```bash
uvicorn merchant_server:app --host 0.0.0.0 --port "${PORT:-8000}"
```

Quelques recettes pour les hébergeurs courants. Aucune n'est obligatoire —
choisissez ce qui colle à votre stack.

### 3.a — Render (web service + disque persistant)

| Champ | Valeur |
|---|---|
| Runtime | Python 3.11 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn merchant_server:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/.well-known/oauth-protected-resource` |
| Persistent Disk | mount path `/data`, taille `1 GB` |
| `GOOGLE_MCP_CREDENTIALS_DIR` | `/data/credentials` |

Un `render.yaml` déclaratif est inclus dans le repo.

### 3.b — Fly.io

```bash
fly launch --no-deploy --copy-config
fly volumes create data --size 1 --region cdg     # même région que l'app
# Dans fly.toml, ajoutez :
#   [[mounts]]
#     source = "data"
#     destination = "/data"
fly secrets set MCP_ENABLE_OAUTH21=true \
                GOOGLE_OAUTH_CLIENT_ID=... \
                GOOGLE_OAUTH_CLIENT_SECRET=... \
                GOOGLE_OAUTH_REDIRECT_URI=https://<app>.fly.dev/oauth2callback \
                MERCHANT_MCP_EXTERNAL_URL=https://<app>.fly.dev \
                GOOGLE_MCP_CREDENTIALS_DIR=/data/credentials
fly deploy
```

### 3.c — Railway

Start command :
`uvicorn merchant_server:app --host 0.0.0.0 --port $PORT`. Attachez un
Volume monté sur `/data` et configurez les variables de §6.

### 3.d — Docker (VPS, ECS, GKE, AKS, Cloud Run, Fly machines, etc.)

Le repo n'embarque pas de Dockerfile pour rester neutre, mais ce fichier
de 8 lignes suffit :

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV GOOGLE_MCP_CREDENTIALS_DIR=/data/credentials
EXPOSE 8000
CMD ["sh", "-c", "uvicorn merchant_server:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

Montez un volume sur `/data` (Docker volume nommé, EBS, GCE PD, EFS, PVC
Kubernetes — au choix) et déclarez les variables de §6.

### 3.e — Bare metal / systemd

Clonez le repo sur votre serveur, installez avec `pip`, lancez via
`systemd` ou `supervisord`, mettez nginx/Caddy/Traefik devant, et pointez
`GOOGLE_MCP_CREDENTIALS_DIR` vers n'importe quel dossier local que vous
sauvegardez.

### 3.f — Limitation serverless (Vercel, AWS Lambda…)

Le flux OAuth stocke les credentials et clients DCR sur disque pour que les
redémarrages ne fassent pas perdre les sessions. **Les plateformes serverless
sans état ne collent pas à ce modèle nativement.** Si vous devez tourner sur
Vercel / Lambda / Cloud Functions, branchez un `CredentialStore` externe
(voir `auth/credential_store.py` — c'est une `ABC`) adossé à Redis, Postgres,
DynamoDB, S3, etc. Pas fourni ici, mais ~50 lignes de code.

### 3.g — Mettez à jour le client OAuth Google

Quel que soit l'hébergeur, ajoutez son URL publique aux **Authorized
redirect URIs** du client OAuth :

```
https://<votre-host-public>/oauth2callback
```

Sans ça, Google rejette le redirect au moment du consentement avec
`redirect_uri_mismatch`.

---

## 4. Branchement aux clients MCP

Quand `MCP_ENABLE_OAUTH21=true`, le client effectue un Dynamic Client
Registration sur le `registration_endpoint` du serveur, redirige l'utilisateur
vers Google pour le consentement, puis stocke le jeton MCP émis. Les
credentials Google ne quittent jamais le serveur. Aucun secret n'est requis
côté client.

### 4.a — Claude Desktop

Éditez le fichier de config Claude (créez-le s'il n'existe pas) :

| OS | Chemin |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

Ajoutez l'entrée `googleMerchant` :

```json
{
  "mcpServers": {
    "googleMerchant": {
      "type": "http",
      "url": "https://<votre-host-public>/mcp"
    }
  }
}
```

Redémarrez Claude Desktop, ouvrez une nouvelle conversation : à la première
question liée à Merchant Center, Claude affiche la fenêtre OAuth Google.
Connectez-vous avec le compte Google qui possède (ou a accès à) votre
Merchant Center.

### 4.b — Claude.ai (web) — connecteur MCP custom

Paramètres → **Connecteurs** → **Ajouter un connecteur custom** →

- **Nom :** `Google Merchant`
- **URL du serveur MCP distant :** `https://<votre-host-public>/mcp`

Sauvegardez. Claude.ai vous guide à travers le flux OAuth lors de la
première utilisation.

### 4.c — Cursor

Cursor lit soit une config locale au projet (`.cursor/mcp.json`), soit
votre config utilisateur (`~/.cursor/mcp.json`).

```json
{
  "mcpServers": {
    "googleMerchant": {
      "type": "http",
      "url": "https://<votre-host-public>/mcp"
    }
  }
}
```

Ensuite : Cursor Settings → **MCP** → activez `googleMerchant`. Le bouton
"Connect" déclenche le flux OAuth.

### 4.d — Autres clients MCP (apps custom, OpenAI Apps SDK, etc.)

Tout client supportant le transport MCP **Streamable HTTP** avec
autorisation OAuth 2.1 fonctionne de la même façon — pointez-le sur
`https://<votre-host-public>/mcp`. La découverte se fait via
`/.well-known/oauth-protected-resource` et le Dynamic Client Registration
sur le `registration_endpoint`.

### 4.e — Une fois connecté : que demander ?

Essayez ces prompts (aucun setup additionnel après l'auth) :

- *« Trouve le compte Merchant Center pour webloom.fr »* → appelle
  `find_accounts(query="webloom.fr")` et renvoie l'ID correspondant.
- *« Liste mes comptes Merchant Center »* → `list_accounts`.
- *« Combien de produits sont en attente ou refusés sur le compte
  123456789 ? »* → `aggregate_product_statuses`.
- *« Montre-moi les 20 meilleurs produits par clics sur les 30 derniers
  jours pour le compte 123456789 »* → `get_top_products`.
- *« Affiche les problèmes au niveau du compte 123456789 en français »* →
  `render_account_issues(language_code="fr")`.
- *« Exécute cette requête Merchant Reports sur le compte 123456789 :
  SELECT segments.offer_id, metrics.clicks, metrics.impressions FROM
  productPerformanceView WHERE segments.date BETWEEN '2026-04-01' AND
  '2026-04-30' ORDER BY metrics.clicks DESC LIMIT 20 »* →
  `run_merchant_query`.
- *« Compare les benchmarks de prix vs concurrents aux US pour le compte
  123456789 »* → `get_price_competitiveness`.
- *« Quelles promotions sont actives sur le compte 123456789 ? »* →
  `list_promotions`.

Si vous avez défini `DEFAULT_MERCHANT_ACCOUNT_ID` côté serveur, vous
pouvez omettre l'ID dans tous les prompts ci-dessus.

### 4.f — Stdio local (dev mono-utilisateur)

Pour un setup dev mono-utilisateur, headless (sans flux OAuth), pointez
le client directement sur le script :

```json
{
  "mcpServers": {
    "googleMerchant": {
      "command": "/abs/chemin/.venv/bin/python",
      "args": ["/abs/chemin/merchant_server.py"],
      "env": {
        "MCP_ENABLE_OAUTH21": "false",
        "GOOGLE_MERCHANT_AUTH_TYPE": "oauth",
        "GOOGLE_OAUTH_CLIENT_ID": "...",
        "GOOGLE_OAUTH_CLIENT_SECRET": "...",
        "GOOGLE_MERCHANT_REFRESH_TOKEN": "...",
        "GOOGLE_MERCHANT_READ_ONLY": "1"
      }
    }
  }
}
```

Générez une fois un refresh token via `InstalledAppFlow` (ou tout autre
helper OAuth out-of-band) pour que les exécutions suivantes soient
headless.

---

## 5. Checklist de validation

- [ ] `pip install -r requirements.txt` propre
- [ ] `python merchant_server.py` démarre sans exception en local
- [ ] `GET /.well-known/oauth-authorization-server` renvoie les métadonnées RFC 8414
- [ ] `GET /.well-known/oauth-protected-resource` renvoie les métadonnées de la ressource pointant sur la même base URL
- [ ] Le `registration_endpoint` accepte `POST` avec un body JSON vide et renvoie un client enregistré (par défaut `<base>/register`)
- [ ] Le DCR + auth flow depuis Cursor amène sur l'écran Google n'affichant **que** les scopes `auth/content` + `openid email profile` (pas d'`adwords`)
- [ ] Après consentement, `list_accounts` renvoie les comptes Merchant Center de l'utilisateur
- [ ] `run_merchant_query(account_id, "SELECT product_view.id, product_view.title FROM productView LIMIT 5")` renvoie des lignes
- [ ] Un redémarrage du serveur **ne force pas** la reconnexion (vérifier `<credentials_dir>/mcp_oauth/server_state.json`)
- [ ] Un appel à un (futur) outil d'écriture avec `GOOGLE_MERCHANT_READ_ONLY=1` lève `PermissionError`
- [ ] Deux comptes Google différents qui interrogent le même serveur voient des `list_accounts` différents

---

## 6. Variables d'environnement

Voir `.env.example` pour la liste complète et les commentaires inline. Les
plus importantes :

| Variable | Rôle |
|---|---|
| `MCP_ENABLE_OAUTH21` | `true` pour activer le flux OAuth 2.1 multi-utilisateur |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | Le client OAuth Google Cloud utilisé pour parler à Google |
| `GOOGLE_OAUTH_REDIRECT_URI` | L'URI de redirection enregistrée chez Google (`<base>/oauth2callback`) |
| `MERCHANT_MCP_EXTERNAL_URL` | URL publique du serveur (utilisée derrière les reverse proxies) |
| `GOOGLE_MCP_CREDENTIALS_DIR` | Répertoire des fichiers JSON de credentials par utilisateur (doit être persistant) |
| `MCP_OAUTH_STATE_PERSIST` | `true` pour persister les clients DCR + jetons MCP entre redémarrages |
| `MCP_ACCESS_TOKEN_TTL_SECONDS` | TTL des access tokens MCP (défaut 3600) |
| `MCP_REFRESH_TOKEN_TTL_SECONDS` | TTL des refresh tokens MCP (défaut 30 jours) |
| `GOOGLE_MERCHANT_READ_ONLY` | `1` (défaut) bloque tout appel non `:search` / non `:render*` |
| `DEFAULT_MERCHANT_ACCOUNT_ID` | Compte par défaut, pour omettre `account_id` dans les appels |
| `MCP_HTTP_PATH` | Chemin du transport HTTP streamable (défaut `/mcp`) |
| `MCP_CORS_ORIGINS` | Allow-list CORS séparée par virgules. Vide / non défini = `*` |
| `MERCHANT_HTTP_TIMEOUT_SECONDS` | Timeout sortant pour les appels Merchant API (défaut 30) |
| `MCP_BEARER_TOKEN` | Garde-fou transport mono-secret, **uniquement** quand OAuth 2.1 est désactivé |

Vous pouvez aussi figer la version d'une sous-API (par ex. la passer en
`v1`) via :

```
MERCHANT_ACCOUNTS_API_VERSION
MERCHANT_PRODUCTS_API_VERSION
MERCHANT_DATASOURCES_API_VERSION
MERCHANT_ISSUERESOLUTION_API_VERSION
MERCHANT_REPORTS_API_VERSION
MERCHANT_PROMOTIONS_API_VERSION
MERCHANT_QUOTA_API_VERSION
```

---

## 7. DSL Merchant Reports

La sous-API Reports parle le **Merchant Reports Query Language**, un DSL
SQL-like distinct du GAQL mais conceptuellement proche. Vues principales :

- `productPerformanceView` – clics / impressions / conversions par produit
- `nonProductPerformanceView` – performance non attribuable à un produit
- `productView` – snapshot du catalogue (titre, marque, prix, problèmes)
- `priceCompetitivenessProductView` – benchmarks prix vs. concurrents
- `priceInsightsProductView` – prix conseillé + uplift projeté
- `bestSellersProductClusterView` – clusters de produits best-sellers
- `bestSellersBrandView` – marques best-sellers
- `competitiveVisibilityCompetitorView` – part d'impression vs. concurrents

Utilisez `run_merchant_query` pour les requêtes libres, ou les wrappers
(`get_top_products`, `get_price_competitiveness`, `get_best_sellers`) pour
les rapports courants. Le serveur expose aussi une ressource de référence
`merchant-reports://reference` et le prompt `merchant_reports_help`.

---

## 8. Notes de sécurité

Le serveur hérite de la posture OAuth 2.1 du MCP, mais voici la synthèse
de la conception et les compromis à connaître.

**Ce que le serveur fait bien**

- **OAuth 2.1 avec PKCE (S256)** côté MCP ; PKCE est *obligatoire* quand
  `MCP_ENABLE_OAUTH21=true`.
- **Les jetons MCP sont émis par le serveur**, opaques, avec des TTL
  configurables (1 h access / 30 j refresh par défaut). Les refresh tokens
  sont rotés à chaque utilisation.
- **Les credentials Google ne quittent jamais le serveur.** Les clients MCP
  ne voient que le bearer MCP ; le serveur garde les access/refresh
  Google.
- **Isolation par utilisateur.** Credentials, caches et état OAuth sont
  indexés par l'e-mail Google vérifié ; un utilisateur ne peut jamais lire
  les données d'un autre.
- **Path-traversal bloqué dans le credential store.** Les noms de fichiers
  sont sanitisés et le chemin résolu doit rester sous le répertoire de
  base.
- **Vérification de l'e-mail.** Le claim `email_verified` du id_token
  Google doit valoir `true` pour que le serveur considère l'e-mail comme
  authoritative.
- **Lecture seule par défaut.** Les endpoints Merchant API mutants sont
  bloqués au niveau du wrapper HTTP tant que `GOOGLE_MERCHANT_READ_ONLY`
  n'est pas mis à `0`.
- **Timeout HTTP sortant** (`MERCHANT_HTTP_TIMEOUT_SECONDS`, défaut 30 s)
  empêche une connexion bloquée d'immobiliser un worker.
- **State signé.** Le paramètre `state` du redirect de consentement est
  généré avec `secrets.token_urlsafe(32)` et validé au callback (usage
  unique ; un replay renvoie `invalid_request`).

**Compromis à connaître**

- **Les jetons au repos sont du JSON non chiffré** sous
  `GOOGLE_MCP_CREDENTIALS_DIR`, en `0600`. Quiconque a accès au disque de
  l'hôte peut lire tous les jetons stockés. Traitez ce répertoire comme un
  coffre à secrets et sauvegardez-le en conséquence. Pour du chiffrement
  au repos, échangez `LocalDirectoryCredentialStore` contre une
  implémentation chiffrante (la classe abstraite est dans
  `auth/credential_store.py`).
- **La signature de l'id_token n'est volontairement pas vérifiée**
  pendant le callback Google, parce que le token vient directement de
  Google sur TLS dans la même requête — c'est un pattern OAuth
  server-side standard. Le chemin alternatif qui passe par le réseau
  (`GoogleRemoteAuthProvider`, utilisé seulement si vous l'activez)
  vérifie via le tokeninfo de Google et exige désormais
  `email_verified=true`.
- **CORS par défaut sur `*`** pour faciliter le premier branchement d'un
  client ; mettez `MCP_CORS_ORIGINS` en production pour restreindre.
- **La validation du Host est volontairement permissive** pour supporter
  les déploiements derrière reverse-proxy. L'authentification est faite à
  la couche OAuth ; n'exposez pas le serveur sans OAuth en production. Si
  vous y êtes contraint, mettez `MCP_BEARER_TOKEN` à une longue chaîne
  aléatoire comme garde-fou de transport.
- **Aucun rate-limit par IP n'est intégré.** Mettez le serveur derrière
  un CDN / WAF / reverse proxy capable de gérer l'abus si vous l'exposez
  publiquement.
- **Le DCR est ouvert** par défaut (n'importe quel client peut
  s'enregistrer). C'est by design pour MCP, mais ça veut dire que toute
  personne pouvant joindre le serveur peut lancer la danse OAuth.
  Combinez avec un réseau privé ou un secret transport si c'est un
  problème.
- **Le filtre lecture seule est heuristique**, pas exhaustif : il
  whiteliste `GET`, `:search`, `:render*`, `:listSubaccounts` et
  `:aggregateProductStatuses`. Auditer ce filtre avant d'ajouter des
  outils mutants.

Pour signaler une vulnérabilité : **security@webloom.fr**.

---

## 9. Références utiles

- Racine API : `https://merchantapi.googleapis.com`
- Index de référence : <https://developers.google.com/merchant/api/reference/rest>
- Méthode reports.search :
  <https://developers.google.com/merchant/api/reference/rest/reports_v1beta/accounts.reports>
- Guide des scopes / accès :
  <https://developers.google.com/merchant/api/guides/authorization/access-client-accounts>
- Vue d'ensemble du DSL Reports :
  <https://developers.google.com/merchant/api/guides/reports/overview>
- Model Context Protocol : <https://modelcontextprotocol.io>

---

## À propos de Webloom

Ce serveur MCP est conçu et maintenu par **[Webloom](https://webloom.fr)**,
agence de search-marketing à Paris. Nous concevons et pilotons des
campagnes SEO, SEA et Google Shopping pour des marques B2C et B2B
européennes, et nous développons l'outillage LLM (comme celui-ci) qui
nous permet de produire des analyses plus rapides et plus précises que
les tableurs des autres agences.

Si vous voulez de l'aide pour brancher ce serveur dans votre stack, tirer
plus de valeur de votre Merchant Center, ou faire développer un MCP
custom pour une autre API marketing, écrivez-nous via
<https://webloom.fr>.

---

## Licence

[MIT](./LICENSE).
