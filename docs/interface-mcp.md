# Interface française et MCP Hermes

Cette variante du fork utilise `server.py` (FastAPI et l'interface HTML française),
avec un serveur MCP dans le même processus. Le Gradio upstream reste dans `webui.py`.
Un seul traitement lourd est accepté à la fois, quelle que soit son origine.
Les identifiants des tâches, leurs logs et leurs résultats sont communs au navigateur et à Hermes.

## Build local

`task web:build` construit `pyvideotrans-web-mcp:1.1.0` pour **linux/amd64, CPU**.
Le Dockerfile `Dockerfile.web` réutilise l'image CPU du homelab épinglée par digest,
copie le moteur de cette branche et ajoute des dépendances HTTP/MCP verrouillées avec hashes.
Il ne pousse pas d'image. Le Mac ARM exécute cette image sous émulation.
Le registre de base doit être accessible depuis la machine de build.

Le service écoute sur **9090** dans le conteneur : interface `/`, MCP `/mcp`, probe `/healthz`.
Il refuse de démarrer sans `MCP_AUTH_TOKEN`. Dans le navigateur, saisir ce jeton sur
la page de connexion ; le cookie est HttpOnly, SameSite strict, avec une durée de huit heures.
En HTTPS, le cookie porte aussi Secure. Hermes utilise toujours le Bearer.

Après avoir défini `MCP_AUTH_TOKEN` dans son environnement, lancer :

```sh
docker run --rm --platform linux/amd64 -p 127.0.0.1:9090:9090 -e MCP_AUTH_TOKEN -v pyvideotrans-inputs:/app/uploads -v pyvideotrans-outputs:/app/output -v pyvideotrans-models:/app/models pyvideotrans-web-mcp:1.1.0
```

Les fichiers de configuration locaux (`videotrans/params.json`, `cfg.json`, etc.)
sont exclus du contexte Docker. Ils peuvent contenir des chemins propres à une machine
ou des identifiants : ne pas les embarquer dans une image.

## Whisper et LLM locaux

Sans endpoint externe, sélectionner **faster-whisper (Intégré)** et un modèle multilingue
comme `tiny` pour un essai rapide, ou `large-v3-turbo` pour le traitement habituel.
Le premier lancement télécharge les poids ; les suivants utilisent le cache du volume.

Pour réutiliser des serveurs existants, passer les variables suivantes au conteneur :

| Variable | Valeur attendue | Canal à sélectionner |
|---|---|---|
| `PYVIDEOTRANS_WHISPER_URL` | URL de base compatible OpenAI, terminée par `/v1` | OpenAI Speech to Text (nom traduit dans l'interface) |
| `PYVIDEOTRANS_WHISPER_MODEL` | Identifiant reconnu par le serveur ASR | Même canal |
| `PYVIDEOTRANS_LLM_URL` | URL de base compatible OpenAI, terminée par `/v1` | LLM local / LocalLLM |
| `PYVIDEOTRANS_LLM_MODEL` | Identifiant exact annoncé par le serveur | Même canal |

Ces variables ne rendent pas un endpoint existant disponible : son routage et son modèle
doivent être opérationnels. Les clés des fournisseurs restent celles de la configuration
upstream ; `no-key` est utilisé si aucune clé n'existe, pour les services locaux sans auth.
Les paramètres de fournisseurs ne sont pas modifiables par un outil MCP.
`PYVIDEOTRANS_INPUT_DIR`, `PYVIDEOTRANS_OUTPUT_DIR` et `PYVIDEOTRANS_MAX_UPLOAD_MB`
permettent de changer les répertoires et la limite d'envoi (2048 Mio par défaut).

## Connexion Hermes

Le MCP suit le [SDK Python officiel, branche v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x),
épinglé à `mcp==1.30.0`, en Streamable HTTP sans session persistante.
Exemple de configuration **après** publication et adaptation du Service Kubernetes :

```yaml
mcp_servers:
  pyvideotrans:
    enabled: true
    url: http://pyvideotrans.pyvideotrans.svc:9090/mcp
    headers:
      Authorization: Bearer ${PYVIDEOTRANS_MCP_TOKEN}
    timeout: 60
```

Dans le chart Hermes du homelab, déclarer ce serveur dans `config.mcpServers`
(le fichier de configuration est géré par GitOps, ne pas modifier son PVC à la main) :

```yaml
config:
  mcpServers:
    pyvideotrans:
      profiles: [default, devops]
      url: http://pyvideotrans.pyvideotrans.svc:9090/mcp
      authHeader: Bearer ${PYVIDEOTRANS_MCP_TOKEN}
      timeout: 60
```

Flux utilisateur/agent :

1. Déposer le média via l'interface ou `POST /api/upload` multipart, champ `file`,
   en-tête `Authorization: Bearer ...`. La réponse contient `file_id`.
   Un média joint à Hermes reste sur la machine Hermes : il faut l'envoyer par HTTP
   ou le déposer dans le volume partagé des entrées avant l'appel MCP.
2. `list_inputs` retrouve les fichiers disponibles ; `get_options` décrit les canaux/langues.
3. `start_task` lance le travail et rend immédiatement `{uuid, status}`.
4. `get_task` permet de suivre `running` jusqu'à `done` ou `error`.
5. `read_subtitles` lit le SRT/TXT d'une tâche terminée ; les sorties binaires ou textes
   au-delà de 1 Mio se téléchargent par l'URL relative de `outputs`, avec Bearer.
6. `list_tasks` expose l'historique récent. La resource `pyvideotrans://options` est en lecture seule.

Exemple d'arguments de `start_task`, avec le vrai identifiant reçu à l'upload :

```json
{"request":{"file_id":"IDENTIFIANT_RETOURNE.wav","mode":"stt","source_language":"fr","recogn":"0","model":"large-v3-turbo"}}
```

Les modes `sts`, `vtv`, `tts` réutilisent respectivement la traduction SRT, la vidéo
complète et le doublage upstream. Pour `tts`, fournir un vrai `voice_role`, pas le nom du canal.

## Vérification

Côté agent : `task web:check PYTHON=/chemin/python` valide le lint, le formatage et
les tests ciblés (état, auth, confinement des fichiers, concurrence, handshake, outils et resource).
L'environnement Python doit déjà disposer du moteur upstream et des dépendances de
`requirements-web.lock`. `tests/test_webui_i18n.py` vérifie séparément la localisation Gradio.

`task web:smoke` teste un service déjà lancé, avec `WEB_URL` (localhost par défaut)
et `MCP_AUTH_TOKEN` dans l'environnement. Ajouter `AUDIO=/chemin/extrait.wav`
pour lancer une véritable transcription CPU avec `tiny`, puis lire le SRT via MCP et HTTP.
Ce test crée une tâche et un fichier : le lancer uniquement sur l'instance de test choisie.

Validation réalisée le 13 septembre 2026 : build CPU local, auth et handshake sur les deux
formes `/mcp` et `/mcp/`, six outils, resource, transcription réelle d'un audio synthétique
anglais avec faster-whisper/tiny, lecture du SRT par MCP, téléchargement HTTP, contrôle Brave.
Les modes traduction/TTS et les endpoints LLM/Whisper du cluster n'ont pas été testés de bout en bout.

La CI historique du fork (packaging Windows et fermeture des issues upstream) est retirée
de cette branche Web. Aucun runner hébergé GitHub n'est utilisé. Une CI ARC doit être
enregistrée dans continuous-delivery avant d'ajouter un workflow à ce fork.

## Migration — côté utilisateur

Le provisioning et la connexion cluster sont suivis dans
[continuous-delivery : Bearer MCP pyvideotrans](https://github.com/didlawowo/continuous-delivery/issues/453).

Le build local ne modifie ni le registre ni la prod. Publication de l'image, adaptation
du chart, sync ArgoCD et vérification de bout en bout sur le cluster restent côté utilisateur.
Le chart actuel de continuous-delivery doit être adapté avant de sélectionner cette image :

- `charts/pyvideotrans/values.yaml` : image/version validées, port Service et targetPort 9090.
- `charts/pyvideotrans/templates/deployment.yaml` : probes sur `/healthz`, injection
  `MCP_AUTH_TOKEN` depuis le Secret provisionné, volume d'entrées persistant si nécessaire.
- **Ne pas conserver le montage du PVC config sur tout `/app/videotrans`** : il masque
  le code neuf avec l'ancien package. Sauvegarder ses données puis monter uniquement
  les fichiers de configuration nécessaires par `subPath`, après initialisation contrôlée.
- `infrastructure-crds/rke2-mlops/hermes.yaml` : enregistrer le MCP et injecter son token
  dans les profils souhaités. Le hostname dédié MCP peut router vers le même Service.

L'injection serveur est :

```yaml
- name: MCP_AUTH_TOKEN
  valueFrom:
    secretKeyRef:
      name: pyvideotrans-mcp
      key: token
```

## Risques

L'état des tâches est en mémoire (100 tâches, 2000 logs/événements par tâche) : un redémarrage
interrompt le travail et perd l'historique. Les résultats sur volume persistent. Utiliser
un seul processus et une seule réplique ; le verrou ne coordonne pas plusieurs pods.
Les fichiers d'entrée/sortie ne sont pas purgés automatiquement : prévoir une rétention opérateur.
Le CPU sous émulation sur Mac ARM est plus lent qu'un hôte amd64. Des messages internes du
moteur peuvent rester en chinois. Les dépendances ML de la base sont héritées, seules celles
de la couche Web/MCP sont verrouillées ici.

## Rollback

En local, arrêter le conteneur de test et relancer l'image précédente ; conserver les volumes.
Sur le cluster, l'utilisateur rétablit l'image et le chart antérieurs par GitOps, retire
l'entrée MCP des profils Hermes et restaure la configuration sauvegardée si nécessaire.
Ne pas supprimer les PVC pour revenir en arrière.
