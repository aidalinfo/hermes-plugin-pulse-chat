# Historique des versions

## 1.24.1 — 2026-10-28

- Étale les reconnexions simultanées avec une gigue uniforme de ±50 % autour
  du backoff nominal.
- Conserve le rang de reconnexion après un simple handshake et ne le remet à
  zéro qu'à l'admission `hello.ack` ou lors d'un arrêt volontaire.
- Ajoute des tests déterministes sur les bornes de la gigue, plusieurs clients
  indépendants et les transitions sans admission, avec admission et à l'arrêt.