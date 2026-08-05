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
