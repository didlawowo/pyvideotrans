"""Contrat commun à l'interface HTTP et aux outils Hermes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["vtv", "sts", "stt", "tts"] = "stt"
    file_id: str = Field(min_length=1, max_length=255)
    source_language: str = "auto"
    target_language: str = "fr"
    recogn: str = "0"
    translate: str = "0"
    tts: str = "0"
    model: str = Field(default="large-v3-turbo", min_length=1, max_length=200)
    voice_role: str = "No"
    remove_noise: bool = False
    fix_punc: Literal[0, 1, 2] = 0
    subtitle_type: Literal[0, 1, 2, 3, 4] = 2


def input_path(directory, file_id):
    """Résout un fichier direct du répertoire partagé, jamais un chemin arbitraire."""
    from pathlib import Path

    if Path(file_id).name != file_id or "\\" in file_id or file_id in (".", ".."):
        raise ValueError("Identifiant de fichier invalide")
    path = (directory / file_id).resolve()
    if path.parent != directory.resolve() or not path.is_file():
        raise ValueError("Fichier introuvable dans le répertoire des entrées")
    return path
