# Plugin Hermes « pulse-chat »

Adaptateur de plateforme [Hermes Agent](https://hermes-agent.nousresearch.com)
pour **Pulse Chat**. Adaptateur **mince** : il traduit et transporte, toute la
logique métier vit dans l'app Nuxt (spec `docs/superpowers/specs/2026-08-04-pulse-chat-design.md` §6).

```
hermes-plugin/pulse-chat/
├── plugin.yaml         # métadonnées + variables d'env
├── __init__.py         # expose register(ctx)
├── adapter.py          # PulseChatAdapter + register()
├── classification.py   # classification pure message/tool_event (0 dépendance hermes)
├── hello.py            # frame hello multi-profils pure (0 dépendance hermes)
├── browser.py          # fournisseur de navigateur `pulse` + pulse_browser_handoff
├── podcast.py          # pulse_podcast : payload, résultats et refus (pur)
├── subsessions.py      # pulse_open_subsession / pulse_subsession_report (pur)
├── credentials.py      # identifiants d'agent : relais du coffre « Passwords & Logins » d'Hermes
├── netguard.py         # garde-fous réseau : schémas, redirections, clair, journaux
└── tests/              # pytest (sans hermes installé)
```


## Installation rapide (repo publié)

Ce plugin est publié sur **github.com/aidalinfo/hermes-plugin-pulse-chat**
(miroir automatique du monorepo `PROJET-pulse-chat`, dossier `hermes-plugin/pulse-chat/`).

Voir aussi [`skills/guide/SKILL.md`](./skills/guide/SKILL.md) (enregistré en `pulse-chat:guide`, nommé dans `platform_hint`) : ce que **l'agent** doit savoir pour se
servir de Pulse Chat une fois branché : les outils MCP de `/mcp-hermes`
(lecture de canal `channels_list` / `channel_context` / `channel_members` /
`channel_artifact` / coffre, `connectors_available` et les capacités
`connector_*`, le plan de travail `plan_*`, `routine_deliver`), l'écriture au
coffre et la publication d'un fichier (outils du plugin `pulse_vault_write` et
`pulse_publish_artifact`, secours MCP `channel_vault_upload_url` /
`channel_artifact_publish`), le piège `plan_*` vs `tasks_*`, brouillon contre
envoi réel, les pièces jointes par référence préfixée, et quoi faire d'un
refus. Jusqu'à la 1.11.0, ce fichier vivait à la racine et n'était enregistré
nulle part : aucun agent ne le lisait.

```bash
# Voie 1 — CLI Hermes
hermes plugins install aidalinfo/hermes-plugin-pulse-chat --enable

# Voie 2 — clone direct (nom du dossier = clé du plugin : pulse-chat)
git clone https://github.com/aidalinfo/hermes-plugin-pulse-chat ~/.hermes/plugins/pulse-chat
hermes plugins enable pulse-chat
```

Puis configurer les variables d'env (voir plus bas) et `hermes gateway restart`.

## Fonctionnement

- **app → plugin** : WebSocket `ws(s)://<PULSE_CHAT_URL>/ws/hermes` (header
  `Authorization: Bearer <PULSE_CHAT_TOKEN>`). À l'ouverture (et à chaque
  reconnexion), le plugin envoie une frame
  `{type: 'hello', profiles: [...], agentName: '...'}` : le serveur enregistre
  ce bot pour CES profils (`Channel.hermesProfile`, last-wins par profil) et
  rejoue les messages non livrés de ces profils uniquement. Événements
  `message.created` → `MessageEvent` → agent, puis ack
  `{type: 'ack', messageId}` (le serveur marque `agentDeliveredAt` et rejoue
  les messages non ackés à la reconnexion).
- **plugin → app** : `POST <PULSE_CHAT_URL>/api/agent/messages`
  (header `Authorization: Bearer <PULSE_CHAT_TOKEN>`), body JSON
  `{channelSlug, kind, content, raw, hermesMessageId, tool, phase, replyToHermesId}`
  avec `kind = 'message' | 'tool_event'`.
- **Classification** (`classification.py`, décision 3 du plan) :
  - `edit_message()` (tool progress accumulé) ⇒ **toujours** `tool_event`
    (upsert serveur par `hermesMessageId`) ;
  - réponse **réexpédiée** par Hermes (« ♻️ Recovered reply — … : », ligne
    vide, puis la réponse — registre de livraison, après un échec d'envoi) ⇒
    classée sur son **corps**, qui est aussi ce que la bulle affiche ; le
    marqueur ne reste que dans `raw` (1.15.3 — avant, toute la réponse partait
    en `interim`, repliée sous « 1 activité d'outil ») ;
  - préfixe réservé en début de contenu (⚡ ⏳ ⏩ ↪ ♻️ ♻ 🔄 ✅ ❌ 💬 💻)
    ⇒ `tool_event` phase `interim` ;
  - motif tool-progress `^<emoji court> <mot>…`
    (ex. `🔍 Searching the web for "…"`, `💻 terminal\n\`\`\`…`)
    ⇒ `tool_event` phase `progress`, outil extrait ;
  - sinon ⇒ `message`. **Doute ⇒ `tool_event`** (jamais `Message`) — le contenu
    original part toujours dans `raw`.
- **Documents** : les URLs présignées restent dans le **texte** du message
  (l'agent les lit avec ses outils, validité ~15 min) ; les **images** de
  `mediaUrls` sont téléchargées localement et passées en `media_urls` (vision).
- **Notes vocales** (`voice.py`) — les deux sens :
  - *entrant* : une frame `message.created` portant `messageType: 'voice'`
    (ou un média `audio/*`) produit un `MessageEvent` de type **`VOICE`**, audio
    matérialisé en fichier local. C'est ce type qui déclenche, **côté Hermes**,
    la transcription automatique (`stt.provider`) **et** l'auto-TTS de la
    réponse — un audio typé `TEXT` donne un agent sourd et muet, sans erreur ;
  - *sortant* : `send_voice()` poste l'audio brut sur
    `POST <PULSE_CHAT_URL>/api/agent/voice/<slug>` (`Content-Type` = type audio,
    `x-filename` et `x-caption` percent-encodés). Appelé par l'auto-TTS
    (`play_tts`) **et** par le tool `text_to_speech` de l'agent.
    Sans cette méthode, le repli d'Hermes posterait `🔊 Audio: <chemin>` — un
    chemin de conteneur dans le fil d'un client, et aucun son.
  - L'audio sortant suit le chemin des **pièces jointes** (S3, presign 15 min,
    audit), **pas le coffre-fort** : le coffre est un espace de travail borné
    par un quota de fichiers, qu'une note vocale par réponse remplirait.
- **Voix en flux** (`voxtral_streaming.py`) : Hermes n'enregistre que quatre
  streamers TTS (`elevenlabs`, `openai`, `gemini`, `xai`) et refuse de changer
  de fournisseur en silence — un agent en `tts.provider: mistral` ne parlerait
  donc qu'une fois la réponse **entièrement rédigée**. Ce module enregistre
  `mistral` comme streamer : `POST /v1/audio/speech` avec `stream: true` et
  `response_format: pcm`, événements SSE `speech.audio.delta`.
  **Mesuré le 2026-08-14 : premier son à 0,53 s** sur une phrase courte.
  ⚠️ Le flux `pcm` de Mistral est du **float32** (le `wav` du même endpoint est
  de l'int16) : le module convertit, sans quoi l'audio sort deux fois plus long
  et inintelligible. Sans effet sur une version d'Hermes antérieure au contrat
  de streaming (v0.20) — l'installation est alors inerte.
- **Contrat de streaming audio** (`audio_stream.py`, `#60671`) : l'adaptateur
  implémente `supports/begin/write/finish/abort_streaming_tts` et **transporte**
  le PCM que produit Hermes ; le découpage en phrases, la synthèse et la
  suppression du doublon « audio complet » restent en amont.
  **Négociation** : `supports_streaming_tts` ne répond `true` que si l'app l'a
  annoncé dans son `hello.ack` — `{"audio": {"streaming": true,
  "sampleRate": 24000}}`. Sans ce bloc, rien ne change : ce plugin peut donc
  être déployé **avant** l'app.

  Trames sur le WS existant (bornes en JSON, PCM en binaire) :

  ```
  {"type":"audio.begin","chatId","streamId","format":{sampleRate,channels,sampleWidth}}
  <binaire>  octet 0 version(1) · octet 1 type(1) · octets 2-3 longueur d'en-tête (uint16 LE)
             en-tête JSON {"chatId","streamId","seq"} · puis PCM int16 LE mono
  {"type":"audio.end","chatId","streamId","interrupted":bool}
  {"type":"audio.abort","chatId","streamId","error":string|null}
  ```

  Le `streamId` permet à l'app de **jeter** les morceaux d'un tour interrompu ;
  le `seq` rend visible une perte d'ordre, qui s'entendrait sinon comme un
  hoquet inexplicable. `decode_audio_frame()` est la définition exécutable du
  format — l'app la réimplémente en TypeScript d'après elle.

- **Approbations du garde-fou en CARTE** (`send_exec_approval`) : depuis Hermes
  **v2026.9.14**, le runner ne regarde plus seulement si la méthode existe — il
  consulte d'abord `supports_exec_approval_buttons()`, dont la version de base
  ne dit oui que si `_send_exec_approval_prompt` est surchargé. L'adaptateur
  surcharge donc la **sonde** (réponse `True`) : sans elle, Hermes repostait son
  invite texte « Reply `/approve`… » sans aucune erreur (corrigé en **1.13.1**).
  La sonde plutôt que le nouveau crochet, pour rester compatible avec un Hermes
  antérieur qui ne l'appelle pas.

- **Questions de l'agent en CARTE** (`questions.py`, `send_clarify`) : quand
  Hermes pose une question à l'humain (primitive `clarify`) et bloque son thread
  en attendant, l'adaptateur poste une carte à boutons dans le fil plutôt que de
  laisser Hermes rendre son invite texte (« ❓ … / 1. … / Reply with the
  number… »). Le point d'extension est le **jumeau** de `send_exec_approval`, et
  il manquait pour la même raison — le repli texte arrivait ici en `ToolEvent`
  intitulé du premier mot de la question, replié sous « 1 activité d'outil ».

  Trois méthodes, et rien d'autre :

  ```
  send_clarify(chat_id, question, choices, clarify_id, session_key)
      -> POST {kind: "question_request", requestId: <clarify_id>, question, choices}
  retire_clarify_card(clarify_id, notice)      # Hermes a relâché son attente
      -> POST {kind: "question_retire", requestId: <clarify_id>}
  _handle_question_reply(frame)                # trame WS question.reply
      -> resolve_gateway_clarify(clarify_id, answer)   # débloque le thread agent
  ```

  `requestId` **EST** le `clarify_id` : lui seul dénoue l'attente. `answer` est
  le libellé **brut** du choix, suffixe « (Recommended) » compris — c'est l'app
  qui le retire à l'affichage seulement. Le **multi-select** est délégué à la
  base (liste numérotée + capture de texte) : la carte ne coche qu'un choix.
  Tout échec de POST rend `SendResult(success=False)` et c'est le runner
  d'Hermes qui replanifie son invite texte — on ne rappelle jamais `super()`
  soi-même, ce serait deux invites pour une question. Sans `tools.clarify_gateway`
  (Hermes trop ancien), la carte n'est pas postée du tout : des boutons qu'on ne
  saurait pas dénouer figeraient l'agent.

### Faire valider un plan ou un livrable (`pulse_request_approval`)

Le premier **outil** que le plugin enregistre lui-même (`ctx.register_tool`,
présent dès v2026.8.3) — tout le reste passe par l'adaptateur ou par le MCP de
l'app. L'agent soumet son plan ou son livrable ; l'app le route vers les
**approbateurs** désignés pour lui dans son organisation (Réglages → Agents),
et l'outil attend leur décision.

```
pulse_request_approval(title, body?, reason?, steps?, scope?, impact?,
                       risk?, reversible?, attachments?)   # outil, toolset "pulse_chat"
    -> POST {kind: "gate_request", requestId: gate-<hex>, title, body, …rubriques}
    -> 400 unrecognized_keys (app antérieure) : REPOSTE une fois en titre + corps
_handle_gate_reply(frame)                      # trame WS gate.reply
    -> attente active : l'outil rend {status, granted, comment}
    -> AUCUNE attente  : décision injectée comme MessageEvent entrant
```

**Rubriques structurées (≥ 1.9.0).** `reason` (pourquoi), `steps`
(`[{label, command?}]`, dans l'ordre), `scope` (étiquettes), `impact`, `risk`
(`low|medium|high`), `reversible` (booléen, omis = non annoncé) et
`attachments` (références `vault:<chemin>` / `message:<id>`, jamais des
octets) alimentent l'écran /approvals de l'app : stepper, pastilles de risque,
pièces jointes téléchargeables par l'approbateur. Toutes facultatives ; `body`
le devient dès qu'une rubrique est fournie. Le plugin **transporte** : il
garde ce qui a la bonne forme et coupe aux bornes de l'app (`gates.py`, miroir
de `shared/gates.ts`), il ne déduit rien et ne corrige pas une référence sans
préfixe — c'est l'app qui la refuse (`gate_attachment_invalid_ref`) ou qui ne
la résout pas (`gate_attachment_not_found`), et ce refus revient au modèle
avec un conseil. Les **descriptions des paramètres** enseignent quand et
comment les remplir : le skill n'étant jamais annoncé, c'est là que le modèle
apprend.

**Compatibilité.** Plugin ancien + app récente : rien ne change, la trame
titre + corps est reçue comme avant. Plugin ≥ 1.9.0 + app antérieure : le
schéma d'entrée de l'app est STRICT et refuse les rubriques en 400
`unrecognized_keys` ; le plugin reposte alors **une fois** la même demande
(même `requestId`) en titre + corps, rubriques repliées en Markdown
(`legacy_payload`), pièces jointes seulement **nommées** (sans chemin). Seul ce
refus-là déclenche le repli : un 400 qui porte un `code` n'est jamais
contourné — reposter sans les pièces jointes livrerait à l'approbateur un
dossier amputé.

Quatre choses qui ne se devinent pas :

- **Le canal n'est pas un paramètre.** Il est lu dans
  `gateway.session_context` (`HERMES_SESSION_CHAT_ID`, `ContextVar`
  task-local) : le modèle ne peut pas soumettre au nom d'une conversation où il
  n'est pas.
- **L'attente est bornée par Hermes, pas par nous.** `model_tools._run_async`
  coupe un outil asynchrone à **300 s** sur le chemin de la passerelle.
  L'outil attend donc `GATE_WAIT_SECONDS` (270 s), puis rend `pending` en
  disant à l'agent de s'arrêter. La demande, elle, **n'expire jamais** côté
  app : la décision tardive arrive en **message entrant** `[Approbation] …`,
  le même chemin qu'après un redémarrage du bot, où l'attente a disparu avec le
  process. Dédoublonnée : un rejeu au `hello` ne fait pas exécuter deux fois le
  même plan.
- **Ce message porte TOUJOURS un `user_id` (≥ 1.15.4).** Après un redémarrage,
  aucune source n'est mémorisée pour le canal et le plugin en fabrique une.
  Jusqu'en 1.15.3 elle avait `user_id=None` : Hermes la mettait en file pendant
  la restauration de démarrage puis la **jetait en silence** au rejeu
  (`_hm_admit_event` : « Ignoring message with no user_id »), si bien que le
  chemin « bot redémarré » n'a jamais fonctionné. La source de repli
  (`_fallback_source`, partagée avec la relance du navigateur) porte
  l'identifiant du décideur (`gate.decidedBy.userId`), sinon
  `pulse-chat:system`. Elle passe l'autorisation parce que
  `PULSE_CHAT_ALLOW_ALL_USERS=true` (ou `GATEWAY_ALLOW_ALL_USERS`) accepte tout
  `user_id` non vide ; un bot restreint par `PULSE_CHAT_ALLOWED_USERS`
  jetterait encore un repli dont l'identifiant n'y figure pas. Et la clé de
  session d'un `group` inclut le participant (`group_sessions_per_user`, vrai
  par défaut) : la décision ouvre la session du **décideur** dans le canal, qui
  n'est celle de la demande que si c'est lui qui l'a faite — d'où le titre de
  la demande dans le texte.
- **Autre boucle, autre Future.** Le handler tourne sur la boucle que
  `_run_async` lui ouvre dans un thread, pas sur celle du WebSocket : l'attente
  est un `concurrent.futures.Future` (sûre entre threads), et le POST est
  planifié sur la boucle du WebSocket (`run_coroutine_threadsafe`).
- **Nom préfixé.** `register_tool` sans `override=True` face à un nom déjà pris
  rend `None` sans lever : `request_approval` nu risquerait la collision avec
  la machinerie d'approbation que le cœur d'Hermes construit. Un `None` est
  journalisé.

L'outil est annoncé au modèle par sa **description** et par `platform_hint` ;
le mode d'emploi détaillé est un skill enregistré par le plugin
(`skills/approvals/SKILL.md`, résolu en `pulse-chat:approvals`) — **jamais
annoncé** dans `<available_skills>`, d'où le fait que les deux textes le
nomment. Un refus de l'app (`no_approver_configured`, **422**) est lu dans le
corps de la réponse et rendu tel quel à l'agent.

### Déposer et montrer un fichier (`pulse_vault_write`, `pulse_publish_artifact`)

Deux outils du plugin (≥ 1.10.0), enregistrés à côté de
`pulse_request_approval`. **Ils manquaient** : `vault_write` et
`publish_artifact` sont des méthodes de l'adaptateur, que le modèle ne voit
pas, et aucun outil ne les appelait. Un agent qui avait produit un PDF n'avait
donc aucun moyen de le poser dans la conversation — le support `kind: "file"`
de la 1.9.1 publiait dans une méthode que rien ne déclenchait, et l'ancienne
doc (« c'est ton adaptateur qui appelle le coffre ») envoyait l'agent chercher
un point d'entrée inexistant.

```
pulse_vault_write(path, local_path | content)          # outil, toolset "pulse_chat"
    -> PUT /api/agent/vault/<canal>/<path>              (fichier local EN FLUX)
pulse_publish_artifact(kind, title, path | content, artifact_id?)
    -> [PUT du contenu texte]  puis  POST {kind: "artifact", artifactKind, path}
```

Le geste pour un PDF : `pulse_vault_write(path="artifacts/devis.pdf",
local_path="/tmp/devis.pdf")`, puis `pulse_publish_artifact(kind="file",
path="artifacts/devis.pdf", title="Devis — V4", artifact_id="devis-client")`.

- **`local_path`, jamais des octets en base64.** Le plugin tourne dans le
  process de la passerelle, là où l'agent a généré son fichier : il le lit et
  l'envoie en flux (`Content-Length` annoncé, plafond de 100 Mo vérifié AVANT
  d'ouvrir la connexion). C'est pour cela que l'écriture vit ici et non dans le
  MCP de l'app. ⚠️ Si le terminal de l'agent tourne dans un bac à sable séparé
  (backend Docker, SSH…), son disque n'est pas celui de la passerelle : l'outil
  rend `local_file_not_found` en le disant.
- **Le canal n'est pas un paramètre**, comme pour `pulse_request_approval` :
  il vient de `gateway.session_context`, et l'appel est planifié sur la boucle
  du WebSocket (jeton de session = identité de l'agent auteur).
- **Aucun succès sans 2xx de l'app.** Les méthodes `vault_write` /
  `publish_artifact` rendent un booléen et journalisent ; les outils rendent le
  **message** de l'app (`statusMessage`) et un conseil par statut, pour que le
  modèle sache s'il corrige un chemin ou prévient un humain.
- **Écrire n'affiche rien** : `pulse_vault_write` le dit dans sa réponse, et
  `platform_hint` nomme les deux outils ensemble.
- **Aucune liste noire de fichiers locaux** : l'agent a déjà un terminal qui
  affiche n'importe quel fichier en une commande. La frontière réelle est la
  configuration de ce terminal, pas une liste qui donnerait l'illusion de
  fermer quelque chose.

Aucune mise à jour de l'app n'est requise : les routes existent déjà.

### Faire un podcast (`pulse_podcast`)

Outil du plugin (≥ 1.16.0), **app ≥ 0.45.0 requise**. L'agent écrit un titre
et des chapitres en prose parlée ; **l'app** fait tout le reste — synthèse
(voix Voxtral de l'agent), dépôt au coffre, carte dans le fil (lecteur,
chapitres, transcription). Le plugin reste mince : il transporte.

```
pulse_podcast(title, chapters: [{title, summary?, text}])   # outil, toolset "pulse_chat"
    -> POST /api/agent/podcasts/<canal>  {title, chapters}
    <- 202 {podcastId, status: "queued"}
```

- **Réponse immédiate `queued`.** La synthèse prend plusieurs minutes, au-delà
  du plafond de 300 s qu'Hermes impose à un outil asynchrone : l'outil n'attend
  pas, la carte apparaît d'elle-même. Consigne rendue au modèle : ne pas
  recopier le texte en message, ne pas le lire à voix haute. Même titre ⇒
  nouvelle version de la même carte.
- **Aucune troncature, aucune borne appliquée par le plugin.** Les bornes
  (titre ≤ 120, 1 à 20 chapitres, titre de chapitre ≤ 120, résumé ≤ 200,
  total des `text` entre 200 et 15 000 caractères) sont **annoncées** dans le
  schéma et la description, jamais appliquées par coupe : un podcast tronqué
  ferait s'arrêter la voix au milieu d'une phrase. L'app refuse avec un code
  nommé, la borne dans le message.
- **Le canal n'est pas un paramètre**, comme pour les autres outils du plugin.
- **Refus relayés tels quels** (`code`, `message`, conseil `next`) :
  `voice_disabled` (422, voix de l'agent éteinte), `podcast_in_progress` (409),
  `podcast_text_too_short` / `podcast_text_too_long` (400 / 413),
  `podcast_synthesis_unavailable` (501 — rendu « synthèse indisponible sur
  cette instance »), `channel_not_found` (404, ou app < 0.45.0 qui ne connaît
  pas la route), `invalid_request` (400 de schéma, issues Zod résumées).
- **`kind: "podcast"` existe côté artefacts mais ne se publie pas** : il est
  hors de l'enum de `pulse_publish_artifact`, qui le refuse en nommant
  `pulse_podcast` (`podcast_not_publishable`).

**Diagnostic — « l'agent ne propose pas de podcast ».** L'outil n'existe que
sur une image du plugin ≥ 1.16.0, et rien ne le signale ailleurs : vérifier la
version réellement chargée (`hermes plugins list`, et `pulse_podcast` dans la
liste des outils du bot), puis qu'**aucun dossier de sauvegarde du plugin**
(ex. `pulse-chat.bak/`) ne traîne sous `/opt/data/plugins/` — Hermes peut le
charger à la place de la nouvelle version. `platform_hint` est le seul texte
qui fait connaître l'outil au modèle. Un refus `channel_not_found` sur un canal
qui existe signale une app antérieure à 0.45.0.

### Ouvrir une sous-session (`pulse_open_subsession`, `pulse_subsession_report`)

Outils du plugin (≥ 1.17.0), **app ≥ 0.51.0 requise** (docs/47 de l'app,
Tête-à-tête). L'agent principal d'un fil Tête-à-tête ouvre **de lui-même** une
sous-session — une conversation séparée, visible de la personne dans son fil —
où il réunit 1 à 5 agents qu'il a le droit d'appeler ; il y **participe**, puis
en rapporte le résultat. **L'app** fait tout le reste (canal, membres, carte
dans le fil, livraison du rapport) ; le plugin transporte.

```
pulse_open_subsession(agents: [profil], title, brief, subsession?)   # outil, toolset "pulse_chat"
    -> POST /api/agent/subsessions/open    {channel, agents, title, brief, subsession?}
    <- 200 {subsession, slug, status}

pulse_subsession_report(text)                                         # appelé DEPUIS la sous-session
    -> POST /api/agent/subsessions/report  {channel, text}
    <- 200 {subsession, status, delivered}
```

- **Le canal n'est pas un paramètre** : `channel` est lu dans
  `gateway.session_context` — le fil Tête-à-tête pour l'ouverture, la
  sous-session pour le rapport. `emitterProfile` n'est jamais envoyé (l'en-tête
  de session dit déjà quel agent parle).
- **Réponse immédiate** : l'app n'attend pas les autres agents. Leurs réponses
  arrivent dans la sous-session comme des messages ordinaires ; le rapport
  revient au fil principal comme un **message entrant ordinaire** marqué
  `[Sous-session « titre » · rapport]` (ou `· sans rapport]` quand l'app
  constate un silence), où l'agent répond à la personne. Aucune trame nouvelle.
  Avec `subsession` (slug `ss-…`), l'ouverture **relance** une sous-session
  existante (nouvelle consigne, mêmes agents). Plusieurs rapports successifs
  sont permis ; `delivered: false` n'est pas un échec, l'app rejoue la remise.
- **La consigne est le SEUL contexte** de la sous-session (les autres agents
  n'ont pas lu la conversation) : la description l'exige complète. Les fichiers
  passent par le coffre, partagé avec le fil.
- **Aucune borne appliquée par le plugin** : 1 à 5 agents, titre ≤ 120,
  consigne et rapport ≤ 8 000 caractères sont **annoncés**, jamais appliqués par
  coupe. L'app refuse avec un code nommé.
- **Refus relayés tels quels** (`code`, `message`, conseil `next`) :
  `agent_session_required` (409, transitoire), `channel_not_found` (404),
  `subsession_not_allowed_here` (409 : pas un fil Tête-à-tête, pas servi par cet
  agent, ou depuis une sous-session), `callee_not_allowed` (422 — **la liste
  `allowed`** des agents appelables, ≤ 10, est rendue au modèle pour qu'il
  corrige au coup suivant, avec `notAllowed`), `callee_unreachable` (422,
  `agent`), `callees_required` / `too_many_callees` / `duplicate_callee` /
  `opener_cannot_be_callee` / `title_required` / `title_too_long` /
  `brief_required` / `brief_too_long` (400, `max`), `subsession_not_found` (404),
  `subsession_closed` (409), `subsession_callees_fixed` (400),
  `not_subsession_opener` (409), `text_required` / `text_too_long` (400),
  `invalid_request` (400 de schéma, issues Zod résumées), et
  `subsessions_unavailable` (404 **sans code** : app < 0.51.0 qui ne connaît
  pas la route).

**Prérequis — chaque bot APPELÉ doit porter son identité** (session vérifiée,
ou secret d'agent `pca_…`). Une sous-session est multi-agents : sans identité,
les réponses d'un appelé y sont refusées en 409 `emitter_ambiguous`, **sans
rien à l'écran**.

**Diagnostic — « la sous-session ne s'ouvre pas ».** Dans l'ordre :
1. la version réellement chargée : `hermes plugins list`, et
   `pulse_open_subsession` dans la liste des outils du bot (≥ 1.17.0). Vérifier
   qu'**aucun dossier de sauvegarde du plugin** (ex. `pulse-chat.bak/`) ne
   traîne sous `/opt/data/plugins/` — Hermes peut le charger à la place de la
   nouvelle version. `platform_hint` est le seul texte qui fait connaître les
   outils au modèle ;
2. le réglage **« Peut appeler »** de l'agent dans CETTE organisation
   (Réglages › Agents › Accès), **vide par défaut** : un refus
   `callee_not_allowed` avec `allowed: []` le signale ;
3. la joignabilité : un appelé désactivé, ou en accès restreint qui n'ouvre pas
   à la personne du fil, rend `callee_unreachable` ;
4. le fil est-il un Tête-à-tête ? Ailleurs, `subsession_not_allowed_here`. Un
   `subsessions_unavailable` signale une app antérieure à 0.51.0.

### Identifiants d'agent (trame `credentials.command`)

À partir de la **1.18.0**, **app ≥ 0.53.0 requise** (docs/48 de l'app). Un
**admin d'instance** voit, ajoute, supprime et — avec garde-fous — révèle les
identifiants du coffre « Passwords & Logins » d'Hermes (`agent/vault_store.py`,
Hermes **≥ v2026.9.11**) depuis le sous-onglet **Identifiants** de la page de
l'agent. Plus besoin de `docker exec … hermes vault add`.

**Le bot doit porter un secret d'agent `pca_…`** (`PULSE_CHAT_AGENT_TOKEN`,
AgentCredential émis dans Pulse Chat) : l'app n'envoie AUCUNE commande
d'identifiants à une connexion authentifiée par le jeton de service partagé (un
ajout transporte un mot de passe), et refuse en 403 l'instantané et les
réponses d'une session non vérifiée. Sans secret, l'onglet dit « posez un
secret d'agent » et le journal du bot montre l'instantané refusé (HTTP 403).

**Pur transport** (`credentials.py`) : qui a le droit, la fraîcheur de session,
le débit, les doublons vivent dans l'app. La **vérité reste dans le bot** :
l'app ne garde qu'un instantané de MÉTADONNÉES.

```
hello                    -> capabilities.features: ["credentials"]   (seulement si `import agent.vault_store` réussit)
hello.ack                -> POST /api/agent/credentials/snapshot  {profile, items}   (métadonnées seules)
credentials.command      <- {requestId, profile, op: add|remove|reveal, …}
   add    -> add_item("login", label, {identifier_type, identifier, password, [otp_secret]}, origin=)
   remove -> remove_item(handle)                (poignée `vault_…` locale seulement)
   reveal -> resolve_secret(handle)["password"] ou totp_now(otp_secret)   (le CODE, jamais la clé)
   puis, pour add/remove : POST …/snapshot      (AVANT la réponse : l'app relit la liste)
          -> POST /api/agent/credentials/result/<requestId>  {ok, value?} | {ok: false, code, message}
```

- **Le secret ne se pose nulle part** : celui d'un ajout arrive dans la trame,
  celui d'une révélation ne part **que** dans le corps de la réponse HTTP.
  Aucun journal ne porte la trame ni le résultat (seulement l'opération,
  l'issue et le statut HTTP) ; une exception imprévue rend son NOM de classe,
  jamais son texte.
- **Hors de la boucle WebSocket** : le coffre est synchrone (verrou de
  fichier) et un gestionnaire externe peut être lent — chaque commande est
  une tâche, chaque appel au coffre passe par `asyncio.to_thread`.
- **1Password / Bitwarden** : listés seulement s'ils sont déverrouillés sans
  invite (`source: onepassword|bitwarden`) ; jamais supprimés ni révélés
  depuis Pulse (refus `not_allowed`). Cartes et adresses : listées, jamais
  révélées.
- **Plusieurs profils** : chaque commande s'exécute dans le HERMES_HOME du
  profil qu'elle vise (même règle que la fiche de capacités), et l'instantané
  porte `profile`, que l'app contraint aux profils de la session.
- L'app attend la réponse **10 s** ; au-delà elle répond « le bot n'a pas
  répondu, rien n'a été modifié » et jette une réponse tardive.

**Diagnostic — « l'onglet dit : posez un secret d'agent ».** Le bot est
connecté par `PULSE_CHAT_TOKEN` seul : poser `PULSE_CHAT_AGENT_TOKEN`, puis
`hermes gateway restart`.

**Diagnostic — « l'onglet dit capacité absente ».** La capacité n'est annoncée
que si `import agent.vault_store` réussit : vérifier l'image Hermes
(≥ v2026.9.11), puis la version réellement chargée (`hermes plugins list`,
≥ 1.18.0, et **aucun dossier de sauvegarde** du plugin sous
`/opt/data/plugins/`).

### Navigateur des agents (fournisseur `pulse`, `pulse_browser_handoff`)

À partir de la **1.12.0**, le plugin enregistre un **fournisseur de
navigateur** Hermes nommé `pulse` (`ctx.register_browser_provider`, Hermes
**≥ v2026.9.24**). Les outils de navigation d'Hermes — `browser_exec`
(Browser Use) comme `browser_*` (agent-browser) — pilotent alors le Chromium
**hébergé par l'app** : celui qui porte le profil prêté par la personne qui
parle à l'agent, que les membres du canal voient en direct et dont un humain
peut prendre la main. Le plugin ne décide rien : il demande une session à
l'app et rend l'URL CDP à Hermes, qui pilote sans modification.

> ⚠️ **Réglage bot OBLIGATOIRE** — sans lui, RIEN n'appelle l'app et l'agent
> navigue en local, sans profil ni vue en direct. Hermes ne choisit **jamais**
> un fournisseur de plugin par auto-détection :
>
> ```yaml
> # ~/.hermes/config.yaml (de CHAQUE profil qui doit naviguer par Pulse)
> browser:
>   cloud_provider: pulse
> ```
>
> Prérequis : app Pulse Chat **≥ 0.37.0** (routes `/api/agent/browser/*`,
> relais `/ws/browser-cdp`, trame `browser.control`) avec le navigateur
> configuré (`BROWSER_RUNNER_URL`…), et un **secret d'agent**
> (`PULSE_CHAT_AGENT_TOKEN`) : l'app exige une session `verified` et refuse
> une identité auto-déclarée (`agent_credential_required`).

```
create_session(task_id)        # fil d'outil : canal lu dans HERMES_SESSION_CHAT_ID
    -> POST /api/agent/browser/sessions {channel}
    <- {sessionId, cdpUrl, profile}  ->  {session_name, bb_session_id: sessionId, cdp_url, features}
close_session(sessionId)       # fin de tour / concierge, SANS contexte de session
    -> DELETE /api/agent/browser/sessions/:id      (LIBÈRE : le Chromium reste ouvert)
pulse_browser_handoff(reason)  # outil, toolset "pulse_chat"
    -> POST /api/agent/browser/sessions/:id/handoff {reason}
    <- trame WS browser.control {controller: "agent"}  ->  {status: "done"}
    <- rien en 270 s                                   ->  {status: "pending"}
browser.control {controller: "agent", event, turnEnded, channelSlug}   # ≥ 1.14.0
    sans outil en attente, tour fini OU handoff en pending
        ->  message entrant « [Navigateur] … t'a rendu la main … »
```

Ce qui ne se devine pas :

- **Un refus de l'app ne se voit pas.** Si `create_session` lève, Hermes
  retombe **sans bruit** sur son Chromium local (`browser_tool_session.py`).
  Le `RuntimeError` porte le code de l'app (`browser_unavailable`,
  `browser_capacity`, `agent_credential_required`…) : devant « l'agent a
  navigué sans mon profil », lire les **journaux du bot** (« Cloud provider
  PulseBrowserProvider failed … »). Une URL privée ou de réseau local est
  aussi routée d'office en local par Hermes (`auto_local_for_private_urls`),
  sans passer par nous.
- **Après un 501 (`browser_unavailable`), plus aucun appel pendant 5 min** :
  `create_session` lève immédiatement, Hermes navigue en local comme avant.
  `is_available()` reste pourtant **vrai**, et c'est délibéré : en mode cloud,
  c'est le `check_fn` des outils `browser_*` — faux, Hermes les **retire** du
  schéma au lieu de naviguer en local, et l'agent perd la navigation entière.
  `is_available()` ne lit que la configuration (URL + jeton), jamais le réseau.
- **`close_session` libère, il ne ferme pas.** Hermes l'appelle à chaque fin
  de tour ; fermer ferait perdre les onglets d'un message à l'autre et couper
  la personne en train de se connecter. Le tour suivant reprend la session
  chaude si le demandeur est le même (l'app décide). 404 = déjà fermée = succès ;
  aucune exception ne remonte au concierge.
- **Le canal n'est pas un paramètre**, et une conversation d'une AUTRE
  plateforme (bot Telegram + Pulse) ne poste rien : `create_session` lève et
  Hermes navigue en local.
- **L'outil est synchrone** : HTTP bloquant puis attente d'un
  `concurrent.futures.Future` que la boucle du WebSocket résout à la trame
  `browser.control` — seul le retour à l'agent réveille, jamais la prise de
  main elle-même. 270 s au plus (sous les plafonds d'Hermes) ; `pending` dit au
  modèle de ne pas insister et de prévenir l'humain **par écrit**. Sans
  navigateur Pulse ouvert pour la tâche, l'outil répond « ouvre d'abord le
  navigateur ». Il n'apparaît que sur un bot réglé en `cloud_provider: pulse`,
  et le paragraphe « Browser » de `platform_hint` aussi — lu UNE fois, à
  l'enregistrement : changer le réglage demande un redémarrage de la passerelle.
- **Rendre la main RELANCE l'agent (≥ 1.14.0).** Passé les 270 s, l'outil a
  rendu `pending` et l'agent s'est arrêté ; un humain qui rend la main plus
  tard — ou qui l'avait prise alors que l'agent n'attendait rien et que son
  tour est fini — ne réveillait personne, et l'agent ne reprenait jamais. La
  trame porte désormais `event` (`released` · `auto_released` · `closed`),
  `turnEnded` et `channelSlug`/`channelName` : si aucun outil n'attend, que
  l'`event` est un rendu, et que le tour est fini **ou** qu'un handoff de
  cette session avait rendu `pending`, le plugin injecte un **message
  entrant** `[Navigateur] …` dans le canal (même chemin que la décision
  tardive d'une approbation). Un tour qui tourne encore n'est pas
  interrompu ; `closed` et une trame sans `event` (app antérieure) n'injectent
  rien — le comportement d'avant. **Déployer l'app ≥ 0.39.0 AVANT** : c'est
  elle qui envoie ces champs, et sans eux la consigne de `pending` (« un
  message te relancera aussi ») ne serait tenue que par l'humain. Le tour
  injecté emporte le dernier `agentConfig` reçu dans le canal (ton,
  consignes, `disabledTools`), et une nouvelle ouverture de session oublie le
  `pending` du tour précédent (sinon un rendu en plein tour suivant
  l'interromprait).
- **Nom `pulse`, jamais `browser-use`** : Browser Use saute le fournisseur qui
  porte exactement ce nom.

### Plan de tâches de l'agent (hook `post_tool_call`)

À partir de la **1.13.0**, la liste d'étapes interne d'Hermes (`todo_list`,
alias `todo` avant v2026.9.7) apparaît dans le fil comme une carte « Plan de
tâches » : barre d'avancement, étape en cours, checklist au dépli.

- **Le texte n'en dit rien** : côté plateforme, Hermes n'envoie qu'une ligne
  `📋 Updating tasks planning 3 task(s)`. Le plugin enregistre un hook
  `post_tool_call` et lit le **résultat** de l'outil, qui porte toujours la
  liste complète (`{"todos": [...], "revision": n, …}`).
- **Filtré au premier test** sur `todo_list` / `todo` : tout autre outil ressort
  sans rien lire. Sessions **Pulse Chat seulement** (`HERMES_SESSION_PLATFORM ==
  pulse_chat`, plateforme vide comprise dans les refus), enfants de
  `delegate_task` ignorés. Le hook tourne sur un thread `hermes-hook-*` sous
  `copy_context()` : le contexte de session y est visible, le canal en vient.
- **Une carte par tour** : `hermesMessageId = todo:<turn_id>` (repli `task_id`,
  puis `session_id`) — l'app met la carte à jour tant que le tour dure. Les
  envois sont planifiés sur la boucle du WebSocket sans attendre et
  **sérialisés** (un plan ne doit jamais « reculer » parce que deux POST se sont
  doublés). Un plan vide sans carte ce tour-ci n'en crée pas.
- **Rien n'est décidé ici** : la phase (en cours / terminé), les bornes et le
  compteur sont dérivés par l'app. Un résultat illisible ne produit aucune
  carte et ne lève jamais dans Hermes. Face à une app antérieure au champ
  `todos`, le POST est refusé en 400 (une fois journalisé) : rien d'autre ne
  change.

### Router un envoi vers un canal Pulse Chat

Le plugin déclare son propre parseur de cible
(`parse_target_ref_fn`, Hermes ≥ **v2026.8.13**) : `pulse_chat:<slug>` est
résolu tel quel, sans thread.

C'est ce qui **remplace le patch** qui réécrivait
`/opt/hermes/tools/send_message_tool.py` à l'installation. Hermes résout une
cible en trois temps — parseur du plugin, règles génériques, annuaire de
canaux — et un slug Pulse Chat (`general`, `rt-1a2b3c4d`) n'est ni numérique
ni une syntaxe native connue : il tombait dans l'annuaire, qui ne contient
aucune entrée Pulse Chat, et l'envoi échouait sur un « Could not resolve »
alors que le canal existe. La règle de routage vit désormais chez le plugin
qui possède la syntaxe, au lieu d'une copie modifiée du cœur d'Hermes qu'il
faut réappliquer à chaque version.

Le slug **n'est pas validé** ici : l'app est la seule autorité sur l'existence
d'un canal et sur le droit d'y écrire (404). Un motif de slug codé dans le
plugin serait une seconde règle à tenir d'accord avec l'app.

**Hermes plus ancien** : le kwarg est inconnu de `PlatformEntry` et
`register_platform` le fait remonter en `TypeError`. Le plugin **réessaie sans
lui** et journalise un avertissement — sans ce repli, le bot ne perdrait pas
le routage, il perdrait l'enregistrement de la plateforme entière. Dans ce
mode dégradé, un `send_message` / une livraison de cron `deliver=` vers
`pulse_chat:<slug>` échoue ; **les réponses dans le canal ne sont pas
affectées** (elles passent par le WebSocket et `POST /api/agent/messages`).

> ⚠️ Depuis **v2026.9.7**, Hermes n'expose plus `send_message` comme outil
> appelable par le modèle (`toolsets.py` : « there is deliberately no
> agent-callable send_message tool »). Le parseur sert donc les chemins **sans
> modèle dans la boucle** : cron `deliver=`, CLI, `react`/`unreact`.
> Réexposer la capacité au modèle est une décision séparée, à prendre côté
> plugin.

## Installation (machine du bot)

```bash
# 1. Déposer le plugin
mkdir -p ~/.hermes/plugins
cp -r hermes-plugin/pulse-chat ~/.hermes/plugins/pulse-chat

# 2. Dépendance Python (WebSocket client)
pip install websockets

# 3. Activer le plugin
hermes plugins enable pulse-chat
```

### Variables d'environnement

| Variable | Requise | Description |
|---|---|---|
| `PULSE_CHAT_URL` | oui | URL de base de l'app (ex. `https://chat.pulsemyit.fr`) |
| `PULSE_CHAT_TOKEN` | oui | Token de service (WS + API), secret |
| `PULSE_CHAT_PROFILE` | non | Profil(s) Hermes servis par ce bot, séparés par des virgules (défaut `default`) — routage par `Channel.hermesProfile` |
| `PULSE_CHAT_AGENT_NAME` | non | Nom d'affichage envoyé dans la frame hello (défaut : nom du premier profil) |
| `PULSE_CHAT_CHANNELS` | non | Slugs autorisés séparés par des virgules (vide = tous) |
| `PULSE_CHAT_ALLOW_ALL_USERS` | non | Mettre `true` : l'accès est déjà filtré côté app via `ChannelMember` (ne pas dupliquer la règle) |
| `PULSE_CHAT_MEDIA_HOSTS` | non | Hôtes supplémentaires d'où les images peuvent être téléchargées, séparés par des virgules. Défaut : l'hôte de `PULSE_CHAT_URL` seul. **À poser** si le bot joint l'app par une adresse interne (`http://pulse-chat:3000`) alors que les liens de pièces jointes portent le domaine public (`AUTH_BASE_URL`, ex. `chat.pulsemyit.fr`) |
| `PULSE_CHAT_ALLOW_INSECURE` | non | `1` pour accepter `http`/`ws` en clair vers un hôte **public**. Sans lui, le clair n'est accepté que vers un hôte local (loopback, IP privée, nom de service Docker sans point, `.local`/`.internal`/`.lan`/`.test`/`.localhost`) |

**Garde-fous réseau (1.15.2).** Toute requête du plugin passe par un ouvreur
HTTP(S) seul (`netguard.py`) : ni `file://` ni `ftp://`, aucune redirection
hors de l'origine demandée (urllib recopierait sinon `Authorization` vers le
nouvel hôte), clair refusé vers un hôte public — la connexion WebSocket échoue
alors avec `insecure_transport`, sans rejeu. Les médias ne sont suivis que vers
l'hôte de l'app (ou `PULSE_CHAT_MEDIA_HOSTS`), et les journaux n'impriment
jamais la query d'une URL (la signature d'un lien de téléchargement y vit).
`pulse_vault_write` refuse de déposer un fichier de secrets évident (`.env*`,
`auth.json`, `.netrc`, `~/.ssh`, `~/.aws`, `/proc`…) : le coffre est
téléchargeable par tous les membres du canal.

### `~/.hermes/config.yaml`

```yaml
display:
  tool_progress: all        # l'activité outils est une fonctionnalité du produit

gateway:
  platforms:
    pulse_chat:
      enabled: true

plugins:
  enabled:
    - pulse-chat

browser:
  cloud_provider: pulse     # OBLIGATOIRE pour le navigateur des agents (≥ 1.12.0)
```

Pour que l'agent **parle** (notes vocales), ajouter au même fichier :

```yaml
voice:
  auto_tts: true            # répondre en audio à un message AUDIO entrant
                            # (jamais à un message texte : le déclencheur Hermes
                            #  est le type VOICE du message reçu)
tts:
  provider: mistral         # Voxtral TTS — clé MISTRAL_API_KEY
  mistral:
    model: voxtral-mini-tts-2603
    # voice_id: 5a271406-039d-46fe-835b-fbbb00eaf08d   # fr_marie_neutral (défaut)
    # ⚠️ une voix `en_*` prononce le français avec un accent anglophone :
    #    le catalogue compte 6 voix `fr_fr` (« Marie »), 24 voix anglaises.
stt:
  provider: mistral         # Voxtral Transcribe — même clé
  language: fr
```

**Hermes ≥ v2026.9.24 : plugin ≥ 1.15.1 obligatoire.** Depuis cette version,
Hermes applique `voice.auto_tts: true` à **tout** message, tapé compris : chaque
réponse écrite partait doublée d'une note vocale (postée juste avant le texte,
sans légende). Le plugin 1.15.1 borne l'auto-TTS aux tours `voice` — c'est-à-dire
à l'appel — via `_should_auto_tts_for_chat`. Ne pas passer `auto_tts` à `false`
à la place : c'est ce réglage qui donne sa voix à l'agent pendant un appel.

Le toolset `tts` doit être actif pour que l'agent puisse aussi parler de sa
propre initiative (`text_to_speech`) ; l'auto-TTS, lui, n'en dépend pas.
Le SDK `mistralai==2.4.8` est installé à la demande par Hermes : dans un
conteneur, le figer dans l'image (sinon réinstallé à chaque reconstruction).

Puis redémarrer : `hermes gateway restart`.
Debug de la découverte plugin : `HERMES_PLUGINS_DEBUG=1`.

## Déploiement dans le conteneur du bot (spec §9)

Le plugin est déployé **dans le conteneur du bot Hermes** :

1. copier le dossier vers `~/.hermes/plugins/pulse-chat/` dans l'image ou via
   un volume monté ;
2. installer la dépendance : `pip install websockets` ;
3. fournir `PULSE_CHAT_URL` / `PULSE_CHAT_TOKEN` (+ options) dans l'env du
   conteneur — `PULSE_CHAT_TOKEN` doit correspondre au
   `NUXT_HERMES_SERVICE_TOKEN` de l'app ;
4. activer sur le bon profil : `hermes -p <profil> plugins enable pulse-chat` ;
5. redémarrer le gateway.

**Multi-bots** : un conteneur Hermes par bot, chacun avec son
`PULSE_CHAT_PROFILE` (ex. `client-a`) et son `PULSE_CHAT_AGENT_NAME`. L'app
route chaque canal vers le bot de son `hermesProfile` ; les messages d'un
profil sans bot connecté restent en file et sont rejoués au retour de CE bot.
Un plugin ancien (sans frame hello) est enregistré sur `default` après un
délai de grâce (`HERMES_HELLO_GRACE_MS` côté app, 5 s par défaut).

## Tests

Sans hermes installé (la classification est un module pur) :

```bash
python3 -m pytest hermes-plugin/ -q
```
