#!/usr/bin/env bash

set -euo pipefail

# Versions
SELECT2_VERSION="4.1.0-beta.1"
FULLCALENDAR_VERSION="6.1.17"
JQUERY_VERSION="3.6.0"
LEAFLET_VERSION="1.9.4"

# Define vendor files: name | url | output path
declare -a VENDORS=(
  "Select2 CSS|https://cdnjs.cloudflare.com/ajax/libs/select2/${SELECT2_VERSION}/css/select2.min.css|src/static/styles/select2.min.css"
  "Select2 JS|https://cdnjs.cloudflare.com/ajax/libs/select2/${SELECT2_VERSION}/js/select2.min.js|src/static/scripts/select2.min.js"
  "jQuery|https://code.jquery.com/jquery-${JQUERY_VERSION}.min.js|src/static/scripts/jquery.min.js"
  "Leaflet CSS|https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.css|src/static/styles/leaflet.css"
  "Leaflet JS|https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.js|src/static/scripts/leaflet.js"
  "Leaflet layers image|https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/images/layers.png|src/static/styles/images/layers.png"
  "Leaflet layers retina image|https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/images/layers-2x.png|src/static/styles/images/layers-2x.png"
  "Leaflet marker icon|https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/images/marker-icon.png|src/static/styles/images/marker-icon.png"
  "Leaflet marker shadow|https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/images/marker-shadow.png|src/static/styles/images/marker-shadow.png"
  "Chart JS|https://cdn.jsdelivr.net/npm/chart.js|src/static/scripts/chart.js"
  "Fullcalendar JS|https://cdn.jsdelivr.net/npm/fullcalendar@${FULLCALENDAR_VERSION}/index.global.min.js|src/static/scripts/calendar.min.js"
)

# Download each vendor file
for vendor in "${VENDORS[@]}"; do
  IFS='|' read -r name url output <<< "$vendor"
  echo "Updating $name..."
  if curl -sL "$url" -o "$output"; then
    echo "  ✓ Downloaded to $output"
  else
    echo "  ✗ Failed to download $name" >&2
    exit 1
  fi
done

echo "All vendor files updated."
