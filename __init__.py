"""Plugin de plateforme Pulse Chat pour Hermes Agent."""

try:
    from .adapter import register  # noqa: F401
except ImportError as exc:  # pragma: no cover - chemin hors runtime hermes
    # Hors environnement hermes (ex: pytest sur la classification pure, ou
    # module charge sans contexte de paquet), ``gateway.*`` n'existe pas :
    # seul le module pur ``classification`` reste utilisable.
    _missing = (getattr(exc, "name", None) or "").split(".")[0]
    if _missing in ("gateway", "agent") or "no known parent package" in str(exc):
        register = None  # type: ignore[assignment]
    else:
        raise

# Streamer TTS Voxtral : sans lui, un agent en `tts.provider: mistral` ne parle
# qu'une fois la reponse entierement redigee (Mistral n'est pas un streamer
# enregistre en amont, et Hermes refuse de changer de fournisseur en silence).
# Sans effet sur une version d'Hermes anterieure au contrat de streaming, et
# sans effet sur le chat texte : l'echec est journalise, jamais propage.
try:
    import logging as _logging

    from .voxtral_streaming import install as _install_voxtral_streaming

    if _install_voxtral_streaming():
        _logging.getLogger(__name__).info(
            "Pulse Chat: streamer TTS Voxtral enregistre (voix phrase par phrase)"
        )
    else:
        # Retour False = le contrat de streaming n'etait pas importable A CET
        # INSTANT. Ce n'est pas forcement definitif : l'ordre de chargement des
        # plugins peut precéder celui de `tools.tts_streaming`. L'adaptateur
        # retentera au premier tour parle (`begin_streaming_tts`).
        _logging.getLogger(__name__).warning(
            "Pulse Chat: streamer TTS Voxtral NON enregistre au chargement — "
            "nouvelle tentative au premier tour parle"
        )
except Exception as _exc:  # pragma: no cover - jamais bloquant pour le plugin
    # JAMAIS un `pass` muet : c'est precisement ce silence qui a fait chercher
    # une soiree pourquoi l'agent ouvrait une piste audio sans jamais rien y
    # ecrire. L'echec ne bloque pas le plugin, mais il se voit.
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "Pulse Chat: enregistrement du streamer TTS en echec — %s", _exc
    )
