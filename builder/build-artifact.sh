#!/bin/bash
# A normal build creates fresh artifacts. An installer rebuild may reuse its
# package snapshot and system when their inputs and stored bytes still match.
set -euo pipefail
shopt -s inherit_errexit
kind=$1
shift
cache=/build-artifacts

case $kind in
  packages) script=/builder/build-omarchy-packages.sh ;;
  system) script=/builder/build-root-image.sh ;;
  *) echo "Unknown build artifact: $kind" >&2; exit 1 ;;
esac
[[ -d $cache ]] || exec bash "$script" "$@"
source /builder/local-package-archives.sh

package_inputs() {
  local source
  for source in /omarchy-source /omarchy-pkgs; do
    # Recipes may use the working tree (including untracked files) and Git
    # versions. Exclude Git storage, whose packing can change independently.
    if [[ -e $source/.git ]]; then
      git -c safe.directory="$source" -C "$source" rev-parse HEAD
      git -c safe.directory="$source" -C "$source" describe --always --tags
      git -c safe.directory="$source" -C "$source" rev-parse --abbrev-ref HEAD
    fi
    tar --sort=name --exclude=.git --acls --xattrs --pax-option=delete=atime,delete=ctime -C "$source" -cf - .
  done
  sha256sum "$script" /etc/makepkg.conf /builder/local-package-archives.sh
  for conf in /etc/makepkg.conf.d/*.conf; do
    if [[ -f $conf ]]; then sha256sum "$conf"; fi
  done
}

system_inputs() {
  local conf=$1 output=$2 mirror=/var/cache/omarchy/mirror/offline
  shift 2
  printf '%s\0' "${output##*/}" "${OMARCHY_IMAGE_SIZE:-24G}" "$@"
  sha256sum "$script" "$conf" /builder/{attach-loop,follow-renamed-packages,ship-sync-databases}.sh \
    /builder/omarchy-image-write.c "$OMARCHY_IMAGE_SYNC_DBS"/*.db
  for list in "$OMARCHY_RENAMED_PACKAGES" "$OMARCHY_UNRESOLVED_PACKAGES"; do
    printf '%s\0' "$list"
    if [[ -f $list ]]; then sha256sum "$list"; fi
  done
  # repo-add's descriptors carry package filenames, content hashes and
  # signatures. The tar headers of two equivalent indexes can have new dates.
  bsdtar -xOf "$mirror/offline.db.tar.gz" --include='*/desc'
}

input_key() {
  {
    printf '%s\0' "$kind" "$OMARCHY_ARCH" "$OMARCHY_KERNEL" "$OMARCHY_MIRROR" \
      "$OMARCHY_ISO_REF" "$OMARCHY_RUNTIME_PACKAGE" "$OMARCHY_SETTINGS_PACKAGE" \
      "$OMARCHY_NVIM_PACKAGE" "${SOURCE_DATE_EPOCH:-}"
    uname -rm
    pacman -Q
    sha256sum /builder/{build-iso,build-artifact}.sh
    if [[ $kind == packages ]]; then package_inputs; else system_inputs "$@"; fi
  } | sha256sum | cut -d' ' -f1
}

key=$(input_key "$@")
if [[ $kind == packages ]]; then
  directory=$1
else
  directory=$cache
  output=$2
fi
if [[ ${OMARCHY_REBUILD_INSTALLER:-} == 1 && -f $cache/$kind.inputs && $(<"$cache/$kind.inputs") == "$key" ]] &&
    (cd "$directory" && sha256sum --quiet --strict --check "$cache/$kind.sha256"); then
  if [[ $kind == system ]]; then
    cp --reflink=auto "$cache/system.img" "$output"
    rm -rf -- "${OMARCHY_IMAGE_LOCALDB_COPY:?}/local"
    mkdir -p "$OMARCHY_IMAGE_LOCALDB_COPY"
    tar -xzf "$cache/localdb.tar.gz" -C "$OMARCHY_IMAGE_LOCALDB_COPY"
  fi
  echo "Reusing verified $kind from the last build."
  exit 0
fi

# The caller holds the channel's build lock. Invalidate before replacing any
# file so an interrupted build always takes the full path on the next attempt.
rm -f "$cache/$kind.inputs"
bash "$script" "$@"

if [[ $(input_key "$@") != "$key" ]]; then
  echo "The $kind inputs changed during the build. Run the build again." >&2
  exit 1
fi

if [[ $kind == packages ]]; then
  names=("$OMARCHY_RUNTIME_PACKAGE" "$OMARCHY_SETTINGS_PACKAGE" "$OMARCHY_NVIM_PACKAGE")
  (
    cd "$directory"
    for name in "${names[@]}"; do
      mapfile -t archives < <(local_package_archives . "$name")
      (( ${#archives[@]} == 1 )) || { echo "Expected one local archive for $name" >&2; exit 1; }
      sha256sum -- "${archives[0]}"
    done
  ) > "$cache/$kind.sha256"
else
  cp --reflink=auto "$output" "$cache/system.img"
  tar -czf "$cache/localdb.tar.gz" -C "$OMARCHY_IMAGE_LOCALDB_COPY" local
  (cd "$cache" && sha256sum system.img localdb.tar.gz) > "$cache/$kind.sha256"
fi
printf '%s\n' "$key" > "$cache/$kind.inputs.tmp"
mv -f "$cache/$kind.inputs.tmp" "$cache/$kind.inputs"
