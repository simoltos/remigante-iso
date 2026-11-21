#!/bin/bash

# Package filenames end in -pkgver-pkgrel-arch. Package names may contain
# dashes, and an unrelated package may start with the entire requested name.
local_package_archives() {
  local directory=$1 name=$2 archive stem
  for archive in "$directory/$name-"*.pkg.tar.*; do
    [[ -f $archive && $archive != *.sig && $archive != *.part ]] || continue
    stem=${archive##*/}
    stem=${stem%.pkg.tar.*}
    stem=${stem%-*}
    stem=${stem%-*}
    [[ ${stem%-*} != "$name" ]] || printf '%s\n' "$archive"
  done
}

remove_local_package_archives() {
  local archive
  while IFS= read -r archive; do
    rm -f -- "$archive" "$archive.sig"
  done < <(local_package_archives "$1" "$2")
}
