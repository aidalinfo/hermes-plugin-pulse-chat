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
    from .voxtral_streaming import install as _install_voxtral_streaming

    _install_voxtral_streaming()
except Exception:  # pragma: no cover - jamais bloquant pour le plugin
    pass
