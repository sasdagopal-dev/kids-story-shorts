#!/usr/bin/env bash
# Downloads Blender 3.6 LTS safely (checks the file is a REAL archive, tries several mirrors).
set -u
FOLDER="Blender3.6"
FILE="blender-3.6.5-linux-x64.tar.xz"
DEST="$HOME/blender"

if [ -x "$DEST/blender" ] && "$DEST/blender" -b --version >/dev/null 2>&1; then
  echo "Blender already installed (cache)."
  "$DEST/blender" -b --version
  exit 0
fi
mkdir -p "$DEST"

BASES=(
  "https://download.blender.org/release"
  "https://mirrors.ocf.berkeley.edu/blender/release"
  "https://mirror.clarkson.edu/blender/release"
  "https://ftp.nluug.nl/pub/graphics/blender/release"
  "https://mirrors.dotsrc.org/blender/blender-release"
)
ok=0

# Optional: your own copy in the GitHub Release called "assets" (most reliable)
if [ -n "${GH_TOKEN:-}" ] && command -v gh >/dev/null 2>&1; then
  rm -f "$FILE"
  if gh release download assets --repo "$GITHUB_REPOSITORY" --pattern "$FILE" --dir . --clobber 2>/dev/null; then
    if xz -t "$FILE" 2>/dev/null; then echo "Using Blender from your Release assets."; ok=1; fi
  fi
fi

for round in 1 2 3; do
  [ "$ok" = 1 ] && break
  for base in "${BASES[@]}"; do
    echo "Trying: $base/$FOLDER/$FILE"
    rm -f "$FILE"
    if curl -fL --retry 3 --retry-delay 5 --connect-timeout 30 --max-time 900 \
         -A "Mozilla/5.0 (X11; Linux x86_64)" -o "$FILE" "$base/$FOLDER/$FILE"; then
      if xz -t "$FILE" 2>/dev/null; then
        echo "Download is a valid archive ($(du -h "$FILE" | cut -f1))."
        ok=1
        break
      else
        echo "Not a real archive (probably a web page). First bytes:"
        head -c 200 "$FILE"; echo
      fi
    fi
  done
  if [ "$ok" != 1 ]; then
    echo "Round $round failed. Waiting $((round * 20)) seconds."
    sleep $((round * 20))
  fi
done

if [ "$ok" != 1 ]; then
  echo "::error::Could not download Blender from any mirror."
  exit 1
fi
tar -xf "$FILE" -C "$DEST" --strip-components=1
rm -f "$FILE"
"$DEST/blender" -b --version
