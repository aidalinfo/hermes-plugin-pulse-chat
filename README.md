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
└── tests/              # pytest (sans hermes installé)
```


## Installation rapide (repo publié)

Ce plugin est publié sur **github.com/aidalinfo/hermes-plugin-pulse-chat**
(miroir automatique du monorepo `PROJET-pulse-chat`, dossier `hermes-plugin/pulse-chat/`).

Voir aussi [`SKILL.md`](./SKILL.md) : ce que **l'agent** doit savoir pour se
servir de Pulse Chat une fois branché (outils MCP `/mcp-hermes`, le piège
`plan_*` vs `tasks_*`, le coffre-fort, les approbations, et
`routine_deliver` — le seul chemin par lequel une réponse de routine sort de
sa salle de travail). Format non vérifié dans une image Hermes réelle : voir
l'avertissement en tête du fichier.

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
