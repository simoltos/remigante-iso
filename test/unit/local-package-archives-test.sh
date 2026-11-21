#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/../../builder/local-package-archives.sh"
cache=$(mktemp -d)
trap 'rm -rf -- "$cache"' EXIT
for archive in omarchy-4.5-1-x86_64.pkg.tar.zst omarchy-next-2-any.pkg.tar.xz \
    omarchy-settings-4.5-1-x86_64.pkg.tar.zst omarchy-themes-intros-2-1-any.pkg.tar.zst; do
  touch "$cache/$archive" "$cache/$archive.sig"
done
remove_local_package_archives "$cache" omarchy
for archive in omarchy-settings-4.5-1-x86_64.pkg.tar.zst omarchy-themes-intros-2-1-any.pkg.tar.zst; do
  [[ -f $cache/$archive && -f $cache/$archive.sig ]]
done
[[ $(find "$cache" -type f | wc -l) == 4 ]]
