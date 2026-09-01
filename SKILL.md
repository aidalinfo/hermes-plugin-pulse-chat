---
name: pulse-chat
description: Se servir de Pulse Chat depuis un agent Hermes — les outils MCP de /mcp-hermes (connectors_available et les 20 capacités connector_*, le plan de travail plan_*, routine_deliver), ce qui n'en est PAS (le coffre-fort passe par les routes HTTP du plugin, les approbations par request_approval() côté Hermes, les artifacts par POST /api/agent/messages), le piège plan_* contre tasks_*, brouillon contre envoi réel, aperçu contre corps de courriel, les pièces jointes par référence préfixée bornées à 3 Mo, et quoi faire d'un refus. À charger dès qu'un canal Pulse Chat demande d'agir sur un compte tiers délégué (courriel, agenda, To Do, Teams, GitHub), de tenir un plan de tâches, de déposer un fichier, ou de répondre à une routine.
---

> ⚠️ **Format non vérifié.** Cette skill a été écrite sans pouvoir inspecter
> `tools.skills_tool._find_all_skills` ni `hermes_cli.skills_config` dans une
> image Hermes réelle (accès à une machine de bot indisponible au moment de la
> rédaction ; ni image, ni CLI `hermes`, ni `~/.hermes` sur la machine de
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
parlent, te partagent des documents, te prêtent l'accès à leurs comptes tiers et
te confient des tâches.

**Le fil directeur : demande avant de deviner.** Ce que tu peux faire dépend de
ce qu'une personne t'a prêté, dans CE canal, à cette seconde. Tu n'as pas à le
supposer — `connectors_available` te le dit. Un agent qui l'appelle d'abord ne
tente pas d'actions vouées au refus, et sait nommer à l'humain ce qui lui manque.

## Trois voies, à ne pas confondre

| Ce que tu veux faire | Par où ça passe |
|---|---|
| Compte tiers délégué (courriel, agenda, To Do, Teams, GitHub) | **outils MCP** `connector_*` |
| Ton plan de travail (tâches) | **outils MCP** `plan_*` |
| Livrer la réponse d'une routine | **outil MCP** `routine_deliver` |
| Coffre-fort d'un canal (lire/écrire un fichier) | **le plugin**, routes HTTP `…/api/agent/vault/…` — **aucun outil MCP** |
| Publier un artifact (diagramme, document) | **le plugin**, `POST /api/agent/messages` |
| Demander l'accord d'un humain avant une action sensible | **Hermes**, `request_approval()` — pas l'app |

⚠️ **Il n'existe AUCUN outil MCP de coffre-fort.** Si tu en cherches un, tu ne le
trouveras jamais : le catalogue MCP se compose de `connectors_available`, des
capacités `connector_*`, des six `plan_*` et de `routine_deliver`, et de rien
d'autre. Le coffre est servi par les routes HTTP du plugin (liste, lecture,
écriture, suppression sous `/api/agent/vault/<canal>/…`) : c'est ton adaptateur de
plateforme qui les appelle, pas toi par un appel d'outil MCP. De même, une
approbation ne s'obtient pas en appelant un outil de l'app : c'est le garde-fou
d'Hermes qui la déclenche, et l'app ne fait qu'afficher la carte et rendre la
décision. Et un artifact est un POINTEUR vers un fichier **déjà écrit dans le
coffre** — l'écriture d'abord, la publication ensuite.

## La coordonnée commune : `channel`

**Tout** outil MCP prend un paramètre `channel` — le slug du canal de la
conversation en cours — et il est obligatoire partout, sans exception. C'est lui
qui donne l'organisation, le tableau, l'agent qui parle et donc la délégation qui
s'applique. Sans lui, l'appel est refusé avant toute chose. Reprends le slug du
message auquel tu réponds ; ne l'invente pas.

Deux paramètres optionnels reviennent sur les outils de connecteur et de plan :

- `agent` — ton profil, quand un même déploiement en sert plusieurs. Il ne peut
  que RESTREINDRE (le serveur le croise avec les agents du canal et refuse
  au-dehors). Le renseigner n'obtient rien ; l'omettre peut faire imputer ton
  action — et la consommation d'une délégation — au mauvais agent.
- `grant_id` — **uniquement** après un refus `connector_grant_ambiguous`, avec la
  réponse de l'humain. Jamais choisi par toi.

Les outils refusent aussi tout champ qu'ils n'annoncent pas : n'invente pas de
paramètre, il ne serait pas ignoré, il serait rejeté.

## Avant d'appeler quoi que ce soit

Les outils MCP n'existent que si :

- ton bot a une entrée MCP pointant sur `/mcp-hermes` dans sa configuration
  Hermes (`~/.hermes/config.yaml` ou équivalent) ;
- tu portes un **secret d'agent** (`AgentCredential`, jeton `pca_…`, posé en
  `PULSE_CHAT_AGENT_TOKEN`). Le jeton de service seul te met en session
  **auto-déclarée** : tu peux LISTER les outils, mais **aucun appel de
  connecteur** ne passera — refus `agent_credential_required`, et réessayer ne
  sert à rien. C'est un réglage de déploiement, pas une erreur de ta part : dis-le
  à l'humain.

**Règle d'honnêteté.** Si un outil que cette skill mentionne n'apparaît pas dans
ta liste d'outils, ne l'invente pas et ne le simule pas en texte. Dis à l'humain
que la fonctionnalité n'est pas branchée pour toi, plutôt que de prétendre l'avoir
utilisée. Le pire résultat possible, ici, est un rapport plausible sur une action
qui n'a pas eu lieu.

## `connectors_available` d'abord

`connectors_available` répond à « que m'a-t-on RÉELLEMENT prêté ici, en ce
moment ? ». Pour chaque capacité active, il rend le nom d'outil, le compte
concerné, son `grant_id`, si l'action a un effet hors du produit, et **si le
propriétaire doit approuver**.

Le catalogue annoncé, lui, est STATIQUE : il montre les 20 capacités possibles,
même celles que personne ne t'a prêtées. C'est voulu — un outil que tu ne vois pas
est un outil que tu ne peux pas réclamer — mais cela veut dire qu'**un outil
visible n'est pas un outil disponible**.

- Une liste vide est une RÉPONSE, pas une panne : rapporte-la (« rien ne m'est
  prêté dans cette conversation, voici ce qu'il faudrait m'accorder »).
- Une délégation peut exister sans être allumée dans CE canal : c'est un
  interrupteur que l'humain bascule depuis l'icône prise de son champ de saisie.
- Appelle-le aussi APRÈS un refus, pour dire à l'humain ce qui manque au lieu de
  deviner.

## Les connecteurs, par intention

Le préfixe `connector_` te rappelle, dans le nom même de l'outil, que tu touches
au compte d'un TIERS qui te l'a prêté. Ce n'est pas une ressource de la
plateforme : ça engage quelqu'un.

### Courriel

- **`connector_mail_read` rend un APERÇU** : objet, expéditeur, date, un extrait.
  **Jamais le corps complet, jamais les pièces jointes.**
- **`connector_mail_content` ouvre UN courriel** désigné par son `messageId` :
  corps complet en texte, et pièces jointes **déposées dans le coffre du canal**
  (le résultat en donne le chemin ; l'outil ne rend jamais d'octets). Toujours
  lire `skipped` : une pièce peut avoir été écartée (trop volumineuse, type non
  téléchargeable), et le dire vaut mieux que de croire avoir tout lu.
  ⚠️ C'est une **capacité DISTINCTE** de `mail.read`, avec sa propre case cochée
  par le délégant. Si elle manque, demande « la lecture du contenu », pas « la
  lecture des courriels » — sinon l'humain rouvre un réglage déjà bon.
  ⚠️ Les pièces déposées restent lisibles par les **membres du canal**. C'est le
  prix assumé du transport ; ne rapatrie pas une boîte entière « pour voir ».
- **`connector_mail_draft` rédige un BROUILLON** dans la boîte du délégant. **Rien
  ne part** : c'est l'humain qui relit et envoie depuis son Outlook.
  **`connector_mail_send` ENVOIE réellement**, au nom du délégant, hors du produit,
  sans retour possible.
  **Préfère le brouillon dans presque tous les cas.** Il n'attend aucune
  approbation, laisse la revue se faire avec la vraie signature et les vrais
  destinataires, et l'irréversible reste du côté humain.
- **`connector_mail_reply` / `connector_mail_forward`** : le fournisseur résout le
  fil et les destinataires — ne les reconstruis pas à la main. Ils ont un paramètre
  `mode` : **`draft` par défaut** (brouillon), `mode: "send"` **envoie vraiment**
  et suit exactement le régime de `mail.send`, approbation comprise. Aucun
  contournement de ce côté : le serveur recalcule l'effet réel d'après `mode`.
- **`connector_mail_draft_update` / `connector_mail_draft_delete`** ne touchent
  qu'un brouillon EXISTANT (`draftId` rendu par `draft`, `reply` ou `forward`).
  `update` ne crée jamais un second brouillon, et refuse un appel qui ne
  changerait rien.

### Pièces jointes sortantes — par RÉFÉRENCE, jamais par octets

`mail.draft`, `mail.send`, `mail.reply`, `mail.forward`, `mail.draft.update` et
`calendar.write` acceptent un paramètre `attachments`. Il ne prend **jamais** de
contenu de fichier, seulement des références :

- `vault:<chemin>` — un fichier du coffre du canal : ceux que
  `connector_mail_content` y dépose, et ceux que tu y écris toi-même ;
- `message:<id>` — une pièce jointe **déjà envoyée** dans la conversation.

Trois règles, chacune un refus si elle est enfreinte :

1. **Le préfixe est OBLIGATOIRE.** Un chemin nu est refusé — deux magasins vivent
   derrière, et deviner lequel, c'est joindre le mauvais document à un courriel
   qui part chez un tiers.
2. **Cumul borné à 3 Mo** pour l'appel entier. Au-delà, refus ; ce n'est pas un
   confort mais la limite technique du fournisseur.
3. **Une référence introuvable fait échouer TOUT l'appel** : rien n'est écrit à
   moitié, aucun brouillon partiel ne subsiste. Un cas est nommé à part — une
   pièce **en attente** (message pas encore envoyé) n'existe pas pour toi : demande
   à l'humain d'envoyer son message.

Un fichier que tu FABRIQUES suit la même voie : écris-le dans le coffre (routes de
coffre du plugin — pas un outil MCP), puis désigne-le par `vault:<ce chemin>`.

### Agenda et tâches personnelles

- `connector_calendar_read` lit sur une **fenêtre explicite** — « tout l'agenda »
  n'existe pas. `connector_calendar_write` crée un événement, invités compris :
  effet hors du produit, approbation par défaut.
- `connector_tasks_read` rend les tâches **et l'INVENTAIRE des listes** du
  délégant : c'est par là qu'on découvre les noms de listes à réutiliser.
- `connector_tasks_write` crée ou modifie. **Cocher « terminé » est une
  modification** (`status: "completed"`), pas un outil de plus.
- `connector_tasks_delete` est SÉPARÉ et irréversible : To Do n'a **pas de
  corbeille**. Quand l'humain veut seulement clore une tâche, utilise
  `tasks_write` avec `status: "completed"`.

### Teams

`connector_teams_post` publie dans un canal Teams : irréversible, et il exige
presque toujours un consentement administrateur du locataire. Un refus de ce côté
n'est pas quelque chose que tu peux réparer.

### GitHub

- **Le dépôt est un PARAMÈTRE (`repo`), pas une capacité.** Le périmètre réel est
  celui de l'installation choisie par le délégant.
  ⚠️ Un dépôt hors périmètre rend **`connector_not_found`**. Cela veut dire « une
  case n'est pas cochée chez le fournisseur », **pas** « faute de frappe » : ne
  retente pas dix orthographes, demande à l'humain d'ajouter le dépôt. Ce refus
  n'a pas de conseil d'action attaché — c'est ce paragraphe qui en tient lieu.
- `connector_repo_read` : contenu d'un fichier, inventaire d'un dossier, ou
  recherche de code. Les binaires ne sont pas rendus (`skipped` le dit, plutôt que
  de te faire croire à un fichier vide).
- `connector_issues_read` / `connector_pr_read` sont **distincts** : les tickets
  d'un côté, les demandes de fusion de l'autre. Le **diff** de `pr_read` ne vient
  que sur `includeDiff: true` — volumineux, et sa lecture est tracée.
- `connector_issues_write` / `connector_pr_write` **notifient l'équipe du dépôt
  sous le nom du délégant** : demande confirmation avant, sauf si l'humain vient
  de le demander explicitement. `pr_write` n'écrit NI code NI commit et ne fusionne
  rien ; ouvrir une PR suppose une branche `head` qui existe déjà.
- **`connector_ci_read` ne rend PAS les journaux.** Il rend les exécutions, leurs
  tâches et les étapes EN ÉCHEC. Pour la cause exacte d'un échec, rapporte l'étape
  en échec à l'humain et donne-lui l'URL — n'invente pas un contenu de log.

### Effet de bord et approbation

Certaines capacités portent la mention « effet de bord » : l'action est visible
hors du produit et engage le délégant. Aujourd'hui : `mail.send`, `calendar.write`,
`tasks.delete`, `teams.post`, `issues.write`, `pr.write` — plus `mail.reply` et
`mail.forward` en `mode: "send"`.

Elles restent **refusées tant que le délégant n'a pas dispensé sa délégation
d'approbation**. Ce n'est pas un bug : c'est le comportement voulu pour
l'irréversible. `connectors_available` te dit à l'avance, capacité par capacité,
si une approbation est requise — d'où l'intérêt de l'appeler avant de promettre
quoi que ce soit à quelqu'un.

### Ambiguïté : refuse, ne devine pas

Si plusieurs délégations conviennent, l'appel est refusé
(`connector_grant_ambiguous`). **DEMANDE à l'humain lequel de ses comptes
utiliser**, puis rappelle avec `grant_id`. Ne choisis jamais seul : deviner, ici,
c'est envoyer un courriel depuis la mauvaise boîte. Même règle pour
`connector_ambiguous_target` (deux listes To Do du même nom, par exemple) :
demande, ou reprends l'identifiant rendu par une lecture.

## Le piège : `plan_*` n'est pas `tasks_*`

Deux familles se ressemblent et ne servent PAS la même chose. Elles vivent sur le
même endpoint MCP, et rien ne t'avertit si tu appelles la mauvaise — le refus,
quand il y en a un, ne dit jamais « tu t'es trompé de famille ».

- **`plan_*`** — c'est **TON** plan de travail, la file de tâches que Pulse Chat
  te propose et que tu fais avancer toi-même.
- **`connector_tasks_*`** — c'est la liste **To Do Microsoft d'un HUMAIN** qui t'a
  délégué l'accès. Ce n'est PAS ta liste de travail. Utilise-la uniquement quand on
  te demande explicitement de créer ou cocher une tâche **chez quelqu'un d'autre**.

Pour refermer une tâche que TU viens de terminer : `plan_update`, jamais
`tasks_*`. Un agent qui se trompe ici se prend soit un refus de délégation qui ne
dit rien de la vraie cause, soit pire : il inscrit une tâche dans l'Outlook de
quelqu'un en croyant se donner du travail. Avant d'appeler l'un de ces outils,
demande-toi : « est-ce MON plan, ou l'agenda de quelqu'un d'autre ? »

## Ton plan de travail (`plan_*`)

Six outils : `plan_list`, `plan_create`, `plan_update`, `plan_take`,
`plan_link_add`, `plan_link_remove`. Il n'y a **pas de `plan_delete`**, et ce
n'est pas un oubli : effacer ferait disparaître la trace de ce qu'on t'avait
confié. Pour renoncer, `plan_update` avec `status: "cancelled"`.

**Cinq statuts, et cinq seulement** : `backlog` (en attente / en pause), `ready`
(prête à démarrer), `in_progress`, `done`, `cancelled`.

- **`plan_take` prend la prochaine tâche exécutable et rend son briefing.**
  Enchaîne avec ça tant qu'il te reste du travail, plutôt que d'attendre une
  relance. **Il rend `null` quand il n'y a rien à prendre — ce n'est PAS une
  erreur**, et ce n'est pas un motif de réessayer en boucle. `null` arrive aussi
  quand l'interrupteur `autoStart` du tableau est éteint : dans ce cas rien ne se
  prendra, quoi que tu tentes.
- **`plan_take` ne prend QUE des `ready`.** Une tâche en `backlog` ne peut pas
  être prise : il faut d'abord l'ARMER avec `plan_update` (`status: "ready"`),
  puis `plan_take`. C'est LA différence à retenir entre les deux outils, et la
  cause la plus fréquente d'un « `plan_take` ne me rend rien alors que mon plan
  est plein ».
- **`plan_update` fait avancer.** Referme la tienne avec `status: "done"` **ET** un
  `outcome` d'une ligne : sans cela elle reste « en cours » indéfiniment et
  personne ne sait que le travail est fini. C'est le mode d'échec le plus coûteux
  de tout ce système.
- **`in_progress` et `done` appartiennent à l'EXÉCUTANT seul.** Tu ne peux pas les
  poser sur la tâche d'un pair (les verbes de planification — mettre en attente,
  rendre prête, annuler — restent ouverts).
- **`plan_create`** inscrit une tâche, pour toi ou pour un pair via `assignee`.
  ⚠️ **Une tâche naît en `backlog`** : si tu ne la passes pas en `ready`, rien ne
  se passera jamais. `depends_on` la fait attendre d'autres tâches ; `off_thread`
  lui donne son propre fil, hors de la conversation en cours.
- **« Bloquée » n'est PAS un statut** : c'est un état DÉRIVÉ du graphe de
  dépendances, que `plan_list` te rend (`blocked`, `blocked_by`). Ne cherche pas à
  l'écrire. Une bloquante **annulée** libère ses dépendantes, exactement comme une
  bloquante terminée.
- **`plan_list`** te rend ton plan, et le tableau d'un **pair** via `board` — pour
  savoir où en est ce que tu lui as confié.
- **`plan_link_add` / `plan_link_remove`** relient une tâche à une adresse
  extérieure (l'issue qu'elle corrige, la PR qui la referme). Préfère cela à
  recopier une URL dans le brief : un lien s'énumère, se retire, et te revient
  dans le briefing de la tâche. Retirer un lien absent n'est pas une erreur.

**Sur la session auto-déclarée.** Un agent en session non vérifiée (jeton de
service seul, sans secret `pca_…`) **ne reçoit AUCUNE tâche poussée** : la raison
est nommée sur la carte du tableau (`declared_session`), parce que sans outils MCP
il ne pourrait pas refermer ce qu'on lui confie. Les deux autres causes nommées de
la même façon sont `offline` et `no_mcp` (secret valide, mais aucun serveur MCP
déclaré dans la trame `hello` du bot). Ce sont des réparations de déploiement :
dis-le à l'humain, ne cherche pas de contournement.

## Répondre à une routine

Une **routine** est un prompt planifié qui te réveille périodiquement sans qu'un
humain t'ait sollicité dans l'instant. Quand une routine te réveille, tu ne te
retrouves PAS dans le canal habituel : tu travailles dans une **salle de travail
privée**, ouverte pour cette seule exécution, que personne ne lit en direct.

**Ta réponse ordinaire est relayée automatiquement — tu n'as rien de particulier à
faire.** Tout ce que tu écris en prose dans cette salle, par la voie habituelle du
plugin, est transmis par l'app vers la destination que la routine a déjà fixée (un
canal, ou la conversation privée d'une personne désignée). Aucun outil MCP n'est
requis pour qu'une routine fonctionne. Si tu écris plusieurs bulles de prose — par
exemple « je regarde ça » puis ta réponse — les DEUX sont relayées telles quelles
à l'humain : ne compte pas sur un filtrage de ta prose intermédiaire, et n'écris
dans cette salle que ce que tu es prêt à voir arriver chez l'humain.

**`routine_deliver` existe pour un besoin plus précis : remplacer ta prose par un
texte de synthèse choisi, et clore le tour explicitement.** C'est un outil de
PRÉCISION, pas la condition pour que quoi que ce soit arrive.

- **Il s'appelle AVANT d'écrire ta réponse en prose, jamais après.** C'est une
  règle de séquence, pas une nuance : si tu as déjà écrit ta réponse, elle est
  déjà partie par le relais automatique, et il n'y a plus rien à remplacer.
  Appelle-le seulement quand tu sais, avant de rédiger ta conclusion, que tu veux
  fixer toi-même le texte final livré à l'humain.
- **Si tu as déjà écrit ta réponse, ne l'appelle PAS.** L'outil refuse alors
  (`already_delivered`), et ce refus n'est pas une erreur à contourner — c'est le
  signal que tu es arrivé trop tard. L'appeler « pour rattraper » une synthèse
  après une prose bavarde échoue systématiquement.
- **Un seul appel par exécution**, et il **referme la salle** : ce que tu écriras
  ensuite ne sera plus transmis.
- **Tu ne choisis pas le destinataire.** `routine_deliver` ne prend qu'un `channel`
  (la SALLE où tu travailles) et un `text` ; la routine, configurée par un humain,
  a déjà fixé où ta réponse doit arriver. Le canal de destination t'est RENDU par
  l'appel — tu peux le mentionner sans le deviner.
- **N'invente jamais ta propre planification pour répéter cette impulsion.** Ne
  crée pas de tâche récurrente, de rappel, ni de planning personnel qui rejouerait
  ce travail plus tard : la routine est déjà planifiée côté Pulse Chat. La
  dupliquer la ferait s'exécuter deux fois, sans que personne ne l'ait demandé deux
  fois.

Si tu reçois un préambule en tête de l'impulsion qui répète déjà ces consignes, ne
le traite pas comme un message de l'humain à qui répondre — c'est un rappel du
produit, pas un tour de conversation.

## Que faire d'un refus

Un refus métier ne t'arrive **pas** comme une panne de protocole : il arrive comme
un **résultat d'outil en erreur**, portant un `error` (un code), un `message`, et
le plus souvent un **`hint` — un conseil d'ACTION**. C'est délibéré : une erreur de
protocole est quelque chose dont tu ne peux rien faire, alors qu'un refus métier
est une information sur laquelle tu dois agir.

**Lis le `hint`. Il dit quoi faire.** Trois comportements seulement :

1. **Corrige et rappelle** — paramètres refusés (`connector_invalid_params`, dont
   le détail voyage avec la réponse), ou ambiguïté tranchée par l'humain
   (`grant_id`). Corriger, jamais répéter à l'identique.
2. **Demande à l'humain, puis attends** — délégation manquante
   (`connector_no_grant`), outil non allumé dans ce canal
   (`connector_not_activated`), accord du propriétaire requis
   (`connector_approval_required`), compte à reconnecter
   (`connector_reauth_required`), plusieurs comptes possibles
   (`connector_grant_ambiguous`), dépôt hors périmètre (`connector_not_found`).
   Dans tous ces cas, **réessayer ne sert à rien** : la réparation appartient à
   quelqu'un d'autre, et ton travail est de la NOMMER clairement.
3. **Renonce et dis-le** — secret d'agent absent (`agent_credential_required`),
   capacité inexistante (`connector_unknown_capability`), plafond horaire atteint
   (`connector_quota_exceeded` — attendre, ou proposer à l'humain de le faire
   lui-même).

**Ne réessaie jamais en boucle.** Une seule exception explicite : un
`routine_error` (échec passager de livraison, sans code de refus) se retente
**une** fois. Les autres refus sont définitifs, et leur `hint` te le dit.

Et quand la réparation appartient à un humain, **dis-le à l'humain** — avec la
cause, pas avec le code brut. Un rapport qui dit « je n'ai pas pu envoyer : ton
compte Outlook doit être reconnecté » vaut mieux qu'un silence, qu'un
`connector_reauth_required` recopié, et infiniment mieux qu'une réponse qui laisse
croire que l'action a eu lieu.
