#!/bin/zsh
# Put the web UI on Vercel, proxying /api and /media to your Modal backend.
#   ./deploy_vercel.sh https://<your-workspace>--twin-web.modal.run
set -e
[[ -z "$1" ]] && { echo "usage: $0 <modal web url printed by 'modal deploy'>"; exit 1; }
cd "$(dirname "$0")/app/static"
sed "s#MODAL_WEB_URL#${1%/}#g" vercel.example.json > vercel.json
vercel deploy --prod --yes
