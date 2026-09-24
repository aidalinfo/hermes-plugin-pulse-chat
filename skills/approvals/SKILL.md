---
name: approvals
description: Faire valider un livrable ou un plan de travail par les approbateurs Pulse Chat de l'agent — quand appeler pulse_request_approval, comment remplir le titre, les rubriques (pourquoi, étapes, périmètre, impact, risque, réversibilité) et les pièces jointes par référence pour qu'un humain tranche en dix secondes, quoi faire de chaque issue (approved, changes_requested, denied, pending, refused), et ce que cet outil N'EST PAS (l'accord d'un envoi extérieur par connecteur, le garde-fou des commandes d'Hermes).
---

# Faire valider ton travail — `pulse_request_approval`

Dans Pulse Chat, chaque agent a des **approbateurs** désignés par son
organisation. Quand tu appelles `pulse_request_approval`, ta demande part à eux
tous en même temps (carte dans la conversation, file « Approbations »,
notification) ; le premier qui tranche l'emporte.

Tu ne passes **pas** le canal : c'est celui de la conversation en cours.

C'est une **validation de ton travail par ton organisation** — « est-ce que ce
que j'ai produit, ou ce que je m'apprête à faire, est bon ? ». Ce n'est ni une
permission d'envoyer quelque chose à l'extérieur, ni l'accord d'Hermes sur une
commande (voir « Trois accords qui ne se remplacent pas », plus bas).

## Quand l'appeler

1. **À la remise d'un livrable** — quand on t'a demandé un travail à faire
   valider, ou qu'il engage l'organisation : compte rendu, document,
   proposition chiffrée, analyse, rapport. Soumets le **livrable** : son
   contenu, ou son résumé et l'endroit où il se trouve (chemin du coffre,
   artifact publié).
2. **Avant d'exécuter un plan à risque** — dès que la suite est coûteuse,
   difficile à défaire ou touche des données réelles : écraser ou réorganiser
   des documents du coffre, retraiter un jeu de données, lancer un traitement
   long, engager une série d'actions au nom de l'équipe. Soumets ton **plan**.

Ne l'appelle **pas** pour :

- une lecture, une recherche, un brouillon de travail intermédiaire ;
- une question de clarification — c'est `clarify` ;
- chaque petite étape d'un plan déjà approuvé : une approbation couvre le plan
  que tu as soumis, tel que soumis ;
- obtenir le droit d'envoyer un courriel ou de poster chez un tiers — ce n'est
  pas lui qui le donne (section suivante).

## Trois accords qui ne se remplacent pas

| Ce qui est demandé | Qui décide | Par quoi |
|---|---|---|
| Valider un livrable ou un plan | les **approbateurs** de l'agent, désignés par l'organisation | `pulse_request_approval` (cet outil) |
| Envoyer à l'extérieur par un connecteur (courriel, commentaire GitHub, événement d'agenda…) | la **personne qui a prêté son compte** (le délégant), selon sa case « me demander mon accord » | la délégation du connecteur — refus `connector_approval_required` |
| Exécuter une commande dangereuse sur ta machine | un humain, par le **garde-fou d'Hermes** | la carte d'approbation de commande, à part |

Conséquences :

- Un `approved` ici **n'ouvre aucun connecteur**. Si un connecteur te répond
  `connector_approval_required`, ce refus reste valable après n'importe quelle
  approbation : dis à l'humain que l'envoi attend l'accord du propriétaire du
  compte. Le plus sûr pour un envoi reste le **brouillon** (`mail.draft`), que
  la personne enverra elle-même.
- Tu peux avoir besoin des **deux** : faire valider le contenu d'un courriel
  important par tes approbateurs (le livrable), puis le préparer en brouillon
  ou l'envoyer par le connecteur, qui appliquera sa propre règle.
- Le garde-fou d'Hermes continue de demander son accord sur les commandes,
  même pendant l'exécution d'un plan approuvé.

## Écrire une demande qui se tranche vite

L'approbateur n'a peut-être **pas** accès à la conversation : ce que tu
soumets doit se suffire à lui-même. Préfère les **rubriques** à un long
`body` — l'écran d'approbation les montre en premier, dans cet ordre.

| Paramètre | Ce que tu y mets | Exemple |
|---|---|---|
| `title` (requis) | Une ligne : ce qu'on valide. C'est tout ce que dit la notification. | « Redémarrer le builder multi-arch » |
| `reason` | **Pourquoi**, en un court paragraphe : le problème constaté ou la demande de l'humain. | « Le builder buildx est bloqué depuis 9 h : trois pipelines échouent. » |
| `steps` | **Ce que tu vas faire**, une entrée par action, dans l'ordre. La commande **exacte** dans `command` quand il y en a une — c'est elle qu'on valide. | `[{"label": "Redémarrer le builder", "command": "docker restart buildx_buildkit_multiarch0"}, {"label": "Relancer la CI"}]` |
| `scope` | **Périmètre touché** : deux ou trois mots par étiquette. | `["CI", "Builder multi-arch"]` |
| `impact` | Qui ou quoi est affecté, et combien de temps. | « Les builds en cours échouent une fois ; 30 s d'indisponibilité. » |
| `risk` | Ton estimation **honnête** : `low`, `medium`, `high`. | `medium` |
| `reversible` | `true` si ça se défait simplement, `false` sinon (suppression, envoi, paiement). | `false` |
| `attachments` | Les documents à **ouvrir** pour trancher, en **références** : `vault:<chemin>` (coffre de la conversation) ou `message:<id>` (pièce déjà envoyée). Jamais le contenu. | `["vault:rapports/incident-0923.pdf"]` |
| `body` | Facultatif dès qu'une rubrique est remplie. Le contenu du livrable, ou le détail qui ne rentre nulle part ailleurs, en Markdown. Ne recopie pas `reason` ni `steps`. | |

Règles qui évitent un refus ou une mauvaise décision :

- **Pour un livrable**, le livrable lui-même va dans `attachments` (écris-le
  d'abord dans le coffre avec pulse_vault_write) ou dans `body` s'il est
  court ; `steps` décrit alors ce que tu feras une fois approuvé (« envoyer à la
  compta »), ou s'omet.
- **Pour un plan**, `steps` est la rubrique qui compte : une étape = une action
  que l'approbateur peut juger. Une liste de trois étapes se lit ; un script de
  trente lignes, non (12 étapes au plus).
- **Ne devine pas `risk` ni `reversible`.** Omis, l'écran n'affiche rien — ce
  qui vaut mieux qu'une promesse fausse. Et ne sous-estime pas un risque pour
  obtenir un « oui » plus vite.
- **Le préfixe d'une pièce jointe est obligatoire.** Un chemin nu est refusé
  (`gate_attachment_invalid_ref`) ; une référence qui n'existe pas fait refuser
  **toute** la demande (`gate_attachment_not_found`) — rien n'est ouvert à
  moitié. Corrige la référence et rappelle l'outil.
- **Un fichier du coffre est lu vivant** : si tu le réécris après avoir demandé,
  l'approbateur verra la nouvelle version. Ne touche pas à ce que tu as soumis
  tant que ce n'est pas tranché.
- Face à une version de Pulse Chat **antérieure** aux rubriques, le plugin les
  replie de lui-même dans le corps et les pièces jointes n'y sont que nommées :
  la demande part quand même, tu n'as rien à faire de différent.

## Chaque issue

| Issue | Ce que tu fais |
|---|---|
| `approved` | Tu livres ou tu exécutes **exactement** ce que tu as soumis. Rien de plus. |
| `changes_requested` | Tu lis `comment`, tu corriges, et tu **resoumets** avant de livrer ou d'agir. |
| `denied` | Tu ne livres pas et n'exécutes pas ce que tu avais soumis. Tu le dis, et tu demandes la suite. |
| `pending` | Personne n'a encore tranché. Tu **t'arrêtes**, tu dis à l'humain que tu attends. La décision t'arrivera plus tard comme un message commençant par `[Approbation]` : reprends à ce moment-là. **Ne resoumets pas.** |
| `refused` | La demande n'a pas pu être ouverte (`code` dit pourquoi). `no_approver_configured` : tu répètes la raison à l'humain. `invalid_request`, `gate_attachment_*`, `gate_body_required` : un de TES paramètres est à corriger (`message` dit lequel) — corrige et rappelle l'outil. Dans tous les cas, tu n'exécutes pas. |

## Ce qui n'est jamais vrai

- « Pas de réponse » ne vaut **jamais** accord. Seul `approved` t'autorise.
- Une demande approuvée d'office (un approbateur à auto-validation t'avait lui-même
  demandé ce travail) reste une approbation de **ce que tu as soumis** — pas
  d'autre chose.
- Une approbation ici n'est jamais l'accord du propriétaire d'un compte tiers,
  ni celui du garde-fou d'Hermes sur une commande.
