---
name: interface
description: Cartes interactives dans Pulse Chat (outil pulse_ui) — quand une carte sert mieux que du texte (comparer des options, montrer des chiffres qui évoluent, simuler un calcul, recueillir des informations), le catalogue fermé des composants (tableau, chiffres clés, graphique, comparatif, frise, formulaire, boutons), les liaisons et formules, ce que la personne te renvoie, et huit exemples complets. À charger AVANT ta première carte.
---

> Enregistré par le plugin sous le nom `pulse-chat:interface` (`register_skill`),
> nommé dans le `platform_hint`. Le catalogue fait foi côté app
> (`shared/genui/schema.ts`, docs/66) : un refus te dit exactement quoi corriger.

# Cartes interactives

`pulse_ui` affiche dans la conversation une **carte native** que tu composes :
la personne la lit, la manipule (trie, filtre, bouge un curseur, coche), et ce
qu'elle **soumet** te revient comme un message.

## 1. Quand faire une carte — et surtout quand NE PAS en faire

Le texte reste le **cas par défaut**. Une carte n'est justifiée que si elle sert
mieux qu'une phrase.

| Le message demande… | Forme |
|---|---|
| une explication, un avis, une réponse courte | **texte** |
| comparer 2+ options sur plusieurs critères | `comparison` ou `table` |
| des chiffres qui se suivent dans le temps | `chart` + `stat` |
| « combien si… », un calcul avec paramètres | simulateur : `slider`/`input` + `computed` |
| recueillir 3 informations ou plus | formulaire + `button` `intent: reply` |
| un choix unique parmi 2–5 propositions | **texte** + question Hermes (`clarify`), pas une carte |
| une validation avant d'agir | **`pulse_request_approval`**, jamais un bouton |
| un document à garder, imprimer, versionner | **`pulse_publish_artifact`** |

## 2. Règles d'écriture

- **Une carte = un sujet.** Le titre dit la conclusion (« Pro est le meilleur
  rapport qualité-prix »), pas le thème (« Comparatif »).
- **3 `stat` au plus** en tête ; le détail va dans un tableau.
- **Jamais de donnée inventée.** Dis la source et la date dans `description`.
  Un chiffre provisoire se dit (`hint: "estimation"`).
- **Boutons : 3 au plus, un seul `primary`** par rangée `actions`, et un
  `intent` honnête :
  - `reply` — la personne te répond (choix, formulaire) ;
  - `refine` — elle te demande de recalculer ou d'affiner la carte ;
  - `request_action` — elle te demande d'AGIR (envoyer, créer, réserver). Tu
    restes soumis à tes gardes : approbations, connecteurs. Le clic ne fait rien
    d'autre que t'écrire.
- **Valeurs BRUTES, format à part** : `4800` + `format: "currency"`, jamais
  `"4 800 €"`. `percent` attend une FRACTION (`0.125` ⇒ 12,5 %). L'app met en
  forme dans la langue du lecteur.
- **Bulle courte** : `text` = 1–2 phrases d'accompagnement, sans paraphraser la
  carte. Sans `text`, la bulle porte seulement le titre.
- **Mettre à jour plutôt que reposter** : même `surface_id` + `op: "patch"`
  quand la personne affine ; `op: "close"` quand c'est tranché.

## 3. Le document

```json
{
  "title": "…",
  "description": "…",
  "layout": "inline",
  "root": "root",
  "components": [ { "id": "root", "type": "stack", "children": ["a", "b"] }, … ],
  "data": { … },
  "computed": { "/total": "prix * quantite" },
  "submit": "once"
}
```

- **Liste PLATE** : chaque composant a un `id` unique ; un parent cite ses
  enfants par id (`children`, `footer`, `items[].children`). Un composant
  n'a qu'un parent ; aucun orphelin ; profondeur ≤ 8 ; 200 composants au plus.
- **Liaisons** : toute valeur peut être littérale, **liée** à `data`
  (`{ "$": "/offres/0/prix" }`, pointeur JSON) ou **calculée**
  (`{ "=": "prix * (1 - remise / 100)" }`).
- **`computed`** : valeurs dérivées (`"/chemin": "formule"`), recalculées à
  chaque saisie dans le navigateur ET par le serveur à la soumission.
- **Formules** : `+ - * / % ^`, `== != < <= > >=`, `&& || !`, `? :`,
  fonctions `min max abs round floor ceil sqrt pow sum avg count if clamp pmt`.
  Les noms sont des clés de `data` (`offres[0].prix`). `pmt(taux, n, capital)` =
  mensualité d'un emprunt. `sum(liste, "clé")` additionne une colonne.
- **`visible`** sur tout composant : `{ "=": "retard > 0" }`.
- **`submit`** : `once` (défaut — la première réponse ferme la carte) ou
  `each` (sondage : chacun répond).
- **`layout: "wide"`** pour un tableau large (ignoré au téléphone).
- **Couleurs** par nom seulement : `primary secondary success info warning
  error neutral`. **Icônes** Lucide de la liste fermée (`euro`, `calendar`,
  `users`, `chart-line`, `trending-up`, `check`, `clock`, `rocket`…) ; en doute,
  omets `icon`.

## 4. Catalogue

**Mise en page** — `stack` (`children`, `direction` vertical|horizontal, `gap`
sm|md|lg, `align`), `grid` (`columns` 1–4, une colonne au téléphone), `card`
(`title`, `description`, `icon`, `children`, `footer`), `tabs` (`items[{label,
icon, children}]`), `accordion` (`items`, `multiple`), `separator` (`label`).

**Texte** — `heading` (`text`, `level` 2–4), `text` (`markdown`, `tone`
default|muted, `size`), `badge` (`label`, `color`, `variant` soft|outline|solid),
`alert` (`title`, `description`, `color`), `code` (`code`, `language`), `kv`
(`items[{label, value, format, currency, hint}]`, `columns` 1|2).

**Données** —
- `stat` : `label`, `value`, `format`, `currency`, `delta` (nombre), `deltaFormat`,
  `goodWhen` up|down, `hint`, `icon`, `sparkline` (liste de nombres) ;
- `table` : `rows` (liste d'objets), `columns[{key, label, format, currency,
  align, colors}]` (`format: "badge"` + `colors: {"En retard": "error"}`),
  `sortable`, `filterable`, `pageSize`, `emptyText`, `select: {bind, valueKey}`
  (ligne choisie) — 500 lignes au plus ;
- `list` : `items` (objets `{title, description, icon, badge, badgeColor, trailing}`) ;
- `timeline` : `items` (objets `{date, title, description, status: done|current|upcoming, icon}`) ;
- `progress` : `value`, `max`, `label`, `color` ;
- `comparison` : `options` (objets `{value, name, price, period, description,
  highlights[], badge, recommended}`), `features[{label, values[]}]` (une valeur
  par option, `true`/`false` ⇒ ✓/—), `format`, `currency`, `select: {bind}` ;
- `chart` : `kind` line|area|bar|stacked-bar|donut, `data` (liste d'objets),
  `x` (clé de l'abscisse ou de la catégorie), `series[{key, label, color}]`
  (1 à 6 ; un donut en a exactement une), `format`, `currency`, `xFormat`,
  `height` sm|md|lg, `legend`, `title`, `description` — 2 000 points au plus.

**Formats** : `text number integer decimal percent currency date datetime
relative duration bytes boolean badge`.

**Champs** (tous : `bind` = chemin de `data` qu'ils écrivent, `label`
obligatoire, `hint`, `required`, `disabled`) — `input` (`inputType`
text|email|number|tel|url, `min`, `max`, `step`, `maxLength`, `prefix`,
`suffix`), `textarea` (`rows`, `maxLength`), `select` (`options[{value, label}]`,
`multiple`), `radio` (`options`, `orientation`), `segmented` (2–6 `options`),
`checkbox`, `switch` (`description`), `slider` (`min`, `max`, `step`, `format`,
`currency`), `date` (`min`, `max` en AAAA-MM-JJ).

**Actions** — `button` (`label`, `icon`, `variant`
primary|secondary|ghost|destructive, `block`, `action: {name, intent, send[],
confirm}`), `actions` (rangée de boutons/liens/copies, `align`), `link` (`href`
https:// ou chemin interne, `label`), `copy` (`label`, `value`).

- `action.send` liste les chemins envoyés (`bind` d'un champ, clé de
  `computed`, ou chemin de `data`). Le serveur juge chaque valeur ; une option
  se renvoie par sa `value`, tu reçois aussi son libellé.
- `variant: "destructive"` exige `confirm` (la question posée avant l'envoi).

## 5. Ce que tu reçois

Une soumission arrive comme un message de la personne, qui CITE ta carte :

```
Marie Dupont: Retenir cette offre — choix : Pro
[Interface : Trois offres de maintenance → choisir_offre] (surface offres-maintenance, v1, bouton « Retenir cette offre », reply)
- /choix (choix) : "pro"
```

Réponds normalement. Si la personne affine (`refine`), mets la carte à jour
(`op: "patch"` sur `/data/…`) puis dis-le en une phrase.

## 6. Lire un refus

```json
{"status": "refused", "code": "ui_invalid",
 "issues": [{"path": "components[4].columns[1].format", "message": "…", "hint": "valeurs permises : …"}]}
```

Corrige CHAQUE `path` cité et renvoie la carte entière (`op: "render"`). Ne
demande pas à l'humain : c'est ton document.

## 7. Exemples complets

Chaque exemple est le `document` d'un appel `pulse_ui` avec `op: "render"`.

### Exemple 1 — Comparatif d'offres (choix + réponse)

```json
{
  "title": "Pro offre le meilleur rapport qualité-prix",
  "description": "Prix annuels HT, devis reçus le 22/10",
  "root": "root",
  "components": [
    { "id": "root", "type": "stack", "children": ["offres", "actions"] },
    {
      "id": "offres",
      "type": "comparison",
      "options": { "$": "/offres" },
      "select": { "bind": "/choix" },
      "features": [
        { "label": "Intervention sur site", "values": [false, true, true] },
        { "label": "Délai d'intervention", "values": ["J+1", "8 h", "4 h"] }
      ]
    },
    { "id": "actions", "type": "actions", "children": ["retenir"] },
    {
      "id": "retenir",
      "type": "button",
      "label": "Retenir cette offre",
      "action": { "name": "choisir_offre", "intent": "reply", "send": ["/choix"] }
    }
  ],
  "data": {
    "offres": [
      { "value": "essentiel", "name": "Essentiel", "price": 4800, "period": "an", "highlights": ["Support à distance", "Heures ouvrées"] },
      { "value": "pro", "name": "Pro", "price": 6600, "period": "an", "recommended": true, "highlights": ["Intervention sur site", "Pièces incluses"] },
      { "value": "premium", "name": "Premium", "price": 9200, "period": "an", "highlights": ["24 h/24", "Technicien dédié"] }
    ],
    "choix": null
  }
}
```

### Exemple 2 — Tableau de bord (chiffres clés, courbe, détail)

```json
{
  "title": "Le chiffre d'affaires progresse de 12 % sur le trimestre",
  "description": "Source : export comptable du 21/10",
  "layout": "wide",
  "root": "root",
  "components": [
    { "id": "root", "type": "stack", "children": ["kpis", "courbe", "detail"] },
    { "id": "kpis", "type": "grid", "columns": 3, "children": ["ca", "marge", "clients"] },
    { "id": "ca", "type": "stat", "label": "CA du trimestre", "value": { "=": "sum(mois, \"ca\")" }, "format": "currency", "delta": 0.12, "goodWhen": "up", "icon": "euro" },
    { "id": "marge", "type": "stat", "label": "Marge moyenne", "value": { "=": "avg(mois, \"marge\")" }, "format": "percent", "delta": -0.02, "deltaFormat": "percent", "goodWhen": "up" },
    { "id": "clients", "type": "stat", "label": "Nouveaux clients", "value": 14, "format": "integer", "sparkline": [3, 4, 2, 5] },
    {
      "id": "courbe",
      "type": "chart",
      "kind": "area",
      "data": { "$": "/mois" },
      "x": "libelle",
      "series": [{ "key": "ca", "label": "Chiffre d'affaires" }],
      "format": "currency",
      "title": "Chiffre d'affaires mensuel"
    },
    {
      "id": "detail",
      "type": "table",
      "rows": { "$": "/mois" },
      "columns": [
        { "key": "libelle", "label": "Mois" },
        { "key": "ca", "label": "CA", "format": "currency" },
        { "key": "marge", "label": "Marge", "format": "percent" }
      ],
      "sortable": true
    }
  ],
  "data": {
    "mois": [
      { "libelle": "Juillet", "ca": 41200, "marge": 0.31 },
      { "libelle": "Août", "ca": 38900, "marge": 0.29 },
      { "libelle": "Septembre", "ca": 46100, "marge": 0.33 }
    ]
  }
}
```

### Exemple 3 — Simulateur de prêt (curseurs + formules)

```json
{
  "title": "Simulez votre emprunt",
  "description": "Taux fixe, assurance non comprise",
  "root": "root",
  "components": [
    { "id": "root", "type": "stack", "children": ["capital", "taux", "duree", "resultats", "actions"] },
    { "id": "capital", "type": "slider", "bind": "/capital", "label": "Montant emprunté", "min": 10000, "max": 800000, "step": 5000, "format": "currency" },
    { "id": "taux", "type": "slider", "bind": "/taux", "label": "Taux annuel (%)", "min": 0.5, "max": 8, "step": 0.05, "format": "decimal" },
    { "id": "duree", "type": "segmented", "bind": "/duree", "label": "Durée", "options": [
      { "value": 15, "label": "15 ans" }, { "value": 20, "label": "20 ans" }, { "value": 25, "label": "25 ans" }
    ] },
    { "id": "resultats", "type": "grid", "columns": 2, "children": ["mensualite", "cout"] },
    { "id": "mensualite", "type": "stat", "label": "Mensualité", "value": { "$": "/mensualite" }, "format": "currency" },
    { "id": "cout", "type": "stat", "label": "Coût total du crédit", "value": { "$": "/cout" }, "format": "currency" },
    { "id": "actions", "type": "actions", "children": ["garder"] },
    { "id": "garder", "type": "button", "label": "Garder ce scénario", "action": { "name": "garder_scenario", "intent": "reply", "send": ["/capital", "/taux", "/duree", "/mensualite"] } }
  ],
  "data": { "capital": 200000, "taux": 3.5, "duree": 20 },
  "computed": {
    "/mensualite": "pmt(taux / 100 / 12, duree * 12, capital)",
    "/cout": "mensualite * duree * 12 - capital"
  }
}
```

### Exemple 4 — Formulaire de demande

```json
{
  "title": "Demande d'intervention",
  "description": "Je crée le ticket dès réception",
  "root": "root",
  "components": [
    { "id": "root", "type": "stack", "children": ["site", "urgence", "description", "contact", "actions"] },
    { "id": "site", "type": "select", "bind": "/site", "label": "Site concerné", "required": true, "options": [
      { "value": "lyon", "label": "Lyon" }, { "value": "paris", "label": "Paris" }, { "value": "nantes", "label": "Nantes" }
    ] },
    { "id": "urgence", "type": "radio", "bind": "/urgence", "label": "Urgence", "orientation": "horizontal", "options": [
      { "value": "basse", "label": "Basse" }, { "value": "normale", "label": "Normale" }, { "value": "haute", "label": "Haute" }
    ] },
    { "id": "description", "type": "textarea", "bind": "/description", "label": "Que se passe-t-il ?", "required": true, "rows": 4 },
    { "id": "contact", "type": "input", "bind": "/contact", "label": "Téléphone de rappel", "inputType": "tel" },
    { "id": "actions", "type": "actions", "children": ["envoyer"] },
    { "id": "envoyer", "type": "button", "label": "Créer le ticket", "icon": "send", "action": { "name": "creer_ticket", "intent": "request_action", "send": ["/site", "/urgence", "/description", "/contact"] } }
  ],
  "data": { "site": null, "urgence": "normale", "description": "", "contact": "" }
}
```

### Exemple 5 — Frise de projet

```json
{
  "title": "Migration de la messagerie : étape 3 sur 5",
  "root": "root",
  "components": [
    { "id": "root", "type": "stack", "children": ["avancement", "frise"] },
    { "id": "avancement", "type": "progress", "value": 3, "max": 5, "label": "Étapes terminées" },
    { "id": "frise", "type": "timeline", "items": { "$": "/etapes" } }
  ],
  "data": {
    "etapes": [
      { "date": "2026-10-01", "title": "Audit des boîtes", "status": "done" },
      { "date": "2026-10-08", "title": "Création des comptes", "status": "done" },
      { "date": "2026-10-15", "title": "Copie des archives", "status": "done" },
      { "date": "2026-10-24", "title": "Bascule du domaine", "status": "current", "description": "Coupure prévue de 20 h à 22 h" },
      { "date": "2026-10-31", "title": "Retrait de l'ancien serveur", "status": "upcoming" }
    ]
  }
}
```

### Exemple 6 — Liste de tâches à arbitrer

```json
{
  "title": "4 tickets attendent un arbitrage",
  "root": "root",
  "components": [
    { "id": "root", "type": "stack", "children": ["liste", "actions"] },
    { "id": "liste", "type": "table", "rows": { "$": "/tickets" }, "select": { "bind": "/ticket", "valueKey": "id" }, "columns": [
      { "key": "titre", "label": "Ticket" },
      { "key": "client", "label": "Client" },
      { "key": "etat", "label": "État", "format": "badge", "colors": { "En retard": "error", "À planifier": "warning" } },
      { "key": "ouvert", "label": "Ouvert", "format": "date" }
    ], "sortable": true },
    { "id": "actions", "type": "actions", "children": ["prendre", "plus"] },
    { "id": "prendre", "type": "button", "label": "Je prends ce ticket", "action": { "name": "prendre_ticket", "intent": "reply", "send": ["/ticket"] } },
    { "id": "plus", "type": "button", "variant": "ghost", "label": "Voir les tickets clos", "action": { "name": "voir_clos", "intent": "refine" } }
  ],
  "data": {
    "ticket": null,
    "tickets": [
      { "id": "T-112", "titre": "Imprimante HS", "client": "Garage Martin", "etat": "En retard", "ouvert": "2026-10-14" },
      { "id": "T-118", "titre": "VPN instable", "client": "Cabinet Roux", "etat": "À planifier", "ouvert": "2026-10-19" },
      { "id": "T-121", "titre": "Nouveau poste", "client": "Boulangerie Lenoir", "etat": "À planifier", "ouvert": "2026-10-20" },
      { "id": "T-124", "titre": "Licence Office", "client": "Garage Martin", "etat": "Nouveau", "ouvert": "2026-10-21" }
    ]
  }
}
```

### Exemple 7 — Sondage d'équipe (chacun répond)

```json
{
  "title": "Quel créneau pour la réunion trimestrielle ?",
  "submit": "each",
  "root": "root",
  "components": [
    { "id": "root", "type": "stack", "children": ["creneau", "remarque", "actions"] },
    { "id": "creneau", "type": "radio", "bind": "/creneau", "label": "Créneau", "required": true, "options": [
      { "value": "mar-10", "label": "Mardi 4 novembre, 10 h" },
      { "value": "jeu-14", "label": "Jeudi 6 novembre, 14 h" },
      { "value": "ven-9", "label": "Vendredi 7 novembre, 9 h" }
    ] },
    { "id": "remarque", "type": "input", "bind": "/remarque", "label": "Une contrainte ?", "maxLength": 200 },
    { "id": "actions", "type": "actions", "children": ["voter"] },
    { "id": "voter", "type": "button", "label": "Envoyer ma réponse", "action": { "name": "voter", "intent": "reply", "send": ["/creneau", "/remarque"] } }
  ],
  "data": { "creneau": null, "remarque": "" }
}
```

### Exemple 8 — Suivi avec alerte conditionnelle

```json
{
  "title": "Sauvegardes de la semaine",
  "root": "root",
  "components": [
    { "id": "root", "type": "stack", "children": ["alerte", "etat", "details"] },
    { "id": "alerte", "type": "alert", "color": "warning", "title": { "=": "echecs + \" sauvegarde(s) en échec\"" }, "visible": { "=": "echecs > 0" } },
    { "id": "etat", "type": "progress", "value": { "=": "reussies / total * 100" }, "label": "Sauvegardes réussies", "color": "success" },
    { "id": "details", "type": "kv", "columns": 2, "items": [
      { "label": "Réussies", "value": { "$": "/reussies" }, "format": "integer" },
      { "label": "En échec", "value": { "$": "/echecs" }, "format": "integer" },
      { "label": "Volume", "value": { "$": "/volume" }, "format": "bytes" },
      { "label": "Dernière", "value": { "$": "/derniere" }, "format": "datetime" }
    ] }
  ],
  "data": { "reussies": 26, "echecs": 2, "total": 28, "volume": 412000000000, "derniere": "2026-10-22T02:14:00Z" }
}
```

### Mettre une carte à jour

```json
{ "op": "patch", "surface_id": "offres-maintenance", "patch": [
  { "op": "replace", "path": "/data/offres/1/price", "value": 6200 },
  { "op": "replace", "path": "/title", "value": "Pro passe à 6 200 € après négociation" }
] }
```
