#!/bin/sh
# Builds Dockup's image for amd64 and arm64 and loads it into the local
# Docker as dockup:<version>, ready to be run and checked.
#
# Nothing is pushed from here. Releases are built and published by GitHub
# when a version tag is pushed (see "Publishing a new version" in the
# runbook); this is for trying an image on your own machine first.
#
#   tools/build-image.sh                    # build and load locally
#   BUILD_HOST=user@host tools/build-image.sh   # build on another machine
#
# arm64 on an x86 machine needs QEMU registered with binfmt. Check with
# `docker buildx inspect | grep -i platforms`; if linux/arm64 is missing,
# register it once as root with
# `docker run --privileged --rm tonistiigi/binfmt --install arm64`
# (rootless Docker cannot do this - the kernel registration is host-wide).
# BUILD_HOST is the way out when this machine can't: the source is copied
# there, built, and the finished image brought back. SSH_KEY names the key
# to use with it.
#
# PLATFORMS overrides the pair (default linux/amd64,linux/arm64 - a home
# server is as likely to be a Raspberry Pi as an x86 box).
set -eu

cd "$(dirname "$0")/.."
version=$(sed -n 's/^VERSION = "\([0-9][0-9.]*\)"$/\1/p' app/config.py)
[ -n "$version" ] || { echo "No VERSION line in app/config.py" >&2; exit 1; }
platforms=${PLATFORMS:-linux/amd64,linux/arm64}
tag=dockup:$version

if [ -n "${BUILD_HOST:-}" ]; then
  ssh_cmd="ssh"
  [ -n "${SSH_KEY:-}" ] && ssh_cmd="ssh -i $SSH_KEY -o IdentitiesOnly=yes"
  remote_dir=/tmp/dockup-build-$version
  echo "Building $tag on $BUILD_HOST for $platforms"
  rsync -a --delete -e "$ssh_cmd" \
    --exclude .git --exclude data --exclude .publish --exclude __pycache__ \
    ./ "$BUILD_HOST:$remote_dir/"
  # An OCI archive, not `docker save`: save writes only the architecture the
  # build machine happens to be, which would quietly drop arm64 on the way
  # home. The OCI archive carries the whole manifest list.
  $ssh_cmd "$BUILD_HOST" "set -eu; cd $remote_dir && \
    docker buildx build --platform '$platforms' \
      --label org.opencontainers.image.description='Self-hosted Docker Compose stack manager for home labs' \
      --label org.opencontainers.image.version='$version' \
      -t '$tag' --output 'type=oci,dest=$remote_dir.tar' ."
  $ssh_cmd "$BUILD_HOST" "cat $remote_dir.tar" | docker load
  $ssh_cmd "$BUILD_HOST" "rm -rf $remote_dir $remote_dir.tar"
else
  echo "Building $tag for $platforms"
  docker buildx build --platform "$platforms" \
    --label org.opencontainers.image.description="Self-hosted Docker Compose stack manager for home labs" \
    --label org.opencontainers.image.version="$version" \
    -t "$tag" --load .
fi

echo
echo "Loaded $tag. Architectures it holds:"
# `docker image inspect` reports only this machine's platform, and
# `imagetools inspect` reads registries, not the local store. The image
# list's tree view is what actually shows every platform held locally.
docker image ls --tree 2>/dev/null | sed -n "/^$(echo "$tag" | sed 's/[.]/[.]/g')  */,/^$/p" \
  | sed -n 's/^[^ ]* \(linux\/[a-z0-9/]*\) .*/  \1/p' \
  || docker image inspect "$tag" --format '  {{.Os}}/{{.Architecture}}'
echo
echo "Run it, check it, then ask for the push:"
echo "  version $version, local image $tag"
