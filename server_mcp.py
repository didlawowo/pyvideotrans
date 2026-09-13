"""Outils MCP : mêmes validations et mêmes tâches que l'interface française."""

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from server_models import TaskRequest


def build_mcp(service):
    mcp = FastMCP(
        "pyvideotrans",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @mcp.tool()
    def list_inputs() -> dict[str, Any]:
        """Liste les médias disponibles dans le répertoire partagé des entrées.

        Déposer d'abord un fichier via POST /api/upload (multipart, Bearer) ou
        le volume partagé des entrées. Aucun accès aux autres chemins du serveur.

        Returns:
            files: liste de {file_id, name, size}, utilisables par start_task.
        """
        return service.api_inputs()

    @mcp.tool()
    def get_options() -> dict[str, Any]:
        """Liste les langues, modèles et canaux disponibles pour créer une tâche.

        Returns:
            recogn/translate/tts: listes de noms (indice numérique accepté comme
            chaîne dans start_task), models et languages, sans clé API.
        """
        return service.api_options()

    @mcp.tool()
    def start_task(request: TaskRequest) -> dict[str, Any]:
        """Lance une transcription ou traduction asynchrone et retourne son identifiant.

        Un seul travail lourd est accepté à la fois ; une tâche déjà active produit
        une erreur. Consulter get_task jusqu'à done/error avant de lire les résultats.

        Args:
            request: file_id retourné par list_inputs/upload ; mode stt (transcription),
                sts (SRT traduit), vtv (vidéo traduite), tts (doublage SRT).
                recogn/translate/tts: nom exact ou indice chaîne de get_options.
                source_language: code ISO ou auto ; target_language: code ISO.
                model: modèle ASR ; voice_role: identifiant de voix pour le doublage.

        Returns:
            uuid et status running. Les erreurs de validation sont des erreurs MCP.
        """
        return service.api_task_create(request)

    @mcp.tool()
    def get_task(task_id: str) -> dict[str, Any]:
        """Consulte l'avancement d'une tâche sans attendre sa fin.

        Args:
            task_id: uuid retourné par start_task.

        Returns:
            uuid, status, logs, error et outputs (nom, taille, URL HTTP relative).
            Les téléchargements HTTP nécessitent le même Bearer.
        """
        return service.api_task_get(task_id)

    @mcp.tool()
    def list_tasks() -> dict[str, Any]:
        """Consulte les tâches de ce processus ; l'historique est perdu au redémarrage.

        Returns:
            tasks: les 50 dernières tâches avec uuid, status, mode et name.
        """
        return service.api_tasks()

    @mcp.tool()
    def read_subtitles(task_id: str, filename: str) -> dict[str, Any]:
        """Lit un résultat texte terminé, jusqu'à 1 Mio, pour l'exploiter dans Hermes.

        Args:
            task_id: uuid d'une tâche done.
            filename: nom exact d'un fichier .srt ou .txt présent dans ses outputs.

        Returns:
            name et text UTF-8 ; une sortie trop volumineuse doit être téléchargée.
        """
        return service.read_subtitles(task_id, filename)

    @mcp.resource("pyvideotrans://options")
    def options_resource() -> str:
        """Retourne les options publiques de traitement sous forme de JSON."""
        return json.dumps(service.api_options(), ensure_ascii=False)

    return mcp
