---
name: approvals
description: Faire valider un plan ou un livrable par les approbateurs Pulse Chat avant d'agir — quand appeler pulse_request_approval, comment écrire un titre et un corps qu'un humain tranche en dix secondes, et quoi faire de chaque issue (approved, changes_requested, denied, pending, refus).
---

# Faire valider avant d'agir — `pulse_request_approval`

Dans Pulse Chat, chaque agent a des **approbateurs** désignés par son
organisation. Quand tu appelles `pulse_request_approval`, ta demande part à eux
tous en même temps (carte dans la conversation, file « Approbations »,
notification) ; le premier qui tranche l'emporte.

Tu ne passes **pas** le canal : c'est celui de la conversation en cours.

## Quand l'appeler

1. **Avant d'agir** — dès que la suite est coûteuse, irréversible ou visible
   par des tiers : envoyer un courriel, publier, supprimer, modifier des
   données réelles, lancer un traitement long. Soumets ton **plan**.
2. **À la remise** — quand on t'a demandé un livrable à faire valider (compte
   rendu, document, proposition chiffrée). Soumets le **livrable**, ou son
   contenu essentiel.

Ne l'appelle **pas** pour une lecture, une recherche, une question de
clarification (utilise `clarify`), ni pour chaque petite étape d'un plan déjà
approuvé : une approbation couvre le plan que tu as soumis, tel que soumis.

## Écrire une demande qui se tranche vite

- **`title`** — une ligne, ce qu'on valide : « Plan d'extraction des factures
  de septembre », « Compte rendu du comité du 20/09 ». C'est tout ce que
  l'approbateur voit dans sa notification.
- **`body`** — en Markdown, **autonome** : l'approbateur n'a peut-être pas
  accès à la conversation. Pour un plan : les étapes numérotées, et pour
  chacune ce qu'elle touche (quels fichiers, quels destinataires, quelles
  données). Pour un livrable : le contenu, ou son résumé et où il se trouve.
  Une liste de trois impacts se lit ; un script de trente lignes, non.

## Chaque issue

| Issue | Ce que tu fais |
|---|---|
| `approved` | Tu exécutes **exactement** ce que tu as soumis. Rien de plus. |
| `changes_requested` | Tu lis `comment`, tu corriges, et tu **resoumets** avant d'agir. |
| `denied` | Tu ne fais pas ce que tu avais soumis. Tu le dis, et tu demandes la suite. |
| `pending` | Personne n'a encore tranché. Tu **t'arrêtes**, tu dis à l'humain que tu attends. La décision t'arrivera plus tard comme un message commençant par `[Approbation]` : reprends à ce moment-là. **Ne resoumets pas.** |
| `refused` | La demande n'a pas pu être ouverte (`code` dit pourquoi — souvent `no_approver_configured`). Tu répètes la raison à l'humain et tu n'exécutes pas. |

## Ce qui n'est jamais vrai

- « Pas de réponse » ne vaut **jamais** accord. Seul `approved` t'autorise.
- Une demande approuvée d'office (l'approbateur principal t'avait lui-même
  demandé ce travail) reste une approbation de **ce que tu as soumis** — pas
  d'autre chose.
- Cet outil ne remplace pas le garde-fou d'Hermes sur les commandes
  dangereuses : celui-là continue de demander son accord à part.
