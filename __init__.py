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
