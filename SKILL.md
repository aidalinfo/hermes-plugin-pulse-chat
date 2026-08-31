---
name: pulse-chat
description: Utiliser Pulse Chat depuis un agent Hermes — appeler les outils MCP exposés par /mcp-hermes (plan de travail, coffre-fort, connecteurs), ne pas confondre plan_* (ton propre plan) avec tasks_* (la liste To Do d'un humain délégant), et livrer une réponse de routine avec routine_deliver (aucun repli : sans cet appel, personne ne reçoit rien).
---

> ⚠️ **Format non vérifié.** Cette skill a été écrite sans pouvoir inspecter
> `tools.skills_tool._find_all_skills` ni `hermes_cli.skills_config` dans une
> image Hermes réelle (accès à une machine de bot indisponible au moment de la
> rédaction). Le frontmatter ci-dessus (`name` + `description`) reprend le
> format des skills Claude, choisi par HYPOTHÈSE, parce que
> `capabilities.py` du plugin lit ce que rend `_find_all_skills()` sous la
> forme de dictionnaires `{"name": ..., "description": ...}` (voir
> `capabilities.py` lignes ~175-188 et `tests/test_capabilities.py`) — c'est
> une correspondance partielle, pas une confirmation de l'emplacement du
> fichier ni du reste du frontmatter. **Avant de déployer cette skill sur un
> bot, vérifie sur l'image réelle qu'elle se charge** (une skill mal formée
> ne lève aucune erreur — elle disparaît en silence, exactement le mode de
> panne que ce chantier existe pour éliminer) et corrige ce fichier au besoin.

# Pulse Chat

Tu es branché sur **Pulse Chat**, un front de conversation où des humains te
parlent, te partagent des documents et te délèguent des tâches. Tout ce que
tu peux y faire au-delà de répondre en texte — lire ton plan de travail,
poser un fichier dans le coffre-fort d'un canal, envoyer un courriel au nom
de quelqu'un qui te l'a délégué, refermer une tâche — passe par des **outils
MCP**, pas par le plugin de plateforme. Le plugin (celui qui t'a fait
recevoir ce message) ne fait que transporter le texte ; c'est un second
canal, `POST /mcp-hermes`, qui te donne ces outils.

## Avant d'appeler quoi que ce soit

Ces outils n'existent que si :

- ton bot a une entrée MCP pointant sur `/mcp-hermes` dans sa configuration
  Hermes (`~/.hermes/config.yaml` ou équivalent) ;
- tu portes un `AgentCredential` valide (identité vérifiée) — sans lui,
  certains outils te refusent l'accès ou s'exécutent sous une identité qui
  n'est pas la tienne.

Si un outil `*_*` que cette skill mentionne n'apparaît pas dans ta liste
d'outils, ne l'invente pas et ne le simule pas en texte : dis à l'humain que
la fonctionnalité n'est pas branchée pour toi plutôt que de prétendre l'avoir
utilisée. Détails de branchement complets : `docs/12-mcp-connecteurs.md` du
dépôt `PROJET-pulse-chat` (côté app, pas ce dépôt).

## Le piège : `plan_*` n'est pas `tasks_*`

Deux familles d'outils se ressemblent et ne servent PAS la même chose. Elles
existent sur le même endpoint MCP, et rien ne t'avertit si tu appelles la
mauvaise — le refus, quand il y en a un, ne dit jamais « tu t'es trompé de
famille ».

- **`plan_*`** (`plan_take`, `plan_update`, `plan_create`, …) — c'est **TON**
  plan de travail, la file de tâches que Pulse Chat te propose et que tu
  fais avancer toi-même. Utilise `plan_take` pour prendre une tâche prête,
  `plan_update` pour la faire progresser ou la refermer.
- **`tasks_*`** — c'est la liste **To Do Microsoft d'un HUMAIN** qui t'a
  délégué l'accès à sa boîte. Ce n'est PAS ta liste de travail. Utilise-la
  uniquement quand on te demande explicitement de créer ou cocher une tâche
  **dans l'agenda de quelqu'un d'autre**.

Si tu veux refermer une tâche que TU viens de terminer, appelle `plan_update`
— jamais `tasks_*`. Un agent qui se trompe ici se prend soit un refus de
délégation qui ne dit rien de la vraie cause, soit pire : il inscrit une
tâche dans l'Outlook de quelqu'un en croyant simplement se donner du travail.
Avant d'appeler l'un de ces outils, demande-toi : « est-ce MON plan, ou
l'agenda de quelqu'un d'autre ? »

## Le coffre-fort du canal

Chaque canal a un espace de stockage borné (quota de fichiers) où tu peux
déposer un document que tu produis, ou lire ce qu'un humain (ou toi-même) y a
mis. Les outils de coffre te rendent des **références** (`vault:<chemin>`),
jamais des octets bruts à faire circuler ailleurs — passe ces références aux
autres outils qui en attendent (ex. joindre un fichier à un courriel), ne
recopie pas leur contenu dans un paramètre texte.

## Approbations

Une action jugée sensible (agenda de quelqu'un, envoi d'un courriel qui part
réellement, etc.) peut être soumise à l'approbation d'un humain avant de
s'exécuter pour de vrai. Si un appel te répond qu'une approbation est en
attente, ne réessaie pas en boucle : c'est en cours de décision côté humain,
la réponse arrivera par le canal normal.

## `routine_deliver` : livrer une réponse de routine

Une **routine** est un prompt planifié qui te réveille périodiquement sans
qu'un humain t'ait sollicité dans l'instant. Quand une routine te réveille,
tu ne te retrouves PAS dans le canal habituel : tu travailles dans une
**salle privée**, ouverte pour cette seule exécution, que **personne ne
lit**. Tout ce que tu écris dans cette salle — brouillons, réflexions,
appels d'outils intermédiaires — reste invisible de tous.

**La seule façon de faire sortir ta réponse de cette salle est d'appeler
l'outil `routine_deliver`** avec le canal de la salle et le texte de ta
réponse finale. Il n'y a **aucun repli** : si tu ne l'appelles pas, ta
réponse ne va nulle part, et l'exécution est déclarée en échec à l'échéance
— comme si tu n'avais jamais répondu, même si tu as fini le travail.

Points à retenir :

- **Tu ne choisis pas le destinataire.** `routine_deliver` ne prend qu'un
  canal et un texte ; c'est la routine, configurée par un humain, qui a déjà
  fixé où ta réponse doit arriver (un canal, ou une conversation privée avec
  quelqu'un). N'essaie pas de deviner ou de forcer une autre destination.
- **Un seul appel par exécution.** `routine_deliver` clôt la salle de
  travail. Un second appel sur la même exécution est refusé — ce n'est pas
  un moyen d'envoyer plusieurs messages ou de te corriger après coup.
  Prépare ta réponse finale avant d'appeler l'outil.
- **N'appelle jamais l'outil « en cours de route » sur une étape
  intermédiaire.** Ce que tu livres est ta conclusion, pas un état
  d'avancement — la salle n'a pas de spectateur pour lire un « je continue
  à chercher ».
- **N'invente jamais ta propre planification pour répéter cette
  impulsion.** Ne crée pas de tâche récurrente, de rappel, ni de planning
  personnel qui rejouerait ce même travail plus tard : la routine est déjà
  planifiée côté Pulse Chat. La dupliquer la ferait s'exécuter deux fois,
  sans que personne ne l'ait demandé deux fois.

Si tu reçois un préambule en tête de l'impulsion qui répète déjà ces
consignes, ne le traite pas comme un message de l'humain à qui répondre —
c'est un rappel du produit, pas un tour de conversation.
