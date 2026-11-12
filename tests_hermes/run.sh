#!/bin/sh
# Lance tests_hermes/ contre un VRAI Hermes, epingle par son commit.
#
#   tests_hermes/run.sh <commit Hermes>
#
# Exige `uv` et un Python 3.14 (`PYTHON`, defaut python3.14) : Hermes ne
# declare ses dependances que pour Python >= 3.14, et son uv.lock (installe
# tel quel, --frozen) est la seule resolution qu'il garantisse. Tout se passe
# dans un dossier temporaire : HOME et HERMES_HOME y pointent, aucun secret,
# aucun reseau hors telechargement de Hermes et de ses dependances.
set -eu

sha="${1:?usage: tests_hermes/run.sh <commit Hermes>}"
python="${PYTHON:-python3.14}"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl -fsSL "https://github.com/NousResearch/hermes-agent/archive/${sha}.tar.gz" -o "$work/hermes.tgz" 2>/dev/null \
  || node -e "fetch(process.argv[1]).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.arrayBuffer()}).then(b=>require('fs').writeFileSync(process.argv[2],Buffer.from(b)))" \
       "https://github.com/NousResearch/hermes-agent/archive/${sha}.tar.gz" "$work/hermes.tgz"
mkdir -p "$work/src" "$work/home/.hermes"
tar xzf "$work/hermes.tgz" -C "$work/src" --strip-components=1

(cd "$work/src" && UV_PYTHON="$python" uv sync --frozen --no-dev -q)
"$work/src/.venv/bin/python" -m ensurepip -q >/dev/null 2>&1 || true
uv pip install -q --python "$work/src/.venv/bin/python" pytest

echo "Hermes ${sha}"
HERMES_MATRIX_HOME="$work/home/.hermes" \
  "$work/src/.venv/bin/python" -m pytest -q -p no:cacheprovider "$here"
