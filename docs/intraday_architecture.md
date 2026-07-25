# Architecture intraday « two-speed » à blocker déterministe

> **Statut** : document d'analyse et de spécification. **Aucun code n'est encore écrit.**
> **Portée** : évaluation de RMATS (arXiv 2605.25311) comme base d'un système de trading
> intraday piloté par *interactive blockers* via API broker, et spécification de
> l'architecture qui survit à une revue adversariale.
> **Posture de risque retenue** : *agressif borné* — maximiser l'exposition **sous** un
> garde-fou déterministe dur (vol-target élevé + hard gate).

> ⚠️ **Avertissement.** Ce document est une analyse technique et opérationnelle. Il ne
> constitue ni un conseil en investissement, ni un conseil fiscal ou juridique. Les règles
> citées (TTF, PDT, éligibilité des brokers) évoluent et dépendent de la situation
> individuelle : elles doivent être confirmées auprès des sources réglementaires et du
> broker avant toute mise en œuvre. TradingAgents est un cadre de recherche ; les
> performances passées ou publiées ne préjugent d'aucun résultat futur.

---

## 1. Contexte et objectif

TradingAgents est aujourd'hui un système **mono-ticker, journalier**, produisant une
décision Buy/Hold/Sell via un graphe LangGraph d'agents LLM.

L'objectif exploré ici : s'inspirer de RMATS pour bâtir un système **multi-agents
intraday** piloté par des **interactive blockers via API broker**, visant le rendement
maximal — mais **survivable**, c'est-à-dire à risque borné par un garde-fou déterministe.

Ce document (a) restitue ce qu'est réellement RMATS, (b) démontre pourquoi une
transposition littérale à l'intraday échoue, (c) spécifie l'architecture défendable,
(d) fixe les choix d'infrastructure depuis la France, (e) donne un plan d'action phasé.

---

## 2. Ce qu'est réellement RMATS

**Titre** : *Recursive Multi-Agent Trading System: Iterative Optimized Portfolio Strategy
Under Geopolitical Uncertainty* — arXiv 2605.25311.

| Élément | Contenu |
|---|---|
| Agents | Sentiment, Report, Analysis, Risk + **Manager récursif** |
| Coordination | Messages typés ; protocole récursif à convergence garantie, `‖w_r − w_{r−1}‖₂ < ε` (ε ≈ 0,005), borné à ~3 rounds |
| Univers | 24 actifs multi-classes (ETF sectoriels, obligations, or, international) |
| Interactive Blocker | Hard-shift vers actifs défensifs (TLT, IEF, GLD, LQD…) au franchissement d'un seuil : drawdown (~−6 %), risque géopolitique (GPR normalisé), ratio de volatilité. **Priorité absolue** : bypass de toute coordination |
| Signal géopolitique | Indice **GPR (Caldara–Iacoviello)** — **mensuel**, publié avec ~1 mois de retard |
| Cadence | **Journalière** — backtest sur **561 jours de bourse, jan. 2023 → mars 2025** |
| Résultat phare | **Max drawdown 9,62 %** vs MVO 15,49 % et FinBERT 15,28 % ; meilleur DD d'événement dans **3 scénarios géopolitiques / 5** |

### Deux constats déterminants

1. **La contribution démontrée est défensive.** RMATS réduit le drawdown. Le papier
   **ne démontre pas** de surperformance en rendement.
2. **Il n'existe aucun dépôt de code public RMATS.** Le dépôt
   [`RecursiveMAS/RecursiveMAS`](https://github.com/RecursiveMAS/RecursiveMAS)
   (arXiv 2604.25917) est un **travail distinct** : raisonnement multi-agents en espace
   latent, **sans aucun code de trading**. Les résultats RMATS sont donc **non
   reproductibles** en l'état.

**Conséquence** : le cœur de RMATS est un mécanisme **de protection basse-fréquence**,
pas un moteur de rendement intraday.

---

## 3. Analyse adversariale : pourquoi « blockers RMATS pour intraday max-risque » échoue

Chaque ligne est un **motif de re-conception**, pas un détail d'implémentation.

| # | Faille | Gravité | Implication |
|---|---|---|---|
| A1 | **Mismatch de fréquence.** GPR mensuel (+1 mois de lag), DD 20 j, vol 252 j : **zéro information intraday** | Rédhibitoire | Le blocker RMATS **ne peut pas** piloter de l'intraday tel quel |
| A2 | **Latence LLM.** Coordination récursive = secondes→minutes × ~3 rounds ; l'alpha intraday décroît en secondes | Rédhibitoire | Le LLM **ne peut pas** être dans le chemin d'exécution |
| A3 | **Objectif inversé.** RMATS gagne en drawdown, pas en rendement ; « max risque » jette le seul edge prouvé | Élevée | La preuve empirique ne soutient pas l'objectif |
| A4 | **Backtest non transférable.** 561 barres journalières ne disent rien de l'intraday, où **spread / slippage / latence / frais** dominent le P&L | Élevée | Fort turnover × max-risque ⇒ les coûts effacent l'edge |
| A5 | **Infra absente.** yfinance = EOD/retardé ; pas de flux temps réel, pas d'OMS, pas de fills partiels | Élevée | Une couche **API marché + exécution** est à construire |
| A6 | **Sémantique du blocker.** Un blocker **plafonne** le risque ; « max risque » le libère — contradiction directe | Élevée | « Agressif » doit être **borné par** le gate, jamais illimité |
| A7 | **Réglementaire / opérationnel.** PDT, marge, rate-limits API, TTF, fiscalité du turnover | Moyenne | Contraintes dures à encoder dès le design (§5) |
| A8 | **Math de la ruine.** Risque max sans plafond (Kelly / vol-target) ⇒ ruine ; un gate **LLM (lent)** ne se déclenche pas à temps | Élevée | Le gate doit être **déterministe et sub-ms** |
| A9 | **Reproductibilité.** Pas de code RMATS + non-déterminisme LLM (le README du projet l'énonce lui-même) | Moyenne | Risque de modèle élevé ⇒ **paper-trading obligatoire** |

**Verdict.** On conserve l'idée « interactive blocker via API » et le comité multi-agents,
mais on **inverse leur emploi** : le blocker devient un garde-fou **déterministe rapide**,
les LLM une couche **lente de contexte**.

---

## 4. Architecture retenue : « two-speed »

Principe directeur : **séparer la décision lente (LLM, contexte) de l'exécution rapide
(déterministe, sûre)**. Le LLM n'émet jamais d'ordre ; le gate n'appelle jamais de LLM.

```
┌──────────────── VOIE LENTE (minutes / pré-ouverture / horaire) ────────────────┐
│  Comité multi-agents LLM — héritage RMATS, réutilise TradingAgents/LangGraph   │
│    Sentiment · Report · Analysis · Risk  ──►  Manager récursif (‖Δw‖₂ < ε)     │
│                                                                                │
│  SORTIE : régime + watchlist + BUDGET DE RISQUE     ⚠️ n'émet aucun ordre      │
└───────────────────────────────────┬────────────────────────────────────────────┘
                                    │ (régime, watchlist, budget_risque, vol_target)
                                    ▼
┌──────────────── VOIE RAPIDE (temps réel, déterministe, zéro LLM) ──────────────┐
│  Signal intraday (règles / quant)                                              │
│            └──► Sizer : vol-target borné, plafonné par Kelly fractionnaire     │
│                        │                                                       │
│                        ▼                                                       │
│   ★ INTERACTIVE BLOCKER — pre-trade check + monitoring continu ★               │
│      • perte journalière max          → flatten + halt du jour                 │
│      • stop par position / par trade                                           │
│      • exposition brute/nette max, cap de concentration par symbole            │
│      • vol-target (redimensionne selon la vol réalisée)                        │
│      • garde-fous marché : prix périmé, spread trop large, gap → BLOCK         │
│      • rate-limit API, kill-switch global                                      │
│                        │ (ordre autorisé)                                      │
│                        ▼                                                       │
│   Adaptateur Broker API — IBKR (PAPER d'abord) : OMS, fills partiels,          │
│   reconciliation de position                                                   │
└────────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Le blocker déterministe (cœur du système)

- **Pur Python, aucune I/O LLM, sub-milliseconde.**
- Appelé à deux endroits : **(1) pré-trade** — autorise ou refuse chaque ordre ;
  **(2) monitoring continu** — déclenche flatten / halt.
- **Priorité absolue** (héritage RMATS) : au franchissement d'un seuil dur, il
  **court-circuite** toute décision amont.
- Interface pressentie :
  `evaluate(portfolio_state) -> Decision(allow: bool, reasons: list[str], forced_action: Order | None)`
  retournant un **patch d'état** (et non un tuple positionnel — cohérence avec les
  conventions du repo).
- **Emplacement** : nouveau package `tradingagents/execution/` — **surtout pas**
  `agents/risk_mgmt/`, qui ne contient que des *factories* de nœuds LLM
  (`create_*_debator(llm)`) et dont ce module romprait le contrat implicite.

### 4.2 Posture « agressif borné »

| Levier | Réglage |
|---|---|
| Vol-target | **Élevé** (p. ex. 25–40 % annualisé) : le sizer *augmente* l'exposition pour atteindre la cible… |
| Plafond dur | …**mais** plafonné par le gate : perte journalière max (p. ex. −3 à −5 % de l'equity → flatten + halt), stop par trade, exposition brute max (levier borné), cap de concentration |
| Taille maximale | **Kelly fractionnaire** comme borne supérieure, même en mode agressif (anti-ruine) |

« Max profit » se lit donc **maximiser sous contrainte**, jamais levier illimité.

### 4.3 La voie lente (réutilisation de l'existant)

Réutilise le graphe LangGraph et les agents actuels ; ajoute un **Manager récursif**
(protocole `‖Δw‖₂ < ε`) produisant un **budget de risque + watchlist + régime** consommés
par la voie rapide. Gaté derrière une clé `mode` dont la valeur par défaut reste
`"classic"` → **100 % de compatibilité** avec le comportement existant.

---

## 5. Infrastructure depuis la France

### 5.1 LLM : modèles chinois / OpenRouter

Le repo supporte **déjà nativement** DeepSeek, Qwen (DashScope), GLM (Zhipu), MiniMax et
**OpenRouter**. Dans l'architecture two-speed le LLM est **exclusivement en voie lente**
(cadence minutes) : latence et variabilité de ces fournisseurs **n'impactent pas**
l'exécution.

- **Recommandé** : **OpenRouter** comme passerelle unique (une clé, accès DeepSeek / Qwen /
  GLM, fallback entre modèles) → `llm_provider="openrouter"`.
- **Option coût minimal** : DeepSeek ou Qwen en direct.
- ⚠️ Ne pas transmettre de données personnelles à un endpoint hébergé hors UE ; pour de
  l'analyse de marché publique, l'enjeu est neutre.

### 5.2 Broker : IBKR

Pour du retail algorithmique couvrant **US + Euronext** depuis la France, IBKR est
l'option de référence.

| Broker | Résident FR | US | Euronext / CAC40 | API |
|---|---|---|---|---|
| **IBKR Pro** | ✅ | ✅ | ✅ | TWS / Gateway + Web API REST ; lib `ib_async` |
| Saxo | ✅ | ✅ | ✅ | OpenAPI REST, frais plus élevés |
| Alpaca | ❌ *(comptes US — à vérifier)* | ✅ | ❌ | REST |
| Trading212 / DEGIRO / Bourse Direct | ✅ | partiel | ✅ | ❌ pas d'API officielle |

➡️ **Décision : IBKR Pro, en paper-trading d'abord**, via `ib_async` (ex-`ib_insync`).

### 5.3 « Marché US à zéro frais » : non applicable

Les offres **0 commission** sur actions US (**IBKR Lite**, Alpaca) sont **réservées aux
résidents américains**. Depuis la France, le compte est **IBKR Pro** : commissions **très
faibles mais non nulles** (ordre de grandeur ~0,0035 $/action, minimum ~1 $/ordre —
à confirmer sur la grille tarifaire en vigueur).

Ce n'est pas bloquant : ces coûts restent assez bas pour laisser vivre un edge réel. Le
backtest de la phase P2 doit modéliser **l'ensemble** des coûts :

- commissions IBKR Pro ;
- **spread + slippage** — les véritables postes dominants en intraday ;
- conversion EUR/USD (~0,2 bps chez IBKR, négligeable) ;
- **contrainte PDT** : le *pattern day trader* impose ≥ 25 000 $ d'équité sur un compte
  marge US. **Son applicabilité dépend de l'entité IBKR de rattachement** (IBIE Irlande
  vs IBLLC US) et **doit être vérifiée** auprès du broker.

### 5.4 Concentration CAC40 vs marché US

Pour de l'**intraday**, le marché **US est nettement supérieur** : liquidité et volatilité
plus élevées, spreads plus serrés → un edge y survit aux coûts, ce qui est rarement le cas
sur les mid-caps d'Euronext.

Deux spécificités françaises pèsent sur l'arbitrage :

- **TTF (taxe sur les transactions financières) : 0,3 % à l'achat** de toute action
  française dont la capitalisation dépasse 1 Md€ — donc **tout le CAC40**. La taxe frappe
  **l'augmentation nette de position en fin de journée** : un **intraday pur, remis à plat
  avant la clôture, en est exonéré**, tandis qu'une position **overnight coûte 0,3 % à
  l'achat** — rédhibitoire à fort turnover. *(À confirmer : le régime dépend aussi du
  statut de l'intermédiaire.)*
- **Fuseau horaire** : le marché US (15h30–22h00 heure de Paris) se trade **après les
  heures de bureau** ; le CAC40 (9h00–17h30) tombe en pleine journée de travail.

➡️ **Décision : cœur intraday sur large-caps / ETF US liquides.** Le CAC40 reste
envisageable en intraday-flat (exonéré de TTF) mais en **watchlist secondaire**, pas comme
cœur de stratégie.

---

## 6. Points tranchés issus de la revue adversariale

Ces décisions corrigent la première ébauche de plan (couche « foundation RMATS ») :

- **Source GPR** : ni API JSON, ni série FRED — l'indice est distribué en **Excel**. Le
  parser implique donc une **nouvelle dépendance** (`openpyxl`) ou un snapshot CSV
  statique lu en stdlib. Ne pas revendiquer « zéro dépendance ». Le GPR restant
  basse-fréquence, il n'a sa place **qu'en voie lente**.
- **Normalisation GPR** : percentile calculé sur une **fenêtre de référence fixe**, jamais
  expansive — sinon la sémantique du seuil dérive au fil du backtest.
- **Look-ahead** : filtrage **par date-as-of** (comme `dataflows/market_data_validator.py`),
  et non par un lag fixe de 30 jours.
- **Emplacement du blocker** : `tradingagents/execution/`, hors de `agents/risk_mgmt/`.
- **Surcharges d'environnement** : la machinerie `_coerce` de `default_config.py` ne gère
  que des scalaires — listes (univers) et dicts (seuils imbriqués) **ne sont pas**
  surchargeables via `TRADINGAGENTS_*`. Limiter les entrées `_ENV_OVERRIDES` aux scalaires.
- **Schémas** : `regime` en `IntEnum` cohérent partout, `Field(default_factory=...)`,
  **validateur de somme des poids ≈ 1**, unités documentées (valeur brute vs percentile).
- **Un seul drapeau de mode** (`mode`), pas le doublon `mode` + `rmats_enabled`.

---

## 7. Conventions de documentation applicables

Le dépôt n'a **ni `ARCHITECTURE.md` ni `CONTRIBUTING.md`** : la documentation vit dans les
**docstrings**, le **CHANGELOG** et la section **News** du README. Ce fichier inaugure le
répertoire `docs/`, justifié par le caractère multi-phase du chantier.

- **Docstrings** : style `dataflows/fred.py` / `agents/schemas.py` — première ligne « quoi »,
  puis un paragraphe « pourquoi / qui consomme », constantes justifiées en commentaire.
- **CHANGELOG** : Keep a Changelog + SemVer, titre `## [X.Y.Z] — YYYY-MM-DD` (tiret
  cadratin), bloc `### Added`, phrase-titre en gras suivie du *pourquoi*, attribution
  `(#PR, @auteur)`.
- **README** : une puce en tête de `## News`.
- **Dépendances** : tout SDK broker ou flux temps réel va en **extra optionnel**
  (`[project.optional-dependencies]`), jamais dans les dépendances cœur — le repo pose
  explicitement la règle « core install stays lean ». `numpy` (aujourd'hui transitif via
  pandas) est à déclarer explicitement s'il est importé directement.
- **Tests** : `@pytest.mark.unit`, classes `<Sujet>Tests`, fixtures autouse
  `_isolate_config` / `_dummy_api_keys`, réseau mocké via **un point d'entrée privé unique**
  et `mock.patch.dict` du routeur. L'adaptateur broker doit exposer **un seul choke-point**
  mockable ; **aucun ordre réel ne doit pouvoir partir depuis un test**.

---

## 8. Plan d'action phasé

| Phase | Contenu | Sortie / critère |
|---|---|---|
| **P0** | Ce document — direction figée : two-speed, agressif borné | ✅ livré |
| **P1** | Spec exécutable : interface du blocker (règles, seuils, patch d'état), sizer vol-target / Kelly fractionnaire, contrat de données temps réel, extra de dépendances IBKR | Spec validée |
| **P2** | **Backtester intraday coûts-inclus** (spread, slippage, frais, latence), walk-forward | **BLOQUANT** — pas d'edge net > coûts ⇒ arrêt du chantier |
| **P3** | Blocker + adaptateur IBKR en **paper-trading**, déterministe seul, sans LLM | Halts et flatten déclenchés sur scénarios injectés ; reconciliation OK |
| **P4** | Voie lente LLM (Manager récursif → budget de risque / watchlist), branchée en amont, gatée par `mode` | Suite de tests existante 100 % verte ; `mode` défaut `classic` inchangé |
| **P5** | Boucle complète en paper-trading prolongé + documentation (CHANGELOG, README, `docs/`) | Préalable à toute considération d'argent réel |

### Critères de vérification transverses

- **P2** : rapport de backtest **incluant les coûts** démontrant (ou non) un edge net.
- **P3** : en paper-trading, le gate déclenche flatten/halt sur scénarios injectés
  (perte journalière, gap, spread large, prix périmé) ; **aucun** dépassement
  d'exposition ou de levier ; reconciliation des fills conforme.
- **Compatibilité** : à chaque phase, suite de tests existante verte, `ruff` propre,
  `mode` par défaut `"classic"` au comportement strictement inchangé.

---

## Annexe — Sources

- **RMATS** — *Recursive Multi-Agent Trading System: Iterative Optimized Portfolio Strategy
  Under Geopolitical Uncertainty*, arXiv [2605.25311](https://arxiv.org/abs/2605.25311).
- **RecursiveMAS** (projet **distinct**, non-trading) — arXiv
  [2604.25917](https://arxiv.org/abs/2604.25917) ·
  [github.com/RecursiveMAS/RecursiveMAS](https://github.com/RecursiveMAS/RecursiveMAS).
- **Indice GPR** — Caldara & Iacoviello, *Measuring Geopolitical Risk*
  ([matteoiacoviello.com/gpr.htm](https://www.matteoiacoviello.com/gpr.htm)).
- **Conventions internes** — `tradingagents/dataflows/fred.py`,
  `tradingagents/dataflows/errors.py`, `tradingagents/dataflows/config.py`,
  `tradingagents/dataflows/market_data_validator.py`, `tradingagents/agents/schemas.py`,
  `tradingagents/agents/risk_mgmt/`, `tradingagents/default_config.py`, `pyproject.toml`,
  `CHANGELOG.md`, `README.md`.
